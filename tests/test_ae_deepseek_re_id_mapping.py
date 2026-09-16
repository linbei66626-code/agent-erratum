"""The protocol-limited tool-call-id mapping, on the REAL completed capture.

The completed run `ae-deepseek-re-live-20260916-r4` was audited offline and stopped at
`assistant_history_changed`: the provider's ids (`call_00_bIzNFTjB8CduPr3zBpyX3052`) come
back in the service's next request as their 29-character projections
(`call_00_bIzNFTjB8CduPr3zBpyX3`, `letta/constants.py::TOOL_CALL_ID_MAX_LEN`). The run
declares the multicall compatibility protocol, whose declared rule is to preserve ids
whole - and the service that actually wrote this capture was launched WITHOUT that
profile (`launch.json` records `multicall_profile: null`), so the pinned serializer's
slice ran.

This suite pins the repair: a history id is accepted when it IS one of this capture's own
native ids, or when it is EXACTLY the 29-character projection of exactly ONE of them.
Anything else - an unknown id, a shorter prefix, an arbitrary replacement, a projection
shared by two native ids - is refused, and name/argument text and non-empty content still
have to match exactly. The sealed protocols are not touched.

The real ids and the real triples are read from the sealed capture (read-only); no
capture, plan, result or old report is modified anywhere in this suite.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRANSFER = ROOT / "transfers/ae-deepseek-re-complete-20260916-r4"
RUN_DIR = TRANSFER / "deployment/runs/ae-deepseek-re-live-20260916-r4"
JOURNAL = (TRANSFER / "deployment/runs"
           / "ae-deepseek-re-live-20260916-r4.private.jsonl")
CONFIG = ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-budget1024-candidate.json"
FIRST_NATIVE = "call_00_bIzNFTjB8CduPr3zBpyX3052"
FIRST_WIRE = "call_00_bIzNFTjB8CduPr3zBpyX3"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _audit_object():
    """A real audit object over the SEALED capture, loaded read-only.

    `load()` and the shared `cloud_calls()` walk run (they only read the recorded
    files), and the provider id namespace is then frozen by the audit's OWN
    `_freeze_provider_ids`, exactly as the wire gate does. The environment-dependent
    gates (site-absolute paths) are deliberately not evaluated here, because this suite
    is about the id rule, not about the recording's paths.
    """
    module = _load("ae_cloud_re_multiturn_input_audit_for_ids",
                   ROOT / "ae_cloud_re_multiturn_input_audit.py")
    audit = module.MultiturnInputAudit(RUN_DIR, JOURNAL, config_path=CONFIG)
    audit.load()
    audit.declared_profile()      # the audit's own resolver, from the capture's cloud_open
    # The declared receive policy, resolved from the plan exactly as `config_gate` does:
    # the id rule is protocol-aware, so the protocol has to be resolved first.
    from ae_cloud_re_multiturn import resolve_policy
    audit.multicall = resolve_policy(audit.plan["config"], allow_absent=False).as_dict()
    audit.cloud_calls()
    audit._freeze_provider_ids()
    # The wire gate binds the per-arm native-id sets as it walks; this suite drives the
    # same methods directly, so it binds the same structure up front.
    audit.known_tool_ids = {arm: set() for arm in module.ARMS}
    return audit


def _real_namespace_and_pairs():
    """Native ids plus one real (provider turn, wire history) pair per tool call."""
    native, pairs = [], []
    wire_seen = set()
    with JOURNAL.open() as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") == "upstream_response":
                body = json.loads(base64.b64decode(row["body_base64"]).decode("utf-8"))
                for choice in body.get("choices") or ():
                    for call in ((choice or {}).get("message") or {}).get("tool_calls") or ():
                        cid = (call or {}).get("id")
                        if isinstance(cid, str) and cid:
                            native.append(cid)
            elif row.get("kind") == "client_request" and (row.get("body_bytes") or 0) > 0:
                body = json.loads(base64.b64decode(row["body_base64"]).decode("utf-8"))
                for message in body.get("messages") or ():
                    if not isinstance(message, dict):
                        continue
                    for call in message.get("tool_calls") or ():
                        cid = (call or {}).get("id")
                        if isinstance(cid, str) and cid and cid not in wire_seen:
                            wire_seen.add(cid)
                            pairs.append((cid, (call or {}).get("function") or {}))
    return sorted(set(native)), pairs


class TheSealedCaptureIdMappingTests(unittest.TestCase):
    """The rule against the real ids and the real pair the live run produced."""

    @classmethod
    def setUpClass(cls):
        if not JOURNAL.is_file():
            raise unittest.SkipTest("the completed capture is absent")
        cls.audit = _audit_object()
        cls.native, cls.pairs = _real_namespace_and_pairs()
        cls._pristine = {
            "multicall": cls.audit.multicall,
            "provider_ids": set(cls.audit.provider_ids),
            "known_tool_ids": {arm: set(values)
                               for arm, values in cls.audit.known_tool_ids.items()},
        }

    def setUp(self):
        # Every case starts from the capture's frozen namespace: the audit object is
        # shared, and a case that deliberately mutates it must not leak into the next.
        audit = self.audit
        audit.multicall = self._pristine["multicall"]
        audit.provider_ids = set(self._pristine["provider_ids"])
        audit.known_tool_ids = {arm: set(values)
                                for arm, values in self._pristine["known_tool_ids"].items()}
        audit._id_forms = {"identity": 0, "projection": 0, "refused": 0}

    def test_the_first_live_id_and_its_return_both_resolve(self):
        audit = self.audit
        self.assertEqual(FIRST_NATIVE[:29], FIRST_WIRE)
        self.assertIn(FIRST_NATIVE, audit._known_native_ids())
        self.assertEqual(audit.resolve_history_tool_call_id(FIRST_WIRE), FIRST_NATIVE)
        self.assertEqual(audit.resolve_history_tool_call_id(FIRST_NATIVE), FIRST_NATIVE)

    def test_every_real_wire_id_resolves_to_exactly_one_native_id(self):
        audit = self.audit
        resolved = {}
        for wire_id, _function in self.pairs:
            native = audit.resolve_history_tool_call_id(wire_id)
            resolved[wire_id] = native
        self.assertEqual(len(resolved), len(self.pairs))
        self.assertTrue(all(value in audit._known_native_ids()
                            for value in resolved.values()))
        # ... and both sides of the wire carry the same ids in this capture.
        self.assertGreater(len(self.pairs), 100)

    def test_the_call_form_and_the_projection_form_compare_equal(self):
        audit = self.audit
        wire_id, function = self.pairs[0]
        native = audit.resolve_history_tool_call_id(wire_id)
        observed = [{"id": wire_id, "function": function}]
        expected = [{"id": native, "function": function}]
        self.assertTrue(audit.same_tool_calls(observed, expected))
        self.assertTrue(audit.history_tool_return_id_matches(wire_id, native))
        self.assertTrue(audit.history_tool_return_id_matches(native, native))

    def test_a_shorter_prefix_and_a_longer_value_are_not_wire_forms(self):
        audit = self.audit
        for value in (FIRST_NATIVE[:20], FIRST_NATIVE[:28], FIRST_NATIVE + "9",
                      FIRST_NATIVE[:29] + "0"):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    audit.resolve_history_tool_call_id(value)

    def test_an_unknown_id_is_refused(self):
        audit = self.audit
        for value in ("call_00_AAAAAAAAAAAAAAAAAAAAA", "call_00_unknown", "", None, 29):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    audit.resolve_history_tool_call_id(value)

    def test_an_arbitrary_replacement_is_refused(self):
        """Another capture id is not this call's wire form, even though it resolves."""
        audit = self.audit
        wire_id, function = self.pairs[0]
        native = audit.resolve_history_tool_call_id(wire_id)
        other = next(value for value in sorted(audit._known_native_ids()) if value != native)
        observed = [{"id": other[:29], "function": function}]
        expected = [{"id": native, "function": function}]
        self.assertFalse(audit.same_tool_calls(observed, expected))

    def test_a_projection_shared_by_two_native_ids_is_refused(self):
        """The one case where truncation is genuinely ambiguous."""
        audit = self.audit
        first = "call_00_sharedPrefixForTheTest0001"
        second = "call_00_sharedPrefixForTheTest0002"
        self.assertEqual(first[:29], second[:29])
        audit.provider_ids = {first, second}
        audit.known_tool_ids = {arm: set() for arm in audit.known_tool_ids}
        with self.assertRaises(Exception) as caught:
            audit.resolve_history_tool_call_id(first[:29])
        self.assertIn("tool_call_id_truncation_collision", str(caught.exception))

    def test_parameter_and_nonempty_text_changes_are_still_refused(self):
        audit = self.audit
        wire_id, function = self.pairs[0]
        native = audit.resolve_history_tool_call_id(wire_id)
        changed_arguments = dict(function, arguments=(function.get("arguments") or "") + " ")
        self.assertFalse(audit.same_tool_calls(
            [{"id": wire_id, "function": changed_arguments}],
            [{"id": native, "function": function}]))
        changed_name = dict(function, name="another_tool")
        self.assertFalse(audit.same_tool_calls(
            [{"id": wire_id, "function": changed_name}],
            [{"id": native, "function": function}]))
        text = [{"role": "assistant", "content": "changed",
                 "tool_calls": [{"id": wire_id, "function": function}]}]
        wanted = [{"role": "assistant", "content": "original",
                   "tool_calls": [{"id": native, "function": function}]}]
        self.assertFalse(audit.assistant_history_matches(text[0], wanted[0]))
        # ... while the pinned empty-string -> null transform still compares equal.
        self.assertTrue(audit.assistant_history_matches(
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": wire_id, "function": function}]},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": native, "function": function}]}))

    def test_the_sealed_protocol_rule_is_untouched(self):
        audit = self.audit
        audit.multicall = None
        audit.known_tool_ids = {FIRST_NATIVE}   # the sealed rule reads a FLAT id set
        # The sealed rule projects BOTH sides with the fixed slice and compares exactly.
        self.assertTrue(audit.same_tool_calls(
            [{"id": FIRST_WIRE, "function": {"name": "memory_update", "arguments": "{}"}}],
            [{"id": FIRST_NATIVE, "function": {"name": "memory_update", "arguments": "{}"}}]))
        self.assertFalse(audit.history_tool_return_id_matches(FIRST_NATIVE, FIRST_NATIVE),
                         "the sealed expected form is the projection, not the full id")
        self.assertTrue(audit.history_tool_return_id_matches(FIRST_WIRE, FIRST_NATIVE))

    def test_the_report_states_which_form_was_used(self):
        audit = self.audit
        for wire_id, _function in self.pairs[:5]:
            audit.resolve_history_tool_call_id(wire_id)
        self.assertEqual(audit._id_forms["projection"], 5)
        self.assertEqual(audit._id_forms["identity"], 0)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
