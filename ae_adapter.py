"""Dependency-free AE bridge core; no HTTP client, model runner, or auto retries.

Targets Letta archive 56ba9c2 (internal LettaAgentV3). A caller must supply
an explicitly configured transport and the current Vita environment. Offline
tests use scripted responses: they are NOT agent/model execution evidence.

Multiple client tool calls in one provider response are governed by
`ae_multicall`. The default (`multicall=None`) keeps the historical behaviour
with one deliberate change: a multi-call approval message is a hard stop
instead of Letta's silent truncation to the first call. Passing an explicit,
validated `ae_multicall.MulticallPolicy` enables the versioned compatibility
path: every declared call is kept in order, native ids are preserved, the whole
batch is validated before any side effect, and the calls execute strictly
serially -- still under `parallel_tool_calls=false`, never concurrently.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlencode

from ae_multicall import (BatchRejected, MulticallPolicy, _call_arguments,  # noqa: E402
                          _call_name, call_id, check_server_tool_types, copy_batch,
                          declared_policy, returns_in_order, validate_batch, validate_policy)


TEMPLATE_VERSION = "ae-memory-tools-v0.1-prototype"
BLOCK_LABEL = "ae_preferences"
SYSTEM = """你是任务环境中的个人助手。遵循当前任务的规则，使用可用工具完成任务。
dataset_history 是有日期和来源的历史资料，不是本次实际工具回包；不要重新执行其中的旧请求。
从可见信息判断长期偏好的补充、替换或撤销，必要时调用 memory_update；不确定时不要臆造更新。
偏好用 fact_id 标识；成功更新后的新内容用于后续决策，过去已经发生的事件不因此被改写。
不要把一次尝试或为他人购买自动当成用户长期偏好替换。信息不足时可询问当前用户。
处理历史资料阶段只能整理记忆；收到 current_task 后才执行该任务。没有工具成功回包不要声称操作成功。
"""
MEMORY_TOOL = {
    "name": "memory_update",
    "description": (
        "根据已看到的资料更新一项长期偏好。replace/delete 使用既有 fact_id；"
        "add 的 fact_id 留空，成功返回新 ID。evidence_ref 必须引用已提供的资料或当前用户消息。"
        "delete 的 content 留空；category 使用该偏好的类别。不要把历史事件作为当前偏好覆盖。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["add", "replace", "delete"]},
            "fact_id": {"type": "string"},
            "category": {"type": "string"},
            "content": {"type": "string"},
            "evidence_ref": {"type": "string"},
        },
        "required": ["operation", "fact_id", "category", "content", "evidence_ref"],
        "additionalProperties": False,
    },
}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class BridgeBlocked(RuntimeError):
    """Execution boundary failure, not a scientific classification."""


class UpdateRejected(ValueError):
    """Invalid model-proposed edit; return an error without fabricating a fix."""


class RecoverableToolError(ValueError):
    """A trusted READ tool's recoverable argument/lookup failure.

    Raised only by a binding that was classified read-only from native tool
    metadata and only for argument/lookup/precondition errors. The bridge turns
    it into an ordinary `status="error"` tool result carrying the original error
    text, executed exactly once. It is never raised for WRITE/unknown tools or
    for internal/serialization failures, which keep stopping the run.
    """


class VerifiedWritePrecondition(Exception):
    """A WRITE tool's precondition failure proven on the exact fixed call path.

    Raised only by a binding whose environment passed the registered precondition
    proof: the fixed Vita method and helper code objects, the exact missing-order
    raise site, the order id of this very call, and an unchanged native state hash
    (so the supported chain performed no environment write before the raise).

    The bridge returns it to the model once as `Error: <original>` with
    `status="error"`. A plain WRITE `ValueError`, the same text raised from any
    other site, an unproven or fake method/helper, and every other WRITE failure
    still stop the run with `BridgeBlocked`.
    """


class Transport(Protocol):
    def request(self, method: str, path: str, body: dict | None = None) -> dict: ...


class MemoryPolicy:
    """Symmetric logical bookkeeping; only R publishes it back to the block.

    The E ledger is NOT injected/readable as another memory backend. It only
    resolves stable IDs, validates operations, and records proposed writes.
    It never reads gold after initialization or decides if an edit is true.
    """

    def __init__(self, arm: str, facts: dict, *, block_char_limit: int):
        if arm not in {"rewrite", "erratum"} or block_char_limit <= 0:
            raise ValueError("invalid arm or block_char_limit")
        self.arm = arm
        self.facts = deepcopy(facts)
        self.block_char_limit = block_char_limit
        for key, fact in self.facts.items():
            if not key or set(fact) != {"category", "content"}:
                raise ValueError("invalid initial fact")
            if not all(isinstance(v, str) and v and "\x00" not in v for v in fact.values()):
                raise ValueError("initial facts must be nonempty text")
        self.initial_block = dumps(self.facts)
        if len(self.initial_block) > block_char_limit:
            raise ValueError("initial block exceeds character limit; no truncation")
        self.block_text = self.initial_block
        self.next_id = len(self.facts)
        self.writes: list[dict] = []

    def update(self, args: dict, visible_refs: set[str], patch: Callable[[str], None]) -> str:
        if not isinstance(args, dict) or set(args) != set(MEMORY_TOOL["parameters"]["required"]):
            raise UpdateRejected("memory_update requires exactly the declared fields")
        if not all(isinstance(x, str) and "\x00" not in x for x in args.values()):
            raise UpdateRejected("all update arguments must be strings without null bytes")
        op, fid = args["operation"], args["fact_id"]
        category, content = args["category"], args["content"]
        if args["evidence_ref"] not in visible_refs:
            raise UpdateRejected("evidence_ref is not visible; no update was applied")
        if op not in {"add", "replace", "delete"} or not category.strip():
            raise UpdateRejected("invalid operation/category")
        if (op == "delete" and content) or (op != "delete" and not content.strip()):
            raise UpdateRejected("delete needs empty content; other operations need content")
        candidate = deepcopy(self.facts)
        next_id = self.next_id
        if op == "add":
            if fid:
                raise UpdateRejected("add needs an empty fact_id")
            fid = f"p{next_id:03d}"
            while fid in candidate:
                next_id += 1
                fid = f"p{next_id:03d}"
            next_id += 1
        elif fid not in candidate:
            raise UpdateRejected("unknown or deleted fact_id; no update was applied")
        elif candidate[fid]["category"] != category:
            raise UpdateRejected("category does not match this fact_id")
        if op == "delete":
            del candidate[fid]
        else:
            candidate[fid] = {"category": category, "content": content}
        rendered = dumps(candidate)
        # Apply the same logical size bound in BOTH arms, not R-only rejection.
        if len(rendered) > self.block_char_limit:
            raise UpdateRejected("logical memory limit exceeded; no truncation or update")
        resolved = dict(args, fact_id=fid)
        if self.arm == "rewrite":
            patch(rendered)  # Wait for verified PATCH response before committing locally.
            self.block_text = rendered
            result = {"status": "updated", "update": resolved, "memory_block": candidate}
        else:
            correction = (
                "[STATE UPDATE]\n" + dumps(resolved) +
                "\n该更新用于后续当前状态判断，覆盖该 fact_id 的此前默认内容；"
                "delete 表示撤销该偏好，不是否定过去已发生的事件。"
            )
            result = {"status": "appended", "update": resolved, "erratum": correction}
        self.facts = candidate
        self.next_id = next_id
        self.writes.append({"submitted": deepcopy(args), "resolved": resolved,
                            "block_sha": sha(self.block_text)})
        return dumps(result)


def creation_payload(*, name: str, model: str, profile: dict, memory: MemoryPolicy,
                     tags=None) -> dict:
    """Build only; never contacts a server. Model/limits must be chosen by caller.

    `tags` defaults to the empty list the reviewed payloads already carry, so an
    existing candidate's bytes do not move; a caller that needs a declared policy
    tag passes it explicitly.
    """
    if not model or "/" not in model:
        raise ValueError("explicit Letta provider/model handle required")
    if tags is None:
        tags = []
    if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
        raise ValueError("agent tags must be a list of non-empty strings")
    return {
        "name": name, "model": model, "agent_type": "letta_v1_agent",
        "system": SYSTEM + "\n共同起点的用户资料：\n" + dumps(profile),
        "memory_blocks": [{"label": BLOCK_LABEL, "value": memory.initial_block,
                           "description": "带稳定 fact_id 的偏好；以之后成功更新为准。",
                           "limit": memory.block_char_limit}],
        "include_base_tools": False, "include_base_tool_rules": False,
        "include_multi_agent_tools": False,
        "include_default_source": False, "tool_ids": [], "tags": list(tags),
        "enable_sleeptime": False, "message_buffer_autoclear": False,
        "initial_message_sequence": [],
    }


def _declared_type_matches(declared, value):
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "array":
        return isinstance(value, list)
    if declared == "object":
        return isinstance(value, dict)
    if declared == "null":
        return value is None
    return True


def validate_declared_arguments(schema: dict, args: dict):
    """Check model arguments against the tool's own declared JSON schema.

    Returns an error string when the call cannot be the declared contract, else
    None. Only what the tool declares is enforced; a tool that declares no
    properties is left unconstrained. A violation is an invalid model proposal,
    so it is rejected without executing the tool.
    """
    parameters = (schema or {}).get("parameters")
    if not isinstance(parameters, dict):
        return None
    properties = parameters.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    # A closed schema rejects extra arguments even when it declares no
    # properties; otherwise a schema without declared properties is left
    # unconstrained (documented unsupported boundary, not full JSON Schema).
    if parameters.get("additionalProperties") is False:
        for key in args:
            if key not in properties:
                return f"undeclared argument: {key}"
    if not properties:
        return None
    for key in parameters.get("required") or []:
        if key not in args:
            return f"missing required argument: {key}"
    for key, value in args.items():
        if key not in properties:
            return f"undeclared argument: {key}"
        declared = properties[key] if isinstance(properties[key], dict) else {}
        if "type" in declared and not _declared_type_matches(declared["type"], value):
            return f"argument {key} is not of declared type {declared['type']}"
        if isinstance(declared.get("enum"), list) and value not in declared["enum"]:
            return f"argument {key} is outside the declared enum"
    return None


def frozen_environment_call(environment: Any, name: str) -> Callable[..., Any]:
    """Return a callable that dispatches to one frozen native tool name.

    The tool name is closed over by this factory, so model-supplied arguments can
    never redirect the dispatch (for example by passing `_name`).
    """
    def invoke(**kwargs):
        return environment.make_tool_call(tool_name=name, requestor="assistant", **kwargs)
    return invoke


@dataclass
class ToolBinding:
    schema: dict
    call: Callable[..., Any]
    owner: Any = None
    # Set only from native tool-type metadata (never from the tool name); a
    # read-only binding may surface recoverable argument errors to the model.
    read_only: bool = False
    # Set only by the registered WRITE-precondition proof (fixed code identity and
    # native classification). It authorizes the binding to raise
    # `VerifiedWritePrecondition`; the bridge refuses that proof from any other
    # binding, so a stand-in binding cannot declare the failure recoverable.
    write_precondition: bool = False


def vita_bindings(environment: Any) -> dict[str, ToolBinding]:
    """Bind the actual environment.get_tools()/make_tool_call interface.

    No Vita import/install is needed here. The caller owns a separate current
    environment per arm; it must NEVER pass gold fields as tool schemas. The
    dispatch target is frozen per tool, so model arguments cannot override it.
    """
    result = {}
    for tool in environment.get_tools():
        schema = deepcopy(tool.openai_schema["function"])
        name = schema["name"]
        if name == MEMORY_TOOL["name"] or name in result:
            raise ValueError("duplicate/reserved environment tool")
        result[name] = ToolBinding(schema, frozen_environment_call(environment, name),
                                   owner=environment)
    return result


class LettaBridge:
    """Synchronous client-tool exchange. No model/user simulator is supplied.

    A supplied transport can be live only after separate run authorization.
    The current CLI supplies NO live transport. A completed exchange is merely
    end_turn, never task success. Trace records are retained on every failure.
    """

    def __init__(self, transport: Transport, agent_id: str, memory: MemoryPolicy, *,
                 max_rounds: int, max_steps: int, include_memory_tool: bool = True,
                 multicall: MulticallPolicy | None = None):
        if min(max_rounds, max_steps) <= 0 or not agent_id:
            raise ValueError("explicit positive execution limits and agent ID required")
        if not isinstance(include_memory_tool, bool):
            raise ValueError("include_memory_tool must be an explicit bool")
        if multicall is not None:
            # Refuse an unreviewed policy object or a weakened field: the
            # compatibility path may only run under the exact declared profile.
            validate_policy(multicall.as_dict())
        self.multicall = multicall
        self.include_memory_tool = include_memory_tool
        self.transport, self.agent_id, self.memory = transport, agent_id, memory
        self.path = "/v1/agents/" + quote(agent_id, safe="")
        self.max_rounds, self.max_steps = max_rounds, max_steps
        self.trace: list[dict] = []
        self.block_id: str | None = None
        self.message_ids: list[str] = []
        self.visible_refs: set[str] = set()
        self.seen_calls: set[str] = set()
        #: One evidence record per approval batch, in execution order. Proves
        #: which calls were retained, in what order, under which id rule.
        self.multicall_batches: list[dict] = []
        self.last_turn = 3
        self.blocked = False
        self.current_bindings: dict[str, ToolBinding] = {}
        self.runtime_user_count = 0
        #: The ACTIVE stage clarification for THIS agent, set by the driver immediately
        #: before one stage runs and cleared when it ends. It is per-agent state on purpose:
        #: two arms run against two bridges, so a clock can never be read from a global
        #: "current stage" and applied to the wrong arm.
        self.stage_clarification: "dict | None" = None

    def _fail(self, message: str) -> None:
        self.blocked = True
        self.trace.append({"kind": "bridge_blocked", "reason": message})
        raise BridgeBlocked(message)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        self.trace.append({"kind": "request", "method": method, "path": path,
                           "body": deepcopy(body)})
        try:
            response = self.transport.request(method, path, deepcopy(body))
        except Exception as exc:
            self._fail(f"transport outcome uncertain; no retry: {type(exc).__name__}: {exc}")
        self.trace.append({"kind": "response", "body": deepcopy(response)})
        if not isinstance(response, dict):
            self._fail("transport returned a non-object")
        return response

    def verify_session(self, *, allow_pending: bool = False) -> None:
        if self.blocked:
            raise BridgeBlocked("session is blocked; inspect trace before any recovery")
        includes = ["agent.blocks", "agent.tools", "agent.sources", "agent.tags",
                    "agent.managed_group", "agent.pending_approval"]
        state = self._request("GET", self.path + "?" + urlencode([("include", x) for x in includes]))
        required = {"id", "blocks", "tools", "sources", "tags", "message_ids",
                    "message_buffer_autoclear", "enable_sleeptime", "agent_type",
                    "managed_group", "pending_approval"}
        if not required <= state.keys():
            self._fail("agent inspection is incomplete; absent relations are not empty")
        if state["pending_approval"] is not None and not allow_pending:
            self._fail("unexpected pending approval before new user input; do not resume an unknown call")
        if (state["id"] != self.agent_id or state["agent_type"] != "letta_v1_agent"
                or state["tools"] != [] or state["sources"] != []
                or state["message_buffer_autoclear"] is not False
                or state["enable_sleeptime"] not in (False, None)
                or "git-memory-enabled" in state["tags"]
                or state.get("managed_group") or state.get("multi_agent_group")
                or state.get("memory", {}).get("git_enabled")
                or state.get("memory", {}).get("file_blocks")):
            self._fail("unexpected agent type, memory channel, or autoclear setting")
        blocks = state["blocks"]
        if (not isinstance(blocks, list) or len(blocks) != 1
                or blocks[0].get("label") != BLOCK_LABEL
                or blocks[0].get("value") != self.memory.block_text
                or not blocks[0].get("id")):
            self._fail("unexpected block set or content")
        bid = blocks[0]["id"]
        if self.block_id is not None and bid != self.block_id:
            self._fail("block identity changed")
        self.block_id = bid
        ids = state["message_ids"]
        if not isinstance(ids, list) or not ids:
            self._fail("in-context message IDs unavailable")
        # System ID can change after R's intentional PATCH. Other IDs must be
        # retained in order; deleting/resetting history is not append-only.
        prior = self.message_ids[1:]
        if ids[1:1 + len(prior)] != prior:
            self._fail("in-context history was reset/replaced/compacted")
        self.message_ids = list(ids)

    def _patch(self, value: str) -> None:
        response = self._request("PATCH", self.path + "/core-memory/blocks/" + BLOCK_LABEL,
                                 {"value": value})
        if response.get("id") != self.block_id or response.get("value") != value:
            self._fail("PATCH did not confirm the expected block/value; no success return")

    def _execute(self, tool: dict, bindings: dict[str, ToolBinding]) -> dict:
        fid, name, encoded = tool.get("tool_call_id"), tool.get("name"), tool.get("arguments")
        if not isinstance(fid, str) or not fid or fid in self.seen_calls:
            self._fail("missing/repeated tool_call_id; refusing duplicate execution")
        self.seen_calls.add(fid)
        status = "success"
        try:
            if not isinstance(encoded, str):
                raise UpdateRejected("nonstream tool arguments must be a JSON string")
            args = json.loads(encoded)
            if not isinstance(args, dict):
                raise UpdateRejected("tool arguments must be a JSON object")
            if name == MEMORY_TOOL["name"]:
                if not self.include_memory_tool:
                    # Capability probe: the schema omits this tool; refuse any
                    # pending call without executing it or mutating memory.
                    raise UpdateRejected("memory_update is disabled for this agent")
                returned = self.memory.update(args, self.visible_refs, self._patch)
            elif name in bindings:
                # Contract check before any execution: an undeclared, missing,
                # mistyped or out-of-enum argument is an invalid proposal and is
                # answered with an error result, never dispatched.
                declared_error = validate_declared_arguments(bindings[name].schema, args)
                if declared_error is not None:
                    raise UpdateRejected(declared_error)
                try:
                    returned = bindings[name].call(**args)
                except RecoverableToolError as exc:
                    # Only a binding explicitly classified read-only may surface
                    # this; a WRITE/unknown binding raising the same exception
                    # still stops. The text follows the native get_response form
                    # (`Error: <original>`) with no extra business assertion.
                    if not bindings[name].read_only:
                        self._fail("recoverable tool error on a non read-only binding")
                    returned = "Error: " + str(exc)
                    status = "error"
                except VerifiedWritePrecondition as exc:
                    # A dedicated proof, produced by a binding whose environment
                    # passed the registered WRITE-precondition check. It is never
                    # a general WRITE rule: an unproven binding cannot raise it and
                    # every other WRITE exception still reaches the uncertain-outcome
                    # stop below.
                    if not bindings[name].write_precondition:
                        self._fail("verified precondition on an undeclared binding")
                    returned = "Error: " + str(exc)
                    status = "error"
                except Exception as exc:
                    # A tool can mutate then throw. Never turn an uncertain
                    # side-effect into a retryable ordinary tool error.
                    self._fail(f"environment tool outcome uncertain: {type(exc).__name__}: {exc}")
                else:
                    if not isinstance(returned, str):
                        returned = dumps(returned)
            else:
                raise UpdateRejected("tool not available in this phase")
        except (UpdateRejected, json.JSONDecodeError) as exc:
            returned, status = dumps({"error": str(exc), "applied": False}), "error"
        result = {"type": "tool", "tool_call_id": fid, "tool_return": returned, "status": status}
        self.trace.append({"kind": "client_tool_result", "call": deepcopy(tool),
                           "result": deepcopy(result), "block_sha": sha(self.memory.block_text)})
        return result

    def _resolve_batch(self, payload: dict, bindings: dict[str, ToolBinding],
                       declared_tools: list[dict]):
        """Turn one approval payload into the calls this bridge may execute.

        Returns ``(calls, names, evidence, batch)``. ``batch`` is None when an
        empty batch is the only valid reading (no pending calls at all).

        Two modes, one rule: no call is ever silently dropped.
        - compatibility mode (an explicit policy was declared): the complete
          batch is validated BEFORE any side effect and returned in order;
        - protection mode (no policy): more than one pending call is a hard
          stop, because executing only the first is the defect being fixed.
        """
        if self.multicall is None:
            single = payload.get("tool_calls")
            shape = "tool_calls"
            if single is None:
                single = payload.get("tool_call")
                shape = "tool_call"
            if isinstance(single, list) and len(single) > 1:
                self._fail(
                    f"provider returned {len(single)} tool calls while the multicall "
                    "compatibility policy is not declared; refusing to truncate or "
                    "execute a partial batch")
            if single is None:
                return [], [], None, None
            if not isinstance(single, list) and not isinstance(single, dict):
                self._fail("incomplete/streamed pending tool calls")
            # A real Letta approval already carries `tool_call_id` and is passed
            # through byte-identical, so no wire capture changes. Only an
            # OpenAI-shaped fixture is rewritten into the executor's spelling.
            try:
                raw_calls = single if isinstance(single, list) else [single]
                calls = [dict(raw) if isinstance(raw.get("tool_call_id"), str)
                         and raw.get("tool_call_id")
                         else {"type": "tool", "tool_call_id": call_id(raw),
                               "name": _call_name(raw), "arguments": _call_arguments(raw)}
                         for raw in raw_calls]
            except BatchRejected as exc:
                self._fail(f"invalid pending tool call ({exc.code}); nothing executed")
            return calls, [_call_name(call) for call in calls], None, None
        check_server_tool_types([tool.get("tool_type") for tool in declared_tools])
        try:
            batch = validate_batch(payload, bindings,
                                   known_ids=self.seen_calls,
                                   memory_tool_name=MEMORY_TOOL["name"]
                                   if self.include_memory_tool else None)
        except BatchRejected as exc:
            # Whole batch refused before any side effect: no partial prefix.
            self._fail(f"multicall batch rejected ({exc.code}); nothing executed")
        calls = [dict(call) for call in batch.calls]
        evidence = batch.as_evidence()
        evidence["policy"] = declared_policy()
        evidence["payload_shape"] = batch.shape
        return calls, list(batch.names), evidence, batch

    def exchange(self, messages: list[dict], bindings: dict[str, ToolBinding]) -> list[dict]:
        if self.blocked:
            raise BridgeBlocked("session is blocked")
        # Refuse caller-injected tool returns: only genuine pending calls below
        # can produce ToolReturnCreate. This public entry accepts user input.
        if not messages or any(m.get("role") != "user" or m.get("type", "message") != "message"
                               for m in messages):
            self._fail("exchange accepts user messages, not fabricated tool returns")
        for name, binding in bindings.items():
            if name == MEMORY_TOOL["name"] or binding.schema.get("name") != name:
                self._fail("invalid/reserved binding name")
        self.verify_session()
        client_tools = ([deepcopy(MEMORY_TOOL)] if self.include_memory_tool else []) + \
            [deepcopy(b.schema) for b in bindings.values()]
        # This agent's OWN active clarification, applied per request and never globally.
        client_tools = self._clarified_client_tools(client_tools)
        assistant = []
        for _ in range(self.max_rounds):
            response = self._request("POST", self.path + "/messages", {
                "messages": messages, "client_tools": client_tools,
                "max_steps": self.max_steps, "include_compaction_messages": True,
            })
            output = response.get("messages")
            if not isinstance(output, list):
                self._fail("missing nonstream messages")
            if any(m.get("message_type") == "summary_message" or
                   (m.get("message_type") == "event_message" and "compaction" in dumps(m)) for m in output):
                self._fail("compaction observed; stop without silently changing the setting")
            # Archive V3's nonstream conversion can disguise a stored summary
            # as user_message. Allow only verbatim echoes of this POST's input;
            # otherwise stop for inspection, not a scientific failure verdict.
            submitted_user_text = [m.get("content") for m in messages if m.get("role") == "user"]
            if any(m.get("message_type") == "user_message" and
                   m.get("content") not in submitted_user_text for m in output):
                self._fail("unexpected user-message output; inspect possible summary conversion")
            if any("[truncated " in dumps(m) for m in output):
                self._fail("truncation marker observed; actual input requires inspection")
            self.verify_session(allow_pending=True)
            assistant.extend(deepcopy(m) for m in output if m.get("message_type") == "assistant_message")
            reason = response.get("stop_reason", {}).get("stop_reason")
            approvals = [m for m in output if m.get("message_type") == "approval_request_message"]
            if reason == "end_turn" and not approvals:
                return assistant
            if reason != "requires_approval" or len(approvals) != 1:
                self._fail(f"exchange did not end normally: {reason!r}")
            calls, names, evidence, batch = self._resolve_batch(approvals[0], bindings, client_tools)
            if batch is None:
                # Legacy protection path: the already-validated per-call gate in
                # `_execute` owns id uniqueness; nothing else is widened here.
                returns = [self._execute(c, bindings) for c in calls]
                messages = [{"type": "tool_return", "tool_returns": returns}]
                continue
            # Serial execution, in the provider's original order. Each call sees
            # the state committed by the previous one; `_execute` confirms R's
            # PATCH before the next item starts. Never asyncio.gather.
            returns = [self._execute(c, bindings) for c in calls]
            try:
                returns = returns_in_order(copy_batch(batch), returns)
            except BatchRejected as exc:
                # A missing or mismatched return is an execution-boundary
                # failure, not a scientific failure verdict.
                self._fail(f"tool returns do not match the executed batch ({exc.code})")
            evidence["returns"] = [{"tool_call_id": r["tool_call_id"], "status": r["status"]}
                                   for r in returns]
            evidence["executed_count"] = len(returns)
            evidence["patches_during_batch"] = sum(
                1 for step in self.trace
                if step.get("kind") == "request" and step.get("method") == "PATCH")
            self.multicall_batches.append(evidence)
            self.trace.append({"kind": "multicall_batch", "batch": deepcopy(evidence),
                               "calls": deepcopy(calls), "names": list(names)})
            messages = [{"type": "tool_return", "tool_returns": returns}]
        self._fail("client exchange round limit reached; not task success")

    def set_stage_clarification(self, clarification: "dict | None") -> None:
        """Bind (or clear) the clarification that applies to the NEXT stage of THIS agent.

        A blocked session refuses a NEW clarification - it must not start another stage.
        CLEARING one is always allowed: that is the end-of-stage bookkeeping the driver does
        in its `finally`, and after a refused request it must not replace the real refusal
        with a bookkeeping error.
        """
        if self.blocked and clarification:
            self._fail("session is blocked; refuse a clarification change")
        self.stage_clarification = deepcopy(clarification) if clarification else None

    def _clarified_client_tools(self, client_tools: list) -> list:
        """Apply this stage's attributes note to the tool schemas Letta receives.

        Only the DECLARED tool's parameter description changes, in a copy: the agent's own
        tool registration, its memory block, the tool return guard and the native execution
        are untouched, and a stage without that tool is left exactly as it was.
        """
        clarification = self.stage_clarification or {}
        note = clarification.get("attributes_clarification")
        if not note:
            return client_tools
        changed = []
        for tool in client_tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            parameters = (function or {}).get("parameters")
            properties = (parameters or {}).get("properties")
            if (isinstance(function, dict)
                    and function.get("name") == clarification.get("attributes_tool")
                    and isinstance(properties, dict) and isinstance(
                        properties.get("attributes"), dict)):
                tool = deepcopy(tool)
                attributes = tool["function"]["parameters"]["properties"]["attributes"]
                original = attributes.get("description")
                if not (isinstance(original, str) and note in original):
                    attributes["description"] = ((original + "\n\n")
                                                 if isinstance(original, str)
                                                 and original.strip() else "") + note
            changed.append(tool)
        return changed

    def run_stage(self, task: dict, *, bindings: dict[str, ToolBinding], domain_policy: str,
                  clarification: "dict | None" = None) -> dict:
        from ae_inputs import history_message
        if task["number"] != self.last_turn + 1:
            self._fail("tasks must be contiguous from t4; do not reset at a later gold state")
        # Do not accept a whole raw dataset task: it contains gold and a complete
        # environment. Whitelist the public projection from ae_inputs.
        fields = {"number", "subtask_id", "domain", "current_time", "instruction", "history"}
        if set(task) != fields:
            self._fail("run_stage requires the public task projection only")
        refs = {item["ref"] for item in task["history"]}
        if refs & self.visible_refs or len(refs) != len(task["history"]):
            self._fail("history batch already consumed or duplicate evidence refs")
        self.visible_refs.update(refs)
        history_reply = self.exchange([history_message(task)], {})
        current = {"source": "current_task", "subtask_id": task["subtask_id"],
                   "domain": task["domain"], "current_time": task["current_time"],
                   "domain_policy": domain_policy, "instruction": task["instruction"]}
        ref = f"t{task['number']}/user/0"
        self.visible_refs.add(ref)
        current["ref"] = ref
        if clarification is not None:
            # The clock is added to THIS stage's payload, so it travels with the request
            # that belongs to this agent, this task and this turn - and so it is recorded
            # in the public envelope rather than written into the agent's memory.
            hours = clarification.get("environment_now")
            if not isinstance(hours, str) or not hours:
                self._fail("a declared stage clock is not a nonempty string")
            current["environment_now"] = hours
            current["environment_clock_format"] = clarification.get("clock_format")
        reply = self.exchange([{"role": "user", "content": dumps(current)}], bindings)
        self.current_bindings = dict(bindings)
        self.last_turn = task["number"]
        self.runtime_user_count = 0
        return {"history_reply": history_reply, "assistant_messages": reply,
                "exchange_completed": True, "task_success": None}

    def reply_to_user(self, content: str) -> list[dict]:
        if self.last_turn < 4:
            self._fail("no task is active")
        self.runtime_user_count += 1
        ref = f"t{self.last_turn}/user/{self.runtime_user_count}"
        self.visible_refs.add(ref)
        return self.exchange([{"role": "user", "content": dumps({
            "source": "runtime_user", "ref": ref, "content": content})}], self.current_bindings)


def assert_separate_arms(left: LettaBridge, right: LettaBridge) -> None:
    if (left.agent_id == right.agent_id or left.memory is right.memory
            or left.block_id is None or right.block_id is None or left.block_id == right.block_id
            or left.memory.initial_block != right.memory.initial_block):
        raise BridgeBlocked("arms must have equal initial text but distinct agents, blocks and local memory")
    left_owners = [b.owner for b in left.current_bindings.values() if b.owner is not None]
    right_owners = [b.owner for b in right.current_bindings.values() if b.owner is not None]
    if any(a is b for a in left_owners for b in right_owners):
        raise BridgeBlocked("arms must not share the same mutable Vita environment")
