"""SiliconFlow transport prototype, separate from the vLLM token-proof profile.

No network/key access on import. Only the proxy holds the upstream credential.
Bodies are private evidence; known credential echoes are withheld, never saved
as raw/base64. No automatic retries, token estimates, model-list fabrication,
or claims of deterministic generation / KV reuse. Not a task-level audit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import time
import uuid

from ae_http import JSONLJournal
from ae_model_proxy import ProxyBlocked, _NoRedirect, decode_object, wire_record

PROFILE = "siliconflow-nonthinking-text-transport-v1-prototype"
COMPAT_PROFILE = "siliconflow-letta-text-transport-v2-prototype"
#: The explicit opt-in OpenRouter transport. Its endpoint base is the documented
#: `https://openrouter.ai/api/v1`, so the real send path is
#: `/api/v1/chat/completions` (base + `/chat/completions`) and the catalog path is
#: `/api/v1/models`. The wire model id and the provider route are FIXED by this
#: profile: the pinned Nebius route, fallbacks off, parameter support required.
OPENROUTER_PROFILE = "openrouter-nebius-qwen3-30b-instruct-v1"
ORIGIN = "https://api.siliconflow.cn"
MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
OPENROUTER_ORIGIN = "https://openrouter.ai/api/v1"
OPENROUTER_WIRE_MODEL = "qwen/qwen3-30b-a3b-instruct-2507"
#: The declared candidate route. `nebius` is the documented provider slug shape; it is
#: recorded in the journal and re-checked by the audit, and it MUST be confirmed against
#: the live endpoints metadata before the first paid probe (see the delivery README):
#: the slug is declared here rather than discovered because this workspace cannot reach
#: openrouter.ai.
OPENROUTER_PROVIDER_SLUG = "nebius"


#: The SCREENING transport: the same SiliconFlow endpoint, but an independent profile
#: whose model set is exactly the two small models the user authorised a screen for. It
#: exists so a screening run never has to touch the frozen profiles' model whitelist.
SCREEN_PROFILE = "siliconflow-screening-small-models-v1"
#: The DeepSeek official multi-turn transport. It is EXPLICIT and separate: it pins its own
#: origin and wire model, it sends the provider's own non-thinking field (never the local
#: vLLM one), and it has no tokenizer - so its pre-send gate is a REQUEST-BYTE gate, declared
#: by the candidate as a non-guaranteeing exploratory protocol.
DEEPSEEK_PROFILE = "deepseek-official-re-transport-v1"
DEEPSEEK_ORIGIN = "https://api.deepseek.com"
DEEPSEEK_WIRE_MODEL = "deepseek-flash"
DEEPSEEK_MODE_FIELD = "thinking"
DEEPSEEK_MODE_VALUE = {"type": "disabled"}
#: The one count basis that means "measure the request BYTES against a declared budget".
#: It is deliberately not a token basis and is only reachable through the DeepSeek profile.
BYTE_GATE_COUNT_BASIS = "no_token_count_byte_gate_only"
SCREEN_MODELS = ("Qwen/Qwen3.5-4B", "Qwen/Qwen3.5-9B")
SCREEN_FAMILY_MODEL = "Qwen/Qwen3.5-SCREEN"
#: The explicit thinking switch. SiliconFlow documents `enable_thinking` for its hybrid
#: Qwen3 models; it is set HERE, identically for both models, and recorded in the
#: request changes, so the mode is never a silent default. Whether this endpoint accepts
#: the field in this placement is an ONLINE to-check (see the screening delivery README):
#: if it rejects the request, the refusal is visible in the journal instead of being
#: hidden by a fallback.
SCREEN_THINKING_FIELD = "enable_thinking"
SCREEN_THINKING_VALUE = False


@dataclass(frozen=True)
class TransportProfile:
    """One reviewed transport: where requests really go, and under which identity.

    A profile is the ONLY thing that decides an origin, a wire model and a provider
    route. `CloudConfig.validate` refuses any origin/model that is not the declared
    profile's, so this table cannot be widened by a config file: adding a provider means
    adding a profile here, not relaxing a whitelist.
    """
    profile: str
    origin: str
    upstream_paths: dict
    internal_model: str
    wire_model: str
    provider_slug: "str | None" = None
    require_parameters: bool = False
    allow_fallbacks: bool = False
    response_provider_field: "str | None" = None
    routing_scope: str = "none"
    #: A profile may declare a MODEL SET instead of one wire model (a screening profile
    #: runs several models through ONE endpoint and ONE journal). The request's own
    #: model must then be a member of the set and is never rewritten.
    wire_models: tuple = ()
    #: Extra request fields this profile explicitly supports (recorded, never dropped).
    extra_request_fields: tuple = ()
    #: Fields this profile REQUIRES the client to send, with the exact value it must
    #: carry. They are checked, never added by the proxy: a transport whose semantics
    #: depend on an explicit mode must refuse a request that does not state it, instead
    #: of silently running in the provider's default mode.
    required_request_fields: dict = field(default_factory=dict)
    #: Whether this profile accepts the pinned service's `tools: null` shape, which the
    #: auxiliary (judge/user) calls really carry: it means "no tools were offered" and is
    #: not the same as a malformed tool list.
    allows_null_tools: bool = False

    def declared_models(self):
        return tuple(self.wire_models) or (self.wire_model,)

    def routing_block(self):
        """The fixed routing parameters this profile adds to every request."""
        if self.provider_slug is None:
            return None
        return {"order": [self.provider_slug], "allow_fallbacks": self.allow_fallbacks,
                "require_parameters": self.require_parameters}


SILICONFLOW_PROFILE = TransportProfile(
    profile=COMPAT_PROFILE, origin=ORIGIN,
    upstream_paths={"POST": "/v1/chat/completions", "GET": "/v1/models"},
    internal_model=MODEL, wire_model=MODEL,
    # The pinned service's auxiliary calls carry `tools: null`.
    allows_null_tools=True)
OPENROUTER_TRANSPORT = TransportProfile(
    profile=OPENROUTER_PROFILE, origin=OPENROUTER_ORIGIN,
    upstream_paths={"POST": "/chat/completions", "GET": "/models"},
    internal_model=MODEL, wire_model=OPENROUTER_WIRE_MODEL,
    provider_slug=OPENROUTER_PROVIDER_SLUG, require_parameters=True,
    allow_fallbacks=False, response_provider_field="provider",
    routing_scope="pinned provider, no fallback, parameter support required")

DEEPSEEK_TRANSPORT = TransportProfile(
    profile=DEEPSEEK_PROFILE, origin=DEEPSEEK_ORIGIN,
    upstream_paths={"POST": "/chat/completions", "GET": "/models"},
    internal_model=DEEPSEEK_WIRE_MODEL, wire_model=DEEPSEEK_WIRE_MODEL,
    wire_models=(DEEPSEEK_WIRE_MODEL,),
    # The provider's own non-thinking field travels as an explicit, recorded parameter,
    # and its VALUE is required: without it the endpoint's own default mode would run.
    extra_request_fields=(DEEPSEEK_MODE_FIELD, "user", "parallel_tool_calls"),
    required_request_fields={DEEPSEEK_MODE_FIELD: DEEPSEEK_MODE_VALUE},
    # The same pinned service sends the same `tools: null` auxiliary shape here.
    allows_null_tools=True)

SCREENING_TRANSPORT = TransportProfile(
    profile=SCREEN_PROFILE, origin=ORIGIN,
    upstream_paths={"POST": "/v1/chat/completions", "GET": "/v1/models"},
    internal_model=SCREEN_FAMILY_MODEL, wire_model=SCREEN_MODELS[0],
    wire_models=SCREEN_MODELS,
    # The screening requests use the SAME pinned pass-through shape the Letta protocol
    # sends (`user`, `parallel_tool_calls: false`) plus the explicit thinking switch.
    extra_request_fields=(SCREEN_THINKING_FIELD, "user", "parallel_tool_calls"))

#: Every accepted profile. Keys are the strings a config/journal may carry.
PROFILES = {PROFILE: SILICONFLOW_PROFILE, COMPAT_PROFILE: SILICONFLOW_PROFILE,
            OPENROUTER_PROFILE: OPENROUTER_TRANSPORT,
            SCREEN_PROFILE: SCREENING_TRANSPORT,
            DEEPSEEK_PROFILE: DEEPSEEK_TRANSPORT}


def profile_of(value):
    """The declared transport profile of a CloudConfig, a run config dict or a name.

    A `CloudConfig` carries it as `profile`; a run config carries it as
    `transport_profile`. Anything else - including a missing or unknown value - is a
    refusal, never a default.
    """
    if isinstance(value, dict):
        name = value.get("profile", value.get("transport_profile"))
    else:
        name = getattr(value, "profile", value)
    if not isinstance(name, str) or name not in PROFILES:
        raise ValueError(f"unknown transport profile: {name!r}")
    return PROFILES[name]
FIELDS = frozenset({"model", "messages", "tools", "tool_choice", "stream", "n",
                    "max_tokens", "max_completion_tokens", "temperature", "top_p",
                    "top_k", "frequency_penalty", "stop", "response_format"})


def source_hashes():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("ae_cloud_proxy.py", "ae_model_proxy.py", "ae_http.py")}


def positive_int(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class CloudConfig:
    model: str
    max_output_tokens: int
    max_request_bytes: int
    max_response_bytes: int
    max_requests: int
    io_timeout_seconds: float
    upstream_origin: str = ORIGIN
    profile: str = PROFILE
    # Explicit conservative send interval for POST chat calls. 0 keeps the old
    # behaviour (no wait) so every existing config is unchanged.
    pace_seconds: float = 0.0
    #: The wire model this transport sends, when it differs from the internal alias.
    #: Required (and pinned) for a profile whose wire model is not the internal one.
    upstream_model: "str | None" = None
    #: The fixed provider slug this transport routes to, when the profile declares one.
    provider_slug: "str | None" = None
    #: The declared pre-send REQUEST-BYTE budget, used only by a profile whose gate counts
    #: bytes because the provider has no verified tokenizer. `None` keeps every existing
    #: config byte-identical.
    declared_request_byte_budget: "int | None" = None

    def validate(self):
        if self.profile not in PROFILES:
            raise ValueError("unsupported cloud origin/model/profile")
        declared = PROFILES[self.profile]
        if self.upstream_origin != declared.origin or self.model != declared.internal_model:
            raise ValueError("unsupported cloud origin/model/profile")
        if declared.wire_models:
            # A model-SET profile: `model` is the family handle, the set is fixed.
            if self.upstream_model not in (None, declared.wire_models[0]):
                raise ValueError("unsupported cloud origin/model/profile")
        elif declared.wire_model != declared.internal_model:
            if self.upstream_model != declared.wire_model:
                raise ValueError(
                    "this transport profile sends a different wire model; declare "
                    f"upstream_model={declared.wire_model!r}")
        elif self.upstream_model not in (None, declared.wire_model):
            raise ValueError("unsupported cloud origin/model/profile")
        if declared.provider_slug is not None:
            if self.provider_slug != declared.provider_slug:
                raise ValueError(
                    f"this transport profile pins the provider route "
                    f"{declared.provider_slug!r}; declare provider_slug accordingly")
        elif self.provider_slug is not None:
            raise ValueError("this transport profile has no provider route to declare")
        for name in ("max_output_tokens", "max_request_bytes", "max_response_bytes", "max_requests"):
            if not positive_int(getattr(self, name)):
                raise ValueError("positive integer required: " + name)
        t = self.io_timeout_seconds
        if type(t) not in (int, float) or not math.isfinite(t) or t <= 0:
            raise ValueError("finite positive I/O timeout required")
        p = self.pace_seconds
        if type(p) not in (int, float) or not math.isfinite(p) or p < 0:
            raise ValueError("finite non-negative pace interval required")
        if declared.profile == DEEPSEEK_PROFILE:
            # The byte gate is this profile's ONLY pre-send bound, so it must be declared
            # and it must not exceed the request-size limit the transport already enforces.
            budget = self.declared_request_byte_budget
            if not positive_int(budget):
                raise ValueError(
                    "the DeepSeek transport requires an explicit declared_request_byte_budget")
            if budget > self.max_request_bytes:
                raise ValueError(
                    "the declared byte budget cannot exceed max_request_bytes")
        elif self.declared_request_byte_budget is not None:
            raise ValueError(
                "only the DeepSeek transport declares a request-byte budget")


def read_private_key(path: Path) -> str:
    """Explicit opt-in path only; reject links, shared permissions and large files."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_size > 4096):
            raise ValueError("key must be a user-owned regular 0600 file")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        key = raw.decode("ascii").strip()
    except UnicodeError:
        raise ValueError("invalid credential format") from None
    validate_key(key)
    return key


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{16,4096}=*", key):
        raise ValueError("invalid credential format")


def normalize_request(raw: bytes, config: CloudConfig) -> tuple[bytes, list[dict]]:
    """Only a recorded output-limit rename; no prompt/tool/sampling repair."""
    body = decode_object(raw)
    declared_profile = profile_of(config)
    # Routing is the TRANSPORT's decision, declared by the profile and checked by the
    # audit. A client that tries to choose it is refused - before the generic field
    # check, so the refusal says what it is - never silently overridden.
    for routing_key in ("provider", "models", "route"):
        if routing_key in body:
            raise ProxyBlocked("client_routing_not_allowed")
    allowed = (FIELDS
               | ({"user", "parallel_tool_calls"} if config.profile == COMPAT_PROFILE else set())
               | set(declared_profile.extra_request_fields))
    if set(body) - allowed:
        # Deliberately reject seed/chat_template_kwargs and every field no declared
        # profile names. Their cloud semantics have not been verified; never silently
        # drop them. A model-specific profile may DECLARE a pass-through field (the
        # pinned Letta protocol sends `user`/`parallel_tool_calls`, and DeepSeek needs
        # its own non-thinking switch); that declaration is what admits it here.
        raise ProxyBlocked("unsupported_request_fields")
    for required, value in declared_profile.required_request_fields.items():
        # A transport whose semantics depend on an explicit field refuses a request that
        # does not state it, instead of letting the endpoint's own default decide.
        if required not in body:
            raise ProxyBlocked("declared_transport_field_missing")
        if body[required] != value:
            raise ProxyBlocked("declared_transport_field_value_changed")
    if "user" in allowed or "parallel_tool_calls" in allowed:
        # Explicit pass-through candidates emitted by pinned Letta (and by the screening
        # CLI, which sends the same shape). Do not drop them to make an API call succeed;
        # a non-false parallel_tool_calls is refused for every profile that allows it.
        if "user" in body and (not isinstance(body["user"], str) or len(body["user"]) > 256):
            raise ProxyBlocked("invalid_user_metadata")
        if "parallel_tool_calls" in body and body["parallel_tool_calls"] is not False:
            raise ProxyBlocked("parallel_tool_calls_must_be_false")
    if declared_profile.wire_models:
        # A model-SET profile names its models directly; the family handle is not a wire
        # model and must never be sent.
        if body.get("model") not in declared_profile.declared_models():
            raise ProxyBlocked("unexpected_model")
    elif body.get("model") != config.model:
        raise ProxyBlocked("unexpected_model")
    if "stream" in body and body["stream"] is not False:
        raise ProxyBlocked("streaming_not_supported")
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise ProxyBlocked("single_completion_required")
    limits = [k for k in ("max_tokens", "max_completion_tokens") if k in body]
    if len(limits) != 1 or not positive_int(body[limits[0]]):
        raise ProxyBlocked("one_explicit_output_limit_required")
    if body[limits[0]] > config.max_output_tokens:
        raise ProxyBlocked("output_limit_exceeded", 413)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProxyBlocked("messages_required")
    for m in messages:
        if (not isinstance(m, dict) or not isinstance(m.get("role"), str)
                or m["role"] not in {"system", "user", "assistant", "tool"}
                or set(m) - {"role", "content", "name", "tool_calls", "tool_call_id"}):
            raise ProxyBlocked("unsupported_message_shape")
        content = m.get("content")
        if isinstance(content, list):
            if not content or any(not isinstance(part, dict) or set(part) != {"type", "text"}
                                  or part["type"] != "text" or not isinstance(part["text"], str)
                                  for part in content):
                raise ProxyBlocked("text_only_profile")
        elif content is not None and not isinstance(content, str):
            raise ProxyBlocked("text_only_profile")
    if "tools" in body and not (declared_profile.allows_null_tools and body["tools"] is None):
        tools = body["tools"]
        if (not isinstance(tools, list) or not 1 <= len(tools) <= 128
                or any(not isinstance(t, dict) or t.get("type") != "function"
                       or not isinstance(t.get("function"), dict) for t in tools)):
            raise ProxyBlocked("function_tools_required")
    mapped = []
    if limits[0] == "max_completion_tokens":
        body["max_tokens"] = body.pop("max_completion_tokens")
        mapped.append({"operation": "rename", "from": "max_completion_tokens",
                       "to": "max_tokens", "value": body["max_tokens"],
                       "scope": "selected nonthinking model only"})
    if declared_profile.wire_models:
        # A model-SET profile: the request names its own model and it must be a member.
        if body.get("model") not in declared_profile.declared_models():
            raise ProxyBlocked("unexpected_model")
    else:
        wire_model = declared_profile.wire_model
        if body.get("model") != wire_model:
            mapped.append({"operation": "replace", "from": "model", "to": "model",
                           "value": wire_model,
                           "scope": "the transport profile's declared wire model"})
            body["model"] = wire_model
    for extra in declared_profile.extra_request_fields:
        if extra in body:
            # Declared by the profile, so it is kept AND recorded: an explicitly sent
            # mode must never look like a silent default.
            mapped.append({"operation": "keep", "from": extra, "to": extra,
                           "value": body[extra],
                           "scope": "explicit screening parameter declared by the profile"})
    routing = declared_profile.routing_block()
    if routing is not None:
        # Added HERE, never by the client, and recorded so the audit can recompute it.
        body["provider"] = routing
        mapped.append({"operation": "add", "from": None, "to": "provider",
                       "value": routing, "scope": declared_profile.routing_scope})
    if not mapped:
        return raw, []
    return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(), mapped


def response_summary(raw: bytes, status: int, requested_output: int, config: CloudConfig) -> dict:
    issues = [] if status == 200 else ["http_" + str(status)]
    try:
        body = decode_object(raw)
    except ProxyBlocked:
        body = {}
        issues.append("non_json_object_response")
    usage = body.get("usage")
    valid_usage = (isinstance(usage, dict)
                   and all(type(usage.get(k)) is int and usage[k] >= 0
                           for k in ("prompt_tokens", "completion_tokens", "total_tokens")))
    if not valid_usage:
        issues.append("missing_or_invalid_reported_usage")
    elif (usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]
          or usage["completion_tokens"] > requested_output):
        issues.append("reported_usage_inconsistent")
    choices = body.get("choices")
    reasons = []
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        issues.append("single_response_required")
    else:
        choice = choices[0]
        reason = choice.get("finish_reason")
        reasons = [reason]
        if not isinstance(reason, str) or reason not in {"stop", "tool_calls"}:
            issues.append("incomplete_or_unsupported_finish")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            issues.append("invalid_assistant_message")
        else:
            if message.get("reasoning_content") not in (None, ""):
                issues.append("unexpected_reasoning_content")
            if reason == "tool_calls" and not message.get("tool_calls"):
                issues.append("missing_tool_calls")
    if isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict) and details.get("reasoning_tokens") not in (None, 0):
            issues.append("unexpected_reasoning_tokens")
    declared = profile_of(config)
    if body.get("model") not in declared.declared_models():
        issues.append("unexpected_response_model")
    # The responder's own identity: for a profile that declares a provider field, a
    # missing or different provider is a transport issue, and the raw value is kept in
    # the journal either way.
    response_provider = None
    if declared.response_provider_field is not None:
        response_provider = body.get(declared.response_provider_field)
        if not isinstance(response_provider, str) or not response_provider:
            issues.append("missing_response_provider_identity")
        elif (declared.provider_slug is not None
              and response_provider.lower() != declared.provider_slug.lower()):
            issues.append("unexpected_response_provider")
    summary = {"http_status": status, "usage": usage, "finish_reasons": reasons,
               "response_model": body.get("model"),
               "system_fingerprint": body.get("system_fingerprint"),
               "transport_issues": issues,
               "usage_source": "provider_reported_not_independently_tokenized",
               "preflight_prompt_tokens": None, "token_ids": None,
               "tokenize_prompt_usage_agreement": None, "kv_reuse_verified": False}
    if declared.response_provider_field is not None:
        # Only a profile that HAS a responder-identity field records one: the sealed
        # SiliconFlow captures keep their original summary shape byte for byte.
        summary["response_provider"] = response_provider
    return summary


class CloudAuditProxy:
    """Serialized by the existing loopback HTTP server. Tests inject a fake opener."""
    def __init__(self, config: CloudConfig, journal_path: Path, *, api_key: str,
                 clock=time.monotonic, sleep=time.sleep):
        config.validate()
        validate_key(api_key)
        self.config, self._key = config, api_key
        self._clock, self._sleep = clock, sleep
        self.last_send_monotonic = None
        if self._contains_key(json.dumps(asdict(config)).encode()):
            raise ValueError("credential overlaps public configuration")
        self.request_count, self.blocked, self.closed = 0, False, False
        #: The pre-send capacity gate. `None` (every sealed protocol) leaves this proxy
        #: byte-for-byte as it was; a capacity candidate arms it with the declared
        #: window, the per-role output reserves and a real counting basis.
        self.capacity = None
        #: One record per gated request, kept on the proxy so the driver can persist
        #: what was checked instead of only what was blocked.
        self.capacity_checks = []
        #: Optional caller callback for those records. They are ALSO journalled as
        #: `capacity_check` rows, because the evidence a refusal must leave behind is
        #: per request: the audit reads the journal, not this object.
        self.capacity_emit = None
        #: The candidate declaration this proxy was armed from, when it was armed by
        #: its own CLI; `None` means the sealed protocols, which arm nothing.
        self.declaration = None
        self.declaration_sha256 = None
        self.journal = JSONLJournal(journal_path)
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.journal.append({"kind": "cloud_open", "profile": config.profile, "config": asdict(config),
                             "code_sha256": source_hashes(),
                             "automatic_retry": False, "private_raw_bodies": True,
                             "server_tokenizer_available": False, "scientific_result": None})

    def close(self):
        if not self.closed:
            try:
                self.journal.append({"kind": "cloud_close", "request_count": self.request_count,
                                     "blocked": self.blocked})
            finally:
                self.journal.close()
                self.closed = True

    def _contains_key(self, raw: bytes) -> bool:
        if self._key.encode() in raw:
            return True
        # Inspect each JSON string even in malformed/duplicate-key JSON; normal
        # json.loads would otherwise discard an earlier duplicate's secret.
        for match in re.finditer(rb'"(?:\\.|[^"\\])*"', raw):
            try:
                if self._key in json.loads(match.group()):
                    return True
            except (ValueError, UnicodeError):
                pass
        return False

    def _wire(self, kind, raw, **metadata):
        if self._contains_key(raw):
            self.journal.append({"kind": "credential_body_withheld", "original_kind": kind,
                                 "body_bytes": len(raw), **metadata})
            raise ProxyBlocked("credential_in_body_withheld", 502)
        self.journal.append({"kind": kind, **metadata, **wire_record(raw)})

    def _upstream(self, method, path, raw, request_id):
        declared = profile_of(self.config)
        upstream_path = declared.upstream_paths[method]
        # The journal records the origin and path the request REALLY went to, so the
        # audit can compare them with the declared profile instead of a global constant.
        self._wire("upstream_request", raw or b"", request_id=request_id, method=method,
                   path=upstream_path, origin=declared.origin)
        headers = {"Authorization": "Bearer " + self._key, "Accept": "application/json"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        request = Request(declared.origin + upstream_path, data=raw, headers=headers,
                          method=method)
        try:
            try:
                response = self.opener.open(request, timeout=self.config.io_timeout_seconds)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code
                result = response.read(self.config.max_response_bytes + 1)
                trace = (response.headers or {}).get("x-siliconcloud-trace-id")
                if (trace is not None and (self._key in trace or not re.fullmatch(r"[!-~]{1,256}", trace))):
                    trace = None
            if len(result) > self.config.max_response_bytes:
                self._wire("upstream_response_prefix", result, request_id=request_id,
                           http_status=status, complete_body=False)
                raise ProxyBlocked("upstream_response_size_limit", 502)
            self._wire("upstream_response", result, request_id=request_id, http_status=status,
                       trace_id=trace, complete_body=True)
            if 300 <= status < 400:
                raise ProxyBlocked("upstream_redirect_refused", 502)
            return status, result
        except (TimeoutError, URLError, OSError):
            raise ProxyBlocked("upstream_io_failure_no_retry", 502) from None

    def _pace(self, request_id):
        """Explicit POST-chat send interval; disabled pacing writes no record.

        With `pace_seconds == 0` the journal keeps its original five-event chat
        sequence. With pacing enabled exactly one `pace_wait` record is written
        per chat send, carrying the wait start, the target, the scheduled and the
        measured wait, and the actual send time. A sleep that returns early is
        topped up in a loop; a clock that does not advance fails instead of
        sending. GET /v1/models is never paced or recorded here.
        """
        interval = float(self.config.pace_seconds)
        if interval <= 0:
            return
        before = self._clock()
        previous = self.last_send_monotonic
        first_send = previous is None
        target = (before + interval) if first_send else (previous + interval)
        scheduled = max(0.0, target - before)
        while True:
            now = self._clock()
            remaining = target - now
            if remaining <= 1e-9:
                break
            self._sleep(remaining)
            # Compare each sleep's own before/after so a clock that advances and
            # then stalls is detected instead of looping forever.
            if self._clock() <= now:
                raise ProxyBlocked("pace_clock_did_not_advance")
        after = self._clock()
        self.last_send_monotonic = after
        self.journal.append({"kind": "pace_wait", "request_id": request_id,
                             "interval_seconds": interval, "first_send": first_send,
                             "wait_start_monotonic": before, "target_monotonic": target,
                             "scheduled_wait_seconds": scheduled,
                             "waited_seconds": after - before,
                             "previous_send_monotonic": previous,
                             "send_monotonic": after})


    def arm_capacity_from_declaration(self, declaration, *, count_request, count_source,
                                      emit=None, count_basis=None):
        """Arm the gate from a candidate's OWN capacity block, or refuse.

        The proxy is its own process, started by `scripts/ae_01_cloud_proxy.py`; the
        driver cannot reach into it. So the declaration travels as a file and this is
        where it is consumed: the window, the per-role reserves and the policy name are
        read from the same block the run record carries, and the declaration's digest
        is journalled so the audit can prove WHICH policy was armed.
        """
        capacity = declaration.get("capacity") if isinstance(declaration, dict) else None
        if not isinstance(capacity, dict):
            raise ValueError("the declaration carries no capacity block")
        self.declaration = declaration
        # The SAME canonical digest the driver records in its plan, so the audit can
        # require the two to be one policy instead of merely both present.
        from ae_inputs import canonical_sha256

        # The DECLARATION is the single source of truth for the policy, so the gate is
        # armed as the byte gate exactly when the declaration itself names the byte
        # basis - and never otherwise. A caller that forgets the argument therefore
        # cannot arm a 0.4 candidate as an unqualified token gate, and a caller cannot
        # arm a token declaration as a byte gate either: both are refusals here.
        declared_basis = list(capacity["count_basis"])
        if count_basis is None and declared_basis == [BYTE_GATE_COUNT_BASIS]:
            count_basis = BYTE_GATE_COUNT_BASIS
        if ((count_basis == BYTE_GATE_COUNT_BASIS)
                != (declared_basis == [BYTE_GATE_COUNT_BASIS])):
            raise ValueError("the arming basis is not the basis the declaration names")
        self.declaration_sha256 = canonical_sha256(declaration)
        self.arm_capacity(
            context_window=capacity["context_window"],
            agent_reserve_tokens=capacity["output_reserve_tokens"],
            auxiliary_reserve_tokens=capacity["auxiliary_reserve_tokens"],
            count_request=count_request, count_source=count_source, emit=emit,
            count_basis=count_basis)
        self.journal.append({
            "kind": "capacity_armed", "declaration_sha256": self.declaration_sha256,
            "context_window": capacity["context_window"],
            "no_compaction": capacity["no_compaction"],
            "count_basis": declared_basis,
            "count_source": count_source,
            "agent_reserve_tokens": capacity["output_reserve_tokens"],
            "auxiliary_reserve_tokens": capacity["auxiliary_reserve_tokens"]})
        return self.declaration_sha256

    def arm_capacity(self, *, context_window, agent_reserve_tokens, auxiliary_reserve_tokens,
                     count_request, count_source, emit=None, count_basis=None):
        """Arm the pre-send gate for EVERY role that reaches the provider.

        `count_request(normalized_body, role)` must return a REAL token count for that
        request or raise; there is no fallback here, because a byte ratio can
        under-estimate and let an over-capacity request through. The gate runs after
        the request is normalized and before it is journalled, paced or sent, so a
        refused request produces ZERO upstream sends.
        """
        for name, value in (("context_window", context_window),
                            ("agent_reserve_tokens", agent_reserve_tokens),
                            ("auxiliary_reserve_tokens", auxiliary_reserve_tokens)):
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"explicit positive {name} required")
        if not callable(count_request) or not isinstance(count_source, str) or not count_source:
            raise ValueError("an explicit counting basis is required")
        if emit is not None and not callable(emit):
            raise ValueError("the capacity emitter must be callable")
        self.capacity = {"count_basis": count_basis,
                         "context_window": context_window,
                         "agent_reserve_tokens": agent_reserve_tokens,
                         "auxiliary_reserve_tokens": auxiliary_reserve_tokens,
                         "count_request": count_request, "count_source": count_source}
        self.capacity_emit = emit

    def _capacity_role_reserve(self, role):
        return (self.capacity["agent_reserve_tokens"] if role == "agent_or_unknown"
                else self.capacity["auxiliary_reserve_tokens"])

    def _capacity_record(self, normalized, role, request_id):
        """Count THIS request and build its evidence record. Never decides by itself.

        `request_id` and the digest of the exact normalized bytes are part of the
        record, so an audit binds a count to the request it counted by CONTENT
        instead of trusting that a list of checks lines up with a list of requests.
        """
        if self.capacity is None:
            return None
        reserve = self._capacity_role_reserve(role)
        basis = self.capacity.get("count_basis")
        if basis == BYTE_GATE_COUNT_BASIS:
            # A provider WITHOUT a verified tokenizer is gated on the DECLARED request-byte
            # budget instead: the real outgoing body is measured, and no token count is
            # invented. `input_tokens` stays absent so an audit can never read a number here
            # as a token count.
            measured = len(normalized)
            budget = self.capacity["count_request"](normalized, role)
            if not isinstance(budget, int) or budget <= 0:
                raise ProxyBlocked("capacity_byte_budget_unavailable")
            record = {"role": role, "input_bytes": measured,
                      "request_byte_budget": budget,
                      "capacity_basis": BYTE_GATE_COUNT_BASIS,
                      "count_source": self.capacity["count_source"],
                      "output_reserve_tokens": reserve,
                      "context_window": self.capacity["context_window"],
                      "fits": measured <= budget,
                      "request_id": request_id,
                      "input_sha256": hashlib.sha256(normalized).hexdigest(),
                      "operation_guard_not_a_capacity_guarantee": True,
                      "token_count_available": False}
            record["over_by"] = max(0, measured - budget)
            return record
        counted = self.capacity["count_request"](normalized, role)
        if not isinstance(counted, int) or counted < 0:
            raise ProxyBlocked("capacity_count_unavailable")
        record = {"role": role, "input_tokens": counted,
                  "output_reserve_tokens": reserve,
                  "context_window": self.capacity["context_window"],
                  "count_source": self.capacity["count_source"],
                  "fits": counted + reserve <= self.capacity["context_window"],
                  "request_id": request_id,
                  "input_sha256": hashlib.sha256(normalized).hexdigest()}
        record["over_by"] = max(0, counted + reserve - self.capacity["context_window"])
        return record

    def _publish_capacity_check(self, record):
        """Put one check in the journal (and the caller's emitter) exactly once.

        The record kind is part of the journal format now: a gated capture shows, per
        request, which bytes were counted and against which basis - including the
        requests the gate refused before sending.
        """
        self.capacity_checks.append(record)
        entry = {"kind": "capacity_check", **record}
        self.journal.append(entry)
        if self.capacity_emit is not None:
            self.capacity_emit(entry)

    def check_capacity(self, normalized, role, request_id=None):
        """Count THIS request, publish the evidence and refuse when it does not fit.

        Kept as one call for callers that do not care about journal ordering; the
        request path below splits count from publish so the journal reads
        `normalized_request -> capacity_check -> (pace) -> upstream_request`.
        """
        if self.capacity is None:
            return None
        record = self._capacity_record(normalized, role, request_id)
        self._publish_capacity_check(record)
        if not record["fits"]:
            # The blocked row's `code` is the stable gate code; the numbers live in the
            # record just published, so an audit can attribute the refusal.
            raise ProxyBlocked("capacity_exceeded_before_send")
        return record

    def dispatch(self, method, path, raw, purpose=None, role="agent_or_unknown"):
        if self.closed:
            raise ProxyBlocked("proxy_closed", 503)
        request_id = uuid.uuid4().hex
        try:
            # Do not journal raw user-controlled headers/paths before validation.
            if (purpose is not None and (not isinstance(purpose, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", purpose) or self._key in purpose)):
                raise ProxyBlocked("invalid_purpose_header")
            if (not isinstance(role, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", role)
                    or self._key in role):
                raise ProxyBlocked("invalid_role_header")
            if (method, path) not in {("GET", "/v1/models"), ("POST", "/v1/chat/completions")}:
                raise ProxyBlocked("route_not_allowed", 404)
            if self.blocked or self.request_count >= self.config.max_requests:
                raise ProxyBlocked("proxy_stopped_or_request_limit", 503)
            if len(raw) > self.config.max_request_bytes:
                raise ProxyBlocked("request_size_limit", 413)
            self.request_count += 1
            self._wire("client_request", raw, request_id=request_id, method=method, path=path,
                       purpose=purpose, role=role)
            if method == "GET":
                if raw:
                    raise ProxyBlocked("models_request_must_be_bodyless")
                status, reply = self._upstream(method, path, None, request_id)
                if status != 200:
                    self.blocked = True
                return status, reply  # Never invent max_model_len for Letta.
            normalized, changes = normalize_request(raw, self.config)
            if len(normalized) > self.config.max_request_bytes:
                raise ProxyBlocked("normalized_request_size_limit", 413)
            # The declared-capacity gate runs on the NORMALIZED body, and the body is
            # journalled before the decision is published, so a refusal can neither
            # reach the provider nor consume the request budget - and the refused
            # request stays in the journal as evidence of what was refused.
            # Only a chat request carries a countable prompt; the models listing has no
            # body and no output reserve, so it is not gated.
            self._wire("normalized_request", normalized, request_id=request_id, changes=changes)
            capacity_record = (self._capacity_record(normalized, role, request_id)
                               if (method, path) == ("POST", "/v1/chat/completions") else None)
            if capacity_record is not None:
                self._publish_capacity_check(capacity_record)
                if not capacity_record["fits"]:
                    # The blocked row's `code` is the stable gate code; the numbers are
                    # in the record just published, so an audit can attribute the refusal.
                    raise ProxyBlocked("capacity_exceeded_before_send")
            self._pace(request_id)
            status, reply = self._upstream(method, path, normalized, request_id)
            summary = response_summary(reply, status, decode_object(normalized)["max_tokens"], self.config)
            self.journal.append({"kind": "cloud_summary", "request_id": request_id,
                                 "purpose": purpose, "role": role, **summary})
            if summary["transport_issues"]:
                self.blocked = True
            return status, reply  # Preserve original failure/truncation; stop later calls.
        except BaseException as exc:
            self.blocked = True
            code = exc.code if isinstance(exc, ProxyBlocked) else "cloud_internal_failure_no_retry"
            self.journal.append({"kind": "blocked", "request_id": request_id, "code": code,
                                 "outcome": "do_not_assume_safe_to_retry"})
            if isinstance(exc, (KeyboardInterrupt, SystemExit, ProxyBlocked)):
                raise
            raise ProxyBlocked(code, 502) from None
