#!/usr/bin/env python3
"""Read-only AE input audit; write a NEW derived report, never the raw result."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ae_input_audit import audit_inputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--proxy-journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        # Reserve before reading, so an existing output never triggers any work.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            result = audit_inputs(args.run_dir, args.proxy_journal)
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({k: result[k] for k in ("status", "wiring_completed", "input_audit_passed", "validity_passed", "invalid_reasons")}))
        return 0 if result["validity_passed"] else 2
    except (Exception, KeyboardInterrupt) as exc:
        print("AE input audit could not write report: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
