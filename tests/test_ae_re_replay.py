"""Offline tests for the read-only R/E replay tool.

Positive checks run against the REAL sealed capture
(`transfers/lab-cloud-re-watchdog-run-20260913-r1`); negative checks use a small
synthetic capture so a missing response, a corrupt line, an unattributed request or
an unsafe message body can be exercised without touching frozen data.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.ae_re_replay import parser as replay  # noqa: E402
from tools.ae_re_replay import server as replay_server  # noqa: E402

DATA_OVERRIDE = os.environ.get("AE_RE_REPLAY_DATA")
DATA_DEFAULT = ROOT / "transfers/lab-cloud-re-watchdog-run-20260913-r1"
DATA = Path(DATA_OVERRIDE) if DATA_OVERRIDE else DATA_DEFAULT
RUN_NAME = replay.RUN_DIR
OPS_NAME = replay.OPS_DIR
PROXY_NAME = replay.PROXY_FILE


def _fixture(root: Path, *, drop_response=False, corrupt_line=False, unsafe=False,
             unattributed=False, missing_file=False, expected_orders=("OT1",), patch_arm=True):
    """A minimal synthetic capture with the same file layout as the sealed one."""
    run = root / RUN_NAME
    ops = root / OPS_NAME
    run.mkdir(parents=True)
    ops.mkdir(parents=True)
    block = json.dumps({"p000": {"category": "饮食偏好", "content": "旧内容"}}, ensure_ascii=False)
    new_block = json.dumps({"p000": {"category": "饮食偏好", "content": "新内容"}}, ensure_ascii=False)
    plan = {"schema_version": "x", "task_preview": {"history_records": 26},
            "arms": {"rewrite": {"memory_blocks": [{"label": "ae_preferences", "value": block}]},
                     "erratum": {"memory_blocks": [{"label": "ae_preferences", "value": block}]}}}
    (run / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    arms = {}
    for arm in ("rewrite", "erratum"):
        arms[arm] = {"agent_id": f"agent-{arm}", "final_block_sha256": "f" * 64,
                     "termination_reason": "agent_stop", "model_posts": 2,
                     "duration_seconds": 1.0,
                     "patches": [{"value": new_block}] if (arm == "rewrite" and patch_arm) else [],
                     "memory_writes": [{"resolved": {"fact_id": "p000"}}],
                     "task_oracle": {"diagnostic_only": True},
                     "judge": {"reward_info": {"reward": 1.0}},
                     "transcript": [],
                     "native_snapshot": {"environment_db": {"orders": {
                         order_id: {"order_id": order_id, "status": "paid"}
                         for order_id in expected_orders}}}}
    result = {"schema_version": "x", "status": "RE_PAIR_COMPLETED_AUDIT_PENDING",
              "scientific_result": None, "started_at_utc": "t0", "finished_at_utc": "t1",
              "arms": arms}
    (run / "result.json").write_text(json.dumps(result), encoding="utf-8")
    events = [
        {"kind": "agent_created", "arm": "rewrite", "sequence": 1, "timestamp": "t1",
         "payload": {"memory_blocks": [{"label": "ae_preferences", "value": block}]}},
        {"kind": "agent_created", "arm": "erratum", "sequence": 2, "timestamp": "t2",
         "payload": {"memory_blocks": [{"label": "ae_preferences", "value": block}]}},
        {"kind": "bridge_event", "arm": "rewrite", "sequence": 3, "timestamp": "t3",
         "event": {"kind": "client_tool_result",
                   "call": {"tool_call_id": "m1", "name": "memory_update",
                            "arguments": json.dumps({"operation": "replace"})},
                   "result": {"status": "success",
                              "tool_return": json.dumps({"erratum": "[STATE UPDATE]\n新内容"})},
                   "block_sha": "b" * 64}},
        {"kind": "bridge_event", "arm": "rewrite", "sequence": 4, "timestamp": "t4",
         "event": {"kind": "request", "method": "PATCH", "path": "/blocks/ae_preferences",
                   "body": {"value": new_block}}},
        {"kind": "bridge_event", "arm": "rewrite", "sequence": 5, "timestamp": "t5",
         "event": {"kind": "client_tool_result",
                   "call": {"tool_call_id": "s1", "name": "delivery_product_search_recommand",
                            "arguments": "{}"},
                   "result": {"status": "success", "tool_return": "搜索回包正文"},
                   "block_sha": "b" * 64}},
        {"kind": "bridge_event", "arm": "rewrite", "sequence": 6, "timestamp": "t6",
         "event": {"kind": "client_tool_result",
                   "call": {"tool_call_id": "c1", "name": "create_delivery_order",
                            "arguments": "{}"},
                   "result": {"status": "success",
                              "tool_return": f"Order(order_id:{expected_orders[0]}, status:unpaid)"},
                   "block_sha": "b" * 64}},
        {"kind": "bridge_event", "arm": "rewrite", "sequence": 7, "timestamp": "t7",
         "event": {"kind": "client_tool_result",
                   "call": {"tool_call_id": "p1", "name": "pay_delivery_order",
                            "arguments": json.dumps({"order_id": expected_orders[0]})},
                   "result": {"status": "success", "tool_return": "Payment successful"},
                   "block_sha": "b" * 64}},
        {"kind": "simulated_user", "arm": "rewrite", "sequence": 8, "timestamp": "t8",
         "reply": {"content": "确认支付"}},
    ]
    (run / "events.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in events) + "\n", encoding="utf-8")
    (run / "letta-http.jsonl").write_text("", encoding="utf-8")
    request_body = {"model": "m", "messages": [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "<script>alert(1)</script>" if unsafe else "历史与任务"}]}
    reply = {"choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": "回复"}}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    rows = [
        {"kind": "client_request", "request_id": "r1", "method": "GET", "path": "/v1/models",
         "sequence": 1, "timestamp": "t1", "role": "agent_or_unknown"},
        {"kind": "client_request", "request_id": "r2", "method": "POST",
         "path": "/v1/chat/completions", "sequence": 2, "timestamp": "t2", "role": "agent_or_unknown"},
        {"kind": "normalized_request", "request_id": "r2", "sequence": 3, "timestamp": "t3",
         "body_utf8": json.dumps(request_body, ensure_ascii=False)},
    ]
    if not drop_response:
        rows.append({"kind": "upstream_response", "request_id": "r2", "sequence": 4,
                     "timestamp": "t4", "complete_body": True,
                     "body_utf8": json.dumps(reply, ensure_ascii=False)})
    text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    if corrupt_line:
        text += "\n{not json"
    text += "\n"
    (root / PROXY_NAME).write_text(text, encoding="utf-8")
    audit = {"status": "VALID", "scientific_result": None, "input_audit_passed": True,
             "counts": {"agent_posts": 1}, "scope": {},
             "arms": {arm: {"initial_block_sha256": hashlib.sha256(block.encode()).hexdigest(),
                            "final_block_sha256": "f" * 64} for arm in ("rewrite", "erratum")},
             "frames": ([] if unattributed else [{"kind": "agent_frame", "arm": "rewrite",
                                                  "phase": "history", "request_id": "r2"}]),
             "auxiliary_mappings": [], "limitations": []}
    (ops / "input-audit.json").write_text(json.dumps(audit), encoding="utf-8")
    (ops / "watchdog.json").write_text(json.dumps({"monitor": {"status": "RUNNER_EXITED_NO_TRIGGER"},
                                                   "trigger": None, "watchdog": {}}), encoding="utf-8")
    if missing_file:
        (run / "result.json").unlink()
    return root


class RealCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if DATA_OVERRIDE and not DATA.is_dir():
            raise RuntimeError(f"AE_RE_REPLAY_DATA is set but missing: {DATA}")
        if not DATA.is_dir():
            raise RuntimeError(f"sealed capture is missing: {DATA}")
        cls.model = replay.build_replay(DATA)

    def test_source_sha_matches_the_manifests(self):
        for entry in self.model["sources"]:
            digest = hashlib.sha256((DATA / entry["path"]).read_bytes()).hexdigest()
            self.assertEqual(digest, entry["sha256"], entry["path"])
            self.assertIn(entry["sha256"], (entry["local_manifest_sha256"],
                                            entry["remote_manifest_sha256"]), entry["path"])

    def test_33_model_requests_attributed_without_double_counting(self):
        self.assertEqual(self.model["meta"]["model_requests"], 33)
        self.assertEqual(self.model["meta"]["chat_roles"],
                         {"agent_request": 22, "user_simulator": 5, "evaluator": 6})
        self.assertEqual(len(self.model["meta"]["non_chat_calls"]), 1)
        self.assertIn("not a chat request", self.model["meta"]["non_chat_calls"][0]["note"])
        self.assertEqual(self.model["unassociated"], [])
        for item in self.model["requests"]:
            self.assertIsNotNone(item["audit"])
            self.assertIn(item["arm"], ("rewrite", "erratum"))
            self.assertIn(item["kind"], ("agent_request", "user_simulator", "evaluator"))
        self.assertEqual(self.model["flow"]["counts"]["agent_requests"], 22)
        self.assertEqual(self.model["flow"]["counts"]["tool_results"], 14)
        self.assertEqual(self.model["flow"]["counts"]["patches"], 1)

    def test_provenance_lines_resolve_to_the_same_record(self):
        wire = (DATA / PROXY_NAME).read_text(encoding="utf-8").splitlines()
        for item in self.model["requests"][:6]:
            line = item["source"]["normalized_request_line"]
            row = json.loads(wire[line - 1])
            self.assertEqual(row["request_id"], item["request_id"])
            body = json.loads(row["body_utf8"])
            # the replay model tags every message with safety flags; compare the record itself
            self.assertEqual(body["messages"], [
                {key: value for key, value in message.items()
                 if key not in ("unsafe_content", "markup_like")}
                for message in item["messages"]])
            self.assertTrue(all("unsafe_content" in message for message in item["messages"]))
            response_line = item["source"]["upstream_response_line"]
            self.assertEqual(json.loads(wire[response_line - 1])["request_id"], item["request_id"])

    def test_responses_are_linked_by_identifier_not_similarity(self):
        executed = {}
        for number, line in enumerate(
                (DATA / RUN_NAME / "events.jsonl").read_text(encoding="utf-8").splitlines(), 1):
            row = json.loads(line)
            event = row.get("event") or {}
            if event.get("kind") == "client_tool_result":
                executed[(row.get("arm"), (event.get("call") or {}).get("tool_call_id"))] = \
                    (event.get("result") or {}).get("tool_return")
        matched = 0
        for item in self.model["requests"]:
            if item["kind"] != "agent_request":
                continue
            for call in item["response"]["tool_calls"]:
                key = (item["arm"], call["id"])
                if key in executed:
                    matched += 1
        self.assertEqual(matched, 14, "every executed tool call must be tied to a model response id")

    def test_memory_update_patch_and_appended_correction(self):
        rewrite, erratum = self.model["arms"]["rewrite"], self.model["arms"]["erratum"]
        self.assertTrue(rewrite["memory"]["patch_applied"])
        self.assertEqual(rewrite["result"]["patches"], 1)
        self.assertTrue(rewrite["memory"]["initial_block"])
        self.assertNotEqual(rewrite["memory"]["initial_block"], rewrite["memory"]["final_block"])
        self.assertEqual(rewrite["memory"]["initial_sha256"],
                         rewrite["memory"]["audit_initial_sha256"])
        self.assertEqual(rewrite["memory"]["final_sha256"],
                         rewrite["memory"]["audit_final_sha256"])
        patch = [step for step in rewrite["steps"] if step["kind"] == "patch_block"]
        self.assertEqual(len(patch), 1)
        facts = {change["fact_id"]: change for change in patch[0]["payload"]["changed_facts"]}
        self.assertEqual(sorted(facts), ["p007"])
        self.assertIn("7分糖", facts["p007"]["after"]["content"])
        self.assertFalse(erratum["memory"]["patch_applied"])
        self.assertEqual(erratum["result"]["patches"], 0)
        self.assertEqual(erratum["memory"]["initial_block"], erratum["memory"]["final_block"])
        self.assertIn("[STATE UPDATE]", erratum["memory"]["appended_correction_text"])
        self.assertIn("7分糖", erratum["memory"]["appended_correction_text"])

    def test_orders_are_real_paid_and_state_does_not_leak_backwards(self):
        for arm, expected in (("rewrite", "OT9edba81d08"), ("erratum", "OT29e4aa93ec")):
            record = self.model["arms"][arm]
            self.assertIn(expected, record["orders"])
            self.assertEqual(record["orders"][expected]["status"], "paid")
            steps = record["steps"]
            create = next(step for step in steps if step["kind"] == "create_order")
            pay = next(step for step in steps if step["kind"] == "pay_order")
            create_index = steps.index(create)
            pay_index = steps.index(pay)
            self.assertLess(create_index, pay_index)
            for step in steps[:create_index]:
                self.assertEqual(step["state"]["orders"], {}, step["id"])
            for step in steps[create_index + 1:pay_index]:
                self.assertEqual(step["state"]["orders"][expected]["status"], "unpaid", step["id"])
            for step in steps[pay_index:]:
                self.assertEqual(step["state"]["orders"][expected]["status"], "paid", step["id"])
            patched = [index for index, step in enumerate(steps) if step["kind"] == "patch_block"]
            for index, step in enumerate(steps):
                self.assertTrue(step["state"]["memory_sha256"])
                if index < (patched[0] if patched else 10**9):
                    self.assertEqual(step["state"]["memory_sha256"],
                                     record["memory"]["initial_sha256"], step["id"])

    def test_first_task_request_shows_history_and_the_update_round_trip(self):
        for arm in ("rewrite", "erratum"):
            record = self.model["arms"][arm]
            first = next(step for step in record["steps"] if step["kind"] == "agent_request"
                         and step["payload"].get("phase") == "task")
            self.assertEqual(first["payload"]["history_records"], 26)
            request = self.model["requests"][first["request_index"] - 1]
            joined = json.dumps(request["messages"], ensure_ascii=False)
            self.assertIn("memory_update", joined)
            update_step = next(step for step in record["steps"] if step["kind"] == "memory_update")
            self.assertLess(record["steps"].index(update_step), record["steps"].index(first))
            history_reply = [step for step in record["steps"] if step["kind"] == "agent_request"
                             and step["payload"].get("phase") == "history"]
            self.assertEqual(len(history_reply), 2)

    def test_the_request_after_the_search_return_carries_the_real_return(self):
        record = self.model["arms"]["rewrite"]
        steps = record["steps"]
        search = steps[steps.index(next(step for step in steps if step["kind"] == "tool_result"
                                        and step["payload"].get("name")
                                        == "delivery_product_search_recommand"))]
        after = next(step for step in steps[steps.index(search) + 1:]
                     if step["kind"] == "agent_request")
        request = self.model["requests"][after["request_index"] - 1]
        returned = []
        for message in request["messages"]:
            if message.get("role") != "tool":
                continue
            try:
                envelope = json.loads(message["content"])
            except ValueError:
                continue
            returned.append(envelope.get("message") if isinstance(envelope, dict) else None)
        self.assertIn(search["payload"]["tool_return"], returned,
                      "the real search return must reach the next model input verbatim")
        self.assertEqual(record["jumps"]["search"], search["id"])
        self.assertEqual(steps[steps.index(search) + 1]["id"], after["id"],
                         "the request fed by the search return is the step right after it")

    def test_no_unsafe_content_in_the_real_capture(self):
        unsafe = [item["index"] for item in self.model["requests"]
                  if any(message.get("unsafe_content") for message in item["messages"])]
        self.assertEqual(unsafe, [])

    def test_audit_and_original_result_status_stay_separate(self):
        meta = self.model["meta"]
        self.assertEqual(meta["audit_status"], "VALID")
        self.assertEqual(meta["input_audit_passed"], True)
        self.assertEqual(meta["result_status"], "RE_PAIR_COMPLETED_AUDIT_PENDING")
        self.assertIsNone(meta["result_scientific_result"])
        self.assertIsNone(meta["audit_scientific_result"])
        self.assertEqual(meta["history_records"], 26)


class NegativeFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-re-replay-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_missing_response_is_reported_not_invented(self):
        _fixture(self.dir / "missing", drop_response=True)
        model = replay.build_replay(self.dir / "missing")
        self.assertEqual(model["requests"], [])
        self.assertEqual(len(model["unassociated"]), 1)
        self.assertEqual(model["unassociated"][0]["reason"], "missing upstream response")
        self.assertTrue(any("unassociated proxy request" in warning for warning in model["warnings"]))

    def test_a_corrupt_line_refuses_the_replay(self):
        _fixture(self.dir / "corrupt", corrupt_line=True)
        with self.assertRaises(replay.ReplayDataError) as caught:
            replay.build_replay(self.dir / "corrupt")
        self.assertIn("not valid JSON", str(caught.exception))

    def test_an_unsafe_message_body_is_flagged_not_executed(self):
        _fixture(self.dir / "unsafe", unsafe=True)
        model = replay.build_replay(self.dir / "unsafe")
        message = model["requests"][0]["messages"][1]
        self.assertTrue(message["unsafe_content"])
        self.assertIn("<script>", message["content"])

    def test_an_unattributed_request_is_reported(self):
        _fixture(self.dir / "unattributed", unattributed=True)
        model = replay.build_replay(self.dir / "unattributed")
        self.assertEqual(model["requests"][0]["kind"], "unattributed")
        self.assertIsNone(model["requests"][0]["arm"])
        self.assertTrue(any("no audit attribution" in warning for warning in model["warnings"]))

    def test_an_incomplete_capture_refuses_the_replay(self):
        _fixture(self.dir / "incomplete", missing_file=True)
        with self.assertRaises(replay.ReplayDataError) as caught:
            replay.build_replay(self.dir / "incomplete")
        self.assertIn("incomplete", str(caught.exception))

    def test_synthetic_state_is_derived_per_step(self):
        _fixture(self.dir / "ok")
        model = replay.build_replay(self.dir / "ok")
        steps = model["arms"]["rewrite"]["steps"]
        with_order = [index for index, step in enumerate(steps) if step["state"]["orders"]]
        self.assertTrue(with_order)
        self.assertEqual(steps[with_order[0]]["kind"], "create_order")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-re-replay-server-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        _fixture(self.dir / "data")
        self.server, self.model = replay_server.create_server(self.dir / "data", "127.0.0.1", 0)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
                return response.status, response.headers.get("Content-Type"), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type"), error.read()

    def test_the_model_endpoint_serves_the_replay_json(self):
        status, content_type, body = self.get("/api/model")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        model = json.loads(body.decode("utf-8"))
        self.assertEqual(model["schema"], replay.SCHEMA)
        self.assertEqual(model["notice"], replay.NOTICE)

    def test_static_assets_and_health_are_served(self):
        for path, needle in (("/", "真实记录回放"), ("/app.js", "renderFlow"),
                             ("/style.css", ".timeline")):
            status, content_type, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(needle, body.decode("utf-8"))
        status, _type, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode("utf-8"))["ok"])

    def test_the_page_renders_captured_text_without_executing_it(self):
        _status, _type, body = self.get("/app.js")
        source = body.decode("utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, source, f"{sink} would let captured text execute")
        self.assertIn("textContent", source)
        # a bad deep link must be reported, not silently padded with another record
        self.assertIn("resolveTarget", source)
        self.assertIn("未替记录补齐任何事件", source)

    def test_only_whitelisted_paths_are_served(self):
        for path in ("/nope", "/../etc/passwd", "/..%2f..%2fetc%2fpasswd",
                     f"/{RUN_NAME}/result.json", "/static/", "/api/model/../model"):
            status, _type, _body = self.get(path)
            self.assertEqual(status, 404, path)

    def test_a_non_loopback_bind_is_refused(self):
        with self.assertRaises(ValueError):
            replay_server.create_server(self.dir / "data", "0.0.0.0", 0)

    def test_no_hardcoded_absolute_data_path_in_the_tool(self):
        for path in sorted((ROOT / "tools/ae_re_replay").rglob("*")):
            if path.is_file() and path.suffix in (".py", ".js", ".html", ".css"):
                self.assertNotIn("/Users/", path.read_text(encoding="utf-8"), path.name)

    def test_the_data_directory_is_read_only_used(self):
        before = {path.relative_to(self.dir / "data"): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (self.dir / "data").rglob("*") if path.is_file()}
        self.get("/api/model")
        after = {path.relative_to(self.dir / "data"): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (self.dir / "data").rglob("*") if path.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
