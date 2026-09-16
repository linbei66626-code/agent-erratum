"""R3-1: the SERVICE startup entry proves which stacked checkout it loaded.

The r2 review's finding was that the capacity patch was only ever loaded by the
driver-side agents, while the service load gate verified a different, multicall-only
checkout. This module exercises the new opt-in STACK contract offline:

* the REAL bootstrap gate (`scripts/deployment/letta_bootstrap.py`) runs with the
  REAL stacked manifest and the REAL `find_spec`, bound to the checkout the service
  would import, and writes the load receipt the deployment needs;
* the receipt is then validated against the manifest AND against the bytes on disk,
  so "the service loaded the capacity patch" is a checked statement;
* every negative case is checked: no declaration, a file changed in the checkout, a
  manifest whose digests belong to another tree, and an import path that resolves
  outside the declared checkout. All of them refuse before Letta is imported.

Nothing here claims a live service ran: it is offline evidence about the startup
entry's own resolution and hashing.
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

import ae_multicall  # noqa: E402

STACK_MANIFEST = ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json"
STACKED = Path(os.environ.get(
    "AE_LETTA_NO_COMPACTION_SOURCE",
    ROOT / ".ae-verify-src/letta-no-compaction-patch/letta-v1"))
MULTICALL = Path(os.environ.get(
    "AE_LETTA_PATCHED_SOURCE", ROOT / ".ae-verify-src/letta-multicall-patch/letta-v1"))


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "ae_letta_bootstrap_stack", ROOT / "scripts/deployment/letta_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_launcher():
    spec = importlib.util.spec_from_file_location(
        "ae_letta_local_stack", ROOT / "scripts/deployment/letta_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PatchStackManifestTests(unittest.TestCase):
    """The manifest itself: an exact file set, real digests, no self-reported truth."""

    def setUp(self):
        if not STACK_MANIFEST.is_file() or not STACKED.is_dir():
            self.skipTest("the stacked manifest or checkout is absent")
        self.manifest = json.loads(STACK_MANIFEST.read_text(encoding="utf-8"))

    def test_the_manifest_is_the_stacked_chain_and_verifies_against_its_checkout(self):
        record = ae_multicall.read_stack_manifest(STACK_MANIFEST)
        self.assertEqual(record["schema"], ae_multicall.STACK_SCHEMA)
        self.assertEqual(record["profile_version"], ae_multicall.STACK_PROFILE_VERSION)
        self.assertEqual(set(record["patched_files"]), set(ae_multicall.STACK_PATCHED_FILES))
        self.assertEqual(set(record["new_files"]), set(ae_multicall.STACK_NEW_FILES))
        self.assertEqual(Path(record["letta_checkout"]).resolve(), STACKED.resolve())
        # Every declared digest is the digest of the file in that checkout.
        for key in ("patched_files", "new_files"):
            for name, digest in record[key].items():
                self.assertEqual(_sha256(STACKED / name), digest, name)
        env = {ae_multicall.STACK_ENV_VAR: ae_multicall.STACK_PROFILE_VERSION,
               ae_multicall.STACK_MANIFEST_ENV_VAR: str(STACK_MANIFEST),
               ae_multicall.MODULE_SHA_ENV_VAR: record["compat_module"]["sha256"]}
        self.assertTrue(ae_multicall.verify_stack_gate(env, module=ae_multicall))
        # A declaration naming another profile, manifest or module is refused.
        for broken in ({**env, ae_multicall.STACK_ENV_VAR: "ae-multicall-receive-compat-1"},
                       {**env, ae_multicall.STACK_MANIFEST_ENV_VAR: str(ROOT / "absent.json")},
                       {**env, ae_multicall.MODULE_SHA_ENV_VAR: "0" * 64}):
            self.assertFalse(ae_multicall.verify_stack_gate(broken, module=ae_multicall))

    def test_the_stacked_checkout_is_the_reviewed_multicall_patch_plus_this_patch(self):
        """The files the multicall patch owns are IDENTICAL in both checkouts."""
        reviewed = json.loads((ROOT / "deployment-assets/letta-multicall/manifest.json")
                              .read_text(encoding="utf-8"))
        for name in ("letta/schemas/message.py", "letta/helpers/ae_multicall_compat.py"):
            expected = reviewed["patched_files"][name]["patched_sha256"]
            self.assertEqual(_sha256(STACKED / name), expected, name)
            self.assertEqual(_sha256(MULTICALL / name), expected, name)
        # ... and the files THIS patch owns are DIFFERENT from the multicall checkout,
        # i.e. the stack really contains both patches rather than only one.
        for name in ("letta/agents/letta_agent_v3.py",
                     "letta/services/summarizer/compact.py"):
            self.assertNotEqual(_sha256(STACKED / name), _sha256(MULTICALL / name), name)


class PatchStackLoadGateTests(unittest.TestCase):
    """The startup entry: resolve what the service imports, or refuse to start."""

    def setUp(self):
        if not STACK_MANIFEST.is_file() or not STACKED.is_dir():
            self.skipTest("the stacked manifest or checkout is absent")
        self.bootstrap = _load_bootstrap()
        self.record = json.loads(STACK_MANIFEST.read_text(encoding="utf-8"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.receipt = Path(self.tmp.name) / "patch-stack-load.json"
        self.env = {
            ae_multicall.STACK_ENV_VAR: ae_multicall.STACK_PROFILE_VERSION,
            ae_multicall.STACK_MANIFEST_ENV_VAR: str(STACK_MANIFEST),
            ae_multicall.MODULE_SHA_ENV_VAR: self.record["compat_module"]["sha256"],
            # R3-2: the stacked profile COMPOSES the receive compatibility policy, so the
            # declaration a launch hands over names it too, and the startup gate refuses
            # to start without it (a loaded source is not an active policy).
            ae_multicall.RECEIVE_PROFILE_ENV_VAR: ae_multicall.PROFILE_VERSION,
            "AE_LETTA_MULTICALL_SUPPORT": str(ROOT),
            ae_multicall.STACK_RECEIPT_ENV_VAR: str(self.receipt),
        }

    def _find_spec(self, checkout=STACKED):
        """The REAL module names, resolved inside one checkout."""
        mapping = {module: (filename, key)
                   for key, (module, filename) in ae_multicall.STACK_SOURCES.items()}

        def find(name, package=None):
            if name not in mapping:
                return None
            filename, key = mapping[name]
            return type("Spec", (), {"origin": str(Path(checkout) / key)})()
        return find

    def _gate(self, env=None, checkout=STACKED):
        return self.bootstrap.install_patch_stack_gate(
            env or self.env, find_spec=self._find_spec(checkout))

    def test_no_declaration_keeps_the_old_paths_untouched(self):
        self.assertIsNone(self.bootstrap.install_patch_stack_gate({}))
        self.assertFalse(self.receipt.exists())
        # ... and the multicall-only profile still verifies and writes ITS receipt.
        # The reviewed manifest records the path its own generator ran against, so a
        # manifest is regenerated over the checkout present here - the same production
        # generator, not a hand-written one.
        multispec = importlib.util.spec_from_file_location(
            "ae_write_manifest_stack_test",
            ROOT / "deployment-assets/letta-multicall/write_manifest.py")
        writer = importlib.util.module_from_spec(multispec)
        multispec.loader.exec_module(writer)
        multicall_manifest = Path(self.tmp.name) / "multicall-manifest.json"
        record = writer.build_manifest(
            patched=MULTICALL,
            pinned=Path(os.environ.get("AE_VERIFY_ROOT", ROOT / ".ae-verify-src")) / "letta-v1",
            project=ROOT, letta_checkout=MULTICALL, out=multicall_manifest)
        multicall_receipt = Path(self.tmp.name) / "multicall-load.json"
        multicall_env = {
            ae_multicall.LETTA_ENV_VAR: ae_multicall.PROFILE_VERSION,
            ae_multicall.MANIFEST_ENV_VAR: str(multicall_manifest),
            ae_multicall.MODULE_SHA_ENV_VAR: record["compat_module"]["sha256"],
            "AE_LETTA_MULTICALL_SUPPORT": str(ROOT),
            ae_multicall.LAUNCH_RECEIPT_ENV_VAR: str(multicall_receipt),
        }
        written = self.bootstrap.install_multicall_gate(
            multicall_env, find_spec=lambda name, package=None: (
                None if name not in {
                    "letta.agents.letta_agent_v3",
                    "letta.schemas.message",
                    "letta.helpers.ae_multicall_compat"}
                else type("Spec", (), {
                    "origin": str(MULTICALL / {
                        "letta.agents.letta_agent_v3": "letta/agents/letta_agent_v3.py",
                        "letta.schemas.message": "letta/schemas/message.py",
                        "letta.helpers.ae_multicall_compat":
                            "letta/helpers/ae_multicall_compat.py"}[name])})()))
        self.assertTrue(multicall_receipt.is_file())
        self.assertEqual(written["profile_version"], ae_multicall.PROFILE_VERSION)
        self.assertFalse(self.receipt.exists())

    def test_a_verified_stack_writes_a_receipt_bound_to_the_imported_files(self):
        receipt = self._gate()
        self.assertTrue(self.receipt.is_file())
        written = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(written, receipt)
        self.assertEqual(set(written), set(ae_multicall.STACK_RECEIPT_FIELDS))
        self.assertEqual(written["profile_version"], ae_multicall.STACK_PROFILE_VERSION)
        self.assertEqual(written["checkout"], str(STACKED.resolve()))
        self.assertEqual(written["manifest_sha256"], _sha256(STACK_MANIFEST))
        self.assertEqual(written["source_verification"],
                         "importlib_find_spec_before_letta_import")
        self.assertTrue(written["source_verified"])
        # The receipt names the RESOLVED path and digest of every file the service
        # would import - the manifest's own strings would not be enough.
        for key in ("patched_files", "new_files"):
            self.assertEqual(set(written[key]), set(self.record[key]))
            for name, entry in written[key].items():
                self.assertEqual(entry["path"], str((STACKED / name).resolve()), name)
                self.assertEqual(entry["sha256"], _sha256(STACKED / name), name)
                self.assertEqual(entry["sha256"], self.record[key][name], name)
        # The receipt validates against the manifest AND against the bytes on disk.
        ae_multicall.validate_stack_receipt(written, self.record, _sha256(STACK_MANIFEST))

    def test_a_helper_changed_after_the_manifest_refuses_before_letta_is_imported(self):
        changed = Path(self.tmp.name) / "changed-checkout"
        shutil.copytree(STACKED, changed)
        target = changed / "letta/helpers/ae_qwen_tokenizer.py"
        target.write_bytes(target.read_bytes() + b"\n# tampered\n")
        manifest = json.loads(STACK_MANIFEST.read_text(encoding="utf-8"))
        manifest["letta_checkout"] = str(changed)
        variant = Path(self.tmp.name) / "changed-manifest.json"
        variant.write_text(json.dumps(manifest), encoding="utf-8")
        env = dict(self.env, **{ae_multicall.STACK_MANIFEST_ENV_VAR: str(variant)})
        with self.assertRaises(RuntimeError) as raised:
            self._gate(env, checkout=changed)
        self.assertIn("did not verify against its manifest", str(raised.exception))
        self.assertFalse(self.receipt.exists())

    def test_an_import_path_that_leaves_the_declared_checkout_is_refused(self):
        # The manifest is correct, but the import machinery resolves the SAME module
        # names to another tree: that is exactly what a wrong checkout looks like.
        with self.assertRaises(RuntimeError) as raised:
            self._gate(checkout=MULTICALL)
        self.assertIn("outside the declared stacked checkout", str(raised.exception))
        self.assertFalse(self.receipt.exists())
        # The resolver has its OWN digest check too, for a resolution that stays
        # inside the declared checkout but does not hash to the declared bytes.
        mutated = Path(self.tmp.name) / "inside-checkout"
        shutil.copytree(STACKED, mutated)
        target = mutated / "letta/helpers/ae_no_compaction.py"
        target.write_bytes(target.read_bytes() + b"\n# tampered\n")
        manifest = json.loads(STACK_MANIFEST.read_text(encoding="utf-8"))
        manifest["letta_checkout"] = str(mutated)
        with self.assertRaises(RuntimeError) as raised:
            self.bootstrap.resolve_stack_sources(
                manifest, find_spec=self._find_spec(mutated))
        self.assertIn("bytes differ from the reviewed stacked patch", str(raised.exception))

    def test_a_manifest_from_another_tree_cannot_describe_this_one(self):
        manifest = json.loads(STACK_MANIFEST.read_text(encoding="utf-8"))
        manifest["new_files"]["letta/helpers/ae_qwen_tokenizer.py"] = "0" * 64
        variant = Path(self.tmp.name) / "wrong-digest.json"
        variant.write_text(json.dumps(manifest), encoding="utf-8")
        env = dict(self.env, **{ae_multicall.STACK_MANIFEST_ENV_VAR: str(variant)})
        with self.assertRaises(RuntimeError) as raised:
            self._gate(env)
        self.assertIn("did not verify against its manifest", str(raised.exception))

    def test_a_tampered_receipt_is_refused_by_the_validator(self):
        receipt = self._gate()
        broken = json.loads(json.dumps(receipt))
        broken["patched_files"]["letta/agents/letta_agent_v3.py"]["sha256"] = "0" * 64
        with self.assertRaises(ae_multicall.MulticallPolicyError):
            ae_multicall.validate_stack_receipt(broken, self.record, _sha256(STACK_MANIFEST))
        missing = json.loads(json.dumps(receipt))
        del missing["new_files"]
        with self.assertRaises(ae_multicall.MulticallPolicyError):
            ae_multicall.validate_stack_receipt(missing, self.record, _sha256(STACK_MANIFEST))


class PatchStackLauncherTests(unittest.TestCase):
    """The launcher: the stack is opt-in, and its receipt must validate at startup."""

    def setUp(self):
        if not STACK_MANIFEST.is_file() or not STACKED.is_dir():
            self.skipTest("the stacked manifest or checkout is absent")
        self.local = _load_launcher()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The counting declaration is part of the runtime now: the service gate counts
        # every request, so the assets and the identity they belong to must be handed
        # over explicitly. This file uses the official assets this host really has.
        from ae_multiturn_capacity import load_official_tokenizer_assets
        assets = load_official_tokenizer_assets()
        self.runtime = self.local.PatchStackRuntime(
            manifest=STACK_MANIFEST, support=ROOT,
            receipt=Path(self.tmp.name) / "patch-stack-load.json",
            profile=self.local.STACK_PROFILE_VERSION,
            tokenizer_json=Path(assets["tokenizer_json"]),
            tokenizer_config=Path(assets["tokenizer_config"]),
            tokenizer_target="Qwen/Qwen3-30B-A3B-Instruct-2507",
            tokenizer_asset_target="Qwen/Qwen3-30B-A3B-Instruct-2507",
            tokenizer_revision=str(assets["revision"]))

    def test_the_runtime_validates_the_manifest_and_names_the_child_environment(self):
        record = self.runtime.validate()
        self.assertEqual(record["profile_version"], ae_multicall.STACK_PROFILE_VERSION)
        env = self.runtime.env(Path(self.tmp.name))
        self.assertEqual(env["AE_LETTA_PATCH_STACK_PROFILE"], self.local.STACK_PROFILE_VERSION)
        self.assertEqual(env["AE_LETTA_PATCH_STACK_MANIFEST"], str(STACK_MANIFEST))
        self.assertEqual(env["AE_LETTA_PATCH_STACK_RECEIPT"],
                         str(Path(self.tmp.name) / "patch-stack-load.json"))
        self.assertEqual(env["AE_LETTA_MULTICALL_SUPPORT"], str(ROOT))
        self.assertEqual(env["AE_LETTA_MULTICALL_MODULE_SHA256"],
                         record["compat_module"]["sha256"])
        # The counting keys cross too, with the resolved (real) asset paths.
        for key in self.local.PatchStackRuntime.TOKENIZER_KEYS:
            self.assertIn(key, env, key)
        self.assertEqual(env["AE_QWEN_TOKENIZER_TARGET"],
                         "Qwen/Qwen3-30B-A3B-Instruct-2507")
        with self.assertRaises(RuntimeError):
            self.local.PatchStackRuntime(manifest=STACK_MANIFEST, support=ROOT,
                                         receipt=Path(self.tmp.name) / "r.json",
                                         profile="ae-multicall-receive-compat-1").validate()

    def test_the_launcher_refuses_a_stack_launch_without_the_bootstrap_wrapper(self):
        """ONLY the wrapper rule: the real entry chain is covered elsewhere.

        `tests/test_ae_no_compaction_launcher_entry.py` drives the real
        `Deployment.start`/`source_check`/`env` and the bootstrap decision chain; this
        case keeps the single rule that the gate must run in the service process, which
        is checked before any launch, so `source_check` is stubbed here on purpose.
        """
        project = Path(self.tmp.name) / "project"
        (project / "vendor/letta-v1").mkdir(parents=True)
        (project / "deployment/private").mkdir(parents=True)
        deploy = self.local.Deployment(project)
        from unittest.mock import patch
        with patch.object(self.local.Deployment, "source_check"), \
             patch.object(self.local.Deployment, "db_config", return_value={}), \
             patch.object(self.local.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError) as raised:
                deploy.start("http://127.0.0.1:8000/v1", 8284, patch_stack=self.runtime)
        self.assertIn("bootstrap", str(raised.exception))
        popen.assert_not_called()


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
