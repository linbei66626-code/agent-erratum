#!/usr/bin/env python3
"""Write (or verify) a delivery's SHA256SUMS over every file it contains.

    .venv-vita/bin/python tools/write_sha256sums.py <delivery dir> [--check]

The manifest never lists itself. `--check` recomputes every line and reports the
first mismatch instead of rewriting anything.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def files(root: Path):
    return sorted(path for path in root.rglob("*")
                  if path.is_file() and path.name != "SHA256SUMS")


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        raise SystemExit(__doc__)
    root = Path(arguments[0]).resolve()
    check = "--check" in arguments
    if check:
        failures = 0
        for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ", 1)
            target = root / name[2:] if name.startswith("./") else root / name
            live = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
            if live != digest:
                failures += 1
                print(f"FAIL {name}: {live}")
        print(f"{len(files(root))} files checked, {failures} failures")
        return 1 if failures else 0
    lines = []
    for path in files(root):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  ./{path.relative_to(root).as_posix()}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {root / 'SHA256SUMS'} with {len(lines)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
