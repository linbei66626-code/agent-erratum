"""Read-only parser for the sealed single-t4 R/E replay.

Builds one normalised replay model from the frozen capture:

    <data>/ae-cloud-re-pair-t4-watchdog-20260913-r1/{plan,result,events,letta-http}.jsonl/json
    <data>/ae-cloud-re-pair-t4-watchdog-20260913-r1.private.jsonl        (proxy wire)
    <data>/lab-cloud-re-watchdog-run-20260913-r1/input-audit.json        (independent audit)
    <data>/{SHA256SUMS,remote-SHA256SUMS}

Nothing here executes a model or a tool and nothing is written: it only reads the
captured bytes and derives a replay. Every step carries a source reference (file +
line number or JSON pointer); anything that cannot be associated is reported in
`warnings` / `unassociated` instead of being invented.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SCHEMA = "ae-re-replay-1"
NOTICE = "真实记录回放，不执行模型或工具"
RUN_DIR = "ae-cloud-re-pair-t4-watchdog-20260913-r1"
OPS_DIR = "lab-cloud-re-watchdog-run-20260913-r1"
PROXY_FILE = "ae-cloud-re-pair-t4-watchdog-20260913-r1.private.jsonl"
ARM_LABELS = {"rewrite": "R", "erratum": "E"}
ARM_ORDER = ("rewrite", "erratum")
#: Script-capable or control-character content that must never be executed.
UNSAFE_PATTERNS = (re.compile(r"<\s*script", re.I), re.compile(r"javascript\s*:", re.I),
                   re.compile(r"on(?:error|load|click|mouseover)\s*=", re.I))
#: Markup-shaped text (e.g. Letta's `</memory_metadata>` fence). It is rendered as
#: plain text like everything else; the flag is informational only.
MARKUP_PATTERN = re.compile(r"<\s*/?\s*[a-z][a-z0-9]*", re.I)


class ReplayDataError(RuntimeError):
    """The capture is corrupt or structurally unusable; replay refuses to guess."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unsafe(text) -> bool:
    if not isinstance(text, str):
        return False
    if any(ord(character) < 9 for character in text):
        return True
    return any(pattern.search(text) for pattern in UNSAFE_PATTERNS)


def _markup_like(text) -> bool:
    return isinstance(text, str) and bool(MARKUP_PATTERN.search(text))


def _read_jsonl(path: Path):
    """[(line_number, record)] with a hard error on a corrupt line."""
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append((number, json.loads(line)))
        except ValueError as exc:
            raise ReplayDataError(f"{path.name} line {number} is not valid JSON: {exc}") from None
    return rows


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ReplayDataError(f"{path.name} is not valid JSON: {exc}") from None


def _manifest(path: Path):
    entries = {}
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            entries[name.strip()] = digest
    return entries


def _sources(data_dir: Path, used):
    local = _manifest(data_dir / "SHA256SUMS")
    remote = _manifest(data_dir / "remote-SHA256SUMS")
    out = []
    for role, path, lines in used:
        raw = path.read_bytes()
        digest = _sha256(raw)
        relative = path.relative_to(data_dir).as_posix()
        out.append({
            "role": role, "path": relative, "bytes": len(raw), "lines": lines,
            "sha256": digest,
            "local_manifest_sha256": local.get(relative),
            "remote_manifest_sha256": remote.get(relative),
        })
    return out


def _load(data_dir: Path):
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise ReplayDataError(f"data directory is missing: {data_dir}")
    run_dir, ops_dir = data_dir / RUN_DIR, data_dir / OPS_DIR
    paths = {
        "plan.json": run_dir / "plan.json",
        "result.json": run_dir / "result.json",
        "events.jsonl": run_dir / "events.jsonl",
        "letta-http.jsonl": run_dir / "letta-http.jsonl",
        "proxy.jsonl": data_dir / PROXY_FILE,
        "input-audit.json": ops_dir / "input-audit.json",
        "watchdog.json": ops_dir / "watchdog.json",
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise ReplayDataError("the sealed capture is incomplete: " + ", ".join(missing))
    return paths


def _proxy_requests(path: Path, warnings):
    """The 33 chat requests only; the GET /v1/models call is recorded separately."""
    rows = _read_jsonl(path)
    clients, normalized, responses, non_chat = {}, {}, {}, []
    for number, row in rows:
        kind = row.get("kind")
        if kind == "client_request":
            if row.get("method") == "GET":
                non_chat.append({"method": "GET", "path": row.get("path"), "line": number,
                                 "note": "model catalogue GET; not a chat request"})
                continue
            clients[row.get("request_id")] = (number, row)
        elif kind == "normalized_request":
            normalized[row.get("request_id")] = (number, row)
        elif kind == "upstream_response":
            responses[row.get("request_id")] = (number, row)
    requests, unassociated = [], []
    for request_id, (line, row) in clients.items():
        norm, resp = normalized.get(request_id), responses.get(request_id)
        if norm is None or resp is None:
            unassociated.append({"request_id": request_id, "file": path.name, "line": line,
                                 "reason": "missing " + ("request body" if norm is None
                                                         else "upstream response")})
            continue
        try:
            body = json.loads(norm[1]["body_utf8"])
            reply = json.loads(resp[1]["body_utf8"])
            message = reply["choices"][0]["message"]
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            raise ReplayDataError(f"proxy record {request_id} is unusable: {exc}") from None
        requests.append({
            "request_id": request_id, "sequence": row.get("sequence"),
            "timestamp": row.get("timestamp"), "role": row.get("role"),
            "source": {"file": path.name, "client_request_line": line,
                       "normalized_request_line": norm[0], "upstream_response_line": resp[0]},
            "messages": body.get("messages") or [],
            "model": body.get("model"), "tool_choice": body.get("tool_choice"),
            "parallel_tool_calls": body.get("parallel_tool_calls"),
            "max_tokens": body.get("max_tokens") or body.get("max_completion_tokens"),
            "response": {"content": message.get("content"),
                         "tool_calls": message.get("tool_calls") or [],
                         "finish_reason": reply["choices"][0].get("finish_reason"),
                         "usage": reply.get("usage")},
        })
    requests.sort(key=lambda item: item["sequence"])
    for index, item in enumerate(requests, 1):
        item["index"] = index
    if unassociated:
        warnings.extend(f"unassociated proxy request: {entry}" for entry in unassociated)
    return requests, non_chat, unassociated


def _attribute(requests, audit, warnings):
    frames = {frame["request_id"]: frame for frame in audit.get("frames", [])
              if frame.get("kind") == "agent_frame"}
    auxiliary = {entry["request_id"]: entry for entry in audit.get("auxiliary_mappings", [])}
    for item in requests:
        frame, extra = frames.get(item["request_id"]), auxiliary.get(item["request_id"])
        if frame is not None:
            item.update({"arm": frame.get("arm"), "phase": frame.get("phase"),
                         "kind": "agent_request", "block_sha256": frame.get("block_sha256"),
                         "audit": "agent_frame"})
        elif extra is not None:
            item.update({"arm": extra.get("arm"),
                         "phase": extra.get("role"), "kind": extra.get("role"),
                         "block_sha256": None, "audit": "auxiliary_mapping"})
        else:
            item.update({"arm": None, "phase": None, "kind": "unattributed",
                         "block_sha256": None, "audit": None})
            warnings.append(f"request {item['index']} ({item['request_id']}) has no audit attribution")
    return requests


def _tag_messages(item):
    for message in item["messages"]:
        content = message.get("content")
        blob = json.dumps(message.get("tool_calls") or [], ensure_ascii=False)
        message["unsafe_content"] = _unsafe(content) or _unsafe(blob)
        message["markup_like"] = _markup_like(content) or _markup_like(blob)
    return item


def _tool_name(text):
    match = re.search(r"order_id[:=]['\"]?([^,'\")\s]+)", text or "")
    return match.group(1) if match else None


def _block_facts(text):
    try:
        facts = json.loads(text)
    except (TypeError, ValueError):
        return None
    return facts if isinstance(facts, dict) else None


def _initial_blocks(events, plan):
    """The exact initial memory block per arm, from the agent_created payload."""
    blocks = {}
    for number, row in events:
        if row.get("kind") == "agent_created":
            payload = row.get("payload") or {}
            for entry in payload.get("memory_blocks") or []:
                if entry.get("label") == "ae_preferences":
                    blocks[row.get("arm")] = {"line": number, "value": entry.get("value")}
    if not blocks:
        for arm, payload in (plan.get("arms") or {}).items():
            for entry in (payload or {}).get("memory_blocks") or []:
                blocks[arm] = {"line": None, "value": entry.get("value")}
    return blocks


def _arm_timeline(arm, requests, events, result_arm, plan_block, audit, warnings):
    """Chronological replay steps for one arm, with a per-step derived state."""
    steps = []
    audit_arm = audit.get("arms", {}).get(arm, {})
    memory = {"initial_block": plan_block["value"], "block": plan_block["value"],
              "initial_line": plan_block["line"], "initial_timestamp": plan_block.get("timestamp"),
              "initial_sha256": _sha256((plan_block["value"] or "").encode("utf-8")),
              "sha256": _sha256((plan_block["value"] or "").encode("utf-8")),
              "audit_initial_sha256": audit_arm.get("initial_block_sha256"),
              "patch_applied": False, "appended_correction": None,
              "appended_correction_text": None}
    orders = {}
    arm_events = [(number, row) for number, row in events if row.get("arm") == arm]

    def state():
        facts = _block_facts(memory["block"])
        return {"memory_sha256": memory["sha256"], "memory_block": memory["block"],
                "memory_facts": facts, "memory_source": "captured block/PATCH text",
                "appended_correction": memory["appended_correction"],
                "appended_correction_text": memory["appended_correction_text"],
                "orders": {key: dict(value) for key, value in orders.items()}}

    def add(kind, label, source, *, timestamp=None, request=None, payload=None):
        steps.append({"id": f"{arm}:{len(steps) + 1}", "kind": kind, "label": label,
                      "timestamp": timestamp, "source": source,
                      "request_index": request["index"] if request else None,
                      "payload": payload or {}, "state": state()})

    history_steps = [item for item in requests if item["arm"] == arm and item["phase"] == "history"]
    task_steps = [item for item in requests if item["arm"] == arm and item["phase"] == "task"]
    auxiliary = [item for item in requests if item["arm"] == arm and item["kind"] in
                 ("user_simulator", "evaluator")]
    if len(history_steps) != 2 or not task_steps:
        warnings.append(f"{arm}: expected 2 history requests and a task phase, saw "
                        f"{len(history_steps)}/{len(task_steps)}")

    add("arm_start", f"{ARM_LABELS[arm]} 臂开始（初始记忆块）",
        {"file": "events.jsonl", "line": memory["initial_line"],
         "record": f"agent_created[{arm}].payload.memory_blocks[ae_preferences]"},
        timestamp=memory["initial_timestamp"],
        payload={"initial_block": plan_block,
                 "initial_sha256": memory["initial_sha256"]})

    merged = []
    for number, row in arm_events:
        merged.append(("event", number, row))
    for item in requests:
        if item["arm"] == arm:
            merged.append(("request", item["sequence"] or 0, item))
    merged.sort(key=lambda entry: (str(entry[2].get("timestamp") if entry[0] == "event"
                                       else entry[2].get("timestamp") or ""), entry[1]))

    for kind, _order, payload in merged:
        if kind == "request":
            item = payload
            if item["kind"] == "agent_request":
                phase_label = "历史阶段" if item["phase"] == "history" else "当前任务"
                label = f"模型请求 #{item['index']}（{phase_label}，{ARM_LABELS[arm]} 臂）"
                add("agent_request", label,
                    {"file": item["source"]["file"],
                     "line": item["source"]["normalized_request_line"],
                     "record": item["request_id"]}, timestamp=item["timestamp"],
                    request=item, payload={"message_count": len(item["messages"]),
                                           "history_records": 26 if item["phase"] == "task"
                                           and not any(step["kind"] == "agent_request"
                                                       and step["payload"].get("phase") == "task"
                                                       for step in steps) else None})
                steps[-1]["payload"]["phase"] = item["phase"]
            else:
                label = ("用户模拟器请求" if item["kind"] == "user_simulator" else "评分请求（独立角色）")
                add(item["kind"], f"{label} #{item['index']}",
                    {"file": item["source"]["file"],
                     "line": item["source"]["upstream_response_line"],
                     "record": item["request_id"]}, timestamp=item["timestamp"], request=item)
            continue
        row = payload
        event = row.get("event") or {}
        ekind = event.get("kind")
        if ekind == "client_tool_result":
            call = event.get("call") or {}
            result = event.get("result") or {}
            name = call.get("name")
            text = result.get("tool_return")
            step_payload = {"tool_call_id": call.get("tool_call_id"), "name": name,
                            "status": result.get("status"), "tool_return": text,
                            "block_sha": event.get("block_sha")}
            if name == "memory_update":
                memory["appended_correction"] = text
                correction_text = None
                try:
                    parsed_return = json.loads(text)
                    correction_text = parsed_return.get("erratum")
                except (TypeError, ValueError):
                    correction_text = None
                if correction_text:
                    memory["appended_correction_text"] = correction_text
                    step_payload["correction_appended"] = True
                step_payload["memory_write"] = next(
                    (write for write in result_arm.get("memory_writes", [])
                     if write.get("resolved", {}).get("fact_id")), None)
                add("memory_update", f"记忆更新回包（{name}）",
                    {"file": "events.jsonl", "line": row.get("_line"),
                     "record": call.get("tool_call_id")}, timestamp=row.get("timestamp"),
                    payload=step_payload)
            elif name == "create_delivery_order":
                order_id = _tool_name(text)
                if order_id:
                    orders[order_id] = {"order_id": order_id, "status": "unpaid",
                                        "created_by_return": text,
                                        "derived": "从真实 create 回包推导"}
                step_payload["created_order_id"] = order_id
                add("create_order", "建单回包（真实订单号）",
                    {"file": "events.jsonl", "line": row.get("_line"),
                     "record": call.get("tool_call_id")}, timestamp=row.get("timestamp"),
                    payload=step_payload)
            elif name == "pay_delivery_order":
                order_id = (call.get("arguments") or {})
                try:
                    parsed = json.loads(call.get("arguments") or "{}")
                    order_id = parsed.get("order_id")
                except ValueError:
                    order_id = None
                if order_id in orders and result.get("status") == "success":
                    orders[order_id]["status"] = "paid"
                    orders[order_id]["paid_by_return"] = text
                step_payload["paid_order_id"] = order_id
                add("pay_order", "支付回包",
                    {"file": "events.jsonl", "line": row.get("_line"),
                     "record": call.get("tool_call_id")}, timestamp=row.get("timestamp"),
                    payload=step_payload)
            else:
                add("tool_result", f"工具回包（{name}）",
                    {"file": "events.jsonl", "line": row.get("_line"),
                     "record": call.get("tool_call_id")}, timestamp=row.get("timestamp"),
                    payload=step_payload)
        elif ekind == "request" and event.get("method") == "PATCH":
            before = memory["block"]
            value = (event.get("body") or {}).get("value")
            memory["block"], memory["patch_applied"] = value, True
            memory["sha256"] = _sha256((value or "").encode("utf-8"))
            changed = []
            before_facts, after_facts = _block_facts(before) or {}, _block_facts(value) or {}
            for key in sorted(set(before_facts) | set(after_facts)):
                if before_facts.get(key) != after_facts.get(key):
                    changed.append({"fact_id": key, "before": before_facts.get(key),
                                    "after": after_facts.get(key)})
            add("patch_block", "记忆 PATCH（R 改写块）",
                {"file": "events.jsonl", "line": row.get("_line"), "record": "PATCH "
                 + str(event.get("path"))}, timestamp=row.get("timestamp"),
                payload={"before_block": before, "after_block": value, "changed_facts": changed})
        elif row.get("kind") == "simulated_user":
            add("user_message", "用户消息（模拟器文本，不是工具证据）",
                {"file": "events.jsonl", "line": row.get("_line"), "record": "simulated_user"},
                timestamp=row.get("timestamp"),
                payload={"content": (row.get("reply") or {}).get("content")})

    snapshot = (result_arm.get("native_snapshot") or {}).get("environment_db") or {}
    final_orders = snapshot.get("orders") or {}
    for order_id, order in final_orders.items():
        orders[order_id] = {"order_id": order_id, "status": order.get("status"),
                            "captured": order, "derived": "最终环境快照"}
    add("final_state", f"{ARM_LABELS[arm]} 臂最终状态（封存快照）",
        {"file": "result.json",
         "record": f"arms.{arm}.native_snapshot.environment_db + task_oracle"},
        timestamp=result_arm.get("finished_at_utc"),
        payload={"task_oracle": result_arm.get("task_oracle"),
                 "judge": result_arm.get("judge"),
                 "final_block_sha256": result_arm.get("final_block_sha256"),
                 "patches": len(result_arm.get("patches") or []),
                 "memory_updates": len(result_arm.get("memory_writes") or [])})
    if auxiliary:
        warnings.append(f"{arm}: {len(auxiliary)} auxiliary calls are attributed by the audit")
    return steps, memory, orders


def _jumps(steps):
    jumps = {}
    for step in steps:
        kind = step["kind"]
        if kind == "memory_update" and "memory_update" not in jumps:
            jumps["memory_update"] = step["id"]
        if kind == "agent_request" and step["payload"].get("phase") == "task" \
                and "first_task_request" not in jumps:
            jumps["first_task_request"] = step["id"]
        if kind == "tool_result" and (step["payload"].get("name") ==
                                      "delivery_product_search_recommand") \
                and "search" not in jumps:
            jumps["search"] = step["id"]
        if kind == "create_order" and "create" not in jumps:
            jumps["create"] = step["id"]
        if kind == "pay_order" and "pay" not in jumps:
            jumps["pay"] = step["id"]
    return jumps


def build_replay(data_dir) -> dict:
    """Build the replay model from the sealed capture; raise on corrupt data."""
    data_dir = Path(data_dir).resolve()
    paths = _load(data_dir)
    warnings: list = []
    events = _read_jsonl(paths["events.jsonl"])
    for number, row in events:
        row["_line"] = number
    plan = _read_json(paths["plan.json"])
    result = _read_json(paths["result.json"])
    audit = _read_json(paths["input-audit.json"])
    watchdog = _read_json(paths["watchdog.json"])
    requests, non_chat, unassociated = _proxy_requests(paths["proxy.jsonl"], warnings)
    _attribute(requests, audit, warnings)
    for item in requests:
        _tag_messages(item)
    unsafe = [item["index"] for item in requests
              if any(message.get("unsafe_content") for message in item["messages"])]
    if unsafe:
        warnings.append("unsafe message content detected in requests: " + ", ".join(map(str, unsafe)))

    initial = _initial_blocks(events, plan)
    timeline, arms = {}, {}
    for arm in ARM_ORDER:
        if arm not in initial:
            raise ReplayDataError(f"no captured initial memory block for arm {arm}")
        block = dict(initial[arm])
        block["timestamp"] = next((row.get("timestamp") for _n, row in events
                                   if row.get("kind") == "agent_created"
                                   and row.get("arm") == arm), None)
        steps, memory, orders = _arm_timeline(arm, requests, events, result["arms"][arm],
                                              block, audit, warnings)
        timeline[arm] = steps
        arms[arm] = {
            "label": ARM_LABELS[arm], "agent_id": result["arms"][arm].get("agent_id"),
            "steps": steps, "jumps": _jumps(steps),
            "memory": {"initial_block": memory["initial_block"],
                       "initial_source": {"file": "events.jsonl", "line": memory["initial_line"]},
                       "final_block": memory["block"],
                       "initial_sha256": memory["initial_sha256"],
                       "final_sha256": memory["sha256"],
                       "audit_initial_sha256": memory["audit_initial_sha256"],
                       "audit_final_sha256": audit["arms"].get(arm, {}).get("final_block_sha256"),
                       "patch_applied": memory["patch_applied"],
                       "appended_correction": memory["appended_correction"],
                       "appended_correction_text": memory["appended_correction_text"]},
            "orders": orders,
            "result": {"status": result.get("status"),
                       "termination_reason": result["arms"][arm].get("termination_reason"),
                       "model_posts": result["arms"][arm].get("model_posts"),
                       "duration_seconds": result["arms"][arm].get("duration_seconds"),
                       "patches": len(result["arms"][arm].get("patches") or []),
                       "memory_writes": result["arms"][arm].get("memory_writes"),
                       "final_block_sha256": result["arms"][arm].get("final_block_sha256"),
                       "task_oracle": result["arms"][arm].get("task_oracle"),
                       "judge": result["arms"][arm].get("judge"),
                       "transcript": result["arms"][arm].get("transcript")},
        }

    roles = {}
    for item in requests:
        roles[item["kind"]] = roles.get(item["kind"], 0) + 1
    tool_results = [row for _number, row in events
                    if (row.get("event") or {}).get("kind") == "client_tool_result"]
    tool_names = [(row["event"].get("call") or {}).get("name") for row in tool_results]
    flow = {
        "lanes": ["实验脚本 driver", "Letta 服务", "云模型 (proxy)", "适配桥 bridge",
                  "记忆工具 memory_update", "Vita 环境/工具"],
        "counts": {
            "agent_requests": roles.get("agent_request", 0),
            "user_simulator_requests": roles.get("user_simulator", 0),
            "evaluator_requests": roles.get("evaluator", 0),
            "tool_results": len(tool_results),
            "patches": sum(1 for _n, row in events
                           if (row.get("event") or {}).get("kind") == "request"
                           and (row.get("event") or {}).get("method") == "PATCH"),
            "creates": sum(1 for name in tool_names if name == "create_delivery_order"),
            "pays": sum(1 for name in tool_names if name == "pay_delivery_order"),
            "searches": sum(1 for name in tool_names if name == "delivery_product_search_recommand"),
        },
        "direction_by_kind": {
            "agent_request": ["实验脚本 driver", "Letta 服务", "云模型 (proxy)"],
            "tool_result": ["适配桥 bridge", "Vita 环境/工具"],
            "memory_update": ["适配桥 bridge", "记忆工具 memory_update"],
            "patch_block": ["适配桥 bridge", "Letta 服务"],
            "create_order": ["Vita 环境/工具", "适配桥 bridge"],
            "pay_order": ["Vita 环境/工具", "适配桥 bridge"],
            "user_message": ["实验脚本 driver", "Letta 服务"],
            "user_simulator": ["实验脚本 driver", "云模型 (proxy)"],
            "evaluator": ["实验脚本 driver", "云模型 (proxy)"],
        },
        "auxiliary_note": "用户模拟器与评分请求不是 Agent 动作，单独用虚线泳道表示",
    }
    sources = _sources(data_dir, [
        ("plan", paths["plan.json"], None),
        ("result", paths["result.json"], None),
        ("events", paths["events.jsonl"], len(events)),
        ("letta-http", paths["letta-http.jsonl"],
         len(paths["letta-http.jsonl"].read_text(encoding="utf-8").splitlines())),
        ("proxy-wire", paths["proxy.jsonl"],
         len(paths["proxy.jsonl"].read_text(encoding="utf-8").splitlines())),
        ("input-audit", paths["input-audit.json"], None),
        ("watchdog", paths["watchdog.json"], None),
    ])
    return {
        "schema": SCHEMA, "notice": NOTICE, "data_dir": str(data_dir),
        "sources": sources,
        "meta": {
            "audit_status": audit.get("status"), "input_audit_passed": audit.get("input_audit_passed"),
            "result_status": result.get("status"),
            "result_scientific_result": result.get("scientific_result"),
            "audit_scientific_result": audit.get("scientific_result"),
            "model_requests": len(requests), "chat_roles": roles,
            "non_chat_calls": non_chat,
            "watchdog": {"status": watchdog.get("monitor", {}).get("status"),
                         "trigger": watchdog.get("trigger"),
                         "restarts_performed": watchdog.get("watchdog", {}).get(
                             "restarts_performed")},
            "arm_order": list(ARM_ORDER),
            "arm_order_note": "原运行按 R→E 顺序执行；页面切换展示不代表两臂并行",
            "run_started_at": result.get("started_at_utc"),
            "run_finished_at": result.get("finished_at_utc"),
            "scope": audit.get("scope"), "counts": audit.get("counts"),
            "limitations": audit.get("limitations"),
            "history_records": (plan.get("task_preview") or {}).get("history_records"),
        },
        "flow": flow,
        "requests": requests,
        "unassociated": unassociated,
        "timeline": timeline,
        "arms": arms,
        "warnings": warnings,
    }
