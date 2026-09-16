"""Read-only input audit for the single-t4 cloud R/E pair (rewrite vs erratum).

Reuses the verified capability checker helpers (`render_memory_blocks`,
`auxiliary_wire_messages`, transport journal checks, cloud-call grouping, HTTP
trace correlation, Letta framing and OpenAI wire comparisons) and replaces only
the capability-specific gates: this run has TWO agents, an explicit history
phase followed by a task phase, a real `memory_update` client tool in both
phases, per-arm memory semantics (R publishes a verified PATCH; E never
PATCHes), and dataset history that IS sent.

The check never judges whether an update was correct: a missing update, a wrong
update, or a legal task failure are valid behaviour. It only verifies that every
input the model actually saw is the declared public input, that R's block
changes have a real update + PATCH source and appear in the next rendered
system, that E's block never changes and its errata reach the wire, and that the
two arms cannot be cross-attributed or mixed.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from ae_adapter import BLOCK_LABEL, MEMORY_TOOL, dumps
from ae_capability import EVIDENCE_REF, EVIDENCE_TEXT, TASK_ID, TASK_NUMBER, USER_ID
from ae_cloud_audit import audit_cloud_journal
from ae_cloud_input_audit import (AUXILIARY_INTERNAL_FIELDS, AUXILIARY_TOOL_CALL_FIELDS,
                                  CLOUD_FIELDS, MEMORY_METADATA, MEMORY_METADATA_AGENT,
                                  check_declared_transport_fields, cloud_change_reasons,
                                  declared_cloud_fields,
                                  MEMORY_METADATA_ARCHIVAL, MEMORY_METADATA_CONVERSATION,
                                  MEMORY_METADATA_RECALL, MEMORY_METADATA_RECOMPILED,
                                  MEMORY_METADATA_TAGS, MEMORY_BLOCK_HEADER,
                                  MESSAGE_POST_PATH, MODELS_PATH, CHAT_PATH,
                                  PINNED_LIMITS, TOOL_CALL_ID_MAX_LEN,
                                  TOOL_RETURN_WRAPPER_KEYS, CloudInputAudit,
                                  auxiliary_wire_messages, render_memory_blocks)
from ae_cloud_proxy import (COMPAT_PROFILE, MODEL, CloudConfig, normalize_request,
                            profile_of)
from ae_input_audit import (AuditFailure, digest, need, no_nulls, only, read_stable, rows,
                            tool_schemas, utc, wire, user_text)
from ae_inputs import canonical_sha256
from ae_multicall import (CONFIG_KEY as MULTICALL_CONFIG_KEY,
                          MulticallPolicyError, UPSTREAM_BASELINE, collision_groups,
                          read_manifest, resolve_policy, validate_launch_receipt,
                          validate_policy,
                          PROFILE_VERSION as MULTICALL_PROFILE_VERSION)
from ae_cloud_re_pair import (ARM_LABELS, ARMS, PAIR_MAX_REQUESTS, PACING_CANDIDATE,
                              PURPOSE, REQUEST_COUNTING, SCHEMA_VERSION as PAIR_SCHEMA,
                              SCHEMA_VERSION_MULTICALL as PAIR_SCHEMA_MULTICALL,
                              re_code_files)

SCHEMA_VERSION = "ae-cloud-re-input-audit-0.1"
#: The protocol this audit pass is reading. `None` means the sealed 0.1 protocol,
#: whose wire comparison still uses the fixed 29-character id projection. A dict
#: (the reviewed policy) means the 0.2 multi-client-tool-call protocol, whose wire
#: comparison keeps native ids whole and whose manifest is cross-checked.
MULTICALL = None
# Pinned `vita/user/base.py:30-32` stop markers and
# `vita/user/user_simulator.py:94-101 UserSimulator.is_stop`: a native user reply
# is a stop reply when its content contains any of these. The audit derives the
# expected per-arm user turns from the captured native reply with this rule; it
# never takes the driver's own event text as the source of truth.
USER_STOP_MARKERS = ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###")
REF_PATTERN = re.compile(r"^t4/(?:user|history)/\d+$")
MEMORY_TOOL_NAME = MEMORY_TOOL["name"]
HISTORY_SOURCE = "dataset_history/material"

#: Every field name a tool-call id may appear under, in an approval payload, a
#: bridge trace frame, an execution event, or a tool return.
_ID_FIELD_NAMES = ("tool_call_id", "id")


def _require_native_ids(values, label):
    """Require a present string tool-call id in every captured slot."""
    for value in values or ():
        need(isinstance(value, str) and value, label + "_missing_native_tool_call_id")


def check_id_projection(expected, observed, *, label):
    """Exact per-call identity for one batch's ids.

    Identity means **exact string equality** against the provider's own ids:

    * every wire id must be one of the provider's ids;
    * the wire must carry the same number of distinct calls as the provider sent;
    * if the provider sent more distinct ids than the wire has, the wire collapsed
      them onto a shared prefix by slicing (the truncation this work removes);
    * an id the provider never sent that is a prefix of one it did send is that
      slice, and is refused.

    Two legitimate distinct ids where one is a prefix of the other (`call_A` and
    `call_AB`) both pass: each is one of the provider's own ids. Nothing is matched
    by substring, suffix or tool name, and no "no collision" guarantee is implied.
    """
    expected = list(expected or ())
    observed = list(observed or ())
    _require_native_ids(expected, label + "_provider")
    _require_native_ids(observed, label + "_wire")
    expected_set, observed_set = set(expected), set(observed)
    need(len(expected_set) == len(expected), label + "_duplicate_provider_id")
    need(len(observed_set) == len(observed), label + "_duplicate_wire_id")
    # (1) A wire id must be one of the provider's own ids. A 29-character slice is
    # not, because the provider never emitted that string.
    need(observed_set <= expected_set, label + "_wire_id_not_a_provider_id")
    # (2) Distinct provider ids may not collapse onto fewer wire ids.
    need(len(observed_set) == len(expected_set),
         label + "_distinct_provider_ids_collapsed")
    # (3) A provider id the wire never carried, whose prefix the wire does carry,
    # is the slice of that call.
    for value in expected_set - observed_set:
        need(not any(other.startswith(value) for other in observed_set),
             label + "_wire_id_truncated_from_provider_id")
    need(observed_set == expected_set, label + "_wire_ids_differ_from_provider_ids")


PINNED = {
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
    "max_requests": PAIR_MAX_REQUESTS,
}


def re_source_files(root=None):
    base = Path(root) if root else Path(__file__).resolve().parent
    names = ("ae_cloud_re_input_audit.py", "scripts/ae_01_cloud_re_input_audit.py",
             "ae_cloud_re_pair.py", "ae_input_audit.py", "ae_cloud_input_audit.py",
             "ae_cloud_audit.py", "ae_cloud_proxy.py", "ae_model_proxy.py", "ae_http.py")
    return {name: hashlib.sha256((base / name).read_bytes()).hexdigest()
            for name in names if (base / name).is_file()}


class RePairInputAudit(CloudInputAudit):
    """One audit pass over one immutable R/E pair capture set."""

    def __init__(self, run_dir, proxy_journal, *, config_path=None, dataset_path=None):
        super().__init__(run_dir, proxy_journal, config_path=config_path,
                         dataset_path=dataset_path)
        # The declared receive policy for THIS pass; set in `config_gate` from the
        # run's own plan, never from a caller argument or a CLI flag.
        self.multicall = MULTICALL
        self.manifest = None
        self.provider_ids = set()
        self.collision_groups = []
        self.report = {
            "schema_version": SCHEMA_VERSION, "status": "INVALID",
            "transport_capture_checked": False, "input_audit_passed": False,
            "task_success": None, "scientific_result": None,
            "sources": {}, "code_sha256": re_source_files(),
            "frames": [], "auxiliary_mappings": [], "tool_mappings": [],
            "memory_mappings": [], "erratum_mappings": [], "patches": [],
            # One evidence record per validated approval batch, per arm.
            "multicall_batches": [],
            "cloud_calls": {"models": 0, "agent": 0, "auxiliary": 0, "unaccounted": 0},
            "scope": {"user_id": USER_ID, "task_number": TASK_NUMBER, "subtask_id": TASK_ID,
                      "t5_executed": None, "dataset_history_sent": None,
                      "memory_update_tool": None, "arms": list(ARMS),
                      "multicall_profile": None, "multicall_manifest": None},
            "limitations": [
                "read-only over the captured files; a stable snapshot does not certify future requests",
                "no token / KV-prefix / cache agreement is checked or implied",
                "auxiliary calls are attributed to an arm by the arm's stage window and native-call "
                "snapshot, then matched by content; identical text across arms is rejected as ambiguous",
                "an update's truth is never judged here: missing, wrong or unused updates are valid behaviour",
                "the proxy journal's own read/client fields are not covered by the transport check",
                "if a real capture renders the memory block differently than the pinned Letta framing "
                "helper, this file fails closed rather than passing",
            ],
            "invalid_reasons": [],
        }

    # -- RE config / provenance / window -----------------------------------
    def config_gate(self):
        opened = only(self.proxy, "cloud_open")
        need(opened.get("profile") == COMPAT_PROFILE, "wrong_cloud_profile")
        config = CloudConfig(**opened["config"])
        config.validate()
        need(config.profile == self.transport["profile"], "profile_config_mismatch")
        need(config.max_requests <= PAIR_MAX_REQUESTS, "safety_cap_exceeded")
        self.cloud_config = config
        plan_config = self.plan.get("config")
        need(isinstance(plan_config, dict), "plan_config_missing")
        need(plan_config == self.result.get("config"), "plan_and_result_config_differ")
        need(plan_config.get("schema_version") in (PAIR_SCHEMA, PAIR_SCHEMA_MULTICALL),
             "wrong_task_profile")
        need(plan_config.get("purpose") == PURPOSE, "wrong_purpose")
        need(plan_config.get("expected_model") == MODEL, "wrong_expected_model")
        need(plan_config.get("transport_profile") == COMPAT_PROFILE, "wrong_transport_profile")
        need(plan_config.get("context_window_source") == "local_agent_budget_not_server_measurement",
             "context_budget_provenance_missing")
        need(plan_config.get("arms") == list(ARMS), "plan_arm_set_or_order_changed")
        pacing = plan_config.get("pacing")
        pacing_ok = pacing == PACING_CANDIDATE
        for key, value in PINNED.items():
            if key == "timeout_seconds" and pacing_ok:
                need(plan_config.get(key) == pacing["driver_io_timeout_seconds"],
                     "pacing_driver_timeout_changed")
                continue
            need(plan_config.get(key) == value, "pinned_limit_changed_" + key)
        self._set_protocol(plan_config)
        need(config.model == MODEL and config.upstream_origin == "https://api.siliconflow.cn",
             "wrong_cloud_origin_or_model")
        # The whole-pair budget is the SAME proxy counter the second arm cannot reset.
        need(config.max_requests == plan_config["max_requests"], "pair_request_budget_differ")
        need(plan_config["max_requests"] == PAIR_MAX_REQUESTS, "pair_request_budget_changed")
        need(config.max_output_tokens == plan_config["auxiliary_output_tokens"],
             "transport_output_ceiling_not_auxiliary_limit")
        need(config.max_request_bytes == plan_config["max_request_bytes"],
             "transport_request_bytes_differ")
        need(config.max_response_bytes == plan_config["max_response_bytes"],
             "transport_response_bytes_differ")
        if pacing_ok:
            need(config.io_timeout_seconds == pacing["proxy_upstream_io_timeout_seconds"],
                 "pacing_proxy_timeout_changed")
            need(config.pace_seconds == pacing["min_interval_seconds"], "pacing_interval_changed")
        else:
            need(config.io_timeout_seconds == plan_config["timeout_seconds"],
                 "transport_io_timeout_differ")
            need(not config.pace_seconds, "unexpected_pacing_interval")
        if self.config_path is not None:
            declared = json.loads(read_stable(self.config_path, self.report).decode("utf-8"))
            need(declared == plan_config, "declared_config_not_the_run_config")
        self.report["transport_config"] = {
            "profile": config.profile, "max_output_tokens": config.max_output_tokens,
            "max_request_bytes": config.max_request_bytes,
            "max_response_bytes": config.max_response_bytes,
            "max_requests": config.max_requests, "io_timeout_seconds": config.io_timeout_seconds,
            "pair_request_counting": REQUEST_COUNTING}
        self.report["scope"]["pair_request_budget"] = {
            "max_requests": config.max_requests, "counting": REQUEST_COUNTING}

    # -- declared receive protocol ------------------------------------------
    def _set_protocol(self, plan_config):
        """Resolve the run's declared receive protocol and verify its manifest.

        Absent `multicall_profile` is the sealed 0.1 protocol: the old projection
        stays, nothing is loosened. Present means the versioned compatibility
        protocol, which must be exact, must be recorded identically in the run
        result, and must be backed by a reviewed manifest that both sides of the
        run recorded with the same digest and the same patched-Letta file hashes.
        Unknown profiles, a missing manifest, and any manifest mismatch are
        refusals, never warnings.
        """
        declared = plan_config.get(MULTICALL_CONFIG_KEY)
        if declared is None:
            need(self.result.get("config", {}).get(MULTICALL_CONFIG_KEY) is None,
                 "multicall_profile_only_in_plan")
            need(plan_config.get("schema_version") == PAIR_SCHEMA,
                 "multicall_profile_missing_for_its_schema")
            self.multicall = None
            self.report["scope"]["multicall_profile"] = None
            self.report["scope"]["multicall_manifest"] = None
            return
        need(plan_config.get("schema_version") == PAIR_SCHEMA_MULTICALL,
             "multicall_profile_in_a_schema_that_cannot_declare_it")
        try:
            self.multicall = resolve_policy({MULTICALL_CONFIG_KEY: declared},
                                            allow_absent=False)
        except MulticallPolicyError as exc:
            self.fail("invalid_multicall_profile: " + str(exc))
        need(self.result.get("config", {}).get(MULTICALL_CONFIG_KEY) == declared,
             "plan_and_result_multicall_profile_differ")
        self._verify_multicall_manifest()
        self.report["scope"]["multicall_profile"] = self.multicall.as_dict()

    def _verify_multicall_manifest(self):
        """Recompute the declared compatibility manifest, strictly.

        One contract is shared by the production entry point that writes the run
        record, the runtime gate that enables the profile, this audit and the
        tests. Everything here is recomputed from bytes on disk:

        * schema, profile version and upstream baseline;
        * the record itself: `{path, sha256}` only, identical in plan and result;
        * the compatibility module: `{path, sha256}` only, digest recomputed;
        * `letta_checkout` present and every `patched_files` entry
          `{baseline_sha256, patched_sha256}` non-empty with its real digest.

        A manifest may not claim a live server on the strength of a hand-filled
        flag: when it declares that a running service loaded it, the run must
        carry a runtime receipt whose live file digests the audit recomputes.
        Offline verification is expressed separately and is never read as
        evidence of live loading.
        """
        report = self.report
        recorded = {}
        for label in ("plan", "result"):
            provenance = (self.plan if label == "plan" else self.result).get("provenance")
            need(isinstance(provenance, dict), label + "_provenance_missing")
            entry = provenance.get("multicall_manifest")
            need(isinstance(entry, dict), label + "_multicall_manifest_missing")
            recorded[label] = entry
        need(recorded["plan"] == recorded["result"], "multicall_manifest_record_differs")
        entry = recorded["plan"]
        need(set(entry) == {"path", "sha256"}, "multicall_manifest_record_fields_changed")
        path, recorded_sha = entry["path"], entry["sha256"]
        need(isinstance(path, str) and path, "multicall_manifest_path_missing")
        need(isinstance(recorded_sha, str) and len(recorded_sha) == 64,
             "multicall_manifest_sha_missing")
        file = Path(path) if Path(path).is_absolute() else self.root / path
        need(file.is_file(), "multicall_manifest_absent")
        raw = read_stable(file, report)
        need(digest(raw) == recorded_sha, "multicall_manifest_sha256_mismatch")
        manifest = json.loads(raw.decode("utf-8"))
        self.manifest = manifest
        # The audit and the runtime gate must accept exactly the same manifest.
        try:
            checked = read_manifest(file)
        except MulticallPolicyError as exc:
            self.fail("multicall_manifest_rejected_by_the_runtime_gate: " + str(exc))
        need(checked["schema"] == manifest.get("schema"), "multicall_manifest_schema_changed")
        need(manifest.get("profile_version") == MULTICALL_PROFILE_VERSION,
             "multicall_manifest_profile_version_changed")
        need(manifest.get("upstream_baseline") == UPSTREAM_BASELINE,
             "multicall_manifest_upstream_baseline_changed")
        verification = manifest.get("verification")
        need(isinstance(verification, dict), "multicall_manifest_verification_missing")
        need(set(verification) <= {"offline_patch_verified", "applied_to_live_server", "note"},
             "multicall_manifest_verification_fields_changed")
        need(verification.get("offline_patch_verified") is True,
             "multicall_manifest_not_offline_verified")
        module = manifest.get("compat_module")
        module_path = self.root / module["path"]
        need(module_path.is_file(), "multicall_compat_module_absent")
        need(digest(module_path.read_bytes()) == module["sha256"],
             "multicall_compat_module_sha256_mismatch")
        checkout = Path(manifest["letta_checkout"])
        need(checkout.is_dir(), "multicall_letta_checkout_absent")
        files = manifest["patched_files"]
        for name, item in files.items():
            target = checkout / name
            need(target.is_file(), "multicall_patched_file_absent")
            need(digest(target.read_bytes()) == item["patched_sha256"],
                 "multicall_patched_file_sha256_mismatch")
        report["scope"]["multicall_manifest"] = {
            "path": str(file), "sha256": recorded_sha,
            "schema": manifest.get("schema"),
            "profile_version": manifest.get("profile_version"),
            "letta_checkout": str(checkout),
            "offline_patch_verified": verification.get("offline_patch_verified"),
            "applied_to_live_server": verification.get("applied_to_live_server"),
            "patched_files": {name: item["patched_sha256"] for name, item in files.items()}}
        # Always checked for a 0.2 run: the optional live boolean is not a gate.
        self._check_receipt_when_executed(manifest, recorded_sha)

    def _check_receipt_when_executed(self, manifest, manifest_sha):
        """A 0.2 run that actually EXECUTED must carry a valid service receipt.

        The r4 review showed the receipt check was gated on the manifest's optional
        `applied_to_live_server` boolean, so a default manifest let an invalid
        receipt - or no receipt at all - audit as VALID. The requirement is now
        unconditional for an executed run: an offline PLAN/PREFLIGHT record is
        explicitly marked unverified and is not an executed run, while a record
        that claims execution must prove the service's loaded sources.

        The content checks are the SAME shared function the production RUN entry
        point calls, so both enforce one contract.
        """
        report = self.report
        executed = (self.result.get("execution_complete") is True
                    or self.result.get("status") == "RE_PAIR_COMPLETED_AUDIT_PENDING")
        record = None
        for label in ("plan", "result"):
            provenance = (self.plan if label == "plan" else self.result).get("provenance")
            entry = (provenance or {}).get("multicall_live_loading")
            if entry is not None:
                need(record is None or record == entry,
                     "multicall_live_loading_record_differs")
                record = entry
        if record is None:
            if executed:
                self.fail("multicall_live_loading_missing")
            # Offline PLAN/PREFLIGHT: record that the service's loaded sources were
            # not checked rather than reading the absence as a pass.
            report["scope"]["multicall_live_loading"] = {
                "checked": False,
                "reason": "offline record: no executed run, so no service load receipt"}
            return
        need(set(record) == {"path", "sha256"},
             "multicall_live_loading_record_fields_changed")
        path = Path(str(record["path"]))
        need(path.is_file(), "multicall_live_loading_receipt_absent")
        raw = read_stable(path, report)
        need(digest(raw) == record["sha256"], "multicall_live_loading_receipt_sha_mismatch")
        try:
            receipt = json.loads(raw.decode("utf-8"))
        except ValueError:
            self.fail("multicall_live_loading_receipt_not_json")
        try:
            validate_launch_receipt(receipt, manifest, manifest_sha)
        except MulticallPolicyError as exc:
            self.fail("multicall_live_loading_invalid: " + str(exc))
        report["scope"]["multicall_live_loading"] = {
            "checked": True, "path": str(path), "sha256": record["sha256"],
            "instance_id": receipt["instance_id"], "module_path": receipt["module_path"],
            "checkout": receipt["checkout"]}

    def _wire_ids(self, *values):
        """Return the wire-tool-call ids under the declared protocol."""
        if self.multicall is None:
            return values
        _require_native_ids(values, "wire")
        return values

    def initialise_id_namespace(self, provider_ids):
        """Freeze the authoritative provider id set once, before any comparison.

        Under the compatibility protocol the only authoritative ids are the ones
        the provider actually returned (plus the ids of calls the bridge really
        executed). Nothing is derived from substrings, suffixes or tool names.

        The 29-character prefix groups are recorded as **evidence** of what the
        old path would have collapsed, not used for matching.
        """
        if self.multicall is None:
            self.provider_ids, self.collision_groups = set(), []
            return
        ids = sorted({str(value) for value in provider_ids if value})
        _require_native_ids(ids, "provider")
        self.provider_ids = set(ids)
        groups = collision_groups(ids)
        self.collision_groups = [list(group) for group in groups]

    def wire_tool_call_id(self, full_id):
        """Protocol-aware replacement for the parent's fixed 29-character projection.

        The compatibility path compares the native id exactly; there is no
        remapping step, so this is the identity for any id the run really used.
        """
        if self.multicall is None:
            return super().wire_tool_call_id(full_id)
        _require_native_ids([full_id], "wire")
        return full_id

    def same_tool_calls(self, observed, expected):
        """Compare OpenAI tool calls by exact id, name and argument text.

        No alias table and no substring matching: a call matches only if its id
        is exactly the other side's id. `call_A` and `call_AB` are two distinct
        legitimate ids and both pass.
        """
        if self.multicall is None:
            return super().same_tool_calls(observed, expected)

        def project(calls):
            output = []
            for call in calls or ():
                function = call.get("function") or {}
                output.append({"id": call.get("id"), "name": function.get("name"),
                               "arguments": function.get("arguments")})
            return output
        need(len(observed or ()) == len(expected or ()), "tool_call_count_changed")
        return project(observed) == project(expected)


    def provenance_gate(self):
        """Same recomputation as the verified checker, for the RE code file set."""
        code_files = re_code_files(self.root)
        report = self.report
        for label in ("plan", "result"):
            provenance = (self.plan if label == "plan" else self.result).get("provenance")
            need(isinstance(provenance, dict), label + "_provenance_missing")
            code = provenance.get("code_sha256")
            need(isinstance(code, dict), label + "_provenance_code_sha256_missing")
            need(set(code) == set(code_files), label + "_provenance_code_file_set_changed")
            for name, expected in code_files.items():
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
                need(provenance.get("config_file_sha256") == digest(self.config_path.read_bytes()),
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
        need(self.plan["provenance"]["code_sha256"] == self.result["provenance"]["code_sha256"],
             "plan_and_result_code_sha256_differ")
        need(self.plan["provenance"]["config_canonical_sha256"]
             == self.result["provenance"]["config_canonical_sha256"],
             "plan_and_result_config_canonical_sha256_differ")

    def window_gate(self):
        need(self.result.get("execution_complete") is True, "execution_not_complete")
        need(not self.result.get("invalid_reasons"), "result_reports_invalid")
        need(self.result.get("task_success") is None and self.result.get("scientific_result") is None,
             "result_claims_a_scientific_outcome")
        need(self.result.get("status") == "RE_PAIR_COMPLETED_AUDIT_PENDING",
             "unexpected_result_status")
        self.start = utc(self.result.get("started_at_utc"))
        self.end = utc(self.result.get("finished_at_utc"))
        need(self.result.get("pair_order") == list(ARMS), "pair_order_changed")
        need(self.result.get("stopped_after_arm") is None, "pair_stopped_before_both_arms")
        scope = self.result.get("scope") or {}
        need(scope.get("task_number") == TASK_NUMBER and scope.get("subtask_id") == TASK_ID,
             "wrong_result_scope")
        need(scope.get("t5_executed") is False, "t5_was_executed")
        need(scope.get("dataset_history_sent") is True, "history_sent_flag_changed")
        need(scope.get("memory_update_tool") is True, "memory_tool_flag_changed")
        need(scope.get("arm_order") == list(ARMS), "result_arm_order_changed")
        self.report["scope"]["t5_executed"] = False
        self.report["scope"]["dataset_history_sent"] = True
        self.report["scope"]["memory_update_tool"] = True

    # -- two-arm event correlation -----------------------------------------
    def event_correlation_setup(self):
        bridge = [e for e in self.events if e.get("kind") == "bridge_event"]
        self.bridge_events = bridge
        arms = {e.get("arm") for e in bridge}
        need(arms == set(ARMS), "bridge_events_do_not_cover_exactly_the_two_arms")
        need(not [e for e in self.events if e.get("kind") == "pair_stopped"],
             "pair_stopped_event_present")
        for event in bridge:
            need(event.get("event", {}).get("kind") != "bridge_blocked", "bridge_blocked_event")
        self.bridge_requests = [e for e in bridge if e.get("event", {}).get("kind") == "request"]
        self.bridge_responses = [e for e in bridge if e.get("event", {}).get("kind") == "response"]
        self.tool_events = [e for e in bridge
                            if e.get("event", {}).get("kind") == "client_tool_result"]
        self.all_http_requests = [r for r in self.http if r.get("kind") == "request"]
        self.all_http_responses = [r for r in self.http if r.get("kind") == "response"]
        self.seen_calls = {}
        self.seen_refs = {arm: set() for arm in ARMS}
        self.users_seen = {arm: 0 for arm in ARMS}
        self.user_replies = {arm: [] for arm in ARMS}
        self.expected_runtime_users = {arm: [] for arm in ARMS}
        self.runtime_user_index = {arm: 0 for arm in ARMS}
        self.pending = {}
        self.consumed_tool_events = set()
        self.pending_patch = []

    # -- creation -----------------------------------------------------------
    def creation_gate(self):
        report = self.report
        created = [e for e in self.events if e.get("kind") == "agent_created"]
        need(len(created) == 2, "missing_or_extra_agent_created_event")
        need({e.get("arm") for e in created} == set(ARMS), "agent_created_arm_set_changed")
        creates = [r for r in self.http if r.get("kind") == "request" and r.get("method") == "POST"
                   and r.get("path") == "/v1/agents/"]
        need(len(creates) == 2, "missing_or_duplicate_agent_create_request")
        self.arm_state = {}
        for event in created:
            arm = event["arm"]
            payload = event.get("payload")
            need(isinstance(payload, dict), "agent_creation_payload_missing")
            declared = (self.plan.get("arms") or {}).get(arm, {}).get("agent_payload")
            need(isinstance(declared, dict), "plan_agent_payload_missing")
            declared_fields = {k: v for k, v in declared.items() if k != "name"}
            observed_fields = {k: v for k, v in payload.items() if k != "name"}
            need(observed_fields == declared_fields, "creation_payload_differs_from_plan_" + arm)
            need(re.fullmatch(r"ae-re-pair-cloud-" + arm + r"-[0-9a-f]{12}",
                              payload.get("name") or "") is not None, "unexpected_agent_name")
            aid = event.get("agent_id")
            need(isinstance(aid, str) and aid, "agent_created_event_id_missing")
            need(self.result["arms"][arm].get("agent_id") == aid, "result_agent_id_mismatch")
            need(event.get("block_id") == self.result["arms"][arm].get("block_id"),
                 "result_block_id_mismatch")
            matched = [r for r in creates if r.get("body") == payload]
            need(len(matched) == 1, "agent_create_http_body_differs_from_event_payload")
            body = matched[0]["body"]
            need(body.get("initial_message_sequence") == [], "initial_message_sequence_not_empty")
            need(body.get("include_base_tools") is False and body.get("tool_ids") == []
                 and body.get("include_default_source") is False,
                 "unexpected_creation_tools_or_sources")
            need(body.get("message_buffer_autoclear") is False
                 and body.get("enable_sleeptime") is False, "unexpected_memory_channel_setting")
            blocks = body.get("memory_blocks")
            need(isinstance(blocks, list) and len(blocks) == 1, "unexpected_created_block_count")
            block = blocks[0]
            need(block.get("label") == BLOCK_LABEL, "wrong_memory_block_label")
            need(block.get("limit") == PINNED["block_char_limit"], "wrong_memory_block_limit")
            need(body.get("system") == declared["system"], "creation_system_changed_" + arm)
            blob = json.dumps(body, ensure_ascii=False)
            # The t3-cutoff block still carries the old fact and must NOT carry the
            # replacement; the preset history evidence must not be pre-applied.
            need("奶茶偏好5分糖" in block.get("value", ""), "old_fact_missing_from_initial_block")
            need("奶茶偏好7分糖" not in blob, "replacement_leaked_into_creation")
            need(EVIDENCE_TEXT not in blob, "history_evidence_leaked_into_creation")
            initial = (self.plan.get("arms") or {}).get(arm, {}).get("initial_block")
            need(block.get("value") == initial, "initial_block_differs_from_plan")
            need(self.plan["inputs"]["initial_block"] == initial, "arm_initial_blocks_differ")
            self.arm_state[arm] = {"system": body["system"], "block": block, "value": block["value"],
                                   "agent_id": aid, "block_id": event.get("block_id"),
                                   "phase": "setup", "posts": 0}
            report["frames"].append({"kind": "agent_creation", "arm": arm, "agent_id": aid,
                                     "block_id": event.get("block_id"),
                                     "block_sha256": digest(block["value"].encode()),
                                     "passed": True})
        need(self.arm_state["rewrite"]["block"]["value"]
             == self.arm_state["erratum"]["block"]["value"], "arm_initial_blocks_differ")
        need(self.arm_state["rewrite"].get("block_id")
             != self.arm_state["erratum"].get("block_id"), "arms_share_a_block")

    # -- stages / lifecycle -------------------------------------------------
    def stage_gate(self):
        starts = [e for e in self.events if e.get("kind") == "stage_start"]
        need(len(starts) == 2, "missing_or_duplicate_stage_start")
        need({e.get("arm") for e in starts} == set(ARMS), "stage_start_arm_set_changed")
        self.stage_tools, self.stage_native = {}, {}
        for event in starts:
            arm = event["arm"]
            tools = event.get("tools")
            need(isinstance(tools, list) and tools, "stage_start_tools_missing")
            schemas = tool_schemas(tools)
            for name in schemas:
                need(name != MEMORY_TOOL_NAME and "preference_memory" not in name,
                     "second_memory_backend_in_stage_tools")
            self.stage_native[arm] = schemas
            self.stage_tools[arm] = dict(schemas, **{MEMORY_TOOL_NAME: deepcopy(MEMORY_TOOL)})
            need(event.get("task") == TASK_ID, "unexpected_task_in_stage_start")
            need((event.get("public_task") or {}).get("instruction")
                 == self.plan["task_preview"]["instruction"], "stage_start_task_changed")
        need(self.stage_native["rewrite"] == self.stage_native["erratum"],
             "arms_offer_different_native_tools")

    def native_capture(self):
        """Per-arm prefix-extension of the retained native auxiliary calls."""
        self.native_calls = {}
        for arm in ARMS:
            lifecycle = [e for e in self.events
                         if e.get("arm") == arm
                         and e.get("kind") in ("stage_start", "arm_complete")]
            need(len(lifecycle) == 2, "missing_arm_lifecycle_event")
            need([e["kind"] for e in lifecycle] == ["stage_start", "arm_complete"],
                 "arm_lifecycle_order_changed")
            for event in lifecycle:
                need(event.get("task") == TASK_ID, "unexpected_task_in_arm_lifecycle")
            previous = []
            for event in lifecycle:
                snapshot = event.get("snapshot")
                need(isinstance(snapshot, dict), "missing_snapshot_in_" + event["kind"])
                native = snapshot.get("native_calls")
                need(isinstance(native, list), "missing_auxiliary_capture_in_" + event["kind"])
                need(len(native) >= len(previous), "native_calls_snapshot_rollback")
                need(native[:len(previous)] == previous, "native_call_history_changed")
                previous = native
            self.native_calls[arm] = previous
            self._derive_user_replies(arm, previous)

    def _derive_user_replies(self, arm, native_calls):
        """Per-arm native user replies, in order, from the captured native record.

        The reply text comes from the fixed Vita user message the native wrapper
        captured (`response.content`, cross-checked against the raw provider
        choice), and the stop decision comes from the pinned `is_stop` markers.
        The driver's own `simulated_user` event may only repeat that derived
        value, never define it.
        """
        derived = []
        for record in native_calls:
            if record.get("role") != "user_simulator":
                continue
            response = record.get("response")
            need(isinstance(response, dict), "native_user_reply_missing")
            raw = response.get("raw_data")
            choices = (raw or {}).get("choices") if isinstance(raw, dict) else None
            need(isinstance(choices, list) and len(choices) == 1,
                 "native_user_reply_shape_changed")
            message = (choices[0] or {}).get("message") or {}
            raw_text = message.get("content")
            need(isinstance(raw_text, str) and raw_text, "native_user_reply_missing")
            text = response.get("content", raw_text)
            need(isinstance(text, str) and text, "native_user_reply_missing")
            need(text == raw_text, "native_user_reply_content_changed")
            # Pinned UserSimulator.is_stop never treats a tool call as a stop; the
            # wrapper already rejects tool calls for the native user role.
            stop = any(marker in text for marker in USER_STOP_MARKERS)
            derived.append({"text": text, "stop": stop})
        events = [e for e in self.events
                  if e.get("kind") == "simulated_user" and e.get("arm") == arm]
        need(len(events) == len(derived), "simulated_user_event_count_changed")
        for event, expected in zip(events, derived):
            reply = event.get("reply") or {}
            need(reply.get("content") == expected["text"],
                 "simulated_user_content_not_from_native_reply")
            need(bool(reply.get("stop")) == expected["stop"],
                 "simulated_user_stop_not_from_native_reply")
        self.user_replies[arm] = derived
        self.expected_runtime_users[arm] = [item["text"] for item in derived
                                            if not item["stop"]]

    # -- Letta POSTs --------------------------------------------------------
    def agent_posts(self):
        posts = [r for r in self.http if r.get("kind") == "request" and r.get("method") == "POST"
                 and MESSAGE_POST_PATH.fullmatch(r.get("path") or "")]
        need(posts, "no_agent_message_posts")
        need(len(posts) <= 2 * PINNED["max_stage_posts"], "stage_post_limit_exceeded")
        self.posts = sorted(posts, key=lambda item: utc(item["timestamp"]))
        # The block each POST actually saw: walk the capture in order and apply
        # only real PATCH requests, so a post-PATCH request must render the new
        # value and a pre-PATCH request must render the old one.
        current = {arm: self.plan["arms"][arm]["initial_block"] for arm in ARMS}
        self.block_at_post = {}
        for record in sorted(self.http, key=lambda row: row.get("sequence", 0)):
            if record.get("kind") == "request" and record.get("method") == "PATCH":
                arm = self._agent_of_path(record.get("path"))
                need(arm in ARMS, "patch_for_unknown_agent")
                need(set(record.get("body") or {}) == {"value"}, "unexpected_patch_fields")
                current[arm] = record["body"]["value"]
            elif (record.get("kind") == "request" and record.get("method") == "POST"
                    and MESSAGE_POST_PATH.fullmatch(record.get("path") or "")):
                arm = self._agent_of_path(record.get("path"))
                need(arm in ARMS, "post_for_unknown_agent")
                self.block_at_post[record.get("request_id")] = current[arm]
        need(len(self.block_at_post) == len(self.posts), "post_block_state_incomplete")
        self.report["counts"] = {"agent_posts": len(self.posts), "user_messages": 0,
                                 "tool_returns": 0, "history_messages": 0}

    def _bridge_request_for(self, post):
        """Attribute one POST to an arm and phase through its own bridge frame.

        The arm/phase never come from message text; they come from the driver's
        own trace frame for this exact HTTP request (method, path, body, clock).
        """
        candidates = [e for e in self.bridge_requests
                      if e["event"].get("method") == post.get("method")
                      and e["event"].get("path") == post.get("path")
                      and e["event"].get("body") == post.get("body")
                      and abs((utc(e["timestamp"]) - utc(post["timestamp"])).total_seconds()) <= 0.25]
        need(len(candidates) == 1, "http_request_missing_or_ambiguous_bridge_trace")
        self.bridge_requests.remove(candidates[0])
        return candidates[0]

    def _consume_bridge_response(self, post, generation_response):
        responses = [e for e in self.bridge_responses
                     if e["event"].get("body") == generation_response]
        need(len(responses) >= 1, "http_response_missing_bridge_trace")
        self.bridge_responses.remove(responses[0])

    def check_user_message(self, message, arm, phase):
        need(message.get("type", "message") == "message", "unexpected_user_message_type")
        content = message.get("content")
        need(isinstance(content, str), "user_input_not_string")
        material = json.loads(content)
        source = material.get("source")
        declared_message = self.plan["inputs"]["history_material"]
        if source == HISTORY_SOURCE:
            # The history batch is a material wrapper, not a referenced turn; its
            # own records carry the t4/history/N refs.
            need(phase == "history", "history_material_in_non_history_phase")
            need(self.arm_state[arm]["posts"] == 0, "history_material_not_first")
            need(set(material) == {"source", "handling", "task_number", "records"},
                 "history_material_fields_changed")
            need(material.get("task_number") == TASK_NUMBER, "wrong_task_number_in_history")
            need(material.get("records") == self.plan["inputs"]["history_records"],
                 "history_records_changed")
            need(content == declared_message["content"], "history_material_changed")
            refs = [entry.get("ref") for entry in material["records"]
                    if isinstance(entry, dict)]
            need(refs == [f"t4/history/{i}" for i in range(len(refs))],
                 "history_records_order_changed")
            self.report["frames"].append({"kind": "history_material", "arm": arm,
                                          "records": len(material["records"]), "passed": True})
            self.report["counts"]["history_messages"] += 1
            return
        ref = material.get("ref")
        need(isinstance(ref, str) and REF_PATTERN.fullmatch(ref), "undeclared_user_ref")
        need(ref not in self.seen_refs[arm], "duplicate_user_ref")
        if source == "current_task":
            need(phase == "task", "current_task_in_non_task_phase")
            need(set(material) == {"source", "subtask_id", "domain", "current_time",
                                   "domain_policy", "instruction", "ref"},
                 "current_task_fields_changed")
            need(material["subtask_id"] == TASK_ID, "wrong_subtask_in_current_task")
            need(material["instruction"] == self.plan["task_preview"]["instruction"],
                 "current_task_instruction_changed")
            need(material["domain"] == self.plan["task_preview"]["domain"], "wrong_domain")
            need(self.users_seen[arm] == 0, "duplicate_current_task_message")
            self.users_seen[arm] += 1
            self.report["frames"].append({"kind": "current_task", "arm": arm, "ref": ref,
                                          "passed": True})
        elif source == "runtime_user":
            need(phase == "task", "runtime_user_in_non_task_phase")
            need(set(material) == {"source", "ref", "content"}, "runtime_user_fields_changed")
            need(isinstance(material.get("content"), str) and material["content"].strip(),
                 "runtime_user_content_missing")
            need(self.users_seen[arm] == 1, "runtime_user_before_current_task")
            expected = self.expected_runtime_users[arm]
            index = self.runtime_user_index[arm]
            need(index < len(expected), "unexpected_runtime_user_message")
            need(ref == f"t4/user/{index + 1}", "runtime_user_ref_out_of_order")
            need(material["content"] == expected[index],
                 "runtime_user_content_not_from_native_reply")
            self.runtime_user_index[arm] = index + 1
            self.report["frames"].append({"kind": "runtime_user", "arm": arm, "ref": ref,
                                          "passed": True})
        else:
            self.fail("unrecognized_user_message_source")
        self.seen_refs[arm].add(ref)
        self.report["counts"]["user_messages"] += 1

    def check_submitted_tool_return(self, submitted, item, *, source):
        """Protocol-aware tool-return comparison (history wire only).

        The submission branch is byte-for-byte the parent's: a Letta
        `tool_returns` entry carries the native id and is compared exactly in
        both protocols. Only the OpenAI history wire differs: the sealed 0.1
        protocol compares the fixed 29-character projection, while the
        compatibility protocol keeps the native id whole. Everything after the
        id check - wrapper shape, status, message text - is unchanged and is
        applied by the parent.
        """
        if self.multicall is None or source != "history":
            return super().check_submitted_tool_return(submitted, item, source=source)
        returned = item["returned"]
        need(set(submitted) <= {"role", "content", "tool_call_id", "name"},
             "unexpected_history_tool_fields")
        need(submitted.get("role") == "tool", "history_tool_role_changed")
        need(submitted.get("tool_call_id") == self.wire_tool_call_id(
            returned.get("tool_call_id")), "tool_id_changed")
        # Reuse the parent for the wrapper/status/message checks by presenting the
        # same item unchanged: the id branch is the only one replaced above.
        return super().check_submitted_tool_return(
            dict(submitted, tool_call_id=self.wire_tool_call_id(
                returned.get("tool_call_id"))), item, source=source)

    def check_model_output(self, post, response):
        """Per-call checks plus the declared protocol's batch requirements.

        The per-call rules are unchanged from the sealed checker: only declared
        tools, a present native id, parsed arguments, no id repeated inside one
        capture. This run allows the memory tool in both phases and carries the
        executor's exact call shape so the audit's equality check against the
        execution event is meaningful.
        """
        messages = response.get("body", {}).get("messages")
        need(isinstance(messages, list), "letta_response_messages_missing")
        approvals = [m for m in messages if m.get("message_type") == "approval_request_message"]
        assistants = [m for m in messages if m.get("message_type") == "assistant_message"]
        need(bool(approvals) or bool(assistants), "letta_response_has_no_model_output")
        declared = post["_declared_tools"]
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
                need(isinstance(call.get("name"), str) and call["name"],
                     "invalid_model_tool_name")
                need(isinstance(call.get("arguments"), str), "invalid_model_tool_arguments")
                json.loads(call["arguments"])
                need(call["name"] in declared, "model_called_undeclared_tool")
                # The executor's call shape is the one the declared protocol
                # produces: the sealed path passes Letta's approval call through
                # unchanged, the compatibility path rebuilds it from the batch.
                entry = {"tool_call_id": call["tool_call_id"], "name": call["name"],
                         "arguments": call["arguments"]}
                if self.multicall is not None:
                    entry = {"type": "tool", **entry}
                observed.append(entry)
        ids = [c["tool_call_id"] for c in observed]
        observed_ids = list(ids)
        need(len(set(ids)) == len(ids), "duplicate_model_tool_call_id")
        seen = self.seen_calls.setdefault(post["_arm"], set())
        need(not (set(ids) & seen), "repeated_model_tool_call_id")
        seen.update(ids)
        for call in observed:
            self.pending[call["tool_call_id"]] = call

        if self.multicall is None:
            return
        # The compatibility policy declares that the whole batch is validated
        # before any side effect and that the results come back in the same
        # order, so the batch recorded here is the batch the provider returned.
        batches = []
        for approval in approvals:
            calls = approval.get("tool_calls")
            if calls is None:
                calls = [approval.get("tool_call")]
            batches.append(list(calls or ()))
        if not batches:
            return
        # Per-batch identity, exactly: the approval's ids must be the provider's
        # own ids, all of them, with no truncation and no collapse onto one shared
        # prefix. This is the wire the compatibility path must not shorten.
        for batch in batches:
            _require_native_ids([call.get("tool_call_id") for call in batch],
                                "approval_batch")
        wire_ids = [call.get("tool_call_id") for batch in batches for call in batch]
        check_id_projection(observed_ids, wire_ids, label="approval_wire")
        self.report.setdefault("multicall_batches", []).append({
            "arm": post["_arm"], "phase": post["_phase"], "task_phase": post["_phase"],
            "batch_sizes": [len(batch) for batch in batches],
            "count": sum(len(batch) for batch in batches),
            "ids": wire_ids})

    def check_tool_return(self, message):
        returns = message.get("tool_returns")
        need(isinstance(returns, list) and returns, "empty_tool_return_batch")
        appended = []
        for returned in returns:
            tid = returned.get("tool_call_id")
            need(tid in self.pending, "return_without_observed_model_tool_call")
            executed = self.pending[tid]
            self.check_submitted_tool_return(returned, {"returned": returned}, source="submission")
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
            appended.append({"role": "tool", "returned": deepcopy(returned), "tool_call_id": tid})
        return appended

    def classify_new_input(self, post):
        body = post["body"]
        need(not body.get("messages") or isinstance(body["messages"], list), "messages_not_list")
        new_input, tool_returns = [], 0
        for message in body.get("messages") or []:
            need(isinstance(message, dict), "unexpected_submitted_message_shape")
            role = message.get("role")
            if role == "user":
                self.check_user_message(message, post["_arm"], post["_phase"])
                new_input.append({"role": "user", "content": message["content"]})
            elif message.get("type") == "tool_return" and role is None:
                for entry in self.check_tool_return(message):
                    new_input.append(entry)
                tool_returns += 1
            else:
                self.fail("unexpected_submitted_message_shape")
        need(new_input, "message_post_adds_no_new_input")
        post["_new_input"] = new_input
        self.report["counts"]["tool_returns"] += tool_returns

    # -- model calls --------------------------------------------------------
    def check_system_framing(self, body, arm):
        sent = body.get("messages")
        need(isinstance(sent, list) and sent and sent[0].get("role") == "system",
             "system_not_first")
        system = sent[0].get("content")
        need(isinstance(system, str), "system_content_not_string")
        declared = self.plan["arms"][arm]["agent_payload"]["system"]
        need(isinstance(declared, str) and declared, "plan_system_missing")
        need(system.startswith(declared), "system_prefix_changed")
        rest = system[len(declared):]
        state = self.arm_state[arm]
        memory = render_memory_blocks(state["block"], state["value"])
        need(rest.count(MEMORY_BLOCK_HEADER) == 1, "extra_memory_block_header")
        need(rest.startswith("\n\n" + MEMORY_BLOCK_HEADER), "memory_block_framing_changed")
        need(rest.startswith("\n\n" + memory), "rendered_block_does_not_match_arm_state")
        marker = "\n\n" + memory + "\n\n"
        need(rest.startswith(marker + "<memory_metadata>\n"), "memory_metadata_separator_changed")
        tail = rest[len(marker):]
        match = MEMORY_METADATA.match(tail)
        need(match is not None, "memory_metadata_tail_changed")
        lines = match.group("lines").splitlines()
        need(len(lines) >= 4, "memory_metadata_lines_missing")
        agent = MEMORY_METADATA_AGENT.match(lines[0])
        need(agent is not None, "memory_metadata_agent_id_format_changed")
        need(agent.group("agent_id") == state["agent_id"], "system_agent_id_mismatch")
        need(MEMORY_METADATA_CONVERSATION.match(lines[1]) is not None,
             "memory_metadata_conversation_id_format_changed")
        need(MEMORY_METADATA_RECOMPILED.match(lines[2]) is not None,
             "memory_metadata_recompiled_line_format_changed")
        need(MEMORY_METADATA_RECALL.match(lines[3]) is not None,
             "memory_metadata_recall_line_format_changed")
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

    def check_agent_model_call(self, call, post):
        arm = post["_arm"]
        body = call["body"]
        profile = profile_of(self.cloud_config)
        need(set(body) <= declared_cloud_fields(profile), "undeclared_cloud_request_field")
        missing = []
        check_declared_transport_fields(body, profile, missing)
        need(not missing, missing[0] if missing else "declared_transport_field_missing")
        need("seed" not in body and "chat_template_kwargs" not in body,
             "generation_seed_or_template_sent")
        need(body.get("temperature") == PINNED["temperature"], "cloud_temperature_changed")
        if "parallel_tool_calls" in body:
            need(body["parallel_tool_calls"] is False, "parallel_tool_calls_not_false")
        if "stream" in body:
            need(body["stream"] is False, "streaming_requested")
        if "n" in body:
            need(body["n"] == 1, "multiple_completions_requested")
        limit = body.get("max_tokens", body.get("max_completion_tokens"))
        need(limit == PINNED["max_output_tokens"], "agent_output_limit_not_2048")
        reasons = cloud_change_reasons(call["changes"], body, profile, limit)
        need(not reasons, reasons[0] if reasons else "unexpected_cloud_change")
        tools = body.get("tools")
        need(isinstance(tools, list) and tools, "agent_request_has_no_tools")
        actual_tools = tool_schemas(tools, True)
        need(actual_tools == post["_declared_tools"], "upstream_tools_differ_from_declared")
        self.check_system_framing(body, arm)
        sent = body.get("messages")
        need(isinstance(sent, list) and sent, "cloud_call_without_messages")
        need(sent[0].get("role") == "system", "system_not_first")
        actual_history = sent[1:]
        expected_history = deepcopy(self.turns[arm]) + deepcopy(post["_new_input"])
        need(len(actual_history) == len(expected_history),
             "history_count_changed_or_extra_message")
        for observed, item in zip(actual_history, expected_history):
            need(observed.get("role") == item["role"], "history_order_or_role_changed")
            if item["role"] == "user":
                user_text(observed.get("content"), item["content"])
            elif item["role"] == "assistant":
                need(self.assistant_history_matches(observed, item), "assistant_history_changed")
            else:
                self.check_submitted_tool_return(observed, item, source="history")
        self.turns[arm] = deepcopy(expected_history)
        self.turns[arm].append(self.raw_assistant_turn(call["response"]))

    def map_posts(self):
        responses = {r.get("request_id"): r for r in self.http if r.get("kind") == "response"}
        self.turns = {arm: [] for arm in ARMS}
        ordered = sorted(self.posts, key=lambda item: utc(item["timestamp"]))
        generations = self.bind_generation_responses(ordered)
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
        # The authoritative id namespace is frozen once, from the provider's own
        # responses and the calls the bridge really executed. Tool names and wire
        # spellings are never mixed into it: identity is exact equality against
        # these ids, so `call_A` and `call_AB` stay two distinct legitimate ids.
        provider_ids, event_ids = set(), set()
        for chat in self.chat_calls:
            for choice in (chat.get("response") or {}).get("choices") or []:
                for tool_call in ((choice.get("message") or {}).get("tool_calls") or []):
                    if isinstance(tool_call.get("id"), str) and tool_call["id"]:
                        provider_ids.add(tool_call["id"])
        for entry in self.tool_events:
            call = entry.get("event", {}).get("call") or {}
            if isinstance(call.get("tool_call_id"), str) and call["tool_call_id"]:
                event_ids.add(call["tool_call_id"])
        self.initialise_id_namespace(provider_ids | event_ids)
        self.report["scope"]["multicall_provider_id_count"] = len(provider_ids)
        self.report["scope"]["multicall_prefix_collision_groups"] = self.collision_groups
        for post in ordered:
            request_event = self._bridge_request_for(post)
            arm, phase = request_event.get("arm"), request_event.get("phase")
            need(arm in ARMS, "unattributable_post_arm")
            need(phase in {"history", "task"}, "unrecognized_message_phase")
            post["_arm"], post["_phase"] = arm, phase
            # The history phase offers only the memory tool; the task phase offers
            # the memory tool plus the arm's native tools (ae_adapter.exchange).
            post["_declared_tools"] = ({MEMORY_TOOL_NAME: deepcopy(MEMORY_TOOL)} if phase == "history"
                                       else dict(self.stage_native[arm],
                                                 **{MEMORY_TOOL_NAME: deepcopy(MEMORY_TOOL)}))
            need(self.arm_state[arm]["phase"] != "task" or phase == "task" or phase == "setup",
                 "phase_regressed")
            self.arm_state[arm]["phase"] = phase
            self.arm_state[arm]["value"] = self.block_at_post[post["request_id"]]
            response = responses.get(post.get("request_id"))
            need(isinstance(response, dict) and response.get("http_status") == 200,
                 "letta_post_without_successful_response")
            generation = generations[post["request_id"]]
            generation_row = next(r for r in self.all_http_responses if r.get("body") is generation)
            a, z = utc(post["timestamp"]), utc(generation_row["timestamp"])
            inside = [c for c in self.chat_calls if a < c["start"] and c["end"] < z]
            need(len(inside) <= 1, "unsupported_multiple_model_calls_per_post")
            need(len(inside) == 1, "letta_post_without_mapped_model_call")
            call = inside[0]
            need(call["request_id"] not in used, "cloud_call_mapped_twice")
            need(self.start < call["start"] and call["end"] < self.end,
                 "cloud_call_outside_run_window")
            used.add(call["request_id"])
            post["_call"], post["_response"] = call, response
            post["_generation_response"] = generation
            self._consume_bridge_response(post, generation)
            # A verified PATCH must be reflected by the next rendered system, so
            # apply the pending per-arm block state before framing is checked.
            self.check_model_output(post, response)
            self.classify_new_input(post)
            self.check_agent_model_call(call, post)
            self.report["frames"].append({"kind": "agent_frame", "arm": arm, "phase": phase,
                                          "request_id": call["request_id"],
                                          "agent_id": self.arm_state[arm]["agent_id"],
                                          "block_sha256": digest(
                                              self.arm_state[arm]["value"].encode()),
                                          "passed": True})
            self.arm_state[arm]["posts"] += 1
        for row in self.http:
            if (row.get("kind") == "request" and row.get("method") == "GET"
                    and row.get("path") != "/v1/health/"):
                need(self.bridge_frame_count(row) == 1, "http_request_missing_bridge_trace")
        need(not self.pending, "unreturned_pending_tool_call")
        self.report["cloud_calls"]["agent"] = len(used)

    # -- arm-bounded auxiliary correlation ----------------------------------
    def auxiliary(self):
        self.report["cloud_calls"]["auxiliary"] = 0
        self.report["cloud_calls"]["unaccounted"] = 0
        agent_ids = {p["_call"]["request_id"] for p in self.posts}
        remaining = [c for c in self.chat_calls if c["request_id"] not in agent_ids]
        # The proxy records which role issued each call; that captured role is an
        # independent correlation, not a self-report from the native capture.
        captured_roles = {r.get("request_id"): r.get("role") for r in self.proxy
                          if r.get("kind") == "client_request"}
        for arm in ARMS:
            native = self.native_calls[arm]
            window = self._arm_window(arm)
            need(window is not None, "missing_arm_window")
            start, end = window
            candidates = [c for c in remaining if start < c["start"] and c["end"] < end]
            need(len(candidates) == len(native), "unaccounted_auxiliary_call_count_" + arm)
            # STRICT CAPTURE ORDER: native call i pairs with captured call i. A
            # later captured response can never be used to explain an earlier
            # native request, so two identical requests whose provider responses
            # were swapped are rejected instead of matched out of order.
            for index, record in enumerate(native):
                need(record.get("error") is None, "native_call_incomplete")
                request, response = record.get("request"), record.get("response")
                need(isinstance(request, dict) and isinstance(response, dict),
                     "native_call_incomplete")
                need(not request.get("tools"), "auxiliary_tools_not_supported")
                expected = no_nulls(auxiliary_wire_messages(request.get("messages")))
                match = candidates[index]
                need(no_nulls(match["body"].get("messages")) == expected,
                     "auxiliary_call_order_or_request_changed_in_" + arm)
                need(no_nulls(match["response"]) == no_nulls(response.get("raw_data")),
                     "auxiliary_call_order_or_response_changed_in_" + arm)
                need(captured_roles.get(match["request_id"]) == record.get("role"),
                     "auxiliary_captured_role_mismatch_in_" + arm)
                remaining.remove(match)
                for key in ("model", "temperature", "max_tokens"):
                    if key in request:
                        need(request[key] == match["body"].get(key),
                             "auxiliary_parameters_changed")
                need("seed" not in match["body"], "auxiliary_seed_sent")
                need(match["body"].get("tools") in (None, []), "unexpected_auxiliary_tools")
                limit = match["body"].get("max_tokens", match["body"].get("max_completion_tokens"))
                need(limit == PINNED["auxiliary_output_tokens"], "wrong_auxiliary_output_limit")
                reasons = cloud_change_reasons(match["changes"], match["body"],
                                               profile_of(self.cloud_config), limit)
                need(not reasons, reasons[0] if reasons else "unexpected_cloud_change")
                self.report["auxiliary_mappings"].append({
                    "arm": arm, "request_id": match["request_id"], "role": record.get("role"),
                    "subtask_id": record.get("subtask_id")})
            self.report["cloud_calls"]["auxiliary"] += len(native)
        need(not remaining, "unaccounted_auxiliary_call_count")
        counts = self.report["cloud_calls"]
        need(counts["agent"] + counts["auxiliary"] + counts["unaccounted"] == len(self.chat_calls),
             "unaccounted_agent_model_call")

    def _arm_window(self, arm):
        events = [e for e in self.events if e.get("arm") == arm]
        starts = [e for e in events if e.get("kind") == "stage_start"]
        ends = [e for e in events if e.get("kind") == "arm_complete"]
        if not starts or not ends:
            return None
        return utc(starts[0]["timestamp"]), utc(ends[0]["timestamp"])

    # -- memory semantics ---------------------------------------------------
    def memory_gate(self):
        """R: every block change has a real update + verified PATCH and is seen next.

        E: the original block is never PATCHed and every successful update's real
        erratum return reaches the arm's later wire. Update truth is not judged.
        """
        patches = [r for r in self.http if r.get("kind") == "request"
                   and r.get("method") == "PATCH"
                   and r.get("path", "").endswith("/core-memory/blocks/" + BLOCK_LABEL)]
        need(all(self._agent_of_path(r.get("path")) in ARMS for r in patches),
             "patch_for_unknown_agent")
        by_arm = {arm: [] for arm in ARMS}
        for row in sorted(patches, key=lambda r: r.get("sequence", 0)):
            by_arm[self._agent_of_path(row["path"])].append(row)
        need(not by_arm["erratum"], "erratum_block_was_patched")
        need(by_arm["rewrite"] or True, "noop")  # R may legitimately never update

        # Every PATCH must be confirmed by its own response with the same value,
        # in capture order, and its own bridge frames are consumed positionally
        # (a frame can never be skipped to find a later matching body).
        responses = {r.get("request_id"): r for r in self.http if r.get("kind") == "response"}
        ordered_patches = sorted(patches, key=lambda r: r.get("sequence", 0))
        for row in ordered_patches:
            response = responses.get(row.get("request_id"))
            need(isinstance(response, dict), "patch_without_response")
            body = row.get("body")
            need(set(body) == {"value"}, "unexpected_patch_fields")
            need(response.get("body", {}).get("value") == body["value"],
                 "patch_not_confirmed_with_same_value")
        self._consume_patch_frames(ordered_patches, responses)

        # R's successful updates and PATCHes are paired STRICTLY in capture
        # order: the i-th successful memory_update event must be explained by the
        # i-th PATCH, with the i-th call's own rendered block. Two updates with
        # identical values still pair one-to-one; two different updates whose
        # execution events were re-ordered are rejected.
        successful = {}
        for event in sorted(self.tool_events, key=lambda e: e.get("sequence", 0)):
            inner = event["event"]
            call, result = inner.get("call") or {}, inner.get("result") or {}
            if call.get("name") != MEMORY_TOOL_NAME:
                continue
            arm = event.get("arm")
            need(arm in ARMS, "memory_update_without_arm")
            need(result.get("status") in {"success", "error"}, "unexpected_native_tool_status")
            if result.get("status") == "success":
                successful.setdefault(arm, []).append((event, call, result))
        rewrites = successful.get("rewrite", [])
        need(len(rewrites) == len(by_arm["rewrite"]),
             "rewrite_update_patch_count_changed")
        self._patch_rows = ordered_patches
        for index, (event, call, result) in enumerate(rewrites):
            returned = json.loads(result["tool_return"])
            need(returned.get("status") == "updated", "rewrite_update_not_updated")
            rendered = dumps(returned.get("memory_block"))
            patch = by_arm["rewrite"][index]
            need(patch.get("body", {}).get("value") == rendered,
                 "rewrite_update_patch_order_or_value_changed")
            self.report["memory_mappings"].append({
                "arm": "rewrite", "tool_call_id": call.get("tool_call_id"),
                "patch_request_id": patch["request_id"],
                "block_sha256": digest(rendered.encode()), "patch_verified": True})

        # E: no PATCH, and every successful update's real erratum return must
        # reach the arm's later wire.
        for event in sorted(self.tool_events, key=lambda e: e.get("sequence", 0)):
            inner = event["event"]
            call, result = inner.get("call") or {}, inner.get("result") or {}
            if call.get("name") != MEMORY_TOOL_NAME:
                continue
            arm = event.get("arm")
            need(arm in ARMS, "memory_update_without_arm")
            need(result.get("status") in {"success", "error"}, "unexpected_native_tool_status")
            if result.get("status") != "success":
                continue
            returned = json.loads(result["tool_return"])
            if arm == "rewrite":
                continue
            if True:
                need(returned.get("status") == "appended", "erratum_update_not_appended")
                erratum = returned.get("erratum")
                need(isinstance(erratum, str) and erratum.startswith("[STATE UPDATE]\n"),
                     "erratum_missing_or_malformed")
                # The real return is a JSON document, so the erratum field is
                # compared after parsing, never by substring on escaped JSON.
                def submitted_erratum(entry):
                    returned_text = (entry.get("returned") or {}).get("tool_return")
                    if not isinstance(returned_text, str):
                        return None
                    try:
                        parsed = json.loads(returned_text)
                    except ValueError:
                        return None
                    if not isinstance(parsed, dict):
                        return None
                    if parsed.get("erratum") == erratum or parsed.get("tool_return") == erratum:
                        return parsed
                    return None

                wire_seen = any(
                    submitted_erratum(entry) is not None
                    for post in self.posts if post.get("_arm") == arm
                    and utc(post["timestamp"]) > utc(event["timestamp"])
                    for entry in post.get("_new_input") or [])
                need(wire_seen, "erratum_not_in_later_wire")
                self.report["erratum_mappings"].append({
                    "arm": arm, "tool_call_id": call.get("tool_call_id"),
                    "erratum_sha256": digest(erratum.encode()), "entered_later_wire": True})

        # The block the agent actually saw must equal the verified PATCH state.
        self._check_block_state(by_arm)

    def _check_block_state(self, by_arm):
        """GET block values follow only verified PATCHes, in capture order."""
        expected = {arm: self.plan["arms"][arm]["initial_block"] for arm in ARMS}
        for record in sorted(self.http, key=lambda row: row.get("sequence", 0)):
            if record.get("kind") == "request" and record.get("method") == "PATCH":
                arm = self._agent_of_path(record.get("path"))
                need(arm in ARMS, "patch_for_unknown_agent")
                expected[arm] = record["body"]["value"]
            elif (record.get("kind") == "response" and isinstance(record.get("body"), dict)
                  and "blocks" in record["body"]):
                arm = self._agent_of_id(record["body"].get("id"))
                if arm not in ARMS:
                    continue
                blocks = record["body"]["blocks"]
                need(isinstance(blocks, list) and len(blocks) == 1,
                     "unexpected_inspected_block_count")
                need(blocks[0].get("label") == BLOCK_LABEL, "wrong_inspected_block_label")
                need(blocks[0].get("value") == expected[arm],
                     "inspected_block_differs_from_verified_state")
                need(record["body"].get("tools") == [], "inspected_tools_changed")
        for arm in ARMS:
            need(self.arm_state[arm]["value"] == expected[arm],
                 "final_arm_block_differs_from_verified_patches")
        self.report["arms"] = {
            arm: {"agent_id": self.arm_state[arm]["agent_id"],
                  "initial_block_sha256": digest(
                      self.plan["arms"][arm]["initial_block"].encode()),
                  "final_block_sha256": digest(expected[arm].encode()),
                  "patches": len(by_arm[arm]), "memory_arm": arm, "label": ARM_LABELS[arm]}
            for arm in ARMS}
        self.report["patches"] = [
            {"arm": "rewrite", "value_sha256": digest(row["body"]["value"].encode())}
            for row in by_arm["rewrite"]]

    def _consume_patch_frames(self, ordered_patches, responses):
        """Consume PATCH bridge frames STRICTLY positionally.

        The k-th captured PATCH row (in capture order) must own the k-th captured
        PATCH frame; frames can never be skipped to find a later equal body. Two
        byte-identical same-fact replaces still pair one-to-one, while a missing,
        extra or re-ordered frame is rejected.
        """
        frames = sorted((e for e in self.bridge_requests
                         if e["event"].get("method") == "PATCH"),
                        key=lambda e: (utc(e["timestamp"]), e.get("sequence", 0)))
        need(len(frames) == len(ordered_patches), "patch_bridge_frame_count_changed")
        for row, frame in zip(ordered_patches, frames):
            need(frame["event"].get("path") == row.get("path"),
                 "patch_bridge_frame_path_changed")
            need(frame["event"].get("body") == row.get("body"),
                 "patch_bridge_frame_body_order_changed")
            need(abs((utc(frame["timestamp"]) - utc(row["timestamp"])).total_seconds()) <= 0.25,
                 "patch_bridge_frame_clock_changed")
            self.bridge_requests.remove(frame)
        expected = [responses[row["request_id"]].get("body") for row in ordered_patches]
        response_frames = [e for e in self.bridge_responses
                           if any(e["event"].get("body") == body for body in expected)]
        need(len(response_frames) == len(ordered_patches),
             "patch_bridge_response_frame_count_changed")
        for body, frame in zip(expected, response_frames):
            need(frame["event"].get("body") == body,
                 "patch_bridge_response_frame_order_changed")
            self.bridge_responses.remove(frame)

    def _agent_of_path(self, path):
        match = re.match(r"^/v1/agents/([^/?]+)", path or "")
        if match is None:
            return None
        for arm in ARMS:
            if self.arm_state.get(arm, {}).get("agent_id") == match.group(1):
                return arm
        return None

    def _agent_of_id(self, agent_id):
        for arm in ARMS:
            if self.arm_state.get(arm, {}).get("agent_id") == agent_id:
                return arm
        return None

    def judge_gate(self):
        """Both required native evaluation chains must be present and complete.

        A low score, no update or a legal task failure stays valid; only a
        missing/failed/aborted evaluation chain, or a forged completion flag on
        top of one, invalidates the pair.
        """
        result = self.result
        need(result.get("stopped_after_arm") is None, "pair_stopped_before_both_arms")
        for key in ("native_aborted_after_judge_failure", "abort_error", "snapshot_error"):
            need(key not in result, "judge_failure_recorded_" + key)
        for arm in ARMS:
            record = (result.get("arms") or {}).get(arm)
            need(isinstance(record, dict), "arm_record_missing_" + arm)
            judge = record.get("judge")
            need(isinstance(judge, dict), "judge_record_missing_" + arm)
            need(judge.get("error") is None, "judge_error_present_" + arm)
            need(judge.get("status") == "MODEL_JUDGED_DEBUG_ONLY",
                 "judge_status_not_completed_" + arm)
            need(isinstance(judge.get("reward_info"), dict), "judge_reward_missing_" + arm)
            evaluator = [r for r in self.native_calls[arm] if r.get("role") == "evaluator"]
            need(bool(evaluator), "native_evaluator_call_missing_" + arm)
            for record_call in evaluator:
                need(record_call.get("error") is None, "native_evaluator_call_failed_" + arm)
        self.report["judge_chains"] = {
            arm: {"status": result["arms"][arm]["judge"]["status"],
                  "evaluator_calls": len([r for r in self.native_calls[arm]
                                          if r.get("role") == "evaluator"])}
            for arm in ARMS}

    # -- separation / private truth / closing --------------------------------
    def separation_gate(self):
        """No cross-arm mixing, no private future input on an agent wire."""
        arms = {e.get("arm") for e in self.events if "arm" in e} | {
            p.get("_arm") for p in self.posts} | set(self.report["arms"])
        need(arms <= set(ARMS), "event_or_post_arm_outside_declared_arms")
        need({arm: self.arm_state[arm]["agent_id"] for arm in ARMS}.__len__() == 2,
             "missing_arm_agent")
        need(self.arm_state["rewrite"]["agent_id"] != self.arm_state["erratum"]["agent_id"],
             "arms_share_an_agent")
        first = [e for e in self.events if e.get("kind") == "stage_start"]
        need([e.get("arm") for e in first] == list(ARMS), "arm_execution_order_changed")
        # Private/future values must not appear on any agent Letta POST or agent
        # provider call. Auxiliary roles (native user/judge) legitimately receive
        # the current task projection and are checked separately.
        private = self.plan.get("inputs", {}).get("private_boundary") or {}
        forbidden = []
        t5_instruction = private.get("t5_instruction")
        if isinstance(t5_instruction, str) and t5_instruction.strip():
            forbidden.append(t5_instruction)
        for value in private.get("forbidden_value_strings") or []:
            if isinstance(value, str) and value.strip() and value not in forbidden:
                forbidden.append(value)
        agent_blobs = [json.dumps(p.get("body"), ensure_ascii=False) for p in self.posts]
        agent_blobs += [json.dumps(p.get("_call", {}).get("body"), ensure_ascii=False)
                        for p in self.posts]
        blob = "\n".join(agent_blobs)
        for value in forbidden:
            need(value not in blob, "private_future_value_on_agent_wire")
        need("sub_U000828_5" not in blob, "future_turn_leaked")
        need("t5/" not in blob, "future_turn_leaked")
        self.report["private_boundary"] = {
            "declared_strings": len(forbidden), "present_on_agent_wire": False,
            "t4_evaluation_criteria_sha256": private.get("t4_evaluation_criteria_sha256"),
            "t5_subtask_id": private.get("t5_subtask_id")}
        # Every declared post/auxiliary call is consumed exactly once.
        need(not self.bridge_requests and not self.bridge_responses,
             "unmatched_bridge_http_trace")
        need(len(self.consumed_tool_events) == len(self.tool_events),
             "executed_tool_result_not_submitted")
        need(not self.pending, "unreturned_pending_tool_call")
        for arm in ARMS:
            need(self.runtime_user_index[arm] == len(self.expected_runtime_users[arm]),
                 "native_user_reply_not_sent_to_agent_" + arm)
        need(self.result.get("arms", {}).get("rewrite", {}).get("memory_arm") == "rewrite"
             and self.result.get("arms", {}).get("erratum", {}).get("memory_arm") == "erratum",
             "result_arm_semantics_changed")

    # -- top level ----------------------------------------------------------
    def verify(self):
        report = self.report
        try:
            self.load()
            self.transport_gate()
            self.config_gate()
            self.provenance_gate()
            self.dataset_gate()
            self.window_gate()
            self.event_correlation_setup()
            self.http_trace_gate()
            self.creation_gate()
            self.stage_gate()
            self.native_capture()
            self.judge_gate()
            self.agent_posts()
            self.cloud_calls()
            self.map_posts()
            self.auxiliary()
            self.memory_gate()
            self.separation_gate()
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


def audit_re_pair_inputs(run_dir, proxy_journal, *, config_path=None, dataset_path=None) -> dict:
    """Convenience entry point; always returns a report dict, never raises."""
    return RePairInputAudit(run_dir, proxy_journal, config_path=config_path,
                            dataset_path=dataset_path).verify()
