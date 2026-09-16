"""Read-only input audit for the continuous original-data t4->t12 cloud R/E pair.

This is a SEPARATE schema from the single-t4 audit (`ae-cloud-re-input-audit-0.1`)
because the captured object is different: ONE persistent agent per arm executes
NINE consecutive original tasks, so every consumed cloud record has to be
attributed to exactly one (arm, task, phase) without searching journals by
content, and the audit must distinguish a legal low score from an execution
anomaly across all 18 phases.

It reuses the verified shared machinery instead of re-implementing it:

* the cloud journal transport check and journal consumption walk
  (`CloudInputAudit.cloud_calls`),
* the pinned Letta PromptGenerator framing (`check_system_framing`), the real
  memory-block renderer (`render_memory_blocks`), the OpenAI history projection
  (`assistant_history_matches`, `same_tool_calls`) and the packaged tool-return
  wrapper (`check_submitted_tool_return`),
* the native auxiliary capture projection (`auxiliary_wire_messages`,
  `AUXILIARY_INTERNAL_FIELDS`),
* stable-snapshot reading (`read_stable`, `rows`, `wire`).

Only the multi-turn gates are new. Every gate here performs a real check and
records its result; a mapping that is empty because no check ran is never
reported as a passed check (`checks_executed` lists what actually ran).

The check never judges whether a preference update was CORRECT. A missing
update, a wrong update, or a legal task failure are valid behaviour and keep
their real recorded score. It verifies that:

* every model input on every wire is the declared public input for exactly the
  phase it was sent in, continued from that arm's own prior verified turns;
* the Letta system framing, the OpenAI wire, tool schemas, native tool-call ids,
  arguments and returns agree frame by frame;
* each arm's history admission, current-task and reply indices are isolated per
  (arm, task): one arm's history never satisfies the other arm's prerequisite;
* every runtime_user body is the real native user-simulator reply for that task
  AND appears byte-identically on the actual wire;
* every auxiliary call is matched to exactly one (arm, task, phase) window in
  authoritative capture order, and every cloud record is consumed;
* R's block changes have a real memory_update + PATCH source and are visible to
  the next rendered system; E's block never changes and its errata reach the
  wire as real tool returns;
* the judge ran on this task's own transcript and this task's own environment.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import importlib
import os
import re
import sys
import tarfile

from ae_adapter import BLOCK_LABEL, MEMORY_TOOL, dumps
from ae_capability import USER_ID
from ae_inputs import USER_INDEX
from ae_cloud_input_audit import (MESSAGE_POST_PATH, PINNED_LIMITS, TOOL_RETURN_WRAPPER_KEYS,
                                  CLOUD_FIELDS, MEMORY_BLOCK_HEADER, MODELS_PATH, CHAT_PATH,
                                  CloudInputAudit, auxiliary_wire_messages,
                                  TOOL_CALL_ID_MAX_LEN, check_declared_transport_fields,
                                  cloud_change_reasons, declared_cloud_fields,
                                  render_memory_blocks, tool_schemas)
from ae_cloud_proxy import profile_of
#: The ONE run-identity file that is the POST-HOC AUDITOR rather than a producer of the
#: capture. A run record pins its digest as it was at RUN time, so repairing the audit after
#: the capture exists makes that single digest differ; the explicit sealed re-audit must
#: then be handed the runtime file's real sealed copy instead of the record being ignored.
SEALED_AUDIT_MODULE = "ae_cloud_re_multiturn_input_audit.py"

#: The auditor's OWN code identity, by path and by the digest of the bytes that really run
#: here. It is deliberately NOT taken from the run record: a report must never present the
#: sealed run's hashes as the identity of the code that audited it.
AUDITOR_IDENTITY_FILES = (
    "scripts/ae_01_cloud_re_multiturn_input_audit.py",
    SEALED_AUDIT_MODULE,
    "ae_cloud_input_audit.py",
    "ae_cloud_audit.py",
)

from ae_cloud_re_multiturn import (ARM_LABELS, ARMS, END_TURN, NO_COMPACTION_TAG,
                                   PAIR_MAX_REQUESTS,
                                   PACING_CANDIDATE, PURPOSE,
                                   REQUEST_COUNTING, SCHEMA_VERSION as MULTITURN_SCHEMA,
                                   SCHEMA_VERSION_CAPACITY as MULTITURN_SCHEMA_CAPACITY,
                                   SCHEMA_VERSION_DEEPSEEK as MULTITURN_SCHEMA_DEEPSEEK,
                                   SCHEMA_VERSION_MULTICALL as MULTITURN_SCHEMA_MULTICALL,
                                   SCHEMA_VERSION_SIM_EVAL as MULTITURN_SCHEMA_SIM_EVAL,
                                   START_TURN, TASK_NUMBERS, WINDOW_SOURCES,
                                   multiturn_code_files, validate_capacity)
from ae_input_audit import (AuditFailure, digest, need, no_nulls, only, read_stable,
                            rows, utc, wire)
from ae_inputs import canonical_sha256
from ae_multicall import (CONFIG_KEY as MULTICALL_CONFIG_KEY, read_manifest,
                          read_stack_manifest, validate_stack_receipt,
                          resolve_policy, validate_launch_receipt, validate_policy)

SCHEMA_VERSION = "ae-cloud-re-multiturn-input-audit-0.1"

#: The ONE reviewed POST-HOC judge semantics protocol. The sealed protocol requires the
#: reply to echo every rubric's requirement text VERBATIM; the PINNED native evaluator
#: only reads `rubric_idx` (plus the justification and the boolean) and always keeps the
#: requirement text of the task's own initial states. This option re-derives the whole
#: scoring chain under that native rule and reports it SEPARATELY - it never turns the
#: strict protocol's own failure into a pass.
JUDGE_SEMANTICS_NATIVE_BY_ID = "native-by-id-v1"
JUDGE_SEMANTICS_MODES = (JUDGE_SEMANTICS_NATIVE_BY_ID,)

#: `NativeVita.finish` builds the native task id with exactly this prefix; the captured
#: simulation's `task_id` must BE that deterministic conversion of the subtask id.
NATIVE_TASK_ID_PREFIX = "U000828_subtask_"

#: The pinned native `Message` classes stamp a missing `timestamp` from `get_now()`, whose
#: format is this. A value the transcript did not carry was generated while the run's own
#: messages were converted; the audit checks the SHAPE (and the copies' agreement) and
#: never claims to have re-proved the wall clock.
NATIVE_TIMESTAMP_PATTERN = re.compile(r"^\d{8}_\d{6}$")

#: The run-local Vita model configuration the native wrapper used, by name: it lives inside
#: the run directory and nowhere else, so a leftover environment cannot supply another one.
VITA_MODEL_CONFIG_NAME = "vita-models.json"

#: The pinned wrapper's own conversion rules, matched (whitespace-flattened) against the
#: live `ae_vita.py` whose digest the run record pins: the task-id prefix, the per-row
#: native class conversion with `turn_idx`, and the default timestamp factory.
NATIVE_CONVERSION_SOURCE_MARKERS = (
    'id="U000828_subtask_"+self._active.subtask_id',
    'ifrow["role"]=="tool"and(notisinstance(row.get("id"),str)ornotrow["id"]'
    'ornotisinstance(row.get("name"),str)ornotrow["name"])',
    'message=classes[row["role"]].model_validate(deepcopy(row))',
    "message.turn_idx=i",
)


#: The schemas whose provider contract is the DeepSeek byte-gate protocol: 0.4 and its
#: protocol-carrying successor 0.5. Every DeepSeek-shaped branch below uses this set, so
#: the repair schema is audited by the SAME contract rather than by a second copy of it.
MULTITURN_DEEPSEEK_SCHEMAS = (MULTITURN_SCHEMA_DEEPSEEK, MULTITURN_SCHEMA_SIM_EVAL)


def _repair_module():
    """The repair protocol module, loaded by path (it is not a package member)."""
    import importlib.util
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "ae_sim_eval_protocol", root / "ae_sim_eval_protocol.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deepseek_module():
    """The DeepSeek integration module, loaded by path (it is not a package member)."""
    import importlib.util
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "ae_deepseek_re_transport", root / "ae_deepseek_re_transport.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
MEMORY_TOOL_NAME = MEMORY_TOOL["name"]
HISTORY_SOURCE = "dataset_history/material"
CURRENT_SOURCE = "current_task"
RUNTIME_SOURCE = "runtime_user"
SOURCES = (HISTORY_SOURCE, CURRENT_SOURCE, RUNTIME_SOURCE)
#: Pinned `vita/user/base.py` stop markers, mirrored exactly as in the single-t4
#: audit. Only the native captured reply decides whether a reply is a stop.
USER_STOP_MARKERS = ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###")
#: The only auxiliary dispatch roles the capture may declare. Anything else is a
#: role mismatch, not a new category.
AUXILIARY_ROLES = ("user_simulator", "evaluator")
#: The pinned SCORING modules the judge chain is re-derived from. These are the
#: modules whose bytes decide the score, so each one is compared with a traced
#: declaration - never with its own freshly computed digest. Paths are relative to
#: the checkout's `src` import root.
PINNED_SCORER_MODULES = {
    "evaluator": ("vita.evaluator.evaluator_traj", "vita/evaluator/evaluator_traj.py"),
    "message": ("vita.data_model.message", "vita/data_model/message.py"),
    "simulation": ("vita.data_model.simulation", "vita/data_model/simulation.py"),
    "tasks": ("vita.data_model.tasks", "vita/data_model/tasks.py"),
    "utils": ("vita.utils.utils", "vita/utils/utils.py"),
    "prompts": ("vita.prompts", "vita/prompts/__init__.py"),
}
#: The reviewed pinned scoring baseline (derived from the archived pinned source).
SCORER_BASELINE_SCHEMA = "ae-vita-scorer-baseline-1"
#: The delivered project this audit lives in; the `$REPO` anchor of a declaration.
REPO_ROOT = Path(__file__).resolve().parent
SCORER_BASELINE_ENV_VAR = "AE_VITA_SCORER_BASELINE"
SCORER_BASELINE_ARCHIVE_ENV_VAR = "AE_VITA_BASELINE_ARCHIVE"
#: The pinned `_evaluate_window` state update, as it really is in the fixed source.
#: The audit reproduces this rule to derive the state each window carries; these
#: markers are matched in order against the PINNED FUNCTION'S OWN SOURCE, so a
#: change to the pinned rule stops the derivation instead of silently diverging.
PINNED_STATE_UPDATE_MARKERS = (
    'updated_states = copy.deepcopy(current_states)',
    'result_data = evaluator_extracter(assistant_message.content)',
    'if result_data:',
    'for result in result_data:',
    'rubric_idx = result.get("rubric_idx")',
    'if rubric_idx and rubric_idx in updated_states:',
    'updated_states[rubric_idx]["justification"] = result.get(',
    '"No justification provided"',
    'updated_states[rubric_idx]["meetExpectation"] = result.get(',
)
#: Marker pairs inside an f-string, so they are matched as (head, tail) fragments.
PINNED_STATE_UPDATE_PAIRS = (
    ('updated_states[rubric_idx]["justification"] = result.get("justification"',
     '"No justification provided")'),
    ('updated_states[rubric_idx]["meetExpectation"] = result.get("meetExpectation"',
     'updated_states[rubric_idx]["meetExpectation"])'),
)
#: The pinned initial justification the initialiser writes for every rubric.
PINNED_INITIAL_JUSTIFICATION = "Not evaluated yet"
#: The pinned `_initialize_rubric_states` markers: the initial state is the fixed
#: values below, so the first window must carry exactly them.
PINNED_INITIAL_STATE_MARKERS = ('rubric_states[key] = {', '"rubric": rubric,',
                                '"meetExpectation": False')

PINNED = {
    "start_turn": START_TURN,
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

#: The public envelope ref rule, per (arm, task): `<tN>/history/<i>`,
#: `<tN>/user/0` for the current task, `<tN>/user/<k>` for replies.
REF_PATTERN = re.compile(r"^t(?P<turn>\d+)/(?P<kind>history|user)/(?P<index>\d+)$")
#: Fields each public envelope must declare exactly.
ENVELOPE_FIELDS = {
    HISTORY_SOURCE: {"source", "handling", "task_number", "records"},
    CURRENT_SOURCE: {"source", "subtask_id", "domain", "current_time",
                     "domain_policy", "instruction", "ref"},
    RUNTIME_SOURCE: {"source", "ref", "content"},
}


def multiturn_source_files(root=None):
    base = Path(root) if root else Path(__file__).resolve().parent
    names = ("ae_cloud_re_multiturn_input_audit.py",
             "scripts/ae_01_cloud_re_multiturn_input_audit.py",
             "ae_cloud_re_multiturn.py", "ae_input_audit.py",
             "ae_cloud_input_audit.py", "ae_cloud_audit.py", "ae_cloud_proxy.py",
             "ae_model_proxy.py", "ae_http.py")
    return {name: hashlib.sha256((base / name).read_bytes()).hexdigest()
            for name in names if (base / name).is_file()}


def _task_of(source: str, material: dict) -> tuple[int | None, str | None]:
    """Map one public message envelope to its declared task, structurally.

    The task id is never inferred from free text: a history envelope declares
    `task_number`, and a current/runtime envelope declares `subtask_id`.
    """
    if source == HISTORY_SOURCE:
        number = material.get("task_number")
        return (number if type(number) is int else None, None)
    subtask_id = material.get("subtask_id")
    if isinstance(subtask_id, str) and subtask_id.startswith(f"sub_{USER_ID}_"):
        tail = subtask_id.rsplit("_", 1)[-1]
        return (int(tail) if tail.isdigit() else None, subtask_id)
    return (None, None)


def _resolve_baseline_archive(declaration_path: Path, archive: dict, document: dict):
    """Find the archived pinned source the declaration is anchored to, or refuse.

    The declared `archive.path` is relative to the delivered project, so it is
    resolved against the declaration's own roots - an explicit
    `AE_VITA_BASELINE_ARCHIVE`, then an explicit `AE_VITA_BASELINE_ROOT`, then
    `AE_VERIFY_ROOT`, then `$REPO` as the declaration spells it, then the
    declaration's own directory. Nothing is ever written: the archive is only read.
    """
    relative = Path(archive["path"])
    if relative.is_absolute():
        candidates = [("declared_absolute", relative)]
    else:
        override = os.environ.get(SCORER_BASELINE_ARCHIVE_ENV_VAR)
        if override:
            candidates = [("environment_archive_override", Path(override))]
        else:
            candidates = []
            for root in document.get("roots") or ["$REPO"]:
                if root == "$AE_VITA_BASELINE_ROOT":
                    base = os.environ.get("AE_VITA_BASELINE_ROOT")
                    if base:
                        candidates.append(("AE_VITA_BASELINE_ROOT", Path(base)))
                elif root == "$AE_VERIFY_ROOT":
                    base = os.environ.get("AE_VERIFY_ROOT")
                    if base:
                        candidates.append(("AE_VERIFY_ROOT", Path(base)))
                elif root == "$REPO":
                    candidates.append(("repository_root", REPO_ROOT))
            candidates.append(("declaration_directory", declaration_path.parent))
    for anchor, base in candidates:
        candidate = (base / relative).resolve()
        if candidate.is_file():
            return candidate, anchor
    raise AuditFailure("scorer_baseline_archive_missing")


def _drop_foreign_vita_modules(package: Path) -> list:
    """Drop every already-imported `vita` module that is NOT from `package`.

    An import is not evidence of where a module came from: a `vita` already loaded
    from site-packages or from a second checkout would be handed straight back by
    `sys.modules`. The audit removes those entries (and only those) so the declared
    checkout is really imported, and reports which names it had to drop.
    """
    dropped = []
    for name, module in list(sys.modules.items()):
        if name != "vita" and not name.startswith("vita."):
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        try:
            path = Path(origin).resolve()
        except OSError:
            continue
        if path.is_relative_to(package):
            continue
        sys.modules.pop(name, None)
        dropped.append(name)
    return sorted(dropped)


def _flat_source(function):
    """One method's own source, whitespace-flattened for ordered marker matching."""
    import inspect

    try:
        return re.sub(r"\s+", "", inspect.getsource(function))
    except Exception as exc:
        raise AuditFailure("pinned_rule_source_unavailable") from exc


def _verify_pinned_state_update(evaluator_module):
    """Prove the pinned rules still ARE the rules the audit reproduces.

    `_evaluate_window` is a method body, not a callable rule, so the audit cannot
    invoke it offline without a model. Instead it matches the PINNED FUNCTIONS' OWN
    SOURCE: the update statements of `_evaluate_window`, and the initial values of
    `_initialize_rubric_states`. If the fixed source changes either rule, this stops
    the derivation instead of silently diverging from it.
    """
    evaluator = evaluator_module.TrajectoryEvaluator
    flat = _flat_source(evaluator._evaluate_window)
    position = 0
    for marker in PINNED_STATE_UPDATE_MARKERS:
        found = flat.find(re.sub(r"\s+", "", marker), position)
        need(found >= 0, "pinned_state_update_rule_changed")
        position = found + 1
    for head, tail in PINNED_STATE_UPDATE_PAIRS:
        need(re.sub(r"\s+", "", head) in flat and re.sub(r"\s+", "", tail) in flat,
             "pinned_state_update_fallback_changed")
    initial = _flat_source(evaluator._initialize_rubric_states)
    for marker in PINNED_INITIAL_STATE_MARKERS:
        need(re.sub(r"\s+", "", marker) in initial, "pinned_initial_state_rule_changed")
    need(re.sub(r"\s+", "", f'"{PINNED_INITIAL_JUSTIFICATION}"') in initial,
         "pinned_initial_state_rule_changed")


def _pinned_initial_states(evaluator, criteria) -> dict:
    """The exact initial state the pinned initialiser produces for this task.

    Every rubric starts unmet with the pinned initial justification; the first
    window must carry exactly this, so the running justification is verified from
    its initial value rather than only from a boolean.
    """
    states = evaluator._initialize_rubric_states(criteria)
    need(isinstance(states, dict) and states, "task_has_no_rubrics_to_judge")
    initial = {}
    for key, state in states.items():
        need(isinstance(state, dict) and isinstance(state.get("rubric"), str),
             "pinned_initial_state_shape_changed")
        initial[key] = {"rubric_idx": key, "rubric": state["rubric"],
                        "justification": state.get("justification"),
                        "meetExpectation": state.get("meetExpectation")}
    return initial


def _apply_pinned_state_update(states, decisions, parser):
    """Apply the pinned state update rule to one window's own decisions.

    This mirrors `TrajectoryEvaluator._evaluate_window` exactly - deep copy, parse
    through the pinned parser, update only indices the state carries, keep the
    previous justification/met when the reply omits one - and `_verify_pinned_state_update`
    proves the fixed source still says the same thing.
    """
    updated = deepcopy(states)
    result_data = parser(decisions)
    need(result_data, "judge_state_update_parsed_no_decisions")
    for result in result_data:
        need(isinstance(result, dict), "judge_state_update_result_not_an_object")
        key = result.get("rubric_idx")
        if key and key in updated:
            updated[key]["justification"] = result.get("justification",
                                                       "No justification provided")
            updated[key]["meetExpectation"] = result.get(
                "meetExpectation", updated[key]["meetExpectation"])
    return updated


def _requires_carried_state(expected, carried, window_index):
    """One window's carried rubric state must EQUAL the derived state exactly.

    Keys, requirement text, the running justification (the previous window's own
    justification, or the pinned initial one) and the boolean all have to match:
    a flipped boolean or a rewritten justification is a changed carried state.
    """
    need([item["rubric_idx"] for item in carried] == list(expected),
         f"judge_window_{window_index}_carried_rubric_order_changed")
    for item in carried:
        wanted = expected[item["rubric_idx"]]
        need(item["rubric"] == wanted["rubric"],
             f"judge_window_{window_index}_carried_rubric_text_changed")
        need(isinstance(item["justification"], str)
             and isinstance(wanted["justification"], str)
             and item["justification"] == wanted["justification"],
             f"judge_window_{window_index}_carried_justification_changed")
        need(item["meetExpectation"] is wanted["meetExpectation"],
             f"judge_window_{window_index}_carried_meet_expectation_changed")


#: The extra CURRENT-task envelope fields the 0.4 protocol adds: the clock of THAT
#: stage's own environment, carried on the wire instead of written into memory. They are
#: declared here so a sealed envelope can never grow them, and so a 0.4 envelope that
#: carries them is checked rather than merely tolerated.
CLARIFIED_ENVELOPE_FIELDS = ("environment_now", "environment_clock_format")


def _envelope_of(content, extra_fields=frozenset()) -> dict:
    """Decode one submitted user message into its declared envelope, or fail.

    A user message on an agent wire is never free text: it is the JSON envelope
    `ae_inputs.history_message` / `run_stage` / `reply_to_user` produce. `extra_fields`
    is empty for every sealed protocol and names the declared additions otherwise.
    """
    need(isinstance(content, str), "user_input_not_string")
    try:
        material = json.loads(content)
    except (TypeError, ValueError):
        raise AuditFailure("user_input_not_declared_material") from None
    need(isinstance(material, dict), "user_input_not_declared_material")
    source = material.get("source")
    need(source in SOURCES, "undeclared_user_input_source")
    declared = set(ENVELOPE_FIELDS[source])
    if source == CURRENT_SOURCE:
        declared |= set(extra_fields)
    need(set(material) == declared, "user_envelope_fields_changed")
    return material


class MultiturnInputAudit(CloudInputAudit):
    """One audit pass over one immutable multi-turn R/E pair capture set."""

    def __init__(self, run_dir, proxy_journal, *, config_path=None, dataset_path=None,
                 sealed_audit_copy=None, judge_semantics=None, vita_model_config=None):
        super().__init__(run_dir, proxy_journal, config_path=config_path,
                         dataset_path=dataset_path)
        #: The explicit OFFLINE RE-AUDIT of a sealed capture: the real copy of the audit
        #: module as it existed when the run was recorded. It is only ever compared and
        #: recorded, never imported or executed - the code that runs is this file.
        self.sealed_audit_copy = Path(sealed_audit_copy) if sealed_audit_copy else None
        #: The explicit POST-HOC judge semantics recheck, or None for the strict protocol.
        #: An unknown value is refused in `verify`, never treated as the default.
        self.judge_semantics = judge_semantics
        self.native_judge_semantics = judge_semantics == JUDGE_SEMANTICS_NATIVE_BY_ID
        #: THIS run's own `vita-models.json`, established before the pinned Vita package is
        #: imported: with no declaration the pinned `vita.config` silently falls back to the
        #: checkout's `models.yaml`/`models.yaml.example`, which is a configuration the run
        #: never declared (and which asked for an API key the run does not use). The
        #: default IS this run's own file; the gate refuses any other one.
        self.vita_model_config = (Path(vita_model_config) if vita_model_config
                                  else Path(run_dir) / VITA_MODEL_CONFIG_NAME)
        #: Every config path this process has proven to carry THIS run's declaration.
        self._vita_config_paths = {self.vita_model_config.resolve()}
        #: Every collected judge echo difference (arm/task/window/rubric_idx/texts). Only
        #: the explicit native-semantics mode may collect one; the strict mode refuses.
        self.judge_echo_differences = []
        self.multicall = None
        #: How many OpenAI-history tool-call ids were accepted as the native id itself
        #: (the declared compatibility form) or as its pinned 29-character projection
        #: (the form the deployed service really wrote), and how many were refused.
        self._id_forms = {"identity": 0, "projection": 0, "refused": 0}
        self.manifest = None
        self.task_numbers = TASK_NUMBERS
        self.report = {
            "schema_version": SCHEMA_VERSION, "status": "INVALID",
            "transport_capture_checked": False, "input_audit_passed": False,
            "task_success": None, "scientific_result": None,
            # The STRICT sealed protocol's own verdict, and the SEPARATE post-hoc native
            # semantics recheck. The second never turns the first into a pass.
            "strict_protocol_passed": None, "native_semantics_checks_passed": None,
            "judge_semantics": None, "judge_echo_differences": [],
            "judged_simulation_sources": [], "native_conversions": [],
            "runtime_generated_timestamps": {"count": 0, "per_phase": []},
            "vita_model_config": None,
            # Every entry is appended only when the check actually ran.
            "checks_executed": [],
            "sources": {}, "code_sha256": multiturn_source_files(),
            "frames": [], "auxiliary_mappings": [], "tool_mappings": [],
            "memory_mappings": [], "erratum_mappings": [], "patches": [],
            "judge_chains": [],
            "pinned_scoring_modules": None,
            "capacity_policy": None,
            "multicall_batches": [],
            "cloud_calls": {"models": 0, "agent": 0, "auxiliary": 0, "unaccounted": 0},
            "scope": {"user_id": USER_ID, "start_turn": START_TURN, "end_turn": None,
                      "task_numbers": [], "phase_count": None,
                      "arms": list(ARMS), "arm_order": list(ARMS),
                      "dataset_history_sent": None, "memory_update_tool": None,
                      "multicall_profile": None, "multicall_manifest": None,
                      "phases": [], "tasks": [], "partial": False,
                      "task_scores": []},
            "limitations": [
                "read-only over the captured files; a stable snapshot does not certify future requests",
                "no token / KV-prefix / cache agreement is checked or implied",
                "auxiliary calls are attributed to an arm, task and phase by the captured "
                "native reply order plus the phase windows derived from the verified agent "
                "posts, not by a cryptographic identity",
                "the proxy journal's own read/client fields are not covered by the transport check",
                "if a real capture renders the memory block differently than the pinned Letta "
                "framing helper, this file fails closed rather than passing",
                "the judge chain is checked against the task transcript the bridge submitted; "
                "it is not an independent re-scoring of the task",
                "the scored simulation is located inside the phase's own recorded native "
                "snapshot by the arm's and task's `completed[].subtask_id` and cross-checked "
                "with `last_evaluation`; the record carries no per-phase copy of its own, and "
                "the audit never searches another arm or a later phase for one",
                "a message `timestamp` the task transcript did not carry was generated by the "
                "pinned native default factory when the run converted its messages: the audit "
                "checks that saved value's shape and the snapshot copies' agreement, and does "
                "NOT claim to have re-proved the wall clock it recorded",
                "auxiliary native records carry no per-call time; they are bound to the "
                "proxy's captured calls position by position, in the verified phase order "
                "and within-phase record order, not by a content search",
                "the explicit `native-by-id-v1` judge recheck re-derives the pinned native "
                "rubric-by-id update semantics over the same captured windows; it is a "
                "POST-HOC protocol and never re-labels the run as passing the strict verbatim "
                "echo requirement",
            ],
            "invalid_reasons": [],
        }
        self.plan, self.result, self.events, self.http = None, None, None, None
        self.proxy = None
        self.phases = []
        self.tasks = {}
        self.phase_records = {}
        self.post_agent = {}
        self.post_task = {}
        self.post_phase = {}
        self.phase_of_post = {}
        self.auxiliary_by_phase = {}
        self.judge_chains = {}
        #: The declared pinned Vita checkout root, resolved once in `config_gate`.
        self._evaluator = None
        #: Judge-chain derivations, reset per gate run so a second run cannot reuse
        #: the previous run's pinned modules, baseline, dataset or rubric criteria.
        self._pinned = None
        self._baseline = None
        self._initial_states = {}
        self._criteria = {}
        self._env_times = {}
        self._dataset = None

    # -- helpers -----------------------------------------------------------
    def _checked(self, name):
        self.report["checks_executed"].append(name)

    def _agent_of_path(self, path):
        match = re.match(r"^/v1/agents/([^/?]+)", path or "")
        if match is None:
            return None
        for arm in ARMS:
            if (self.result["arms"].get(arm) or {}).get("agent_id") == match.group(1):
                return arm
        return None

    def _agent_of_id(self, agent_id):
        for arm in ARMS:
            if (self.result["arms"].get(arm) or {}).get("agent_id") == agent_id:
                return arm
        return None

    def _agent_id(self, arm):
        return (self.result["arms"].get(arm) or {}).get("agent_id")

    def initialise_id_namespace(self, provider_ids):
        """Freeze the authoritative provider id set once, before any comparison.

        Under the sealed protocol (`multicall is None`) the pinned 29-character
        prefix projection is used, exactly as the shared single-t4 audit uses it.
        Under the declared compatibility protocol the provider's own ids are
        authoritative and are compared whole, with the prefix groups recorded as
        evidence of what the old path would have collapsed.
        """
        if self.multicall is None:
            self.provider_ids, self.collision_groups = set(), []
            return
        from ae_multicall import collision_groups
        ids = sorted({str(value) for value in provider_ids if value})
        for value in ids:
            need(isinstance(value, str) and value, "provider_tool_call_id_invalid")
        self.provider_ids = set(ids)
        self.collision_groups = [list(group) for group in collision_groups(ids)]

    def _freeze_provider_ids(self):
        """Freeze every native tool-call id the provider really returned, once.

        The audit reads them from its own proxy journal - the response the provider sent -
        and NOT from the history the service rewrote, so the resolution basis cannot be
        supplied by the thing being checked.
        """
        ids = set()
        for call in getattr(self, "chat_calls", None) or ():
            turn = self.raw_assistant_turn(call.get("response") or {})
            for tool_call in turn.get("tool_calls") or ():
                value = (tool_call or {}).get("id")
                if isinstance(value, str) and value:
                    ids.add(value)
        self.initialise_id_namespace(ids)

    def wire_tool_call_id(self, full_id):
        """Protocol-aware OpenAI-history id mapping.

        The sealed protocol uses the parent's fixed 29-character projection; the
        declared compatibility protocol keeps the native id whole. This is the form the
        protocol DECLARES; a capture whose service wrote the other accepted form is
        resolved by `resolve_history_tool_call_id` instead of being compared to this.
        """
        if self.multicall is None:
            return super().wire_tool_call_id(full_id)
        need(isinstance(full_id, str) and full_id, "wire_tool_call_id_missing")
        return full_id

    def _known_native_ids(self):
        """The authoritative native tool-call ids this capture has already shown."""
        values = getattr(self, "provider_ids", None)
        known = {value for value in (values or ()) if isinstance(value, str) and value}
        groups = getattr(self, "known_tool_ids", None)
        if isinstance(groups, dict):
            for group in groups.values():
                known.update(value for value in (group or ())
                             if isinstance(value, str) and value)
        elif groups:
            known.update(value for value in groups if isinstance(value, str) and value)
        return known

    def resolve_history_tool_call_id(self, wire_id):
        """The ONE native id this OpenAI-history wire id can have come from.

        The declared compatibility protocol preserves native ids, and the service that
        actually wrote this capture emitted `TOOL_CALL_ID_MAX_LEN`-character projections
        of them instead (its launch record declares no multicall profile, so the pinned
        serializer's slice ran; see the delivery note). BOTH forms are therefore accepted
        under ONE rule, and only for ids this capture itself produced:

        * the value IS one of the capture's native ids - the declared form; or
        * the value is EXACTLY `TOOL_CALL_ID_MAX_LEN` characters long and is the
          projection of EXACTLY ONE native id of this capture.

        Anything else is refused: an unknown id, a shorter prefix, a longer value, or a
        projection shared by two native ids (`tool_call_id_truncation_collision`) is not
        a wire form of anything here. This is a one-to-one resolution, never a prefix
        match, and the counter records which form was used.
        """
        need(isinstance(wire_id, str) and wire_id, "wire_tool_call_id_missing")
        known = self._known_native_ids()
        if wire_id in known:
            self._id_forms["identity"] = self._id_forms.get("identity", 0) + 1
            return wire_id
        if len(wire_id) == TOOL_CALL_ID_MAX_LEN:
            matches = sorted(value for value in known
                             if value[:TOOL_CALL_ID_MAX_LEN] == wire_id)
            if len(matches) > 1:
                raise AuditFailure("tool_call_id_truncation_collision")
            if len(matches) == 1:
                self._id_forms["projection"] = self._id_forms.get("projection", 0) + 1
                return matches[0]
        self._id_forms["refused"] = self._id_forms.get("refused", 0) + 1
        raise AuditFailure("wire_tool_call_id_not_a_native_id")

    def history_tool_return_id_matches(self, observed_id, native_id):
        """The multicall history comparison, by unique resolution of the wire id.

        The declared protocol would write `native_id` itself; the deployed service wrote
        its 29-character projection. Either resolves to `native_id`; nothing else does.
        """
        if self.multicall is None:
            return super().history_tool_return_id_matches(observed_id, native_id)
        need(isinstance(native_id, str) and native_id, "missing_native_tool_call_id")
        try:
            return self.resolve_history_tool_call_id(observed_id) == native_id
        except AuditFailure:
            return False

    def same_tool_calls(self, observed, expected):
        """Compare OpenAI tool calls by id, name and argument text.

        The comparison projects both sides onto the same four values, so the
        OpenAI `{"type": "function", ...}` wrapper is not treated as a difference:
        the id (projected by the protocol's own rule), the name, and the argument
        text must match exactly. A missing `type` wrapper and an explicit one are
        the same call; an id, name or argument change never is. A malformed entry
        on either side is refused rather than passed.
        """
        def project(calls, resolve):
            output = []
            for call in calls or ():
                need(isinstance(call, dict), "malformed_tool_call_entry")
                function = call.get("function") or {}
                call_id = call.get("id")
                need(isinstance(call_id, str) and call_id, "missing_tool_call_id")
                name = function.get("name")
                need(isinstance(name, str) and name, "missing_tool_call_name")
                arguments = function.get("arguments")
                need(isinstance(arguments, str), "missing_tool_call_arguments")
                if resolve is not None:
                    call_id = resolve(call_id)
                output.append({"id": call_id, "name": name, "arguments": arguments})
            return output

        need(len(observed or ()) == len(expected or ()), "tool_call_count_changed")
        if self.multicall is None:
            # The sealed protocol, unchanged: the OBSERVED history is already the wire
            # form the pinned 29-character rule writes, so only the EXPECTED (native) side
            # is projected - and the collision check that guards that projection stands.
            return project(observed, None) == project(expected, self.wire_tool_call_id)
        # The declared compatibility protocol: BOTH sides resolve to the native id every
        # wire form is a one-to-one image of, so a truncated history entry and a full one
        # compare equal while any other id still differs.
        return (project(observed, self.resolve_history_tool_call_id)
                == project(expected, self.resolve_history_tool_call_id))

    # -- loading -----------------------------------------------------------
    def load(self):
        report = self.report
        self.plan = json.loads(read_stable(self.run_dir / "plan.json", report))
        self.result = json.loads(read_stable(self.run_dir / "result.json", report))
        self.events = rows(self.run_dir / "events.jsonl", report)
        self.http = rows(self.run_dir / "letta-http.jsonl", report)
        self.proxy = rows(self.proxy_journal, report)
        for extra in (self.config_path, self.dataset_path):
            if extra is not None:
                need(extra.is_file(), "declared_provenance_input_missing")
                report["sources"][str(extra)] = {
                    "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
                    "bytes": extra.stat().st_size}
        report["sources"]["proxy_journal"] = {
            "sha256": hashlib.sha256(self.proxy_journal.read_bytes()).hexdigest(),
            "bytes": self.proxy_journal.stat().st_size}
        self.report["scope"]["config_file_sha256"] = (
            hashlib.sha256(self.config_path.read_bytes()).hexdigest()
            if self.config_path else None)
        # The audited task set is the run's OWN declared scope, never a constant
        # assumed by the auditor.
        declared = (self.plan.get("scope") or {}).get("task_numbers")
        need(isinstance(declared, list) and declared
             and declared == list(range(declared[0], declared[0] + len(declared)))
             and declared[0] == START_TURN and declared[-1] in (5, END_TURN),
             "plan_task_numbers_not_a_verified_continuous_scope")
        self.task_numbers = tuple(declared)
        self.report["scope"]["task_numbers"] = list(self.task_numbers)
        self.report["scope"]["end_turn"] = self.task_numbers[-1]
        self.report["scope"]["phase_count"] = len(self.task_numbers) * len(ARMS)
        plan_scope = self.plan.get("scope") or {}
        self.report["scope"]["dataset_history_sent"] = bool(plan_scope.get("dataset_history_sent"))
        self.report["scope"]["memory_update_tool"] = bool(plan_scope.get("memory_update_tool"))
        # The limits every later gate compares against. They START as the sealed protocol's
        # pinned values and are replaced, member by member, by the ones the DECLARED 0.4
        # protocol pins in its own module - so a run is never judged against another
        # protocol's constants, and the sealed protocols keep exactly their old table.
        self.limits = dict(PINNED)
        # The 0.4 protocol carries each stage's own clock in that stage's user envelope.
        # The declared addition and the per-arm/per-task clarification it must equal are
        # resolved ONCE here, from the run's own schema and events - a sealed run gets an
        # empty set and no mapping, so its envelope rule is exactly what it always was.
        self.clarified_envelope_fields = frozenset()
        self.stage_clarifications = {}
        if (self.plan.get("config") or {}).get("schema_version") in MULTITURN_DEEPSEEK_SCHEMAS:
            self.clarified_envelope_fields = frozenset(CLARIFIED_ENVELOPE_FIELDS)
            for event in self.events:
                if event.get("kind") != "stage_clarification":
                    continue
                key = (event.get("arm"), event.get("task"))
                need(key not in self.stage_clarifications,
                     "duplicate_stage_clarification_for_one_phase")
                self.stage_clarifications[key] = event.get("clarification") or {}
            need(self.stage_clarifications, "stage_clarifications_missing")
        self._checked("load")

    def vita_environment_gate(self):
        """The RUN'S OWN Vita model configuration, established BEFORE Vita is imported.

        The pinned `vita.config` reads `VITA_MODEL_CONFIG_PATH` while it is imported; with
        the variable unset it falls back to the checkout's own `models.yaml`, and then to
        `models.yaml.example` - a configuration THIS run never declared (the sealed run
        was refused by exactly that fallback). An audit that imported the pinned package
        that way would re-derive the scoring chain against another model's declaration, so
        this gate runs before any `vita.*` import and requires, fail-closed:

        * the declared config IS this run's own `<run>/vita-models.json`, not another file;
        * its CONTENT equals the run's declaration, re-derived from the plan's own config
          (`expected_model`, the loopback `model_origin` and the declared transport's
          required request fields) - so an arbitrary external config cannot be supplied;
        * this process is pointed at that file: a caller's leftover
          `VITA_MODEL_CONFIG_PATH` is recorded and OVERRIDDEN, because a leftover value is
          not a declaration this run made;
        * the pinned Vita package is not ALREADY imported under ANOTHER configuration: an
          imported package keeps the configuration it was built with, so a package whose
          loaded `models` differ from this run's declaration is refused rather than reused.
        """
        report = self.report
        path = self.vita_model_config.resolve()
        need(path.is_file(), "declared_vita_model_config_missing")
        need(path.parent == self.run_dir.resolve()
             and path.name == VITA_MODEL_CONFIG_NAME,
             "vita_model_config_is_not_this_runs_own_file")
        raw = path.read_bytes()
        try:
            document = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise AuditFailure("vita_model_config_not_json") from None
        from ae_vita import native_model_config
        plan_config = self.plan.get("config")
        need(isinstance(plan_config, dict), "plan_config_missing")
        expected = native_model_config(plan_config)
        expected_models = self._expected_vita_models(plan_config)
        need(document == expected, "vita_model_config_content_differs_from_the_run_declaration")
        leftover = os.environ.get("VITA_MODEL_CONFIG_PATH")
        overridden = None
        if leftover and Path(leftover).resolve() != path:
            overridden = str(Path(leftover).resolve())
        os.environ["VITA_MODEL_CONFIG_PATH"] = str(path)
        loaded = sys.modules.get("vita.config")
        loaded_from = None
        if loaded is not None:
            loaded_from = str(Path(getattr(loaded, "_models_yaml_path", "")).resolve())
            # The configuration IN FORCE is what the package was built with: it must be
            # this run's declaration, whatever file it was read from, and that file must
            # still exist so the record can be re-checked.
            need(getattr(loaded, "models", None) == expected_models,
                 "vita_package_already_imported_with_another_configuration")
            need(Path(loaded_from).is_file(), "loaded_vita_config_file_missing")
            self._vita_config_paths.add(Path(loaded_from))
        report["vita_model_config"] = {
            "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "expected_from": "the run's own plan config (expected_model, model_origin, "
                             "declared transport request fields)",
            "content_matches_the_run_declaration": True,
            "environment_variable": "VITA_MODEL_CONFIG_PATH",
            "caller_environment_value_overridden": overridden,
            "pinned_package_already_loaded": loaded is not None,
            "pinned_package_configuration": loaded_from,
            "in_force_configuration_matches_the_declaration": True,
        }
        self._checked("vita_environment_gate")

    @staticmethod
    def _expected_vita_models(plan_config):
        """The pinned `vita.config.models` mapping this run's declaration implies."""
        from ae_vita import native_model_config
        entry = native_model_config(plan_config)["models"][0]
        return {"default": {}, entry["name"]: {key: value for key, value in entry.items()
                                               if key != "name"}}

    def _verify_loaded_vita_config(self):
        """After the pinned package is imported: it really read THIS run's declaration.

        `_load_native` performs the same comparisons for the writer it uses as the audit
        performs here for the pinned source it imports: the package was configured from a
        file this audit accepted, that file still exists, and the configuration in force
        IS the run's declaration. The environment variable is recorded rather than
        re-asserted: it is process-global, and what decides the derivation is the
        configuration the imported package actually carries.
        """
        if self.report.get("vita_model_config") is None:
            # This derivation was reached WITHOUT the gate (an internal probe over the
            # pinned modules). Establish the same declaration here instead of skipping it:
            # the configuration the package was imported with is not optional evidence.
            self.vita_environment_gate()
        config_module = sys.modules.get("vita.config")
        need(config_module is not None, "pinned_vita_config_module_not_loaded")
        expected = self._expected_vita_models(self.plan.get("config") or {})
        self.report["vita_model_config"]["environment_at_import_time"] = os.environ.get(
            "VITA_MODEL_CONFIG_PATH")
        loaded_from = Path(getattr(config_module, "_models_yaml_path", "")).resolve()
        need(loaded_from in self._vita_config_paths, "pinned_vita_config_is_another_file")
        need(loaded_from.is_file(), "pinned_vita_config_file_missing")
        need(getattr(config_module, "models", None) == expected,
             "pinned_vita_config_content_differs")

    # -- gates -------------------------------------------------------------
    def transport_gate(self):
        """The shared transport walk, plus one bounded reading of a refused request.

        A capacity refusal happens AFTER the request is built and BEFORE anything is
        sent: the proxy records a `blocked` row and performs no upstream call. The
        shared walk's "no completed capture" verdict is still what it is, so this
        override keeps it for every other case and treats ONLY a journal whose last
        record is exactly that refusal as a legitimate partial capture - so the run is
        still refused, but the capacity gate below can report the evidence it holds.
        """
        refusal = [row for row in self.proxy
                   if row.get("kind") == "blocked"
                   and str(row.get("code", "")).startswith("capacity_")]
        self.report["capacity_refusal_before_send"] = (
            deepcopy(refusal[-1]) if refusal else None)
        try:
            super().transport_gate()
        except AuditFailure:
            # The ONLY tolerated incomplete capture: the run's own declared capacity
            # gate refused the next request before anything was sent, and the proxy
            # sealed the journal with exactly that refusal as its last record. The
            # report stays INVALID (see `verify`), so this is a reading of evidence,
            # never a pass.
            # The proxy seals its journal with `cloud_close` after the refusal, so the
            # tolerated tail is exactly: the refusal, optionally followed by that seal.
            tail = [row for row in self.proxy if row.get("kind") != "pace_wait"][-2:]
            need(refusal and tail and tail[0].get("kind") == "blocked"
                 and tail[0].get("code") == refusal[-1].get("code")
                 and all(row.get("kind") in ("blocked", "cloud_close") for row in tail),
                 "transport_capture_check_failed")
            self.report["transport"]["capacity_refusal_only"] = True
            self.report["transport_capture_checked"] = False
            return
        self.report["transport"]["capacity_refusal_only"] = False

    def config_gate(self):
        opened = [r for r in self.proxy if r.get("kind") == "cloud_open"]
        need(len(opened) == 1, "wrong_cloud_open_count")
        from ae_cloud_proxy import CloudConfig, PROFILES, profile_of
        need(opened[0].get("profile") in PROFILES, "wrong_cloud_profile")
        config = CloudConfig(**opened[0]["config"])
        config.validate()
        declared_transport = profile_of(config)
        need(declared_transport.profile == opened[0].get("profile"),
             "profile_config_mismatch")
        if (self.plan.get("config") or {}).get("schema_version") in MULTITURN_DEEPSEEK_SCHEMAS:
            # The 0.4 protocol has TWO reviewed whole-pair budgets (256 and 1024) and the
            # candidate selects one explicitly; the budget gate below then requires the
            # plan, the proxy that counted and the capture itself to agree on that value.
            need(config.max_requests in _deepseek_module().ACCEPTED_REQUEST_BUDGETS,
                 "request_budget_not_a_reviewed_value")
        else:
            need(config.max_requests <= PAIR_MAX_REQUESTS, "safety_cap_exceeded")
        self.cloud_config = config
        self.declared_transport = declared_transport
        plan_config = self.plan.get("config")
        need(isinstance(plan_config, dict), "plan_config_missing")
        self._declare_run_contract(plan_config, config, declared_transport)
        # The service-identity half: the reviewed multicall manifest and the stacked
        # service receipt the plan names. These are files of the recording machine.
        self._resolve_declared_service_evidence(plan_config)
        if self.config_path is not None:
            declared = json.loads(self.config_path.read_text(encoding="utf-8"))
            need(declared == plan_config, "declared_config_not_the_run_config")
        # The pinned Vita source the native wrapper used is re-derived from the
        # run record; the multi-window windowing/formatting check calls that same
        # fixed source instead of re-implementing the scoring rule.
        declared_source = (self.plan.get("provenance") or {}).get("vita_source")
        need(isinstance(declared_source, dict) and declared_source.get("path"),
             "vita_source_not_recorded")
        source = Path(declared_source["path"])
        need(source.is_dir(), "declared_vita_source_missing")
        registry = source / "src/vita/registry.py"
        need(registry.is_file(), "declared_vita_source_not_a_checkout")
        need(hashlib.sha256(registry.read_bytes()).hexdigest()
             == declared_source.get("registry_sha256"),
             "declared_vita_source_bytes_changed")
        self.pinned_source = source
        self.report["transport_config"] = {
            "profile": config.profile, "max_output_tokens": config.max_output_tokens,
            "max_request_bytes": config.max_request_bytes,
            "max_response_bytes": config.max_response_bytes,
            "max_requests": config.max_requests, "io_timeout_seconds": config.io_timeout_seconds,
            "pace_seconds": config.pace_seconds,
        }
        self._checked("config_gate")

    def _declare_run_contract(self, plan_config, config, declared_transport):
        """The run's declared protocol, limits and multicall policy - from ITS OWN bytes.

        This is the path-INDEPENDENT half of `config_gate`: the plan/result agreement, the
        schema, the capacity contract, EVERY pinned limit as its own protocol declares it,
        and the multicall policy. It reads nothing from the filesystem, so the same
        derivation is what a local replay of a sealed capture uses for the gates after it
        (the pinned checkout, the service manifests and the run's own absolute paths are
        the parts that only exist on the machine that recorded the run).
        """
        need(plan_config == self.result.get("config"), "plan_and_result_config_differ")
        need(plan_config.get("schema_version") in (MULTITURN_SCHEMA, MULTITURN_SCHEMA_MULTICALL,
                                                   MULTITURN_SCHEMA_CAPACITY)
             + MULTITURN_DEEPSEEK_SCHEMAS,
             "wrong_multiturn_schema")
        need(plan_config.get("purpose") == PURPOSE, "wrong_purpose")
        # The internal alias and the transport are whatever the run declared; the
        # transport's WIRE model (which may differ from the alias) is a separate,
        # explicitly recorded mapping.
        need(plan_config.get("expected_model") == config.model, "wrong_expected_model")
        need(plan_config.get("transport_profile") == config.profile, "wrong_transport_profile")
        need(plan_config.get("upstream_model", declared_transport.wire_model)
             == declared_transport.wire_model, "wire_model_mapping_changed")
        need(plan_config.get("provider_slug", declared_transport.provider_slug)
             == declared_transport.provider_slug, "provider_route_changed")
        capacity = plan_config.get("capacity")
        if capacity is None:
            need(plan_config.get("context_window_source")
                 == "local_agent_budget_not_server_measurement",
                 "context_budget_provenance_missing")
        elif plan_config.get("schema_version") in MULTITURN_DEEPSEEK_SCHEMAS:
            # The 0.4 provider has NO verified tokenizer, so its contract is re-validated
            # from the plan's own bytes by the provider module rather than by the 0.3
            # capacity validator - which would (correctly) refuse a tokenless candidate.
            deepseek = _deepseek_module()
            need(plan_config.get("exploratory_capacity_protocol")
                 == deepseek.NON_GUARANTEE_PROTOCOL,
                 "deepseek_exploratory_protocol_missing")
            need(plan_config.get("context_window_source") in WINDOW_SOURCES,
                 "context_budget_provenance_missing")
            try:
                deepseek.validate_deepseek_capacity(capacity, plan_config)
                # The 0.4 protocol pins its OWN run limits: the members whose sealed 0.3
                # values do not apply (a larger block budget and round budget) are
                # validated in the provider module and adopted here, so every later gate
                # compares this run against THIS protocol's declared numbers.
                self.limits.update(
                    deepseek.validate_deepseek_run_limits(plan_config)["limits"])
                self.limits["context_window"] = capacity["context_window"]
            except Exception as exc:  # noqa: BLE001 - any refusal is an audit failure
                raise AuditFailure("declared_capacity_contract_invalid") from exc
        else:
            # The 0.3 candidate is the only path that may move the window, and it must
            # carry its whole contract: re-validated here from the plan's own bytes.
            need(plan_config.get("schema_version") == MULTITURN_SCHEMA_CAPACITY,
                 "capacity_block_on_a_sealed_schema")
            need(plan_config.get("context_window_source") in WINDOW_SOURCES,
                 "context_budget_provenance_missing")
            try:
                validate_capacity(capacity, plan_config)
            except ValueError as exc:
                raise AuditFailure("declared_capacity_contract_invalid") from exc
        need(plan_config.get("start_turn") == START_TURN, "multiturn_start_changed")
        need(plan_config.get("end_turn") == (self.plan.get("scope") or {}).get("end_turn"),
             "multiturn_end_changed")
        need(plan_config.get("end_turn") == self.task_numbers[-1], "multiturn_scope_disagrees")
        need(plan_config.get("arms") == list(ARMS), "multiturn_arm_order_changed")
        pacing = plan_config.get("pacing")
        pacing_ok = pacing == PACING_CANDIDATE
        if (not pacing_ok
                and plan_config.get("schema_version") in MULTITURN_DEEPSEEK_SCHEMAS):
            # The 0.4 protocol replaces the sealed 65 s combination with its own SERIAL
            # candidate (no fixed sleep, same one-at-a-time discipline). The provider
            # module validates it - including that the timeouts and the whole-pair request
            # budget did NOT move - and the validated declaration is what the checks below
            # compare against, instead of another protocol's constant.
            try:
                pacing = _deepseek_module().validate_serial_pacing(plan_config)["declared"]
            except Exception as exc:  # noqa: BLE001 - any refusal is an audit failure
                raise AuditFailure("declared_pacing_contract_invalid") from exc
            pacing_ok = True
        for key, value in PINNED.items():
            if key == "timeout_seconds" and pacing_ok:
                need(plan_config.get(key) == pacing["driver_io_timeout_seconds"],
                     "pacing_driver_timeout_changed")
                continue
            if key == "context_window" and capacity is not None:
                # The window is the one pinned value the capacity contract may move,
                # and only through the block validated above; every derived limit is
                # re-checked against it there and in `capacity_gate`.
                continue
            if self.limits[key] != PINNED[key]:
                # A declared protocol replaced this member with its own pinned value,
                # validated in its provider module above; the value compared here is
                # that protocol's, not the sealed one's.
                need(plan_config.get(key) == self.limits[key], "pinned_limit_changed_" + key)
                continue
            need(plan_config.get(key) == value, "pinned_limit_changed_" + key)
        if pacing_ok:
            need(config.io_timeout_seconds == pacing["proxy_upstream_io_timeout_seconds"],
                 "pacing_proxy_timeout_changed")
            need(config.pace_seconds == pacing["min_interval_seconds"],
                 "pacing_interval_changed")
        else:
            need(not config.pace_seconds, "unexpected_pacing_interval")
        need(config.max_output_tokens == plan_config["auxiliary_output_tokens"],
             "transport_output_ceiling_not_auxiliary_limit")
        need(config.max_request_bytes == plan_config["max_request_bytes"],
             "transport_request_bytes_differ")
        need(config.max_response_bytes == plan_config["max_response_bytes"],
             "transport_response_bytes_differ")
        if plan_config.get("schema_version") in ((MULTITURN_SCHEMA_MULTICALL,
                                                 MULTITURN_SCHEMA_CAPACITY)
                                                + MULTITURN_DEEPSEEK_SCHEMAS):
            try:
                policy = resolve_policy(plan_config, allow_absent=False)
            except Exception:
                self.fail("declared_multicall_profile_invalid")
            self.multicall = policy.as_dict()
            validate_policy(self.multicall)
            self.report["scope"]["multicall_profile"] = deepcopy(self.multicall)

    def _resolve_declared_service_evidence(self, plan_config):
        """The multicall manifest + the stacked service receipt the plan declares.

        Both are resolved from the run record's own provenance: the manifest path is
        compared byte for byte with the digest the record pinned, and the stack receipt is
        validated against that manifest. This is the part of the contract that names files
        outside the run directory, so it is kept separate from the declaration above.
        """
        if plan_config.get("schema_version") in ((MULTITURN_SCHEMA_MULTICALL,
                                                 MULTITURN_SCHEMA_CAPACITY)
                                                + MULTITURN_DEEPSEEK_SCHEMAS):
            provenance = self.plan.get("provenance") or {}
            declared_manifest = provenance.get("multicall_manifest")
            need(isinstance(declared_manifest, dict), "multicall_manifest_not_recorded")
            manifest_path = Path(declared_manifest.get("path") or "")
            need(manifest_path.is_file(), "multicall_manifest_missing")
            need(hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                 == declared_manifest.get("sha256"), "multicall_manifest_bytes_changed")
            self.manifest = read_manifest(manifest_path)
            need((self.manifest.get("verification") or {}).get("offline_patch_verified") is True,
                 "multicall_manifest_not_offline_verified")
            self.report["scope"]["multicall_manifest"] = {
                "path": str(manifest_path), "sha256": declared_manifest["sha256"],
                "offline_patch_verified": True}
            # The 0.3 service identity is verified FIRST: when the run carries a valid
            # STACK receipt, that receipt is the service evidence for the multicall
            # files too (the stack manifest lists them with the reviewed digests), so
            # the completed run does not also need a multicall-only receipt - which
            # describes a different service tree.
            stack_verified = False
            if plan_config.get("schema_version") in ((MULTITURN_SCHEMA_CAPACITY,)
                                                     + MULTITURN_DEEPSEEK_SCHEMAS):
                stack_verified = self.stack_evidence_gate(provenance)
            receipt = provenance.get("multicall_live_loading")
            if receipt is not None:
                need(isinstance(receipt, dict), "multicall_receipt_record_invalid")
                receipt_path = Path(receipt.get("path") or "")
                need(receipt_path.is_file(), "multicall_receipt_missing")
                need(hashlib.sha256(receipt_path.read_bytes()).hexdigest()
                     == receipt.get("sha256"), "multicall_receipt_bytes_changed")
                validate_launch_receipt(
                    json.loads(receipt_path.read_text(encoding="utf-8")),
                    self.manifest, declared_manifest["sha256"])
                self.report["scope"]["multicall_receipt_verified"] = True
            else:
                need(stack_verified
                     or self.result.get("status") != "RE_MULTITURN_COMPLETED_AUDIT_PENDING",
                     "executed_multicall_run_without_service_receipt")
                self.report["scope"]["multicall_receipt_verified"] = False
                if stack_verified:
                    self.report["scope"]["multicall_covered_by_stack_receipt"] = {
                        "stack_manifest": self.report["scope"]["patch_stack"]["path"],
                        "files": sorted(self.stack_manifest["patched_files"])}


    def stack_evidence_gate(self, provenance):
        """The 0.3 service identity: the STACKED manifest and the service's receipt.

        A capacity/no-compaction run is served by the stacked patch, so the evidence is
        this pair and nothing else. It is re-validated HERE from bytes - manifest
        structure and `new_files`, the manifest digest the receipt was written against,
        and `validate_stack_receipt` over the receipt's own paths and digests - rather
        than reading the driver's `patch_stack_loading_verified` flag as proof. The
        multicall receipt is a different artefact and cannot stand in for it.
        """
        record = provenance.get("patch_stack_manifest")
        protocol = provenance.get("patch_stack_protocol")
        # Returns True only when a full stack receipt was validated for this run; the
        # caller uses that (never the driver's flag) to decide whether the service
        # identity is accounted for.
        need(protocol in ("required", "declared", "absent"),
             "patch_stack_protocol_not_recorded")
        need(provenance.get("patch_stack_live_loading") is None
             or isinstance(provenance.get("patch_stack_live_loading"), dict),
             "stack_receipt_record_invalid")
        if record is None:
            need(protocol == "absent", "patch_stack_manifest_missing")
            need(provenance.get("patch_stack_loading_verified") is not True,
                 "stack_loading_verified_without_a_manifest")
            self.report["scope"]["patch_stack"] = {
                "declared": False, "protocol": protocol,
                "note": ("no stacked service evidence was declared for this run; a real "
                         "RUN of a capacity config refuses to start without it")}
            self._checked("stack_evidence_gate")
            return False
        need(isinstance(record, dict), "patch_stack_manifest_record_invalid")
        manifest_path = Path(record.get("path") or "")
        need(manifest_path.is_file(), "patch_stack_manifest_missing")
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        need(manifest_sha == record.get("sha256"), "patch_stack_manifest_bytes_changed")
        try:
            manifest = read_stack_manifest(manifest_path)
        except Exception:
            # The reviewer's validator refuses it; the audit reports that as a failure
            # of THIS gate instead of letting a validator exception escape.
            self.fail("patch_stack_manifest_unreadable")
        need((manifest.get("verification") or {}).get("offline_chain_verified") is True,
             "patch_stack_manifest_not_offline_verified")
        # The manifest must carry BOTH file sets, and they must be the ones the record
        # names: a run cannot describe a stack whose helpers it does not list.
        need(bool(manifest.get("patched_files")) and bool(manifest.get("new_files")),
             "patch_stack_manifest_files_missing")
        need(sorted(manifest["patched_files"]) == sorted(record.get("patched_files") or []),
             "patch_stack_patched_files_differ_from_the_record")
        need(sorted(manifest["new_files"]) == sorted(record.get("new_files") or []),
             "patch_stack_new_files_differ_from_the_record")
        self.stack_manifest = manifest
        self.report["scope"]["patch_stack"] = {
            "declared": True, "protocol": protocol, "path": str(manifest_path),
            "sha256": manifest_sha, "profile_version": manifest.get("profile_version"),
            "letta_checkout": manifest.get("letta_checkout"),
            "patched_files": sorted(manifest["patched_files"]),
            "new_files": sorted(manifest["new_files"]),
            "offline_chain_verified": True}
        # Published before the refusals below, so a run refused for a missing receipt
        # still reports that no stack receipt was verified.
        self.report["scope"]["patch_stack_receipt_verified"] = False
        receipt = provenance.get("patch_stack_live_loading")
        if receipt is None:
            need(protocol != "required", "required_stack_service_receipt_missing")
            need(self.result.get("status") != "RE_MULTITURN_COMPLETED_AUDIT_PENDING",
                 "executed_stack_run_without_service_receipt")
            self._checked("stack_evidence_gate")
            return False
        receipt_path = Path(receipt.get("path") or "")
        need(receipt_path.is_file(), "stack_receipt_missing")
        need(hashlib.sha256(receipt_path.read_bytes()).hexdigest() == receipt.get("sha256"),
             "stack_receipt_bytes_changed")
        try:
            validated = validate_stack_receipt(
                json.loads(receipt_path.read_text(encoding="utf-8")), manifest, manifest_sha)
        except Exception:
            # A receipt that does not describe this manifest/tree - an old multicall
            # receipt, a tampered `new_files` digest, a foreign checkout - is refused.
            self.fail("stack_receipt_validation_failed")
        # The receipt's own `new_files` are re-checked against the tree it names: the
        # validator hashes every one of them, and the audit states the set it saw.
        need(sorted(validated["new_files"]) == sorted(manifest["new_files"]),
             "stack_receipt_new_files_differ_from_the_manifest")
        self.report["scope"]["patch_stack_receipt_verified"] = True
        self.report["scope"]["patch_stack"]["receipt"] = {
            "path": str(receipt_path), "sha256": receipt["sha256"],
            "instance_id": validated.get("instance_id"),
            "checkout": validated.get("checkout"),
            "new_files": sorted(validated["new_files"])}
        self._checked("stack_evidence_gate")
        return True

    def auditor_identity(self):
        """The code that REALLY audits this capture, by path and by its own digest.

        Computed from the files that exist here, never from the run record, so a report
        cannot present the sealed run's hashes as the identity of the executing auditor.
        A listed file that is absent is recorded as absent instead of being skipped.
        """
        identity = {}
        for name in AUDITOR_IDENTITY_FILES:
            path = (self.root / name)
            identity[name] = {
                "path": str(path),
                "sha256": (hashlib.sha256(path.read_bytes()).hexdigest()
                           if path.is_file() else None),
                "present": path.is_file()}
        return identity

    def _sealed_audit_identity(self, plan_code, result_code):
        """Check the ONE post-hoc-auditor digest against its real sealed copy.

        Everything else in the run identity is still compared file by file, and this
        option cannot name files to ignore: it carries exactly one sealed copy, it must be
        the runtime copy of `SEALED_AUDIT_MODULE`, and its digest must equal BOTH records
        (which must also agree with each other). Nothing is imported from it.
        """
        plan_sha = plan_code.get(SEALED_AUDIT_MODULE)
        result_sha = result_code.get(SEALED_AUDIT_MODULE)
        need(plan_sha == result_sha, "plan_and_result_audit_module_digests_differ")
        need(self.sealed_audit_copy is not None, "sealed_audit_copy_not_declared")
        need(self.sealed_audit_copy.is_file(), "sealed_audit_copy_missing")
        copy_sha = hashlib.sha256(self.sealed_audit_copy.read_bytes()).hexdigest()
        need(copy_sha == plan_sha, "sealed_audit_copy_digest_mismatch")
        return {"mode": "sealed_run_audit_identity",
                "sealed_audit_copy": {"path": str(self.sealed_audit_copy),
                                      "sha256": copy_sha,
                                      "file": SEALED_AUDIT_MODULE},
                "recorded_at_run_sha256": plan_sha,
                "executed_sha256": hashlib.sha256(
                    (self.root / SEALED_AUDIT_MODULE).read_bytes()).hexdigest(),
                "note": ("the sealed copy identifies the audit module the RUN record pins; "
                         "the code that executed this audit is the file recorded under "
                         "auditor_identity, and its digest is never reported as the "
                         "recorded one")}

    def provenance_gate(self):
        report = self.report
        current = multiturn_code_files(self.root)
        plan_code = ((self.plan.get("provenance") or {}).get("code_sha256"))
        result_code = ((self.result.get("provenance") or {}).get("code_sha256"))
        # Published BEFORE any comparison, so even a refusal states which code audited this
        # capture and in which mode - a report never has to be trusted for that.
        report["auditor_identity"] = self.auditor_identity()
        report["code_sha256"] = current
        reaudit = None
        if self.sealed_audit_copy is not None:
            need(isinstance(plan_code, dict) and isinstance(result_code, dict),
                 "provenance_code_sha256_missing")
            reaudit = self._sealed_audit_identity(plan_code, result_code)
        report["reaudit"] = reaudit or {
            "mode": "strict",
            "sealed_audit_copy": None,
            "executed_sha256": hashlib.sha256(
                (self.root / SEALED_AUDIT_MODULE).read_bytes()).hexdigest(),
            "note": ("every run-identity file, including the post-hoc audit module, must "
                     "match the record exactly")}
        for label, value in (("plan", self.plan), ("result", self.result)):
            provenance = value.get("provenance")
            need(isinstance(provenance, dict), label + "_provenance_missing")
            code = provenance.get("code_sha256")
            need(isinstance(code, dict), label + "_provenance_code_sha256_missing")
            need(set(code) == set(current), label + "_provenance_code_file_set_changed")
            for name, sha in current.items():
                if reaudit is not None and name == SEALED_AUDIT_MODULE:
                    # The only file this option covers: its run-time identity was verified
                    # against the sealed copy above, so the RUN-side files below are still
                    # compared exactly and no arbitrary ignore list exists.
                    continue
                need(code[name] == sha, label + "_provenance_code_sha256_mismatch_"
                     + name.replace("/", "_"))
            need(provenance.get("live_letta_commit_verified") is False,
                 label + "_provenance_claims_letta_verification")
            need(provenance.get("project_git_commit") is None,
                 label + "_provenance_unexpected_git_commit")
            need(provenance.get("config_canonical_sha256")
                 == canonical_sha256(self.plan["config"]),
                 label + "_config_canonical_sha256_mismatch")
            if self.config_path is not None:
                need(provenance.get("config_file_sha256")
                     == hashlib.sha256(Path(self.config_path).read_bytes()).hexdigest(),
                     label + "_config_file_sha256_mismatch")
        self._checked("provenance_gate")

    # -- phase structure and per-phase boundaries --------------------------
    def _deepseek_request_budget(self, plan_config, journal):
        """ONE reviewed request budget, in the plan, in the proxy and in the count.

        The budget is the number that really stops a run, so it is never taken from a
        label: the value must be one of the protocol's two reviewed budgets, the PROXY that
        counted the requests must have served exactly the value the plan recorded, the
        plan's own budget record must carry it (and must not claim the historical baseline
        was kept when it was not), and the capture may never show more requests - or more
        upstream sends - than that budget allows. A budget stop is reported as a budget
        stop: exactly at the boundary, and never as a completed run.
        """
        report = self.report
        module = _deepseek_module()
        accepted = list(module.ACCEPTED_REQUEST_BUDGETS)
        declared = plan_config.get("max_requests")
        need(declared in accepted, "request_budget_not_a_reviewed_value")
        need(self.cloud_config.max_requests == declared, "proxy_and_plan_request_budgets_differ")
        planned = (self.plan.get("scope") or {}).get("pair_request_budget") or {}
        need(planned.get("max_requests") == declared, "plan_budget_record_differs")
        need(planned.get("budget_unchanged") is (declared == module.PAIR_REQUEST_BUDGET),
             "plan_claims_an_unchanged_budget_it_did_not_keep")
        client_requests = [row for row in journal if row.get("kind") == "client_request"]
        upstream = [row for row in journal if row.get("kind") == "upstream_request"]
        need(len(client_requests) <= declared, "capture_exceeds_the_declared_budget")
        need(len(upstream) <= declared, "upstream_sends_exceed_the_declared_budget")
        stops = [row for row in journal if row.get("kind") == "blocked"
                 and row.get("code") == "proxy_stopped_or_request_limit"]
        if stops:
            need(len(client_requests) == declared, "budget_stop_before_the_declared_boundary")
            need(self.result.get("status") != "RE_MULTITURN_COMPLETED_AUDIT_PENDING",
                 "completed_run_that_was_stopped_by_its_budget")
        report["request_budget"] = {
            "declared": declared, "accepted": accepted,
            "historical_baseline": module.PAIR_REQUEST_BUDGET,
            "budget_unchanged": declared == module.PAIR_REQUEST_BUDGET,
            "source": "the candidate's own max_requests field",
            "proxy_config_max_requests": self.cloud_config.max_requests,
            "client_requests_in_capture": len(client_requests),
            "upstream_requests_in_capture": len(upstream),
            "counting": ("the /models GET and every /chat/completions POST both count "
                         "against this one budget"),
            "stopped_by_budget": bool(stops),
            "generation_quota_claimed": False}
        self._checked("request_budget")

    def _deepseek_capacity_gate(self, capacity):
        """The 0.4 byte-gate protocol, verified against the run's OWN wire evidence.

        The records come from the same authoritative source the 0.3 gate uses: the PROXY
        JOURNAL's own `capacity_check` rows. The driver's attested copies must agree with
        them field by field, and every row must be a genuine BYTE record whose
        `input_sha256` is a real digest of the normalized request it belongs to - a
        nonempty hash string is not accepted as a binding.

        Not taken on the run's word: no token count, no tokenizer and no capacity
        guarantee may appear anywhere, and a completed run with no gate records is refused.
        The per-stage clarifications must be bound to the agent and task they were derived
        for, with the two arms clarified on the same stages.
        """
        report = self.report
        module = _deepseek_module()
        basis = module.NON_GUARANTEE_COUNT_BASIS
        need(capacity.get("count_basis") == [basis], "wrong_deepseek_count_basis")
        need(capacity.get("capacity_is_a_guarantee") is False,
             "deepseek_run_claims_a_capacity_guarantee")
        need(capacity.get("window_is_measured") is False,
             "deepseek_run_claims_a_measured_window")
        need(capacity.get("max_request_bytes") == module.BYTE_BUDGET,
             "deepseek_byte_budget_changed")
        # The gate is armed in the process that really SENDS, so the PROXY's own journal
        # is the evidence - exactly as for 0.3. A run whose proxy was never armed, or was
        # armed from a different declaration, cannot make the plan's declaration true.
        journal = self.transport or {}
        need(journal.get("capacity_armed") is True,
             "the_proxy_that_served_this_run_was_never_armed")
        need(journal.get("capacity_declaration_sha256")
             == canonical_sha256(self.plan.get("config")),
             "proxy_armed_from_a_different_declaration")
        arming = self.result.get("capacity_arming")
        need(isinstance(arming, dict), "capacity_arming_missing_from_the_run")
        need(arming.get("declaration_sha256") == canonical_sha256(self.plan.get("config")),
             "run_attests_a_different_declaration")
        need(arming.get("context_window") == capacity["context_window"],
             "run_attests_a_different_window")
        need(arming.get("agent_reserve_tokens") == capacity["output_reserve_tokens"],
             "run_attests_a_different_agent_reserve")
        need(arming.get("auxiliary_reserve_tokens") == capacity["auxiliary_reserve_tokens"],
             "run_attests_a_different_auxiliary_reserve")
        rows = [deepcopy(row) for row in (journal.get("capacity_checks") or [])]
        completed = self.result.get("status") == "RE_MULTITURN_COMPLETED_AUDIT_PENDING"
        if not rows:
            # Only a refused run may legitimately hold no gate record; a completed one
            # must show every request it sent.
            if completed:
                raise AuditFailure("deepseek_gate_records_missing")
            if not report.get("transport", {}).get("capacity_refusal_only"):
                raise AuditFailure("deepseek_gate_records_missing")
        attested = [deepcopy(row) for row in (self.result.get("capacity_checks") or [])]
        if attested:
            need(len(attested) == len(rows),
                 "driver_and_journal_capacity_check_counts_differ")
            for left, right in zip(attested, rows):
                need(all(left.get(key) == right.get(key) for key in
                         ("request_id", "role", "input_bytes", "request_byte_budget",
                          "capacity_basis", "count_source", "fits")),
                     "driver_and_journal_capacity_checks_differ")
        need(all(row.get("count_source") == basis for row in rows),
             "journal_and_run_count_bases_differ")

        checks, stops = [], []
        for row in rows:
            need(isinstance(row, dict), "capacity_check_not_an_object")
            need(row.get("capacity_basis") == basis, "gate_record_with_a_foreign_basis")
            need("input_tokens" not in row, "gate_record_carries_a_token_count")
            need(row.get("token_count_available") is False,
                 "gate_record_claims_a_token_count")
            need(row.get("operation_guard_not_a_capacity_guarantee") is True,
                 "gate_record_claims_a_guarantee")
            measured = row.get("input_bytes")
            need(type(measured) is int and measured > 0,
                 "gate_record_without_a_byte_measurement")
            need(row.get("request_byte_budget") == module.BYTE_BUDGET,
                 "gate_record_with_another_budget")
            digest = row.get("input_sha256")
            need(isinstance(digest, str) and len(digest) == 64
                 and all(character in "0123456789abcdef" for character in digest),
                 "gate_record_hash_is_not_a_sha256")
            need(row.get("fits") == (measured <= row["request_byte_budget"]),
                 "gate_record_boundary_differs_from_the_rule")
            checks.append(deepcopy(row))
            if row["fits"] is False:
                stops.append(deepcopy(row))
        if completed:
            need(checks, "deepseek_gate_records_missing")
            need(not stops, "completed_run_with_an_over_budget_request")
        elif stops or checks:
            # An incomplete run is how a budget stop looks; it must keep what completed.
            started = [event for event in self.events
                       if event.get("kind") == "stage_start"]
            need(0 < len(started) <= len(self.plan.get("phases") or []),
                 "budget_stop_without_a_started_phase")
            arms = self.result.get("arms") or {}
            need(any((arms.get(arm) or {}).get("post_tasks") for arm in ARMS),
                 "budget_stop_with_no_completed_evidence")

        # The two-arm clarification: bound, per stage, to the right agent and task.
        clarifications = [event for event in self.events
                          if event.get("kind") == "stage_clarification"]
        need(clarifications, "stage_clarifications_missing")
        seen = {}
        for event in clarifications:
            clar = event.get("clarification") or {}
            arm, task = event.get("arm"), event.get("task")
            need(arm in ARMS, "stage_clarification_without_a_known_arm")
            need(isinstance(task, str) and task, "stage_clarification_without_a_task")
            need(clar.get("subtask_id") == task,
                 "stage_clarification_bound_to_another_task")
            need(isinstance(clar.get("environment_now"), str) and clar["environment_now"],
                 "stage_clarification_without_a_clock")
            need(clar.get("host_wall_clock_used") is False,
                 "stage_clarification_used_the_host_clock")
            need(clar.get("hardcoded_value_used") is False,
                 "stage_clarification_used_a_hardcoded_time")
            need(clar.get("same_clock_the_tools_validate_with") is True,
                 "stage_clarification_clock_is_not_the_tool_clock")
            if clar.get("attributes_note") == "applied":
                need(isinstance(clar.get("attributes_clarification"), str)
                     and clar["attributes_clarification"],
                     "applied_attributes_note_without_its_text")
                need(clar.get("attributes_tool") == "create_delivery_order",
                     "attributes_note_on_another_tool")
            else:
                need(clar.get("attributes_note") == "not_applicable",
                     "stage_clarification_without_a_note_verdict")
            seen.setdefault(arm, set()).add(task)
        need(set(seen) == set(ARMS), "stage_clarification_missing_for_an_arm")
        task_sets = [frozenset(value) for value in seen.values()]
        need(len(set(task_sets)) == 1,
             "the two arms were not clarified on the same stages")
        report["capacity_policy"] = {
            "declared": True, "protocol": module.NON_GUARANTEE_PROTOCOL,
            "count_basis": [basis], "request_byte_budget": module.BYTE_BUDGET,
            "capacity_is_a_guarantee": False, "token_count_available": False,
            "context_window": capacity["context_window"], "window_is_measured": False,
            "checks": len(checks), "stops": deepcopy(stops),
            "checked_from": "the proxy journal's own capacity_check rows",
            "gate_record_basis": basis,
            "stages_clarified": {arm: sorted(tasks) for arm, tasks in seen.items()},
            "clarifications_bound_to_arm_and_task": True,
        }
        self._checked("capacity_gate")

    def capacity_gate(self):
        """The declared capacity/no-compaction contract, against the run's own records.

        The sealed 0.1/0.2 protocols declare none of this and are skipped exactly as
        before. A 0.3 run must show ALL of:

        * the plan and the result carrying the SAME capacity decision record;
        * EVERY arm's creation payload carrying the declared policy tag (not "one of
          the two", which is what a set union would prove);
        * the pre-send gate's own per-request records, one per provider request, each
          linked to the role it belongs to and to the request it counted;
        * a complete run whose gate records are missing or empty - refused;
        * a mid-run stop recorded as an incomplete run with its completed evidence.
        """
        report = self.report
        plan_config = self.plan.get("config") or {}
        capacity = plan_config.get("capacity")
        if capacity is None:
            report["capacity_policy"] = {"declared": False}
            self._checked("capacity_gate")
            return
        decision = self.plan.get("capacity_decision")
        need(isinstance(decision, dict) and decision.get("capacity_declared") is True,
             "capacity_decision_missing_from_the_plan")
        need(self.result.get("capacity_decision") == decision,
             "plan_and_result_capacity_decision_differ")
        if plan_config.get("schema_version") in MULTITURN_DEEPSEEK_SCHEMAS:
            self._deepseek_request_budget(plan_config, self.proxy)
            self._deepseek_capacity_gate(capacity)
            return
        if capacity["verification"] != "verified":
            need(decision.get("open_item"), "unverified_capacity_without_an_open_item")
            need(self.result.get("status") != "RE_MULTITURN_COMPLETED_AUDIT_PENDING",
                 "unverified_capacity_run_claims_completion")
        # Every ARM's own creation payload must carry the policy tag.
        tags_by_arm = {}
        for event in self.events:
            if event.get("kind") != "agent_created":
                continue
            payload = event.get("payload")
            need(isinstance(payload, dict), "agent_created_event_without_a_payload")
            tags = payload.get("tags")
            need(isinstance(tags, list) and tags, "agent_creation_tags_missing")
            for tag in tags:
                need(isinstance(tag, str) and tag, "agent_creation_tag_invalid")
            tags_by_arm[event.get("arm")] = sorted(tags)
        need(set(tags_by_arm) == set(ARMS), "agent_creation_missing_for_an_arm")
        for arm in ARMS:
            need(NO_COMPACTION_TAG in tags_by_arm[arm],
                 "no_compaction_tag_absent_for_" + arm)
            need(set(tags_by_arm[arm]) <= {NO_COMPACTION_TAG},
                 "undeclared_agent_tag_present_for_" + arm)
        # Published BEFORE the evidence checks below, so a run refused for missing or
        # mismatched gate evidence still reports the declaration it was judged against
        # (and a mid-run capacity stop still reports its own stop record).
        report["capacity_policy"] = {
            "declared": True, "context_window": capacity["context_window"],
            "window_source": capacity["window_source"],
            "verification": capacity["verification"],
            "endpoint_measured_tokens": capacity["service_limit"]["endpoint_measured_tokens"],
            "no_compaction": capacity["no_compaction"],
            "count_basis": list(capacity["count_basis"]),
            "agent_tags_by_arm": tags_by_arm,
            "checks": 0, "stops": [],
            "reserve_rule": {"agent_or_unknown": capacity["output_reserve_tokens"],
                             "auxiliary_roles": capacity["auxiliary_reserve_tokens"]},
        }
        # The gate is armed in the process that really SENDS, and the driver cannot
        # reach into that process: the proxy's own journal is the evidence. A run whose
        # proxy was never armed, or was armed from a different declaration, cannot make
        # the plan's declaration true, whatever the driver recorded on its side.
        journal = self.transport or {}
        need(journal.get("capacity_armed") is True,
             "the_proxy_that_served_this_run_was_never_armed")
        need(journal.get("capacity_declaration_sha256") == canonical_sha256(plan_config),
             "proxy_armed_from_a_different_declaration")
        # What the driver can attest about its own side, and nothing more.
        arming = self.result.get("capacity_arming")
        need(isinstance(arming, dict), "capacity_arming_missing_from_the_run")
        need(arming.get("declaration_sha256") == canonical_sha256(plan_config),
             "run_attests_a_different_declaration")
        need(arming.get("context_window") == capacity["context_window"],
             "run_attests_a_different_window")
        need(arming.get("agent_reserve_tokens") == capacity["output_reserve_tokens"],
             "run_attests_a_different_agent_reserve")
        need(arming.get("auxiliary_reserve_tokens") == capacity["auxiliary_reserve_tokens"],
             "run_attests_a_different_auxiliary_reserve")
        # The pre-send gate's own records, linked to the request each one counted. The
        # journal's rows are the authoritative per-request evidence; an in-process gate
        # also leaves the driver its own copies, and those must not disagree.
        rows = [deepcopy(row) for row in (journal.get("capacity_checks") or [])]
        need(rows, "the_proxy_journal_holds_no_capacity_check")
        attested = [deepcopy(row) for row in (self.result.get("capacity_checks") or [])]
        if attested:
            need(len(attested) == len(rows),
                 "driver_and_journal_capacity_check_counts_differ")
            for left, right in zip(attested, rows):
                need(all(left.get(key) == right.get(key) for key in
                         ("request_id", "role", "input_tokens", "output_reserve_tokens",
                          "context_window", "count_source", "fits")),
                     "driver_and_journal_capacity_checks_differ")
        need(all(row.get("count_source") == arming.get("count_source") for row in rows),
             "journal_and_run_count_bases_differ")
        checks, stops = [], []
        for row in rows:
            need(isinstance(row, dict), "capacity_check_not_an_object")
            need(row.get("role") in ("agent_or_unknown",) + AUXILIARY_ROLES,
                 "capacity_check_role_unrecognised")
            need(row.get("context_window") == capacity["context_window"],
                 "capacity_check_window_differs_from_the_declaration")
            reserve = capacity["output_reserve_tokens"] if row["role"] == "agent_or_unknown" \
                else capacity["auxiliary_reserve_tokens"]
            need(row.get("output_reserve_tokens") == reserve,
                 "capacity_check_reserve_is_not_this_role_own_reserve")
            counted = row.get("input_tokens")
            need(type(counted) is int and counted > 0,
                 "capacity_check_has_no_counted_input")
            need(isinstance(row.get("count_source"), str) and row["count_source"],
                 "capacity_check_has_no_count_source")
            need(row.get("fits") == (counted + reserve <= capacity["context_window"]),
                 "capacity_check_boundary_differs_from_the_rule")
            checks.append(deepcopy(row))
            if row["fits"] is False:
                stops.append(deepcopy(row))
        completed = self.result.get("status") == "RE_MULTITURN_COMPLETED_AUDIT_PENDING"
        need(checks, "capacity_checks_missing_from_the_run")
        if completed:
            need(not stops, "completed_run_with_an_over_capacity_request")
        else:
            # An incomplete run is fine (that is how a capacity stop looks), but it
            # must show the stop and keep the work that completed before it.
            need(stops, "incomplete_run_without_a_recorded_capacity_stop")
            started = [event for event in self.events if event.get("kind") == "stage_start"]
            need(0 < len(started) <= len(self.plan.get("phases") or []),
                 "capacity_stop_without_a_started_phase")
            arms = self.result.get("arms") or {}
            need(any((arms.get(arm) or {}).get("post_tasks") for arm in ARMS),
                 "capacity_stop_with_no_completed_evidence")
            need([(e.get("arm"), e.get("task")) for e in started]
                 == [(p["arm"], p["subtask_id"])
                     for p in (self.plan.get("phases") or [])][:len(started)],
                 "a_phase_outside_the_fixed_prefix_started_after_the_stop")
        report["capacity_policy"].update({
            "checks": len(checks), "stops": deepcopy(stops)})
        self._checked("capacity_gate")

    def phase_gate(self):
        """The fixed phase order, present exactly once each, in order.

        Also consumes each phase's recorded slice boundary, so every later gate
        attributes POSTs, tool calls, memory writes and native auxiliary calls to
        exactly one (arm, task) by an index the driver recorded, never by content.
        """
        report = self.report
        expected = [{"index": index, "arm": arm, "label": ARM_LABELS[arm], "task_number": number,
                     "subtask_id": f"sub_{USER_ID}_{number}"}
                    for index, (arm, number) in enumerate(
                        [(arm, number) for arm in ARMS for number in self.task_numbers])]
        need(self.plan.get("phases") == expected, "plan_phase_order_changed")
        need(self.plan["scope"].get("phase_count") == len(expected), "plan_phase_count_changed")
        stages = [e for e in self.events if e.get("kind") == "stage_start"]
        completes = [e for e in self.events if e.get("kind") == "arm_task_complete"]
        phases = [e for e in self.events if e.get("kind") == "phase_complete"]
        need(0 < len(stages) <= len(expected), "stage_start_count_out_of_range")
        partial = len(stages) < len(expected)
        report["scope"]["partial"] = partial
        keys = [(e.get("arm"), e.get("task")) for e in stages]
        need(len(set(keys)) == len(keys), "duplicate_phase_in_stage_start")
        fixed = [(p["arm"], p["subtask_id"]) for p in expected]
        need(keys == fixed[:len(keys)], "phase_order_not_the_fixed_prefix")
        need([event.get("phase_index") for event in stages] == list(range(len(stages))),
             "stage_start_phase_index_changed")
        need(len(completes) == len(stages), "stage_and_completion_counts_differ")
        need([(e.get("arm"), e.get("task")) for e in completes] == keys,
             "completion_order_changed")
        need(len(phases) == len(stages), "phase_complete_count_changed")
        need([(e.get("arm"), e.get("task")) for e in phases] == keys,
             "phase_complete_order_changed")
        order = [e.get("kind") for e in self.events
                 if e.get("kind") in ("stage_start", "arm_task_complete")]
        need(order == ["stage_start", "arm_task_complete"] * len(stages),
             "stage_lifecycle_interleaved")
        # Per-phase tool tables: exactly the tools the native environment offered,
        # identical across arms for the same task, and NEVER the previous
        # domain's binding.
        self.phase_records = {}
        for phase, event in enumerate(stages):
            arm, task_id = event["arm"], event["task"]
            tools = event.get("tools")
            need(isinstance(tools, list) and tools, "stage_start_tools_missing")
            schemas = tool_schemas(tools)
            for name in schemas:
                need(name != MEMORY_TOOL_NAME and "preference_memory" not in name,
                     "second_memory_backend_in_stage_tools")
            need(event.get("tool_names") == sorted(schemas), "stage_start_tool_names_changed")
            need(isinstance(event.get("boundary"), dict), "stage_start_boundary_missing")
            boundary = event["boundary"]
            need(set(boundary) == {"kind", "task", "phase_index", "post_index",
                                   "tool_call_index", "memory_write_index",
                                   "native_call_index", "trace_index"},
                 "stage_start_boundary_fields_changed")
            need(boundary["kind"] == "phase_start" and boundary["task"] == task_id
                 and boundary["phase_index"] == phase, "stage_start_boundary_identity_changed")
            record = (self.result["arms"].get(arm) or {}).get("tasks") or []
            local = int(task_id.rsplit("_", 1)[-1]) - self.task_numbers[0]
            need(0 <= local < len(record), "driver_phase_record_missing")
            task_record = record[local]
            need(task_record.get("subtask_id") == task_id, "driver_phase_record_identity_changed")
            need(task_record.get("boundary") == boundary, "driver_phase_boundary_differs")
            need(len(task_record.get("post_slice") or []) >= 1, "driver_post_slice_missing")
            need(isinstance(task_record.get("tool_call_slice"), list), "driver_tool_slice_missing")
            need(isinstance(task_record.get("native_calls_this_task"), list),
                 "driver_native_slice_missing")
            self.phase_records[(arm, task_id)] = {
                "event": event, "record": task_record, "tools": schemas,
                "index": phase, "arm": arm, "task": task_id}
        for number in self.task_numbers:
            task_id = f"sub_{USER_ID}_{number}"
            per_arm = [self.phase_records.get((arm, task_id)) for arm in ARMS]
            per_arm = [entry["tools"] for entry in per_arm if entry is not None]
            if len(per_arm) < 2:
                continue
            need(per_arm[0] == per_arm[1], "arms_offer_different_native_tools_at_" + task_id)
        report["scope"]["phases"] = [
            {"index": entry["index"], "arm": entry["arm"], "task": entry["task"],
             "native_tools": sorted(entry["tools"])}
            for entry in self.phase_records.values()]
        self._checked("phase_gate")

    # -- per-(arm, task) public material ----------------------------------
    def task_material_gate(self):
        """Each arm's own history is admitted once, per task, in the fixed order."""
        report = self.report
        plan_tasks = {task["subtask_id"]: task for task in self.plan["inputs"]["tasks"]}
        need(list(plan_tasks) == [f"sub_{USER_ID}_{n}" for n in self.task_numbers],
             "plan_task_set_changed")
        expected_refs = {number: [f"t{number}/history/{i}" for i in range(
            plan_tasks[f"sub_{USER_ID}_{number}"]["history_records"])]
            for number in self.task_numbers}
        self.history_admission = {}
        self.current_admission = {}
        for entry in self.phase_records.values():
            arm, task_id, number = entry["arm"], entry["task"], int(entry["task"].rsplit("_", 1)[-1])
            record = entry["record"]
            posts = record["post_slice"]
            # The first POST of a phase must be that task's own history batch.
            first = posts[0]
            need(first.get("task_number") == number, "phase_first_post_task_number_changed")
            body = self._post_body(first)
            envelopes = self._envelopes_of(body)
            need(len(envelopes) == 1 and envelopes[0]["source"] == HISTORY_SOURCE,
                 "phase_does_not_open_with_its_history_batch")
            material = envelopes[0]
            records = material.get("records")
            need(isinstance(records, list), "history_records_not_a_list")
            refs = [item.get("ref") if isinstance(item, dict) else None for item in records]
            need(refs == expected_refs[number], "history_batch_refs_changed")
            # The batch is that task's OWN original records, byte for byte.
            declared_records = plan_tasks[task_id].get("history_records")
            if declared_records is not None:
                need(len(records) == declared_records, "history_batch_size_changed")
            self.history_admission[(arm, number)] = {
                "signature": dumps(records), "request_id": first.get("request_id"),
                "refs": refs}
            # Each (arm, task) has its own current-task admission and reply indices.
            self.current_admission[(arm, task_id)] = False
        # No cross-arm or cross-task reuse: each key is distinct and each batch
        # was admitted by that arm's own phase.
        keys = [(arm, number) for (arm, number) in self.history_admission]
        need(len(keys) == len(set(keys)), "history_admission_key_duplicated")
        for arm in ARMS:
            admitted = sorted(number for (a, number) in self.history_admission if a == arm)
            done = sorted({int(entry["task"].rsplit("_", 1)[-1])
                           for entry in self.phase_records.values() if entry["arm"] == arm})
            need(admitted == done, "history_admission_incomplete_for_" + arm)
        report["scope"]["tasks"] = [
            {"number": number, "subtask_id": f"sub_{USER_ID}_{number}",
             "history_records": len(expected_refs[number]),
             "admitted_per_arm": sorted(arm for (arm, n) in self.history_admission
                                        if n == number)}
            for number in self.task_numbers]
        self._checked("task_material_gate")

    def _post_body(self, post):
        need(isinstance(post, dict), "driver_post_record_missing")
        path = post.get("path")
        need(isinstance(path, str) and MESSAGE_POST_PATH.fullmatch(path),
             "driver_post_path_changed")
        return post

    def _envelopes_of(self, post):
        """The public envelopes a driver-recorded POST actually submitted.

        The driver record is only a pointer to a request id; the CONTENT is taken
        from the Letta transport capture, so a driver that lies about what it sent
        cannot pass.
        """
        request_id = post.get("request_id")
        need(isinstance(request_id, str), "driver_post_request_id_missing")
        rows_ = [r for r in self.posts if r.get("request_id") == request_id]
        need(len(rows_) == 1, "driver_post_not_in_letta_capture")
        body = rows_[0].get("body") or {}
        envelopes = []
        for message in body.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            envelopes.append(_envelope_of(message.get("content"),
                                          self.clarified_envelope_fields))
        return envelopes

    # -- agent POST wire closure -------------------------------------------
    def agent_posts(self):
        """Consume every agent POST, in capture order, with its own task/phase.

        A POST is attributed to a phase by the driver boundary that owns its
        index, and the attribution is then confirmed against the actual HTTP
        agent path, so a phase identity is never taken from a declaration alone.
        """
        posts = [r for r in self.http if r.get("kind") == "request" and r.get("method") == "POST"
                 and MESSAGE_POST_PATH.fullmatch(r.get("path") or "")]
        need(posts, "no_agent_message_posts")
        need(len(posts) <= len(self.phase_records) * self.limits["max_stage_posts"],
             "stage_post_limit_exceeded")
        self.posts = sorted(posts, key=lambda item: utc(item["timestamp"]))
        # Assign every POST to exactly one phase, in the driver's own order.
        boundaries = []
        for entry in self.phase_records.values():
            record = entry["record"]
            boundaries.append((entry, record["boundary"]["post_index"],
                               len(record["post_slice"])))
        # The driver's own recorded slice boundaries must tile the capture exactly
        # and in order. Captures are compared in authoritative timestamp order,
        # so the boundaries are ordered by the driver's recorded index rather than
        # by dictionary insertion order.
        boundaries.sort(key=lambda item: item[1])
        need(sum(size for _, _, size in boundaries) == len(self.posts),
             "post_count_differs_from_phase_slices")
        self.post_task = {}
        self.post_request_id = {}
        # Each ARM's own slice boundaries must tile that arm's captured POST
        # stream exactly and in order. The two arms never interleave, and the
        # global capture order must be R's whole stream followed by E's.
        captured_by_arm = {arm: [p for p in self.posts
                                 if self._agent_of_path(p.get("path")) == arm]
                           for arm in ARMS}
        cursor = 0
        for arm in ARMS:
            boundaries = sorted(
                ((entry, entry["record"]["boundary"]["post_index"],
                  len(entry["record"]["post_slice"]))
                 for entry in self.phase_records.values() if entry["arm"] == arm),
                key=lambda item: item[1])
            need(boundaries, "phase_boundaries_missing_" + arm)
            arm_cursor = 0
            for entry, start, size in boundaries:
                need(start == arm_cursor, "arm_phase_post_slices_not_contiguous_" + arm)
                for post in captured_by_arm[arm][start:start + size]:
                    self.post_task[post["request_id"]] = entry
                arm_cursor += size
            need(arm_cursor == len(captured_by_arm[arm]),
                 "arm_post_count_differs_from_phase_slices_" + arm)
            # Bind each driver entry to one captured request id by the driver's own
            # per-arm ordinal, then require the captured order to agree exactly.
            driver_posts = [item for entry in sorted(
                (e for e in self.phase_records.values() if e["arm"] == arm),
                key=lambda e: e["index"]) for item in entry["record"]["post_slice"]]
            ordinals = [item.get("post_ordinal") for item in driver_posts]
            need(all(type(value) is int and value > 0 for value in ordinals),
                 "driver_post_ordinal_missing_" + arm)
            need(ordinals == list(range(1, len(ordinals) + 1)),
                 "driver_post_ordinals_not_contiguous_" + arm)
            for item, post in zip(driver_posts, captured_by_arm[arm]):
                self.post_request_id[(arm, item["post_ordinal"])] = post["request_id"]
                item["request_id"] = post["request_id"]
            # The global order must be this arm's stream after the previous arm's.
            need(self.posts[cursor:cursor + len(captured_by_arm[arm])]
                 == captured_by_arm[arm], "arm_post_streams_interleaved")
            cursor += len(captured_by_arm[arm])
        # The block each POST actually saw, from the capture's own order.
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
                self.block_at_post[record["request_id"]] = current[arm]
        need(len(self.block_at_post) == len(self.posts), "post_block_state_incomplete")
        self.report["cloud_calls"]["agent"] = len(self.posts)
        self._checked("agent_posts")

    def wire_gate(self):
        """Frame-by-frame closure of every agent provider call.

        For every agent POST, in capture order: the exact continuation from that
        arm's own verified prior turns, the pinned Letta system framing rendered
        with the block that POST actually saw, the declared client tool schemas,
        the declared new input, and the cloud request normalization. The raw
        provider assistant turn is carried into the next POST, so a changed or
        dropped assistant turn cannot pass. This replaces - it does not call -
        the single-t4 `check_agent_model_call`, whose declared-tool table is
        t4-only and whose turn state is a single global list.
        """
        report = self.report
        # The authoritative native id set is frozen BEFORE any comparison, so a wire id
        # can only ever resolve to an id this capture itself produced (and an ambiguous
        # projection is caught even when the colliding id appears later).
        self._freeze_provider_ids()
        # Per arm: the verified OpenAI history so far, for the CURRENT phase.
        turns = {arm: [] for arm in ARMS}
        pending = {arm: {} for arm in ARMS}
        known_ids = {arm: set() for arm in ARMS}
        # The shared `wire_tool_call_id` collision check reads this attribute, so
        # the per-arm authoritative id set is bound to the live object before any
        # wire comparison runs.
        self.known_tool_ids = {arm: known_ids[arm] for arm in ARMS}
        per_phase = {}
        for entry in sorted(self.phase_records.values(), key=lambda item: item["index"]):
            arm, task_id = entry["arm"], entry["task"]
            record = entry["record"]
            number = int(task_id.rsplit("_", 1)[-1])
            # A new task starts from THIS arm's own verified turns carried over
            # from its previous task; never the other arm's and never empty.
            per_phase[(arm, task_id)] = {"start_turns": deepcopy(turns[arm]),
                                          "phase": None}
            # Only the CURRENT task phase may offer the domain tools. The history
            # phase offers the memory tool alone, which is how the driver keeps
            # "organize memory" and "execute this task" separate.
            history_tools = {MEMORY_TOOL_NAME: MEMORY_TOOL}
            task_tools = dict(entry["tools"], **{MEMORY_TOOL_NAME: MEMORY_TOOL})
            for index, post in enumerate(record["post_slice"]):
                request_id = post.get("request_id")
                wire_post = self._letta_post(request_id)
                body = wire_post.get("body")
                need(isinstance(body, dict), "message_post_body_missing")
                need(set(body) == {"messages", "client_tools", "max_steps",
                                   "include_compaction_messages"},
                     "unexpected_message_post_fields")
                need(body.get("max_steps") == self.limits["max_steps"],
                     "message_post_max_steps_changed")
                need(body.get("include_compaction_messages") is True,
                     "compaction_messages_not_requested")
                # The declared new input, decoded from the ACTUAL submitted body,
                # and the tool profile its phase owns.
                new_input, ids, profile, source = self._declared_new_input(
                    wire_post, pending[arm])
                # The PHASE follows this arm's and task's own VERIFIED envelopes, never the
                # POST's position and never the tools that happen to be offered. A history
                # task can legitimately span several tool continuations - the live capture
                # answers two consecutive `memory_update` calls before `current_task`
                # appears - so only that explicit envelope switches to the task phase, and
                # a continuation (or a mid-task `runtime_user` reply) inherits the phase
                # already verified here. A continuation before any envelope is refused
                # rather than guessed.
                phase_state = per_phase[(arm, task_id)]
                if source == HISTORY_SOURCE:
                    need(phase_state["phase"] != "task",
                         "history_envelope_after_the_task_phase_started")
                    phase_state["phase"] = "history"
                elif source == CURRENT_SOURCE:
                    phase_state["phase"] = "task"
                need(phase_state["phase"] is not None, "continuation_without_a_verified_phase")
                profile = phase_state["phase"]
                declared_tools = history_tools if profile == "history" else task_tools
                actual_tools = tool_schemas(body.get("client_tools") or [])
                need(actual_tools == declared_tools, "client_tools_differ_from_declared")
                need(not any("preference_memory" in n for n in actual_tools),
                     "second_memory_backend_present")
                # The cloud provider call this POST produced, and its raw turn.
                call = self._cloud_call_of(wire_post)
                self._check_cloud_agent_call(call, declared_tools)
                cloud_body = call["body"]
                # The pinned Letta PromptGenerator framing, against the block this
                # exact call saw (so a stale or pre-emptive block render fails).
                self.agent_id = self._agent_id(arm)
                self.state = {
                    "block": {"label": BLOCK_LABEL,
                              "description": "带稳定 fact_id 的偏好；以之后成功更新为准。",
                              "limit": self.limits["block_char_limit"]},
                    "value": self.block_at_post[request_id]}
                self._check_arm_system_framing(arm, cloud_body)
                cloud_sent = cloud_body.get("messages")
                need(isinstance(cloud_sent, list) and cloud_sent, "cloud_call_without_messages")
                need(cloud_sent[0].get("role") == "system", "system_not_first")
                expected_history = deepcopy(turns[arm]) + deepcopy(new_input)
                need(len(cloud_sent) - 1 == len(expected_history),
                     "history_count_changed_or_extra_message")
                # Only THIS POST's new input is phase-bound; the earlier turns in
                # the same arm were already verified when they were submitted (a
                # task's own history batch stays in context for the rest of that
                # task, and that is correct).
                new_count = len(new_input)
                for index, (observed, item) in enumerate(zip(cloud_sent[1:], expected_history)):
                    need(observed.get("role") == item["role"], "history_order_or_role_changed")
                    if item["role"] == "user":
                        need(observed.get("content") == item["content"],
                             "user_history_content_changed")
                        if index >= len(expected_history) - new_count:
                            # The material the MODEL actually received must be the
                            # declared public envelope for THIS phase: the
                            # envelope's own task number, source and records are
                            # checked against the plan, so a re-labelled, emptied
                            # or exchanged history batch is refused here.
                            self._check_cloud_envelope(observed, item["content"], entry)
                    elif item["role"] == "assistant":
                        need(self.assistant_history_matches(observed, item),
                             "assistant_history_changed")
                    else:
                        self.check_submitted_tool_return(observed, item, source="history")
                turns[arm] = deepcopy(expected_history)
                raw_turn = self.raw_assistant_turn(call["response"])
                turns[arm].append(raw_turn)
                for tool_call in raw_turn.get("tool_calls") or []:
                    call_id = tool_call.get("id")
                    need(isinstance(call_id, str) and call_id,
                         "model_tool_call_id_missing")
                    need(call_id not in known_ids[arm], "repeated_model_tool_call_id")
                    known_ids[arm].add(call_id)
                    function = tool_call.get("function") or {}
                    need(function.get("name") in declared_tools,
                         "model_called_undeclared_tool")
                    arguments = function.get("arguments")
                    need(isinstance(arguments, str), "model_tool_arguments_not_text")
                    decoded = json.loads(arguments)
                    need(isinstance(decoded, dict), "model_tool_arguments_not_object")
                    pending[arm][call_id] = {"name": function["name"],
                                             "arguments": arguments}
                report["frames"].append({
                    "request_id": request_id, "arm": arm, "task": task_id,
                    "post_index_in_phase": index,
                    "history_length": len(expected_history),
                    "new_input_roles": [item["role"] for item in new_input],
                    "tool_call_ids": ids,
                    "declared_tool_count": len(declared_tools)})
            per_phase[(arm, task_id)]["end_turns"] = deepcopy(turns[arm])
            need(not pending[arm], "pending_tool_call_unanswered_at_phase_end")
        self.per_phase_turns = per_phase
        self.known_tool_ids = known_ids
        # Continuity: each arm's next task must start from EXACTLY the turns its
        # previous task left behind, and the first task of each arm must start
        # from an empty context. A reset, a truncation or a foreign prefix fails
        # here even if every single POST is internally consistent.
        for arm in ARMS:
            entries = sorted((entry for entry in self.phase_records.values()
                              if entry["arm"] == arm), key=lambda item: item["index"])
            previous = []
            for entry in entries:
                phase = self.per_phase_turns[(arm, entry["task"])]
                need(phase["start_turns"] == previous,
                     "arm_context_not_continued_from_its_previous_task")
                previous = phase["end_turns"]
        # What the id rule actually accepted, as evidence rather than as a claim.
        self.report["tool_call_id_forms"] = dict(self._id_forms)
        self.report["tool_call_id_rule"] = (
            "a history id must be one of this capture's own native tool-call ids, or the "
            "pinned 29-character projection of exactly one of them; anything else is refused")
        self._checked("wire_gate")

    def _check_cloud_envelope(self, observed, declared_content, entry):
        """One submitted user envelope must be the declared public material."""
        extra = self.clarified_envelope_fields
        envelope = _envelope_of(observed.get("content"), extra)
        declared = _envelope_of(declared_content, extra)
        need(envelope == declared, "cloud_user_envelope_differs_from_declared_input")
        number = int(entry["task"].rsplit("_", 1)[-1])
        source = envelope["source"]
        if source == HISTORY_SOURCE:
            need(envelope["task_number"] == number,
                 "cloud_history_batch_labelled_with_another_task")
            task = self.plan["inputs"]["tasks"][number - self.task_numbers[0]]
            need(envelope["records"] and len(envelope["records"]) == task["history_records"],
                 "cloud_history_batch_size_changed")
            need([item.get("ref") for item in envelope["records"]]
                 == [f"t{number}/history/{i}" for i in range(task["history_records"])],
                 "cloud_history_batch_refs_changed")
        elif source == CURRENT_SOURCE:
            task = self.plan["inputs"]["tasks"][number - self.task_numbers[0]]
            need(envelope["instruction"] == task["instruction"],
                 "cloud_current_instruction_changed")
            need(envelope["subtask_id"] == entry["task"],
                 "cloud_current_task_identity_changed")
            if extra:
                # The clock on the wire must be THIS stage's own declared clock: the
                # per-arm, per-task clarification the driver derived from this stage's
                # environment - not a global value, not another arm's, not the host's.
                clar = self.stage_clarifications.get((entry["arm"], entry["task"]))
                need(isinstance(clar, dict), "stage_clock_without_its_own_clarification")
                need(envelope.get("environment_now") == clar.get("environment_now"),
                     "wire_clock_differs_from_this_stage_clarification")
                need(envelope.get("environment_clock_format") == clar.get("clock_format"),
                     "wire_clock_format_differs_from_this_stage_clarification")
        else:
            # A runtime_user envelope on the wire must carry a text this phase's
            # real native simulator actually produced; the per-phase reply list is
            # assembled by `runtime_user_gate`, so only the ref is pinned here.
            match = REF_PATTERN.fullmatch(envelope.get("ref") or "")
            need(match is not None and int(match.group("turn")) == number,
                 "cloud_runtime_user_ref_changed")

    def _check_arm_system_framing(self, arm, cloud_body):
        """Run the shared framed-system check for THIS arm's declared payload.

        The shared `check_system_framing` reads `self.plan["agent_payload"]`,
        which is the single-t4 plan shape. A multi-turn plan carries one payload
        per arm, so the check is run unchanged against a temporary view that
        exposes the arm's own payload; the framing rules themselves are never
        re-implemented or relaxed.
        """
        payload = (self.plan.get("arms") or {}).get(arm, {}).get("agent_payload")
        need(isinstance(payload, dict) and isinstance(payload.get("system"), str),
             "arm_agent_payload_missing_" + arm)
        original = self.plan
        self.plan = {"agent_payload": payload}
        try:
            self.check_system_framing(cloud_body)
        finally:
            self.plan = original
        self._checked("system_framing_" + arm)

    def _letta_post(self, request_id):
        """The one Letta transport POST row this driver entry maps to.

        The mapping is established by `agent_posts` from the capture itself (the
        driver's per-arm POST ordinal against the capture order), so a driver
        entry that does not correspond to a real request cannot be resolved.
        """
        need(isinstance(request_id, str) and request_id, "driver_post_request_id_missing")
        rows_ = [r for r in self.posts if r.get("request_id") == request_id]
        need(len(rows_) == 1, "driver_post_not_in_letta_capture")
        return rows_[0]

    def _declared_new_input(self, wire_post, pending):
        """The declared new input of one POST, decoded from the submitted body.

        A POST adds either exactly one public user envelope, or a batch of tool
        returns for calls the model already made on this arm. Every return must
        answer a pending call of THIS arm, exactly once; a memory_update call is
        never allowed to reach an environment wire.

        The return value carries the envelope's own SOURCE alongside the tool profile:
        `history` and `current_task` are the two envelopes that DEFINE a phase, while a
        `runtime_user` reply and a tool-return continuation only inherit the phase that
        was already verified for this arm and task. The caller therefore never has to
        guess a phase from a POST's position or from the tools that happen to be offered.
        """
        body = wire_post["body"]
        messages = body.get("messages") or []
        returns = [m for m in messages if isinstance(m, dict)
                   and m.get("type") == "tool_return"]
        users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        need(bool(returns) != bool(users), "post_mixes_user_input_and_tool_returns")
        if users:
            need(len(users) == 1, "post_submits_multiple_user_messages")
            envelope = _envelope_of(users[0].get("content"),
                                    self.clarified_envelope_fields)
            source = envelope["source"]
            profile = "history" if source == HISTORY_SOURCE else "task"
            return ([{"role": "user", "content": users[0]["content"]}], [], profile, source)
        new_input, ids = [], []
        for message in returns:
            for returned in message.get("tool_returns") or []:
                tid = returned.get("tool_call_id")
                need(isinstance(tid, str) and tid, "tool_return_without_id")
                need(tid in pending, "tool_return_without_observed_model_call")
                executed = pending.pop(tid)
                # `memory_update` is handled by the bridge, not by the native
                # environment; a pending approval carrying it is a real update
                # path in either arm, and its result is checked against the
                # bridge's own record in `memory_gate`.
                self.check_submitted_tool_return(returned, {"returned": returned},
                                                 source="submission")
                new_input.append({"role": "tool", "returned": deepcopy(returned),
                                  "tool_call_id": tid})
                ids.append(tid)
        need(new_input, "tool_return_post_adds_nothing")
        # A tool-return continuation carries no envelope of its own: it keeps the phase
        # this arm and task already verified.
        return new_input, ids, "continuation", None

    def _bind_capture_roles(self):
        """Bind each cloud call's own captured dispatch role, in capture order.

        The proxy journals the role the caller declared for every chat request.
        The audit never invents a role: it reads the captured one and later
        requires it to equal the role of the native record the call answers.
        """
        roles = {r.get("request_id"): r.get("role")
                 for r in self.proxy if r.get("kind") == "client_request"}
        need(len(roles) == len([r for r in self.proxy
                                if r.get("kind") == "client_request"]),
             "duplicate_capture_request_id")
        self.capture_role = {}
        for call in self.chat_calls:
            role = roles.get(call["request_id"])
            need(isinstance(role, str) and role, "capture_role_missing")
            call["capture_role"] = role
            self.capture_role[call["request_id"]] = role
        need(len(self.capture_role) == len(self.chat_calls), "capture_role_incomplete")
        self._checked("capture_role_binding")

    def bind_cloud_calls(self):
        """Bind each agent POST to exactly one cloud call, never reused.

        The Letta transport and the cloud proxy mint their own request ids, so the
        binding is made the way the shared single-t4 audit makes it: the matching
        Letta `/messages` response is the next response frame after the POST, and
        the cloud call is the ONE cloud call that starts after that POST and ends
        before that response. A POST with zero or more than one cloud call fails,
        so an extra unconsumed model request cannot hide.
        """
        ordered = sorted(self.http, key=lambda row: row.get("sequence", 0))
        used = set()
        self.post_cloud_call = {}
        for post in self.posts:
            index = next((i for i, row in enumerate(ordered) if row is post), None)
            need(index is not None, "post_not_in_transport_capture")
            response = None
            for row in ordered[index + 1:]:
                if row.get("kind") == "response":
                    response = row
                    break
            need(isinstance(response, dict) and response.get("http_status") == 200,
                 "letta_post_without_successful_response")
            start, end = utc(post["timestamp"]), utc(response["timestamp"])
            # `chat_calls` start/end are already UTC ISO strings, so a lexical
            # comparison is the chronological one and needs no re-parsing.
            inside = [c for c in self.chat_calls
                      if start < c["start"] and c["end"] < end]
            need(len(inside) == 1, "letta_post_without_exactly_one_mapped_model_call")
            call = inside[0]
            need(call["request_id"] not in used, "cloud_call_mapped_twice")
            used.add(call["request_id"])
            self.post_cloud_call[post["request_id"]] = call
        need(len(self.post_cloud_call) == len(self.posts), "cloud_call_binding_incomplete")
        self._bound_cloud_call_count = len(used)
        self._checked("bind_cloud_calls")

    def _cloud_call_of(self, wire_post):
        request_id = wire_post.get("request_id")
        call = self.post_cloud_call.get(request_id)
        need(call is not None, "post_without_mapped_cloud_call")
        return call

    def _check_cloud_agent_call(self, call, declared_tools):
        """The cloud request/response is exactly the declared agent call."""
        body = call["body"]
        profile = profile_of(self.cloud_config)
        need(set(body) <= declared_cloud_fields(profile), "undeclared_cloud_request_field")
        missing = []
        check_declared_transport_fields(body, profile, missing)
        need(not missing, missing[0] if missing else "declared_transport_field_missing")
        need("seed" not in body and "chat_template_kwargs" not in body,
             "generation_seed_or_template_sent")
        need(body.get("temperature") == self.limits["temperature"], "cloud_temperature_changed")
        if "parallel_tool_calls" in body:
            need(body["parallel_tool_calls"] is False, "parallel_tool_calls_not_false")
        if "stream" in body:
            need(body["stream"] is False, "streaming_requested")
        if "n" in body:
            need(body["n"] == 1, "multiple_completions_requested")
        limit = body.get("max_tokens", body.get("max_completion_tokens"))
        need(limit == self.limits["max_output_tokens"], "agent_output_limit_not_2048")
        reasons = cloud_change_reasons(call["changes"], body, profile, limit)
        need(not reasons, reasons[0] if reasons else "unexpected_cloud_change")
        tools = body.get("tools")
        need(isinstance(tools, list) and tools, "agent_request_has_no_tools")
        need(tool_schemas(tools, True) == declared_tools,
             "upstream_tools_differ_from_declared")
        response = call["response"]
        need(isinstance(response, dict), "cloud_response_missing")
        choices = response.get("choices")
        need(isinstance(choices, list) and len(choices) == 1,
             "cloud_response_not_single_choice")

    def tool_mapping_gate(self):
        """Every executed tool call is closed against the real submission.

        For every tool the bridge really executed: the approval call it answered
        must be a call the MODEL made on this arm, its return must be the return
        the driver really submitted back to Letta, and the id/name/arguments must
        agree across the model reply, the approval, the execution and the
        submission. This is the closure that makes the tool table evidence
        rather than a declaration.
        """
        report = self.report
        # The model's own calls, by arm, from the verified wire pass.
        model_calls = {}
        for frame in report["frames"]:
            for call_id in frame.get("tool_call_ids") or []:
                model_calls.setdefault(frame["arm"], set()).add(call_id)
        for entry in sorted(self.phase_records.values(), key=lambda item: item["index"]):
            arm, task_id = entry["arm"], entry["task"]
            record = entry["record"]
            submitted = {}
            for post in record.get("post_slice") or []:
                body = self.posts and [r for r in self.posts
                                       if r.get("request_id") == post.get("request_id")]
                need(len(body) == 1, "post_not_in_letta_capture")
                for message in body[0].get("body", {}).get("messages") or []:
                    if not isinstance(message, dict) or message.get("type") != "tool_return":
                        continue
                    for returned in message.get("tool_returns") or []:
                        submitted[returned.get("tool_call_id")] = returned
            for item in record.get("tool_call_slice") or []:
                call_id, name = item.get("tool_call_id"), item.get("name")
                need(isinstance(call_id, str) and call_id, "executed_tool_call_id_missing")
                need(isinstance(name, str) and name, "executed_tool_name_missing")
                need(call_id in model_calls.get(arm, set()),
                     "executed_tool_was_not_a_declared_model_call")
                pending = item.get("pending") or {}
                need(pending.get("tool_call_id") == call_id, "approval_id_differs_from_execution")
                need(pending.get("name") == name, "approval_name_differs_from_execution")
                need(isinstance(pending.get("arguments"), str),
                     "approval_arguments_missing")
                need(call_id in submitted, "executed_tool_return_never_submitted")
                returned = submitted[call_id]
                need(returned.get("status") == item.get("status"),
                     "submitted_status_differs_from_execution")
                need(returned.get("tool_return") == (item.get("result") or {}).get("tool_return"),
                     "submitted_return_differs_from_execution")
                report["tool_mappings"].append({
                    "arm": arm, "task": task_id, "tool_call_id": call_id,
                    "name": name, "status": item.get("status")})
        need(report["tool_mappings"], "no_executed_tool_call_was_mapped")
        self._checked("tool_mapping_gate")

    # -- memory closure over the real wire ---------------------------------
    def memory_gate(self):
        """R's PATCHes and E's errata are closed against the real wire.

        R: every successful memory_update executes as a real tool call with a
        real return, produces exactly one PATCH, and that PATCH body equals the
        block rebuilt from the verified update ledger; the next POST of that arm
        renders the new block.
        E: never a PATCH, block byte-identical across the whole run, and each
        erratum reaches the wire as a real tool return with the correction text.
        """
        report = self.report
        self.patch_windows = {arm: [] for arm in ARMS}
        for record in sorted(self.http, key=lambda row: row.get("sequence", 0)):
            if not (record.get("kind") == "request" and record.get("method") == "PATCH"):
                continue
            arm = self._agent_of_path(record.get("path"))
            need(arm == "rewrite", "erratum_or_unknown_arm_patched_the_block")
            need((record.get("path") or "").endswith("/core-memory/blocks/" + BLOCK_LABEL),
                 "patch_target_changed")
            body = record.get("body")
            need(set(body or {}) == {"value"}, "unexpected_patch_fields")
            self.patch_windows[arm].append({"value": body["value"],
                                            "sequence": record.get("sequence")})
        for arm in ARMS:
            entries = [entry for entry in self.phase_records.values() if entry["arm"] == arm]
            entries.sort(key=lambda item: item["index"])
            writes, patches = [], []
            executed_updates = 0
            for entry in entries:
                record = entry["record"]
                writes.extend(record.get("memory_writes_this_task_expected") or [])
                slices = record.get("tool_call_slice") or []
                for item in slices:
                    if item.get("name") != MEMORY_TOOL_NAME:
                        continue
                    executed_updates += 1
                    need(item.get("status") in ("success", "error"),
                         "memory_update_status_missing")
                    need(isinstance(item.get("pending"), dict), "memory_update_call_missing")
                    need(item["pending"].get("tool_call_id") == item.get("tool_call_id"),
                         "memory_update_call_id_changed")
                    arguments = item["pending"].get("arguments")
                    need(isinstance(arguments, str), "memory_update_arguments_not_text")
                    submitted = json.loads(arguments)
                    need(isinstance(submitted, dict), "memory_update_arguments_not_object")
                    need(item.get("result", {}).get("status") == item.get("status"),
                         "memory_update_result_status_changed")
            # The verified update ledger must equal the executed memory_update
            # calls. The ledger stores the SUBMITTED arguments (the model's own
            # proposal) and the RESOLVED arguments (its stable id); the executed
            # approval call must equal the submitted proposal exactly, and its
            # resolved id must equal the ledger's resolved id.
            ledger = [(w["submitted"], w["resolved"]) for w in writes]
            executed = [(json.loads(item["pending"]["arguments"]), item)
                        for entry in entries
                        for item in (entry["record"].get("tool_call_slice") or [])
                        if item.get("name") == MEMORY_TOOL_NAME
                        and item.get("status") == "success"]
            need(len(executed) == len(ledger),
                 "executed_memory_updates_differ_from_verified_ledger")
            for (submitted, item), (ledger_submitted, resolved) in zip(executed, ledger):
                need(dumps(submitted) == dumps(ledger_submitted),
                     "memory_update_arguments_differ_from_verified_update")
                returned = json.loads(item.get("result", {}).get("tool_return")
                                      or "{}").get("update")
                need(isinstance(returned, dict), "memory_update_return_without_update")
                need(dumps(returned) == dumps(resolved),
                     "memory_update_resolved_id_differs_from_verified_update")
            executable_patches = self.patch_windows[arm]
            if arm == "rewrite":
                need(len(executable_patches) == len(writes),
                     "rewrite_patch_count_differs_from_verified_updates")
                expected = self.plan["arms"][arm]["initial_block"]
                for index, write in enumerate(writes):
                    block = self._apply_update(expected, write["resolved"],
                                               f"rewrite#{index}")
                    need(executable_patches[index]["value"] == block,
                         "rewrite_patch_body_differs_from_its_verified_update")
                    expected = block
                need(self.result["arms"][arm].get("final_block") == expected,
                     "rewrite_final_block_differs_from_its_verified_updates")
            else:
                need(not executable_patches, "erratum_patched_the_block")
                need(self.result["arms"][arm].get("initial_block")
                     == self.result["arms"][arm].get("final_block"),
                     "erratum_block_changed")
                # Each successful E update produced a real erratum tool return.
                for entry in entries:
                    for item in entry["record"].get("tool_call_slice") or []:
                        if item.get("name") != MEMORY_TOOL_NAME:
                            continue
                        if item.get("status") != "success":
                            continue
                        need("erratum" in (item.get("result", {}).get("tool_return") or ""),
                             "erratum_missing_from_successful_update_return")
                        report["erratum_mappings"].append({
                            "arm": arm, "task": entry["task"],
                            "tool_call_id": item.get("tool_call_id")})
            need(executed_updates == len(
                [i for entry in entries
                 for i in (entry["record"].get("tool_call_slice") or [])
                 if i.get("name") == MEMORY_TOOL_NAME]),
                "memory_update_tool_call_count_changed")
            report["memory_mappings"].append({"arm": arm, "updates": len(writes),
                                              "patches": len(executable_patches)})
        # PATCH order is monotone in the capture.
        sequences = [p["sequence"] for p in self.patch_windows["rewrite"]]
        need(sequences == sorted(sequences), "patch_order_changed")
        self._checked("memory_gate")

    @staticmethod
    def _apply_update(block_text, resolved, label):
        """Apply one verified update to a rendered block, exactly as the policy does."""
        try:
            facts = json.loads(block_text)
        except ValueError:
            raise AuditFailure("block_text_not_the_declared_fact_map") from None
        need(isinstance(facts, dict), "block_text_not_the_declared_fact_map")
        operation, fact_id = resolved["operation"], resolved["fact_id"]
        category, content = resolved["category"], resolved["content"]
        if operation == "add":
            need(fact_id and fact_id not in facts, "verified_add_id_not_the_policy_id_" + label)
            facts[fact_id] = {"category": category, "content": content}
        elif operation == "replace":
            need(fact_id in facts, "verified_replace_id_absent_" + label)
            need(facts[fact_id]["category"] == category,
                 "verified_replace_category_changed_" + label)
            facts[fact_id] = {"category": category, "content": content}
        elif operation == "delete":
            need(fact_id in facts, "verified_delete_id_absent_" + label)
            del facts[fact_id]
        else:
            raise AuditFailure("verified_update_operation_unknown_" + label)
        return json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    # -- runtime_user binding to the real native reply AND the real wire ----
    def runtime_user_gate(self):
        """Every runtime_user body is the real native reply and is on the wire."""
        for entry in self.phase_records.values():
            arm, task_id = entry["arm"], entry["task"]
            record = entry["record"]
            native = record.get("native_calls_this_task")
            replies = [call for call in native if call.get("role") == "user_simulator"]
            for call in replies:
                need(call.get("subtask_id") == task_id,
                     "native_user_reply_bound_to_another_task")
            derived = []
            for call in replies:
                response = call.get("response")
                need(isinstance(response, dict), "native_user_reply_missing")
                raw = response.get("raw_data")
                choices = (raw or {}).get("choices") if isinstance(raw, dict) else None
                need(isinstance(choices, list) and len(choices) == 1,
                     "native_user_reply_shape_changed")
                text = ((choices[0] or {}).get("message") or {}).get("content")
                need(isinstance(text, str) and text, "native_user_reply_missing")
                need(response.get("content") == text, "native_user_reply_content_changed")
                derived.append({"text": text,
                                "stop": any(marker in text for marker in USER_STOP_MARKERS)})
            # The driver's own events for this phase must repeat those replies.
            events = [e for e in self.events if e.get("kind") == "simulated_user"
                      and e.get("arm") == arm and e.get("task") == task_id]
            need(len(events) == len(derived), "simulated_user_event_count_changed")
            for event, expected in zip(events, derived):
                reply = event.get("reply") or {}
                need(reply.get("content") == expected["text"],
                     "simulated_user_content_not_from_native_reply")
                need(bool(reply.get("stop")) == expected["stop"],
                     "simulated_user_stop_not_from_native_reply")
            # And the actual wire must contain each NON-STOP reply as a runtime_user
            # envelope, in order, with byte-identical content, for THIS task.
            expected_texts = [item["text"] for item in derived if not item["stop"]]
            observed = []
            for post in record["post_slice"]:
                for envelope in self._envelopes_of(post):
                    if envelope["source"] != RUNTIME_SOURCE:
                        continue
                    ref = envelope.get("ref")
                    need(isinstance(ref, str), "runtime_user_ref_missing")
                    match = REF_PATTERN.fullmatch(ref)
                    need(match is not None, "runtime_user_ref_shape_changed")
                    need(int(match.group("turn")) == entry["record"]["number"],
                         "runtime_user_ref_turn_changed")
                    need(match.group("kind") == "user", "runtime_user_ref_kind_changed")
                    observed.append(envelope["content"])
            need(observed == expected_texts, "runtime_user_text_not_the_native_reply_on_wire")
            # Reply indices are per (arm, task): 1..n, contiguous from 1.
            indices = sorted(int(REF_PATTERN.fullmatch(envelope["ref"]).group("index"))
                             for post in record["post_slice"]
                             for envelope in self._envelopes_of(post)
                             if envelope["source"] == RUNTIME_SOURCE)
            if indices:
                need(indices == list(range(1, len(indices) + 1)),
                     "runtime_user_indices_not_contiguous_per_arm_task")
            # The current-task envelope is admitted exactly once, with ref tN/user/0.
            current_refs = [envelope["ref"] for post in record["post_slice"]
                            for envelope in self._envelopes_of(post)
                            if envelope["source"] == CURRENT_SOURCE]
            need(len(current_refs) == 1, "current_task_envelope_count_changed")
            need(current_refs[0] == f"t{record['number']}/user/0",
                 "current_task_ref_changed")
        self._checked("runtime_user_gate")

    # -- judge chain --------------------------------------------------------
    # -- pinned scoring components -----------------------------------------
    def _scorer_baseline(self):
        """The traced pinned scoring baseline, and the archive it is anchored to.

        The audit cannot compare a module with the digest it just computed for that
        same file - that proves nothing. The expected digests therefore come from a
        reviewed declaration (`deployment-assets/vita-scorer-baseline.json`, named by
        the run record) which is itself derived from the ARCHIVED pinned source; the
        archive's own SHA-256 is declared, re-read here, and every declared module is
        re-extracted from it. So a changed module, a changed declaration or a changed
        archive each stops the audit, and the archive is never unpacked onto disk.
        """
        if self._baseline is not None:
            return self._baseline
        declared = (self.plan.get("provenance") or {}).get("vita_scorer_baseline")
        need(isinstance(declared, dict) and declared.get("path"),
             "scorer_baseline_not_recorded_in_the_plan")
        path = Path(declared["path"])
        need(path.is_file(), "declared_scorer_baseline_missing")
        raw = path.read_bytes()
        need(declared.get("sha256") == hashlib.sha256(raw).hexdigest(),
             "declared_scorer_baseline_bytes_changed")
        need(declared.get("declared") is True, "declared_scorer_baseline_not_a_declaration")
        try:
            document = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise AuditFailure("declared_scorer_baseline_not_json") from None
        need(isinstance(document, dict), "declared_scorer_baseline_not_an_object")
        need(document.get("schema") == SCORER_BASELINE_SCHEMA,
             "scorer_baseline_schema_changed")
        archive = document.get("archive")
        need(isinstance(archive, dict) and archive.get("path") and archive.get("sha256"),
             "scorer_baseline_archive_not_declared")
        modules = document.get("modules")
        need(isinstance(modules, dict) and modules, "scorer_baseline_has_no_modules")
        prefix = archive.get("member_prefix") or ""
        blob, anchor = _resolve_baseline_archive(path, archive, document)
        blob_bytes = blob.read_bytes()
        need(hashlib.sha256(blob_bytes).hexdigest() == archive["sha256"],
             "scorer_baseline_archive_bytes_changed")
        if archive.get("bytes") is not None:
            need(len(blob_bytes) == archive["bytes"], "scorer_baseline_archive_size_changed")
        expected = {}
        try:
            with tarfile.open(blob, "r:gz") as tar:
                for key, (_name, relative) in sorted(PINNED_SCORER_MODULES.items()):
                    entry = modules.get("src/" + relative)
                    need(isinstance(entry, dict) and entry.get("sha256"),
                         "scorer_baseline_module_not_declared_" + key)
                    member = tar.extractfile(prefix + "src/" + relative)
                    need(member is not None, "scorer_baseline_archive_member_missing_" + key)
                    data = member.read()
                    need(hashlib.sha256(data).hexdigest() == entry["sha256"],
                         "scorer_baseline_module_not_from_the_archive_" + key)
                    expected[key] = {"relative": relative, "declared": "src/" + relative,
                                     "sha256": entry["sha256"], "bytes": len(data)}
        except AuditFailure:
            raise
        except Exception as exc:
            raise AuditFailure("scorer_baseline_archive_not_readable") from exc
        self._baseline = {
            "declared": deepcopy(declared), "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "schema": document.get("schema"), "modules": expected,
            "archive": {"path": str(blob), "sha256": archive["sha256"],
                        "bytes": len(blob_bytes),
                        "pinned_revision": archive.get("pinned_revision"),
                        "resolved_from": anchor},
            "status": "verified_against_the_archived_pinned_source"}
        self.report["pinned_scoring_modules"] = deepcopy(self._baseline)
        return self._baseline

    def _pinned_modules(self):
        """Import the PINNED scoring components and prove which bytes they are.

        The source directory is resolved from the run record, and every module the
        scoring chain uses is then checked by its REAL `__file__` (inside that
        directory) - so a module already imported from another checkout is refused,
        not reused from `sys.modules` - and by the SHA-256 of its bytes COMPARED WITH
        THE TRACED BASELINE, never with a digest computed from that same file.
        A changed byte, a comment included, therefore stops the audit.
        """
        if self._pinned is not None:
            return self._pinned
        need(self.pinned_source is not None, "pinned_vita_source_not_declared")
        package = (self.pinned_source / "src").resolve()
        for key, (_name, relative) in sorted(PINNED_SCORER_MODULES.items()):
            need((package / relative).is_file(), "pinned_file_missing_" + key)
        entry = str(package)
        if entry not in sys.path:
            sys.path.insert(0, entry)
        # The production wrapper refuses to load the pinned source when ANOTHER Vita
        # checkout is already imported. The audit resolves that the same way - the
        # foreign module is dropped and the declared checkout is imported again -
        # so a module from site-packages (or from a second checkout) can never be
        # what the scoring chain is re-derived from.
        dropped = _drop_foreign_vita_modules(package)
        modules, digests = {}, {}
        baseline = self._scorer_baseline()
        for key, (name, relative) in sorted(PINNED_SCORER_MODULES.items()):
            # The declared digest belongs to the declared FILE, so the path comes
            # from the baseline entry and the imported module must sit exactly there.
            declared_relative = baseline["modules"][key]["relative"]
            need(declared_relative == relative, "scorer_baseline_module_path_changed_" + key)
            try:
                module = importlib.import_module(name)
            except Exception as exc:
                raise AuditFailure("pinned_module_not_importable_" + key) from exc
            origin = getattr(module, "__file__", None)
            need(isinstance(origin, str) and origin, "pinned_module_has_no_file_" + key)
            path = Path(origin).resolve()
            need(path.is_relative_to(package), "pinned_module_outside_checkout_" + key)
            expected = baseline["modules"][key]
            need(path == (package / relative).resolve(),
                 "pinned_module_at_another_path_" + key)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            need(actual == expected["sha256"],
                 "pinned_module_bytes_differ_from_the_baseline_" + key)
            modules[key] = module
            digests[key] = {"path": str(path), "sha256": actual,
                            "baseline_sha256": expected["sha256"],
                            "baseline_source": baseline["archive"]["path"]}
        _verify_pinned_state_update(modules["evaluator"])
        evaluator = modules["evaluator"].TrajectoryEvaluator
        for name in ("_create_sliding_windows", "_format_window_content",
                     "_format_current_rubrics", "_initialize_rubric_states",
                     "_convert_states_to_checks"):
            need(callable(getattr(evaluator, name, None)),
                 "pinned_evaluator_missing_" + name)
        need(callable(getattr(modules["utils"], "evaluator_extracter", None)),
             "pinned_response_parser_missing")
        # The pinned package reads its model configuration WHILE it is imported, so the
        # audit proves - after the import - that it really read THIS run's declaration.
        self._verify_loaded_vita_config()
        self._pinned = {"package": package, "modules": modules, "digests": digests,
                        "baseline": baseline, "evaluator": evaluator,
                        "dropped_foreign_modules": dropped}
        return self._pinned

    def _native_messages(self, transcript):
        """Build the pinned native message objects from a task transcript.

        The scorer is handed real `Message` objects, so the audit builds the same
        objects from the same record; a transcript the pinned classes cannot
        represent is refused instead of being coerced. The pinned wrapper's OWN
        per-row rules are applied too (a tool row must carry its native id and name,
        an assistant row's tool calls must carry ids, names and object arguments, and
        `turn_idx` is the row's index), so "the record converts to the scored
        simulation" is checked against the same conversion the run performed.
        """
        pinned = self._pinned_modules()
        module = pinned["modules"]["message"]
        classes = {"assistant": module.AssistantMessage, "user": module.UserMessage,
                   "tool": module.ToolMessage}
        messages = []
        for index, row in enumerate(transcript or []):
            need(isinstance(row, dict) and row.get("role") in classes,
                 "transcript_row_not_a_native_message")
            fields = set(getattr(classes[row["role"]], "model_fields", {}) or {})
            need(fields and set(row) <= fields,
                 "transcript_row_has_fields_outside_the_native_class")
            if row["role"] == "tool":
                need(isinstance(row.get("id"), str) and row["id"]
                     and isinstance(row.get("name"), str) and row["name"],
                     "transcript_tool_row_without_native_id_and_name")
            if row.get("tool_calls") is not None:
                calls = row["tool_calls"]
                need(row["role"] == "assistant" and isinstance(calls, list) and calls
                     and all(isinstance(call, dict)
                             and isinstance(call.get("id"), str) and call["id"]
                             and isinstance(call.get("name"), str) and call["name"]
                             and isinstance(call.get("arguments"), dict)
                             and call.get("requestor", "assistant") == "assistant"
                             for call in calls),
                     "transcript_assistant_tool_calls_not_native")
            message = classes[row["role"]].model_validate(deepcopy(row))
            message.turn_idx = index
            messages.append(message)
        need(messages, "task_transcript_empty")
        return messages

    def _verify_native_conversion_source(self):
        """The pinned wrapper's own conversion rules are still what this audit applies.

        The rules live in `ae_vita.py` (`NativeVita.finish` / `NativeVita._messages`), whose
        digest the run record pins; the file is re-read here and its own statements are
        matched, so the derivation is anchored to the reviewed source rather than to the
        audit's description of it.
        """
        path = Path(__file__).with_name("ae_vita.py")
        need(path.is_file(), "native_wrapper_source_missing")
        recorded = ((self.plan.get("provenance") or {}).get("code_sha256") or {})
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        need(recorded.get("ae_vita.py") == digest,
             "native_wrapper_source_differs_from_the_run_record")
        flat = re.sub(r"\s+", "", path.read_text(encoding="utf-8"))
        for marker in NATIVE_CONVERSION_SOURCE_MARKERS:
            need(re.sub(r"\s+", "", marker) in flat, "native_conversion_rule_changed")
        pinned = self._pinned_modules()
        for key in ("message", "utils"):
            need(pinned["modules"].get(key) is not None,
                 "pinned_native_module_missing_" + key)
        stamp = getattr(pinned["modules"]["utils"], "get_now", None)
        need(callable(stamp), "pinned_native_timestamp_factory_missing")
        import inspect
        parameters = inspect.signature(stamp).parameters
        need("format" in parameters
             and parameters["format"].default == "%Y%m%d_%H%M%S",
             "pinned_native_timestamp_format_changed")
        self._checked("native_conversion_source")

    def _judged_simulation_for(self, entry):
        """Resolve THIS phase's own judged simulation from the run's real record.

        The native wrapper's snapshot carries `completed` and `last_evaluation`; it has no
        per-task `judged_simulation_this_task` field, and the sealed run's driver recorded
        none (its empty view is why every phase looked unjudged). The simulation is
        therefore located by THIS arm's and task's own `completed[].subtask_id` - exactly
        one entry, never another arm's or a later phase's - and cross-checked against
        `last_evaluation`, the recorded reward copies and the recorded termination reason.
        An explicit `judged_simulation_this_task`, when a record carries one, is compared
        with that resolution: it is never preferred over it and may not mask a conflict.
        """
        record = entry["record"]
        task_id = entry["task"]
        snapshot = record.get("native_snapshot")
        need(isinstance(snapshot, dict), "native_snapshot_missing_for_this_task")
        completed = snapshot.get("completed")
        need(isinstance(completed, list) and completed,
             "native_snapshot_completed_missing")
        matches = [item for item in completed
                   if isinstance(item, dict) and item.get("subtask_id") == task_id]
        need(len(matches) == 1,
             "completed_simulation_identity_not_unique_for_this_task")
        judged = matches[0].get("simulation")
        need(isinstance(judged, dict), "completed_simulation_missing")
        last = snapshot.get("last_evaluation")
        need(isinstance(last, dict) and isinstance(last.get("simulation"), dict),
             "snapshot_last_evaluation_missing")
        need(last["simulation"] == judged,
             "completed_simulation_differs_from_last_evaluation")
        reward_copies = {
            "completed": matches[0].get("reward_info"),
            "last_evaluation": last.get("reward_info"),
            "judge": (record.get("judge") or {}).get("reward_info")}
        need(reward_copies["completed"] == reward_copies["last_evaluation"]
             == reward_copies["judge"], "judge_reward_records_differ")
        need(judged.get("termination_reason") == record.get("termination_reason"),
             "judged_termination_reason_differs_from_the_task")
        explicit = record.get("judged_simulation_this_task")
        explicit_check = {"present": explicit is not None}
        if explicit is not None:
            need(isinstance(explicit, dict), "judged_simulation_this_task_not_an_object")
            # The driver's field, when a record carries one, may spell the task id as the
            # subtask id; every other claim it makes must agree with the resolution.
            need(explicit.get("task_id") in (judged.get("task_id"), task_id),
                 "judged_simulation_this_task_names_another_task")
            need(explicit.get("messages") == judged.get("messages"),
                 "judged_simulation_this_task_messages_differ")
            need(explicit.get("termination_reason", judged.get("termination_reason"))
                 == judged.get("termination_reason"),
                 "judged_simulation_this_task_termination_differs")
            if explicit.get("reward_info") is not None:
                need(explicit["reward_info"] == reward_copies["judge"],
                     "judged_simulation_this_task_reward_differs")
            explicit_check["agrees_with_the_snapshot"] = True
        evidence = {
            "arm": entry["arm"], "task": task_id, "phase_index": entry["index"],
            "source": "record.native_snapshot.completed[subtask_id]",
            "completed_entries_for_this_task": len(matches),
            "snapshot_completed_entries": len(completed),
            "cross_checked_with": ["native_snapshot.last_evaluation.simulation",
                                   "completed[].reward_info",
                                   "last_evaluation.reward_info", "judge.reward_info",
                                   "record.termination_reason"],
            "native_task_id": judged.get("task_id"),
            "termination_reason": judged.get("termination_reason"),
            "reward_copies_equal": True,
            "explicit_judged_simulation_this_task": explicit_check,
        }
        self.report["judged_simulation_sources"].append(deepcopy(evidence))
        return judged, evidence

    def _verify_native_conversion(self, entry, judged):
        """Reproduce the run's own Task/Message conversion and compare it field by field.

        `NativeVita.finish` builds `task_id = "U000828_subtask_" + subtask_id` and hands
        the pinned classes one native message per transcript row with `turn_idx` set to the
        row's index. The audit re-derives exactly that from the recorded transcript and
        requires the saved simulation to be its result:

        * `task_id` is the deterministic conversion of THIS phase's subtask id;
        * every row converts to the native class, with no field outside it, and the
          converted message equals the saved one - role, content, tool ids, arguments,
          order and `turn_idx` included;
        * a `timestamp` the transcript CARRIED is compared exactly (the conversion copies
          it), so a rewritten transcript time is refused;
        * a `timestamp` the transcript did NOT carry was generated by the pinned default
          factory while the run converted its messages. The audit cannot reproduce that
          clock and must not substitute today's: it requires the saved value's pinned
          shape, records it as runtime-generated metadata, and requires the run's own two
          copies of the simulation to carry the SAME generated values.
        """
        from ae_vita import _plain as native_plain
        record = entry["record"]
        task_id = entry["task"]
        transcript = record.get("transcript")
        need(isinstance(transcript, list) and transcript, "task_transcript_missing")
        need(judged.get("task_id") == NATIVE_TASK_ID_PREFIX + task_id,
             "judged_task_id_is_not_the_native_conversion_of_this_subtask")
        saved = judged.get("messages")
        need(isinstance(saved, list), "judged_messages_missing")
        messages = self._native_messages(transcript)
        need(len(saved) == len(messages),
             "judged_messages_count_differs_from_the_task_transcript")
        generated = []
        rows = []
        for index, (message, kept) in enumerate(zip(messages, saved)):
            need(isinstance(kept, dict), "judged_message_not_an_object")
            converted = native_plain(message)
            carried = "timestamp" in (transcript[index] or {})
            if carried:
                need(converted == kept,
                     f"judged_message_{index}_is_not_the_native_conversion_of_the_transcript")
            else:
                stamp = converted.get("timestamp")
                need(isinstance(stamp, str) and NATIVE_TIMESTAMP_PATTERN.match(stamp),
                     f"runtime_generated_timestamp_{index}_has_an_unexpected_shape")
                need(isinstance(kept.get("timestamp"), str)
                     and NATIVE_TIMESTAMP_PATTERN.match(kept["timestamp"]),
                     f"saved_runtime_timestamp_{index}_has_an_unexpected_shape")
                converted = dict(converted, timestamp=kept["timestamp"])
                need(converted == kept,
                     f"judged_message_{index}_is_not_the_native_conversion_of_the_transcript")
                generated.append({"index": index, "saved": kept["timestamp"]})
            rows.append({"index": index, "role": kept.get("role"),
                         "timestamp_from_transcript": carried,
                         "turn_idx": kept.get("turn_idx")})
        last = ((record.get("native_snapshot") or {}).get("last_evaluation") or {})
        copies = [saved, (last.get("simulation") or {}).get("messages")]
        explicit = record.get("judged_simulation_this_task")
        if isinstance(explicit, dict):
            copies.append(explicit.get("messages"))
        for copy_index, copy_of in enumerate(copies[1:], start=1):
            need(copy_of == saved,
                 "snapshot_copy_of_the_messages_differs_from_the_scored_one")
        evidence = {
            "arm": entry["arm"], "task": task_id, "phase_index": entry["index"],
            "native_task_id": judged.get("task_id"),
            "task_id_rule": "U000828_subtask_ + subtask_id",
            "messages": len(rows),
            "runtime_generated_timestamps": generated,
            "runtime_generated_timestamp_count": len(generated),
            "snapshot_copies_compared": len(copies),
            "note": ("a generated timestamp is the pinned default factory's output at "
                     "conversion time; its saved value and shape are checked, its wall "
                     "clock is not re-proved"),
        }
        self.report["native_conversions"].append(deepcopy(evidence))
        runtime = self.report["runtime_generated_timestamps"]
        runtime["count"] += len(generated)
        runtime["per_phase"].append({"arm": entry["arm"], "task": task_id,
                                     "count": len(generated)})
        return evidence

    def _expected_windows(self, transcript):
        """The pinned sliding windows AND their pinned formatted window content.

        The numbering is NOT derived from range(): the pinned formatter emits one
        line per message that renders to non-empty text, so a message with empty
        content (for example a tool return that carries nothing) consumes an index
        without producing a line. The audit therefore compares the ACTUAL prompt
        bytes with the pinned formatter's own output, and the visible numbers are
        whatever that function produced.
        """
        pinned = self._pinned_modules()
        evaluator = pinned["evaluator"]
        messages = self._native_messages(transcript)
        windows = evaluator._create_sliding_windows(messages, 10, 2)
        step = 10 - 2
        spans = []
        for index, window in enumerate(windows):
            start = index * step
            content = evaluator._format_window_content(window, start)
            numbers = [int(value) for value in re.findall(r"^\[(\d+)\]", content, re.M)]
            spans.append({"index": index + 1, "start": start,
                          "native_message_count": len(window),
                          "visible_numbers": numbers,
                          "window_content": content})
        return spans

    def _phase_user_turns(self, task):
        """Every user turn of one phase's OWN transcript, with the agent text before it.

        The transcript is the audited record of what the simulator really produced, so
        it - not a self-reported list - is what the decisions are bound to.
        """
        transcript = task.get("transcript") or []
        out = []
        for index, message in enumerate(transcript):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            incoming = ""
            for back in range(index - 1, -1, -1):
                previous = transcript[back]
                if isinstance(previous, dict) and previous.get("role") == "assistant":
                    incoming = str(previous.get("content") or "")
                    break
            out.append({"index": index, "content": message.get("content"),
                        "incoming": incoming})
        return out

    def _phase_orders(self, task):
        """The phase's own live order table and the ids captured before it started."""
        snapshot = task.get("native_snapshot") or {}
        environment = snapshot.get("environment_db") or {}
        return environment.get("orders") or {}, (task.get("baseline_order_ids") or [])

    def _order_state_by_turn(self, task):
        """The recorded per-turn order table, cross-checked against the tool evidence.

        The runtime reads the LIVE table when the simulator answers, so a decision is
        only reproducible from the table that existed at THAT turn. Each recorded
        projection must show only orders the phase's own ordered tool evidence had
        already created by that point, and must not have lost an order it already had -
        so a table padded with a FUTURE order is refused.
        """
        recorded = task.get("order_state_by_turn")
        need(isinstance(recorded, list) and recorded,
             "order_state_by_turn_not_recorded")
        created_by_tool = []
        for call in task.get("tool_call_slice") or []:
            result = str((call or {}).get("result") or "")
            for order_id in re.findall(r"order_id[:=]\s*([A-Za-z0-9]+)", result):
                if order_id not in created_by_tool:
                    created_by_tool.append(order_id)
        need(created_by_tool, "phase_tool_evidence_records_no_order_creation")
        state = []
        for entry in recorded:
            need(isinstance(entry, dict) and isinstance(entry.get("orders"), dict),
                 "order_state_by_turn_malformed")
            allowed = set(created_by_tool[: int(entry.get("tool_calls_so_far") or 0)])
            visible = set(entry["orders"])
            # Every visible order was created by a tool call this phase had already made,
            # and the table never shrinks: an order cannot be used before it exists.
            need(visible <= allowed,
                 "order_state_cites_an_order_no_tool_had_created_yet")
            state.append(entry)
        need([e.get("turn_index") for e in state] == list(range(len(state))),
             "order_state_turn_index_out_of_order")
        # The phase's own tool evidence must ACCOUNT FOR every order in its database:
        # a table missing an order a tool of this phase really created has been edited,
        # and no amount of internal consistency can make that a valid phase record.
        for order_id in created_by_tool:
            need(order_id in (state[-1]["orders"] if state else {}),
                 "phase_order_missing_from_the_captured_table")
        final_orders, _baseline = self._phase_orders(task)
        need(sorted(state[-1]["orders"]) == sorted(final_orders),
             "order_state_does_not_reach_the_captured_table")
        # The visible table only ever grows within a phase.
        previous: set = set()
        for entry in state:
            visible = set(entry["orders"])
            need(visible >= previous, "order_state_shrank_between_turns")
            previous = visible
        return [{k: v for k, v in entry["orders"].items()} for entry in state]

    def _replayed_simulator_decisions(self, task, bundle):
        """Recompute every user-turn decision from the table visible AT that turn.

        The recorded per-turn projections supply those tables; reusing the FINAL table
        for every turn would let a later order stand in for evidence an earlier turn
        never had. The projections themselves are checked against the tool evidence.
        """
        repair = _repair_module()
        turns = self._phase_user_turns(task)
        states = self._order_state_by_turn(task)
        need(len(turns) == len(states),
             "order_state_count_differs_from_the_user_turns")
        decisions = []
        for turn, visible in zip(turns, states):
            decisions.append(repair.validate_user_reply(
                text=turn["content"], incoming_assistant_text=turn["incoming"],
                new_order_ids=sorted(visible), paid_order_ids=[],
                visible_orders=visible,
                protocol=bundle["simulator_protocol"]))
        return decisions

    def _recomputed_business(self, task, task_id):
        """Recompute the phase's deterministic business result from its own database.

        The standard comes from the declared dataset and the facts from the phase's own
        captured environment, so a fabricated order id, a deleted `requirements` block
        or an invented completeness claim cannot survive: the result is recomputed and
        compared, never adopted.
        """
        repair = _repair_module()
        criteria = self._criteria_for(task_id)
        standard = repair.standard_items({"evaluation_criteria": criteria.model_dump()})
        raw = self._dataset_users()
        user = raw[USER_INDEX]
        subtask = user["subtasks"][int(task_id.rsplit("_", 1)[-1]) - 1]
        profile = ((subtask.get("user_scenario") or {}).get("user_profile") or {})
        # The environment re-read is THIS phase's own captured database - the same bytes
        # the orders come from - while the profile is the declared dataset's own field.
        snapshot = task.get("native_snapshot") or {}
        environment = snapshot.get("environment_db") or {}
        task_view = {
            "instruction": subtask.get("instruction"),
            "environment": environment,
            "user_profile": profile,
        }
        orders, baseline = self._phase_orders(task)
        return repair.deterministic_business_result(
            arm=task.get("arm"), number=int(task_id.rsplit("_", 1)[-1]),
            task=task_view,
            scope_task_record={"target_product_ids": list(
                subtask.get("target_product_ids") or [])},
            orders=orders, baseline_order_ids=baseline,
            dataset_rubric_texts=[x["rubric"] for x in standard],
            standard_item_pool=standard)

    def repair_protocol_gate(self):
        """A 0.5 run's declared repair protocols, verified against its own record.

        Nothing is whitelisted away: the two protocol fields must name versions the
        repair module implements, EVERY phase must carry the SAME declared bundle, every
        user turn must carry the protocol's own decision record, the simulator's own
        system prompt must carry that protocol's versioned constraint block, and the
        separated evaluation must be present and internally consistent. A 0.1-0.4 run
        declares no protocols, so this gate records `not_declared` and checks nothing.
        """
        report = self.report
        plan_config = self.plan.get("config") or {}
        if plan_config.get("schema_version") not in (MULTITURN_SCHEMA_SIM_EVAL,):
            report["repair_protocols"] = {"declared": False,
                                          "reason": "the run declares no repair protocols"}
            self._checked("repair_protocol_gate")
            return
        repair = _repair_module()
        declared = {
            "simulator_protocol": plan_config.get("simulator_protocol"),
            "evaluation_protocol": plan_config.get("evaluation_protocol"),
        }
        try:
            bundle = repair.validate_protocol_bundle(declared)
        except Exception:  # noqa: BLE001 - any refusal is an audit failure
            self.fail("declared_repair_protocol_unknown")
        need(declared["simulator_protocol"] in repair.USER_SIMULATOR_PROTOCOLS,
             "simulator_protocol_not_reviewed")
        need(declared["evaluation_protocol"] in repair.EVALUATION_PROTOCOLS,
             "evaluation_protocol_not_reviewed")
        bundle_sha = repair.protocol_bundle_sha256(bundle)
        constraints = repair.user_simulator_system_suffix(bundle["simulator_protocol"])

        # Every phase must have run under THIS bundle, and must say so in its own record.
        phases = [task for arm in ARMS
                  for task in (self.result.get("arms") or {}).get(arm, {}).get("tasks") or []]
        need(phases, "no_phase_records_to_verify_protocols")
        for task in phases:
            identity = task.get("subtask_id")
            need(task.get("protocols") == bundle,
                 "phase_protocol_bundle_differs_from_the_declaration_" + str(identity))
            need(task.get("protocol_bundle_sha256") == bundle_sha,
                 "phase_protocol_bundle_digest_changed_" + str(identity))

        # Every user turn the driver produced carries the protocol's own decision, and
        # the simulator's OWN system prompt carries the versioned constraint block.
        decisions, prompts = 0, 0
        for task in phases:
            identity = task.get("subtask_id")
            snapshot = task.get("native_snapshot") or {}
            for event in snapshot.get("events") or []:
                if not isinstance(event, dict) or event.get("event") != "start":
                    continue
                prompt = event.get("user_system_prompt")
                need(isinstance(prompt, str) and prompt,
                     "simulator_system_prompt_not_recorded")
                need(constraints in prompt,
                     "simulator_system_prompt_lacks_the_declared_constraints")
                prompts += 1
            recorded = task.get("simulator_decisions") or []
            expected = self._replayed_simulator_decisions(task, bundle)
            # Count, order and content are bound to the phase's OWN real user turns: a
            # deleted, duplicated, reordered or invented decision list is refused, and
            # each entry is RECOMPUTED from the message pair + the phase database rather
            # than believed because it says `violations: []`.
            need(len(recorded) == len(expected),
                 "simulator_decision_count_differs_from_the_user_turns_" + str(identity))
            for position, (got, want) in enumerate(zip(recorded, expected)):
                need(isinstance(got, dict), "simulator_decision_record_malformed")
                for field in ("outcome", "stop", "violations", "claims",
                              "user_ended_normally"):
                    need(got.get(field) == want[field],
                         f"simulator_decision_{field}_not_reproducible_"
                         f"{identity}_{position}")
                need(got.get("protocol") == bundle["simulator_protocol"],
                     "simulator_decision_declares_another_protocol")
                need(got.get("stop") is not True
                     or (not got["violations"] and not got["claims"]),
                     "a_violating_simulator_turn_was_accepted_as_a_stop")
                decisions += 1
        need(prompts == len(phases), "phase_without_a_recorded_simulator_prompt")

        # The separated evaluation must be present, name the same protocols, and keep
        # the three results apart for every phase it reports.
        evaluation = self.result.get("evaluation")
        need(isinstance(evaluation, dict), "separated_evaluation_not_recorded")
        need(evaluation.get("bundle") == bundle, "evaluation_bundle_differs_from_the_declaration")
        need(evaluation.get("bundle_sha256") == bundle_sha,
             "evaluation_bundle_digest_changed")
        need(evaluation.get("separates") == ["native_rubric_score", "business_completion",
                                             "evaluation_conflict"],
             "evaluation_does_not_separate_the_three_results")
        evaluated = evaluation.get("phases")
        need(isinstance(evaluated, list) and len(evaluated) == len(phases),
             "evaluation_phase_count_differs_from_the_record")
        by_phase = {(t.get("arm"), t.get("subtask_id")): t for t in phases}
        need(len(by_phase) == len(phases), "duplicate_phase_in_the_evaluation_record")
        for phase in evaluated:
            identity = phase.get("subtask_id")
            task = by_phase.get((phase.get("arm"), identity))
            need(task is not None, "evaluation_phase_not_in_the_record_" + str(identity))
            need(isinstance(phase.get("native_rubric_score"), dict),
                 "evaluation_phase_without_a_native_score")
            business = phase.get("business_completion")
            need(isinstance(business, dict), "evaluation_phase_without_a_business_result")
            # RECOMPUTE it from the phase's own database and the dataset's own standard,
            # then require the record to agree. A fabricated order id, a deleted
            # `requirements` block or an invented completeness claim is refused here -
            # the audit never adopts the record's own account of itself.
            want = self._recomputed_business(task, identity)
            for field in ("new_order_ids", "paid_order_ids", "statuses", "requirements",
                          "checked_requirements", "uncovered_requirements",
                          "uncovered_standard_items", "unmet_requirements", "complete",
                          "completion_gap_kind", "payment_requirement",
                          "executed_checks"):
                need(business.get(field) == want[field],
                     f"business_{field}_not_reproducible_from_the_phase_database_"
                     f"{identity}")
            # And the claim itself must still be self-consistent.
            if business.get("complete") is True:
                need(not business.get("unmet_requirements"),
                     "business_complete_with_unmet_requirements")
                need(not business.get("uncovered_requirements"),
                     "business_complete_with_uncovered_requirements")
                need(not business.get("uncovered_standard_items"),
                     "business_complete_with_uncovered_standard_items")
                need(business.get("new_order_ids"),
                     "business_complete_without_a_phase_order")
            conflict = phase.get("evaluation_conflict")
            need(isinstance(conflict, dict), "evaluation_phase_without_a_conflict_result")
            need(conflict.get("is_capability_failure") is False,
                 "a_conflict_was_recorded_as_a_capability_failure")
        report["repair_protocols"] = {
            "declared": True, "bundle": bundle, "bundle_sha256": bundle_sha,
            "phases": len(phases), "simulator_decisions": decisions,
            "simulator_prompts": prompts,
            "constraint_block_sha256": hashlib.sha256(
                constraints.encode("utf-8")).hexdigest(),
        }
        self._checked("repair_protocol_gate")

    def _rubric_keys_and_text(self, task_id):
        """The task's OWN rubric keys and requirement text, from the fixed data.

        The keys come from the pinned initialiser over the real evaluation
        criteria, and the text comes from that criteria object itself, so a
        judged decision can be compared with the requirement it claims to decide.
        """
        declared = self.plan["inputs"]["tasks"][
            int(task_id.rsplit("_", 1)[-1]) - self.task_numbers[0]]
        criteria = self._criteria_for(task_id)
        states = self._pinned_initial_states_for(task_id, criteria)
        return {key: state["rubric"] for key, state in states.items()}, declared

    def _pinned_initial_states_for(self, task_id, criteria=None):
        """The pinned initialiser's output for this task, cached per task."""
        cached = self._initial_states.get(task_id)
        if cached is not None:
            return cached
        if criteria is None:
            criteria = self._criteria_for(task_id)
        states = _pinned_initial_states(self._pinned_modules()["evaluator"], criteria)
        self._initial_states[task_id] = states
        return states

    def _dataset_users(self):
        """The declared dataset, read once and verified against the plan's SHA.

        The rubric text a decision claims to decide comes from this file, so the
        bytes are hashed against the plan's own declaration before they are used;
        a substituted dataset cannot supply different rubric text.
        """
        if self._dataset is not None:
            return self._dataset
        need(self.dataset_path is not None and self.dataset_path.is_file(),
             "declared_dataset_missing_for_rubrics")
        raw_bytes = self.dataset_path.read_bytes()
        declared = (self.plan.get("source") or {}).get("sha256")
        need(isinstance(declared, str) and declared,
             "plan_source_provenance_missing")
        need(hashlib.sha256(raw_bytes).hexdigest() == declared,
             "dataset_declaration_mismatch")
        self._dataset = json.loads(raw_bytes.decode("utf-8"))
        return self._dataset

    def _criteria_for(self, task_id):
        """The real `EvaluationCriteria` for one task, from the declared dataset."""
        cached = self._criteria.setdefault(task_id, None)
        if cached is not None:
            return cached
        raw = self._dataset_users()
        need(isinstance(raw, list) and len(raw) > USER_INDEX, "dataset_shape_changed")
        user = raw[USER_INDEX]
        need(user.get("id") == USER_ID, "dataset_user_identity_changed")
        number = int(task_id.rsplit("_", 1)[-1])
        subtask = user["subtasks"][number - 1]
        need(subtask.get("subtask_id") == task_id, "dataset_subtask_identity_changed")
        module = self._pinned_modules()["modules"]["tasks"]
        criteria_class = getattr(module, "EvaluationCriteria", None)
        need(criteria_class is not None, "pinned_criteria_class_missing")
        criteria = criteria_class.model_validate(subtask["evaluation_criteria"])
        self._criteria[task_id] = criteria
        return criteria

    def judge_gate(self):
        """The judge chain, checked with the PINNED scoring components.

        For every phase the audit re-derives the sliding windows, their formatted
        content, the rubric state each window carries, the response parsing and the
        final aggregation from the fixed Vita source. It checks:

        * this task's own instruction and environment time are in the prompt;
        * the window's `<window_content>` is BYTE-IDENTICAL to the pinned
          formatter's output for that window (so a changed word with an unchanged
          number is refused);
        * the rubric section carries the task's real rubric keys AND their real
          requirement text, and EQUALS the state the pinned initialiser and the
          preceding window's own decisions produce - boolean, justification and
          text together, from the first window's initial state onwards;
        * the response parses through the pinned `evaluator_extracter` (which
          handles the real ```json fence), answers exactly the carried keys, and
          uses the pinned decision fields;
        * `window_evaluations` matches the calls one-to-one, in order, with the
          exact prompts that were sent;
        * the final `nl_rubrics` uses the pinned output shape
          (`nl_rubric`/`met`/`justification`), is non-empty, and its `met` flags
          are the last window's decisions for the SAME rubric text;
        * the reward equals the pinned aggregation over those final checks.

        The scored simulation is RESOLVED from the phase's own recorded snapshot (the
        real record carries `completed`/`last_evaluation`, not a per-task copy) and the
        task/message conversion is re-derived from the transcript with the pinned
        classes. Under the default STRICT protocol a reply whose `rubric` text is not
        the task's own requirement text is refused; the explicit `native-by-id-v1`
        recheck collects those echo differences instead, because the pinned evaluator
        reads only `rubric_idx` for the state update - and then reports the strict
        failure SEPARATELY instead of turning it into a pass.
        """
        self._pinned = None
        self._baseline = None
        self._initial_states = {}
        self._criteria = {}
        self._env_times = {}
        self._dataset = None
        self.report["pinned_scoring_modules"] = None
        self.judge_chains = {}
        need(self.pinned_source is not None, "pinned_vita_source_not_declared")
        pinned = self._pinned_modules()
        evaluator = pinned["evaluator"]
        parser = pinned["modules"]["utils"].evaluator_extracter
        checks_class = getattr(pinned["modules"]["simulation"], "NLRubricCheck", None)
        need(checks_class is not None, "pinned_final_check_class_missing")
        self._verify_native_conversion_source()
        for entry in sorted(self.phase_records.values(), key=lambda item: item["index"]):
            arm, task_id = entry["arm"], entry["task"]
            record = entry["record"]
            transcript = record.get("transcript") or []
            # THIS phase's own judged simulation, resolved from its own snapshot and
            # cross-checked (rewards, termination, the optional explicit copy), and the
            # real native Task/Message conversion of the recorded transcript.
            judged, _source = self._judged_simulation_for(entry)
            conversion = self._verify_native_conversion(entry, judged)
            rubric_text, declared_task = self._rubric_keys_and_text(task_id)
            # The state window 1 must carry: the PINNED initialiser's own output for
            # this task's real criteria (unmet, with its initial justification).
            carried_expected = self._pinned_initial_states_for(task_id)
            spans = self._expected_windows(transcript)
            native = record.get("native_calls_this_task")
            evaluators = [call for call in native if call.get("role") == "evaluator"]
            need(len(evaluators) == len(spans),
                 "judge_window_count_differs_from_the_pinned_expansion")
            window_records = []
            for position, (call, span) in enumerate(zip(evaluators, spans), start=1):
                need(call.get("subtask_id") == task_id, "judge_bound_to_another_task")
                request = call.get("request")
                need(isinstance(request, dict), "judge_request_missing")
                messages = request.get("messages")
                need(isinstance(messages, list) and len(messages) == 2,
                     "judge_request_shape_changed")
                system, user = messages[0], messages[1]
                need(system.get("role") == "system" and user.get("role") == "user",
                     "judge_request_roles_changed")
                system_text, user_text = system.get("content"), user.get("content")
                need(isinstance(system_text, str) and isinstance(user_text, str),
                     "judge_request_content_not_text")
                # The window identity, from the pinned template's own text.
                match = re.search(r"第\s*(\d+)\s*个窗口", system_text)
                total = re.search(r"总共\s*(\d+)\s*个窗口", system_text)
                need(match is not None, "judge_window_index_not_declared")
                need(total is not None, "judge_window_total_not_declared")
                need(int(match.group(1)) == position, "judge_window_index_out_of_order")
                need(int(total.group(1)) == len(spans), "judge_window_total_changed")
                # This task's own instruction, and its own environment time.
                need(declared_task["instruction"] in system_text,
                     "judge_request_is_not_this_task_instruction")
                env_time = (self._environment_time_for(task_id))
                if env_time:
                    need(env_time in system_text, "judge_request_missing_this_task_env_time")
                # The window's own content, BYTE-IDENTICAL to the pinned formatter.
                block = re.search(r"<window_content>\n(?P<body>.*?)\n</window_content>",
                                  user_text, re.S)
                need(block is not None, "judge_window_content_section_missing")
                need(block.group("body") == span["window_content"],
                     "judge_window_content_differs_from_the_pinned_format")
                carried = self._rubric_section(user_text)
                need({item["rubric_idx"] for item in carried} == set(rubric_text),
                     "judge_rubric_keys_differ_from_the_task_criteria")
                for item in carried:
                    need(item["rubric"] == rubric_text[item["rubric_idx"]],
                         "judge_rubric_text_differs_from_the_task_criteria")
                    need(isinstance(item["justification"], str),
                         "judge_rubric_justification_missing")
                # The carried state must BE the state the pinned rule left: the
                # initialiser's output for window 1, and the previous window's own
                # decisions (justification included) for every later window. A
                # flipped boolean or a rewritten justification is refused here.
                _requires_carried_state(carried_expected, carried, position)
                # The response is parsed by the PINNED parser (fences included).
                response = call.get("response")
                need(isinstance(response, dict), "judge_response_missing")
                raw = response.get("raw_data")
                choices = (raw or {}).get("choices") if isinstance(raw, dict) else None
                need(isinstance(choices, list) and len(choices) == 1,
                     "judge_response_shape_changed")
                content = ((choices[0] or {}).get("message") or {}).get("content")
                need(isinstance(content, str) and content, "judge_response_empty")
                need(response.get("content") == content, "judge_response_content_changed")
                try:
                    decisions = parser(content)
                except Exception as exc:
                    raise AuditFailure("judge_response_not_parsed_by_the_pinned_parser") from exc
                need(isinstance(decisions, list) and decisions,
                     "judge_decisions_not_a_list")
                states = []
                echoes = []
                for decision in decisions:
                    need(isinstance(decision, dict), "judge_decision_not_an_object")
                    need(set(decision) >= {"rubric_idx", "rubric", "justification",
                                           "meetExpectation"},
                         "judge_decision_fields_changed")
                    need(isinstance(decision["rubric_idx"], str)
                         and isinstance(decision["meetExpectation"], bool)
                         and isinstance(decision["justification"], str)
                         and isinstance(decision["rubric"], str),
                         "judge_decision_field_types_changed")
                    criteria_text = rubric_text.get(decision["rubric_idx"])
                    if decision["rubric"] != criteria_text:
                        # The STRICT protocol requires the reply to echo the task's own
                        # requirement text. The PINNED evaluator does not: it updates the
                        # state by `rubric_idx` and always keeps the criteria text of the
                        # initial states, so this difference changes no score. The strict
                        # default still refuses it; the explicit `native-by-id-v1` recheck
                        # collects it, keeps the strict failure in the report, and never
                        # presents the run as passing the strict protocol.
                        echo = {"arm": arm, "task": task_id, "window_index": position,
                                "rubric_idx": decision["rubric_idx"],
                                "criteria_text": criteria_text,
                                "echoed_text": decision["rubric"]}
                        if not self.native_judge_semantics:
                            self.fail("judge_decision_rubric_text_differs_from_the_criteria")
                        self.judge_echo_differences.append(deepcopy(echo))
                        echoes.append(echo)
                    states.append({"rubric_idx": decision["rubric_idx"],
                                   "meetExpectation": decision["meetExpectation"],
                                   "justification": decision["justification"]})
                need({item["rubric_idx"] for item in states} == set(rubric_text),
                     "judge_decision_rubric_set_differs_from_the_task_criteria")
                # Each rubric exactly ONCE: the pinned wrapper refuses a reply with a
                # missing or duplicated index while the run is executing, so a record that
                # carries one is not the reply the run accepted. A duplicated index would
                # otherwise be invisible to the set comparison above.
                need(len(states) == len({item["rubric_idx"] for item in states}),
                     "judge_decision_rubric_index_duplicated")
                # The pinned update rule, applied to THIS window's own reply: the
                # state the NEXT window must carry. Parsing inside the derivation is
                # the pinned parser, so the reply is not interpreted twice.
                carried_expected = _apply_pinned_state_update(carried_expected, content,
                                                              parser)
                window_records.append({"window_index": position,
                                       "visible_numbers": span["visible_numbers"],
                                       "native_message_count": span["native_message_count"],
                                       "carried_rubric_states": deepcopy(carried),
                                       "rubric_states": deepcopy(states),
                                       "echo_differences": deepcopy(echoes),
                                       "resulting_rubric_states": deepcopy(carried_expected)})
            # The recorded window evaluations must be these very windows.
            judge = record.get("judge") or {}
            need(judge.get("status") != "JUDGE_FAILED_ORDERS_RETAINED",
                 "executed_run_carries_a_failed_judge_phase")
            reward_info = judge.get("reward_info") or {}
            need(reward_info.get("info", {}).get("evaluation_method") == "sliding_window",
                 "judge_reward_method_changed")
            need(reward_info.get("info", {}).get("num_windows") == len(spans),
                 "judge_reward_num_windows_differs_from_the_expansion")
            evaluations = reward_info.get("window_evaluations")
            need(isinstance(evaluations, list) and len(evaluations) == len(evaluators),
                 "judge_window_evaluations_differ_from_the_expansion")
            for position, (call, evaluation) in enumerate(zip(evaluators, evaluations),
                                                           start=1):
                request = call["request"]["messages"]
                need(evaluation.get("window_idx") == position,
                     "judge_window_evaluation_index_out_of_order")
                need(evaluation.get("system_prompt") == request[0]["content"],
                     "judge_window_evaluation_system_prompt_differs")
                need(evaluation.get("user_prompt") == request[1]["content"],
                     "judge_window_evaluation_user_prompt_differs")
                need(evaluation.get("assistant_message_content") == call["response"]["content"],
                     "judge_window_evaluation_reply_differs")
            # Final rubrics: the pinned output shape, non-empty, bound to the last
            # window's decisions for the SAME rubric text, and aggregated by the
            # pinned rule.
            final = reward_info.get("nl_rubrics")
            need(isinstance(final, list) and final, "judge_final_rubrics_missing_or_empty")
            for item in final:
                need(isinstance(item, dict), "judge_final_rubric_not_an_object")
                need(set(item) == {"nl_rubric", "met", "justification"},
                     "judge_final_rubric_fields_changed")
                need(isinstance(item["nl_rubric"], str) and item["nl_rubric"],
                     "judge_final_rubric_text_missing")
                need(isinstance(item["met"], bool), "judge_final_rubric_met_not_bool")
                need(isinstance(item["justification"], str),
                     "judge_final_rubric_justification_missing")
                need(item["nl_rubric"] in set(rubric_text.values()),
                     "judge_final_rubric_text_not_from_the_task_criteria")
            last_decisions = window_records[-1]["rubric_states"]
            by_text = {rubric_text[item["rubric_idx"]]: item["meetExpectation"]
                       for item in last_decisions}
            need({item["nl_rubric"] for item in final} == set(by_text),
                 "judge_final_rubrics_differ_from_the_last_window_decisions")
            for item in final:
                need(item["met"] == by_text[item["nl_rubric"]],
                     "judge_final_rubric_met_differs_from_the_last_window_decision")
            # The pinned aggregation, executed through the pinned classes.
            checks = [checks_class(nl_rubric=item["nl_rubric"], met=item["met"],
                                   justification=item["justification"]) for item in final]
            expected_reward = (1.0 if all(check.met for check in checks) and len(checks) > 0
                               else 0.0)
            need(reward_info.get("reward") == expected_reward,
                 "judge_reward_not_aggregated_by_the_pinned_rule")
            self.judge_chains[(arm, task_id)] = {
                "status": judge.get("status"), "reward": deepcopy(reward_info),
                "windows": window_records, "reward_expected": expected_reward,
                "pinned_modules": deepcopy(pinned["digests"]),
                "transcript_sha256": hashlib.sha256(
                    json.dumps(transcript, ensure_ascii=False).encode()).hexdigest()}
            # Published per phase, so a refusal in a LATER phase still states exactly
            # which phases were derived and what each of them found.
            self.report["judge_chains"] = [
                {"arm": chain_arm, "task": chain_task, "status": chain["status"],
                 "num_windows": len(chain["windows"]),
                 "reward_expected": chain["reward_expected"],
                 "windows": chain["windows"]}
                for (chain_arm, chain_task), chain in self.judge_chains.items()]
        self.report["judge_echo_differences"] = deepcopy(self.judge_echo_differences)
        self.report["judge_semantics"] = self._judge_semantics_record()
        self._checked("judge_gate")

    def _judge_semantics_record(self):
        """Which judge semantics this run was checked under, and what it found.

        The record keeps the two verdicts apart on purpose: the sealed protocol's own
        verbatim-echo requirement, and the explicit POST-HOC recheck of the pinned native
        rubric-by-id semantics. A collected echo difference is a strict-protocol failure
        even when the native recheck is satisfied.
        """
        native = self.native_judge_semantics
        return {
            "mode": JUDGE_SEMANTICS_NATIVE_BY_ID if native else "strict-verbatim-echo-v1",
            "protocol": JUDGE_SEMANTICS_NATIVE_BY_ID if native else None,
            "post_hoc": bool(native),
            "scope": ("post-hoc offline re-derivation of the pinned native rubric-by-id "
                      "update semantics over the same captured windows, prompts and "
                      "replies" if native else
                      "the sealed protocol's own verbatim rubric-echo requirement"),
            "pinned_rule": ("the pinned `TrajectoryEvaluator._evaluate_window` updates "
                            "`justification` and `meetExpectation` by `rubric_idx` and "
                            "never replaces the state's requirement text with the reply's"),
            "echo_differences": deepcopy(self.judge_echo_differences),
            "echo_difference_count": len(self.judge_echo_differences),
            "strict_requirement": "judge_decision_rubric_text_differs_from_the_criteria",
            "strict_protocol_failed_by_echo_differences": bool(self.judge_echo_differences),
            "phases_checked": len(self.judge_chains),
            "claim": ("the native scoring CHAIN is consistent with the pinned semantics; "
                      "this is not a claim that the scoring model's judgement is correct "
                      "or that the R/E hypothesis holds"),
        }

    def _environment_time_for(self, task_id):
        """The task's own environment time, from the fixed dataset (or None)."""
        criteria_holder = self._criteria.get(task_id)
        if criteria_holder is None:
            self._criteria_for(task_id)
        raw = getattr(self, "_env_times", None)
        if raw is None:
            raw = self._env_times = {}
        if task_id in raw:
            return raw[task_id]
        value = None
        try:
            dataset = self._dataset_users()
            number = int(task_id.rsplit("_", 1)[-1])
            value = (dataset[USER_INDEX]["subtasks"][number - 1]
                     .get("environment", {}).get("time"))
        except Exception:
            value = None
        raw[task_id] = value if isinstance(value, str) else None
        return raw[task_id]

    @staticmethod
    def _rubric_section(user_text):
        """Parse the `<current_rubrics>` JSON out of one judge user prompt.

        The section is the pinned `_format_current_rubrics` output, so every entry
        must carry the pinned state fields including the requirement text and the
        running justification; a section reduced to keys and booleans is refused.
        """
        match = re.search(r"<current_rubrics>\s*(?P<body>.*?)\s*</current_rubrics>",
                          user_text, re.S)
        need(match is not None, "judge_rubrics_section_unparseable")
        try:
            rubrics = json.loads(match.group("body"))
        except ValueError:
            raise AuditFailure("judge_rubrics_section_not_json") from None
        need(isinstance(rubrics, list) and rubrics, "judge_rubrics_section_not_a_list")
        states = []
        for item in rubrics:
            need(isinstance(item, dict), "judge_rubric_entry_changed")
            need(set(item) == {"rubric_idx", "rubric", "justification", "meetExpectation"},
                 "judge_rubric_entry_fields_changed")
            need(isinstance(item["rubric_idx"], str)
                 and isinstance(item["rubric"], str)
                 and isinstance(item["justification"], str)
                 and isinstance(item["meetExpectation"], bool),
                 "judge_rubric_entry_types_changed")
            states.append({"rubric_idx": item["rubric_idx"], "rubric": item["rubric"],
                           "justification": item["justification"],
                           "meetExpectation": item["meetExpectation"]})
        return states

    # -- auxiliary and cloud closure ---------------------------------------
    def auxiliary_gate(self):
        """Pair every auxiliary call to its native record BY VERIFIED ORDER.

        The authoritative order is the run's own: the verified phase execution order, and
        within a phase the order the native records were appended (the wrapper appends one
        record per native user/judge generation, and the phase's slice is that list). The
        real native records carry NO per-call time, so the audit does not invent one and
        does not sort by anything: the k-th unconsumed captured cloud call - in the proxy's
        own capture order - must answer the k-th native record. Same role, same request
        messages, same parameters, same response, inside the record's own phase window.

        Nothing is searched by content across the remainder, so a swapped pair or a
        repeated identical pair cannot be resolved by "finding a match somewhere". Two
        byte-identical records cannot be told apart beyond their position; that evidence
        boundary is stated instead of being papered over. If a record DOES carry a time
        (`recorded_at`), its consistency is checked rather than ignored: every record of a
        phase must carry one, the phase's sequence must be non-decreasing and each value
        must fall inside that phase's own capture window.
        """
        need(len(self.post_cloud_call) == len(self.posts), "agent_calls_not_all_bound")
        bound = {c["request_id"] for c in self.post_cloud_call.values()}
        need(len(bound) == len(self.post_cloud_call), "one_cloud_call_bound_twice")
        remaining = [c for c in self.chat_calls if c["request_id"] not in bound]
        # The authoritative order: the verified phase order, and each phase's own native
        # record order. No sort, no key, no content search.
        records = []
        for entry in sorted(self.phase_records.values(), key=lambda item: item["index"]):
            for call in entry["record"].get("native_calls_this_task") or []:
                records.append((entry, call))
        need(len(remaining) == len(records), "unaccounted_auxiliary_call_count")
        # The captured side is consumed in its own capture order, position by position.
        remaining = sorted(remaining, key=lambda call: call["start"])
        pairing = list(zip(records, remaining))
        remaining = list(remaining)
        windows = self._auxiliary_windows()
        recorded_at = self._native_record_times(records, windows)
        per_phase_position = {}
        for index, (entry, call) in enumerate(records):
            position = per_phase_position.get(entry["index"], 0)
            per_phase_position[entry["index"]] = position + 1
            match = pairing[index][1]
            remaining.remove(match)
            need(call.get("error") is None, "native_call_incomplete")
            request, response = call.get("request"), call.get("response")
            need(isinstance(request, dict) and isinstance(response, dict),
                 "native_call_incomplete")
            need(not request.get("tools"), "auxiliary_tools_not_supported")
            role = call.get("role")
            need(role in AUXILIARY_ROLES, "native_auxiliary_role_unexpected")
            # The captured dispatch role must be the native record's role.
            need(match.get("capture_role") == role, "capture_role_differs_from_native_role")
            expected = no_nulls(auxiliary_wire_messages(request.get("messages")))
            actual = no_nulls(match["body"].get("messages"))
            need(actual == expected, "auxiliary_request_not_at_its_capture_position")
            need(no_nulls(match["response"]) == no_nulls(response.get("raw_data")),
                 "auxiliary_response_not_at_its_capture_position")
            for key in ("model", "temperature", "max_tokens"):
                if key in request:
                    need(request[key] == match["body"].get(key), "auxiliary_parameters_changed")
            need("seed" not in match["body"], "auxiliary_seed_sent")
            need(match["body"].get("tools") in (None, []), "unexpected_auxiliary_tools")
            limit = match["body"].get("max_tokens", match["body"].get("max_completion_tokens"))
            need(limit == self.limits["auxiliary_output_tokens"], "wrong_auxiliary_output_limit")
            reasons = cloud_change_reasons(match["changes"], match["body"],
                                           profile_of(self.cloud_config), limit)
            need(not reasons, reasons[0] if reasons else "unexpected_cloud_change")
            window = windows.get((entry["arm"], entry["task"]))
            need(window is not None, "auxiliary_phase_window_missing")
            start, end = window
            need(start <= match["start"] and match["end"] <= end,
                 "auxiliary_call_outside_its_phase_window")
            stamp = (call or {}).get("recorded_at")
            if stamp is not None:
                try:
                    when = utc(stamp)
                except AuditFailure:
                    raise AuditFailure("native_call_time_not_a_utc_timestamp") from None
                need(start <= when <= end,
                     "native_call_time_outside_its_phase_window")
            self.report["auxiliary_mappings"].append({
                "request_id": match["request_id"], "role": role,
                "capture_role": match["capture_role"],
                "subtask_id": call.get("subtask_id"), "arm": entry["arm"],
                "task": entry["task"], "phase_index": entry["index"],
                "position_in_phase": position, "recorded_at": stamp,
                # The window bounds are RECORDED as their ISO form: the report is
                # serialized as JSON by the CLI entry points, and this mapping is the one
                # place a datetime would otherwise reach it.
                "window_start": start.isoformat(), "window_end": end.isoformat()})
        need(not remaining, "unaccounted_auxiliary_call_count")
        self.report["cloud_calls"]["auxiliary"] = len(records)
        self.report["cloud_calls"]["unaccounted"] = 0
        self.report["auxiliary_binding"] = {
            "order": ("verified phase order + within-phase native record order, bound "
                      "position by position to the proxy's capture order"),
            "records": len(records), "captured_calls": len(pairing),
            "native_records_with_a_time": sum(1 for _entry, call in records
                                              if (call or {}).get("recorded_at") is not None),
            "phase_time_sequences": recorded_at,
            "identical_records_cannot_be_told_apart_by_content": True,
        }
        counts = self.report["cloud_calls"]
        need(counts["agent"] + counts["auxiliary"] + counts["unaccounted"]
             == len(self.chat_calls), "unaccounted_agent_model_call")
        self._checked("auxiliary_gate")

    def _native_record_times(self, records, windows):
        """Consistency of `recorded_at` when the native records DO carry one.

        The real records carry no time at all; a record that does must be consistent with
        the order and the phase window it is bound by - a partial set, a non-monotonic
        sequence or a value outside its own phase's capture window is refused rather than
        ignored.
        """
        per_phase = {}
        for entry, call in records:
            stamp = (call or {}).get("recorded_at")
            per_phase.setdefault(entry["index"], []).append(stamp)
        summary = []
        seen_any = False
        for index, stamps in sorted(per_phase.items()):
            present = [stamp for stamp in stamps if stamp is not None]
            if present and len(present) != len(stamps):
                raise AuditFailure("native_call_times_partially_recorded")
            if not present:
                summary.append({"phase_index": index, "records": len(stamps),
                                "recorded_at": "absent"})
                continue
            seen_any = True
            try:
                values = [utc(stamp) for stamp in present]
            except AuditFailure:
                raise AuditFailure("native_call_time_not_a_utc_timestamp") from None
            need(values == sorted(values), "native_call_times_not_in_record_order")
            summary.append({"phase_index": index, "records": len(stamps),
                            "recorded_at": "present_and_non_decreasing",
                            "first": present[0], "last": present[-1]})
        if not seen_any:
            self.report["auxiliary_record_time_evidence"] = "absent_in_this_record"
        else:
            self.report["auxiliary_record_time_evidence"] = "verified_where_present"
        return summary


    def _auxiliary_windows(self):
        """Each phase's own capture-time window for its auxiliary calls.

        The window starts at the phase's first captured submission and ends at the
        capture time of the next phase's first submission (or the run's close for
        the final phase). A reply-after-stop or a final judging call therefore
        still falls inside its own phase's window.
        """
        ordered = sorted(self.phase_records.values(), key=lambda item: item["index"])
        starts = []
        for entry in ordered:
            record = entry["record"]
            first = record["post_slice"][0]
            rows_ = [r for r in self.posts if r.get("request_id") == first.get("request_id")]
            need(len(rows_) == 1, "phase_first_post_not_captured")
            starts.append(rows_[0]["timestamp"])
        close = [r for r in self.proxy if r.get("kind") == "cloud_close"]
        need(len(close) == 1, "cloud_close_missing")
        closes = [utc(c["timestamp"]) for c in close]
        windows = {}
        for index, entry in enumerate(ordered):
            start = utc(starts[index])
            end = utc(starts[index + 1]) if index + 1 < len(ordered) else closes[0]
            windows[(entry["arm"], entry["task"])] = (start, end)
        return windows

    def cloud_totals_gate(self):
        """The report totals are recomputed from the executed checks."""
        counts = self.report["cloud_calls"]
        need(counts["agent"] == len(self.posts), "agent_count_not_the_checked_posts")
        need(counts["auxiliary"] == len(self.report["auxiliary_mappings"]),
             "auxiliary_count_not_the_matched_calls")
        need(counts["unaccounted"] == 0, "unaccounted_calls_remain")
        self._checked("cloud_totals_gate")

    # -- separation and scores ---------------------------------------------
    def separation_gate(self):
        report = self.report
        ids = {arm: self._agent_id(arm) for arm in ARMS}
        need(all(isinstance(value, str) and value for value in ids.values()),
             "arm_agent_id_missing")
        need(ids["rewrite"] != ids["erratum"], "arms_share_one_agent")
        blocks = {arm: (self.result["arms"].get(arm) or {}).get("block_id") for arm in ARMS}
        need(blocks["rewrite"] != blocks["erratum"], "arms_share_one_block")
        initials = {arm: (self.result["arms"].get(arm) or {}).get("initial_block") for arm in ARMS}
        need(initials["rewrite"] == initials["erratum"], "arms_started_from_different_blocks")
        need(initials["rewrite"] == self.plan["arms"]["rewrite"]["initial_block"],
             "executed_initial_block_differs_from_plan")
        for arm in ARMS:
            # Continuity witness: ONE agent identity for the whole arm. A phase
            # record may not carry a second identity.
            for task in (self.result["arms"].get(arm) or {}).get("tasks") or []:
                need("agent_id" not in task,
                     "task_record_redeclares_an_agent_identity_" + arm)
        report["separation"] = {"agent_ids": ids, "block_ids": blocks,
                                "identical_initial_block": True}
        # One identity per (arm, task): no phase may claim another arm's agent.
        for request_id, entry in self.post_task.items():
            need(entry["arm"] in ARMS, "post_phase_arm_changed")
        self._checked("separation_gate")

    def task_scores(self):
        report = self.report
        scores = []
        for entry in sorted(self.phase_records.values(), key=lambda item: item["index"]):
            record = entry["record"]
            judge = record.get("judge") or {}
            scores.append({"arm": entry["arm"], "task": entry["task"],
                           "phase_index": entry["index"],
                           "termination_reason": record.get("termination_reason"),
                           "judge_status": judge.get("status"),
                           "reward_info": judge.get("reward_info"),
                           "score_anomaly": judge.get("status") == "JUDGE_FAILED_ORDERS_RETAINED"})
        report["scope"]["task_scores"] = scores
        report["task_success"] = None
        self._checked("task_scores")
        return scores

    def verify(self):
        report = self.report
        try:
            if self.judge_semantics is not None \
                    and self.judge_semantics not in JUDGE_SEMANTICS_MODES:
                # An unknown value is refused, never silently treated as the default.
                self.fail("judge_semantics_option_unknown")
            self.load()
            # Before ANY gate (and therefore before any `vita.*` import): this run's own
            # Vita model configuration, established explicitly.
            self.vita_environment_gate()
            self.transport_gate()
            self.config_gate()
            self.provenance_gate()
            self.dataset_gate()
            # Before phase_gate: a capacity stop leaves a started phase unfinished,
            # which is exactly what the phase gate refuses, so this gate runs first and
            # reports the stop from its own evidence.
            self.capacity_gate()
            self.phase_gate()
            self.agent_posts()
            # Consume the cloud journal (it sets self.chat_calls), then bind each
            # agent POST to exactly one cloud call. The cloud walk proves every
            # proxy record is consumed - there is no second, weaker path.
            self.cloud_calls()
            self._bind_capture_roles()
            self.bind_cloud_calls()
            self.task_material_gate()
            self.wire_gate()
            self.runtime_user_gate()
            self.tool_mapping_gate()
            self.memory_gate()
            self.judge_gate()
            self.auxiliary_gate()
            self.cloud_totals_gate()
            self.separation_gate()
            self.task_scores()
            # The repair protocols are verified from the record's own bytes, and ONLY
            # for a run that declares them: a sealed 0.1-0.4 record never reaches it,
            # so no old protocol is loosened and no old verdict moves.
            self.repair_protocol_gate()
            for path, captured in report["sources"].items():
                if path == "proxy_journal" or path == str(self.config_path) \
                        or path == str(self.dataset_path):
                    continue
                current = Path(path)
                need(current.is_file(), "capture_disappeared_during_audit")
                need(current.stat().st_size == captured["bytes"],
                     "capture_size_changed_during_audit")
            if report.get("capacity_refusal_before_send"):
                report["input_audit_passed"] = False
                report["status"] = "INVALID"
                self.note("capacity_refusal_before_send")
            elif report["scope"]["partial"]:
                report["input_audit_passed"] = False
                report["status"] = "PARTIAL_NOT_VALID"
            elif self.judge_echo_differences:
                # The STRICT protocol's own requirement was not met (the reply did not
                # echo the task's requirement text verbatim). The post-hoc native
                # semantics recheck does NOT turn that into a pass: the run is reported
                # under its own status, the strict failure is named, and only the
                # separate `native_semantics_checks_passed` field records that the
                # pinned native scoring chain was re-derived and is consistent.
                report["input_audit_passed"] = False
                report["status"] = "STRICT_PROTOCOL_NOT_MET"
                self.note("judge_decision_rubric_text_differs_from_the_criteria")
            else:
                report["input_audit_passed"] = True
                report["status"] = "VALID"
            self.finalize_verdict()
        except (Exception, KeyboardInterrupt) as exc:
            if isinstance(exc, AuditFailure):
                code = str(exc)
            else:
                # An unexpected exception is a bounded failure, not a pass; its
                # type and location are recorded so the cause is diagnosable
                # without ever echoing a message body or a credential.
                import traceback as _traceback
                frames = _traceback.extract_tb(exc.__traceback__)
                where = (f"{Path(frames[-1].filename).name}:{frames[-1].lineno}"
                         if frames else "unknown")
                code = f"malformed_or_unavailable_capture_{type(exc).__name__}_{where}"
            self.note(code)
            report["input_audit_passed"] = False
            report["status"] = "INVALID"
            report["strict_protocol_passed"] = False
            report["native_semantics_checks_passed"] = False
            if report["judge_semantics"] is None:
                record = self._judge_semantics_record()
                record["ran"] = False
                record["refused_before_finishing"] = code
                report["judge_semantics"] = record
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        return report

    def finalize_verdict(self):
        """The two verdicts, derived from the checks that actually ran.

        `strict_protocol_passed` is the sealed protocol's own verdict and stays false
        whenever a rubric-echo difference was collected; `native_semantics_checks_passed`
        additionally requires the explicit POST-HOC mode, a COMPLETE phase set and every
        later gate. A documented partial replay of a sealed capture calls this too, so the
        two verdicts are computed by one implementation rather than described twice.
        """
        report = self.report
        report["strict_protocol_passed"] = (
            report["status"] == "VALID" and not self.judge_echo_differences)
        report["native_semantics_checks_passed"] = bool(
            self.native_judge_semantics
            and not report["scope"]["partial"]
            and not report.get("capacity_refusal_before_send")
            and len(self.judge_chains) == (report["scope"]["phase_count"] or 0)
            and report["judge_semantics"] is not None)
        if report["judge_semantics"] is None:
            report["judge_semantics"] = self._judge_semantics_record()
        return {"strict_protocol_passed": report["strict_protocol_passed"],
                "native_semantics_checks_passed":
                    report["native_semantics_checks_passed"]}


def audit_re_multiturn_inputs(run_dir, proxy_journal, *, config_path=None,
                              dataset_path=None, sealed_audit_copy=None,
                              judge_semantics=None, vita_model_config=None) -> dict:
    """Convenience entry point; always returns a report dict, never raises.

    `sealed_audit_copy` is the EXPLICIT offline re-audit of a sealed capture: the real
    runtime copy of the post-hoc audit module, whose digest the run record pins. Without
    it the strict behaviour is unchanged and a repaired audit module is refused.

    `vita_model_config` is THIS run's own `<run>/vita-models.json`, established before the
    pinned Vita package is imported; `judge_semantics` is the explicit POST-HOC judge
    protocol (`native-by-id-v1`), which never replaces the strict verdict.
    """
    return MultiturnInputAudit(run_dir, proxy_journal, config_path=config_path,
                               dataset_path=dataset_path,
                               sealed_audit_copy=sealed_audit_copy,
                               judge_semantics=judge_semantics,
                               vita_model_config=vita_model_config).verify()
