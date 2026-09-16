#!/usr/bin/env python3
"""Cloud-specific synthetic tool probe. Default plan; --execute makes paid calls
through an already-running loopback proxy. No credential is read by this CLI.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ae_cloud_task import CloudExecutionProfile
from ae_probe import probe_payload, execute_probe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    owned = False
    try:
        profile = CloudExecutionProfile("connection")
        config = profile.validate(json.loads(args.config.read_text()))
        payload = profile.payload(config, probe_payload(config, name="ae-cloud-probe-PLAN"))
        args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        owned = True
        def save(name, value):
            path = args.output_dir / name
            with path.open("x") as f:
                os.chmod(path, 0o600)
                json.dump(value, f, indent=2, ensure_ascii=False, allow_nan=False)
        provenance = {"config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
                      "code_sha256": {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                                      ("ae_cloud_task.py", "ae_probe.py", "ae_adapter.py", "ae_http.py",
                                       "ae_cloud_proxy.py", "scripts/ae_01_cloud_connection.py")}}
        save("plan.json", profile.annotate({"status": "PLAN_ONLY", "config": config,
             "agent_payload": payload, "model_called": False, "scientific_result": None,
             "provenance": provenance}))
        if not args.execute:
            print("CLOUD CONNECTION PLAN_ONLY; no key/network/model")
            return 0
        from ae_http import JSONHTTPTransport
        with ExitStack() as stack:
            limits = {k:config[k] for k in ("timeout_seconds", "max_request_bytes", "max_response_bytes")}
            model = stack.enter_context(JSONHTTPTransport(config["model_origin"],
                     args.output_dir / "model-http.jsonl", max_requests=1, **limits))
            letta = stack.enter_context(JSONHTTPTransport(config["letta_origin"],
                     args.output_dir / "letta-http.jsonl", max_requests=3+2*config["max_rounds"], **limits))
            result = execute_probe(config, model_transport=model, letta_transport=letta,
                                   execution_profile=profile)
        result["provenance"] = provenance
        save("result.json", result)
        print("CLOUD CONNECTION " + result["status"] + "; scientific_result=null")
        return 0 if result["connectivity_passed"] else 2
    except Exception as exc:
        # No upstream error text on stdout; full service records are private.
        if owned and not (args.output_dir / "result.json").exists():
            save("result.json", {"status":"FAIL", "error_type":type(exc).__name__, "scientific_result":None})
        print("CLOUD CONNECTION FAIL: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
