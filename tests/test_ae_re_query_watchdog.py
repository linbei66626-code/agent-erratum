"""Offline tests for the candidate R/E repeated-query run watchdog.

No model, network, service or database is involved. The real trajectory replay
reads the frozen payment run's `events.jsonl`; the lifecycle tests start tiny local
fixture runners (sleeping / writing events) and check that the watchdog stops only
the child it started, never restarts it, and never pretends a stop was success.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from deployment import ae_re_query_watchdog as watchdog  # noqa: E402

FROZEN_LOOP_EVENTS = (ROOT / "transfers/lab-cloud-re-payment-run-20260913-r1/deployment/runs"
                             / "ae-cloud-re-pair-t4-payment-20260913-r1/events.jsonl")
LOOP_EVENTS_OVERRIDE = os.environ.get("AE_RE_QUERY_LOOP_EVENTS")
LOOP_EVENTS = Path(LOOP_EVENTS_OVERRIDE) if LOOP_EVENTS_OVERRIDE else FROZEN_LOOP_EVENTS
STORE = "get_delivery_store_info"
PRODUCT = "get_delivery_product_info"
BLOCK = "b" * 64

FIXTURE_RUNNER = '''
import json, sys, time
from pathlib import Path
mode, events, marker = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
extra = sys.argv[4] if len(sys.argv) > 4 else None
if mode != "production":
    events.parent.mkdir(parents=True, exist_ok=True)
else:
    events.parent.parent.mkdir(parents=True, exist_ok=True)
with marker.open("a", encoding="utf-8") as out:
    out.write("started\\n")
STAGE = {"kind": "stage_start", "arm": "rewrite", "task": "sub_U000828_4",
         "sequence": 0, "timestamp": "t0"}
def row(seq, tool, args, returned, status="success", block="b" * 64, cid=None):
    cid = cid or "call-%d" % seq
    return {"kind": "bridge_event", "arm": "rewrite", "phase": "task", "task": "sub_U000828_4",
            "event": {"kind": "client_tool_result",
                      "call": {"type": "tool", "tool_call_id": cid, "name": tool,
                               "arguments": json.dumps(args, ensure_ascii=False)},
                      "result": {"type": "tool", "tool_call_id": cid,
                                 "tool_return": returned, "status": status},
                      "block_sha": block},
            "sequence": seq, "timestamp": "t%d" % seq}
def emit(obj, newline=True):
    with events.open("a", encoding="utf-8") as out:
        out.write(json.dumps(obj, ensure_ascii=False) + ("\\n" if newline else ""))
        out.flush()
def loop_rows(start, groups):
    rows = []
    for index in range(groups):
        rows.append(row(start + 2 * index, "get_delivery_store_info",
                        {"store_id": "S1"}, "Store One,tea"))
        rows.append(row(start + 2 * index + 1, "get_delivery_product_info",
                        {"product_id": "P1"}, "StoreProduct P1"))
    return rows
if mode == "production":
    import importlib.util
    spec = importlib.util.spec_from_file_location("ae_cli_fixture", extra)
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    cli.ensure_output_dir(events.parent)     # the PRODUCTION new-run contract
    emit(STAGE)
    for item in loop_rows(1, 3):
        emit(item)
    time.sleep(60)
    raise SystemExit(0)
emit(STAGE)
if mode == "sleep":
    time.sleep(60)
elif mode == "loop":
    emit(row(1, "get_user_all_orders", {}, ""))
    emit(row(2, "memory_update", {"operation": "replace"}, "updated"))
    emit(row(3, "delivery_product_search_recommand", {"keywords": ["x"]}, "results"))
    first = json.dumps(loop_rows(4, 3)[0], ensure_ascii=False)
    with events.open("a", encoding="utf-8") as out:   # a real partial write
        out.write(first[:len(first) // 2]); out.flush()
    time.sleep(0.1)
    with events.open("a", encoding="utf-8") as out:
        out.write(first[len(first) // 2:] + "\\n"); out.flush()
    for item in loop_rows(4, 3)[1:]:
        emit(item)
    time.sleep(60)
elif mode == "two_groups":
    for item in loop_rows(1, 2):
        emit(item)
    with marker.open("a", encoding="utf-8") as out:
        out.write("done\\n")
elif mode == "malformed":
    with events.open("a", encoding="utf-8") as out:
        out.write('{"kind": "bridge_event",' + "\\n"); out.flush()
    time.sleep(60)
elif mode == "partial":
    items = loop_rows(1, 3)
    for item in items[:5]:
        emit(item)
    last = json.dumps(items[5], ensure_ascii=False)
    with events.open("a", encoding="utf-8") as out:
        out.write(last); out.flush()
    time.sleep(60)
elif mode == "exit7":
    sys.exit(7)
elif mode == "signal":
    import os, signal
    os.kill(os.getpid(), signal.SIGTERM)
else:
    raise SystemExit("unknown mode")
'''


def tool_row(sequence, tool=STORE, args=None, returned="Store One,tea", *, status="success",
             block=BLOCK, arm="rewrite", phase="task", task="sub_U000828_4", call_id=None,
             kind="client_tool_result"):
    call_id = call_id or f"call-{sequence}"
    if kind != "client_tool_result":
        event = {"kind": kind}
    else:
        event = {
            "kind": "client_tool_result",
            "call": {"type": "tool", "tool_call_id": call_id, "name": tool,
                     "arguments": args if isinstance(args, str) else json.dumps(
                         {"store_id": "S1"} if args is None else args, ensure_ascii=False)},
            "result": {"type": "tool", "tool_call_id": call_id,
                       "tool_return": returned, "status": status},
            "block_sha": block,
        }
    return {"kind": "bridge_event", "arm": arm, "phase": phase, "task": task,
            "event": event, "sequence": sequence, "timestamp": f"t{sequence}"}


def stage_row(sequence, arm="rewrite", task="sub_U000828_4"):
    return {"kind": "stage_start", "arm": arm, "task": task,
            "sequence": sequence, "timestamp": f"t{sequence}"}


def loop_rows(start=1, groups=3, *, store_args=None, product_args=None,
              store_return="Store One,tea", product_return="StoreProduct P1", **kwargs):
    rows = []
    for index in range(groups):
        rows.append(tool_row(start + 2 * index, STORE, store_args or {"store_id": "S1"},
                             store_return, **kwargs))
        rows.append(tool_row(start + 2 * index + 1, PRODUCT,
                             product_args or {"product_id": "P1"}, product_return, **kwargs))
    return rows


def detector_with(rows):
    """Feed the rows after the stage announcement every real run starts with."""
    detector = watchdog.QueryLoopDetector()
    detector.feed_row(stage_row(0))
    for row in rows:
        detector.feed_row(row)
    return detector


class RealTrajectoryReplayTests(unittest.TestCase):
    """Read-only replay of the frozen run that showed the repeated query loop."""

    def test_the_frozen_events_path_is_present(self):
        if LOOP_EVENTS_OVERRIDE and not LOOP_EVENTS.is_file():
            raise RuntimeError(
                f"AE_RE_QUERY_LOOP_EVENTS is set but the events file is missing: {LOOP_EVENTS}")
        self.assertTrue(LOOP_EVENTS.is_file(), f"frozen events file missing: {LOOP_EVENTS}")

    def test_the_frozen_trajectory_triggers_at_the_third_period_two_group(self):
        detector = watchdog.QueryLoopDetector()
        tail = watchdog.EventTail()
        with LOOP_EVENTS.open("rb") as handle:
            while True:
                chunk = handle.read(4096)
                if not chunk:
                    break
                for line in tail.feed(chunk):
                    detector.feed_line(line)
        trigger = detector.trigger
        self.assertIsNotNone(trigger, "the frozen loop must trigger")
        self.assertEqual(trigger.period, 2)
        self.assertEqual(trigger.repeated_query_results, 6)
        self.assertEqual(trigger.after_tool_results, 9)
        # The run continued with 10 further query results after the trigger point:
        # the historical saving is real but only historical (no guard was active).
        self.assertEqual(detector.after_trigger_query_results, 10)
        self.assertEqual(detector.tool_results, 19)
        self.assertEqual(detector.resets[:6], [
            "lifecycle:agent_created", "lifecycle:agent_created", "stage_start",
            "other_tool:get_user_all_orders", "other_tool:memory_update",
            "other_tool:delivery_product_search_recommand"])


class DetectorRuleTests(unittest.TestCase):
    """The candidate rule, positive and negative, without any process."""

    def test_period_one_three_repeats_triggers(self):
        rows = [tool_row(1, STORE, {"store_id": "S1"}),
                tool_row(2, STORE, {"store_id": "S1"}),
                tool_row(3, STORE, {"store_id": "S1"})]
        trigger = detector_with(rows).trigger
        self.assertIsNotNone(trigger)
        self.assertEqual((trigger.period, trigger.repeated_query_results), (1, 3))

    def test_period_two_three_groups_triggers(self):
        trigger = detector_with(loop_rows(1, 3)).trigger
        self.assertIsNotNone(trigger)
        self.assertEqual((trigger.period, trigger.repeated_query_results,
                          trigger.after_tool_results), (2, 6, 6))

    def test_two_groups_do_not_trigger(self):
        self.assertIsNone(detector_with(loop_rows(1, 2)).trigger)

    def test_a_changed_argument_breaks_the_cycle(self):
        rows = [tool_row(1, STORE, {"store_id": "S1"}),
                tool_row(2, STORE, {"store_id": "S1"}),
                tool_row(3, STORE, {"store_id": "S2"}),
                tool_row(4, STORE, {"store_id": "S1"}),
                tool_row(5, STORE, {"store_id": "S1"})]
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("signature_changed", detector.resets)

    def test_a_changed_return_breaks_the_cycle(self):
        rows = [tool_row(1, STORE, {"store_id": "S1"}, "Store One,tea"),
                tool_row(2, STORE, {"store_id": "S1"}, "Store One,tea"),
                tool_row(3, STORE, {"store_id": "S1"}, "Store One,coffee"),
                tool_row(4, STORE, {"store_id": "S1"}, "Store One,tea"),
                tool_row(5, STORE, {"store_id": "S1"}, "Store One,tea")]
        self.assertIsNone(detector_with(rows).trigger)

    def test_a_changed_block_breaks_the_cycle(self):
        rows = loop_rows(1, 2)
        rows.append(tool_row(5, STORE, {"store_id": "S1"}, block="c" * 64))
        rows.extend(loop_rows(6, 1))
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("memory_block_changed", detector.resets)

    def test_another_tool_interleaved_resets(self):
        rows = [tool_row(1, STORE, {"store_id": "S1"}),
                tool_row(2, STORE, {"store_id": "S1"}),
                tool_row(3, "delivery_product_search_recommand", {"keywords": ["x"]}, "r"),
                tool_row(4, STORE, {"store_id": "S1"}),
                tool_row(5, STORE, {"store_id": "S1"})]
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("other_tool:delivery_product_search_recommand", detector.resets)

    def test_a_memory_update_resets(self):
        rows = [tool_row(1, STORE, {"store_id": "S1"}),
                tool_row(2, STORE, {"store_id": "S1"}),
                tool_row(3, "memory_update", {"operation": "add"}, "added"),
                tool_row(4, STORE, {"store_id": "S1"}),
                tool_row(5, STORE, {"store_id": "S1"})]
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("other_tool:memory_update", detector.resets)

    def test_an_error_result_resets(self):
        rows = [tool_row(1, STORE, {"store_id": "S1"}),
                tool_row(2, STORE, {"store_id": "S1"}),
                tool_row(3, STORE, {"store_id": "S1"}, status="error"),
                tool_row(4, STORE, {"store_id": "S1"}),
                tool_row(5, STORE, {"store_id": "S1"})]
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("query_status:error", detector.resets)

    def test_a_user_message_resets(self):
        rows = [tool_row(1, STORE, {"store_id": "S1"}),
                tool_row(2, STORE, {"store_id": "S1"}),
                {"kind": "simulated_user", "arm": "rewrite", "sequence": 3, "timestamp": "t3",
                 "reply": {"content": "hello", "stop": False}},
                tool_row(4, STORE, {"store_id": "S1"}),
                tool_row(5, STORE, {"store_id": "S1"})]
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("lifecycle:simulated_user", detector.resets)

    def test_a_stage_or_arm_switch_resets(self):
        rows = loop_rows(1, 2) + [stage_row(5, arm="erratum")] + loop_rows(6, 2)
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("stage_start", detector.resets)

    def test_a_duplicate_call_id_is_not_counted(self):
        rows = [tool_row(1, STORE, {"store_id": "S1"}, call_id="same"),
                tool_row(2, STORE, {"store_id": "S1"}, call_id="same"),
                tool_row(3, STORE, {"store_id": "S1"}, call_id="same")]
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("duplicate_call_id", detector.resets)

    def test_a_replayed_event_is_not_counted(self):
        first = tool_row(1, STORE, {"store_id": "S1"})
        rows = [first,
                tool_row(2, STORE, {"store_id": "S1"}),
                dict(first, timestamp="replayed")]
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("replayed_event", detector.resets)

    def test_a_tool_name_or_a_call_count_alone_cannot_trigger(self):
        different = [tool_row(index, STORE, {"store_id": f"S{index}"})
                     for index in range(1, 7)]
        self.assertIsNone(detector_with(different).trigger)
        period_three = []
        for index in range(2):  # S1,S2,S3,S1,S2,S3: period 3 is outside the candidate rule
            for store in ("S1", "S2", "S3"):
                period_three.append(tool_row(len(period_three) + 1, STORE, {"store_id": store}))
        self.assertIsNone(detector_with(period_three).trigger)

    def test_a_complete_malformed_line_is_a_monitoring_failure(self):
        detector = watchdog.QueryLoopDetector()
        with self.assertRaises(watchdog.MonitoringFailure):
            detector.feed_line('{"kind": "bridge_event",')
        self.assertIsNone(detector.trigger)

    def test_an_unparseable_query_argument_is_a_monitoring_failure(self):
        detector = watchdog.QueryLoopDetector()
        detector.feed_row(stage_row(0))
        with self.assertRaises(watchdog.MonitoringFailure):
            detector.feed_row(tool_row(1, STORE, "not json"))

    def test_other_bridge_events_reset(self):
        rows = [tool_row(1, STORE, {"store_id": "S1"}),
                tool_row(2, STORE, {"store_id": "S1"}),
                {"kind": "bridge_event", "arm": "rewrite", "phase": "task",
                 "task": "sub_U000828_4", "sequence": 3, "timestamp": "t3",
                 "event": {"kind": "block_patch", "value": "x"}},
                tool_row(4, STORE, {"store_id": "S1"}),
                tool_row(5, STORE, {"store_id": "S1"})]
        detector = detector_with(rows)
        self.assertIsNone(detector.trigger)
        self.assertIn("bridge_event:block_patch", detector.resets)

    def test_ignored_bookkeeping_events_do_not_reset(self):
        rows = []
        rows.append(tool_row(1, STORE, {"store_id": "S1"}))
        rows.append({"kind": "bridge_event", "arm": "rewrite", "phase": "task",
                     "task": "sub_U000828_4", "sequence": 2, "timestamp": "t2",
                     "event": {"kind": "multicall_batch", "batch": {"count": 1}}})
        rows.append({"kind": "bridge_event", "arm": "rewrite", "phase": "task",
                     "task": "sub_U000828_4", "sequence": 3, "timestamp": "t3",
                     "event": {"kind": "request", "method": "POST", "path": "/messages"}})
        rows.append(tool_row(4, STORE, {"store_id": "S1"}))
        rows.append(tool_row(5, STORE, {"store_id": "S1"}))
        detector = detector_with(rows)
        self.assertIsNotNone(detector.trigger)
        self.assertEqual(detector.trigger.period, 1)
        self.assertEqual(detector.skipped_bridge_events, 2)


class EventTailTests(unittest.TestCase):
    """Only newline-terminated lines are ever judged."""

    def test_a_partial_line_is_not_emitted(self):
        tail = watchdog.EventTail()
        self.assertEqual(tail.feed(b'{"kind": "a"'), [])
        self.assertEqual(tail.lines, 0)
        self.assertEqual(tail.feed(b'}\n{"kind": "b"}\n{"kind": "c"'), ['{"kind": "a"}',
                                                                       '{"kind": "b"}'])
        self.assertEqual(tail.lines, 2)

    def test_the_digest_covers_only_complete_lines(self):
        import hashlib
        tail = watchdog.EventTail()
        tail.feed(b"one\ntwo\nthree")
        self.assertEqual(tail.digest.hexdigest(), hashlib.sha256(b"one\ntwo\n").hexdigest())

    def test_invalid_utf8_is_a_monitoring_failure(self):
        tail = watchdog.EventTail()
        with self.assertRaises(watchdog.MonitoringFailure):
            tail.feed(b"\xff\xfe\n")


class WatchdogLifecycleTests(unittest.TestCase):
    """Real child processes: stop only ours, never restart, never fake success."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-watchdog-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fixture = self.dir / "fixture_runner.py"
        self.fixture.write_text(FIXTURE_RUNNER, encoding="utf-8")
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def run_watchdog(self, mode, *, max_seconds=15.0, events=None, sidecar=None, marker=None,
                     fixture_extra=None):
        events = Path(events or self.dir / f"{mode}-run" / "events.jsonl")
        sidecar = Path(sidecar or self.dir / f"{mode}-sidecar.json")
        marker = Path(marker or self.dir / f"{mode}-marker.txt")
        command = [sys.executable, "-B", str(ROOT / "scripts/deployment/ae_re_query_watchdog.py"),
                   "--events", str(events), "--sidecar", str(sidecar),
                   "--poll-seconds", "0.05", "--max-seconds", str(max_seconds),
                   "--grace-seconds", "3",
                   "--", sys.executable, "-B", str(self.fixture), mode, str(events), str(marker)]
        if fixture_extra is not None:
            command.append(str(fixture_extra))
        done = subprocess.run(command, capture_output=True, text=True, timeout=max_seconds + 30)
        payload = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else None
        return done, payload, events, sidecar, marker

    def assert_child_gone(self, payload):
        pid = payload["runner"]["pid"]
        if pid:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_a_triggering_child_is_stopped_once_and_never_restarted(self):
        done, payload, _events, _sidecar, marker = self.run_watchdog("loop")
        self.assertEqual(done.returncode, watchdog.EXIT_TRIGGERED, done.stderr)
        self.assertEqual(payload["monitor"]["status"], "WATCHDOG_TRIGGERED_STOPPED")
        self.assertEqual(payload["trigger"]["period"], 2)
        self.assertEqual(payload["trigger"]["after_tool_results"], 9)
        self.assertEqual(payload["trigger"]["repeated_query_results"], 6)
        self.assertEqual(payload["runner"]["signal_sent"], "SIGTERM")
        self.assertIsNotNone(payload["runner"]["returncode"])
        self.assert_child_gone(payload)
        # The runner started exactly once: no restart loop.
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["started"])
        self.assertEqual(payload["watchdog"]["restarts_performed"], 0)
        self.assertFalse(payload["watchdog"]["process_scan_or_name_kill"])
        # An active stop is never a driver result and never a success.
        self.assertFalse(payload["diagnostic"]["scoring_performed"])
        self.assertFalse(payload["diagnostic"]["audit_relaxed"])
        self.assertFalse(payload["diagnostic"]["driver_result_faked"])
        self.assertEqual(payload["diagnostic"]["status_on_active_stop"],
                         "WATCHDOG_STOPPED_DIAGNOSTIC_INCOMPLETE")
        self.assertFalse((self.dir / "result.json").exists())
        # The scorecard records the real profile: rule, threshold and time.
        self.assertEqual(payload["rule"]["required_groups"], 3)
        self.assertEqual(payload["rule"]["period_max"], 2)
        self.assertTrue(payload["created_at_utc"].endswith("Z"))

    def test_a_child_below_the_threshold_is_not_signalled(self):
        done, payload, _events, _sidecar, marker = self.run_watchdog("two_groups")
        self.assertEqual(done.returncode, watchdog.EXIT_OK, done.stderr)
        self.assertEqual(payload["monitor"]["status"], "RUNNER_EXITED_NO_TRIGGER")
        self.assertIsNone(payload["trigger"])
        self.assertIsNone(payload["runner"]["signal_sent"])
        self.assertEqual(payload["runner"]["returncode"], 0)
        self.assertIn("done", marker.read_text(encoding="utf-8").splitlines())

    def test_a_sleeping_child_is_stopped_at_the_timeout_bound(self):
        done, payload, _events, _sidecar, _marker = self.run_watchdog(
            "sleep", max_seconds=0.8)
        self.assertEqual(done.returncode, watchdog.EXIT_TIMEOUT, done.stderr)
        self.assertEqual(payload["monitor"]["status"], "WATCHDOG_TIMEOUT_STOPPED")
        self.assertEqual(payload["runner"]["signal_sent"], "SIGTERM")
        self.assertIsNone(payload["trigger"])
        self.assert_child_gone(payload)

    def test_a_complete_malformed_line_stops_as_a_monitoring_failure(self):
        done, payload, _events, _sidecar, _marker = self.run_watchdog("malformed")
        self.assertEqual(done.returncode, watchdog.EXIT_MONITOR_FAILED, done.stderr)
        self.assertEqual(payload["monitor"]["status"], "MONITORING_FAILED_STOPPED")
        self.assertIn("not valid JSON", payload["monitor_failure"])
        self.assertIsNone(payload["trigger"])
        self.assert_child_gone(payload)

    def test_a_partial_line_never_triggers(self):
        done, payload, _events, _sidecar, _marker = self.run_watchdog(
            "partial", max_seconds=0.9)
        self.assertEqual(done.returncode, watchdog.EXIT_TIMEOUT, done.stderr)
        self.assertIsNone(payload["trigger"])
        # stage_start + 5 complete rows; the sixth is incomplete and not judged.
        self.assertEqual(payload["monitor"]["complete_lines"], 6)

    def test_a_bystander_process_of_the_same_shape_is_untouched(self):
        bystander_events = self.dir / "bystander-run" / "events.jsonl"
        bystander_marker = self.dir / "bystander-marker.txt"
        bystander = subprocess.Popen(
            [sys.executable, "-B", str(self.fixture), "sleep",
             str(bystander_events), str(bystander_marker)])
        self.children.append(bystander)
        time.sleep(0.2)
        done, payload, _e, _s, _m = self.run_watchdog("loop", marker=self.dir / "ours-marker.txt")
        self.assertEqual(done.returncode, watchdog.EXIT_TRIGGERED)
        time.sleep(0.2)
        self.assertIsNone(bystander.poll(), "an unrelated process must not be touched")
        self.assertEqual(bystander_marker.read_text(encoding="utf-8").splitlines(), ["started"])
        self.assertNotEqual(payload["runner"]["pid"], bystander.pid)

    def test_preconditions_refuse_without_starting_a_child(self):
        # A stale events file in an existing run directory is refused.
        stale = self.dir / "stale-run"
        stale.mkdir()
        (stale / "events.jsonl").write_text("", encoding="utf-8")
        marker = self.dir / "precondition-marker.txt"
        done, payload, _e, sidecar, _m = self.run_watchdog(
            "loop", events=stale / "events.jsonl",
            sidecar=self.dir / "precondition-sidecar.json", marker=marker)
        self.assertEqual(done.returncode, watchdog.EXIT_USAGE)
        self.assertIn("already exists", done.stdout)
        self.assertIsNone(payload)
        self.assertFalse(sidecar.exists())
        self.assertFalse(marker.exists(), "no child may be started")

        # A run directory that already holds content (not just events.jsonl) is refused.
        used = self.dir / "used-run"
        used.mkdir()
        (used / "plan.json").write_text("{}", encoding="utf-8")
        marker = self.dir / "used-marker.txt"
        done, _payload, _e, _s, _m = self.run_watchdog(
            "loop", events=used / "events.jsonl",
            sidecar=self.dir / "used-sidecar.json", marker=marker)
        self.assertEqual(done.returncode, watchdog.EXIT_USAGE)
        self.assertIn("stale run", done.stdout)
        self.assertFalse(marker.exists())

        sidecar = self.dir / "taken-sidecar.json"
        sidecar.write_text("{}", encoding="utf-8")
        marker = self.dir / "taken-marker.txt"
        done, _payload, _e, _s, _m = self.run_watchdog(
            "loop", events=self.dir / "fresh-run" / "events.jsonl", sidecar=sidecar, marker=marker)
        self.assertEqual(done.returncode, watchdog.EXIT_USAGE)
        self.assertFalse(marker.exists())
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "{}")

        done, _payload, _e, _s, _m = self.run_watchdog(
            "loop", events=self.dir / "missing-sidecar-dir" / "sub" / "events.jsonl",
            sidecar=self.dir / "absent-dir" / "sidecar.json", marker=self.dir / "missing-marker.txt")
        self.assertEqual(done.returncode, watchdog.EXIT_USAGE)
        self.assertFalse((self.dir / "missing-marker.txt").exists())

    def test_the_sidecar_is_exclusive(self):
        events = self.dir / "exclusive-run" / "events.jsonl"
        sidecar = self.dir / "exclusive-sidecar.json"
        marker = self.dir / "exclusive-marker.txt"
        done, payload, _e, _s, _m = self.run_watchdog(
            "two_groups", events=events, sidecar=sidecar, marker=marker)
        self.assertEqual(done.returncode, watchdog.EXIT_OK)
        first = sidecar.read_bytes()
        marker.unlink()
        second_events = self.dir / "exclusive-run-2" / "events.jsonl"
        done, _payload, _e, _s, _m = self.run_watchdog(
            "two_groups", events=second_events, sidecar=sidecar,
            marker=self.dir / "exclusive-marker-2.txt")
        self.assertEqual(done.returncode, watchdog.EXIT_USAGE)
        self.assertEqual(sidecar.read_bytes(), first)




class NewRunDirectoryContractTests(unittest.TestCase):
    """The run directory is created by the real runner, never by the watchdog.

    The production entry point (`ensure_output_dir`) refuses ANY existing path, so
    the watchdog must accept a not-yet-existing `events.parent` and must not create
    or occupy it. These tests use the real production function and the real CLI.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-watchdog-dir-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fixture = self.dir / "fixture_runner.py"
        self.fixture.write_text(FIXTURE_RUNNER, encoding="utf-8")
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_a_child_using_the_production_ensure_output_dir_is_stopped(self):
        """The production directory contract still triggers the loop stop."""
        run_dir = self.dir / "new-run"
        sidecar = self.dir / "production-sidecar.json"
        marker = self.dir / "production-marker.txt"
        self.assertFalse(run_dir.exists())
        command = [sys.executable, "-B", str(ROOT / "scripts/deployment/ae_re_query_watchdog.py"),
                   "--events", str(run_dir / "events.jsonl"), "--sidecar", str(sidecar),
                   "--poll-seconds", "0.05", "--max-seconds", "20", "--grace-seconds", "3",
                   "--", sys.executable, "-B", str(self.fixture), "production",
                   str(run_dir / "events.jsonl"), str(marker),
                   str(ROOT / "scripts/ae_01_cloud_re_pair.py")]
        done = subprocess.run(command, capture_output=True, text=True, timeout=60)
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(done.returncode, watchdog.EXIT_TRIGGERED, done.stderr)
        self.assertEqual(payload["monitor"]["status"], "WATCHDOG_TRIGGERED_STOPPED")
        self.assertEqual(payload["trigger"]["after_tool_results"], 6)
        self.assertEqual(payload["trigger"]["repeated_query_results"], 6)
        self.assertTrue((run_dir / "events.jsonl").is_file())
        self.assertFalse(payload["new_run_directory"]["existed_at_start"])
        self.assertFalse(payload["new_run_directory"]["created_or_occupied_by_watchdog"])
        self.assertEqual(payload["new_run_directory"]["created_by"], "runner")

    def test_the_real_cli_plan_runs_under_the_watchdog_in_a_new_directory(self):
        """PLAN needs only fixed local inputs; the new directory is fine."""
        config = ROOT / "configs/ae-01__re-pair__siliconflow.pacing-candidate.json"
        source = Path(os.environ.get("AE_VITA_SOURCE", "/tmp/ae01-vita-8WHDvk/source"))
        dataset = Path(os.environ.get(
            "AE_VITA_DATASET", "/tmp/ae01-vita-8WHDvk/tasks-full-a4553e1.json"))
        for label, path, override in (("AE_VITA_SOURCE", source, os.environ.get("AE_VITA_SOURCE")),
                                      ("AE_VITA_DATASET", dataset, os.environ.get("AE_VITA_DATASET"))):
            if override and not path.exists():
                raise RuntimeError(f"{label} is set but missing: {path}")
            self.assertTrue(path.exists(), f"fixed input missing: {path}")
        run_dir = self.dir / "plan-run"
        sidecar = self.dir / "plan-sidecar.json"
        command = [sys.executable, "-B", str(ROOT / "scripts/deployment/ae_re_query_watchdog.py"),
                   "--events", str(run_dir / "events.jsonl"), "--sidecar", str(sidecar),
                   "--poll-seconds", "0.1", "--max-seconds", "300", "--grace-seconds", "5",
                   "--", sys.executable, "-B", str(ROOT / "scripts/ae_01_cloud_re_pair.py"),
                   "--stage", "plan", "--config", str(config), "--dataset", str(dataset),
                   "--vita-source", str(source), "--output-dir", str(run_dir)]
        self.assertFalse(run_dir.exists())
        done = subprocess.run(command, capture_output=True, text=True, timeout=330)
        self.assertEqual(done.returncode, watchdog.EXIT_OK, done.stderr)
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(payload["monitor"]["status"], "RUNNER_EXITED_NO_TRIGGER")
        self.assertEqual(payload["runner"]["returncode"], 0)
        self.assertIsNone(payload["trigger"])
        self.assertTrue((run_dir / "plan.json").is_file(), done.stdout + done.stderr)
        self.assertFalse(payload["new_run_directory"]["existed_at_start"])
        self.assertFalse((run_dir / "events.jsonl").exists())


class RunnerFailureExitCodeTests(unittest.TestCase):
    """A failing child is never reported as success and is never restarted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-watchdog-exit-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fixture = self.dir / "fixture_runner.py"
        self.fixture.write_text(FIXTURE_RUNNER, encoding="utf-8")

    def invoke(self, mode, *, run_dir):
        sidecar = self.dir / f"{mode}-sidecar.json"
        marker = self.dir / f"{mode}-marker.txt"
        command = [sys.executable, "-B", str(ROOT / "scripts/deployment/ae_re_query_watchdog.py"),
                   "--events", str(run_dir / "events.jsonl"), "--sidecar", str(sidecar),
                   "--poll-seconds", "0.05", "--max-seconds", "20", "--grace-seconds", "3",
                   "--", sys.executable, "-B", str(self.fixture), mode,
                   str(run_dir / "events.jsonl"), str(marker)]
        done = subprocess.run(command, capture_output=True, text=True, timeout=60)
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        return done, payload, marker

    def test_exit_zero_is_success(self):
        done, payload, marker = self.invoke("two_groups", run_dir=self.dir / "ok-run")
        self.assertEqual(done.returncode, watchdog.EXIT_OK, done.stderr)
        self.assertEqual(payload["runner"]["returncode"], 0)
        self.assertEqual(payload["monitor"]["status"], "RUNNER_EXITED_NO_TRIGGER")

    def test_exit_seven_is_not_success(self):
        done, payload, marker = self.invoke("exit7", run_dir=self.dir / "seven-run")
        self.assertEqual(done.returncode, watchdog.EXIT_RUNNER_FAILED, done.stdout)
        self.assertEqual(payload["runner"]["returncode"], 7)
        self.assertEqual(payload["monitor"]["status"], "RUNNER_EXITED_NO_TRIGGER")
        self.assertIsNone(payload["trigger"])
        self.assertFalse(payload["diagnostic"]["scoring_performed"])
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["started"])

    def test_a_signal_terminated_child_is_not_success(self):
        done, payload, marker = self.invoke("signal", run_dir=self.dir / "signal-run")
        self.assertEqual(done.returncode, watchdog.EXIT_RUNNER_FAILED, done.stdout)
        self.assertEqual(payload["runner"]["returncode"], -15)
        self.assertEqual(payload["monitor"]["status"], "RUNNER_EXITED_NO_TRIGGER")
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["started"])

    def test_the_outer_shell_runs_its_cleanup_on_success_and_on_failure(self):
        """The existing shell owns cleanup; the watchdog never claims it ran."""
        cleanup = self.dir / "cleanup.log"
        runner = ROOT / "scripts/deployment/ae_re_query_watchdog.py"
        outcomes = (("two_groups", "cleanup 0"), ("exit7", f"cleanup {watchdog.EXIT_RUNNER_FAILED}"))
        for index, (mode, expected) in enumerate(outcomes):
            run_dir = self.dir / f"shell-{index}-run"
            sidecar = self.dir / f"shell-{index}-sidecar.json"
            marker = self.dir / f"shell-{index}-marker.txt"
            script = self.dir / f"outer-{index}.sh"
            script.write_text(
                "#!/bin/sh\n"
                f'"{sys.executable}" -B "{runner}" --events "{run_dir / "events.jsonl"}" '
                f'--sidecar "{sidecar}" --poll-seconds 0.05 --max-seconds 20 --grace-seconds 3 -- '
                f'"{sys.executable}" -B "{self.fixture}" {mode} '
                f'"{run_dir / "events.jsonl"}" "{marker}"\n'
                "code=$?\n"
                f'echo "cleanup $code" >> "{cleanup}"\n'
                "exit $code\n", encoding="utf-8")
            os.chmod(script, 0o700)
            done = subprocess.run([str(script)], capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode,
                             watchdog.EXIT_OK if mode == "two_groups"
                             else watchdog.EXIT_RUNNER_FAILED, done.stderr)
        self.assertEqual(cleanup.read_text(encoding="utf-8").splitlines(),
                         ["cleanup 0", f"cleanup {watchdog.EXIT_RUNNER_FAILED}"])


if __name__ == "__main__":
    unittest.main()
