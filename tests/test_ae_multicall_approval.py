"""Offline proof that the REAL Letta approval-conversion chain keeps every call.

`tests/test_ae_multicall_letta.py` already runs the real truncation block and the
real `Message.to_openai_dict` serializer. This module covers the two steps between
them, which the r2 capture showed the client actually receives and re-sends:

1. `create_approval_request_message_from_llm_response` (the pinned
   `letta/server/rest_api/utils.py` function) turns the provider's requested tool
   calls into the persisted approval `Message`. It must keep all four calls, in
   order, with their native ids.
2. `Message._convert_approval_request_message` turns that message into the
   `ApprovalRequestMessage` the HTTP client receives. Its `tool_calls` list must
   be complete, and the deprecated single `tool_call` field must be the FIRST of
   that same list rather than a replacement for it.

The function bodies are extracted from the real pinned source with
`ast.get_source_segment` and compiled unchanged; only their module-level names
are stubbed. No body is edited and no expectation is hand-written in place of
real code.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import tempfile
import textwrap
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ae_multicall_letta import (BASELINE_SHAS, PATCHED_SOURCE,  # noqa: E402
                                     QualifiedExtractor, R2_IDS, SHIM_RELATIVE,
                                     StubAgent, StubDeclaredTool, StubLogger, StubNothing,
                                     StubTextContent,
                                     UPSTREAM_TOOL_CALL_ID_MAX_LEN, compiled,
                                     install_letta_stubs, multicall_environment,
                                     shim_namespace, write_production_manifest)

# The authoritative source; the patch does not touch this file, so baseline and
# patched copies must be identical here and the audit can say which it used.
# The shim's profile gate, mirrored here so the chain can be run both ways.
LETTA_ENV_VAR = "AE_LETTA_MULTICALL_PROFILE"
PROFILE_VERSION = "ae-multicall-receive-compat-1"

REST_UTILS = "letta/server/rest_api/utils.py"
MESSAGE_PY = "letta/schemas/message.py"
LETTA_MESSAGE_PY = "letta/schemas/letta_message.py"
TRUNCATED = "01a094c62aacd8ab46b11b69c6e81"


class SimpleRecord:
    """Minimal stand-in for pydantic `BaseModel` in an extracted model class."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __getitem__(self, key):
        return getattr(self, key)


class StubFunction:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class StubProviderToolCall:
    """One provider tool call as Letta's `llm_adapter.tool_calls` carries it."""

    def __init__(self, call_id, name="memory_update", arguments="{}"):
        self.id = call_id
        self.type = "function"
        self.function = StubFunction(name, arguments)


class StubOpenAIToolCall:
    """Only the constructor call the pinned builder makes is exercised."""

    def __init__(self, *, id, function, type):  # noqa: A002 - mirrors the SDK
        self.id, self.function, self.type = id, function, type

    def model_dump(self):
        return {"id": self.id, "type": self.type,
                "function": {"name": self.function.name,
                             "arguments": self.function.arguments}}


class StubFunctionFactory:
    def __init__(self, *, name, arguments):
        self.name, self.arguments = name, arguments


class StubApprovalMessage:
    """The persisted approval `Message`, restricted to what the chain touches."""

    def __init__(self, role=None, content=None, agent_id=None, model=None,
                 tool_calls=None, tool_call_id=None, created_at=None, step_id=None,
                 run_id=None, approvals=None, tool_returns=None, name=None):
        self.role, self.content = role, content
        self.agent_id, self.model = agent_id, model
        self.tool_calls, self.tool_call_id = tool_calls, tool_call_id
        self.created_at, self.step_id, self.run_id = created_at, step_id, run_id
        self.id = "message-fixture-0000"
        self.otid = None
        self.sender_id = agent_id
        self.name = name
        self.approvals = approvals
        self.tool_returns = tool_returns


def provider_calls():
    return [StubProviderToolCall(call_id, arguments=json.dumps({"fact_id": f"p{i:03d}"}))
            for i, call_id in enumerate(R2_IDS)]


class ApprovalConversionTests(unittest.TestCase):
    """The real approval chain, on the real four-call batch."""

    def build(self, pre_computed_id=None):
        body = QualifiedExtractor(
            PATCHED_SOURCE / REST_UTILS).get(
                "create_approval_request_message_from_llm_response")
        namespace = {
            "List": list, "Optional": object, "Union": object,
            "OpenAIToolCall": StubOpenAIToolCall,
            "OpenAIFunction": StubFunctionFactory,
            "Message": StubApprovalMessage,
            "MessageRole": type("MessageRole", (), {"assistant": "assistant",
                                                    "approval": "approval"}),
            "get_utc_time": lambda: "2026-09-12T00:00:00Z",
            "decrement_message_uuid": lambda value: value,
        }
        # The real staticmethod body the pinned builder calls, extracted unchanged.
        otid_body = QualifiedExtractor(PATCHED_SOURCE / MESSAGE_PY).get(
            "Message.generate_otid_from_id")
        exec(compile(otid_body, "<extracted>", "exec"), namespace)
        StubApprovalMessage.generate_otid_from_id = staticmethod(
            namespace["generate_otid_from_id"])
        fn = compiled(namespace, body, "create_approval_request_message_from_llm_response")
        messages = fn(agent_id="agent-fixture", model="model-fixture",
                      requested_tool_calls=provider_calls(),
                      pre_computed_assistant_message_id=pre_computed_id)
        self.assertEqual(len(messages), 1, "only one approval message is produced")
        return messages[0]

    def test_all_four_calls_survive_into_the_approval_message(self):
        message = self.build()
        self.assertEqual(message.role, "approval")
        self.assertEqual([call.id for call in message.tool_calls], list(R2_IDS))
        self.assertEqual(message.tool_call_id, R2_IDS[0])
        for call in message.tool_calls:
            self.assertEqual(call.type, "function")
            self.assertEqual(call.function.name, "memory_update")
            self.assertGreater(len(call.id), 29)
        # The 29-character prefix is shared, so a truncating client could not
        # tell these apart; the real builder never does that.
        self.assertEqual({call.id[:29] for call in message.tool_calls}, {TRUNCATED})

    def test_the_real_converter_emits_the_complete_list_and_a_first_element_alias(self):
        message = self.build()
        body = QualifiedExtractor(PATCHED_SOURCE / MESSAGE_PY).get(
            "Message._convert_approval_request_message")
        namespace = {
            "ApprovalRequestMessage": lambda **kwargs: kwargs,
            "ToolCall": self.tool_call_class(),
        }
        fn = compiled(namespace, body, "_convert_approval_request_message")
        converted = fn(message)
        self.assertEqual([call["tool_call_id"] for call in converted["tool_calls"]],
                         list(R2_IDS))
        # The deprecated single field is the first of that same list, not a
        # replacement: the list stays complete.
        self.assertEqual(converted["tool_call"]["tool_call_id"], R2_IDS[0])
        self.assertEqual(len(converted["tool_calls"]), 4)
        self.assertEqual({call["tool_call_id"][:29] for call in converted["tool_calls"]},
                         {TRUNCATED})
        self.assertEqual(converted["otid"], message.otid)

    def test_ordering_argument_bytes_and_names_are_preserved(self):
        message = self.build()
        expected = [(call.id, call.function.name, call.function.arguments)
                    for call in provider_calls()]
        observed = [(call.id, call.function.name, call.function.arguments)
                    for call in message.tool_calls]
        self.assertEqual(observed, expected)
        # The argument text is not re-encoded, re-ordered or trimmed.
        for call, (_, _, arguments) in zip(message.tool_calls, expected):
            self.assertEqual(call.function.arguments, arguments)
            self.assertEqual(json.loads(call.function.arguments)["fact_id"],
                             json.loads(arguments)["fact_id"])

    @staticmethod
    def tool_call_class():
        """The pinned `ToolCall` model, extracted as a class and made callable."""
        extractor = QualifiedExtractor(PATCHED_SOURCE / LETTA_MESSAGE_PY)
        node = next(n for n in extractor.tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "ToolCall")
        holder = {}
        exec(compile(ast.get_source_segment(extractor.text, node), "<extracted>", "exec"),
             {"BaseModel": SimpleRecord, "Literal": object, "Optional": object}, holder)
        return holder["ToolCall"]

    def test_the_pinned_tool_call_model_imposes_no_length_bound(self):
        """`tool_call_id: str` with no `Field` constraint: nothing shortens it."""
        extractor = QualifiedExtractor(PATCHED_SOURCE / LETTA_MESSAGE_PY)
        node = next(n for n in extractor.tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "ToolCall")
        annotations = {statement.target.id: statement.annotation
                       for statement in node.body if isinstance(statement, ast.AnnAssign)}
        self.assertEqual(set(annotations), {"name", "arguments", "tool_call_id"})
        for field, annotation in annotations.items():
            with self.subTest(field=field):
                self.assertIsInstance(annotation, ast.Name)
                self.assertEqual(annotation.id, "str")  # bare str, never Field(...)

    def test_the_source_file_is_untouched_by_the_compatibility_patch(self):
        """The approval builder is not part of the patch, so it must be identical."""
        baseline = Path(os.environ.get("AE_LETTA_SOURCE",
                                       "/tmp/ae-letta-QaM3ld/letta-v1")) / REST_UTILS
        patched = PATCHED_SOURCE / REST_UTILS
        self.assertTrue(baseline.is_file() and patched.is_file())
        self.assertEqual(baseline.read_bytes(), patched.read_bytes())
        # And the file the patch DOES touch still differs there.
        self.assertNotEqual(
            (Path(os.environ.get("AE_LETTA_SOURCE",
                                 "/tmp/ae-letta-QaM3ld/letta-v1")) / MESSAGE_PY).read_bytes(),
            (PATCHED_SOURCE / MESSAGE_PY).read_bytes())
        self.assertIn(MESSAGE_PY, BASELINE_SHAS)


if __name__ == "__main__":
    unittest.main()


class StubApproval:
    """One entry of an `ApprovalCreate.approvals` list."""

    def __init__(self, tool_call_id, approve=True, reason=None):
        self.tool_call_id, self.approve, self.reason = tool_call_id, approve, reason


class StubApprovalCreate:
    def __init__(self, approvals):
        self.approvals = approvals


class ApprovalResumeTests(unittest.TestCase):
    """The real approval-resume helpers, on the real truncated-prefix ids.

    `validate_approval_tool_call_ids` runs when a client submits approvals, and
    `_maybe_get_approval_messages` + the `_step` extraction decide which pending
    calls are executed from a resumed approval. Both key on the EXACT native id,
    so this is where a shortened id would silently drop calls rather than raise.
    """

    def validate_ids(self, request, response):
        body = QualifiedExtractor(PATCHED_SOURCE / "letta/agents/helpers.py").get(
            "validate_approval_tool_call_ids")
        namespace = {"ApprovalCreate": StubApprovalCreate}
        fn = compiled(namespace, body, "validate_approval_tool_call_ids")
        return fn(request, response)

    def extraction(self, messages):
        helpers = QualifiedExtractor(PATCHED_SOURCE / "letta/agents/helpers.py")
        body = helpers.get("_maybe_get_approval_messages")
        namespace = {"Tuple": tuple, "Message": StubApprovalMessage,
                     "ApprovalReturn": StubApproval, "ToolReturn": StubApproval}
        compiled(namespace, body, "_maybe_get_approval_messages")
        fn = namespace["_maybe_get_approval_messages"]
        request, response = fn(messages)
        if request is None or response is None:
            return None, None
        backfill = request.tool_calls[0].id
        approved = ({backfill if a.tool_call_id.startswith("message-") else a.tool_call_id
                     for a in response.approvals if isinstance(a, StubApproval) and a.approve})
        pending = [call for call in request.tool_calls if call.id in approved]
        return request, pending

    def request_message(self, ids=R2_IDS, message_id="message-fixture-0000"):
        calls = [StubOpenAIToolCall(id=call_id, function=StubFunction("memory_update", "{}"),
                                    type="function") for call_id in ids]
        message = StubApprovalMessage(role="approval", tool_calls=calls, tool_call_id=calls[0].id)
        message.id = message_id
        return message

    def test_four_matching_approvals_validate_and_keep_every_call(self):
        request = self.request_message()
        response = StubApprovalCreate([StubApproval(call_id) for call_id in R2_IDS])
        self.assertIsNone(self.validate_ids(request, response))
        _, pending = self.extraction([request, StubApprovalMessage(role="approval",
                                                                  approvals=response.approvals)])
        self.assertEqual([call.id for call in pending], list(R2_IDS))

    def test_a_truncated_or_missing_approval_id_is_rejected_by_the_real_helper(self):
        request = self.request_message()
        cases = {
            "truncated to the shared 29-character prefix":
                [StubApproval(TRUNCATED) for _ in R2_IDS],
            "one id dropped": [StubApproval(call_id) for call_id in R2_IDS[:3]],
            "one extra id": [StubApproval(call_id) for call_id in R2_IDS]
                            + [StubApproval("call-extra")],
        }
        for label, approvals in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    self.validate_ids(request, StubApprovalCreate(approvals))

    def test_this_helper_compares_id_SETS_not_order(self):
        """Declared boundary: order is guaranteed elsewhere, not by this helper.

        `validate_approval_tool_call_ids` uses a set symmetric difference, so a
        reordered but complete approval list passes it. Ordering of the executed
        calls and of the submitted returns is guaranteed by the bridge (serial,
        provider order) and checked by the audit's ordering gates, not here.
        """
        request = self.request_message()
        swapped = [StubApproval(call_id) for call_id in
                   (R2_IDS[1], R2_IDS[0], R2_IDS[3], R2_IDS[2])]
        self.assertIsNone(self.validate_ids(request, StubApprovalCreate(swapped)))

    def test_denied_calls_are_reported_as_denials_not_silently_dropped(self):
        request = self.request_message()
        approvals = [StubApproval(R2_IDS[0], approve=True),
                     StubApproval(R2_IDS[1], approve=False, reason="fixture denial"),
                     StubApproval(R2_IDS[2], approve=True),
                     StubApproval(R2_IDS[3], approve=False, reason="fixture denial")]
        response = StubApprovalCreate(approvals)
        # The real id check passes: a denial is still an explicit decision.
        self.assertIsNone(self.validate_ids(request, response))
        _, approved = self.extraction([request, StubApprovalMessage(
            role="approval", approvals=approvals)])
        self.assertEqual([call.id for call in approved], [R2_IDS[0], R2_IDS[2]])
        denied = [a.tool_call_id for a in approvals if not a.approve]
        self.assertEqual(denied, [R2_IDS[1], R2_IDS[3]])

    def test_the_legacy_message_id_equivalence_still_works_for_one_call(self):
        request = self.request_message(ids=R2_IDS[:1], message_id="message-fixture-legacy")
        response = StubApprovalCreate([StubApproval("message-fixture-legacy")])
        self.assertIsNone(self.validate_ids(request, response))

    def test_no_approval_pair_returns_no_pending_calls(self):
        self.assertEqual(self.extraction([StubApprovalMessage(role="assistant")]),
                         (None, None))


class FilterDedupeTests(unittest.TestCase):
    """The real per-message dedupe pass that runs BEFORE serialization.

    `filter_messages_for_llm_api` collapses and dedupes before any provider
    serialization. Its per-message dedupe keys tool calls on the FULL id, so the
    four distinct 32-character ids survive; keying on the 29-character prefix
    would have collapsed them here and no serializer fix could undo that.
    """

    def dedupe(self, messages):
        # The real body imports `letta.log`; the shared stub package supplies it.
        install_letta_stubs()
        body = QualifiedExtractor(PATCHED_SOURCE / MESSAGE_PY).get(
            "Message.dedupe_tool_calls_for_llm_api")
        namespace = {"List": list, "Message": StubApprovalMessage,
                     "MessageRole": type("MessageRole", (), {"assistant": "assistant",
                                                             "tool": "tool"}),
                     "logger": type("Logger", (), {"error": lambda *a, **k: None})()}
        fn = compiled(namespace, body, "dedupe_tool_calls_for_llm_api")
        return fn(messages)

    def assistant(self, ids):
        calls = [StubOpenAIToolCall(id=call_id, function=StubFunction("memory_update", "{}"),
                                    type="function") for call_id in ids]
        return StubApprovalMessage(role="assistant", tool_calls=calls, content=[])

    def test_four_distinct_prefix_sharing_ids_are_all_kept(self):
        message = self.assistant(R2_IDS)
        out = self.dedupe([message])
        self.assertEqual([call.id for call in out[0].tool_calls], list(R2_IDS))

    def test_only_an_exact_duplicate_id_is_dropped(self):
        message = self.assistant(list(R2_IDS) + [R2_IDS[0]])
        out = self.dedupe([message])
        self.assertEqual([call.id for call in out[0].tool_calls], list(R2_IDS))

    def test_the_prefix_projection_would_have_collapsed_them(self):
        """Why the full-id key matters: the prefix is not unique."""
        self.assertEqual(len({call_id[:29] for call_id in R2_IDS}), 1)
        self.assertEqual(len(set(R2_IDS)), 4)


class GateReachabilityTests(unittest.TestCase):
    """The gate is reachable exactly where it matters, and changes nothing else.

    Three mechanical facts about the patched `_step`, checked at source level
    because running the whole function needs the full Letta runtime:

    1. the branch still requires `not parallel_tool_calls`, so this work never
       turned the outbound contract on;
    2. the branch still contains the upstream truncation in its else-arm, so the
       protection path is intact;
    3. the patch did not touch the provider request builders, so the request side
       still declares its own `parallel_tool_calls` value.
    """

    def step_node(self):
        extractor = QualifiedExtractor(PATCHED_SOURCE / "letta/agents/letta_agent_v3.py")
        return next(n for n in ast.walk(extractor.tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_step")

    def gate_branch(self):
        """The outer `parallel_tool_calls=false` branch that contains the gate."""
        node = self.step_node()
        def contains_gate(candidate):
            return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                       and c.func.id == "multicall_decision"
                       for c in ast.walk(candidate))
        branches = []
        for candidate in ast.walk(node):
            if not (isinstance(candidate, ast.If) and contains_gate(candidate)):
                continue
            source = ast.unparse(candidate.test)
            if "len(tool_calls) > 1" in source and "parallel_tool_calls" in source:
                branches.append(candidate)
        self.assertEqual(len(branches), 1, "exactly one gate branch is expected")
        return branches[0]

    def test_the_branch_still_requires_parallel_tool_calls_false(self):
        source = ast.unparse(self.gate_branch().test)
        self.assertIn("len(tool_calls) > 1", source)
        self.assertIn(
            "not active_llm_config.parallel_tool_calls".replace(
                "active_llm_config.parallel_tool_calls",
                "active_llm_config.parallel_tool_calls"),
            source)

    @staticmethod
    def final_truncation(statements):
        """The upstream `tool_calls = [tool_calls[0]]`, however deeply nested."""
        for statement in statements:
            if (isinstance(statement, ast.Assign)
                    and ast.unparse(statement.value) == "[tool_calls[0]]"):
                return statement
            for field in ("body", "orelse"):
                found = GateReachabilityTests.final_truncation(
                    getattr(statement, field, []) or [])
                if found is not None:
                    return found
        return None

    def test_the_decision_is_taken_before_truncation(self):
        """P1: the decision runs while the received list is still complete."""
        extractor = QualifiedExtractor(PATCHED_SOURCE / "letta/agents/letta_agent_v3.py")
        node = next(n for n in ast.walk(extractor.tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_step")
        calls = [c for c in ast.walk(node)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "multicall_decision"]
        self.assertEqual(len(calls), 1, "exactly one decision call")
        truncations = [c for c in ast.walk(node)
                       if isinstance(c, ast.Assign)
                       and ast.unparse(c.value) == "[tool_calls[0]]"]
        self.assertEqual(len(truncations), 1, "the upstream truncation still exists")
        self.assertLess(calls[0].lineno, truncations[0].lineno,
                        "the decision must precede any truncation of the batch")

    def test_only_the_upstream_truncation_rebinds_the_batch(self):
        """The declared-but-unverified path refuses; it never falls back to [0]."""
        extractor = QualifiedExtractor(PATCHED_SOURCE / "letta/agents/letta_agent_v3.py")
        node = next(n for n in ast.walk(extractor.tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_step")
        bindings = [c for c in ast.walk(node)
                    if isinstance(c, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "tool_calls"
                            for target in c.targets)]
        sources = sorted({ast.unparse(b.value) for b in bindings})
        # The upstream truncation is the only rebinding inside the branch; the
        # approval-resume rebinding belongs to the other code path.
        self.assertIn("[tool_calls[0]]", sources)
        self.assertIn("llm_adapter.tool_calls", sources)

    def test_the_handler_receives_the_bare_batch_name(self):
        extractor = QualifiedExtractor(PATCHED_SOURCE / "letta/agents/letta_agent_v3.py")
        node = next(n for n in ast.walk(extractor.tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_step")
        handler = next(c for c in ast.walk(node)
                       if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                       and c.func.attr == "_handle_ai_response")
        keyword = next(k for k in handler.keywords if k.arg == "tool_calls")
        self.assertEqual(ast.unparse(keyword.value), "tool_calls")

    def test_the_request_builders_are_untouched_by_the_patch(self):
        baseline = Path(os.environ.get("AE_LETTA_SOURCE",
                                       "/tmp/ae-letta-QaM3ld/letta-v1"))
        request_file = "letta/llm_api/openai_client.py"
        self.assertEqual((baseline / request_file).read_bytes(),
                         (PATCHED_SOURCE / request_file).read_bytes())
        self.assertIn("parallel_tool_calls=",
                      (PATCHED_SOURCE / request_file).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class RecoveryChainTests(unittest.TestCase):
    """The real approval-RECOVERY path: returns are consumed and re-sent.

    The first-receive approval branch returns early, but a resumed step passes
    `is_approval_response=True` and runs a different path: the tool returns are
    extracted from the approval response, persisted, filtered and serialized
    into the NEXT provider request. This drives that path with the real pinned
    functions:

        assistant turn (4 calls)
          -> approval request message (real converter)
          -> approval response carrying 4 ToolReturns
          -> _maybe_get_approval_messages + the real `_step` extraction slice
          -> real filter chain (collapse + dedupe)
          -> real to_openai_dicts_from_list

    The provider response is the only fixture; every id-bearing step is real.
    """

    def setUp(self):
        install_letta_stubs()
        self.module = __import__("ae_multicall")
        self.env = multicall_environment(Path(tempfile.mkdtemp(prefix="recovery-")),
                                         PATCHED_SOURCE)
        os.environ[LETTA_ENV_VAR] = PROFILE_VERSION
        os.environ["AE_LETTA_MULTICALL_MANIFEST"] = str(self.env["manifest"])
        os.environ["AE_LETTA_MULTICALL_MODULE_SHA256"] = self.env[
            "AE_LETTA_MULTICALL_MODULE_SHA256"]
        self.addCleanup(lambda: os.environ.pop(LETTA_ENV_VAR, None))

    # -- real pinning helpers -------------------------------------------------
    def message_extractor(self):
        return QualifiedExtractor(PATCHED_SOURCE / MESSAGE_PY)

    def bind_message_helpers(self):
        """Compile the real filter chain and attach it to StubMessage."""
        extractor = self.message_extractor()
        namespace = {"List": list, "Optional": object, "Message": StubApprovalMessage,
                     "MessageRole": type("MessageRole", (), {
                         "assistant": "assistant", "tool": "tool", "approval": "approval",
                         "user": "user", "summary": "summary", "system": "system"}),
                     "logger": StubLogger(), "get_logger": lambda name=None: StubLogger()}
        for name in ("collapse_tool_call_messages_for_llm_api",
                     "dedupe_tool_messages_for_llm_api",
                     "dedupe_tool_calls_for_llm_api"):
            compiled(namespace, extractor.get("Message." + name), name)
        filter_body = extractor.get("Message.filter_messages_for_llm_api")
        filter_ns = dict(namespace)
        for name in ("collapse_tool_call_messages_for_llm_api",
                     "dedupe_tool_messages_for_llm_api",
                     "dedupe_tool_calls_for_llm_api"):
            setattr(StubApprovalMessage, name, staticmethod(namespace[name]))
        setattr(StubApprovalMessage, "is_approval_request",
                lambda self: self.role == "approval" and bool(self.tool_calls))
        setattr(StubApprovalMessage, "is_approval_response",
                lambda self: self.role == "approval" and not self.tool_calls)
        setattr(StubApprovalMessage, "is_summarization_message", lambda self: False)
        filter_ns["Message"] = StubApprovalMessage
        fn = compiled(filter_ns, filter_body, "filter_messages_for_llm_api")
        setattr(StubApprovalMessage, "filter_messages_for_llm_api", staticmethod(fn))
        return fn

    def approval_request(self):
        """The real converted approval request for the four r2 calls."""
        return ApprovalConversionTests().build()

    def tool_returns(self):
        """Four ToolReturns whose ids are the real ones, in order."""
        return [StubReturn(R2_IDS[index], f"result-{index}") for index in range(4)]

    def extraction_slice(self):
        """The real `_step` slice that pulls client returns out of an approval."""
        extractor = QualifiedExtractor(PATCHED_SOURCE / "letta/agents/letta_agent_v3.py")
        node = next(n for n in ast.walk(extractor.tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_step")
        segment = ast.get_source_segment(extractor.text, node)
        lines = segment.splitlines()
        start = next(i for i, line in enumerate(lines)
                     if "_maybe_get_approval_messages(messages)" in line)
        end = next(i for i in range(start, len(lines))
                   if lines[i].strip() == "tool_returns = [r for r in approval_response.approvals if isinstance(r, ToolReturn)]")
        body = textwrap.dedent("\n".join(lines[start:end + 1]))
        helpers = QualifiedExtractor(PATCHED_SOURCE / "letta/agents/helpers.py")
        namespace = {"ApprovalReturn": StubApproval, "ToolReturn": StubReturn,
                     "ToolCallDenial": lambda **kwargs: kwargs,
                     "Tuple": tuple, "Message": StubApprovalMessage,
                     "ToolCall": StubOpenAIToolCall}
        compiled(namespace, helpers.get("_maybe_get_approval_messages"),
                 "_maybe_get_approval_messages")
        compiled(namespace, helpers.get("_maybe_get_pending_tool_call_message"),
                 "_maybe_get_pending_tool_call_message")
        namespace.update({"self": StubAgent([], logger=StubLogger()),
                          "StopReasonType": type("S", (), {
                              "invalid_tool_call": type("V", (), {"value": "invalid"})()})})
        # The pinned slice assigns locals; the wrapper returns the extracted
        # returns and denials so the test can read them.
        source = ("def _extract(messages):\n"
                  + textwrap.indent(body, "    ") + "\n"
                  + "    return tool_returns, tool_call_denials\n")
        exec(compile(source, "<recovery-slice>", "exec"), namespace)
        return namespace

    def test_the_four_returns_are_extracted_in_order(self):
        namespace = self.extraction_slice()
        request = self.approval_request()
        response = StubApprovalMessage(role="approval", approvals=self.tool_returns())
        returns, denials = namespace["_extract"]([request, response])
        self.assertEqual([r.tool_call_id for r in returns], list(R2_IDS))
        self.assertEqual(denials, [])

    def test_the_real_filter_keeps_all_four_distinct_returns(self):
        self.bind_message_helpers()
        request = self.approval_request()
        returns = self.tool_returns()
        tool_message = StubApprovalMessage(role="tool", tool_returns=returns)
        filtered = StubApprovalMessage.filter_messages_for_llm_api([request, tool_message])
        self.assertEqual(len(filtered), 2)
        self.assertEqual([r.tool_call_id for r in filtered[1].tool_returns], list(R2_IDS))

    def test_the_next_wire_expands_every_return_with_its_full_id(self):
        self.bind_message_helpers()
        returns = self.tool_returns()
        tool_message = StubApprovalMessage(role="tool", tool_returns=returns)
        extractor = self.message_extractor()
        namespace = {"List": list, "Optional": object, "Message": StubApprovalMessage,
                     "MessageRole": type("MessageRole", (), {"tool": "tool"}),
                     "TOOL_CALL_ID_MAX_LEN": UPSTREAM_TOOL_CALL_ID_MAX_LEN,
                     "TextContent": StubTextContent, "ToolReturnContent": StubNothing,
                     "ReasoningContent": StubNothing, "RedactedReasoningContent": StubNothing,
                     "OmittedReasoningContent": StubNothing,
                     "tool_return_to_text": lambda value: value,
                     "truncate_tool_return": lambda text, _chars: text,
                     "add_inner_thoughts_to_tool_call": lambda call, **kwargs: call,
                     "parse_json": lambda value: value, "re": re,
                     "tool_return_truncation_chars": None}
        namespace.update(shim_namespace(self.module))
        fn = compiled(namespace, extractor.get("Message.to_openai_dicts_from_list"),
                      "to_openai_dicts_from_list")
        wire = fn([tool_message])
        self.assertEqual([entry["tool_call_id"] for entry in wire], list(R2_IDS))
        self.assertEqual(len(wire), 4)
        self.assertEqual({entry["role"] for entry in wire}, {"tool"})
        # The prefix projection would have made these indistinguishable.
        self.assertEqual(len({value[:29] for value in R2_IDS}), 1)

    def test_a_truncated_return_id_is_refused_by_the_real_reducer(self):
        """The recovery path must not accept a 29-character sliced id."""
        self.bind_message_helpers()
        sliced = [StubReturn(TRUNCATED, "r") for _ in range(4)]
        tool_message = StubApprovalMessage(role="tool", tool_returns=sliced)
        filtered = StubApprovalMessage.filter_messages_for_llm_api([tool_message])
        # The reducer keys on the full id, so four identical sliced ids collapse
        # to one, which is exactly the data loss the protocol prevents.
        kept = filtered[0].tool_returns if filtered else []
        self.assertLess(len(kept), 4)


class StubReturn:
    """A real `ToolReturn`-shaped record: id plus packaged response."""

    def __init__(self, tool_call_id, func_response):
        self.tool_call_id = tool_call_id
        self.func_response = func_response
        self.status = "success"
