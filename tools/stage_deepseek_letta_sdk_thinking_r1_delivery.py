#!/usr/bin/env python3
"""Stage the Letta thinking-over-the-SDK delivery r1.

    .venv-vita/bin/python tools/stage_deepseek_letta_sdk_thinking_r1_delivery.py

Baselines are the previous round's delivery copies (that round shipped the patch, both
manifests and the acceptance suite) plus a pre-image RECONSTRUCTED by reversing this
round's edit to the service-gate probe harness. The output directory is exclusive.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREV = ROOT / "results/ae-deepseek-letta-thinking-r1"
OUT = ROOT / "results/ae-deepseek-letta-sdk-thinking-r1"
PROBE = ROOT / "tests/test_ae_no_compaction.py"

FILES = [
    ("deployment_ae-no-compaction-letta.patch",
     "deployment-assets/letta-no-compaction/ae-no-compaction-letta.patch"),
    ("deployment_stack-manifest.json",
     "deployment-assets/letta-no-compaction/stack-manifest.json"),
    ("deployment_manifest.json",
     "deployment-assets/letta-no-compaction/manifest.json"),
    ("tests_test_ae_deepseek_letta_thinking.py",
     "tests/test_ae_deepseek_letta_thinking.py"),
    ("tests_test_ae_no_compaction.py", "tests/test_ae_no_compaction.py"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reconstruct_test_ae_no_compaction(text: str) -> str:
    """Undo this round's binding of the new `_ae_wire_body` in the gate probe harness."""
    new = '''    for name in ("_ae_primary_count", "_ae_tokenizer_identity", "_ae_pinned_count",
                 "_ae_byte_gate_policy", "_ae_wire_body", "_ae_request_bytes"):'''
    old = '''    for name in ("_ae_primary_count", "_ae_tokenizer_identity", "_ae_pinned_count",
                 "_ae_byte_gate_policy", "_ae_request_bytes"):'''
    if text.count(new) != 2:
        raise SystemExit("the recorded harness edit is not present exactly twice")
    text = text.replace(new, old)
    note_new = '''    # drives the production logic, not a lookalike. `_ae_wire_body` merges the SDK's
    # `extra_body` carrier so the byte bound is measured on the body really sent. With no
    # `AE_BYTE_GATE_*` declaration the byte policy reports itself undeclared, which is
    # exactly the Qwen path below.'''
    note_old = '''    # drives the production logic, not a lookalike. With no `AE_BYTE_GATE_*` declaration
    # the byte policy reports itself undeclared, which is exactly the Qwen path below.'''
    if text.count(note_new) != 1:
        raise SystemExit("the recorded harness note is not present exactly once")
    return text.replace(note_new, note_old)


def baseline_for(name: str):
    if name == "tests_test_ae_no_compaction.py":
        text = PROBE.read_text(encoding="utf-8")
        return (reconstruct_test_ae_no_compaction(text).encode("utf-8"),
                "reconstructed pre-image",
                "this round's harness edit reversed, guarded by an exact-match assertion")
    path = PREV / "after" / name
    if not path.is_file():
        raise SystemExit(f"the previous delivery has no {name!r} pre-image")
    return path.read_bytes(), "previous round's delivery copy", (
        "the ae-deepseek-letta-thinking-r1 delivery's own after/ copy")


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
        diff_path.write_text("".join(difflib.unified_diff(
            before.decode("utf-8").splitlines(keepends=True),
            after.decode("utf-8").splitlines(keepends=True),
            fromfile=f"before/{name}", tofile=f"after/{name}")), encoding="utf-8")
        table.append({"file": name, "repo_path": source,
                      "before_sha256": hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(path), "changed": before != after,
                      "baseline_kind": baseline_kind, "baseline": note,
                      "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-deepseek-letta-sdk-thinking-diff-1",
         "note": ("The previous round put the declared `thinking` field at the TOP LEVEL of "
                  "the Python request, and the deployed service then failed with "
                  "`AsyncCompletions.create() got an unexpected keyword argument 'thinking'` "
                  "before anything was sent (run ae-deepseek-re-live-20260916-r2; the proxy "
                  "saw only the /models listing). This round keeps the declaration, the "
                  "model binding and the conflict refusals, and moves the field into the "
                  "`extra_body` mapping the pinned OpenAI SDK turns into top-level JSON; the "
                  "send path is the REAL `OpenAIClient.request_async` with the real "
                  "`openai.AsyncOpenAI` client, and only its HTTP transport is replaced by "
                  "an offline capture. The pre-send byte bound is now measured on the MERGED "
                  "wire body (`_ae_wire_body`), so the gate counts exactly the bytes the SDK "
                  "serializes - never the Python `extra_body` wrapper - and an existing "
                  "`extra_body` keeps its other entries. The proxy's required-field check is "
                  "not relaxed, the historical Qwen path is byte-identical, and no prompt, "
                  "R/E, capacity, budget or experiment definition is touched. A service-side "
                  "patch change means: rebuild the stack, restart the service, and let the "
                  "new instance write a NEW receipt."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} ({row['baseline_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
