"""Offline tests for the single-request cloud replay script.

A real CloudAuditProxy (fake opener, fake monotonic clock) produces the source
journal fixture; no socket, no model, no real 65-second wait. Only the replay
script is exercised.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ae_cloud_proxy  # noqa: E402
from ae_cloud_proxy import CloudAuditProxy, CloudConfig, MODEL  # noqa: E402
from test_ae_cloud_proxy import KEY, Reply  # noqa: E402

PACED = ROOT / "configs/ae-01__cloud-transport__siliconflow.capability-pacing-candidate.json"

spec = importlib.util.spec_from_file_location("cloud_replay", ROOT / "scripts/ae_01_cloud_replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def reply(content, *, finish_reason="stop", tool_calls=None, status=200):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body = {"model": MODEL, "id": "fixture", "system_fingerprint": "fixture",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    return Reply(json.dumps(body, ensure_ascii=False).encode(), status=status)


class SourceFixture:
    def __init__(self, tmp, *, content="fixture reply, not a model"):
        self.tmp = Path(tmp)
        self.clock = FakeClock()
        self.requests = []
        proxy = CloudAuditProxy(CloudConfig(**json.loads(PACED.read_text())),
                                self.tmp / "capture.private.jsonl", api_key=KEY,
                                clock=self.clock.monotonic, sleep=self.clock.sleep)
        fixture = self

        class Opener:
            def open(self, request, timeout):
                fixture.requests.append((request.get_method(), request.data))
                if request.get_method() == "GET":
                    return Reply(json.dumps({"object": "list",
                                             "data": [{"id": MODEL}]}).encode())
                return fixture.response()

        proxy.opener = Opener()
        body = {"model": MODEL, "messages": [{"role": "user", "content": "fixture"}],
                "max_tokens": 2048, "temperature": 0, "stream": False}
        proxy.dispatch("POST", "/v1/chat/completions",
                       json.dumps(body, ensure_ascii=False).encode(), "fixture", "agent_or_unknown")
        proxy.close()
        rows = [json.loads(line) for line in (self.tmp / "capture.private.jsonl").read_text().splitlines()]
        self.rows = rows
        upstream = [r for r in rows if r.get("kind") == "upstream_request"][0]
        self.upstream = upstream
        self.request_id = upstream["request_id"]
        self.source_raw = base64.b64decode(upstream["body_base64"])
        self.journal = self.tmp / "source.private.jsonl"
        self.journal.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                                encoding="utf-8")

    def journal_with(self, name, mutate):
        rows = json.loads(json.dumps(self.rows))
        mutate(rows)
        path = self.tmp / name
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                        encoding="utf-8")
        return path

    def response(self):
        return reply("fixture reply, not a model")

    def key_file(self):
        key = self.tmp / "key"
        key.write_text(KEY + "\n", encoding="utf-8")
        key.chmod(0o600)
        return key

    def argv(self, out, *, execute=False):
        args = ["--source-journal", str(self.journal), "--request-id", self.request_id,
                "--config", str(PACED), "--output-dir", str(out)]
        if execute:
            args += ["--execute", "--key-file", str(self.key_file())]
        return args


class FastProxy(CloudAuditProxy):
    """Replay script's proxy with the injected fake clock (no real 65s wait)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, clock=FakeClock().monotonic, sleep=FakeClock().sleep, **kwargs)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        self.fixture = SourceFixture(self.tmp)

    def test_plan_only_needs_no_key_and_no_network(self):
        out = self.tmp / "plan-out"
        with patch("socket.socket.connect", side_effect=AssertionError("offline")), \
             patch.object(replay, "read_private_key", side_effect=AssertionError("no key")):
            self.assertEqual(replay.main(self.fixture.argv(out)), 0)
        plan = json.loads((out / "plan.json").read_text())
        self.assertEqual(plan["mode"], "PLAN_ONLY")
        self.assertFalse(plan["network_called"])
        self.assertFalse(plan["key_read"])
        self.assertEqual(plan["source_request_sha256"],
                         __import__("hashlib").sha256(self.fixture.source_raw).hexdigest())
        self.assertEqual(plan["source_request_id"], self.fixture.request_id)
        self.assertEqual(plan["replay_script_sha256"],
                         __import__("hashlib").sha256(
                             (ROOT / "scripts/ae_01_cloud_replay.py").read_bytes()).hexdigest())
        self.assertFalse((out / "replay.private.jsonl").exists())
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(out / "plan.json").st_mode & 0o777, 0o600)

    def _execute(self, out, *, response=None):
        sent = []

        class Opener:
            def open(self, request, timeout):
                sent.append(request.data)
                return response
        clock = FakeClock()

        class Fast(CloudAuditProxy):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, clock=clock.monotonic, sleep=clock.sleep, **kwargs)

        with patch.object(ae_cloud_proxy, "build_opener", return_value=Opener()), \
             patch.object(replay, "CloudAuditProxy", Fast):
            code = replay.main(self.fixture.argv(out, execute=True))
        return code, sent

    def test_execute_sends_the_same_bytes_once(self):
        out = self.tmp / "exec-out"
        code, sent = self._execute(out, response=reply("ok, fixture only"))
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], self.fixture.source_raw)
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual(summary["http_status"], 200)
        self.assertEqual(summary["attempts"], 1)
        self.assertGreater(summary["content_length"], 0)
        self.assertFalse(summary["task_pass"])
        self.assertIsNone(summary["task_success"])
        self.assertEqual([w["first_send"] for w in summary["pace_waits"]], [True])
        self.assertEqual(os.stat(out / "summary.json").st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(out / "replay.private.jsonl").st_mode & 0o777, 0o600)

    def test_empty_content_is_recorded_honestly(self):
        out = self.tmp / "empty-out"
        code, sent = self._execute(out, response=reply(""))
        self.assertEqual(code, 0)
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual(summary["http_status"], 200)
        self.assertEqual(summary["content_length"], 0)
        self.assertTrue(summary["content_empty"])
        self.assertEqual(summary["finish_reason"], "stop")
        self.assertFalse(summary["task_pass"])
        self.assertIsNone(summary["task_success"])
        self.assertEqual(len(sent), 1)

    def test_429_is_a_single_attempt_and_replay_failed(self):
        out = self.tmp / "rate-out"
        code, sent = self._execute(out, response=reply("rate limited", status=429))
        self.assertEqual(code, 2)
        self.assertEqual(len(sent), 1)
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual(summary["http_status"], 429)
        self.assertEqual(summary["attempts"], 1)
        self.assertFalse(summary["task_pass"])

    def test_exception_is_replay_failed_with_summary(self):
        out = self.tmp / "error-out"
        from urllib.error import URLError

        class Opener:
            def open(self, request, timeout):
                raise URLError("fixture network failure")

        clock = FakeClock()

        class Fast(CloudAuditProxy):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, clock=clock.monotonic, sleep=clock.sleep, **kwargs)

        with patch.object(ae_cloud_proxy, "build_opener", return_value=Opener()), \
             patch.object(replay, "CloudAuditProxy", Fast):
            code = replay.main(self.fixture.argv(out, execute=True))
        self.assertEqual(code, 2)
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual(summary["exception_type"], "ProxyBlocked")
        self.assertIsNotNone(summary["exception_code"])
        self.assertFalse(summary["task_pass"])
        self.assertIsNone(summary["http_status"])

    def test_unclosed_or_incomplete_source_journal_is_refused(self):
        cases = {
            "no_close": self.fixture.journal_with("no-close.jsonl",
                                                  lambda rows: rows.__delitem__(-1)),
            "close_not_last": self.fixture.journal_with(
                "close-early.jsonl",
                lambda rows: rows.insert(1, rows.pop())),
            "sequence_gap": self.fixture.journal_with(
                "gap.jsonl", lambda rows: rows[-1].__setitem__("sequence", 99)),
            "complete_body_false": self.fixture.journal_with(
                "complete.jsonl",
                lambda rows: [r for r in rows if r.get("kind") == "upstream_request"][0]
                .__setitem__("complete_body", False)),
        }
        for name, journal in cases.items():
            with self.subTest(case=name):
                out = self.tmp / f"out-{name}"
                stderr = io.StringIO()
                with patch("socket.socket.connect", side_effect=AssertionError("offline")), \
                     patch("sys.stderr", stderr):
                    code = replay.main([
                        "--source-journal", str(journal), "--request-id", self.fixture.request_id,
                        "--config", str(PACED), "--output-dir", str(out)])
                self.assertEqual(code, 2, name)
                self.assertFalse(out.exists(), name)
                # Errors print only a type/code JSON line, never the message text.
                printed = stderr.getvalue()
                self.assertIn("REPLAY_FAILED", printed, name)
                self.assertNotIn("cloud_close", printed, name)
                self.assertNotIn("sequence", printed.split("exception_code")[0], name)

    def test_tampered_body_duplicate_id_and_existing_output_are_refused(self):
        def tamper(rows):
            record = next(r for r in rows if r.get("kind") == "upstream_request")
            record["body_bytes"] = record["body_bytes"] + 1
        bad = self.fixture.journal_with("tampered.jsonl", tamper)
        out = self.tmp / "bad-out"
        with patch("socket.socket.connect", side_effect=AssertionError("offline")):
            self.assertEqual(replay.main([
                "--source-journal", str(bad), "--request-id", self.fixture.request_id,
                "--config", str(PACED), "--output-dir", str(out)]), 2)
        self.assertFalse(out.exists())

        def duplicate(rows):
            index = next(i for i, r in enumerate(rows) if r.get("kind") == "upstream_request")
            rows.insert(index, json.loads(json.dumps(rows[index])))
            for i, row in enumerate(rows):
                row["sequence"] = i
        dup = self.fixture.journal_with("duplicate.jsonl", duplicate)
        with patch("socket.socket.connect", side_effect=AssertionError("offline")):
            self.assertEqual(replay.main([
                "--source-journal", str(dup), "--request-id", self.fixture.request_id,
                "--config", str(PACED), "--output-dir", str(self.tmp / "dup-out")]), 2)

        existing = self.tmp / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with patch("socket.socket.connect", side_effect=AssertionError("offline")):
            self.assertEqual(replay.main(self.fixture.argv(existing)), 2)
        self.assertEqual(marker.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
