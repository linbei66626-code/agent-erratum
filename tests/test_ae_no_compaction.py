"""Targeted tests for the AE no-compaction + capacity contract (offline).

These tests are honest about what runs for real and what is stubbed:

* `compact_messages` is the REAL function from the REAL patched Letta file, executed
  from source with only the summariser functions it would CALL stubbed out. So the
  "zero summariser calls" assertion is a statement about the production code path,
  and the guard is proven to sit before every summariser call.
* the request-time capacity gate is the REAL `LettaAgentV3._ae_capacity_gate` method
  from the same file, called on an instance built with `__new__` (the service's heavy
  constructor needs the full Letta dependency set, which is not installed here), with
  the token counter and the agent state supplied by the test.
* the driver-side decisions (config schema, tags, run refusal) are the production
  functions themselves, and the mid-run stop is a REAL offline chain.

The full Letta package is NOT importable in this offline environment (it needs
`cryptography`, `fastapi`, a database driver, ...). That is a stated gap, not a pass:
it means "the service really loaded this patch" is only supported by the built
checkout's digests plus the deployment receipt, never by these tests.
"""
from __future__ import annotations

from copy import deepcopy
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import ae_cloud_re_multiturn as mt  # noqa: E402
from ae_cloud_re_multiturn import (NO_COMPACTION_POLICY, NO_COMPACTION_TAG,  # noqa: E402
                                   SCHEMA_VERSION_CAPACITY, validate_config)

#: The checkout the no-compaction patch builds: the multicall-patched pinned Letta
#: plus this round's patch and helper.
CHECKOUT = Path(os.environ.get("AE_LETTA_NO_COMPACTION_SOURCE",
                               ROOT / ".ae-verify-src/letta-no-compaction-patch/letta-v1"))
COMPACT = CHECKOUT / "letta/services/summarizer/compact.py"
AGENT = CHECKOUT / "letta/agents/letta_agent_v3.py"
HELPER = CHECKOUT / "letta/helpers/ae_no_compaction.py"
#: The pinned wire-request schema and the client that builds from it.
SCHEMA = CHECKOUT / "letta/schemas/openai/chat_completion_request.py"
BUILDER = CHECKOUT / "letta/llm_api/openai_client.py"


def _record_class(name):
    """A kwargs-accepting stand-in for a pinned pydantic schema."""
    class _Record:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def __repr__(self):
            return f"{name}({sorted(self.__dict__)})"

    _Record.__name__ = name
    return _Record


def _module_from(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """A stand-in for a summariser: records that it was asked to run."""

    def __init__(self, result):
        self.calls = []
        self.result = result

    async def __call__(self, *args, **kwargs):
        self.calls.append({"args": len(args), "kwargs": sorted(kwargs)})
        return self.result


def _llm_config():
    """A minimal stand-in for the pinned LLMConfig the summariser builder reads."""
    return types.SimpleNamespace(handle="vllm/Qwen/Qwen3-30B-A3B-Instruct-2507",
                                 model="Qwen/Qwen3-30B-A3B-Instruct-2507",
                                 provider_name="vllm", model_endpoint_type="openai",
                                 context_window=262144,
                                 max_tokens=2048, temperature=0, model_settings=None,
                                 model_endpoint="http://127.0.0.1:8000/v1")


def load_helper():
    """The real helper module, loaded once per process so its types stay identical."""
    existing = sys.modules.get("ae_no_compaction_under_test")
    if existing is not None:
        return existing
    return _module_from("ae_no_compaction_under_test", HELPER)


_STUBS = {}
_STUBS_CONFIGURED = False


def _compact_stub_modules():
    """The stub module table, configured once per process.

    Configuration is guarded by its own flag, NOT by the table being non-empty: the
    table is filled before the attribute wiring finishes, and later calls must never
    wipe an earlier call's stubs.
    """
    global _STUBS_CONFIGURED
    if _STUBS_CONFIGURED:
        return _STUBS
    stubs = {}
    # Registered shallowest-first with a valid __spec__: `from letta.x.y import z`
    # makes the import system resolve the parent packages, and a module with no
    # __spec__ is reported as "unknown location" instead of being used.
    for name in ("letta", "letta.errors", "letta.helpers", "letta.helpers.message_helper",
                 "letta.llm_api", "letta.llm_api.llm_client", "letta.log",
                 "letta.otel", "letta.otel.tracing", "letta.schemas", "letta.schemas.agent",
                 "letta.schemas.enums", "letta.schemas.letta_message_content",
                 "letta.schemas.llm_config", "letta.schemas.message",
                 "letta.schemas.provider_trace", "letta.schemas.user",
                 "letta.helpers.ae_no_compaction",
                 "letta.services", "letta.services.summarizer",
                 "letta.services.summarizer.self_summarizer",
                 "letta.services.summarizer.summarizer_all",
                 "letta.services.summarizer.summarizer_config",
                 "letta.services.summarizer.summarizer_sliding_window",
                 "letta.services.telemetry_manager", "letta.system"):
        module = types.ModuleType(name)
        module.__spec__ = importlib.util.spec_from_loader(name, loader=None)
        module.__path__ = []
        stubs[name] = module
        sys.modules[name] = module
    sys.modules["letta.helpers.ae_no_compaction"] = load_helper()
    # The r3 agent gate imports the shared counting entry from the helper module the
    # staged patch carries. The test supplies the project's own identical function;
    # `tools/check_tokenizer_drift.py` proves the two sections are byte-identical.
    import ae_multiturn_capacity
    tokenizer_module = types.ModuleType("letta.helpers.ae_qwen_tokenizer")
    tokenizer_module.__spec__ = importlib.util.spec_from_loader(
        "letta.helpers.ae_qwen_tokenizer", loader=None)
    tokenizer_module.__path__ = []
    tokenizer_module.count_request_prompt = ae_multiturn_capacity.count_request_prompt
    stubs["letta.helpers.ae_qwen_tokenizer"] = tokenizer_module
    sys.modules["letta.helpers.ae_qwen_tokenizer"] = tokenizer_module
    sys.modules["letta.errors"].ContextWindowExceededError = type(
        "ContextWindowExceededError", (Exception,), {})
    sys.modules["letta.helpers.message_helper"].convert_message_creates_to_messages = \
        lambda *a, **k: []
    sys.modules["letta.llm_api.llm_client"].LLMClient = _record_class("LLMClient")
    logger = types.SimpleNamespace(warning=lambda *a, **k: None,
                                   info=lambda *a, **k: None,
                                   error=lambda *a, **k: None)
    sys.modules["letta.log"].get_logger = lambda *a, **k: logger
    sys.modules["letta.otel.tracing"].trace_method = lambda fn: fn
    sys.modules["letta.schemas.agent"].AgentType = type("AgentType", (), {})
    sys.modules["letta.schemas.enums"].MessageRole = type(
        "MessageRole", (), {"summary": "summary", "user": "user",
                            "assistant": "assistant", "system": "system",
                            "tool": "tool"})
    sys.modules["letta.schemas.letta_message_content"].TextContent = _record_class(
        "TextContent")
    sys.modules["letta.schemas.llm_config"].LLMConfig = _record_class("LLMConfig")
    sys.modules["letta.schemas.message"].Message = _record_class("Message")
    sys.modules["letta.schemas.message"].MessageCreate = _record_class("MessageCreate")
    sys.modules["letta.schemas.provider_trace"].BillingContext = _record_class("BillingContext")
    sys.modules["letta.schemas.user"].User = _record_class("User")
    sys.modules["letta.services.telemetry_manager"].TelemetryManager = _record_class("TelemetryManager")

    class _CompactionSettings:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.mode = kwargs.get("mode", "sliding_window")
            self.prompt = kwargs.get("prompt")
            self.model = kwargs.get("model")

        def model_copy(self, update=None):
            data = dict(self.__dict__)
            data.update(update or {})
            return _CompactionSettings(**data)

    sys.modules["letta.services.summarizer.summarizer_config"].CompactionSettings = \
        _CompactionSettings
    _STUBS.update(stubs)
    sys.modules["letta.services.summarizer.summarizer_config"].get_default_prompt_for_mode = \
        lambda mode: f"prompt:{mode}"
    sys.modules["letta.services.summarizer.summarizer_config"].get_default_summarizer_model = \
        lambda provider: None
    sliding = sys.modules["letta.services.summarizer.summarizer_sliding_window"]

    async def _count(*args, **kwargs):
        return 0

    sliding.count_tokens = _count
    sliding.count_tokens_with_tools = _count
    # Default no-op summarisers: the guard test replaces them with a recorder, and a
    # test that runs on its own must still see a complete module (an absent attribute
    # would be an ImportError in the module under test, not a meaningful result).
    async def _noop(*args, **kwargs):
        # The sliding-window path unpacks exactly two values.
        return ({"id": "summary"}, [])

    for module_name, attribute in (
            ("letta.services.summarizer.summarizer_all", "summarize_all"),
            ("letta.services.summarizer.summarizer_sliding_window",
             "summarize_via_sliding_window"),
            ("letta.services.summarizer.self_summarizer", "self_summarize_all"),
            ("letta.services.summarizer.self_summarizer", "self_summarize_sliding_window")):
        setattr(sys.modules[module_name], attribute, _noop)
    _STUBS_CONFIGURED = True
    return stubs


def load_compact(monkeypatch_targets):
    """Execute the REAL compact.py with its imported collaborators stubbed.

    Only the names the module imports are replaced; the module body - including the
    injected guard - is the reviewed file's own source. The stub table is created
    once and reused, so a later call cannot wipe an earlier call's stubs.
    """
    stubs = _compact_stub_modules()
    for target in monkeypatch_targets:
        name, attribute = target.rsplit(".", 1)
        setattr(stubs[name], attribute, monkeypatch_targets[target])
    sys.modules["letta.system"].package_summarize_message_no_counts = lambda *a, **k: "packed"
    # The module is imported afresh on every call so the stub table is bound FIRST:
    # the patched file's names are bound once, at import.
    return _module_from("ae_compact_under_test", COMPACT)


def _official_tokenizer():
    """The official-asset tokenizer, loaded once for the counting assertions."""
    global _TOKENIZER
    if _TOKENIZER is None:
        from ae_multiturn_capacity import QwenBpeTokenizer, load_official_tokenizer_assets
        assets = load_official_tokenizer_assets()
        _TOKENIZER = QwenBpeTokenizer(assets["tokenizer_json"], assets["tokenizer_config"])
    return _TOKENIZER


_TOKENIZER = None
class _StubCleanupMixin:
    """Remove the fabricated `letta.*` entries this module puts in `sys.modules`.

    The stub table replaces real modules for the duration of a test; leaving it in
    place would make a LATER test import a stub instead of the checkout, so every
    test class here clears them on entry and exit.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(_drop_stub_modules)

    def tearDown(self):
        _drop_stub_modules()
        super().tearDown()


def _drop_stub_modules():
    """Drop the stub-only `letta.*` modules; keep anything loaded from a real file."""
    global _STUBS_CONFIGURED
    for name, module in list(sys.modules.items()):
        if name != "letta" and not name.startswith("letta."):
            continue
        if getattr(module, "__file__", None) is None:
            sys.modules.pop(name, None)
    _STUBS.clear()
    _STUBS_CONFIGURED = False


def _llm_config():
    """A minimal stand-in for the pinned LLMConfig the summariser builder reads."""
    return types.SimpleNamespace(handle="vllm/Qwen/Qwen3-30B-A3B-Instruct-2507",
                                 model="Qwen/Qwen3-30B-A3B-Instruct-2507",
                                 provider_name="vllm", model_endpoint_type="openai",
                                 context_window=262144, max_tokens=2048,
                                 temperature=0, model_settings=None,
                                 model_endpoint="http://127.0.0.1:8000/v1")


class NoCompactionGuardTests(_StubCleanupMixin, unittest.TestCase):
    """The guard, at the one place a compaction can reach a summariser."""

    def setUp(self):
        super().setUp()
        self.helper = load_helper()
        self.tag = self.helper.NO_COMPACTION_TAG

    def test_the_policy_tag_is_the_one_the_driver_sends(self):
        self.assertEqual(self.tag, NO_COMPACTION_TAG)
        self.assertEqual(self.tag, "ae-no-compaction:1")
        self.assertEqual(self.helper.NO_COMPACTION_POLICY, NO_COMPACTION_POLICY)

    def test_an_agent_without_the_tag_is_not_affected(self):
        self.assertFalse(self.helper.no_compaction_declared([]))
        self.assertFalse(self.helper.no_compaction_declared(["other-tag"]))
        self.helper.require_no_compaction([], "test")  # must not raise

    def test_the_policy_state_has_exactly_three_outcomes(self):
        self.assertEqual(self.helper.policy_state([])["state"], "off")
        self.assertEqual(self.helper.policy_state([self.tag])["state"], "on")
        self.assertEqual(self.helper.policy_state(["ae-no-compaction:999"])["state"],
                         "unknown")

    def test_the_declared_policy_refuses_before_calling_a_summariser(self):
        """Zero summariser calls, and the in-context set unchanged."""
        recorder = _Recorder(("summary-message", ["compacted"]))
        compact = load_compact({
            "letta.services.summarizer.summarizer_all.summarize_all": recorder,
            "letta.services.summarizer.summarizer_sliding_window.summarize_via_sliding_window":
                recorder,
            "letta.services.summarizer.self_summarizer.self_summarize_all": recorder,
            "letta.services.summarizer.self_summarizer.self_summarize_sliding_window": recorder,
        })
        messages = [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]
        frozen = deepcopy(messages)
        with self.assertRaises(self.helper.NoCompactionPolicyError) as raised:
            asyncio.run(compact.compact_messages(
                actor=None, agent_id="agent", agent_llm_config=None,
                telemetry_manager=None, llm_client=None, agent_type=None,
                messages=messages, timezone="UTC",
                compaction_settings=None, agent_tags=[self.tag]))
        self.assertEqual(recorder.calls, [], "a summariser was called under the policy")
        self.assertEqual(messages, frozen, "the in-context message set was modified")
        self.assertIn("declared policy reached compact_messages", str(raised.exception))

    def test_the_untagged_path_keeps_the_upstream_behaviour(self):
        """No tag: the summariser runs and its compacted set is returned."""
        recorder = _Recorder(("summary-message", ["compacted"]))
        compact = load_compact({
            "letta.services.summarizer.summarizer_sliding_window.summarize_via_sliding_window":
                recorder,
            "letta.services.summarizer.summarizer_all.summarize_all": recorder,
        })
        messages = [{"id": "m1"}, {"id": "m2"}]
        result = asyncio.run(compact.compact_messages(
            actor=None, agent_id="agent", agent_llm_config=_llm_config(),
            telemetry_manager=None, llm_client=None, agent_type="letta_v1_agent",
            messages=messages, timezone="UTC", compaction_settings=None,
            agent_tags=["unrelated"]))
        self.assertEqual(len(recorder.calls), 1, "the untagged path stopped calling the summariser")
        self.assertEqual(result.compacted_messages[0], "compacted")

    def test_the_guard_is_inserted_before_every_summariser_call(self):
        """Structural check of the REAL patched file, not of a fixture."""
        import ast
        source = COMPACT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "compact_messages")
        lines = [node.lineno for node in ast.walk(function) if isinstance(node, ast.Call)]
        guard = [node.lineno for node in ast.walk(function)
                 if isinstance(node, ast.Call)
                 and getattr(node.func, "id", None) == "require_no_compaction"]
        self.assertEqual(len(guard), 1, "the guard call is not exactly once")
        self.assertTrue(all(line > guard[0] for line in lines if line != guard[0]),
                        "a call precedes the guard inside compact_messages")


class CapacityGateTests(_StubCleanupMixin, unittest.TestCase):
    """The gate decision: real decision function, real role reserves."""

    def setUp(self):
        super().setUp()
        self.helper = load_helper()
        self.tag = [NO_COMPACTION_TAG]

    def test_within_capacity_is_allowed(self):
        decision = self.helper.capacity_gate_decision(
            agent_tags=self.tag, prompt_tokens=61494, reserve_tokens=2048,
            context_window=262144, count_source="official_qwen_tokenizer")
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "within_capacity")
        self.assertEqual(decision["input_tokens"], 61494)
        self.assertEqual(decision["count_source"], "official_qwen_tokenizer")

    def test_input_plus_reserve_over_capacity_is_refused(self):
        decision = self.helper.capacity_gate_decision(
            agent_tags=self.tag, prompt_tokens=65536, reserve_tokens=2048,
            context_window=65536, count_source="official_qwen_tokenizer")
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "input_plus_reserve_exceeds_capacity")
        self.assertEqual(decision["over_by"], 2048)

    def test_exactly_at_capacity_is_allowed_and_one_token_more_is_not(self):
        exact = self.helper.capacity_gate_decision(
            agent_tags=self.tag, prompt_tokens=65536 - 2048, reserve_tokens=2048,
            context_window=65536, count_source="official_qwen_tokenizer")
        self.assertTrue(exact["allowed"])
        over = self.helper.capacity_gate_decision(
            agent_tags=self.tag, prompt_tokens=65536 - 2047, reserve_tokens=2048,
            context_window=65536, count_source="official_qwen_tokenizer")
        self.assertFalse(over["allowed"])

    def test_a_decision_without_a_declared_count_source_is_refused(self):
        """R3-4: an undeclared basis is a refusal, not an authorisation."""
        for undeclared in (None, "", "   "):
            decision = self.helper.capacity_gate_decision(
                agent_tags=self.tag, prompt_tokens=10, reserve_tokens=2048,
                context_window=65536, count_source=undeclared)
            self.assertFalse(decision["allowed"], undeclared)
            self.assertEqual(decision["reason"], "count_source_undeclared")

    def test_no_count_basis_is_refused(self):
        decision = self.helper.capacity_gate_decision(
            agent_tags=self.tag, prompt_tokens=None, reserve_tokens=2048,
            context_window=65536)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "no_trustworthy_count_basis")

    def test_an_unknown_policy_version_is_refused_not_ignored(self):
        decision = self.helper.capacity_gate_decision(
            agent_tags=["ae-no-compaction:999"], prompt_tokens=10,
            reserve_tokens=2048, context_window=65536)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "unknown_policy_version")

    def test_an_agent_without_the_policy_is_not_gated(self):
        decision = self.helper.capacity_gate_decision(
            agent_tags=[], prompt_tokens=10 ** 6, reserve_tokens=2048,
            context_window=65536)
        self.assertTrue(decision["allowed"])
        self.assertFalse(decision["active"])

    def test_the_digit_string_counterexample_cannot_slip_through(self):
        """10000 bytes ARE 10000 official tokens; a ratio would have said 4167."""
        payload = "0123456789" * 1000
        self.assertEqual(len(payload.encode("utf-8")), 10000)
        self.assertEqual(_official_tokenizer().count(payload), 10000)
        self.assertFalse(hasattr(self.helper, "byte_bound_token_estimate"),
                         "the byte-ratio fallback must be gone")
        decision = self.helper.capacity_gate_decision(
            agent_tags=self.tag, prompt_tokens=None, reserve_tokens=2048,
            context_window=65536)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "no_trustworthy_count_basis")


class InAgentCapacityGateTests(_StubCleanupMixin, unittest.TestCase):
    """The service-side gate, on the REAL method body and the REAL request shape."""

    def test_a_normal_request_is_allowed_and_counted_officially(self):
        method, states = load_agent_capacity_gate()
        built = _built_request()
        decision = asyncio.run(method(states, built))
        self.assertTrue(decision["allowed"], decision)
        # The label names the basis AND the revision of the assets it counted with.
        self.assertTrue(decision["count_source"].startswith("official_qwen_tokenizer"), decision)
        self.assertEqual(decision["count_source"], "official_qwen_tokenizer:fixture")
        from ae_multiturn_capacity import count_request_prompt
        self.assertEqual(decision["input_tokens"],
                         count_request_prompt(_official_tokenizer(), built["messages"],
                                              built.get("tools") or [])["prompt_tokens"])
        self.assertEqual(decision["reserve_tokens"], 2048)

    def test_a_request_over_the_boundary_is_refused(self):
        method, states = load_agent_capacity_gate(window=1000)
        decision = asyncio.run(method(states, _built_request()))
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "input_plus_reserve_exceeds_capacity")

    def test_a_request_with_no_count_basis_is_refused(self):
        method, states = load_agent_capacity_gate(official=False, pinned=None)
        decision = asyncio.run(method(states, _built_request()))
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "no_trustworthy_count_basis")

    def test_an_unknown_policy_version_is_refused_at_the_send_boundary(self):
        method, states = load_agent_capacity_gate()
        states.agent_state.tags = ["ae-no-compaction:999"]
        decision = asyncio.run(method(states, _built_request()))
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "unknown_policy_version")

    def test_the_pinned_counter_is_only_used_when_it_counts_more(self):
        built = _built_request()
        # The primary count is the SHARED entry's rendered-prompt count, not the raw
        # message text: the pinned counter is compared against that number.
        from ae_multiturn_capacity import count_request_prompt
        official = count_request_prompt(_official_tokenizer(), built["messages"],
                                        built["tools"])["prompt_tokens"]
        method, states = load_agent_capacity_gate(pinned=official + 500)
        raised = asyncio.run(method(states, built))
        self.assertEqual(raised["input_tokens"], official + 500)
        # The pinned counter may only TIGHTEN a primary count that already exists,
        # and its label says exactly that.
        self.assertEqual(raised["count_source"], "pinned_token_counter_tightened")
        method2, states2 = load_agent_capacity_gate(pinned=max(0, official - 500))
        kept = asyncio.run(method2(states2, built))
        self.assertEqual(kept["input_tokens"], official)
        self.assertEqual(kept["count_source"], "official_qwen_tokenizer:fixture")

    def test_the_agent_gate_is_wired_before_the_provider_invocation(self):
        """Structural check of the REAL patched agent file."""
        source = AGENT.read_text(encoding="utf-8")
        gate = source.index("_ae_gate = await self._ae_capacity_gate(request_data)")
        invoke = source.index("llm_adapter.invoke_llm(")
        dry_run = source.index("if dry_run:\n                            yield request_data")
        self.assertLess(gate, dry_run)
        self.assertLess(gate, invoke)

    def test_the_gate_reads_a_dict_request_not_an_attribute_object(self):
        """The real `build_request_data` result is a dict; the method must treat it so."""
        source = AGENT.read_text(encoding="utf-8")
        self.assertIn('request_data.get("messages")', source)
        self.assertNotIn("request_data.messages", source)


BYTE_GATE_ENV = {"AE_BYTE_GATE_COUNT_BASIS": "no_token_count_byte_gate_only",
                 "AE_BYTE_GATE_MAX_REQUEST_BYTES": "2097152",
                 "AE_BYTE_GATE_WIRE_MODEL": "deepseek-flash"}


class ModelBoundByteGateTests(_StubCleanupMixin, unittest.TestCase):
    """The service gate's MODEL-SPECIFIC byte branch, on the REAL method body.

    The probe is the reviewed agent file's own `_ae_capacity_gate` (plus its counting and
    byte-policy helpers). The declaration travels exactly as it does on site: three
    `AE_BYTE_GATE_*` environment variables handed to the service process.
    """

    def setUp(self):
        super().setUp()
        self._saved = {name: os.environ.pop(name, None) for name in BYTE_GATE_ENV}
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _declare(self, **overrides):
        for name, value in {**BYTE_GATE_ENV, **overrides}.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def _deepseek_request():
        built = _built_request()
        built["model"] = "deepseek-flash"
        return built

    def test_a_deepseek_request_is_byte_bounded_and_never_tokenised(self):
        self._declare()
        # The tokenizer is deliberately ABSENT: if this request were counted by the Qwen
        # path it could only end in `no_trustworthy_count_basis`. An allowed decision
        # therefore proves the byte branch ran and the token path was never reached.
        method, states = load_agent_capacity_gate(official=False, pinned=None)
        decision = asyncio.run(method(states, self._deepseek_request()))
        self.assertTrue(decision["allowed"], decision)
        self.assertEqual(decision["count_basis"], "no_token_count_byte_gate_only")
        self.assertEqual(decision["count_source"], "no_token_count_byte_gate_only")
        self.assertFalse(decision["capacity_is_a_guarantee"])
        self.assertFalse(decision["token_count_available"])
        self.assertNotIn("input_tokens", decision)
        self.assertGreater(decision["request_bytes"], 0)
        self.assertEqual(decision["byte_budget"], 2097152)
        self.assertEqual(decision["declared_wire_model"], "deepseek-flash")
        self.assertEqual(decision["request_wire_model"], "deepseek-flash")

    def test_another_model_is_not_waved_through_by_the_byte_policy(self):
        self._declare()
        method, states = load_agent_capacity_gate()
        qwen = _built_request()          # the pinned Qwen model
        decision = asyncio.run(method(states, qwen))
        self.assertFalse(decision["allowed"], decision)
        self.assertEqual(decision["reason"], "byte_policy_model_mismatch")
        self.assertEqual(decision["request_wire_model"],
                         "Qwen/Qwen3-30B-A3B-Instruct-2507")
        unknown = _built_request()
        unknown["model"] = "some/unknown-model"
        refused = asyncio.run(method(states, unknown))
        self.assertFalse(refused["allowed"], refused)
        self.assertEqual(refused["reason"], "byte_policy_model_mismatch")

    def test_an_over_budget_request_is_refused(self):
        self._declare(AE_BYTE_GATE_MAX_REQUEST_BYTES="64")
        method, states = load_agent_capacity_gate()
        decision = asyncio.run(method(states, self._deepseek_request()))
        self.assertFalse(decision["allowed"], decision)
        self.assertEqual(decision["reason"], "over_declared_byte_budget")
        self.assertEqual(decision["byte_budget"], 64)

    def test_a_half_declaration_is_refused_not_silently_fallen_back(self):
        self._declare(AE_BYTE_GATE_WIRE_MODEL=None)
        method, states = load_agent_capacity_gate()
        decision = asyncio.run(method(states, self._deepseek_request()))
        self.assertFalse(decision["allowed"], decision)
        self.assertEqual(decision["reason"], "byte_policy_declares_no_model")
        self._declare(AE_BYTE_GATE_MAX_REQUEST_BYTES="not-a-number")
        method, states = load_agent_capacity_gate()
        decision = asyncio.run(method(states, self._deepseek_request()))
        self.assertFalse(decision["allowed"], decision)
        self.assertEqual(decision["reason"], "byte_budget_unavailable")

    def test_without_a_declaration_the_qwen_path_is_exactly_the_old_one(self):
        for name in BYTE_GATE_ENV:
            os.environ.pop(name, None)
        method, states = load_agent_capacity_gate()
        decision = asyncio.run(method(states, _built_request()))
        self.assertTrue(decision["allowed"], decision)
        self.assertTrue(decision["count_source"].startswith("official_qwen_tokenizer"),
                        decision)


def _built_request(messages=None, tools=None):
    """A request in the REAL production shape.

    `LettaAgentV3.step` passes the result of `build_request_data` straight to the
    provider adapter, and that function ends with
    `request_data = data.model_dump(exclude_unset=True)` — a DICT whose `messages` are
    the wire message dictionaries and whose `tools` are the wire tool schemas. The
    dict below is that shape, and `dict_request_contract()` in the patch bundle is
    what derives the shape's field list from the pinned `ChatCompletionRequest`
    schema (the full builder cannot run here: it needs `openai`/`httpx`, which the
    offline environment does not have).
    """
    body = {
        "model": "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "messages": messages if messages is not None else [
            {"role": "system", "content": "你是任务环境中的个人助手。"},
            {"role": "user", "content": "给我点一杯奶茶到店里。"},
        ],
        "tools": tools if tools is not None else [
            {"type": "function", "function": {"name": "pay_delivery_order",
                                              "parameters": {"type": "object", "properties": {}}}},
        ],
        "max_completion_tokens": 2048,
        "temperature": 0,
    }
    return body


def _built_text(built):
    return json.dumps({"messages": built.get("messages") or [],
                       "tools": built.get("tools") or []},
                      ensure_ascii=False, separators=(", ", ": "))


def load_agent_capacity_gate(window=262144, official=True, pinned=None):
    """Extract the REAL `_ae_capacity_gate` method from the patched agent file.

    The agent class cannot be imported offline (the Letta package needs dependencies
    that are not installed), so the method's own source is compiled with a stubbed
    module namespace. Everything inside the method is the reviewed file's code; the
    only substitutions are the two collaborators the test controls: whether the
    official tokenizer loader finds an asset, and what the pinned counter returns.
    """
    import ast
    source = AGENT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ae_capacity_gate":
            method = node
            break
    assert method is not None, "the capacity gate method is absent from the patched agent"
    import textwrap
    snippet = textwrap.dedent(ast.get_source_segment(source, method))
    # The gate's own local import of the pinned counter must resolve to a module the
    # test controls; the stub table provides it (and every other `letta.*` name).
    stubs = _compact_stub_modules()
    module = types.ModuleType("ae_agent_gate_probe")
    module.__dict__.update({
        "ae_helper": load_helper(),
        "os": os,
        "json": json,
        "NoCompactionPolicyError": load_helper().NoCompactionPolicyError,
        "capacity_gate_decision": load_helper().capacity_gate_decision,
        "policy_state": load_helper().policy_state,
        "count_tokens_with_tools": None,
    })
    exec(compile(snippet.replace("async def _ae_capacity_gate", "async def _probe", 1),
                 "<agent-gate>", "exec"), module.__dict__)
    probe = module.__dict__["_probe"]
    # The gate splits counting into a PRIMARY (shared entry over the declared assets)
    # and a PINNED tightening counter, and the model-specific byte policy is consulted
    # BEFORE either. All of them are real methods from the same reviewed file: the test
    # drives the production logic, not a lookalike. `_ae_wire_body` merges the SDK's
    # `extra_body` carrier so the byte bound is measured on the body really sent. With no
    # `AE_BYTE_GATE_*` declaration the byte policy reports itself undeclared, which is
    # exactly the Qwen path below.
    for name in ("_ae_primary_count", "_ae_tokenizer_identity", "_ae_pinned_count",
                 "_ae_byte_gate_policy", "_ae_wire_body", "_ae_request_bytes"):
        node = next((item for item in ast.walk(tree)
                     if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and item.name == name), None)
        assert node is not None, f"{name} is absent from the patched agent"
        exec(compile(textwrap.dedent(ast.get_source_segment(source, node)),
                     "<agent-gate>", "exec"), module.__dict__)
    tokenizer = _official_tokenizer() if official else None
    instance = types.SimpleNamespace(
        agent_state=types.SimpleNamespace(
            tags=[NO_COMPACTION_TAG],
            llm_config=types.SimpleNamespace(context_window=window, max_tokens=2048)),
        logger=types.SimpleNamespace(warning=lambda *a, **k: None,
                                     info=lambda *a, **k: None),
        actor=None,
        _ae_counter_cache=tokenizer,
        _ae_capacity_counter=lambda: tokenizer,
    )

    for name in ("_ae_primary_count", "_ae_tokenizer_identity", "_ae_pinned_count",
                 "_ae_byte_gate_policy", "_ae_wire_body", "_ae_request_bytes"):
        setattr(instance, name, module.__dict__[name].__get__(instance))
    # The counting basis this fixture declares, cached exactly where the real method
    # caches it: target and asset target agree, so `_ae_tokenizer_identity` accepts a
    # production count. The refusal sources are exercised separately, through the
    # environment, by `test_each_tokenizer_identity_refusal_is_reported`.
    instance._ae_identity_cache = {
        "target": "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "asset_target": "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "revision": "fixture",
    }

    async def pinned_counter(**kwargs):
        if pinned is None:
            raise RuntimeError("the pinned counter is not available in this test")
        return pinned

    stubs["letta.services.summarizer.summarizer_sliding_window"].count_tokens_with_tools = \
        pinned_counter
    return probe, instance


class ProxyCapacityGateTests(_StubCleanupMixin, unittest.TestCase):
    """The pre-send gate, on the REAL proxy, for every role that reaches the provider.

    These tests exercise the transport that really sends: the gate runs after the
    request is normalized and BEFORE the wire record, the pacing wait and the upstream
    call, so a refused request produces ZERO upstream sends. The counting basis is the
    official tokenizer over the exact normalized body.
    """

    def setUp(self):
        super().setUp()
        import io
        import tempfile
        self._io = io
        from ae_cloud_proxy import CloudAuditProxy, CloudConfig
        from ae_cloud_proxy import MODEL as PROXY_MODEL, ORIGIN
        from test_ae_cloud_input_audit import KEY
        self.KEY = KEY
        self.PROXY_MODEL = PROXY_MODEL
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "capture.private.jsonl"
        self.config = CloudConfig(PROXY_MODEL, 4096, 10000, 10000, 64, 180.0)
        self.proxy = CloudAuditProxy(self.config, self.log, api_key=KEY)
        self.addCleanup(self.proxy.close)
        self.sent = []
        fixture = self
        tokenizer = _official_tokenizer()
        self.counted = lambda normalized, role: tokenizer.count(
            json.dumps(json.loads(normalized.decode("utf-8")),
                       ensure_ascii=False, separators=(", ", ": ")))

        class Reply(io.BytesIO):
            def __init__(self):
                super().__init__(json.dumps({
                    "model": PROXY_MODEL, "id": "fixture", "system_fingerprint": "fixture",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "fixture"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                              "total_tokens": 12}}, ensure_ascii=False).encode())
                self.code, self.headers = 200, {"x-trace": "fixture"}

        class Opener:
            def open(self, request, timeout):
                fixture.sent.append(request)
                return Reply()

        self.proxy.opener = Opener()

    def _fresh_proxy(self, name="next"):
        """A new proxy over its OWN journal: a blocked proxy stays blocked by design."""
        from ae_cloud_proxy import CloudAuditProxy
        self.log = Path(self.tmp.name) / f"capture-{name}.private.jsonl"
        self.proxy = CloudAuditProxy(self.config, self.log, api_key=self.KEY)
        self.addCleanup(self.proxy.close)
        fixture = self

        class Reply(fixture._io.BytesIO):
            def __init__(self):
                super().__init__(json.dumps({
                    "model": fixture.PROXY_MODEL, "id": "fixture", "system_fingerprint": "fixture",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "fixture"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                              "total_tokens": 12}}, ensure_ascii=False).encode())
                self.code, self.headers = 200, {"x-trace": "fixture"}

        class Opener:
            def open(self, request, timeout):
                fixture.sent.append(request)
                return Reply()

        self.proxy.opener = Opener()
        return self.proxy

    def _arm(self, window, agent=2048, auxiliary=4096, counter=None):
        """Arm exactly as the proxy CLI does: from a candidate DECLARATION."""
        declaration = {"capacity": {
            "context_window": window, "output_reserve_tokens": agent,
            "auxiliary_reserve_tokens": auxiliary,
            "no_compaction": "ae-no-compaction-1",
            "count_basis": ["official_qwen_tokenizer"]}}
        self.proxy.arm_capacity_from_declaration(
            declaration, count_request=counter or self.counted,
            count_source="official_qwen_tokenizer(fixture)")

    def _send(self, content, role="agent_or_unknown"):
        body = {"model": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 2048, "stream": False, "temperature": 0,
                "tools": [{"type": "function",
                           "function": {"name": "pay_delivery_order",
                                        "parameters": {"type": "object", "properties": {}}}}]}
        raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
        return self.proxy.dispatch("POST", "/v1/chat/completions", raw, "re/task", role)

    def test_each_role_is_gated_with_its_own_output_reserve(self):
        self._arm(window=262144)
        for role in ("agent_or_unknown", "user_simulator", "evaluator"):
            self._send("你好", role)
        reserves = [row["output_reserve_tokens"] for row in self.proxy.capacity_checks]
        self.assertEqual(reserves, [2048, 4096, 4096])
        self.assertEqual([row["role"] for row in self.proxy.capacity_checks],
                         ["agent_or_unknown", "user_simulator", "evaluator"])
        self.assertEqual(len(self.sent), 3, "a normal request did not reach the sender")

    def test_an_over_capacity_request_never_reaches_the_sender(self):
        """Each role gets its OWN attempt: a blocked proxy stops, exactly as designed."""
        from ae_model_proxy import ProxyBlocked
        for role in ("agent_or_unknown", "user_simulator", "evaluator"):
            self._fresh_proxy(role)
            self._arm(window=64)
            before = len(self.sent)
            with self.assertRaises(ProxyBlocked) as raised:
                self._send("x" * 200, role)
            self.assertIn("capacity_exceeded_before_send", str(raised.exception))
            self.assertEqual(len(self.sent), before,
                             "an over-capacity request was sent upstream")
            records = [json.loads(line) for line in self.log.read_text().splitlines()]
            self.assertEqual([row["kind"] for row in records
                              if row["kind"] == "upstream_request"], [],
                             "the journal shows an upstream send for a refused request")
            self.assertTrue(all(row["fits"] is False for row in self.proxy.capacity_checks))

    def test_the_boundary_is_this_request_plus_this_role_reserve(self):
        from ae_model_proxy import ProxyBlocked
        # A 64-token window leaves 64 - 4096 < 0 for an auxiliary role: refused.
        self._arm(window=64, agent=32, auxiliary=32)
        with self.assertRaises(ProxyBlocked):
            self._send("x" * 400, "evaluator")
        # The agent role with the same window is governed by ITS OWN reserve.
        self._fresh_proxy("boundary")
        self._arm(window=64, agent=2048, auxiliary=32)
        with self.assertRaises(ProxyBlocked):
            self._send("x" * 400, "agent_or_unknown")
        self.assertEqual(self.proxy.capacity_checks[0]["output_reserve_tokens"], 2048)

    def test_an_unavailable_count_refuses_instead_of_passing(self):
        """A counter that cannot produce an integer refuses; it never passes by default."""
        from ae_model_proxy import ProxyBlocked
        for label, broken in (
                ("raises", lambda normalized, role: (_ for _ in ()).throw(
                    RuntimeError("no counting basis"))),
                ("returns_none", lambda normalized, role: None)):
            self._fresh_proxy(label)
            self._arm(window=262144, counter=broken)
            before = len(self.sent)
            with self.assertRaises(ProxyBlocked):
                self._send("hello")
            self.assertEqual(self.proxy.capacity_checks, [],
                             "an uncounted request must not produce a check record")
            self.assertTrue(self.proxy.blocked)
            self.assertEqual(len(self.sent), before,
                             "an uncounted request was sent upstream")

    def test_an_unarmed_proxy_is_untouched(self):
        """The sealed protocols never arm the gate, so nothing is checked or blocked."""
        self._send("你好", "evaluator")
        self.assertEqual(self.proxy.capacity_checks, [])
        self.assertEqual(len(self.sent), 1)
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([row["kind"] for row in records if row["kind"] == "capacity_check"],
                         [])


class RealChainCapacityTests(unittest.TestCase):
    """The 0.3 candidate through a REAL offline chain.

    Both outcomes are covered: a chain that fits (it must complete and audit as a
    normal run) and a chain whose own history exceeds the declared window (it must
    stop at the first over-capacity request, before the provider is sent anything,
    keeping the phases that already ran).
    """

    def setUp(self):
        import re_multiturn_chain as chain
        self.chain = chain
        import test_ae_cloud_re_multiturn as t
        if t.real_sample(12) is None:
            self.skipTest("the fixed dataset is not available")

    def _config(self, tmp, window, verified=False):
        # Start from the reviewed 0.2 candidate (the fixture's own transport and ID
        # handling are built for it) and move it to the 0.3 capacity contract.
        base = json.loads((ROOT / "configs"
                           / "ae-01__re-multiturn__siliconflow.original-candidate.json")
                          .read_text(encoding="utf-8"))
        candidate = json.loads((ROOT / "configs"
                                / "ae-01__re-multiturn__siliconflow.capacity-256k-nocompaction-candidate.json")
                               .read_text(encoding="utf-8"))
        base["schema_version"] = SCHEMA_VERSION_CAPACITY
        base["multicall_profile"] = candidate["multicall_profile"]
        # The reviewed pacing triple is required by the 900-second run timeout the
        # candidate declares; the fixture's own transport uses it unchanged.
        base["pacing"] = candidate["pacing"]
        base["context_window"] = window
        base["context_window_source"] = "published_native"
        base["capacity"] = json.loads(json.dumps(candidate["capacity"]))
        # The tests run on this machine, where the target model's own tokenizer assets
        # are absent. They therefore declare the family asset as an EXPLICIT
        # exploration, exactly the label the candidate carries, and the RUN-stage
        # refusal for that label is asserted separately.
        base["capacity"]["tokenizer"]["exploration_only"] = True
        base["capacity"]["context_window"] = window
        base["capacity"]["derived_caps"]["native_tool_return_truncation_chars"] = \
            max(5000, int(window * 0.8))
        if verified:
            # A RECORDED offline measurement (the fixture's own declared bound). This
            # is an offline candidate artefact, never a claim about the real endpoint.
            base["capacity"]["service_limit"]["endpoint_measured_tokens"] = window
            base["capacity"]["service_limit"]["endpoint_measurement"] = \
                "offline fixture: declared bound recorded as a measurement"
            base["capacity"]["verification"] = "verified"
        path = Path(tmp) / f"capacity-{window}-{int(verified)}.json"
        path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def _inflated_history(self, sample):
        """Real projected history, made long enough to exceed a small window."""
        for task in sample["tasks"]:
            if task["number"] != 4:
                continue
            task["history"] = [
                {"ref": f"t4/history/{index}",
                 "record": {"date": f"2024-05-{index % 28 + 1:02d}", "behavior": [],
                            "dialogue": [
                                {"role": "user",
                                 "content": f"t4 历史提问 {index} " + "补充说明" * 20},
                                {"role": "assistant",
                                 "content": f"t4 历史回答 {index} " + "解释" * 20}]}}
                for index in range(400)]
        return sample

    def test_a_capacity_chain_that_fits_completes_and_audits(self):
        """The NORMAL 0.3 chain: every counted request fits, so nothing is refused.

        The endpoint capacity is declared VERIFIED with a recorded offline measurement
        so the run may present itself as complete; the unverified case is covered by
        the audit test below, which must refuse that claim.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, config_path=self._config(tmp, 262144, verified=True))
            result = fixture["result"]
            self.assertEqual(result["status"], "RE_MULTITURN_COMPLETED_AUDIT_PENDING",
                             result.get("invalid_reasons"))
            checks = result["capacity_checks"]
            self.assertTrue(checks, "the pre-send gate recorded nothing")
            self.assertTrue(all(row["fits"] for row in checks))
            self.assertEqual(sorted({row["role"] for row in checks}),
                             ["agent_or_unknown", "evaluator", "user_simulator"])
            reserves = {row["role"]: row["output_reserve_tokens"] for row in checks}
            self.assertEqual(reserves["agent_or_unknown"], 2048)
            self.assertEqual(reserves["evaluator"], 4096)
            self.assertEqual(reserves["user_simulator"], 4096)
            # On this machine the counting asset is the Qwen3 family tokenizer, and the
            # label says so: an exploration count is never presented as the target
            # model's own count.
            self.assertTrue(all(row["count_source"].startswith(
                "exploration_qwen_family_asset:") for row in checks), checks[0])
            report = self.chain.audit_of(fixture)
            self.assertTrue(report["input_audit_passed"], report["invalid_reasons"])
            self.assertEqual(report["status"], "VALID")
            policy = report["capacity_policy"]
            self.assertEqual(policy["agent_tags_by_arm"],
                             {"rewrite": [NO_COMPACTION_TAG], "erratum": [NO_COMPACTION_TAG]})
            self.assertEqual(policy["checks"], len(checks))

    def _mutate_journal(self, fixture, mutate):
        """Rewrite the run's journal with one minimal mutation, renumbered."""
        path = Path(fixture["journal"])
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        rows = mutate(rows)
        for index, row in enumerate(rows):
            row["sequence"] = index
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True)
                                  for row in rows) + "\n", encoding="utf-8")
        return path

    def test_the_audit_refuses_gate_evidence_that_is_missing_wrong_or_duplicated(self):
        """R3-6: every check is correlated to the real request, or the run is refused.

        The chain is built ONCE and each mutation is applied to the sealed journal
        bytes, so this is a reading of evidence, not a re-run: a proxy that armed
        nothing, a proxy armed from another declaration, a check that does not
        describe its own request, a duplicated check and a missing check.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, config_path=self._config(tmp, 262144, verified=True))
            path = Path(fixture["journal"])
            original = path.read_bytes()

            def audit():
                return self.chain.audit_of(fixture)

            def codes(report):
                return [entry["code"] for entry in report["invalid_reasons"]]

            # (1) A journal that shows no arming at all: the walk is clean (the sealed
            # shape) and the capacity gate refuses the run's own declaration.
            def never_armed(rows):
                return [row for row in rows
                        if row.get("kind") not in ("capacity_armed", "capacity_check")]

            self._mutate_journal(fixture, never_armed)
            report = audit()
            self.assertFalse(report["input_audit_passed"])
            self.assertIn("the_proxy_that_served_this_run_was_never_armed", codes(report))
            self.assertTrue(report["capacity_policy"]["declared"])

            # (2) Armed from a DIFFERENT declaration: the digest the proxy journalled
            # is not the digest of the config this run declares.
            def other_declaration(rows):
                for row in rows:
                    if row.get("kind") == "capacity_armed":
                        row["declaration_sha256"] = "0" * 64
                return rows

            path.write_bytes(original)
            self._mutate_journal(fixture, other_declaration)
            report = audit()
            self.assertIn("proxy_armed_from_a_different_declaration", codes(report))
            self.assertTrue(report["capacity_policy"]["declared"])

            # (3) A check that does not describe the request it sits behind: the
            # transport walk refuses it and the capacity gate never sees a count.
            def wrong_digest(rows):
                for row in rows:
                    if row.get("kind") == "capacity_check":
                        row["input_sha256"] = "0" * 64
                        break
                return rows

            path.write_bytes(original)
            self._mutate_journal(fixture, wrong_digest)
            report = audit()
            self.assertIn("transport_capture_check_failed", codes(report))
            self.assertIn("capacity_check_does_not_describe_this_request",
                          report["transport"]["issues"])

            # (4) A duplicated check: exactly one check per chat request, or refuse.
            def duplicate_check(rows):
                index = next(position for position, row in enumerate(rows)
                             if row.get("kind") == "capacity_check")
                rows.insert(index + 1, json.loads(json.dumps(rows[index])))
                return rows

            path.write_bytes(original)
            self._mutate_journal(fixture, duplicate_check)
            report = audit()
            self.assertIn("capacity_checks_differ_from_chat_requests",
                          report["transport"]["issues"])

            # (5) A missing check: the same rule in the other direction.
            def drop_check(rows):
                index = next(position for position, row in enumerate(rows)
                             if row.get("kind") == "capacity_check")
                del rows[index]
                return rows

            path.write_bytes(original)
            self._mutate_journal(fixture, drop_check)
            report = audit()
            self.assertIn("capacity_checks_differ_from_chat_requests",
                          report["transport"]["issues"])
            path.write_bytes(original)

    def test_the_candidate_as_shipped_is_refused_at_the_run_stage(self):
        """Both open items - unverified endpoint AND exploration asset - refuse a RUN."""
        from scripts.ae_01_cloud_re_multiturn import capacity_decision
        config = validate_config(json.loads(self._config(tmp=None, window=262144)
                                            .read_text(encoding="utf-8"))) if False else None
        # Build the declaration in memory (no temp dir needed): the decision is taken
        # before any output directory exists.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            config = validate_config(json.loads(
                self._config(Path(tmp), 262144).read_text(encoding="utf-8")))
        state = capacity_decision(config, "plan")
        self.assertFalse(state["run_allowed"])
        self.assertEqual(state["tokenizer_asset"]["exploration_only"], True)
        self.assertEqual(state["tokenizer_asset"]["asset_target"], "Qwen/Qwen3-8B")
        self.assertIn("EXPLORATION", state["open_item"])
        # Two INDEPENDENT refusals, either of which must stop a RUN on its own:
        #  * the unverified endpoint (the shipped candidate), and
        #  * the exploration counting asset (which survives even a verified endpoint).
        with self.assertRaises(RuntimeError) as raised:
            capacity_decision(config, "run")
        self.assertIn("not verified for this endpoint", str(raised.exception))
        verified = dict(config)
        verified["capacity"] = json.loads(json.dumps(config["capacity"]))
        verified["capacity"]["service_limit"]["endpoint_measured_tokens"] = 262144
        verified["capacity"]["verification"] = "verified"
        verified = validate_config(verified)
        with self.assertRaises(RuntimeError) as raised:
            capacity_decision(verified, "run")
        self.assertIn("EXPLORATION", str(raised.exception))

    def test_an_over_capacity_chain_stops_before_the_provider(self):
        """A chain whose own history exceeds the window stops with its evidence kept."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, config_path=self._config(tmp, 32768), mutate=self._inflated_history)
            result = fixture["result"]
            self.assertEqual(result["status"], "INVALID")
            message = " ".join(entry["message"] for entry in result["invalid_reasons"])
            self.assertIn("capacity_exceeded_before_send", message)
            checks = result["capacity_checks"]
            self.assertTrue(checks)
            self.assertFalse(checks[-1]["fits"])
            self.assertGreater(checks[-1]["input_tokens"], 32768 - 2048)
            starts = [event for event in fixture["events"]
                      if event.get("kind") == "stage_start"]
            self.assertEqual([event["task"] for event in starts], ["sub_U000828_4"])
            self.assertEqual([event for event in fixture["events"]
                              if event.get("kind") == "phase_complete"], [])
            report = self.chain.audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])
            policy = report["capacity_policy"]
            self.assertTrue(policy["declared"], report.get("invalid_reasons"))
            self.assertEqual(policy["agent_tags_by_arm"],
                             {"rewrite": [NO_COMPACTION_TAG], "erratum": [NO_COMPACTION_TAG]})
            self.assertEqual(len(policy["stops"]), 1, policy["stops"])
            self.assertEqual(policy["reserve_rule"],
                             {"agent_or_unknown": 2048, "auxiliary_roles": 4096})
            self.assertIn("capacity_gate", report["checks_executed"])



class LoadGateCompatTests(_StubCleanupMixin, unittest.TestCase):
    """The load/manifest chain stays compatible with the capacity patch (F6).

    The reviewed multicall manifest pins `letta/agents/letta_agent_v3.py` to its OWN
    patched digest, so the capacity patch must not be applied to the checkout the
    service load gate verifies. These tests run the REAL `verify_letta_gate` over both
    checkouts and assert the split, instead of asserting it in prose.
    """

    def setUp(self):
        super().setUp()
        import ae_multicall as ai
        self.ai = ai
        self.multicall_checkout = Path(os.environ.get(
            "AE_LETTA_PATCHED_SOURCE",
            ROOT / ".ae-verify-src/letta-multicall-patch/letta-v1"))
        self.stacked_checkout = CHECKOUT
        if not self.multicall_checkout.is_dir() or not self.stacked_checkout.is_dir():
            self.skipTest("the patched Letta checkouts are not available")

    def _manifest_for(self, checkout):
        """The reviewed manifest with its checkout pointed at a real directory."""
        import tempfile
        source = json.loads((ROOT / "deployment-assets/letta-multicall/manifest.json")
                            .read_text(encoding="utf-8"))
        source["letta_checkout"] = str(checkout)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "manifest.json"
        path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def _environ(self, manifest):
        import hashlib
        return {"AE_LETTA_MULTICALL_PROFILE": self.ai.PROFILE_VERSION,
                "AE_LETTA_MULTICALL_MANIFEST": str(manifest),
                "AE_LETTA_MULTICALL_MODULE_SHA256": hashlib.sha256(
                    (ROOT / "ae_multicall.py").read_bytes()).hexdigest()}

    def test_the_multicall_only_checkout_still_verifies(self):
        """The service-side checkout and the reviewed manifest remain one identity."""
        manifest = self._manifest_for(self.multicall_checkout)
        self.assertTrue(self.ai.verify_letta_gate(
            environ=self._environ(manifest), module=self.ai), 
            "the multicall manifest no longer verifies its own checkout")

    def test_the_capacity_stacked_checkout_is_refused_by_that_manifest(self):
        """Stacking onto the service checkout would falsify the pinned identity."""
        manifest = self._manifest_for(self.stacked_checkout)
        self.assertFalse(self.ai.verify_letta_gate(
            environ=self._environ(manifest), module=self.ai),
            "the capacity-stacked checkout passed the multicall-only identity check")

    def test_the_capacity_manifest_pins_the_stacked_checkout_bytes(self):
        """The capacity manifest's own digests describe the runtime checkout exactly."""
        import hashlib
        declaration = json.loads((ROOT / "deployment-assets/letta-no-compaction/manifest.json")
                                 .read_text(encoding="utf-8"))
        for name, entry in declaration["patched_files"].items():
            target = self.stacked_checkout / name
            self.assertTrue(target.is_file(), name)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                             entry["patched_sha256"], name)
            self.assertNotEqual(entry["baseline_sha256"], entry["patched_sha256"], name)
        for name, entry in declaration["new_files"].items():
            target = self.stacked_checkout / name
            self.assertTrue(target.is_file(), name)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                             entry["sha256"], name)

    def test_the_staged_tokenizer_is_the_projects_tokenizer(self):
        """The service counts with the same implementation the report counts with."""
        import subprocess
        completed = subprocess.run([sys.executable, "-B",
                                    str(ROOT / "tools/check_tokenizer_drift.py")],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("tokenizer section identical: True", completed.stdout)



class RequestContractTests(_StubCleanupMixin, unittest.TestCase):
    """The gate reads the request shape the PINNED schema declares (F2).

    The production builder cannot run offline (it needs `openai`/`httpx`), so the
    claim "`build_request_data` returns a dict with `messages` and `tools`" is made
    checkable a different way: the schema the builder validates against is parsed from
    the pinned file, and the gate is required to read exactly those declared fields -
    while the attribute form the r1 patch used is required to fail.
    """

    def test_the_pinned_schema_declares_the_fields_the_gate_reads(self):
        contract = load_helper().dict_request_contract(SCHEMA)
        self.assertTrue(contract["is_pydantic_model"])
        for field in ("messages", "tools", "model", "max_completion_tokens"):
            self.assertIn(field, contract["declared_fields"])
        self.assertEqual(contract["model"], "ChatCompletionRequest")

    def test_the_builder_returns_a_dict_not_an_attribute_object(self):
        """`data.model_dump(...)` is the last statement of the real builder."""
        source = BUILDER.read_text(encoding="utf-8")
        function = source[source.index("    def build_request_data("):]
        function = function[:function.index("\n    def ", 1)]
        self.assertIn("request_data = data.model_dump(exclude_unset=True)", function)
        self.assertIn("return request_data", function)

    def test_the_attribute_form_the_r1_patch_used_would_fail(self):
        """A dict has no `.messages`, which is why the r1 gate never counted."""
        built = _built_request()
        self.assertIsInstance(built, dict)
        with self.assertRaises(AttributeError):
            _ = built.messages
        self.assertEqual(sorted(built)[:2], ["max_completion_tokens", "messages"])

    def test_the_gate_reads_the_declared_fields(self):
        source = AGENT.read_text(encoding="utf-8")
        self.assertIn('request_data.get("messages")', source)
        self.assertIn('request_data.get("tools")', source)
        self.assertNotIn("request_data.messages", source)


if __name__ == "__main__":
    unittest.main()
