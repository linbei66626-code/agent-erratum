#!/usr/bin/env python3
"""Stage the local t4 execution probe r3 delivery (native user conversation fix).

    .venv-vita/bin/python tools/stage_local_t4_probe_delivery.py

SAFETY: this refuses to touch an existing output directory, refuses to write anywhere
except its own r3 directory, and refuses to treat the reviewed r2 delivery as anything but
a READ-ONLY pre-image. An earlier version of this script destroyed the reviewed r1 delivery
because its OUT constant still pointed at r1; the guard below is load-bearing.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-local-t4-execution-probe-r2"
OUT = ROOT / "results/ae-local-t4-execution-probe-r3"
FILES = [
    ("scripts_ae_01_local_t4_probe.py", ROOT / "scripts/ae_01_local_t4_probe.py"),
    ("tests_test_ae_local_t4_probe.py", ROOT / "tests/test_ae_local_t4_probe.py"),
    ("tools_stage_local_t4_probe_delivery.py",
     ROOT / "tools/stage_local_t4_probe_delivery.py"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def guard() -> None:
    if OUT.resolve() == PREV.resolve():
        raise SystemExit("refusing to stage: OUT and PREV are the same directory")
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists "
                         "(this round writes a NEW directory only)")
    for _name, source in FILES:
        if not source.is_file():
            raise SystemExit(f"the source file is absent: {source}")


def main() -> int:
    guard()
    for directory in ("before", "after", "diffs"):
        (OUT / directory).mkdir(parents=True, exist_ok=False)
    table = []
    for index, (name, source) in enumerate(FILES, start=1):
        after = source.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        pre = PREV / "after" / name
        before = pre.read_bytes() if pre.is_file() else None
        note = "absent in the reviewed r2 delivery"
        if before is not None:
            (OUT / "before" / name).write_bytes(before)
            note = f"reviewed r2 after-copy: {pre.relative_to(ROOT)}"
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if before is None:
            diff_path.write_text(
                f"# {name}: NEW in this round (no pre-image)\n"
                f"# after sha256 {sha256(source)}\n", encoding="utf-8")
        else:
            diff_path.write_text("".join(difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}")), encoding="utf-8")
        table.append({"file": name, "repo_path": str(source.relative_to(ROOT)),
                      "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(source),
                      "changed": before != after, "baseline": note,
                      "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-local-t4-probe-diff-3",
         "note": ("r3 closes the native user conversation chain that r2 got wrong: the agent's "
                  "question now really enters the conversation and the NATIVE "
                  "UserSimulator.generate_next_message / state.flip_roles drives every "
                  "exchange, with the counted send gate as the only substituted transport. "
                  "The counting gate, role budgets, tools, prompts/oracle, model and service "
                  "window are unchanged."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
