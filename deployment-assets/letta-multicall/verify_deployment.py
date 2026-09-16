#!/usr/bin/env python3
"""Offline verification of the AE multicall Letta patch and its manifest.

    .venv-vita/bin/python deployment-assets/letta-multicall/verify_deployment.py

Read-only with respect to the project: it inspects the pinned checkout, the
patched scratch checkout (rebuilt if missing), the patch file, the manifest and
the live compat module, and prints a JSON report.

What it proves:
  * the pinned baseline is the declared commit and has the declared file hashes;
  * the patch applies to a pristine copy of that baseline;
  * the patched files hash exactly to the manifest's patched hashes;
  * the live `ae_multicall.py` hashes to the manifest's compat-module hash;
  * the fail-closed gate returns True only for the exact reviewed environment and
    False for every weakened or incomplete one;
  * a patch run twice is refused, and a wrong baseline is refused.

What it does NOT prove, and will not claim: that any live Letta server loaded
this patch, that a cloud call ran, or that a task succeeded. There is no
network, no service, no model and no socket anywhere in this script.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(PROJECT))

import ae_multicall  # noqa: E402
from ae_multicall import (LETTA_ENV_VAR, MANIFEST_ENV_VAR, MANIFEST_SCHEMA,  # noqa: E402
                          MODULE_SHA_ENV_VAR, PROFILE_VERSION, UPSTREAM_BASELINE,
                          read_manifest, verify_letta_gate)

MANIFEST = HERE / "manifest.json"
PATCH = HERE / "ae-multicall-letta.patch"
BUILDER = HERE / "build_patch.py"
EMBEDDER = HERE / "embed_patch.py"
SHIM = HERE / "files/letta/helpers/ae_multicall_compat.py"
PATCHED = Path(os.environ.get("AE_LETTA_PATCHED_SOURCE", "/tmp/ae-multicall-patch/letta-v1"))
PINNED = Path(os.environ.get("AE_LETTA_SOURCE", "/tmp/ae-letta-QaM3ld/letta-v1"))
PATCHED = Path(os.environ.get("AE_LETTA_PATCHED_SOURCE", "/tmp/ae-multicall-patch/letta-v1"))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


def main():
    report = {"schema": "ae-letta-multicall-verify-1", "checks": {}, "failures": []}

    def check(name, ok, detail=""):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            report["failures"].append(name)
        return ok

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    check("manifest_schema", manifest.get("schema") == MANIFEST_SCHEMA)
    check("manifest_profile", manifest.get("profile_version") == PROFILE_VERSION)
    check("manifest_upstream_baseline",
          manifest.get("upstream_baseline") == UPSTREAM_BASELINE)

    check("patch_present", PATCH.is_file())
    check("patch_hash_matches_manifest",
          sha256(PATCH) == manifest["patch"]["sha256"], sha256(PATCH))
    check("builder_present", BUILDER.is_file())
    check("embedder_present", EMBEDDER.is_file())
    check("shim_source_present", SHIM.is_file())

    # 1. The pinned baseline is the declared commit with the declared bytes.
    check("pinned_checkout_present", (PINNED / "letta").is_dir(), str(PINNED))
    if (PINNED / ".git").is_dir():
        head = run(["git", "rev-parse", "HEAD"], cwd=PINNED).stdout.strip()
        check("pinned_commit", head == manifest["upstream_commit"], head)
    for name, want in manifest["upstream_baseline"].items():
        got = sha256(PINNED / name) if (PINNED / name).is_file() else None
        check("baseline_sha:" + name, got == want, str(got))

    # 2. The patch applies to a pristine copy, and the result hashes as declared.
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "letta-v1"
        shutil.copytree(PINNED, candidate, symlinks=True)
        applied = run(["git", "apply", "--check", str(PATCH)], cwd=candidate)
        check("patch_applies_cleanly", applied.returncode == 0,
              applied.stderr.strip()[:400])
        if applied.returncode == 0:
            run(["git", "apply", str(PATCH)], cwd=candidate)
            shutil.copy2(SHIM, candidate / "letta/helpers/ae_multicall_compat.py")
            for name, record in manifest["patched_files"].items():
                got = sha256(candidate / name)
                check("patched_sha:" + name, got == record["patched_sha256"], str(got))
            # 3. A second application must be refused.
            twice = run([sys.executable, str(EMBEDDER), str(candidate)],
                        cwd=PROJECT)
            check("second_apply_refused", twice.returncode == 2,
                  twice.stdout.strip().splitlines()[-1:] or [""])
        # 4. A wrong baseline must be refused.
        broken = Path(tmp) / "broken"
        shutil.copytree(PINNED, broken, symlinks=True)
        (broken / "letta/schemas/message.py").write_text("not the baseline\n",
                                                        encoding="utf-8")
        refused = run([sys.executable, str(EMBEDDER), str(broken)], cwd=PROJECT)
        check("wrong_baseline_refused", refused.returncode == 2,
              refused.stdout.strip().splitlines()[-1:] or [""])

    # 5. The live compat module is the reviewed one.
    live = ae_multicall.live_module_sha()
    check("live_module_hash", live == manifest["compat_module"]["sha256"], live)
    try:
        record = read_manifest(MANIFEST)
        check("manifest_readable_by_the_runtime_gate", True)
        check("manifest_module_hash_agrees",
              record["compat_module"]["sha256"] == manifest["compat_module"]["sha256"])
    except Exception as exc:  # noqa: BLE001
        record = {}
        check("manifest_readable_by_the_runtime_gate", False,
              f"{type(exc).__name__}: {exc}")

    # 6. The fail-closed gate matrix, using the real implementation.
    good = {LETTA_ENV_VAR: PROFILE_VERSION, MODULE_SHA_ENV_VAR: live,
            MANIFEST_ENV_VAR: str(MANIFEST)}
    check("gate_enabled_with_reviewed_env", verify_letta_gate(good) is True)
    weakened = {
        "wrong value": {**good, LETTA_ENV_VAR: "ae-multicall-receive-compat-999"},
        "unset value": {**good, LETTA_ENV_VAR: ""},
        "missing module hash": {k: v for k, v in good.items() if k != MODULE_SHA_ENV_VAR},
        "wrong module hash": {**good, MODULE_SHA_ENV_VAR: "0" * 64},
        "short module hash": {**good, MODULE_SHA_ENV_VAR: "abc"},
        "missing manifest": {k: v for k, v in good.items() if k != MANIFEST_ENV_VAR},
        "bad manifest path": {**good, MANIFEST_ENV_VAR: "/nonexistent/manifest.json"},
    }
    for label, environ in weakened.items():
        check("gate_refuses:" + label, verify_letta_gate(environ) is False)

    # 6b. The manifest must PROVE patch identity: pinned baseline + reviewed patch.
    try:
        record = read_manifest(MANIFEST)
        check("required_file_set",
              set(record["patched_files"]) == set(ae_multicall.REQUIRED_PATCHED_FILES),
              sorted(record["patched_files"]))
        applied = ae_multicall.verify_patch_identity(record)
        check("patch_identity_applied_equals_manifest",
              all(record["patched_files"][name]["patched_sha256"] == digest
                  for name, digest in applied.items()))
    except Exception as exc:  # noqa: BLE001
        check("patch_identity_proof", False, f"{type(exc).__name__}: {exc}")

    # 6c. The generator must REFUSE to certify an unpatched or incomplete tree.
    with tempfile.TemporaryDirectory() as tmp:
        unpatched = Path(tmp) / "unpatched"
        shutil.copytree(PINNED, unpatched, symlinks=True)
        shutil.copy2(SHIM, unpatched / "letta/helpers/ae_multicall_compat.py")
        refused = run([sys.executable, str(BUILDER.parent / "write_manifest.py"),
                       "--patched", str(unpatched), "--out", str(Path(tmp) / "bad.json")],
                      cwd=PROJECT)
        check("generator_refuses_unpatched_tree", refused.returncode != 0,
              (refused.stderr or refused.stdout).strip()[-160:])
        partial = Path(tmp) / "partial"
        shutil.copytree(PATCHED, partial, symlinks=True)
        (partial / "letta/schemas/message.py").unlink()
        refused = run([sys.executable, str(BUILDER.parent / "write_manifest.py"),
                       "--patched", str(partial), "--out", str(Path(tmp) / "bad2.json")],
                      cwd=PROJECT)
        check("generator_refuses_incomplete_tree", refused.returncode != 0,
              (refused.stderr or refused.stdout).strip()[-160:])

    # 7. Tampered manifests and the runtime gate's structural rules.
    with tempfile.TemporaryDirectory() as tmp:
        for label, mutate in (
            ("tampered module digest",
             lambda m: m["compat_module"].__setitem__("sha256", "1" * 64)),
            ("empty patched_files", lambda m: m.__setitem__("patched_files", {})),
            ("one patched file only",
             lambda m: m.__setitem__("patched_files", {
                 "letta/agents/letta_agent_v3.py":
                     m["patched_files"]["letta/agents/letta_agent_v3.py"]})),
            ("wrong baseline digest", lambda m: m["patched_files"][
                "letta/agents/letta_agent_v3.py"].__setitem__("baseline_sha256", "0" * 64)),
            ("patched equals baseline", lambda m: m["patched_files"][
                "letta/agents/letta_agent_v3.py"].__setitem__(
                    "patched_sha256",
                    m["patched_files"]["letta/agents/letta_agent_v3.py"]["baseline_sha256"])),
            ("unknown schema", lambda m: m.__setitem__("schema", "unknown")),
            ("missing letta_checkout", lambda m: m.pop("letta_checkout")),
            ("missing baseline_checkout", lambda m: m.pop("baseline_checkout")),
            ("patched file digest", lambda m: m["patched_files"][
                sorted(m["patched_files"])[0]].__setitem__("patched_sha256", "0" * 64)),
        ):
            tampered = json.loads(MANIFEST.read_text(encoding="utf-8"))
            mutate(tampered)
            edited = Path(tmp) / (label.replace(" ", "_") + ".json")
            edited.write_text(json.dumps(tampered), encoding="utf-8")
            check("gate_refuses:" + label,
                  verify_letta_gate({**good, MANIFEST_ENV_VAR: str(edited)}) is False)

    report["status"] = ("local_offline_patch_verified" if not report["failures"]
                        else "FAILED")
    report["claim_boundary"] = (
        "Offline only: patch applicability, byte hashes and gate behaviour. "
        "No live server loading and no cloud call is claimed or checked here.")
    report["environment"] = {"pinned_source": str(PINNED), "python": sys.version}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
