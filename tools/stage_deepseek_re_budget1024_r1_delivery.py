#!/usr/bin/env python3
"""Stage the DeepSeek R/E 1024-request-budget delivery r1.

    .venv-vita/bin/python tools/stage_deepseek_re_budget1024_r1_delivery.py

The pre-images are the r3 delivery's own `after/` copies (that round shipped the four
production files) plus "new in this round" for the 1024 candidate and its suite. The
output directory is exclusive: an existing one is refused, and no earlier delivery is
touched.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R3 = ROOT / "results/ae-deepseek-re-multiturn-integration-r3"
OUT = ROOT / "results/ae-deepseek-re-budget1024-r1"

FILES = [
    ("configs_ae-01__re-multiturn__deepseek-flash.serial-budget1024-candidate.json",
     "configs/ae-01__re-multiturn__deepseek-flash.serial-budget1024-candidate.json"),
    ("ae_deepseek_re_transport.py", "ae_deepseek_re_transport.py"),
    ("ae_cloud_re_multiturn.py", "ae_cloud_re_multiturn.py"),
    ("ae_cloud_re_multiturn_input_audit.py", "ae_cloud_re_multiturn_input_audit.py"),
    ("scripts_ae_01_cloud_proxy.py", "scripts/ae_01_cloud_proxy.py"),
    ("tests_test_ae_deepseek_re_budget1024.py",
     "tests/test_ae_deepseek_re_budget1024.py"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def baseline_for(name: str):
    if name.startswith("configs_") or name.startswith("tests_test_ae_deepseek_re_budget1024"):
        return None, "new in this round", "no pre-image exists; first shipped here"
    path = R3 / "after" / name
    if not path.is_file():
        raise SystemExit(f"the r3 delivery has no {name!r} pre-image")
    return path.read_bytes(), "r3 delivery copy", "the r3 delivery's own after/ copy"


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists")
    for _name, source in FILES:
        if not (ROOT / source).is_file():
            raise SystemExit(f"the source file is absent: {source}")
    for directory in ("after", "diffs", "evidence"):
        (OUT / directory).mkdir(parents=True, exist_ok=False)
    table = []
    for index, (name, source) in enumerate(FILES, start=1):
        path = ROOT / source
        after = path.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        before, baseline_kind, note = baseline_for(name)
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if before is None:
            diff_path.write_text(f"# {name}: NEW in this round\n"
                                 f"# after sha256 {sha256(path)}\n", encoding="utf-8")
        else:
            diff_path.write_text("".join(difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}")), encoding="utf-8")
        table.append({"file": name, "repo_path": source,
                      "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(path), "changed": before != after,
                      "baseline_kind": baseline_kind, "baseline": note,
                      "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-deepseek-re-budget1024-diff-1",
         "note": ("The live run `ae-deepseek-re-live-20260916-r3` finished 13 of 18 phases "
                  "and was stopped by the LOCAL proxy at exactly 256 requests (255 "
                  "generation POSTs + 1 `/models` GET, then `proxy_stopped_or_request_limit`) "
                  "- not by the provider and not by the model. This round adds ONE reviewed "
                  "candidate with `max_requests: 1024` and makes that number travel the "
                  "whole chain: the 0.4 declaration contract accepts exactly {256, 1024} "
                  "(nothing defaults to 1024), the plan records the budget it will spend "
                  "and its source, the proxy that counts refuses to serve a candidate whose "
                  "budget differs from its own config, and the post-hoc audit re-derives the "
                  "budget from the plan, the proxy config and the capture itself. The "
                  "historical 256 candidate's bytes are untouched, the sealed 0.1/0.2/0.3 "
                  "protocols keep their own budget rule, the counter is not split (the GET "
                  "and every POST still share it), and no capacity, prompt, R/E or "
                  "experiment definition is changed."),
         "historical_256_candidate_unchanged": True,
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} ({row['baseline_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
