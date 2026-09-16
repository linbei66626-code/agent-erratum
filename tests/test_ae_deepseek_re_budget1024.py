"""The 1024-request budget candidate: one explicit budget, end to end.

The live run `ae-deepseek-re-live-20260916-r3` finished 13 of 18 phases and was stopped by
the LOCAL proxy at exactly 256 requests (255 generation POSTs + 1 `/models` GET), NOT by
the provider. A second reviewed candidate now declares 1024, and this suite pins the whole
chain that number travels through:

* the candidate file itself (its diff against the 256 one is exactly `max_requests`);
* the 0.4 declaration contract (only 256/1024 are accepted; nothing defaults to 1024);
* the REAL CLI PLAN, which must record the budget it will really spend;
* the REAL proxy dispatch boundary, counted the way the proxy counts (GET and POST share
  one counter, no reset), for BOTH candidates;
* the post-hoc audit's public entry, over the real 0.4 chain fixture, which must accept a
  legitimate 1024 budget and refuse a plan/proxy/capture that disagree with it.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

CANDIDATE_256 = ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-candidate.json"
CANDIDATE_1024 = (ROOT / "configs"
                  / "ae-01__re-multiturn__deepseek-flash.serial-budget1024-candidate.json")
QWEN_CANDIDATE = (ROOT / "configs"
                  / "ae-01__re-multiturn__siliconflow.capacity-250k-measured-candidate.json")
OPTION = "--exploratory-capacity-option"
FAKE_KEY = "sk-" + "z" * 40


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(path=CANDIDATE_256):
    record = json.loads(path.read_text(encoding="utf-8"))
    record["letta_origin"] = "http://127.0.0.1:8283"
    record["model_origin"] = "http://127.0.0.1:8000"
    return record


class TheCandidateFileTests(unittest.TestCase):
    def test_the_only_difference_is_the_budget(self):
        old = json.loads(CANDIDATE_256.read_text(encoding="utf-8"))
        new = json.loads(CANDIDATE_1024.read_text(encoding="utf-8"))
        self.assertEqual(new["max_requests"], 1024)
        self.assertEqual(old["max_requests"], 256)
        differing = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
        self.assertEqual(differing, {"max_requests"},
                         "the 1024 candidate must differ in exactly one field")
        self.assertEqual(CANDIDATE_256.read_bytes(),
                         CANDIDATE_256.read_bytes(),
                         "the historical candidate's bytes are kept")

    def test_only_the_two_reviewed_budgets_are_accepted(self):
        driver = _load("ae_deepseek_re_transport_budget", ROOT / "ae_deepseek_re_transport.py")
        self.assertEqual(list(driver.ACCEPTED_REQUEST_BUDGETS), [256, 1024])
        for value in (257, 1025, 255, 0, -1, True, "1024", None, 1024.0):
            with self.subTest(value=value):
                record = _candidate()
                record["max_requests"] = value
                with self.assertRaises(driver.DeclarationRejected):
                    driver.validate_deepseek_run_limits(record)
                with self.assertRaises(driver.DeclarationRejected):
                    driver.validate_serial_pacing(record)
        for value, unchanged in ((256, True), (1024, False)):
            record = _candidate()
            record["max_requests"] = value
            limits = driver.validate_deepseek_run_limits(record)
            self.assertEqual(limits["limits"]["max_requests"], value)
            self.assertEqual(limits["request_budget"]["declared"], value)
            self.assertIs(limits["request_budget"]["budget_unchanged"], unchanged)
            pacing = driver.validate_serial_pacing(record)
            self.assertEqual(pacing["request_budget"], value)
            self.assertIs(pacing["budget_unchanged"], unchanged)
            self.assertIn("never enlarges the budget", pacing["note"])


class TheRealCliPlanTests(unittest.TestCase):
    """The production entry point, offline: PLAN records the budget it will spend."""

    @classmethod
    def setUpClass(cls):
        cls.chain = _load("re_multiturn_chain_for_budget",
                          ROOT / "tests/re_multiturn_chain.py")
        cls.cli = _load("ae_01_cloud_re_multiturn_for_budget",
                        ROOT / "scripts/ae_01_cloud_re_multiturn.py")
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            raise unittest.SkipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))
        cls.dataset = dataset

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.config_path = self.tmp / "candidate.json"
        self.vita_source = Path(os.environ.get(
            "AE_VITA_SOURCE", ROOT / ".ae-verify-src/source"))

    def _plan(self, record, *, with_option=True):
        self.config_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
        out = self.tmp / ("out-" + str(len(list(self.tmp.iterdir()))))
        argv = ["--stage", "plan", "--config", str(self.config_path),
                "--dataset", str(self.dataset), "--vita-source", str(self.vita_source),
                "--output-dir", str(out)]
        if with_option:
            argv.append(OPTION)
        code = self.cli.main(argv)
        return code, out

    def test_the_1024_candidate_plans_with_its_own_budget(self):
        code, out = self._plan(_candidate(CANDIDATE_1024))
        self.assertEqual(code, 0)
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["config"]["max_requests"], 1024)
        budget = plan["scope"]["pair_request_budget"]
        self.assertEqual(budget["max_requests"], 1024)
        self.assertIs(budget["budget_unchanged"], False)
        self.assertIn("candidate's own max_requests", budget["source"])
        self.assertIn("256", budget["source"])
        self.assertFalse(plan["network_called"])
        self.assertFalse(plan["model_called"])

    def test_the_256_candidate_still_plans_256(self):
        code, out = self._plan(_candidate(CANDIDATE_256))
        self.assertEqual(code, 0)
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        budget = plan["scope"]["pair_request_budget"]
        self.assertEqual(budget["max_requests"], 256)
        # It is still a 0.4 candidate, so its record also states the source - and it says
        # truthfully that the historical baseline was kept.
        self.assertIs(budget["budget_unchanged"], True)
        self.assertIn("candidate's own max_requests", budget["source"])

    def test_the_exploratory_option_is_still_required(self):
        code, out = self._plan(_candidate(CANDIDATE_1024), with_option=False)
        self.assertNotEqual(code, 0)
        self.assertFalse((out / "plan.json").exists())
        self.assertFalse((out / "result.json").exists())

    def test_a_qwen_candidate_may_not_borrow_the_1024_budget(self):
        record = json.loads(QWEN_CANDIDATE.read_text(encoding="utf-8"))
        record["max_requests"] = 1024
        code, out = self._plan(record)
        self.assertNotEqual(code, 0, "the old protocols keep their own budget contract")
        self.assertFalse((out / "plan.json").exists())

    def test_an_unreviewed_budget_is_refused_by_the_cli(self):
        record = _candidate(CANDIDATE_1024)
        record["max_requests"] = 1025
        code, out = self._plan(record)
        self.assertNotEqual(code, 0)
        self.assertFalse((out / "plan.json").exists())


class TheProxyBoundaryTests(unittest.TestCase):
    """The REAL proxy counter: GET and POST share it, and it stops exactly at the budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _proxy(self, budget, *, name="proxy"):
        import ae_cloud_proxy as px
        cloud = px.CloudConfig(
            model="deepseek-flash", max_output_tokens=4096, max_request_bytes=2097152,
            max_response_bytes=16777216, max_requests=budget, io_timeout_seconds=180.0,
            upstream_origin=px.DEEPSEEK_ORIGIN, profile=px.DEEPSEEK_PROFILE,
            upstream_model="deepseek-flash", declared_request_byte_budget=2097152,
            pace_seconds=0)
        cloud.validate()
        journal = Path(self.tmp.name) / f"{name}.jsonl"
        proxy = px.CloudAuditProxy(cloud, journal, api_key=FAKE_KEY,
                                   clock=lambda: 1.0, sleep=lambda seconds: None)
        opener = _RecordingOpener()
        proxy.opener = opener
        declared = _candidate(CANDIDATE_1024 if budget == 1024 else CANDIDATE_256)

        def count_bytes(normalized, role):
            return 2097152

        proxy.arm_capacity_from_declaration(
            declared, count_request=count_bytes, count_source=px.BYTE_GATE_COUNT_BASIS,
            count_basis=px.BYTE_GATE_COUNT_BASIS)
        return proxy, opener

    @staticmethod
    def _small_body(index):
        return json.dumps({"model": "deepseek-flash",
                           "messages": [{"role": "user", "content": f"r{index}"}],
                           "max_tokens": 2048, "temperature": 0, "stream": False,
                           "thinking": {"type": "disabled"}}).encode("utf-8")

    def _boundary(self, budget, *, name):
        import ae_model_proxy
        proxy, opener = self._proxy(budget, name=name)
        status, _reply = proxy.dispatch("GET", "/v1/models", b"", "re/catalog", "agent")
        self.assertEqual(status, 200)
        for index in range(budget - 1):
            status, _reply = proxy.dispatch("POST", "/v1/chat/completions",
                                            self._small_body(index), "re/stage", "agent")
            self.assertEqual(status, 200, f"request {index} refused before the budget")
        self.assertEqual(proxy.request_count, budget)
        sent = len(opener.sent)
        with self.assertRaises(ae_model_proxy.ProxyBlocked) as caught:
            proxy.dispatch("POST", "/v1/chat/completions", self._small_body(budget),
                           "re/stage", "agent")
        self.assertIn("proxy_stopped_or_request_limit", str(caught.exception))
        self.assertEqual(len(opener.sent), sent, "a refused request must not be sent")
        self.assertEqual(proxy.request_count, budget, "the refused request is not counted")
        # Repeated local arrivals after the stop cannot widen the upstream boundary.
        for _ in range(3):
            with self.assertRaises(ae_model_proxy.ProxyBlocked):
                proxy.dispatch("POST", "/v1/chat/completions", self._small_body(budget),
                               "re/stage", "agent")
        self.assertEqual(len(opener.sent), sent)
        proxy.close()
        return opener

    def test_the_1024_boundary_is_exact(self):
        opener = self._boundary(1024, name="budget1024")
        posts = [entry for entry in opener.sent
                 if entry["url"].endswith("/chat/completions")]
        self.assertEqual(len(posts), 1023, "1 GET + 1023 POST = 1024 served requests")
        self.assertEqual(sum(1 for entry in opener.sent
                             if entry["url"].endswith("/models")), 1)

    def test_the_256_boundary_keeps_its_semantics(self):
        opener = self._boundary(256, name="budget256")
        self.assertEqual(len(opener.sent), 256)

    def test_a_proxy_config_must_match_the_candidate_it_serves(self):
        cli = _load("ae_01_cloud_proxy_for_budget",
                    ROOT / "scripts/ae_01_cloud_proxy.py")
        import ae_cloud_proxy as px
        matching = px.CloudConfig(
            model="deepseek-flash", max_output_tokens=4096, max_request_bytes=2097152,
            max_response_bytes=16777216, max_requests=1024, io_timeout_seconds=180.0,
            upstream_origin=px.DEEPSEEK_ORIGIN, profile=px.DEEPSEEK_PROFILE,
            upstream_model="deepseek-flash", declared_request_byte_budget=2097152,
            pace_seconds=0)
        self.assertEqual(cli.bind_request_budget(matching, _candidate(CANDIDATE_1024)), 1024)
        stale = px.CloudConfig(
            model="deepseek-flash", max_output_tokens=4096, max_request_bytes=2097152,
            max_response_bytes=16777216, max_requests=256, io_timeout_seconds=180.0,
            upstream_origin=px.DEEPSEEK_ORIGIN, profile=px.DEEPSEEK_PROFILE,
            upstream_model="deepseek-flash", declared_request_byte_budget=2097152,
            pace_seconds=0)
        with self.assertRaises(ValueError) as caught:
            cli.bind_request_budget(stale, _candidate(CANDIDATE_1024))
        self.assertIn("share ONE budget", str(caught.exception))


class _RecordingOpener:
    """Answers as the provider would: a catalog for GET, a completion for POST.

    The proxy validates the responder's identity, so a single canned body would latch the
    proxy as blocked and hide the budget boundary this suite is about.
    """

    def __init__(self):
        self.sent = []

    def open(self, request, timeout):
        import io
        body = json.loads((request.data or b"{}").decode("utf-8"))
        self.sent.append({"url": request.full_url, "method": request.get_method(),
                          "body": body})
        if request.get_method() == "GET":
            payload = {"object": "list", "data": [{"id": "deepseek-flash"}]}
        else:
            payload = {
                "id": "chatcmpl-budget", "object": "chat.completion", "created": 1,
                "model": "deepseek-flash",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        blob = json.dumps(payload).encode("utf-8")

        class _Response(io.BytesIO):
            def __init__(self):
                super().__init__(blob)
                self.code, self.headers = 200, {}

        return _Response()


class TheAuditBudgetTests(unittest.TestCase):
    """The public audit entry, over the real 0.4 chain, for the 1024 candidate."""

    @classmethod
    def setUpClass(cls):
        cls.audit_entry = _load("test_ae_deepseek_re_audit_for_budget",
                                ROOT / "tests/test_ae_deepseek_re_audit.py")
        cls.audit_entry.DeepSeekAuditEntryTests.setUpClass()
        # Point the fixture at the 1024 candidate: the chain itself is unchanged.
        cls.audit_entry.CANDIDATE = CANDIDATE_1024
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            raise unittest.SkipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))

    def _case(self):
        case = self.audit_entry.DeepSeekAuditEntryTests(
            "test_a_complete_deepseek_capture_reaches_the_final_verdict")
        case.setUp()
        self.addCleanup(case.tmp and (lambda: None))
        return case

    def _codes(self, report):
        return [str(item.get("code")) for item in (report.get("invalid_reasons") or [])] + \
               [str(item) for item in (report.get("transport") or {}).get("issues") or []] + \
               [str(item) for item in (report.get("failures") or [])]

    def test_a_legitimate_1024_capture_is_accepted(self):
        case = self._case()
        fixture, config_path = case._build()
        report = case._audit(fixture, config_path)
        self.assertEqual(report.get("status"), "VALID", report.get("invalid_reasons"))
        budget = report.get("request_budget") or {}
        self.assertEqual(budget.get("declared"), 1024)
        self.assertIs(budget.get("budget_unchanged"), False)
        self.assertEqual(budget.get("proxy_config_max_requests"), 1024)
        self.assertLessEqual(budget.get("client_requests_in_capture"), 1024)
        self.assertFalse(budget.get("stopped_by_budget"))
        self.assertIn("request_budget", report.get("checks_executed") or [])
        self.assertEqual(fixture["result"]["status"],
                         "RE_MULTITURN_COMPLETED_AUDIT_PENDING")

    def _restamp_budget(self, fixture, config_path, budget):
        """Rewrite the WHOLE declaration to another budget, digests included.

        A run whose plan, result and `--config` bytes all say one number while the proxy
        that counted says another is exactly the inconsistency the budget gate exists for;
        re-stamping the provenance digests keeps every OTHER gate satisfied so the refusal
        can only come from the budget itself.
        """
        from ae_inputs import canonical_sha256
        import hashlib
        record = json.loads(Path(config_path).read_text(encoding="utf-8"))
        record["max_requests"] = budget
        Path(config_path).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        file_sha = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
        canonical = canonical_sha256(record)
        for name in ("plan.json", "result.json"):
            path = fixture["run_dir"] / name
            document = json.loads(path.read_text(encoding="utf-8"))
            document["config"] = dict(document["config"], max_requests=budget)
            provenance = document.get("provenance") or {}
            provenance["config_file_sha256"] = file_sha
            provenance["config_canonical_sha256"] = canonical
            document["provenance"] = provenance
            if name == "plan.json":
                document["scope"]["pair_request_budget"] = dict(
                    document["scope"]["pair_request_budget"], max_requests=budget,
                    budget_unchanged=budget == 256)
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def test_a_plan_that_kept_256_is_refused(self):
        case = self._case()
        fixture, config_path = case._build()
        # The candidate, the plan and the result all say 256; the PROXY counted 1024.
        self._restamp_budget(fixture, config_path, 256)
        report = case._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("proxy_and_plan_request_budgets_differ", self._codes(report))

    def test_a_proxy_that_counted_another_budget_is_refused(self):
        case = self._case()
        fixture, config_path = case._build()
        journal = fixture["journal"]
        rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        for row in rows:
            if row.get("kind") == "cloud_open":
                # The other REVIEWED budget: it passes the schema-level check and can only
                # be caught by comparing it with the plan's own number.
                row["config"]["max_requests"] = 256
        journal.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                           encoding="utf-8")
        report = case._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("proxy_and_plan_request_budgets_differ", self._codes(report))

    def test_a_budget_below_the_capture_is_refused(self):
        case = self._case()
        fixture, config_path = case._build()
        plan_path = fixture["run_dir"] / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["scope"]["pair_request_budget"]["max_requests"] = 64   # below the capture
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = case._audit(fixture, config_path)
        self.assertFalse(report.get("input_audit_passed"))
        self.assertIn("plan_budget_record_differs", self._codes(report))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
