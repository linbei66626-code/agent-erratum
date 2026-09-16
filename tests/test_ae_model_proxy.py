"""Real loopback HTTP fixtures with invented responses: NOT model evidence."""
import base64
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ae_model_proxy import ModelAuditProxy, ProxyConfig, make_server, tokenize_projection


class ProxyFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "model-private.jsonl"
        self.observed = []
        self.token_count = 10
        self.window = 100
        self.reply = b'{ "choices": [{"finish_reason": "stop", "message":{"content":"fixture-not-a-model"}}], "usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12} }'
        self.status = 200
        self.redirect = False
        self.delay = 0
        fixture = self

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self): self.route()
            do_POST = do_GET
            def route(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fixture.observed.append((self.path, raw, dict(self.headers)))
                # The upstream request body is already fsync'ed when it arrives.
                records = fixture.records()
                assert records[-1]["kind"] == "upstream_request"
                assert base64.b64decode(records[-1]["body_base64"]) == raw
                if self.path == "/tokenize":
                    data = json.dumps({"count": fixture.token_count, "tokens": list(range(fixture.token_count)),
                                       "max_model_len": fixture.window}).encode()
                elif self.path == "/v1/models":
                    data = b'{"data":[{"id":"Qwen3-8B","max_model_len":100}]}'
                else:
                    data = fixture.reply
                    if fixture.delay: time.sleep(fixture.delay)
                self.send_response(302 if fixture.redirect else fixture.status)
                if fixture.redirect: self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try: self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError): pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        thread = threading.Thread(target=self.upstream.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.config = ProxyConfig(f"http://127.0.0.1:{self.upstream.server_port}", "Qwen3-8B",
                                  100, 70, 20, 8192, 8192, 20, .2)
        self.body = {"model": "Qwen3-8B", "messages": [{"role": "user", "content": "测试"}],
                     "tools": [{"type": "function", "function": {"name": "fixture_tool", "parameters": {"type": "object"}}}],
                     "tool_choice": "auto", "max_tokens": 20, "stream": False,
                     "chat_template_kwargs": {"enable_thinking": True}}

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def start(self, config=None):
        proxy = ModelAuditProxy(config or self.config, self.log)
        server = make_server(proxy, 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(proxy.close)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return proxy, f"http://127.0.0.1:{server.server_port}"

    def request(self, origin, body=None, *, path="/v1/chat/completions", headers=None, method="POST"):
        raw = json.dumps(self.body if body is None else body, ensure_ascii=False, indent=2).encode() if method == "POST" else None
        req = Request(origin + path, data=raw, method=method, headers=headers or {})
        try: response = urlopen(req, timeout=2)
        except HTTPError as exc: response = exc
        with response: return response.code, response.read(), raw

    def test_exact_bodies_tools_and_purpose_are_saved_no_auth_forwarding(self):
        _, origin = self.start()
        status, reply, raw = self.request(origin, headers={"Authorization": "Bearer fixture-secret",
                         "X-AE-Purpose": "agent/rewrite/t4", "X-AE-Role": "user_simulator"})
        self.assertEqual(status, 200)
        self.assertEqual(reply, self.reply)
        self.assertEqual([x[0] for x in self.observed], ["/tokenize", "/v1/chat/completions"])
        projected = json.loads(self.observed[0][1])
        self.assertEqual(projected["messages"], self.body["messages"])
        self.assertEqual(projected["tools"], self.body["tools"])
        self.assertEqual(projected["chat_template_kwargs"], self.body["chat_template_kwargs"])
        self.assertEqual(self.observed[1][1], raw)
        for _, _, headers in self.observed:
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("X-Ae-Purpose", headers)
            self.assertNotIn("X-Ae-Role", headers)
        records = self.records()
        self.assertEqual(records[-1]["purpose"], "agent/rewrite/t4")
        self.assertEqual(records[-1]["role"], "user_simulator")
        self.assertTrue(records[-1]["tokenize_prompt_usage_agreement"])
        self.assertNotIn("fixture-secret", self.log.read_text())
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_models_only_does_not_tokenize_or_infer(self):
        _, origin = self.start()
        status, reply, _ = self.request(origin, path="/v1/models", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.observed), 1)
        self.assertEqual(self.observed[0][0], "/v1/models")

    def test_overflow_blocks_before_model_and_latches(self):
        self.token_count = 75
        _, origin = self.start()
        self.assertEqual(self.request(origin)[0], 413)
        self.assertEqual([r[0] for r in self.observed], ["/tokenize"])
        self.assertEqual(self.request(origin)[0], 503)
        self.assertEqual(len(self.observed), 1)

    def test_actual_output_limit_is_not_overwritten_by_reserve(self):
        _, origin = self.start()
        self.assertEqual(self.request(origin, dict(self.body, max_tokens=95))[0], 413)
        self.assertEqual(self.observed, [])

    def test_unparseable_model_response_is_saved_and_forwarded_unchanged(self):
        self.reply = b"not-json-private-model-response"
        _, origin = self.start()
        status, reply, _ = self.request(origin)
        self.assertEqual((status, reply), (200, self.reply))
        self.assertEqual(self.records()[-1]["response_parse_error"], "non_json_object_response")
        self.assertEqual(self.request(origin)[0], 503)

    def test_wrong_runtime_window_blocks(self):
        self.window = 8192
        _, origin = self.start()
        self.assertEqual(self.request(origin)[0], 502)
        self.assertEqual(len(self.observed), 1)

    def test_truncated_response_is_unchanged_and_blocks_next_request(self):
        self.reply = self.reply.replace(b'"stop"', b'"length"')
        _, origin = self.start()
        status, reply, _ = self.request(origin)
        self.assertEqual((status, reply), (200, self.reply))
        self.assertTrue(self.records()[-1]["truncation_finish"])
        self.assertEqual(self.request(origin)[0], 503)

    def test_count_usage_mismatch_blocks_next_request(self):
        self.reply = self.reply.replace(b'"prompt_tokens":10', b'"prompt_tokens":11')
        _, origin = self.start()
        self.assertEqual(self.request(origin)[0], 200)
        self.assertFalse(self.records()[-1]["tokenize_prompt_usage_agreement"])
        self.assertEqual(self.request(origin)[0], 503)

    def test_unsupported_projection_is_not_silently_counted(self):
        for patch_body in ({"stream": True}, {"documents": [{"text": "uncovered"}]},
                           {"truncate_prompt_tokens": 4}, {"max_tokens": None},
                           {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://never.fetch"}}]}]}):
            with self.subTest(patch_body=patch_body):
                with self.assertRaises(Exception): tokenize_projection(dict(self.body, **patch_body), self.config)
        self.assertEqual(self.observed, [])

    def test_redirect_not_followed(self):
        self.redirect = True
        _, origin = self.start()
        self.assertEqual(self.request(origin)[0], 502)
        self.assertEqual([r[0] for r in self.observed], ["/tokenize"])

    def test_timeout_not_retried(self):
        self.delay = .1
        _, origin = self.start(replace(self.config, io_timeout_seconds=.02))
        self.assertEqual(self.request(origin)[0], 502)
        time.sleep(.12)
        self.assertEqual([r[0] for r in self.observed], ["/tokenize", "/v1/chat/completions"])

    def test_upstream_error_body_is_preserved_and_saved(self):
        # /models has no preflight; forward the raw upstream HTTP error body.
        self.status = 503
        _, origin = self.start()
        self.assertEqual(self.request(origin, path="/v1/models", method="GET")[0], 503)
        self.assertEqual(self.records()[-1]["http_status"], 503)

    def test_exclusive_journal(self):
        self.log.write_text("unchanged")
        with self.assertRaises(FileExistsError): ModelAuditProxy(self.config, self.log)
        self.assertEqual(self.log.read_text(), "unchanged")

    def test_remote_origin_and_invalid_config_refused(self):
        for value in ("http://example.com:80", "http://user:secret@127.0.0.1:8000",
                      "http://127.0.0.1:8000/v1", "http://127.0.0.1:8000?key=x"):
            with self.assertRaises(ValueError): ModelAuditProxy(replace(self.config, upstream_origin=value), self.log)
        self.assertFalse(self.log.exists())

    def test_disallowed_route_not_forwarded(self):
        _, origin = self.start()
        self.assertEqual(self.request(origin, path="/tokenize")[0], 404)
        self.assertEqual(self.observed, [])

    def test_request_count_limit_prevents_further_upstream_calls(self):
        _, origin = self.start(replace(self.config, max_requests=1))
        self.assertEqual(self.request(origin, path="/v1/models", method="GET")[0], 200)
        self.assertEqual(self.request(origin)[0], 503)
        self.assertEqual([r[0] for r in self.observed], ["/v1/models"])

    def test_duplicate_json_keys_rejected_instead_of_rewritten(self):
        _, origin = self.start()
        req = Request(origin + "/v1/chat/completions", data=b'{"model":"a","model":"b"}', method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(req, timeout=2)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertEqual(self.observed, [])

    def test_same_listen_and_upstream_port_rejected(self):
        proxy = ModelAuditProxy(self.config, self.log)
        self.addCleanup(proxy.close)
        with self.assertRaises(ValueError): make_server(proxy, self.upstream.server_port)

    def test_request_and_response_size_caps(self):
        _, origin = self.start(replace(self.config, max_request_bytes=4))
        self.assertEqual(self.request(origin)[0], 413)
        self.assertEqual(self.observed, [])

    def test_response_size_cap(self):
        _, origin = self.start(replace(self.config, max_response_bytes=4))
        self.assertEqual(self.request(origin)[0], 502)
        self.assertEqual(len(self.observed), 1)
        partial = [r for r in self.records() if r["kind"] == "upstream_response_prefix"]
        self.assertEqual(len(partial), 1)
        self.assertFalse(partial[0]["complete_body"])


if __name__ == "__main__":
    unittest.main()
