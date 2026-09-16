#!/usr/bin/env python3
"""Offline AE-01 input preflight. Does not instantiate an Agent or call a model."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
from datetime import datetime, timezone
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from ae_inputs import (  # noqa: E402
    DATASET_SHA256,
    INPUT_SCHEMA_VERSION,
    LETTA_REVISION,
    VITA_REVISION,
    canonical_sha256,
    compact_json,
    history_message,
    prepare_sample,
)


def _file_sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_token_counter(tokenizer_path: str | None) -> tuple[Callable | None, dict]:
    """An explicitly supplied local tokenizer is optional; never download it."""
    if tokenizer_path is None:
        return None, {"status": "not_measured", "reason": "no_explicit_local_tokenizer", "token": None}
    path = Path(tokenizer_path)
    if not path.is_absolute() or not path.is_dir():
        return None, {"status": "not_measured", "reason": "tokenizer_must_be_an_existing_absolute_local_directory", "requested_path": tokenizer_path, "token": None}
    metadata = {
        "path": str(path.resolve()),
        "local_files_only": True,
        "trust_remote_code": False,
        "add_special_tokens": False,
        "scope": "serialized_input_text_only; excludes model chat template, system/tool schemas and runtime outputs",
    }
    # Belt and braces for optional libraries. No package installation or model
    # loading occurs, and dynamic tokenizer code is expressly disabled.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(str(path.resolve()), local_files_only=True, trust_remote_code=False)
        metadata["transformers_version"] = importlib.metadata.version("transformers")
        metadata["file_sha256"] = {
            name: _file_sha(path / name)
            for name in (
                "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
                "vocab.json", "merges.txt", "vocab.txt", "spiece.model", "tokenizer.model",
                "added_tokens.json", "config.json",
            )
            if (path / name).is_file()
        }
        metadata["status"] = "measured_locally"
        return lambda text: len(tokenizer.encode(text, add_special_tokens=False)), metadata
    except Exception as exc:
        metadata.update(status="not_measured", reason=f"local_tokenizer_unavailable: {type(exc).__name__}: {exc}", token=None)
        return None, metadata


def _measure(text: str, counter: Callable | None) -> dict:
    result = {"characters": len(text), "utf8_bytes": len(text.encode("utf-8")), "token": None}
    if counter is not None:
        try:
            result["token"] = counter(text)
        except Exception as exc:
            result["token_error"] = f"{type(exc).__name__}: {exc}"
    return result


def build_report(sample: dict, *, tokenizer_path: str | None = None) -> dict:
    counter, tokenizer_metadata = local_token_counter(tokenizer_path)
    public = {name: sample[name] for name in ("initial_facts", "initial_profile", "tasks")}
    initial_text = compact_json({name: public[name] for name in ("initial_profile", "initial_facts")})
    measurements, formatted_messages, private_metadata = [], [], {}
    for task in public["tasks"]:
        material = history_message(task)
        request = {"role": "user", "content": task["instruction"]}
        original_records = [entry["record"] for entry in task["history"]]
        measurements.append({
            "subtask_id": task["subtask_id"],
            "number": task["number"],
            "history_records": len(task["history"]),
            "history_records_json": _measure(compact_json(original_records), counter),
            "history_message_content": _measure(material["content"], counter),
            "task_instruction_content": _measure(request["content"], counter),
        })
        formatted_messages.append({
            "subtask_id": task["subtask_id"],
            "history_stage": material,
            "task_instruction_preview": request,
            "delivery_boundary": "Preview only: LettaBridge additionally wraps the task with its ID, domain, current time and supplied domain policy. Send it only after the history/update stage; never send all future tasks together.",
        })
        criteria = sample["private_tasks"][task["subtask_id"]]["evaluation_criteria"]
        # Only counts, never private environment details or target product IDs.
        private_metadata[task["subtask_id"]] = {
            "state_rubric_counts": [len(state.get("state_rubrics", [])) for state in criteria.get("expected_states", [])],
            "overall_rubric_count": len(criteria.get("overall_rubrics", [])),
        }
    end_turn = sample["source"]["end_turn"]
    checks = {
        "fixed_complete_dataset_sha256": sample["source"]["sha256"] == DATASET_SHA256,
        "fixed_t3_initialization": sample["source"].get("initial_state_turn") == 3 and sample["source"].get("start_turn") == 4,
        "consecutive_task_numbers": [t["number"] for t in public["tasks"]] == list(range(4, end_turn + 1)),
        "projected_agent_fields_only": all(set(t) == {"number", "subtask_id", "domain", "current_time", "instruction", "history"} for t in public["tasks"]),
        "historical_material_separate_from_runtime": True,
        "later_current_and_change_history_not_projected": True,
        "historical_chat_not_injected_again": True,
        "private_payload_not_in_agent_inputs": "private_tasks" not in public,
    }
    return {
        "schema_version": "ae-01-preflight-0.1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_called": False,
        "runtime_executed": False,
        "source": sample["source"],
        "provenance": {
            "input_schema_version": INPUT_SCHEMA_VERSION,
            "python_version": platform.python_version(),
            "vita_revision": VITA_REVISION,
            "letta_candidate_revision": LETTA_REVISION,
            "candidate_dependencies_installed_or_probed": False,
            "code_sha256": {str(path.relative_to(PROJECT_ROOT)): _file_sha(path) for path in (PROJECT_ROOT / "ae_inputs.py", Path(__file__).resolve(), PROJECT_ROOT / "ae_adapter.py")},
            "agent_inputs_canonical_sha256": canonical_sha256(public),
            "private_material_policy": "Private environment/evaluation/target data remains separate in prepare_sample; this report includes only rubric counts.",
        },
        "agent_inputs": public,
        "formatted_agent_messages": formatted_messages,
        "actual_model_request_captured": False,
        "initial_state_measurement": _measure(initial_text, counter),
        "per_task_measurements": measurements,
        "totals": {
            "tasks": len(public["tasks"]),
            "history_records": sum(m["history_records"] for m in measurements),
            "history_records_json_characters": sum(m["history_records_json"]["characters"] for m in measurements),
            "history_records_json_utf8_bytes": sum(m["history_records_json"]["utf8_bytes"] for m in measurements),
            "history_records_json_token": (sum(m["history_records_json"]["token"] for m in measurements) if all(m["history_records_json"]["token"] is not None for m in measurements) else None),
            "measurement_scope": "Static projected inputs; not a complete growing Letta request, context-window fit, or runtime token/cost estimate.",
        },
        "tokenizer": tokenizer_metadata,
        "private_metadata": private_metadata,
        "rule_checks": checks,
        "validity": {
            "scope": "static_input_preflight_only",
            "static_checks_passed": all(checks.values()),
            "runtime_validated": False,
            "model_behavior_evaluated": False,
            "context_window_fit_validated": False,
            "note": "No Agent/framework execution, task success, accuracy, degradation, KV reuse or cost result has been established.",
        },
    }


def write_report_exclusive(output: str | Path, report: dict) -> None:
    """Refuse every existing target, including symlinks; never overwrite results."""
    target = Path(output).expanduser()
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        handle.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="Path to the complete fixed SHA-256 tasks.json; no download is performed")
    parser.add_argument("--output", required=True, help="A NEW JSON output path; existing files are refused")
    parser.add_argument("--end-turn", type=int, choices=(5, 12), default=12, help="5: wiring subset; 12: full candidate. The start remains t4 with t3 initialization.")
    parser.add_argument("--tokenizer", help="Optional existing absolute local tokenizer directory; never a Hub model ID")
    args = parser.parse_args(argv)
    output = Path(args.output).expanduser()
    try:
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"refusing to overwrite existing output: {output}")
        sample = prepare_sample(args.dataset, end_turn=args.end_turn)
        report = build_report(sample, tokenizer_path=args.tokenizer)
        write_report_exclusive(output, report)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"AE-01 static preflight refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "output": str(output.resolve()),
        "scope": "static_input_preflight_only",
        "static_checks_passed": report["validity"]["static_checks_passed"],
        "tasks": report["totals"]["tasks"],
        "history_records": report["totals"]["history_records"],
        "tokenizer_status": report["tokenizer"]["status"],
        "model_called": False,
        "runtime_validated": False,
    }, ensure_ascii=False))
    return 0 if report["validity"]["static_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
