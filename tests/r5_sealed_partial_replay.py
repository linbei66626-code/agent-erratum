"""LOCAL PARTIAL replay of a sealed multiturn audit over the REAL captured record.

Why a partial replay exists at all: a sealed run record names files by the ABSOLUTE paths
of the machine that recorded it (the pinned Vita checkout, the reviewed scorer-baseline
declaration) and by digests only that machine's copies have (its service manifests and the
launcher's stack receipt). Those files are not in the delivered capture, so the sealed
entry's front gates - provenance, the service-identity gates and the absolute-path halves
of `config_gate` - cannot be re-run off-site. What CAN be re-run off-site is everything
else: the whole 18-phase chain of the real record, through the same audit methods, in the
same order, with the same declarations.

This module therefore:

* bans sockets before anything else, so a replay is provably offline;
* establishes the run's OWN Vita model configuration through the sealed entry's own
  `prepare_vita_environment` (the real function, not a copy);
* re-derives the run's declared contract with the audit's own path-independent
  `_declare_run_contract`;
* substitutes ONLY the two absolute-path inputs, in memory, after proving the local files
  are the recorded bytes (`registry_sha256`, the scorer-baseline digest); no sealed file is
  edited, and the substitution is recorded in the result;
* runs the remaining gates in `verify()`'s exact order, recording each outcome, and stops
  at the first refusal exactly as `verify` does.

Nothing here is a mock of a gate: every gate that runs is the audit's own method over the
real capture. The result states which gates ran and which are left to the machine that
recorded the run.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(ROOT), str(ROOT / "tests")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

#: The gates a partial replay runs, in `verify()`'s order. The four that name files only
#: the recording machine has are listed in `UNCOVERED_GATES`.
COVERED_GATES = ("transport_gate", "dataset_gate", "capacity_gate", "phase_gate",
                 "agent_posts", "cloud_calls", "_bind_capture_roles", "bind_cloud_calls",
                 "task_material_gate", "wire_gate", "runtime_user_gate",
                 "tool_mapping_gate", "memory_gate", "judge_gate", "auxiliary_gate",
                 "cloud_totals_gate", "separation_gate", "task_scores")
UNCOVERED_GATES = ("config_gate (its declared-contract half IS run; the pinned-checkout, "
                   "service-manifest, multicall-receipt and stack-receipt halves need the "
                   "recording machine)",
                   "provenance_gate (needs the run's absolute Vita paths and the "
                   "site-owned manifest digests)",
                   "sealed auditor identity (needs plan/result agreement with the sealed "
                   "copy; covered separately by the targeted suite)")


def ban_network():
    """Refuse every socket connection attempt in this process."""
    import socket

    def blocked(*args, **kwargs):
        raise RuntimeError("offline network forbidden")

    socket.socket.connect = blocked
    socket.create_connection = blocked
    return "socket.socket.connect/create_connection replaced before any import of the audit"


def load_sealed_entry():
    spec = importlib.util.spec_from_file_location(
        "ae_r5_sealed_entry",
        ROOT / "scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replay(run_dir, proxy_journal, config_path, dataset_path, *, judge_semantics=None,
           vita_models=None, sealed_audit_copy=None, scorer_baseline=None,
           vita_source=None, gates=COVERED_GATES):
    """Run the remaining gates over the real record; return what each one did."""
    run_dir = Path(run_dir).resolve()
    proxy_journal = Path(proxy_journal).resolve()
    dataset_path = Path(dataset_path).resolve()
    config_path = Path(config_path).resolve() if config_path else None
    notes = [ban_network()]
    entry = load_sealed_entry()
    vita = entry.prepare_vita_environment(run_dir, Path(vita_models) if vita_models else None)
    notes.append("vita model configuration established by the sealed entry's own function")

    from ae_cloud_re_multiturn_input_audit import MultiturnInputAudit
    from ae_cloud_proxy import CloudConfig, PROFILES, profile_of
    audit = MultiturnInputAudit(run_dir, proxy_journal, config_path=config_path,
                                dataset_path=dataset_path,
                                sealed_audit_copy=sealed_audit_copy,
                                judge_semantics=judge_semantics,
                                vita_model_config=Path(vita["path"]))
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    substitutions = []
    outcomes = []
    audit.load()
    audit.vita_environment_gate()
    outcomes.append({"gate": "vita_environment_gate", "outcome": "PASSED"})
    # 1. The path-independent half of `config_gate`, through the audit's own method.
    opened = [row for row in audit.proxy if row.get("kind") == "cloud_open"]
    if len(opened) != 1:
        raise RuntimeError("the sealed journal does not carry exactly one cloud_open row")
    if opened[0].get("profile") not in PROFILES:
        raise RuntimeError("the sealed journal names an unknown proxy profile")
    cloud_config = CloudConfig(**opened[0]["config"])
    cloud_config.validate()
    declared_transport = profile_of(cloud_config)
    audit.cloud_config = cloud_config
    audit.declared_transport = declared_transport
    audit._declare_run_contract(plan["config"], cloud_config, declared_transport)
    outcomes.append({"gate": "config_gate.declared_contract", "outcome": "PASSED",
                     "note": "the audit's own path-independent declaration half"})
    # 2. The two absolute-path inputs, substituted in memory after proving the bytes.
    declared_source = (plan.get("provenance") or {}).get("vita_source") or {}
    recorded_path = declared_source.get("path")
    local_source = Path(vita_source) if vita_source else ROOT / ".ae-verify-src/source"
    if recorded_path and Path(recorded_path).resolve() != local_source.resolve():
        digest = _sha256(local_source / "src/vita/registry.py")
        if digest != declared_source.get("registry_sha256"):
            raise RuntimeError("the local pinned checkout is not the recorded one")
        substitutions.append({
            "input": "plan.provenance.vita_source.path", "recorded": recorded_path,
            "used_locally": str(local_source.resolve()),
            "proof": {"file": "src/vita/registry.py",
                      "sha256": digest,
                      "equals_recorded_registry_sha256": True}})
    audit.pinned_source = local_source.resolve()
    declared_dataset = plan.get("source") or {}
    if Path(declared_dataset.get("path") or "").resolve() != dataset_path:
        digest = _sha256(dataset_path)
        if digest != declared_dataset.get("sha256"):
            raise RuntimeError("the local dataset is not the recorded one")
        substitutions.append({
            "input": "plan.source.path", "recorded": declared_dataset.get("path"),
            "used_locally": str(dataset_path),
            "proof": {"sha256": digest, "equals_recorded_sha256": True}})
        audit.plan["source"] = dict(declared_dataset, path=str(dataset_path))
    baseline_declared = (plan.get("provenance") or {}).get("vita_scorer_baseline") or {}
    local_baseline = (Path(scorer_baseline) if scorer_baseline
                      else ROOT / "deployment-assets/vita-scorer-baseline.json")
    if Path(baseline_declared.get("path") or "").resolve() != local_baseline.resolve():
        digest = _sha256(local_baseline)
        if digest != baseline_declared.get("sha256"):
            raise RuntimeError("the local scorer baseline is not the recorded one")
        substitutions.append({
            "input": "plan.provenance.vita_scorer_baseline.path",
            "recorded": baseline_declared.get("path"),
            "used_locally": str(local_baseline.resolve()),
            "proof": {"sha256": digest, "equals_recorded_sha256": True}})
        audit.plan["provenance"]["vita_scorer_baseline"] = dict(
            baseline_declared, path=str(local_baseline.resolve()))
    # 3. The remaining gates, in verify()'s order, stopping at the first refusal.
    refusal = None
    for name in gates:
        method = (audit._bind_capture_roles if name == "_bind_capture_roles"
                  else getattr(audit, name))
        try:
            method()
        except BaseException as exc:  # noqa: BLE001 - a refusal is the outcome to record
            refusal = {"gate": name, "type": type(exc).__name__, "code": str(exc)}
            outcomes.append({"gate": name, "outcome": "REFUSED", **refusal})
            # The same thing `verify` does with a refusal: name it in the report.
            audit.note(refusal["code"])
            break
        outcomes.append({"gate": name, "outcome": "PASSED"})
    if refusal is None:
        # The same capture-stability walk `verify` performs after the gates.
        try:
            for path, captured in audit.report["sources"].items():
                if path in ("proxy_journal", str(config_path), str(dataset_path)):
                    continue
                current = Path(path)
                if not current.is_file():
                    raise RuntimeError("capture_disappeared_during_audit")
                if current.stat().st_size != captured["bytes"]:
                    raise RuntimeError("capture_size_changed_during_audit")
            outcomes.append({"gate": "capture_stability", "outcome": "PASSED"})
        except BaseException as exc:  # noqa: BLE001
            refusal = {"gate": "capture_stability", "type": type(exc).__name__,
                       "code": str(exc)}
            outcomes.append({"gate": "capture_stability", "outcome": "REFUSED", **refusal})
    # The verdicts, through the audit's OWN finaliser. The strict status is INVALID by
    # default here (this replay never claims the front gates passed); `finalize_verdict`
    # reports the strict-echo verdict and the post-hoc native-semantics verdict.
    audit.report["input_audit_passed"] = False
    audit.report["status"] = "INVALID"
    verdict = audit.finalize_verdict()
    report = audit.report
    return {
        "partial_replay": True,
        "note": ("LOCAL partial replay over the REAL sealed record: the remaining gates "
                 "ran as the audit's own methods, in verify()'s order. The front gates "
                 "that name files of the recording machine are NOT covered here and are "
                 "run by the sealed entry on that machine."),
        "offline": {"network_banned": True, "method": notes[0]},
        "run_dir": str(run_dir), "proxy_journal": str(proxy_journal),
        "vita_model_config": vita,
        "contract_derived_from": "the sealed plan config + the journal's own cloud_open row",
        "substituted_inputs": substitutions,
        "gates_run": [row["gate"] for row in outcomes],
        "gate_outcomes": outcomes,
        "refusal": refusal,
        "uncovered_gates": list(UNCOVERED_GATES),
        "scope": report["scope"], "cloud_calls": report["cloud_calls"],
        "judge_chains": len(report["judge_chains"]),
        "auxiliary_mappings": len(report["auxiliary_mappings"]),
        "judged_simulation_sources": report["judged_simulation_sources"],
        "native_conversions": report["native_conversions"],
        "runtime_generated_timestamps": report["runtime_generated_timestamps"],
        "auxiliary_binding": report.get("auxiliary_binding"),
        "auxiliary_record_time_evidence": report.get("auxiliary_record_time_evidence"),
        "judge_semantics": report.get("judge_semantics"),
        "judge_echo_differences": report.get("judge_echo_differences"),
        "judge_echo_difference_count": len(report.get("judge_echo_differences") or []),
        "strict_protocol_passed": report.get("strict_protocol_passed"),
        "native_semantics_checks_passed": report.get("native_semantics_checks_passed"),
        "verdict": verdict,
        "partial_replay_status": report["status"],
        "invalid_reasons": report["invalid_reasons"],
        "checks_executed": list(report["checks_executed"]),
        "task_scores": report["scope"]["task_scores"],
    }
