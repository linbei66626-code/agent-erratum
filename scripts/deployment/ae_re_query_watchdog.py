#!/usr/bin/env python3
"""Explicit outer watchdog for one AE R/E run: stop a repeating READ query loop.

    .venv-vita/bin/python -B scripts/deployment/ae_re_query_watchdog.py \
        --events /root/agent-erratum/deployment/runs/<new-run>/events.jsonl \
        --sidecar /root/agent-erratum/deployment/<run>/watchdog.json \
        -- .venv-vita/bin/python -B scripts/ae_01_cloud_re_pair.py --stage run ...

The runner must be a DIRECT python process (as above). The outer shell that calls
this wrapper is the one that performs the existing service cleanup, in every
outcome; the watchdog deliberately does not wrap a shell that would leave
grandchildren behind and then claim the run was cleaned up.

`events` is the NEW run directory's `events.jsonl`. That directory may not exist
yet: it is expected to be created by the real runner (the production
`ensure_output_dir` refuses any existing path). The watchdog never creates or
occupies it, and it refuses a run directory that already holds content.

This is a CANDIDATE run-protection rule, not a model-capability fix and not a
security sandbox. It starts exactly one explicitly named child runner, watches
exactly one new `events.jsonl` that must not exist yet, and, when the candidate
pattern below holds, sends SIGTERM to THAT child only (escalating to SIGKILL of the
same pid after a grace period). It never scans for processes by name, never signals
anything it did not start, and never restarts the runner. The existing run script
keeps its own service cleanup and result handling: an active stop leaves no
`result.json`, and the diagnostic stays incomplete.

Candidate v1 trigger rule (all conditions, phase `task`, same arm, same continuous
interaction):

* a period-1 or period-2 cycle of successful `get_delivery_store_info` /
  `get_delivery_product_info` results, repeated for 3 complete groups;
* the signature is the tool name, the strictly parsed and normalized full
  arguments, the sha256 of the exact `tool_return` text and the `block_sha`; it
  excludes the per-call id, but every id must still differ and an event must not be
  counted twice;
* anything else resets the run: another real tool result, a user message, a
  memory/block change, a phase/arm/task switch, an error result, different
  arguments or a different return, a duplicate call id or a replayed event.

Only complete, newline-terminated JSON lines are considered. An incomplete final
line never triggers; a COMPLETE line that is not valid JSON is reported as a
monitoring failure and stops the child instead of being treated as success.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

PROFILE_VERSION = "ae-re-query-watchdog-1"
QUERY_TOOLS = ("get_delivery_store_info", "get_delivery_product_info")
PERIOD_MAX = 2
REQUIRED_GROUPS = 3
# Transport bookkeeping and the per-batch summary are not real actions, so they
# neither count nor reset. Everything else that is not a tool result resets.
IGNORED_BRIDGE_EVENTS = ("request", "response", "multicall_batch")
DEFAULT_POLL_SECONDS = 0.2
DEFAULT_MAX_SECONDS = 3600.0
DEFAULT_GRACE_SECONDS = 10.0

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_TRIGGERED = 3
EXIT_TIMEOUT = 4
EXIT_MONITOR_FAILED = 5
EXIT_RUNNER_FAILED = 6

RULE = {
    "profile_version": PROFILE_VERSION,
    "kind": "candidate_run_protection_not_a_model_fix",
    "trigger_scope": {"phase": "task", "same_arm": True,
                      "same_continuous_interaction": True, "applies_to_arms": ["rewrite", "erratum"]},
    "query_tools": list(QUERY_TOOLS),
    "period_max": PERIOD_MAX,
    "required_groups": REQUIRED_GROUPS,
    "signature_fields": ["tool", "normalized_arguments", "tool_return_sha256", "block_sha"],
    "signature_excludes": ["tool_call_id (each id must still differ)"],
    "ignored_bridge_events": list(IGNORED_BRIDGE_EVENTS),
    "reset_on": ["other real tool result", "user message", "memory/block change",
                 "phase, arm or task switch", "error result", "changed arguments",
                 "changed tool_return", "duplicate call id", "replayed event"],
    "incomplete_line": "never triggers",
    "complete_malformed_line": "monitoring failure, never success",
    "stops_only": "the single child process this wrapper started",
    "runner_shape": "a direct python process; the outer existing shell owns service cleanup",
    "process_scan_or_name_kill": False,
    "restarts_performed": 0,
    "security_sandbox": False,
}


class MonitoringFailure(RuntimeError):
    """The event stream cannot be judged any more; the child must be stopped."""


class WatchdogRefused(RuntimeError):
    """A precondition was not met; no child process has been started."""


@dataclass
class Trigger:
    period: int
    repeated_query_results: int
    after_tool_results: int
    groups: list = field(default_factory=list)
    sequence: object = None
    timestamp: object = None


def _normalized_arguments(value):
    """Strictly parse and normalize the full arguments; raise on any defect."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise MonitoringFailure(f"query arguments are not valid JSON: {exc}") from None
    elif isinstance(value, dict):
        parsed = value
    else:
        raise MonitoringFailure("query arguments are neither a JSON string nor an object")
    if not isinstance(parsed, dict):
        raise MonitoringFailure("query arguments are not a JSON object")
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class QueryLoopDetector:
    """Candidate v1 detector over the driver's `events.jsonl` rows.

    Feed complete lines (or rows) in order. The detector never decides from the
    tool name or the call count alone: every entry of a group must carry the same
    signature, and any other real action resets the run.
    """

    def __init__(self, *, query_tools=QUERY_TOOLS, period_max=PERIOD_MAX,
                 required_groups=REQUIRED_GROUPS):
        self.query_tools = tuple(query_tools)
        self.period_max = int(period_max)
        self.required_groups = int(required_groups)
        self.run = []
        self.seen_ids = set()
        self.all_seen_ids = set()
        self.seen_sequences = set()
        self.tool_results = 0
        self.query_results = 0
        self.skipped_bridge_events = 0
        self.resets = []
        self.trigger = None
        self.after_trigger_query_results = 0
        self.arm = self.phase = self.task = None
        self.block_sha = None
        self.complete_lines = 0

    # -- feeding ---------------------------------------------------------
    def feed_line(self, line):
        text = line.strip()
        if not text:
            return
        try:
            row = json.loads(text)
        except ValueError as exc:
            raise MonitoringFailure(f"complete event line is not valid JSON: {exc}") from None
        self.complete_lines += 1
        self.feed_row(row)

    def feed_row(self, row):
        if not isinstance(row, dict):
            raise MonitoringFailure("event row is not a JSON object")
        if self.trigger is not None:
            self._after_trigger(row)
            return
        sequence = row.get("sequence")
        if sequence is not None:
            if sequence in self.seen_sequences:
                self._reset("replayed_event")
                return
            self.seen_sequences.add(sequence)
        kind = row.get("kind")
        if kind != "bridge_event":
            if kind == "stage_start":
                self._reset("stage_start")
                self.arm, self.phase, self.task = row.get("arm"), "task", row.get("task")
            else:
                self._reset(f"lifecycle:{kind}")
            return
        event = row.get("event")
        if not isinstance(event, dict):
            raise MonitoringFailure("bridge_event row has no event object")
        event_kind = event.get("kind")
        if event_kind in IGNORED_BRIDGE_EVENTS:
            self.skipped_bridge_events += 1
            return
        if event_kind != "client_tool_result":
            self._reset(f"bridge_event:{event_kind}")
            return
        self._tool_result(row, event)

    # -- internals --------------------------------------------------------
    def _after_trigger(self, row):
        """After a trigger, only keep counting evidence (the run already stopped)."""
        if row.get("kind") != "bridge_event":
            return
        event = row.get("event") or {}
        if event.get("kind") != "client_tool_result":
            return
        self.tool_results += 1
        if ((event.get("result") or {}).get("status") == "success"
                and (event.get("call") or {}).get("name") in self.query_tools):
            self.after_trigger_query_results += 1

    def _reset(self, reason):
        self.run = []
        self.seen_ids = set()
        if len(self.resets) < 64:
            self.resets.append(reason)

    def _tool_result(self, row, event):
        call, result = event.get("call"), event.get("result")
        if not isinstance(call, dict) or not isinstance(result, dict):
            raise MonitoringFailure("tool result event lacks a call/result object")
        arm, phase, task = row.get("arm"), row.get("phase"), row.get("task")
        if (arm, phase, task) != (self.arm, self.phase, self.task):
            self._reset("scope_changed")
            self.arm, self.phase, self.task = arm, phase, task
            return
        if phase != "task":
            self._reset("not_task_phase")
            return
        call_id = call.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise MonitoringFailure("tool result event has no call id")
        if call_id in self.seen_ids or call_id in self.all_seen_ids:
            self._reset("duplicate_call_id")
            return
        name = call.get("name")
        if not isinstance(name, str) or not name:
            raise MonitoringFailure("tool result event has no tool name")
        self.tool_results += 1
        if name not in self.query_tools:
            self._reset(f"other_tool:{name}")
            return
        if result.get("status") != "success":
            self._reset(f"query_status:{result.get('status')}")
            return
        normalized = _normalized_arguments(call.get("arguments"))
        tool_return = result.get("tool_return")
        if not isinstance(tool_return, str):
            raise MonitoringFailure("tool_return is not a string")
        block_sha = event.get("block_sha")
        if not isinstance(block_sha, str) or not block_sha:
            raise MonitoringFailure("tool result event has no block_sha")
        if self.block_sha is not None and block_sha != self.block_sha:
            self._reset("memory_block_changed")
        self.block_sha = block_sha
        signature = {
            "tool": name,
            "normalized_arguments": normalized,
            "tool_return_sha256": hashlib.sha256(tool_return.encode("utf-8")).hexdigest(),
            "block_sha": block_sha,
        }
        self.seen_ids.add(call_id)
        self.all_seen_ids.add(call_id)
        self.query_results += 1
        self._append({"call_id": call_id, "signature": signature,
                      "sequence": row.get("sequence"), "timestamp": row.get("timestamp")})

    def _append(self, entry):
        """Keep the longest period-1/2-consistent suffix of query results.

        A result that breaks the established cycle restarts the candidate run, so a
        changed argument or return can never be part of a matching group; it also
        means a genuine loop needs three COMPLETE fresh groups after any break.
        """
        signature = entry["signature"]
        if len(self.run) < 2:
            self.run.append(entry)
            self._check_trigger()
            return
        period = 1 if self.run[0]["signature"] == self.run[1]["signature"] else 2
        if signature == self.run[-period]["signature"]:
            self.run.append(entry)
        else:
            self._reset("signature_changed")
            self.run = [entry]
        self._check_trigger()

    def _check_trigger(self):
        for period in range(1, self.period_max + 1):
            span = period * self.required_groups
            if len(self.run) < span:
                continue
            groups = [[entry["signature"] for entry in
                       self.run[len(self.run) - span + index * period:
                                len(self.run) - span + (index + 1) * period]]
                      for index in range(self.required_groups)]
            if all(group == groups[0] for group in groups[1:]):
                last = self.run[-1]
                self.trigger = Trigger(
                    period=period, repeated_query_results=span,
                    after_tool_results=self.tool_results,
                    groups=[entry["signature"] for entry in self.run[-span:]],
                    sequence=last.get("sequence"), timestamp=last.get("timestamp"))
                return


class EventTail:
    """Incremental bytes -> complete lines. Partial lines are never emitted."""

    def __init__(self):
        self.buffer = b""
        self.digest = hashlib.sha256()
        self.lines = 0

    def feed(self, chunk: bytes):
        complete = []
        self.buffer += chunk
        while b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MonitoringFailure(f"event line is not valid UTF-8: {exc}") from None
            self.digest.update(raw + b"\n")
            self.lines += 1
            complete.append(text)
        return complete


def build_sidecar(*, events, sidecar, argv, pid, state, detector, monitored_sha256,
                  run_dir_preexisted=None):
    trigger = None
    if detector.trigger is not None:
        trigger = {
            "period": detector.trigger.period,
            "repeated_query_results": detector.trigger.repeated_query_results,
            "after_tool_results": detector.trigger.after_tool_results,
            "sequence": detector.trigger.sequence,
            "timestamp": detector.trigger.timestamp,
            "groups": [{"tool": entry["tool"],
                        "normalized_arguments": entry["normalized_arguments"],
                        "tool_return_sha256": entry["tool_return_sha256"],
                        "block_sha": entry["block_sha"]} for entry in detector.trigger.groups],
        }
    active_stop = state["status"] in ("WATCHDOG_TRIGGERED_STOPPED", "WATCHDOG_TIMEOUT_STOPPED",
                                      "MONITORING_FAILED_STOPPED")
    return {
        "schema": "ae-re-query-watchdog-sidecar-1",
        "profile_version": PROFILE_VERSION,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule": RULE,
        "events_path": str(events),
        "new_run_directory": {
            "path": str(Path(events).parent),
            "existed_at_start": run_dir_preexisted,
            "created_by": ("preexisting_empty_directory" if run_dir_preexisted else "runner"),
            "created_or_occupied_by_watchdog": False,
            "contract": ("the new run directory must be absent (the production CLI refuses any "
                         "existing path) or an empty directory; content means a stale run"),
        },
        "runner": {"argv": list(argv), "pid": pid, "returncode": state["runner_returncode"],
                   "signal_sent": state["signal_sent"], "killed_after_grace": state["killed"]},
        "monitor": {"status": state["status"], "complete_lines": detector.complete_lines,
                    "tool_results": detector.tool_results, "query_results": detector.query_results,
                    "skipped_bridge_events": detector.skipped_bridge_events,
                    "resets": list(detector.resets),
                    "monitored_sha256": monitored_sha256,
                    "monitored_scope": "complete newline-terminated lines only"},
        "trigger": trigger,
        "observed_query_results_after_trigger": detector.after_trigger_query_results,
        "monitor_failure": state["monitor_failure"],
        "diagnostic": {
            "result_json_produced_by_this_wrapper": False,
            "scoring_performed": False,
            "audit_relaxed": False,
            "driver_result_faked": False,
            "status_on_active_stop": ("WATCHDOG_STOPPED_DIAGNOSTIC_INCOMPLETE"
                                      if active_stop else None),
            "note": ("an active stop leaves no driver result.json; the pair stays incomplete "
                     "and the existing audit is not loosened"),
        },
        "watchdog": {"stopped_scope": "own_child_only", "process_scan_or_name_kill": False,
                     "restarts_performed": 0, "security_sandbox": False,
                     "candidate_only": True},
    }


def _terminate(child, grace_seconds, state):
    if child.poll() is None:
        state["signal_sent"] = "SIGTERM"
        child.terminate()
        try:
            child.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            state["killed"] = True
            child.kill()
            child.wait(timeout=grace_seconds)
    state["runner_returncode"] = child.returncode


def run_watchdog(*, events: Path, sidecar: Path, argv, poll_seconds=DEFAULT_POLL_SECONDS,
                 max_seconds=DEFAULT_MAX_SECONDS, grace_seconds=DEFAULT_GRACE_SECONDS,
                 start_child=True) -> dict:
    """Start the child and watch one new events file; write the exclusive sidecar."""
    events, sidecar = Path(events), Path(sidecar)
    if events.exists():
        raise WatchdogRefused(f"events path already exists; refusing a stale run: {events}")
    if sidecar.exists():
        raise WatchdogRefused(f"sidecar path already exists; refusing to overwrite: {sidecar}")
    run_dir = events.parent
    run_dir_preexisted = run_dir.exists()
    if run_dir_preexisted:
        if not run_dir.is_dir():
            raise WatchdogRefused(f"events parent exists and is not a directory: {run_dir}")
        if any(run_dir.iterdir()):
            raise WatchdogRefused(
                f"the new run directory already holds content; refusing a stale run: {run_dir}")
    if not sidecar.parent.is_dir():
        raise WatchdogRefused(f"sidecar directory does not exist: {sidecar.parent}")
    detector = QueryLoopDetector()
    tail = EventTail()
    state = {"status": "RUNNING", "signal_sent": None, "killed": False,
             "runner_returncode": None, "monitor_failure": None}
    child = None
    handle = None
    started = time.monotonic()
    try:
        child = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, start_new_session=True)
        while True:
            if handle is None and events.is_file():
                handle = events.open("rb")
            if handle is not None:
                chunk = handle.read()
                if chunk:
                    for line in tail.feed(chunk):
                        detector.feed_line(line)
                    if detector.trigger is not None:
                        _terminate(child, grace_seconds, state)
                        state["status"] = "WATCHDOG_TRIGGERED_STOPPED"
                        break
            if child.poll() is not None:
                state["runner_returncode"] = child.returncode
                state["status"] = "RUNNER_EXITED_NO_TRIGGER"
                break
            if time.monotonic() - started > max_seconds:
                _terminate(child, grace_seconds, state)
                state["status"] = "WATCHDOG_TIMEOUT_STOPPED"
                break
            time.sleep(poll_seconds)
    except MonitoringFailure as exc:
        state["monitor_failure"] = str(exc)
        if child is not None:
            _terminate(child, grace_seconds, state)
        state["status"] = "MONITORING_FAILED_STOPPED"
    finally:
        if handle is not None:
            handle.close()
        if child is not None and child.poll() is None:  # never leave our child behind
            _terminate(child, grace_seconds, state)
    payload = build_sidecar(events=events, sidecar=sidecar, argv=argv, pid=child.pid if child else None,
                            state=state, detector=detector,
                            monitored_sha256=tail.digest.hexdigest(),
                            run_dir_preexisted=run_dir_preexisted)
    with sidecar.open("x", encoding="utf-8") as out:  # exclusive create
        json.dump(payload, out, ensure_ascii=False, indent=2)
        out.write("\n")
    return {"status": state["status"], "sidecar": str(sidecar), "trigger": payload["trigger"],
            "runner_returncode": state["runner_returncode"], "lines": detector.complete_lines}


def _exit_code(status, runner_returncode=None):
    """0 only for a runner that exited 0 with no anomaly.

    A non-zero exit or a signal-terminated runner is never reported as success; the
    real returncode stays in the sidecar. Active stops keep their own codes.
    """
    if status == "RUNNER_EXITED_NO_TRIGGER":
        return EXIT_OK if runner_returncode == 0 else EXIT_RUNNER_FAILED
    return {"WATCHDOG_TRIGGERED_STOPPED": EXIT_TRIGGERED,
            "WATCHDOG_TIMEOUT_STOPPED": EXIT_TIMEOUT,
            "MONITORING_FAILED_STOPPED": EXIT_MONITOR_FAILED}.get(status, EXIT_FAILED)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--events", type=Path, required=True,
                        help="the NEW run's events.jsonl; it must not exist yet")
    parser.add_argument("--sidecar", type=Path, required=True,
                        help="exclusive JSON result path; it must not exist yet")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
                        help="watchdog lifetime bound; on expiry the child is stopped")
    parser.add_argument("--grace-seconds", type=float, default=DEFAULT_GRACE_SECONDS)
    parser.add_argument("runner", nargs=argparse.REMAINDER,
                        help="-- RUNNER [ARGS...] (explicit; no shell)")
    args = parser.parse_args(argv)
    runner = list(args.runner)
    if runner and runner[0] == "--":
        runner = runner[1:]
    if not runner:
        parser.error("an explicit runner command is required after --")
    if min(args.poll_seconds, args.max_seconds, args.grace_seconds) <= 0:
        parser.error("poll/max/grace seconds must be positive")
    try:
        outcome = run_watchdog(events=args.events, sidecar=args.sidecar, argv=runner,
                               poll_seconds=args.poll_seconds, max_seconds=args.max_seconds,
                               grace_seconds=args.grace_seconds)
    except WatchdogRefused as exc:
        print(json.dumps({"watchdog_refused": str(exc), "child_started": False},
                         ensure_ascii=False, indent=2))
        return EXIT_USAGE
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return _exit_code(outcome["status"], outcome["runner_returncode"])


if __name__ == "__main__":
    raise SystemExit(main())
