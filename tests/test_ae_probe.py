"""Synthetic transport tests only; no Letta, model, GPU, or network calls."""
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from ae_adapter import BLOCK_LABEL
from ae_probe import build_plan, execute_probe, validate_config


ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    "schema_version": "ae-connection-probe-0.1", "purpose": "connectivity_only",
    "model_origin": "http://127.0.0.1:8000", "letta_origin": "http://127.0.0.1:8283",
    "model_handle": "vllm/Qwen3-8B", "expected_model": "Qwen3-8B",
    "context_window": 8192, "max_output_tokens": 1024, "max_rounds": 3,
    "max_steps": 3, "temperature": 0.0, "timeout_seconds": 60,
    "max_response_bytes": 1048576, "max_request_bytes": 262144,
}


class ModelFixture:
    def __init__(self, model="Qwen3-8B", maximum=8192, duplicate=False):
        self.requests = []
        self.data = [{"id": model, "max_model_len": maximum}]
        if duplicate:
            self.data *= 2

    def request(self, method, path, body=None):
        self.requests.append((method, path, deepcopy(body)))
        assert (method, path, body) == ("GET", "/v1/models", None)
        return {"data": deepcopy(self.data)}


class LettaFixture:
    def __init__(self, *, mode="ok", llm_changes=None, embedding=None, config=None):
        self.mode, self.llm_changes, self.embedding = mode, llm_changes or {}, embedding
        self.config = config or CONFIG
        self.requests = []
        self.posts = 0
        self.state = None
        self.returned_nonce = None

    def request(self, method, path, body=None):
        self.requests.append((method, path, deepcopy(body)))
        if (method, path) == ("GET", "/v1/health/"):
            return {"status": "ok", "version": "fixture-not-a-deployed-server"}
        if (method, path) == ("POST", "/v1/agents/"):
            settings = body["model_settings"]
            llm = {"handle": body["model"], "model": self.config["expected_model"],
                   "model_endpoint_type": "openai", "model_endpoint": self.config["model_origin"] + "/v1",
                   "context_window": body["context_window_limit"], "max_tokens": settings["max_output_tokens"],
                   "temperature": settings["temperature"], "parallel_tool_calls": False, "strict": False}
            llm.update(self.llm_changes)
            self.state = {
                "id": "agent-synthetic-probe", "agent_type": "letta_v1_agent",
                "blocks": [{"id": "block-synthetic-probe", "label": BLOCK_LABEL, "value": "{}"}],
                "tools": [], "sources": [], "tags": [], "message_ids": ["system-fixture"],
                "managed_group": None, "pending_approval": None,
                "message_buffer_autoclear": False, "enable_sleeptime": False,
                "embedding_config": self.embedding, "embedding": None, "llm_config": llm,
            }
            return deepcopy(self.state)
        if method == "GET" and path.startswith("/v1/agents/agent-synthetic-probe?"):
            if self.mode == "model_drift" and self.posts:
                self.state["llm_config"]["model"] = "different-model"
            return deepcopy(self.state)
        assert (method, path) == ("POST", "/v1/agents/agent-synthetic-probe/messages")
        self.posts += 1
        if self.mode == "timeout":
            raise TimeoutError("synthetic timeout; server outcome unknown")
        if self.mode == "interrupt":
            raise KeyboardInterrupt()
        self.state["message_ids"].append(f"message-{self.posts}")
        if self.posts == 1:
            assert body["messages"][0]["role"] == "user"
            if self.mode == "no_tool":
                return self.done("AE_PROBE_guessed_without_a_tool")
            if self.mode == "max_steps":
                return {"messages": [], "stop_reason": {"stop_reason": "max_steps"}}
            if self.mode == "summary":
                return {"messages": [{"message_type": "summary_message", "summary": "fixture"}],
                        "stop_reason": {"stop_reason": "end_turn"}}
            name = "memory_update" if self.mode == "memory" else "read_probe"
            arguments = {"unexpected": 1} if self.mode == "arguments" else {}
            calls = [{"tool_call_id": "real-fixture-call-1", "name": name,
                      "arguments": json.dumps(arguments)}]
            if self.mode == "two_calls":
                calls.append({"tool_call_id": "real-fixture-call-2", "name": "read_probe", "arguments": "{}"})
            self.state["pending_approval"] = {"tool_calls": deepcopy(calls)}
            return {"messages": [{"message_type": "approval_request_message", "tool_calls": calls}],
                    "stop_reason": {"stop_reason": "requires_approval"}}
        message = body["messages"][0]
        assert message["type"] == "tool_return"
        returned = message["tool_returns"]
        assert len(returned) == 1 and returned[0]["tool_call_id"] == "real-fixture-call-1"
        assert returned[0]["status"] == "success"
        self.returned_nonce = json.loads(returned[0]["tool_return"])["nonce"]
        self.state["pending_approval"] = None
        if self.mode == "earlier_nonce_only":
            response = self.done("wrong final answer")
            response["messages"].insert(0, {"message_type": "assistant_message", "content": self.returned_nonce})
            return response
        if self.mode == "none_content":
            return self.done(None)
        return self.done("wrong value" if self.mode == "wrong_nonce" else self.returned_nonce)

    @staticmethod
    def done(text):
        return {"messages": [{"message_type": "assistant_message", "content": text}],
                "stop_reason": {"stop_reason": "end_turn"}}


class ProbeConfigTests(unittest.TestCase):
    def test_plan_is_pure_and_has_no_nonce_or_runtime_claim(self):
        plan = build_plan(CONFIG)
        self.assertEqual(plan["status"], "PLAN")
        self.assertIs(plan["network_called"], False)
        self.assertIsNone(plan["connectivity_passed"])
        self.assertIsNone(plan["task_success"])
        self.assertNotIn("AE_PROBE_", json.dumps(plan))
        creation = next(r["body"] for r in plan["planned_requests"] if r["path"] == "/v1/agents/")
        self.assertEqual(creation["initial_message_sequence"], [])
        self.assertEqual(creation["memory_blocks"][0]["value"], "{}")
        self.assertFalse(creation["include_base_tools"])
        self.assertEqual(creation["model_settings"]["max_output_tokens"], 1024)
        self.assertNotIn("max_tokens", creation)

    def test_origins_cannot_escape_loopback(self):
        for value in ("https://api.example.com", "https://127.0.0.1:8000", "http://192.168.1.1:8000", "http://127.0.0.1:8000/v1",
                      "http://user@127.0.0.1:8000", "http://127.0.0.1:8000?x=1", "http://127.0.0.1:8000/"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(dict(CONFIG, model_origin=value))

    def test_explicit_all_fields_and_small_caps(self):
        for key, value in (("purpose", "science"), ("max_rounds", 4), ("max_steps", 4),
                           ("max_output_tokens", 2048), ("context_window", 16384),
                           ("max_steps", True), ("temperature", float("nan")), ("timeout_seconds", 61)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_config(dict(CONFIG, **{key: value}))
        with self.assertRaises(ValueError):
            validate_config(dict(CONFIG, api_key="never-read-credentials"))
        with self.assertRaises(ValueError):
            validate_config({k: v for k, v in CONFIG.items() if k != "max_steps"})


class ProbeExecutionTests(unittest.TestCase):
    def run_fixture(self, **kwargs):
        model, letta = ModelFixture(), LettaFixture(**kwargs)
        result = execute_probe(CONFIG, model_transport=model, letta_transport=letta)
        return result, model, letta

    def test_success_requires_matching_pending_call_continuation_and_answer(self):
        result, model, letta = self.run_fixture()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["connectivity_passed"])
        self.assertIsNone(result["task_success"])
        self.assertIsNone(result["scientific_result"])
        self.assertEqual(result["tool_invocations"], 1)
        self.assertEqual(result["tool_call_id"], "real-fixture-call-1")
        self.assertEqual(result["nonce"], letta.returned_nonce)
        self.assertEqual(letta.posts, 2)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(result["agent_id"], "agent-synthetic-probe")
        self.assertFalse(result["agent_cleanup_performed"])
        pretool = [b for m, p, b in letta.requests if m == "POST"][:2]
        self.assertNotIn(result["nonce"], json.dumps(pretool))

    def test_provider_identity_or_window_failure_creates_no_agent(self):
        for fixture in (ModelFixture(model="wrong"), ModelFixture(maximum=4096),
                        ModelFixture(maximum=None), ModelFixture(duplicate=True)):
            letta = LettaFixture()
            result = execute_probe(CONFIG, model_transport=fixture, letta_transport=letta)
            self.assertFalse(result["connectivity_passed"])
            self.assertIsNone(result["agent_id"])
            self.assertEqual(letta.requests, [])

    def test_wrong_actual_config_prevents_any_messages(self):
        for change in ({"handle": "wrong/model"}, {"model": "wrong"}, {"context_window": 4096},
                       {"max_tokens": 2048}, {"temperature": 0.7}, {"parallel_tool_calls": True},
                       {"strict": True}, {"model_endpoint": "https://api.example.com/v1"},
                       {"model_endpoint_type": "anthropic"}):
            with self.subTest(change=change):
                result, _, letta = self.run_fixture(llm_changes=change)
                self.assertFalse(result["connectivity_passed"])
                self.assertFalse(result["model_messages_attempted"])
                self.assertEqual(letta.posts, 0)
                self.assertEqual(result["agent_id"], "agent-synthetic-probe")

    def test_embedding_must_be_off_before_messages(self):
        result, _, letta = self.run_fixture(embedding={"embedding_model": "unexpected"})
        self.assertFalse(result["connectivity_passed"])
        self.assertEqual(letta.posts, 0)

    def test_answer_without_tool_is_never_pass(self):
        result, _, _ = self.run_fixture(mode="no_tool")
        self.assertFalse(result["connectivity_passed"])
        self.assertIsNone(result["nonce"])
        self.assertEqual(result["tool_invocations"], 0)

    def test_memory_update_is_stopped_not_applied(self):
        result, _, letta = self.run_fixture(mode="memory")
        self.assertFalse(result["connectivity_passed"])
        self.assertEqual(letta.posts, 1)
        self.assertEqual(result["tool_invocations"], 0)
        self.assertEqual(letta.state["blocks"][0]["value"], "{}")
        self.assertIn("refuses memory_update", json.dumps(result["invalid_reasons"]))

    def test_extra_call_wrong_args_or_wrong_answer_cannot_pass(self):
        for mode in ("two_calls", "arguments", "wrong_nonce", "earlier_nonce_only", "none_content"):
            with self.subTest(mode=mode):
                result, _, _ = self.run_fixture(mode=mode)
                self.assertFalse(result["connectivity_passed"])
                self.assertTrue(result["invalid_reasons"])

    def test_server_noncompletion_compaction_or_model_drift_cannot_pass(self):
        for mode in ("max_steps", "summary", "model_drift"):
            with self.subTest(mode=mode):
                result, _, _ = self.run_fixture(mode=mode)
                self.assertFalse(result["connectivity_passed"])
                self.assertEqual(result["tool_invocations"], 0)

    def test_uncertain_post_is_not_retried_and_keeps_agent_id_and_trace(self):
        result, _, letta = self.run_fixture(mode="timeout")
        self.assertFalse(result["connectivity_passed"])
        self.assertEqual(letta.posts, 1)
        self.assertEqual(result["agent_id"], "agent-synthetic-probe")
        self.assertTrue(result["model_messages_attempted"])
        self.assertTrue(any(x["kind"] == "request_error" for x in result["transport_trace"]))

    def test_interrupt_preserves_agent_and_does_not_claim_server_cancellation(self):
        result, _, letta = self.run_fixture(mode="interrupt")
        self.assertFalse(result["connectivity_passed"])
        self.assertTrue(result["interrupted"])
        self.assertFalse(result["server_side_work_cancelled"])
        self.assertEqual(result["agent_id"], "agent-synthetic-probe")
        self.assertEqual(letta.posts, 1)


class ProbeCLITests(unittest.TestCase):
    @staticmethod
    def cli():
        spec = spec_from_file_location("probe_cli_fixture", ROOT / "scripts/ae_01_connection_probe.py")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_default_plan_does_not_construct_http_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, output = Path(tmp) / "config.json", Path(tmp) / "new-plan"
            config.write_text(json.dumps(CONFIG), encoding="utf-8")
            with patch("ae_http.JSONHTTPTransport", side_effect=AssertionError("plan must be offline")):
                with redirect_stdout(io.StringIO()):
                    code = self.cli().main(["--config", str(config), "--output-dir", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual([p.name for p in output.iterdir()], ["plan.json"])

    def test_plan_without_output_is_stdout_only_and_has_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps(CONFIG), encoding="utf-8")
            text = io.StringIO()
            with patch("ae_http.JSONHTTPTransport", side_effect=AssertionError("plan must be offline")):
                with redirect_stdout(text):
                    code = self.cli().main(["--config", str(config)])
            self.assertEqual(code, 0)
            plan = json.loads(text.getvalue())
            self.assertEqual(len(plan["provenance"]["code_sha256"]), 4)
            self.assertFalse(plan["provenance"]["live_letta_commit_verified"])
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["config.json"])

    def test_execute_requires_new_output_before_contacting_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps(CONFIG), encoding="utf-8")
            with patch("ae_http.JSONHTTPTransport", side_effect=AssertionError("must not connect")):
                with redirect_stderr(io.StringIO()):
                    code = self.cli().main(["--config", str(config), "--execute"])
            self.assertEqual(code, 2)

    def test_existing_output_is_not_written_even_if_it_contains_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, output = Path(tmp) / "config.json", Path(tmp) / "old"
            config.write_text(json.dumps(CONFIG), encoding="utf-8")
            output.mkdir()
            (output / "plan.json").write_text("original-plan", encoding="utf-8")
            (output / "result.json").write_text("original-result", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                code = self.cli().main(["--config", str(config), "--output-dir", str(output), "--execute"])
            self.assertEqual(code, 2)
            self.assertEqual((output / "plan.json").read_text(), "original-plan")
            self.assertEqual((output / "result.json").read_text(), "original-result")


class LocalFixtureServer:
    """HTTP serialization fixture; never runs an LLM or real Letta server."""
    def __init__(self, delegate):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond("GET")

            def do_POST(self):
                self.respond("POST")

            def respond(self, method):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length)) if length else None
                    response = delegate.request(method, self.path, body)
                    status = 200
                except Exception as exc:
                    response, status = {"fixture_error": str(exc)}, 500
                data = json.dumps(response).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class ProbeHTTPFixtureTests(unittest.TestCase):
    def test_two_real_loopback_http_transports_complete_scripted_probe_and_journals(self):
        from ae_http import JSONHTTPTransport
        model_fixture, letta_fixture = ModelFixture(), LettaFixture()
        with tempfile.TemporaryDirectory() as tmp, LocalFixtureServer(model_fixture) as ms, LocalFixtureServer(letta_fixture) as ls:
            config = dict(CONFIG, model_origin=ms.origin, letta_origin=ls.origin)
            letta_fixture.config = config
            options = {"timeout_seconds": 2, "max_response_bytes": 1048576, "max_request_bytes": 262144}
            with JSONHTTPTransport(ms.origin, Path(tmp) / "model.jsonl", max_requests=1, **options) as model:
                with JSONHTTPTransport(ls.origin, Path(tmp) / "letta.jsonl", max_requests=9, **options) as letta:
                    result = execute_probe(config, model_transport=model, letta_transport=letta)
            self.assertTrue(result["connectivity_passed"], result["invalid_reasons"])
            self.assertEqual(letta_fixture.posts, 2)
            self.assertIsNone(result["task_success"])
            model_log = [json.loads(x) for x in (Path(tmp) / "model.jsonl").read_text().splitlines()]
            letta_log = [json.loads(x) for x in (Path(tmp) / "letta.jsonl").read_text().splitlines()]
            self.assertTrue(model_log and letta_log)
            self.assertIn("/v1/agents/", json.dumps(letta_log))
            self.assertIn("real-fixture-call-1", json.dumps(letta_log))
            self.assertIn(result["nonce"], json.dumps(letta_log))


if __name__ == "__main__":
    unittest.main()
