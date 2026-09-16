#!/usr/bin/env python3
"""Scoped Letta/PostgreSQL deployment helper; no package installation or model calls.

Every invocation has a fresh record directory. Credentials and raw process logs
are private (0600); displayed and command logs are redacted. Never run upstream
init.sql or reuse an existing database/cluster during prepare-db.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
import urllib.request


COMMIT = "56ba9c25552605eec89de8ed3dc6394b625c1993"

#: The opt-in AE multi-client-tool-call runtime. Absent means the deployment
#: starts the plain service, exactly as before; present means the child process
#: must verify the reviewed profile + manifest before Letta is imported.
MULTICALL_ENV_KEYS = ("AE_LETTA_MULTICALL_PROFILE", "AE_LETTA_MULTICALL_MANIFEST",
                      "AE_LETTA_MULTICALL_MODULE_SHA256", "AE_LETTA_MULTICALL_SUPPORT",
                      "AE_LETTA_MULTICALL_LOAD_RECEIPT")


#: The stacked profile this launcher can opt into. Kept as a literal here so the
#: launcher never takes the value from its own environment.
STACK_PROFILE_VERSION = "ae-no-compaction-stack-1"


class MulticallRuntime:
    """Explicit opt-in description of the compatibility runtime.

    The launcher never inherits these from its own environment: a caller must ask
    for them, and every path is validated here. `support` is the directory holding
    the project's `ae_multicall.py`; it is passed as an explicit child variable
    rather than by widening the child's `PYTHONPATH`.
    """

    def __init__(self, *, manifest: Path, support: Path, receipt: Path, profile: str):
        self.manifest = Path(manifest)
        self.support = Path(support)
        self.receipt = Path(receipt)
        self.profile = profile
        self._verified = None

    def support_module(self):
        """Import the support module FROM THE EXPLICIT SUPPORT PATH.

        Running `python scripts/deployment/letta_local.py` puts the SCRIPT
        directory on `sys.path`, not the project root, so a bare
        `import ae_multicall` fails in exactly the environment this launcher is
        meant to run in. The module is therefore loaded by file location, and the
        path and bytes it actually produced are checked below. No parent
        `PYTHONPATH` and no pre-imported module is relied upon.
        """
        import importlib.util
        module_file = (self.support / "ae_multicall.py").resolve()
        if not module_file.is_file():
            raise RuntimeError(f"AE multicall support module is absent: {module_file}")
        spec = importlib.util.spec_from_file_location("ae_multicall", module_file)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"AE multicall support module cannot be loaded: {module_file}")
        module = importlib.util.module_from_spec(spec)
        # Register before executing: the real import machinery does this, and
        # `dataclasses` resolves annotations through `sys.modules[cls.__module__]`.
        # Without it a from-file-location load raises AttributeError on the first
        # frozen dataclass.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        resolved = Path(module.__file__).resolve()
        if resolved != module_file:
            raise RuntimeError(
                f"the support module resolved to another path: {resolved} != {module_file}")
        return module, resolved

    def validate(self, *, profile_version: str, schema: str):
        if self.profile != profile_version:
            raise RuntimeError(f"unsupported AE multicall profile {self.profile!r}")
        if not self.manifest.is_file():
            raise RuntimeError(f"AE multicall manifest is absent: {self.manifest}")
        module, module_path = self.support_module()
        record = module.read_manifest(self.manifest)
        if record.get("schema") != schema:
            raise RuntimeError("AE multicall manifest schema is not the reviewed one")
        declared = (self.support / record["compat_module"]["path"]).resolve()
        if declared != module_path:
            raise RuntimeError(
                f"the loaded support module is not the manifested one: {module_path}")
        digest = hashlib.sha256(module_path.read_bytes()).hexdigest()
        if digest != record["compat_module"]["sha256"]:
            raise RuntimeError("the support module does not match the manifest digest")
        return record

    def verified(self):
        """The loaded `(module, record)` after the full support+manifest check.

        Cached for one launch: the source gate and `start` must reason about the
        SAME manifest and the SAME loaded module. Re-loading per question would
        leave a window in which the support path or the manifest file could be
        swapped between two independent reads.
        """
        if self._verified is None:
            module, _ = self.support_module()
            record = self.validate(profile_version=module.PROFILE_VERSION,
                                   schema=module.MANIFEST_SCHEMA)
            self._verified = (module, record)
        return self._verified

    def env(self, receipt_dir: Path) -> dict:
        return {
            "AE_LETTA_MULTICALL_PROFILE": self.profile,
            "AE_LETTA_MULTICALL_MANIFEST": str(self.manifest),
            "AE_LETTA_MULTICALL_MODULE_SHA256": self._module_sha(),
            "AE_LETTA_MULTICALL_SUPPORT": str(self.support),
            # The child writes the load receipt; a fresh path per launch so a stale
            # receipt can never be reused for another instance.
            "AE_LETTA_MULTICALL_LOAD_RECEIPT": str(receipt_dir / "multicall-load.json"),
        }

    def _module_sha(self) -> str:
        module, _ = self.support_module()
        return module.read_manifest(self.manifest)["compat_module"]["sha256"]


class PatchStackRuntime:
    """Explicit opt-in description of the STACKED service runtime (R3-1).

    The stacked checkout is the multicall patch AND the no-compaction capacity patch
    in ONE tree, and it has its OWN manifest and load receipt. Passing this runtime
    is the only way the service is asked for the stacked profile: nothing is
    inherited from the launcher's environment, and no path is defaulted.

    It also carries the COUNTING declaration: the service gate counts every request,
    so the assets it counts with and the model identity they belong to are part of
    what this launcher must hand over explicitly.

    And it carries the RECEIVE policy the stack composes (R3-2): the stacked tree
    contains the receive patch, but the patched receive point acts on its own
    declaration, so this runtime declares that policy in the child environment too.
    One switch - this runtime - activates both, and the service process verifies the
    receive policy against the shared-stack contract before it imports Letta.
    """

    #: The counting configuration the service needs, and the ONLY keys that cross into
    #: the child for it. The launcher's own environment is never the source: a value
    #: must be passed explicitly (target/asset identity) or named as a path here.
    TOKENIZER_KEYS = ("AE_QWEN_TOKENIZER_JSON", "AE_QWEN_TOKENIZER_CONFIG",
                      "AE_QWEN_TOKENIZER_TARGET", "AE_QWEN_TOKENIZER_ASSET_TARGET",
                      "AE_QWEN_TOKENIZER_REVISION")
    #: The MODEL-SPECIFIC counting policy for a provider with no verified tokenizer. When
    #: declared, the tokenizer declaration above is NOT required: the service gate bounds
    #: each request by its own bytes against this budget instead, and says so in its record.
    BYTE_GATE_KEYS = ("AE_BYTE_GATE_COUNT_BASIS", "AE_BYTE_GATE_MAX_REQUEST_BYTES",
                      "AE_BYTE_GATE_WIRE_MODEL")
    #: The DECLARED request shape of a model-specific transport. When a profile is
    #: declared, the service adds that profile's own required fields (a provider's
    #: explicit non-thinking mode) to the request IT sends, bound to that profile's wire
    #: model. Undeclared means the service adds nothing, so the Qwen path is unchanged.
    TRANSPORT_KEYS = ("AE_TRANSPORT_WIRE_MODEL", "AE_TRANSPORT_REQUIRED_FIELDS")

    def __init__(self, *, manifest: Path, support: Path, receipt: Path, profile: str,
                 tokenizer_json: Path | None = None, tokenizer_config: Path | None = None,
                 tokenizer_target: str | None = None, tokenizer_asset_target: str | None = None,
                 tokenizer_revision: str | None = None, allow_exploration: bool = False,
                 byte_gate_count_basis: str | None = None,
                 byte_gate_max_request_bytes: int | None = None,
                 byte_gate_wire_model: str | None = None,
                 transport_profile: str | None = None):
        self.manifest = Path(manifest)
        self.support = Path(support)
        self.receipt = Path(receipt)
        self.profile = profile
        self.tokenizer_json = Path(tokenizer_json) if tokenizer_json else None
        self.tokenizer_config = Path(tokenizer_config) if tokenizer_config else None
        self.tokenizer_target = tokenizer_target
        self.tokenizer_asset_target = tokenizer_asset_target
        self.tokenizer_revision = tokenizer_revision
        # A counting asset that is NOT the target model's own tokenizer may only be used
        # as an explicitly labelled exploration; a formal start refuses it.
        self.allow_exploration = bool(allow_exploration)
        self.byte_gate_count_basis = byte_gate_count_basis
        self.byte_gate_max_request_bytes = byte_gate_max_request_bytes
        self.byte_gate_wire_model = byte_gate_wire_model
        self.transport_profile = transport_profile
        self._transport = None
        self._counting = None
        self._verified = None
        self._tokenizer = None

    def support_module(self):
        """Import the support module from the EXPLICIT support path (never PYTHONPATH)."""
        import importlib.util
        module_file = (self.support / "ae_multicall.py").resolve()
        if not module_file.is_file():
            raise RuntimeError(f"AE multicall support module is absent: {module_file}")
        spec = importlib.util.spec_from_file_location("ae_multicall", module_file)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"AE multicall support module cannot be loaded: {module_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        resolved = Path(module.__file__).resolve()
        if resolved != module_file:
            raise RuntimeError(
                f"the support module resolved to another path: {resolved} != {module_file}")
        return module, resolved

    def validate(self):
        if self.profile != STACK_PROFILE_VERSION:
            raise RuntimeError(f"unsupported AE patch stack profile {self.profile!r}")
        if not self.manifest.is_file():
            raise RuntimeError(f"AE stacked manifest is absent: {self.manifest}")
        module, module_path = self.support_module()
        record = module.read_stack_manifest(self.manifest)
        digest = hashlib.sha256(module_path.read_bytes()).hexdigest()
        if digest != record["compat_module"]["sha256"]:
            raise RuntimeError("the support module does not match the stacked manifest digest")
        return record
        self.counting()           # the counting declaration is part of the contract

    def transport_shape(self) -> dict:
        """The request-shape declaration this launch hands the service.

        Undeclared is the default: the service adds NOTHING to a request, so the
        historical Qwen path stays byte-identical and no provider mode is ever assumed.

        A declared profile contributes the fields ITS OWN profile requires - read from the
        proxy's reviewed profile table, never restated here - together with the wire model
        they belong to. The service applies them only to a request that names that model,
        so the declaration cannot leak onto another model in the same process.
        """
        if self._transport is not None:
            return self._transport
        if self.transport_profile is None:
            self._transport = {"policy": "undeclared", "profile": None,
                               "wire_model": None, "fields": {},
                               "applies_only_to_the_declared_wire_model": True}
            return self._transport
        import ae_cloud_proxy
        profile = ae_cloud_proxy.PROFILES.get(self.transport_profile)
        if profile is None:
            raise RuntimeError(
                f"unknown declared transport profile {self.transport_profile!r}: the "
                "request shape must come from a reviewed profile")
        fields = {name: value for name, value in profile.required_request_fields.items()}
        self._transport = {"policy": "declared_transport_fields",
                           "profile": self.transport_profile,
                           "wire_model": profile.wire_model, "fields": fields,
                           "applies_only_to_the_declared_wire_model": True}
        return self._transport

    def counting(self) -> dict:
        """WHICH pre-send bound this launch hands the service.

        Exactly two shapes, and never both:

        * the tokenizer declaration (the default, and the only shape the Qwen models used);
        * the byte-gate policy for a provider with no verified tokenizer, which requires an
          explicit basis and a positive budget and does NOT need - or accept - tokenizer
          assets, because the service will never count tokens under it.

        A half-declared byte policy is a refusal, not a fallback: it must not silently turn
        into the tokenizer path, which would then refuse every request.
        """
        if self._counting is not None:
            return self._counting
        basis = self.byte_gate_count_basis
        budget = self.byte_gate_max_request_bytes
        model = self.byte_gate_wire_model
        if basis is None and budget is None and model is None:
            self._counting = {"policy": "tokenizer", "tokenizer": self.tokenizer()}
            return self._counting
        # A HALF-declared policy is refused here, not silently completed: the service
        # would otherwise fall back to a token path that may hold a counter for another
        # model - a silent change of which gate a request faces.
        if not basis or not isinstance(basis, str):
            raise RuntimeError("the byte-gate policy requires AE_BYTE_GATE_COUNT_BASIS")
        if type(budget) is not int or budget <= 0:
            raise RuntimeError(
                "the byte-gate policy requires a positive AE_BYTE_GATE_MAX_REQUEST_BYTES")
        if not model or not isinstance(model, str):
            raise RuntimeError(
                "the byte-gate policy requires AE_BYTE_GATE_WIRE_MODEL: the policy is "
                "model-specific, so the model it may bound must be declared with it")
        if self.tokenizer_json is not None or self.tokenizer_config is not None:
            raise RuntimeError(
                "a byte-gate launch must not also declare tokenizer assets: the service "
                "never counts tokens under this policy")
        self._counting = {"policy": "byte_gate", "count_basis": basis,
                          "max_request_bytes": budget, "wire_model": model,
                          "applies_only_to_the_declared_wire_model": True,
                          "capacity_is_a_guarantee": False,
                          "token_count_available": False}
        return self._counting

    def tokenizer(self) -> dict:
        """The explicit counting declaration, with the REAL digests of its files.

        A missing declaration is a refusal, not a default: the service gate cannot
        count without assets AND the identity of the model they belong to. The files
        are hashed here, from the bytes this launch hands to the child, so the run
        record can state which tokenizer it counted with.
        """
        if self._tokenizer is not None:
            return self._tokenizer
        declared = {}
        for label, value in (("--tokenizer-json", self.tokenizer_json),
                             ("--tokenizer-config", self.tokenizer_config)):
            if value is None:
                raise RuntimeError(
                    f"the AE patch stack runtime requires {label}: the service gate "
                    "counts every request and refuses to run without its assets")
            # A cache entry is normally a symlink into the hub's blob store, so the
            # requirement is on what it RESOLVES to. Both paths are recorded, and the
            # digest is taken from the resolved bytes, so a swapped link is visible.
            resolved = value.resolve()
            if not resolved.is_file():
                raise RuntimeError(
                    f"{label} must name a readable file (resolved: {resolved})")
            declared[label] = (str(value), str(resolved), resolved)
        if not self.tokenizer_target:
            raise RuntimeError("the AE patch stack runtime requires --tokenizer-target")
        if not self.tokenizer_asset_target:
            raise RuntimeError(
                "the AE patch stack runtime requires --tokenizer-asset-target: naming the "
                "asset that will be counted with is part of the declaration")
        exploration = self.tokenizer_asset_target != self.tokenizer_target
        if exploration and not self.allow_exploration:
            raise RuntimeError(
                "the declared counting asset is not the target model's own tokenizer "
                f"({self.tokenizer_asset_target!r} != {self.tokenizer_target!r}); a formal "
                "service start refuses it. Pass the exploration flag only for a run that "
                "must be labelled as an exploration estimate.")
        self._tokenizer = {
            "json": {"declared_path": declared["--tokenizer-json"][0],
                     "path": declared["--tokenizer-json"][1],
                     "sha256": hashlib.sha256(
                         declared["--tokenizer-json"][2].read_bytes()).hexdigest()},
            "config": {"declared_path": declared["--tokenizer-config"][0],
                       "path": declared["--tokenizer-config"][1],
                       "sha256": hashlib.sha256(
                           declared["--tokenizer-config"][2].read_bytes()).hexdigest()},
            "target": self.tokenizer_target,
            "asset_target": self.tokenizer_asset_target,
            "revision": self.tokenizer_revision,
            "asset_is_target": not exploration,
            "exploration_only": exploration,
        }
        return self._tokenizer

    def check_source(self, source: Path) -> dict:
        """Prove the tree the service will import IS the manifested stacked checkout.

        Every fact is recomputed from bytes:

        * the manifest names THIS tree as its stacked checkout, and its pinned baseline
          and multicall checkout are different, existing trees;
        * the files the stack patch CHANGES but the reviewed multicall patch OWNS must
          carry the multicall digests, so the stack cannot quietly redefine one;
        * the compose is reproducible: for every file this patch changes, the multicall
          checkout carries the patch's declared BASELINE bytes and this tree carries its
          declared PATCHED bytes, which is what makes "stack = multicall + this patch"
          a checked statement instead of the manifest's own claim;
        * every changed and new file in the tree hashes to the manifested digest, and
          the counting declaration is present with the real digests of its files.

        Returns the record the source gate stores. Raises on any defect.
        """
        module, record = self.verified()
        tree = Path(source).resolve()
        if Path(record["letta_checkout"]).resolve() != tree:
            raise RuntimeError("the stacked manifest names another checkout as its "
                               f"stacked tree: {record['letta_checkout']}")
        multicall_tree = Path(record["multicall_checkout"]).resolve()
        baseline = Path(record["baseline_checkout"]).resolve()
        if baseline == tree or multicall_tree == tree:
            raise RuntimeError("the stacked manifest names this tree as its own baseline")
        if not baseline.is_dir():
            raise RuntimeError(f"the pinned baseline is absent: {baseline}")
        if not multicall_tree.is_dir():
            raise RuntimeError(f"the multicall checkout of the chain is absent: {multicall_tree}")
        reviewed = json.loads(Path(record["multicall_manifest"]).read_text(encoding="utf-8"))
        capacity_record = json.loads(
            Path(record["no_compaction_manifest"]).read_text(encoding="utf-8"))
        # The multicall files THIS patch does not touch must still carry the reviewed
        # multicall bytes. The files the stack patch DOES change are proven by the chain
        # check below instead (multicall checkout = this patch's baseline, this tree =
        # its patched bytes), which is a stronger statement than a digest copy.
        owned_by_multicall = set(module.REQUIRED_PATCHED_FILES) - set(
            capacity_record["patched_files"])
        for name in sorted(owned_by_multicall):
            if name not in record["patched_files"]:
                continue
            expected = reviewed["patched_files"][name]["patched_sha256"]
            target = tree / name
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise RuntimeError(
                    "the stacked tree redefines a file the reviewed multicall patch owns: "
                    + name)
        for name, entry in capacity_record["patched_files"].items():
            sibling = multicall_tree / name
            if not sibling.is_file():
                raise RuntimeError(f"the multicall checkout is missing {name}")
            if hashlib.sha256(sibling.read_bytes()).hexdigest() != entry["baseline_sha256"]:
                raise RuntimeError(
                    f"the multicall checkout's {name} is not this patch's baseline")
            target = tree / name
            if (not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != entry["patched_sha256"]):
                raise RuntimeError(
                    f"the stacked tree does not carry the patched bytes of {name}")
        for key in ("patched_files", "new_files"):
            for name, digest in sorted(record[key].items()):
                target = tree / name
                if not target.is_file():
                    raise RuntimeError("a stacked patch file is absent from the service "
                                       f"source: {name}")
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise RuntimeError(
                        f"the service source does not match the stacked patch bytes: {name}")
        tracked_allowed = sorted(
            {name for name, item in reviewed["patched_files"].items()
             if item.get("baseline_sha256") is not None}
            | set(capacity_record["patched_files"]))
        return {"manifest": str(self.manifest),
                "manifest_sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                "tracked_changes_allowed": tracked_allowed,
                "stacked_checkout": str(tree),
                "multicall_checkout": str(multicall_tree),
                "baseline_checkout": str(baseline),
                "compat_module_sha256": record["compat_module"]["sha256"],
                "patched_files": dict(sorted(record["patched_files"].items())),
                "new_files": dict(sorted(record["new_files"].items())),
                # The historical key is kept for record compatibility; `counting`
                # states WHICH policy shape this launch handed over.
                "tokenizer": (self.counting().get("tokenizer")
                              if self.counting()["policy"] == "tokenizer" else None),
                "counting": self.counting()}

    def verified(self):
        """The loaded `(module, record)` after the full support+manifest check."""
        if self._verified is None:
            module, _ = self.support_module()
            self._verified = (module, self.validate())
        return self._verified

    def receive_policy(self) -> dict:
        """The RECEIVE compatibility policy this stack COMPOSES and ACTIVATES.

        The stacked checkout contains the receive patch and the capacity patch in one
        tree, but loading that tree is not activation: the patched receive point acts
        on its own declaration (`AE_LETTA_MULTICALL_PROFILE`). A launch that declared
        the stack and not that policy let the pinned upstream code truncate every
        multi-call response to its first call (the live r4 capture), so this launcher
        declares the composed profile in the child environment, and the service
        process verifies it against the STACKED contract before Letta is imported
        (`ae_multicall.verify_receive_gate`).

        The profile value is read from the support module the stack manifest pins; it
        is never restated here. The multicall-only manifest is deliberately NOT handed
        over: it pins the multicall bytes of the file the capacity patch also changes,
        so declaring it beside the stack would be a mixed declaration the runtime
        refuses.
        """
        module, record = self.verified()
        return {"policy": "declared_composed_receive_policy",
                "profile": module.PROFILE_VERSION,
                "declared_in_the_child_environment": module.RECEIVE_PROFILE_ENV_VAR,
                "activated_by": module.STACK_ENV_VAR,
                "stack_profile": self.profile,
                "stack_manifest": str(self.manifest),
                "composed_receive_manifest": record["multicall_manifest"],
                "module_sha256": record["compat_module"]["sha256"],
                "standalone_manifest_declared": False,
                "parallel_tool_calls": False,
                "execute_serially": True,
                "preserve_native_ids": True,
                "verified_in_the_service_process_by": "ae_multicall.verify_receive_gate"}

    def env(self, receipt_dir: Path) -> dict:
        _module, record = self.verified()
        # The counting policy is passed EXPLICITLY, and only ONE of its two shapes: the
        # service gate cannot count without assets, or (for a provider with no verified
        # tokenizer) cannot bound a request without the declared byte policy. Nothing here
        # is inherited from this process.
        counting = self.counting()
        receive = self.receive_policy()
        env = {
            "AE_LETTA_PATCH_STACK_PROFILE": self.profile,
            "AE_LETTA_PATCH_STACK_MANIFEST": str(self.manifest),
            # The receive compatibility policy this stack composes, ACTIVATED here. The
            # patched receive point reads exactly this variable, so without it the
            # service would truncate a multi-call response to its first call.
            receive["declared_in_the_child_environment"]: receive["profile"],
            "AE_LETTA_MULTICALL_MODULE_SHA256": record["compat_module"]["sha256"],
            "AE_LETTA_MULTICALL_SUPPORT": str(self.support),
            # The child writes the receipt; a fresh path per launch so a stale
            # receipt can never be reused for another instance.
            "AE_LETTA_PATCH_STACK_RECEIPT": str(receipt_dir / "patch-stack-load.json"),
        }
        transport = self.transport_shape()
        if transport["policy"] == "declared_transport_fields" and transport["fields"]:
            # The declared transport's OWN required request fields, with the model they
            # belong to. Passed as data, so the service applies exactly what the reviewed
            # profile declares and nothing is defaulted inside the service.
            env.update({
                "AE_TRANSPORT_WIRE_MODEL": transport["wire_model"],
                "AE_TRANSPORT_REQUIRED_FIELDS": json.dumps(
                    transport["fields"], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")),
            })
        if counting["policy"] == "byte_gate":
            # The model-specific branch: the service measures each request's own bytes
            # against this budget and never counts tokens. The tokenizer variables are
            # deliberately ABSENT, so the service cannot fall back to a Qwen count for a
            # provider they do not describe.
            env.update({
                "AE_BYTE_GATE_COUNT_BASIS": counting["count_basis"],
                "AE_BYTE_GATE_MAX_REQUEST_BYTES": str(counting["max_request_bytes"]),
                # The service checks this against the request's own model, so the byte
                # gate can only ever apply to the model it was declared for.
                "AE_BYTE_GATE_WIRE_MODEL": counting["wire_model"],
            })
            return env
        tokenizer = counting["tokenizer"]
        env.update({
            "AE_QWEN_TOKENIZER_JSON": tokenizer["json"]["path"],
            "AE_QWEN_TOKENIZER_CONFIG": tokenizer["config"]["path"],
            "AE_QWEN_TOKENIZER_TARGET": tokenizer["target"],
            "AE_QWEN_TOKENIZER_ASSET_TARGET": tokenizer["asset_target"],
            "AE_QWEN_TOKENIZER_REVISION": tokenizer["revision"] or "undeclared",
        })
        return env
ROLE = "ae01_letta"
PGDATA = Path("/var/lib/postgresql/ae01_letta")
PGSOCKET = Path("/run/postgresql/ae01_letta")
EXPECTED = {
    # Letta imports this cwd-level config even with an environment whitelist.
    "conf.yaml": "3cae0e67f01b813bd2498ad834aff4394c098a7e3aa905b7a6a3be17bbb6de91",
    "pyproject.toml": "ba86147a334a4900962b260d1912e263e011e8698377327b9f1a8fd943540c3e",
    "uv.lock": "7d86dc1075143b24ab8864e6f443cb2220ee3aaefc385ff3919f8ea5b52b9c75",
    "letta/server/server.py": "3e014a80733c5c0e96a73d0ca16f0d58bc5e560f3246356b0a81831aa47c6f6d",
    "letta/schemas/providers/vllm.py": "7809b4c37a717c652f0f8c0635146343305a027971ab66d2484700a3fa69621b",
}


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def write_new(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as out:
        os.chmod(path, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write("\n")


def redacted(text: str, password: str = "") -> str:
    if password:
        text = text.replace(password, "[REDACTED]")
    return re.sub(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+(@)",
                  r"\1[REDACTED]\2", text)


def loopback_url(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
            or url.username or url.password or url.query or url.fragment
            or url.path.rstrip("/") != "/v1"):
        raise ValueError("model URL must be http://127.0.0.1:PORT/v1, without credentials/query")
    return value.rstrip("/")


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class Deployment:
    def __init__(self, project: Path):
        self.project = project.resolve(strict=True)
        self.source = self.project / "vendor/letta-v1"
        self.venv = self.project / ".venv-letta"
        self.base = self.project / "deployment"
        self.private = self.base / "private"
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.private, 0o700)
        self.record = self.base / "runs" / stamp()
        self.record.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.credentials = self.private / "letta-db.json"
        self.process = self.private / "letta-process.json"
        self.password = ""
        self.steps = 0
        write_new(self.record / "invocation.json", {
            "argv": sys.argv, "project": str(self.project), "source_commit_expected": COMMIT,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "python": sys.version, "pid": os.getpid(), "evidence_type": "deployment_not_experiment",
        })

    def env(self, db: dict | None = None, model_url: str | None = None,
            multicall: "MulticallRuntime | None" = None,
            patch_stack: "PatchStackRuntime | None" = None) -> dict[str, str]:
        if multicall is not None and patch_stack is not None:
            # Two service contracts, two checkouts: the multicall-only manifest pins the
            # multicall bytes of a file the capacity patch also changes, so a child given
            # both declarations could only be under one of them. Refused here as well as
            # in the source gate, so no entry point can build such an environment.
            raise RuntimeError(
                "declare either the multicall runtime or the patch stack runtime, not "
                "both: they verify different trees")
        runtime = self.base / "runtime"
        runtime.mkdir(exist_ok=True, mode=0o700)
        # Explicit whitelist: do not inherit cloud keys, proxies, tracing sinks,
        # default embedding, auto-mode, or another project's Python environment.
        env = {
            "PATH": str(self.venv / "bin") + ":/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "LETTA_DIR": str(runtime), "MEMGPT_CONFIG_PATH": str(runtime / "config"),
            "LETTA_LOGGING_LETTA_LOG_PATH": str(self.record / "letta-library.private.log"),
            "LETTA_DISABLE_TRACING": "true", "LETTA_TELEMETRY_ENABLE_DATADOG": "false",
            "LETTA_STORE_LLM_TRACES": "false", "LETTA_USE_TPUF": "false",
            "LETTA_EMBED_ALL_MESSAGES": "false", "LETTA_EMBED_TOOLS": "false",
            "LETTA_ENABLE_PINECONE": "false", "LETTA_UVICORN_WORKERS": "1",
            "LETTA_DISABLE_SQLALCHEMY_POOLING": "true", "AUTO_MODE_ENABLED": "false",
            "PYTHONUNBUFFERED": "1",
        }
        if db:
            env["LETTA_PG_URI"] = (
                f"postgresql://{ROLE}:{quote(db['password'], safe='')}@127.0.0.1:{db['port']}/{ROLE}"
            )
        if model_url:
            env["VLLM_API_BASE"] = loopback_url(model_url)
        if multicall is not None:
            # Explicit opt-in: only these five keys cross into the child, and only
            # because the caller asked for this runtime. PYTHONPATH is NOT widened;
            # the child adds its own support directory after verifying the module.
            env.update(multicall.env(self.record))
        if patch_stack is not None:
            # The stacked profile's own five keys. The module digest and support
            # directory are the same facts, so both profiles cross consistently.
            env.update(patch_stack.env(self.record))
        return env

    def run(self, args: list[str], *, env: dict | None = None, stdin: str | None = None,
            cwd: Path | None = None, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
        self.steps += 1
        result = subprocess.run(args, input=stdin, text=True, capture_output=True,
                                env=env or self.env(), cwd=cwd, timeout=timeout)
        write_new(self.record / f"step-{self.steps:03d}.json", {
            "argv": [redacted(s, self.password) for s in args], "cwd": str(cwd) if cwd else None,
            "returncode": result.returncode, "stdout": redacted(result.stdout, self.password),
            "stderr": redacted(result.stderr, self.password),
            "stdin": "not recorded" if stdin is not None else None,
        })
        if check and result.returncode:
            raise RuntimeError(f"command failed ({result.returncode}): {args[0]}; see redacted step log")
        return result

    def bind_reviewed_patch(self, multicall: "MulticallRuntime") -> tuple:
        """Prove this service tree IS the manifest's reviewed patched checkout.

        Every fact comes from the EXISTING strict verification, never from a path
        whitelist: the support module is loaded from the explicit support path, the
        manifest is structurally checked by `read_manifest`, the reviewed patch is
        re-applied to the pinned baseline by `verify_patch_identity`, the composed
        fail-closed gate `verify_letta_gate` must accept the exact child
        environment, and only then are this tree's own bytes compared with the
        manifested post-patch digests. The pinned baseline may never be this tree,
        so a patched checkout cannot be passed off as its own baseline.
        """
        module, record = multicall.verified()
        source = self.source.resolve()
        if Path(record["letta_checkout"]).resolve() != source:
            raise RuntimeError("the reviewed manifest names another checkout as its "
                               f"patched tree: {record['letta_checkout']}")
        baseline = Path(record["baseline_checkout"]).resolve()
        if baseline == source:
            raise RuntimeError("the reviewed manifest names this tree as its own pinned baseline")
        if not baseline.is_dir():
            raise RuntimeError(f"the reviewed pinned baseline is absent: {baseline}")
        # The manifested digests must be reproducible from the pinned baseline plus
        # the reviewed patch, so a manifest cannot simply declare edited bytes.
        module.verify_patch_identity(record)
        for name in module.REQUIRED_PATCHED_FILES:
            expected = record["patched_files"][name]["patched_sha256"]
            target = source / name
            if not target.is_file():
                raise RuntimeError(
                    f"the reviewed patched file is absent from the service source: {name}")
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise RuntimeError(
                    f"the service source does not match the reviewed patch bytes: {name}")
        if not module.verify_letta_gate(multicall.env(self.record), module=module,
                                        manifest=multicall.manifest):
            raise RuntimeError("the AE multicall gate refused the service source")
        return module, record

    def reviewed_dirty(self, status: str, allowed: set, label: str = "the reviewed patch") -> list:
        """Accept ONLY a declared patch as uncommitted tracked changes.

        Real `git status --porcelain` output is parsed by its two status columns:
        anything staged, deleted, renamed/copied, of an unexpected type, or outside
        the declared upstream files is refused, and every declared upstream file
        must actually be reported modified. That last condition also refuses a tree
        that hides a change with `assume-unchanged`/`skip-worktree`, because the
        hidden file can no longer be accounted for as the patch.

        `allowed` is the exact set of tracked files the declared patch chain may
        modify; `label` only names it in the refusals, so the multicall path and the
        stacked path report which contract they judged the tree against.
        """
        reviewed_tracked = set(allowed)
        accepted: set = set()
        refused = []
        for line in status.splitlines():
            if not line.strip():
                continue
            if len(line) < 4:
                refused.append(f"unparseable git status line {line!r}")
                continue
            code, path = line[:2], line[3:]
            if "R" in code or "C" in code:
                refused.append(f"renamed or copied tracked path: {path}")
            elif "D" in code:
                refused.append(f"deleted tracked path: {path}")
            elif code[0] != " ":
                refused.append(f"staged change: {path}")
            elif code[1] != "M":
                refused.append(f"unexpected git state {code!r}: {path}")
            elif path not in reviewed_tracked:
                refused.append(f"tracked modification outside {label}: {path}")
            else:
                accepted.add(path)
        if refused:
            raise RuntimeError(f"the tracked changes are not {label}: "
                               + "; ".join(sorted(refused)))
        if accepted != reviewed_tracked:
            missing = sorted(reviewed_tracked - accepted)
            raise RuntimeError(f"{label} is not present as tracked changes: "
                               + ", ".join(missing))
        return sorted(accepted)

    def source_check(self, multicall: "MulticallRuntime | None" = None,
                     patch_stack: "PatchStackRuntime | None" = None) -> None:
        if not self.source.is_dir():
            raise RuntimeError("fixed vendor/letta-v1 source is absent")
        if (self.source / ".env").exists():
            raise RuntimeError("vendor/.env exists; refuse hidden provider configuration")
        # config_file.py merges ~/.letta/conf.yaml BEFORE cwd/conf.yaml. LETTA_DIR
        # and MEMGPT_CONFIG_PATH do not disable it; its provider keys could be
        # restored despite our environment whitelist. The child receives no HOME,
        # so Path.home() resolves the OS account home. Do not read its secrets.
        account_config = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".letta/conf.yaml"
        if account_config.exists() or account_config.is_symlink():
            raise RuntimeError("account ~/.letta/conf.yaml exists; refuse implicit user configuration")
        observed = {}
        for name, expected in EXPECTED.items():
            observed[name] = hashlib.sha256((self.source / name).read_bytes()).hexdigest()
            if observed[name] != expected:
                raise RuntimeError(f"fixed-source hash mismatch: {name}")
        # The STACKED opt-in is its own contract: this tree changes files the
        # multicall-only manifest pins, so the old gate would refuse it by design.
        # It is checked against the stacked manifest instead, and the two opt-ins are
        # mutually exclusive so neither can be used to weaken the other.
        if patch_stack is not None:
            if multicall is not None:
                raise RuntimeError(
                    "declare either the multicall runtime or the patch stack runtime, "
                    "not both: they verify different trees")
            stack_record = patch_stack.check_source(self.source)
            # The manifest digests are not a whole-source identity. When the tree is a
            # git checkout, the SAME fixed-source boundary the other paths use applies:
            # HEAD must be the pinned commit, and the only tolerated tracked changes are
            # the files the declared patch chain modifies. A sealed tree without `.git`
            # keeps the narrower, explicitly recorded boundary below instead.
            head = None
            accepted_changes = []
            if (self.source / ".git").exists():
                head = self.run(["git", "rev-parse", "HEAD"], cwd=self.source).stdout.strip()
                if head != COMMIT:
                    raise RuntimeError(
                        f"the stacked tree's git HEAD is not the pinned commit: {head}")
                status = self.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                  cwd=self.source).stdout
                if status.strip():
                    accepted_changes = self.reviewed_dirty(
                        status, set(stack_record["tracked_changes_allowed"]),
                        "the stacked patch chain")
            identity = ("pinned_git_head_plus_manifest_digests" if head is not None
                        else "no_git_tree_selective_hashes_not_full_source_identity")
            record = {"git_head": head, "selected_file_shas": observed,
                      "commit_claim": COMMIT, "source_identity": identity,
                      "patch_stack": dict(stack_record,
                                          tracked_changes_accepted=accepted_changes)}
            write_new(self.record / "source.json", record)
            return
        # With an explicit opt-in, the designated patch is verified against the
        # reviewed manifest BEFORE the tree is judged: the same tree is then
        # accepted as dirty only because it IS the manifested patched checkout.
        reviewed = self.bind_reviewed_patch(multicall) if multicall is not None else None
        accepted_changes = []
        if (self.source / ".git").exists():
            head = self.run(["git", "rev-parse", "HEAD"], cwd=self.source).stdout.strip()
            status = self.run(["git", "status", "--porcelain", "--untracked-files=no"],
                              cwd=self.source).stdout
            # No opt-in keeps the original strict rule: any tracked change refuses.
            if head != COMMIT or (status.strip() and reviewed is None):
                raise RuntimeError("fixed vendor git commit differs or tracked files are dirty")
            if status.strip():
                accepted_changes = self.reviewed_dirty(
                    status,
                    {name for name, item in reviewed[1]["patched_files"].items()
                     if item["baseline_sha256"] is not None},
                    "the reviewed patch")
        else:
            head = None  # Selective hashes are not proof of a whole-tree commit.
        record = {"git_head": head, "selected_file_shas": observed,
                  "commit_claim": COMMIT,
                  "source_identity": ("pinned_git_head_plus_manifest_digests"
                                      if head is not None else
                                      "no_git_tree_selective_hashes_not_full_source_identity")}
        if reviewed is not None:
            module, patch_record = reviewed
            record["reviewed_patch"] = {
                "manifest": str(multicall.manifest),
                "manifest_sha256": hashlib.sha256(multicall.manifest.read_bytes()).hexdigest(),
                "patched_checkout": str(self.source.resolve()),
                "baseline_checkout": str(Path(patch_record["baseline_checkout"]).resolve()),
                "compat_module_sha256": patch_record["compat_module"]["sha256"],
                "patched_files": {name: patch_record["patched_files"][name]["patched_sha256"]
                                  for name in module.REQUIRED_PATCHED_FILES},
                "tracked_changes_accepted": accepted_changes,
            }
        write_new(self.record / "source.json", record)

    def db_config(self) -> dict:
        if self.credentials.is_symlink() or self.credentials.stat().st_mode & 0o077:
            raise RuntimeError("credential file must be regular and mode 0600")
        db = json.loads(self.credentials.read_text())
        self.password = db["password"]
        if (db["role"] != ROLE or db["database"] != ROLE or db["pgdata"] != str(PGDATA)
                or db["socket"] != str(PGSOCKET) or db["port"] not in (5432, 5543)):
            raise RuntimeError("unexpected managed DB identity/path/port")
        if PGDATA.is_symlink():
            raise RuntimeError("managed PostgreSQL data path must not be a symlink")
        return db

    def check_pg_pid(self, db: dict) -> None:
        """Refuse PID reuse before asking pg_ctl to signal a server."""
        pidfile = PGDATA / "postmaster.pid"
        if not pidfile.exists():
            return
        pid = int(pidfile.read_text().splitlines()[0])
        command_file = Path(f"/proc/{pid}/cmdline")
        if not command_file.exists():
            return
        command = command_file.read_bytes().split(b"\0")
        if not any(command):
            return
        if str(PGDATA).encode() not in command or not any(b"postgres" in part for part in command):
            raise RuntimeError("postmaster PID belongs to another process; refuse signaling")

    def pg(self, db: dict, tool: str, args: list[str], **kw) -> subprocess.CompletedProcess:
        return self.run(["runuser", "-u", "postgres", "--", str(Path(db["bindir"]) / tool)] + args,
                        cwd=Path("/tmp"), **kw)

    def pg_start(self, db: dict) -> None:
        self.check_pg_pid(db)
        postgres_user = pwd.getpwnam("postgres")
        if PGSOCKET.exists():
            if PGSOCKET.is_symlink() or PGSOCKET.stat().st_uid != postgres_user.pw_uid:
                raise RuntimeError("unexpected socket directory owner/type")
        else:
            PGSOCKET.mkdir(mode=0o700)
            os.chown(PGSOCKET, postgres_user.pw_uid, postgres_user.pw_gid)
        if not port_free(db["port"]):
            raise RuntimeError("managed PostgreSQL port occupied; no takeover")
        logfile = self.record / "postgres-process.private.log"
        with logfile.open("x", encoding="utf-8") as out:
            os.chmod(logfile, 0o600)
            process = subprocess.Popen([
                "runuser", "-u", "postgres", "--", str(Path(db["bindir"]) / "postgres"),
                "-D", str(PGDATA), "-h", "127.0.0.1", "-p", str(db["port"]), "-k", str(PGSOCKET),
            ], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                cwd="/tmp", env=self.env(), start_new_session=True)
        write_new(self.record / "postgres-process.json", {
            "launcher_pid": process.pid, "pgdata": str(PGDATA), "port": db["port"],
            "log_private": str(logfile), "listen_addresses": "127.0.0.1",
        })
        for _ in range(40):
            if process.poll() is not None:
                raise RuntimeError("PostgreSQL exited during startup; inspect redacted private log")
            result = self.pg(db, "pg_isready", ["-h", str(PGSOCKET), "-p", str(db["port"])], check=False)
            if result.returncode == 0:
                return
            time.sleep(0.25)
        raise RuntimeError("PostgreSQL startup timed out; inspect recorded managed process")

    def prepare_db(self) -> dict:
        if os.geteuid() != 0 or sys.platform != "linux":
            raise RuntimeError("prepare-db requires the authorized Linux server root account")
        if self.credentials.exists() or PGDATA.exists() or PGSOCKET.exists():
            raise RuntimeError("managed DB assets already exist; refuse reinitialization (use start-db/status)")
        config_tool = "/usr/lib/postgresql/15/bin/pg_config"
        if not Path(config_tool).is_file():
            raise RuntimeError(f"PostgreSQL 15 is required; missing {config_tool}; package installation is separate")
        version = self.run([config_tool, "--version"]).stdout.strip()
        version_match = re.match(r"^PostgreSQL\s+(\d+)(?:[.\s]|$)", version)
        major_version = int(version_match.group(1)) if version_match else None
        write_new(self.record / "postgres-selection.json", {
            "pg_config": config_tool, "version": version, "major_version": major_version,
            "required_major_version": 15, "accepted": major_version == 15,
        })
        if major_version != 15:
            raise RuntimeError(f"PostgreSQL 15 is required; {config_tool} reported {version!r}")
        bindir = Path(self.run([config_tool, "--bindir"]).stdout.strip())
        sharedir = Path(self.run([config_tool, "--sharedir"]).stdout.strip())
        for name in ("postgres", "initdb", "pg_ctl", "psql", "pg_isready"):
            if not (bindir / name).is_file():
                raise RuntimeError(f"PostgreSQL binary absent: {name}")
        if not (sharedir / "extension/vector.control").is_file():
            raise RuntimeError("pgvector is not installed for the selected PostgreSQL version")
        clusters = self.run(["pg_lsclusters", "--no-header"], check=False) if shutil.which("pg_lsclusters") else None
        existing = bool(clusters and clusters.stdout.strip())
        processes = self.run(["pgrep", "-x", "postgres"], check=False)
        existing = existing or processes.returncode == 0
        port = 5543 if existing or not port_free(5432) else 5432
        if not port_free(port):
            raise RuntimeError(f"chosen port {port} is occupied; do not stop another service")
        db = {"role": ROLE, "database": ROLE, "password": secrets.token_urlsafe(32),
              "port": port, "pgdata": str(PGDATA), "socket": str(PGSOCKET), "bindir": str(bindir)}
        self.password = db["password"]
        write_new(self.credentials, db)  # Preserve credentials even on a partial failure; never overwrite.
        postgres_user = pwd.getpwnam("postgres")
        PGDATA.mkdir(mode=0o700)
        os.chown(PGDATA, postgres_user.pw_uid, postgres_user.pw_gid)
        self.pg(db, "initdb", ["-D", str(PGDATA), "--auth-local=peer", "--auth-host=scram-sha-256",
                              "--encoding=UTF8", "--locale=C"])
        self.pg_start(db)
        common = ["-h", str(PGSOCKET), "-p", str(port), "-d", "postgres", "-X", "-v", "ON_ERROR_STOP=1"]
        sql = f"CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{db['password']}';\n"
        sql += f"CREATE DATABASE {ROLE} OWNER {ROLE};\n"
        self.pg(db, "psql", common, stdin=sql)
        self.pg(db, "psql", ["-h", str(PGSOCKET), "-p", str(port), "-d", ROLE,
                             "-X", "-v", "ON_ERROR_STOP=1", "-c", "CREATE EXTENSION vector;"])
        self.pg(db, "psql", ["-h", str(PGSOCKET), "-p", str(port), "-d", ROLE,
                             "-X", "-Atc", "SELECT version(); SHOW listen_addresses; SELECT extversion FROM pg_extension WHERE extname='vector';"])
        return {"database": ROLE, "role": ROLE, "port": port, "credential_file": str(self.credentials),
                "existing_postgres_detected": existing, "prepared": True}

    def migrate(self) -> dict:
        self.source_check()
        db = self.db_config()
        alembic = self.venv / "bin/alembic"
        if not alembic.is_file():
            raise RuntimeError("isolated .venv-letta is absent; install locked dependencies first")
        env = self.env(db)
        # Alembic prints the DB URI: capture in memory, redact before persistence/display.
        self.run([str(alembic), "upgrade", "head"], cwd=self.source, env=env, timeout=300)
        current = self.run([str(alembic), "current"], cwd=self.source, env=env).stdout
        return {"migrated": True, "alembic_current": redacted(current, self.password)}

    def start(self, model_url: str, port: int, bounded_nltk_startup: bool = False,
              explicit_llm_config: bool = False,
              multicall: "MulticallRuntime | None" = None,
              patch_stack: "PatchStackRuntime | None" = None) -> dict:
        self.source_check(multicall=multicall, patch_stack=patch_stack)
        if patch_stack is not None:
            # The stacked gate also lives in the bootstrap wrapper: it must run in the
            # process that imports Letta, and that process writes the receipt.
            patch_stack.verified()
            if not bounded_nltk_startup:
                raise RuntimeError(
                    "the AE patch stack runtime requires the bounded-startup bootstrap "
                    "wrapper so the gate and stack load receipt run in the service process")
        if multicall is not None:
            # `verified()` already ran the support-load + manifest validation inside
            # the source gate and is cached, so the gate and this branch cannot see
            # two different manifests.
            _multicall, _record = multicall.verified()
            if not bounded_nltk_startup:
                # The compatibility gate lives in the bootstrap wrapper: the child
                # must be launched through it so verification happens before Letta
                # is imported and the load receipt is written by that process.
                raise RuntimeError(
                    "the AE multicall runtime requires the bounded-startup bootstrap "
                    "wrapper so the gate and load receipt run in the service process")
        db = self.db_config()
        if self.process.exists():
            raise RuntimeError("Letta process record already exists; inspect/stop before another start")
        if not port_free(port):
            raise RuntimeError("Letta port occupied; no takeover")
        executable = self.venv / "bin/letta"
        if not executable.is_file():
            raise RuntimeError("isolated Letta executable is absent")
        cli_argv = [str(executable), "server", "--host", "127.0.0.1", "--port", str(port)]
        launch_argv = list(cli_argv)
        launch_info = {"launch_mode": "direct", "letta_argv": cli_argv,
                       "entrypoint_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
        if bounded_nltk_startup:
            bootstrap = self.project / "scripts/deployment/letta_bootstrap.py"
            interpreter = self.venv / "bin/python"
            if not bootstrap.is_file() or not interpreter.is_file():
                raise RuntimeError("bounded NLTK bootstrap or dedicated interpreter is absent")
            launch_argv = [str(interpreter), "-u", str(bootstrap)] + cli_argv
            launch_info.update({"launch_mode": "bounded_nltk_startup",
                                "bootstrap_sha256": hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
                                "nltk_expected_version": "3.9.1", "nltk_per_io_timeout_seconds": 10,
                                "nltk_total_timeout_seconds": None, "nltk_result_not_yet_observed": True})
        launch_info["launch_argv"] = launch_argv
        launch_info["explicit_llm_config"] = explicit_llm_config
        launch_info["service_mode"] = ("ae_no_compaction_stack" if patch_stack is not None
                                       else "ae_multicall_receive_compat"
                                       if multicall is not None else "legacy_sequential")
        if patch_stack is not None:
            counting = patch_stack.counting()
            launch_info["patch_stack_profile"] = patch_stack.profile
            launch_info["patch_stack_manifest_sha256"] = hashlib.sha256(
                patch_stack.manifest.read_bytes()).hexdigest()
            launch_info["patch_stack_load_receipt"] = str(
                self.record / "patch-stack-load.json")
            # The RECEIVE policy this stack composes, declared in the child environment:
            # the service process re-verifies it against the stacked contract before
            # Letta is imported, and refuses to start when it cannot be established.
            launch_info["receive_policy"] = patch_stack.receive_policy()
            # The counting basis this launch hands the service: either the tokenizer
            # declaration with the real digests of the asset files it will read, or the
            # declared byte-gate policy for a provider without a tokenizer.
            # The historical record key is still written (null under the byte policy, which
            # has no tokenizer at all), and `counting` states WHICH policy shape was handed
            # over so an audit never has to infer it.
            launch_info["tokenizer"] = (counting["tokenizer"]
                                        if counting["policy"] == "tokenizer" else None)
            launch_info["counting"] = counting
            launch_info["counting_policy"] = counting["policy"]
            launch_info["tokenizer_child_keys"] = (
                list(patch_stack.BYTE_GATE_KEYS) if counting["policy"] == "byte_gate"
                else list(patch_stack.TOKENIZER_KEYS))
            # The declared request shape handed to the service, and the exact child keys
            # it travelled in: an audit reads this instead of inferring a mode.
            transport = patch_stack.transport_shape()
            launch_info["transport"] = transport
            launch_info["transport_child_keys"] = (
                list(patch_stack.TRANSPORT_KEYS)
                if transport["policy"] == "declared_transport_fields" and transport["fields"]
                else [])
        if multicall is not None:
            launch_info["multicall_profile"] = multicall.profile
            launch_info["multicall_manifest_sha256"] = hashlib.sha256(
                multicall.manifest.read_bytes()).hexdigest()
            launch_info["multicall_load_receipt"] = str(
                self.record / "multicall-load.json")
        write_new(self.record / "launch.json", launch_info)
        # Explicit cloud LLMConfig bypasses automatic vLLM catalog registration.
        # Never synthesize max_model_len in a cloud /models response.
        env = self.env(db, None if explicit_llm_config else model_url, multicall, patch_stack)
        logfile = self.record / "letta-process.private.log"
        with logfile.open("x", encoding="utf-8") as out:
            os.chmod(logfile, 0o600)
            process = subprocess.Popen(launch_argv,
                                       cwd=self.source, env=env, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        state = {"pid": process.pid, "executable": str(executable), "port": port,
                 "model_url": model_url, "private_log": str(logfile), "record": str(self.record),
                 **launch_info}
        write_new(self.process, state)
        write_new(self.record / "process.json", state)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("Letta exited at startup; use status for redacted log tail, then stop")
            try:
                with opener.open(f"http://127.0.0.1:{port}/v1/health/", timeout=1) as response:
                    health = json.load(response)
                if health.get("status") == "ok" and health.get("version") == "0.16.8":
                    started = {"started": True, "health": health, "pid": process.pid,
                               "service_mode": launch_info["service_mode"],
                               "warning": "liveness only; no agent/model inference or "
                                          "task success checked"}
                    if multicall is not None:
                        # The receipt is written by the child process, not here, so
                        # its absence means the gate did not run in the service.
                        receipt = self.record / "multicall-load.json"
                        if not receipt.is_file():
                            raise RuntimeError(
                                "the AE multicall service started without writing its load "
                                "receipt; treat this service as unverified")
                        started["multicall_load_receipt"] = str(receipt)
                    if patch_stack is not None:
                        # A started process is not evidence by itself: the receipt must
                        # exist AND describe the checkout and digests this launch
                        # declared, or the service is treated as unverified.
                        module, record = patch_stack.verified()
                        receipt = self.record / "patch-stack-load.json"
                        if not receipt.is_file():
                            raise RuntimeError(
                                "the AE patch stack service started without writing its "
                                "stack load receipt; treat this service as unverified")
                        module.validate_stack_receipt(
                            json.loads(receipt.read_text(encoding="utf-8")), record,
                            hashlib.sha256(patch_stack.manifest.read_bytes()).hexdigest())
                        started["patch_stack_load_receipt"] = str(receipt)
                        # The receive policy the child was asked to activate is verified
                        # HERE too, on the very environment this launch built: a start
                        # whose composed receive policy cannot be established is not a
                        # verified stacked service, whatever the health endpoint says.
                        # (The child process performs the same check before it imports
                        # Letta and refuses to start without it.)
                        try:
                            activation = module.verify_receive_gate(
                                env, module=module)
                        except module.MulticallPolicyError as exc:
                            raise RuntimeError(
                                "the stacked service's composed receive policy could not "
                                f"be established: {exc}") from exc
                        if not activation["active"]:
                            raise RuntimeError(
                                "the stacked service was started without an active receive "
                                "compatibility policy; treat this service as unverified")
                        started["receive_policy_verified"] = {
                            "contract": activation["contract"],
                            "profile_version": activation["profile_version"],
                            "manifest": activation["manifest"],
                            "receive_owned_files": sorted(
                                activation.get("receive_owned_files") or {}),
                            "parallel_tool_calls": False,
                            "execute_serially": True}
                    return started
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        raise RuntimeError("Letta health/version not confirmed; inspect status; no automatic retry")

    def process_matches(self, state: dict) -> bool:
        try:
            command = Path(f"/proc/{int(state['pid'])}/cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            return False
        command = [part for part in command if part]
        if not command:  # A dead/zombie process has no executable command line.
            return False
        executable = self.venv / "bin/letta"
        if state.get("executable") != str(executable) or type(state.get("port")) is not int:
            raise RuntimeError("unexpected recorded Letta executable/port; refuse signaling")
        cli_argv = [str(executable), "server", "--host", "127.0.0.1", "--port", str(state["port"])]
        mode = state.get("launch_mode", "direct")  # Pre-bootstrap records remain supported.
        if mode == "bounded_nltk_startup":
            expected = [str(self.venv / "bin/python"), "-u",
                        str(self.project / "scripts/deployment/letta_bootstrap.py")] + cli_argv
            allowed = [expected]
            if state.get("launch_argv") != expected or state.get("letta_argv") != cli_argv:
                raise RuntimeError("wrapper process record differs from fixed argv; refuse signaling")
        elif mode == "direct":
            # A Python console script appears in /proc with its shebang
            # interpreter prepended. Accept only that exact dedicated path.
            shebang = executable.read_bytes().splitlines()[0].decode("utf-8")
            interpreter = shebang[2:] if shebang.startswith("#!") else ""
            interpreter_path = Path(interpreter)
            if (interpreter_path.parent != self.venv / "bin"
                    or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", interpreter_path.name)):
                raise RuntimeError("unexpected Letta shebang; refuse signaling")
            allowed = [cli_argv, [interpreter] + cli_argv]
            if "launch_argv" in state and state["launch_argv"] != cli_argv:
                raise RuntimeError("direct process record differs from fixed argv; refuse signaling")
        else:
            raise RuntimeError("unknown Letta launch mode; refuse signaling")
        if command not in [[part.encode() for part in argv] for argv in allowed]:
            raise RuntimeError("PID belongs to an unexpected command; refuse signaling")
        return True

    def status(self) -> dict:
        db = self.db_config()
        pg = self.pg(db, "pg_ctl", ["-D", str(PGDATA), "status"], check=False)
        result = {"postgres_status": redacted(pg.stdout + pg.stderr, self.password),
                  "postgres_running": pg.returncode == 0, "letta_record_exists": self.process.exists()}
        if self.process.exists():
            state = json.loads(self.process.read_text())
            result["letta_process_matches"] = self.process_matches(state)
            log = Path(state["private_log"])
            if not log.resolve().is_relative_to((self.base / "runs").resolve()):
                raise RuntimeError("unexpected process log path")
            if log.exists():
                result["letta_log_tail_redacted"] = redacted(log.read_text(errors="replace")[-10000:], self.password)
        return result

    def stop(self) -> dict:
        db = self.db_config()
        if self.process.exists():
            state = json.loads(self.process.read_text())
            if self.process_matches(state):
                os.kill(state["pid"], 15)
                for _ in range(40):
                    if not self.process_matches(state):
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError("Letta did not stop after SIGTERM; no escalation/forced kill")
            self.process.rename(self.record / "stopped-letta-process.json")
        pg = self.pg(db, "pg_ctl", ["-D", str(PGDATA), "status"], check=False)
        if pg.returncode == 0:
            self.check_pg_pid(db)
            self.pg(db, "pg_ctl", ["-D", str(PGDATA), "-m", "fast", "-w", "stop"])
        return {"stopped": True, "preserved": "credentials, PostgreSQL data, source, venv and all records"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare-db", "start-db", "migrate", "start", "status", "stop"))
    parser.add_argument("--project", type=Path, default=Path("/root/rivermind-data/agent-erratum"))
    parser.add_argument("--model-url", type=loopback_url, default="http://127.0.0.1:8000/v1")
    parser.add_argument("--port", type=int, default=8283)
    parser.add_argument("--explicit-llm-config", action="store_true",
                        help="Start without auto-registering a vLLM provider; agents must supply LLMConfig")
    parser.add_argument("--bounded-nltk-startup", action="store_true",
                        help="Opt-in: validate English punkt, bound only startup NLTK download I/O to 10s")
    parser.add_argument("--multicall-manifest", type=Path, default=None,
                        help="Opt-in: reviewed AE multicall manifest; selects the 0.2 service mode")
    parser.add_argument("--multicall-support", type=Path, default=None,
                        help="Directory holding the project's ae_multicall.py (with --multicall-manifest)")
    parser.add_argument("--multicall-profile", default="ae-multicall-receive-compat-1",
                        help="The reviewed profile value to require in the service process")
    parser.add_argument("--patch-stack-manifest", type=Path, default=None,
                        help="Opt-in: reviewed STACKED (multicall + no-compaction) manifest")
    parser.add_argument("--patch-stack-support", type=Path, default=None,
                        help="Directory holding the project's ae_multicall.py (with the stack manifest)")
    parser.add_argument("--patch-stack-profile", default=STACK_PROFILE_VERSION,
                        help="The stacked profile value to require in the service process")
    parser.add_argument("--tokenizer-json", type=Path, default=None,
                        help="The official tokenizer.json the service gate counts with")
    parser.add_argument("--tokenizer-config", type=Path, default=None,
                        help="The official tokenizer_config.json the service gate counts with")
    parser.add_argument("--tokenizer-target", default=None,
                        help="The target model the run declares (its own assets are required)")
    parser.add_argument("--tokenizer-asset-target", default=None,
                        help="The model the asset files actually belong to")
    parser.add_argument("--tokenizer-revision", default=None,
                        help="The asset revision/hash label recorded in the run record")
    parser.add_argument("--byte-gate-count-basis", default=None,
                        help="declare the model-specific byte-gate policy instead of a "
                             "tokenizer; the service then bounds each request by its own "
                             "bytes and never counts tokens")
    parser.add_argument("--byte-gate-max-request-bytes", type=int, default=None,
                        help="the declared pre-send REQUEST-BYTE budget for that policy")
    parser.add_argument("--byte-gate-wire-model", default=None,
                        help="the ONE wire model this byte policy may bound; the service "
                             "refuses any other model under it")
    parser.add_argument("--transport-profile", default=None,
                        help="declare a reviewed transport profile whose REQUIRED request "
                             "fields (a provider's explicit non-thinking mode) the service "
                             "must put on the requests it sends; undeclared adds nothing")
    parser.add_argument("--tokenizer-allow-exploration", action="store_true",
                        help="Explicitly allow a non-target counting asset; the run is "
                             "then labelled as an exploration estimate")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    if args.bounded_nltk_startup and args.action != "start":
        parser.error("--bounded-nltk-startup is only valid for start")
    if args.explicit_llm_config and args.action != "start":
        parser.error("--explicit-llm-config is only valid for start")
    if (args.multicall_manifest is None) != (args.multicall_support is None):
        parser.error("--multicall-manifest and --multicall-support must be given together")
    if (args.patch_stack_manifest is None) != (args.patch_stack_support is None):
        parser.error("--patch-stack-manifest and --patch-stack-support must be given "
                     "together")
    if args.multicall_manifest is not None and args.patch_stack_manifest is not None:
        # Two service contracts over two different trees. The stacked launch activates
        # the receive policy ITSELF, so passing both is never needed and could only
        # produce an environment with two contradictory manifests.
        parser.error("declare either the multicall runtime or the patch stack runtime, "
                     "not both: the stacked runtime already declares the receive policy "
                     "it composes")
    if args.multicall_manifest is not None:
        if args.action != "start":
            parser.error("the AE multicall runtime is only valid for start")
        if not args.bounded_nltk_startup:
            parser.error("the AE multicall runtime requires --bounded-nltk-startup "
                         "(its gate and load receipt run in the bootstrap wrapper)")
    if args.patch_stack_manifest is not None:
        if args.action != "start":
            parser.error("the AE patch stack runtime is only valid for start")
        if not args.bounded_nltk_startup:
            parser.error("the AE patch stack runtime requires --bounded-nltk-startup "
                         "(its gate, composed receive policy and load receipt run in the "
                         "bootstrap wrapper)")
    os.umask(0o077)
    deploy = Deployment(args.project)
    try:
        if args.action == "prepare-db":
            result = deploy.prepare_db()
        elif args.action == "start-db":
            deploy.pg_start(deploy.db_config())
            result = {"postgres_started": True}
        elif args.action == "migrate":
            result = deploy.migrate()
        elif args.action == "start":
            runtime = None
            if args.multicall_manifest is not None:
                runtime = MulticallRuntime(manifest=args.multicall_manifest,
                                           support=args.multicall_support,
                                           receipt=deploy.record / "multicall-load.json",
                                           profile=args.multicall_profile)
            stack = None
            if args.patch_stack_manifest is not None:
                stack = PatchStackRuntime(
                    manifest=args.patch_stack_manifest, support=args.patch_stack_support,
                    receipt=deploy.record / "patch-stack-load.json",
                    profile=args.patch_stack_profile,
                    tokenizer_json=args.tokenizer_json, tokenizer_config=args.tokenizer_config,
                    tokenizer_target=args.tokenizer_target,
                    tokenizer_asset_target=args.tokenizer_asset_target,
                    tokenizer_revision=args.tokenizer_revision,
                    allow_exploration=args.tokenizer_allow_exploration,
                    byte_gate_count_basis=args.byte_gate_count_basis,
                    byte_gate_max_request_bytes=args.byte_gate_max_request_bytes,
                    byte_gate_wire_model=args.byte_gate_wire_model,
                    transport_profile=args.transport_profile)
            result = deploy.start(args.model_url, args.port, bounded_nltk_startup=args.bounded_nltk_startup,
                                  explicit_llm_config=args.explicit_llm_config,
                                  multicall=runtime, patch_stack=stack)
        elif args.action == "status":
            result = deploy.status()
        else:
            result = deploy.stop()
        report = {"action": args.action, "success": True, "record": str(deploy.record), "result": result}
        write_new(deploy.record / "result.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        report = {"action": args.action, "success": False, "record": str(deploy.record),
                  "error": redacted(f"{type(exc).__name__}: {exc}", deploy.password),
                  "note": "No auto-retry or cleanup. Preserve partial state and inspect before recovery."}
        write_new(deploy.record / "result.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
