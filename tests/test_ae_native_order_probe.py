"""Offline tests for the native create->pay probe. FIXTURES ONLY: no model, no network.

The probe code under test is the production `scripts/ae_01_native_order_probe.py`.
Tests use the real fixed Vita classes, the real trusted `environment_bindings`, real
tool execution/serialization/argument checks and the real `CloudAuditProxy`; only
the HTTP opener is a scripted fixture, which is exactly what the offline acceptance
requires. Scripted replies are decided from the REAL request bodies, so the order id
the model "pays" comes from the actual create return -- but this is a fixture, never
a capability result.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

_spec = importlib.util.spec_from_file_location(
    "ae_native_order_probe", ROOT / "scripts/ae_01_native_order_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

from ae_cloud_proxy import MODEL  # noqa: E402
from test_ae_tool_errors import _VitaFixture  # noqa: E402

VITA_SOURCE_OVERRIDE = os.environ.get("AE_VITA_SOURCE")
VITA_SOURCE = (Path(VITA_SOURCE_OVERRIDE) if VITA_SOURCE_OVERRIDE
               else Path("/tmp/ae01-vita-8WHDvk/source"))
TRANSPORT_CONFIG = (ROOT / "configs"
                    / "ae-01__cloud-transport__siliconflow.capability-pacing-candidate.json")
KEY = "sk-offline-fixture-not-real-0001"
CREATE_ARGS = {"user_id": "U1", "store_id": "S1", "product_ids": ["P1"], "product_cnts": [1],
               "address": "Addr", "dispatch_time": "2024-06-23 15:00:00",
               "attributes": ["规格: 7分糖"]}


class FakeClock:
    """Deterministic monotonic clock so the frozen 65 s pacing never really sleeps."""

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
    """Offline transport fixture: replies are decided from the REAL request bodies."""

    def __init__(self, decide):
        self.decide = decide
        self.bodies = []

    def open(self, request, timeout):
        del timeout
        body = json.loads((request.data or b"{}").decode("utf-8"))
        self.bodies.append(body)
        return Reply(self.decide(len(self.bodies), body))


class _ProbeFixture(_VitaFixture):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-order-probe-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def case(self):
        return probe.build_case(VITA_SOURCE)

    def run_scripted(self, decide, mutate=None):
        case = self.case()
        if mutate is not None:
            mutate(case)
        opener = ScriptedOpener(decide)
        self._runs = getattr(self, "_runs", 0) + 1
        journal = self.dir / f"chat-wire-{self._runs}.private.jsonl"
        clock = FakeClock()
        result = probe.execute_probe(
            case, TRANSPORT_CONFIG, request_output_tokens=2048, temperature=0.0,
            api_key=KEY, journal_path=journal, opener=opener,
            clock=clock.monotonic, sleep=clock.sleep)
        return result, opener, case, journal

    @staticmethod
    def tool_messages(body):
        return [message for message in body["messages"] if message.get("role") == "tool"]


class PlanAndRefusalTests(_ProbeFixture):
    def test_plan_is_offline_and_freezes_the_case_and_budget(self):
        out = self.dir / "plan-run"
        with patch.object(socket.socket, "connect",
                          side_effect=AssertionError("PLAN must not touch the network")):
            code = probe.main(["--stage", "plan", "--vita-source", str(VITA_SOURCE),
                               "--transport-config", str(TRANSPORT_CONFIG),
                               "--output-dir", str(out)])
        self.assertEqual(code, 0)
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], probe.STATUS_PLAN)
        self.assertEqual(set(plan["exposed_tools"]), set(probe.TOOLS))
        self.assertTrue(plan["only_these_tools_exposed"])
        self.assertEqual(plan["budget"]["max_requests"], 3)
        self.assertEqual(plan["budget"]["temperature"], 0.0)
        self.assertIsNone(plan["budget"]["seed"])
        self.assertFalse(plan["budget"]["parallel_tool_calls"])
        self.assertEqual(plan["case"]["db"]["orders"], {})
        self.assertIn("P1", json.dumps(plan["case"]["db"], ensure_ascii=False))
        self.assertIn("U1", plan["case"]["user_request"])
        self.assertNotIn("不要重复", plan["case"]["system_prompt"])
        self.assertNotIn("check existing", plan["case"]["system_prompt"].lower())
        self.assertIsNone(plan["scientific_result"])

    def snapshot(self, directory):
        return {entry.name: entry.read_bytes() for entry in sorted(directory.iterdir())}

    def test_refusing_an_existing_directory_writes_nothing(self):
        """Zero-write on refusal: not even a failure report may be written."""
        variants = {
            "empty": {},
            "with a file": {"keep.txt": b"keep"},
            "with an old result": {"result.json": b'{"old": true}\n',
                                   "keep.txt": b"keep"},
        }
        for label, files in variants.items():
            with self.subTest(directory=label):
                out = self.dir / f"taken-{label.replace(' ', '-')}"
                out.mkdir()
                for name, raw in files.items():
                    (out / name).write_bytes(raw)
                before = self.snapshot(out)
                with contextlib.redirect_stderr(io.StringIO()):
                    code = probe.main(["--stage", "plan", "--vita-source", str(VITA_SOURCE),
                                       "--transport-config", str(TRANSPORT_CONFIG),
                                       "--output-dir", str(out)])
                self.assertEqual(code, 2)
                self.assertEqual(self.snapshot(out), before)
                self.assertNotIn("plan.json", self.snapshot(out))

    def test_an_owned_directory_still_saves_a_bounded_failure(self):
        """A failure AFTER this call created the directory is still recorded."""
        out = self.dir / "owned-failure"
        key_file = self.dir / "world-readable.key"
        key_file.write_text(KEY, encoding="utf-8")
        os.chmod(key_file, 0o644)
        with contextlib.redirect_stderr(io.StringIO()):
            code = probe.main(["--stage", "run", "--vita-source", str(VITA_SOURCE),
                               "--transport-config", str(TRANSPORT_CONFIG),
                               "--key-file", str(key_file), "--output-dir", str(out)])
        self.assertEqual(code, 2)
        self.assertTrue((out / "plan.json").is_file())
        failure = json.loads((out / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["status"], probe.STATUS_INVALID)
        self.assertIn("0600", failure["invalid_reasons"][0]["message"])

    def test_a_declared_missing_vita_source_fails_hard(self):
        missing = self.dir / "no-such-vita"
        with self.assertRaises(RuntimeError) as caught:
            probe.build_plan(missing, TRANSPORT_CONFIG, request_output_tokens=2048,
                             temperature=0.0)
        self.assertIn("fixed Vita source is missing", str(caught.exception))
        with contextlib.redirect_stderr(io.StringIO()):
            code = probe.main(["--stage", "plan", "--vita-source", str(missing),
                               "--transport-config", str(TRANSPORT_CONFIG),
                               "--output-dir", str(self.dir / "missing-run")])
        self.assertEqual(code, 2)
        self.assertFalse((self.dir / "missing-run").exists())

    def test_run_requires_an_explicit_key_file(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                probe.main(["--stage", "run", "--vita-source", str(VITA_SOURCE),
                            "--transport-config", str(TRANSPORT_CONFIG),
                            "--output-dir", str(self.dir / "no-key")])
        self.assertFalse((self.dir / "no-key").exists())


class ScriptedChainTests(_ProbeFixture):
    def test_a_real_create_return_supplies_the_paid_id(self):
        def decide(index, body):
            if index == 1:
                return assistant(calls=[tool_call("c1", "create_delivery_order", CREATE_ARGS)])
            returns = self.tool_messages(body)
            self.assertTrue(returns, "the create return must reach the next request")
            real = probe.order_id_from(returns[-1]["content"])
            self.assertTrue(real, returns[-1]["content"])
            return assistant(calls=[tool_call("c2", "pay_delivery_order", {"order_id": real})])

        result, opener, _case, journal = self.run_scripted(decide)
        self.assertEqual(result["status"], probe.STATUS_PASSED)
        self.assertTrue(result["validity_passed"])
        self.assertTrue(result["model_task_completed"])
        self.assertIs(result["task_success"], True)
        self.assertIsNone(result["scientific_result"])
        self.assertEqual(result["requests_sent"], 2)
        self.assertEqual(len(opener.bodies), 2)
        self.assertEqual(len(result["created_order_ids"]), 1)
        self.assertTrue(result["payment_source_verified"])
        self.assertEqual(result["payment_id_created_in_request"], 1)
        self.assertEqual(result["order_state_check"], "ok")
        orders = result["final_native_snapshot"]["orders"]
        self.assertEqual(len(orders), 1)
        order = list(orders.values())[0]
        self.assertEqual(order["status"], "paid")
        self.assertEqual(order["user_id"], "U1")
        self.assertEqual(order["store_id"], "S1")
        self.assertEqual(order["location"]["address"], "Addr")
        self.assertEqual(order["dispatch_time"], "2024-06-23 15:00:00")
        self.assertEqual(order["products"][0]["product_id"], "P1")
        self.assertEqual(order["products"][0]["quantity"], 1)
        attributes = order["products"][0]["attributes"]
        if isinstance(attributes, str):
            attributes = [attributes]
        self.assertIn("7分糖", " ".join(attributes))
        # The create return entered the model's next input UNCHANGED, and the paid
        # id is exactly the one from that return.
        create_return = self.tool_messages(opener.bodies[1])[-1]
        self.assertEqual(create_return["tool_call_id"], "c1")
        self.assertEqual(probe.order_id_from(create_return["content"]),
                         result["created_order_ids"][0])
        self.assertEqual(result["paid_order_id"], result["created_order_ids"][0])
        self.assertTrue(result["fixture_used"])
        self.assertTrue(journal.is_file())
        kinds = [json.loads(line)["kind"] for line in journal.read_text().splitlines()]
        self.assertIn("client_request", kinds)
        self.assertIn("upstream_request", kinds)
        self.assertIn("cloud_summary", kinds)
        self.assertEqual(kinds.count("client_request"), 2)

    def test_a_fabricated_id_is_recorded_and_never_substituted(self):
        invented = "O202406231430001"

        def decide(index, body):
            if index == 1:
                return assistant(calls=[tool_call("c1", "pay_delivery_order",
                                                  {"order_id": invented})])
            return assistant(content="订单已创建并支付成功", finish="stop")

        result, opener, _case, _journal = self.run_scripted(decide)
        self.assertEqual(result["requests_sent"], 2)
        self.assertEqual(result["created_order_ids"], [])
        record = result["steps"][0]["tool_calls"][0]
        self.assertEqual(record["name"], "pay_delivery_order")
        self.assertEqual(record["arguments"]["order_id"], invented)
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["kind"], "VerifiedWritePrecondition")
        self.assertTrue(record["dispatched"])
        self.assertEqual(record["tool_return"], f"Error: Order {invented} not found")
        self.assertNotEqual(result["status"], probe.STATUS_PASSED)
        self.assertIs(result["task_success"], False)
        self.assertEqual(result["final_native_snapshot"]["orders"], {})
        # The model's own proposal is recorded verbatim and nothing was rewritten:
        # the first request carries only the frozen user prompt, and the recorded
        # call still holds the invented id.
        self.assertFalse(any(message.get("tool_calls") for message in opener.bodies[0]["messages"]))
        self.assertEqual(result["steps"][0]["request_messages"][-1]["content"],
                         probe.USER_REQUEST)
        self.assertEqual(record["arguments"], {"order_id": invented})
        self.assertEqual(opener.bodies[0]["model"], MODEL)

    def test_a_verbal_claim_without_tool_calls_is_not_success(self):
        def decide(_index, _body):
            return assistant(content="我已经为你创建订单并完成支付。", finish="stop")

        result, opener, _case, _journal = self.run_scripted(decide)
        self.assertEqual(result["status"], probe.STATUS_CLAIMED)
        self.assertEqual(result["requests_sent"], 1)
        self.assertEqual(len(opener.bodies), 1)
        self.assertIs(result["task_success"], False)
        self.assertEqual(result["final_native_snapshot"]["orders"], {})
        self.assertEqual(result["steps"][0]["assistant_text"],
                         "我已经为你创建订单并完成支付。")

    def test_bad_arguments_and_undeclared_tools_are_never_dispatched(self):
        bad_create = dict(CREATE_ARGS, bogus=1)

        def decide(index, _body):
            if index == 1:
                return assistant(calls=[tool_call("c1", "create_delivery_order", bad_create)])
            if index == 2:
                return assistant(calls=[tool_call("c2", "get_user_all_orders", {})])
            return assistant(content="done", finish="stop")

        result, _opener, _case, _journal = self.run_scripted(decide)
        kinds = [record["kind"] for step in result["steps"] for record in step["tool_calls"]]
        self.assertEqual(kinds, ["schema", "undeclared_tool"])
        self.assertEqual([record["dispatched"] for step in result["steps"]
                          for record in step["tool_calls"]], [False, False])
        self.assertEqual(result["final_native_snapshot"]["orders"], {})
        self.assertEqual(result["created_order_ids"], [])
        self.assertEqual(result["status"], probe.STATUS_CLAIMED)
        self.assertIs(result["task_success"], False)

    def test_a_batch_that_pays_then_creates_the_whole_batch_is_processed(self):
        """A payment must not swallow the tail of its own provider batch."""
        def decide(index, body):
            if index == 1:
                return assistant(calls=[tool_call("c1", "create_delivery_order", CREATE_ARGS)])
            real = probe.order_id_from(self.tool_messages(body)[-1]["content"])
            return assistant(calls=[
                tool_call("c2", "pay_delivery_order", {"order_id": real}),
                tool_call("c3", "create_delivery_order", CREATE_ARGS)])

        result, _opener, _case, _journal = self.run_scripted(decide)
        self.assertEqual(result["requests_sent"], 2)
        self.assertNotEqual(result["status"], probe.STATUS_PASSED)
        self.assertIs(result["task_success"], False)
        # The SECOND ORDER, not the tail itself, is what rejects the task.
        self.assertEqual(result["stop_reason"], "paid_state_mismatch")
        self.assertEqual(result["order_state_check"], "expected exactly one order, found 2")
        records = [record for step in result["steps"] for record in step["tool_calls"]]
        self.assertEqual([record["name"] for record in records],
                         ["create_delivery_order", "pay_delivery_order", "create_delivery_order"])
        self.assertEqual([record["status"] for record in records],
                         ["success", "success", "success"])
        self.assertEqual(len(result["final_native_snapshot"]["orders"]), 2)
        self.assertEqual(result["redundant_actions"], 1)
        self.assertEqual([tail["name"] for tail in result["batch_tail_after_success"]],
                         ["create_delivery_order"])

    def test_a_batch_tail_after_the_payment_is_fully_evidenced(self):
        """An invalid call after the payment is recorded, never hidden by it."""
        def decide(index, body):
            if index == 1:
                return assistant(calls=[tool_call("c1", "create_delivery_order", CREATE_ARGS)])
            real = probe.order_id_from(self.tool_messages(body)[-1]["content"])
            return assistant(calls=[
                tool_call("c2", "pay_delivery_order", {"order_id": real}),
                tool_call("c3", "get_user_all_orders", {})])

        result, _opener, _case, _journal = self.run_scripted(decide)
        self.assertEqual(result["requests_sent"], 2)
        self.assertNotEqual(result["status"], probe.STATUS_PASSED)
        self.assertIs(result["task_success"], False)
        self.assertEqual(result["stop_reason"], "batch_tail_protocol_violation")
        tail = result["batch_tail_after_success"]
        self.assertEqual([item["name"] for item in tail], ["get_user_all_orders"])
        self.assertEqual(tail[0]["status"], "error")
        self.assertEqual(tail[0]["kind"], "undeclared_tool")
        self.assertFalse(tail[0]["dispatched"])
        # The successful part is still fully evidenced.
        self.assertEqual(len(result["created_order_ids"]), 1)
        self.assertTrue(result["payment_source_verified"])

    def test_a_repeated_payment_of_the_same_order_completes_the_task(self):
        """Two valid payments of the same real order: redundancy, not failure."""
        def decide(index, body):
            if index == 1:
                return assistant(calls=[tool_call("c1", "create_delivery_order", CREATE_ARGS)])
            real = probe.order_id_from(self.tool_messages(body)[-1]["content"])
            return assistant(calls=[
                tool_call("c2", "pay_delivery_order", {"order_id": real}),
                tool_call("c3", "pay_delivery_order", {"order_id": real})])

        result, _opener, _case, _journal = self.run_scripted(decide)
        self.assertEqual(result["requests_sent"], 2)
        self.assertEqual(result["status"], probe.STATUS_PASSED)
        self.assertTrue(result["validity_passed"])
        self.assertTrue(result["model_task_completed"])
        self.assertIs(result["task_success"], True)
        self.assertEqual(result["stop_reason"], "created_then_paid_real_order")
        self.assertEqual(result["order_state_check"], "ok")
        self.assertTrue(result["payment_source_verified"])
        self.assertEqual(len(result["final_native_snapshot"]["orders"]), 1)
        self.assertEqual(result["redundant_actions"], 1)
        tail = result["batch_tail_after_success"]
        self.assertEqual([item["name"] for item in tail], ["pay_delivery_order"])
        self.assertEqual(tail[0]["status"], "success")
        self.assertTrue(tail[0]["dispatched"])
        records = [record for step in result["steps"] for record in step["tool_calls"]]
        self.assertEqual([record["tool_call_id"] for record in records], ["c1", "c2", "c3"])
        self.assertEqual([record["status"] for record in records],
                         ["success", "success", "success"])

    def test_a_noncanonical_specification_never_passes(self):
        """The frozen case needs the exact canonical specification, not a substring."""
        cases = {"negation": ["不要7分糖，改5分糖"], "five": ["5分糖"],
                 "seventeen": ["17分糖"], "empty": []}
        for label, attributes in cases.items():
            with self.subTest(spec=label):
                arguments = dict(CREATE_ARGS, attributes=attributes)

                def decide(index, body):
                    if index == 1:
                        return assistant(calls=[tool_call("c1", "create_delivery_order", arguments)])
                    real = probe.order_id_from(self.tool_messages(body)[-1]["content"])
                    return assistant(calls=[tool_call("c2", "pay_delivery_order",
                                                      {"order_id": real})])

                result, _opener, _case, _journal = self.run_scripted(decide)
                self.assertNotEqual(result["status"], probe.STATUS_PASSED)
                self.assertIs(result["task_success"], False)
                self.assertEqual(result["stop_reason"], "paid_state_mismatch")
                self.assertIn("attribute", result["order_state_check"])
                orders = result["final_native_snapshot"]["orders"]
                self.assertEqual(len(orders), 1)
                self.assertEqual(orders[result["created_order_ids"][0]]["status"], "paid")
                record = [r for step in result["steps"] for r in step["tool_calls"]
                          if r["name"] == "create_delivery_order"][0]
                self.assertEqual(record["arguments"]["attributes"], attributes)

    def test_the_canonical_specification_passes(self):
        def decide(index, body):
            if index == 1:
                return assistant(calls=[tool_call("c1", "create_delivery_order", CREATE_ARGS)])
            real = probe.order_id_from(self.tool_messages(body)[-1]["content"])
            return assistant(calls=[tool_call("c2", "pay_delivery_order", {"order_id": real})])

        result, _opener, _case, _journal = self.run_scripted(decide)
        self.assertEqual(result["status"], probe.STATUS_PASSED)
        self.assertEqual(result["order_state_check"], "ok")

    def test_a_payment_id_that_differs_from_the_real_return_does_not_pass(self):
        def decide(index, body):
            if index == 1:
                return assistant(calls=[tool_call("c1", "create_delivery_order", CREATE_ARGS)])
            if index == 2:
                real = probe.order_id_from(self.tool_messages(body)[-1]["content"])
                return assistant(calls=[tool_call("c2", "pay_delivery_order",
                                                  {"order_id": real + "X"})])
            return assistant(content="done", finish="stop")

        result, _opener, _case, _journal = self.run_scripted(decide)
        record = [r for step in result["steps"] for r in step["tool_calls"]
                  if r["name"] == "pay_delivery_order"][0]
        self.assertEqual(record["status"], "error")
        self.assertFalse(record["order_id_from_an_earlier_request_create"])
        self.assertNotEqual(result["status"], probe.STATUS_PASSED)
        self.assertIs(result["task_success"], False)

    def test_an_uncertain_tool_outcome_stops_without_replay(self):
        def mutate(case):
            from vita.data_model.tasks import Order
            injected = Order.model_validate({
                "order_id": "#INJECTED", "order_type": "delivery", "user_id": "U1",
                "store_id": "S1", "status": "unpaid", "products": []})

            class ExplodingOrders(dict):
                def __contains__(self, key):
                    dict.__setitem__(self, "#INJECTED", injected)
                    raise RuntimeError("fixture write failure before the lookup raises")

            case["environment"].tools.db.orders = ExplodingOrders(
                case["environment"].tools.db.orders)

        def decide(_index, _body):
            return assistant(calls=[tool_call("c1", "pay_delivery_order",
                                              {"order_id": "O-INVENTED"})])

        result, opener, _case, _journal = self.run_scripted(decide, mutate=mutate)
        self.assertEqual(result["status"], probe.STATUS_UNCERTAIN)
        self.assertEqual(result["requests_sent"], 1)
        self.assertEqual(len(opener.bodies), 1, "no replay after an uncertain outcome")
        self.assertIsNone(result["task_success"])
        self.assertFalse(result["validity_passed"])
        self.assertEqual(result["errors"][0]["type"], "uncertain_tool_outcome")
        self.assertIn("final_native_snapshot", result)

    def test_the_request_budget_is_three_with_no_automatic_retry(self):
        def decide(index, _body):
            return assistant(calls=[tool_call(f"c{index}", "pay_delivery_order",
                                              {"order_id": f"O-INVENTED-{index}"})])

        result, opener, _case, _journal = self.run_scripted(decide)
        self.assertEqual(result["requests_sent"], 3)
        self.assertEqual(len(opener.bodies), 3)
        self.assertEqual(result["request_count"], 3)
        self.assertEqual(result["status"], probe.STATUS_INCOMPLETE)
        self.assertEqual(result["stop_reason"], "request_budget_exhausted")
        self.assertIs(result["task_success"], False)
        # Every call id appears exactly once: nothing was retried or replayed.
        ids = [record["tool_call_id"] for step in result["steps"]
               for record in step["tool_calls"]]
        self.assertEqual(ids, ["c1", "c2", "c3"])
        self.assertEqual(result["final_native_snapshot"]["orders"], {})

    def test_a_create_and_a_guessed_payment_in_one_batch_never_passes(self):
        def decide(index, _body):
            if index == 1:
                return assistant(calls=[
                    tool_call("c1", "create_delivery_order", CREATE_ARGS),
                    tool_call("c2", "pay_delivery_order", {"order_id": "O-GUESSED"})])
            return assistant(content="done", finish="stop")

        result, _opener, _case, _journal = self.run_scripted(decide)
        self.assertEqual(result["requests_sent"], 2)
        self.assertNotEqual(result["status"], probe.STATUS_PASSED)
        payment = [r for step in result["steps"] for r in step["tool_calls"]
                   if r["name"] == "pay_delivery_order"][0]
        self.assertEqual(payment["status"], "error")
        self.assertFalse(payment["order_id_from_an_earlier_request_create"])
        self.assertEqual(result["final_native_snapshot"]["orders"][
            result["created_order_ids"][0]]["status"], "unpaid")


if __name__ == "__main__":
    unittest.main()
