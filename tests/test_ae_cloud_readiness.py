"""Offline readiness checks for the single-t4 cloud capability transport candidate.

Every reply in this module is an invented fixture. No key is read, no socket is
opened and no model/service is called: the proxy uses an injected opener and the
driver fixtures are scripted. A passing test here is a configuration/transport
boundary check, never a capability or scientific result.

Covered: candidate CloudConfig shape and provenance, byte/output/timeout limits,
the agent 2048 / auxiliary 4096 output allowance without automatic enlargement,
the legacy 256-token connection profile still refusing capability-sized output,
verbatim message/tool/tool-return/user pass-through, refusal of seed and
undeclared fields, three independently bounded request constraints plus the 256
safety cap (no summed total is claimed), and the shapes actually present in the
transferred real connection journal. The explicit `llm_config` no-catalog check
here is driver-fixture-path coverage only; its real grounds are the pinned source
and that same journal, not this fixture.

NOT covered (no offline substitute exists): the complete rendered task-input
audit, account TPM/429 admission, `tools=null` or other capability-task field
combinations against the real service, a whole-run request upper bound, task
correctness, and any scientific claim. Cloud seed omission is covered by
`tests/test_ae_native_integration.py` on the real native generate path and is
deliberately not re-asserted here.
"""
from copy import deepcopy
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Reuse the existing fixture modules whether this file is imported as
# `tests.test_ae_cloud_readiness` or discovered from `-s tests`.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from ae_cloud_proxy import (
    CloudAuditProxy, CloudConfig, COMPAT_PROFILE, MODEL, ORIGIN, PROFILE,
    normalize_request,
)
from ae_cloud_task import CloudExecutionProfile
from ae_capability import validate_config as validate_capability_config
from ae_model_proxy import ProxyBlocked

KEY = "sk-readiness-fixture-not-real-1234"
CANDIDATE = ROOT / "configs/ae-01__cloud-transport__siliconflow.capability-candidate.json"
CAPABILITY = ROOT / "configs/ae-01__capability__siliconflow.prototype.json"
CONNECTION = ROOT / "configs/ae-01__cloud-connection__siliconflow.prototype.json"
LEGACY_TRANSPORT = ROOT / "configs/ae-01__cloud-transport__siliconflow.letta-probe.json"

# Small enough to stay a fixture, never a provider response.
FIXTURE_REPLY = json.dumps({
    "model": MODEL, "id": "fixture", "system_fingerprint": "fixture-revision",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "fixture, not a model"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
}, ensure_ascii=False).encode()


class FixtureReply:
    def __init__(self, raw, status=200, trace="readiness-fixture"):
        self._raw, self.code = raw, status
        self.headers = {"x-siliconcloud-trace-id": trace}

    def read(self, _limit=None):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class RecordingOpener:
    """Records outbound request objects; never opens a real connection."""

    def __init__(self, proxy, reply=FIXTURE_REPLY, status=200):
        self.proxy, self.reply, self.status, self.sent = proxy, reply, status, []

    def open(self, request, timeout):
        record = json.loads(self.proxy.journal.path.read_text().splitlines()[-1])
        assert record["kind"] == "upstream_request", record["kind"]
        self.sent.append(request)
        assert request.full_url.startswith(ORIGIN)
        return FixtureReply(self.reply, self.status)


def candidate_bytes():
    return CANDIDATE.read_bytes()


def candidate_config():
    return CloudConfig(**json.loads(candidate_bytes()))


def compatible_body(**overrides):
    """Shape pinned Letta 0.9/0.16 emits through the v2 transport."""
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": "系统：只读偏好，不得修改。"},
                     {"role": "user", "content": "{\"source\":\"current_task\"}"}],
        "tools": [{"type": "function", "function": {"name": "create_order",
                   "parameters": {"type": "object", "properties": {}}}}],
        "tool_choice": "auto", "stream": False, "n": 1,
        "max_completion_tokens": 2048, "temperature": 0,
        "user": "U000828", "parallel_tool_calls": False,
    }
    body.update(overrides)
    return body


class CandidateConfigTests(unittest.TestCase):
    """The candidate is a transport candidate only; it is never a capability run."""

    def test_candidate_file_exists_and_parses_as_cloud_config(self):
        config = candidate_config()
        config.validate()
        self.assertEqual(config.profile, COMPAT_PROFILE)
        self.assertEqual((config.model, config.upstream_origin), (MODEL, ORIGIN))
        self.assertEqual((config.max_output_tokens, config.max_requests), (4096, 256))
        self.assertEqual((config.max_request_bytes, config.max_response_bytes),
                         (2097152, 16777216))
        self.assertEqual(config.io_timeout_seconds, 180)
        # The candidate carries transport keys only; execution/profile provenance
        # lives in the capability config so no field is silently duplicated.
        self.assertEqual(set(json.loads(candidate_bytes())), {
            "profile", "upstream_origin", "model", "max_output_tokens",
            "max_request_bytes", "max_response_bytes", "max_requests",
            "io_timeout_seconds",
        })

    def test_candidate_limits_match_the_capability_config(self):
        config = candidate_config()
        capability = json.loads(CAPABILITY.read_text())
        # The execution profile is proven to accept the file as the cloud-selectable
        # capability config; the legacy pinned-value check then runs on a copy that
        # mirrors exactly the conversion the profile performs internally.
        self.assertEqual(CloudExecutionProfile("capability").validate(capability), capability)
        legacy_view = deepcopy(capability)
        legacy_view.pop("transport_profile")
        legacy_view.pop("context_window_source")
        legacy_view.update(schema_version="ae-01-capability-probe-0.1",
                           expected_model="Qwen3-4B-Instruct-2507",
                           model_handle="vllm/Qwen3-4B-Instruct-2507")
        validate_capability_config(legacy_view)
        self.assertEqual(json.loads(CAPABILITY.read_text()), capability)
        for transport_key, config_key in (("max_request_bytes", "max_request_bytes"),
                                          ("max_response_bytes", "max_response_bytes"),
                                          ("io_timeout_seconds", "timeout_seconds")):
            with self.subTest(key=transport_key):
                self.assertEqual(getattr(config, transport_key), capability[config_key])
        # 4096 covers agent 2048 and auxiliary 4096 exactly; it is not a raise of
        # either capability value.
        self.assertEqual(config.max_output_tokens,
                         max(capability["max_output_tokens"], capability["auxiliary_output_tokens"]))
        self.assertEqual(capability["max_output_tokens"], 2048)
        self.assertEqual(capability["auxiliary_output_tokens"], 4096)
        self.assertEqual(capability["transport_profile"], COMPAT_PROFILE)

    def test_candidate_request_budget_is_a_safety_truncation_budget(self):
        """Separate verified limits and one safety cap; no invented total.

        `max_stage_posts` and `max_user_exchanges` are MAXIMA, not guaranteed call
        counts, so no sum of them is a floor on real cloud requests. A whole-run
        upper bound is NOT proven in this round.
        """
        budget = candidate_config().max_requests
        capability = json.loads(CAPABILITY.read_text())
        self.assertIs(type(budget), int)
        self.assertGreater(budget, 6)  # strictly above the connection probe's budget
        self.assertLessEqual(budget, 256)  # safety cap only
        # 1. Driver issues exactly one model catalog GET of its own (probe path).
        # 2. The task bridge hard-gates POST /messages at max_stage_posts, and the
        #    counter is reset only at the t4 begin, so posts <= 64 for the task.
        # 3. The outer user-simulator loop calls at most max_user_exchanges times.
        # Pinned values, each an independent maximum rather than a call count.
        self.assertEqual(capability["max_stage_posts"], 64)
        self.assertEqual(capability["max_user_exchanges"], 12)
        self.assertEqual(capability["max_rounds"], 32)
        self.assertEqual(capability["max_steps"], 3)
        # Not proven and therefore not asserted: step-internal LLM requests, the
        # sliding-window evaluator, cumulative tool-return input, or any whole-run
        # cloud request total. Reaching the cap latches the proxy (no retry) and
        # the run is INVALID, not a scientific failure.

    def test_candidate_did_not_touch_existing_transport_configs(self):
        legacy = json.loads(LEGACY_TRANSPORT.read_text())
        self.assertEqual(legacy["max_output_tokens"], 256)
        self.assertEqual(legacy["max_requests"], 6)
        self.assertEqual(legacy["io_timeout_seconds"], 60)
        v1 = json.loads((ROOT / "configs/ae-01__cloud-transport__siliconflow.prototype.json").read_text())
        self.assertEqual(v1["profile"], PROFILE)
        self.assertEqual(v1["max_output_tokens"], 256)
        for path in (CONNECTION, CAPABILITY):
            self.assertNotIn("max_requests", json.loads(path.read_text()))
        with tempfile.TemporaryDirectory() as tmp:
            before = {p.name: p.read_bytes() for p in (LEGACY_TRANSPORT, CONNECTION, CAPABILITY)}
            proxy = CloudAuditProxy(candidate_config(), Path(tmp) / "capture.private.jsonl", api_key=KEY)
            proxy.close()
            self.assertEqual(before, {p.name: p.read_bytes() for p in (LEGACY_TRANSPORT, CONNECTION, CAPABILITY)})


class OutputLimitTests(unittest.TestCase):
    """2048 and 4096 are allowed and stay exactly as requested."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proxy = CloudAuditProxy(candidate_config(),
                                     Path(self.tmp.name) / "capture.private.jsonl", api_key=KEY)
        self.addCleanup(self.proxy.close)
        self.opener = RecordingOpener(self.proxy)
        self.proxy.opener = self.opener

    def dispatch(self, body, purpose="capability/stage", role="agent_or_unknown"):
        raw = json.dumps(body, ensure_ascii=False).encode()
        return self.proxy.dispatch("POST", "/v1/chat/completions", raw, purpose, role)

    def test_agent_2048_and_auxiliary_4096_are_allowed_but_never_enlarged(self):
        for limit in (2048, 4096):
            with self.subTest(limit=limit):
                body = compatible_body(max_completion_tokens=limit)
                status, raw = self.dispatch(body)
                self.assertEqual(status, 200)
                sent = json.loads(self.opener.sent[-1].data)
                self.assertEqual(sent["max_tokens"], limit)  # recorded rename only
                self.assertNotIn("max_completion_tokens", sent)
                self.assertEqual(set(sent), set(body) - {"max_completion_tokens"} | {"max_tokens"})
                # No automatic enlargement toward the 4096 transport ceiling.
                self.assertLessEqual(sent["max_tokens"], self.proxy.config.max_output_tokens)

    def test_above_ceiling_is_refused_without_reaching_upstream(self):
        with self.assertRaises(ProxyBlocked) as caught:
            self.dispatch(compatible_body(max_completion_tokens=4097))
        self.assertEqual(caught.exception.code, "output_limit_exceeded")
        self.assertEqual(self.opener.sent, [])
        self.assertTrue(self.proxy.blocked)  # no silent retry or repair

    def test_two_limits_or_missing_limit_is_refused(self):
        for body in (compatible_body(max_tokens=2048),  # two explicit limits
                     {k: v for k, v in compatible_body().items() if k != "max_completion_tokens"}):
            with self.subTest(body=sorted(body)):
                with self.assertRaises(ProxyBlocked):
                    normalize_request(json.dumps(body, ensure_ascii=False).encode(),
                                      candidate_config())


class LegacyConnectionStillRefusesCapabilityOutputTests(unittest.TestCase):
    def test_old_256_transport_rejects_2048_and_4096(self):
        legacy = json.loads(LEGACY_TRANSPORT.read_text())
        legacy_config = CloudConfig(**legacy)
        legacy_config.validate()
        self.assertIn(legacy_config.profile, (PROFILE, COMPAT_PROFILE))
        for limit in (2048, 4096):
            with self.subTest(limit=limit), self.assertRaises(ProxyBlocked) as caught:
                normalize_request(json.dumps(compatible_body(max_completion_tokens=limit),
                                             ensure_ascii=False).encode(), legacy_config)
            self.assertEqual(caught.exception.code, "output_limit_exceeded")
        # The 256 generation still passes through unchanged.
        normalized, changes = normalize_request(
            json.dumps(compatible_body(max_completion_tokens=256), ensure_ascii=False).encode(),
            legacy_config)
        self.assertEqual(json.loads(normalized)["max_tokens"], 256)
        self.assertEqual([c["operation"] for c in changes], ["rename"])

    def test_capability_request_against_connection_transport_config_is_refused_at_limit(self):
        # The connection transport must never be pressed into capability duty: its
        # 256 ceiling refuses a capability-sized request before any upstream call.
        legacy_config = CloudConfig(**json.loads(LEGACY_TRANSPORT.read_text()))
        capability = json.loads(CAPABILITY.read_text())
        self.assertNotEqual(legacy_config.max_output_tokens, capability["max_output_tokens"])
        with self.assertRaises(ProxyBlocked):
            normalize_request(json.dumps(compatible_body(
                max_completion_tokens=capability["max_output_tokens"]), ensure_ascii=False).encode(),
                legacy_config)


class ContentPassThroughTests(unittest.TestCase):
    """Messages, tools, tool returns, user metadata and the seed policy are untouched."""

    def test_messages_tools_and_tool_returns_are_byte_preserved(self):
        body = compatible_body()
        body["messages"] = body["messages"] + [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call-fixture-1", "type": "function",
                             "function": {"name": "create_order",
                                          "arguments": "{\"product_ids\":[\"S1_P1\"]}"}}]},
            {"role": "tool", "tool_call_id": "call-fixture-1",
             "content": "{\"status\":\"paid\",\"order_id\":\"O-fixture-1\"}"},
        ]
        raw = json.dumps(body, ensure_ascii=False).encode()
        normalized, changes = normalize_request(raw, candidate_config())
        before, after = json.loads(raw), json.loads(normalized)
        self.assertEqual(before["messages"], after["messages"])
        self.assertEqual(before["tools"], after["tools"])
        self.assertEqual(before["tool_choice"], after["tool_choice"])
        self.assertEqual(before["user"], after["user"])
        self.assertIs(before["parallel_tool_calls"], after["parallel_tool_calls"])
        self.assertFalse(before["parallel_tool_calls"])
        self.assertEqual(before["temperature"], after["temperature"])
        self.assertEqual(set(before) ^ set(after), {"max_completion_tokens", "max_tokens"})
        self.assertEqual([c["from"] for c in changes], ["max_completion_tokens"])
        # Original raw bytes are untouched; only a new normalized copy is returned.
        self.assertEqual(json.loads(raw)["max_completion_tokens"], before["max_completion_tokens"])

    def test_tools_null_is_offline_only_and_service_uncovered(self):
        """The `tools=null` allowance is offline-supported, NOT service-verified.

        The two real normalized requests in the connection journal both carry a
        non-null `tools` array, so the real provider has never been probed with
        `tools=null`. This test only pins the offline transport behaviour; the
        separate journal test below records what the real service actually saw.
        """
        body = compatible_body(tools=None)
        normalized, _ = normalize_request(json.dumps(body, ensure_ascii=False).encode(),
                                          candidate_config())
        self.assertIsNone(json.loads(normalized)["tools"])
        # The same shape is refused by the legacy strict profile.
        legacy_config = CloudConfig(**json.loads(LEGACY_TRANSPORT.read_text()))
        with self.assertRaises(ProxyBlocked):
            normalize_request(json.dumps(body, ensure_ascii=False).encode(), legacy_config)

    def test_real_connection_journal_shapes_are_not_tools_null(self):
        """Evidence, not fixture: read the actual r1 cloud journal shapes.

        Skips when the transferred evidence is absent. Only top-level request
        shape fields are inspected; message bodies are never read or logged.
        """
        journal = (ROOT / "transfers/lab-cloud-connection-20260911-r1"
                   / "ae-cloud-connection-20260911-r1.private.jsonl")
        if not journal.is_file():
            self.skipTest("transferred cloud connection journal is not present")
        shapes = []
        for line in journal.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("kind") != "normalized_request":
                continue
            body = json.loads(base64.b64decode(record["body_base64"]))
            shapes.append({
                "keys": set(body),
                "user_present": "user" in body,
                "parallel_tool_calls": body.get("parallel_tool_calls"),
                "tools_present": "tools" in body,
                "tools_is_null": body.get("tools") is None,
            })
        self.assertEqual(len(shapes), 2)
        for shape in shapes:
            self.assertTrue(shape["user_present"])
            self.assertIs(shape["parallel_tool_calls"], False)
            self.assertTrue(shape["tools_present"])
            self.assertFalse(shape["tools_is_null"])
            self.assertNotIn("seed", shape["keys"])

    def test_undeclared_and_seed_fields_are_refused_not_dropped(self):
        for extra in ({"seed": 300}, {"chat_template_kwargs": {}},
                      {"parallel_tool_calls": True}, {"user": []},
                      {"top_logprobs": 3}, {"stream_options": {"include_usage": True}}):
            with self.subTest(field=sorted(extra)):
                raw = json.dumps(compatible_body(**extra), ensure_ascii=False).encode()
                with self.assertRaises(ProxyBlocked):
                    normalize_request(raw, candidate_config())

    def test_explicit_llm_config_path_makes_no_letta_catalog_request(self):
        """Driver-fixture-path coverage only; it is NOT service evidence.

        This exercises the injected fixtures. The real framework grounds are the
        pinned source (`create_agent_async` skips
        `get_llm_config_from_handle_async` whenever `request.llm_config` is
        supplied) plus the actual connection journal, which shows exactly one
        catalog GET for the whole run and none from Letta. Seed omission on the
        real native generate path is already covered by
        `tests/test_ae_native_integration.py::test_cloud_native_requests_omit_seed_preserve_real_tools_and_judge`.
        """
        from ae_probe import execute_probe
        from test_ae_probe import ModelFixture
        from test_ae_cloud_task import CloudLetta, cfg
        cloud_config = cfg("connection")
        model = ModelFixture(MODEL)
        model.data = [{"id": MODEL}]  # cloud catalog carries no max_model_len
        letta = CloudLetta(config=cloud_config)
        result = execute_probe(cloud_config, model_transport=model, letta_transport=letta,
                               execution_profile=CloudExecutionProfile("connection"))
        self.assertTrue(result["connectivity_passed"], result["invalid_reasons"])
        self.assertEqual(model.requests, [("GET", "/v1/models", None)])
        letta_model_calls = [r for r in letta.requests if "/v1/models" in str(r[1])]
        self.assertEqual(letta_model_calls, [])

    def test_streaming_multi_completion_and_wrong_model_are_refused(self):
        for extra in ({"stream": True}, {"n": 2}, {"model": "other/model"}):
            with self.subTest(field=sorted(extra)):
                with self.assertRaises(ProxyBlocked):
                    normalize_request(json.dumps(compatible_body(**extra), ensure_ascii=False).encode(),
                                      candidate_config())


class ProxyBoundaryTests(unittest.TestCase):
    def test_request_budget_stops_further_calls_without_retry(self):
        import dataclasses
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = dataclasses.replace(candidate_config(), max_requests=1)
        proxy = CloudAuditProxy(config, Path(tmp.name) / "capture.private.jsonl", api_key=KEY)
        self.addCleanup(proxy.close)
        opener = RecordingOpener(proxy)
        proxy.opener = opener
        raw = json.dumps(compatible_body(max_completion_tokens=2048), ensure_ascii=False).encode()
        self.assertEqual(proxy.dispatch("POST", "/v1/chat/completions", raw)[0], 200)
        with self.assertRaises(ProxyBlocked) as caught:
            proxy.dispatch("POST", "/v1/chat/completions", raw)
        self.assertEqual(caught.exception.code, "proxy_stopped_or_request_limit")
        self.assertEqual(len(opener.sent), 1)  # exactly one upstream attempt, no retry

    def test_candidate_max_requests_parses_from_its_own_file(self):
        import dataclasses
        parsed = json.loads(candidate_bytes())
        config = CloudConfig(**parsed)
        self.assertEqual(dataclasses.asdict(config)["max_requests"], parsed["max_requests"])


class ReadinessCliTests(unittest.TestCase):
    def test_transport_cli_plan_only_needs_no_key_and_no_journal(self):
        spec = importlib.util.spec_from_file_location(
            "readiness_cloud_cli", ROOT / "scripts/ae_01_cloud_proxy.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp, \
             patch("sys.argv", ["cloud-proxy", "--config", str(CANDIDATE)]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(module.main(), 0)
            payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "PLAN_ONLY")
        self.assertFalse(payload["key_loaded"])
        self.assertFalse(payload["network_called"])
        self.assertFalse(payload["task_runner_ready"])
        self.assertEqual(payload["config"]["max_requests"], 256)
        self.assertEqual(payload["config"]["max_output_tokens"], 4096)

    def test_capability_config_alone_does_not_carry_transport_limits(self):
        capability = json.loads(CAPABILITY.read_text())
        for key in ("profile", "upstream_origin", "max_requests", "io_timeout_seconds"):
            self.assertNotIn(key, capability)
        profile = CloudExecutionProfile("capability")
        self.assertEqual(profile.validate(capability)["transport_profile"], COMPAT_PROFILE)
        # The candidate carries no key material or private content and no group/world bits.
        self.assertIn(os.stat(CANDIDATE).st_mode & 0o777, (0o600, 0o644))
        self.assertNotIn(KEY, CANDIDATE.read_text())
        self.assertEqual(deepcopy(capability), json.loads(CAPABILITY.read_text()))


if __name__ == "__main__":
    unittest.main()
