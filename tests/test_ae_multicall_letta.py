"""Offline proof that the real patched Letta source behaves as declared.

The pinned Letta package cannot be imported in `.venv-vita` (no sqlalchemy,
no cryptography, no letta installed), so - exactly like
`tests/test_ae_cloud_framing.py` - the exact function bodies are pulled out of
the real files with `ast.get_source_segment` and compiled unchanged, with only
their module-level names stubbed. No body is edited and no expectation is
hand-written in place of the real code.

The default pinned source is `/tmp/ae-letta-QaM3ld/letta-v1`, overridable with
`AE_LETTA_SOURCE`. The patched source is the scratch checkout produced by
`deployment-assets/letta-multicall/build_patch.py`, default
`/tmp/ae-multicall-patch/letta-v1`, overridable with
`AE_LETTA_PATCHED_SOURCE`.

Both the UNPATCHED and the PATCHED truncation block are executed, and the
patched serializer is compared against the unpatched one, so "the patch changed
exactly this, and nothing when the profile is off" is measured, not claimed.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import textwrap
from unittest.mock import patch
import unittest

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from ae_multicall import (LETTA_ENV_VAR, PROFILE_VERSION,  # noqa: E402
                          UPSTREAM_TOOL_CALL_ID_MAX_LEN)

# The historical pinned checkout, overridable with the project-wide
# `AE_LETTA_SOURCE` convention. An explicitly declared source must exist: a run
# must never silently fall back to a stale machine-specific default.
LETTA_SOURCE_OVERRIDE = os.environ.get("AE_LETTA_SOURCE")
# An explicit declaration wins; otherwise the checked-in extraction of the pinned
# archive is used, and the historical /tmp path only when that extraction is absent.
_HISTORICAL = Path("/tmp/ae-letta-QaM3ld/letta-v1")
_CHECKED_IN = PROJECT / ".ae-verify-src/letta-v1"
BASELINE_SOURCE = (Path(LETTA_SOURCE_OVERRIDE) if LETTA_SOURCE_OVERRIDE
                   else (_CHECKED_IN if _CHECKED_IN.is_dir() else _HISTORICAL))
_PATCHED_HISTORICAL = Path("/tmp/ae-multicall-patch/letta-v1")
_PATCHED_CHECKED_IN = PROJECT / ".ae-verify-src/letta-multicall-patch/letta-v1"
PATCHED_SOURCE = Path(os.environ.get("AE_LETTA_PATCHED_SOURCE") or (
    _PATCHED_CHECKED_IN if _PATCHED_CHECKED_IN.is_dir() else _PATCHED_HISTORICAL))


def pinned_baseline():
    """The pinned baseline to hand the production generator explicitly.

    A declared `AE_LETTA_SOURCE` that is absent is a hard failure, not a skip:
    otherwise a lab run would quietly build its manifest against a Mac default
    that does not exist there.
    """
    if LETTA_SOURCE_OVERRIDE and not BASELINE_SOURCE.is_dir():
        raise RuntimeError(
            "AE_LETTA_SOURCE was set to " + LETTA_SOURCE_OVERRIDE
            + " but the pinned Letta baseline is missing")
    return BASELINE_SOURCE
LETTA_COMMIT = "56ba9c25552605eec89de8ed3dc6394b625c1993"

BASELINE_SHAS = {
    "letta/agents/letta_agent_v3.py":
        "1f11745d6ae86e64e90c76e287d25552a6c7823a8109b178da6951b21c788166",
    "letta/schemas/message.py":
        "c50de8d2792645a51f34f8f264c85bcea8ad252527270e96ebbe23bdd8a8e7e6",
}
R2_IDS = ("01a094c62aacd8ab46b11b69c6e81881", "01a094c62aacd8ab46b11b69c6e81882",
          "01a094c62aacd8ab46b11b69c6e81883", "01a094c62aacd8ab46b11b69c6e81884")
TRUNCATED = "01a094c62aacd8ab46b11b69c6e81"
SHIM_RELATIVE = "letta/helpers/ae_multicall_compat.py"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class QualifiedExtractor:
    """Extract `def`s by their DOTTED owner path from one source file."""

    def __init__(self, path):
        self.path = Path(path)
        self.text = self.path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.text)
        self.bodies = {}
        self._walk(self.tree, ())

    def _walk(self, node, path):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._record(child, path + (child.name,))
            elif isinstance(child, ast.ClassDef):
                self._walk(child, path + (child.name,))
            else:
                self._walk(child, path)

    def _record(self, node, dotted):
        lines = ast.get_source_segment(self.text, node).splitlines()
        while lines and lines[0].lstrip().startswith("@"):
            lines.pop(0)
        self.bodies[".".join(dotted)] = "\n".join(lines)

    def get(self, dotted):
        if dotted not in self.bodies:
            raise AssertionError(f"{dotted} not found in {self.path}")
        return self.bodies[dotted]


def compiled(namespace, source, name="fn"):
    exec(compile(source, "<extracted>", "exec"), namespace)
    return namespace[name]


def real_sanitize_tool_call_id():
    """The pinned `letta.utils.sanitize_tool_call_id`, extracted unchanged.

    The shim falls back to it when the profile is off, so the fallback itself is
    the real upstream function rather than a hand-written equivalent.
    """
    body = QualifiedExtractor(BASELINE_SOURCE / "letta/utils.py").get("sanitize_tool_call_id")
    namespace = {"re": re, "TOOL_CALL_ID_MAX_LEN": UPSTREAM_TOOL_CALL_ID_MAX_LEN,
                 "TOOL_CALL_ID_PATTERN": re.compile(r"^[a-zA-Z0-9_-]+$")}
    return compiled(namespace, body, "sanitize_tool_call_id")


def write_production_manifest(directory, checkout, *, live=False):
    """Call the PRODUCTION manifest generator, not a test-only schema.

    Nothing in this test writes a manifest field itself, so a field production
    omits or spells differently cannot be supplied here.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ae_write_manifest", PROJECT / "deployment-assets/letta-multicall/write_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = Path(directory) / "manifest.json"
    module.build_manifest(patched=Path(checkout), project=PROJECT,
                          pinned=pinned_baseline(),
                          letta_checkout=Path(checkout), out=path,
                          applied_to_live_server=live)
    return path


def multicall_environment(directory, checkout):
    """The real runtime environment a verified 0.2 service would have.

    Returns the env dict for `verify_letta_gate(environ)`: the profile, the
    reviewed manifest the production generator wrote over the real patched
    checkout, and the reviewed compatibility-module digest.
    """
    import ae_multicall
    manifest = write_production_manifest(directory, checkout)
    return {
        LETTA_ENV_VAR: PROFILE_VERSION,
        "AE_LETTA_MULTICALL_MANIFEST": str(manifest),
        "AE_LETTA_MULTICALL_MODULE_SHA256": ae_multicall.live_module_sha(),
        "manifest": manifest,
    }


def shim_namespace(module):
    """Namespace for the real shim's profile helpers (no manifest needed).

    `preserve_tool_call_id` and `sanitize_tool_call_id_compat` only read whether
    a profile is declared; the strict manifest gate is exercised at the receive
    point, not at serialization.
    """
    shim = QualifiedExtractor(PATCHED_SOURCE / SHIM_RELATIVE)
    namespace = {"os": os, "re": re, "sys": sys,
                 "AE_MULTICALL_ENV_VAR": LETTA_ENV_VAR,
                 "AE_MULTICALL_PROFILE": PROFILE_VERSION,
                 "AE_MULTICALL_MANIFEST_ENV_VAR": "AE_LETTA_MULTICALL_MANIFEST",
                 "AE_MULTICALL_MODULE_SHA_ENV_VAR": "AE_LETTA_MULTICALL_MODULE_SHA256",
                 "AE_MULTICALL_MODULE": "ae_multicall",
                 "MulticallRefused": module.MulticallRefused,
                 "_TOOL_CALL_ID_PATTERN": re.compile(r"^[a-zA-Z0-9_-]+$")}
    for name in ("declared_profile", "_import_version_support", "multicall_decision",
                 "ae_multicall_enabled", "keep_all_client_tool_calls",
                 "preserve_tool_call_id", "sanitize_tool_call_id_compat"):
        compiled(namespace, shim.get(name), name)
    return namespace


def install_letta_stubs():
    """A minimal `letta` package so extracted bodies' internal imports resolve."""
    if "letta" in sys.modules and "letta.log" in sys.modules:
        return
    letta = type(sys)("letta")
    letta.__path__ = []  # a package, so `letta.log`/`letta.utils` can resolve
    utils = type(sys)("letta.utils")
    utils.sanitize_tool_call_id = real_sanitize_tool_call_id()
    letta.utils = utils
    log = type(sys)("letta.log")
    log.get_logger = lambda name=None: StubLogger()
    letta.log = log
    sys.modules["letta"] = letta
    sys.modules["letta.utils"] = utils
    sys.modules["letta.log"] = log


def shim_helpers(extra=None):
    """The real helper bodies from the added compat shim, compiled offline."""
    install_letta_stubs()
    extractor = QualifiedExtractor(PATCHED_SOURCE / SHIM_RELATIVE)
    namespace = {"os": os, "re": re, "AE_MULTICALL_ENV_VAR": LETTA_ENV_VAR,
                 "AE_MULTICALL_PROFILE": PROFILE_VERSION,
                 "_TOOL_CALL_ID_PATTERN": re.compile(r"^[a-zA-Z0-9_-]+$")}
    namespace.update(extra or {})
    for name in ("ae_multicall_enabled", "keep_all_client_tool_calls",
                     "preserve_tool_call_id", "sanitize_tool_call_id_compat"):
        compiled(namespace, extractor.get(name), name)
    return namespace


class FakeCompatModule:
    """Stands in for `letta.helpers.ae_multicall_compat` during extraction."""

    @staticmethod
    def keep_all_client_tool_calls(calls, declared):
        return _ACTIVE_KEEP["fn"](calls, declared)


_ACTIVE_KEEP = {"fn": None}


class StubLogger:
    """Captures the real code's log calls; `error` is used by the dedupe pass."""

    def __init__(self):
        self.info_calls, self.warning_calls, self.error_calls = [], [], []

    def info(self, message, *args, **kwargs):
        self.info_calls.append(message)

    def warning(self, message, *args, **kwargs):
        self.warning_calls.append(message)

    def error(self, message, *args, **kwargs):
        self.error_calls.append(message)


class StubFunction:
    def __init__(self, name):
        self.name = name


class StubCall:
    """Stands in for `openai` `ChatCompletionMessageToolCall`.

    `model_dump` mirrors the real pydantic object's OpenAI shape, which is what
    `to_openai_dict` serializes before it touches the id.
    """

    def __init__(self, name, call_id=None, arguments="{}"):
        self.function = StubFunction(name)
        self.function.arguments = arguments
        self.id = call_id

    def model_dump(self):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name,
                             "arguments": self.function.arguments}}


class StubDeclaredTool:
    def __init__(self, name):
        self.name = name


class StubAgent:
    def __init__(self, client_tools, logger=None):
        self.client_tools = [StubDeclaredTool(name) for name in client_tools]
        self.logger = logger or StubLogger()


class StubMessage:
    """Minimal `Message` stand-in for the list serializer; no pydantic needed."""

    def __init__(self, **attributes):
        defaults = {"role": "assistant", "content": None, "name": None,
                    "tool_calls": None, "tool_returns": None, "tool_call_id": None,
                    "id": "message-fixture"}
        defaults.update(attributes)
        self.__dict__.update(defaults)

    @staticmethod
    def filter_messages_for_llm_api(messages):
        return list(messages)


class StubTextContent:
    def __init__(self, text=""):
        self.text = text


class StubToolReturn:
    def __init__(self, tool_call_id, func_response):
        self.tool_call_id = tool_call_id
        self.func_response = func_response
        self.status = "success"


class StubNothing:
    pass


def baseline_and_patched(relative):
    return (QualifiedExtractor(BASELINE_SOURCE / relative),
            QualifiedExtractor(PATCHED_SOURCE / relative))


class SourceIdentityTests(unittest.TestCase):
    def test_pinned_baseline_files_match_the_declared_shas(self):
        for relative, want in BASELINE_SHAS.items():
            with self.subTest(file=relative):
                self.assertTrue((BASELINE_SOURCE / relative).is_file(),
                                f"pinned Letta source missing: {BASELINE_SOURCE}")
                self.assertEqual(sha256(BASELINE_SOURCE / relative), want)

    def test_patched_checkout_is_present_and_differs_from_baseline(self):
        for relative in BASELINE_SHAS:
            with self.subTest(file=relative):
                self.assertTrue((PATCHED_SOURCE / relative).is_file(),
                                f"patched checkout missing: {PATCHED_SOURCE}")
                self.assertNotEqual(sha256(PATCHED_SOURCE / relative),
                                    BASELINE_SHAS[relative])
        self.assertTrue((PATCHED_SOURCE / SHIM_RELATIVE).is_file())


class StepTruncationTests(unittest.TestCase):
    """The real `_step` receive block, driven with the real shim decision.

    The extraction executes the pinned lines, including the real import of
    `multicall_decision`, so the fail-closed behaviour is measured rather than
    stubbed. The compatibility support module is the real project module and the
    environment is a real production-generated manifest over the real patched
    checkout.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = multicall_environment(self.tmp.name, PATCHED_SOURCE)
        self.module = __import__("ae_multicall")
        # The extracted block imports `letta.helpers.ae_multicall_compat`; the
        # fake module executes the REAL shim source so the decision is real.
        shim = QualifiedExtractor(PATCHED_SOURCE / SHIM_RELATIVE)
        namespace = {"os": os, "re": re, "sys": sys,
                     "AE_MULTICALL_ENV_VAR": LETTA_ENV_VAR,
                     "AE_MULTICALL_PROFILE": PROFILE_VERSION,
                     "AE_MULTICALL_MANIFEST_ENV_VAR": "AE_LETTA_MULTICALL_MANIFEST",
                     "AE_MULTICALL_MODULE_SHA_ENV_VAR": "AE_LETTA_MULTICALL_MODULE_SHA256",
                     "AE_MULTICALL_MODULE": "ae_multicall",
                     "MulticallRefused": self.module.MulticallRefused,
                     "_TOOL_CALL_ID_PATTERN": re.compile(r"^[a-zA-Z0-9_-]+$")}
        for name in ("declared_profile", "_import_version_support", "multicall_decision",
                     "ae_multicall_enabled", "keep_all_client_tool_calls",
                     "preserve_tool_call_id", "sanitize_tool_call_id_compat"):
            compiled(namespace, shim.get(name), name)
        namespace["KEEP_ALL"] = "keep_all_client_tool_calls"
        namespace["NOT_DECLARED"] = "not_declared"
        self.shim = type(sys)("letta.helpers.ae_multicall_compat")
        for key, value in namespace.items():
            setattr(self.shim, key, value)
        self.addCleanup(lambda: os.environ.pop(LETTA_ENV_VAR, None))
        self.addCleanup(lambda: os.environ.pop("AE_LETTA_MULTICALL_MANIFEST", None))
        self.addCleanup(lambda: os.environ.pop("AE_LETTA_MULTICALL_MODULE_SHA256", None))

    def verified_environment(self):
        os.environ[LETTA_ENV_VAR] = PROFILE_VERSION
        os.environ["AE_LETTA_MULTICALL_MANIFEST"] = str(self.env["manifest"])
        os.environ["AE_LETTA_MULTICALL_MODULE_SHA256"] = self.env[
            "AE_LETTA_MULTICALL_MODULE_SHA256"]

    def step_block(self, source_kind, tool_names, client_tools, parallel=False):
        relative = "letta/agents/letta_agent_v3.py"
        source, patched = baseline_and_patched(relative)
        body = (patched if source_kind == "patched" else source).get("LettaAgentV3._step")
        lines = body.splitlines()
        start = next(index for index, line in enumerate(lines)
                     if "parallel_tool_calls=false by truncating" in line)
        end = next(index for index in range(start, len(lines))
                   if lines[index].strip() == "tool_calls = [tool_calls[0]]")
        block = textwrap.dedent("\n".join(lines[start:end + 1]))
        sys.modules["letta.helpers.ae_multicall_compat"] = self.shim
        namespace = {"active_llm_config":
                     type("Config", (), {"parallel_tool_calls": parallel})()}
        agent = StubAgent(client_tools)
        local = {"self": agent, "tool_calls": [StubCall(name) for name in tool_names]}
        exec(compile(block, "<step-block>", "exec"), namespace, local)
        return local["tool_calls"], agent.logger

    def test_baseline_block_drops_three_of_four_declared_calls(self):
        calls, logger = self.step_block("baseline", ["memory_update"] * 4,
                                       ["memory_update"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(logger.warning_calls), 1)

    def test_verified_profile_keeps_all_declared_client_calls(self):
        self.verified_environment()
        calls, logger = self.step_block("patched", ["memory_update"] * 4,
                                        ["memory_update"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(logger.warning_calls, [])
        self.assertEqual(len(logger.info_calls), 1)

    def test_a_declared_but_unverified_profile_refuses_before_any_truncation(self):
        """The review's P1: no fallback to `[tool_calls[0]]` when declared."""
        for label in ("profile only, no manifest", "unknown profile", "tampered digest"):
            with self.subTest(label=label):
                self.verified_environment()
                if label == "profile only, no manifest":
                    os.environ.pop("AE_LETTA_MULTICALL_MANIFEST", None)
                elif label == "unknown profile":
                    os.environ[LETTA_ENV_VAR] = "ae-multicall-receive-compat-999"
                else:
                    os.environ["AE_LETTA_MULTICALL_MODULE_SHA256"] = "0" * 64
                with self.assertRaises(self.module.MulticallRefused):
                    self.step_block("patched", ["memory_update"] * 4, ["memory_update"])

    def test_a_declared_profile_with_an_undeclared_call_refuses(self):
        self.verified_environment()
        with self.assertRaises(self.module.MulticallRefused):
            self.step_block("patched", ["memory_update", "send_email"], ["memory_update"])

    def test_an_unset_profile_keeps_the_upstream_truncation(self):
        os.environ.pop(LETTA_ENV_VAR, None)
        calls, logger = self.step_block("patched", ["memory_update"] * 4,
                                        ["memory_update"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(logger.warning_calls), 1)

    def test_parallel_true_never_enters_the_branch(self):
        os.environ.pop(LETTA_ENV_VAR, None)
        calls, logger = self.step_block("patched", ["memory_update"] * 4,
                                        ["memory_update"], parallel=True)
        self.assertEqual(len(calls), 4)
        self.assertEqual(logger.warning_calls, [])


class ShimHelperTests(unittest.TestCase):
    """The real shim, evaluated against the real project gate and manifest."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = multicall_environment(self.tmp.name, PATCHED_SOURCE)
        self.module = __import__("ae_multicall")
        self.addCleanup(lambda: os.environ.pop(LETTA_ENV_VAR, None))
        self.addCleanup(lambda: os.environ.pop("AE_LETTA_MULTICALL_MANIFEST", None))
        self.addCleanup(lambda: os.environ.pop("AE_LETTA_MULTICALL_MODULE_SHA256", None))

    def helpers(self, profile=PROFILE_VERSION):
        """Compile the real shim with a real profile/manifest environment."""
        install_letta_stubs()  # the shim's non-profile fallback imports letta.utils
        if profile is None:
            os.environ.pop(LETTA_ENV_VAR, None)
        else:
            os.environ[LETTA_ENV_VAR] = profile
        os.environ["AE_LETTA_MULTICALL_MANIFEST"] = str(self.env["manifest"])
        os.environ["AE_LETTA_MULTICALL_MODULE_SHA256"] = self.env[
            "AE_LETTA_MULTICALL_MODULE_SHA256"]
        shim = QualifiedExtractor(PATCHED_SOURCE / SHIM_RELATIVE)
        namespace = {"os": os, "re": re, "sys": sys,
                     "AE_MULTICALL_ENV_VAR": LETTA_ENV_VAR,
                     "AE_MULTICALL_PROFILE": PROFILE_VERSION,
                     "AE_MULTICALL_MANIFEST_ENV_VAR": "AE_LETTA_MULTICALL_MANIFEST",
                     "AE_MULTICALL_MODULE_SHA_ENV_VAR": "AE_LETTA_MULTICALL_MODULE_SHA256",
                     "AE_MULTICALL_MODULE": "ae_multicall",
                     "MulticallRefused": self.module.MulticallRefused,
                     "_TOOL_CALL_ID_PATTERN": re.compile(r"^[a-zA-Z0-9_-]+$")}
        for name in ("declared_profile", "_import_version_support", "multicall_decision",
                     "ae_multicall_enabled", "keep_all_client_tool_calls",
                     "preserve_tool_call_id", "sanitize_tool_call_id_compat"):
            compiled(namespace, shim.get(name), name)
        namespace["KEEP_ALL"] = namespace["multicall_decision"](
            [], [], self.module) if False else None
        namespace["KEEP_ALL"] = "keep_all_client_tool_calls"
        namespace["NOT_DECLARED"] = "not_declared"
        return namespace

    def test_enabling_requires_the_full_verified_environment(self):
        ns = self.helpers()
        decision = ns["multicall_decision"]
        declared = [StubDeclaredTool("memory_update")]
        four = [StubCall("memory_update") for _ in range(4)]
        self.assertEqual(decision(four, declared, self.module), "keep_all_client_tool_calls")

    def test_a_missing_or_tampered_manifest_refuses_instead_of_truncating(self):
        declared = [StubDeclaredTool("memory_update")]
        four = [StubCall("memory_update") for _ in range(4)]
        cases = {
            "no manifest": lambda: os.environ.pop("AE_LETTA_MULTICALL_MANIFEST", None),
            "no module digest": lambda: os.environ.pop("AE_LETTA_MULTICALL_MODULE_SHA256", None),
            "wrong module digest": lambda: os.environ.__setitem__(
                "AE_LETTA_MULTICALL_MODULE_SHA256", "0" * 64),
            "manifest path missing": lambda: os.environ.__setitem__(
                "AE_LETTA_MULTICALL_MANIFEST", str(Path(self.tmp.name) / "absent.json")),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                ns = self.helpers()
                mutate()
                with self.assertRaises(self.module.MulticallRefused):
                    ns["multicall_decision"](four, declared, self.module)

    def test_an_empty_or_unknown_manifest_is_refused(self):
        manifest = json.loads(self.env["manifest"].read_text(encoding="utf-8"))
        for label, mutate in (
            ("empty patched_files", lambda m: m.__setitem__("patched_files", {})),
            ("unknown schema", lambda m: m.__setitem__("schema", "unknown-schema")),
            ("no letta_checkout", lambda m: m.pop("letta_checkout")),
            ("compat module fields", lambda m: m.__setitem__(
                "compat_module", {"project_path": "ae_multicall.py",
                                  "sha256": m["compat_module"]["sha256"]})),
        ):
            with self.subTest(label=label):
                broken = json.loads(json.dumps(manifest))
                mutate(broken)
                path = Path(self.tmp.name) / (label.replace(" ", "_") + ".json")
                path.write_text(json.dumps(broken), encoding="utf-8")
                ns = self.helpers()
                os.environ["AE_LETTA_MULTICALL_MANIFEST"] = str(path)
                with self.assertRaises(self.module.MulticallRefused):
                    ns["multicall_decision"]([StubCall("memory_update")] * 2,
                                             [StubDeclaredTool("memory_update")],
                                             self.module)

    def test_unknown_profile_and_undeclared_calls_refuse(self):
        ns = self.helpers(profile="ae-multicall-receive-compat-999")
        with self.assertRaises(self.module.MulticallRefused):
            ns["multicall_decision"]([StubCall("memory_update")] * 2,
                                     [StubDeclaredTool("memory_update")], self.module)
        ns = self.helpers()
        with self.assertRaises(self.module.MulticallRefused):
            ns["multicall_decision"]([StubCall("memory_update"), StubCall("send_email")],
                                     [StubDeclaredTool("memory_update")], self.module)
        with self.assertRaises(self.module.MulticallRefused):
            ns["multicall_decision"]([StubCall("memory_update")] * 2, [], self.module)

    def test_an_unset_profile_means_the_upstream_path(self):
        ns = self.helpers(profile=None)
        self.assertEqual(ns["multicall_decision"]([StubCall("memory_update")] * 4,
                                                  [StubDeclaredTool("memory_update")],
                                                  self.module), "not_declared")

    def test_preserve_and_sanitize_keep_full_ids_only_under_the_profile(self):
        ns = self.helpers()
        preserve, sanitize = ns["preserve_tool_call_id"], ns["sanitize_tool_call_id_compat"]
        for native_id in R2_IDS:
            with self.subTest(native_id=native_id, enabled=False):
                os.environ.pop(LETTA_ENV_VAR, None)
                self.assertEqual(preserve(native_id, UPSTREAM_TOOL_CALL_ID_MAX_LEN),
                                 TRUNCATED)
                self.assertEqual(sanitize(native_id), TRUNCATED)
            with self.subTest(native_id=native_id, enabled=True):
                os.environ[LETTA_ENV_VAR] = PROFILE_VERSION
                self.assertEqual(preserve(native_id, UPSTREAM_TOOL_CALL_ID_MAX_LEN),
                                 native_id)
                self.assertEqual(sanitize(native_id), native_id)
        os.environ[LETTA_ENV_VAR] = PROFILE_VERSION
        self.assertEqual(sanitize("Read:93"), "Read_93")

    def test_keep_all_client_tool_calls_matches_the_decision(self):
        ns = self.helpers()
        keep = ns["keep_all_client_tool_calls"]
        declared = [StubDeclaredTool("memory_update")]
        four = [StubCall("memory_update") for _ in range(4)]
        self.assertTrue(keep(four, declared, self.module))
        self.assertTrue(keep(four[:1], declared, self.module))


class SerializerTests(unittest.TestCase):
    """The real `Message.to_openai_dict` assistant and tool-return branches."""

    def setUp(self):
        self.module = __import__("ae_multicall")
        self.addCleanup(lambda: os.environ.pop(LETTA_ENV_VAR, None))
        os.environ.pop(LETTA_ENV_VAR, None)

    def openai_dict(self, extractor, receiver,
                    max_tool_id_length=UPSTREAM_TOOL_CALL_ID_MAX_LEN):
        body = extractor.get("Message.to_openai_dict")
        namespace = {
            "REQUEST_HEARTBEAT_PARAM": "_request_heartbeat",
            "INNER_THOUGHTS_KWARG": "inner_thoughts",
            "TextContent": StubTextContent,
            "ToolReturnContent": StubNothing,
            "ReasoningContent": StubNothing,
            "RedactedReasoningContent": StubNothing,
            "OmittedReasoningContent": StubNothing,
            "tool_return_to_text": lambda value: value,
            "truncate_tool_return": lambda text, _chars: text,
            "add_inner_thoughts_to_tool_call": lambda call, **kwargs: call,
            "parse_json": lambda value: value,
            "logger": StubLogger(),
            "re": re,
            # Needed for the signature default in BOTH variants.
            "TOOL_CALL_ID_MAX_LEN": UPSTREAM_TOOL_CALL_ID_MAX_LEN,
        }
        if "preserve_tool_call_id(" in body:
            namespace.update(shim_namespace(self.module))
        fn = compiled(namespace, body, "to_openai_dict")
        return fn(receiver, max_tool_id_length=max_tool_id_length)

    def message(self, **attributes):
        message = type("Message", (), {})()
        defaults = {"role": "assistant", "content": None, "name": None, "tool_calls": None,
                    "tool_returns": None, "tool_call_id": None, "id": "message-fixture"}
        defaults.update(attributes)
        message.__dict__.update(defaults)
        return message

    def four_calls(self):
        return [StubCall("memory_update", native_id) for native_id in R2_IDS]

    def test_baseline_serializer_collapses_the_four_real_ids(self):
        extractor, _ = baseline_and_patched("letta/schemas/message.py")
        out = self.openai_dict(extractor, self.message(tool_calls=self.four_calls()))
        self.assertEqual([call["id"] for call in out["tool_calls"]], [TRUNCATED] * 4)
        self.assertEqual(len({call["id"] for call in out["tool_calls"]}), 1)

    def test_patched_serializer_preserves_the_four_real_ids_under_the_profile(self):
        _, patched = baseline_and_patched("letta/schemas/message.py")
        os.environ[LETTA_ENV_VAR] = PROFILE_VERSION
        out = self.openai_dict(patched, self.message(tool_calls=self.four_calls()))
        self.assertEqual([call["id"] for call in out["tool_calls"]], list(R2_IDS))
        self.assertEqual(len({call["id"] for call in out["tool_calls"]}), 4)

    def test_patched_serializer_is_unchanged_when_the_profile_is_off(self):
        _, patched = baseline_and_patched("letta/schemas/message.py")
        out = self.openai_dict(patched, self.message(tool_calls=self.four_calls()))
        self.assertEqual([call["id"] for call in out["tool_calls"]], [TRUNCATED] * 4)

    def test_tool_return_id_survives_under_the_profile(self):
        _, patched = baseline_and_patched("letta/schemas/message.py")
        returned = StubToolReturn(R2_IDS[0], "ok")
        os.environ[LETTA_ENV_VAR] = PROFILE_VERSION
        out = self.openai_dict(patched, self.message(role="tool", tool_returns=[returned]))
        self.assertEqual(out["tool_call_id"], R2_IDS[0])
        os.environ.pop(LETTA_ENV_VAR, None)
        out = self.openai_dict(patched, self.message(role="tool", tool_returns=[returned]))
        self.assertEqual(out["tool_call_id"], TRUNCATED)

    def test_patched_serializer_changes_only_the_expected_lines(self):
        source, patched = baseline_and_patched("letta/schemas/message.py")
        before = source.get("Message.to_openai_dict").splitlines()
        after = patched.get("Message.to_openai_dict").splitlines()
        self.assertNotEqual(before, after)
        added = [line for line in after if line not in before]
        removed = [line for line in before if line not in after]
        self.assertTrue(any("preserve_tool_call_id" in line for line in added), added)
        self.assertEqual(len(removed), 3, removed)
        self.assertTrue(all("[:max_tool_id_length]" in line for line in removed), removed)




class ListSerializationTests(unittest.TestCase):
    """The real `Message.to_openai_dicts_from_list`, with a declared boundary.

    `to_openai_dicts_from_list` calls `Message.filter_messages_for_llm_api`,
    whose collapse/dedupe chain inlines many other helpers. Re-implementing that
    chain here would be a second, unfaithful implementation, so this test stubs
    ONLY that call to identity - the function under test still runs its own real
    body, including its own `tool_call_id` truncation site at the multi-return
    expansion. Nothing else in the function is replaced.
    """

    def setUp(self):
        self.module = __import__("ae_multicall")
        self.addCleanup(lambda: os.environ.pop(LETTA_ENV_VAR, None))
        os.environ.pop(LETTA_ENV_VAR, None)

    def serialize(self, message):
        extractor = QualifiedExtractor(PATCHED_SOURCE / "letta/schemas/message.py")
        body = extractor.get("Message.to_openai_dicts_from_list")
        call_body = extractor.get("Message.to_openai_dict")
        namespace = {
            "List": list, "Optional": object,
            "Message": StubMessage,
            "MessageRole": type("MessageRole", (), {"tool": "tool"}),
            "TOOL_CALL_ID_MAX_LEN": UPSTREAM_TOOL_CALL_ID_MAX_LEN,
            "put_inner_thoughts_in_kwargs": False,
            "TextContent": StubTextContent,
            "ToolReturnContent": StubNothing,
            "ReasoningContent": StubNothing,
            "RedactedReasoningContent": StubNothing,
            "OmittedReasoningContent": StubNothing,
            "tool_return_to_text": lambda value: value,
            "truncate_tool_return": lambda text, _chars: text,
            "add_inner_thoughts_to_tool_call": lambda call, **kwargs: call,
            "parse_json": lambda value: value,
            "logger": StubLogger(),
            "re": re,
        }
        if "preserve_tool_call_id(" in body or "preserve_tool_call_id(" in call_body:
            namespace.update(shim_namespace(self.module))
        namespace["filter_messages_for_llm_api"] = lambda messages: list(messages)
        namespace["message"] = message
        compiled(namespace, call_body, "to_openai_dict")
        StubMessage.to_openai_dict = namespace["to_openai_dict"]
        fn = compiled(namespace, body, "to_openai_dicts_from_list")
        return fn([message])

    def test_multi_return_expansion_keeps_full_ids_under_the_profile(self):
        returned = [StubToolReturn(native_id, "ok") for native_id in R2_IDS]
        message = StubMessage(role="tool", tool_returns=returned)
        os.environ[LETTA_ENV_VAR] = PROFILE_VERSION
        out = self.serialize(message)
        self.assertEqual([entry["tool_call_id"] for entry in out], list(R2_IDS))
        self.assertEqual({entry["role"] for entry in out}, {"tool"})

    def test_multi_return_expansion_truncates_without_the_profile(self):
        returned = [StubToolReturn(native_id, "ok") for native_id in R2_IDS]
        message = StubMessage(role="tool", tool_returns=returned)
        out = self.serialize(message)
        self.assertEqual([entry["tool_call_id"] for entry in out], [TRUNCATED] * 4)
        self.assertEqual(len({entry["tool_call_id"] for entry in out}), 1)


class PinnedBaselinePortabilityTests(unittest.TestCase):
    """The manifest helper must use the DECLARED pinned baseline, not a Mac default.

    The deployment probe showed both helpers falling back to
    `/tmp/ae-letta-QaM3ld/letta-v1` when `AE_LETTA_SOURCE` pointed at a real
    alternate tree, so the lab host could not build a manifest at all.
    """

    def alternate_baseline(self, root):
        """A REAL alternate pinned tree: the two files the patch touches."""
        target = Path(root) / "baseline"
        for name in ("letta/agents/letta_agent_v3.py", "letta/schemas/message.py"):
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BASELINE_SOURCE / name, destination)
        return target

    def test_the_declared_alternate_baseline_is_what_the_generator_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            alternate = self.alternate_baseline(tmp)
            self.assertTrue(alternate.is_dir())
            self.assertNotEqual(alternate.resolve(), BASELINE_SOURCE.resolve())
            out = Path(tmp) / "out"
            out.mkdir()
            with patch.object(sys.modules[__name__], "LETTA_SOURCE_OVERRIDE", str(alternate)), \
                 patch.object(sys.modules[__name__], "BASELINE_SOURCE", alternate):
                path = write_production_manifest(out, PATCHED_SOURCE)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(Path(record["baseline_checkout"]).resolve(),
                             alternate.resolve())
            self.assertTrue(record["verification"]["offline_patch_verified"])

    def test_a_declared_missing_baseline_fails_instead_of_skipping(self):
        missing = Path("/nonexistent/ae-portability-baseline")
        with patch.object(sys.modules[__name__], "LETTA_SOURCE_OVERRIDE", str(missing)), \
             patch.object(sys.modules[__name__], "BASELINE_SOURCE", missing):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "out"
                out.mkdir()
                with self.assertRaises(RuntimeError) as caught:
                    write_production_manifest(out, PATCHED_SOURCE)
        self.assertIn("pinned Letta baseline is missing", str(caught.exception))

    @staticmethod
    def resolve_without_a_declaration():
        """Re-import this module with `AE_LETTA_SOURCE` unset, as a lab host would.

        The fallback is bound at import time, so patching the module global would
        only test the patch. A fresh import of the real file exercises the real
        resolution expression with no declaration present.
        """
        import importlib.util
        declared = os.environ.pop("AE_LETTA_SOURCE", None)
        name = "test_ae_multicall_letta_nodeclared"
        try:
            spec = importlib.util.spec_from_file_location(name, Path(__file__))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
                return module.pinned_baseline()
            finally:
                sys.modules.pop(name, None)
        finally:
            if declared is not None:
                os.environ["AE_LETTA_SOURCE"] = declared

    def test_the_historical_default_still_resolves_when_nothing_is_declared(self):
        """No declaration: the checked-in extraction, else the historical /tmp path.

        A lab host that prepared `/tmp/ae-letta-QaM3ld/letta-v1` keeps working; a plain
        checkout resolves to the pinned archive extraction instead of a path that does
        not exist on that machine.
        """
        resolved = self.resolve_without_a_declaration()
        checked_in = PROJECT / ".ae-verify-src/letta-v1"
        if checked_in.is_dir():
            self.assertEqual(resolved, checked_in)
            self.assertTrue((resolved / "letta/schemas/message.py").is_file())
        else:
            self.assertEqual(resolved, Path("/tmp/ae-letta-QaM3ld/letta-v1"))


if __name__ == "__main__":
    unittest.main()
