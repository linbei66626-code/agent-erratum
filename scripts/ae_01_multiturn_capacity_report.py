#!/usr/bin/env python3
"""Offline capacity pre-check for the original t4->t12 multi-turn R/E pair.

    scripts/ae_01_multiturn_capacity_report.py \\
      --journal <sealed *.private.jsonl> \\
      --dataset <tasks-full-a4553e1.json> \\
      --config  <the run's config> \\
      --output  <new report path>

Writes ONE new JSON report and nothing else. It never contacts a service, never
calls a model, never re-sends a sealed request and never downloads a tokenizer:
the official Qwen tokenizer assets are read from the local cache, and a missing
asset is reported as a gap instead of being replaced by another model's tokenizer.
Exit code 0 means the report was written; the report itself carries the verdict.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ae_inputs import prepare_sample  # noqa: E402
from ae_multiturn_capacity import (QwenBpeTokenizer, build_capacity_report,  # noqa: E402
                                   load_official_tokenizer_assets)


def load_summary(path):
    candidate = Path(path)
    return json.loads(candidate.read_text(encoding="utf-8")) if candidate.is_file() else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True,
                        help="the sealed run's PRIVATE proxy capture (read-only)")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, default=None,
                        help="the finished-evidence verified-summary.json")
    parser.add_argument("--capacity-check", type=Path, default=None,
                        help="the finished-evidence capacity-check.json")
    parser.add_argument("--measured-material", type=Path, default=None,
                        help="the pinned per-task material measurement "
                             "(tools/measure_multiturn_material.py output)")
    parser.add_argument("--tokenizer-json", type=Path, default=None)
    parser.add_argument("--tokenizer-config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("the capacity report refuses to overwrite an existing output", file=sys.stderr)
        return 2
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        if args.tokenizer_json and args.tokenizer_config:
            assets = {"source": "explicit_paths",
                      "tokenizer_json": args.tokenizer_json,
                      "tokenizer_config": args.tokenizer_config, "revision": None,
                      "snapshot": str(args.tokenizer_json.parent)}
        else:
            assets = load_official_tokenizer_assets()
        tokenizer = QwenBpeTokenizer(assets["tokenizer_json"], assets["tokenizer_config"])
        sample = prepare_sample(args.dataset)
        measured = (json.loads(args.measured_material.read_text(encoding="utf-8"))
                    if args.measured_material else None)
        if measured is not None:
            measured.setdefault("source", str(args.measured_material))
        report = build_capacity_report(
            args.journal, args.dataset, config_path=args.config, prepared=sample,
            tokenizer=tokenizer, measured_material=measured,
            tokenizer_assets={key: (str(value) if isinstance(value, Path) else value)
                              for key, value in assets.items()},
            sealed_summary=load_summary(args.summary) if args.summary else None,
            sealed_capacity=load_summary(args.capacity_check) if args.capacity_check else None)
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    verdict = report["verdict"]
    print(json.dumps({"requests": report["layer_2_sealed_requests"]["agreement"],
                      "fits_64k_in_every_case": verdict["fits_64k_in_every_case"],
                      "fits_64k_in_every_case_above_the_trigger":
                          verdict["fits_64k_in_every_case_above_the_trigger"],
                      "by_capacity": {key: {
                          "fits_in_cases": value["fits_in_cases"],
                          "over_the_hard_window_at": value["over_the_hard_window_at"],
                          "over_the_compaction_trigger_at":
                              value["over_the_compaction_trigger_at"]}
                          for key, value in verdict["by_capacity"].items()}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
