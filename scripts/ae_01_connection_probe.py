#!/usr/bin/env python3
"""Plan by default; --execute alone authorizes this CLI's loopback requests."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ae_probe import build_plan, execute_probe, validate_config


def provenance(config: dict, config_path: Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    files = ("ae_probe.py", "ae_http.py", "ae_adapter.py", "scripts/ae_01_connection_probe.py")
    return {
        "config_canonical_sha256": hashlib.sha256(json.dumps(
            config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "config_file_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "code_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files},
        "expected_letta_revision": "56ba9c25552605eec89de8ed3dc6394b625c1993",
        "live_letta_commit_verified": False,
    }


def save_new_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, help="new output directory; required for --execute")
    parser.add_argument("--execute", action="store_true", help="contact only the configured loopback services")
    args = parser.parse_args(argv)
    owns_output_dir = False
    result = None
    prov = None
    try:
        if args.execute and args.output_dir is None:
            raise ValueError("--execute requires --output-dir pointing to a new directory")
        if args.output_dir is not None and args.output_dir.exists():
            raise FileExistsError("output directory already exists; never overwrite a probe")
        if args.config.stat().st_size > 65536:
            raise ValueError("config exceeds 64 KiB")
        config = validate_config(json.loads(args.config.read_text(encoding="utf-8")))
        plan = build_plan(config)
        prov = provenance(config, args.config)
        plan["provenance"] = prov
        if args.output_dir is not None:
            args.output_dir.mkdir(parents=True, exist_ok=False)
            owns_output_dir = True
            save_new_json(args.output_dir / "plan.json", plan)
        if not args.execute:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        # Delayed import is deliberate: plan does not even construct HTTP clients.
        from ae_http import JSONHTTPTransport
        common = {"timeout_seconds": config["timeout_seconds"],
                  "max_response_bytes": config["max_response_bytes"],
                  "max_request_bytes": config["max_request_bytes"]}
        with ExitStack() as stack:
            model = stack.enter_context(JSONHTTPTransport(
                config["model_origin"], journal_path=args.output_dir / "model-http.jsonl",
                max_requests=1, **common))
            letta = stack.enter_context(JSONHTTPTransport(
                config["letta_origin"], journal_path=args.output_dir / "letta-http.jsonl",
                max_requests=3 + 2 * config["max_rounds"], **common))
            result = execute_probe(config, model_transport=model, letta_transport=letta)
    except (Exception, KeyboardInterrupt) as exc:
        if result is None:
            result = {"schema_version": "ae-connection-probe-0.1", "purpose": "connectivity_only",
                      "task_success": None, "scientific_result": None,
                      "execution_requested": args.execute, "invalid_reasons": []}
        result["status"], result["connectivity_passed"] = "FAIL", False
        result["invalid_reasons"].append({"type": type(exc).__name__, "message": str(exc)})
        if isinstance(exc, KeyboardInterrupt):
            result["interrupted"] = True
            result["server_side_work_cancelled"] = False
        # Ownership, not filename presence, decides whether writing is safe.
        if not owns_output_dir:
            print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
            return 130 if result.get("interrupted") else 2
    result["provenance"] = prov
    save_new_json(args.output_dir / "result.json", result)
    print(f"AE connection probe {result['status']}; task_success=null; output={args.output_dir}")
    return 130 if result.get("interrupted") else (0 if result["connectivity_passed"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
