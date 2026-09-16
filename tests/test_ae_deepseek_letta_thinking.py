"""The declared non-thinking mode through the REAL SDK send path, up to the proxy.

Two live runs fixed two different defects, and this suite pins the second one:

* `transfers/ae-deepseek-re-live-20260916-r1`: the agent's request reached the proxy
  WITHOUT `thinking` -> `declared_transport_field_missing` (the field was never added);
* `transfers/ae-thinking-deploy-20260916-r1` (run `...-r2`): the field was added as a
  TOP-LEVEL Python keyword -> `AsyncCompletions.create() got an unexpected keyword
  argument 'thinking'` before anything was sent, because the pinned OpenAI SDK only turns
  `extra_body` into top-level JSON.

The whole path is exercised offline with the HTTP transport as the ONLY substitution:

    real OpenAIClient.build_request_data
      -> real _ae_apply_transport_fields            (carries the field in `extra_body`)
      -> real OpenAIClient.request_async            (the pinned method body)
      -> real openai.AsyncOpenAI + real httpx2 client with a capture transport
      -> the captured request's REAL JSON bytes -> the real CloudAuditProxy

Nothing is hand-written: the body comes from the reviewed construction code, the request
bytes come from the SDK's own serializer, and the proxy is the one that will run on site.
"""
from __future__ import annotations

import ast
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STACKED = Path(os.environ.get(
    "AE_LETTA_NO_COMPACTION_SOURCE",
    ROOT / ".ae-verify-src/letta-no-compaction-patch/letta-v1"))
LIVE_R1 = (ROOT / "transfers/ae-deepseek-re-live-20260916-r1"
           / "ae-deepseek-re-live-20260916-r1.private.jsonl")
AGENT = STACKED / "letta/agents/letta_agent_v3.py"
OPENAI_CLIENT = STACKED / "letta/llm_api/openai_client.py"
FAKE_KEY = "sk-" + "z" * 40
MODE = {"thinking": {"type": "disabled"}}
DECLARED = {"AE_TRANSPORT_WIRE_MODEL": "deepseek-flash",
            "AE_TRANSPORT_REQUIRED_FIELDS": json.dumps(MODE, separators=(",", ":"))}
TRANSPORT_ENV = tuple(DECLARED)
OTHER_BODY = {"foo": "bar"}

TOOL = {"name": "memory_update", "description": "更新长期记忆",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}},
                       "required": ["content"], "additionalProperties": False}}
MESSAGES = [{"role": "system", "content": "system prompt"},
            {"role": "user", "content": "{\"source\": \"current_task\"}"}]

CHAT_RESPONSE = {
    "id": "chatcmpl-fixture", "object": "chat.completion", "created": 1,
    "model": "deepseek-flash",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _plain(value):
    """Nested pydantic models become wire dicts, exactly as `model_dump` does."""
    if isinstance(value, _Record):
        return {key: _plain(item) for key, item in value._fields.items()}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class _Record:
    """The pydantic request object, reduced to what the reviewed method really uses."""

    def __init__(self, **kwargs):
        object.__setattr__(self, "_fields", {})
        for key, value in kwargs.items():
            self._fields[key] = value

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        fields = self.__dict__.get("_fields")
        if fields is None or name not in fields:
            raise AttributeError(name)
        return fields[name]

    def __setattr__(self, name, value):
        self._fields[name] = value

    def __deepcopy__(self, memo):
        clone = _Record()
        clone.__dict__["_fields"] = copy.deepcopy(self.__dict__["_fields"], memo)
        return clone

    def model_dump(self, exclude_unset=False):
        return _plain(copy.deepcopy(self._fields))

    def model_copy(self, deep=False):
        clone = _Record()
        object.__setattr__(clone, "_fields",
                           copy.deepcopy(self._fields) if deep else dict(self._fields))
        return clone


def _load_build_request_data():
    """The REAL `OpenAIClient.build_request_data`, with only module names stubbed."""
    source = OPENAI_CLIENT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == "build_request_data"
                and "Constructs a request object" in ast.get_source_segment(source, item))
    module = SimpleNamespace(
        use_responses_api=lambda _cfg: False,
        AgentType=SimpleNamespace(letta_v1_agent="letta_v1_agent"),
        INNER_THOUGHTS_KWARG="inner_thoughts",
        INNER_THOUGHTS_KWARG_DESCRIPTION="desc",
        INNER_THOUGHTS_KWARG_DESCRIPTION_GO_FIRST="desc",
        add_inner_thoughts_to_functions=lambda **kwargs: kwargs["functions"],
        REQUEST_HEARTBEAT_PARAM="request_heartbeat",
        ChatCompletionRequest=_Record,
        OpenAITool=lambda **kwargs: _Record(
            **{key: value for key, value in kwargs.items() if key != "function"},
            function=_Record(**kwargs["function"])),
        ToolFunctionChoice=lambda **kwargs: _Record(**kwargs),
        ToolFunctionChoiceFunctionCall=lambda **kwargs: _Record(**kwargs),
        JsonSchemaResponseFormat=type("JsonSchemaResponseFormat", (), {}),
        LETTA_MODEL_ENDPOINT="https://inference.letta.com/v1/",
        PydanticMessage=SimpleNamespace(
            to_openai_dicts_from_list=lambda messages, **_: copy.deepcopy(messages)),
        cast_message_to_subtype=lambda message: message,
        fill_image_content_in_messages=lambda openai_messages, messages: openai_messages,
        accepts_developer_role=lambda _model: False,
        supports_content_none=lambda _cfg: True,
        supports_temperature_param=lambda _model: True,
        supports_verbosity_control=lambda _model: False,
        is_openai_reasoning_model=lambda _model: False,
        supports_parallel_tool_calling=lambda _model: True,
        supports_structured_output=lambda _cfg: False,
        logger=SimpleNamespace(warning=lambda *a, **k: None),
    )
    exec(compile(textwrap.dedent(ast.get_source_segment(source, node)),
                 "<openai-build-request-data>", "exec"), vars(module))
    client = SimpleNamespace(
        actor=SimpleNamespace(id="user-00000000-0000-4000-8000-000000000000"),
        _apply_system_override=lambda messages, system: messages,
        _is_openrouter_request=lambda _cfg: False,
        requires_auto_tool_choice=lambda _cfg: True,
        _apply_prompt_cache_settings=lambda **_: None,
    )
    return module.build_request_data.__get__(client)


def _load_agent_methods():
    """The REAL `_ae_*` methods the declared request shape is built and measured by."""
    source = AGENT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    module = {"json": json, "os": os,
              "NoCompactionPolicyError": type("NoCompactionPolicyError", (RuntimeError,), {})}
    names = ("_ae_transport_fields", "_ae_apply_transport_fields", "_ae_wire_body",
             "_ae_request_bytes")
    for name in names:
        node = next(item for item in ast.walk(tree)
                    if isinstance(item, ast.FunctionDef) and item.name == name)
        exec(compile(textwrap.dedent(ast.get_source_segment(source, node)),
                     f"<agent-{name}>", "exec"), module)
    instance = SimpleNamespace(logger=SimpleNamespace(warning=lambda *a, **k: None))
    for name in names:
        setattr(instance, name, module[name].__get__(instance))
    return instance


def _load_request_async(capture_client):
    """The REAL `OpenAIClient.request_async`, with ONLY the HTTP client substituted."""
    source = OPENAI_CLIENT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in ast.walk(tree)
                if isinstance(item, ast.AsyncFunctionDef) and item.name == "request_async")
    import openai
    from openai import ChatCompletion  # noqa: F401 - the method's annotation only
    module = {
        "sanitize_unicode_surrogates": lambda data: data,
        # The REAL pinned SDK class, not a stand-in.
        "AsyncOpenAI": openai.AsyncOpenAI,
        "ChatCompletion": ChatCompletion,
        "json": json,
        "logger": SimpleNamespace(error=lambda *a, **k: None),
        "LLMServerError": type("LLMServerError", (RuntimeError,), {}),
        "ErrorCode": SimpleNamespace(INTERNAL_SERVER_ERROR="internal"),
    }
    exec(compile(textwrap.dedent(ast.get_source_segment(source, node)),
                 "<openai-request-async>", "exec"), module)

    async def prepare(_llm_config):
        # The keys the real builder returns, plus the offline capture transport.
        return {"api_key": "EMPTY", "base_url": "http://127.0.0.1:8000/v1",
                "http_client": capture_client}

    client = SimpleNamespace(_prepare_client_kwargs_async=prepare)
    return module["request_async"].__get__(client)


class _CaptureTransport:
    """An httpx2 async transport that records the FINAL serialized request."""

    @staticmethod
    def build():
        import httpx2 as httpx

        captured = {}

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                captured["method"] = request.method
                captured["url"] = str(request.url)
                captured["content"] = request.content
                return httpx.Response(
                    200, headers={"content-type": "application/json"},
                    content=json.dumps(CHAT_RESPONSE).encode("utf-8"))

        client = httpx.AsyncClient(transport=Transport())
        return captured, client


def _llm_config(model):
    return SimpleNamespace(
        model=model, handle="openai/" + model, max_tokens=2048, temperature=0.0,
        put_inner_thoughts_in_kwargs=False, verbosity=None, reasoning_effort=None,
        frequency_penalty=None, return_logprobs=False, top_logprobs=None,
        response_format=None, model_endpoint="http://127.0.0.1:8000/v1",
        parallel_tool_calls=False, enable_reasoner=False, provider_name="deepseek",
        model_dump_json=lambda **_: "{}")


def _recorded_opener():
    class _Response(io.BytesIO):
        def __init__(self):
            super().__init__(json.dumps({
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode("utf-8"))
            self.code, self.headers = 200, {}

    class Opener:
        def __init__(self):
            self.sent = []

        def open(self, request, timeout):
            self.sent.append({"url": request.full_url,
                              "body": json.loads(request.data.decode("utf-8"))})
            return _Response()

    return Opener()


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not AGENT.is_file() or not OPENAI_CLIENT.is_file():
            raise unittest.SkipTest("the stacked checkout is absent")
        cls.build_request_data = _load_build_request_data()
        cls.agent = _load_agent_methods()

    def setUp(self):
        self._saved = {name: os.environ.pop(name, None) for name in TRANSPORT_ENV}
        self.addCleanup(self._restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _declare(self, **overrides):
        for name, value in {**DECLARED, **overrides}.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _body(self, model="deepseek-flash"):
        return self.build_request_data(
            agent_type="letta_v1_agent", messages=copy.deepcopy(MESSAGES),
            llm_config=_llm_config(model), tools=[copy.deepcopy(TOOL)],
            force_tool_call=None, requires_subsequent_tool_call=False,
            tool_return_truncation_chars=26214, system=None)

    def _send_through_the_sdk(self, request_data):
        """Run the REAL `request_async` with the real SDK; return the captured wire bytes."""
        import asyncio
        captured, client = _CaptureTransport.build()
        request_async = _load_request_async(client)

        async def go():
            try:
                return await request_async(request_data, _llm_config(request_data["model"]))
            finally:
                await client.aclose()

        response = asyncio.run(go())
        return captured, response

    def _proxy(self, *, budget=None, profile=None):
        import ae_cloud_proxy as px
        cloud = px.CloudConfig(
            model="deepseek-flash", max_output_tokens=4096, max_request_bytes=2097152,
            max_response_bytes=16777216, max_requests=8, io_timeout_seconds=180.0,
            upstream_origin=px.DEEPSEEK_ORIGIN,
            profile=profile or px.DEEPSEEK_PROFILE, upstream_model="deepseek-flash",
            declared_request_byte_budget=budget or 2097152, pace_seconds=0)
        cloud.validate()
        journal = Path(self.tmp.name) / f"j-{len(list(Path(self.tmp.name).iterdir()))}.jsonl"
        proxy = px.CloudAuditProxy(cloud, journal, api_key=FAKE_KEY,
                                   clock=lambda: 1.0, sleep=lambda seconds: None)
        opener = _recorded_opener()
        proxy.opener = opener
        declared = json.loads(
            (ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-candidate.json")
            .read_text(encoding="utf-8"))

        def count_bytes(normalized, role):
            return budget or 2097152

        proxy.arm_capacity_from_declaration(
            declared, count_request=count_bytes, count_source=px.BYTE_GATE_COUNT_BASIS,
            count_basis=px.BYTE_GATE_COUNT_BASIS)
        return proxy, journal, opener

    def _dispatch(self, proxy, raw):
        return proxy.dispatch("POST", "/v1/chat/completions", raw,
                              "re-multiturn/stage", "agent_or_unknown")


class TheSdkSendPathTests(_Case):
    """The acceptance: the REAL SDK call sends the mode at the HTTP top level."""

    def test_the_declared_mode_survives_the_sdk_and_reaches_the_proxy(self):
        self._declare()
        body = self._body()
        applied = self.agent._ae_apply_transport_fields(body)
        self.assertTrue(applied["applied"], applied)
        self.assertEqual(applied["carrier"], "extra_body")
        # The Python call is valid: no top-level keyword the SDK would reject.
        self.assertNotIn("thinking", body)
        self.assertEqual(body["extra_body"], MODE)
        measured = self.agent._ae_request_bytes(body)
        captured, response = self._send_through_the_sdk(body)
        raw = captured["content"]
        wire = json.loads(raw)
        self.assertEqual(response["choices"][0]["message"]["content"], "ok")
        self.assertEqual(wire["thinking"], MODE["thinking"],
                         "the FINAL HTTP JSON must carry the mode at the top level")
        self.assertNotIn("extra_body", wire,
                         "extra_body is a Python carrier, never a wire key")
        self.assertEqual(wire["model"], "deepseek-flash")
        # The wire body is the live capture's own key set plus the declared mode; the
        # `max_completion_tokens` -> `max_tokens` rename happens later, in the proxy.
        self.assertEqual(sorted(wire), sorted(
            ["max_completion_tokens", "messages", "model", "parallel_tool_calls",
             "temperature", "thinking", "tool_choice", "tools", "user"]))
        # The gate measured the request that was really sent, byte for byte.
        self.assertEqual(measured, len(raw),
                         "the capacity gate must measure the merged wire body, not the "
                         "Python extra_body wrapper")
        proxy, journal, opener = self._proxy()
        status, _reply = self._dispatch(proxy, raw)
        proxy.close()
        self.assertEqual(status, 200)
        self.assertEqual(opener.sent[0]["body"]["thinking"], MODE["thinking"])
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        normalized_row = next(row for row in rows if row.get("kind") == "normalized_request")
        normalized = base64.b64decode(normalized_row["body_base64"])
        check = next(row for row in rows if row.get("kind") == "capacity_check")
        self.assertEqual(check["input_bytes"], len(normalized))
        self.assertEqual(check["input_sha256"], hashlib.sha256(normalized).hexdigest())
        self.assertEqual(json.loads(normalized)["thinking"], MODE["thinking"])
        kept = {change["to"] for change in normalized_row["changes"]
                if change.get("operation") == "keep"}
        self.assertEqual(kept, {"thinking", "user", "parallel_tool_calls"})

    def test_the_previous_top_level_placement_is_what_the_sdk_rejects(self):
        """The live r2 failure, reproduced offline through the same SDK call.

        This is the control for the acceptance above: the previous round's placement (a
        bare top-level `thinking`) fails with exactly the error the deployed service hit,
        so the harness demonstrably walks the path that broke on site.
        """
        self._declare()
        request_data = self._body()
        request_data["thinking"] = dict(MODE["thinking"])
        with self.assertRaises(TypeError) as caught:
            self._send_through_the_sdk(request_data)
        self.assertIn("thinking", str(caught.exception))

    def test_an_existing_extra_body_is_merged_not_replaced(self):
        self._declare()
        body = self._body()
        body["extra_body"] = dict(OTHER_BODY)
        self.agent._ae_apply_transport_fields(body)
        self.assertEqual(body["extra_body"], {**OTHER_BODY, **MODE},
                         "another carrier entry must survive the merge")
        captured, _response = self._send_through_the_sdk(body)
        wire = json.loads(captured["content"])
        self.assertEqual(wire["thinking"], MODE["thinking"])
        self.assertEqual(wire["foo"], "bar")
        self.assertNotIn("extra_body", wire)

    def test_a_top_level_value_the_sdk_rejects_is_moved_to_its_carrier(self):
        self._declare()
        body = self._body()
        body["thinking"] = dict(MODE["thinking"])   # same value, unusable placement
        self.agent._ae_apply_transport_fields(body)
        self.assertNotIn("thinking", body)
        captured, _response = self._send_through_the_sdk(body)
        self.assertEqual(json.loads(captured["content"])["thinking"], MODE["thinking"])

    def test_the_constructed_body_still_reproduces_the_live_capture(self):
        if not LIVE_R1.is_file():
            self.skipTest("the first live journal is absent")
        rows = [json.loads(line) for line in LIVE_R1.read_text().splitlines() if line.strip()]
        live = next(r for r in rows if r.get("kind") == "client_request"
                    and (r.get("body_bytes") or 0) > 0)
        live_keys = sorted(json.loads(base64.b64decode(live["body_base64"]).decode()))
        self._declare()
        body = self._body()
        self.agent._ae_apply_transport_fields(body)
        captured, _response = self._send_through_the_sdk(body)
        self.assertEqual(sorted(json.loads(captured["content"])),
                         sorted(live_keys + ["thinking"]))


class TheProxyStillRefusesTests(_Case):
    """The required-field check is NOT relaxed: missing and rewritten values fail."""

    def test_a_request_without_the_mode_is_refused(self):
        self._declare()
        body = self._body()          # the exact first live failure: no mode at all
        captured, _response = self._send_through_the_sdk(body)
        raw = captured["content"]
        self.assertNotIn("thinking", json.loads(raw))
        proxy, journal, opener = self._proxy()
        with self.assertRaises(Exception) as caught:
            self._dispatch(proxy, raw)
        proxy.close()
        self.assertIn("declared_transport_field_missing", str(caught.exception))
        self.assertEqual(opener.sent, [], "nothing may be sent without the declared mode")
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        self.assertEqual([row.get("code") for row in rows if row.get("kind") == "blocked"],
                         ["declared_transport_field_missing"])

    def test_a_rewritten_mode_value_is_refused(self):
        self._declare()
        body = self._body()
        self.agent._ae_apply_transport_fields(body)
        body["extra_body"] = {"thinking": {"type": "enabled"}}
        captured, _response = self._send_through_the_sdk(body)
        raw = captured["content"]
        self.assertEqual(json.loads(raw)["thinking"], {"type": "enabled"})
        proxy, _journal, opener = self._proxy()
        with self.assertRaises(Exception) as caught:
            self._dispatch(proxy, raw)
        self.assertIn("declared_transport_field_value_changed", str(caught.exception))
        self.assertEqual(opener.sent, [])


class TheDeclarationItselfTests(_Case):
    """A partial or conflicting declaration is a refusal, never a guess."""

    def test_a_half_declaration_is_refused(self):
        self._declare(AE_TRANSPORT_REQUIRED_FIELDS=None)
        with self.assertRaises(RuntimeError):
            self.agent._ae_transport_fields()
        self._declare(AE_TRANSPORT_WIRE_MODEL=None)
        with self.assertRaises(RuntimeError):
            self.agent._ae_transport_fields()

    def test_an_unusable_declaration_is_refused(self):
        self._declare(AE_TRANSPORT_REQUIRED_FIELDS="not-json")
        with self.assertRaises(RuntimeError):
            self.agent._ae_transport_fields()
        self._declare(AE_TRANSPORT_REQUIRED_FIELDS="{}")
        with self.assertRaises(RuntimeError):
            self.agent._ae_transport_fields()

    def test_conflicting_values_are_never_overridden(self):
        self._declare()
        top_level = self._body()
        top_level["thinking"] = {"type": "enabled"}
        with self.assertRaises(RuntimeError):
            self.agent._ae_apply_transport_fields(top_level)
        self.assertEqual(top_level["thinking"], {"type": "enabled"})
        carried = self._body()
        carried["extra_body"] = {"thinking": {"type": "enabled"}}
        with self.assertRaises(RuntimeError):
            self.agent._ae_apply_transport_fields(carried)
        self.assertEqual(carried["extra_body"], {"thinking": {"type": "enabled"}})
        broken = self._body()
        broken["extra_body"] = "not-a-mapping"
        with self.assertRaises(RuntimeError):
            self.agent._ae_apply_transport_fields(broken)

    def test_an_undeclared_service_adds_nothing(self):
        for name in TRANSPORT_ENV:
            os.environ.pop(name, None)
        body = self._body()
        applied = self.agent._ae_apply_transport_fields(body)
        self.assertEqual(applied, {"declared": False, "applied": False, "fields": []})
        self.assertNotIn("thinking", body)
        self.assertNotIn("extra_body", body)


class TheHistoricalQwenPathIsUnchangedTests(_Case):
    """With no declaration the old profile's request and the old proxy both work."""

    def test_the_old_profile_still_accepts_the_undeclared_body(self):
        import ae_cloud_proxy as px
        for name in TRANSPORT_ENV:
            os.environ.pop(name, None)
        body = self._body(model=px.MODEL)
        self.agent._ae_apply_transport_fields(body)
        captured, _response = self._send_through_the_sdk(body)
        raw = captured["content"]
        self.assertNotIn("thinking", json.loads(raw))
        compat = px.CloudConfig(px.MODEL, 4096, 2097152, 16777216, 8, 180.0,
                                profile=px.COMPAT_PROFILE)
        compat.validate()
        journal = Path(self.tmp.name) / "qwen.jsonl"
        proxy = px.CloudAuditProxy(compat, journal, api_key=FAKE_KEY,
                                   clock=lambda: 1.0, sleep=lambda seconds: None)
        opener = _recorded_opener()
        proxy.opener = opener
        status, _reply = self._dispatch(proxy, raw)
        proxy.close()
        self.assertEqual(status, 200)
        self.assertEqual(opener.sent[0]["body"]["model"], px.MODEL)
        self.assertNotIn("thinking", opener.sent[0]["body"])

    def test_the_launcher_passes_the_declaration_only_when_it_is_declared(self):
        launcher = importlib.util.spec_from_file_location(
            "letta_local_for_transport", ROOT / "scripts/deployment/letta_local.py")
        module = importlib.util.module_from_spec(launcher)
        launcher.loader.exec_module(module)
        import ae_cloud_proxy as px
        runtime = module.PatchStackRuntime(
            manifest=ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json",
            support=ROOT, receipt=Path(self.tmp.name) / "receipt.json",
            profile=module.STACK_PROFILE_VERSION,
            byte_gate_count_basis="no_token_count_byte_gate_only",
            byte_gate_max_request_bytes=2097152, byte_gate_wire_model="deepseek-flash",
            transport_profile=px.DEEPSEEK_PROFILE)
        self.assertEqual(runtime.transport_shape()["fields"], MODE)
        child = runtime.env(Path(self.tmp.name) / "receipts")
        self.assertEqual(child["AE_TRANSPORT_WIRE_MODEL"], "deepseek-flash")
        self.assertEqual(json.loads(child["AE_TRANSPORT_REQUIRED_FIELDS"]), MODE)
        undeclared = module.PatchStackRuntime(
            manifest=ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json",
            support=ROOT, receipt=Path(self.tmp.name) / "receipt-2.json",
            profile=module.STACK_PROFILE_VERSION,
            byte_gate_count_basis="no_token_count_byte_gate_only",
            byte_gate_max_request_bytes=2097152, byte_gate_wire_model="deepseek-flash")
        self.assertEqual(undeclared.transport_shape()["policy"], "undeclared")
        bare = undeclared.env(Path(self.tmp.name) / "receipts-2")
        self.assertNotIn("AE_TRANSPORT_WIRE_MODEL", bare)
        self.assertNotIn("AE_TRANSPORT_REQUIRED_FIELDS", bare)
        with self.assertRaises(RuntimeError):
            module.PatchStackRuntime(
                manifest=ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json",
                support=ROOT, receipt=Path(self.tmp.name) / "receipt-3.json",
                profile=module.STACK_PROFILE_VERSION,
                transport_profile="not-a-reviewed-profile").transport_shape()


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
