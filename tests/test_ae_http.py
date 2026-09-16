"""Offline transport checks against an in-process loopback HTTP server."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ae_http import HTTPTransportError, JSONHTTPTransport, JSONLJournal


@contextmanager
def server(callback):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.handle_test_request()

        do_POST = do_GET
        do_PATCH = do_GET

        def handle_test_request(self):
            self.server.requests.append((self.command, self.path, dict(self.headers)))
            try:
                callback(self)
            except (BrokenPipeError, ConnectionResetError):
                pass

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    service.daemon_threads = True
    service.requests = []
    thread = threading.Thread(target=service.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{service.server_port}", service
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=1)


def respond(handler, raw=b'{"ok":true}', status=200, headers=None):
    handler.send_response(status)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "transport.jsonl"

    def client(self, origin, **kwargs):
        args = dict(timeout_seconds=0.5, max_response_bytes=1024, max_request_bytes=1024)
        args.update(kwargs)
        return JSONHTTPTransport(origin, self.log, **args)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_success_is_one_request_and_journal_is_durable_before_send(self):
        seen = []
        fsynced = []
        original = os.fsync

        def track_fsync(fd):
            original(fd)
            fsynced.append(len(self.records()))

        def callback(handler):
            records = self.records()
            seen.append(records[-1])
            self.assertEqual(records[-1]["kind"], "request")
            self.assertIn(len(records), fsynced)
            self.assertEqual(json.loads(handler.rfile.read(int(handler.headers["Content-Length"]))),
                             {"message": "测试"})
            respond(handler)

        with patch("ae_http.os.fsync", side_effect=track_fsync), server(callback) as (url, service):
            with self.client(url) as client:
                self.assertEqual(client.request("POST", "/v1/agents?include=agent.blocks", {"message": "测试"}),
                                 {"ok": True})
            self.assertEqual(len(service.requests), 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual([r["kind"] for r in self.records()],
                         ["transport_open", "request", "response", "transport_close"])

    def test_exclusive_log_creation_does_not_overwrite(self):
        self.log.write_text("original\n")
        with self.assertRaises(FileExistsError):
            self.client("http://127.0.0.1:9")
        self.assertEqual(self.log.read_text(), "original\n")

    def test_log_permissions_are_private(self):
        with self.client("http://127.0.0.1:9"):
            self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_fsync_failure_prevents_sending(self):
        with server(lambda h: respond(h)) as (url, service), self.client(url) as client:
            with patch("ae_http.os.fsync", side_effect=OSError("simulated disk failure")):
                with self.assertRaises(OSError):
                    client.request("GET", "/test")
            self.assertEqual(service.requests, [])

    def test_http_error_sanitized_and_recorded_without_retry(self):
        secret = "test-credential-never-log"
        with server(lambda h: respond(h, secret.encode(), 401)) as (url, service):
            with self.client(url, bearer_token=secret) as client:
                with self.assertRaises(HTTPTransportError) as caught:
                    client.request("GET", "/test")
            self.assertEqual(len(service.requests), 1)
        self.assertEqual(caught.exception.http_status, 401)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, self.log.read_text())
        self.assertEqual(self.records()[-2]["kind"], "failure")

    def test_invalid_json_response_and_nonobject(self):
        for raw, code in [(b"not JSON", "invalid_json"), (b"[]", "response_not_object"),
                          (b'{"a":NaN}', "invalid_json"), (b"\xff", "invalid_json")]:
            with self.subTest(raw=raw):
                self.log = Path(self.temp.name) / (code + str(len(raw)) + ".jsonl")
                with server(lambda h, raw=raw: respond(h, raw)) as (url, service):
                    with self.client(url) as client:
                        with self.assertRaises(HTTPTransportError) as caught:
                            client.request("GET", "/test")
                    self.assertEqual(len(service.requests), 1)
                self.assertEqual(caught.exception.code, code)

    def test_timeout_is_not_retried(self):
        def callback(handler):
            time.sleep(0.12)
            respond(handler)

        with server(callback) as (url, service):
            with self.client(url, timeout_seconds=0.02) as client:
                with self.assertRaises(HTTPTransportError) as caught:
                    client.request("GET", "/slow")
            time.sleep(0.13)
            self.assertEqual(len(service.requests), 1)
        self.assertEqual(caught.exception.code, "io_timeout")
        self.assertEqual(self.records()[-2]["code"], "io_timeout")

    def test_redirect_is_not_followed_or_retried(self):
        with server(lambda h: respond(h, b"", 302, {"Location": "/redirect-target"})) as (url, service):
            with self.client(url) as client:
                with self.assertRaises(HTTPTransportError) as caught:
                    client.request("GET", "/test")
            self.assertEqual([r[1] for r in service.requests], ["/test"])
        self.assertEqual(caught.exception.code, "redirect_refused")

    def test_response_limit(self):
        with server(lambda h: respond(h, b'{"value":"' + b"x" * 100 + b'"}')) as (url, service):
            with self.client(url, max_response_bytes=32) as client:
                with self.assertRaises(HTTPTransportError) as caught:
                    client.request("GET", "/large")
            self.assertEqual(len(service.requests), 1)
        self.assertEqual(caught.exception.code, "response_size_limit")

    def test_request_limit_prevents_send(self):
        with server(lambda h: respond(h)) as (url, service), self.client(url, max_request_bytes=8) as client:
            with self.assertRaises(HTTPTransportError) as caught:
                client.request("POST", "/test", {"value": "too large"})
            self.assertEqual(service.requests, [])
        self.assertEqual(caught.exception.code, "request_size_limit")

    def test_request_count_limit_records_block_without_sending(self):
        with server(lambda h: respond(h)) as (url, service), self.client(url, max_requests=1) as client:
            client.request("GET", "/first")
            with self.assertRaises(HTTPTransportError) as caught:
                client.request("GET", "/second")
            self.assertEqual(len(service.requests), 1)
            self.assertEqual(client.request_count, 1)
        self.assertEqual(caught.exception.code, "request_count_limit")
        self.assertEqual(self.records()[-2]["kind"], "blocked")

    def test_credentials_redacted_in_body_query_response_and_echo(self):
        secret = "test-secret-credential"
        def callback(handler):
            self.assertEqual(handler.headers["Authorization"], "Bearer " + secret)
            respond(handler, json.dumps({"Authorization": "another-secret",
                                       "echo": secret, "nested": [{"api_key": "third-secret"}]}).encode())

        with server(callback) as (url, service):
            with self.client(url, bearer_token=secret) as client:
                result = client.request("POST", "/test?api_key=another-secret&include=agent.blocks", {
                    "password": "fourth-secret", "message": "token echo " + secret})
        raw = self.log.read_text()
        for value in [secret, "another-secret", "third-secret", "fourth-secret"]:
            self.assertNotIn(value, raw)
        self.assertEqual(result["echo"], secret)  # Redaction must not mutate the response.
        self.assertIn("agent.blocks", raw)

    def test_keyboard_interrupt_preserves_intent_and_failure(self):
        with self.client("http://127.0.0.1:9") as client:
            with patch.object(client._opener, "open", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    client.request("GET", "/test")
        self.assertEqual([r["kind"] for r in self.records()],
                         ["transport_open", "request", "failure", "transport_close"])
        self.assertEqual(self.records()[-2]["code"], "interrupted")

    def test_closed_transport_cannot_send(self):
        client = self.client("http://127.0.0.1:9")
        client.close()
        with self.assertRaises(HTTPTransportError) as caught:
            client.request("GET", "/test")
        self.assertEqual(caught.exception.code, "transport_closed")

    def test_origin_policy_and_remote_opt_in(self):
        for url in ["http://example.test", "https://example.test", "http://127.0.0.1/v1",
                    "http://x:y@127.0.0.1", "http://127.0.0.1?token=x", "http://127.0.0.1#x",
                    "http://127.0.0.1?", "http://127.0.0.1#", "file:///tmp/test"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.client(url)
        self.assertFalse(self.log.exists())
        with self.client("https://example.test", allow_remote_https=True) as client:
            self.assertEqual(client.base_url, "https://example.test")
        # Merely constructing an explicitly authorized client does not contact it.

    def test_localhost_is_literal_loopback(self):
        with self.client("http://localhost:1234/") as client:
            self.assertEqual(client.base_url, "http://127.0.0.1:1234")

    def test_unsafe_paths_never_send(self):
        with server(lambda h: respond(h)) as (url, service), self.client(url) as client:
            for path in ["https://elsewhere.test/x", "//elsewhere.test/x", "/x#fragment",
                         "/../x", "/%2e%2e/x", "/%2felsewhere.test/x", "/x\\y", "/x\nY", "/%0dX"]:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    client.request("GET", path)
            self.assertEqual(service.requests, [])

    def test_limits_and_bearer_values_are_checked_before_log_creation(self):
        for kwargs in [{"timeout_seconds": float("nan")}, {"timeout_seconds": 0},
                       {"max_request_bytes": True}, {"max_response_bytes": -1},
                       {"max_requests": 0}, {"max_requests": True},
                       {"bearer_token": "unsafe\nAuthorization: x"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.client("http://127.0.0.1:9", **kwargs)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
