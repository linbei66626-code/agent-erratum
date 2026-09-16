#!/usr/bin/env python3
"""Native create->pay tool-chain probe: PLAN offline, RUN explicit, at most 3 requests.

    /root/agent-erratum/.venv-vita/bin/python -B scripts/ae_01_native_order_probe.py \
        --stage plan --transport-config configs/ae-01__cloud-transport__siliconflow.capability-pacing-candidate.json \
        --vita-source /root/agent-erratum/vendor/vita/source \
        --output-dir deployment/runs/ae-native-order-probe-<UTC>

What this answers (nothing more): with complete information, an empty history and
only the two necessary tools, can the current Qwen model obtain the order id from
the REAL create return and then pay it? One success would not prove stability and
says nothing about Letta or the long-context R/E protocol.

The probe talks to the fixed Vita environment directly (no Letta), executes tools
through the existing trusted `ae_task_run.environment_bindings`, and routes every
chat request through the existing strict `ae_cloud_proxy.CloudAuditProxy`. There is
no database, no Letta, no user simulator, no judge, no memory and no anti-repeat
hint. `plan` (default) never opens a socket; only an explicit `run` calls the
model, once per new output directory, at most three chat requests.

Frozen case: user U1, time 2024-06-23 10:00:00, empty orders, store S1, product
P1 (Milk Tea, 10, 规格: 7分糖), address Addr at (1.0, 2.0) (same as the store),
dispatch 2024-06-23 15:00:00. The user states identity, authorization and every
argument, so no preference inference or product search is required.

An order here is a record in Vita's in-memory database, not a real transaction.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ae_adapter import (RecoverableToolError, VerifiedWritePrecondition,  # noqa: E402
                        dumps, validate_declared_arguments)
from ae_cloud_proxy import (MODEL, CloudAuditProxy, CloudConfig,  # noqa: E402
                            read_private_key)
from ae_task_run import environment_bindings  # noqa: E402

SCHEMA_VERSION = "ae-native-order-probe-0.1"
CHAT_PATH = "/v1/chat/completions"
PURPOSE = "native-order-probe"
MAX_REQUESTS = 3
REQUEST_OUTPUT_TOKENS = 2048
TEMPERATURE = 0.0
TOOLS = ("create_delivery_order", "pay_delivery_order")
CODE_FILES = ("ae_adapter.py", "ae_cloud_proxy.py", "ae_task_run.py", "ae_inputs.py",
              "ae_probe.py", "scripts/ae_01_native_order_probe.py")

SYSTEM_PROMPT = (
    "你是任务环境中的个人助手。用户已明确给出完成操作所需的全部信息。"
    "请调用工具真实完成任务；以工具的真实回包为依据，没有成功回包不得声称已完成。"
)
USER_REQUEST = (
    "我是 U1，身份已确认并授权本次操作。请在门店 S1 为我购买 1 件商品 P1（Milk Tea，"
    "规格 7分糖），送到 Addr，配送时间 2024-06-23 15:00:00，并完成支付。"
)
CASE_DB = {
    "user_id": "U1",
    "time": "2024-06-23 10:00:00",
    "orders": {},
    "location": [{"address": "Addr", "longitude": 1.0, "latitude": 2.0}],
    "stores": {"S1": {
        "store_id": "S1", "name": "Store One", "score": 4.5,
        "location": {"longitude": 1.0, "latitude": 2.0, "address": "Addr"},
        "tags": ["tea"],
        "products": [{"product_id": "P1", "name": "Milk Tea", "store_id": "S1",
                      "store_name": "Store One", "attributes": ["规格: 7分糖"],
                      "tags": ["tea"], "quantity": 1, "price": 10.0}]}},
}
#: The frozen case's canonical product specification. The check is an EXACT match
#: against this complete value (never a substring search): this is a frozen small
#: case, not a general natural-language sugar parser.
EXPECTED_SPEC = "规格: 7分糖"
EXPECTED_ORDER = {"user_id": "U1", "store_id": "S1", "product_id": "P1", "quantity": 1,
                  "address": "Addr", "dispatch_time": "2024-06-23 15:00:00",
                  "attribute": EXPECTED_SPEC, "status": "paid"}
ORDER_ID_PATTERN = re.compile(r"order_id[:=]['\"]?([^,'\")\s]+)")
STATUS_PLAN = "PLAN_FROZEN"
STATUS_PASSED = "NATIVE_ORDER_PROBE_PASSED"
STATUS_INCOMPLETE = "MODEL_TASK_INCOMPLETE"
STATUS_CLAIMED = "MODEL_CLAIMED_WITHOUT_TOOL_CALL"
STATUS_UNCERTAIN = "UNCERTAIN_TOOL_OUTCOME_INVALID"
STATUS_INVALID = "INVALID"


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


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


#: Kept alive so the run-local EMPTY-key Vita config exists for the import.
_VITA_MODEL_CONFIG_DIR = None


def ensure_vita_model_config():
    """A run-local EMPTY-key models.yaml so importing the fixed Vita works.

    This probe never calls Vita's own LLM roles (no user simulator, no judge), and the
    real API key only ever goes to the strict CloudAuditProxy. An already declared
    VITA_MODEL_CONFIG_PATH is respected.
    """
    global _VITA_MODEL_CONFIG_DIR
    if os.environ.get("VITA_MODEL_CONFIG_PATH"):
        return os.environ["VITA_MODEL_CONFIG_PATH"]
    if _VITA_MODEL_CONFIG_DIR is None:
        _VITA_MODEL_CONFIG_DIR = tempfile.TemporaryDirectory(prefix="ae-order-probe-vita-")
        path = Path(_VITA_MODEL_CONFIG_DIR.name) / "models.yaml"
        path.write_text(json.dumps({"default": {}, "models": [
            {"name": "Qwen3-8B", "base_url": "http://127.0.0.1:8000/v1",
             "api_key": "EMPTY"}]}), encoding="utf-8")
        os.environ["VITA_MODEL_CONFIG_PATH"] = str(path)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    return os.environ["VITA_MODEL_CONFIG_PATH"]


def load_vita(vita_source: Path):
    """Import the FIXED Vita classes from the explicitly declared source.

    A declared but missing source is a hard error: this probe must never skip.
    """
    source = Path(vita_source)
    if not (source / "src/vita").is_dir():
        raise RuntimeError(f"fixed Vita source is missing: {source}")
    ensure_vita_model_config()
    sys.path.insert(0, str(source / "src"))
    from vita.data_model.message import (AssistantMessage, SystemMessage,  # noqa: E402
                                         ToolCall, ToolMessage, UserMessage)
    from vita.domains.delivery.environment import get_environment  # noqa: E402
    from vita.utils.llm_utils import format_messages  # noqa: E402
    return {"get_environment": get_environment, "format_messages": format_messages,
            "SystemMessage": SystemMessage, "UserMessage": UserMessage,
            "AssistantMessage": AssistantMessage, "ToolCall": ToolCall,
            "ToolMessage": ToolMessage}


def build_case(vita_source: Path):
    """The frozen environment, the two exposed schemas and the trusted bindings."""
    vita = load_vita(vita_source)
    environment = vita["get_environment"](db=deepcopy(CASE_DB))
    bindings = environment_bindings(environment)
    missing = [name for name in TOOLS if name not in bindings]
    if missing:
        raise RuntimeError(f"the fixed environment lacks the required tools: {missing}")
    exposed = {name: bindings[name] for name in TOOLS}
    return {"vita": vita, "environment": environment, "bindings": exposed,
            "schemas": {name: deepcopy(binding.schema) for name, binding in exposed.items()}}


def case_snapshot(environment):
    """Full native database snapshot (the probe's own state witness)."""
    return environment.tools.db.model_dump(mode="json")


def order_id_from(text):
    match = ORDER_ID_PATTERN.search(text or "")
    return match.group(1) if match else None


def order_matches(orders):
    """Exactly one new order, with the frozen case's fields, paid."""
    if len(orders) != 1:
        return False, f"expected exactly one order, found {len(orders)}"
    order = list(orders.values())[0]
    checks = {
        "user_id": order.get("user_id") == EXPECTED_ORDER["user_id"],
        "store_id": order.get("store_id") == EXPECTED_ORDER["store_id"],
        "dispatch_time": order.get("dispatch_time") == EXPECTED_ORDER["dispatch_time"],
        "status": order.get("status") == EXPECTED_ORDER["status"],
        "location": (order.get("location") or {}).get("address") == EXPECTED_ORDER["address"],
    }
    products = order.get("products") or []
    if len(products) != 1:
        checks["products"] = False
    else:
        product = products[0]
        checks["product_id"] = product.get("product_id") == EXPECTED_ORDER["product_id"]
        checks["quantity"] = product.get("quantity") == EXPECTED_ORDER["quantity"]
        # The native delivery product carries `attributes` as one complete string;
        # a list is accepted structurally, but every element is compared EXACTLY.
        # "不要7分糖，改5分糖", "5分糖" or "17分糖" must never satisfy the check.
        attributes = product.get("attributes")
        if isinstance(attributes, str):
            attributes = [attributes]
        elif not isinstance(attributes, list):
            attributes = []
        checks["attribute"] = attributes == [EXPECTED_ORDER["attribute"]]
    failed = sorted(name for name, ok in checks.items() if not ok)
    return (not failed), ("ok" if not failed else "mismatched fields: " + ", ".join(failed))


def build_plan(vita_source: Path, transport_config: Path, *, request_output_tokens: int,
               temperature: float) -> dict:
    case = build_case(vita_source)
    config_doc = json.loads(Path(transport_config).read_text(encoding="utf-8"))
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "native_create_pay_tool_chain_probe_not_preference_experiment",
        "status": STATUS_PLAN,
        "network_called": False, "model_called": False,
        "task_success": None, "scientific_result": None,
        "case": {"db": deepcopy(CASE_DB), "expected_order": deepcopy(EXPECTED_ORDER),
                 "user_request": USER_REQUEST, "system_prompt": SYSTEM_PROMPT,
                 "case_sha256": sha256_bytes(dumps(CASE_DB).encode("utf-8"))},
        "exposed_tools": {name: case["schemas"][name] for name in TOOLS},
        "only_these_tools_exposed": True,
        "budget": {"max_requests": MAX_REQUESTS, "request_output_tokens": request_output_tokens,
                   "temperature": temperature, "seed": None,
                   "no_retry": True, "no_resume_same_directory": True,
                   "tool_choice": "auto", "parallel_tool_calls": False},
        "transport_config": {"path": str(transport_config),
                             "sha256": sha256_bytes(Path(transport_config).read_bytes()),
                             "document": config_doc},
        "model": MODEL,
        "scope": ["fixed Vita environment, no Letta, no database, no user simulator, no judge",
                  "no memory and no anti-repeat hint",
                  "an order is an in-memory Vita record, not a real transaction",
                  "one success does not establish stability or any R/E conclusion"],
        "decision_rules": [
            "the whole provider batch is processed in provider order before any decision",
            "success requires exactly one correct paid order whose payment id came from an earlier "
            "request's real create return, with no unprocessed call and no uncertain side effect",
            "redundant actions after a successful payment are recorded as redundancy, not as task failure",
            "protocol violations in a batch tail (undeclared tool or invalid arguments) are recorded "
            "and are never a clean pass",
            "the final native order state is checked only after the batch has no unprocessed calls",
            "the product specification is matched EXACTLY against the frozen canonical value "
            "(see case.expected_order.attribute); this is a frozen small case, not a general "
            "natural-language sugar parser",
        ],
        "id_dependency_rule": [
            "every payment order_id must come from a previous successful create return in THIS run",
            "a payment id with no such source is recorded as a model proposal error and handled by the real error path",
            "a create and a guessed payment in the same batch never lets the driver fill the id",
        ],
        "provenance": {"code_sha256": {name: sha256_bytes((ROOT / name).read_bytes())
                                       for name in CODE_FILES},
                       "python": sys.version, "python_executable": sys.executable},
    }


def _chat_body(messages, schemas, *, request_output_tokens: int, temperature: float) -> bytes:
    return dumps({
        "model": MODEL,
        "messages": messages,
        "tools": [{"type": "function", "function": deepcopy(schemas[name])} for name in TOOLS],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "temperature": temperature,
        "max_completion_tokens": request_output_tokens,
    }).encode("utf-8")


def _execute(binding, name, arguments):
    """Execute one model call through the trusted binding; never fabricate a result."""
    if not isinstance(arguments, dict):
        return {"status": "error", "text": "Error: arguments must be a JSON object",
                "kind": "schema", "dispatched": False}
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


def execute_probe(case, transport_config: Path, *, request_output_tokens: int,
                  temperature: float, api_key: str, journal_path: Path, opener=None,
                  clock=None, sleep=None):
    """One explicit RUN: at most MAX_REQUESTS chat requests over the strict proxy."""
    document = json.loads(Path(transport_config).read_text(encoding="utf-8"))
    document = dict(document)
    document["max_requests"] = MAX_REQUESTS
    config = CloudConfig(**document)
    proxy = CloudAuditProxy(config, Path(journal_path), api_key=api_key,
                            **_proxy_timing(clock, sleep))
    if opener is not None:
        proxy.opener = opener  # offline fixtures only; RUN keeps the real opener
    vita, environment, bindings = case["vita"], case["environment"], case["bindings"]
    messages = [vita["SystemMessage"](role="system", content=SYSTEM_PROMPT),
                vita["UserMessage"](role="user", content=USER_REQUEST)]
    created_ids, created_sources, steps = [], {}, []
    result = {"schema_version": SCHEMA_VERSION, "status": STATUS_INVALID,
              "validity_passed": False, "model_task_completed": False,
              "task_success": None, "scientific_result": None,
              "model_called": False, "requests_sent": 0, "stop_reason": None,
              "created_order_ids": created_ids, "payment_source_verified": None,
              "steps": steps, "errors": [],
              "started_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        for index in range(1, MAX_REQUESTS + 1):
            body = _chat_body(vita["format_messages"](messages), case["schemas"],
                              request_output_tokens=request_output_tokens,
                              temperature=temperature)
            result["requests_sent"] = index
            result["model_called"] = True
            status, reply = proxy.dispatch("POST", CHAT_PATH, body, PURPOSE, "agent")
            step = {"index": index, "http_status": status, "request_sha256": sha256_bytes(body),
                    "request_messages": json.loads(body.decode("utf-8"))["messages"],
                    "response_sha256": sha256_bytes(reply), "tool_calls": [], "assistant_text": None}
            steps.append(step)
            if status != 200:
                result["status"] = STATUS_INVALID
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
                result["status"] = STATUS_INVALID
                result["stop_reason"] = "unparseable_provider_response"
                result["errors"].append({"type": "unparseable_response", "message": str(exc)})
                return result
            calls = message.get("tool_calls") or []
            if not calls:
                # A verbal claim of completion is not a tool success.
                step["assistant_text"] = message.get("content")
                result["status"] = STATUS_CLAIMED
                result["stop_reason"] = "no_tool_call"
                result["validity_passed"] = True
                result["model_task_completed"] = False
                result["task_success"] = False
                return result
            native_calls, parsed_arguments = [], {}
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
                    result["errors"].append({"type": "unparseable_model_arguments",
                                             "message": f"{name}: {raw_arguments!r}"})
                    return result
                parsed_arguments[call.get("id", "")] = arguments
                native_calls.append(vita["ToolCall"](id=call.get("id", ""), name=name,
                                                     arguments=arguments))
            messages.append(vita["AssistantMessage"](
                role="assistant", content=message.get("content"), tool_calls=native_calls))
            for call, native in zip(calls, native_calls):
                name = native.name
                if name not in bindings:
                    outcome = {"status": "error", "text": f"Error: tool not available: {name}",
                               "kind": "undeclared_tool", "dispatched": False}
                else:
                    outcome = _execute(bindings[name], name, arguments_of(call))
                record = {"tool_call_id": native.id, "name": name,
                          "arguments": arguments_of(call), "status": outcome["status"],
                          "kind": outcome["kind"], "dispatched": outcome["dispatched"],
                          "tool_return": outcome["text"]}
                if outcome["status"] == "success" and name == "create_delivery_order":
                    found = order_id_from(outcome["text"])
                    if found and found not in created_sources:
                        created_ids.append(found)
                        created_sources[found] = index
                        record["created_order_id"] = found
                if name == "pay_delivery_order":
                    # The provenance of every payment proposal is recorded, whether
                    # the payment succeeded or the real error path refused it.
                    record["order_id"] = arguments_of(call).get("order_id")
                    record["order_id_from_an_earlier_request_create"] = (
                        record["order_id"] in created_sources
                        and created_sources[record["order_id"]] < index)
                    if outcome["status"] == "success" and "first_successful_payment" not in step:
                        step["first_successful_payment"] = record["tool_call_id"]
                step["tool_calls"].append(record)
                step["native_snapshot"] = case_snapshot(environment)
                if outcome["status"] == "uncertain":
                    result["status"] = STATUS_UNCERTAIN
                    result["stop_reason"] = "uncertain_tool_outcome_no_replay"
                    result["errors"].append({"type": "uncertain_tool_outcome",
                                             "message": outcome["kind"]})
                    result["final_native_snapshot"] = step["native_snapshot"]
                    return result
                text = outcome["text"] if outcome["text"] is not None else "Error: unknown outcome"
                messages.append(vita["ToolMessage"](id=native.id, name=name, role="tool",
                                                    content=text, requestor="assistant"))
            # The WHOLE provider batch has now been processed in provider order.
            # Nothing received may be dropped: a payment that succeeded early must
            # not swallow the tail of its own batch.
            payments = [record for record in step["tool_calls"]
                        if record["name"] == "pay_delivery_order"
                        and record["status"] == "success"]
            if payments:
                first = next(record for record in step["tool_calls"]
                             if record["tool_call_id"] == step.get("first_successful_payment"))
                tail = step["tool_calls"][step["tool_calls"].index(first) + 1:]
                result["batch_tail_after_success"] = [
                    {"tool_call_id": record["tool_call_id"], "name": record["name"],
                     "status": record["status"], "kind": record["kind"],
                     "dispatched": record["dispatched"]} for record in tail]
                order_id = first["order_id"]
                result["payment_source_verified"] = first["order_id_from_an_earlier_request_create"]
                result["payment_id_created_in_request"] = created_sources.get(order_id)
                orders = case_snapshot(environment)["orders"]
                matched, detail = order_matches(orders)
                result["order_state_check"] = detail
                result["paid_order_id"] = order_id
                result["db_paid_order_ids"] = sorted(orders)
                # Redundant actions after a successful payment are recorded as
                # redundancy; they are NOT by themselves a task failure. Only the
                # final order conditions (and any protocol violation) decide.
                result["redundant_actions"] = len(tail)
                protocol_violations = [record for record in tail
                                       if record["kind"] in ("schema", "undeclared_tool")]
                if protocol_violations:
                    result["status"] = STATUS_INCOMPLETE
                    result["validity_passed"] = True
                    result["model_task_completed"] = False
                    result["task_success"] = False
                    result["stop_reason"] = "batch_tail_protocol_violation"
                elif result["payment_source_verified"] and matched:
                    result["status"] = STATUS_PASSED
                    result["validity_passed"] = True
                    result["model_task_completed"] = True
                    result["task_success"] = True
                    result["stop_reason"] = "created_then_paid_real_order"
                else:
                    result["status"] = STATUS_INCOMPLETE
                    result["validity_passed"] = True
                    result["model_task_completed"] = False
                    result["task_success"] = False
                    result["stop_reason"] = ("payment_id_without_this_run_create_source"
                                             if not result["payment_source_verified"]
                                             else "paid_state_mismatch")
                return result
            if index == MAX_REQUESTS:
                result["status"] = STATUS_INCOMPLETE
                result["validity_passed"] = True
                result["model_task_completed"] = False
                result["task_success"] = False
                result["stop_reason"] = "request_budget_exhausted"
                result["final_native_snapshot"] = case_snapshot(environment)
                return result
        return result
    finally:
        result.setdefault("final_native_snapshot", case_snapshot(environment))
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        result["request_count"] = proxy.request_count
        result["proxy_blocked"] = proxy.blocked
        result["wire_journal"] = str(journal_path)
        result["fixture_used"] = opener is not None
        proxy.close()


def _proxy_timing(clock, sleep):
    """Real pacing/sleep by default; offline fixtures may inject a fast clock."""
    if clock is None and sleep is None:
        return {}
    import time as _time
    return {"clock": clock or _time.monotonic, "sleep": sleep or _time.sleep}


def arguments_of(call):
    """The model's arguments as a JSON object, or the raw value when malformed."""
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=("plan", "run"), default="plan")
    parser.add_argument("--vita-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--transport-config", required=True, type=Path)
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
        plan = build_plan(args.vita_source, args.transport_config,
                          request_output_tokens=args.request_output_tokens,
                          temperature=args.temperature)
        out = ensure_output_dir(args.output_dir)
        owned = True  # from here the directory is ours; only now may we report into it
        save(out / "plan.json", plan)
        if args.stage == "plan":
            print(f"AE native order probe PLAN; no network/model; output={out}")
            return 0
        key = read_private_key(args.key_file)
        case = build_case(args.vita_source)
        result = execute_probe(case, args.transport_config,
                               request_output_tokens=args.request_output_tokens,
                               temperature=args.temperature, api_key=key,
                               journal_path=out / "chat-wire.private.jsonl")
        result["plan_sha256"] = sha256_bytes((out / "plan.json").read_bytes())
        result["case_sha256"] = plan["case"]["case_sha256"]
        save(out / "result.json", result)
        print(f"AE native order probe {result['status']}; requests={result['requests_sent']}; "
              f"scientific_result=null; output={out}")
        return 0 if result["status"] == STATUS_PASSED else 2
    except (Exception, KeyboardInterrupt) as exc:
        failure = {"schema_version": SCHEMA_VERSION, "status": STATUS_INVALID,
                   "stage": args.stage, "validity_passed": False, "task_success": None,
                   "scientific_result": None,
                   "invalid_reasons": [{"type": type(exc).__name__, "message": str(exc)}]}
        try:
            # Zero-write on refusal: a directory this call did not exclusively create
            # is never written into, not even with a failure report.
            if owned and args.output_dir.is_dir() and not (args.output_dir / "result.json").exists():
                save(args.output_dir / "result.json", failure)
        except Exception:
            pass
        print(f"AE native order probe INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
