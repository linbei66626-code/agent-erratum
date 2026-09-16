#!/usr/bin/env python3
"""Stage the tokenizer JINJA-PATH fix delivery, mechanically.

    .venv-vita/bin/python tools/stage_target_tokenizer_jinja_fix_delivery.py

The pre-images are the bytes the previous round DELIVERED
(`results/ae-target-tokenizer-reconciliation-r1/after/`), so this round's diffs are
exactly the Jinja-path work and nothing else. Earlier deliveries are read only.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-target-tokenizer-reconciliation-r1"
OUT = ROOT / "results/ae-target-tokenizer-jinja-fix-r1"

#: delivery name -> repo path, with the pre-image taken from the r3 delivery's after/.
FILES = [
    ("ae_multiturn_capacity.py", ROOT / "ae_multiturn_capacity.py",
     "ae_multiturn_capacity.py"),
    ("deployment-assets_letta-no-compaction_files_ae_qwen_tokenizer.py",
     ROOT / "deployment-assets/letta-no-compaction/files/letta/helpers/ae_qwen_tokenizer.py",
     "deployment-assets_letta-no-compaction_files_ae_qwen_tokenizer.py"),
    ("deployment-assets_letta-no-compaction_manifest.json",
     ROOT / "deployment-assets/letta-no-compaction/manifest.json",
     "deployment-assets_letta-no-compaction_manifest.json"),
    ("deployment-assets_letta-no-compaction_stack-manifest.json",
     ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json",
     "deployment-assets_letta-no-compaction_stack-manifest.json"),
    ("tools_reconcile_target_tokenizer.py", ROOT / "tools/reconcile_target_tokenizer.py",
     "tools_reconcile_target_tokenizer.py"),
    ("tests_test_ae_target_tokenizer.py", ROOT / "tests/test_ae_target_tokenizer.py",
     "tests_test_ae_target_tokenizer.py"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    for directory in ("before", "after", "diffs", "evidence"):
        (OUT / directory).mkdir(parents=True, exist_ok=True)
    (OUT / "before-after.json").unlink(missing_ok=True)
    table = []
    for index, (name, path, prior) in enumerate(FILES, start=1):
        if not path.is_file():
            raise SystemExit(f"the source file is absent: {path}")
        after = path.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        before = None
        note = "absent before this round"
        if prior is not None:
            source = PREV / "after" / prior
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
        table.append({
            "file": name, "repo_path": str(path.relative_to(ROOT)),
            "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "before_lines": None if before is None else before.decode("utf-8", "replace").count("\n"),
            "after_lines": after.decode("utf-8", "replace").count("\n"),
            "changed": changed, "baseline": note,
            "diff": None if not changed else str(diff_path.relative_to(OUT)),
        })
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-target-tokenizer-jinja-fix-diff-1",
         "note": ("pre-images are the bytes the previous target-tokenizer round shipped; "
                  "this round aligns the Jinja execution path with the pinned renderer"),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
