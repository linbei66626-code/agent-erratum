"""Local 9B order probe: the offline acceptance for the complete-information case.

The provider is a scripted loopback opener, so no network and no model call happens;
everything else is the production path - the fixed loopback-only endpoint, the frozen
native case and its message formatting, the REAL Vita tool execution, and the native
order-state acceptance. A pass here is a statement about this PROBE, never a claim about
model capability.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

MODEL = "Qwen/Qwen3.5-9B"
#: The frozen catalogue value the new case states VERBATIM (the old run's loss).
EXACT_SPEC = "规格: 7分糖"
#: What the old run actually sent: the same sugar level without the catalogue prefix.
TRUNCATED_SPEC = "7分糖"
CREATE_ARGS = {"user_id": "U1", "store_id": "S1", "product_ids": ["P1"], "product_cnts": [1],
               "address": "Addr", "dispatch_time": "2024-06-23 15:00:00"}
DIET_NOTE = "无特殊饮食禁忌"


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "ae_01_local_order_probe_under_test", ROOT / "scripts/ae_01_local_order_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vita_source():
    import os
    root = Path(os.environ.get("AE_VERIFY_ROOT", ROOT / ".ae-verify-src"))
    return Path(os.environ.get("AE_VITA_SOURCE", root / "source"))


def _reply(payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    class _Response(io.BytesIO):
        def __init__(self):
            super().__init__(body)
            self.code, self.headers = status, {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False
    return _Response()


def _tool_call(name, arguments, call_id, finish_reason="tool_calls"):
    return {"index": 0, "finish_reason": finish_reason,
            "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": call_id, "type": "function",
                                        "function": {"name": name,
                                                     "arguments": json.dumps(
                                                         arguments, ensure_ascii=False)}}]},
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


class _ScriptedProvider:
    """A model stand-in that READS the real tool return it was given.

    This is what a model is supposed to do: the order id comes from the tool's own return,
    never from thin air. `attribute` is what it copies into the order specification.
    """

    def __init__(self, *, attribute=EXACT_SPEC, note=DIET_NOTE, mode="create_then_pay",
                 statuses=None):
        self.sent = []
        self.requests = []
        self.attribute = attribute
        self.note = note
        self.mode = mode
        self.statuses = statuses or {}

    def _order_id_from(self, body):
        import re
        pattern = re.compile(r"order_id[:=]['\"]?([^,'\")\s]+)")
        for message in reversed(body.get("messages") or []):
            if message.get("role") != "tool":
                continue
            match = pattern.search(message.get("content") or "")
            if match:
                return match.group(1)
        return None

    def open(self, request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        attempt = len(self.sent) + 1
        self.sent.append(body)
        self.requests.append({"url": request.full_url, "timeout": timeout, "body": body})
        status = self.statuses.get(attempt, 200)
        if status != 200:
            return _reply({"error": {"message": "scripted failure", "code": status}}, status)
        call_id = f"call-{attempt}"
        if self.mode == "truncated":
            return _reply({"id": "gen", "object": "chat.completion", "model": MODEL,
                           "choices": [{"index": 0, "finish_reason": "length",
                                        "message": {"role": "assistant", "content": "..."}}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 2048,
                                     "total_tokens": 2058}})
        if self.mode == "guessed_payment":
            return _reply({"id": "gen", "object": "chat.completion", "model": MODEL,
                           "choices": [_tool_call("pay_delivery_order",
                                                  {"order_id": "guessed-0001"}, call_id)],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                     "total_tokens": 15}})
        order_id = self._order_id_from(body)
        if order_id is None:
            create = dict(CREATE_ARGS, attributes=[self.attribute], note=self.note)
            return _reply({"id": "gen", "object": "chat.completion", "model": MODEL,
                           "choices": [_tool_call("create_delivery_order", create, call_id)],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                     "total_tokens": 15}})
        return _reply({"id": "gen", "object": "chat.completion", "model": MODEL,
                       "choices": [_tool_call("pay_delivery_order",
                                              {"order_id": order_id}, call_id)],
                       "usage": {"prompt_tokens": 20, "completion_tokens": 8,
                                 "total_tokens": 28}})


class _FailingConnect:
    """A real opener contract whose send fails: no HTTP answer ever arrives."""

    def __init__(self, error=None):
        self.error = error or TimeoutError("scripted connect timeout")

    def open(self, request, timeout):
        raise self.error


class LocalOrderProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = _load_probe()
        self.vita = _vita_source()
        if not (self.vita / "src/vita").is_dir():
            self.skipTest("the fixed Vita source is absent")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _out(self, name):
        return Path(self.tmp.name) / name

    def _execute(self, provider, name="run"):
        out = self._out(name)
        result = self.probe.execute_probe(vita_source=self.vita, output_dir=out,
                                          opener=provider)
        return result, out

    def _saved_database(self, out, result):
        """The saved artifact must agree with the result's own snapshot."""
        saved = json.loads((out / "final-database.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, result["final_native_snapshot"])
        return saved

    # ---------------------------------------------------------------- plan / endpoint

    def test_plan_sends_nothing_and_declares_the_new_case_version(self):
        called = []

        def _forbidden(request, timeout):  # pragma: no cover - must never run
            called.append(request.full_url)
            raise AssertionError("plan must not open a socket")

        plan = self.probe.build_plan(self.vita)
        self.assertEqual(plan["status"], self.probe.STATUS_PLAN)
        self.assertFalse(plan["network_called"])
        self.assertFalse(plan["model_called"])
        self.assertEqual(plan["case_version"], "local-order-complete-info-v1")
        self.assertEqual(plan["model"], MODEL)
        self.assertEqual(plan["endpoint"]["origin"], "http://127.0.0.1:8001")
        self.assertTrue(plan["endpoint"]["loopback_only"])
        self.assertEqual(plan["budget"]["max_requests"], 3)
        self.assertIs(plan["budget"]["thinking"]["enable_thinking"], False)
        self.assertEqual(plan["budget"]["thinking_placement"], "chat_template_kwargs")
        self.assertEqual(called, [])

        # the plan command creates the directory and calls nothing
        out = self._out("plan")
        code = self.probe.main(["--stage", "plan", "--vita-source", str(self.vita),
                                "--output-dir", str(out)])
        self.assertEqual(code, 0)
        saved = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["case_version"], "local-order-complete-info-v1")
        self.assertFalse((out / "result.json").exists())
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["plan.json"])

    def test_the_endpoint_can_never_leave_loopback(self):
        self.assertEqual(self.probe.validate_local_endpoint("http://127.0.0.1:8001"),
                         "http://127.0.0.1:8001")
        for bad in ("https://127.0.0.1:8001", "http://api.siliconflow.cn",
                    "http://10.0.0.5:8001", "http://127.0.0.1:8001/v1/chat/completions"):
            with self.assertRaises(ValueError, msg=bad):
                self.probe.validate_local_endpoint(bad)
        self.assertEqual(self.probe.endpoint_url(), "http://127.0.0.1:8001/v1/chat/completions")

    # ------------------------------------------------------------------ the new facts

    def test_the_wire_carries_the_exact_catalogue_value_and_the_diet_fact(self):
        provider = _ScriptedProvider()
        result, out = self._execute(provider)
        self.assertEqual(result["status"], self.probe.STATUS_PASSED, result)
        body = provider.sent[0]
        wire = json.dumps(body, ensure_ascii=False)
        # the exact catalogue value and the diet fact are both VISIBLE to the model
        self.assertIn(EXACT_SPEC, wire)
        self.assertIn("无特殊饮食禁忌", wire)
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 2048)
        self.assertIs(body["chat_template_kwargs"]["enable_thinking"], False)
        self.assertIs(body["parallel_tool_calls"], False)
        self.assertEqual(sorted(t["function"]["name"] for t in body["tools"]),
                         ["create_delivery_order", "pay_delivery_order"])
        # ...and the saved case document records the facts and refuses to claim equality
        saved = json.loads((out / "case.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["case_version"], "local-order-complete-info-v1")
        self.assertTrue(saved["not_claimed_identical_to_the_old_input"])
        self.assertTrue(saved["old_version_unchanged"])
        self.assertTrue(saved["expected_spec_matches_declared_fact"])
        facts = saved["completion_facts"]
        self.assertEqual(facts["catalog_attribute_value"]["value"], EXACT_SPEC)
        self.assertFalse(facts["catalog_attribute_value"]["was_visible_before"])
        self.assertEqual(facts["user_dietary_restriction"]["value"], "无特殊饮食禁忌")
        self.assertFalse(facts["user_dietary_restriction"]["was_visible_before"])

    # ------------------------------------------------------------------- the outcomes

    def test_real_create_then_pay_with_the_exact_spec_passes(self):
        provider = _ScriptedProvider()
        result, out = self._execute(provider)
        self.assertEqual(result["status"], self.probe.STATUS_PASSED)
        self.assertTrue(result["task_success"])
        self.assertTrue(result["payment_source_verified"])
        self.assertTrue(result["order_state_matched"])
        self.assertEqual(result["order_state_check"], "ok")
        self.assertEqual(result["stop_reason"], "created_then_paid_real_order")
        self.assertEqual(result["requests_attempted"], 2)
        self.assertEqual(result["responses_received"], 2)
        self.assertEqual(len(provider.sent), 2, "no third request was needed or made")
        # the real native database holds exactly one paid order with the exact spec
        orders = self._saved_database(out, result)["orders"]
        self.assertEqual(len(orders), 1)
        order = list(orders.values())[0]
        self.assertEqual(order["status"], "paid")
        self.assertEqual(order["products"][0]["attributes"], EXACT_SPEC)
        # the payment id came from the real create return, not from the model's imagination
        self.assertIn(result["paid_order_id"], result["created_order_ids"])
        for index in (1, 2):
            self.assertTrue((out / f"request-{index}.json").is_file())
            self.assertTrue((out / f"response-{index}.json").is_file())

    def test_a_wrong_spec_stays_false_after_payment_and_keeps_the_reason(self):
        """The old failure, made explicit: payment succeeds, the field check does not."""
        provider = _ScriptedProvider(attribute=TRUNCATED_SPEC)
        result, out = self._execute(provider)
        self.assertEqual(result["status"], self.probe.STATUS_INCOMPLETE)
        self.assertFalse(result["task_success"])
        self.assertFalse(result["order_state_matched"])
        self.assertEqual(result["order_state_check"], "mismatched fields: attribute")
        self.assertEqual(result["stop_reason"], "paid_state_mismatch")
        # the id provenance is still real, and the run ended ON the failing batch
        self.assertTrue(result["payment_source_verified"])
        self.assertEqual(result["requests_attempted"], 2)
        self.assertEqual(len(provider.sent), 2)
        self.assertEqual(len(result["steps"]), 2)
        self.assertNotIn("NO_TOOL_CALL", json.dumps(result))
        # the saved native database shows what was really created
        order = list(result["final_native_snapshot"]["orders"].values())[0]
        self.assertEqual(order["products"][0]["attributes"], TRUNCATED_SPEC)

    def test_a_fabricated_order_id_never_passes(self):
        provider = _ScriptedProvider(mode="guessed_payment")
        result, _out = self._execute(provider, name="guessed")
        self.assertNotEqual(result["status"], self.probe.STATUS_PASSED)
        self.assertFalse(result["task_success"])
        self.assertEqual(result["payment_source_verified"], None)
        payments = result["steps"][0]["tool_calls"]
        self.assertEqual(payments[0]["status"], "error")
        self.assertFalse(payments[0]["order_id_from_an_earlier_request_create"])
        self.assertIn("not found", (payments[0]["tool_return"] or "").lower())
        self.assertEqual(result["final_native_snapshot"]["orders"], {},
                         "a refused payment must leave no order behind")

    def test_truncated_output_is_incomplete_and_stops(self):
        provider = _ScriptedProvider(mode="truncated")
        result, _out = self._execute(provider, name="length")
        self.assertEqual(result["status"], self.probe.STATUS_LENGTH)
        self.assertFalse(result["task_success"])
        self.assertEqual(result["stop_reason"], "finish_reason_length")
        self.assertEqual(result["requests_attempted"], 1)
        self.assertEqual(result["responses_received"], 1)
        self.assertEqual(len(provider.sent), 1, "a truncation is never retried")

    def test_a_provider_error_status_stops_and_is_recorded_as_itself(self):
        provider = _ScriptedProvider(statuses={1: 400})
        result, _out = self._execute(provider, name="http400")
        self.assertEqual(result["status"], self.probe.STATUS_INVALID)
        self.assertEqual(result["stop_reason"], "provider_http_400")
        self.assertEqual(result["requests_attempted"], 1)
        self.assertEqual(result["responses_received"], 1, "a 400 IS an answer")
        self.assertEqual(len(provider.sent), 1, "an error status is never retried")

    def test_a_real_run_is_not_reported_as_a_fixture_and_marks_the_attempt(self):
        """No opener is passed: the probe builds its own, so `fixture_used` must be false.

        The default opener FACTORY is replaced by the test, which is how a real run's own
        opener gets an offline answer - the probe itself is never told it is under test.
        This drives the REAL native create->pay chain through the real production path.
        """
        provider = _ScriptedProvider()
        with unittest.mock.patch.object(
                self.probe, "build_opener",
                unittest.mock.MagicMock(return_value=provider)) as factory:
            result, out = self._execute(None, name="realpath")
        factory.assert_called_once()
        self.assertFalse(result["fixture_used"],
                         "a run whose opener the probe built itself is NOT a fixture run")
        # model_called is the transport-attempt marker, documented as such
        self.assertTrue(result["model_called"])
        self.assertIn("TRANSPORT-ATTEMPT", result["model_called_definition"])
        self.assertEqual(result["requests_attempted"], 2)
        self.assertEqual(result["responses_received"], 2)
        self.assertEqual(result["status"], self.probe.STATUS_PASSED)
        self.assertTrue(result["task_success"])
        self.assertTrue(result["payment_source_verified"])
        self.assertEqual(result["order_state_check"], "ok")
        self.assertEqual(len(provider.sent), 2)
        orders = self._saved_database(out, result)["orders"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(list(orders.values())[0]["status"], "paid")

    def test_an_injected_opener_is_still_reported_as_a_fixture(self):
        provider = _ScriptedProvider()
        result, _out = self._execute(provider, name="injected")
        self.assertTrue(result["fixture_used"],
                        "an explicitly injected opener IS a fixture run")
        self.assertTrue(result["model_called"])
        self.assertEqual(result["requests_attempted"], 2)
        self.assertEqual(result["responses_received"], 2)

    def test_an_unanswered_send_is_not_counted_as_executed(self):
        """attempted=1 but responses=0, and the provider outcome stays UNKNOWN."""
        result, _out = self._execute(_FailingConnect(), name="timeout")
        self.assertEqual(result["status"], self.probe.STATUS_INVALID)
        self.assertEqual(result["stop_reason"], "request_not_answered_no_retry")
        self.assertEqual(result["requests_attempted"], 1)
        self.assertEqual(result["responses_received"], 0)
        # the attempt marker is set, but it must NOT be read as "the service executed it"
        self.assertTrue(result["model_called"])
        self.assertIn("does NOT assert", result["model_called_definition"])
        self.assertFalse(result["accounting_ok"])
        self.assertEqual(result["accounting_error"],
                         "send_not_answered_provider_execution_unknown")
        self.assertIsNone(result["steps"][0]["http_status"])
        self.assertFalse(result["steps"][0]["response_received"])
        self.assertIsNone(result["task_success"])

    def test_a_second_run_into_the_same_directory_is_refused(self):
        out = self._out("once")
        self.probe.execute_probe(vita_source=self.vita, output_dir=out,
                                 opener=_ScriptedProvider())
        with self.assertRaises(FileExistsError):
            self.probe.execute_probe(vita_source=self.vita, output_dir=out,
                                     opener=_ScriptedProvider())


class LocalProbeBoundaryTests(unittest.TestCase):
    """The frozen inputs this probe reuses must not have been edited by it."""

    def setUp(self):
        self.probe = _load_probe()
        self.native = self.probe._load_native()

    def test_the_frozen_case_and_expected_spec_are_untouched(self):
        self.assertEqual(self.native.EXPECTED_SPEC, EXACT_SPEC)
        self.assertEqual(self.native.CASE_DB["stores"]["S1"]["products"][0]["attributes"],
                         [EXACT_SPEC])
        self.assertEqual(self.native.MAX_REQUESTS, 3)
        self.assertEqual(self.native.MODEL, "Qwen/Qwen3-30B-A3B-Instruct-2507")
        # the probe's own model is the LOCAL one, and it is not the cloud constant
        self.assertEqual(self.probe.MODEL_NAME, MODEL)
        self.assertNotEqual(self.probe.MODEL_NAME, self.native.MODEL)

    def test_the_declared_fact_is_the_oracle_value_not_a_new_one(self):
        self.assertEqual(self.probe.COMPLETION_FACTS["catalog_attribute_value"]["value"],
                         self.native.EXPECTED_SPEC)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
