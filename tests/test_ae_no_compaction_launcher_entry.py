"""The launcher ENTRY chain for the stacked service, exercised for real (offline).

What this module proves, and what it deliberately substitutes:

* REAL: `letta_local.Deployment.start` / `source_check` / `env`, the real stack
  manifest, the real stacked checkout bytes, the real support module, the real
  bootstrap gate (`install_patch_stack_gate` with the child environment the launcher
  produced), the real receipt validator.
* SUBSTITUTED (offline dependencies only): the managed PostgreSQL credentials file
  (no database is contacted), the child process (`Popen` is faked and writes the
  receipt the real bootstrap would write, using the REAL gate), the health check
  (a loopback response object), and the provider (never involved in `start`).

The counterexamples are the point of the chain: a tampered stacked tree, a missing
counting declaration, a non-target asset without the explicit exploration opt-in, and
the two old paths (no opt-in, multicall opt-in) must all be refused by the same real
entry.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ae_multicall  # noqa: E402

STACKED = Path(os.environ.get(
    "AE_LETTA_NO_COMPACTION_SOURCE",
    ROOT / ".ae-verify-src/letta-no-compaction-patch/letta-v1"))
MULTICALL = Path(os.environ.get(
    "AE_LETTA_PATCHED_SOURCE", ROOT / ".ae-verify-src/letta-multicall-patch/letta-v1"))
STACK_MANIFEST = ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json"
MULTICALL_MANIFEST = ROOT / "deployment-assets/letta-multicall/manifest.json"
NO_COMPACTION_MANIFEST = ROOT / "deployment-assets/letta-no-compaction/manifest.json"
TARGET = "Qwen/Qwen3-30B-A3B-Instruct-2507"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _tokenizer_assets():
    """The official assets this host really has (the family cache, not the target)."""
    from ae_multiturn_capacity import load_official_tokenizer_assets
    assets = load_official_tokenizer_assets()
    return Path(assets["tokenizer_json"]), Path(assets["tokenizer_config"])


class _FakeProcess:
    def __init__(self, pid=424242):
        self.pid = pid

    def poll(self):
        return None


class _FakeHealth:
    """A context manager over the loopback health JSON (no socket is bound)."""

    def __init__(self, payload):
        self._buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self._buffer

    def __exit__(self, *_):
        return False


class LauncherEntryTests(unittest.TestCase):
    def setUp(self):
        if not STACKED.is_dir() or not STACK_MANIFEST.is_file():
            self.skipTest("the stacked checkout or manifest is absent")
        self.local = _load("ae_letta_local_entry", ROOT / "scripts/deployment/letta_local.py")
        self.bootstrap = _load("ae_letta_bootstrap_entry",
                               ROOT / "scripts/deployment/letta_bootstrap.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.tree = self.project / "vendor/letta-v1"
        shutil.copytree(STACKED, self.tree)
        (self.project / "deployment/private").mkdir(parents=True, exist_ok=True)
        (self.project / ".venv-letta/bin").mkdir(parents=True, exist_ok=True)
        (self.project / ".venv-letta/bin/letta").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.project / ".venv-letta/bin/python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shutil.copytree(ROOT / "scripts", self.project / "scripts")
        # The copied stacked checkout carries its OWN git repository: HEAD is the REAL
        # pinned Letta commit and the worktree holds exactly the patch chain's tracked
        # modifications. Nothing is re-initialised or re-committed here - faking the
        # identity is what made the previous round's fixture unable to prove it.
        head = self._git("rev-parse", "HEAD").strip()
        self.assertEqual(head, self.local.COMMIT,
                         "the stacked checkout must carry the pinned git identity")
        credentials = self.project / "deployment/private/letta-db.json"
        credentials.write_text(json.dumps({
            "password": "offline-fixture-password", "role": self.local.ROLE,
            "database": self.local.ROLE, "pgdata": str(self.local.PGDATA),
            "socket": str(self.local.PGSOCKET), "port": 5543}), encoding="utf-8")
        credentials.chmod(0o600)
        self.manifest = self._stack_manifest()
        self.tokenizer_json, self.tokenizer_config = _tokenizer_assets()

    def _git(self, *arguments, tree=None):
        import subprocess
        done = subprocess.run(["git", "-C", str(tree or self.tree), *arguments],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def _stack_manifest(self):
        """The real stack manifest, re-pointed at THIS test's copied checkout."""
        record = json.loads(STACK_MANIFEST.read_text(encoding="utf-8"))
        record["letta_checkout"] = str(self.tree)
        record["multicall_checkout"] = str(MULTICALL)
        record["baseline_checkout"] = str(
            Path(os.environ.get("AE_VERIFY_ROOT", ROOT / ".ae-verify-src")) / "letta-v1")
        record["multicall_manifest"] = str(MULTICALL_MANIFEST)
        record["no_compaction_manifest"] = str(NO_COMPACTION_MANIFEST)
        path = Path(self.tmp.name) / "stack-manifest.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _runtime(self, **overrides):
        kwargs = {
            "manifest": self.manifest, "support": ROOT,
            "receipt": Path(self.tmp.name) / f"receipt-{len(overrides)}-{os.urandom(4).hex()}.json",
            "profile": self.local.STACK_PROFILE_VERSION,
            "tokenizer_json": self.tokenizer_json,
            "tokenizer_config": self.tokenizer_config,
            "tokenizer_target": TARGET, "tokenizer_asset_target": TARGET,
            "tokenizer_revision": "offline-fixture",
        }
        kwargs.update(overrides)
        return self.local.PatchStackRuntime(**kwargs)

    def _deployment(self):
        return self.local.Deployment(self.project)

    def _find_spec(self, checkout=None):
        """The REAL module names, resolved inside one checkout."""
        mapping = {module: (filename, key)
                   for key, (module, filename) in ae_multicall.STACK_SOURCES.items()}
        root = Path(checkout or self.tree)

        def find(name, package=None):
            if name not in mapping:
                return None
            _filename, key = mapping[name]
            return type("Spec", (), {"origin": str(root / key)})()
        return find

    def _start(self, runtime, *, capture):
        """`Deployment.start` with ONLY db/process/health substituted."""
        from unittest.mock import patch
        deploy = self._deployment()
        real_popen = self.local.subprocess.Popen

        def fake_popen(argv, *args, **kwargs):
            # ONLY the service process is substituted. Every other Popen (the launcher's
            # own `git` calls inside the source gate) goes to the real implementation,
            # because `subprocess.run` resolves Popen from this same module.
            is_service = (kwargs.get("start_new_session")
                          and "letta_bootstrap.py" in " ".join(str(part) for part in argv))
            if not is_service:
                return real_popen(argv, *args, **kwargs)
            env = kwargs.get("env") or {}
            capture["argv"], capture["cwd"], capture["env"] = list(argv), kwargs.get("cwd"), dict(env)
            # The real bootstrap gate runs with the environment the launcher built,
            # in the process that would import Letta, and writes its own receipt.
            receipt = self.bootstrap.install_patch_stack_gate(env, find_spec=self._find_spec())
            self.assertIsNotNone(receipt)
            Path(env["AE_LETTA_PATCH_STACK_RECEIPT"]).write_text(
                json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
            return _FakeProcess()

        class _Opener:
            def open(self, url, timeout=None):
                return _FakeHealth({"status": "ok", "version": "0.16.8"})

        with patch.object(self.local.subprocess, "Popen", side_effect=fake_popen), \
             patch.object(self.local.urllib.request, "build_opener",
                          side_effect=lambda *a, **k: _Opener()):
            started = deploy.start("http://127.0.0.1:8000/v1", _free_port(),
                                   bounded_nltk_startup=True, explicit_llm_config=True,
                                   patch_stack=runtime)
        return deploy, started

    def test_the_real_entry_accepts_the_manifested_stack_and_hands_over_counting(self):
        """source_check -> env -> bootstrap: one normal case, nothing mocked in it."""
        os.environ["AE_QWEN_TOKENIZER_JSON"] = "/parent/planted/should-not-win.json"
        os.environ["AE_PARENT_SECRET"] = "must-not-cross"
        self.addCleanup(lambda: [os.environ.pop(key, None)
                                 for key in ("AE_QWEN_TOKENIZER_JSON", "AE_PARENT_SECRET")])
        capture = {}
        deploy, started = self._start(self._runtime(), capture=capture)
        # 1. The real source gate accepted the tree and recorded the stack identity.
        source = json.loads((deploy.record / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["git_head"], self.local.COMMIT)
        self.assertEqual(source["source_identity"], "pinned_git_head_plus_manifest_digests")
        self.assertEqual(source["patch_stack"]["tracked_changes_allowed"],
                         ["letta/agents/letta_agent_v3.py", "letta/schemas/message.py",
                          "letta/services/summarizer/compact.py"])
        self.assertEqual(sorted(source["patch_stack"]["tracked_changes_accepted"]),
                         source["patch_stack"]["tracked_changes_allowed"])
        self.assertEqual(source["patch_stack"]["stacked_checkout"], str(self.tree.resolve()))
        self.assertEqual(source["patch_stack"]["multicall_checkout"], str(MULTICALL.resolve()))
        record = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(source["patch_stack"]["patched_files"],
                         dict(sorted(record["patched_files"].items())))
        self.assertEqual(source["patch_stack"]["new_files"],
                         dict(sorted(record["new_files"].items())))
        # 2. The launch record carries the counting basis with REAL file digests.
        launch = json.loads((deploy.record / "launch.json").read_text(encoding="utf-8"))
        self.assertEqual(launch["service_mode"], "ae_no_compaction_stack")
        self.assertEqual(launch["tokenizer"]["json"]["sha256"], _sha256(self.tokenizer_json))
        self.assertEqual(launch["tokenizer"]["config"]["sha256"], _sha256(self.tokenizer_config))
        self.assertEqual(launch["tokenizer"]["target"], TARGET)
        self.assertTrue(launch["tokenizer"]["asset_is_target"])
        self.assertFalse(launch["tokenizer"]["exploration_only"])
        self.assertEqual(launch["tokenizer_child_keys"],
                         list(self.local.PatchStackRuntime.TOKENIZER_KEYS))
        # 3. The CHILD environment carries exactly those keys, from the declaration -
        #    not from this process's environment, which planted a bogus value.
        for key in self.local.PatchStackRuntime.TOKENIZER_KEYS:
            self.assertIn(key, capture["env"], key)
        self.assertEqual(capture["env"]["AE_QWEN_TOKENIZER_JSON"],
                         str(self.tokenizer_json.resolve()))
        self.assertEqual(capture["env"]["AE_QWEN_TOKENIZER_CONFIG"],
                         str(self.tokenizer_config.resolve()))
        self.assertEqual(capture["env"]["AE_QWEN_TOKENIZER_TARGET"], TARGET)
        self.assertEqual(capture["env"]["AE_QWEN_TOKENIZER_ASSET_TARGET"], TARGET)
        self.assertEqual(capture["env"]["AE_QWEN_TOKENIZER_REVISION"], "offline-fixture")
        self.assertNotIn("AE_PARENT_SECRET", capture["env"])
        self.assertNotIn("AE_QWEN_TOKENIZER_ASSET_TARGET", os.environ)
        # 4. The bootstrap's own selector: the stack declaration wins and the old
        #    multicall gate is not asked to judge the stacked tree. The child ALSO
        #    carries the receive policy the stack composes (R3-2): the receive point acts
        #    on its own declaration, so a stack launch that omitted it would run the
        #    pinned truncation. It is declared WITHOUT the multicall-only manifest, which
        #    pins different bytes for the file the capacity patch also changes.
        self.assertEqual(capture["env"]["AE_LETTA_MULTICALL_PROFILE"],
                         ae_multicall.PROFILE_VERSION)
        self.assertNotIn("AE_LETTA_MULTICALL_MANIFEST", capture["env"])
        self.assertIsNotNone(self.bootstrap._required_stack_environment(capture["env"]))
        activation = ae_multicall.verify_receive_gate(capture["env"], module=ae_multicall)
        self.assertEqual(activation["contract"], ae_multicall.RECEIVE_STACKED)
        self.assertTrue(activation["active"])
        self.assertEqual(activation["profile_version"], ae_multicall.PROFILE_VERSION)
        # ... and the launch record states it, so the activation is not inferred from a
        # variable's presence by whoever reads the deployment afterwards.
        self.assertEqual(launch["receive_policy"]["profile"], ae_multicall.PROFILE_VERSION)
        self.assertEqual(launch["receive_policy"]["declared_in_the_child_environment"],
                         "AE_LETTA_MULTICALL_PROFILE")
        self.assertFalse(launch["receive_policy"]["standalone_manifest_declared"])
        self.assertEqual(started["receive_policy_verified"]["contract"],
                         ae_multicall.RECEIVE_STACKED)
        # 5. The child's receipt validates against the manifest and the bytes.
        receipt_path = Path(capture["env"]["AE_LETTA_PATCH_STACK_RECEIPT"])
        self.assertTrue(receipt_path.is_file())
        written = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(written["checkout"], str(self.tree.resolve()))
        ae_multicall.validate_stack_receipt(
            written, record, _sha256(self.manifest))
        self.assertTrue(started["started"])
        self.assertEqual(started["service_mode"], "ae_no_compaction_stack")
        self.assertEqual(started["patch_stack_load_receipt"], str(receipt_path))
        self.assertEqual(str(capture["cwd"]), str(deploy.source))

    def test_a_tampered_stacked_tree_is_refused_by_the_real_source_gate(self):
        # A file the stack patch CHANGES: the chain check catches it (the tree no
        # longer carries the patch's declared patched bytes).
        target = self.tree / "letta/agents/letta_agent_v3.py"
        target.write_bytes(target.read_bytes() + b"\n# tampered\n")
        with self.assertRaises(RuntimeError) as raised:
            self._deployment().source_check(patch_stack=self._runtime())
        self.assertIn("does not carry the patched bytes", str(raised.exception))
        # A file the stack patch ADDS: the manifested-digest loop catches it.
        target.write_bytes(target.read_bytes()[:-len(b"\n# tampered\n")])
        helper = self.tree / "letta/helpers/ae_qwen_tokenizer.py"
        helper.write_bytes(helper.read_bytes() + b"\n# tampered\n")
        with self.assertRaises(RuntimeError) as raised:
            self._deployment().source_check(patch_stack=self._runtime())
        self.assertIn("does not match the stacked patch bytes", str(raised.exception))
        # A file the reviewed MULTICALL patch owns and this patch does not touch.
        helper.write_bytes(helper.read_bytes()[:-len(b"\n# tampered\n")])
        shim = self.tree / "letta/schemas/message.py"
        shim.write_bytes(shim.read_bytes() + b"\n# tampered\n")
        with self.assertRaises(RuntimeError) as raised:
            self._deployment().source_check(patch_stack=self._runtime())
        self.assertIn("redefines a file the reviewed multicall patch owns",
                      str(raised.exception))

    def test_a_stacked_tree_missing_a_declared_helper_is_refused(self):
        (self.tree / "letta/helpers/ae_no_compaction.py").unlink()
        with self.assertRaises(RuntimeError) as raised:
            self._deployment().source_check(patch_stack=self._runtime())
        self.assertIn("absent from the service source", str(raised.exception))

    def test_a_missing_counting_declaration_is_refused(self):
        for overrides, expected in (
                ({"tokenizer_json": None}, "--tokenizer-json"),
                ({"tokenizer_config": None}, "--tokenizer-config"),
                ({"tokenizer_target": None}, "--tokenizer-target"),
                ({"tokenizer_asset_target": None}, "--tokenizer-asset-target"),
                ({"tokenizer_json": Path(self.tmp.name) / "absent.json"}, "readable file")):
            runtime = self._runtime(**overrides)
            with self.assertRaises(RuntimeError) as raised:
                self._deployment().source_check(patch_stack=runtime)
            self.assertIn(expected, str(raised.exception), overrides)
            with self.assertRaises(RuntimeError):
                runtime.env(Path(self.tmp.name))

    def test_a_non_target_asset_is_a_formal_refusal_unless_marked_exploration(self):
        formal = self._runtime(tokenizer_asset_target="Qwen/Qwen3-8B")
        with self.assertRaises(RuntimeError) as raised:
            self._deployment().source_check(patch_stack=formal)
        self.assertIn("not the target model's own tokenizer", str(raised.exception))
        explicit = self._runtime(tokenizer_asset_target="Qwen/Qwen3-8B",
                                 allow_exploration=True)
        capture = {}
        deploy, started = self._start(explicit, capture=capture)
        self.assertTrue(started["started"])
        self.assertEqual(capture["env"]["AE_QWEN_TOKENIZER_ASSET_TARGET"], "Qwen/Qwen3-8B")
        self.assertEqual(capture["env"]["AE_QWEN_TOKENIZER_TARGET"], TARGET)
        # The run record says what it is: an EXPLORATION estimate, not the target
        # model's own count. Nothing here claims the target assets were obtained.
        launch = json.loads((deploy.record / "launch.json").read_text(encoding="utf-8"))
        self.assertTrue(launch["tokenizer"]["exploration_only"])
        self.assertFalse(launch["tokenizer"]["asset_is_target"])
        source = json.loads((deploy.record / "source.json").read_text(encoding="utf-8"))
        self.assertTrue(source["patch_stack"]["tokenizer"]["exploration_only"])

    def test_the_old_paths_keep_their_own_constraints(self):
        # (a) No opt-in at all: the tree carries the pinned HEAD but its tracked files
        #     are dirty, and without an opt-in that alone refuses.
        with self.assertRaises(RuntimeError) as raised:
            self._deployment().source_check()
        self.assertIn("fixed vendor git commit differs or tracked files are dirty",
                      str(raised.exception))
        # (b) The multicall opt-in must NOT accept the stacked tree.
        multicall = self.local.MulticallRuntime(
            manifest=MULTICALL_MANIFEST, support=ROOT,
            receipt=Path(self.tmp.name) / "multicall-load.json",
            profile=ae_multicall.PROFILE_VERSION)
        with self.assertRaises(RuntimeError) as raised:
            self._deployment().source_check(multicall=multicall)
        self.assertIn("another checkout", str(raised.exception))
        # (c) Declaring both runtimes is refused instead of silently picking one.
        with self.assertRaises(RuntimeError) as raised:
            self._deployment().source_check(multicall=multicall, patch_stack=self._runtime())
        self.assertIn("not both", str(raised.exception))

    def test_a_non_pinned_head_is_refused(self):
        """A REAL extra commit moves HEAD off the pinned identity, offline."""
        self._git("-c", "user.email=ae@local", "-c", "user.name=ae",
                  "commit", "--allow-empty", "-q", "-m", "offline: move HEAD")
        moved = self._git("rev-parse", "HEAD").strip()
        self.assertNotEqual(moved, self.local.COMMIT)
        try:
            with self.assertRaises(RuntimeError) as raised:
                self._deployment().source_check(patch_stack=self._runtime())
            self.assertIn("git HEAD is not the pinned commit", str(raised.exception))
            self.assertIn(moved, str(raised.exception))
        finally:
            # Restore the ref (the empty commit changed no bytes) so the other cases
            # keep running against the pinned identity.
            self._git("update-ref", "HEAD", self.local.COMMIT)

    def test_a_tracked_change_outside_the_stack_chain_is_refused(self):
        """The reported counterexample: a real service file nobody manifested."""
        target = self.tree / "letta/server/rest_api/routers/v1/agents.py"
        self.assertTrue(target.is_file())
        target.write_bytes(target.read_bytes() + b"\n# offline review: unmanifested change\n")
        with self.assertRaises(RuntimeError) as raised:
            self._deployment().source_check(patch_stack=self._runtime())
        self.assertIn("tracked modification outside the stacked patch chain",
                      str(raised.exception))

    def test_the_other_tracked_change_rules_still_apply_to_the_stack(self):
        """Staged, deleted, renamed and hidden changes are refused as before.

        Each mutation is applied to the tree the deployment really checks and undone
        afterwards, so every case is judged by the same real gate.
        """
        unrelated = "letta/server/rest_api/routers/v1/agents.py"
        unrelated_path = self.tree / unrelated
        cases = [
            ("staged change",
             lambda: self._git("add", "letta/schemas/message.py"),
             lambda: self._git("reset", "-q", "--", "letta/schemas/message.py"),
             "staged change"),
            ("deleted tracked path",
             lambda: unrelated_path.unlink(),
             lambda: self._git("checkout", "--", unrelated),
             "deleted tracked path"),
            # A rename of a file NO manifest covers, so the git-level rule is what
            # refuses it (renaming a manifested file would already fail the digests).
            ("renamed tracked path",
             lambda: self._git("mv", unrelated, "letta/helpers/renamed.py"),
             lambda: self._git("mv", "letta/helpers/renamed.py", unrelated),
             "renamed or copied"),
            ("hidden change (skip-worktree)",
             lambda: self._git("update-index", "--skip-worktree",
                               "letta/agents/letta_agent_v3.py"),
             lambda: self._git("update-index", "--no-skip-worktree",
                               "letta/agents/letta_agent_v3.py"),
             "not present as tracked changes"),
        ]
        for label, mutate, restore, expected in cases:
            mutate()
            try:
                with self.assertRaises(RuntimeError) as raised:
                    self._deployment().source_check(patch_stack=self._runtime())
                self.assertIn(expected, str(raised.exception), label)
            finally:
                restore()
        # ... and after every restore, the real gate accepts the tree again.
        self._deployment().source_check(patch_stack=self._runtime())

    def test_a_sealed_tree_without_git_records_the_narrower_boundary(self):
        """No `.git` keeps the digest-only boundary, and SAYS so in the record."""
        shutil.rmtree(self.tree / ".git")
        capture = {}
        deploy, started = self._start(self._runtime(), capture=capture)
        self.assertTrue(started["started"])
        source = json.loads((deploy.record / "source.json").read_text(encoding="utf-8"))
        self.assertIsNone(source["git_head"])
        self.assertEqual(source["source_identity"],
                         "no_git_tree_selective_hashes_not_full_source_identity")
        self.assertEqual(source["patch_stack"]["tracked_changes_accepted"], [])

    def test_the_wrapper_requirement_still_applies_to_the_stack(self):
        """The gate lives in the service process; a direct launch is refused."""
        from unittest.mock import patch
        deploy = self._deployment()
        started = []
        real_popen = self.local.subprocess.Popen

        def spy(argv, *args, **kwargs):
            if kwargs.get("start_new_session"):
                started.append(list(argv))
                return _FakeProcess()
            return real_popen(argv, *args, **kwargs)   # the launcher's own git calls

        with patch.object(self.local.subprocess, "Popen", side_effect=spy):
            with self.assertRaises(RuntimeError) as raised:
                deploy.start("http://127.0.0.1:8000/v1", _free_port(),
                             bounded_nltk_startup=False, explicit_llm_config=True,
                             patch_stack=self._runtime())
        self.assertIn("bootstrap", str(raised.exception))
        self.assertEqual(started, [], "the service process must not be started")


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
