"""Cloud fixtures are invented responses. No real provider/API key is used."""
import base64
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from ae_cloud_proxy import (CloudAuditProxy, CloudConfig, MODEL, ORIGIN,
                            normalize_request, read_private_key, response_summary)
from ae_cloud_audit import audit_cloud_journal
from ae_model_proxy import ProxyBlocked, make_server

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-fixture-private-not-real-12345"


class Reply(io.BytesIO):
    def __init__(self, raw, status=200, trace="fixture-trace"):
        super().__init__(raw)
        self.code, self.headers = status, {"x-siliconcloud-trace-id": trace}


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "capture.private.jsonl"
        self.config = CloudConfig(MODEL, 128, 10000, 10000, 6, .3)
        self.body = {"model": MODEL, "messages": [{"role": "user", "content": "测试：别改我的信息"}],
                     "max_tokens": 128, "stream": False, "temperature": 0,
                     "tools": [{"type": "function", "function": {"name": "read_probe", "parameters": {"type": "object"}}}],
                     "tool_choice": "auto"}
        self.reply = {"model": MODEL, "id": "fixture", "system_fingerprint": "fixture-revision",
                      "choices": [{"index": 0, "finish_reason": "stop",
                                   "message": {"role": "assistant", "content": "fixture, not a model"}}],
                      "usage": {"prompt_tokens": 23, "completion_tokens": 5, "total_tokens": 28}}
        self.raw_reply = json.dumps(self.reply, ensure_ascii=False, indent=2).encode()
        self.sent = []
        self.status = 200
        self.trace = "fixture-trace"
        self.failure = None
        self.proxy = CloudAuditProxy(self.config, self.log, api_key=KEY)
        self.addCleanup(self.proxy.close)
        fixture = self
        class Opener:
            def open(self, request, timeout):
                fixture.sent.append(request)
                rec = fixture.records()[-1]
                fixture.assertEqual(rec["kind"], "upstream_request")
                fixture.assertEqual(base64.b64decode(rec["body_base64"]), request.data or b"")
                fixture.assertEqual(request.full_url, ORIGIN + rec["path"])
                fixture.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
                if fixture.failure:
                    raise fixture.failure
                return Reply(fixture.raw_reply, fixture.status, fixture.trace)
        self.proxy.opener = Opener()

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def send(self, body=None):
        raw = json.dumps(self.body if body is None else body, ensure_ascii=False, indent=2).encode()
        return self.proxy.dispatch("POST", "/v1/chat/completions", raw, "transport/probe", "fixture")

    def audit(self):
        self.proxy.close()
        return audit_cloud_journal(self.log)

    def test_exact_body_response_and_unknown_token_boundary(self):
        status, raw = self.send()
        self.assertEqual((status, raw), (200, self.raw_reply))
        self.assertEqual(self.sent[0].data, json.dumps(self.body, ensure_ascii=False, indent=2).encode())
        self.assertEqual(len(self.sent), 1)  # No /tokenize call.
        rec = self.records()[-1]
        self.assertIsNone(rec["token_ids"])
        self.assertIsNone(rec["preflight_prompt_tokens"])
        self.assertIsNone(rec["tokenize_prompt_usage_agreement"])
        self.assertFalse(rec["kv_reuse_verified"])
        self.assertEqual(self.records()[-2]["trace_id"], "fixture-trace")
        self.assertNotIn(KEY, self.log.read_text())
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o600)
        result = self.audit()
        self.assertTrue(result["transport_capture_checked"], result)
        self.assertIsNone(result["scientific_result"])
        self.assertIsNone(result["task_input_audit_passed"])

    def test_output_rename_is_only_change_and_audited(self):
        body = dict(self.body)
        body["max_completion_tokens"] = body.pop("max_tokens")
        self.send(body)
        self.assertEqual(json.loads(self.sent[0].data), self.body)
        event = self.records()[2]
        self.assertEqual(event["changes"][0]["from"], "max_completion_tokens")
        self.assertTrue(self.audit()["transport_capture_checked"])

    def test_conflicting_limits_and_unknown_parameters_rejected(self):
        variants = [{"seed": 300}, {"parallel_tool_calls": False}, {"chat_template_kwargs": {}},
                    {"max_completion_tokens": 128}, {"stream": True}, {"stream": 0},
                    {"n": True}, {"n": 2}, {"model": "other"}, {"max_tokens": 129},
                    {"max_tokens": True}, {"enable_thinking": True},
                    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "x"}]}]}]
        for extra in variants:
            with self.subTest(extra=extra), self.assertRaises(ProxyBlocked):
                normalize_request(json.dumps(dict(self.body, **extra)).encode(), self.config)
        self.assertEqual(self.sent, [])

    def test_no_silent_prompt_shortening_on_large_input(self):
        self.proxy.config = replace(self.config, max_request_bytes=2)
        with self.assertRaises(ProxyBlocked):
            self.send()
        self.assertEqual(self.sent, [])

    def test_http_rate_limit_preserved_stops_without_retry(self):
        self.status = 429
        self.raw_reply = b'{"message":"TPM limit reached"}'
        self.assertEqual(self.send(), (429, self.raw_reply))
        with self.assertRaises(ProxyBlocked):
            self.send()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.audit()["transport_capture_checked"])

    def test_redirect_is_never_followed(self):
        self.status = 302
        with self.assertRaisesRegex(ProxyBlocked, "redirect"):
            self.send()
        self.assertEqual(len(self.sent), 1)

    def test_timeout_exception_text_not_logged_or_retried(self):
        self.failure = TimeoutError(KEY)
        with self.assertRaises(ProxyBlocked):
            self.send()
        with self.assertRaises(ProxyBlocked):
            self.send()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn(KEY, self.log.read_text())

    def test_length_returned_unchanged_but_latched(self):
        self.reply["choices"][0]["finish_reason"] = "length"
        self.raw_reply = json.dumps(self.reply).encode()
        self.assertEqual(self.send(), (200, self.raw_reply))
        self.assertTrue(self.proxy.blocked)
        self.assertFalse(self.audit()["transport_capture_checked"])

    def test_bad_usage_model_and_reasoning_are_not_pass(self):
        mutations = [{"usage": None}, {"usage": {"prompt_tokens": True, "completion_tokens": 5, "total_tokens": 6}},
                     {"usage": {"prompt_tokens": 1, "completion_tokens": 200, "total_tokens": 201}},
                     {"model": "different"}, {"choices": []},
                     {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "reasoning_content": "thought"}}]}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                summary = response_summary(json.dumps(dict(self.reply, **mutation)).encode(), 200, 128, self.config)
                self.assertTrue(summary["transport_issues"])

    def test_models_catalog_not_synthesized(self):
        self.raw_reply = b'{"data":[{"id":"real-catalog-fixture"}]}'
        self.assertEqual(self.proxy.dispatch("GET", "/v1/models", b""), (200, self.raw_reply))
        self.assertNotIn("max_model_len", self.log.read_text())
        result = self.audit()
        self.assertTrue(result["transport_capture_checked"])
        self.assertEqual(result["completed_chat_requests_checked"], 0)

    def test_routes_cannot_escape_fixed_origin(self):
        for path in ("/tokenize", "//evil.example/", "/v1/models?key=" + KEY, "https://evil.example/"):
            with self.subTest(path=path), self.assertRaises(ProxyBlocked):
                self.proxy.dispatch("GET", path, b"")
        self.assertFalse(self.sent)
        self.assertNotIn(KEY, self.log.read_text())

    def test_credential_response_echo_is_withheld(self):
        self.raw_reply = json.dumps({"error": KEY}).encode()
        with self.assertRaisesRegex(ProxyBlocked, "credential"):
            self.send()
        self.assertNotIn(KEY, self.log.read_text())
        self.assertFalse(self.audit()["transport_capture_checked"])

    def test_unicode_escaped_credential_in_duplicate_key_is_withheld(self):
        escaped = ''.join('\\u%04x' % ord(c) for c in KEY)
        raw = ('{"x":"' + escaped + '","x":"clean"}').encode()
        with self.assertRaisesRegex(ProxyBlocked, "credential"):
            self.proxy.dispatch("POST", "/v1/chat/completions", raw)
        self.assertNotIn("body_base64", self.log.read_text())
        self.assertEqual(self.sent, [])

    def test_credential_trace_is_not_saved(self):
        self.trace = KEY
        self.send()
        self.assertIsNone(self.records()[-2]["trace_id"])
        self.assertNotIn(KEY, self.log.read_text())

    def test_response_size_prefix_preserved_and_failed(self):
        self.proxy.config = replace(self.config, max_response_bytes=12)
        with self.assertRaisesRegex(ProxyBlocked, "size_limit"):
            self.send()
        prefix = [r for r in self.records() if r["kind"] == "upstream_response_prefix"][0]
        self.assertFalse(prefix["complete_body"])
        self.assertEqual(base64.b64decode(prefix["body_base64"]), self.raw_reply[:13])

    def test_no_overwrite(self):
        with self.assertRaises(FileExistsError):
            CloudAuditProxy(self.config, self.log, api_key=KEY)

    def test_request_cap_blocks_upstream(self):
        self.proxy.config = replace(self.config, max_requests=1)
        self.send()
        with self.assertRaises(ProxyBlocked):
            self.send()
        self.assertEqual(len(self.sent), 1)

    def test_incoming_authorization_not_forwarded(self):
        server = make_server(self.proxy, 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True).start()
        request = Request(f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                          data=json.dumps(self.body).encode(), headers={"Authorization": "Bearer caller-secret"})
        with urlopen(request, timeout=2) as reply:
            self.assertEqual(reply.status, 200)
        self.assertEqual(self.sent[0].get_header("Authorization"), "Bearer " + KEY)
        self.assertNotIn("caller-secret", self.log.read_text())

    def test_audit_detects_body_summary_and_mapping_tampering(self):
        self.send()
        self.proxy.close()
        original = self.records()
        for field, value in [("body_sha256", "0" * 64), ("body_bytes", 1),
                             ("body_utf8", "different")]:
            changed = json.loads(json.dumps(original))
            changed[1][field] = value
            self.log.write_text('\n'.join(json.dumps(r) for r in changed) + '\n')
            self.assertFalse(audit_cloud_journal(self.log)["transport_capture_checked"])
        changed = json.loads(json.dumps(original))
        changed[-2]["token_ids"] = [1, 2]
        self.log.write_text('\n'.join(json.dumps(r) for r in changed) + '\n')
        self.assertFalse(audit_cloud_journal(self.log)["transport_capture_checked"])

    def test_audit_requires_close_and_rejects_old_profile(self):
        self.send()
        self.assertFalse(audit_cloud_journal(self.log)["transport_capture_checked"])
        self.proxy.close()
        records = self.records()
        records[0]["profile"] = "vllm-old"
        self.log.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
        self.assertFalse(audit_cloud_journal(self.log)["transport_capture_checked"])

    def test_audit_rejects_changed_mapping_and_runtime_source(self):
        self.send()
        self.proxy.close()
        records = self.records()
        records[2]["changes"] = [{"operation": "pretend"}]
        self.log.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
        self.assertFalse(audit_cloud_journal(self.log)["transport_capture_checked"])
        records[2]["changes"] = []
        records[0]["code_sha256"]["ae_http.py"] = "wrong"
        self.log.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
        self.assertFalse(audit_cloud_journal(self.log)["transport_capture_checked"])

    def test_tool_call_and_next_tool_return_are_preserved(self):
        call = {"id": "call_fixture", "type": "function",
                "function": {"name": "read_probe", "arguments": "{}"}}
        self.reply["choices"][0] = {"index": 0, "finish_reason": "tool_calls",
                                   "message": {"role": "assistant", "content": None, "tool_calls": [call]}}
        self.raw_reply = json.dumps(self.reply).encode()
        self.send()
        second = dict(self.body, messages=self.body["messages"] + [self.reply["choices"][0]["message"],
                               {"role": "tool", "tool_call_id": "call_fixture", "content": "fixture-value"}])
        self.send(second)
        self.assertEqual(json.loads(self.sent[-1].data)["messages"], second["messages"])
        self.assertTrue(self.audit()["transport_capture_checked"])

    def test_summary_omitted_unknown_field_is_not_same_as_explicit_null(self):
        self.send()
        self.proxy.close()
        records = self.records()
        del records[-2]["token_ids"]
        self.log.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
        self.assertFalse(audit_cloud_journal(self.log)["transport_capture_checked"])

    def test_unicode_escaped_credential_response_withheld(self):
        escaped = ''.join('\\u%04x' % ord(c) for c in KEY)
        self.raw_reply = ('{"error":"' + escaped + '"}').encode()
        with self.assertRaisesRegex(ProxyBlocked, "credential"):
            self.send()
        self.assertFalse(any(r['kind'] == 'upstream_response' for r in self.records()))


class ConfigKeyAndPlanTests(unittest.TestCase):
    def test_invalid_origins_and_models_rejected(self):
        cfg = CloudConfig(MODEL, 128, 1024, 1024, 3, 1)
        for extra in ({"upstream_origin": "http://api.siliconflow.cn"},
                      {"upstream_origin": ORIGIN + ".evil"}, {"model": "other"},
                      {"max_requests": True}, {"io_timeout_seconds": float("nan")}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                replace(cfg, **extra).validate()

    def test_private_file_modes_links_and_format(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            key.write_text(KEY + '\n')
            key.chmod(0o600)
            self.assertEqual(read_private_key(key), KEY)
            key.chmod(0o644)
            with self.assertRaises(ValueError):
                read_private_key(key)
            key.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(key)
            with self.assertRaises(OSError):
                read_private_key(link)
            key.write_text(KEY + '\nHeader: injection')
            with self.assertRaises(ValueError):
                read_private_key(key)

    def test_default_plan_does_not_load_key_or_construct_proxy(self):
        spec = importlib.util.spec_from_file_location("cloud_cli", ROOT / "scripts/ae_01_cloud_proxy.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, "read_private_key", side_effect=AssertionError("no key access")), \
             patch.object(module, "CloudAuditProxy", side_effect=AssertionError("no networking")), \
             patch("sys.argv", ["cloud", "--config", str(ROOT / "configs/ae-01__cloud-transport__siliconflow.prototype.json")]), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(module.main(), 0)
            result = json.loads(output.getvalue())
            self.assertFalse(result["key_loaded"])
            self.assertFalse(result["network_called"])
            self.assertFalse(result["task_runner_ready"])


if __name__ == "__main__":
    unittest.main()
