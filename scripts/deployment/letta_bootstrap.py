#!/usr/bin/env python3
"""Opt-in NLTK 3.9.1 startup I/O bound; executes the unchanged Letta CLI.

Only nltk.downloader.urlopen is temporarily wrapped during the first exact
startup download. Ten seconds bounds each network I/O, NOT total startup time.
Existing English punkt_tab must load first. No success is synthesized.

This wrapper also carries the AE multi-client-tool-call OPT-IN. When the
environment declares the reviewed profile, the bootstrap (the process that
actually loads Letta and the support module):

  1. imports the support module from the declared path,
  2. runs the strict `verify_letta_gate()` against the manifest, validating the
     module BYTES and the checkout it is about to load,
  3. writes a load receipt naming this process instance and the exact files it
     verified, and
  4. only then executes the unchanged Letta CLI.

The STACKED profile is verified the same way by `install_patch_stack_gate()`, and
it additionally REQUIRES the receive compatibility policy that stack composes to be
declared and to verify here: the stacked checkout contains the receive patch, but
the patched receive point acts on its own declaration, so a service started from
the stacked tree without it would run the pinned truncation and silently drop
multi-call responses. That state refuses to start.

A missing or failing verification raises BEFORE Letta is imported, so no model
request or tool side effect can happen. Nothing here is inferred from a parent
process's environment: the receipt is written by this process.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import runpy
import sys
import urllib.request


NLTK_VERSION = "3.9.1"
IO_TIMEOUT_SECONDS = 10.0


def report(event, **fields):
    print(json.dumps({"component": "ae_bounded_nltk_startup", "event": event, **fields},
                     ensure_ascii=False, allow_nan=False), flush=True)


def install_startup_bound(nltk, downloader, emit=report):
    """Validate real local data before installing one narrowly scoped wrapper."""
    if nltk.__version__ != NLTK_VERSION:
        raise RuntimeError("bounded startup supports only the inspected NLTK 3.9.1")
    original_urlopen = downloader.urlopen
    original_download = nltk.download
    if (original_urlopen is not urllib.request.urlopen
            or downloader.Downloader._update_index.__globals__.get("urlopen") is not original_urlopen):
        raise RuntimeError("NLTK downloader urlopen binding differs from the inspected implementation")
    # This really loads all four English tables; existence alone is insufficient.
    try:
        nltk.tokenize.PunktTokenizer("english")
    except BaseException as exc:
        emit("existing_punkt_load_failed", language="english", error_type=type(exc).__name__)
        raise
    emit("existing_punkt_loaded", language="english", nltk_version=nltk.__version__)

    def bounded_urlopen(*args, **kwargs):
        positional = list(args)
        supplied = positional[2] if len(positional) >= 3 else kwargs.get("timeout")
        timeout = (min(float(supplied), IO_TIMEOUT_SECONDS)
                   if isinstance(supplied, (int, float)) and not isinstance(supplied, bool)
                   and math.isfinite(supplied) and supplied > 0 else IO_TIMEOUT_SECONDS)
        if len(positional) >= 3:
            positional[2] = timeout
        else:
            kwargs["timeout"] = timeout
        return original_urlopen(*positional, **kwargs)

    def restore():
        downloader.urlopen = original_urlopen
        nltk.download = original_download

    def startup_download(*args, **kwargs):
        try:
            if args != ("punkt_tab",) or kwargs != {"quiet": True}:
                raise RuntimeError("unexpected NLTK startup download; no fallback or network retry")
            downloader.urlopen = bounded_urlopen
            emit("download_started", package="punkt_tab", per_io_timeout_seconds=IO_TIMEOUT_SECONDS,
                 total_timeout_seconds=None, automatic_retry=False)
            result = original_download(*args, **kwargs)
            # NLTK normally returns bool. Preserve unexpected values rather than
            # converting a false/None result into a success claim.
            emit("download_returned", return_type=type(result).__name__,
                 returned=result if result is None or isinstance(result, (bool, str, int, float)) else repr(result))
            return result
        except BaseException as exc:
            emit("download_raised", error_type=type(exc).__name__)
            raise
        finally:
            restore()
            emit("download_hooks_restored")

    nltk.download = startup_download
    return restore


def _required_environment(source=None):
    """Read the declared protocol from the process environment (or a given mapping)."""
    environ = os.environ if source is None else source
    profile = environ.get("AE_LETTA_MULTICALL_PROFILE")
    if not profile:
        return None
    return {
        "profile": profile,
        "manifest": environ.get("AE_LETTA_MULTICALL_MANIFEST"),
        "module_sha256": environ.get("AE_LETTA_MULTICALL_MODULE_SHA256"),
        "support": environ.get("AE_LETTA_MULTICALL_SUPPORT"),
        "receipt": environ.get("AE_LETTA_MULTICALL_LOAD_RECEIPT"),
    }


def resolve_letta_sources(manifest, *, find_spec=None, search_path=None):
    """Resolve the Letta files THIS process would import, and check them.

    The receipt may not copy the manifest's paths or digests: a correct scratch
    tree must not let an original or different checkout pass. Each reviewed file is
    resolved through the real import machinery (`importlib.util.find_spec`) under
    the search path the CLI will use, and it must both live inside the manifested
    checkout AND hash to the manifested digest. The returned mapping is the set of
    paths and bytes this process actually resolved.
    """
    if find_spec is None:
        import importlib.util
        find_spec = importlib.util.find_spec
    if search_path is not None:
        import contextlib
        with contextlib.ExitStack() as stack:
            # Resolve under the exact path list the CLI will run with, without
            # permanently mutating this process.
            import sys
            original = list(sys.path)
            sys.path[:] = list(search_path)
            stack.callback(lambda: sys.path.__setitem__(slice(None), original))
            return resolve_letta_sources(manifest, find_spec=find_spec)
    checkout = Path(manifest["letta_checkout"]).resolve()
    resolved = {}
    lookup = {
        "letta/agents/letta_agent_v3.py":
            ("letta.agents.letta_agent_v3", "letta_agent_v3.py"),
        "letta/schemas/message.py":
            ("letta.schemas.message", "message.py"),
        "letta/helpers/ae_multicall_compat.py":
            ("letta.helpers.ae_multicall_compat", "ae_multicall_compat.py"),
    }
    for key, (module_name, filename) in lookup.items():
        spec = find_spec(module_name)
        origin = getattr(spec, "origin", None) if spec is not None else None
        if not origin or origin in ("built-in", "frozen") or not Path(origin).is_file():
            raise RuntimeError(
                f"the service cannot resolve {module_name} from the declared checkout")
        path = Path(origin).resolve()
        if path.name != filename:
            raise RuntimeError(f"{module_name} resolved to an unexpected file: {path}")
        if checkout not in path.parents:
            raise RuntimeError(
                f"{module_name} resolves outside the declared checkout: {path} not under {checkout}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = manifest["patched_files"][key]["patched_sha256"]
        if digest != expected:
            raise RuntimeError(
                f"{module_name} bytes differ from the reviewed patch: {path}")
        resolved[key] = {"path": str(path), "sha256": digest}
    return resolved


def _required_stack_environment(source=None):
    """Read the declared STACKED profile from the process environment."""
    environ = os.environ if source is None else source
    profile = environ.get("AE_LETTA_PATCH_STACK_PROFILE")
    if not profile:
        return None
    return {
        "profile": profile,
        "manifest": environ.get("AE_LETTA_PATCH_STACK_MANIFEST"),
        "module_sha256": environ.get("AE_LETTA_MULTICALL_MODULE_SHA256"),
        "support": environ.get("AE_LETTA_MULTICALL_SUPPORT"),
        "receipt": environ.get("AE_LETTA_PATCH_STACK_RECEIPT"),
    }


def resolve_stack_sources(manifest, *, find_spec=None, search_path=None):
    """Resolve every changed and new STACK file through the real import machinery.

    The stack receipt may not copy the manifest's paths or digests: each file is
    resolved with `importlib.util.find_spec` under the search path the CLI will use,
    must live inside the manifested stacked checkout, and must hash to the
    manifested digest. Files the patch ADDS are resolved exactly like the ones it
    changes, because a checkout that is missing a helper is not a loaded patch.
    """
    if find_spec is None:
        import importlib.util
        find_spec = importlib.util.find_spec
    if search_path is not None:
        import contextlib
        import sys
        with contextlib.ExitStack() as stack:
            original = list(sys.path)
            sys.path[:] = list(search_path)
            stack.callback(lambda: sys.path.__setitem__(slice(None), original))
            return resolve_stack_sources(manifest, find_spec=find_spec)
    checkout = Path(manifest["letta_checkout"]).resolve()
    resolved = {}
    for key, source in sorted(manifest["sources"].items()):
        module_name, filename = source["module"], source["filename"]
        spec = find_spec(module_name)
        origin = getattr(spec, "origin", None) if spec is not None else None
        if not origin or origin in ("built-in", "frozen") or not Path(origin).is_file():
            raise RuntimeError(
                f"the service cannot resolve {module_name} from the declared stacked checkout")
        path = Path(origin).resolve()
        if path.name != filename:
            raise RuntimeError(f"{module_name} resolved to an unexpected file: {path}")
        if checkout not in path.parents:
            raise RuntimeError(
                f"{module_name} resolves outside the declared stacked checkout: "
                f"{path} not under {checkout}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key_kind = "patched_files" if key in manifest["patched_files"] else "new_files"
        expected = manifest[key_kind][key]
        if digest != expected:
            raise RuntimeError(
                f"{module_name} bytes differ from the reviewed stacked patch: {path}")
        resolved[key] = {"path": str(path), "sha256": digest}
    expected_keys = set(manifest["patched_files"]) | set(manifest["new_files"])
    if set(resolved) != expected_keys:
        raise RuntimeError("the resolved stacked file set is not the manifested one")
    return resolved


def install_patch_stack_gate(environ=None, *, find_spec=None, search_path=None):
    """Verify the declared STACKED profile and write this process's stack receipt.

    Returns None when no stack profile is declared (the multicall-only and sealed
    paths, which must keep working exactly as before). Raises RuntimeError before
    Letta is touched when the declaration cannot be verified.

    The stacked checkout COMPOSES the reviewed receive compatibility policy, so the
    stack declaration also requires that policy to be declared and verifiable here.
    Loading the stacked bytes is not activation: the patched receive point decides
    from its own declaration, and a service that declared the stack without it ran
    the pinned upstream truncation and silently dropped calls (the live r4 capture).
    A stack that cannot establish its composed receive policy therefore REFUSES TO
    START - it never falls back to truncation.
    """
    source = os.environ if environ is None else environ
    env = _required_stack_environment(source)
    if env is None:
        return None
    for key in ("manifest", "module_sha256", "support"):
        if not env.get(key):
            raise RuntimeError(f"declared AE patch stack profile is missing {key}")
    support = Path(env["support"])
    if not support.is_dir() or not (support / "ae_multicall.py").is_file():
        raise RuntimeError(f"declared AE multicall support module is absent in: {support}")
    sys.path.insert(0, str(support))
    import ae_multicall

    module_path = Path(ae_multicall.__file__).resolve()
    live_sha = hashlib.sha256(module_path.read_bytes()).hexdigest()
    if live_sha != env["module_sha256"]:
        raise RuntimeError("the loaded AE multicall module does not match its declared digest")
    manifest = ae_multicall.read_stack_manifest(env["manifest"])
    if not ae_multicall.verify_stack_gate(
            {ae_multicall.STACK_ENV_VAR: env["profile"],
             ae_multicall.STACK_MANIFEST_ENV_VAR: env["manifest"],
             ae_multicall.MODULE_SHA_ENV_VAR: env["module_sha256"]},
            module=ae_multicall):
        raise RuntimeError("the declared AE patch stack did not verify against its manifest")
    # The composed RECEIVE policy, verified on the environment this process will run
    # with: the declaration must be present, must be this module's own profile, and
    # must verify against the stacked contract (which cross-checks the multicall
    # manifest the stack names as its receive half). Without this the receive point
    # would take its `not_declared` branch and truncate multi-call responses.
    try:
        activation = ae_multicall.verify_receive_gate(source, module=ae_multicall)
    except ae_multicall.MulticallPolicyError as exc:
        raise RuntimeError(
            "the declared AE patch stack composes a receive compatibility policy that "
            f"this process cannot establish: {exc}") from exc
    if not activation["active"]:
        raise RuntimeError(
            "the declared AE patch stack has no active receive compatibility policy; "
            "refusing to start rather than truncating multi-call responses")
    sources = resolve_stack_sources(manifest, find_spec=find_spec, search_path=search_path)
    bound_files = {name: {"path": entry["path"], "sha256": entry["sha256"]}
                   for name, entry in sources.items()}
    agent = Path(sources["letta/agents/letta_agent_v3.py"]["path"])
    receipt = {
        "profile_version": env["profile"],
        "instance_id": f"{os.getpid()}-{int.from_bytes(os.urandom(8), 'big'):016x}",
        "module_path": str(module_path),
        "module_sha256": live_sha,
        "checkout": str(agent.parents[2]),
        "manifest_sha256": hashlib.sha256(Path(env["manifest"]).read_bytes()).hexdigest(),
        "patched_files": {name: bound_files[name] for name in sorted(manifest["patched_files"])},
        "new_files": {name: bound_files[name] for name in sorted(manifest["new_files"])},
        "source_verified": True,
        "source_verification": ae_multicall.STACK_SOURCE_VERIFICATION,
    }
    if env.get("receipt"):
        target = Path(env["receipt"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            os.chmod(target, 0o600)
            json.dump(receipt, stream, ensure_ascii=False, allow_nan=False)
        report("patch_stack_gate_verified", profile=env["profile"],
               module_path=str(module_path), checkout=receipt["checkout"],
               receipt=str(target))
    # The activation is reported SEPARATELY from the receipt: the receipt's field set is
    # exact-checked by the audit, so adding to it would change a contract earlier rounds
    # already sealed. This event is the service process's own statement of which receive
    # policy it verified before importing Letta.
    report("patch_stack_receive_policy_activated",
           contract=activation["contract"],
           receive_profile=activation["profile_version"],
           declared_in=ae_multicall.RECEIVE_PROFILE_ENV_VAR,
           stacked_manifest=activation["manifest"],
           composed_receive_manifest=activation.get("composed_receive_manifest"),
           module_sha256=activation["module_sha256"],
           receive_owned_files=sorted(activation.get("receive_owned_files") or {}),
           parallel_tool_calls=False, execute_serially=True,
           preserve_native_ids=True)
    return receipt


def install_multicall_gate(environ=None, *, find_spec=None, search_path=None):
    """Verify the declared profile and write this process's load receipt.

    Returns None when no profile is declared (the sealed/legacy path, which the
    caller must treat as "not this experiment's mode"). Raises RuntimeError before
    Letta is touched when the declaration cannot be verified.

    The receipt is bound to the Letta files THIS process resolves and reads, not to
    the manifest: `checkout` is the resolved agent file's directory, and
    `patched_files[name]` carries the resolved path and digest. A run whose import
    path leads to an original or different checkout is refused here.
    """
    env = _required_environment(environ)
    if env is None:
        return None
    # A stacked declaration is a DIFFERENT tree: its agent file carries the capacity patch
    # too, so the multicall-only manifest's digests cannot describe it. The two contracts
    # are alternatives, and the multicall-only gate refuses to judge a stacked process.
    if _required_stack_environment(environ) is not None:
        raise RuntimeError(
            "a stacked profile is also declared: the stacked gate - including the receive "
            "policy that stack composes - is the identity gate for that tree, so the "
            "multicall-only gate must not be asked to judge it")
    for key in ("manifest", "module_sha256", "support"):
        if not env.get(key):
            raise RuntimeError(f"declared AE multicall profile is missing {key}")
    # `support` is the DIRECTORY holding the support module, not the module file:
    # the launcher passes a directory so the child can import it without the
    # parent widening the child's PYTHONPATH.
    support = Path(env["support"])
    if not support.is_dir() or not (support / "ae_multicall.py").is_file():
        raise RuntimeError(f"declared AE multicall support module is absent in: {support}")
    sys.path.insert(0, str(support))
    import ae_multicall

    module_path = Path(ae_multicall.__file__).resolve()
    live_sha = hashlib.sha256(module_path.read_bytes()).hexdigest()
    if live_sha != env["module_sha256"]:
        raise RuntimeError("the loaded AE multicall module does not match its declared digest")
    manifest = ae_multicall.read_manifest(env["manifest"])
    if not ae_multicall.verify_letta_gate({ae_multicall.LETTA_ENV_VAR: env["profile"],
                                           ae_multicall.MANIFEST_ENV_VAR: env["manifest"],
                                           ae_multicall.MODULE_SHA_ENV_VAR: env["module_sha256"]},
                                          module=ae_multicall):
        raise RuntimeError("the declared AE multicall profile did not verify against its manifest")
    # The manifest proves the SCRATCH tree; this proves what THIS process resolves.
    sources = resolve_letta_sources(manifest, find_spec=find_spec, search_path=search_path)
    # Built explicitly so the receipt carries the RESOLVED path and digest of each
    # file this process will import, not the manifest's copy.
    bound_files = {}
    for name, entry in sources.items():
        bound_files[name] = {"path": entry["path"], "sha256": entry["sha256"]}
    agent = Path(sources["letta/agents/letta_agent_v3.py"]["path"])
    receipt = {
        "profile_version": env["profile"],
        "instance_id": f"{os.getpid()}-{int.from_bytes(os.urandom(8), 'big'):016x}",
        "module_path": str(module_path),
        "module_sha256": live_sha,
        # The directory of the Letta agent this process actually resolved.
        "checkout": str(agent.parents[2]),
        "manifest_sha256": hashlib.sha256(Path(env["manifest"]).read_bytes()).hexdigest(),
        "patched_files": bound_files,
        # Explicitly marked: this is a pre-launch resolution check of the real
        # import machinery, not proof that a full agent run happened.
        "source_verified": True,
        "source_verification": "importlib_find_spec_before_letta_import",
    }
    if env.get("receipt"):
        target = Path(env["receipt"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            os.chmod(target, 0o600)
            json.dump(receipt, stream, ensure_ascii=False, allow_nan=False)
        report("multicall_gate_verified", profile=env["profile"],
               module_path=str(module_path), checkout=receipt["checkout"],
               receipt=str(target))
    return receipt


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    cli = Path(__file__).resolve().parents[2] / ".venv-letta/bin/letta"
    if (len(arguments) != 6 or arguments[0] != str(cli)
            or arguments[1:5] != ["server", "--host", "127.0.0.1", "--port"]
            or not arguments[5].isdigit() or not 1024 <= int(arguments[5]) <= 65535):
        raise RuntimeError("expected the project venv Letta CLI and exact loopback server argv")
    if not cli.is_file() or Path(sys.prefix).resolve() != cli.parent.parent.resolve():
        raise RuntimeError("bootstrap must run inside the dedicated project Letta venv")
    # The opt-in gates run BEFORE Letta is imported, so a missing or failing
    # declaration stops with no model request and no tool side effect.
    #
    # The two declarations are alternatives, not a sequence. A service asked for the
    # STACKED profile runs a checkout that this round's patch changes, which the
    # multicall-only manifest pins to different bytes - so running the multicall gate
    # first would refuse the very tree the stack declaration describes. When the stack
    # profile IS declared, the stack gate is the identity gate for ALL of it: its
    # manifest carries the multicall-owned files with the reviewed digests
    # (`STACK_PATCHED_FILES`) and `read_stack_manifest` refuses a stack that renames or
    # omits them, so no multicall evidence is dropped - it is verified under the stack
    # contract instead. With no stack declaration, the multicall gate is exactly what
    # it always was.
    if _required_stack_environment() is not None:
        install_patch_stack_gate()
    else:
        install_multicall_gate()

    import nltk
    import nltk.downloader

    restore = install_startup_bound(nltk, nltk.downloader)
    original_argv, original_path = list(sys.argv), list(sys.path)
    try:
        sys.argv[:] = arguments
        sys.path[0] = str(cli.parent)
        report("original_cli_entered", argv=arguments, modified_upstream_source=False)
        # Same entrypoint and arguments as direct venv/bin/letta invocation.
        return runpy.run_path(str(cli), run_name="__main__")
    finally:
        restore()
        sys.argv[:] = original_argv
        sys.path[:] = original_path


if __name__ == "__main__":
    main()
