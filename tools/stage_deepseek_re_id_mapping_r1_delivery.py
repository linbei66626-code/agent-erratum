#!/usr/bin/env python3
"""Stage the completed-run ID-mapping audit repair r1.

    .venv-vita/bin/python tools/stage_deepseek_re_id_mapping_r1_delivery.py

Baselines are the delivery copies that last shipped each file (the budget round for the
multiturn audit, r3 for the shared audit) plus "new in this round" for its suite. The
output directory is exclusive and no earlier delivery is touched.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "results/ae-deepseek-re-budget1024-r1"
R3 = ROOT / "results/ae-deepseek-re-multiturn-integration-r3"
OUT = ROOT / "results/ae-deepseek-re-id-mapping-r1"

FILES = [
    ("ae_cloud_input_audit.py", "ae_cloud_input_audit.py",
     R3 / "after/ae_cloud_input_audit.py", "r3 delivery copy"),
    ("ae_cloud_re_multiturn_input_audit.py", "ae_cloud_re_multiturn_input_audit.py",
     BUDGET / "after/ae_cloud_re_multiturn_input_audit.py",
     "budget-1024 delivery copy"),
    ("tests_test_ae_deepseek_re_id_mapping.py", "tests/test_ae_deepseek_re_id_mapping.py",
     None, "new in this round"),
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
        {"schema": "ae-deepseek-re-id-mapping-diff-1",
         "note": ("The completed run ae-deepseek-re-live-20260916-r4 (both arms 18/18, 337 "
                  "generation responses 200) failed its offline audit at "
                  "`assistant_history_changed`: the provider's tool-call ids come back in "
                  "the service's next request as their 29-character projections "
                  "(`letta/constants.py::TOOL_CALL_ID_MAX_LEN`), while the declared "
                  "multicall compatibility protocol promises to preserve ids whole. The "
                  "service that wrote this capture was launched WITHOUT that profile "
                  "(`launch.json` records `multicall_profile: null`), so the pinned "
                  "serializer's slice ran - a launch setting, not a data defect. This round "
                  "teaches the audit the REAL service behaviour under ONE bounded rule: a "
                  "history id is accepted when it IS one of this capture's own native ids, "
                  "or when it is EXACTLY the 29-character projection of exactly ONE of "
                  "them, on BOTH the call and the tool-return side; an unknown id, a shorter "
                  "prefix, an arbitrary replacement, an ambiguous projection, a parameter "
                  "change or a non-empty text change is still refused, and the sealed "
                  "protocols keep their own fixed-projection rule unchanged."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} ({row['baseline_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
