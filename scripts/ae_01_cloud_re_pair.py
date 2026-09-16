#!/usr/bin/env python3
"""AE-01 single-t4 cloud R/E pair CLI (plan/preflight offline, run explicit).

plan (default) and preflight never contact a service; only an explicit `run`
connects to the loopback model and Letta services. Output directories are
exclusive and never resumed or overwritten. The pair executes only U000828 t4,
once per arm, in the fixed R-then-E order recorded in the plan.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ae_cloud_re_pair import (  # noqa: E402
    ARMS, SCHEMA_VERSION, build_plan, execute_re_pair, preflight, re_code_files,
    validate_config,
)
from ae_capability import TASK_ID  # noqa: E402
from ae_inputs import canonical_sha256, prepare_sample  # noqa: E402
import ae_multicall as ai  # noqa: E402
from ae_multicall import LAUNCH_RECEIPT_ENV_VAR, MANIFEST_ENV_VAR  # noqa: E402


def ensure_output_dir(path):
    """Create a fresh run directory; never resume or overwrite existing content."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"output already exists; no resume or overwrite: {path}")
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def provenance(config, config_path, multicall_manifest_path=None,
               multicall_load_receipt=None, *, require_receipt=False):
    """Production provenance, including the manifest and the service load receipt.

    The 0.2 protocol is not only a config flag. The run record names the reviewed
    manifest the runtime gate and the audit both read, and it references the load
    receipt the SERVICE process wrote. This function never synthesizes receipt
    contents: it records `{path, sha256}` of a receipt that already exists, or it
    records nothing and marks the loading state unverified.

    `require_receipt` is set for a real RUN: without a service receipt, a run that
    claims the compatibility protocol cannot be started, so the caller refuses
    before any model request. PLAN and PREFLIGHT stay runnable offline and record
    `verified: false`, which the audit reads as "loading not checked" rather than
    as a passed deployment.
    """
    record = {
        "config_file_sha256": hashlib.sha256(Path(config_path).read_bytes()).hexdigest(),
        "config_canonical_sha256": canonical_sha256(config),
        "code_sha256": re_code_files(ROOT),
        "python": sys.version, "python_executable": sys.executable,
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "project_git_commit": None, "live_letta_commit_verified": False,
    }
    if config.get("multicall_profile") is None:
        return record
    declared = multicall_manifest_path or os.environ.get(MANIFEST_ENV_VAR)
    if declared is None:
        raise RuntimeError(
            "a 0.2 run must record the reviewed compatibility manifest; pass "
            "--multicall-manifest or set " + MANIFEST_ENV_VAR)
    path = Path(declared)
    if not path.is_file():
        raise RuntimeError(f"the reviewed compatibility manifest is absent: {path}")
    manifest_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    record["multicall_manifest"] = {"path": str(path), "sha256": manifest_sha}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    claims_live = (manifest.get("verification") or {}).get("applied_to_live_server") is True
    receipt_path = multicall_load_receipt or os.environ.get(LAUNCH_RECEIPT_ENV_VAR)
    if receipt_path:
        receipt_file = Path(receipt_path)
        if not receipt_file.is_file():
            raise RuntimeError(
                f"the declared service load receipt is absent: {receipt_file}")
        # Naming a file is not enough: its CONTENTS are validated here, before the
        # caller can create or enter execution, using the same shared check the
        # audit uses. A garbage receipt cannot reach a model request.
        try:
            receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RuntimeError(f"the service load receipt is not valid JSON: {exc}") from None
        ai.validate_launch_receipt(receipt, manifest, manifest_sha)
        record["multicall_live_loading"] = {
            "path": str(receipt_file),
            "sha256": hashlib.sha256(receipt_file.read_bytes()).hexdigest()}
        record["multicall_loading_verified"] = True
        return record
    if require_receipt or claims_live:
        raise RuntimeError(
            "this run declares the AE multicall protocol but no service load receipt "
            "was supplied; refusing to start before any model request")
    record["multicall_loading_verified"] = False
    record["multicall_loading_note"] = (
        "offline PLAN/PREFLIGHT: no service receipt is recorded, so the service's "
        "loaded Letta sources are NOT checked")
    return record


def local_environment():
    """No inherited proxy may reroute loopback; only a run-local EMPTY key is used."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("plan", "preflight", "run"), default="plan")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--vita-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--multicall-manifest", type=Path, default=None,
                        help="the reviewed compatibility manifest to record for a 0.2 config")
    parser.add_argument("--multicall-load-receipt", type=Path, default=None,
                        help="the service process's load receipt (required for a 0.2 run)")
    args = parser.parse_args(argv)
    result, prov, owned = None, None, False
    try:
        raw_config = json.loads(args.config.read_text(encoding="utf-8"))
        config = validate_config(raw_config)
        sample = prepare_sample(args.dataset, end_turn=5)
        plan = build_plan(config, sample, code_files=re_code_files(ROOT))
        prov = provenance(config, args.config, args.multicall_manifest,
                          args.multicall_load_receipt,
                          require_receipt=args.stage == "run")
        plan["provenance"] = prov
        out = ensure_output_dir(args.output_dir)
        owned = True
        save(out / "plan.json", plan)
        native_config = out.resolve() / "vita-models.json"
        from ae_vita import native_model_config
        save(native_config, native_model_config(config))
        if args.stage == "plan":
            print(f"AE R/E pair PLAN; no network/model; arms={list(ARMS)}; output={out}")
            return 0

        os.environ["VITA_MODEL_CONFIG_PATH"] = str(native_config)
        local_environment()
        from ae_vita import NativeVita

        def runtime_factory(arm):
            return NativeVita(args.vita_source, args.dataset, config["model_origin"],
                              config["expected_model"], config["temperature"],
                              config["auxiliary_output_tokens"], seed=config["seed"],
                              transport_profile=config["transport_profile"])

        if args.stage == "preflight":
            original_connect = socket.socket.connect

            def no_network(*_a, **_k):
                raise RuntimeError("preflight forbids socket connections")

            socket.socket.connect = no_network
            try:
                checks = preflight(sample, config)
                previews = {}
                for arm in ARMS:
                    native = runtime_factory(arm)
                    try:
                        preview = native.preview_tasks()
                        assert [p["task_id"] for p in preview] == [TASK_ID, "sub_U000828_5"]
                        assert preview[0]["tool_schemas"], "t4 native tools missing"
                        names = [t["name"] for t in preview[0]["tool_schemas"]]
                        assert not any("memory_update" in n or "preference_memory" in n
                                       for n in names), "second memory backend in native tools"
                        previews[arm] = {k: preview[0][k] for k in (
                            "task_id", "domain", "instruction", "tool_schemas",
                            "env_initial_hash", "lengths_chars", "model_called",
                            "tools_executed")}
                    finally:
                        native.abort()
                result = {
                    "schema_version": SCHEMA_VERSION, "purpose": plan["purpose"],
                    "status": "PREFLIGHT_PASS", "network_called": False, "model_called": False,
                    "task_success": None, "scientific_result": None, "scope": plan["scope"],
                    "pair_preflight": checks["checks"], "private_boundary": checks["private_boundary"],
                    "arm_previews": previews, "t5_executed": False,
                    "history_records": plan["task_preview"]["history_records"],
                    "replacement_ref": plan["inputs"]["replacement"]["ref"],
                    "provenance": prov,
                }
            finally:
                socket.socket.connect = original_connect
        else:
            from ae_http import JSONHTTPTransport, JSONLJournal
            common = {key: config[key] for key in
                      ("timeout_seconds", "max_request_bytes", "max_response_bytes")}
            with ExitStack() as stack:
                journal = JSONLJournal(out / "events.jsonl")
                stack.callback(journal.close)
                model = stack.enter_context(JSONHTTPTransport(
                    config["model_origin"], out / "model-http.jsonl", max_requests=1, **common))
                letta = stack.enter_context(JSONHTTPTransport(
                    config["letta_origin"], out / "letta-http.jsonl", **common))
                result = execute_re_pair(config, sample, model_transport=model,
                                         letta_transport=letta, runtime_factory=runtime_factory,
                                         emit=journal.append)
        result["provenance"] = prov
        save(out / "result.json", result)
        print(f"AE R/E pair {result['status']}; scientific_result=null; output={out}")
        return 0 if result["status"] in ("PREFLIGHT_PASS", "RE_PAIR_COMPLETED_AUDIT_PENDING") else 2
    except (Exception, KeyboardInterrupt) as exc:
        failure = {"schema_version": SCHEMA_VERSION, "status": "INVALID", "stage": args.stage,
                   "validity_passed": False, "task_success": None, "scientific_result": None,
                   "provenance": prov,
                   "invalid_reasons": [{"type": type(exc).__name__, "message": str(exc)}]}
        try:
            if owned and args.output_dir.is_dir() and not (args.output_dir / "result.json").exists():
                save(args.output_dir / "result.json", failure)
        except Exception:
            pass
        print(f"AE R/E pair INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
