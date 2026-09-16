#!/usr/bin/env python3
"""AE-01 continuous original-data t4->t12 cloud R/E pair CLI.

plan (default) and preflight never contact a service; only an explicit `run`
connects to the loopback model and Letta services. Output directories are
exclusive and never resumed or overwritten.

The pair creates two persistent Letta agents, gives each the SAME verified t3
cutoff, then executes R's whole t4->t12 sequence FIRST and E's whole t4->t12
sequence SECOND. Only original consecutive subtasks are ever sent.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
#: The one accepted spelling of the exploratory opt-in. It is re-stated here so the CLI
#: cannot drift from the provider module's own constant.
EXPLORATORY_OPTION = "--exploratory-capacity-option"
EXPLORATORY_MARKER = "_exploratory_capacity_option"
sys.path.insert(0, str(ROOT))
from ae_cloud_re_multiturn import (  # noqa: E402
    ARMS, DEEPSEEK_SCHEMAS, END_TURN, SCHEMA_VERSION_CAPACITY, SCHEMA_VERSION_SIM_EVAL,
    START_TURN, TASK_NUMBERS, build_plan,
    EXPLORATORY_MARKER, deterministic_evaluation_mode, execute_re_multiturn,
    multiturn_code_files, preflight, run_protocols_of, validate_config,
)


def capacity_decision(config, stage):
    """The capacity state a stage is allowed to start with.

    PLAN and PREFLIGHT may run with an unverified declared window: they contact no
    service, and they must be able to report the open items. A real RUN may not:
    when the candidate declares a window above the previously used 65536 without an
    endpoint measurement, this raises instead of pretending the window is proven.
    """
    capacity = config.get("capacity")
    if capacity is None:
        return {"capacity_declared": False, "run_allowed": True}
    if config.get("schema_version") in DEEPSEEK_SCHEMAS:
        # The byte-gate protocol makes NO token-capacity claim, so there is no endpoint
        # measurement to demand and none may be invented. The run is allowed under the
        # DECLARED exploratory option, and the open item says exactly what is unproven -
        # a RUN is never reported as a capacity pass. The repair schema 0.5 carries the
        # SAME provider contract, so it takes this same branch and claims nothing more.
        if config.get("exploratory_capacity_protocol") != (
                "exploratory_no_token_capacity_guarantee"):
            raise RuntimeError("the DeepSeek candidate must declare its exploratory protocol")
        decision = {"capacity_declared": True, "run_allowed": True,
                "protocol": "exploratory_no_token_capacity_guarantee",
                "context_window": capacity["context_window"],
                "window_source": capacity["window_source"],
                "verification": capacity["verification"],
                "endpoint_measured_tokens": None,
                "open_item": ("no token capacity is guaranteed: the endpoint's token window "
                              "is unmeasured and the operational bound is the declared "
                              "request-byte budget"),
                "count_basis": list(capacity["count_basis"]),
                "no_compaction": capacity["no_compaction"]}
        if config.get("schema_version") == SCHEMA_VERSION_SIM_EVAL:
            # What the repair adds is RECORDED in the capacity decision, never claimed
            # as a capacity property: the window and its verification state are the
            # same unmeasured values the 0.4 protocol declares.
            decision["repair_protocols"] = {
                "simulator_protocol": config["simulator_protocol"],
                "evaluation_protocol": config["evaluation_protocol"]}
        return decision
    service = capacity["service_limit"]
    measured = service.get("endpoint_measured_tokens")
    tokenizer = capacity["tokenizer"]
    exploration = tokenizer["asset_target"] != tokenizer["target"]
    tokenizer_open_item = None
    if exploration:
        tokenizer_open_item = (
            f"the counting asset is {tokenizer['asset_target']!r}, not the run's target "
            f"{tokenizer['target']!r}: counts are an explicitly labelled EXPLORATION")
    if stage == "run":
        if capacity["verification"] != "verified" or measured is None:
            raise RuntimeError(
                "the declared context window is not verified for this endpoint: "
                f"window={capacity['context_window']} source={capacity['window_source']} "
                f"verification={capacity['verification']} "
                f"endpoint_measured={measured}; a real RUN refuses to start. "
                "Either measure the endpoint capacity or run PLAN/PREFLIGHT, which "
                "report this as an open item instead of a pass")
        if measured < capacity["context_window"]:
            raise RuntimeError(
                f"the endpoint measurement ({measured}) is below the declared window "
                f"({capacity['context_window']})")
        if exploration:
            # A formal RUN may not count with another model's assets: the window is
            # expressed in the TARGET model's tokens, so a family asset is a
            # diagnostic instrument, never a production authorisation.
            raise RuntimeError(
                "the declared capacity policy counts with an EXPLORATION asset: "
                f"{tokenizer['asset_target']!r} is not {tokenizer['target']!r}. A real "
                "RUN refuses to start; PLAN/PREFLIGHT report this as an open item")
    open_items = []
    if measured is None:
        open_items.append("endpoint capacity unverified: no measurement exists for this endpoint")
    if tokenizer_open_item:
        open_items.append(tokenizer_open_item)
    return {"capacity_declared": True,
            "run_allowed": measured is not None and not exploration,
            "context_window": capacity["context_window"],
            "window_source": capacity["window_source"],
            "verification": capacity["verification"],
            "endpoint_measured_tokens": measured,
            "tokenizer_asset": {"target": tokenizer["target"],
                                "asset_target": tokenizer["asset_target"],
                                "exploration_only": tokenizer["exploration_only"]},
            "open_item": "; ".join(open_items) or None,
            "no_compaction": capacity["no_compaction"],
            "count_basis": list(capacity["count_basis"])}
from ae_inputs import canonical_sha256, prepare_sample  # noqa: E402
import ae_multicall as ai  # noqa: E402
from ae_multicall import (LAUNCH_RECEIPT_ENV_VAR, MANIFEST_ENV_VAR,  # noqa: E402
                          STACK_MANIFEST_ENV_VAR, STACK_RECEIPT_ENV_VAR)

#: The reviewed pinned SCORING-module baseline. The multi-turn audit compares the
#: scoring modules it imports with this declaration (never with their own freshly
#: computed digests); the declaration is itself derived from the archived pinned
#: source, whose digest it carries.
SCORER_BASELINE_PATH = ROOT / "deployment-assets/vita-scorer-baseline.json"
SCORER_BASELINE_ENV_VAR = "AE_VITA_SCORER_BASELINE"


def scorer_baseline():
    """The declared pinned scoring-module baseline, or an explicit absence.

    A record is returned either way: an absent declaration must be visible in the
    plan rather than silently omitted, because the audit refuses to compare a
    module with its own digest.
    """
    declared = os.environ.get(SCORER_BASELINE_ENV_VAR)
    path = Path(declared).resolve() if declared else SCORER_BASELINE_PATH
    if not path.is_file():
        return {"path": str(path), "sha256": None, "declared": False,
                "note": "the pinned scoring-module baseline is absent; the audit "
                        "refuses to derive the expected digests from the files it checks"}
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                "declared": False, "note": "the declared baseline is not JSON"}
    archive = document.get("archive") or {}
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "declared": True, "schema": document.get("schema"),
            "modules": sorted(document.get("modules") or {}),
            "archive": {"path": archive.get("path"), "sha256": archive.get("sha256"),
                        "bytes": archive.get("bytes"),
                        "pinned_revision": archive.get("pinned_revision")}}


def ensure_output_dir(path):
    """Create a fresh run directory; never resume or overwrite existing content."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"output already exists; no resume or overwrite: {path}")
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def provenance(config, config_path, multicall_manifest_path=None,
               multicall_load_receipt=None, *, require_receipt=False, vita_source=None,
               patch_stack_manifest=None, patch_stack_load_receipt=None):
    """Production provenance, including the manifest and the service load receipt.

    This function never synthesizes receipt contents: it records `{path, sha256}`
    of a receipt that already exists, or it records nothing and marks the loading
    state unverified. `require_receipt` is set for a real RUN, so a run that
    claims the compatibility protocol cannot start without a real service
    receipt. PLAN and PREFLIGHT stay runnable offline.

    Two service identities are supported and they are alternatives, never a pair:

    * a 0.2 multicall-only service, evidenced by the reviewed multicall manifest and
      its `multicall-load.json` receipt (`validate_launch_receipt`), and
    * a 0.3 capacity/no-compaction service, whose service process runs the STACKED
      patch and writes `patch-stack-load.json` (the `ae-no-compaction-stack-1`
      profile). That receipt has its own field set - `new_files` among them - so it is
      validated by `validate_stack_receipt` and is REFUSED by the multicall validator.
      Declaring both is a refusal instead of a silent preference, and a RUN of a
      capacity config cannot start without its stack manifest AND receipt.
    """
    record = {
        "config_file_sha256": hashlib.sha256(Path(config_path).read_bytes()).hexdigest(),
        # The DECLARATION's digest, without the private opt-in marker: the marker is a
        # hand-over detail between this CLI and the driver, so a digest that included it
        # could never be compared with the `--config` bytes or the plan's own record.
        "config_canonical_sha256": canonical_sha256(
            {key: value for key, value in config.items() if key != EXPLORATORY_MARKER}),
        "code_sha256": multiturn_code_files(ROOT),
        "python": sys.version, "python_executable": sys.executable,
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "project_git_commit": None, "live_letta_commit_verified": False,
    }
    if vita_source is not None:
        # The audit re-derives the pinned sliding windows from the SAME fixed
        # source the native wrapper used, so the run record has to name it - and
        # it has to name the reviewed baseline those scoring modules are compared
        # with, because a module can never be its own expected digest.
        source = Path(vita_source).resolve()
        record["vita_source"] = {"path": str(source),
                                 "registry_sha256": hashlib.sha256(
                                     (source / "src/vita/registry.py").read_bytes()).hexdigest()
                                 if (source / "src/vita/registry.py").is_file() else None}
        record["vita_scorer_baseline"] = scorer_baseline()
    stack_manifest_declared = patch_stack_manifest or os.environ.get(STACK_MANIFEST_ENV_VAR)
    stack_receipt_declared = (patch_stack_load_receipt
                              or os.environ.get(STACK_RECEIPT_ENV_VAR))
    # Two SERVICE identities are alternatives: the multicall-only service writes
    # `multicall-load.json`, the stacked capacity service writes
    # `patch-stack-load.json`. Declaring both receipts is a contradiction, not a
    # preference to resolve silently. The multicall MANIFEST alone is not a service
    # identity - it is the offline patch proof a 0.3 run still records - so it may
    # accompany the stack evidence.
    multicall_receipt_declared = (multicall_load_receipt
                                  or os.environ.get(LAUNCH_RECEIPT_ENV_VAR))
    if (stack_manifest_declared or stack_receipt_declared) and multicall_receipt_declared:
        raise RuntimeError(
            "declare either the 0.2 multicall service receipt or the 0.3 patch-stack "
            "service evidence, not both: they identify different service trees")
    if config.get("capacity") is not None:
        return _capacity_provenance(
            record, config, require_receipt=require_receipt,
            manifest_declared=stack_manifest_declared,
            receipt_declared=stack_receipt_declared,
            multicall_manifest_path=multicall_manifest_path,
            multicall_load_receipt=multicall_receipt_declared)
    if config.get("multicall_profile") is None:
        return record
    declared = multicall_manifest_path or os.environ.get(MANIFEST_ENV_VAR)
    if declared is None:
        # Offline bookkeeping only. An executed RUN is refused, and the audit
        # refuses to read a completed multiturm run whose manifest is missing.
        if require_receipt:
            raise RuntimeError(
                "an executed 0.2 run must record the reviewed compatibility manifest; pass "
                "--multicall-manifest or set " + MANIFEST_ENV_VAR)
        record["multicall_manifest"] = None
        record["multicall_loading_verified"] = False
        record["multicall_loading_note"] = (
            "offline PLAN/PREFLIGHT: no reviewed compatibility manifest was declared, so the "
            "0.2 receive compatibility is NOT verified; an executed RUN would be refused")
        return record
    path = Path(declared)
    if not path.is_file():
        raise RuntimeError(f"the reviewed compatibility manifest is absent: {path}")
    manifest_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    record["multicall_manifest"] = {"path": str(path), "sha256": manifest_sha}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    claims_live = (manifest.get("verification") or {}).get("applied_to_live_server") is True
    receipt_path = multicall_load_receipt or os.environ.get(LAUNCH_RECEIPT_ENV_VAR)
    if receipt_path:
        receipt_file = Path(receipt_path)
        if not receipt_file.is_file():
            raise RuntimeError(f"the declared service load receipt is absent: {receipt_file}")
        try:
            receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RuntimeError(f"the service load receipt is not valid JSON: {exc}") from None
        ai.validate_launch_receipt(receipt, manifest, manifest_sha)
        record["multicall_live_loading"] = {
            "path": str(receipt_file),
            "sha256": hashlib.sha256(receipt_file.read_bytes()).hexdigest()}
        record["multicall_loading_verified"] = True
        return record
    if require_receipt or claims_live:
        raise RuntimeError(
            "this run declares the AE multicall protocol but no service load receipt "
            "was supplied; refusing to start before any model request")
    record["multicall_loading_verified"] = False
    record["multicall_loading_note"] = (
        "offline PLAN/PREFLIGHT: no service receipt is recorded, so the service's "
        "loaded Letta sources are NOT checked")
    return record


def _record_multicall_manifest(record, declared, receipt_path, *, require_receipt):
    """Record the 0.2 offline patch proof, and its receipt when one is declared."""
    path = Path(declared)
    if not path.is_file():
        raise RuntimeError(f"the reviewed compatibility manifest is absent: {path}")
    manifest_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    record["multicall_manifest"] = {"path": str(path), "sha256": manifest_sha}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    claims_live = (manifest.get("verification") or {}).get("applied_to_live_server") is True
    if not receipt_path:
        if require_receipt or claims_live:
            raise RuntimeError(
                "this run declares the AE multicall protocol but no service load receipt "
                "was supplied; refusing to start before any model request")
        record["multicall_loading_verified"] = False
        record["multicall_loading_note"] = (
            "offline PLAN/PREFLIGHT: no service receipt is recorded, so the service's "
            "loaded Letta sources are NOT checked")
        return record
    receipt_file = Path(receipt_path)
    if not receipt_file.is_file():
        raise RuntimeError(f"the declared service load receipt is absent: {receipt_file}")
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"the service load receipt is not valid JSON: {exc}") from None
    ai.validate_launch_receipt(receipt, manifest, manifest_sha)
    record["multicall_live_loading"] = {
        "path": str(receipt_file),
        "sha256": hashlib.sha256(receipt_file.read_bytes()).hexdigest()}
    record["multicall_loading_verified"] = True
    return record


def _capacity_provenance(record, config, *, require_receipt, manifest_declared,
                         receipt_declared, multicall_manifest_path=None,
                         multicall_load_receipt=None):
    """The 0.3 service evidence: the STACKED checkout's manifest and load receipt.

    The service process that runs a capacity/no-compaction protocol loads the stacked
    patch (multicall + no-compaction), and the receipt it writes is the only proof of
    WHAT it loaded: it carries `patched_files`, `new_files` and the manifest digest.
    A run therefore records that pair, validated by `validate_stack_receipt`, and a
    real RUN refuses to start without it - no fallback to the multicall receipt, which
    describes a different tree and is refused by this validator anyway.
    """
    # The offline multicall patch proof is still recorded when the caller names it:
    # a 0.3 run reuses the same reviewed compatibility patch inside the stack.
    declared_multicall = multicall_manifest_path or os.environ.get(MANIFEST_ENV_VAR)
    if declared_multicall:
        _record_multicall_manifest(record, declared_multicall,
                                   multicall_load_receipt if not (manifest_declared
                                                                  or receipt_declared)
                                   else None,
                                   require_receipt=False)
    manifest = None
    if manifest_declared:
        path = Path(manifest_declared)
        if not path.is_file():
            raise RuntimeError(f"the declared stacked manifest is absent: {path}")
        manifest_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        loaded = ai.read_stack_manifest(path)
        claims_chain = ((loaded.get("verification") or {}).get("offline_chain_verified")
                        is True)
        record["patch_stack_manifest"] = {
            "path": str(path), "sha256": manifest_sha,
            "profile_version": loaded.get("profile_version"),
            "letta_checkout": loaded.get("letta_checkout"),
            "patched_files": sorted(loaded.get("patched_files") or {}),
            "new_files": sorted(loaded.get("new_files") or {}),
            "offline_chain_verified": claims_chain}
        record["patch_stack_chain_verified"] = claims_chain
        manifest = (loaded, manifest_sha)
    receipt_path = receipt_declared
    if receipt_path:
        if manifest is None:
            raise RuntimeError(
                "a stacked service load receipt was declared without the stacked "
                "manifest it must be validated against")
        receipt_file = Path(receipt_path)
        if not receipt_file.is_file():
            raise RuntimeError(f"the declared stacked service load receipt is absent: "
                               f"{receipt_file}")
        try:
            receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RuntimeError(
                f"the stacked service load receipt is not valid JSON: {exc}") from None
        ai.validate_stack_receipt(receipt, manifest[0], manifest[1])
        record["patch_stack_live_loading"] = {
            "path": str(receipt_file),
            "sha256": hashlib.sha256(receipt_file.read_bytes()).hexdigest(),
            "instance_id": receipt.get("instance_id"),
            "checkout": receipt.get("checkout"),
            "profile_version": receipt.get("profile_version")}
        record["patch_stack_loading_verified"] = True
        record["patch_stack_protocol"] = "required" if require_receipt else "declared"
        return record
    if require_receipt:
        raise RuntimeError(
            "this RUN declares a 0.3 capacity protocol, which is served by the STACKED "
            "service patch, but no stacked service load receipt was supplied; refusing "
            "to start before any model request (pass --patch-stack-manifest and "
            "--patch-stack-load-receipt)")
    record["patch_stack_loading_verified"] = False
    record["patch_stack_protocol"] = "declared" if manifest is not None else "absent"
    record["patch_stack_loading_note"] = (
        "offline PLAN/PREFLIGHT: no stacked service receipt is recorded, so the service's "
        "loaded sources are NOT checked"
        if manifest is not None else
        "offline PLAN/PREFLIGHT: no stacked manifest or receipt was declared, so the 0.3 "
        "service identity is NOT verified; an executed RUN would be refused")
    return record


def local_environment():
    """No inherited proxy may reroute loopback; only a run-local EMPTY key is used."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("plan", "preflight", "run"), default="plan")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--vita-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--multicall-manifest", type=Path, default=None,
                        help="the reviewed compatibility manifest to record for a 0.2 config")
    parser.add_argument("--multicall-load-receipt", type=Path, default=None,
                        help="the service process's load receipt (required for a 0.2 run)")
    parser.add_argument("--patch-stack-manifest", type=Path, default=None,
                        help="the STACKED (multicall + no-compaction) service manifest; "
                             "required for a 0.3 capacity run")
    parser.add_argument("--patch-stack-load-receipt", type=Path, default=None,
                        help="the stacked service process's own patch-stack-load.json "
                             "(required for a 0.3 capacity run)")
    parser.add_argument("--exploratory-capacity-option", action="store_true",
                        help="the EXPLICIT opt-in for the DeepSeek 0.4 protocol, which has "
                             "no verified tokenizer and is bounded by a declared request-byte "
                             "budget only; without it such a config is refused and the "
                             "default RUN keeps refusing an unresolved capacity premise")
    args = parser.parse_args(argv)
    result, prov, owned = None, None, False
    try:
        raw_config = json.loads(args.config.read_text(encoding="utf-8"))
        # The exploratory option is honoured ONLY when the caller passes the flag; the
        # provider module turns it into its own literal, so nothing is defaulted here.
        exploratory = (EXPLORATORY_OPTION if args.exploratory_capacity_option else None)
        config = validate_config(raw_config, exploratory_capacity_option=exploratory)
        # Carry the SAME opt-in into every later re-validation inside the driver, so a
        # DeepSeek config cannot pass here and then be refused (or admitted) elsewhere.
        # The marker is PRIVATE to that hand-over: the plan records the candidate the
        # caller declared - the same document the gate is armed from, and the same bytes
        # `--config` points at - so the audit compares ONE declaration in three places.
        if exploratory is not None:
            config = dict(config, **{EXPLORATORY_MARKER: exploratory})
        # The capacity decision is taken BEFORE any output directory exists, so a
        # refused RUN leaves no run record that could be mistaken for an attempt.
        capacity = capacity_decision(config, args.stage)
        sample = prepare_sample(args.dataset, start_turn=START_TURN, end_turn=END_TURN)
        # The plan records the candidate document (the marker is a hand-over detail), so
        # plan, `--config` bytes and the armed declaration are one declaration.
        plan = build_plan(config, sample, code_files=multiturn_code_files(ROOT),
                          exploratory_capacity_option=exploratory)
        prov = provenance(config, args.config, args.multicall_manifest,
                          args.multicall_load_receipt,
                          require_receipt=args.stage == "run",
                          vita_source=args.vita_source,
                          patch_stack_manifest=args.patch_stack_manifest,
                          patch_stack_load_receipt=args.patch_stack_load_receipt)
        plan["provenance"] = prov
        plan["capacity_decision"] = capacity
        out = ensure_output_dir(args.output_dir)
        owned = True
        save(out / "plan.json", plan)
        native_config = out.resolve() / "vita-models.json"
        # The run-local auxiliary model config comes from the SAME contract the loader
        # checks: the declared transport's own model and required request fields.
        from ae_vita import native_model_config
        save(native_config, native_model_config(config))
        if args.stage == "plan":
            print(f"AE R/E multiturn PLAN; no network/model; tasks=t{START_TURN}..t{END_TURN}; "
                  f"arms={list(ARMS)}; output={out}")
            return 0

        os.environ["VITA_MODEL_CONFIG_PATH"] = str(native_config)
        local_environment()
        from ae_vita import NativeVita

        def runtime_factory(arm):
            # The repair protocols are part of the runtime's own construction, exactly
            # like the transport profile: a 0.5 run's wrapper enforces the declared
            # simulator boundary from its first user turn. Both arms get the same
            # bundle, and a schema that declares none passes None and behaves natively.
            return NativeVita(args.vita_source, args.dataset, config["model_origin"],
                              config["expected_model"], config["temperature"],
                              config["auxiliary_output_tokens"], seed=config["seed"],
                              transport_profile=config["transport_profile"],
                              end_turn=END_TURN,
                              protocols=run_protocols_of(config),
                              # Where the ADDITIONAL deterministic evaluation runs is
                              # read from the SAME validated declaration the driver uses,
                              # so a runtime can never disagree with the run record.
                              deterministic_evaluation=deterministic_evaluation_mode(config))

        if args.stage == "preflight":
            original_connect = socket.socket.connect

            def no_network(*_a, **_k):
                raise RuntimeError("preflight forbids socket connections")

            socket.socket.connect = no_network
            try:
                checks = preflight(sample, config)
                previews = {}
                for arm in ARMS:
                    native = runtime_factory(arm)
                    try:
                        preview = native.preview_tasks()
                        assert [p["task_id"] for p in preview] == \
                            [f"sub_U000828_{n}" for n in TASK_NUMBERS], \
                            "native scope does not match the continuous original tasks"
                        assert all(p["tool_schemas"] for p in preview), "native tools missing"
                        assert not any("memory_update" in t["name"] or
                                       "preference_memory" in t["name"]
                                       for p in preview for t in p["tool_schemas"]), \
                            "second memory backend in native tools"
                        previews[arm] = {p["task_id"]: {
                            "domain": p["domain"], "instruction": p["instruction"],
                            "tool_names": [t["name"] for t in p["tool_schemas"]],
                            "env_initial_hash": p["env_initial_hash"],
                            "model_called": p["model_called"],
                            "tools_executed": p["tools_executed"]} for p in preview}
                    finally:
                        native.abort()
                result = {
                    "schema_version": plan["schema_version"], "purpose": plan["purpose"],
                    "status": "PREFLIGHT_PASS", "network_called": False, "model_called": False,
                    "task_success": None, "scientific_result": None,
                    "scope": plan["scope"], "pair_preflight": checks["checks"],
                    "private_boundary": checks["private_boundary"],
                    "arm_previews": previews, "provenance": prov,
                }
            finally:
                socket.socket.connect = original_connect
        else:
            from ae_http import JSONHTTPTransport, JSONLJournal
            common = {key: config[key] for key in
                      ("timeout_seconds", "max_request_bytes", "max_response_bytes")}
            with ExitStack() as stack:
                journal = JSONLJournal(out / "events.jsonl")
                stack.callback(journal.close)
                model = stack.enter_context(JSONHTTPTransport(
                    config["model_origin"], out / "model-http.jsonl", max_requests=1, **common))
                letta = stack.enter_context(JSONHTTPTransport(
                    config["letta_origin"], out / "letta-http.jsonl", **common))
                result = execute_re_multiturn(config, sample, model_transport=model,
                                              letta_transport=letta,
                                              runtime_factory=runtime_factory,
                                              emit=journal.append)
        result["provenance"] = prov
        result["capacity_decision"] = capacity
        save(out / "result.json", result)
        print(f"AE R/E multiturn {result['status']}; scientific_result=null; output={out}")
        return 0 if result["status"] in ("PREFLIGHT_PASS",
                                         "RE_MULTITURN_COMPLETED_AUDIT_PENDING") else 2
    except (Exception, KeyboardInterrupt) as exc:
        failure = {"schema_version": None, "status": "INVALID", "stage": args.stage,
                   "validity_passed": False, "task_success": None, "scientific_result": None,
                   "provenance": prov,
                   "invalid_reasons": [{"type": type(exc).__name__, "message": str(exc)}]}
        try:
            if owned and args.output_dir.is_dir() and not (args.output_dir / "result.json").exists():
                save(args.output_dir / "result.json", failure)
        except Exception:
            pass
        print(f"AE R/E multiturn INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
