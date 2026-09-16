"""Local t4 execution probe: the offline acceptance.

The provider is a scripted loopback opener, so no network and no model call happens;
everything else is the production path - the fixed loopback-only endpoint, the REAL native
t4 environment from the private dataset task, the REAL delivery tool execution, the
capacity gate, the unified post budget, and the deterministic private order diagnosis.
A pass here is a statement about this PROBE, never a claim about model capability.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

MODEL = "Qwen/Qwen3.5-9B"
ENDPOINT = "http://127.0.0.1:8001"
TARGET_PRODUCT_ID = "S17791041622763865_P00011"
WRONG_STORE_ID = "S17791041622763865_S00006"
WORK_ADDRESS = "甘肃省兰州市安宁区安宁西路地五大道88号"
HOME_ADDRESS = "甘肃省兰州市安宁区安宁西路381号飞天世纪新城3幢2单元602"


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "ae_01_local_t4_probe_under_test", ROOT / "scripts/ae_01_local_t4_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vita_source():
    root = Path(os.environ.get("AE_VERIFY_ROOT", ROOT / ".ae-verify-src"))
    return Path(os.environ.get("AE_VITA_SOURCE", root / "source"))


def _reply(payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    class _Response(io.BytesIO):
        def __init__(self):
            super().__init__(body)
            self.code, self.headers = status, {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False
    return _Response()


def _chat_reply(tool_calls=None, content=None, finish_reason="tool_calls"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
            for name, arguments, call_id in tool_calls]
    return {"id": "gen", "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


class _ScriptedProvider:
    """A model stand-in that QUERIES first and READS the real returns it gets.

    `mode` selects the behaviour. The order id always comes from the real create return and
    the product id always from the real search return: the script never invents either.
    """

    SEARCH_ARGS = {"keywords": ["恋与深空", "奶茶"]}

    def __init__(self, *, mode="query_create_pay", order_id=None, address=WORK_ADDRESS,
                 sugar="7分糖", chat_status=None, length_on=None, scripts=None,
                 create_args=None):
        self.mode = mode
        self.order_id = order_id
        self.address = address
        self.sugar = sugar
        self.chat_status = chat_status or {}
        self.length_on = length_on
        self.scripts = scripts
        self.create_args = create_args or {}
        self.tokenize_calls = []
        self.chat_calls = []
        self.get_calls = []
        self.search_return = None
        #: Optional overrides for the service-side answers the probe must read.
        self.tokenize_max_model_len = None
        self.tokenize_model = None
        self.tokenize_status = 200

    # ---------------------------------------------------------------- opener contract

    #: What the fake service says its window is. The window is READ from the service.
    max_model_len = 128000
    reported_model = MODEL

    def open(self, request, timeout):
        path = request.full_url[len(ENDPOINT):]
        if request.data is None:  # GET
            self.get_calls.append(path)
            if path == "/v1/models":
                return _reply({"object": "list", "data": [
                    {"id": self.reported_model, "max_model_len": self.max_model_len}]})
            return _reply({"error": {"message": "no such route"}}, 404)
        body = json.loads(request.data.decode("utf-8"))
        if path == "/tokenize":
            if self.tokenize_status != 200:
                return _reply({"error": {"message": "scripted tokenize failure",
                                         "code": self.tokenize_status}},
                              self.tokenize_status)
            return self._tokenize(body)
        return self._chat(body)

    #: Deterministic service counts: the first request is CHEAP, any request that already
    #: carries a tool return is EXPENSIVE. That is the real growth shape and it makes the
    #: gate arithmetic checkable without pretending to be a tokenizer.
    CHEAP_COUNT = 100
    GROWTH_PER_PAIR = 30
    TOOL_RETURN_GROWTH = 5000

    def _tokenize(self, body):
        self.tokenize_calls.append(body)
        messages = body.get("messages") or []
        # A deterministic, monotone stand-in for a real tokenizer: the first request is
        # cheap, and every further message pair grows the count (a tool return grows it a
        # lot). This is what makes the gate arithmetic checkable in both directions.
        pairs = max(0, len(messages) - 2)
        tool_returns = sum(1 for m in messages if m.get("role") == "tool")
        answer = {"count": (self.CHEAP_COUNT + pairs * self.GROWTH_PER_PAIR
                            + tool_returns * self.TOOL_RETURN_GROWTH)}
        if self.tokenize_max_model_len is not None:
            answer["max_model_len"] = self.tokenize_max_model_len
        if self.tokenize_model is not None:
            answer["model"] = self.tokenize_model
        return _reply(answer)

    def _chat(self, body):
        self.chat_calls.append(body)
        index = len(self.chat_calls)
        status = self.chat_status.get(index, 200)
        if status != 200:
            return _reply({"error": {"message": "scripted failure", "code": status}}, status)
        if self.length_on == index:
            return _reply(_chat_reply(content="...", finish_reason="length"))
        if "tools" not in body:  # the user simulator has no tools
            return _reply(_chat_reply(content="没有了，谢谢", finish_reason="stop"))
        if self.scripts is not None:
            return _reply(self.scripts(index, body, self))
        return _reply(self._agent_reply(body))

    # ------------------------------------------------------------------ agent script

    def _order_id_from(self, body):
        import re
        pattern = re.compile(r"order_id[:=]['\"]?([^,'\")\s]+)")
        for message in reversed(body.get("messages") or []):
            if message.get("role") != "tool":
                continue
            match = pattern.search(message.get("content") or "")
            if match:
                return match.group(1)
        return None

    def _product_id_from(self, body):
        for message in reversed(body.get("messages") or []):
            if message.get("role") != "tool":
                continue
            match = re.search(r"product_id=(S\d+_P\d+)", message.get("content") or "")
            if match:
                return match.group(1)
        return None

    def _store_id_from(self, body):
        for message in reversed(body.get("messages") or []):
            if message.get("role") != "tool":
                continue
            match = re.search(r"store_id=(S\d+_S\d+)", message.get("content") or "")
            if match:
                return match.group(1)
        return None

    def _tool_turns(self, body):
        return [m for m in body.get("messages") or [] if m.get("role") == "tool"]

    def _has_searched(self, body):
        return any("product_id=" in (m.get("content") or "") for m in self._tool_turns(body))

    def _has_created(self, body):
        return any("Order(order_id:" in (m.get("content") or "")
                   for m in self._tool_turns(body))

    def _agent_reply(self, body):
        """Turn 1: search. Turn 2: create from the real search return. Turn 3: pay."""
        if not self._has_searched(body):
            return _chat_reply([("delivery_product_search_recommand", self.SEARCH_ARGS, "c1")])
        product_id = self._product_id_from(body) or TARGET_PRODUCT_ID
        store_id = self._store_id_from(body) or WRONG_STORE_ID
        if self._has_created(body):
            order_id = self._order_id_from(body)
            if order_id is None:  # pragma: no cover - defensive
                return _chat_reply(content="下单失败，抱歉。", finish_reason="stop")
            return _chat_reply([("pay_delivery_order", {"order_id": order_id}, "c3")])
        if self.mode == "wrong_spec":
            sugar = "5分糖"
        else:
            sugar = self.sugar
        address = HOME_ADDRESS if self.mode == "wrong_address" else self.address
        if self.order_id == "guessed":
            return _chat_reply([("pay_delivery_order", {"order_id": "OTguessed0001"}, "c3")])
        create = {"user_id": "U000828", "store_id": store_id, "product_ids": [product_id],
                  "product_cnts": [1], "address": address,
                  "dispatch_time": "2024-06-23 18:00:00", "attributes": [sugar]}
        create.update(self.create_args)
        return _chat_reply([("create_delivery_order", create, "c2")])


class _CreateThenGuessedPay(_ScriptedProvider):
    """Creates for real, then pays an id that no create ever returned."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.paid_attempted = False

    def _agent_reply(self, body):
        if not self._has_searched(body):
            return _chat_reply([("delivery_product_search_recommand", self.SEARCH_ARGS, "c1")])
        if self.paid_attempted:
            return _chat_reply(content="完成。", finish_reason="stop")
        if self._has_created(body):
            self.paid_attempted = True
            return _chat_reply([("pay_delivery_order", {"order_id": "OTguessed0001"}, "c4")])
        product_id = self._product_id_from(body) or TARGET_PRODUCT_ID
        store_id = self._store_id_from(body) or WRONG_STORE_ID
        return _chat_reply([("create_delivery_order", {
            "user_id": "U000828", "store_id": store_id, "product_ids": [product_id],
            "product_cnts": [1], "address": self.address,
            "dispatch_time": "2024-06-23 18:00:00", "attributes": [self.sugar]}, "c2")])


class _FailingConnect:
    def open(self, request, timeout):
        raise TimeoutError("scripted connect timeout")


class LocalT4ProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = _load_probe()
        self.vita = _vita_source()
        if not (self.vita / "src/vita").is_dir():
            self.skipTest("the fixed Vita source is absent")
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            self.skipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _out(self, name):
        return Path(self.tmp.name) / name

    def _run(self, provider, name="run"):
        out = self._out(name)
        result = self.probe.execute_probe(vita_source=self.vita, output_dir=out,
                                          opener=provider)
        return result, out

    @staticmethod
    def _summary(result):
        """A compact view for assertion messages: the raw result holds the whole database."""
        return {
            "status": result.get("status"), "stop_reason": result.get("stop_reason"),
            "task_success": result.get("task_success"), "posts": result.get("total_model_posts"),
            "capacity": [(c["step"], c["total"], c["fits"]) for c in result.get("capacity", [])],
            "tool_calls": [(r["name"], r["status"]) for r in result.get("tool_returns", [])],
            "order_state_check": (result.get("order_diagnosis") or {}).get("orders", [{}])[0]
            .get("criteria") if result.get("order_diagnosis") else None,
            "final_orders": sorted((result.get("final_native_snapshot") or {})
                                   .get("orders", {})),
        }

    # -------------------------------------------------------------- plan / preflight

    def test_plan_and_preflight_never_call_the_model(self):
        plan_out = self._out("plan")
        code = self.probe.main(["--stage", "plan", "--vita-source", str(self.vita),
                                "--output-dir", str(plan_out)])
        self.assertEqual(code, 0)
        plan = json.loads((plan_out / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], self.probe.STATUS_PLAN)
        self.assertFalse(plan["network_called"])
        self.assertFalse(plan["model_called"])
        self.assertEqual(plan["endpoint"]["origin"], ENDPOINT)
        self.assertEqual(plan["endpoint"]["model"], MODEL)
        self.assertEqual(plan["budget"]["agent_max_requests"], 12)
        self.assertEqual(plan["budget"]["user_simulator_max_exchanges"], 4)
        self.assertEqual(plan["budget"]["total_model_posts_max"], 16)
        self.assertEqual(plan["budget"]["agent_output_tokens"], 2048)
        self.assertEqual(plan["budget"]["auxiliary_output_tokens"], 4096)
        self.assertEqual(plan["budget"]["max_tool_return_chars"], 26214)
        self.assertIs(plan["budget"]["thinking"]["enable_thinking"], False)
        self.assertFalse(plan["private_oracle"]["injected_into_agent"])
        self.assertFalse(plan["case"]["dataset_history_sent"])
        self.assertEqual(plan["case"]["history_records_sent"], 0)
        self.assertEqual(sorted(p.name for p in plan_out.iterdir()), ["plan.json"])

        # preflight contacts ONLY the tokenizer: no chat completion, no inference
        provider = _ScriptedProvider()
        pre_out = self._out("pre")
        readiness = self.probe.preflight(self.vita, opener=provider)
        self.assertEqual(readiness["status"], self.probe.STATUS_PREFLIGHT_OK,
                         {k: v for k, v in readiness.items() if k != "transport_journal"})
        self.assertEqual(provider.chat_calls, [], "preflight must not call the model")
        self.assertTrue(provider.tokenize_calls)
        self.assertFalse(readiness["network_called"],
                         "no network at all happened in this fixture")

    def test_preflight_blocks_when_the_service_cannot_be_read(self):
        """No service at all: the window and the count are both unreadable -> zero chat."""
        out = self._out("blocked")
        result = self.probe.execute_probe(vita_source=self.vita, output_dir=out,
                                          opener=_FailingConnect())
        self.assertEqual(result["status"], self.probe.STATUS_PREFLIGHT_BLOCKED)
        self.assertIsNone(result["task_success"])
        self.assertEqual(result["model_called"], False)
        self.assertEqual(result["total_model_posts"], 0)

    def test_preflight_blocks_when_tokenize_is_unavailable_but_models_is_not(self):
        """The window is readable but the counter is not: still no generation."""
        provider = _ScriptedProvider()
        provider.tokenize_status = 500
        readiness = self.probe.preflight(self.vita, opener=provider)
        self.assertEqual(readiness["status"], self.probe.STATUS_PREFLIGHT_BLOCKED)
        self.assertTrue(readiness["capacity_blocked"])
        self.assertIn("service_tokenize", readiness["missing"])
        self.assertFalse(readiness["model_inference_called"])
        self.assertEqual(provider.chat_calls, [])

    def test_a_service_without_a_readable_window_refuses_to_run(self):
        provider = _ScriptedProvider()
        provider.max_model_len = None
        readiness = self.probe.preflight(self.vita, opener=provider)
        self.assertEqual(readiness["status"], self.probe.STATUS_PREFLIGHT_BLOCKED)
        self.assertIn("service_window", readiness["missing"])
        self.assertIn("service_window_unreadable", readiness["block_reason"])
        self.assertEqual(provider.chat_calls, [])

    def test_a_service_that_reports_another_model_refuses_to_run(self):
        provider = _ScriptedProvider()
        provider.reported_model = "Qwen/Qwen3-30B-A3B-Instruct-2507"
        readiness = self.probe.preflight(self.vita, opener=provider)
        self.assertEqual(readiness["status"], self.probe.STATUS_PREFLIGHT_BLOCKED)
        self.assertIn("service_model_identity_mismatch", readiness["block_reason"])
        self.assertEqual(provider.chat_calls, [])

    # ------------------------------------------------------------- input discipline

    def test_the_agent_input_carries_the_current_state_and_nothing_private(self):
        public = self.probe.prepare_public(self.vita)
        native = self.probe.build_native(public, self.vita)
        system = self.probe.agent_system_message(native, public)
        user = self.probe.task_user_message(public)
        # the current preference is present ...
        self.assertIn("奶茶偏好7分糖", system)
        self.assertNotIn("奶茶偏好5分糖", system)
        self.assertIn("高德地图" if False else "工作地址", system)
        # ... and the private answers are NOT
        self.assertNotIn(TARGET_PRODUCT_ID, system + user)
        self.assertNotIn("恋与深空联名·深空告白奶茶", system + user)
        self.assertNotIn("evaluation_criteria", system + user)
        self.assertNotIn("overall_rubrics", system + user)
        self.assertNotIn("7分糖", user, "the oracle value must not be in the task message")
        # the simulator is the NATIVE one and is given only the public material
        user_module = self.probe.load_user_simulator(self.vita)
        simulator = user_module["UserSimulator"](
            tools=None, instructions=user, persona=json.dumps(public["inputs"]["profile"],
                                                              ensure_ascii=False),
            llm=MODEL)
        prompt = simulator.system_prompt
        self.assertNotIn(TARGET_PRODUCT_ID, prompt)
        self.assertNotIn("7分糖", prompt)
        self.assertIn(user, prompt)
        # ALL native tools are exposed, unfiltered, and include the search tools
        names = sorted(binding.schema["name"] for binding in native["bindings"].values())
        self.assertIn("delivery_product_search_recommand", names)
        self.assertIn("delivery_store_search_recommand", names)
        self.assertIn("create_delivery_order", names)
        self.assertIn("pay_delivery_order", names)
        self.assertEqual(len(names), len(native["bindings"]))

    # ------------------------------------------------------------------- execution

    def test_agent_queries_then_creates_and_pays_the_real_order(self):
        provider = _ScriptedProvider()
        result, out = self._run(provider)
        self.assertEqual(result["status"], self.probe.STATUS_PASSED, self._summary(result))
        self.assertTrue(result["task_success"])
        self.assertTrue(result["oracle_passed"])
        # the query really went to the real tool and really found the target product
        searched = [r for r in result["tool_returns"]
                    if r["name"] == "delivery_product_search_recommand"]
        self.assertTrue(searched)
        self.assertIn(TARGET_PRODUCT_ID, searched[0]["raw_text"])
        self.assertIn("益禾堂×恋与深空联名·深空告白奶茶", searched[0]["raw_text"])
        # every request carried the fixed local shape
        for body in provider.chat_calls:
            self.assertEqual(body["model"], MODEL)
            self.assertEqual(body["temperature"], 0)
            self.assertIs(body["chat_template_kwargs"]["enable_thinking"], False)
        agent_bodies = [b for b in provider.chat_calls if "tools" in b]
        for body in agent_bodies:
            self.assertIn("tools", body)
            self.assertIs(body["parallel_tool_calls"], False)
            self.assertEqual(sorted(t["function"]["name"] for t in body["tools"]),
                             sorted(binding.schema["name"]
                                    for binding in
                                    self.probe.build_native(
                                        self.probe.prepare_public(self.vita),
                                        self.vita)["bindings"].values()))
        # the private answer only ever arrived through the REAL search return
        first_agent = agent_bodies[0]
        self.assertNotIn(TARGET_PRODUCT_ID, json.dumps(first_agent, ensure_ascii=False))
        diagnosis = result["order_diagnosis"]
        self.assertTrue(diagnosis["oracle_passed"])
        self.assertTrue(diagnosis["orders"][0]["criteria"]["sugar_7"])
        self.assertTrue(diagnosis["orders"][0]["criteria"]["work_address"])
        self.assertEqual(diagnosis["orders"][0]["observed"]["line_sugar_tokens"], ["7分糖"])
        self.assertEqual(diagnosis["orders"][0]["observed"]["address"], WORK_ADDRESS)
        self.assertEqual(len(result["paid_order_ids"]), 1)
        self.assertIn(result["paid_order_ids"][0], result["created_order_ids"])
        self.assertTrue(result["total_model_posts_within_budget"])
        self.assertLessEqual(result["total_model_posts"], 16)
        # artifacts
        for name in ("plan.json", "preflight.json", "case.json", "initial-database.json",
                     "final-database.json", "result.json", "tool-returns.json"):
            self.assertTrue((out / name).is_file(), name)

    def test_a_wrong_spec_or_address_does_not_pass(self):
        for mode, criterion in (("wrong_spec", "sugar_7"), ("wrong_address", "work_address")):
            with self.subTest(mode=mode):
                provider = _ScriptedProvider(mode=mode)
                result, _out = self._run(provider, name=f"bad-{mode}")
                self.assertNotEqual(result["status"], self.probe.STATUS_PASSED)
                self.assertFalse(result["task_success"])
                self.assertFalse(result["oracle_passed"])
                record = result["order_diagnosis"]["orders"][0]
                self.assertFalse(record["criteria"][criterion])
                self.assertFalse(record["all_passed"])
                if mode == "wrong_spec":
                    self.assertEqual(record["observed"]["line_sugar_tokens"], ["5分糖"])
                else:
                    self.assertEqual(record["observed"]["address"], HOME_ADDRESS)

    def test_the_counted_input_is_the_sent_input_for_both_roles(self):
        """The /tokenize payload must be the SAME messages/tools as the chat payload."""
        provider = _ScriptedProvider()
        provider.max_model_len = 128000
        result, _out = self._run(provider, name="identity")
        self.assertEqual(result["status"], self.probe.STATUS_PASSED,
                         self._summary(result))
        self.assertTrue(provider.chat_calls, "the run really sent chat requests")
        self.assertTrue(provider.tokenize_calls, "every chat request was counted first")
        # every CHAT body was counted first, on the same messages/tools/kwargs
        self.assertGreaterEqual(len(provider.tokenize_calls), len(provider.chat_calls))
        for sent in provider.chat_calls:
            counted = [c for c in provider.tokenize_calls
                       if c["messages"] == sent["messages"]
                       and c.get("tools") == sent.get("tools")]
            self.assertTrue(counted, "no /tokenize carried the body that was sent")
            for c in counted:
                self.assertEqual(c["model"], sent["model"])
                self.assertEqual(c["chat_template_kwargs"], sent["chat_template_kwargs"])
                self.assertIs(c["add_generation_prompt"], True)
        # the agent role's counted/sent shape carries the native tool schemas
        self.assertTrue(any("tools" in c for c in provider.tokenize_calls))
        self.assertTrue(all("prompt" not in c for c in provider.tokenize_calls),
                        "the probe must send chat input, never a pre-rendered prompt string")

    def test_the_user_role_is_counted_on_the_same_contract(self):
        """The user simulator's request is counted as chat input too, and carries no tools."""
        def asking(index, body, provider):
            return _chat_reply(content="请问送到哪里？", finish_reason="stop")
        provider = _ScriptedProvider(scripts=asking)
        provider.max_model_len = 128000
        result, _out = self._run(provider, name="identity-user")
        self.assertGreaterEqual(result["user_exchanges"], 1)
        user_chats = [b for b in provider.chat_calls if "tools" not in b]
        self.assertEqual(len(user_chats), result["requests_by_role"]["user"])
        for sent in user_chats:
            counted = [c for c in provider.tokenize_calls if c["messages"] == sent["messages"]]
            self.assertTrue(counted, "the user request was not counted before sending")
            self.assertNotIn("tools", counted[0])
            self.assertIs(counted[0]["add_generation_prompt"], True)
            self.assertEqual(counted[0]["chat_template_kwargs"],
                             {"enable_thinking": False})

    def test_two_consecutive_agent_questions_reach_the_native_user_simulator(self):
        """The counterexample Codex found: BOTH questions must be in the wire, in order.

        Question 1 must appear in the first user request (flipped to the user role), and the
        second request must carry the task, question 1, answer 1 and question 2 - exactly
        once each, in the native `state.flip_roles()` order. Nothing may be dropped and
        nothing may be duplicated.
        """
        questions = ["请问送到哪里？", "上一条地址需要修改吗？"]

        asked = []

        def scripted(index, body, provider):
            # index counts ALL chat calls (agent + user), so count agent turns here instead
            if "tools" in body:
                asked.append(index)
                if len(asked) <= len(questions):
                    return _chat_reply(content=questions[len(asked) - 1],
                                       finish_reason="stop")
            return _chat_reply(content="没有了，谢谢###STOP###", finish_reason="stop")

        provider = _ScriptedProvider(scripts=scripted)
        provider.max_model_len = 128000
        # the native user simulator must be the one driving the exchange
        calls = []
        probe = self.probe
        original = probe.NativeUser.next_message

        def traced(self, wired):
            calls.append(list(wired))
            self.simulator.native_generate_called = True
            return original(self, wired)

        probe.NativeUser.next_message = traced
        self.addCleanup(setattr, probe.NativeUser, "next_message", original)
        result, out = self._run(provider, name="two-questions")
        self.assertEqual(result["user_exchanges"], 2, self._summary(result))
        self.assertEqual(len(calls), 2, "the native simulator ran exactly twice")

        user_chats = [b for b in provider.chat_calls if "tools" not in b]
        self.assertEqual(len(user_chats), 2, "one user request per question")
        # every user request was counted on the same contract before being sent
        for sent in user_chats:
            counted = [c for c in provider.tokenize_calls
                       if c["messages"] == sent["messages"]]
            self.assertTrue(counted, "the user request was not counted before sending")
            self.assertIs(counted[0]["add_generation_prompt"], True)
            self.assertNotIn("tools", counted[0])

        def contents(body):
            return [(m["role"], m.get("content")) for m in body["messages"]
                    if m.get("content")]

        first = contents(user_chats[0])
        self.assertIn(("user", questions[0]), first,
                      "question 1 must reach the model as a user turn")
        self.assertNotIn(("user", questions[1]), first,
                         "question 2 must NOT be in the first request")
        self.assertEqual(sum(1 for role, text in first if text == questions[0]), 1)

        second = contents(user_chats[1])
        answer_one = result["native_user_simulator"]["exchanges"][0]
        self.assertEqual(answer_one["question"], questions[0])
        expected_order = [("assistant", self.probe.task_user_message(
            self.probe.prepare_public(self.vita))),
            ("user", questions[0]),
            ("assistant", result["steps"][0]["user_reply"]),
            ("user", questions[1])]
        observed = [item for item in second if item in expected_order]
        self.assertEqual(observed, expected_order,
                         "the second request must carry task, Q1, A1, Q2 in native order")
        for role, text in expected_order:
            self.assertEqual(sum(1 for r, t in second if t == text), 1,
                             f"{text!r} must appear exactly once")
        # the native state really held the conversation
        self.assertTrue(result["native_user_simulator"]["native_state_held_across_turns"])
        self.assertTrue(result["native_user_simulator"]["drives_native_generate_next_message"])
        self.assertEqual([e.get("state_messages")
                          for e in result["native_user_simulator"]["exchanges"]], [3, 5])
        self.assertTrue((out / "user-simulator.json").is_file())

    def test_the_user_role_has_its_own_capacity_gate(self):
        """The user simulator's 4096 reserve is gated too, on the same window.

        The agent's first request fits (100 + 2048 < 4000) but the user simulator's answer
        needs 100 + 4096, so the USER role is the one refused and no chat is sent for it.
        """
        def asking(index, body, provider):
            return _chat_reply(content="请问送到哪里？", finish_reason="stop")
        provider = _ScriptedProvider(scripts=asking)
        provider.max_model_len = 4000
        result, _out = self._run(provider, name="user-gate")
        self.assertEqual(result["stop_reason"], "capacity_blocked_before_send")
        self.assertEqual(len(provider.chat_calls), 1,
                         "only the agent's own request went out")
        refused = result["gate_refusals"][-1]
        self.assertEqual(refused["role"], "user")
        self.assertEqual(refused["reserve"], self.probe.AUX_OUTPUT_TOKENS)
        self.assertEqual(refused["window"], 4000)
        self.assertGreater(refused["total"], 4000)
        self.assertFalse(refused["fits"])
        self.assertEqual(result["requests_by_role"]["user"], 0,
                         "no user request may be sent once the gate refuses")

    def test_a_fabricated_order_id_never_passes(self):
        provider = _CreateThenGuessedPay()
        result, _out = self._run(provider, name="guessed")
        self.assertNotEqual(result["status"], self.probe.STATUS_PASSED)
        self.assertFalse(result["task_success"])
        payments = [r for r in result["tool_returns"] if r["name"] == "pay_delivery_order"]
        self.assertTrue(payments, ("the payment really was attempted", self._summary(result)))
        self.assertEqual(payments[0]["status"], "error")
        self.assertIn("not found", (payments[0]["raw_text"] or "").lower())
        self.assertEqual(result["paid_order_ids"], [],
                         "a refused payment must leave no paid order behind")

    def test_length_and_non_200_each_stop_the_run(self):
        length = _ScriptedProvider(length_on=1)
        result, _out = self._run(length, name="length")
        self.assertEqual(result["status"], self.probe.STATUS_LENGTH)
        self.assertEqual(result["stop_reason"], "finish_reason_length")
        self.assertEqual(result["total_model_posts"], 1, "a truncation is never retried")

        http = _ScriptedProvider(chat_status={1: 400})
        result, _out = self._run(http, name="http400")
        self.assertEqual(result["status"], self.probe.STATUS_INVALID)
        self.assertEqual(result["stop_reason"], "provider_http_400")
        self.assertEqual(result["total_model_posts"], 1, "an error status is never retried")

    def test_an_unanswered_send_is_not_counted_as_executed(self):
        result, _out = self._run(_FailingConnect(), name="timeout")
        self.assertEqual(result["status"], self.probe.STATUS_PREFLIGHT_BLOCKED)

    def test_the_capacity_gate_blocks_the_initial_request_with_zero_chat(self):
        """A window too small for the initial shape blocks the run with no chat at all."""
        provider = _ScriptedProvider()
        provider.max_model_len = 200
        out = self._out("capacity")
        result = self.probe.execute_probe(vita_source=self.vita, output_dir=out,
                                          opener=provider)
        self.assertEqual(result["status"], self.probe.STATUS_PREFLIGHT_BLOCKED)
        self.assertEqual(provider.chat_calls, [],
                         "an over-capacity shape must never reach the model")
        self.assertTrue(result["preflight"]["capacity_blocked"])
        self.assertEqual(result["preflight"]["service_window_tokens"], 200)
        self.assertGreater(result["preflight"]["initial_total_tokens"], 200)

    def test_a_later_over_capacity_request_stops_before_being_sent(self):
        """The WINDOW IS READ FROM THE SERVICE: shrink it and the second request is gated.

        The fixture counts 100 for the initial request and 4000 once a tool return is in
        the conversation (the real growth shape). With a service window of 5000 the first
        request fits and the second is refused BEFORE generation.
        """
        provider = _ScriptedProvider()
        provider.max_model_len = 5000
        out = self._out("later")
        result = self.probe.execute_probe(vita_source=self.vita, output_dir=out,
                                          opener=provider)
        self.assertEqual(result["status"], self.probe.STATUS_CAPACITY_BLOCKED)
        self.assertEqual(result["stop_reason"], "capacity_blocked_before_send")
        self.assertEqual(result["capacity_blocked_at_step"], 2)
        self.assertEqual(result["service_window_tokens"], 5000)
        self.assertEqual(result["service_window_source"], "service_models_metadata")
        first = result["capacity"][0]
        self.assertTrue(first["fits"])
        self.assertEqual(first["total"], provider.CHEAP_COUNT + self.probe.AGENT_OUTPUT_TOKENS)
        self.assertEqual(first["window"], 5000)
        # the refused request is recorded in the gate record, never sent
        refused = result["gate_refusals"][-1]
        self.assertGreaterEqual(refused["count"],
                                provider.CHEAP_COUNT + provider.TOOL_RETURN_GROWTH)
        self.assertFalse(refused["fits"])
        self.assertGreater(refused["total"], 5000)
        self.assertTrue(refused["counted"]["sends_the_same_messages_and_tools_as_the_chat"])
        self.assertEqual(len(provider.chat_calls), 1,
                         "the second request was refused BEFORE being sent")
        self.assertEqual(result["total_model_posts"], 1)
        # the tool really ran and really returned the target product
        self.assertIn(TARGET_PRODUCT_ID, result["tool_returns"][0]["raw_text"])
        self.assertTrue((out / "result.json").is_file())

    def test_a_changed_window_mid_run_is_refused_not_ignored(self):
        """A service that answers a different max_model_len mid-run stops the request."""
        provider = _ScriptedProvider()
        provider.max_model_len = 5000
        provider.tokenize_max_model_len = 8192
        result, _out = self._run(provider, name="changed-window")
        self.assertEqual(result["stop_reason"], "service_window_changed_between_reads")
        self.assertEqual(provider.chat_calls, [],
                         "a window that changed between reads must not be sent through")

    def test_the_user_simulator_is_bounded_and_counts_against_the_budget(self):
        # An agent that only ever asks questions: the simulator must run, be capped, and
        # the whole run must stay inside the shared post budget.
        def asking(index, body, provider):
            return _chat_reply(content=f"请问送到哪里？(第{index}次)", finish_reason="stop")
        provider = _ScriptedProvider(scripts=asking)
        result, _out = self._run(provider, name="asking")  # roomy fixture window
        self.assertEqual(result["status"], self.probe.STATUS_INCOMPLETE)
        self.assertEqual(result["stop_reason"], "user_exchange_budget_exhausted")
        self.assertEqual(result["user_exchanges"], self.probe.AUX_MAX_REQUESTS)
        self.assertLessEqual(result["total_model_posts"], 16)
        self.assertTrue(result["total_model_posts_within_budget"])
        agent_posts = result["requests_by_role"]["agent"]
        user_posts = result["requests_by_role"]["user"]
        # each asking turn costs one agent call and one native user-simulator call
        self.assertEqual(user_posts, self.probe.AUX_MAX_REQUESTS)
        self.assertEqual(agent_posts, self.probe.AUX_MAX_REQUESTS + 1)
        self.assertLessEqual(agent_posts, self.probe.AGENT_MAX_REQUESTS)
        # the user simulator is the NATIVE class, recorded as such
        self.assertTrue(result["native_user_simulator"]["uses_native_user_simulator"])
        self.assertEqual(result["native_user_simulator"]["native_class"],
                         "vita.user.user_simulator.UserSimulator")
        self.assertEqual(len(result["native_user_simulator"]["exchanges"]),
                         self.probe.AUX_MAX_REQUESTS)
        # the user role never carries the agent's tools
        user_bodies = [b for b in provider.chat_calls if "tools" not in b]
        agent_bodies = [b for b in provider.chat_calls if "tools" in b]
        self.assertEqual(len(user_bodies), self.probe.AUX_MAX_REQUESTS)
        self.assertEqual(len(agent_bodies), agent_posts)
        self.assertEqual(len(provider.chat_calls), len(user_bodies) + len(agent_bodies))

    def test_an_agent_stop_marker_without_a_payment_does_not_pass(self):
        def stopping(index, body, provider):
            return _chat_reply(content="已完成，###STOP###", finish_reason="stop")
        provider = _ScriptedProvider(scripts=stopping)
        result, _out = self._run(provider, name="stopped")
        self.assertEqual(result["status"], self.probe.STATUS_INCOMPLETE)
        self.assertEqual(result["stop_reason"], "agent_declared_stop")
        self.assertFalse(result["task_success"])
        self.assertEqual(result["user_exchanges"], 0)


class LocalT4BoundaryTests(unittest.TestCase):
    """The frozen inputs this probe reuses must not have been edited by it."""

    def setUp(self):
        self.probe = _load_probe()

    def test_the_endpoint_can_never_leave_loopback(self):
        self.assertEqual(self.probe.validate_local_endpoint(ENDPOINT), ENDPOINT)
        for bad in ("https://127.0.0.1:8001", "http://api.siliconflow.cn",
                    "http://10.0.0.5:8001", "http://127.0.0.1:8001/v1/chat/completions"):
            with self.assertRaises(ValueError, msg=bad):
                self.probe.validate_local_endpoint(bad)

    def test_the_run_carries_no_30B_tokenizer_or_template_dependency(self):
        """The retired local counting path must be gone, not merely unused."""
        for gone in ("load_local_tokenizer", "count_tokens_locally", "count_rendered_tokens",
                     "render_prompt_text", "self_simulated_user",
                     "user_simulator_system_message"):
            self.assertFalse(hasattr(self.probe, gone),
                             f"{gone} must not exist in the probe anymore")
        source = (ROOT / "scripts/ae_01_local_t4_probe.py").read_text(encoding="utf-8")
        # The filename may still appear in the provenance hash list; the point is that the
        # run never IMPORTS or USES the retired 30B counting path.
        for forbidden in ("resolve_tokenizer_assets", "QwenBpeTokenizer",
                          "render_prompt_bytes", "TARGET_TOKENIZER", "QwenBpe",
                          "import ae_multiturn_capacity", "count_request_prompt"):
            self.assertNotIn(forbidden, source,
                             f"the run must not reach for the 30B asset path: {forbidden}")
        self.assertIsNone(self.probe.SERVICE_WINDOW_FALLBACK,
                          "there is no built-in window fallback")

    def test_the_private_oracle_values_are_the_dataset_ones(self):
        dataset = ROOT / ".ae-verify-src/tasks-full-a4553e1.json"
        if not dataset.is_file():
            self.skipTest("the dataset is absent")
        import ae_inputs
        sample = ae_inputs.prepare_sample(dataset)
        private = sample["private_tasks"]["sub_U000828_4"]
        self.assertEqual(private["target_product_ids"], [TARGET_PRODUCT_ID])
        self.assertEqual(self.probe.TARGET_PRODUCT_ID, TARGET_PRODUCT_ID)
        self.assertEqual(self.probe.EXPECTED_SUGAR, "7分糖")

    def test_the_tool_return_guard_matches_the_driver(self):
        short = "x" * 10
        sent, guard = self.probe.guarded_tool_return(short)
        self.assertEqual(sent, short)
        self.assertFalse(guard["truncated"])
        long_text = "y" * (self.probe.MAX_TOOL_RETURN_CHARS + 50)
        sent, guard = self.probe.guarded_tool_return(long_text)
        self.assertEqual(len(sent), self.probe.MAX_TOOL_RETURN_CHARS)
        self.assertTrue(guard["truncated"])
        self.assertEqual(guard["raw_chars"], len(long_text))
        self.assertEqual(self.probe.MAX_TOOL_RETURN_CHARS, 26214)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
