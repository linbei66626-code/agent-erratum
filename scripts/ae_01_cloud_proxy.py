#!/usr/bin/env python3
"""Default: offline plan. --serve explicitly loads a private key and binds loopback.

This does not start Letta, create a database/Agent, or run a task. Once serving,
an incoming allowed request can spend API credits. Stop after the bounded probe.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ae_cloud_proxy import (BYTE_GATE_COUNT_BASIS, CloudConfig, CloudAuditProxy,
                            read_private_key)
from ae_model_proxy import decode_object, make_server


def bind_request_budget(config, declaration):
    """The served request budget must be the one the candidate declares.

    The proxy's own per-process counter is what actually stops a run, and the driver's
    plan and the post-hoc audit both read the candidate. A proxy started with a different
    number would silently truncate (or over-spend) the pair, so a mismatch is REFUSED
    here, in the process that serves the requests, instead of being discovered later.
    Returns the bound budget so the startup line can state it.
    """
    declared = declaration.get("max_requests")
    if declared is None:
        # A candidate that does not state a budget keeps whatever the proxy config says;
        # every reviewed candidate does state one, so this stays a compatibility path.
        return config.max_requests
    if config.max_requests != declared:
        raise ValueError(
            f"the proxy config declares max_requests={config.max_requests!r} but the "
            f"candidate declares {declared!r}: regenerate the proxy config from the "
            "candidate so the driver, the proxy and the audit share ONE budget")
    return declared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--listen-port", type=int, default=8000)
    parser.add_argument("--capacity-config", type=Path, default=None,
                        help="a candidate config whose capacity block arms the pre-send "
                             "gate in THIS process; without it the proxy is unchanged "
                             "(the sealed 0.1/0.2 protocols arm nothing)")
    parser.add_argument("--count-basis-tokenizer", type=Path, default=None,
                        help="the official tokenizer.json the gate counts with "
                             "(defaults to the local cache)")
    parser.add_argument("--count-basis-tokenizer-config", type=Path, default=None,
                        help="the official tokenizer_config.json the gate counts with")
    args = parser.parse_args()
    proxy = server = None
    try:
        config_bytes = args.config.read_bytes()
        config = CloudConfig(**decode_object(config_bytes))
        config.validate()
        if not 1024 <= args.listen_port <= 65535:
            raise ValueError("listen port must be 1024..65535")
        if not args.serve:
            print(json.dumps({"status": "PLAN_ONLY", "config": asdict(config),
                              "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
                              "key_loaded": False, "network_called": False,
                              "task_runner_ready": False, "server_token_ids": None,
                              "note": "No seed support claimed. No /tokenize or fabricated model metadata."}))
            return 0
        if args.key_file is None or args.journal is None:
            raise ValueError("serve requires key-file and new private journal")
        if args.journal.exists():
            raise FileExistsError("journal already exists")
        key = read_private_key(args.key_file)
        proxy = CloudAuditProxy(config, args.journal, api_key=key)
        armed = None
        if args.capacity_config is not None:
            declaration = json.loads(args.capacity_config.read_text(encoding="utf-8"))
            # The budget the DRIVER plans with and the audit verifies is the candidate's
            # own; this process must serve exactly that number.
            request_budget = bind_request_budget(config, declaration)
            declared_basis = ((declaration.get("capacity") or {}).get("count_basis") or [])
            if declared_basis == [BYTE_GATE_COUNT_BASIS]:
                # The MODEL-SPECIFIC byte policy: this provider has no verified tokenizer,
                # so the gate bounds each request by its own bytes against the declared
                # budget. The official tokenizer is NOT loaded and NOT called here.
                budget = (declaration.get("capacity") or {}).get("max_request_bytes")
                if not isinstance(budget, int) or budget <= 0:
                    raise ValueError("the byte-gate declaration carries no usable budget")

                def count_bytes(normalized, role):
                    return budget

                armed = proxy.arm_capacity_from_declaration(
                    declaration, count_request=count_bytes,
                    count_source=BYTE_GATE_COUNT_BASIS,
                    count_basis=BYTE_GATE_COUNT_BASIS)
            else:
                # The Qwen token path is unchanged: the counter is the shared counting
                # entry over the official assets, and a missing asset is a refusal at
                # startup rather than a run that silently checks nothing.
                from ae_multiturn_capacity import (QwenBpeTokenizer, count_request_prompt,
                                                   load_official_tokenizer_assets,
                                                   load_target_tokenizer_assets)

                capacity_block = declaration.get("capacity") or {}
                declared_asset = (capacity_block.get("tokenizer") or {}).get("asset_target")
                declared_target = (capacity_block.get("tokenizer") or {}).get("target")
                if args.count_basis_tokenizer and args.count_basis_tokenizer_config:
                    assets = {"tokenizer_json": args.count_basis_tokenizer,
                              "tokenizer_config": args.count_basis_tokenizer_config,
                              "revision": "explicit_paths", "asset_is_target": None}
                elif declared_asset and declared_asset == declared_target:
                    # The declaration names the model's OWN assets: read them by pinned
                    # file identity. A missing or altered target file refuses the startup
                    # here instead of arming the gate with another model's tokenizer.
                    assets = load_target_tokenizer_assets()
                else:
                    assets = load_official_tokenizer_assets()
                    assets["asset_is_target"] = False
                tokenizer = QwenBpeTokenizer(assets["tokenizer_json"],
                                             assets["tokenizer_config"])

                def count_request(normalized, role):
                    body = json.loads(normalized.decode("utf-8"))
                    messages = body.get("messages")
                    if not isinstance(messages, list) or not messages:
                        raise ValueError("the request carries no messages to count")
                    return count_request_prompt(tokenizer, messages,
                                                body.get("tools") or [])["prompt_tokens"]

                armed = proxy.arm_capacity_from_declaration(
                    declaration, count_request=count_request,
                    count_source=("official_qwen_tokenizer:"
                                  if assets.get("asset_is_target") else
                                  ("exploration_qwen_family_asset:"
                                   if assets.get("asset_is_target") is False else
                                   "official_qwen_tokenizer:"))
                                 + str(assets.get("revision") or "?"))
        server = make_server(proxy, args.listen_port)
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        print(json.dumps({"status": "listening",
                          "listen": f"http://127.0.0.1:{args.listen_port}",
                          "capacity_armed": armed is not None,
                          "capacity_declaration_sha256": armed,
                          "request_budget": request_budget if args.capacity_config else None,
                          "task_runner_ready": False, "inference_started": False}), flush=True)
        server.serve_forever(poll_interval=.25)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Exception text may contain credentials/remote content; never print it.
        print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__,
                          "note": "No automatic retry. Preserve private evidence."}), file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if proxy is not None:
            proxy.close()


if __name__ == "__main__":
    raise SystemExit(main())
