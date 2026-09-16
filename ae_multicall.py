"""Versioned receive-side compatibility for multiple client tool calls.

## Why this module exists

The pinned Letta agent (`letta/agents/letta_agent_v3.py`) enforces
`parallel_tool_calls=false` by **silently truncating** a provider response that
contains more than one tool call to its first call, before the approval message
is ever built:

    if len(tool_calls) > 1 and not active_llm_config.parallel_tool_calls:
        ...
        tool_calls = [tool_calls[0]]

A real capture showed the provider returning four `memory_update` calls in one
assistant response while the next agent request carried only the first one.
The four native ids were distinct but shared their first 29 characters, which is
`TOOL_CALL_ID_MAX_LEN` (`letta/constants.py`), and several serializers also
truncate ids to that length (`letta/schemas/message.py`).

## What this module is

An explicit, versioned **compatibility policy** for the receive side. It is NOT
a claim that this matches old Letta behaviour, and it does not turn
`parallel_tool_calls=false` into `true`. The outbound request still says
`parallel_tool_calls: false`; the policy only states what this project does when
a provider returns multiple already-declared client tool calls anyway:

1. keep every call, in the provider's original order;
2. preserve every native tool-call id end to end (no truncation on the
   compatibility path);
3. validate the complete batch BEFORE any tool side effect;
4. execute the calls strictly serially, in order, each one based on the state
   committed by the previous one;
5. submit exactly one tool return per call, in that same order;
6. never silently drop a call. When the policy is not enabled, a multi-call
   response is a hard stop, not a truncation.

This module owns only the policy, the id rules and the batch gate. The Letta
bridge applies it; the RE input audit re-states the same policy so a capture can
declare exactly which protocol produced it.

## Runtime activation (a loaded source is not an active policy)

The patched receive point reads its own declaration (`AE_LETTA_MULTICALL_PROFILE`)
and verifies it before it touches a received call list; a service started from the
stacked checkout WITHOUT that declaration ran the pinned upstream truncation and
silently dropped calls (the live r4 capture). The stacked profile therefore
COMPOSES this policy and requires it to be declared with it, and
`verify_receive_gate` / `verify_receive_contract` resolve which contract is in
force and verify it against that contract's manifest - never against the variable's
presence. A declared contract that cannot be established is an explicit refusal.
"""
from __future__ import annotations

from copy import deepcopy
# The module is loaded by FILE LOCATION from the deployment launcher (the script
# directory is on sys.path, not the project root), so it must import the module it
# decorates rather than relying on its own import side effects.
import dataclasses
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Profile identity
# ---------------------------------------------------------------------------

#: Version of the receive-side compatibility protocol implemented here. Bump it
#: whenever the gate, the id rule or the ordering rule changes; every capture
#: must record the value it actually ran under.
PROFILE_VERSION = "ae-multicall-receive-compat-1"

#: Machine-readable policy key expected in an opt-in RE pair config.
CONFIG_KEY = "multicall_profile"

#: Environment variable that enables the matching pinned-Letta patch. The patch
#: only changes behaviour when this is exactly ``PROFILE_VERSION``; every other
#: value (including unset) keeps the original upstream behaviour.
LETTA_ENV_VAR = "AE_LETTA_MULTICALL_PROFILE"

#: Environment variable carrying the SHA-256 of this module, checked by the
#: patched Letta entry point so "enabled" can never mean "an undeclared or
#: edited policy". See ``ae_multicall_manifest.json``.
MODULE_SHA_ENV_VAR = "AE_LETTA_MULTICALL_MODULE_SHA256"

#: Environment variable naming the reviewed manifest that declares the compat
#: module hash and the post-patch Letta file hashes. Read at gate time so the
#: patch never embeds a hash in the file it is verifying.
MANIFEST_ENV_VAR = "AE_LETTA_MULTICALL_MANIFEST"

#: Manifest schema this gate understands.
MANIFEST_SCHEMA = "ae-letta-multicall-manifest-1"

#: Where the SERVICE process writes the load receipt that a live-loading claim
#: must carry. The parent process only tells the child where to write it; the
#: receipt's contents are produced by the child after it has imported the support
#: module and verified the checkout it actually loaded.
LAUNCH_RECEIPT_ENV_VAR = "AE_LETTA_MULTICALL_LOAD_RECEIPT"

#: Fields a service-produced load receipt must carry. `instance_id` ties the
#: receipt to one process instance, so a receipt cannot be replayed for a
#: different run, and `module_path`/`checkout` are the paths that process really
#: imported and hashed.
LAUNCH_RECEIPT_FIELDS = ("profile_version", "instance_id", "module_path",
                         "module_sha256", "checkout", "patched_files", "manifest_sha256",
                         "source_verified", "source_verification")

#: The pinned upstream body that the patch replaces, as it exists at
#: `56ba9c25552605eec89de8ed3dc6394b625c1993`. Kept here so a patched checkout
#: can be checked against the baseline it claims to patch.
UPSTREAM_BASELINE = {
    "letta/agents/letta_agent_v3.py":
        "1f11745d6ae86e64e90c76e287d25552a6c7823a8109b178da6951b21c788166",
    "letta/schemas/message.py":
        "c50de8d2792645a51f34f8f264c85bcea8ad252527270e96ebbe23bdd8a8e7e6",
}

#: `letta/constants.py:67`. The compatibility path must never shorten an id to
#: this length. It is recorded (not imported) so a bare environment can still
#: state the constraint it is relaxing.
UPSTREAM_TOOL_CALL_ID_MAX_LEN = 29

#: Attribute names a provider/Letta payload may use for the same id. Both are
#: accepted on the receive side and preserved exactly as received.
ID_KEYS = ("tool_call_id", "id")

#: The exact set of files the reviewed patch touches. A manifest that omits,
#: adds or renames any of these does not describe the reviewed patch, so it is
#: refused rather than read as "some files were patched".
REQUIRED_PATCHED_FILES = (
    "letta/agents/letta_agent_v3.py",
    "letta/schemas/message.py",
    "letta/helpers/ae_multicall_compat.py",
)

#: For each patched file: the pinned upstream digest it starts from, or None for a
#: file the patch adds. The baseline is CHECKED against this, never taken from the
#: manifest on trust.
PATCH_BASELINE = {
    "letta/agents/letta_agent_v3.py": UPSTREAM_BASELINE["letta/agents/letta_agent_v3.py"],
    "letta/schemas/message.py": UPSTREAM_BASELINE["letta/schemas/message.py"],
    "letta/helpers/ae_multicall_compat.py": None,
}


class MulticallPolicyError(RuntimeError):
    """The declared compatibility policy is absent, unknown or inconsistent."""


class MulticallRefused(RuntimeError):
    """The declared receive compatibility could not be established.

    Raised at the receive point while the provider's call list is still complete,
    so the caller stops with every received call intact and no tool side effect.
    This is the difference between a bounded refusal and silently falling back to
    the upstream truncation, which would drop calls.
    """


class BatchRejected(RuntimeError):
    """The whole pending batch is invalid; nothing was executed."""

    def __init__(self, code, detail=""):
        self.code = code
        self.detail = detail
        super().__init__(code + (": " + detail if detail else ""))


@dataclasses.dataclass(frozen=True)
class MulticallPolicy:
    """One immutable, hashable declaration of the receive-side policy."""

    version: str
    retain_all_calls: bool
    preserve_native_ids: bool
    execute_serially: bool
    capture_order: str
    unsupported_batch: str
    wire_parallel_tool_calls: bool

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "retain_all_calls": self.retain_all_calls,
            "preserve_native_ids": self.preserve_native_ids,
            "execute_serially": self.execute_serially,
            "capture_order": self.capture_order,
            "unsupported_batch": self.unsupported_batch,
            "wire_parallel_tool_calls": self.wire_parallel_tool_calls,
        }


#: The single reviewed policy. Every field is explicit and asserted below so a
#: later edit cannot quietly weaken one of them.
CURRENT_POLICY = MulticallPolicy(
    version=PROFILE_VERSION,
    retain_all_calls=True,
    preserve_native_ids=True,
    execute_serially=True,
    capture_order="provider_response_order",
    unsupported_batch="stop_before_any_side_effect",
    # The request contract is unchanged: the pair still declares false.
    wire_parallel_tool_calls=False,
)

_REQUIRED_TRUE = ("retain_all_calls", "preserve_native_ids", "execute_serially")
_REQUIRED_EQ = {
    "version": PROFILE_VERSION,
    "capture_order": "provider_response_order",
    "unsupported_batch": "stop_before_any_side_effect",
    # A compatibility policy that switched this to true would be a different,
    # unreviewed protocol; `validate_config` refuses it.
    "wire_parallel_tool_calls": False,
}


def validate_policy(value) -> dict:
    """Return the declared policy as a dict, refusing anything unreviewed.

    Accepts the exact policy dict (as written into a config or a manifest) and
    rejects unknown keys, missing keys and any weakened value.
    """
    if not isinstance(value, dict):
        raise MulticallPolicyError("multicall policy must be an explicit object")
    expected = set(CURRENT_POLICY.as_dict())
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unknown = sorted(set(value) - expected)
        raise MulticallPolicyError(
            "multicall policy keys differ from the reviewed set"
            + (f"; missing={missing}" if missing else "")
            + (f"; unknown={unknown}" if unknown else ""))
    declared = MulticallPolicy(**{key: value[key] for key in expected})
    for key in _REQUIRED_TRUE:
        if declared.__dict__[key] is not True:
            raise MulticallPolicyError(f"multicall policy field {key!r} must be true")
    for key, wanted in _REQUIRED_EQ.items():
        if declared.__dict__[key] != wanted:
            raise MulticallPolicyError(
                f"multicall policy field {key!r} must be {wanted!r}, got "
                f"{declared.__dict__[key]!r}")
    return declared.as_dict()


def declared_policy() -> dict:
    """The reviewed policy, ready to embed in a config or a manifest."""
    return CURRENT_POLICY.as_dict()


def resolve_policy(config: dict, *, allow_absent: bool = True):
    """Read the opt-in policy out of a config.

    Absent means the old protocol: return ``None`` and every caller keeps its
    previous behaviour. A present-but-wrong declaration is always an error, so a
    typo cannot silently buy the old truncating path. The returned value is the
    frozen :class:`MulticallPolicy`, ready to hand to a bridge.
    """
    if not isinstance(config, dict):
        raise MulticallPolicyError("config must be an object")
    if CONFIG_KEY not in config:
        if allow_absent:
            return None
        raise MulticallPolicyError(f"{CONFIG_KEY} must be declared explicitly")
    declared = validate_policy(config[CONFIG_KEY])
    return MulticallPolicy(**declared)


# ---------------------------------------------------------------------------
# Tool-call id rules
# ---------------------------------------------------------------------------

def retained_length(native_id) -> int:
    """How much of a native id survives the compatibility path it is given."""
    if not isinstance(native_id, str) or not native_id:
        raise BatchRejected("missing_tool_call_id")
    return len(native_id)


def collision_groups(native_ids) -> list:
    """Native ids that collapse onto the same 29-character upstream prefix.

    This is the exact failure the compatibility path exists to remove. It is
    reported for evidence; it is NOT a hash and makes no "no collision"
    guarantee. The compatibility path never truncates, so within one batch the
    returned groups only matter for a capture that ran without the policy.
    """
    seen = {}
    for native_id in native_ids:
        if not isinstance(native_id, str) or not native_id:
            raise BatchRejected("missing_tool_call_id")
        seen.setdefault(native_id[:UPSTREAM_TOOL_CALL_ID_MAX_LEN], []).append(native_id)
    return [sorted(set(group)) for group in seen.values() if len(set(group)) > 1]


def id_mapping(native_ids) -> dict:
    """The id mapping this policy adopts.

    The reviewed rule is **identity**: every native id is retained end to end,
    so the mapping is exactly ``{native: native}``. There is deliberately no
    digest or shortening fallback -- a mapping whose image differs from its
    domain would be a new protocol requiring its own review, and inventing one
    here would hide the very collision this task is about.
    """
    mapping = {}
    for native_id in native_ids:
        if not isinstance(native_id, str) or not native_id:
            raise BatchRejected("missing_tool_call_id")
        mapping[native_id] = native_id
    if len(mapping) != len(list(native_ids)):
        raise BatchRejected("duplicate_tool_call_id")
    return mapping


# ---------------------------------------------------------------------------
# Batch parsing and the pre-side-effect gate
# ---------------------------------------------------------------------------

_TOOL_CALL_FIELDS = ("name", "arguments")
_KNOWN_CALL_FIELDS = {
    "id", "tool_call_id", "type", "name", "arguments", "function",
    "requestor", "source", "index",
}
#: Native Letta tool types that the server, not the client, executes. A batch
#: mixing one of these with declared client tools cannot be executed by this
#: bridge, so it stops instead of dropping the server-side call.
SERVER_SIDE_TOOL_TYPES = (
    "letta_core", "letta_memory", "letta_multi_agent", "letta_sleep", "letta_builtin",
)


def call_id(call: dict) -> str:
    """The preserved native id of one call, accepting either wire spelling."""
    if not isinstance(call, dict):
        raise BatchRejected("tool_call_not_an_object")
    found = [call[key] for key in ID_KEYS if isinstance(call.get(key), str) and call.get(key)]
    if not found:
        raise BatchRejected("missing_tool_call_id",
                            json.dumps(sorted(call), ensure_ascii=False))
    if len(set(found)) > 1:
        # `id` and `tool_call_id` must agree; a disagreement is tampering.
        raise BatchRejected("conflicting_tool_call_id_fields")
    return found[0]


def _call_name(call: dict) -> str:
    name = call.get("name")
    if not isinstance(name, str) or not name:
        function = call.get("function")
        if isinstance(function, dict):
            name = function.get("name")
    if not isinstance(name, str) or not name:
        raise BatchRejected("missing_tool_call_name", json.dumps(sorted(call), ensure_ascii=False))
    return name


def _call_arguments(call: dict) -> str:
    """The argument payload as an unmodified string, for the real executor.

    Only the container is normalized (a nested ``function`` object is lifted
    out); the argument text itself is never re-encoded, re-ordered or edited.
    """
    arguments = call.get("arguments")
    if not isinstance(arguments, str):
        function = call.get("function")
        if isinstance(function, dict):
            arguments = function.get("arguments")
    if not isinstance(arguments, str):
        raise BatchRejected("missing_tool_call_arguments",
                            json.dumps(sorted(call), ensure_ascii=False))
    return arguments


def _is_server_side(call: dict) -> bool:
    marker = call.get("type")
    if call.get("source") == "server" or call.get("requestor") == "server":
        return True
    return isinstance(marker, str) and marker in SERVER_SIDE_TOOL_TYPES


def batch_source(payload):
    """Normalize an approval payload into an ordered list plus its field form.

    Returns ``(calls, shape)``. ``shape`` is ``"tool_calls"`` or the legacy
    ``"tool_call"`` spelling, and is recorded so the audit can prove it did not
    confuse the two. Order is preserved exactly as received.
    """
    if not isinstance(payload, dict):
        raise BatchRejected("approval_payload_not_an_object")
    calls = payload.get("tool_calls")
    shape = "tool_calls"
    if calls is None:
        single = payload.get("tool_call")
        if single is None:
            raise BatchRejected("no_tool_calls_in_approval")
        calls, shape = [single], "tool_call"
    if not isinstance(calls, list) or not calls:
        raise BatchRejected("tool_calls_not_a_nonempty_list")
    if any(not isinstance(call, dict) for call in calls):
        raise BatchRejected("tool_call_not_an_object")
    return calls, shape


@dataclasses.dataclass(frozen=True)
class ValidatedBatch:
    """A batch that has passed every pre-execution check.

    Nothing here has been executed yet; ``calls`` is already in execution
    order and carries the preserved native ids.
    """

    ids: tuple
    names: tuple
    calls: tuple
    shape: str
    id_rule: str
    collision_groups: tuple

    def as_evidence(self) -> dict:
        return {
            "count": len(self.calls),
            "shape": self.shape,
            "id_rule": self.id_rule,
            "ids": list(self.ids),
            "names": list(self.names),
            "id_lengths": [len(value) for value in self.ids],
            "upstream_prefix_collision_groups": [list(group) for group in self.collision_groups],
        }


def validate_batch(payload, bindings, *, known_ids=(), memory_tool_name=None) -> ValidatedBatch:
    """Validate one complete pending batch BEFORE any side effect.

    Checks, in order: payload shape; a non-empty ordered list; per-call object,
    name and string arguments; the exact set of declared client tools (a call
    for an undeclared tool stops the whole batch rather than being dropped);
    no server-side tool mixed in; unique ids inside the batch; and no id repeated
    from an earlier batch.

    A group of distinct ids that would have collapsed onto the same upstream
    29-character prefix is **recorded**, not refused: the compatibility path
    keeps every id in full, so the collapse never happens and such a batch is
    exactly the case this policy exists to accept. Only an exact duplicate id is
    fatal, because that is ambiguous even with full ids.

    The returned batch is the only thing any caller may execute.
    """
    calls, shape = batch_source(payload)
    declared = set(bindings or ())
    if memory_tool_name is not None:
        declared = declared | {memory_tool_name}
    seen = set()
    ids, names, normalized = [], [], []
    for call in calls:
        native_id = call_id(call)
        name = _call_name(call)
        arguments = _call_arguments(call)
        if _is_server_side(call):
            raise BatchRejected("server_side_tool_in_client_batch", name)
        if name not in declared:
            raise BatchRejected("undeclared_tool_in_batch", name)
        if native_id in seen:
            raise BatchRejected("duplicate_tool_call_id_in_batch", native_id)
        if native_id in set(known_ids):
            raise BatchRejected("tool_call_id_reused_from_earlier_batch", native_id)
        seen.add(native_id)
        ids.append(native_id)
        names.append(name)
        # Rebuilt in the wire spelling the existing single-call executor reads,
        # with the argument TEXT passed through unchanged.
        normalized.append({"type": "tool", "tool_call_id": native_id, "name": name,
                           "arguments": arguments})
    return ValidatedBatch(ids=tuple(ids), names=tuple(names), calls=tuple(normalized),
                          shape=shape, id_rule="identity_retained_end_to_end",
                          collision_groups=tuple(tuple(group) for group in collision_groups(ids)))


def check_server_tool_types(tool_types) -> None:
    """Refuse a declared tool set that contains a server-side tool type.

    Called before a session is used, so an unsupported client/server mix is
    caught at setup rather than in the middle of a batch.
    """
    for tool_type in tool_types or ():
        if tool_type in SERVER_SIDE_TOOL_TYPES:
            raise BatchRejected("server_side_tool_declared", str(tool_type))


def identical_batch(a: "ValidatedBatch", b: "ValidatedBatch") -> bool:
    """True when two validated batches are the same calls in the same order."""
    return (a.ids == b.ids and a.names == b.names
            and [dict(call) for call in a.calls] == [dict(call) for call in b.calls]
            and a.shape == b.shape)


def returns_in_order(batch: ValidatedBatch, returns) -> list:
    """Bind exactly one return to each call, in batch order.

    Refuses a missing, extra, duplicated or reordered return instead of
    pairing by position blindly.
    """
    returns = list(returns or ())
    if len(returns) != len(batch.calls):
        raise BatchRejected("tool_return_count_mismatch",
                            f"calls={len(batch.calls)} returns={len(returns)}")
    expected = list(batch.ids)
    observed = []
    for result in returns:
        if not isinstance(result, dict):
            raise BatchRejected("tool_return_not_an_object")
        value = result.get("tool_call_id")
        if not isinstance(value, str) or not value:
            raise BatchRejected("tool_return_missing_id")
        observed.append(value)
    if observed != expected:
        raise BatchRejected("tool_return_order_or_identity_mismatch",
                            json.dumps({"expected": expected, "observed": observed},
                                       ensure_ascii=False))
    return deepcopy(returns)


def copy_batch(batch: ValidatedBatch) -> ValidatedBatch:
    """A defensive copy so a caller cannot mutate a validated batch in place."""
    return ValidatedBatch(ids=tuple(batch.ids), names=tuple(batch.names),
                          calls=tuple(deepcopy(list(batch.calls))), shape=batch.shape,
                          id_rule=batch.id_rule, collision_groups=tuple(batch.collision_groups))


# ---------------------------------------------------------------------------
# The patched-Letta gate
# ---------------------------------------------------------------------------

def live_module_sha(path=None) -> str:
    """SHA-256 of the compatibility module the gate is actually running."""
    target = Path(path) if path is not None else Path(__file__).resolve()
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _check_hex(label, value):
    if not isinstance(value, str) or len(value) != 64:
        raise MulticallPolicyError(f"{label} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError:
        raise MulticallPolicyError(f"{label} is not hexadecimal") from None


def verify_patch_identity(manifest, *, git=None):
    """Prove the manifest's patched digests equal pinned-baseline + reviewed patch.

    The pinned checkout is copied to a temporary tree, the reviewed patch is
    applied to that copy, and each patched file's bytes are compared with the
    manifest's `patched_sha256`. A candidate tree that merely reports its own
    digests - for example an unpatched upstream tree, or a tree with a missing
    file - cannot satisfy this. The shim, which the patch adds rather than edits,
    is compared with `shim_source`.

    Returns the applied digests on success and raises `MulticallPolicyError`.
    """
    import subprocess  # local: this is a verification helper, not a hot path
    import tempfile

    shim_source = Path(manifest.get("shim_source", "")) if manifest.get("shim_source") else None
    checkout = Path(manifest["letta_checkout"])
    patch = manifest.get("patch", {}).get("path")
    if not patch:
        raise MulticallPolicyError("the manifest must name the reviewed patch file")
    patch_path = Path(patch)
    if not patch_path.is_absolute():
        patch_path = checkout.parents[1] / patch if (checkout.parents[1] / patch).is_file() else Path(patch)
    if not patch_path.is_file():
        raise MulticallPolicyError(f"the reviewed patch is absent: {patch_path}")
    expected_patch_sha = manifest["patch"].get("sha256")
    _check_hex("patch.sha256", expected_patch_sha)
    if hashlib.sha256(patch_path.read_bytes()).hexdigest() != expected_patch_sha:
        raise MulticallPolicyError("the reviewed patch bytes do not match patch.sha256")
    baseline_tree = Path(manifest.get("baseline_checkout", "")).resolve() if manifest.get(
        "baseline_checkout") else None
    if baseline_tree is None or not baseline_tree.is_dir():
        raise MulticallPolicyError("the manifest must name the pinned baseline checkout")
    for name, expected in PATCH_BASELINE.items():
        if expected is None:
            continue
        target = baseline_tree / name
        if not target.is_file():
            raise MulticallPolicyError(f"the pinned baseline is missing {name}")
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise MulticallPolicyError(f"the pinned baseline {name} is not the pinned upstream file")
    command = [git or "git", "apply", str(patch_path)]
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "tree"
        shutil_copytree(baseline_tree, candidate)
        applied = subprocess.run(command, cwd=candidate, capture_output=True, text=True)
        if applied.returncode != 0:
            raise MulticallPolicyError(
                "the reviewed patch does not apply to the pinned baseline: "
                + (applied.stderr.strip() or applied.stdout.strip())[:200])
        shim = candidate / "letta/helpers/ae_multicall_compat.py"
        if shim_source is None or not shim_source.is_file():
            raise MulticallPolicyError("the manifest must name the reviewed shim source")
        shutil_copy(shim_source, shim)
        applied_digests = {}
        for name in REQUIRED_PATCHED_FILES:
            target = candidate / name
            if not target.is_file():
                raise MulticallPolicyError(f"applying the patch did not produce {name}")
            applied_digests[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    for name, digest in applied_digests.items():
        if manifest["patched_files"][name]["patched_sha256"] != digest:
            raise MulticallPolicyError(
                f"patched_files[{name!r}] does not equal pinned-baseline + reviewed patch")
    return applied_digests


def shutil_copytree(source, target):
    import shutil
    shutil.copytree(source, target, symlinks=True)


def shutil_copy(source, target):
    import shutil
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def validate_launch_receipt(receipt, manifest, manifest_sha256):
    """Check a service load receipt's CONTENTS against a fixed manifest.

    Shared by the production RUN entry point and the capture audit so both enforce
    one contract: naming a receipt file is never enough. Everything is recomputed
    from bytes on disk:

    * the exact field set, profile, a non-empty instance id, and the declared
      source-verification method;
    * `manifest_sha256` must be the digest of the manifest being used, which pins
      the receipt to one fixed manifest (the bootstrap references it; the manifest
      never references the receipt, so there is no digest cycle);
    * the compatibility-module path and bytes;
    * `checkout` equal to the manifest's declared checkout;
    * exactly the manifest's patched-file set, each entry a `{path, sha256}` pair
      that lives inside that checkout and whose bytes hash to that digest.

    Returns the validated receipt. Raises `MulticallPolicyError` on any defect.
    """
    if not isinstance(receipt, dict):
        raise MulticallPolicyError("the load receipt must be a JSON object")
    if set(receipt) != set(LAUNCH_RECEIPT_FIELDS):
        missing = sorted(set(LAUNCH_RECEIPT_FIELDS) - set(receipt))
        extra = sorted(set(receipt) - set(LAUNCH_RECEIPT_FIELDS))
        raise MulticallPolicyError(
            "[receipt_fields] the load receipt fields differ from the reviewed set"
            + (f"; missing={missing}" if missing else "")
            + (f"; unexpected={extra}" if extra else ""))
    if receipt["profile_version"] != PROFILE_VERSION:
        raise MulticallPolicyError("[receipt_profile] the receipt names another profile")
    if not isinstance(receipt["instance_id"], str) or not receipt["instance_id"]:
        raise MulticallPolicyError("[receipt_instance] the receipt has no instance id")
    if receipt["source_verified"] is not True:
        raise MulticallPolicyError("[receipt_source_unverified] the receipt does not "
                                   "declare a verified source")
    if receipt["source_verification"] != "importlib_find_spec_before_letta_import":
        raise MulticallPolicyError("[receipt_source_method] unknown source "
                                   "verification method")
    if receipt["manifest_sha256"] != manifest_sha256:
        raise MulticallPolicyError("[receipt_manifest_sha] the receipt was written for "
                                   "another manifest")
    module_path = Path(str(receipt["module_path"]))
    if not module_path.is_file():
        raise MulticallPolicyError("[receipt_module_absent] the receipt's module is absent")
    module_digest = hashlib.sha256(module_path.read_bytes()).hexdigest()
    if module_digest != receipt["module_sha256"]:
        raise MulticallPolicyError("[receipt_module_sha] the receipt's module bytes changed")
    if module_digest != manifest["compat_module"]["sha256"]:
        raise MulticallPolicyError("[receipt_module_manifest] the receipt's module is not "
                                   "the manifested one")
    checkout = Path(str(receipt["checkout"])).resolve()
    if checkout != Path(manifest["letta_checkout"]).resolve():
        raise MulticallPolicyError("[receipt_checkout] the receipt names another checkout")
    if not checkout.is_dir():
        raise MulticallPolicyError("[receipt_checkout_absent] the receipt's checkout is absent")
    files = receipt["patched_files"]
    if not isinstance(files, dict) or set(files) != set(REQUIRED_PATCHED_FILES):
        raise MulticallPolicyError("[receipt_file_set] the receipt's file set is not the "
                                   "reviewed one")
    for name in REQUIRED_PATCHED_FILES:
        entry = files[name]
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise MulticallPolicyError("[receipt_entry] a receipt entry must be "
                                       "{path, sha256}")
        expected = manifest["patched_files"][name]["patched_sha256"]
        if entry["sha256"] != expected:
            raise MulticallPolicyError("[receipt_digest] a receipt digest differs from "
                                       "the manifest")
        live = Path(str(entry["path"]))
        if not live.is_file():
            raise MulticallPolicyError("[receipt_file_absent] a receipt file is absent")
        if checkout not in live.resolve().parents:
            raise MulticallPolicyError("[receipt_file_outside_checkout] a receipt file is "
                                       "outside the declared checkout")
        if hashlib.sha256(live.read_bytes()).hexdigest() != entry["sha256"]:
            raise MulticallPolicyError("[receipt_file_changed] a receipt file changed on disk")
    return receipt


def manifest_path(environ=None) -> "Path | None":
    """The reviewed manifest path named by the environment, or None."""
    env = os.environ if environ is None else environ
    value = env.get(MANIFEST_ENV_VAR)
    return Path(value) if value else None


def read_manifest(manifest_path_value):
    """Read and structurally check the reviewed manifest; raise on any defect."""
    path = Path(manifest_path_value)
    if not path.is_file():
        raise MulticallPolicyError(f"the reviewed manifest is absent: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MulticallPolicyError(f"the reviewed manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise MulticallPolicyError("the reviewed manifest must be a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise MulticallPolicyError(
            f"unknown manifest schema {manifest.get('schema')!r}; expected {MANIFEST_SCHEMA!r}")
    if manifest.get("profile_version") != PROFILE_VERSION:
        raise MulticallPolicyError("the reviewed manifest declares another profile version")
    if manifest.get("upstream_baseline") != UPSTREAM_BASELINE:
        raise MulticallPolicyError("the reviewed manifest declares another upstream baseline")
    module = manifest.get("compat_module")
    if not isinstance(module, dict) or set(module) != {"path", "sha256"}:
        raise MulticallPolicyError(
            "the reviewed manifest must declare compat_module as exactly {path, sha256}")
    if module.get("path") != "ae_multicall.py":
        raise MulticallPolicyError(
            "the reviewed manifest must name ae_multicall.py as its compat module")
    _check_hex("compat_module.sha256", module.get("sha256"))
    files = manifest.get("patched_files")
    if not isinstance(files, dict):
        raise MulticallPolicyError("the reviewed manifest must declare a patched_files map")
    # Exactly the reviewed file set: missing, extra or renamed files are refused.
    if set(files) != set(REQUIRED_PATCHED_FILES):
        missing = sorted(set(REQUIRED_PATCHED_FILES) - set(files))
        extra = sorted(set(files) - set(REQUIRED_PATCHED_FILES))
        raise MulticallPolicyError(
            "patched_files must be exactly the reviewed file set"
            + (f"; missing={missing}" if missing else "")
            + (f"; unexpected={extra}" if extra else ""))
    for name in REQUIRED_PATCHED_FILES:
        item = files[name]
        if not isinstance(item, dict) or set(item) != {"baseline_sha256", "patched_sha256"}:
            raise MulticallPolicyError(
                f"patched_files[{name!r}] must declare exactly "
                "{baseline_sha256, patched_sha256}")
        baseline, patched = item["baseline_sha256"], item["patched_sha256"]
        expected = PATCH_BASELINE[name]
        if expected is None:
            if baseline is not None:
                raise MulticallPolicyError(
                    f"patched_files[{name!r}] is added by the patch and must declare a "
                    "null baseline_sha256")
        else:
            _check_hex(f"patched_files[{name!r}].baseline_sha256", baseline)
            if baseline != expected:
                raise MulticallPolicyError(
                    f"patched_files[{name!r}].baseline_sha256 is not the pinned upstream "
                    f"digest for that file")
        _check_hex(f"patched_files[{name!r}].patched_sha256", patched)
        if expected is not None and patched == expected:
            raise MulticallPolicyError(
                f"patched_files[{name!r}].patched_sha256 equals the unpatched baseline; "
                "the patch cannot be unapplied")
    checkout = manifest.get("letta_checkout")
    if not isinstance(checkout, str) or not checkout:
        raise MulticallPolicyError("the reviewed manifest must declare letta_checkout")
    # The pinned baseline the patch was applied to is part of the identity proof,
    # so a manifest that does not name it cannot be verified at all.
    baseline = manifest.get("baseline_checkout")
    if not isinstance(baseline, str) or not baseline:
        raise MulticallPolicyError("the reviewed manifest must declare baseline_checkout")
    if not isinstance(manifest.get("shim_source"), str) or not manifest["shim_source"]:
        raise MulticallPolicyError("the reviewed manifest must declare shim_source")
    return manifest


def verify_letta_gate(environ=None, *, module=None, manifest=None) -> bool:
    """Fail-closed activation check for the patched pinned Letta source.

    Returns True only when the environment names the reviewed profile version, a
    reviewed manifest exists at the declared path with the reviewed schema, the
    manifest's compatibility-module digest is the one the environment declares AND
    the bytes of the module that is actually executing, and every patched Letta
    file in the manifest's checkout hashes to its manifested digest.

    Every missing, malformed, empty or tampered fact returns False. The patched
    code treats "declared but not verified" as a refusal, never as a silent
    fallback to truncation.

    This is also the entry the (unmodified) patched checkout calls at its receive
    point, so it resolves WHICH contract is in force: when the environment declares
    the STACKED profile, the stacked contract - including the receive policy that
    stack composes - is the one that must verify (`verify_stacked_receive_contract`).
    The multicall-only manifest is deliberately NOT consulted there: the stacked tree
    carries the capacity patch in the same agent file, so its bytes differ from the
    multicall-only manifest's by design, and a stacked declaration that cannot
    establish its own contract returns False here so the receive point refuses
    instead of truncating.
    """
    env = os.environ if environ is None else environ
    if env.get(STACK_ENV_VAR):
        try:
            verify_stacked_receive_contract(env, module=module)
        except MulticallPolicyError:
            return False
        return True
    if env.get(LETTA_ENV_VAR) != PROFILE_VERSION:
        return False
    declared_module = env.get(MODULE_SHA_ENV_VAR)
    if not isinstance(declared_module, str) or len(declared_module) != 64:
        return False
    path = manifest_path(env) if manifest is None else Path(manifest)
    if path is None:
        return False
    try:
        record = read_manifest(path)
    except MulticallPolicyError:
        return False
    if record["compat_module"]["sha256"] != declared_module:
        return False
    # The manifest, the environment and the executing module must be one policy,
    # byte for byte. `module` lets a caller (or a test) name the loaded module
    # explicitly instead of relying on sys.modules.
    live = live_module_sha(Path(module.__file__) if module is not None else None)
    if live != declared_module:
        return False
    checkout = Path(record["letta_checkout"])
    if not checkout.is_dir():
        return False
    for name, item in record["patched_files"].items():
        target = checkout / name
        if not target.is_file():
            return False
        if hashlib.sha256(target.read_bytes()).hexdigest() != item["patched_sha256"]:
            return False
    return True


# ---------------------------------------------------------------------------
# The STACKED checkout: the reviewed multicall patch PLUS the no-compaction
# capacity patch (R3-1)
# ---------------------------------------------------------------------------
#
# Everything above describes the multicall-only protocol and is unchanged. A run
# that declares the capacity contract needs BOTH patches in the checkout the
# service really imports, so it declares this second, additive profile instead.
# The two contracts never share a manifest: the multicall manifest stays exactly
# what the earlier rounds reviewed, and this one names the stacked file set.

#: Opt-in environment variable for the stacked profile value.
STACK_ENV_VAR = "AE_LETTA_PATCH_STACK_PROFILE"

#: The stacked profile's own version. A service that is asked for this value and
#: cannot verify the stack refuses to start; nothing falls back to multicall-only.
STACK_PROFILE_VERSION = "ae-no-compaction-stack-1"

#: Environment variable naming the stacked manifest.
STACK_MANIFEST_ENV_VAR = "AE_LETTA_PATCH_STACK_MANIFEST"

#: Environment variable naming the load receipt THIS service process must write.
STACK_RECEIPT_ENV_VAR = "AE_LETTA_PATCH_STACK_RECEIPT"

#: Schema of the stacked manifest.
STACK_SCHEMA = "ae-letta-patch-stack-manifest-1"

#: Schema of the CAPACITY manifest the stack names as the second link of its chain.
STACK_CAPACITY_SCHEMA = "ae-letta-no-compaction-manifest-2"

#: The files the stacked chain's CAPACITY patch changes ON TOP of the reviewed
#: multicall patch. Recorded here so the receive contract can tell the files the
#: capacity patch owns from the files the RECEIVE patch owns: a stack whose declared
#: capacity change set differs from this one is not the reviewed chain.
CAPACITY_PATCHED_FILES = (
    "letta/agents/letta_agent_v3.py",
    "letta/services/summarizer/compact.py",
)

#: Files the stack CHANGES that already exist in the pinned checkout.
STACK_PATCHED_FILES = (
    "letta/agents/letta_agent_v3.py",
    "letta/schemas/message.py",
    "letta/helpers/ae_multicall_compat.py",
    "letta/services/summarizer/compact.py",
)

#: Files the stack ADDS.
STACK_NEW_FILES = (
    "letta/helpers/ae_no_compaction.py",
    "letta/helpers/ae_qwen_tokenizer.py",
)

#: file -> (importable module, file name). The gate resolves each of these through
#: the REAL import machinery, because a path in a manifest is not what a service
#: process imports.
STACK_SOURCES = {
    "letta/agents/letta_agent_v3.py": ("letta.agents.letta_agent_v3", "letta_agent_v3.py"),
    "letta/schemas/message.py": ("letta.schemas.message", "message.py"),
    "letta/helpers/ae_multicall_compat.py": ("letta.helpers.ae_multicall_compat",
                                             "ae_multicall_compat.py"),
    "letta/services/summarizer/compact.py": ("letta.services.summarizer.compact", "compact.py"),
    "letta/helpers/ae_no_compaction.py": ("letta.helpers.ae_no_compaction",
                                          "ae_no_compaction.py"),
    "letta/helpers/ae_qwen_tokenizer.py": ("letta.helpers.ae_qwen_tokenizer",
                                           "ae_qwen_tokenizer.py"),
}

#: Fields of a service-produced STACK load receipt. Separate from the multicall
#: receipt on purpose: that receipt's field set is exact-checked, so adding to it
#: would change a contract earlier rounds already sealed.
STACK_RECEIPT_FIELDS = ("profile_version", "instance_id", "module_path", "module_sha256",
                        "checkout", "patched_files", "new_files", "manifest_sha256",
                        "source_verified", "source_verification")

#: The only accepted source-verification method: the receipt records what
#: `importlib.util.find_spec` resolved in THIS process, before Letta was imported.
STACK_SOURCE_VERIFICATION = "importlib_find_spec_before_letta_import"


def _check_stack_digest(label, value):
    if not isinstance(value, str) or len(value) != 64:
        raise MulticallPolicyError(f"{label} must be a 64-hex digest")
    try:
        int(value, 16)
    except ValueError:
        raise MulticallPolicyError(f"{label} must be a 64-hex digest") from None


def read_stack_manifest(manifest_path_value):
    """Read and structurally check the STACKED manifest; raise on any defect.

    The file sets are exact: a manifest that omits, adds or renames a changed or a
    new file does not describe this stack, so it is refused instead of being read
    as "some of the capacity patch was applied".
    """
    path = Path(manifest_path_value)
    if not path.is_file():
        raise MulticallPolicyError(f"the stacked manifest is absent: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MulticallPolicyError(f"the stacked manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise MulticallPolicyError("the stacked manifest must be a JSON object")
    if manifest.get("schema") != STACK_SCHEMA:
        raise MulticallPolicyError(
            f"unknown stacked manifest schema {manifest.get('schema')!r}; "
            f"expected {STACK_SCHEMA!r}")
    if manifest.get("profile_version") != STACK_PROFILE_VERSION:
        raise MulticallPolicyError("the stacked manifest declares another profile version")
    module = manifest.get("compat_module")
    if not isinstance(module, dict) or set(module) != {"path", "sha256"}:
        raise MulticallPolicyError(
            "the stacked manifest must declare compat_module as exactly {path, sha256}")
    if module.get("path") != "ae_multicall.py":
        raise MulticallPolicyError(
            "the stacked manifest must name ae_multicall.py as its compat module")
    _check_stack_digest("compat_module.sha256", module.get("sha256"))
    for key, expected in (("patched_files", STACK_PATCHED_FILES),
                          ("new_files", STACK_NEW_FILES)):
        files = manifest.get(key)
        if not isinstance(files, dict):
            raise MulticallPolicyError(f"the stacked manifest must declare a {key} map")
        if set(files) != set(expected):
            missing = sorted(set(expected) - set(files))
            extra = sorted(set(files) - set(expected))
            raise MulticallPolicyError(
                f"{key} must be exactly the stacked file set"
                + (f"; missing={missing}" if missing else "")
                + (f"; unexpected={extra}" if extra else ""))
        for name in expected:
            _check_stack_digest(f"{key}[{name!r}]", files[name])
    for key in ("letta_checkout", "baseline_checkout", "multicall_manifest",
                "no_compaction_manifest"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value:
            raise MulticallPolicyError(f"the stacked manifest must declare {key}")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(STACK_SOURCES):
        raise MulticallPolicyError(
            "the stacked manifest must map every stacked file to its module")
    for name, (module_name, filename) in STACK_SOURCES.items():
        entry = sources[name]
        if (not isinstance(entry, dict) or set(entry) != {"module", "filename"}
                or entry["module"] != module_name or entry["filename"] != filename):
            raise MulticallPolicyError(
                f"sources[{name!r}] must be exactly the reviewed module and file name")
    return manifest


def verify_stack_gate(environ=None, *, module=None) -> bool:
    """Fail-closed activation check for the STACKED checkout.

    Returns True only when the environment names this profile, the stacked manifest
    exists with the reviewed schema and file sets, its compatibility-module digest
    is the digest of the module that is actually executing, the declared checkout is
    a directory, and EVERY changed and new file in it hashes to its manifested
    digest. Any missing, malformed or tampered fact returns False, and the service
    treats that as a refusal rather than as "run with less checking".
    """
    env = os.environ if environ is None else environ
    if env.get(STACK_ENV_VAR) != STACK_PROFILE_VERSION:
        return False
    declared_module = env.get(MODULE_SHA_ENV_VAR)
    if not isinstance(declared_module, str) or len(declared_module) != 64:
        return False
    value = env.get(STACK_MANIFEST_ENV_VAR)
    if not value:
        return False
    try:
        record = read_stack_manifest(value)
    except MulticallPolicyError:
        return False
    if record["compat_module"]["sha256"] != declared_module:
        return False
    live = live_module_sha(Path(module.__file__) if module is not None else None)
    if live != declared_module:
        return False
    checkout = Path(record["letta_checkout"])
    if not checkout.is_dir():
        return False
    for key in ("patched_files", "new_files"):
        for name, digest in record[key].items():
            target = checkout / name
            if not target.is_file():
                return False
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                return False
    return True


# ---------------------------------------------------------------------------
# The RUNTIME receive declaration: which contract the service is under
# ---------------------------------------------------------------------------
#
# R3-2 (the live r4 defect): a service was started from the STACKED checkout with
# `AE_LETTA_PATCH_STACK_PROFILE` declared but no receive declaration, so the patched
# receive point took the `NOT_DECLARED` branch of `ae_multicall_compat` and the pinned
# upstream code truncated every multi-call response to its first call. Loading the
# right SOURCE is not activating the POLICY: the receive point decides from its own
# declaration, and that declaration has to be verified against whichever contract is
# in force.
#
# Exactly one contract can be in force, and each has its own manifest:
#
#   * multicall-only: `AE_LETTA_MULTICALL_PROFILE` + `AE_LETTA_MULTICALL_MANIFEST`,
#     verified by `verify_letta_gate` against the multicall manifest (unchanged);
#   * stacked: `AE_LETTA_PATCH_STACK_PROFILE` + `AE_LETTA_PATCH_STACK_MANIFEST`, which
#     COMPOSES the receive policy - so the stacked declaration REQUIRES the receive
#     declaration and is verified against the stacked manifest, cross-checked with the
#     multicall manifest it names as the receive half of its chain;
#   * neither: the sealed/legacy path, where the upstream truncation is the pinned
#     behaviour and must stay byte-for-byte unchanged.

#: The environment variable that ACTIVATES the receive compatibility policy in the
#: patched checkout. It is the same variable the multicall-only protocol uses, so the
#: patched receive point keeps one switch to read.
RECEIVE_PROFILE_ENV_VAR = LETTA_ENV_VAR

#: The receive contracts a process can be under.
RECEIVE_NOT_DECLARED = "not_declared"
RECEIVE_MULTICALL_ONLY = "multicall_only"
RECEIVE_STACKED = "stacked"


def read_capacity_manifest(manifest_path_value):
    """Read the composed CAPACITY manifest's chain facts; raise on any defect.

    Only what the receive contract depends on is read: WHICH files the capacity patch
    changes and the two digests of each, so a stack that declares a different change
    set - or a capacity edit that claims the receive files - cannot pass as this chain.
    """
    path = Path(manifest_path_value)
    if not path.is_file():
        raise MulticallPolicyError(f"the composed capacity manifest is absent: {path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MulticallPolicyError(
            f"the composed capacity manifest is unreadable: {exc}") from exc
    if not isinstance(record, dict):
        raise MulticallPolicyError("the composed capacity manifest must be a JSON object")
    if record.get("schema") != STACK_CAPACITY_SCHEMA:
        raise MulticallPolicyError(
            f"unknown composed capacity manifest schema {record.get('schema')!r}; "
            f"expected {STACK_CAPACITY_SCHEMA!r}")
    files = record.get("patched_files")
    if not isinstance(files, dict):
        raise MulticallPolicyError(
            "the composed capacity manifest must declare a patched_files map")
    if set(files) != set(CAPACITY_PATCHED_FILES):
        missing = sorted(set(CAPACITY_PATCHED_FILES) - set(files))
        extra = sorted(set(files) - set(CAPACITY_PATCHED_FILES))
        raise MulticallPolicyError(
            "patched_files must be exactly the capacity patch's file set"
            + (f"; missing={missing}" if missing else "")
            + (f"; unexpected={extra}" if extra else ""))
    for name in CAPACITY_PATCHED_FILES:
        item = files[name]
        # The two DIGESTS are the contract; the reviewed capacity manifest also carries
        # prose next to them (`baseline_is`), so only these two keys are required here.
        if (not isinstance(item, dict)
                or not {"baseline_sha256", "patched_sha256"} <= set(item)):
            raise MulticallPolicyError(
                f"patched_files[{name!r}] must declare baseline_sha256 and patched_sha256")
        _check_stack_digest(f"patched_files[{name!r}].baseline_sha256", item["baseline_sha256"])
        _check_stack_digest(f"patched_files[{name!r}].patched_sha256", item["patched_sha256"])
    return record


def verify_receive_contract(environ=None, *, module=None) -> dict:
    """The receive contract THIS environment declares, or an explicit refusal.

    Never raises: it returns the state a caller must report, including the
    `not_declared` state the sealed/legacy path runs under. Anything a caller may act
    on (`active`) has been verified from bytes, never from the variable's presence.
    A declared-but-unverifiable or MIXED declaration is returned as
    `declared=True, active=False` with the reason, so no caller can read "declared" as
    "activated" and fall back to the upstream truncation.
    """
    env = os.environ if environ is None else environ
    receive_profile = env.get(RECEIVE_PROFILE_ENV_VAR)
    stack_profile = env.get(STACK_ENV_VAR)
    if not receive_profile and not stack_profile:
        return {"declared": False, "active": False, "contract": RECEIVE_NOT_DECLARED,
                "profile_version": None,
                "reason": "no_receive_declaration",
                "note": ("neither the receive profile nor the stacked profile is "
                         "declared, so the pinned upstream truncation is the behaviour")}
    try:
        if stack_profile:
            record = verify_stacked_receive_contract(env, module=module)
        else:
            record = _verify_multicall_only_receive_contract(env, module=module)
    except MulticallPolicyError as exc:
        return {"declared": True, "active": False, "contract": None, "profile_version": None,
                "reason": str(exc), "note": ("a declared receive policy that cannot be "
                                             "established is a refusal, never a fallback "
                                             "to truncation")}
    return record


def verify_receive_gate(environ=None, *, module=None) -> dict:
    """`verify_receive_contract`, but a declared-and-unverifiable state RAISES.

    This is the fail-closed entry the service startup uses: when the environment
    declares a receive policy, the process may only start if that policy verifies.
    """
    state = verify_receive_contract(environ, module=module)
    if state["declared"] and not state["active"]:
        raise MulticallPolicyError(state["reason"])
    return state


def _verify_multicall_only_receive_contract(env, *, module=None) -> dict:
    """The multicall-only receive contract: the reviewed manifest over its own tree."""
    receive_profile = env.get(RECEIVE_PROFILE_ENV_VAR)
    if receive_profile != PROFILE_VERSION:
        raise MulticallPolicyError(
            f"[unknown_receive_profile] unknown receive profile {receive_profile!r}; "
            f"expected {PROFILE_VERSION!r}")
    if not verify_letta_gate(env, module=module):
        raise MulticallPolicyError(
            "[receive_gate_unverified] the declared receive profile did not verify "
            f"against the multicall manifest named by {MANIFEST_ENV_VAR}")
    manifest = Path(env[MANIFEST_ENV_VAR])
    return {"declared": True, "active": True, "contract": RECEIVE_MULTICALL_ONLY,
            "profile_version": receive_profile,
            "manifest": str(manifest),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "module_sha256": env.get(MODULE_SHA_ENV_VAR),
            "reason": "multicall_only_receive_contract_verified",
            "parallel_tool_calls": False,
            "execute_serially": True,
            "preserve_native_ids": True}


def verify_stacked_receive_contract(environ=None, *, module=None) -> dict:
    """The STACKED receive contract: the stack AND the receive policy it composes.

    Loading the stacked checkout is not activation, and the stacked manifest alone does
    not say which receive policy is in force - so this check requires BOTH declarations
    and then proves, from bytes and from the two manifests the stack names, that the
    composed receive half really is the reviewed multicall policy:

    * the stack gate verifies (module digest, stacked manifest, every changed and new
      file in the declared checkout);
    * the receive profile is declared, is this module's own `PROFILE_VERSION`, and the
      stack declares ITS own manifest - a multicall-only manifest beside a stack
      declaration is a mixed declaration and is refused;
    * the stack's named multicall manifest is the reviewed receive manifest (its own
      schema, profile version, pinned baseline and exact file set are re-checked by
      `read_manifest`), and both manifests pin the SAME compatibility module;
    * every file the capacity patch does NOT change carries the reviewed multicall
      digests in the stacked tree, and every file it DOES change declares the reviewed
      multicall bytes as its baseline and the stacked bytes as its result.

    Raises `MulticallPolicyError` with a bracketed reason on any defect.
    """
    env = os.environ if environ is None else environ
    stack_profile = env.get(STACK_ENV_VAR)
    if stack_profile != STACK_PROFILE_VERSION:
        raise MulticallPolicyError(
            f"[unknown_stack_profile] unknown stacked profile {stack_profile!r}; "
            f"expected {STACK_PROFILE_VERSION!r}")
    receive_profile = env.get(RECEIVE_PROFILE_ENV_VAR)
    if not receive_profile:
        raise MulticallPolicyError(
            "[stack_without_receive_policy] the stacked profile is declared but "
            f"{RECEIVE_PROFILE_ENV_VAR} is not: the stacked checkout COMPOSES the "
            "receive compatibility policy and loading it is not activating it, so this "
            "process must not run the pinned truncation under a stack declaration")
    if receive_profile != PROFILE_VERSION:
        raise MulticallPolicyError(
            f"[unknown_receive_profile] unknown receive profile {receive_profile!r}; "
            f"expected {PROFILE_VERSION!r}")
    if env.get(MANIFEST_ENV_VAR):
        raise MulticallPolicyError(
            f"[mixed_receive_declaration] {MANIFEST_ENV_VAR} names the multicall-only "
            "tree, which the stacked manifest pins to different bytes for the file the "
            "capacity patch also changes; a stacked process declares only the stacked "
            "manifest")
    if not verify_stack_gate(env, module=module):
        raise MulticallPolicyError(
            "[stack_gate_unverified] the declared stacked profile did not verify against "
            f"the stacked manifest named by {STACK_MANIFEST_ENV_VAR}")
    manifest_path = Path(env[STACK_MANIFEST_ENV_VAR])
    record = read_stack_manifest(manifest_path)
    reviewed = read_manifest(record["multicall_manifest"])
    capacity = read_capacity_manifest(record["no_compaction_manifest"])
    if reviewed["profile_version"] != receive_profile:
        raise MulticallPolicyError(
            "[receive_profile_not_the_composed_one] the stacked manifest composes "
            f"{reviewed['profile_version']!r}, not the declared {receive_profile!r}")
    if reviewed["compat_module"]["sha256"] != record["compat_module"]["sha256"]:
        raise MulticallPolicyError(
            "[composed_module_differs] the stacked and multicall manifests pin different "
            "compatibility modules")
    receive_owned = sorted(set(reviewed["patched_files"]) - set(capacity["patched_files"]))
    # The files BOTH patches change: their capacity baseline must be the reviewed receive
    # bytes, which is what makes "the capacity patch was applied ON TOP of the receive
    # patch" a checked statement. The capacity patch may also own files of its own
    # (the summariser), which have no receive baseline to compare with.
    review_owned_chain = sorted(set(CAPACITY_PATCHED_FILES) & set(reviewed["patched_files"]))
    if not receive_owned:
        raise MulticallPolicyError(
            "[composed_receive_files_absent] the capacity patch claims every file the "
            "reviewed receive policy owns, so this chain is not the reviewed composition")
    if not review_owned_chain:
        raise MulticallPolicyError(
            "[composed_receive_chain_absent] the capacity patch shares no file with the "
            "reviewed receive policy, so the two patches are not composed on one tree")
    for name in receive_owned:
        expected = reviewed["patched_files"][name]["patched_sha256"]
        if record["patched_files"].get(name) != expected:
            raise MulticallPolicyError(
                f"[composed_receive_file_differs] the stacked tree does not carry the "
                f"reviewed receive bytes of {name}")
    for name in CAPACITY_PATCHED_FILES:
        item = capacity["patched_files"][name]
        if name in review_owned_chain:
            if item["baseline_sha256"] != reviewed["patched_files"][name]["patched_sha256"]:
                raise MulticallPolicyError(
                    f"[capacity_baseline_is_not_the_receive_bytes] {name} does not start "
                    "from the reviewed receive bytes, so the composed chain is not the "
                    "reviewed one")
            if record["patched_files"].get(name) != item["patched_sha256"]:
                raise MulticallPolicyError(
                    f"[capacity_result_is_not_the_stacked_bytes] {name} in the stacked "
                    "tree is not what the composed capacity patch declares")
    if (sorted(set(receive_owned) | set(review_owned_chain))
            != sorted(reviewed["patched_files"])):
        raise MulticallPolicyError(
            "[composed_receive_file_set_changed] the composed chain does not account for "
            "exactly the reviewed receive patch's files")
    return {"declared": True, "active": True, "contract": RECEIVE_STACKED,
            "profile_version": receive_profile,
            "stack_profile_version": stack_profile,
            "manifest": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "composed_receive_manifest": str(record["multicall_manifest"]),
            "composed_capacity_manifest": str(record["no_compaction_manifest"]),
            "module_sha256": record["compat_module"]["sha256"],
            "letta_checkout": record["letta_checkout"],
            "receive_owned_files": {name: record["patched_files"][name]
                                    for name in receive_owned},
            "capacity_chained_files": sorted(capacity["patched_files"]),
            "reason": "stacked_receive_contract_verified",
            "parallel_tool_calls": False,
            "execute_serially": True,
            "preserve_native_ids": True}


def validate_stack_receipt(receipt, manifest, manifest_sha256):
    """Check a STACK load receipt's contents against the stacked manifest.

    Same rule as the multicall receipt: naming a file is never enough. Every fact
    is recomputed from bytes on disk, and the receipt must describe exactly the
    manifest's own checkout and file sets - which is what makes "the service really
    loaded the stacked patch" a checked statement instead of a claim.
    """
    if not isinstance(receipt, dict):
        raise MulticallPolicyError("the stack load receipt must be a JSON object")
    if set(receipt) != set(STACK_RECEIPT_FIELDS):
        missing = sorted(set(STACK_RECEIPT_FIELDS) - set(receipt))
        extra = sorted(set(receipt) - set(STACK_RECEIPT_FIELDS))
        raise MulticallPolicyError(
            "[stack_receipt_fields] the stack load receipt fields differ from the "
            "reviewed set"
            + (f"; missing={missing}" if missing else "")
            + (f"; unexpected={extra}" if extra else ""))
    if receipt["profile_version"] != STACK_PROFILE_VERSION:
        raise MulticallPolicyError("[stack_receipt_profile] the receipt names another profile")
    if not isinstance(receipt["instance_id"], str) or not receipt["instance_id"]:
        raise MulticallPolicyError("[stack_receipt_instance] the receipt has no instance id")
    if receipt["source_verified"] is not True:
        raise MulticallPolicyError("[stack_receipt_source_unverified] the receipt does not "
                                   "declare a verified source")
    if receipt["source_verification"] != STACK_SOURCE_VERIFICATION:
        raise MulticallPolicyError("[stack_receipt_source_method] unknown source "
                                   "verification method")
    if receipt["manifest_sha256"] != manifest_sha256:
        raise MulticallPolicyError("[stack_receipt_manifest_sha] the receipt was written "
                                   "for another manifest")
    module_path = Path(str(receipt["module_path"]))
    if not module_path.is_file():
        raise MulticallPolicyError("[stack_receipt_module_absent] the receipt's module "
                                   "is absent")
    module_digest = hashlib.sha256(module_path.read_bytes()).hexdigest()
    if module_digest != receipt["module_sha256"]:
        raise MulticallPolicyError("[stack_receipt_module_sha] the receipt's module bytes "
                                   "changed")
    if module_digest != manifest["compat_module"]["sha256"]:
        raise MulticallPolicyError("[stack_receipt_module_manifest] the receipt's module "
                                   "is not the manifested one")
    checkout = Path(str(receipt["checkout"])).resolve()
    if checkout != Path(manifest["letta_checkout"]).resolve():
        raise MulticallPolicyError("[stack_receipt_checkout] the receipt names another "
                                   "checkout")
    if not checkout.is_dir():
        raise MulticallPolicyError("[stack_receipt_checkout_absent] the receipt's checkout "
                                   "is absent")
    for key in ("patched_files", "new_files"):
        files = receipt[key]
        if not isinstance(files, dict) or set(files) != set(manifest[key]):
            raise MulticallPolicyError(f"[stack_receipt_file_set] the receipt's {key} set "
                                       "is not the manifested one")
        for name in sorted(manifest[key]):
            entry = files[name]
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                raise MulticallPolicyError("[stack_receipt_entry] a receipt entry must be "
                                           "{path, sha256}")
            if entry["sha256"] != manifest[key][name]:
                raise MulticallPolicyError("[stack_receipt_digest] a receipt digest differs "
                                           "from the manifest")
            live = Path(str(entry["path"]))
            if not live.is_file():
                raise MulticallPolicyError("[stack_receipt_file_absent] a receipt file is "
                                           "absent")
            if checkout not in live.resolve().parents:
                raise MulticallPolicyError("[stack_receipt_file_outside_checkout] a receipt "
                                           "file is outside the declared checkout")
            if hashlib.sha256(live.read_bytes()).hexdigest() != entry["sha256"]:
                raise MulticallPolicyError("[stack_receipt_file_changed] a receipt file "
                                           "changed on disk")
    return receipt
