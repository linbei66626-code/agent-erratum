#!/usr/bin/env python3
"""Offline tests for scripts/ae_01_capacity_report.py (stdlib unittest only).

Run from the repository root:

    python3 tests/test_ae_01_capacity_report.py -v
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ae_01_capacity_report as report_mod  # noqa: E402


def wire(body: dict | None) -> dict:
    raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    record = {
        "body_base64": base64.b64encode(raw).decode("ascii"),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "body_bytes": len(raw),
    }
    if body is not None:
        record["body_utf8"] = raw.decode("utf-8")
    return record


def chat_body(messages, *, tools=None, max_completion_tokens=2048, model="Qwen3-8B"):
    body = {"model": model, "messages": messages}
    if tools is not None:
        body["tools"] = tools
    if max_completion_tokens is not None:
        body["max_completion_tokens"] = max_completion_tokens
    return body


def tools_fixture(count: int) -> list[dict]:
    return [{"type": "function",
             "function": {"name": f"tool_{i}", "description": "说明文字",
                          "parameters": {"type": "object", "properties": {}}}}
            for i in range(count)]


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def base_records(request_id: str, seq: int, body: dict, *, role="agent_or_unknown",
                 timestamp="2026-09-11T02:36:17.000000+00:00") -> list[dict]:
    return [{
        "kind": "client_request", "sequence": seq, "request_id": request_id,
        "method": "POST", "path": "/v1/chat/completions", "purpose": None,
        "role": role, "timestamp": timestamp, **wire(body),
    }]


class NormalAssociationTest(unittest.TestCase):
    """A well-formed call is associated across tokenize, gate and inference."""

    def test_completed_call_is_associated_and_counted(self):
        body = chat_body([{"role": "system", "content": "你是一个助手"},
                          {"role": "user", "content": "偏好"}],
                         tools=tools_fixture(1))
        records = [{"kind": "proxy_open", "sequence": 0,
                    "config": {"context_window": 32768, "max_prompt_tokens": 28672,
                               "output_reserve_tokens": 4096, "model": "Qwen3-8B"},
                    "source_wheel_sha256": "a" * 64, "automatic_retry": False}]
        records += base_records("rid-ok", 1, body)
        records.append({"kind": "upstream_request", "sequence": 2, "request_id": "rid-ok",
                        "purpose": "preflight_tokenize", **wire(body)})
        records.append({"kind": "upstream_response", "sequence": 3, "request_id": "rid-ok",
                        "purpose": "preflight_tokenize", "http_status": 200,
                        **wire({"count": 1234, "tokens": [1] * 1234,
                                "max_model_len": 32768})})
        records.append({"kind": "token_gate", "sequence": 4, "request_id": "rid-ok",
                        "prompt_tokens": 1234, "output_reserve": 4096,
                        "context_window": 32768, "max_prompt_tokens": 28672,
                        "passed": True})
        records.append({"kind": "upstream_request", "sequence": 5, "request_id": "rid-ok",
                        "purpose": "model_inference", **wire(body)})
        records.append({"kind": "upstream_response", "sequence": 6, "request_id": "rid-ok",
                        "purpose": "model_inference", "http_status": 200,
                        **wire({"choices": [{"finish_reason": "stop"}]})})
        records.append({"kind": "model_summary", "sequence": 7, "request_id": "rid-ok",
                        "http_status": 200, "usage": {"prompt_tokens": 1234},
                        "finish_reasons": ["stop"], "truncation_finish": False,
                        "tokenize_prompt_usage_agreement": True})
        records.append({"kind": "proxy_close", "sequence": 8, "request_count": 1,
                        "blocked": False})

        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "capture.jsonl"
            write_jsonl(capture, records)
            report = report_mod.build_report(capture)
            capture_digest = hashlib.sha256(capture.read_bytes()).hexdigest()

            call = report["calls"][0]
            self.assertEqual(call["classification"], "model_inference_completed")
            self.assertTrue(call["completed_inference"])
            self.assertTrue(call["real_model_call"])
            self.assertTrue(call["preflight_tokenize_called"])
            self.assertEqual(call["recorded_prompt_tokens"], 1234)
            self.assertEqual(call["requested_output_limit"], 2048)
            self.assertEqual(call["proxy_output_reserve"], 4096)
            self.assertEqual(report["completed_inference_calls"], 1)
            self.assertEqual(report["real_model_upstream_calls"], 1)
            self.assertEqual(report["counts"], {"model_inference_completed": 1})
            # source SHA is the capture file digest, not a guess.
            self.assertEqual(call["source_capture_sha256"], capture_digest)


class RejectionNotCompletionTest(unittest.TestCase):
    """The gate rejection and the follow-up 503s are never counted as calls."""

    def test_rejected_and_post_stop_503_are_not_completed(self):
        big = chat_body([{"role": "system", "content": "你是一个助手"},
                         {"role": "user", "content": "历史" * 10}],
                        tools=tools_fixture(1))
        small = chat_body([{"role": "system", "content": "summarize"},
                           {"role": "user", "content": "transcript"}],
                          tools=[], max_completion_tokens=4096)
        records = [{"kind": "proxy_open", "sequence": 0,
                    "config": {"context_window": 32768, "max_prompt_tokens": 28672,
                               "output_reserve_tokens": 4096, "model": "Qwen3-8B"},
                    "automatic_retry": False}]
        records += base_records("rid-reject", 1, big)
        records.append({"kind": "upstream_request", "sequence": 2, "request_id": "rid-reject",
                        "purpose": "preflight_tokenize", **wire(big)})
        records.append({"kind": "upstream_response", "sequence": 3, "request_id": "rid-reject",
                        "purpose": "preflight_tokenize", "http_status": 200,
                        **wire({"count": 31501, "tokens": [1] * 31501,
                                "max_model_len": 32768})})
        records.append({"kind": "token_gate", "sequence": 4, "request_id": "rid-reject",
                        "prompt_tokens": 31501, "output_reserve": 4096,
                        "context_window": 32768, "max_prompt_tokens": 28672,
                        "passed": False})
        records.append({"kind": "blocked", "sequence": 5, "request_id": "rid-reject",
                        "code": "prompt_or_context_limit",
                        "outcome": "do_not_assume_safe_to_retry"})
        records.append({"kind": "client_rejected", "sequence": 6,
                        "code": "prompt_or_context_limit", "http_status": 413})
        seq = 7
        for index in range(3):
            rid = f"rid-503-{index}"
            records += base_records(rid, seq, small,
                                    timestamp=f"2026-09-11T02:39:40.{index}00000+00:00")
            seq += 1
            records.append({"kind": "blocked", "sequence": seq, "request_id": rid,
                            "code": "proxy_stopped_after_failure",
                            "outcome": "do_not_assume_safe_to_retry"})
            seq += 1
            records.append({"kind": "client_rejected", "sequence": seq,
                            "code": "proxy_stopped_after_failure", "http_status": 503})
            seq += 1
        records.append({"kind": "proxy_close", "sequence": seq, "request_count": 1,
                        "blocked": True})

        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "capture.jsonl"
            write_jsonl(capture, records)
            report = report_mod.build_report(capture)

        rejected = report["calls"][0]
        self.assertEqual(rejected["classification"], "rejected_at_token_gate")
        self.assertFalse(rejected["completed_inference"])
        self.assertFalse(rejected["real_model_call"])
        self.assertIsNone(rejected["model_summary"])
        self.assertEqual(rejected["rejection"]["code"], "prompt_or_context_limit")
        self.assertEqual(rejected["rejection"]["http_status"], 413)
        self.assertFalse(rejected["token_gate_passed"])

        for call in report["calls"][1:]:
            self.assertEqual(call["classification"], "rejected_after_proxy_stop")
            self.assertFalse(call["completed_inference"])
            self.assertFalse(call["real_model_call"])
            self.assertFalse(call["preflight_tokenize_called"])
            self.assertIsNone(call["recorded_prompt_tokens"])
            self.assertIsNone(call["model_summary"])
            self.assertTrue(call["post_rejection_attempt"])

        self.assertEqual(report["completed_inference_calls"], 0)
        self.assertEqual(report["real_model_upstream_calls"], 0)
        self.assertEqual(report["preflight_tokenize_calls"], 1)
        self.assertEqual(report["counts"],
                         {"rejected_at_token_gate": 1, "rejected_after_proxy_stop": 3})
        # The rejected request has no previous rewrite call to compare with.
        self.assertIsNone(report["previous_rewrite_request"])
        self.assertIsNone(report["comparison"])


class CharByteAndTokenHonestyTest(unittest.TestCase):
    """Chinese text: chars != UTF-8 bytes; per-message tokens stay null."""

    def test_chinese_chars_differ_from_bytes_and_tokens_are_null(self):
        text = "不吃香菜，偏好七分糖。"
        body = chat_body([{"role": "system", "content": "你是一个助手"},
                          {"role": "user", "content": text}])
        records = base_records("rid-cn", 1, body)
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "capture.jsonl"
            write_jsonl(capture, records)
            report = report_mod.build_report(capture)
            entry = report_mod.group_capture(report_mod.read_jsonl(capture))[1]["rid-cn"]

        rows = report_mod.message_breakdown(entry["client_request"]["body_utf8"] and
                                            json.loads(entry["client_request"]["body_utf8"])["messages"])
        user_row = rows[1]
        self.assertEqual(user_row["content_chars"], len(text))
        self.assertEqual(user_row["content_utf8_bytes"], len(text.encode("utf-8")))
        self.assertGreater(user_row["content_utf8_bytes"], user_row["content_chars"])
        self.assertIsNone(user_row["prompt_tokens"])
        self.assertIsNone(user_row["token_source"])
        self.assertEqual(user_row["category"], "user_unrecognized")

    def test_token_accounting_block_declares_no_per_message_tokens(self):
        body = chat_body([{"role": "user", "content": "中文"}])
        records = [{"kind": "proxy_open", "sequence": 0,
                    "config": {"context_window": 32768, "max_prompt_tokens": 28672,
                               "output_reserve_tokens": 4096, "model": "Qwen3-8B"}}]
        records += base_records("rid-t", 1, body)
        records.append({"kind": "token_gate", "sequence": 2, "request_id": "rid-t",
                        "prompt_tokens": 31501, "output_reserve": 4096,
                        "context_window": 32768, "max_prompt_tokens": 28672,
                        "passed": False})
        records.append({"kind": "blocked", "sequence": 3, "request_id": "rid-t",
                        "code": "prompt_or_context_limit"})
        records.append({"kind": "client_rejected", "sequence": 4,
                        "code": "prompt_or_context_limit", "http_status": 413})
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "capture.jsonl"
            write_jsonl(capture, records)
            report = report_mod.build_report(capture)
        accounting = report["token_accounting"]
        self.assertEqual(accounting["measured_whole_request_tokens"], 31501)
        self.assertIsNone(accounting["per_message_tokens"])
        self.assertFalse(accounting["character_byte_ratio_used_for_tokens"])
        rejected = report["rejected_request"]
        for row in rejected["messages"]:
            self.assertIsNone(row["prompt_tokens"])
            self.assertIsNone(row["token_source"])


class AttributionTest(unittest.TestCase):
    """Arm/stage/phase come from event windows, otherwise unknown."""

    def _events(self) -> list[dict]:
        history_message = {"role": "user",
                           "content": json.dumps({"source": "dataset_history/material"})}
        return [
            {"kind": "stage_start", "sequence": 0, "arm": "rewrite",
             "task": "sub_U000828_4", "timestamp": "2026-09-11T02:35:32.000000+00:00"},
            {"kind": "bridge_event", "sequence": 1, "arm": "rewrite",
             "task": None, "phase": "history",
             "timestamp": "2026-09-11T02:35:32.200000+00:00",
             "event": {"kind": "request", "method": "POST",
                       "path": "/v1/agents/a/messages",
                       "body": {"messages": [history_message]}}},
            {"kind": "stage_complete", "sequence": 2, "arm": "rewrite",
             "timestamp": "2026-09-11T02:37:05.000000+00:00",
             "stage": {"subtask_id": "sub_U000828_4"}},
        ]

    def test_inside_window_is_labelled_outside_is_unknown(self):
        attribution = report_mod.build_attribution(self._events())
        inside = report_mod.attribute(
            report_mod.parse_ts("2026-09-11T02:36:00.000000+00:00"), attribution)
        self.assertEqual(inside["arm"], "rewrite")
        self.assertEqual(inside["stage"], "sub_U000828_4")
        self.assertEqual(inside["turn"], "t4")
        self.assertEqual(inside["phase"], "history")
        self.assertNotEqual(inside["arm_stage_source"], "unknown")

        outside = report_mod.attribute(
            report_mod.parse_ts("2026-09-11T02:34:02.000000+00:00"), attribution)
        self.assertIsNone(outside["arm"])
        self.assertIsNone(outside["stage"])
        self.assertIsNone(outside["phase"])
        self.assertEqual(outside["arm_stage_source"], "unknown")


class MessageIdentityTest(unittest.TestCase):
    """Complete-message identity: all fields, matched one-to-one by count."""

    @staticmethod
    def detail(messages, tokens=None, tools=(0, 0)):
        rows = report_mod.message_breakdown(messages)
        return {
            "messages": rows,
            "message_count": len(rows),
            "recorded_prompt_tokens": tokens,
            "content_chars_total": sum(row["content_chars"] for row in rows),
            "content_utf8_bytes_total": sum(row["content_utf8_bytes"] for row in rows),
            "tools": {"count": tools[0], "json_chars": 0, "utf8_bytes": tools[1],
                      "names": []},
        }

    def test_reasoning_only_change_is_not_retained(self):
        same_content = [{"role": "assistant", "content": "结果", "reasoning_content": "A"}]
        changed_reasoning = [{"role": "assistant", "content": "结果",
                              "reasoning_content": "B"}]
        control = report_mod.compare(self.detail(same_content),
                                     self.detail([{"role": "assistant", "content": "结果",
                                                   "reasoning_content": "A"}]))
        self.assertEqual(control["retained_count"], 1)
        self.assertEqual(control["added_count"], 0)

        changed = report_mod.compare(self.detail(same_content),
                                     self.detail(changed_reasoning))
        self.assertEqual(changed["retained_count"], 0)
        self.assertEqual(changed["added_count"], 1)
        # content is identical, only reasoning_content differs
        self.assertEqual(changed["added_messages"][0]["content_chars"],
                         control["retained_messages"][0]["content_chars"])
        self.assertIn("reasoning_content", changed["identity_basis"])

    def test_duplicate_messages_match_one_to_one(self):
        message = {"role": "user", "content": "重复"}
        both = report_mod.compare(self.detail([message, message]),
                                  self.detail([message, message]))
        self.assertEqual((both["retained_count"], both["added_count"]), (2, 0))

        surplus = report_mod.compare(self.detail([message]),
                                     self.detail([message, message]))
        self.assertEqual((surplus["retained_count"], surplus["added_count"]), (1, 1))
        self.assertEqual(surplus["added_messages"][0]["index"], 1)
        self.assertEqual(surplus["retained_messages"][0]["previously_at_index"], 0)

        removed = report_mod.compare(self.detail([message, message]),
                                     self.detail([message]))
        self.assertEqual((removed["retained_count"], removed["added_count"]), (1, 0))

    def test_system_category_is_system_message_with_segments(self):
        system = {"role": "system",
                  "content": "前缀\n共同起点的用户资料：\n{\"a\":1}\n"
                             "<memory_blocks>\n<value>{}</value>\n</memory_blocks>\n"
                             "<memory_metadata>\n- AGENT_ID: x\n</memory_metadata>"}
        rows = report_mod.message_breakdown([system])
        category, _ = report_mod.classify_message(system)
        self.assertEqual(category, "system_message")
        self.assertEqual(rows[0]["category"], "system_message")
        segments = rows[0]["system_segments"]
        self.assertIn("memory_blocks", segments)
        self.assertGreater(segments["memory_blocks"]["chars"], 0)


class GrowthSummaryTest(unittest.TestCase):
    """Signed tool-schema change and complete-message wording in summary.md."""

    @staticmethod
    def report(tools_byte_delta, retained_count=10):
        return {
            "counts": {"model_inference_completed": 18, "rejected_at_token_gate": 1},
            "rejected_request": {
                "request_id": "a" * 32, "arm": "rewrite", "turn": "t5",
                "phase": "history", "recorded_prompt_tokens": 31501,
                "max_prompt_tokens": 28672, "context_window": 32768,
                "proxy_output_reserve": 4096, "rejection": {"http_status": 413},
                "message_count": 12,
                "category_totals": {"system_message": {"content_chars": 1696,
                                                       "content_utf8_bytes": 2694}},
                "tools": {"count": 1, "utf8_bytes": 753},
            },
            "previous_rewrite_request": {"recorded_prompt_tokens": 26046},
            "comparison": {"added_count": 2, "added_content_chars": 20182,
                           "added_content_utf8_bytes": 31118,
                           "retained_count": retained_count,
                           "tools_byte_delta": tools_byte_delta,
                           "token_delta": 5455},
        }

    def test_signed_schema_phrase(self):
        self.assertEqual(report_mod.schema_delta_phrase(11595),
                         "schema 增加 11595 字节")
        self.assertEqual(report_mod.schema_delta_phrase(-11595),
                         "schema 减少 11595 字节")
        self.assertEqual(report_mod.schema_delta_phrase(0), "schema 不变")
        self.assertIn("未知", report_mod.schema_delta_phrase(None))

    def test_summary_direction_and_identity_wording(self):
        for delta, phrase in ((500, "schema 增加 500 字节"),
                              (-500, "schema 减少 500 字节"),
                              (0, "schema 不变")):
            text = report_mod.build_summary(self.report(delta))
            self.assertIn(phrase, text)
            self.assertIn("完整消息对象相同", text)
            self.assertNotIn("逐字节保留", text)
            self.assertIn("非原始传输 JSON 字节一致", text)


class OutputGuardTest(unittest.TestCase):
    """The output directory is exclusive: existing content is never overwritten."""

    def test_nonempty_output_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            (out_dir / "report.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(report_mod.CliError):
                report_mod.ensure_output_dir(out_dir)
            self.assertEqual((out_dir / "report.json").read_text(encoding="utf-8"), "{}")

    def test_empty_or_absent_output_dir_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "nested" / "out"
            report_mod.ensure_output_dir(out_dir)
            self.assertTrue(out_dir.is_dir())
            report_mod.ensure_output_dir(out_dir)  # still empty -> fine


if __name__ == "__main__":
    unittest.main(verbosity=2)
