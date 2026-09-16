#!/usr/bin/env python3
"""Stage the DeepSeek multi-turn R/E integration delivery (r1).

    .venv-vita/bin/python tools/stage_deepseek_re_integration_delivery.py

SAFETY: refuses to touch an existing output directory and never writes outside it.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/ae-deepseek-re-multiturn-integration-r1"
FILES = [
    ("ae_deepseek_re_transport.py", ROOT / "ae_deepseek_re_transport.py"),
    ("configs_ae-01__re-multiturn__deepseek-flash.serial-candidate.json",
     ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-candidate.json"),
    ("tests_test_ae_deepseek_re_transport_driver.py",
     ROOT / "tests/test_ae_deepseek_re_transport_driver.py"),
    ("t4_case_variants.py", ROOT / "t4_case_variants.py"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists")
    for _name, source in FILES:
        if not source.is_file():
            raise SystemExit(f"the source file is absent: {source}")
    (OUT / "after").mkdir(parents=True, exist_ok=False)
    table = []
    for name, source in FILES:
        (OUT / "after" / name).write_bytes(source.read_bytes())
        table.append({"file": name, "repo_path": str(source.relative_to(ROOT)),
                      "after_sha256": sha256(source),
                      "baseline": "NEW in this round"})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-deepseek-re-integration-diff-1",
         "note": ("this round ADDS one integration module, one candidate config and one "
                  "targeted suite. NO existing file is edited: the sealed driver, its sealed "
                  "pacing/request-budget constants, the sealed capacity count bases, the "
                  "cloud proxy profile table, the Letta stack patch and every frozen "
                  "profile/config are byte-identical. The service-side capacity gate is "
                  "therefore NOT yet usable for this provider, and that blocker is recorded "
                  "rather than worked around."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  new {row['file']}  {row['after_sha256'][:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
