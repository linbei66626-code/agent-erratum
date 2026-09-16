"""Offline cloud-input consistency audit for the AE-01 single-t4 capability run.

This is a READ-ONLY checker over an already captured run directory and an
already closed cloud proxy journal:

    <run-dir>/plan.json          production build_plan output
    <run-dir>/result.json        production execute_capability output
    <run-dir>/events.jsonl       driver emit stream (agent_created, bridge_event, ...)
    <run-dir>/letta-http.jsonl   loopback Letta transport journal (request/response)
    <proxy-journal>              CloudAuditProxy journal (cloud_open .. cloud_close)

Transport structure is checked first by the real ``ae_cloud_audit``
``audit_cloud_journal``; this module never re-implements that gate. It then
rebuilds the expected model inputs from the plan, the creation request, the
Letta message POSTs and the execution events, and compares them with the actual
cloud ``client_request`` bodies.

Boundaries (deliberate, fail-closed):
  * ``transport_capture_checked`` and ``input_audit_passed`` are separate.
  * ``task_success`` and ``scientific_result`` stay null; no token, KV-prefix or
    cache-agreement claim is made or possible from these captures.
  * Cloud normalization accepts only the transforms the existing proxy declares
    (recorded ``changes``); anything else fails as ``unexpected_cloud_change``.
  * More than one cloud chat call inside one Letta POST window fails closed as
    ``unsupported_multiple_model_calls_per_post`` instead of auditing the first.
  * Error entries carry gate codes only; no credential or message body is echoed.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from ae_adapter import MEMORY_TOOL
from ae_capability import CAPABILITY_SYSTEM, EVIDENCE_TEXT, NEW_SUGAR_FACT, OLD_SUGAR_FACT
from ae_cloud_audit import audit_cloud_journal
from ae_cloud_proxy import (BYTE_GATE_COUNT_BASIS, COMPAT_PROFILE, MODEL, ORIGIN,
                            PROFILES, CloudConfig, normalize_request, profile_of)
from ae_http import _request_path
from ae_input_audit import (
    AuditFailure, assistant_message, digest, native_messages, need, no_nulls, only, read_stable,
    rows, tool_schemas, tool_text, user_text, utc, wire,
)
from ae_inputs import canonical_sha256
from ae_model_proxy import decode_object

SCHEMA_VERSION = "ae-cloud-input-audit-0.1"
TASK_PROFILE = "ae-cloud-capability-0.1"
TASK_NUMBER = 4
SUBTASK_ID = "sub_U000828_4"
USER_ID = "U000828"
BLOCK_LABEL = "ae_preferences"
MODELS_PATH = "/v1/models"
CHAT_PATH = "/v1/chat/completions"

# Every bound the capability run must pin, independent of the transport file.
PINNED_LIMITS = {
    "task_number": TASK_NUMBER,
    "context_window": 65536,
    "max_output_tokens": 2048,
    "auxiliary_output_tokens": 4096,
    "temperature": 0,
    "seed": 300,
    "block_char_limit": 8000,
    "max_rounds": 32,
    "max_steps": 3,
    "max_stage_posts": 64,
    "max_user_exchanges": 12,
    "max_tool_return_chars": 26214,
    "timeout_seconds": 180,
    "max_request_bytes": 2097152,
    "max_response_bytes": 16777216,
}
CLOUD_FIELDS = {"model", "messages", "tools", "tool_choice", "stream", "n", "max_tokens",
                "max_completion_tokens", "temperature", "top_p", "top_k", "frequency_penalty",
                "stop", "response_format", "user", "parallel_tool_calls"}


def declared_cloud_fields(profile):
    """The body fields a capture may carry: the pinned set plus the DECLARED transport's.

    A model-specific profile may name extra fields it supports - the pinned Letta
    protocol's `user`/`parallel_tool_calls` pass-through, or a provider's own mode
    switch. Nothing else becomes admissible: every sealed profile declares no extra
    field, so for them this set is exactly `CLOUD_FIELDS`.
    """
    return CLOUD_FIELDS | set(getattr(profile, "extra_request_fields", ()) or ())


def check_declared_transport_fields(body, profile, report):
    """Every field the declared transport REQUIRES must be present with its value."""
    problems = []
    for name, value in (getattr(profile, "required_request_fields", None) or {}).items():
        if name not in body:
            problems.append("declared_transport_field_missing")
        elif body[name] != value:
            problems.append("declared_transport_field_value_changed")
    for code in problems:
        report.append(code)
    return not problems


def cloud_change_reasons(changes, body, profile, limit):
    """Why a capture's recorded normalisation is not the declared one, if it is not.

    Legal, and bounded: ONE output-limit rename, plus - for each field the DECLARED
    profile lists as a supported pass-through - a `keep` row that names that field and
    records the value actually sent. A profile with no extra fields (every sealed one)
    therefore keeps exactly the old rename-only rule, while a model-specific transport
    can neither smuggle in an undeclared field nor hide one it declared.
    """
    declared = set(getattr(profile, "extra_request_fields", ()) or ())
    renames = [c for c in changes if c.get("operation") == "rename"]
    keeps = [c for c in changes if c.get("operation") == "keep"]
    reasons = []
    if len(renames) + len(keeps) != len(changes):
        reasons.append("unexpected_cloud_change")
    if len(changes) > 1 + len(declared):
        reasons.append("unexpected_cloud_change_count")
    for keep in keeps:
        field = keep.get("to")
        if (keep.get("from") != field or field not in declared or field not in body
                or keep.get("value") != body.get(field)):
            reasons.append("undeclared_cloud_change")
    if renames and not reasons:
        rename = renames[0]
        if (rename.get("from") != "max_completion_tokens" or rename.get("to") != "max_tokens"
                or rename.get("value") != limit):
            reasons.append("unexpected_cloud_rename")
    return reasons

# The production provenance writer (production_code_hashes) covers exactly these
# files; the audit requires this exact set and compares every digest to disk.
PRODUCTION_CODE_FILES = (
    "ae_inputs.py", "ae_adapter.py", "ae_http.py", "ae_probe.py", "ae_task_run.py",
    "ae_vita.py", "ae_capability.py", "ae_cloud_task.py", "ae_cloud_proxy.py",
    "scripts/ae_01_capability_probe.py",
)
SOURCE_FILES = (
    "ae_cloud_input_audit.py", "ae_cloud_audit.py", "ae_cloud_proxy.py", "ae_capability.py",
    "ae_input_audit.py", "ae_model_proxy.py", "ae_adapter.py",
    "scripts/ae_01_cloud_input_audit.py",
)
REF_PATTERN = re.compile(r"^t4/(?:user|history)/\d+$")
MESSAGE_POST_PATH = re.compile(r"/v1/agents/[^/]+/messages")
MEMORY_TOOL_NAME = MEMORY_TOOL["name"]

# Exact framing produced by the pinned Letta PromptGenerator / Memory renderer.
# Sources (commit 56ba9c25552605eec89de8ed3dc6394b625c1993):
#   letta/schemas/memory.py:143-173   Memory._render_memory_blocks_standard
#   letta/prompts/prompt_generator.py:26-90   compile_memory_metadata_block
#   letta/prompts/prompt_generator.py:107-179 get_system_message_from_compiled_memory
#   letta/constants.py:64             IN_CONTEXT_MEMORY_KEYWORD = "CORE_MEMORY"
# The memory section is NOT wrapped in an extra ae_preferences tag: the block label
# itself becomes the XML tag, and the CORE_MEMORY placeholder is replaced by
# `memory + "\n\n" + metadata`, so one blank line separates them.
# Pinned Letta OpenAI-history wire facts.
#   letta/constants.py:67                    TOOL_CALL_ID_MAX_LEN = 29
#   letta/schemas/message.py to_openai_dict  tool ids and tool_call_ids are
#                                            truncated to that length
#   letta/system.py:150-168                  package_function_response builds
#                                            {"status": "OK"|"Failed",
#                                             "message": <tool return text>,
#                                             "time": <local time>}
TOOL_CALL_ID_MAX_LEN = 29
TOOL_RETURN_WRAPPER_KEYS = {"status", "message", "time"}
MEMORY_BLOCK_HEADER = ("<memory_blocks>\nThe following memory blocks are currently engaged "
                       "in your core memory unit:\n\n")
MEMORY_BLOCK_CLOSE = "\n</memory_blocks>"
CORE_MEMORY_PLACEHOLDER = "{CORE_MEMORY}"


def render_memory_blocks(block, value):
    """Byte-exact `Memory._render_memory_blocks_standard` for a single block.

    One block means no inter-block separator newline, and the renderer ends with
    the closing `</memory_blocks>` wrapper and no trailing newline.
    """
    need(isinstance(block, dict), "memory_block_not_object")
    need(isinstance(value, str), "memory_block_not_text")
    label = block.get("label") or "block"
    description = block.get("description") or ""
    read_only = "\n- read_only=true" if block.get("read_only") else ""
    return (MEMORY_BLOCK_HEADER
            + "<" + label + ">\n<description>\n" + description + "\n</description>\n"
            + "<metadata>" + read_only + "\n- chars_current=" + str(len(value))
            + "\n- chars_limit=" + str(block.get("limit") if block.get("limit") is not None else 0)
            + "\n</metadata>\n<value>\n" + value + "\n</value>\n</" + label + ">\n"
            + MEMORY_BLOCK_CLOSE)


MEMORY_METADATA = re.compile(
    r"^<memory_metadata>\n"
    r"(?P<lines>(?:- [^\n]*\n)+)"
    r"</memory_metadata>$")
MEMORY_METADATA_AGENT = re.compile(r"^- AGENT_ID: (?P<agent_id>[^\n]+)$", re.M)
MEMORY_METADATA_CONVERSATION = re.compile(r"^- CONVERSATION_ID: [^\n]+$", re.M)
MEMORY_METADATA_RECOMPILED = re.compile(r"^- System prompt last recompiled: [^\n]+$", re.M)
MEMORY_METADATA_RECALL = re.compile(
    r"^- \d+ previous messages between you and the user are stored in recall memory$", re.M)
MEMORY_METADATA_ARCHIVAL = re.compile(
    r"^- \d+ total memories you created are stored in archival memory"
    r" \(use tools to access them\)$")
MEMORY_METADATA_TAGS = re.compile(r"^- Available archival memory tags: [^\n]*$")

# Pinned Vita auxiliary message formatting. The captured `native_calls` store
# the internal message dumps; the provider wire is built by
# `vita/utils/llm_utils.py:122-154 format_messages` (called from generate at
# line 185), which projects only the declared wire fields per role. Internal
# metadata (`timestamp`, `turn_idx`, `cost`, `usage`, `raw_data`, `requestor`,
# `error`) is not part of the wire, but it is dropped by an explicit per-role
# projection, never by a blanket deletion, and a field outside the role's
# declared set is rejected instead of ignored.
#
# The declared sets below are the exact default field sets of the pinned
# classes: `SystemMessage`, `ParticipantMessageBase` (user/assistant) and
# `ToolMessage` in `vita/data_model/message.py`. `tool` is not a participant:
# it declares no cost/usage/raw_data but does declare requestor/error.
AUXILIARY_INTERNAL_FIELDS = {
    "system": {"role", "content", "turn_idx", "timestamp"},
    "user": {"role", "content", "tool_calls", "turn_idx", "timestamp", "cost", "usage",
             "raw_data"},
    "assistant": {"role", "content", "tool_calls", "turn_idx", "timestamp", "cost", "usage",
                  "raw_data"},
    "tool": {"id", "name", "role", "content", "requestor", "error", "turn_idx", "timestamp"},
}
# Pinned `ToolCall` fields. `requestor` is declared internal state of the call
# (who asked for it) and is not emitted on the wire by the pinned formatter,
# which writes only id/type/function{name,arguments}; an unknown field inside a
# tool-call entry is rejected rather than projected away.
AUXILIARY_TOOL_CALL_FIELDS = {"id", "name", "arguments", "requestor"}


def auxiliary_wire_messages(messages):
    """Mirror the pinned `format_messages` for captured auxiliary requests.

    Returns the provider wire list for one captured request. A message with a
    role outside the pinned set, or with a field outside that role's declared
    internal set, is rejected rather than silently projected; likewise for a
    field inside an assistant tool-call entry. Fields that the pinned formatter
    declares but does not put on the wire are projected away, not deleted
    globally.
    """
    need(isinstance(messages, list) and messages, "missing_native_messages")
    output = []
    for message in messages:
        need(isinstance(message, dict), "unsupported_native_message")
        role = message.get("role")
        need(role in AUXILIARY_INTERNAL_FIELDS, "unsupported_native_message_role")
        need(not (set(message) - AUXILIARY_INTERNAL_FIELDS[role]),
             "unknown_native_auxiliary_field")
        if role in ("system", "user"):
            output.append({"role": role, "content": message.get("content")})
        elif role == "assistant":
            entry = {"role": "assistant", "content": message.get("content")}
            calls = message.get("tool_calls")
            if calls:
                need(isinstance(calls, list) and all(isinstance(c, dict) for c in calls),
                     "unsupported_native_tool_calls")
                wire_calls = []
                for call in calls:
                    need(not (set(call) - AUXILIARY_TOOL_CALL_FIELDS),
                         "unknown_native_tool_call_field")
                    need("name" in call and "arguments" in call,
                         "unsupported_native_tool_call")
                    wire_calls.append({"id": call.get("id"), "type": "function",
                                       "function": {"name": call["name"],
                                                    "arguments": json.dumps(call["arguments"])}})
                entry["tool_calls"] = wire_calls
            output.append(entry)
        else:
            output.append({"role": "tool", "content": message.get("content"),
                           "tool_call_id": message.get("id"), "name": message.get("name")})
    return output


def source_hashes(root=None) -> dict:
    """SHA256 of the checker and of every reused production helper module."""
    base = Path(root) if root else Path(__file__).resolve().parent
    hashes = {}
    for name in SOURCE_FILES:
        path = base / name
        if path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _artifact_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _looks_like_credential(value: str) -> bool:
    return bool(re.search(r"sk-[A-Za-z0-9]|Bearer\s+\S", value))


class CloudInputAudit:
    """One audit pass over one immutable capture set."""

    def __init__(self, run_dir, proxy_journal, *, config_path=None, dataset_path=None):
        self.run_dir = Path(run_dir)
        self.root = Path(__file__).resolve().parent
        #: The capture's own declared transport config, resolved from its `cloud_open` row.
        #: `config_gate` fills it; a gate that runs earlier (an auxiliary-only check) asks
        #: for it through `declared_profile()` instead of assuming the gate order.
        self.cloud_config = None
        self.proxy_journal = Path(proxy_journal)
        self.config_path = Path(config_path) if config_path else None
        self.dataset_path = Path(dataset_path) if dataset_path else None
        self.report = {
            "schema_version": SCHEMA_VERSION, "status": "INVALID",
            "transport_capture_checked": False, "input_audit_passed": False,
            "task_success": None, "scientific_result": None,
            "sources": {}, "code_sha256": source_hashes(),
            "frames": [], "auxiliary_mappings": [], "tool_mappings": [],
            "cloud_calls": {"models": 0, "agent": 0, "auxiliary": 0, "unaccounted": 0},
            "scope": {"user_id": USER_ID, "task_number": TASK_NUMBER, "subtask_id": SUBTASK_ID,
                      "t5_executed": None, "dataset_history_sent": None,
                      "memory_update_tool": None},
            "limitations": [
                "read-only over the captured files; a stable snapshot does not certify future requests",
                "no token / KV-prefix / cache agreement is checked or implied",
                "auxiliary role attribution is by content match to native_calls, not a cryptographic identity",
                "one Letta POST window containing more than one cloud call is reported unsupported",
                "the proxy journal's own read/client fields are not covered by the transport check",
                "if a real capture renders the memory block differently than the pinned Letta framing helper, this file fails closed rather than passing",
            ],
            "invalid_reasons": [],
        }

    # -- helpers -----------------------------------------------------------
    def fail(self, code: str):
        raise AuditFailure(code)

    def note(self, code: str):
        self.report["invalid_reasons"].append(
            {"code": code if not _looks_like_credential(code) else "redacted_failure_code"})

    # -- loading -----------------------------------------------------------
    def load(self):
        report = self.report
        self.plan = decode_object(read_stable(self.run_dir / "plan.json", report))
        self.result = decode_object(read_stable(self.run_dir / "result.json", report))
        self.events = rows(self.run_dir / "events.jsonl", report)
        self.http = rows(self.run_dir / "letta-http.jsonl", report)
        self.proxy = rows(self.proxy_journal, report)
        for extra in (self.config_path, self.dataset_path):
            if extra is not None:
                need(extra.is_file(), "declared_provenance_input_missing")
                report["sources"][str(extra)] = {"sha256": _artifact_hash(extra),
                                                 "bytes": extra.stat().st_size}
        report["sources"]["proxy_journal"] = {"sha256": _artifact_hash(self.proxy_journal),
                                              "bytes": self.proxy_journal.stat().st_size}

    # -- gates -------------------------------------------------------------
    def transport_gate(self):
        audit = audit_cloud_journal(self.proxy_journal)
        self.transport = audit
        self.report["transport"] = {
            "profile": audit.get("profile"),
            "transport_capture_checked": audit.get("transport_capture_checked"),
            "completed_chat_requests_checked": audit.get("completed_chat_requests_checked"),
            "issues": list(audit.get("issues") or []),
            "token_ids": None, "exact_prefix_reuse_verified": False,
            "task_input_audit_passed": None, "scientific_result": None,
        }
        if not audit.get("transport_capture_checked"):
            self.fail("transport_capture_check_failed")
        self.report["transport_capture_checked"] = True

    def declared_profile(self):
        """The transport profile the capture itself declares, or None without a capture.

        Read from the `cloud_open` row (never from a global) and cached, so a check that
        runs before `config_gate` resolves the SAME declaration instead of failing on an
        attribute nobody set. A partial in-process harness that drives ONE matcher has no
        capture to read; it then gets `None`, and every declared-field rule falls back to
        the STRICTEST one (the sealed field set, rename-only changes) rather than to an
        assumption about which transport it might have been.
        """
        if getattr(self, "cloud_config", None) is None:
            rows_ = getattr(self, "proxy", None)
            if not rows_:
                return None
            opened = only(rows_, "cloud_open")
            config = CloudConfig(**opened["config"])
            config.validate()
            self.cloud_config = config
        return profile_of(self.cloud_config)

    def config_gate(self):
        opened = only(self.proxy, "cloud_open")
        # The profile is whatever the run DECLARED (and the table accepts); it is never
        # re-derived from a global constant, so a new opt-in transport is audited by its
        # own declared origin/model/route.
        need(opened.get("profile") in PROFILES, "wrong_cloud_profile")
        config = CloudConfig(**opened["config"])
        config.validate()
        declared = profile_of(config)
        need(declared.profile == opened.get("profile"), "profile_config_mismatch")
        need(config.profile == self.transport["profile"], "profile_config_mismatch")
        need(config.max_requests <= 256, "safety_cap_exceeded")
        self.cloud_config = config
        plan_config = self.plan.get("config")
        need(isinstance(plan_config, dict), "plan_config_missing")
        need(plan_config == self.result.get("config"), "plan_and_result_config_differ")
        need(plan_config.get("schema_version") == TASK_PROFILE, "wrong_task_profile")
        need(plan_config.get("purpose") == "current_state_capability_diagnostic", "wrong_purpose")
        need(plan_config.get("expected_model") == config.model, "wrong_expected_model")
        need(plan_config.get("transport_profile") == config.profile, "wrong_transport_profile")
        need(plan_config.get("context_window_source") == "local_agent_budget_not_server_measurement",
             "context_budget_provenance_missing")
        pacing = plan_config.get("pacing")
        pacing_ok = pacing == {"driver_io_timeout_seconds": 900,
                               "proxy_upstream_io_timeout_seconds": 180,
                               "min_interval_seconds": 65}
        for key, value in PINNED_LIMITS.items():
            if key == "timeout_seconds" and pacing_ok:
                # Only the explicit reviewed pacing candidate may move the run I/O
                # timeout, and only to its declared value.
                need(plan_config.get(key) == pacing["driver_io_timeout_seconds"],
                     "pacing_driver_timeout_changed")
                continue
            need(plan_config.get(key) == value, "pinned_limit_changed_" + key)
        need(config.model == declared.internal_model
             and config.upstream_origin == declared.origin, "wrong_cloud_origin_or_model")
        need(config.max_output_tokens == plan_config["auxiliary_output_tokens"],
             "transport_output_ceiling_not_auxiliary_limit")
        need(config.max_request_bytes == plan_config["max_request_bytes"], "transport_request_bytes_differ")
        need(config.max_response_bytes == plan_config["max_response_bytes"], "transport_response_bytes_differ")
        if pacing_ok:
            # Exact triple only: driver 900 in the capability config, proxy
            # upstream 180, send interval 65; never loosened for other configs.
            need(config.io_timeout_seconds == pacing["proxy_upstream_io_timeout_seconds"],
                 "pacing_proxy_timeout_changed")
            need(config.pace_seconds == pacing["min_interval_seconds"],
                 "pacing_interval_changed")
        else:
            need(config.io_timeout_seconds == plan_config["timeout_seconds"],
                 "transport_io_timeout_differ")
            need(not config.pace_seconds, "unexpected_pacing_interval")
        if self.config_path is not None:
            declared = decode_object(read_stable(self.config_path, self.report))
            need(isinstance(declared, dict), "declared_transport_config_invalid")
            for key, value in declared.items():
                need(plan_config.get(key, value) == value, "declared_transport_config_differs_" + key)
        self.report["transport_config"] = {
            "profile": config.profile, "max_output_tokens": config.max_output_tokens,
            "max_request_bytes": config.max_request_bytes,
            "max_response_bytes": config.max_response_bytes,
            "max_requests": config.max_requests, "io_timeout_seconds": config.io_timeout_seconds,
        }

    def provenance_gate(self):
        """Verify plan/result provenance against the real files on disk.

        Nothing here is taken on trust: every production code digest is
        recomputed, the config file digest is recomputed, and the config
        canonical digest is recomputed with the production `canonical_sha256`.
        """
        report = self.report
        for label in ("plan", "result"):
            provenance = (self.plan if label == "plan" else self.result).get("provenance")
            need(isinstance(provenance, dict), label + "_provenance_missing")
            code = provenance.get("code_sha256")
            need(isinstance(code, dict), label + "_provenance_code_sha256_missing")
            need(set(code) == set(PRODUCTION_CODE_FILES),
                 label + "_provenance_code_file_set_changed")
            for name in PRODUCTION_CODE_FILES:
                path = self.root / name
                need(path.is_file(), "production_file_missing_" + name.replace("/", "_"))
                need(code[name] == hashlib.sha256(path.read_bytes()).hexdigest(),
                     label + "_provenance_code_sha256_mismatch_" + name.replace("/", "_"))
            need(provenance.get("live_letta_commit_verified") is False,
                 label + "_provenance_claims_letta_verification")
            need(provenance.get("project_git_commit") is None,
                 label + "_provenance_unexpected_git_commit")
            need(isinstance(provenance.get("python"), str) and provenance["python"],
                 label + "_provenance_python_missing")
            config = self.plan["config"]
            canonical = canonical_sha256(config)
            need(provenance.get("config_canonical_sha256") == canonical,
                 label + "_config_canonical_sha256_mismatch")
            if self.config_path is not None:
                declared = json.loads(self.config_path.read_text(encoding="utf-8"))
                need(declared == config, "declared_config_not_the_run_config")
                need(provenance.get("config_file_sha256") == _artifact_hash(self.config_path),
                     label + "_config_file_sha256_mismatch")
            else:
                need(isinstance(provenance.get("config_file_sha256"), str)
                     and len(provenance["config_file_sha256"]) == 64,
                     label + "_config_file_sha256_missing")
            report.setdefault("provenance", {})[label] = {
                "code_files": len(code), "config_canonical_sha256": canonical,
                "config_file_sha256": provenance.get("config_file_sha256"),
                "verified_against_disk": True,
            }
        plan_code = self.plan["provenance"]["code_sha256"]
        result_code = self.result["provenance"]["code_sha256"]
        need(plan_code == result_code, "plan_and_result_code_sha256_differ")
        need(self.plan["provenance"]["config_canonical_sha256"]
             == self.result["provenance"]["config_canonical_sha256"],
             "plan_and_result_config_canonical_sha256_differ")

    def dataset_gate(self):
        """Dataset declaration: required, optional, or explicitly unverified."""
        declared = self.plan.get("source")
        need(isinstance(declared, dict), "plan_source_provenance_missing")
        if self.dataset_path is not None:
            raw = read_stable(self.dataset_path, self.report)
            need(declared.get("sha256") == digest(raw), "dataset_declaration_mismatch")
            need(declared.get("path") == str(self.dataset_path), "dataset_path_declaration_mismatch")
            status = "verified_against_declared_dataset"
        else:
            status = "recorded_only_not_verified"
        self.report["dataset_verification"] = {
            "status": status,
            "declared_sha256": declared.get("sha256"),
            "declared_path": declared.get("path"),
            "required_for_input_audit_passed": False,
            "note": ("the dataset bytes are not re-verified without --dataset; a run without "
                     "it is reported as recorded_only_not_verified, never as passed"),
        }

    def window_gate(self):
        need(self.result.get("execution_complete") is True, "execution_not_complete")
        need(not self.result.get("invalid_reasons"), "result_reports_invalid")
        need(self.result.get("task_success") is None and self.result.get("scientific_result") is None,
             "result_claims_a_scientific_outcome")
        need(self.result.get("status") == "CAPABILITY_COMPLETED_AUDIT_PENDING",
             "unexpected_result_status")
        self.start = utc(self.result.get("started_at_utc"))
        self.end = utc(self.result.get("finished_at_utc"))
        terminated = self.result.get("execution", {}).get("termination_reason")
        need(terminated in {"agent_stop", "user_stop"}, "no_native_termination")
        scope = self.result.get("scope") or {}
        need(scope.get("task_number") == TASK_NUMBER and scope.get("subtask_id") == SUBTASK_ID,
             "wrong_result_scope")
        need(scope.get("t5_executed") is False, "t5_was_executed")
        self.report["scope"]["t5_executed"] = False

    def event_correlation_setup(self):
        bridge = [e for e in self.events if e.get("kind") == "bridge_event"]
        arms = {e.get("arm") for e in bridge}
        need(len(arms) <= 1, "multiple_bridge_arms_in_capability_run")
        self.bridge_arm = arms.pop() if arms else None
        self.bridge_requests = [e for e in bridge if e.get("event", {}).get("kind") == "request"]
        self.bridge_responses = [e for e in bridge if e.get("event", {}).get("kind") == "response"]
        self.tool_events = [e for e in bridge
                            if e.get("event", {}).get("kind") == "client_tool_result"]
        self.all_http_requests = [r for r in self.http if r.get("kind") == "request"]
        self.all_http_responses = [r for r in self.http if r.get("kind") == "response"]
        self.seen_refs, self.seen_calls = set(), set()
        self.pending, self.consumed_tool_events = {}, set()
        self.users_seen = 0

    def http_trace_gate(self):
        """Every bridge-traced request/response must match a transport journal record."""
        for event in self.bridge_requests:
            inner = event["event"]
            path = _request_path(inner.get("path") or "")
            event_time = utc(event["timestamp"])
            matches = [r for r in self.all_http_requests
                       if r.get("method") == inner.get("method")
                       and _request_path(r.get("path") or "") == path
                       and r.get("body") == inner.get("body")
                       # The emit hook runs microseconds before the transport
                       # journal write, so allow a small same-host skew.
                       and abs((utc(r["timestamp"]) - event_time).total_seconds()) <= 0.25]
            need(bool(matches), "http_request_missing_bridge_trace")
        for event in self.bridge_responses:
            matches = [r for r in self.all_http_responses
                       if r.get("body") == event["event"].get("body")]
            need(bool(matches), "http_response_missing_bridge_trace")
        # Direct driver calls (health, agent create) are not bridge-traced, so the
        # bridge frame count must be smaller by exactly that known set.
        direct = [r for r in self.all_http_requests
                  if (r.get("method") == "GET" and r.get("path") == "/v1/health/")
                  or (r.get("method") == "POST" and r.get("path") == "/v1/agents/")]
        need(len(self.bridge_requests) == len(self.all_http_requests) - len(direct),
             "unexpected_http_request_count")
        need(len(self.bridge_responses) == len(self.all_http_responses) - len(direct),
             "unexpected_http_response_count")

    # -- creation ----------------------------------------------------------
    def agent_creation(self):
        report = self.report
        created = [e for e in self.events if e.get("kind") == "agent_created"]
        need(len(created) == 1, "missing_or_extra_agent_created_event")
        payload = created[0].get("payload")
        need(isinstance(payload, dict), "agent_creation_payload_missing")
        declared = self.plan.get("agent_payload")
        need(isinstance(declared, dict), "plan_agent_payload_missing")
        # The driver randomizes only the agent name; every other declared field
        # must match the plan exactly.
        declared_fields = {k: v for k, v in declared.items() if k != "name"}
        observed_fields = {k: v for k, v in payload.items() if k != "name"}
        need(observed_fields == declared_fields, "creation_payload_differs_from_plan")
        need(re.fullmatch(r"ae-capability-cloud-[0-9a-f]{12}", payload.get("name") or "") is not None,
             "unexpected_agent_name")
        aid = created[0].get("agent_id")
        need(isinstance(aid, str) and aid, "agent_created_event_id_missing")
        need(self.result.get("agent_id") == aid, "result_agent_id_mismatch")
        posts = [r for r in self.http if r.get("kind") == "request" and r.get("method") == "POST"
                 and r.get("path") == "/v1/agents/"]
        need(len(posts) == 1, "missing_or_duplicate_agent_create_request")
        body = posts[0].get("body")
        need(body == payload, "agent_create_http_body_differs_from_event_payload")
        need(body.get("initial_message_sequence") == [], "initial_message_sequence_not_empty")
        need(body.get("include_base_tools") is False and body.get("tool_ids") == []
             and body.get("include_default_source") is False, "unexpected_creation_tools_or_sources")
        need(body.get("message_buffer_autoclear") is False and body.get("enable_sleeptime") is False,
             "unexpected_memory_channel_setting")
        blocks = body.get("memory_blocks")
        need(isinstance(blocks, list) and len(blocks) == 1, "unexpected_created_block_count")
        block = blocks[0]
        need(block.get("label") == BLOCK_LABEL, "wrong_memory_block_label")
        need(block.get("limit") == PINNED_LIMITS["block_char_limit"], "wrong_memory_block_limit")
        need(body.get("system") == self.plan["agent_payload"]["system"], "creation_system_changed")
        need(CAPABILITY_SYSTEM.splitlines()[0] in body["system"], "capability_system_missing")
        blob = json.dumps(body, ensure_ascii=False)
        need(OLD_SUGAR_FACT not in blob, "old_preference_leaked_into_creation")
        need(NEW_SUGAR_FACT in block.get("value", ""), "current_preference_missing_from_block")
        need(EVIDENCE_TEXT not in blob, "history_evidence_leaked_into_creation")
        # Health and agent creation are direct driver calls, not bridge frames.
        self.state = {"system": body["system"], "block": block, "value": block["value"], "history": []}
        self.agent_id = aid
        report["frames"].append({"kind": "agent_creation", "agent_id": aid,
                                 "block_sha256": digest(block["value"].encode()), "passed": True})

    def stage_gate(self):
        starts = [e for e in self.events if e.get("kind") == "stage_start"]
        need(len(starts) == 1, "missing_or_duplicate_stage_start")
        tools = starts[0].get("tools")
        need(isinstance(tools, list) and tools, "stage_start_tools_missing")
        schemas = tool_schemas(tools)
        for name in schemas:
            need(MEMORY_TOOL_NAME not in name and "preference_memory" not in name,
                 "second_memory_backend_in_stage_tools")
        need(self.report["scope"]["memory_update_tool"] is None, "scope_already_set")
        self.report["scope"]["memory_update_tool"] = False
        self.stage_start = starts[0]
        self.stage_tools = set(schemas)
        self.declared_tools = schemas
        self.auxiliary_agent_tools = None

    def native_capture(self):
        """Single-t4 lifecycle gate and ordered native-call snapshots.

        The real driver emits `agent_created`, `stage_start`, zero or more
        `simulated_user`, then exactly one `task_terminated` and one
        `task_complete` for the one t4 subtask (ae_capability.py:501-588). Each
        snapshot must extend the previous one prefix by prefix in that order; the
        longest snapshot is never used to hide a later rollback.
        """
        stage_starts = [e for e in self.events if e.get("kind") == "stage_start"]
        terminated = [e for e in self.events if e.get("kind") == "task_terminated"]
        completed = [e for e in self.events if e.get("kind") == "task_complete"]
        need(len(stage_starts) == 1, "missing_or_duplicate_stage_start")
        need(len(terminated) == 1, "missing_or_duplicate_task_terminated")
        need(len(completed) == 1, "missing_or_duplicate_task_complete")
        lifecycle = (("stage_start", stage_starts[0]),
                     ("task_terminated", terminated[0]),
                     ("task_complete", completed[0]))
        for label, event in lifecycle:
            need(event.get("task") == SUBTASK_ID, "unexpected_task_in_" + label)
        positions = [self.events.index(event) for _, event in lifecycle]
        need(positions == sorted(positions) and len(set(positions)) == 3,
             "task_lifecycle_order_changed")
        previous = []
        for label, event in lifecycle:
            snapshot = event.get("snapshot")
            need(isinstance(snapshot, dict), "missing_snapshot_in_" + label)
            native = snapshot.get("native_calls")
            need(isinstance(native, list), "missing_auxiliary_capture_in_" + label)
            need(len(native) >= len(previous), "native_calls_snapshot_rollback_in_" + label)
            need(native[:len(previous)] == previous, "native_call_history_changed_in_" + label)
            previous = native
        self.native_calls = previous

    # -- Letta POSTs -------------------------------------------------------
    def agent_posts(self):
        posts = [r for r in self.http if r.get("kind") == "request" and r.get("method") == "POST"
                 and MESSAGE_POST_PATH.fullmatch(r.get("path") or "")]
        need(posts, "no_agent_message_posts")
        need(len(posts) <= PINNED_LIMITS["max_stage_posts"], "stage_post_limit_exceeded")
        declared = tool_schemas(self.stage_start["tools"])
        for post in posts:
            body = post.get("body")
            need(isinstance(body, dict), "message_post_body_missing")
            need(set(body) == {"messages", "client_tools", "max_steps",
                               "include_compaction_messages"}, "unexpected_message_post_fields")
            need(body.get("max_steps") == PINNED_LIMITS["max_steps"], "message_post_max_steps_changed")
            need(body.get("include_compaction_messages") is True, "compaction_messages_not_requested")
            # client_tools are checked bare, exactly as ae_probe/ae_task_run submit
            # them and exactly as the pinned frame_check helper expects.
            actual = tool_schemas(body.get("client_tools") or [])
            need(set(actual) == set(declared), "client_tools_differ_from_declared")
            for name, schema in actual.items():
                need(schema == declared[name], "client_tool_schema_changed")
            need(MEMORY_TOOL_NAME not in actual, "memory_update_tool_present")
            need(not any("preference_memory" in n for n in actual), "second_memory_backend_present")
        blob = json.dumps([p.get("body") for p in posts], ensure_ascii=False)
        need("dataset_history" not in blob, "dataset_history_was_sent")
        need("t5/" not in blob and "sub_U000828_5" not in blob, "future_turn_leaked")
        self.report["scope"]["dataset_history_sent"] = False
        self.posts = posts

    def classify_new_input(self, post):
        """Turn one POST's declared messages into the expected new-input list.

        Runs after the matching Letta response has populated ``pending`` so every
        submitted tool return can be tied back to an observed model tool call.
        """
        body = post["body"]
        need(not body.get("messages") or isinstance(body["messages"], list), "messages_not_list")
        new_input, users, tool_returns = [], 0, 0
        for message in body.get("messages") or []:
            need(isinstance(message, dict), "unexpected_submitted_message_shape")
            role = message.get("role")
            if role == "user":
                self.check_user_message(message)
                new_input.append({"role": "user", "content": message["content"]})
                users += 1
            elif message.get("type") == "tool_return" and role is None:
                for entry in self.check_tool_return(message):
                    new_input.append(entry)
                tool_returns += 1
            else:
                self.fail("unexpected_submitted_message_shape")
        need(new_input, "message_post_adds_no_new_input")
        post["_new_input"] = new_input
        counts = self.report.setdefault("counts", {"agent_posts": len(self.posts),
                                                   "user_messages": 0, "tool_returns": 0})
        counts["user_messages"] += users
        counts["tool_returns"] += tool_returns
        counts["agent_posts"] = len(self.posts)

    def check_user_message(self, message):
        need(message.get("type", "message") == "message", "unexpected_user_message_type")
        content = message.get("content")
        need(isinstance(content, str), "user_input_not_string")
        material = decode_object(content.encode("utf-8"))
        ref = material.get("ref")
        need(isinstance(ref, str) and REF_PATTERN.fullmatch(ref), "undeclared_user_ref")
        need(ref not in self.seen_refs, "duplicate_user_ref")
        self.seen_refs.add(ref)
        source = material.get("source")
        if source == "current_task":
            need(set(material) == {"source", "subtask_id", "domain", "current_time",
                                   "domain_policy", "instruction", "ref"},
                 "current_task_fields_changed")
            need(material["subtask_id"] == SUBTASK_ID, "wrong_subtask_in_current_task")
            need(material["instruction"] == self.plan["task_preview"]["instruction"],
                 "current_task_instruction_changed")
            need(material["domain"] == self.plan["task_preview"]["domain"], "wrong_domain")
            need(material["domain_policy"] == self.stage_start.get("domain_policy"),
                 "domain_policy_changed")
            need(self.users_seen == 0, "duplicate_current_task_message")
            self.users_seen += 1
            self.report["frames"].append({"kind": "current_task", "ref": ref, "passed": True})
        elif source == "runtime_user":
            need(set(material) == {"source", "ref", "content"}, "runtime_user_fields_changed")
            need(isinstance(material.get("content"), str) and material["content"].strip(),
                 "runtime_user_content_missing")
            need(self.users_seen == 1, "runtime_user_before_current_task")
            self.report["frames"].append({"kind": "runtime_user", "ref": ref, "passed": True})
        else:
            self.fail("unrecognized_user_message_source")

    def check_submitted_tool_return(self, submitted, item, *, source):
        """Compare one tool return on a wire the CALLER selected.

        The audited message never chooses its own validation branch. `source`
        is `submission` for the Letta tool_returns body entry (raw
        status/tool_return) or `history` for the OpenAI history tool message
        (packaged wrapper content, truncated id, no status fields). Each wire has
        a strict, mutually exclusive field set, so submission fields mixed into a
        history message are rejected instead of switching branches.
        """
        need(source in {"submission", "history"}, "unknown_tool_return_source")
        returned = item["returned"]
        if source == "submission":
            need(set(submitted) == {"type", "tool_call_id", "tool_return", "status"},
                 "unexpected_submission_tool_fields")
            need(returned.get("status") in {"success", "error"},
                 "unexpected_native_tool_status")
            need(submitted.get("tool_call_id") == returned.get("tool_call_id"), "tool_id_changed")
            need(submitted.get("status") == returned.get("status"), "tool_status_changed")
            need(submitted.get("tool_return") == returned.get("tool_return"),
                 "tool_return_content_changed")
            return
        # OpenAI history wire.
        need(set(submitted) <= {"role", "content", "tool_call_id", "name"},
             "unexpected_history_tool_fields")
        need(submitted.get("role") == "tool", "history_tool_role_changed")
        need(self.history_tool_return_id_matches(submitted.get("tool_call_id"),
                                                 returned.get("tool_call_id")),
             "tool_id_changed")
        content = submitted.get("content")
        need(isinstance(content, str), "tool_return_wrapper_not_text")
        try:
            wrapper = decode_object(content.encode("utf-8"))
        except Exception:
            self.fail("tool_return_wrapper_shape_changed")
        need(isinstance(wrapper, dict) and set(wrapper) == TOOL_RETURN_WRAPPER_KEYS,
             "tool_return_wrapper_shape_changed")
        need(isinstance(wrapper["time"], str), "tool_return_wrapper_time_changed")
        expected_status = "OK" if returned.get("status") == "success" else "Failed"
        need(wrapper["status"] == expected_status, "tool_return_wrapper_status_changed")
        need(wrapper["message"] == returned.get("tool_return"),
             "tool_return_content_changed")

    def check_tool_return(self, message):
        returns = message.get("tool_returns")
        need(isinstance(returns, list) and returns, "empty_tool_return_batch")
        appended = []
        for returned in returns:
            tid = returned.get("tool_call_id")
            need(tid in self.pending, "return_without_observed_model_tool_call")
            executed = self.pending[tid]
            self.check_submitted_tool_return(returned, {"returned": returned},
                                             source="submission")
            need(executed["name"] != MEMORY_TOOL_NAME, "undeclared_memory_update_attempted")
            matches = [e for e in self.tool_events if e["sequence"] not in self.consumed_tool_events
                       and e.get("event", {}).get("call", {}).get("tool_call_id") == tid]
            need(len(matches) == 1, "tool_return_not_same_as_execution_event")
            event = matches[0]["event"]
            need(event.get("result") == returned, "tool_return_body_changed")
            need(event.get("call") == executed, "tool_call_changed_before_execution")
            self.consumed_tool_events.add(matches[0]["sequence"])
            self.report["tool_mappings"].append({"tool_call_id": tid, "name": executed["name"],
                                                 "status": returned.get("status")})
            del self.pending[tid]
            appended.append({"role": "tool", "returned": deepcopy(returned),
                             "tool_call_id": tid})
        return appended

    # -- cloud journal -----------------------------------------------------
    def cloud_calls(self):
        records = self.proxy
        only(records, "cloud_open")
        chat_groups, models = [], []
        cursor, seen = 1, set()
        armed = [r for r in records if r.get("kind") == "capacity_armed"]
        need(len(armed) <= 1, "duplicate_capacity_armed_record")
        gated = bool(armed)
        # A gated capture writes one more row per request (the gate's own count) and one
        # arming row before the first request. Both are handled by KIND here: the sealed
        # protocols arm nothing, so their walk is exactly what it always was.
        while cursor < len(records) - 1:
            while cursor < len(records) - 1 and records[cursor].get("kind") == "capacity_armed":
                cursor += 1
            if cursor >= len(records) - 1:
                break
            client = records[cursor]
            rid = client.get("request_id")
            need(client.get("kind") == "client_request" and isinstance(rid, str) and rid not in seen,
                 "unexpected_or_duplicate_client_event")
            seen.add(rid)
            method, route = client.get("method"), client.get("path")
            if (method, route) == ("GET", MODELS_PATH):
                group = records[cursor:cursor + 3]
                need([r.get("kind") for r in group]
                     == ["client_request", "upstream_request", "upstream_response"],
                     "incomplete_models_call")
                need(wire(client) == b"", "models_request_not_bodyless")
                response = decode_object(wire(group[2]))
                data = response.get("data")
                need(isinstance(data, list), "catalog_data_missing")
                # The catalog must expose the WIRE model this transport sends; the
                # internal alias stays a project-side name and the mapping is recorded.
                need(len([m for m in data if isinstance(m, dict)
                          and m.get("id") == profile_of(self.cloud_config).wire_model]) == 1,
                     "catalog_model_missing_or_duplicated")
                models.append({"request_id": rid, "timestamp": utc(client["timestamp"])})
                cursor += 3
                continue
            need((method, route) == ("POST", CHAT_PATH), "unknown_cloud_route")
            paced = float(self.cloud_config.pace_seconds) > 0
            expected_kinds = ["client_request", "normalized_request"]
            if gated:
                expected_kinds.append("capacity_check")
            if paced:
                expected_kinds.append("pace_wait")
            expected_kinds += ["upstream_request", "upstream_response", "cloud_summary"]
            group = records[cursor:cursor + len(expected_kinds)]
            need([r.get("kind") for r in group] == expected_kinds,
                 "incomplete_or_extra_model_call_events")
            need(all(r.get("request_id") == rid for r in group), "cloud_request_id_mismatch")
            raw = wire(client)
            normalized = wire(group[1])
            if gated:
                # The count must describe THIS request's normalized bytes, for THIS
                # role, under the declared basis - otherwise it is not evidence.
                check = group[expected_kinds.index("capacity_check")]
                need(check.get("input_sha256") == hashlib.sha256(normalized).hexdigest(),
                     "capacity_check_does_not_describe_this_request")
                need(check.get("role") == client.get("role"),
                     "capacity_check_role_differs_from_the_request_role")
                need(check.get("fits") is True, "sent_request_did_not_fit_the_declared_window")
                need(isinstance(check.get("count_source"), str) and check["count_source"],
                     "capacity_check_without_a_count_source")
                if check.get("capacity_basis") == BYTE_GATE_COUNT_BASIS:
                    # A provider with no verified tokenizer is bounded by its own request
                    # BYTES against a declared budget. The record must say so, must not
                    # carry a token count, and the measured bytes must really fit.
                    need("input_tokens" not in check,
                         "byte_gate_record_must_not_carry_a_token_count")
                    need(isinstance(check.get("input_bytes"), int)
                         and check["input_bytes"] == len(normalized),
                         "byte_gate_record_does_not_describe_this_request")
                    need(isinstance(check.get("request_byte_budget"), int)
                         and check["request_byte_budget"] > 0,
                         "byte_gate_record_without_a_budget")
                    need(check["input_bytes"] <= check["request_byte_budget"],
                         "byte_gate_record_over_budget_but_sent")
                    need(check.get("operation_guard_not_a_capacity_guarantee") is True,
                         "byte_gate_record_claims_a_capacity_guarantee")
                else:
                    need(check.get("input_tokens", 0) + check.get("output_reserve_tokens", 0)
                         <= check.get("context_window", 0),
                         "capacity_check_arithmetic_inconsistent")
            try:
                expected_normalized, changes = normalize_request(raw, self.cloud_config)
            except Exception:
                self.fail("unsupported_cloud_normalization")
            need(expected_normalized == normalized, "unaccounted_cloud_normalization")
            need(decode_object(normalized).get("model") == profile_of(self.cloud_config).wire_model,
                 "cloud_model_changed")
            need(("pace_wait" in expected_kinds) if paced
                 else ("pace_wait" not in [r.get("kind") for r in group]),
                 "missing_or_unexpected_pace_event")
            summary = group[-1]
            need(summary.get("usage_source") == "provider_reported_not_independently_tokenized",
                 "usage_provenance_changed")
            need(summary.get("token_ids") is None and summary.get("kv_reuse_verified") is False,
                 "unsupported_token_or_kv_claim")
            chat_groups.append({
                "request_id": rid, "body": decode_object(raw), "normalized": decode_object(normalized),
                "changes": changes, "response": decode_object(wire(group[-2])),
                "summary": summary, "start": utc(client["timestamp"]),
                "end": utc(summary["timestamp"]),
            })
            cursor += len(expected_kinds)
        need(cursor == len(records) - 1, "unconsumed_proxy_records")
        need(bool(chat_groups), "no_cloud_chat_requests")
        need(len(models) == 1, "unexpected_model_catalog_call_count")
        self.chat_calls = sorted(chat_groups, key=lambda c: c["start"])
        self.report["cloud_calls"]["models"] = len(models)

    def map_to_posts(self):
        responses = {r.get("request_id"): r for r in self.http if r.get("kind") == "response"}
        for call in self.chat_calls:
            need(self.start < call["start"] and call["end"] < self.end,
                 "cloud_call_outside_run_window")
        used = set()
        self.known_tool_ids = set()
        for event in self.tool_events:
            call_id = (event.get("event", {}).get("call") or {}).get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                self.known_tool_ids.add(call_id)
        for chat in self.chat_calls:
            for choice in (chat.get("response") or {}).get("choices") or []:
                for tool_call in ((choice.get("message") or {}).get("tool_calls") or []):
                    if isinstance(tool_call.get("id"), str) and tool_call["id"]:
                        self.known_tool_ids.add(tool_call["id"])
        ordered = sorted(self.posts, key=lambda item: utc(item["timestamp"]))
        generations = self.bind_generation_responses(ordered)
        for index, post in enumerate(ordered):
            response = responses.get(post.get("request_id"))
            need(isinstance(response, dict) and response.get("http_status") == 200,
                 "letta_post_without_successful_response")
            generation = generations[post["request_id"]]
            generation_row = next(r for r in self.all_http_responses
                                  if r.get("body") is generation)
            a, z = utc(post["timestamp"]), utc(generation_row["timestamp"])
            inside = [c for c in self.chat_calls if a < c["start"] and c["end"] < z]
            if len(inside) > 1:
                self.fail("unsupported_multiple_model_calls_per_post")
            need(len(inside) == 1, "letta_post_without_mapped_model_call")
            call = inside[0]
            need(call["request_id"] not in used, "cloud_call_mapped_twice")
            used.add(call["request_id"])
            post["_call"] = call
            post["_response"] = response
            post["_generation_response"] = generation
            if not self.bridge_frame_count(post, generation):
                self.fail("http_request_missing_bridge_trace")
            self.check_model_output(post, response)
            # The matching response is now known, so tool returns can be tied to
            # observed model tool calls and the expected new input can be built.
            self.classify_new_input(post)
            self.check_agent_model_call(call, post)
            self.report["frames"].append({"kind": "agent_frame", "request_id": call["request_id"],
                                          "agent_id": self.agent_id,
                                          "block_sha256": digest(self.state["value"].encode()),
                                          "passed": True})
        for row in self.http:
            # /v1/health/ is a direct driver call and is not bridge-traced.
            if (row.get("kind") == "request" and row.get("method") == "GET"
                    and row.get("path") != "/v1/health/"):
                need(self.bridge_frame_count(row) == 1, "http_request_missing_bridge_trace")
        need(not self.pending, "unreturned_pending_tool_call")
        self.report["cloud_calls"]["agent"] = len(used)

    def bind_generation_responses(self, ordered_posts):
        """The matching Letta /messages response per POST, never reused.

        Bound by the transport journal's own request/response request_id, not by a
        time window, so an in-between session-inspection GET can never be taken as
        the generation response.
        """
        bindings = {}
        used = set()
        for post in ordered_posts:
            path = _request_path(post.get("path") or "")
            candidates = [r for r in self.all_http_responses
                          if r.get("request_id") != post.get("request_id")
                          and r.get("http_status") == 200
                          and r.get("request_id") not in used
                          and MESSAGE_POST_PATH.fullmatch(_request_path(
                              r.get("path") or path) or path) is not None]
            # The response record carries no path, so pair it with the /messages
            # request that precedes it and follows the previous POST's response.
            ordered = sorted(self.http, key=lambda row: row.get("sequence", 0))
            index = next(i for i, row in enumerate(ordered)
                         if row is post)
            response = None
            for row in ordered[index + 1:]:
                if row.get("kind") == "response":
                    response = row
                    break
            need(isinstance(response, dict) and response.get("http_status") == 200,
                 "missing_generation_response")
            need(response.get("request_id") not in used, "reused_generation_response")
            used.add(response.get("request_id"))
            bindings[post["request_id"]] = response.get("body")
        return bindings

    def bridge_frame_count(self, post, generation_response=None):
        """Consume the bridge trace frame for one HTTP request/response pair.

        The response frame is identified by ``generation_response`` when given, so
        repeated identical Letta responses can never be exchanged between POSTs.
        """
        body = post.get("body")
        path = _request_path(post.get("path") or "")
        event_time = utc(post["timestamp"])
        requests = [e for e in self.bridge_requests
                    if e["event"].get("method") == post.get("method")
                    and _request_path(e["event"].get("path") or "") == path
                    and e["event"].get("body") == body
                    and abs((utc(e["timestamp"]) - event_time).total_seconds()) <= 0.25]
        response_body = generation_response
        if response_body is None:
            for row in self.http:
                if (row.get("kind") == "response"
                        and row.get("request_id") == post.get("request_id")):
                    response_body = row.get("body")
        responses = [e for e in self.bridge_responses if e["event"].get("body") == response_body]
        if not requests or not responses:
            return 0
        # Consume the earliest matching frames so identical repeated bodies can
        # never be paired across POSTs out of order.
        self.bridge_requests.remove(requests[0])
        self.bridge_responses.remove(responses[0])
        return 1

    def check_model_output(self, post, response):
        messages = response.get("body", {}).get("messages")
        need(isinstance(messages, list), "letta_response_messages_missing")
        approvals = [m for m in messages if m.get("message_type") == "approval_request_message"]
        assistants = [m for m in messages if m.get("message_type") == "assistant_message"]
        need(bool(approvals) or bool(assistants), "letta_response_has_no_model_output")
        observed = []
        for approval in approvals:
            calls = approval.get("tool_calls")
            if calls is None:
                calls = [approval.get("tool_call")]
            need(isinstance(calls, list) and calls and all(isinstance(c, dict) for c in calls),
                 "incomplete_pending_tool_calls")
            for call in calls:
                need(isinstance(call.get("tool_call_id"), str) and call["tool_call_id"],
                     "invalid_model_tool_call_id")
                need(isinstance(call.get("name"), str) and call["name"], "invalid_model_tool_name")
                need(isinstance(call.get("arguments"), str), "invalid_model_tool_arguments")
                decode_object(call["arguments"].encode("utf-8"))
                need(call["name"] in self.stage_tools, "model_called_undeclared_tool")
                need(call["name"] != MEMORY_TOOL_NAME, "undeclared_memory_update_attempted")
                observed.append({"tool_call_id": call["tool_call_id"], "name": call["name"],
                                 "arguments": call["arguments"]})
        ids = [c["tool_call_id"] for c in observed]
        need(len(set(ids)) == len(ids), "duplicate_model_tool_call_id")
        need(not (set(ids) & self.seen_calls), "repeated_model_tool_call_id")
        self.seen_calls.update(ids)
        for call in observed:
            self.pending[call["tool_call_id"]] = call

    def check_system_framing(self, body):
        r"""Strict Letta PromptGenerator framing, not a substring containment check.

        The declared system text must be the exact prefix; everything after it must
        match what the real pinned PromptGenerator renders from the declared plan:
        the CORE_MEMORY placeholder replaced by the real
        _render_memory_blocks_standard output plus the real metadata block, with
        the real blank-line separation. Appending instructions or changing the
        agent id is rejected even with recomputed transport hashes.
        """
        sent = body.get("messages")
        need(isinstance(sent, list) and sent and sent[0].get("role") == "system",
             "system_not_first")
        system = sent[0].get("content")
        need(isinstance(system, str), "system_content_not_string")
        declared = self.plan["agent_payload"]["system"]
        need(isinstance(declared, str) and declared, "plan_system_missing")
        need(system.startswith(declared), "system_prefix_changed")
        rest = system[len(declared):]
        memory = render_memory_blocks(self.state["block"], self.state["value"])
        # Explicit checks keep each failure code diagnostic.
        need(rest.count(MEMORY_BLOCK_HEADER) == 1, "extra_memory_block_header")
        need(rest.startswith("\n\n" + MEMORY_BLOCK_HEADER), "memory_block_framing_changed")
        need(rest.startswith("\n\n" + memory), "memory_block_render_changed")
        marker = "\n\n" + memory + "\n\n"
        need(rest.startswith(marker + "<memory_metadata>\n"),
             "memory_metadata_separator_changed")
        tail = rest[len(marker):]
        match = MEMORY_METADATA.match(tail)
        need(match is not None, "memory_metadata_tail_changed")
        lines = match.group("lines").splitlines()
        # Line set and order are the pinned real format; only the values the real
        # generator varies by run may differ.
        need(len(lines) >= 4, "memory_metadata_lines_missing")
        agent = MEMORY_METADATA_AGENT.match(lines[0])
        need(agent is not None, "memory_metadata_agent_id_format_changed")
        need(agent.group("agent_id") == self.agent_id, "system_agent_id_mismatch")
        need(MEMORY_METADATA_CONVERSATION.match(lines[1]) is not None,
             "memory_metadata_conversation_id_format_changed")
        need(MEMORY_METADATA_RECOMPILED.match(lines[2]) is not None,
             "memory_metadata_recompiled_line_format_changed")
        need(MEMORY_METADATA_RECALL.match(lines[3]) is not None,
             "memory_metadata_recall_line_format_changed")
        # The pinned real generator appends at most one archival line, then at
        # most one archive-tags line; any other count or order is not that format.
        need(len(lines) <= 6, "memory_metadata_extra_line_not_in_real_format")
        extra = lines[4:]
        has_archival = bool(extra) and MEMORY_METADATA_ARCHIVAL.match(extra[0]) is not None
        if has_archival:
            need(len(extra) <= 2, "memory_metadata_extra_line_not_in_real_format")
            if len(extra) == 2:
                need(MEMORY_METADATA_TAGS.match(extra[1]) is not None,
                     "memory_metadata_extra_line_not_in_real_format")
        elif extra:
            need(len(extra) == 1 and MEMORY_METADATA_TAGS.match(extra[0]) is not None,
                 "memory_metadata_extra_line_not_in_real_format")

    def raw_assistant_turn(self, generation_response):
        """The assistant turn built from the RAW cloud provider response.

        Letta's own response is used only to bind the frame; the conversation
        continuation comes from the provider reply the model actually produced.
        The raw OpenAI assistant shape is kept verbatim.
        """
        choice = (generation_response or {}).get("choices")
        need(isinstance(choice, list) and len(choice) == 1, "cloud_response_not_single_choice")
        message = choice[0].get("message")
        need(isinstance(message, dict) and message.get("role") == "assistant",
             "cloud_response_not_assistant")
        turn = {"role": "assistant", "content": message.get("content")}
        if message.get("tool_calls"):
            turn["tool_calls"] = deepcopy(message["tool_calls"])
        return turn

    def wire_tool_call_id(self, full_id):
        """Fixed-length OpenAI-history mapping of a native tool-call id.

        `Message.to_openai_dict` truncates tool ids to TOOL_CALL_ID_MAX_LEN, and
        this mapping is used only for the OpenAI history wire comparison; native
        and approval ids are compared exactly. Two distinct native ids sharing
        the truncated prefix are a collision and are rejected.
        """
        need(isinstance(full_id, str) and full_id, "missing_native_tool_call_id")
        prefix = full_id[:TOOL_CALL_ID_MAX_LEN]
        colliding = [other for other in self.known_tool_ids
                     if other != full_id and other[:TOOL_CALL_ID_MAX_LEN] == prefix]
        need(not colliding, "tool_call_id_truncation_collision")
        return prefix

    def same_tool_calls(self, observed, expected):
        """Compare OpenAI tool calls; only the history wire truncates ids."""
        def project(calls, wire):
            output = []
            for call in calls or []:
                function = call.get("function") or {}
                call_id = call.get("id")
                if wire:
                    call_id = self.wire_tool_call_id(call_id)
                output.append({"id": call_id, "name": function.get("name"),
                               "arguments": function.get("arguments")})
            return output
        need(len(observed or []) == len(expected or []), "tool_call_count_changed")
        return project(observed, False) == project(expected, True)

    def history_tool_return_id_matches(self, observed_id, native_id):
        """Whether a history wire id is the ONE wire form of this native id.

        The sealed protocols write the fixed 29-character projection of the native id
        into the OpenAI history, and that is exactly what this default requires. A
        protocol whose service is known to write something else overrides this instead of
        loosening the shared rule.
        """
        return observed_id == self.wire_tool_call_id(native_id)

    def assistant_history_matches(self, observed, expected):
        """Exact assistant comparison plus the one pinned empty-text transform.

        A provider `content=""` assistant turn is stored without text content, and
        `Message.to_openai_dict` then emits `content: None` when tool calls are
        present. Only that empty-string/None pair is equivalent; non-empty text
        changes still fail and nothing is stripped.
        """
        actual, wanted = observed.get("content"), expected.get("content")
        if actual != wanted and not (wanted == "" and actual is None
                                     and observed.get("tool_calls")):
            return False
        return self.same_tool_calls(observed.get("tool_calls"), expected.get("tool_calls"))

    def check_agent_model_call(self, call, post):
        body = call["body"]
        profile = self.declared_profile()
        need(set(body) <= declared_cloud_fields(profile), "undeclared_cloud_request_field")
        missing = []
        check_declared_transport_fields(body, profile, missing)
        need(not missing, missing[0] if missing else "declared_transport_field_missing")
        need("seed" not in body and "chat_template_kwargs" not in body,
             "generation_seed_or_template_sent")
        need(body.get("temperature") == PINNED_LIMITS["temperature"], "cloud_temperature_changed")
        if "parallel_tool_calls" in body:
            need(body["parallel_tool_calls"] is False, "parallel_tool_calls_not_false")
        if "stream" in body:
            need(body["stream"] is False, "streaming_requested")
        if "n" in body:
            need(body["n"] == 1, "multiple_completions_requested")
        limit = body.get("max_tokens", body.get("max_completion_tokens"))
        # The AGENT role is pinned to 2048. The 4096 auxiliary ceiling must never
        # be usable to widen an agent call.
        need(limit == PINNED_LIMITS["max_output_tokens"], "agent_output_limit_not_2048")
        reasons = cloud_change_reasons(call["changes"], body, profile, limit)
        need(not reasons, reasons[0] if reasons else "unexpected_cloud_change")
        # Cloud v2 transport may carry tools=null (no tools offered); that case is
        # checked structurally instead of being passed to the framing helper.
        tools = body.get("tools")
        need(tools is None or isinstance(tools, list), "cloud_tools_not_list_or_null")
        self.check_system_framing(body)
        # Upstream tool schemas must equal the declared stage contract exactly
        # (bare function schemas, as ae_task_run and ae_probe submit them).
        if tools:
            actual_tools = tool_schemas(tools, True)
            need(actual_tools == self.declared_tools, "upstream_tools_differ_from_declared")
        else:
            need(self.auxiliary_agent_tools is False, "agent_request_has_no_tools")
        # What the driver appended for this POST must be exactly what the model saw.
        sent = body.get("messages")
        need(isinstance(sent, list) and sent, "cloud_call_without_messages")
        need(sent[0].get("role") == "system", "system_not_first")
        actual_history = sent[1:]
        # One canonical structure: the previously confirmed turns followed by this
        # POST's declared new input; every element is compared in a single pass.
        expected_history = deepcopy(self.turns) + deepcopy(post["_new_input"])
        need(len(actual_history) == len(expected_history), "history_count_changed_or_extra_message")
        for observed, item in zip(actual_history, expected_history):
            need(observed.get("role") == item["role"], "history_order_or_role_changed")
            if item["role"] == "user":
                user_text(observed.get("content"), item["content"])
            elif item["role"] == "assistant":
                need(self.assistant_history_matches(observed, item),
                     "assistant_history_changed")
            else:
                self.check_submitted_tool_return(observed, item, source="history")
        # Carry the RAW provider assistant turn forward for the next POST; the
        # Letta response summary is never trusted as the conversation content.
        self.turns = deepcopy(expected_history)
        self.turns.append(self.raw_assistant_turn(call["response"]))

    def auxiliary(self):
        agent_ids = {p["_call"]["request_id"] for p in self.posts}
        remaining = [c for c in self.chat_calls if c["request_id"] not in agent_ids]
        need(len(remaining) == len(self.native_calls), "unaccounted_auxiliary_call_count")
        for record in self.native_calls:
            need(record.get("error") is None, "native_call_incomplete")
            request, response = record.get("request"), record.get("response")
            need(isinstance(request, dict) and isinstance(response, dict), "native_call_incomplete")
            need(not request.get("tools"), "auxiliary_tools_not_supported")
            # The native capture stores the internal message dumps, not the
            # provider wire, so the expected body is the pinned
            # `format_messages` projection of them (nulls stripped: the SDK
            # drops unset optional keys when it serializes the request).
            expected = no_nulls(auxiliary_wire_messages(request.get("messages")))
            matches = [c for c in remaining
                       if no_nulls(c["body"].get("messages")) == expected
                       and no_nulls(c["response"]) == no_nulls(response.get("raw_data"))]
            need(len(matches) == 1, "auxiliary_call_missing_ambiguous_or_changed")
            match = matches[0]
            remaining.remove(match)
            for key in ("model", "temperature", "max_tokens"):
                if key in request:
                    need(request[key] == match["body"].get(key), "auxiliary_parameters_changed")
            need("seed" not in match["body"], "auxiliary_seed_sent")
            # Absent and null tools are the same auxiliary shape; both mean the
            # role was not offered tools.
            need(match["body"].get("tools") in (None, []), "unexpected_auxiliary_tools")
            limit = match["body"].get("max_tokens", match["body"].get("max_completion_tokens"))
            need(limit == PINNED_LIMITS["auxiliary_output_tokens"], "wrong_auxiliary_output_limit")
            reasons = cloud_change_reasons(match["changes"], match["body"],
                                           self.declared_profile(), limit)
            need(not reasons, reasons[0] if reasons else "unexpected_cloud_change")
            self.report["auxiliary_mappings"].append({
                "request_id": match["request_id"], "role": record.get("role"),
                "subtask_id": record.get("subtask_id")})
        need(not remaining, "unaccounted_auxiliary_call_count")
        self.report["cloud_calls"]["auxiliary"] = len(self.native_calls)
        self.report["cloud_calls"]["unaccounted"] = 0
        counts = self.report["cloud_calls"]
        need(counts["agent"] + counts["auxiliary"] + counts["unaccounted"] == len(self.chat_calls),
             "unaccounted_agent_model_call")

    # -- top level ---------------------------------------------------------
    def verify(self):
        report = self.report
        self.turns = []
        try:
            self.load()
            self.transport_gate()
            self.config_gate()
            self.provenance_gate()
            self.dataset_gate()
            self.window_gate()
            self.event_correlation_setup()
            self.http_trace_gate()
            self.agent_creation()
            self.stage_gate()
            self.native_capture()
            self.agent_posts()
            self.cloud_calls()
            self.map_to_posts()
            self.auxiliary()
            need(not self.bridge_requests and not self.bridge_responses,
                 "unmatched_bridge_http_trace")
            need(len(self.consumed_tool_events) == len(self.tool_events),
                 "executed_tool_result_not_submitted")
            need(not self.pending, "unreturned_pending_tool_call")
            for path, captured in report["sources"].items():
                if path == "proxy_journal":
                    continue
                current = Path(path).stat()
                need((current.st_size, current.st_mtime_ns)
                     == (captured["bytes"], captured["mtime_ns"]), "capture_changed_during_audit")
            report["input_audit_passed"] = True
            report["status"] = "VALID"
        except (Exception, KeyboardInterrupt) as exc:
            code = str(exc) if isinstance(exc, AuditFailure) else (
                "malformed_or_unavailable_capture_" + type(exc).__name__)
            self.note(code)
            report["input_audit_passed"] = False
            report["status"] = "INVALID"
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        return report


def audit_cloud_inputs(run_dir, proxy_journal, *, config_path=None, dataset_path=None) -> dict:
    """Convenience entry point; always returns a report dict, never raises."""
    return CloudInputAudit(run_dir, proxy_journal, config_path=config_path,
                           dataset_path=dataset_path).verify()
