"""DeepSeek multi-turn R/E integration: the targeted acceptance.

Only what this round added is tested here, plus the directly affected behaviour of the
existing driver constants. The real Letta agents, the native tools, the user simulator and
the scorer are NOT mocked: what is exercised is the added layer's declarations, the two-arm
clarification against REAL native environments of different domains, the byte gate, and the
recorded service-chain blocker.
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

CANDIDATE = ROOT / "configs/ae-01__re-multiturn__deepseek-flash.serial-candidate.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ds():
    return _load("ae_deepseek_re_transport_under_test",
                 ROOT / "ae_deepseek_re_transport.py")


def _local():
    return _load("ae_local_probe_for_ds_tests", ROOT / "scripts/ae_01_local_t4_probe.py")


def _vita_source():
    root = Path(os.environ.get("AE_VERIFY_ROOT", ROOT / ".ae-verify-src"))
    return Path(os.environ.get("AE_VITA_SOURCE", root / "source"))


def _candidate() -> dict:
    return json.loads(CANDIDATE.read_text(encoding="utf-8"))


class TransportDeclarationTests(unittest.TestCase):
    """A Qwen-labelled run can never pass as a DeepSeek run."""

    def setUp(self):
        self.ds = _ds()

    def test_the_profile_is_the_declared_official_identity(self):
        profile = self.ds.deepseek_profile()
        self.assertEqual(profile["origin"], "https://api.deepseek.com")
        self.assertEqual(profile["wire_model"], "deepseek-flash")
        self.assertEqual(profile["chat_path"], "/chat/completions")
        self.assertEqual(profile["mode_field"], "thinking")
        self.assertEqual(profile["mode_value"], {"type": "disabled"})
        self.assertFalse(profile["local_vllm_field_reused"])
        # the wire fields the audit compares, with NO local mode field anywhere
        fields = self.ds.wire_request_fields()
        self.assertEqual(fields["model"], "deepseek-flash")
        self.assertEqual(fields["thinking"], {"type": "disabled"})
        self.assertNotIn("chat_template_kwargs", json.dumps(fields, ensure_ascii=False))

    def test_the_new_provider_does_not_disturb_the_sealed_entries(self):
        """r2 registers the profile, so the check is that nothing SEALED moved."""
        import ae_cloud_proxy
        self.assertIn(self.ds.DEEPSEEK_PROFILE, ae_cloud_proxy.PROFILES)
        self.assertEqual(ae_cloud_proxy.MODEL, "Qwen/Qwen3-30B-A3B-Instruct-2507",
                         "the old default model constant changed")
        for name in ("siliconflow-letta-text-transport-v2-prototype",
                     "openrouter-nebius-qwen3-30b-instruct-v1"):
            sealed = ae_cloud_proxy.PROFILES[name]
            self.assertEqual(sealed.internal_model, ae_cloud_proxy.MODEL,
                             f"{name} no longer targets the sealed model")
        sealed_transport = ae_cloud_proxy.PROFILES["siliconflow-letta-text-transport-v2-prototype"]
        self.assertEqual(sealed_transport.origin, "https://api.siliconflow.cn")
        deepseek = ae_cloud_proxy.PROFILES[self.ds.DEEPSEEK_PROFILE]
        self.assertEqual(deepseek.origin, "https://api.deepseek.com")
        self.assertEqual(deepseek.extra_request_fields,
                         ("thinking", "user", "parallel_tool_calls"),
                         "the provider must send its own mode field, not the local one, "
                         "and the pinned service's pass-through shape must be declared")
        # The mode field is REQUIRED with its value: a request that omits it is refused
        # instead of silently running the endpoint's default mode.
        self.assertEqual(deepseek.required_request_fields,
                         {"thinking": {"type": "disabled"}})
        self.assertEqual(self.ds.declared_request_shape()["required_fields"],
                         {"model": "deepseek-flash", "thinking": {"type": "disabled"}})

    def test_the_candidate_config_declares_this_provider_correctly(self):
        declared = self.ds.validate_deepseek_declaration(_candidate())
        self.assertTrue(declared["declared_correctly"])
        self.assertEqual(declared["wire_model"], "deepseek-flash")
        self.assertFalse(declared["weights_version_claimed"])
        self.assertEqual(declared["wire_request_fields"]["thinking"], {"type": "disabled"})

    def test_a_qwen_labelled_config_is_refused(self):
        sealed = json.loads((ROOT / "configs/ae-01__re-multiturn__siliconflow"
                                    ".capacity-250k-measured-candidate.json"
                             ).read_text(encoding="utf-8"))
        with self.assertRaises(self.ds.DeclarationRejected) as caught:
            self.ds.validate_deepseek_declaration(sealed)
        message = str(caught.exception)
        self.assertIn("transport_profile", message)
        self.assertIn("Qwen", message)

    def test_a_wrong_wire_model_or_origin_is_refused(self):
        for broken in ({"transport_profile": "siliconflow-letta-text-transport-v2-prototype"},
                       {"upstream_model": "deepseek-chat"},
                       {"expected_model": "Qwen/Qwen3-30B-A3B-Instruct-2507"}):
            with self.subTest(broken=broken):
                config = _candidate()
                config.update(broken)
                with self.assertRaises(self.ds.DeclarationRejected):
                    self.ds.validate_deepseek_declaration(config)

    def test_an_audit_comparing_a_real_identity_catches_a_qwen_label(self):
        expected = {"model": "deepseek-flash", "origin": "https://api.deepseek.com",
                    "profile": "deepseek-official-re-transport-v1"}
        self.assertTrue(self.ds.audit_identity(expected, dict(expected))["matches"])
        wrong = dict(expected, model="Qwen/Qwen3-30B-A3B-Instruct-2507")
        result = self.ds.audit_identity(expected, wrong)
        self.assertFalse(result["matches"])
        self.assertEqual(result["mismatches"][0]["field"], "model")
        self.assertFalse(result["identity_from_a_label_only"])


class SerialPacingTests(unittest.TestCase):
    """0 s interval, still serial, and nothing else widened."""

    def setUp(self):
        self.ds = _ds()

    def test_the_declared_candidate_is_accepted(self):
        result = self.ds.validate_serial_pacing(_candidate())
        self.assertEqual(result["sleep_seconds"], 0)
        self.assertTrue(result["fixed_sleep_removed"])
        self.assertTrue(result["still_serial"])
        self.assertEqual(result["concurrency"], 1)
        self.assertEqual(result["automatic_retries"], 0)
        self.assertTrue(result["budget_unchanged"])

    def test_a_nonzero_interval_is_refused(self):
        config = _candidate()
        config["pacing"] = dict(config["pacing"], min_interval_seconds=65)
        with self.assertRaises(self.ds.DeclarationRejected) as caught:
            self.ds.validate_serial_pacing(config)
        self.assertIn("min_interval_seconds=0", str(caught.exception))

    def test_a_faster_run_cannot_widen_the_budget_or_timeouts(self):
        for broken, needle in (({"max_requests": 512}, "request budget"),
                               ({"timeout_seconds": 60}, "driver I/O timeout")):
            with self.subTest(broken=broken):
                config = _candidate()
                config.update(broken)
                with self.assertRaises(self.ds.DeclarationRejected) as caught:
                    self.ds.validate_serial_pacing(config)
                self.assertIn(needle, str(caught.exception))
        config = _candidate()
        config["pacing"] = dict(config["pacing"], driver_io_timeout_seconds=30)
        with self.assertRaises(self.ds.DeclarationRejected):
            self.ds.validate_serial_pacing(config)

    def test_the_sealed_pacing_constant_is_untouched(self):
        import ae_cloud_re_multiturn as driver
        self.assertEqual(driver.PACING_CANDIDATE["min_interval_seconds"], 65,
                         "the sealed 65 s candidate was edited")
        self.assertEqual(driver.PAIR_MAX_REQUESTS, 256,
                         "the whole-pair request budget was widened")


class CapacityDeclarationTests(unittest.TestCase):
    """No token claim, no Qwen counter, refused by default."""

    def setUp(self):
        self.ds = _ds()

    def test_the_declared_protocol_is_accepted_only_with_the_explicit_option(self):
        with self.assertRaises(self.ds.DeclarationRejected) as caught:
            self.ds.validate_capacity_declaration(_candidate())
        self.assertIn("explicit", str(caught.exception))
        accepted = self.ds.validate_capacity_declaration(
            _candidate(), option=self.ds.NON_GUARANTEE_OPTION)
        self.assertEqual(accepted["count_basis"], "no_token_count_byte_gate_only")
        self.assertFalse(accepted["capacity_is_a_guarantee"])
        self.assertFalse(accepted["qwen_counter_used"])
        self.assertFalse(accepted["bytes_per_token_estimate_used"])
        self.assertFalse(accepted["response_usage_used_as_a_pre_send_count"])
        self.assertFalse(accepted["verified_capacity_fields_touched"])
        self.assertTrue(accepted["declared_context_window_is_published_not_measured"])
        self.assertEqual(accepted["request_byte_budget"], 2097152,
                         "the old multi-turn byte budget must be kept")
        self.assertEqual(accepted["output_reserves"], {"agent": 2048, "auxiliary": 4096})

    def test_a_qwen_count_basis_or_a_guarantee_claim_is_refused(self):
        for broken, needle in (({"count_basis": ["official_qwen_tokenizer"]},
                                "count_basis"),
                               ({"capacity_is_a_guarantee": True}, "guarantee"),
                               ({"window_is_measured": True}, "measured"),
                               ({"max_request_bytes": None}, "positive integer"),
                               ({"output_reserve_tokens": 4096}, "output_reserve_tokens")):
            with self.subTest(broken=broken):
                config = _candidate()
                config["capacity"] = dict(config["capacity"], **broken)
                with self.assertRaises(self.ds.DeclarationRejected) as caught:
                    self.ds.validate_capacity_declaration(
                        config, option=self.ds.NON_GUARANTEE_OPTION)
                self.assertIn(needle, str(caught.exception))

    def test_the_byte_gate_refuses_before_any_send(self):
        allowed = self.ds.byte_gate_decision(1000, budget=2097152)
        self.assertTrue(allowed["allowed"])
        self.assertTrue(allowed["checked_before_send"])
        self.assertFalse(allowed["is_a_capacity_guarantee"])
        over = self.ds.byte_gate_decision(2097153, budget=2097152)
        self.assertFalse(over["allowed"])
        self.assertEqual(over["reason"], "over_declared_byte_budget")
        unknown = self.ds.byte_gate_decision(None)
        self.assertFalse(unknown["allowed"])

    def test_the_unverified_parameters_are_declared_not_dropped(self):
        config = _candidate()
        config["seed"] = 12345
        result = self.ds.validate_unverified_parameters(config)
        self.assertEqual(result["present_in_config"], ["seed"])
        self.assertFalse(result["silently_removed"])
        self.assertFalse(result["claimed_supported"])
        self.assertEqual(result["status"], "to_be_confirmed_on_site")

    def test_the_service_chain_blocker_is_recorded_not_worked_around(self):
        blocker = self.ds.SERVICE_CHAIN_BLOCKER
        self.assertTrue(blocker["not_done_this_round"])
        self.assertFalse(blocker["mock_pass_claim"])
        self.assertIn("ae_no_compaction.py", blocker["where"])
        self.assertTrue(blocker["minimal_remaining_work"])

        # ... and the blocker is REAL: the deployed gate refuses an uncounted request
        helper = ROOT / ("deployment-assets/letta-no-compaction/files/letta/helpers/"
                         "ae_no_compaction.py")
        source = helper.read_text(encoding="utf-8")
        self.assertIn("no_trustworthy_count_basis", source)
        self.assertIn("if not isinstance(prompt_tokens, int) or prompt_tokens < 0:",
                      source)
        import ae_cloud_re_multiturn as driver
        self.assertEqual(driver.CAPACITY_COUNT_BASES,
                         ("official_qwen_tokenizer", "pinned_tokenizer"),
                         "this round must not add a basis to the sealed driver")
        self.assertNotIn(self.ds.NON_GUARANTEE_COUNT_BASIS, driver.CAPACITY_COUNT_BASES)


class StageClarificationTests(unittest.TestCase):
    """Both arms get the same clarification, from each stage's OWN native environment."""

    def setUp(self):
        self.ds = _ds()
        self.local = _local()
        self.vita = _vita_source()
        if not (self.vita / "src/vita").is_dir():
            self.skipTest("the fixed Vita source is absent")
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            self.skipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))
        self.public = self.local.prepare_public(self.vita)
        self.native = self.local.build_native(self.public, self.vita)

    def test_a_delivery_stage_gets_both_clarifications(self):
        tools = self.native["tools"]
        snapshot = json.dumps(tools, ensure_ascii=False, sort_keys=True)
        base = self.local.agent_system_message(self.native, self.public)
        system, changed, record = self.ds.stage_clarification(
            environment=self.native["environment"], system_message=base, tools=tools,
            domain="delivery")
        self.assertIn("14:30:00", system, "the stage clock must be the native one")
        self.assertIn("2024-06-23", system)
        variants = _load("v", ROOT / "t4_case_variants.py")
        self.assertIn(variants.ATTRIBUTES_CLARIFICATION,
                      json.dumps(changed, ensure_ascii=False))
        self.assertEqual(record["attributes_note"], "applied")
        self.assertTrue(record["schema_touched"])
        self.assertFalse(record["required_changed"])
        self.assertFalse(record["arguments_filled_in"])
        self.assertTrue(record["applies_to_both_arms_identically"])
        self.assertFalse(record["user_persona_changed"])
        self.assertFalse(record["task_changed"])
        self.assertFalse(record["oracle_changed"])
        # the defaults were not mutated
        self.assertEqual(json.dumps(tools, ensure_ascii=False, sort_keys=True), snapshot)
        self.assertEqual(len(changed), len(tools))

    def test_a_stage_without_the_tool_is_not_applicable_and_not_forced(self):
        tools = [tool for tool in self.native["tools"]
                 if (tool.get("function") or {}).get("name") != "create_delivery_order"]
        base = self.local.agent_system_message(self.native, self.public)
        system, returned, record = self.ds.stage_clarification(
            environment=self.native["environment"], system_message=base, tools=tools,
            domain="instore")
        self.assertEqual(record["attributes_note"], "not_applicable")
        self.assertFalse(record["schema_touched"])
        self.assertEqual([t["function"]["name"] for t in returned],
                         [t["function"]["name"] for t in tools],
                         "no delivery schema may be forced onto another domain")
        self.assertIn("14:30:00", system, "the clock clarification still applies")

    def test_two_stages_use_their_own_clocks(self):
        base = self.local.agent_system_message(self.native, self.public)
        import copy
        from vita.domains.delivery.environment import get_environment
        times = []
        for when in ("2024-06-23 14:30:00", "2024-06-24 09:05:00"):
            database = copy.deepcopy(dict(self.public["private"]["environment"]))
            database["time"] = when
            environment = get_environment(db=database)
            _system, _tools, record = self.ds.stage_clarification(
                environment=environment, system_message=base,
                tools=self.native["tools"], domain="delivery")
            times.append(record["clock"]["environment_now"])
        self.assertEqual(times, ["2024-06-23 14:30:00", "2024-06-24 09:05:00"])
        self.assertNotEqual(times[0], times[1], "the clock must not be hardcoded")

    def test_an_unreadable_stage_clock_is_refused(self):
        class _NoClock:
            tools = object()
        with self.assertRaises(self.ds.DeclarationRejected) as caught:
            self.ds.stage_clarification(environment=_NoClock(), system_message="sys",
                                        tools=[], domain="delivery")
        self.assertIn("no readable environment clock", str(caught.exception))

    def test_the_clarification_carries_no_oracle_value(self):
        base = self.local.agent_system_message(self.native, self.public)
        system, changed, _record = self.ds.stage_clarification(
            environment=self.native["environment"], system_message=base,
            tools=self.native["tools"], domain="delivery")
        blob = json.dumps(changed, ensure_ascii=False)
        for forbidden in (self.local.TARGET_PRODUCT_ID, "深空告白", "益禾堂", "7分糖"):
            self.assertNotIn(forbidden, blob)
        self.assertNotIn(self.local.TARGET_PRODUCT_ID, system)
        # ... and no "correct current state snapshot" is injected anywhere
        self.assertNotIn("奶茶偏好7分糖", json.dumps(changed, ensure_ascii=False))


class CandidateConfigTests(unittest.TestCase):
    """The candidate is complete, honest, and refuses to claim verification."""

    def setUp(self):
        self.ds = _ds()
        self.config = _candidate()

    def test_it_carries_every_field_the_existing_driver_requires(self):
        import ae_cloud_re_multiturn as driver
        missing = driver.FIELDS - set(self.config)
        self.assertEqual(missing, set(), f"the candidate lacks {sorted(missing)}")
        for key in driver.MULTICALL_FIELDS | driver.CAPACITY_FIELDS:
            self.assertIn(key, self.config)
        self.assertEqual(self.config["arms"], list(driver.ARMS))
        self.assertEqual([self.config["start_turn"], self.config["end_turn"]],
                         [driver.START_TURN, driver.END_TURN])
        self.assertEqual(self.config["max_requests"], driver.PAIR_MAX_REQUESTS)

    def test_nothing_is_marked_verified(self):
        blob = json.dumps(self.config, ensure_ascii=False)
        self.assertNotIn('"verified"', blob)
        self.assertEqual(self.config["capacity"]["verification"], "unverified")
        self.assertFalse(self.config["capacity"]["capacity_is_a_guarantee"])
        self.assertFalse(self.config["capacity"]["window_is_measured"])
        self.assertIsNone(self.config["capacity"]["service_limit"]["endpoint_measured_tokens"])
        self.assertEqual(self.config["capacity"]["count_basis"],
                         ["no_token_count_byte_gate_only"])
        # the honesty note lives in the delivery README, not in a config field the driver
        # would reject as undeclared
        self.assertNotIn("source_note", self.config)
        readme = (ROOT / "results/ae-deepseek-re-multiturn-integration-r1/README.md")
        if readme.is_file():
            self.assertIn("unverified", readme.read_text(encoding="utf-8"))

    def test_the_old_default_configs_are_untouched(self):
        for name in ("ae-01__re-multiturn__siliconflow.capacity-250k-measured-candidate.json",
                     "ae-01__re-multiturn__openrouter.capacity-250k-candidate.json"):
            config = json.loads((ROOT / "configs" / name).read_text(encoding="utf-8"))
            self.assertNotEqual(config.get("transport_profile"), self.ds.DEEPSEEK_PROFILE)
            self.assertNotEqual(config["model_origin"], self.ds.DEEPSEEK_ORIGIN)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
