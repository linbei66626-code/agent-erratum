"""Offline tests for the single-t4 cloud R/E pair driver and its input audit.

Positive fixtures are produced by the REAL production path (`execute_re_pair` +
the pinned `MemoryPolicy`/`TaskBridge`/`CheckedTaskTransport`) against a
scripted offline two-agent Letta session and a real `CloudAuditProxy` whose
opener is a fixture. The audited bytes are therefore the bytes the drivers
really emit; the pin `render_memory_blocks`/`format_messages` helpers and the
real Letta renderer come from the fixed sources. Model replies are fixtures, not
model output, and no socket is used.

The negative cases mutate those captured files to prove each bounded gate fires
and no transport artifact is mistaken for a task-input failure.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import ast
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for entry in (TESTS, ROOT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from ae_adapter import MemoryPolicy, dumps  # noqa: E402
from ae_capability import TARGET_PRODUCT_ID  # noqa: E402
from ae_cloud_proxy import (CloudAuditProxy, CloudConfig, MODEL, normalize_request,  # noqa: E402
                            response_summary)
from ae_cloud_re_input_audit import audit_re_pair_inputs  # noqa: E402
from ae_cloud_re_pair import (ARMS, build_plan, execute_re_pair, preflight,  # noqa: E402
                              re_code_files, validate_config)
from ae_input_audit import utc, wire  # noqa: E402
from ae_inputs import canonical_sha256  # noqa: E402
from test_ae_capability import CapEnv  # noqa: E402
from test_ae_cloud_framing import RealLettaRenderer, _Block  # noqa: E402
from test_ae_cloud_input_audit import (KEY, native_package_function_response,  # noqa: E402
                                       proxy_rows, rewrite_records, rewrite_wire,
                                       save_proxy_rows, write_json, write_records,
                                       mutate_letta, mutate_events, mutate_lifecycle)

RE_CONFIG = ROOT / "configs/ae-01__re-pair__siliconflow.pacing-candidate.json"
MULTICALL_CONFIG = (ROOT / "configs"
                    / "ae-01__re-pair__siliconflow.multicall-candidate.json")
# The project-wide `AE_LETTA_SOURCE` convention, resolved exactly once. The value
# is handed to the production generator explicitly instead of letting it fall back
# to a machine-specific default that a lab host does not have.
LETTA_SOURCE_OVERRIDE = os.environ.get("AE_LETTA_SOURCE")
LETTA_BASELINE = (Path(LETTA_SOURCE_OVERRIDE) if LETTA_SOURCE_OVERRIDE
                  else ROOT / ".ae-verify-src/letta-v1")
# The patched tree the manifest's post-patch digests are checked against.
PATCHED_CHECKOUT = Path(os.environ.get("AE_LETTA_PATCHED_SOURCE",
                                       ROOT / ".ae-verify-src/letta-multicall-patch/letta-v1"))


def pinned_baseline():
    """The pinned baseline for the production generator; declared-missing fails."""
    if LETTA_SOURCE_OVERRIDE and not LETTA_BASELINE.is_dir():
        raise RuntimeError(
            "AE_LETTA_SOURCE was set to " + LETTA_SOURCE_OVERRIDE
            + " but the pinned Letta baseline is missing")
    return LETTA_BASELINE
TRANSPORT_CONFIG = (ROOT / "configs"
                    / "ae-01__cloud-transport__siliconflow.capability-pacing-candidate.json")
WORK_ADDRESS = "甘肃省兰州市安宁区安宁西路地五大道88号"
EVIDENCE_TEXT = "嗯！以后就7分糖了"
RUBRIC_TEXT = "fixture rubric must never reach the agent"
HISTORY_RECORDS = 26

# The provider's own bytes from the sealed r2 response: ids, names and argument
# strings. Nothing here re-encodes an argument, and the fourth call keeps its real
# category "消费购物偏好".
R2_DIAGNOSIS = (ROOT / "transfers/lab-cloud-re-pair-run-20260912-r2/diagnosis"
                / "tool-call-truncation.json")
R2_PROVIDER_CALLS = json.loads(R2_DIAGNOSIS.read_text(encoding="utf-8"))["provider_calls"]
MULTICALL_RAW_IDS = tuple(call["id"] for call in R2_PROVIDER_CALLS)
MULTICALL_ARGUMENTS = tuple(call["function"]["arguments"] for call in R2_PROVIDER_CALLS)
MULTICALL_UPDATES = tuple(json.loads(text) for text in MULTICALL_ARGUMENTS)
MULTICALL_PREFIX = MULTICALL_RAW_IDS[0][:29]
MULTICALL_TRUNCATED = MULTICALL_RAW_IDS[0][:29]
# The two arms replay the SAME real response; only the final id character differs
# so the arms cannot collide inside one capture. This derivation is declared here
# and reported in the evidence: a derived arm id is never claimed as an original
# byte from the capture.
MULTICALL_DERIVED_ARM_IDS = True
MULTICALL_IDS = tuple(MULTICALL_PREFIX + value[-1] + "1" for value in MULTICALL_RAW_IDS)
MULTICALL_IDS_ERRATUM = tuple(MULTICALL_PREFIX + value[-1] + "2"
                              for value in MULTICALL_RAW_IDS)


def multicall_tool_calls(arm):
    """The provider's four-call `tool_calls` list for one arm, in provider order.

    The argument strings are the sealed capture's bytes verbatim; only the id's
    final character is arm-derived (documented above).
    """
    ids = MULTICALL_IDS if arm == "rewrite" else MULTICALL_IDS_ERRATUM
    return [{"id": native_id, "type": "function",
             "function": {"name": "memory_update", "arguments": arguments}}
            for native_id, arguments in zip(ids, MULTICALL_ARGUMENTS)]


def MULTICALL_HISTORY_REPLY(arm):  # noqa: N802 - fixture constructor, uppercase by symmetry
    return chat_reply("tool_calls", tool_calls=multicall_tool_calls(arm))


def now():
    return datetime.now(timezone.utc).isoformat()


class FakeClock:
    """Deterministic monotonic clock so the 65 s pacing never really sleeps."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)


def _history():
    records = []
    for index in range(HISTORY_RECORDS):
        dialogue = [{"role": "user", "content": f"历史提问 {index}"},
                    {"role": "assistant", "content": f"历史回答 {index}"}]
        if index == 15:
            dialogue.append({"role": "user", "content": EVIDENCE_TEXT})
        records.append({"ref": f"t4/history/{index}",
                        "record": {"date": f"2024-06-{index % 20 + 1:02d}", "behavior": [],
                                   "dialogue": dialogue}})
    return records


def pair_sample():
    """A t3-cutoff sample projection with the real 26-record t4 shape.

    The four facts the sealed r2 response replaces carry the capture's own
    categories, including "消费购物偏好" for p008, so the replay starts from the
    state the real calls were written against.
    """
    facts = {f"p{i:03d}": {"category": "其他", "content": f"偏好 {i}"} for i in range(8)}
    facts["p007"] = {"category": "饮食偏好", "content": "奶茶偏好5分糖"}
    for update in MULTICALL_UPDATES:
        if update["fact_id"] == "p007":
            # The pinned t3 state must keep exactly one 奶茶偏好5分糖 fact; the real
            # call replaces it with 7分糖.
            continue
        facts[update["fact_id"]] = {"category": update["category"],
                                    "content": f"旧{update['fact_id']}"}
    return {
        "initial_facts": facts,
        "initial_profile": {"user_id": "U000828", "工作地址": WORK_ADDRESS, "姓名": "测试用户"},        "tasks": [
            {"number": 4, "subtask_id": "sub_U000828_4", "domain": "delivery",
             "current_time": "2024-06-23", "instruction": "帮我买一杯联名奶茶送到工作地址",
             "history": _history()},
            {"number": 5, "subtask_id": "sub_U000828_5", "domain": "delivery",
             "current_time": "2024-06-24", "instruction": "t5 fixture instruction",
             "history": [{"ref": "t5/history/0", "record": {
                 "date": "2024-06-24", "behavior": [],
                 "dialogue": [{"role": "user", "content": "t5 fixture"}]}}]},
        ],
        "private_tasks": {
            "sub_U000828_4": {"environment": {}, "target_product_ids": [TARGET_PRODUCT_ID],
                              "evaluation_criteria": [{"rubric": RUBRIC_TEXT}]},
            "sub_U000828_5": {"environment": {}, "target_product_ids": ["T5_TARGET_ONLY"],
                              "evaluation_criteria": [{"rubric": "t5 rubric"}]},
        },
        "source": {"fixture": True},
    }


def re_config(path=None, *, multicall=False):
    """The sealed 0.1 config by default; the versioned 0.2 config on request."""
    if path is None:
        path = MULTICALL_CONFIG if multicall else RE_CONFIG
    return validate_config(json.loads(Path(path).read_text(encoding="utf-8")))


def multicall_manifest(tmp_path, checkout, *, live=False):
    """Produce a reviewed compatibility manifest with the PRODUCTION generator.

    Nothing here hand-writes a manifest field: the fixture calls the same
    generator the delivery uses, pointed at a real patched Letta checkout, so the
    test cannot supply a field production would omit or spell differently.
    """
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location(
        "ae_multicall_write_manifest",
        ROOT / "deployment-assets/letta-multicall/write_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = Path(tmp_path) / "ae-multicall-manifest.json"
    module.build_manifest(patched=Path(checkout), project=ROOT,
                          pinned=pinned_baseline(),
                          letta_checkout=Path(checkout), out=path,
                          applied_to_live_server=live)
    return path


def provenance(config, *, config_path=None, manifest_path=None, receipt_path=None):
    """Use the PRODUCTION CLI provenance writer, not a fixture copy of it.

    The review found the delivery, the CLI and the audit each had their own
    manifest convention. Tests now call the CLI's own writer so the run record
    carries exactly what production would record, including the receipt check the
    writer performs.
    """
    module = load_cli_module("ae_01_cloud_re_pair")
    return module.provenance(config, Path(config_path or RE_CONFIG), manifest_path,
                             receipt_path)


def service_receipt(tmp_path, checkout, manifest_path, *, name="multicall-load.json"):
    """A receipt produced the way the SERVICE produces it.

    The fixture calls the real bootstrap gate over a resolvable checkout, so the
    receipt's fields, paths and digests are the ones the service would write. A
    test that wants an invalid receipt mutates the produced file rather than
    hand-writing one, which is what the r4 review objected to.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ae_bootstrap_receipt", ROOT / "scripts/deployment/letta_bootstrap.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    import ae_multicall as multicall
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    target = Path(tmp_path) / name

    def find(module_name, package=None):
        mapping = {
            "letta.agents.letta_agent_v3": "letta/agents/letta_agent_v3.py",
            "letta.schemas.message": "letta/schemas/message.py",
            "letta.helpers.ae_multicall_compat": "letta/helpers/ae_multicall_compat.py",
        }
        if module_name not in mapping:
            return None
        return type("Spec", (), {
            "origin": str(Path(checkout) / mapping[module_name])})()
    bootstrap.install_multicall_gate({
        "AE_LETTA_MULTICALL_PROFILE": multicall.PROFILE_VERSION,
        "AE_LETTA_MULTICALL_MANIFEST": str(manifest_path),
        "AE_LETTA_MULTICALL_MODULE_SHA256": multicall.live_module_sha(),
        "AE_LETTA_MULTICALL_SUPPORT": str(ROOT),
        "AE_LETTA_MULTICALL_LOAD_RECEIPT": str(target)}, find_spec=find)
    return target


def load_cli_module(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The fixture system message is rendered by the REAL pinned Letta source through
# the renderer validated in tests/test_ae_cloud_framing.py.
_REAL_RENDERER = None


def real_renderer():
    global _REAL_RENDERER
    if _REAL_RENDERER is None:
        _REAL_RENDERER = RealLettaRenderer()
    return _REAL_RENDERER


def letta_system_prompt(declared_system, block, agent_id):
    return real_renderer().system_message(declared_system, [_Block(block)], agent_id)


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
                      "path": path, "body": deepcopy(body)})
        response = self.session.handle(method, path, deepcopy(body))
        self._append({"kind": "response", "request_id": request_id, "http_status": 200,
                      "body": deepcopy(response)})
        return deepcopy(response)

    def close(self):
        if not self.closed:
            self._append({"kind": "transport_close"})
            self._file.close()
            self.closed = True


class ScriptedLettaPair:
    """Two-agent scripted Letta 0.16.8 session with real response shapes."""

    def __init__(self, config=None, update_modes=None):
        self.config = deepcopy(config or re_config())
        self.update_modes = update_modes or {}
        self.agent_ids = {arm: f"agent-fixture-re-{index}"
                          for index, arm in enumerate(ARMS)}
        self.order = list(ARMS)
        self.sessions = {}
        self.provider = None
        self.created = 0

    def handle(self, method, path, body):
        if path == "/v1/health/":
            return {"status": "ok", "version": "0.16.8"}
        if method == "POST" and path == "/v1/agents/":
            arm = self.order[self.created]
            self.created += 1
            aid = self.agent_ids[arm]
            block = deepcopy(body["memory_blocks"][0])
            llm = body["llm_config"]
            session = {
                "arm": arm, "agent_id": aid, "block": block, "block_id": f"block-{aid}",
                "declared_system": body["system"],
                "system": letta_system_prompt(body["system"], block, aid),
                "tool_schemas": [], "native_history": [], "pending": {},
                "message_ids": [f"system-{aid}"],
                "llm_config": {"handle": llm["handle"], "model": self.config["expected_model"],
                               "model_endpoint_type": llm["model_endpoint_type"],
                               "model_endpoint": llm["model_endpoint"],
                               "context_window": llm["context_window"],
                               "max_tokens": llm["max_tokens"],
                               "temperature": llm["temperature"],
                               "parallel_tool_calls": llm["parallel_tool_calls"],
                               "strict": llm["strict"]},
                "update_mode": self.update_modes.get(arm, "update"),
            }
            self.sessions[aid] = session
            return {"id": aid}
        agent = path.split("/")[3].split("?")[0]
        session = self.sessions[agent]
        if method == "GET":
            return {
                "id": agent, "agent_type": "letta_v1_agent",
                "blocks": [dict(deepcopy(session["block"]), id=session["block_id"])],
                "tools": [], "sources": [], "tags": [],
                "message_ids": list(session["message_ids"]),
                "managed_group": None,
                "pending_approval": deepcopy(session["pending"]) or None,
                "message_buffer_autoclear": False, "enable_sleeptime": False,
                "embedding": None, "embedding_config": None,
                "llm_config": deepcopy(session["llm_config"]),
            }
        if method == "PATCH":
            assert path.endswith("/core-memory/blocks/ae_preferences")
            session["block"]["value"] = body["value"]
            # Real Letta recompiles the system prompt after a block change (the
            # bridge explicitly tolerates a changed system message id for R), so
            # the next request must render the NEW block.
            session["system"] = letta_system_prompt(session["declared_system"],
                                                    session["block"], session["agent_id"])
            return {"id": session["block_id"], "value": body["value"]}
        if method == "POST" and path == f"/v1/agents/{agent}/messages":
            session["tool_schemas"] = deepcopy(body.get("client_tools") or [])
            reply = self.provider.chat(session, body["messages"], session["tool_schemas"])
            return self._messages(session, body, reply)
        raise AssertionError(f"unexpected fixture Letta call: {method} {path}")

    def _messages(self, session, body, reply):
        for message in body["messages"]:
            if message.get("type") == "tool_return":
                for returned in message["tool_returns"]:
                    session["pending"].pop(returned["tool_call_id"], None)
        session["message_ids"].append(f"message-{len(session['message_ids'])}")
        message = reply["choices"][0]["message"]
        call = message.get("tool_calls")
        if not call:
            text = message.get("content") or "fixture final answer"
            session["native_history"].append({"role": "assistant", "content": text})
            return {"messages": [{"message_type": "assistant_message", "content": text}],
                    "stop_reason": {"stop_reason": "end_turn"}}
        converted = [{"tool_call_id": one["id"], "name": one["function"]["name"],
                      "arguments": one["function"]["arguments"]} for one in call]
        for one in converted:
            session["pending"][one["tool_call_id"]] = one
        session["native_history"].append({"role": "assistant", "content": None,
                                          "tool_calls": [deepcopy(one) for one in call]})
        return {"messages": [{"message_type": "approval_request_message",
                              "tool_calls": deepcopy(converted)}],
                "stop_reason": {"stop_reason": "requires_approval"}}

def chat_reply(finish_reason, content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"model": MODEL, "id": "fixture", "system_fingerprint": "fixture",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


def _pay_call(call_id, order_id):
    return {"id": call_id, "type": "function",
            "function": {"name": "pay_delivery_order",
                         "arguments": dumps({"order_id": order_id})}}


class FixtureReply:
    def __init__(self, raw, status=200, trace="re-pair-fixture"):
        self._raw, self.code = raw, status
        self.headers = {"x-siliconcloud-trace-id": trace}

    def read(self, _limit=None):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class ChatOpener:
    """Serves the fake catalog and the exact reply the provider computed."""

    def __init__(self):
        self.catalog = json.dumps({"object": "list", "data": [{"id": MODEL}]}).encode()
        self.next_reply = None
        self.role_replies = {}
        self.sent = []

    def register(self, marker, reply):
        self.role_replies[marker] = reply

    @staticmethod
    def echoed(reply, body):
        """The reply a real provider sends: it names the model it was asked for.

        The sealed profiles ask for the Qwen model, which is the model the fixture
        replies already carry, so their bytes are unchanged. A model-specific transport
        asks for its own wire model, and the fixture must answer as that provider would
        instead of being judged against another provider's constant.
        """
        reply = dict(reply)
        if isinstance(body.get("model"), str):
            reply["model"] = body["model"]
        return reply

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
                return FixtureReply(json.dumps(self.echoed(reply, body),
                                               ensure_ascii=False).encode())
        assert self.next_reply is not None, "agent reply not prepared"
        reply, self.next_reply = self.next_reply, None
        return FixtureReply(json.dumps(self.echoed(reply, body),
                                       ensure_ascii=False).encode())


class PairProvider:
    """Completes the provider body as Letta does, then proxies it."""

    def __init__(self, proxy, opener):
        self.proxy, self.opener = proxy, opener
        self.calls = []

    def request(self, method, path, body=None):
        if method == "GET" and path == "/v1/models":
            self.proxy.dispatch(method, path, b"", "re-pair/catalog", "agent_or_unknown")
            return {"data": [{"id": MODEL}]}
        raise AssertionError(f"unexpected model transport call: {method} {path}")

    def close(self):
        pass

    @staticmethod
    def wire_turn(turns):
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

    def decide(self, session, new_input, messages):
        sources = []
        for message in new_input:
            if message.get("role") == "user":
                try:
                    sources.append(json.loads(message["content"]).get("source"))
                except (TypeError, ValueError):
                    sources.append(None)
        tool_returns = any(m.get("type") == "tool_return" for m in new_input)
        mode, fact = session["update_mode"], session["arm"]
        memory_call = lambda args: chat_reply("tool_calls", tool_calls=[
            {"id": f"call-{fact}-memory-{session.get('memory_calls', 0)}", "type": "function",
             "function": {"name": "memory_update", "arguments": dumps(args)}}])

        def next_memory():
            session["memory_calls"] = session.get("memory_calls", 0) + 1
            if session["memory_calls"] == 1:
                content = "奶茶偏好3分糖" if mode == "wrong" else "奶茶偏好7分糖"
                return memory_call({"operation": "replace", "fact_id": "p007",
                                    "category": "饮食偏好", "content": content,
                                    "evidence_ref": "t4/history/15"})
            if mode == "replace_twice":
                # A legal second replace with the SAME fact/value as the first.
                return memory_call({"operation": "replace", "fact_id": "p007",
                                    "category": "饮食偏好", "content": "奶茶偏好7分糖",
                                    "evidence_ref": "t4/history/15"})
            return memory_call({"operation": "add", "fact_id": "", "category": "饮食偏好",
                                "content": "奶茶偏好7分糖", "evidence_ref": "t4/history/15"})

        if "dataset_history/material" in sources:
            session["stage"] = "history"
            if mode == "multicall" and session.get("memory_calls", 0) == 0:
                session["memory_calls"] = 1
                return MULTICALL_HISTORY_REPLY(fact)
            if mode != "none" and session.get("memory_calls", 0) == 0:
                return next_memory()
            if mode in ("double", "replace_twice") and session.get("memory_calls", 0) == 1:
                return next_memory()
            return chat_reply("stop", content="history material noted")
        if session.get("stage") == "history" and tool_returns:
            if mode == "multicall" and session.get("memory_calls", 0) == 1:
                # The bridge already executed the whole batch and submitted all
                # four returns in one POST; nothing more is owed for them.
                return chat_reply("stop", content="history material noted")
            if mode in ("double", "replace_twice") and session.get("memory_calls", 0) == 1:
                return next_memory()
            return chat_reply("stop", content="history material noted")
        if "current_task" in sources:
            session["stage"] = "task"
            if mode in ("pay_precondition", "pay_precondition_no_remedy"):
                # The real E failure: pay a non-existent order first. The reply
                # after the environment's error is decided from the ACTUAL return.
                session["pay_stage"] = "missing"
                return chat_reply("tool_calls", tool_calls=[_pay_call(
                    f"call-{fact}-pay-missing", PAY_PRECONDITION_MISSING_ORDER)])
            return chat_reply("tool_calls", tool_calls=[
                {"id": f"call-{fact}-native", "type": "function",
                 "function": {"name": "create_delivery_order",
                              "arguments": dumps({"user_id": "U000828", "store_id": "S1",
                                                  "product_ids": [TARGET_PRODUCT_ID],
                                                  "product_cnts": [1], "address": WORK_ADDRESS,
                                                  "dispatch_time": "2024-06-23 15:00:00",
                                                  "attributes": ["规格: 7分糖"]})}}])
        if mode == "pay_precondition" and session.get("stage") == "task" and tool_returns:
            return self._pay_precondition_reply(session, fact, new_input)
        if mode == "pay_precondition_no_remedy" and tool_returns:
            # A legal model behaviour: after the environment's error it simply
            # stops. That must never be packaged as a successful task.
            return chat_reply("stop", content="fixture final answer")
        return chat_reply("stop", content="fixture final answer")

    @staticmethod
    def _pay_precondition_reply(session, fact, new_input):
        """Scripted recovery steps; the id comes from the real create return."""
        stage = session.get("pay_stage")
        if stage == "missing":
            session["pay_stage"] = "create"
            return chat_reply("tool_calls", tool_calls=[
                {"id": f"call-{fact}-create", "type": "function",
                 "function": {"name": "create_delivery_order",
                              "arguments": dumps({"user_id": "U000828", "store_id": "S1",
                                                  "product_ids": [TARGET_PRODUCT_ID],
                                                  "product_cnts": [1], "address": WORK_ADDRESS,
                                                  "dispatch_time": "2024-06-23 15:00:00",
                                                  "attributes": ["规格: 7分糖"]})}}])
        if stage == "create":
            created = [returned for message in new_input
                       if message.get("type") == "tool_return"
                       for returned in message.get("tool_returns") or []]
            order_id = None
            for returned in created:
                if returned.get("status") == "success":
                    order_id = real_order_id(returned.get("tool_return"))
                    if order_id:
                        break
            assert order_id, "the real create return must supply the order id"
            session["pay_stage"] = "pay"
            session["resolved_order_id"] = order_id
            return chat_reply("tool_calls", tool_calls=[_pay_call(
                f"call-{fact}-pay-real", order_id)])
        session["pay_stage"] = "done"
        return chat_reply("stop", content="fixture final answer")

    def chat(self, session, new_input, tool_schemas):
        messages = [{"role": "system", "content": session["system"]}]
        messages.extend(deepcopy(session["native_history"]))
        messages.extend(self.wire_turn(new_input))
        session["native_history"] = messages[1:]
        reply = self.decide(session, new_input, messages)
        body = {"model": MODEL, "messages": messages, "max_completion_tokens": 2048,
                "temperature": 0, "stream": False, "parallel_tool_calls": False,
                "user": "U000828",
                "tools": [{"type": "function", "function": deepcopy(schema)}
                          for schema in tool_schemas],
                "tool_choice": "auto"}
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self.calls.append(raw)
        self.opener.next_reply = reply
        self.proxy.dispatch("POST", "/v1/chat/completions", raw, "re-pair/stage",
                            "agent_or_unknown")
        return reply


class PairNative:
    """Scripted native runtime double mirroring NativeVita's record shape."""

    def __init__(self, arm, proxy, opener, *, fail=False, user_script=None, judge_fail=None,
                 environment_factory=None):
        self.arm, self.proxy, self.opener, self.fail = arm, proxy, opener, fail
        self.user_script = list(user_script or [])
        self.judge_fail = judge_fail
        self.user_index = 0
        self.calls = []
        self.aborted = False
        # The task environment is normally the dataset's native delivery
        # environment; the default scripted double is kept for the shape fixtures.
        self.environment_factory = environment_factory or (lambda arm: CapEnv())

    def record(self, role, messages, content):
        request = {"model": MODEL, "messages": deepcopy(messages), "temperature": 0,
                   "max_tokens": 4096, "tools": None, "stream": False}
        marker = f"{role.replace('_', ' ')} {self.arm} fixture"
        reply = chat_reply("stop", content=content)
        self.opener.register(marker, reply)
        # The real NativeVita `_capture` stores the plain native message plus the
        # provider raw reply; mirror that shape exactly.
        self.calls.append({"role": role, "subtask_id": "sub_U000828_4",
                           "request": request, "error": None,
                           "response": {"role": role, "content": content, "tool_calls": None,
                                        "raw_data": reply, "timestamp": None, "turn_idx": None,
                                        "cost": 0.0, "usage": None}})
        body = {k: v for k, v in request.items() if k not in ("stream",)}
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self.proxy.dispatch("POST", "/v1/chat/completions", raw, "re-pair/aux", role)
        return reply

    def start(self, task):
        if self.fail:
            raise RuntimeError("fixture native failure")
        return {"environment": self.environment_factory(self.arm), "domain_policy": "fixture policy",
                "instruction": task["instruction"],
                "greeting": {"role": "assistant", "content": "fixture greeting"}}

    def agent_stop(self, text):
        return "###STOP###" in text

    def user_reply(self, text):
        if self.user_index < len(self.user_script):
            content = self.user_script[self.user_index]
        else:
            content = "谢谢，结束本次离线测试。###STOP###"
        self.user_index += 1
        reply = self.record("user_simulator",
                            [{"role": "system", "content": f"user simulator {self.arm} fixture"},
                             {"role": "user", "content": task_instruction()}],
                            content)
        returned = reply["choices"][0]["message"]["content"]
        # Pinned `vita/user/base.py:30-32` stop markers (mirrored offline).
        stop = any(marker in returned for marker in
                   ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###"))
        return {"content": returned, "stop": stop, "raw": {}}

    def finish(self, task, transcript, termination_reason, duration):
        if self.judge_fail == "before":
            raise RuntimeError("fixture judge failure before the scoring request")
        self.record("evaluator",
                    [{"role": "system", "content": f"evaluator {self.arm} fixture"},
                     {"role": "user", "content": dumps(transcript)}],
                    dumps([{"rubric_idx": "rubric_0", "meetExpectation": True,
                            "justification": "fixture"}]))
        if self.judge_fail == "after_call":
            # The native capture already holds this evaluator request; the chain
            # still must not be treated as a completed pair.
            raise RuntimeError("fixture judge failure after a partial native capture")
        return {"reward_info": {"reward": 0.0}, "scientific_success": None,
                "judging_status": "MODEL_JUDGED_DEBUG_ONLY"}

    def snapshot(self):
        return {"fixture": True, "native_calls": deepcopy(self.calls)}

    def abort(self):
        self.aborted = True


def task_instruction():
    return pair_sample()["tasks"][0]["instruction"]


# The order id the real E run hallucinated before any create call. The recovery
# fixture starts from the same missing-order failure and never invents the id.
PAY_PRECONDITION_MISSING_ORDER = "O202406231430001"
PAY_PRECONDITION_MODEL_BASE = "http://127.0.0.1:8190/v1"


def payment_precondition_db():
    """A real delivery database the fixed create/pay chain can actually run on."""
    return {
        "user_id": "U000828",
        "time": "2024-06-23 10:00:00",
        "location": [{"address": WORK_ADDRESS, "longitude": 1.0, "latitude": 2.0}],
        "stores": {"S1": {
            "store_id": "S1", "name": "Store One", "score": 4.5,
            "location": {"longitude": 1.0, "latitude": 2.0, "address": WORK_ADDRESS},
            "tags": ["tea"],
            "products": [{"product_id": TARGET_PRODUCT_ID, "name": "Milk Tea",
                          "store_id": "S1", "store_name": "Store One",
                          "attributes": ["规格: 7分糖"], "tags": ["tea"],
                          "quantity": 1, "price": 10.0}]}},
        "orders": {},
    }


def real_order_id(tool_return):
    """The order id out of a REAL native order return, or None."""
    match = re.search(r"order_id[:=]['\"]?([^,'\")\s]+)", tool_return or "")
    return match.group(1) if match else None


def build_pair_run(tmp_path, *, update_modes=None, fail_arm=None, user_scripts=None,
                   judge_fails=None, multicall=False, checkout=None, config_mutate=None,
                   environment_factory=None):
    """Produce a real-format pair run directory using the production driver.

    `multicall=True` uses the versioned 0.2 config, writes a compatibility
    manifest, and (with `update_modes={"rewrite": "multicall", ...}`) makes the
    scripted provider return four memory updates in ONE history response, which
    is the shape that used to be truncated to a single call.
    """
    run_dir = Path(tmp_path) / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = MULTICALL_CONFIG if multicall else RE_CONFIG
    if config_mutate is not None:
        declared = json.loads(config_path.read_text(encoding="utf-8"))
        config_mutate(declared)
        config_path = Path(tmp_path) / "mutated-config.json"
        config_path.write_text(json.dumps(declared, ensure_ascii=False),
                               encoding="utf-8")
        # A deliberately invalid declaration must not be silently repaired by the
        # fixture: it is handed on as written so the rejection under test is the
        # production one.
        config = declared
    else:
        config = re_config(config_path)
    sample = pair_sample()
    plan = build_plan(config, sample, code_files=re_code_files(ROOT))
    # A 0.2 run needs a real reviewed manifest over a real patched checkout; the
    # default is the standard scratch checkout the delivery tooling produces.
    if multicall and checkout is None:
        checkout = os.environ.get("AE_LETTA_PATCHED_SOURCE") or (
            PATCHED_CHECKOUT if PATCHED_CHECKOUT.is_dir()
            else "/tmp/ae-multicall-patch/letta-v1")
    manifest_path = multicall_manifest(tmp_path, checkout) if multicall else None
    # A 0.2 run must carry the service process's load receipt; the fixture has the
    # real bootstrap gate produce it, then the production CLI writer validates and
    # records the reference. Nothing here hand-writes receipt contents.
    receipt_path = (service_receipt(tmp_path, checkout, manifest_path)
                    if multicall else None)
    plan["provenance"] = provenance(config, config_path=config_path,
                                    manifest_path=manifest_path,
                                    receipt_path=receipt_path)
    write_json(run_dir / "plan.json", plan)

    clock = FakeClock()
    proxy = CloudAuditProxy(CloudConfig(**json.loads(TRANSPORT_CONFIG.read_text())),
                            Path(tmp_path) / "proxy.private.jsonl", api_key=KEY,
                            clock=clock.monotonic, sleep=clock.sleep)
    opener = ChatOpener()
    proxy.opener = opener
    session = ScriptedLettaPair(config=config, update_modes=update_modes)
    provider = PairProvider(proxy, opener)
    session.provider = provider
    recorder = RecorderTransport(session, run_dir / "letta-http.jsonl")
    natives = {}

    def runtime_factory(arm):
        native = PairNative(arm, proxy, opener, fail=(fail_arm == arm),
                            user_script=(user_scripts or {}).get(arm),
                            judge_fail=(judge_fails or {}).get(arm),
                            environment_factory=environment_factory)
        natives[arm] = native
        return native

    events = []

    def emit(event):
        events.append(dict(deepcopy(event), timestamp=now()))

    result = execute_re_pair(config, sample, model_transport=provider,
                             letta_transport=recorder, runtime_factory=runtime_factory,
                             emit=emit)
    recorder.close()
    proxy.close()
    result["provenance"] = provenance(config, config_path=config_path,
                                      manifest_path=manifest_path,
                                      receipt_path=receipt_path)
    write_json(run_dir / "result.json", result)
    write_records(run_dir / "events.jsonl", events)
    return {"run_dir": run_dir, "journal": Path(tmp_path) / "proxy.private.jsonl",
            "config_path": config_path, "result": result, "events": events,
            "session": session, "natives": natives, "plan": plan,
            "manifest_path": manifest_path, "receipt_path": receipt_path,
            "checkout": Path(checkout) if checkout is not None else None}


def normalize_records(path, rows):
    """Renumber a temporary fixture journal and make timestamps monotonic."""
    path = Path(path)
    base = min(utc(row["timestamp"]) for row in rows)
    for index, row in enumerate(rows):
        row["sequence"] = index
        row["timestamp"] = (base + timedelta(microseconds=index)).isoformat()
    rewrite_records(path, rows)


def align_agent_instruction(fixture, text):
    """Declare ``text`` as the current-task instruction and rewrite every wire.

    This keeps the structural derivation checks passing, so it isolates the
    private/future-value scan (a declared input that should never be declared).
    """
    from test_ae_cloud_input_audit import mutate_json
    mutate_json(fixture["run_dir"] / "plan.json",
                lambda plan: plan["task_preview"].update({"instruction": text}))

    def fix_material(row):
        if (row.get("kind") == "request" and row.get("method") == "POST"
                and "/messages" in (row.get("path") or "")):
            for message in row["body"]["messages"]:
                if message.get("role") == "user":
                    material = json.loads(message["content"])
                    if material.get("source") == "current_task":
                        material["instruction"] = text
                        message["content"] = dumps(material)

    path = fixture["run_dir"] / "letta-http.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        fix_material(row)
    rewrite_records(path, rows)

    def fix_event(row):
        if row.get("kind") == "stage_start":
            row["public_task"]["instruction"] = text
        inner = row.get("event") or {}
        if (row.get("kind") == "bridge_event" and inner.get("kind") == "request"
                and inner.get("method") == "POST" and "/messages" in (inner.get("path") or "")):
            for message in inner["body"]["messages"]:
                if message.get("role") == "user":
                    material = json.loads(message["content"])
                    if material.get("source") == "current_task":
                        material["instruction"] = text
                        message["content"] = dumps(material)
    path = fixture["run_dir"] / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        fix_event(row)
    rewrite_records(path, rows)

    def fix_wire(body):
        for message in body.get("messages") or []:
            if message.get("role") != "user":
                continue
            try:
                material = json.loads(message.get("content") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(material, dict) and material.get("source") == "current_task":
                material["instruction"] = text
                message["content"] = dumps(material)

    total = len([r for r in proxy_rows(fixture)
                 if r.get("kind") == "client_request" and r.get("role") == "agent_or_unknown"
                 and r.get("path") == "/v1/chat/completions"])
    for index in range(total):
        mutate_cloud_body(fixture, fix_wire, index=index)
    return fixture


def audit_of(fixture, **kwargs):
    kwargs.setdefault("config_path", fixture["config_path"])
    return audit_re_pair_inputs(fixture["run_dir"], fixture["journal"], **kwargs)


def codes(fixture, **kwargs):
    return [entry["code"] for entry in audit_of(fixture, **kwargs)["invalid_reasons"]]


def mutate_cloud_body(fixture, mutate, *, role="agent_or_unknown", index=0):
    """Mutate one cloud client_request body and resync its own record chain."""
    rows = proxy_rows(fixture)
    config = CloudConfig(**json.loads(TRANSPORT_CONFIG.read_text()))
    candidates = [r for r in rows if r.get("kind") == "client_request"
                  and r.get("path") == "/v1/chat/completions" and r.get("role") == role]
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
    response_raw = wire(upstream_response)
    status = upstream_response.get("http_status")
    requested_output = json.loads(normalized_raw)["max_tokens"]
    summary = next(r for r in rows if r.get("kind") == "cloud_summary"
                   and r.get("request_id") == rid)
    summary.update(response_summary(response_raw, status, requested_output, config))
    save_proxy_rows(fixture, rows)
    return fixture


def _runtime_user_occurrences(rows, *, arm=None, agent_ids=None):
    """Yield (container_list, message, material, count) for runtime_user inputs."""
    seen = 0
    for row in rows:
        messages = None
        if (row.get("kind") == "request" and row.get("method") == "POST"
                and "/messages" in (row.get("path") or "")):
            if arm is not None and agent_ids and agent_ids[arm] not in (row.get("path") or ""):
                continue
            messages = row["body"]["messages"]
        elif row.get("kind") == "bridge_event":
            inner = row.get("event") or {}
            if (inner.get("kind") != "request" or inner.get("method") != "POST"
                    or "/messages" not in (inner.get("path") or "")):
                continue
            if arm is not None and row.get("arm") != arm:
                continue
            messages = inner["body"]["messages"]
        if messages is None:
            continue
        for message in messages:
            if message.get("role") != "user":
                continue
            try:
                material = json.loads(message["content"])
            except (TypeError, ValueError):
                continue
            if material.get("source") != "runtime_user":
                continue
            yield seen, material, message
            seen += 1


def rewrite_runtime_user_wire(fixture, new_text, *, index=0, arm=None):
    """Rewrite ONLY the submitted wire runtime_user content at one occurrence.

    The native capture, the `simulated_user` event and the provider body keep
    their original bytes, which is exactly the injection the audit must catch.
    """
    agent_ids = {a: fixture["result"]["arms"][a].get("agent_id") for a in ARMS}
    path = fixture["run_dir"] / "letta-http.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for seen, material, message in _runtime_user_occurrences(rows, arm=arm,
                                                             agent_ids=agent_ids):
        if seen == index:
            material["content"] = new_text
            message["content"] = dumps(material)
    rewrite_records(path, rows)
    path = fixture["run_dir"] / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for seen, material, message in _runtime_user_occurrences(rows, arm=arm):
        if seen == index:
            material["content"] = new_text
            message["content"] = dumps(material)
    rewrite_records(path, rows)
    return fixture


def rewrite_runtime_user_self_report(fixture, new_text, *, index=0, arm=None):
    """Rewrite the native capture and the simulated_user event, NOT the wire."""
    path = fixture["run_dir"] / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    seen = 0
    for row in rows:
        if row.get("kind") == "simulated_user" and (arm is None or row.get("arm") == arm):
            if seen == index:
                row["reply"]["content"] = new_text
            seen += 1
        snapshot = row.get("snapshot")
        if isinstance(snapshot, dict) and snapshot.get("native_calls"):
            for record in snapshot["native_calls"]:
                if record.get("role") != "user_simulator":
                    continue
                if arm is not None and row.get("arm") != arm:
                    continue
                record["response"]["content"] = new_text
                choices = (record["response"].get("raw_data") or {}).get("choices")
                if choices:
                    choices[0]["message"]["content"] = new_text
    rewrite_records(path, rows)
    path = fixture["run_dir"] / "result.json"
    result = json.loads(path.read_text())
    for target in ARMS:
        if arm is not None and target != arm:
            continue
        snapshot = result["arms"][target].get("native_snapshot") or {}
        for record in snapshot.get("native_calls") or []:
            if record.get("role") != "user_simulator":
                continue
            record["response"]["content"] = new_text
            choices = (record["response"].get("raw_data") or {}).get("choices")
            if choices:
                choices[0]["message"]["content"] = new_text
    with path.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return fixture


def continuation_run(tmp, *, rewrite=None, erratum=None, judge_fails=None):
    return build_pair_run(tmp, user_scripts={"rewrite": rewrite or [],
                                             "erratum": erratum or []},
                          judge_fails=judge_fails)


class RuntimeUserProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_one_continuation_is_matched_to_the_native_reply(self):
        fixture = continuation_run(self.tmp.name, rewrite=["请继续处理订单。"],
                                   erratum=["请继续处理订单。"])
        self.assertEqual(codes(fixture), [])
        report = audit_of(fixture)
        frames = [f for f in report["frames"] if f["kind"] == "runtime_user"]
        self.assertEqual(len(frames), 2)

    def test_multiple_continuations_keep_per_reply_order(self):
        fixture = continuation_run(self.tmp.name, rewrite=["先继续。", "再确认一次。"],
                                   erratum=["同一句。", "同一句。"])
        self.assertEqual(codes(fixture), [])

    def test_injected_wire_content_is_rejected(self):
        fixture = continuation_run(self.tmp.name, rewrite=["请继续处理订单。"],
                                   erratum=["请继续处理订单。"])
        rewrite_runtime_user_wire(fixture, "请继续处理订单，这次明确要7分糖。", arm="rewrite")
        self.assertIn("runtime_user_content_not_from_native_reply", codes(fixture))

    def test_self_report_change_without_wire_change_is_rejected(self):
        fixture = continuation_run(self.tmp.name, rewrite=["请继续处理订单。"],
                                   erratum=["请继续处理订单。"])
        rewrite_runtime_user_self_report(fixture, "请继续处理订单，这次明确要7分糖。",
                                         arm="rewrite")
        self.assertTrue(codes(fixture), "a self-report-only change must be rejected")

    def test_missing_continuation_is_rejected(self):
        fixture = continuation_run(self.tmp.name, rewrite=["请继续处理订单。"],
                                   erratum=["请继续处理订单。"])
        path = fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if (row.get("kind") == "request" and row.get("method") == "POST"
                    and "/messages" in (row.get("path") or "")):
                row["body"]["messages"] = [
                    m for m in row["body"]["messages"]
                    if not (m.get("role") == "user"
                            and json.loads(m["content"]).get("source") == "runtime_user")]
        rewrite_records(path, rows)
        events = [json.loads(line) for line in
                  (fixture["run_dir"] / "events.jsonl").read_text().splitlines()]
        for row in events:
            inner = row.get("event") or {}
            if (row.get("kind") == "bridge_event" and inner.get("kind") == "request"
                    and inner.get("method") == "POST"
                    and "/messages" in (inner.get("path") or "")):
                inner["body"]["messages"] = [
                    m for m in inner["body"]["messages"]
                    if not (m.get("role") == "user"
                            and json.loads(m["content"]).get("source") == "runtime_user")]
        rewrite_records(fixture["run_dir"] / "events.jsonl", events)
        self.assertTrue(codes(fixture))

    def _edit_runtime_user_messages(self, fixture, mutate):
        for name in ("letta-http.jsonl", "events.jsonl"):
            path = fixture["run_dir"] / name
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            for row in rows:
                inner = row.get("event") or {}
                if (row.get("kind") == "bridge_event" and inner.get("kind") == "request"
                        and inner.get("method") == "POST"
                        and "/messages" in (inner.get("path") or "")):
                    mutate(inner["body"]["messages"])
                elif (row.get("kind") == "request" and row.get("method") == "POST"
                      and "/messages" in (row.get("path") or "")):
                    mutate(row["body"]["messages"])
            rewrite_records(path, rows, renumber=True)
        return fixture

    def test_duplicate_continuation_message_is_rejected(self):
        fixture = continuation_run(self.tmp.name, rewrite=["请继续处理订单。"],
                                   erratum=["请继续处理订单。"])

        def duplicate(messages):
            for index, message in enumerate(list(messages)):
                if (message.get("role") == "user"
                        and json.loads(message["content"]).get("source") == "runtime_user"):
                    messages.insert(index + 1, deepcopy(message))
                    return
        self._edit_runtime_user_messages(fixture, duplicate)
        self.assertTrue(codes(fixture))

    def test_extra_continuation_message_is_rejected(self):
        fixture = continuation_run(self.tmp.name, rewrite=["请继续处理订单。"],
                                   erratum=["请继续处理订单。"])

        def extra(messages):
            for message in messages:
                if (message.get("role") == "user"
                        and json.loads(message["content"]).get("source") == "runtime_user"):
                    material = json.loads(message["content"])
                    material["ref"] = "t4/user/2"
                    material["content"] = "多出来的一句。"
                    messages.append({"role": "user", "content": dumps(material)})
                    return
        self._edit_runtime_user_messages(fixture, extra)
        self.assertIn("unexpected_runtime_user_message", codes(fixture))

    def test_out_of_order_continuations_are_rejected(self):
        fixture = continuation_run(self.tmp.name, rewrite=["第一句继续。", "第二句继续。"],
                                   erratum=["第一句继续。", "第二句继续。"])
        rewrite_runtime_user_wire(fixture, "第二句继续。", index=0, arm="rewrite")
        rewrite_runtime_user_wire(fixture, "第一句继续。", index=1, arm="rewrite")
        self.assertIn("runtime_user_content_not_from_native_reply", codes(fixture))

    def test_cross_arm_continuation_is_rejected(self):
        fixture = continuation_run(self.tmp.name, rewrite=["R 的继续说。"],
                                   erratum=["E 的继续说。"])
        rewrite_runtime_user_wire(fixture, "E 的继续说。", arm="rewrite")
        self.assertIn("runtime_user_content_not_from_native_reply", codes(fixture))

    def test_stop_markers_match_the_pinned_vita_constants(self):
        import ast as _ast
        # Existing `AE_VITA_SOURCE` convention (see test_ae_aux_wire); the
        # historical Mac default is kept only when the variable is unset. An
        # explicitly selected source that lacks the pinned file is a hard
        # failure, never a skip.
        explicit = os.environ.get("AE_VITA_SOURCE")
        source = Path(explicit) if explicit else Path("/tmp/ae01-vita-8WHDvk/source")
        base = source / "src/vita/user/base.py"
        if not base.is_file():
            if explicit:
                self.fail(f"AE_VITA_SOURCE is set but the pinned Vita file is missing: {base}")
            self.skipTest("fixed Vita source not present")
        tree = _ast.parse(base.read_text(encoding="utf-8"))
        constants = {node.targets[0].id: node.value.value
                     for node in tree.body
                     if isinstance(node, _ast.Assign) and isinstance(node.targets[0], _ast.Name)
                     and node.targets[0].id in {"STOP", "TRANSFER", "OUT_OF_SCOPE"}}
        from ae_cloud_re_input_audit import USER_STOP_MARKERS
        self.assertEqual(sorted(USER_STOP_MARKERS),
                         sorted([constants["STOP"], constants["TRANSFER"],
                                 constants["OUT_OF_SCOPE"]]))


class CaptureOrderTests(unittest.TestCase):
    """Strict per-arm capture order for auxiliary calls and memory updates."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _aux_request_order(self, fixture, arm):
        rows = proxy_rows(fixture)
        window = None
        events = [e for e in
                  [json.loads(line) for line in
                   (fixture["run_dir"] / "events.jsonl").read_text().splitlines()]
                  if e.get("arm") == arm]
        start = min(utc(e["timestamp"]) for e in events if e.get("kind") == "stage_start")
        end = min(utc(e["timestamp"]) for e in events if e.get("kind") == "arm_complete")
        order = []
        for row in rows:
            if row.get("kind") != "client_request" or row.get("role") == "agent_or_unknown":
                continue
            when = utc(row["timestamp"])
            if start < when < end:
                order.append(row["request_id"])
        return order

    def test_provider_reply_order_swap_is_rejected(self):
        from unittest.mock import patch
        scripts = {"rewrite": ["第一句继续。", "第二句继续。"]}
        with patch.object(ChatOpener, "register",
                          _swapped_user_simulator_register):
            fixture = build_pair_run(self.tmp.name, user_scripts=scripts)
        result = codes(fixture)
        self.assertTrue(any(code.endswith("_in_rewrite") and "auxiliary_call_order" in code
                            for code in result), result)

    def test_control_auxiliary_mappings_follow_capture_order(self):
        fixture = build_pair_run(self.tmp.name,
                                 user_scripts={"rewrite": ["第一句继续。", "第二句继续。"]})
        report = audit_of(fixture)
        self.assertEqual(report["status"], "VALID", report["invalid_reasons"])
        expected = self._aux_request_order(fixture, "rewrite")
        mapped = [m["request_id"] for m in report["auxiliary_mappings"] if m["arm"] == "rewrite"]
        self.assertEqual(mapped, expected)

    def test_same_arm_update_event_swap_is_rejected(self):
        fixture = build_pair_run(self.tmp.name, update_modes={"rewrite": "double"})
        self.assertEqual(codes(fixture), [])  # control is valid
        path = fixture["run_dir"] / "events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        indexes = [i for i, row in enumerate(rows)
                   if row.get("arm") == "rewrite"
                   and (row.get("event") or {}).get("kind") == "client_tool_result"
                   and ((row["event"].get("call") or {}).get("name") == "memory_update")]
        self.assertEqual(len(indexes), 2)
        a, b = indexes
        rows[a]["event"], rows[b]["event"] = rows[b]["event"], rows[a]["event"]
        # Keep every slot's original sequence/timestamp: this isolates the
        # semantic order gate from the JSONL structure checks.
        rewrite_records(path, rows)
        result = codes(fixture)
        self.assertIn("rewrite_update_patch_order_or_value_changed", result)

    def test_real_sugar_clarification_is_valid(self):
        fixture = build_pair_run(self.tmp.name,
                                 user_scripts={"rewrite": ["那就7分糖吧。", "送到工作地址。"],
                                               "erratum": ["那就7分糖吧。"]})
        self.assertEqual(codes(fixture), [])


def _swapped_user_simulator_register(self, marker, reply):
    """Swap two different fixture provider replies at the opener only.

    Mirrors the Codex r2 probe: the native self-report, the simulated_user event
    and the Agent input keep their original text while the real CloudAuditProxy
    captures the swapped provider responses.
    """
    changed = deepcopy(reply)
    if marker == "user simulator rewrite fixture":
        message = changed["choices"][0]["message"]
        message["content"] = {"第一句继续。": "第二句继续。",
                              "第二句继续。": "第一句继续。"}.get(message["content"],
                                                                    message["content"])
    return _ORIGINAL_REGISTER(self, marker, changed)


_ORIGINAL_REGISTER = ChatOpener.register


class JudgeChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _erratum_started(self, fixture):
        return any(e.get("kind") == "stage_start" and e.get("arm") == "erratum"
                   for e in fixture["events"])

    def _erratum_requests(self, fixture):
        erratum = fixture["result"]["arms"]["erratum"].get("agent_id")
        rows = [json.loads(line) for line in
                (fixture["run_dir"] / "letta-http.jsonl").read_text().splitlines()]
        return [r for r in rows if r.get("kind") == "request" and r.get("method") == "POST"
                and erratum and erratum in (r.get("path") or "")]

    def test_rewrite_judge_failure_before_request_stops_the_pair(self):
        fixture = build_pair_run(self.tmp.name, judge_fails={"rewrite": "before"})
        result = fixture["result"]
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["stopped_after_arm"], "rewrite")
        self.assertFalse(self._erratum_started(fixture), "E must not start after R's failure")
        self.assertFalse(self._erratum_requests(fixture), "E must make no model request")
        self.assertTrue(result["arms"]["rewrite"]["transcript"])
        self.assertTrue(result["arms"]["rewrite"]["memory_writes"])
        self.assertEqual(result["arms"]["rewrite"]["judge"]["status"],
                         "JUDGE_FAILED_ORDERS_RETAINED")

    def test_erratum_judge_failure_keeps_rewrite_evidence(self):
        fixture = build_pair_run(self.tmp.name, judge_fails={"erratum": "after_call"})
        result = fixture["result"]
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["stopped_after_arm"], "erratum")
        self.assertEqual(result["arms"]["rewrite"]["judge"]["status"], "MODEL_JUDGED_DEBUG_ONLY")
        self.assertTrue(result["arms"]["rewrite"]["transcript"])
        self.assertEqual(result["arms"]["erratum"]["judge"]["status"],
                         "JUDGE_FAILED_ORDERS_RETAINED")

    def test_failed_judge_capture_is_rejected_by_the_audit(self):
        fixture = build_pair_run(self.tmp.name, judge_fails={"rewrite": "before"})
        report = audit_of(fixture)
        self.assertEqual(report["status"], "INVALID")
        self.assertTrue(report["invalid_reasons"])

    def test_forged_completion_cannot_bypass_the_judge_chain(self):
        """A capture that claims completion but has a failed judge chain fails."""
        fixture = build_pair_run(self.tmp.name)
        from test_ae_cloud_input_audit import mutate_json

        def forge(result):
            result["arms"]["rewrite"]["judge"] = {
                "status": "JUDGE_FAILED_ORDERS_RETAINED", "reward_info": None,
                "scientific_success": None,
                "error": {"type": "RuntimeError", "message": "forged failure"}}
        mutate_json(fixture["run_dir"] / "result.json", forge)
        result = codes(fixture)
        self.assertIn("judge_error_present_rewrite", result)

    def test_forged_pair_completion_flags_are_rejected(self):
        fixture = build_pair_run(self.tmp.name, judge_fails={"rewrite": "after_call"})
        from test_ae_cloud_input_audit import mutate_json

        def forge(result):
            result["status"] = "RE_PAIR_COMPLETED_AUDIT_PENDING"
            result["execution_complete"] = True
            result["invalid_reasons"] = []
            result.pop("stopped_after_arm", None)
        mutate_json(fixture["run_dir"] / "result.json", forge)
        self.assertTrue(codes(fixture), "forged completion must not be accepted")

    def test_low_score_is_still_valid(self):
        fixture = build_pair_run(self.tmp.name)
        self.assertEqual(fixture["result"]["arms"]["rewrite"]["judge"]["reward_info"],
                         {"reward": 0.0})
        self.assertEqual(codes(fixture), [])


class SameValueRepeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_pair_run(self.tmp.name,
                                      update_modes={"rewrite": "replace_twice",
                                                    "erratum": "replace_twice"})

    def patch_rows(self):
        return [json.loads(line) for line in
                (self.fixture["run_dir"] / "letta-http.jsonl").read_text().splitlines()
                if json.loads(line).get("kind") == "request"
                and json.loads(line).get("method") == "PATCH"]

    def test_repeated_same_fact_replace_is_valid(self):
        report = audit_of(self.fixture)
        self.assertEqual(report["status"], "VALID", report["invalid_reasons"])
        self.assertEqual(report["arms"]["rewrite"]["patches"], 2)
        self.assertEqual(len(report["memory_mappings"]), 2)
        self.assertEqual(self.fixture["result"]["arms"]["rewrite"]["memory_writes"][0]["resolved"],
                         self.fixture["result"]["arms"]["rewrite"]["memory_writes"][1]["resolved"])

    def test_missing_patch_is_rejected(self):
        path = self.fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        patches = [r for r in rows if r.get("kind") == "request" and r.get("method") == "PATCH"]
        drop = patches[-1]
        dropped_response = next(r for r in rows if r.get("kind") == "response"
                                and r.get("request_id") == drop["request_id"])["body"]
        rows = [r for r in rows if r.get("request_id") != drop["request_id"]]
        rewrite_records(path, rows, renumber=True)
        events = [json.loads(line) for line in
                  (self.fixture["run_dir"] / "events.jsonl").read_text().splitlines()]
        removed_request = removed_response = False
        kept = []
        for event in events:
            inner = event.get("event") or {}
            if event.get("kind") == "bridge_event" and inner.get("method") == "PATCH" \
                    and inner.get("body") == drop["body"] and not removed_request:
                removed_request = True
                continue
            if event.get("kind") == "bridge_event" and inner.get("kind") == "response" \
                    and inner.get("body") == dropped_response and not removed_response:
                removed_response = True
                continue
            kept.append(event)
        self.assertTrue(removed_request and removed_response)
        rewrite_records(self.fixture["run_dir"] / "events.jsonl", kept, renumber=True)
        result = codes(self.fixture)
        self.assertTrue(result)
        self.assertTrue(any(code.startswith("rewrite_update_without_matching_patch")
                            or "patch" in code for code in result), result)

    def test_extra_patch_is_rejected(self):
        path = self.fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        patches = [r for r in rows if r.get("kind") == "request" and r.get("method") == "PATCH"]
        clone = deepcopy(patches[-1])
        clone["request_id"] = "http-extra-patch"
        clone["sequence"] = len(rows)
        clone["timestamp"] = now()
        rows.append(clone)
        response = {"kind": "response", "request_id": "http-extra-patch", "http_status": 200,
                    "body": {"id": patches[-1]["body"].get("id"), "value": clone["body"]["value"]},
                    "sequence": len(rows), "timestamp": now()}
        rows.append(response)
        rewrite_records(path, rows, renumber=True)
        events = [json.loads(line) for line in
                  (self.fixture["run_dir"] / "events.jsonl").read_text().splitlines()]
        arm = self.fixture["result"]["arms"]["rewrite"]["agent_id"]
        events.append({"kind": "bridge_event", "arm": "rewrite", "task": "sub_U000828_4",
                       "phase": "history",
                       "event": {"kind": "request", "method": "PATCH",
                                 "path": clone["path"], "body": clone["body"]},
                       "sequence": len(events), "timestamp": now()})
        events.append({"kind": "bridge_event", "arm": "rewrite", "task": "sub_U000828_4",
                       "phase": "history",
                       "event": {"kind": "response", "body": response["body"]},
                       "sequence": len(events), "timestamp": now()})
        base = max(row["timestamp"] for row in events if row.get("kind") == "bridge_event")
        events[-1]["timestamp"] = (datetime.fromisoformat(base)
                                   + timedelta(milliseconds=1)).isoformat()
        rewrite_records(self.fixture["run_dir"] / "events.jsonl", events)
        result = codes(self.fixture)
        self.assertTrue(result)

    def test_missing_patch_confirmation_is_rejected(self):
        path = self.fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        patches = [r for r in rows if r.get("kind") == "request" and r.get("method") == "PATCH"]
        drop = patches[-1]["request_id"]
        rows = [r for r in rows if r.get("request_id") != drop
                or r.get("kind") == "request"]
        rewrite_records(path, rows, renumber=True)
        result = codes(self.fixture)
        self.assertTrue(result)

    def test_changed_patch_confirmation_is_rejected(self):
        path = self.fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        patches = [r for r in rows if r.get("kind") == "request" and r.get("method") == "PATCH"]
        target = patches[-1]["request_id"]
        for row in rows:
            if row.get("kind") == "response" and row.get("request_id") == target:
                row["body"] = dict(row["body"], value="tampered")
        rewrite_records(path, rows)
        self.assertTrue(codes(self.fixture))

    def test_swapped_distinct_patches_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_pair_run(tmp, update_modes={"rewrite": "double",
                                                        "erratum": "double"})
            path = fixture["run_dir"] / "letta-http.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            patches = [r for r in rows if r.get("kind") == "request" and r.get("method") == "PATCH"]
            self.assertEqual(len(patches), 2)
            first, second = patches[0]["body"]["value"], patches[1]["body"]["value"]
            self.assertNotEqual(first, second)
            patches[0]["body"]["value"], patches[1]["body"]["value"] = second, first
            rewrite_records(path, rows)
            self.assertTrue(codes(fixture))


class ProductionEntryPointTests(unittest.TestCase):
    """The fixture's provenance is the CLI's OWN writer, and it is really called.

    r2 had two functions named `provenance`; the later one shadowed the production
    wrapper, so a whole-chain fixture silently stopped using production code. This
    test spies on the loader to prove the CLI module is imported and its writer is
    what produced the record.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_fixture_provenance_comes_from_the_production_cli(self):
        calls = []
        original = load_cli_module

        def spy(name):
            module = original(name)
            if name == "ae_01_cloud_re_pair":
                real = module.provenance

                def wrapped(*args, **kwargs):
                    calls.append((args, kwargs))
                    return real(*args, **kwargs)
                module.provenance = wrapped
            return module

        with patch("test_ae_cloud_re_pair.load_cli_module", side_effect=spy):
            fixture = build_pair_run(self.tmp.name, multicall=True,
                                     update_modes={"rewrite": "multicall",
                                                   "erratum": "multicall"})
        # The driver writes provenance for the plan and for the result.
        self.assertGreaterEqual(len(calls), 1, "the production CLI provenance must be called")
        self.assertEqual(calls[0][0][1].name, "ae-01__re-pair__siliconflow.multicall-candidate.json")
        self.assertIn("multicall_manifest", fixture["result"]["provenance"])
        # And the shadowing definition is gone: only one provenance exists here.
        self.assertEqual(_provenance_definitions(), 1)

    def test_the_module_has_exactly_one_provenance_definition(self):
        self.assertEqual(_provenance_definitions(), 1)


def _provenance_definitions():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == "provenance")


class PairFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_pair_run(self.tmp.name)

    def test_fixture_uses_the_real_production_path(self):
        result = self.fixture["result"]
        self.assertEqual(result["status"], "RE_PAIR_COMPLETED_AUDIT_PENDING")
        self.assertEqual(result["pair_order"], ["rewrite", "erratum"])
        for arm in ARMS:
            record = result["arms"][arm]
            self.assertTrue(record["agent_id"])
            self.assertTrue(record["block_id"])
            self.assertTrue(record["bridge_trace"])
            self.assertEqual(record["memory_arm"], arm)
            self.assertTrue(record["transcript"])
        self.assertTrue(result["arms"]["rewrite"]["patches"])
        self.assertFalse(result["arms"]["erratum"]["patches"])
        self.assertEqual(self.fixture["plan"]["task_preview"]["history_records"], HISTORY_RECORDS)

    def test_positive_pair_passes_every_gate(self):
        report = audit_of(self.fixture)
        self.assertEqual(report["status"], "VALID", report["invalid_reasons"])
        self.assertTrue(report["transport_capture_checked"])
        self.assertTrue(report["input_audit_passed"])
        self.assertEqual(report["cloud_calls"]["unaccounted"], 0)
        self.assertEqual(report["cloud_calls"]["agent"],
                         report["counts"]["agent_posts"])
        self.assertGreater(report["cloud_calls"]["auxiliary"], 0)
        self.assertEqual(sorted(report["arms"]), sorted(ARMS))
        self.assertEqual(report["arms"]["rewrite"]["patches"], 1)
        self.assertEqual(report["arms"]["erratum"]["patches"], 0)
        self.assertEqual(len(report["memory_mappings"]), 1)
        self.assertEqual(len(report["erratum_mappings"]), 1)
        self.assertTrue(report["private_boundary"]["present_on_agent_wire"] is False)

    def test_no_update_and_wrong_update_remain_valid_input(self):
        for mode in ("none", "wrong"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    fixture = build_pair_run(tmp, update_modes={"rewrite": mode, "erratum": mode})
                    self.assertEqual(codes(fixture), [],
                                     f"{mode} behaviour must not invalidate the input")

    def test_two_consecutive_updates_keep_id_and_order(self):
        memory = MemoryPolicy("rewrite", pair_sample()["initial_facts"], block_char_limit=8000)
        patches = []
        first = json.loads(memory.update({"operation": "replace", "fact_id": "p007",
                                          "category": "饮食偏好", "content": "奶茶偏好7分糖",
                                          "evidence_ref": "t4/history/15"},
                                         {"t4/history/15"}, patches.append))
        second = json.loads(memory.update({"operation": "add", "fact_id": "",
                                           "category": "饮食偏好", "content": "奶茶偏好7分糖",
                                           "evidence_ref": "t4/history/15"},
                                          {"t4/history/15"}, patches.append))
        self.assertEqual(first["update"]["fact_id"], "p007")
        # The real t3 state already carries p000..p008, so the next free id is p009.
        self.assertEqual(second["update"]["fact_id"], "p009")
        # The real t3 state carries p000..p008, so the added fact is p009.
        self.assertEqual([w["resolved"]["fact_id"] for w in memory.writes], ["p007", "p009"])
        self.assertEqual(patches, [dumps(first["memory_block"]),
                                   dumps(second["memory_block"])])
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_pair_run(tmp, update_modes={"rewrite": "double", "erratum": "double"})
            self.assertEqual(codes(fixture), [])

    def test_preflight_verifies_the_offline_input_boundary(self):
        checks = preflight(pair_sample(), re_config())["checks"]
        self.assertTrue(checks["identical_initial_block"])
        self.assertTrue(checks["t3_old_fact_present"])
        self.assertTrue(checks["t3_replacement_absent"])
        self.assertEqual(checks["history_records"], HISTORY_RECORDS)
        self.assertEqual(checks["replacement_evidence"]["ref"], "t4/history/15")
        self.assertNotIn("sub_U000828_5", json.dumps(checks))

    def test_preflight_rejects_a_changed_history_length(self):
        broken = pair_sample()
        broken["tasks"][0]["history"] = broken["tasks"][0]["history"][:20]
        with self.assertRaises(RuntimeError):
            preflight(broken, re_config())


class MemorySemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_pair_run(self.tmp.name)

    def test_erratum_block_was_patched_is_rejected(self):
        erratum = self.fixture["result"]["arms"]["erratum"]
        path = f"/v1/agents/{erratum['agent_id']}/core-memory/blocks/ae_preferences"
        value = dumps(pair_sample()["initial_facts"])
        http_path = self.fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in http_path.read_text().splitlines()]
        rows.extend([
            {"kind": "request", "request_id": "http-fixture-patch", "method": "PATCH",
             "path": path, "body": {"value": value}, "sequence": len(rows), "timestamp": now()},
            {"kind": "response", "request_id": "http-fixture-patch", "http_status": 200,
             "body": {"id": erratum["block_id"], "value": value},
             "sequence": len(rows) + 1, "timestamp": now()}])
        # Keep the real capture clock: the POST<->provider-call windows must
        # survive so the added PATCH is what the audit rejects.
        rewrite_records(http_path, rows)
        events = [json.loads(line) for line in
                  (self.fixture["run_dir"] / "events.jsonl").read_text().splitlines()]
        events.append({"kind": "bridge_event", "arm": "erratum", "task": "sub_U000828_4",
                       "phase": "task",
                       "event": {"kind": "request", "method": "PATCH", "path": path,
                                 "body": {"value": value}},
                       "sequence": len(events), "timestamp": now()})
        events.append({"kind": "bridge_event", "arm": "erratum", "task": "sub_U000828_4",
                       "phase": "task",
                       "event": {"kind": "response",
                                 "body": {"id": erratum["block_id"], "value": value}},
                       "sequence": len(events), "timestamp": now()})
        # The added PATCH must not perturb the POST window correlation.
        base = max(utc(row["timestamp"]) for row in events
                   if row.get("kind") == "bridge_event")
        events[-1]["timestamp"] = (base + timedelta(milliseconds=1)).isoformat()
        rewrite_records(self.fixture["run_dir"] / "events.jsonl", events)
        self.assertIn("erratum_block_was_patched", codes(self.fixture))

    def test_rewrite_patch_value_must_match_the_real_update(self):
        mutate_letta(self.fixture, lambda row: row["body"].update({"value": "tampered"})
                     if row.get("method") == "PATCH" else None)
        result = codes(self.fixture)
        self.assertTrue(result, "a changed PATCH value must invalidate the capture")

    def test_erratum_correction_must_be_consumed(self):
        path = self.fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        erratum = self.fixture["result"]["arms"]["erratum"]["agent_id"]
        for row in rows:
            if (row.get("kind") == "request" and row.get("method") == "POST"
                    and erratum in row.get("path", "")
                    and any(m.get("type") == "tool_return" for m in row["body"]["messages"])):
                row["body"]["messages"] = [{"role": "user", "content": dumps({
                    "source": "runtime_user", "ref": "t4/user/9", "content": "dropped"})}]
        rewrite_records(path, rows)
        events = [json.loads(line) for line in
                  (self.fixture["run_dir"] / "events.jsonl").read_text().splitlines()]
        for event in events:
            inner = event.get("event") or {}
            if (inner.get("kind") == "request" and inner.get("method") == "POST"
                    and erratum in (inner.get("path") or "")
                    and any(m.get("type") == "tool_return"
                            for m in inner["body"]["messages"])):
                inner["body"] = deepcopy(row["body"]) if False else inner["body"]
        rewrite_records(self.fixture["run_dir"] / "events.jsonl", events)
        self.assertTrue(codes(self.fixture))

    def test_swapped_update_events_are_rejected(self):
        path = self.fixture["run_dir"] / "events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        indexes = [i for i, row in enumerate(rows)
                   if row.get("kind") == "bridge_event"
                   and (row.get("event") or {}).get("kind") == "client_tool_result"
                   and ((row["event"].get("call") or {}).get("name") == "memory_update")]
        if len(indexes) < 2:
            self.skipTest("fixture produced a single memory update")
        first, second = indexes[0], indexes[1]
        rows[first], rows[second] = rows[second], rows[first]
        rewrite_records(path, rows)
        self.assertTrue(codes(self.fixture))


class HistoryPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_pair_run(self.tmp.name)

    def test_changed_history_records_are_rejected(self):
        from test_ae_cloud_input_audit import mutate_json

        def mutate_plan(plan):
            material = json.loads(plan["inputs"]["history_material"]["content"])
            material["records"] = material["records"][:-1]
            plan["inputs"]["history_material"]["content"] = dumps(material)
        mutate_json(self.fixture["run_dir"] / "plan.json", mutate_plan)
        self.assertTrue(codes(self.fixture))

    def test_history_material_in_task_phase_is_rejected(self):
        def mutate(rows):
            for row in rows:
                inner = row.get("event") or {}
                if (row.get("kind") == "bridge_event" and row.get("phase") == "history"
                        and inner.get("kind") == "request"):
                    row["phase"] = "task"
        mutate_lifecycle(self.fixture, mutate)
        self.assertTrue(codes(self.fixture))

    def test_current_task_instruction_change_is_rejected(self):
        path = self.fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if (row.get("kind") != "request" or row.get("method") != "POST"
                    or "/messages" not in (row.get("path") or "")):
                continue
            for message in row["body"]["messages"]:
                if message.get("role") != "user":
                    continue
                material = json.loads(message["content"])
                if material.get("source") == "current_task":
                    material["instruction"] = "tampered instruction"
                    message["content"] = dumps(material)
        rewrite_records(path, rows)
        events = [json.loads(line) for line in
                  (self.fixture["run_dir"] / "events.jsonl").read_text().splitlines()]
        for row in events:
            inner = row.get("event") or {}
            if (row.get("kind") == "bridge_event" and inner.get("kind") == "request"
                    and inner.get("method") == "POST" and "/messages" in (inner.get("path") or "")):
                for message in inner["body"]["messages"]:
                    if message.get("role") == "user":
                        material = json.loads(message["content"])
                        if material.get("source") == "current_task":
                            material["instruction"] = "tampered instruction"
                            message["content"] = dumps(material)
        rewrite_records(self.fixture["run_dir"] / "events.jsonl", events)
        self.assertTrue(codes(self.fixture))


class MulticallReceiveCompatTests(unittest.TestCase):
    """The versioned multi-client-tool-call protocol, end to end and offline.

    The provider fixture returns FOUR memory_update calls in ONE history
    response with the real r2 ids (distinct, 32 characters, sharing their first
    29). The run is produced by the production driver against the real
    `LettaBridge`/`MemoryPolicy`, the real `CloudAuditProxy` and the real RE
    audit, so the audited bytes are the bytes the driver really emitted.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_pair_run(
            self.tmp.name, multicall=True,
            update_modes={"rewrite": "multicall", "erratum": "multicall"})

    def wire_posts(self):
        path = self.fixture["run_dir"] / "letta-http.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()
                if json.loads(line).get("kind") == "request"]

    def history_batch_posts(self):
        """POSTs whose body carries the four-call tool_return batch."""
        found = []
        for row in self.wire_posts():
            if row.get("method") != "POST" or "/messages" not in (row.get("path") or ""):
                continue
            for message in row["body"]["messages"]:
                returns = message.get("tool_returns") or []
                if len(returns) == 4:
                    found.append((row, returns))
        return found

    def test_every_one_of_the_four_calls_survives_to_the_executor(self):
        """The defect was three calls vanishing; all four must execute and return."""
        batches = self.history_batch_posts()
        # One batch per arm; each batch carries all four calls.
        self.assertEqual(len(batches), 2, "each arm must submit exactly one four-call batch")
        for _, returns in batches:
            self.assertEqual(len(returns), 4)
        for _, returns in batches:
            ids = [r["tool_call_id"] for r in returns]
            self.assertIn(ids, [list(MULTICALL_IDS), list(MULTICALL_IDS_ERRATUM)])
            self.assertEqual(len(set(ids)), 4)
            self.assertEqual({r["status"] for r in returns}, {"success"})
        # The old 29-character projection collapsed all four onto one string.
        self.assertEqual({native_id[:29] for native_id in MULTICALL_IDS},
                         {MULTICALL_TRUNCATED})
        self.assertNotEqual(MULTICALL_IDS, MULTICALL_IDS_ERRATUM)
        for arm in ("rewrite", "erratum"):
            events = [json.loads(line) for line in
                      (self.fixture["run_dir"] / "events.jsonl").read_text().splitlines()]
            expected = set(MULTICALL_IDS if arm == "rewrite" else MULTICALL_IDS_ERRATUM)
            executions = [row["event"] for row in events
                          if row.get("arm") == arm
                          and (row.get("event") or {}).get("kind") == "client_tool_result"
                          and (row["event"].get("call") or {}).get(
                              "tool_call_id") in expected]
            self.assertEqual(len(executions), 4, arm)
            self.assertEqual({e["call"]["tool_call_id"] for e in executions}, expected)

    def test_one_arm_patches_per_call_and_the_other_never_patches(self):
        rewrite_patches = [p for p in self.fixture["result"]["arms"]["rewrite"]["patches"]]
        erratum_patches = [p for p in self.fixture["result"]["arms"]["erratum"]["patches"]]
        self.assertEqual(len(rewrite_patches), 4)
        self.assertEqual(erratum_patches, [])
        writes = self.fixture["result"]["arms"]["erratum"]["memory_writes"]
        self.assertEqual(len(writes), 4)
        self.assertTrue(all(write["resolved"]["operation"] == "replace" for write in writes))

    def test_audit_accepts_the_declared_protocol_and_records_both_profiles(self):
        report = audit_of(self.fixture)
        self.assertEqual([e["code"] for e in report["invalid_reasons"]], [])
        self.assertEqual(report["status"], "VALID")
        self.assertTrue(report["input_audit_passed"])
        profile = report["scope"]["multicall_profile"]
        self.assertEqual(profile["version"], "ae-multicall-receive-compat-1")
        self.assertFalse(profile["wire_parallel_tool_calls"])
        self.assertIsInstance(report["scope"]["multicall_manifest"]["sha256"], str)
        # One batch per arm is recorded, with the declared batch size.
        batches = report.get("multicall_batches", [])
        self.assertEqual({b["count"] for b in batches if b["task_phase"] == "history"}, {4})

    def test_batch_evidence_matches_the_provider_batch(self):
        for arm, expected in (("rewrite", MULTICALL_IDS), ("erratum", MULTICALL_IDS_ERRATUM)):
            record = self.fixture["result"]["arms"][arm]
            batches = [b for b in record.get("multicall_batches", []) if b["count"] == 4]
            self.assertEqual(len(batches), 1, arm)
            self.assertEqual(batches[0]["executed_count"], 4)
            self.assertEqual(batches[0]["ids"], list(expected))
            self.assertEqual(batches[0]["policy"]["version"],
                             "ae-multicall-receive-compat-1")
            # The shared 29-character prefix is recorded as evidence, not hidden.
            self.assertEqual(batches[0]["upstream_prefix_collision_groups"],
                             [sorted(expected)])

    def test_sealed_protocol_refuses_the_same_multi_call_response(self):
        """The old 0.1 config must stop, not silently keep one call."""
        sealed = build_pair_run(self.tmp.name + "/sealed",
                                update_modes={"rewrite": "multicall"})
        result = sealed["result"]
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["stopped_after_arm"], "rewrite")
        reason = result["invalid_reasons"][-1]["message"]
        self.assertIn("multicall compatibility policy is not declared", reason)
        # Nothing was executed and the second arm never started.
        self.assertFalse(any(e.get("kind") == "client_tool_result"
                             for e in sealed["events"] if e.get("arm") == "rewrite"))
        self.assertFalse(any(e.get("kind") == "stage_start" and e.get("arm") == "erratum"
                             for e in sealed["events"]))
        # The sealed protocol's own audit still refuses the capture for the same
        # reason it refuses any incomplete run: nothing may be read as a result.
        self.assertEqual(codes(sealed), ["execution_not_complete"])

    def test_declaring_the_profile_in_the_sealed_schema_is_refused_by_the_audit(self):
        """A 0.1 run may not carry a profile; the audit says so, not the driver."""
        from ae_cloud_re_input_audit import RePairInputAudit
        audit = RePairInputAudit(self.fixture["run_dir"], self.fixture["journal"],
                                 config_path=self.fixture["config_path"])
        audit.load()
        opened = json.loads((self.fixture["run_dir"] / "plan.json").read_text(encoding="utf-8"))
        with self.assertRaises(Exception) as caught:
            audit._set_protocol(dict(opened["config"],
                                     schema_version="ae-cloud-re-pair-0.1"))
        self.assertIn("multicall_profile_in_a_schema_that_cannot_declare_it",
                      str(caught.exception))

    def test_a_missing_manifest_is_refused(self):
        from test_ae_cloud_input_audit import mutate_json

        def drop(plan):
            plan["provenance"].pop("multicall_manifest", None)
        mutate_json(self.fixture["run_dir"] / "plan.json", drop)
        mutate_json(self.fixture["run_dir"] / "result.json", drop)
        result = codes(self.fixture)
        self.assertIn("plan_multicall_manifest_missing", result)

    def test_a_tampered_manifest_is_refused(self):
        from test_ae_cloud_input_audit import mutate_json
        path = self.fixture["manifest_path"]
        original = path.read_text(encoding="utf-8")

        def tamper(plan):
            plan["provenance"]["multicall_manifest"]["sha256"] = "0" * 64
        mutate_json(self.fixture["run_dir"] / "plan.json", tamper)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertIn("multicall_manifest_record_differs", codes(self.fixture))

    def _claim_live(self, *, receipt_mutate=None, drop_receipt=False):
        """Make the manifest claim a live server and (optionally) write a receipt.

        The receipt is written the way the SERVICE writes it (the bootstrap
        wrapper), including an instance id and the module path it loaded. A
        client-side record cannot satisfy the audit.
        """
        from test_ae_cloud_input_audit import mutate_json
        manifest_path = self.fixture["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["verification"]["applied_to_live_server"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        new_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        module = ROOT / "ae_multicall.py"
        receipt = {
            "profile_version": "ae-multicall-receive-compat-1",
            "instance_id": "fixture-service-0001",
            "module_path": str(module),
            "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
            "checkout": manifest["letta_checkout"],
            # Each entry binds the resolved path and the bytes the service read.
            "patched_files": {
                name: {"path": str(Path(manifest["letta_checkout"]) / name),
                       "sha256": item["patched_sha256"]}
                for name, item in manifest["patched_files"].items()},
            "manifest_sha256": new_sha,
            "source_verified": True,
            "source_verification": "importlib_find_spec_before_letta_import",
        }
        if receipt_mutate is not None:
            receipt_mutate(receipt)
        record = {"sha256": new_sha}
        if not drop_receipt:
            receipt_path = Path(self.tmp.name) / "multicall-load.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            record = {"path": str(receipt_path),
                      "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}

        def resync(entry):
            entry["provenance"]["multicall_manifest"]["sha256"] = new_sha
            if drop_receipt:
                entry["provenance"].pop("multicall_live_loading", None)
            else:
                entry["provenance"]["multicall_live_loading"] = dict(record)
        mutate_json(self.fixture["run_dir"] / "plan.json", resync)
        mutate_json(self.fixture["run_dir"] / "result.json", resync)

    def test_a_live_server_claim_without_a_service_receipt_is_refused(self):
        """A hand-filled boolean is not evidence that a service loaded the patch."""
        self._claim_live(drop_receipt=True)
        result = codes(self.fixture)
        # The r4 fix: a 0.2 run that EXECUTED must carry a valid receipt, and the
        # optional applied_to_live_server boolean can no longer gate that check.
        self.assertIn("multicall_live_loading_missing", result)

    def test_a_service_receipt_is_accepted_for_a_live_claim(self):
        self._claim_live()
        report = audit_of(self.fixture)
        self.assertEqual([e["code"] for e in report["invalid_reasons"]], [])
        self.assertTrue(report["scope"]["multicall_manifest"]["applied_to_live_server"])
        self.assertEqual(report["scope"]["multicall_live_loading"]["instance_id"],
                         "fixture-service-0001")

    def test_a_receipt_naming_another_module_or_checkout_is_refused(self):
        cases = {
            "module digest differs": lambda receipt: receipt.__setitem__(
                "module_sha256", "0" * 64),
            "module path absent": lambda receipt: receipt.__setitem__(
                "module_path", str(Path(self.tmp.name) / "absent.py")),
            "checkout differs": lambda receipt: receipt.__setitem__(
                "checkout", str(Path(self.tmp.name))),
            "no instance id": lambda receipt: receipt.__setitem__("instance_id", ""),
            "patched file digest differs": lambda receipt: receipt.__setitem__(
                "patched_files", {name: {"path": entry["path"], "sha256": "0" * 64}
                                  for name, entry in receipt["patched_files"].items()}),
            "source not verified": lambda receipt: receipt.__setitem__(
                "source_verified", False),
            "resolved file outside checkout": lambda receipt: receipt.__setitem__(
                "patched_files", {name: {"path": str(ROOT / "ae_adapter.py"),
                                         "sha256": entry["sha256"]}
                                  for name, entry in receipt["patched_files"].items()}),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                self.fixture = build_pair_run(
                    tempfile.mkdtemp(prefix="live-claim-"), multicall=True,
                    update_modes={"rewrite": "multicall", "erratum": "multicall"})
                self._claim_live(receipt_mutate=mutate)
                result = codes(self.fixture)
                self.assertTrue(result, f"{label} must be refused")
                self.assertTrue(any("live_loading" in code for code in result), result)

    def test_the_driver_refuses_unreviewed_or_weakened_profile_declarations(self):
        """An unknown version or a weakened field never reaches a run."""
        base = json.loads(MULTICALL_CONFIG.read_text(encoding="utf-8"))
        for label, mutate in (
            ("unknown version",
             lambda profile: profile.__setitem__("version", "ae-multicall-999")),
            ("weakened retain",
             lambda profile: profile.__setitem__("retain_all_calls", False)),
            ("weakened ids",
             lambda profile: profile.__setitem__("preserve_native_ids", False)),
            ("weakened ordering",
             lambda profile: profile.__setitem__("execute_serially", False)),
            ("concurrency enabled",
             lambda profile: profile.__setitem__("wire_parallel_tool_calls", True)),
            ("unsupported batch kept",
             lambda profile: profile.__setitem__("unsupported_batch", "truncate_to_first")),
            ("fields added",
             lambda profile: profile.__setitem__("extra", True)),
        ):
            with self.subTest(label=label):
                broken = deepcopy(base)
                mutate(broken["multicall_profile"])
                with self.assertRaises(ValueError):
                    validate_config(broken)

    def test_reordering_or_dropping_a_call_on_the_wire_is_rejected(self):
        path = self.fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if row.get("kind") != "request" or "/messages" not in (row.get("path") or ""):
                continue
            for message in row["body"]["messages"]:
                returns = message.get("tool_returns")
                if isinstance(returns, list) and len(returns) == 4:
                    returns[1], returns[2] = returns[2], returns[1]
        rewrite_records(path, rows)
        self.assertTrue(codes(self.fixture))

    def test_a_truncated_id_on_the_wire_is_rejected(self):
        """A 29-character prefix instead of the full id must not pass."""
        ids = set(MULTICALL_IDS) | set(MULTICALL_IDS_ERRATUM)
        path = self.fixture["run_dir"] / "letta-http.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        mutated = 0
        for row in rows:
            if row.get("kind") != "request" or "/messages" not in (row.get("path") or ""):
                continue
            for message in row["body"]["messages"]:
                for returned in message.get("tool_returns") or []:
                    if returned.get("tool_call_id") in ids:
                        returned["tool_call_id"] = MULTICALL_TRUNCATED
                        mutated += 1
        self.assertEqual(mutated, 8, "all four returns of both arms must be mutated")
        rewrite_records(path, rows)
        # The shortened id is refused. Which gate names it depends on how far the
        # capture gets: the wire no longer matches the bridge's own trace, which
        # is itself a refusal. What must NOT happen is a silent pass.
        result = codes(self.fixture)
        self.assertTrue(result, "a shortened wire id must never audit as valid")
        self.assertIn(result[0], {"http_request_missing_bridge_trace",
                                  "tool_return_not_same_as_execution_event",
                                  "wire_tool_call_id_truncated_from_provider_id"})

    def test_provenance_still_must_match_the_project_files(self):
        """The compatibility path must not weaken the provenance gate."""
        from test_ae_cloud_input_audit import mutate_json

        def wrong(plan):
            plan["provenance"]["code_sha256"]["ae_adapter.py"] = "1" * 64
        mutate_json(self.fixture["run_dir"] / "plan.json", wrong)
        mutate_json(self.fixture["run_dir"] / "result.json", wrong)
        self.assertIn("plan_provenance_code_sha256_mismatch_ae_adapter.py", codes(self.fixture))


class ArmSeparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_pair_run(self.tmp.name)

    def test_creation_payload_change_is_rejected(self):
        from test_ae_cloud_input_audit import mutate_json
        mutate_json(self.fixture["run_dir"] / "plan.json",
                    lambda plan: plan["arms"]["erratum"]["agent_payload"].update(
                        {"system": "tampered"}))
        self.assertTrue(codes(self.fixture))

    def test_arm_order_change_is_rejected(self):
        from test_ae_cloud_input_audit import mutate_json
        mutate_json(self.fixture["run_dir"] / "result.json",
                    lambda result: result.update({"pair_order": ["erratum", "rewrite"]}))
        self.assertIn("pair_order_changed", codes(self.fixture))

    def test_rubric_text_on_agent_wire_is_rejected(self):
        align_agent_instruction(self.fixture, RUBRIC_TEXT)
        self.assertIn("private_future_value_on_agent_wire", codes(self.fixture))

    def test_t5_instruction_on_agent_wire_is_rejected(self):
        align_agent_instruction(self.fixture, "t5 fixture instruction")
        self.assertIn("private_future_value_on_agent_wire", codes(self.fixture))

    def test_declared_future_text_is_rejected(self):
        align_agent_instruction(self.fixture, "sub_U000828_5")
        self.assertTrue(any(code in {"private_future_value_on_agent_wire", "future_turn_leaked"}
                            for code in codes(self.fixture)))

    def test_agent_parameter_change_is_rejected(self):
        mutate_cloud_body(self.fixture, lambda body: body.update({"temperature": 0.7}))
        self.assertIn("cloud_temperature_changed", codes(self.fixture))

    def test_agent_body_change_is_rejected(self):
        def mutate(body):
            for message in body["messages"]:
                if message.get("role") == "user":
                    message["content"] = "tampered user input"
                    return
        mutate_cloud_body(self.fixture, mutate)
        self.assertTrue(codes(self.fixture))

    def test_missing_lifecycle_event_is_rejected(self):
        def mutate(rows):
            rows[:] = [row for row in rows
                       if not (row.get("kind") == "stage_start" and row.get("arm") == "erratum")]
        mutate_lifecycle(self.fixture, mutate)
        self.assertTrue(codes(self.fixture))

    def test_duplicate_agent_created_event_is_rejected(self):
        def mutate(rows):
            for row in rows:
                if row.get("kind") == "agent_created":
                    clone = deepcopy(row)
                    clone["arm"] = "erratum" if row.get("arm") == "rewrite" else "rewrite"
                    rows.append(clone)
                    return
        mutate_lifecycle(self.fixture, mutate)
        self.assertTrue(codes(self.fixture))


class AuxiliaryWireProvenanceTests(unittest.TestCase):
    """The RE auxiliary stage reuses the wire verified against the real classes."""

    def test_auxiliary_stage_matches_real_vita_dumps(self):
        try:
            from test_ae_aux_wire import LLM_UTILS, pinned_format_messages, real_message_classes
        except ImportError:  # pragma: no cover - environment dependent
            self.skipTest("auxiliary wire test helpers unavailable")
        if not LLM_UTILS.is_file():
            self.skipTest(f"fixed Vita source not present: {LLM_UTILS}")
        before_modules = {k: v for k, v in sys.modules.items()
                          if k == "vita" or k.startswith("vita.")}
        before_path = list(sys.path)
        before_env = {k: os.environ.get(k)
                      for k in ("VITA_MODEL_CONFIG_PATH", "PYTHON_DOTENV_DISABLED")}

        def restore():
            for name in list(sys.modules):
                if name == "vita" or name.startswith("vita."):
                    sys.modules.pop(name, None)
            sys.modules.update(before_modules)
            sys.path[:] = before_path
            for key, value in before_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        with tempfile.TemporaryDirectory() as tmp:
            classes = real_message_classes(tmp)
            formatter = pinned_format_messages(classes)
            objects = [classes["system"](role="system", content="aux system"),
                       classes["user"](role="user", content="aux user")]
            captured = [obj.model_dump(mode="json") for obj in objects]
        from ae_cloud_re_input_audit import auxiliary_wire_messages
        from ae_input_audit import no_nulls
        self.assertEqual(no_nulls(auxiliary_wire_messages(captured)),
                         no_nulls(formatter(objects)))


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_duplicate_cloud_call_record_is_rejected(self):
        """An extra/unconsumable cloud call group is never silently ignored."""
        fixture = build_pair_run(self.tmp.name)
        rows = proxy_rows(fixture)
        kinds = ["client_request", "normalized_request", "pace_wait", "upstream_request",
                 "upstream_response", "cloud_summary"]
        start = next(i for i in range(len(rows))
                     if all(rows[i + k].get("kind") == kinds[k] for k in range(len(kinds))))
        group = deepcopy(rows[start:start + len(kinds)])
        request_id = "duplicate-fixture-call"
        for row in group:
            row["request_id"] = request_id
        rows[start + len(kinds):start + len(kinds)] = group
        normalize_records(fixture["journal"], rows)
        result = codes(fixture)
        self.assertTrue(result, "an extra cloud call record must be rejected")

    def test_executed_tool_result_must_be_submitted(self):
        fixture = build_pair_run(self.tmp.name)

        def mutate(rows):
            for row in rows:
                inner = row.get("event") or {}
                if (inner.get("kind") == "client_tool_result"
                        and (inner.get("call") or {}).get("name") == "memory_update"):
                    inner["result"] = dict(inner["result"], tool_return=dumps({"tampered": True}))
                    return
        mutate_lifecycle(fixture, mutate)
        result = codes(fixture)
        self.assertTrue(any("tool" in code or "erratum" in code or "memory" in code
                            for code in result), result)

    def test_one_arm_failure_stops_the_pair_and_keeps_the_other_evidence(self):
        fixture = build_pair_run(self.tmp.name, fail_arm="erratum")
        result = fixture["result"]
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["stopped_after_arm"], "erratum")
        self.assertTrue(result["invalid_reasons"])
        self.assertTrue(result["arms"]["rewrite"].get("bridge_trace"))
        self.assertIn("rewrite", result["arms"])
        self.assertNotIn("arm_complete", [e.get("kind") for e in fixture["events"]
                                          if e.get("arm") == "erratum"])

    def test_report_is_still_invalid_when_inputs_are_missing(self):
        fixture = build_pair_run(self.tmp.name)
        (fixture["run_dir"] / "plan.json").unlink()
        report = audit_of(fixture)
        self.assertEqual(report["status"], "INVALID")
        self.assertFalse(report["input_audit_passed"])

    def test_audit_cli_reserves_its_output(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ae_01_cloud_re_input_audit",
            ROOT / "scripts/ae_01_cloud_re_input_audit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixture = build_pair_run(self.tmp.name)
        out = Path(self.tmp.name) / "report.json"
        out.write_text("{}", encoding="utf-8")
        code = module.main(["--run-dir", str(fixture["run_dir"]),
                            "--proxy-journal", str(fixture["journal"]),
                            "--output", str(out)])
        self.assertEqual(code, 2)
        self.assertEqual(out.read_text(encoding="utf-8"), "{}")


class PaymentPreconditionPairTests(unittest.TestCase):
    """The two-arm production driver fixture with a REAL recovered tool error.

    Both arms run the same real fixed Vita delivery environment through the
    production driver, the real bridge and the real `CloudAuditProxy` chain. The
    scripted provider first pays a non-existent order -- the exact failure the real
    E run produced -- then creates a real order and pays the id the REAL create
    return supplied. The full input audit must accept that wire; deleting or
    tampering the error return, and the un-remedied variant, must not become a
    success. This is a fixture chain, not evidence of model capability, and no
    model, network or service is involved.
    """

    @classmethod
    def setUpClass(cls):
        from test_ae_tool_errors import DELIVERY_TOOLS, VITA_SOURCE, VITA_SOURCE_OVERRIDE
        if Path(sys.prefix).resolve() != (ROOT / ".venv-vita").resolve():
            raise unittest.SkipTest("the payment-precondition pair fixture requires .venv-vita")
        if VITA_SOURCE_OVERRIDE and not DELIVERY_TOOLS.is_file():
            raise RuntimeError(
                "AE_VITA_SOURCE is set but the pinned Vita delivery tools are missing: "
                f"{DELIVERY_TOOLS}")
        if not (VITA_SOURCE / "src/vita").is_dir():
            raise unittest.SkipTest(f"fixed Vita source not present: {VITA_SOURCE}")
        cls._prior_modules = {k: v for k, v in sys.modules.items()
                              if k == "vita" or k.startswith("vita.")}
        cls._prior_path = list(sys.path)
        cls._tmp = tempfile.TemporaryDirectory(prefix="ae-pay-precondition-")
        config_path = Path(cls._tmp.name) / "models.yaml"
        config_path.write_text(json.dumps({"default": {}, "models": [
            {"name": "Qwen3-8B", "base_url": PAY_PRECONDITION_MODEL_BASE,
             "api_key": "EMPTY"}]}), encoding="utf-8")
        cls._prior_env = os.environ.get("VITA_MODEL_CONFIG_PATH")
        os.environ["VITA_MODEL_CONFIG_PATH"] = str(config_path)
        sys.path.insert(0, str(VITA_SOURCE / "src"))
        from vita.domains.delivery.environment import get_environment
        cls.get_environment = staticmethod(get_environment)

        def restore():
            os.environ.pop("VITA_MODEL_CONFIG_PATH", None)
            if cls._prior_env is not None:
                os.environ["VITA_MODEL_CONFIG_PATH"] = cls._prior_env
            for name in [n for n in list(sys.modules) if n == "vita" or n.startswith("vita.")]:
                if name not in cls._prior_modules:
                    sys.modules.pop(name, None)
            sys.modules.update(cls._prior_modules)
            sys.path[:] = cls._prior_path
            cls._tmp.cleanup()

        cls.addClassCleanup(restore)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environments = {}

    def environment_factory(self, arm):
        environment = self.get_environment(db=deepcopy(payment_precondition_db()))
        self.environments[arm] = environment
        return environment

    def payment_fixture(self, modes=None):
        return build_pair_run(
            self.tmp.name,
            update_modes=modes or {"rewrite": "pay_precondition",
                                   "erratum": "pay_precondition"},
            environment_factory=self.environment_factory)

    @staticmethod
    def tool_results(trace, name):
        return [e["result"] for e in trace
                if e["kind"] == "client_tool_result" and e["call"]["name"] == name]

    def test_a_recovered_error_return_passes_the_full_audit(self):
        fixture = self.payment_fixture()
        result = fixture["result"]
        self.assertEqual(result["status"], "RE_PAIR_COMPLETED_AUDIT_PENDING")
        self.assertIsNone(result["task_success"])
        self.assertIsNone(result["scientific_result"])
        self.assertEqual(codes(fixture), [], codes(fixture))
        for arm in ARMS:
            trace = result["arms"][arm]["bridge_trace"]
            payments = self.tool_results(trace, "pay_delivery_order")
            self.assertEqual([r["status"] for r in payments], ["error", "success"], arm)
            self.assertEqual(payments[0]["tool_return"],
                             f"Error: Order {PAY_PRECONDITION_MISSING_ORDER} not found")
            self.assertEqual([r["status"] for r in self.tool_results(
                trace, "create_delivery_order")], ["success"], arm)
            orders = self.environments[arm].tools.db.orders
            self.assertEqual(len(orders), 1)
            self.assertEqual(list(orders.values())[0].status, "paid")
            self.assertNotIn(PAY_PRECONDITION_MISSING_ORDER, orders)

    def test_a_tampered_error_return_is_rejected(self):
        fixture = self.payment_fixture()

        def mutate(rows):
            for row in rows:
                inner = row.get("event") or {}
                if (inner.get("kind") == "client_tool_result"
                        and (inner.get("call") or {}).get("name") == "pay_delivery_order"
                        and inner["result"]["status"] == "error"):
                    inner["result"] = dict(inner["result"],
                                           tool_return="Error: Order tampered not found")
                    return
        mutate_lifecycle(fixture, mutate)
        self.assertTrue(codes(fixture), "a tampered error return must be rejected")

    def test_a_deleted_error_return_is_rejected(self):
        fixture = self.payment_fixture()

        def mutate(rows):
            for index, row in enumerate(rows):
                inner = row.get("event") or {}
                if (inner.get("kind") == "client_tool_result"
                        and (inner.get("call") or {}).get("name") == "pay_delivery_order"
                        and inner["result"]["status"] == "error"):
                    rows.pop(index)
                    return

        mutate_lifecycle(fixture, mutate)
        self.assertTrue(codes(fixture), "a deleted error return must be rejected")

    def test_a_tampered_error_id_is_rejected(self):
        fixture = self.payment_fixture()

        def mutate(rows):
            for row in rows:
                inner = row.get("event") or {}
                if (inner.get("kind") == "client_tool_result"
                        and (inner.get("call") or {}).get("name") == "pay_delivery_order"
                        and inner["result"]["status"] == "error"):
                    inner["result"] = dict(inner["result"], tool_call_id="forged-id")
                    return

        mutate_lifecycle(fixture, mutate)
        self.assertTrue(codes(fixture), "a tampered error id must be rejected")

    def test_an_unremedied_error_is_not_packaged_as_success(self):
        """The model may legally stop after the error; that is not task success."""
        fixture = self.payment_fixture(modes={"rewrite": "pay_precondition_no_remedy",
                                              "erratum": "pay_precondition_no_remedy"})
        result = fixture["result"]
        self.assertIsNone(result["task_success"])
        self.assertIsNone(result["scientific_result"])
        for arm in ARMS:
            trace = result["arms"][arm]["bridge_trace"]
            payments = self.tool_results(trace, "pay_delivery_order")
            self.assertEqual([r["status"] for r in payments], ["error"], arm)
            self.assertEqual(self.tool_results(trace, "create_delivery_order"), [], arm)
            self.assertEqual(self.environments[arm].tools.db.orders, {})
            judge = result["arms"][arm]["judge"]
            self.assertEqual(judge["reward_info"], {"reward": 0.0})
            self.assertIsNone(judge["scientific_success"])
        self.assertEqual(codes(fixture), [], codes(fixture))


class PinnedBaselinePortabilityTests(unittest.TestCase):
    """The RE fixture helper must use the DECLARED pinned baseline.

    The deployment probe showed `multicall_manifest` falling back to
    `/tmp/ae-letta-QaM3ld/letta-v1` even when `AE_LETTA_SOURCE` named a real
    alternate tree, so the lab host could not build a manifest at all.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def alternate_baseline(self):
        target = Path(self.tmp.name) / "baseline"
        for name in ("letta/agents/letta_agent_v3.py", "letta/schemas/message.py"):
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(LETTA_BASELINE / name, destination)
        return target

    def test_the_declared_alternate_baseline_is_what_the_generator_uses(self):
        alternate = self.alternate_baseline()
        if alternate.resolve() == LETTA_BASELINE.resolve():
            self.skipTest("history default and alternate coincide")
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        with patch.object(sys.modules[__name__], "LETTA_SOURCE_OVERRIDE", str(alternate)), \
             patch.object(sys.modules[__name__], "LETTA_BASELINE", alternate):
            path = multicall_manifest(out, PATCHED_CHECKOUT)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(Path(record["baseline_checkout"]).resolve(), alternate.resolve())
        self.assertTrue(record["verification"]["offline_patch_verified"])

    def test_a_declared_missing_baseline_fails_instead_of_skipping(self):
        missing = Path("/nonexistent/ae-re-portability-baseline")
        with patch.object(sys.modules[__name__], "LETTA_SOURCE_OVERRIDE", str(missing)), \
             patch.object(sys.modules[__name__], "LETTA_BASELINE", missing):
            out = Path(self.tmp.name) / "out"
            out.mkdir()
            with self.assertRaises(RuntimeError) as caught:
                multicall_manifest(out, PATCHED_CHECKOUT)
        self.assertIn("pinned Letta baseline is missing", str(caught.exception))

    @staticmethod
    def resolve_without_a_declaration():
        """Re-import this module with `AE_LETTA_SOURCE` unset, as a lab host would.

        The fallback is bound at import time, so patching the module global would
        only test the patch. A fresh import of the real file exercises the real
        resolution expression with no declaration present.
        """
        declared = os.environ.pop("AE_LETTA_SOURCE", None)
        name = "test_ae_cloud_re_pair_nodeclared"
        try:
            spec = importlib.util.spec_from_file_location(name, Path(__file__))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
                return module.pinned_baseline()
            finally:
                sys.modules.pop(name, None)
        finally:
            if declared is not None:
                os.environ["AE_LETTA_SOURCE"] = declared

    def test_the_historical_default_still_resolves_when_nothing_is_declared(self):
        """No declaration: the checked-in extraction, or the historical path.

        The machine-specific `/tmp` checkout is still the LAST resort (a lab host
        that prepared it must keep working); on a plain checkout the pinned archive
        extraction is used instead of a path that does not exist.
        """
        resolved = self.resolve_without_a_declaration()
        checked_in = ROOT / ".ae-verify-src/letta-v1"
        if checked_in.is_dir():
            self.assertEqual(resolved, checked_in)
            self.assertTrue((resolved / "letta/agents/letta_agent_v3.py").is_file())
        else:
            self.assertEqual(resolved, Path("/tmp/ae-letta-QaM3ld/letta-v1"))


if __name__ == "__main__":
    unittest.main()
