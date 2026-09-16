"""Synthetic capture fixtures only: no Letta, Vita, GPU or real model evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from ae_input_audit import audit_inputs, memory_render
from ae_adapter import MEMORY_TOOL
from ae_model_proxy import wire_record, tokenize_projection, ProxyConfig


TOOL = MEMORY_TOOL


class CaptureFixture:
    def __init__(self, root):
        self.root = root
        self.journals = {"events.jsonl": [], "letta-http.jsonl": [], "proxy.jsonl": []}
        self.clock = datetime(2026, 9, 11, tzinfo=timezone.utc)
        self.counter = 0
        self.active_task = None
        self.native = {"rewrite": [], "erratum": []}
        self.histories = {"rewrite": [], "erratum": []}
        self.values = {"rewrite": "初始偏好", "erratum": "初始偏好"}
        self.blocks = {"label": "ae_preferences", "description": "fixture", "value": "初始偏好", "limit": 10000}
        self.config = ProxyConfig("http://127.0.0.1:8000", "Qwen3-8B", 32768, 28672, 4096,
                                  2000000, 2000000, 100, 60)
        self.result = {"status": "WIRING_COMPLETED_AUDIT_PENDING", "wiring_completed": True,
                       "validity_passed": False, "input_audit_passed": None, "invalid_reasons": [],
                       "config": {"arms": ["rewrite", "erratum"], "start_turn": 4, "end_turn": 5},
                       "started_at_utc": self.tick(), "arms": {}}
        self.emit("proxy.jsonl", {"kind": "proxy_open", "config": vars(self.config)})
        self.emit("letta-http.jsonl", {"kind": "transport_open", "base_url": "http://127.0.0.1:8283"})
        for arm in ("rewrite", "erratum"):
            aid = "agent-" + arm
            payload = {"system": "共同资料:" + arm, "memory_blocks": [self.blocks],
                       "initial_message_sequence": [], "include_base_tools": False,
                       "tool_ids": [], "message_buffer_autoclear": False}
            self.http("POST", "/v1/agents/", payload, {"id": aid}, arm=None)
            self.emit("events.jsonl", {"kind": "agent_created", "arm": arm, "agent_id": aid, "payload": payload})
            self.result["arms"][arm] = {"agent_id": aid, "stages": []}
            self.http("GET", "/v1/agents/" + aid + "?include=blocks", None,
                      {"blocks": [self.blocks], "tools": []}, arm=arm)
        for turn in (4, 5):
            for arm in ("rewrite", "erratum"):
                self.stage(arm, turn)
        self.emit("letta-http.jsonl", {"kind": "transport_close"})
        self.result["finished_at_utc"] = self.tick()
        self.emit("proxy.jsonl", {"kind": "proxy_close", "blocked": False})

    def tick(self):
        self.clock += timedelta(milliseconds=1)
        return self.clock.isoformat()

    def emit(self, file, row):
        row = deepcopy(row)
        row.update(sequence=len(self.journals[file]), timestamp=self.tick())
        self.journals[file].append(row)
        return row

    def event(self, arm, event):
        return self.emit("events.jsonl", {"kind": "bridge_event", "arm": arm,
                                          "task": self.active_task, "phase": "task", "event": event})

    def model(self, body, reply, role):
        self.counter += 1
        rid = "model-" + str(self.counter)
        raw = json.dumps(body, ensure_ascii=False).encode()
        projected, reserve = tokenize_projection(body, self.config)
        self.emit("proxy.jsonl", {"kind": "client_request", "request_id": rid,
                                  "role": role, "method": "POST", "path": "/v1/chat/completions", **wire_record(raw)})
        token_raw = json.dumps(projected).encode()
        self.emit("proxy.jsonl", {"kind": "upstream_request", "request_id": rid, "method": "POST",
                                  "purpose": "preflight_tokenize", "path": "/tokenize", **wire_record(token_raw)})
        self.emit("proxy.jsonl", {"kind": "upstream_response", "request_id": rid, "http_status": 200,
                                  "purpose": "preflight_tokenize", **wire_record(json.dumps({"count": 2, "tokens": [1, 2], "max_model_len": 32768}).encode())})
        self.emit("proxy.jsonl", {"kind": "token_gate", "request_id": rid, "role": role,
                                  "passed": True, "prompt_tokens": 2, "output_reserve": reserve,
                                  "context_window": 32768, "max_prompt_tokens": 28672})
        self.emit("proxy.jsonl", {"kind": "upstream_request", "request_id": rid, "method": "POST",
                                  "purpose": "model_inference", "path": "/v1/chat/completions", **wire_record(raw)})
        self.emit("proxy.jsonl", {"kind": "upstream_response", "request_id": rid, "http_status": 200,
                                  "purpose": "model_inference", **wire_record(json.dumps(reply, ensure_ascii=False).encode())})
        self.emit("proxy.jsonl", {"kind": "model_summary", "request_id": rid, "role": role, "http_status": 200,
                                  "usage": reply["usage"], "finish_reasons": [reply["choices"][0]["finish_reason"]],
                                  "truncation_finish": False, "response_parse_error": None,
                                  "tokenize_prompt_usage_agreement": True})

    def http(self, method, path, body, reply, arm, model_body=None, model_reply=None):
        self.counter += 1
        rid = "http-" + str(self.counter)
        if arm:
            self.event(arm, {"kind": "request", "method": method, "path": path, "body": body})
        self.emit("letta-http.jsonl", {"kind": "request", "request_id": rid, "method": method, "path": path, "body": body})
        if model_body is not None:
            self.model(model_body, model_reply, "agent_or_unknown")
        self.emit("letta-http.jsonl", {"kind": "response", "request_id": rid, "http_status": 200, "body": reply})
        if arm:
            self.event(arm, {"kind": "response", "body": reply})

    def stage(self, arm, turn):
        aid = "agent-" + arm
        task = "sub_U000828_" + str(turn)
        self.active_task = task
        self.emit("events.jsonl", {"kind": "stage_start", "arm": arm, "task": task, "tools": []})
        text = "全部用户资料 " + arm + str(turn) + " [truncated 自然文本，不该误报]"
        self.histories[arm].append({"role": "user", "content": text})
        tid = "call-" + arm + str(turn)
        call = {"tool_call_id": tid, "name": "memory_update", "arguments": '{"content":"新偏好"}'}
        assistant = {"role": "assistant", "content": "", "tool_calls": [{"id": tid, "type": "function",
                      "function": {"name": "memory_update", "arguments": call["arguments"]}}]}
        model_reply = {"id": "response-" + tid, "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                       "choices": [{"message": assistant, "finish_reason": "tool_calls"}]}
        self.post(arm, [{"role": "user", "content": text}], model_reply,
                  {"messages": [{"message_type": "approval_request_message", "tool_calls": [call]}]})
        self.histories[arm].append(assistant)
        if arm == "rewrite":
            self.values[arm] = "新偏好" + str(turn)
            self.http("PATCH", "/v1/agents/" + aid + "/core-memory/blocks/ae_preferences",
                      {"value": self.values[arm]}, {"value": self.values[arm]}, arm)
        returned = {"type": "tool", "tool_call_id": tid, "tool_return": "完整回包\n " + str(turn) + " ", "status": "success"}
        self.event(arm, {"kind": "client_tool_result", "call": call, "result": returned,
                         "block_sha": hashlib.sha256(self.values[arm].encode()).hexdigest()})
        self.histories[arm].append({"role": "tool", "tool_call_id": tid,
                                   "content": json.dumps({"status": "OK", "message": returned["tool_return"], "time": "fixture"}, ensure_ascii=False)})
        final = {"role": "assistant", "content": "已处理 " + tid}
        final_reply = {"id": "final-" + tid, "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                       "choices": [{"message": final, "finish_reason": "stop"}]}
        self.post(arm, [{"type": "tool_return", "tool_returns": [returned]}], final_reply, {"messages": []})
        self.histories[arm].append(final)
        aux_req = {"model": "Qwen3-8B", "messages": [{"role": "user", "content": "judge " + tid}],
                   "max_tokens": 4096, "temperature": 0, "seed": 300}
        aux_reply = {"id": "aux-" + tid, "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                     "choices": [{"message": {"role": "assistant", "content": "fixture evaluation"}, "finish_reason": "stop"}]}
        self.model(aux_req, aux_reply, "evaluator")
        self.native[arm].append({"role": "evaluator", "subtask_id": task, "request": aux_req,
                                 "response": {"raw_data": aux_reply}, "error": None})
        stage = {"subtask_id": task, "block_text": self.values[arm]}
        self.result["arms"][arm]["stages"].append(stage)
        self.emit("events.jsonl", {"kind": "stage_complete", "arm": arm, "stage": stage,
                                   "snapshot": {"native_calls": self.native[arm]}})

    def post(self, arm, messages, model_reply, letta_reply):
        system = ("共同资料:" + arm + "\n<memory_blocks>\nThe following memory blocks are currently engaged in your core memory unit:\n\n"
                  + memory_render(self.blocks, self.values[arm]) + "\n</memory_blocks>")
        body = {"model": "Qwen3-8B", "messages": [{"role": "system", "content": system}] + deepcopy(self.histories[arm]),
                "tools": [{"type": "function", "function": TOOL}], "max_tokens": 2048}
        self.http("POST", "/v1/agents/agent-" + arm + "/messages", {"messages": messages, "client_tools": [TOOL]},
                  letta_reply, arm, body, model_reply)

    def mutate_model_body(self, rid, mutate):
        for row in self.journals["proxy.jsonl"]:
            if row.get("request_id") != rid:
                continue
            if row["kind"] == "client_request" or row["kind"] == "upstream_request":
                body = json.loads(row["body_utf8"])
                mutate(body)
                row.update(wire_record(json.dumps(body, ensure_ascii=False).encode()))

    def save(self):
        for name, entries in self.journals.items():
            (self.root / name).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in entries), encoding="utf-8")
        (self.root / "result.json").write_text(json.dumps(self.result, ensure_ascii=False), encoding="utf-8")


class InputAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = CaptureFixture(self.root)

    def audit(self):
        self.fixture.save()
        return audit_inputs(self.root, self.root / "proxy.jsonl")

    def invalid(self, code):
        report = self.audit()
        self.assertFalse(report["validity_passed"], report)
        self.assertEqual(report["invalid_reasons"][0]["code"], code)
        return report

    def calls(self):
        return [r for r in self.fixture.journals["proxy.jsonl"] if r["kind"] == "client_request" and r["role"] == "agent_or_unknown"]

    def test_complete_fixture_valid_but_not_scientific(self):
        report = self.audit()
        self.assertTrue(report["validity_passed"], report["invalid_reasons"])
        self.assertEqual(len(report["frames"]), 8)
        self.assertEqual(len(report["auxiliary_mappings"]), 4)
        self.assertIsNone(report["scientific_result"])
        before = (self.root / "result.json").read_bytes()
        audit_inputs(self.root, self.root / "proxy.jsonl")
        self.assertEqual(before, (self.root / "result.json").read_bytes())

    def test_exact_user_wrapper_allowed(self):
        rid = self.calls()[0]["request_id"]
        def wrap(body):
            text = body["messages"][1]["content"]
            body["messages"][1]["content"] = json.dumps({"type": "user_message", "message": text, "time": "fixture"})
        self.fixture.mutate_model_body(rid, wrap)
        self.assertTrue(self.audit()["validity_passed"])

    def test_user_ids_preserved_but_character_lost_invalid(self):
        self.fixture.mutate_model_body(self.calls()[0]["request_id"], lambda b: b["messages"][1].update(content=b["messages"][1]["content"][:-1]))
        self.invalid("user_content_changed")

    def test_history_drop_invalid(self):
        self.fixture.mutate_model_body(self.calls()[-1]["request_id"], lambda b: b["messages"].pop(1))
        self.invalid("history_count_changed_or_extra_message")

    def test_nested_tool_string_not_semantic_json_comparison(self):
        def change(body):
            tool = next(m for m in body["messages"] if m["role"] == "tool")
            value = json.loads(tool["content"])
            value["message"] = value["message"].rstrip()
            tool["content"] = json.dumps(value)
        self.fixture.mutate_model_body(self.calls()[1]["request_id"], change)
        self.invalid("tool_return_content_changed")

    def test_tool_wrapper_whole_string_truncation_invalid(self):
        def change(body):
            tool = next(m for m in body["messages"] if m["role"] == "tool")
            tool["content"] = tool["content"][:-4]
        self.fixture.mutate_model_body(self.calls()[1]["request_id"], change)
        self.assertFalse(self.audit()["validity_passed"])

    def test_original_block_present_elsewhere_does_not_hide_current_block_change(self):
        def change(body):
            body["messages"][0]["content"] = body["messages"][0]["content"].replace("<value>\n初始偏好", "<value>\n替换偏好") + "初始偏好"
        self.fixture.mutate_model_body(self.calls()[2]["request_id"], change)
        self.invalid("memory_block_changed_or_missing")

    def test_extra_memory_tool_invalid(self):
        self.fixture.mutate_model_body(self.calls()[0]["request_id"], lambda b: b["tools"].append({"type": "function", "function": dict(TOOL, name="memory_replace")}))
        self.invalid("actual_tool_schemas_changed")

    def test_schema_description_changed_invalid(self):
        self.fixture.mutate_model_body(self.calls()[0]["request_id"], lambda b: b["tools"][0]["function"].update(description="different"))
        self.invalid("actual_tool_schemas_changed")

    def test_model_timestamp_outside_http_window_invalid(self):
        call = self.calls()[0]
        call["timestamp"] = self.fixture.result["started_at_utc"]
        self.invalid("journal_clock_reversed")

    def test_unmatched_extra_agent_call_invalid(self):
        # Relabel one auxiliary call consistently; it still cannot match a Letta POST.
        proxy = self.fixture.journals["proxy.jsonl"]
        rid = next(r["request_id"] for r in proxy if r["kind"] == "client_request" and r["role"] == "evaluator")
        for row in proxy:
            if row.get("request_id") == rid and "role" in row:
                row["role"] = "agent_or_unknown"
        self.invalid("unaccounted_agent_model_call")

    def test_missing_summary_invalid(self):
        proxy = self.fixture.journals["proxy.jsonl"]
        proxy.remove(next(r for r in proxy if r["kind"] == "model_summary"))
        for i, row in enumerate(proxy):
            row["sequence"] = i
        self.invalid("incomplete_or_extra_model_call_events")

    def test_blocked_event_invalid(self):
        self.fixture.emit("proxy.jsonl", {"kind": "blocked", "code": "fixture"})
        self.invalid("proxy_blocked_rejected_or_incomplete_response")

    def test_usage_and_finish_gate_failures(self):
        for field, value, code in [("tokenize_prompt_usage_agreement", False, "usage_missing_or_unequal"),
                                   ("truncation_finish", True, "model_finish_not_clean"),
                                   ("http_status", 500, "model_http_not_200")]:
            with self.subTest(field=field):
                self.fixture = CaptureFixture(self.root)
                row = next(r for r in self.fixture.journals["proxy.jsonl"] if r["kind"] == "model_summary")
                row[field] = value
                self.invalid(code)

    def test_proxy_wire_digest_invalid(self):
        self.calls()[0]["body_sha256"] = "0" * 64
        self.invalid("raw_body_digest_mismatch")

    def test_execution_event_differs_from_submitted_return_invalid(self):
        row = next(r for r in self.fixture.journals["events.jsonl"] if r.get("event", {}).get("kind") == "client_tool_result")
        row["event"]["result"]["tool_return"] += "extra"
        self.invalid("tool_return_not_same_as_execution_event")

    def test_auxiliary_capture_missing_invalid(self):
        for row in self.fixture.journals["events.jsonl"]:
            if row.get("kind") == "stage_complete":
                row["snapshot"]["native_calls"] = []
        self.invalid("unaccounted_auxiliary_call_count")

    def test_incomplete_run_cannot_become_valid(self):
        self.fixture.result["wiring_completed"] = False
        report = self.invalid("wiring_not_completed_or_invalid")
        self.assertTrue(report["input_audit_passed"])

    def test_missing_capture_fails_honestly(self):
        report = audit_inputs(self.root, self.root / "missing.jsonl")
        self.assertFalse(report["validity_passed"])

    def test_cli_exclusive_and_private_output(self):
        spec = importlib.util.spec_from_file_location("audit_cli", Path(__file__).resolve().parents[1] / "scripts/ae_01_input_audit.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        self.fixture.save()
        output = self.root / "audit.json"
        args = ["--run-dir", str(self.root), "--proxy-journal", str(self.root / "proxy.jsonl"), "--output", str(output)]
        self.assertEqual(cli.main(args), 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        raw = output.read_bytes()
        self.assertEqual(cli.main(args), 2)
        self.assertEqual(output.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
