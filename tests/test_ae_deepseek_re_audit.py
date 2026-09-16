"""The 0.4 protocol through the PUBLIC audit entry, over a REAL capture.

The fixture is the project's own full-chain builder: real dataset, real native
environments, real Letta session double, the real proxy journal, and the real stacked
manifest + bootstrap receipt. Nothing before the capacity gate is mocked, so a failure
here means the chain is wrong - not that a helper returned the wrong value.

A completed 0.4 run must reach the final verdict, and every negative case must fail for
ITS OWN reason: the codes asserted below are the specific refusals, not "something went
wrong", so a gate that stopped running would show up as a missing code rather than as a
still-green test.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

CANDIDATE = ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-candidate.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeepSeekAuditEntryTests(unittest.TestCase):
    """The public entry point, over the real chain, for the 0.4 protocol."""

    @classmethod
    def setUpClass(cls):
        cls.chain = _load("re_multiturn_chain_for_ds_audit",
                          ROOT / "tests/re_multiturn_chain.py")
        cls.pair = cls.chain.t.pair
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            raise unittest.SkipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        # The chain builder must not inherit a service-identity variable from the ambient
        # shell: a stray `AE_LETTA_*_RECEIPT` would make the run declare two service trees.
        import ae_multicall
        self._saved_env = {}
        for name in (ae_multicall.LAUNCH_RECEIPT_ENV_VAR,
                     ae_multicall.STACK_RECEIPT_ENV_VAR,
                     ae_multicall.STACK_MANIFEST_ENV_VAR,
                     ae_multicall.STACK_ENV_VAR):
            self._saved_env[name] = os.environ.pop(name, None)
        def restore():
            for name, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.addCleanup(restore)

    def _candidate_path(self):
        """The 0.4 candidate, with the loopback origins the driver requires.

        The file is exactly the candidate the operator would hand `--config`: the
        exploratory opt-in is NOT written into it, because on site it travels as the
        `--exploratory-capacity-option` FLAG. The chain fixture stands in for that flag
        from the config's own declared protocol field, so the file bytes are the same
        document the plan records and the gate is armed from.
        """
        record = json.loads(CANDIDATE.read_text(encoding="utf-8"))
        record["letta_origin"] = "http://127.0.0.1:8283"
        record["model_origin"] = "http://127.0.0.1:8000"
        path = self.tmp / "deepseek-0.4.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def _stack_pair(self):
        """The reviewed stack fixture: a real manifest and a real bootstrap receipt."""
        wiring = _load("stack_wiring_for_ds_audit",
                       ROOT / "tests/test_ae_stack_receipt_wiring.py")
        return wiring.StackEvidenceFixture(self.tmp)

    def _build(self, *, with_stack_receipt=True, **kwargs):
        config_path = self._candidate_path()
        pair = self._stack_pair()
        fixture = self.chain.build_full_chain(
            self.tmp, config_path=config_path, patch_stack_manifest=pair.manifest,
            patch_stack_load_receipt=pair.receipt if with_stack_receipt else None,
            # The stacked service identity REPLACES the multicall-only one: declaring both
            # receipts is a contradiction the provenance entry refuses, so the fixture
            # declares only the stack pair.
            receipt=False, **kwargs)
        fixture["stack"] = pair
        return fixture, config_path

    def _audit(self, fixture, config_path):
        from ae_cloud_re_multiturn_input_audit import audit_re_multiturn_inputs
        dataset = Path(os.environ.get(
            "AE_VITA_DATASET", ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        return audit_re_multiturn_inputs(fixture["run_dir"], fixture["journal"],
                                         config_path=config_path,
                                         dataset_path=dataset)

    @staticmethod
    def _codes(report):
        """Every refusal code the report names, wherever it records it."""
        codes = [str(item.get("code")) for item in (report.get("invalid_reasons") or [])]
        codes += [str(item) for item in ((report.get("transport") or {}).get("issues") or [])]
        codes += [str(item) for item in (report.get("failures") or [])]
        return codes

    @staticmethod
    def _drop_journal_rows(journal, kinds):
        """Remove rows AND renumber `sequence`, exactly as a rewritten journal would.

        Without the renumbering the capture fails its own sequence check and no gate
        downstream would ever be reached - which would make every negative below pass
        for the wrong reason.
        """
        kept = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
                if line.strip() and json.loads(line).get("kind") not in kinds]
        for index, row in enumerate(kept):
            row["sequence"] = index
        journal.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in kept) + "\n",
                           encoding="utf-8")

    # -- the reachable branch -------------------------------------------------
    def test_a_complete_deepseek_capture_reaches_the_final_verdict(self):
        fixture, config_path = self._build()
        report = self._audit(fixture, config_path)
        self.assertEqual(report.get("status"), "VALID", report.get("invalid_reasons"))
        self.assertTrue(report.get("input_audit_passed"))
        # The gate really ran, and it was the BYTE gate - over the run's own records.
        policy = report.get("capacity_policy") or {}
        self.assertTrue(policy.get("declared"), report.get("invalid_reasons"))
        self.assertEqual(policy.get("protocol"), "exploratory_no_token_capacity_guarantee")
        self.assertEqual(policy.get("count_basis"), ["no_token_count_byte_gate_only"])
        self.assertFalse(policy.get("capacity_is_a_guarantee"))
        self.assertFalse(policy.get("token_count_available"))
        self.assertGreater(policy.get("checks") or 0, 0)
        self.assertEqual(policy.get("stops"), [])
        self.assertEqual(sorted(policy.get("stages_clarified") or {}), ["erratum", "rewrite"])
        self.assertTrue(policy.get("clarifications_bound_to_arm_and_task"))
        # ... and the run is judged on its own merits, past every shared gate
        executed = report.get("checks_executed") or []
        for gate in ("stack_evidence_gate", "config_gate", "provenance_gate",
                     "capacity_gate", "phase_gate", "agent_posts", "wire_gate",
                     "memory_gate", "judge_gate"):
            self.assertIn(gate, executed)

    # -- the negatives --------------------------------------------------------
    def test_a_capture_without_per_request_gate_records_fails(self):
        fixture, config_path = self._build()
        self._drop_journal_rows(fixture["journal"], {"capacity_check"})
        report = self._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("capacity_checks_differ_from_chat_requests", self._codes(report))

    def test_a_capture_that_was_never_armed_fails_in_the_deepseek_gate(self):
        fixture, config_path = self._build()
        self._drop_journal_rows(fixture["journal"], {"capacity_check", "capacity_armed"})
        report = self._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        # The 0.4 gate itself refuses: a completed run must show the policy it ran under.
        self.assertIn("the_proxy_that_served_this_run_was_never_armed", self._codes(report))

    def test_a_tampered_gate_hash_fails(self):
        fixture, config_path = self._build()
        journal = fixture["journal"]
        rows = []
        for line in journal.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") == "capacity_check":
                # a nonempty but WRONG digest must not be accepted as a binding
                row["input_sha256"] = "0" * 64
            rows.append(json.dumps(row, ensure_ascii=False))
        journal.write_text("\n".join(rows) + "\n", encoding="utf-8")
        report = self._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("capacity_check_does_not_describe_this_request", self._codes(report))

    def test_a_driver_attestation_that_disagrees_with_the_journal_fails(self):
        fixture, config_path = self._build()
        result_path = fixture["run_dir"] / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["capacity_checks"][0]["input_bytes"] += 1
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        report = self._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("driver_and_journal_capacity_checks_differ", self._codes(report))

    def test_a_completed_run_without_the_stack_receipt_fails(self):
        fixture, config_path = self._build(with_stack_receipt=False)
        report = self._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("executed_stack_run_without_service_receipt", self._codes(report))

    def test_a_tampered_stack_receipt_fails(self):
        fixture, config_path = self._build()
        receipt = fixture["stack"].receipt
        receipt.write_text(receipt.read_text(encoding="utf-8") + "\n",
                           encoding="utf-8")
        report = self._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("stack_receipt_bytes_changed", self._codes(report))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
