"""Explicit, dependency-free HTTP transport for local AE protocol checks.

This module neither starts services nor reads credentials/environment settings.
Every request is fsync'ed before sending. Logs are exclusive, never resumed or
overwritten, and intentionally redact authentication fields and the supplied
bearer token. They therefore are NOT byte-for-byte captures of secret-bearing
traffic. A failed/interrupted call is never retried automatically.

``timeout_seconds`` is a socket I/O timeout, not a total run/billing deadline.
The caller must additionally enforce its run budget and stop conditions.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


class HTTPTransportError(RuntimeError):
    """Sanitized transport failure; the remote operation may have happened."""

    def __init__(self, code: str, *, http_status: int | None = None):
        self.code = code
        self.http_status = http_status
        suffix = "" if http_status is None else f" (HTTP {http_status})"
        super().__init__(code + suffix + "; no automatic retry")


_SECRET_KEYS = frozenset({
    "authorization", "proxy_authorization", "api_key", "apikey", "access_token",
    "refresh_token", "bearer_token", "token", "password", "secret", "client_secret",
    "auth", "credential", "credentials",
})
_REDACTED = "[REDACTED]"


def _secret_key(value: str) -> bool:
    return value.casefold().replace("-", "_") in _SECRET_KEYS


def _reject_nonfinite(_: str) -> None:
    raise ValueError("non-finite JSON value")


def _origin(base_url: str, allow_remote_https: bool) -> str:
    if (not isinstance(base_url, str) or not base_url or re.search(r"[\s\\]", base_url)
            or "?" in base_url or "#" in base_url):
        raise ValueError("base_url must be a bare origin without query or fragment")
    try:
        parts = urlsplit(base_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError("invalid base_url origin") from None
    if (parts.username is not None or parts.password is not None or not host
            or parts.path not in {"", "/"} or parts.scheme not in {"http", "https"}):
        raise ValueError("base_url must be an HTTP(S) origin without userinfo or path")
    if host.casefold() == "localhost":
        # Do not let an altered resolver send an unapproved HTTP request remotely.
        host = "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not (parts.scheme == "http" and loopback):
        if not (parts.scheme == "https" and allow_remote_https is True):
            raise ValueError("only loopback HTTP is allowed without explicit HTTPS authorization")
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{rendered_host}" + ("" if port is None else f":{port}")


def _request_path(path: str) -> str:
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
            or re.search(r"[\s\\]", path) or "#" in path):
        raise ValueError("request path must be an origin-relative path without fragment")
    parts = urlsplit(path)
    decoded = unquote(parts.path)
    if (parts.scheme or parts.netloc or decoded.startswith("//") or "\\" in decoded
            or re.search(r"[\x00-\x20\x7f]", decoded)
            or any(segment in {".", ".."} for segment in decoded.split("/"))):
        raise ValueError("request path contains an unsafe authority or path escape")
    return path


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JSONLJournal:
    """Append-only journal with exclusive creation and fsync on every record."""

    def __init__(self, path: str | Path, *, bearer_token: str | None = None):
        self.path = Path(path)
        self._token = bearer_token
        self._lock = threading.Lock()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._file = os.fdopen(fd, "w", encoding="utf-8")
        self._sequence = 0

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): _REDACTED if _secret_key(str(key)) else self.redact(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, str) and self._token:
            return value.replace(self._token, _REDACTED)
        return value

    def append(self, event: dict) -> None:
        with self._lock:
            record = dict(event, sequence=self._sequence,
                          timestamp=datetime.now(timezone.utc).isoformat())
            line = json.dumps(self.redact(record), ensure_ascii=False, allow_nan=False,
                              sort_keys=True, separators=(",", ":")) + "\n"
            self._file.write(line)
            self._file.flush()
            os.fsync(self._file.fileno())
            self._sequence += 1

    def close(self) -> None:
        with self._lock:
            self._file.close()


class JSONHTTPTransport:
    """Object-only JSON transport compatible with ``ae_adapter.Transport``.

    All limits are explicit. ``bearer_token`` is passed by the caller, never
    fetched from a file or environment. Remote HTTPS authorization is opt-in;
    cleartext remote HTTP and all redirects remain prohibited. Environment
    proxies are ignored. Requests on one transport are serialized.
    """

    def __init__(self, base_url: str, journal_path: str | Path, *,
                 timeout_seconds: float, max_response_bytes: int, max_request_bytes: int,
                 bearer_token: str | None = None, allow_remote_https: bool = False,
                 max_requests: int | None = None):
        self.base_url = _origin(base_url, allow_remote_https)
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be finite and positive")
        for limit in (max_response_bytes, max_request_bytes):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("byte limits must be positive integers")
        if max_requests is not None and (isinstance(max_requests, bool)
                or not isinstance(max_requests, int) or max_requests <= 0):
            raise ValueError("max_requests must be a positive integer or None")
        if bearer_token is not None and (not isinstance(bearer_token, str)
                or not bearer_token or re.search(r"[^\x21-\x7e]", bearer_token)):
            raise ValueError("bearer_token must be nonempty visible ASCII without whitespace")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_request_bytes = max_request_bytes
        self.max_requests = max_requests
        self.request_count = 0
        self._token = bearer_token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._lock = threading.Lock()
        self._closed = False
        self.journal = JSONLJournal(journal_path, bearer_token=bearer_token)
        try:
            self.journal.append({"kind": "transport_open", "base_url": self.base_url,
                                 "timeout_seconds": timeout_seconds,
                                 "max_response_bytes": max_response_bytes,
                                 "max_request_bytes": max_request_bytes,
                                 "max_requests": max_requests,
                                 "allow_remote_https": allow_remote_https is True})
        except BaseException:
            self.journal.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self.journal.append({"kind": "transport_close"})
                finally:
                    self.journal.close()
                    self._closed = True

    def _log_path(self, path: str) -> str:
        parts = urlsplit(path)
        query = [(key, _REDACTED if _secret_key(key) else value)
                 for key, value in parse_qsl(parts.query, keep_blank_values=True)]
        return parts.path + ("?" + urlencode(query) if parts.query else "")

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        # Validation fails before network I/O; errors never interpolate user data.
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise ValueError("unsupported HTTP method")
        path = _request_path(path)
        if body is not None and not isinstance(body, dict):
            raise ValueError("request body must be a JSON object or None")
        try:
            payload = None if body is None else json.dumps(
                body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("request body is not valid serializable JSON") from None
        if payload is not None and len(payload) > self.max_request_bytes:
            raise HTTPTransportError("request_size_limit")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self._token is not None:
            headers["Authorization"] = "Bearer " + self._token
        req = Request(self.base_url + path, data=payload, headers=headers, method=method)
        with self._lock:
            if self._closed:
                raise HTTPTransportError("transport_closed")
            if self.max_requests is not None and self.request_count >= self.max_requests:
                self.journal.append({"kind": "blocked", "code": "request_count_limit",
                                     "request_count": self.request_count})
                raise HTTPTransportError("request_count_limit")
            request_id = uuid.uuid4().hex
            # Never send if this durable intent record cannot be written.
            self.journal.append({"kind": "request", "request_id": request_id,
                                 "method": method, "path": self._log_path(path), "body": body})
            self.request_count += 1
            try:
                with self._opener.open(req, timeout=self.timeout_seconds) as response:
                    status = response.status
                    if not 200 <= status < 300:
                        raise HTTPTransportError("http_status", http_status=status)
                    raw = response.read(self.max_response_bytes + 1)
                    if len(raw) > self.max_response_bytes:
                        raise HTTPTransportError("response_size_limit", http_status=status)
                    try:
                        result = json.loads(raw.decode("utf-8"), parse_constant=_reject_nonfinite)
                    except (ValueError, UnicodeError, RecursionError):
                        raise HTTPTransportError("invalid_json", http_status=status) from None
                    if not isinstance(result, dict):
                        raise HTTPTransportError("response_not_object", http_status=status)
                self.journal.append({"kind": "response", "request_id": request_id,
                                     "http_status": status, "body": result})
                return result
            except BaseException as exc:
                if isinstance(exc, HTTPTransportError):
                    failure = exc
                elif isinstance(exc, HTTPError):
                    code = "redirect_refused" if 300 <= exc.code < 400 else "http_status"
                    failure = HTTPTransportError(code, http_status=exc.code)
                    exc.close()
                elif isinstance(exc, TimeoutError) or (isinstance(exc, URLError)
                                                       and isinstance(exc.reason, TimeoutError)):
                    failure = HTTPTransportError("io_timeout")
                elif isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    failure = HTTPTransportError("interrupted")
                else:
                    # HTTP error text, URLs, headers, and server error bodies can
                    # contain credentials. Never interpolate the original error.
                    failure = HTTPTransportError("transport_error")
                self.journal.append({"kind": "failure", "request_id": request_id,
                                     "code": failure.code, "http_status": failure.http_status,
                                     "outcome": "not_assumed_safe_to_repeat"})
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise failure from None
