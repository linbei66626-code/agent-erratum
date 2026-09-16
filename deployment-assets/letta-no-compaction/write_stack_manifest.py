#!/usr/bin/env python3
"""Rebuild the STACKED checkout and write the stacked service manifest.

    .venv-vita/bin/python deployment-assets/letta-no-compaction/write_stack_manifest.py

The chain is explicit and every byte is checked, in this order:

1. the reviewed multicall patch is applied to the PINNED baseline (the same
   generator the multicall manifest uses), producing the multicall checkout;
2. the reviewed no-compaction patch is applied to THAT checkout;
3. the two helper files this round adds are copied from the staged sources in
   `deployment-assets/letta-no-compaction/files/` (they are NOT in the patch, so a
   stale checkout cannot pass as "the patch was applied");
4. the stacked manifest is written from the rebuilt bytes, and the service load
   gate (`ae_multicall.verify_stack_gate`) is required to verify it.

A checkout that does not equal this chain is refused instead of described.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(PROJECT))

import ae_multicall  # noqa: E402

WORKSPACE = PROJECT / ".ae-verify-src"
PINNED = WORKSPACE / "letta-v1"
MULTICALL = WORKSPACE / "letta-multicall-patch/letta-v1"
STACKED = WORKSPACE / "letta-no-compaction-patch/letta-v1"
MULTICALL_MANIFEST = PROJECT / "deployment-assets/letta-multicall/manifest.json"
NO_COMPACTION_MANIFEST = HERE / "manifest.json"
NO_COMPACTION_PATCH = HERE / "ae-no-compaction-letta.patch"
STAGED_FILES = HERE / "files"
OUT = HERE / "stack-manifest.json"

UPSTREAM_COMMIT = "56ba9c25552605eec89de8ed3dc6394b625c1993"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _apply_patch(patch, checkout):
    applied = subprocess.run(["git", "apply", str(patch)], cwd=checkout,
                             capture_output=True, text=True)
    if applied.returncode:
        raise SystemExit("the reviewed patch did not apply: "
                         + (applied.stderr.strip() or applied.stdout.strip())[:400])


def rebuild_stacked(*, pinned=None, multicall=None, stacked=None, workspace=None):
    """Rebuild the multicall checkout and the stacked checkout from the sources."""
    workspace = Path(workspace) if workspace else WORKSPACE
    pinned = Path(pinned) if pinned else workspace / "letta-v1"
    multicall = Path(multicall) if multicall else workspace / "letta-multicall-patch/letta-v1"
    stacked = Path(stacked) if stacked else workspace / "letta-no-compaction-patch/letta-v1"
    if not pinned.is_dir():
        raise SystemExit(f"the pinned baseline checkout is absent: {pinned}")
    for name, expected in ae_multicall.PATCH_BASELINE.items():
        if expected is None:
            continue
        target = pinned / name
        if not target.is_file():
            raise SystemExit(f"the pinned baseline is missing {name}")
        if sha256(target) != expected:
            raise SystemExit(f"the pinned baseline {name} is not the pinned upstream file")
    for target in (multicall, stacked):
        if target.exists():
            shutil.rmtree(target)
    shutil.copytree(pinned, multicall)
    _apply_patch(PROJECT / "deployment-assets/letta-multicall/ae-multicall-letta.patch",
                 multicall)
    shutil.copy(PROJECT / "deployment-assets/letta-multicall/files/letta/helpers"
                / "ae_multicall_compat.py",
                multicall / "letta/helpers/ae_multicall_compat.py")
    shutil.copytree(multicall, stacked)
    _apply_patch(NO_COMPACTION_PATCH, stacked)
    for name in ae_multicall.STACK_NEW_FILES:
        shutil.copy(STAGED_FILES / name, stacked / name)
    return {"pinned": str(pinned), "multicall": str(multicall), "stacked": str(stacked),
            "multicall_patch_sha256": sha256(
                PROJECT / "deployment-assets/letta-multicall/ae-multicall-letta.patch"),
            "no_compaction_patch_sha256": sha256(NO_COMPACTION_PATCH)}


def build_stack_manifest(*, pinned=None, multicall=None, stacked=None, project=None,
                         out=None, applied_to_live_server=False, verify=True):
    """Write the stacked manifest from the REBUILT checkouts. Refuses on any drift."""
    project = Path(project) if project else PROJECT
    multicall = Path(multicall) if multicall else MULTICALL
    stacked = Path(stacked) if stacked else STACKED
    pinned = Path(pinned) if pinned else PINNED
    for checkout in (multicall, stacked):
        if not checkout.is_dir():
            raise SystemExit(f"a checkout in the chain is absent: {checkout}")
    reviewed = json.loads(MULTICALL_MANIFEST.read_text(encoding="utf-8"))
    capacity = json.loads(NO_COMPACTION_MANIFEST.read_text(encoding="utf-8"))

    # The multicall files must be IDENTICAL in both checkouts and equal to the
    # reviewed multicall manifest: the stack rebuilds them, it does not redefine them.
    for name in ("letta/schemas/message.py", "letta/helpers/ae_multicall_compat.py"):
        expected = reviewed["patched_files"][name]["patched_sha256"]
        for checkout in (multicall, stacked):
            if sha256(checkout / name) != expected:
                raise SystemExit(
                    f"{checkout / name} is not the reviewed multicall file")
    # The no-compaction patch's OWN baseline must be the multicall checkout, and the
    # stacked bytes must be the patched bytes it declares.
    for name, entry in capacity["patched_files"].items():
        if sha256(multicall / name) != entry["baseline_sha256"]:
            raise SystemExit(
                f"the multicall checkout's {name} is not the no-compaction baseline")
        if sha256(stacked / name) != entry["patched_sha256"]:
            raise SystemExit(f"the stacked checkout's {name} is not the patched file")
    for name, entry in capacity["new_files"].items():
        source = project / entry["source"]
        if not source.is_file():
            raise SystemExit(f"a staged helper source is absent: {source}")
        if sha256(source) != entry["sha256"]:
            raise SystemExit(f"the staged helper {name} differs from its manifest entry")
        if sha256(stacked / name) != entry["sha256"]:
            raise SystemExit(f"the stacked helper {name} is not the staged source")
    manifest = {
        "schema": ae_multicall.STACK_SCHEMA,
        "profile_version": ae_multicall.STACK_PROFILE_VERSION,
        "note": ("The STACKED service checkout: the reviewed multicall patch and this "
                 "round's no-compaction capacity patch in ONE tree, which is what a "
                 "capacity run's service must import."),
        "verification": {
            "offline_chain_verified": True,
            "applied_to_live_server": bool(applied_to_live_server),
            "note": ("offline_chain_verified means the stacked bytes equal the pinned "
                     "baseline plus the reviewed multicall patch plus the reviewed "
                     "no-compaction patch plus this round's staged helper sources. "
                     "applied_to_live_server is only set once the running service itself "
                     "produced a stack load receipt; the audit requires that receipt and "
                     "never accepts a client-built one."),
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "upstream_commit": UPSTREAM_COMMIT,
        "patch_chain": [
            "deployment-assets/letta-multicall/ae-multicall-letta.patch",
            "deployment-assets/letta-no-compaction/ae-no-compaction-letta.patch",
        ],
        "multicall_manifest": str(MULTICALL_MANIFEST.resolve()),
        "no_compaction_manifest": str(NO_COMPACTION_MANIFEST.resolve()),
        "enabling": {
            "environment": ae_multicall.STACK_ENV_VAR,
            "value": ae_multicall.STACK_PROFILE_VERSION,
            "manifest_environment": ae_multicall.STACK_MANIFEST_ENV_VAR,
            "module_sha_environment": ae_multicall.MODULE_SHA_ENV_VAR,
            "receipt_environment": ae_multicall.STACK_RECEIPT_ENV_VAR,
            "strict_gate": ("the service startup entry calls "
                            "ae_multicall.verify_stack_gate() and resolves every changed "
                            "and new file through the real import machinery before Letta "
                            "is imported; a declared stack that does not verify stops the "
                            "process instead of running with less checking"),
            "unchanged": ("the multicall-only profile and every sealed protocol keep their "
                          "own manifest, checkout and receipt"),
        },
        "compat_module": {
            "path": "ae_multicall.py",
            "sha256": sha256(project / "ae_multicall.py"),
        },
        "baseline_checkout": str(pinned.resolve()),
        "multicall_checkout": str(multicall.resolve()),
        "letta_checkout": str(stacked.resolve()),
        "patch": {
            "path": str(NO_COMPACTION_PATCH.resolve()),
            "sha256": sha256(NO_COMPACTION_PATCH),
            "applies_with": "git apply",
            "patch_base": "the multicall-patched checkout root",
        },
        "sources": {
            name: {"module": module, "filename": filename}
            for name, (module, filename) in sorted(ae_multicall.STACK_SOURCES.items())
        },
        "patched_files": {
            name: sha256(stacked / name) for name in ae_multicall.STACK_PATCHED_FILES
        },
        "new_files": {
            name: sha256(stacked / name) for name in ae_multicall.STACK_NEW_FILES
        },
        "deployment_layout": {
            "service_checkout": ("the STACKED checkout, loaded under "
                                 f"{ae_multicall.STACK_ENV_VAR}="
                                 f"{ae_multicall.STACK_PROFILE_VERSION}"),
            "multicall_only_checkout": ("kept for the earlier multicall protocol; its "
                                        "manifest, checkout and receipt are unchanged"),
            "receipt": ("written by the SERVICE process after it resolved the files it "
                        "imports; a client-built receipt is never accepted"),
        },
    }
    target = Path(out) if out else OUT
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    if verify:
        # The structural contract, checked on the bytes that were just written.
        ae_multicall.read_stack_manifest(target)
        env = {ae_multicall.STACK_ENV_VAR: ae_multicall.STACK_PROFILE_VERSION,
               ae_multicall.STACK_MANIFEST_ENV_VAR: str(target),
               ae_multicall.MODULE_SHA_ENV_VAR: manifest["compat_module"]["sha256"]}
        if not ae_multicall.verify_stack_gate(env, module=ae_multicall):
            raise SystemExit("the written stack manifest does not verify against the "
                             "stacked checkout")
        # The file-set check through the REAL import machinery is exercised by
        # `tests/test_ae_no_compaction.py::PatchStackLoadGateTests`, which runs the
        # bootstrap gate itself; this only proves the manifest and the tree agree.
    return manifest


def refresh_no_compaction_manifest(*, manifest=None, multicall=None, stacked=None,
                                   staged=None, patch=None):
    """Record the REBUILT digests in the no-compaction manifest.

    The manifest's prose (the policy, the counting contract, the layout) is authored
    by hand; the DIGESTS are not. They are recomputed here from the rebuilt checkouts
    and the staged helper sources, so a helper edit that is not rebuilt into the
    checkout is visible as a refusal instead of as a stale number.
    """
    manifest = Path(manifest) if manifest else NO_COMPACTION_MANIFEST
    multicall = Path(multicall) if multicall else MULTICALL
    stacked = Path(stacked) if stacked else STACKED
    staged = Path(staged) if staged else STAGED_FILES
    patch = Path(patch) if patch else NO_COMPACTION_PATCH
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["patch"]["sha256"] = sha256(patch)
    for name in record["patched_files"]:
        baseline = multicall / name
        patched = stacked / name
        if not baseline.is_file() or not patched.is_file():
            raise SystemExit(f"the rebuilt checkouts are missing {name}")
        record["patched_files"][name]["baseline_sha256"] = sha256(baseline)
        record["patched_files"][name]["patched_sha256"] = sha256(patched)
    for name, entry in record["new_files"].items():
        source = staged / name
        if not source.is_file():
            raise SystemExit(f"the staged helper source is absent: {source}")
        if sha256(source) != sha256(stacked / name):
            raise SystemExit(f"the rebuilt checkout's {name} is not the staged source")
        entry["sha256"] = sha256(source)
    manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    parser.add_argument("--rebuild", action="store_true",
                        help="rebuild the multicall and stacked checkouts first, and "
                             "refresh the no-compaction manifest's digests from them")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--applied-to-live-server", action="store_true")
    args = parser.parse_args(argv)
    if args.rebuild:
        info = rebuild_stacked(workspace=args.workspace)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        refresh_no_compaction_manifest(
            multicall=Path(info["multicall"]), stacked=Path(info["stacked"]))
    manifest = build_stack_manifest(out=args.out,
                                    applied_to_live_server=args.applied_to_live_server)
    print(f"wrote {args.out}")
    for name, digest in manifest["patched_files"].items():
        print(f"  changed {name}: {digest}")
    for name, digest in manifest["new_files"].items():
        print(f"  added   {name}: {digest}")
    print(f"  compat module: {manifest['compat_module']['sha256']}")


if __name__ == "__main__":
    raise SystemExit(main())
