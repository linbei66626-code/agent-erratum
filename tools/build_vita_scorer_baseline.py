#!/usr/bin/env python3
"""Build the pinned Vita SCORING-MODULE baseline from the archived pinned source.

The multi-turn audit compares the scoring modules it actually imports with a
traced baseline instead of with their own freshly computed digests. That baseline
cannot come from the checkout under audit (that would be circular), so it is
derived from the archived pinned source tarball, whose OWN digest is recorded in
this declaration - the audit re-reads the archive and refuses a mismatch.

    tools/build_vita_scorer_baseline.py            # print the declaration
    tools/build_vita_scorer_baseline.py --write    # write it once (never overwrite)

Read-only: it never unpacks the archive onto disk and never touches a checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "deployment-assets/ae-vita-data-20260911-r1.tar.gz"
CHOICE = ROOT / "deployment-assets/vita-scorer-baseline.json"
ARCHIVE_MEMBER_PREFIX = "source/"
#: The scoring chain's modules: the evaluator itself, the message/simulation/task
#: data models it consumes, the response parser and the prompt template package.
MODULES = ("src/vita/evaluator/evaluator_traj.py",
           "src/vita/data_model/message.py",
           "src/vita/data_model/simulation.py",
           "src/vita/data_model/tasks.py",
           "src/vita/utils/utils.py",
           "src/vita/prompts/__init__.py")


def build(archive: Path = ARCHIVE) -> dict:
    raw = archive.read_bytes()
    modules = {}
    with tarfile.open(archive, "r:gz") as tar:
        for relative in MODULES:
            member = tar.extractfile(ARCHIVE_MEMBER_PREFIX + relative)
            if member is None:
                raise SystemExit(f"member is not a regular file: {relative}")
            data = member.read()
            modules[relative] = {"sha256": hashlib.sha256(data).hexdigest(),
                                 "bytes": len(data)}
    return {
        "schema": "ae-vita-scorer-baseline-1",
        "note": ("Pinned scoring-module baseline for the multi-turn input audit. The "
                 "digests are the ARCHIVED pinned source's own module bytes, so the "
                 "audit compares the modules it imports with a declaration instead of "
                 "with their own freshly computed digests."),
        "generated_by": "tools/build_vita_scorer_baseline.py",
        "archive": {"path": str(ARCHIVE.relative_to(ROOT)),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "member_prefix": ARCHIVE_MEMBER_PREFIX,
                    "pinned_revision": "f60169e89f30499cb7883f3dad76bd03facc908d"},
        "roots": ["$AE_VITA_BASELINE_ROOT", "$AE_VERIFY_ROOT", "$REPO"],
        "modules": modules,
        "environment": {"baseline_path": "AE_VITA_SCORER_BASELINE",
                        "archive_path": "AE_VITA_BASELINE_ARCHIVE",
                        "root_path": "AE_VITA_BASELINE_ROOT"},
        "strict": ("the audit requires the archive to be present and to match "
                   "archive.sha256, extracts each module's bytes and compares them with "
                   "the imported file's bytes; a missing archive, a changed archive, a "
                   "changed module file or a module imported from another checkout is "
                   "refused"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="write the declaration; refuses to overwrite an existing file")
    parser.add_argument("--force", action="store_true",
                        help="with --write, replace an existing declaration (only after a "
                             "review of the printed difference)")
    args = parser.parse_args(argv)
    declaration = build()
    text = json.dumps(declaration, ensure_ascii=False, indent=2) + "\n"
    if not args.write:
        sys.stdout.write(text)
        return 0
    if CHOICE.exists() and not args.force:
        print(f"refusing to overwrite the existing baseline: {CHOICE}", file=sys.stderr)
        return 2
    CHOICE.write_text(text, encoding="utf-8")
    print(f"wrote {CHOICE} ({len(text.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
