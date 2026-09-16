"""Driver protocol fixtures: no real model, server, or task-score evidence."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from ae_adapter import BLOCK_LABEL, BridgeBlocked, MemoryPolicy, dumps
from ae_task_run import (TaskBridge, build_plan, environment_bindings, execute_wiring,
                         final_public_text, task_payload, validate_config)
from test_ae_adapter import FakeTransport, call, approval, done, public_task, FACTS


def config():
    return json.loads((Path(__file__).resolve().parents[1] / "configs/ae-01__task-wiring.local.json").read_text())


def sample():
    return {"initial_facts": deepcopy(FACTS), "initial_profile": {},
            "tasks": [public_task(4), public_task(5)], "source": {"fixture": True}}


class Services:
    def __init__(self, cfg, stop=True):
        self.config, self.stop, self.requests, self.agents = cfg, stop, [], {}

    def request(self, method, path, body=None):
        self.requests.append((method, path, deepcopy(body)))
        if path == "/v1/models":
            return {"data": [{"id": "Qwen3-8B", "max_model_len": self.config["context_window"]}]}
        if path == "/v1/health/":
            return {"status": "ok", "version": "0.16.8"}
        if method == "POST" and path == "/v1/agents/":
            aid = f"agent-fixture-{len(self.agents)}"
            arm = "rewrite" if "rewrite" in body["name"] else "erratum"
            memory = MemoryPolicy(arm, FACTS, block_char_limit=8000)
            responses = [done("history fixture"), done("###STOP###" if self.stop else "user-facing fixture"),
                         done("history fixture"), done("###STOP###" if self.stop else "user-facing fixture")]
            transport = FakeTransport(memory, responses, aid=aid, bid=f"block-{aid}")
            transport.state.update({"embedding": None, "embedding_config": None,
                "llm_config": {"handle": self.config["model_handle"], "model": "Qwen3-8B",
                    "model_endpoint_type": "openai", "model_endpoint": self.config["model_origin"] + "/v1",
                    "context_window": self.config["context_window"], "max_tokens": self.config["max_output_tokens"],
                    "temperature": self.config["temperature"], "parallel_tool_calls": False, "strict": False}})
            self.agents[aid] = transport
            return {"id": aid}
        aid = path.split("/")[3].split("?")[0]
        return self.agents[aid].request(method, path, body)


class Environment:
    def get_tools(self):
        return [SimpleNamespace(openai_schema={"type": "function", "function": {
            "name": "read_item", "description": "fixture", "parameters": {"type": "object", "properties": {}}}})]

    def make_tool_call(self, tool_name, requestor, **kwargs):
        return {"result": "fixture native JSON conversion"}

    def to_json_str(self, value):
        return dumps(value)


class Runtime:
    def __init__(self):
        self.started, self.finished, self.aborted, self.replies = [], [], False, []

    def start(self, task):
        self.started.append(task["number"])
        return {"environment": Environment(), "domain_policy": "fixture policy",
                "instruction": task["instruction"], "greeting": {"role": "assistant", "content": "fixture greeting"}}

    def agent_stop(self, text):
        return "###STOP###" in text

    def user_reply(self, text):
        self.replies.append(text)
        return {"content": "###STOP###", "stop": True, "raw": {"fixture": True}}

    def finish(self, task, transcript, termination_reason, duration):
        self.finished.append((task["number"], deepcopy(transcript), termination_reason))
        return {"reward_info": {"reward": 0}, "scientific_success": None, "fixture": True}

    def snapshot(self):
        return {"fixture": True}

    def abort(self):
        self.aborted = True


class DriverTests(unittest.TestCase):
    def test_plan_is_public_projection_not_gold(self):
        data = sample()
        data["private_tasks"] = {"gold_canary": "NEVER_SEND"}
        plan = build_plan(config(), data)
        self.assertFalse(plan["network_called"])
        self.assertNotIn("NEVER_SEND", json.dumps(plan))

    def test_scope_and_character_guard_cannot_silently_expand(self):
        for patch in ({"end_turn": 12}, {"max_tool_return_chars": 26215}, {"max_rounds": True},
                      {"model_origin": "https://example.org"}):
            with self.assertRaises(ValueError):
                validate_config(dict(config(), **patch))

    def test_equal_initial_payload_not_shared_memory(self):
        a, m1 = task_payload(config(), sample(), "rewrite", "r")
        b, m2 = task_payload(config(), sample(), "erratum", "e")
        self.assertEqual(a["memory_blocks"], b["memory_blocks"])
        self.assertIsNot(m1, m2)
        self.assertNotIn("enable_thinking", dumps(a))

    def test_final_response_ignores_intermediate_stop(self):
        self.assertEqual(final_public_text([{"content": "intermediate ###STOP###"}, {"content": "final"}]), "final")
        with self.assertRaises(BridgeBlocked):
            final_public_text([{"content": "\n"}])

    def test_native_return_serialization(self):
        binding = environment_bindings(Environment())["read_item"]
        self.assertEqual(json.loads(binding.call())["result"], "fixture native JSON conversion")

    def test_paired_tasks_keep_agents_and_do_not_promote_judge_score(self):
        cfg, runtimes, events = config(), {}, []
        services = Services(cfg)
        def factory(arm):
            runtimes[arm] = Runtime()
            return runtimes[arm]
        result = execute_wiring(cfg, sample(), model_transport=services, letta_transport=services,
                                runtime_factory=factory, emit=events.append)
        self.assertEqual(result["invalid_reasons"], [])
        self.assertTrue(result["wiring_completed"])
        self.assertFalse(result["validity_passed"])
        self.assertIsNone(result["scientific_result"])
        self.assertEqual(len(services.agents), 2)
        for native in runtimes.values():
            self.assertEqual(native.started, [4, 5])
            self.assertEqual(len(native.finished), 2)
            self.assertEqual(native.replies, [])
            for _, transcript, reason in native.finished:
                self.assertEqual(reason, "agent_stop")
                self.assertNotIn("history fixture", dumps(transcript))
                self.assertEqual(sum(m["role"] == "user" for m in transcript), 1)

    def test_user_stop_is_not_sent_to_agent_as_next_task(self):
        cfg, runtime = config(), Runtime()
        services = Services(cfg, stop=False)
        result = execute_wiring(cfg, sample(), model_transport=services, letta_transport=services,
                                runtime_factory=lambda _: Runtime(), emit=lambda _: None)
        self.assertTrue(result["wiring_completed"])
        for arm in result["arms"].values():
            self.assertEqual([s["termination_reason"] for s in arm["stages"]], ["user_stop"] * 2)
            self.assertEqual([s["model_posts"] for s in arm["stages"]], [2, 2])

    def test_last_allowed_reply_can_stop_normally(self):
        cfg = dict(config(), max_user_exchanges=1)
        services = Services(cfg)
        original_request = services.request
        def request(method, path, body=None):
            out = original_request(method, path, body)
            if method == "POST" and path == "/v1/agents/":
                services.agents[out["id"]].responses = [done("history"), done("question"), done("###STOP###"),
                                                         done("history"), done("question"), done("###STOP###")]
            return out
        services.request = request
        class OneReply(Runtime):
            def user_reply(self, text):
                return {"content": "answer", "stop": False, "raw": {}}
        result = execute_wiring(cfg, sample(), model_transport=services, letta_transport=services,
                                runtime_factory=lambda _: OneReply(), emit=lambda _: None)
        self.assertTrue(result["wiring_completed"], result["invalid_reasons"])

    def test_snapshot_error_still_aborts(self):
        cfg, bad = config(), Runtime()
        def snapshot():
            raise RuntimeError("fixture snapshot error")
        bad.snapshot = snapshot
        services = Services(cfg)
        result = execute_wiring(cfg, sample(), model_transport=services, letta_transport=services,
                                runtime_factory=lambda _: bad, emit=lambda _: None)
        self.assertTrue(bad.aborted)
        self.assertFalse(result["wiring_completed"])
        self.assertEqual(result["status"], "INVALID")

    def test_real_call_id_and_full_oversized_return_saved_before_stop(self):
        cfg, events = dict(config(), max_tool_return_chars=10), []
        memory = MemoryPolicy("erratum", FACTS, block_char_limit=8000)
        transport = FakeTransport(memory, [approval(call("fixture-c1", "read_item", {}))])
        bridge = TaskBridge(transport, "fixture", memory, max_rounds=2, max_steps=3, config=cfg, emit=events.append)
        transport.state["id"] = "fixture"
        bridge.begin(public_task(), {"role": "assistant", "content": "greet"})
        bridge.phase = "task"
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": dumps({"source": "runtime_user", "content": "test"})}],
                            environment_bindings(Environment()))
        tools = [x for x in bridge.transcript if x["role"] == "tool"]
        self.assertEqual(tools[0]["id"], "fixture-c1")
        self.assertGreater(len(tools[0]["content"]), 10)
        self.assertEqual(sum(r[0] == "POST" for r in transport.requests), 1)


if __name__ == "__main__":
    unittest.main()
