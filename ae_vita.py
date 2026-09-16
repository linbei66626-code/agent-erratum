"""Native Vita components for the bounded AE-01 t4--t5 wiring run.

No upstream files are changed. This is a derived persistent-Letta driver, not
the native orchestrator or an official benchmark run. Native user/judge calls
are captured in memory; the caller must journal snapshot() to a private run.
Importing this module neither imports Vita nor accesses a model endpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

from ae_inputs import VITA_REVISION, prepare_sample
import ae_sim_eval_protocol as sim_eval
from ae_sim_eval_protocol import (SimulatorProtocolViolation, USER_SIMULATOR_PROTOCOL,
                                  USER_SIMULATOR_PROTOCOLS,
                                  new_orders_of, paid_orders_of,
                                  user_simulator_system_suffix, validate_protocol_bundle,
                                  validate_user_reply)


class NativeVitaError(RuntimeError):
    """A wiring/judging boundary failed; not a scientific task failure."""


#: The only accepted native execution bounds: the historical t4--t5 wiring scope
#: and the fixed continuous original-data t4--t12 scope. The start is never
#: caller-selectable, so a later gold snapshot can never become a start point.
NATIVE_END_TURN_BOUNDS = (5, 12)


def validate_end_turn(end_turn) -> int:
    """Validate the explicit native execution bound, or refuse it.

    A boolean, a string, a float, an unverified task count or any other value is
    refused; only the two inspected original-data bounds are accepted.
    """
    if type(end_turn) is not int or end_turn not in NATIVE_END_TURN_BOUNDS:
        raise NativeVitaError("end_turn must be the explicit wiring bound 5 or the "
                              "fixed continuous original-data bound 12")
    return end_turn


# Explicit whitelist of inspected local checkpoints for the derived driver. The
# default Qwen3-8B path is unchanged; Qwen3-4B-Instruct-2507 is added only for
# the separate single-task capability probe. A CLOUD transport does not use this
# list: its model is the one its own declared profile names (see
# `declared_transport`), so a model-specific provider is never served by a
# locally inspected checkpoint's name.
NATIVE_MODELS = ("Qwen3-8B", "Qwen3-4B-Instruct-2507")

#: The historical local path: no cloud profile at all, and no proxy egress.
LOCAL_TRANSPORT = "local-vllm"


def declared_transport(transport_profile):
    """The reviewed transport contract for a declared profile, or refuse.

    `local-vllm` returns None (the historical local path). Every other value must be a
    profile the PROXY table really declares - the sealed compatibility profile and the
    model-specific DeepSeek transport alike - and its wire model, its model set and the
    request fields it requires are read from THAT profile. Nothing is restated here, so
    this entry cannot drift from the transport the proxy actually enforces, and a new
    provider is wired by declaring it once rather than by copying a model name.
    """
    if transport_profile == LOCAL_TRANSPORT:
        return None
    from ae_cloud_proxy import PROFILES
    profile = PROFILES.get(transport_profile)
    if profile is None:
        raise NativeVitaError("unknown explicit transport profile")
    return profile


def native_model_choice(transport_profile, model_name, language="chinese"):
    """The model this run may use: the DECLARED transport's own, or the local whitelist.

    A cloud run is never served by a locally inspected checkpoint's name and a local run
    is never served by a provider's wire model, so a label swap in either direction is a
    refusal here rather than a request that reaches a different endpoint.
    """
    transport = declared_transport(transport_profile)
    allowed = transport.declared_models() if transport is not None else NATIVE_MODELS
    if model_name not in allowed or language != "chinese":
        raise NativeVitaError(
            "native model must be the declared transport's own model, or an inspected "
            "Qwen3 checkpoint on the local path, with Chinese prompts")
    return allowed


def native_model_config_entry(*, model, model_base, required_request_fields=None):
    """The ONE accepted run-local model entry, for the writer and the loader alike.

    It is the audited local egress (`name`/`base_url`/`api_key=EMPTY`) plus, when the
    declared transport requires them, that transport's own request fields under
    `extra_body` - the field the pinned Vita client merges into every native call. The
    historical profiles require nothing, so their entry stays byte-identical to the one
    earlier rounds wrote.
    """
    entry = {"name": model, "base_url": model_base, "api_key": "EMPTY"}
    required = {key: deepcopy(value)
                for key, value in (required_request_fields or {}).items()}
    if required:
        entry["extra_body"] = required
    return entry


def native_model_config(config):
    """The run-local `vita-models.json` for a DECLARED run config.

    Every CLI that starts a native driver writes this file, and it is built once - here -
    so the bytes a CLI writes are exactly the bytes `_load_native` accepts. The base URL
    is normalized by `_model_base` (the same normalization the loader compares against),
    and the transport's required fields are read from its declared profile rather than
    restated by each caller.
    """
    from ae_cloud_proxy import PROFILES
    profile = PROFILES.get(config.get("transport_profile") or LOCAL_TRANSPORT)
    required = dict(profile.required_request_fields) if profile is not None else {}
    return {"default": {}, "models": [native_model_config_entry(
        model=config["expected_model"], model_base=_model_base(config["model_origin"]),
        required_request_fields=required)]}


_ACTIVE = threading.local()
_CAPTURE_LOCK = threading.RLock()


def _plain(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, dict):
        value = {key: _plain(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        value = [_plain(item) for item in value]
    # Reject nonfinite numbers rather than serializing nonstandard raw JSON.
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _model_base(origin: str) -> str:
    p = urlsplit(origin)
    if (p.scheme != "http" or p.hostname != "127.0.0.1" or not p.port
            or p.username or p.password or p.query or p.fragment
            or p.path.rstrip("/") not in ("", "/v1")):
        raise NativeVitaError("model origin must be a credential-free loopback HTTP endpoint")
    return f"http://127.0.0.1:{p.port}/v1"


def _verify_source(source: Path) -> dict:
    def git(*args):
        result = subprocess.run(["git", "-C", str(source), *args], capture_output=True,
                                text=True, check=False, timeout=30)
        if result.returncode:
            raise NativeVitaError("fixed Vita checkout could not be verified")
        return result.stdout.strip()
    head = git("rev-parse", "HEAD")
    if head != VITA_REVISION or git("status", "--porcelain", "--untracked-files=no"):
        raise NativeVitaError("Vita HEAD must match the fixed revision with no tracked changes")
    return {"source": str(source), "git_head": head, "tracked_clean": True}


def _load_native(source: Path, config_path: Path, model_base: str, model: str,
                 required_request_fields=None):
    """Load only after a caller-created, dummy-key-only local config is checked.

    The accepted entry is built by `native_model_config_entry`, so a model-specific
    transport's required request fields (its mode) must be present in the run-local
    config as `extra_body`; a config that omits them, or adds anything else, is refused
    instead of being loaded and then silently sending a different request shape.
    """
    import yaml
    entry = native_model_config_entry(model=model, model_base=model_base,
                                      required_request_fields=required_request_fields)
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    expected = {"default": {}, "models": [entry]}
    if cfg != expected:
        raise NativeVitaError("Vita config must contain only the selected local model, "
                              "its declared request fields and api_key=EMPTY")
    package = source / "src/vita"
    for name, module in list(sys.modules.items()):
        if name == "vita" or name.startswith("vita."):
            filename = getattr(module, "__file__", None)
            if filename and not Path(filename).resolve().is_relative_to(package):
                raise NativeVitaError("another Vita checkout is already imported")
    sys.path.insert(0, str(source / "src"))
    config = importlib.import_module("vita.config")
    loaded_entry = {key: value for key, value in entry.items() if key != "name"}
    if (Path(config._models_yaml_path).resolve() != config_path
            or config.models != {"default": {}, model: loaded_entry}):
        raise NativeVitaError("already-loaded Vita model configuration differs from the audited config")
    modules = {name: importlib.import_module("vita." + path) for name, path in {
        "user_module": "user.personalization_user", "user_simulator": "user.user_simulator",
        "agent": "agent.base", "messages": "data_model.message", "tasks": "data_model.tasks",
        "personalization": "data_model.personalization_task", "simulation": "data_model.simulation",
        "orchestrator": "orchestrator.orchestrator", "registry": "registry", "prompts": "prompts",
        "utils": "utils.utils", "evaluator": "evaluator.evaluator", "judge_module": "evaluator.evaluator_traj",
    }.items()}
    return SimpleNamespace(**modules)


def clear_thread_registries_for(tasks_module=None):
    """Clear the pinned Vita thread-local store/product/location registries.

    Exposed as a module function so the same per-task isolation the native
    wrapper performs can be reproduced by an offline driver without constructing
    a full wrapper. `tasks_module` is the pinned `vita.data_model.tasks` module
    when the caller already holds it; otherwise it is looked up in `sys.modules`
    and nothing happens when the pinned package is not importable.
    """
    if tasks_module is None:
        tasks_module = sys.modules.get("vita.data_model.tasks")
    if tasks_module is None:
        return []
    classes = [getattr(tasks_module, attribute, None)
               for attribute in ("StoreBaseModel", "ProductBaseModel", "Location")]
    classes = [cls for cls in classes if cls is not None]
    for cls in classes:
        cls.clear_thread_data()
    return [cls.__name__ for cls in classes]


class NativeVita:
    def __init__(self, source_path, dataset_path, model_origin, model_name,
                 temperature, max_output_tokens, language="chinese", seed=300,
                 transport_profile="local-vllm", end_turn=5, protocols=None,
                 deterministic_evaluation="in_run"):
        # The transport contract comes from the DECLARED profile: the sealed compatibility
        # profile and the model-specific DeepSeek transport are both cloud egress through
        # the local proxy, while `local-vllm` stays the historical local path. Nothing is
        # disguised: the model must be one the declared profile names, and the fields the
        # profile requires travel with every native call.
        transport = declared_transport(transport_profile)
        cloud = transport is not None
        required_request_fields = (dict(transport.required_request_fields)
                                   if cloud else {})
        # Explicit, bounded scope only. The default preserves the historical
        # single-t4/t4--t5 wiring behaviour byte-for-byte; the sole new accepted
        # value is the fixed continuous original-data bound 12. The start is
        # never caller-selectable, so a later gold snapshot can never be used as
        # a fresh starting point.
        validate_end_turn(end_turn)
        native_model_choice(transport_profile, model_name, language)
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature) or not 0 <= temperature <= 2
                or type(max_output_tokens) is not int or max_output_tokens <= 0
                or type(seed) is not int):
            raise NativeVitaError("invalid explicit native model parameters")
        source = Path(source_path).resolve(strict=True)
        self._provenance = _verify_source(source)
        self._sample = prepare_sample(dataset_path, start_turn=4, end_turn=end_turn)
        dataset_bytes = Path(dataset_path).read_bytes()
        if hashlib.sha256(dataset_bytes).hexdigest() != self._sample["source"]["sha256"]:
            raise NativeVitaError("dataset changed after public projection verification")
        original = json.loads(dataset_bytes)[25]
        if original["user_profile"] != self._sample["initial_profile"]:
            raise NativeVitaError("root persona differs from the controlled t3 initial profile")
        # The executed subtasks are exactly the projected consecutive tasks, in
        # order. The t3 cutoff is fixed by prepare_sample; nothing here can start
        # from a later gold state.
        self._raw_subtasks = deepcopy(original["subtasks"][3:end_turn])
        self._persona = deepcopy(original["user_profile"])
        config_name = os.environ.get("VITA_MODEL_CONFIG_PATH")
        if not config_name:
            raise NativeVitaError("caller must set VITA_MODEL_CONFIG_PATH to a new run-local config")
        config_path = Path(config_name).resolve(strict=True)
        model_base = _model_base(model_origin)
        self._native = _load_native(source, config_path, model_base, model_name,
                                    required_request_fields=required_request_fields)
        self._provenance.update({"dataset": self._sample["source"],
                                 "executed_scope": {"start_turn": 4, "end_turn": end_turn,
                                                    "task_count": len(self._raw_subtasks),
                                                    "subtask_ids": [x["subtask_id"] for x in
                                                                    self._raw_subtasks]},
                                 "model_config_path": str(config_path),
                                 "model_config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
                                 "model_base": model_base, "model": model_name,
                                 "root_persona_equals_t3_profile": True})
        #: The declared transport's own required request fields (its mode). They are
        #: carried by every native call through the run-local model config, exactly like
        #: the agent path carries them through the proxy profile.
        self._required_request_fields = deepcopy(required_request_fields)
        self._language, self._model, self._seed = language, model_name, seed
        self._args = {"temperature": temperature, "max_tokens": max_output_tokens,
                      "seed": seed, "num_retries": 0}
        if cloud:
            self._args.pop("seed")
            self._provenance.update({
                "transport_profile": transport_profile,
                "generation_seed_sent": False,
                "simulation_seed_metadata_only": seed,
                # What the declared transport REQUIRES on the wire, recorded by field
                # name: the native calls carry exactly this and nothing is inferred.
                "declared_request_fields": sorted(required_request_fields)})
        self._subtasks = [self._native.personalization.SubTask.model_validate(x)
                          for x in self._raw_subtasks]
        # The declared repair protocols, or None. A declared simulator protocol is
        # enforced at the ONE seam where a user turn is produced (see `user_reply`),
        # and its versioned constraint block is appended to the NATIVE simulator's own
        # system prompt - the pinned prompt template is never replaced or edited.
        self._protocols = (None if protocols is None
                           else validate_protocol_bundle(protocols))
        # Whether the ADDITIONAL deterministic evaluation is computed inside this run.
        # In `offline_diagnostic` mode it is not computed at all: the run records the
        # raw evidence and states plainly that the extra evaluation did not run.
        if deterministic_evaluation not in ("in_run", "offline_diagnostic"):
            raise NativeVitaError("unknown deterministic evaluation mode")
        if deterministic_evaluation == "offline_diagnostic" and self._protocols is None:
            raise NativeVitaError(
                "offline diagnostic mode requires a declared protocol bundle")
        self._deterministic_evaluation = deterministic_evaluation
        self._compute_deterministic = (deterministic_evaluation == "in_run")
        self._simulator_suffix = (
            "" if self._protocols is None
            else user_simulator_system_suffix(self._protocols["simulator_protocol"]))
        self._user = self._make_user_simulator(self._subtasks)
        if not cloud:
            self._user.set_seed(seed)  # Native method inserts an API seed argument.
        self._next = 0
        self._active = None
        self._environment = None
        self._user_state = None
        self._aborted = False
        self._calls = []
        self._events = []
        self._completed = []
        self._last_evaluation = None
        self._baseline_orders = {}
        self._last_simulator_decision = None

    def _make_user_simulator(self, subtasks):
        """The NATIVE PersonalizationUser, plus the declared constraint block.

        The subclass is created per wrapper instance and APPENDS to the pinned
        property, so no shared class is mutated and an undeclared protocol builds the
        native object itself, byte-for-byte as before.
        """
        native_class = self._native.user_module.PersonalizationUser
        if not self._simulator_suffix:
            return native_class(
                subtasks=subtasks, persona=str(self._persona), instructions=None,
                llm=self._model, llm_args=self._llm_args("user_simulator"),
                language=self._language)
        base_property = native_class.system_prompt
        suffix = self._simulator_suffix

        class _BoundaryUser(native_class):
            @property
            def system_prompt(self):
                return base_property.fget(self) + suffix

        return _BoundaryUser(
            subtasks=subtasks, persona=str(self._persona), instructions=None,
            llm=self._model, llm_args=self._llm_args("user_simulator"),
            language=self._language)

    def _llm_args(self, role):
        args = dict(deepcopy(self._args), extra_headers={"X-AE-Role": role})
        if self._required_request_fields:
            # The declared transport's own fields (its explicit mode) travel with every
            # native user/judge call through the SAME egress and parameter contract the
            # run-local model config declares - the pinned client merges `extra_body`
            # into the request it sends, so a request missing the mode is never sent.
            args["extra_body"] = deepcopy(self._required_request_fields)
        return args

    def _check_task(self, public_task):
        if self._aborted or self._next >= len(self._sample["tasks"]):
            raise NativeVitaError("native run is aborted or the verified native scope is exhausted")
        if public_task != self._sample["tasks"][self._next]:
            raise NativeVitaError("public task differs from the verified consecutive projection")

    def _check_active(self):
        if self._aborted or self._active is None or getattr(_ACTIVE, "owner", None) is not self:
            raise NativeVitaError("no active native task on this thread")

    def _clear_registries(self):
        # The fixed pinned classes, as resolved by this wrapper's own module load.
        for cls in (self._native.tasks.StoreBaseModel, self._native.tasks.ProductBaseModel,
                    self._native.tasks.Location):
            cls.clear_thread_data()

    def _policy(self, subtask):
        prompts = self._native.prompts.get_prompts(self._language)
        template = (prompts.personalization_agent_proactive_system_prompt
                    if "proactive" in subtask.skill_tested else prompts.personalization_agent_system_prompt)
        task_time = subtask.environment["time"]
        weekday = self._native.utils.get_weekday(task_time, self._language)
        return (template + "\n\n" + prompts.personalization_agent_addendum).format(time=task_time + " " + weekday)

    def preview_tasks(self):
        """Build actual native env/schema/prompt previews; never call tools or an LLM."""
        if self._aborted or self._next != 0 or self._active is not None or getattr(_ACTIVE, "owner", None) is not None:
            raise NativeVitaError("preview requires an unstarted native wrapper and no active environment")
        _ACTIVE.owner = self
        previews = []
        try:
            for subtask in self._subtasks:
                self._clear_registries()
                constructor = self._native.registry.registry.get_env_constructor(subtask.domain)
                environment = constructor(deepcopy(subtask.environment), self._language)
                schemas = [deepcopy(tool.openai_schema["function"]) for tool in environment.get_tools()]
                preview_user = self._make_user_simulator([subtask])
                db_text = json.dumps(_plain(environment.tools.db), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                policy = self._policy(subtask)
                user_prompt = preview_user.system_prompt
                previews.append({"task_id": subtask.subtask_id, "domain": subtask.domain,
                                 "tool_schemas": schemas, "domain_policy": policy,
                                 "user_system_prompt": user_prompt, "instruction": subtask.instruction,
                                 "env_initial_hash": hashlib.sha256(db_text.encode()).hexdigest(),
                                 "lengths_chars": {"tool_schemas": len(json.dumps(schemas, ensure_ascii=False)),
                                                   "domain_policy": len(policy), "user_system_prompt": len(user_prompt),
                                                   "instruction": len(subtask.instruction), "environment_db": len(db_text)},
                                 "model_called": False, "tools_executed": False,
                                 "native_orchestrator_used": False})
            return previews
        finally:
            self._clear_registries()
            _ACTIVE.owner = None

    def _validate_response(self, response):
        data = _plain(response)
        if response.is_tool_call() or not isinstance(response.content, str) or not response.content.strip():
            raise NativeVitaError("native user/judge must produce nonempty text without tool calls")
        raw = data.get("raw_data")
        choices = raw.get("choices") if isinstance(raw, dict) else None
        if not isinstance(choices, list) or len(choices) != 1 or choices[0].get("finish_reason") != "stop":
            raise NativeVitaError("native response did not finish cleanly; inspect retained raw response")
        return data

    @contextmanager
    def _capture(self, role, rubric_keys=None):
        module = self._native.user_module if role == "user_simulator" else self._native.judge_module
        with _CAPTURE_LOCK:
            original = module.generate
            def generate(*args, **kwargs):
                if args or kwargs.get("model") != self._model:
                    raise NativeVitaError("native generation used an unexpected model or positional request")
                record = {"role": role, "subtask_id": self._active.subtask_id,
                          "request": _plain(kwargs), "response": None, "error": None}
                self._calls.append(record)
                try:
                    response = original(**kwargs)
                    record["response"] = _plain(response)
                    self._validate_response(response)
                    if rubric_keys is not None:
                        parsed = self._native.utils.evaluator_extracter(response.content)
                        if (not isinstance(parsed, list) or len(parsed) != len(rubric_keys)
                                or any(not isinstance(x, dict) for x in parsed)
                                or {x.get("rubric_idx") for x in parsed} != set(rubric_keys)
                                or any(type(x.get("meetExpectation")) is not bool
                                       or not isinstance(x.get("justification"), str) for x in parsed)):
                            raise NativeVitaError("judge output has missing/duplicate/malformed rubric decisions")
                    return response
                except Exception as exc:
                    record["error"] = {"type": type(exc).__name__, "message": str(exc)}
                    # Omit upstream error text: prevents the native evaluator's
                    # extra 429 retry layer from repeating an audited request.
                    raise NativeVitaError("native generation failed; inspect retained call record") from exc
            module.generate = generate
            try:
                yield
            finally:
                module.generate = original

    def start(self, public_task, protocols=None):
        """Begin one native task. `protocols` must match the one this wrapper was built with.

        Passing a different bundle here is a refusal rather than a silent override: the
        protocol a run was configured with is the protocol its phases run under.
        """
        if protocols is not None and self._protocols is None:
            raise NativeVitaError("this native wrapper was built without a protocol bundle")
        if protocols is not None and validate_protocol_bundle(protocols) != self._protocols:
            raise NativeVitaError("the phase protocol bundle differs from the wrapper's own")
        self._check_task(public_task)
        if self._active is not None or getattr(_ACTIVE, "owner", None) is not None:
            raise NativeVitaError("native environments must run strictly sequentially on each thread")
        _ACTIVE.owner = self
        try:
            # Native tools read these thread registries (not only their DB).
            self._clear_registries()
            self._active = self._subtasks[self._next]
            self._events.append({"event": "clear_thread_registries", "classes": ["StoreBaseModel", "ProductBaseModel", "Location"],
                                 "subtask_id": self._active.subtask_id, "adaptation": "per-subtask environment isolation"})
            if self._active.message_history:
                raise NativeVitaError("this bounded wiring implementation requires the verified empty initial history")
            constructor = self._native.registry.registry.get_env_constructor(self._active.domain)
            self._environment = constructor(deepcopy(self._active.environment), self._language)
            # The phase's own pre-phase order table. Recorded BEFORE the first user turn
            # so "a new order" always means "absent from the table this phase started
            # with" - a historical or earlier-phase order can never satisfy it.
            self._baseline_orders = dict(self._environment_orders_now())
            self._user_state = self._user.get_init_state()
            greeting = self._native.orchestrator.get_default_first_agent_message(self._language)
            instruction, self._user_state = self._user.generate_next_message(greeting, self._user_state)
            if instruction.is_tool_call() or instruction.content != public_task["instruction"]:
                raise NativeVitaError("native first instruction differs from the public task")
            # A declared simulator protocol governs EVERY user turn, including the one
            # that delivers the task instruction. It is checked before the phase runs and
            # handed to the driver with the first `user_reply`, so the whole conversation
            # - instruction turn included - is bound to a decision record.
            self._last_simulator_decision = None
            self._pending_instruction_decision = None
            if self._protocols is not None:
                self._pending_instruction_decision = self._check_simulator_turn(
                    instruction.content, incoming="", enforce=True)
            policy = self._policy(self._active)
            self._events.append({"event": "start", "subtask_id": self._active.subtask_id,
                                 "user_system_prompt": self._user.system_prompt, "domain_policy": policy,
                                 "greeting": _plain(greeting), "instruction": _plain(instruction),
                                 "simulator_decision": _plain(self._last_simulator_decision)})
            return {"environment": self._environment, "domain_policy": policy,
                    "instruction": instruction.content, "greeting": greeting}
        except Exception:
            self.abort()
            raise

    def _environment_orders_now(self):
        """This phase's own live order table, or {} when no environment is active."""
        try:
            return dict(self._environment.tools.db.orders or {})
        except Exception:  # pragma: no cover - defensive
            return {}

    def _check_simulator_turn(self, text, *, incoming, enforce):
        """Apply the declared simulator protocol to ONE user turn.

        The decision record is kept either way. With `enforce`, a boundary violation
        stops the phase as a `SimulatorProtocolViolation`: the offending text is kept
        verbatim in the record, nothing is deleted or rewritten, no substitute reply is
        invented and nothing is retried.
        """
        orders = self._environment_orders_now()
        baseline = getattr(self, "_baseline_orders", None) or {}
        new_ids = sorted(new_orders_of(orders, baseline))
        paid_ids = sorted(paid_orders_of(orders, baseline))
        decision = validate_user_reply(
            text=text, incoming_assistant_text=incoming, new_order_ids=new_ids,
            paid_order_ids=paid_ids,
            protocol=self._protocols["simulator_protocol"])
        # Only the phase's own database may substantiate a payment claim: the user
        # simulator has no tool results of its own.
        decision["phase_new_order_ids"] = new_ids
        decision["phase_paid_order_ids"] = paid_ids
        self._last_simulator_decision = decision
        if enforce and decision["violations"]:
            raise SimulatorProtocolViolation(decision)
        return decision

    def user_reply(self, text):
        self._check_active()
        if not isinstance(text, str) or not text.strip():
            raise NativeVitaError("user simulator needs actual final assistant text")
        message = self._native.messages.AssistantMessage(role="assistant", content=text)
        with self._capture("user_simulator"):
            response, self._user_state = self._user.generate_next_message(message, self._user_state)
        data = self._validate_response(response)
        stop = self._native.user_simulator.UserSimulator.is_stop(response)
        if self._protocols is None:
            return {"content": response.content, "stop": stop, "raw": data,
                    "protocol_decision": None}
        # The declared protocol REPLACES the raw marker test: a bare marker inside a
        # fabricated assistant turn is not a user decision to end the conversation, and
        # a phase that ends on one is not a normal completion.
        decision = self._check_simulator_turn(response.content, incoming=text,
                                              enforce=True)
        # The instruction turn's own decision travels with the phase's first reply, so a
        # phase's decision list covers EVERY user turn its transcript contains.
        return {"content": response.content, "stop": decision["stop"], "raw": data,
                "protocol_decision": decision}

    def pending_instruction_decision(self):
        """The instruction turn's decision, claimed ONCE by the driver.

        The driver collects it immediately after `start`, so an Agent that returns STOP
        on its first reply - with no `user_reply` at all - still leaves a decision for
        every user turn its transcript contains. Claiming clears it, so a later call
        cannot hand the same decision out twice.
        """
        decision = getattr(self, "_pending_instruction_decision", None)
        self._pending_instruction_decision = None
        return decision

    def agent_stop(self, text):
        return self._native.agent.BaseAgent.is_stop(
            self._native.messages.AssistantMessage(role="assistant", content=text))

    def _messages(self, transcript):
        if not isinstance(transcript, list) or not transcript:
            raise NativeVitaError("evaluation requires an actual nonempty subtask transcript")
        classes = {"assistant": self._native.messages.AssistantMessage,
                   "user": self._native.messages.UserMessage, "tool": self._native.messages.ToolMessage}
        messages = []
        for i, row in enumerate(transcript):
            if not isinstance(row, dict) or row.get("role") not in classes:
                raise NativeVitaError("transcript accepts only native user/assistant/tool messages")
            _plain(row)
            if row["role"] == "tool" and (not isinstance(row.get("id"), str) or not row["id"]
                                              or not isinstance(row.get("name"), str) or not row["name"]):
                raise NativeVitaError("tool transcript requires actual native id and name")
            if row.get("tool_calls") is not None:
                calls = row["tool_calls"]
                if (row["role"] != "assistant" or not isinstance(calls, list) or not calls
                        or any(not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"]
                               or not isinstance(c.get("name"), str) or not c["name"]
                               or not isinstance(c.get("arguments"), dict)
                               or c.get("requestor", "assistant") != "assistant" for c in calls)):
                    raise NativeVitaError("assistant transcript requires actual native tool call IDs and arguments")
            message = classes[row["role"]].model_validate(deepcopy(row))
            message.turn_idx = i
            messages.append(message)
        return messages

    def finish(self, public_task, transcript, termination_reason, duration, *,
               arm=None, target_product_ids=None):
        """Judge one finished phase. `arm`/`target_product_ids` are scoring material only.

        Neither is ever sent to a model: the arm label and the dataset's target
        product ids are used to compute the deterministic business fact.
        """
        self._check_task(public_task)
        self._check_active()
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
            raise NativeVitaError("invalid task duration")
        messages = self._messages(transcript)
        task = self._native.tasks.Task(
            id="U000828_subtask_" + self._active.subtask_id,
            domain=self._active.domain, environment=deepcopy(self._active.environment),
            user_scenario=self._active.user_scenario, instructions=self._active.instruction,
            evaluation_criteria=self._active.evaluation_criteria)
        now = self._native.utils.get_now()
        states = self._native.orchestrator.Orchestrator.get_states(
            None, self._environment.tools.db, self._environment.tools.db.time)
        simulation = self._native.simulation.SimulationRun(
            id=str(uuid.uuid4()), task_id=task.id, start_time=now, end_time=now, duration=duration,
            termination_reason=termination_reason, messages=messages, states=states, seed=self._seed)
        rubric_keys = self._native.judge_module.TrajectoryEvaluator._initialize_rubric_states(task.evaluation_criteria)
        if not rubric_keys:
            raise NativeVitaError("verified task must have nonempty rubrics; no automatic reward=1")
        self._last_evaluation = {"simulation": _plain(simulation), "reward_info": None, "error": None}
        call_start = len(self._calls)
        try:
            with self._capture("evaluator", rubric_keys):
                reward = self._native.evaluator.evaluate_simulation(
                    simulation=simulation, task=task, evaluation_type="trajectory", domain=self._active.domain,
                    llm_evaluator=self._model, llm_args_evaluator=self._llm_args("evaluator"), language=self._language)
            reward_data = _plain(reward)
            self._last_evaluation["reward_info"] = reward_data
        except Exception as exc:
            self._last_evaluation["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise NativeVitaError("native evaluation failed; no scientific failure score assigned") from exc
        result = {"subtask_id": self._active.subtask_id, "reward_info": reward_data,
                  "judging_status": "MODEL_JUDGED_DEBUG_ONLY" if len(self._calls) > call_start else "NOT_MODEL_JUDGED_PREMATURE_TERMINATION",
                  "scientific_success": None, "official_benchmark": False,
                  "simulation": _plain(simulation),
                  "note": "Native components inside a derived loop; outer limits are not native Orchestrator max_steps."}
        if self._compute_deterministic:
            # The deterministic half of the separated evaluation, computed HERE from
            # this phase's own database. It never reads the transcript, so "支付成功"
            # text cannot make an order paid, and it is recorded next to - never
            # instead of - the native reward above. The dataset's own target ids are
            # optional because they are private scoring material the caller may not
            # hold; when they are absent the check says so rather than guessing.
            active_task = _plain(self._active)
            # Called THROUGH the module, so the one switch that decides whether the
            # additional evaluation runs at all is also the one place a caller can
            # substitute or disable it.
            result["business_completion"] = sim_eval.deterministic_business_result(
                arm=arm, number=self._next + 1, task=active_task,
                scope_task_record={"target_product_ids": list(target_product_ids or ())},
                orders=self._environment_orders_now(),
                baseline_order_ids=sorted(self._baseline_orders),
                dataset_rubric_texts=active_task.get("rubrics") or (),
                transcript=_plain(messages))
            result["simulator_decision"] = _plain(self._last_simulator_decision)
        elif self._protocols is not None:
            # The mode is recorded in the phase's own record, with an explicit pending
            # marker: the extra evaluation did not run, and nothing claims it passed.
            result["business_completion"] = None
            result["additional_evaluation"] = {
                "deterministic_evaluation": self._deterministic_evaluation,
                "ran": False, "status": "NOT_RUN_PENDING_MANUAL_REVIEW",
                "note": ("the additional deterministic evaluation is an OFFLINE DIAGNOSTIC "
                         "for this exploratory run; it was not computed here and must not "
                         "be read as a pass or a failure")}
        self._completed.append(deepcopy(result))
        self._user.mark_subtask_completed()
        self._user.advance_to_next_subtask()
        self._next += 1
        self._active = None
        _ACTIVE.owner = None
        return result

    def snapshot(self):
        """PRIVATE journal payload; never provide this object to an Agent."""
        return _plain({"visibility": "private_driver_and_judge_only", "provenance": self._provenance,
                       "raw_subtasks": self._raw_subtasks, "root_persona": self._persona,
                       "model_parameters": self._args, "next_index": self._next, "aborted": self._aborted,
                       "active_subtask_id": self._active.subtask_id if self._active else None,
                       "environment_db": self._environment.tools.db if self._environment else None,
                       "user_state": self._user_state, "native_calls": self._calls,
                       "events": self._events, "completed": self._completed,
                       "last_evaluation": self._last_evaluation,
                       "protocols": self._protocols,
                       "baseline_order_ids": sorted(self._baseline_orders),
                       "last_simulator_decision": self._last_simulator_decision})

    def abort(self):
        """Release only the local ownership guard; retain all evidence and do not advance."""
        self._aborted = True
        self._events.append({"event": "abort", "subtask_id": self._active.subtask_id if self._active else None})
        if getattr(_ACTIVE, "owner", None) is self:
            _ACTIVE.owner = None
