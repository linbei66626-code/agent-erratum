#!/usr/bin/env python3
"""Replay exactly ONE upstream chat request from a closed cloud proxy journal.

Default is plan-only: parse the source journal, verify the unique
`upstream_request` for `--request-id`, verify its method/path/origin and body
bytes, and require that re-normalizing the body is byte-identical. It reads no
key, opens no socket, starts no server and runs no tool.

`--execute --key-file` additionally sends that exact body ONCE through a fresh
CloudAuditProxy (the paced 65s candidate, first send also waits) and writes a
private replay journal plus a metadata-only summary. It never retries, never runs
a tool, and the source task/config/raw are not modified. An HTTP 200 is a
completed replay, not a task PASS.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ae_cloud_proxy import (  # noqa: E402
    CloudAuditProxy, CloudConfig, ORIGIN, normalize_request, read_private_key, source_hashes,
)
from ae_model_proxy import decode_object  # noqa: E402

SCHEMA_VERSION = "ae-cloud-replay-0.1"
CHAT_PATH = "/v1/chat/completions"
EXPECTED_PACE_SECONDS = 65


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _verified_body(record: dict) -> bytes:
    """Verify and return one record's wire body without exposing its content."""
    try:
        raw = base64.b64decode(record["body_base64"], validate=True)
    except (KeyError, ValueError):
        raise ValueError("source record has no valid body_base64") from None
    if record.get("body_bytes") != len(raw) or record.get("body_sha256") != _sha(raw):
        raise ValueError("source record body bytes/hash mismatch")
    if "body_utf8" in record and record["body_utf8"] != raw.decode("utf-8", "strict"):
        raise ValueError("source record body_utf8 mismatch")
    return raw


def load_source_request(journal: Path, request_id: str):
    journal = Path(journal)
    raw_journal = journal.read_bytes()
    records = [decode_object(line) for line in raw_journal.splitlines() if line.strip()]
    if [r.get("sequence") for r in records] != list(range(len(records))):
        raise ValueError("source journal sequence is not contiguous")
    closes = [i for i, r in enumerate(records) if r.get("kind") == "cloud_close"]
    if len(closes) != 1 or closes[0] != len(records) - 1:
        raise ValueError("source journal is not closed by a final cloud_close")
    matches = [r for r in records
               if r.get("kind") == "upstream_request" and r.get("request_id") == request_id]
    if len(matches) != 1:
        raise ValueError("source request id must match exactly one upstream_request")
    record = matches[0]
    if record.get("complete_body") is False:
        raise ValueError("source request body is marked incomplete")
    if record.get("method") != "POST" or record.get("path") != CHAT_PATH:
        raise ValueError("source request is not a POST chat completion")
    if record.get("origin") != ORIGIN:
        raise ValueError("source request origin is not the fixed upstream origin")
    return {"record": record, "raw": _verified_body(record), "request_id": request_id,
            "journal_sha256": _sha(raw_journal), "request_sha256": _sha(_verified_body(record))}


def build_plan(source: dict, config: CloudConfig, config_path: Path, source_journal: Path) -> dict:
    expected, changes = normalize_request(source["raw"], config)
    if expected != source["raw"]:
        raise ValueError("re-normalizing the source body is not byte-identical")
    return {
        "schema_version": SCHEMA_VERSION, "mode": "PLAN_ONLY",
        "source_journal": str(source_journal),
        "source_journal_sha256": source["journal_sha256"],
        "source_request_id": source["request_id"],
        "replay_script_sha256": _sha(Path(__file__).read_bytes()),
        "source_request_sha256": source["request_sha256"],
        "source_request_bytes": len(source["raw"]),
        "config_path": str(config_path),
        "config_sha256": _sha(Path(config_path).read_bytes()),
        "config": asdict(config), "normalization_changes": changes,
        "normalized_matches_source": True,
        "code_sha256": source_hashes(), "pace_seconds": float(config.pace_seconds),
        "network_called": False, "key_read": False,
        "task_success": None, "scientific_result": None,
    }


def _reserve_dir(output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("output directory already exists; no overwrite")
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_dir, 0o700)
    return output_dir


def _save(path: Path, value: dict) -> None:
    with Path(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _summarize(proxy_journal: Path) -> dict:
    records = [json.loads(line) for line in proxy_journal.read_text().splitlines()]
    summary = {"http_status": None, "finish_reason": None, "content_length": None,
               "content_empty": None, "tool_call_count": None, "usage": None,
               "response_model": None, "trace_id": None, "transport_issues": None,
               "exception_type": None, "exception_code": None, "attempts": 0,
               "pace_waits": [], "task_pass": False, "task_success": None,
               "scientific_result": None}
    for record in records:
        kind = record.get("kind")
        if kind == "client_request":
            summary["attempts"] += 1
        elif kind == "pace_wait":
            summary["pace_waits"].append({"first_send": record.get("first_send"),
                                          "waited_seconds": record.get("waited_seconds")})
        elif kind == "upstream_response":
            summary["http_status"] = record.get("http_status")
            summary["trace_id"] = record.get("trace_id")
            try:
                body = decode_object(_verified_body(record))
            except Exception:
                body = None
            if isinstance(body, dict):
                summary["response_model"] = body.get("model")
                summary["usage"] = body.get("usage")
                choices = body.get("choices")
                if isinstance(choices, list) and len(choices) == 1:
                    choice = choices[0]
                    summary["finish_reason"] = choice.get("finish_reason")
                    message = choice.get("message") or {}
                    content = message.get("content")
                    summary["content_length"] = len(content) if isinstance(content, str) else 0
                    summary["content_empty"] = not (content or "").strip() \
                        if isinstance(content, str) else True
                    calls = message.get("tool_calls")
                    summary["tool_call_count"] = len(calls) if isinstance(calls, list) else 0
        elif kind == "cloud_summary":
            summary["transport_issues"] = record.get("transport_issues")
        elif kind == "blocked":
            summary["exception_code"] = record.get("code")
        elif kind == "cloud_close":
            if record.get("request_count"):
                summary["attempts"] = max(summary["attempts"], record["request_count"])
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-journal", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args(argv)
    try:
        config = CloudConfig(**decode_object(args.config.read_bytes()))
        config.validate()
        source = load_source_request(args.source_journal, args.request_id)
        plan = build_plan(source, config, args.config, args.source_journal)
        if args.execute and float(config.pace_seconds) != EXPECTED_PACE_SECONDS:
            raise ValueError("execute requires the reviewed 65 second pacing config")
        out = _reserve_dir(args.output_dir)
        _save(out / "plan.json", plan)
        if not args.execute:
            print(json.dumps({"status": "PLAN_ONLY", "key_read": False, "network_called": False,
                              "task_success": None, "scientific_result": None,
                              "output_dir": str(out)}, ensure_ascii=False))
            return 0
        if args.key_file is None:
            raise ValueError("execute requires --key-file")
        key = read_private_key(args.key_file)
        summary = {"http_status": None, "exception_type": None, "exception_code": None}
        with ExitStack() as stack:
            proxy = CloudAuditProxy(config, out / "replay.private.jsonl", api_key=key)
            stack.callback(proxy.close)
            try:
                proxy.dispatch("POST", CHAT_PATH, source["raw"], "replay", "agent_or_unknown")
            except BaseException as exc:
                summary = {"http_status": None, "exception_type": type(exc).__name__,
                           "exception_code": getattr(exc, "code", None)}
            else:
                summary = {"http_status": None, "exception_type": None, "exception_code": None}
        summary = _summarize(out / "replay.private.jsonl") | {
            "exception_type": summary["exception_type"],
            "exception_code": summary["exception_code"]}
        summary["replay_of_request_id"] = args.request_id
        summary["source_request_sha256"] = source["request_sha256"]
        _save(out / "summary.json", summary)
        ok = summary["http_status"] == 200 and summary["exception_type"] is None
        print(json.dumps({"status": "REPLAY_COMPLETED" if ok else "REPLAY_FAILED",
                          "http_status": summary["http_status"],
                          "content_length": summary["content_length"],
                          "content_empty": summary["content_empty"],
                          "exception_type": summary["exception_type"],
                          "task_success": None, "scientific_result": None,
                          "output_dir": str(out)}, ensure_ascii=False))
        return 0 if ok else 2
    except (Exception, KeyboardInterrupt) as exc:
        # Never echo an exception's message text; only its type and gate code.
        code = getattr(exc, "code", None)
        print(json.dumps({"status": "REPLAY_FAILED", "exception_type": type(exc).__name__,
                          "exception_code": code}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
