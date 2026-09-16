#!/usr/bin/env python3
"""Stage the history-continuation phase-fix delivery r1.

    .venv-vita/bin/python tools/stage_deepseek_re_history_phase_r1_delivery.py

Baseline: the re-audit identity delivery's copy of the audit module; the suite is new in
this round. The output directory is exclusive and no earlier delivery is touched.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-deepseek-re-reaudit-identity-r1"
OUT = ROOT / "results/ae-deepseek-re-history-phase-r1"

FILES = [
    ("ae_cloud_re_multiturn_input_audit.py", "ae_cloud_re_multiturn_input_audit.py",
     PREV / "after/ae_cloud_re_multiturn_input_audit.py",
     "re-audit-identity delivery copy"),
    ("tests_test_ae_deepseek_re_history_phase.py",
     "tests/test_ae_deepseek_re_history_phase.py", None, "new in this round"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists")
    for _name, source, _baseline, _kind in FILES:
        if not (ROOT / source).is_file():
            raise SystemExit(f"the source file is absent: {source}")
    for directory in ("after", "diffs", "evidence"):
        (OUT / directory).mkdir(parents=True, exist_ok=False)
    table = []
    for index, (name, source, baseline, kind) in enumerate(FILES, start=1):
        path = ROOT / source
        after = path.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if baseline is None:
            before = None
            diff_path.write_text(f"# {name}: NEW in this round\n"
                                 f"# after sha256 {sha256(path)}\n", encoding="utf-8")
        else:
            if not baseline.is_file():
                raise SystemExit(f"the baseline is absent: {baseline}")
            before = baseline.read_bytes()
            diff_path.write_text("".join(difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}")), encoding="utf-8")
        table.append({"file": name, "repo_path": source,
                      "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(path), "changed": before != after,
                      "baseline_kind": kind, "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-deepseek-re-history-phase-diff-1",
         "note": ("The sealed re-audit of the completed capture passed the identity, stack, "
                  "budget and capacity gates and the first two wire frames, then refused the "
                  "THIRD Letta POST with `client_tools_differ_from_declared`. The real "
                  "capture answers TWO consecutive `memory_update` calls in the history phase "
                  "before the explicit `current_task` envelope appears, while the audit picked "
                  "the phase from the POST's POSITION inside the phase "
                  "(`\"history\" if index == 1 else \"task\"`) and therefore demanded the 19 "
                  "domain tools for a continuation the service correctly served with the "
                  "memory tool alone. This round makes the phase follow the arm's and task's "
                  "own VERIFIED envelopes: only `current_task` switches to the task phase, a "
                  "tool-return continuation (or a mid-task `runtime_user` reply) inherits the "
                  "verified phase, a continuation with no verified phase and a history "
                  "envelope after the task phase are refused, and every tool-schema and "
                  "tool-return check is unchanged. The run bytes, plan, result, model, driver, "
                  "service, scoring and the recorded code list are untouched; the existing "
                  "sealed_audit_copy entry and the new auditor identity are kept."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} ({row['baseline_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
