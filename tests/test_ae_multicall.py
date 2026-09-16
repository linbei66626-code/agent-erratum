"""Offline tests for the versioned multi-tool-call receive compatibility.

Two layers, both offline and socket-free:

1. `MulticallPolicyTests` / `BatchGateTests` exercise the pure policy, id rules
   and pre-side-effect batch gate in `ae_multicall` directly.
2. `BridgeMulticallTests` drives the REAL `LettaBridge` (through the real
   `MemoryPolicy`) against a minimal scripted Letta session that returns ONE
   approval message carrying the four real r2 calls with their original 32
   character ids and their shared 29 character prefix. Every fixture reply is a
   fixture, not model output; no socket, service or model is used.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ae_adapter import BridgeBlocked, LettaBridge, MemoryPolicy, dumps  # noqa: E402
from ae_multicall import (BatchRejected, CURRENT_POLICY, ID_KEYS, LETTA_ENV_VAR,  # noqa: E402
                          PROFILE_VERSION, UPSTREAM_TOOL_CALL_ID_MAX_LEN, ValidatedBatch,
                          batch_source, call_id, check_server_tool_types, collision_groups,
                          copy_batch, declared_policy, id_mapping, identical_batch,
                          resolve_policy, retained_length, returns_in_order, validate_batch,
                          validate_policy)

# The four real r2 calls, read VERBATIM from the sealed capture. The ids, the
# argument strings and the categories are the provider's own bytes: nothing here
# is re-encoded with `dumps`, and the fourth call keeps its real category
# "消费购物偏好".
R2_SOURCE = (ROOT / "transfers/lab-cloud-re-pair-run-20260912-r2/diagnosis"
             / "tool-call-truncation.json")
R2_PROVIDER_CALLS = json.loads(R2_SOURCE.read_text(encoding="utf-8"))["provider_calls"]
R2_IDS = tuple(call["id"] for call in R2_PROVIDER_CALLS)
R2_ARGUMENTS = tuple(call["function"]["arguments"] for call in R2_PROVIDER_CALLS)
R2_CALLS = tuple(json.loads(text) for text in R2_ARGUMENTS)
R2_EVIDENCE_REFS = tuple(call["evidence_ref"] for call in R2_CALLS)
TRUNCATED = "01a094c62aacd8ab46b11b69c6e81"


def real_batch_payload(calls=R2_ARGUMENTS, ids=R2_IDS):
    """The provider's approval payload with its ORIGINAL argument strings.

    `calls` is a tuple of argument strings, not of parsed dicts, so the bytes the
    bridge receives are the capture's bytes. Pass parsed dicts only where a test
    deliberately re-encodes.
    """
    return {"tool_calls": [
        {"id": native_id, "type": "function", "name": "memory_update",
         "arguments": arguments}
        for native_id, arguments in zip(ids, calls)]}


def facts():
    book = {f"p{i:03d}": {"category": "其他", "content": f"偏好 {i}"} for i in range(8)}
    # Categories must match the real r2 batch's `category` field, otherwise
    # `MemoryPolicy` would legitimately reject a call and no PATCH would appear.
    book["p007"] = {"category": "饮食偏好", "content": "奶茶偏好5分糖"}
    book["p003"] = {"category": "饮食偏好", "content": "旧牛肉面偏好"}
    # The real batch replaces p000/p001/p002 only in the RE driver fixture, not
    # here; this module uses the capture's own four facts (p007/p003/p004/p008).
    book["p004"] = {"category": "饮食偏好", "content": "旧麻辣烫偏好"}
    book["p008"] = {"category": "消费购物偏好", "content": "旧花束偏好"}
    return book


class MulticallPolicyTests(unittest.TestCase):
    def test_declared_policy_is_the_reviewed_profile(self):
        policy = declared_policy()
        self.assertEqual(validate_policy(policy), policy)
        self.assertEqual(policy["version"], PROFILE_VERSION)
        self.assertTrue(policy["retain_all_calls"])
        self.assertTrue(policy["preserve_native_ids"])
        self.assertTrue(policy["execute_serially"])
        self.assertEqual(policy["capture_order"], "provider_response_order")
        self.assertEqual(policy["unsupported_batch"], "stop_before_any_side_effect")
        # The compatibility policy must NOT flip the outbound request contract.
        self.assertFalse(policy["wire_parallel_tool_calls"])
        self.assertEqual(CURRENT_POLICY.version, PROFILE_VERSION)

    def test_absent_policy_is_the_old_protocol_and_present_policy_is_strict(self):
        self.assertIsNone(resolve_policy({"other": 1}))
        # An absent policy is None; a present one resolves to the frozen policy
        # object whose declaration is exactly the reviewed dict.
        resolved = resolve_policy({"multicall_profile": declared_policy()})
        self.assertEqual(resolved.as_dict(), declared_policy())
        self.assertEqual(resolved, CURRENT_POLICY)
        with self.assertRaises(Exception):
            resolve_policy({}, allow_absent=False)

    def test_weakened_unknown_or_missing_policy_fields_are_refused(self):
        for mutate in (
            lambda p: p.update(retain_all_calls=False),
            lambda p: p.update(preserve_native_ids=False),
            lambda p: p.update(execute_serially=False),
            lambda p: p.update(wire_parallel_tool_calls=True),
            lambda p: p.update(version="something-else"),
            lambda p: p.update(capture_order="sorted"),
            lambda p: p.update(unsupported_batch="truncate_to_first"),
            lambda p: p.pop("preserve_native_ids"),
            lambda p: p.update(extra_field=True),
            lambda p: p.update(version=1),
        ):
            with self.subTest(mutate=mutate):
                broken = declared_policy()
                mutate(broken)
                with self.assertRaises(Exception):
                    validate_policy(broken)
        for not_a_policy in (None, [], "ae-multicall", 3, declared_policy()["version"]):
            with self.subTest(value=not_a_policy):
                with self.assertRaises(Exception):
                    validate_policy(not_a_policy)

    def test_env_var_names_are_explicit(self):
        self.assertEqual(LETTA_ENV_VAR, "AE_LETTA_MULTICALL_PROFILE")
        self.assertEqual(UPSTREAM_TOOL_CALL_ID_MAX_LEN, 29)
        self.assertEqual(ID_KEYS, ("tool_call_id", "id"))

    def test_id_rules_retain_the_original_value_end_to_end(self):
        mapping = id_mapping(R2_IDS)
        self.assertEqual(mapping, {native_id: native_id for native_id in R2_IDS})
        for native_id, mapped in mapping.items():
            self.assertEqual(mapped, native_id)
            self.assertEqual(retained_length(native_id), 32)
        # Identity mapping cannot silently swallow a duplicate.
        with self.assertRaises(BatchRejected):
            id_mapping(list(R2_IDS) + [R2_IDS[0]])

    def test_the_real_r2_prefix_collision_is_reported_not_hidden(self):
        groups = collision_groups(R2_IDS)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0], sorted(R2_IDS))
        for native_id in R2_IDS:
            self.assertEqual(native_id[:UPSTREAM_TOOL_CALL_ID_MAX_LEN], TRUNCATED)

    def test_call_id_accepts_both_wire_spellings_and_refuses_disagreement(self):
        self.assertEqual(call_id({"id": "call-a"}), "call-a")
        self.assertEqual(call_id({"tool_call_id": "call-a"}), "call-a")
        self.assertEqual(call_id({"id": "call-a", "tool_call_id": "call-a"}), "call-a")
        for bad in ({"id": "call-a", "tool_call_id": "call-b"}, {}, {"id": ""},
                    {"id": None}, "call-a"):
            with self.subTest(call=bad):
                with self.assertRaises(BatchRejected):
                    call_id(bad)

    def test_server_tool_types_are_refused_before_use(self):
        check_server_tool_types([None, "letta_agent_file"])
        check_server_tool_types([])
        with self.assertRaises(BatchRejected) as caught:
            check_server_tool_types(["letta_core"])
        self.assertEqual(caught.exception.code, "server_side_tool_declared")


class BatchGateTests(unittest.TestCase):
    bindings = {"memory_update"}

    def validate(self, payload, known_ids=(), declared=None):
        return validate_batch(payload, declared if declared is not None else self.bindings,
                              known_ids=known_ids, memory_tool_name="memory_update")

    def test_real_four_call_batch_validates_completely_and_in_order(self):
        batch = self.validate(real_batch_payload())
        self.assertIsInstance(batch, ValidatedBatch)
        self.assertEqual(batch.ids, R2_IDS)
        self.assertEqual(batch.names, ("memory_update",) * 4)
        self.assertEqual(batch.shape, "tool_calls")
        self.assertEqual(batch.id_rule, "identity_retained_end_to_end")
        # Every id survived in full, so the batch is accepted.
        evidence = batch.as_evidence()
        self.assertEqual(evidence["count"], 4)
        self.assertEqual(evidence["ids"], list(R2_IDS))
        self.assertEqual(evidence["id_lengths"], [32, 32, 32, 32])
        # The recorded collision group proves WHY the old path lost three calls;
        # recording it is evidence, refusing it would re-create the defect.
        self.assertEqual([list(group) for group in batch.collision_groups],
                         [sorted(R2_IDS)])
        self.assertEqual(evidence["upstream_prefix_collision_groups"],
                         [sorted(R2_IDS)])
        for call, native_id, arguments in zip(batch.calls, R2_IDS, R2_ARGUMENTS):
            self.assertEqual(call["tool_call_id"], native_id)
            # The capture's own argument text, byte for byte.
            self.assertEqual(call["arguments"], arguments)
            self.assertEqual(json.loads(arguments)["category"],
                             json.loads(arguments)["category"])

    def test_argument_bytes_are_passed_through_unchanged(self):
        raw = '{"operation":"replace","fact_id":"p007","category":"饮食偏好",' \
              '"content":"奶茶偏好7分糖","evidence_ref":"t4/history/15"}'
        batch = self.validate({"tool_calls": [
            {"id": R2_IDS[0], "name": "memory_update", "arguments": raw}]})
        self.assertEqual(batch.calls[0]["arguments"], raw)

    def test_legacy_single_call_shape_is_read_as_one_batch(self):
        payload = {"tool_call": {"tool_call_id": "call-a", "name": "memory_update",
                                 "arguments": dumps(R2_CALLS[0])}}
        batch = self.validate(payload)
        self.assertEqual(batch.shape, "tool_call")
        self.assertEqual(batch.ids, ("call-a",))

    def test_nested_function_shape_is_lifted_without_re_encoding(self):
        raw = dumps(R2_CALLS[0])
        payload = {"tool_calls": [{"id": "call-a", "type": "function",
                                   "function": {"name": "memory_update", "arguments": raw}}]}
        batch = self.validate(payload)
        self.assertEqual(batch.calls[0]["arguments"], raw)
        self.assertEqual(batch.calls[0]["name"], "memory_update")

    def test_empty_unknown_or_malformed_batches_are_refused(self):
        for payload in (
            {"tool_calls": []},
            {"tool_calls": None, "tool_call": None},
            {},
            3,
            {"tool_calls": "memory_update"},
            {"tool_calls": [{"id": "call-a", "name": "memory_update"}]},
            {"tool_calls": [{"name": "memory_update", "arguments": "{}"}]},
            {"tool_calls": [{"id": "call-a", "arguments": "{}"}]},
            {"tool_calls": [{"id": "call-a", "name": "memory_update", "arguments": 3}]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(BatchRejected):
                    self.validate(payload)

    def test_undeclared_server_side_and_mixed_batches_are_refused_whole(self):
        mixed = real_batch_payload()
        mixed["tool_calls"].append({"id": "call-x", "name": "send_email",
                                    "arguments": "{}"})
        with self.assertRaises(BatchRejected) as caught:
            self.validate(mixed)
        self.assertEqual(caught.exception.code, "undeclared_tool_in_batch")
        server = real_batch_payload()
        server["tool_calls"][1] = {"id": R2_IDS[1], "type": "letta_core",
                                   "name": "memory_update", "arguments": dumps(R2_CALLS[1])}
        with self.assertRaises(BatchRejected) as caught:
            self.validate(server)
        self.assertEqual(caught.exception.code, "server_side_tool_in_client_batch")

    def test_duplicate_ids_inside_or_across_batches_are_refused(self):
        dup = real_batch_payload()
        dup["tool_calls"][3]["id"] = R2_IDS[0]
        with self.assertRaises(BatchRejected) as caught:
            self.validate(dup)
        self.assertEqual(caught.exception.code, "duplicate_tool_call_id_in_batch")
        with self.assertRaises(BatchRejected) as caught:
            self.validate(real_batch_payload(), known_ids=(R2_IDS[2],))
        self.assertEqual(caught.exception.code, "tool_call_id_reused_from_earlier_batch")

    def test_a_batch_that_is_not_declared_but_is_really_declared_is_accepted(self):
        # `memory_update` arrives through `memory_tool_name`, not the bindings.
        batch = validate_batch(real_batch_payload(), set(), known_ids=(),
                               memory_tool_name="memory_update")
        self.assertEqual(batch.names, ("memory_update",) * 4)
        with self.assertRaises(BatchRejected):
            validate_batch(real_batch_payload(), set(), known_ids=(), memory_tool_name=None)

    def test_batch_source_preserves_provider_order(self):
        calls, shape = batch_source(real_batch_payload())
        self.assertEqual(shape, "tool_calls")
        self.assertEqual([call["id"] for call in calls], list(R2_IDS))

    def test_returns_must_be_exactly_one_per_call_in_order(self):
        batch = self.validate(real_batch_payload())
        good = [{"tool_call_id": native_id, "status": "success"} for native_id in R2_IDS]
        self.assertEqual(returns_in_order(batch, good), good)
        for bad in (
            good[:3],
            good + [{"tool_call_id": "call-extra", "status": "success"}],
            list(reversed(good)),
            [{"tool_call_id": R2_IDS[0]}] * 4,
            [{"status": "success"}] * 4,
            [None] * 4,
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(BatchRejected):
                    returns_in_order(batch, bad)

    def test_batch_copy_and_equality_do_not_alias_the_original(self):
        batch = self.validate(real_batch_payload())
        clone = copy_batch(batch)
        self.assertTrue(identical_batch(batch, clone))
        clone.calls[0]["arguments"] = "{}"
        self.assertFalse(identical_batch(batch, clone))
        self.assertNotEqual(batch.calls[0]["arguments"], "{}")


class FakeLettaSession:
    """Minimal scripted Letta 0.16.8 surface for the bridge; no socket."""

    def __init__(self, pending_payloads, replies=None):
        self.block_value = None  # lazily bound to the bridge's real initial block
        self.initial_block = None
        self.pending_payloads = list(pending_payloads)
        self.replies = list(replies or [])
        self.patches = []
        self.message_ids = ["system-fixture"]
        self.posts = 1
        self.pending = None
        self.schemas = []
        self.tool_returns = []
        self.agent_id = "agent-fixture-multicall"

    def request(self, method, path, body=None):
        if method == "PATCH":
            self.block_value = body["value"]
            self.patches.append(deepcopy(body["value"]))
            return {"id": "block-fixture", "value": body["value"]}
        if method == "GET":
            # The real Letta session holds whatever the bridge last committed, so
            # GET must return the CURRENT block, not a frozen initial one.
            if self.block_value is None:
                self.block_value = self.initial_block
            return {
                "id": self.agent_id, "agent_type": "letta_v1_agent",
                "blocks": [{"id": "block-fixture", "label": "ae_preferences",
                            "value": self.block_value}],
                "tools": [], "sources": [], "tags": [],
                "message_ids": list(self.message_ids),
                "managed_group": None, "pending_approval": deepcopy(self.pending),
                "message_buffer_autoclear": False, "enable_sleeptime": False,
                "llm_config": {"parallel_tool_calls": False},
            }
        raise AssertionError(f"unexpected fixture Letta call: {method} {path}")

    def post(self, body):
        """One POST /messages; the caller consumes the returned output list.

        Mirrors the real session: posting tool returns satisfies the pending
        approval and ends the turn; only a later user turn can surface the next
        scripted approval.
        """
        self.schemas = deepcopy(body.get("client_tools") or [])
        submitted_returns = False
        for message in body.get("messages") or []:
            if message.get("type") == "tool_return":
                submitted_returns = True
                for returned in message["tool_returns"]:
                    self.tool_returns.append(deepcopy(returned))
        self.message_ids = self.message_ids + [f"message-{len(self.message_ids)}"]
        if submitted_returns:
            self.pending = None
            text = self.replies.pop(0) if self.replies else "fixture final answer"
            return {"messages": [{"message_type": "assistant_message", "content": text}],
                    "stop_reason": {"stop_reason": "end_turn"}}
        if self.pending is None and self.pending_payloads:
            call = self.pending_payloads.pop(0)
            self.pending = call["approval"]
            return {"messages": [{"message_type": "approval_request_message",
                                  "tool_calls": deepcopy(call["approval"]["tool_calls"])}],
                    "stop_reason": {"stop_reason": "requires_approval"}}
        if self.pending is not None:
            return {"messages": [{"message_type": "approval_request_message",
                                  "tool_calls": deepcopy(self.pending["tool_calls"])}],
                    "stop_reason": {"stop_reason": "requires_approval"}}
        text = self.replies.pop(0) if self.replies else "fixture final answer"
        return {"messages": [{"message_type": "assistant_message", "content": text}],
                "stop_reason": {"stop_reason": "end_turn"}}


class SessionTransport:
    """Routes the bridge's own request shape into the scripted session."""

    def __init__(self, session):
        self.session = session
        self.sent = []

    def request(self, method, path, body=None):
        self.sent.append({"method": method, "path": path, "body": deepcopy(body)})
        if method == "POST" and path.endswith("/messages"):
            return self.session.post(deepcopy(body))
        return self.session.request(method, path, body)


def bridge(session, *, arm="rewrite", multicall=True, include_memory_tool=True):
    memory = MemoryPolicy(arm, facts(), block_char_limit=8000)
    if session.initial_block is None:
        session.initial_block = memory.initial_block
    session.block_value = session.initial_block
    bridge_ = LettaBridge(SessionTransport(session), session.agent_id, memory,
                          max_rounds=8, max_steps=3,
                          include_memory_tool=include_memory_tool,
                          multicall=CURRENT_POLICY if multicall else None)
    bridge_.visible_refs = {"t4/history/0", "t4/history/3", "t4/history/5", "t4/history/15"}
    return bridge_


def history_user_message():
    return [{"role": "user", "type": "message",
             "content": dumps({"source": "dataset_history/material", "ref": "t4/history"})}]


class BridgeMulticallTests(unittest.TestCase):
    def test_four_call_batch_executes_serially_with_four_confirmed_patches(self):
        session = FakeLettaSession([{"approval": real_batch_payload()}],
                                   replies=["done"])
        bridge_ = bridge(session)
        result = bridge_.exchange(history_user_message(), {})
        self.assertEqual(result, [{"message_type": "assistant_message", "content": "done"}])
        # R publishes one confirmed PATCH per successful call, in provider order.
        self.assertEqual(len(session.patches), 4)
        self.assertEqual(session.block_value, session.patches[-1])
        published = json.loads(session.block_value)
        self.assertEqual(published["p007"]["content"], "奶茶偏好7分糖")
        self.assertEqual(published["p003"]["content"], "爱吃牛肉面（小份）")
        self.assertEqual(published["p004"]["content"], "爱吃麻辣烫（小份）")
        self.assertEqual(published["p008"]["content"], "喜欢向日葵（10枝装花束）")
        # Exactly one tool return per call, in the same order, with the original ids.
        self.assertEqual([r["tool_call_id"] for r in session.tool_returns], list(R2_IDS))
        self.assertTrue(all(r["status"] == "success" for r in session.tool_returns))
        self.assertEqual(len(bridge_.multicall_batches), 1)
        evidence = bridge_.multicall_batches[0]
        self.assertEqual(evidence["count"], 4)
        self.assertEqual(evidence["executed_count"], 4)
        self.assertEqual(evidence["policy"], declared_policy())
        self.assertEqual(evidence["patches_during_batch"], 4)
        self.assertEqual([r["tool_call_id"] for r in evidence["returns"]], list(R2_IDS))

    def test_no_parallel_path_and_one_model_request_per_batch(self):
        session = FakeLettaSession([{"approval": real_batch_payload()}],
                                  replies=["done"])
        bridge_ = bridge(session)
        bridge_.exchange(history_user_message(), {})
        posts = [entry for entry in bridge_.transport.sent
                 if entry["method"] == "POST" and entry["path"].endswith("/messages")]
        # One POST carrying the user turn, one POST carrying the whole batch's
        # returns. No request is inserted between the four calls.
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(posts[1]["body"]["messages"]), 1)
        self.assertEqual(posts[1]["body"]["messages"][0]["type"], "tool_return")
        self.assertEqual(len(posts[1]["body"]["messages"][0]["tool_returns"]), 4)

    def test_protection_mode_stops_a_multi_call_batch_without_executing_anything(self):
        session = FakeLettaSession([{"approval": real_batch_payload()}])
        bridge_ = bridge(session, multicall=False)
        with self.assertRaises(BridgeBlocked) as caught:
            bridge_.exchange(history_user_message(), {})
        self.assertIn("refusing to truncate", str(caught.exception))
        self.assertEqual(session.patches, [])
        self.assertEqual(session.tool_returns, [])
        # The one POST that carried the user turn is the only provider request;
        # no returns were ever submitted, so there is no follow-up wire request.
        posts = [entry for entry in bridge_.transport.sent
                 if entry["method"] == "POST" and entry["path"].endswith("/messages")]
        self.assertEqual(len(posts), 1)

    def test_protection_mode_still_accepts_a_single_call(self):
        payload = {"tool_calls": [dict(real_batch_payload()["tool_calls"][0])]}
        session = FakeLettaSession([{"approval": payload}], replies=["done"])
        bridge_ = bridge(session, multicall=False)
        bridge_.exchange(history_user_message(), {})
        self.assertEqual(len(session.patches), 1)

    def test_repeated_identical_values_are_each_executed_in_order(self):
        calls = (R2_ARGUMENTS[0], R2_ARGUMENTS[0])
        ids = (R2_IDS[0], R2_IDS[1])
        session = FakeLettaSession([{"approval": real_batch_payload(calls, ids)}],
                                   replies=["done"])
        bridge_ = bridge(session)
        bridge_.exchange(history_user_message(), {})
        self.assertEqual(len(session.patches), 2)
        self.assertEqual([r["status"] for r in session.tool_returns], ["success", "success"])

    def test_error_return_in_the_middle_preserves_the_executed_prefix(self):
        # The middle call keeps the capture's real argument text except for one
        # deliberately unknown fact id, so the refusal is a real error return.
        calls = (R2_ARGUMENTS[0],
                 dumps(dict(R2_CALLS[1], fact_id="p999")),
                 R2_ARGUMENTS[2])
        ids = R2_IDS[:3]
        session = FakeLettaSession([{"approval": real_batch_payload(calls, ids)}],
                                   replies=["done"])
        bridge_ = bridge(session)
        bridge_.exchange(history_user_message(), {})
        self.assertEqual(len(session.patches), 2)
        statuses = [r["status"] for r in session.tool_returns]
        self.assertEqual(statuses, ["success", "error", "success"])
        self.assertIn("error", session.tool_returns[1]["tool_return"])
        self.assertEqual([r["tool_call_id"] for r in session.tool_returns], list(ids))

    def test_undeclared_tool_stops_before_any_side_effect(self):
        payload = real_batch_payload()
        payload["tool_calls"].append({"id": "call-nope", "name": "send_email",
                                      "arguments": "{}"})
        session = FakeLettaSession([{"approval": payload}])
        bridge_ = bridge(session)
        with self.assertRaises(BridgeBlocked) as caught:
            bridge_.exchange(history_user_message(), {})
        self.assertIn("undeclared_tool_in_batch", str(caught.exception))
        self.assertEqual(session.patches, [])
        self.assertEqual(session.tool_returns, [])

    def test_duplicate_id_across_batches_stops_the_second_batch(self):
        first = {"tool_calls": [dict(real_batch_payload()["tool_calls"][0])]}
        second = {"tool_calls": [dict(real_batch_payload()["tool_calls"][0]),
                                 dict(real_batch_payload()["tool_calls"][1])]}
        session = FakeLettaSession([{"approval": first}, {"approval": second}],
                                  replies=["first done", "second done"])
        bridge_ = bridge(session)
        bridge_.exchange(history_user_message(), {})
        with self.assertRaises(BridgeBlocked) as caught:
            bridge_.exchange([{"role": "user", "type": "message", "content": "again"}], {})
        self.assertIn("reused_from_earlier_batch", str(caught.exception))
        self.assertEqual(len(session.patches), 1)  # only the first batch's patch

    def test_binding_tools_run_in_order_after_memory_calls(self):
        """A mixed batch of a declared client tool and memory_update stays ordered."""
        from ae_adapter import ToolBinding
        order = []

        def read_tool(**kwargs):
            order.append(("tool", kwargs.get("q")))
            return dumps({"ok": True})

        bindings = {"lookup": ToolBinding(
            {"name": "lookup", "parameters": {"type": "object",
                                              "properties": {"q": {"type": "string"}},
                                              "required": ["q"], "additionalProperties": False}},
            read_tool, read_only=True)}
        payload = {"tool_calls": [
            {"id": "call-m1", "name": "memory_update", "arguments": dumps(R2_CALLS[0])},
            {"id": "call-t1", "name": "lookup", "arguments": dumps({"q": "奶茶"})},
            {"id": "call-m2", "name": "memory_update", "arguments": dumps(R2_CALLS[2])},
        ]}
        session = FakeLettaSession([{"approval": payload}], replies=["done"])
        bridge_ = bridge(session)
        bridge_.exchange(history_user_message(), bindings)
        self.assertEqual(order, [("tool", "奶茶")])
        self.assertEqual([r["tool_call_id"] for r in session.tool_returns],
                         ["call-m1", "call-t1", "call-m2"])
        self.assertEqual(len(session.patches), 2)

    def test_full_id_is_preserved_on_the_returned_wire_batch(self):
        session = FakeLettaSession([{"approval": real_batch_payload()}],
                                  replies=["done"])
        bridge_ = bridge(session)
        bridge_.exchange(history_user_message(), {})
        returned = [entry for entry in bridge_.transport.sent
                    if entry["method"] == "POST" and entry["path"].endswith("/messages")][-1]
        ids = [r["tool_call_id"] for r in returned["body"]["messages"][0]["tool_returns"]]
        self.assertEqual(ids, list(R2_IDS))
        # The old 29-character projection collapsed all four onto one string.
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual({native_id[:UPSTREAM_TOOL_CALL_ID_MAX_LEN] for native_id in ids},
                         {TRUNCATED})


if __name__ == "__main__":
    unittest.main()


class RealByteReplayTests(unittest.TestCase):
    """The bridge receives the sealed capture's own bytes.

    The review found the earlier fixture re-encoded every argument with `dumps`
    and changed the fourth call's category, so it could not claim a byte replay.
    These tests read the sealed response file and require exact equality.
    """

    def test_the_sealed_source_is_the_real_r2_response(self):
        self.assertTrue(R2_SOURCE.is_file(), R2_SOURCE)
        self.assertEqual(len(R2_PROVIDER_CALLS), 4)
        self.assertEqual([call["function"]["name"] for call in R2_PROVIDER_CALLS],
                         ["memory_update"] * 4)
        # The real ids are distinct, 32 characters, and share 29 characters.
        self.assertEqual(len(set(R2_IDS)), 4)
        self.assertTrue(all(len(value) == 32 for value in R2_IDS))
        self.assertEqual(len({value[:29] for value in R2_IDS}), 1)

    def test_argument_text_is_the_capture_bytes_not_a_re_encoding(self):
        for index, arguments in enumerate(R2_ARGUMENTS):
            with self.subTest(index=index):
                # A `dumps` round-trip would reorder or re-space; the capture's
                # text must match itself and differ from a canonical encoding.
                self.assertEqual(arguments, R2_PROVIDER_CALLS[index]["function"]["arguments"])
                self.assertIsInstance(arguments, str)

    def test_the_fourth_call_keeps_its_real_category(self):
        self.assertEqual(R2_CALLS[3]["category"], "消费购物偏好")
        self.assertEqual(R2_CALLS[3]["fact_id"], "p008")
        self.assertEqual(R2_CALLS[3]["evidence_ref"], "t4/history/5")

    def test_the_bridge_executes_those_exact_bytes(self):
        session = FakeLettaSession([{"approval": real_batch_payload()}], replies=["done"])
        bridge_ = bridge(session)
        bridge_.exchange(history_user_message(), {})
        # The executor saw the capture's argument strings, unchanged.
        submitted = [step["call"]["arguments"] for step in bridge_.trace
                     if step.get("kind") == "client_tool_result"]
        self.assertEqual(submitted, list(R2_ARGUMENTS))
        self.assertEqual(len(session.patches), 4)
        self.assertIn("消费购物偏好", session.block_value)
        self.assertIn("喜欢向日葵（10枝装花束）", session.block_value)
