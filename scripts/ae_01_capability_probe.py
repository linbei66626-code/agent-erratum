#!/usr/bin/env python3
"""AE-01 current-state capability probe CLI (plan/preflight offline, run explicit).

plan (default) and preflight never contact a service; only an explicit `run`
connects to the loopback model and Letta services. Output directories are
exclusive and never resumed or overwritten. The probe executes only U000828 t4.
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
sys.path.insert(0, str(ROOT))
from ae_capability import (  # noqa: E402
    EXPECTED_SUGAR, TARGET_PRODUCT_ID, TASK_ID, WORK_ADDRESS_KEY,
    build_plan, catalog_products, catalog_sugar, environment_orders,
    environment_stores, execute_capability, validate_config,
)
from ae_inputs import canonical_sha256, prepare_sample  # noqa: E402

CODE_FILES = ("ae_inputs.py", "ae_adapter.py", "ae_http.py", "ae_probe.py",
              "ae_task_run.py", "ae_vita.py", "ae_capability.py",
              "ae_cloud_task.py", "ae_cloud_proxy.py",
              "scripts/ae_01_capability_probe.py")


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


def provenance(config, config_path):
    return {
        "config_file_sha256": hashlib.sha256(Path(config_path).read_bytes()).hexdigest(),
        "config_canonical_sha256": canonical_sha256(config),
        "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in CODE_FILES},
        "python": sys.version, "python_executable": sys.executable,
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "project_git_commit": None, "live_letta_commit_verified": False,
    }


def local_environment():
    """No inherited proxy may reroute loopback; only a run-local EMPTY key is used."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("plan", "preflight", "run"), default="plan")
    parser.add_argument("--cloud", action="store_true", help="Explicit cloud profile; no credential in driver")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--vita-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    result, prov, owned = None, None, False
    try:
        from ae_cloud_task import CloudExecutionProfile
        profile = CloudExecutionProfile("capability") if args.cloud else None
        raw_config = json.loads(args.config.read_text(encoding="utf-8"))
        config = profile.validate(raw_config) if profile else validate_config(raw_config)
        sample = prepare_sample(args.dataset, end_turn=5)
        plan = build_plan(config, sample, execution_profile=profile)
        prov = provenance(config, args.config)
        plan["provenance"] = prov
        out = ensure_output_dir(args.output_dir)
        owned = True
        save(out / "plan.json", plan)
        # Only an explicit run-local dummy-key config can route auxiliary LLMs.
        native_config = out.resolve() / "vita-models.json"
        from ae_vita import native_model_config
        save(native_config, native_model_config(config))
        if args.stage == "plan":
            print(f"AE capability PLAN; no network/model; output={out}")
            return 0

        os.environ["VITA_MODEL_CONFIG_PATH"] = str(native_config)
        local_environment()
        from ae_vita import NativeVita

        def runtime_factory():
            return NativeVita(args.vita_source, args.dataset, config["model_origin"],
                              config["expected_model"], config["temperature"],
                              config["auxiliary_output_tokens"], seed=config["seed"],
                              transport_profile=config.get("transport_profile", "local-vllm"))

        if args.stage == "preflight":
            original_connect = socket.socket.connect

            def no_network(*_a, **_k):
                raise RuntimeError("preflight forbids socket connections")

            socket.socket.connect = no_network
            try:
                native = runtime_factory()
                try:
                    previews = native.preview_tasks()
                    assert previews and previews[0]["task_id"] == TASK_ID, "t4 preview missing"
                    started = native.start(sample["tasks"][0])
                    environment = started["environment"]
                    stores = environment_stores(environment)
                    orders = environment_orders(environment)
                    addresses = [loc.get("address") for loc in (native.snapshot().get("environment_db") or {}).get("location") or []]
                    tool_names = sorted(t["name"] for t in previews[0]["tool_schemas"])
                    catalog = catalog_products(stores)
                    target_product = catalog.get(TARGET_PRODUCT_ID)
                    checks = {
                        "previewed_subtask_ids": [p["task_id"] for p in previews],
                        "t4_previewed": any(p["task_id"] == TASK_ID for p in previews),
                        "t5_previewed": any(p["task_id"] == "sub_U000828_5" for p in previews),
                        "t5_started": False,
                        "t5_executed": False,
                        "target_product_in_t4_catalog": target_product is not None,
                        "target_name_contains_milk_tea": (isinstance(target_product, dict)
                                                          and "奶茶" in (target_product.get("name") or "")),
                        "target_spec_is_expected_sugar": catalog_sugar(stores, TARGET_PRODUCT_ID) == EXPECTED_SUGAR,
                        "catalog_product_count": len(catalog),
                        "work_address_in_t4_and_profile": (sample["initial_profile"][WORK_ADDRESS_KEY] in addresses),
                        "t4_initial_order_ids": sorted(orders),
                        "no_second_memory_backend": not any(
                            "memory_update" in n or "preference_memory" in n for n in tool_names),
                        "tool_schema_count": len(tool_names),
                    }
                    snapshot = native.snapshot()
                finally:
                    native.abort()
                required = ("target_product_in_t4_catalog", "target_name_contains_milk_tea",
                            "target_spec_is_expected_sugar", "work_address_in_t4_and_profile")
                if not all(checks[key] for key in required):
                    raise RuntimeError("t4 environment does not expose the audited target/address")
                result = {
                    "schema_version": plan["schema_version"], "purpose": plan["purpose"],
                    "status": "PREFLIGHT_PASS", "network_called": False, "model_called": False,
                    "task_success": None, "scientific_result": None, "scope": plan["scope"],
                    "t4_preview": {k: previews[0][k] for k in (
                        "task_id", "domain", "instruction", "domain_policy", "tool_schemas",
                        "env_initial_hash", "lengths_chars")},
                    "t4_environment_check": checks,
                    "native_snapshot": snapshot, "provenance": prov,
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
                result = execute_capability(config, sample, model_transport=model,
                                            letta_transport=letta, runtime_factory=runtime_factory,
                                            emit=journal.append, execution_profile=profile)
        if profile:
            profile.annotate(result)
        result["provenance"] = prov
        save(out / "result.json", result)
        print(f"AE capability {result['status']}; scientific_result=null; output={out}")
        return 0 if result["status"] in ("PREFLIGHT_PASS", "CAPABILITY_COMPLETED_AUDIT_PENDING") else 2
    except (Exception, KeyboardInterrupt) as exc:
        failure = {"schema_version": "ae-01-capability-probe-0.1", "status": "INVALID",
                   "stage": args.stage, "validity_passed": False, "task_success": None,
                   "scientific_result": None, "provenance": prov,
                   "invalid_reasons": [{"type": type(exc).__name__, "message": str(exc)}]}
        try:
            if owned and args.output_dir.is_dir() and not (args.output_dir / "result.json").exists():
                save(args.output_dir / "result.json", failure)
        except Exception:
            pass
        print(f"AE capability INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
