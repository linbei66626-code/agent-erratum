"""Target tokenizer assets: identity, template selection and the service copy.

The task this module serves: the target model's own tokenizer/template must be
actually consumed by the shared counting entry, and the sealed requests must be
re-counted against it offline. These tests cover the parts that can be checked
without a provider:

* the target assets are verified by FILE identity (bytes + SHA-256 + template
  digest), and a missing, renamed or altered file is refused - never silently
  replaced by the family cache;
* the counting entry renders with the ASSET'S OWN template: the pinned renderer is
  selected BY THE TEMPLATE TEXT, and the branches the target template removed really
  do change the output (so an asset swap cannot be a no-op);
* the counting cache keys on the tool SCHEMAS, not on how many tools there are;
* the service-side copy of the counting entry is the same code and the same count.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The offline template runtime Codex staged for review (Jinja2 3.1.6 + MarkupSafe
#: 3.0.3). It is NOT installed into the project environment: these tests put the
#: reviewed directory on `sys.path` for their own process only, and say in their
#: output which path ran, so the Jinja branch is exercised instead of skipped.
TEMPLATE_RUNTIME_DIR = Path(os.environ.get(
    "AE_TEMPLATE_RUNTIME_PATH", "/tmp/ae-tokenizer-jinja-review-20260914"))


def _ensure_template_runtime():
    """Make the reviewed template runtime importable, or report that it is absent."""
    import importlib.util
    if importlib.util.find_spec("jinja2") is not None:
        return {"available": True, "source": "already importable"}
    if TEMPLATE_RUNTIME_DIR.is_dir():
        sys.path.insert(0, str(TEMPLATE_RUNTIME_DIR))
        if importlib.util.find_spec("jinja2") is not None:
            return {"available": True, "source": str(TEMPLATE_RUNTIME_DIR)}
    return {"available": False, "source": None}


TEMPLATE_RUNTIME = _ensure_template_runtime()

import ae_multiturn_capacity as cap  # noqa: E402
from ae_cloud_re_multiturn import _official_count_request  # noqa: E402

STAGED_HELPER = (ROOT / "deployment-assets/letta-no-compaction/files/letta/helpers"
                 / "ae_qwen_tokenizer.py")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TargetAssetIdentityTests(unittest.TestCase):
    def setUp(self):
        try:
            self.assets = cap.load_target_tokenizer_assets()
        except (FileNotFoundError, ValueError) as exc:
            self.skipTest(f"the target assets are not available: {exc}")

    def test_the_declaration_is_a_file_identity_and_it_verifies(self):
        declaration = cap.TARGET_TOKENIZER
        self.assertEqual(declaration["model"], "Qwen/Qwen3-30B-A3B-Instruct-2507")
        self.assertEqual(self.assets["revision"], declaration["revision"])
        for name, expected in declaration["files"].items():
            entry = self.assets["files"][name]
            self.assertEqual(entry["sha256"], expected["sha256"], name)
            self.assertEqual(entry["bytes"], expected["bytes"], name)
        self.assertEqual(self.assets["template_sha256"], declaration["template_sha256"])
        config = json.loads(Path(self.assets["tokenizer_config"]).read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(config["chat_template"].encode("utf-8")).hexdigest(),
            declaration["template_sha256"])
        # The target config's own model_max_length is NOT an endpoint capacity.
        self.assertEqual(config["model_max_length"],
                         declaration["model_max_length_is_not_endpoint_capacity"])
        self.assertEqual(cap.TARGET_TOKENIZER["native_max_position_embeddings"], 262144)

    def test_an_altered_or_missing_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / cap.TARGET_TOKENIZER["revision"]
            shutil.copytree(Path(self.assets["directory"]), copy)
            config = copy / "tokenizer_config.json"
            document = json.loads(config.read_text(encoding="utf-8"))
            document["chat_template"] = document["chat_template"].replace(
                "assistant\\n", "assistant\\n", 1)
            document["chat_template"] = document["chat_template"] + "{# altered #}"
            config.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                cap.load_target_tokenizer_assets(directory=copy)
            self.assertIn("tokenizer_config.json", str(raised.exception))
            # A missing file is refused too, and a wrong revision directory name with it.
            (copy / "tokenizer_config.json").unlink()
            with self.assertRaises(FileNotFoundError):
                cap.load_target_tokenizer_assets(directory=copy)
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / "not-the-declared-revision"
            wrong.mkdir()
            with self.assertRaises(ValueError) as raised:
                cap.load_target_tokenizer_assets(directory=wrong)
            self.assertIn("not the declared revision", str(raised.exception))

    def test_the_family_cache_cannot_stand_in_for_the_target(self):
        """`target` means the target's files; the family cache is a labelled other."""
        family = cap.load_official_tokenizer_assets()
        self.assertNotEqual(Path(family["tokenizer_json"]),
                            Path(self.assets["tokenizer_json"]))
        self.assertFalse(cap.resolve_tokenizer_assets("exploration")["asset_is_target"])
        self.assertTrue(cap.resolve_tokenizer_assets("target")["asset_is_target"])
        with self.assertRaises(ValueError):
            cap.resolve_tokenizer_assets("whatever")
        # An empty directory at the declared revision is a refusal, not a fallback to
        # whatever else happens to exist on this host.
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / cap.TARGET_TOKENIZER["revision"]
            empty.mkdir()
            with self.assertRaises(FileNotFoundError) as raised:
                cap.load_target_tokenizer_assets(directory=empty)
            self.assertIn("is absent", str(raised.exception))


class TemplateSelectionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.target = cap.load_target_tokenizer_assets()
            self.family = cap.load_official_tokenizer_assets()
        except (FileNotFoundError, ValueError) as exc:
            self.skipTest(f"the tokenizer assets are not available: {exc}")
        self.target_tokenizer = cap.QwenBpeTokenizer(self.target["tokenizer_json"],
                                                     self.target["tokenizer_config"])
        self.family_tokenizer = cap.QwenBpeTokenizer(self.family["tokenizer_json"],
                                                     self.family["tokenizer_config"])

    def test_the_count_names_the_template_and_the_renderer_it_used(self):
        counted = cap.count_request_prompt(
            self.target_tokenizer, [{"role": "user", "content": "你好"}], [])
        self.assertEqual(counted["template_variant"], "qwen3_instruct_no_thinking")
        self.assertEqual(counted["template_sha256"],
                         cap.TARGET_TOKENIZER["template_sha256"])
        self.assertIn(counted["renderer"],
                      ("jinja2_chat_template_verified_equal",
                       "pinned_renderer_from_template"))
        if counted["renderer"] == "pinned_renderer_from_template":
            # No template runtime here: the count says so instead of hiding it.
            self.assertIn("jinja2", counted["renderer_basis"])
            self.assertIn("jinja2", str(cap.template_runtime()["missing"]))
            self.assertFalse(counted["paths_compared"])
        else:
            # Both paths ran and were compared for THIS request.
            self.assertTrue(counted["paths_compared"])
        family = cap.count_request_prompt(
            self.family_tokenizer, [{"role": "user", "content": "你好"}], [])
        self.assertEqual(family["template_variant"], "qwen3_thinking")
        self.assertNotEqual(family["template_sha256"], counted["template_sha256"])

    def test_the_removed_branches_really_change_the_output(self):
        """An asset swap must not be a no-op: the target template drops thinking."""
        cases = {
            "assistant after the last query": [
                {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}],
            "assistant reasoning_content": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a", "reasoning_content": "hidden"}],
        }
        for label, messages in cases.items():
            target = cap.count_request_prompt(self.target_tokenizer, messages, [])
            family = cap.count_request_prompt(self.family_tokenizer, messages, [])
            self.assertLess(target["prompt_tokens"], family["prompt_tokens"], label)
        target = cap.count_request_prompt(
            self.target_tokenizer, [{"role": "user", "content": "q"}],
            add_generation_prompt=True, enable_thinking=False)
        family = cap.count_request_prompt(
            self.family_tokenizer, [{"role": "user", "content": "q"}],
            add_generation_prompt=True, enable_thinking=False)
        self.assertLess(target["prompt_tokens"], family["prompt_tokens"])

    def test_a_declared_target_refuses_instead_of_falling_back(self):
        """The runtime counting basis: target declared -> target files, or refuse."""
        capacity = {"count_basis": ["official_qwen_tokenizer"],
                    "tokenizer": {"target": cap.TARGET_TOKENIZER["model"],
                                  "asset_target": cap.TARGET_TOKENIZER["model"]}}
        counter, source = _official_count_request(capacity)
        self.assertTrue(source.startswith("official_qwen_tokenizer:"))
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}],
                           "tools": []}).encode()
        self.assertIsInstance(counter(body, "agent_or_unknown"), int)
        # A declared EXPLORATION asset is a different, explicitly labelled basis.
        exploration = {"count_basis": ["official_qwen_tokenizer"],
                       "tokenizer": {"target": cap.TARGET_TOKENIZER["model"],
                                     "asset_target": "Qwen/Qwen3-8B"}}
        _, exploration_source = _official_count_request(exploration)
        self.assertTrue(exploration_source.startswith("exploration_qwen_family_asset:"))

    def test_the_cache_keys_on_the_tool_schema_content(self):
        capacity = {"count_basis": ["official_qwen_tokenizer"],
                    "tokenizer": {"target": cap.TARGET_TOKENIZER["model"],
                                  "asset_target": cap.TARGET_TOKENIZER["model"]}}
        counter, _ = _official_count_request(capacity)
        messages = [{"role": "user", "content": "same messages"}]
        short = {"type": "function",
                 "function": {"name": "t", "parameters": {"type": "object",
                                                          "properties": {}}}}
        longer = json.loads(json.dumps(short))
        longer["function"]["description"] = "x" * 400
        first = counter(json.dumps({"messages": messages, "tools": [short]}).encode(), "agent")
        second = counter(json.dumps({"messages": messages, "tools": [longer]}).encode(), "agent")
        repeat = counter(json.dumps({"messages": messages, "tools": [short]}).encode(), "agent")
        self.assertNotEqual(first, second,
                            "the same tool COUNT with a longer schema must be re-counted")
        self.assertEqual(repeat, first, "an identical request may be served from the cache")
        self.assertEqual(second, cap.count_request_prompt(
            self.target_tokenizer, messages, [longer])["prompt_tokens"])


class JinjaPathConsistencyTests(unittest.TestCase):
    """The two supported renderer paths must mean the SAME thing, or refuse."""

    def setUp(self):
        if not TEMPLATE_RUNTIME["available"]:
            self.skipTest("no template runtime is available for the Jinja path")
        try:
            self.assets = cap.load_target_tokenizer_assets()
        except (FileNotFoundError, ValueError) as exc:
            self.skipTest(f"the target assets are not available: {exc}")
        self.tokenizer = cap.QwenBpeTokenizer(self.assets["tokenizer_json"],
                                              self.assets["tokenizer_config"])

    def test_a_missing_optional_tool_calls_field_is_not_an_error(self):
        """The reported 22 failures: a plain assistant turn has no `tool_calls`."""
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "普通回答"}]
        counted = cap.count_request_prompt(self.tokenizer, messages, [])
        self.assertEqual(counted["renderer"], "jinja2_chat_template_verified_equal")
        self.assertTrue(counted["paths_compared"])
        # ... and the same request through messages that DO carry tool calls works too.
        with_calls = messages[:-1] + [
            {"role": "assistant", "content": "调用",
             "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}]},
            {"role": "tool", "content": "结果"}]
        counted_calls = cap.count_request_prompt(self.tokenizer, with_calls, [])
        self.assertTrue(counted_calls["paths_compared"])

    def test_chinese_html_and_key_order_render_identically_in_both_paths(self):
        """`tojson` must not ASCII-escape, HTML-escape or reorder the tool schema."""
        tools = [{"type": "function",
                  "function": {"name": "支付订单",
                               "description": "含 <标签> & '引号' 的说明",
                               "parameters": {"type": "object", "required": ["金额"],
                                              "properties": {"金额": {"type": "number"},
                                                             "备注": {"type": "string"}}}}}]
        messages = [{"role": "system", "content": "系统提示：中文 <b>&</b>"},
                    {"role": "user", "content": "请支付 ¥12.5"}]
        pinned = cap.render_qwen_chat(messages, tools, template=self.tokenizer.chat_template)
        executed = cap.render_with_template_runtime(
            self.tokenizer.chat_template, messages, tools)
        self.assertEqual(executed, pinned)
        self.assertIn("支付订单", executed)
        self.assertIn("<b>&</b>", executed)
        self.assertNotIn("\\u652f", executed)     # no ASCII escaping of Chinese
        self.assertNotIn("\\u003c", executed)     # no HTML escaping of '<'
        policy = cap.TOJSON_POLICY
        self.assertEqual((policy["ensure_ascii"], policy["sort_keys"]), (False, False))
        counted = cap.count_request_prompt(self.tokenizer, messages, tools)
        self.assertEqual(counted["tojson_policy"], policy)

    def test_the_entry_refuses_when_the_two_paths_disagree(self):
        """A template that means something else must not be silently executed."""
        class _OtherTemplate:
            chat_template = "{%- for message in messages %}{{ message.role }}:{{ message.content }};{%- endfor %}"
        with self.assertRaises(RuntimeError) as raised:
            cap.count_request_prompt(_OtherTemplate(),
                                     [{"role": "user", "content": "x"}], [])
        self.assertIn("disagree", str(raised.exception))

    def test_the_asset_s_own_template_path_is_recorded(self):
        counted = cap.count_request_prompt(self.tokenizer,
                                           [{"role": "user", "content": "你好"}], [])
        self.assertEqual(counted["template_sha256"], cap.TARGET_TOKENIZER["template_sha256"])
        self.assertEqual(counted["template_variant"], "qwen3_instruct_no_thinking")
        self.assertEqual(counted["undefined_policy"], cap.UNDEFINED_POLICY)
        self.assertTrue(counted["rendered_sha256"])


class ServiceCopyConsistencyTests(unittest.TestCase):
    """One consistency check: the service's copy is the project's counting entry."""

    def setUp(self):
        if not STAGED_HELPER.is_file():
            self.skipTest("the staged service helper is absent")

    def test_the_drift_check_passes(self):
        completed = subprocess.run([sys.executable, "-B",
                                    str(ROOT / "tools/check_tokenizer_drift.py")],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_the_service_copy_counts_identically(self):
        staged = _load_module("ae_qwen_tokenizer_staged_check",
                              STAGED_HELPER)
        try:
            assets = cap.load_target_tokenizer_assets()
        except (FileNotFoundError, ValueError) as exc:
            self.skipTest(f"the target assets are not available: {exc}")
        project_tokenizer = cap.QwenBpeTokenizer(assets["tokenizer_json"],
                                                 assets["tokenizer_config"])
        staged_tokenizer = staged.QwenBpeTokenizer(assets["tokenizer_json"],
                                                   assets["tokenizer_config"])
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "answer",
                     "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}]},
                    {"role": "tool", "content": "result"}]
        tools = [{"type": "function",
                  "function": {"name": "f", "parameters": {"type": "object",
                                                           "properties": {}}}}]
        project = cap.count_request_prompt(project_tokenizer, messages, tools)
        service = staged.count_request_prompt(staged_tokenizer, messages, tools)
        self.assertEqual(project["prompt_tokens"], service["prompt_tokens"])
        self.assertEqual(project["template_variant"], service["template_variant"])
        self.assertEqual(project["renderer"], service["renderer"])
        self.assertEqual(project["template_sha256"], service["template_sha256"])


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
