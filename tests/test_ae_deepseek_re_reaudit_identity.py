"""Sealed re-audit identity: the auditor is separated from the run's own identity.

A run record pins the digest of every file in `MULTITURN_CODE_FILES`, and that set includes
the POST-HOC audit module. Repairing the audit after a completed run therefore changes one
digest the sealed record holds - which is why the completed capture
`ae-deepseek-re-live-20260916-r4` could not simply be re-audited with the fixed module.

The decision taken here is identity SEPARATION, not a one-off waiver:

* the default entry is unchanged and still refuses when the audit module changed;
* an EXPLICIT offline re-audit (`sealed_audit_copy=`) must be handed the runtime copy of
  that ONE module; its digest is recomputed and must equal BOTH the plan's and the
  result's recorded digest (which must also agree with each other);
* every other run-identity file is still compared exactly - there is no ignore list;
* the report records the auditor's OWN code identity (entry point, this module, the shared
  audit module, the shared journal walk) by path and by the digest of the code that really
  ran, plus the sealed copy's digest and the digest the run recorded - so a passing report
  can never present the old hashes as the executing code.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

#: The real runtime copy of the audit module, from the delivery that shipped the code the
#: completed run was recorded with. Its digest is the one the sealed plan and result carry.
SEALED_AUDIT_COPY = (ROOT / "results/ae-deepseek-re-budget1024-r1/after"
                     / "ae_cloud_re_multiturn_input_audit.py")
SEALED_AUDIT_SHA = "7d631dfb8a3e13a22619ae78b64078a538720d47b51adc8473e1ec5b1868f4f6"
SEALED_PLAN = (ROOT / "transfers/ae-deepseek-re-complete-20260916-r4/deployment/runs"
               / "ae-deepseek-re-live-20260916-r4/plan.json")
CANDIDATE_1024 = (ROOT / "configs"
                  / "ae-01__re-multiturn__deepseek-flash.serial-budget1024-candidate.json")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TheSealedCopyItselfTests(unittest.TestCase):
    """The one file this option may point at, verified against the real seal."""

    def test_the_delivered_copy_is_the_runtime_file_the_record_pins(self):
        self.assertTrue(SEALED_AUDIT_COPY.is_file(),
                        "the budget-1024 delivery must carry the runtime audit module")
        self.assertEqual(_sha256(SEALED_AUDIT_COPY), SEALED_AUDIT_SHA)
        self.assertNotEqual(_sha256(ROOT / "ae_cloud_re_multiturn_input_audit.py"),
                            SEALED_AUDIT_SHA,
                            "the repaired module must differ, or this option proves nothing")

    def test_the_sealed_plan_and_result_both_record_that_digest(self):
        for name in ("plan.json", "result.json"):
            document = json.loads((SEALED_PLAN.parent / name).read_text(encoding="utf-8"))
            code = (document.get("provenance") or {}).get("code_sha256") or {}
            self.assertEqual(code.get("ae_cloud_re_multiturn_input_audit.py"),
                             SEALED_AUDIT_SHA, name)

    def test_the_option_only_ever_names_that_one_file(self):
        module = _load("ae_cloud_re_multiturn_input_audit_for_reaudit",
                       ROOT / "ae_cloud_re_multiturn_input_audit.py")
        self.assertEqual(module.SEALED_AUDIT_MODULE,
                         "ae_cloud_re_multiturn_input_audit.py")
        self.assertIn(module.SEALED_AUDIT_MODULE, module.MULTITURN_CODE_FILES
                      if hasattr(module, "MULTITURN_CODE_FILES") else
                      module.multiturn_code_files(ROOT))
        # The auditor's own identity list is exactly the reviewed four files.
        self.assertEqual(module.AUDITOR_IDENTITY_FILES, (
            "scripts/ae_01_cloud_re_multiturn_input_audit.py",
            "ae_cloud_re_multiturn_input_audit.py",
            "ae_cloud_input_audit.py",
            "ae_cloud_audit.py"))


class TheReauditEntryTests(unittest.TestCase):
    """The public entry over the real offline fixture, with and without the option."""

    @classmethod
    def setUpClass(cls):
        cls.entry = _load("test_ae_deepseek_re_audit_for_reaudit",
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

    @staticmethod
    def _codes(report):
        return [str(item.get("code")) for item in (report.get("invalid_reasons") or [])] + \
               [str(item) for item in (report.get("transport") or {}).get("issues") or []] + \
               [str(item) for item in (report.get("failures") or [])]

    def _audit(self, case, fixture, config_path, **kwargs):
        from ae_cloud_re_multiturn_input_audit import audit_re_multiturn_inputs
        dataset = Path(os.environ.get(
            "AE_VITA_DATASET", ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        return audit_re_multiturn_inputs(fixture["run_dir"], fixture["journal"],
                                         config_path=config_path, dataset_path=dataset,
                                         **kwargs)

    def _record_old_audit_digest(self, fixture):
        """Make the capture look like one recorded BEFORE the audit repair.

        Only the record's own audit-module digest is replaced with the real runtime digest
        of the delivered sealed copy - the same thing the completed run's plan carries.
        Nothing else in the plan, the result or the capture is touched.
        """
        for name in ("plan.json", "result.json"):
            path = fixture["run_dir"] / name
            document = json.loads(path.read_text(encoding="utf-8"))
            code = document["provenance"]["code_sha256"]
            self.assertIn("ae_cloud_re_multiturn_input_audit.py", code)
            code["ae_cloud_re_multiturn_input_audit.py"] = SEALED_AUDIT_SHA
            document["provenance"]["code_sha256"] = code
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def test_the_default_entry_refuses_a_changed_audit_module(self):
        case = self._case()
        fixture, config_path = case._build()
        self._record_old_audit_digest(fixture)
        report = self._audit(case, fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn(
            "plan_provenance_code_sha256_mismatch_ae_cloud_re_multiturn_input_audit.py",
            self._codes(report))
        self.assertEqual((report.get("reaudit") or {}).get("mode"), "strict")

    def test_the_explicit_reaudit_passes_the_identity_gate(self):
        case = self._case()
        fixture, config_path = case._build()
        self._record_old_audit_digest(fixture)
        report = self._audit(case, fixture, config_path,
                             sealed_audit_copy=SEALED_AUDIT_COPY)
        self.assertEqual(report.get("status"), "VALID", report.get("invalid_reasons"))
        reaudit = report.get("reaudit") or {}
        self.assertEqual(reaudit.get("mode"), "sealed_run_audit_identity")
        self.assertEqual(reaudit["sealed_audit_copy"]["sha256"], SEALED_AUDIT_SHA)
        self.assertEqual(reaudit["recorded_at_run_sha256"], SEALED_AUDIT_SHA)
        # The EXECUTED code is reported as itself, never as the recorded old hash.
        self.assertEqual(reaudit["executed_sha256"],
                         _sha256(ROOT / "ae_cloud_re_multiturn_input_audit.py"))
        self.assertNotEqual(reaudit["executed_sha256"], SEALED_AUDIT_SHA)
        identity = report.get("auditor_identity") or {}
        for name in ("scripts/ae_01_cloud_re_multiturn_input_audit.py",
                     "ae_cloud_re_multiturn_input_audit.py",
                     "ae_cloud_input_audit.py", "ae_cloud_audit.py"):
            self.assertIn(name, identity, name)
            self.assertTrue(identity[name]["present"], name)
            self.assertEqual(identity[name]["sha256"], _sha256(ROOT / name), name)

    def test_a_missing_or_wrong_sealed_copy_is_refused(self):
        case = self._case()
        fixture, config_path = case._build()
        self._record_old_audit_digest(fixture)
        missing = Path(case.tmp) / "not-there.py"
        report = self._audit(case, fixture, config_path, sealed_audit_copy=missing)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("sealed_audit_copy_missing", self._codes(report))
        wrong = Path(case.tmp) / "similar-looking.py"
        wrong.write_text((ROOT / "ae_cloud_re_multiturn_input_audit.py").read_text(
            encoding="utf-8").replace("Sealed re-audit", "Sealed re-audit (copy)"),
            encoding="utf-8")
        report = self._audit(case, fixture, config_path, sealed_audit_copy=wrong)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("sealed_audit_copy_digest_mismatch", self._codes(report))

    def test_a_changed_run_side_file_is_still_refused(self):
        case = self._case()
        fixture, config_path = case._build()
        self._record_old_audit_digest(fixture)
        for name in ("plan.json", "result.json"):
            path = fixture["run_dir"] / name
            document = json.loads(path.read_text(encoding="utf-8"))
            code = document["provenance"]["code_sha256"]
            code["ae_cloud_re_multiturn.py"] = "0" * 64
            document["provenance"]["code_sha256"] = code
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        report = self._audit(case, fixture, config_path,
                             sealed_audit_copy=SEALED_AUDIT_COPY)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("plan_provenance_code_sha256_mismatch_ae_cloud_re_multiturn.py",
                      self._codes(report))

    def test_plan_and_result_audit_digests_must_agree(self):
        case = self._case()
        fixture, config_path = case._build()
        self._record_old_audit_digest(fixture)
        path = fixture["run_dir"] / "result.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["provenance"]["code_sha256"]["ae_cloud_re_multiturn_input_audit.py"] = \
            "1" * 64
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        report = self._audit(case, fixture, config_path,
                             sealed_audit_copy=SEALED_AUDIT_COPY)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("plan_and_result_audit_module_digests_differ", self._codes(report))

    def test_the_strict_entry_and_the_recorded_code_list_are_untouched(self):
        """The separate re-audit entry must not require changing anything recorded.

        The strict CLI is part of the run identity (`MULTITURN_CODE_FILES`), so adding the
        option THERE would have changed a second sealed digest and defeated the point. The
        sealed entry is a NEW file that no run identity lists.
        """
        module = _load("ae_cloud_re_multiturn_input_audit_for_lists",
                       ROOT / "ae_cloud_re_multiturn_input_audit.py")
        sealed_entry = "scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py"
        strict_entry = "scripts/ae_01_cloud_re_multiturn_input_audit.py"
        self.assertIn(strict_entry, module.multiturn_code_files(ROOT))
        self.assertNotIn(sealed_entry, module.multiturn_code_files(ROOT))
        self.assertTrue((ROOT / sealed_entry).is_file())
        document = json.loads((SEALED_PLAN.parent / "plan.json").read_text(encoding="utf-8"))
        recorded = document["provenance"]["code_sha256"]
        self.assertEqual(recorded[strict_entry], _sha256(ROOT / strict_entry),
                         "the strict audit CLI must still be the recorded bytes")
        self.assertNotIn(sealed_entry, recorded)

    def test_the_sealed_entry_refuses_to_overwrite_an_existing_output(self):
        entry = _load("ae_01_cloud_re_multiturn_input_audit_sealed",
                      ROOT / "scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py")
        existing = Path(self.tmp if hasattr(self, "tmp") else tempfile.mkdtemp())
        output = existing / "report.json"
        output.write_text("{}", encoding="utf-8")
        code = entry.main(["--run-dir", str(SEALED_PLAN.parent),
                           "--proxy-journal", str(SEALED_PLAN.parent / "x.jsonl"),
                           "--config", str(CANDIDATE_1024), "--dataset", str(SEALED_PLAN),
                           "--sealed-audit-copy", str(SEALED_AUDIT_COPY),
                           "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(output.read_text(encoding="utf-8"), "{}")

    def test_the_strict_path_still_passes_when_nothing_changed(self):
        case = self._case()
        fixture, config_path = case._build()
        report = self._audit(case, fixture, config_path)
        self.assertEqual(report.get("status"), "VALID", report.get("invalid_reasons"))
        self.assertEqual((report.get("reaudit") or {}).get("mode"), "strict")
        self.assertIn("auditor_identity", report)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
