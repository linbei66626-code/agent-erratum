#!/usr/bin/env python3
"""Stage the local 9B complete-information probe delivery (NEW files only).

    .venv-vita/bin/python tools/stage_local_order_probe_delivery.py

Both production files are new in this round, so there is no pre-image: the diff files say
so explicitly and record the after sha256. No earlier delivery is modified.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/ae-local-order-complete-info-probe-r1"
FILES = [
    ("scripts_ae_01_local_order_probe.py",
     ROOT / "scripts/ae_01_local_order_probe.py", None),
    ("tests_test_ae_local_order_probe.py",
     ROOT / "tests/test_ae_local_order_probe.py", None),
    # Included so the plan's own code_sha256 provenance can be re-checked from the delivery.
    ("tools_stage_local_order_probe_delivery.py",
     ROOT / "tools/stage_local_order_probe_delivery.py", None),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    for directory in ("after", "diffs"):
        (OUT / directory).mkdir(parents=True, exist_ok=True)
    table = []
    for index, (name, source, prior) in enumerate(FILES, start=1):
        if not source.is_file():
            raise SystemExit(f"the source file is absent: {source}")
        after = source.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        diff_path.write_text(
            f"# {name}: NEW in this round (no pre-image)\n"
            f"# repo path {source.relative_to(ROOT)}\n"
            f"# after sha256 {sha256(source)}\n", encoding="utf-8")
        table.append({"file": name, "repo_path": str(source.relative_to(ROOT)),
                      "before_sha256": None, "after_sha256": sha256(source),
                      "changed": True, "baseline": "absent before this round",
                      "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-local-order-probe-diff-1",
         "note": ("this round ADDS a local-only probe and its tests; it edits no existing "
                  "file, keeps the frozen native case/database/oracle and the cloud profiles "
                  "byte-identical, and does not claim the new case equals the old input"),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  new {row['file']}  {row['after_sha256'][:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
