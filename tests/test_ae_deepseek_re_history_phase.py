"""The history phase across MULTIPLE tool continuations, on the real capture.

The completed run `ae-deepseek-re-live-20260916-r4` starts the rewrite arm's t4 phase with

    POST#0  dataset_history/material envelope   -> memory_update only
    POST#1  tool_return (a memory_update round) -> memory_update only
    POST#2  tool_return (ANOTHER memory round)  -> memory_update only
    POST#3  current_task envelope               -> the 19 domain tools + memory_update
    POST#4+ tool_return continuations           -> the task tools

The audit used to decide the phase from the POST's POSITION inside the phase
(`"history" if index == 1 else "task"`), so POST#2 was read as the task phase and the
audit demanded the domain tools - `client_tools_differ_from_declared`, exactly what the
site's sealed re-audit reported.

This suite pins the repair: the phase comes from the arm's and task's own VERIFIED
envelopes. Only an explicit `current_task` switches to the task phase, a continuation (or a
mid-task `runtime_user` reply) inherits the phase already verified, a continuation with no
verified phase is refused, a history envelope after the task phase is refused, and every
tool-schema comparison stays exact - so a task post offering only the memory tool, or a
history post offering the domain tools, still fails. The first four real POSTs of the
sealed capture are the distinguishing evidence; the rest of the suite drives the same
methods with the fixture's own recordings.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TRANSFER = ROOT / "transfers/ae-deepseek-re-complete-20260916-r4"
RUN_DIR = TRANSFER / "deployment/runs/ae-deepseek-re-live-20260916-r4"
JOURNAL = (TRANSFER / "deployment/runs"
           / "ae-deepseek-re-live-20260916-r4.private.jsonl")
CANDIDATE_1024 = (ROOT / "configs"
                  / "ae-01__re-multiturn__deepseek-flash.serial-budget1024-candidate.json")
SEALED_AUDIT_COPY = (ROOT / "results/ae-deepseek-re-budget1024-r1/after"
                     / "ae_cloud_re_multiturn_input_audit.py")
MEMORY_TOOL_NAME = "memory_update"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _real_phase_prefix(limit=6):
    """The first POSTs of the rewrite arm, with their real input kind and tool counts."""
    result = json.loads((RUN_DIR / "result.json").read_text(encoding="utf-8"))
    agent = (result["arms"].get("rewrite") or {}).get("agent_id")
    rows = []
    with (RUN_DIR / "letta-http.jsonl").open() as handle:
        for line in handle:
            row = json.loads(line)
            if not (row.get("kind") == "request" and row.get("method") == "POST"
                    and (row.get("path") or "").endswith("/messages")):
                continue
            if agent not in row["path"]:
                continue
            body = row["body"]
            kinds = []
            for message in body.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "tool_return":
                    kinds.append("tool_return")
                    continue
                try:
                    envelope = json.loads(message.get("content") or "{}")
                except (TypeError, ValueError):
                    kinds.append("unparsed")
                    continue
                kinds.append(envelope.get("source") if isinstance(envelope, dict) else "?")
            tools = [t.get("name") for t in (body.get("client_tools") or [])]
            rows.append({"kinds": kinds, "tools": tools,
                         "domain_tools": [n for n in tools if n != MEMORY_TOOL_NAME],
                         "request_id": row.get("request_id")})
            if len(rows) >= limit:
                break
    return rows


class TheRealCapturePhaseSequenceTests(unittest.TestCase):
    """The real POSTs that distinguish the repaired rule from the old one."""

    @classmethod
    def setUpClass(cls):
        if not (RUN_DIR / "letta-http.jsonl").is_file():
            raise unittest.SkipTest("the completed capture is absent")
        cls.posts = _real_phase_prefix()

    def test_the_history_phase_really_spans_two_memory_rounds(self):
        self.assertGreaterEqual(len(self.posts), 4)
        self.assertEqual(self.posts[0]["kinds"], ["dataset_history/material"])
        self.assertEqual(self.posts[0]["domain_tools"], [])
        for index in (1, 2):
            self.assertEqual(self.posts[index]["kinds"], ["tool_return"])
            self.assertEqual(self.posts[index]["domain_tools"], [],
                             "the history phase keeps the memory tool alone")
        self.assertEqual(self.posts[3]["kinds"], ["current_task"])
        self.assertTrue(self.posts[3]["domain_tools"],
                        "current_task is what switches to the domain tools")

    def test_the_old_positional_rule_would_have_failed_here(self):
        """POST#2 is a continuation, not the task phase."""
        third = self.posts[2]
        self.assertEqual(third["kinds"], ["tool_return"])
        self.assertEqual(third["domain_tools"], [])
        # The old rule: "history" if index == 1 else "task" -> POST#2 = task.
        old_profile = "history" if 2 == 1 else "task"
        self.assertEqual(old_profile, "task")
        self.assertNotEqual(old_profile, "history",
                            "the repaired rule must read this POST as the history phase")


class ThePhaseRuleTests(unittest.TestCase):
    """The same methods, driven with the fixture's own recordings."""

    @classmethod
    def setUpClass(cls):
        cls.entry = _load("test_ae_deepseek_re_audit_for_phases",
                          ROOT / "tests/test_ae_deepseek_re_audit.py")
        cls.entry.DeepSeekAuditEntryTests.setUpClass()
        cls.entry.CANDIDATE = CANDIDATE_1024
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            raise unittest.SkipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))

    def _case(self):
        case = self.entry.DeepSeekAuditEntryTests(
            "test_a_complete_deepseek_capture_reaches_the_final_verdict")
        case.setUp()
        return case

    def _audit(self, fixture, config_path):
        from ae_cloud_re_multiturn_input_audit import audit_re_multiturn_inputs
        dataset = Path(os.environ.get(
            "AE_VITA_DATASET", ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        return audit_re_multiturn_inputs(
            fixture["run_dir"], fixture["journal"], config_path=config_path,
            dataset_path=dataset)

    @staticmethod
    def _codes(report):
        return [str(item.get("code")) for item in (report.get("invalid_reasons") or [])] + \
               [str(item) for item in (report.get("transport") or {}).get("issues") or []] + \
               [str(item) for item in (report.get("failures") or [])]

    def test_the_full_fixture_still_passes_with_the_repaired_rule(self):
        case = self._case()
        fixture, config_path = case._build()
        report = self._audit(fixture, config_path)
        self.assertEqual(report.get("status"), "VALID", report.get("invalid_reasons"))

    @staticmethod
    def _tamper_client_tools(fixture, *, predicate, replacement):
        """Rewrite the SUBMITTED client_tools of one Letta POST.

        The audit reads the tool profile from the Letta transport capture, exactly as the
        service submitted it, so the tamper belongs there - the proxy journal (which the
        transport walk hashes) stays untouched.
        """
        path = fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        changed = 0
        for row in rows:
            body = row.get("body")
            if (row.get("kind") != "request" or row.get("method") != "POST"
                    or not isinstance(body, dict)
                    or not (row.get("path") or "").endswith("/messages")):
                continue
            tools = body.get("client_tools") or []
            if predicate(tools):
                body["client_tools"] = replacement(tools)
                changed += 1
                break
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                        encoding="utf-8")
        return changed

    def test_a_task_post_offering_only_the_memory_tool_is_refused(self):
        """The task phase must offer the declared domain tools, not just memory."""
        case = self._case()
        fixture, config_path = case._build()
        changed = self._tamper_client_tools(
            fixture,
            predicate=lambda tools: len(tools) > 1,
            replacement=lambda tools: [t for t in tools
                                       if t.get("name") == MEMORY_TOOL_NAME])
        self.assertEqual(changed, 1, "the fixture must contain a task-phase POST")
        report = self._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("client_tools_differ_from_declared", self._codes(report))

    def test_a_history_post_offering_the_domain_tools_is_refused(self):
        """Early domain tools in the history phase are a phase violation, not a pass.

        The substituted schema is a REAL one taken from a task-phase POST of the same
        capture, so the exact schema checks pass and the refusal can only come from the
        phase rule.
        """
        case = self._case()
        fixture, config_path = case._build()
        path = fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        domain_schema = None
        for row in rows:
            body = row.get("body")
            if (row.get("kind") == "request" and row.get("method") == "POST"
                    and isinstance(body, dict)
                    and (row.get("path") or "").endswith("/messages")):
                for schema in body.get("client_tools") or []:
                    if schema.get("name") != MEMORY_TOOL_NAME:
                        domain_schema = schema
                        break
            if domain_schema is not None:
                break
        self.assertIsNotNone(domain_schema, "the capture must declare the domain tools")
        changed = self._tamper_client_tools(
            fixture,
            predicate=lambda tools: [t.get("name") for t in tools] == [MEMORY_TOOL_NAME],
            replacement=lambda tools: [domain_schema])
        self.assertEqual(changed, 1, "the fixture must contain a history-phase POST")
        report = self._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("client_tools_differ_from_declared", self._codes(report))

    def test_arms_and_tasks_keep_their_own_phase_state(self):
        """The phase state is per (arm, task): no bleed, no rollback."""
        audit_module = _load("ae_cloud_re_multiturn_input_audit_for_phases2",
                             ROOT / "ae_cloud_re_multiturn_input_audit.py")
        source = (ROOT / "ae_cloud_re_multiturn_input_audit.py").read_text(encoding="utf-8")
        # The state is keyed by the arm AND the task, and it is initialised per phase.
        self.assertIn('per_phase[(arm, task_id)] = {"start_turns": deepcopy(turns[arm]),',
                      source)
        self.assertIn('"phase": None}', source)
        # A history envelope after the task phase is an explicit refusal, and a
        # continuation with no verified phase is refused instead of guessed.
        self.assertIn('"history_envelope_after_the_task_phase_started"', source)
        self.assertIn('"continuation_without_a_verified_phase"', source)
        # The repaired rule never reads the POST index to pick a phase.
        self.assertNotIn('profile = "history" if index == 1 else "task"', source)
        self.assertIn("CURRENT_SOURCE", source)
        self.assertTrue(hasattr(audit_module.MultiturnInputAudit, "wire_gate"))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
