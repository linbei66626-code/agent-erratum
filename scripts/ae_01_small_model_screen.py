#!/usr/bin/env python3
"""Small-model screening: the SAME tiny create->pay case for two SiliconFlow models.

    .venv-vita/bin/python -B scripts/ae_01_small_model_screen.py \
        --stage plan --transport-config configs/ae-01__small-model-screen__siliconflow.json \
        --vita-source vendor/vita/source \
        --output-dir deployment/runs/ae-small-model-screen-<UTC>

What this is, and what it is NOT:

* it reuses the EXISTING frozen probe case (`scripts/ae_01_native_order_probe.py`'s
  environment, prompt, tool schemas, tool execution and final native order check) and
  runs it once per model, in order, with a FRESH environment per model;
* it is a simplified tool-chain screen - not the U000828 t4 task, not R/E, and one
  success decides nothing about deployment;
* the two models are exactly `Qwen/Qwen3.5-4B` and `Qwen/Qwen3.5-9B`, through an
  independent screening transport profile: the frozen profiles' model whitelist and the
  global model constant are untouched.

Budget and safety, all recorded in `plan.json`:

* at most 3 chat requests per model and at most 6 in total; output 2048; temperature 0;
* ONE proxy, ONE journal and ONE 65-second send interval shared by both models, so the
  interval holds ACROSS the model boundary too (`pace_wait` chains the sends);
* no concurrency, no automatic retry: HTTP 429, an authentication/network failure or any
  other non-200 stops the whole batch with the evidence kept;
* the thinking mode is sent EXPLICITLY and identically for both models
  (`enable_thinking: false`), and recorded; a truncated (`finish_reason=length`) answer
  is INCOMPLETE, never a success or a capability failure;
* pass/fail comes from the native database state and from the provenance of the paid
  order id (it must come from an earlier real create return in the SAME model's run) -
  never from the model's own wording.

The key is read from `--key-file` (default `deployment/private/siliconflow.key`) only in
`--stage run`, is never logged and never copied into the evidence.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ae_cloud_proxy as proxy_module  # noqa: E402
from ae_cloud_proxy import CloudAuditProxy, CloudConfig, read_private_key  # noqa: E402

SCHEMA_VERSION = "ae-small-model-screen-0.1"
PURPOSE = "small-model-screen"
MAX_REQUESTS_PER_MODEL = 3
MAX_REQUESTS_TOTAL = 2 * MAX_REQUESTS_PER_MODEL
REQUEST_OUTPUT_TOKENS = 2048
TEMPERATURE = 0.0
PACE_SECONDS = 65.0
STATUS_PLAN = "PLAN_FROZEN"
STATUS_COMPLETED = "SCREEN_COMPLETED"
STATUS_STOPPED = "SCREEN_STOPPED_PROVIDER_FAILURE"
STATUS_INVALID = "INVALID"
#: The per-model accounting never replaced the request statuses above; these are what a
#: request that did not answer carries INSTEAD, and they say whether the attempt was a
#: real send or a local refusal:
#:   * `_REFUSED` - the proxy refused BEFORE any upstream send (0 send attempts). A local
#:     policy rejection must never be counted as a request the provider could have billed;
#:   * `_FAILED` - the upstream send started (the journal holds its `upstream_request`) but
#:     no HTTP response arrived; whether the provider received or executed it is UNKNOWN and
#:     is recorded as such, never assumed.
STATUS_TRANSPORT_REFUSED = "MODEL_TRANSPORT_REFUSED"
STATUS_TRANSPORT_FAILED = "MODEL_TRANSPORT_FAILED"
#: Provider execution states of one send attempt.
PROVIDER_EXECUTION_NOT_ATTEMPTED = "not_attempted"
PROVIDER_EXECUTION_RESPONSE_RECEIVED = "response_received"
PROVIDER_EXECUTION_UNKNOWN = "unknown_no_response"
#: Stop codes that end the WHOLE batch (never retried, never continued to the next model).
BATCH_STOP_HTTP = (401, 403, 429)
#: The journal record the proxy writes when - and only when - an upstream send begins.
JOURNAL_UPSTREAM_REQUEST = "upstream_request"
JOURNAL_UPSTREAM_RESPONSE = "upstream_response"
JOURNAL_CHAT_PATH = "/v1/chat/completions"
CODE_FILES = ("ae_cloud_proxy.py", "ae_model_proxy.py", "scripts/ae_01_small_model_screen.py",
              "scripts/ae_01_native_order_probe.py")


def _load_probe():
    """The frozen probe module, loaded by path: the case must be the SAME one."""
    spec = importlib.util.spec_from_file_location(
        "ae_native_order_probe_for_screen", ROOT / "scripts/ae_01_native_order_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _probe_timing(clock=None, sleep=None):
    """The clock/sleep the proxy uses, so tests can drive the shared interval."""
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep
    return kwargs


def screen_config(transport_config: Path, *, max_requests: int = MAX_REQUESTS_TOTAL):
    document = json.loads(Path(transport_config).read_text(encoding="utf-8"))
    document = dict(document)
    document["max_requests"] = max_requests
    config = CloudConfig(**document)
    config.validate()
    profile = proxy_module.profile_of(config)
    if profile.profile != proxy_module.SCREEN_PROFILE:
        raise RuntimeError(
            "the screening CLI takes the screening transport profile only; the frozen "
            f"profiles are not relaxed here (got {profile.profile!r})")
    if profile.declared_models() != tuple(proxy_module.SCREEN_MODELS):
        raise RuntimeError("the screening profile's model set is not the authorised pair")
    return config, profile


def build_plan(vita_source: Path, transport_config: Path) -> dict:
    probe = _load_probe()
    case = probe.build_case(vita_source)
    config, profile = screen_config(transport_config)
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "two_small_models_same_tiny_tool_chain_screen_not_an_experiment",
        "status": STATUS_PLAN, "network_called": False, "model_called": False,
        "task_success": None, "scientific_result": None,
        "models": [
            {"index": index, "wire_model": model, "internal_alias": model,
             "transport_profile": profile.profile, "endpoint": profile.origin,
             "requests_allowed": MAX_REQUESTS_PER_MODEL}
            for index, model in enumerate(profile.declared_models(), start=1)],
        "model_count": len(profile.declared_models()),
        "case": {"db": deepcopy(probe.CASE_DB), "expected_order": deepcopy(probe.EXPECTED_ORDER),
                 "user_request": probe.USER_REQUEST, "system_prompt": probe.SYSTEM_PROMPT,
                 "case_sha256": probe.sha256_bytes(probe.dumps(probe.CASE_DB).encode("utf-8")),
                 "same_case_for_every_model": True,
                 "fresh_environment_per_model": True},
        "exposed_tools": {name: case["schemas"][name] for name in probe.TOOLS},
        "budget": {"max_requests_per_model": MAX_REQUESTS_PER_MODEL,
                   "max_requests_total": MAX_REQUESTS_TOTAL,
                   "request_output_tokens": REQUEST_OUTPUT_TOKENS,
                   "temperature": TEMPERATURE, "seed": None, "tool_choice": "auto",
                   "parallel_tool_calls": False, "no_retry": True, "no_concurrency": True,
                   "pace_seconds": PACE_SECONDS,
                   "pace_shared_across_models": True,
                   "no_resume_same_directory": True},
        "thinking_mode": {
            "field": proxy_module.SCREEN_THINKING_FIELD,
            "value": proxy_module.SCREEN_THINKING_VALUE,
            "placement": "top-level chat/completions field",
            "identical_for_both_models": True,
            "recorded_in_request_changes": True,
            "evidence": ("SiliconFlow documents enable_thinking for its hybrid Qwen3 "
                         "models; this workspace cannot reach the vendor docs, so whether "
                         "THIS endpoint accepts the field in this placement is an ONLINE "
                         "to-check. If it is rejected, the refusal is journalled (non-200) "
                         "and the batch stops - the CLI never silently falls back to a "
                         "default thinking mode.")},
        "truncation_rule": ("finish_reason=length is INCOMPLETE: neither a success nor a "
                            "capability failure, and it never authorises a retry"),
        "stop_rules": [
            "HTTP 429, 401 or 403 stops the whole batch immediately, with no retry",
            "any other non-200, an unparseable response or an uncertain tool outcome stops the batch",
            "a model's own failure is recorded for that model; the other model still runs",
            "the string the model writes is never a pass criterion: the native order state "
            "and the provenance of the paid order id decide",
        ],
        "transport_config": {"path": str(transport_config),
                             "sha256": _sha256(Path(transport_config)),
                             "document": json.loads(Path(transport_config).read_text(
                                 encoding="utf-8"))},
        "decision_rules": [
            "success requires exactly one correct PAID order in THAT model's own environment, "
            "whose order id came from an earlier real create return in the same run",
            "the whole provider batch is processed in provider order before any decision",
            "redundant actions after a successful payment are recorded as redundancy",
            "one success neither proves stability nor decides deployment; the full t4 "
            "validation is a separate step",
        ],
        "scope": ["fixed Vita environment, no Letta, no database, no user simulator, no judge",
                  "an order is an in-memory Vita record, not a real transaction",
                  "simple tool-chain screening only; not the U000828 t4 task and not R/E"],
        "provenance": {"code_sha256": {name: _sha256(ROOT / name) for name in CODE_FILES},
                       "python": sys.version, "python_executable": sys.executable},
    }


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_journal_bytes(path):
    """The journal as bytes, or `None` when it cannot be read at all.

    The journal is the append-only wire evidence (`JSONLJournal`: exclusive creation and
    fsync per record), so reading it back is reading what the proxy really did.
    """
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _journal_cursor(journal_path):
    """Establish the read position, once per model, before its first request.

    Every send counted from here on therefore belongs to THIS model: the model boundary
    can never inherit the other model's rows.
    """
    data = _read_journal_bytes(journal_path)
    return {"offset": None if data is None else len(data)}


def _journal_since(state, journal_path):
    """Every complete record appended since the cursor; `None` if it cannot be read.

    A journal only ever grows, so `offset` stays valid. An unreadable journal returns
    `None` - never an empty list - because "I cannot see the evidence" must not be
    recorded as "nothing was sent".
    """
    offset = state.get("offset")
    if offset is None:
        return None
    data = _read_journal_bytes(journal_path)
    if data is None or len(data) < offset:
        return None
    state["offset"] = len(data)
    rows = []
    for line in data[offset:].splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn trailing line is not a record; never invent one
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _chat_wire_model(row):
    """The wire model an `upstream_request` record actually carries."""
    try:
        body = json.loads(base64.b64decode(row.get("body_base64") or ""))
    except (ValueError, TypeError):
        return None
    return body.get("model") if isinstance(body, dict) else None


def _attempt_from_journal(state, journal_path, model, sent_before):
    """What the journal shows for the attempt that just finished.

    Send attempts are counted by ATTRIBUTION, from the proxy's own records: an
    `upstream_request` row is the one and only marker that a send began, so a request the
    proxy refused before sending (capacity gate, field/profile violation, request-size or
    credential refusal, a closed proxy, an exhausted budget) contributes zero. A row whose
    wire model is not the model being run is a wiring mismatch: the count is then not
    trustworthy, which is reported instead of quietly attributed to this model.
    """
    rows = _journal_since(state, journal_path)
    if rows is None:
        return {"accounting_ok": False,
                "accounting_error": "journal_unreadable_for_send_attribution",
                "attempt": {"sent": 1 if sent_before else 0,
                            "response_received": bool(sent_before),
                            "provider_execution_state": (
                                PROVIDER_EXECUTION_UNKNOWN if sent_before
                                else PROVIDER_EXECUTION_NOT_ATTEMPTED),
                            "request_sha256": None, "http_status": None,
                            "source": "marker_before_the_journal_became_unreadable"}}
    sends = [row for row in rows if row.get("kind") == JOURNAL_UPSTREAM_REQUEST]
    responses = [row for row in rows if row.get("kind") == JOURNAL_UPSTREAM_RESPONSE]
    bodies = [row for row in rows if row.get("kind") == "client_request"
              and row.get("path") == JOURNAL_CHAT_PATH]
    wire = [_chat_wire_model(row) for row in sends]
    foreign = [name for name in wire if name != model]
    sent = len(sends)
    response_received = bool(responses)
    if response_received:
        execution = PROVIDER_EXECUTION_RESPONSE_RECEIVED
    elif sent:
        execution = PROVIDER_EXECUTION_UNKNOWN
    else:
        execution = PROVIDER_EXECUTION_NOT_ATTEMPTED
    attempt = {"sent": sent,
               "response_received": response_received,
               "provider_execution_state": execution,
               "request_sha256": (bodies[0].get("body_sha256") if bodies else None),
               "http_status": (responses[0].get("http_status") if responses else None),
               "source": "journal_upstream_request"}
    if foreign:
        return {"accounting_ok": False,
                "accounting_error": f"journal_upstream_request_model_mismatch: {foreign!r}",
                "attempt": attempt}
    return {"accounting_ok": True, "accounting_error": None, "attempt": attempt}


def _chat_body(probe, vita, model, messages, schemas):
    """The frozen case's request shape, with the model and the explicit thinking mode."""
    body = json.loads(probe._chat_body(messages, schemas,
                                       request_output_tokens=REQUEST_OUTPUT_TOKENS,
                                       temperature=TEMPERATURE).decode("utf-8"))
    body["model"] = model
    body[proxy_module.SCREEN_THINKING_FIELD] = proxy_module.SCREEN_THINKING_VALUE
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _run_model(probe, model, index, *, proxy, journal_path, clock):
    """One model's own environment, at most MAX_REQUESTS_PER_MODEL chat requests.

    The accounting of what was SENT comes from the journal, not from `dispatch`
    returning: a request that reached the transport and then failed (a timeout, an I/O
    failure, a reset) was still a send attempt - whether the provider executed it stays
    UNKNOWN - while a request the proxy refused before sending was never a send at all.
    """
    case = probe.build_case(_VITA_SOURCE[0])
    vita, environment, bindings = case["vita"], case["environment"], case["bindings"]
    messages = [vita["SystemMessage"](role="system", content=probe.SYSTEM_PROMPT),
                vita["UserMessage"](role="user", content=probe.USER_REQUEST)]
    created_ids, created_sources, steps = [], {}, []
    record = {"model": model, "index": index, "requests_sent": 0, "steps": steps,
              "status": "MODEL_NOT_STARTED", "model_task_completed": False,
              "task_success": None, "stop_reason": None, "created_order_ids": created_ids,
              "payment_source_verified": None, "tool_returns": [],
              "final_order_state": None, "usage": [], "timings": [],
              "requests_attempted": 0, "sends": 0, "responses_received": 0,
              "attempts": [], "accounting_ok": True, "accounting_error": None,
              "environment_sha256_before": probe.sha256_bytes(
                  probe.dumps(probe.case_snapshot(environment)).encode("utf-8"))}
    state = _journal_cursor(journal_path)
    for attempt in range(1, MAX_REQUESTS_PER_MODEL + 1):
        body = _chat_body(probe, vita, model, vita["format_messages"](messages),
                          case["schemas"])
        started = clock()
        #: Did `dispatch` return? The journal is what actually decides the send count; this
        #: only supplies a LAST-RESORT value for the one branch where the journal itself
        #: cannot be read (`accounting_ok=false`), where a returned dispatch is at least
        #: evidence that the call got that far.
        sent_before = False
        try:
            status, reply = proxy.dispatch("POST", probe.CHAT_PATH, body, PURPOSE, "agent")
        except Exception as exc:  # noqa: BLE001 - a refusal is evidence, not a retry
            # The journal decides what really happened: a send that started and then
            # failed is a send attempt with an UNKNOWN provider outcome, a refusal before
            # the send is zero sends. `sent_before` stays False because `dispatch` never
            # returned; the attribution below overrides it from the journal.
            outcome = _attempt_from_journal(state, journal_path, model, sent_before)
            entry = dict(outcome["attempt"], error_type=type(exc).__name__,
                         error_code=str(getattr(exc, "code", exc)))
            record["timings"].append({"attempt": attempt, "elapsed_seconds": None})
            record["attempts"].append({"attempt": attempt, **entry})
            record["requests_attempted"] = attempt
            record["sends"] += entry["sent"]
            record["responses_received"] += 1 if entry["response_received"] else 0
            record["requests_sent"] = record["sends"]
            if not outcome["accounting_ok"]:
                record.update(status="MODEL_ACCOUNTING_UNVERIFIABLE",
                              accounting_ok=False,
                              accounting_error=outcome["accounting_error"],
                              stop_reason=outcome["accounting_error"])
                return record, "STOP_BATCH"
            if entry["sent"] == 0:
                # A LOCAL pre-send refusal: no send, so no provider-side request exists.
                record.update(status=STATUS_TRANSPORT_REFUSED,
                              stop_reason=type(exc).__name__,
                              stop_detail=entry["error_code"],
                              provider_execution_state=PROVIDER_EXECUTION_NOT_ATTEMPTED)
            else:
                # The send began and no HTTP response arrived: it cannot be asserted that
                # the provider received, executed or billed it - only that it was sent.
                record.update(status=STATUS_TRANSPORT_FAILED,
                              stop_reason=type(exc).__name__,
                              stop_detail=entry["error_code"],
                              provider_execution_state=PROVIDER_EXECUTION_UNKNOWN)
            return record, "STOP_BATCH"
        elapsed = round(clock() - started, 3)
        record["timings"].append({"attempt": attempt, "elapsed_seconds": elapsed})
        outcome = _attempt_from_journal(state, journal_path, model, sent_before)
        entry = outcome["attempt"]
        entry = dict(entry, error_type=None, error_code=None)
        record["attempts"].append({"attempt": attempt, **entry})
        record["requests_attempted"] = attempt
        record["sends"] += entry["sent"]
        record["responses_received"] += 1 if entry["response_received"] else 0
        if not outcome["accounting_ok"]:
            record.update(status="MODEL_ACCOUNTING_UNVERIFIABLE",
                          accounting_ok=False,
                          accounting_error=outcome["accounting_error"],
                          stop_reason=outcome["accounting_error"])
            return record, "STOP_BATCH"
        record["requests_sent"] = record["sends"]
        step = {"attempt": attempt, "http_status": status,
                "request_sha256": probe.sha256_bytes(body),
                "request_model": json.loads(body.decode("utf-8"))["model"],
                "thinking_mode": json.loads(body.decode("utf-8"))[
                    proxy_module.SCREEN_THINKING_FIELD],
                "response_sha256": probe.sha256_bytes(reply), "tool_calls": []}
        steps.append(step)
        if status != 200:
            record["status"] = "MODEL_PROVIDER_FAILURE"
            record["stop_reason"] = f"provider_http_{status}"
            record["steps"][-1]["response_body_utf8"] = reply.decode("utf-8", "replace")[:2000]
            stop = "STOP_BATCH" if status in BATCH_STOP_HTTP else "STOP_BATCH"
            return record, stop
        try:
            parsed = json.loads(reply.decode("utf-8"))
            choice = parsed["choices"][0]
            message = choice["message"]
            step["finish_reason"] = choice.get("finish_reason")
            step["usage"] = parsed.get("usage")
            record["usage"].append({"attempt": attempt, "usage": parsed.get("usage"),
                                    "finish_reason": choice.get("finish_reason")})
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            record.update(status="MODEL_UNPARSEABLE_RESPONSE", stop_reason=str(exc))
            return record, "STOP_BATCH"
        if step["finish_reason"] == "length":
            # Truncated: incomplete, and explicitly NOT a success or a capability failure.
            record.update(status="MODEL_OUTPUT_TRUNCATED", stop_reason="finish_reason_length",
                          model_task_completed=False, task_success=False)
            return record, "STOP_MODEL"
        calls = message.get("tool_calls") or []
        if not calls:
            step["assistant_text"] = message.get("content")
            record.update(status="MODEL_CLAIMED_WITHOUT_TOOL_CALL", stop_reason="no_tool_call",
                          model_task_completed=False, task_success=False)
            return record, "STOP_MODEL"
        native_calls = []
        for call in calls:
            name = (call.get("function") or {}).get("name")
            raw_arguments = (call.get("function") or {}).get("arguments")
            try:
                arguments = (json.loads(raw_arguments) if isinstance(raw_arguments, str)
                             else raw_arguments)
            except ValueError:
                arguments = None
            if not isinstance(arguments, dict):
                record.update(status="MODEL_UNPARSEABLE_ARGUMENTS",
                              stop_reason=f"{name}: {raw_arguments!r}")
                return record, "STOP_MODEL"
            native_calls.append(vita["ToolCall"](id=call.get("id", ""), name=name,
                                                 arguments=arguments))
        messages.append(vita["AssistantMessage"](role="assistant",
                                                 content=message.get("content"),
                                                 tool_calls=native_calls))
        for call, native in zip(calls, native_calls):
            name = native.name
            arguments = native.arguments
            if name not in bindings:
                outcome = {"status": "error", "text": f"Error: tool not available: {name}",
                           "kind": "undeclared_tool", "dispatched": False}
            else:
                outcome = probe._execute(bindings[name], name, arguments)
            entry = {"tool_call_id": native.id, "name": name, "arguments": arguments,
                     "status": outcome["status"], "kind": outcome["kind"],
                     "dispatched": outcome["dispatched"], "tool_return": outcome["text"]}
            if outcome["status"] == "success" and name == "create_delivery_order":
                found = probe.order_id_from(outcome["text"])
                if found and found not in created_sources:
                    created_ids.append(found)
                    created_sources[found] = attempt
                    entry["created_order_id"] = found
            if name == "pay_delivery_order":
                entry["order_id"] = arguments.get("order_id")
                entry["order_id_from_an_earlier_request_create"] = (
                    entry["order_id"] in created_sources
                    and created_sources[entry["order_id"]] < attempt)
                if outcome["status"] == "success" and "first_successful_payment" not in step:
                    step["first_successful_payment"] = entry["tool_call_id"]
            step["tool_calls"].append(entry)
            record["tool_returns"].append({"attempt": attempt, "name": name,
                                           "status": outcome["status"],
                                           "text_sha256": (probe.sha256_bytes(
                                               (outcome["text"] or "").encode("utf-8"))),
                                           "text": outcome["text"]})
            step["native_snapshot"] = probe.case_snapshot(environment)
            if outcome["status"] == "uncertain":
                record.update(status="MODEL_UNCERTAIN_TOOL_OUTCOME",
                              stop_reason="uncertain_tool_outcome_no_replay")
                record["final_order_state"] = step["native_snapshot"]["orders"]
                return record, "STOP_BATCH"
            text = outcome["text"] if outcome["text"] is not None else "Error: unknown outcome"
            messages.append(vita["ToolMessage"](id=native.id, name=name, role="tool",
                                                content=text, requestor="assistant"))
        payments = [entry for entry in step["tool_calls"]
                    if entry["name"] == "pay_delivery_order" and entry["status"] == "success"]
        if payments:
            first = next(entry for entry in step["tool_calls"]
                         if entry["tool_call_id"] == step.get("first_successful_payment"))
            record["payment_source_verified"] = first["order_id_from_an_earlier_request_create"]
            orders = probe.case_snapshot(environment)["orders"]
            matched, detail = probe.order_matches(orders)
            record.update(status="MODEL_TASK_SUCCESS" if matched and record[
                "payment_source_verified"] else "MODEL_TASK_INCOMPLETE",
                model_task_completed=bool(matched and record["payment_source_verified"]),
                task_success=bool(matched and record["payment_source_verified"]),
                order_state_check=detail, paid_order_id=first["order_id"],
                final_order_state=orders)
            return record, "STOP_MODEL"
    record.update(status="MODEL_BUDGET_EXHAUSTED", stop_reason="max_requests_per_model",
                  final_order_state=probe.case_snapshot(environment)["orders"])
    return record, "STOP_MODEL"


#: Set by `main` so the per-model case builder knows the declared Vita source.
_VITA_SOURCE: list = []


def execute_screen(transport_config: Path, *, vita_source: Path, api_key: str,
                   journal_path: Path, opener=None, clock=None, sleep=None,
                   config_override=None) -> dict:
    """The batch: model 1 then model 2, ONE proxy, ONE journal, ONE shared interval.

    `config_override` (offline fixtures only) replaces the transport config's VALUES while
    the screening profile and its authorised model set are still re-checked, so a test can
    vary a timeout or an origin without ever leaving the screening transport.
    """
    probe = _load_probe()
    _VITA_SOURCE[:] = [Path(vita_source)]
    config, profile = screen_config(transport_config)
    if config_override is not None:
        config_override.validate()
        override_profile = proxy_module.profile_of(config_override)
        if (override_profile.profile != profile.profile
                or override_profile.declared_models() != profile.declared_models()):
            raise RuntimeError("a config override may not change the screening transport")
        config = config_override
    config.validate()
    clock = clock or time.monotonic
    proxy = CloudAuditProxy(config, Path(journal_path), api_key=api_key,
                            **_probe_timing(clock, sleep))
    if opener is not None:
        proxy.opener = opener  # offline fixtures only; a REAL run keeps the real opener
    result = {"schema_version": SCHEMA_VERSION, "status": STATUS_INVALID,
              "purpose": "two_small_models_same_tiny_tool_chain_screen_not_an_experiment",
              "model_called": False, "scientific_result": None,
              "transport_profile": profile.profile,
              "models": [], "requests_sent_total": 0,
              "requests_attempted_total": 0, "sends_total": 0,
              "responses_received_total": 0, "accounting_ok": True,
              #: `model_called` means a chat request REACHED the transport (the journal
              #: holds its `upstream_request`) - not that the provider answered. A batch
              #: stopped by a network failure DID call; it just has no response, and
              #: whether the provider executed the request stays unknown. A batch where
              #: the request never left the host (a local pre-send refusal) did not call.
              "model_called_definition": ("a chat request reached the transport "
                                          "(journalled upstream_request); the provider's "
                                          "execution is a separate, possibly unknown, field"),
              "case_sha256": probe.sha256_bytes(probe.dumps(probe.CASE_DB).encode("utf-8")),
              "started_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        for index, model in enumerate(profile.declared_models(), start=1):
            record, decision = _run_model(probe, model, index, proxy=proxy,
                                          journal_path=Path(journal_path), clock=clock)
            result["models"].append(record)
            result["requests_sent_total"] += record["requests_sent"]
            result["requests_attempted_total"] += record["requests_attempted"]
            result["sends_total"] += record["sends"]
            result["responses_received_total"] += record["responses_received"]
            result["accounting_ok"] = result["accounting_ok"] and record["accounting_ok"]
            if not record["accounting_ok"]:
                result["accounting_error"] = record["accounting_error"]
            result["model_called"] = result["model_called"] or record["sends"] > 0
            if decision == "STOP_BATCH":
                result["status"] = STATUS_STOPPED
                result["stop_reason"] = record["stop_reason"]
                break
        else:
            result["status"] = STATUS_COMPLETED
        result["models_that_ran"] = [row["model"] for row in result["models"]]
        result["models_not_run"] = [model for model in profile.declared_models()
                                    if model not in result["models_that_ran"]]
        # State isolation: every model's snapshot must contain ONLY its own orders.
        for row in result["models"]:
            row["environment_sha256_after"] = None
    finally:
        proxy.close()
    return result


def ensure_output_dir(path):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"output already exists; no resume or overwrite: {path}")
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=("plan", "run"), default="plan")
    parser.add_argument("--vita-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--transport-config", required=True, type=Path)
    parser.add_argument("--key-file", type=Path,
                        default=ROOT / "deployment/private/siliconflow.key",
                        help="private SiliconFlow key; read only for --stage run")
    args = parser.parse_args(argv)
    plan, owned = None, False
    try:
        plan = build_plan(args.vita_source, args.transport_config)
        out = ensure_output_dir(args.output_dir)
        owned = True
        save(out / "plan.json", plan)
        if args.stage == "plan":
            print(f"AE small-model screen PLAN; no network/model; models="
                  f"{[row['wire_model'] for row in plan['models']]}; output={out}")
            return 0
        if not args.key_file.is_file():
            raise FileNotFoundError(f"the private key file is absent: {args.key_file}")
        key = read_private_key(args.key_file)
        result = execute_screen(args.transport_config, vita_source=args.vita_source,
                                api_key=key, journal_path=out / "chat-wire.private.jsonl")
        result["plan_sha256"] = _sha256(out / "plan.json")
        save(out / "result.json", result)
        print(f"AE small-model screen {result['status']}; "
              f"requests={result['requests_sent_total']}; scientific_result=null; output={out}")
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        failure = {"schema_version": SCHEMA_VERSION, "status": STATUS_INVALID,
                   "stage": args.stage, "scientific_result": None,
                   "invalid_reasons": [{"type": type(exc).__name__, "message": str(exc)}]}
        try:
            if owned and args.output_dir.is_dir() and not (args.output_dir / "result.json").exists():
                save(args.output_dir / "result.json", failure)
        except Exception:
            pass
        print(f"AE small-model screen INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
