"""Original-data continuous t4->t12 cloud R/E pair driver (rewrite vs erratum).

Offline PLAN/PREFLIGHT are network-free. Only an explicit ``run`` contacts the
loopback model and Letta services.

One run creates exactly TWO persistent Letta agents -- "rewrite" (R) and
"erratum" (E) -- both initialized ONCE from the same verified t3 cutoff (which
still carries the old ``奶茶偏好5分糖`` fact). Each arm then walks the ORIGINAL
consecutive subtasks of U000828 from t4 to t12 (9 tasks) using the arm's own
single persistent agent, block and local memory. The fixed arm order is: R's
whole t4->t12 sequence first, then E's; the pinned Vita thread-local registries
cannot interleave, so no task of one arm is ever interleaved with the other.

Per task the driver appends ONLY that task's original ``interactions`` records
and its ``current_task`` instruction. Prior batches are never resent, future
tasks are never sent, and the driver never fills in a preference, never edits
the block itself and never judges the truth of an update. The two arms differ
only in the existing ``ae_adapter.MemoryPolicy`` semantics: R publishes an
update with a verified PATCH before the next request sees it; E never PATCHes
the initial block and only returns a real erratum from the update tool.

Environment continuity is declared honestly and is NOT claimed to be real-world
state continuity. The pinned Vita wrapper builds a FRESH environment for every
task from that task's own recorded environment snapshot and clears the native
thread-local store/order/location registries before each task, so a previous
task's orders are never injected into a later task. What is continuous across
tasks is the AGENT side only: agent identity, memory block, E's erratum
messages, ordinary chat and tool interactions.

Not a statistical R/E comparison, not a KV-cache or repeat-update experiment,
and not an official benchmark result. No network, secrets or upstream patching
on import; ``run`` is explicit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from ae_adapter import (MemoryPolicy, assert_separate_arms, creation_payload, dumps)
from ae_capability import (USER_ID, WORK_ADDRESS_KEY, diagnose_orders,
                           environment_orders, environment_stores)
from ae_cloud_proxy import (BYTE_GATE_COUNT_BASIS, COMPAT_PROFILE, MODEL, PROFILES,
                            profile_of)
from ae_http import JSONHTTPTransport
from ae_inputs import USER_INDEX, canonical_sha256, history_message, prepare_sample
from ae_multicall import (CONFIG_KEY, MulticallPolicyError, declared_policy,
                          resolve_policy)
from ae_sim_eval_protocol import (EVALUATION_PROTOCOL, PROTOCOL_BUNDLE_FIELDS,
                                  SimulatorProtocolViolation, USER_SIMULATOR_PROTOCOL,
                                  build_evaluation_report, make_protocol_bundle,
                                  protocol_bundle_sha256, validate_protocol_bundle)
from ae_task_run import (TaskBridge, CheckedTaskTransport, environment_bindings,
                         final_public_text)

#: The repository root, so a by-path module (the DeepSeek integration layer) can be located
#: without making it a package member.
ROOT = Path(__file__).resolve().parent


SCHEMA_VERSION = "ae-cloud-re-multiturn-0.1"
#: Opt-in schema for the versioned multi-client-tool-call receive compatibility.
#: 0.1 stays the sealed protocol (no profile field; a multi-call response stops
#: the run). 0.2 requires the reviewed profile to be declared exactly.
SCHEMA_VERSION_MULTICALL = "ae-cloud-re-multiturn-0.2"
#: Opt-in schema for the explicit capacity + no-compaction contract. It keeps the
#: reviewed multicall profile AND declares the Agent context window with its
#: verification state, the lossy-history policy and the request-time capacity gate.
#: 0.1/0.2 stay exactly as sealed: this schema is the only one that may move the
#: context window, and it never changes any other frozen budget.
SCHEMA_VERSION_CAPACITY = "ae-cloud-re-multiturn-0.3"
#: 0.4 is the DeepSeek exploratory protocol. It is a SEPARATE schema on purpose: 0.3's
#: capacity block requires the official tokenizer basis and stays exactly as sealed, while
#: 0.4 declares a provider with no verified tokenizer and is bounded by a request-byte gate
#: under an explicit, non-guaranteeing exploratory option.
SCHEMA_VERSION_DEEPSEEK = "ae-cloud-re-multiturn-0.4"
DEEPSEEK_CAPACITY_FIELDS = {"capacity", "exploratory_capacity_protocol"}
#: 0.5 is 0.4 PLUS the two repair protocols (the user-simulator role boundary and the
#: separated evaluation). It is a SEPARATE schema on purpose: 0.4's declaration and every
#: sealed 0.4 run stay exactly as they are, and a run that does not declare a protocol
#: bundle cannot silently acquire one.
SCHEMA_VERSION_SIM_EVAL = "ae-cloud-re-multiturn-0.5"
#: The two fields 0.5 adds. Both are REQUIRED by 0.5 and refused by every other schema,
#: so "which simulator played the user" is never left to a default.
REPAIR_PROTOCOL_FIELDS = {"simulator_protocol", "evaluation_protocol"}
#: The one field that moves the ADDITIONAL deterministic evaluation between "computed
#: inside the run" and "left to an offline diagnostic". It is a 0.5-only field and it is
#: EXPLICIT: no schema defaults it, and the sealed 0.1-0.4 declarations cannot carry it,
#: so an old candidate never changes behaviour.
DETERMINISTIC_EVALUATION_FIELD = "deterministic_evaluation"
DETERMINISTIC_EVALUATION_IN_RUN = "in_run"
DETERMINISTIC_EVALUATION_OFFLINE = "offline_diagnostic"
DETERMINISTIC_EVALUATION_MODES = (DETERMINISTIC_EVALUATION_IN_RUN,
                                  DETERMINISTIC_EVALUATION_OFFLINE)
SIM_EVAL_FIELDS = REPAIR_PROTOCOL_FIELDS | {DETERMINISTIC_EVALUATION_FIELD}
#: Every schema whose provider contract is the DeepSeek byte-gate protocol.
DEEPSEEK_SCHEMAS = (SCHEMA_VERSION_DEEPSEEK, SCHEMA_VERSION_SIM_EVAL)
#: The private marker that carries the EXPLICIT exploratory opt-in from the CLI into the
#: driver's own re-validation. It is not a config field: `validate_config` pops it and it is
#: never written into a run record. Only the two known literals are accepted.
EXPLORATORY_MARKER = "_exploratory_capacity_option"
EXPLORATORY_OPTION = "--exploratory-capacity-option"
_RESEALED = (EXPLORATORY_OPTION, None)
#: The one reviewed no-compaction policy version. The string is what the Letta
#: creation payload carries as an agent tag and what the pinned service gate reads.
NO_COMPACTION_POLICY = "ae-no-compaction-1"
NO_COMPACTION_TAG = "ae-no-compaction:" + NO_COMPACTION_POLICY.rsplit("-", 1)[-1]
#: The two count bases the request-time gate may use. `pinned_tokenizer` means the
#: service counts the actual request with Letta's own token counter; the byte bound
#: is the declared conservative fallback used only when that count is unavailable.
#: The reviewed counting bases. `official_qwen_tokenizer` is the authoritative one;
#: `pinned_tokenizer` (Letta's own counter) is allowed only as a second, larger vote.
#: There is deliberately no byte-ratio basis: a ratio can under-estimate.
CAPACITY_COUNT_BASES = ("official_qwen_tokenizer", "pinned_tokenizer")
CAPACITY_EVIDENCE_STATES = ("verified", "unverified")
#: Where a declared window may come from. `published_native` is a documentation
#: bound (the model card), never a claim about the endpoint that will serve the run.
WINDOW_SOURCES = ("published_native", "endpoint_measured", "local_agent_budget")
PURPOSE = "original_multiturn_replacement_pair"
ARMS = ("rewrite", "erratum")
ARM_LABELS = {"rewrite": "R", "erratum": "E"}
ARM_ORDER_SOURCE = "fixed_before_any_result:rewrite_t4_to_t12_then_erratum_t4_to_t12"
START_TURN = 4
#: The fixed original-data upper bound. The dataset projection itself accepts
#: 5 or 12; this driver is the one that pins the continuous 9-task scope.
END_TURN = 12
TASK_NUMBERS = tuple(range(START_TURN, END_TURN + 1))
#: The only other verified projection bound: the bounded t4--t5 pilot scope.
PILOT_END_TURN = 5
PILOT_END_TURN_SET = (PILOT_END_TURN, END_TURN)
#: 18 = 9 tasks x 2 arms; the fixed phase order is R(t4..t12), then E(t4..t12).
PHASE_COUNT = len(TASK_NUMBERS) * len(ARMS)

# The one explicit, reviewed low-frequency combination (identical to the audited
# single-t4 capability candidate).
PACING_CANDIDATE = {
    "driver_io_timeout_seconds": 900,
    "proxy_upstream_io_timeout_seconds": 180,
    "min_interval_seconds": 65,
}

# Shared whole-pair request budget requested from the proxy. The proxy's own
# counter is per-process and includes every role and every task of BOTH arms; it
# is NOT reset per task and NOT reset for the second arm. This is a run budget,
# not an authorization to spend it.
PAIR_MAX_REQUESTS = 256
REQUEST_COUNTING = ("whole pair, every provider role, every task of both arms, counted by "
                    "the proxy's own per-process counter, never reset per task or per arm")

FIELDS = {
    "schema_version", "purpose", "start_turn", "end_turn", "arms", "model_origin",
    "letta_origin", "model_handle", "expected_model", "context_window",
    "context_window_source", "transport_profile", "max_output_tokens",
    "auxiliary_output_tokens", "temperature", "seed", "block_char_limit",
    "max_rounds", "max_steps", "max_stage_posts", "max_user_exchanges",
    "max_tool_return_chars", "timeout_seconds", "max_request_bytes",
    "max_response_bytes", "max_requests", "pacing",
}
MULTICALL_FIELDS = {"multicall_profile"}
#: The explicit transport mapping a non-default profile must record. Both are optional
#: for the default SiliconFlow transport (where wire model == internal alias and there is
#: no provider route block), and REQUIRED when the declared profile differs - so the run
#: record always carries the identity the wire request will use.
TRANSPORT_FIELDS = {"upstream_model", "provider_slug"}
#: The 0.3 schema's own block. Every member is explicit: a missing member is a
#: malformed candidate, never a silent default.
CAPACITY_FIELDS = {"capacity"}
CAPACITY_MEMBERS = {"context_window", "window_source", "service_limit", "verification",
                    "output_reserve_tokens", "auxiliary_reserve_tokens", "no_compaction",
                    "count_basis", "tokenizer", "derived_caps"}
TOKENIZER_MEMBERS = {"target", "asset_target", "asset_evidence", "exploration_only"}
SERVICE_LIMIT_MEMBERS = {"published_native_context_tokens", "endpoint_measured_tokens",
                         "endpoint_measurement", "published_sources"}
DERIVED_CAP_MEMBERS = {"native_tool_return_truncation_chars", "tool_return_guard_chars"}
_STRING_FIELDS = {"schema_version", "purpose", "model_origin", "letta_origin", "model_handle",
                  "expected_model", "context_window_source", "transport_profile"}
_NUMERIC_FIELDS = {"temperature", "timeout_seconds"}


def stage_clarification_for(*, environment, tools, domain, subtask_id) -> dict:
    """Derive ONE stage's clarification from that stage's own native environment.

    Recorded with the stage identity it was derived for, so an audit can see which
    agent/task/request the clock belonged to instead of trusting a global current value.
    """
    module = _deepseek_transport()
    _system, _tools, record = module.stage_clarification(
        environment=environment, system_message="stage", tools=tools, domain=domain)
    clock = record["clock"]
    return {"environment_now": clock["environment_now"],
            "clock_format": clock["format"],
            "clock_source": clock["source"],
            "same_clock_the_tools_validate_with": clock["same_clock_the_tools_validate_with"],
            "host_wall_clock_used": clock["host_wall_clock_used"],
            "hardcoded_value_used": clock["hardcoded_value_used"],
            "attributes_clarification": (record.get("schema_change") or {}).get(
                "clarification"),
            "attributes_tool": "create_delivery_order",
            "attributes_note": record["attributes_note"],
            "domain": domain, "subtask_id": subtask_id,
            "bound_to": "this agent, this task, this request"}


def _deepseek_transport():
    """The DeepSeek integration module, loaded by path (it is not a package member).

    A DeepSeek run's own contract - identity, serial pacing and the non-guaranteeing
    capacity protocol - is validated THERE, so the sealed 0.3 rules below are never
    loosened to accommodate a different provider.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ae_deepseek_re_transport", ROOT / "ae_deepseek_re_transport.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_capacity(capacity: dict, config: dict) -> dict:
    """Validate the 0.3 explicit capacity + no-compaction contract, exactly.

    The block is the ONLY place a candidate may move the context window, and it must
    say where the number comes from, whether it is verified, what output it reserves,
    which lossy-history policy is active and how each request is counted. Nothing is
    defaulted: an undeclared member is a malformed candidate.
    """
    if not isinstance(capacity, dict) or set(capacity) != CAPACITY_MEMBERS:
        raise ValueError("the capacity block must declare exactly its reviewed members")
    window = capacity["context_window"]
    if type(window) is not int or window <= 0:
        raise ValueError("capacity.context_window must be an explicit positive integer")
    if config["context_window"] != window:
        raise ValueError("config.context_window and capacity.context_window must agree")
    if capacity["window_source"] not in WINDOW_SOURCES:
        raise ValueError("capacity.window_source is not a reviewed source")
    service = capacity["service_limit"]
    if not isinstance(service, dict) or set(service) != SERVICE_LIMIT_MEMBERS:
        raise ValueError("capacity.service_limit must declare exactly its reviewed members")
    published = service["published_native_context_tokens"]
    measured = service["endpoint_measured_tokens"]
    if not (published is None or (type(published) is int and published > 0)):
        raise ValueError("service_limit.published_native_context_tokens must be null or positive")
    if not (measured is None or (type(measured) is int and measured > 0)):
        raise ValueError("service_limit.endpoint_measured_tokens must be null or positive")
    if not isinstance(service["endpoint_measurement"], str) or not service["endpoint_measurement"]:
        raise ValueError("service_limit.endpoint_measurement must name its evidence")
    sources = service["published_sources"]
    if not isinstance(sources, list) or not all(isinstance(item, str) and item for item in sources):
        raise ValueError("service_limit.published_sources must be a list of sources")
    if published is not None and not sources:
        raise ValueError("a published native context must cite its source")
    verification = capacity["verification"]
    if verification not in CAPACITY_EVIDENCE_STATES:
        raise ValueError("capacity.verification must be verified or unverified")
    # The endpoint is verified only by a measured value; a published number is a
    # candidate bound and can never be recorded as a verified endpoint capacity.
    if verification == "verified" and measured is None:
        raise ValueError("a verified endpoint capacity requires an endpoint measurement")
    reserve = capacity["output_reserve_tokens"]
    if type(reserve) is not int or reserve <= 0:
        raise ValueError("capacity.output_reserve_tokens must be an explicit positive integer")
    if reserve != config["max_output_tokens"]:
        raise ValueError("the capacity output reserve must be the frozen agent output limit")
    if reserve >= window:
        raise ValueError("the context window must leave room for input")
    if capacity["no_compaction"] != NO_COMPACTION_POLICY:
        raise ValueError("the 0.3 candidate requires the reviewed no-compaction policy")
    basis = capacity["count_basis"]
    if not isinstance(basis, list) or not basis or set(basis) - set(CAPACITY_COUNT_BASES):
        raise ValueError("capacity.count_basis must list reviewed count bases")
    if "official_qwen_tokenizer" not in basis:
        raise ValueError("the capacity gate requires the official tokenizer count basis")
    tokenizer = capacity["tokenizer"]
    if not isinstance(tokenizer, dict) or set(tokenizer) != TOKENIZER_MEMBERS:
        raise ValueError("capacity.tokenizer must declare exactly its reviewed members")
    if not isinstance(tokenizer["target"], str) or tokenizer["target"] != config["expected_model"]:
        raise ValueError("capacity.tokenizer.target must be the run's own model")
    if not isinstance(tokenizer["asset_target"], str) or not tokenizer["asset_target"]:
        raise ValueError("capacity.tokenizer.asset_target must name the asset's own model")
    if not isinstance(tokenizer["asset_evidence"], str) or not tokenizer["asset_evidence"]:
        raise ValueError("capacity.tokenizer.asset_evidence must name how the asset was identified")
    if type(tokenizer["exploration_only"]) is not bool:
        raise ValueError("capacity.tokenizer.exploration_only must be an explicit bool")
    if tokenizer["asset_target"] != tokenizer["target"] and not tokenizer["exploration_only"]:
        raise ValueError("an asset that is not the target model may only be used as an "
                         "explicitly labelled exploration")
    auxiliary = capacity["auxiliary_reserve_tokens"]
    if type(auxiliary) is not int or auxiliary <= 0:
        raise ValueError("capacity.auxiliary_reserve_tokens must be an explicit positive integer")
    if auxiliary != config["auxiliary_output_tokens"]:
        raise ValueError("the auxiliary reserve must be the frozen auxiliary output limit")
    caps = capacity["derived_caps"]
    if not isinstance(caps, dict) or set(caps) != DERIVED_CAP_MEMBERS:
        raise ValueError("capacity.derived_caps must declare the derived limits")
    native = caps["native_tool_return_truncation_chars"]
    guard = caps["tool_return_guard_chars"]
    # The derived limits are recomputed from the window and must match the frozen
    # rule (0.8 of the window) and the frozen guard; a candidate may not smuggle a
    # different tool-return budget in through the capacity block.
    expected_native = max(5000, int(window * 0.8))
    if native != expected_native:
        raise ValueError("the derived native truncation limit is not 0.8 of the window")
    if guard != config["max_tool_return_chars"]:
        raise ValueError("the tool return guard must stay the frozen value")
    if guard > native:
        raise ValueError("the tool return guard cannot exceed the derived native limit")
    return deepcopy(capacity)


def capacity_derived_caps(window: int, guard: int) -> dict:
    """The limits a window implies, so a change is visible instead of incidental."""
    native = max(5000, int(window * 0.8))
    # Letta's own per-return cap, from the pinned source: 20% of the window in
    # tokens, read as chars (letta/agents/letta_agent_v3.py::_compute_tool_return_truncation_chars).
    letta = max(5000, int(window * 0.2 * 4))
    return {"native_tool_return_truncation_chars": native,
            "tool_return_guard_chars": guard,
            "letta_tool_return_truncation_chars": letta,
            "agent_output_reserve_tokens": None}


def _origin(value):
    text = value if isinstance(value, str) else ""
    if not text.startswith("http://127.0.0.1:") or text.rstrip("/") != text:
        raise ValueError("origins must be explicit credential-free loopback URLs")
    return text


def validate_run_protocols(config: dict) -> dict:
    """The repair protocol bundle a run declares, or an explicit refusal.

    Both arms are handed the SAME bundle object: this function is the only way a run
    obtains one, and `_run_one_phase` passes that one object to whichever arm's
    runtime is running. There is deliberately no per-arm form, so R and E cannot be
    measured under different simulator or evaluation rules.
    """
    bundle = validate_protocol_bundle({key: config.get(key)
                                       for key in PROTOCOL_BUNDLE_FIELDS})
    return bundle


def deterministic_evaluation_mode(config: dict) -> str:
    """Where the ADDITIONAL deterministic evaluation runs for this declaration.

    `in_run` (the default for a 0.5 candidate that says nothing) computes it inside the
    run. `offline_diagnostic` skips it: the run then records explicitly that the extra
    evaluation did NOT run and is pending manual review, and never claims it passed. A
    sealed 0.1-0.4 declaration cannot carry the field at all, so its behaviour is
    untouched.
    """
    if config.get("schema_version") not in (SCHEMA_VERSION_SIM_EVAL,):
        return DETERMINISTIC_EVALUATION_IN_RUN
    return config.get(DETERMINISTIC_EVALUATION_FIELD,
                      DETERMINISTIC_EVALUATION_IN_RUN)


def run_protocols_of(config: dict) -> dict | None:
    """The declared bundle, or None for a schema that declares none (0.1-0.4)."""
    if config.get("schema_version") not in (SCHEMA_VERSION_SIM_EVAL,):
        return None
    return validate_run_protocols(config)


def validate_config(config: dict, *, exploratory_capacity_option: str | None = None) -> dict:
    # The marker travels with the config so every driver entry re-validates with the SAME
    # opt-in the CLI was given; an unknown value is a refusal, not a silent default.
    if isinstance(config, dict) and EXPLORATORY_MARKER in config:
        config = dict(config)
        carried = config.pop(EXPLORATORY_MARKER)
        if carried not in _RESEALED:
            raise ValueError("unknown exploratory capacity option")
        if exploratory_capacity_option is None:
            exploratory_capacity_option = carried
        # The marker is private to the hand-over and is NEVER part of the declaration, so
        # it is also stripped from the copy the caller's own record should carry.
        if isinstance(config.get("config"), dict):
            config["config"] = {key: value for key, value in config["config"].items()
                                if key != EXPLORATORY_MARKER}
    """Exact-field continuous multiturm R/E pair config; nothing is inferred.

    Two schemas exist and each is exact:

    * 0.1 - the sealed protocol. It must NOT carry a profile field, and callers
      then get the protection behaviour: a multi-call response stops the run
      instead of being truncated to its first call.
    * 0.2 - the opt-in multi-client-tool-call receive compatibility. It MUST
      carry the one reviewed ``multicall_profile``; every other field keeps the
      same pinned values.
    """
    if not isinstance(config, dict) or set(config) - MULTICALL_FIELDS - CAPACITY_FIELDS \
            - TRANSPORT_FIELDS - DEEPSEEK_CAPACITY_FIELDS - SIM_EVAL_FIELDS != FIELDS:
        raise ValueError("multiturn R/E pair config must contain exactly the declared fields")
    schema = config.get("schema_version")
    if schema == SCHEMA_VERSION:
        if set(config) != FIELDS:
            raise ValueError("the sealed 0.1 schema must not carry the profile field")
        if config.get("multicall_profile") is not None:
            raise ValueError("the sealed 0.1 schema cannot declare a multicall profile")
    elif schema == SCHEMA_VERSION_MULTICALL:
        if set(config) != FIELDS | MULTICALL_FIELDS:
            raise ValueError("the 0.2 schema must declare exactly the profile field once")
    elif schema == SCHEMA_VERSION_CAPACITY:
        # A 0.3 run may also record the explicit transport mapping. It is optional for
        # the default transport (its wire model IS the internal alias and it has no
        # provider route) and required by the profile check below when it differs.
        if set(config) not in (FIELDS | MULTICALL_FIELDS | CAPACITY_FIELDS,
                               FIELDS | MULTICALL_FIELDS | CAPACITY_FIELDS | TRANSPORT_FIELDS):
            raise ValueError("the 0.3 schema must declare exactly the profile and capacity fields"
                             " (plus the optional transport mapping)")
    elif schema == SCHEMA_VERSION_DEEPSEEK:
        if set(config) not in (FIELDS | MULTICALL_FIELDS | DEEPSEEK_CAPACITY_FIELDS
                               | TRANSPORT_FIELDS,
                               FIELDS | MULTICALL_FIELDS | DEEPSEEK_CAPACITY_FIELDS):
            raise ValueError("the 0.4 schema must declare exactly the profile and its "
                             "DeepSeek capacity members")
        # The provider's own contract is checked in ITS module, so the sealed 0.3 rules
        # below are never loosened to accommodate a different provider.
        _deepseek_transport().validate_deepseek_multiturn_config(
            config, option=exploratory_capacity_option)
    elif schema == SCHEMA_VERSION_SIM_EVAL:
        # The repair schema is 0.4's declaration PLUS exactly the two protocol fields.
        # Both are required: a 0.5 run cannot omit the simulator protocol (which would
        # silently fall back to the unconstrained native simulator) or the evaluation
        # protocol (which would silently fall back to the unsplit report).
        # The two protocol fields are REQUIRED. The one evaluation-mode field is
        # OPTIONAL: omitting it keeps the in-run computation, which is what the 0.5
        # candidates written before it already do.
        if set(config) not in (FIELDS | MULTICALL_FIELDS | DEEPSEEK_CAPACITY_FIELDS
                               | TRANSPORT_FIELDS | REPAIR_PROTOCOL_FIELDS,
                               FIELDS | MULTICALL_FIELDS | DEEPSEEK_CAPACITY_FIELDS
                               | TRANSPORT_FIELDS | SIM_EVAL_FIELDS,
                               FIELDS | MULTICALL_FIELDS | DEEPSEEK_CAPACITY_FIELDS
                               | REPAIR_PROTOCOL_FIELDS,
                               FIELDS | MULTICALL_FIELDS | DEEPSEEK_CAPACITY_FIELDS
                               | SIM_EVAL_FIELDS):
            raise ValueError("the 0.5 schema must declare exactly the profile, its DeepSeek "
                             "capacity members and the two repair protocol fields "
                             "(plus the optional evaluation-mode field)")
        declared_mode = config.get(DETERMINISTIC_EVALUATION_FIELD,
                                   DETERMINISTIC_EVALUATION_IN_RUN)
        if declared_mode not in DETERMINISTIC_EVALUATION_MODES:
            raise ValueError(
                f"{DETERMINISTIC_EVALUATION_FIELD} must be one of "
                f"{list(DETERMINISTIC_EVALUATION_MODES)}")
        # The SAME provider contract as 0.4, enforced by the same module.
        _deepseek_transport().validate_deepseek_multiturn_config(
            config, option=exploratory_capacity_option)
        # The protocol bundle is validated HERE, before any other check, so an unknown
        # protocol version is a refused candidate rather than a run that behaves natively.
        validate_run_protocols(config)
    else:
        raise ValueError("wrong multiturn R/E pair schema")
    if config["purpose"] != PURPOSE:
        raise ValueError("wrong multiturn R/E pair schema or purpose")
    if schema in (SCHEMA_VERSION_MULTICALL, SCHEMA_VERSION_CAPACITY,
                  *DEEPSEEK_SCHEMAS):
        try:
            resolve_policy(config, allow_absent=False)
        except MulticallPolicyError as exc:
            raise ValueError(f"invalid {CONFIG_KEY}: {exc}") from None
    # The continuous original-data scope is fixed, not caller-selectable: a
    # caller must not be able to shrink it to one task or move the start.
    if type(config["start_turn"]) is not int or config["start_turn"] != START_TURN:
        raise ValueError("the continuous pair starts at the fixed t4 t3-cutoff")
    # Only the two verified projection bounds are accepted: the fixed continuous
    # t4..t12 production scope and the bounded t4..t5 pilot scope. Nothing else,
    # so a later gold snapshot can never be requested as a start or an end.
    if type(config["end_turn"]) is not int or config["end_turn"] not in PILOT_END_TURN_SET:
        raise ValueError("the pair covers only the fixed original t4-t12 scope "
                         "(or the t4-t5 pilot projection)")
    if config["arms"] != list(ARMS):
        raise ValueError("the R/E pair arms and their fixed order are not editable")
    for key in ("model_origin", "letta_origin"):
        _origin(config[key])
    if config["model_origin"] == config["letta_origin"]:
        raise ValueError("Letta and model origins must differ")
    if config["transport_profile"] not in PROFILES:
        raise ValueError("the run must declare a reviewed transport profile")
    transport = PROFILES[config["transport_profile"]]
    # The INTERNAL alias is the project's one model handle; the transport profile maps
    # it to the WIRE model the provider must receive. Both are checked, and the mapping
    # is declared explicitly in the config so the run record carries it.
    if transport.profile == "deepseek-official-re-transport-v1":
        # The DeepSeek transport names its OWN internal model; it is not the Qwen handle.
        if config["expected_model"] != transport.internal_model:
            raise ValueError("the DeepSeek run must target the transport's own wire model")
        if not isinstance(config["model_handle"], str) \
                or not config["model_handle"].endswith(transport.internal_model):
            raise ValueError("the DeepSeek model handle must name the declared wire model")
    elif (config["model_handle"] != "vllm/" + MODEL
            or config["expected_model"] != transport.internal_model):
        raise ValueError("the R/E pair targets the one explicit cloud model")
    if transport.wire_model != transport.internal_model:
        if config.get("upstream_model") != transport.wire_model:
            raise ValueError(
                "this transport sends another wire model; the config must record the "
                "mapping in `upstream_model`")
    elif config.get("upstream_model") not in (None, transport.wire_model):
        raise ValueError("the declared wire model is not this transport profile's")
    if transport.provider_slug is not None:
        if config.get("provider_slug") != transport.provider_slug:
            raise ValueError(
                "this transport pins a provider route; the config must record it in "
                "`provider_slug`")
    elif config.get("provider_slug") is not None:
        raise ValueError("this transport profile has no provider route")
    if schema in DEEPSEEK_SCHEMAS:
        # Already validated by the provider module above; only the window/source
        # agreement is re-stated here so both schemas read the same way.
        if config["context_window_source"] != config["capacity"]["window_source"]:
            raise ValueError("the context window source must match the capacity declaration")
    elif schema == SCHEMA_VERSION_CAPACITY:
        # The window is declared by the capacity block, which is validated below;
        # the two must agree, and the derived limits are recomputed from the window.
        validate_capacity(config["capacity"], config)
        if config["context_window_source"] != config["capacity"]["window_source"]:
            raise ValueError("the context window source must match the capacity declaration")
    else:
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
                or not math.isfinite(value) or value < 0
                or (key == "timeout_seconds" and value == 0)):
            raise ValueError(f"invalid {key}")
    if max(config["max_output_tokens"], config["auxiliary_output_tokens"]) >= config["context_window"]:
        raise ValueError("context must leave room for input")
    # Fixed archive V3, not a fitted scientific threshold (letta_agent_v3.py).
    native_char_limit = max(5000, int(config["context_window"] * 0.8))
    if config["max_tool_return_chars"] > native_char_limit:
        raise ValueError("tool return guard cannot exceed the fixed Letta V3 truncation limit")
    pacing = config["pacing"]
    if schema in DEEPSEEK_SCHEMAS:
        # The driver re-validates its own config at several entries, so the internal copy
        # this function RETURNS carries the opt-in marker. Every outward record (the plan's
        # `config`, the result, any saved document) is written from the caller's own
        # declaration, which never contains it.
        pass
        # The serial candidate (no fixed sleep) is validated by the provider module; the
        # sealed 65 s combination is NOT reachable through 0.4 and vice versa.
        if pacing is None:
            raise ValueError("the DeepSeek candidate must declare its serial pacing")
        _deepseek_transport().validate_serial_pacing(config)
        validated = deepcopy(config)
        if exploratory_capacity_option is not None:
            validated[EXPLORATORY_MARKER] = exploratory_capacity_option
        return validated
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


def check_scope(sample: dict) -> dict:
    """Verify the prepared sample is the continuous public t4--t12 projection.

    Only the PUBLIC projection is inspected here: the t3 initial facts/profile
    and the nine projected tasks. Private evaluator/environment payloads are
    used by the native wrapper and by the separate private oracle, never as an
    agent input and never as a driver-side preference source.
    """
    if not isinstance(sample, dict):
        raise ValueError("multiturn R/E pair requires a verified sample projection")
    source = sample.get("source") or {}
    # The sample declares its own verified projection bound. Production runs use
    # the fixed 12; a bounded pilot may declare the wiring bound 5. Nothing else
    # is accepted, so the continuous start can never move and the scope can never
    # be widened past the fixed original data.
    sample_end = source.get("end_turn")
    if sample_end not in (5, END_TURN) or source.get("start_turn") != START_TURN:
        raise ValueError("the prepared sample is not a verified t4..t5 or t4..t12 projection")
    numbers = tuple(range(START_TURN, sample_end + 1))
    tasks = sample.get("tasks")
    if not isinstance(tasks, list) or [t.get("number") for t in tasks] != list(numbers):
        raise ValueError("multiturn R/E pair requires the prepared consecutive public projection")
    if sample.get("initial_facts") is None or sample.get("initial_profile") is None:
        raise ValueError("multiturn R/E pair requires the verified t3 initial state")
    for task in tasks:
        if set(task) != {"number", "subtask_id", "domain", "current_time", "instruction", "history"}:
            raise ValueError("each task must be the public task projection only")
        if task["subtask_id"] != f"sub_{USER_ID}_{task['number']}":
            raise ValueError("unexpected task identity in the continuous scope")
        if not isinstance(task["history"], list) or not task["history"]:
            raise ValueError("every task must retain its original historical records")
        if [item["ref"] for item in task["history"]] != \
                [f"t{task['number']}/history/{i}" for i in range(len(task["history"]))]:
            raise ValueError(f"t{task['number']} history order/refs changed")
    facts = sample["initial_facts"]
    old = [k for k, v in facts.items() if isinstance(v, dict) and v.get("content") == "奶茶偏好5分糖"]
    if len(old) != 1:
        raise ValueError("the t3 initial block must carry exactly one 奶茶偏好5分糖 fact")
    if any(v.get("content") == "奶茶偏好7分糖" for v in facts.values() if isinstance(v, dict)):
        raise ValueError("the initial block must not already carry a replacement")
    if sample["initial_profile"].get("user_id") != USER_ID:
        raise ValueError("invalid t3 profile")
    if not isinstance(source, dict) or source.get("user_id") != USER_ID \
            or source.get("original_user_index") != USER_INDEX:
        raise ValueError("the prepared sample did not come from the fixed original user")
    # The dataset's own target product ids, kept PRIVATE and keyed by subtask. They are
    # the only extra material this scope carries, they are read from the same verified
    # projection as the public tasks, and `_run_one_phase` hands them to the
    # deterministic check - never to the Agent, the user simulator or a prompt.
    private = sample.get("private_tasks") or {}
    target_products = {}
    for task in tasks:
        entry = private.get(task.get("subtask_id")) or {}
        ids = [x for x in entry.get("target_product_ids") or [] if isinstance(x, str)]
        if ids:
            target_products[task["subtask_id"]] = list(ids)
    return {"tasks": deepcopy(tasks), "facts": deepcopy(facts),
            "profile": deepcopy(sample["initial_profile"]), "task_numbers": numbers,
            "target_products": target_products,
            "target_products_visibility": "private_scoring_only"}


def pair_payload(config: dict, scope: dict, arm: str, *, name: str) -> tuple[dict, MemoryPolicy]:
    """Build only; the arm's MemoryPolicy carries the arm semantics.

    The two arms get byte-identical initial blocks and system text; only the
    local ``MemoryPolicy.arm`` differs, and that difference is never visible to
    the agent as an extra memory channel.
    """
    if arm not in ARMS:
        raise ValueError("unknown R/E arm")
    memory = MemoryPolicy(arm, scope["facts"], block_char_limit=config["block_char_limit"])
    # Only the explicit capacity candidate carries the no-compaction tag; the sealed
    # 0.1/0.2 payloads stay byte-identical (they keep the empty tag list).
    tags = [NO_COMPACTION_TAG] if config.get("capacity") is not None else None
    payload = creation_payload(name=name, model=config["model_handle"],
                               profile=scope["profile"], memory=memory, tags=tags)
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



def _byte_gate_count_request(capacity):
    """The 0.4 in-process counter: the DECLARED byte budget.

    It returns the budget, not a measurement: the proxy itself measures the request's own
    bytes and compares them with this number, exactly as it does for the 0.3 token count.
    No tokenizer is loaded and no token count is produced.
    """
    budget = capacity.get("max_request_bytes")
    if type(budget) is not int or budget <= 0:
        raise RuntimeError("the byte-gate candidate declares no usable byte budget")

    def count_bytes(normalized, role):
        return budget

    return count_bytes, BYTE_GATE_COUNT_BASIS


def _official_count_request(capacity):
    """The capacity candidate's PRIMARY counting basis, from the shared entry.

    It counts the MODEL-VISIBLE prompt of the request the proxy is about to send -
    system prompt, every retained message and the wire tool schemas, rendered by the
    official chat template and tokenized by the official asset - through the SAME
    `count_request_prompt` function the offline capacity report uses. Counting the
    HTTP JSON body is explicitly not this: JSON syntax is not model input.

    The asset's identity is checked against the target the candidate declares. When
    the target model's own assets are absent the counter refuses; a labelled
    exploration count is a separate, explicit mode that a real RUN does not accept.
    """
    if "official_qwen_tokenizer" not in capacity["count_basis"]:
        raise RuntimeError("the candidate declares no official tokenizer count basis")
    from ae_multiturn_capacity import (QwenBpeTokenizer, count_request_prompt,
                                       load_official_tokenizer_assets,
                                       load_target_tokenizer_assets)

    declared_target = capacity["tokenizer"].get("asset_target")
    target = capacity["tokenizer"].get("target")
    if not target or not declared_target:
        raise RuntimeError("the candidate must declare the tokenizer target and the asset "
                           "identity it counts with")
    if declared_target == target:
        # The run declares the model's OWN assets: they are pinned by file identity and
        # a missing or altered file refuses the count. There is no fall back to the
        # family cache - a formal run must not be counted with another model's file.
        assets = load_target_tokenizer_assets()
        basis = "official_qwen_tokenizer"
    else:
        # A declared asset that is not the run's target is allowed ONLY as a labelled
        # exploration: the formal-RUN refusal is taken at the stage decision, which the
        # run record carries, so the count is always explicit about what produced it.
        assets = load_official_tokenizer_assets()
        basis = f"exploration_qwen_family_asset:{declared_target}"
    tokenizer = QwenBpeTokenizer(assets["tokenizer_json"], assets["tokenizer_config"])
    revision = assets.get("revision") or "?"
    label = f"{basis}:{revision}"
    cache = {}

    def count(normalized, role):
        body = json.loads(normalized.decode("utf-8"))
        messages = body.get("messages")
        tools = body.get("tools") or []
        if not isinstance(messages, list) or not messages:
            raise RuntimeError("the request carries no messages to count")
        # The KEY carries the tool SCHEMAS, not merely how many there are: the same
        # messages with the same number of tools but a longer schema is a different
        # prompt and must be counted again. `default=str` keeps the key total.
        key = (len(messages), len(tools), json.dumps(messages, ensure_ascii=False,
                                                     sort_keys=True),
               json.dumps(tools, ensure_ascii=False, sort_keys=True))
        if key not in cache:
            counted = count_request_prompt(tokenizer, messages, tools)
            cache[key] = counted["prompt_tokens"]
        return cache[key]

    return count, label


def phase_order(numbers=TASK_NUMBERS) -> list[dict]:
    """The fixed phase order: R's whole t4..tN, then E's whole t4..tN."""
    return [{"index": index, "arm": arm, "label": ARM_LABELS[arm], "task_number": number,
             "subtask_id": f"sub_{USER_ID}_{number}"}
            for index, (arm, number) in enumerate(
                [(arm, number) for arm in ARMS for number in numbers])]


def private_boundary(sample: dict) -> dict:
    """Declare the private values that must never reach an executing agent.

    Every projected task's native environment and evaluation criteria are
    private run evidence and are never sent to any agent. Their digests are
    declared so a later audit can prove the executing wire did not carry them;
    target product ids are declared for provenance but NOT scanned, because a
    legitimate agent tool result may contain them.
    """
    private = sample.get("private_tasks") or {}
    numbers = tuple(sorted(int(key.rsplit("_", 1)[-1]) for key in private)) or TASK_NUMBERS
    rubrics, targets = {}, {}
    for number in numbers:
        task_id = f"sub_{USER_ID}_{number}"
        declared = private.get(task_id) or {}
        rubrics[task_id] = hashlib.sha256(dumps(declared.get("evaluation_criteria")).encode()).hexdigest()
        targets[task_id] = deepcopy(declared.get("target_product_ids"))
    return {
        "rubric_sha256": rubrics,
        "target_product_ids": targets,
        "forbidden_source": "dataset private_tasks (environment/evaluation_criteria/target_product_ids)",
        "note": ("target product ids are declared for provenance only and are not scanned: a "
                 "legitimate agent tool result may contain them; private current-preference "
                 "memory is covered by exact input-derivation checks instead"),
    }


def build_plan(config: dict, sample: dict, *, code_files,
               exploratory_capacity_option: str | None = None) -> dict:
    """Offline PLAN: no service, no model, no socket, no receipt fabrication.

    The plan's own `config` is the DECLARATION - the candidate document, without the
    private opt-in marker - so a plan can be compared digest-for-digest with the
    `--config` bytes the operator passed and with the proxy's armed declaration. The
    opt-in is therefore taken as an argument, exactly like the CLI flag it stands for.
    """
    config = validate_config(config, exploratory_capacity_option=exploratory_capacity_option)
    declaration = {key: value for key, value in config.items() if key != EXPLORATORY_MARKER}
    scope = check_scope(sample)
    payloads = {arm: pair_payload(config, scope, arm, name=f"ae-re-multiturn-{arm}-GENERATED")[0]
                for arm in ARMS}
    initial_blocks = {arm: MemoryPolicy(arm, scope["facts"],
                                        block_char_limit=config["block_char_limit"]).initial_block
                      for arm in ARMS}
    if len(set(initial_blocks.values())) != 1:
        raise ValueError("both arms must start from byte-identical blocks")
    # The whole-pair request budget this plan is allowed to spend. The historical constant
    # is the budget for every sealed protocol and for a 0.4 candidate that keeps it; a 0.4
    # candidate may select the other reviewed value in its own field, and then the plan
    # records THAT number with its source - a 1024 run never carries a 256 plan.
    if config.get("schema_version") in DEEPSEEK_SCHEMAS:
        pair_request_budget = {
            "max_requests": config["max_requests"],
            "counting": REQUEST_COUNTING,
            "source": ("the candidate's own max_requests field; reviewed values are "
                       f"{list(_deepseek_transport().ACCEPTED_REQUEST_BUDGETS)}"),
            "budget_unchanged": config["max_requests"] == PAIR_MAX_REQUESTS,
            "phase": "not started; run budget only"}
    else:
        pair_request_budget = {"max_requests": PAIR_MAX_REQUESTS,
                               "counting": REQUEST_COUNTING,
                               "phase": "not started; run budget only"}
    numbers = scope["task_numbers"]
    phases = phase_order(numbers)
    history_sizes = {task["subtask_id"]: len(task["history"]) for task in scope["tasks"]}
    total_records = sum(history_sizes.values())
    plan = {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": "PLAN",
        "network_called": False, "model_called": False, "task_success": None,
        "scientific_result": None, "config": declaration,
        "source": deepcopy(sample.get("source")),
        "scope": {
            "user_id": USER_ID, "start_turn": START_TURN, "end_turn": numbers[-1],
            "task_numbers": list(numbers), "task_count": len(numbers),
            "phase_count": len(phases),
            "arm_order": list(ARMS), "arm_order_source": ARM_ORDER_SOURCE,
            "execution_model": (f"one persistent agent per arm; R's {len(numbers)} phases first, "
                                f"then E's {len(numbers)} phases; never interleaved"),
            "dataset_history_sent": True, "memory_update_tool": True,
            "pair_request_budget": pair_request_budget,
            "history_records_total": total_records,
            "history_records_per_task": history_sizes,
            "code_files": sorted(code_files),
        },
        "phases": phases,
        "inputs": {
            "initial_facts": scope["facts"], "initial_profile": scope["profile"],
            "initial_block": initial_blocks[ARMS[0]],
            "tasks": [{"number": task["number"], "subtask_id": task["subtask_id"],
                       "domain": task["domain"], "current_time": task["current_time"],
                       "instruction": task["instruction"], "history_records": len(task["history"]),
                       "history_material": history_message(task)} for task in scope["tasks"]],
            "private_boundary": private_boundary(sample),
        },
        "arms": {arm: {"label": ARM_LABELS[arm], "memory_arm": arm,
                       "agent_payload": payloads[arm],
                       "initial_block": initial_blocks[arm],
                       "initial_block_sha256": hashlib.sha256(
                           initial_blocks[arm].encode("utf-8")).hexdigest()}
                 for arm in ARMS},
        "boundaries": [
            "one fresh persistent Letta agent per arm, initialized once from the SAME verified t3 cutoff",
            "R executes t4->t12 to completion before E starts t4; the two arms never interleave",
            "each phase appends only that task's original interactions records plus its raw instruction",
            "prior batches are never resent; no future task is ever sent; no gold preference is filled in",
            "the t4 predicate oracle is diagnostic only and is declared NULL for t5..t12",
            "a low or wrong native score keeps its real value and does NOT stop the remaining tasks",
            "a native evaluation/protocol anomaly stops the whole pair and keeps earlier evidence",
            "environment continuity is per-task re-initialization, not real-world state continuity",
        ],
    }
    return plan


def preflight(sample: dict, config: dict) -> dict:
    """Offline checks that need no service, model or socket (caller blocks sockets).

    Verifies the two arms start from byte-identical correct t3 blocks, that each
    of the nine phases has its own original history batch in original order, that
    no future batch can enter the current phase material, and that the fixed
    18-phase order is R(t4..t12) then E(t4..t12).
    """
    config = validate_config(config)
    scope = check_scope(sample)
    blocks = {arm: MemoryPolicy(arm, scope["facts"],
                                block_char_limit=config["block_char_limit"]).initial_block
              for arm in ARMS}
    numbers = scope["task_numbers"]
    per_task = {}
    seen_refs = set()
    for task in scope["tasks"]:
        refs = [item["ref"] for item in task["history"]]
        if set(refs) & seen_refs:
            raise RuntimeError(f"t{task['number']} repeats a previously sent history ref")
        seen_refs.update(refs)
        material = history_message(task)
        blob = json.dumps(material["content"], ensure_ascii=False)
        for other in numbers:
            if other > task["number"] and f"t{other}/" in blob:
                raise RuntimeError(f"future t{other} material leaked into t{task['number']}")
        per_task[task["subtask_id"]] = {
            "number": task["number"], "domain": task["domain"],
            "history_records": len(refs), "history_refs": refs,
            "history_refs_in_order": refs == [f"t{task['number']}/history/{i}"
                                              for i in range(len(refs))],
            "instruction": task["instruction"],
            "history_material_sha256": hashlib.sha256(material["content"].encode("utf-8")).hexdigest(),
        }
    checks = {
        "arms": list(ARMS), "arm_order": list(ARMS), "arm_order_source": ARM_ORDER_SOURCE,
        "phase_count": len(phase_order(numbers)), "phases": phase_order(numbers),
        "identical_initial_block": len(set(blocks.values())) == 1,
        "initial_block_sha256": {arm: hashlib.sha256(blocks[arm].encode()).hexdigest()
                                 for arm in ARMS},
        "t3_old_fact_present": "奶茶偏好5分糖" in blocks[ARMS[0]],
        "t3_replacement_absent": "奶茶偏好7分糖" not in blocks[ARMS[0]],
        "task_count": len(scope["tasks"]), "task_numbers": list(numbers),
        "history_records_total": sum(len(t["history"]) for t in scope["tasks"]),
        "per_task": per_task,
    }
    return {"checks": checks, "private_boundary": private_boundary(sample), "config": config}


class MultiturnTaskBridge(TaskBridge):
    """The production bridge plus delivery/tool-call capture for one arm.

    Behaviour is unchanged from ``TaskBridge``; this subclass only records which
    projected task each provider POST belongs to, and the delivered tool-call
    batches, so the multi-turn audit can attribute every consumed cloud record
    to exactly one (arm, task, phase) without searching by content.

    Capacity is NOT decided here. The request that must fit the declared window is
    the one the PROVIDER receives, which this bridge never sees; the pre-send gate
    lives in the cloud proxy, on the request that is really sent.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: One record per POST, in execution order. `declared_input` and
        #: `task_number` are attached when the bridge knows the source; a
        #: tool-return POST keeps the entry and is attributed to the same task.
        self.post_tasks: list[dict] = []
        #: This arm's own POST ordinal, from 1, in execution order.
        self.post_ordinal = 0
        #: One record per delivered tool execution, in execution order. `pending`
        #: is the full native approval call this execution answers.
        self.tool_calls: list[dict] = []

    def _request(self, method: str, path: str, body: dict | None = None):
        if method == "POST" and path == self.path + "/messages":
            # A driver-side ordinal for this arm's POST stream. It is checked
            # against the capture by ORDER, not by value, so a dropped or extra
            # POST is visible even though the transport owns the real request id.
            self.post_ordinal += 1
            self.post_tasks.append({"task_number": self.last_turn + 1,
                                    "subtask_id": f"sub_{USER_ID}_{self.last_turn + 1}",
                                    "post_ordinal": self.post_ordinal,
                                    "phase": "pending", "path": path, "declared_input": None})
        return super()._request(method, path, body)

    def _execute(self, tool: dict, bindings):
        result = super()._execute(tool, bindings)
        self.tool_calls.append({"name": tool.get("name"), "tool_call_id": tool.get("tool_call_id"),
                                "arguments": tool.get("arguments"),
                                # The exact approval call this execution answers, so a
                                # later audit can require return == call and reject a
                                # changed name/arguments/id.
                                "pending": deepcopy(tool),
                                "result": deepcopy(result),
                                "status": result.get("status"),
                                "task_number": self.last_turn,
                                "subtask_id": self.task_id})
        return result


def _records_pending_instruction_decision(native) -> bool:
    return callable(getattr(native, "pending_instruction_decision", None))


def pending(native):
    """The instruction turn's decision, claimed once from the runtime (or None)."""
    claim = getattr(native, "pending_instruction_decision", None)
    if not callable(claim):
        return None
    return claim()


def orders_now(bridge, environment, turn_index):
    """The phase's visible order table at one turn boundary, as a compact projection."""
    orders = environment_orders(environment)
    return {
        "turn_index": turn_index,
        "tool_calls_so_far": len(bridge.tool_calls),
        "orders": {k: (v or {}).get("status") for k, v in sorted(orders.items())},
    }


def _phase_boundary(bridge, native, writes_count, native_calls_count, kind, task_id,
                    phase_index):
    """Record the exact slice boundary for one phase, before the phase runs.

    A phase's own records are `[prior_boundary_index:]` of each cumulative list.
    Recording the boundary at the moment the phase starts is what lets a later
    audit attribute every POST and every native auxiliary call to exactly one
    (arm, task) without searching by content.
    """
    return {"kind": kind, "task": task_id, "phase_index": phase_index,
            "post_index": len(bridge.post_tasks),
            "tool_call_index": len(bridge.tool_calls),
            "memory_write_index": writes_count,
            "native_call_index": native_calls_count,
            "trace_index": len(bridge.trace)}


def _run_one_phase(config, scope, arm, task, bridge, native, *, emit, phase_index,
                   runtimes_baseline, protocols=None):
    """Run exactly one task of one arm on its persistent agent.

    Returns a per-phase record. Raises on any execution-boundary failure; a low
    or wrong native score is NOT a failure and is recorded as the real score.

    `protocols` is the ONE bundle both arms are handed (None for a run that declares
    none). It is passed to the runtime's own `start`/`user_reply`, so the simulator
    boundary and the separated evaluation belong to the runtime rather than to a
    caller-side convention; a simulator violation raised there stops the phase.
    """
    started_clock = time.monotonic()
    # The dataset's own target product ids for THIS phase, when the caller supplied a
    # scoring scope. They are private scoring material: they go to the deterministic
    # check and to nothing else - never into a prompt, a tool schema or a memory write.
    target_product_ids = list((scope or {}).get("target_products", {}).get(
        task.get("subtask_id")) or ())
    prepared = native.start(deepcopy(task), protocols=protocols)
    if prepared["instruction"] != task["instruction"]:
        raise RuntimeError("native user's first instruction differs from the public task")
    greeting = prepared["greeting"]
    if hasattr(greeting, "model_dump"):
        greeting = greeting.model_dump(mode="json")
    environment = prepared["environment"]
    baseline_order_ids = set(environment_orders(environment))
    bridge.begin(task, greeting)
    bindings = environment_bindings(environment)
    # Per-stage interface clarification (0.4 only). It is derived from THIS stage's own
    # native environment and bound to THIS arm's bridge, so the clock always belongs to the
    # agent+task+request it is sent with. It changes no memory content: the clock travels in
    # the public task envelope and the attributes note only edits the tool schema that this
    # request carries.
    clarification = None
    if config.get("schema_version") in DEEPSEEK_SCHEMAS:
        clarification = stage_clarification_for(
            environment=environment, tools=[b.schema for b in bindings.values()],
            domain=task["domain"], subtask_id=task["subtask_id"])
        bridge.set_stage_clarification(clarification)
        emit({"kind": "stage_clarification", "arm": arm, "task": task["subtask_id"],
              "phase_index": phase_index, "clarification": deepcopy(clarification)})
    snapshot_at_start = _safe_snapshot(native)
    boundary = _phase_boundary(
        bridge, native, len(bridge.memory.writes),
        len((snapshot_at_start or {}).get("native_calls") or []),
        "phase_start", task["subtask_id"], phase_index)
    emit({"kind": "stage_start", "arm": arm, "task": task["subtask_id"],
          "phase_index": phase_index, "public_task": deepcopy(task),
          "domain_policy": prepared["domain_policy"],
          "tools": [v.schema for v in bindings.values()],
          "tool_names": sorted(bindings), "boundary": deepcopy(boundary),
          "snapshot": snapshot_at_start})

    try:
        staged = bridge.run_stage(task, bindings=bindings,
                                  domain_policy=prepared["domain_policy"],
                                  clarification=clarification)
    finally:
        # The binding is per stage: clearing it means a later stage of the OTHER arm can
        # never inherit this one's clock or note.
        bridge.set_stage_clarification(None)
    history_reply = deepcopy(staged["history_reply"])
    text = final_public_text(staged["assistant_messages"])
    termination = None
    exchanges = 0
    simulator_decisions = []
    order_state_by_turn = []
    # The instruction turn's decision is collected HERE, at phase start: the Agent may
    # return STOP on its very first reply, in which case no `user_reply` ever runs and a
    # decision deferred to the first user turn would be lost - while the transcript still
    # contains the instruction user turn.
    instruction_decision = pending(native)
    if isinstance(instruction_decision, dict):
        simulator_decisions.append(deepcopy(instruction_decision))
        order_state_by_turn.append(orders_now(bridge, environment, 0))
    for _ in range(config["max_user_exchanges"]):
        if native.agent_stop(text):
            termination = "agent_stop"
            break
        user_reply = native.user_reply(text)
        exchanges += 1
        # The reply carries the declared protocol's own decision record, and it is
        # emitted exactly as returned: a violation stops the phase from inside the
        # runtime and is never repaired, deleted or retried here.
        emit({"kind": "simulated_user", "arm": arm, "task": task["subtask_id"],
              "phase_index": phase_index, "reply": user_reply})
        decision = user_reply.get("protocol_decision")
        if isinstance(decision, dict):
            simulator_decisions.append(deepcopy(decision))
            order_state_by_turn.append(orders_now(bridge, environment, exchanges))
        if user_reply["stop"]:
            bridge.transcript.append({"role": "user", "content": user_reply["content"]})
            termination = "user_stop"
            break
        text = final_public_text(bridge.reply_to_user(user_reply["content"]))
        if native.agent_stop(text):
            termination = "agent_stop"
            break
    if termination is None:
        # A hard outer limit is a bounded stop of the WHOLE pair, not a legal
        # task outcome: no completed-task score exists for this phase.
        raise RuntimeError(
            f"user exchange limit reached at {task['subtask_id']}; no completed-task score")

    duration = time.monotonic() - started_clock
    captured_orders = environment_orders(environment)
    captured_stores = environment_stores(environment)
    record = {
        "arm": arm, "label": ARM_LABELS[arm], "phase_index": phase_index,
        "number": task["number"], "subtask_id": task["subtask_id"], "domain": task["domain"],
        "current_time": task["current_time"], "instruction": task["instruction"],
        "history_records": len(task["history"]),
        "history_reply": history_reply,
        "termination_reason": termination, "duration_seconds": duration,
        "user_exchanges": exchanges, "model_posts": bridge.stage_posts,
        "transcript": deepcopy(bridge.transcript),
        "final_block": bridge.memory.block_text,
        "final_block_sha256": hashlib.sha256(bridge.memory.block_text.encode("utf-8")).hexdigest(),
        "patch_count": sum(1 for step in bridge.trace
                           if step.get("kind") == "request" and step.get("method") == "PATCH"),
        "patches": [deepcopy(step["body"]) for step in bridge.trace
                    if step.get("kind") == "request" and step.get("method") == "PATCH"],
        "memory_writes_this_task": deepcopy(
            bridge.memory.writes[runtimes_baseline["writes"][arm]:]),
        "memory_writes_cumulative": len(bridge.memory.writes),
        "baseline_order_ids": sorted(baseline_order_ids),
        "task_oracle": (diagnose_orders(
            captured_orders, baseline_order_ids=baseline_order_ids, stores=captured_stores,
            work_address=scope["profile"][WORK_ADDRESS_KEY])
            if task["number"] == 4 else None),
        "task_oracle_note": ("t4 replacement predicate; diagnostic only, never injected"
                             if task["number"] == 4 else
                             "no replacement predicate is defined for this original task"),
        "multicall_batches": deepcopy(bridge.multicall_batches),
        # The declared protocol bundle this phase really ran under, named in the
        # record itself so a report never has to infer it from the run config.
        "protocols": (None if protocols is None else deepcopy(protocols)),
        "protocol_bundle_sha256": (None if protocols is None
                                   else protocol_bundle_sha256(protocols)),
        "simulator_decisions": simulator_decisions,
        # The phase's order table at each agent turn boundary. The user simulator reads
        # the LIVE table when it answers, so a decision is only reproducible from the
        # table that existed at THAT turn - never from the final one. These projections
        # are cross-checked by the audit against the phase's own ordered tool evidence.
        "order_state_by_turn": order_state_by_turn,
      }
    runtimes_baseline["writes"][arm] = len(bridge.memory.writes)
    try:
        judged = native.finish(task, deepcopy(bridge.transcript), termination, duration,
                               arm=arm, target_product_ids=target_product_ids)
    except Exception as exc:
        # A failed native evaluation chain is an execution-boundary anomaly:
        # keep every order/transcript/update capture already made and let the
        # caller STOP the pair. No scientific score is invented.
        record["judge"] = {"status": "JUDGE_FAILED_ORDERS_RETAINED", "reward_info": None,
                           "scientific_success": None,
                           "error": {"type": type(exc).__name__, "message": str(exc)}}
        record["native_snapshot"] = _safe_snapshot(native)
        try:
            native.abort()
            record["native_aborted_after_judge_failure"] = True
        except Exception as abort_error:  # pragma: no cover - defensive
            record["abort_error"] = str(abort_error)
        raise RuntimeError(
            f"native evaluation failed at {task['subtask_id']} of arm {arm}") from exc
    record["judge"] = {"status": judged.get("judging_status", "MODEL_JUDGED_DEBUG_ONLY"),
                       "reward_info": deepcopy(judged.get("reward_info")),
                       "scientific_success": None, "error": None}
    snapshot = _safe_snapshot(native)
    record["native_snapshot"] = snapshot
    # The per-task slice of this arm's native auxiliary capture. The wrapper's
    # own snapshot is cumulative for the whole arm, so the phase boundary has to
    # be recorded here; a later audit must never re-slice by content.
    all_calls = (snapshot or {}).get("native_calls")
    if isinstance(all_calls, list):
        prior = runtimes_baseline["native_calls"][arm]
        record["native_calls_this_task"] = deepcopy(all_calls[prior:])
        runtimes_baseline["native_calls"][arm] = len(all_calls)
    # The same per-phase slice for the wrapper's cumulative simulation records, so
    # a later audit compares the judge against THIS task's own submission rather
    # than a cumulative list that also holds later tasks.
    all_simulations = (snapshot or {}).get("simulations")
    if isinstance(all_simulations, dict):
        this_task = str(task["subtask_id"])
        need_simulation = all_simulations.get(this_task)
        record["judged_simulation_this_task"] = deepcopy(need_simulation)
    # The same explicit boundary for the bridge's own cumulative records.
    record["post_slice"] = [deepcopy(item) for item in bridge.post_tasks[boundary["post_index"]:]]
    record["tool_call_slice"] = [
        deepcopy(item) for item in bridge.tool_calls[boundary["tool_call_index"]:]]
    record["memory_writes_this_task_expected"] = [
        deepcopy(item) for item in bridge.memory.writes[boundary["memory_write_index"]:]]
    record["boundary"] = deepcopy(boundary)
    emit({"kind": "arm_task_complete", "arm": arm, "task": task["subtask_id"],
          "phase_index": phase_index, "termination_reason": termination,
          "transcript": deepcopy(bridge.transcript),
          "memory_writes": deepcopy(record["memory_writes_this_task"]),
          "final_block_sha256": record["final_block_sha256"],
          "task_oracle": deepcopy(record["task_oracle"]),
          "judge": deepcopy(record["judge"]), "snapshot": native.snapshot()})
    return record


def _safe_snapshot(native):
    try:
        return native.snapshot()
    except Exception as exc:  # pragma: no cover - defensive
        return {"snapshot_error": f"{type(exc).__name__}: {exc}"}


def scoring_scope(sample: dict) -> dict:
    """The SCORING view of a verified projection: public tasks plus their standards.

    Each task carries the standard the judge is given (the dataset's own
    `evaluation_criteria` state/overall rubrics, with the DATASET's own rubric keys),
    the dataset's target product ids and the task's own environment. This object is
    driver/judge material only - it is built here, never handed to the Agent or to the
    user simulator, and it declares that in `visibility`.
    """
    from ae_sim_eval_protocol import standard_items
    private = sample.get("private_tasks") or {}
    profile = deepcopy(sample.get("initial_profile") or {})
    tasks = []
    for task in sample.get("tasks") or []:
        number = task.get("number")
        entry = private.get(task.get("subtask_id")) or {}
        criteria = deepcopy(entry.get("evaluation_criteria") or {})
        tasks.append({
            "number": number, "subtask_id": task.get("subtask_id"),
            "domain": task.get("domain"), "instruction": task.get("instruction"),
            "rubrics": [x["rubric"] for x in standard_items({"evaluation_criteria": criteria})],
            # The keyed form: the keys are the dataset's own, so a judged decision is
            # compared with the standard by the id it was given.
            "standard_items": standard_items({"evaluation_criteria": criteria}),
            "target_product_ids": list(entry.get("target_product_ids") or []),
            "environment": deepcopy(entry.get("environment")),
            # The work address the deterministic address check compares against. It is
            # the VERIFIED t3 profile's own field, not a restatement of it.
            "user_scenario": {"user_profile": profile},
            "user_profile": profile,
        })
    return {"visibility": "private_driver_and_judge_only",
            "source": "the verified sample's private projection",
            "user_profile": profile,
            "tasks": tasks}


def separated_evaluation(result: dict, sample: dict,
                         protocols: dict) -> dict:
    """The three separated results for a finished pair, with the simulator decisions.

    Simulator decisions are read from each phase's own recorded replies, so the
    report states what the declared simulator protocol actually decided per phase
    instead of re-deriving it here.
    """
    scoring = scoring_scope(sample)
    decisions = {}
    for record in _recorded_simulator_decisions(result):
        key = f"{record['arm']}:{record['subtask_id']}"
        entry = decisions.setdefault(key, {"protocol": protocols["simulator_protocol"],
                                           "recorded": True, "exchanges": []})
        entry["exchanges"].append(record["decision"])
    for key, entry in decisions.items():
        outcomes = [x.get("outcome") for x in entry["exchanges"]]
        entry["outcome"] = ("SIMULATOR_PROTOCOL_VIOLATION"
                            if "SIMULATOR_PROTOCOL_VIOLATION" in outcomes
                            else (outcomes[-1] if outcomes else "NOT_RECORDED"))
        entry["violations"] = [v for x in entry["exchanges"] for v in x.get("violations") or []]
        entry["unverified_claims"] = [c for x in entry["exchanges"]
                                      for c in x.get("claims") or []]
    report = build_evaluation_report(result, scope=scoring,
                                     simulator_decisions=decisions, bundle=protocols)
    report["scoring_scope_visibility"] = scoring["visibility"]
    return report


def _recorded_simulator_decisions(result: dict):
    """Every simulator decision THIS run recorded, from its own phase records.

    A phase that ended before a user turn ran simply has no decisions; nothing is
    inferred for it and the report marks it NOT_RECORDED.
    """
    for arm, arm_record in (result.get("arms") or {}).items():
        for phase in (arm_record or {}).get("tasks") or []:
            for decision in (phase or {}).get("simulator_decisions") or []:
                yield {"arm": arm, "subtask_id": phase.get("subtask_id"),
                       "decision": decision}


def execute_re_multiturn(config: dict, sample: dict, *, model_transport, letta_transport,
                         runtime_factory, emit=lambda _: None) -> dict:
    """Run the continuous t4->t12 pair once per arm, serially and without retry."""
    config = validate_config(config)
    scope = check_scope(sample)
    numbers = scope["task_numbers"]
    phases = phase_order(numbers)
    policy = resolve_policy(config)
    # The ONE repair protocol bundle this whole pair runs under, or None for a run
    # that declares none. Both arms are handed this same object below.
    protocols = run_protocols_of(config)
    # Where the ADDITIONAL deterministic evaluation runs for THIS declaration. In
    # `offline_diagnostic` mode the driver computes no separated report at all and says
    # so in the run record instead of inventing a result.
    evaluation_mode = deterministic_evaluation_mode(config)
    compute_evaluation = (protocols is not None
                          and evaluation_mode == DETERMINISTIC_EVALUATION_IN_RUN)
    # The pre-send capacity gate is armed in the process that really sends. That may be
    # an in-process transport (tests, or a runner that hosts the gate itself), or the
    # SEPARATE proxy process the production CLI talks to over loopback - in which case
    # the declaration travels as a file and the DRIVER only records it, with the digest
    # the audit matches against the proxy's own journal. The sealed 0.1/0.2 protocols
    # arm nothing and stay byte-identical.
    # The private opt-in marker is not part of the candidate: it exists only so that every
    # internal re-validation uses the same opt-in the CLI was given. Everything this
    # function RECORDS or HANDS OUT is the candidate document itself - the same bytes
    # `--config` points at - so the plan, the run record and the armed declaration are
    # one document whose digests are comparable. For the sealed protocols the marker
    # never exists and this is the identity.
    declaration = {key: value for key, value in config.items()
                   if key != EXPLORATORY_MARKER}
    capacity_arming = None
    if config.get("capacity") is not None:
        if config.get("schema_version") in DEEPSEEK_SCHEMAS:
            count_request, count_source = _byte_gate_count_request(config["capacity"])
            armed_basis = BYTE_GATE_COUNT_BASIS
        else:
            count_request, count_source = _official_count_request(config["capacity"])
            armed_basis = None
        capacity_arming = {
            "declaration": declaration,
            "declaration_sha256": canonical_sha256(declaration),
            "context_window": config["capacity"]["context_window"],
            "agent_reserve_tokens": config["max_output_tokens"],
            "auxiliary_reserve_tokens": config["auxiliary_output_tokens"],
            "count_request": count_request, "count_source": count_source,
            "count_basis": armed_basis,
            "emit": emit}
    result = {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": "INVALID",
        "execution_complete": False, "validity_passed": False, "input_audit_passed": None,
        "re_pair_passed": None, "task_success": None, "scientific_result": None,
        "config": declaration, "source": deepcopy(sample.get("source")),
        "scope": {"user_id": USER_ID, "start_turn": START_TURN, "end_turn": END_TURN,
                  "task_numbers": list(numbers), "phase_count": len(phases),
                  "arm_order": list(ARMS), "arm_order_source": ARM_ORDER_SOURCE,
                  "dataset_history_sent": True, "memory_update_tool": True,
                  "multicall_profile": (None if policy is None else policy.as_dict()),
                  "target_products": deepcopy(scope.get("target_products") or {}),
                  "target_products_visibility": "private_scoring_only_not_an_agent_input"},
        "phases": phases, "arms": {}, "stopped_after": None,
        "agent_cleanup_performed": False, "invalid_reasons": [],
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        # The repair protocols this pair ran under, recorded on the run itself. The
        # evaluation report is ADDED next to the run's own fields, never instead of
        # them, so nothing an older reader relies on changes shape.
        "protocols": (None if protocols is None else deepcopy(protocols)),
        "protocol_bundle_sha256": (None if protocols is None
                                   else protocol_bundle_sha256(protocols)),
        "deterministic_evaluation": evaluation_mode,
        "evaluation": None,
        "additional_evaluation": None,
    }
    bridges, runtimes, memories = {}, {}, {}
    active, active_arm, active_task = None, None, None
    baseline = {"writes": {arm: 0 for arm in ARMS},
                "native_calls": {arm: 0 for arm in ARMS}}
    if capacity_arming is not None:
        # Two legitimate shapes, and nothing else:
        #  * an in-process transport that IS the proxy (or holds one) arms it HERE,
        #    from the same declaration the proxy CLI would read;
        #  * the production CLI talks to a SEPARATE proxy process over loopback, which
        #    arms itself from `--capacity-config`; the driver then only records the
        #    declaration, and the audit requires the proxy's journal to show that same
        #    policy digest plus one check per request.
        holder = (model_transport if callable(getattr(model_transport,
                                                      "arm_capacity_from_declaration", None))
                  else getattr(model_transport, "proxy", None))
        arm = getattr(holder, "arm_capacity_from_declaration", None)
        if callable(arm):
            arm(capacity_arming["declaration"],
                count_request=capacity_arming["count_request"],
                count_source=capacity_arming["count_source"],
                count_basis=capacity_arming["count_basis"], emit=capacity_arming["emit"])
        elif not isinstance(model_transport, JSONHTTPTransport):
            raise RuntimeError(
                "the declared capacity policy cannot be enforced: this transport does "
                "not expose the pre-send gate and is not the loopback HTTP transport a "
                "separately armed proxy serves")
    try:
        listed = model_transport.request("GET", "/v1/models")
        wire_model = profile_of(config).wire_model
        matches = [m for m in listed.get("data", [])
                   if isinstance(m, dict) and m.get("id") == wire_model]
        if len(matches) != 1:
            raise RuntimeError("provider catalog does not expose exactly one selected cloud model")
        health = letta_transport.request("GET", "/v1/health/")
        if health.get("status") != "ok" or health.get("version") != "0.16.8":
            raise RuntimeError("unexpected Letta health/version")
        result["provider_model"], result["letta_health"] = deepcopy(matches[0]), deepcopy(health)

        # Two agents, created up front, both from the same verified t3 cutoff.
        for arm in ARMS:
            payload, memory = pair_payload(
                config, scope, arm, name="ae-re-multiturn-cloud-" + arm + "-" + secrets.token_hex(6))
            created = letta_transport.request("POST", "/v1/agents/", payload)
            aid = created.get("id")
            if not isinstance(aid, str) or not aid:
                raise RuntimeError("agent creation returned no ID")
            path = "/v1/agents/" + quote(aid, safe="")
            bridge = MultiturnTaskBridge(
                CheckedTaskTransport(letta_transport, path, config), aid, memory,
                max_rounds=config["max_rounds"], max_steps=config["max_steps"],
                config=config, emit=emit, include_memory_tool=True, multicall=policy)
            bridges[arm], memories[arm] = bridge, memory
            bridge.verify_session()
            result["arms"][arm] = {
                "label": ARM_LABELS[arm], "memory_arm": arm, "agent_id": aid,
                "block_id": bridge.block_id, "initial_block": memory.initial_block,
                "initial_block_sha256": hashlib.sha256(memory.initial_block.encode()).hexdigest(),
                "tasks": [], "termination_reason": None, "duration_seconds": None,
                "final_block": None, "final_block_sha256": None,
                "model_posts": 0, "patch_count": 0,
            }
            emit({"kind": "agent_created", "arm": arm, "label": ARM_LABELS[arm],
                  "agent_id": aid, "block_id": bridge.block_id, "payload": deepcopy(payload)})

        assert_separate_arms(bridges[ARMS[0]], bridges[ARMS[1]])

        for arm in ARMS:
            runtimes[arm] = runtime_factory(arm)
        # The fixed order: R's whole t4->t12, then E's whole t4->t12.
        for phase in phases:
            arm, number = phase["arm"], phase["task_number"]
            task = scope["tasks"][number - START_TURN]
            bridge, native = bridges[arm], runtimes[arm]
            active, active_arm, active_task = native, arm, task["subtask_id"]
            started_clock = time.monotonic()
            record = _run_one_phase(config, scope, arm, task, bridge, native, emit=emit,
                                    phase_index=phase["index"], runtimes_baseline=baseline,
                                    protocols=protocols)
            active = None
            record["elapsed_seconds"] = time.monotonic() - started_clock
            result["arms"][arm]["tasks"].append(record)
            result["arms"][arm]["termination_reason"] = record["termination_reason"]
            note = {"kind": "phase_complete", "arm": arm, "phase_index": phase["index"],
                    "task": task["subtask_id"], "judge_status": record["judge"]["status"]}
            emit(note)

        for arm in ARMS:
            bridge, memory = bridges[arm], memories[arm]
            tasks = result["arms"][arm]["tasks"]
            result["arms"][arm].update({
                "duration_seconds": sum(t["duration_seconds"] for t in tasks),
                "model_posts": sum(t["model_posts"] for t in tasks),
                "patch_count": sum(t["patch_count"] for t in tasks),
                "final_block": memory.block_text,
                "final_block_sha256": hashlib.sha256(memory.block_text.encode("utf-8")).hexdigest(),
                "memory_writes": deepcopy(memory.writes),
                "completed_task_count": len(tasks),
            })
        result["execution_complete"] = True
        result["status"] = "RE_MULTITURN_COMPLETED_AUDIT_PENDING"
        if compute_evaluation:
            # The separated evaluation is ADDED here, from the material the run
            # itself captured: the standard the judge was given comes from the
            # verified private projection (which the Agent never receives), and the
            # business facts come from each phase's own captured database. It is
            # produced even when the native score is low, and it never overwrites
            # the native reward.
            result["evaluation"] = separated_evaluation(result, sample, protocols)
        elif protocols is not None:
            # Exploratory mode: the additional deterministic evaluation is an OFFLINE
            # DIAGNOSTIC. Nothing is computed, and the record states that plainly - it
            # is not reported as passed, and it is not silently absent either.
            result["evaluation"] = None
            result["additional_evaluation"] = {
                "deterministic_evaluation": evaluation_mode,
                "ran": False, "status": "NOT_RUN_PENDING_MANUAL_REVIEW",
                "note": ("the additional deterministic evaluation and the separated "
                         "report are OFFLINE DIAGNOSTICS for this exploratory run; the "
                         "run record keeps the native judge output, the per-phase "
                         "database, transcript, tool calls, memory writes and the "
                         "termination reason for a later manual review"),
                "per_phase": "native_snapshot / judge / transcript / tool_call_slice",
                "may_be_read_as_a_pass": False}
    except (Exception, KeyboardInterrupt) as exc:
        import traceback as _traceback
        result["invalid_reasons"].append({"type": type(exc).__name__, "message": str(exc),
                                          "traceback": _traceback.format_exc()})
        result["stopped_after"] = ({"arm": active_arm, "subtask_id": active_task}
                                   if active_arm is not None else None)
        if isinstance(exc, KeyboardInterrupt):
            result["interrupted"] = True
            result["server_side_work_cancelled"] = False
        if active is not None:
            result["partial_native_snapshot"] = _safe_snapshot(active)
            try:
                active.abort()
            except Exception as abort_error:  # pragma: no cover - defensive
                result["abort_error"] = str(abort_error)
        emit({"kind": "pair_stopped", "stopped_after": deepcopy(result["stopped_after"]),
              "reason": deepcopy(result["invalid_reasons"][-1])})
    finally:
        for arm, bridge in bridges.items():
            record = result["arms"].setdefault(arm, {"label": ARM_LABELS[arm], "memory_arm": arm})
            record["bridge_trace"] = list(bridge.trace)
            record["post_tasks"] = list(bridge.post_tasks)
            record["tool_calls"] = list(bridge.tool_calls)
            record.setdefault("tasks", [])
            record.setdefault("memory_writes", deepcopy(bridge.memory.writes))
            record.setdefault("partial_transcript", deepcopy(bridge.transcript))
            record.setdefault("agent_id", bridge.agent_id)
            record.setdefault("block_id", bridge.block_id)
            record["trace_message_ids"] = list(bridge.message_ids)
            record["visible_refs"] = sorted(bridge.visible_refs)
        # The pre-send gate's own records, one per provider request it checked. They
        # are the evidence that a mid-run stop compared THIS role's own request with
        # ITS OWN output reserve, instead of a number the bridge inferred afterwards.
        holder = model_transport if hasattr(model_transport, "capacity_checks") \
            else getattr(model_transport, "proxy", None)
        if capacity_arming is not None:
            # What the driver can attest from ITS side: the declaration it handed out.
            # The per-request checks live in the proxy process's own journal, and the
            # audit matches the two - a driver-side list would only be the driver's
            # word about another process.
            result["capacity_arming"] = {
                "declaration_sha256": capacity_arming["declaration_sha256"],
                "count_source": capacity_arming["count_source"],
                "count_basis": capacity_arming["count_basis"],
                "context_window": capacity_arming["context_window"],
                "agent_reserve_tokens": capacity_arming["agent_reserve_tokens"],
                "auxiliary_reserve_tokens": capacity_arming["auxiliary_reserve_tokens"],
                "armed_in_process": getattr(holder, "capacity", None) is not None,
                "in_process_declaration_sha256": getattr(holder, "declaration_sha256", None)}
            if getattr(holder, "capacity", None) is not None:
                # An in-process gate (tests) also exposes its records directly.
                result["capacity_checks"] = list(holder.capacity_checks)
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


MULTITURN_CODE_FILES = (
    "ae_inputs.py", "ae_adapter.py", "ae_http.py", "ae_probe.py", "ae_task_run.py",
    "ae_vita.py", "ae_capability.py", "ae_cloud_task.py", "ae_cloud_proxy.py",
    "ae_cloud_re_multiturn.py", "ae_cloud_re_multiturn_input_audit.py",
    "ae_cloud_re_pair.py", "ae_cloud_re_input_audit.py",
    "ae_multicall.py", "ae_sim_eval_protocol.py",
    "scripts/ae_01_cloud_re_multiturn.py", "scripts/ae_01_cloud_re_multiturn_input_audit.py",
)


def multiturn_code_files(root=None):
    """The multiturn RE production code file set, optionally hashed from a root."""
    from pathlib import Path as _Path
    base = _Path(root) if root else _Path(__file__).resolve().parent
    return {name: hashlib.sha256((base / name).read_bytes()).hexdigest()
            for name in MULTITURN_CODE_FILES if (base / name).is_file()}
