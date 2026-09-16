"""The NativeVita preflight for the DECLARED DeepSeek transport.

`ae_vita.NativeVita` used to accept only `local-vllm` and the sealed compatibility
profile, so the on-site 0.4 CLI PREFLIGHT stopped at `unknown explicit transport
profile`. These tests pin the fixed contract:

* the transport contract (model, model set, required request fields) is read from the
  DECLARED profile in the proxy table - never restated, never disguised as Qwen;
* the run-local auxiliary model config is built by ONE builder that the loader itself
  checks, so the file a CLI writes is exactly the file NativeVita accepts, and a
  model-specific transport's mode travels with every native call;
* the historical paths stay byte-identical;
* the REAL CLI `--stage preflight` passes offline for the DeepSeek candidate, in a
  fresh process, with no network and no model call.

The CLI case runs as a subprocess on purpose: importing the pinned Vita package into
this process would break suites that require a fresh interpreter.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CANDIDATE = ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-candidate.json"
Qwen_CANDIDATE = (ROOT / "configs"
                  / "ae-01__re-multiturn__siliconflow.capacity-250k-measured-candidate.json")
DEEPSEEK_PROFILE = "deepseek-official-re-transport-v1"
MODE = {"thinking": {"type": "disabled"}}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vita_source():
    source = Path(os.environ.get("AE_VITA_SOURCE", ROOT / ".ae-verify-src/source"))
    return source if (source / "src/vita").is_dir() else None


def _dataset():
    dataset = Path(os.environ.get(
        "AE_VITA_DATASET", ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
    return dataset if dataset.is_file() else None


class DeclaredTransportContractTests(unittest.TestCase):
    """The profile table is the single source of the transport contract."""

    @classmethod
    def setUpClass(cls):
        cls.vita = _load("ae_vita_for_deepseek_preflight", ROOT / "ae_vita.py")

    def _config(self):
        record = json.loads(CANDIDATE.read_text(encoding="utf-8"))
        record["letta_origin"] = "http://127.0.0.1:8283"
        record["model_origin"] = "http://127.0.0.1:8000"
        return record

    def test_the_deepseek_profile_is_accepted_and_read_not_restated(self):
        import ae_cloud_proxy as px
        profile = self.vita.declared_transport(DEEPSEEK_PROFILE)
        self.assertIs(profile, px.PROFILES[DEEPSEEK_PROFILE])
        self.assertEqual(profile.declared_models(), ("deepseek-flash",))
        self.assertEqual(profile.required_request_fields, MODE)
        # the local path is not a cloud profile at all, and an unknown value is refused
        self.assertIsNone(self.vita.declared_transport("local-vllm"))
        with self.assertRaises(self.vita.NativeVitaError):
            self.vita.declared_transport("deepseek-official-re-transport-v2")

    def test_the_run_local_model_config_carries_the_declared_mode(self):
        built = self.vita.native_model_config(self._config())
        self.assertEqual(built, {"default": {}, "models": [{
            "name": "deepseek-flash", "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "EMPTY", "extra_body": MODE}]})
        # ... and the entry the loader checks is built by the SAME function
        self.assertEqual(built["models"][0], self.vita.native_model_config_entry(
            model="deepseek-flash", model_base="http://127.0.0.1:8000/v1",
            required_request_fields=MODE))

    def test_the_historical_model_config_is_byte_identical(self):
        """No `extra_body` is invented for a transport that declares no required field."""
        sealed = json.loads(Qwen_CANDIDATE.read_text(encoding="utf-8"))
        built = self.vita.native_model_config(sealed)
        self.assertEqual(built, {"default": {}, "models": [{
            "name": sealed["expected_model"],
            "base_url": sealed["model_origin"] + "/v1", "api_key": "EMPTY"}]})
        local = {"expected_model": "Qwen3-8B", "model_origin": "http://127.0.0.1:1"}
        self.assertEqual(self.vita.native_model_config(local),
                         {"default": {}, "models": [{
                             "name": "Qwen3-8B", "base_url": "http://127.0.0.1:1/v1",
                             "api_key": "EMPTY"}]})

    def test_a_model_config_without_the_required_mode_is_refused(self):
        """The refusal happens BEFORE the pinned package is imported."""
        source = _vita_source()
        if source is None:
            self.skipTest("the pinned Vita source is absent")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vita-models.json"
            # exactly what earlier rounds wrote: no declared request fields
            path.write_text(json.dumps({"default": {}, "models": [{
                "name": "deepseek-flash", "base_url": "http://127.0.0.1:8000/v1",
                "api_key": "EMPTY"}]}), encoding="utf-8")
            with self.assertRaises(self.vita.NativeVitaError) as caught:
                self.vita._load_native(source, path, "http://127.0.0.1:8000/v1",
                                       "deepseek-flash", required_request_fields=MODE)
            self.assertIn("declared request fields", str(caught.exception))
            self.assertNotIn("vita", sys.modules,
                             "the config must be refused before the package is imported")

    def test_a_model_from_another_transport_is_refused(self):
        """A Qwen label must not ride a DeepSeek declaration, or the other way round."""
        with self.assertRaises(self.vita.NativeVitaError):
            self.vita.native_model_choice(DEEPSEEK_PROFILE,
                                          "Qwen/Qwen3-30B-A3B-Instruct-2507")
        with self.assertRaises(self.vita.NativeVitaError):
            self.vita.native_model_choice(
                "siliconflow-letta-text-transport-v2-prototype", "deepseek-flash")
        with self.assertRaises(self.vita.NativeVitaError):
            self.vita.native_model_choice("local-vllm", "deepseek-flash")
        # the two paths that must keep working
        self.assertEqual(self.vita.native_model_choice("local-vllm", "Qwen3-8B"),
                         self.vita.NATIVE_MODELS)
        self.assertEqual(
            self.vita.native_model_choice(
                "siliconflow-letta-text-transport-v2-prototype",
                "Qwen/Qwen3-30B-A3B-Instruct-2507"),
            ("Qwen/Qwen3-30B-A3B-Instruct-2507",))
        self.assertEqual(self.vita.native_model_choice(DEEPSEEK_PROFILE, "deepseek-flash"),
                         ("deepseek-flash",))


class RealCliPreflightTests(unittest.TestCase):
    """The acceptance itself: the REAL CLI, offline, in a fresh process."""

    def test_the_deepseek_candidate_passes_preflight(self):
        source, dataset = _vita_source(), _dataset()
        if source is None or dataset is None:
            self.skipTest("the pinned Vita source or the fixed dataset is absent")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "preflight"
            environment = dict(os.environ)
            environment["AE_VITA_SOURCE"] = str(source)
            environment["AE_VITA_DATASET"] = str(dataset)
            environment.pop("VITA_MODEL_CONFIG_PATH", None)
            completed = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/ae_01_cloud_re_multiturn.py"),
                 "--stage", "preflight", "--config", str(CANDIDATE),
                 "--dataset", str(dataset), "--vita-source", str(source),
                 "--output-dir", str(out), "--exploratory-capacity-option"],
                cwd=str(ROOT), env=environment, capture_output=True, text=True, timeout=900)
            self.assertEqual(completed.returncode, 0,
                             completed.stdout[-2000:] + completed.stderr[-2000:])
            self.assertIn("PREFLIGHT_PASS", completed.stdout)
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "PREFLIGHT_PASS")
            self.assertFalse(result["network_called"])
            self.assertFalse(result["model_called"])
            self.assertEqual(sorted(result["arm_previews"]), ["erratum", "rewrite"])
            for arm, previews in result["arm_previews"].items():
                self.assertEqual(len(previews), 9, arm)
                self.assertFalse(any(item["model_called"] or item["tools_executed"]
                                     for item in previews.values()))
            self.assertTrue(result["pair_preflight"]["identical_initial_block"])
            # the run-local auxiliary config is the declared contract, not a Qwen name
            config = json.loads((out / "vita-models.json").read_text(encoding="utf-8"))
            self.assertEqual(config["models"][0], {
                "name": "deepseek-flash", "base_url": "http://127.0.0.1:8000/v1",
                "api_key": "EMPTY", "extra_body": MODE})


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
