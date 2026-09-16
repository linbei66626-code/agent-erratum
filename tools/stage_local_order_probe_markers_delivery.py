#!/usr/bin/env python3
"""Stage the local-probe run-marker fix delivery (single-point repair).

    .venv-vita/bin/python tools/stage_local_order_probe_markers_delivery.py

The pre-image of each file is the reviewed r1 delivery's after-copy, so the diff is exactly
this round's edit and the r1 delivery keeps its own bytes untouched.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-local-order-complete-info-probe-r1"
OUT = ROOT / "results/ae-local-order-complete-info-probe-markers-fix-r1"
FILES = [
    ("scripts_ae_01_local_order_probe.py",
     ROOT / "scripts/ae_01_local_order_probe.py",
     ("prev", "scripts_ae_01_local_order_probe.py")),
    ("tests_test_ae_local_order_probe.py",
     ROOT / "tests/test_ae_local_order_probe.py",
     ("prev", "tests_test_ae_local_order_probe.py")),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    for directory in ("before", "after", "diffs"):
        (OUT / directory).mkdir(parents=True, exist_ok=True)
    table = []
    for index, (name, path, prior) in enumerate(FILES, start=1):
        if not path.is_file():
            raise SystemExit(f"the source file is absent: {path}")
        after = path.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        before = None
        note = "absent before this round"
        if prior is not None:
            source = PREV / "after" / prior[1]
            if not source.is_file():
                raise SystemExit(f"the pre-image is absent: {source}")
            before = source.read_bytes()
            (OUT / "before" / name).write_bytes(before)
            note = f"previous round's after-copy: {source.relative_to(ROOT)}"
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if before is None:
            diff_path.write_text(f"# {name}: NEW in this round (no pre-image)\n"
                                 f"# after sha256 {sha256(path)}\n", encoding="utf-8")
            changed = True
        else:
            completed = subprocess.run(
                ["diff", "-u", "--label", f"before/{name}", "--label", f"after/{name}",
                 str(OUT / "before" / name), str(OUT / "after" / name)],
                capture_output=True, text=True)
            changed = completed.returncode != 0
            diff_path.write_text(completed.stdout, encoding="utf-8")
            if not changed:
                diff_path.unlink()
        table.append({"file": name, "repo_path": str(path.relative_to(ROOT)),
                      "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
                      "after_sha256": hashlib.sha256(after).hexdigest(),
                      "changed": changed, "baseline": note,
                      "diff": None if not changed else str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-local-order-probe-markers-diff-1",
         "note": ("this round edits ONLY the local probe's run markers (model_called "
                  "semantics, fixture_used capture, requests_sent ambiguity) and one "
                  "targeted regression; the prompts, the exact oracle, the payment-batch "
                  "stop and the loopback boundary are unchanged"),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
