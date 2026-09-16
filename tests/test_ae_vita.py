"""Offline fixtures, not native Vita/Qwen execution or task-success evidence."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ae_vita as av


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    @classmethod
    def model_validate(cls, row):
        return cls(**deepcopy(row))

    def model_dump(self, **kw):
        return {k: av._plain(v) for k, v in self.__dict__.items()}


class Msg(Obj):
    def __init__(self, **kw):
        super().__init__(**dict({"content": None, "tool_calls": None, "raw_data": None}, **kw))

    def is_tool_call(self):
        return self.tool_calls is not None


def response(content="好的", finish="stop", tool_calls=None):
    return Msg(role="assistant", content=content, tool_calls=tool_calls,
               raw_data={"choices": [{"finish_reason": finish,
                                      "message": {"content": content, "reasoning_content": "fixture reasoning"}}]})


class NativeVitaFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.config = self.root / "models.json"
        self.config.write_text('{}')
        self.dataset = self.root / "tasks.json"
        self.profile = {"user_id": "U000828"}
        self.raw_tasks = [{"subtask_id": "sub_U000828_" + str(n), "domain": "delivery" if n == 4 else "instore",
                           "environment": {"time": "2026-01-0" + str(n) + " 10:00:00", "secret_gold": "PRIVATE"},
                           "instruction": "task " + str(n), "user_scenario": {"private": "PRIVATE"},
                           "evaluation_criteria": {"private_rubric": "PRIVATE"},
                           "message_history": [], "skill_tested": []} for n in (4, 5)]
        original = {"user_profile": self.profile, "subtasks": [{}, {}, {}] + self.raw_tasks}
        self.dataset.write_text(json.dumps([None] * 25 + [original]))
        self.tasks = [{"number": n, "subtask_id": "sub_U000828_" + str(n),
                       "domain": "delivery" if n == 4 else "instore", "current_time": "2026-01-0" + str(n),
                       "instruction": "task " + str(n), "history": []} for n in (4, 5)]
        self.sample = {"initial_profile": self.profile, "tasks": self.tasks,
                       "source": {"sha256": hashlib.sha256(self.dataset.read_bytes()).hexdigest()}}
        self.clears = []
        self.envs = []
        self.user_calls = []
        self.judge_calls = []
        self.user_response = response()
        self.judge_response = response('[{"rubric_idx":"rubric_0","meetExpectation":true,"justification":"fixture"}]')
        owner = self
        user_module = SimpleNamespace()
        judge_module = SimpleNamespace()

        def user_generate(**kw):
            owner.user_calls.append(kw)
            return owner.user_response

        def judge_generate(**kw):
            owner.judge_calls.append(kw)
            return owner.judge_response

        class User:
            def __init__(self, subtasks, persona, instructions, llm, llm_args, language):
                self.subtasks, self.persona = subtasks, persona
                self.llm, self.llm_args = llm, llm_args
                self.current_subtask_idx = 0
                self._subtask_started = False

            @property
            def system_prompt(self):
                return "NATIVE-FIXTURE persona=" + self.persona + " task=" + str(self.current_subtask_idx)

            def set_seed(self, seed):
                self.llm_args['seed'] = seed

            def get_init_state(self):
                return Obj(system_messages=[Msg(role="system", content=self.system_prompt)], messages=[])

            def generate_next_message(self, message, state):
                state.messages.append(message)
                if not self._subtask_started:
                    self._subtask_started = True
                    result = Msg(role="user", content=self.subtasks[self.current_subtask_idx].instruction)
                else:
                    raw = user_module.generate(model=self.llm, messages=state.system_messages + state.messages,
                                               tools=None, **self.llm_args)
                    result = Msg(role="user", content=raw.content, tool_calls=raw.tool_calls, raw_data=raw.raw_data)
                state.messages.append(result)
                return result, state

            def mark_subtask_completed(self):
                pass

            def advance_to_next_subtask(self):
                self.current_subtask_idx += 1
                self._subtask_started = False

        user_module.PersonalizationUser = User
        user_module.generate = user_generate
        judge_module.generate = judge_generate
        judge_module.TrajectoryEvaluator = SimpleNamespace(_initialize_rubric_states=lambda _: {"rubric_0": {}})

        def environment(raw, language):
            db = Obj(time=raw['time'], orders={})
            env = Obj(tools=Obj(db=db))
            env.get_tools = lambda: [Obj(openai_schema={"function": {"name": "fixture_tool", "parameters": {}}})]
            owner.envs.append(env)
            return env

        def evaluate(**kw):
            if kw['simulation'].termination_reason == 'max_steps':
                return Obj(reward=0.0, info={"note": "premature"}, window_evaluations=None)
            judge_module.generate(model=kw['llm_evaluator'],
                                  messages=[Msg(role="system", content="NATIVE-JUDGE-PROMPT")],
                                  **kw['llm_args_evaluator'])
            return Obj(reward=1.0, info={}, window_evaluations=[])

        self.native = SimpleNamespace(
            personalization=SimpleNamespace(SubTask=Obj), user_module=user_module, judge_module=judge_module,
            user_simulator=SimpleNamespace(UserSimulator=SimpleNamespace(is_stop=lambda m: '###STOP###' in m.content)),
            agent=SimpleNamespace(BaseAgent=SimpleNamespace(is_stop=lambda m: bool(m.content and '###STOP###' in m.content))),
            messages=SimpleNamespace(AssistantMessage=Msg, UserMessage=Msg, ToolMessage=Msg),
            tasks=SimpleNamespace(Task=Obj, **{name: SimpleNamespace(clear_thread_data=lambda n=name: owner.clears.append(n))
                                             for name in ('StoreBaseModel', 'ProductBaseModel', 'Location')}),
            orchestrator=SimpleNamespace(get_default_first_agent_message=lambda _: Msg(role='assistant', content='你好'),
                                         Orchestrator=SimpleNamespace(get_states=lambda _, db, time: {'old_states': [], 'new_states': []})),
            registry=SimpleNamespace(registry=SimpleNamespace(get_env_constructor=lambda _: environment)),
            prompts=SimpleNamespace(get_prompts=lambda _: SimpleNamespace(personalization_agent_system_prompt='NATIVE {time}',
                    personalization_agent_proactive_system_prompt='PROACTIVE {time}', personalization_agent_addendum='ADDENDUM')),
            utils=SimpleNamespace(get_weekday=lambda *args: '星期一', get_now=lambda: '2026-01-01T00:00:00Z', evaluator_extracter=json.loads),
            simulation=SimpleNamespace(SimulationRun=Obj), evaluator=SimpleNamespace(evaluate_simulation=evaluate))
        for patcher in (patch.dict(os.environ, {'VITA_MODEL_CONFIG_PATH': str(self.config)}),
                        patch.object(av, '_verify_source', return_value={'git_head': av.VITA_REVISION}),
                        patch.object(av, 'prepare_sample', return_value=self.sample),
                        patch.object(av, '_load_native', return_value=self.native)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: setattr(av._ACTIVE, 'owner', None))

    def create(self):
        return av.NativeVita(self.source, self.dataset, 'http://127.0.0.1:8190', 'Qwen3-8B', 0, 4096)

    def transcript(self):
        return [{'role': 'user', 'content': 'task 4'}, {'role': 'assistant', 'content': '完成 ###STOP###'}]

    def test_start_is_native_instruction_without_model_or_gold(self):
        native = self.create()
        started = native.start(self.tasks[0])
        self.assertEqual(started['instruction'], 'task 4')
        self.assertEqual(self.user_calls, [])
        self.assertEqual(self.clears, ['StoreBaseModel', 'ProductBaseModel', 'Location'])
        self.assertNotIn('PRIVATE', started['domain_policy'])
        self.assertNotIn('{time}', started['domain_policy'])
        self.assertEqual(set(started), {'environment', 'domain_policy', 'instruction', 'greeting'})

    def test_user_uses_native_state_and_records_prompts_raw(self):
        native = self.create()
        native.start(self.tasks[0])
        self.user_response = response('满意 ###STOP###')
        result = native.user_reply('已完成')
        self.assertTrue(result['stop'])
        self.assertEqual(result['content'], '满意 ###STOP###')
        self.assertEqual(self.user_calls[0]['num_retries'], 0)
        self.assertEqual(self.user_calls[0]['extra_headers'], {'X-AE-Role': 'user_simulator'})
        self.assertNotIn('extra_body', self.user_calls[0])
        self.assertEqual(native.snapshot()['native_calls'][0]['role'], 'user_simulator')
        self.assertIn('reasoning_content', result['raw']['raw_data']['choices'][0]['message'])

    def test_finish_native_judgment_is_debug_only_and_advances(self):
        native = self.create()
        native.start(self.tasks[0])
        result = native.finish(self.tasks[0], self.transcript(), 'agent_stop', 1.0)
        self.assertIsNone(result['scientific_success'])
        self.assertFalse(result['official_benchmark'])
        self.assertEqual(result['judging_status'], 'MODEL_JUDGED_DEBUG_ONLY')
        self.assertEqual(self.judge_calls[0]['extra_headers'], {'X-AE-Role': 'evaluator'})
        self.assertEqual(native.start(self.tasks[1])['instruction'], 'task 5')
        self.assertIsNot(self.envs[0], self.envs[1])

    def test_native_stop_not_letta_end_turn(self):
        native = self.create()
        self.assertFalse(native.agent_stop('完成了'))
        self.assertTrue(native.agent_stop('完成 ###STOP###'))

    def test_preview_builds_each_environment_without_consuming_user_or_calling_llm(self):
        native = self.create()
        previews = native.preview_tasks()
        self.assertEqual([p['task_id'] for p in previews], ['sub_U000828_4', 'sub_U000828_5'])
        self.assertEqual(len(self.envs), 2)
        self.assertEqual(self.user_calls + self.judge_calls, [])
        self.assertTrue(all(not p['tools_executed'] and not p['model_called'] for p in previews))
        self.assertIsNone(getattr(av._ACTIVE, 'owner', None))
        self.assertEqual(native.start(self.tasks[0])['instruction'], 'task 4')
        with self.assertRaises(av.NativeVitaError):
            native.preview_tasks()

    def test_rejects_root_profile_drift(self):
        self.sample['initial_profile'] = {'user_id': 'different'}
        with self.assertRaisesRegex(av.NativeVitaError, 'persona'):
            self.create()

    def test_rejects_skips_and_modified_public_tasks(self):
        native = self.create()
        with self.assertRaises(av.NativeVitaError):
            native.start(self.tasks[1])
        with self.assertRaises(av.NativeVitaError):
            native.start(dict(self.tasks[0], user_scenario={'gold': 'PRIVATE'}))

    def test_rejects_interleaved_environments_abort_retains_snapshot(self):
        left, right = self.create(), self.create()
        left.start(self.tasks[0])
        with self.assertRaises(av.NativeVitaError):
            right.start(self.tasks[0])
        left.abort()
        self.assertTrue(left.snapshot()['aborted'])
        self.assertIsNotNone(left.snapshot()['environment_db'])
        right.start(self.tasks[0])
        with self.assertRaises(av.NativeVitaError):
            left.user_reply('do not retry')

    def test_user_length_or_tool_call_is_invalid(self):
        for bad in (response('partial', finish='length'), response('text', tool_calls=[])):
            with self.subTest(bad=bad.raw_data):
                native = self.create()
                native.start(self.tasks[0])
                self.user_response = bad
                with self.assertRaises(av.NativeVitaError):
                    native.user_reply('actual answer')
                self.assertIsNotNone(native.snapshot()['native_calls'][0]['response'])
                native.abort()

    def test_judge_parse_failure_does_not_become_zero_score(self):
        for text in ('[]', 'not json', '[{"rubric_idx":"rubric_0","meetExpectation":"false","justification":"bad"}]'):
            with self.subTest(text=text):
                native = self.create()
                native.start(self.tasks[0])
                self.judge_response = response(text)
                original = self.native.judge_module.generate
                with self.assertRaises(av.NativeVitaError):
                    native.finish(self.tasks[0], self.transcript(), 'agent_stop', 1)
                self.assertIsNone(native.snapshot()['last_evaluation']['reward_info'])
                self.assertIs(self.native.judge_module.generate, original)
                native.abort()

    def test_premature_stop_is_not_model_judged(self):
        native = self.create()
        native.start(self.tasks[0])
        result = native.finish(self.tasks[0], self.transcript(), 'max_steps', 1)
        self.assertEqual(self.judge_calls, [])
        self.assertIsNone(result['scientific_success'])
        self.assertEqual(result['judging_status'], 'NOT_MODEL_JUDGED_PREMATURE_TERMINATION')

    def test_finite_parameters_and_duration(self):
        with self.assertRaises(av.NativeVitaError):
            av.NativeVita(self.source, self.dataset, 'http://127.0.0.1:8190', 'Qwen3-8B', float('nan'), 4096)
        native = self.create()
        native.start(self.tasks[0])
        with self.assertRaises(av.NativeVitaError):
            native.finish(self.tasks[0], self.transcript(), 'agent_stop', float('inf'))

    def test_transcript_requires_real_tool_ids(self):
        native = self.create()
        native.start(self.tasks[0])
        for row in ({'role': 'tool', 'content': 'result', 'tool_call_id': 'id_only_in_wrong_field'},
                    {'role': 'assistant', 'tool_calls': [{'name': 'x', 'arguments': {}}]},
                    {'role': 'user', 'tool_calls': [{'id': 'x', 'name': 'x', 'arguments': {}}]}):
            with self.subTest(row=row), self.assertRaises(av.NativeVitaError):
                native._messages([row])


class NativeVitaBoundaryTests(unittest.TestCase):
    def test_loopback_model_base(self):
        self.assertEqual(av._model_base('http://127.0.0.1:8190/v1/'), 'http://127.0.0.1:8190/v1')
        for url in ('https://example.com/v1', 'http://localhost:8190', 'http://x:y@127.0.0.1:8190/v1'):
            with self.assertRaises(av.NativeVitaError):
                av._model_base(url)

    def test_source_head_and_dirty_gates(self):
        def result(stdout):
            return subprocess.CompletedProcess([], 0, stdout=stdout, stderr='')
        with patch.object(av.subprocess, 'run', side_effect=[result(av.VITA_REVISION), result(' M src/vita/config.py')]):
            with self.assertRaises(av.NativeVitaError):
                av._verify_source(Path('/fixture'))
        with patch.object(av.subprocess, 'run', side_effect=[result('wrong')]):
            with self.assertRaises(av.NativeVitaError):
                av._verify_source(Path('/fixture'))
        with patch.object(av.subprocess, 'run', side_effect=[result(av.VITA_REVISION), result('')]):
            self.assertTrue(av._verify_source(Path('/fixture'))['tracked_clean'])

    def test_local_only_dummy_key_config_gate_before_vita_import(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(sys.modules, {'yaml': SimpleNamespace(safe_load=json.loads)}):
            p = Path(td) / 'models.json'
            for config in ({'default': {}, 'models': [{'name': 'Qwen3-8B', 'base_url': 'https://external.invalid/v1', 'api_key': 'EMPTY'}]},
                           {'default': {}, 'models': [{'name': 'Qwen3-8B', 'base_url': 'http://127.0.0.1:8190/v1', 'api_key': 'not-the-allowed-placeholder'}]},
                           {'default': {'temperature': 1}, 'models': []}):
                p.write_text(json.dumps(config))
                with self.subTest(config=config), self.assertRaises(av.NativeVitaError):
                    av._load_native(Path(td), p, 'http://127.0.0.1:8190/v1', 'Qwen3-8B')


if __name__ == '__main__':
    unittest.main()
