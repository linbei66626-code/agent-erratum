"""The 0.3 capacity run's SERVICE identity: the stacked manifest and its receipt.

The live service that serves a capacity/no-compaction protocol runs the STACKED patch
(multicall + no-compaction) and writes `patch-stack-load.json`. That receipt has its own
field set - `new_files` among them - so the multicall validator refuses it, and the
production driver/audit must validate it with `validate_stack_receipt` instead. These
tests drive the REAL provenance entry and the REAL post-hoc audit over a local fixture
that keeps the live byte relations (same file digests, same manifest/receipt shape);
nothing here claims to verify the remote paths themselves.

Covered: a valid stack fixture is accepted and recorded by both entries; a missing
receipt at RUN, a changed manifest digest, a tampered or missing `new_files`, an old
multicall receipt offered as the stack receipt, and both protocols declared together are
all refused; the 0.2 multicall-only path still passes unchanged.
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

LIVE_EVIDENCE = ROOT / "transfers/ae-stack-startup-20260914-r1/evidence"
STACKED = Path(os.environ.get(
    "AE_LETTA_NO_COMPACTION_SOURCE",
    ROOT / ".ae-verify-src/letta-no-compaction-patch/letta-v1"))
MULTICALL = Path(os.environ.get(
    "AE_LETTA_PATCHED_SOURCE", ROOT / ".ae-verify-src/letta-multicall-patch/letta-v1"))
STACK_MANIFEST = ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json"
NO_COMPACTION_MANIFEST = ROOT / "deployment-assets/letta-no-compaction/manifest.json"
CANDIDATE_250K = (ROOT / "configs"
                  / "ae-01__re-multiturn__siliconflow.capacity-250k-measured-candidate.json")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class StackEvidenceFixture:
    """A local stack manifest + a REAL service receipt over the local checkout."""

    def __init__(self, tmp: Path):
        self.tmp = Path(tmp)
        self.tree = STACKED
        self.manifest = self._manifest()
        self.manifest_sha = _sha256(self.manifest)
        self.receipt = self._receipt()

    def _manifest(self):
        """The live manifest with the checkout paths re-pointed at this checkout.

        The DIGESTS are the live ones; the local checkout carries the same bytes (the
        test asserts that below), so the byte relations the validators check hold.
        """
        record = json.loads(STACK_MANIFEST.read_text(encoding="utf-8"))
        record["letta_checkout"] = str(self.tree)
        record["multicall_checkout"] = str(MULTICALL)
        record["baseline_checkout"] = str(
            Path(os.environ.get("AE_VERIFY_ROOT", ROOT / ".ae-verify-src")) / "letta-v1")
        record["multicall_manifest"] = str(
            ROOT / "deployment-assets/letta-multicall/manifest.json")
        record["no_compaction_manifest"] = str(NO_COMPACTION_MANIFEST)
        path = self.tmp / "stack-manifest.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def _receipt(self):
        """Write the receipt the SERVICE would write, with the REAL bootstrap gate."""
        spec = importlib.util.spec_from_file_location(
            "ae_bootstrap_stack_fixture",
            ROOT / "scripts/deployment/letta_bootstrap.py")
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        import ae_multicall

        mapping = {module: (filename, key)
                   for key, (module, filename) in ae_multicall.STACK_SOURCES.items()}

        def find(name, package=None):
            if name not in mapping:
                return None
            _filename, key = mapping[name]
            return type("Spec", (), {"origin": str(self.tree / key)})()
        env = {
            ae_multicall.STACK_ENV_VAR: ae_multicall.STACK_PROFILE_VERSION,
            ae_multicall.STACK_MANIFEST_ENV_VAR: str(self.manifest),
            ae_multicall.MODULE_SHA_ENV_VAR: json.loads(
                self.manifest.read_text(encoding="utf-8"))["compat_module"]["sha256"],
            # R3-2: the stacked profile composes the receive compatibility policy, so the
            # startup gate requires that activation to be declared with it.
            ae_multicall.RECEIVE_PROFILE_ENV_VAR: ae_multicall.PROFILE_VERSION,
            "AE_LETTA_MULTICALL_SUPPORT": str(ROOT),
            ae_multicall.STACK_RECEIPT_ENV_VAR: str(self.tmp / "patch-stack-load.json"),
        }
        receipt = bootstrap.install_patch_stack_gate(env, find_spec=find)
        (self.tmp / "patch-stack-load.json").write_text(
            json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        return self.tmp / "patch-stack-load.json"


class CapacityServiceEvidenceTests(unittest.TestCase):
    def setUp(self):
        if not STACK_MANIFEST.is_file() or not STACKED.is_dir():
            self.skipTest("the stacked checkout or manifest is absent")
        self.cli = importlib.util.spec_from_file_location(
            "ae_01_cloud_re_multiturn_stack", ROOT / "scripts/ae_01_cloud_re_multiturn.py")
        self.driver = importlib.util.module_from_spec(self.cli)
        self.cli.loader.exec_module(self.driver)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fixture = StackEvidenceFixture(self.tmp)
        self.config_path = self._config()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))

    def _config(self):
        """The reviewed 250k candidate (capacity + measured endpoint + target assets)."""
        if not CANDIDATE_250K.is_file():
            self.skipTest("the 250k candidate config is absent")
        record = json.loads(CANDIDATE_250K.read_text(encoding="utf-8"))
        path = self.tmp / "capacity-250k.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def _provenance(self, **overrides):
        kwargs = {"require_receipt": False}
        kwargs.update(overrides)
        return self.driver.provenance(self.config, self.config_path, None, None,
                                      vita_source=None, **kwargs)

    # -- the CURRENT patch fixture, and what the historical snapshot is still for -----
    def test_the_current_fixture_receipt_describes_the_current_patch_bytes(self):
        """The fixture's receipt must describe the patch THIS TREE carries.

        The service writes the receipt, through the real bootstrap gate, over the current
        stacked checkout. Its digests are compared with the bytes on disk here, so the
        fixture cannot quietly describe an older patch than the one under test.
        """
        fixture_manifest = self.fixture.manifest
        manifest = json.loads(fixture_manifest.read_text(encoding="utf-8"))
        written = json.loads(self.fixture.receipt.read_text(encoding="utf-8"))
        self.assertEqual(written["profile_version"], "ae-no-compaction-stack-1")
        self.assertEqual(written["checkout"], str(STACKED))
        self.assertEqual(sorted(written["new_files"]), sorted(manifest["new_files"]))
        # the manifest maps a file to its digest; the receipt maps it to path + digest
        for name, digest in manifest["new_files"].items():
            self.assertEqual(_sha256(STACKED / name), digest, name)
            self.assertEqual(written["new_files"][name]["sha256"], digest, name)
        for name, digest in manifest["patched_files"].items():
            self.assertEqual(_sha256(STACKED / name), digest, name)
            self.assertEqual(written["patched_files"][name]["sha256"], digest, name)

    def test_the_historical_snapshot_is_a_past_record_and_rejects_the_current_tree(self):
        """`transfers/ae-stack-startup-20260914-r1` is a SEALED point-in-time record.

        It describes the tree that was running on 2026-09-14, so it must NOT be expected to
        match this checkout after the patch changes - and it must be REFUSED against the
        current manifest rather than silently accepted. Keeping it as the expected value
        would have made the suite demand that the current patch never change.
        """
        live_path = LIVE_EVIDENCE / "patch-stack-load.json"
        if not live_path.is_file():
            self.skipTest("the sealed 2026-09-14 stack startup record is absent")
        live = json.loads(live_path.read_text(encoding="utf-8"))
        self.assertEqual(live["profile_version"], "ae-no-compaction-stack-1")
        # the historical receipt still describes its OWN frozen tree faithfully
        changed = [name for name, entry in live["new_files"].items()
                   if _sha256(STACKED / name) != entry["sha256"]]
        self.assertTrue(changed, "the sealed snapshot must not match a rebuilt tree")
        self.assertTrue(changed,
                        "the sealed snapshot unexpectedly matches this tree; if the patch "
                        "was reverted, this test is no longer meaningful")
        # ... and the current validators refuse it against the CURRENT manifest
        from ae_multicall import validate_stack_receipt
        with self.assertRaises(Exception) as caught:
            validate_stack_receipt(live, self.fixture.manifest, self.fixture.manifest_sha)
        self.assertIn("stack_receipt_manifest_sha", str(caught.exception))

    # -- the driver's real provenance entry ----------------------------------------
    def test_a_valid_stack_pair_is_recorded_by_the_run_entry(self):
        record = self._provenance(require_receipt=True,
                                  patch_stack_manifest=self.fixture.manifest,
                                  patch_stack_load_receipt=self.fixture.receipt)
        self.assertEqual(record["patch_stack_protocol"], "required")
        self.assertTrue(record["patch_stack_loading_verified"])
        self.assertTrue(record["patch_stack_chain_verified"])
        manifest = record["patch_stack_manifest"]
        self.assertEqual(manifest["sha256"], self.fixture.manifest_sha)
        self.assertEqual(manifest["profile_version"], "ae-no-compaction-stack-1")
        self.assertEqual(sorted(manifest["new_files"]),
                         ["letta/helpers/ae_no_compaction.py",
                          "letta/helpers/ae_qwen_tokenizer.py"])
        receipt = record["patch_stack_live_loading"]
        self.assertEqual(receipt["path"], str(self.fixture.receipt))
        self.assertEqual(receipt["sha256"], _sha256(self.fixture.receipt))
        self.assertTrue(receipt["instance_id"])
        self.assertEqual(receipt["checkout"], str(STACKED))

    def test_a_run_without_the_stack_receipt_is_refused(self):
        with self.assertRaises(RuntimeError) as raised:
            self._provenance(require_receipt=True,
                             patch_stack_manifest=self.fixture.manifest)
        self.assertIn("no stacked service load receipt", str(raised.exception))
        # PLAN/PREFLIGHT may keep the explicit unverified state instead.
        record = self._provenance(patch_stack_manifest=self.fixture.manifest)
        self.assertFalse(record["patch_stack_loading_verified"])
        self.assertEqual(record["patch_stack_protocol"], "declared")
        record = self._provenance()
        self.assertEqual(record["patch_stack_protocol"], "absent")
        self.assertFalse(record["patch_stack_loading_verified"])

    def test_a_changed_manifest_digest_is_refused(self):
        tampered = self.tmp / "tampered-manifest.json"
        document = json.loads(self.fixture.manifest.read_text(encoding="utf-8"))
        document["new_files"]["letta/helpers/ae_no_compaction.py"] = "0" * 64
        tampered.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(Exception) as raised:
            self._provenance(require_receipt=True, patch_stack_manifest=tampered,
                             patch_stack_load_receipt=self.fixture.receipt)
        # Either the manifest itself refuses (digest does not describe the tree) or the
        # receipt's own new_files check does; both are refusals, never a silent pass.
        self.assertTrue(str(raised.exception))

    def test_an_old_multicall_receipt_cannot_stand_in_for_the_stack_receipt(self):
        """The live evidence for this exact case: multicall validation is not it."""
        multicall_receipt = self.tmp / "multicall-load.json"
        multicall_receipt.write_text(json.dumps({
            "profile_version": "ae-multicall-receive-compat-1", "instance_id": "x",
            "module_path": str(ROOT / "ae_multicall.py"), "module_sha256": "0" * 64,
            "checkout": str(MULTICALL), "patched_files": {},
            "manifest_sha256": "0" * 64, "source_verified": True,
            "source_verification": "importlib_find_spec_before_letta_import"}),
            encoding="utf-8")
        with self.assertRaises(Exception) as raised:
            self._provenance(require_receipt=True, patch_stack_manifest=self.fixture.manifest,
                             patch_stack_load_receipt=multicall_receipt)
        self.assertIn("stack", str(raised.exception).lower())
        import ae_multicall
        with self.assertRaises(ae_multicall.MulticallPolicyError) as raised:
            ae_multicall.validate_stack_receipt(
                json.loads(multicall_receipt.read_text(encoding="utf-8")),
                json.loads(self.fixture.manifest.read_text(encoding="utf-8")),
                self.fixture.manifest_sha)
        self.assertIn("stack_receipt_fields", str(raised.exception))

    def test_declaring_both_service_identities_is_refused(self):
        """A multicall receipt AND stack evidence name two different services."""
        multicall_manifest = ROOT / "deployment-assets/letta-multicall/manifest.json"
        with self.assertRaises(RuntimeError) as raised:
            self.driver.provenance(self.config, self.config_path, multicall_manifest,
                                   self.tmp / "multicall-load.json",
                                   require_receipt=False,
                                   patch_stack_manifest=self.fixture.manifest,
                                   patch_stack_load_receipt=self.fixture.receipt)
        self.assertIn("not both", str(raised.exception))

    def test_the_multicall_manifest_may_accompany_the_stack_evidence(self):
        """The offline patch proof is not a service identity: a 0.3 run keeps it."""
        multicall_manifest = json.loads(
            (ROOT / "deployment-assets/letta-multicall/manifest.json").read_text(
                encoding="utf-8"))
        multicall_manifest["letta_checkout"] = str(MULTICALL)
        path = self.tmp / "multicall-manifest.json"
        path.write_text(json.dumps(multicall_manifest, ensure_ascii=False), encoding="utf-8")
        record = self.driver.provenance(
            self.config, self.config_path, path, None, require_receipt=True,
            patch_stack_manifest=self.fixture.manifest,
            patch_stack_load_receipt=self.fixture.receipt)
        self.assertTrue(record["multicall_manifest"]["path"].endswith(
            "multicall-manifest.json"))
        self.assertTrue(record["patch_stack_loading_verified"])
        self.assertNotIn("multicall_live_loading", record)

    def test_the_receipt_requires_its_manifest(self):
        with self.assertRaises(RuntimeError) as raised:
            self._provenance(require_receipt=False,
                             patch_stack_load_receipt=self.fixture.receipt)
        self.assertIn("without the stacked manifest", str(raised.exception))

    def test_the_02_multicall_path_is_unchanged(self):
        """A 0.2 config still uses the multicall manifest and its own receipt."""
        record = json.loads((ROOT / "configs"
                             / "ae-01__re-multiturn__siliconflow.original-candidate.json")
                            .read_text(encoding="utf-8"))
        path = self.tmp / "multicall-config.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        manifest = json.loads(
            (ROOT / "deployment-assets/letta-multicall/manifest.json").read_text(
                encoding="utf-8"))
        local_manifest = self.tmp / "multicall-manifest.json"
        manifest["letta_checkout"] = str(MULTICALL)
        local_manifest.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        provenance = self.driver.provenance(record, path, local_manifest, None,
                                            require_receipt=False, vita_source=None)
        self.assertIn("multicall_manifest", provenance)
        self.assertFalse(provenance["multicall_loading_verified"])
        self.assertNotIn("patch_stack_manifest", provenance)
        # ... and a stack manifest can never pass the 0.2 manifest reader.
        import ae_multicall
        with self.assertRaises(ae_multicall.MulticallPolicyError):
            ae_multicall.read_manifest(self.fixture.manifest)


class CapacityServiceEvidenceAuditTests(unittest.TestCase):
    """The post-hoc audit re-validates the same evidence from bytes."""

    def setUp(self):
        if not STACK_MANIFEST.is_file() or not STACKED.is_dir():
            self.skipTest("the stacked checkout or manifest is absent")
        import re_multiturn_chain as chain
        import test_ae_cloud_re_multiturn as t
        self.chain = chain
        if t.real_sample(12) is None:
            self.skipTest("the fixed dataset is not available")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fixture = StackEvidenceFixture(self.tmp)
        self.config_path = self._config()

    def _config(self):
        import test_ae_no_compaction as no_compaction
        case = no_compaction.RealChainCapacityTests()
        path = case._config(self.tmp, 262144, verified=True)
        record = json.loads(path.read_text(encoding="utf-8"))
        # Keep the fixture's own exploration basis (the target assets are what the
        # live run used; this chain fixture only needs a valid 0.3 declaration).
        record["capacity"]["tokenizer"]["exploration_only"] = True
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def _audit(self, **overrides):
        # A 0.3 run carries the STACK service receipt; it does not also carry the
        # multicall service receipt (two identities), so the chain fixture is built
        # with that client artefact switched off. The offline multicall MANIFEST is
        # still built and recorded, exactly as a real 0.3 run does.
        overrides.setdefault("receipt", False)
        fixture = self.chain.build_full_chain(self.tmp, config_path=self.config_path,
                                              **overrides)
        return fixture, self.chain.audit_of(fixture)

    def test_the_audit_accepts_and_revalidates_the_stack_evidence(self):
        fixture, report = self._audit(
            patch_stack_manifest=self.fixture.manifest,
            patch_stack_load_receipt=self.fixture.receipt)
        provenance = json.loads((fixture["run_dir"] / "plan.json").read_text(
            encoding="utf-8"))["provenance"]
        self.assertEqual(provenance["patch_stack_protocol"], "declared")
        scope = report["scope"]["patch_stack"]
        self.assertTrue(scope["declared"])
        self.assertEqual(scope["sha256"], self.fixture.manifest_sha)
        self.assertEqual(scope["new_files"],
                         ["letta/helpers/ae_no_compaction.py",
                          "letta/helpers/ae_qwen_tokenizer.py"])
        self.assertTrue(report["scope"]["patch_stack_receipt_verified"])
        self.assertEqual(scope["receipt"]["sha256"], _sha256(self.fixture.receipt))
        self.assertEqual(scope["receipt"]["checkout"], str(STACKED))
        self.assertIn("stack_evidence_gate", report["checks_executed"])

    def test_a_tampered_new_file_is_refused_by_the_audit(self):
        """The receipt is re-read and re-hashed: a changed `new_files` is refused.

        The driver's recorded digest is tampered WITH the file, so the audit cannot be
        satisfied by the record: the reviewer's validator has to refuse the receipt.
        """
        fixture, _ = self._audit(patch_stack_manifest=self.fixture.manifest,
                                 patch_stack_load_receipt=self.fixture.receipt)
        plan_path = fixture["run_dir"] / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        document = json.loads(self.fixture.receipt.read_text(encoding="utf-8"))
        document["new_files"]["letta/helpers/ae_no_compaction.py"]["sha256"] = "0" * 64
        self.fixture.receipt.write_text(json.dumps(document, ensure_ascii=False),
                                       encoding="utf-8")
        digest = _sha256(self.fixture.receipt)
        plan["provenance"]["patch_stack_live_loading"]["sha256"] = digest
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = self.chain.audit_of(fixture)
        codes = [entry["code"] for entry in report["invalid_reasons"]]
        self.assertIn("stack_receipt_validation_failed", codes)
        self.assertFalse(report["input_audit_passed"])

    def test_a_receipt_changed_after_the_run_is_refused_by_its_digest(self):
        fixture, _ = self._audit(patch_stack_manifest=self.fixture.manifest,
                                 patch_stack_load_receipt=self.fixture.receipt)
        document = json.loads(self.fixture.receipt.read_text(encoding="utf-8"))
        document["instance_id"] = "rewritten-after-the-fact"
        self.fixture.receipt.write_text(json.dumps(document, ensure_ascii=False),
                                       encoding="utf-8")
        report = self.chain.audit_of(fixture)
        codes = [entry["code"] for entry in report["invalid_reasons"]]
        self.assertIn("stack_receipt_bytes_changed", codes)

    def test_a_missing_new_files_set_is_refused(self):
        """A manifest whose `new_files` was emptied is refused, record or not."""
        fixture, _ = self._audit(patch_stack_manifest=self.fixture.manifest,
                                 patch_stack_load_receipt=self.fixture.receipt)
        plan_path = fixture["run_dir"] / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        document = json.loads(self.fixture.manifest.read_text(encoding="utf-8"))
        document["new_files"] = {}
        self.fixture.manifest.write_text(json.dumps(document, ensure_ascii=False),
                                        encoding="utf-8")
        plan["provenance"]["patch_stack_manifest"]["sha256"] = _sha256(self.fixture.manifest)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = self.chain.audit_of(fixture)
        codes = [entry["code"] for entry in report["invalid_reasons"]]
        self.assertIn("patch_stack_manifest_unreadable", codes)
        self.assertFalse(report["input_audit_passed"])

    def test_a_completed_run_that_declares_a_stack_but_no_receipt_is_refused(self):
        fixture, report = self._audit(patch_stack_manifest=self.fixture.manifest)
        codes = [entry["code"] for entry in report["invalid_reasons"]]
        self.assertIn("executed_stack_run_without_service_receipt", codes)
        self.assertFalse(report["scope"]["patch_stack_receipt_verified"])


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
