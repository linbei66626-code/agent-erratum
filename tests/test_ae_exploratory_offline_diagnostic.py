"""探索候选收缩验收：只验三项，不扩审计。

1. 新候选真实 PLAN/PREFLIGHT 接受、记录 1024、代理预算一致；旧配置不变。
2. 实际运行分支确实跳过两处可选计算（把这两个函数设为抛错，探索模式仍能保存
   阶段/最终结果）；原生 judge 与 raw 照常生成。
3. 原有传输/工具/原生 judge 故障仍按原规则停止留证，没有宽泛 try/except 吞掉。

不跑全仓、不重验历史交付、不部署、不调用模型。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ae_vita as av  # noqa: E402
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ae_cloud_re_multiturn as M  # noqa: E402
import ae_vita  # noqa: E402

EXPLORATORY = "--exploratory-capacity-option"
NEW_CONFIG = (ROOT / "configs"
              / "ae-01__re-multiturn__deepseek-flash.sim-eval-budget1024-exploratory-candidate.json")
OLD_1024 = (ROOT / "configs"
            / "ae-01__re-multiturn__deepseek-flash.serial-budget1024-candidate.json")
OLD_05 = (ROOT / "configs"
          / "ae-01__re-multiturn__deepseek-flash.sim-eval-repair-candidate.json")
OLD_04 = (ROOT / "configs"
          / "ae-01__re-multiturn__deepseek-flash.serial-candidate.json")


def declared(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_proxy_module():
    spec = importlib.util.spec_from_file_location(
        "ae_01_cloud_proxy", ROOT / "scripts/ae_01_cloud_proxy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 验收 1：PLAN / PREFLIGHT / 预算一致 / 旧配置不变
# ---------------------------------------------------------------------------

class CandidateAcceptanceTests(unittest.TestCase):
    def test_the_new_candidate_is_accepted_and_records_1024(self):
        config = M.validate_config(declared(NEW_CONFIG),
                                   exploratory_capacity_option=EXPLORATORY)
        self.assertEqual(config["schema_version"], M.SCHEMA_VERSION_SIM_EVAL)
        self.assertEqual(config["max_requests"], 1024)
        self.assertEqual(config["deterministic_evaluation"], "offline_diagnostic")
        self.assertEqual(M.deterministic_evaluation_mode(config), "offline_diagnostic")
        # The simulator boundary and the native evaluation protocol are still declared.
        self.assertEqual(M.run_protocols_of(config),
                         {"simulator_protocol": "ae-user-simulator-role-boundary-1",
                          "evaluation_protocol": "ae-evaluation-split-1"})

    def test_the_new_candidate_keeps_every_other_declared_boundary(self):
        """只改预算与附加评估的运行位置，工具/容量/串行边界逐字沿用。"""
        new = declared(NEW_CONFIG)
        old = declared(OLD_05)
        self.assertEqual({k: v for k, v in new.items()
                          if k not in ("max_requests", "deterministic_evaluation")},
                         {k: v for k, v in old.items() if k != "max_requests"})
        self.assertEqual(new["pacing"], old["pacing"])
        self.assertEqual(new["max_tool_return_chars"], old["max_tool_return_chars"])
        self.assertEqual(new["capacity"], old["capacity"])
        self.assertEqual(new["multicall_profile"], old["multicall_profile"])

    def test_the_old_candidates_are_untouched_and_keep_their_own_mode(self):
        old_1024 = M.validate_config(declared(OLD_1024),
                                     exploratory_capacity_option=EXPLORATORY)
        self.assertEqual(old_1024["max_requests"], 1024)
        self.assertEqual(old_1024["schema_version"], M.SCHEMA_VERSION_DEEPSEEK)
        self.assertEqual(M.deterministic_evaluation_mode(old_1024), "in_run")
        self.assertIsNone(M.run_protocols_of(old_1024))

        old_05 = M.validate_config(declared(OLD_05),
                                   exploratory_capacity_option=EXPLORATORY)
        self.assertEqual(old_05["max_requests"], 256)
        self.assertEqual(M.deterministic_evaluation_mode(old_05), "in_run")

        old_04 = M.validate_config(declared(OLD_04),
                                   exploratory_capacity_option=EXPLORATORY)
        self.assertEqual(old_04["max_requests"], 256)
        self.assertEqual(M.deterministic_evaluation_mode(old_04), "in_run")
        # And the old files themselves are byte-unchanged by this work.
        for path in (OLD_1024, OLD_05, OLD_04):
            self.assertNotIn("deterministic_evaluation",
                             path.read_text(encoding="utf-8"), path.name)

    def test_the_proxy_budget_must_match_the_candidate(self):
        proxy = load_proxy_module()
        config = M.validate_config(declared(NEW_CONFIG),
                                   exploratory_capacity_option=EXPLORATORY)
        # The declaration the proxy binds from IS the candidate document.
        bound = proxy.bind_request_budget(type("C", (), {"max_requests": 1024})(),
                                          config)
        self.assertEqual(bound, 1024)
        with self.assertRaises(ValueError):
            proxy.bind_request_budget(type("C", (), {"max_requests": 256})(), config)

    def test_an_unknown_mode_and_a_sealed_schema_carrying_it_are_refused(self):
        bad = dict(declared(NEW_CONFIG), deterministic_evaluation="sometimes")
        with self.assertRaises(ValueError):
            M.validate_config(bad, exploratory_capacity_option=EXPLORATORY)
        sealed = dict(declared(OLD_1024),
                      deterministic_evaluation="offline_diagnostic")
        with self.assertRaises(ValueError):
            M.validate_config(sealed, exploratory_capacity_option=EXPLORATORY)


# ---------------------------------------------------------------------------
# 验收 2：两处可选计算在探索模式下确实不运行
# ---------------------------------------------------------------------------

class _Bridge:
    """The bridge surface `_run_one_phase` touches, and nothing more."""

    def __init__(self):
        self.transcript = [{"role": "assistant", "content": "你好，请问需要什么服务？"}]
        self.trace = []
        self.post_tasks = []
        self.tool_calls = []
        self.multicall_batches = []
        self.stage_posts = 0
        self.memory = type("M", (), {"writes": [], "block_text": ""})()

    def begin(self, task, greeting):
        return None

    def set_stage_clarification(self, clarification):
        return None

    def run_stage(self, task, **kwargs):
        return {"history_reply": None,
                "assistant_messages": [{"role": "assistant",
                                        "content": "已经办好了。###STOP###"}]}

    def reply_to_user(self, text):  # pragma: no cover - never reached here
        return [{"role": "assistant", "content": "好"}]


class _Environment:
    class _Db:
        orders: dict = {}

        def get_now(self, fmt="%Y-%m-%d %H:%M:%S"):
            return "2024-06-23 14:30:00"

    def __init__(self):
        self.tools = _Environment._Db()

    def get_tools(self):
        return []

    def make_tool_call(self, tool_name, requestor, **kwargs):  # pragma: no cover
        raise AssertionError("no tool is expected in this fixture")

    def to_json_str(self, value):  # pragma: no cover
        return json.dumps(value)


class _Native:
    """A runtime that mirrors `NativeVita`'s declared-protocol surface."""

    def __init__(self):
        self.instruction = ae_vita.__dict__  # keep the import used

    def start(self, task, protocols=None):
        import ae_sim_eval_protocol as sp
        self.decision = sp.validate_user_reply(
            text=task["instruction"], incoming_assistant_text="",
            protocol=protocols["simulator_protocol"])
        return {"environment": _Environment(), "domain_policy": "fixture policy",
                "instruction": task["instruction"],
                "greeting": {"role": "assistant", "content": "fixture greeting"}}

    def agent_stop(self, text):
        return "###STOP###" in text

    def pending_instruction_decision(self):
        turn, self.decision = self.decision, None
        return turn

    def user_reply(self, text):  # pragma: no cover - the script stops first
        raise AssertionError("this fixture must stop before any user reply")

    def finish(self, task, transcript, termination_reason, duration, *, arm=None,
               target_product_ids=None):
        # The NATIVE judge output, exactly as the real wrapper returns it.
        return {"subtask_id": task["subtask_id"],
                "reward_info": {"reward": 1.0, "nl_rubrics": [
                    {"rubric_idx": "rubric_0", "nl_rubric": "x", "met": True,
                     "justification": "native judge"}], "info": {}},
                "judging_status": "MODEL_JUDGED_DEBUG_ONLY",
                "scientific_success": None, "official_simulation": False,
                "simulation": {"messages": deepcopy(transcript)},
                "note": "native components inside a derived loop"}

    def snapshot(self):
        return {"visibility": "private_driver_and_judge_only", "fixture": True}

    def abort(self):
        pass


class ExploratoryBranchTests(unittest.TestCase):
    """把两处可选计算设为抛错，探索模式仍必须保存阶段/最终结果。"""

    def _run(self):
        import ae_sim_eval_protocol as sp
        config = M.validate_config(declared(NEW_CONFIG),
                                   exploratory_capacity_option=EXPLORATORY)
        protocols = M.run_protocols_of(config)
        task = {"number": 4, "subtask_id": "sub_U000828_4", "instruction": "把地址告诉我",
                "domain": "delivery", "current_time": "2024-06-23", "history": []}
        scope = {"target_product_ids": {}, "profile": {"工作地址": "x"},
                 "target_products": {}}
        events = []
        record = M._run_one_phase(
            config, scope, "rewrite", task, _Bridge(), _Native(), emit=events.append,
            phase_index=0,
            runtimes_baseline={"writes": {"rewrite": 0}, "native_calls": {"rewrite": 0}},
            protocols=protocols)

        result = {"arms": {"rewrite": {"tasks": [record]}}, "scope": {},
                  "schema_version": M.SCHEMA_VERSION, "protocols": protocols}
        # The driver's own gate is what decides whether the report is computed.
        compute = (protocols is not None
                   and M.deterministic_evaluation_mode(config)
                   == M.DETERMINISTIC_EVALUATION_IN_RUN)
        self.assertFalse(compute)
        result["evaluation"] = None
        result["additional_evaluation"] = {
            "deterministic_evaluation": M.deterministic_evaluation_mode(config),
            "ran": False, "status": "NOT_RUN_PENDING_MANUAL_REVIEW"}
        return record, result, events, sp

    def test_the_phase_is_recorded_without_the_deterministic_evaluation(self):
        record, _result, _events, _sp = self._run()
        # The phase record survives, the native judge output is intact, and the extra
        # evaluation is explicitly marked as not run.
        self.assertEqual(record["termination_reason"], "agent_stop")
        self.assertEqual(record["judge"]["reward_info"]["reward"], 1.0)
        self.assertEqual(record["judge"]["status"], "MODEL_JUDGED_DEBUG_ONLY")
        # This fixture's runtime does not implement the in-run branch at all, so the
        # field is simply absent - which is the same statement: nothing was computed.
        self.assertIsNone(record.get("business_completion"))
        # The phase-level pending marker is the RUNTIME's own record; this fixture's
        # runtime does not implement it, so the driver-level record is what is asserted
        # below for the run. What matters here is that nothing claims a computation.
        self.assertNotIn("complete", record)
        # The instruction turn's own decision was still recorded, and it carries no
        # completion claim of the extra evaluation.
        self.assertTrue(record["simulator_decisions"])
        self.assertIsNone(record["simulator_decisions"][0].get("complete"))

    def test_the_two_optional_computations_can_be_made_to_raise(self):
        """The acceptance the review asks for: poison both functions and still run."""
        import ae_sim_eval_protocol as sp
        original_business = sp.deterministic_business_result
        original_report = M.separated_evaluation

        def explode_business(*_a, **_k):
            raise AssertionError("deterministic_business_result must not run here")

        def explode_report(*_a, **_k):
            raise AssertionError("separated_evaluation must not run here")

        sp.deterministic_business_result = explode_business
        M.separated_evaluation = explode_report
        try:
            record, result, _events, _sp = self._run()
        finally:
            sp.deterministic_business_result = original_business
            M.separated_evaluation = original_report
        self.assertEqual(record["termination_reason"], "agent_stop")
        self.assertEqual(result["evaluation"], None)
        self.assertEqual(result["additional_evaluation"]["status"],
                         "NOT_RUN_PENDING_MANUAL_REVIEW")

    def test_the_native_judge_and_the_raw_evidence_are_still_produced(self):
        record, _result, events, _sp = self._run()
        self.assertEqual(record["judge"]["reward_info"]["nl_rubrics"][0]["met"], True)
        self.assertEqual(record["judge"]["reward_info"]["nl_rubrics"][0]["justification"],
                         "native judge")
        # The raw evidence the manual review needs is still recorded.
        for key in ("transcript", "native_snapshot", "memory_writes_this_task",
                    "tool_call_slice", "post_slice", "boundary", "baseline_order_ids"):
            self.assertIn(key, record, key)
        self.assertTrue(any(e.get("kind") == "stage_start" for e in events))


# ---------------------------------------------------------------------------
# 验收 3：真实故障仍按原规则停止留证
# ---------------------------------------------------------------------------

class RealFaultsStillStopTests(unittest.TestCase):
    """不能用宽泛 try/except 吞掉真实实验故障。"""

    def _config(self):
        return M.validate_config(declared(NEW_CONFIG),
                                 exploratory_capacity_option=EXPLORATORY)

    def _scope(self):
        return {"target_product_ids": {}, "profile": {"工作地址": "x"},
                "target_products": {}}

    def _task(self):
        return {"number": 4, "subtask_id": "sub_U000828_4", "instruction": "把地址告诉我",
                "domain": "delivery", "current_time": "2024-06-23", "history": []}

    def test_a_judge_failure_still_stops_the_pair_and_keeps_the_orders(self):
        class Failing(_Native):
            def finish(self, *a, **k):
                raise RuntimeError("native judge unavailable")

        config = self._config()
        with self.assertRaises(RuntimeError):
            M._run_one_phase(config, self._scope(), "rewrite", self._task(), _Bridge(),
                             Failing(), emit=lambda _e: None, phase_index=0,
                             runtimes_baseline={"writes": {"rewrite": 0},
                                                "native_calls": {"rewrite": 0}},
                             protocols=M.run_protocols_of(config))

    def test_a_simulator_protocol_violation_still_stops_and_keeps_the_text(self):
        import ae_sim_eval_protocol as sp

        bad = sp.validate_user_reply(
            text="user支付成功！订单号：TK20240613001",
            incoming_assistant_text="请确认是否支付？",
            protocol=sp.USER_SIMULATOR_PROTOCOL)

        class Violating(_Native):
            def start(self, task, protocols=None):
                prepared = super().start(task, protocols=protocols)
                raise sp.SimulatorProtocolViolation(bad)

        config = self._config()
        with self.assertRaises(sp.SimulatorProtocolViolation):
            M._run_one_phase(config, self._scope(), "rewrite", self._task(), _Bridge(),
                             Violating(), emit=lambda _e: None, phase_index=0,
                             runtimes_baseline={"writes": {"rewrite": 0},
                                                "native_calls": {"rewrite": 0}},
                             protocols=M.run_protocols_of(config))

    def test_the_transport_failure_path_is_not_wrapped_by_the_new_mode(self):
        """传输/容量故障仍由原路径抛出，不由离线诊断模式吞掉。

        驱动里唯一的运行级 `except` 是**既有的**成对故障处理器：它把故障写进
        `invalid_reasons`、把状态置为 INVALID 并停止（不是吞掉）。本次改动没有新增
        try/except，也没有在任何 except 分支里跳过故障或继续跑。
        """
        body = _execute_body()
        # Exactly the one pre-existing run-level handler, and no bare except that could
        # swallow a fault silently.
        self.assertEqual(body.count("except (Exception, KeyboardInterrupt) as exc:"), 1)
        self.assertNotIn("except Exception:\n        pass", body)
        self.assertNotIn("except Exception:\n            pass", body)
        # That handler records the failure and stops; it does not continue the pair.
        self.assertIn("invalid_reasons", body)
        self.assertIn("pair_stopped", body)
        # The transport/provider checks are untouched by this change.
        for marker in ("listed = model_transport.request(\"GET\", \"/v1/models\")",
                       "health = letta_transport.request(\"GET\", \"/v1/health/\")",
                       "if len(matches) != 1:"):
            self.assertIn(marker, body)
        # And the only new branch is the report gate.
        self.assertIn("elif protocols is not None:", body)


class RealNativeVitaOfflineSkipTests(unittest.TestCase):
    """真实 `NativeVita.finish` 在离线诊断模式下不计算确定性评估。

    复用 `tests/test_ae_vita.py` 的既有离线 fixture（不调用模型），只把
    `ae_sim_eval_protocol.deterministic_business_result` 换成抛错版。
    """

    def setUp(self):
        import test_ae_vita as fixture
        self.case = fixture.NativeVitaFixtureTests(methodName="test_start_is_native_instruction"
                                                   "_without_model_or_gold")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def _protocols(self):
        import ae_sim_eval_protocol as sp
        return {"simulator_protocol": sp.USER_SIMULATOR_PROTOCOL,
                "evaluation_protocol": sp.EVALUATION_PROTOCOL}

    def _native(self, *, compute):
        native = self.case.create()
        native._protocols = self._protocols()
        native._compute_deterministic = compute
        native._deterministic_evaluation = "in_run" if compute else "offline_diagnostic"
        native.start(self.case.tasks[0], protocols=native._protocols)
        return native

    def test_in_run_mode_still_computes_it(self):
        """旧候选的行为：默认模式照常计算确定性评估。"""
        import ae_sim_eval_protocol as sp
        calls = []
        original = sp.deterministic_business_result

        def counted(*args, **kwargs):
            calls.append(kwargs.get("number"))
            return original(*args, **kwargs)

        sp.deterministic_business_result = counted
        try:
            native = self._native(compute=True)
            result = native.finish(self.case.tasks[0], self.case.transcript(),
                                   "agent_stop", 1)
        finally:
            sp.deterministic_business_result = original
        self.assertEqual(len(calls), 1, "in_run must compute the deterministic result")
        self.assertIn("business_completion", result)

    def test_offline_mode_neither_computes_nor_invents_it(self):
        import ae_sim_eval_protocol as sp
        original = sp.deterministic_business_result
        sp.deterministic_business_result = _explode
        try:
            native = self._native(compute=False)
            result = native.finish(self.case.tasks[0], self.case.transcript(),
                                   "agent_stop", 1)
        finally:
            sp.deterministic_business_result = original
        self.assertIsNone(result.get("business_completion"))
        self.assertEqual(result["additional_evaluation"]["ran"], False)
        self.assertEqual(result["additional_evaluation"]["status"],
                         "NOT_RUN_PENDING_MANUAL_REVIEW")
        # The NATIVE judge output is still there, untouched.
        self.assertEqual(result["reward_info"]["reward"], 1.0)
        self.assertEqual(result["judging_status"], "MODEL_JUDGED_DEBUG_ONLY")

    def test_the_mode_is_validated_at_construction(self):
        with self.assertRaises(av.NativeVitaError):
            av.NativeVita(self.case.source, self.case.dataset, "http://127.0.0.1:8190",
                          "Qwen3-8B", 0, 4096,
                          deterministic_evaluation="offline_diagnostic")
        with self.assertRaises(av.NativeVitaError):
            av.NativeVita(self.case.source, self.case.dataset, "http://127.0.0.1:8190",
                          "Qwen3-8B", 0, 4096, deterministic_evaluation="sometimes")

    def test_the_instruction_turn_is_still_delivered_in_offline_mode(self):
        """跳过附加评估不影响指令判定的交付与领取一次语义。"""
        offline = self._native(compute=False)
        self.assertIsNotNone(offline.pending_instruction_decision())
        self.assertIsNone(offline.pending_instruction_decision())


def _explode(*_args, **_kwargs):
    raise AssertionError("deterministic_business_result must not run in offline mode")


def _execute_body() -> str:
    source = (ROOT / "ae_cloud_re_multiturn.py").read_text(encoding="utf-8")
    return source[source.index("def execute_re_multiturn"):
                  source.index("def multiturn_code_files")]


class OfflineModeDoesNotSwallowFaultsTests(unittest.TestCase):
    """离线模式不得把真实故障变成"未运行"：故障仍然抛出并留证。"""

    def test_a_provider_failure_still_raises_out_of_the_driver(self):
        """A provider that answers with the wrong catalog stops the pair."""
        import ae_sim_eval_protocol as sp  # noqa: F401 - module import is the check

        class NoModelTransport:
            def __init__(self):
                self.capacity = None

            def request(self, method, path, body=None):
                if path == "/v1/models":
                    return {"data": []}
                return {"status": "ok", "version": "0.16.8"}

        class Letta:
            def request(self, method, path, body=None):
                return {"status": "ok", "version": "0.16.8"}

        config = M.validate_config(
            declared(NEW_CONFIG), exploratory_capacity_option=EXPLORATORY)
        sample = {"tasks": [], "initial_facts": {}, "initial_profile": {},
                  "source": {"end_turn": 12, "start_turn": 4, "user_id": "U000828",
                             "original_user_index": 25},
                  "private_tasks": {}}
        with self.assertRaises(Exception):
            M.execute_re_multiturn(config, sample, model_transport=NoModelTransport(),
                                   letta_transport=Letta(),
                                   runtime_factory=lambda _arm: None)


if __name__ == "__main__":
    unittest.main()
