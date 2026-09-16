"""Bounded, synthetic Letta client-tool connectivity probe; not a Vita run.

Pure plan/validation helpers never instantiate a transport. execute_probe is
explicit and uses only caller-supplied transports to verified loopback origins.
No retries, service startup, API credential discovery, or cleanup are performed.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import json
import math
import secrets
from typing import Any
from urllib.parse import urlsplit

from ae_adapter import (
    BridgeBlocked, LettaBridge, MemoryPolicy, ToolBinding, Transport,
    creation_payload,
)


SCHEMA_VERSION = "ae-connection-probe-0.1"
CONFIG_FIELDS = {
    "schema_version", "purpose", "model_origin", "letta_origin", "model_handle",
    "expected_model", "context_window", "max_output_tokens", "max_rounds",
    "max_steps", "temperature", "timeout_seconds", "max_response_bytes",
    "max_request_bytes",
}
PROBE_TOOL = {
    "name": "read_probe",
    "description": "Read the unpredictable nonce from this local connectivity test tool.",
    "parameters": {"type": "object", "properties": {}, "required": [],
                   "additionalProperties": False},
}
PROBE_SYSTEM = (
    "This is a synthetic client-tool connectivity check, not a user task or a "
    "memory experiment. Call read_probe exactly once with no arguments. After "
    "its successful return, answer with the exact nonce in that tool result. "
    "Do not call memory_update or any other tool. Do not invent a nonce."
)


class ProbeRejected(RuntimeError):
    """Connectivity validation failed; no scientific meaning is assigned."""


def _origin(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("origins must be strings")
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("use an explicit loopback IP origin with a port") from exc
    if (parsed.scheme != "http" or not address.is_loopback
            or port is None or not 1 <= port <= 65535
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or any(ch.isspace() for ch in value)):
        raise ValueError("only plain HTTP loopback origins with explicit ports are allowed")
    return value


def validate_config(config: dict) -> dict:
    """Validate engineering caps for this small probe, not research thresholds."""
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise ValueError("config must contain exactly the documented probe fields")
    if config["schema_version"] != SCHEMA_VERSION or config["purpose"] != "connectivity_only":
        raise ValueError("wrong probe schema or purpose")
    for name in ("model_origin", "letta_origin"):
        _origin(config[name])
    if config["model_origin"] == config["letta_origin"]:
        raise ValueError("model and Letta origins must be distinct services")
    for name in ("model_handle", "expected_model"):
        value = config[name]
        if (not isinstance(value, str) or not value or len(value) > 256
                or any(ch.isspace() or ord(ch) < 32 for ch in value)
                or "://" in value):
            raise ValueError(f"invalid explicit {name}")
    if "/" not in config["model_handle"]:
        raise ValueError("model_handle must include its Letta provider prefix")
    caps = {"context_window": 8192, "max_output_tokens": 1024,
            "max_rounds": 3, "max_steps": 3,
            "max_response_bytes": 1048576, "max_request_bytes": 262144}
    for name, maximum in caps.items():
        value = config[name]
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be an integer in [1, {maximum}] for this probe")
    if config["max_output_tokens"] >= config["context_window"]:
        raise ValueError("output limit must leave room for the probe input")
    for name, maximum in (("temperature", 2), ("timeout_seconds", 60)):
        value = config[name]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= maximum
                or (name == "timeout_seconds" and value == 0)):
            raise ValueError(f"invalid bounded {name}")
    return deepcopy(config)


def probe_payload(config: dict, *, name: str) -> dict:
    memory = MemoryPolicy("erratum", {}, block_char_limit=1024)
    payload = creation_payload(name=name, model=config["model_handle"], profile={}, memory=memory)
    payload["system"] = PROBE_SYSTEM
    # Fixed archive CreateAgent supports these fields. model_settings converts
    # max_output_tokens to legacy llm_config.max_tokens (model.py:254-261).
    payload["context_window_limit"] = config["context_window"]
    payload["model_settings"] = {
        "provider_type": "openai", "max_output_tokens": config["max_output_tokens"],
        "temperature": config["temperature"], "parallel_tool_calls": False,
        "strict": False,
    }
    return payload


def build_plan(config: dict) -> dict:
    config = validate_config(config)
    return {
        "schema_version": SCHEMA_VERSION, "purpose": "connectivity_only",
        "status": "PLAN", "network_called": False, "model_called": False,
        "connectivity_passed": None, "task_success": None,
        "config": config,
        "planned_requests": [
            {"origin": config["model_origin"], "method": "GET", "path": "/v1/models"},
            {"origin": config["letta_origin"], "method": "GET", "path": "/v1/health/"},
            {"origin": config["letta_origin"], "method": "POST", "path": "/v1/agents/",
             "body": probe_payload(config, name="ae-connection-probe-GENERATED")},
            {"origin": config["letta_origin"], "method": "GET",
             "path": "/v1/agents/NEW_AGENT_ID?include=...", "purpose": "verify actual state and llm_config"},
            {"origin": config["letta_origin"], "method": "POST",
             "path": "/v1/agents/NEW_AGENT_ID/messages",
             "maximum_client_posts": config["max_rounds"], "max_steps_per_post": config["max_steps"]},
        ],
        "pass_requires": ["one real pending read_probe call", "matching tool_call_id continuation",
                          "exact returned nonce in final assistant text", "no other tool call"],
        "exclusions": ["Vita execution", "preference accuracy", "memory strategy comparison",
                       "automatic service startup", "automatic retries", "automatic cleanup"],
        "notes": ["No nonce is supplied before the tool is called.",
                  "Agent creation writes local Letta server state; execute retains its ID.",
                  "A request timeout has an uncertain server outcome; inspect saved journals.",
                  "This does not claim Qwen thinking mode is disabled or measure KV reuse."],
    }


class _RecordingTransport:
    def __init__(self, delegate: Transport, role: str, trace: list[dict]):
        self.delegate, self.role, self.trace = delegate, role, trace

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        self.trace.append({"kind": "request", "service": self.role, "method": method,
                           "path": path, "body": deepcopy(body)})
        try:
            response = self.delegate.request(method, path, body)
        except Exception as exc:
            self.trace.append({"kind": "request_error", "service": self.role,
                               "type": type(exc).__name__, "message": str(exc)})
            raise
        self.trace.append({"kind": "response", "service": self.role, "body": deepcopy(response)})
        if not isinstance(response, dict):
            raise ProbeRejected("transport response must be a JSON object")
        return response


def _check_llm_config(state: dict, config: dict) -> None:
    if any(field not in state or state[field] is not None for field in ("embedding_config", "embedding")):
        raise ProbeRejected("agent embedding configuration is absent or unexpectedly enabled")
    llm = state.get("llm_config")
    if not isinstance(llm, dict):
        raise ProbeRejected("actual agent llm_config is missing")
    expected = {
        "handle": config["model_handle"], "model": config["expected_model"],
        "model_endpoint_type": "openai", "context_window": config["context_window"],
        "max_tokens": config["max_output_tokens"], "temperature": config["temperature"],
        "parallel_tool_calls": False, "strict": False,
    }
    for key, value in expected.items():
        if key not in llm or llm[key] != value or (isinstance(value, bool) and llm[key] is not value):
            raise ProbeRejected(f"actual llm_config.{key} does not match the explicit probe config")
    endpoint = llm.get("model_endpoint")
    if not isinstance(endpoint, str) or endpoint.rstrip("/") != config["model_origin"] + "/v1":
        raise ProbeRejected("actual model endpoint differs from the checked loopback service")


class _CheckedAgentTransport:
    """Reject model/config drift on every GET before bridge continuation."""
    def __init__(self, delegate: Transport, agent_path: str, config: dict):
        self.delegate, self.agent_path, self.config = delegate, agent_path, config
        self.verified = False

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if method == "GET" and path.startswith(self.agent_path + "?"):
            response = self.delegate.request(method, path, body)
            _check_llm_config(response, self.config)
            self.verified = True
            return response
        if method == "POST" and path == self.agent_path + "/messages" and self.verified:
            self.verified = False
            return self.delegate.request(method, path, body)
        raise ProbeRejected("unexpected request or messages attempted before verified agent inspection")


class _ProbeBridge(LettaBridge):
    def _execute(self, tool: dict, bindings: dict[str, ToolBinding]) -> dict:
        if tool.get("name") != "read_probe":
            self._fail("connectivity probe refuses memory_update and all tools except read_probe")
        return super()._execute(tool, bindings)


def execute_probe(config: dict, *, model_transport: Transport, letta_transport: Transport,
                  execution_profile=None) -> dict:
    """Execute one new synthetic probe; return a failure record on any exception.

    Production callers must use the bounded loopback JSONHTTPTransport. Injected
    fakes are for offline tests only and do not constitute connectivity evidence.
    """
    config = execution_profile.validate(config) if execution_profile else validate_config(config)
    started = datetime.now(timezone.utc).isoformat()
    trace: list[dict] = []
    model = _RecordingTransport(model_transport, "model", trace)
    letta = _RecordingTransport(letta_transport, "letta", trace)
    result = {
        "schema_version": SCHEMA_VERSION, "purpose": "connectivity_only", "status": "FAIL",
        "started_at_utc": started, "config": config, "execution_requested": True,
        "connectivity_passed": False, "task_success": None, "scientific_result": None,
        "agent_id": None, "agent_cleanup_performed": False,
        "model_messages_attempted": False, "invalid_reasons": [],
        "tool_invocations": 0, "tool_call_id": None, "nonce": None,
        "transport_trace": trace, "bridge_trace": [], "assistant_messages": [],
    }
    bridge = None
    if execution_profile:
        execution_profile.annotate(result)
    try:
        listed = model.request("GET", "/v1/models")
        data = listed.get("data")
        if not isinstance(data, list):
            raise ProbeRejected("model list lacks a data array")
        matches = [m for m in data if isinstance(m, dict) and m.get("id") == config["expected_model"]]
        if len(matches) != 1:
            raise ProbeRejected("expected model is missing or duplicated in /v1/models")
        if execution_profile:
            execution_profile.check_catalog(listed, config)
        else:
            maximum = matches[0].get("max_model_len")
            if type(maximum) is not int or maximum < config["context_window"]:
                raise ProbeRejected("provider max_model_len is absent or smaller than requested context_window")
        result["provider_model"] = deepcopy(matches[0])
        health = letta.request("GET", "/v1/health/")
        result["letta_health"] = deepcopy(health)
        if health.get("status") != "ok" or not isinstance(health.get("version"), str):
            raise ProbeRejected("Letta health response lacks status=ok and version")
        if execution_profile and health["version"] != "0.16.8":
            raise ProbeRejected("cloud explicit config requires pinned Letta 0.16.8")
        name = "ae-connection-probe-" + secrets.token_hex(8)
        result["requested_agent_name"] = name
        payload = probe_payload(config, name=name)
        if execution_profile:
            payload = execution_profile.payload(config, payload)
        created = letta.request("POST", "/v1/agents/", payload)
        aid = created.get("id")
        if not isinstance(aid, str) or not aid or len(aid) > 256:
            raise ProbeRejected("create response did not provide a usable new agent ID; inspect journal")
        result["agent_id"] = aid
        memory = MemoryPolicy("erratum", {}, block_char_limit=1024)
        bridge = _ProbeBridge(letta, aid, memory, max_rounds=config["max_rounds"], max_steps=config["max_steps"])
        checked = _CheckedAgentTransport(letta, bridge.path, config)
        bridge.transport = checked

        def read_probe(**arguments):
            result["tool_invocations"] += 1
            if arguments or result["tool_invocations"] != 1:
                raise ProbeRejected("read_probe must be called exactly once with no arguments")
            result["nonce"] = "AE_PROBE_" + secrets.token_hex(16)
            return {"nonce": result["nonce"], "source": "local_synthetic_read_probe"}

        bindings = {"read_probe": ToolBinding(deepcopy(PROBE_TOOL), read_probe)}
        answer = bridge.exchange([{"role": "user", "content": (
            "Run this connectivity check now: call read_probe exactly once with an empty object, "
            "then output the exact nonce returned by that tool. Do not use memory_update."
        )}], bindings)
        result["assistant_messages"] = deepcopy(answer)
        successful = [r for r in bridge.trace if r.get("kind") == "client_tool_result"
                      and r["call"].get("name") == "read_probe" and r["result"].get("status") == "success"]
        if result["tool_invocations"] != 1 or len(successful) != 1 or not result["nonce"]:
            raise ProbeRejected("one successful real read_probe call was not observed")
        cid = successful[0]["call"]["tool_call_id"]
        result["tool_call_id"] = cid
        continued = [r for r in bridge.trace if r.get("kind") == "request" and r.get("method") == "POST"
                     and any(m.get("type") == "tool_return" and any(
                         t.get("tool_call_id") == cid and result["nonce"] in t.get("tool_return", "")
                         for t in m.get("tool_returns", [])) for m in (r.get("body") or {}).get("messages", []))]
        if len(continued) != 1:
            raise ProbeRejected("matching tool_call_id and nonce were not submitted exactly once")
        if not answer or not isinstance(answer[-1].get("content"), str) or result["nonce"] not in answer[-1]["content"]:
            raise ProbeRejected("final assistant text did not contain the tool nonce")
        if memory.writes or memory.block_text != "{}":
            raise ProbeRejected("unexpected memory modification during connectivity-only probe")
        result["status"], result["connectivity_passed"] = "PASS", True
    except (Exception, KeyboardInterrupt) as exc:
        result["invalid_reasons"].append({"type": type(exc).__name__, "message": str(exc)})
        if isinstance(exc, KeyboardInterrupt):
            result["interrupted"] = True
            result["server_side_work_cancelled"] = False
    finally:
        if bridge is not None:
            result["bridge_trace"] = deepcopy(bridge.trace)
        result["model_messages_attempted"] = any(
            r.get("kind") == "request" and r.get("service") == "letta"
            and r.get("method") == "POST" and r.get("path", "").endswith("/messages") for r in trace)
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result
