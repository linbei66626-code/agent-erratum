#!/usr/bin/env python3
"""Stage the DeepSeek multi-turn R/E integration r2 delivery.

    .venv-vita/bin/python tools/stage_deepseek_re_integration_r2_delivery.py

SAFETY: refuses to touch an existing output directory and never writes outside it.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-deepseek-re-multiturn-integration-r1"
OUT = ROOT / "results/ae-deepseek-re-multiturn-integration-r2"
FILES = [
    ("ae_deepseek_re_transport.py", ROOT / "ae_deepseek_re_transport.py"),
    ("ae_cloud_re_multiturn.py", ROOT / "ae_cloud_re_multiturn.py"),
    ("ae_cloud_proxy.py", ROOT / "ae_cloud_proxy.py"),
    ("ae_adapter.py", ROOT / "ae_adapter.py"),
    ("ae_cloud_input_audit.py", ROOT / "ae_cloud_input_audit.py"),
    ("ae_cloud_re_multiturn_input_audit.py",
     ROOT / "ae_cloud_re_multiturn_input_audit.py"),
    ("scripts_ae_01_cloud_re_multiturn.py",
     ROOT / "scripts/ae_01_cloud_re_multiturn.py"),
    ("scripts_deployment_letta_local.py", ROOT / "scripts/deployment/letta_local.py"),
    ("configs_ae-01__re-multiturn__deepseek-flash.serial-candidate.json",
     ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-candidate.json"),
    ("deployment_ae-no-compaction-letta.patch",
     ROOT / "deployment-assets/letta-no-compaction/ae-no-compaction-letta.patch"),
    ("deployment_ae_no_compaction.py",
     ROOT / "deployment-assets/letta-no-compaction/files/letta/helpers/ae_no_compaction.py"),
    ("deployment_stack-manifest.json",
     ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json"),
    ("deployment_manifest.json",
     ROOT / "deployment-assets/letta-no-compaction/manifest.json"),
    ("tests_test_ae_deepseek_re_chain.py", ROOT / "tests/test_ae_deepseek_re_chain.py"),
    ("tests_test_ae_deepseek_re_transport_driver.py",
     ROOT / "tests/test_ae_deepseek_re_transport_driver.py"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists")
    for _name, source in FILES:
        if not source.is_file():
            raise SystemExit(f"the source file is absent: {source}")
    for directory in ("after", "diffs"):
        (OUT / directory).mkdir(parents=True, exist_ok=False)
    table = []
    for index, (name, source) in enumerate(FILES, start=1):
        after = source.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        pre = PREV / "after" / name
        before = pre.read_bytes() if pre.is_file() else None
        note = ("no pre-image in r1 (r1 shipped this as a component only)"
                if before is None else "r1 delivery copy")
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if before is None:
            diff_path.write_text(f"# {name}: NEW or first shipped here\n"
                                 f"# after sha256 {sha256(source)}\n", encoding="utf-8")
        else:
            diff_path.write_text("".join(difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}")), encoding="utf-8")
        table.append({"file": name, "repo_path": str(source.relative_to(ROOT)),
                      "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(source), "changed": before != after,
                      "baseline": note, "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-deepseek-re-integration-diff-2",
         "note": ("r2 wires the DeepSeek provider through the WHOLE chain: the CLI opt-in, "
                  "the driver's 0.4 branch, the proxy profile with its pre-send byte gate, "
                  "the per-stage two-arm clarification bound to each agent/task/request, the "
                  "Letta service's model-specific byte-only policy (with the stack rebuilt "
                  "and the manifests refreshed), the launcher handing that policy to the "
                  "child, and the post-hoc audit accepting 0.4 against real evidence. The "
                  "sealed 0.1/0.2/0.3 protocols, their constants and their count bases are "
                  "NOT loosened, and the Qwen token path is unchanged."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
