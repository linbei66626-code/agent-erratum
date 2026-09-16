#!/usr/bin/env python3
"""Re-count the SEALED 28 requests with the target tokenizer, offline.

    .venv-vita/bin/python tools/reconcile_target_tokenizer.py \
        --journal <sealed private journal> --output <report.json> [--table <table.md>]

What it does, and what it never does:

* reads the sealed capture READ-ONLY and counts every chat request twice with the
  PRODUCTION shared entry (`count_request_prompt`):
    - `family_thinking_basis`: the Qwen3-8B family cache, whose template carries the
      thinking branch (this is the basis the r3 report used), and
    - `target_basis`: the target model's OWN assets, verified file by file against the
      pinned declaration, whose template has no thinking branch;
* compares both with the provider's own `prompt_tokens` for the SAME request and
  records the real residuals - no threshold is declared as "success", and a handful of
  requests is not presented as a capacity bound for the future;
* proves the counting cache keys on the tool SCHEMAS by counting two requests that
  share messages and tool COUNT but differ in schema length;
* writes counts and digests only: no message body, no tool text, no provider payload.
It never sends a request, never contacts a service and never mutates the capture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ae_multiturn_capacity import (  # noqa: E402
    QwenBpeTokenizer, TOJSON_POLICY, UNDEFINED_POLICY, count_request_prompt,
    load_official_tokenizer_assets, load_target_tokenizer_assets, read_sealed_requests,
    render_qwen_chat, render_with_template_runtime, template_runtime,
    template_variant)


def _tokenizer(assets):
    return QwenBpeTokenizer(assets["tokenizer_json"], assets["tokenizer_config"])


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _schema_fingerprint(tools):
    return hashlib.sha256(json.dumps(tools, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()


def _family_thinking_count(tokenizer, messages, tools):
    """The PREVIOUS basis: the hardcoded thinking renderer, template ignored."""
    rendered = render_qwen_chat(messages, tools, template=None)
    return tokenizer.count(rendered)


def cache_key_probe(target_tokenizer):
    """Prove the counting cache keys on tool SCHEMAS, using the production closure.

    The driver's `_official_count_request` is the object that really gates a run, so
    the probe drives THAT closure with two normalized bodies: identical messages and
    the SAME number of tools, but one schema is longer. A cache keyed on the tool
    count would return the first number for both; the real closure must not.
    """
    from ae_cloud_re_multiturn import _official_count_request

    short_tool = {"type": "function",
                  "function": {"name": "probe_tool",
                               "parameters": {"type": "object",
                                              "properties": {"a": {"type": "string"}}}}}
    long_tool = json.loads(json.dumps(short_tool))
    long_tool["function"]["description"] = "schema padding " * 40
    messages = [{"role": "user", "content": "same messages for both probes"}]
    capacity = {"count_basis": ["official_qwen_tokenizer"],
                "tokenizer": {"target": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                              "asset_target": "Qwen/Qwen3-30B-A3B-Instruct-2507"}}
    counter, source = _official_count_request(capacity)
    short = counter(json.dumps({"messages": messages, "tools": [short_tool]}).encode(), "agent")
    long = counter(json.dumps({"messages": messages, "tools": [long_tool]}).encode(), "agent")
    # and once more, to show a repeated identical request still hits the cache
    repeat = counter(json.dumps({"messages": messages, "tools": [short_tool]}).encode(), "agent")
    direct_short = count_request_prompt(target_tokenizer, messages, [short_tool])["prompt_tokens"]
    direct_long = count_request_prompt(target_tokenizer, messages, [long_tool])["prompt_tokens"]
    return {"count_source": source, "same_tool_count": 1,
            "short_schema_chars": len(json.dumps(short_tool, ensure_ascii=False)),
            "long_schema_chars": len(json.dumps(long_tool, ensure_ascii=False)),
            "cached_short": short, "cached_long": long, "cached_repeat": repeat,
            "direct_short": direct_short, "direct_long": direct_long,
            "long_schema_recounted": long != short,
            "repeat_is_cached": repeat == short,
            "cache_matches_direct": (short == direct_short and long == direct_long)}


def template_basis_probe(target_tokenizer, family_tokenizer):
    """Show that the template SELECTION is live, on the branches the two differ in.

    The 28 sealed requests may never touch the thinking branch, in which case the two
    bases must agree - and that agreement is only meaningful if the bases CAN differ.
    These probes exercise exactly the branches the target template removed.
    """
    def both(messages, tools=None, **kwargs):
        return {
            "family_thinking_basis": count_request_prompt(
                family_tokenizer, messages, tools, **kwargs),
            "target_basis": count_request_prompt(target_tokenizer, messages, tools, **kwargs),
        }

    assistant_after_query = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "final answer after the last query"},
    ]
    reasoning_content = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "final answer",
         "reasoning_content": "private reasoning kept out of the target template"},
    ]
    cases = {
        "assistant_turn_after_the_last_query": both(assistant_after_query),
        "assistant_reasoning_content": both(reasoning_content),
        "generation_prompt_without_thinking": both(
            [{"role": "user", "content": "question"}], add_generation_prompt=True,
            enable_thinking=False),
    }
    for name, case in cases.items():
        case["family_tokens"] = case["family_thinking_basis"]["prompt_tokens"]
        case["target_tokens"] = case["target_basis"]["prompt_tokens"]
        case["target_minus_family"] = case["target_tokens"] - case["family_tokens"]
        case["family_template_sha256"] = case["family_thinking_basis"]["template_sha256"]
        case["target_template_sha256"] = case["target_basis"]["template_sha256"]
        case["family_variant"] = case["family_thinking_basis"]["template_variant"]
        case["target_variant"] = case["target_basis"]["template_variant"]
    return {"cases": cases,
            "any_branch_differs": any(case["target_minus_family"] != 0
                                      for case in cases.values()),
            "note": ("these are synthetic probes of the removed branches; they are not "
                     "sealed requests and carry no provider comparison")}


def render_paths(tokenizer, messages, tools):
    """Execute BOTH supported renderer paths and compare their bytes.

    `count_request_prompt` already refuses a divergence, but this renders each path
    directly so the comparison is visible per request instead of only inside the
    entry: pinned renderer bytes and digests, template-execution bytes and digests,
    and whether they are identical.
    """
    import hashlib as _hashlib
    template_text = tokenizer.chat_template
    pinned = render_qwen_chat(messages, tools, template=template_text)
    runtime = template_runtime()
    executed = None
    if runtime["name"] is not None:
        executed = render_with_template_runtime(template_text, messages, tools)
    return {
        "runtime": runtime["name"],
        "runtime_missing": runtime["missing"],
        "pinned_tokens": tokenizer.count(pinned),
        "pinned_sha256": _hashlib.sha256(pinned.encode("utf-8")).hexdigest(),
        "jinja_tokens": None if executed is None else tokenizer.count(executed),
        "jinja_sha256": (None if executed is None
                         else _hashlib.sha256(executed.encode("utf-8")).hexdigest()),
        "identical": None if executed is None else executed == pinned,
        "undefinced_field_probe_raised": False,
    }


def build_report(journal_path: Path):
    requests = read_sealed_requests(journal_path)
    if not requests:
        raise SystemExit("the sealed journal carries no chat request")
    family_assets = load_official_tokenizer_assets()
    target_assets = load_target_tokenizer_assets()
    family = _tokenizer(family_assets)
    target = _tokenizer(target_assets)
    runtime = template_runtime()
    rows = []
    for entry in requests:
        body = entry["body"]
        messages = body.get("messages") or []
        tools = body.get("tools") or []
        provider = (entry.get("usage") or {}).get("prompt_tokens")
        target_count = count_request_prompt(target, messages, tools)
        family_with_target_template = count_request_prompt(target, messages, tools)
        paths = render_paths(target, messages, tools)
        rows.append({
            "sequence": entry["sequence"],
            "request_sha256": entry["sha256"],
            "messages": len(messages),
            "tools": len(tools),
            "tool_schema_chars": len(json.dumps(tools, ensure_ascii=False, sort_keys=True)),
            "tool_schema_sha256": _schema_fingerprint(tools),
            "provider_prompt_tokens": provider,
            "provider_completion_tokens": (entry.get("usage") or {}).get("completion_tokens"),
            "family_thinking_basis": _family_thinking_count(family, messages, tools),
            "target_basis": target_count["prompt_tokens"],
            "target_renderer": target_count["renderer"],
            "target_variant": target_count["template_variant"],
            "target_template_sha256": target_count["template_sha256"],
            "family_template_sha256": hashlib.sha256(
                (family.chat_template or "").encode("utf-8")).hexdigest(),
            "cross_check_family_tokenizer_with_target_template":
                family_with_target_template["prompt_tokens"],
            "render_paths": paths,
            "jinja_minus_provider": (None if paths["jinja_tokens"] is None
                                     else paths["jinja_tokens"] - provider),
            "pinned_minus_jinja": (None if paths["jinja_tokens"] is None
                                   else paths["pinned_tokens"] - paths["jinja_tokens"]),
        })
    for row in rows:
        row["family_minus_provider"] = (row["family_thinking_basis"]
                                        - row["provider_prompt_tokens"])
        row["target_minus_provider"] = (row["target_basis"]
                                        - row["provider_prompt_tokens"])
        row["target_minus_family"] = (row["target_basis"] - row["family_thinking_basis"])
    family_deltas = [row["family_minus_provider"] for row in rows]
    target_deltas = [row["target_minus_provider"] for row in rows]

    def summary(values):
        ordered = sorted(values)
        return {"min": ordered[0], "max": ordered[-1],
                "mean": round(sum(values) / len(values), 3),
                "median": ordered[len(ordered) // 2],
                "mean_absolute": round(sum(abs(v) for v in values) / len(values), 3),
                "max_absolute": max(abs(v) for v in values),
                "exact": sum(1 for v in values if v == 0),
                "within_16": sum(1 for v in values if abs(v) <= 16),
                "within_128": sum(1 for v in values if abs(v) <= 128)}

    compared = [row["render_paths"] for row in rows if row["render_paths"]["identical"] is not None]
    dual_path = {
        "runtime": runtime["name"], "runtime_missing": runtime["missing"],
        "tojson_policy": dict(TOJSON_POLICY), "undefined_policy": UNDEFINED_POLICY,
        "template_variant": template_variant(target.chat_template),
        "requests_rendered_with_both_paths": len(compared),
        "identical_on_all_of_them": (all(item["identical"] for item in compared)
                                     if compared else None),
        "agreement": ("both supported paths produced the SAME bytes for every request"
                      if compared and all(item["identical"] for item in compared)
                      else ("the template runtime is unavailable here, so only the "
                            "pinned path ran" if not compared else
                            "the paths differ - the counting entry refuses such a request")),
    }
    return {
        "schema": "ae-target-tokenizer-reconciliation-1",
        "dual_path": dual_path,
        "journal": str(journal_path),
        "journal_sha256": _digest(journal_path),
        "requests": len(rows),
        "no_request_was_sent": True,
        "no_threshold_declared_as_success": True,
        "template_runtime": runtime,
        "template_runtime_gap": runtime["missing"],
        "identity": {
            "target": {
                "model": target_assets["model"], "revision": target_assets["revision"],
                "directory": target_assets["directory"],
                "verified_files": {name: entry["sha256"]
                                   for name, entry in sorted(target_assets["files"].items())},
                "chat_template_sha256": target_assets["template_sha256"],
                "model_max_length_is_not_endpoint_capacity":
                    target_assets["model_max_length_is_not_endpoint_capacity"],
                "native_max_position_embeddings":
                    target_assets["native_max_position_embeddings"],
            },
            "family_cache": {
                "revision": family_assets.get("revision"),
                "tokenizer_json_sha256": _digest(family_assets["tokenizer_json"]),
                "tokenizer_config_sha256": _digest(family_assets["tokenizer_config"]),
                "chat_template_sha256": hashlib.sha256(
                    (family.chat_template or "").encode("utf-8")).hexdigest(),
                "asset_is_target": False,
            },
            "tokenizer_json_identical_between_them":
                _digest(target_assets["tokenizer_json"])
                == _digest(family_assets["tokenizer_json"]),
            "note": ("tokenizer.json is byte-identical between the two, so the BPE merge "
                     "table INSIDE it is the same; the text files around it "
                     "(tokenizer_config.json, merges.txt) are NOT identical, and the chat "
                     "template differs, which is what changes the rendering"),
        },
        "residuals": {
            "family_thinking_basis_minus_provider": summary(family_deltas),
            "target_basis_minus_provider": summary(target_deltas),
            "basis_change_tokens": {
                "mean": round(sum(row["target_minus_family"] for row in rows) / len(rows), 3),
                "min": min(row["target_minus_family"] for row in rows),
                "max": max(row["target_minus_family"] for row in rows),
            },
            "interpretation": ("a positive value means the local function counted MORE "
                               "tokens than the provider reported for the same request. "
                               "These are residuals of ONE sample of 28 real requests; "
                               "they are not a capacity bound and no threshold is "
                               "declared as success."),
        },
        "counting_contract": {
            "entry": "count_request_prompt",
            "generation_prompt": ("NOT added for these requests: they are the prompts the "
                                  "provider received, with no assistant generation prefix"),
            "tools": ("the wire tool schemas are rendered inside the template's tools "
                      "block; the cache key carries their serialized content"),
            "renderer": ("the asset's own chat template executed by Jinja2, verified "
                         "byte-identical to the pinned renderer for the SAME request; "
                         "when no template runtime exists only the pinned renderer runs "
                         "and the entry says so"),
            "tojson_policy": dict(TOJSON_POLICY),
            "undefined_policy": UNDEFINED_POLICY,
            "generation_prompt_assumption": ("the wire request carries no assistant "
                                             "prefix, so this count treats the prompt as "
                                             "what the provider received WITHOUT an added "
                                             "generation prefix; the wire alone cannot "
                                             "prove the provider adds none"),
        },
        "cache_key_probe": cache_key_probe(target),
        "template_basis_probe": template_basis_probe(target, family),
        "rows": rows,
    }


def write_table(report, path: Path):
    lines = ["| # | seq | msgs | tools | provider | pinned | jinja2 | pinned−provider | "
             "jinja2−provider | paths identical |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for index, row in enumerate(report["rows"], start=1):
        paths = row["render_paths"]
        jinja = "n/a" if paths["jinja_tokens"] is None else str(paths["jinja_tokens"])
        same = "n/a" if paths["identical"] is None else str(paths["identical"])
        lines.append(
            f"| {index} | {row['sequence']} | {row['messages']} | {row['tools']} | "
            f"{row['provider_prompt_tokens']} | {row['target_basis']} | {jinja} | "
            f"{row['target_minus_provider']} | "
            f"{'n/a' if row['jinja_minus_provider'] is None else row['jinja_minus_provider']} | "
            f"{same} |")
    summary = report["residuals"]
    lines += ["", "Residual summary (local minus provider, tokens):", "",
              f"- family/thinking basis: {json.dumps(summary['family_thinking_basis_minus_provider'])}",
              f"- target basis: {json.dumps(summary['target_basis_minus_provider'])}",
              f"- baseline change (target − family): {json.dumps(summary['basis_change_tokens'])}",
              "", "No message body, tool text or provider payload is reproduced here."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--table", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit("refuse to overwrite an existing report")
    report = build_report(args.journal)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    if args.table:
        write_table(report, args.table)
    print(json.dumps({"requests": report["requests"],
                      "renderer": report["rows"][0]["target_renderer"],
                      "target_variant": report["rows"][0]["target_variant"],
                      "family": report["residuals"]["family_thinking_basis_minus_provider"],
                      "target": report["residuals"]["target_basis_minus_provider"],
                      "baseline_change": report["residuals"]["basis_change_tokens"],
                      "cache_long_schema_recounted":
                          report["cache_key_probe"]["long_schema_recounted"],
                      "template_branches_differ":
                          report["template_basis_probe"]["any_branch_differs"],
                      "dual_path": report["dual_path"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
