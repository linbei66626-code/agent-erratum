#!/usr/bin/env python3
"""Stage the `t4-interface-clarified-v1` control delivery (r1).

    .venv-vita/bin/python tools/stage_t4_interface_clarified_delivery.py

SAFETY: refuses to touch an existing output directory and never writes outside it.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-cloud-t4-screen-r1"
OUT = ROOT / "results/ae-t4-interface-clarified-r1"
FILES = [
    ("t4_case_variants.py", ROOT / "t4_case_variants.py"),
    ("scripts_ae_01_cloud_t4_probe.py", ROOT / "scripts/ae_01_cloud_t4_probe.py"),
    ("tests_test_ae_t4_interface_clarified.py",
     ROOT / "tests/test_ae_t4_interface_clarified.py"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists")
    for _name, source in FILES:
        if not source.is_file():
            raise SystemExit(f"the source file is absent: {source}")
    for directory in ("after", "diffs"):
        (OUT / directory).mkdir(parents=True, exist_ok=False)
    table = []
    for index, (name, source) in enumerate(FILES, start=1):
        after = source.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        pre = PREV / "after" / name
        before = pre.read_bytes() if pre.is_file() else None
        note = "no pre-image in the reviewed cloud delivery (new in this round)"
        if before is not None:
            note = ("reviewed cloud-r1 after-copy, unchanged" if before == after
                    else "reviewed cloud-r1 after-copy")
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if before is None:
            diff_path.write_text(f"# {name}: NEW in this round (no pre-image)\n"
                                 f"# after sha256 {sha256(source)}\n", encoding="utf-8")
        elif before != after:
            diff_path.write_text("".join(difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}")), encoding="utf-8")
        else:
            diff_path.write_text(f"# {name}: unchanged from the reviewed cloud delivery\n"
                                 f"# sha256 {sha256(source)}\n", encoding="utf-8")
        table.append({"file": name, "repo_path": str(source.relative_to(ROOT)),
                      "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(source), "changed": before != after,
                      "baseline": note, "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-t4-interface-clarified-diff-1",
         "note": ("this round ADDS one opt-in case variant module and the explicit "
                  "--case-variant switch on the cloud entry. The DEFAULT path is unchanged: "
                  "its messages, its 19 tool schemas, its required list, the execution "
                  "bindings, the mode, the budget and the diagnosis are untouched."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
