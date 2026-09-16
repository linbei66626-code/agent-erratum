#!/usr/bin/env python3
"""DeepSeek integration for the multi-turn t4->t12 R/E pair: the added layer only.

REUSE (nothing here re-implements it): the real Letta agents, the native tools, the native
user simulator and scorer, the t4..t12 history input, the per-task state flow, the R/E arm
difference, the driver (`ae_cloud_re_multiturn.py`), its CLI and its input audit all stay
exactly where they are. This module adds ONLY what a new provider needs:

* `DEEPSEEK_TRANSPORT` - the explicit transport profile (origin, wire model, mode field);
* the driver-side declaration checks for that profile and for the SERIAL pacing candidate;
* the non-guaranteeing capacity protocol for a provider with no verified local counter;
* the two-arm interface clarification derived from `t4_case_variants.py`.

WHY THIS IS A SEPARATE MODULE: `ae_cloud_proxy.py`'s profile table and
`ae_cloud_re_multiturn.py`'s `PACING_CANDIDATE`/`CAPACITY_COUNT_BASES` are consumed by the
sealed 0.3 protocol and its audit. Adding DeepSeek there would put a new provider on the path
of already-sealed runs, so the new provider is declared here instead and the driver is asked
to consult this module. The old constants are not edited.

CAPACITY HONESTY, in one paragraph: DeepSeek publishes a 1M context, and that is a
PUBLISHED DECLARATION, not a measurement. There is no verified DeepSeek tokenizer in this
workspace, so this layer refuses to invent one: it never calls the Qwen counter, never
divides bytes by four and never presents a response's `usage` as a pre-send count. What it
does offer is an operational REQUEST-BYTE budget (the old multi-turn 2097152 bytes) that can
stop an obviously oversized request before it is sent, under an explicitly declared
exploratory protocol with `capacity_is_a_guarantee = false`. The default RUN keeps refusing
an unresolved capacity premise.

THE SERVICE-SIDE BLOCKER (recorded, not worked around): the Letta capacity gate refuses any
request without a trustworthy prompt count, and the only accepted bases are the Qwen
tokenizer ones. With DeepSeek that means every request would be refused. Finishing this
integration therefore needs a model-specific branch in the service patch plus a rebuilt
stack manifest/receipt - see `SERVICE_CHAIN_BLOCKER` below. This round does not perform it.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

SCHEMA_VERSION = "ae-deepseek-re-transport-0.1"
DEEPSEEK_PROFILE = "deepseek-official-re-transport-v1"
DEEPSEEK_ORIGIN = "https://api.deepseek.com"
DEEPSEEK_CHAT_PATH = "/chat/completions"
DEEPSEEK_MODELS_PATH = "/models"
DEEPSEEK_INTERNAL_MODEL = "deepseek-flash"
DEEPSEEK_WIRE_MODEL = "deepseek-flash"
#: The mode DeepSeek documents for non-thinking. It REPLACES the local vLLM field; a
#: request built for this transport never carries `chat_template_kwargs`.
DEEPSEEK_MODE_FIELD = "thinking"
DEEPSEEK_MODE_VALUE = {"type": "disabled"}
#: What the pinned Letta service puts on EVERY OpenAI-shaped provider call, next to the
#: model's own fields: the actor id and the parallel-tool-call switch (Letta's
#: `openai_client.build_request_data` sets both). They are part of the request the proxy
#: receives, so they are declared rather than dropped.
SERVICE_PASS_THROUGH_FIELDS = ("user", "parallel_tool_calls")

#: The serial pacing candidate: no fixed sleep. Requests still run one at a time, each one
#: waiting for its own response and for its tools to finish, with no retry.
SERIAL_PACING = {
    "driver_io_timeout_seconds": 900,
    "proxy_upstream_io_timeout_seconds": 180,
    "min_interval_seconds": 0,
}
SERIAL_PACING_LABEL = "serial_no_fixed_interval"

#: The old multi-turn whole-pair budget is kept for the request-byte guard: speeding the run
#: up is not a reason to enlarge a budget.
BYTE_BUDGET = 2097152
#: The historical whole-pair request budget, re-stated here so the serial candidate can
#: check that a faster run did not enlarge it on its own.
PAIR_REQUEST_BUDGET = 256
#: The DISCRETE request budgets the 0.4 protocol accepts. The candidate's own
#: `max_requests` selects one of them explicitly: 256 is the historical baseline, and 1024
#: is the user-authorised enlargement that leaves headroom over the observed 256-request
#: stop. The set is closed on purpose - an arbitrary positive integer is not a budget this
#: protocol has reviewed - and 1024 is never a default: a candidate that does not state it
#: keeps 256.
ACCEPTED_REQUEST_BUDGETS = (PAIR_REQUEST_BUDGET, 1024)
OUTPUT_RESERVE_AGENT = 2048
OUTPUT_RESERVE_AUXILIARY = 4096

#: The explicit exploratory protocol. With no verified counter, a token-capacity claim
#: cannot be made, so a run may only start when the caller declares this option.
NON_GUARANTEE_COUNT_BASIS = "no_token_count_byte_gate_only"
NON_GUARANTEE_PROTOCOL = "exploratory_no_token_capacity_guarantee"
NON_GUARANTEE_OPTION = "--exploratory-capacity-option"
NON_GUARANTEE_FLAG = "exploratory_no_token_guarantee"

#: The parameters whose support by the declared endpoint is NOT confirmed offline.
UNVERIFIED_PARAMETERS = ("seed",)

SERVICE_CHAIN_BLOCKER = {
    "where": ("deployment-assets/letta-no-compaction/files/letta/helpers/"
              "ae_no_compaction.py :: capacity_gate_decision"),
    "observed_rule": ("a request with no trustworthy prompt count is REFUSED with "
                      "`no_trustworthy_count_basis`, and `CAPACITY_COUNT_BASES` in "
                      "ae_cloud_re_multiturn.py accepts only the two Qwen bases"),
    "consequence": ("with DeepSeek there is no verified local tokenizer, so the deployed "
                    "service would refuse EVERY request of both arms"),
    "minimal_remaining_work": [
        "add a model-specific branch to the service gate that, under the declared "
        "non-guarantee protocol, activates the request-byte gate instead of the token gate "
        "(the byte gate must still refuse over-budget requests, and the decision record must "
        "carry capacity_is_a_guarantee=false)",
        "add the matching driver-side count basis so the declaration and the service agree",
        "rebuild the Letta overlay and refresh stack-manifest.json / manifest.json",
        "list the deployment increment and re-verify the stack receipt chain on site",
    ],
    "not_done_this_round": True,
    "mock_pass_claim": False,
}


class DeclarationRejected(ValueError):
    """A driver-side declaration that cannot be trusted as written."""


#: The DeepSeek capacity block has its own reviewed member set. It is deliberately NOT the
#: sealed 0.3 set: that one requires a Qwen token basis and a published/measured window
#: semantics this provider cannot satisfy, and loosening it would change sealed runs.
DEEPSEEK_CAPACITY_MEMBERS = {
    "context_window", "window_source", "window_is_measured", "capacity_is_a_guarantee",
    "service_limit", "verification", "output_reserve_tokens", "auxiliary_reserve_tokens",
    "no_compaction", "count_basis", "max_request_bytes", "tokenizer", "derived_caps",
}
DEEPSEEK_SERVICE_LIMIT_MEMBERS = {
    "published_native_context_tokens", "endpoint_measured_tokens", "endpoint_measurement",
    "published_sources",
}
DEEPSEEK_TOKENIZER_MEMBERS = {"target", "asset_target", "asset_evidence", "exploration_only"}
DEEPSEEK_DERIVED_CAP_MEMBERS = {"native_tool_return_truncation_chars", "tool_return_guard_chars"}
#: The schema that carries this protocol. The sealed 0.1/0.2/0.3 schemas are untouched, and
#: the default RUN still refuses an unresolved capacity premise.
DEEPSEEK_SCHEMA_VERSION = "ae-cloud-re-multiturn-0.4"
#: The same provider contract with the repair protocols added. It is a SEPARATE schema so
#: 0.4's declaration and every 0.4 run record stay byte-identical; the provider contract
#: this module enforces - serial pacing, the byte-gate capacity declaration, the fixed
#: request budget, the exploratory non-guarantee - is IDENTICAL under both, so the new
#: schema is validated here rather than by a second copy of the same rules.
DEEPSEEK_SIM_EVAL_SCHEMA_VERSION = "ae-cloud-re-multiturn-0.5"
DEEPSEEK_SCHEMAS = (DEEPSEEK_SCHEMA_VERSION, DEEPSEEK_SIM_EVAL_SCHEMA_VERSION)
EXPLORATORY_PROTOCOL_KEY = "exploratory_capacity_protocol"
NO_COMPACTION_POLICY = "ae-no-compaction-1"


def validate_deepseek_capacity(capacity: dict, config: dict) -> dict:
    """The DeepSeek capacity contract, validated exactly and independently of 0.3.

    Every member is explicit and nothing is defaulted. The point of this branch is that a
    provider WITHOUT a verified tokenizer can still be bounded honestly: by a request-byte
    budget, under a declared exploratory protocol, with `capacity_is_a_guarantee` false and
    no token count claimed anywhere.
    """
    if not isinstance(capacity, dict) or set(capacity) != DEEPSEEK_CAPACITY_MEMBERS:
        raise DeclarationRejected(
            "the DeepSeek capacity block must declare exactly its reviewed members: "
            f"missing={sorted(DEEPSEEK_CAPACITY_MEMBERS - set(capacity or {}))} "
            f"unexpected={sorted(set(capacity or {}) - DEEPSEEK_CAPACITY_MEMBERS)}")
    window = capacity["context_window"]
    if type(window) is not int or window <= 0:
        raise DeclarationRejected("capacity.context_window must be a positive integer")
    if config.get("context_window") != window:
        raise DeclarationRejected("config.context_window must equal capacity.context_window")
    if capacity["window_source"] not in ("published_native", "provider_published"):
        raise DeclarationRejected(
            "a DeepSeek window may only be the provider's PUBLISHED declaration")
    if capacity["window_is_measured"] is not False:
        raise DeclarationRejected(
            "no DeepSeek window has been measured; window_is_measured must be exactly false")
    if capacity["capacity_is_a_guarantee"] is not False:
        raise DeclarationRejected("capacity_is_a_guarantee must be exactly false")
    service = capacity["service_limit"]
    if not isinstance(service, dict) or set(service) != DEEPSEEK_SERVICE_LIMIT_MEMBERS:
        raise DeclarationRejected("the DeepSeek service_limit must declare its members")
    if service["published_native_context_tokens"] != window:
        raise DeclarationRejected(
            "the published native context must be the same declared window")
    if service["endpoint_measured_tokens"] is not None:
        raise DeclarationRejected(
            "this provider's endpoint capacity has NOT been measured; the field must be null")
    if not isinstance(service["endpoint_measurement"], str) or not service["endpoint_measurement"]:
        raise DeclarationRejected("endpoint_measurement must say what is missing")
    if not isinstance(service["published_sources"], list) or not service["published_sources"]:
        raise DeclarationRejected("a published window must cite its sources")
    if capacity["verification"] != "unverified":
        raise DeclarationRejected(
            "the endpoint capacity is unverified for this provider; nothing may claim "
            "otherwise and no verified field is touched")
    if capacity["no_compaction"] != NO_COMPACTION_POLICY:
        raise DeclarationRejected("the reviewed no-compaction policy is required")
    basis = capacity["count_basis"]
    if basis != [NON_GUARANTEE_COUNT_BASIS]:
        raise DeclarationRejected(
            f"count_basis must be exactly [{NON_GUARANTEE_COUNT_BASIS!r}]; a Qwen token "
            "basis must never be reused for this provider")
    tokenizer = capacity["tokenizer"]
    if not isinstance(tokenizer, dict) or set(tokenizer) != DEEPSEEK_TOKENIZER_MEMBERS:
        raise DeclarationRejected("the DeepSeek tokenizer block must declare its members")
    if tokenizer["target"] is not None or tokenizer["asset_target"] is not None:
        raise DeclarationRejected(
            "no DeepSeek tokenizer exists in this workspace; both target and asset_target "
            "must be null rather than naming another model's assets")
    if tokenizer["exploration_only"] is not True:
        raise DeclarationRejected("the missing counter must be declared exploration_only")
    if not isinstance(tokenizer["asset_evidence"], str) or not tokenizer["asset_evidence"]:
        raise DeclarationRejected("the tokenizer block must say why no asset is declared")
    # The byte budget: this provider's ONLY operational pre-send bound.
    declared_budget = capacity["max_request_bytes"]
    if declared_budget != BYTE_BUDGET:
        raise DeclarationRejected(
            f"capacity.max_request_bytes must be the kept multi-turn budget {BYTE_BUDGET}")
    if config.get("max_request_bytes") != declared_budget:
        raise DeclarationRejected("the run's own byte limit must equal the declared budget")
    if capacity["output_reserve_tokens"] != OUTPUT_RESERVE_AGENT:
        raise DeclarationRejected("the agent output reserve must stay 2048")
    if capacity["auxiliary_reserve_tokens"] != OUTPUT_RESERVE_AUXILIARY:
        raise DeclarationRejected("the auxiliary output reserve must stay 4096")
    if capacity["output_reserve_tokens"] >= window:
        raise DeclarationRejected("the window must leave room for input")
    caps = capacity["derived_caps"]
    if not isinstance(caps, dict) or set(caps) != DEEPSEEK_DERIVED_CAP_MEMBERS:
        raise DeclarationRejected("the derived caps must declare their members")
    expected_native = max(5000, int(window * 0.8))
    if caps["native_tool_return_truncation_chars"] != expected_native:
        raise DeclarationRejected("the derived native truncation must be 0.8 of the window")
    if caps["tool_return_guard_chars"] != config.get("max_tool_return_chars"):
        raise DeclarationRejected("the tool return guard must stay the frozen value")
    if caps["tool_return_guard_chars"] > expected_native:
        raise DeclarationRejected("the tool return guard cannot exceed the derived limit")
    return copy.deepcopy(capacity)


#: The 0.4 protocol's OWN run limits. They are not inherited from the sealed 0.3
#: constants on purpose: this candidate serves a 1M-window provider with larger blocks
#: and more rounds, so each member is pinned here, where the protocol is defined, and
#: the audit compares the run record against THESE - never against another protocol's.
DEEPSEEK_RUN_LIMITS = {
    "block_char_limit": 50000,
    "max_rounds": 64,
    "max_steps": 3,
    "max_stage_posts": 64,
    "max_user_exchanges": 12,
    "temperature": 0,
    "seed": 300,
    "max_tool_return_chars": 26214,
    "max_request_bytes": 2097152,
    "max_response_bytes": 16777216,
    "max_requests": 256,
}


def validate_deepseek_run_limits(config: dict) -> dict:
    """Every non-capacity run limit the 0.4 protocol pins, checked exactly.

    The whole-pair REQUEST budget is the one pinned member with two reviewed values (256
    and 1024). The candidate selects one of them in its own `max_requests` field, and the
    record says which one it selected, where that number came from, and whether it kept
    the historical baseline - a 1024 run never claims its budget is unchanged.
    """
    declared = config.get("max_requests")
    if type(declared) is not int or declared not in ACCEPTED_REQUEST_BUDGETS:
        raise DeclarationRejected(
            f"max_requests must be one of the reviewed whole-pair budgets "
            f"{list(ACCEPTED_REQUEST_BUDGETS)}, got {declared!r}; an unreviewed number is "
            "not a budget this protocol may spend")
    limits = dict(DEEPSEEK_RUN_LIMITS, max_requests=declared)
    reasons = [f"{key} is {config.get(key)!r}, expected {value!r}"
               for key, value in limits.items()
               if config.get(key) != value]
    if reasons:
        raise DeclarationRejected("; ".join(reasons))
    return {"limits": limits,
            "source": "declared by the 0.4 protocol, not inherited from 0.3",
            "request_budget": {
                "declared": declared,
                "accepted": list(ACCEPTED_REQUEST_BUDGETS),
                "source": "the candidate's own max_requests field",
                "historical_baseline": PAIR_REQUEST_BUDGET,
                "budget_unchanged": declared == PAIR_REQUEST_BUDGET,
                "counting": ("the proxy's own per-process counter over EVERY request it "
                             "serves: the /models GET and every /chat/completions POST "
                             "both count, and nothing is reset per task or per arm")}}


def validate_deepseek_multiturn_config(config: dict, *, option: str | None = None) -> dict:
    """The whole DeepSeek multi-turn declaration, checked as one contract.

    Order matters: the exploratory option is required BEFORE any of the provider's
    invariants are accepted, so the default RUN still refuses an unresolved capacity
    premise and nothing here can be reached by accident.
    """
    if not isinstance(config, dict):
        raise DeclarationRejected("the config must be an object")
    if config.get("schema_version") not in DEEPSEEK_SCHEMAS:
        raise DeclarationRejected(
            f"the DeepSeek protocol is declared by schema_version="
            f"{DEEPSEEK_SCHEMA_VERSION!r} (or its protocol-carrying successor "
            f"{DEEPSEEK_SIM_EVAL_SCHEMA_VERSION!r}), got "
            f"{config.get('schema_version')!r}")
    if config.get(EXPLORATORY_PROTOCOL_KEY) != NON_GUARANTEE_PROTOCOL:
        raise DeclarationRejected(
            f"the config must declare {EXPLORATORY_PROTOCOL_KEY}="
            f"{NON_GUARANTEE_PROTOCOL!r}; an unresolved capacity premise is never implicit")
    if option != NON_GUARANTEE_OPTION:
        raise DeclarationRejected(
            "an unresolved capacity premise is refused by default; starting this run "
            f"requires the explicit {NON_GUARANTEE_OPTION}")
    if config.get("arms") != ["rewrite", "erratum"]:
        raise DeclarationRejected("the R/E arms and their fixed order are not editable")
    if config.get("model_origin") == config.get("letta_origin"):
        raise DeclarationRejected("the Letta and model origins must differ")
    declared = validate_deepseek_declaration(config)
    pacing = validate_serial_pacing(config)
    capacity = validate_deepseek_capacity(config.get("capacity") or {}, config)
    limits = validate_deepseek_run_limits(config)
    parameters = validate_unverified_parameters(config)
    return {"protocol": NON_GUARANTEE_PROTOCOL,
            "schema_version": config.get("schema_version"),
            "schema_versions_under_this_protocol": list(DEEPSEEK_SCHEMAS),
            "exploratory_option": option,
            "identity": declared, "pacing": pacing, "capacity": capacity,
            "run_limits": limits,
            "parameters": parameters,
            "service_pass_through": declared_service_pass_through(),
            "default_run_would_refuse_without_the_option": True,
            "sealed_0_3_untouched": True,
            "qwen_capacity_path_used": False,
            "service_chain_blocker": copy.deepcopy(SERVICE_CHAIN_BLOCKER)}


# --------------------------------------------------------------------------- the profile


def deepseek_profile():
    """The transport profile this provider needs, as data.

    It is deliberately NOT inserted into `ae_cloud_proxy.PROFILES`: that table belongs to
    the sealed protocols, and a new provider must not alter what an already-sealed run
    resolves. Callers that must resolve it register it explicitly.
    """
    return {
        "profile": DEEPSEEK_PROFILE,
        "origin": DEEPSEEK_ORIGIN,
        "chat_path": DEEPSEEK_CHAT_PATH,
        "models_path": DEEPSEEK_MODELS_PATH,
        "internal_model": DEEPSEEK_INTERNAL_MODEL,
        "wire_model": DEEPSEEK_WIRE_MODEL,
        "mode_field": DEEPSEEK_MODE_FIELD,
        "mode_value": copy.deepcopy(DEEPSEEK_MODE_VALUE),
        "provider_slug": None,
        "response_provider_field": None,
        # The pinned Letta protocol sends this pass-through shape on EVERY provider
        # call; the DeepSeek profile declares it so those fields are recorded and kept
        # instead of being dropped to make a request succeed. Whether the endpoint
        # tolerates them is an on-site item (see `declared_service_pass_through`).
        "pass_through_fields": list(SERVICE_PASS_THROUGH_FIELDS),
        "local_vllm_field_reused": False,
        "weights_version_claimed": False,
    }


def wire_request_fields(profile=None) -> dict:
    """Exactly what this transport puts on the wire, for the audit to compare.

    The mode field is REQUIRED: the proxy refuses a request that does not state it, so
    the endpoint's own default mode can never silently run instead of the declared
    non-thinking one.
    """
    profile = profile or deepseek_profile()
    return {"model": profile["wire_model"],
            profile["mode_field"]: copy.deepcopy(profile["mode_value"])}


def declared_request_shape(profile=None) -> dict:
    """The whole provider request as declared: the transport's own fields, whose VALUES
    are required and checked, plus the service-shaped members it passes through."""
    profile = profile or deepseek_profile()
    return {"required_fields": wire_request_fields(profile),
            "pass_through_fields": list(profile["pass_through_fields"]),
            "added_by": "the pinned Letta service, never by the proxy"}


def declared_service_pass_through() -> dict:
    """The pinned service's pass-through fields: declared, unverified, never dropped.

    The deployed Letta sets `user` to the actor id and `parallel_tool_calls` from the
    LLM config on every OpenAI-shaped call. Their semantics at the DeepSeek endpoint are
    NOT confirmed offline, so this is an explicit on-site item: the run stops and
    reports if the endpoint rejects one, and no value is ever silently removed.
    """
    return {"pass_through_fields": list(SERVICE_PASS_THROUGH_FIELDS),
            "mode_field": DEEPSEEK_MODE_FIELD,
            "silently_removed": False,
            "claimed_supported": False,
            "status": "to_be_confirmed_on_site",
            "note": ("if the declared endpoint refuses `user` or `parallel_tool_calls`, "
                     "the request must fail and be reported; the proxy never strips them "
                     "and never re-sends the call without them")}


def validate_deepseek_declaration(config: dict, profile=None) -> dict:
    """Check a multi-turn config that claims to target this provider.

    Raises `DeclarationRejected` on any mismatch, so a Qwen-labelled run can never be
    silently reused for DeepSeek (and the audit does not have to trust a label).
    """
    profile = profile or deepseek_profile()
    reasons = []
    if config.get("transport_profile") != profile["profile"]:
        reasons.append(f"transport_profile is {config.get('transport_profile')!r}, "
                       f"expected {profile['profile']!r}")
    # NOTE: in this driver `model_origin` is the LOCAL PROXY the driver talks to, not the
    # provider. The provider origin belongs to the proxy's own CloudConfig and is checked
    # against the proxy profile instead (see `validate_proxy_config_for_deepseek`).
    if config.get("expected_model") != profile["internal_model"]:
        reasons.append(f"expected_model is {config.get('expected_model')!r}, "
                       f"expected {profile['internal_model']!r}")
    if config.get("upstream_model") != profile["wire_model"]:
        reasons.append(f"upstream_model is {config.get('upstream_model')!r}, "
                       f"expected {profile['wire_model']!r}")
    if config.get("provider_slug") is not None:
        reasons.append("this transport pins no provider route; provider_slug must be absent")
    handle = config.get("model_handle")
    if not isinstance(handle, str) or not handle.endswith(profile["internal_model"]):
        reasons.append(f"model_handle {handle!r} does not name the declared wire model")
    # Only the IDENTITY fields are checked for a Qwen label: unrelated members (a window
    # source, sample metadata) may legitimately name another model without making this a
    # Qwen run.
    identity_blob = json.dumps({key: config.get(key) for key in
                                ("transport_profile", "model_handle", "expected_model",
                                 "upstream_model", "provider_slug")},
                               ensure_ascii=False).lower()
    if "qwen" in identity_blob:
        reasons.append("the declared identity still names Qwen; a Qwen-labelled run must "
                       "not be reused as a DeepSeek run")
    if reasons:
        raise DeclarationRejected("; ".join(reasons))
    return {"provider": "deepseek",
            "profile": profile["profile"],
            "wire_model": profile["wire_model"],
            "origin": profile["origin"],
            "mode": {profile["mode_field"]: copy.deepcopy(profile["mode_value"])},
            "wire_request_fields": wire_request_fields(profile),
            "declared_correctly": True,
            "weights_version_claimed": False}


# ---------------------------------------------------------------------------- the pacing


def validate_serial_pacing(config: dict, *, pacing=None) -> dict:
    """Accept ONLY the explicit serial candidate: no fixed sleep, still serial, no retry.

    A zero interval is not an invitation to widen anything else: the request budget, the
    timeouts and the stop-on-first-failure rules are re-checked here so a faster run cannot
    quietly spend more.
    """
    pacing = pacing or SERIAL_PACING
    declared = config.get("pacing")
    if not isinstance(declared, dict):
        raise DeclarationRejected("the serial candidate must be declared in `pacing`")
    if declared.get("min_interval_seconds") != 0:
        raise DeclarationRejected(
            f"the serial candidate requires min_interval_seconds=0, got "
            f"{declared.get('min_interval_seconds')!r}")
    for key in ("driver_io_timeout_seconds", "proxy_upstream_io_timeout_seconds"):
        if declared.get(key) != pacing[key]:
            raise DeclarationRejected(f"{key} must stay {pacing[key]}, got {declared.get(key)!r}")
    if config.get("timeout_seconds") != pacing["driver_io_timeout_seconds"]:
        raise DeclarationRejected(
            "the serial candidate requires its own explicit driver I/O timeout")
    # A faster run is not a reason to spend more: the interval cannot enlarge the budget
    # by itself. The budget is whatever the candidate EXPLICITLY declares, and it must be
    # one of the reviewed values - so 0-interval pacing keeps 256 unless the candidate
    # itself says 1024.
    if ("max_requests" in config
            and (type(config["max_requests"]) is not int
                 or config["max_requests"] not in ACCEPTED_REQUEST_BUDGETS)):
        raise DeclarationRejected(
            f"the whole-pair request budget must be one of the reviewed values "
            f"{list(ACCEPTED_REQUEST_BUDGETS)}, got {config['max_requests']!r}; serial "
            "pacing does not enlarge it")
    budget = config.get("max_requests", PAIR_REQUEST_BUDGET)
    return {"pacing": SERIAL_PACING_LABEL, "declared": copy.deepcopy(declared),
            "fixed_sleep_removed": True, "sleep_seconds": 0,
            "still_serial": True, "concurrency": 1, "automatic_retries": 0,
            "request_budget": budget,
            "budget_unchanged": budget == PAIR_REQUEST_BUDGET,
            "note": ("requests run one at a time and each waits for its response and tools; "
                     "the serial pacing never enlarges the budget itself, and a provider "
                     "429 still stops the run and is recorded as such")}


# -------------------------------------------------------------------------- the capacity


def validate_capacity_declaration(config: dict, *, option: str | None = None) -> dict:
    """The non-guaranteeing capacity contract, accepted only when EXPLICITLY declared.

    Without `option` the declaration is refused, which is what keeps the default RUN
    refusing an unresolved capacity premise. With it, the run is still bounded by the
    request-byte budget and still says, in its own record, that no token capacity is
    guaranteed.
    """
    capacity = config.get("capacity")
    if not isinstance(capacity, dict):
        raise DeclarationRejected("capacity must be declared explicitly")
    basis = capacity.get("count_basis")
    bases = basis if isinstance(basis, list) else [basis]
    if NON_GUARANTEE_COUNT_BASIS not in bases:
        raise DeclarationRejected(
            f"this candidate requires count_basis={NON_GUARANTEE_COUNT_BASIS!r}, "
            f"got {basis!r}")
    forbidden = [b for b in bases if b in ("official_qwen_tokenizer", "pinned_tokenizer")]
    if forbidden:
        raise DeclarationRejected(
            f"a DeepSeek run must not claim a Qwen count basis: {forbidden}")
    if capacity.get("capacity_is_a_guarantee") is not False:
        raise DeclarationRejected("capacity_is_a_guarantee must be declared exactly false")
    if capacity.get("context_window_source") not in (None, "published_native",
                                                     "provider_published"):
        raise DeclarationRejected(
            "the window may only be a PROVIDER-PUBLISHED declaration here")
    if capacity.get("window_is_measured") is True:
        raise DeclarationRejected(
            "no token capacity has been measured for this provider; the declaration must "
            "not claim a measurement")
    budget = capacity.get("max_request_bytes")
    if type(budget) is not int or budget <= 0:
        raise DeclarationRejected("max_request_bytes must be a positive integer")
    for key, expected in (("output_reserve_tokens", OUTPUT_RESERVE_AGENT),
                          ("auxiliary_reserve_tokens", OUTPUT_RESERVE_AUXILIARY)):
        if capacity.get(key) != expected:
            raise DeclarationRejected(f"{key} must stay {expected}, got {capacity.get(key)!r}")
    if config.get("max_request_bytes") not in (None, budget):
        raise DeclarationRejected("the run's own byte limit must equal the declared budget")
    if option != NON_GUARANTEE_OPTION:
        raise DeclarationRejected(
            "an unresolved capacity premise is refused by default; starting this run "
            f"requires the explicit {NON_GUARANTEE_OPTION} declaration")
    return {"protocol": NON_GUARANTEE_PROTOCOL,
            "explicit_option": option,
            "count_basis": NON_GUARANTEE_COUNT_BASIS,
            "capacity_is_a_guarantee": False,
            "token_count_available": False,
            "qwen_counter_used": False,
            "bytes_per_token_estimate_used": False,
            "response_usage_used_as_a_pre_send_count": False,
            "request_byte_budget": budget,
            "request_byte_budget_is": "an operational guard, not a capacity guarantee",
            "output_reserves": {"agent": OUTPUT_RESERVE_AGENT,
                                "auxiliary": OUTPUT_RESERVE_AUXILIARY},
            "declared_context_window_is_published_not_measured": True,
            "default_run_would_refuse_without_the_option": True,
            "verified_capacity_fields_touched": False}


def byte_gate_decision(request_bytes: int, *, budget: int = BYTE_BUDGET) -> dict:
    """The driver-side operation guard: over budget is refused BEFORE any upstream call."""
    if type(request_bytes) is not int or request_bytes < 0:
        return {"allowed": False, "reason": "request_bytes_unknown"}
    if type(budget) is not int or budget <= 0:
        return {"allowed": False, "reason": "byte_budget_undeclared"}
    allowed = request_bytes <= budget
    return {"allowed": allowed, "request_bytes": request_bytes, "budget": budget,
            "reason": "within_declared_byte_budget" if allowed
            else "over_declared_byte_budget",
            "is_a_capacity_guarantee": False,
            "checked_before_send": True}


# ------------------------------------------------------------------ interface clarification


def stage_clarification(*, environment, system_message: str, tools: list,
                        domain: str | None = None):
    """The two-arm clarification for ONE stage, or an honest not_applicable.

    The clock comes from the stage's OWN native environment, and the attributes note is
    added only when the delivery domain really exposes `create_delivery_order.attributes`.
    A non-delivery stage is recorded as `not_applicable` and is NOT forced to carry a
    delivery schema, so a normal cross-domain task is never blocked by this layer.
    """
    import importlib.util

    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("ae_t4_case_variants",
                                                  root / "t4_case_variants.py")
    variants = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(variants)

    record = {"domain": domain, "clock": None, "attributes_note": "not_applicable",
              "schema_touched": False, "required_changed": False,
              "execution_default_changed": False, "arguments_filled_in": False,
              "applies_to_both_arms_identically": True,
              "user_persona_changed": False, "task_changed": False,
              "oracle_changed": False}
    try:
        fact = variants.time_fact(environment)
    except variants.VariantRejected as exc:
        raise DeclarationRejected(
            f"this stage exposes no readable environment clock ({exc}); the stage is not "
            "built with a guessed time") from None
    record["clock"] = fact
    system = system_message
    if not isinstance(system, str) or not system:
        raise DeclarationRejected("the stage needs its own system message to extend")
    system = system + (
        "\n# 当前时间\n"
        f"- 当前时间：{fact['environment_now']}（环境时钟，{fact['format']}）\n"
        "- 该时间与环境工具校验 dispatch_time 使用的是同一个时钟。\n")

    has_attributes = any(
        isinstance(tool, dict)
        and ((tool.get("function") or {}).get("name") == "create_delivery_order")
        and isinstance(((tool.get("function") or {}).get("parameters") or {})
                       .get("properties"), dict)
        and "attributes" in ((tool["function"]["parameters"]["properties"]))
        for tool in tools or [])
    if not has_attributes:
        record["tools_count"] = len(tools or [])
        return system, copy.deepcopy(tools or []), record
    cloned, change = variants.clarified_tools(tools, environment)
    record.update(attributes_note="applied", schema_touched=True,
                  schema_change=change,
                  required_changed=change.get("parameter_now_required", False),
                  oracle_value_included=change.get("oracle_value_included", False))
    return system, cloned, record


def validate_unverified_parameters(config: dict) -> dict:
    """Parameters whose endpoint support is NOT confirmed offline stay declared, not dropped.

    `seed` is carried by the sealed configs because SiliconFlow accepted it. Whether the
    declared endpoint honours it is an on-site item: it is neither silently removed (that
    would change the sampling declaration) nor claimed as supported.
    """
    present = [name for name in UNVERIFIED_PARAMETERS if config.get(name) is not None]
    return {"unverified_parameters": list(UNVERIFIED_PARAMETERS),
            "present_in_config": present,
            "silently_removed": False,
            "claimed_supported": False,
            "status": "to_be_confirmed_on_site",
            "note": ("if the endpoint rejects one of these, the run must stop and report it; "
                     "the value is never dropped to make a request succeed")}


def audit_identity(expected: dict, observed: dict) -> dict:
    """The audit-side check: the REAL wire identity, not a label."""
    mismatches = []
    for key in ("model", "origin", "profile"):
        if observed.get(key) != expected.get(key):
            mismatches.append({"field": key, "expected": expected.get(key),
                               "observed": observed.get(key)})
    if "qwen" in json.dumps(observed, ensure_ascii=False).lower():
        mismatches.append({"field": "observed_identity",
                           "expected": "the declared DeepSeek model",
                           "observed": "a Qwen label is still present"})
    return {"matches": not mismatches, "mismatches": mismatches,
            "compared_fields": ["model", "origin", "profile"],
            "identity_from_a_label_only": False}


def validate_proxy_config_for_deepseek(proxy_document: dict) -> dict:
    """Check the PROXY-side document that really reaches the provider.

    This is where the provider origin, the wire model and the declared byte budget live, so
    it is checked here rather than on the driver's loopback origin.
    """
    profile = deepseek_profile()
    reasons = []
    if proxy_document.get("profile") != profile["profile"]:
        reasons.append(f"proxy profile is {proxy_document.get('profile')!r}, expected "
                       f"{profile['profile']!r}")
    if proxy_document.get("upstream_origin") != profile["origin"]:
        reasons.append(f"proxy upstream_origin is {proxy_document.get('upstream_origin')!r}, "
                       f"expected {profile['origin']!r}")
    if proxy_document.get("model") != profile["internal_model"]:
        reasons.append(f"proxy model is {proxy_document.get('model')!r}, expected "
                       f"{profile['internal_model']!r}")
    if proxy_document.get("upstream_model") != profile["wire_model"]:
        reasons.append(f"proxy upstream_model is {proxy_document.get('upstream_model')!r}, "
                       f"expected {profile['wire_model']!r}")
    if proxy_document.get("declared_request_byte_budget") != BYTE_BUDGET:
        reasons.append("the proxy must declare the reviewed request-byte budget")
    if reasons:
        raise DeclarationRejected("; ".join(reasons))
    return {"profile": profile["profile"], "upstream_origin": profile["origin"],
            "wire_model": profile["wire_model"],
            "declared_request_byte_budget": BYTE_BUDGET,
            "mode": {profile["mode_field"]: copy.deepcopy(profile["mode_value"])},
            "declared_correctly": True}


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
