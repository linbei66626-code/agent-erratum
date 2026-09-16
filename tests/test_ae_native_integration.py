"""Real fixed Vita components with fixture model replies; never model evidence.

Run with .venv-vita/bin/python -B -m unittest tests.test_ae_native_integration -v.
Other interpreters or absent local source/data skip this optional integration
test. Existing but changed source/data fail NativeVita's real identity checks.
Only the OpenAI client's completion response is replaced: native generate,
user prompts, evaluator prompts, parsing, and scoring all still execute.
"""
from contextlib import ExitStack
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ae_inputs import prepare_sample
from ae_vita import NativeVita


PROJECT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("AE_VITA_SOURCE", "/tmp/ae01-vita-8WHDvk/source"))
DATASET = Path(os.environ.get("AE_VITA_DATASET", "/tmp/ae01-vita-8WHDvk/tasks-full-a4553e1.json"))
MODEL_BASE = "http://127.0.0.1:8190/v1"


class NativeVitaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Path(sys.prefix).resolve() != (PROJECT / ".venv-vita").resolve():
            raise unittest.SkipTest("optional native integration requires project .venv-vita")
        if not SOURCE.is_dir() or not DATASET.is_file():
            raise unittest.SkipTest("optional native integration requires fixed local Vita source/data")

    def test_real_t4_t5_environments_user_and_evaluator_with_no_network(self):
        self._exercise()

    def test_cloud_native_requests_omit_seed_preserve_real_tools_and_judge(self):
        self._exercise(cloud=True)

    def _exercise(self, cloud=False):
        """A wiring-test failure must not become a simulated model/task failure."""
        from ae_cloud_proxy import MODEL, COMPAT_PROFILE, CloudConfig, normalize_request
        model = MODEL if cloud else "Qwen3-8B"
        sample = prepare_sample(DATASET, end_turn=5)
        private_original = json.loads(DATASET.read_text(encoding="utf-8"))[25]
        requests = []
        judge_fixtures = []
        selected = {"task": None, "ordinal": None}

        def fixture_completion(**request):
            # This is the actual OpenAI-format request built by native generate.
            requests.append(deepcopy(request))
            self.assertEqual(request["model"], model)
            self.assertEqual(request["temperature"], 0)
            self.assertEqual(request["max_tokens"], 4096)
            if cloud:
                self.assertNotIn("seed", request)
                body = {k:v for k,v in request.items() if k != "extra_headers"}
                raw = json.dumps(body).encode()
                mapped, changes = normalize_request(raw, CloudConfig(MODEL, 4096, 2097152, 2097152, 6, 60,
                                                       profile=COMPAT_PROFILE))
                self.assertEqual(mapped, raw)
                self.assertEqual(changes, [])
            else:
                self.assertEqual(request["seed"], 300)
            self.assertNotIn("tools", request)
            self.assertNotIn("extra_body", request)  # Retain model-default thinking.
            self.assertNotIn("enable_thinking", request)
            prompt_text = json.dumps(request["messages"], ensure_ascii=False)
            self.assertIn(selected["task"]["instruction"], prompt_text)
            # At t4 exclude t5 and every later instruction; at t5 exclude t6+.
            for future in private_original["subtasks"][selected["ordinal"]:]:
                self.assertNotIn(future["instruction"], prompt_text)
            role = request["extra_headers"]["X-AE-Role"]
            if role == "user_simulator":
                self.assertNotIn("<current_rubrics>", prompt_text)
                content = "谢谢，结束本次离线测试。###STOP###"
            else:
                self.assertEqual(role, "evaluator")
                # Read rubric identifiers from the real native prompt, not a
                # fabricated Task or substituted evaluator implementation.
                rubric_section = re.search(
                    r"<current_rubrics>\s*(.*?)\s*</current_rubrics>",
                    request["messages"][-1]["content"], re.DOTALL)
                self.assertIsNotNone(rubric_section)
                labels = re.findall(r'"rubric_idx"\s*:\s*"(rubric_\d+)"',
                                    rubric_section.group(1))
                self.assertEqual(len(labels), 3 if selected["ordinal"] == 4 else 4)
                self.assertEqual(len(labels), len(set(labels)))
                # Deliberately exercise both native all-pass and one-fail
                # aggregation. These booleans are fixtures, not task judgments.
                decisions = [{"rubric_idx": label,
                              "meetExpectation": selected["ordinal"] == 4 or i != 0,
                              "justification": "Offline integration fixture; not a model judgment."}
                             for i, label in enumerate(labels)]
                judge_fixtures.append(deepcopy(decisions))
                content = json.dumps(decisions, ensure_ascii=False)
            response = {
                "id": "fixture-no-model-" + str(len(requests)),
                "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content,
                                         "tool_calls": None,
                                         "reasoning_content": "Private fixture reasoning, not agent input."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            return SimpleNamespace(model_dump=lambda: deepcopy(response))

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=fixture_completion)))
        # Vita config is module-level. Keep this two-task sequence in one test
        # and restore modules/path afterwards so the temporary config cannot
        # leak into a later test. No on-disk source or production config changes.
        prior_modules = {k: v for k, v in sys.modules.items()
                         if k == "vita" or k.startswith("vita.")}
        prior_path = list(sys.path)
        self.assertFalse(prior_modules, "run this native integration in a fresh process")

        def restore_imports():
            for name in list(sys.modules):
                if name == "vita" or name.startswith("vita."):
                    sys.modules.pop(name, None)
            sys.modules.update(prior_modules)
            sys.path[:] = prior_path

        self.addCleanup(restore_imports)
        with TemporaryDirectory(prefix="ae-native-integration-") as temporary, ExitStack() as stack:
            config_path = Path(temporary) / "models.json"
            config_path.write_text(json.dumps({"default": {}, "models": [
                {"name": model, "base_url": MODEL_BASE, "api_key": "EMPTY"}
            ]}), encoding="utf-8")
            stack.enter_context(patch.dict(os.environ, {"VITA_MODEL_CONFIG_PATH": str(config_path)}))
            denied = [stack.enter_context(patch(target, side_effect=AssertionError(
                "NETWORK FORBIDDEN: offline integration test, not a model failure")))
                for target in ("socket.socket.connect", "socket.socket.connect_ex",
                               "socket.create_connection")]
            native = NativeVita(SOURCE, DATASET, MODEL_BASE, model, 0, 4096,
                                transport_profile=COMPAT_PROFILE if cloud else "local-vllm")
            stack.callback(native.abort)
            llm_utils = importlib.import_module("vita.utils.llm_utils")
            # Do not patch generate/capture/evaluator/parser: only the SDK seam.
            factory = stack.enter_context(patch.object(llm_utils, "_get_client", return_value=client))
            original_user_generate = native._native.user_module.generate
            original_judge_generate = native._native.judge_module.generate
            self.assertIs(original_user_generate, llm_utils.generate)
            self.assertIs(original_judge_generate, llm_utils.generate)
            previews = native.preview_tasks()
            self.assertEqual(len(requests), 0)
            self.assertEqual([p["domain"] for p in previews], ["delivery", "instore"])
            user_identity = id(native._user)
            environments = []
            results = []
            schemas_by_task = []

            for index, task in enumerate(sample["tasks"]):
                ordinal = index + 4
                selected.update(task=task, ordinal=ordinal)
                self.assertEqual(native._next, index)
                self.assertEqual(native._user.current_subtask_idx, index)
                started = native.start(task)
                self.assertEqual(len(requests), index * 2)  # First instruction requires no LLM.
                self.assertEqual(id(native._user), user_identity)
                self.assertEqual(started["instruction"], task["instruction"])
                self.assertEqual(started["domain_policy"], previews[index]["domain_policy"])
                env = started["environment"]
                environments.append(env)
                tools = {tool.name: tool for tool in env.get_tools()}
                schemas = {name: tool.openai_schema["function"] for name, tool in tools.items()}
                schemas_by_task.append(schemas)
                self.assertEqual(list(schemas.values()), previews[index]["tool_schemas"])
                tool_name, keyword = (("delivery_store_search_recommand", "奶茶") if ordinal == 4
                                      else ("instore_shop_search_recommend", "按摩"))
                args = {"keywords": [keyword]}
                schema = schemas[tool_name]["parameters"]
                self.assertEqual(schema["required"], ["keywords"])
                self.assertEqual(schema["properties"]["keywords"]["type"], "array")
                self.assertEqual(schema["properties"]["keywords"]["items"]["type"], "string")
                self.assertEqual(tools[tool_name].params.model_validate(args).model_dump(), args)
                before_hash = env.get_db_hash()
                self.assertIsNotNone(before_hash)
                # Exactly one real, local, native read-only search per task.
                tool_result = env.make_tool_call(tool_name, requestor="assistant", **args)
                tool_text = env.to_json_str(tool_result)
                self.assertIsInstance(tool_text, str)
                self.assertTrue(tool_text.strip())
                self.assertEqual(env.get_db_hash(), before_hash)
                self.assertEqual(json.loads(env.to_json_str({"result": tool_result})),
                                 {"result": tool_result})
                assistant_text = "这是离线集成测试的固定最终答复，请结束本次测试。"
                reply = native.user_reply(assistant_text)
                self.assertTrue(reply["stop"])
                self.assertIn("###STOP###", reply["content"])
                self.assertNotIn("Private fixture reasoning", reply["content"])
                self.assertEqual(native._native.user_module.generate, original_user_generate)
                call_id = "fixture-native-search-t" + str(ordinal)
                transcript = [
                    started["greeting"].model_dump(mode="json"),
                    {"role": "user", "content": started["instruction"]},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": call_id, "name": tool_name, "arguments": args, "requestor": "assistant"}]},
                    {"role": "tool", "id": call_id, "name": tool_name,
                     "content": tool_text, "requestor": "assistant", "error": False},
                    {"role": "assistant", "content": assistant_text},
                    reply["raw"],
                ]
                result = native.finish(task, transcript, "user_stop", 0.0)
                results.append(result)
                self.assertIsNone(result["scientific_success"])
                self.assertFalse(result["official_benchmark"])
                self.assertEqual(result["judging_status"], "MODEL_JUDGED_DEBUG_ONLY")
                reward = result["reward_info"]
                self.assertEqual(reward["reward"], 1.0 if ordinal == 4 else 0.0)
                self.assertEqual([r["met"] for r in reward["nl_rubrics"]],
                                 [d["meetExpectation"] for d in judge_fixtures[index]])
                self.assertEqual(len(reward["window_evaluations"]), 1)
                window = reward["window_evaluations"][0]
                self.assertEqual(native._native.utils.evaluator_extracter(window["assistant_message_content"]),
                                 judge_fixtures[index])
                self.assertIn(task["instruction"], window["system_prompt"])
                self.assertIn(tool_name, window["user_prompt"])
                self.assertEqual(native._native.judge_module.generate, original_judge_generate)
                self.assertEqual(native._next, index + 1)
                self.assertEqual(native._user.current_subtask_idx, index + 1)

            self.assertIsNot(environments[0], environments[1])
            self.assertIn("delivery_store_search_recommand", schemas_by_task[0])
            self.assertNotIn("delivery_store_search_recommand", schemas_by_task[1])
            self.assertIn("instore_shop_search_recommend", schemas_by_task[1])
            self.assertNotIn("instore_shop_search_recommend", schemas_by_task[0])
            snapshot = native.snapshot()
            self.assertEqual(snapshot["next_index"], 2)
            self.assertIsNone(snapshot["active_subtask_id"])
            self.assertEqual(snapshot["completed"], results)
            self.assertEqual([c["role"] for c in snapshot["native_calls"]],
                             ["user_simulator", "evaluator"] * 2)
            self.assertEqual([c["subtask_id"] for c in snapshot["native_calls"]],
                             [sample["tasks"][i]["subtask_id"] for i in (0, 0, 1, 1)])
            for call in snapshot["native_calls"]:
                self.assertIsNone(call["error"])
                self.assertIn("reasoning_content", call["response"]["raw_data"]["choices"][0]["message"])
            self.assertEqual(factory.call_count, 4)
            for call in factory.call_args_list:
                self.assertEqual(call.kwargs, {"base_url": MODEL_BASE, "api_key": "EMPTY", "max_retries": 0})
            self.assertEqual(len(requests), 4)
            for guard in denied:
                guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
