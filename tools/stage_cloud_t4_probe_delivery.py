#!/usr/bin/env python3
"""Stage the cloud single-t4 screening delivery (r1).

    .venv-vita/bin/python tools/stage_cloud_t4_probe_delivery.py

SAFETY: refuses to touch an existing output directory and never writes outside it. The
reviewed r3 delivery is used as a READ-ONLY pre-image for the files it already contains.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-local-t4-execution-probe-r3"
OUT = ROOT / "results/ae-cloud-t4-screen-r1"
FILES = [
    ("t4_flow.py", ROOT / "t4_flow.py"),
    ("scripts_ae_01_cloud_t4_probe.py", ROOT / "scripts/ae_01_cloud_t4_probe.py"),
    ("tests_test_ae_cloud_t4_probe.py", ROOT / "tests/test_ae_cloud_t4_probe.py"),
    ("configs_ae-01__cloud-t4__declared-identity.template.json",
     ROOT / "configs/ae-01__cloud-t4__declared-identity.template.json"),
    # r3's probe is REUSED as a library (not rewritten); shipping the reviewed copy here
    # makes the exact reused bytes part of this delivery.
    ("reused_scripts_ae_01_local_t4_probe.py", ROOT / "scripts/ae_01_local_t4_probe.py"),
    ("tests_test_ae_local_t4_probe.py", ROOT / "tests/test_ae_local_t4_probe.py"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists "
                         "(this round writes a NEW directory only)")
    for _name, source in FILES:
        if not source.is_file():
            raise SystemExit(f"the source file is absent: {source}")
    for directory in ("after", "diffs"):
        (OUT / directory).mkdir(parents=True, exist_ok=False)
    table = []
    for index, (name, source) in enumerate(FILES, start=1):
        after = source.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        # pre-image only exists for files the reviewed r3 delivery already carried
        candidates = [PREV / "after" / name,
                      PREV / "after" / name.replace("reused_", "")]
        before = next((p.read_bytes() for p in candidates if p.is_file()), None)
        note = "no pre-image in the reviewed r3 delivery (new in this round)"
        if before is not None:
            note = "reviewed r3 after-copy (byte-identical reuse)" if before == after else \
                   "reviewed r3 after-copy"
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
            diff_path.write_text(f"# {name}: unchanged from the reviewed r3 delivery\n"
                                 f"# sha256 {sha256(source)}\n", encoding="utf-8")
        table.append({"file": name, "repo_path": str(source.relative_to(ROOT)),
                      "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(source), "changed": before != after,
                      "baseline": note, "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-cloud-t4-screen-diff-1",
         "note": ("this round ADDS a cloud screening entry and the shared flow module the "
                  "r3 probe now uses. The cloud entry imports r3's task construction, 19 "
                  "tools, native user chain and deterministic diagnosis instead of copying "
                  "them; the loop itself moved to t4_flow.py, which r3 and the cloud entry "
                  "share. No frozen profile, dataset, score or capacity state is touched."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
