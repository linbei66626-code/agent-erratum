#!/usr/bin/env python3
"""Stage the OpenRouter integration delivery from the working tree.

    .venv-vita/bin/python tools/stage_openrouter_delivery.py

Pre-images: the r3 delivery's stored copies where it shipped the file, and for the two
files it did not ship a pre-image reconstructed by reversing this round's recorded edits
(`tools/openrouter_reverse_edits.json`). `before-after.json` marks which is which.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R3 = ROOT / "results/ae-multiturn-capacity-no-compaction-r3"
OUT = ROOT / "results/ae-openrouter-integration-r1"
REVERSALS = json.loads((ROOT / "tools/openrouter_reverse_edits.json").read_text())

FILES = [
    ("ae_cloud_proxy.py", ROOT / "ae_cloud_proxy.py", ("r3", "ae_cloud_proxy.py")),
    ("ae_cloud_audit.py", ROOT / "ae_cloud_audit.py", ("r3", "ae_cloud_audit.py")),
    ("ae_cloud_input_audit.py", ROOT / "ae_cloud_input_audit.py",
     ("r3", "ae_cloud_input_audit.py")),
    ("ae_cloud_re_multiturn.py", ROOT / "ae_cloud_re_multiturn.py", ("reconstruct", None)),
    ("ae_cloud_re_multiturn_input_audit.py", ROOT / "ae_cloud_re_multiturn_input_audit.py",
     ("reconstruct", None)),
    ("configs_ae-01__re-multiturn__openrouter.capacity-250k-candidate.json",
     ROOT / "configs/ae-01__re-multiturn__openrouter.capacity-250k-candidate.json", None),
    ("tests_test_ae_openrouter_transport.py",
     ROOT / "tests/test_ae_openrouter_transport.py", None),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reconstruct(name: str, text: str) -> str:
    for new, old in REVERSALS[name]:
        if text.count(new) != 1:
            raise SystemExit(f"the recorded edit is not present exactly once in {name}")
        text = text.replace(new, old)
    return text


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
            kind, value = prior
            if kind == "reconstruct":
                before = reconstruct(name, after.decode("utf-8")).encode("utf-8")
                note = "reconstructed by reversing this round's recorded edits"
            else:
                source = R3 / "after" / value
                if not source.is_file():
                    raise SystemExit(f"the pre-image is absent: {source}")
                before = source.read_bytes()
                note = f"r3 delivery after-copy: {source.relative_to(ROOT)}"
            (OUT / "before" / name).write_bytes(before)
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
        table.append({
            "file": name, "repo_path": str(path.relative_to(ROOT)),
            "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "before_lines": None if before is None else before.decode("utf-8", "replace").count("\n"),
            "after_lines": after.decode("utf-8", "replace").count("\n"),
            "changed": changed, "baseline": note,
            "diff": None if not changed else str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-openrouter-integration-diff-1",
         "note": ("pre-images are the bytes this repository carried before this round; the "
                  "reconstructed ones are marked as such"),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
