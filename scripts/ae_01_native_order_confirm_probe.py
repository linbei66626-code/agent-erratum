#!/usr/bin/env python3
"""Continuation probe: pay the sealed order after the user's confirmation (max 1 request).

    /root/agent-erratum/.venv-vita/bin/python -B scripts/ae_01_native_order_confirm_probe.py \
        --stage plan --source-dir <sealed run dir> \
        --transport-config configs/ae-01__cloud-transport__siliconflow.capability-pacing-candidate.json \
        --output-dir deployment/runs/ae-native-order-confirm-<UTC>-plan

The sealed live run got as far as a real unpaid order (`OTb95a8ee52a`) and the model
asking whether to confirm payment. This probe does NOT rerun the original small case:
it restores the sealed Vita state from `result.json`'s `final_native_snapshot`, keeps
the real history and both real provider replies verbatim from `chat-wire.private.jsonl`,
appends ONE new user message (``确认支付，请现在完成支付。``) and sends ONE real chat
request. It answers only: after that confirmation, can the model really pay the
already-existing order id? It is not an R/E result and does not prove stable ability.

`plan` (default) never opens a socket; only an explicit `run` calls the model, once.
The restored environment is an in-memory Vita database rebuilt from the sealed
snapshot (`restored_from_snapshot=true`), not the same live process.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ae_adapter import (RecoverableToolError, VerifiedWritePrecondition,  # noqa: E402
                        dumps, validate_declared_arguments)
from ae_cloud_proxy import (MODEL, CloudAuditProxy, CloudConfig,  # noqa: E402
                            read_private_key)
from ae_task_run import environment_bindings  # noqa: E402


def _load_order_probe():
    """Reuse the reviewed probe's fixed-Vita loading and helpers, unmodified."""
    spec = importlib.util.spec_from_file_location(
        "ae_native_order_probe", ROOT / "scripts/ae_01_native_order_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORDER_PROBE = _load_order_probe()

SCHEMA_VERSION = "ae-native-order-confirm-probe-0.1"
CHAT_PATH = "/v1/chat/completions"
PURPOSE = "native-order-confirm-probe"
MAX_REQUESTS = 1
REQUEST_OUTPUT_TOKENS = 2048
TEMPERATURE = 0.0
TOOLS = ORDER_PROBE.TOOLS
CONFIRM_TEXT = "确认支付，请现在完成支付。"
SOURCE_FILES = ("plan.json", "result.json", "chat-wire.private.jsonl")
CODE_FILES = ("ae_adapter.py", "ae_cloud_proxy.py", "ae_task_run.py", "ae_inputs.py",
              "ae_probe.py", "scripts/ae_01_native_order_probe.py",
              "scripts/ae_01_native_order_confirm_probe.py")
STATUS_PLAN = "PLAN_FROZEN"
STATUS_PASSED = "NATIVE_ORDER_CONFIRM_PASSED"
STATUS_INCOMPLETE = "MODEL_TASK_INCOMPLETE"
STATUS_CLAIMED = "MODEL_CLAIMED_WITHOUT_TOOL_CALL"
STATUS_UNCERTAIN = "UNCERTAIN_TOOL_OUTCOME_INVALID"
STATUS_INVALID = "INVALID"


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


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


def read_manifest(path):
    entries = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        entries[name.strip()] = digest
    return entries


def load_sources(source_dir, sha_manifest=None):
    """Load and SHA-verify the sealed run; any mismatch refuses before a request."""
    source = Path(source_dir)
    if not source.is_dir():
        raise RuntimeError(f"sealed source directory is missing: {source}")
    absent = [name for name in SOURCE_FILES if not (source / name).is_file()]
    if absent:
        raise RuntimeError(f"sealed source is incomplete: {', '.join(absent)}")
    manifest_path = Path(sha_manifest) if sha_manifest else source.parent / "remote-SHA256SUMS"
    if not manifest_path.is_file():
        raise RuntimeError(f"the authoritative SHA manifest is missing: {manifest_path}")
    entries = read_manifest(manifest_path)
    observed = {}
    for name in SOURCE_FILES:
        key = f"{source.name}/{name}"
        if key not in entries:
            raise RuntimeError(f"the SHA manifest does not cover {key}")
        digest = sha256_bytes((source / name).read_bytes())
        if digest != entries[key]:
            raise RuntimeError(f"sealed source SHA mismatch: {key}")
        observed[key] = digest
    return {"dir": source, "manifest": manifest_path, "sha256": observed,
            "result": json.loads((source / "result.json").read_text(encoding="utf-8")),
            "wire": [json.loads(line) for line in
                     (source / "chat-wire.private.jsonl").read_text(encoding="utf-8").splitlines()
                     if line.strip()]}


def _sealed_contract(sources):
    """The sealed run must be the known two-request, one-unpaid-order state."""
    result, wire = sources["result"], sources["wire"]
    if result.get("schema_version") != ORDER_PROBE.SCHEMA_VERSION:
        raise RuntimeError("sealed result has an unexpected schema version")
    if result.get("requests_sent") != 2 or len(result.get("steps") or []) != 2:
        raise RuntimeError("sealed result is not the known two-request run")
    orders = (result.get("final_native_snapshot") or {}).get("orders")
    if not isinstance(orders, dict) or len(orders) != 1:
        raise RuntimeError("sealed result must hold exactly one order")
    order_id = next(iter(orders))
    order = orders[order_id]
    if order.get("status") != "unpaid":
        raise RuntimeError("the sealed order is not unpaid")
    if result.get("created_order_ids") != [order_id]:
        raise RuntimeError("sealed created_order_ids do not match the sealed order")
    if result.get("payment_source_verified") is not None or result.get("paid_order_id") is not None:
        raise RuntimeError("the sealed run must not contain a payment")
    first, second = result["steps"]
    if [record["name"] for record in first["tool_calls"]] != ["create_delivery_order"]:
        raise RuntimeError("the sealed first request is not a single create")
    if second["tool_calls"] or not second.get("assistant_text"):
        raise RuntimeError("the sealed second request must be an assistant confirmation")
    requests = [row for row in wire if row.get("kind") == "normalized_request"]
    responses = [row for row in wire if row.get("kind") == "upstream_response"]
    if len(requests) != 2 or len(responses) != 2:
        raise RuntimeError("the sealed wire is not the known two-request capture")
    if not all(row.get("complete_body") is True for row in responses):
        raise RuntimeError("the sealed wire holds an incomplete response body")
    history = json.loads(requests[1]["body_utf8"])["messages"]
    if [message.get("role") for message in history] != ["system", "user", "assistant", "tool"]:
        raise RuntimeError("the sealed second request has an unexpected message history")
    create_return = first["tool_calls"][0]["tool_return"]
    if history[3].get("content") != create_return or history[3].get("tool_call_id") != (
            first["tool_calls"][0]["tool_call_id"]):
        raise RuntimeError("the sealed tool return does not match the sealed create call")
    if ORDER_PROBE.order_id_from(create_return) != order_id:
        raise RuntimeError("the sealed create return does not carry the sealed order id")
    reply = json.loads(responses[1]["body_utf8"])["choices"][0]["message"]
    if reply.get("tool_calls") or not reply.get("content"):
        raise RuntimeError("the sealed second reply is not the assistant confirmation")
    if reply["content"] != second["assistant_text"]:
        raise RuntimeError("the sealed assistant reply does not match the sealed result")
    return {"order_id": order_id, "order": order, "snapshot": result["final_native_snapshot"],
            "history": history, "assistant_reply": reply, "create_return": create_return,
            "create_call": first["tool_calls"][0]}


def build_case(sources, vita_source):
    """Rebuild the real Vita environment from the sealed snapshot, byte-exactly."""
    sealed = _sealed_contract(sources)
    vita = ORDER_PROBE.load_vita(Path(vita_source))
    environment = vita["get_environment"](db=deepcopy(sealed["snapshot"]))
    restored = ORDER_PROBE.case_snapshot(environment)
    if restored != sealed["snapshot"]:
        raise RuntimeError("the restored Vita snapshot differs from the sealed snapshot")
    bindings = environment_bindings(environment)
    missing = [name for name in TOOLS if name not in bindings]
    if missing:
        raise RuntimeError(f"the fixed environment lacks the required tools: {missing}")
    messages = deepcopy(sealed["history"])
    messages.append({"role": "assistant", "content": sealed["assistant_reply"].get("content")})
    messages.append({"role": "user", "content": CONFIRM_TEXT})
    if sealed["order_id"] in CONFIRM_TEXT:
        raise RuntimeError("the new user message must not carry the order id")
    return {"vita": vita, "environment": environment,
            "bindings": {name: bindings[name] for name in TOOLS},
            "schemas": {name: deepcopy(bindings[name].schema) for name in TOOLS},
            "messages": messages, "sealed": sealed}


def default_vita_source():
    """The project-wide AE_VITA_SOURCE convention (override wins)."""
    override = os.environ.get("AE_VITA_SOURCE")
    return Path(override) if override else Path("/tmp/ae01-vita-8WHDvk/source")


def build_plan(source_dir, sha_manifest, transport_config, vita_source, *,
               request_output_tokens, temperature):
    sources = load_sources(source_dir, sha_manifest)
    case = build_case(sources, vita_source)
    sealed = case["sealed"]
    document = json.loads(Path(transport_config).read_text(encoding="utf-8"))
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "sealed_order_confirmation_continuation_probe_not_the_original_case",
        "status": STATUS_PLAN,
        "network_called": False, "model_called": False,
        "task_success": None, "scientific_result": None,
        "restored_from_snapshot": True, "new_user_confirmation": True,
        "sealed_source": {"dir": str(sources["dir"]), "sha_manifest": str(sources["manifest"]),
                          "sha256": sources["sha256"],
                          "result_status": sources["result"].get("status"),
                          "requests_sent": sources["result"].get("requests_sent")},
        "sealed_state": {"order_id": sealed["order_id"], "order": deepcopy(sealed["order"]),
                         "snapshot": deepcopy(sealed["snapshot"]),
                         "snapshot_sha256": sha256_bytes(dumps(sealed["snapshot"]).encode("utf-8")),
                         "create_return": sealed["create_return"],
                         "assistant_confirmation": sealed["assistant_reply"]["content"]},
        "appended_user_message": CONFIRM_TEXT,
        "request": {"messages": deepcopy(case["messages"]),
                    "tools": {name: case["schemas"][name] for name in TOOLS},
                    "model": MODEL, "tool_choice": "auto", "parallel_tool_calls": False,
                    "temperature": temperature,
                    "max_completion_tokens": request_output_tokens, "seed": None},
        "budget": {"max_requests": MAX_REQUESTS, "no_retry": True,
                   "no_resume_same_directory": True, "pace_seconds": document.get("pace_seconds")},
        "transport_config": {"path": str(transport_config),
                             "sha256": sha256_bytes(Path(transport_config).read_bytes()),
                             "document": document},
        "decision_rules": [
            "the sealed order must end paid, with its other fields unchanged and no new order",
            "the payment id must be the sealed order id the model received in its input history",
            "redundant repeat payments are recorded as redundancy, not as failure",
            "protocol violations (undeclared tool or invalid arguments) are recorded and are never a clean pass",
            "an uncertain tool outcome stops immediately and is never replayed",
            "exactly one real chat request; the model's own output is the only capability evidence",
        ],
        "vita_source": str(Path(vita_source)),
        "scope": ["in-memory Vita database rebuilt from the sealed snapshot; not the same live process",
                  "the original two-request run is NOT claimed to have succeeded",
                  "no R/E conclusion; scientific_result stays null"],
        "case_sha256": sha256_bytes(dumps(sealed["snapshot"]).encode("utf-8")),
        "provenance": {"code_sha256": {name: sha256_bytes((ROOT / name).read_bytes())
                                       for name in CODE_FILES},
                       "python": sys.version, "python_executable": sys.executable},
    }


def _chat_body(case, *, request_output_tokens, temperature):
    return dumps({
        "model": MODEL, "messages": deepcopy(case["messages"]),
        "tools": [{"type": "function", "function": deepcopy(case["schemas"][name])}
                  for name in TOOLS],
        "tool_choice": "auto", "parallel_tool_calls": False, "temperature": temperature,
        "max_completion_tokens": request_output_tokens,
    }).encode("utf-8")


def _order_fields_changed(before, after):
    changed = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed[key] = {"before": before.get(key), "after": after.get(key)}
    return changed


def execute_confirm(case, transport_config, *, request_output_tokens, temperature, api_key,
                    journal_path, opener=None, clock=None, sleep=None):
    """One explicit RUN: exactly one chat request over the strict proxy."""
    document = dict(json.loads(Path(transport_config).read_text(encoding="utf-8")))
    document["max_requests"] = MAX_REQUESTS
    config = CloudConfig(**document)
    proxy = CloudAuditProxy(config, Path(journal_path), api_key=api_key,
                            **_proxy_timing(clock, sleep))
    if opener is not None:
        proxy.opener = opener  # offline fixtures only; RUN keeps the real opener
    vita, environment, bindings = case["vita"], case["environment"], case["bindings"]
    sealed = case["sealed"]
    order_id = sealed["order_id"]
    result = {"schema_version": SCHEMA_VERSION, "status": STATUS_INVALID,
              "validity_passed": False, "model_task_completed": False,
              "task_success": None, "scientific_result": None,
              "restored_from_snapshot": True, "new_user_confirmation": True,
              "sealed_order_id": order_id, "model_called": False, "requests_sent": 0,
              "stop_reason": None, "errors": [], "steps": [],
              "started_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        body = _chat_body(case, request_output_tokens=request_output_tokens,
                          temperature=temperature)
        result["requests_sent"] = 1
        result["model_called"] = True
        status, reply = proxy.dispatch("POST", CHAT_PATH, body, PURPOSE, "agent")
        step = {"index": 1, "http_status": status, "request_sha256": sha256_bytes(body),
                "request_messages": json.loads(body.decode("utf-8"))["messages"],
                "response_sha256": sha256_bytes(reply), "tool_calls": []}
        result["steps"].append(step)
        if status != 200:
            result["stop_reason"] = f"provider_http_{status}"
            result["errors"].append({"type": "provider_http", "message": str(status)})
            return result
        try:
            parsed = json.loads(reply.decode("utf-8"))
            choice = parsed["choices"][0]
            message = choice["message"]
            step["finish_reason"] = choice.get("finish_reason")
            step["usage"] = parsed.get("usage")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            result["stop_reason"] = "unparseable_provider_response"
            result["errors"].append({"type": "unparseable_response", "message": str(exc)})
            return result
        calls = message.get("tool_calls") or []
        if not calls:
            step["assistant_text"] = message.get("content")
            result["status"] = STATUS_CLAIMED
            result["stop_reason"] = "no_tool_call"
            result["validity_passed"] = True
            result["task_success"] = False
            return result
        native_calls = []
        for call in calls:
            name = (call.get("function") or {}).get("name")
            raw = (call.get("function") or {}).get("arguments")
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                arguments = None
            if not isinstance(arguments, dict):
                result["stop_reason"] = "unparseable_model_arguments"
                result["errors"].append({"type": "unparseable_model_arguments",
                                         "message": f"{name}: {raw!r}"})
                return result
            native_calls.append(vita["ToolCall"](id=call.get("id", ""), name=name,
                                                 arguments=arguments))
        for call, native in zip(calls, native_calls):
            name = native.name
            arguments = native.arguments
            if name not in bindings:
                outcome = {"status": "error", "text": f"Error: tool not available: {name}",
                           "kind": "undeclared_tool", "dispatched": False}
            else:
                outcome = _execute(bindings[name], arguments)
            record = {"tool_call_id": native.id, "name": name, "arguments": deepcopy(arguments),
                      "status": outcome["status"], "kind": outcome["kind"],
                      "dispatched": outcome["dispatched"], "tool_return": outcome["text"]}
            if name == "pay_delivery_order":
                record["order_id"] = arguments.get("order_id")
                record["order_id_is_the_sealed_order"] = arguments.get("order_id") == order_id
                record["order_id_in_input_history"] = any(
                    order_id in json.dumps(message) for message in case["messages"])
            step["tool_calls"].append(record)
            step["native_snapshot"] = ORDER_PROBE.case_snapshot(environment)
            if outcome["status"] == "uncertain":
                result["status"] = STATUS_UNCERTAIN
                result["stop_reason"] = "uncertain_tool_outcome_no_replay"
                result["errors"].append({"type": "uncertain_tool_outcome",
                                         "message": outcome["kind"]})
                return result
        # The whole batch was processed; decide only on the final order conditions.
        before_orders = sealed["snapshot"]["orders"]
        after_orders = ORDER_PROBE.case_snapshot(environment)["orders"]
        result["orders_before"] = len(before_orders)
        result["orders_after"] = len(after_orders)
        result["new_order_ids"] = sorted(set(after_orders) - set(before_orders))
        result["final_native_snapshot"] = ORDER_PROBE.case_snapshot(environment)
        payments = [record for record in step["tool_calls"]
                    if record["name"] == "pay_delivery_order" and record["status"] == "success"]
        if not payments:
            result["status"] = STATUS_INCOMPLETE
            result["validity_passed"] = True
            result["task_success"] = False
            result["stop_reason"] = "no_successful_payment_in_the_batch"
            return result
        first = payments[0]
        result["paid_order_id"] = first["order_id"]
        result["payment_source_verified"] = bool(first["order_id_is_the_sealed_order"]
                                                 and first["order_id_in_input_history"])
        result["redundant_actions"] = max(0, len(payments) - 1)
        protocol_violations = [record for record in step["tool_calls"]
                               if record["kind"] in ("schema", "undeclared_tool")]
        after_order = after_orders.get(order_id)
        changed = _order_fields_changed(before_orders.get(order_id, {}), after_order or {})
        result["order_changed_fields"] = sorted(changed)
        paid = bool(after_order) and after_order.get("status") == "paid"
        clean = (result["payment_source_verified"] and paid and not result["new_order_ids"]
                 and set(changed) <= {"status", "update_time"}
                 and not protocol_violations)
        if protocol_violations:
            result["status"] = STATUS_INCOMPLETE
            result["stop_reason"] = "batch_protocol_violation"
        elif clean:
            result["status"] = STATUS_PASSED
            result["stop_reason"] = "sealed_order_paid_after_user_confirmation"
        else:
            result["status"] = STATUS_INCOMPLETE
            result["stop_reason"] = ("new_order_created" if result["new_order_ids"]
                                     else "payment_id_not_the_sealed_order"
                                     if not result["payment_source_verified"]
                                     else "sealed_order_not_paid" if not paid
                                     else "order_fields_changed")
        result["validity_passed"] = True
        result["model_task_completed"] = result["status"] == STATUS_PASSED
        result["task_success"] = result["status"] == STATUS_PASSED
        return result
    finally:
        result.setdefault("final_native_snapshot", ORDER_PROBE.case_snapshot(environment))
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        result["request_count"] = proxy.request_count
        result["proxy_blocked"] = proxy.blocked
        result["wire_journal"] = str(journal_path)
        result["fixture_used"] = opener is not None
        proxy.close()


def _execute(binding, arguments):
    declared_error = validate_declared_arguments(binding.schema, arguments)
    if declared_error is not None:
        return {"status": "error", "text": "Error: " + declared_error,
                "kind": "schema", "dispatched": False}
    try:
        value = binding.call(**arguments)
    except (RecoverableToolError, VerifiedWritePrecondition) as exc:
        return {"status": "error", "text": "Error: " + (str(exc).strip() or type(exc).__name__),
                "kind": type(exc).__name__, "dispatched": True}
    except Exception as exc:  # noqa: BLE001 - an uncertain side effect stops the probe
        return {"status": "uncertain", "text": None,
                "kind": f"{type(exc).__name__}: {exc}", "dispatched": True}
    return {"status": "success", "text": value if isinstance(value, str) else dumps(value),
            "kind": None, "dispatched": True}


def _proxy_timing(clock, sleep):
    if clock is None and sleep is None:
        return {}
    import time as _time
    return {"clock": clock or _time.monotonic, "sleep": sleep or _time.sleep}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=("plan", "run"), default="plan")
    parser.add_argument("--source-dir", required=True, type=Path,
                        help="the sealed run directory (plan.json/result.json/chat-wire.private.jsonl)")
    parser.add_argument("--sha-manifest", type=Path, default=None,
                        help="authoritative SHA manifest (defaults to <source-dir>/../remote-SHA256SUMS)")
    parser.add_argument("--vita-source", type=Path, default=None,
                        help="pinned Vita source (defaults to AE_VITA_SOURCE or the standard path)")
    parser.add_argument("--transport-config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--key-file", type=Path, default=None,
                        help="private API key file for --stage run only")
    parser.add_argument("--request-output-tokens", type=int, default=REQUEST_OUTPUT_TOKENS)
    parser.add_argument("--temperature", type=float, default=TEMPERATURE)
    args = parser.parse_args(argv)
    if args.stage == "run" and args.key_file is None:
        parser.error("--key-file is required for --stage run")
    if args.request_output_tokens <= 0 or args.temperature < 0:
        parser.error("request output tokens must be positive and temperature non-negative")
    plan, owned = None, False
    try:
        vita_source = args.vita_source or default_vita_source()
        plan = build_plan(args.source_dir, args.sha_manifest, args.transport_config,
                          vita_source, request_output_tokens=args.request_output_tokens,
                          temperature=args.temperature)
        out = ensure_output_dir(args.output_dir)
        owned = True
        save(out / "plan.json", plan)
        if args.stage == "plan":
            print(f"AE native order confirm probe PLAN; no network/model; output={out}")
            return 0
        key = read_private_key(args.key_file)
        sources = load_sources(args.source_dir, args.sha_manifest)
        case = build_case(sources, vita_source)
        result = execute_confirm(case, args.transport_config,
                                 request_output_tokens=args.request_output_tokens,
                                 temperature=args.temperature, api_key=key,
                                 journal_path=out / "chat-wire.private.jsonl")
        result["plan_sha256"] = sha256_bytes((out / "plan.json").read_bytes())
        result["sealed_source"] = plan["sealed_source"]
        save(out / "result.json", result)
        print(f"AE native order confirm probe {result['status']}; requests={result['requests_sent']}; "
              f"scientific_result=null; output={out}")
        return 0 if result["status"] == STATUS_PASSED else 2
    except (Exception, KeyboardInterrupt) as exc:
        failure = {"schema_version": SCHEMA_VERSION, "status": STATUS_INVALID,
                   "stage": args.stage, "validity_passed": False, "task_success": None,
                   "scientific_result": None, "restored_from_snapshot": True,
                   "new_user_confirmation": True,
                   "invalid_reasons": [{"type": type(exc).__name__, "message": str(exc)}]}
        try:
            # Zero-write on refusal: never touch a directory this call did not create.
            if owned and args.output_dir.is_dir() and not (args.output_dir / "result.json").exists():
                save(args.output_dir / "result.json", failure)
        except Exception:
            pass
        print(f"AE native order confirm probe INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
