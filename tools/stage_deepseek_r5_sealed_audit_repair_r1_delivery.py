#!/usr/bin/env python3
"""Stage the r5 sealed-result audit repair delivery r1.

    .venv-vita/bin/python tools/stage_deepseek_r5_sealed_audit_repair_r1_delivery.py

The output directory is exclusive: no earlier delivery is touched. Baselines are the
delivery copies that ARE the code the r5 run recorded (the post-hoc audit module) or the
nearest reviewed copy whose diff contains only this round's change.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/ae-deepseek-r5-sealed-audit-repair-r1"
WORK = Path("/tmp/ae-r5")

#: (delivery name, repo path, baseline, baseline kind)
FILES = [
    ("ae_cloud_re_multiturn_input_audit.py", "ae_cloud_re_multiturn_input_audit.py",
     ROOT / "results/ae-deepseek-re-history-phase-r1/after/ae_cloud_re_multiturn_input_audit.py",
     "the r5 RUNTIME copy of the audit module: the plan AND the result pin its digest "
     "(51ab42fa...), so this is the code that audited the sealed run"),
    ("scripts_ae_01_cloud_re_multiturn_input_audit_sealed.py",
     "scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py",
     ROOT / "results/ae-deepseek-re-reaudit-identity-r1"
          "/after/scripts_ae_01_cloud_re_multiturn_input_audit_sealed.py",
     "the sealed entry delivered with the re-audit identity round"),
    ("tests_test_ae_cloud_re_multiturn.py", "tests/test_ae_cloud_re_multiturn.py",
     ROOT / "results/ae-deepseek-re-multiturn-integration-r3"
          "/after/tests_test_ae_cloud_re_multiturn.py",
     "the integration-r3 copy; the diff contains only this round's fixture change"),
    ("tests_re_multiturn_chain.py", "tests/re_multiturn_chain.py",
     ROOT / "results/ae-deepseek-re-multiturn-integration-r3"
          "/after/tests_re_multiturn_chain.py",
     "the integration-r3 copy; the diff contains only this round's fixture change"),
    ("tests_test_ae_deepseek_r5_sealed_audit_repair.py",
     "tests/test_ae_deepseek_r5_sealed_audit_repair.py", None, "new in this round"),
    ("tests_r5_sealed_partial_replay.py", "tests/r5_sealed_partial_replay.py", None,
     "new in this round"),
]

NOTE = (
    "The sealed r5 capture (18 phases, 258 generation responses, 98 auxiliary calls) was "
    "refused by the offline audit for five interface reasons that were about the AUDIT's "
    "reading of a REAL record: (1) it imported the pinned Vita package without this run's "
    "`vita-models.json`, so `vita.config` fell back to the checkout's example config; (2) it "
    "looked for `snapshot.simulations`, which the real wrapper never produces "
    "(`completed`/`last_evaluation` instead), so every phase looked unjudged; (3) it demanded "
    "that the bridge transcript EQUAL the scored simulation, while the real `NativeVita.finish` "
    "converts each row through the pinned classes, prefixes the task id and generates a "
    "`timestamp` the transcript never carried; (4) it ordered auxiliary records by "
    "`recorded_at`, which the real native records do not have; (5) the sealed protocol's "
    "verbatim rubric-echo requirement is not met by six replies of three phases, while the "
    "pinned native evaluator never reads that text. This round fixes the five, keeps the "
    "STRICT protocol as the default (still refusing those six), and adds the explicit "
    "POST-HOC `native-by-id-v1` recheck that collects the echo differences, finishes all 18 "
    "phases and the closure gates, and reports `native_semantics_checks_passed` SEPARATELY - "
    "`input_audit_passed` stays false and no unconditional VALID is produced. The run records, "
    "the driver, NativeVita, the model/prompt/R-E definitions, the budget, the service patches, "
    "the manifests, the receipts, the strict CLI and the recorded code-identity list are "
    "untouched."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists")
    for _name, source, _baseline, _kind in FILES:
        if not (ROOT / source).is_file():
            raise SystemExit(f"the source file is absent: {source}")
    for _name, _source, baseline, _kind in FILES:
        if baseline is not None and not baseline.is_file():
            raise SystemExit(f"the baseline is absent: {baseline}")
    for directory in ("after", "before", "diffs", "evidence"):
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
            before = baseline.read_bytes()
            (OUT / "before" / name).write_bytes(before)
            diff_path.write_text("".join(difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}")), encoding="utf-8")
        table.append({"file": name, "repo_path": source,
                      "before_sha256": None if before is None
                      else hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(path), "changed": before != after,
                      "baseline_kind": kind, "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-deepseek-r5-sealed-audit-repair-diff-1", "note": NOTE,
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # The run-time copy of the audit module the sealed entry must be handed on site.
    sealed = ROOT / "results/ae-deepseek-re-history-phase-r1/after/ae_cloud_re_multiturn_input_audit.py"
    (OUT / "evidence/audit-module-at-r5-run.py").write_bytes(sealed.read_bytes())
    # What the LOCAL replay could not cover, with the exact paths and digests.
    for source, target in ((WORK / "site-full-entry-requirements.json",
                            "evidence/site-full-entry-requirements.json"),
                           (WORK / "local-partial-strict.json",
                            "evidence/real-r5-local-replay-strict.json"),
                           (WORK / "local-partial-native.json",
                            "evidence/real-r5-local-replay-native.json"),
                           (WORK / "targeted.log", "evidence/tests-targeted.log"),
                           (WORK / "regression.log", "evidence/tests-affected-regression.log")):
        if not source.is_file():
            raise SystemExit(f"the evidence file is absent: {source}")
        (OUT / target).write_bytes(source.read_bytes())
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} "
              f"({row['baseline_kind'][:60]}...)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
