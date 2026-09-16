"""AE-01 single-task current-state capability probe (offline construction core).

One fresh Letta agent receives one public t4 with a fixed, read-only 7分糖
preference derived from the t4 history evidence, and no dataset_history, no
memory_update tool, no old conversation and no future information. This is a
capability diagnostic, not an R/E comparison, a memory-update experiment, an
official benchmark, or a per-turn gold injection for the main experiment.

The module never imports Vita, never builds a transport, and never touches the
network at import time. The private order oracle is derived from actual native
environment fields and is never injected into the Agent context.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import math
import re
import secrets
import time
from urllib.parse import quote

from ae_adapter import MemoryPolicy, creation_payload, dumps
from ae_probe import _origin
from ae_task_run import (
    CheckedTaskTransport, TaskBridge, environment_bindings, final_public_text,
)


SCHEMA_VERSION = "ae-01-capability-probe-0.1"
PURPOSE = "current_state_capability_diagnostic"
ORACLE_VERSION = "ae-01-capability-oracle-0.1"

USER_ID = "U000828"
TASK_NUMBER = 4
TASK_ID = "sub_U000828_4"
EVIDENCE_REF = "t4/history/15"
EVIDENCE_DIALOGUE_INDEX = 2
EVIDENCE_TEXT = "嗯！以后就7分糖了"
OLD_SUGAR_FACT = "奶茶偏好5分糖"
NEW_SUGAR_FACT = "奶茶偏好7分糖"
TARGET_PRODUCT_ID = "S17791041622763865_P00011"
EXPECTED_SUGAR = "7分糖"
WORK_ADDRESS_KEY = "工作地址"

# Explicit capability-probe config, pinned to the inspected 4B local stack.
FIELDS = {
    "schema_version", "purpose", "task_number", "model_origin", "letta_origin",
    "model_handle", "expected_model", "context_window", "max_output_tokens",
    "auxiliary_output_tokens", "temperature", "seed", "block_char_limit",
    "max_rounds", "max_steps", "max_stage_posts", "max_user_exchanges",
    "max_tool_return_chars", "timeout_seconds", "max_request_bytes",
    "max_response_bytes",
}
PINNED = {
    "schema_version": SCHEMA_VERSION,
    "purpose": PURPOSE,
    "task_number": TASK_NUMBER,
    "model_handle": "vllm/Qwen3-4B-Instruct-2507",
    "expected_model": "Qwen3-4B-Instruct-2507",
    "context_window": 65536,
    "max_output_tokens": 2048,
    "auxiliary_output_tokens": 4096,
    "temperature": 0,
    "seed": 300,
    "block_char_limit": 8000,
    "max_rounds": 32,
    "max_steps": 3,
    "max_stage_posts": 64,
    "max_user_exchanges": 12,
    "max_tool_return_chars": 26214,
    "timeout_seconds": 180,
    "max_request_bytes": 2097152,
    "max_response_bytes": 16777216,
}

CAPABILITY_SYSTEM = """你是任务环境中的个人助手。当前用户资料和长期偏好已经给出；本轮不允许修改偏好，也没有记忆更新工具。
请依据当前已知偏好和当前任务指令，使用可用工具真实完成操作；需要多步时逐步调用工具。
信息不足时可以向当前用户提问，等待真实回复后再继续。
没有工具的成功回包，就不要声称下单、支付或完成；不要臆造订单号、商品或地址。
只依据本次会话中的用户消息和工具回包描述结果。
"""

_SUGAR = re.compile(r"(?:[0-9]+分糖|全糖|无糖|半糖|正常糖|少糖|微糖)")


class CapabilityRejected(RuntimeError):
    """The capability-probe boundary failed; not a scientific task failure."""


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


_STRING_PINNED = {"schema_version", "purpose", "model_handle", "expected_model"}
_NUMERIC_PINNED = {"temperature", "timeout_seconds"}
_COUNT_PINNED = set(PINNED) - _STRING_PINNED - _NUMERIC_PINNED


def validate_config(config: dict) -> dict:
    """Validate the explicit, pinned 4B capability config; never reach the network.

    Count/limit fields must be real ints (a float such as 32.0 is rejected).
    Only temperature and timeout_seconds accept finite numeric values.
    """
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError("capability config must contain exactly the declared fields")
    for key, expected in PINNED.items():
        value = config[key]
        if key in _STRING_PINNED:
            valid = isinstance(value, str) and value == expected
        elif key in _NUMERIC_PINNED:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and value == expected)
        else:
            valid = type(value) is int and value == expected
        if not valid:
            raise ValueError(f"{key} must be the pinned capability value {expected!r}")
    for key in ("model_origin", "letta_origin"):
        _origin(config[key])
    if config["model_origin"] == config["letta_origin"]:
        raise ValueError("Letta and model origins must differ")
    if max(config["max_output_tokens"], config["auxiliary_output_tokens"]) >= config["context_window"]:
        raise ValueError("context must leave room for input")
    # Same fixed Letta V3 truncation guard used by the task-wiring driver.
    native_char_limit = max(5000, int(config["context_window"] * 0.8))
    if not _positive_int(config["max_tool_return_chars"]) or config["max_tool_return_chars"] > native_char_limit:
        raise ValueError("tool return guard cannot exceed the fixed Letta V3 truncation limit")
    return deepcopy(config)


def _evidence_record(task: dict) -> dict:
    matches = [h for h in task["history"] if h.get("ref") == EVIDENCE_REF]
    if len(matches) != 1:
        raise ValueError(f"t4 must retain exactly one {EVIDENCE_REF} evidence record")
    return matches[0]["record"]


def build_capability_inputs(sample: dict) -> dict:
    """Verify the public t4 evidence, then build the controlled read-only snapshot.

    Reads only the public t4 projection, t3 `initial_facts` and t3 profile. It
    never reads t4 current preference memory, evaluation_criteria,
    target_product_ids, or the complete environment.
    """
    if not isinstance(sample, dict):
        raise ValueError("capability inputs require a verified sample projection")
    tasks = sample.get("tasks")
    if (not isinstance(tasks, list) or [t.get("number") for t in tasks] != [4, 5]
            or sample.get("initial_facts") is None or sample.get("initial_profile") is None):
        raise ValueError("capability probe requires the prepared t4-t5 public projection")
    task = tasks[0]
    if set(task) != {"number", "subtask_id", "domain", "current_time", "instruction", "history"}:
        raise ValueError("t4 must be the public task projection only")
    if task["number"] != TASK_NUMBER or task["subtask_id"] != TASK_ID or task["domain"] != "delivery":
        raise ValueError("unexpected t4 identity")

    record = _evidence_record(task)
    dialogue = record.get("dialogue")
    if not isinstance(dialogue, list) or len(dialogue) <= EVIDENCE_DIALOGUE_INDEX:
        raise ValueError("t4 evidence dialogue is shorter than the replacement turn")
    message = dialogue[EVIDENCE_DIALOGUE_INDEX]
    if (not isinstance(message, dict) or message.get("role") != "user"
            or message.get("content") != EVIDENCE_TEXT):
        raise ValueError("t4 evidence lacks the explicit 以后就7分糖了 replacement statement")
    evidence_sha = hashlib.sha256(EVIDENCE_TEXT.encode("utf-8")).hexdigest()

    facts = deepcopy(sample["initial_facts"])
    if not isinstance(facts, dict) or not facts:
        raise ValueError("t3 initial facts must be a nonempty mapping")
    matches = [k for k, v in facts.items()
               if isinstance(v, dict) and v.get("content") == OLD_SUGAR_FACT]
    if len(matches) != 1:
        raise ValueError("t3 baseline must contain exactly one 奶茶偏好5分糖 fact")
    fid = matches[0]
    if any(v.get("content") == NEW_SUGAR_FACT for v in facts.values() if isinstance(v, dict)):
        raise ValueError("t3 baseline unexpectedly already carries the 7分糖 replacement")
    category = facts[fid]["category"]
    current = deepcopy(facts)
    current[fid] = {"category": category, "content": NEW_SUGAR_FACT}
    changed = {k for k in facts if facts[k] != current[k]}
    if changed != {fid} or len(current) != len(facts):
        raise ValueError("controlled snapshot may change only the single sugar fact")

    profile = deepcopy(sample["initial_profile"])
    if not isinstance(profile, dict) or profile.get("user_id") != USER_ID:
        raise ValueError("invalid t3 profile")
    work_address = profile.get(WORK_ADDRESS_KEY)
    if not isinstance(work_address, str) or not work_address.strip():
        raise ValueError("t3 profile must carry a nonempty work address for the private oracle")

    return {
        "task": deepcopy(task),
        "facts_control": facts,
        "facts_current": current,
        "profile": profile,
        "preference_edit": {
            "fact_id": fid, "category": category,
            "from": OLD_SUGAR_FACT, "to": NEW_SUGAR_FACT,
            "evidence_ref": EVIDENCE_REF,
            "evidence_dialogue_index": EVIDENCE_DIALOGUE_INDEX,
            "evidence_text": EVIDENCE_TEXT,
            "evidence_text_sha256": evidence_sha,
            "source": "public t4 history only; t4 current preference memory not read",
            "history_evidence_verified": True,
        },
        "diagnostic_only": True,
    }


def capability_payload(config: dict, facts: dict, profile: dict, *, name: str) -> tuple[dict, MemoryPolicy]:
    """Reuse creation_payload, then install the capability system and read-only block."""
    memory = MemoryPolicy("erratum", facts, block_char_limit=config["block_char_limit"])
    payload = creation_payload(name=name, model=config["model_handle"], profile=profile, memory=memory)
    payload["system"] = CAPABILITY_SYSTEM + "\n共同起点的用户资料：\n" + dumps(profile)
    payload["context_window_limit"] = config["context_window"]
    payload["model_settings"] = {
        "provider_type": "openai", "max_output_tokens": config["max_output_tokens"],
        "temperature": config["temperature"], "parallel_tool_calls": False, "strict": False,
    }
    return payload, memory


def current_task_message(task: dict, domain_policy: str) -> tuple[dict, str]:
    """Build the same current_task user message run_stage sends, with no history phase."""
    current = {"source": "current_task", "subtask_id": task["subtask_id"], "domain": task["domain"],
               "current_time": task["current_time"], "domain_policy": domain_policy,
               "instruction": task["instruction"]}
    ref = f"t{task['number']}/user/0"
    current["ref"] = ref
    return {"role": "user", "content": dumps(current)}, ref


def build_plan(config: dict, sample: dict, *, execution_profile=None) -> dict:
    config = (execution_profile.validate(config) if execution_profile else validate_config(config))
    inputs = build_capability_inputs(sample)
    payload, memory = capability_payload(config, inputs["facts_current"], inputs["profile"],
                                         name="ae-capability-qwen3-4b-GENERATED")
    if execution_profile:
        payload = execution_profile.payload(config, payload)
    plan = {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": "PLAN",
        "network_called": False, "model_called": False, "task_success": None,
        "scientific_result": None, "config": config, "source": deepcopy(sample.get("source")),
        "scope": {"user_id": USER_ID, "task_number": TASK_NUMBER, "subtask_id": TASK_ID,
                  "t5_executed": False, "dataset_history_sent": False,
                  "memory_update_tool": False, "read_only_preference": True},
        "task_preview": {
            "number": inputs["task"]["number"], "subtask_id": inputs["task"]["subtask_id"],
            "domain": inputs["task"]["domain"], "current_time": inputs["task"]["current_time"],
            "instruction": inputs["task"]["instruction"],
            "history_records": len(inputs["task"]["history"]),
            "history_sent_to_agent": False,
        },
        "inputs": {
            "facts_control": inputs["facts_control"], "facts_current": inputs["facts_current"],
            "profile": inputs["profile"], "preference_edit": inputs["preference_edit"],
            "diagnostic_only": True,
        },
        "agent_payload": payload,
        "initial_block": memory.initial_block,
        "boundaries": [
            "one fresh Letta agent, one public t4; t5 is never started",
            "no dataset_history message and no old conversation are sent",
            "memory_update is absent from client_tools and refused if submitted",
            "fixed read-only 7分糖 snapshot is a diagnostic, not the main per-turn gold injection",
            "native environment/user/evaluator and native termination predicate are retained",
            "all three native LLM roles may share Qwen3-4B; judge scores are debug only",
            "no silent summary/truncation/retry; a failed step is not reward zero",
            "unaudited input can never be marked VALID by this driver",
        ],
    }
    return execution_profile.annotate(plan) if execution_profile else plan


def _plain(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _db(environment):
    return getattr(getattr(environment, "tools", None), "db", None)


def environment_orders(environment) -> dict:
    """Plain JSON view of the live native environment's final orders."""
    orders = getattr(_db(environment), "orders", None)
    if orders is None:
        return {}
    plain = _plain(orders)
    if not isinstance(plain, dict):
        raise CapabilityRejected("native environment orders are not a mapping")
    return plain


def environment_stores(environment) -> dict:
    stores = getattr(_db(environment), "stores", None)
    if stores is None:
        return {}
    plain = _plain(stores)
    return plain if isinstance(plain, dict) else {}


def _sugar_tokens(attributes):
    if isinstance(attributes, str):
        texts = [attributes]
    elif isinstance(attributes, (list, tuple)):
        texts = [x for x in attributes if isinstance(x, str)]
    else:
        return []
    return _SUGAR.findall(" ".join(texts))


def catalog_sugar(stores: dict, product_id: str):
    """Diagnostic only: first catalog sugar token, never used to pass the oracle."""
    for store in stores.values():
        products = store.get("products") if isinstance(store, dict) else None
        for product in products or []:
            if isinstance(product, dict) and product.get("product_id") == product_id:
                tokens = _sugar_tokens(product.get("attributes"))
                return tokens[0] if tokens else None
    return None


def _diagnose_one(order_id, order, *, baseline_order_ids, stores, user_id,
                  target_product_id, expected_sugar, work_address):
    lines = [p for p in (order.get("products") or []) if isinstance(p, dict)]
    target_lines = [p for p in lines if p.get("product_id") == target_product_id]
    target_line = target_lines[0] if len(target_lines) == 1 else None
    # The ordered line must state the spec explicitly and unambiguously: exactly
    # one sugar token, equal to 7分糖. A missing or conflicting line spec never
    # passes, and a catalog option is diagnostic only (it lists choices, not the
    # selected spec).
    line_tokens = _sugar_tokens(target_line.get("attributes")) if target_line else []
    catalog = catalog_sugar(stores, target_product_id)
    location = order.get("location")
    address = location.get("address") if isinstance(location, dict) else None
    quantity = target_line.get("quantity") if target_line is not None else None
    criteria = {
        "newly_created": isinstance(order_id, str) and order_id not in baseline_order_ids,
        "not_cancelled": order.get("status") != "cancelled",
        "paid": order.get("status") == "paid",
        "owned_by_user": order.get("user_id") == user_id,
        "exactly_one_item": len(lines) == 1,
        "exactly_target_product": len(target_lines) == 1,
        "quantity_one": type(quantity) is int and quantity == 1,
        "sugar_7": target_line is not None and line_tokens == [expected_sugar],
        "work_address": isinstance(address, str) and address.strip() == work_address,
    }
    return {
        "order_id": order_id,
        "criteria": criteria,
        "observed": {"status": order.get("status"), "user_id": order.get("user_id"),
                     "product_ids": [p.get("product_id") for p in lines],
                     "item_count": len(lines), "target_line_count": len(target_lines),
                     "target_quantity": quantity, "line_sugar_tokens": line_tokens,
                     "catalog_sugar_diagnostic_only": catalog, "address": address},
        "all_passed": all(criteria.values()),
    }


def diagnose_orders(orders: dict, *, baseline_order_ids, stores: dict, work_address: str,
                    user_id: str = USER_ID, target_product_id: str = TARGET_PRODUCT_ID,
                    expected_sugar: str = EXPECTED_SUGAR) -> dict:
    """Independent private oracle over the actual final native order records.

    It requires EXACTLY ONE newly created, non-cancelled order; that single order
    must be paid, owned by the user, carry exactly one item -- the target milk tea
    at quantity 1 with an explicit 7分糖 spec -- delivered to the profile work
    address. Any other new uncancelled order, an extra item, a duplicate target
    line, or quantity > 1 fails the oracle. This is never injected into the Agent.
    """
    if not isinstance(orders, dict):
        raise ValueError("orders must be the native order mapping")
    if not isinstance(stores, dict):
        raise ValueError("stores must be the native store mapping")
    baseline = set(baseline_order_ids)
    records = [
        _diagnose_one(order_id, order if isinstance(order, dict) else {},
                      baseline_order_ids=baseline, stores=stores, user_id=user_id,
                      target_product_id=target_product_id, expected_sugar=expected_sugar,
                      work_address=work_address)
        for order_id, order in orders.items()
    ]
    new_uncancelled = [r for r in records
                       if r["criteria"]["newly_created"] and r["criteria"]["not_cancelled"]]
    passing = [r["order_id"] for r in new_uncancelled if r["all_passed"]]
    return {
        "oracle_version": ORACLE_VERSION, "diagnostic_only": True,
        "injected_into_agent": False,
        "criteria": ["exactly_one_new_uncancelled_order", "newly_created", "not_cancelled",
                     "paid", "owned_by_user", "exactly_one_item", "exactly_target_product",
                     "quantity_one", "sugar_7", "work_address"],
        "expected": {"user_id": user_id, "target_product_id": target_product_id,
                     "expected_sugar": expected_sugar, "work_address": work_address},
        "baseline_order_count": len(baseline), "final_order_count": len(records),
        "new_uncancelled_order_ids": [r["order_id"] for r in new_uncancelled],
        "new_uncancelled_count": len(new_uncancelled),
        "orders": records, "passing_order_ids": passing,
        "oracle_passed": len(new_uncancelled) == 1 and new_uncancelled[0]["all_passed"],
        "note": "Diagnostic only; it never replaces the separate unaudited-input check.",
    }


def catalog_products(stores: dict) -> dict:
    """Flat product_id -> native catalog entry, from the actually loaded env."""
    found = {}
    for store in stores.values():
        for product in (store.get("products") if isinstance(store, dict) else None) or []:
            if isinstance(product, dict) and product.get("product_id"):
                found[product["product_id"]] = product
    return found


def tool_sequence(trace) -> list:
    sequence = []
    for event in trace:
        if event.get("kind") != "client_tool_result":
            continue
        call = event.get("call") or {}
        result = event.get("result") or {}
        sequence.append({"tool_call_id": call.get("tool_call_id"), "name": call.get("name"),
                         "arguments": call.get("arguments"), "status": result.get("status")})
    return sequence


def confirmation_review(transcript: list, trace) -> dict:
    """Record the real dialogue and native tool order for manual payment review."""
    return {
        "status": "MANUAL_REVIEW_REQUIRED",
        "payment_confirmation_by_user": None,
        "evidence_rule": ("No keyword or regex inference is used as strong evidence; "
                          "review the retained real dialogue and native tool sequence."),
        "tool_sequence": tool_sequence(trace),
        "transcript": deepcopy(transcript),
    }


def execute_capability(config: dict, sample: dict, *, model_transport, letta_transport,
                       runtime_factory, emit=lambda _: None, execution_profile=None) -> dict:
    """Run one fresh agent on t4 once. Never retries or repairs a failed step."""
    config = (execution_profile.validate(config) if execution_profile else validate_config(config))
    inputs = build_capability_inputs(sample)
    task, profile = inputs["task"], inputs["profile"]
    started = datetime.now(timezone.utc).isoformat()
    result = {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": "INVALID",
        "execution_complete": False, "validity_passed": False,
        "input_audit_passed": None, "capability_passed": None, "task_success": None,
        "scientific_result": None, "config": config, "source": deepcopy(sample.get("source")),
        "scope": {"user_id": USER_ID, "task_number": TASK_NUMBER, "subtask_id": TASK_ID,
                  "t5_executed": False},
        "agent_id": None, "agent_cleanup_performed": False,
        "execution": {"subtask_id": TASK_ID, "termination_reason": None, "model_posts": 0,
                      "duration_seconds": None, "transcript": [], "assistant_messages": [],
                      "partial_native_snapshot": None},
        "judge": {"status": "NOT_RUN", "reward_info": None, "error": None},
        "task_oracle": None, "confirmation_review": None,
        "input_audit": {"status": "PENDING", "input_audit_passed": None,
                        "note": "Full rendered-input audit is a separate post-run check; "
                                "absence of audit can never be VALID."},
        "native_snapshot": None, "invalid_reasons": [], "started_at_utc": started,
    }
    bridge = None
    if execution_profile:
        execution_profile.annotate(result)
    active = None
    termination = None
    baseline_order_ids = set()
    captured_orders, captured_stores, captured_transcript = None, None, None
    try:
        listed = model_transport.request("GET", "/v1/models")
        if execution_profile:
            provider_model = execution_profile.check_catalog(listed, config)
        else:
            matches = [m for m in listed.get("data", []) if m.get("id") == config["expected_model"]]
            if len(matches) != 1 or matches[0].get("max_model_len") != config["context_window"]:
                raise CapabilityRejected("provider model/window differs from the capability config")
            provider_model = matches[0]
        health = letta_transport.request("GET", "/v1/health/")
        if health.get("status") != "ok" or health.get("version") != "0.16.8":
            raise CapabilityRejected("unexpected Letta health/version")
        result["provider_model"], result["letta_health"] = deepcopy(provider_model), deepcopy(health)

        payload, memory = capability_payload(config, inputs["facts_current"], profile,
                                             name="ae-capability-qwen3-4b-" + secrets.token_hex(6))
        if execution_profile:
            payload = execution_profile.payload(config, payload)
            payload["name"] = "ae-capability-cloud-" + secrets.token_hex(6)
        created = letta_transport.request("POST", "/v1/agents/", payload)
        aid = created.get("id")
        if not isinstance(aid, str) or not aid:
            raise CapabilityRejected("agent creation returned no ID")
        result["agent_id"] = aid
        emit({"kind": "agent_created", "agent_id": aid, "payload": payload})
        path = "/v1/agents/" + quote(aid, safe="")
        bridge = TaskBridge(CheckedTaskTransport(letta_transport, path, config), aid, memory,
                            max_rounds=config["max_rounds"], max_steps=config["max_steps"],
                            config=config, emit=emit, include_memory_tool=False)
        bridge.verify_session()

        native = runtime_factory()
        active = native
        started_clock = time.monotonic()
        prepared = native.start(deepcopy(task))
        if prepared["instruction"] != task["instruction"]:
            raise CapabilityRejected("native user's first instruction differs from the public task")
        greeting = prepared["greeting"]
        if hasattr(greeting, "model_dump"):
            greeting = greeting.model_dump(mode="json")
        environment = prepared["environment"]
        baseline_order_ids = set(environment_orders(environment))
        bridge.begin(task, greeting)
        bindings = environment_bindings(environment)
        emit({"kind": "stage_start", "task": task["subtask_id"], "public_task": task,
              "domain_policy": prepared["domain_policy"],
              "tools": [v.schema for v in bindings.values()], "snapshot": native.snapshot()})

        message, ref = current_task_message(task, prepared["domain_policy"])
        bridge.visible_refs.add(ref)
        reply = bridge.exchange([message], bindings)
        bridge.current_bindings, bridge.last_turn, bridge.runtime_user_count = dict(bindings), task["number"], 0
        result["execution"]["assistant_messages"] = deepcopy(reply)
        text = final_public_text(reply)

        for _ in range(config["max_user_exchanges"]):
            if native.agent_stop(text):
                termination = "agent_stop"
                break
            user_reply = native.user_reply(text)
            emit({"kind": "simulated_user", "task": task["subtask_id"], "reply": user_reply})
            if user_reply["stop"]:
                bridge.transcript.append({"role": "user", "content": user_reply["content"]})
                termination = "user_stop"
                break
            text = final_public_text(bridge.reply_to_user(user_reply["content"]))
            if native.agent_stop(text):
                termination = "agent_stop"
                break
        if termination is None:
            raise CapabilityRejected("user exchange limit reached; no completed-task score")

        duration = time.monotonic() - started_clock
        # Capture real orders and dialogue BEFORE judging so a judge exception
        # can never erase them.
        captured_orders = environment_orders(environment)
        captured_stores = environment_stores(environment)
        captured_transcript = deepcopy(bridge.transcript)
        finished_snapshot = native.snapshot()
        result["execution"].update({"termination_reason": termination, "duration_seconds": duration,
                                    "model_posts": bridge.stage_posts,
                                    "transcript": captured_transcript})
        emit({"kind": "task_terminated", "task": task["subtask_id"],
              "termination_reason": termination, "transcript": captured_transcript,
              "snapshot": finished_snapshot})
        try:
            judged = native.finish(task, deepcopy(captured_transcript), termination, duration)
            result["judge"] = {"status": judged.get("judging_status", "MODEL_JUDGED_DEBUG_ONLY"),
                               "reward_info": deepcopy(judged.get("reward_info")),
                               "scientific_success": None, "error": None}
        except Exception as exc:
            result["judge"] = {"status": "JUDGE_FAILED_ORDERS_RETAINED", "reward_info": None,
                               "scientific_success": None,
                               "error": {"type": type(exc).__name__, "message": str(exc)}}
            # The native wrapper still owns the thread-local environment after a
            # judging failure; release it explicitly and keep the captured
            # orders/dialogue. This is cleanup, not a retry or a repair.
            try:
                active.abort()
                result["native_aborted_after_judge_failure"] = True
            except Exception as abort_error:
                result["abort_error"] = str(abort_error)
            active = None

        result["task_oracle"] = diagnose_orders(
            captured_orders, baseline_order_ids=baseline_order_ids, stores=captured_stores,
            work_address=profile[WORK_ADDRESS_KEY])
        result["confirmation_review"] = confirmation_review(captured_transcript, bridge.trace)
        result["execution_complete"] = True
        result["status"] = "CAPABILITY_COMPLETED_AUDIT_PENDING"
        result["native_snapshot"] = native.snapshot()
        emit({"kind": "task_complete", "task": task["subtask_id"],
              "task_oracle": result["task_oracle"], "snapshot": result["native_snapshot"]})
        active = None
    except (Exception, KeyboardInterrupt) as exc:
        result["invalid_reasons"].append({"type": type(exc).__name__, "message": str(exc)})
        if isinstance(exc, KeyboardInterrupt):
            result["interrupted"] = True
            result["server_side_work_cancelled"] = False
        if active is not None:
            try:
                result["execution"]["partial_native_snapshot"] = active.snapshot()
            except Exception as snapshot_error:
                result["snapshot_error"] = str(snapshot_error)
            finally:
                try:
                    active.abort()
                except Exception as abort_error:
                    result["abort_error"] = str(abort_error)
    finally:
        if bridge is not None:
            result["execution"]["partial_transcript"] = deepcopy(bridge.transcript)
            result["execution"]["bridge_trace"] = list(bridge.trace)
            if result["task_oracle"] is None:
                orders, stores = captured_orders, captured_stores
                if orders is None:
                    partial = result["execution"].get("partial_native_snapshot")
                    db = partial.get("environment_db") if isinstance(partial, dict) else None
                    if isinstance(db, dict):
                        orders, stores = db.get("orders"), db.get("stores")
                if isinstance(orders, dict):
                    try:
                        result["task_oracle"] = diagnose_orders(
                            orders, baseline_order_ids=baseline_order_ids,
                            stores=stores if isinstance(stores, dict) else {},
                            work_address=profile[WORK_ADDRESS_KEY])
                    except Exception as diagnose_error:
                        result["task_oracle_error"] = str(diagnose_error)
            if captured_transcript is not None and result["confirmation_review"] is None:
                result["confirmation_review"] = confirmation_review(captured_transcript, bridge.trace)
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result
