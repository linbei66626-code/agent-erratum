"""OpenRouter transport: the opt-in profile, its fixed route, and its audit.

What is exercised here, offline (no network, no key, no model call):

* the REAL proxy builds the real send: `https://openrouter.ai/api/v1/chat/completions`,
  the wire model `qwen/qwen3-30b-a3b-instruct-2507`, and the pinned route
  `{order: [nebius], allow_fallbacks: false, require_parameters: true}` - captured from
  the opener the proxy would use, and from its own journal;
* a CLIENT that tries to choose a route is refused, and a config that declares another
  origin/model/profile is refused instead of being sent;
* the response must carry the responder's own identity: a missing or different
  `provider` is a transport issue and the audit refuses it;
* the transport audit re-derives all of it from the journal bytes, so tampering with the
  route, the model or the origin fails;
* the old SiliconFlow profile still records and audits exactly as before;
* the capacity gate still refuses a formal RUN for an OpenRouter candidate whose endpoint
  capacity has never been measured, and it stays BEFORE the send.

The provider is replaced by a loopback opener that records what it was asked to send.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ae_cloud_proxy as px  # noqa: E402
from ae_cloud_audit import audit_cloud_journal  # noqa: E402
from ae_model_proxy import ProxyBlocked  # noqa: E402

OPENROUTER_CANDIDATE = (ROOT / "configs"
                        / "ae-01__re-multiturn__openrouter.capacity-250k-candidate.json")
KEY = "offline-openrouter-fixture-key-01"


def _response(model, provider, *, usage=(5, 1), finish="stop"):
    payload = {
        "id": "gen-offline", "object": "chat.completion", "model": model,
        "provider": provider, "created": 0,
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": "offline"}}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                  "total_tokens": usage[0] + usage[1]}}
    return payload


class _Capture:
    """A loopback opener that records every request and replies with a fixture body."""

    def __init__(self, *, model, provider, chat_status=200, chat_payload=None):
        self.sent = []
        self.model, self.provider = model, provider
        self.chat_status, self.chat_payload = chat_status, chat_payload

    def open(self, request, timeout):
        raw = request.data
        self.sent.append({"url": request.full_url, "method": request.method,
                          "headers": dict(request.headers), "body": raw})
        if request.method == "GET":
            payload = {"object": "list", "data": [{"id": self.model, "object": "model",
                                                   "created": 0, "owned_by": "openrouter"}]}
            status = 200
        else:
            payload = self.chat_payload if self.chat_payload is not None else _response(
                self.model, self.provider)
            status = self.chat_status
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        class _Reply(io.BytesIO):
            def __init__(self):
                super().__init__(body)
                self.code, self.headers = status, {"x-siliconcloud-trace-id": None}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return _Reply()


class _ProxyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "proxy.private.jsonl"

    def openrouter_config(self, **overrides):
        record = json.loads(OPENROUTER_CANDIDATE.read_text(encoding="utf-8"))
        settings = dict(model=record["expected_model"],
                        max_output_tokens=record["max_output_tokens"],
                        max_request_bytes=record["max_request_bytes"],
                        max_response_bytes=record["max_response_bytes"],
                        max_requests=record["max_requests"], io_timeout_seconds=180,
                        upstream_origin=px.OPENROUTER_ORIGIN,
                        profile=px.OPENROUTER_PROFILE,
                        upstream_model=record["upstream_model"],
                        provider_slug=record["provider_slug"])
        settings.update(overrides)
        return px.CloudConfig(**settings)

    def siliconflow_config(self, **overrides):
        settings = dict(model=px.MODEL, max_output_tokens=2048, max_request_bytes=2097152,
                        max_response_bytes=16777216, max_requests=64,
                        io_timeout_seconds=180, upstream_origin=px.ORIGIN,
                        profile=px.COMPAT_PROFILE)
        settings.update(overrides)
        return px.CloudConfig(**settings)

    def proxy(self, config, capture):
        proxy = px.CloudAuditProxy(config, self.log, api_key=KEY)
        self.addCleanup(proxy.close)
        proxy.opener = capture
        return proxy

    def chat_body(self, config, *, model=None, extra=None):
        body = {"model": model or config.model,
                "messages": [{"role": "user", "content": "你好"}],
                "max_tokens": 2048, "stream": False, "temperature": 0}
        if extra:
            body.update(extra)
        return json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")


class OpenRouterSendConstructionTests(_ProxyCase):
    def test_the_real_send_uses_the_declared_url_model_and_pinned_route(self):
        config = self.openrouter_config()
        config.validate()
        capture = _Capture(model=px.OPENROUTER_WIRE_MODEL, provider="Nebius")
        proxy = self.proxy(config, capture)
        listing = proxy.dispatch("GET", "/v1/models", b"", "re/catalog", "agent_or_unknown")
        self.assertEqual(listing[0], 200)
        status, _reply = proxy.dispatch("POST", "/v1/chat/completions",
                                       self.chat_body(config), "re/stage",
                                       "agent_or_unknown")
        self.assertEqual(status, 200)
        # 1. the URL the provider would receive
        chat = [item for item in capture.sent if item["method"] == "POST"]
        self.assertEqual(len(chat), 1)
        self.assertEqual(chat[0]["url"], "https://openrouter.ai/api/v1/chat/completions")
        catalog = [item for item in capture.sent if item["method"] == "GET"]
        self.assertEqual(catalog[0]["url"], "https://openrouter.ai/api/v1/models")
        # 2. the wire body: the profile's model and the ONE pinned route
        sent = json.loads(chat[0]["body"].decode("utf-8"))
        self.assertEqual(sent["model"], px.OPENROUTER_WIRE_MODEL)
        self.assertEqual(sent["provider"], {"order": ["nebius"], "allow_fallbacks": False,
                                            "require_parameters": True})
        self.assertNotIn("route", sent)
        # 3. the journal records the same origin/path and the mapping/reason
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        upstream = [row for row in rows if row.get("kind") == "upstream_request"
                    and row.get("method") == "POST"][0]
        self.assertEqual(upstream["origin"], "https://openrouter.ai/api/v1")
        self.assertEqual(upstream["path"], "/chat/completions")
        normalized = [row for row in rows if row.get("kind") == "normalized_request"][0]
        operations = {change["operation"] + ":" + str(change["to"])
                      for change in normalized["changes"]}
        self.assertIn("replace:model", operations)
        self.assertIn("add:provider", operations)
        summary = [row for row in rows if row.get("kind") == "cloud_summary"][0]
        self.assertEqual(summary["response_provider"], "Nebius")
        self.assertEqual(summary["response_model"], px.OPENROUTER_WIRE_MODEL)
        self.assertEqual(summary["transport_issues"], [])

    def test_a_client_that_chooses_a_route_is_refused(self):
        """Each attempt uses a FRESH proxy: a refusal latches the proxy by design."""
        for index, extra in enumerate(({"provider": {"order": ["DeepInfra"]}},
                                       {"provider": {"allow_fallbacks": True}},
                                       {"route": "fallback"}, {"models": ["a", "b"]})):
            self.log = Path(self.tmp.name) / f"override-{index}.jsonl"
            config = self.openrouter_config()
            capture = _Capture(model=px.OPENROUTER_WIRE_MODEL, provider="Nebius")
            proxy = self.proxy(config, capture)
            with self.assertRaises(ProxyBlocked) as raised:
                proxy.dispatch("POST", "/v1/chat/completions",
                               self.chat_body(config, extra=extra), "re/stage",
                               "agent_or_unknown")
            self.assertIn("client_routing_not_allowed", str(raised.exception), extra)
            self.assertEqual([item for item in capture.sent if item["method"] == "POST"],
                             [], extra)

    def test_another_origin_or_model_or_profile_is_refused(self):
        cases = [
            dict(upstream_origin="https://api.siliconflow.cn"),
            dict(upstream_origin="https://evil.example/api/v1"),
            dict(profile=px.COMPAT_PROFILE),
            dict(model="Qwen/Qwen3-8B"),
            dict(upstream_model="qwen/qwen3-8b"),
            dict(provider_slug="deepinfra"),
            dict(provider_slug=None),
        ]
        for overrides in cases:
            with self.assertRaises(ValueError):
                self.openrouter_config(**overrides).validate()
        # ... and the SiliconFlow profile refuses the OpenRouter origin/model pair.
        with self.assertRaises(ValueError):
            self.siliconflow_config(upstream_origin=px.OPENROUTER_ORIGIN).validate()
        with self.assertRaises(ValueError):
            self.siliconflow_config(provider_slug="nebius").validate()


class OpenRouterResponseIdentityTests(_ProxyCase):
    def test_a_missing_or_wrong_response_provider_is_a_transport_issue(self):
        config = self.openrouter_config()
        for provider, expected in (("DeepInfra", "unexpected_response_provider"),
                                   (None, "missing_response_provider_identity"),
                                   ("", "missing_response_provider_identity")):
            raw = json.dumps(_response(px.OPENROUTER_WIRE_MODEL, provider),
                             ensure_ascii=False).encode()
            summary = px.response_summary(raw, 200, 2048, config)
            self.assertIn(expected, summary["transport_issues"], provider)
            self.assertEqual(summary["response_provider"], provider)
        # the accepted identity is recorded as-is (case preserved)
        raw = json.dumps(_response(px.OPENROUTER_WIRE_MODEL, "Nebius"),
                         ensure_ascii=False).encode()
        summary = px.response_summary(raw, 200, 2048, config)
        self.assertEqual(summary["transport_issues"], [])
        self.assertEqual(summary["response_provider"], "Nebius")

    def test_a_wrong_response_model_is_a_transport_issue(self):
        config = self.openrouter_config()
        raw = json.dumps(_response("some/other-model", "Nebius"),
                         ensure_ascii=False).encode()
        summary = px.response_summary(raw, 200, 2048, config)
        self.assertIn("unexpected_response_model", summary["transport_issues"])


class OpenRouterAuditTests(_ProxyCase):
    """The transport audit re-derives the route/model/origin from the journal bytes."""

    def _capture(self, *, model=None, provider="Nebius"):
        config = self.openrouter_config()
        capture = _Capture(model=model or px.OPENROUTER_WIRE_MODEL, provider=provider)
        proxy = self.proxy(config, capture)
        proxy.dispatch("GET", "/v1/models", b"", "re/catalog", "agent_or_unknown")
        proxy.dispatch("POST", "/v1/chat/completions", self.chat_body(config), "re/stage",
                       "agent_or_unknown")
        proxy.close()
        return config, capture

    def _rewrite(self, mutate):
        """Rewrite the sealed journal with one mutation, renumbering the sequences."""
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        rows = mutate(rows)
        for index, row in enumerate(rows):
            row["sequence"] = index
        self.log.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True)
                                      for row in rows) + "\n", encoding="utf-8")

    def _replace_body(self, rows, kind, mutate):
        for row in rows:
            if row.get("kind") == kind and "body_base64" in row:
                raw_body = base64.b64decode(row["body_base64"])
                if not raw_body:
                    continue          # the catalog GET carries no body
                body = json.loads(raw_body.decode("utf-8"))
                mutate(body)
                raw = json.dumps(body, ensure_ascii=False,
                                 separators=(", ", ": ")).encode("utf-8")
                # keep the record self-consistent so the refusal comes from the audit's
                # own route/model checks, not from a broken wire hash
                from ae_model_proxy import wire_record
                row.update(wire_record(raw))
                return row
        raise AssertionError(f"no {kind} record")

    def test_the_new_profile_records_and_audits_cleanly(self):
        self._capture()
        report = audit_cloud_journal(self.log)
        self.assertEqual(report["issues"], [], report["issues"])
        self.assertTrue(report["transport_capture_checked"])
        self.assertEqual(report["profile"], px.OPENROUTER_PROFILE)

    def test_a_tampered_route_or_model_fails_the_audit(self):
        """Any route/model change is refused; the audit re-derives the request itself.

        The common tamper (a body whose route no longer matches the declaration) is
        caught by the recomputation check `unaccounted_request_change`; the explicit
        `cloud_provider_route_changed` / `cloud_wire_model_changed` checks are defence
        in depth behind it. Either way the capture cannot pass.
        """
        self._capture()
        original = self.log.read_bytes()
        cases = [
            ("normalized_request", {"provider": {"order": ["DeepInfra"],
                                                 "allow_fallbacks": False,
                                                 "require_parameters": True}}),
            ("normalized_request", {"model": "some/other-model"}),
            ("normalized_request", {"provider": {"order": ["nebius"],
                                                 "allow_fallbacks": True,
                                                 "require_parameters": True}}),
            ("upstream_request", {"provider": {"order": ["DeepInfra"]}}),
            ("upstream_request", {"model": "some/other-model"}),
        ]
        for kind, patch in cases:
            self.log.write_bytes(original)
            self._rewrite(lambda rows: [self._replace_body(
                rows, kind, lambda body: body.update(patch))] and rows)
            issues = audit_cloud_journal(self.log)["issues"]
            self.assertTrue(issues, (kind, patch))
            self.assertIn(issues[0],
                          {"unaccounted_request_change", "cloud_provider_route_changed",
                           "cloud_wire_model_changed"}, (kind, patch))
        self.log.write_bytes(original)
        self.assertEqual(audit_cloud_journal(self.log)["issues"], [])

    def test_a_tampered_origin_fails_the_audit(self):
        self._capture()
        self._rewrite(lambda rows: [row.__setitem__("origin", "https://openrouter.ai")
                                    if row.get("kind") == "upstream_request" else None
                                    for row in rows] and rows)
        self.assertIn("upstream_route_mismatch", audit_cloud_journal(self.log)["issues"])

    def test_a_wrong_recorded_response_provider_fails_the_audit(self):
        self._capture()
        self._rewrite(lambda rows: [row.__setitem__("response_provider", "DeepInfra")
                                    if row.get("kind") == "cloud_summary" else None
                                    for row in rows] and rows)
        self.assertIn("unexpected_response_provider", audit_cloud_journal(self.log)["issues"])

    def test_a_missing_recorded_response_provider_fails_the_audit(self):
        self._capture()
        self._rewrite(lambda rows: [row.pop("response_provider", None)
                                    if row.get("kind") == "cloud_summary" else None
                                    for row in rows] and rows)
        self.assertIn("missing_response_provider_identity",
                      audit_cloud_journal(self.log)["issues"])

    def test_the_old_siliconflow_path_still_audits(self):
        config = self.siliconflow_config()
        capture = _Capture(model=px.MODEL, provider=None)
        proxy = self.proxy(config, capture)
        proxy.dispatch("GET", "/v1/models", b"", "re/catalog", "agent_or_unknown")
        proxy.dispatch("POST", "/v1/chat/completions",
                       json.dumps({"model": px.MODEL,
                                   "messages": [{"role": "user", "content": "你好"}],
                                   "max_completion_tokens": 2048, "stream": False},
                                  ensure_ascii=False).encode(), "re/stage",
                       "agent_or_unknown")
        proxy.close()
        self.assertEqual(capture.sent[1]["url"],
                         "https://api.siliconflow.cn/v1/chat/completions")
        report = audit_cloud_journal(self.log)
        self.assertEqual(report["issues"], [], report["issues"])
        # the old path carries no routing block and no provider identity requirement
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        summary = [row for row in rows if row.get("kind") == "cloud_summary"][0]
        # The sealed SiliconFlow summary shape is unchanged: no provider field at all.
        self.assertNotIn("response_provider", summary)
        self.assertNotIn("provider", json.loads(base64.b64decode(
            [row for row in rows if row.get("kind") == "normalized_request"][0][
                "body_base64"]).decode()))


class OpenRouterCapacityGateTests(_ProxyCase):
    def test_an_unmeasured_openrouter_candidate_cannot_start_a_run(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ae_01_cloud_re_multiturn_openrouter",
            ROOT / "scripts/ae_01_cloud_re_multiturn.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        from ae_cloud_re_multiturn import validate_config
        record = json.loads(OPENROUTER_CANDIDATE.read_text(encoding="utf-8"))
        config = validate_config(copy.deepcopy(record))
        state = cli.capacity_decision(config, "plan")
        self.assertFalse(state["run_allowed"])
        self.assertIn("unverified", str(state["open_item"]))
        with self.assertRaises(RuntimeError) as raised:
            cli.capacity_decision(config, "run")
        self.assertIn("not verified", str(raised.exception))
        # The SiliconFlow measurement is NOT inherited as this endpoint's evidence.
        self.assertIsNone(config["capacity"]["service_limit"]["endpoint_measured_tokens"])

    def test_the_capacity_gate_still_runs_before_the_send(self):
        """An over-capacity request produces ZERO upstream sends, on this profile too."""
        config = self.openrouter_config()
        capture = _Capture(model=px.OPENROUTER_WIRE_MODEL, provider="Nebius")
        proxy = self.proxy(config, capture)
        proxy.arm_capacity(context_window=64, agent_reserve_tokens=2048,
                           auxiliary_reserve_tokens=4096,
                           count_request=lambda normalized, role: 10_000,
                           count_source="offline_fixture_counter")
        with self.assertRaises(ProxyBlocked) as raised:
            proxy.dispatch("POST", "/v1/chat/completions", self.chat_body(config),
                           "re/stage", "agent_or_unknown")
        self.assertIn("capacity_exceeded_before_send", str(raised.exception))
        self.assertEqual([item for item in capture.sent if item["method"] == "POST"], [])
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        self.assertEqual([row["kind"] for row in rows if row["kind"] == "capacity_check"]
                         and [row["kind"] for row in rows
                              if row["kind"] == "capacity_check"][0], "capacity_check")
        self.assertFalse([row for row in rows if row["kind"] == "capacity_check"][0]["fits"])


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
