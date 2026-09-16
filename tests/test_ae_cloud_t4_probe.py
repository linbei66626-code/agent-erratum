"""Cloud single-t4 screening entry: the offline acceptance.

The endpoint is a scripted loopback opener, so no network and no model call happens. What
is real: the DECLARED config contract, the cloud transport's request construction and
byte-budget gate, the shared t4 flow (r3's), the native environment and 19 native tools,
the native user conversation, and the deterministic private diagnosis.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

MODEL = "Qwen/Qwen3.5-9B"
CLOUD_BASE = "https://api.example-endpoint.test"
CLOUD_MODEL = "declared-model-v1"
#: A synthetic key: it must never reach an artifact.
FAKE_KEY = "sk-synthetic-DO-NOT-PERSIST-3f9a"
TARGET_PRODUCT_ID = "S17791041622763865_P00011"
WORK_ADDRESS = "甘肃省兰州市安宁区安宁西路地五大道88号"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cloud():
    return _load("ae_01_cloud_t4_probe_under_test",
                 ROOT / "scripts/ae_01_cloud_t4_probe.py")


def _local():
    return _load("ae_local_probe_for_cloud_tests", ROOT / "scripts/ae_01_local_t4_probe.py")


def _vita_source():
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


def _chat_reply(tool_calls=None, content=None, finish_reason="tool_calls"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
            for name, arguments, call_id in tool_calls]
    return {"id": "gen", "object": "chat.completion", "model": CLOUD_MODEL,
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


def declared_config(**overrides):
    document = {
        "schema_version": "ae-cloud-t4-probe-0.1",
        "api": {"base_url": CLOUD_BASE, "chat_path": "/chat/completions",
                "models_path": "/models", "tokenize_path": None},
        "wire_model": CLOUD_MODEL,
        "mode": {"name": "declared-non-thinking",
                 "payload": {"thinking": {"type": "disabled"}},
                 "source": "the endpoint's own documentation, declared by the operator"},
        "identity": {"probe": "required",
                     "evidence": "the endpoint's /models listing names the declared model"},
        "capacity": {"token_counting_available": False, "max_request_bytes": 200000},
    }
    document.update(overrides)
    return document


def write_config(directory, **overrides) -> Path:
    path = Path(directory) / "cloud-identity.json"
    path.write_text(json.dumps(declared_config(**overrides), ensure_ascii=False),
                    encoding="utf-8")
    return path


class _CloudProvider:
    """A scripted cloud endpoint: model listing + chat completions, no network."""

    def __init__(self, *, models=(CLOUD_MODEL,), chat_statuses=None, mode_rejects=False,
                 scripts=None):
        self.models = list(models)
        self.chat_statuses = chat_statuses or {}
        self.mode_rejects = mode_rejects
        self.scripts = scripts
        self.requests = []
        self.get_calls = []
        self.chat_calls = []

    def open(self, request, timeout):
        path = request.full_url[len(CLOUD_BASE):]
        if request.data is None:
            self.get_calls.append({"url": request.full_url,
                                   "authorization": request.get_header("Authorization")})
            if path == "/models":
                return _reply({"object": "list",
                               "data": [{"id": name} for name in self.models]})
            return _reply({"error": {"message": "no such route"}}, 404)
        body = json.loads(request.data.decode("utf-8"))
        self.chat_calls.append(body)
        self.requests.append({"url": request.full_url, "body": body,
                              "authorization": request.get_header("Authorization")})
        index = len(self.chat_calls)
        status = self.chat_statuses.get(index, 200)
        if self.mode_rejects and "thinking" in body and index == 1:
            return _reply({"error": {"message": "unknown field: thinking",
                                     "type": "invalid_request_error"}}, 400)
        if status != 200:
            return _reply({"error": {"message": "scripted failure", "code": status}}, status)
        if self.scripts is not None:
            return _reply(self.scripts(index, body, self))
        return _reply(self._agent_reply(body))

    # ------------------------------------------------------------------ agent script

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

    def _product_id_from(self, body):
        import re
        for message in reversed(body.get("messages") or []):
            if message.get("role") != "tool":
                continue
            match = re.search(r"product_id=(S\d+_P\d+)", message.get("content") or "")
            if match:
                return match.group(1)
        return None

    def _store_id_from(self, body):
        import re
        for message in reversed(body.get("messages") or []):
            if message.get("role") != "tool":
                continue
            match = re.search(r"store_id=(S\d+_S\d+)", message.get("content") or "")
            if match:
                return match.group(1)
        return None

    def _has_searched(self, body):
        return any("product_id=" in (m.get("content") or "")
                   for m in body.get("messages") or [] if m.get("role") == "tool")

    def _has_created(self, body):
        return any("Order(order_id:" in (m.get("content") or "")
                   for m in body.get("messages") or [] if m.get("role") == "tool")

    def _agent_reply(self, body):
        if not self._has_searched(body):
            return _chat_reply([("delivery_product_search_recommand",
                                 {"keywords": ["恋与深空", "奶茶"]}, "c1")])
        if self._has_created(body):
            order_id = self._order_id_from(body)
            if order_id:
                return _chat_reply([("pay_delivery_order", {"order_id": order_id}, "c3")])
            return _chat_reply(content="下单失败。", finish_reason="stop")
        return _chat_reply([("create_delivery_order", {
            "user_id": "U000828",
            "store_id": self._store_id_from(body) or "S17791041622763865_S00006",
            "product_ids": [self._product_id_from(body) or TARGET_PRODUCT_ID],
            "product_cnts": [1], "address": WORK_ADDRESS,
            "dispatch_time": "2024-06-23 18:00:00", "attributes": ["7分糖"]}, "c2")])


class _FailingConnect:
    def __init__(self, error=None):
        self.error = error or TimeoutError("scripted connect timeout")

    def open(self, request, timeout):
        raise self.error


class CloudConfigContractTests(unittest.TestCase):
    """The declared identity/mode/budget contract refuses anything it cannot trust."""

    def setUp(self):
        self.cloud = _cloud()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_declared_config_is_accepted(self):
        config = self.cloud.load_config(write_config(self.tmp.name))
        self.assertEqual(config["api"]["base_url"], CLOUD_BASE)
        self.assertEqual(config["wire_model"], CLOUD_MODEL)
        self.assertEqual(config["mode"]["payload"], {"thinking": {"type": "disabled"}})
        self.assertFalse(config["capacity"]["token_counting_available"])
        self.assertFalse(config["capacity"]["declared_context_window_is_verified"])

    def test_placeholder_identity_is_refused(self):
        template = ROOT / "configs/ae-01__cloud-t4__declared-identity.template.json"
        with self.assertRaises(self.cloud.ConfigRejected) as caught:
            self.cloud.load_config(template)
        self.assertIn("placeholder", str(caught.exception))

    def test_the_local_vllm_field_cannot_be_reused(self):
        path = write_config(self.tmp.name, mode={
            "name": "vllm", "payload": {"chat_template_kwargs": {"enable_thinking": False}},
            "source": "docs"})
        with self.assertRaises(self.cloud.ConfigRejected) as caught:
            self.cloud.load_config(path)
        self.assertIn("vLLM", str(caught.exception))

    def test_a_remote_plain_http_endpoint_is_refused(self):
        path = write_config(self.tmp.name, api={
            "base_url": "http://api.example-endpoint.test", "chat_path": "/chat/completions",
            "models_path": "/models", "tokenize_path": None})
        with self.assertRaises(self.cloud.ConfigRejected) as caught:
            self.cloud.load_config(path)
        self.assertIn("https", str(caught.exception))

    def test_a_token_capacity_claim_is_refused(self):
        path = write_config(self.tmp.name, capacity={
            "token_counting_available": True, "max_request_bytes": 1000})
        with self.assertRaises(self.cloud.ConfigRejected):
            self.cloud.load_config(path)

    def test_a_missing_byte_budget_is_refused(self):
        path = write_config(self.tmp.name, capacity={"token_counting_available": False})
        with self.assertRaises(self.cloud.ConfigRejected) as caught:
            self.cloud.load_config(path)
        self.assertIn("max_request_bytes", str(caught.exception))

    def test_the_key_file_must_be_0600_and_is_never_returned_elsewhere(self):
        path = Path(self.tmp.name) / "k"
        path.write_text(FAKE_KEY, encoding="utf-8")
        path.chmod(0o600)
        self.assertEqual(self.cloud.read_key(path), FAKE_KEY)
        path.chmod(0o644)
        with self.assertRaises(self.cloud.ConfigRejected) as caught:
            self.cloud.read_key(path)
        self.assertIn("0600", str(caught.exception))


class CloudRequestConstructionTests(unittest.TestCase):
    """The real HTTP shape: declared model, declared mode, redacted authorization."""

    def setUp(self):
        self.cloud = _cloud()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = self.cloud.load_config(write_config(self.tmp.name))

    def _transport(self, provider):
        return self.cloud.CloudTransport(self.config, api_key=FAKE_KEY, opener=provider,
                                         max_inference_posts=16)

    def test_the_cloud_body_uses_the_declared_identity_and_mode(self):
        provider = _CloudProvider()
        transport = self._transport(provider)
        status, raw, failure, gate = transport.send(
            [{"role": "user", "content": "hi"}], None, role="agent", max_tokens=2048)
        self.assertEqual(status, 200)
        body = provider.chat_calls[0]
        self.assertEqual(body["model"], CLOUD_MODEL)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("chat_template_kwargs", body)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 2048)
        self.assertEqual(gate["capacity_basis"], "declared_request_byte_budget")
        self.assertIsNone(gate["count"], "a cloud endpoint must not be credited a token count")
        self.assertTrue(gate["fits"])

    def test_auth_is_present_on_the_wire_and_redacted_in_the_record(self):
        provider = _CloudProvider()
        transport = self._transport(provider)
        transport.read_model_listing()
        transport.send([{"role": "user", "content": "hi"}], None, role="agent",
                       max_tokens=2048)
        # on the wire the header IS there ...
        self.assertEqual(provider.chat_calls and
                         provider.requests[0]["authorization"], "Bearer " + FAKE_KEY)
        # ... but the journal never keeps it
        for row in transport.journal:
            self.assertNotIn(FAKE_KEY, json.dumps(row, ensure_ascii=False))
            self.assertTrue(row["request"]["authorization_header_redacted"])

    def test_the_declared_byte_budget_is_the_gate(self):
        provider = _CloudProvider()
        tiny = dict(self.config)
        tiny["capacity"] = dict(self.config["capacity"], max_request_bytes=10)
        transport = self.cloud.CloudTransport(tiny, api_key=FAKE_KEY, opener=provider,
                                              max_inference_posts=16)
        with self.assertRaises(Exception) as caught:
            transport.send([{"role": "user", "content": "x" * 100}], None, role="agent",
                           max_tokens=2048)
        self.assertIn("request_byte_budget_exhausted", str(caught.exception))
        self.assertEqual(provider.chat_calls, [], "nothing may be sent over budget")

    def test_a_rejected_mode_field_stops_without_a_silent_fallback(self):
        provider = _CloudProvider(mode_rejects=True)
        transport = self._transport(provider)
        status, raw, failure, gate = transport.send(
            [{"role": "user", "content": "hi"}], None, role="agent", max_tokens=2048)
        self.assertEqual(status, 400)
        self.assertTrue(transport.mode_was_rejected_by_the_service)
        self.assertEqual(len(provider.chat_calls), 1, "a rejected mode is never retried")
        self.assertEqual(provider.chat_calls[0]["thinking"], {"type": "disabled"})
        self.assertFalse(transport.descriptor()["mode_was_verified_against_documentation"])

    def test_401_429_and_a_timeout_each_stop_after_one_attempt(self):
        for status in (401, 429):
            with self.subTest(status=status):
                provider = _CloudProvider(chat_statuses={1: status})
                transport = self._transport(provider)
                got, _raw, _failure, _gate = transport.send(
                    [{"role": "user", "content": "hi"}], None, role="agent", max_tokens=2048)
                self.assertEqual(got, status)
                self.assertEqual(len(provider.chat_calls), 1)
        provider = _FailingConnect()
        transport = self._transport(provider)
        status, raw, failure, gate = transport.send(
            [{"role": "user", "content": "hi"}], None, role="agent", max_tokens=2048)
        self.assertIsNone(status)
        self.assertIsNotNone(failure)
        self.assertEqual(transport.attempted, 1, "a timeout is not retried")


class CloudFlowTests(unittest.TestCase):
    """The full flow over the cloud transport, with the REAL native tools and diagnosis."""

    def setUp(self):
        self.cloud = _cloud()
        self.local = _local()
        self.vita = _vita_source()
        if not (self.vita / "src/vita").is_dir():
            self.skipTest("the fixed Vita source is absent")
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            self.skipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = self.cloud.load_config(write_config(self.tmp.name))

    def _run(self, provider, name="run"):
        out = Path(self.tmp.name) / name
        transport = self.cloud.CloudTransport(self.config, api_key=FAKE_KEY, opener=provider,
                                              max_inference_posts=16)
        result = self.cloud.run(config=self.config, vita_source=self.vita, key=FAKE_KEY,
                                output_dir=out, transport=transport)
        return result, out

    def test_a_real_create_and_pay_over_the_cloud_transport_passes(self):
        provider = _CloudProvider()
        result, out = self._run(provider)
        self.assertEqual(result["status"], self.local.STATUS_PASSED,
                         {k: result.get(k) for k in ("status", "stop_reason", "errors")})
        self.assertTrue(result["oracle_passed"])
        self.assertTrue(result["task_success"])
        # the query really went through the real 19-tool native environment
        self.assertIn(TARGET_PRODUCT_ID, result["tool_returns"][0]["raw_text"])
        # every agent request used the declared identity and mode, never the local field
        for body in provider.chat_calls:
            self.assertEqual(body["model"], CLOUD_MODEL)
            self.assertNotIn("chat_template_kwargs", body)
            if "tools" in body:
                self.assertEqual(len(body["tools"]), 19)
                self.assertEqual(body["thinking"], {"type": "disabled"})
        # the private answer only arrived through the real search return
        self.assertNotIn(TARGET_PRODUCT_ID,
                         json.dumps(provider.chat_calls[0], ensure_ascii=False))
        # capacity honesty
        self.assertIsNone(result["service_window_tokens"])
        self.assertFalse(result["capacity_is_a_guarantee"])
        self.assertFalse(result["token_counting_available"])
        self.assertEqual(result["capacity_basis"], "declared_request_byte_budget")
        self.assertGreater(result["largest_request_bytes"], 0)
        self.assertLessEqual(result["largest_request_bytes"],
                             self.config["capacity"]["max_request_bytes"])
        # the key never reached an artifact
        self.assertTrue(result["secret_hygiene"]["key_absent_from_artifacts"])
        for name in ("plan.json", "preflight.json", "case.json", "initial-database.json",
                     "final-database.json", "result.json", "tool-returns.json",
                     "user-simulator.json"):
            self.assertTrue((out / name).is_file(), name)
        self.assertNotIn(FAKE_KEY, (out / "result.json").read_text(encoding="utf-8"))

    def test_the_agent_and_user_exits_share_one_budget(self):
        def asking(index, body, provider):
            return _chat_reply(content=f"请问送到哪里？(第{index}次)", finish_reason="stop")
        provider = _CloudProvider(scripts=asking)
        result, _out = self._run(provider, name="asking")
        self.assertEqual(result["status"], self.local.STATUS_INCOMPLETE)
        self.assertEqual(result["stop_reason"], "user_exchange_budget_exhausted")
        self.assertEqual(result["user_exchanges"], self.local.AUX_MAX_REQUESTS)
        self.assertEqual(result["requests_by_role"]["user"], self.local.AUX_MAX_REQUESTS)
        self.assertLessEqual(result["total_model_posts"], 16)
        self.assertTrue(result["total_model_posts_within_budget"])
        # the native user conversation is the r3 one, driven through the cloud transport
        self.assertTrue(result["native_user_simulator"]["uses_native_user_simulator"])
        self.assertTrue(result["native_user_simulator"]["drives_native_generate_next_message"])
        user_bodies = [b for b in provider.chat_calls if "tools" not in b]
        self.assertEqual(len(user_bodies), self.local.AUX_MAX_REQUESTS)
        for body in user_bodies:
            self.assertNotIn("chat_template_kwargs", body)
            self.assertEqual(body["model"], CLOUD_MODEL)

    def test_the_original_t4_prompt_and_tools_are_unchanged(self):
        provider = _CloudProvider()
        self._run(provider)
        public = self.local.prepare_public(self.vita)
        native = self.local.build_native(public, self.vita)
        first = provider.chat_calls[0]
        self.assertEqual(first["messages"][0]["content"],
                         self.local.agent_system_message(native, public))
        self.assertEqual(first["messages"][1]["content"],
                         self.local.task_user_message(public))
        self.assertEqual(len(first["tools"]), 19)
        names = sorted(t["function"]["name"] for t in first["tools"])
        self.assertEqual(names, sorted(binding.schema["name"]
                                       for binding in native["bindings"].values()))

    def test_401_stops_the_run_and_keeps_the_evidence(self):
        provider = _CloudProvider(chat_statuses={1: 401})
        result, out = self._run(provider, name="unauthorized")
        self.assertEqual(result["status"], self.cloud.STATUS_INVALID)
        self.assertEqual(result["stop_reason"], "provider_http_401")
        self.assertEqual(result["total_model_posts"], 1, "a 401 is never retried")
        self.assertEqual(len(provider.chat_calls), 1)
        self.assertIsNone(result["task_success"])
        self.assertTrue((out / "result.json").is_file())

    def test_a_timeout_stops_the_run_after_one_attempt(self):
        # Only the CHAT call fails: the identity listing succeeds, so the timeout is the
        # first inference attempt and must stop the run after exactly one try.
        provider = _CloudProvider(chat_statuses={1: None})
        provider.chat_statuses = {}

        class _TimeoutChats(_CloudProvider):
            def open(self, request, timeout):
                if request.data is not None:
                    self.chat_calls.append(json.loads(request.data.decode("utf-8")))
                    raise TimeoutError("scripted chat timeout")
                return super().open(request, timeout)

        result, _out = self._run(_TimeoutChats(), name="timeout")
        self.assertEqual(result["stop_reason"], "request_not_answered_no_retry")
        # exactly ONE inference attempt: the preflight identity read is not an inference call
        self.assertEqual(result["requests_by_role"]["agent"], 1)
        self.assertEqual(result["requests_by_role"]["user"], 0)
        self.assertEqual(result["total_model_posts"], 1)
        self.assertEqual(result["responses_received"], 1, "only the identity read answered")
        self.assertIsNone(result["task_success"])

    def test_a_length_truncation_stops_the_run(self):
        provider = _CloudProvider(scripts=lambda i, b, p: _chat_reply(
            content="...", finish_reason="length"))
        result, _out = self._run(provider, name="length")
        self.assertEqual(result["status"], self.local.STATUS_LENGTH)
        self.assertEqual(result["stop_reason"], "finish_reason_length")
        self.assertEqual(result["total_model_posts"], 1)

    def test_preflight_blocks_when_the_declared_model_is_not_listed(self):
        provider = _CloudProvider(models=("some-other-model",))
        out = Path(self.tmp.name) / "identity"
        out.mkdir()
        report = self.cloud.preflight(self.config, key=FAKE_KEY,
                                      transport=self.cloud.CloudTransport(
                                          self.config, api_key=FAKE_KEY, opener=provider))
        self.assertEqual(report["status"], self.cloud.STATUS_PREFLIGHT_BLOCKED)
        self.assertIn("declared_wire_model_not_listed", report["missing"])
        self.assertFalse(report["model_inference_called"])
        self.assertEqual(provider.chat_calls, [], "preflight must never infer")


class CloudCliTests(unittest.TestCase):
    def setUp(self):
        self.cloud = _cloud()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_plan_needs_no_key_and_sends_nothing(self):
        config = write_config(self.tmp.name)
        out = Path(self.tmp.name) / "plan"
        code = self.cloud.main(["--stage", "plan", "--config", str(config),
                                "--vita-source", str(_vita_source()),
                                "--output-dir", str(out),
                                "--key-file", str(Path(self.tmp.name) / "absent.key")])
        self.assertEqual(code, 0)
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        self.assertFalse(plan["network_called"])
        self.assertFalse(plan["model_called"])
        self.assertFalse(plan["api_identity"]["third_party_gateway_assumed_official"])
        self.assertFalse(plan["reasoning_mode"]["verified_by_this_probe"])
        self.assertFalse(plan["capacity"]["token_counting_available"])
        self.assertFalse(plan["capacity"]["bytes_over_four_token_estimate_used"])
        self.assertFalse(plan["scope"]["post_hoc_specification_reminder_used"])
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["plan.json"])

    def test_run_without_a_key_file_is_invalid_and_sends_nothing(self):
        config = write_config(self.tmp.name)
        out = Path(self.tmp.name) / "nokey"
        code = self.cloud.main(["--stage", "run", "--config", str(config),
                                "--vita-source", str(_vita_source()),
                                "--output-dir", str(out),
                                "--key-file", str(Path(self.tmp.name) / "absent.key")])
        self.assertEqual(code, 2)
        self.assertFalse((out / "result.json").exists())

    def test_the_local_entry_still_refuses_a_public_origin(self):
        local = _local()
        for bad in ("https://api.example-endpoint.test", "http://10.0.0.5:8001",
                    "http://127.0.0.1:8001/v1/chat/completions"):
            with self.assertRaises(ValueError, msg=bad):
                local.validate_local_endpoint(bad)
        self.assertEqual(local.validate_local_endpoint("http://127.0.0.1:8001"),
                         "http://127.0.0.1:8001")


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
