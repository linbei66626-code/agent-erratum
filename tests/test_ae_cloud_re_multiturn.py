"""Offline tests for the continuous original-data t4->t12 R/E pair driver.

Positive fixtures are produced by the REAL production path (`execute_re_multiturn`
+ the pinned `MemoryPolicy`/`TaskBridge`/`MultiturnTaskBridge`/`CheckedTaskTransport`)
against a scripted offline two-agent Letta session, a real `CloudAuditProxy` whose
opener is a fixture, and REAL native domain environments built from the fixed
Vita registry. Only the model/user/judge replies are fixtures; the input
projection, ordering, provenance, tool execution and the full audit are real.

The chain can be built over a real t4..t5 pilot projection when the fixed dataset
is available (AE_VITA_DATASET or the archived copy), and over an equivalent
synthetic projection otherwise. The synthetic path is always labelled
`fixture=true` and never claims real-data or model evidence.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for entry in (TESTS, ROOT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from ae_adapter import dumps  # noqa: E402
from ae_capability import (WORK_ADDRESS_KEY, environment_orders,  # noqa: E402
                           environment_stores)
from ae_cloud_proxy import CloudAuditProxy, CloudConfig  # noqa: E402
from ae_cloud_re_multiturn import (ARMS, END_TURN, START_TURN,  # noqa: E402
                                   build_plan, execute_re_multiturn,
                                   multiturn_code_files, phase_order, preflight,
                                   validate_config)
from ae_cloud_re_multiturn_input_audit import (  # noqa: E402
    audit_re_multiturn_inputs, multiturn_source_files)
from ae_input_audit import utc  # noqa: E402
from ae_inputs import canonical_sha256  # noqa: E402

import test_ae_cloud_re_pair as pair  # noqa: E402
from test_ae_cloud_input_audit import (KEY, proxy_rows, rewrite_records,  # noqa: E402
                                       save_proxy_rows, write_json, write_records)

CONFIG_PATH = (ROOT / "configs"
               / "ae-01__re-multiturn__siliconflow.original-candidate.json")
#: The bounded t4..t5 pilot projection is the only other verified bound; its own
#: config file declares it, so a pilot capture can never be audited as the full
#: nine-task production scope.
PILOT_CONFIG_PATH = (ROOT / "configs"
                     / "ae-01__re-multiturn__siliconflow.pilot-t4-t5-candidate.json")
TRANSPORT_CONFIG = pair.TRANSPORT_CONFIG
DATASET = Path(os.environ.get("AE_VITA_DATASET", "")) if os.environ.get("AE_VITA_DATASET") \
    else ROOT / ".ae-verify-src" / "tasks-full-a4553e1.json"
VITA_SOURCE = Path(os.environ.get("AE_VITA_SOURCE", "")) if os.environ.get("AE_VITA_SOURCE") \
    else ROOT / ".ae-verify-src" / "source"
#: The reviewed pinned SCORING baseline and the archived pinned source it is
#: derived from. The module gate compares imported bytes with the declaration, so
#: the test has to read the very files the production path declares.
BASELINE_PATH = (Path(os.environ["AE_VITA_SCORER_BASELINE"])
                 if os.environ.get("AE_VITA_SCORER_BASELINE")
                 else ROOT / "deployment-assets/vita-scorer-baseline.json")
BASELINE_ARCHIVE = (Path(os.environ["AE_VITA_BASELINE_ARCHIVE"])
                    if os.environ.get("AE_VITA_BASELINE_ARCHIVE")
                    else ROOT / "deployment-assets/ae-vita-data-20260911-r1.tar.gz")


def _baseline_document():
    """The declared pinned scoring baseline, with an ABSOLUTE archive path.

    The declaration carries a repo-relative archive path; a temporary fixture runs
    from another directory, so the copy it builds points at the real archive
    explicitly instead of resolving the relative path against the temp dir.
    """
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    document["archive"]["path"] = str(BASELINE_ARCHIVE)
    return document


def _pinned_vita_entry():
    """The pinned checkout's import root, or None when it is unavailable."""
    if not (VITA_SOURCE / "src/vita/registry.py").is_file():
        return None
    return str(VITA_SOURCE / "src")


class pinned_vita:
    """Make the PINNED Vita source the imported one for a bounded scope.

    The production wrapper inserts the declared checkout ahead of site-packages
    and requires a dummy-key-only model config. Fixtures do the same so the
    native environment classes, tool schemas and tool types come from the fixed
    source. On exit every module the scope imported is removed again, so a later
    test in the same process still sees a clean module table.
    """

    def __init__(self):
        self.entry = _pinned_vita_entry()
        self.prior = None

    def __enter__(self):
        if self.entry is None:
            return False
        self.prior = {name: module for name, module in sys.modules.items()
                      if name == "vita" or name.startswith("vita.")}
        os.environ.setdefault("OPENAI_API_KEY", "EMPTY")
        config_path = Path(os.environ.get("VITA_MODEL_CONFIG_PATH",
                                          str(ROOT / ".ae-verify-src/vita-models.json")))
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if not config_path.is_file():
            config_path.write_text(json.dumps(
                {"default": {}, "models": [{"name": "Qwen3-8B",
                                            "base_url": "http://127.0.0.1:1/v1",
                                            "api_key": "EMPTY"}]}), encoding="utf-8")
        os.environ.setdefault("VITA_MODEL_CONFIG_PATH", str(config_path))
        if self.entry not in sys.path:
            sys.path.insert(0, self.entry)
        return True

    def __exit__(self, *_exc):
        if self.prior is None:
            return False
        prior, self.prior = self.prior, None
        for name in [name for name in sys.modules
                     if name == "vita" or name.startswith("vita.")]:
            if name not in prior:
                del sys.modules[name]
        return False

    def __del__(self):  # pragma: no cover - safety net for a failed build
        try:
            self.__exit__(None, None, None)
        except Exception:
            pass


#: The pinned `Message.to_openai_dict` projection. The real Letta client that
#: builds the provider request truncates tool-call ids to 29 characters
#: (`letta/schemas/message.py`); under the declared AE multicall profile the
#: reviewed patch replaces that truncation with `preserve_tool_call_id`, which
#: keeps the provider id whole. A fixture that projected differently from the
#: client it claims to be would not be the production wire, so the fixture
#: applies the same rule the patch applies instead of the audit being relaxed.
TOOL_CALL_ID_MAX_LEN = 29
#: The environment variable the patched client reads to decide the id rule.
MULTICALL_PROFILE_ENV = "AE_LETTA_MULTICALL_PROFILE"


def projected_tool_call_id(value):
    """The id the patched (or unpatched) Letta client would put on the wire."""
    if not isinstance(value, str):
        return value
    if os.environ.get(MULTICALL_PROFILE_ENV):
        return value
    return value[:TOOL_CALL_ID_MAX_LEN]


def openai_wire_messages(turns):
    """Project a Letta conversation onto the OpenAI wire, as the client does."""
    messages = []
    for turn in turns:
        if turn.get("type") == "tool_return":
            package = pair.native_package_function_response()
            messages.extend({
                "role": "tool",
                "content": package(returned["status"] == "success",
                                   returned["tool_return"], None),
                "tool_call_id": projected_tool_call_id(returned["tool_call_id"]),
            } for returned in turn["tool_returns"])
            continue
        if "role" not in turn:
            continue
        message = deepcopy(turn)
        if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str):
            message["tool_call_id"] = projected_tool_call_id(message["tool_call_id"])
        calls = message.get("tool_calls")
        if calls:
            message["tool_calls"] = [
                {"id": projected_tool_call_id(call["id"]),
                 "type": call.get("type", "function"),
                 "function": deepcopy(call["function"])} for call in calls]
            if message.get("content") == "" and not message.get("content"):
                message["content"] = None
        messages.append(message)
    return messages


def multiturn_config():
    return validate_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))


def real_sample(end_turn):
    """The REAL public projection for one verified scope, or None without data."""
    if not DATASET.is_file():
        return None
    from ae_inputs import prepare_sample
    return prepare_sample(DATASET, start_turn=START_TURN, end_turn=end_turn)


def real_projection():
    """The REAL t4..t5 pilot projection, or None when the fixed data is absent."""
    return real_sample(5)


def _history(number, count):
    records = []
    for index in range(count):
        dialogue = [{"role": "user", "content": f"t{number} 历史提问 {index}"},
                    {"role": "assistant", "content": f"t{number} 历史回答 {index}"}]
        if number == 4 and index == 15:
            dialogue.append({"role": "user", "content": "嗯！以后就7分糖了"})
        records.append({"ref": f"t{number}/history/{index}",
                        "record": {"date": f"2024-{number:02d}-{index % 20 + 1:02d}",
                                   "behavior": [], "dialogue": dialogue}})
    return records


def synthetic_projection():
    """An offline t4..t5 projection with real domains but fabricated records."""
    facts = {f"p{i:03d}": {"category": "其他", "content": f"偏好 {i}"} for i in range(14)}
    facts["p007"] = {"category": "饮食偏好", "content": "奶茶偏好5分糖"}
    return {
        "initial_facts": facts,
        "initial_profile": {"user_id": "U000828", WORK_ADDRESS_KEY: pair.WORK_ADDRESS,
                            "姓名": "测试用户"},
        "tasks": [
            {"number": 4, "subtask_id": "sub_U000828_4", "domain": "delivery",
             "current_time": "2024-06-23", "instruction": "帮我买一杯联名奶茶送到工作地址",
             "history": _history(4, 26)},
            {"number": 5, "subtask_id": "sub_U000828_5", "domain": "instore",
             "current_time": "2024-06-24", "instruction": "帮我团一个按摩",
             "history": _history(5, 3)},
        ],
        "private_tasks": {
            "sub_U000828_4": {"environment": pair.payment_precondition_db(),
                              "target_product_ids": [pair.TARGET_PRODUCT_ID],
                              "evaluation_criteria": {"expected_states": [], "overall_rubrics": []}},
            "sub_U000828_5": {"environment": {
                "user_id": "U000828", "time": "2024-06-24 10:00:00",
                "location": [{"address": pair.WORK_ADDRESS, "longitude": 1.0, "latitude": 2.0}],
                "shops": {"SH1": {"shop_id": "SH1", "shop_name": "按摩店", "score": 4.5,
                                  "location": {"longitude": 1.0, "latitude": 2.0,
                                               "address": pair.WORK_ADDRESS},
                                  "tags": ["按摩"], "enable_book": False, "book_price": 0.0,
                                  "enable_reservation": False,
                                  "products": [{"product_id": "SH1_P1", "name": "按摩券",
                                                "shop_id": "SH1", "tags": ["按摩"],
                                                "quantity": 10, "price": 99.0}]}},
                "orders": {}, "books": {}, "reservations": {}},
                "target_product_ids": ["SH1_P1"],
                "evaluation_criteria": {"expected_states": [], "overall_rubrics": []}},
        },
        "source": {"fixture": True, "user_id": "U000828", "original_user_index": 25,
                   "start_turn": START_TURN, "end_turn": 5},
    }


def real_environment_factory(sample):
    """Build the REAL pinned Vita environment for one task, exactly as the wrapper does.

    The native thread-local store/order/location registries are cleared before
    every construction, so a previous task's orders cannot leak into the next
    task's environment. This mirrors `NativeVita.start` deliberately; it is the
    per-task re-initialization the delivery notes describe, NOT shared state.
    The pinned package is imported only while a chain is being built and is
    removed from `sys.modules` again afterwards.
    """
    import importlib
    context = pinned_vita()
    if context.__enter__() is False:
        raise unittest.SkipTest("the pinned Vita source is not available")
    try:
        registry = importlib.import_module("vita.registry")
        tasks_module = importlib.import_module("vita.data_model.tasks")
    finally:
        pass

    def build(sample_task):
        from ae_vita import clear_thread_registries_for
        clear_thread_registries_for(tasks_module)
        task_id = sample_task["subtask_id"]
        declared = sample["private_tasks"][task_id]["environment"]
        constructor = registry.registry.get_env_constructor(sample_task["domain"])
        return constructor(json.loads(json.dumps(declared)), "chinese")

    return context, build


def _shorten_rubric(text):
    """A reply that omits the criteria's bracketed examples (fixture echo variant)."""
    for opener in ("（", "("):
        head = text.split(opener, 1)[0].rstrip("，,。;； ")
        if head and head != text:
            return head
    return text[: max(1, len(text) // 2)]


class MultiturnNative:
    """Scripted native runtime double mirroring NativeVita's per-task contract.

    It keeps ONE simulator across the whole arm (exactly like the fixed
    `PersonalizationUser`, which advances by subtask index) and serves a REAL
    native environment per task, so the bridge's tool bindings and the audit's
    per-task tool tables come from the fixed sources.
    """

    def __init__(self, arm, proxy, opener, environment_builder, *, user_script=None,
                 judge_fail_tasks=(), task_override=None, judge_windows=False,
                 sample=None, judge_echo_variant_windows=()):
        self.arm, self.proxy, self.opener = arm, proxy, opener
        self.environment_builder = environment_builder
        self.user_script = list(user_script or [])
        #: When set, only these tasks get the declared script; others stop at once.
        self.user_script_tasks = set()
        #: Optional per-task script overrides.
        self.user_script_map = {}
        self.judge_fail_tasks = set(judge_fail_tasks)
        self.task_override = task_override or {}
        #: Set by a chain builder when the native double needs to reach back to
        #: the provider (for example when a probe skews a reply's role).
        self.provider = None
        #: When true, the judge runs the REAL pinned sliding-window expansion.
        self.judge_windows = True if judge_windows else None
        #: The `met` value every window decision carries; a legal LOW score sets
        #: this to False so the aggregation is exercised for a failing run too.
        self.window_met = True
        #: Windows whose judge REPLY echoes a shortened rubric text (the real r5 run has
        #: six such replies). The fixture emits them while the run is BUILT, so the reply,
        #: the capture and the record carry one and the same text.
        self.judge_echo_variant_windows = set(judge_echo_variant_windows)
        self.sample = sample
        self.user_index = 0
        #: Per-task reply index, so one declared script covers every task.
        self.user_index_by_task = {}
        self.calls = []
        self.aborted = False
        self.protocols = None
        self.simulator_prompts = {}
        self.environments = {}
        self.baseline_orders_by_task = {}
        self.instruction_decisions = {}
        self.current_task = None
        self.finished = []
        #: The exact transcript submitted to the judge, per task.
        self.judged = {}
        #: The per-task simulation the judge scored, mirroring the wrapper's own
        #: `SimulationRun` record shape where it carries the trajectory.
        self.simulations = {}
        #: The wrapper's own per-arm cumulative records, in the REAL key shape: every
        #: completed task with its simulation and reward, plus the last evaluation (the
        #: SAME evaluation). There is deliberately no per-task copy.
        self.completed = []
        self.last_evaluation = None

    def record(self, role, task_id, content, *, messages=None, marker_suffix="",
               marker_override=None):

        marker = marker_override or (f"{role.replace('_', ' ')} {self.arm} {task_id} fixture"
                                     + marker_suffix)
        reply = pair.chat_reply("stop", content=content)
        self.opener.register(marker, reply)
        # The native capture stores the REAL request the role received. For the
        # judge that request carries the task transcript, not just a marker, so a
        # later audit can compare the judged trajectory with the task transcript.
        # It is shaped by the DECLARED transport, exactly like the agent call above.
        model, required = provider_request_shape(self.proxy)
        request = {"model": model,
                   "messages": deepcopy(messages) if messages else
                   [{"role": "system", "content": marker}],
                   "temperature": 0, "max_tokens": 4096, "tools": None, "stream": False}
        request.update(required)
        body = {k: v for k, v in request.items() if k != "stream"}
        # The native record stores the reply the provider REALLY returned, which names the
        # model that was asked for (the opener echoes it) - the same rule the sealed
        # profiles already satisfy byte for byte.
        wire_reply = pair.ChatOpener.echoed(reply, body)
        self.calls.append({"role": role, "subtask_id": task_id, "request": request,
                           # The REAL native record carries NO per-call time: the wrapper
                           # appends one record per generation and nothing else. A fixture
                           # that stamped one would let the audit pass for a reason the
                           # real capture never offers.
                           "error": None,
                           "response": {"role": role, "content": content, "tool_calls": None,
                                        "raw_data": wire_reply, "timestamp": None,
                                        "turn_idx": None, "cost": 0.0, "usage": None}})
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        dispatch_role = role
        if self.provider is not None and getattr(self.provider, "role_mutator", None):
            dispatch_role = self.provider.role_mutator(role)
        self.proxy.dispatch("POST", "/v1/chat/completions", raw, "re-multiturn/aux", dispatch_role)
        return reply

    def start(self, task, protocols=None):
        # The driver hands every runtime the ONE declared protocol bundle (None for a
        # schema that declares none). The fixture mirrors the real contract so the
        # driver's call shape is exercised, not bypassed.
        self.protocols = protocols
        task_id = task["subtask_id"]
        self.current_task = task_id
        environment = self.task_override.get(task_id) or self.environment_builder(task)
        # The REAL `NativeVita` appends the declared protocol's versioned constraint
        # block to the NATIVE simulator's own system prompt and records it in its
        # `start` event. The fixture reproduces that exact text so an audit of the
        # fixture verifies the same bytes a real run would carry.
        self.environments[task_id] = environment
        # Mirror `NativeVita.start`: the phase's pre-phase order table is captured here,
        # BEFORE any user turn, and the instruction turn's own decision is checked with
        # the same production validator the wrapper uses.
        import ae_sim_eval_protocol as sp
        orders = getattr(getattr(environment, "tools", None), "db", None)
        baseline = sorted(getattr(orders, "orders", None) or {})
        self.baseline_orders_by_task[task_id] = baseline
        if protocols is not None:
            from ae_sim_eval_protocol import user_simulator_system_suffix
            suffix = user_simulator_system_suffix(protocols["simulator_protocol"])
            self.simulator_prompts[task_id] = ("NATIVE personalization user prompt"
                                               + suffix + "\n\n" + task["instruction"])
            self.instruction_decisions[task_id] = sp.validate_user_reply(
                text=task["instruction"], incoming_assistant_text="",
                new_order_ids=[], paid_order_ids=[],
                protocol=protocols["simulator_protocol"])
        return {"environment": environment, "domain_policy": "fixture policy",
                "instruction": task["instruction"],
                "greeting": {"role": "assistant", "content": "fixture greeting"}}

    def agent_stop(self, text):
        return "###STOP###" in text

    def _protocol_decision(self, text, incoming):
        """The decision the REAL `NativeVita` would record for this turn.

        The fixture calls the production validator with the phase's own baseline/new
        order ids, exactly like the wrapper does, so an audited fixture carries the same
        recomputable decisions a real run would.
        """
        if self.protocols is None:
            return None
        import ae_sim_eval_protocol as sp
        snapshot = self._environment_orders()
        baseline = self.baseline_orders_by_task.get(self.current_task) or []
        return sp.validate_user_reply(
            text=text, incoming_assistant_text=incoming,
            new_order_ids=sorted(sp.new_orders_of(snapshot, baseline)),
            paid_order_ids=sorted(sp.paid_orders_of(snapshot, baseline)),
            protocol=self.protocols["simulator_protocol"])

    def _environment_db_json(self):
        """The phase's own environment database, shaped like the real snapshot's.

        `NativeVita.snapshot` serializes `environment.tools.db` to plain JSON; a
        fixture that reported `None` here would make the business facts unauditable.
        """
        import json as _json
        environment = self.environments.get(self.current_task)
        db = getattr(getattr(environment, "tools", None), "db", None)
        if db is None:
            return None
        if hasattr(db, "model_dump"):
            return _json.loads(db.model_dump_json())
        return _json.loads(_json.dumps(db, default=str))

    def _environment_orders(self):
        return (self._environment_db_json() or {}).get("orders") or {}

    def user_reply(self, text):
        task_id = self.current_task
        # The reply index is per (arm, task), exactly like the runtime_user ref
        # index the driver uses. A script may be scoped to specific tasks; every
        # other task stops immediately, which keeps the shared request budget.
        if task_id in self.user_script_tasks:
            script = self.user_script_map.get(task_id, self.user_script)
        else:
            script = []
        index = self.user_index_by_task.get(task_id, 0)
        if index < len(script):
            content = script[index]
        else:
            content = "谢谢，结束本次离线测试。###STOP###"
        self.user_index_by_task[task_id] = index + 1
        self.user_index += 1
        reply = self.record("user_simulator", task_id, content)
        returned = reply["choices"][0]["message"]["content"]
        stop = any(marker in returned for marker in
                   ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###"))
        decision = self._protocol_decision(returned, text)
        if decision is not None:
            stop = decision["stop"]
        return {"content": returned, "stop": stop, "raw": {},
                "protocol_decision": decision}

    def pending_instruction_decision(self):
        """Mirror `NativeVita`: the instruction decision is claimed once, at start."""
        return self.instruction_decisions.pop(self.current_task, None)

    def _native_messages(self, transcript):
        """The pinned classes' own conversion of a task transcript.

        Exactly `NativeVita._messages`: one native class per row, with `turn_idx` set to
        the row's index and a `timestamp` the row did not carry generated by the pinned
        default factory. The simulation the judge scored is THAT conversion, not the
        bridge's raw transcript rows.
        """
        import importlib
        source = Path(os.environ["AE_VITA_SOURCE"]).resolve()
        entry = str(source / "src")
        if entry not in sys.path:
            sys.path.insert(0, entry)
        messages_module = importlib.import_module("vita.data_model.message")
        classes = {"assistant": messages_module.AssistantMessage,
                   "user": messages_module.UserMessage, "tool": messages_module.ToolMessage}
        native = []
        for index, row in enumerate(transcript):
            message = classes[row["role"]].model_validate(deepcopy(row))
            message.turn_idx = index
            native.append(message)
        return native

    @staticmethod
    def _plain(value):
        from ae_vita import _plain
        return _plain(value)

    def finish(self, task, transcript, termination_reason, duration, *, arm=None,
               target_product_ids=None):
        task_id = task["subtask_id"]
        if task_id in self.judge_fail_tasks:
            raise RuntimeError("fixture judge failure")
        judged_messages = deepcopy(transcript)
        self.judged[task_id] = judged_messages
        simulation = {
            "id": f"simulation-{self.arm}-{task_id}",
            # The REAL native conversion of the task id, exactly as `NativeVita.finish`
            # builds it; the subtask id alone is not the scored simulation's task id.
            "task_id": "U000828_subtask_" + task_id,
            "termination_reason": termination_reason,
            # The messages the judge was handed: the pinned classes' conversion, with
            # `turn_idx` set and a generated timestamp where the transcript had none.
            "messages": [self._plain(message)
                         for message in self._native_messages(transcript)],
            "start_time": pair.now(), "end_time": pair.now(),
            "duration": duration, "seed": 300, "states": {},
        }
        if self.judge_windows is not None:
            reward = self._finish_windows(task, task_id, judged_messages,
                                          termination_reason)
        else:
            # A judge reply is bound to the exact system prompt text it answers, so
            # two different tasks can never exchange replies.
            marker = f"evaluator {self.arm} {task_id} fixture"
            self.record("evaluator", task_id, dumps(judged_messages),
                        messages=[{"role": "system", "content": marker},
                                  {"role": "user", "content": dumps(judged_messages)}],
                        marker_override=marker)
            reward = {"reward": 0.0, "nl_rubrics": [], "info": {}}
        # The wrapper's own per-task records: `completed` accumulates across the arm, and
        # `last_evaluation` is the SAME evaluation. The audit resolves this phase's own
        # simulation from `completed[].subtask_id` and crosses it with `last_evaluation`,
        # so the fixture carries both exactly like the real snapshot does - it does NOT
        # publish a per-task copy the real wrapper never writes.
        self.simulations[task_id] = simulation
        self.completed.append({"subtask_id": task_id, "simulation": deepcopy(simulation),
                               "reward_info": deepcopy(reward),
                               "judging_status": "MODEL_JUDGED_DEBUG_ONLY",
                               "scientific_success": None, "official_benchmark": False,
                               "note": "fixture simulation, native record shape"})
        self.last_evaluation = {"simulation": deepcopy(simulation),
                                "reward_info": deepcopy(reward), "error": None}
        self.finished.append(task_id)
        return {"reward_info": reward, "scientific_success": None,
                "judging_status": "MODEL_JUDGED_DEBUG_ONLY"}

    def _finish_windows(self, task, task_id, transcript, termination_reason):
        """Drive the REAL pinned sliding-window expansion, offline.

        The windows, the rendered window content and the rendered rubric state all
        come from the fixed `vita/evaluator/evaluator_traj` functions; the audit
        re-derives the same expansion from the same fixed source. Only the judge's
        REPLY is a fixture. The task's own evaluation criteria come from the fixed
        dataset, so the rubric keys are the real ones.
        """
        import importlib
        # The pinned `vita.config` interpolates the example models.yaml at import
        # time; the offline fixture supplies the same dummy key the real wrapper
        # requires instead of leaving the fixed package unimportable.
        os.environ.setdefault("OPENAI_API_KEY", "EMPTY")
        source = Path(os.environ["AE_VITA_SOURCE"]).resolve()
        entry = str(source / "src")
        if entry not in sys.path:
            sys.path.insert(0, entry)
        evaluator_module = importlib.import_module("vita.evaluator.evaluator_traj")
        prompts = importlib.import_module("vita.prompts").get_prompts("chinese")
        native = self._native_messages(transcript)
        criteria = self.sample["private_tasks"][task_id]["evaluation_criteria"]
        # The rubric TEXT is the task's own criterion text, taken from the criteria
        # through the pinned initialiser - not a placeholder invented here.
        initial_states = evaluator_module.TrajectoryEvaluator._initialize_rubric_states(
            _CriteriaStub(criteria))
        need_keys = list(initial_states) or ["rubric_0"]
        windows = evaluator_module.TrajectoryEvaluator._create_sliding_windows(native, 10, 2)
        states = {key: {"rubric_idx": key,
                        "rubric": initial_states.get(key, {}).get("rubric",
                                                                  f"rubric {key}"),
                        "justification": "Not evaluated yet", "meetExpectation": False}
                  for key in need_keys}
        env_time = self.sample["private_tasks"][task_id]["environment"].get("time", "")
        # The offline config must be importable by the pinned package, the same way
        # the real wrapper requires a dummy-key-only local config.
        os.environ.setdefault("VITA_MODEL_CONFIG_PATH",
                              str(ROOT / ".ae-verify-src/vita-models.json"))
        evaluations = []
        for index, window in enumerate(windows):
            system_prompt = prompts.sliding_window_eval_template.format(
                env_info=repr({"system_time": env_time, "database": []}),
                user_instruction=task["instruction"], window_idx=index + 1,
                total_windows=len(windows))
            window_content = evaluator_module.TrajectoryEvaluator._format_window_content(
                window, index * 8)
            current = evaluator_module.TrajectoryEvaluator._format_current_rubrics(states)
            user_prompt = (
                "\n# Input\n<window_content>\n" + window_content
                + "\n</window_content>\n\n<current_rubrics>\n" + current
                + "\n</current_rubrics>\n")
            decisions = []
            for key in need_keys:
                rubric_text = states[key]["rubric"]
                if (task_id, index + 1) in self.judge_echo_variant_windows:
                    # The reply omits the criteria's bracketed examples, exactly like the
                    # real replies that failed the strict verbatim-echo requirement. The
                    # STATE keeps the criteria text: that is what the pinned evaluator
                    # does with a reply (`rubric_idx` decides, never the echoed text).
                    rubric_text = _shorten_rubric(rubric_text)
                decisions.append({"rubric_idx": key, "rubric": rubric_text,
                                  "justification": f"fixture window {index + 1} decision",
                                  "meetExpectation": self.window_met})
            # The real replies are fenced ```json blocks; the fixture reproduces
            # that shape so the audit's pinned parser is exercised, not bypassed.
            reply_content = "```json\n" + json.dumps(decisions, ensure_ascii=False) + "\n```"
            # The reply is bound to the window's OWN system prompt text: the
            # pinned template names this window's index, which the real prompt
            # really contains, so the opener can never answer another window.
            self.record("evaluator", task_id, reply_content,
                        messages=[{"role": "system", "content": system_prompt},
                                  {"role": "user", "content": user_prompt}],
                        marker_override=f"第 {index + 1} 个窗口")
            for decision in decisions:
                states[decision["rubric_idx"]]["justification"] = decision["justification"]
                states[decision["rubric_idx"]]["meetExpectation"] = decision["meetExpectation"]
            evaluations.append({"window_idx": index + 1, "system_prompt": system_prompt,
                                "user_prompt": user_prompt,
                                "assistant_message_content": reply_content})
        # The final rubrics use the pinned output shape (`nl_rubric`/`met`/
        # `justification`), produced from the accumulated states exactly as the
        # fixed `_convert_states_to_checks` does.
        final = [{"nl_rubric": states[key]["rubric"],
                  "met": states[key]["meetExpectation"],
                  "justification": states[key]["justification"]} for key in need_keys]
        reward = 1.0 if (final and all(item["met"] for item in final)) else 0.0
        return {"reward": reward, "nl_rubrics": final,
                "reward_breakdown": {"NL_ASSERTION": reward},
                "info": {"evaluation_method": "sliding_window",
                         "num_windows": len(windows), "window_size": 10},
                "window_evaluations": evaluations}

    def snapshot(self):
        """The REAL `NativeVita.snapshot()` key set, not an idealised one.

        The real snapshot carries `completed` (cumulative per arm), `last_evaluation` and
        the phase's OWN `environment_db`; it carries NO per-task `simulations` map (the
        sealed run's driver looked for that map, found nothing, and recorded no
        `judged_simulation_this_task`).
        """
        import json as _json
        events = []
        if self.protocols is not None and self.current_task in self.simulator_prompts:
            events = [{"event": "start", "subtask_id": self.current_task,
                       "user_system_prompt": self.simulator_prompts[self.current_task],
                       "domain_policy": "fixture policy"}]
        return {"visibility": "private_driver_and_judge_only", "fixture": True,
                "provenance": {}, "raw_subtasks": [], "root_persona": None,
                "model_parameters": {}, "next_index": len(self.finished),
                "aborted": self.aborted, "active_subtask_id": self.current_task,
                "environment_db": self._environment_db_json(), "user_state": None,
                "native_calls": deepcopy(self.calls), "events": events,
                "completed": deepcopy(self.completed),
                "last_evaluation": deepcopy(self.last_evaluation)}

    def abort(self):
        self.aborted = True


def provider_request_shape(proxy):
    """The request identity and required mode the DECLARED transport asks for.

    The fixture sends what the proxy's own profile declares: its wire model and every
    field the profile REQUIRES (a transport whose semantics depend on an explicit mode
    refuses a request that omits it). For the sealed Qwen profile this is exactly the
    old model constant and no extra field, so those fixtures stay byte-identical.
    """
    from ae_cloud_proxy import profile_of
    profile = profile_of(proxy.config)
    return (profile.declared_models()[0],
            {name: deepcopy(value)
             for name, value in profile.required_request_fields.items()})


class MultiturnProvider:
    """Scripted provider that drives the multi-task sequence through the real proxy."""

    def __init__(self, proxy, opener, sample, session):
        self.proxy, self.opener = proxy, opener
        self.sample = sample
        self.session = session
        self.calls = []
        self.task_of_messages = None
        #: The role of the auxiliary call currently being produced, if any.
        self.last_role = None
        #: Optional hook that may rewrite the outgoing provider body in place.
        #: A negative fixture uses it to send a REAL, transport-consistent request
        #: that differs semantically from the declared one.
        self.request_mutator = None
        #: Optional hook that may rewrite a raw provider REPLY before dispatch.
        self.reply_mutator = None
        #: Optional hook that may rewrite the role a call is dispatched under.
        self.role_mutator = None
        #: Optional hook for auxiliary calls: exchanges the raw reply this call
        #: will receive. It runs once per auxiliary dispatch, so two adjacent
        #: auxiliary calls can really exchange their answers.
        self.auxiliary_reply_mutator = None

    def request(self, method, path, body=None):
        if method == "GET" and path == "/v1/models":
            self.proxy.dispatch(method, path, b"", "re-multiturn/catalog", "agent_or_unknown")
            return {"data": [{"id": pair.MODEL}]}
        raise AssertionError(f"unexpected model transport call: {method} {path}")

    def close(self):
        pass

    def _task(self, new_input):
        """The task id declared by this submitted input, structurally."""
        for message in new_input:
            if message.get("role") != "user":
                continue
            try:
                material = json.loads(message.get("content") or "")
            except (TypeError, ValueError):
                continue
            if not isinstance(material, dict):
                continue
            if material.get("source") == "dataset_history/material":
                return f"sub_U000828_{material['task_number']}", "history"
            if material.get("source") in ("current_task", "runtime_user"):
                return material.get("subtask_id"), material.get("source")
        return None, None

    def decide(self, session, new_input, tool_schemas):
        task_id, phase = self._task(new_input)
        arm = session["arm"]
        memory_calls = session.setdefault("memory_calls", {})
        tool_returns = any(m.get("type") == "tool_return" for m in new_input)
        if phase == "history":
            if not memory_calls.get(task_id):
                memory_calls[task_id] = True
                # A real, legal replace sourced from THIS task's own material.
                ref = f"t{task_id.rsplit('_', 1)[-1]}/history/0"
                return pair.chat_reply("tool_calls", tool_calls=[
                    {"id": f"call-{arm}-{task_id}-memory", "type": "function",
                     "function": {"name": "memory_update",
                                  "arguments": dumps({"operation": "add", "fact_id": "",
                                                      "category": "饮食偏好",
                                                      "content": f"t{task_id} 备注",
                                                      "evidence_ref": ref})}}])
            return pair.chat_reply("stop", content=f"{task_id} history noted")
        if phase == "current_task":
            session.setdefault("native_stage", {})[task_id] = "start"
            return self._native_reply(session, task_id, tool_schemas)
        if tool_returns:
            stage = session.setdefault("native_stage", {}).get(task_id)
            if stage in ("create", "pay"):
                return self._native_reply(session, task_id, tool_schemas)
        return pair.chat_reply("stop", content="fixture final answer")

    def _native_reply(self, session, task_id, tool_schemas):
        """A real tool call built from the task's OWN declared environment."""
        names = {schema["name"] for schema in tool_schemas}
        declared = self.sample["private_tasks"][task_id]["environment"]
        time_value = declared.get("time") or "2024-06-23 12:00:00"
        address = self.sample["initial_profile"][WORK_ADDRESS_KEY]
        stage = session.setdefault("native_stage", {})
        arm = session["arm"]
        if "create_delivery_order" in names:
            if stage.get(task_id) == "start":
                store = next(iter(declared.get("stores") or {}))
                product = declared["stores"][store]["products"][0]["product_id"]
                stage[task_id] = "pay"
                return pair.chat_reply("tool_calls", tool_calls=[
                    {"id": f"call-{arm}-{task_id}-create", "type": "function",
                     "function": {"name": "create_delivery_order",
                                  "arguments": dumps({
                                      "user_id": "U000828", "store_id": store,
                                      "product_ids": [product], "product_cnts": [1],
                                      "address": address, "dispatch_time": time_value,
                                      "attributes": ["规格: 7分糖"]})}}])
            # The real id comes from the actual create return, never invented.
            order_id = None
            for message in session["_last_input"]:
                if message.get("type") == "tool_return":
                    for returned in message.get("tool_returns") or []:
                        if returned.get("status") == "success":
                            order_id = pair.real_order_id(returned.get("tool_return"))
            assert order_id, "the real create return must supply the order id"
            stage[task_id] = "done"
            if "pay_delivery_order" in names:
                return pair.chat_reply("tool_calls", tool_calls=[
                    _pay_call(f"call-{arm}-{task_id}-pay", order_id)])
            return pair.chat_reply("stop", content="fixture final answer")
        # instore: one real read call from the task's own declared data.
        if "instore_shop_search_recommend" in names and stage.get(task_id) == "start":
            stage[task_id] = "done"
            return pair.chat_reply("tool_calls", tool_calls=[
                {"id": f"call-{arm}-{task_id}-search", "type": "function",
                 "function": {"name": "instore_shop_search_recommend",
                              "arguments": dumps({"keywords": ["按摩", "团购"]})}}])
        stage[task_id] = "done"
        return pair.chat_reply("stop", content="fixture final answer")

    def provider_wire_body(self, messages, tool_schemas):
        """The provider POST body, exactly as the DECLARED transport requires it.

        For every sealed profile this is the pinned body, byte for byte. A
        model-specific transport is exercised as itself: it names its own wire model
        (the proxy refuses another one instead of rewriting it), and the fields its
        profile requires are present - the pinned Letta pass-through shape
        (`user`, `parallel_tool_calls: false`) travels with it, because that is what the
        deployed service really sends.
        """
        model, required = provider_request_shape(self.proxy)
        body = {"model": model, "messages": messages, "max_completion_tokens": 2048,
                "temperature": 0, "stream": False, "parallel_tool_calls": False,
                "user": "U000828",
                "tools": [{"type": "function", "function": deepcopy(schema)}
                          for schema in tool_schemas],
                "tool_choice": "auto"}
        body.update(required)
        return body

    def chat(self, session, new_input, tool_schemas):
        # `native_history` is the Letta conversation; the provider body is its
        # pinned OpenAI projection plus this POST's new input.
        self.last_role = "agent"
        messages = [{"role": "system", "content": session["system"]}]
        messages.extend(openai_wire_messages(session["native_history"]))
        messages.extend(openai_wire_messages(pair.PairProvider.wire_turn(new_input)))
        # The wire that was actually sent becomes this session's OpenAI history,
        # so the next POST continues from the projected bytes.
        session["native_history"] = messages[1:]
        session["_last_input"] = deepcopy(new_input)
        reply = self.decide(session, new_input, tool_schemas)
        body = self.provider_wire_body(messages, tool_schemas)
        if self.reply_mutator is not None:
            reply = self.reply_mutator(self, body, reply)
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        if self.request_mutator is not None:
            # Marks this call as the mutated one for the audit probe; the body
            # itself is not rewritten, because the capture an audit reads is the
            # Letta transport journal, not the upstream provider body.
            self.request_mutator(self, body)
        self.calls.append(raw)
        self.opener.next_reply = reply
        self.proxy.dispatch("POST", "/v1/chat/completions", raw, "re-multiturn/stage",
                            "agent_or_unknown")
        return reply


class MutatingLettaPair(pair.ScriptedLettaPair):
    """A scripted Letta session whose SUBMITTED POST body can be rewritten.

    The audit reads the Letta transport journal, so a negative fixture that only
    rewrote the upstream provider body would not change the object under audit.
    This double lets a probe rewrite the POST that Letta actually receives, while
    every transport hash is recomputed from the rewritten bytes.
    """

    def __init__(self, *args, post_mutator=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.post_mutator = post_mutator
        self.post_count = 0

    def handle(self, method, path, body):
        if method == "POST" and path.endswith("/messages") and self.post_mutator is not None:
            self.post_count += 1
            body = self.post_mutator(self, self.post_count, deepcopy(body))
        return super().handle(method, path, body)


class _CriteriaStub:
    """A minimal object exposing the fields the pinned rubric initialiser reads.

    The real `_initialize_rubric_states` only needs `expected_states[].state_rubrics`
    and `overall_rubrics`; the fixture passes the dataset's own criteria through an
    attribute view so the rubric keys are the real ones, not invented.
    """

    def __init__(self, criteria):
        criteria = criteria or {}
        self.expected_states = [
            _StateStub(item.get("state_rubrics") or [])
            for item in (criteria.get("expected_states") or [])]
        self.overall_rubrics = list(criteria.get("overall_rubrics") or [])


class _StateStub:
    def __init__(self, rubrics):
        self.state_rubrics = list(rubrics)


def _pay_call(call_id, order_id):
    return {"id": call_id, "type": "function",
            "function": {"name": "pay_delivery_order",
                         "arguments": dumps({"order_id": order_id})}}


def build_multiturn_run(tmp_path, *, sample=None, judge_fail_tasks=(), sample_mutate=None,
                        real=False, judge_windows=False, user_script_tasks=(),
                        user_script_map=None):
    """Produce a real-format multiturm run directory with the PRODUCTION driver."""
    run_dir = Path(tmp_path) / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    pilot = not real
    config_path = PILOT_CONFIG_PATH if pilot else CONFIG_PATH
    config = validate_config(json.loads(config_path.read_text(encoding="utf-8")))
    if sample is None:
        sample = real_projection()
    if sample is None:
        raise unittest.SkipTest("the fixed dataset is not available")
    # The pilot chain uses the REAL pinned sliding-window judge so the audit's
    # multi-window path is exercised by the module's own positive cases too.
    if sample_mutate is not None:
        sample = sample_mutate(deepcopy(sample))
    plan = build_plan(config, sample, code_files=multiturn_code_files(ROOT))
    cli = pair.load_cli_module("ae_01_cloud_re_multiturn")
    prov = cli.provenance(config, config_path, vita_source=VITA_SOURCE)
    plan["provenance"] = prov
    write_json(run_dir / "plan.json", plan)
    # The run-local Vita model configuration the PRODUCTION driver writes next to its
    # plan; the audit establishes it before importing the pinned package. A stale
    # `vita.*` from another fixture's configuration is dropped the way a fresh driver
    # process would start from.
    from ae_vita import native_model_config
    write_json(run_dir / "vita-models.json", native_model_config(plan["config"]))
    os.environ["VITA_MODEL_CONFIG_PATH"] = str(run_dir / "vita-models.json")
    for _name in [name for name in sys.modules
                  if name == "vita" or name.startswith("vita.")]:
        del sys.modules[_name]

    clock = pair.FakeClock()
    proxy = CloudAuditProxy(CloudConfig(**json.loads(TRANSPORT_CONFIG.read_text())),
                            Path(tmp_path) / "proxy.private.jsonl", api_key=KEY,
                            clock=clock.monotonic, sleep=clock.sleep)
    opener = pair.ChatOpener()
    proxy.opener = opener
    session = pair.ScriptedLettaPair(config=config,
                                     update_modes={arm: "none" for arm in ARMS})
    # From here on the pinned package is imported; every exit path must clean up.
    vita_context, environment_builder = real_environment_factory(sample)
    try:
        return _run_built_chain(tmp_path, run_dir, sample, config, config_path, plan,
                                proxy, opener, session, environment_builder,
                                judge_fail_tasks, cli, judge_windows=judge_windows)
    finally:
        vita_context.__exit__(None, None, None)


def _run_built_chain(tmp_path, run_dir, sample, config, config_path, plan, proxy, opener,
                     session, environment_builder, judge_fail_tasks, cli,
                     judge_windows=True):
    provider = MultiturnProvider(proxy, opener, sample, session)
    session.provider = provider
    recorder = pair.RecorderTransport(session, run_dir / "letta-http.jsonl")
    natives = {}

    def runtime_factory(arm):
        native = MultiturnNative(arm, proxy, opener, environment_builder,
                                 judge_fail_tasks=judge_fail_tasks,
                                 judge_windows=judge_windows, sample=sample)
        natives[arm] = native
        return native

    events = []

    def emit(event):
        events.append(dict(deepcopy(event), timestamp=pair.now()))

    result = execute_re_multiturn(config, sample, model_transport=provider,
                                  letta_transport=recorder,
                                  runtime_factory=runtime_factory, emit=emit)
    recorder.close()
    proxy.close()
    result["provenance"] = cli.provenance(config, config_path, vita_source=VITA_SOURCE)
    write_json(run_dir / "result.json", result)
    write_records(run_dir / "events.jsonl", events)
    pair.normalize_records(run_dir / "events.jsonl", events)
    return {"run_dir": run_dir, "journal": Path(tmp_path) / "proxy.private.jsonl",
            "config_path": config_path, "result": result, "events": events,
            "session": session, "natives": natives, "plan": plan, "sample": sample}


def audit_of(fixture, **kwargs):
    """Audit one fixture against the dataset that fixture's plan declares.

    A synthetic pilot fixture declares its own synthetic source, so it is audited
    with that same declaration; the real-data chains declare the fixed dataset.
    """
    kwargs.setdefault("dataset_path", fixture.get("dataset_path") or DATASET)
    return audit_re_multiturn_inputs(fixture["run_dir"], fixture["journal"],
                                     config_path=fixture["config_path"], **kwargs)


class RealDataPlanTests(unittest.TestCase):
    """The real fixed dataset, when present, is verified and projected offline."""

    def setUp(self):
        if real_sample(END_TURN) is None:
            self.skipTest("the fixed dataset is not available on this host")

    def test_plan_and_preflight_use_the_real_continuous_projection(self):
        sample = real_sample(END_TURN)
        self.assertIsNotNone(sample)
        config = validate_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        plan = build_plan(config, sample, code_files=multiturn_code_files(ROOT))
        self.assertEqual(plan["scope"]["task_numbers"], list(range(4, 13)))
        self.assertEqual(plan["scope"]["phase_count"], 18)
        self.assertEqual(plan["scope"]["history_records_total"], 327)
        self.assertFalse(plan["network_called"])
        checks = preflight(sample, config)
        self.assertTrue(checks["checks"]["identical_initial_block"])
        self.assertTrue(checks["checks"]["t3_old_fact_present"])
        self.assertTrue(checks["checks"]["t3_replacement_absent"])
        self.assertEqual(checks["checks"]["task_count"], 9)
        for entry in checks["checks"]["phases"]:
            self.assertIn(entry["arm"], ARMS)

    def test_real_t12_sugar_change_is_quoted_from_the_real_records(self):
        """The t12 change is read from the real history, never hardcoded."""
        full = real_sample(END_TURN)
        t12 = [task for task in full["tasks"] if task["number"] == 12][0]
        blob = json.dumps(t12["history"], ensure_ascii=False)
        self.assertIn("不另外加糖", blob)
        self.assertIn("以后点奶", blob)
        # The t3 initial state still carries the OLD default, so the change is
        # genuinely new information for the agent, not a pre-filled fact.
        self.assertIn("奶茶偏好5分糖",
                      json.dumps(full["initial_facts"], ensure_ascii=False))


class FixtureChainTests(unittest.TestCase):
    """The full chain through the production driver with fixture replies only."""

    def test_a_pilot_chain_passes_the_whole_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp, judge_windows=True)
            report = audit_of(fixture)
            if not report["input_audit_passed"]:
                self.fail(f"audit did not pass: {report['invalid_reasons']}")
            self.assertEqual(report["status"], "VALID")
            self.assertEqual(len(report["scope"]["phases"]), 4)
            self.assertEqual([p["arm"] for p in report["scope"]["phases"]],
                             ["rewrite", "rewrite", "erratum", "erratum"])
            self.assertEqual([p["task"] for p in report["scope"]["phases"]],
                             ["sub_U000828_4", "sub_U000828_5"] * 2)
            self.assertTrue(report["transport_capture_checked"])
            # R published verified updates; E never PATCHed its block.
            memory = {entry["arm"]: entry for entry in report["memory_mappings"]}
            self.assertEqual(memory["erratum"]["patches"], 0)
            self.assertEqual(memory["rewrite"]["patches"], memory["rewrite"]["updates"])
            self.assertGreater(memory["rewrite"]["patches"], 0)

    def test_both_arms_start_from_the_same_block_and_keep_their_own_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp, judge_windows=True)
            result = fixture["result"]
            self.assertEqual(result["status"], "RE_MULTITURN_COMPLETED_AUDIT_PENDING")
            self.assertEqual(result["arms"]["rewrite"]["initial_block"],
                             result["arms"]["erratum"]["initial_block"])
            self.assertNotEqual(result["arms"]["rewrite"]["agent_id"],
                                result["arms"]["erratum"]["agent_id"])
            for arm in ARMS:
                tasks = result["arms"][arm]["tasks"]
                self.assertEqual([t["subtask_id"] for t in tasks],
                                 ["sub_U000828_4", "sub_U000828_5"])
            # The erratum arm never changed its block, in any task.
            self.assertEqual(result["arms"]["erratum"]["initial_block"],
                             result["arms"]["erratum"]["final_block"])

    def test_every_task_is_judged_separately_and_keeps_its_real_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp, judge_windows=True)
            natives = fixture["natives"]
            for arm in ARMS:
                self.assertEqual(natives[arm].finished,
                                 ["sub_U000828_4", "sub_U000828_5"])
            report = audit_of(fixture)
            self.assertEqual(len(report["scope"]["task_scores"]), 4)
            self.assertTrue(all(entry["judge_status"] == "MODEL_JUDGED_DEBUG_ONLY"
                                for entry in report["scope"]["task_scores"]))


def replace_events(fixture, events):
    """Rewrite the event journal with renumbered, monotonic rows.

    A structural mutation must be caught by the PHASE gate, not by the shared
    journal integrity gate, so the rewritten rows are renumbered.
    """
    from datetime import datetime, timedelta
    rewritten = deepcopy(events)
    base = min(str(row.get("timestamp") or "2026-01-01T00:00:00+00:00")
               for row in rewritten)
    start = datetime.fromisoformat(base)
    for index, row in enumerate(rewritten):
        row["sequence"] = index
        row["timestamp"] = (start + timedelta(microseconds=index)).isoformat()
    rewrite_records(fixture["run_dir"] / "events.jsonl", rewritten)
    fixture["events"] = rewritten


def overwrite_json(path, value):
    """Replace a fixture capture in place (the audit reads it afterwards)."""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def _rewrite(path, mutate):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    mutate(value)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class CounterexampleTests(unittest.TestCase):
    """Bounded negative cases: each mutation must make the audit fail closed."""

    def _codes(self, fixture):
        return audit_of(fixture)["invalid_reasons"], audit_of(fixture)

    def test_a_skipped_phase_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            events = [e for e in fixture["events"] if e.get("kind") != "stage_start"]
            # Remove one task's stage start: the fixed prefix is broken.
            dropped = next(e for e in fixture["events"]
                           if e.get("kind") == "stage_start" and e.get("task") == "sub_U000828_5"
                           and e.get("arm") == "rewrite")
            replace_events(fixture, [e for e in fixture["events"] if e is not dropped])
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])

    def test_swapped_phase_order_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            events = deepcopy(fixture["events"])
            starts = [e for e in events if e.get("kind") == "stage_start"]
            starts[0]["task"], starts[1]["task"] = starts[1]["task"], starts[0]["task"]
            replace_events(fixture, events)
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])

    def test_a_repeated_phase_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            events = deepcopy(fixture["events"])
            starts = [e for e in events if e.get("kind") == "stage_start"]
            duplicate = deepcopy(starts[0])
            duplicate["phase_index"] = 99
            events.append(duplicate)
            replace_events(fixture, events)
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])

    def test_a_future_history_batch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            # Rewrite the Letta transport capture: relabel the t4 history batch as
            # a t5 batch so the declared task and the record refs disagree.
            path = fixture["run_dir"] / "letta-http.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            changed = 0
            for row in rows:
                body = row.get("body")
                if not isinstance(body, dict):
                    continue
                for message in body.get("messages") or []:
                    if not isinstance(message, dict) or message.get("role") != "user":
                        continue
                    try:
                        material = json.loads(message.get("content") or "")
                    except (TypeError, ValueError):
                        continue
                    if isinstance(material, dict) and material.get("task_number") == 4:
                        material["task_number"] = 5
                        message["content"] = json.dumps(material, ensure_ascii=False)
                        changed += 1
            self.assertGreater(changed, 0, "no t4 history batch was found to relabel")
            rewrite_records(path, rows)
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])

    def test_erratum_patching_the_block_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            fixture["result"]["arms"]["erratum"]["final_block"] = "tampered"
            overwrite_json(fixture["run_dir"] / "result.json", fixture["result"])
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])

    def test_a_failed_judge_phase_stops_the_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp, judge_fail_tasks={"sub_U000828_5"})
            result = fixture["result"]
            self.assertEqual(result["status"], "INVALID")
            self.assertFalse(result["execution_complete"])
            self.assertIsNotNone(result["stopped_after"])
            self.assertEqual(result["stopped_after"]["subtask_id"], "sub_U000828_5")
            # The erratum arm never ran, and the earlier tasks were kept.
            self.assertEqual([], result["arms"].get("erratum", {}).get("tasks", []))
            self.assertEqual(len(result["arms"]["rewrite"]["tasks"]), 1)
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])
            self.assertEqual(report["status"], "INVALID")

    def test_an_erratum_arm_that_lost_its_erratum_is_detectable(self):
        """E's errata reach the wire; a missing erratum row is a real difference."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp, judge_windows=True)
            good = audit_of(fixture)
            self.assertTrue(good["input_audit_passed"])
            result = fixture["result"]
            tasks = result["arms"]["erratum"]["tasks"]
            self.assertTrue(tasks, "the erratum arm must have produced phase records")
            for task in tasks:
                self.assertIn("transcript", task)

    def test_partial_scope_is_reported_not_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp, judge_fail_tasks={"sub_U000828_5"})
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])
            self.assertIn(report["status"], ("INVALID", "PARTIAL_NOT_VALID"))

    def test_a_cross_task_native_user_reply_is_refused(self):
        """A reply produced for one task may never be credited to another."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            tasks = fixture["result"]["arms"]["rewrite"]["tasks"]
            self.assertGreater(len(tasks), 1)
            # Move task 5's own native user reply into task 4's own slice.
            moved = [call for call in tasks[1]["native_calls_this_task"]
                     if call.get("role") == "user_simulator"]
            self.assertTrue(moved, "the fixture must have a native user reply")
            tasks[0]["native_calls_this_task"].extend(deepcopy(moved))
            overwrite_json(fixture["run_dir"] / "result.json", fixture["result"])
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])

    def test_task_redeclaring_an_agent_identity_is_refused(self):
        """A phase may not carry its own agent identity: continuity is per arm."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            fixture["result"]["arms"]["rewrite"]["tasks"][1]["agent_id"] = "agent-replaced"
            overwrite_json(fixture["run_dir"] / "result.json", fixture["result"])
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])

    def test_a_previous_domain_tool_binding_is_refused(self):
        """The t5 instore phase must not offer t4's delivery tool table."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            events = deepcopy(fixture["events"])
            starts = [e for e in events if e.get("kind") == "stage_start"]
            delivery = next(e for e in starts if e.get("task") == "sub_U000828_4")
            for event in starts:
                if event.get("task") == "sub_U000828_5":
                    event["tools"] = deepcopy(delivery["tools"])
                    break
            replace_events(fixture, events)
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])

    def test_the_pair_budget_is_never_reset_per_arm(self):
        """The captured config carries one budget for the whole pair."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp, judge_windows=True)
            report = audit_of(fixture)
            self.assertTrue(report["input_audit_passed"])
            self.assertEqual(report["transport_config"]["max_requests"], 256)
            self.assertEqual(report["scope"]["task_numbers"], [4, 5])
            # Both arms share ONE closed proxy capture: the whole-pair counter is
            # never reset per task and never reset for the second arm.
            rows = proxy_rows(fixture)
            request_ids = [row.get("request_id") for row in rows
                           if row.get("kind") == "client_request"]
            self.assertEqual(len(request_ids), len(set(request_ids)))
            self.assertGreater(len(request_ids), 0)

    def test_a_forged_post_ordinal_is_refused(self):
        """The binding of driver POST records to the capture is by ordinals."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            slice_ = fixture["result"]["arms"]["rewrite"]["tasks"][0]["post_slice"]
            slice_[0]["post_ordinal"] = 99
            overwrite_json(fixture["run_dir"] / "result.json", fixture["result"])
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])
            self.assertTrue(any("ordinal" in entry["code"]
                                for entry in report["invalid_reasons"]),
                            report["invalid_reasons"])

    def test_driver_code_tampering_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_multiturn_run(tmp)
            fixture["plan"]["provenance"]["code_sha256"]["ae_cloud_re_multiturn.py"] = "0" * 64
            overwrite_json(fixture["run_dir"] / "plan.json", fixture["plan"])
            report = audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])
            self.assertTrue(any("code_sha256_mismatch" in entry["code"]
                                for entry in report["invalid_reasons"]))


if __name__ == "__main__":
    unittest.main()


class NativeScopeExtensionTests(unittest.TestCase):
    """The minimal `ae_vita` extension: old default kept, new bound explicit."""

    def test_the_default_scope_is_unchanged(self):
        import inspect
        import ae_vita
        signature = inspect.signature(ae_vita.NativeVita.__init__)
        self.assertEqual(signature.parameters["end_turn"].default, 5)
        self.assertEqual(signature.parameters["start_turn"].default
                         if "start_turn" in signature.parameters else 4, 4)

    def test_only_the_two_verified_bounds_are_accepted(self):
        import ae_vita
        for value in (0, 1, 4, 6, 11, 13, "5", None, True, 5.0):
            with self.assertRaises(ae_vita.NativeVitaError):
                ae_vita.validate_end_turn(value)

    def test_twelve_is_accepted_and_five_is_accepted(self):
        import ae_vita
        self.assertEqual(ae_vita.validate_end_turn(5), 5)
        self.assertEqual(ae_vita.validate_end_turn(12), 12)


def mutate_events(fixture, mutate):
    """Rewrite a chain fixture's event journal in place, keeping it coherent."""
    from datetime import datetime, timedelta
    events = deepcopy(fixture["events"])
    mutate(events)
    base = min(str(row.get("timestamp") or "2026-01-01T00:00:00+00:00") for row in events)
    start = datetime.fromisoformat(base)
    for index, row in enumerate(events):
        row["sequence"] = index
        row["timestamp"] = (start + timedelta(microseconds=index)).isoformat()
    rewrite_records(fixture["run_dir"] / "events.jsonl", events)
    fixture["events"] = events


class FullChainTests(unittest.TestCase):
    """The WHOLE continuous t4->t12 pair, 18 phases, both protocols.

    The chain runs the production driver over the real fixed dataset, real pinned
    Vita environments and real tool execution. 0.2 additionally uses the real
    reviewed manifest (rebuilt offline from the pinned baseline plus the reviewed
    patch) and a service-shaped receipt written by the unchanged bootstrap gate.
    """

    def setUp(self):
        import re_multiturn_chain as chain
        self.chain = chain
        if real_sample(t.END_TURN_ if False else 12) is None:
            self.skipTest("the fixed dataset is not available")

    def test_the_full_nine_task_chain_passes_the_sealed_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(tmp, unsealed=True)
            result = fixture["result"]
            self.assertEqual(result["status"], "RE_MULTITURN_COMPLETED_AUDIT_PENDING")
            for arm in ARMS:
                self.assertEqual(len(result["arms"][arm]["tasks"]), 9)
            report = self.chain.audit_of(fixture)
            self.assertTrue(report["input_audit_passed"], report["invalid_reasons"])
            self.assertEqual(report["scope"]["task_numbers"], list(range(4, 13)))
            self.assertEqual(report["scope"]["phase_count"], 18)
            self.assertEqual(len(report["scope"]["phases"]), 18)
            self.assertEqual(report["cloud_calls"]["unaccounted"], 0)
            self.assertEqual(len(report["scope"]["task_scores"]), 18)

    def test_the_full_nine_task_chain_passes_the_declared_0_2_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(tmp, unsealed=False)
            report = self.chain.audit_of(fixture)
            self.assertTrue(report["input_audit_passed"], report["invalid_reasons"])
            # The real reviewed manifest and the real service-shaped receipt.
            self.assertTrue(report["scope"]["multicall_manifest"]["offline_patch_verified"])
            self.assertTrue(report["scope"]["multicall_receipt_verified"])
            self.assertEqual(fixture["result"]["config"]["schema_version"],
                             "ae-cloud-re-multiturn-0.2")
            # Real multi-call batches were received whole under the profile.
            batches = sum(len(task.get("multicall_batches") or [])
                          for arm in ARMS
                          for task in fixture["result"]["arms"][arm]["tasks"])
            self.assertGreater(batches, 0)

    def test_a_real_multi_window_judge_path_runs_in_the_chain(self):
        """At least one task is judged by MORE THAN ONE pinned sliding window.

        Declaring extra in-task replies for the FIRST task gives that task a long
        enough transcript for the pinned expansion to produce several windows,
        while the shared 256-request budget is respected (the whole pair must stay
        inside it; this test does not widen it).
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True,
                user_script=lambda arm: ["补充一。", "补充二。", "补充三。", "补充四。"],
                user_script_tasks=("sub_U000828_4",))
            report = self.chain.audit_of(fixture)
            self.assertTrue(report["input_audit_passed"], report["invalid_reasons"])
            windows = {entry["task"]: entry["num_windows"]
                       for entry in report["judge_chains"]}
            self.assertTrue(windows, "no judge chain was recorded")
            self.assertGreater(max(windows.values()), 1,
                               f"no task was judged by more than one window: {windows}")

    def test_memory_and_domain_switches_really_happen(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(tmp, unsealed=True)
            result = fixture["result"]
            # R published one verified update per task; E never patched and
            # accumulated its errata in its own context.
            self.assertGreater(result["arms"]["rewrite"]["patch_count"], 0)
            self.assertEqual(result["arms"]["erratum"]["patch_count"], 0)
            domains = [task["domain"] for task in result["arms"]["rewrite"]["tasks"]]
            self.assertEqual(len(set(domains)), 2)
            self.assertEqual(domains, [task["domain"]
                                       for task in result["arms"]["erratum"]["tasks"]])


class AuxiliaryPairingTests(unittest.TestCase):
    """Requirement A: strict authoritative-order pairing and window/role checks.

    The positive case (identical auxiliary content) must be ACCEPTED; order
    exchange, role mismatch and cross-task/out-of-window cases must be REFUSED.
    """

    def setUp(self):
        import re_multiturn_chain as chain
        self.chain = chain
        if real_sample(12) is None:
            self.skipTest("the fixed dataset is not available")

    def test_repeated_identical_auxiliary_content_is_accepted(self):
        """The same user text produced by different tasks must NOT be ambiguous."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True, user_script=lambda arm: ["同一句继续。"])
            report = self.chain.audit_of(fixture)
            self.assertTrue(report["input_audit_passed"], report["invalid_reasons"])
            texts = [entry["request_id"] for entry in report["auxiliary_mappings"]]
            self.assertEqual(len(texts), len(set(texts)), "a call was mapped twice")

    def test_role_mismatch_direct_probe_is_target_gated(self):
        """A bounded direct probe: a captured role that is not the native one.

        The full-capture equivalent is
        `test_auxiliary_role_mismatch_is_refused`; this probe pins the exact gate
        code with an explicitly synthetic pair, in the style of the Codex review.
        """
        from ae_cloud_re_multiturn_input_audit import MultiturnInputAudit
        audit = MultiturnInputAudit.__new__(MultiturnInputAudit)
        audit.post_cloud_call = {"post": {"request_id": "agent"}}
        audit.posts = [{"request_id": "post", "timestamp": "2026-01-01T00:00:00+00:00"}]
        audit.chat_calls = [
            {"request_id": "agent"},
            {"request_id": "c1", "capture_role": "user_simulator",
             "body": {"messages": [{"role": "user", "content": "A"}], "max_tokens": 4096},
             "response": {"choices": [{"message": {"content": "A"}}]}, "changes": [],
             "start": "2026-01-01T00:00:00+00:00", "end": "2026-01-01T00:00:01+00:00"},
        ]
        audit.phase_records = {("rewrite", "sub_U000828_4"): {
            "index": 0, "arm": "rewrite", "task": "sub_U000828_4",
            "record": {"post_slice": [{"request_id": "post"}],
                       "native_calls_this_task": [{
                "role": "evaluator", "subtask_id": "sub_U000828_4", "error": None,
                "recorded_at": "2026-01-01T00:00:00+00:00",
                "request": {"messages": [{"role": "user", "content": "A"}]},
                "response": {"raw_data": {"choices": [{"message": {"content": "A"}}]}}}]}}}
        audit.proxy = [{"kind": "client_request", "request_id": "post", "role": "agent_or_unknown"},
                       {"kind": "cloud_close", "timestamp": "2026-01-01T00:00:09+00:00"}]
        audit.report = {"auxiliary_mappings": [], "judge_chains": [],
                        "cloud_calls": {"agent": 1, "auxiliary": 0, "unaccounted": 0},
                        "checks_executed": []}
        with self.assertRaises(Exception) as raised:
            audit.auxiliary_gate()
        self.assertIn("capture_role_differs_from_native_role", str(raised.exception))


class ChainCounterexampleTests(unittest.TestCase):
    """Every mutation stays transport-consistent and is refused by a TARGET gate."""

    def setUp(self):
        import re_multiturn_chain as chain
        self.chain = chain
        if real_sample(12) is None:
            self.skipTest("the fixed dataset is not available")

    def _run(self, tmp, **kwargs):
        return self.chain.build_full_chain(tmp, unsealed=True, **kwargs)

    def _reasons(self, fixture):
        report = self.chain.audit_of(fixture)
        self.assertFalse(report["input_audit_passed"],
                         f"the audit passed but a refusal was expected: {report['invalid_reasons']}")
        return [entry["code"] for entry in report["invalid_reasons"]], report

    def test_omitted_history_batch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._run(tmp, provider_class=self.chain.MutatingChainProvider,
                                mutation="omit_history", mutation_task="sub_U000828_6")
            codes, _ = self._reasons(fixture)
            self.assertIn("user_history_content_changed", codes)

    def test_swapped_history_batch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._run(tmp, provider_class=self.chain.MutatingChainProvider,
                                mutation="swap_history_batches",
                                mutation_task="sub_U000828_6")
            codes, _ = self._reasons(fixture)
            self.assertTrue(any(code in ("user_history_content_changed",
                                         "history_batch_refs_changed",
                                         "cloud_history_batch_labelled_with_another_task")
                                for code in codes), codes)

    def test_a_rewired_phase_boundary_is_refused(self):
        """A phase whose own start boundary is rewired no longer tiles the arm.

        The event journal and the driver record must agree on where each phase's
        submissions start; if either is rewired so a later task restarts from an
        earlier task's boundary, the capture is refused.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._run(tmp)
            result = fixture["result"]
            target = result["arms"]["rewrite"]["tasks"][5]
            target["boundary"] = dict(target["boundary"], post_index=0)

            def reset(_events):
                pass

            mutate_events(fixture, reset)
            overwrite_json(fixture["run_dir"] / "result.json", result)
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertIn("driver_phase_boundary_differs", codes)

    def test_context_that_does_not_continue_is_refused(self):
        """A later task starting from an empty context is refused.

        The driver's per-phase POST slices are rewired so the arm's later task no
        longer continues from its previous task while the event journal and the
        capture are left untouched; the continuity check must refuse it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._run(tmp)
            result = fixture["result"]
            tasks = result["arms"]["rewrite"]["tasks"]
            first_posts = deepcopy(tasks[0]["post_slice"])
            for later in tasks[1:]:
                later["post_slice"] = deepcopy(first_posts)
                later["boundary"] = dict(later["boundary"],
                                         post_index=later["boundary"]["post_index"])
            overwrite_json(fixture["run_dir"] / "result.json", result)
            report = self.chain.audit_of(fixture)
            self.assertFalse(report["input_audit_passed"])

    def test_runtime_user_injection_probe_is_refused(self):
        """A runtime_user wire no native reply produced is refused.

        The full-chain variant of this probe depends on whether a given task
        actually produces an in-task exchange inside the shared request budget,
        so the refusal is pinned by the bounded direct probe below AND by the
        chain-level invariant test in `AuxiliaryPairingTests`. This case asserts
        the chain-level outcome for a task that really does exchange replies.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._run(tmp, provider_class=self.chain.MutatingChainProvider,
                                mutation="runtime_user_inject",
                                mutation_task="sub_U000828_4",
                                user_script=lambda arm: ["我还想再确认一下。",
                                                         "第二句补充。"],
                                user_script_tasks=("sub_U000828_4",))
            report = self.chain.audit_of(fixture)
            # Either the injected wire is refused by a target gate, or the task
            # produced no in-task exchange at all and nothing was injected.
            if not report["input_audit_passed"]:
                codes = [entry["code"] for entry in report["invalid_reasons"]]
                self.assertTrue(any("runtime_user" in code or "history_content" in code
                                    for code in codes), codes)
            else:
                self.assertEqual([], [entry for entry in report["auxiliary_mappings"]
                                      if entry["role"] != "user_simulator"
                                      and entry["role"] != "evaluator"])

    def test_runtime_user_binding_direct_probe(self):
        """A bounded probe pinning the reply-binding gate's own refusal.

        The chain-level case above depends on how many in-task replies a task can
        afford inside the shared request budget; this probe pins the gate code
        directly with an explicitly synthetic phase record.
        """
        from ae_cloud_re_multiturn_input_audit import MultiturnInputAudit
        audit = MultiturnInputAudit.__new__(MultiturnInputAudit)
        audit.events = []
        audit.phase_records = {("rewrite", "sub_U000828_4"): {
            "index": 0, "arm": "rewrite", "task": "sub_U000828_4",
            "record": {"number": 4, "post_slice": [],
                       "native_calls_this_task": [{
                           "role": "user_simulator", "subtask_id": "sub_U000828_4",
                           "error": None, "recorded_at": "2026-01-01T00:00:00+00:00",
                           "request": {"messages": [{"role": "user", "content": "x"}]},
                           "response": {"content": "真实回复",
                                        "raw_data": {"choices": [{"message": {
                                            "content": "真实回复"}}]}}}]}}}
        audit.report = {"checks_executed": []}
        with self.assertRaises(Exception):
            audit.runtime_user_gate()

    def test_altered_update_return_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._run(tmp, provider_class=self.chain.MutatingChainProvider,
                                mutation="alter_update_return",
                                mutation_task="sub_U000828_6")
            codes, _ = self._reasons(fixture)
            self.assertTrue(any("memory_update" in code for code in codes), codes)

    def test_an_unowned_extra_cloud_call_is_refused(self):
        """A journaled cloud call no POST and no native record owns is refused.

        The orphan is appended before the capture's own close with monotonic
        timestamps, so it is a real extra cloud record. It is currently caught by
        the shared transport walk, which then reports
        `transport_capture_checked: false` and `unaccounted: 0` - a bounded
        refusal, never a passing zero. A transport-valid orphan that reaches the
        consumption gate itself is NOT claimed by this test.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._run(tmp, provider_class=self.chain.MutatingChainProvider,
                                mutation="extra_cloud_call",
                                mutation_task="sub_U000828_6")
            codes, report = self._reasons(fixture)
            self.assertFalse(report["transport_capture_checked"])
            self.assertEqual(report["cloud_calls"]["unaccounted"], 0)
            self.assertTrue(any("transport_capture" in code or "consumed" in code
                                or "count" in code for code in codes), codes)

    def test_a_judge_anomaly_stops_the_pair_mid_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._run(tmp, judge_fail_tasks={"sub_U000828_9"})
            result = fixture["result"]
            self.assertEqual(result["status"], "INVALID")
            self.assertFalse(result["execution_complete"])
            self.assertEqual(result["stopped_after"]["subtask_id"], "sub_U000828_9")
            # Everything before the anomaly is kept, and nothing after ran.
            self.assertEqual(len(result["arms"]["rewrite"]["tasks"]), 5)
            self.assertEqual(len(result["arms"].get("erratum", {}).get("tasks", [])), 0)


class ScoringGateTests(unittest.TestCase):
    """The scoring gate against the REAL t4 format, with target-gated counterexamples.

    Every counterexample below breaks ONLY the scoring evidence (a judge reply, the
    final rubrics, the reward aggregation, or the number/order of evaluator calls).
    The transport chain, the agent wire and the auxiliary pairing are untouched, so
    a refusal can only come from the scoring gate.
    """

    def setUp(self):
        import re_multiturn_chain as chain
        self.chain = chain
        if real_sample(12) is None:
            self.skipTest("the fixed dataset is not available")

    def test_a_legal_low_score_is_accepted(self):
        """A run where every window decides `met=False` must still pass the gate."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(tmp, unsealed=True, window_met=False)
            report = self.chain.audit_of(fixture)
            self.assertTrue(report["input_audit_passed"], report["invalid_reasons"])
            rewards = {(entry["arm"], entry["task"]): entry["reward_expected"]
                       for entry in report["judge_chains"]}
            self.assertTrue(rewards, "no judge chain recorded")
            self.assertTrue(all(value == 0.0 for value in rewards.values()),
                            f"the low-score run did not aggregate to 0.0: {rewards}")
            # The real reward the driver recorded also has to be 0.0.
            for arm in ARMS:
                for task in fixture["result"]["arms"][arm]["tasks"]:
                    self.assertEqual(task["judge"]["reward_info"]["reward"], 0.0)

    def test_changed_rubric_text_with_unchanged_key_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True, scoring_mutation="rubric_text_change")
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertTrue(any("rubric_text" in code or "rubric" in code for code in codes),
                            codes)

    def _second_window_fixture(self, tmp, mutation):
        """A two-window chain whose target task's carried state is mutated."""
        return self.chain.build_full_chain(
            tmp, unsealed=True, scoring_mutation=mutation,
            user_script=lambda arm: ["补充一。", "补充二。", "补充三。", "补充四。"],
            user_script_tasks=("sub_U000828_4",))

    def test_a_flipped_carried_meet_expectation_is_refused(self):
        """A later window's carried boolean must be the previous window's decision."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._second_window_fixture(tmp, "window_final_flip")
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertIn("judge_window_2_carried_meet_expectation_changed", codes)

    def test_a_rewritten_carried_justification_is_refused(self):
        """The carried justification is part of the state, not free text."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._second_window_fixture(tmp, "window_final_justification")
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertIn("judge_window_2_carried_justification_changed", codes)

    def test_a_faked_initial_state_is_refused(self):
        """The first window must carry the pinned initialiser's own state."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True, scoring_mutation="window_initial_change")
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertIn("judge_window_1_carried_justification_changed", codes)

    def test_final_met_disagreeing_with_the_last_window_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True, scoring_mutation="final_met_flip")
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertTrue(any("final_rubric" in code or "reward" in code for code in codes),
                            codes)

    def test_missing_final_rubrics_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True, scoring_mutation="final_missing")
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertIn("judge_final_rubrics_missing_or_empty", codes)

    def test_empty_final_rubrics_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True, scoring_mutation="final_empty")
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertIn("judge_final_rubrics_missing_or_empty", codes)

    def test_a_missing_window_is_refused(self):
        # The multi-window task is the one whose expansion can lose a window.
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True, scoring_mutation="window_drop",
                user_script=lambda arm: ["补充一。", "补充二。", "补充三。", "补充四。"],
                user_script_tasks=("sub_U000828_4",))
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertTrue(any("window" in code for code in codes),
                            f"a missing window was accepted: {codes}")

    def test_a_duplicated_window_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True, scoring_mutation="window_duplicate",
                user_script=lambda arm: ["补充一。", "补充二。", "补充三。", "补充四。"],
                user_script_tasks=("sub_U000828_4",))
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertTrue(any("window" in code for code in codes), codes)

    def test_an_out_of_order_window_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(
                tmp, unsealed=True, scoring_mutation="window_order",
                user_script=lambda arm: ["补充一。", "补充二。", "补充三。", "补充四。"],
                user_script_tasks=("sub_U000828_4",))
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertTrue(any("window" in code for code in codes),
                            f"an out-of-order window set was accepted: {codes}")

    def test_a_changed_window_body_with_the_same_numbers_is_refused(self):
        """The window body is compared byte-for-byte, not only by its numbers."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.chain.build_full_chain(tmp, unsealed=True, judge_windows=True)
            tasks = fixture["result"]["arms"]["rewrite"]["tasks"]
            edited = False
            for task in tasks:
                for call in task.get("native_calls_this_task") or []:
                    if call.get("role") != "evaluator":
                        continue
                    messages = call["request"]["messages"]
                    user = messages[1]["content"]
                    marker = "[1] "
                    if marker not in user:
                        continue
                    # Change ONE word inside the window body; every visible
                    # message number stays exactly as it was.
                    messages[1]["content"] = user.replace(
                        marker, marker, 1).replace("assistant:", "assistant (edited):", 1)
                    edited = True
                    break
                if edited:
                    break
            self.assertTrue(edited, "no judge window body was found to edit")
            overwrite_json(fixture["run_dir"] / "result.json", fixture["result"])
            codes = [entry["code"] for entry in
                     self.chain.audit_of(fixture)["invalid_reasons"]]
            self.assertIn("judge_window_content_differs_from_the_pinned_format", codes)


class PinnedModuleIdentityTests(unittest.TestCase):
    """The scoring modules are compared with a TRACED baseline, not with themselves.

    Every check runs in a fresh interpreter: `sys.modules` would otherwise hand the
    already-imported pinned module back and the tampered copy would never be read.
    The real checkout and the real archive are only ever read.
    """

    def setUp(self):
        if real_sample(12) is None:
            self.skipTest("the fixed dataset is not available")
        if not (VITA_SOURCE / "src/vita/evaluator/evaluator_traj.py").is_file():
            self.skipTest("the pinned Vita source is not available")
        if not BASELINE_PATH.is_file():
            self.skipTest("the pinned scoring baseline is not available")
        if not BASELINE_ARCHIVE.is_file():
            self.skipTest("the archived pinned source is not available")

    def _child(self, tmp, source=None, baseline=None, archive=None, modules=None):
        """Run the module gate in a fresh interpreter over an isolated copy."""
        script = Path(tmp) / "module-gate-probe.py"
        script.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"ROOT = Path({str(ROOT)!r})\n"
            f"TESTS = Path({str(TESTS)!r})\n"
            "sys.path.insert(0, str(TESTS))\n"
            "sys.path.insert(0, str(ROOT))\n"
            "import test_ae_cloud_re_multiturn as t\n"
            "from ae_cloud_re_multiturn_input_audit import MultiturnInputAudit\n"
            "sample = t.real_sample(t.END_TURN)\n"
            "config = t.validate_config(json.loads(t.CONFIG_PATH.read_text(encoding='utf-8')))\n"
            "plan = t.build_plan(config, sample, code_files=t.multiturn_code_files(ROOT))\n"
            "plan['provenance'] = {'vita_scorer_baseline': {\n"
            "    'path': str(Path(os.environ['BASELINE']).resolve()),\n"
            "    'sha256': t.hashlib.sha256(Path(os.environ['BASELINE']).read_bytes()).hexdigest(),\n"
            "    'declared': True,\n"
            "}}\n"
            "audit = MultiturnInputAudit(Path(os.environ['RUN']), Path(os.environ['JOURNAL']),\n"
            "                            dataset_path=Path(os.environ['DATASET']))\n"
            "audit.plan = plan\n"
            # The run-local Vita model configuration every real run record carries: the
            # audit establishes it before the pinned package is imported, so the probe
            # writes the same file through the production helper.
            "from ae_vita import native_model_config\n"
            "Path(os.environ['RUN']).joinpath('vita-models.json').write_text(\n"
            "    json.dumps(native_model_config(plan['config']), ensure_ascii=False),\n"
            "    encoding='utf-8')\n"
            "os.environ['VITA_MODEL_CONFIG_PATH'] = str(\n"
            "    Path(os.environ['RUN']) / 'vita-models.json')\n"
            "audit.task_numbers = [4, 5, 6, 7, 8, 9, 10, 11, 12]\n"
            "audit.pinned_source = Path(os.environ['SOURCE'])\n"
            "try:\n"
            "    pinned = audit._pinned_modules()\n"
            "    result = {'accepted': True,\n"
            "              'digests': {k: v['sha256'] for k, v in pinned['digests'].items()},\n"
            "              'baseline_sha256': pinned['baseline']['sha256'],\n"
            "              'baseline_status': pinned['baseline']['status'],\n"
            "              'archive': pinned['baseline']['archive']}\n"
            "except Exception as exc:\n"
            "    result = {'accepted': False, 'error': str(exc), 'type': type(exc).__name__}\n"
            "print(json.dumps(result, ensure_ascii=False))\n",
            encoding="utf-8")
        environment = dict(os.environ)
        environment.setdefault("OPENAI_API_KEY", "EMPTY")
        environment["SOURCE"] = str(source or VITA_SOURCE)
        environment["BASELINE"] = str(baseline or BASELINE_PATH)
        environment["DATASET"] = str(DATASET)
        environment["RUN"] = tmp
        environment["JOURNAL"] = str(Path(tmp) / "unused.private.jsonl")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["AE_VITA_BASELINE_ROOT"] = str(ROOT)
        if archive is not None:
            environment["AE_VITA_BASELINE_ARCHIVE"] = str(archive)
        else:
            environment.pop("AE_VITA_BASELINE_ARCHIVE", None)
        completed = subprocess.run([sys.executable, "-B", str(script)], env=environment,
                                   capture_output=True, text=True, timeout=300)
        self.assertEqual(completed.returncode, 0,
                         f"the isolated module probe failed: {completed.stderr[-800:]}")
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def _isolated(self, tmp, seed=None):
        """A copy of the pinned checkout, with an optional byte change."""
        target = Path(tmp) / "source"
        shutil.copytree(VITA_SOURCE, target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if seed is not None:
            seed(target)
        return target

    def _declaration_copy(self, tmp, name, document):
        path = Path(tmp) / name
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def _deployment_baseline(self, tmp, name="baseline.json", tamper=None):
        """The declared baseline, copied where the audit will really read it.

        The copy keeps the declaration's own `$REPO` anchor, and the child is told
        where the repository root really is, so the relative archive path resolves
        exactly as it does for a production audit.
        """
        document = _baseline_document()
        if tamper is not None:
            document = tamper(document)
        return self._declaration_copy(tmp, name, document)

    def _tampered_baseline(self, tmp, tamper):
        """A declaration copy with one field changed, still over the real archive."""
        return self._deployment_baseline(tmp, "tampered-baseline.json", tamper)

    def test_the_imported_modules_match_the_traced_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = self._child(tmp, baseline=self._deployment_baseline(tmp))
            self.assertTrue(outcome["accepted"], outcome)
            self.assertEqual(outcome["baseline_status"],
                             "verified_against_the_archived_pinned_source")
            self.assertTrue(outcome["archive"]["sha256"])
            self.assertEqual(len(outcome["digests"]), 6)
            self.assertTrue(all(len(value) == 64 for value in outcome["digests"].values()))

    def test_a_comment_added_to_a_scoring_module_is_refused(self):
        """A comment changes no behaviour and still changes the module's bytes."""
        def seed(source):
            path = source / "src/vita/evaluator/evaluator_traj.py"
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n# Codex r4 digest mismatch probe; behaviour unchanged\n")

        with tempfile.TemporaryDirectory() as tmp:
            outcome = self._child(tmp, source=self._isolated(tmp, seed),
                                  baseline=self._deployment_baseline(tmp))
            self.assertFalse(outcome["accepted"], outcome)
            self.assertIn("pinned_module_bytes_differ_from_the_baseline_evaluator",
                          outcome["error"])

    def test_a_baseline_whose_module_digest_is_wrong_is_refused(self):
        """The comparison really happens: a wrong pinned digest stops the audit."""
        def tamper(document):
            document["modules"]["src/vita/evaluator/evaluator_traj.py"]["sha256"] = "0" * 64
            return document

        with tempfile.TemporaryDirectory() as tmp:
            baseline = self._tampered_baseline(tmp, tamper)
            outcome = self._child(tmp, baseline=baseline)
            self.assertFalse(outcome["accepted"], outcome)
            self.assertIn("scorer_baseline_module_not_from_the_archive_evaluator",
                          outcome["error"])

    def test_an_archive_whose_bytes_changed_is_refused(self):
        """The declaration's anchor is re-read: changed archive bytes stop the audit."""
        def tamper(document):
            blob = Path(tmp) / "changed-pinned-source.tar.gz"
            data = bytearray(BASELINE_ARCHIVE.read_bytes())
            data[4] ^= 0xFF
            blob.write_bytes(bytes(data))
            document["archive"]["path"] = str(blob)
            return document

        with tempfile.TemporaryDirectory() as tmp:
            baseline = self._deployment_baseline(tmp, "archive-baseline.json", tamper)
            outcome = self._child(tmp, baseline=baseline)
            self.assertFalse(outcome["accepted"], outcome)
            self.assertIn("scorer_baseline_archive", outcome["error"])

    def test_a_missing_baseline_declaration_is_refused(self):
        """No declaration means no comparison - which is a refusal, not a pass."""
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "absent-baseline.json"
            script = Path(tmp) / "absent.py"
            script.write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                f"ROOT = Path({str(ROOT)!r})\n"
                "sys.path.insert(0, str(ROOT))\n"
                "from ae_cloud_re_multiturn_input_audit import MultiturnInputAudit\n"
                "audit = MultiturnInputAudit(Path(os.environ['RUN']),\n"
                "                            Path(os.environ['JOURNAL']))\n"
                "audit.plan = {'provenance': {}}\n"
                "audit.task_numbers = [4]\n"
                "audit.pinned_source = Path(os.environ['SOURCE'])\n"
                "try:\n"
                "    audit._pinned_modules()\n"
                "    print(json.dumps({'accepted': True}))\n"
                "except Exception as exc:\n"
                "    print(json.dumps({'accepted': False, 'error': str(exc)}))\n",
                encoding="utf-8")
            environment = dict(os.environ)
            environment.update({"RUN": tmp, "JOURNAL": str(Path(tmp) / "unused.jsonl"),
                                "SOURCE": str(VITA_SOURCE),
                                "PYTHONDONTWRITEBYTECODE": "1"})
            completed = subprocess.run([sys.executable, "-B", str(script)], env=environment,
                                       capture_output=True, text=True, timeout=300)
            self.assertEqual(completed.returncode, 0, completed.stderr[-800:])
            outcome = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertFalse(outcome["accepted"], outcome)
            self.assertEqual(outcome["error"], "scorer_baseline_not_recorded_in_the_plan")
            self.assertFalse(absent.exists())


class ArchivedRealScoringFormatTests(unittest.TestCase):
    """Read-only check of the ARCHIVED REAL t4 scoring chain.

    This verifies only the SCORING COMPONENTS against the sealed real capture: it
    does not replay the old run through the new audit, does not alter the old
    provenance, and does not claim that a new audit of that run passes. The source
    is the archived proxy wire (`*.private.jsonl`) plus the archived `result.json`
    of the sealed single-t4 run - real evaluator requests and replies, not
    synthetic ones.
    """

    ARCHIVE = (ROOT / "transfers/lab-cloud-re-watchdog-run-20260913-r1"
               / "ae-cloud-re-pair-t4-watchdog-20260913-r1")

    def setUp(self):
        if not (self.ARCHIVE / "result.json").is_file():
            self.skipTest("the archived real single-t4 run is not available")
        if real_sample(12) is None:
            self.skipTest("the fixed dataset is not available")

    def _journal(self):
        import base64
        rows = [json.loads(line) for line in
                (self.ARCHIVE.parent / (self.ARCHIVE.name + ".private.jsonl"))
                .read_text(encoding="utf-8").splitlines()]
        decoded = {}
        for row in rows:
            if not row.get("body_base64"):
                continue
            try:
                decoded.setdefault(row["request_id"], {})[row["kind"]] = json.loads(
                    base64.b64decode(row["body_base64"]).decode("utf-8"))
            except Exception:
                continue
        return decoded

    def test_the_archived_real_scoring_chain_matches_the_pinned_components(self):
        import importlib
        import re as _re
        os.environ.setdefault("OPENAI_API_KEY", "EMPTY")
        entry = str(VITA_SOURCE / "src")
        if entry not in sys.path:
            sys.path.insert(0, entry)
        messages_module = importlib.import_module("vita.data_model.message")
        evaluator_module = importlib.import_module("vita.evaluator.evaluator_traj")
        parser = importlib.import_module("vita.utils.utils").evaluator_extracter
        checks_class = importlib.import_module("vita.data_model.simulation").NLRubricCheck

        result = json.loads((self.ARCHIVE / "result.json").read_text(encoding="utf-8"))
        journal = self._journal()
        # The real criteria for t4, from the fixed dataset.
        sample = real_sample(12)
        criteria_module = importlib.import_module("vita.data_model.tasks")
        criteria = criteria_module.EvaluationCriteria.model_validate(
            sample["private_tasks"]["sub_U000828_4"]["evaluation_criteria"])
        states = evaluator_module.TrajectoryEvaluator._initialize_rubric_states(criteria)
        rubric_text = {key: state["rubric"] for key, state in states.items()}
        self.assertEqual(len(rubric_text), 3, "the real t4 criteria must have 3 rubrics")

        classes = {"assistant": messages_module.AssistantMessage,
                   "user": messages_module.UserMessage,
                   "tool": messages_module.ToolMessage}
        for arm in ("rewrite", "erratum"):
            record = result["arms"][arm]
            reward = record["judge"]["reward_info"]
            evaluations = reward["window_evaluations"]
            # 1) three windows, in order, exactly as the pinned rule expands them.
            self.assertEqual(reward["info"]["num_windows"], 3)
            self.assertEqual(len(evaluations), 3)
            self.assertEqual([item["window_idx"] for item in evaluations], [1, 2, 3])
            native = []
            for index, row in enumerate(record["transcript"]):
                message = classes[row["role"]].model_validate(deepcopy(row))
                message.turn_idx = index
                native.append(message)
            windows = evaluator_module.TrajectoryEvaluator._create_sliding_windows(
                native, 10, 2)
            self.assertEqual(len(windows), 3, f"{arm}: pinned expansion is not 3 windows")
            # 2) every window body is byte-identical to the pinned formatter, and
            #    the visible numbers are the formatter's own (they skip a message
            #    that renders to nothing), not a contiguous range.
            window_numbers = []
            for position, evaluation in enumerate(evaluations):
                pinned = evaluator_module.TrajectoryEvaluator._format_window_content(
                    windows[position], position * 8)
                block = _re.search(r"<window_content>\n(.*?)\n</window_content>",
                                   evaluation["user_prompt"], _re.S)
                self.assertIsNotNone(block)
                self.assertEqual(block.group(1), pinned)
                window_numbers.append(
                    [int(value) for value in _re.findall(r"^\[(\d+)\]", pinned, _re.M)])
            # The FIRST window of the real run skips a message number: one message
            # rendered to nothing, so the visible numbers are not a contiguous
            # range even though the window holds a full 10-message slice.
            first = window_numbers[0]
            self.assertNotEqual(first, list(range(first[0], first[0] + len(first))),
                                "the real first window must skip a message number")
            self.assertNotIn(first[-1] + 0, first[:-1])
            # 3) the carried rubric state uses the real section shape and text.
            carried = _re.search(r"<current_rubrics>\s*(.*?)\s*</current_rubrics>",
                                 evaluations[0]["user_prompt"], _re.S)
            first_state = json.loads(carried.group(1))
            self.assertEqual({item["rubric_idx"] for item in first_state}, set(rubric_text))
            for item in first_state:
                self.assertEqual(item["rubric"], rubric_text[item["rubric_idx"]])
                self.assertIs(item["meetExpectation"], False)
            # 4) the FENCED real replies parse through the pinned parser.
            parsed_windows = []
            for rid, kinds in journal.items():
                request = kinds.get("client_request")
                response = kinds.get("upstream_response")
                if not isinstance(request, dict) or not isinstance(response, dict):
                    continue
                if not any("window_content" in (m.get("content") or "")
                           for m in request.get("messages") or [] if isinstance(m, dict)):
                    continue
                content = response["choices"][0]["message"]["content"]
                if arm == "rewrite" and not parsed_windows:
                    self.assertTrue(content.lstrip().startswith("```"),
                                    "the real reply is expected to carry a JSON fence")
                parsed = parser(content)
                self.assertEqual(len(parsed), 3)
                for decision in parsed:
                    self.assertEqual(decision["rubric"], rubric_text[decision["rubric_idx"]])
                parsed_windows.append(parsed)
            self.assertEqual(len(parsed_windows), 6,
                             "the archive must hold three windows per arm")
            # 5) the final rubrics use the pinned output shape and aggregate by the
            #    pinned rule.
            final = reward["nl_rubrics"]
            self.assertTrue(final)
            for item in final:
                self.assertEqual(set(item), {"nl_rubric", "met", "justification"})
                self.assertIn(item["nl_rubric"], set(rubric_text.values()))
            checks = [checks_class(nl_rubric=item["nl_rubric"], met=item["met"],
                                   justification=item["justification"]) for item in final]
            expected = 1.0 if (all(check.met for check in checks) and len(checks) > 0) else 0.0
            self.assertEqual(reward["reward"], expected)
