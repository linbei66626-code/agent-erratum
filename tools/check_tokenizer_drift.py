#!/usr/bin/env python3
"""Prove the staged Letta tokenizer is the project's tokenizer, byte for byte.

The service process and the offline report must count with the SAME implementation:
if they drift, the report's capacity conclusion no longer describes what the service
enforces. This check regenerates the tokenizer section from `ae_multiturn_capacity.py`
and compares it with the staged file's body, so a change to one without the other is
refused. Offline, read-only.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from stage_letta_tokenizer import TARGET, tokenizer_section, SOURCE  # noqa: E402

HEADER_END = 'import unicodedata\n\n\n'


def main():
    staged = TARGET.read_text(encoding="utf-8")
    body = staged[staged.index(HEADER_END) + len(HEADER_END):].rstrip("\n")
    expected = tokenizer_section(SOURCE.read_text(encoding="utf-8")).rstrip("\n")
    ok = body == expected
    print(f"staged : {TARGET} ({hashlib.sha256(TARGET.read_bytes()).hexdigest()[:16]})")
    print(f"project: {SOURCE} ({hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]})")
    print("tokenizer section identical:", ok)
    if not ok:
        print("run tools/stage_letta_tokenizer.py --write after reviewing the change",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
