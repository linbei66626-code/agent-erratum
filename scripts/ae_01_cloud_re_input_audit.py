#!/usr/bin/env python3
"""Read-only AE-01 cloud R/E pair input audit; writes ONE new report, never overwrites.

    scripts/ae_01_cloud_re_input_audit.py \\
      --run-dir <captured pair run dir> \\
      --proxy-journal <closed cloud proxy journal> \\
      --output <new report path>

The output path is reserved exclusively before any file is read, so an existing
report can never be overwritten or resumed. Exit code 0 means
`input_audit_passed`; anything else is a bounded failure with a gate code and no
credential or message body echoed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ae_cloud_re_input_audit import audit_re_pair_inputs  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--proxy-journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path,
                        help="the RUN R/E capability config (the plan.json provenance config)")
    parser.add_argument("--dataset", type=Path, help="optional dataset file for provenance")
    args = parser.parse_args(argv)
    try:
        # Reserve before reading so an existing output never triggers any work.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("AE cloud R/E input audit refused to overwrite an existing output", file=sys.stderr)
        return 2
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        report = audit_re_pair_inputs(args.run_dir, args.proxy_journal,
                                      config_path=args.config, dataset_path=args.dataset)
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"status": report["status"],
                      "transport_capture_checked": report["transport_capture_checked"],
                      "input_audit_passed": report["input_audit_passed"],
                      "task_success": None, "scientific_result": None,
                      "invalid_reasons": report["invalid_reasons"]}, ensure_ascii=False))
    return 0 if report["input_audit_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
