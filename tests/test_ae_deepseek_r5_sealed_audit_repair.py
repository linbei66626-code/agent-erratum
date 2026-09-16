"""r5 sealed-result audit repair: the five interface defects, closed.

The sealed r5 capture (18 phases, 258 generation responses, 98 auxiliary calls) was
refused by the offline audit for reasons that were about the AUDIT's reading of a REAL
record, not about the run's own evidence:

1. the audit imported the pinned Vita package without this run's `vita-models.json`, so
   the pinned `vita.config` fell back to the checkout's `models.yaml.example`;
2. the driver looked for `snapshot.simulations`, which the real wrapper never produces
   (`completed` / `last_evaluation` instead), so every phase looked unjudged;
3. the audit required the bridge transcript to EQUAL the scored simulation, but the real
   `NativeVita.finish` converts each row through the pinned classes, prefixes the task id
   and generates a `timestamp` the transcript never carried;
4. the audit ordered auxiliary records by `recorded_at`, which the real native records do
   not have;
5. the sealed protocol requires the judge reply to echo the rubric text verbatim, which 6
   replies of 3 phases do not do; the pinned native evaluator never reads that text.

Those five are one authorised task. This suite pins each of them over the project's own
full-chain fixture (real dataset, real pinned scorer, real proxy journal, real stacked
manifest and receipt) and over the REAL sealed r5 record, and it pins the boundaries:

* the strict verbatim-echo protocol is still the default and still refuses;
* the explicit post-hoc `native-by-id-v1` recheck collects the echo differences, finishes
  the whole chain, and reports `native_semantics_checks_passed` SEPARATELY while
  `input_audit_passed` stays false;
* no other judge defect is swallowed by that mode;
* the auditor identity, the sealed copy and the report file are still enforced.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

R5_RUN = (ROOT / "transfers/ae-deepseek-re-complete-20260916-r5/deployment/runs"
          / "ae-deepseek-re-live-20260916-r5")
R5_JOURNAL = R5_RUN.parent / (R5_RUN.name + ".private.jsonl")
R5_SEALED_COPY = (ROOT / "results/ae-deepseek-re-history-phase-r1/after"
                  / "ae_cloud_re_multiturn_input_audit.py")
#: The runtime copy of the audit module the r5 record pins (plan AND result).
R5_RECORDED_AUDIT_SHA = "51ab42fa69993f3ae5df3f4d608be12515527990c94e4314a70b2f7ae1bf124b"
CANDIDATE = ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-budget1024-candidate.json"
DATASET = Path(os.environ.get("AE_VITA_DATASET",
                              ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FixtureCase(unittest.TestCase):
    """One built 18-phase chain, copied per test so no mutation can leak."""

    @classmethod
    def setUpClass(cls):
        if not DATASET.is_file():
            raise unittest.SkipTest("the fixed dataset is absent")
        cls.chain = _load("re_multiturn_chain_for_r5_repair",
                          ROOT / "tests/re_multiturn_chain.py")
        cls.wiring = _load("stack_wiring_for_r5_repair",
                           ROOT / "tests/test_ae_stack_receipt_wiring.py")
        cls.tmp = Path(tempfile.mkdtemp(prefix="r5-repair-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.fixtures = {"base": cls._build(cls.tmp / "base"),
                        # The real r5 run has six judge replies that omit the criteria's
                        # bracketed examples; this fixture is BUILT with such a reply, so
                        # the reply, the capture and the record carry one text.
                        "echo": cls._build(cls.tmp / "base-echo",
                                           judge_echo_variant_windows={
                                               ("sub_U000828_4", 1)})}
        cls.fixture = cls.fixtures["base"]
        cls.base_run = cls.fixture["run_dir"]
        cls.base_journal = cls.fixture["journal"]

    @classmethod
    def _build(cls, tmp, **kwargs):
        tmp = Path(tmp)
        tmp.mkdir(parents=True, exist_ok=True)
        record = json.loads(CANDIDATE.read_text(encoding="utf-8"))
        record["letta_origin"] = "http://127.0.0.1:8283"
        record["model_origin"] = "http://127.0.0.1:8000"
        config_path = tmp / "deepseek-0.4.json"
        config_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        pair = cls.wiring.StackEvidenceFixture(tmp)
        fixture = cls.chain.build_full_chain(
            tmp, config_path=config_path, patch_stack_manifest=pair.manifest,
            patch_stack_load_receipt=pair.receipt, receipt=False, **kwargs)
        fixture["stack"] = pair
        return fixture

    def setUp(self):
        import ae_multicall
        self._saved_env = {}
        for name in (ae_multicall.LAUNCH_RECEIPT_ENV_VAR,
                     ae_multicall.STACK_RECEIPT_ENV_VAR,
                     ae_multicall.STACK_MANIFEST_ENV_VAR,
                     ae_multicall.STACK_ENV_VAR):
            self._saved_env[name] = os.environ.pop(name, None)
        self.addCleanup(self._restore_env)
        self.work = Path(tempfile.mkdtemp(prefix="r5-case-", dir=str(self.tmp)))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.run_dir = self.work / "run"
        shutil.copytree(self.base_run, self.run_dir)
        self.journal = self.work / "proxy.private.jsonl"
        shutil.copy(self.base_journal, self.journal)
        self.config_path = self.work / "candidate.json"
        shutil.copy(self.fixture["config_path"], self.config_path)
        # Each case is audited with THIS copy's own `vita-models.json`, the way a fresh
        # auditor process would: the pinned package reads its configuration while it is
        # imported, so a `vita.*` left over from the previous case (or from the previous
        # case's deleted directory) must not be reused.
        for name in [name for name in sys.modules
                     if name == "vita" or name.startswith("vita.")]:
            del sys.modules[name]
        os.environ["VITA_MODEL_CONFIG_PATH"] = str(self.vita_models())

    def _restore_env(self):
        import ae_multicall
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        # The audit points the pinned package at the RUN's configuration; a leftover from
        # another fixture must not decide what this case audits.
        os.environ.pop("VITA_MODEL_CONFIG_PATH", None)

    # -- report plumbing -----------------------------------------------------
    def audit(self, **kwargs):
        from ae_cloud_re_multiturn_input_audit import audit_re_multiturn_inputs
        return audit_re_multiturn_inputs(self.run_dir, self.journal,
                                         config_path=self.config_path,
                                         dataset_path=str(DATASET), **kwargs)

    @staticmethod
    def codes(report):
        return [str(item.get("code")) for item in (report.get("invalid_reasons") or [])]

    def refused(self, report, code):
        self.assertFalse(report.get("input_audit_passed"), report.get("status"))
        self.assertIn(code, self.codes(report), self.codes(report))

    # -- record mutation -----------------------------------------------------
    def result(self):
        return json.loads((self.run_dir / "result.json").read_text(encoding="utf-8"))

    def write_result(self, result):
        (self.run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")

    def reset(self):
        """Restore this case's record from the built fixture (one mutation per run)."""
        shutil.copy(self.base_run / "result.json", self.run_dir / "result.json")
        shutil.copy(self.base_journal, self.journal)

    def select_base(self, name):
        """Re-copy this case's run from another built fixture (a different run SHAPE)."""
        fixture = self.fixtures[name]
        shutil.rmtree(self.run_dir, ignore_errors=True)
        shutil.copytree(fixture["run_dir"], self.run_dir)
        shutil.copy(fixture["journal"], self.journal)

    def mutate(self, mutator, *, arm="rewrite", task="sub_U000828_4", mirror_reward=False,
               mirror_native_slice=False):
        result = self.result()
        record = next(item for item in result["arms"][arm]["tasks"]
                      if item["subtask_id"] == task)
        slice_before = record.get("native_calls_this_task") or []
        cumulative = (record.get("native_snapshot") or {}).get("native_calls") or []
        offset = max(0, len(cumulative) - len(slice_before))
        mutator(record)
        if mirror_native_slice and cumulative:
            # The phase's slice IS the tail of the arm's cumulative native list (the
            # driver sliced it at the phase boundary). A mutation of one is only a
            # faithful record if the other carries it too, otherwise an earlier gate
            # refuses the inconsistency instead of the pairing rule under test.
            record["native_snapshot"]["native_calls"] = (
                cumulative[:offset]
                + json.loads(json.dumps(record.get("native_calls_this_task") or [])))
        if mirror_reward:
            # A real run records the SAME evaluation three times (the completion's reward,
            # the last evaluation's reward and the judge record). A mutation aimed at the
            # scoring rule must keep those copies identical, or the copy cross-check would
            # refuse it first.
            reward = json.loads(json.dumps((record.get("judge") or {}).get("reward_info")))
            snapshot = record.get("native_snapshot") or {}
            for entry in snapshot.get("completed") or []:
                if entry.get("subtask_id") == task:
                    entry["reward_info"] = json.loads(json.dumps(reward))
            if isinstance(snapshot.get("last_evaluation"), dict):
                snapshot["last_evaluation"]["reward_info"] = json.loads(json.dumps(reward))
        self.write_result(result)
        return record

    def scoring_mutation(self, mutation, *, task="sub_U000828_4"):
        result = self.result()
        self.chain._mutate_scoring(result, task, mutation)
        self.write_result(result)

    def vita_models(self):
        return self.run_dir / "vita-models.json"


class VitaModelConfigurationTests(_FixtureCase):
    """1. The audit uses THIS run's own `vita-models.json`, established before import."""

    def test_the_run_declares_its_own_configuration_and_the_audit_uses_it(self):
        report = self.audit()
        self.assertTrue(report["input_audit_passed"], self.codes(report))
        declared = report["vita_model_config"]
        self.assertEqual(Path(declared["path"]).resolve(), self.vita_models().resolve())
        self.assertTrue(declared["content_matches_the_run_declaration"])
        self.assertEqual(os.environ["VITA_MODEL_CONFIG_PATH"],
                         str(self.vita_models().resolve()))
        self.assertTrue(declared["in_force_configuration_matches_the_declaration"])
        # The declaration it was checked against comes from the plan, not from the file.
        self.assertTrue(declared["expected_from"].startswith("the run's own plan config"))

    def test_the_sealed_entry_establishes_the_configuration_before_importing_vita(self):
        entry = _load("sealed_entry_for_r5_repair",
                      ROOT / "scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py")
        os.environ.pop("VITA_MODEL_CONFIG_PATH", None)
        declared = entry.prepare_vita_environment(self.run_dir, None)
        self.assertEqual(Path(declared["path"]).resolve(), self.vita_models().resolve())
        self.assertTrue(declared["content_matches_the_plan_declaration"])
        self.assertEqual(os.environ["VITA_MODEL_CONFIG_PATH"], declared["path"])

    def test_the_sealed_entry_refuses_another_file_and_a_stale_package(self):
        entry = _load("sealed_entry_for_r5_repair_refusals",
                      ROOT / "scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py")
        other = self.work / "other-vita-models.json"
        shutil.copy(self.vita_models(), other)
        with self.assertRaises(RuntimeError) as raised:
            entry.prepare_vita_environment(self.run_dir, other)
        self.assertIn("this run's own", str(raised.exception))
        # A pinned package already imported in this process cannot be re-pointed.
        sentinel = type(sys)("vita")
        saved = sys.modules.get("vita")
        sys.modules["vita"] = sentinel
        try:
            with self.assertRaises(RuntimeError) as raised:
                entry.prepare_vita_environment(self.run_dir, None)
            self.assertIn("fresh", str(raised.exception))
        finally:
            if saved is None:
                sys.modules.pop("vita", None)
            else:
                sys.modules["vita"] = saved

    def test_the_audit_refuses_a_config_that_is_not_this_runs_declaration(self):
        self.vita_models().write_text(
            json.dumps({"default": {}, "models": [{"name": "Qwen3-8B",
                                                   "base_url": "http://127.0.0.1:1/v1",
                                                   "api_key": "EMPTY"}]}),
            encoding="utf-8")
        self.refused(self.audit(),
                     "vita_model_config_content_differs_from_the_run_declaration")

    def test_the_audit_refuses_a_foreign_config_path(self):
        foreign = self.work / "vita-models.json"
        shutil.copy(self.vita_models(), foreign)
        self.refused(self.audit(vita_model_config=foreign),
                     "vita_model_config_is_not_this_runs_own_file")

    def test_the_audit_overrides_a_callers_leftover_environment_value(self):
        os.environ["VITA_MODEL_CONFIG_PATH"] = str(
            ROOT / ".ae-verify-src/vita-models.json")
        report = self.audit()
        declared = report["vita_model_config"]
        self.assertEqual(declared["caller_environment_value_overridden"],
                         str((ROOT / ".ae-verify-src/vita-models.json").resolve()))
        self.assertEqual(os.environ["VITA_MODEL_CONFIG_PATH"],
                         str(self.vita_models().resolve()))
        self.assertTrue(report["input_audit_passed"], self.codes(report))

    def test_an_already_loaded_configuration_that_differs_is_refused(self):
        saved = sys.modules.get("vita.config")
        stub = type(sys)("vita.config")
        stub._models_yaml_path = str(ROOT / ".ae-verify-src/vita-models.json")
        stub.models = {"default": {}, "Qwen3-8B": {"base_url": "http://127.0.0.1:1/v1",
                                                   "api_key": "EMPTY"}}
        sys.modules["vita.config"] = stub
        try:
            self.refused(self.audit(),
                         "vita_package_already_imported_with_another_configuration")
        finally:
            if saved is None:
                sys.modules.pop("vita.config", None)
            else:
                sys.modules["vita.config"] = saved


class JudgedSimulationResolutionTests(_FixtureCase):
    """2. The scored simulation comes from the phase's OWN snapshot, cross-checked."""

    def test_the_resolution_is_recorded_with_its_cross_checks(self):
        report = self.audit()
        self.assertTrue(report["input_audit_passed"], self.codes(report))
        sources = report["judged_simulation_sources"]
        self.assertEqual(len(sources), 18)
        for item in sources:
            self.assertEqual(item["source"], "record.native_snapshot.completed[subtask_id]")
            self.assertEqual(item["completed_entries_for_this_task"], 1)
            self.assertTrue(item["reward_copies_equal"])
            self.assertTrue(item["native_task_id"].startswith("U000828_subtask_"))
            self.assertFalse(item["explicit_judged_simulation_this_task"]["present"])

    def test_a_missing_snapshot_or_completed_list_is_refused(self):
        for key, code in (("native_snapshot", "native_snapshot_missing_for_this_task"),):
            with self.subTest(key=key):
                def drop(record, key=key):
                    record.pop(key, None)
                self.mutate(drop)
                self.refused(self.audit(), code)
        self.reset()
        def empty(record):
            record["native_snapshot"]["completed"] = []
        self.mutate(empty)
        self.refused(self.audit(), "native_snapshot_completed_missing")

    def test_a_duplicate_or_foreign_completed_entry_is_refused(self):
        def duplicate(record):
            completed = record["native_snapshot"]["completed"]
            completed.append(json.loads(json.dumps(completed[0])))
        self.mutate(duplicate)
        self.refused(self.audit(), "completed_simulation_identity_not_unique_for_this_task")

        self.reset()
        def foreign(record):
            completed = record["native_snapshot"]["completed"]
            completed[0]["subtask_id"] = "sub_U000828_5"
        self.mutate(foreign)
        self.refused(self.audit(), "completed_simulation_identity_not_unique_for_this_task")

    def test_a_conflicting_last_evaluation_reward_or_termination_is_refused(self):
        def last_differs(record):
            sim = record["native_snapshot"]["last_evaluation"]["simulation"]
            sim["termination_reason"] = "changed_by_test"
        self.mutate(last_differs)
        self.refused(self.audit(), "completed_simulation_differs_from_last_evaluation")

        self.reset()
        def reward_differs(record):
            record["native_snapshot"]["completed"][0]["reward_info"] = {"reward": 0.5}
        self.mutate(reward_differs)
        self.refused(self.audit(), "judge_reward_records_differ")

        self.reset()
        def termination_differs(record):
            record["native_snapshot"]["completed"][0]["simulation"][
                "termination_reason"] = "changed_by_test"
            record["native_snapshot"]["last_evaluation"]["simulation"][
                "termination_reason"] = "changed_by_test"
        self.mutate(termination_differs)
        self.refused(self.audit(), "judged_termination_reason_differs_from_the_task")

    def test_an_explicit_copy_must_agree_and_is_never_preferred(self):
        def agreeing(record):
            record["judged_simulation_this_task"] = json.loads(json.dumps(
                record["native_snapshot"]["completed"][0]["simulation"]))
        self.mutate(agreeing)
        report = self.audit()
        self.assertTrue(report["input_audit_passed"], self.codes(report))
        self.assertTrue(report["judged_simulation_sources"][0][
            "explicit_judged_simulation_this_task"]["agrees_with_the_snapshot"])

        self.reset()
        def conflicting(record):
            copy_of = json.loads(json.dumps(
                record["native_snapshot"]["completed"][0]["simulation"]))
            copy_of["task_id"] = "U000828_subtask_sub_U000828_9"
            record["judged_simulation_this_task"] = copy_of
        self.mutate(conflicting)
        self.refused(self.audit(), "judged_simulation_this_task_names_another_task")

        self.reset()
        def conflicting_messages(record):
            copy_of = json.loads(json.dumps(
                record["native_snapshot"]["completed"][0]["simulation"]))
            copy_of["messages"] = copy_of["messages"][:-1]
            record["judged_simulation_this_task"] = copy_of
        self.mutate(conflicting_messages)
        self.refused(self.audit(), "judged_simulation_this_task_messages_differ")


class NativeConversionTests(_FixtureCase):
    """3. The real Task/Message conversion is reproduced, field by field."""

    def test_the_conversion_and_its_generated_timestamps_are_recorded(self):
        report = self.audit()
        self.assertTrue(report["input_audit_passed"], self.codes(report))
        conversions = report["native_conversions"]
        self.assertEqual(len(conversions), 18)
        for item in conversions:
            self.assertEqual(item["task_id_rule"], "U000828_subtask_ + subtask_id")
            self.assertTrue(item["native_task_id"].startswith("U000828_subtask_"))
            self.assertGreater(item["messages"], 0)
            self.assertGreaterEqual(item["snapshot_copies_compared"], 2)
        runtime = report["runtime_generated_timestamps"]
        self.assertEqual(runtime["count"],
                         sum(item["runtime_generated_timestamp_count"]
                             for item in conversions))
        self.assertEqual(len(runtime["per_phase"]), 18)

    def test_a_task_id_that_is_not_the_native_conversion_is_refused(self):
        def strip_prefix(record):
            record["native_snapshot"]["completed"][0]["simulation"]["task_id"] = \
                record["subtask_id"]
            record["native_snapshot"]["last_evaluation"]["simulation"]["task_id"] = \
                record["subtask_id"]
        self.mutate(strip_prefix)
        self.refused(self.audit(),
                     "judged_task_id_is_not_the_native_conversion_of_this_subtask")

    def test_message_fields_and_order_must_be_the_native_conversion(self):
        cases = (
            ("content", lambda message: message.update(content="被篡改的内容")),
            ("role", lambda message: message.update(role="user")),
            ("turn_idx", lambda message: message.update(turn_idx=99)),
            ("tool_id", lambda message: message.update(id="call_tampered")),
        )
        for label, mutate_message in cases:
            with self.subTest(field=label):
                self.reset()
                def mutate(record, mutate_message=mutate_message, label=label):
                    messages = record["native_snapshot"]["completed"][0]["simulation"]["messages"]
                    target = next((index for index, message in enumerate(messages)
                                   if (label != "tool_id" or message.get("role") == "tool")
                                   and (label == "tool_id" or message.get("role") == "assistant")),
                                  None)
                    self.assertIsNotNone(target, f"no message to mutate for {label}")
                    mutate_message(messages[target])
                    record["native_snapshot"]["last_evaluation"]["simulation"][
                        "messages"] = json.loads(json.dumps(messages))
                self.mutate(mutate)
                report = self.audit()
                self.assertFalse(report["input_audit_passed"])
                self.assertTrue(any("is_not_the_native_conversion" in code
                                    for code in self.codes(report)), self.codes(report))

    def test_a_timestamp_the_transcript_carried_is_compared_exactly(self):
        """The conversion copies a carried timestamp, so it must match exactly."""
        def carry_consistent(record):
            messages = record["native_snapshot"]["completed"][0]["simulation"]["messages"]
            record["transcript"][1]["timestamp"] = messages[1]["timestamp"]
            record["native_snapshot"]["last_evaluation"]["simulation"]["messages"] = \
                json.loads(json.dumps(messages))
        self.mutate(carry_consistent)
        report = self.audit()
        self.assertTrue(report["input_audit_passed"], self.codes(report))
        recorded = report["native_conversions"][0]
        carried = [row for row in recorded["runtime_generated_timestamps"]
                   if row["index"] == 1]
        self.assertEqual(carried, [], "a carried timestamp is not a generated one")

        self.reset()
        def carrying_but_different(record):
            messages = record["native_snapshot"]["completed"][0]["simulation"]["messages"]
            record["transcript"][1]["timestamp"] = "20200101_000000"
            record["native_snapshot"]["last_evaluation"]["simulation"]["messages"] = \
                json.loads(json.dumps(messages))
        self.mutate(carrying_but_different)
        report = self.audit()
        self.assertFalse(report["input_audit_passed"])
        self.assertTrue(any("is_not_the_native_conversion" in code
                            for code in self.codes(report)), self.codes(report))

    def test_a_transcript_row_outside_the_native_class_is_refused(self):
        def add_field(record):
            record["transcript"][1]["invented_field"] = "not native"
            messages = record["native_snapshot"]["completed"][0]["simulation"]["messages"]
            messages[1]["invented_field"] = "not native"
            record["native_snapshot"]["last_evaluation"]["simulation"]["messages"] = \
                json.loads(json.dumps(messages))
        self.mutate(add_field)
        self.refused(self.audit(), "transcript_row_has_fields_outside_the_native_class")

    def test_a_runtime_generated_timestamp_must_keep_its_shape(self):
        def break_shape(record):
            messages = record["native_snapshot"]["completed"][0]["simulation"]["messages"]
            index = 1
            messages[index]["timestamp"] = "2026-09-16T04:32:33"
            record["native_snapshot"]["last_evaluation"]["simulation"]["messages"] = \
                json.loads(json.dumps(messages))
        self.mutate(break_shape)
        self.refused(self.audit(), "saved_runtime_timestamp_1_has_an_unexpected_shape")

    def test_a_snapshot_copy_that_disagrees_is_refused(self):
        """The run's own two copies of the scored simulation must carry one record."""
        def break_copy(record):
            record["native_snapshot"]["last_evaluation"]["simulation"]["messages"][1][
                "turn_idx"] = 42
        self.mutate(break_copy)
        self.refused(self.audit(),
                     "completed_simulation_differs_from_last_evaluation")


class AuxiliaryOrderBindingTests(_FixtureCase):
    """4. Auxiliary calls are bound position by position, in the verified order."""

    def test_every_auxiliary_call_is_bound_in_order_without_invented_times(self):
        report = self.audit()
        self.assertTrue(report["input_audit_passed"], self.codes(report))
        binding = report["auxiliary_binding"]
        self.assertEqual(binding["records"], len(report["auxiliary_mappings"]))
        self.assertEqual(binding["captured_calls"], binding["records"])
        self.assertEqual(binding["native_records_with_a_time"], 0)
        self.assertEqual(report["auxiliary_record_time_evidence"],
                         "absent_in_this_record")
        self.assertEqual(report["cloud_calls"]["unaccounted"], 0)
        self.assertEqual(report["cloud_calls"]["auxiliary"], binding["records"])

    def _phase_records(self, result, arm="rewrite", task="sub_U000828_4"):
        return next(item for item in result["arms"][arm]["tasks"]
                    if item["subtask_id"] == task)["native_calls_this_task"]

    def test_swapped_duplicated_or_dropped_records_are_refused(self):
        def swap(record):
            calls = record["native_calls_this_task"]
            self.assertGreaterEqual(len(calls), 2)
            calls[0], calls[1] = calls[1], calls[0]
        self.mutate(swap, mirror_native_slice=True)
        report = self.audit()
        self.assertFalse(report["input_audit_passed"])
        self.assertTrue(any(code in self.codes(report) for code in
                            ("auxiliary_request_not_at_its_capture_position",
                             "capture_role_differs_from_native_role")), self.codes(report))

        self.reset()
        def duplicate(record):
            evaluator = next(item for item in record["native_calls_this_task"]
                             if item.get("role") == "evaluator")
            record["native_calls_this_task"].append(json.loads(json.dumps(evaluator)))
        self.mutate(duplicate, mirror_native_slice=True)
        # An invented native reply is refused by the gate that owns the evidence it
        # changed: an extra EVALUATOR reply breaks the judge window count, which is
        # checked before the pairing gate (an extra user reply breaks the wire count
        # instead). Either way no invented call survives into the pairing.
        self.refused(self.audit(),
                     "judge_window_count_differs_from_the_pinned_expansion")

        self.reset()
        def drop(record):
            # A dropped native reply is refused by the gate that owns the evidence it
            # changed: the arm's simulated-user events no longer match the wire, which is
            # checked before the pairing gate. The record is still refused, and the report
            # names the rule that caught it.
            record["native_calls_this_task"].pop(0)
        self.mutate(drop, mirror_native_slice=True)
        self.refused(self.audit(), "simulated_user_event_count_changed")

    def test_a_record_out_of_its_recorded_order_is_refused(self):
        """The record order is evidence: moving one record is a changed pairing."""
        def rotate(record):
            calls = record["native_calls_this_task"]
            self.assertGreaterEqual(len(calls), 2)
            calls.append(calls.pop(0))
        self.mutate(rotate, mirror_native_slice=True)
        report = self.audit()
        self.assertFalse(report["input_audit_passed"])
        self.assertTrue(any(code in self.codes(report) for code in
                            ("auxiliary_request_not_at_its_capture_position",
                             "auxiliary_response_not_at_its_capture_position",
                             "capture_role_differs_from_native_role")), self.codes(report))

    def test_a_changed_response_is_refused(self):
        """An evaluator reply is checked against the capture it was answered by.

        The user-simulator reply is additionally echoed on the agent wire, so changing
        THAT one is refused earlier by the runtime-user gate; the judge reply is not on
        the agent wire, which makes it the case that isolates this pairing.
        """
        def change_response(record):
            call = [item for item in record["native_calls_this_task"]
                    if item.get("role") == "evaluator"][-1]
            parsed = json.loads(call["response"]["content"]
                                .split("```json", 1)[1].rsplit("```", 1)[0])
            # The LAST window's justification is not carried anywhere, so the judge chain
            # stays internally consistent; the capture the reply answers no longer does.
            parsed[0]["justification"] = "被篡改的理由"
            content = "```json\n" + json.dumps(parsed, ensure_ascii=False) + "\n```"
            call["response"]["content"] = content
            choices = (call["response"].get("raw_data") or {}).get("choices")
            if choices:
                choices[0]["message"]["content"] = content
            evaluations = ((record.get("judge") or {}).get("reward_info") or {}).get(
                "window_evaluations") or []
            if evaluations:
                evaluations[-1]["assistant_message_content"] = content
        self.mutate(change_response, mirror_reward=True)
        self.refused(self.audit(), "auxiliary_response_not_at_its_capture_position")

    def test_recorded_times_are_checked_when_a_record_carries_them(self):
        def add_times(record):
            for index, call in enumerate(record["native_calls_this_task"]):
                call["recorded_at"] = f"2026-09-16T04:3{9 - index}:00+00:00"
        self.mutate(add_times, mirror_native_slice=True)
        self.refused(self.audit(), "native_call_times_not_in_record_order")

        self.reset()
        def partial(record):
            record["native_calls_this_task"][0]["recorded_at"] = \
                "2026-09-16T04:30:00+00:00"
        self.mutate(partial, mirror_native_slice=True)
        self.refused(self.audit(), "native_call_times_partially_recorded")

        self.reset()
        def outside(record):
            for call in record["native_calls_this_task"]:
                call["recorded_at"] = "2020-01-01T00:00:00+00:00"
        self.mutate(outside, mirror_native_slice=True)
        self.refused(self.audit(), "native_call_time_outside_its_phase_window")


class JudgeSemanticsTests(_FixtureCase):
    """5. The strict echo protocol is kept; the native recheck is explicit and separate."""

    def test_the_strict_protocol_still_refuses_an_echo_difference(self):
        self.select_base("echo")
        self.refused(self.audit(),
                     "judge_decision_rubric_text_differs_from_the_criteria")

    def test_the_native_recheck_collects_the_difference_and_finishes(self):
        self.select_base("echo")
        report = self.audit(judge_semantics="native-by-id-v1")
        self.assertFalse(report["input_audit_passed"])
        self.assertEqual(report["status"], "STRICT_PROTOCOL_NOT_MET")
        self.assertFalse(report["strict_protocol_passed"])
        self.assertTrue(report["native_semantics_checks_passed"])
        self.assertIn("judge_decision_rubric_text_differs_from_the_criteria",
                      self.codes(report))
        semantics = report["judge_semantics"]
        self.assertEqual(semantics["mode"], "native-by-id-v1")
        self.assertEqual(semantics["protocol"], "native-by-id-v1")
        self.assertTrue(semantics["post_hoc"])
        self.assertTrue(semantics["strict_protocol_failed_by_echo_differences"])
        self.assertEqual(semantics["phases_checked"], report["scope"]["phase_count"])
        differences = report["judge_echo_differences"]
        self.assertTrue(differences)
        for item in differences:
            self.assertEqual(set(item), {"arm", "task", "window_index", "rubric_idx",
                                         "criteria_text", "echoed_text"})
            self.assertNotEqual(item["criteria_text"], item["echoed_text"])
        # The state update still follows the PINNED rule: the criteria text of the reply is
        # never adopted, so the next window's carried state carries the DATASET text.
        chain = next(chain for chain in report["judge_chains"]
                     if chain["arm"] == "rewrite" and chain["task"] == "sub_U000828_4")
        echoed = {item["rubric_idx"]: item["echoed_text"]
                  for item in differences if item["task"] == "sub_U000828_4"}
        self.assertTrue(echoed, "the echoed replies must be recorded")
        window = chain["windows"][0]
        carried = {item["rubric_idx"]: item["rubric"]
                   for item in window["carried_rubric_states"]}
        for index, echoed_text in echoed.items():
            state = window["resulting_rubric_states"][index]
            self.assertNotEqual(state["rubric"], echoed_text)
            self.assertEqual(state["rubric"], carried[index])

    def test_the_native_recheck_does_not_swallow_other_judge_defects(self):
        for mutation, code in (("window_duplicate",
                                "judge_window_count_differs_from_the_pinned_expansion"),
                               ("final_met_flip",
                                "judge_final_rubric_met_differs_from_the_last_window_decision"),
                               ("final_missing", "judge_final_rubrics_missing_or_empty"),
                               ("window_initial_change",
                                "judge_window_1_carried_justification_changed")):
            with self.subTest(mutation=mutation):
                self.reset()  # one mutation per record
                self.scoring_mutation(mutation)
                report = self.audit(judge_semantics="native-by-id-v1")
                self.assertFalse(report["input_audit_passed"])
                self.assertFalse(report["native_semantics_checks_passed"])
                self.assertIn(code, self.codes(report), self.codes(report))
                self.assertEqual((report["judge_semantics"] or {}).get("mode"),
                                 "native-by-id-v1")

    def test_an_unknown_judge_semantics_value_is_refused(self):
        self.refused(self.audit(judge_semantics="native-by-id-v2"),
                     "judge_semantics_option_unknown")

    def test_a_duplicate_or_unknown_rubric_index_is_refused_in_either_mode(self):
        def duplicate_index(record):
            for call in record["native_calls_this_task"]:
                if call.get("role") != "evaluator":
                    continue
                content = call["response"]["content"]
                parsed = json.loads(content.split("```json", 1)[1].rsplit("```", 1)[0])
                parsed.append(json.loads(json.dumps(parsed[0])))
                original = content
                content = "```json\n" + json.dumps(parsed, ensure_ascii=False) + "\n```"
                call["response"]["content"] = content
                choices = (call["response"].get("raw_data") or {}).get("choices")
                if choices:
                    choices[0]["message"]["content"] = content
                evaluations = ((record.get("judge") or {}).get("reward_info") or {}).get(
                    "window_evaluations") or []
                for evaluation in evaluations:
                    if evaluation.get("assistant_message_content") == original:
                        evaluation["assistant_message_content"] = content
        for mode in (None, "native-by-id-v1"):
            with self.subTest(mode=mode):
                self.reset()
                self.mutate(duplicate_index, mirror_reward=True)
                report = self.audit(judge_semantics=mode)
                self.assertFalse(report["input_audit_passed"])
                self.assertFalse(report["native_semantics_checks_passed"])
                self.assertIn("judge_decision_rubric_index_duplicated",
                              self.codes(report), self.codes(report))


class SealedCommandTests(_FixtureCase):
    """The new entry point end to end, in its own process, with sockets banned."""

    def _run_entry(self, *, extra=(), output_name="report.json", copy_module=True):
        output = self.work / output_name
        copy_of = self.work / "audit-module-at-run.py"
        if copy_module:
            shutil.copy(ROOT / "ae_cloud_re_multiturn_input_audit.py", copy_of)
        entry_argv = [
            str(ROOT / "scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py"),
            "--run-dir", str(self.run_dir), "--proxy-journal", str(self.journal),
            "--config", str(self.config_path), "--dataset", str(DATASET),
            "--sealed-audit-copy", str(copy_of), "--output", str(output),
            *extra]
        script = ("import socket\n"
                  "socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw("
                  "RuntimeError('offline network forbidden'))\n"
                  "socket.create_connection = socket.socket.connect\n"
                  "import runpy, sys\n"
                  "sys.argv = sys.argv[1:]\n"
                  "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        env = dict(os.environ)
        env["AE_VERIFY_ROOT"] = str(ROOT / ".ae-verify-src")
        env["AE_VITA_SOURCE"] = str(Path(os.environ.get("AE_VITA_SOURCE",
                                                        ROOT / ".ae-verify-src/source")))
        env.pop("VITA_MODEL_CONFIG_PATH", None)
        done = subprocess.run([sys.executable, "-B", "-c", script, *entry_argv],
                              cwd=str(ROOT), capture_output=True, text=True, env=env)
        return done, output

    def test_the_entry_audits_a_capture_offline_and_writes_a_new_report(self):
        done, output = self._run_entry()
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["input_audit_passed"], report["invalid_reasons"])
        self.assertEqual(os.path.basename(report["vita_model_config"]["path"]),
                         "vita-models.json")
        self.assertEqual(report["judge_semantics"]["mode"], "strict-verbatim-echo-v1")
        summary = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["input_audit_passed"], True)
        self.assertFalse(summary["native_semantics_checks_passed"])

    def test_an_existing_report_is_never_overwritten(self):
        output = self.work / "existing.json"
        output.write_text("keep me", encoding="utf-8")
        done, _ = self._run_entry(output_name="existing.json")
        self.assertEqual(done.returncode, 2)
        self.assertIn("refused to overwrite", done.stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_an_unknown_semantics_value_is_refused_before_any_work(self):
        done, output = self._run_entry(extra=["--judge-semantics", "native-by-id-v2"],
                                      output_name="unknown.json")
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown --judge-semantics", done.stderr)
        self.assertFalse(output.exists())

    def test_a_foreign_vita_config_is_refused_before_importing_vita(self):
        other = self.work / "elsewhere-vita-models.json"
        shutil.copy(self.vita_models(), other)
        done, output = self._run_entry(extra=["--vita-models", str(other)],
                                       output_name="foreign.json")
        self.assertEqual(done.returncode, 2)
        self.assertIn("this run's own", done.stderr)
        self.assertFalse(output.exists())

    def test_the_native_recheck_runs_through_the_entry(self):
        self.select_base("echo")
        done, output = self._run_entry(extra=["--judge-semantics", "native-by-id-v1"],
                                       output_name="native.json")
        # The strict protocol still failed, so the entry exits non-zero ...
        self.assertEqual(done.returncode, 2, done.stderr[-2000:])
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["input_audit_passed"])
        self.assertTrue(report["native_semantics_checks_passed"])
        self.assertEqual(report["status"], "STRICT_PROTOCOL_NOT_MET")
        self.assertTrue(report["judge_echo_differences"])
        summary = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["native_semantics_checks_passed"], True)
        self.assertEqual(summary["input_audit_passed"], False)


@unittest.skipUnless(R5_RUN.is_dir() and R5_JOURNAL.is_file(),
                     "the sealed r5 capture is not present")
class TheRealR5RecordTests(unittest.TestCase):
    """The REAL sealed r5 capture, replayed in its own process with sockets banned."""

    @classmethod
    def setUpClass(cls):
        if not R5_SEALED_COPY.is_file():
            raise unittest.SkipTest("the r5 runtime copy of the audit module is absent")
        import hashlib
        digest = hashlib.sha256(R5_SEALED_COPY.read_bytes()).hexdigest()
        if digest != R5_RECORDED_AUDIT_SHA:
            raise unittest.SkipTest(
                "the sealed runtime copy is not the one the r5 record pins")
        plan = json.loads((R5_RUN / "plan.json").read_text(encoding="utf-8"))
        result = json.loads((R5_RUN / "result.json").read_text(encoding="utf-8"))
        recorded = [plan["provenance"]["code_sha256"]["ae_cloud_re_multiturn_input_audit.py"],
                    result["provenance"]["code_sha256"]["ae_cloud_re_multiturn_input_audit.py"]]
        if recorded != [R5_RECORDED_AUDIT_SHA, R5_RECORDED_AUDIT_SHA]:
            raise unittest.SkipTest("plan and result do not agree on the r5 audit module")

    def _replay(self, judge_semantics=None):
        script = (
            "import json, sys\n"
            "sys.path.insert(0, 'tests')\n"
            "import r5_sealed_partial_replay as replay\n"
            "out = replay.replay(\n"
            f"    {str(R5_RUN)!r}, {str(R5_JOURNAL)!r},\n"
            f"    {str(CANDIDATE)!r}, {str(DATASET)!r},\n"
            f"    judge_semantics={judge_semantics!r},\n"
            f"    sealed_audit_copy={str(R5_SEALED_COPY)!r})\n"
            "json.dump(out, sys.stdout, ensure_ascii=False)\n")
        env = dict(os.environ)
        env["AE_VERIFY_ROOT"] = str(ROOT / ".ae-verify-src")
        env["AE_VITA_SOURCE"] = str(ROOT / ".ae-verify-src/source")
        env.pop("VITA_MODEL_CONFIG_PATH", None)
        done = subprocess.run([sys.executable, "-B", "-c", script], cwd=str(ROOT),
                              capture_output=True, text=True, env=env, timeout=1800)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        return json.loads(done.stdout[done.stdout.index("{"):])

    def test_the_strict_protocol_reaches_the_echo_requirement_and_refuses_it(self):
        out = self._replay()
        self.assertIsNotNone(out["refusal"])
        self.assertEqual(out["refusal"]["gate"], "judge_gate")
        self.assertEqual(out["refusal"]["code"],
                         "judge_decision_rubric_text_differs_from_the_criteria")
        for gate in ("vita_environment_gate", "config_gate.declared_contract",
                     "transport_gate", "dataset_gate", "capacity_gate", "phase_gate",
                     "wire_gate", "tool_mapping_gate", "memory_gate"):
            self.assertIn(gate, out["gates_run"])
        self.assertIn("judge_decision_rubric_text_differs_from_the_criteria",
                      [item["code"] for item in out["invalid_reasons"]])
        # The interface repairs are what let the chain reach the echo requirement.
        self.assertGreaterEqual(len(out["native_conversions"]), 4)
        self.assertFalse(out["strict_protocol_passed"])
        self.assertFalse(out["native_semantics_checks_passed"])

    def test_the_native_recheck_covers_all_eighteen_phases_and_the_closure_gates(self):
        out = self._replay("native-by-id-v1")
        self.assertIsNone(out["refusal"])
        self.assertEqual(out["judge_chains"], 18)
        self.assertEqual(out["auxiliary_mappings"], 98)
        self.assertEqual(out["judge_echo_difference_count"], 6)
        self.assertEqual(out["cloud_calls"]["auxiliary"], 98)
        self.assertEqual(out["cloud_calls"]["unaccounted"], 0)
        self.assertEqual(out["runtime_generated_timestamps"]["count"],
                         sum(item["count"] for item in
                             out["runtime_generated_timestamps"]["per_phase"]))
        self.assertEqual(len(out["judged_simulation_sources"]), 18)
        self.assertEqual(len(out["native_conversions"]), 18)
        self.assertEqual(out["auxiliary_binding"]["records"], 98)
        self.assertEqual(out["auxiliary_binding"]["native_records_with_a_time"], 0)
        self.assertEqual(out["verdict"], {"strict_protocol_passed": False,
                                          "native_semantics_checks_passed": True})
        for gate in ("judge_gate", "auxiliary_gate", "cloud_totals_gate",
                     "separation_gate", "task_scores"):
            self.assertIn(gate, out["gates_run"])
        self.assertEqual(out["judge_semantics"]["mode"], "native-by-id-v1")
        for item in out["judge_echo_differences"]:
            self.assertIn(item["arm"], ("rewrite", "erratum"))
            self.assertIn(item["task"], ("sub_U000828_7", "sub_U000828_10"))
            self.assertNotEqual(item["criteria_text"], item["echoed_text"])
        # The uncovered front gates are named, not silently skipped.
        self.assertTrue(any("provenance_gate" in item for item in out["uncovered_gates"]))


if __name__ == "__main__":
    unittest.main()
