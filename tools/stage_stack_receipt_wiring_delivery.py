#!/usr/bin/env python3
"""Stage the stack-receipt WIRING delivery, mechanically.

    .venv-vita/bin/python tools/stage_stack_receipt_wiring_delivery.py

The pre-images are the bytes the previous round DELIVERED
(`results/ae-target-tokenizer-jinja-fix-r1/after/`) for the files it shipped, and the
r3 delivery's copies for the two production files it did not touch. Nothing here
rewrites an earlier delivery.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-target-tokenizer-jinja-fix-r1"
R3 = ROOT / "results/ae-multiturn-capacity-no-compaction-r3"
OUT = ROOT / "results/ae-stack-receipt-wiring-r1"

#: delivery name -> repo path, with the pre-image taken from the r3 delivery's after/.

# ---------------------------------------------------------------------------
# Pre-images for the two production files and the chain fixture: no stored copy of
# "the bytes before this round" exists for them, so the baseline is RECONSTRUCTED by
# reversing this round's recorded edits. `before-after.json` marks them as such.
# ---------------------------------------------------------------------------

def _reverse(text, new, old, label):
    if text.count(new) != 1:
        raise SystemExit(f"the recorded edit is not present exactly once: {label}")
    return text.replace(new, old)


def reconstruct_driver(text):
    text = _reverse(text, """from ae_multicall import (LAUNCH_RECEIPT_ENV_VAR, MANIFEST_ENV_VAR,  # noqa: E402
                          STACK_MANIFEST_ENV_VAR, STACK_RECEIPT_ENV_VAR)""",
                    "from ae_multicall import LAUNCH_RECEIPT_ENV_VAR, MANIFEST_ENV_VAR  # noqa: E402",
                    "import")
    text = _reverse(text, """               multicall_load_receipt=None, *, require_receipt=False, vita_source=None,
               patch_stack_manifest=None, patch_stack_load_receipt=None):""",
                    """               multicall_load_receipt=None, *, require_receipt=False, vita_source=None):""",
                    "signature")
    start = text.index("    Two service identities are supported and they are alternatives")
    end = text.index('    """\n    record = {')
    text = text[:start] + text[end:]
    start = text.index("    stack_manifest_declared = patch_stack_manifest")
    end = text.index('    if config.get("multicall_profile") is None:')
    text = text[:start] + text[end:]
    start = text.index("def _record_multicall_manifest(")
    end = text.index("def local_environment():")
    text = text[:start] + text[end:]
    text = _reverse(text, """    parser.add_argument("--patch-stack-manifest", type=Path, default=None,
                        help="the STACKED (multicall + no-compaction) service manifest; "
                             "required for a 0.3 capacity run")
    parser.add_argument("--patch-stack-load-receipt", type=Path, default=None,
                        help="the stacked service process's own patch-stack-load.json "
                             "(required for a 0.3 capacity run)")
""", "", "cli args")
    text = _reverse(text, """                          require_receipt=args.stage == "run",
                          vita_source=args.vita_source,
                          patch_stack_manifest=args.patch_stack_manifest,
                          patch_stack_load_receipt=args.patch_stack_load_receipt)""",
                    """                          require_receipt=args.stage == "run",
                          vita_source=args.vita_source)""", "call")
    return text


def reconstruct_audit(text):
    text = _reverse(text, """from ae_multicall import (CONFIG_KEY as MULTICALL_CONFIG_KEY, read_manifest,
                          read_stack_manifest, validate_stack_receipt,""",
                    """from ae_multicall import (CONFIG_KEY as MULTICALL_CONFIG_KEY, read_manifest,""",
                    "import")
    start = text.index("            # The 0.3 service identity is verified FIRST:")
    end = text.index("            receipt = provenance.get(\"multicall_live_loading\")")
    text = text[:start] + text[end:]
    text = _reverse(text, """            else:
                need(stack_verified
                     or self.result.get("status") != "RE_MULTITURN_COMPLETED_AUDIT_PENDING",
                     "executed_multicall_run_without_service_receipt")
                self.report["scope"]["multicall_receipt_verified"] = False
                if stack_verified:
                    self.report["scope"]["multicall_covered_by_stack_receipt"] = {
                        "stack_manifest": self.report["scope"]["patch_stack"]["path"],
                        "files": sorted(self.stack_manifest["patched_files"])}""",
                    """            else:
                need(self.result.get("status") != "RE_MULTITURN_COMPLETED_AUDIT_PENDING",
                     "executed_multicall_run_without_service_receipt")
                self.report["scope"]["multicall_receipt_verified"] = False""",
                    "receipt else-branch")
    start = text.index("    def stack_evidence_gate(self, provenance):")
    end = text.index("    def provenance_gate(self):")
    text = text[:start] + text[end:]
    return text


def reconstruct_chain(text):
    text = _reverse(text, """                     scoring_task="sub_U000828_4", config_path=None,
                     patch_stack_manifest=None, patch_stack_load_receipt=None):""",
                    """                     scoring_task="sub_U000828_4", config_path=None):""",
                    "signature")
    return _reverse(text, """    prov = cli.provenance(config, config_path, manifest_path, receipt_path,
                          vita_source=vita_source,
                          patch_stack_manifest=patch_stack_manifest,
                          patch_stack_load_receipt=patch_stack_load_receipt)""",
                    """    prov = cli.provenance(config, config_path, manifest_path, receipt_path,
                          vita_source=vita_source)""", "call")


FILES = [
    # (delivery name, repo path, ("prev"|"r3", prior file name) or None)
    ("scripts_ae_01_cloud_re_multiturn.py", ROOT / "scripts/ae_01_cloud_re_multiturn.py",
     ("reconstruct", reconstruct_driver)),
    ("ae_cloud_re_multiturn_input_audit.py", ROOT / "ae_cloud_re_multiturn_input_audit.py",
     ("reconstruct", reconstruct_audit)),
    ("tests_re_multiturn_chain.py", ROOT / "tests/re_multiturn_chain.py",
     ("reconstruct", reconstruct_chain)),
    ("tests_test_ae_stack_receipt_wiring.py",
     ROOT / "tests/test_ae_stack_receipt_wiring.py", None),
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
            kind, value = prior
            if kind == "reconstruct":
                before = value(after.decode("utf-8")).encode("utf-8")
                note = "reconstructed by reversing this round's recorded edits"
            else:
                root = PREV if kind == "prev" else R3
                source = root / "after" / value
                if not source.is_file():
                    raise SystemExit(f"the pre-image is absent: {source}")
                before = source.read_bytes()
                note = f"previous round's after-copy: {source.relative_to(ROOT)}"
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
            "diff": None if not changed else str(diff_path.relative_to(OUT)),
        })
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-stack-receipt-wiring-diff-1",
         "note": ("pre-images are the bytes the earlier rounds delivered: the 0.3 service "
                  "identity wiring is this round's only change"),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
