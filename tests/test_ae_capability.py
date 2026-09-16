"""Offline fixtures for the AE-01 capability probe; no model, server, or GPU.

These tests exercise construction, tool gating, the private order oracle and the
driver's separation of execution / input-audit / oracle / confirmation review.
No text here is a model output or task-success evidence.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from ae_adapter import MemoryPolicy, dumps
from ae_capability import (
    CAPABILITY_SYSTEM, EVIDENCE_DIALOGUE_INDEX, EVIDENCE_REF, EVIDENCE_TEXT,
    EXPECTED_SUGAR, NEW_SUGAR_FACT, OLD_SUGAR_FACT, TARGET_PRODUCT_ID,
    build_capability_inputs, build_plan, capability_payload, catalog_sugar,
    diagnose_orders, execute_capability, validate_config,
)
from ae_task_run import TaskBridge
from test_ae_adapter import FakeTransport, approval, call, done

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import ae_01_capability_probe as probe_mod  # noqa: E402

WORK_ADDRESS = "甘肃省兰州市安宁区安宁西路地五大道88号"
PROFILE = {"user_id": "U000828", "职业": "服装店老板", "工作地址": WORK_ADDRESS,
           "常住住址": "甘肃省兰州市安宁区安宁西路381号飞天世纪新城3幢2单元603"}
FACTS = {
    "p000": {"category": "饮食偏好", "content": OLD_SUGAR_FACT},
    "p001": {"category": "饮食偏好", "content": "不吃香菜"},
    "p002": {"category": "消费购物偏好", "content": "喜欢向日葵"},
}
FUTURE_INSTRUCTION = "FUTURE_T5_INSTRUCTION_MUST_NOT_LEAK"


def config():
    return json.loads((REPO_ROOT / "configs/ae-01__capability__qwen3-4b.local.json").read_text())


def evidence_history():
    history = [{"ref": f"t4/history/{i}", "record": {
        "date": "2024-05-10", "behavior": [],
        "dialogue": [{"role": "user", "content": f"fixture history {i}"}]}} for i in range(15)]
    history.append({"ref": EVIDENCE_REF, "record": {
        "date": "2024-06-03", "behavior": [],
        "dialogue": [
            {"role": "user", "content": "感觉7分糖比5分糖甜，好像更好喝一点呢！"},
            {"role": "assistant", "content": "以后奶茶都默认给你点7分糖？"},
            {"role": "user", "content": EVIDENCE_TEXT},
        ]}})
    return history


def public_task(number):
    instruction = ("快给我买个联名款的奶茶送店里来，啥口味都行！" if number == 4
                   else FUTURE_INSTRUCTION)
    return {"number": number, "subtask_id": f"sub_U000828_{number}", "domain": "delivery",
            "current_time": "2024-06-23", "instruction": instruction,
            "history": evidence_history() if number == 4 else []}


def sample():
    return {"initial_facts": deepcopy(FACTS), "initial_profile": deepcopy(PROFILE),
            "tasks": [public_task(4), public_task(5)], "source": {"fixture": True}}


def make_bridge(include_memory_tool, responses):
    cfg = config()
    memory = MemoryPolicy("erratum", deepcopy(FACTS), block_char_limit=cfg["block_char_limit"])
    transport = FakeTransport(memory, responses, aid=f"agent-{include_memory_tool}",
                              bid=f"block-{include_memory_tool}")
    bridge = TaskBridge(transport, f"agent-{include_memory_tool}", memory, max_rounds=4,
                        max_steps=3, config=cfg, emit=lambda _: None,
                        include_memory_tool=include_memory_tool)
    bridge.visible_refs.add("t4/user/0")
    return bridge, transport, memory


def fixture_user_message():
    return {"role": "user", "content": dumps({"source": "runtime_user", "ref": "t4/user/1",
                                              "content": "fixture"})}


class InputConstructionTests(unittest.TestCase):
    def test_controlled_snapshot_only_replaces_one_sourced_fact(self):
        inputs = build_capability_inputs(sample())
        control, current = inputs["facts_control"], inputs["facts_current"]
        self.assertEqual(control, FACTS)
        self.assertEqual(len(control), len(current))
        self.assertEqual({k for k in control if control[k] != current[k]}, {"p000"})
        self.assertNotIn(OLD_SUGAR_FACT, [v["content"] for v in current.values()])
        self.assertEqual([v["content"] for v in current.values()].count(NEW_SUGAR_FACT), 1)
        self.assertEqual(current["p000"]["category"], FACTS["p000"]["category"])
        edit = inputs["preference_edit"]
        self.assertEqual(edit["fact_id"], "p000")
        self.assertEqual(edit["evidence_ref"], EVIDENCE_REF)
        self.assertEqual(edit["evidence_dialogue_index"], EVIDENCE_DIALOGUE_INDEX)
        self.assertEqual(edit["evidence_text"], EVIDENCE_TEXT)
        self.assertEqual(edit["evidence_text_sha256"],
                         hashlib.sha256(EVIDENCE_TEXT.encode("utf-8")).hexdigest())
        self.assertTrue(edit["history_evidence_verified"])
        self.assertTrue(inputs["diagnostic_only"])

    def test_history_evidence_check_is_not_swallowed(self):
        for mutate in ("drop", "wrong_text", "wrong_role"):
            data = sample()
            history = data["tasks"][0]["history"]
            if mutate == "drop":
                history[:] = [h for h in history if h["ref"] != EVIDENCE_REF]
            elif mutate == "wrong_text":
                history[-1]["record"]["dialogue"][EVIDENCE_DIALOGUE_INDEX]["content"] = "以后随便吧"
            else:
                history[-1]["record"]["dialogue"][EVIDENCE_DIALOGUE_INDEX]["role"] = "assistant"
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                build_capability_inputs(data)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                build_plan(config(), data)

    def test_agent_input_has_no_old_sugar_future_or_rubric_leak(self):
        plan = build_plan(config(), sample())
        blob = dumps(plan["agent_payload"])
        self.assertNotIn("5分糖", blob)
        self.assertIn(NEW_SUGAR_FACT, blob)  # the read-only current preference is given
        self.assertIn(NEW_SUGAR_FACT, dumps(plan["initial_block"]))
        self.assertNotIn(FUTURE_INSTRUCTION, dumps(plan))
        self.assertNotIn("sub_U000828_5", dumps(plan))
        for forbidden in ("evaluation_criteria", "target_product_ids", "private_tasks", "rubric"):
            self.assertNotIn(forbidden, dumps(plan["inputs"]))
            self.assertNotIn(forbidden, blob)
        self.assertTrue(plan["inputs"]["diagnostic_only"])
        self.assertFalse(plan["network_called"])
        self.assertFalse(plan["model_called"])

    def test_capability_system_has_no_memory_or_history_instructions(self):
        payload, memory = capability_payload(config(), deepcopy(FACTS), PROFILE, name="fixture")
        self.assertTrue(payload["system"].startswith(CAPABILITY_SYSTEM))
        self.assertNotIn("memory_update", payload["system"])
        self.assertNotIn("整理", payload["system"])
        self.assertEqual(memory.initial_block, dumps(FACTS))
        self.assertEqual(payload["memory_blocks"][0]["value"], dumps(FACTS))


class MemoryToolGateTests(unittest.TestCase):
    def test_disabled_bridge_omits_schema_and_refuses_without_execution(self):
        bridge, transport, memory = make_bridge(False, [approval(call("c1")), done("after")])
        bridge.exchange([fixture_user_message()], {})
        first_post = [r for r in transport.requests if r[0] == "POST"][0]
        self.assertEqual(first_post[2]["client_tools"], [])
        returns = [r for r in transport.requests if r[0] == "POST"][-1][2]["messages"][0]["tool_returns"]
        self.assertEqual(returns[0]["status"], "error")
        self.assertEqual(memory.writes, [])
        self.assertFalse(any(r[0] == "PATCH" for r in transport.requests))
        self.assertFalse(bridge.blocked)

    def test_default_bridge_still_includes_memory_update(self):
        bridge, transport, memory = make_bridge(True, [done("after")])
        bridge.exchange([fixture_user_message()], {})
        first_post = [r for r in transport.requests if r[0] == "POST"][0]
        self.assertEqual([t["name"] for t in first_post[2]["client_tools"]], ["memory_update"])


def store_catalog(sugar="7分糖", product_id=TARGET_PRODUCT_ID):
    return {"S1": {"store_id": "S1", "products": [
        {"product_id": product_id, "product_type": "target", "attributes": [f"规格: {sugar}"]}]}}


def target_line(sugar="7分糖", quantity=1, product_id=TARGET_PRODUCT_ID):
    return {"product_id": product_id, "attributes": f"规格: {sugar}", "quantity": quantity}


def good_order(**overrides):
    order = {"order_id": "#DELIVERY#x", "order_type": "delivery", "user_id": "U000828",
             "store_id": "S1", "status": "paid", "location": {"address": WORK_ADDRESS},
             "products": [target_line()]}
    order.update(overrides)
    return order


class OracleTests(unittest.TestCase):
    def diagnose_many(self, orders, *, baseline=(), stores=None):
        return diagnose_orders(orders, baseline_order_ids=set(baseline),
                               stores=store_catalog() if stores is None else stores,
                               work_address=WORK_ADDRESS)

    def diagnose(self, order, *, baseline=(), stores=None):
        return self.diagnose_many({"#DELIVERY#x": order}, baseline=baseline, stores=stores)

    def test_all_criteria_pass_for_the_audited_order(self):
        result = self.diagnose(good_order())
        self.assertTrue(result["oracle_passed"])
        self.assertTrue(all(result["orders"][0]["criteria"].values()))
        self.assertFalse(result["injected_into_agent"])
        self.assertEqual(result["expected"]["expected_sugar"], EXPECTED_SUGAR)
        self.assertEqual(result["new_uncancelled_count"], 1)

    def test_paid_sugar_product_address_and_identity_negatives(self):
        cases = {
            "unpaid": good_order(status="unpaid"),
            "wrong_product": good_order(products=[target_line(product_id="OTHER")]),
            "wrong_sugar": good_order(products=[target_line(sugar="5分糖")]),
            "wrong_address": good_order(location={"address": "别的地方"}),
            "wrong_user": good_order(user_id="U999999"),
        }
        for label, order in cases.items():
            with self.subTest(label=label):
                self.assertFalse(self.diagnose(order)["oracle_passed"], label)
        with self.subTest(label="cancelled"):
            self.assertFalse(self.diagnose(good_order(status="cancelled"))["oracle_passed"])

    def test_extra_unpaid_or_duplicate_orders_fail_but_cancelled_extra_is_ignored(self):
        wrong = dict(good_order(), order_id="#DELIVERY#extra", status="unpaid",
                     products=[target_line(product_id="OTHER")])
        extra = self.diagnose_many({"#DELIVERY#x": good_order(), "#DELIVERY#extra": wrong})
        self.assertFalse(extra["oracle_passed"])
        self.assertEqual(extra["new_uncancelled_count"], 2)
        second_paid = dict(wrong, status="paid")
        self.assertFalse(self.diagnose_many(
            {"#DELIVERY#x": good_order(), "#DELIVERY#extra": second_paid})["oracle_passed"])
        cancelled = dict(wrong, status="cancelled")
        result = self.diagnose_many({"#DELIVERY#x": good_order(), "#DELIVERY#extra": cancelled})
        self.assertTrue(result["oracle_passed"])
        self.assertEqual(result["new_uncancelled_count"], 1)

    def test_extra_item_duplicate_target_or_quantity_over_one_fail(self):
        with_extra_item = good_order(products=[target_line(), target_line(product_id="OTHER")])
        self.assertFalse(self.diagnose(with_extra_item)["oracle_passed"])
        duplicate_target = good_order(products=[target_line(), target_line()])
        self.assertFalse(self.diagnose(duplicate_target)["oracle_passed"])
        quantity_two = good_order(products=[target_line(quantity=2)])
        self.assertFalse(self.diagnose(quantity_two)["oracle_passed"])
        missing_quantity = good_order(products=[{"product_id": TARGET_PRODUCT_ID,
                                                 "attributes": "规格: 7分糖"}])
        self.assertFalse(self.diagnose(missing_quantity)["oracle_passed"])

    def test_pre_existing_order_is_not_newly_created(self):
        result = self.diagnose(good_order(), baseline=["#DELIVERY#x"])
        self.assertFalse(result["oracle_passed"])
        self.assertFalse(result["orders"][0]["criteria"]["newly_created"])

    def test_missing_or_conflicting_line_sugar_never_passes(self):
        missing = good_order(products=[target_line(sugar="")])
        self.assertFalse(self.diagnose(missing)["oracle_passed"])
        # A catalog option is diagnostic only; it cannot rescue a missing line spec.
        self.assertEqual(catalog_sugar(store_catalog(), TARGET_PRODUCT_ID), "7分糖")
        self.assertFalse(self.diagnose(missing, stores=store_catalog())["oracle_passed"])
        conflict = good_order(products=[{"product_id": TARGET_PRODUCT_ID, "quantity": 1,
                                         "attributes": "规格: 7分糖, 备注: 5分糖"}])
        self.assertFalse(self.diagnose(conflict)["oracle_passed"])
        empty = diagnose_orders({}, baseline_order_ids=set(), stores={}, work_address=WORK_ADDRESS)
        self.assertFalse(empty["oracle_passed"])
        self.assertEqual(empty["final_order_count"], 0)


class ConfigTests(unittest.TestCase):
    def test_wrong_values_are_rejected(self):
        cases = {
            "purpose": "real_task_wiring",
            "model_handle": "vllm/Qwen3-8B",
            "expected_model": "Qwen3-8B",
            "context_window": 32768,
            "task_number": 5,
            "max_tool_return_chars": 26215,
            "max_steps": 0,
            "seed": 301,
            "temperature": True,
            "timeout_seconds": 0,
            "max_request_bytes": 1,
        }
        for key, value in cases.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_config(dict(config(), **{key: value}))
        extra = dict(config(), unexpected=1)
        with self.assertRaises(ValueError):
            validate_config(extra)
        missing = {k: v for k, v in config().items() if k != "seed"}
        with self.assertRaises(ValueError):
            validate_config(missing)
        with self.assertRaises(ValueError):
            validate_config(dict(config(), letta_origin="http://127.0.0.1:8000"))

    def test_float_counts_are_rejected_but_numeric_limits_are_allowed(self):
        for key in ("max_steps", "max_rounds", "seed", "context_window",
                    "max_output_tokens", "block_char_limit"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_config(dict(config(), **{key: float(config()[key])}))
        validate_config(dict(config(), temperature=0.0, timeout_seconds=180.0))
        validate_config(dict(config(), temperature=0, timeout_seconds=180))

    def test_validate_returns_an_isolated_copy(self):
        original = config()
        validated = validate_config(original)
        validated["context_window"] = 1
        self.assertEqual(original["context_window"], 65536)

    def test_existing_output_dir_is_refused_without_touching_content(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            out.mkdir()
            sentinel = out / "result.json"
            sentinel.write_text("{}", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                probe_mod.ensure_output_dir(out)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "{}")
            fresh = Path(tmp) / "nested" / "fresh"
            self.assertEqual(probe_mod.ensure_output_dir(fresh), fresh)
            self.assertTrue(fresh.is_dir())


class _Tool:
    def __init__(self, name):
        self.openai_schema = {"type": "function", "function": {
            "name": name, "description": "fixture", "parameters": {"type": "object", "properties": {}}}}


class CapEnv:
    """Scripted native environment double; it is NOT Vita execution."""

    def __init__(self):
        self.tools = SimpleNamespace(db=SimpleNamespace(orders={}, stores=store_catalog()))

    def get_tools(self):
        return [_Tool("create_delivery_order"), _Tool("pay_delivery_order")]

    def make_tool_call(self, tool_name, requestor, **kwargs):
        if tool_name == "create_delivery_order":
            attributes = kwargs.get("attributes") or [""] * len(kwargs["product_ids"])
            counts = kwargs["product_cnts"]
            products = [{"product_id": pid, "name": "fixture", "attributes": attributes[i],
                         "quantity": counts[i]} for i, pid in enumerate(kwargs["product_ids"])]
            self.tools.db.orders["#DELIVERY#fixture"] = {
                "order_id": "#DELIVERY#fixture", "order_type": "delivery",
                "user_id": kwargs["user_id"], "store_id": kwargs["store_id"],
                "location": {"address": kwargs["address"]}, "status": "unpaid",
                "products": products}
            return {"order_id": "#DELIVERY#fixture"}
        if tool_name == "pay_delivery_order":
            self.tools.db.orders[kwargs["order_id"]]["status"] = "paid"
            return {"status": "paid"}
        return {"fixture": True}

    def to_json_str(self, value):
        return dumps(value)


class CapRuntime:
    def __init__(self, env, judge_error=False):
        self.env, self.judge_error = env, judge_error
        self.started, self.finished, self.aborted = [], [], False

    def start(self, task):
        self.started.append(task["number"])
        return {"environment": self.env, "domain_policy": "fixture policy",
                "instruction": task["instruction"],
                "greeting": {"role": "assistant", "content": "fixture greeting"}}

    def agent_stop(self, text):
        return "###STOP###" in text

    def user_reply(self, text):
        return {"content": "###STOP###", "stop": True, "raw": {}}

    def finish(self, task, transcript, termination_reason, duration):
        if self.judge_error:
            raise RuntimeError("fixture judge failure")
        self.finished.append((task["number"], termination_reason))
        return {"reward_info": {"reward": 0.0}, "scientific_success": None,
                "judging_status": "MODEL_JUDGED_DEBUG_ONLY"}

    def snapshot(self):
        return {"fixture": True, "environment_db": {
            "orders": deepcopy(self.env.tools.db.orders),
            "stores": deepcopy(self.env.tools.db.stores)}}

    def abort(self):
        self.aborted = True


class CapServices:
    """Letta/model API shape only; it never executes HTTP or a model call."""

    def __init__(self, cfg, responses):
        self.config, self.responses, self.agents, self.requests = cfg, responses, {}, []

    def request(self, method, path, body=None):
        self.requests.append((method, path, deepcopy(body)))
        if path == "/v1/models":
            return {"data": [{"id": self.config["expected_model"],
                              "max_model_len": self.config["context_window"]}]}
        if path == "/v1/health/":
            return {"status": "ok", "version": "0.16.8"}
        if method == "POST" and path == "/v1/agents/":
            aid = f"agent-{len(self.agents)}"
            memory = MemoryPolicy("erratum", {}, block_char_limit=self.config["block_char_limit"])
            transport = FakeTransport(memory, deepcopy(self.responses), aid=aid, bid=f"block-{aid}")
            transport.state["blocks"][0]["value"] = body["memory_blocks"][0]["value"]
            transport.state.update({
                "embedding": None, "embedding_config": None,
                "llm_config": {"handle": self.config["model_handle"],
                               "model": self.config["expected_model"],
                               "model_endpoint_type": "openai",
                               "model_endpoint": self.config["model_origin"] + "/v1",
                               "context_window": self.config["context_window"],
                               "max_tokens": self.config["max_output_tokens"],
                               "temperature": self.config["temperature"],
                               "parallel_tool_calls": False, "strict": False}})
            self.agents[aid] = transport
            return {"id": aid}
        aid = path.split("/")[3].split("?")[0]
        return self.agents[aid].request(method, path, body)


CREATE_ARGS = {"user_id": "U000828", "store_id": "S1", "product_ids": [TARGET_PRODUCT_ID],
               "product_cnts": [1], "address": WORK_ADDRESS,
               "dispatch_time": "2024-06-23 15:00:00", "attributes": ["规格: 7分糖"]}


class DriverTests(unittest.TestCase):
    def run_driver(self, responses, *, judge_error=False):
        cfg = config()
        services = CapServices(cfg, responses)
        runtimes = {}
        result = execute_capability(
            cfg, sample(), model_transport=services, letta_transport=services,
            runtime_factory=lambda: runtimes.setdefault("native", CapRuntime(CapEnv(), judge_error)),
            emit=lambda _: None)
        return result, services, runtimes["native"]

    def test_task_completion_is_not_oracle_success(self):
        result, services, native = self.run_driver([done("我已完成 ###STOP###")])
        self.assertEqual(result["invalid_reasons"], [])
        self.assertTrue(result["execution_complete"])
        self.assertEqual(result["status"], "CAPABILITY_COMPLETED_AUDIT_PENDING")
        self.assertFalse(result["task_oracle"]["oracle_passed"])
        self.assertIsNone(result["capability_passed"])
        self.assertIsNone(result["input_audit_passed"])
        self.assertEqual(result["input_audit"]["status"], "PENDING")
        self.assertIsNone(result["scientific_result"])
        self.assertFalse(result["validity_passed"])
        self.assertEqual(result["confirmation_review"]["payment_confirmation_by_user"], None)
        transport = services.agents[result["agent_id"]]
        posts = [r for r in transport.requests if r[0] == "POST"]
        self.assertTrue(posts)
        self.assertFalse(any(t["name"] == "memory_update" for t in posts[0][2]["client_tools"]))
        self.assertEqual(native.started, [4])

    def test_oracle_uses_final_paid_order_and_judge_failure_retains_it(self):
        responses = [approval(call("c1", "create_delivery_order", CREATE_ARGS)),
                     approval(call("c2", "pay_delivery_order", {"order_id": "#DELIVERY#fixture"})),
                     done("完成 ###STOP###")]
        result, _, native = self.run_driver(responses, judge_error=True)
        self.assertTrue(result["execution_complete"])
        self.assertEqual(result["judge"]["status"], "JUDGE_FAILED_ORDERS_RETAINED")
        self.assertIsNone(result["judge"]["reward_info"])
        self.assertTrue(result["task_oracle"]["oracle_passed"])
        self.assertTrue(result["native_aborted_after_judge_failure"])
        self.assertTrue(native.aborted)
        self.assertIsNotNone(result["native_snapshot"])
        self.assertEqual(result["task_oracle"]["passing_order_ids"], ["#DELIVERY#fixture"])
        transcript = result["execution"]["transcript"]
        self.assertTrue(any(row["role"] == "tool" for row in transcript))
        self.assertTrue(any(row.get("tool_calls") for row in transcript))
        sequence = result["confirmation_review"]["tool_sequence"]
        self.assertEqual([row["name"] for row in sequence],
                         ["create_delivery_order", "pay_delivery_order"])
        self.assertEqual([row["status"] for row in sequence], ["success", "success"])

    def test_wrong_order_completes_without_oracle_success(self):
        wrong = dict(CREATE_ARGS, product_ids=["S17791041622763865_P00012"],
                     attributes=["规格: 5分糖"])
        responses = [approval(call("c1", "create_delivery_order", wrong)),
                     approval(call("c2", "pay_delivery_order", {"order_id": "#DELIVERY#fixture"})),
                     done("完成 ###STOP###")]
        result, _, _ = self.run_driver(responses)
        self.assertTrue(result["execution_complete"])
        self.assertFalse(result["task_oracle"]["oracle_passed"])
        self.assertIsNone(result["capability_passed"])


if __name__ == "__main__":
    unittest.main()
