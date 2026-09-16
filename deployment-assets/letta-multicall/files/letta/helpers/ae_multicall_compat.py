"""AE multi-client-tool-call receive compatibility (opt-in; new file).

This file is added to the pinned Letta checkout by the AE multicall patch. It
changes nothing unless the environment declares the reviewed compatibility
profile AND the reviewed manifest verifies, so an unpatched-vs-patched
comparison is a small set of environment variables and every other Letta
behaviour is byte-for-byte unchanged.

AE-MULTICALL: profile `ae-multicall-receive-compat-1`.

Why: with `parallel_tool_calls=false`, `LettaAgentV3._step` truncates a
provider response containing more than one tool call to its first call, and
`Message` serializers shorten every tool-call id to `TOOL_CALL_ID_MAX_LEN` (29)
characters. A real capture returned four `memory_update` calls whose distinct
ids shared their first 29 characters, so three calls were dropped and the
survivor's id was ambiguous on the wire.

What this profile does: keep every declared client tool call, in the provider's
original order, without shortening any id; the client executes them strictly
serially. It never enables concurrency and never flips `parallel_tool_calls`.

Protection position: the decision is taken while the received call list is still
complete, i.e. BEFORE the upstream truncation. The three outcomes are

  * the profile variable is absent  -> `NOT_DECLARED`: the upstream truncation
    runs unchanged (the sealed protocol, and an unpatched server);
  * the profile is declared and the reviewed gate verifies -> the whole batch is
    kept for the client to execute serially;
  * the profile is declared but the gate fails, or any other profile value
    appears -> `MulticallRefused` is raised, so the step stops with the raw
    provider response preserved and NO tool is executed. Falling back to
    truncation here would silently drop calls, which is the defect this work
    exists to remove.
"""

from __future__ import annotations

import os
import re
import sys

#: The one reviewed profile value.
AE_MULTICALL_PROFILE = "ae-multicall-receive-compat-1"

#: Environment variable that selects the profile.
AE_MULTICALL_ENV_VAR = "AE_LETTA_MULTICALL_PROFILE"

#: Environment variable naming the reviewed manifest the project gate must read.
AE_MULTICALL_MANIFEST_ENV_VAR = "AE_LETTA_MULTICALL_MANIFEST"

#: Environment variable carrying the reviewed compatibility-module digest.
AE_MULTICALL_MODULE_SHA_ENV_VAR = "AE_LETTA_MULTICALL_MODULE_SHA256"

#: Mirrors `letta/utils.py` TOOL_CALL_ID_PATTERN (Anthropic compatibility).
_TOOL_CALL_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

#: Return values from :func:`multicall_decision`.
KEEP_ALL = "keep_all_client_tool_calls"
NOT_DECLARED = "not_declared"

#: The compatibility support module the strict gate lives in.
AE_MULTICALL_MODULE = "ae_multicall"


class MulticallRefused(RuntimeError):
    """The declared receive compatibility could not be established.

    Raised BEFORE the provider's call list is touched, so the caller stops with
    every received call intact and no side effect.
    """


def declared_profile(environ=None):
    """The raw profile value in the environment, or None when unset."""
    env = os.environ if environ is None else environ
    return env.get(AE_MULTICALL_ENV_VAR)


def _import_version_support(module=None):
    """Import the project's compatibility module for its policy version.

    A missing module is itself a failure: the profile cannot be honoured without
    it, and importing it is what makes the runtime use the same policy object the
    manifest and the audit use.
    """
    if module is None:
        module = sys.modules.get(AE_MULTICALL_MODULE)
    if module is None:
        try:
            module = __import__(AE_MULTICALL_MODULE)
        except Exception as exc:
            raise MulticallRefused(
                "the declared multicall profile cannot be honoured: the "
                f"{AE_MULTICALL_MODULE} support module is unavailable: "
                f"{type(exc).__name__}: {exc}") from exc
    return module


def multicall_decision(tool_calls, client_tools, module=None):
    """Decide what to do with a multi-call provider response, before truncation.

    Returns :data:`NOT_DECLARED` (run the upstream truncation) or
    :data:`KEEP_ALL` (keep the whole batch). Any declared-but-unverifiable or
    unreviewed configuration raises :class:`MulticallRefused` instead, so the
    caller can stop without dropping a call.
    """
    profile = declared_profile()
    if profile is None:
        return NOT_DECLARED
    support = _import_version_support(module)
    expected = getattr(support, "PROFILE_VERSION", None)
    if profile != expected or profile != AE_MULTICALL_PROFILE:
        raise MulticallRefused(
            f"unknown multicall profile {profile!r}; expected {AE_MULTICALL_PROFILE!r}")
    # The runtime uses the project's own strict gate: it re-reads the manifest,
    # recomputes the compatibility-module digest and re-hashes the patched Letta
    # files. A missing, damaged, empty or tampered manifest therefore fails here
    # rather than silently enabling the profile.
    try:
        verified = support.verify_letta_gate()
    except Exception as exc:
        raise MulticallRefused(
            f"the multicall gate raised {type(exc).__name__}: {exc}") from exc
    if not verified:
        raise MulticallRefused(
            "the declared multicall profile did not verify against its reviewed "
            "manifest; refusing to process the batch")
    calls = list(tool_calls or [])
    if len(calls) <= 1:
        return KEEP_ALL
    declared = {tool.name for tool in (client_tools or [])}
    if not declared:
        raise MulticallRefused(
            "the declared multicall profile received tool calls but no client "
            "tool is declared, so none of them can be executed")
    names = [getattr(getattr(call, "function", None), "name", None) for call in calls]
    unsupported = [name for name in names if name not in declared]
    if unsupported:
        raise MulticallRefused(
            "the declared multicall profile received calls that are not declared "
            f"client tools: {sorted(set(unsupported))}; refusing the whole batch")
    return KEEP_ALL


def ae_multicall_enabled(module=None):
    """True only when the profile is declared and the strict gate verifies."""
    try:
        return multicall_decision([], [], module) == KEEP_ALL
    except MulticallRefused:
        return False


def keep_all_client_tool_calls(tool_calls, client_tools, module=None):
    """Deprecated boolean shim kept for older callers; never truncates silently."""
    return multicall_decision(tool_calls, client_tools, module) == KEEP_ALL


def preserve_tool_call_id(tool_call_id, max_tool_id_length):
    """The outbound id: unchanged under the profile, upstream slice otherwise."""
    profile = declared_profile()
    if profile is not None:
        # The profile is declared, so ids must stay whole. Whether the gate
        # verifies is decided at the receive point (`multicall_decision`); by the
        # time serialization runs, a declared profile means "preserve".
        return tool_call_id
    if max_tool_id_length and isinstance(tool_call_id, str):
        return tool_call_id[:max_tool_id_length]
    return tool_call_id


def sanitize_tool_call_id_compat(tool_call_id):
    """`sanitize_tool_call_id` without the 29-character slice under the profile.

    The provider-required character set is still enforced; only the length
    shortening is skipped, so the full id survives to the next request.
    """
    if declared_profile() is not None and isinstance(tool_call_id, str):
        if _TOOL_CALL_ID_PATTERN.match(tool_call_id):
            return tool_call_id
        return re.sub(r"[^a-zA-Z0-9_-]", "_", tool_call_id)
    from letta.utils import sanitize_tool_call_id

    return sanitize_tool_call_id(tool_call_id)
