#!/usr/bin/env python3
"""Measure the REAL per-stage material of t4..t12 with the official tokenizer.

Everything here is measured, not assumed:

* each task's history envelope tokens (pinned `ae_inputs.history_message`);
* each task's instruction, domain policy and user-simulator system prompt tokens;
* each task's REAL tool schema set, taken from the pinned Vita environment the run
  uses (not from a fixture), rendered the way the wire renders it;
* the sealed run's own per-request category split, so the anchor can be placed on a
  real request rather than on an assumption.

Offline: it builds the pinned environments but never calls a model or a tool.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ae_inputs import history_message, prepare_sample  # noqa: E402
from ae_multiturn_capacity import QwenBpeTokenizer, load_official_tokenizer_assets  # noqa: E402

CHECKOUT = Path(os.environ.get("AE_VITA_SOURCE", ROOT / ".ae-verify-src/source"))


def main():
    assets = load_official_tokenizer_assets()
    tokenizer = QwenBpeTokenizer(assets["tokenizer_json"], assets["tokenizer_config"])
    sample = prepare_sample(ROOT / ".ae-verify-src/tasks-full-a4553e1.json")
    os.environ.setdefault("OPENAI_API_KEY", "EMPTY")
    os.environ.setdefault("VITA_MODEL_CONFIG_PATH", str(ROOT / ".ae-verify-src/vita-models.json"))
    from ae_vita import NativeVita

    native = NativeVita(CHECKOUT, ROOT / ".ae-verify-src/tasks-full-a4553e1.json",
                        "http://127.0.0.1:8000", "Qwen/Qwen3-30B-A3B-Instruct-2507", 0, 4096,
                        seed=300, transport_profile="siliconflow-letta-text-transport-v2-prototype",
                        end_turn=12)
    try:
        previews = native.preview_tasks()
    finally:
        native.abort()
    rows = []
    for task, preview in zip(sample["tasks"], previews):
        envelope = history_message(task)
        schemas = preview["tool_schemas"]
        schema_text = json.dumps({"tools": schemas}, ensure_ascii=False, separators=(", ", ": "))
        rows.append({
            "task_number": task["number"], "subtask_id": task["subtask_id"],
            "domain": task["domain"],
            "history_records": len(json.loads(envelope["content"])["records"]),
            "history_tokens": tokenizer.count(envelope["content"]),
            "instruction_tokens": tokenizer.count(task["instruction"]),
            "domain_policy_tokens": tokenizer.count(preview["domain_policy"]),
            "user_system_prompt_tokens": tokenizer.count(preview["user_system_prompt"]),
            "tool_count": len(schemas),
            "tool_schema_tokens": tokenizer.count(schema_text),
            "tool_names": [schema["name"] for schema in schemas],
        })
    print(json.dumps({"tokenizer": {"snapshot": assets["snapshot"], "revision": assets["revision"]},
                      "tasks": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
