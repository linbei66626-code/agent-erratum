"""Directed tests for the auxiliary message wire-format fix.

Positive expectations come from the pinned Vita classes, not from the new
implementation: the checker input is the real `model_dump(mode="json")` of real
message objects (the captured representation), and the expected wire is the
pinned `format_messages` applied to those same objects. A handwritten internal
dict is never used as the positive capture. The real r4 raw is used read-only as
a regression.

This module imports the pinned classes in-process, so it restores
`sys.modules`/`sys.path`/env at class teardown; a later test
(`tests/test_ae_native_integration.py`) requires that no `vita` module is
already loaded when it re-imports the pinned source under a temporary config.
"""
from __future__ import annotations

import ast
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ae_cloud_input_audit import (  # noqa: E402
    CloudInputAudit, auxiliary_wire_messages,
)
from ae_cloud_proxy import CloudConfig, normalize_request, response_summary  # noqa: E402
from ae_input_audit import AuditFailure, no_nulls, wire as wire_bytes  # noqa: E402
from test_ae_cloud_input_audit import (  # noqa: E402
    assert_transport_still_checked, proxy_rows, rewrite_wire, save_proxy_rows)

VITA_SOURCE = Path(os.environ.get("AE_VITA_SOURCE", "/tmp/ae01-vita-8WHDvk/source"))
LLM_UTILS = VITA_SOURCE / "src/vita/utils/llm_utils.py"
R4_RUN = ROOT / "transfers/lab-cloud-t4-20260912-r4/deployment/runs/ae-capability-cloud-t4-20260912-r4"
R4_JOURNAL = (ROOT / "transfers/lab-cloud-t4-20260912-r4/deployment/runs"
              / "ae-capability-cloud-t4-20260912-r4.private.jsonl")
CAPABILITY = ROOT / "configs/ae-01__capability__siliconflow.pacing-candidate.json"


def pinned_format_messages(classes):
    """The unmodified pinned `format_messages` body, compiled from the source.

    The body dispatches on `isinstance` against the real message classes, so the
    extracted function is executed against the real imported classes.
    """
    text = LLM_UTILS.read_text(encoding="utf-8")
    node = next(n for n in ast.parse(text).body
                if isinstance(n, ast.FunctionDef) and n.name == "format_messages")
    source = ast.get_source_segment(text, node)
    namespace = {
        "json": json,
        "Message": object,
        "SystemMessage": classes["system"],
        "UserMessage": classes["user"],
        "AssistantMessage": classes["assistant"],
        "ToolMessage": classes["tool"],
    }
    exec(compile(source, str(LLM_UTILS), "exec"), namespace)
    return namespace["format_messages"]


def real_message_classes(tmp):
    """Import the real message classes with a dummy-key model config (offline)."""
    import importlib
    config = Path(tmp) / "models.yaml"
    config.write_text(json.dumps({"default": {}, "models": [
        {"name": "Qwen3-8B", "base_url": "http://127.0.0.1:8190/v1", "api_key": "EMPTY"}]}),
        encoding="utf-8")
    os.environ["VITA_MODEL_CONFIG_PATH"] = str(config)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    sys.path.insert(0, str(VITA_SOURCE / "src"))
    importlib.invalidate_caches()
    module = importlib.import_module("vita.data_model.message")
    return {"system": module.SystemMessage, "user": module.UserMessage,
            "assistant": module.AssistantMessage, "tool": module.ToolMessage,
            "tool_call": module.ToolCall}


class RealVitaTestCase(unittest.TestCase):
    """Base fixture: real pinned classes + pinned formatter + process restore."""

    @classmethod
    def setUpClass(cls):
        if not LLM_UTILS.is_file():
            raise unittest.SkipTest(f"fixed Vita source not present: {LLM_UTILS}")
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory(prefix="ae-aux-wire-")
        cls.addClassCleanup(cls._tmp.cleanup)
        before_modules = {k: v for k, v in sys.modules.items()
                          if k == "vita" or k.startswith("vita.")}
        before_path = list(sys.path)
        before_env = {k: os.environ.get(k)
                      for k in ("VITA_MODEL_CONFIG_PATH", "PYTHON_DOTENV_DISABLED")}

        def restore_process():
            for name in list(sys.modules):
                if name == "vita" or name.startswith("vita."):
                    sys.modules.pop(name, None)
            sys.modules.update(before_modules)
            sys.path[:] = before_path
            for key, value in before_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        cls.addClassCleanup(restore_process)
        try:
            cls.classes = real_message_classes(cls._tmp.name)
        except Exception as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"real Vita message classes unavailable: {type(exc).__name__}")
        cls.format_messages = staticmethod(pinned_format_messages(cls.classes))

    def dump(self, objects):
        """The captured representation: real `model_dump(mode="json")` output."""
        return [obj.model_dump(mode="json") for obj in objects]

    def captured_messages(self):
        """A representative captured sequence built from real message objects."""
        classes = self.classes
        return [
            classes["system"](role="system", content="sys", turn_idx=0),
            classes["user"](role="user", content="hello", turn_idx=1),
            classes["assistant"](role="assistant", content=None, turn_idx=2,
                                 tool_calls=[classes["tool_call"](
                                     id="c1", name="read", arguments={"a": 1})]),
            classes["tool"](role="tool", id="c1", name="read", content="{}", turn_idx=3),
        ]

    def wire_of(self, objects):
        """The expected provider wire for real objects (nulls stripped)."""
        return no_nulls(self.format_messages(objects))


class PinnedFormatterTests(RealVitaTestCase):
    def test_converter_matches_pinned_format_messages(self):
        objects = self.captured_messages()
        captured = self.dump(objects)  # what the capture actually stores
        expected = self.format_messages(objects)
        self.assertEqual(auxiliary_wire_messages(deepcopy(captured)), expected)
        # Null stripping is the SDK's serialization step, applied afterwards.
        self.assertEqual(no_nulls(auxiliary_wire_messages(deepcopy(captured))),
                         self.wire_of(objects))
        # Internal metadata must not leak into the wire.
        for message in expected:
            self.assertEqual(set(message) & {"timestamp", "turn_idx", "cost", "usage",
                                             "raw_data", "requestor", "error", "id"},
                             set())

    def test_default_tool_message_dump_is_accepted(self):
        tool = self.classes["tool"](role="tool", id="c1", name="read", content="{}")
        captured = self.dump([tool])
        # The real default dump declares requestor/error; they are internal.
        self.assertEqual(set(captured[0]), {"id", "name", "role", "content", "requestor",
                                            "error", "turn_idx", "timestamp"})
        self.assertEqual(captured[0]["requestor"], "assistant")
        self.assertFalse(captured[0]["error"])
        self.assertEqual(auxiliary_wire_messages(deepcopy(captured)),
                         [{"role": "tool", "content": "{}", "tool_call_id": "c1",
                           "name": "read"}])
        self.assertEqual(auxiliary_wire_messages(deepcopy(captured)),
                         self.format_messages([tool]))

    def test_assistant_tool_call_dump_is_accepted(self):
        classes = self.classes
        assistant = classes["assistant"](role="assistant", content=None, tool_calls=[
            classes["tool_call"](id="c1", name="read", arguments={"a": [1, 2]})])
        captured = self.dump([assistant])
        # requestor is declared internal state of the pinned ToolCall class.
        self.assertEqual(set(captured[0]["tool_calls"][0]),
                         {"id", "name", "arguments", "requestor"})
        self.assertEqual(auxiliary_wire_messages(deepcopy(captured)),
                         self.format_messages([assistant]))
        self.assertEqual(auxiliary_wire_messages(deepcopy(captured))[0]["tool_calls"],
                         [{"id": "c1", "type": "function",
                           "function": {"name": "read", "arguments": json.dumps({"a": [1, 2]})}}])

    def test_assistant_without_tool_calls_dump_is_accepted(self):
        assistant = self.classes["assistant"](role="assistant", content="plain")
        captured = self.dump([assistant])
        self.assertIsNone(captured[0]["tool_calls"])
        self.assertEqual(auxiliary_wire_messages(deepcopy(captured)),
                         self.format_messages([assistant]))

    def test_assistant_with_empty_tool_calls_dump_is_accepted(self):
        assistant = self.classes["assistant"](role="assistant", content="plain",
                                              tool_calls=[])
        captured = self.dump([assistant])
        self.assertEqual(captured[0]["tool_calls"], [])
        # The pinned formatter emits no tool_calls key for an empty list, and the
        # converter mirrors that instead of inventing an empty list.
        self.assertEqual(auxiliary_wire_messages(deepcopy(captured)),
                         [{"role": "assistant", "content": "plain"}])
        self.assertEqual(auxiliary_wire_messages(deepcopy(captured)),
                         self.format_messages([assistant]))

    def test_timestamp_value_change_does_not_change_the_wire(self):
        objects = self.captured_messages()
        captured = self.dump(objects)
        changed = deepcopy(captured)
        for message in changed:
            message["timestamp"] = "different-internal-time"
        self.assertEqual(auxiliary_wire_messages(changed),
                         auxiliary_wire_messages(deepcopy(captured)))

    def test_undeclared_system_metadata_is_rejected(self):
        system = self.classes["system"](role="system", content="sys")
        captured = self.dump([system])
        captured[0]["cost"] = 1  # SystemMessage declares no cost
        with self.assertRaises(AuditFailure) as caught:
            auxiliary_wire_messages(captured)
        self.assertEqual(str(caught.exception), "unknown_native_auxiliary_field")

    def test_undeclared_tool_metadata_is_rejected(self):
        tool = self.classes["tool"](role="tool", id="c1", name="read", content="{}")
        captured = self.dump([tool])
        for undeclared in ("cost", "usage", "raw_data"):
            bad = deepcopy(captured)
            bad[0][undeclared] = {} if undeclared != "cost" else 1
            with self.assertRaises(AuditFailure) as caught:
                auxiliary_wire_messages(bad)
            self.assertEqual(str(caught.exception), "unknown_native_auxiliary_field")

    def test_unknown_nested_tool_call_field_is_rejected(self):
        classes = self.classes
        assistant = classes["assistant"](role="assistant", content=None, tool_calls=[
            classes["tool_call"](id="c1", name="read", arguments={"a": 1})])
        captured = self.dump([assistant])
        captured[0]["tool_calls"][0]["mystery"] = 1
        with self.assertRaises(AuditFailure) as caught:
            auxiliary_wire_messages(captured)
        self.assertEqual(str(caught.exception), "unknown_native_tool_call_field")

    def test_unknown_field_is_rejected_not_ignored(self):
        captured = self.dump(self.captured_messages())
        captured[1]["mystery"] = 1
        with self.assertRaises(AuditFailure) as caught:
            auxiliary_wire_messages(captured)
        self.assertEqual(str(caught.exception), "unknown_native_auxiliary_field")

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(AuditFailure):
            auxiliary_wire_messages([{"role": "summary", "content": "x"}])


def chat_call(request_id, messages, *, response=None, **overrides):
    body = {"model": "Qwen/Qwen3-30B-A3B-Instruct-2507", "messages": messages,
            "temperature": 0, "max_tokens": 4096, "tools": None}
    body.update(overrides)
    return {"request_id": request_id, "body": body, "changes": [],
            "response": response if response is not None else {"choices": [{"index": 0,
                       "finish_reason": "stop", "message": {"role": "assistant", "content": "x"}}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}}


def auxiliary_audit(native_calls, chat_calls):
    audit = CloudInputAudit.__new__(CloudInputAudit)
    audit.posts = []
    audit.chat_calls = chat_calls
    audit.native_calls = native_calls
    audit.report = {"auxiliary_mappings": [],
                    "cloud_calls": {"agent": 0, "auxiliary": 0, "unaccounted": 0}}
    return audit


def native_record(messages, *, response=None, role="user_simulator", **request_extra):
    request = {"model": "Qwen/Qwen3-30B-A3B-Instruct-2507", "messages": messages,
               "temperature": 0, "max_tokens": 4096, "tools": None}
    request.update(request_extra)
    return {"role": role, "subtask_id": "sub_U000828_4", "request": request,
            "response": {"raw_data": response if response is not None else
                         {"choices": [{"index": 0, "finish_reason": "stop",
                                       "message": {"role": "assistant", "content": "x"}}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}},
            "error": None}


class AuxiliaryMatcherTests(RealVitaTestCase):
    """Response/duplicate/parameter/body/time negatives through the real matcher.

    The captured request and the expected wire both come from the same real
    message objects, so a positive match proves the conversion of the actual
    capture representation, not of a handwritten dict.
    """

    def record_and_call(self, *, mutate_call=None, **record_extra):
        objects = self.captured_messages()
        captured = self.dump(objects)
        wire = self.wire_of(objects)
        call = chat_call("r1", wire)
        if mutate_call:
            mutate_call(call)
        record = native_record(captured, **record_extra)
        return auxiliary_audit([record], [call])

    def test_matching_auxiliary_call_is_accepted(self):
        audit = self.record_and_call()
        audit.auxiliary()
        self.assertEqual(len(audit.report["auxiliary_mappings"]), 1)

    def test_internal_timestamp_change_still_matches(self):
        # A capture-side timestamp is internal metadata and must not break the
        # match, unlike a timestamp injected into the wire body below.
        objects = self.captured_messages()
        captured = self.dump(objects)
        for message in captured:
            message["timestamp"] = "another-internal-time"
        audit = auxiliary_audit([native_record(captured)],
                                [chat_call("r1", self.wire_of(objects))])
        audit.auxiliary()
        self.assertEqual(len(audit.report["auxiliary_mappings"]), 1)

    def test_response_change_is_rejected(self):
        def mutate(call):
            call["response"]["choices"][0]["message"]["content"] = "different"
        with self.assertRaises(AuditFailure) as caught:
            self.record_and_call(mutate_call=mutate).auxiliary()
        self.assertEqual(str(caught.exception), "auxiliary_call_missing_ambiguous_or_changed")

    def test_duplicate_match_is_rejected(self):
        objects = self.captured_messages()
        captured = self.dump(objects)
        call = chat_call("r1", self.wire_of(objects))
        with self.assertRaises(AuditFailure):
            auxiliary_audit([native_record(captured)],
                            [call, deepcopy(call)]).auxiliary()

    def test_parameter_change_is_rejected(self):
        def mutate(call):
            call["body"]["max_tokens"] = 2048
        with self.assertRaises(AuditFailure) as caught:
            self.record_and_call(mutate_call=mutate).auxiliary()
        self.assertEqual(str(caught.exception), "auxiliary_parameters_changed")

    def test_message_order_change_is_rejected(self):
        def mutate(call):
            messages = call["body"]["messages"]
            call["body"]["messages"] = [messages[0], messages[1], messages[3], messages[2]]
        with self.assertRaises(AuditFailure) as caught:
            self.record_and_call(mutate_call=mutate).auxiliary()
        self.assertEqual(str(caught.exception), "auxiliary_call_missing_ambiguous_or_changed")

    def test_message_role_change_is_rejected(self):
        def mutate(call):
            call["body"]["messages"][1]["role"] = "assistant"
        with self.assertRaises(AuditFailure) as caught:
            self.record_and_call(mutate_call=mutate).auxiliary()
        self.assertEqual(str(caught.exception), "auxiliary_call_missing_ambiguous_or_changed")

    def test_message_content_change_is_rejected(self):
        def mutate(call):
            call["body"]["messages"][1]["content"] = "tampered"
        with self.assertRaises(AuditFailure) as caught:
            self.record_and_call(mutate_call=mutate).auxiliary()
        self.assertEqual(str(caught.exception), "auxiliary_call_missing_ambiguous_or_changed")

    def test_tool_message_body_change_is_rejected(self):
        def mutate(call):
            call["body"]["messages"][3]["content"] = '{"tampered": true}'
        with self.assertRaises(AuditFailure) as caught:
            self.record_and_call(mutate_call=mutate).auxiliary()
        self.assertEqual(str(caught.exception), "auxiliary_call_missing_ambiguous_or_changed")

    def test_wire_timestamp_field_change_is_rejected(self):
        def mutate(call):
            call["body"]["messages"][1]["timestamp"] = "tampered-wire-time"
        with self.assertRaises(AuditFailure) as caught:
            self.record_and_call(mutate_call=mutate).auxiliary()
        self.assertEqual(str(caught.exception), "auxiliary_call_missing_ambiguous_or_changed")


def frozen_provenance_hashes(recorded, root=ROOT):
    """Map each production file's CURRENT digest onto the recorded digest.

    The r4 capture is sealed and recorded the digests of the code that actually
    ran. `ae_adapter.py` has changed since, so the live provenance gate stops the
    audit. To exercise the gates BEYOND provenance on that sealed capture, the
    digest comparison is fed the recorded values, keyed by the current file bytes
    so nothing else in the audit is affected.
    """
    import hashlib as _hashlib
    mapping = {}
    for name, recorded_digest in recorded.items():
        path = Path(root) / name
        if path.is_file():
            mapping[_hashlib.sha256(path.read_bytes()).hexdigest()] = recorded_digest
    return mapping


class _StubHashlib:
    """`hashlib` look-alike: known file digests resolve to the recorded value."""

    def __init__(self, real, mapping):
        self._real, self._mapping = real, mapping

    def sha256(self, data=b"", *args, **kwargs):
        return type("Digest", (), {
            "hexdigest": lambda self_: self._mapping.get(
                self._real.sha256(data).hexdigest(),
                self._real.sha256(data).hexdigest())})()

    def __getattr__(self, name):
        return getattr(self._real, name)


def mutate_auxiliary_body(journal, mutate):
    """Mutate one auxiliary request and keep its whole transport chain consistent.

    The mutation touches the client request body, then re-derives the normalized
    request, the upstream request and the cloud summary from it, exactly as the
    production proxy would have. The provider response bytes and status are
    reused, so the transport gate still passes and a later failure is a real
    auxiliary-gate verdict rather than transport damage.
    """
    # The transport configuration is the audit's own declared transport profile,
    # exactly as the other input-audit fixtures use it.
    config = CloudConfig(**json.loads(
        (ROOT / "configs"
         / "ae-01__cloud-transport__siliconflow.capability-candidate.json")
        .read_text(encoding="utf-8")))
    rows = [json.loads(line) for line in Path(journal).read_text(encoding="utf-8").splitlines()]
    targets = [row for row in rows if row.get("kind") == "client_request"
               and row.get("role") not in (None, "agent_or_unknown")]
    if not targets:
        raise AssertionError("no auxiliary client_request to mutate")
    target = targets[0]
    body = json.loads(base64.b64decode(target["body_base64"]))
    mutate(body)
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    rewrite_wire(target, raw)
    request_id = target["request_id"]
    normalized = next(row for row in rows if row.get("kind") == "normalized_request"
                      and row.get("request_id") == request_id)
    normalized_raw, changes = normalize_request(raw, config)
    rewrite_wire(normalized, normalized_raw)
    normalized["changes"] = changes
    upstream = next(row for row in rows if row.get("kind") == "upstream_request"
                    and row.get("request_id") == request_id)
    rewrite_wire(upstream, normalized_raw)
    upstream_response = next(row for row in rows if row.get("kind") == "upstream_response"
                             and row.get("request_id") == request_id)
    response_raw = wire_bytes(upstream_response)
    requested_output = json.loads(normalized_raw)["max_tokens"]
    summary = next(row for row in rows if row.get("kind") == "cloud_summary"
                   and row.get("request_id") == request_id)
    summary.update(response_summary(response_raw, upstream_response.get("http_status"),
                                    requested_output, config))
    with Path(journal).open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return request_id


class RealR4RegressionTests(unittest.TestCase):
    def test_r4_auxiliary_gate_is_cleared(self):
        if not (R4_RUN / "result.json").is_file() or not R4_JOURNAL.is_file():
            self.skipTest("transferred r4 raw is not present")
        from unittest.mock import patch
        from ae_cloud_input_audit import audit_cloud_inputs
        import ae_cloud_proxy
        import hashlib as _hashlib
        plan = json.loads((R4_RUN / "plan.json").read_text(encoding="utf-8"))
        # The transport gate hashes the proxy's OWN sources, and the capacity work
        # changed `ae_cloud_proxy.py` after this capture was sealed. Feed ONLY that
        # gate's source set its recorded digests; the plan's provenance gate keeps
        # comparing live bytes, so the capture still reports its real `ae_adapter.py`
        # mismatch below instead of being rewritten to look consistent.
        transport_files = {"ae_cloud_proxy.py", "ae_model_proxy.py", "ae_http.py"}
        stub = _StubHashlib(_hashlib, frozen_provenance_hashes(
            {name: digest for name, digest in plan["provenance"]["code_sha256"].items()
             if name in transport_files}))
        with patch.object(ae_cloud_proxy, "hashlib", stub):
            report = audit_cloud_inputs(R4_RUN, R4_JOURNAL, config_path=CAPABILITY)
        codes = [entry["code"] for entry in report["invalid_reasons"]]
        # The auxiliary representation gate must no longer be the failure, and no
        # later gate may take its place on this real captured run.
        self.assertNotIn("auxiliary_call_missing_ambiguous_or_changed", codes, codes)
        # 2026-09-12: this project changed `ae_adapter.py` on purpose for the
        # multi-client-tool-call compatibility work. The r4 capture is SEALED and
        # recorded the pre-change digest, so its provenance gate now reports
        # exactly that one mismatch -- the capture must not claim to have run code
        # that did not exist when it was made. Any other code is still a hard
        # failure and the sealed r4 bytes are never rewritten to make this pass.
        self.assertEqual(codes, ["plan_provenance_code_sha256_mismatch_ae_adapter.py"], codes)
        self.assertFalse(report["input_audit_passed"])
        self.assertNotEqual(report["status"], "VALID")
        # The provenance gate stops the audit before the dataset gate, so the
        # declared-but-absent container dataset is never reported as verified.
        self.assertNotEqual(report.get("dataset_verification", {}).get("status"), "verified")

    def test_r4_auxiliary_gate_passes_with_the_recorded_provenance(self):
        """The auxiliary gate itself, on the same sealed capture.

        Provenance is fed the capture's own recorded digests (see
        `frozen_provenance_hashes`), so every other gate runs against the real
        bytes and the auxiliary gate is genuinely exercised: seven auxiliary
        calls, in capture order, with no unconsumed or unaccounted call. This is
        a local gate check on an already-sealed capture; it does not upgrade the
        capture and is not a claim that a fresh run's audit passes.
        """
        if not (R4_RUN / "result.json").is_file() or not R4_JOURNAL.is_file():
            self.skipTest("transferred r4 raw is not present")
        from unittest.mock import patch
        import ae_cloud_input_audit
        import ae_cloud_proxy
        import ae_input_audit
        import hashlib as _hashlib
        plan = json.loads((R4_RUN / "plan.json").read_text(encoding="utf-8"))
        recorded = dict(plan["provenance"]["code_sha256"])
        mapping = frozen_provenance_hashes(recorded)
        self.assertTrue(mapping, "the recorded production files must be present")
        stub = _StubHashlib(_hashlib, mapping)
        # `ae_cloud_proxy` hashes its OWN sources for the transport gate, so the
        # frozen digests have to reach that module too, not only the input audit.
        with patch.object(ae_cloud_input_audit, "hashlib", stub), \
             patch.object(ae_input_audit, "hashlib", stub), \
             patch.object(ae_cloud_proxy, "hashlib", stub):
            report = ae_cloud_input_audit.audit_cloud_inputs(
                R4_RUN, R4_JOURNAL, config_path=CAPABILITY)
        codes = [entry["code"] for entry in report["invalid_reasons"]]
        self.assertEqual(codes, [])
        self.assertEqual(report["cloud_calls"]["auxiliary"], 7)
        self.assertEqual(len(report["auxiliary_mappings"]), 7)
        self.assertTrue(report["transport_capture_checked"])

    def test_a_transport_consistent_auxiliary_mutation_reaches_the_gate(self):
        """A content mutation with an intact transport chain fails IN THE GATE.

        `test_a_damaged_auxiliary_transport_is_rejected` below keeps covering raw
        transport damage. This one mutates the request content and rebuilds its
        whole record chain, so the transport gate passes and the AUXILIARY gate is
        the thing that refuses -- which is what "the auxiliary semantics are
        checked" means.
        """
        if not (R4_RUN / "result.json").is_file() or not R4_JOURNAL.is_file():
            self.skipTest("transferred r4 raw is not present")
        from unittest.mock import patch
        import shutil
        import tempfile
        import ae_cloud_input_audit
        import ae_cloud_proxy
        import ae_input_audit
        import hashlib as _hashlib
        plan = json.loads((R4_RUN / "plan.json").read_text(encoding="utf-8"))
        mapping = frozen_provenance_hashes(dict(plan["provenance"]["code_sha256"]))
        stub = _StubHashlib(_hashlib, mapping)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            shutil.copy2(R4_JOURNAL, journal)
            request_id = mutate_auxiliary_body(
                journal,
                lambda body: body["messages"][-1].__setitem__(
                    "content", "tampered auxiliary content"))
            with patch.object(ae_cloud_proxy, "hashlib", stub):
                assert_transport_still_checked({"journal": journal})
            with patch.object(ae_cloud_input_audit, "hashlib", stub), \
                 patch.object(ae_input_audit, "hashlib", stub), \
                 patch.object(ae_cloud_proxy, "hashlib", stub):
                report = ae_cloud_input_audit.audit_cloud_inputs(
                    R4_RUN, journal, config_path=CAPABILITY)
        codes = [entry["code"] for entry in report["invalid_reasons"]]
        self.assertTrue(codes, "a content-mutated auxiliary request must be refused")
        self.assertEqual(report["transport_capture_checked"], True,
                         f"transport must still be checked; codes={codes}")
        # The refusal must come from the auxiliary gate, not from transport damage.
        self.assertNotIn("transport_capture_check_failed", codes, codes)
        self.assertTrue(any("auxiliary" in code for code in codes),
                        f"the auxiliary gate must name the failure; codes={codes}")
        self.assertEqual(report["cloud_calls"].get("auxiliary", 0), 0)
        self.assertTrue(request_id)

    def test_a_damaged_auxiliary_transport_is_rejected(self):
        """Raw transport damage stays a transport verdict (kept from r2)."""
        if not (R4_RUN / "result.json").is_file() or not R4_JOURNAL.is_file():
            self.skipTest("transferred r4 raw is not present")
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            lines = R4_JOURNAL.read_text(encoding="utf-8").splitlines()
            mutated = 0
            for index, line in enumerate(lines):
                row = json.loads(line)
                if (row.get("kind") == "client_request" and row.get("role")
                        and row.get("role") != "agent_or_unknown" and mutated == 0):
                    body = json.loads(base64.b64decode(row["body_base64"]))
                    body["messages"][-1]["content"] = "tampered auxiliary content"
                    row["body_base64"] = base64.b64encode(
                        json.dumps(body, ensure_ascii=False).encode()).decode()
                    lines[index] = json.dumps(row, ensure_ascii=False)
                    mutated += 1
            self.assertEqual(mutated, 1)
            journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
            import ae_cloud_input_audit
            report = ae_cloud_input_audit.audit_cloud_inputs(
                R4_RUN, journal, config_path=CAPABILITY)
        codes = [entry["code"] for entry in report["invalid_reasons"]]
        self.assertTrue(codes)


if __name__ == "__main__":
    unittest.main()
