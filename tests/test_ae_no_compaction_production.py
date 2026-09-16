"""R3-2: the PRODUCTION entry really arms the pre-send capacity gate (offline).

The r2 review's finding was that the capacity contract existed but the production
entry never armed anything: the driver talked to a proxy that checked nothing. This
module exercises the real relation instead of an in-process object graph:

* the PROXY is started exactly as the deployment starts it - the real
  `scripts/ae_01_cloud_proxy.py --serve` process, its own private key file, its own
  journal, `--capacity-config` as the only way the gate can be armed;
* the PROVIDER is a real loopback HTTP server in this process, reached by a
  `sitecustomize` shim that redirects only the upstream SOCKET. The request bytes,
  the headers, the journal and the recorded `origin` stay the production ones, which
  is why the same `audit_cloud_journal` can read the result afterwards;
* the DRIVER side uses the production transport class (`ae_http.JSONHTTPTransport`)
  for the agent call, and the proxy's own annotation headers for the auxiliary roles.

Nothing here leaves the machine: no model is called, no credential is real, and the
sender counts what it received so "refused before send" is an observation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ae_cloud_audit import audit_cloud_journal  # noqa: E402
from ae_cloud_re_multiturn import canonical_sha256  # noqa: E402

MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
TRANSPORT_CONFIG = (ROOT / "configs"
                    / "ae-01__cloud-transport__siliconflow.capability-candidate.json")
CANDIDATE = (ROOT / "configs"
             / "ae-01__re-multiturn__siliconflow.capacity-256k-nocompaction-candidate.json")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _OfflineSender:
    """A real loopback HTTP server standing in for the cloud provider."""

    def __init__(self):
        import http.server
        import threading
        fixture = self
        self.sends = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            server_version = "OfflineSender"
            sys_version = ""

            def log_message(self, *_):
                pass

            def do_GET(self):
                self._serve()

            do_POST = do_GET

            def _serve(self):
                lengths = self.headers.get_all("Content-Length") or ["0"]
                raw = self.rfile.read(int(lengths[0])) if self.command == "POST" else b""
                fixture.sends.append({
                    "method": self.command, "path": self.path,
                    "role": self.headers.get("X-AE-Role"),
                    "body": json.loads(raw.decode("utf-8")) if raw else None})
                if self.path == "/v1/models":
                    payload = {"object": "list", "data": [
                        {"id": MODEL, "object": "model", "created": 0,
                         "owned_by": "offline-sender"}]}
                else:
                    payload = {
                        "id": "offline-sender", "object": "chat.completion", "model": MODEL,
                        "created": 0,
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "offline"}}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def chat_sends(self):
        return [send for send in self.sends if send["path"].endswith("chat/completions")]


class ProductionProxyCliTests(unittest.TestCase):
    """The real proxy CLI process is the only thing that can arm the gate."""

    def setUp(self):
        self.assets = None
        try:
            from ae_multiturn_capacity import load_official_tokenizer_assets
            self.assets = load_official_tokenizer_assets()
        except Exception as exc:  # pragma: no cover - the assets are present here
            self.skipTest(f"the counting assets are unavailable: {type(exc).__name__}")

    def _shim(self, tmp, sender_port):
        """A `sitecustomize` that redirects ONLY the upstream socket."""
        directory = Path(tmp) / "offline-shim"
        directory.mkdir(exist_ok=True)
        (directory / "sitecustomize.py").write_text(
            "import urllib.parse as _urlparse\n"
            "import ae_cloud_proxy as _proxy\n"
            f"_BASE = 'http://127.0.0.1:{sender_port}'\n"
            "_REAL = _proxy.build_opener(_proxy.ProxyHandler({}), _proxy._NoRedirect())\n"
            "class _Loopback:\n"
            "    def open(self, request, timeout):\n"
            "        request.full_url = _BASE + _urlparse.urlsplit(request.full_url).path\n"
            "        return _REAL.open(request, timeout=timeout)\n"
            "def _build_opener(*_args, **_kwargs):\n"
            "    return _Loopback()\n"
            "_proxy.build_opener = _build_opener\n", encoding="utf-8")
        return directory

    def _start(self, tmp, *, shim, port, declaration=None):
        key_file = Path(tmp) / "proxy-key"
        key_file.write_text("offline-fixture-key-0123456789\n", encoding="ascii")
        key_file.chmod(0o600)
        journal = Path(tmp) / "proxy.private.jsonl"
        if journal.exists():
            journal.unlink()
        command = [sys.executable, "-B", str(ROOT / "scripts/ae_01_cloud_proxy.py"),
                   "--config", str(TRANSPORT_CONFIG), "--serve",
                   "--key-file", str(key_file), "--journal", str(journal),
                   "--listen-port", str(port),
                   "--count-basis-tokenizer", str(self.assets["tokenizer_json"]),
                   "--count-basis-tokenizer-config", str(self.assets["tokenizer_config"])]
        if declaration is not None:
            command += ["--capacity-config", str(declaration)]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(shim), str(ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env)
        self.addCleanup(self._stop, process)
        return process, journal

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
                process.wait(timeout=30)

    @staticmethod
    def _await_listening(process):
        """Read the CLI's own startup line, or fail with what it printed instead."""
        import select
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], 0.5)[0]:
                line = process.stdout.readline()
                if not line:
                    break
                payload = json.loads(line)
                if payload.get("status") == "listening":
                    return payload
                raise AssertionError(f"the proxy CLI refused to start: {payload}")
            if process.poll() is not None:
                break
        raise AssertionError("the proxy CLI did not report a listening socket: "
                             + (process.stderr.read() or "")[-400:])

    @staticmethod
    def _await_refusal(process):
        """Read the CLI's own FAILED line and return it."""
        import select
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if select.select([process.stderr], [], [], 0.5)[0]:
                line = process.stderr.readline()
                if line:
                    return json.loads(line)
            if process.poll() is not None:
                break
        raise AssertionError("the proxy CLI did not print a refusal")

    @staticmethod
    def _declaration(tmp, window, name=None):
        """A declaration whose capacity block drives the gate (offline test value)."""
        declaration = json.loads(CANDIDATE.read_text(encoding="utf-8"))
        declaration["capacity"] = json.loads(json.dumps(declaration["capacity"]))
        declaration["capacity"]["context_window"] = window
        path = Path(tmp) / (name or f"declaration-{window}.json")
        path.write_text(json.dumps(declaration, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    @staticmethod
    def _request(port, text, role="agent_or_unknown"):
        """One REAL loopback request, with the proxy's own annotation headers."""
        body = json.dumps({
            "model": MODEL, "messages": [{"role": "user", "content": text}],
            "max_tokens": 2048, "stream": False, "temperature": 0},
            ensure_ascii=False).encode("utf-8")
        request = Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
                          headers={"Content-Type": "application/json",
                                   "X-AE-Purpose": "re-multiturn/stage", "X-AE-Role": role},
                          method="POST")
        try:
            with build_opener().open(request, timeout=120) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    @staticmethod
    def _rows(journal):
        return [json.loads(line) for line in Path(journal).read_text().splitlines() if line.strip()]

    def test_the_cli_process_arms_its_own_gate_for_every_role(self):
        """Agent 2048, user-simulator 4096, evaluator 4096 - armed by the CLI itself."""
        with tempfile.TemporaryDirectory() as tmp:
            sender = _OfflineSender()
            self.addCleanup(sender.close)
            port = _free_port()
            declaration = self._declaration(tmp, 262144)
            process, journal = self._start(
                tmp, shim=self._shim(tmp, sender.port), port=port, declaration=declaration)
            started = self._await_listening(process)
            self.assertTrue(started["capacity_armed"], started)
            self.assertEqual(started["capacity_declaration_sha256"],
                             canonical_sha256(json.loads(
                                 declaration.read_text(encoding="utf-8"))))
            # The production transport class the driver uses ...
            from ae_http import JSONHTTPTransport
            transport = JSONHTTPTransport(f"http://127.0.0.1:{port}",
                                          Path(tmp) / "driver.transport.jsonl",
                                          timeout_seconds=120, max_response_bytes=1048576,
                                          max_request_bytes=1048576)
            self.addCleanup(transport.close)
            listed = transport.request("GET", "/v1/models")
            self.assertEqual([m["id"] for m in listed["data"]], [MODEL])
            status, reply = self._request(port, "你好")
            self.assertEqual(status, 200, reply)
            # ... and the auxiliary roles' own annotation headers.
            for role in ("user_simulator", "evaluator"):
                status, reply = self._request(port, f"role {role}", role)
                self.assertEqual(status, 200, reply)
            self.assertEqual(len(sender.chat_sends()), 3, sender.sends)
            kinds = [row["kind"] for row in self._rows(journal)]
            self.assertIn("capacity_armed", kinds)
            checks = [row for row in self._rows(journal) if row["kind"] == "capacity_check"]
            self.assertEqual([row["output_reserve_tokens"] for row in checks], [2048, 4096, 4096])
            self.assertEqual([row["role"] for row in checks],
                             ["agent_or_unknown", "user_simulator", "evaluator"])
            self.assertTrue(all(row["fits"] for row in checks))
            self.assertTrue(all(row["count_source"].startswith("official_qwen_tokenizer:")
                                for row in checks), checks[0])
            # The evidence names the request it counted, inside the journal itself.
            self.assertTrue(all(len(row["input_sha256"]) == 64 for row in checks))
            self.assertEqual(len({row["request_id"] for row in checks}), 3)
            process.terminate()
            process.wait(timeout=30)
            audit = audit_cloud_journal(journal)
            self.assertEqual(audit["issues"], [], audit["issues"])
            self.assertTrue(audit["capacity_armed"])
            self.assertEqual([row["role"] for row in audit["capacity_checks"]],
                             ["agent_or_unknown", "user_simulator", "evaluator"])

    def test_an_over_capacity_request_is_refused_before_the_sender_sees_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = _OfflineSender()
            self.addCleanup(sender.close)
            port = _free_port()
            process, journal = self._start(
                tmp, shim=self._shim(tmp, sender.port), port=port,
                declaration=self._declaration(tmp, 64))
            self._await_listening(process)
            status, reply = self._request(port, "x" * 400, "evaluator")
            # The gate's stable code travels in the body; the HTTP status is the
            # blocked-response status the proxy has always used (400).
            self.assertEqual(status, 400, reply)
            self.assertEqual(reply.get("error", {}).get("code"), "capacity_exceeded_before_send")
            self.assertEqual(sender.chat_sends(), [], sender.sends)
            # Seal the journal by stopping the CLI, then read the whole capture.
            process.terminate()
            process.wait(timeout=30)
            rows = self._rows(journal)
            # The last two rows are the refusal and the HTTP handler's own annotation
            # of it; the check that refused the request sits before them.
            self.assertEqual([row["kind"] for row in rows],
                             ["cloud_open", "capacity_armed", "client_request",
                              "normalized_request", "capacity_check", "blocked",
                              "client_rejected", "cloud_close"])
            self.assertEqual(rows[6]["code"], "capacity_exceeded_before_send")
            check = rows[4]
            self.assertFalse(check["fits"])
            self.assertEqual(check["output_reserve_tokens"], 4096)
            self.assertEqual(check["role"], "evaluator")
            self.assertGreater(check["input_tokens"], 0)
            self.assertEqual([row for row in rows if row["kind"] == "upstream_request"], [],
                             "a refused request must not be sent upstream")

    def test_a_missing_or_unusable_declaration_is_a_startup_refusal(self):
        """No gate at all, or a declaration without a capacity block: refuse."""
        with tempfile.TemporaryDirectory() as tmp:
            sender = _OfflineSender()
            self.addCleanup(sender.close)
            # (a) started WITHOUT --capacity-config: the sealed behaviour, no arming.
            port = _free_port()
            process, journal = self._start(tmp, shim=self._shim(tmp, sender.port), port=port)
            started = self._await_listening(process)
            self.assertFalse(started["capacity_armed"])
            self.assertIsNone(started["capacity_declaration_sha256"])
            status, reply = self._request(port, "你好")
            self.assertEqual(status, 200, reply)
            kinds = [row["kind"] for row in self._rows(journal)]
            self.assertNotIn("capacity_armed", kinds)
            self.assertNotIn("capacity_check", kinds)
            process.terminate()
            process.wait(timeout=30)
            # (b) a declaration that carries no capacity block: the CLI must refuse to
            # serve at all instead of running unchecked.
            broken = Path(tmp) / "no-capacity-block.json"
            broken.write_text(json.dumps({"schema_version": "not-a-capacity-config"}),
                              encoding="utf-8")
            port = _free_port()
            before = len(sender.sends)
            process, journal = self._start(
                tmp, shim=self._shim(tmp, sender.port), port=port, declaration=broken)
            refusal = self._await_refusal(process)
            self.assertEqual(refusal.get("status"), "FAILED", refusal)
            self.assertEqual(process.wait(timeout=60), 2)
            kinds = [row["kind"] for row in self._rows(journal)]
            self.assertNotIn("capacity_armed", kinds)
            self.assertEqual(len(sender.sends), before,
                             "a refused startup must send nothing")


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
