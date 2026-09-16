#!/usr/bin/env python3
"""Real t4 execution probe on the LOCAL loopback model (query -> select -> order -> pay).

    .venv-vita/bin/python -B scripts/ae_01_local_t4_probe.py \
        --stage plan     --vita-source vendor/vita/source --output-dir deployment/runs/ae-local-t4-<UTC>
    .venv-vita/bin/python -B scripts/ae_01_local_t4_probe.py \
        --stage preflight --vita-source vendor/vita/source --output-dir deployment/runs/ae-local-t4-<UTC>
    .venv-vita/bin/python -B scripts/ae_01_local_t4_probe.py \
        --stage run      --vita-source vendor/vita/source --output-dir deployment/runs/ae-local-t4-<UTC>

What this measures (nothing more): with the CURRENT preference snapshot, the public t4
instruction and the FULL native tool set, can the local model search, select the right
product, create the order and pay it - exactly once, in a new directory, on the loopback
service. It is a CURRENT-STATE EXECUTION DIAGNOSTIC: the dataset history is never sent,
so a pass says nothing about recognising an update from history and nothing about long
contexts, and it is not the R/E protocol.

Input discipline, all recorded in `plan.json`:

* the agent sees ONLY the current preference snapshot (built from t3 facts + the public
  t4 change evidence by `ae_capability.build_capability_inputs`), the user profile, the
  native t4 instruction and the native domain policy plus ALL native delivery tool
  schemas;
* `target_product_ids`, `evaluation_criteria` and the background store database are NEVER
  given to the agent, no target product id is hinted, and the tool set is not narrowed to
  the correct store or filtered to remove wrong candidates - the product specification
  must come from a real tool return;
* the user simulator sees only what the user knows: the public instruction, the profile
  and the conversation; it is never shown the private oracle, the target product ids or
  the evaluation criteria, and it is instructed not to invent new material.

Runtime discipline:

* endpoint fixed to the loopback ``http://127.0.0.1:8001`` and model fixed to
  ``Qwen/Qwen3.5-9B``; both are re-validated as loopback before any send and there is no
  parameter that can point either role at a cloud origin or another model;
* explicit non-thinking (``chat_template_kwargs.enable_thinking = false``) and
  temperature 0 for EVERY model role;
* at most 12 agent requests and at most 4 user-simulator exchanges, ALL model POSTs
  counted together against a hard 16; no retry, no concurrency, 180 s request timeout;
  agent output 2048, auxiliary (user simulator) output 4096;
* before EACH send the request is counted on the same-model vLLM ``/tokenize`` with the
  same messages/tools/chat_template kwargs, plus the generation reserve, and compared
  with the service's real window. If the tokenizer is unavailable, or the shape cannot be
  counted equivalently, or the INITIAL shape does not fit, the run is blocked without any
  inference and the missing piece is reported. A later request that does not fit stops the
  run as `capacity_blocked` with the evidence kept: nothing is summarised, trimmed, schema
  reduced or guessed with bytes/4, and the service window is not restarted or widened here;
* tool returns go over the wire under the same 26214-character guard the driver uses; the
  COMPLETE raw return is kept next to the sent text and any truncation is marked, so a
  task that fails for lack of truncated information is not reported as a clean model
  failure;
* uncertain tool outcome, network failure, length truncation and budget exhaustion each
  stop the run and keep their evidence; the run never loops until it passes.

The verdict is deterministic and private: `ae_capability.diagnose_orders` over the actual
final native order records, with this task's target product and sugar. No model judge.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The native `UserSimulator` calls the module-global `generate`, which is why the adapted
#: simulator redirects that name to `_counted_user_generate` for the duration of one turn.
#: It is a module-level NAME (not a lambda) so assigning it to a module works on every
#: Python build.
_counted_user_generate = None


def _set_counted_user_generate(function) -> None:
    """Point the native flow's module-global `generate` at the counted transport."""
    global _counted_user_generate
    _counted_user_generate = function

SCHEMA_VERSION = "ae-local-t4-probe-0.1"
PURPOSE = "local_current_state_t4_execution_diagnostic"
ENDPOINT = "http://127.0.0.1:8001"
TOKENIZE_PATH = "/tokenize"
CHAT_PATH = "/v1/chat/completions"
MODEL_NAME = "Qwen/Qwen3.5-9B"
IO_TIMEOUT_SECONDS = 180.0
#: The window is READ from the running service; this is only the answer to "what if the
#: service does not tell us" - which blocks the run instead of assuming a number.
SERVICE_WINDOW_FALLBACK = None
MODELS_PATH = "/v1/models"
AGENT_MAX_REQUESTS = 12
AUX_MAX_REQUESTS = 4
TOTAL_MAX_POSTS = 16
AGENT_OUTPUT_TOKENS = 2048
AUX_OUTPUT_TOKENS = 4096
AGENT_TEMPERATURE = 0.0
AUX_TEMPERATURE = 0.0
MAX_TOOL_RETURN_CHARS = 26214
THINKING_FIELD = "enable_thinking"
THINKING_VALUE = False
STOP_MARKER = "###STOP###"
#: This task's private oracle values. They are NEVER placed in the agent messages.
TARGET_PRODUCT_ID = "S17791041622763865_P00011"
EXPECTED_SUGAR = "7分糖"
USER_ID = "U000828"
TASK_NUMBER = 4
TASK_ID = "sub_U000828_4"
WORK_ADDRESS_KEY = "工作地址"

STATUS_PLAN = "PLAN_FROZEN"
STATUS_PREFLIGHT_OK = "PREFLIGHT_READY"
STATUS_PREFLIGHT_BLOCKED = "PREFLIGHT_CAPACITY_BLOCKED"
STATUS_PASSED = "LOCAL_T4_PROBE_PASSED"
STATUS_INCOMPLETE = "LOCAL_T4_TASK_INCOMPLETE"
STATUS_CAPACITY_BLOCKED = "CAPACITY_BLOCKED"
STATUS_LENGTH = "MODEL_OUTPUT_TRUNCATED_INCOMPLETE"
STATUS_UNCERTAIN = "UNCERTAIN_TOOL_OUTCOME_INVALID"
STATUS_INVALID = "INVALID"

CODE_FILES = ("ae_adapter.py", "ae_capability.py", "ae_inputs.py", "ae_multiturn_capacity.py",
              "ae_task_run.py", "scripts/ae_01_native_order_probe.py",
              "scripts/ae_01_local_t4_probe.py")


# --------------------------------------------------------------------------- loading


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_native():
    return _load_module("ae_native_order_probe_for_t4",
                        ROOT / "scripts/ae_01_native_order_probe.py")


def _load_flow():
    """The shared t4 flow: one implementation for every transport.

    The loop lives in `t4_flow.py` so a second entry point (the cloud screening probe) can
    reuse it instead of copying it; this module keeps its own plan, preflight, transport and
    diagnosis wiring.
    """
    return _load_module("ae_t4_flow", ROOT / "t4_flow.py")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def ensure_output_dir(path):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"output already exists; no resume or overwrite: {path}")
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


# ------------------------------------------------------------------- loopback + config


def validate_local_endpoint(endpoint: str = ENDPOINT) -> str:
    """Refuse anything that is not plain HTTP on a loopback host. No cloud, ever."""
    parts = urlsplit(endpoint)
    if parts.scheme != "http":
        raise ValueError(f"the local probe speaks plain HTTP on loopback only: {endpoint!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment or not parts.port:
        raise ValueError(f"the local endpoint must be a bare origin with a port: {endpoint!r}")
    host = (parts.hostname or "").strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"the local endpoint must be a numeric loopback address: {endpoint!r}") from None
    if not address.is_loopback:
        raise ValueError(f"refusing a non-loopback address: {endpoint!r}")
    return endpoint


def ensure_vita_model_config(endpoint: str = ENDPOINT) -> str:
    """A run-local models.yaml pointing Vita's own LLM helper at the LOCAL endpoint.

    `VITA_MODEL_CONFIG_PATH` is respected when the caller already declared it; otherwise a
    temporary EMPTY-key config is written. The key is literally "EMPTY": the loopback
    service needs no credential and this probe never reads one.
    """
    if os.environ.get("VITA_MODEL_CONFIG_PATH"):
        return os.environ["VITA_MODEL_CONFIG_PATH"]
    import tempfile
    directory = tempfile.mkdtemp(prefix="ae-t4-probe-vita-")
    path = Path(directory) / "models.yaml"
    path.write_text(json.dumps({"default": {}, "models": [
        {"name": MODEL_NAME, "base_url": endpoint + "/v1", "api_key": "EMPTY"}]}),
        encoding="utf-8")
    os.environ["VITA_MODEL_CONFIG_PATH"] = str(path)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    return str(path)


# ------------------------------------------------------------------- public projection


#: Parsing the 131 MB dataset is expensive; one parse per (path, mtime, size) in-process.
_SAMPLE_CACHE: dict = {}


def _prepared_sample(ae_inputs, dataset: Path):
    key = (str(dataset), dataset.stat().st_mtime_ns, dataset.stat().st_size)
    if key not in _SAMPLE_CACHE:
        _SAMPLE_CACHE.clear()
        _SAMPLE_CACHE[key] = ae_inputs.prepare_sample(dataset)
    return _SAMPLE_CACHE[key]


def prepare_public(vita_source: Path):
    """The verified public projection plus the private environment, kept apart."""
    import ae_inputs
    import ae_capability
    dataset = Path(os.environ.get("AE_VITA_DATASET",
                                  ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
    if not dataset.is_file():
        raise FileNotFoundError(f"the dataset is absent: {dataset}")
    sample = _prepared_sample(ae_inputs, dataset)
    numbers = sorted(t["number"] for t in sample["tasks"])
    if TASK_NUMBER not in numbers:
        raise ValueError(f"the dataset carries no t{TASK_NUMBER}")
    # `build_capability_inputs` requires EXACTLY the [4, 5] public projection, while
    # `prepare_sample` returns the whole t4..t12 run. Project the public shape only: no
    # frozen constant is touched and no private field is read.
    projection = {"tasks": [t for t in sample["tasks"] if t["number"] in (TASK_NUMBER, TASK_NUMBER + 1)],
                  "initial_facts": sample["initial_facts"],
                  "initial_profile": sample["initial_profile"],
                  "source": sample["source"]}
    inputs = ae_capability.build_capability_inputs(projection)
    private = sample["private_tasks"].get(TASK_ID)
    if not isinstance(private, dict):
        raise ValueError(f"the dataset carries no private task {TASK_ID}")
    return {"sample": sample, "inputs": inputs, "private": private,
            "dataset_path": dataset, "capability": ae_capability}


def build_native(public, vita_source: Path):
    """The REAL native environment, bindings and message formatting for this task."""
    import ae_task_run
    ensure_vita_model_config()
    source = Path(vita_source)
    if not (source / "src/vita").is_dir():
        raise RuntimeError(f"fixed Vita source is missing: {source}")
    sys.path.insert(0, str(source / "src"))
    from vita.domains.delivery.environment import get_environment
    from vita.utils.llm_utils import format_messages
    environment = get_environment(db=deepcopy(public["private"]["environment"]))
    bindings = ae_task_run.environment_bindings(environment)
    if not bindings:
        raise RuntimeError("the native environment exposes no tools")
    tools = [{"type": "function", "function": deepcopy(binding.schema)}
             for binding in bindings.values()]
    from vita.data_model.message import (AssistantMessage, SystemMessage, ToolCall,
                                         ToolMessage, UserMessage)
    return {"environment": environment, "bindings": bindings, "tools": tools,
            "format_messages": format_messages, "policy": environment.policy,
            "messages": {"SystemMessage": SystemMessage, "UserMessage": UserMessage,
                         "AssistantMessage": AssistantMessage, "ToolMessage": ToolMessage,
                         "ToolCall": ToolCall}}


def agent_system_message(native, public) -> str:
    """The native agent policy + the current-state snapshot, and nothing private."""
    inputs = public["inputs"]
    current = inputs["facts_current"]
    facts = "\n".join(f"- [{v['category']}] {v['content']}"
                      for v in current.values() if isinstance(v, dict))
    return (
        "你是任务环境中的个人助手。当前用户资料和长期偏好已经给出；本轮不允许修改偏好，"
        "也没有记忆更新工具。\n"
        "请依据当前已知偏好和当前任务指令，使用可用工具真实完成操作；需要多步时逐步调用工具。\n"
        "信息不足时可以向当前用户提问，等待真实回复后再继续。\n"
        "工具的真实回包是唯一依据：没有成功回包不得声称已完成。\n\n"
        "# 当前用户资料\n" + json.dumps(inputs["profile"], ensure_ascii=False) + "\n\n"
        "# 当前已知长期偏好（当前状态快照）\n" + facts + "\n\n"
        "# 环境\n- 当前时间：" + str(inputs["task"]["current_time"]) + "\n\n"
        "# 工具使用规范：\n"
        "- 当用户需求需要调工具来完成时，先判断是否已知全部参数信息，如果已知则抽取相应参数，"
        "否则询问用户相关参数值\n"
        "- 当用户无法提供相关信息时，首先通过工具获取相关信息\n"
        "- 参考Precondition和Postcondition完成任务\n\n"
        "# 对话规范\n"
        "- 仅利用上文已有信息，禁止无根据地构造信息并回复用户\n"
        "- 以完成用户需求为目标，禁止发散性引导用户提出新需求\n"
        "- 完成用户的任务需求后询问用户是否还有其他需求，如果用户表示没有，生成 '###STOP###' "
        "标记来结束对话\n"
    )


def task_user_message(public) -> str:
    """The public t4 instruction, as the dataset gives it."""
    return public["inputs"]["task"]["instruction"]


def inputs_profile(public) -> dict:
    return public["inputs"]["profile"]


# ------------------------------------------------------------------------ capacity
#
# The service window is a READ value, never a constant and never a silent default. It is
# resolved ONCE per run from the running service, and a run that cannot read it, cannot
# tokenize through the service, or sees a different model identity REFUSES to generate.
# Nothing here restarts, widens or re-provisions the service.
#
# Offline note: this workspace has only the Qwen3-30B target tokenizer assets, NOT the 9B
# model's own assets, so no offline 9B token count is possible here and none is faked. The
# counts this probe gates on are the SERVICE's, taken on the exact outgoing chat body.


class GateRefused(RuntimeError):
    """A request that the send gate refused before any generation happened."""

    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else str(code))


def chat_body(messages, tools, *, max_tokens, temperature=0.0) -> dict:
    """The ONE outgoing chat body shape, so counting and sending cannot diverge."""
    body = {"model": MODEL_NAME, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {THINKING_FIELD: THINKING_VALUE}}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = False
    return body


def tokenize_body(chat: dict) -> dict:
    """The CHAT-input shape `/tokenize` must receive: same messages/tools/kwargs.

    The service renders the chat template itself with the same kwargs and
    `add_generation_prompt=true`; the probe never pre-renders a prompt string with another
    model's template.
    """
    return {"model": chat["model"], "messages": chat["messages"],
            "add_generation_prompt": True,
            "chat_template_kwargs": chat["chat_template_kwargs"],
            **({"tools": chat["tools"]} if "tools" in chat else {})}


class LocalTransport:
    """Every POST this probe makes to the fixed service: one journal, no retry.

    Every CHAT request goes through `send`, whose gate counts the ACTUAL outgoing body on
    the service itself and compares it with the service's own window, adding THAT
    request's reserve. `tokenize` calls are counted separately from the 16-request
    inference budget but still consume the service post budget.
    """

    def __init__(self, endpoint: str = ENDPOINT, opener=None, clock=time.monotonic,
                 max_inference_posts: int = TOTAL_MAX_POSTS):
        self.endpoint = validate_local_endpoint(endpoint)
        self.opener = opener if opener is not None else build_opener(ProxyHandler({}))
        self.clock = clock or time.monotonic
        self.journal = []
        #: The unified INFERENCE budget (agent + user simulator). Tokenize and metadata
        #: reads are counted and reported separately; they are not inference calls and
        #: must not consume the model-call budget.
        self.max_inference_posts = max_inference_posts
        self.attempted = 0
        self.responses_received = 0
        self.by_role = {"agent": 0, "user": 0, "tokenize": 0, "models": 0}
        #: Resolved once, from the service. `None` until then; never a built-in default.
        self.service_window = None
        self.window_source = None
        self.model_metadata = None
        self.gate_refusals = []

    @property
    def inference_posts(self) -> int:
        return self.by_role["agent"] + self.by_role["user"]

    def descriptor(self) -> dict:
        """What this transport knows about itself, for the run record.

        The shared flow copies this verbatim, so a transport that cannot count tokens (a
        cloud API without `/tokenize`) reports that instead of a fabricated number.
        """
        return {"service_window_tokens": self.service_window,
                "service_window_source": self.window_source,
                "capacity_basis": "service_max_model_len_and_tokenize" if self.service_window
                else "unknown"}

    def budget_left(self, *, inference: bool) -> bool:
        return self.inference_posts < self.max_inference_posts

    # ------------------------------------------------------------------ raw transport

    def _post(self, path, payload, role):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.endpoint + path, data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
        started = self.clock()
        self.attempted += 1
        self.by_role[role] = self.by_role.get(role, 0) + 1
        try:
            with self.opener.open(request, timeout=IO_TIMEOUT_SECONDS) as response:
                status, raw = response.code, response.read()
        except HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001 - the status alone is still evidence
                raw = b""
            status = exc.code
        except Exception as exc:  # noqa: BLE001 - a transport failure is evidence, not a retry
            self.journal.append({"role": role, "path": path, "attempted": True,
                                 "response_received": False,
                                 "error_type": type(exc).__name__,
                                 "error_detail": str(exc)[:500],
                                 "elapsed_seconds": round(self.clock() - started, 3)})
            return None, None, {"error_type": type(exc).__name__, "error_detail": str(exc)[:500]}
        self.responses_received += 1
        self.journal.append({"role": role, "path": path, "attempted": True,
                             "response_received": True, "http_status": status,
                             "response_sha256": _sha256_bytes(raw),
                             "elapsed_seconds": round(self.clock() - started, 3)})
        return status, raw, None

    def _get(self, path, role):
        request = Request(self.endpoint + path, method="GET")
        started = self.clock()
        self.attempted += 1
        self.by_role[role] = self.by_role.get(role, 0) + 1
        try:
            with self.opener.open(request, timeout=IO_TIMEOUT_SECONDS) as response:
                status, raw = response.code, response.read()
        except HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                raw = b""
            status = exc.code
        except Exception as exc:  # noqa: BLE001
            self.journal.append({"role": role, "path": path, "method": "GET",
                                 "attempted": True, "response_received": False,
                                 "error_type": type(exc).__name__,
                                 "error_detail": str(exc)[:500],
                                 "elapsed_seconds": round(self.clock() - started, 3)})
            return None, None, {"error_type": type(exc).__name__, "error_detail": str(exc)[:500]}
        self.responses_received += 1
        self.journal.append({"role": role, "path": path, "method": "GET", "attempted": True,
                             "response_received": True, "http_status": status,
                             "response_sha256": _sha256_bytes(raw),
                             "elapsed_seconds": round(self.clock() - started, 3)})
        return status, raw, None

    # ------------------------------------------------------------------ service reads

    @staticmethod
    def _valid_window(value):
        return type(value) is int and value > 0

    def _window_from_tokenize(self, parsed):
        for key in ("max_model_len", "max_model_length"):
            if self._valid_window(parsed.get(key)):
                return parsed[key]
        return None

    def _models_metadata(self):
        """Identity plus window from the service's own model listing."""
        if self.model_metadata is not None:
            return self.model_metadata
        status, raw, failure = self._get(MODELS_PATH, "models")
        record = {"path": MODELS_PATH, "http_status": status, "failure": failure}
        if failure is None and status == 200:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                entries = parsed.get("data")
                record["models"] = [entry.get("id") for entry in entries
                                    if isinstance(entry, dict)] if isinstance(entries, list) else []
                match = next((entry for entry in (entries or [])
                              if isinstance(entry, dict) and entry.get("id") == MODEL_NAME), None)
                record["matched_model"] = MODEL_NAME if match else None
                record["max_model_len"] = (match.get("max_model_len")
                                           if match and self._valid_window(match.get("max_model_len"))
                                           else None)
                record["unparsed_max_model_len"] = (match.get("max_model_len")
                                                    if match else None)
            except (ValueError, AttributeError, TypeError) as exc:
                record["parse_error"] = f"{type(exc).__name__}: {exc}"
        self.model_metadata = record
        return record

    def declare_window(self):
        """Read the window (and the model identity) from the service, once.

        Raises `GateRefused` when the service cannot be read, does not list the declared
        model, or does not give a positive integer window - in which case NO request is
        sent for generation.
        """
        if self.service_window is not None:
            return self.service_window
        metadata = self._models_metadata()
        if metadata.get("failure") is not None or metadata.get("http_status") != 200:
            raise GateRefused("service_models_unavailable",
                              f"{MODELS_PATH} -> {metadata.get('http_status')} "
                              f"{metadata.get('failure')}")
        if metadata.get("matched_model") != MODEL_NAME:
            raise GateRefused("service_model_identity_mismatch",
                              f"{MODEL_NAME} not among {metadata.get('models')}")
        window = metadata.get("max_model_len")
        if not self._valid_window(window):
            raise GateRefused("service_window_unreadable",
                              f"max_model_len={metadata.get('unparsed_max_model_len')!r}")
        self.service_window = window
        self.window_source = "service_models_metadata"
        return window

    def tokenize_chat(self, chat: dict):
        """Count the EXACT chat body on the service. Returns (count|None, record)."""
        payload = tokenize_body(chat)
        status, raw, failure = self._post(TOKENIZE_PATH, payload, "tokenize")
        record = {"request": payload, "http_status": status, "failure": failure,
                  "sends_the_same_messages_and_tools_as_the_chat":
                      payload["messages"] == chat["messages"]
                      and payload.get("tools") == chat.get("tools")}
        if failure is not None:
            record.update(ok=False, reason="tokenize_request_failed")
            return None, record
        record["response_body"] = raw[:2000].decode("utf-8", "replace")
        if status != 200:
            record.update(ok=False, reason=f"tokenize_http_{status}")
            return None, record
        try:
            parsed = json.loads(raw.decode("utf-8"))
            count = parsed["count"]
            if not self._valid_window(count):
                raise ValueError(f"bad count {count!r}")
        except (ValueError, KeyError, TypeError) as exc:
            record.update(ok=False, reason="tokenize_unparseable",
                          detail=f"{type(exc).__name__}: {exc}")
            return None, record
        observed = parsed.get("model")
        record.update(ok=True, count=count, response_model=observed,
                      window_from_tokenize=self._window_from_tokenize(parsed))
        if observed is not None and observed != MODEL_NAME:
            record.update(ok=False, reason="tokenize_model_identity_mismatch",
                          detail=f"service answered for {observed!r}")
            return None, record
        return count, record

    # ------------------------------------------------------------------- the send gate

    def send(self, messages, tools, *, role, max_tokens, temperature=0.0):
        """Count -> gate -> send. Raises `GateRefused` BEFORE any generation if refused."""
        chat = chat_body(messages, tools, max_tokens=max_tokens, temperature=temperature)
        window = self.declare_window()
        if not self.budget_left(inference=True):
            raise GateRefused("total_post_budget_exhausted",
                              f"inference posts {self.inference_posts}/"
                              f"{self.max_inference_posts} used")
        count, counted = self.tokenize_chat(chat)
        gate = {"role": role, "count": count, "reserve": max_tokens,
                "window": window, "window_source": self.window_source,
                "counted": {k: v for k, v in counted.items() if k != "request"}}
        if self._valid_window(counted.get("window_from_tokenize"))                 and counted["window_from_tokenize"] != window:
            gate.update(fits=False)
            self.gate_refusals.append(gate)
            raise GateRefused("service_window_changed_between_reads",
                              f"{window} -> {counted['window_from_tokenize']}")
        if count is None:
            gate.update(fits=None, refused=counted.get("reason"))
            self.gate_refusals.append(gate)
            raise GateRefused("tokenize_unavailable", counted)
        total = count + max_tokens
        gate["total"] = total
        gate["fits"] = total <= window
        if not gate["fits"]:
            self.gate_refusals.append(gate)
            raise GateRefused("capacity_blocked_before_send",
                              f"prompt {count} + reserve {max_tokens} > window {window}")
        self.gate_refusals.append(gate)
        status, raw, failure = self._post(CHAT_PATH, chat, role)
        return status, raw, failure, gate

# --------------------------------------------------------------------- tool returns


def guarded_tool_return(text: str):
    """The same 26214-character guard the driver uses, with the truncation marked."""
    raw = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    if len(raw) <= MAX_TOOL_RETURN_CHARS:
        return raw, {"truncated": False, "raw_chars": len(raw), "sent_chars": len(raw)}
    sent = raw[:MAX_TOOL_RETURN_CHARS]
    return sent, {"truncated": True, "raw_chars": len(raw), "sent_chars": len(sent),
                  "guard_chars": MAX_TOOL_RETURN_CHARS,
                  "note": ("the wire text is truncated at the fixed guard; the complete raw "
                           "return is kept beside it, so a failure caused by missing "
                           "truncated information is not reported as a clean model failure")}


# --------------------------------------------------------------------------- planning


def build_plan(vita_source: Path, endpoint: str = ENDPOINT) -> dict:
    native_module = _load_native()
    public = prepare_public(vita_source)
    inputs = public["inputs"]
    private = public["private"]
    return {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": STATUS_PLAN,
        "diagnostic_only": True, "network_called": False, "model_called": False,
        "task_success": None, "scientific_result": None,
        "endpoint": {"origin": validate_local_endpoint(endpoint), "chat_path": CHAT_PATH,
                     "tokenize_path": TOKENIZE_PATH, "loopback_only": True,
                     "model": MODEL_NAME, "service_window_tokens": None,
                     "window_is_read_from_the_service": True,
                     "window_is_a_hard_capacity_guarantee": False,
                     "window_note": ("the window is READ from the running service at run "
                                     "time (`/v1/models` max_model_len, or the tokenize "
                                     "answer); it is never a constant in this probe")},
        "case": {
            "user_id": USER_ID, "task_number": TASK_NUMBER, "subtask_id": TASK_ID,
            "domain": inputs["task"]["domain"], "current_time": inputs["task"]["current_time"],
            "instruction": inputs["task"]["instruction"],
            "dataset_history_sent": False,
            "current_state_execution_diagnostic": True,
            "not_the_original_benchmark": True, "not_r_e": True,
            "facts_current": deepcopy(inputs["facts_current"]),
            "facts_control": deepcopy(inputs["facts_control"]),
            "profile": deepcopy(inputs["profile"]),
            "preference_edit": deepcopy(inputs["preference_edit"]),
            "evidence_text": inputs["preference_edit"]["evidence_text"],
            "history_record_count": len(inputs["task"]["history"]),
            "history_records_sent": 0,
        },
        "private_oracle": {
            "target_product_id": TARGET_PRODUCT_ID,
            "expected_sugar": EXPECTED_SUGAR,
            "work_address": inputs["profile"][WORK_ADDRESS_KEY],
            "injected_into_agent": False, "used_for_scoring_only": True,
            "target_product_ids_in_agent_messages": False,
            "evaluation_criteria_in_agent_messages": False,
            "store_database_in_agent_messages": False,
        },
        "exposed_tools": "ALL native delivery tools, unfiltered and un-narrowed",
        "budget": {"agent_max_requests": AGENT_MAX_REQUESTS,
                   "user_simulator_max_exchanges": AUX_MAX_REQUESTS,
                   "total_model_posts_max": TOTAL_MAX_POSTS,
                   "agent_output_tokens": AGENT_OUTPUT_TOKENS,
                   "auxiliary_output_tokens": AUX_OUTPUT_TOKENS,
                   "temperature": AGENT_TEMPERATURE,
                   "thinking": {THINKING_FIELD: THINKING_VALUE},
                   "thinking_placement": "chat_template_kwargs",
                   "io_timeout_seconds": IO_TIMEOUT_SECONDS,
                   "max_tool_return_chars": MAX_TOOL_RETURN_CHARS,
                   "no_retry": True, "no_concurrency": True,
                   "no_resume_same_directory": True},
        "capacity_gate": {
            "counting": "the same-model vLLM /tokenize on the same messages/tools/"
                        "chat_template_kwargs plus the generation reserve, before EACH send",
            "initial_shape_gate": "the run is refused before any inference when the "
                                  "tokenizer is unavailable or the initial shape does not fit",
            "later_request_policy": "a request that does not fit stops the run as "
                                    "CAPACITY_BLOCKED with the evidence kept",
            "forbidden": ["summarising", "trimming", "reducing the schemas",
                          "raising the tool-return guard", "guessing with bytes/4",
                          "restarting or widening the service window from this task"],
        },
        "decision_rules": [
            "the verdict is the deterministic `ae_capability.diagnose_orders` over the "
            "actual final native order records; there is no model judge",
            "a pass means this one run executed the task correctly in the CURRENT state; "
            "it does not prove history-based update recognition or long-context ability",
            "uncertain tool outcome, network failure, length truncation and budget "
            "exhaustion each stop the run and keep their evidence; the run never loops "
            "until it passes",
        ],
        "provenance": {
            "code_sha256": {name: _sha256_file(ROOT / name) for name in CODE_FILES},
            "native_probe_schema_version": native_module.SCHEMA_VERSION,
            "python": sys.version, "python_executable": sys.executable,
            "dataset": {"path": str(public["dataset_path"]),
                        "sha256": _sha256_file(public["dataset_path"]),
                        "revision": inputs["preference_edit"]["evidence_ref"]},
        },
    }


def preflight(vita_source: Path, endpoint: str = ENDPOINT, *, opener=None, transport=None) -> dict:
    """Zero-inference readiness: the service, its window, and the INITIAL shape.

    It contacts the service ONLY to read the model listing/window and to tokenize the
    initial chat input. NO chat completion is ever sent, so an unfit initial shape is
    refuted BEFORE any generation. It measures the INITIAL request only: it says nothing
    about the later requests of the task, which are gated individually at send time.
    """
    public = prepare_public(vita_source)
    native = build_native(public, vita_source)
    transport = transport if transport is not None else LocalTransport(endpoint, opener=opener)
    usage_before = transport.attempted
    system = agent_system_message(native, public)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": task_user_message(public)}]
    chat = chat_body(messages, native["tools"], max_tokens=AGENT_OUTPUT_TOKENS,
                     temperature=AGENT_TEMPERATURE)
    report = {
        "schema_version": SCHEMA_VERSION, "stage": "preflight", "purpose": PURPOSE,
        "network_called": transport.attempted > 0, "model_inference_called": False,
        "endpoint": validate_local_endpoint(endpoint), "model": MODEL_NAME,
        "counting": ("the service's own /tokenize on the exact outgoing chat input "
                     "(messages + tools + chat_template_kwargs, add_generation_prompt=true); "
                     "no local template render and no local tokenizer"),
        "generation_reserve_tokens": AGENT_OUTPUT_TOKENS,
        "auxiliary_reserve_tokens": AUX_OUTPUT_TOKENS,
        "tools_exposed": len(native["tools"]),
        "initial_messages": [{"role": m["role"], "chars": len(m["content"])} for m in messages],
        "status": STATUS_INVALID, "capacity_blocked": False, "missing": [],
        "scope": ("the INITIAL agent request only; the later requests of the task are NOT "
                  "measured here and are gated one by one at send time"),
        "claims_full_task_fits": False,
    }
    try:
        window = transport.declare_window()
        report["service_window_tokens"] = window
        report["window_source"] = transport.window_source
        report["service_model_metadata"] = transport.model_metadata
        report["service_window_is_read_not_assumed"] = True
        report["window_is_a_hard_capacity_guarantee"] = False
    except GateRefused as exc:
        report["status"] = STATUS_PREFLIGHT_BLOCKED
        report["capacity_blocked"] = True
        report["service_window_tokens"] = None
        report["service_model_metadata"] = transport.model_metadata
        report["missing"].append("service_window")
        report["block_reason"] = f"{exc.code}: {exc.detail}"
        report["transport_journal"] = transport.journal[usage_before:]
        return report
    count, service = transport.tokenize_chat(chat)
    report["service_tokenize"] = {k: v for k, v in service.items() if k != "request"}
    if count is None:
        report["status"] = STATUS_PREFLIGHT_BLOCKED
        report["capacity_blocked"] = True
        report["missing"].append("service_tokenize")
        report["block_reason"] = (
            "the same-model tokenizer is unavailable or cannot count this shape; refusing "
            "to run rather than guessing with bytes/4")
    else:
        total = count + AGENT_OUTPUT_TOKENS
        report["service_tokenize_count"] = count
        report["initial_total_tokens"] = total
        report["fits"] = total <= window
        if report["fits"]:
            report["status"] = STATUS_PREFLIGHT_OK
            report["later_requests"] = ("NOT measured here: each later request (agent or "
                                        "user simulator) is counted and gated at send time")
        else:
            report["status"] = STATUS_PREFLIGHT_BLOCKED
            report["capacity_blocked"] = True
            report["block_reason"] = (
                f"the INITIAL request needs {total} tokens (prompt {count} + reserve "
                f"{AGENT_OUTPUT_TOKENS}) but the service window is {window}")
    report["transport_journal"] = transport.journal[usage_before:]
    return report


# -------------------------------------------------------------------------- execution


def _parse_reply(raw: bytes):
    """Alias: the parsing lives in the shared flow."""
    return _load_flow().parse_reply(raw)


def _tool_calls_of(message):
    """Alias: the parsing lives in the shared flow."""
    return _load_flow().tool_calls_of(message)


def execute_probe(*, vita_source: Path, output_dir: Path, endpoint: str = ENDPOINT,
                  opener=None, transport=None, clock=time.monotonic) -> dict:
    """One explicit RUN: plan -> preflight -> bounded agent/user loop."""
    native_module = _load_native()
    out = ensure_output_dir(output_dir)
    plan = build_plan(vita_source, endpoint)
    save(out / "plan.json", plan)
    # The run gets its OWN transport, so the preflight tokenize call is not billed to the
    # run's inference budget, while its result still travels in the readiness record.
    run_transport = (transport if transport is not None
                     else LocalTransport(endpoint, opener=opener, clock=clock))
    readiness = preflight(vita_source, endpoint, opener=opener)
    save(out / "preflight.json", readiness)
    public = prepare_public(vita_source)
    native = build_native(public, vita_source)
    save(out / "case.json", {
        "case": plan["case"], "private_oracle": plan["private_oracle"],
        "exposed_tool_names": sorted(native["bindings"]),
        "agent_system_message": agent_system_message(native, public),
        "agent_user_message": task_user_message(public),
        "user_simulator": {
            "native_class": "vita.user.user_simulator.UserSimulator",
            "system_prompt": ("built by the native simulator from the official user "
                              "guidelines + persona + instruction; it is recorded in "
                              "user-simulator.json at run time"),
            "persona": json.dumps(inputs_profile(public), ensure_ascii=False),
            "instructions": task_user_message(public),
            "given_the_private_oracle": False,
        },
    })
    result = {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": STATUS_INVALID,
        "diagnostic_only": True, "model": MODEL_NAME, "endpoint": validate_local_endpoint(endpoint),
        "task_success": None, "scientific_result": None, "oracle_passed": None,
        "model_called": False, "stage": "run", "service_window_tokens": None,
        "service_window_source": None, "gate_refusals": [],
        "requests_attempted": 0, "responses_received": 0,
        "requests_by_role": {}, "user_exchanges": 0, "steps": [],
        "baseline_order_ids": [], "created_order_ids": [], "paid_order_ids": [],
        "tool_returns": [], "capacity": [], "errors": [],
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        # Accounting is present on EVERY return path, including the blocked ones.
        "total_model_posts": 0, "total_model_posts_within_budget": True,
        "service_posts_total": 0, "service_posts_including_tokenize": 0,
        "wire_journal": [],
    }
    if readiness["status"] != STATUS_PREFLIGHT_OK:
        result["status"] = STATUS_PREFLIGHT_BLOCKED
        result["stop_reason"] = "preflight_" + str(readiness.get("block_reason"))
        result["preflight"] = readiness
        save(out / "result.json", result)
        return result

    environment, bindings = native["environment"], native["bindings"]
    tools = native["tools"]
    baseline = native_module.case_snapshot(environment)["orders"]
    result["baseline_order_ids"] = sorted(baseline)
    save(out / "initial-database.json", native_module.case_snapshot(environment))
    kinds = native["messages"]
    messages = [kinds["SystemMessage"](role="system",
                                       content=agent_system_message(native, public)),
                kinds["UserMessage"](role="user", content=task_user_message(public))]
    native_user = NativeUser(run_transport, public, vita_source, native=native)
    # The loop itself lives in t4_flow.py: this entry supplies its runtime, its constants
    # and its own result skeleton, and the shared flow does the rest.
    public_view = dict(public, work_address_key=WORK_ADDRESS_KEY, user_id=USER_ID,
                       target_product_id=TARGET_PRODUCT_ID, expected_sugar=EXPECTED_SUGAR,
                       status_passed=STATUS_PASSED, status_incomplete=STATUS_INCOMPLETE)
    helpers = {"native_module": native_module, "kinds": kinds,
               "status_invalid": STATUS_INVALID, "status_length": STATUS_LENGTH,
               "status_uncertain": STATUS_UNCERTAIN, "status_capacity": STATUS_CAPACITY_BLOCKED,
               "gate_refused": GateRefused, "guarded_tool_return": guarded_tool_return,
               "total_max_posts": TOTAL_MAX_POSTS, "save": save}
    try:
        return _load_flow().run_agent_loop(
            runtime={"transport": run_transport, "native_user": native_user},
            out=out, result=result, messages=messages, tools=native["tools"],
            bindings=native["bindings"], helpers=helpers, native=native,
            public=public_view, max_agent_requests=AGENT_MAX_REQUESTS,
            aux_max_requests=AUX_MAX_REQUESTS, agent_output_tokens=AGENT_OUTPUT_TOKENS,
            agent_temperature=AGENT_TEMPERATURE, stop_marker=STOP_MARKER)
    finally:
        # The flow writes its own artifacts; the ENTRY decides when the result is final.
        if (out / "result.json").exists():
            (out / "result.json").unlink()
        save(out / "result.json", result)


class _AdaptedUserSimulator:
    """The NATIVE UserSimulator with ONLY its transport replaced.

    `vita.user.user_simulator.UserSimulator._generate_next_message` builds its own OpenAI
    client through `vita.utils.llm_utils.generate`, which cannot pass a request through this
    probe's counter, capacity gate and inference budget. So `_generate_next_message` is
    overridden to call `self.probe_generate(...)` instead of the module-level `generate`,
    and everything else - the state handling, the native `state.flip_roles()`, the native
    `is_stop`, the native `UserMessage` construction and the native response post-processing
    - is the parent class's own code, unchanged.

    The override saves and restores `vita.utils.llm_utils.generate` around the parent call,
    so a real OpenAI client can never bypass the counted send gate, and no other caller in
    the process is affected.
    """

    def _generate_next_message(self, message, state):
        # The native module did `from vita.utils.llm_utils import generate`, so BOTH
        # bindings must be redirected for the parent's own code to use the counted
        # transport; both are restored afterwards, so no other caller is affected and a
        # real OpenAI client can never slip through.
        from vita.utils import llm_utils
        module = sys.modules.get("vita.user.user_simulator")
        if module is None:  # pragma: no cover - the class came from an importable module
            import vita.user.user_simulator as module
        counted = _counted_user_generate
        saved = ((llm_utils, llm_utils.generate), (module, module.generate))
        llm_utils.generate = counted
        module.generate = counted
        try:
            return super()._generate_next_message(message, state)
        finally:
            for holder, original in saved:
                holder.generate = original


def _native_assistant_message(parsed: dict):
    """The native SDK-response -> AssistantMessage conversion, applied to our body.

    `vita.utils.llm_utils.generate` builds an OpenAI client only to obtain this exact
    conversion. Rather than re-implement its post-processing, the counted transport feeds
    it the parsed body through the same steps it performs: `model_dump()`, then
    `choices[0].message.{role,content,tool_calls}` and `get_response_usage`.
    """
    from types import SimpleNamespace
    from vita.data_model.message import AssistantMessage, ToolCall
    from vita.utils.llm_utils import get_response_cost, get_response_usage

    choices = []
    for choice in parsed.get("choices") or []:
        message = choice.get("message") or {}
        tool_calls = [
            SimpleNamespace(id=call.get("id"), type=call.get("type") or "function",
                            function=SimpleNamespace(
                                name=(call.get("function") or {}).get("name"),
                                arguments=(call.get("function") or {}).get("arguments")))
            for call in message.get("tool_calls") or []
        ]
        choices.append(SimpleNamespace(finish_reason=choice.get("finish_reason"),
                                       message=SimpleNamespace(
                                           role=message.get("role") or "assistant",
                                           content=message.get("content"),
                                           tool_calls=tool_calls or None)))
    response_dict = SimpleNamespace(choices=choices, usage=parsed.get("usage"),
                                    model=parsed.get("model"), id=parsed.get("id"),
                                    object=parsed.get("object"),
                                    model_dump=lambda: parsed).model_dump()
    usage = get_response_usage(response_dict)
    cost = get_response_cost(usage or {}, MODEL_NAME)
    choice = response_dict["choices"][0]
    message = choice["message"]
    calls = [
        ToolCall(id=call.get("id"),
                 name=(call.get("function") or {}).get("name"),
                 arguments=(json.loads(call["function"]["arguments"])
                            if (call.get("function") or {}).get("arguments") else {}))
        for call in (message.get("tool_calls") or [])
    ] or None
    return AssistantMessage(role="assistant", content=message.get("content"),
                            tool_calls=calls, cost=cost, usage=usage,
                            raw_data=response_dict)


class NativeUser:
    """The NATIVE Vita user simulator, driven through this probe's counted send gate.

    It holds ONE native `UserState` for the whole task, so the conversation the simulator
    sees is the native one: the task instruction as a user turn, then each agent question
    and each user answer, put through the native `state.flip_roles()`.

    Each agent question enters the state EXACTLY ONCE, and the agent's tool-call turns are
    never part of the user's conversation (the native `flip_roles` refuses an assistant
    tool-call turn, and they are the agent's internal working).

    `next_message` goes through the parent's own `generate_next_message`, so the question is
    really delivered to the model and the answer really comes back through the native
    post-processing - it is not a re-implementation that looks similar.
    """

    def __init__(self, transport, public, vita_source: Path, native=None):
        self.native = native or build_native(public, vita_source)
        simulator = load_user_simulator(vita_source)
        # The adapted class is a REAL subclass of the native one: only
        # `_generate_next_message` is overridden, so every other native method (state,
        # flip_roles, is_stop, message conversion) is the parent's own code.
        counted_class = type("CountedUserSimulator",
                             (_AdaptedUserSimulator, simulator["UserSimulator"]), {})
        self.simulator = counted_class(
            tools=None, instructions=task_user_message(public),
            persona=json.dumps(public["inputs"]["profile"], ensure_ascii=False),
            llm=MODEL_NAME,
            llm_args={"temperature": AUX_TEMPERATURE, "max_tokens": AUX_OUTPUT_TOKENS})
        self.simulator.probe_generate = self.probe_generate
        self.transport = transport
        self.messages = simulator["messages"]
        #: The native task turn: the user opens by stating the request, exactly as the
        #: native flow does (see the probe README's native-flow note).
        self.state = self.simulator.get_init_state(
            [self.messages["UserMessage"](role="user", content=task_user_message(public))])
        #: The native flow calls the module-global `generate`; this name is redirected to
        #: `_counted_user_generate` (which forwards to the bound method below) for exactly
        #: the duration of one native turn.
        #: How many agent questions have been handed to the native simulator so far.
        self.delivered = 0
        def counted(**kwargs):
            return self.probe_generate(**kwargs)
        _set_counted_user_generate(counted)
        self.given_texts = {}
        self.simulator.probe_generate = self.probe_generate
        self.record = {"uses_native_user_simulator": True,
                       "native_class": "vita.user.user_simulator.UserSimulator",
                       "native_state_held_across_turns": True,
                       "drives_native_generate_next_message": True,
                       "substituted": "the module-level generate transport only",
                       "system_prompt_chars": len(self.simulator.system_prompt),
                       "task_turn_delivered": True,
                       "exchanges": []}

    # ------------------------------------------------------------------ transport

    def probe_generate(self, *, model=None, messages=None, tools=None, **kwargs):
        """The stand-in for `vita.utils.llm_utils.generate`: counted, gated, no retry.

        It is invoked as the MODULE GLOBAL `generate`, so it takes keyword arguments only
        and must not be a bound method.
        """
        wire = self.native["format_messages"](messages or [])
        try:
            status, raw, failure, gate = self.transport.send(
                wire, tools, role="user", max_tokens=AUX_OUTPUT_TOKENS,
                temperature=AUX_TEMPERATURE)
        except GateRefused as exc:
            # The ONLY pre-send refusal: nothing was generated at all.
            self.record.setdefault("refusals", []).append({"code": exc.code,
                                                           "detail": str(exc.detail)[:300]})
            raise _UserGenerationRefused(exc.code, exc.detail) from None
        if failure is not None:
            raise _UserGenerationFailed("user_request_not_answered", str(failure)[:300],
                                        sent=True)
        if status != 200:
            raise _UserGenerationFailed(f"user_provider_http_{status}",
                                        raw[:300].decode("utf-8", "replace"), sent=True)
        try:
            parsed = json.loads(raw.decode("utf-8"))
            choice = parsed["choices"][0]
            if not isinstance(choice.get("message"), dict):
                raise ValueError("choice carries no message object")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _UserGenerationFailed("user_response_unparseable",
                                        f"{type(exc).__name__}: {exc}", sent=True) from None
        self.record.setdefault("generated", []).append(
            {"gate": gate, "raw_sha256": _sha256_bytes(raw)})
        if choice.get("finish_reason") == "length":
            raise _UserGenerationFailed("user_response_length",
                                        "finish_reason=length", sent=True)
        # The transport is the ONLY substituted step: the native `generate` then does its
        # own SDK response conversion, so the AssistantMessage the parent flow consumes is
        # produced by NATIVE code, not by a re-implementation here.
        return _native_assistant_message(parsed)

    # ------------------------------------------------------------------ one exchange

    def next_message(self, wired_messages):
        """One native exchange. Returns (user_text, stop|None, record).

        `wired_messages` is the CURRENT agent wire conversation. The text-only agent turns
        that have not been delivered yet are handed to the native simulator in order, each
        exactly once; tool-call turns are skipped (they are the agent's internal working and
        the native `flip_roles` refuses them).
        """
        # POSITION-based delivery, not text-based: an agent may legitimately ask the same
        # question twice, so the questions already handed to the simulator are counted and
        # everything after that offset is new. (Counting user turns in the state would be
        # off by the task turn, and matching by text would silently drop a repeated one.)
        delivered = self.delivered
        text_turns = [entry["content"] for entry in wired_messages
                      if entry.get("role") == "assistant" and not entry.get("tool_calls")
                      and isinstance(entry.get("content"), str) and entry["content"]]
        pending = text_turns[delivered:]
        for text in pending:
            self.delivered += 1
            question = self.messages["AssistantMessage"](role="assistant", content=text)
            try:
                user_message, self.state = self.simulator.generate_next_message(
                    question, self.state)
            except _UserGenerationRefused as exc:
                self.delivered -= 1
                return None, None, self._record({
                    "question": text, "sent": False,
                    "refused_code": exc.code, "refused_detail": str(exc.detail)[:300]})
            except _UserGenerationFailed as exc:
                self.delivered -= 1
                return None, None, self._record({
                    "question": text, "sent": exc.sent,
                    "failure": {"code": exc.code, "detail": exc.detail, "sent": exc.sent}})
            except Exception as exc:  # noqa: BLE001 - a native refusal is evidence too
                self.delivered -= 1
                return None, None, self._record({
                    "question": text, "sent": False,
                    "failure": {"code": "native_user_simulator_error",
                                "detail": f"{type(exc).__name__}: {exc}"[:300],
                                "sent": False}})
            content = user_message.content
            entry = {"question": text, "sent": True,
                     "state_messages": len(self.state.messages),
                     "system_messages": len(self.state.system_messages),
                     "stop": self.simulator.is_stop(user_message),
                     "content_chars": len(content) if isinstance(content, str) else None}
            self.record["exchanges"].append(entry)
            if not isinstance(content, str) or not content.strip():
                entry["stop_reason"] = "empty_user_response"
                return None, None, entry
            return content, entry["stop"], entry
        return None, None, self._record({
            "question": None, "sent": False,
            "failure": {"code": "no_pending_agent_question",
                        "detail": "no undelivered agent text turn was found", "sent": False}})

    def _record(self, entry):
        self.record["exchanges"].append(entry)
        return entry


class _UserGenerationRefused(RuntimeError):
    """The gate refused the user request BEFORE any generation."""

    def __init__(self, code, detail=None):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else str(code))


class _UserGenerationFailed(RuntimeError):
    """The user request WAS sent but produced no usable answer."""

    def __init__(self, code, detail=None, *, sent=True):
        self.code, self.detail, self.sent = code, detail, sent
        super().__init__(f"{code}: {detail}" if detail else str(code))


def load_user_simulator(vita_source: Path):
    """Only the native user simulator and the native message classes."""
    source = Path(vita_source)
    if not (source / "src/vita").is_dir():
        raise RuntimeError(f"fixed Vita source is missing: {source}")
    ensure_vita_model_config()
    sys.path.insert(0, str(source / "src"))
    from vita.data_model.message import AssistantMessage, SystemMessage, UserMessage
    from vita.user.user_simulator import UserSimulator
    return {"UserSimulator": UserSimulator,
            "messages": {"AssistantMessage": AssistantMessage,
                         "SystemMessage": SystemMessage, "UserMessage": UserMessage}}


def _finish_after_payment(native_module, result, environment, public, out=None):
    """Kept as a thin wrapper: the implementation is the shared flow's."""
    view = dict(public, work_address_key=WORK_ADDRESS_KEY, user_id=USER_ID,
                target_product_id=TARGET_PRODUCT_ID, expected_sugar=EXPECTED_SUGAR,
                status_passed=STATUS_PASSED, status_incomplete=STATUS_INCOMPLETE)
    return _load_flow().finish_after_payment(native_module, result, environment, view)


def _finish_without_tool_call(native_module, result, environment, reason):
    return _load_flow().finish_without_tool_call(native_module, result, environment, reason,
                                                 STATUS_INCOMPLETE)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=("plan", "preflight", "run"), default="plan")
    parser.add_argument("--vita-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.stage == "plan":
            out = ensure_output_dir(args.output_dir)
            plan = build_plan(args.vita_source)
            save(out / "plan.json", plan)
            print(f"AE local t4 probe PLAN; no network/model; model={plan['endpoint']['model']}; "
                  f"endpoint={plan['endpoint']['origin']}; "
                  f"budget=agent{AGENT_MAX_REQUESTS}/user{AUX_MAX_REQUESTS}/total{TOTAL_MAX_POSTS}; "
                  f"output={out}")
            return 0
        if args.stage == "preflight":
            out = ensure_output_dir(args.output_dir)
            readiness = preflight(args.vita_source)
            save(out / "preflight.json", readiness)
            print(f"AE local t4 probe PREFLIGHT {readiness['status']}; "
                  f"prompt={readiness.get('service_tokenize_count')} "
                  f"reserve={readiness['generation_reserve_tokens']} "
                  f"window={readiness['service_window_tokens']}; "
                  f"output={out}")
            return 0 if readiness["status"] == STATUS_PREFLIGHT_OK else 3
        result = execute_probe(vita_source=args.vita_source, output_dir=args.output_dir)
        print(f"AE local t4 probe {result['status']}; task_success={result['task_success']}; "
              f"posts={result.get('total_model_posts')}; scientific_result=null; "
              f"output={args.output_dir}")
        return 0
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - reported, not hidden
        failure = {"schema_version": SCHEMA_VERSION, "status": STATUS_INVALID,
                   "stage": args.stage, "scientific_result": None,
                   "invalid_reasons": [{"type": type(exc).__name__, "message": str(exc)}]}
        try:
            if args.output_dir.is_dir() and not (args.output_dir / "result.json").exists():
                save(args.output_dir / "result.json", failure)
        except Exception:
            pass
        print(f"AE local t4 probe INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
