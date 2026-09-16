"""Scripted protocol fixtures, NOT responses from a model or live server."""
from copy import deepcopy
import json
import unittest

from ae_adapter import (
    BLOCK_LABEL, MEMORY_TOOL, SYSTEM, BridgeBlocked, LettaBridge, MemoryPolicy,
    ToolBinding, UpdateRejected, assert_separate_arms, creation_payload, dumps,
    vita_bindings,
)


FACTS = {"p000": {"category": "饮食偏好", "content": "奶茶偏好5分糖"}}


def edit(value="奶茶偏好7分糖", *, ref="t4/history/0", op="replace", fid="p000"):
    return {"operation": op, "fact_id": fid, "category": "饮食偏好",
            "content": value, "evidence_ref": ref}


def call(cid, name="memory_update", args=None):
    return {"tool_call_id": cid, "name": name, "arguments": dumps(edit() if args is None else args)}


def approval(*calls, legacy=False):
    msg = {"message_type": "approval_request_message"}
    msg["tool_call" if legacy else "tool_calls"] = calls[0] if legacy else list(calls)
    return {"messages": [msg], "stop_reason": {"stop_reason": "requires_approval"}, "usage": {}}


def done(content="fixture response, not model output"):
    return {"messages": [{"message_type": "assistant_message", "content": content}],
            "stop_reason": {"stop_reason": "end_turn"}, "usage": {}}


class FakeTransport:
    """Models API data shape only; never executes HTTP or model calls."""
    def __init__(self, memory, responses=(), *, aid="agent-fixture-R", bid="block-fixture-R"):
        self.responses = list(deepcopy(responses))
        self.requests = []
        self.fail_patch = False
        self.fail_post = False
        self.bad_patch_echo = False
        self.state = {
            "id": aid, "agent_type": "letta_v1_agent", "message_buffer_autoclear": False,
            "enable_sleeptime": False, "tools": [], "sources": [], "tags": [],
            "blocks": [{"id": bid, "label": BLOCK_LABEL, "value": memory.block_text}],
            "message_ids": ["system-fixture"], "pending_approval": None, "managed_group": None,
        }

    def request(self, method, path, body=None):
        self.requests.append((method, path, deepcopy(body)))
        if method == "GET":
            return deepcopy(self.state)
        if method == "PATCH":
            if self.fail_patch:
                raise ConnectionError("fixture uncertain PATCH")
            self.state["blocks"][0]["value"] = body["value"]
            result = deepcopy(self.state["blocks"][0])
            if self.bad_patch_echo:
                result["value"] = "unexpected"
            return result
        if method != "POST":
            raise AssertionError(method)
        if self.fail_post:
            raise ConnectionError("fixture uncertain POST")
        response = self.responses.pop(0)
        for _ in body["messages"] + response["messages"]:
            self.state["message_ids"].append(f"m{len(self.state['message_ids'])}")
        return response


def bridge(arm="rewrite", responses=()):
    memory = MemoryPolicy(arm, FACTS, block_char_limit=4000)
    transport = FakeTransport(memory, responses, aid=f"agent-{arm}", bid=f"block-{arm}")
    b = LettaBridge(transport, f"agent-{arm}", memory, max_rounds=8, max_steps=4)
    b.visible_refs.add("t4/history/0")
    return b, transport


def public_task(number=4):
    return {"number": number, "subtask_id": f"sub_U000828_{number}", "domain": "delivery",
            "current_time": "2024-06-23", "instruction": "测试任务，不是真实模型执行",
            "history": [{"ref": f"t{number}/history/0", "record": {
                "date": "2024-06-03", "behavior": [],
                "dialogue": [{"role": "user", "content": "fixture history"}]}}]}


class MemoryTests(unittest.TestCase):
    def test_two_updates_keep_erratum_block_and_do_not_patch(self):
        original = deepcopy(FACTS)
        p = MemoryPolicy("erratum", original, block_char_limit=4000)
        original["p000"]["content"] = "caller mutated source"
        patches = []
        for text in ("奶茶偏好7分糖", "奶茶偏好不另外加糖"):
            result = json.loads(p.update(edit(text), {"t4/history/0"}, patches.append))
            self.assertEqual(result["update"]["content"], text)
            self.assertIn("[STATE UPDATE]", result["erratum"])
        self.assertEqual(patches, [])
        self.assertEqual(p.block_text, dumps(FACTS))
        self.assertEqual(len(p.writes), 2)
        self.assertEqual(p.facts["p000"]["content"], "奶茶偏好不另外加糖")

    def test_rewrite_echo_contains_new_value_but_no_extra_erratum(self):
        p = MemoryPolicy("rewrite", FACTS, block_char_limit=4000)
        patches = []
        result = json.loads(p.update(edit(), {"t4/history/0"}, patches.append))
        self.assertEqual(p.block_text, patches[-1])
        self.assertNotIn("erratum", result)
        self.assertEqual(result["memory_block"]["p000"]["content"], "奶茶偏好7分糖")

    def test_future_evidence_is_not_visible(self):
        p = MemoryPolicy("rewrite", FACTS, block_char_limit=4000)
        with self.assertRaises(UpdateRejected):
            p.update(edit(ref="t12/history/21"), {"t4/history/0"}, lambda _: self.fail())
        self.assertEqual(p.block_text, dumps(FACTS))
        self.assertEqual(p.writes, [])

    def test_bad_semantic_edit_is_not_silently_replaced_with_gold(self):
        p = MemoryPolicy("erratum", FACTS, block_char_limit=4000)
        p.update(edit("模型可能判断错的任意内容"), {"t4/history/0"}, lambda _: self.fail())
        self.assertEqual(p.facts["p000"]["content"], "模型可能判断错的任意内容")

    def test_add_delete_stable_ids_and_no_old_block_string_match(self):
        p = MemoryPolicy("erratum", FACTS, block_char_limit=4000)
        refs = {"t4/history/0"}
        p.update(edit("", op="delete"), refs, lambda _: self.fail())
        self.assertNotIn("p000", p.facts)
        self.assertIn("p000", p.block_text)
        added = json.loads(p.update(edit("新偏好", op="add", fid=""), refs, lambda _: self.fail()))
        self.assertEqual(added["update"]["fact_id"], "p001")
        p.update(edit("又更新", fid="p001"), refs, lambda _: self.fail())
        self.assertEqual(p.facts["p001"]["content"], "又更新")

    def test_limit_rejection_is_symmetric_and_no_truncation(self):
        for arm in ("rewrite", "erratum"):
            p = MemoryPolicy(arm, FACTS, block_char_limit=100)
            with self.assertRaises(UpdateRejected):
                p.update(edit("长" * 500), {"t4/history/0"}, lambda _: self.fail())
            self.assertEqual(p.writes, [])

    def test_patch_exception_does_not_commit_local_success(self):
        p = MemoryPolicy("rewrite", FACTS, block_char_limit=4000)
        def fail(_): raise ConnectionError("fixture")
        with self.assertRaises(ConnectionError):
            p.update(edit(), {"t4/history/0"}, fail)
        self.assertEqual(p.facts, FACTS)
        self.assertEqual(p.writes, [])

    def test_rejects_null_unknown_target_and_wrong_category(self):
        for args in (edit("a\x00b"), edit(fid="p999"), dict(edit(), category="wrong")):
            p = MemoryPolicy("erratum", FACTS, block_char_limit=4000)
            with self.assertRaises(UpdateRejected):
                p.update(args, {"t4/history/0"}, lambda _: self.fail())

    def test_creation_has_no_implicit_memory_backends(self):
        p = MemoryPolicy("rewrite", FACTS, block_char_limit=4000)
        data = creation_payload(name="fixture", model="test/not-a-real-model", profile={}, memory=p)
        for field in ("include_base_tools", "include_base_tool_rules", "include_multi_agent_tools",
                      "include_default_source", "enable_sleeptime", "message_buffer_autoclear"):
            self.assertIs(data[field], False)
        self.assertEqual(data["initial_message_sequence"], [])
        self.assertEqual(data["memory_blocks"][0]["value"], dumps(FACTS))


class BridgeTests(unittest.TestCase):
    def test_patch_before_real_tool_return_and_client_schemas_resent(self):
        b, t = bridge(responses=[approval(call("c1")), done()])
        b.exchange([{"role": "user", "content": "fixture"}], {})
        writes = [r for r in t.requests if r[0] != "GET"]
        self.assertEqual([r[0] for r in writes], ["POST", "PATCH", "POST"])
        self.assertIn("/core-memory/blocks/ae_preferences", writes[1][1])
        ret = writes[2][2]["messages"][0]
        self.assertEqual(ret["type"], "tool_return")
        self.assertEqual(ret["tool_returns"][0]["tool_call_id"], "c1")
        self.assertEqual(writes[0][2]["client_tools"], writes[2][2]["client_tools"])
        self.assertTrue(writes[0][2]["include_compaction_messages"])
        self.assertIn("include=agent.tools", t.requests[0][1])

    def test_erratum_never_sends_patch_and_supports_second_update(self):
        b, t = bridge("erratum", [approval(call("c1")), done(),
                                  approval(call("c2", args=edit("奶茶偏好不另外加糖"))), done()])
        for _ in range(2): b.exchange([{"role": "user", "content": "fixture"}], {})
        self.assertFalse(any(r[0] == "PATCH" for r in t.requests))
        self.assertEqual(t.state["blocks"][0]["value"], dumps(FACTS))

    def test_legacy_tool_call_shape(self):
        b, _ = bridge(responses=[approval(call("c1"), legacy=True), done()])
        b.exchange([{"role": "user", "content": "fixture"}], {})
        self.assertEqual(len(b.memory.writes), 1)

    def test_invalid_proposal_returns_error_without_gold_repair(self):
        b, t = bridge(responses=[approval(call("c1", args=edit(ref="future"))), done()])
        b.exchange([{"role": "user", "content": "fixture"}], {})
        returns = [r for r in t.requests if r[0] == "POST"][-1][2]["messages"][0]["tool_returns"]
        self.assertEqual(returns[0]["status"], "error")
        self.assertEqual(b.memory.writes, [])
        self.assertFalse(b.blocked)

    def test_unknown_tools_not_executed(self):
        b, t = bridge(responses=[approval(call("c1", "core_memory_replace", {})), done()])
        b.exchange([{"role": "user", "content": "fixture"}], {})
        ret = [r for r in t.requests if r[0] == "POST"][-1][2]["messages"][0]["tool_returns"][0]
        self.assertEqual(ret["status"], "error")

    def test_no_update_is_not_replaced_by_scripted_update(self):
        b, _ = bridge(responses=[done()])
        b.exchange([{"role": "user", "content": "fixture"}], {})
        self.assertEqual(b.memory.writes, [])

    def test_patch_failure_blocks_without_success_tool_return_or_retry(self):
        for failure in ("fail_patch", "bad_patch_echo"):
            b, t = bridge(responses=[approval(call("c1")), done()])
            setattr(t, failure, True)
            with self.assertRaises(BridgeBlocked):
                b.exchange([{"role": "user", "content": "fixture"}], {})
            self.assertEqual(sum(r[0] == "POST" for r in t.requests), 1)
            self.assertEqual(b.memory.writes, [])
            self.assertTrue(b.blocked)

    def test_compaction_and_nonterminal_stop_are_not_success(self):
        cases = [{"messages": [{"message_type": "summary_message", "summary": "compressed"}],
                  "stop_reason": {"stop_reason": "end_turn"}},
                 {"messages": [{"message_type": "event_message", "event_type": "compaction"}],
                  "stop_reason": {"stop_reason": "end_turn"}},
                 {"messages": [], "stop_reason": {"stop_reason": "max_steps"}}]
        for response in cases:
            b, _ = bridge(responses=[response])
            with self.assertRaises(BridgeBlocked):
                b.exchange([{"role": "user", "content": "fixture"}], {})

    def test_duplicate_batch_refused_before_any_side_effect(self):
        b, t = bridge(responses=[approval(call("same"), call("same"))])
        with self.assertRaises(BridgeBlocked):
            b.exchange([{"role": "user", "content": "fixture"}], {})
        self.assertEqual(b.memory.writes, [])
        self.assertFalse(any(r[0] == "PATCH" for r in t.requests))

    def test_summary_disguised_as_user_message_is_blocked_on_first_post(self):
        response = done()
        response["messages"].insert(0, {"message_type": "user_message", "content": "packed summary"})
        b, _ = bridge(responses=[response])
        with self.assertRaisesRegex(BridgeBlocked, "unexpected user-message"):
            b.exchange([{"role": "user", "content": "fixture"}], {})

    def test_verbatim_input_echo_is_allowed(self):
        response = done()
        response["messages"].insert(0, {"message_type": "user_message", "content": "fixture"})
        b, _ = bridge(responses=[response])
        b.exchange([{"role": "user", "content": "fixture"}], {})

    def test_unknown_pending_call_stops_before_new_user_post(self):
        b, t = bridge()
        t.state["pending_approval"] = {"tool_call": call("unowned")}
        with self.assertRaisesRegex(BridgeBlocked, "unexpected pending approval"):
            b.exchange([{"role": "user", "content": "fixture"}], {})
        self.assertFalse(any(r[0] == "POST" for r in t.requests))

    def test_uncertain_post_cannot_be_retried(self):
        b, t = bridge()
        t.fail_post = True
        for _ in range(2):
            with self.assertRaises(BridgeBlocked):
                b.exchange([{"role": "user", "content": "fixture"}], {})
        self.assertEqual(sum(r[0] == "POST" for r in t.requests), 1)

    def test_cannot_forge_tool_return(self):
        b, t = bridge()
        with self.assertRaises(BridgeBlocked): b.exchange([{"type": "tool_return", "tool_returns": []}], {})
        self.assertEqual(t.requests, [])

    def test_missing_relations_or_extra_tools_are_not_safe_defaults(self):
        for mutate in (lambda s: s.pop("tools"), lambda s: s.update(tools=[{"name": "memory_replace"}]),
                       lambda s: s.update(message_buffer_autoclear=True)):
            b, t = bridge()
            mutate(t.state)
            with self.assertRaises(BridgeBlocked): b.verify_session()

    def test_external_block_mutation_and_history_reset_detected(self):
        b, t = bridge(responses=[done()])
        b.exchange([{"role": "user", "content": "fixture"}], {})
        t.state["message_ids"] = ["new-system"]
        with self.assertRaises(BridgeBlocked): b.verify_session()
        b, t = bridge("erratum")
        t.state["blocks"][0]["value"] = "someone rewrote E"
        with self.assertRaises(BridgeBlocked): b.verify_session()

    def test_tool_dispatch_uses_actual_supplied_environment_interface(self):
        class Tool:
            openai_schema = {"type": "function", "function": {
                "name": "fixture_search", "description": "mock", "parameters": {"type": "object"}}}
        class Env:
            def __init__(self): self.calls = []
            def get_tools(self): return [Tool()]
            def make_tool_call(self, **kwargs):
                self.calls.append(kwargs)
                return "actual return from TEST DOUBLE, not Vita runtime"
        env = Env()
        b, t = bridge(responses=[approval(call("c1", "fixture_search", {"keywords": ["奶茶"]})), done()])
        b.exchange([{"role": "user", "content": "fixture"}], vita_bindings(env))
        self.assertEqual(env.calls, [{"tool_name": "fixture_search", "requestor": "assistant", "keywords": ["奶茶"]}])
        ret = [r for r in t.requests if r[0] == "POST"][-1][2]["messages"][0]["tool_returns"][0]
        self.assertIn("TEST DOUBLE", ret["tool_return"])

    def test_contiguous_stages_preserve_one_session_without_refreshing_gold(self):
        b, t = bridge(responses=[done(), done(), done(), done()])
        b.visible_refs.clear()
        one = b.run_stage(public_task(4), bindings={}, domain_policy="fixture policy")
        two = b.run_stage(public_task(5), bindings={}, domain_policy="fixture policy")
        self.assertIsNone(one["task_success"])
        self.assertIsNone(two["task_success"])
        self.assertEqual(b.memory.writes, [])
        posts = [r for r in t.requests if r[0] == "POST"]
        self.assertEqual(len({r[1] for r in posts}), 1)
        self.assertEqual(len(posts), 4)
        self.assertEqual(len(posts[0][2]["client_tools"]), 1)
        self.assertEqual(json.loads(posts[1][2]["messages"][0]["content"])["source"], "current_task")

    def test_rejects_skipped_task_and_raw_task_gold(self):
        for task in (public_task(12), dict(public_task(), evaluation_criteria={"gold": "no"})):
            b, t = bridge()
            b.visible_refs.clear()
            with self.assertRaises(BridgeBlocked): b.run_stage(task, bindings={}, domain_policy="fixture")
            self.assertEqual(t.requests, [])

    def test_arm_independence(self):
        a, _ = bridge("rewrite")
        b, _ = bridge("erratum")
        a.verify_session(); b.verify_session()
        assert_separate_arms(a, b)
        b.block_id = a.block_id
        with self.assertRaises(BridgeBlocked): assert_separate_arms(a, b)


if __name__ == "__main__":
    unittest.main()
