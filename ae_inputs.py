"""Verified, offline input projection for AE-01; no framework or model imports."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any


INPUT_SCHEMA_VERSION = "ae-inputs-0.1"
DATASET_REVISION = "a4553e13ec081be65cc37b48845da5819692d438"
DATASET_SHA256 = "3a05f7e2742f204ae28b71db95b14e2daa94c9ff354f510a7d9c707564c7466e"
USER_CANONICAL_SHA256 = "a1c3a97d9c26dd2025bcfd1af75be05c16d5da979e8298b3159cd36686481aa9"
VITA_REVISION = "f60169e89f30499cb7883f3dad76bd03facc908d"
LETTA_REVISION = "56ba9c25552605eec89de8ed3dc6394b625c1993"
USER_ID = "U000828"
USER_INDEX = 25
TASK_FIELDS = frozenset(
    {"number", "subtask_id", "domain", "current_time", "instruction", "history"}
)
HISTORY_FIELDS = frozenset({"date", "behavior", "dialogue"})


class InputValidationError(ValueError):
    """The proposed input is outside the inspected AE-01 sample boundary."""


def compact_json(value: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(compact_json(value, sort_keys=True).encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InputValidationError(message)


def _day(value: Any, label: str) -> date:
    _require(isinstance(value, str), f"{label} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InputValidationError(f"{label} is not an ISO date: {value!r}") from exc
    _require(parsed.isoformat() == value, f"{label} must use YYYY-MM-DD")
    return parsed


def _validate_range(start_turn: int, end_turn: int) -> None:
    _require(type(start_turn) is int and start_turn == 4, "start_turn is fixed at 4; later gold resets are forbidden")
    _require(type(end_turn) is int and end_turn in (5, 12), "end_turn must be 5 (wiring subset) or 12 (full candidate)")


def _locate_user(users: Any) -> dict:
    _require(isinstance(users, list), "dataset root must be a list")
    matches = [(i, u) for i, u in enumerate(users) if isinstance(u, dict) and u.get("id") == USER_ID]
    _require(len(matches) == 1, f"expected exactly one {USER_ID} user")
    index, user = matches[0]
    _require(index == USER_INDEX, f"{USER_ID} must retain original index {USER_INDEX}, got {index}")
    _require(user.get("user_id") == USER_ID, "id and user_id disagree")
    return user


def _validate_record(record: Any, label: str) -> None:
    _require(isinstance(record, dict) and set(record) == HISTORY_FIELDS, f"{label} must contain only date, behavior, dialogue")
    _day(record["date"], f"{label}.date")
    _require(isinstance(record["behavior"], list), f"{label}.behavior must be a list")
    _require(isinstance(record["dialogue"], list), f"{label}.dialogue must be a list")
    for i, message in enumerate(record["dialogue"]):
        _require(isinstance(message, dict) and set(message) == {"role", "content"}, f"{label}.dialogue[{i}] has unexpected fields")
        _require(message["role"] in ("user", "assistant"), f"{label}.dialogue[{i}] has an unexpected historical role")
        _require(isinstance(message["content"], str), f"{label}.dialogue[{i}].content must be text")


def project_user(user: dict, *, start_turn: int = 4, end_turn: int = 12) -> dict:
    """Project a user object without claiming its file identity was verified.

    This small-object seam supports unit fixtures. Production callers must use
    prepare_sample(), which checks the complete fixed file before projection.
    """
    _validate_range(start_turn, end_turn)
    _require(isinstance(user, dict) and user.get("id") == USER_ID and user.get("user_id") == USER_ID, "wrong sample user")
    subtasks = user.get("subtasks")
    _require(isinstance(subtasks, list) and len(subtasks) >= end_turn, "sample lacks required consecutive subtasks")

    # Deliberately fixed at t3: never select the task immediately before an
    # arbitrary requested start, and never refresh from later current snapshots.
    initial_task = subtasks[2]
    scenario = initial_task["user_scenario"]
    current = scenario["personalized_preference_memory"]["current"]
    _require(isinstance(current, dict), "t3 current must be a category mapping")
    facts: dict[str, dict[str, str]] = {}
    for category, entries in current.items():
        _require(isinstance(category, str) and isinstance(entries, list), "invalid t3 preference category")
        for content in entries:
            _require(isinstance(content, str), "t3 preference facts must be strings")
            facts[f"p{len(facts):03d}"] = {"category": category, "content": content}
    _require(any(f["content"] == "奶茶偏好5分糖" for f in facts.values()), "t3 baseline must contain 奶茶偏好5分糖")
    profile = scenario["user_profile"]
    _require(isinstance(profile, dict) and profile.get("user_id") == USER_ID, "invalid t3 user profile")

    public_tasks, private_tasks = [], {}
    previous_task_day = _day(initial_task["current_time"], "t3.current_time")
    for number in range(start_turn, end_turn + 1):
        task = subtasks[number - 1]
        task_id = task["subtask_id"]
        _require(task_id == f"sub_{USER_ID}_{number}", f"unexpected task id at t{number}: {task_id!r}")
        _require(task.get("task_turn_num") == f"{USER_ID}_{number:02d}", f"task order mismatch at t{number}")
        task_day = _day(task["current_time"], f"t{number}.current_time")
        _require(task_day > previous_task_day, "task dates must advance")
        start_day = _day(task["start_date"], f"t{number}.start_date")
        end_day = _day(task["end_date"], f"t{number}.end_date")
        _require(start_day <= end_day <= task_day, f"invalid history window at t{number}")
        interactions = task["interactions"]
        _require(isinstance(interactions, list) and interactions, f"t{number} must retain its historical records")
        history = []
        last_record_day = None
        for i, record in enumerate(interactions):
            ref = f"t{number}/history/{i}"
            _validate_record(record, ref)
            record_day = _day(record["date"], ref)
            _require(start_day <= record_day <= end_day, f"{ref} is outside its original historical window")
            _require(record_day < task_day, f"{ref} reaches the current/future task date")
            if number == 4:
                _require(record_day >= previous_task_day, f"{ref} predates the t3 initialization cutoff")
            else:
                _require(record_day > previous_task_day, f"{ref} overlaps the preceding executed task date")
            _require(last_record_day is None or record_day >= last_record_day, f"{ref} is out of chronological order")
            last_record_day = record_day
            history.append({"ref": ref, "record": copy.deepcopy(record)})
        _require(isinstance(task["instruction"], str) and isinstance(task["domain"], str), f"invalid task text at t{number}")
        public_tasks.append({
            "number": number,
            "subtask_id": task_id,
            "domain": task["domain"],
            "current_time": task["current_time"],
            "instruction": task["instruction"],
            "history": history,
        })
        # Kept separate for the environment/official evaluator; never send this
        # mapping through history_message or as an Agent message.
        private_tasks[task_id] = {
            name: copy.deepcopy(task[name])
            for name in ("environment", "evaluation_criteria", "target_product_ids")
        }
        previous_task_day = task_day
    return {
        "initial_facts": facts,
        "initial_profile": copy.deepcopy(profile),
        "tasks": public_tasks,
        "private_tasks": private_tasks,
    }


def prepare_sample(path: str | Path, *, start_turn: int = 4, end_turn: int = 12) -> dict:
    """Verify the fixed complete dataset, then create isolated public/private inputs."""
    _validate_range(start_turn, end_turn)
    source_path = Path(path).expanduser().resolve(strict=True)
    raw = source_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    _require(digest == DATASET_SHA256, f"dataset SHA-256 mismatch: expected {DATASET_SHA256}, got {digest}")
    byte_count = len(raw)
    users = json.loads(raw)
    del raw
    user = _locate_user(users)
    user_digest = canonical_sha256(user)
    _require(user_digest == USER_CANONICAL_SHA256, "canonical sample SHA-256 mismatch")
    sample = project_user(user, start_turn=start_turn, end_turn=end_turn)
    sample["source"] = {
        "path": str(source_path),
        "sha256": digest,
        "bytes": byte_count,
        "dataset_revision": DATASET_REVISION,
        "dataset_users": len(users),
        "dataset_subtasks": sum(len(u["subtasks"]) for u in users),
        "user_id": USER_ID,
        "original_user_index": USER_INDEX,
        "user_canonical_sha256": user_digest,
        "initial_state_turn": 3,
        "initial_state_time": user["subtasks"][2]["current_time"],
        "start_turn": start_turn,
        "end_turn": end_turn,
    }
    return sample


def history_message(task: dict) -> dict[str, str]:
    """Wrap the retained batch as material, without sending its current instruction."""
    _require(isinstance(task, dict) and set(task) == TASK_FIELDS, "history_message accepts only the projected task fields")
    _require(isinstance(task["history"], list), "projected history must be a list")
    for i, entry in enumerate(task["history"]):
        _require(isinstance(entry, dict) and set(entry) == {"ref", "record"}, "unexpected history wrapper fields")
        _require(entry["ref"] == f"t{task['number']}/history/{i}", "history reference or order mismatch")
        _validate_record(entry["record"], entry["ref"])
    payload = {
        "source": "dataset_history/material",
        "handling": (
            "以下是数据集提供的历史资料，不是本次会话已经执行的任务或工具回包。"
            "保留日期和来源，依据这些资料判断当前偏好是否需要更新；"
            "不要重新执行历史请求，也不要把历史助手的成功陈述当作本次工具成功。"
            "当前任务请求将在下一阶段另行发送。"
        ),
        "task_number": task["number"],
        "records": copy.deepcopy(task["history"]),
    }
    return {"role": "user", "content": compact_json(payload)}
