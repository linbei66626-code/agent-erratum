#!/usr/bin/env python3
"""Stage the sealed re-audit identity-separation delivery r1.

    .venv-vita/bin/python tools/stage_deepseek_re_reaudit_identity_r1_delivery.py

Baselines: the id-mapping delivery's copy for the audit module, and "new in this round" for
the separate sealed entry point and its suite. The strict audit CLI is deliberately NOT
shipped - it was restored byte-for-byte to the bytes the sealed run recorded. The output
directory is exclusive and no earlier delivery is touched.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-deepseek-re-id-mapping-r1"
OUT = ROOT / "results/ae-deepseek-re-reaudit-identity-r1"

FILES = [
    ("ae_cloud_re_multiturn_input_audit.py", "ae_cloud_re_multiturn_input_audit.py",
     PREV / "after/ae_cloud_re_multiturn_input_audit.py", "id-mapping delivery copy"),
    ("scripts_ae_01_cloud_re_multiturn_input_audit_sealed.py",
     "scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py", None, "new in this round"),
    ("tests_test_ae_deepseek_re_reaudit_identity.py",
     "tests/test_ae_deepseek_re_reaudit_identity.py", None, "new in this round"),
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
        {"schema": "ae-deepseek-re-reaudit-identity-diff-1",
         "note": ("Decision taken: AUDITOR IDENTITY SEPARATION, not a one-off waiver, and only "
                  "for the sealed re-audit entry. A run record pins the digest of every file "
                  "in the multiturn code set, and that set includes the post-hoc audit module, "
                  "so repairing the audit after the capture exists makes exactly that one "
                  "digest differ. The strict entry, the recorded code list and the strict CLI "
                  "are UNCHANGED (the CLI was restored byte-for-byte to the bytes the sealed "
                  "run recorded, e439ab38...); the new explicit entry "
                  "`scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py` requires the REAL "
                  "runtime copy of that one module, recomputes its digest, and demands that it "
                  "equal BOTH the plan's and the result's recorded digest - there is no ignore "
                  "list, every other run-identity file is still compared exactly, and a "
                  "missing copy, a wrong copy or disagreeing records are refused. The report "
                  "separates the two identities: the auditor's own code (entry point, audit "
                  "module, shared audit module, shared journal walk) by path and by the digest "
                  "of the code that really ran, plus the sealed copy's digest and the digest "
                  "the run recorded - the recorded hash is never presented as the executing "
                  "code, and no audit file is dropped from the provenance set."),
         "strict_cli_restored": {"file": "scripts/ae_01_cloud_re_multiturn_input_audit.py",
                                 "sha256": sha256(ROOT / "scripts/ae_01_cloud_re_multiturn_input_audit.py"),
                                 "note": "not shipped because it is byte-identical to the seal"},
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} ({row['baseline_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
