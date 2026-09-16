#!/usr/bin/env python3
"""EXPLICIT offline re-audit of a SEALED capture, with the auditor identity separated.

    .venv-vita/bin/python scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py \
        --run-dir <sealed run> --proxy-journal <sealed journal> \
        --config <the run's candidate> --dataset <absolute dataset path> \
        --sealed-audit-copy <the runtime copy of ae_cloud_re_multiturn_input_audit.py> \
        --output <NEW report file> \
        [--vita-models <run>/vita-models.json] [--judge-semantics native-by-id-v1]

Why this is a SEPARATE entry point (and not a flag on the strict one):

* a run record pins the digest of every file in the multiturn code set, and that set
  includes the POST-HOC audit module. Repairing the audit after the capture exists makes
  that one digest differ, so the strict entry correctly refuses the sealed run;
* the strict entry, the run driver and the recorded code list are deliberately left
  untouched - this script is a NEW file that is not part of any run identity;
* this entry requires the REAL runtime copy of the audit module, recomputes its digest and
  demands that it equal BOTH the plan's and the result's recorded digest. There is no
  ignore list: every other run-identity file is still compared exactly;
* the report states the auditor's OWN identity (this entry point, the audit module, the
  shared audit module and the shared journal walk) by path and by the digest of the code
  that really ran, plus the sealed copy's digest and the digest the run recorded - the
  recorded hash is never presented as the executing code.

Two things this entry establishes BEFORE the audit module (and therefore before any
`vita.*`) is imported:

* the run's OWN Vita model configuration. The pinned `vita.config` reads
  `VITA_MODEL_CONFIG_PATH` while it is imported and silently falls back to the checkout's
  `models.yaml`/`models.yaml.example` when it is unset - which is how the sealed run was
  refused with a missing API key. The file must therefore be `<run-dir>/vita-models.json`,
  its CONTENT must equal the run's declaration re-derived from the plan's own config, and
  this process must not already carry another configuration (an already-imported pinned
  package keeps the config it was built with, so a leftover variable cannot fix it);
* the explicit POST-HOC judge semantics, when one is requested, are validated here so an
  unknown value can never be read as the strict default.

An existing output is refused, so a sealed re-audit never overwrites an earlier report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The run-local file the pinned Vita package must be pointed at.
VITA_MODEL_CONFIG_NAME = "vita-models.json"


def prepare_vita_environment(run_dir: Path, declared: Path | None) -> dict:
    """Point the pinned Vita package at THIS run's own model config, or refuse.

    Called before `ae_cloud_re_multiturn_input_audit` (and so before any `vita.*`) is
    imported. The expected content is re-derived from the run's own plan through the SAME
    production helper the driver used to write it, so an arbitrary external config cannot
    be substituted, and the value this process already carries - if any - must be that
    same file.
    """
    if "vita" in sys.modules or "vita.config" in sys.modules:
        raise RuntimeError(
            "a pinned Vita package is already imported in this process: its model "
            "configuration cannot be changed afterwards; run this re-audit in a fresh "
            "process")
    plan_path = Path(run_dir) / "plan.json"
    if not plan_path.is_file():
        raise RuntimeError(f"the sealed plan is absent: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    config = plan.get("config")
    if not isinstance(config, dict):
        raise RuntimeError("the sealed plan declares no run config")
    path = Path(declared) if declared else Path(run_dir) / VITA_MODEL_CONFIG_NAME
    path = path.resolve()
    if path.parent != Path(run_dir).resolve() or path.name != VITA_MODEL_CONFIG_NAME:
        raise RuntimeError(
            "the Vita model configuration must be this run's own "
            f"{VITA_MODEL_CONFIG_NAME}, not {path}")
    if not path.is_file():
        raise RuntimeError(f"this run's Vita model configuration is absent: {path}")
    from ae_vita import native_model_config
    expected = native_model_config(config)
    document = json.loads(path.read_bytes().decode("utf-8"))
    if document != expected:
        raise RuntimeError(
            "this run's Vita model configuration does not match the config the plan "
            "declares (model, loopback base URL and the declared transport's required "
            "request fields)")
    existing = os.environ.get("VITA_MODEL_CONFIG_PATH")
    if existing and Path(existing).resolve() != path:
        raise RuntimeError(
            "this process already declares another VITA_MODEL_CONFIG_PATH: "
            f"{Path(existing).resolve()}")
    os.environ["VITA_MODEL_CONFIG_PATH"] = str(path)
    return {"path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "expected_from": "the sealed plan's own config",
            "content_matches_the_plan_declaration": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--proxy-journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="a NEW report file; an existing path is refused")
    parser.add_argument("--config", type=Path,
                        help="the run's capability config (the plan's provenance config)")
    parser.add_argument("--dataset", type=Path, required=True,
                        help="the fixed dataset, by ABSOLUTE path: the scoring gate "
                             "re-derives each task's rubric text from it")
    parser.add_argument("--sealed-audit-copy", type=Path, required=True,
                        help="the REAL runtime copy of ae_cloud_re_multiturn_input_audit.py "
                             "as it existed when the run was recorded; it is hashed and "
                             "recorded, never imported or executed")
    parser.add_argument("--vita-models", type=Path, default=None,
                        help="this run's own vita-models.json (default: <run-dir>/"
                             "vita-models.json); another file is refused")
    parser.add_argument("--judge-semantics", default=None,
                        help="explicit POST-HOC judge protocol for the rubric-echo "
                             "difference (native-by-id-v1); the strict verbatim-echo "
                             "protocol stays the default and its failure is always kept")
    args = parser.parse_args(argv)
    if args.judge_semantics is not None and args.judge_semantics != "native-by-id-v1":
        # Refused BEFORE the output is reserved, so a refusal leaves nothing behind.
        print("AE sealed re-audit refused an unknown --judge-semantics value",
              file=sys.stderr)
        return 2
    try:
        # Reserve before reading so an existing output never triggers any work.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("AE sealed re-audit refused to overwrite an existing output", file=sys.stderr)
        return 2
    try:
        vita = prepare_vita_environment(args.run_dir, args.vita_models)
    except Exception as exc:  # noqa: BLE001 - the entry refuses before any audit work
        os.close(fd)
        os.unlink(args.output)
        print(f"AE sealed re-audit refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    # Imported only now: this is the first point at which the pinned Vita package may be
    # imported, and it will read the configuration established above.
    from ae_cloud_re_multiturn_input_audit import audit_re_multiturn_inputs
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        report = audit_re_multiturn_inputs(
            args.run_dir, args.proxy_journal, config_path=args.config,
            dataset_path=args.dataset, sealed_audit_copy=args.sealed_audit_copy,
            judge_semantics=args.judge_semantics, vita_model_config=Path(vita["path"]))
        report["vita_model_config_declared_by_this_entry"] = vita
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"status": report["status"],
                      "transport_capture_checked": report["transport_capture_checked"],
                      "input_audit_passed": report["input_audit_passed"],
                      "strict_protocol_passed": report.get("strict_protocol_passed"),
                      "native_semantics_checks_passed":
                          report.get("native_semantics_checks_passed"),
                      "judge_semantics_mode": (report.get("judge_semantics") or {}).get("mode"),
                      "judge_echo_differences":
                          len(report.get("judge_echo_differences") or []),
                      "reaudit_mode": (report.get("reaudit") or {}).get("mode"),
                      "task_success": None, "scientific_result": None,
                      "invalid_reasons": report["invalid_reasons"]}, ensure_ascii=False))
    return 0 if report["input_audit_passed"] else 2


if __name__ == "__main__":  # pragma: no cover - manual run
    sys.exit(main())
