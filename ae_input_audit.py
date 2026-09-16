"""Read-only rendered-input audit for the bounded AE-01 t4-t5 wiring run.

This checks captured inputs, not preference correctness, task scores, KV reuse,
or absence of effects outside the captured run. Raw files are never rewritten.
Letta archive 56ba9c2: system.py:126-168 defines optional user/time and tool
status/message/time wrappers; rest_api/utils.py:164-185 leaves normal users
unwrapped and wraps string client tool returns. schemas/message.py:1457-1475
can truncate that wrapper a second time. We compare the recovered *string*,
not parsed inner JSON, IDs alone, or the presence of a truncation marker.
schemas/memory.py:143-173 defines the standard memory-block rendering.
Vita f60169e8 utils/llm_utils.py:122-154 defines auxiliary message projection.
"""
from __future__ import annotations

import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote

from ae_adapter import MEMORY_TOOL
from ae_model_proxy import ProxyConfig, decode_object, tokenize_projection


class AuditFailure(ValueError):
    pass


def need(condition, code):
    if not condition:
        raise AuditFailure(code)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc(value):
    need(isinstance(value, str), "missing_utc_timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AuditFailure("malformed_utc_timestamp") from None
    need(stamp.tzinfo is not None and stamp.utcoffset().total_seconds() == 0,
         "timestamp_not_utc")
    return stamp


def read_stable(path, report):
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    need((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
         "input_changed_during_read")
    report["sources"][str(path)] = {"sha256": digest(raw), "bytes": len(raw), "mtime_ns": after.st_mtime_ns}
    return raw


def rows(path, report):
    raw = read_stable(path, report)
    need(raw.endswith(b"\n"), "journal_missing_terminal_newline")
    records = [decode_object(line) for line in raw.splitlines()]
    need(bool(records), "empty_journal")
    prior = None
    for i, row in enumerate(records):
        need(row.get("sequence") == i, "journal_sequence_gap_or_duplicate")
        stamp = utc(row.get("timestamp"))
        need(prior is None or stamp >= prior, "journal_clock_reversed")
        prior = stamp
    return records


def wire(row):
    try:
        raw = base64.b64decode(row["body_base64"], validate=True)
    except (KeyError, ValueError):
        raise AuditFailure("missing_or_invalid_raw_body") from None
    need(row.get("body_bytes") == len(raw) and row.get("body_sha256") == digest(raw),
         "raw_body_digest_mismatch")
    if "body_utf8" in row:
        need(raw.decode("utf-8") == row["body_utf8"], "raw_body_utf8_mismatch")
    return raw


def only(records, kind, purpose=None):
    found = [r for r in records if r.get("kind") == kind
             and (purpose is None or r.get("purpose") == purpose)]
    need(len(found) == 1, "missing_or_duplicate_" + kind + ("_" + purpose if purpose else ""))
    return found[0]


def plain_text(content):
    if isinstance(content, str):
        return content
    # A single text part is lossless. Joining multiple parts would hide a seam.
    if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)):
        return content[0]["text"]
    raise AuditFailure("unsupported_non_single_text_content")


def user_text(content, expected):
    text = plain_text(content)
    if text == expected:
        return text
    try:
        wrapped = json.loads(text)
    except ValueError:
        raise AuditFailure("user_content_changed") from None
    need(isinstance(wrapped, dict) and wrapped.get("type") == "user_message"
         and set(wrapped) <= {"type", "message", "time", "location", "name"}
         and isinstance(wrapped.get("time"), str) and wrapped.get("message") == expected,
         "user_content_changed")
    return wrapped["message"]


def tool_text(message, returned):
    need(message.get("tool_call_id") == returned["tool_call_id"], "tool_id_changed")
    wrapped = decode_object(plain_text(message.get("content")).encode("utf-8"))
    need(set(wrapped) == {"status", "message", "time"} and isinstance(wrapped["time"], str),
         "unsupported_tool_wrapper")
    need(wrapped["status"] == ("OK" if returned["status"] == "success" else "Failed"),
         "tool_status_changed")
    need(isinstance(wrapped["message"], str) and wrapped["message"] == returned["tool_return"],
         "tool_return_content_changed")
    return wrapped["message"]


def tool_schemas(items, openai=False):
    need(isinstance(items, list), "tools_not_list")
    schemas = {}
    for item in items:
        if openai:
            need(item.get("type") == "function", "unsupported_tool_type")
            item = item.get("function")
        need(isinstance(item, dict) and isinstance(item.get("name"), str), "invalid_tool_schema")
        name = item["name"]
        need(name not in schemas, "duplicate_tool_name")
        schemas[name] = {k: item.get(k) for k in ("name", "description", "parameters")}
    return schemas


def assistant_message(message):
    need(isinstance(message, dict) and message.get("role") == "assistant", "invalid_assistant_message")
    content = message.get("content")
    need(content is None or isinstance(content, str), "unsupported_assistant_content")
    calls = []
    for call in message.get("tool_calls") or []:
        need(call.get("type") == "function" and isinstance(call.get("id"), str)
             and isinstance(call.get("function"), dict), "invalid_assistant_tool_call")
        calls.append({"id": call["id"], "name": call["function"].get("name"),
                      "arguments": call["function"].get("arguments")})
    # Letta's text-part conversion uses empty string for an absent TextContent
    # alongside reasoning parts. Only this empty/None distinction is equivalent.
    return {"role": "assistant", "content": content or "", "tool_calls": calls}


def proxy_calls(records, report):
    opened = only(records, "proxy_open")
    config = ProxyConfig(**opened["config"])
    config.validate()
    report["proxy_closed"] = bool([r for r in records if r.get("kind") == "proxy_close"])
    report["proxy_config"] = opened["config"]
    bad = [r for r in records if r.get("kind") in {"blocked", "client_rejected", "upstream_response_prefix"}]
    need(not bad, "proxy_blocked_rejected_or_incomplete_response")
    allowed = {"proxy_open", "proxy_close", "client_request", "upstream_request", "upstream_response",
               "token_gate", "model_summary"}
    need(all(r.get("kind") in allowed for r in records), "unknown_proxy_event")
    grouped = defaultdict(list)
    for row in records:
        if row.get("kind") not in {"proxy_open", "proxy_close"}:
            need(isinstance(row.get("request_id"), str), "missing_proxy_request_id")
            grouped[row["request_id"]].append(row)
            if row["kind"] in {"client_request", "upstream_request", "upstream_response"}:
                wire(row)
    calls = []
    for rid, group in grouped.items():
        request = only(group, "client_request")
        raw = wire(request)
        if (request["method"], request["path"]) == ("GET", "/v1/models"):
            need(len(group) == 3 and raw == b"", "unexpected_models_call")
            sent, reply = only(group, "upstream_request", "models"), only(group, "upstream_response", "models")
            need(sent.get("path") == "/v1/models" and reply.get("http_status") == 200,
                 "models_request_failed")
            continue
        need((request["method"], request["path"]) == ("POST", "/v1/chat/completions"), "unknown_model_route")
        need(len(group) == 7, "incomplete_or_extra_model_call_events")
        gate, summary = only(group, "token_gate"), only(group, "model_summary")
        token_req = only(group, "upstream_request", "preflight_tokenize")
        token_res = only(group, "upstream_response", "preflight_tokenize")
        sent, reply = only(group, "upstream_request", "model_inference"), only(group, "upstream_response", "model_inference")
        need([r["sequence"] for r in (request, token_req, token_res, gate, sent, reply, summary)] ==
             sorted(r["sequence"] for r in (request, token_req, token_res, gate, sent, reply, summary)),
             "model_event_order_wrong")
        body, response = decode_object(raw), decode_object(wire(reply))
        projected, reserve = tokenize_projection(body, config)
        need(decode_object(wire(token_req)) == projected and token_req.get("path") == "/tokenize",
             "tokenization_input_not_same_projection")
        need(wire(sent) == raw and sent.get("path") == request["path"], "proxy_changed_model_request")
        tokenized = decode_object(wire(token_res))
        count = tokenized.get("count")
        need(type(count) is int and count >= 0 and isinstance(tokenized.get("tokens"), list)
             and len(tokenized["tokens"]) == count
             and all(type(t) is int and t >= 0 for t in tokenized["tokens"]), "invalid_tokenize_record")
        need(token_res.get("http_status") == 200 and reply.get("http_status") == 200
             and summary.get("http_status") == 200, "model_http_not_200")
        need(gate.get("passed") is True and gate.get("prompt_tokens") == count
             and gate.get("output_reserve") == reserve and tokenized.get("max_model_len") == config.context_window
             and gate.get("context_window") == config.context_window
             and gate.get("max_prompt_tokens") == config.max_prompt_tokens
             and count <= config.max_prompt_tokens and count + reserve <= config.context_window,
             "token_gate_failed_or_inconsistent")
        usage, choices = response.get("usage"), response.get("choices")
        need(isinstance(usage, dict) and type(usage.get("prompt_tokens")) is int
             and usage["prompt_tokens"] == count and summary.get("usage") == usage
             and summary.get("tokenize_prompt_usage_agreement") is True, "usage_missing_or_unequal")
        need(isinstance(choices, list) and len(choices) == 1, "missing_or_multiple_choices")
        reasons = [c.get("finish_reason") for c in choices]
        need(reasons[0] in {"stop", "tool_calls"} and summary.get("finish_reasons") == reasons
             and summary.get("truncation_finish") is False and summary.get("response_parse_error") is None,
             "model_finish_not_clean")
        need(request.get("role") in {"agent_or_unknown", "user_simulator", "evaluator"}, "unknown_model_role")
        need(summary.get("role") == request["role"] == gate.get("role"), "model_role_changed")
        calls.append({"id": rid, "request": request, "summary": summary, "body": body,
                      "response": response, "start": utc(request["timestamp"]),
                      "end": utc(summary["timestamp"]), "role": request["role"]})
    need(bool(calls), "no_model_calls")
    report["model_call_counts"] = dict(Counter(c["role"] for c in calls))
    return sorted(calls, key=lambda c: c["start"])


def http_pairs(records):
    need(records[0].get("kind") == "transport_open", "letta_transport_open_missing")
    pending, pairs = {}, []
    for row in records[1:]:
        kind = row.get("kind")
        if kind == "transport_close":
            continue
        need(kind in {"request", "response"}, "letta_http_failed_or_unknown_event")
        rid = row.get("request_id")
        need(isinstance(rid, str), "missing_letta_request_id")
        if kind == "request":
            need(rid not in pending and not pending, "overlapping_or_duplicate_letta_request")
            pending[rid] = row
        else:
            need(rid in pending and 200 <= row.get("http_status", 0) < 300, "unmatched_or_failed_letta_response")
            request = pending.pop(rid)
            need(utc(request["timestamp"]) < utc(row["timestamp"]), "letta_window_empty")
            pairs.append((request, row))
    need(not pending, "incomplete_letta_request")
    return pairs


def memory_render(block, value):
    need(isinstance(value, str), "memory_block_not_text")
    readonly = "\n- read_only=true" if block.get("read_only") else ""
    return ("<ae_preferences>\n<description>\n" + (block.get("description") or "") +
            "\n</description>\n<metadata>" + readonly + "\n- chars_current=" + str(len(value)) +
            "\n- chars_limit=" + str(block["limit"]) + "\n</metadata>\n<value>\n" + value +
            "\n</value>\n</ae_preferences>\n")


def frame_check(call, state, body, report):
    actual = call["body"].get("messages")
    need(isinstance(actual, list) and actual and actual[0].get("role") == "system", "system_not_first")
    need(sum(m.get("role") == "system" for m in actual) == 1, "extra_system_message")
    system = plain_text(actual[0].get("content"))
    need(state["system"] in system, "initial_profile_or_system_changed")
    render = memory_render(state["block"], state["value"])
    need(system.count("<memory_blocks>") == 1 and system.count("</memory_blocks>") == 1
         and system.count("<ae_preferences>") == 1 and render in system, "memory_block_changed_or_missing")
    memory_body = system.split("<memory_blocks>", 1)[1].split("</memory_blocks>", 1)[0]
    need(memory_body == "\nThe following memory blocks are currently engaged in your core memory unit:\n\n" + render + "\n",
         "unexpected_memory_block_or_rendering")
    need(tool_schemas(call["body"].get("tools", []), True) == tool_schemas(body["client_tools"]),
         "actual_tool_schemas_changed")
    expected = state["history"]
    need(len(actual) - 1 == len(expected), "history_count_changed_or_extra_message")
    for observed, item in zip(actual[1:], expected):
        need(observed.get("role") == item["role"], "history_order_or_role_changed")
        if item["role"] == "user":
            user_text(observed.get("content"), item["content"])
        elif item["role"] == "tool":
            tool_text(observed, item["returned"])
        else:
            need(assistant_message(observed) == item, "assistant_history_changed")
    report["frames"].append({"request_id": call["id"], "agent_id": state["id"], "arm": state["arm"],
                              "user_messages": sum(x["role"] == "user" for x in expected),
                              "tool_returns": sum(x["role"] == "tool" for x in expected),
                              "block_sha256": digest(state["value"].encode()), "passed": True})
    state["history"].append(assistant_message(call["response"]["choices"][0]["message"]))


def native_messages(items):
    need(isinstance(items, list), "missing_native_messages")
    output = []
    for item in items:
        need(isinstance(item, dict), "unsupported_native_message")
        role = item.get("role")
        need(role in {"system", "user", "assistant", "tool"}, "unsupported_native_message_role")
        row = {"role": role, "content": item.get("content")}
        if role == "assistant" and item.get("tool_calls"):
            row["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                "name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in item["tool_calls"]]
        if role == "tool":
            row.update(tool_call_id=item["id"], name=item["name"])
        output.append(row)
    return output


def no_nulls(value):
    """SDK model_dump adds absent optional dictionary fields as None."""
    if isinstance(value, dict):
        return {k: no_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [no_nulls(v) for v in value]
    return value


def auxiliary_check(events, calls, report):
    snapshots = {}
    for event in events:
        if event.get("kind") == "stage_complete":
            arm, snapshot = event.get("arm"), event.get("snapshot", {})
            native = snapshot.get("native_calls")
            need(isinstance(native, list), "missing_auxiliary_capture")
            prior = snapshots.get(arm, [])
            need(native[:len(prior)] == prior, "native_call_history_changed")
            snapshots[arm] = native
    captured = [(arm, row) for arm, native in snapshots.items() for row in native]
    remaining = [c for c in calls if c["role"] != "agent_or_unknown"]
    need(len(captured) == len(remaining), "unaccounted_auxiliary_call_count")
    for arm, native in captured:
        req, reply = native.get("request"), native.get("response")
        need(native.get("error") is None and isinstance(req, dict) and isinstance(reply, dict), "native_call_incomplete")
        need(not req.get("tools"), "auxiliary_tools_not_supported")
        projected = native_messages(req.get("messages"))
        matches = [c for c in remaining if c["role"] == native.get("role")
                   and c["body"].get("messages") == projected
                   and no_nulls(c["response"]) == no_nulls(reply.get("raw_data"))]
        need(len(matches) == 1, "auxiliary_call_missing_ambiguous_or_changed")
        match = matches[0]
        for key in ("model", "temperature", "max_tokens", "seed"):
            if key in req:
                need(req[key] == match["body"].get(key), "auxiliary_parameters_changed")
        need(not match["body"].get("tools"), "unexpected_auxiliary_tools")
        remaining.remove(match)
        report["auxiliary_mappings"].append({"request_id": match["id"], "arm": arm,
                                              "role": native["role"], "task": native.get("subtask_id")})


def audit_inputs(run_dir: Path, proxy_journal: Path) -> dict:
    """Return explicit INVALID on missing/unsupported/inconsistent capture."""
    run_dir, proxy_journal = Path(run_dir), Path(proxy_journal)
    report = {"schema_version": "ae-input-audit-0.1", "status": "INVALID", "wiring_completed": False,
              "input_audit_passed": False, "validity_passed": False, "scientific_result": None,
              "sources": {}, "frames": [], "auxiliary_mappings": [], "invalid_reasons": [],
              "scope": "captured t4-t5 inputs; no score or KV conclusion",
              "limitations": ["same-host UTC timestamps must delimit exactly one agent inference per Letta POST",
                              "one standard ae_preferences block; text only; known Letta/Vita serialization",
                              "role annotations are checked against captures, not cryptographic identities",
                              "a stable journal snapshot does not authorize or certify future requests"]}
    try:
        result = decode_object(read_stable(run_dir / "result.json", report))
        events = rows(run_dir / "events.jsonl", report)
        http = rows(run_dir / "letta-http.jsonl", report)
        proxy = rows(proxy_journal, report)
        report["wiring_completed"] = result.get("wiring_completed") is True
        config = result.get("config", {})
        need(config.get("arms") == ["rewrite", "erratum"] and config.get("start_turn") == 4
             and config.get("end_turn") == 5, "not_paired_t4_t5_wiring")
        start, end = utc(result.get("started_at_utc")), utc(result.get("finished_at_utc"))
        calls = proxy_calls(proxy, report)
        need(all(start < c["start"] < c["end"] < end for c in calls), "proxy_call_outside_run_window")
        need(not any(e.get("kind") == "wiring_stopped" or
                     e.get("event", {}).get("kind") == "bridge_blocked" for e in events), "wiring_or_bridge_stopped")
        pairs = http_pairs(http)
        created_events = [e for e in events if e.get("kind") == "agent_created"]
        states, used_calls, traced_requests, traced_responses = {}, set(), [], []
        for event in events:
            if event.get("kind") == "bridge_event":
                inner = event.get("event", {})
                if inner.get("kind") == "request":
                    traced_requests.append(event)
                elif inner.get("kind") == "response":
                    traced_responses.append(event)
        tool_events = [e for e in events if e.get("kind") == "bridge_event"
                       and e.get("event", {}).get("kind") == "client_tool_result"]
        stage_starts = [e for e in events if e.get("kind") == "stage_start"]
        consumed_tool_events = set()
        for request, response in pairs:
            method, path, body = request.get("method"), request.get("path"), request.get("body")
            a, z = utc(request["timestamp"]), utc(response["timestamp"])
            if method == "POST" and path == "/v1/agents/":
                aid = response.get("body", {}).get("id")
                matches = [e for e in created_events if e.get("agent_id") == aid and e.get("payload") == body]
                need(len(matches) == 1 and aid not in states, "agent_create_capture_mismatch")
                arm = matches[0]["arm"]
                need(arm in {"rewrite", "erratum"} and not any(s["arm"] == arm for s in states.values()), "agent_arm_duplicate")
                need(body.get("initial_message_sequence") == [] and body.get("include_base_tools") is False
                     and body.get("tool_ids") == [] and body.get("message_buffer_autoclear") is False,
                     "unexpected_initial_memory_or_tools")
                blocks = body.get("memory_blocks")
                need(isinstance(blocks, list) and len(blocks) == 1 and blocks[0].get("label") == "ae_preferences", "unexpected_created_blocks")
                states[aid] = {"id": aid, "arm": arm, "system": body["system"], "block": blocks[0],
                               "value": blocks[0]["value"], "history": [], "pending": {}}
                continue
            match = re.fullmatch(r"/v1/agents/([^/?]+)(.*)", path or "")
            if not match:
                need(method == "GET" and path == "/v1/health/", "unknown_letta_endpoint")
                continue
            aid, suffix = unquote(match[1]), match[2]
            need(aid in states, "request_for_unknown_agent")
            state = states[aid]
            # The independent event stream must bracket and reproduce HTTP IO.
            trace_req = [e for e in traced_requests if e["arm"] == state["arm"] and
                         e["event"].get("method") == method and e["event"].get("path") == path
                         and e["event"].get("body") == body and utc(e["timestamp"]) < a]
            need(bool(trace_req), "http_request_missing_bridge_trace")
            chosen = trace_req[-1]
            traced_requests.remove(chosen)
            trace_res = [e for e in traced_responses if e["arm"] == state["arm"] and
                         e["event"].get("body") == response.get("body") and utc(e["timestamp"]) > z]
            need(bool(trace_res), "http_response_missing_bridge_trace")
            traced_responses.remove(trace_res[0])
            if method == "GET" and suffix.startswith("?"):
                inspected = response["body"]
                blocks = inspected.get("blocks")
                need(isinstance(blocks, list) and len(blocks) == 1 and blocks[0].get("value") == state["value"]
                     and blocks[0].get("label") == "ae_preferences" and inspected.get("tools") == [],
                     "inspected_memory_or_tools_changed")
                continue
            if method == "PATCH" and suffix == "/core-memory/blocks/ae_preferences":
                need(state["arm"] == "rewrite", "erratum_block_was_patched")
                need(set(body) == {"value"} and response["body"].get("value") == body["value"], "patch_not_confirmed")
                state["value"] = body["value"]
                continue
            need(method == "POST" and suffix == "/messages", "unknown_agent_operation")
            expected_tools = [MEMORY_TOOL]
            need(chosen.get("phase") in {"history", "task"}, "unrecognized_message_phase")
            if chosen["phase"] == "task":
                starts = [e for e in stage_starts if e.get("arm") == state["arm"]
                          and e.get("task") == chosen.get("task") and utc(e["timestamp"]) < a]
                need(len(starts) == 1 and isinstance(starts[0].get("tools"), list), "missing_stage_tool_contract")
                native_tools = starts[0]["tools"]
                need(all("memory" not in t.get("name", "").lower() for t in native_tools),
                     "second_memory_backend_in_stage_tools")
                expected_tools += native_tools
            need(tool_schemas(body.get("client_tools")) == tool_schemas(expected_tools),
                 "client_tools_differ_from_stage_contract")
            mapped = [c for c in calls if c["role"] == "agent_or_unknown" and a < c["start"] < c["end"] < z]
            need(len(mapped) == 1 and mapped[0]["id"] not in used_calls, "letta_model_call_mapping_not_one_to_one")
            call = mapped[0]
            used_calls.add(call["id"])
            for message in body["messages"]:
                if message.get("role") == "user" and message.get("type", "message") == "message":
                    need(not state["pending"], "new_user_before_pending_tool_return")
                    need(isinstance(message.get("content"), str), "user_input_not_string")
                    state["history"].append({"role": "user", "content": message["content"]})
                else:
                    need(message.get("type") == "tool_return", "unexpected_submitted_message")
                    for returned in message["tool_returns"]:
                        tid = returned.get("tool_call_id")
                        need(tid in state["pending"], "return_without_observed_model_call")
                        matching = [e for e in tool_events if e["sequence"] not in consumed_tool_events
                                    and e.get("arm") == state["arm"] and e["event"].get("result") == returned
                                    and e["event"].get("call") == state["pending"][tid]
                                    and utc(e["timestamp"]) < a]
                        need(len(matching) == 1, "tool_return_not_same_as_execution_event")
                        consumed_tool_events.add(matching[0]["sequence"])
                        need(matching[0]["event"].get("block_sha") == digest(state["value"].encode()), "tool_event_block_digest_changed")
                        state["history"].append({"role": "tool", "returned": returned})
                        del state["pending"][tid]
            need(not state["pending"], "unreturned_pending_tool_call")
            frame_check(call, state, body, report)
            raw_calls = state["history"][-1]["tool_calls"]
            expected_calls = [{"tool_call_id": x["id"], "name": x["name"], "arguments": x["arguments"]} for x in raw_calls]
            approvals = [m for m in response["body"].get("messages", []) if m.get("message_type") == "approval_request_message"]
            observed_calls = []
            for approval in approvals:
                observed_calls += approval.get("tool_calls") or [approval.get("tool_call")]
            need([{k: c.get(k) for k in ("tool_call_id", "name", "arguments")} for c in observed_calls] == expected_calls,
                 "model_tool_call_changed_before_execution")
            state["pending"] = {c["tool_call_id"]: c for c in expected_calls}
        need(len(states) == 2 and len(created_events) == 2, "missing_or_extra_created_agent")
        need(not traced_requests and not traced_responses, "unmatched_bridge_http_trace")
        need(len(consumed_tool_events) == len(tool_events), "executed_tool_result_not_submitted")
        need(len(used_calls) == sum(c["role"] == "agent_or_unknown" for c in calls), "unaccounted_agent_model_call")
        need(all(not s["pending"] for s in states.values()), "pending_tool_at_run_end")
        for state in states.values():
            arm = state["arm"]
            stored = result.get("arms", {}).get(arm, {})
            need(stored.get("agent_id") == state["id"], "result_agent_id_mismatch")
            completions = [e for e in events if e.get("kind") == "stage_complete" and e.get("arm") == arm]
            need(len(completions) == 2 and [e["stage"] for e in completions] == stored.get("stages"), "result_stage_capture_mismatch")
            need([e["stage"].get("subtask_id") for e in completions] == ["sub_U000828_4", "sub_U000828_5"], "wrong_or_incomplete_tasks")
            need(completions[-1]["stage"].get("block_text") == state["value"], "final_block_result_mismatch")
        auxiliary_check(events, calls, report)
        for path, captured in report["sources"].items():
            current = Path(path).stat()
            need((current.st_size, current.st_mtime_ns) == (captured["bytes"], captured["mtime_ns"]),
                 "capture_changed_during_audit")
        report["input_audit_passed"] = True
        report["validity_passed"] = report["wiring_completed"] and not result.get("invalid_reasons")
        need(report["validity_passed"], "wiring_not_completed_or_invalid")
        report["status"] = "VALID"
    except (Exception, KeyboardInterrupt) as exc:
        code = str(exc) if isinstance(exc, AuditFailure) else "malformed_or_unavailable_capture_" + type(exc).__name__
        report["invalid_reasons"].append({"code": code})
        report["validity_passed"] = False
    return report
