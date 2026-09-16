"""Directed tests for the r5 repair: user-simulator boundary and split evaluation.

Scope of this file: ONLY the two defects this repair was opened for, plus the
wiring that puts the new rules on the NEXT round's real entry point. The complete
repository suite is deliberately not re-run here.

Every sealed instance used below is replayed OFFLINE from the r5 record
(`transfers/ae-deepseek-re-complete-20260916-r5/`); nothing here calls a model,
opens a socket, or writes to any r5 artifact.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ae_sim_eval_protocol as P  # noqa: E402
import ae_cloud_re_multiturn as M  # noqa: E402

R5_RUN = (ROOT / "transfers/ae-deepseek-re-complete-20260916-r5/deployment/runs"
          / "ae-deepseek-re-live-20260916-r5")
R5_RESULT = R5_RUN / "result.json"
G5_CONFIG = (ROOT / "configs"
             / "ae-01__re-multiturn__deepseek-flash.sim-eval-repair-candidate.json")
G4_CONFIG = (ROOT / "configs"
             / "ae-01__re-multiturn__deepseek-flash.serial-candidate.json")
DATASET = ROOT / ".ae-verify-src/tasks-full-a4553e1.json"
#: The DeepSeek candidate's own required opt-in. Every DeepSeek declaration is
#: refused without it, so a test that validates one has to pass it - exactly like
#: the CLI does.
EXPLORATORY = "--exploratory-capacity-option"


def r5_result():
    if not R5_RESULT.is_file():
        raise unittest.SkipTest("the sealed r5 record is not present on this host")
    return json.loads(R5_RESULT.read_text(encoding="utf-8"))


def r5_scoring_scope():
    """The scoring scope for the sealed r5 tasks, from the verified projection."""
    if not DATASET.is_file():
        raise unittest.SkipTest("the fixed dataset is not present on this host")
    from ae_inputs import prepare_sample
    return M.scoring_scope(prepare_sample(DATASET, start_turn=4, end_turn=12))


def user_turn(result, arm, task, index):
    record = next(t for t in result["arms"][arm]["tasks"] if t["number"] == task)
    transcript = record["transcript"]
    incoming = ""
    for back in range(index - 1, -1, -1):
        if transcript[back].get("role") == "assistant":
            incoming = str(transcript[back].get("content") or "")
            break
    snapshot = record.get("native_snapshot") or {}
    orders = ((snapshot.get("environment_db") or {}).get("orders")) or {}
    baseline = record.get("baseline_order_ids") or []
    return {
        "text": transcript[index].get("content"),
        "incoming": incoming,
        "record": record,
        "new_order_ids": sorted(P.new_orders_of(orders, baseline)),
        "paid_order_ids": sorted(P.paid_orders_of(orders, baseline)),
        "orders": orders,
        "baseline": baseline,
    }


def validate_turn(result, arm, task, index, **kwargs):
    turn = user_turn(result, arm, task, index)
    return P.validate_user_reply(
        text=turn["text"], incoming_assistant_text=turn["incoming"],
        new_order_ids=turn["new_order_ids"], paid_order_ids=turn["paid_order_ids"],
        **kwargs)


# ---------------------------------------------------------------------------
# 1. The ordinary user paths must still work
# ---------------------------------------------------------------------------

class OrdinaryUserBehaviourTests(unittest.TestCase):
    """A user may confirm, refuse, ask and END. The protocol must allow all of it."""

    def test_a_normal_confirmation_can_continue_the_conversation(self):
        for text in ("可以，就这个吧。", "好，支付吧。", "确认支付。", "嗯嗯确认下单",
                     "你看着办吧，哪个合适就买哪个。"):
            with self.subTest(text=text):
                d = P.validate_user_reply(text=text, incoming_assistant_text="请确认是否支付？")
                self.assertEqual(d["violations"], [], d)
                self.assertEqual(d["claims"], [], d)
                self.assertFalse(d["stop"])
                self.assertEqual(d["outcome"], "USER_TURN_ACCEPTED")

    def test_a_normal_user_ending_ends_the_conversation(self):
        for text in ("没有了，谢谢。\n###STOP###", "没有了，谢谢。 ###STOP###",
                     "没别的事了\n###STOP###", "没有了，谢谢，正好去放松一下。 ###STOP###"):
            with self.subTest(text=text):
                d = P.validate_user_reply(text=text, incoming_assistant_text="还有其他需要帮忙的吗？")
                self.assertEqual(d["violations"], [], d)
                self.assertTrue(d["original_stop_marker"])
                self.assertTrue(d["stop"])
                self.assertTrue(d["user_ended_normally"])
                self.assertEqual(d["outcome"], "USER_STOP_ACCEPTED")

    def test_mentioning_payment_normally_is_not_a_violation(self):
        """The boundary is about FABRICATION, not about the word 支付."""
        incoming = ("已为你下单，订单号 OI4ddc1d9026，状态：待支付。\n\n请确认是否支付？")
        for text in ("确认支付。", "好，支付吧。", "我要怎么支付？", "你是说已经支付成功了吗？",
                     "请帮我支付，谢谢", "还要支付吗？", "能不支付吗？",
                     "你上一条说订单 OI4ddc1d9026 待支付，那就付吧。"):
            with self.subTest(text=text):
                d = P.validate_user_reply(text=text, incoming_assistant_text=incoming,
                                          new_order_ids=["OI4ddc1d9026"])
                self.assertEqual(d["violations"], [], d)
                self.assertEqual(d["claims"], [], d)

    def test_a_users_own_mention_of_itself_is_not_a_role_label(self):
        """The label rule must not fire on the word 用户 used in its ordinary sense."""
        for text in ("我的用户ID是 U000828。", "用户就是我啊。", "用户号是多少？",
                     "我今天想放松一下。"):
            with self.subTest(text=text):
                d = P.validate_user_reply(text=text)
                self.assertEqual(d["violations"], [], d)

    def test_a_glued_role_label_is_a_violation(self):
        """The r5 instances wrote `user支付成功！` with no line break or colon."""
        for text in ("user支付成功！", "user✅ 支付成功！", "assistant: 好的",
                     "用户支付成功了", "助手已下单"):
            with self.subTest(text=text):
                d = P.validate_user_reply(text=text)
                self.assertIn(P.VIOLATION_ASSISTANT_TURN,
                              [v["code"] for v in d["violations"]])

    def test_a_user_quoting_the_agents_own_order_id_is_allowed(self):
        incoming = "订单号：OI4ddc1d9026\n状态：待支付\n请确认是否支付？"
        d = P.validate_user_reply(text="订单 OI4ddc1d9026 是吧，确认支付。",
                                  incoming_assistant_text=incoming,
                                  new_order_ids=["OI4ddc1d9026"])
        self.assertEqual(d["violations"], [])
        self.assertEqual(d["claims"], [])


# ---------------------------------------------------------------------------
# 2. The sealed overreach instances are refused (offline replay)
# ---------------------------------------------------------------------------

class SealedOverreachTests(unittest.TestCase):
    """The exact r5 turns that motivated the repair, replayed from the sealed record."""

    def setUp(self):
        self.result = r5_result()

    def test_every_sealed_overreach_turn_is_a_protocol_violation(self):
        cases = [(c["arm"], c["task"], c["transcript_index"])
                 for c in P.SEALED_R5_INSTANCES if c["expect"] == "violation"]
        self.assertGreaterEqual(len(cases), 7, "the sealed overreach set shrank")
        for arm, task, index in cases:
            with self.subTest(arm=arm, task=task, index=index):
                d = validate_turn(self.result, arm, task, index)
                self.assertEqual(d["outcome"], "SIMULATOR_PROTOCOL_VIOLATION", d)
                self.assertTrue(d["violations"], d)
                self.assertFalse(d["stop"], "a violating turn must never stop the phase")
                self.assertFalse(d["user_ended_normally"])

    def test_every_sealed_clean_user_stop_is_still_accepted(self):
        cases = [(c["arm"], c["task"], c["transcript_index"])
                 for c in P.SEALED_R5_INSTANCES if c["expect"] == "stop_accepted"]
        self.assertGreaterEqual(len(cases), 2)
        for arm, task, index in cases:
            with self.subTest(arm=arm, task=task, index=index):
                d = validate_turn(self.result, arm, task, index)
                self.assertEqual(d["outcome"], "USER_STOP_ACCEPTED", d)
                self.assertTrue(d["stop"])

    def test_the_r5_replay_matches_the_declared_expectations(self):
        rows = P.replay_sealed_user_replies(self.result)
        self.assertEqual(len(rows), len(P.SEALED_R5_INSTANCES))
        for row in rows:
            with self.subTest(instance=row["id"]):
                outcome = row["decision"]["outcome"]
                if row["expect"] == "violation":
                    self.assertEqual(outcome, "SIMULATOR_PROTOCOL_VIOLATION")
                else:
                    self.assertEqual(outcome, "USER_STOP_ACCEPTED")

    def test_r_t11_is_the_documented_case(self):
        """R t11: fabricated 支付成功 with STOP, real order unpaid, native reward 1."""
        d = validate_turn(self.result, "rewrite", 11, 11)
        self.assertEqual(d["outcome"], "SIMULATOR_PROTOCOL_VIOLATION")
        self.assertIn(P.VIOLATION_STOP_CONTINUATION, [v["code"] for v in d["violations"]])
        claim = [c for c in d["claims"] if c["code"] == P.CLAIM_PAYMENT_COMPLETED]
        self.assertEqual(len(claim), 1)
        # The phase's own database says the order was never paid.
        self.assertFalse(claim[0]["verified_against_database"])
        record = next(t for t in self.result["arms"]["rewrite"]["tasks"] if t["number"] == 11)
        self.assertEqual(record["judge"]["reward_info"]["reward"], 1.0)
        self.assertEqual(P.paid_orders_of(
            record["native_snapshot"]["environment_db"]["orders"],
            record["baseline_order_ids"]), {})
        # The invented order number is reported as such, not attributed to this phase.
        turn = user_turn(self.result, "rewrite", 11, 11)
        self.assertIn("TK20240613001", turn["text"])
        self.assertNotIn("TK20240613001", turn["incoming"])
        self.assertEqual(turn["new_order_ids"], ["OT3377474bc4"])
        self.assertEqual([v["code"] for v in d["violations"] if
                          v["code"] == P.VIOLATION_UNKNOWN_ORDER_ID],
                         [P.VIOLATION_UNKNOWN_ORDER_ID])

    def test_a_fabricated_order_id_is_reported_as_invented(self):
        d = P.validate_user_reply(
            text="user支付成功！订单号：TK20240613001",
            incoming_assistant_text="订单号：OT3377474bc4\n请确认是否支付？",
            new_order_ids=["OT3377474bc4"])
        codes = [v["code"] for v in d["violations"]]
        self.assertIn(P.VIOLATION_UNKNOWN_ORDER_ID, codes)
        evidence = next(v for v in d["violations"]
                        if v["code"] == P.VIOLATION_UNKNOWN_ORDER_ID)
        self.assertIn("TK20240613001", " ".join(evidence["evidence"]))

    def test_a_fabricated_order_id_is_reported_even_in_a_single_line_reply(self):
        """The invented id is caught on its own, without the role-label cascade."""
        d = P.validate_user_reply(
            text="支付成功了，订单号 TK20240613001",
            incoming_assistant_text="订单号：OT3377474bc4\n请确认是否支付？",
            new_order_ids=["OT3377474bc4"])
        self.assertEqual([v["code"] for v in d["violations"]],
                         [P.VIOLATION_UNKNOWN_ORDER_ID])


# ---------------------------------------------------------------------------
# 3. Stop-marker matching
# ---------------------------------------------------------------------------

class StopMarkerTests(unittest.TestCase):
    """`STOP in content` is not good enough; a marker is only a USER decision."""

    def test_the_r5_shape_is_not_a_stop(self):
        """The marker is followed by an assistant continuation written by the simulator."""
        text = ("确认支付。\n\nuser✅ 支付成功！\n- 订单号：OT3377474bc4\n\n"
                "assistant没有了，谢谢。\n###STOP###\nassistant祝你用餐愉快！")
        d = P.validate_user_reply(text=text, incoming_assistant_text="请确认是否支付？",
                                  new_order_ids=["OT3377474bc4"])
        self.assertTrue(d["original_stop_marker"])
        self.assertFalse(d["stop"])
        codes = [v["code"] for v in d["violations"]]
        self.assertIn(P.VIOLATION_STOP_CONTINUATION, codes)
        self.assertIn(P.VIOLATION_ASSISTANT_TURN, codes)

    def test_a_role_labelled_sentence_sharing_the_markers_line_is_not_a_stop(self):
        d = P.validate_user_reply(
            text="assistant没有了，谢谢。 ###STOP###",
            incoming_assistant_text="请问还有其他需要帮忙的吗？")
        self.assertTrue(d["original_stop_marker"])
        self.assertFalse(d["stop"])
        codes = [v["code"] for v in d["violations"]]
        self.assertIn(P.VIOLATION_STOP_CONTINUATION, codes)
        self.assertIn(P.VIOLATION_ASSISTANT_TURN, codes)

    def test_an_unlabelled_assistant_sentence_sharing_the_markers_line_is_not_a_stop(self):
        d = P.validate_user_reply(
            text="祝您明天健身愉快！ ###STOP###",
            incoming_assistant_text="请问还有其他需要帮忙的吗？")
        self.assertTrue(d["original_stop_marker"])
        self.assertFalse(d["stop"])
        self.assertIn(P.VIOLATION_STOP_CONTINUATION,
                      [v["code"] for v in d["violations"]])

    def test_a_bare_marker_is_a_stop_and_a_marker_with_a_continuation_is_not(self):
        bare = P.validate_user_reply(text="没有了，谢谢。\n###STOP###")
        self.assertTrue(bare["stop"])
        carried = P.validate_user_reply(
            text="没有了，谢谢。\n###STOP###\nuser对了，再帮我买一束花")
        self.assertFalse(carried["stop"])

    def test_a_violating_turn_stops_the_phase_rather_than_completing_it(self):
        """The driver acts on `stop`; a violation must never produce a normal user_stop."""
        result = r5_result()
        record = next(t for t in result["arms"]["rewrite"]["tasks"] if t["number"] == 11)
        self.assertEqual(record["termination_reason"], "user_stop")
        d = validate_turn(result, "rewrite", 11, 11)
        self.assertFalse(d["stop"])
        self.assertEqual(d["outcome"], "SIMULATOR_PROTOCOL_VIOLATION")


# ---------------------------------------------------------------------------
# 4. Business completion from the database, never from text
# ---------------------------------------------------------------------------

class BusinessCompletionTests(unittest.TestCase):
    def setUp(self):
        self.result = r5_result()
        self.scope = r5_scoring_scope()
        self.by_number = {t["number"]: t for t in self.scope["tasks"]}

    def report(self):
        return P.evaluate_sealed_r5_run(result=self.result, scope=self.scope)

    def phase(self, report, arm, number):
        return next(p for p in report["phases"]
                    if p["arm"] == arm and p["task_number"] == number)

    def test_text_saying_paid_does_not_pass_when_the_database_is_unpaid(self):
        """R t11: the text claims 支付成功; the order is unpaid and no payment was required."""
        report = self.report()
        phase = self.phase(report, "rewrite", 11)
        business = phase["business_completion"]
        self.assertEqual(business["statuses"], {"OT3377474bc4": "unpaid"})
        self.assertEqual(business["paid_order_ids"], [])
        self.assertFalse(business["payment_requirement"]["required"],
                         "t11's own standard carries no payment item; payment must not "
                         "be imposed on it")
        self.assertNotIn("payment_completed", business["requirements"])
        self.assertEqual(phase["native_rubric_score"]["reward"], 1.0,
                         "the native reward keeps its original meaning")

    def test_a_real_create_then_pay_passes_the_business_check(self):
        """R t4: real create and real pay, so the deterministic check passes."""
        report = self.report()
        business = self.phase(report, "rewrite", 4)["business_completion"]
        self.assertTrue(business["complete"], business)
        self.assertEqual(business["statuses"], {"OTf2297e7b31": "paid"})
        self.assertEqual(business["paid_order_ids"], ["OTf2297e7b31"])
        self.assertEqual(business["unmet_requirements"], [])

    def test_payment_is_only_required_when_the_task_itself_requires_it(self):
        report = self.report()
        for phase in report["phases"]:
            business = phase["business_completion"]
            with self.subTest(arm=phase["arm"], task=phase["task_number"]):
                self.assertFalse(business["payment_requirement"]["required"], business)
                self.assertNotIn("payment_completed", business["requirements"])
                self.assertEqual(business["payment_requirement"]["policy"],
                                 P.PAYMENT_REQUIREMENT_POLICY)

    def test_a_task_that_really_requires_payment_gets_the_check(self):
        """The predicate is evidence driven, not this round's task list."""
        task = {"instruction": "帮我下单并且完成支付"}
        with_payment = P.task_requires_payment(task, ["下单并支付成功"])
        self.assertTrue(with_payment["required"])
        without = P.task_requires_payment({"instruction": "帮我买个券"}, ["下单的商品应该是券"])
        self.assertFalse(without["required"])
        excluded = P.task_requires_payment({"instruction": "先下单，不用支付"},
                                           ["下单的商品应该是券"])
        self.assertFalse(excluded["required"])
        self.assertEqual(excluded["matched"], "explicit_not_required")

    def test_a_historical_or_earlier_phase_order_cannot_stand_in(self):
        """Only orders absent from THIS phase's own baseline count."""
        record = next(t for t in self.result["arms"]["rewrite"]["tasks"]
                      if t["number"] == 4)
        orders = record["native_snapshot"]["environment_db"]["orders"]
        self.assertEqual(list(orders), ["OTf2297e7b31"])
        # Treating that order as pre-existing leaves this phase with no new order.
        business = P.deterministic_business_result(
            arm="rewrite", number=4, task=self.by_number[4],
            scope_task_record=self.by_number[4], orders=orders,
            baseline_order_ids=["OTf2297e7b31"])
        self.assertEqual(business["new_order_ids"], [])
        self.assertFalse(business["complete"])
        self.assertIn("order_created_in_this_phase", business["unmet_requirements"])

    def test_the_deterministic_rule_is_the_same_for_both_arms(self):
        """t5: the same purchase fact must give the same deterministic answer."""
        report = self.report()
        rewrite = self.phase(report, "rewrite", 5)["business_completion"]
        erratum = self.phase(report, "erratum", 5)["business_completion"]
        # The deterministic rule is identical; the two phases differ only in facts that
        # the rule reports the SAME way (E also cancelled three wrong orders, which its
        # own facts show and the rule flags identically).
        self.assertEqual(set(rewrite["requirements"]), set(erratum["requirements"]))
        for business in (rewrite, erratum):
            self.assertFalse(business["complete"])
            self.assertIn("items_match_target_product", business["unmet_requirements"])
            self.assertEqual(business["payment_requirement"], rewrite["payment_requirement"])
            self.assertEqual(
                business["requirements"]["items_match_target_product"]["rule"],
                ["S17791041709546489_P00067"])
            self.assertEqual(
                rewrite["requirements"]["items_match_target_product"]["rule"],
                erratum["requirements"]["items_match_target_product"]["rule"])
        # E created wrong orders and cancelled them; R never cancelled. The rule reports
        # both facts and neither arm is excused by the other's history.
        self.assertIn("order_not_cancelled", erratum["unmet_requirements"])
        self.assertNotIn("order_not_cancelled", rewrite["unmet_requirements"])
        self.assertEqual(rewrite["statuses"], {"OI4ddc1d9026": "paid"})
        self.assertEqual(sorted(set(erratum["statuses"].values())),
                         ["cancelled", "unpaid"])
        # t5 is an INSTORE phase: the delivery-address rule does not apply, and it is
        # reported as not-applicable rather than counted as met.
        for business in (rewrite, erratum):
            address = business["requirements"]["delivery_at_work_address"]
            self.assertFalse(address["covered"])
            self.assertNotIn("delivery_at_work_address", business["unmet_requirements"])
        # Both arms bought the SAME product, and the deterministic rule names it.
        names = {item["name"] for row in rewrite["purchase_facts"] for item in row["items"]}
        self.assertEqual(names, {"全身油压按摩放松券（100分钟）"})
        self.assertEqual(
            {item["name"] for row in erratum["purchase_facts"] for item in row["items"]},
            {"全身油压按摩放松券（100分钟）", "背部精油spa按摩券（70分钟）"})


# ---------------------------------------------------------------------------
# 5. Conflicts and unknowns are not capability failures
# ---------------------------------------------------------------------------

class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.result = r5_result()
        self.scope = r5_scoring_scope()

    def report(self):
        return P.evaluate_sealed_r5_run(result=self.result, scope=self.scope)

    def test_the_t8_distance_conflict_is_reproduced_from_the_coordinates(self):
        """715m by the pinned formula, while the task's own standard says ~2.1km."""
        task = next(t for t in self.scope["tasks"] if t["number"] == 8)
        home = P.home_location(task)
        self.assertEqual((home["longitude"], home["latitude"]), (103.7195, 36.1005))
        shop = None
        for row in (task["environment"]["shops"] or {}).values():
            if row.get("shop_name") == "型动健身空间(银滩路店)":
                shop = row
                break
        self.assertIsNotNone(shop, "the annotated merchant must be in t8's own database")
        distance = P.haversine_meters(home["longitude"], home["latitude"],
                                      shop["location"]["longitude"],
                                      shop["location"]["latitude"])
        self.assertEqual(distance, 715.0)
        # The recorded tool result for the same pair of points.
        record = next(t for t in self.result["arms"]["erratum"]["tasks"]
                      if t["number"] == 8)
        self.assertIn("715.0", json.dumps(record["transcript"], ensure_ascii=False))
        # The standard's own annotation, verbatim, contradicts it.
        standard = [x for x in task["rubrics"] if "型动健身空间" in x]
        self.assertEqual(len(standard), 1)
        self.assertIn("型动健身空间(银滩路店)（约2.1km）", standard[0])

    def test_the_distance_conflict_is_systematic_across_the_annotated_merchants(self):
        """No single home point reproduces the annotations: they are not straight-line."""
        task = next(t for t in self.scope["tasks"] if t["number"] == 8)
        home = P.home_location(task)
        annotated = {"动力无限健身俱乐部(安宁万达店)": 2300, "悦动健身工作室(师大店)": 2500,
                     "金刚健身俱乐部(培黎广场店)": 2200, "力源健身会所(交大店)": 2800,
                     "型动健身空间(银滩路店)": 2100, "火力全开健身(安宁区政府店)": 3100,
                     "卡路里健身中心(十里店店)": 3200}
        rows = {row.get("shop_name"): row
                for row in (task["environment"]["shops"] or {}).values()}
        ratios = []
        for name, claimed in annotated.items():
            self.assertIn(name, rows)
            row = rows[name]
            measured = P.haversine_meters(home["longitude"], home["latitude"],
                                          row["location"]["longitude"],
                                          row["location"]["latitude"])
            self.assertLess(measured, claimed, f"{name}: the annotation is farther")
            ratios.append(measured / claimed)
        self.assertGreater(min(ratios), 0.2)
        self.assertLess(max(ratios), 0.7)

    def test_the_conflict_is_recorded_and_the_task_is_excluded_from_clean_comparison(self):
        report = self.report()
        for arm in ("rewrite", "erratum"):
            phase = next(p for p in report["phases"]
                         if p["arm"] == arm and p["task_number"] == 8)
            self.assertTrue(phase["evaluation_conflict"]["has_conflict"])
            kinds = [c["kind"] for c in phase["evaluation_conflict"]["conflicts"]]
            self.assertIn(P.CONFLICT_TOOL_FACT_VS_STANDARD, kinds)
            self.assertFalse(phase["comparison_eligible"])
            self.assertFalse(phase["evaluation_conflict"]["is_capability_failure"])
        self.assertIn("t8", report["clean_comparison_exclusions"])

    def test_the_t5_standard_conflict_is_recorded_without_taking_a_side(self):
        report = self.report()
        for arm in ("rewrite", "erratum"):
            phase = next(p for p in report["phases"]
                         if p["arm"] == arm and p["task_number"] == 5)
            conflict = phase["evaluation_conflict"]
            self.assertTrue(conflict["has_conflict"])
            declared = conflict["conflicts"][0]
            self.assertEqual(declared["kind"], P.CONFLICT_RUBRIC_TEXT_DIVERGES)
            self.assertEqual(declared["status"], P.CONFLICT_INDETERMINATE)
            self.assertTrue(declared["unresolved"], "the open questions must be stated")
            self.assertFalse(conflict["is_capability_failure"])
            self.assertFalse(phase["comparison_eligible"])
        self.assertIn("t5", report["clean_comparison_exclusions"])

    def test_a_conflict_is_never_an_automatic_success(self):
        report = self.report()
        for phase in report["phases"]:
            if not phase["evaluation_conflict"]["has_conflict"]:
                continue
            with self.subTest(arm=phase["arm"], task=phase["task_number"]):
                self.assertFalse(phase["business_completion"]["complete"])
                self.assertFalse(phase["evaluation_conflict"]["is_capability_failure"])

    def test_the_judge_is_aligned_to_the_standard_by_stable_rubric_id(self):
        """Every scored item is identified by position against the dataset standard.

        The measured r5 record shows the judge echoed the standard it was given for all
        18 phases, so nothing is undecidable on TEXT grounds here; the alignment still
        comes from the dataset standard, never from the echoed text.
        """
        report = self.report()
        checked = 0
        for phase in report["phases"]:
            for item in phase["native_rubric_score"]["items"]:
                self.assertIsNotNone(item["dataset_rubric"])
                self.assertTrue(item["rubric_id"].startswith("rubric_"))
                self.assertIn(item["rubric_id_source"],
                              ("dataset_key", "position_fallback"))
                if item["judge_rubric_matches_dataset"]:
                    checked += 1
        self.assertEqual(checked, 62, "31 items across 18 phases were expected to match")
        self.assertEqual(report["clean_comparison_exclusions"], ["t5", "t8"],
                         "only the two declared standard conflicts exclude a task")

    def test_a_divergent_echo_is_recorded_as_undecidable(self):
        """If a judge echoes something else, the item is unknown, not quietly passed."""
        native = {"items": [{"rubric_id": "rubric_0", "met": True,
                             "dataset_rubric": "标准原文",
                             "judge_rubric_text": "标准原文（评委扩写）",
                             "judge_rubric_matches_dataset": False}],
                  "rubrics_met": 1, "rubrics_total": 1, "reward": 1.0}
        business = {"complete": True, "new_order_ids": ["OI1"], "purchase_facts": []}
        conflict = P.evaluation_conflict_result(
            arm="rewrite", number=6, business=business, native=native, task={},
            dataset_rubric_texts=["标准原文"], public_rubric_texts=["标准原文"])
        self.assertEqual(len(conflict["undecidable_items"]), 1)
        self.assertFalse(conflict["is_capability_failure"])

    def test_a_never_evaluated_phase_is_unknown_not_a_failure(self):
        """A phase whose simulator decision was not recorded is marked as such."""
        report = self.report()
        for phase in report["phases"]:
            with self.subTest(arm=phase["arm"], task=phase["task_number"]):
                self.assertEqual(phase["user_simulator"]["outcome"], "NOT_RECORDED")
                self.assertFalse(phase["user_simulator"]["recorded"])


# ---------------------------------------------------------------------------
# 6. The three results really are separated, and the old report is untouched
# ---------------------------------------------------------------------------

class SeparatedReportTests(unittest.TestCase):
    def setUp(self):
        self.result = r5_result()
        self.scope = r5_scoring_scope()
        self.report = P.evaluate_sealed_r5_run(result=self.result, scope=self.scope)

    def test_the_report_separates_the_three_results(self):
        self.assertEqual(self.report["separates"],
                         ["native_rubric_score", "business_completion",
                          "evaluation_conflict"])
        for phase in self.report["phases"]:
            with self.subTest(arm=phase["arm"], task=phase["task_number"]):
                self.assertIn("native_rubric_score", phase)
                self.assertIn("business_completion", phase)
                self.assertIn("evaluation_conflict", phase)
                self.assertIn("user_simulator", phase)

    def test_the_native_reward_is_reproduced_exactly(self):
        """The split report must not move a single native score."""
        recorded = {}
        for arm, arm_record in self.result["arms"].items():
            for task in arm_record["tasks"]:
                recorded[(arm, task["number"])] = task["judge"]["reward_info"]["reward"]
        for phase in self.report["phases"]:
            key = (phase["arm"], phase["task_number"])
            self.assertEqual(phase["native_rubric_score"]["reward"], recorded[key])
        totals = {arm: sum(v["native_reward_sum"] for a, v in self.report["totals"].items()
                           if a == arm) for arm in ("rewrite", "erratum")}
        self.assertEqual(totals, {"rewrite": 4.0, "erratum": 4.0})

    def test_rubric_items_are_aligned_by_the_dataset_key_not_by_echoed_text(self):
        """Each scored item carries the DATASET's own rubric key and standard text."""
        for phase in self.report["phases"]:
            for item in phase["native_rubric_score"]["items"]:
                self.assertIsNotNone(item["dataset_rubric"],
                                     "the dataset standard must be the standard source")
                self.assertIsNotNone(item["rubric_id"])
                self.assertTrue(item["rubric_id"].startswith("rubric_"))
                # The scored record's final decisions carry no `rubric_idx`, so the
                # identity is the dataset key at the SAME position, and that is recorded
                # as a position fallback rather than passed off as a verified key bind.
                self.assertIn(item["rubric_id_source"],
                              ("dataset_key", "position_fallback"))
                self.assertNotEqual(item["rubric_id_source"], "unmatched")
                self.assertEqual(item["standard_source"], "dataset_evaluation_criteria")
                if item["rubric_id_declared"] is not None:
                    self.assertTrue(item["rubric_id_matches_declared"])

    def test_the_old_default_path_is_unchanged(self):
        """No protocol declared means no new fields and no behaviour change."""
        payload = json.loads(G4_CONFIG.read_text(encoding="utf-8"))
        config = M.validate_config(payload, exploratory_capacity_option=EXPLORATORY)
        self.assertIsNone(M.run_protocols_of(config))
        # The driver's returned copy carries the private opt-in hand-over marker; the
        # declaration itself is byte-identical to the sealed 0.4 candidate.
        declared = {k: v for k, v in config.items() if k != M.EXPLORATORY_MARKER}
        self.assertEqual(declared, payload)
        self.assertNotIn("simulator_protocol", config)

    def test_a_sealed_config_cannot_smuggle_a_protocol_field(self):
        payload = json.loads(G4_CONFIG.read_text(encoding="utf-8"))
        payload = dict(payload, simulator_protocol="ae-user-simulator-role-boundary-1")
        with self.assertRaises(ValueError):
            M.validate_config(payload, exploratory_capacity_option=EXPLORATORY)

    def test_the_0_5_config_carries_both_protocols_and_nothing_else_new(self):
        payload = json.loads(G5_CONFIG.read_text(encoding="utf-8"))
        config = M.validate_config(payload, exploratory_capacity_option=EXPLORATORY)
        bundle = M.run_protocols_of(config)
        self.assertEqual(bundle, {"simulator_protocol": P.USER_SIMULATOR_PROTOCOL,
                                  "evaluation_protocol": P.EVALUATION_PROTOCOL})
        g4 = json.loads(G4_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(set(payload) ^ set(g4), {"simulator_protocol",
                                                  "evaluation_protocol"})
        self.assertEqual({k: v for k, v in payload.items()
                          if k not in ("schema_version", "simulator_protocol",
                                       "evaluation_protocol")},
                         {k: v for k, v in g4.items() if k != "schema_version"})

    def test_an_unknown_protocol_version_is_refused(self):
        payload = json.loads(G5_CONFIG.read_text(encoding="utf-8"))
        for field in ("simulator_protocol", "evaluation_protocol"):
            with self.subTest(field=field):
                bad = dict(payload, **{field: "not-a-reviewed-version"})
                with self.assertRaises(P.ProtocolDeclarationError):
                    M.validate_config(bad, exploratory_capacity_option=EXPLORATORY)

    def test_a_0_5_config_missing_a_protocol_field_is_refused(self):
        payload = json.loads(G5_CONFIG.read_text(encoding="utf-8"))
        for field in ("simulator_protocol", "evaluation_protocol"):
            with self.subTest(field=field):
                bad = {k: v for k, v in payload.items() if k != field}
                with self.assertRaises(ValueError):
                    M.validate_config(bad, exploratory_capacity_option=EXPLORATORY)


# ---------------------------------------------------------------------------
# 7. The same rules reach BOTH arms, and the next round's entry point is wired
# ---------------------------------------------------------------------------

class WiringTests(unittest.TestCase):
    def test_the_runtime_owns_the_protocol_and_both_arms_get_the_same_bundle(self):
        payload = json.loads(G5_CONFIG.read_text(encoding="utf-8"))
        config = M.validate_config(payload, exploratory_capacity_option=EXPLORATORY)
        bundle = M.run_protocols_of(config)
        self.assertIsNotNone(bundle)
        # The bundle is a single object with no per-arm form.
        self.assertNotIn("arm", bundle)
        self.assertEqual(bundle, P.validate_protocol_bundle(bundle))
        self.assertEqual(P.protocol_bundle_sha256(bundle),
                         P.protocol_bundle_sha256(dict(bundle)))

    def test_the_driver_hands_the_bundle_to_every_phase_and_records_it(self):
        source = (ROOT / "ae_cloud_re_multiturn.py").read_text(encoding="utf-8")
        self.assertIn("protocols=protocols", source,
                      "the phase must be started with the declared bundle")
        self.assertIn('"simulator_decisions": simulator_decisions', source,
                      "each phase must record its own simulator decisions")
        self.assertIn("result[\"evaluation\"] = separated_evaluation(result, sample, protocols)",
                      source, "the separated report must be attached by the driver")

    def test_the_runtime_builds_the_simulator_with_the_constraints_and_enforces_them(self):
        source = (ROOT / "ae_vita.py").read_text(encoding="utf-8")
        self.assertIn("user_simulator_system_suffix", source)
        self.assertIn("_check_simulator_turn", source)
        self.assertIn("SimulatorProtocolViolation(decision)", source)
        # The native marker test is still applied only when no protocol is declared.
        self.assertIn('if self._protocols is None:', source)

    def test_the_start_and_finish_contract_accepts_the_protocol_arguments(self):
        import inspect
        import ae_vita
        start = inspect.signature(ae_vita.NativeVita.start)
        finish = inspect.signature(ae_vita.NativeVita.finish)
        self.assertIn("protocols", start.parameters)
        self.assertIn("arm", finish.parameters)
        self.assertIn("target_product_ids", finish.parameters)

    def test_the_private_scoring_scope_is_not_an_agent_input(self):
        scope = r5_scoring_scope()
        self.assertEqual(scope["visibility"], "private_driver_and_judge_only")
        for task in scope["tasks"]:
            self.assertIn("rubrics", task)
            self.assertIn("target_product_ids", task)
        # The public task projection the Agent receives carries none of it.
        from ae_inputs import prepare_sample
        sample = prepare_sample(DATASET, start_turn=4, end_turn=12)
        public = M.check_scope(sample)
        for task in public["tasks"]:
            self.assertEqual(set(task), {"number", "subtask_id", "domain",
                                         "current_time", "instruction", "history"})
        self.assertEqual(public["target_products_visibility"], "private_scoring_only")

    def test_the_protocol_module_is_declared_as_run_code(self):
        self.assertIn("ae_sim_eval_protocol.py", M.MULTITURN_CODE_FILES)
        digests = M.multiturn_code_files(ROOT)
        self.assertIn("ae_sim_eval_protocol.py", digests)
        self.assertEqual(
            digests["ae_sim_eval_protocol.py"],
            hashlib.sha256((ROOT / "ae_sim_eval_protocol.py").read_bytes()).hexdigest())


# ---------------------------------------------------------------------------
# 8. The violation really travels the phase boundary of the production driver
# ---------------------------------------------------------------------------

class _NoToolsEnvironment:
    """The minimum native environment `environment_bindings` can bind."""

    class _Db:
        orders = {}

        def get_now(self, fmt="%Y-%m-%d %H:%M:%S"):
            # The 0.4/0.5 interface clarification reads the stage's OWN environment
            # clock; this fixture provides one so the phase really reaches the
            # simulator seam instead of failing earlier on the clock gate.
            return "2024-06-23 14:30:00"

    def __init__(self):
        self.tools = _NoToolsEnvironment._Db()

    def get_tools(self):
        return []

    def make_tool_call(self, tool_name, requestor, **kwargs):  # pragma: no cover
        raise AssertionError("no tool is expected in this fixture")

    def to_json_str(self, value):  # pragma: no cover
        return json.dumps(value)


class _FakeBridge:
    """The bridge surface `_run_one_phase` actually touches, and nothing more."""

    def __init__(self):
        self.transcript = []
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
                                        "content": "请确认是否支付？"}]}

    def reply_to_user(self, text):
        return [{"role": "assistant", "content": "收到。"}]


class _ViolatingNative:
    """A runtime whose user turn overreaches exactly like the sealed r5 instances."""

    def __init__(self, reply):
        self.reply = reply
        self.aborted = False

    def start(self, task, protocols=None):
        self.protocols = protocols
        return {"environment": _NoToolsEnvironment(), "domain_policy": "fixture",
                "instruction": task["instruction"],
                "greeting": {"role": "assistant", "content": "fixture greeting"}}

    def agent_stop(self, text):
        return "###STOP###" in text

    def user_reply(self, text):
        raise P.SimulatorProtocolViolation(self.reply)

    def finish(self, task, transcript, termination_reason, duration, *, arm=None,
               target_product_ids=None):  # pragma: no cover
        raise AssertionError("a refused phase must never reach the judge")

    def snapshot(self):
        return {"fixture": True}

    def abort(self):
        self.aborted = True


class PhaseBoundaryTests(unittest.TestCase):
    """`ae_cloud_re_multiturn._run_one_phase` must stop the pair on a violation."""

    def _config(self):
        payload = json.loads(G5_CONFIG.read_text(encoding="utf-8"))
        return M.validate_config(payload, exploratory_capacity_option=EXPLORATORY)

    def test_a_violation_stops_the_phase_instead_of_becoming_a_normal_completion(self):
        decision = P.validate_user_reply(
            text=("确认支付。\n\nuser✅ 支付成功！\n- 订单号：OI1\n\n"
                  "assistant没有了，谢谢。 ###STOP###"),
            new_order_ids=["OI1"], paid_order_ids=[])
        config = self._config()
        task = {"number": 4, "subtask_id": "sub_U000828_4",
                "instruction": "把店里地址告诉我", "domain": "delivery",
                "current_time": "2024-06-23", "history": []}
        events = []
        with self.assertRaises(P.SimulatorProtocolViolation):
            M._run_one_phase(config, {"target_products": {}}, "rewrite", task,
                             _FakeBridge(), _ViolatingNative(decision),
                             emit=events.append, phase_index=0,
                             runtimes_baseline={"writes": {"rewrite": 0},
                                                "native_calls": {"rewrite": 0}},
                             protocols=M.run_protocols_of(config))
        # No phase completion was recorded and no judge score was invented.
        self.assertNotIn("arm_task_complete", [e.get("kind") for e in events])

    def test_the_driver_does_not_swallow_the_refusal(self):
        source = (ROOT / "ae_cloud_re_multiturn.py").read_text(encoding="utf-8")
        # The generic per-phase handler wraps judge failures; the simulator refusal is
        # raised from `user_reply` BEFORE that block, so it travels out of the pair.
        self.assertIn("user_reply = native.user_reply(text)", source)
        self.assertLess(source.index("user_reply = native.user_reply(text)"),
                        source.index("judged = native.finish("))


if __name__ == "__main__":
    unittest.main()
