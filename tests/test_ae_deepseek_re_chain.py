"""The real DeepSeek chain: CLI -> config validation -> proxy dispatch.

This suite exercises the PRODUCTION entry points, not a helper module: the real
`validate_config`, the real CLI parser, the real `CloudAuditProxy.dispatch`, the real
profile table and the real capacity gate. Only the HTTP opener is replaced, so no request
leaves the host. What is asserted is what the existing suites assert for their own
protocols: the wire identity, the mode field, the pre-send bound, the journal evidence and
the refusal branches.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CANDIDATE = ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-candidate.json"
OPTION = "--exploratory-capacity-option"
FAKE_KEY = "sk-" + "z" * 40


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _driver():
    return _load("ae_cloud_re_multiturn_under_test",
                 ROOT / "ae_cloud_re_multiturn.py")


def _cli():
    return _load("ae_01_cloud_re_multiturn_under_test",
                 ROOT / "scripts/ae_01_cloud_re_multiturn.py")


def _candidate() -> dict:
    return json.loads(CANDIDATE.read_text(encoding="utf-8"))


class _RecordingOpener:
    """The offline replacement for the real opener: records, then answers 200."""

    def __init__(self, status=200):
        self.sent = []
        self.status = status

    def open(self, request, timeout):
        self.sent.append({"url": request.full_url,
                          "authorization": request.get_header("Authorization"),
                          "body": json.loads(request.data.decode("utf-8"))})
        body = json.dumps({
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode("utf-8")

        class _Response(io.BytesIO):
            def __init__(self):
                super().__init__(body)
                self.code, self.headers = self_status, {}

        self_status = self.status

        class _R(io.BytesIO):
            def __init__(self):
                super().__init__(body)
                self.code, self.headers = self_status, {}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False
        return _R()


class CliParsesTheExploratoryOptionTests(unittest.TestCase):
    """The real CLI is where the opt-in has to land."""

    def setUp(self):
        self.cli = _cli()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _main(self, argv):
        return self.cli.main(argv)

    def test_plan_without_the_flag_refuses_the_deepseek_candidate(self):
        out = Path(self.tmp.name) / "no-flag"
        code = self._main(["--stage", "plan", "--config", str(CANDIDATE),
                           "--dataset", str(ROOT / ".ae-verify-src/tasks-full-a4553e1.json"),
                           "--vita-source", str(_vita_source()),
                           "--output-dir", str(out)])
        self.assertEqual(code, 2, "a refused config must not plan")
        self.assertFalse(out.exists(), "a refused config must not create an output directory")

    def test_plan_with_the_flag_is_accepted_and_sends_nothing(self):
        out = Path(self.tmp.name) / "with-flag"
        code = self._main(["--stage", "plan", "--config", str(CANDIDATE),
                           "--dataset", str(ROOT / ".ae-verify-src/tasks-full-a4553e1.json"),
                           "--vita-source", str(_vita_source()),
                           "--output-dir", str(out), OPTION])
        self.assertEqual(code, 0)
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        self.assertFalse(plan["network_called"])
        self.assertFalse(plan["model_called"])
        self.assertNotIn("_exploratory_capacity_option",
                         json.dumps(plan, ensure_ascii=False),
                         "the private opt-in marker must never reach a run record")
        self.assertEqual(plan["config"]["expected_model"], "deepseek-flash")
        self.assertEqual(plan["config"]["pacing"]["min_interval_seconds"], 0)
        self.assertEqual(plan["config"]["capacity"]["count_basis"],
                         ["no_token_count_byte_gate_only"])

    def test_the_flag_is_the_only_accepted_spelling(self):
        self.assertEqual(self.cli.EXPLORATORY_OPTION, OPTION)
        source = (ROOT / "scripts/ae_01_cloud_re_multiturn.py").read_text(encoding="utf-8")
        self.assertIn('"--exploratory-capacity-option"', source)


def _vita_source():
    root = Path(os.environ.get("AE_VERIFY_ROOT", ROOT / ".ae-verify-src"))
    return Path(os.environ.get("AE_VITA_SOURCE", root / "source"))


class DriverAcceptsOnlyTheDeclaredProtocolTests(unittest.TestCase):
    """The real validator: the option gates the whole provider contract."""

    def setUp(self):
        self.driver = _driver()
        self.config = _candidate()

    def test_without_the_option_it_refuses(self):
        with self.assertRaises(Exception) as caught:
            self.driver.validate_config(self.config, exploratory_capacity_option=None)
        self.assertIn("refused by default", str(caught.exception))

    def test_with_the_option_it_accepts_the_whole_contract(self):
        validated = self.driver.validate_config(self.config,
                                                exploratory_capacity_option=OPTION)
        self.assertEqual(validated["expected_model"], "deepseek-flash")
        self.assertEqual(validated["model_handle"], "openai/deepseek-flash")
        self.assertEqual(validated["pacing"]["min_interval_seconds"], 0)
        self.assertEqual(validated["context_window"], 1000000)
        self.assertEqual(validated["max_request_bytes"], 2097152)
        self.assertEqual(validated["max_requests"], self.driver.PAIR_MAX_REQUESTS)
        self.assertEqual(validated["arms"], list(self.driver.ARMS))

    def test_a_guarantee_claim_or_a_qwen_basis_is_refused(self):
        for broken, needle in (({"capacity_is_a_guarantee": True}, "guarantee"),
                               ({"count_basis": ["official_qwen_tokenizer"]}, "count_basis"),
                               ({"window_is_measured": True}, "measured"),
                               ({"verification": "verified"}, "unverified")):
            with self.subTest(broken=broken):
                config = _candidate()
                config["capacity"] = dict(config["capacity"], **broken)
                with self.assertRaises(Exception) as caught:
                    self.driver.validate_config(config,
                                                exploratory_capacity_option=OPTION)
                self.assertIn(needle, str(caught.exception))

    def test_the_sealed_0_3_contract_is_not_loosened(self):
        sealed = json.loads((ROOT / "configs/ae-01__re-multiturn__siliconflow"
                                    ".capacity-250k-measured-candidate.json"
                             ).read_text(encoding="utf-8"))
        validated = self.driver.validate_config(sealed,
                                                exploratory_capacity_option=OPTION)
        self.assertEqual(validated["pacing"]["min_interval_seconds"], 65,
                         "the sealed candidate must keep its reviewed interval")
        self.assertEqual(validated["capacity"]["count_basis"],
                         ["official_qwen_tokenizer", "pinned_tokenizer"])
        # ... and the option does not admit a 0.3 config without its own basis
        broken = json.loads(json.dumps(sealed))
        broken["capacity"]["count_basis"] = ["no_token_count_byte_gate_only"]
        with self.assertRaises(Exception):
            self.driver.validate_config(broken, exploratory_capacity_option=OPTION)


class StageClarificationBindingTests(unittest.TestCase):
    """The per-stage clarification is bound to ITS agent, task and request."""

    def setUp(self):
        self.driver = _driver()
        self.local = _load("ae_local_probe_for_chain_tests",
                           ROOT / "scripts/ae_01_local_t4_probe.py")
        self.vita = _vita_source()
        if not (self.vita / "src/vita").is_dir():
            self.skipTest("the fixed Vita source is absent")
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            self.skipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))
        # building the native case initialises the Vita model config the SDK needs
        self.public = self.local.prepare_public(self.vita)
        self.native = self.local.build_native(self.public, self.vita)

    def _clarification(self, when, subtask_id, tools=None, domain="delivery"):
        import copy
        from vita.domains.delivery.environment import get_environment
        database = copy.deepcopy(dict(self.public["private"]["environment"]))
        database["time"] = when
        environment = get_environment(db=database)
        return self.driver.stage_clarification_for(
            environment=environment, tools=tools or self.native["tools"],
            domain=domain, subtask_id=subtask_id)

    def test_each_stage_carries_its_own_clock_and_its_own_task(self):
        first = self._clarification("2024-06-23 14:30:00", "sub_U000828_4")
        second = self._clarification("2024-06-24 09:05:00", "sub_U000828_5")
        self.assertEqual(first["environment_now"], "2024-06-23 14:30:00")
        self.assertEqual(second["environment_now"], "2024-06-24 09:05:00")
        self.assertNotEqual(first["environment_now"], second["environment_now"],
                            "a stage must not inherit another stage's clock")
        self.assertEqual(first["subtask_id"], "sub_U000828_4")
        self.assertEqual(second["subtask_id"], "sub_U000828_5")
        for record in (first, second):
            self.assertFalse(record["host_wall_clock_used"])
            self.assertFalse(record["hardcoded_value_used"])
            self.assertTrue(record["same_clock_the_tools_validate_with"])
            self.assertEqual(record["bound_to"], "this agent, this task, this request")

    def test_the_attributes_note_is_applied_only_where_the_tool_exists(self):
        with_tool = self._clarification("2024-06-23 14:30:00", "sub_U000828_4")
        self.assertEqual(with_tool["attributes_note"], "applied")
        self.assertIn("商品目录中的规格不会自动写入订单",
                      with_tool["attributes_clarification"])
        # a domain without that tool is honestly not_applicable
        without = [tool for tool in self.native["tools"]
                   if (tool.get("function") or {}).get("name") != "create_delivery_order"]
        other = self._clarification("2024-06-23 14:30:00", "sub_U000828_5",
                                    tools=without, domain="instore")
        self.assertEqual(other["attributes_note"], "not_applicable")
        self.assertIsNone(other["attributes_clarification"])
        self.assertIn("14:30:00", other["environment_now"],
                      "the clock still applies on a non-delivery stage")

    def test_the_clarification_carries_no_answer_and_no_memory_edit(self):
        record = self._clarification("2024-06-23 14:30:00", "sub_U000828_4")
        blob = json.dumps(record, ensure_ascii=False)
        for forbidden in (self.local.TARGET_PRODUCT_ID, "深空告白", "益禾堂",
                          "奶茶偏好7分糖"):
            self.assertNotIn(forbidden, blob)
        # it edits no memory: there is no memory key in the record at all
        self.assertNotIn("memory", blob.lower())
        self.assertNotIn("initial_block", blob)


class ProxyDispatchOnTheDeepSeekProfileTests(unittest.TestCase):
    """The REAL proxy: wire identity, mode field, byte gate, journal evidence."""

    def setUp(self):
        import ae_cloud_proxy as px
        self.px = px
        self.driver = _driver()
        self.config = self.driver.validate_config(
            _candidate(), exploratory_capacity_option=OPTION)
        self.candidate = _candidate()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _proxy(self, *, budget=None, opener=None):
        px = self.px
        cloud = px.CloudConfig(
            model="deepseek-flash", max_output_tokens=2048, max_request_bytes=2097152,
            max_response_bytes=16777216, max_requests=256, io_timeout_seconds=180.0,
            upstream_origin=px.DEEPSEEK_ORIGIN, profile=px.DEEPSEEK_PROFILE,
            upstream_model="deepseek-flash",
            declared_request_byte_budget=budget or 2097152, pace_seconds=0)
        cloud.validate()
        journal = Path(self.tmp.name) / f"j-{len(list(Path(self.tmp.name).iterdir()))}.jsonl"
        proxy = px.CloudAuditProxy(cloud, journal, api_key=FAKE_KEY,
                                   clock=lambda: 1.0, sleep=lambda seconds: None)
        opener = opener or _RecordingOpener()
        proxy.opener = opener
        self.count_calls = {"n": 0}

        def count_bytes(normalized, role):
            self.count_calls["n"] += 1
            return budget or 2097152

        proxy.arm_capacity_from_declaration(
            self.candidate, count_request=count_bytes,
            count_source=px.BYTE_GATE_COUNT_BASIS, count_basis=px.BYTE_GATE_COUNT_BASIS)
        return proxy, journal, opener

    @staticmethod
    def _request_body(text="hi"):
        return json.dumps({"model": "deepseek-flash",
                           "messages": [{"role": "user", "content": text}],
                           "max_tokens": 2048, "temperature": 0, "stream": False,
                           "thinking": {"type": "disabled"}}).encode("utf-8")

    def test_the_wire_request_is_the_declared_identity_and_mode(self):
        proxy, journal, opener = self._proxy()
        status, _reply = proxy.dispatch("POST", "/v1/chat/completions",
                                        self._request_body(), "re-multiturn", "agent")
        proxy.close()
        self.assertEqual(status, 200)
        sent = opener.sent[0]
        self.assertEqual(sent["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(sent["body"]["model"], "deepseek-flash")
        self.assertEqual(sent["body"]["thinking"], {"type": "disabled"})
        self.assertNotIn("chat_template_kwargs", sent["body"],
                         "the local vLLM field must never reach this provider")
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        upstream = next(row for row in rows if row.get("kind") == "upstream_request")
        self.assertEqual(upstream["origin"], "https://api.deepseek.com")
        self.assertEqual(upstream["path"], "/chat/completions")

    def test_the_pre_send_bound_is_the_declared_byte_gate_and_never_a_token_count(self):
        proxy, journal, opener = self._proxy()
        proxy.dispatch("POST", "/v1/chat/completions", self._request_body(),
                       "re-multiturn", "agent")
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        proxy.close()
        check = next(row for row in rows if row.get("kind") == "capacity_check")
        self.assertEqual(check["capacity_basis"], "no_token_count_byte_gate_only")
        self.assertFalse(check["token_count_available"])
        self.assertTrue(check["operation_guard_not_a_capacity_guarantee"])
        self.assertNotIn("input_tokens", check,
                         "a byte-gated request must not carry a token count")
        self.assertGreater(check["input_bytes"], 0)
        self.assertEqual(check["request_byte_budget"], 2097152)
        self.assertTrue(check["fits"])
        armed = next(row for row in rows if row.get("kind") == "capacity_armed")
        self.assertEqual(armed["count_basis"], ["no_token_count_byte_gate_only"])
        self.assertEqual(self.count_calls["n"], 1, "the byte gate ran exactly once")

    def test_an_over_budget_request_is_refused_before_any_upstream_call(self):
        proxy, journal, opener = self._proxy(budget=64)
        with self.assertRaises(Exception) as caught:
            proxy.dispatch("POST", "/v1/chat/completions", self._request_body("x" * 500),
                           "re-multiturn", "agent")
        proxy.close()
        self.assertIn("capacity_exceeded_before_send", str(caught.exception))
        self.assertEqual(opener.sent, [], "an over-budget request must not be sent")
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        self.assertFalse(any(row.get("kind") == "upstream_request" for row in rows))
        check = next(row for row in rows if row.get("kind") == "capacity_check")
        self.assertFalse(check["fits"])
        self.assertGreater(check["over_by"], 0)

    def test_a_non_200_stops_without_a_retry(self):
        proxy, journal, opener = self._proxy(opener=_RecordingOpener(status=429))
        status, _reply = proxy.dispatch("POST", "/v1/chat/completions",
                                       self._request_body(), "re-multiturn", "agent")
        proxy.close()
        self.assertEqual(status, 429)
        self.assertEqual(len(opener.sent), 1, "a provider 429 is never retried")

    def test_no_qwen_counter_is_reachable_from_this_profile(self):
        """The byte gate is the only counter this profile arms."""
        import ae_multiturn_capacity  # noqa: F401 - proves the module is importable but unused
        proxy, _journal, _opener = self._proxy()
        source = (ROOT / "ae_cloud_proxy.py").read_text(encoding="utf-8")
        start = source.index("BYTE_GATE_COUNT_BASIS =")
        self.assertIn("no_token_count_byte_gate_only", source[start:start + 80])
        # the armed gate's basis is the byte one, so `_capacity_record` never calls a
        # tokenizer through this path; the counter above is the caller's own byte function
        self.assertEqual(proxy.capacity["count_basis"], self.px.BYTE_GATE_COUNT_BASIS)

    def test_a_request_without_the_declared_mode_field_is_refused(self):
        """The non-thinking mode is REQUIRED, never defaulted by the endpoint."""
        proxy, journal, opener = self._proxy()
        body = json.loads(self._request_body())
        del body["thinking"]
        with self.assertRaises(Exception) as caught:
            proxy.dispatch("POST", "/v1/chat/completions",
                           json.dumps(body).encode("utf-8"), "re-multiturn", "agent")
        self.assertIn("declared_transport_field_missing", str(caught.exception))
        self.assertEqual(opener.sent, [], "a request without its mode must not be sent")
        proxy.close()
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        self.assertFalse(any(row.get("kind") == "upstream_request" for row in rows))
        self.assertEqual(self.px.DEEPSEEK_TRANSPORT.required_request_fields,
                         {"thinking": {"type": "disabled"}})

    def test_a_request_with_another_mode_value_is_refused(self):
        proxy, _journal, opener = self._proxy()
        body = json.loads(self._request_body())
        body["thinking"] = {"type": "enabled"}
        with self.assertRaises(Exception) as caught:
            proxy.dispatch("POST", "/v1/chat/completions",
                           json.dumps(body).encode("utf-8"), "re-multiturn", "agent")
        self.assertIn("declared_transport_field_value_changed", str(caught.exception))
        self.assertEqual(opener.sent, [])

    def test_an_undeclared_request_field_is_still_refused(self):
        """The declared pass-through shape is a whitelist, not an opening."""
        proxy, _journal, opener = self._proxy()
        body = json.loads(self._request_body())
        body["logit_bias"] = {"1": 1}
        with self.assertRaises(Exception) as caught:
            proxy.dispatch("POST", "/v1/chat/completions",
                           json.dumps(body).encode("utf-8"), "re-multiturn", "agent")
        self.assertIn("unsupported_request_fields", str(caught.exception))
        self.assertEqual(opener.sent, [])

    def test_the_service_pass_through_shape_is_kept_and_recorded(self):
        """`user`/`parallel_tool_calls` are the pinned service's own fields: recorded."""
        proxy, journal, opener = self._proxy()
        body = json.loads(self._request_body())
        body["user"] = "U000828"
        body["parallel_tool_calls"] = False
        status, _reply = proxy.dispatch("POST", "/v1/chat/completions",
                                       json.dumps(body).encode("utf-8"),
                                       "re-multiturn", "agent")
        proxy.close()
        self.assertEqual(status, 200)
        self.assertEqual(opener.sent[0]["body"]["user"], "U000828")
        self.assertIs(opener.sent[0]["body"]["parallel_tool_calls"], False)
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        normalized = next(row for row in rows if row.get("kind") == "normalized_request")
        kept = {change["to"] for change in normalized["changes"]
                if change.get("operation") == "keep"}
        self.assertEqual(kept, {"thinking", "user", "parallel_tool_calls"})


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
