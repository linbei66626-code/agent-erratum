"""Small, standard-library tests for the evidence and input isolation boundary."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ae_inputs import (
    DATASET_SHA256,
    InputValidationError,
    TASK_FIELDS,
    USER_ID,
    _locate_user,
    history_message,
    prepare_sample,
    project_user,
)


def fixture_user():
    tasks = []
    for number in range(1, 13):
        month = f"{number:02d}"
        record = {
            "date": f"2024-{month}-10",
            "behavior": [{"behavior_type": "order", "content": {"items": [{"product_name": "历史商品"}]}}],
            "dialogue": [{"role": "assistant", "content": "历史助手说已下单；不得当成本次执行。"}],
        }
        tasks.append({
            "task_turn_num": f"{USER_ID}_{number:02d}",
            "subtask_id": f"sub_{USER_ID}_{number}",
            "domain": "delivery",
            "current_time": f"2024-{month}-20",
            "start_date": f"2024-{month}-01",
            "end_date": f"2024-{month}-19",
            "instruction": f"当前任务 {number}",
            "interactions": [record],
            "user_scenario": {
                "user_profile": {"user_id": USER_ID, "阶段": "t3" if number == 3 else "FUTURE_PROFILE"},
                "personalized_preference_memory": {
                    "current": {"饮食偏好": ["奶茶偏好5分糖", "不吃蒜苗"] if number == 3 else ["FUTURE_CURRENT"], "其他偏好": ["初始内容"]},
                    "preference_tag_change_history": ["PRIVATE_CHANGE_HISTORY"],
                },
            },
            "historical_chat": ["DUPLICATED_SUMMARY"],
            "environment": {"secret": {"value": "PRIVATE_ENV"}},
            "evaluation_criteria": {"expected_states": [{"state_rubrics": ["PRIVATE_RUBRIC"]}], "overall_rubrics": []},
            "target_product_ids": ["PRIVATE_TARGET"],
        })
    return {"id": USER_ID, "user_id": USER_ID, "subtasks": tasks}


def load_preflight_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "ae_01_preflight.py"
    spec = importlib.util.spec_from_file_location("ae_01_preflight_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InputProjectionTests(unittest.TestCase):
    def test_consecutive_full_and_wiring_subset_keep_t3_initialization(self):
        user = fixture_user()
        for end in (5, 12):
            sample = project_user(user, end_turn=end)
            self.assertEqual([t["number"] for t in sample["tasks"]], list(range(4, end + 1)))
            self.assertEqual(sample["initial_facts"]["p000"], {"category": "饮食偏好", "content": "奶茶偏好5分糖"})
            self.assertEqual(list(sample["initial_facts"]), ["p000", "p001", "p002"])
            self.assertEqual(sample["initial_facts"]["p002"]["category"], "其他偏好")
            self.assertEqual(sample["initial_profile"]["阶段"], "t3")

    def test_gold_and_summary_fields_never_enter_projection(self):
        sample = project_user(fixture_user())
        public = {key: sample[key] for key in ("initial_facts", "initial_profile", "tasks")}
        text = json.dumps(public)
        for sentinel in ("FUTURE_PROFILE", "FUTURE_CURRENT", "PRIVATE_CHANGE_HISTORY", "DUPLICATED_SUMMARY", "PRIVATE_ENV", "PRIVATE_RUBRIC", "PRIVATE_TARGET"):
            self.assertNotIn(sentinel, text)
        self.assertTrue(all(set(task) == TASK_FIELDS for task in sample["tasks"]))
        self.assertEqual(set(sample["private_tasks"]), {f"sub_{USER_ID}_{n}" for n in range(4, 13)})

    def test_records_and_private_material_are_independent_copies(self):
        user = fixture_user()
        before = copy.deepcopy(user)
        sample = project_user(user)
        self.assertEqual(sample["tasks"][0]["history"][0]["record"], user["subtasks"][3]["interactions"][0])
        sample["tasks"][0]["history"][0]["record"]["behavior"][0]["content"]["items"][0]["product_name"] = "changed"
        sample["private_tasks"][f"sub_{USER_ID}_4"]["environment"]["secret"]["value"] = "changed"
        sample["initial_profile"]["阶段"] = "changed"
        self.assertEqual(user, before)

    def test_history_wrapper_has_references_and_separate_current_task(self):
        task = project_user(fixture_user())["tasks"][0]
        message = history_message(task)
        payload = json.loads(message["content"])
        self.assertEqual(message["role"], "user")
        self.assertEqual(payload["source"], "dataset_history/material")
        self.assertEqual(payload["records"][0]["ref"], "t4/history/0")
        self.assertEqual(payload["records"], task["history"])
        self.assertIn("不要重新执行历史请求", payload["handling"])
        self.assertNotIn(task["instruction"], message["content"])
        self.assertNotIn("instruction", payload)

    def test_raw_task_and_extra_history_fields_are_rejected(self):
        user = fixture_user()
        with self.assertRaises(InputValidationError):
            history_message(user["subtasks"][3])
        user["subtasks"][3]["interactions"][0]["current"] = "GOLD"
        with self.assertRaises(InputValidationError):
            project_user(user)

    def test_task_number_and_history_reference_cannot_be_reordered(self):
        user = fixture_user()
        user["subtasks"][4], user["subtasks"][5] = user["subtasks"][5], user["subtasks"][4]
        with self.assertRaises(InputValidationError):
            project_user(user)
        task = project_user(fixture_user())["tasks"][0]
        task["history"][0]["ref"] = "t12/history/0"
        with self.assertRaises(InputValidationError):
            history_message(task)

    def test_no_later_start_or_unapproved_end(self):
        for start, end in ((12, 12), (5, 12), (4, 4), (4, 6), (True, 12)):
            with self.subTest(start=start, end=end), self.assertRaises(InputValidationError):
                prepare_sample("/this/file/is/intentionally/not/read", start_turn=start, end_turn=end)

    def test_future_and_unsorted_history_rejected(self):
        user = fixture_user()
        user["subtasks"][3]["interactions"][0]["date"] = "2024-04-21"
        with self.assertRaises(InputValidationError):
            project_user(user)
        user = fixture_user()
        earlier = copy.deepcopy(user["subtasks"][3]["interactions"][0])
        earlier["date"] = "2024-04-09"
        user["subtasks"][3]["interactions"].append(earlier)
        with self.assertRaises(InputValidationError):
            project_user(user)

    def test_t4_may_share_initial_cutoff_but_next_batch_must_be_later(self):
        user = fixture_user()
        user["subtasks"][3]["start_date"] = "2024-03-20"
        user["subtasks"][3]["interactions"][0]["date"] = "2024-03-20"
        project_user(user)
        user["subtasks"][4]["start_date"] = "2024-04-20"
        user["subtasks"][4]["interactions"][0]["date"] = "2024-04-20"
        with self.assertRaises(InputValidationError):
            project_user(user)

    def test_fixed_hash_and_original_user_index_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wrong.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(InputValidationError, "SHA-256 mismatch"):
                prepare_sample(path)
        user = fixture_user()
        with self.assertRaises(InputValidationError):
            _locate_user([user])
        users = [{} for _ in range(25)] + [user]
        self.assertIs(_locate_user(users), user)
        with self.assertRaises(InputValidationError):
            _locate_user(users + [copy.deepcopy(user)])


class OfflinePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preflight = load_preflight_module()

    def sample(self):
        sample = project_user(fixture_user(), end_turn=5)
        sample["source"] = {"sha256": DATASET_SHA256, "initial_state_turn": 3, "start_turn": 4, "end_turn": 5}
        return sample

    def test_default_report_is_static_has_no_private_content_or_token_guess(self):
        with patch.dict("sys.modules", {"transformers": None}):
            report = self.preflight.build_report(self.sample())
        self.assertFalse(report["model_called"])
        self.assertFalse(report["validity"]["runtime_validated"])
        self.assertFalse(report["validity"]["context_window_fit_validated"])
        self.assertIsNone(report["totals"]["history_records_json_token"])
        self.assertEqual(report["tokenizer"]["reason"], "no_explicit_local_tokenizer")
        self.assertEqual(report["private_metadata"][f"sub_{USER_ID}_4"]["state_rubric_counts"], [1])
        for secret in ("PRIVATE_ENV", "PRIVATE_TARGET", "PRIVATE_RUBRIC", "FUTURE_CURRENT", "PRIVATE_CHANGE_HISTORY"):
            self.assertNotIn(secret, json.dumps(report))
        self.assertIn("history_stage", report["formatted_agent_messages"][0])
        self.assertIn("task_instruction_preview", report["formatted_agent_messages"][0])
        self.assertFalse(report["actual_model_request_captured"])

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            self.preflight.write_report_exclusive(path, {"original": True})
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                self.preflight.write_report_exclusive(path, {"original": False})
            self.assertEqual(path.read_bytes(), before)

    def test_nonlocal_tokenizer_never_attempts_import(self):
        with patch.dict("sys.modules", {"transformers": None}):
            counter, metadata = self.preflight.local_token_counter("organization/model")
        self.assertIsNone(counter)
        self.assertEqual(metadata["status"], "not_measured")

    def test_missing_optional_library_is_reported_without_install(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("sys.modules", {"transformers": None}):
            counter, metadata = self.preflight.local_token_counter(temporary)
        self.assertIsNone(counter)
        self.assertEqual(metadata["status"], "not_measured")
        self.assertIn("local_tokenizer_unavailable", metadata["reason"])


if __name__ == "__main__":
    unittest.main()
