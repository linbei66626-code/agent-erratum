"""Offline tests for the AE-01 cloud input audit checker.

Positive fixtures are produced by the REAL production driver and builder path
(`CloudExecutionProfile("capability")` + `execute_capability` + the pinned
`TaskBridge`) against a scripted offline Letta session and a real
`CloudAuditProxy` whose opener is a fixture. The "actual" bytes audited are
therefore the bytes the drivers really emit; the checker never generates its own
expected values. Model replies are fixtures, not model output, and no socket is
used.

The transferred real connection journal is only ever used as a wrong-scope
negative; it is not a t4 capture and is never treated as one.
"""
from __future__ import annotations

from copy import deepcopy
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for entry in (TESTS, ROOT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import ast  # noqa: E402

from ae_adapter import dumps  # noqa: E402
from ae_capability import build_plan, execute_capability  # noqa: E402
from ae_cloud_input_audit import (  # noqa: E402
    TOOL_CALL_ID_MAX_LEN, CloudInputAudit, PRODUCTION_CODE_FILES, audit_cloud_inputs,
)
from ae_input_audit import AuditFailure  # noqa: E402
from ae_inputs import canonical_sha256  # noqa: E402
from test_ae_cloud_framing import RealLettaRenderer, _Block  # noqa: E402
from ae_cloud_audit import audit_cloud_journal  # noqa: E402
from ae_cloud_proxy import CloudAuditProxy, CloudConfig, MODEL, normalize_request, response_summary  # noqa: E402
from ae_cloud_task import CloudExecutionProfile  # noqa: E402
from ae_http import _request_path  # noqa: E402
from ae_input_audit import utc, wire  # noqa: E402
from test_ae_capability import CapEnv, sample  # noqa: E402

KEY = "sk-readiness-fixture-not-real-1234"
LETTA_SOURCE = Path(os.environ.get("AE_LETTA_SOURCE",
                                   ROOT / ".ae-verify-src/letta-v1"))
LETTA_SYSTEM_PY = LETTA_SOURCE / "letta/system.py"
REAL_T4_JOURNAL = (ROOT / "transfers/lab-cloud-t4-20260912-r1/deployment/runs"
                   / "ae-capability-cloud-t4-20260912-r1.private.jsonl")


def _pinned_function(path, name):
    text = Path(path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(text, node)
            lines = segment.splitlines()
            while lines and lines[0].lstrip().startswith("@"):
                lines.pop(0)
            return "\n".join(lines)
    raise AssertionError(f"{name} not found in {path}")


_NATIVE_PACKAGE = None


def native_package_function_response():
    """The pinned `letta.system.package_function_response` body, compiled as-is.

    `pytz` is not installed here, so the real `get_local_time` cannot run; the
    time string is injected, while the wrapper construction (key set and the
    OK/Failed status mapping) comes from the unmodified pinned function.
    """
    global _NATIVE_PACKAGE
    if _NATIVE_PACKAGE is None:
        namespace = {
            "json_dumps": lambda data, indent=2: json.dumps(data, indent=indent,
                                                            ensure_ascii=False),
            "get_local_time": lambda timezone=None: "2024-06-23 03:00:00 PM UTC+0000",
        }
        source = _pinned_function(LETTA_SYSTEM_PY, "package_function_response")
        exec(compile(source, str(LETTA_SYSTEM_PY), "exec"), namespace)
        _NATIVE_PACKAGE = namespace["package_function_response"]
    return _NATIVE_PACKAGE
CAPABILITY_CONFIG = ROOT / "configs/ae-01__capability__siliconflow.prototype.json"
CANDIDATE_CONFIG = ROOT / "configs/ae-01__cloud-transport__siliconflow.capability-candidate.json"
REAL_CONNECTION_JOURNAL = (ROOT / "transfers/lab-cloud-connection-20260911-r1"
                           / "ae-cloud-connection-20260911-r1.private.jsonl")
TARGET_PRODUCT_ID = "S17791041622763865_P00011"
WORK_ADDRESS = "甘肃省兰州市安宁区安宁西路地五大道88号"
CREATE_ORDER_ARGS = {"user_id": "U000828", "store_id": "S1",
                     "product_ids": [TARGET_PRODUCT_ID], "product_cnts": [1],
                     "address": WORK_ADDRESS, "dispatch_time": "2024-06-23 15:00:00",
                     "attributes": ["规格: 7分糖"]}


def cloud_config(path=CAPABILITY_CONFIG):
    return CloudExecutionProfile("capability").validate(json.loads(Path(path).read_text()))


def provenance(config, config_path):
    """Same fields and values as the CLI provenance() for a real plan."""
    import importlib.metadata
    return {
        "config_file_sha256": hashlib.sha256(Path(config_path).read_bytes()).hexdigest(),
        "config_canonical_sha256": canonical_sha256(config),
        "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in PRODUCTION_CODE_FILES},
        "python": sys.version, "python_executable": sys.executable,
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "project_git_commit": None, "live_letta_commit_verified": False,
    }


def now():
    return datetime.now(timezone.utc).isoformat()


class RecorderTransport:
    """Writes exactly the Letta transport journal shape the CLI produces."""

    def __init__(self, session, path):
        self.session, self.path = session, Path(path)
        self.sequence = 0
        self._file = self.path.open("x", encoding="utf-8")
        self._append({"kind": "transport_open", "base_url": "http://127.0.0.1:8283"})
        self.closed = False

    def _append(self, record):
        record = dict(record, sequence=self.sequence, timestamp=now())
        self._file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self.sequence += 1

    def request(self, method, path, body=None):
        request_id = f"http-{self.sequence}"
        self._append({"kind": "request", "request_id": request_id, "method": method,
                      "path": _request_path(path), "body": deepcopy(body)})
        response = self.session.handle(method, _request_path(path), deepcopy(body))
        self._append({"kind": "response", "request_id": request_id, "http_status": 200,
                      "body": deepcopy(response)})
        return deepcopy(response)

    def close(self):
        if not self.closed:
            self._append({"kind": "transport_close"})
            self._file.close()
            self.closed = True


class ScriptedLetta:
    """Scripted Letta 0.16.8 /messages session returning the real response shape."""

    def __init__(self, model_proxy=None, config=None):
        self.model_proxy = model_proxy
        self.native_history, self.tool_schemas = [], []
        self.config = deepcopy(config or cloud_config())
        self.agent_id = "agent-fixture-cloud-0001"
        self.block = None
        self.system = None
        self.message_ids = ["system-fixture"]
        self.pending = {}
        self.llm_config = {}

    def handle(self, method, path, body):
        if path == "/v1/health/":
            return {"status": "ok", "version": "0.16.8"}
        if method == "POST" and path == "/v1/agents/":
            self.block = deepcopy(body["memory_blocks"][0])
            self.system = letta_system_prompt(body["system"], self.block, self.agent_id)
            self.tool_schemas = deepcopy(body.get("client_tools") or [])
            self.native_history = []
            llm = body["llm_config"]
            self.llm_config = {
                "handle": llm["handle"], "model": self.config["expected_model"],
                "model_endpoint_type": llm["model_endpoint_type"],
                "model_endpoint": llm["model_endpoint"],
                "context_window": llm["context_window"], "max_tokens": llm["max_tokens"],
                "temperature": llm["temperature"],
                "parallel_tool_calls": llm["parallel_tool_calls"], "strict": llm["strict"],
            }
            return {"id": self.agent_id}
        if method == "GET" and path.startswith(f"/v1/agents/{self.agent_id}?"):
            return {
                "id": self.agent_id, "agent_type": "letta_v1_agent",
                "blocks": [dict(deepcopy(self.block), id="block-fixture")],
                "tools": [], "sources": [], "tags": [], "message_ids": list(self.message_ids),
                "managed_group": None, "pending_approval": deepcopy(self.pending) or None,
                "message_buffer_autoclear": False, "enable_sleeptime": False,
                "embedding": None, "embedding_config": None,
                "llm_config": deepcopy(self.llm_config),
            }
        if method == "POST" and path == f"/v1/agents/{self.agent_id}/messages":
            # A real Letta step calls the provider between the HTTP request and
            # its response; the model call must fall inside that window.
            self.tool_schemas = deepcopy(body.get("client_tools") or [])
            reply = self.model_proxy.chat(body["messages"], bool(body.get("client_tools")))
            return self.messages(body, reply)
        raise AssertionError(f"unexpected fixture Letta call: {method} {path}")

    def messages(self, body, reply):
        saw_tool_return = False
        self.tool_schemas = deepcopy(body.get("client_tools") or [])
        assert isinstance(body.get("messages"), list)
        for message in body["messages"]:
            if message.get("type") == "tool_return":
                saw_tool_return = True
                for returned in message["tool_returns"]:
                    self.pending.pop(returned["tool_call_id"], None)
        self.message_ids.append(f"message-{len(self.message_ids)}")
        # New input is translated into wire messages by the provider proxy; only
        # the assistant output below is added to the stored conversation here.
        message = reply["choices"][0]["message"]
        finish = reply["choices"][0]["finish_reason"]
        if saw_tool_return or not message.get("tool_calls"):
            text = message.get("content") or "fixture final answer ###STOP###"
            self.native_history.append({"role": "assistant", "content": text})
            assistant = {"message_type": "assistant_message", "content": text}
            return {"messages": [assistant], "stop_reason": {"stop_reason": "end_turn"}}
        call = {"tool_call_id": message["tool_calls"][0]["id"],
                "name": message["tool_calls"][0]["function"]["name"],
                "arguments": message["tool_calls"][0]["function"]["arguments"]}
        self.pending[call["tool_call_id"]] = call
        self.native_history.append({"role": "assistant", "content": None,
                                    "tool_calls": [deepcopy(message["tool_calls"][0])]})
        return {"messages": [{"message_type": "approval_request_message",
                              "tool_calls": [deepcopy(call)]}],
                "stop_reason": {"stop_reason": "requires_approval"}}


class FakeNative:
    """Native runtime double mirroring NativeVita's real auxiliary call record shape."""

    def __init__(self, env, proxy):
        self.env, self.proxy = env, proxy
        self.calls = []

    def record(self, role, messages, content):
        request = {"model": MODEL, "messages": deepcopy(messages), "temperature": 0,
                   "max_tokens": 4096, "tools": None, "stream": False}
        marker = f"{role.replace('_', ' ')} fixture"
        reply = chat_reply("stop", content=content)
        self.proxy.opener.register(marker, reply)
        self.calls.append({"role": role, "subtask_id": "sub_U000828_4",
                           "request": request, "response": {"raw_data": reply}, "error": None})
        body = {k: v for k, v in request.items() if k not in ("stream", "tools")}
        body["tools"] = None
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self.proxy.dispatch("POST", "/v1/chat/completions", raw, "cloud/capability", role)
        return reply

    def start(self, task):
        return {"environment": self.env, "domain_policy": "fixture policy",
                "instruction": task["instruction"],
                "greeting": {"role": "assistant", "content": "fixture greeting"}}

    def agent_stop(self, text):
        return "###STOP###" in text

    def user_reply(self, text):
        reply = self.record("user_simulator",
                            [{"role": "system", "content": "user simulator fixture"},
                             {"role": "user", "content": task_instruction()}],
                            "谢谢，结束本次离线测试。###STOP###")
        return {"content": reply["choices"][0]["message"]["content"], "stop": True, "raw": {}}

    def finish(self, task, transcript, termination_reason, duration):
        self.record("evaluator",
                    [{"role": "system", "content": "evaluator fixture"},
                     {"role": "user", "content": dumps(transcript)}],
                    dumps([{"rubric_idx": "rubric_0", "meetExpectation": True,
                            "justification": "fixture"}]))
        return {"reward_info": {"reward": 0.0}, "scientific_success": None,
                "judging_status": "MODEL_JUDGED_DEBUG_ONLY"}

    def snapshot(self):
        return {"fixture": True, "native_calls": deepcopy(self.calls),
                "environment_db": {"orders": deepcopy(self.env.tools.db.orders),
                                   "stores": deepcopy(self.env.tools.db.stores)}}

    def abort(self):
        pass


def task_instruction():
    return sample()["tasks"][0]["instruction"]


# The fixture system message is rendered by the REAL pinned Letta source through
# the renderer validated in tests/test_ae_cloud_framing.py. Neither the checker's
# own renderer nor a hand-copied metadata tail is used here.
_REAL_RENDERER = None


def real_renderer():
    global _REAL_RENDERER
    if _REAL_RENDERER is None:
        _REAL_RENDERER = RealLettaRenderer()
    return _REAL_RENDERER


def letta_system_prompt(declared_system, block, agent_id):
    """Real Letta system framing (PromptGenerator + Memory render), no hand copy."""
    return real_renderer().system_message(declared_system, [_Block(block)], agent_id)


class FixtureReply:
    def __init__(self, raw, status=200, trace="cloud-audit-fixture"):
        self._raw, self.code = raw, status
        self.headers = {"x-siliconcloud-trace-id": trace}

    def read(self, _limit=None):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def chat_reply(finish_reason, content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"model": MODEL, "id": "fixture", "system_fingerprint": "fixture",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


class ChatFixtureOpener:
    """Serves a fake provider catalog and content-keyed fixture chat replies.

    Replies are keyed by the request's own system role, not by call order, so the
    captured pairing is stable no matter which optional roles actually execute.
    """

    AGENT_REPLIES = (
        chat_reply("tool_calls", tool_calls=[
            {"id": "call-fixture-1", "type": "function",
             "function": {"name": "create_delivery_order",
                          "arguments": dumps(CREATE_ORDER_ARGS)}}]),
        chat_reply("stop", content="fixture final answer ###STOP###"),
    )

    def __init__(self):
        self.sent, self.agent_calls = [], 0
        # Role-keyed replies the native double registers before dispatching, so
        # the captured provider response and the recorded raw_data are identical.
        self.role_replies = {}
        self.catalog = json.dumps({"object": "list", "data": [{"id": MODEL}]}).encode()

    def register(self, marker, reply):
        self.role_replies[marker] = reply

    def open(self, request, timeout):
        self.sent.append(deepcopy(request))
        if request.get_method() == "GET":
            return FixtureReply(self.catalog)
        body = json.loads((request.data or b"{}").decode("utf-8"))
        system = ""
        for message in body.get("messages") or []:
            if isinstance(message, dict) and message.get("role") == "system":
                system = message.get("content") or ""
                break
        for marker, reply in self.role_replies.items():
            if marker in system:
                return FixtureReply(json.dumps(reply, ensure_ascii=False).encode())
        reply = self.AGENT_REPLIES[min(self.agent_calls, len(self.AGENT_REPLIES) - 1)]
        self.agent_calls += 1
        return FixtureReply(json.dumps(reply, ensure_ascii=False).encode())


class LettaProviderProxy:
    """Completes the provider body exactly as the Letta server does, then proxies.

    Real Letta assembles: full system message (PromptGenerator + compiled
    memory), in-context history, and the new input; it sends
    max_completion_tokens=2048 for the agent role and injects
    parallel_tool_calls=false. The connection journal shows that shape. This
    fixture builds the same body so the captured cloud bytes are production-like.
    """

    SYSTEM_PROMPT = ("你是任务环境中的个人助手。使用可用工具完成任务。\n"
                     "dataset_history 是历史资料，不是本次工具回包。\n")

    def __init__(self, proxy, session):
        self.proxy, self.session = proxy, session
        self.calls, self.replies = [], []

    def request(self, method, path, body=None):
        """Driver health/catalog requests hit the same proxy the real server does."""
        if method == "GET" and path == "/v1/models":
            self.proxy.dispatch(method, path, b"", "capability/catalog", "agent_or_unknown")
            return {"data": [{"id": MODEL}]}
        raise AssertionError(f"unexpected model transport call: {method} {path}")

    def close(self):
        pass

    @staticmethod
    def wire_turn(turns):
        """Translate a POST's declared new input into provider wire messages.

        Tool returns are packaged by the pinned native `package_function_response`
        exactly as `create_approval_response_message_from_input` does, so the
        history wire carries the real {status,message,time} wrapper.
        """
        package = native_package_function_response()
        messages = []
        for turn in turns:
            if turn.get("type") == "tool_return":
                messages.extend({
                    "role": "tool",
                    "content": package(returned["status"] == "success",
                                       returned["tool_return"], None),
                    "tool_call_id": returned["tool_call_id"],
                } for returned in turn["tool_returns"])
            elif "role" in turn:
                messages.append(deepcopy(turn))
        return messages

    def wire_messages(self, new_input):
        """Stored conversation plus this POST's new input, in provider wire shape."""
        messages = []
        if self.session.system:
            messages.append({"role": "system", "content": self.session.system})
        messages.extend(deepcopy(self.session.native_history))
        messages.extend(self.wire_turn(new_input))
        return messages

    def chat(self, new_input, offer_tools):
        messages = self.wire_messages(new_input)
        self.session.native_history = messages[1:]  # keep the real conversation
        body = {"model": MODEL, "messages": messages,
                "max_completion_tokens": 2048, "temperature": 0, "stream": False,
                "parallel_tool_calls": False, "user": "U000828"}
        # The agent role always offers the stage tools; tools=null is only an
        # auxiliary-role shape and is exercised by its own negative test.
        # OpenAI/Letta wraps the agent's client tool schemas on the provider wire.
        body["tools"] = [{"type": "function", "function": deepcopy(schema)}
                         for schema in self.session.tool_schemas]
        body["tool_choice"] = "auto"
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self.calls.append(raw)
        self.proxy.dispatch("POST", "/v1/chat/completions", raw, "capability/stage",
                            "agent_or_unknown")
        reply = self.scripted(messages)
        self.replies.append(reply)
        return reply

    def scripted(self, messages):
        """Fixture decision on the Letta replay shape (never the provider body)."""
        if any(m.get("role") == "tool" for m in messages):
            return chat_reply("stop", content="fixture final answer ###STOP###")
        return chat_reply("tool_calls", tool_calls=[
            {"id": "call-fixture-1", "type": "function",
             "function": {"name": "create_delivery_order",
                          "arguments": dumps(CREATE_ORDER_ARGS)}}])


def build_run(tmp_path, *, capability_config=CAPABILITY_CONFIG, transport_config=CANDIDATE_CONFIG,
              proxy_clock=None):
    """Produce a real-format run directory using the production driver path."""
    run_dir = Path(tmp_path) / "run"
    run_dir.mkdir()
    config = cloud_config(capability_config)
    profile = CloudExecutionProfile("capability")
    plan = build_plan(config, sample(), execution_profile=profile)
    # The real CLI stamps plan["provenance"] (scripts/ae_01_capability_probe.py
    # provenance()) before writing plan.json; reproduce its exact format so the
    # provenance gate is exercised in the positive case.
    plan["provenance"] = provenance(config, Path(capability_config))
    write_json(run_dir / "plan.json", plan)

    proxy_kwargs = {}
    if proxy_clock is not None:
        proxy_kwargs = {"clock": proxy_clock.monotonic, "sleep": proxy_clock.sleep}
    proxy = CloudAuditProxy(CloudConfig(**json.loads(Path(transport_config).read_text())),
                            Path(tmp_path) / "proxy.private.jsonl", api_key=KEY, **proxy_kwargs)
    opener = ChatFixtureOpener()
    proxy.opener = opener

    session = ScriptedLetta()
    provider = LettaProviderProxy(proxy, session)
    session.model_proxy = provider
    recorder = RecorderTransport(session, run_dir / "letta-http.jsonl")
    env = CapEnv()
    native = FakeNative(env, proxy)
    events = []
    def emit(event):
        # The real CLI journal stamps each emitted event when it is emitted.
        events.append(dict(deepcopy(event), timestamp=now()))

    result = execute_capability(config, sample(), model_transport=provider,
                                letta_transport=recorder, runtime_factory=lambda: native,
                                emit=emit, execution_profile=profile)
    recorder.close()
    proxy.close()
    # The CLI also stamps result["provenance"] before writing result.json.
    result["provenance"] = provenance(config, Path(capability_config))
    write_json(run_dir / "result.json", result)
    write_records(run_dir / "events.jsonl", events)
    return {"run_dir": run_dir, "journal": Path(tmp_path) / "proxy.private.jsonl",
            "capability_config": Path(capability_config), "transport_config": Path(transport_config),
            "result": result, "events": events, "opener": opener, "session": session,
            "native": native, "plan": plan}


def write_json(path, value):
    """Fresh fixture write only: exclusive create, never overwrites real raw."""
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def mutate_json(path, mutate):
    """In-place edit of an already generated temporary fixture file."""
    path = Path(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def write_records(path, records):
    """Create a journal exclusively, with monotonic sequence."""
    with Path(path).open("x", encoding="utf-8") as stream:
        for index, record in enumerate(records):
            row = {k: v for k, v in record.items() if k != "sequence"}
            row.setdefault("timestamp", now())
            row["sequence"] = index
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def rewrite_records(path, records, *, renumber=False):
    """Rewrite an existing temporary journal, keeping captured sequence by default.

    Deliberate sequence gaps must survive, so renumbering only happens when a
    structural add/remove mutation explicitly asks for it.
    """
    with Path(path).open("w", encoding="utf-8") as stream:
        for index, record in enumerate(records):
            row = dict(record)
            row.setdefault("timestamp", now())
            if renumber or not isinstance(row.get("sequence"), int):
                row["sequence"] = index
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def audit_of(fixture, **kwargs):
    return audit_cloud_inputs(fixture["run_dir"], fixture["journal"], **kwargs)


def codes(fixture, **kwargs):
    return [entry["code"] for entry in audit_of(fixture, **kwargs)["invalid_reasons"]]


def rewrite_wire(record, raw: bytes):
    record["body_base64"] = base64.b64encode(raw).decode("ascii")
    record["body_sha256"] = hashlib.sha256(raw).hexdigest()
    record["body_bytes"] = len(raw)
    try:
        record["body_utf8"] = raw.decode("utf-8")
    except UnicodeError:
        record.pop("body_utf8", None)


def proxy_rows(fixture):
    return [json.loads(line) for line in Path(fixture["journal"]).read_text().splitlines()]


def save_proxy_rows(fixture, rows):
    rewrite_records(Path(fixture["journal"]), rows)


def assert_transport_still_checked(fixture):
    """The proxy journal must still satisfy the real transport gate.

    Content-only mutations keep the provider response untouched, so a failure
    after this point is a checker input gate, not a transport artifact.
    """
    audit = audit_cloud_journal(fixture["journal"])
    if not audit["transport_capture_checked"]:
        raise AssertionError("transport self-inconsistent before the input gate: "
                             + repr(audit["issues"]))
    return audit


def mutate_cloud_body(fixture, mutate, *, index=0):
    """Mutate one cloud client_request body, then resync its own record chain.

    Resynced: the client request bytes/hash, the normalized request bytes/hash and
    recorded changes, the upstream request bytes/hash. The original provider
    response bytes and HTTP status are preserved and reused to recompute the
    cloud_summary fields.
    """
    rows = proxy_rows(fixture)
    config = CloudConfig(**json.loads(CANDIDATE_CONFIG.read_text()))
    candidates = [r for r in rows if r.get("kind") == "client_request"
                  and r.get("path") == "/v1/chat/completions"]
    target = candidates[index]
    body = json.loads(base64.b64decode(target["body_base64"]))
    mutate(body)
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    rewrite_wire(target, raw)
    rid = target["request_id"]
    normalized = next(r for r in rows if r.get("kind") == "normalized_request"
                      and r.get("request_id") == rid)
    normalized_raw, changes = normalize_request(raw, config)
    rewrite_wire(normalized, normalized_raw)
    normalized["changes"] = changes
    upstream = next(r for r in rows if r.get("kind") == "upstream_request"
                    and r.get("request_id") == rid)
    rewrite_wire(upstream, normalized_raw)
    upstream_response = next(r for r in rows if r.get("kind") == "upstream_response"
                             and r.get("request_id") == rid)
    # The provider reply is NOT part of the mutated request chain; reuse the
    # recorded response bytes and status so the summary stays truthful.
    response_raw = wire(upstream_response)
    status = upstream_response.get("http_status")
    requested_output = json.loads(normalized_raw)["max_tokens"]
    summary = next(r for r in rows if r.get("kind") == "cloud_summary"
                   and r.get("request_id") == rid)
    summary.update(response_summary(response_raw, status, requested_output, config))
    save_proxy_rows(fixture, rows)
    assert_transport_still_checked(fixture)
    return fixture


def event_rows(fixture):
    path = fixture["run_dir"] / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def same_journal_record(row, inner):
    """Match a bridge event to its transport journal record.

    Responses carry no path, so those are matched on kind and body only.
    """
    if row.get("kind") != inner.get("kind") or row.get("method") != inner.get("method"):
        return False
    if inner.get("kind") == "response":
        return row.get("body") == inner.get("body")
    try:
        return (_request_path(row.get("path") or "") == _request_path(inner.get("path") or "")
                and row.get("body") == inner.get("body"))
    except ValueError:
        return False


def event_mutations(fixture, mutate):
    """Apply a mutation to both the Letta journal and the bridge event stream.

    Mutating only the transport journal leaves the independently captured bridge
    trace disagreeing with it, so the cross-record consistency gate fires first.
    Bridge events carry the Letta agent id rather than the transport request_id,
    so journal records are matched by their captured method/path/body instead.
    """
    http_path = fixture["run_dir"] / "letta-http.jsonl"
    rows = [json.loads(line) for line in http_path.read_text().splitlines()]
    events = event_rows(fixture)
    changed = 0
    for event in events:
        if event.get("kind") != "bridge_event":
            continue
        inner = event.get("event") or {}
        if inner.get("kind") not in {"request", "response"}:
            continue
        cloned = deepcopy(inner.get("body"))
        if not mutate(cloned):
            continue
        matches = [row for row in rows if same_journal_record(row, inner)]
        if not matches:
            raise AssertionError("no journal record matches the bridge event")
        matches[0]["body"] = deepcopy(cloned)
        inner["body"] = deepcopy(cloned)
        changed += 1
    if not changed:
        raise AssertionError("event_mutations matched no bridge record")
    rewrite_records(http_path, rows)
    rewrite_records(fixture["run_dir"] / "events.jsonl", events)
    return fixture


def mutate_letta(fixture, mutate):
    """Mutate the loopback Letta journal request bodies."""
    path = fixture["run_dir"] / "letta-http.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row.get("kind") == "request":
            mutate(row)
    rewrite_records(path, rows)
    return fixture


def mutate_events(fixture, mutate):
    path = fixture["run_dir"] / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    mutate(rows)
    rewrite_records(path, rows)
    return fixture


def mutate_lifecycle(fixture, mutate):
    """Structure-changing event edits that keep the enclosing journal legal.

    Sequence is renumbered and timestamps are made strictly increasing, so an
    added/removed/reordered event reaches the lifecycle gate instead of failing
    the journal-integrity gate first.
    """
    path = fixture["run_dir"] / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    mutate(rows)
    base = min(utc(row["timestamp"]) for row in rows)
    for index, row in enumerate(rows):
        row["sequence"] = index
        row["timestamp"] = (base + timedelta(microseconds=index)).isoformat()
    rewrite_records(path, rows)
    return fixture


def lifecycle_row(rows, kind):
    return next(row for row in rows if row.get("kind") == kind)


class PositiveFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_run(self.tmp.name)

    def test_fixture_uses_the_real_production_path(self):
        rows = [json.loads(line) for line in
                (self.fixture["run_dir"] / "letta-http.jsonl").read_text().splitlines()]
        posts = [r["body"] for r in rows if r.get("kind") == "request"
                 and r["path"].endswith("/messages")]
        self.assertEqual(len(posts), 2)
        self.assertEqual([len(p["messages"]) for p in posts], [1, 1])
        self.assertGreaterEqual(len(posts[0]["client_tools"]), 2)
        self.assertEqual(posts[0]["messages"][0]["role"], "user")
        self.assertEqual(posts[1]["messages"][0]["type"], "tool_return")
        self.assertTrue(self.fixture["result"]["execution_complete"],
                        self.fixture["result"].get("invalid_reasons"))
        self.assertEqual(self.fixture["result"]["status"],
                         "CAPABILITY_COMPLETED_AUDIT_PENDING")

    def test_positive_fixture_passes_every_gate(self):
        report = audit_of(self.fixture)
        self.assertEqual(report["invalid_reasons"], [])
        self.assertTrue(report["transport_capture_checked"])
        self.assertTrue(report["input_audit_passed"])
        self.assertEqual(report["status"], "VALID")
        self.assertIsNone(report["task_success"])
        self.assertIsNone(report["scientific_result"])
        self.assertFalse(report["scope"]["t5_executed"])
        self.assertFalse(report["scope"]["dataset_history_sent"])
        self.assertIs(report["scope"]["memory_update_tool"], False)
        self.assertTrue(report["code_sha256"])
        # The scripted agent answers ###STOP### on its first reply, so the outer
        # user-simulator loop never runs and only the evaluator auxiliary call is
        # captured; the mapping still proves content-based auxiliary verification.
        self.assertEqual(report["cloud_calls"], {"models": 1, "agent": 2, "auxiliary": 1,
                                                 "unaccounted": 0})
        self.assertEqual(report["counts"], {"agent_posts": 2, "user_messages": 1,
                                            "tool_returns": 1})
        kinds = [frame["kind"] for frame in report["frames"]]
        self.assertIn("agent_creation", kinds)
        self.assertIn("current_task", kinds)
        self.assertIn("agent_frame", kinds)
        self.assertEqual([m["role"] for m in report["auxiliary_mappings"]], ["evaluator"])

    def test_cli_reserves_output_exclusively_and_needs_no_network(self):
        spec = importlib.util.spec_from_file_location(
            "cloud_input_audit_cli", ROOT / "scripts/ae_01_cloud_input_audit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = Path(self.tmp.name) / "report.json"
        with patch("socket.socket.connect", side_effect=AssertionError("offline")):
            code = module.main(["--run-dir", str(self.fixture["run_dir"]),
                                "--proxy-journal", str(self.fixture["journal"]),
                                "--output", str(out)])
        self.assertEqual(code, 0)
        report = json.loads(out.read_text())
        self.assertTrue(report["input_audit_passed"])
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        before = out.read_bytes()
        with patch("socket.socket.connect", side_effect=AssertionError("offline")):
            again = module.main(["--run-dir", str(self.fixture["run_dir"]),
                                 "--proxy-journal", str(self.fixture["journal"]),
                                 "--output", str(out)])
        self.assertEqual(again, 2)
        self.assertEqual(before, out.read_bytes())


class InputContentNegativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_run(self.tmp.name)

    def assert_code(self, expected):
        # Content mutations must keep the transport gate satisfied, so the
        # reported failure is the input gate and not a transport artifact.
        assert_transport_still_checked(self.fixture)
        found = codes(self.fixture)
        self.assertIn(expected, found, found)

    def test_preference_drift_between_plan_and_creation_is_caught(self):
        def edit(plan):
            block = plan["agent_payload"]["memory_blocks"][0]
            block["value"] = block["value"].replace("7分糖", "5分糖")
        mutate_json(self.fixture["run_dir"] / "plan.json", edit)
        self.assert_code("creation_payload_differs_from_plan")

    def test_system_change_in_creation_body_is_caught(self):
        mutate_letta(self.fixture, lambda row: row["body"].__setitem__(
            "system", row["body"]["system"] + "额外注入")
            if row["path"] == "/v1/agents/" else None)
        self.assert_code("agent_create_http_body_differs_from_event_payload")

    def test_tool_schema_change_is_caught(self):
        """client_tools are bare schemas on this wire; the bridge trace is synced."""
        def mutate(body):
            if isinstance(body, dict) and "client_tools" in body:
                body["client_tools"][0]["parameters"] = {"type": "object",
                                                         "properties": {"x": {}}}
                return True
            return False
        event_mutations(self.fixture, mutate)
        self.assert_code("client_tool_schema_changed")

    def test_extra_tool_in_post_is_caught(self):
        def mutate(body):
            if isinstance(body, dict) and "client_tools" in body:
                body["client_tools"].append(
                    {"name": "unexpected_tool", "description": "fixture",
                     "parameters": {"type": "object", "properties": {}}})
                return True
            return False
        event_mutations(self.fixture, mutate)
        self.assert_code("client_tools_differ_from_declared")

    def test_tool_call_argument_change_is_caught(self):
        def mutate(body):
            for message in body["messages"]:
                if isinstance(message, dict) and message.get("tool_calls"):
                    message["tool_calls"][0]["function"]["arguments"] = dumps(
                        dict(CREATE_ORDER_ARGS, product_cnts=[9]))
        mutate_cloud_body(self.fixture, mutate, index=1)
        self.assert_code("assistant_history_changed")

    def test_tool_return_body_change_is_caught(self):
        def mutate(body):
            for message in body["messages"]:
                if isinstance(message, dict) and message.get("role") == "tool":
                    message["content"] = '{"status": "OK", "message": "{}", "time": "2024-06-23"}'
        mutate_cloud_body(self.fixture, mutate, index=1)
        # The wire comparison checks the raw tool result string, so this hits the
        # tool-return gate rather than the assistant-history gate.
        self.assert_code("tool_return_content_changed")

    def test_missing_message_is_caught(self):
        mutate_cloud_body(self.fixture, lambda body: body.__setitem__(
            "messages", body["messages"][:1]), index=1)
        self.assert_code("history_count_changed_or_extra_message")

    def test_extra_message_is_caught(self):
        mutate_cloud_body(self.fixture, lambda body: body["messages"].append(
            {"role": "assistant", "content": "注入的额外助手消息"}), index=1)
        self.assert_code("history_count_changed_or_extra_message")

    def test_reordered_messages_are_caught(self):
        def mutate(body):
            if len(body["messages"]) > 2:
                body["messages"][1], body["messages"][2] = (body["messages"][2],
                                                            body["messages"][1])
        mutate_cloud_body(self.fixture, mutate, index=1)
        self.assert_code("history_order_or_role_changed")

    def test_dataset_history_injection_is_caught(self):
        def mutate(body):
            if isinstance(body, dict) and "messages" in body:
                body["messages"] = body["messages"] + [
                    {"role": "user", "content": dumps({"source": "dataset_history/material",
                                                       "ref": "t4/history/0", "record": {}})}]
                return True
            return False
        event_mutations(self.fixture, mutate)
        # The run-wide dataset-history gate fires before the per-message source gate.
        self.assert_code("dataset_history_was_sent")

    def test_future_turn_reference_is_caught(self):
        def mutate(body):
            if not isinstance(body, dict) or "messages" not in body:
                return False
            for message in body["messages"]:
                if message.get("role") == "user":
                    material = json.loads(message["content"])
                    material["ref"] = "t5/user/0"
                    message["content"] = dumps(material)
                    return True
            return False
        event_mutations(self.fixture, mutate)
        # The future-turn reference is caught by the run-wide leak gate before the
        # per-message ref gate can be reached.
        self.assert_code("future_turn_leaked")

    def test_undeclared_tool_in_model_output_is_caught(self):
        """Journal and bridge trace are synced; the model named an unoffered tool."""
        def mutate(body):
            if not isinstance(body, dict) or "messages" not in body:
                return False
            for message in body.get("messages", []):
                if message.get("message_type") == "approval_request_message":
                    message["tool_calls"][0]["name"] = "memory_update"
                    return True
            return False
        event_mutations(self.fixture, mutate)
        self.assert_code("model_called_undeclared_tool")


class AuxiliaryNegativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_run(self.tmp.name)

    def assert_code(self, expected):
        found = codes(self.fixture)
        self.assertIn(expected, found, found)

    def _aux_index(self, role):
        rows = proxy_rows(self.fixture)
        candidates = [r for r in rows if r.get("kind") == "client_request"
                      and r.get("path") == "/v1/chat/completions"]
        for index, row in enumerate(candidates):
            body = json.loads(base64.b64decode(row["body_base64"]))
            text = json.dumps(body.get("messages"), ensure_ascii=False)
            if f"{role} fixture" in text:
                return index
        raise AssertionError("auxiliary fixture call not found: " + role)

    def test_auxiliary_message_change_is_caught(self):
        mutate_cloud_body(self.fixture,
                          lambda body: body["messages"][0].__setitem__(
                              "content", "tampered " + body["messages"][0]["content"]),
                          index=self._aux_index("evaluator"))
        self.assert_code("auxiliary_call_missing_ambiguous_or_changed")

    def test_auxiliary_max_tokens_change_is_caught(self):
        mutate_cloud_body(self.fixture,
                          lambda body: body.__setitem__("max_tokens", 2048),
                          index=self._aux_index("evaluator"))
        self.assert_code("auxiliary_parameters_changed")

    def test_auxiliary_seed_is_rejected_by_the_transport_gate(self):
        """`seed` is undeclared for the production proxy, so it never reaches a
        semantic gate: the request cannot be normalized and the journal fails the
        real transport check. This asserts that, instead of a faked semantic pass.
        """
        index = self._aux_index("evaluator")
        rows = proxy_rows(self.fixture)
        target = [r for r in rows if r.get("kind") == "client_request"
                  and r.get("path") == "/v1/chat/completions"][index]
        body = json.loads(base64.b64decode(target["body_base64"]))
        body["seed"] = 300
        rewrite_wire(target, json.dumps(body, ensure_ascii=False,
                                        separators=(",", ":")).encode())
        save_proxy_rows(self.fixture, rows)
        report = audit_of(self.fixture)
        self.assertFalse(report["transport_capture_checked"])
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("transport_capture_check_failed",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_auxiliary_capture_count_mismatch_is_caught(self):
        """Dropping a captured native call leaves a cloud call unaccounted for."""
        def mutate(rows):
            for row in rows:
                if row.get("kind") == "task_complete":
                    row["snapshot"]["native_calls"] = row["snapshot"]["native_calls"][:0]
        mutate_events(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("unaccounted_auxiliary_call_count",
                      [e["code"] for e in report["invalid_reasons"]])


class JournalNegativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_run(self.tmp.name)

    def assert_code(self, expected):
        found = codes(self.fixture)
        self.assertIn(expected, found, found)

    def test_unclosed_proxy_journal_is_caught(self):
        save_proxy_rows(self.fixture, [r for r in proxy_rows(self.fixture)
                                       if r.get("kind") != "cloud_close"])
        report = audit_of(self.fixture)
        self.assertFalse(report["transport_capture_checked"])
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("transport_capture_check_failed", codes(self.fixture))

    def test_missing_task_complete_is_rejected(self):
        """Dropping task_complete is now caught by the lifecycle gate."""
        def mutate(rows):
            rows.remove(lifecycle_row(rows, "task_complete"))
        mutate_lifecycle(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("missing_or_duplicate_task_complete",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_missing_task_terminated_is_rejected(self):
        def mutate(rows):
            rows.remove(lifecycle_row(rows, "task_terminated"))
        mutate_lifecycle(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("missing_or_duplicate_task_terminated",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_duplicate_identical_task_complete_is_rejected(self):
        def mutate(rows):
            rows.append(deepcopy(lifecycle_row(rows, "task_complete")))
        mutate_lifecycle(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("missing_or_duplicate_task_complete",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_duplicate_conflicting_task_complete_is_rejected(self):
        def mutate(rows):
            clone = deepcopy(lifecycle_row(rows, "task_complete"))
            clone["snapshot"]["native_calls"] = []
            clone["conflicting_duplicate"] = True
            rows.append(clone)
        mutate_lifecycle(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("missing_or_duplicate_task_complete",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_duplicate_task_terminated_is_rejected(self):
        def mutate(rows):
            rows.insert(len(rows) - 1, deepcopy(lifecycle_row(rows, "task_terminated")))
        mutate_lifecycle(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("missing_or_duplicate_task_terminated",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_task_lifecycle_order_changed_is_rejected(self):
        """Swapping terminated and complete keeps the journal legal but breaks order."""
        def mutate(rows):
            terminated = rows.index(lifecycle_row(rows, "task_terminated"))
            completed = rows.index(lifecycle_row(rows, "task_complete"))
            rows[terminated], rows[completed] = rows[completed], rows[terminated]
        mutate_lifecycle(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("task_lifecycle_order_changed",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_wrong_task_in_lifecycle_event_is_rejected(self):
        def mutate(rows):
            lifecycle_row(rows, "task_complete")["task"] = "sub_U000828_5"
        mutate_lifecycle(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("unexpected_task_in_task_complete",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_native_snapshot_rollback_is_rejected(self):
        """A later snapshot shorter than the previous one must not be hidden."""
        def mutate(rows):
            terminated = lifecycle_row(rows, "task_terminated")
            completed = lifecycle_row(rows, "task_complete")
            terminated["snapshot"]["native_calls"] = deepcopy(
                completed["snapshot"]["native_calls"]) + [{"role": "extra_fixture"}]
            completed["snapshot"]["native_calls"] = []
        mutate_lifecycle(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("native_calls_snapshot_rollback_in_task_complete",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_native_snapshot_conflicting_same_length_is_rejected(self):
        """Equal-length but non-prefix snapshots are a history conflict."""
        def mutate(rows):
            terminated = lifecycle_row(rows, "task_terminated")
            completed = lifecycle_row(rows, "task_complete")
            terminated["snapshot"]["native_calls"] = [{"role": "conflicting_fixture"}]
            completed["snapshot"]["native_calls"] = [{"role": "evaluator"}]
        mutate_lifecycle(self.fixture, mutate)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("native_call_history_changed_in_task_complete",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_event_sequence_gap_is_caught(self):
        """The deliberate gap must survive; the real gate code is asserted."""
        path = self.fixture["run_dir"] / "events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[3]["sequence"] = 99
        rewrite_records(path, rows)
        report = audit_of(self.fixture)
        self.assertFalse(report["input_audit_passed"])
        self.assertIn("journal_sequence_gap_or_duplicate",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_proxy_call_outside_run_window_is_caught(self):
        """Shift every proxy call by the same offset so journal order is preserved.

        A single reordered timestamp would trip the journal clock check instead of
        the run-window gate.
        """
        rows = proxy_rows(self.fixture)
        stamps = [utc(row["timestamp"]) for row in rows]
        offset = timedelta(days=2000) - (max(stamps) - min(stamps))
        base = min(stamps) + timedelta(days=2000)
        for row, stamp in zip(rows, stamps):
            row["timestamp"] = (base + (stamp - min(stamps))).isoformat()
        save_proxy_rows(self.fixture, rows)
        report = audit_of(self.fixture)
        self.assertIn("cloud_call_outside_run_window",
                      [e["code"] for e in report["invalid_reasons"]])

    def test_declared_config_path_mismatch_is_caught(self):
        declared = Path(self.tmp.name) / "declared-config.json"
        declared.write_text(json.dumps({"status": "PLAN_ONLY", "key_loaded": False,
                                        "network_called": False, "task_runner_ready": False,
                                        "config": {}}, ensure_ascii=False), encoding="utf-8")
        report = audit_of(self.fixture, config_path=declared)
        self.assertFalse(report["input_audit_passed"])
        self.assertTrue(report["invalid_reasons"])

    def test_config_pin_change_in_plan_and_result_is_caught(self):
        """Change the pin in BOTH files so the config-consistency gate does not
        mask the pinned-limit gate being tested."""
        fixture = build_run(tempfile.mkdtemp())
        def change(value):
            value["config"]["max_user_exchanges"] = 99
        mutate_json(fixture["run_dir"] / "plan.json", change)
        mutate_json(fixture["run_dir"] / "result.json", change)
        self.assertIn("pinned_limit_changed_max_user_exchanges", codes(fixture))

    def test_extra_cloud_chat_call_is_caught(self):
        rows = proxy_rows(self.fixture)
        extra = deepcopy(next(r for r in rows if r.get("kind") == "cloud_summary"))
        save_proxy_rows(self.fixture, rows)
        self.assertNotIn("unaccounted", codes(self.fixture))
        # An unclosed extra client_request is a transport failure, not a silent skip.
        client = deepcopy(next(r for r in rows if r.get("kind") == "client_request"
                               and r.get("path") == "/v1/chat/completions"))
        client["request_id"] = "injected-extra"
        rows = rows + [client]
        save_proxy_rows(self.fixture, rows)
        found = codes(self.fixture)
        self.assertTrue(found, found)


class WrongScopeTests(unittest.TestCase):
    def test_real_connection_journal_is_only_a_wrong_scope_negative(self):
        if not REAL_CONNECTION_JOURNAL.is_file():
            self.skipTest("transferred connection journal is not present")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fixture = build_run(tmp.name)
        report = audit_cloud_inputs(fixture["run_dir"], REAL_CONNECTION_JOURNAL)
        self.assertFalse(report["input_audit_passed"])
        # The real synthetic-connection journal is a wrong-scope/wrong-version
        # negative: it was captured by an earlier proxy source version, so the
        # accurate first failure is the transport gate (source mismatch), before
        # any config or agent gate. It is never used to claim an agent-level gate.
        self.assertFalse(report["transport_capture_checked"])
        self.assertIn("proxy_source_changed_use_matching_version",
                      report["transport"]["issues"])
        self.assertIn("transport_capture_check_failed",
                      [entry["code"] for entry in report["invalid_reasons"]])


if __name__ == "__main__":
    unittest.main()


PACED_CAPABILITY = ROOT / "configs/ae-01__capability__siliconflow.pacing-candidate.json"
PACED_TRANSPORT = (ROOT / "configs/ae-01__cloud-transport__siliconflow"
                   ".capability-pacing-candidate.json")


class _FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PacedCandidateTests(unittest.TestCase):
    """The full paced candidate path must pass audit_cloud_inputs end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_paced_candidate_full_input_audit_passes(self):
        clock = _FakeClock()
        fixture = build_run(self.tmp.name, capability_config=PACED_CAPABILITY,
                            transport_config=PACED_TRANSPORT, proxy_clock=clock)
        report = audit_cloud_inputs(fixture["run_dir"], fixture["journal"],
                                    config_path=PACED_CAPABILITY)
        self.assertEqual(report["invalid_reasons"], [])
        self.assertTrue(report["transport_capture_checked"])
        self.assertTrue(report["input_audit_passed"])
        self.assertEqual(report["status"], "VALID")
        self.assertTrue(clock.sleeps)  # pacing ran, without a real 65s wait
        marks = [(json.loads(line)) for line in fixture["journal"].read_text().splitlines()]
        paced = [r for r in marks if r.get("kind") == "pace_wait"]
        self.assertTrue(paced)
        self.assertEqual({r["interval_seconds"] for r in paced}, {65.0})

    def test_paced_candidate_payload_matches_the_old_candidate(self):
        old = build_run(tempfile.mkdtemp())
        paced = build_run(tempfile.mkdtemp(), capability_config=PACED_CAPABILITY,
                          transport_config=PACED_TRANSPORT, proxy_clock=_FakeClock())
        old_plan = json.loads((old["run_dir"] / "plan.json").read_text())
        paced_plan = json.loads((paced["run_dir"] / "plan.json").read_text())
        # Only the reviewed run I/O timeout and the pacing marker differ.
        self.assertEqual({k: v for k, v in paced_plan["config"].items()
                          if k not in {"timeout_seconds", "pacing"}},
                         {k: v for k, v in old_plan["config"].items()
                          if k not in {"timeout_seconds", "pacing"}})
        self.assertEqual(paced_plan["config"]["timeout_seconds"], 900)
        payload = dict(paced_plan["agent_payload"])
        old_payload = dict(old_plan["agent_payload"])
        payload.pop("name"), old_payload.pop("name")
        self.assertEqual(payload, old_payload)

    def test_wrong_pacing_combination_is_rejected(self):
        config = json.loads(PACED_CAPABILITY.read_text())
        for mutate in (lambda c: c.__setitem__("timeout_seconds", 600),
                       lambda c: c["pacing"].__setitem__("proxy_upstream_io_timeout_seconds", 900),
                       lambda c: c["pacing"].__setitem__("min_interval_seconds", 30),
                       lambda c: c.pop("pacing")):
            with self.subTest(mutate=mutate):
                bad = json.loads(json.dumps(config))
                mutate(bad)
                with self.assertRaises(ValueError):
                    cloud_config_dict(bad)

    def test_changed_scientific_parameter_is_still_rejected(self):
        config = json.loads(PACED_CAPABILITY.read_text())
        config["max_output_tokens"] = 4096
        with self.assertRaises(ValueError):
            cloud_config_dict(config)


def cloud_config_dict(value):
    from ae_cloud_task import CloudExecutionProfile
    return CloudExecutionProfile("capability").validate(value)


class ToolWireTransformTests(unittest.TestCase):
    """Pinned Letta OpenAI-history transforms, with native-generated wrappers."""

    FULL_ID = "01a0933a89bc07340fb45faa2a1f8c9d"[:32]

    def setUp(self):
        if not LETTA_SYSTEM_PY.is_file():
            self.skipTest(f"pinned Letta source not present: {LETTA_SYSTEM_PY}")
        self.audit = CloudInputAudit.__new__(CloudInputAudit)
        self.audit.known_tool_ids = {self.FULL_ID}

    def wrapper(self, text, *, success=True):
        return native_package_function_response()(success, text, None)

    def history(self, content, tool_call_id=None):
        return {"role": "tool", "content": content,
                "tool_call_id": tool_call_id or self.FULL_ID[:TOOL_CALL_ID_MAX_LEN]}

    def test_native_wrapper_success_empty_is_accepted(self):
        returned = {"status": "success", "tool_return": "", "tool_call_id": self.FULL_ID}
        self.audit.check_submitted_tool_return(self.history(self.wrapper("")),
                                               {"returned": returned}, source="history")

    def test_native_wrapper_error_is_accepted(self):
        returned = {"status": "error",
                    "tool_return": '{"error": "P1001 not found", "applied": false}',
                    "tool_call_id": self.FULL_ID}
        self.audit.check_submitted_tool_return(
            self.history(self.wrapper(returned["tool_return"], success=False)),
            {"returned": returned}, source="history")

    def test_long_id_is_mapped_only_on_the_history_wire(self):
        full = self.FULL_ID
        returned = {"status": "success", "tool_return": "ok", "tool_call_id": full}
        # Letta submission body keeps the exact native id.
        self.audit.check_submitted_tool_return(
            {"type": "tool", "tool_call_id": full, "status": "success", "tool_return": "ok"},
            {"returned": returned}, source="submission")
        # OpenAI history wire carries the 29-char truncation.
        self.audit.check_submitted_tool_return(self.history(self.wrapper("ok")),
                                               {"returned": returned}, source="history")
        self.assertNotEqual(full, full[:TOOL_CALL_ID_MAX_LEN])

    def test_untruncated_id_on_the_history_wire_is_rejected(self):
        returned = {"status": "success", "tool_return": "ok", "tool_call_id": self.FULL_ID}
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(
                self.history(self.wrapper("ok"), tool_call_id=self.FULL_ID),
                {"returned": returned}, source="history")
        self.assertEqual(str(caught.exception), "tool_id_changed")

    def test_truncation_collision_is_rejected(self):
        other = self.FULL_ID[:TOOL_CALL_ID_MAX_LEN] + "zzz"
        self.audit.known_tool_ids = {self.FULL_ID, other}
        with self.assertRaises(AuditFailure) as caught:
            self.audit.wire_tool_call_id(self.FULL_ID)
        self.assertEqual(str(caught.exception), "tool_call_id_truncation_collision")

    def test_wrong_wrapper_status_is_rejected(self):
        returned = {"status": "success", "tool_return": "ok", "tool_call_id": self.FULL_ID}
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(
                self.history(self.wrapper("ok", success=False)),
                {"returned": returned}, source="history")
        self.assertEqual(str(caught.exception), "tool_return_wrapper_status_changed")

    def test_error_must_map_to_failed(self):
        returned = {"status": "error", "tool_return": "boom", "tool_call_id": self.FULL_ID}
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(
                self.history(self.wrapper("boom", success=True)),
                {"returned": returned}, source="history")
        self.assertEqual(str(caught.exception), "tool_return_wrapper_status_changed")

    def test_changed_wrapper_message_is_rejected(self):
        returned = {"status": "success", "tool_return": "ok", "tool_call_id": self.FULL_ID}
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(self.history(self.wrapper("tampered")),
                                                   {"returned": returned}, source="history")
        self.assertEqual(str(caught.exception), "tool_return_content_changed")

    def test_raw_string_instead_of_wrapper_is_rejected(self):
        returned = {"status": "success", "tool_return": "ok", "tool_call_id": self.FULL_ID}
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(self.history("ok"),
                                                   {"returned": returned}, source="history")
        self.assertEqual(str(caught.exception), "tool_return_wrapper_shape_changed")

    def test_extra_wrapper_key_is_rejected(self):
        wrapper = json.loads(self.wrapper("ok"))
        wrapper["extra"] = 1
        returned = {"status": "success", "tool_return": "ok", "tool_call_id": self.FULL_ID}
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(self.history(json.dumps(wrapper)),
                                                   {"returned": returned}, source="history")
        self.assertEqual(str(caught.exception), "tool_return_wrapper_shape_changed")

    def test_history_message_with_submission_fields_is_rejected(self):
        """Submission fields mixed into a history message cannot pick the branch."""
        returned = {"status": "success", "tool_return": "ok", "tool_call_id": self.FULL_ID}
        mixed = self.history(self.wrapper("tampered"))
        mixed.update({"status": "success", "tool_return": "ok"})
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(mixed, {"returned": returned},
                                                   source="history")
        self.assertEqual(str(caught.exception), "unexpected_history_tool_fields")

    def test_matching_history_content_with_submission_fields_is_still_rejected(self):
        returned = {"status": "success", "tool_return": "ok", "tool_call_id": self.FULL_ID}
        mixed = self.history(self.wrapper("ok"))
        mixed.update({"status": "success", "tool_return": "ok"})
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(mixed, {"returned": returned},
                                                   source="history")
        self.assertEqual(str(caught.exception), "unexpected_history_tool_fields")

    def test_native_wrong_status_is_rejected(self):
        bogus = {"type": "tool", "tool_call_id": self.FULL_ID, "tool_return": "ok",
                 "status": "ok"}
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(
                bogus, {"returned": {"status": "ok", "tool_return": "ok",
                                     "tool_call_id": self.FULL_ID}}, source="submission")
        self.assertEqual(str(caught.exception), "unexpected_native_tool_status")

    def test_submission_extra_field_is_rejected(self):
        extra = {"type": "tool", "tool_call_id": self.FULL_ID, "tool_return": "ok",
                 "status": "success", "content": self.wrapper("ok")}
        with self.assertRaises(AuditFailure) as caught:
            self.audit.check_submitted_tool_return(
                extra, {"returned": {"status": "success", "tool_return": "ok",
                                     "tool_call_id": self.FULL_ID}}, source="submission")
        self.assertEqual(str(caught.exception), "unexpected_submission_tool_fields")


class AssistantContentTransformTests(unittest.TestCase):
    """Provider `content=""` with tool calls becomes `content=None` in history."""

    FULL_ID = "01a0933a89bc07340fb45faa2a1f8c9d"[:32]

    def setUp(self):
        self.audit = CloudInputAudit.__new__(CloudInputAudit)
        self.audit.known_tool_ids = {self.FULL_ID}

    def call(self, wire=True):
        return {"id": self.FULL_ID[:TOOL_CALL_ID_MAX_LEN] if wire else self.FULL_ID,
                "type": "function", "function": {"name": "read", "arguments": "{}"}}

    def test_empty_text_with_tool_calls_may_render_as_null(self):
        expected = {"role": "assistant", "content": "", "tool_calls": [self.call(wire=False)]}
        observed = {"role": "assistant", "content": None, "tool_calls": [self.call(wire=True)]}
        self.assertTrue(self.audit.assistant_history_matches(observed, expected))

    def test_non_empty_text_change_is_rejected(self):
        expected = {"role": "assistant", "content": "real answer",
                    "tool_calls": [self.call(wire=False)]}
        observed = {"role": "assistant", "content": None, "tool_calls": [self.call(wire=True)]}
        self.assertFalse(self.audit.assistant_history_matches(observed, expected))

    def test_empty_to_null_without_tool_calls_is_rejected(self):
        expected = {"role": "assistant", "content": ""}
        observed = {"role": "assistant", "content": None}
        self.assertFalse(self.audit.assistant_history_matches(observed, expected))

    def test_whitespace_is_not_treated_as_empty(self):
        expected = {"role": "assistant", "content": " ", "tool_calls": [self.call(wire=False)]}
        observed = {"role": "assistant", "content": None, "tool_calls": [self.call(wire=True)]}
        self.assertFalse(self.audit.assistant_history_matches(observed, expected))

    def test_changed_tool_call_arguments_are_rejected(self):
        expected = {"role": "assistant", "content": "", "tool_calls": [self.call(wire=False)]}
        changed = {"id": self.FULL_ID[:TOOL_CALL_ID_MAX_LEN], "type": "function",
                   "function": {"name": "read", "arguments": '{"x": 1}'}}
        observed = {"role": "assistant", "content": None, "tool_calls": [changed]}
        self.assertFalse(self.audit.assistant_history_matches(observed, expected))


class RealT4ToolWireFragmentTests(unittest.TestCase):
    """The transferred real cloud t4 fragment, read for tool ids and wrappers only."""

    def test_real_fragment_ids_and_wrapper_are_accepted(self):
        if not REAL_T4_JOURNAL.is_file():
            self.skipTest("transferred cloud t4 fragment is not present")
        full_ids, wire_ids, wrappers = set(), set(), []
        for line in REAL_T4_JOURNAL.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("kind") == "upstream_response":
                body = json.loads(base64.b64decode(record["body_base64"]))
                for choice in body.get("choices") or []:
                    for tool_call in ((choice.get("message") or {}).get("tool_calls") or []):
                        full_ids.add(tool_call["id"])
            elif record.get("kind") == "normalized_request":
                body = json.loads(base64.b64decode(record["body_base64"]))
                for message in body.get("messages") or []:
                    if message.get("role") == "assistant":
                        for tool_call in message.get("tool_calls") or []:
                            wire_ids.add(tool_call["id"])
                    elif message.get("role") == "tool":
                        wire_ids.add(message.get("tool_call_id"))
                        wrappers.append(json.loads(message["content"]))
        self.assertTrue(full_ids and wire_ids and wrappers)
        self.assertTrue(all(len(value) == 32 for value in full_ids))
        self.assertTrue(all(len(value) == TOOL_CALL_ID_MAX_LEN for value in wire_ids))
        audit = CloudInputAudit.__new__(CloudInputAudit)
        audit.known_tool_ids = set(full_ids)
        mapped = {audit.wire_tool_call_id(full) for full in full_ids}
        # Every wire id must be the fixed-length prefix of a real provider id,
        # and this fragment must actually exercise the truncation mapping.
        self.assertTrue(wire_ids <= mapped)
        self.assertTrue(wire_ids & mapped)
        for wrapper in wrappers:
            self.assertEqual(set(wrapper), {"status", "message", "time"})
            self.assertIn(wrapper["status"], {"OK", "Failed"})
            self.assertIsInstance(wrapper["time"], str)


if __name__ == "__main__":
    unittest.main()
