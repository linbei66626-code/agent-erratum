#!/usr/bin/env python3
"""Stage the Letta Agent non-thinking wiring delivery r1.

    .venv-vita/bin/python tools/stage_deepseek_letta_thinking_r1_delivery.py

The pre-images are the r3 delivery's own `after/` copies (that round shipped the patch,
both manifests and the launcher) plus "new in this round" for the acceptance suite. The
output directory is exclusive: an existing one is refused and no earlier delivery is
touched.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R3 = ROOT / "results/ae-deepseek-re-multiturn-integration-r3"
OUT = ROOT / "results/ae-deepseek-letta-thinking-r1"

FILES = [
    ("deployment_ae-no-compaction-letta.patch",
     "deployment-assets/letta-no-compaction/ae-no-compaction-letta.patch"),
    ("deployment_stack-manifest.json",
     "deployment-assets/letta-no-compaction/stack-manifest.json"),
    ("deployment_manifest.json",
     "deployment-assets/letta-no-compaction/manifest.json"),
    ("scripts_deployment_letta_local.py", "scripts/deployment/letta_local.py"),
    ("tests_test_ae_deepseek_letta_thinking.py",
     "tests/test_ae_deepseek_letta_thinking.py"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def baseline_for(name: str):
    if name.startswith("tests_test_ae_deepseek_letta_thinking"):
        return None, "new in this round", "no pre-image exists; first shipped here"
    path = R3 / "after" / name
    if not path.is_file():
        raise SystemExit(f"the r3 delivery has no {name!r} pre-image")
    return path.read_bytes(), "r3 delivery copy", "the r3 delivery's own after/ copy"


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists")
    for _name, source in FILES:
        if not (ROOT / source).is_file():
            raise SystemExit(f"the source file is absent: {source}")
    for directory in ("after", "diffs", "evidence"):
        (OUT / directory).mkdir(parents=True, exist_ok=False)
    table = []
    for index, (name, source) in enumerate(FILES, start=1):
        path = ROOT / source
        after = path.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        before, baseline_kind, note = baseline_for(name)
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if before is None:
            diff_path.write_text(f"# {name}: NEW in this round\n"
                                 f"# after sha256 {sha256(path)}\n", encoding="utf-8")
        else:
            diff_path.write_text("".join(difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}")), encoding="utf-8")
        table.append({"file": name, "repo_path": source,
                      "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(path), "changed": before != after,
                      "baseline_kind": baseline_kind, "baseline": note,
                      "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-deepseek-letta-thinking-diff-1",
         "note": ("The live RUN stopped at R t4's first history request: the proxy received "
                  "`deepseek-flash` WITHOUT `thinking` and refused it with "
                  "`declared_transport_field_missing`, so zero generation requests left for "
                  "the provider. NativeVita's `extra_body` wiring covers the native "
                  "user/judge config, NOT the Letta Agent's own provider request. This round "
                  "wires the DECLARED transport's required fields into the agent's request "
                  "construction: the service reads them from an explicit declaration handed "
                  "over by the launcher (the reviewed profile's own required fields, with "
                  "the wire model they belong to), applies them only to a request that "
                  "names that model, refuses a half declaration or a conflicting client "
                  "value instead of guessing, and applies them BEFORE the capacity gate so "
                  "the gate measures the request that is really sent. The proxy's required-"
                  "field check is NOT relaxed, the historical Qwen path is byte-identical, "
                  "and no prompt, R/E, capacity, budget or experiment definition is "
                  "touched. A service-side change means: rebuild the stack, restart the "
                  "service, and let the new instance write a NEW receipt."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} ({row['baseline_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
