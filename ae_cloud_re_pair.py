"""Single-t4 cloud R/E pair driver (rewrite vs erratum); plan/preflight are offline.

One run creates exactly two fresh Letta agents ("rewrite" = R, "erratum" = E),
gives each the SAME t3-cutoff initial block (which still carries the old
`奶茶偏好5分糖` fact) and the SAME raw t4 dataset_history plus the raw t4
instruction, then lets each agent decide whether and how to update memory. The
driver never fills in 7分糖, never edits the block itself, and never judges the
truth of an update.

The two arms differ only in the existing `ae_adapter.MemoryPolicy` semantics:
R (rewrite) publishes an update with a verified PATCH before the next request
sees it; E (erratum) never PATCHes the initial block and only returns a real
erratum from the update tool. Agent, block, local memory and native environment
are per arm; arms are executed strictly serially because the pinned Vita
thread-local registries cannot interleave. Only t4 is ever started: t5 is
projected for provenance/planning and is never sent or executed.

Not a statistical R/E comparison, not a KV-cache or repeat-update experiment.
No network, secrets, or upstream patching on import; `run` is explicit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import secrets
import time
from urllib.parse import quote

from ae_adapter import MemoryPolicy, assert_separate_arms, creation_payload, dumps
from ae_capability import (EVIDENCE_DIALOGUE_INDEX, EVIDENCE_REF, EVIDENCE_TEXT,
                           TARGET_PRODUCT_ID, TASK_ID, TASK_NUMBER, USER_ID, WORK_ADDRESS_KEY,
                           catalog_products, diagnose_orders, environment_orders,
                           environment_stores)
from ae_cloud_proxy import COMPAT_PROFILE, MODEL
from ae_inputs import history_message, prepare_sample
from ae_multicall import (CONFIG_KEY, MulticallPolicyError, declared_policy,  # noqa: E402
                          resolve_policy)
from ae_task_run import (TaskBridge, CheckedTaskTransport, environment_bindings,
                         final_public_text)


SCHEMA_VERSION = "ae-cloud-re-pair-0.1"
#: Opt-in schema for the versioned multi-client-tool-call receive compatibility.
#: 0.1 stays byte-compatible with the sealed old protocol: no profile field is
#: accepted there, and 0.2 requires the profile to be declared exactly.
SCHEMA_VERSION_MULTICALL = "ae-cloud-re-pair-0.2"
PURPOSE = "single_t4_replacement_pair_diagnostic"
ARMS = ("rewrite", "erratum")
ARM_LABELS = {"rewrite": "R", "erratum": "E"}
ARM_ORDER_SOURCE = "fixed_before_any_result:rewrite_then_erratum"

# The one explicit, reviewed low-frequency combination (identical to the audited
# capability candidate). Only this exact triple may move the driver I/O timeout.
PACING_CANDIDATE = {
    "driver_io_timeout_seconds": 900,
    "proxy_upstream_io_timeout_seconds": 180,
    "min_interval_seconds": 65,
}

# Shared whole-pair request budget requested from the proxy. The proxy's own
# counter is per-process and includes every role; it is NOT reset for the second
# arm. This is a run budget, not an authorization to spend it.
PAIR_MAX_REQUESTS = 256
REQUEST_COUNTING = ("whole pair, every provider role, counted by the proxy's own "
                    "per-process counter, not reset for the second arm")

FIELDS = {
    "schema_version", "purpose", "task_number", "arms", "model_origin", "letta_origin",
    "model_handle", "expected_model", "context_window", "context_window_source",
    "transport_profile", "max_output_tokens", "auxiliary_output_tokens", "temperature",
    "seed", "block_char_limit", "max_rounds", "max_steps", "max_stage_posts",
    "max_user_exchanges", "max_tool_return_chars", "timeout_seconds", "max_request_bytes",
    "max_response_bytes", "max_requests", "pacing",
}
#: The additional explicit field the opt-in 0.2 schema must carry.
MULTICALL_FIELDS = {"multicall_profile"}
_STRING_FIELDS = {"schema_version", "purpose", "model_origin", "letta_origin", "model_handle",
                  "expected_model", "context_window_source", "transport_profile"}
_NUMERIC_FIELDS = {"temperature", "timeout_seconds"}


def _origin(value):
    text = value if isinstance(value, str) else ""
    if not text.startswith("http://127.0.0.1:") or text.rstrip("/") != text:
        raise ValueError("origins must be explicit credential-free loopback URLs")
    return text


def validate_config(config: dict) -> dict:
    """Exact-field RE pair config; nothing is inferred from JSON alone.

    Two schemas exist and each is exact:

    * 0.1 - the sealed old protocol. It must NOT carry a profile field, and
      callers then get the protection behaviour: a multi-call response stops
      the run instead of being truncated to its first call.
    * 0.2 - the opt-in multi-client-tool-call receive compatibility. It MUST
      carry the one reviewed `multicall_profile`, and every other field keeps
      the same pinned values.
    """
    if not isinstance(config, dict) or set(config) - MULTICALL_FIELDS != FIELDS:
        raise ValueError("R/E pair config must contain exactly the declared fields")
    schema = config.get("schema_version")
    if schema == SCHEMA_VERSION:
        if set(config) != FIELDS:
            raise ValueError("the sealed 0.1 schema must not carry the profile field")
        if config.get("multicall_profile") is not None:
            raise ValueError("the sealed 0.1 schema cannot declare a multicall profile")
    elif schema == SCHEMA_VERSION_MULTICALL:
        if set(config) != FIELDS | MULTICALL_FIELDS:
            raise ValueError("the 0.2 schema must declare exactly the profile field once")
    else:
        raise ValueError("wrong R/E pair schema")
    if config["purpose"] != PURPOSE:
        raise ValueError("wrong R/E pair schema or purpose")
    if schema == SCHEMA_VERSION_MULTICALL:
        try:
            resolve_policy(config, allow_absent=False)
        except MulticallPolicyError as exc:
            raise ValueError(f"invalid {CONFIG_KEY}: {exc}") from None
    if config["task_number"] != TASK_NUMBER:
        raise ValueError("the R/E pair is limited to the single t4 subtask")
    if config["arms"] != list(ARMS):
        raise ValueError("the R/E pair arms and their fixed order are not editable")
    for key in ("model_origin", "letta_origin"):
        _origin(config[key])
    if config["model_origin"] == config["letta_origin"]:
        raise ValueError("Letta and model origins must differ")
    if config["model_handle"] != "vllm/" + MODEL or config["expected_model"] != MODEL:
        raise ValueError("the R/E pair targets the one explicit cloud model")
    if config["transport_profile"] != COMPAT_PROFILE:
        raise ValueError("the explicit compatibility transport profile is required")
    if config["context_window"] != 65536:
        raise ValueError("wrong local Agent context budget")
    if config["context_window_source"] != "local_agent_budget_not_server_measurement":
        raise ValueError("context budget provenance required")
    integers = FIELDS - _STRING_FIELDS - _NUMERIC_FIELDS - {"arms", "pacing"}
    for key in integers:
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be an explicit positive integer")
    for key in _NUMERIC_FIELDS:
        value = config[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or (key == "timeout_seconds" and value == 0)):
            raise ValueError(f"invalid {key}")
    if max(config["max_output_tokens"], config["auxiliary_output_tokens"]) >= config["context_window"]:
        raise ValueError("context must leave room for input")
    native_char_limit = max(5000, int(config["context_window"] * 0.8))
    if config["max_tool_return_chars"] > native_char_limit:
        raise ValueError("tool return guard cannot exceed the fixed Letta V3 truncation limit")
    pacing = config["pacing"]
    if pacing is not None:
        if pacing != PACING_CANDIDATE:
            raise ValueError("pacing must be the explicit reviewed combination")
        if config["timeout_seconds"] != PACING_CANDIDATE["driver_io_timeout_seconds"]:
            raise ValueError("the pacing candidate requires its explicit driver I/O timeout")
    elif config["timeout_seconds"] != 180:
        raise ValueError("only the explicit pacing candidate may change the run I/O timeout")
    if config["max_requests"] != PAIR_MAX_REQUESTS:
        raise ValueError("the whole-pair request budget is not the reviewed candidate value")
    return deepcopy(config)


def replacement_evidence(task: dict) -> dict:
    """The public t4 default-replacement evidence record, verified not assumed.

    Reads only the public projection. The record is PRESET history; the driver
    never turns it into the initial block and never checks whether an agent used
    it correctly.
    """
    matches = [item for item in task["history"] if item.get("ref") == EVIDENCE_REF]
    if len(matches) != 1:
        raise ValueError(f"t4 must retain exactly one {EVIDENCE_REF} evidence record")
    record = matches[0]["record"]
    dialogue = record.get("dialogue")
    if not isinstance(dialogue, list) or len(dialogue) <= EVIDENCE_DIALOGUE_INDEX:
        raise ValueError("t4 evidence dialogue is shorter than the replacement turn")
    message = dialogue[EVIDENCE_DIALOGUE_INDEX]
    if (not isinstance(message, dict) or message.get("role") != "user"
            or message.get("content") != EVIDENCE_TEXT):
        raise ValueError("t4 evidence lacks the explicit replacement statement")
    return {"ref": EVIDENCE_REF, "date": record["date"], "dialogue_index": EVIDENCE_DIALOGUE_INDEX,
            "evidence_text": EVIDENCE_TEXT,
            "evidence_text_sha256": hashlib.sha256(EVIDENCE_TEXT.encode("utf-8")).hexdigest(),
            "history_record_canonical_sha256": hashlib.sha256(
                json.dumps(record, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")).hexdigest(),
            "source": "public t4 history only; t4 current preference memory was not read"}


def check_scope(sample: dict) -> dict:
    """Verify the prepared sample is the t4--t5 public projection with t3 cutoffs."""
    if not isinstance(sample, dict):
        raise ValueError("R/E pair requires a verified sample projection")
    tasks = sample.get("tasks")
    if (not isinstance(tasks, list) or [t.get("number") for t in tasks] != [TASK_NUMBER, 5]
            or sample.get("initial_facts") is None or sample.get("initial_profile") is None):
        raise ValueError("R/E pair requires the prepared t4-t5 public projection")
    task = tasks[0]
    if set(task) != {"number", "subtask_id", "domain", "current_time", "instruction", "history"}:
        raise ValueError("t4 must be the public task projection only")
    if task["subtask_id"] != TASK_ID or task["domain"] != "delivery":
        raise ValueError("unexpected t4 identity")
    facts = sample["initial_facts"]
    old = [k for k, v in facts.items() if isinstance(v, dict) and v.get("content") == "奶茶偏好5分糖"]
    if len(old) != 1:
        raise ValueError("the t3 initial block must carry exactly one 奶茶偏好5分糖 fact")
    if any(v.get("content") == "奶茶偏好7分糖" for v in facts.values() if isinstance(v, dict)):
        raise ValueError("the initial block must not already carry the replacement")
    if sample["initial_profile"].get("user_id") != USER_ID:
        raise ValueError("invalid t3 profile")
    evidence = replacement_evidence(task)
    return {"task": deepcopy(task), "facts": deepcopy(facts),
            "profile": deepcopy(sample["initial_profile"]), "evidence": evidence}


def pair_payload(config: dict, scope: dict, arm: str, *, name: str) -> tuple[dict, MemoryPolicy]:
    """Build only; the arm's MemoryPolicy carries the arm semantics.

    The two arms get byte-identical initial blocks and system text; only the
    local `MemoryPolicy.arm` differs, and that difference is never visible to the
    agent as an extra memory channel.
    """
    if arm not in ARMS:
        raise ValueError("unknown R/E arm")
    memory = MemoryPolicy(arm, scope["facts"], block_char_limit=config["block_char_limit"])
    payload = creation_payload(name=name, model=config["model_handle"],
                               profile=scope["profile"], memory=memory)
    # Explicit legacy cloud handle on pinned Letta 0.16.8; no seed is sent.
    payload["context_window_limit"] = config["context_window"]
    payload.pop("model")
    payload["llm_config"] = {
        "model": config["expected_model"], "handle": config["model_handle"],
        "model_endpoint_type": "openai", "model_endpoint": config["model_origin"] + "/v1",
        "provider_name": "vllm", "provider_category": "base",
        "context_window": config["context_window"], "max_tokens": config["max_output_tokens"],
        "temperature": config["temperature"],
        "parallel_tool_calls": False, "strict": False,
        "enable_reasoner": False, "put_inner_thoughts_in_kwargs": False,
    }
    return payload, memory


def private_boundary(sample: dict) -> dict:
    """Declare the private values that must never reach an executing agent.

    The plan is private run evidence (never sent to any agent). Only values the
    public projection already holds separately are declared: the t4 private
    evaluation criteria and target product ids, plus the t5 identity. t4 current
    preference memory is deliberately not read by this driver, so it is covered
    structurally (every model input must derive from a declared public input)
    rather than by a declared gold string.
    """
    private = sample.get("private_tasks") or {}
    t4 = private.get(TASK_ID) or {}
    t5_id = sample["tasks"][1]["subtask_id"]
    t5_private = private.get(t5_id) or {}
    forbidden = []

    def collect(value):
        if isinstance(value, str) and value.strip():
            forbidden.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    # Rubric/scoring text must never reach an executing agent. Target product ids
    # are declared but NOT scanned: a legitimate agent search/tool result may
    # contain them, so scanning would be a false positive. They are excluded
    # structurally instead (the agent only ever sees declared public inputs).
    collect(t4.get("evaluation_criteria"))
    collect(t5_private.get("evaluation_criteria"))
    return {
        "t4_evaluation_criteria_sha256": hashlib.sha256(
            dumps(t4.get("evaluation_criteria")).encode("utf-8")).hexdigest(),
        "t5_evaluation_criteria_sha256": hashlib.sha256(
            dumps(t5_private.get("evaluation_criteria")).encode("utf-8")).hexdigest(),
        "t4_target_product_ids": deepcopy(t4.get("target_product_ids")),
        "t5_subtask_id": t5_id,
        "t5_instruction": sample["tasks"][1]["instruction"],
        "forbidden_value_strings": sorted(set(forbidden)),
        "note": ("scoring/rubric text and the t5 material must not appear on any agent "
                 "wire; target product ids are declared for provenance only and are not "
                 "scanned because a legitimate agent tool result may contain them; t4 "
                 "current preference memory is not read by the driver and is covered by "
                 "exact input-derivation checks instead"),
    }


def build_plan(config: dict, sample: dict, *, code_files) -> dict:
    config = validate_config(config)
    scope = check_scope(sample)
    history = history_message(scope["task"])
    payloads = {arm: pair_payload(config, scope, arm, name=f"ae-re-pair-{arm}-GENERATED")[0]
                for arm in ARMS}
    initial_blocks = {arm: MemoryPolicy(arm, scope["facts"],
                                        block_char_limit=config["block_char_limit"]).initial_block
                      for arm in ARMS}
    if len(set(initial_blocks.values())) != 1:
        raise ValueError("both arms must start from byte-identical blocks")
    boundary = private_boundary(sample)
    plan = {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": "PLAN",
        "network_called": False, "model_called": False, "task_success": None,
        "scientific_result": None, "config": config, "source": deepcopy(sample.get("source")),
        "scope": {
            "user_id": USER_ID, "task_number": TASK_NUMBER, "subtask_id": TASK_ID,
            "t5_executed": False, "dataset_history_sent": True, "memory_update_tool": True,
            "arms": list(ARMS), "arm_order": list(ARMS), "arm_order_source": ARM_ORDER_SOURCE,
            "pair_request_budget": {"max_requests": PAIR_MAX_REQUESTS,
                                    "counting": REQUEST_COUNTING,
                                    "phase": "not started; run budget only"},
            "code_files": sorted(code_files),
        },
        "task_preview": {
            "number": scope["task"]["number"], "subtask_id": scope["task"]["subtask_id"],
            "domain": scope["task"]["domain"], "current_time": scope["task"]["current_time"],
            "instruction": scope["task"]["instruction"],
            "history_records": len(scope["task"]["history"]),
            "history_sent_to_agent": True,
        },
        "inputs": {
            "initial_facts": scope["facts"], "initial_profile": scope["profile"],
            "initial_block": initial_blocks[ARMS[0]], "history_material": history,
            "history_records": deepcopy(scope["task"]["history"]),
            "replacement": scope["evidence"], "private_boundary": boundary,
            "diagnostic_only": False,
        },
        "arms": {arm: {"label": ARM_LABELS[arm], "memory_arm": arm,
                       "agent_payload": payloads[arm],
                       "initial_block": initial_blocks[arm],
                       "initial_block_sha256": hashlib.sha256(
                           initial_blocks[arm].encode("utf-8")).hexdigest()}
                 for arm in ARMS},
        "boundaries": [
            "one fresh Letta agent per arm; identical system, profile, initial block and native t4",
            "both arms receive the SAME raw t4 dataset_history (including the preset replacement evidence) and the raw t4 instruction",
            "the preset history is material, not a driver action: the driver never writes 7分糖 and never judges an update",
            "R publishes an update only after a verified PATCH; E never PATCHes the initial block and returns an erratum instead",
            "arms run strictly serially (R then E) because native Vita registries are thread-local",
            "only t4 is started; t5 remains an unexecuted projection",
            "no seed on the provider wire; seed 300 is simulation metadata only, not deterministic pairing",
            "all three native LLM roles share the selected cloud candidate; judge scores are debug only",
            "no silent summary/truncation/retry; a failed arm stops the pair and keeps the other arm's evidence",
            "unaudited input can never be marked VALID by this driver",
        ],
    }
    return plan


def execute_re_pair(config: dict, sample: dict, *, model_transport, letta_transport,
                    runtime_factory, emit=lambda _: None) -> dict:
    """Run one t4 for each arm, serially and without retry or repair."""
    config = validate_config(config)
    scope = check_scope(sample)
    task = scope["task"]
    # The versioned receive policy, resolved once from the validated config.
    policy = resolve_policy(config)
    result = {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": "INVALID",
        "execution_complete": False, "validity_passed": False, "input_audit_passed": None,
        "re_pair_passed": None, "task_success": None, "scientific_result": None,
        "config": config, "source": deepcopy(sample.get("source")),
        "scope": {"user_id": USER_ID, "task_number": TASK_NUMBER, "subtask_id": TASK_ID,
                  "t5_executed": False, "dataset_history_sent": True, "memory_update_tool": True,
                  "arms": list(ARMS), "arm_order": list(ARMS),
                  # Both arms get the identical rule; a null policy means the
                  # sealed protocol, where a multi-call response stops the run.
                  "multicall_profile": (None if policy is None else policy.as_dict())},
        "arms": {}, "pair_order": list(ARMS), "stopped_after_arm": None,
        "agent_cleanup_performed": False, "invalid_reasons": [],
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    bridges, runtimes, memories = {}, {}, {}
    active = None
    last_arm = None
    try:
        listed = model_transport.request("GET", "/v1/models")
        matches = [m for m in listed.get("data", []) if isinstance(m, dict) and m.get("id") == MODEL]
        if len(matches) != 1:
            raise RuntimeError("provider catalog does not expose exactly one selected cloud model")
        health = letta_transport.request("GET", "/v1/health/")
        if health.get("status") != "ok" or health.get("version") != "0.16.8":
            raise RuntimeError("unexpected Letta health/version")
        result["provider_model"], result["letta_health"] = deepcopy(matches[0]), deepcopy(health)

        for arm in ARMS:
            payload, memory = pair_payload(config, scope, arm,
                                           name="ae-re-pair-cloud-" + arm + "-" + secrets.token_hex(6))
            created = letta_transport.request("POST", "/v1/agents/", payload)
            aid = created.get("id")
            if not isinstance(aid, str) or not aid:
                raise RuntimeError("agent creation returned no ID")
            path = "/v1/agents/" + quote(aid, safe="")
            bridge = TaskBridge(CheckedTaskTransport(letta_transport, path, config), aid, memory,
                                max_rounds=config["max_rounds"], max_steps=config["max_steps"],
                                config=config, emit=emit, include_memory_tool=True,
                                multicall=policy)
            bridges[arm], memories[arm] = bridge, memory
            bridge.verify_session()
            result["arms"][arm] = {"label": ARM_LABELS[arm], "memory_arm": arm, "agent_id": aid,
                                   "block_id": bridge.block_id, "model_posts": 0,
                                   "termination_reason": None, "duration_seconds": None,
                                   "transcript": [], "history_reply": None, "patches": [],
                                   "memory_writes": [], "final_block": memory.block_text,
                                   "final_block_sha256": None,
                                   "judge": {"status": "NOT_RUN", "reward_info": None, "error": None},
                                   "task_oracle": None}
            emit({"kind": "agent_created", "arm": arm, "label": ARM_LABELS[arm],
                  "agent_id": aid, "block_id": bridge.block_id, "payload": deepcopy(payload)})
            runtimes[arm] = runtime_factory(arm)

        assert_separate_arms(bridges[ARMS[0]], bridges[ARMS[1]])

        for arm in ARMS:
            last_arm = arm
            bridge, native = bridges[arm], runtimes[arm]
            active = native
            started_clock = time.monotonic()
            prepared = native.start(deepcopy(task))
            if prepared["instruction"] != task["instruction"]:
                raise RuntimeError("native user's first instruction differs from the public task")
            greeting = prepared["greeting"]
            if hasattr(greeting, "model_dump"):
                greeting = greeting.model_dump(mode="json")
            environment = prepared["environment"]
            baseline_order_ids = set(environment_orders(environment))
            bridge.begin(task, greeting)
            bindings = environment_bindings(environment)
            emit({"kind": "stage_start", "arm": arm, "task": task["subtask_id"],
                  "public_task": deepcopy(task), "domain_policy": prepared["domain_policy"],
                  "tools": [v.schema for v in bindings.values()],
                  "replacement_ref": scope["evidence"]["ref"], "snapshot": native.snapshot()})

            staged = bridge.run_stage(task, bindings=bindings,
                                      domain_policy=prepared["domain_policy"])
            result["arms"][arm]["history_reply"] = deepcopy(staged["history_reply"])
            text = final_public_text(staged["assistant_messages"])
            termination = None
            for _ in range(config["max_user_exchanges"]):
                if native.agent_stop(text):
                    termination = "agent_stop"
                    break
                user_reply = native.user_reply(text)
                emit({"kind": "simulated_user", "arm": arm, "task": task["subtask_id"],
                      "reply": user_reply})
                if user_reply["stop"]:
                    bridge.transcript.append({"role": "user", "content": user_reply["content"]})
                    termination = "user_stop"
                    break
                text = final_public_text(bridge.reply_to_user(user_reply["content"]))
                if native.agent_stop(text):
                    termination = "agent_stop"
                    break
            if termination is None:
                raise RuntimeError("user exchange limit reached; no completed-task score")

            duration = time.monotonic() - started_clock
            captured_orders = environment_orders(environment)
            captured_stores = environment_stores(environment)
            record = result["arms"][arm]
            record.update({
                "termination_reason": termination, "duration_seconds": duration,
                "model_posts": bridge.stage_posts, "transcript": deepcopy(bridge.transcript),
                "memory_writes": deepcopy(memories[arm].writes),
                "final_block": memories[arm].block_text,
                "final_block_sha256": hashlib.sha256(
                    memories[arm].block_text.encode("utf-8")).hexdigest(),
                "patches": [deepcopy(step["body"]) for step in bridge.trace
                            if step.get("kind") == "request"
                            and step.get("method") == "PATCH"],
                "task_oracle": diagnose_orders(
                    captured_orders, baseline_order_ids=baseline_order_ids,
                    stores=captured_stores, work_address=scope["profile"][WORK_ADDRESS_KEY]),
                "native_snapshot": native.snapshot(),
                # One record per validated approval batch, in execution order.
                "multicall_batches": deepcopy(bridge.multicall_batches),
            })
            try:
                judged = native.finish(task, deepcopy(bridge.transcript), termination, duration)
                record["judge"] = {"status": judged.get("judging_status", "MODEL_JUDGED_DEBUG_ONLY"),
                                   "reward_info": deepcopy(judged.get("reward_info")),
                                   "scientific_success": None, "error": None}
            except Exception as exc:
                # A failed native evaluation chain is an execution-boundary
                # failure: record it, release this arm's environment, keep every
                # order/transcript/update/native capture already made, and STOP
                # the pair. The second arm is never started and completion is
                # never claimed. A low score is NOT this case.
                record["judge"] = {"status": "JUDGE_FAILED_ORDERS_RETAINED", "reward_info": None,
                                   "scientific_success": None,
                                   "error": {"type": type(exc).__name__, "message": str(exc)}}
                try:
                    active.abort()
                    result["native_aborted_after_judge_failure"] = True
                except Exception as abort_error:
                    result["abort_error"] = str(abort_error)
                active = None
                raise RuntimeError(
                    "native evaluation failed; the pair stops after arm " + arm) from exc
            emit({"kind": "arm_complete", "arm": arm, "label": ARM_LABELS[arm],
                  "task": task["subtask_id"], "termination_reason": termination,
                  "transcript": deepcopy(bridge.transcript),
                  "memory_writes": deepcopy(memories[arm].writes),
                  "final_block_sha256": record["final_block_sha256"],
                  "task_oracle": deepcopy(record["task_oracle"]), "snapshot": native.snapshot()})
            active = None

        result["execution_complete"] = True
        result["status"] = "RE_PAIR_COMPLETED_AUDIT_PENDING"
    except (Exception, KeyboardInterrupt) as exc:
        result["invalid_reasons"].append({"type": type(exc).__name__, "message": str(exc)})
        result["stopped_after_arm"] = last_arm
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
        emit({"kind": "pair_stopped", "stopped_after_arm": last_arm,
              "reason": deepcopy(result["invalid_reasons"][-1])})
    finally:
        for arm, bridge in bridges.items():
            record = result["arms"].setdefault(arm, {"label": ARM_LABELS[arm], "memory_arm": arm})
            record["bridge_trace"] = list(bridge.trace)
            record.setdefault("memory_writes", deepcopy(bridge.memory.writes))
            record.setdefault("partial_transcript", deepcopy(bridge.transcript))
            record.setdefault("agent_id", bridge.agent_id)
            record.setdefault("block_id", bridge.block_id)
            record["trace_message_ids"] = list(bridge.message_ids)
            # A pair that stopped mid-batch must still show the batches that did
            # validate, exactly once, without inventing a completed one.
            record.setdefault("multicall_batches", deepcopy(bridge.multicall_batches))
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def preflight(sample: dict, config: dict) -> dict:
    """Offline checks that need no service, model or socket (caller blocks sockets).

    Verifies the two arms start from byte-identical correct t3 blocks, that the
    raw 26-record t4 history (with the preset t4/history/15 replacement) is the
    same material for both arms, that no future/t5/private input enters, and that
    the native t4 preview keeps the normal environment tools and no second memory
    backend. It never starts t5 and never calls a tool or a model.
    """
    config = validate_config(config)
    scope = check_scope(sample)
    task = scope["task"]
    history = history_message(task)
    blocks = {arm: MemoryPolicy(arm, scope["facts"],
                                block_char_limit=config["block_char_limit"]).initial_block
              for arm in ARMS}
    checks = {
        "arms": list(ARMS),
        "arm_order": list(ARMS),
        "identical_initial_block": len(set(blocks.values())) == 1,
        "initial_block_sha256": {arm: hashlib.sha256(blocks[arm].encode()).hexdigest()
                                 for arm in ARMS},
        "t3_old_fact_present": "奶茶偏好5分糖" in blocks[ARMS[0]],
        "t3_replacement_absent": "奶茶偏好7分糖" not in blocks[ARMS[0]],
        "history_records": len(task["history"]),
        "history_refs_in_order": [item["ref"] for item in task["history"]],
        "history_material_sha256": hashlib.sha256(
            history["content"].encode("utf-8")).hexdigest(),
        "replacement_evidence": scope["evidence"],
        "t5_not_started": True,
        "task_number": task["number"],
    }
    if checks["history_records"] != 26:
        raise RuntimeError("the raw t4 material must retain its 26 history records")
    if [item["ref"] for item in task["history"]] != [f"t4/history/{i}" for i in range(26)]:
        raise RuntimeError("t4 history order/refs changed")
    blob = json.dumps(history["content"], ensure_ascii=False)
    if "t5/" in blob or "sub_U000828_5" in blob:
        raise RuntimeError("future turn leaked into the t4 history material")
    private = private_boundary(sample)
    if private["t4_target_product_ids"] and TARGET_PRODUCT_ID not in private["t4_target_product_ids"]:
        raise RuntimeError("prepared private t4 identity differs from the audited target")
    return {"checks": checks, "private_boundary": private, "config": config}


RE_CODE_FILES = ("ae_inputs.py", "ae_adapter.py", "ae_http.py", "ae_probe.py", "ae_task_run.py",
                 "ae_vita.py", "ae_capability.py", "ae_cloud_task.py", "ae_cloud_proxy.py",
                 "ae_cloud_re_pair.py", "ae_cloud_re_input_audit.py",
                 # The receive compatibility policy is production code for the 0.2
                 # protocol: it decides what the runtime keeps, so the plan, the
                 # manifest and the audit all have to see its digest.
                 "ae_multicall.py",
                 "scripts/ae_01_cloud_re_pair.py", "scripts/ae_01_cloud_re_input_audit.py")


def re_code_files(root=None):
    """The RE production code file set, optionally hashed from a given root."""
    import hashlib
    from pathlib import Path as _Path
    base = _Path(root) if root else _Path(__file__).resolve().parent
    return {name: hashlib.sha256((base / name).read_bytes()).hexdigest()
            for name in RE_CODE_FILES}
