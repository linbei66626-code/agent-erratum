#!/usr/bin/env python3
"""The opt-in `t4-interface-clarified-v1` case variant: exactly two public clarifications.

This is a POST-HOC EXPLORATORY CONTROL, not the original case. The original default path in
`scripts/ae_01_local_t4_probe.py` and `scripts/ae_01_cloud_t4_probe.py` is untouched and
still builds its own messages/schemas; a variant is constructed separately here and is only
used when an entry point is explicitly asked for it.

The two interventions, and nothing else:

1. `complete_environment_time` - the agent is told the FULL current time, taken from the
   SAME clock the native environment uses to validate `dispatch_time`
   (`environment.tools.get_now(...)`, which is the task database's own time). It is not the
   host wall clock, not a hardcoded 14:30 and not read from any private评分 material. If that
   clock cannot be read, the variant REFUSES to build rather than guessing.
   This fact is NEW PUBLIC ENVIRONMENT INFORMATION: the original input did not have it.

2. `attributes_parameter_semantics` - a general clarification appended to the
   `create_delivery_order` `attributes` description. It names no product, no store and no
   sugar value, does not make the parameter required, does not change the execution default
   (an omitted `attributes` still yields an empty order spec) and never fills arguments in
   for the model.

BOTH factors are changed together, so a result cannot be attributed to either one alone.
"""
from __future__ import annotations

import copy
import json

VARIANT_ID = "t4-interface-clarified-v1"
CLOCK_FORMAT = "%Y-%m-%d %H:%M:%S"

#: A general parameter-semantics note. It deliberately contains no oracle value.
ATTRIBUTES_CLARIFICATION = (
    "商品目录中的规格不会自动写入订单；若用户有规格要求，必须显式传入 attributes，"
    "按 product_ids 的顺序逐项对应，取值依据实际商品查询回包；省略时订单规格为空。"
)

#: The exact markers the ORIGINAL variant must NOT carry, because they would be untrue here.
SUPERSEDED_MARKERS = ("same_as_original_input", "post_hoc_reminder_unused",
                      "no_post_hoc_specification_reminder")


class VariantRejected(RuntimeError):
    """The variant cannot be built honestly (for example: no readable environment clock)."""


def environment_now(environment) -> str:
    """The native environment's own current time, in the format the tools validate."""
    tools = getattr(environment, "tools", None)
    getter = getattr(tools, "get_now", None)
    if not callable(getter):
        raise VariantRejected(
            "the native environment exposes no get_now; the environment clock cannot be "
            "read, so the time clarification cannot be built (nothing is guessed here)")
    value = getter(CLOCK_FORMAT)
    if not isinstance(value, str) or not value.strip():
        raise VariantRejected(f"the native environment clock returned {value!r}")
    return value.strip()


def time_fact(environment) -> dict:
    """The added public fact, with its source and its actual value recorded."""
    return {
        "environment_now": environment_now(environment),
        "format": CLOCK_FORMAT,
        "source": "environment.tools.get_now('%Y-%m-%d %H:%M:%S') -> the task database time",
        "same_clock_the_tools_validate_with": True,
        "host_wall_clock_used": False,
        "hardcoded_value_used": False,
        "read_from_private_scoring": False,
        "was_visible_in_the_original_input": False,
        "is_new_public_environment_information": True,
    }


def clarified_system_message(base_system: str, environment) -> tuple[str, dict]:
    """The default agent system message plus the full environment time."""
    fact = time_fact(environment)
    addition = (
        "\n# 当前时间\n"
        f"- 当前时间：{fact['environment_now']}（环境时钟，{CLOCK_FORMAT}）\n"
        "- 该时间与环境工具校验 dispatch_time 使用的是同一个时钟；"
        "提交的配送时间必须晚于该时刻。\n"
    )
    return base_system + addition, fact


def clarified_tools(tools: list, environment=None) -> tuple[list, dict]:
    """DEEP-COPIED tool list with the attributes note appended.

    `tools` is copied, so neither the caller's list nor any default-path schema is touched,
    and a second run cannot inherit the note.
    """
    cloned = copy.deepcopy(tools)
    changed = None
    for tool in cloned:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or function.get("name") != "create_delivery_order":
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        if not isinstance(properties, dict) or "attributes" not in properties:
            continue
        attribute = properties["attributes"]
        if not isinstance(attribute, dict):
            continue
        original = attribute.get("description")
        if isinstance(original, str) and ATTRIBUTES_CLARIFICATION in original:
            # idempotent: applying the variant twice does not duplicate the note
            changed = {"appended": False, "reason": "already present"}
            break
        attribute["description"] = ((original + "\n\n") if isinstance(original, str)
                                    and original.strip() else "") + ATTRIBUTES_CLARIFICATION
        changed = {"appended": True, "original_description": original,
                   "clarification": ATTRIBUTES_CLARIFICATION,
                   "parameter_now_required": "attributes" in (parameters.get("required") or []),
                   "execution_default_changed": False,
                   "oracle_value_included": False,
                   "product_or_store_named": False}
        break
    if changed is None:
        raise VariantRejected(
            "no create_delivery_order tools schema with an attributes property was found; "
            "the schema must be inspected rather than assumed")
    return cloned, changed


def variant_record(environment, tools: list) -> dict:
    """What this variant is, what it added, and which old labels it must NOT inherit."""
    _cloned, schema_change = clarified_tools(tools, environment)
    return {
        "case_variant": VARIANT_ID,
        "opt_in": True,
        "default_path_unchanged": True,
        "interventions": {
            "complete_environment_time": time_fact(environment),
            "attributes_parameter_semantics": schema_change,
        },
        "two_factors_are_changed_together": True,
        "can_attribute_to_one_factor_alone": False,
        "post_hoc_exploratory_control": True,
        "is_the_original_benchmark": False,
        "is_r_e": False,
        "superseded_markers_not_inherited": list(SUPERSEDED_MARKERS),
        "attributes_note_is_one_intervention_not_a_model_answer": True,
    }


def build_variant(environment, base_system: str, tools: list) -> dict:
    """Everything an entry point needs for one variant request, in one call."""
    system, fact = clarified_system_message(base_system, environment)
    cloned, schema_change = clarified_tools(tools, environment)
    return {"system_message": system,
            "tools": cloned,
            "time_fact": fact,
            "schema_change": schema_change,
            "record": variant_record(environment, tools)}


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
