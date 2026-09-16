#!/usr/bin/env python3
"""Report the tokenizer assets this project actually uses, and what it lacks.

The capacity work must not claim the TARGET model's official tokenizer when only a
sibling's assets are present. This tool records, from the local cache only:

* which tokenizer files exist, their digests, the revision they came from and where
  HuggingFace says they came from;
* whether an asset for the target model (`Qwen/Qwen3-30B-A3B-Instruct-2507`) is
  present, and if not, exactly what is missing, where it would come from and how
  large it is;
* the observed local/provider agreement per request, so "the counter is calibrated
  for these samples" is never confused with "the assets are the target model's".

Offline: it reads the local cache and the sealed capture; it downloads nothing.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ae_multiturn_capacity import (QwenBpeTokenizer, load_official_tokenizer_assets,  # noqa: E402
                                   read_sealed_requests)

TARGET = "Qwen/Qwen3-30B-A3B-Instruct-2507"
SEALED = ("transfers/ae-multiturn-live-20260914-r1/finished-evidence/deployment/runs/"
          "ae-cloud-re-multiturn-original-20260914-r1.private.jsonl")
#: The files a target-model tokenizer would need, with their approximate published
#: sizes. Only `tokenizer.json`, `tokenizer_config.json`, `vocab.json` and `merges.txt`
#: are needed by this project's counter; the weights are NOT (and are never fetched).
NEEDED = (("tokenizer.json", "~11.4 MB"), ("tokenizer_config.json", "~10 KB"),
          ("vocab.json", "~2.8 MB"), ("merges.txt", "~1.7 MB"))


def digest(path: Path):
    raw = path.read_bytes()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def main():
    hub = Path.home() / ".cache/huggingface/hub"
    report = {"target_model": TARGET, "hub": str(hub), "cached_models": [], "gaps": []}
    for entry in sorted(hub.glob("models--*")):
        name = entry.name.replace("models--", "").replace("--", "/")
        snapshot = sorted((entry / "snapshots").glob("*")) if (entry / "snapshots").is_dir() else []
        files = {}
        for relative in ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt"):
            candidate = (snapshot[-1] / relative) if snapshot else None
            if candidate is not None and candidate.is_file():
                files[relative] = digest(candidate)
        report["cached_models"].append({"repo": name, "snapshot": str(snapshot[-1]) if snapshot else None,
                                        "files": files})
    target_present = any(item["repo"] == TARGET for item in report["cached_models"])
    report["target_assets_present"] = target_present
    if not target_present:
        report["gaps"].append({
            "item": f"the official tokenizer assets of {TARGET}",
            "where": f"https://huggingface.co/{TARGET} (tokenizer files only; the project "
                     "never fetches model weights)",
            "needed_files": [{"name": name, "approx_size": size} for name, size in NEEDED],
            "why_it_matters": ("the local counter runs on the Qwen3 family tokenizer assets "
                               "present in the cache; the target model's own tokenizer.json "
                               "would prove the two are byte-identical rather than inferring "
                               "it from the family and from the observed agreement"),
            "affected_acceptance": ["layer-1/3 counts (fixed material and projections)",
                                    "the request-time capacity boundary"],
        })
    assets = load_official_tokenizer_assets()
    tokenizer = QwenBpeTokenizer(assets["tokenizer_json"], assets["tokenizer_config"])
    report["counter_assets"] = {
        "snapshot": assets["snapshot"], "revision": assets["revision"],
        "tokenizer_json": digest(Path(assets["tokenizer_json"])),
        "tokenizer_config": digest(Path(assets["tokenizer_config"])),
        "model_max_length": tokenizer.model_max_length,
        "chat_template_sha256": hashlib.sha256(
            (tokenizer.chat_template or "").encode("utf-8")).hexdigest(),
        "vocab_entries": len(tokenizer.vocab), "merge_rules": len(tokenizer.merges),
    }
    # Observed agreement on the sealed samples: a calibration observation only.
    journal = ROOT / SEALED
    rows = []
    if journal.is_file():
        for entry in read_sealed_requests(journal):
            body = entry["body"]
            messages = body.get("messages") or []
            tools = body.get("tools") or []
            from ae_multiturn_capacity import render_qwen_chat
            rendered = render_qwen_chat(messages, tools, add_generation_prompt=False)
            local = tokenizer.count(rendered)
            provider = (entry.get("usage") or {}).get("prompt_tokens")
            rows.append({"sequence": entry["sequence"], "messages": len(messages),
                         "tools": len(tools), "rendered_prompt_tokens": local,
                         "provider": provider,
                         "delta": None if provider is None else local - provider})
        deltas = [row["delta"] for row in rows if row["delta"] is not None]
        tool_bearing = [row["delta"] for row in rows if row["tools"]]
        tool_free = [row["delta"] for row in rows if not row["tools"]]
        report["observed_agreement"] = {
            "requests": len(rows), "comparable": len(deltas),
            "delta_min": min(deltas), "delta_max": max(deltas),
            "delta_mean": round(sum(deltas) / len(deltas), 2),
            "delta_range_with_tools": [min(tool_bearing), max(tool_bearing)] if tool_bearing else None,
            "delta_range_without_tools": [min(tool_free), max(tool_free)] if tool_free else None,
            "isolation": ("the per-request deltas are reported WITH and WITHOUT tool schemas "
                          "so the residual is not attributed to one cause without evidence"),
            "rows": rows,
            "claim": ("this is a CALIBRATION observation on 28 sealed requests of the same "
                      "model family; it is not proof that the target model's assets are "
                      "byte-identical, and it is not a mathematical bound"),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
