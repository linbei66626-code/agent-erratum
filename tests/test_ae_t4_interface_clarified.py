"""`t4-interface-clarified-v1`: the opt-in two-factor clarification, offline.

What is proven here: the DEFAULT path is byte-for-byte unchanged, the variant differs only
by the two declared interventions, the time comes from the SAME clock the native tools
validate with (no wall clock, no hardcoded value), the switch reaches the REAL outgoing
messages/tools rather than only the plan, two consecutive constructions do not contaminate
each other, and the native tools keep their original behaviour (an omitted `attributes`
still yields an empty spec and still fails the strict diagnosis; an explicitly correct spec
still passes).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

VARIANT = "t4-interface-clarified-v1"
CLOUD_MODEL = "declared-model-v1"
CLOUD_BASE = "https://api.example-endpoint.test"
FAKE_KEY = "sk-synthetic-variant-key-DO-NOT-PERSIST"
TARGET_PRODUCT_ID = "S17791041622763865_P00011"
CORRECT_SPEC = "规格: 7分糖"
WORK_ADDRESS = "甘肃省兰州市安宁区安宁西路地五大道88号"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _variants():
    return _load("ae_t4_case_variants_under_test", ROOT / "t4_case_variants.py")


def _local():
    return _load("ae_local_probe_for_variant_tests",
                 ROOT / "scripts/ae_01_local_t4_probe.py")


def _cloud():
    return _load("ae_01_cloud_t4_probe_for_variant_tests",
                 ROOT / "scripts/ae_01_cloud_t4_probe.py")


def _vita_source():
    root = Path(os.environ.get("AE_VERIFY_ROOT", ROOT / ".ae-verify-src"))
    return Path(os.environ.get("AE_VITA_SOURCE", root / "source"))


def _reply(payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    class _Response(io.BytesIO):
        def __init__(self):
            super().__init__(body)
            self.code, self.headers = status, {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False
    return _Response()


def _chat_reply(tool_calls=None, content=None, finish_reason="tool_calls"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
            for name, arguments, call_id in tool_calls]
    return {"id": "gen", "object": "chat.completion", "model": CLOUD_MODEL,
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


def _config(directory, **overrides):
    document = {
        "identity": {"probe": "required", "evidence": "variant tests: declared locally"},
        "api": {"base_url": CLOUD_BASE, "chat_path": "/chat/completions",
                "models_path": "/models", "tokenize_path": None},
        "wire_model": CLOUD_MODEL,
        "mode": {"name": "declared-non-thinking",
                 "payload": {"thinking": {"type": "disabled"}},
                 "source": "variant tests: declared locally"},
        "capacity": {"token_counting_available": False, "max_request_bytes": 400000},
    }
    document.update(overrides)
    path = Path(directory) / "identity.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


class _Provider:
    """A scripted endpoint that can OMIT `attributes` or send a chosen spec."""

    def __init__(self, *, spec=None, requested_spec=None, create_override=None):
        self.models = [CLOUD_MODEL]
        self.chat_calls = []
        self.spec = spec
        self.requested_spec = requested_spec
        self.create_override = create_override

    def open(self, request, timeout):
        path = request.full_url[len(CLOUD_BASE):]
        if request.data is None:
            if path == "/models":
                return _reply({"object": "list",
                               "data": [{"id": name} for name in self.models]})
            return _reply({"error": {"message": "no route"}}, 404)
        body = json.loads(request.data.decode("utf-8"))
        self.chat_calls.append(body)
        return _reply(self._reply_for(body))

    # ---------------------------------------------------------------- agent script

    def _tool_turns(self, body):
        return [m for m in body.get("messages") or [] if m.get("role") == "tool"]

    def _searched(self, body):
        return any("product_id=" in (m.get("content") or "") for m in self._tool_turns(body))

    def _created(self, body):
        return any("Order(order_id:" in (m.get("content") or "") for m in self._tool_turns(body))

    def _order_id(self, body):
        import re
        pattern = re.compile(r"order_id[:=]['\"]?([^,'\")\s]+)")
        for message in reversed(self._tool_turns(body)):
            match = pattern.search(message.get("content") or "")
            if match:
                return match.group(1)
        return None

    def _product_id(self, body):
        import re
        for message in reversed(self._tool_turns(body)):
            match = re.search(r"product_id=(S\d+_P\d+)", message.get("content") or "")
            if match:
                return match.group(1)
        return TARGET_PRODUCT_ID

    def _store_id(self, body):
        import re
        for message in reversed(self._tool_turns(body)):
            match = re.search(r"store_id=(S\d+_S\d+)", message.get("content") or "")
            if match:
                return match.group(1)
        return "S17791041622763865_S00006"

    def _reply_for(self, body):
        if "tools" not in body:  # user simulator role
            return _chat_reply(content="没有了，谢谢###STOP###", finish_reason="stop")
        if not self._searched(body):
            return _chat_reply([("delivery_product_search_recommand",
                                 {"keywords": ["恋与深空", "奶茶"]}, "c1")])
        if self._created(body):
            order_id = self._order_id(body)
            return _chat_reply([("pay_delivery_order", {"order_id": order_id}, "c3")])
        create = {"user_id": "U000828", "store_id": self._store_id(body),
                  "product_ids": [self._product_id(body)], "product_cnts": [1],
                  "address": WORK_ADDRESS, "dispatch_time": "2024-06-23 18:00:00"}
        if self.create_override is not None:
            create = self.create_override(create)
        # spec=None models a model that omits the parameter entirely
        if self.spec is not None:
            create["attributes"] = [self.spec]
        if self.requested_spec is not None:
            create["attributes"] = [self.requested_spec]
        return _chat_reply([("create_delivery_order", create, "c2")])


class VariantConstructionTests(unittest.TestCase):
    """The variant changes exactly the two declared things, and the default does not."""

    def setUp(self):
        self.variants = _variants()
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

    def _base(self):
        return self.local.agent_system_message(self.native, self.public)

    def test_the_default_message_and_tools_are_untouched(self):
        base = self._base()
        tools = self.native["tools"]
        snapshot = json.dumps(tools, ensure_ascii=False, sort_keys=True)
        # the default message carries only the DATE, exactly as before
        self.assertNotIn("14:30:00", base)
        self.assertIn("2024-06-23", base)
        self.assertNotIn(self.variants.ATTRIBUTES_CLARIFICATION,
                         json.dumps(tools, ensure_ascii=False))
        built = self.variants.build_variant(self.native["environment"], base, tools)
        # ... and building the variant did not mutate the defaults
        self.assertEqual(json.dumps(tools, ensure_ascii=False, sort_keys=True), snapshot,
                         "the default tool schemas were mutated by the variant")
        self.assertEqual(self._base(), base)
        self.assertEqual(len(built["tools"]), 19)

    def test_the_variant_delta_is_only_time_and_the_attributes_note(self):
        base = self._base()
        tools = self.native["tools"]
        built = self.variants.build_variant(self.native["environment"], base, tools)
        system = built["system_message"]
        # exactly one addition, prefixed by the original text unchanged
        self.assertTrue(system.startswith(base))
        self.assertEqual(system[len(base):].count("# 当前时间"), 1)
        self.assertIn(self.variants.ATTRIBUTES_CLARIFICATION,
                      json.dumps(built["tools"], ensure_ascii=False))
        # everything else about the tools is identical
        before = json.loads(json.dumps(tools, ensure_ascii=False))
        after = json.loads(json.dumps(built["tools"], ensure_ascii=False))
        for original, changed in zip(before, after):
            self.assertEqual(original["function"]["name"], changed["function"]["name"])
            if original["function"]["name"] != "create_delivery_order":
                self.assertEqual(original, changed, "an unrelated tool schema changed")
        create_before = next(t for t in before
                             if t["function"]["name"] == "create_delivery_order")
        create_after = next(t for t in after
                            if t["function"]["name"] == "create_delivery_order")
        self.assertEqual(create_before["function"]["parameters"]["required"],
                         create_after["function"]["parameters"]["required"],
                         "attributes must NOT become required")
        self.assertIn("attributes",
                      create_after["function"]["parameters"]["properties"])
        self.assertNotIn("attributes",
                         create_after["function"]["parameters"]["required"])

    def test_the_clarification_names_no_oracle_value(self):
        built = self.variants.build_variant(self.native["environment"], self._base(),
                                            self.native["tools"])
        blob = json.dumps(built["tools"], ensure_ascii=False)
        self.assertNotIn(TARGET_PRODUCT_ID, blob)
        self.assertNotIn("深空告白", blob)
        self.assertNotIn("益禾堂", blob)
        self.assertNotIn(CORRECT_SPEC, blob)
        self.assertNotIn("7分糖", blob)

    def test_two_consecutive_constructions_do_not_contaminate(self):
        built_one = self.variants.build_variant(self.native["environment"], self._base(),
                                                self.native["tools"])
        built_two = self.variants.build_variant(self.native["environment"], self._base(),
                                                self.native["tools"])
        self.assertEqual(built_one["tools"], built_two["tools"],
                         "the variant is not idempotent across constructions")
        blob = json.dumps(built_two["tools"], ensure_ascii=False)
        self.assertEqual(blob.count(self.variants.ATTRIBUTES_CLARIFICATION), 1,
                         "the note was appended twice")

    def test_the_variant_record_is_honest_about_what_it_is(self):
        built = self.variants.build_variant(self.native["environment"], self._base(),
                                            self.native["tools"])
        record = built["record"]
        self.assertEqual(record["case_variant"], VARIANT)
        self.assertTrue(record["post_hoc_exploratory_control"])
        self.assertFalse(record["is_the_original_benchmark"])
        self.assertFalse(record["is_r_e"])
        self.assertTrue(record["two_factors_are_changed_together"])
        self.assertFalse(record["can_attribute_to_one_factor_alone"])
        self.assertIn("same_as_original_input", record["superseded_markers_not_inherited"])


class VariantClockTests(unittest.TestCase):
    """The time comes from the native clock, so it matches what the tools enforce."""

    def setUp(self):
        self.variants = _variants()
        self.local = _local()
        self.vita = _vita_source()
        if not (self.vita / "src/vita").is_dir():
            self.skipTest("the fixed Vita source is absent")
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            self.skipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))

    def _environment_at(self, when: str):
        """A REAL native environment whose database time is `when`."""
        import copy
        public = self.local.prepare_public(self.vita)
        private = copy.deepcopy(public["private"])
        private["environment"]["time"] = when
        import ae_inputs  # noqa: F401 - the environment builder needs the package importable
        sys.path.insert(0, str(self.vita / "src"))
        from vita.domains.delivery.environment import get_environment
        return public, get_environment(db=copy.deepcopy(private["environment"]))

    def test_the_offered_time_is_the_time_the_tools_enforce(self):
        """The offered time IS the tool's clock: after it passes, before it is refused."""
        import ae_task_run
        native_module = self.local._load_native()
        for when in ("2024-06-23 14:30:00", "2025-01-02 09:15:00"):
            with self.subTest(when=when):
                public, environment = self._environment_at(when)
                fact = self.variants.time_fact(environment)
                self.assertEqual(fact["environment_now"], when)
                self.assertFalse(fact["host_wall_clock_used"])
                self.assertFalse(fact["hardcoded_value_used"])
                self.assertFalse(fact["read_from_private_scoring"])
                database = public["private"]["environment"]
                store_id = next(iter(database["stores"]))
                product_id = database["stores"][store_id]["products"][0]["product_id"]
                address = database["location"][0]["address"]
                bindings = ae_task_run.environment_bindings(environment)

                def create(dispatch_time):
                    arguments = {"user_id": "U000828", "store_id": store_id,
                                 "product_ids": [product_id], "product_cnts": [1],
                                 "address": address, "dispatch_time": dispatch_time}
                    return native_module._execute(bindings["create_delivery_order"],
                                                  "create_delivery_order", arguments)

                after = create("2099-01-01 00:00:00")
                self.assertIn("Order(order_id:", after["text"], after)
                before = create("2000-01-01 00:00:00")
                self.assertIn("must be in the future", before["text"],
                              "the real tool must still enforce its own clock")

    def test_an_unreadable_clock_is_refused_not_guessed(self):
        class _NoClock:
            tools = object()
        with self.assertRaises(self.variants.VariantRejected) as caught:
            self.variants.time_fact(_NoClock())
        self.assertIn("nothing is guessed", str(caught.exception))

        class _EmptyClock:
            class tools:  # noqa: N801 - a stand-in
                @staticmethod
                def get_now(_format):
                    return "   "
        with self.assertRaises(self.variants.VariantRejected):
            self.variants.time_fact(_EmptyClock())


class VariantEndToEndTests(unittest.TestCase):
    """The switch reaches the REAL outgoing messages/tools, and the tools keep behaving."""

    def setUp(self):
        self.cloud = _cloud()
        self.local = _local()
        self.variants = _variants()
        self.vita = _vita_source()
        if not (self.vita / "src/vita").is_dir():
            self.skipTest("the fixed Vita source is absent")
        dataset = Path(os.environ.get("AE_VITA_DATASET",
                                      ROOT / ".ae-verify-src/tasks-full-a4553e1.json"))
        if not dataset.is_file():
            self.skipTest("the dataset is absent")
        os.environ.setdefault("AE_VITA_DATASET", str(dataset))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = self.cloud.load_config(_config(self.tmp.name))

    def _run(self, provider, *, case_variant, name):
        out = Path(self.tmp.name) / name
        transport = self.cloud.CloudTransport(self.config, api_key=FAKE_KEY, opener=provider,
                                              max_inference_posts=16)
        result = self.cloud.run(config=self.config, vita_source=self.vita, key=FAKE_KEY,
                                output_dir=out, transport=transport,
                                case_variant=case_variant)
        return result, out

    def test_the_default_and_the_variant_differ_only_by_the_two_interventions(self):
        default_provider = _Provider(spec=CORRECT_SPEC)
        default, _out = self._run(default_provider, case_variant=None, name="default")
        variant_provider = _Provider(spec=CORRECT_SPEC)
        variant, out = self._run(variant_provider, case_variant=VARIANT, name="variant")
        first_default = default_provider.chat_calls[0]
        first_variant = variant_provider.chat_calls[0]
        # identity of everything else
        self.assertEqual(first_default["model"], first_variant["model"])
        self.assertEqual(first_default["temperature"], first_variant["temperature"])
        self.assertEqual(len(first_default["tools"]), len(first_variant["tools"]))
        self.assertEqual(first_default["messages"][1], first_variant["messages"][1],
                         "the task instruction must not change")
        # the two interventions are the ONLY differences
        self.assertNotIn("14:30:00", first_default["messages"][0]["content"])
        self.assertIn("14:30:00", first_variant["messages"][0]["content"],
                      "the variant must carry the full environment time")
        default_blob = json.dumps(first_default["tools"], ensure_ascii=False)
        variant_blob = json.dumps(first_variant["tools"], ensure_ascii=False)
        self.assertNotIn(self.variants.ATTRIBUTES_CLARIFICATION, default_blob)
        self.assertIn(self.variants.ATTRIBUTES_CLARIFICATION, variant_blob)
        # the variant run records what it is, and does not inherit misleading labels
        self.assertEqual(variant["case_variant"], VARIANT)
        self.assertTrue(variant["post_hoc_exploratory_control"])
        self.assertFalse(variant["can_attribute_to_one_factor_alone"])
        self.assertTrue((out / "case-variant.json").is_file())
        self.assertTrue((out / "first-request.json").is_file())
        saved = json.loads((out / "case-variant.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["interventions"]["complete_environment_time"]
                         ["environment_now"], "2024-06-23 14:30:00")
        # the default path stays the original
        self.assertIsNone(default["case_variant"])
        # the budget/transport path is unchanged
        self.assertTrue(variant["total_model_posts_within_budget"])
        self.assertLessEqual(variant["total_model_posts"], 16)

    def test_the_default_path_does_not_write_a_variant_record(self):
        provider = _Provider(spec=CORRECT_SPEC)
        _result, out = self._run(provider, case_variant=None, name="plain")
        self.assertFalse((out / "case-variant.json").exists())
        self.assertFalse((out / "first-request.json").exists())

    def test_the_tools_keep_their_original_behaviour(self):
        # an OMITTED attributes still yields an EMPTY order spec and still fails strictly
        provider = _Provider(spec=None)
        result, _out = self._run(provider, case_variant=VARIANT, name="omitted")
        self.assertNotEqual(result["status"], self.local.STATUS_PASSED)
        self.assertFalse(result["task_success"])
        self.assertFalse(result["order_diagnosis"]["oracle_passed"])
        order = list(result["final_native_snapshot"]["orders"].values())[0]
        self.assertEqual(order["products"][0]["attributes"], "",
                         "the native tool must still write an empty spec when omitted")
        # an explicitly correct spec still passes
        provider = _Provider(spec=CORRECT_SPEC)
        result, _out = self._run(provider, case_variant=VARIANT, name="explicit")
        self.assertEqual(result["status"], self.local.STATUS_PASSED)
        self.assertTrue(result["oracle_passed"])
        order = list(result["final_native_snapshot"]["orders"].values())[0]
        self.assertEqual(order["products"][0]["attributes"], CORRECT_SPEC)

    def test_the_variant_is_opt_in_and_an_unknown_name_is_refused(self):
        provider = _Provider(spec=CORRECT_SPEC)
        with self.assertRaises(ValueError) as caught:
            self._run(provider, case_variant="some-other-variant", name="unknown")
        self.assertIn("unknown case variant", str(caught.exception))
        self.assertEqual(provider.chat_calls, [], "an unknown variant must not call the model")

    def test_the_variant_does_not_by_itself_guarantee_a_pass(self):
        """The clarification does not fill the parameter in: a model that omits it still fails."""
        omitting = _Provider(spec=None)
        result, _out = self._run(omitting, case_variant=VARIANT, name="no-autofill")
        self.assertFalse(result["oracle_passed"])
        self.assertFalse(result["task_success"])
        create = [t for t in result["tool_returns"]
                  if t["name"] == "create_delivery_order"]
        self.assertTrue(create, "the create really ran")
        self.assertIn("attributes=", create[0]["raw_text"])


class VariantCliTests(unittest.TestCase):
    def setUp(self):
        self.cloud = _cloud()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_plan_records_the_variant_and_needs_no_key(self):
        config = _config(self.tmp.name)
        out = Path(self.tmp.name) / "plan"
        code = self.cloud.main(["--stage", "plan", "--config", str(config),
                                "--vita-source", str(_vita_source()),
                                "--output-dir", str(out),
                                "--case-variant", VARIANT,
                                "--key-file", str(Path(self.tmp.name) / "absent.key")])
        self.assertEqual(code, 0)
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["case_variant"], VARIANT)
        self.assertFalse(plan["scope"]["default_path"])
        self.assertFalse(plan["scope"]["one_original_t4"])
        self.assertFalse(plan["network_called"])

    def test_plan_without_the_flag_stays_the_default(self):
        config = _config(self.tmp.name)
        out = Path(self.tmp.name) / "plan-default"
        code = self.cloud.main(["--stage", "plan", "--config", str(config),
                                "--vita-source", str(_vita_source()),
                                "--output-dir", str(out)])
        self.assertEqual(code, 0)
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        self.assertIsNone(plan["case_variant"])
        self.assertTrue(plan["scope"]["default_path"])


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
