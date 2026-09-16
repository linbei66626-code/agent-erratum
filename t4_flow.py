#!/usr/bin/env python3
"""The ONE t4 execution flow, shared by every transport (local loopback and cloud API).

Why this module exists: the r3 probe drives the real t4 correctly - the native
environment, the 19 native tools, the native user conversation and the deterministic
private diagnosis - and a cloud screening entry must reuse THAT flow rather than copy it.
Copying it would mean two paths that can silently drift, which is exactly what this module
prevents: the loop below is the single implementation, and a transport only has to supply
`send`/`declare_window`/`descriptor`.

The flow is transport-agnostic on purpose:

* it never counts tokens itself and never assumes `/tokenize` or `max_model_len` exist -
  the transport's gate returns the operational numbers, so a cloud endpoint without those
  endpoints is a supported case rather than a broken one;
* it never builds a vendor payload - `runtime["mode"]["field"]` is the DECLARED reasoning
  mode and `runtime["mode"]["extra"]` the payload it adds, so vLLM's
  `chat_template_kwargs` and an API's own field can both be expressed without this module
  preferring either;
* it never talks to the model directly - the native user simulator's module-global
  `generate` is redirected to the transport for the duration of one native turn, so a real
  client can never bypass the counted gate.

Everything here is called with an explicit runtime, so the caller keeps ownership of its
own plan/preflight/result skeleton and its own endpoint policy.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _sha256_bytes(raw: bytes) -> str:
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def parse_reply(raw: bytes):
    parsed = json.loads(raw.decode("utf-8"))
    choice = parsed["choices"][0]
    return choice.get("finish_reason"), choice.get("message") or {}, parsed.get("usage")


def tool_calls_of(message):
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_arguments = function.get("arguments")
        try:
            arguments = (json.loads(raw_arguments) if isinstance(raw_arguments, str)
                         else raw_arguments)
        except ValueError:
            arguments = None
        calls.append({"id": call.get("id", ""), "name": function.get("name"),
                      "arguments": arguments, "raw_arguments": raw_arguments})
    return calls


def finish_after_payment(native_module, result, environment, public):
    """The paying batch ends the run; the deterministic private diagnosis decides it."""
    orders = native_module.case_snapshot(environment)["orders"]
    diagnosis = public["capability"].diagnose_orders(
        orders, baseline_order_ids=result["baseline_order_ids"],
        stores=native_module.case_snapshot(environment)["stores"],
        work_address=public["inputs"]["profile"][public["work_address_key"]],
        user_id=public["user_id"], target_product_id=public["target_product_id"],
        expected_sugar=public["expected_sugar"])
    result["order_diagnosis"] = diagnosis
    result["oracle_passed"] = diagnosis["oracle_passed"]
    result["task_success"] = bool(diagnosis["oracle_passed"])
    result["status"] = (public["status_passed"] if diagnosis["oracle_passed"]
                        else public["status_incomplete"])
    result["stop_reason"] = ("current_state_t4_executed_correctly"
                             if diagnosis["oracle_passed"] else "payment_state_failed_oracle")
    result["final_native_snapshot"] = native_module.case_snapshot(environment)
    return result


def finish_without_tool_call(native_module, result, environment, reason, status_incomplete):
    orders = native_module.case_snapshot(environment)["orders"]
    result["final_native_snapshot"] = native_module.case_snapshot(environment)
    result["order_diagnosis"] = None
    result["status"] = status_incomplete
    result["stop_reason"] = reason
    result["task_success"] = False
    result["final_order_count"] = len(orders)
    return result


def run_agent_loop(*, runtime, out, result, messages, tools, bindings,
                   helpers, native, public, max_agent_requests, aux_max_requests,
                   agent_output_tokens, agent_temperature, stop_marker):
    """The single t4 agent loop: request -> tools -> native user -> deterministic verdict.

    `runtime` carries the transport and the native-user factory; `helpers` carries the
    caller's own plan-level constants (status names, tool-return guard, snapshots) so this
    module never has to import a particular entry point.
    """
    native_module = helpers["native_module"]
    environment = native["environment"]
    transport = runtime["transport"]
    native_user = runtime["native_user"]
    status_invalid = helpers["status_invalid"]
    status_length = helpers["status_length"]
    status_uncertain = helpers["status_uncertain"]
    status_capacity = helpers["status_capacity"]
    save = helpers["save"]
    status_incomplete = public["status_incomplete"]
    response_path = out / "user-simulator.json"
    created = {}
    try:
        for index in range(1, max_agent_requests + 1):
            wire_messages = native["format_messages"](messages)
            result["model_called"] = True
            # The gate lives in the transport and runs BEFORE any generation: the counted
            # body is exactly the body that would be sent, by construction.
            try:
                status, raw, failure, gate = transport.send(
                    wire_messages, tools, role="agent", max_tokens=agent_output_tokens,
                    temperature=agent_temperature)
            except helpers["gate_refused"] as exc:
                _absorb_transport_state(result, transport)
                result["errors"].append({"type": exc.code, "message": str(exc.detail)[:500]})
                result["capacity_blocked_at_step"] = index
                result["status"] = (status_capacity
                                    if exc.code in ("capacity_blocked_before_send",
                                                    "total_post_budget_exhausted",
                                                    "request_byte_budget_exhausted")
                                    else status_invalid)
                result["stop_reason"] = exc.code
                return result
            gate = {"step": index, **gate}
            result["capacity"].append(gate)
            step = {"index": index, "request_sha256": _sha256_bytes(
                json.dumps(wire_messages, ensure_ascii=False).encode("utf-8")),
                "prompt_tokens": gate.get("count"), "gate": gate, "tool_calls": []}
            result["steps"].append(step)
            if failure is not None:
                step.update(http_status=None, response_received=False, failure=failure)
                result["status"] = status_invalid
                result["stop_reason"] = "request_not_answered_no_retry"
                result["errors"].append({"type": "no_response", "message": failure})
                return result
            (out / f"response-{index}.json").write_bytes(raw)
            step.update(http_status=status, response_sha256=_sha256_bytes(raw),
                        response_received=True)
            if status != 200:
                step["response_body_utf8"] = raw[:2000].decode("utf-8", "replace")
                result["status"] = status_invalid
                result["stop_reason"] = f"provider_http_{status}"
                result["errors"].append({"type": "provider_http", "message": f"HTTP {status}"})
                return result
            try:
                finish_reason, message, usage = parse_reply(raw)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                result["status"] = status_invalid
                result["stop_reason"] = "unparseable_provider_response"
                result["errors"].append({"type": "unparseable_response",
                                         "message": f"{type(exc).__name__}: {exc}"})
                return result
            step["finish_reason"] = finish_reason
            step["usage"] = usage
            if finish_reason == "length":
                step["assistant_text"] = message.get("content")
                result["status"] = status_length
                result["stop_reason"] = "finish_reason_length"
                result["task_success"] = False
                return result
            calls = tool_calls_of(message)
            step["assistant_text"] = message.get("content")
            if not calls:
                text = message.get("content") or ""
                if stop_marker in text:
                    return finish_without_tool_call(native_module, result, environment,
                                                    "agent_declared_stop", status_incomplete)
                if result["user_exchanges"] >= aux_max_requests:
                    result["status"] = status_incomplete
                    result["stop_reason"] = "user_exchange_budget_exhausted"
                    result["task_success"] = False
                    return result
                question = helpers["kinds"]["AssistantMessage"](role="assistant",
                                                               content=text)
                reply, user_stop, exchange = native_user.next_message(
                    native["format_messages"](messages + [question]))
                result["native_user_simulator"] = native_user.record
                _absorb_transport_state(result, transport)
                step["user_exchange"] = exchange
                if reply is None:
                    failure = exchange.get("failure") or {}
                    if exchange.get("refused_code") is not None:
                        # ONLY a pre-send gate refusal (zero chat) is a capacity refusal.
                        result["status"] = status_capacity
                        result["stop_reason"] = exchange["refused_code"]
                        result["errors"].append({"type": exchange["refused_code"],
                                                 "message": exchange.get("refused_detail")})
                    else:
                        # A request that WAS sent and answered badly keeps its own reason.
                        result["stop_reason"] = failure.get("code") or "user_simulator_failed"
                        result["status"] = (status_length
                                            if failure.get("code") == "user_response_length"
                                            else status_invalid)
                        if failure:
                            result["errors"].append({"type": failure.get("code"),
                                                     "message": failure.get("detail")})
                    result["task_success"] = None
                    return result
                result["user_exchanges"] += 1
                step["user_reply"] = reply
                messages = messages + [question,
                                       helpers["kinds"]["UserMessage"](role="user",
                                                                       content=reply)]
                if user_stop:
                    return finish_without_tool_call(native_module, result, environment,
                                                    "user_declared_stop", status_incomplete)
                continue
            for call in calls:
                if not isinstance(call["arguments"], dict):
                    result["status"] = status_invalid
                    result["stop_reason"] = "unparseable_model_arguments"
                    result["errors"].append({"type": "unparseable_model_arguments",
                                             "message": f"{call['name']}: "
                                                        f"{call['raw_arguments']!r}"})
                    return result
            assistant_message = helpers["kinds"]["AssistantMessage"](
                role="assistant", content=message.get("content"),
                tool_calls=[helpers["kinds"]["ToolCall"](id=call["id"], name=call["name"],
                                                         arguments=call["arguments"])
                            for call in calls])
            messages = messages + [assistant_message]
            for call in calls:
                name, arguments = call["name"], call["arguments"]
                if name not in bindings:
                    outcome = {"status": "error", "text": f"Error: tool not available: {name}",
                               "kind": "undeclared_tool", "dispatched": False}
                else:
                    outcome = native_module._execute(bindings[name], name, arguments)
                sent_text, guard = helpers["guarded_tool_return"](outcome["text"])
                record = {"tool_call_id": call["id"], "name": name, "arguments": arguments,
                          "status": outcome["status"], "kind": outcome["kind"],
                          "dispatched": outcome["dispatched"], "guard": guard,
                          "raw_return_sha256": _sha256_bytes(
                              (outcome["text"] or "").encode("utf-8")),
                          "sent_text_sha256": _sha256_bytes(sent_text.encode("utf-8"))}
                if outcome["status"] == "success" and name == "create_delivery_order":
                    found = native_module.order_id_from(outcome["text"])
                    if found and found not in created:
                        created[found] = index
                        result["created_order_ids"].append(found)
                        record["created_order_id"] = found
                if name == "pay_delivery_order":
                    record["order_id"] = arguments.get("order_id")
                    record["order_id_from_an_earlier_request_create"] = (
                        record["order_id"] in created and created[record["order_id"]] <= index)
                    if outcome["status"] == "success":
                        result["paid_order_ids"].append(record["order_id"])
                step["tool_calls"].append(record)
                result["tool_returns"].append({
                    "step": index, "name": name, "status": outcome["status"],
                    "raw_chars": guard["raw_chars"], "sent_chars": guard["sent_chars"],
                    "truncated": guard["truncated"], "raw_text": outcome["text"]})
                step["native_snapshot"] = native_module.case_snapshot(environment)
                if outcome["status"] == "uncertain":
                    result["status"] = status_uncertain
                    result["stop_reason"] = "uncertain_tool_outcome_no_replay"
                    result["task_success"] = None
                    result["errors"].append({"type": "uncertain_tool_outcome",
                                             "message": outcome["kind"]})
                    result["final_native_snapshot"] = step["native_snapshot"]
                    return result
                messages = messages + [helpers["kinds"]["ToolMessage"](
                    id=call["id"], name=name, role="tool", content=sent_text,
                    requestor="assistant")]
            if any(record["name"] == "pay_delivery_order"
                   and record["status"] == "success" for record in step["tool_calls"]):
                return finish_after_payment(native_module, result, environment, public)
            if index == max_agent_requests:
                result["status"] = status_incomplete
                result["stop_reason"] = "agent_request_budget_exhausted"
                result["task_success"] = False
                return result
        return result
    finally:
        if "final_native_snapshot" not in result:
            result["final_native_snapshot"] = native_module.case_snapshot(environment)
        result["requests_attempted"] = transport.attempted
        result["responses_received"] = transport.responses_received
        result["requests_by_role"] = dict(transport.by_role)
        result["total_model_posts"] = transport.inference_posts
        result["total_model_posts_within_budget"] = (
            transport.inference_posts <= helpers["total_max_posts"])
        result["service_posts_total"] = transport.attempted
        result["service_posts_including_tokenize"] = transport.attempted
        result["wire_journal"] = transport.journal
        result["gate_refusals"] = transport.gate_refusals
        _absorb_transport_state(result, transport)
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        # Written through the caller's own saver, so an entry point that also records the
        # result (for example to add its secret-hygiene check) does not collide with it.
        save(out / "final-database.json", result["final_native_snapshot"])
        save(out / "tool-returns.json", result["tool_returns"])
        save(response_path, native_user.record)


def _absorb_transport_state(result, transport) -> None:
    """Copy whatever the transport says about itself into the result.

    The transport is the only part that knows whether it can count tokens, so it reports
    its own identity and capacity basis; the flow never invents those numbers.
    """
    descriptor = getattr(transport, "descriptor", None)
    if callable(descriptor):
        result.update(descriptor())
