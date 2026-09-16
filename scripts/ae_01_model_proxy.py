#!/usr/bin/env python3
"""Foreground loopback audit proxy; does not start vLLM/Letta or run a task."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ae_model_proxy import ModelAuditProxy, ProxyConfig, make_server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-origin", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--journal", type=Path, required=True, help="New PRIVATE JSONL path; never overwritten")
    parser.add_argument("--context-window", type=int, required=True)
    parser.add_argument("--max-prompt-tokens", type=int, required=True)
    parser.add_argument("--output-reserve-tokens", type=int, required=True)
    parser.add_argument("--max-request-bytes", type=int, required=True)
    parser.add_argument("--max-response-bytes", type=int, required=True)
    parser.add_argument("--max-requests", type=int, required=True)
    parser.add_argument("--io-timeout-seconds", type=float, required=True)
    args = parser.parse_args()
    if not 1024 <= args.listen_port <= 65535:
        parser.error("listen-port must be 1024..65535")
    config = ProxyConfig(**{key: getattr(args, key) for key in (
        "upstream_origin", "model", "context_window", "max_prompt_tokens", "output_reserve_tokens",
        "max_request_bytes", "max_response_bytes", "max_requests", "io_timeout_seconds")})
    proxy = None
    server = None
    try:
        proxy = ModelAuditProxy(config, args.journal)
        server = make_server(proxy, args.listen_port)
        def interrupt(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupt)
        print(json.dumps({"status": "listening", "listen": f"http://127.0.0.1:{args.listen_port}",
                          "journal": str(args.journal), "model_inference_started": False,
                          "private_raw_bodies": True}), flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "note": "No retry. Preserve private journal."}), file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if proxy is not None:
            proxy.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
