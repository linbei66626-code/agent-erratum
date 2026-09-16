"""Offline tests for the explicit cloud send pacing (NOT a TPM guarantee).

Fixtures use a fake opener and an injected monotonic clock/sleep: no socket, no
model, and no real 65-second wait. The tests cover first/subsequent/all-role
intervals, slow responses, invalid intervals, interruption without forwarding,
no forwarding after a latch, byte preservation, and strict journal auditing of
the pacing records (missing/duplicate/mismatched/tampered).
"""
from __future__ import annotations

from dataclasses import replace
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ae_cloud_audit import audit_cloud_journal
from ae_cloud_proxy import CloudAuditProxy, CloudConfig, MODEL, ORIGIN
from test_ae_cloud_proxy import KEY, Reply

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "configs/ae-01__cloud-transport__siliconflow.capability-candidate.json"
PACED = ROOT / "configs/ae-01__cloud-transport__siliconflow.capability-pacing-candidate.json"
REPLY = json.dumps({
    "model": MODEL, "id": "fixture", "system_fingerprint": "fixture",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "fixture, not a model"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}, ensure_ascii=False).encode()


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class SlowOpener:
    """One reply per POST; response latency is booked on the same fake clock."""

    def __init__(self, clock, *, latency=0.0, calls=None):
        self.clock, self.latency = clock, latency
        self.sent = []
        self.calls = calls if calls is not None else []

    def open(self, request, timeout):
        self.calls.append(request.get_method())
        if request.get_method() == "GET":
            return Reply(json.dumps({"object": "list", "data": [{"id": MODEL}]}).encode())
        self.sent.append(request.data)
        self.clock.now += self.latency
        return Reply(REPLY)


def paced_config(**overrides):
    data = json.loads(PACED.read_text())
    data.update(overrides)
    return CloudConfig(**data)


def make_proxy(tmp, *, pace=65.0, latency=0.0, clock=None):
    clock = clock or FakeClock()
    config = paced_config(pace_seconds=pace)
    proxy = CloudAuditProxy(config, Path(tmp) / "capture.private.jsonl", api_key=KEY,
                            clock=clock.monotonic, sleep=clock.sleep)
    opener = SlowOpener(clock, latency=latency)
    proxy.opener = opener
    return proxy, opener, clock


def chat(proxy, role="agent_or_unknown", text="fixture"):
    body = {"model": MODEL, "messages": [{"role": "user", "content": text}],
            "max_tokens": 2048, "temperature": 0, "stream": False}
    return proxy.dispatch("POST", "/v1/chat/completions",
                          json.dumps(body, ensure_ascii=False).encode(), "fixture", role)


def records(proxy):
    return [json.loads(line) for line in proxy.journal.path.read_text().splitlines()]


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_first_and_subsequent_sends_wait_the_configured_interval(self):
        proxy, opener, clock = make_proxy(self.tmp.name)
        self.addCleanup(proxy.close)
        for _ in range(3):
            self.assertEqual(chat(proxy)[0], 200)
        self.assertEqual(clock.sleeps, [65.0, 65.0, 65.0])
        marks = [r for r in records(proxy) if r["kind"] == "pace_wait"]
        self.assertEqual([m["first_send"] for m in marks], [True, False, False])
        self.assertEqual([m["interval_seconds"] for m in marks], [65.0, 65.0, 65.0])
        self.assertTrue(all(m["waited_seconds"] >= 65.0 for m in marks))
        self.assertEqual(len(opener.sent), 3)

    def test_all_roles_share_the_same_exit_pacing(self):
        proxy, opener, clock = make_proxy(self.tmp.name)
        self.addCleanup(proxy.close)
        for role in ("agent_or_unknown", "user_simulator", "evaluator"):
            self.assertEqual(chat(proxy, role=role)[0], 200)
        self.assertEqual(clock.sleeps, [65.0, 65.0, 65.0])
        marks = [r for r in records(proxy) if r["kind"] == "pace_wait"]
        self.assertEqual({m["interval_seconds"] for m in marks}, {65.0})

    def test_slow_response_does_not_change_the_send_interval(self):
        proxy, opener, clock = make_proxy(self.tmp.name, latency=30.0)
        self.addCleanup(proxy.close)
        chat(proxy)
        chat(proxy)
        # Second send waits until 65s after the first SEND, not after its reply.
        self.assertEqual(clock.sleeps, [65.0, 35.0])
        marks = [r for r in records(proxy) if r["kind"] == "pace_wait"]
        self.assertEqual(marks[1]["scheduled_wait_seconds"], 35.0)
        self.assertEqual(len(opener.sent), 2)

    def test_zero_interval_keeps_the_original_event_sequence(self):
        proxy, _opener, clock = make_proxy(self.tmp.name, pace=0.0)
        self.addCleanup(proxy.close)
        chat(proxy)
        chat(proxy)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual([r for r in records(proxy) if r["kind"] == "pace_wait"], [])
        kinds = [r["kind"] for r in records(proxy)]
        self.assertEqual(kinds.count("client_request"), 2)
        self.assertEqual(kinds.count("upstream_response"), 2)
        proxy.close()
        report = audit_cloud_journal(proxy.journal.path)
        self.assertTrue(report["transport_capture_checked"], report["issues"])

    def test_get_models_is_not_paced_and_not_recorded(self):
        proxy, _opener, clock = make_proxy(self.tmp.name)
        self.addCleanup(proxy.close)
        proxy.dispatch("GET", "/v1/models", b"")
        self.assertEqual(clock.sleeps, [])
        self.assertFalse([r for r in records(proxy) if r["kind"] == "pace_wait"])

    def test_request_bytes_are_unchanged_by_pacing(self):
        proxy, opener, _clock = make_proxy(self.tmp.name)
        self.addCleanup(proxy.close)
        body = {"model": MODEL, "messages": [{"role": "user", "content": "fixture"}],
                "max_tokens": 2048, "temperature": 0, "stream": False}
        raw = json.dumps(body, ensure_ascii=False).encode()
        proxy.dispatch("POST", "/v1/chat/completions", raw, "fixture")
        normalized = json.loads(base64.b64decode(
            [r for r in records(proxy) if r["kind"] == "normalized_request"][0]["body_base64"]))
        self.assertEqual(json.loads(opener.sent[0]), normalized)

    def test_invalid_interval_is_rejected(self):
        for bad in (-1, float("nan"), float("inf"), True, "65"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                paced_config(pace_seconds=bad).validate()

    def test_interrupted_wait_does_not_forward_and_latches(self):
        proxy, opener, clock = make_proxy(self.tmp.name)
        self.addCleanup(proxy.close)

        def interrupted(_seconds):
            raise KeyboardInterrupt

        proxy._sleep = interrupted
        with self.assertRaises(KeyboardInterrupt):
            chat(proxy)
        self.assertEqual(opener.sent, [])
        self.assertTrue(proxy.blocked)
        with self.assertRaises(Exception):
            chat(proxy)

    def test_wait_failure_before_send_does_not_forward_and_latches(self):
        """The failure happens in the wait, so no upstream send is attempted."""
        proxy, opener, clock = make_proxy(self.tmp.name)
        self.addCleanup(proxy.close)

        def failing(_seconds):
            raise RuntimeError("fixture wait failure")

        proxy._sleep = failing
        with self.assertRaises(Exception):
            chat(proxy)
        self.assertEqual(opener.sent, [])
        self.assertEqual(opener.calls, [])
        self.assertTrue(proxy.blocked)
        proxy._sleep = clock.sleep
        with self.assertRaises(Exception):
            chat(proxy)
        self.assertEqual(opener.sent, [])

    def test_upstream_429_is_attempted_once_and_latches(self):
        """A provider 429 is one POST attempt; the proxy latches, no retry."""
        proxy, _opener, _clock = make_proxy(self.tmp.name)
        self.addCleanup(proxy.close)
        attempts = {"n": 0}
        error_body = json.dumps({"error": {"code": 50602, "message": "TPM limit reached"}}).encode()

        def failing_open(request, timeout):
            attempts["n"] += 1
            return Reply(error_body, status=429)

        proxy.opener = type("Opener", (), {"open": staticmethod(failing_open)})()
        status, _reply = chat(proxy)
        self.assertEqual(status, 429)
        self.assertEqual(attempts["n"], 1)
        self.assertTrue(proxy.blocked)
        with self.assertRaises(Exception):
            chat(proxy)
        self.assertEqual(attempts["n"], 1)

    def test_upstream_network_error_is_attempted_once_and_latches(self):
        from urllib.error import URLError

        proxy, _opener, _clock = make_proxy(self.tmp.name)
        self.addCleanup(proxy.close)
        attempts = {"n": 0}

        def failing_open(request, timeout):
            attempts["n"] += 1
            raise URLError("fixture network failure")

        proxy.opener = type("Opener", (), {"open": staticmethod(failing_open)})()
        with self.assertRaises(Exception):
            chat(proxy)
        self.assertEqual(attempts["n"], 1)
        self.assertTrue(proxy.blocked)

    def test_early_sleep_return_is_topped_up_before_sending(self):
        """A sleep that returns early must loop until the target is reached."""
        class HalfClock(FakeClock):
            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.now += seconds / 2 if seconds > 1 else seconds

        clock = HalfClock()
        proxy, opener, clock = make_proxy(self.tmp.name, clock=clock)
        self.addCleanup(proxy.close)
        chat(proxy)
        self.assertGreaterEqual(clock.now, 1065.0)  # 1000 + 65 target reached
        self.assertGreater(len(clock.sleeps), 1)    # topped up
        mark = [r for r in records(proxy) if r["kind"] == "pace_wait"][0]
        self.assertGreaterEqual(mark["send_monotonic"] + 1e-6, mark["target_monotonic"])
        self.assertEqual(len(opener.sent), 1)

    def test_second_sleep_stall_is_detected(self):
        """A clock that advances once and then stalls fails instead of looping."""
        class StallingClock(FakeClock):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.calls += 1
                if self.calls == 1:
                    self.now += seconds
                # second and later sleeps do not advance the clock

        clock = StallingClock()
        proxy, opener, clock = make_proxy(self.tmp.name, clock=clock)
        self.addCleanup(proxy.close)
        # First request completes using the single advancing sleep; the second
        # request's sleep stalls and must fail without forwarding.
        chat(proxy)
        self.assertEqual(len(opener.sent), 1)
        with self.assertRaises(Exception):
            chat(proxy)
        self.assertEqual(len(opener.sent), 1)
        self.assertTrue(proxy.blocked)

    def test_journal_audit_accepts_paced_capture(self):
        proxy, _opener, _clock = make_proxy(self.tmp.name)
        chat(proxy)
        chat(proxy, role="user_simulator")
        proxy.close()
        report = audit_cloud_journal(proxy.journal.path)
        self.assertTrue(report["transport_capture_checked"], report["issues"])
        self.assertEqual(report["completed_chat_requests_checked"], 2)

    def test_journal_audit_rejects_missing_duplicate_mismatched_or_tampered_pace(self):
        cases = {
            # Structural mutations renumber so the pace/event gate fires, not the
            # journal sequence gate; each case asserts its exact issue code.
            "missing": (lambda rows, m: rows.pop(m[0]), "incomplete_or_reordered_request_events"),
            "duplicate": (lambda rows, m: rows.insert(m[0] + 1, dict(rows[m[0]])),
                          "incomplete_or_reordered_request_events"),
            "interval_mismatch": (lambda rows, m: rows[m[0]].__setitem__(
                "interval_seconds", 1.0), "pace_interval_mismatch"),
            "first_send_mismatch": (lambda rows, m: rows[m[1]].__setitem__(
                "first_send", True), "pace_first_send_mismatch"),
            "previous_mismatch": (lambda rows, m: rows[m[1]].__setitem__(
                "previous_send_monotonic", 123.0), "pace_previous_send_mismatch"),
            "waited_tampered": (lambda rows, m: rows[m[1]].__setitem__(
                "waited_seconds", 0.0), "pace_waited_not_measured"),
            "target_tampered": (lambda rows, m: rows[m[0]].__setitem__(
                "target_monotonic", 1.0), "pace_target_not_derived_from_interval"),
        }
        for name, (action, expected) in cases.items():
            with self.subTest(case=name):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                proxy, _opener, _clock = make_proxy(tmp.name)
                chat(proxy)
                chat(proxy)
                proxy.close()
                rows = [json.loads(line) for line in
                        proxy.journal.path.read_text().splitlines()]
                marks = [i for i, r in enumerate(rows) if r.get("kind") == "pace_wait"]
                action(rows, marks)
                for index, row in enumerate(rows):
                    row["sequence"] = index
                proxy.journal.path.write_text(
                    "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n",
                    encoding="utf-8")
                report = audit_cloud_journal(proxy.journal.path)
                self.assertFalse(report["transport_capture_checked"], name)
                self.assertIn(expected, report["issues"], name)


class PacingConfigTests(unittest.TestCase):
    def test_candidate_and_paced_transport_configs_differ_only_in_pacing(self):
        base = json.loads(CANDIDATE.read_text())
        paced = json.loads(PACED.read_text())
        self.assertEqual(base.get("pace_seconds", 0), 0)
        self.assertEqual(paced["pace_seconds"], 65)
        self.assertEqual({k: v for k, v in paced.items() if k != "pace_seconds"}, base)
        self.assertEqual(paced["io_timeout_seconds"], 180)


if __name__ == "__main__":
    unittest.main()
