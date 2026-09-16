"""Bounded t4-t5 wiring driver; not a confirmatory R/E benchmark.

No framework import or network activity on import. NativeVita owns the private
environment/user/evaluator; the bridge receives only ae_inputs public tasks.
The outer loop uses native stop predicates, not Letta end_turn as task success.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import importlib
import inspect
import json
import math
import re
import time
from urllib.parse import quote

from ae_adapter import (BridgeBlocked, LettaBridge, MemoryPolicy, RecoverableToolError,
                        ToolBinding, VerifiedWritePrecondition, assert_separate_arms,
                        creation_payload, dumps, frozen_environment_call)
from ae_inputs import history_message
from ae_probe import _origin, _check_llm_config


SCHEMA_VERSION = "ae-task-wiring-0.1"
FIELDS = {"schema_version", "purpose", "arms", "start_turn", "end_turn",
          "model_origin", "letta_origin", "model_handle", "expected_model",
          "context_window", "max_output_tokens", "auxiliary_output_tokens",
          "temperature", "seed", "block_char_limit", "max_rounds", "max_steps",
          "max_stage_posts", "max_user_exchanges", "max_tool_return_chars",
          "timeout_seconds", "max_request_bytes", "max_response_bytes"}


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError("task config must contain exactly the declared fields")
    if (config["schema_version"] != SCHEMA_VERSION or config["purpose"] != "real_task_wiring"
            or config["arms"] != ["rewrite", "erratum"]
            or config["start_turn"] != 4 or config["end_turn"] != 5):
        raise ValueError("this driver is limited to paired t4-t5 wiring")
    for key in ("model_origin", "letta_origin"):
        _origin(config[key])
    if config["model_origin"] == config["letta_origin"]:
        raise ValueError("Letta and model origins must differ")
    if (config["model_handle"] != "vllm/Qwen3-8B" or config["expected_model"] != "Qwen3-8B"):
        raise ValueError("this wiring config targets the inspected Qwen3-8B service")
    integer_fields = FIELDS - {"schema_version", "purpose", "arms", "model_origin", "letta_origin",
                              "model_handle", "expected_model", "temperature", "timeout_seconds"}
    for key in integer_fields:
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be an explicit positive integer")
    for key in ("temperature", "timeout_seconds"):
        value = config[key]
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value < 0
                or (key == "timeout_seconds" and value == 0)):
            raise ValueError(f"invalid {key}")
    if max(config["max_output_tokens"], config["auxiliary_output_tokens"]) >= config["context_window"]:
        raise ValueError("context must leave room for input")
    # Fixed archive V3, not a fitted scientific threshold (letta_agent_v3.py).
    native_char_limit = max(5000, int(config["context_window"] * 0.8))
    if config["max_tool_return_chars"] > native_char_limit:
        raise ValueError("tool return guard cannot exceed the fixed Letta V3 truncation limit")
    return deepcopy(config)


def task_payload(config: dict, sample: dict, arm: str, name: str) -> tuple[dict, MemoryPolicy]:
    memory = MemoryPolicy(arm, sample["initial_facts"], block_char_limit=config["block_char_limit"])
    payload = creation_payload(name=name, model=config["model_handle"],
                               profile=sample["initial_profile"], memory=memory)
    payload["context_window_limit"] = config["context_window"]
    payload["model_settings"] = {
        "provider_type": "openai", "max_output_tokens": config["max_output_tokens"],
        "temperature": config["temperature"], "parallel_tool_calls": False, "strict": False,
    }
    return payload, memory


def build_plan(config: dict, sample: dict) -> dict:
    config = validate_config(config)
    if [t["number"] for t in sample["tasks"]] != [4, 5]:
        raise ValueError("sample must contain exactly t4 and t5")
    return {"schema_version": SCHEMA_VERSION, "status": "PLAN", "config": config,
            "network_called": False, "model_called": False, "source": sample["source"],
            "tasks": [{"subtask_id": task["subtask_id"], "domain": task["domain"],
                       "history_records": len(task["history"]),
                       "history_message": history_message(task),
                       "instruction": task["instruction"]} for task in sample["tasks"]],
            "agent_payloads": {arm: task_payload(config, sample, arm, f"ae-wiring-{arm}-GENERATED")[0]
                               for arm in config["arms"]},
            "boundaries": ["native environment/user/evaluator, no native memory backend",
                           "one persistent Letta agent per arm; task environments reset from snapshots",
                           "serial native environments; no shared registry while active",
                           "all three LLM roles use local Qwen3-8B; judge scores are diagnostic only",
                           "outer exchange limits are not the native Orchestrator max_steps count",
                           "no silent summary/truncation/retry; invalid execution is not reward zero",
                           "t4-t5 is not repeated same-field replacement or a KV speed experiment"]}


class CheckedTaskTransport:
    def __init__(self, delegate, path: str, config: dict):
        self.delegate, self.path, self.config = delegate, path, config
        self.verified = False

    def request(self, method, path, body=None):
        if method == "GET" and path.startswith(self.path + "?"):
            state = self.delegate.request(method, path, body)
            _check_llm_config(state, self.config)
            self.verified = True
            return state
        if (method == "POST" and path == self.path + "/messages" and self.verified):
            self.verified = False
            return self.delegate.request(method, path, body)
        if method == "PATCH" and path == self.path + "/core-memory/blocks/ae_preferences":
            return self.delegate.request(method, path, body)
        raise BridgeBlocked("unverified or unexpected task transport request")


class TraceList(list):
    """Persist bridge events as they happen, also make the judge trajectory.

Raw API events (including reasoning) are kept in the journal. Only public
assistant text and tool calls/results go into the task transcript. History
processing is separately journaled, never relabeled as current-task actions.
"""
    def __init__(self, bridge, emit):
        super().__init__()
        self.bridge, self.emit = bridge, emit

    def append(self, event):
        event = deepcopy(event)
        super().append(event)
        b = self.bridge
        self.emit({"kind": "bridge_event", "arm": b.memory.arm, "task": b.task_id,
                   "phase": b.phase, "event": event})
        if b.phase != "task":
            return
        if event.get("kind") == "response":
            for msg in event["body"].get("messages", []):
                typ = msg.get("message_type")
                if typ == "assistant_message" and isinstance(msg.get("content"), str) and msg["content"].strip():
                    b.transcript.append({"role": "assistant", "content": msg["content"]})
                elif typ == "approval_request_message":
                    calls = msg.get("tool_calls")
                    if calls is None:
                        calls = [msg.get("tool_call")]
                    converted = []
                    for call in calls:
                        # The frozen native evaluator trajectory can only carry a
                        # JSON object of arguments. Non-object or malformed
                        # arguments stop the run explicitly; the trace schema is
                        # intentionally NOT widened to invent a representation.
                        try:
                            args = json.loads(call["arguments"])
                        except (TypeError, ValueError):
                            raise BridgeBlocked(
                                "tool arguments are not valid JSON; cannot form a native "
                                "evaluator transcript") from None
                        if not isinstance(args, dict):
                            raise BridgeBlocked("tool arguments cannot form a native evaluator transcript")
                        converted.append({"id": call["tool_call_id"], "name": call["name"],
                                          "arguments": args, "requestor": "assistant"})
                    b.transcript.append({"role": "assistant", "content": None, "tool_calls": converted})
        elif event.get("kind") == "client_tool_result":
            call, result = event["call"], event["result"]
            b.transcript.append({"role": "tool", "id": result["tool_call_id"], "name": call["name"],
                                 "content": result["tool_return"], "requestor": "assistant",
                                 "error": result["status"] != "success"})


class TaskBridge(LettaBridge):
    def __init__(self, *args, config: dict, emit, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config
        self.phase, self.task_id = "setup", None
        self.transcript, self.stage_posts = [], 0
        self.trace = TraceList(self, emit)

    def begin(self, task: dict, greeting: dict):
        self.task_id = task["subtask_id"]
        self.transcript = [deepcopy(greeting)]
        self.stage_posts = 0

    def _request(self, method, path, body=None):
        if method == "POST" and path == self.path + "/messages":
            if self.stage_posts >= self.config["max_stage_posts"]:
                self._fail("per-task model POST limit reached; not a scientific failure")
            for msg in body["messages"]:
                if msg.get("role") == "user":
                    material = json.loads(msg["content"])
                    source = material.get("source")
                    if source == "dataset_history/material":
                        self.phase = "history"
                    elif source in {"current_task", "runtime_user"}:
                        self.phase = "task"
                        text = material["instruction"] if source == "current_task" else material["content"]
                        self.transcript.append({"role": "user", "content": text})
                    else:
                        self._fail("unrecognized public message source")
            self.stage_posts += 1
        return super()._request(method, path, body)

    def _execute(self, tool, bindings):
        result = super()._execute(tool, bindings)
        if len(result["tool_return"]) > self.config["max_tool_return_chars"]:
            self._fail("tool result exceeds fixed Letta character limit; saved full result, not submitted")
        return result


def _tool_type_name(value):
    """Normalize a native ToolType (str enum) to its lower-case value name."""
    name = getattr(value, "value", value)
    return name.lower() if isinstance(name, str) else None


def native_read_only(environment, name: str) -> bool:
    """Read the native tool-type metadata; never guess from the tool name.

    Uses the pinned `ToolKitBase.tool_type` accessor (which returns
    `ToolType.READ`/`WRITE`/`THINK`/`GENERIC` from the `__tool_type__` attribute
    set by `@is_tool`). A missing accessor or an unresolvable tool is not
    read-only, so its errors keep stopping the run.
    """
    toolkit = getattr(environment, "tools", None)
    accessor = getattr(toolkit, "tool_type", None)
    if not callable(accessor):
        return False
    try:
        return _tool_type_name(accessor(name)) == "read"
    except Exception:
        return False


# Explicit registry of native lookup branches that were actually inspected and
# reproduced offline. Only these exact raise sites are recoverable; everything
# else (including other ValueError/LookupError) keeps stopping the run. The
# registry is intentionally narrow: it is not a general "read error" rule.
VERIFIED_READ_FAILURE_BRANCHES = {
    # vita/domains/delivery/tools.py DeliveryTools._get_store_product:
    #     raise ValueError(f"{product_id} not found")
    "get_delivery_product_info": ("_get_store_product", re.compile(r"^(?P<value>.+) not found$")),
}

# Explicit registry of native WRITE preconditions that were inspected and
# reproduced offline. A WRITE tool stays WRITE: this is not a general rule for
# `ValueError`/`RecoverableToolError`, and nothing here is decided from the tool
# name or the error text alone. Every entry names the exact fixed method, the
# exact fixed helper whose raise site is the precondition, and the argument whose
# value the message must quote.
VERIFIED_WRITE_PRECONDITION_BRANCHES = {
    # vita/domains/delivery/tools.py DeliveryTools.pay_delivery_order:
    #     order = self._get_delivery_order(order_id)      <- first statement, read-only
    # and DeliveryTools._get_delivery_order:
    #     raise ValueError(f"Order {order_id} not found")  <- before any status write
    # The second raise in that helper ("is not a delivery order") is a different
    # branch and is deliberately NOT registered.
    "pay_delivery_order": {
        "method": "pay_delivery_order",
        "helper": "_get_delivery_order",
        "pattern": re.compile(r"^Order (?P<value>.+) not found$"),
        "argument": "order_id",
    },
}


def _innermost_frame(exc: BaseException):
    tb = exc.__traceback__
    if tb is None:
        return None
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb.tb_frame


def _fixed_delivery_helper(helper_name: str):
    """The pinned Vita DeliveryTools definition of a helper, or None.

    Imported lazily so non-Vita contexts can import this module; the verification
    only succeeds when the fixed package is actually importable, which is the case
    for the native driver runs.
    """
    try:
        from vita.domains.delivery.tools import DeliveryTools
    except Exception:
        return None
    return getattr(DeliveryTools, helper_name, None)


def _fixed_vita_member(module_name: str, owner_name: str, attribute: str):
    """The pinned Vita definition of one member, or None.

    Identity against these definitions is what makes a witness trustworthy; a
    same-named member installed anywhere else is not accepted.
    """
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None
    owner = getattr(module, owner_name, None)
    member = getattr(owner, attribute, None) if owner is not None else None
    return member if callable(member) else None


def verified_read_failure(environment, tool_name: str, exc: BaseException, args: dict) -> bool:
    """True only for an inspected native lookup branch with exact code identity.

    Requirements, all verifiable:
      * the tool name has a registered, inspected lookup branch;
      * the exception type is exactly ValueError (subclasses such as pydantic
        ValidationError or UnicodeDecodeError are not this branch);
      * the raising frame is the INNERMOST traceback frame and is that helper's
        exact code object -- so a nested/internal raise is not attributed to it;
      * the helper binding resolves to the pinned Vita `DeliveryTools` original
        function, not to a same-named method supplied by a stand-in toolkit;
      * the message matches the inspected shape and its extracted value is the
        exact value the model supplied for this call.
    A same-message error raised from any other site, an internal
    KeyError/IndexError, or a fake same-named helper stays blocked.
    """
    branch = VERIFIED_READ_FAILURE_BRANCHES.get(tool_name)
    if branch is None or type(exc) is not ValueError:
        return False
    helper_name, pattern = branch
    toolkit = getattr(environment, "tools", None)
    helper = getattr(toolkit, helper_name, None)
    if not inspect.ismethod(helper):
        return False
    fixed_helper = _fixed_delivery_helper(helper_name)
    if fixed_helper is None or helper.__func__ is not fixed_helper:
        return False
    frame = _innermost_frame(exc)
    if frame is None or frame.f_code is not helper.__func__.__code__:
        return False
    match = pattern.fullmatch(str(exc))
    if match is None:
        return False
    value = match.group("value")
    return any(isinstance(supplied, str) and supplied == value for supplied in args.values())


def _recoverable_message(exc: BaseException) -> str:
    """Keep the original error text; fall back to the type name when empty."""
    text = str(exc).strip()
    return text or type(exc).__name__


def native_state_hash(environment):
    """The FIXED Vita state witness for this environment, or None.

    The witness is trustworthy only when its whole source chain is the pinned
    code BOUND TO THIS OBJECT: `Environment.get_db_hash` and the
    `ToolKitBase.get_db_hash` it delegates to must both be the fixed definitions
    (`__func__`) AND bound to this environment and this toolkit (`__self__`), and
    the value must satisfy the pinned `get_hash` contract (exactly 64 lower-case
    hex characters). A replaced, wrapped or cached getter -- an instance
    attribute, a subclass override, a lambda returning an old digest, or the SAME
    function definition borrowed from another environment/toolkit -- is refused
    even when its return value looks valid: equal function definitions are not
    equal bound objects, and a fabricated or foreign digest proves nothing. An
    environment whose fixed chain is unavailable can never be recovered.

    The digest is an ADDITIONAL state comparison, not proof of absence of writes:
    equal digests only say the covered state is equal before and after. The
    no-write conclusion comes from the verified fixed execution path (the raise is
    the first statement of the fixed method and only reads).
    """
    fixed_environment = _fixed_vita_member("vita.environment.environment",
                                           "Environment", "get_db_hash")
    fixed_toolkit = _fixed_vita_member("vita.environment.toolkit", "ToolKitBase", "get_db_hash")
    if fixed_environment is None or fixed_toolkit is None:
        return None
    accessor = getattr(environment, "get_db_hash", None)
    if (not inspect.ismethod(accessor) or accessor.__func__ is not fixed_environment
            or accessor.__self__ is not environment):
        return None
    toolkit = getattr(environment, "tools", None)
    toolkit_accessor = getattr(toolkit, "get_db_hash", None)
    if (not inspect.ismethod(toolkit_accessor) or toolkit_accessor.__func__ is not fixed_toolkit
            or toolkit_accessor.__self__ is not toolkit):
        return None
    try:
        # Call the fixed definition itself, never the looked-up attribute.
        value = fixed_environment(environment)
    except Exception:
        return None
    return value if _valid_state_digest(value) else None


def _valid_state_digest(value) -> bool:
    """The pinned `get_hash` contract: exactly 64 lower-case hex characters.

    No coercion: a missing, non-string, short, long, upper-case or non-hex value
    is not a witness, and `str()` is never used to make two invalid values look
    equal.
    """
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _native_write_tool(environment, name: str) -> bool:
    """Native tool-type metadata must say WRITE; never inferred from the name."""
    toolkit = getattr(environment, "tools", None)
    accessor = getattr(toolkit, "tool_type", None)
    if not callable(accessor):
        return False
    try:
        return _tool_type_name(accessor(name)) == "write"
    except Exception:
        return False


def native_write_precondition(environment, name: str) -> bool:
    """True only when this tool is the registered fixed Vita WRITE precondition.

    Checked when the binding is built, so the bridge can require an explicit
    authorization before it accepts the dedicated proof: the tool is classified
    WRITE by native metadata, the toolkit's method is the pinned `DeliveryTools`
    definition itself (a same-named stand-in, a subclass override, or an
    instance-level replacement is refused), the helper is that same pinned
    definition, and the environment can produce its own state hash.
    """
    branch = VERIFIED_WRITE_PRECONDITION_BRANCHES.get(name)
    toolkit = getattr(environment, "tools", None)
    if branch is None or toolkit is None or not _native_write_tool(environment, name):
        return False
    for key in ("method", "helper"):
        member = getattr(toolkit, branch[key], None)
        fixed = _fixed_delivery_helper(branch[key])
        # Same function definition AND bound to THIS toolkit: a same-named method
        # or the fixed definition borrowed from another toolkit is not accepted.
        if (fixed is None or not inspect.ismethod(member) or member.__func__ is not fixed
                or member.__self__ is not toolkit):
            return False
    # The state witness must come from the fixed chain as well, so a replaced or
    # cached getter can never authorize the precondition recovery.
    return native_state_hash(environment) is not None


def verified_write_precondition(environment, tool_name: str, exc: BaseException,
                                args: dict, before_state) -> bool:
    """True only for a registered WRITE precondition proven for THIS call.

    Requirements, all verifiable:
      * the tool name has a registered, inspected precondition branch;
      * the exception type is exactly ValueError (a subclass or the same text on
        another exception type is not this branch);
      * the tool is still classified WRITE by native metadata, and the toolkit's
        method and helper are the pinned `DeliveryTools` definitions BOUND TO THIS
        TOOLKIT (`__func__` and `__self__`) -- so a fake same-named method, a
        wrapper that writes before delegating, a replaced helper, or the fixed
        definition borrowed from another toolkit instance cannot borrow the real
        helper's safety;
      * the raising frame is the INNERMOST traceback frame and is the fixed
        helper's exact code object, so a re-raise from another site is not
        attributed to it;
      * the message matches the inspected shape and quotes exactly the value this
        call supplied for the registered argument;
      * the fixed state witness is available for THIS environment and is unchanged
        across the failure. The witness must come from the pinned
        `Environment.get_db_hash` -> `ToolKitBase.get_db_hash` chain and satisfy the
        pinned digest contract; a missing, empty, wrong-typed, raising, replaced or
        stale-digest getter is refused. Equal digests are an ADDITIONAL state
        comparison: they do not by themselves prove that nothing was written during
        the call nor that no external side effect exists. The no-write conclusion
        rests on the verified fixed execution path, in which the raise is the first
        statement and only reads.
    """
    branch = VERIFIED_WRITE_PRECONDITION_BRANCHES.get(tool_name)
    if branch is None or type(exc) is not ValueError:
        return False
    before_digest = before_state
    after_digest = native_state_hash(environment)
    if not _valid_state_digest(before_digest) or not _valid_state_digest(after_digest):
        return False
    if before_digest != after_digest:
        return False
    if not _native_write_tool(environment, tool_name):
        return False
    toolkit = getattr(environment, "tools", None)
    method = getattr(toolkit, branch["method"], None)
    fixed_method = _fixed_delivery_helper(branch["method"])
    if (fixed_method is None or not inspect.ismethod(method)
            or method.__func__ is not fixed_method or method.__self__ is not toolkit):
        return False
    helper = getattr(toolkit, branch["helper"], None)
    fixed_helper = _fixed_delivery_helper(branch["helper"])
    if (fixed_helper is None or not inspect.ismethod(helper)
            or helper.__func__ is not fixed_helper or helper.__self__ is not toolkit):
        return False
    frame = _innermost_frame(exc)
    if frame is None or frame.f_code is not fixed_helper.__code__:
        return False
    match = branch["pattern"].fullmatch(str(exc))
    if match is None:
        return False
    supplied = args.get(branch["argument"])
    return isinstance(supplied, str) and supplied == match.group("value")


def _recovering_call(environment, tool_name, frozen_call, read_only, write_precondition):
    """Freeze the dispatch target and gate any recovery on real provenance.

    `invoke` takes only `**kwargs`, so model arguments can never rebind the
    dispatch target or the read-only/precondition decision through a wrapper
    parameter. The native state hash is taken immediately before the call so a
    precondition recovery can prove that nothing was written.
    """
    def invoke(**kwargs):
        before_state = native_state_hash(environment) if write_precondition else None
        try:
            value = frozen_call(**kwargs)
        except (ValueError, LookupError) as exc:
            # Serialization below stays outside this handler on purpose: a
            # conversion failure is never turned into a recoverable error.
            if read_only and verified_read_failure(environment, tool_name, exc, kwargs):
                raise RecoverableToolError(_recoverable_message(exc)) from None
            if (write_precondition and isinstance(exc, ValueError)
                    and verified_write_precondition(environment, tool_name, exc, kwargs,
                                                    before_state)):
                raise VerifiedWritePrecondition(_recoverable_message(exc)) from None
            raise
        return environment.to_json_str(value)
    return invoke


def environment_bindings(environment) -> dict:
    """Use native environment execution and native JSON conversion.

    Unexpected exceptions are deliberately NOT retried. A trusted READ tool's
    recoverable argument/lookup error and a registered WRITE tool's proven
    precondition failure (the pinned `pay_delivery_order` missing-order branch)
    are returned to the model once as error results; every other exception,
    including every other WRITE/unknown-tool failure, every serialization
    failure, and every unproven precondition, still stops the bridge. Each
    binding's dispatch target is frozen, so model arguments cannot redirect it.
    """
    bindings = {}
    for tool in environment.get_tools():
        schema = deepcopy(tool.openai_schema["function"])
        name = schema["name"]
        if name == "memory_update" or name in bindings or "preference_memory" in name:
            raise BridgeBlocked("duplicate tool or a second memory backend")
        read_only = native_read_only(environment, name)
        write_precondition = native_write_precondition(environment, name)
        bindings[name] = ToolBinding(
            schema,
            _recovering_call(environment, name,
                             frozen_environment_call(environment, name),
                             read_only, write_precondition),
            owner=environment, read_only=read_only,
            write_precondition=write_precondition)
    return bindings


def final_public_text(messages: list[dict]) -> str:
    texts = [m["content"] for m in messages if isinstance(m.get("content"), str) and m["content"].strip()]
    if not texts:
        raise BridgeBlocked("exchange has no user-facing response; no automatic retry")
    return texts[-1]


def execute_wiring(config: dict, sample: dict, *, model_transport, letta_transport,
                   runtime_factory, emit) -> dict:
    """Run once, recording incomplete/invalid separately from diagnostic reward."""
    config = validate_config(config)
    build_plan(config, sample)
    result = {"schema_version": SCHEMA_VERSION, "purpose": "real_task_wiring",
              "status": "INVALID", "wiring_completed": False, "validity_passed": False,
              "input_audit_passed": None, "task_success": None, "scientific_result": None,
              "config": config, "source": sample["source"], "invalid_reasons": [],
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "arms": {},
              "agent_cleanup_performed": False}
    bridges, runtimes = {}, {}
    active = None
    try:
        listed = model_transport.request("GET", "/v1/models")
        matches = [m for m in listed.get("data", []) if m.get("id") == config["expected_model"]]
        if len(matches) != 1 or matches[0].get("max_model_len") != config["context_window"]:
            raise BridgeBlocked("provider model/window differs from wiring config")
        health = letta_transport.request("GET", "/v1/health/")
        if health.get("status") != "ok" or health.get("version") != "0.16.8":
            raise BridgeBlocked("unexpected Letta health/version")
        result["provider_model"], result["letta_health"] = matches[0], health
        for arm in config["arms"]:
            import secrets
            payload, memory = task_payload(config, sample, arm, "ae-wiring-" + arm + "-" + secrets.token_hex(6))
            created = letta_transport.request("POST", "/v1/agents/", payload)
            aid = created.get("id")
            if not isinstance(aid, str) or not aid:
                raise BridgeBlocked("agent creation returned no ID")
            result["arms"][arm] = {"agent_id": aid, "stages": []}
            emit({"kind": "agent_created", "arm": arm, "agent_id": aid, "payload": payload})
            path = "/v1/agents/" + quote(aid, safe="")
            bridge = TaskBridge(CheckedTaskTransport(letta_transport, path, config), aid, memory,
                                max_rounds=config["max_rounds"], max_steps=config["max_steps"],
                                config=config, emit=emit)
            bridges[arm] = bridge
            bridge.verify_session()
            runtimes[arm] = runtime_factory(arm)
        assert_separate_arms(bridges["rewrite"], bridges["erratum"])
        # One active native environment per thread: Vita has thread-local global
        # Store/Product/Location registries, so two environments cannot interleave.
        for task in sample["tasks"]:
            for arm in config["arms"]:
                b, native = bridges[arm], runtimes[arm]
                active = native
                started = time.monotonic()
                prepared = native.start(deepcopy(task))
                if prepared["instruction"] != task["instruction"]:
                    raise BridgeBlocked("native user's first instruction differs from the public task")
                greeting = prepared["greeting"]
                if hasattr(greeting, "model_dump"):
                    greeting = greeting.model_dump(mode="json")
                b.begin(task, greeting)
                bindings = environment_bindings(prepared["environment"])
                emit({"kind": "stage_start", "arm": arm, "task": task["subtask_id"],
                      "public_task": task, "domain_policy": prepared["domain_policy"],
                      "tools": [v.schema for v in bindings.values()], "snapshot": native.snapshot()})
                initial = b.run_stage(task, bindings=bindings, domain_policy=prepared["domain_policy"])
                text = final_public_text(initial["assistant_messages"])
                termination = None
                for _ in range(config["max_user_exchanges"]):
                    if native.agent_stop(text):
                        termination = "agent_stop"
                        break
                    reply = native.user_reply(text)
                    emit({"kind": "simulated_user", "arm": arm, "task": task["subtask_id"], "reply": reply})
                    if reply["stop"]:
                        b.transcript.append({"role": "user", "content": reply["content"]})
                        termination = "user_stop"
                        break
                    text = final_public_text(b.reply_to_user(reply["content"]))
                    if native.agent_stop(text):
                        termination = "agent_stop"
                        break
                if termination is None:
                    raise BridgeBlocked("user exchange limit reached; no completed-task score")
                duration = time.monotonic() - started
                emit({"kind": "task_terminated", "arm": arm, "task": task["subtask_id"],
                      "termination_reason": termination, "transcript": b.transcript,
                      "snapshot": native.snapshot()})
                judged = native.finish(task, deepcopy(b.transcript), termination, duration)
                stage = {"subtask_id": task["subtask_id"], "termination_reason": termination,
                         "duration_seconds": duration, "model_posts": b.stage_posts,
                         "transcript": deepcopy(b.transcript), "diagnostic_evaluation": judged,
                         "memory_writes": deepcopy(b.memory.writes), "block_text": b.memory.block_text,
                         "message_ids": list(b.message_ids)}
                result["arms"][arm]["stages"].append(stage)
                emit({"kind": "stage_complete", "arm": arm, "stage": stage,
                      "snapshot": native.snapshot()})
                active = None
        result["status"] = "WIRING_COMPLETED_AUDIT_PENDING"
        result["wiring_completed"] = True
        # Full rendered-input audit is a separate post-run check, not inferred
        # from message IDs, HTTP 200, or a completed conversation.
    except (Exception, KeyboardInterrupt) as exc:
        reason = {"type": type(exc).__name__, "message": str(exc)}
        result["invalid_reasons"].append(reason)
        if isinstance(exc, KeyboardInterrupt):
            result["interrupted"] = True
            result["server_side_work_cancelled"] = False
        if active is not None:
            try:
                result["partial_native_snapshot"] = active.snapshot()
            except Exception as snapshot_error:
                result["snapshot_error"] = str(snapshot_error)
            finally:
                try:
                    active.abort()
                except Exception as abort_error:
                    result["abort_error"] = str(abort_error)
        emit({"kind": "wiring_stopped", "reason": reason})
    finally:
        for arm, b in bridges.items():
            result["arms"][arm]["bridge_trace"] = list(b.trace)
            result["arms"][arm]["memory_writes"] = deepcopy(b.memory.writes)
            result["arms"][arm]["partial_transcript"] = deepcopy(b.transcript)
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result
