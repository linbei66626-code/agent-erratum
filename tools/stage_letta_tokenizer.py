#!/usr/bin/env python3
"""Stage the official Qwen tokenizer for the Letta-side capacity gate.

The gate inside the pinned service must count the FINAL provider request, and the
provider expresses its hard window in its own tokens. The service process therefore
needs the same tokenizer implementation the offline report uses. To keep ONE source
of truth, this script COPIES `ae_multiturn_capacity.py`'s tokenizer into the Letta
patch bundle and records the digest of the copy's tokenizer section, so a drift
between the two is detectable by `tools/check_tokenizer_drift.py`.

Offline: reads local files only.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ae_multiturn_capacity.py"
TARGET = (ROOT / "deployment-assets/letta-no-compaction/files/letta/helpers/"
          "ae_qwen_tokenizer.py")
SECTION_START = "#: The official Qwen chat template's tool-framing preamble"
#: Everything up to the report layer: the BPE, the template renderer AND the shared
#: counting entry, so the service counts a request exactly as the report does.
SECTION_END = "\n\ndef _message_material("


def tokenizer_section(text: str) -> str:
    start = text.index(SECTION_START)
    end = text.index(SECTION_END, start)
    return text[start:end]


def main():
    if "--write" not in sys.argv:
        print("pass --write to (re)write the staged tokenizer")
        return 2
    source = SOURCE.read_text(encoding="utf-8")
    section = tokenizer_section(source)
    header = '''"""Official Qwen byte-level BPE + chat template, staged for the AE capacity gate.

This file is GENERATED from `ae_multiturn_capacity.py` in the project checkout by
`tools/stage_letta_tokenizer.py`; the two must stay identical. The service process
uses it to count the FINAL provider request, because the provider's hard window is
expressed in its own tokens and the pinned approximate counter can under-estimate
Chinese text by more than 2x.

The official `tokenizers`/`transformers` packages are not required: this is the
official algorithm over the official assets (vocab + merges + pre-tokenizer regex +
NFC normaliser + the template from `tokenizer_config.json`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import regex
import unicodedata


'''
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(header + section + "\n", encoding="utf-8")
    digest = hashlib.sha256(TARGET.read_bytes()).hexdigest()
    print(f"wrote {TARGET} ({len(TARGET.read_bytes())} bytes, sha256 {digest[:16]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
