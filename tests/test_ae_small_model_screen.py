"""Small-model screening: the offline acceptance for the screening entry.

The provider is a scripted loopback opener, so no network and no model call happens;
everything else is the production path - the screening transport profile, the real
proxy (ONE journal, ONE 65-second interval for both models), the frozen probe case with
its REAL Vita tool execution, and the native order-state acceptance.
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

import ae_cloud_proxy as px  # noqa: E402

TRANSPORT_CONFIG = ROOT / "configs/ae-01__small-model-screen__siliconflow.json"
KEY = "offline-screening-fixture-key-01"
MODELS = ("Qwen/Qwen3.5-4B", "Qwen/Qwen3.5-9B")


def _load_screen():
    spec = importlib.util.spec_from_file_location(
        "ae_01_small_model_screen_under_test", ROOT / "scripts/ae_01_small_model_screen.py")
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


def _tool_call(name, arguments, call_id):
    return {"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": call_id, "type": "function",
                                        "function": {"name": name,
                                                     "arguments": json.dumps(arguments,
                                                                             ensure_ascii=False)}}]},
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


#: The frozen case's create arguments, in the REAL schema's shape (the probe's own
#: fixture uses exactly these).
CREATE_ARGS = {"user_id": "U1", "store_id": "S1", "product_ids": ["P1"],
               "product_cnts": [1], "address": "Addr",
               "dispatch_time": "2024-06-23 15:00:00", "attributes": ["规格: 7分糖"]}


class _ScriptedProvider:
    """Answers the two tool turns by READING the real tool return it was given.

    This is what a model does: the create order id comes from the tool's own return, and
    the payment uses it. The opener never invents an order id.
    """

    def __init__(self, *, mode="create_then_pay", statuses=None, first_status=200):
        self.sent = []
        self.mode = mode
        self.statuses = statuses or {}
        self.first_status = first_status

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
        attempt = sum(1 for item in self.sent if item["model"] == body["model"]) + 1
        self.sent.append({"url": request.full_url, "model": body["model"],
                          "attempt": attempt, "body": body,
                          "thinking": body.get(px.SCREEN_THINKING_FIELD)})
        status = self.statuses.get((body["model"], attempt), 200)
        if len(self.sent) == 1:
            status = self.first_status
        if status != 200:
            return _reply({"error": {"message": "scripted failure", "code": status}}, status)
        call_id = f"call-{body['model'].split('/')[-1]}-{attempt}"
        if self.mode == "guessed_payment":
            payload = {"id": "gen", "object": "chat.completion", "model": body["model"],
                       "choices": [_tool_call("pay_delivery_order",
                                              {"order_id": "guessed-0001"}, call_id)],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            return _reply(payload)
        if self.mode == "truncated":
            payload = {"id": "gen", "object": "chat.completion", "model": body["model"],
                       "choices": [{"index": 0, "finish_reason": "length",
                                    "message": {"role": "assistant", "content": "..."}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 2048,
                                 "total_tokens": 2058}}
            return _reply(payload)
        if self.mode == "endless_create":
            payload = {"id": "gen", "object": "chat.completion", "model": body["model"],
                       "choices": [_tool_call("create_delivery_order", CREATE_ARGS, call_id)],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            return _reply(payload)
        order_id = self._order_id_from(body)
        if order_id is None:
            payload = {"id": "gen", "object": "chat.completion", "model": body["model"],
                       "choices": [_tool_call("create_delivery_order", CREATE_ARGS, call_id)],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        else:
            payload = {"id": "gen", "object": "chat.completion", "model": body["model"],
                       "choices": [_tool_call("pay_delivery_order",
                                              {"order_id": order_id}, call_id)],
                       "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}}
        return _reply(payload)


class _FakeClock:
    """A monotonic clock the test advances, so the 65 s interval is exercised offline."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScreeningCaseTests(unittest.TestCase):
    def setUp(self):
        self.screen = _load_screen()
        if not TRANSPORT_CONFIG.is_file():
            self.skipTest("the screening transport config is absent")
        self.vita = _vita_source()
        if not (self.vita / "src/vita").is_dir():
            self.skipTest("the fixed Vita source is absent")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _execute(self, provider, *, clock=None):
        clock = clock or _FakeClock()
        out = Path(self.tmp.name) / f"run-{len(list(Path(self.tmp.name).iterdir()))}"
        out.mkdir()
        result = self.screen.execute_screen(
            TRANSPORT_CONFIG, vita_source=self.vita, api_key=KEY,
            journal_path=out / "chat-wire.private.jsonl", opener=provider,
            clock=clock, sleep=clock.sleep)
        return result, out / "chat-wire.private.jsonl"

    def test_both_models_run_the_real_tool_chain_with_one_shared_interval(self):
        provider = _ScriptedProvider()
        clock = _FakeClock()
        result, journal = self._execute(provider, clock=clock)
        self.assertEqual(result["status"], self.screen.STATUS_COMPLETED)
        self.assertEqual([row["model"] for row in result["models"]], list(MODELS))
        for row in result["models"]:
            self.assertTrue(row["task_success"], (row["model"], row["status"]))
            self.assertTrue(row["payment_source_verified"])
            self.assertEqual(row["status"], "MODEL_TASK_SUCCESS")
            self.assertIn(row["status"], ("MODEL_TASK_SUCCESS",))
        self.assertEqual(result["requests_sent_total"], 4)
        self.assertLessEqual(result["requests_sent_total"], self.screen.MAX_REQUESTS_TOTAL)
        # every request carried its own model and the explicit thinking mode
        for item in provider.sent:
            self.assertEqual(item["body"]["model"], item["model"])
            self.assertIn(item["model"], MODELS)
            self.assertIs(item["thinking"], False)
            self.assertEqual(item["body"]["max_tokens"], 2048)
            self.assertEqual(item["body"]["temperature"], 0)
        # state isolation: each model's own environment holds exactly one order
        orders = [row["final_order_state"] for row in result["models"]]
        self.assertEqual([len(state) for state in orders], [1, 1])
        self.assertNotEqual(list(orders[0]), list(orders[1]))
        # one journal, and the pace chain crosses the model boundary at 65 s
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        chat = [row for row in rows if row.get("kind") == "client_request"
                and row.get("path") == "/v1/chat/completions"]
        self.assertEqual(len(chat), 4)
        paces = [row for row in rows if row.get("kind") == "pace_wait"]
        self.assertEqual([row["interval_seconds"] for row in paces], [65.0] * 4)
        self.assertTrue(paces[0]["first_send"])
        for previous, current in zip(paces, paces[1:]):
            self.assertEqual(current["previous_send_monotonic"],
                             previous["send_monotonic"])
        # the model boundary is between pace 2 and 3, and the interval still holds
        self.assertGreaterEqual(paces[2]["send_monotonic"] - paces[1]["send_monotonic"], 65.0)

    def test_the_thinking_mode_is_recorded_in_the_journal(self):
        provider = _ScriptedProvider()
        result, journal = self._execute(provider)
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        normalized = [row for row in rows if row.get("kind") == "normalized_request"]
        self.assertTrue(normalized)
        for row in normalized:
            operations = {(change["operation"], change["to"]): change["value"]
                          for change in row["changes"]}
            self.assertIn(("keep", px.SCREEN_THINKING_FIELD), operations)
            self.assertIs(operations[("keep", px.SCREEN_THINKING_FIELD)], False)
        for row in result["models"]:
            for step in row["steps"]:
                self.assertIs(step["thinking_mode"], False)

    def test_a_guessed_payment_is_not_a_pass(self):
        """The real tool refuses an order id the model never received."""
        provider = _ScriptedProvider(mode="guessed_payment")
        result, _journal = self._execute(provider)
        row = result["models"][0]
        self.assertFalse(row["task_success"])
        self.assertFalse(row["payment_source_verified"])
        self.assertNotIn("MODEL_TASK_SUCCESS", row["status"])
        payments = [entry for entry in row["tool_returns"]
                    if entry["name"] == "pay_delivery_order"]
        self.assertTrue(payments)
        self.assertTrue(all(entry["status"] == "error" for entry in payments), payments)
        self.assertIn("not found", (payments[0]["text"] or "").lower())
        self.assertEqual(row["final_order_state"], {},
                         "a refused payment must leave no paid order behind")

    def test_truncated_output_is_incomplete_not_a_success(self):
        provider = _ScriptedProvider(mode="truncated")
        result, _journal = self._execute(provider)
        row = result["models"][0]
        self.assertEqual(row["status"], "MODEL_OUTPUT_TRUNCATED")
        self.assertFalse(row["task_success"])
        self.assertEqual(row["stop_reason"], "finish_reason_length")
        self.assertEqual(row["requests_sent"], 1)

    def test_the_total_budget_is_at_most_six_posts(self):
        provider = _ScriptedProvider(mode="endless_create")
        result, _journal = self._execute(provider)
        self.assertEqual(result["requests_sent_total"], 6)
        self.assertEqual([row["requests_sent"] for row in result["models"]], [3, 3])
        self.assertTrue(all(row["status"] == "MODEL_BUDGET_EXHAUSTED"
                            for row in result["models"]))
        self.assertEqual(result["status"], self.screen.STATUS_COMPLETED)

    def test_http_429_stops_the_batch_without_a_retry(self):
        provider = _ScriptedProvider(first_status=429)
        result, journal = self._execute(provider)
        self.assertEqual(result["status"], self.screen.STATUS_STOPPED)
        self.assertEqual(result["models"][0]["stop_reason"], "provider_http_429")
        self.assertEqual(result["requests_sent_total"], 1)
        self.assertEqual(len(provider.sent), 1, "a 429 must not be retried")
        self.assertEqual(result["models_not_run"], [MODELS[1]])
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        self.assertEqual([row["http_status"] for row in rows
                          if row.get("kind") == "upstream_response"], [429])

    def test_plan_and_missing_key_send_nothing(self):
        out = Path(self.tmp.name) / "cli-plan"
        code = self.screen.main(["--stage", "plan", "--vita-source", str(self.vita),
                                 "--output-dir", str(out),
                                 "--transport-config", str(TRANSPORT_CONFIG)])
        self.assertEqual(code, 0)
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual([row["wire_model"] for row in plan["models"]], list(MODELS))
        self.assertEqual(plan["budget"]["max_requests_total"], 6)
        self.assertFalse((out / "chat-wire.private.jsonl").exists())
        missing = Path(self.tmp.name) / "no-such-key"
        out2 = Path(self.tmp.name) / "cli-run"
        code = self.screen.main(["--stage", "run", "--vita-source", str(self.vita),
                                 "--output-dir", str(out2), "--key-file", str(missing),
                                 "--transport-config", str(TRANSPORT_CONFIG)])
        self.assertEqual(code, 2)
        self.assertFalse((out2 / "chat-wire.private.jsonl").exists())
        self.assertEqual(json.loads((out2 / "result.json").read_text())["status"],
                         self.screen.STATUS_INVALID)

    def test_the_case_is_identical_and_the_environments_are_isolated(self):
        probe = self.screen._load_probe()
        first = probe.build_case(self.vita)
        second = probe.build_case(self.vita)
        self.assertEqual(probe.case_snapshot(first["environment"])["orders"], {})
        self.assertEqual(probe.case_snapshot(second["environment"])["orders"], {})
        probe._execute(first["bindings"]["create_delivery_order"],
                       "create_delivery_order", CREATE_ARGS)
        self.assertEqual(len(probe.case_snapshot(first["environment"])["orders"]), 1)
        self.assertEqual(probe.case_snapshot(second["environment"])["orders"], {})
        self.assertEqual(sorted(first["schemas"]), sorted(second["schemas"]))
        self.assertEqual(probe.dumps(first["schemas"]), probe.dumps(second["schemas"]))


class ScreeningSendAccountingTests(unittest.TestCase):
    """A send that FAILED is still a send; a pre-send refusal is not.

    The live run of 2026-09-15T02:59Z lost the 4B request to a transport failure after
    about 181 s. The journal held one `upstream_request` and no `upstream_response`,
    while `result.json` said `requests_sent_total: 0` and `model_called: false`, because
    the counter only advanced after `dispatch` returned. These cases pin the corrected
    accounting: what was SENT is read back from the journal, and whether the provider
    executed it is recorded as UNKNOWN instead of being assumed either way.
    """

    #: The live failure shape: the send starts, the connection cannot be established, and
    #: NO provider is reached - so the "unknown whether the provider executed it" branch
    #: is exercised without network, without a model call and without faking a response.
    CONNECT_FAILURE = OSError(51, "Network is unreachable")

    def setUp(self):
        self.screen = _load_screen()
        if not TRANSPORT_CONFIG.is_file():
            self.skipTest("the screening transport config is absent")
        self.vita = _vita_source()
        if not (self.vita / "src/vita").is_dir():
            self.skipTest("the fixed Vita source is absent")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _config(self, **overrides):
        document = json.loads(TRANSPORT_CONFIG.read_text(encoding="utf-8"))
        document.update(overrides)
        config = px.CloudConfig(**document)
        config.validate()
        return config

    def _execute(self, config, *, opener=None, prefix="run"):
        clock = _FakeClock()
        out = Path(self.tmp.name) / f"{prefix}-{len(list(Path(self.tmp.name).iterdir()))}"
        out.mkdir()
        journal = out / "chat-wire.private.jsonl"
        result = self.screen.execute_screen(
            TRANSPORT_CONFIG, vita_source=self.vita, api_key=KEY,
            journal_path=journal, opener=opener, clock=clock, sleep=clock.sleep,
            config_override=config)
        return result, journal

    def _rows(self, journal):
        return [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]

    def test_a_failed_send_is_counted_once_with_zero_responses_and_no_retry(self):
        """The live failure, reproduced offline: 1 send, 0 responses, 9B never started.

        Only the socket CONNECT is made to fail - the single layer that would touch the
        network, and it fails before any byte leaves the host. The real opener, the real
        `_upstream` path, the real journal and the real 65 s pacing all run unchanged. The
        origin stays the declared `.cn` endpoint: the transport refuses a config that
        swaps it, exactly as it should.
        """
        config = self._config(io_timeout_seconds=0.5)
        patch = unittest.mock.patch("socket.create_connection",
                                    side_effect=self.CONNECT_FAILURE)
        self.addCleanup(patch.stop)
        patch.start()
        result, journal = self._execute(config)
        row = result["models"][0]
        self.assertEqual(result["status"], self.screen.STATUS_STOPPED)
        self.assertEqual(row["status"], self.screen.STATUS_TRANSPORT_FAILED,
                         "a failed send must not be reported as a refused one")
        self.assertEqual(row["stop_detail"], "upstream_io_failure_no_retry")
        # exactly ONE attempt, ONE send, ZERO responses
        self.assertEqual(row["requests_attempted"], 1)
        self.assertEqual(row["sends"], 1)
        self.assertEqual(row["requests_sent"], 1)
        self.assertEqual(row["responses_received"], 0)
        self.assertEqual(row["steps"], [], "no HTTP response means no completed step")
        self.assertIsNone(row["task_success"])
        self.assertEqual(row["provider_execution_state"],
                         self.screen.PROVIDER_EXECUTION_UNKNOWN)
        self.assertTrue(row["accounting_ok"])
        self.assertEqual(result["requests_sent_total"], 1)
        self.assertEqual(result["requests_attempted_total"], 1)
        self.assertEqual(result["sends_total"], 1)
        self.assertEqual(result["responses_received_total"], 0)
        self.assertTrue(result["model_called"], "the request DID reach the transport")
        self.assertTrue(result["accounting_ok"])
        # the journal agrees, and there is no second attempt and no second model
        rows = self._rows(journal)
        self.assertEqual(len([r for r in rows if r.get("kind") == "upstream_request"]), 1)
        self.assertEqual([r for r in rows if r.get("kind") == "upstream_response"], [])
        self.assertEqual([r["error_code"] for r in row["attempts"]],
                         ["upstream_io_failure_no_retry"])
        self.assertEqual([r["error_type"] for r in row["attempts"]], ["ProxyBlocked"])
        self.assertEqual([r["sent"] for r in row["attempts"]], [1])
        self.assertEqual([r["response_received"] for r in row["attempts"]], [False])
        self.assertEqual(len([r for r in rows if r.get("kind") == "client_request"]), 1,
                         "a failed send must never be retried")
        self.assertEqual(result["models_not_run"], [MODELS[1]])

    def test_a_pre_send_refusal_is_zero_sends(self):
        """A local policy refusal never left the host, so it is NOT a send attempt."""
        # The transport's own output limit is below the frozen request's 2048, so the
        # proxy refuses the request BEFORE any pacing or sending.
        config = self._config(max_output_tokens=1024)
        result, journal = self._execute(config, prefix="refusal")
        row = result["models"][0]
        self.assertEqual(row["status"], self.screen.STATUS_TRANSPORT_REFUSED)
        self.assertEqual(row["stop_detail"], "output_limit_exceeded")
        self.assertEqual(row["requests_attempted"], 1, "it was attempted")
        self.assertEqual(row["sends"], 0, "but it was never sent")
        self.assertEqual(row["responses_received"], 0)
        self.assertEqual(row["provider_execution_state"],
                         self.screen.PROVIDER_EXECUTION_NOT_ATTEMPTED)
        self.assertTrue(row["accounting_ok"])
        self.assertEqual(result["requests_sent_total"], 0)
        self.assertEqual(result["sends_total"], 0)
        self.assertFalse(result["model_called"], "nothing reached the transport")
        rows = self._rows(journal)
        self.assertEqual([r for r in rows if r.get("kind") == "upstream_request"], [],
                         "a refused request must not appear as sent")
        self.assertTrue(any(r.get("kind") == "blocked" for r in rows),
                        "the refusal itself must stay in the journal as evidence")


class ScreeningProfileBoundaryTests(unittest.TestCase):
    """The frozen transport is not relaxed, and the screening set is exactly the pair."""

    def setUp(self):
        self.screen = _load_screen()

    def test_the_frozen_profiles_still_refuse_the_screening_models(self):
        for model in MODELS:
            with self.assertRaises(ValueError):
                px.CloudConfig(model=model, max_output_tokens=2048, max_request_bytes=2097152,
                               max_response_bytes=16777216, max_requests=6,
                               io_timeout_seconds=180, upstream_origin=px.ORIGIN,
                               profile=px.COMPAT_PROFILE).validate()
        frozen = px.CloudConfig(model=px.MODEL, max_output_tokens=2048,
                                max_request_bytes=2097152, max_response_bytes=16777216,
                                max_requests=6, io_timeout_seconds=180,
                                upstream_origin=px.ORIGIN, profile=px.COMPAT_PROFILE)
        body = json.dumps({"model": MODELS[0],
                           "messages": [{"role": "user", "content": "x"}],
                           "max_completion_tokens": 2048, "stream": False}).encode()
        with self.assertRaises(Exception):
            px.normalize_request(body, frozen)
        self.assertEqual(px.MODEL, "Qwen/Qwen3-30B-A3B-Instruct-2507")

    def test_the_screening_profile_accepts_only_the_authorised_pair(self):
        config, profile = self.screen.screen_config(TRANSPORT_CONFIG)
        self.assertEqual(profile.declared_models(), MODELS)
        self.assertEqual(profile.origin, px.ORIGIN)
        with self.assertRaises(Exception):
            px.normalize_request(json.dumps({
                "model": "Qwen/Qwen3-8B",
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 2048, "stream": False}).encode(), config)
        # a frozen profile is refused by the screening CLI even if a config names one
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "frozen.json"
            bad.write_text(json.dumps({"profile": px.COMPAT_PROFILE, "model": px.MODEL,
                                       "max_output_tokens": 2048,
                                       "max_request_bytes": 2097152,
                                       "max_response_bytes": 16777216, "max_requests": 6,
                                       "io_timeout_seconds": 180, "pace_seconds": 65}),
                           encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self.screen.screen_config(bad)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
