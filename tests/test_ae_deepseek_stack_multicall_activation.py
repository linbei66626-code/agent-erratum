"""R3-2: a stacked service must ACTIVATE the receive policy it composes.

The live r4 capture is the premise. The service process (PID 11221) had
`AE_LETTA_PATCH_STACK_PROFILE=ae-no-compaction-stack-1` and no
`AE_LETTA_MULTICALL_PROFILE`, so the patched receive point took the `not_declared`
branch and the pinned upstream code truncated every multi-call provider response to
its first call: 10 batches returned 39 calls and the following history kept 10, so 29
calls (6 `memory_update`, 23 `longitude_latitude_to_distance`) never reached the
driver. The stacked checkout was loaded and verified - the POLICY was not active.
`transfers/ae-deepseek-re-complete-20260916-r4/multicall-loss-diagnostic.json`.

What this module proves, in order, with no live server and no model call:

* the REAL launcher runtime (`scripts/deployment/letta_local.PatchStackRuntime`)
  declares the composed receive policy in the child environment it builds, and the
  value comes from the support module the stack manifest pins - not from the parent
  process's environment, which the test plants a wrong value in;
* the REAL bootstrap gate (`scripts/deployment/letta_bootstrap.install_patch_stack_gate`)
  verifies it in the process that would import Letta and writes its stack receipt -
  and REFUSES TO START when the stack is declared without an activation that can be
  established (missing declaration, unknown version, mixed multicall-only manifest,
  stale module digest, absent manifest);
* the REAL patched receive block from the STACKED checkout keeps both calls of a
  two-`memory_update` batch, hands both to the client-tool branch of the REAL
  `_handle_ai_response`, and the REAL driver bridge (`ae_adapter.LettaBridge`)
  executes each call exactly once, in order, and submits one tool return per call;
* those returns go through the REAL `Message` serializers and the REAL
  `OpenAIClient.request_async` with the REAL `openai` SDK, and the captured HTTP
  request body carries both native ids WHOLE (the pinned serializers kept only their
  first 29 characters).

Two things only are substituted, both outside the logic under test: the driver's
Letta-side session transport (the final HTTP layer) and the tool side effects / DB.
Every decision under test - the multicall decision, the client-tool classification,
the id rule - is the reviewed code, executed unchanged, against the real manifest and
the real stacked checkout.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(ROOT), str(ROOT / "tests")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import ae_adapter  # noqa: E402
import ae_multicall  # noqa: E402
# The reviewed real-SDK harness: the REAL `OpenAIClient.request_async` body, the REAL
# `openai.AsyncOpenAI`, the REAL `build_request_data`, and an httpx2 capture transport.
import test_ae_deepseek_letta_thinking as sdk  # noqa: E402

STACK_MANIFEST = ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json"
MULTICALL_MANIFEST = ROOT / "deployment-assets/letta-multicall/manifest.json"
NO_COMPACTION_MANIFEST = ROOT / "deployment-assets/letta-no-compaction/manifest.json"
STACKED = Path(os.environ.get(
    "AE_LETTA_NO_COMPACTION_SOURCE",
    ROOT / ".ae-verify-src/letta-no-compaction-patch/letta-v1"))
AGENT = STACKED / "letta/agents/letta_agent_v3.py"
SHIM = STACKED / "letta/helpers/ae_multicall_compat.py"
MESSAGE_SOURCE = STACKED / "letta/schemas/message.py"
SHIM_MODULE = "letta.helpers.ae_multicall_compat"
RECEIVE_ENV_VAR = "AE_LETTA_MULTICALL_PROFILE"
STACK_ENV_VAR = "AE_LETTA_PATCH_STACK_PROFILE"
STACK_MANIFEST_ENV_VAR = "AE_LETTA_PATCH_STACK_MANIFEST"
MODULE_SHA_ENV_VAR = "AE_LETTA_MULTICALL_MODULE_SHA256"

#: The two `memory_update` calls of the r4 batch `03cb463e056e4bb5b95234d80eaa4ec8`, with
#: their REAL native ids, longer than the pinned `TOOL_CALL_ID_MAX_LEN` of 29. The
#: second call is one of the 29 calls the live service never handed over.
ID_A = "call_00_8Yg5WdxgTbyfGpsmWWpc8968"
ID_B = "call_01_pqSW6pfWeBIliZnASQme8786"
TRUNCATED_A = ID_A[:ae_multicall.UPSTREAM_TOOL_CALL_ID_MAX_LEN]
TRUNCATED_B = ID_B[:ae_multicall.UPSTREAM_TOOL_CALL_ID_MAX_LEN]

#: The two edits the two calls propose, in the frozen `memory_update` shape.
EDITS = (
    {"operation": "replace", "fact_id": "p000", "category": "饮食偏好",
     "content": "奶茶偏好7分糖", "evidence_ref": "t4/history/0"},
    {"operation": "replace", "fact_id": "p001", "category": "居住城市",
     "content": "住在杭州西湖区", "evidence_ref": "t4/history/1"},
)
FACTS = {"p000": {"category": "饮食偏好", "content": "奶茶偏好5分糖"},
         "p001": {"category": "居住城市", "content": "住在杭州滨江区"}}

#: The provider response the service receives: TWO declared client tool calls, exactly the
#: shape the offline scripted provider returns. It is parsed by the REAL SDK below.
PROVIDER_RESPONSE = {
    "id": "chatcmpl-r4-batch", "object": "chat.completion", "created": 1,
    "model": "deepseek-flash",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [
            {"id": ID_A, "type": "function",
             "function": {"name": "memory_update",
                          "arguments": json.dumps(EDITS[0], ensure_ascii=False)}},
            {"id": ID_B, "type": "function",
             "function": {"name": "memory_update",
                          "arguments": json.dumps(EDITS[1], ensure_ascii=False)}},
        ]}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _Sources:
    """Real code from a real file, pulled out unchanged.

    `function()` returns a whole real `def` with any decorators dropped (a decorator is
    not part of the body), and `block()` selects the lines between two markers inside a
    real method - the same way `tests/test_ae_multicall_letta.py` selects the pinned
    truncation block. No body is edited, and no expectation is written in place of it.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.source = self.path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.nodes = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.nodes[f"{node.name}.{item.name}"] = item
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.nodes[node.name] = node

    def function(self, dotted):
        lines = ast.get_source_segment(self.source, self.nodes[dotted]).splitlines()
        while lines and lines[0].lstrip().startswith("@"):
            lines.pop(0)
        return textwrap.dedent("\n".join(lines))

    def block(self, dotted, start_marker, end_marker):
        lines = self.function(dotted).splitlines()
        start = next(index for index, line in enumerate(lines) if start_marker in line)
        end = next(index for index in range(start, len(lines)) if end_marker in lines[index])
        return textwrap.dedent("\n".join(lines[start:end + 1]))


class _Recorder:
    """Stands in for a log sink; records what the real code reports."""

    def __init__(self):
        self.info_calls, self.warning_calls, self.error_calls = [], [], []

    def info(self, message, *args, **kwargs):
        self.info_calls.append(message)

    def warning(self, message, *args, **kwargs):
        self.warning_calls.append(message)

    def error(self, message, *args, **kwargs):
        self.error_calls.append(message)


def _declared_tool(name):
    return SimpleNamespace(name=name)


def _provider_tool_calls():
    """The batch as the REAL SDK parses it, then as the real adapter extracts it.

    `SimpleLLMRequestAdapter` sets
    `self.tool_calls = list(response.choices[0].message.tool_calls or [])`; that same
    line is applied to the SDK's own parsed object here, so the ids under test are the
    ids the SDK produced from the provider's wire JSON.
    """
    from openai.types.chat import ChatCompletion
    completion = ChatCompletion.model_validate(PROVIDER_RESPONSE)
    return list(completion.choices[0].message.tool_calls or [])


def _install_real_shim():
    """Register the REAL `letta.helpers.ae_multicall_compat` from the stacked checkout.

    The patched receive block imports that module by name, exactly as the service does.
    Loading the real file (never a fake) is what makes the decision under test the
    reviewed one: it runs the module's own `multicall_decision`, which imports the real
    `ae_multicall` support module and runs the real gate.
    """
    names = ("letta", "letta.helpers", SHIM_MODULE, "letta.utils")
    saved = {name: sys.modules.get(name) for name in names}
    letta = sys.modules.get("letta") or type(sys)("letta")
    letta.__path__ = []
    helpers = sys.modules.get("letta.helpers") or type(sys)("letta.helpers")
    helpers.__path__ = []
    letta.helpers = helpers
    sys.modules["letta"] = letta
    sys.modules["letta.helpers"] = helpers
    sys.modules[SHIM_MODULE] = _load(SHIM_MODULE, SHIM)

    def restore():
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return restore


class StackLaunchActivationTests(unittest.TestCase):
    """The launcher's declaration, verified by the real startup gate."""

    @classmethod
    def setUpClass(cls):
        if not STACK_MANIFEST.is_file() or not STACKED.is_dir():
            raise unittest.SkipTest("the stacked manifest or checkout is absent")
        cls.launcher = _load("ae_local_stack_activation",
                             ROOT / "scripts/deployment/letta_local.py")
        cls.bootstrap = _load("ae_bootstrap_stack_activation",
                              ROOT / "scripts/deployment/letta_bootstrap.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.record = json.loads(STACK_MANIFEST.read_text(encoding="utf-8"))

    def _runtime(self):
        """The site's stacked launch shape: byte-gate counting + the DeepSeek transport."""
        return self.launcher.PatchStackRuntime(
            manifest=STACK_MANIFEST, support=ROOT,
            receipt=Path(self.tmp.name) / "patch-stack-load.json",
            profile=self.launcher.STACK_PROFILE_VERSION,
            byte_gate_count_basis="no_token_count_byte_gate_only",
            byte_gate_max_request_bytes=2097152, byte_gate_wire_model="deepseek-flash",
            transport_profile="deepseek-official-re-transport-v1")

    def _child_env(self):
        return self._runtime().env(Path(self.tmp.name))

    def _find_spec(self):
        mapping = {module: (filename, key)
                   for key, (module, filename) in ae_multicall.STACK_SOURCES.items()}

        def find(name, package=None):
            if name not in mapping:
                return None
            _filename, key = mapping[name]
            return type("Spec", (), {"origin": str(STACKED / key)})()
        return find

    def _bootstrap_gate(self, env):
        """Run the REAL gate, capturing the real JSON report lines it prints."""
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            receipt = self.bootstrap.install_patch_stack_gate(env, find_spec=self._find_spec())
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        return receipt, events

    def test_the_stack_launch_declares_and_activates_the_composed_receive_policy(self):
        env = self._child_env()
        # 1. The declaration crosses into the child, from the pinned support module.
        self.assertEqual(env[RECEIVE_ENV_VAR], ae_multicall.PROFILE_VERSION)
        # ... and NOT the multicall-only manifest, which pins different bytes for the
        # file the capacity patch also changes.
        self.assertNotIn("AE_LETTA_MULTICALL_MANIFEST", env)
        self.assertEqual(env[STACK_ENV_VAR], ae_multicall.STACK_PROFILE_VERSION)
        self.assertEqual(env[MODULE_SHA_ENV_VAR], self.record["compat_module"]["sha256"])
        # 2. The contract verifies on that environment, against the stacked manifest AND
        #    the multicall manifest the stack names as its receive half.
        activation = ae_multicall.verify_receive_gate(env, module=ae_multicall)
        self.assertEqual(activation["contract"], ae_multicall.RECEIVE_STACKED)
        self.assertTrue(activation["active"])
        self.assertEqual(sorted(activation["receive_owned_files"]),
                         ["letta/helpers/ae_multicall_compat.py",
                          "letta/schemas/message.py"])
        self.assertFalse(activation["parallel_tool_calls"])
        self.assertTrue(activation["execute_serially"])
        self.assertTrue(activation["preserve_native_ids"])
        # 3. The REAL bootstrap gate verifies it in the process that would import Letta,
        #    and writes the stacking receipt the deployment needs.
        receipt, events = self._bootstrap_gate(env)
        ae_multicall.validate_stack_receipt(receipt, self.record, _sha256(STACK_MANIFEST))
        self.assertEqual(receipt["checkout"], str(STACKED.resolve()))
        self.assertTrue(Path(env["AE_LETTA_PATCH_STACK_RECEIPT"]).is_file())
        activated = [event for event in events
                     if event["event"] == "patch_stack_receive_policy_activated"]
        self.assertEqual(len(activated), 1)
        self.assertEqual(activated[0]["contract"], ae_multicall.RECEIVE_STACKED)
        self.assertEqual(activated[0]["receive_profile"], ae_multicall.PROFILE_VERSION)
        self.assertEqual(activated[0]["declared_in"], RECEIVE_ENV_VAR)
        self.assertEqual(activated[0]["parallel_tool_calls"], False)
        self.assertTrue(activated[0]["execute_serially"])

    def test_the_activation_comes_from_the_launcher_not_the_parent_environment(self):
        planted = {RECEIVE_ENV_VAR: "ae-multicall-receive-compat-999",
                   "AE_LETTA_MULTICALL_MANIFEST": "/parent/planted/manifest.json",
                   MODULE_SHA_ENV_VAR: "0" * 64,
                   "AE_PARENT_SECRET": "must-not-cross"}
        saved = {key: os.environ.get(key) for key in planted}
        os.environ.update(planted)
        self.addCleanup(self._restore, planted, saved)
        env = self._child_env()
        self.assertEqual(env[RECEIVE_ENV_VAR], ae_multicall.PROFILE_VERSION)
        self.assertEqual(env[MODULE_SHA_ENV_VAR], self.record["compat_module"]["sha256"])
        self.assertNotIn("AE_LETTA_MULTICALL_MANIFEST", env)
        self.assertNotIn("AE_PARENT_SECRET", env)
        self.assertTrue(ae_multicall.verify_receive_gate(env, module=ae_multicall)["active"])

    @staticmethod
    def _restore(planted, saved):
        for key in planted:
            if saved[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved[key]

    def test_a_stack_that_is_not_activated_refuses_to_start(self):
        env = self._child_env()
        env.pop(RECEIVE_ENV_VAR)
        # The stack declaration itself is still complete, so the refusal can only come
        # from the missing activation, and it happens before any receipt or import.
        self.assertIsNotNone(self.bootstrap._required_stack_environment(env))
        with self.assertRaises(RuntimeError) as raised:
            self.bootstrap.install_patch_stack_gate(env, find_spec=self._find_spec())
        self.assertIn("stack_without_receive_policy", str(raised.exception))
        self.assertFalse(Path(env["AE_LETTA_PATCH_STACK_RECEIPT"]).exists())
        state = ae_multicall.verify_receive_contract(env, module=ae_multicall)
        self.assertTrue(state["declared"])
        self.assertFalse(state["active"])
        self.assertIn("stack_without_receive_policy", state["reason"])

    def test_an_unknown_or_mixed_receive_declaration_is_refused(self):
        cases = (
            ("unknown receive version",
             lambda env: env.__setitem__(RECEIVE_ENV_VAR, "ae-multicall-receive-compat-999"),
             "unknown_receive_profile"),
            ("mixed multicall-only manifest",
             lambda env: env.__setitem__("AE_LETTA_MULTICALL_MANIFEST",
                                         str(MULTICALL_MANIFEST)),
             "mixed_receive_declaration"),
            ("unknown stacked version",
             lambda env: env.__setitem__(STACK_ENV_VAR, "ae-no-compaction-stack-999"),
             "unknown_stack_profile"),
            ("stale module digest",
             lambda env: env.__setitem__(MODULE_SHA_ENV_VAR, "0" * 64),
             "stack_gate_unverified"),
            ("absent stacked manifest",
             lambda env: env.__setitem__(STACK_MANIFEST_ENV_VAR,
                                         str(Path(self.tmp.name) / "absent.json")),
             "stack_gate_unverified"),
        )
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                env = self._child_env()
                mutate(env)
                # The startup gate refuses, and the receive contract itself states WHY:
                # this is the fail-closed state a runtime decision point reads.
                with self.assertRaises(RuntimeError):
                    self.bootstrap.install_patch_stack_gate(env, find_spec=self._find_spec())
                state = ae_multicall.verify_receive_contract(env, module=ae_multicall)
                self.assertTrue(state["declared"])
                self.assertFalse(state["active"])
                self.assertIn(expected, state["reason"])
                self.assertFalse(Path(env["AE_LETTA_PATCH_STACK_RECEIPT"]).exists())

    def test_the_two_service_contracts_are_alternatives_not_a_pair(self):
        multicall = self.launcher.MulticallRuntime(
            manifest=MULTICALL_MANIFEST, support=ROOT,
            receipt=Path(self.tmp.name) / "multicall-load.json",
            profile=ae_multicall.PROFILE_VERSION)
        deploy = self.launcher.Deployment(Path(self.tmp.name))
        with self.assertRaises(RuntimeError) as raised:
            deploy.env(multicall=multicall, patch_stack=self._runtime())
        self.assertIn("not both", str(raised.exception))
        # ... and the CLI refuses the pair before any action, as a usage error.
        argv = ["letta_local", "start", "--project", self.tmp.name,
                "--multicall-manifest", str(MULTICALL_MANIFEST),
                "--multicall-support", str(ROOT),
                "--patch-stack-manifest", str(STACK_MANIFEST),
                "--patch-stack-support", str(ROOT), "--bounded-nltk-startup"]
        stderr = self._cli_error(argv)
        self.assertIn("not both", stderr)
        # A stacked launch also needs its own support directory and the bootstrap wrapper
        # (the gate, the composed receive policy and the receipt run in that process).
        for extra, expected in (
                (["--patch-stack-manifest", str(STACK_MANIFEST)], "--patch-stack-support"),
                (["--patch-stack-manifest", str(STACK_MANIFEST),
                  "--patch-stack-support", str(ROOT)], "--bounded-nltk-startup")):
            with self.subTest(extra=extra):
                self.assertIn(expected, self._cli_error(
                    ["letta_local", "start", "--project", self.tmp.name] + extra))

    def _cli_error(self, argv):
        """Run the real CLI with one argv; return what it refused with."""
        import unittest.mock
        stderr = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            self.launcher.main()
        self.assertEqual(raised.exception.code, 2)
        return stderr.getvalue()


class _StubNothing:
    pass


class _StubMessage:
    """Minimal `Message` stand-in for the real list serializer; no pydantic needed."""

    def __init__(self, **attributes):
        defaults = {"role": "assistant", "content": None, "name": None, "tool_calls": None,
                    "tool_returns": None, "tool_call_id": None, "id": "message-fixture"}
        defaults.update(attributes)
        self.__dict__.update(defaults)

    @staticmethod
    def filter_messages_for_llm_api(messages):
        """Identity: the collapse/dedupe chain is not what this test measures.

        The real list serializer calls this first; everything after it - including the
        `preserve_tool_call_id` expansion under test - runs from the real body.
        """
        return list(messages)


class _LettaSessionTransport:
    """The driver's Letta-side session, i.e. the FINAL HTTP layer, substituted.

    It answers the driver's own request shapes with the API's data shapes: the agent
    state the session check requires, the block PATCH the memory tool confirms, and the
    approval / end-turn responses. The approval payload it serves is the one the real
    service-side code produced, so the driver is fed the real batch.
    """

    def __init__(self):
        self.sent = []
        self.session = SimpleNamespace(
            agent_id="agent-r4-fixture", block_value=None, patches=[],
            tool_returns=[], pending=None, replies=["done"])

    def request(self, method, path, body=None):
        self.sent.append({"method": method, "path": path, "body": copy.deepcopy(body)})
        session = self.session
        if method == "GET":
            return {"id": session.agent_id, "agent_type": "letta_v1_agent",
                    "message_buffer_autoclear": False, "enable_sleeptime": False,
                    "tools": [], "sources": [], "tags": [],
                    "blocks": [{"id": "block-r4-fixture", "label": ae_adapter.BLOCK_LABEL,
                                "value": session.block_value}],
                    "message_ids": ["system-fixture"], "pending_approval": None,
                    "managed_group": None}
        if method == "PATCH":
            session.block_value = body["value"]
            session.patches.append(body["value"])
            return {"id": "block-r4-fixture", "label": ae_adapter.BLOCK_LABEL,
                    "value": body["value"]}
        if method == "POST":
            for message in body.get("messages") or []:
                if message.get("type") == "tool_return":
                    session.tool_returns.extend(copy.deepcopy(message["tool_returns"]))
                    session.pending = None
                    text = session.replies.pop(0) if session.replies else "done"
                    return {"messages": [{"message_type": "assistant_message",
                                          "content": text}],
                            "stop_reason": {"stop_reason": "end_turn"}, "usage": {}}
            if session.pending is not None:
                return {"messages": [copy.deepcopy(session.pending)],
                        "stop_reason": {"stop_reason": "requires_approval"}, "usage": {}}
            raise AssertionError("the scripted session has no response for this POST")
        raise AssertionError(method)


class TheWholeBatchSurvivesTheStackedRuntimeTests(unittest.TestCase):
    """Launcher env -> real bootstrap -> real receive/response handling -> real wire."""

    @classmethod
    def setUpClass(cls):
        if not STACK_MANIFEST.is_file() or not AGENT.is_file() or not SHIM.is_file():
            raise unittest.SkipTest("the stacked checkout or manifest is absent")
        cls.agent_source = _Sources(AGENT)
        cls.message_source = _Sources(MESSAGE_SOURCE)
        cls.launcher = _load("ae_local_stack_batch",
                             ROOT / "scripts/deployment/letta_local.py")
        cls.bootstrap = _load("ae_bootstrap_stack_batch",
                              ROOT / "scripts/deployment/letta_bootstrap.py")
        cls.build_request_data = sdk._load_build_request_data()
        cls.agent_methods = sdk._load_agent_methods()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        runtime = self.launcher.PatchStackRuntime(
            manifest=STACK_MANIFEST, support=ROOT,
            receipt=Path(self.tmp.name) / "patch-stack-load.json",
            profile=self.launcher.STACK_PROFILE_VERSION,
            byte_gate_count_basis="no_token_count_byte_gate_only",
            byte_gate_max_request_bytes=2097152, byte_gate_wire_model="deepseek-flash",
            transport_profile="deepseek-official-re-transport-v1")
        self.child_env = runtime.env(Path(self.tmp.name))
        # The REAL bootstrap gate runs on that child environment, in the process that
        # would import Letta: it verifies the stacked contract AND the receive policy the
        # stack composes, and writes the receipt the deployment needs. The environment is
        # only installed after it accepted, exactly as the service process does.
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.stack_receipt = self.bootstrap.install_patch_stack_gate(
                self.child_env, find_spec=self._find_spec())
        self.bootstrap_events = [json.loads(line) for line in stream.getvalue().splitlines()
                                 if line.strip()]
        self.assertIsNotNone(self.stack_receipt)
        self.assertTrue(Path(self.child_env["AE_LETTA_PATCH_STACK_RECEIPT"]).is_file())
        # The child's environment is installed in THIS process, so the patched receive
        # block reads exactly what the service process would read - including the
        # declared request shape the real sender applies.
        self.saved_env = {key: os.environ.get(key) for key in
                          sorted(set(self.child_env) | {RECEIVE_ENV_VAR,
                                                         "AE_LETTA_MULTICALL_MANIFEST",
                                                         STACK_MANIFEST_ENV_VAR})}
        os.environ.update({key: value for key, value in self.child_env.items()
                           if key.startswith("AE_")})
        os.environ.pop("AE_LETTA_MULTICALL_MANIFEST", None)
        self.addCleanup(self._restore_env)
        self.addCleanup(_install_real_shim())

    def _find_spec(self):
        mapping = {module: (filename, key)
                   for key, (module, filename) in ae_multicall.STACK_SOURCES.items()}

        def find(name, package=None):
            if name not in mapping:
                return None
            _filename, key = mapping[name]
            return type("Spec", (), {"origin": str(STACKED / key)})()
        return find

    def _restore_env(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # -- segment 1: the real patched receive block -------------------------------

    def _receive_block(self, tool_calls, client_tools=("memory_update",)):
        """Run the REAL receive block of the STACKED `LettaAgentV3._step`.

        The window is the real gather-tool-calls lines plus the patched decision; the
        decision itself, including the real import of `multicall_decision`, runs from
        the checkout's own bytes.
        """
        block = self.agent_source.block(
            "LettaAgentV3._step",
            "# Gather tool calls - check for multi-call API first",
            "tool_calls = [tool_calls[0]]")
        agent = SimpleNamespace(client_tools=[_declared_tool(name) for name in client_tools],
                                logger=_Recorder())
        namespace = {"active_llm_config": SimpleNamespace(parallel_tool_calls=False)}
        local = {"self": agent,
                 "llm_adapter": SimpleNamespace(tool_calls=tool_calls,
                                                tool_call=tool_calls[0] if tool_calls else None)}
        exec(compile(block, "<stacked-step-receive-block>", "exec"), namespace, local)
        return local["tool_calls"], agent.logger

    # -- segment 2: the real client-tool branch of `_handle_ai_response` ----------

    def _client_tool_branch(self, tool_calls):
        """Run the REAL client-tool branch, which builds the driver's approval payload."""
        body = self.agent_source.block(
            "LettaAgentV3._handle_ai_response",
            "client_tool_names = {ct.name for ct in self.client_tools}",
            "StopReasonType.requires_approval.value)")
        recorded = {}

        def create_approval_request_message_from_llm_response(**kwargs):
            recorded.update(kwargs)
            return [{"message_type": "approval_request_message",
                     "tool_calls": [{"type": "tool_call", "tool_call_id": call.id,
                                     "name": call.function.name,
                                     "arguments": call.function.arguments}
                                    for call in kwargs["requested_tool_calls"]]}]

        module = {
            "create_approval_request_message_from_llm_response":
                create_approval_request_message_from_llm_response,
            "StopReasonType": SimpleNamespace(
                requires_approval=SimpleNamespace(value="requires_approval")),
            "LettaStopReason": lambda **kwargs: SimpleNamespace(**kwargs),
        }
        wrapped = ("def _branch(self, tool_calls, tool_rules_solver, content=None,\n"
                   "            pre_computed_assistant_message_id=None, step_id=None,\n"
                   "            run_id=None, initial_messages=None):\n"
                   + textwrap.indent(body, "    "))
        exec(compile(wrapped, "<stacked-client-tool-branch>", "exec"), module)
        solver = SimpleNamespace(is_requires_approval_tool=lambda name: False)
        agent = SimpleNamespace(
            client_tools=[_declared_tool("memory_update")],
            agent_state=SimpleNamespace(id="agent-r4-fixture", timezone="UTC",
                                        llm_config=SimpleNamespace(model="deepseek-flash")))
        return module["_branch"](agent, tool_calls, solver)

    # -- segment 3: the real driver bridge over the service's own approval --------

    def _driver(self, approval_message):
        transport = _LettaSessionTransport()
        memory = ae_adapter.MemoryPolicy("rewrite", FACTS, block_char_limit=8000)
        transport.session.block_value = memory.initial_block
        transport.session.pending = approval_message
        bridge = ae_adapter.LettaBridge(transport, "agent-r4-fixture", memory,
                                       max_rounds=8, max_steps=3,
                                       multicall=ae_multicall.CURRENT_POLICY)
        bridge.visible_refs = {"t4/history/0", "t4/history/1"}
        return bridge, transport

    # -- segment 4: the service's NEXT provider request, through the real SDK -----

    def _serialize(self, message):
        """The REAL `Message.to_openai_dicts_from_list`, with its own real call site."""
        call_body = self.message_source.function("Message.to_openai_dict")
        body = self.message_source.function("Message.to_openai_dicts_from_list")
        namespace = {
            "List": list, "Optional": object, "Message": _StubMessage,
            "MessageRole": type("MessageRole", (), {"tool": "tool"}),
            "TOOL_CALL_ID_MAX_LEN": ae_multicall.UPSTREAM_TOOL_CALL_ID_MAX_LEN,
            "put_inner_thoughts_in_kwargs": False,
            "TextContent": _StubNothing, "ToolReturnContent": _StubNothing,
            "ReasoningContent": _StubNothing, "RedactedReasoningContent": _StubNothing,
            "OmittedReasoningContent": _StubNothing,
            "tool_return_to_text": lambda value: value,
            "truncate_tool_return": lambda text, _chars: text,
            "add_inner_thoughts_to_tool_call": lambda call, **kwargs: call,
            "parse_json": lambda value: value, "logger": _Recorder(), "re": re,
            "REQUEST_HEARTBEAT_PARAM": "_request_heartbeat",
            "INNER_THOUGHTS_KWARG": "inner_thoughts",
            # The REAL id rule the checkout's serializers call, from the REAL shim module
            # the service loads - not a stand-in.
            "preserve_tool_call_id":
                sys.modules[SHIM_MODULE].preserve_tool_call_id,
        }
        # `filter_messages_for_llm_api` is the collapse/dedupe chain this test is not
        # about; stubbing it to identity keeps the body under test (including its own
        # `preserve_tool_call_id` expansion) real.
        namespace["filter_messages_for_llm_api"] = lambda messages: list(messages)
        exec(compile(call_body, "<stacked-to-openai-dict>", "exec"), namespace)
        _StubMessage.to_openai_dict = namespace["to_openai_dict"]
        exec(compile(body, "<stacked-to-openai-dicts>", "exec"), namespace)
        return namespace["to_openai_dicts_from_list"]([message])

    def _next_request_wire(self, tool_returns):
        """The service's NEXT provider request: real serializers, real sender, real SDK."""
        message = _StubMessage(role="tool", tool_returns=[
            SimpleNamespace(tool_call_id=item["tool_call_id"],
                            func_response=item["tool_return"], status=item["status"])
            for item in tool_returns])
        body = self.build_request_data(
            agent_type="letta_v1_agent", messages=self._serialize(message),
            llm_config=sdk._llm_config("deepseek-flash"),
            tools=[copy.deepcopy(sdk.TOOL)], force_tool_call=None,
            requires_subsequent_tool_call=False, tool_return_truncation_chars=26214,
            system=None)
        self.agent_methods._ae_apply_transport_fields(body)
        captured, client = sdk._CaptureTransport.build()
        request_async = sdk._load_request_async(client)

        async def go():
            try:
                return await request_async(body, sdk._llm_config(body["model"]))
            finally:
                await client.aclose()

        asyncio.run(go())
        return json.loads(captured["content"].decode("utf-8")), captured

    # -- the acceptance ----------------------------------------------------------

    def test_two_memory_updates_reach_the_driver_and_their_whole_ids_reach_the_wire(self):
        # 0. The real bootstrap gate accepted this child environment and recorded the
        #    receive policy it activated before anything below ran.
        activated = [event for event in self.bootstrap_events
                     if event["event"] == "patch_stack_receive_policy_activated"]
        self.assertEqual(len(activated), 1)
        self.assertEqual(activated[0]["contract"], ae_multicall.RECEIVE_STACKED)
        self.assertEqual(activated[0]["receive_profile"], ae_multicall.PROFILE_VERSION)
        provider_calls = _provider_tool_calls()
        self.assertEqual([call.id for call in provider_calls], [ID_A, ID_B])
        self.assertEqual(len(ID_A), 32)
        self.assertGreater(len(ID_A), ae_multicall.UPSTREAM_TOOL_CALL_ID_MAX_LEN)
        self.assertNotEqual(TRUNCATED_A, ID_A)
        # 1. The REAL patched receive block keeps the whole batch.
        kept, logger = self._receive_block(provider_calls)
        self.assertEqual([call.id for call in kept], [ID_A, ID_B])
        self.assertEqual(logger.warning_calls, [])
        self.assertEqual(len(logger.info_calls), 1)
        # 2. The REAL client-tool branch hands BOTH calls to the driver's approval.
        messages, continue_stepping, stop_reason = self._client_tool_branch(kept)
        self.assertFalse(continue_stepping)
        self.assertEqual(stop_reason.stop_reason, "requires_approval")
        approval = messages[0]
        self.assertEqual(approval["message_type"], "approval_request_message")
        self.assertEqual([call["tool_call_id"] for call in approval["tool_calls"]],
                         [ID_A, ID_B])
        self.assertEqual([call["name"] for call in approval["tool_calls"]],
                         ["memory_update", "memory_update"])
        # 3. The REAL driver executes each call exactly once, in order.
        bridge, transport = self._driver(approval)
        assistant = bridge.exchange([{"role": "user", "type": "message",
                                      "content": json.dumps({"source": "current_task"})}], {})
        self.assertEqual(assistant, [{"message_type": "assistant_message", "content": "done"}])
        self.assertEqual([result["tool_call_id"] for result in transport.session.tool_returns],
                         [ID_A, ID_B])
        self.assertEqual([result["status"] for result in transport.session.tool_returns],
                         ["success", "success"])
        self.assertEqual(len(transport.session.patches), 2)
        # Serial execution, in the provider's order: the second PATCH carries the state
        # the first call committed, and each id was executed exactly once.
        first, second = (json.loads(value) for value in transport.session.patches)
        self.assertEqual(first["p000"]["content"], EDITS[0]["content"])
        self.assertEqual(first["p001"]["content"], FACTS["p001"]["content"])
        self.assertEqual(second["p000"]["content"], EDITS[0]["content"])
        self.assertEqual(second["p001"]["content"], EDITS[1]["content"])
        evidence = bridge.multicall_batches[0]
        self.assertEqual(evidence["count"], 2)
        self.assertEqual(evidence["executed_count"], 2)
        self.assertEqual([item["tool_call_id"] for item in evidence["returns"]], [ID_A, ID_B])
        posts = [entry for entry in transport.sent if entry["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual([returned["tool_call_id"] for returned
                          in posts[1]["body"]["messages"][0]["tool_returns"]], [ID_A, ID_B])
        # 4. Both returns enter the NEXT real SDK request with their ids WHOLE.
        wire, captured = self._next_request_wire(transport.session.tool_returns)
        tool_messages = [message for message in wire["messages"] if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], [ID_A, ID_B])
        self.assertEqual(len({message["tool_call_id"] for message in tool_messages}), 2)
        for message in tool_messages:
            self.assertNotEqual(message["tool_call_id"],
                                message["tool_call_id"][:29])
        self.assertEqual(wire["model"], "deepseek-flash")
        self.assertEqual(wire["thinking"], {"type": "disabled"})
        # Keeping the whole batch never becomes concurrency: the request still declares
        # `parallel_tool_calls=false`, and the batch was executed serially above.
        self.assertFalse(wire["parallel_tool_calls"])
        self.assertEqual([tool["function"]["name"] for tool in wire["tools"]],
                         ["memory_update"])
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        raw = captured["content"].decode("utf-8")
        # The whole projected value appears as NO id on the wire; the whole native ids do.
        for projected in (TRUNCATED_A, TRUNCATED_B):
            self.assertNotIn('"tool_call_id":"' + projected + '"', raw)
        for native in (ID_A, ID_B):
            self.assertIn('"tool_call_id":"' + native + '"', raw)

    def test_the_previous_service_state_truncates_the_same_batch(self):
        """The live defect, reproduced: without the activation the first call wins."""
        os.environ.pop(RECEIVE_ENV_VAR, None)
        kept, logger = self._receive_block(_provider_tool_calls())
        self.assertEqual([call.id for call in kept], [ID_A])
        self.assertEqual(len(logger.warning_calls), 1)
        self.assertIn("Truncating to first tool call", logger.warning_calls[0])
        # ... and the surviving id was projected to 29 characters on the wire.
        wire, captured = self._next_request_wire(
            [{"tool_call_id": call.id, "tool_return": "{}", "status": "success"}
             for call in kept])
        tool_messages = [message for message in wire["messages"] if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], [TRUNCATED_A])
        self.assertIn('"tool_call_id":"' + TRUNCATED_A + '"',
                      captured["content"].decode("utf-8"))

    def test_a_declared_but_unverifiable_stack_refuses_instead_of_truncating(self):
        """Declared and unverifiable: a refusal, never the truncation fallback."""
        tampered = json.loads(STACK_MANIFEST.read_text(encoding="utf-8"))
        tampered["patched_files"]["letta/schemas/message.py"] = "0" * 64
        tampered_path = Path(self.tmp.name) / "tampered-stack-manifest.json"
        tampered_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        # The exception the CHECKOUT raises is its own class (the shim defines it), which
        # is what the patched `_step` block imports and re-raises.
        refused = sys.modules[SHIM_MODULE].MulticallRefused
        cases = (
            ("absent stacked manifest",
             {"AE_LETTA_PATCH_STACK_MANIFEST": "/absent/stack-manifest.json"},
             "stack_gate_unverified"),
            ("stale module digest", {MODULE_SHA_ENV_VAR: "0" * 64},
             "stack_gate_unverified"),
            ("manifest whose receive digest is tampered",
             {"AE_LETTA_PATCH_STACK_MANIFEST": str(tampered_path)},
             "stack_gate_unverified"),
        )
        for label, broken, expected in cases:
            with self.subTest(label=label):
                saved = {key: os.environ.get(key) for key in broken}
                os.environ.update(broken)
                try:
                    # The decision raises BEFORE it returns a call list, so no partial
                    # batch can ever reach the client-tool branch or the driver, and the
                    # pinned truncation branch is never taken under a declaration.
                    with self.assertRaises(refused) as raised:
                        self._receive_block(_provider_tool_calls())
                    self.assertNotIn("Truncating to first tool call", str(raised.exception))
                    state = ae_multicall.verify_receive_contract(os.environ, module=ae_multicall)
                    self.assertTrue(state["declared"])
                    self.assertFalse(state["active"])
                    self.assertIn(expected, state["reason"])
                finally:
                    for key, value in saved.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
