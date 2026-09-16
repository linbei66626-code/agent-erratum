"""Offline tests for the sealed-order confirmation continuation probe.

FIXTURES ONLY: no model, no network. The probe under test is the production
`scripts/ae_01_native_order_confirm_probe.py`; tests use the REAL sealed run files as
the source, the real fixed Vita classes (restored from the sealed snapshot), the real
trusted bindings and the real CloudAuditProxy, with a scripted HTTP opener whose
reply is decided from the real request history.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

_spec = importlib.util.spec_from_file_location(
    "ae_native_order_confirm_probe", ROOT / "scripts/ae_01_native_order_confirm_probe.py")
confirm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(confirm)

from ae_cloud_proxy import MODEL  # noqa: E402
from test_ae_tool_errors import _VitaFixture  # noqa: E402

SOURCE_OVERRIDE = os.environ.get("AE_NATIVE_ORDER_SOURCE")
SOURCE_DEFAULT = (ROOT / "transfers/ae-native-order-probe-live-20260913-r1"
                         / "ae-native-order-probe-20260913-codex-r1-run")
SOURCE = Path(SOURCE_OVERRIDE) if SOURCE_OVERRIDE else SOURCE_DEFAULT
TRANSPORT_CONFIG = (ROOT / "configs"
                    / "ae-01__cloud-transport__siliconflow.capability-pacing-candidate.json")
KEY = "sk-offline-fixture-not-real-0001"
SEALED_ORDER = "OTb95a8ee52a"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)


class Reply:
    def __init__(self, body, status=200):
        self._raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.code = status
        self.headers = {"x-siliconcloud-trace-id": "offline-fixture"}

    def read(self, _limit=None):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def assistant(content=None, calls=None, finish="tool_calls"):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"model": MODEL, "id": "fixture",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


def tool_call(call_id, name, arguments):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class ScriptedOpener:
    def __init__(self, decide):
        self.decide = decide
        self.bodies = []

    def open(self, request, timeout):
        del timeout
        body = json.loads((request.data or b"{}").decode("utf-8"))
        self.bodies.append(body)
        return Reply(self.decide(len(self.bodies), body))


class _ConfirmFixture(_VitaFixture):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-confirm-probe-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self._runs = 0

    def case(self, source=None):
        sources = confirm.load_sources(source or SOURCE)
        return confirm.build_case(sources, Path(os.environ.get(
            "AE_VITA_SOURCE", "/tmp/ae01-vita-8WHDvk/source")))

    def run_scripted(self, decide, source=None):
        case = self.case(source)
        opener = ScriptedOpener(decide)
        self._runs += 1
        clock = FakeClock()
        journal = self.dir / f"chat-wire-{self._runs}.private.jsonl"
        result = confirm.execute_confirm(
            case, TRANSPORT_CONFIG, request_output_tokens=2048, temperature=0.0,
            api_key=KEY, journal_path=journal, opener=opener,
            clock=clock.monotonic, sleep=clock.sleep)
        return result, opener, case

    def source_copy(self):
        """A writable copy of the sealed run (same directory name) plus its manifest."""
        target = self.dir / "sealed-copy" / SOURCE.name
        target.parent.mkdir()
        shutil.copytree(SOURCE, target)
        manifest = self.dir / "sealed-copy" / "remote-SHA256SUMS"
        manifest.write_text(
            (SOURCE.parent / "remote-SHA256SUMS").read_text(encoding="utf-8"),
            encoding="utf-8")
        return target, manifest

    @staticmethod
    def pay_sealed(history):
        for message in history:
            if message.get("role") == "tool" and SEALED_ORDER in (message.get("content") or ""):
                return SEALED_ORDER
        raise AssertionError("the sealed order id is not in the input history")


class SourceContractTests(_ConfirmFixture):
    def test_the_sealed_source_is_verified_and_restored_exactly(self):
        sources = confirm.load_sources(SOURCE)
        self.assertEqual(sorted(sources["sha256"]), sorted(
            f"{SOURCE.name}/{name}" for name in confirm.SOURCE_FILES))
        sealed = confirm._sealed_contract(sources)
        self.assertEqual(sealed["order_id"], SEALED_ORDER)
        self.assertEqual(sealed["order"]["status"], "unpaid")
        case = self.case()
        self.assertEqual(confirm.ORDER_PROBE.case_snapshot(case["environment"]),
                         sealed["snapshot"])
        self.assertEqual([message["role"] for message in case["messages"]],
                         ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(case["messages"][-1]["content"], confirm.CONFIRM_TEXT)
        self.assertNotIn(SEALED_ORDER, confirm.CONFIRM_TEXT)
        self.assertEqual(case["messages"][4]["content"], sealed["assistant_reply"]["content"])
        self.assertEqual(case["messages"][3]["content"], sealed["create_return"])

    def test_a_tampered_source_file_is_refused(self):
        target, manifest = self.source_copy()
        path = target / "result.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["status"] = "NATIVE_ORDER_PROBE_PASSED"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            confirm.load_sources(target, manifest)
        self.assertIn("SHA mismatch", str(caught.exception))

    def test_a_consistent_hash_but_wrong_state_is_refused(self):
        target, manifest = self.source_copy()
        path = target / "result.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        order = next(iter(record["final_native_snapshot"]["orders"].values()))
        order["status"] = "paid"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        import hashlib
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows = manifest.read_text(encoding="utf-8").splitlines()
        manifest.write_text("\n".join(
            f"{digest}  {row.split(maxsplit=1)[1]}" if row.endswith("result.json") else row
            for row in rows) + "\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            confirm._sealed_contract(confirm.load_sources(target, manifest))
        self.assertIn("not unpaid", str(caught.exception))

    def test_a_mismatched_assistant_reply_is_refused(self):
        target, manifest = self.source_copy()
        wire = target / "chat-wire.private.jsonl"
        rows = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines() if line.strip()]
        responses = [row for row in rows if row.get("kind") == "upstream_response"]
        body = json.loads(responses[1]["body_utf8"])
        body["choices"][0]["message"]["content"] = "tampered confirmation text"
        responses[1]["body_utf8"] = json.dumps(body, ensure_ascii=False)
        responses[1]["body_base64"] = None
        wire.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                        encoding="utf-8")
        import hashlib
        digest = hashlib.sha256(wire.read_bytes()).hexdigest()
        rows_text = manifest.read_text(encoding="utf-8").splitlines()
        manifest.write_text("\n".join(
            f"{digest}  {row.split(maxsplit=1)[1]}" if row.endswith("chat-wire.private.jsonl") else row
            for row in rows_text) + "\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            confirm._sealed_contract(confirm.load_sources(target, manifest))
        self.assertIn("assistant reply does not match", str(caught.exception))

    def test_a_missing_source_directory_is_a_hard_error(self):
        with self.assertRaises(RuntimeError) as caught:
            confirm.load_sources(self.dir / "no-such-run")
        self.assertIn("sealed source directory is missing", str(caught.exception))

    def test_the_plan_is_offline_and_freezes_the_continuation(self):
        out = self.dir / "plan-run"
        with patch.object(socket.socket, "connect",
                          side_effect=AssertionError("PLAN must not touch the network")):
            code = confirm.main(["--stage", "plan", "--source-dir", str(SOURCE),
                                 "--vita-source", os.environ.get("AE_VITA_SOURCE",
                                                                 "/tmp/ae01-vita-8WHDvk/source"),
                                 "--transport-config", str(TRANSPORT_CONFIG),
                                 "--output-dir", str(out)])
        self.assertEqual(code, 0)
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], confirm.STATUS_PLAN)
        self.assertTrue(plan["restored_from_snapshot"])
        self.assertTrue(plan["new_user_confirmation"])
        self.assertEqual(plan["appended_user_message"], confirm.CONFIRM_TEXT)
        self.assertEqual(plan["budget"]["max_requests"], 1)
        self.assertEqual(plan["sealed_state"]["order_id"], SEALED_ORDER)
        self.assertEqual(plan["sealed_state"]["order"]["status"], "unpaid")
        self.assertEqual(len(plan["sealed_source"]["sha256"]), len(confirm.SOURCE_FILES))
        self.assertEqual([message["role"] for message in plan["request"]["messages"]],
                         ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertIsNone(plan["scientific_result"])
        self.assertNotIn(SEALED_ORDER, plan["request"]["messages"][-1]["content"])

    def test_refusing_an_existing_directory_writes_nothing(self):
        out = self.dir / "taken"
        out.mkdir()
        (out / "keep.txt").write_text("keep", encoding="utf-8")
        (out / "result.json").write_text('{"old": true}\n', encoding="utf-8")
        before = {entry.name: entry.read_bytes() for entry in sorted(out.iterdir())}
        with contextlib.redirect_stderr(io.StringIO()):
            code = confirm.main(["--stage", "plan", "--source-dir", str(SOURCE),
                                 "--transport-config", str(TRANSPORT_CONFIG),
                                 "--output-dir", str(out)])
        self.assertEqual(code, 2)
        self.assertEqual({entry.name: entry.read_bytes() for entry in sorted(out.iterdir())},
                         before)

    def test_run_requires_an_explicit_key_file(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                confirm.main(["--stage", "run", "--source-dir", str(SOURCE),
                              "--transport-config", str(TRANSPORT_CONFIG),
                              "--output-dir", str(self.dir / "no-key")])
        self.assertFalse((self.dir / "no-key").exists())

    def test_a_declared_missing_vita_source_fails_hard(self):
        with self.assertRaises(RuntimeError) as caught:
            confirm.build_case(confirm.load_sources(SOURCE), self.dir / "no-vita")
        self.assertIn("fixed Vita source is missing", str(caught.exception))


class ContinuationChainTests(_ConfirmFixture):
    def test_the_sealed_order_is_paid_after_the_confirmation(self):
        def decide(_index, body):
            order_id = self.pay_sealed(body["messages"])
            return assistant(calls=[tool_call("c2", "pay_delivery_order",
                                              {"order_id": order_id})])

        result, opener, _case = self.run_scripted(decide)
        self.assertEqual(result["status"], confirm.STATUS_PASSED)
        self.assertTrue(result["validity_passed"])
        self.assertTrue(result["model_task_completed"])
        self.assertIs(result["task_success"], True)
        self.assertIsNone(result["scientific_result"])
        self.assertEqual(result["requests_sent"], 1)
        self.assertEqual(len(opener.bodies), 1)
        self.assertEqual(result["stop_reason"], "sealed_order_paid_after_user_confirmation")
        self.assertTrue(result["payment_source_verified"])
        self.assertEqual(result["paid_order_id"], SEALED_ORDER)
        self.assertEqual(result["orders_before"], 1)
        self.assertEqual(result["orders_after"], 1)
        self.assertEqual(result["new_order_ids"], [])
        self.assertLessEqual(set(result["order_changed_fields"]), {"status", "update_time"})
        order = result["final_native_snapshot"]["orders"][SEALED_ORDER]
        self.assertEqual(order["status"], "paid")
        self.assertEqual(order["attributes"] if "attributes" in order else None, None)
        self.assertEqual(order["products"][0]["attributes"], "7分糖")
        self.assertEqual(result["redundant_actions"], 0)
        self.assertTrue(result["restored_from_snapshot"])
        self.assertTrue(result["new_user_confirmation"])
        self.assertTrue(result["fixture_used"])

    def test_the_request_history_is_verbatim_and_the_new_message_adds_no_id(self):
        captured = {}

        def decide(_index, body):
            captured["messages"] = body["messages"]
            captured["body"] = body
            return assistant(calls=[tool_call("c2", "pay_delivery_order",
                                              {"order_id": SEALED_ORDER})])

        _result, _opener, case = self.run_scripted(decide)
        sealed_messages = case["sealed"]["history"]
        self.assertEqual(captured["messages"][:4], sealed_messages)
        self.assertEqual(captured["messages"][4]["content"],
                         case["sealed"]["assistant_reply"]["content"])
        self.assertEqual(captured["messages"][5], {"role": "user",
                                                   "content": confirm.CONFIRM_TEXT})
        self.assertNotIn(SEALED_ORDER, captured["messages"][5]["content"])
        self.assertEqual(captured["messages"][0], sealed_messages[0])
        self.assertEqual(captured["body"]["model"], MODEL)
        self.assertEqual(captured["body"]["tool_choice"], "auto")
        self.assertIs(captured["body"]["parallel_tool_calls"], False)
        self.assertEqual(sorted(item["function"]["name"] for item in captured["body"]["tools"]),
                         sorted(confirm.TOOLS))
        self.assertNotIn("seed", captured["body"])

    def test_a_fabricated_id_is_recorded_and_never_substituted(self):
        def decide(_index, _body):
            return assistant(calls=[tool_call("c2", "pay_delivery_order",
                                              {"order_id": "O-INVENTED-0001"})])

        result, _opener, _case = self.run_scripted(decide)
        record = result["steps"][0]["tool_calls"][0]
        self.assertEqual(record["order_id"], "O-INVENTED-0001")
        self.assertFalse(record["order_id_is_the_sealed_order"])
        self.assertEqual(record["status"], "error")
        self.assertTrue(record["dispatched"])
        self.assertEqual(record["tool_return"], "Error: Order O-INVENTED-0001 not found")
        self.assertNotEqual(result["status"], confirm.STATUS_PASSED)
        self.assertIs(result["task_success"], False)
        self.assertEqual(result["final_native_snapshot"]["orders"][SEALED_ORDER]["status"],
                         "unpaid")

    def test_no_tool_call_is_not_success(self):
        def decide(_index, _body):
            return assistant(content="好的，已为你支付。", finish="stop")

        result, _opener, _case = self.run_scripted(decide)
        self.assertEqual(result["status"], confirm.STATUS_CLAIMED)
        self.assertIs(result["task_success"], False)
        self.assertEqual(result["final_native_snapshot"]["orders"][SEALED_ORDER]["status"],
                         "unpaid")
        self.assertEqual(result["steps"][0]["assistant_text"], "好的，已为你支付。")

    def test_another_create_does_not_pass(self):
        def decide(_index, body):
            order_id = self.pay_sealed(body["messages"])
            return assistant(calls=[
                tool_call("c2", "pay_delivery_order", {"order_id": order_id}),
                tool_call("c3", "create_delivery_order",
                          {"user_id": "U1", "store_id": "S1", "product_ids": ["P1"],
                           "product_cnts": [1], "address": "Addr",
                           "dispatch_time": "2024-06-23 15:00:00",
                           "attributes": ["规格: 7分糖"]})])

        result, _opener, _case = self.run_scripted(decide)
        self.assertNotEqual(result["status"], confirm.STATUS_PASSED)
        self.assertEqual(result["stop_reason"], "new_order_created")
        self.assertEqual(result["orders_after"], 2)
        self.assertEqual(len(result["new_order_ids"]), 1)
        self.assertIs(result["task_success"], False)

    def test_an_undeclared_tool_is_recorded_and_not_a_clean_pass(self):
        def decide(_index, body):
            order_id = self.pay_sealed(body["messages"])
            return assistant(calls=[
                tool_call("c2", "pay_delivery_order", {"order_id": order_id}),
                tool_call("c3", "get_user_all_orders", {})])

        result, _opener, _case = self.run_scripted(decide)
        self.assertNotEqual(result["status"], confirm.STATUS_PASSED)
        self.assertEqual(result["stop_reason"], "batch_protocol_violation")
        record = result["steps"][0]["tool_calls"][1]
        self.assertEqual(record["kind"], "undeclared_tool")
        self.assertFalse(record["dispatched"])
        self.assertEqual(result["final_native_snapshot"]["orders"][SEALED_ORDER]["status"], "paid")

    def test_a_redundant_repeat_payment_passes_with_redundancy_recorded(self):
        def decide(_index, body):
            order_id = self.pay_sealed(body["messages"])
            return assistant(calls=[
                tool_call("c2", "pay_delivery_order", {"order_id": order_id}),
                tool_call("c3", "pay_delivery_order", {"order_id": order_id})])

        result, _opener, _case = self.run_scripted(decide)
        self.assertEqual(result["status"], confirm.STATUS_PASSED)
        self.assertEqual(result["redundant_actions"], 1)
        self.assertEqual(len(result["final_native_snapshot"]["orders"]), 1)
        self.assertEqual(result["final_native_snapshot"]["orders"][SEALED_ORDER]["status"], "paid")

    def test_an_uncertain_outcome_stops_without_replay(self):
        def decide(_index, _body):
            return assistant(calls=[tool_call("c2", "pay_delivery_order",
                                              {"order_id": SEALED_ORDER})])

        case = self.case()

        class ExplodingOrders(dict):
            def __contains__(self, key):
                raise RuntimeError("fixture write failure before the lookup raises")

        case["environment"].tools.db.orders = ExplodingOrders(
            case["environment"].tools.db.orders)
        opener = ScriptedOpener(decide)
        clock = FakeClock()
        result = confirm.execute_confirm(
            case, TRANSPORT_CONFIG, request_output_tokens=2048, temperature=0.0,
            api_key=KEY, journal_path=self.dir / "uncertain.jsonl", opener=opener,
            clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(result["status"], confirm.STATUS_UNCERTAIN)
        self.assertEqual(result["requests_sent"], 1)
        self.assertEqual(len(opener.bodies), 1)
        self.assertEqual(result["errors"][0]["type"], "uncertain_tool_outcome")
        self.assertFalse(result["validity_passed"])
        self.assertIn("final_native_snapshot", result)

    def test_exactly_one_request_is_sent_and_never_retried(self):
        def decide(index, _body):
            return assistant(calls=[tool_call(f"c{index}", "pay_delivery_order",
                                              {"order_id": f"O-INVENTED-{index}"})])

        result, opener, _case = self.run_scripted(decide)
        self.assertEqual(result["requests_sent"], 1)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(len(opener.bodies), 1)
        self.assertEqual(result["status"], confirm.STATUS_INCOMPLETE)
        self.assertEqual(result["stop_reason"], "no_successful_payment_in_the_batch")
        self.assertIs(result["task_success"], False)


if __name__ == "__main__":
    unittest.main()
