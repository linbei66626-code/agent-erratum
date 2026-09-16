#!/usr/bin/env python3
"""Plan by default. preflight uses native local components; run contacts services."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ae_inputs import prepare_sample, canonical_sha256
from ae_task_run import validate_config, build_plan, execute_wiring


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("plan", "preflight", "run"), default="plan")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--vita-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    owned = False
    result = None
    prov = None
    try:
        if args.output_dir.exists():
            raise FileExistsError("output already exists; no resume or overwrite")
        config = validate_config(json.loads(args.config.read_text(encoding="utf-8")))
        sample = prepare_sample(args.dataset, end_turn=5)
        plan = build_plan(config, sample)
        files = ["ae_inputs.py", "ae_adapter.py", "ae_http.py", "ae_probe.py", "ae_task_run.py",
                 "ae_vita.py", "ae_model_proxy.py", "scripts/ae_01_task_run.py", "scripts/ae_01_model_proxy.py"]
        prov = {"config_file_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
                "config_canonical_sha256": canonical_sha256(config),
                "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files},
                "python": sys.version, "python_executable": sys.executable,
                "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
                "project_git_commit": None, "live_letta_commit_verified": False}
        plan["provenance"] = prov
        args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        owned = True
        save(args.output_dir / "plan.json", plan)
        if args.stage == "plan":
            print(f"AE task PLAN; no network/model; output={args.output_dir}")
            return 0
        # Only this explicit run-local dummy-key config can route auxiliary LLMs.
        native_config = args.output_dir.resolve() / "vita-models.json"
        from ae_vita import native_model_config
        save(native_config, native_model_config(config))
        os.environ["VITA_MODEL_CONFIG_PATH"] = str(native_config)
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        # No inherited proxy may reroute local requests. These names are not read
        # for secrets; model configuration is the run-local file above.
        for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        from ae_vita import NativeVita
        def runtime_factory(_arm):
            return NativeVita(args.vita_source, args.dataset, config["model_origin"],
                              config["expected_model"], config["temperature"],
                              config["auxiliary_output_tokens"], seed=config["seed"])
        if args.stage == "preflight":
            import socket
            original_connect = socket.socket.connect
            def no_network(*_args, **_kwargs):
                raise RuntimeError("preflight forbids socket connections")
            socket.socket.connect = no_network
            try:
                native = runtime_factory("preflight")
                previews = native.preview_tasks()
                result = {"schema_version": plan["schema_version"], "status": "PREFLIGHT_PASS",
                          "network_called": False, "model_called": False,
                          "task_success": None, "scientific_result": None,
                          "native_previews": previews, "native_snapshot": native.snapshot()}
            finally:
                socket.socket.connect = original_connect
        else:
            from ae_http import JSONHTTPTransport, JSONLJournal
            common = {key: config[key] for key in ("timeout_seconds", "max_request_bytes", "max_response_bytes")}
            with ExitStack() as stack:
                journal = JSONLJournal(args.output_dir / "events.jsonl")
                stack.callback(journal.close)
                model = stack.enter_context(JSONHTTPTransport(config["model_origin"], args.output_dir / "model-http.jsonl",
                                                               max_requests=1, **common))
                letta = stack.enter_context(JSONHTTPTransport(config["letta_origin"], args.output_dir / "letta-http.jsonl",
                                                               **common))
                result = execute_wiring(config, sample, model_transport=model, letta_transport=letta,
                                        runtime_factory=runtime_factory, emit=journal.append)
        result["provenance"] = prov
        save(args.output_dir / "result.json", result)
        print(f"AE task {result['status']}; scientific_result=null; output={args.output_dir}")
        return 0 if result["status"] == "PREFLIGHT_PASS" or result.get("wiring_completed") else 2
    except (Exception, KeyboardInterrupt) as exc:
        result = {"schema_version": "ae-task-wiring-0.1", "status": "INVALID",
                  "stage": args.stage, "validity_passed": False, "task_success": None,
                  "scientific_result": None, "provenance": prov,
                  "invalid_reasons": [{"type": type(exc).__name__, "message": str(exc)}]}
        if owned and not (args.output_dir / "result.json").exists():
            save(args.output_dir / "result.json", result)
        print(f"AE task INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
