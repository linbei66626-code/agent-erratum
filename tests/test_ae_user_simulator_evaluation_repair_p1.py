"""P1 补修验收：三个复核反例与直接影响回归。

只覆盖复核指出的三项（P1-1 正常回复被误拒 / P1-2 子项检查被写成整体完成 /
P1-3 新 0.5 未接进事后审计），不跑全仓。

P1-3 使用**真实形状的离线完成态 fixture**：它由生产驱动 `execute_re_multiturn`
在 `ae-cloud-re-multiturn-0.5` 声明下跑出，再交给**公开审计入口**
`audit_re_multiturn_inputs`，而不是用 mock 前置门宣称通过。
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import ae_sim_eval_protocol as P  # noqa: E402
import ae_cloud_re_multiturn as M  # noqa: E402

DATASET = ROOT / ".ae-verify-src/tasks-full-a4553e1.json"
R5_RUN = (ROOT / "transfers/ae-deepseek-re-complete-20260916-r5/deployment/runs"
          / "ae-deepseek-re-live-20260916-r5")
CONFIG_0_5 = (ROOT / "configs"
              / "ae-01__re-multiturn__deepseek-flash.sim-eval-repair-candidate.json")
EXPLORATORY = "--exploratory-capacity-option"


def dataset_available():
    return DATASET.is_file() and R5_RUN.is_dir()


def scoring_scope():
    from ae_inputs import prepare_sample
    return M.scoring_scope(prepare_sample(DATASET, start_turn=4, end_turn=12))


def r5_result():
    return json.loads((R5_RUN / "result.json").read_text(encoding="utf-8"))


def r5_orders(arm, number):
    result = r5_result()
    record = next(t for t in result["arms"][arm]["tasks"] if t["number"] == number)
    return (record["native_snapshot"]["environment_db"]["orders"],
            record["baseline_order_ids"])


# ---------------------------------------------------------------------------
# P1-1 正常用户回复必须通过
# ---------------------------------------------------------------------------

class NormalUserRepliesAreNotViolationsTests(unittest.TestCase):
    """复核给出的三类正常回复：一轮多行、称呼助手、引用后确认。"""

    def test_a_multi_line_user_turn_is_not_a_violation(self):
        for text in ("请送到公司。\n不要加糖。",
                     "帮我买两杯。\n一杯少冰。\n另一杯常温。",
                     "我要牛肉塔克。\n大份的。\n送到店里。"):
            with self.subTest(text=text):
                d = P.validate_user_reply(text=text)
                self.assertEqual(d["violations"], [], d)
                self.assertEqual(d["claims"], [], d)
                self.assertFalse(d["stop"])
                self.assertGreater(d["body_lines"], 1)

    def test_addressing_the_assistant_is_not_writing_its_turn(self):
        for text in ("助手，你能帮我确认地址吗？",
                     "小助手，这个多少钱？",
                     "智能体，帮我看看还有没有别的店",
                     "小助手在吗"):
            with self.subTest(text=text):
                d = P.validate_user_reply(text=text)
                self.assertEqual(d["violations"], [], d)

    def test_quoting_one_agent_line_and_then_confirming_is_not_a_violation(self):
        incoming = ("我推荐 塔克兄弟(地五大道店) 的「牛肉塔克(大份)」34元，"
                    "含莎莎酱和牛油果酱。\n\n请问确认这款吗？")
        for text in ("塔克兄弟(地五大道店) 的「牛肉塔克(大份)」34元，含莎莎酱和牛油果酱。\n"
                     "就这个吧。",
                     "「牛肉塔克(大份)」34元，含莎莎酱和牛油果酱。就这个吧。",
                     "你推荐的那个就行，确认。"):
            with self.subTest(text=text):
                d = P.validate_user_reply(text=text, incoming_assistant_text=incoming)
                self.assertEqual(d["violations"], [], d)

    def test_pasting_the_agents_whole_turn_is_still_a_violation(self):
        incoming = ("已为你下单，订单信息如下：\n\n"
                    "- 商家：塔克兄弟(地五大道店)\n"
                    "- 商品：牛肉塔克(大份) 加香菜\n"
                    "- 价格：34元 预计12:15送达\n"
                    "- 备注：加香菜不要洋葱\n"
                    "- 配送地址：地五大道88号\n\n请确认是否支付？")
        pasted = ("已为你下单，订单信息如下：\n\n"
                  "- 商家：塔克兄弟(地五大道店)\n"
                  "- 商品：牛肉塔克(大份) 加香菜\n"
                  "- 价格：34元 预计12:15送达")
        d = P.validate_user_reply(text=pasted, incoming_assistant_text=incoming)
        self.assertIn(P.VIOLATION_QUOTED_AGENT_LINE,
                      [v["code"] for v in d["violations"]])

    def test_one_quoted_line_is_never_a_violation(self):
        incoming = ("我推荐 塔克兄弟(地五大道店) 的「牛肉塔克(大份)」34元，含莎莎酱和牛油果酱。\n"
                    "请问确认这款吗？")
        d = P.validate_user_reply(
            text="塔克兄弟(地五大道店) 的「牛肉塔克(大份)」34元，含莎莎酱和牛油果酱。就它了。",
            incoming_assistant_text=incoming)
        self.assertEqual(d["violations"], [], d)

    def test_the_sealed_r5_overreach_turns_are_still_refused(self):
        if not dataset_available():
            self.skipTest("the sealed r5 record is not present on this host")
        rows = P.replay_sealed_user_replies(r5_result())
        for row in rows:
            with self.subTest(instance=row["id"]):
                if row["expect"] == "violation":
                    self.assertEqual(row["decision"]["outcome"],
                                     "SIMULATOR_PROTOCOL_VIOLATION")
                else:
                    self.assertEqual(row["decision"]["outcome"], "USER_STOP_ACCEPTED")

    def test_ordinary_payment_talk_is_still_allowed(self):
        incoming = "订单号：OI1\n状态：待支付\n请确认是否支付？"
        for text in ("确认支付。", "好，支付吧。", "我要怎么支付？",
                     "你是说已经支付成功了吗？", "请帮我支付，谢谢"):
            with self.subTest(text=text):
                d = P.validate_user_reply(text=text, incoming_assistant_text=incoming,
                                          new_order_ids=["OI1"])
                self.assertEqual(d["violations"], [], d)
                self.assertEqual(d["claims"], [], d)


# ---------------------------------------------------------------------------
# P1-2 子项检查不得被写成整体完成
# ---------------------------------------------------------------------------

class DeterministicSubItemTests(unittest.TestCase):
    """真实 t4 投影 + r5 R t4 真实订单的最小变异。"""

    def setUp(self):
        if not dataset_available():
            self.skipTest("the fixed dataset or the sealed r5 record is absent")
        self.scope = scoring_scope()
        self.task = next(t for t in self.scope["tasks"] if t["number"] == 4)
        self.orders, self.baseline = r5_orders("rewrite", 4)

    def business(self, orders, task=None, scope_task=None):
        task = task or self.task
        return P.deterministic_business_result(
            arm="rewrite", number=4, task=task,
            scope_task_record=scope_task or task, orders=orders,
            baseline_order_ids=self.baseline,
            dataset_rubric_texts=task["rubrics"],
            standard_item_pool=task.get("standard_items", []))

    def mutate(self, fn):
        orders = copy.deepcopy(self.orders)
        fn(orders[next(iter(orders))])
        return self.business(orders)

    def test_the_real_correct_order_is_reported_as_covering_its_checks(self):
        business = self.business(self.orders)
        self.assertTrue(business["complete"], business)
        self.assertEqual(business["unmet_requirements"], [])
        self.assertEqual(business["uncovered_requirements"], [])
        self.assertEqual(business["uncovered_standard_items"], [])
        self.assertEqual(business["new_order_ids"], ["OTf2297e7b31"])

    def test_a_cancelled_order_is_not_a_completed_purchase(self):
        business = self.mutate(lambda o: o.__setitem__("status", "cancelled"))
        self.assertFalse(business["complete"])
        self.assertIn("order_not_cancelled", business["unmet_requirements"])

    def test_a_wrong_spec_fails_the_spec_check(self):
        business = self.mutate(lambda o: o["products"][0].__setitem__(
            "attributes", "规格: 全糖, 冰度: 少冰, 容量: 大杯"))
        self.assertFalse(business["complete"])
        self.assertIn("items_match_spec_values", business["unmet_requirements"])
        rule = business["requirements"]["items_match_spec_values"]["rule"]
        self.assertEqual([x["value"] for x in rule], ["7分糖"])

    def test_a_wrong_or_missing_address_fails_the_address_check(self):
        for label, mutation in (
                ("wrong", lambda o: o.__setitem__(
                    "location", {"address": "甘肃省兰州市安宁区别的地址1号"})),
                ("missing", lambda o: o.__setitem__("location", None))):
            with self.subTest(case=label):
                business = self.mutate(mutation)
                self.assertFalse(business["complete"])
                self.assertIn("delivery_at_work_address", business["unmet_requirements"])

    def test_a_missing_target_rule_is_uncovered_not_met(self):
        task = dict(self.task, target_product_ids=[], standard_items=[
            dict(x) for x in self.task["standard_items"]
            if "商品" not in x["rubric"] or "规格" in x["rubric"]])
        business = self.business(self.orders, task=task, scope_task=task)
        self.assertFalse(business["complete"],
                         "a missing standard must never be reported as satisfied")
        self.assertIn("items_match_target_product", business["uncovered_requirements"])
        self.assertFalse(
            business["requirements"]["items_match_target_product"]["covered"])

    def test_an_uncovered_standard_item_blocks_a_completion_claim(self):
        """A standard clause this module has no rule for must not be silently passed."""
        task = dict(self.task, standard_items=list(self.task["standard_items"]) + [
            {"rubric_id": "rubric_soft", "rubric": "优先选择距离用户当前位置最近的店铺",
             "source": "dataset_evaluation_criteria"}])
        business = self.business(self.orders, task=task, scope_task=task)
        self.assertFalse(business["complete"])
        ids = [x["rubric_id"] for x in business["uncovered_standard_items"]]
        self.assertEqual(ids, ["rubric_soft"])
        self.assertEqual(business["unmet_requirements"], [],
                         "the blocked reason is COVERAGE, not an unmet check")

    def test_the_address_check_reads_the_location_structurally(self):
        """`str(dict) == address` silently passed every order; it must not come back."""
        business = self.business(self.orders)
        address = business["requirements"]["delivery_at_work_address"]
        self.assertTrue(address["met"])
        self.assertEqual(address["value"], ["甘肃省兰州市安宁区安宁西路地五大道88号"])
        row = business["purchase_facts"][0]
        self.assertEqual(row["address"], "甘肃省兰州市安宁区安宁西路地五大道88号")
        self.assertNotIn("{", str(row["address"]))

    def test_the_standard_source_is_the_dataset_not_a_positional_label(self):
        business = self.business(self.orders)
        self.assertEqual(business["checked_requirements"],
                         sorted(business["checked_requirements"]))
        self.assertIn("order_created_in_this_phase", business["checked_requirements"])
        self.assertEqual(business["completeness_basis"],
                         "all covered requirements met AND no uncovered standard item")

    def test_the_native_reward_is_untouched_by_this_layer(self):
        if not dataset_available():
            self.skipTest("the sealed r5 record is not present")
        result = r5_result()
        report = P.evaluate_sealed_r5_run(result=result, scope=self.scope)
        recorded = {(arm, t["number"]): t["judge"]["reward_info"]["reward"]
                    for arm, a in result["arms"].items() for t in a["tasks"]}
        for phase in report["phases"]:
            self.assertEqual(phase["native_rubric_score"]["reward"],
                             recorded[(phase["arm"], phase["task_number"])])


# ---------------------------------------------------------------------------
# P1-3 0.5 必须接进事后审计
# ---------------------------------------------------------------------------

class AuditAccepts0_5Tests(unittest.TestCase):
    """公开审计入口对真实形状 0.5 完成态 fixture 的判定与抗篡改。"""

    @classmethod
    def setUpClass(cls):
        if not dataset_available():
            raise unittest.SkipTest("the fixed dataset or the r5 record is absent")
        import re_multiturn_chain as chain
        import tempfile
        cls.chain = chain
        cls._tmp = tempfile.TemporaryDirectory()
        try:
            cls.fixture = chain.build_full_chain(
                cls._tmp.name, unsealed=True, config_path=CONFIG_0_5)
            cls.error = None
        except Exception as exc:  # noqa: BLE001 - reported as a test failure below
            cls.fixture, cls.error = None, exc

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def audit(self, fixture=None):
        fixture = fixture or self.fixture
        return self.chain.audit_of(fixture)

    def fresh_fixture(self, name, mutate):
        """Build a 0.5 chain, apply `mutate` to the RESULT, and re-save it.

        The audit reads the run's own files, so a tamper test has to change the bytes
        on disk; mutating the in-memory dict alone would audit the untampered record.
        """
        fixture = self.chain.build_full_chain(Path(self._tmp.name) / name,
                                              unsealed=True, config_path=CONFIG_0_5)
        mutate(fixture["result"])
        # `write_json` is exclusive by design (a real run never overwrites), so a
        # tamper test REPLACES the bytes it is auditing.
        (Path(fixture["run_dir"]) / "result.json").write_text(
            json.dumps(fixture["result"], ensure_ascii=False, indent=1),
            encoding="utf-8")
        return fixture

    def test_the_fixture_really_ran_under_the_declared_0_5_protocols(self):
        self.assertIsNone(self.error, f"building the 0.5 fixture failed: {self.error}")
        result = self.fixture["result"]
        self.assertEqual(result["config"]["schema_version"], M.SCHEMA_VERSION_SIM_EVAL)
        self.assertEqual(result["protocols"], M.run_protocols_of(result["config"]))
        self.assertIsInstance(result.get("evaluation"), dict)
        phases = [t for a in result["arms"].values() for t in a["tasks"]]
        self.assertEqual(len(phases), 18)
        for phase in phases:
            self.assertEqual(phase["protocols"], result["protocols"])
            self.assertEqual(phase["protocol_bundle_sha256"],
                             P.protocol_bundle_sha256(result["protocols"]))

    def test_the_public_audit_entry_accepts_the_0_5_record(self):
        self.assertIsNone(self.error, f"building the 0.5 fixture failed: {self.error}")
        report = self.audit()
        self.assertNotIn("wrong_multiturn_schema",
                         [x.get("code") for x in report["invalid_reasons"]])
        recorded = report.get("repair_protocols") or {}
        self.assertTrue(recorded.get("declared"), report["invalid_reasons"])
        self.assertEqual(recorded.get("phases"), 18)
        self.assertGreater(recorded.get("simulator_prompts") or 0, 0)
        self.assertEqual(recorded.get("bundle"),
                         {"simulator_protocol": P.USER_SIMULATOR_PROTOCOL,
                          "evaluation_protocol": P.EVALUATION_PROTOCOL})

    def test_an_unknown_protocol_version_is_refused(self):
        self.assertIsNone(self.error)
        payload = json.loads(CONFIG_0_5.read_text(encoding="utf-8"))
        payload["simulator_protocol"] = "not-a-reviewed-version"
        tampered = Path(self._tmp.name) / "tampered-config.json"
        tampered.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            self.chain.build_full_chain(self._tmp.name + "/unknown", unsealed=True,
                                        config_path=tampered)
        except Exception as exc:  # noqa: BLE001 - the refusal IS the expected outcome
            self.assertIn("protocol", str(exc).lower())
            return
        self.fail("a config declaring an unreviewed simulator protocol was accepted")

    def test_tampering_with_the_evaluation_record_is_refused(self):
        self.assertIsNone(self.error)
        fixture = self.fresh_fixture(
            "ev", lambda r: r["evaluation"].__setitem__("bundle_sha256", "0" * 64))
        report = self.audit(fixture)
        self.assertIn("evaluation_bundle_digest_changed",
                      [x.get("code") for x in report["invalid_reasons"]])
        self.assertFalse(report["input_audit_passed"])

    def test_tampering_with_a_phase_bundle_is_refused(self):
        self.assertIsNone(self.error)

        def drop_the_bundle(result):
            first = result["arms"]["rewrite"]["tasks"][0]
            first["protocols"] = None
            first["protocol_bundle_sha256"] = None

        fixture = self.fresh_fixture("phase", drop_the_bundle)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(any(c.startswith("phase_protocol_bundle") for c in codes), codes)

    def test_tampering_with_the_simulator_prompt_is_refused(self):
        self.assertIsNone(self.error)

        def strip_the_constraints(result):
            events = (result["arms"]["rewrite"]["tasks"][0]["native_snapshot"]
                      .get("events") or [])
            for event in events:
                if event.get("event") == "start":
                    event["user_system_prompt"] = "NATIVE personalization user prompt"

        fixture = self.fresh_fixture("prompt", strip_the_constraints)
        report = self.audit(fixture)
        self.assertIn("simulator_system_prompt_lacks_the_declared_constraints",
                      [x.get("code") for x in report["invalid_reasons"]])

    def test_a_violating_turn_accepted_as_a_stop_is_refused(self):
        self.assertIsNone(self.error)

        def accept_a_violation(result):
            # Only the self-consistency of ONE decision changes: the count still matches
            # the phase's real user turns, so the count/recompute checks stay satisfied
            # and this test isolates "a violating turn was accepted as a stop".
            phase = result["arms"]["rewrite"]["tasks"][0]
            decisions = phase["simulator_decisions"]
            index = len(decisions) // 2
            decisions[index] = dict(
                decisions[index], stop=True, user_ended_normally=True,
                violations=[{"code": "simulator_wrote_assistant_turn",
                             "detail": "fixture", "evidence": []}])

        fixture = self.fresh_fixture("stop", accept_a_violation)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(
            "a_violating_simulator_turn_was_accepted_as_a_stop" in codes
            or any("not_reproducible" in c for c in codes), codes)
        self.assertFalse(report["input_audit_passed"])

    def test_a_completion_claim_with_uncovered_checks_is_refused(self):
        """Two independent refusals apply; either one is enough to reject the record."""
        self.assertIsNone(self.error)

        def overclaim(result):
            # Pick a phase whose RECOMPUTED result is incomplete, and change only the
            # claim fields, so the recomputation comparison cannot be what refuses it -
            # only the claim's own consistency can.
            for phase in result["evaluation"]["phases"]:
                business = phase["business_completion"]
                if business["complete"] is True:
                    continue
                business["complete"] = True
                business["unmet_requirements"] = []
                business["uncovered_standard_items"] = []
                business["uncovered_requirements"] = ["delivery_at_work_address"]
                return
            raise AssertionError("the fixture has no incomplete phase to overclaim")

        fixture = self.fresh_fixture("cov", overclaim)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(
            "business_complete_with_uncovered_requirements" in codes
            or any("not_reproducible_from_the_phase_database" in c for c in codes),
            codes)
        self.assertFalse(report["input_audit_passed"])

    def test_deleting_every_simulator_decision_is_refused(self):
        """反例：把各阶段 simulator_decisions 全部清空，仍被判 VALID。"""
        self.assertIsNone(self.error)

        def delete_decisions(result):
            for arm in result["arms"]:
                for task in result["arms"][arm]["tasks"]:
                    task["simulator_decisions"] = []

        fixture = self.fresh_fixture("nodec", delete_decisions)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(
            any(c.startswith("simulator_decision_count_differs_from_the_user_turns")
                for c in codes), codes)
        self.assertFalse(report["input_audit_passed"])

    def test_a_duplicated_or_invented_decision_is_refused(self):
        self.assertIsNone(self.error)

        def duplicate_one(result):
            task = result["arms"]["rewrite"]["tasks"][0]
            decisions = task["simulator_decisions"]
            task["simulator_decisions"] = decisions + [dict(decisions[-1])]

        fixture = self.fresh_fixture("dupdec", duplicate_one)
        report = self.audit(fixture)
        self.assertFalse(report["input_audit_passed"])
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(any(c.startswith("simulator_decision_count_differs")
                            for c in codes), codes)

    def test_a_flipped_decision_is_refused(self):
        """A decision that says `stop: true, violations: []` where the turn violated."""
        self.assertIsNone(self.error)

        def flip_one(result):
            task = result["arms"]["rewrite"]["tasks"][0]
            task["simulator_decisions"] = [
                dict(d, stop=True, outcome="USER_STOP_ACCEPTED", violations=[],
                     claims=[], user_ended_normally=True)
                for d in task["simulator_decisions"]]

        fixture = self.fresh_fixture("flipdec", flip_one)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(any("not_reproducible" in c for c in codes), codes)

    def test_a_fabricated_business_record_is_refused(self):
        """反例：伪造 complete/requirements/FAKE_ORDER，仍被判 VALID。"""
        self.assertIsNone(self.error)

        def fabricate(result):
            for phase in result["evaluation"]["phases"]:
                bc = phase["business_completion"]
                bc.update(requirements={}, complete=True, unmet_requirements=[],
                          uncovered_requirements=[], uncovered_standard_items=[],
                          new_order_ids=["FAKE_ORDER"])

        fixture = self.fresh_fixture("fake", fabricate)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(any("not_reproducible_from_the_phase_database" in c
                            for c in codes), codes)
        self.assertFalse(report["input_audit_passed"])

    def test_a_deleted_order_from_the_database_is_refused(self):
        """Deleting the phase's real order from its own DB cannot pass the recomputation."""
        self.assertIsNone(self.error)

        def drop_the_order(result):
            task = result["arms"]["rewrite"]["tasks"][0]
            snapshot = task["native_snapshot"]
            snapshot["environment_db"]["orders"] = {}

        fixture = self.fresh_fixture("noorder", drop_the_order)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        # The tool evidence of THIS phase created that order, so a table without it has
        # been edited - the phase's own evidence is what refuses it.
        self.assertTrue(any("phase_order_missing_from_the_captured_table" in c
                            or "not_reproducible" in c or "order_state" in c
                            for c in codes), codes)
        self.assertFalse(report["input_audit_passed"])

    def test_a_completion_claim_without_a_phase_order_is_refused(self):
        self.assertIsNone(self.error)

        def claim_without_an_order(result):
            phase = result["evaluation"]["phases"][0]
            bc = phase["business_completion"]
            bc.update(complete=True, unmet_requirements=[],
                      uncovered_requirements=[], uncovered_standard_items=[])

        fixture = self.fresh_fixture("noorderclaim", claim_without_an_order)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(any("not_reproducible_from_the_phase_database" in c
                            for c in codes), codes)

    def test_a_business_record_that_claims_coverage_it_never_ran_is_refused(self):
        """反例一：把一条本模块没有实现的标准也算作已覆盖。"""
        self.assertIsNone(self.error)

        def claim_unimplemented_coverage(result):
            # The phase records a standard item whose sub-claim no check answers, yet
            # leaves `complete` true and the uncovered list empty.
            phase = result["evaluation"]["phases"][0]
            business = phase["business_completion"]
            business["uncovered_standard_items"] = []
            business["complete"] = True
            business["unmet_requirements"] = []
            business["uncovered_requirements"] = []

        fixture = self.fresh_fixture("fakecov", claim_unimplemented_coverage)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(any("not_reproducible_from_the_phase_database" in c
                            for c in codes), codes)
        self.assertFalse(report["input_audit_passed"])

    # --- 判定时序：按“当时的可见输入”重算 -------------------------------------

    def test_a_turn_is_judged_against_the_table_visible_at_that_turn(self):
        """反例：早期用户引用了**后来才存在**的订单号。

        生产函数上先复现：该轮当时表里没有这个 ID → unknown_order_id；若用**终态**
        表重算则变成 USER_TURN_ACCEPTED。修复后审计必须按逐轮表重算并拒绝。
        """
        self.assertIsNone(self.error)
        missing = "AB1234567890"
        live = P.validate_user_reply(
            text=f"订单 {missing} 怎么还没付款？", incoming_assistant_text="请确认是否支付？",
            visible_orders={})
        self.assertEqual(live["outcome"], "SIMULATOR_PROTOCOL_VIOLATION")
        self.assertIn(P.VIOLATION_UNKNOWN_ORDER_ID,
                      [v["code"] for v in live["violations"]])
        final = P.validate_user_reply(
            text=f"订单 {missing} 怎么还没付款？", incoming_assistant_text="请确认是否支付？",
            visible_orders={missing: "paid"})
        self.assertEqual(final["outcome"], "USER_TURN_ACCEPTED")

        def pad_an_earlier_turn(result):
            # Keep every OTHER byte identical and give turn 0 a table that contains an
            # order no tool of that phase had created that early. This is the timing
            # defect exactly: the audit must notice that the turn's own visible input
            # did not contain that order.
            task = result["arms"]["rewrite"]["tasks"][1]
            states = task["order_state_by_turn"]
            states[0] = dict(states[0],
                             orders=dict(states[0]["orders"], **{missing: "paid"}),
                             tool_calls_so_far=0)

        fixture = self.fresh_fixture("timing", pad_an_earlier_turn)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(any("order_state" in c or "not_reproducible" in c
                            for c in codes), codes)
        self.assertFalse(report["input_audit_passed"])

    def test_a_table_padded_with_a_future_order_is_refused(self):
        """逐轮状态不得含有该轮之前没有任何工具创建过的订单。"""
        self.assertIsNone(self.error)

        def pad_an_early_turn(result):
            task = result["arms"]["rewrite"]["tasks"][1]
            states = task.get("order_state_by_turn") or []
            states[0] = dict(states[0],
                             orders=dict(states[0]["orders"], AB1234567890="paid"))

        fixture = self.fresh_fixture("padstate", pad_an_early_turn)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(any("order_state" in c or "not_reproducible" in c
                            for c in codes), codes)

    def test_the_per_turn_state_must_reach_the_captured_table(self):
        self.assertIsNone(self.error)

        def drop_a_state(result):
            task = result["arms"]["rewrite"]["tasks"][1]
            task["order_state_by_turn"] = (task.get("order_state_by_turn") or [])[:-1]

        fixture = self.fresh_fixture("shortstate", drop_a_state)
        report = self.audit(fixture)
        codes = [x.get("code") for x in report["invalid_reasons"]]
        self.assertTrue(any("order_state" in c or "not_reproducible" in c
                            for c in codes), codes)

    def test_the_sealed_r5_record_is_refused_by_the_audit_as_before(self):
        """A sealed 0.1 record declares no protocols, so the new gate checks nothing."""
        self.assertIsNone(self.error)
        report = self.audit()
        # The gate must not have fired for a run that declares nothing.
        self.assertNotIn("declared_repair_protocol_unknown",
                         [x.get("code") for x in report["invalid_reasons"]])


# ---------------------------------------------------------------------------
# 判定时序：指令判定必须在阶段开始时就交回
# ---------------------------------------------------------------------------

class InstructionDecisionTimingTests(unittest.TestCase):
    """零次 user_reply 的路径也必须留下判定。

    Agent 首次回复就 STOP 时驱动直接 finish，`user_reply` 从未被调用；指令轮的用户
    消息仍在 transcript 里，所以判定必须在 `start` 之后就交回。
    """

    def _config(self):
        return M.validate_config(json.loads(CONFIG_0_5.read_text(encoding="utf-8")),
                                 exploratory_capacity_option=EXPLORATORY)

    def _task(self):
        return {"number": 4, "subtask_id": "sub_U000828_4",
                "instruction": "把店里地址告诉我", "domain": "delivery",
                "current_time": "2024-06-23", "history": []}

    def _scope(self):
        return {"target_products": {},
                "profile": {"工作地址": "甘肃省兰州市安宁区安宁西路地五大道88号"}}

    def _run(self, first_reply, user_replies=()):
        """Drive the REAL `_run_one_phase` with a scripted native boundary."""
        from test_ae_user_simulator_evaluation_repair import _FakeBridge, _ViolatingNative

        class Bridge(_FakeBridge):
            def run_stage(self, task, **kwargs):
                return {"history_reply": None,
                        "assistant_messages": [{"role": "assistant", "content": first_reply}]}

        class Native(_ViolatingNative):
            def __init__(self):
                super().__init__(None)
                self.script = list(user_replies)
                self.reply_calls = 0
                self.instruction_decisions_claimed = 0
                self.instruction_decision = P.validate_user_reply(
                    text="把店里地址告诉我", incoming_assistant_text="",
                    protocol=P.USER_SIMULATOR_PROTOCOL)

            def agent_stop(self, text):
                return "###STOP###" in text

            def pending_instruction_decision(self):
                self.instruction_decisions_claimed += 1
                decision, self.instruction_decision = self.instruction_decision, None
                return decision

            def user_reply(self, text):
                self.reply_calls += 1
                content = (self.script.pop(0) if self.script
                           else "没有了，谢谢。###STOP###")
                decision = P.validate_user_reply(
                    text=content, incoming_assistant_text=text,
                    protocol=P.USER_SIMULATOR_PROTOCOL)
                return {"content": content, "stop": decision["stop"], "raw": {},
                        "protocol_decision": decision}

            def finish(self, task, transcript, termination_reason, duration, *, arm=None,
                       target_product_ids=None):
                return {"reward_info": {"reward": 0.0},
                        "judging_status": "MODEL_JUDGED_DEBUG_ONLY"}

        config = self._config()
        native = Native()
        events = []
        record = M._run_one_phase(
            config, self._scope(), "rewrite", self._task(), Bridge(), native,
            emit=events.append, phase_index=0,
            runtimes_baseline={"writes": {"rewrite": 0}, "native_calls": {"rewrite": 0}},
            protocols=M.run_protocols_of(config))
        return record, native, events

    def test_zero_user_replies_still_records_the_instruction_decision(self):
        record, native, events = self._run("已完成，还有别的需要吗？###STOP###")
        self.assertEqual(record["termination_reason"], "agent_stop")
        self.assertEqual(native.reply_calls, 0, "no user turn should have been needed")
        self.assertEqual(len(record["simulator_decisions"]), 1)
        self.assertEqual(record["simulator_decisions"][0]["protocol"],
                         P.USER_SIMULATOR_PROTOCOL)
        self.assertEqual(native.instruction_decisions_claimed, 1,
                         "the instruction decision must be claimed exactly once")
        self.assertEqual(len(record["order_state_by_turn"]), 1)

    def test_one_user_reply_records_both_turns_in_order(self):
        record, native, _events = self._run(
            "请问是要送到哪个地址？", ["送到公司，谢谢。", "没有了，谢谢。###STOP###"])
        self.assertEqual(record["termination_reason"], "user_stop")
        self.assertEqual(native.reply_calls, 2)
        # instruction turn + two user turns, in order, and the instruction turn is first.
        self.assertEqual(len(record["simulator_decisions"]), 3)
        self.assertEqual([d["outcome"] for d in record["simulator_decisions"]],
                         ["USER_TURN_ACCEPTED", "USER_TURN_ACCEPTED",
                          "USER_STOP_ACCEPTED"])
        self.assertEqual([s["turn_index"] for s in record["order_state_by_turn"]],
                         [0, 1, 2])

    def test_the_instruction_decision_is_not_handed_out_twice(self):
        record, native, _events = self._run(
            "请问是要送到哪个地址？", ["没有了，谢谢。###STOP###"])
        self.assertEqual(native.instruction_decisions_claimed, 1)
        first = record["simulator_decisions"][0]
        self.assertNotIn(first, record["simulator_decisions"][1:],
                         "the instruction decision must appear once")

    def test_a_violating_instruction_turn_still_stops_the_phase(self):
        from test_ae_user_simulator_evaluation_repair import _FakeBridge, _ViolatingNative

        class Bridge(_FakeBridge):
            def run_stage(self, task, **kwargs):
                return {"history_reply": None,
                        "assistant_messages": [{"role": "assistant", "content": "好"}]}

        # A decision the PRODUCTION validator really produces for a violating turn.
        bad = P.validate_user_reply(
            text="user支付成功！订单号：TK20240613001",
            incoming_assistant_text="请确认是否支付？",
            protocol=P.USER_SIMULATOR_PROTOCOL)
        self.assertIn(P.VIOLATION_ASSISTANT_TURN,
                      [v["code"] for v in bad["violations"]])

        class Native(_ViolatingNative):
            def __init__(self):
                super().__init__(None)
                self.instruction_decision = bad

            def agent_stop(self, text):
                return "###STOP###" in text

            def pending_instruction_decision(self):
                decision, self.instruction_decision = self.instruction_decision, None
                return decision

            def start(self, task, protocols=None):
                prepared = super().start(task, protocols=protocols)
                raise P.SimulatorProtocolViolation(self.instruction_decision)

            def finish(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("a refused phase must never reach the judge")

        config = self._config()
        with self.assertRaises(P.SimulatorProtocolViolation):
            M._run_one_phase(config, self._scope(), "rewrite", self._task(), Bridge(),
                             Native(), emit=lambda _e: None, phase_index=0,
                             runtimes_baseline={"writes": {"rewrite": 0},
                                                "native_calls": {"rewrite": 0}},
                             protocols=M.run_protocols_of(config))


if __name__ == "__main__":
    unittest.main()
