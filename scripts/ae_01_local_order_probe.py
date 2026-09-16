#!/usr/bin/env python3
"""Local Qwen3.5-9B screening on the COMPLETE-INFORMATION order case.

    .venv-vita/bin/python -B scripts/ae_01_local_order_probe.py \
        --stage plan --vita-source vendor/vita/source \
        --output-dir deployment/runs/ae-local-order-probe-<UTC>

Why this exists: in the sealed local run of 2026-09-15 the model was asked for
"规格 7分糖" while the exact catalogue value "规格: 7分糖" existed only in the background
database and the oracle, and the case also required a dietary-restriction fact the user
never gave. That input cannot fairly test exact copying, so this probe runs the SAME
frozen purchase need and the SAME native database under an explicitly versioned,
information-COMPLETE case: the catalogue value is stated verbatim, and the user states
that there is no dietary restriction. It is NOT claimed to be the old input.

What this answers (nothing more): with complete information, an empty history and only
the two necessary tools, can the local model obtain the order id from the REAL create
return and then pay it, with the exact catalogue specification? One success neither
proves stability nor decides the model choice, and is NOT a model-capability claim.

Boundaries, all recorded in `plan.json`:

* the endpoint is the fixed loopback ``http://127.0.0.1:8001`` (vLLM, OpenAI wire) and the
  model is the fixed ``Qwen/Qwen3.5-9B``; there is NO way to point this probe at a cloud
  origin or another model, and it re-validates the endpoint is loopback before any send;
* `plan` (default) never opens a socket; only an explicit `run` calls the model, in a NEW
  output directory, at most 3 requests, never retried, never concurrent;
* output 2048, temperature 0, and the thinking switch is sent EXPLICITLY as
  ``chat_template_kwargs.enable_thinking = false`` (the local deployment's shape);
* the tools are executed by the REAL frozen Vita environment; every request, response,
  tool return and database snapshot is kept;
* the whole provider batch is processed before any decision, and the native field check
  plus the payment-id provenance are recorded IMMEDIATELY after the batch that contained
  the successful payment - then the run ends, on success AND on failure, so a later
  request can never overwrite the real reason with "no tool call";
* send accounting keeps the minimal honest split: `requests_attempted` (about to call) vs
  `responses_received` (an HTTP response actually arrived). A request that was attempted
  but never answered is never counted as executed by the service. `model_called` is a
  TRANSPORT-ATTEMPT marker set before the call - it does not assert that the service
  received or ran anything; `responses_received` and the per-step fields carry that.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "ae-local-order-probe-0.1"
#: The case is versioned EXPLICITLY: it is NOT the old input, and the old version is kept.
CASE_VERSION = "local-order-complete-info-v1"
CASE_SUPERSEDES = "the sealed 2026-09-15 local run's implicit case (unnamed, incomplete)"
#: Fixed local vLLM endpoint and model. Absolute by construction, loopback by validation.
ENDPOINT = "http://127.0.0.1:8001"
CHAT_PATH = "/v1/chat/completions"
MODEL_NAME = "Qwen/Qwen3.5-9B"
IO_TIMEOUT_SECONDS = 180.0
MAX_REQUESTS = 3
REQUEST_OUTPUT_TOKENS = 2048
TEMPERATURE = 0.0
THINKING_FIELD = "enable_thinking"
THINKING_VALUE = False
TOOLS = ("create_delivery_order", "pay_delivery_order")
CODE_FILES = ("ae_adapter.py", "ae_inputs.py", "ae_probe.py", "ae_task_run.py",
              "scripts/ae_01_native_order_probe.py", "scripts/ae_01_local_order_probe.py")

SYSTEM_PROMPT = (
    "你是任务环境中的个人助手。用户已明确给出完成操作所需的全部信息，包括商品规格的"
    "目录原值和饮食禁忌情况，不需要再向用户询问。请调用工具真实完成任务；以工具的真实"
    "回包为依据，没有成功回包不得声称已完成。"
)
USER_REQUEST = (
    "我是 U1，身份已确认并授权本次操作。请在门店 S1 为我购买 1 件商品 P1（Milk Tea），"
    "送到 Addr，配送时间 2024-06-23 15:00:00，并完成支付。"
    "下单时商品规格按目录原值填写：P1 的目录规格原值是“规格: 7分糖”，"
    "attributes 请填写该目录原值。另外，本次用户明确表示无特殊饮食禁忌。"
)
#: Every fact this version ADDS to what was visible before, stated as the prompt states it.
COMPLETION_FACTS = {
    "catalog_attribute_value": {"value": "规格: 7分糖",
                                "statement": "P1 的目录规格原值是“规格: 7分糖”，attributes 按目录原值填写",
                                "was_visible_before": False},
    "user_dietary_restriction": {"value": "无特殊饮食禁忌",
                                 "statement": "本次用户明确表示无特殊饮食禁忌",
                                 "was_visible_before": False},
    "product_identity": {"product_id": "P1", "name": "Milk Tea", "store_id": "S1",
                         "quantity": 1, "was_visible_before": True},
    "purchase_requirement": {"user_id": "U1", "address": "Addr",
                             "dispatch_time": "2024-06-23 15:00:00",
                             "was_visible_before": True},
}
STATUS_PLAN = "PLAN_FROZEN"
STATUS_PASSED = "LOCAL_ORDER_PROBE_PASSED"
STATUS_INCOMPLETE = "MODEL_TASK_INCOMPLETE"
STATUS_CLAIMED = "MODEL_CLAIMED_WITHOUT_TOOL_CALL"
STATUS_LENGTH = "MODEL_OUTPUT_TRUNCATED_INCOMPLETE"
STATUS_UNCERTAIN = "UNCERTAIN_TOOL_OUTCOME_INVALID"
STATUS_INVALID = "INVALID"


def _load_native():
    """The frozen native probe, loaded by path: build_case/schemas/_execute/order checks."""
    spec = importlib.util.spec_from_file_location(
        "ae_native_order_probe_for_local", ROOT / "scripts/ae_01_native_order_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_local_endpoint(endpoint: str = ENDPOINT) -> str:
    """Refuse anything that is not plain HTTP on a loopback host. No cloud, ever."""
    parts = urlsplit(endpoint)
    if parts.scheme != "http":
        raise ValueError(f"the local probe speaks plain HTTP on loopback only: {endpoint!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment or not parts.port:
        raise ValueError(f"the local endpoint must be a bare origin with a port: {endpoint!r}")
    host = parts.hostname or ""
    literal = host.strip("[]")
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        raise ValueError("the local endpoint must be a numeric loopback address: "
                         f"{endpoint!r}") from None
    if not address.is_loopback:
        raise ValueError(f"refusing a non-loopback address: {endpoint!r}")
    return endpoint


def endpoint_url(endpoint: str = ENDPOINT) -> str:
    return validate_local_endpoint(endpoint) + CHAT_PATH


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def ensure_output_dir(path):
    """A NEW directory only: never resume and never overwrite."""
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


def chat_body(native, messages, schemas) -> bytes:
    """The frozen case's request shape on the LOCAL wire: fixed model + explicit thinking off."""
    body = {
        "model": MODEL_NAME,
        "messages": messages,
        "tools": [{"type": "function", "function": deepcopy(schemas[name])} for name in TOOLS],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "temperature": TEMPERATURE,
        "max_tokens": REQUEST_OUTPUT_TOKENS,
        "chat_template_kwargs": {THINKING_FIELD: THINKING_VALUE},
    }
    return native.dumps(body).encode("utf-8")


def build_case_version(native, vita_source: Path) -> dict:
    """The frozen native case (database, schemas, bindings) under the new version label."""
    case = native.build_case(vita_source)
    document = {
        "case_version": CASE_VERSION,
        "supersedes": CASE_SUPERSEDES,
        "same_frozen_purchase_requirement": True,
        "same_native_database": True,
        "not_claimed_identical_to_the_old_input": True,
        "old_version_unchanged": True,
        "completion_facts": deepcopy(COMPLETION_FACTS),
        "system_prompt": SYSTEM_PROMPT,
        "user_request": USER_REQUEST,
        "database": deepcopy(native.CASE_DB),
        "expected_order": deepcopy(native.EXPECTED_ORDER),
        "expected_spec": native.EXPECTED_SPEC,
        "expected_spec_matches_declared_fact": (
            COMPLETION_FACTS["catalog_attribute_value"]["value"] == native.EXPECTED_SPEC),
        "schemas": {name: deepcopy(case["schemas"][name]) for name in TOOLS},
    }
    document["case_sha256"] = _sha256_bytes(native.dumps(document).encode("utf-8"))
    return document


def build_plan(vita_source: Path, endpoint: str = ENDPOINT) -> dict:
    native = _load_native()
    case_doc = build_case_version(native, vita_source)
    return {
        "schema_version": SCHEMA_VERSION,
        "case_version": CASE_VERSION,
        "purpose": "local_9b_complete_information_order_chain_screen_not_an_experiment",
        "status": STATUS_PLAN, "network_called": False, "model_called": False,
        "task_success": None, "scientific_result": None,
        "stage_defaults_to_plan_and_never_opens_a_socket": True,
        "endpoint": {"origin": validate_local_endpoint(endpoint),
                     "chat_path": CHAT_PATH, "loopback_only": True,
                     "model": MODEL_NAME, "wire": "OpenAI chat/completions (vLLM)"},
        "model": MODEL_NAME,
        "case": case_doc,
        "exposed_tools": {name: case_doc["schemas"][name] for name in TOOLS},
        "only_these_tools_exposed": True,
        "budget": {"max_requests": MAX_REQUESTS, "request_output_tokens": REQUEST_OUTPUT_TOKENS,
                   "temperature": TEMPERATURE, "thinking": {THINKING_FIELD: THINKING_VALUE},
                   "thinking_placement": "chat_template_kwargs",
                   "no_retry": True, "no_concurrency": True,
                   "no_resume_same_directory": True,
                   "io_timeout_seconds": IO_TIMEOUT_SECONDS,
                   "send_accounting": ("requests_attempted is counted before the call; "
                                       "responses_received only when a response arrived; "
                                       "an unanswered attempt is never 'executed by the service'")},
        "decision_rules": [
            "the whole provider batch is processed before any decision",
            "the native field check and the payment-id provenance are recorded immediately "
            "after the batch containing the successful payment, and the run then ENDS - on "
            "success and on failure alike - so a later request cannot overwrite the reason",
            "success requires a successful payment whose id came from an earlier real create "
            "return in THIS run, plus an exact native match of every frozen field",
            "the product specification is compared EXACTLY with the catalogue value; the "
            "check lives in the frozen native probe and is not re-implemented here",
            "a verbal claim of completion is never proof; the final text alone cannot pass",
            "without any tool call the run is incomplete - unless the database is already in "
            "the complete correct paid state, which is then reported as such",
            "fabricated ids, uncertain tool outcomes, non-200 responses and length-truncated "
            "output are each recorded as themselves",
        ],
        "case_scope": [
            "this case tests EXECUTION with complete information only",
            "it cannot test whether the model asks when a restriction is UNKNOWN",
            "the new facts were added after observing a failure, so a pass here is a revised "
            "observation, not an unbiased re-run of the old case",
        ],
        "scope": ["fixed Vita environment, no Letta, no cloud, no user simulator, no judge",
                  "an order is an in-memory Vita record, not a real transaction",
                  "one success is not stability evidence and makes no model-capability claim"],
        "provenance": {"code_sha256": {name: _sha256_file(ROOT / name) for name in CODE_FILES},
                       "python": sys.version, "python_executable": sys.executable},
    }


def _native_messages(native, case, messages):
    vita = case["vita"]
    if not messages:
        return [vita["SystemMessage"](role="system", content=SYSTEM_PROMPT),
                vita["UserMessage"](role="user", content=USER_REQUEST)]
    return messages


def _post(opener, url, body):
    """One send. Returns (status, body_bytes, None) or (None, None, error_record).

    An HTTP error status is an ANSWER from the service and is returned as such (the
    caller records it and stops); only a send that produced no HTTP answer at all comes
    back as a failure record.
    """
    request = Request(url, data=body, headers={"Content-Type": "application/json"},
                      method="POST")
    try:
        with opener.open(request, timeout=IO_TIMEOUT_SECONDS) as response:
            return response.code, response.read(), None
    except HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - the status alone is still evidence
            raw = b""
        return exc.code, raw, None
    except Exception as exc:  # noqa: BLE001 - a transport failure is evidence, not a retry
        return None, None, {"error_type": type(exc).__name__, "error_detail": str(exc)[:500]}


def execute_probe(*, vita_source: Path, output_dir: Path, endpoint: str = ENDPOINT,
                  opener=None, clock=time.monotonic) -> dict:
    """One explicit RUN: at most MAX_REQUESTS requests to the fixed loopback endpoint."""
    native = _load_native()
    url = endpoint_url(endpoint)
    out = ensure_output_dir(output_dir)
    plan = build_plan(vita_source, endpoint)
    save(out / "plan.json", plan)
    case = native.build_case(vita_source)
    document = build_case_version(native, vita_source)
    save(out / "case.json", document)
    clock = clock or time.monotonic
    # Capture whether the CALLER injected the opener BEFORE the default is built: a real
    # run must not report itself as a fixture (and a fixture must not hide as a real run).
    injected_opener = opener is not None
    opener = opener if opener is not None else build_opener(ProxyHandler({}))
    vita = case["vita"]
    environment = case["environment"]
    bindings = case["bindings"]
    messages = _native_messages(native, case, [])
    created_ids, created_sources, steps = [], {}, []
    result = {"schema_version": SCHEMA_VERSION, "case_version": CASE_VERSION,
              "status": STATUS_INVALID, "purpose": plan["purpose"],
              "endpoint": url, "model": MODEL_NAME, "model_called": False,
              "model_called_definition": (
                  "model_called is a TRANSPORT-ATTEMPT marker: it is set when this probe "
                  "starts a call to the local service. It does NOT assert that the service "
                  "received or executed the request - that is what responses_received and "
                  "the per-step response_received/failure fields state."),
              "task_success": None, "scientific_result": None,
              "requests_attempted": 0, "requests_sent": 0, "responses_received": 0,
              "created_order_ids": created_ids, "payment_source_verified": None,
              "steps": steps, "errors": [], "accounting_ok": True,
              "send_accounting_note": (
                  "requests_attempted counts the call about to be made; responses_received "
                  "counts answers that arrived, so an unanswered attempt is never evidence "
                  "that the service executed it (requests_sent is kept only as a legacy "
                  "alias of requests_attempted and proves nothing by itself)"),
              "started_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        for index in range(1, MAX_REQUESTS + 1):
            body = chat_body(native, vita["format_messages"](messages), case["schemas"])
            save(out / f"request-{index}.json", json.loads(body.decode("utf-8")))
            result["requests_attempted"] = index
            # The attempt marker is set BEFORE the call: whether the service answered is
            # decided afterwards by `responses_received`, never by this flag.
            result["model_called"] = True
            started = clock()
            http_status, raw, failure = _post(opener, url, body)
            # The call was made; only an ANSWER may be counted as received. `requests_sent`
            # is a legacy alias of the attempt count - it is NOT proof of egress, because a
            # send whose connection never established is counted here too.
            result["requests_sent"] = index
            step = {"index": index, "request_sha256": _sha256_bytes(body),
                    "request_messages": json.loads(body.decode("utf-8"))["messages"],
                    "elapsed_seconds": round(clock() - started, 3),
                    "tool_calls": [], "assistant_text": None}
            steps.append(step)
            if failure is not None:
                # Sent but never answered: whether the service executed it is UNKNOWN.
                step.update(http_status=None, response_sha256=None,
                            response_received=False, failure=failure)
                result["status"] = STATUS_INVALID
                result["accounting_ok"] = False
                result["accounting_error"] = "send_not_answered_provider_execution_unknown"
                result["stop_reason"] = "request_not_answered_no_retry"
                result["errors"].append({"type": "no_response", "message": failure})
                return result
            # An injected OFFLINE fixture may answer through the opener contract the
            # production code uses; normalise it to the body bytes either way.
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw)
            elif isinstance(raw, tuple) and len(raw) == 2:
                raw = raw[1]
            else:
                result["status"] = STATUS_INVALID
                result["stop_reason"] = "unusable_provider_response_object"
                result["errors"].append({"type": "unusable_response",
                                         "message": repr(type(raw))})
                return result
            (out / f"response-{index}.json").write_bytes(raw)
            result["responses_received"] = index
            step.update(http_status=http_status, response_sha256=_sha256_bytes(raw),
                        response_received=True)
            if http_status != 200:
                # An HTTP error answer: recorded as itself, never retried, whole run stops.
                step["response_body_utf8"] = raw[:2000].decode("utf-8", "replace")
                result.update(status=STATUS_INVALID, stop_reason=f"provider_http_{http_status}",
                              task_success=False)
                result["errors"].append({"type": "provider_http",
                                         "message": f"HTTP {http_status}"})
                return result
            try:
                parsed = json.loads(raw.decode("utf-8"))
                choice = parsed["choices"][0]
                message = choice["message"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                result["status"] = STATUS_INVALID
                result["stop_reason"] = "unparseable_provider_response"
                result["errors"].append({"type": "unparseable_response",
                                         "message": f"{type(exc).__name__}: {exc}"})
                return result
            step["finish_reason"] = choice.get("finish_reason")
            step["usage"] = parsed.get("usage")
            if step["finish_reason"] == "length":
                step["assistant_text"] = message.get("content")
                result.update(status=STATUS_LENGTH, stop_reason="finish_reason_length",
                              task_success=False)
                return result
            calls = message.get("tool_calls") or []
            if not calls:
                step["assistant_text"] = message.get("content")
                return _finish_without_tool_call(native, result, step, environment)
            native_calls = []
            for call in calls:
                name = (call.get("function") or {}).get("name")
                raw_arguments = (call.get("function") or {}).get("arguments")
                try:
                    arguments = (json.loads(raw_arguments) if isinstance(raw_arguments, str)
                                 else raw_arguments)
                except ValueError:
                    arguments = None
                if not isinstance(arguments, dict):
                    result["status"] = STATUS_INVALID
                    result["stop_reason"] = "unparseable_model_arguments"
                    result["errors"].append({
                        "type": "unparseable_model_arguments",
                        "message": f"{name}: {raw_arguments!r}"})
                    return result
                native_calls.append(vita["ToolCall"](id=call.get("id", ""), name=name,
                                                     arguments=arguments))
            messages.append(vita["AssistantMessage"](role="assistant",
                                                     content=message.get("content"),
                                                     tool_calls=native_calls))
            for call, native_call in zip(calls, native_calls):
                name = native_call.name
                arguments = native_call.arguments
                if name not in bindings:
                    outcome = {"status": "error", "text": f"Error: tool not available: {name}",
                               "kind": "undeclared_tool", "dispatched": False}
                else:
                    outcome = native._execute(bindings[name], name, arguments)
                record = {"tool_call_id": native_call.id, "name": name,
                          "arguments": arguments, "status": outcome["status"],
                          "kind": outcome["kind"], "dispatched": outcome["dispatched"],
                          "tool_return": outcome["text"]}
                if outcome["status"] == "success" and name == "create_delivery_order":
                    found = native.order_id_from(outcome["text"])
                    if found and found not in created_sources:
                        created_ids.append(found)
                        created_sources[found] = index
                        record["created_order_id"] = found
                if name == "pay_delivery_order":
                    record["order_id"] = arguments.get("order_id")
                    record["order_id_from_an_earlier_request_create"] = (
                        record["order_id"] in created_sources
                        and created_sources[record["order_id"]] < index)
                    if (outcome["status"] == "success"
                            and "first_successful_payment" not in step):
                        step["first_successful_payment"] = record["tool_call_id"]
                step["tool_calls"].append(record)
                step["native_snapshot"] = native.case_snapshot(environment)
                if outcome["status"] == "uncertain":
                    result.update(status=STATUS_UNCERTAIN,
                                  stop_reason="uncertain_tool_outcome_no_replay",
                                  task_success=False)
                    result["errors"].append({"type": "uncertain_tool_outcome",
                                             "message": outcome["kind"]})
                    result["final_native_snapshot"] = step["native_snapshot"]
                    return result
                text = outcome["text"] if outcome["text"] is not None else "Error: unknown outcome"
                messages.append(vita["ToolMessage"](id=native_call.id, name=name, role="tool",
                                                    content=text, requestor="assistant"))
            # The whole batch is processed. Record the native check IMMEDIATELY, then stop
            # if this batch paid - on success AND on failure - so nothing can overwrite it.
            payments = [record for record in step["tool_calls"]
                        if record["name"] == "pay_delivery_order"
                        and record["status"] == "success"]
            if payments:
                return _finish_after_payment(native, result, step, environment, created_sources)
            if index == MAX_REQUESTS:
                result.update(status=STATUS_INCOMPLETE, stop_reason="request_budget_exhausted",
                              task_success=False)
                result["final_native_snapshot"] = native.case_snapshot(environment)
                return result
        return result
    finally:
        if "final_native_snapshot" not in result:
            result["final_native_snapshot"] = native.case_snapshot(environment)
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        # The ORIGINAL value: a real run must not be reported as a fixture run.
        result["fixture_used"] = injected_opener
        save(out / "final-database.json", result["final_native_snapshot"])
        save(out / "result.json", result)


def _finish_after_payment(native, result, step, environment, created_sources):
    """The successful payment's batch ends the run: record the check, pass or fail."""
    first = next(record for record in step["tool_calls"]
                 if record["tool_call_id"] == step.get("first_successful_payment"))
    order_id = first.get("order_id")
    result["payment_source_verified"] = first["order_id_from_an_earlier_request_create"]
    result["payment_id_created_in_request"] = created_sources.get(order_id)
    result["paid_order_id"] = order_id
    orders = native.case_snapshot(environment)["orders"]
    matched, detail = native.order_matches(orders)
    result["order_state_check"] = detail
    result["order_state_matched"] = matched
    result["db_paid_order_ids"] = sorted(orders)
    result["final_native_snapshot"] = native.case_snapshot(environment)
    tail = step["tool_calls"][step["tool_calls"].index(first) + 1:]
    result["batch_tail_after_payment"] = [
        {"tool_call_id": record["tool_call_id"], "name": record["name"],
         "status": record["status"], "kind": record["kind"],
         "dispatched": record["dispatched"]} for record in tail]
    violations = [record for record in tail if record["kind"] in ("schema", "undeclared_tool")]
    if violations:
        result.update(status=STATUS_INCOMPLETE, stop_reason="batch_tail_protocol_violation",
                      task_success=False)
    elif result["payment_source_verified"] and matched:
        result.update(status=STATUS_PASSED, stop_reason="created_then_paid_real_order",
                      task_success=True, model_task_completed=True)
    else:
        result.update(status=STATUS_INCOMPLETE, task_success=False,
                      model_task_completed=False,
                      stop_reason=("payment_id_without_this_run_create_source"
                                   if not result["payment_source_verified"]
                                   else "paid_state_mismatch"))
    return result


def _finish_without_tool_call(native, result, step, environment):
    """No tool call: incomplete UNLESS the database is already complete and correct.

    The reason is recorded as itself and is never rewritten by a later request, because
    the run ends here. A verbal claim of completion is never proof of completion.
    """
    orders = native.case_snapshot(environment)["orders"]
    matched, detail = native.order_matches(orders)
    result["order_state_check"] = detail
    result["order_state_matched"] = matched
    result["final_native_snapshot"] = native.case_snapshot(environment)
    result["db_paid_order_ids"] = sorted(orders)
    if matched:
        # The state itself is complete; the text is still not what proves it.
        result.update(status=STATUS_PASSED, stop_reason="complete_state_without_a_tool_call",
                      task_success=True, model_task_completed=True)
    else:
        result.update(status=STATUS_CLAIMED, stop_reason="no_tool_call",
                      task_success=False, model_task_completed=False)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=("plan", "run"), default="plan")
    parser.add_argument("--vita-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.stage == "plan":
            out = ensure_output_dir(args.output_dir)
            plan = build_plan(args.vita_source)
            save(out / "plan.json", plan)
            print(f"AE local order probe PLAN; no network/model; model={plan['model']}; "
                  f"endpoint={plan['endpoint']['origin']}; "
                  f"case_version={plan['case_version']}; output={out}")
            return 0
        result = execute_probe(vita_source=args.vita_source, output_dir=args.output_dir)
        print(f"AE local order probe {result['status']}; "
              f"attempted={result['requests_attempted']} "
              f"responses={result['responses_received']}; "
              f"task_success={result['task_success']}; scientific_result=null; "
              f"output={args.output_dir}")
        return 0
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - reported, not hidden
        failure = {"schema_version": SCHEMA_VERSION, "status": STATUS_INVALID,
                   "stage": args.stage, "scientific_result": None,
                   "invalid_reasons": [{"type": type(exc).__name__, "message": str(exc)}]}
        try:
            if args.output_dir.is_dir() and not (args.output_dir / "result.json").exists():
                save(args.output_dir / "result.json", failure)
        except Exception:
            pass
        print(f"AE local order probe INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
