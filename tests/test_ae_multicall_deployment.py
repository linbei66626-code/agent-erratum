"""Offline tests for the AE multicall deployment wiring (no service is started).

The review found the launcher's whitelisted child environment dropped every
`AE_LETTA_MULTICALL_*` variable, so a parent `export` never reached the service
and the patched receive block fell back to `not_declared` -> truncate to one.
These tests exercise the REAL launcher env builder and the REAL bootstrap gate
offline: no service is started, no database is touched and no model is called.

Only the environment construction and the gate are exercised, because those are
the two places a launch could silently lose the declared protocol.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from test_ae_multicall_letta import (BASELINE_SOURCE, PATCHED_SOURCE,  # noqa: E402
                                     multicall_environment,
                                     write_production_manifest)

CHECKOUT = Path(os.environ.get("AE_LETTA_PATCHED_SOURCE", PATCHED_SOURCE))
MANIFEST_ENV = "AE_LETTA_MULTICALL_MANIFEST"
MODULE_SHA_ENV = "AE_LETTA_MULTICALL_MODULE_SHA256"
SUPPORT_ENV = "AE_LETTA_MULTICALL_SUPPORT"
RECEIPT_ENV = "AE_LETTA_MULTICALL_LOAD_RECEIPT"
PROFILE_ENV = "AE_LETTA_MULTICALL_PROFILE"
PATCH_FILE = ROOT / "deployment-assets/letta-multicall/ae-multicall-letta.patch"
SHIM_SOURCE = (ROOT / "deployment-assets/letta-multicall/files/letta/helpers"
                      / "ae_multicall_compat.py")
UPSTREAM_PATCHED_FILES = ("letta/agents/letta_agent_v3.py", "letta/schemas/message.py")
UNRELATED_TRACKED_FILE = "letta/helpers/converters.py"


class ReachedPopen(Exception):
    """The offline observation point: the launch was about to create the child."""


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherEnvironmentTests(unittest.TestCase):
    """The real `Deployment.env()` must carry the declared protocol into the child."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = load_module("ae_letta_local",
                                 ROOT / "scripts/deployment/letta_local.py")
        self.multicall = __import__("ae_multicall")
        self.manifest = write_production_manifest(self.tmp.name, CHECKOUT)
        self.deployment = self.local.Deployment.__new__(self.local.Deployment)
        self.deployment.project = ROOT
        self.deployment.venv = ROOT / ".venv-letta"
        self.deployment.base = Path(self.tmp.name)
        self.deployment.record = Path(self.tmp.name) / "record"
        self.deployment.record.mkdir(parents=True, exist_ok=True)
        self.deployment.private = Path(self.tmp.name) / "private"
        self.deployment.private.mkdir(parents=True, exist_ok=True)
        self.deployment.password = ""

    def runtime(self):
        return self.local.MulticallRuntime(manifest=self.manifest,
                                           support=ROOT,
                                           receipt=self.deployment.record / "multicall-load.json",
                                           profile=self.multicall.PROFILE_VERSION)

    def test_without_the_runtime_the_environment_is_unchanged(self):
        env = self.deployment.env()
        for key in self.local.MULTICALL_ENV_KEYS:
            self.assertNotIn(key, env)
        self.assertNotIn("PYTHONPATH", env)

    def test_the_declared_runtime_crosses_into_the_child_environment(self):
        env = self.deployment.env(multicall=self.runtime())
        for key in self.local.MULTICALL_ENV_KEYS:
            self.assertIn(key, env)
        self.assertEqual(env[PROFILE_ENV], self.multicall.PROFILE_VERSION)
        self.assertEqual(env[MANIFEST_ENV], str(self.manifest))
        self.assertEqual(env[SUPPORT_ENV], str(ROOT))
        # The child is told where to write its receipt; the parent writes nothing.
        self.assertFalse(Path(env[RECEIPT_ENV]).exists())
        # No blanket PYTHONPATH widening: the whole point of the whitelist.
        self.assertNotIn("PYTHONPATH", env)

    def test_the_child_environment_verifies_in_the_project_gate(self):
        env = self.deployment.env(multicall=self.runtime())
        gate_env = {self.multicall.LETTA_ENV_VAR: env[PROFILE_ENV],
                    self.multicall.MANIFEST_ENV_VAR: env[MANIFEST_ENV],
                    self.multicall.MODULE_SHA_ENV_VAR: env[MODULE_SHA_ENV]}
        self.assertTrue(self.multicall.verify_letta_gate(gate_env))

    def test_a_missing_or_wrong_declaration_is_refused_before_launch(self):
        runtime = self.runtime()
        broken = {
            "absent manifest": lambda: setattr(runtime, "manifest",
                                               Path(self.tmp.name) / "absent.json"),
            "absent support": lambda: setattr(runtime, "support",
                                              Path(self.tmp.name) / "absent"),
            "wrong profile": lambda: setattr(runtime, "profile", "ae-multicall-999"),
        }
        for label, mutate in broken.items():
            with self.subTest(label=label):
                fresh = self.runtime()
                runtime = fresh
                mutate()
                with self.assertRaises(RuntimeError):
                    fresh.validate(profile_version=self.multicall.PROFILE_VERSION,
                                   schema=self.multicall.MANIFEST_SCHEMA)

    def test_start_requires_the_bootstrap_wrapper_for_the_declared_runtime(self):
        """`start` refuses the plain CLI when a protocol is declared.

        The gate and the receipt live in the bootstrap wrapper, so starting the
        plain entrypoint with a declared protocol would leave the service
        unverified. `start` must refuse rather than launch; the refusal happens
        before the port, the executable or any child process is touched.
        """
        from unittest.mock import patch
        # This test isolates the wrapper guard, so it stubs the deployment's own
        # preconditions. The REAL source gate is exercised against the same opt-in
        # runtime in `LauncherSourceGateTests`, which never mocks it.
        with patch.object(self.local.Deployment, "source_check"), \
             patch.object(self.local.Deployment, "db_config", return_value={}), \
             patch.object(self.local.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError) as caught:
                self.deployment.start("http://127.0.0.1:8000/v1", 8283,
                                      multicall=self.runtime())
        self.assertIn("bootstrap", str(caught.exception))
        popen.assert_not_called()


class BootstrapGateTests(unittest.TestCase):
    """The real bootstrap gate runs in the service process and writes the receipt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bootstrap = load_module("ae_letta_bootstrap",
                                     ROOT / "scripts/deployment/letta_bootstrap.py")
        self.multicall = __import__("ae_multicall")
        # A resolvable checkout: the reviewed patched files plus the package files
        # `find_spec` needs to name. The environment has no Letta installed, so the
        # gate's real job -- checking what the import machinery resolves -- is
        # exercised through a `find_spec` bound to this tree.
        self.checkout = Path(self.tmp.name) / "letta-v1"
        for name in self.multicall.REQUIRED_PATCHED_FILES:
            target = self.checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CHECKOUT / name, target)
        for package in ("letta", "letta/agents", "letta/schemas", "letta/helpers"):
            (self.checkout / package).mkdir(parents=True, exist_ok=True)
            (self.checkout / package / "__init__.py").write_text("", encoding="utf-8")
        self.manifest = write_production_manifest(self.tmp.name, self.checkout)
        self.receipt = Path(self.tmp.name) / "multicall-load.json"
        # The gate reads the PROCESS environment variable names, exactly as the
        # launcher writes them for the child.
        self.env = {
            PROFILE_ENV: self.multicall.PROFILE_VERSION,
            MANIFEST_ENV: str(self.manifest),
            MODULE_SHA_ENV: self.multicall.live_module_sha(),
            SUPPORT_ENV: str(ROOT),
            RECEIPT_ENV: str(self.receipt),
        }

    def find_spec(self, origin_checkout=None):
        """A `find_spec` that resolves the real module names inside a checkout."""
        checkout = Path(origin_checkout or self.checkout)
        mapping = {
            "letta.agents.letta_agent_v3": "letta/agents/letta_agent_v3.py",
            "letta.schemas.message": "letta/schemas/message.py",
            "letta.helpers.ae_multicall_compat": "letta/helpers/ae_multicall_compat.py",
        }

        def find(name, package=None):
            if name not in mapping:
                return None
            return type("Spec", (), {"origin": str(checkout / mapping[name])})()
        return find

    def gate(self, env=None):
        return self.bootstrap.install_multicall_gate(env or self.env,
                                                     find_spec=self.find_spec())

    def test_no_profile_means_the_legacy_path(self):
        self.assertIsNone(self.bootstrap.install_multicall_gate({}))
        self.assertFalse(self.receipt.exists())

    def test_a_verified_profile_writes_a_service_produced_receipt(self):
        receipt = self.gate()
        self.assertTrue(self.receipt.is_file())
        written = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(written, receipt)
        self.assertEqual(set(written), set(self.multicall.LAUNCH_RECEIPT_FIELDS))
        self.assertEqual(written["profile_version"], self.multicall.PROFILE_VERSION)
        self.assertTrue(written["instance_id"])
        self.assertEqual(Path(written["module_path"]).resolve(),
                         Path(self.multicall.__file__).resolve())
        self.assertEqual(written["module_sha256"], self.multicall.live_module_sha())
        self.assertEqual(Path(written["checkout"]).resolve(), self.checkout.resolve())
        self.assertEqual(set(written["patched_files"]),
                         set(self.multicall.REQUIRED_PATCHED_FILES))
        for entry in written["patched_files"].values():
            self.assertEqual(set(entry), {"path", "sha256"})
            self.assertTrue(Path(entry["path"]).is_file())

    def test_the_receipt_satisfies_the_audit_contract(self):
        receipt = self.gate()
        self.assertEqual(set(receipt), set(self.multicall.LAUNCH_RECEIPT_FIELDS))
        record = self.multicall.read_manifest(self.manifest)
        for name, entry in receipt["patched_files"].items():
            self.assertEqual(entry["sha256"],
                             record["patched_files"][name]["patched_sha256"])

    def test_a_wrong_or_missing_declaration_stops_before_letta(self):
        cases = {
            "missing manifest": lambda env: env.pop(MANIFEST_ENV),
            "missing module digest": lambda env: env.pop(MODULE_SHA_ENV),
            "missing support path": lambda env: env.pop(SUPPORT_ENV),
            "wrong module digest": lambda env: env.__setitem__(MODULE_SHA_ENV, "0" * 64),
            "absent support module": lambda env: env.__setitem__(
                SUPPORT_ENV, str(Path(self.tmp.name) / "absent")),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                env = dict(self.env)
                if self.receipt.exists():
                    self.receipt.unlink()
                mutate(env)
                with self.assertRaises(RuntimeError):
                    self.gate(env)
                self.assertFalse(self.receipt.exists(),
                                 "no receipt may be written for a refused launch")
                # `letta` was never imported by the gate, so no model path exists.
                self.assertNotIn("letta.agents.letta_agent_v3", sys.modules)


if __name__ == "__main__":
    unittest.main()


class FreshProcessImportTests(unittest.TestCase):
    """The launcher must import the support module in a PLAIN script environment.

    Running `python scripts/deployment/letta_local.py` puts the SCRIPT directory on
    `sys.path`, not the project root, and nothing pre-imports `ae_multicall`. The
    r3 review reproduced `ModuleNotFoundError` there. These tests run the launcher
    in a fresh interpreter with that exact clean search path: the old behaviour
    fails, and the explicit support-path load succeeds.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manifest = write_production_manifest(self.tmp.name, CHECKOUT)

    def run_child(self, script):
        import subprocess
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run([sys.executable, "-B", "-c", script],
                              capture_output=True, text=True, cwd=ROOT / "scripts/deployment",
                              env=environment, timeout=120)

    def test_a_bare_import_fails_in_the_script_environment(self):
        """The old shape is a real failure in this environment, not a theory."""
        result = self.run_child(
            "import sys; print('ae_multicall' in sys.modules);\n"
            "import ae_multicall")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ModuleNotFoundError", result.stderr)
        self.assertIn("ae_multicall", result.stderr)

    def test_the_explicit_support_path_loads_and_validates(self):
        script = (
            "import importlib.util, json, sys\n"
            f"spec = importlib.util.spec_from_file_location('ll', {str(ROOT / 'scripts/deployment/letta_local.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "assert 'ae_multicall' not in sys.modules, 'must not be pre-imported'\n"
            "print('script_dir_only', 'ae_multicall' not in sys.modules)\n"
            f"runtime = m.MulticallRuntime(manifest={str(self.manifest)!r}, support={str(ROOT)!r},\n"
            f"                             receipt={str(Path(self.tmp.name) / 'r.json')!r},\n"
            "                             profile='ae-multicall-receive-compat-1')\n"
            "record = runtime.validate(profile_version='ae-multicall-receive-compat-1',\n"
            "                          schema='ae-letta-multicall-manifest-1')\n"
            "print('validated_files', sorted(record['patched_files']))\n"
            "print('loaded_from', sys.modules['ae_multicall'].__file__)\n")
        result = self.run_child(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("script_dir_only True", result.stdout)
        self.assertIn("validated_files", result.stdout)
        self.assertIn(str(ROOT / "ae_multicall.py"), result.stdout)

    def test_a_wrong_support_path_is_refused_not_silently_imported(self):
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('ll', {str(ROOT / 'scripts/deployment/letta_local.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            f"runtime = m.MulticallRuntime(manifest={str(self.manifest)!r}, support={str(Path(self.tmp.name) / 'absent')!r},\n"
            f"                             receipt={str(Path(self.tmp.name) / 'r.json')!r},\n"
            "                             profile='ae-multicall-receive-compat-1')\n"
            "try:\n"
            "    runtime.validate(profile_version='ae-multicall-receive-compat-1',\n"
            "                     schema='ae-letta-multicall-manifest-1')\n"
            "    print('NOT_REFUSED')\n"
            "except RuntimeError as exc:\n"
            "    print('REFUSED', str(exc)[:60])\n")
        result = self.run_child(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REFUSED", result.stdout)


class ResolvedSourceBindingTests(unittest.TestCase):
    """The receipt must bind what THIS process resolves, not the manifest's paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bootstrap = load_module("ae_letta_bootstrap_binding",
                                     ROOT / "scripts/deployment/letta_bootstrap.py")
        self.multicall = __import__("ae_multicall")
        self.declared = Path(self.tmp.name) / "declared"
        for name in self.multicall.REQUIRED_PATCHED_FILES:
            target = self.declared / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CHECKOUT / name, target)
        for package in ("letta", "letta/agents", "letta/schemas", "letta/helpers"):
            (self.declared / package).mkdir(parents=True, exist_ok=True)
            (self.declared / package / "__init__.py").write_text("", encoding="utf-8")
        self.manifest = write_production_manifest(self.tmp.name, self.declared)
        # A SECOND, correct-looking checkout that the import machinery will not use.
        self.other = Path(self.tmp.name) / "other"
        shutil.copytree(self.declared, self.other)

    def find_spec(self, checkout):
        mapping = {
            "letta.agents.letta_agent_v3": "letta/agents/letta_agent_v3.py",
            "letta.schemas.message": "letta/schemas/message.py",
            "letta.helpers.ae_multicall_compat": "letta/helpers/ae_multicall_compat.py",
        }

        def find(name, package=None):
            if name not in mapping:
                return None
            return type("Spec", (), {"origin": str(Path(checkout) / mapping[name])})()
        return find

    def env(self, receipt):
        return {
            PROFILE_ENV: self.multicall.PROFILE_VERSION,
            MANIFEST_ENV: str(self.manifest),
            MODULE_SHA_ENV: self.multicall.live_module_sha(),
            SUPPORT_ENV: str(ROOT),
            RECEIPT_ENV: str(receipt),
        }

    def test_a_manifest_pointing_elsewhere_than_the_resolved_tree_is_refused(self):
        """The r3 counterexample: declared scratch tree, resolved original tree."""
        receipt = Path(self.tmp.name) / "wrong.json"
        with self.assertRaises(RuntimeError) as caught:
            self.bootstrap.install_multicall_gate(
                self.env(receipt), find_spec=self.find_spec(self.other))
        self.assertIn("outside the declared checkout", str(caught.exception))
        self.assertFalse(receipt.exists(), "a refused launch writes no receipt")

    def test_the_receipt_records_the_resolved_paths_and_digests(self):
        receipt_path = Path(self.tmp.name) / "right.json"
        receipt = self.bootstrap.install_multicall_gate(
            self.env(receipt_path), find_spec=self.find_spec(self.declared))
        self.assertTrue(receipt["source_verified"])
        self.assertEqual(receipt["source_verification"],
                         "importlib_find_spec_before_letta_import")
        for name, entry in receipt["patched_files"].items():
            resolved = Path(entry["path"]).resolve()
            self.assertEqual(resolved, (self.declared / name).resolve())
            self.assertEqual(entry["sha256"],
                             hashlib.sha256(resolved.read_bytes()).hexdigest())
        self.assertEqual(Path(receipt["checkout"]).resolve(), self.declared.resolve())

    def test_a_digest_mismatch_inside_the_declared_checkout_is_refused(self):
        """Even inside the declared tree, altered bytes are refused.

        The manifest gate catches this first (the declared tree no longer equals
        pinned baseline + reviewed patch), so the refusal is named there; either
        way no receipt is written.
        """
        (self.declared / "letta/agents/letta_agent_v3.py").write_text(
            "# tampered\n", encoding="utf-8")
        receipt = Path(self.tmp.name) / "tampered.json"
        with self.assertRaises(RuntimeError) as caught:
            self.bootstrap.install_multicall_gate(
                self.env(receipt), find_spec=self.find_spec(self.declared))
        self.assertIn("did not verify against its manifest", str(caught.exception))
        self.assertFalse(receipt.exists())


class ProductionRunReceiptTests(unittest.TestCase):
    """A real RUN must carry the service receipt; PLAN/PREFLIGHT stay offline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cli = load_module("ae_cli_receipt", ROOT / "scripts/ae_01_cloud_re_pair.py")
        self.multicall = __import__("ae_multicall")
        self.checkout = Path(self.tmp.name) / "declared"
        for name in self.multicall.REQUIRED_PATCHED_FILES:
            target = self.checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CHECKOUT / name, target)
        for package in ("letta", "letta/agents", "letta/schemas", "letta/helpers"):
            (self.checkout / package).mkdir(parents=True, exist_ok=True)
            (self.checkout / package / "__init__.py").write_text("", encoding="utf-8")
        self.manifest = write_production_manifest(self.tmp.name, self.checkout)
        self.config_path = ROOT / "configs/ae-01__re-pair__siliconflow.multicall-candidate.json"
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.receipt = Path(self.tmp.name) / "multicall-load.json"
        self.receipt.write_text(json.dumps({
            "profile_version": self.multicall.PROFILE_VERSION,
            "instance_id": "service-0001",
            "module_path": str(ROOT / "ae_multicall.py"),
            "module_sha256": self.multicall.live_module_sha(),
            "checkout": str(self.checkout),
            "patched_files": {name: {"path": str(self.checkout / name),
                                     "sha256": hashlib.sha256(
                                         (self.checkout / name).read_bytes()).hexdigest()}
                              for name in self.multicall.REQUIRED_PATCHED_FILES},
            "manifest_sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
            "source_verified": True,
            "source_verification": "importlib_find_spec_before_letta_import",
        }), encoding="utf-8")

    def provenance(self, **kwargs):
        return self.cli.provenance(self.config, self.config_path, self.manifest, **kwargs)

    def test_plan_records_the_manifest_and_stays_explicitly_unverified(self):
        record = self.provenance()
        self.assertIn("multicall_manifest", record)
        self.assertNotIn("multicall_live_loading", record)
        self.assertFalse(record["multicall_loading_verified"])
        self.assertIn("NOT checked", record["multicall_loading_note"])

    def test_plan_records_a_validated_receipt_reference(self):
        record = self.provenance(multicall_load_receipt=self.receipt)
        self.assertEqual(record["multicall_live_loading"]["path"], str(self.receipt))
        self.assertEqual(record["multicall_live_loading"]["sha256"],
                         hashlib.sha256(self.receipt.read_bytes()).hexdigest())
        # The reference is recorded AND the contents were validated by the shared
        # gate, so this is no longer "a file was named".
        self.assertTrue(record["multicall_loading_verified"])

    def test_bad_receipt_contents_are_refused_before_execution(self):
        """The r4 blocker: garbage JSON or a wrong field set is not a receipt."""
        cases = {
            "garbage json": ("garbage.json", "{not json"),
            "only an instance id": ("bogus.json", json.dumps({"instance_id": "bogus"})),
        }
        for label, (name, text) in cases.items():
            with self.subTest(label=label):
                path = Path(self.tmp.name) / name
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    self.provenance(multicall_load_receipt=path, require_receipt=True)
                with self.assertRaises(RuntimeError):
                    self.provenance(multicall_load_receipt=path)

    def test_wrong_profile_manifest_sha_file_sha_and_path_are_refused(self):
        good = json.loads(self.receipt.read_text(encoding="utf-8"))
        mutations = {
            "wrong profile": lambda r: r.__setitem__("profile_version", "ae-multicall-999"),
            "wrong manifest sha": lambda r: r.__setitem__("manifest_sha256", "0" * 64),
            "wrong file sha": lambda r: r["patched_files"].__setitem__(
                sorted(r["patched_files"])[0],
                {**r["patched_files"][sorted(r["patched_files"])[0]], "sha256": "0" * 64}),
            "path outside checkout": lambda r: r["patched_files"].__setitem__(
                sorted(r["patched_files"])[0],
                {"path": str(ROOT / "ae_adapter.py"),
                 "sha256": r["patched_files"][sorted(r["patched_files"])[0]]["sha256"]}),
            "missing fields": lambda r: r.pop("source_verified"),
            "source unverified": lambda r: r.__setitem__("source_verified", False),
            "wrong checkout": lambda r: r.__setitem__("checkout", str(ROOT)),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                broken = json.loads(json.dumps(good))
                mutate(broken)
                path = Path(self.tmp.name) / (label.replace(" ", "_") + ".json")
                path.write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    self.provenance(multicall_load_receipt=path, require_receipt=True)

    def test_a_run_without_a_receipt_refuses_before_any_model_request(self):
        with self.assertRaises(RuntimeError) as caught:
            self.provenance(require_receipt=True)
        self.assertIn("no service load receipt", str(caught.exception))

    def test_a_run_with_a_missing_receipt_file_refuses(self):
        with self.assertRaises(RuntimeError) as caught:
            self.provenance(multicall_load_receipt=Path(self.tmp.name) / "absent.json",
                            require_receipt=True)
        self.assertIn("absent", str(caught.exception))

    def test_a_run_with_the_receipt_records_the_reference(self):
        record = self.provenance(multicall_load_receipt=self.receipt, require_receipt=True)
        self.assertIn("multicall_live_loading", record)


class ReceiptBypassTests(unittest.TestCase):
    """Neither manifest flag may let an invalid receipt reach execution.

    The r4 review showed `applied_to_live_server` gated the receipt check, so a
    default manifest let `{"instance_id": "bogus"}` through. This exercises the
    PRODUCTION CLI provenance (the same call the RUN path makes before creating any
    transport) with a valid receipt, an invalid receipt, and no receipt, for BOTH
    manifest states.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.multicall = __import__("ae_multicall")
        self.cli = load_module("ae_cli_bypass", ROOT / "scripts/ae_01_cloud_re_pair.py")
        self.checkout = Path(self.tmp.name) / "declared"
        for name in self.multicall.REQUIRED_PATCHED_FILES:
            target = self.checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CHECKOUT / name, target)
        for package in ("letta", "letta/agents", "letta/schemas", "letta/helpers"):
            (self.checkout / package).mkdir(parents=True, exist_ok=True)
            (self.checkout / package / "__init__.py").write_text("", encoding="utf-8")
        self.manifest = write_production_manifest(self.tmp.name, self.checkout)
        self.config_path = ROOT / "configs/ae-01__re-pair__siliconflow.multicall-candidate.json"
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))

    def write_receipt(self, *, valid=True):
        path = Path(self.tmp.name) / ("receipt-valid.json" if valid else "receipt-bogus.json")
        if not valid:
            path.write_text(json.dumps({"instance_id": "bogus"}), encoding="utf-8")
            return path
        # The manifest digest is read at write time, so a caller must set the
        # manifest state FIRST; changing it later invalidates the receipt.
        path.write_text(json.dumps({
            "profile_version": self.multicall.PROFILE_VERSION,
            "instance_id": "service-bypass-0001",
            "module_path": str(ROOT / "ae_multicall.py"),
            "module_sha256": self.multicall.live_module_sha(),
            "checkout": str(self.checkout),
            "patched_files": {name: {
                "path": str(self.checkout / name),
                "sha256": hashlib.sha256((self.checkout / name).read_bytes()).hexdigest()}
                for name in self.multicall.REQUIRED_PATCHED_FILES},
            "manifest_sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
            "source_verified": True,
            "source_verification": "importlib_find_spec_before_letta_import",
        }), encoding="utf-8")
        return path

    def set_live_flag(self, value):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["verification"]["applied_to_live_server"] = value
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def provenance(self, receipt_path, *, require=True):
        return self.cli.provenance(self.config, self.config_path, self.manifest,
                                   receipt_path, require_receipt=require)

    def test_a_bogus_receipt_is_refused_for_both_manifest_states(self):
        bogus = self.write_receipt(valid=False)
        for live in (False, True):
            with self.subTest(applied_to_live_server=live):
                self.set_live_flag(live)
                with self.assertRaises(RuntimeError):
                    self.provenance(bogus)

    def test_no_receipt_is_refused_for_both_manifest_states(self):
        for live in (False, True):
            with self.subTest(applied_to_live_server=live):
                self.set_live_flag(live)
                with self.assertRaises(RuntimeError):
                    self.provenance(None)

    def test_a_valid_receipt_is_recorded_for_both_manifest_states(self):
        for live in (False, True):
            with self.subTest(applied_to_live_server=live):
                # The manifest is fixed FIRST; the receipt references its digest.
                self.set_live_flag(live)
                valid = self.write_receipt(valid=True)
                record = self.provenance(valid)
                self.assertTrue(record["multicall_loading_verified"])
                self.assertIn("multicall_live_loading", record)

    def test_the_manifest_digest_pins_one_fixed_manifest(self):
        """Changing the manifest after the receipt invalidates the receipt."""
        self.set_live_flag(False)
        valid = self.write_receipt(valid=True)
        self.provenance(valid)
        self.set_live_flag(True)  # the manifest bytes changed; the receipt did not
        with self.assertRaises(RuntimeError):
            self.provenance(valid)


class LauncherSourceGateTests(unittest.TestCase):
    """The REAL source gate must accept only the manifest's reviewed patch.

    The lab showed the launcher's `source_check()` refused every tracked change
    unconditionally, so a correctly opt-in multicall deployment could never start:
    the two reviewed Letta files were rejected before the manifest or the gate was
    ever consulted, and the existing wiring tests could not see it because they
    mock `source_check`.

    These tests run the PRODUCTION `source_check` / `start` chain over a real git
    pinned checkout with the reviewed patch applied in the working tree. Nothing
    here mocks `source_check`, the Git status output, or a manifest check: the
    support module, the manifest, `verify_patch_identity`, the patch application and
    `verify_letta_gate` are all the real implementations. The only stubs are the
    deployment's own database credential lookup and the final `subprocess.Popen`,
    which is an explicit offline observation point. No service, database, socket or
    model is involved.
    """

    @classmethod
    def setUpClass(cls):
        cls.local = load_module("ae_letta_local_source_gate",
                                ROOT / "scripts/deployment/letta_local.py")
        cls.multicall = __import__("ae_multicall")
        if not (BASELINE_SOURCE / ".git").is_dir():
            raise RuntimeError(
                "the pinned Letta checkout must be a real git working tree for the "
                f"source-gate tests: {BASELINE_SOURCE}")
        head = subprocess.run(["git", "-C", str(BASELINE_SOURCE), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        if head.returncode or head.stdout.strip() != cls.local.COMMIT:
            raise RuntimeError(
                f"the pinned checkout is not at {cls.local.COMMIT}: {head.stdout.strip()!r}")
        status = subprocess.run(["git", "-C", str(BASELINE_SOURCE), "status", "--porcelain",
                                 "--untracked-files=no"], capture_output=True, text=True)
        if status.stdout.strip():
            raise RuntimeError("the pinned checkout must be clean for the source-gate tests")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.counter = 0

    def next_path(self, label):
        self.counter += 1
        return Path(self.tmp.name) / f"{self.counter:02d}-{label}"

    def git(self, tree, *arguments):
        done = subprocess.run(["git", "-C", str(tree), *arguments],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def patched_tree(self, label="patched", *, patch_file=True, shim=True):
        """A real pinned git checkout with the reviewed patch in the worktree."""
        target = self.next_path(label)
        shutil.copytree(BASELINE_SOURCE, target, symlinks=True)
        if patch_file:
            applied = subprocess.run(["git", "-C", str(target), "apply", str(PATCH_FILE)],
                                     capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stderr)
        if shim:
            destination = target / "letta/helpers/ae_multicall_compat.py"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SHIM_SOURCE, destination)
        return target

    def manifest_for(self, tree):
        directory = self.next_path("manifest")
        directory.mkdir()
        return write_production_manifest(directory, tree)

    def runtime(self, deploy, manifest):
        return self.local.MulticallRuntime(
            manifest=manifest, support=ROOT,
            receipt=deploy.record / "multicall-load.json",
            profile=self.multicall.PROFILE_VERSION)

    def deployment(self, source, label="run"):
        run_dir = self.next_path(label)
        run_dir.mkdir()
        record = run_dir / "record"
        record.mkdir()
        deploy = self.local.Deployment.__new__(self.local.Deployment)
        deploy.project = ROOT
        deploy.source = Path(source)
        deploy.venv = run_dir / "venv"
        (deploy.venv / "bin").mkdir(parents=True)
        (deploy.venv / "bin/letta").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (deploy.venv / "bin/python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        deploy.base = run_dir
        deploy.record = record
        deploy.private = run_dir / "private"
        deploy.private.mkdir()
        deploy.process = deploy.private / "letta-process.json"
        deploy.credentials = deploy.private / "letta-db.json"
        deploy.password = ""
        deploy.steps = 0
        return deploy

    def observe_popen(self, deploy):
        """Record the launch call instead of creating it; delegate everything else."""
        real_popen = subprocess.Popen
        calls = []
        bootstrap = str(ROOT / "scripts/deployment/letta_bootstrap.py")
        executable = str(deploy.venv / "bin/letta")

        def observing(argv, *args, **kwargs):
            joined = " ".join(str(part) for part in argv)
            if bootstrap in joined or str(argv[0]) == executable:
                calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
                raise ReachedPopen(list(argv))
            return real_popen(argv, *args, **kwargs)  # the gate's own git calls

        return patch.object(self.local.subprocess, "Popen", observing), calls

    def free_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def reach_launch(self, deploy, *, multicall=None, bounded=True):
        observer, calls = self.observe_popen(deploy)
        with patch.object(self.local.Deployment, "db_config",
                          return_value={"password": "unused", "port": 5432}), observer:
            with self.assertRaises(ReachedPopen):
                deploy.start("http://127.0.0.1:8000/v1", self.free_port(),
                             bounded_nltk_startup=bounded, explicit_llm_config=True,
                             multicall=multicall)
        return calls

    def assert_refused(self, deploy, *, multicall, label):
        observer, calls = self.observe_popen(deploy)
        with patch.object(self.local.Deployment, "db_config",
                          return_value={"password": "unused", "port": 5432}), observer:
            with self.assertRaises(RuntimeError) as caught:
                deploy.start("http://127.0.0.1:8000/v1", self.free_port(),
                             bounded_nltk_startup=True, explicit_llm_config=True,
                             multicall=multicall)
        self.assertEqual(calls, [], f"{label} must be refused before any Popen call")
        return caught.exception

    def test_the_reviewed_patch_reaches_the_launch_point_under_opt_in(self):
        tree = self.patched_tree("patched")
        deploy = self.deployment(tree)
        calls = self.reach_launch(deploy, multicall=self.runtime(deploy, self.manifest_for(tree)))
        self.assertEqual(len(calls), 1)
        self.assertIn("letta_bootstrap.py", " ".join(calls[0]["argv"]))
        for key in self.local.MULTICALL_ENV_KEYS:
            self.assertIn(key, calls[0]["env"])
        launch = json.loads((deploy.record / "launch.json").read_text(encoding="utf-8"))
        self.assertEqual(launch["service_mode"], "ae_multicall_receive_compat")
        source = json.loads((deploy.record / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["git_head"], self.local.COMMIT)
        self.assertEqual(source["reviewed_patch"]["patched_checkout"], str(tree.resolve()))
        self.assertEqual(source["reviewed_patch"]["tracked_changes_accepted"],
                         sorted(UPSTREAM_PATCHED_FILES))
        self.assertEqual(source["reviewed_patch"]["compat_module_sha256"],
                         self.multicall.live_module_sha())

    def test_a_clean_legacy_tree_is_accepted_and_reaches_the_launch_point(self):
        tree = self.patched_tree("clean", patch_file=False, shim=False)
        deploy = self.deployment(tree)
        calls = self.reach_launch(deploy, multicall=None, bounded=False)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("letta_bootstrap.py", " ".join(calls[0]["argv"]))
        for key in self.local.MULTICALL_ENV_KEYS:
            self.assertNotIn(key, calls[0]["env"])
        source = json.loads((deploy.record / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["git_head"], self.local.COMMIT)
        self.assertNotIn("reviewed_patch", source)

    def test_legacy_and_migrate_keep_the_strict_no_optin_rule(self):
        tree = self.patched_tree("patched")
        deploy = self.deployment(tree)
        with self.assertRaises(RuntimeError) as caught:
            deploy.source_check()
        self.assertIn("tracked files are dirty", str(caught.exception))
        # `migrate` calls the same gate with no opt-in, so a patched tree is still
        # refused before any database credential or alembic is touched.
        with self.assertRaises(RuntimeError) as caught:
            deploy.migrate()
        self.assertIn("tracked files are dirty", str(caught.exception))

    def test_every_unreviewed_source_is_refused_before_the_launch_point(self):
        other = {"tree": None}

        def extra_tree():
            if other["tree"] is None:
                other["tree"] = self.patched_tree("other")
            return other["tree"]

        def broken_patch(manifest):
            corrupted = self.next_path("corrupt.patch")
            corrupted.write_bytes(PATCH_FILE.read_bytes() + b"\n# extra\n")
            record = json.loads(Path(manifest).read_text(encoding="utf-8"))
            record["patch"]["path"] = str(corrupted)
            replacement = self.next_path("corrupt-manifest.json")
            replacement.write_text(json.dumps(record), encoding="utf-8")
            return replacement

        cases = {
            "unpatched tree": (
                lambda t, m, d, r: self.git(t, "checkout", "--", *UPSTREAM_PATCHED_FILES),
                "does not match the reviewed patch bytes"),
            "edited patched bytes": (
                lambda t, m, d, r: (t / "letta/schemas/message.py").write_bytes(
                    (t / "letta/schemas/message.py").read_bytes() + b"\n# edited\n"),
                "does not match the reviewed patch bytes"),
            "missing shim": (
                lambda t, m, d, r: (t / "letta/helpers/ae_multicall_compat.py").unlink(),
                "absent from the service source"),
            "extra tracked modification": (
                lambda t, m, d, r: (t / UNRELATED_TRACKED_FILE).write_bytes(
                    (t / UNRELATED_TRACKED_FILE).read_bytes() + b"\n# extra\n"),
                "tracked modification outside the reviewed patch"),
            "staged reviewed change": (
                lambda t, m, d, r: self.git(t, "add", "letta/schemas/message.py"),
                "staged change"),
            "staged extra change": (
                lambda t, m, d, r: (
                    (t / UNRELATED_TRACKED_FILE).write_bytes(
                        (t / UNRELATED_TRACKED_FILE).read_bytes() + b"\n# staged\n"),
                    self.git(t, "add", UNRELATED_TRACKED_FILE)),
                "staged change"),
            "deleted tracked path": (
                lambda t, m, d, r: (t / UNRELATED_TRACKED_FILE).unlink(),
                "deleted tracked path"),
            "renamed tracked path": (
                lambda t, m, d, r: self.git(
                    t, "mv", UNRELATED_TRACKED_FILE, "letta/helpers/renamed.py"),
                "renamed or copied"),
            "hidden change (skip-worktree)": (
                lambda t, m, d, r: self.git(
                    t, "update-index", "--skip-worktree", "letta/agents/letta_agent_v3.py"),
                "not present as tracked changes"),
            "wrong HEAD": (
                lambda t, m, d, r: self.git(
                    t, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "moved head"),
                "commit differs or tracked files are dirty"),
            "manifest names another checkout": (
                lambda t, m, d, r: setattr(d, "source", extra_tree()),
                "another checkout"),
            "wrong profile": (
                lambda t, m, d, r: setattr(r, "profile", "ae-multicall-999"),
                "unsupported AE multicall profile"),
            "absent manifest": (
                lambda t, m, d, r: setattr(r, "manifest", self.next_path("absent.json")),
                "manifest is absent"),
            "reviewed patch bytes altered": (
                lambda t, m, d, r: setattr(r, "manifest", broken_patch(m)),
                "do not match patch.sha256"),
        }
        for label, (mutate, expected) in cases.items():
            with self.subTest(label=label):
                tree = self.patched_tree(label.replace(" ", "-"))
                manifest = self.manifest_for(tree)
                deploy = self.deployment(tree)
                runtime = self.runtime(deploy, manifest)
                mutate(tree, manifest, deploy, runtime)
                error = self.assert_refused(deploy, multicall=runtime, label=label)
                self.assertIn(expected, str(error), f"{label} was refused for another reason")

    def test_a_manifest_for_another_checkout_cannot_be_used_for_this_one(self):
        """The manifest's `letta_checkout` is bound to the real service source."""
        first = self.patched_tree("first")
        second = self.patched_tree("second")
        deploy = self.deployment(second)
        runtime = self.runtime(deploy, self.manifest_for(first))
        error = self.assert_refused(deploy, multicall=runtime,
                                    label="manifest from another checkout")
        self.assertIn("another checkout", str(error))


if __name__ == "__main__":
    unittest.main()
