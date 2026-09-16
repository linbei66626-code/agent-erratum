#!/usr/bin/env python3
"""Apply the AE multicall patch to a pinned Letta checkout WITHOUT git.

    python deployment-assets/letta-multicall/embed_patch.py <checkout-root>

For a checkout that has no `.git` (or no `git` binary) this reproduces exactly
the bytes that `git apply ae-multicall-letta.patch` produces. It refuses to run
twice, refuses a checkout whose baseline files do not match the pinned hashes,
and refuses to touch any file outside the declared set.

Exit codes: 0 applied, 2 refused (already patched or wrong baseline).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from build_patch import (MESSAGE_EDITS, MESSAGE_IMPORT_NEW,  # noqa: E402
                         MESSAGE_IMPORT_OLD, AGENT_NEW, AGENT_OLD, EXPECTED_HEAD)

BASELINE_SHAS = {
    "letta/agents/letta_agent_v3.py":
        "1f11745d6ae86e64e90c76e287d25552a6c7823a8109b178da6951b21c788166",
    "letta/schemas/message.py":
        "c50de8d2792645a51f34f8f264c85bcea8ad252527270e96ebbe23bdd8a8e7e6",
}
SHIM_SOURCE = HERE / "files/letta/helpers/ae_multicall_compat.py"
SHIM_TARGET = "letta/helpers/ae_multicall_compat.py"
PATCHED_SHAS = {
    "letta/agents/letta_agent_v3.py":
        "702031b3dac0de25bb452bea710d207dee48c55dca94be79a1cfcde2d077ff74",
    "letta/schemas/message.py":
        "8e2f28d8fd24defede7ce8b97b66ac0ffb227d84065f24f16c989a2ee88529eb",
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"REFUSED: {path} is not the expected pinned baseline")
    path.write_text(text.replace(old, new), encoding="utf-8")


def main(argv) -> int:
    if len(argv) != 2:
        raise SystemExit(__doc__)
    root = Path(argv[1]).resolve()
    shim = root / SHIM_TARGET
    if shim.exists():
        print(f"REFUSED: already patched ({shim} exists)")
        return 2
    for name, want in BASELINE_SHAS.items():
        target = root / name
        if not target.is_file() or sha256(target) != want:
            print(f"REFUSED: {name} does not match the pinned baseline {want}")
            return 2
    git_dir = root / ".git"
    if git_dir.exists():
        try:
            head = __import__("subprocess").run(
                ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                check=True).stdout.strip()
        except Exception as exc:  # git present but unusable: fail closed
            print(f"REFUSED: cannot verify checkout revision: {exc}")
            return 2
        if head != EXPECTED_HEAD:
            print(f"REFUSED: checkout HEAD {head} != pinned {EXPECTED_HEAD}")
            return 2

    message = root / "letta/schemas/message.py"
    agent = root / "letta/agents/letta_agent_v3.py"
    replace_once(message, MESSAGE_IMPORT_OLD, MESSAGE_IMPORT_NEW)
    for old, new in MESSAGE_EDITS:
        replace_once(message, old, new)
    replace_once(agent, AGENT_OLD, AGENT_NEW)
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_bytes(SHIM_SOURCE.read_bytes())

    for name, want in PATCHED_SHAS.items():
        got = sha256(root / name)
        if got != want:
            print(f"REFUSED: post-patch {name} hashes to {got}, expected {want}")
            return 2
    print(f"APPLIED to {root}")
    for name, want in sorted(PATCHED_SHAS.items()):
        print(f"  {name}: {want}")
    print(f"  {SHIM_TARGET}: {sha256(shim)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
