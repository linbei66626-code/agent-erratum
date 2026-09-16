"""Loopback-only, non-streaming model audit proxy; no inference is started on import.

The journal is PRIVATE: it includes exact request/response bodies (base64 + UTF-8),
including dataset history. HTTP authentication headers are neither logged nor
forwarded. Byte-for-byte bodies are forwarded; no prompt, tool, or sampling
parameter is repaired. ``io_timeout_seconds`` is a socket I/O timeout, not a
wall-clock billing deadline. Requests are serialized and never retried here.

Tokenization profile: Qwen3 text + Hermes on vLLM 0.10.0. Verified wheel SHA256:
d3a8d58eee37f2475aedaf5b54403d9747da9ec273e802ad81b32b59ba81a87b.
protocol.py:1846-1901 declares TokenizeChatRequest; serving_tokenization.py:68-84
and serving_chat.py:177-198 call the same serving_engine.py:840-939 template
pipeline. Hermes inherits the identity adjust_request. Tool choice is not a
template argument. documents/multimodal/truncation/embeddings are unsupported
here, so they are rejected, not silently excluded from the count. Runtime
prompt-token usage is compared with /tokenize, not assumed to match.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
from pathlib import Path
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from ae_http import JSONLJournal


TOKENIZE_PROFILE = "vllm-0.10.0-qwen3-text-hermes"
SOURCE_WHEEL_SHA256 = "d3a8d58eee37f2475aedaf5b54403d9747da9ec273e802ad81b32b59ba81a87b"
PURPOSE_HEADER = "X-AE-Purpose"
ROLE_HEADER = "X-AE-Role"


class ProxyBlocked(RuntimeError):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _reject_constant(_: str):
    raise ValueError("non-finite JSON")


def decode_object(raw: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                           object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError, RecursionError):
        raise ProxyBlocked("invalid_json_object") from None
    if not isinstance(value, dict):
        raise ProxyBlocked("invalid_json_object")
    return value


def wire_record(raw: bytes) -> dict:
    record = {"body_base64": base64.b64encode(raw).decode("ascii"),
              "body_sha256": hashlib.sha256(raw).hexdigest(), "body_bytes": len(raw)}
    try:
        record["body_utf8"] = raw.decode("utf-8")
    except UnicodeError:
        pass
    return record


@dataclass(frozen=True)
class ProxyConfig:
    upstream_origin: str
    model: str
    context_window: int
    max_prompt_tokens: int
    output_reserve_tokens: int
    max_request_bytes: int
    max_response_bytes: int
    max_requests: int
    io_timeout_seconds: float
    tokenize_profile: str = TOKENIZE_PROFILE

    def validate(self) -> None:
        try:
            parsed = urlsplit(self.upstream_origin)
            valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                     and parsed.port is not None and 0 < parsed.port <= 65535
                     and parsed.path in {"", "/"} and parsed.username is None
                     and parsed.password is None and "?" not in self.upstream_origin
                     and "#" not in self.upstream_origin
                     and not re.search(r"[\s\\]", self.upstream_origin))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("upstream_origin must be http://127.0.0.1:PORT")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("an explicit served model name is required")
        for key in ("context_window", "max_prompt_tokens", "output_reserve_tokens",
                    "max_request_bytes", "max_response_bytes", "max_requests"):
            if not _positive_integer(getattr(self, key)):
                raise ValueError(key + " must be a positive integer")
        if self.max_prompt_tokens + self.output_reserve_tokens > self.context_window:
            raise ValueError("prompt limit plus output reserve exceeds context window")
        if (isinstance(self.io_timeout_seconds, bool)
                or not isinstance(self.io_timeout_seconds, (int, float))
                or not math.isfinite(self.io_timeout_seconds) or self.io_timeout_seconds <= 0):
            raise ValueError("io_timeout_seconds must be finite and positive")
        if self.tokenize_profile != TOKENIZE_PROFILE:
            raise ValueError("unsupported tokenization profile")


def tokenize_projection(body: dict, config: ProxyConfig) -> tuple[dict, int]:
    """Project only fields whose pre-tokenization path is verified in the wheel."""
    if body.get("model") != config.model:
        raise ProxyBlocked("unexpected_model")
    if body.get("stream") not in (False, None):
        raise ProxyBlocked("streaming_not_supported")
    for field in ("documents", "mm_processor_kwargs", "truncate_prompt_tokens",
                  "prompt", "prompt_embeds", "multi_modal_data"):
        if body.get(field) is not None:
            raise ProxyBlocked("unsupported_tokenization_field_" + field)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProxyBlocked("messages_required")
    for message in messages:
        if not isinstance(message, dict):
            raise ProxyBlocked("invalid_message")
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get("type") != "text"
                   or not isinstance(part.get("text"), str) for part in content):
                raise ProxyBlocked("multimodal_content_not_supported")
        elif content is not None and not isinstance(content, str):
            raise ProxyBlocked("nontext_content_not_supported")
        if any(message.get(field) is not None for field in ("audio", "images", "image_url", "video")):
            raise ProxyBlocked("multimodal_content_not_supported")
    requested = body.get("max_completion_tokens")
    if requested is None:
        requested = body.get("max_tokens")
    if not _positive_integer(requested):
        raise ProxyBlocked("explicit_output_limit_required")
    if requested > config.output_reserve_tokens:
        raise ProxyBlocked("output_limit_exceeds_reserve", 413)
    if body.get("n") not in (None, 1):
        raise ProxyBlocked("multiple_completions_not_supported")
    projected = {key: body[key] for key in (
        "model", "messages", "tools", "add_generation_prompt", "continue_final_message",
        "add_special_tokens", "chat_template", "chat_template_kwargs") if key in body}
    return projected, config.output_reserve_tokens


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ModelAuditProxy:
    """Single-threaded audit core; journal path must not exist."""
    def __init__(self, config: ProxyConfig, journal_path: str | Path):
        config.validate()
        self.config = config
        self.journal = JSONLJournal(journal_path)
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.request_count = 0
        self.blocked = False
        self.closed = False
        self.journal.append({"kind": "proxy_open", "config": asdict(config),
                             "source_wheel_sha256": SOURCE_WHEEL_SHA256,
                             "private_raw_bodies": True, "automatic_retry": False})

    def close(self):
        if not self.closed:
            try:
                self.journal.append({"kind": "proxy_close", "request_count": self.request_count,
                                     "blocked": self.blocked})
            finally:
                self.journal.close()
                self.closed = True

    def upstream(self, method: str, path: str, raw: bytes | None, request_id: str, kind: str) -> tuple[int, bytes]:
        # Persist the exact bytes BEFORE any possibly billable request is sent.
        self.journal.append({"kind": "upstream_request", "request_id": request_id,
                             "purpose": kind, "method": method, "path": path,
                             **wire_record(raw or b"")})
        headers = {"Accept": "application/json"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        req = Request(self.config.upstream_origin.rstrip("/") + path,
                      data=raw, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(req, timeout=self.config.io_timeout_seconds)
            except HTTPError as exc:
                response = exc  # Preserve error status/body; do not follow redirects.
            with response:
                status = response.code
                result = response.read(self.config.max_response_bytes + 1)
            if len(result) > self.config.max_response_bytes:
                self.journal.append({"kind": "upstream_response_prefix", "request_id": request_id,
                                     "purpose": kind, "http_status": status, "complete_body": False,
                                     **wire_record(result)})
                raise ProxyBlocked("upstream_response_size_limit", 502)
            self.journal.append({"kind": "upstream_response", "request_id": request_id,
                                 "purpose": kind, "http_status": status, **wire_record(result)})
            if 300 <= status < 400:
                raise ProxyBlocked("upstream_redirect_refused", 502)
            return status, result
        except (TimeoutError, URLError, OSError):
            raise ProxyBlocked("upstream_io_failure_no_retry", 502) from None

    def dispatch(self, method: str, path: str, raw: bytes, purpose: str | None = None,
                 role: str = "agent_or_unknown") -> tuple[int, bytes]:
        request_id = uuid.uuid4().hex
        if self.closed:
            raise ProxyBlocked("proxy_closed", 503)
        self.journal.append({"kind": "client_request", "request_id": request_id,
                             "method": method, "path": path, "purpose": purpose, "role": role,
                             **wire_record(raw)})
        try:
            if self.blocked:
                raise ProxyBlocked("proxy_stopped_after_failure", 503)
            if self.request_count >= self.config.max_requests:
                raise ProxyBlocked("request_count_limit", 503)
            self.request_count += 1
            if purpose is not None and not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", purpose):
                raise ProxyBlocked("invalid_purpose_header")
            if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", role):
                raise ProxyBlocked("invalid_role_header")
            if len(raw) > self.config.max_request_bytes:
                raise ProxyBlocked("request_size_limit", 413)
            if (method, path) == ("GET", "/v1/models"):
                if raw:
                    raise ProxyBlocked("models_request_must_be_bodyless")
                return self.upstream(method, path, None, request_id, "models")
            if (method, path) != ("POST", "/v1/chat/completions"):
                raise ProxyBlocked("route_not_allowed", 404)
            body = decode_object(raw)
            projected, reserve = tokenize_projection(body, self.config)
            token_raw = json.dumps(projected, ensure_ascii=False, allow_nan=False,
                                   separators=(",", ":")).encode("utf-8")
            if len(token_raw) > self.config.max_request_bytes:
                raise ProxyBlocked("tokenize_request_size_limit", 413)
            status, token_reply = self.upstream("POST", "/tokenize", token_raw, request_id, "preflight_tokenize")
            if status != 200:
                raise ProxyBlocked("upstream_tokenize_failed", 502)
            tokenized = decode_object(token_reply)
            count, tokens, window = (tokenized.get("count"), tokenized.get("tokens"), tokenized.get("max_model_len"))
            if (not isinstance(count, int) or isinstance(count, bool) or count < 0
                    or not isinstance(tokens, list) or len(tokens) != count
                    or any(not isinstance(t, int) or isinstance(t, bool) or t < 0 for t in tokens)
                    or window != self.config.context_window):
                raise ProxyBlocked("invalid_or_wrong_window_tokenize_response", 502)
            permitted = count <= self.config.max_prompt_tokens and count + reserve <= window
            self.journal.append({"kind": "token_gate", "request_id": request_id,
                                 "purpose": purpose, "role": role, "prompt_tokens": count, "output_reserve": reserve,
                                 "context_window": window, "max_prompt_tokens": self.config.max_prompt_tokens,
                                 "passed": permitted, "projection_profile": self.config.tokenize_profile})
            if not permitted:
                raise ProxyBlocked("prompt_or_context_limit", 413)
            status, reply = self.upstream(method, path, raw, request_id, "model_inference")
            try:
                response = decode_object(reply)
                parse_error = None
            except ProxyBlocked:
                response, parse_error = {}, "non_json_object_response"
            usage = response.get("usage")
            observed = usage.get("prompt_tokens") if isinstance(usage, dict) else None
            choices = response.get("choices")
            reasons = [c.get("finish_reason") for c in choices if isinstance(c, dict)] if isinstance(choices, list) else []
            agreement = observed == count if isinstance(observed, int) and not isinstance(observed, bool) else None
            self.journal.append({"kind": "model_summary", "request_id": request_id, "purpose": purpose, "role": role,
                                 "http_status": status, "usage": usage, "finish_reasons": reasons,
                                 "response_parse_error": parse_error,
                                 "truncation_finish": "length" in reasons,
                                 "tokenize_prompt_usage_agreement": agreement})
            # Return the original response but disallow any further inference after
            # a drift/truncation/error. The caller must classify this run INVALID.
            if status != 200 or agreement is not True or "length" in reasons:
                self.blocked = True
            return status, reply
        except BaseException as exc:
            self.blocked = True
            code = exc.code if isinstance(exc, ProxyBlocked) else "proxy_internal_failure_no_retry"
            self.journal.append({"kind": "blocked", "request_id": request_id, "code": code,
                                 "outcome": "do_not_assume_safe_to_retry"})
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, ProxyBlocked):
                raise
            raise ProxyBlocked(code, 502) from None


def make_server(proxy: ModelAuditProxy, port: int) -> HTTPServer:
    """Bind only IPv4 loopback. Port 0 is permitted for offline tests."""
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("invalid listen port")
    if port and port == urlsplit(proxy.config.upstream_origin).port:
        raise ValueError("proxy listen port must differ from upstream port")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "AEModelAudit"
        sys_version = ""

        def log_message(self, *_):
            pass  # Never print request URLs, headers, or private bodies.

        def setup(self):
            super().setup()
            self.connection.settimeout(proxy.config.io_timeout_seconds)

        def do_GET(self):
            self.handle_request()

        do_POST = do_GET
        do_PUT = do_GET
        do_PATCH = do_GET
        do_DELETE = do_GET

        def handle_request(self):
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding"):
                    raise ProxyBlocked("encoded_or_chunked_body_not_supported")
                if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]+", lengths[0])):
                    raise ProxyBlocked("invalid_content_length")
                if self.command == "POST" and not lengths:
                    raise ProxyBlocked("content_length_required", 411)
                length = int(lengths[0]) if lengths else 0
                if length > proxy.config.max_request_bytes:
                    raise ProxyBlocked("request_size_limit", 413)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ProxyBlocked("incomplete_request_body")
                # All incoming headers except this annotation are intentionally ignored.
                status, reply = proxy.dispatch(self.command, self.path, raw,
                                               self.headers.get(PURPOSE_HEADER),
                                               self.headers.get(ROLE_HEADER, "agent_or_unknown"))
            except ProxyBlocked as exc:
                status = exc.status
                reply = json.dumps({"error": {"type": "ae_proxy_blocked", "code": exc.code}}).encode()
                # Includes framing/size rejection before dispatch can see a body.
                proxy.blocked = True
                proxy.journal.append({"kind": "client_rejected", "code": exc.code,
                                      "http_status": status})
            except (TimeoutError, OSError):
                status, reply = 408, b'{"error":{"code":"client_io_failure"}}'
                proxy.blocked = True
                proxy.journal.append({"kind": "client_rejected", "code": "client_io_failure",
                                      "http_status": status})
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass  # Upstream/raw evidence is already durable; do not retry it.

    return HTTPServer(("127.0.0.1", port), Handler)
