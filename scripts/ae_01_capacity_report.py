#!/usr/bin/env python3
"""AE-01 context-capacity diagnostic (offline, stdlib only).

Reads the PRIVATE model-audit proxy journal plus the task-wiring event journal
and reports, per proxy request:

  * the client request (seq, request_id, recorded role, body size, body SHA256),
  * the ``preflight_tokenize`` upstream exchange,
  * the ``token_gate`` verdict (recorded prompt tokens, output reserve, limits),
  * the ``model_inference`` upstream exchange and its ``model_summary``,
  * the rejection code(s), including post-stop 503 attempts that never reached
    the model.

Arm / stage / phase labels are attached ONLY when the same-host UTC timestamps
fall inside an evidence window declared by ``events.jsonl`` (``stage_start`` /
``stage_complete`` for arm+task, ``bridge_event`` ``POST .../messages`` for the
history/task phase). Anything that cannot be placed that way is reported as
``unknown``; it is never guessed.

Token accounting: the only measured token value is the whole-request
``prompt_tokens`` returned by the upstream vLLM ``/tokenize`` call and recorded
in ``token_gate``. No local tokenizer is loaded or downloaded, so per-message
token fields are ``null``; character and UTF-8 byte counts are NOT tokens and
are never rescaled onto the measured total.

This module performs no network, no model call, and no file write at import
time. The CLI refuses to overwrite an existing non-empty output directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

SCHEMA_VERSION = "ae-01-capacity-diagnostic-0.1"
OUTPUT_FILES = ("report.json", "summary.md", "tests.log")

MODELS_MARKER = "being evicted from the BEGINNING of your context window"
COMPACTION_MARKER = "create a detailed summary of the conversation so far"
AUXILIARY_ROLES = {"user_simulator", "evaluator"}

LIMITATIONS = [
    "per-message token counts are null: no local tokenizer for the recorded "
    "profile is available offline, so only the whole-request prompt_tokens "
    "from the upstream /tokenize call is a measured value",
    "character and UTF-8 byte counts are not tokens and are not rescaled onto "
    "the measured prompt_tokens",
    "arm/stage/phase labels come from same-host UTC timestamp windows in "
    "events.jsonl; they are evidence alignment, not a cryptographic binding",
    "input-audit records no complete frame for r2, so this report does not "
    "reclassify r2 validity, score any arm, or certify the run",
    "message identity is the canonical-JSON digest of ALL parsed message fields "
    "(including reasoning_content and tool_calls) and is matched one-to-one by "
    "occurrence count; it is not byte identity of the original transport JSON",
    "tool schema volume is measured at the message-list level; it is not "
    "attributed per tool call",
]


class CliError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# IO helpers
# --------------------------------------------------------------------------

def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise CliError(f"{path}: line {number} is not JSON: {exc}") from None
            if not isinstance(record, dict):
                raise CliError(f"{path}: line {number} is not a JSON object")
            records.append(record)
    return records


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise CliError(f"{path}: not JSON: {exc}") from None
    if not isinstance(value, dict):
        raise CliError(f"{path}: top level is not an object")
    return value


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


# --------------------------------------------------------------------------
# Capture parsing: group journal records by request_id
# --------------------------------------------------------------------------

def group_capture(records: list[dict]) -> tuple[dict, dict, list[dict]]:
    """Return (proxy_open config, groups-by-request_id, ordered groups)."""
    config: dict = {"proxy_config": None, "source_wheel_sha256": None,
                    "automatic_retry": None, "proxy_close": None}
    groups: dict[str, dict] = {}
    order: list[str] = []
    last_request_id: str | None = None

    def group_for(request_id: str) -> dict:
        if request_id not in groups:
            groups[request_id] = {
                "request_id": request_id,
                "client_request": None,
                "client_request_seq": None,
                "token_gate": None,
                "upstream": {},          # purpose -> {"request":..,"response":..}
                "model_summary": None,
                "blocked": None,
                "client_rejected": None,
                "sequence": None,
            }
            order.append(request_id)
        return groups[request_id]

    for record in records:
        kind = record.get("kind")
        if kind == "proxy_open":
            config["proxy_config"] = record.get("config")
            config["source_wheel_sha256"] = record.get("source_wheel_sha256")
            config["automatic_retry"] = record.get("automatic_retry")
            continue
        if kind == "proxy_close":
            config["proxy_close"] = {"request_count": record.get("request_count"),
                                     "blocked": record.get("blocked")}
            continue
        if kind == "client_rejected":
            if last_request_id is None:
                continue
            entry = group_for(last_request_id)
            entry["client_rejected"] = {"code": record.get("code"),
                                        "http_status": record.get("http_status"),
                                        "sequence": record.get("sequence")}
            continue
        request_id = record.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        entry = group_for(request_id)
        last_request_id = request_id
        if entry["sequence"] is None:
            entry["sequence"] = record.get("sequence")
        if kind == "client_request":
            entry["client_request"] = record
            entry["client_request_seq"] = record.get("sequence")
        elif kind == "token_gate":
            entry["token_gate"] = record
        elif kind == "upstream_request":
            slot = entry["upstream"].setdefault(record.get("purpose"), {})
            slot["request"] = record
        elif kind == "upstream_response":
            slot = entry["upstream"].setdefault(record.get("purpose"), {})
            slot["response"] = record
        elif kind == "upstream_response_prefix":
            slot = entry["upstream"].setdefault(record.get("purpose"), {})
            slot["response_prefix"] = record
        elif kind == "model_summary":
            entry["model_summary"] = record
        elif kind == "blocked":
            entry["blocked"] = record
    return config, groups, [groups[rid] for rid in order]


def classify_call(entry: dict) -> str:
    client = entry["client_request"]
    path = (client or {}).get("path", "")
    method = (client or {}).get("method", "")
    upstream = entry["upstream"]
    inference = upstream.get("model_inference")
    gate = entry["token_gate"]
    summary = entry["model_summary"]
    blocked = entry["blocked"]

    if method == "GET" and path == "/v1/models":
        return "provider_models_probe"
    if inference is not None:
        if summary and summary.get("http_status") == 200 \
                and not summary.get("truncation_finish") \
                and summary.get("tokenize_prompt_usage_agreement") is not False:
            return "model_inference_completed"
        return "model_inference_anomalous"
    if blocked is not None:
        code = blocked.get("code")
        if code == "proxy_stopped_after_failure":
            return "rejected_after_proxy_stop"
        if gate is not None and gate.get("passed") is False:
            return "rejected_at_token_gate"
        return "rejected_" + str(code)
    if gate is not None and gate.get("passed") is False:
        return "rejected_at_token_gate"
    return "unknown"


def request_body(entry: dict) -> dict | None:
    client = entry["client_request"]
    if not client:
        return None
    text = client.get("body_utf8")
    if not isinstance(text, str) or not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def message_content_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def classify_message(message: dict) -> tuple[str, dict]:
    """Return (category, evidence). Categories are conservative."""
    role = message.get("role")
    text = message_content_text(message)
    evidence: dict = {"matched_source": None, "marker": None}
    if role == "system":
        return "system_message", evidence
    if role == "user":
        try:
            payload = json.loads(text)
        except ValueError:
            return "user_unrecognized", evidence
        if isinstance(payload, dict):
            source = payload.get("source")
            evidence["matched_source"] = source
            if source == "dataset_history/material":
                return "history_input", evidence
            if source == "current_task":
                return "task_instruction", evidence
            if source == "runtime_user":
                return "runtime_user_message", evidence
        return "user_unrecognized", evidence
    if role == "assistant":
        return "assistant_message", evidence
    if role == "tool":
        return "tool_result", evidence
    return "unknown", evidence


def system_segments(text: str) -> dict:
    """Split the system message at its known serialization seams."""
    segments: dict[str, dict] = {}
    seams = [("profile", "共同起点的用户资料："),
             ("memory_blocks", "<memory_blocks>"),
             ("memory_metadata", "<memory_metadata>")]
    positions = []
    for name, marker in seams:
        index = text.find(marker)
        if index >= 0:
            positions.append((index, name))
    positions.sort()
    if not positions:
        return {"whole": {"chars": len(text), "utf8_bytes": len(text.encode("utf-8"))}}
    segments["prompt_prefix"] = {
        "chars": positions[0][0],
        "utf8_bytes": len(text[:positions[0][0]].encode("utf-8")),
    }
    for order, (index, name) in enumerate(positions):
        end = positions[order + 1][0] if order + 1 < len(positions) else len(text)
        chunk = text[index:end]
        segments[name] = {"chars": len(chunk),
                          "utf8_bytes": len(chunk.encode("utf-8"))}
    return segments


def message_breakdown(messages: list) -> list[dict]:
    """Per-message size facts. Token fields are always null (no tokenizer)."""
    rows = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            rows.append({"index": index, "role": None, "category": "unknown",
                         "content_chars": 0, "content_utf8_bytes": 0,
                         "reasoning_chars": None, "reasoning_utf8_bytes": None,
                         "tool_calls": None, "tool_calls_json_chars": None,
                         "message_json_utf8_bytes": None,
                         "content_sha256": None, "message_identity_sha256": None,
                         "message_identity_basis": None,
                         "prompt_tokens": None, "token_source": None})
            continue
        text = message_content_text(message)
        category, evidence = classify_message(message)
        reasoning = message.get("reasoning_content")
        reasoning = reasoning if isinstance(reasoning, str) else None
        calls = message.get("tool_calls")
        calls = calls if isinstance(calls, list) else None
        calls_json = json.dumps(calls, ensure_ascii=False, sort_keys=True) if calls else None
        canonical = json.dumps(message, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        # Identity covers EVERY field of the parsed message object (including
        # reasoning_content), but it is a canonical-JSON digest of the parsed
        # object, not the byte layout of the original transport body.
        identity = canonical
        row = {
            "index": index,
            "role": message.get("role"),
            "category": category,
            "category_evidence": evidence,
            "content_chars": len(text),
            "content_utf8_bytes": len(text.encode("utf-8")),
            "reasoning_chars": len(reasoning) if reasoning is not None else None,
            "reasoning_utf8_bytes": (len(reasoning.encode("utf-8"))
                                     if reasoning is not None else None),
            "tool_calls": len(calls) if calls is not None else None,
            "tool_calls_json_chars": len(calls_json) if calls_json else None,
            "message_json_utf8_bytes": len(canonical.encode("utf-8")),
            "content_sha256": sha256_bytes(text.encode("utf-8")),
            "message_identity_sha256": sha256_bytes(identity.encode("utf-8")),
            "message_identity_basis": ("canonical JSON of all parsed message "
                                       "fields, not raw transport bytes"),
            "prompt_tokens": None,
            "token_source": None,
        }
        if message.get("role") == "system":
            row["system_segments"] = system_segments(text)
        rows.append(row)
    return rows


def tools_summary(body: dict | None) -> dict:
    if not body or not isinstance(body.get("tools"), list):
        return {"count": 0, "json_chars": 0, "utf8_bytes": 0, "names": []}
    tools = body["tools"]
    blob = json.dumps(tools, ensure_ascii=False)
    names = []
    for tool in tools:
        if isinstance(tool, dict):
            function = tool.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.append(function["name"])
    return {"count": len(tools), "json_chars": len(blob),
            "utf8_bytes": len(blob.encode("utf-8")), "names": names}


def requested_output_limit(body: dict | None):
    if not body:
        return None
    for key in ("max_completion_tokens", "max_tokens"):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def infer_role(entry: dict, body: dict | None) -> tuple[str, str]:
    recorded = (entry["client_request"] or {}).get("role") or "unknown"
    if recorded in AUXILIARY_ROLES:
        return recorded, "X-AE-Role header"
    path = (entry["client_request"] or {}).get("path", "")
    if path == "/v1/models":
        return "provider_models_probe", "request path"
    if body:
        messages = body.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            head = message_content_text(messages[0])
            if messages[0].get("role") == "system":
                if MODELS_MARKER in head or COMPACTION_MARKER in head:
                    return "letta_context_compaction", "system prompt marker"
                return "agent_inference", "system + messages present"
    return recorded, "X-AE-Role header"


# --------------------------------------------------------------------------
# Arm / stage attribution from events.jsonl
# --------------------------------------------------------------------------

def build_attribution(events: list[dict]) -> dict:
    stage_starts: list[dict] = []
    open_by_arm: dict[str, dict] = {}
    phase_marks: list[dict] = []
    for record in events:
        kind = record.get("kind")
        stamp = parse_ts(record.get("timestamp"))
        if stamp is None:
            continue
        if kind == "stage_start":
            segment = {"arm": record.get("arm"), "task": record.get("task"),
                       "start": stamp, "end": None}
            stage_starts.append(segment)
            open_by_arm[segment["arm"]] = segment
        elif kind == "stage_complete":
            stage = record.get("stage") or {}
            arm = record.get("arm")
            segment = open_by_arm.pop(arm, None)
            if segment is not None:
                segment["end"] = stamp
                if isinstance(stage, dict) and stage.get("subtask_id"):
                    segment["task"] = stage["subtask_id"]
        elif kind == "bridge_event":
            event = record.get("event")
            if not isinstance(event, dict) or event.get("kind") != "request":
                continue
            if event.get("method") != "POST":
                continue
            if not str(event.get("path", "")).endswith("/messages"):
                continue
            body = event.get("body")
            source = None
            if isinstance(body, dict) and isinstance(body.get("messages"), list):
                for message in body["messages"]:
                    if isinstance(message, dict) and message.get("role") == "user":
                        try:
                            payload = json.loads(message_content_text(message))
                        except ValueError:
                            continue
                        if isinstance(payload, dict):
                            source = payload.get("source")
                        break
            if source == "dataset_history/material":
                phase = "history"
            elif source in {"current_task", "runtime_user"}:
                phase = "task"
            elif source is None:
                continue  # tool-return follow-up: inherit the current phase
            else:
                phase = "unknown_source"
            phase_marks.append({"timestamp": stamp, "arm": record.get("arm"),
                                "task": record.get("task"), "phase": phase})
    return {"stages": stage_starts, "phases": phase_marks,
            "stage_start_count": len(stage_starts)}


def turn_label(task_id) -> str | None:
    if not isinstance(task_id, str):
        return None
    tail = task_id.rsplit("_", 1)
    if len(tail) == 2 and tail[1].isdigit():
        return "t" + tail[1]
    return None


def attribute(stamp: datetime | None, attribution: dict) -> dict:
    result = {"arm": None, "stage": None, "turn": None, "phase": None,
              "arm_stage_source": "unknown", "phase_source": "unknown",
              "note": None}
    if stamp is None:
        result["note"] = "no parseable client_request timestamp"
        return result
    segment = None
    for candidate in attribution["stages"]:
        start, end = candidate["start"], candidate["end"]
        if start <= stamp and (end is None or stamp <= end):
            segment = candidate
            break
    if segment is None:
        result["note"] = "timestamp outside every declared stage window"
        return result
    result["arm"] = segment["arm"]
    result["stage"] = segment["task"]
    result["turn"] = turn_label(segment["task"])
    result["arm_stage_source"] = "events.jsonl stage_start/stage_complete window"
    phase = None
    for mark in attribution["phases"]:
        if mark["arm"] != segment["arm"]:
            continue
        if mark["timestamp"] <= stamp and mark["timestamp"] >= segment["start"]:
            if phase is None or mark["timestamp"] >= phase["timestamp"]:
                phase = mark
    if phase is not None:
        result["phase"] = phase["phase"]
        result["phase_source"] = "events.jsonl bridge_event POST /messages boundary"
    else:
        result["note"] = "stage window found but no phase boundary precedes the call"
    return result


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------

def build_call(entry: dict, attribution: dict) -> dict:
    client = entry["client_request"] or {}
    body = request_body(entry)
    stamp = parse_ts(client.get("timestamp"))
    placement = attribute(stamp, attribution)
    gate = entry["token_gate"]
    summary = entry["model_summary"]
    blocked = entry["blocked"]
    rejected = entry["client_rejected"]
    upstream = entry["upstream"]

    tokenize_req = (upstream.get("preflight_tokenize") or {}).get("request")
    tokenize_resp = (upstream.get("preflight_tokenize") or {}).get("response")
    infer_req = (upstream.get("model_inference") or {}).get("request")
    infer_resp = (upstream.get("model_inference") or {}).get("response")
    models_resp = (upstream.get("models") or {}).get("response")

    role_inference, role_evidence = infer_role(entry, body)
    classification = classify_call(entry)
    if role_inference in AUXILIARY_ROLES or role_inference == "provider_models_probe":
        # The history/task phase machine tracks the Letta agent exchange; these
        # auxiliary roles are not part of it, so no phase is claimed.
        placement["phase"] = None
        placement["phase_source"] = "not_applicable (role is outside the agent phase machine)"

    recorded_tokens = None
    token_source = None
    if gate and isinstance(gate.get("prompt_tokens"), int) \
            and not isinstance(gate.get("prompt_tokens"), bool):
        recorded_tokens = gate["prompt_tokens"]
        token_source = ("token_gate.prompt_tokens from upstream vLLM /tokenize "
                        "on the proxy projection")
    elif summary and isinstance(summary.get("usage"), dict) \
            and isinstance(summary["usage"].get("prompt_tokens"), int):
        recorded_tokens = summary["usage"]["prompt_tokens"]
        token_source = "model_summary.usage.prompt_tokens"

    completed = classification == "model_inference_completed"
    call = {
        "seq": entry.get("sequence"),
        "client_request_seq": entry.get("client_request_seq"),
        "request_id": entry["request_id"],
        "method": client.get("method"),
        "path": client.get("path"),
        "timestamp_utc": client.get("timestamp"),
        "recorded_role_header": client.get("role"),
        "inferred_role": role_inference,
        "inferred_role_evidence": role_evidence,
        "arm": placement["arm"],
        "stage": placement["stage"],
        "turn": placement["turn"],
        "phase": placement["phase"],
        "arm_stage_source": placement["arm_stage_source"],
        "phase_source": placement["phase_source"],
        "attribution_note": placement["note"],
        "classification": classification,
        "completed_inference": completed,
        "real_model_call": infer_req is not None,
        "preflight_tokenize_called": tokenize_req is not None,
        "post_rejection_attempt": classification == "rejected_after_proxy_stop",
        "recorded_prompt_tokens": recorded_tokens,
        "recorded_prompt_tokens_source": token_source,
        "requested_output_limit": requested_output_limit(body),
        "proxy_output_reserve": (gate or {}).get("output_reserve"),
        "max_prompt_tokens": (gate or {}).get("max_prompt_tokens"),
        "context_window": (gate or {}).get("context_window"),
        "token_gate_passed": (gate or {}).get("passed"),
        "model_summary": ({"http_status": summary.get("http_status"),
                           "usage": summary.get("usage"),
                           "finish_reasons": summary.get("finish_reasons"),
                           "truncation_finish": summary.get("truncation_finish"),
                           "tokenize_prompt_usage_agreement":
                               summary.get("tokenize_prompt_usage_agreement")}
                          if summary else None),
        "rejection": ({"code": blocked.get("code"),
                       "http_status": (rejected or {}).get("http_status"),
                       "stage": "token_gate" if classification == "rejected_at_token_gate"
                                else "proxy_after_failure"}
                      if blocked or rejected else None),
        "source_capture_sha256": None,  # filled by caller (file level)
        "request_body_sha256": client.get("body_sha256"),
        "request_body_bytes": client.get("body_bytes"),
        "upstream": {
            "preflight_tokenize": {
                "request_sha256": (tokenize_req or {}).get("body_sha256"),
                "request_bytes": (tokenize_req or {}).get("body_bytes"),
                "response_sha256": (tokenize_resp or {}).get("body_sha256"),
                "response_bytes": (tokenize_resp or {}).get("body_bytes"),
                "http_status": (tokenize_resp or {}).get("http_status"),
            } if tokenize_req is not None else None,
            "model_inference": {
                "request_sha256": (infer_req or {}).get("body_sha256"),
                "request_bytes": (infer_req or {}).get("body_bytes"),
                "response_sha256": (infer_resp or {}).get("body_sha256"),
                "response_bytes": (infer_resp or {}).get("body_bytes"),
                "http_status": (infer_resp or {}).get("http_status"),
            } if infer_req is not None else None,
            "models": {
                "response_sha256": (models_resp or {}).get("body_sha256"),
                "http_status": (models_resp or {}).get("http_status"),
            } if models_resp is not None else None,
        },
    }
    return call


def select_previous_rewrite(calls: list[dict], rejected_index: int) -> dict | None:
    """Last completed agent inference in the same arm before the rejection.

    Returns the call dict, or None when the evidence does not identify one.
    """
    rejected = calls[rejected_index]
    arm = rejected["arm"]
    if arm is None:
        return None
    for call in reversed(calls[:rejected_index]):
        if call["arm"] != arm:
            continue
        if call["inferred_role"] != "agent_inference":
            continue
        if call["completed_inference"]:
            return call
    return None


def detail(call: dict, entry_by_id: dict) -> dict:
    entry = entry_by_id[call["request_id"]]
    body = request_body(entry)
    messages = body.get("messages") if body else None
    messages = messages if isinstance(messages, list) else []
    rows = message_breakdown(messages)
    category_totals: dict[str, dict] = {}
    for row in rows:
        bucket = category_totals.setdefault(
            row["category"], {"messages": 0, "content_chars": 0,
                              "content_utf8_bytes": 0, "reasoning_chars": 0})
        bucket["messages"] += 1
        bucket["content_chars"] += row["content_chars"]
        bucket["content_utf8_bytes"] += row["content_utf8_bytes"]
        bucket["reasoning_chars"] += row["reasoning_chars"] or 0
    system_row = next((row for row in rows if row["role"] == "system"), None)
    tokens = call["recorded_prompt_tokens"]
    max_prompt = call["max_prompt_tokens"]
    window = call["context_window"]
    reserve = call["proxy_output_reserve"]
    headroom = None
    if tokens is not None and max_prompt is not None:
        headroom = {
            "over_max_prompt_tokens_by": tokens - max_prompt,
            "projected_total_with_reserve": (tokens + reserve
                                             if reserve is not None else None),
            "over_context_window_by": (tokens + reserve - window
                                       if reserve is not None and window is not None
                                       else None),
        }
    return {
        "seq": call["client_request_seq"],
        "request_id": call["request_id"],
        "arm": call["arm"],
        "stage": call["stage"],
        "turn": call["turn"],
        "phase": call["phase"],
        "arm_stage_source": call["arm_stage_source"],
        "classification": call["classification"],
        "rejection": call["rejection"],
        "recorded_prompt_tokens": call["recorded_prompt_tokens"],
        "requested_output_limit": call["requested_output_limit"],
        "proxy_output_reserve": call["proxy_output_reserve"],
        "max_prompt_tokens": call["max_prompt_tokens"],
        "context_window": call["context_window"],
        "headroom": headroom,
        "request_body_bytes": call["request_body_bytes"],
        "request_body_sha256": call["request_body_sha256"],
        "messages": rows,
        "message_count": len(rows),
        "category_totals": category_totals,
        "system_segments": system_row.get("system_segments") if system_row else None,
        "tools": tools_summary(body),
        "content_chars_total": sum(row["content_chars"] for row in rows),
        "content_utf8_bytes_total": sum(row["content_utf8_bytes"] for row in rows),
        "reasoning_chars_total": sum(row["reasoning_chars"] or 0 for row in rows),
    }


def compare(previous: dict, rejected: dict) -> dict:
    """Multiset comparison of complete message objects.

    Identity is the canonical-JSON digest of ALL parsed message fields. Matching
    is one-to-one by occurrence count, so repeated identical messages in the
    previous request are consumed one at a time and any surplus occurrence is
    reported as added rather than silently matched to the same message twice.
    Identity is a parsed-object digest, not the byte layout of the original
    transport body.
    """
    positions: dict[str, list[int]] = {}
    for row in previous["messages"]:
        positions.setdefault(row["message_identity_sha256"], []).append(row["index"])
    consumed: dict[str, int] = {}
    retained, added = [], []
    for row in rejected["messages"]:
        identity = row["message_identity_sha256"]
        seat = consumed.get(identity, 0)
        available = positions.get(identity, [])
        if seat < len(available):
            consumed[identity] = seat + 1
            retained.append({"index": row["index"], "role": row["role"],
                             "category": row["category"],
                             "content_chars": row["content_chars"],
                             "content_utf8_bytes": row["content_utf8_bytes"],
                             "previously_at_index": available[seat]})
        else:
            added.append({"index": row["index"], "role": row["role"],
                          "category": row["category"],
                          "content_chars": row["content_chars"],
                          "content_utf8_bytes": row["content_utf8_bytes"]})
    return {
        "retained_messages": retained,
        "added_messages": added,
        "retained_count": len(retained),
        "added_count": len(added),
        "identity_basis": ("canonical JSON of all parsed message fields "
                           "(including reasoning_content and tool_calls); "
                           "one-to-one by occurrence count; not raw transport bytes"),
        "added_content_chars": sum(item["content_chars"] for item in added),
        "added_content_utf8_bytes": sum(item["content_utf8_bytes"] for item in added),
        "token_delta": ((rejected["recorded_prompt_tokens"]
                         - previous["recorded_prompt_tokens"])
                        if rejected["recorded_prompt_tokens"] is not None
                        and previous["recorded_prompt_tokens"] is not None else None),
        "char_delta_total": rejected["content_chars_total"] - previous["content_chars_total"],
        "byte_delta_total": rejected["content_utf8_bytes_total"]
                            - previous["content_utf8_bytes_total"],
        "tools_previous": previous["tools"],
        "tools_rejected": rejected["tools"],
        "tools_byte_delta": rejected["tools"]["utf8_bytes"] - previous["tools"]["utf8_bytes"],
    }


def arm_growth(calls: list[dict], entry_by_id: dict) -> list[dict]:
    rows = []
    for call in calls:
        if call["inferred_role"] != "agent_inference" and call["recorded_role_header"] in AUXILIARY_ROLES:
            pass
        entry = entry_by_id[call["request_id"]]
        body = request_body(entry)
        messages = body.get("messages") if body else None
        messages = messages if isinstance(messages, list) else []
        rows.append({
            "seq": call["client_request_seq"],
            "request_id": call["request_id"],
            "recorded_role_header": call["recorded_role_header"],
            "inferred_role": call["inferred_role"],
            "arm": call["arm"],
            "turn": call["turn"],
            "phase": call["phase"],
            "classification": call["classification"],
            "recorded_prompt_tokens": call["recorded_prompt_tokens"],
            "requested_output_limit": call["requested_output_limit"],
            "message_count": len(messages),
            "tool_schema_count": tools_summary(body)["count"],
            "tool_schema_utf8_bytes": tools_summary(body)["utf8_bytes"],
            "request_body_bytes": call["request_body_bytes"],
        })
    return rows


def source_sha_check(audit: dict | None, paths: dict) -> dict:
    if not audit or not isinstance(audit.get("sources"), dict):
        return {"checked": False, "note": "no audit file supplied"}
    sources = audit["sources"]
    result = {"checked": True, "entries": {}}
    for path in paths.values():
        if path is None:
            continue
        digest = sha256_file(path)
        size = path.stat().st_size
        candidates = [key for key in sources if key.rsplit("/", 1)[-1] == path.name]
        audit_key = candidates[0] if len(candidates) == 1 else None
        recorded = sources.get(audit_key) if audit_key else None
        result["entries"][str(path)] = {
            "sha256": digest,
            "bytes": size,
            "audit_key": audit_key,
            "audit_sha256": (recorded or {}).get("sha256"),
            "matches_audit": bool(recorded and recorded.get("sha256") == digest),
            "note": None if audit_key else "file is not listed in the audit sources",
        }
    return result


def build_report(capture_path: Path, events_path: Path | None = None,
                 audit_path: Path | None = None,
                 previous_request_id: str | None = None) -> dict:
    records = read_jsonl(capture_path)
    config, groups, ordered = group_capture(records)
    events = read_jsonl(events_path) if events_path else []
    attribution = build_attribution(events)
    calls = [build_call(entry, attribution) for entry in ordered]
    capture_digest = sha256_file(capture_path)
    for call in calls:
        call["source_capture_sha256"] = capture_digest

    entry_by_id = {entry["request_id"]: entry for entry in ordered}
    rejected_index = next((index for index, call in enumerate(calls)
                           if call["classification"] == "rejected_at_token_gate"), None)
    previous = None
    if previous_request_id:
        previous = next((call for call in calls
                         if call["request_id"] == previous_request_id), None)
        if previous is None:
            raise CliError("--previous-request-id not present in the capture")
    elif rejected_index is not None:
        previous = select_previous_rewrite(calls, rejected_index)

    rejected_call = calls[rejected_index] if rejected_index is not None else None
    rejected_detail = detail(rejected_call, entry_by_id) if rejected_call else None
    previous_detail = detail(previous, entry_by_id) if previous else None
    comparison = (compare(previous_detail, rejected_detail)
                  if previous_detail and rejected_detail else None)

    counts: dict[str, int] = {}
    for call in calls:
        counts[call["classification"]] = counts.get(call["classification"], 0) + 1

    inputs = {"capture": {"path": str(capture_path), "sha256": capture_digest,
                          "bytes": capture_path.stat().st_size,
                          "records": len(records)},
              "events": None, "audit": None}
    if events_path:
        inputs["events"] = {"path": str(events_path),
                            "sha256": sha256_file(events_path),
                            "bytes": events_path.stat().st_size,
                            "records": len(events)}
    if audit_path:
        inputs["audit"] = {"path": str(audit_path),
                           "sha256": sha256_file(audit_path),
                           "bytes": audit_path.stat().st_size}
    audit = read_json(audit_path) if audit_path else None

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": ("message/history/tool composition and growth of the captured "
                  "proxy requests; no score, no KV claim, no validity change"),
        "offline": True,
        "network_called": False,
        "model_called": False,
        "inputs": inputs,
        "source_sha_check": source_sha_check(
            audit, {"capture": capture_path, "events": events_path,
                    "audit": audit_path}),
        "proxy_config": config.get("proxy_config"),
        "source_wheel_sha256": config.get("source_wheel_sha256"),
        "automatic_retry": config.get("automatic_retry"),
        "proxy_close": config.get("proxy_close"),
        "attribution_windows": {
            "stages": [{"arm": segment["arm"], "task": segment["task"],
                        "start": segment["start"].isoformat(),
                        "end": segment["end"].isoformat() if segment["end"] else None}
                       for segment in attribution["stages"]],
            "phase_boundaries": [
                {"timestamp": mark["timestamp"].isoformat(), "arm": mark["arm"],
                 "task": mark["task"], "phase": mark["phase"]}
                for mark in attribution["phases"]],
        },
        "counts": counts,
        "completed_inference_calls": counts.get("model_inference_completed", 0),
        "real_model_upstream_calls": sum(1 for call in calls if call["real_model_call"]),
        "preflight_tokenize_calls": sum(1 for call in calls
                                        if call["preflight_tokenize_called"]),
        "calls": calls,
        "growth_timeline": arm_growth(calls, entry_by_id),
        "rejected_request": rejected_detail,
        "previous_rewrite_request": previous_detail,
        "previous_rewrite_selection": (
            "--previous-request-id override"
            if previous_request_id else
            "last completed agent-role model inference in the same arm before "
            "the first token_gate rejection"),
        "comparison": comparison,
        "token_accounting": {
            "measured_whole_request_tokens": (
                rejected_detail["recorded_prompt_tokens"] if rejected_detail else None),
            "per_message_tokens": None,
            "per_message_tokens_reason": ("no local tokenizer for profile "
                                          "'vllm-0.10.0-qwen3-text-hermes' is "
                                          "available offline; per-message token "
                                          "counts are not fabricated"),
            "character_byte_ratio_used_for_tokens": False,
        },
        "limitations": LIMITATIONS,
        "reproduce": None,
    }
    return report


# --------------------------------------------------------------------------
# Summary text (Chinese, bounded length)
# --------------------------------------------------------------------------

def schema_delta_phrase(delta) -> str:
    """Signed tool-schema size change, without a forced direction."""
    if delta is None:
        return "schema 体积未知"
    if delta > 0:
        return f"schema 增加 {delta} 字节"
    if delta < 0:
        return f"schema 减少 {abs(delta)} 字节"
    return "schema 不变"


def build_summary(report: dict, limit: int = 800) -> str:
    rejected = report.get("rejected_request") or {}
    previous = report.get("previous_rewrite_request") or {}
    comparison = report.get("comparison") or {}
    counts = report.get("counts") or {}
    categories = rejected.get("category_totals") or {}
    tools = rejected.get("tools") or {}

    def cat(name: str) -> str:
        item = categories.get(name) or {}
        return f"{item.get('content_chars', 0)}/{item.get('content_utf8_bytes', 0)}"

    def ordered_counts() -> str:
        labels = [("model_inference_completed", "完成"),
                  ("rejected_at_token_gate", "门控拒绝"),
                  ("rejected_after_proxy_stop", "停止后503"),
                  ("provider_models_probe", "模型探测")]
        parts = [f"{label} {counts[key]}" for key, label in labels if counts.get(key)]
        for key in sorted(set(counts) - {key for key, _ in labels}):
            parts.append(f"{key} {counts[key]}")
        return "＋".join(parts)

    text = (
        "# AE-01 容量诊断（t4–t5）\n"
        "\n"
        f"观察：{sum(counts.values())} 次请求＝{ordered_counts()}。"
        f"首拒 {str(rejected.get('request_id'))[:8]}（{rejected.get('arm')} 臂 "
        f"{rejected.get('turn')}/{rejected.get('phase')}）："
        f"{rejected.get('recorded_prompt_tokens')} tokens＞上限 "
        f"{rejected.get('max_prompt_tokens')}（窗口 {rejected.get('context_window')}、"
        f"预留 {rejected.get('proxy_output_reserve')}），"
        f"{(rejected.get('rejection') or {}).get('http_status')}，未调模型。\n"
        "\n"
        f"构成（字符/字节，非 token）：被拒请求 {rejected.get('message_count')} 条消息，"
        f"system {cat('system_message')}（含可变 memory_blocks 子段）、"
        f"历史 {cat('history_input')}、"
        f"任务指令 {cat('task_instruction')}、assistant {cat('assistant_message')}、"
        f"tool {cat('tool_result')}；schema {tools.get('count')} 个/"
        f"{tools.get('utf8_bytes')} 字节。\n"
        "\n"
        f"增长：t4→t5 新增 {comparison.get('added_count')} 条"
        f"（{comparison.get('added_content_chars')}/"
        f"{comparison.get('added_content_utf8_bytes')}）；其余 "
        f"{comparison.get('retained_count')} 条完整消息对象相同"
        f"（全字段规范 JSON 一致，非原始传输 JSON 字节一致）；"
        f"{schema_delta_phrase(comparison.get('tools_byte_delta'))}；token "
        f"{previous.get('recorded_prompt_tokens')}→"
        f"{rejected.get('recorded_prompt_tokens')}"
        f"（+{comparison.get('token_delta')}）。\n"
        "\n"
        "缺口：无本地 tokenizer，逐消息 token=null，仅整请求实测，字符不可分摊；"
        "arm/stage 靠 UTC 时间窗对齐 events.jsonl。r2 仍 INVALID，不评分、"
        "不改有效性、不推荐窗口。\n"
    )
    if len(text) > limit:
        raise CliError(f"summary exceeds {limit} characters ({len(text)})")
    return text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def ensure_output_dir(out_dir: Path) -> None:
    if out_dir.exists():
        if not out_dir.is_dir():
            raise CliError(f"output path exists and is not a directory: {out_dir}")
        if any(out_dir.iterdir()):
            existing = ", ".join(sorted(item.name for item in out_dir.iterdir()))
            raise CliError(f"output directory is not empty; refusing to overwrite: "
                           f"{out_dir} ({existing})")
    else:
        out_dir.mkdir(parents=True)


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="AE-01 offline context-capacity diagnostic over the private "
                    "model-audit capture.")
    parser.add_argument("--capture", required=True,
                        help="private model-capture JSONL (proxy journal)")
    parser.add_argument("--events", default=None,
                        help="task-wiring events.jsonl for arm/stage attribution")
    parser.add_argument("--audit", default=None,
                        help="ae-01 input-audit JSON for source-SHA cross-check")
    parser.add_argument("--out-dir", required=True,
                        help="exclusive output directory (must not hold report.json/"
                             "summary.md/tests.log)")
    parser.add_argument("--previous-request-id", default=None,
                        help="override the previous-rewrite request selection")
    parser.add_argument("--summary-limit", type=int, default=800,
                        help="sanity cap on summary.md characters (default 800; "
                             "not an experimental threshold)")
    args = parser.parse_args(argv)

    capture = Path(args.capture).expanduser().resolve()
    events = Path(args.events).expanduser().resolve() if args.events else None
    audit = Path(args.audit).expanduser().resolve() if args.audit else None
    out_dir = Path(args.out_dir).expanduser().resolve()
    for label, path in (("capture", capture), ("events", events), ("audit", audit)):
        if path is not None and not path.is_file():
            raise CliError(f"{label} file not found: {path}")
    ensure_output_dir(out_dir)
    for name in OUTPUT_FILES:
        if (out_dir / name).exists():
            raise CliError(f"refusing to overwrite existing {out_dir / name}")

    report = build_report(capture, events, audit, args.previous_request_id)
    report["reproduce"] = " ".join([
        sys.executable, "scripts/ae_01_capacity_report.py",
        "--capture", str(capture),
        *(("--events", str(events)) if events else ()),
        *(("--audit", str(audit)) if audit else ()),
        "--out-dir", str(out_dir),
    ])
    summary = build_summary(report, args.summary_limit)

    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8")
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")

    print(f"wrote {out_dir / 'report.json'}")
    print(f"wrote {out_dir / 'summary.md'} ({len(summary)} chars)")
    print(json.dumps({"counts": report["counts"],
                      "rejected_request": (report["rejected_request"] or {}).get("request_id"),
                      "previous_rewrite_request":
                          (report["previous_rewrite_request"] or {}).get("request_id")},
                     ensure_ascii=False))
    return 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
