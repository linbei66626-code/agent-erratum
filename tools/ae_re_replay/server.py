"""Read-only local replay server for the sealed single-t4 R/E capture.

    python -m tools.ae_re_replay.server --data-dir <sealed dir> --host 127.0.0.1 --port 8765

Properties (enforced here, not promised):

* binds a LOOPBACK host only (127.0.0.1 / ::1 / localhost); any other host is refused;
* serves exactly four static names plus one JSON model endpoint - no directory
  listing, no path from the request is ever opened, no static download of the data;
* the replay model is built once at startup from the named sealed directory, so a
  corrupt or incomplete capture refuses to serve instead of guessing;
* no network egress, no CDN, no analytics, no upload; the data directory is only read.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from urllib.parse import urlsplit

from . import parser as replay_parser

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html",
                "/app.js": "app.js", "/style.css": "style.css"}
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


class ReplayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, model):
        super().__init__(address, handler)
        self.model = model


class Handler(BaseHTTPRequestHandler):
    server_version = "ae-re-replay/1"
    protocol_version = "HTTP/1.1"

    def _send(self, status, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802 - http.server API
        path = urlsplit(self.path).path
        if path == "/api/model":
            self._json(200, self.server.model)
            return
        if path == "/health":
            self._json(200, {"ok": True, "notice": replay_parser.NOTICE})
            return
        name = STATIC_FILES.get(path)
        if name is None:
            self._json(404, {"error": "not_found", "path": path})
            return
        target = STATIC_DIR / name
        if not target.is_file():
            self._json(500, {"error": "static_asset_missing", "name": name})
            return
        content_type = ("text/html; charset=utf-8" if name.endswith(".html")
                        else "application/javascript; charset=utf-8" if name.endswith(".js")
                        else "text/css; charset=utf-8")
        self._send(200, target.read_bytes(), content_type)

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def log_message(self, format, *args):  # noqa: A002 - http.server API
        sys.stderr.write("replay %s - %s\n" % (self.address_string(), format % args))


def build_model(data_dir):
    return replay_parser.build_replay(data_dir)


def create_server(data_dir, host="127.0.0.1", port=0):
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"the replay server binds a loopback host only, refused: {host}")
    model = build_model(data_dir)
    server = ReplayServer((host, port), Handler, model)
    return server, model


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", required=True, type=Path,
                        help="the sealed capture directory (read-only)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="loopback host only (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        server, model = create_server(args.data_dir, args.host, args.port)
    except (ValueError, replay_parser.ReplayDataError) as exc:
        print(f"replay server refused to start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    summary = {
        "url": f"http://{host}:{port}/",
        "data_dir": str(Path(args.data_dir).resolve()),
        "model_requests": model["meta"]["model_requests"],
        "chat_roles": model["meta"]["chat_roles"],
        "audit_status": model["meta"]["audit_status"],
        "result_status": model["meta"]["result_status"],
        "sources": [{"path": entry["path"], "sha256": entry["sha256"]}
                    for entry in model["sources"]],
        "notice": replay_parser.NOTICE,
    }
    # flush immediately: the banner must be visible when stdout is a pipe or a log file
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
