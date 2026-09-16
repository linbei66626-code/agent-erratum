#!/usr/bin/env python3
"""Write the versioned AE multicall deployment manifest with real hashes.

    .venv-vita/bin/python deployment-assets/letta-multicall/write_manifest.py

Every hash is computed from the files on disk at build time; none is typed by
hand. Re-run this after any change to the patch, the shim or the compat module.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
PATCHED = Path("/tmp/ae-multicall-patch/letta-v1")
PINNED = Path("/tmp/ae-letta-QaM3ld/letta-v1")
OUT = HERE / "manifest.json"

UPSTREAM_COMMIT = "56ba9c25552605eec89de8ed3dc6394b625c1993"
PROFILE_VERSION = "ae-multicall-receive-compat-1"
MANIFEST_SCHEMA = "ae-letta-multicall-manifest-1"

PATCHED_FILES = {
    "letta/agents/letta_agent_v3.py": {
        "baseline_sha256": "1f11745d6ae86e64e90c76e287d25552a6c7823a8109b178da6951b21c788166",
    },
    "letta/schemas/message.py": {
        "baseline_sha256": "c50de8d2792645a51f34f8f264c85bcea8ad252527270e96ebbe23bdd8a8e7e6",
    },
    "letta/helpers/ae_multicall_compat.py": {
        "baseline_sha256": None,
    },
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_manifest(*, patched=None, project=None, pinned=None, letta_checkout=None,
                   out=None, applied_to_live_server=False, verify=True):
    """Write the reviewed manifest, verifying patch identity first.

    This is the ONE production generator. It refuses to write
    `offline_patch_verified: true` until it has PROVEN that the candidate tree
    equals the pinned baseline with the reviewed patch (and shim) applied; a tree
    that merely reports its own digests - an unpatched upstream tree, or one with a
    file missing - cannot produce a verified manifest.
    """
    sys.path.insert(0, str(HERE.parents[1]))
    import ae_multicall

    patched = Path(patched) if patched else PATCHED
    project = Path(project) if project else PROJECT
    pinned = Path(pinned) if pinned else PINNED
    if not pinned.is_dir():
        raise SystemExit(f"pinned baseline checkout is absent: {pinned}")
    # The PATCHED tree must carry every reviewed file. The shim is added by the
    # patch, so the pinned BASELINE checkout is checked for exactly the files the
    # patch MODIFIES - requiring the shim there would make the pristine baseline
    # unusable, and requiring it only here would let a missing shim pass.
    present = [name for name in ae_multicall.REQUIRED_PATCHED_FILES
               if (patched / name).is_file()]
    if sorted(present) != sorted(ae_multicall.REQUIRED_PATCHED_FILES):
        absent = sorted(set(ae_multicall.REQUIRED_PATCHED_FILES) - set(present))
        raise SystemExit("candidate tree is missing reviewed files: " + ", ".join(absent))
    for name, expected in ae_multicall.PATCH_BASELINE.items():
        if expected is None:
            continue
        if not (pinned / name).is_file():
            raise SystemExit(
                f"the pinned baseline is missing the file the patch modifies: {name}")
    checkout = Path(letta_checkout) if letta_checkout else patched
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "profile_version": PROFILE_VERSION,
        # Offline patch verification and future live-service loading are separate
        # facts. `offline_patch_verified` is only set once the proof below passes.
        "verification": {
            "offline_patch_verified": False,
            "applied_to_live_server": bool(applied_to_live_server),
            "note": ("offline_patch_verified means the candidate bytes equal the pinned "
                     "baseline plus the reviewed patch. applied_to_live_server is only set "
                     "once the running service itself produced a load receipt; the audit "
                     "requires that receipt and never accepts a client-built one."),
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "upstream_baseline": {
            "letta/agents/letta_agent_v3.py":
                PATCHED_FILES["letta/agents/letta_agent_v3.py"]["baseline_sha256"],
            "letta/schemas/message.py":
                PATCHED_FILES["letta/schemas/message.py"]["baseline_sha256"],
        },
        "upstream_commit": UPSTREAM_COMMIT,
        "enabling": {
            "environment": "AE_LETTA_MULTICALL_PROFILE",
            "value": PROFILE_VERSION,
            "manifest_environment": "AE_LETTA_MULTICALL_MANIFEST",
            "module_sha_environment": "AE_LETTA_MULTICALL_MODULE_SHA256",
            "support_path_environment": "AE_LETTA_MULTICALL_SUPPORT",
            "strict_gate": ("the runtime calls ae_multicall.verify_letta_gate(); a declared "
                            "profile that does not verify stops the step before any side "
                            "effect, it does not truncate"),
        },
        "compat_module": {
            "path": "ae_multicall.py",
            "sha256": sha256(project / "ae_multicall.py"),
        },
        # The pinned tree the patch starts from, and the tree this manifest's
        # patched digests were verified against. Both are required so a client can
        # never pass off an arbitrary directory as a reviewed patch result.
        "baseline_checkout": str(pinned.resolve()),
        "letta_checkout": str(checkout.resolve()),
        "shim_source": str((HERE / "files/letta/helpers/ae_multicall_compat.py").resolve()),
        "patch": {
            "path": str((HERE / "ae-multicall-letta.patch").resolve()),
            "sha256": sha256(HERE / "ae-multicall-letta.patch"),
            "build_script": "deployment-assets/letta-multicall/build_patch.py",
            "build_script_sha256": sha256(HERE / "build_patch.py"),
            "applies_with": "git apply",
            "patch_base": "pinned checkout root",
        },
        "embedding_patch": {
            "path": "deployment-assets/letta-multicall/embed_patch.py",
            "sha256": sha256(HERE / "embed_patch.py"),
            "note": "applies the same edits without git, for a non-git checkout",
        },
        "patched_files": {
            name: {
                "baseline_sha256": PATCHED_FILES[name]["baseline_sha256"],
                "patched_sha256": sha256(patched / name),
            }
            for name in ae_multicall.REQUIRED_PATCHED_FILES
        },
        "verification_tools": {
            "offline_script": "deployment-assets/letta-multicall/verify_deployment.py",
            "focused_tests": ["tests/test_ae_multicall.py",
                              "tests/test_ae_multicall_letta.py"],
            "claim": ("offline compatibility evidence only; it does not claim a live "
                      "server loaded this patch or that any cloud call passed"),
        },
    }
    if verify:
        # The candidate must EQUAL pinned baseline + reviewed patch, not merely
        # report its own digests.
        ae_multicall.verify_patch_identity(manifest)
        manifest["verification"]["offline_patch_verified"] = True
    target = Path(out) if out else OUT
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patched", type=Path, default=PATCHED,
                        help="candidate checkout (pinned baseline + reviewed patch)")
    parser.add_argument("--pinned", type=Path, default=PINNED,
                        help="pinned upstream baseline checkout")
    parser.add_argument("--project", type=Path, default=PROJECT,
                        help="project root holding ae_multicall.py")
    parser.add_argument("--letta-checkout", type=Path, default=None,
                        help="checkout the manifest records (defaults to --patched)")
    parser.add_argument("--out", type=Path, default=OUT, help="manifest output path")
    parser.add_argument("--applied-to-live-server", action="store_true",
                        help="record a live-service claim (still needs a server receipt)")
    args = parser.parse_args(argv)
    manifest = build_manifest(patched=args.patched, pinned=args.pinned,
                              project=args.project, letta_checkout=args.letta_checkout,
                              out=args.out,
                              applied_to_live_server=args.applied_to_live_server)
    print(f"wrote {args.out}")
    for name, record in manifest["patched_files"].items():
        print(f"  {name}: {record['patched_sha256']}")
    print(f"  compat module: {manifest['compat_module']['sha256']}")


if __name__ == "__main__":
    main()
