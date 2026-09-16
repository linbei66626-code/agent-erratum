#!/usr/bin/env python3
"""Stage the DeepSeek multi-turn R/E integration r3 delivery.

    .venv-vita/bin/python tools/stage_deepseek_re_integration_r3_delivery.py

This round ships ONLY the r2-audit corrections: the 0.4 audit branch made reachable
through the public entry over a complete real-byte fixture, the model-bound service byte
policy, and the corrected receipt narrative. It never touches an earlier delivery and
refuses to run if the output directory already exists.

Baselines are honest and per file (`baseline_kind` in before-after.json):

* `r2 delivery copy`          - the file's bytes in the r2 delivery (the r3 pre-image);
* `stored capture`            - the nearest stored copy, when the file was never shipped
                                in r2; a note says if earlier rounds' edits are included;
* `reconstructed pre-image`   - the recorded r3 edits reversed, guarded by an exact-match
                                assertion, for files with no stored copy of this era;
* `new in r3`                 - no pre-image exists.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R2 = ROOT / "results/ae-deepseek-re-multiturn-integration-r2"
CAPACITY_R2 = ROOT / "results/ae-multiturn-capacity-no-compaction-r2/after/tests"
STACK_R1 = ROOT / "results/ae-stack-receipt-wiring-r1/after"
OUT = ROOT / "results/ae-deepseek-re-multiturn-integration-r3"
COLLATERAL = "stored capture from an earlier round; its diff contains that round's edits too"

FILES = [
    # -- the r2 files this round changed -----------------------------------
    ("ae_deepseek_re_transport.py", "ae_deepseek_re_transport.py", ("r2", None)),
    ("ae_cloud_re_multiturn.py", "ae_cloud_re_multiturn.py", ("r2", None)),
    ("ae_cloud_proxy.py", "ae_cloud_proxy.py", ("r2", None)),
    ("ae_cloud_input_audit.py", "ae_cloud_input_audit.py", ("r2", None)),
    ("ae_cloud_re_multiturn_input_audit.py", "ae_cloud_re_multiturn_input_audit.py",
     ("r2", None)),
    # The proxy CLI is where the production process ARMS the policy from the declaration:
    # its byte branch changed after the capacity round, so it ships with that baseline.
    ("scripts_ae_01_cloud_proxy.py", "scripts/ae_01_cloud_proxy.py",
     ("stored", ROOT / "results/ae-multiturn-capacity-no-compaction-r3/after"
      / "scripts_ae_01_cloud_proxy.py", COLLATERAL)),
    ("scripts_ae_01_cloud_re_multiturn.py", "scripts/ae_01_cloud_re_multiturn.py",
     ("r2", None)),
    # The adapter's per-stage clarification guard: clearing it after a refused request
    # must not replace the real refusal with a bookkeeping error.
    ("ae_adapter.py", "ae_adapter.py", ("r2", None)),
    ("scripts_deployment_letta_local.py", "scripts/deployment/letta_local.py", ("r2", None)),
    ("configs_ae-01__re-multiturn__deepseek-flash.serial-candidate.json",
     "configs/ae-01__re-multiturn__deepseek-flash.serial-candidate.json", ("r2", None)),
    ("deployment_ae-no-compaction-letta.patch",
     "deployment-assets/letta-no-compaction/ae-no-compaction-letta.patch", ("r2", None)),
    ("deployment_ae_no_compaction.py",
     "deployment-assets/letta-no-compaction/files/letta/helpers/ae_no_compaction.py",
     ("r2", None)),
    ("deployment_stack-manifest.json",
     "deployment-assets/letta-no-compaction/stack-manifest.json", ("r2", None)),
    ("deployment_manifest.json",
     "deployment-assets/letta-no-compaction/manifest.json", ("r2", None)),
    ("tests_test_ae_deepseek_re_chain.py", "tests/test_ae_deepseek_re_chain.py",
     ("r2", None)),
    ("tests_test_ae_deepseek_re_transport_driver.py",
     "tests/test_ae_deepseek_re_transport_driver.py", ("r2", None)),
    # -- the audit modules the 0.4 evidence path had to be corrected in ----
    ("ae_cloud_audit.py", "ae_cloud_audit.py", ("reconstruct", "ae_cloud_audit")),
    ("ae_cloud_re_input_audit.py", "ae_cloud_re_input_audit.py",
     ("reconstruct", "ae_cloud_re_input_audit")),
    # -- the fixtures the 0.4 chain is built from --------------------------
    ("tests_re_multiturn_chain.py", "tests/re_multiturn_chain.py",
     ("stored", CAPACITY_R2 / "re_multiturn_chain.py", COLLATERAL)),
    ("tests_test_ae_cloud_re_multiturn.py", "tests/test_ae_cloud_re_multiturn.py",
     ("stored", CAPACITY_R2 / "test_ae_cloud_re_multiturn.py",
      "stored capture; verified to differ only by this round's edits")),
    ("tests_test_ae_cloud_re_pair.py", "tests/test_ae_cloud_re_pair.py",
     ("stored", CAPACITY_R2 / "test_ae_cloud_re_pair.py",
      "stored capture; verified to differ only by this round's edits")),
    ("tests_test_ae_stack_receipt_wiring.py", "tests/test_ae_stack_receipt_wiring.py",
     ("stored", STACK_R1 / "tests_test_ae_stack_receipt_wiring.py", COLLATERAL)),
    # The service-gate harness drives the REAL `_ae_capacity_gate`; the model-bound byte
    # policy it now calls first must be bound there too, or the Qwen path cannot be run.
    ("tests_test_ae_no_compaction.py", "tests/test_ae_no_compaction.py",
     ("stored", ROOT / "results/ae-multiturn-capacity-no-compaction-r3/after"
      / "tests_test_ae_no_compaction.py", COLLATERAL)),
    # -- new this round ----------------------------------------------------
    ("tests_test_ae_deepseek_re_audit.py", "tests/test_ae_deepseek_re_audit.py",
     ("new", None)),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(text: str, old: str) -> None:
    if text.count(old) != 1:
        raise SystemExit(f"the recorded r3 edit is not present exactly once: {old[:70]!r}")


def reconstruct_ae_cloud_audit(text: str) -> str:
    """Undo the r3 byte-gate handling in the shared transport walk."""
    new_import = """from ae_cloud_proxy import (BYTE_GATE_COUNT_BASIS, CloudConfig, PROFILE, COMPAT_PROFILE,
                           PROFILES, ORIGIN, normalize_request, profile_of,
                           response_summary, source_hashes)"""
    old_import = """from ae_cloud_proxy import (CloudConfig, PROFILE, COMPAT_PROFILE, PROFILES, ORIGIN,
                           normalize_request, profile_of, response_summary,
                           source_hashes)"""
    _require(text, new_import)
    text = text.replace(new_import, old_import)
    new_block = '''    if check.get("capacity_basis") == BYTE_GATE_COUNT_BASIS:
        # A byte-gate row carries NO token count: its arithmetic is the measured body
        # against the declared request-byte budget, and the token rule must never be
        # applied to it (nor a token number accepted from it).
        measured = check.get("input_bytes")
        budget = check.get("request_byte_budget")
        if type(measured) is not int or type(budget) is not int or budget <= 0:
            raise ValueError("capacity_check_without_a_byte_measurement")
        if check.get("fits") is not (measured <= budget):
            raise ValueError("capacity_check_arithmetic_inconsistent")
        if check.get("input_tokens") is not None:
            raise ValueError("byte_gate_record_carries_a_token_count")
    else:
        if check.get("fits") is not (check.get("input_tokens", 0)
                                     + check.get("output_reserve_tokens", 0)
                                     <= check.get("context_window", 0)):
            raise ValueError("capacity_check_arithmetic_inconsistent")
        if check.get("input_tokens") is not None and not isinstance(check.get("input_tokens"), int):
            raise ValueError("capacity_check_input_not_an_integer")
'''
    old_block = '''    if check.get("fits") is not (check.get("input_tokens", 0)
                                 + check.get("output_reserve_tokens", 0)
                                 <= check.get("context_window", 0)):
        raise ValueError("capacity_check_arithmetic_inconsistent")
    if check.get("input_tokens") is not None and not isinstance(check.get("input_tokens"), int):
        raise ValueError("capacity_check_input_not_an_integer")
'''
    _require(text, new_block)
    text = text.replace(new_block, old_block)
    helpers = text[text.index("#: The fields a BYTE gate is audited by."):
                   text.index("def audit_cloud_journal(")]
    if "def carry_byte_fields" not in helpers or helpers.count("def carry_byte_fields") != 1:
        raise SystemExit("the recorded byte-field helper block is not where it was staged")
    text = text.replace(helpers, "\n\n")
    refused_new = '''                checks.append(carry_byte_fields(
                    {"request_id": rid, "refused": True,
                     "code": body[-1].get("code"),
                     "role": check.get("role") if check else None,
                     "input_tokens": check.get("input_tokens") if check else None,
                     "output_reserve_tokens": (check.get("output_reserve_tokens")
                                               if check else None),
                     "context_window": check.get("context_window") if check else None,
                     "count_source": check.get("count_source") if check else None,
                     "fits": check.get("fits") if check else False,
                     "input_sha256": check.get("input_sha256") if check else None,
                     "journal_position": cursor},
                    check or {}))'''
    refused_old = '''                checks.append({"request_id": rid, "refused": True,
                               "code": body[-1].get("code"),
                               "role": check.get("role") if check else None,
                               "input_tokens": check.get("input_tokens") if check else None,
                               "output_reserve_tokens": (check.get("output_reserve_tokens")
                                                         if check else None),
                               "context_window": check.get("context_window") if check else None,
                               "count_source": check.get("count_source") if check else None,
                               "fits": check.get("fits") if check else False,
                               "input_sha256": check.get("input_sha256") if check else None,
                               "journal_position": cursor})'''
    _require(text, refused_new)
    text = text.replace(refused_new, refused_old)
    sent_new = '''                checks.append(carry_byte_fields(
                    {"request_id": rid, "role": check.get("role"),
                     "input_tokens": check.get("input_tokens"),
                     "output_reserve_tokens": check.get("output_reserve_tokens"),
                     "context_window": check.get("context_window"),
                     "count_source": check.get("count_source"),
                     "fits": check.get("fits"), "refused": False,
                     "input_sha256": check.get("input_sha256"),
                     "journal_position": cursor}, check))'''
    sent_old = '''                checks.append({"request_id": rid, "role": check.get("role"),
                               "input_tokens": check.get("input_tokens"),
                               "output_reserve_tokens": check.get("output_reserve_tokens"),
                               "context_window": check.get("context_window"),
                               "count_source": check.get("count_source"),
                               "fits": check.get("fits"), "refused": False,
                               "input_sha256": check.get("input_sha256"),
                               "journal_position": cursor})'''
    _require(text, sent_new)
    text = text.replace(sent_new, sent_old)
    return text


def reconstruct_ae_cloud_re_input_audit(text: str) -> str:
    """Undo the r3 declared-pass-through handling in the single-task R/E audit."""
    new_import = """from ae_cloud_input_audit import (AUXILIARY_INTERNAL_FIELDS, AUXILIARY_TOOL_CALL_FIELDS,
                                  CLOUD_FIELDS, MEMORY_METADATA, MEMORY_METADATA_AGENT,
                                  check_declared_transport_fields, cloud_change_reasons,
                                  declared_cloud_fields,
                                  MEMORY_METADATA_ARCHIVAL, MEMORY_METADATA_CONVERSATION,
                                  MEMORY_METADATA_RECALL, MEMORY_METADATA_RECOMPILED,
                                  MEMORY_METADATA_TAGS, MEMORY_BLOCK_HEADER,
                                  MESSAGE_POST_PATH, MODELS_PATH, CHAT_PATH,
                                  PINNED_LIMITS, TOOL_CALL_ID_MAX_LEN,
                                  TOOL_RETURN_WRAPPER_KEYS, CloudInputAudit,
                                  auxiliary_wire_messages, render_memory_blocks)"""
    old_import = """from ae_cloud_input_audit import (AUXILIARY_INTERNAL_FIELDS, AUXILIARY_TOOL_CALL_FIELDS,
                                  CLOUD_FIELDS, MEMORY_METADATA, MEMORY_METADATA_AGENT,
                                  MEMORY_METADATA_ARCHIVAL, MEMORY_METADATA_CONVERSATION,
                                  MEMORY_METADATA_RECALL, MEMORY_METADATA_RECOMPILED,
                                  MEMORY_METADATA_TAGS, MEMORY_BLOCK_HEADER,
                                  MESSAGE_POST_PATH, MODELS_PATH, CHAT_PATH,
                                  PINNED_LIMITS, TOOL_CALL_ID_MAX_LEN,
                                  TOOL_RETURN_WRAPPER_KEYS, CloudInputAudit,
                                  auxiliary_wire_messages, render_memory_blocks)"""
    _require(text, new_import)
    text = text.replace(new_import, old_import)
    new_proxy_import = """from ae_cloud_proxy import (COMPAT_PROFILE, MODEL, CloudConfig, normalize_request,
                            profile_of)"""
    old_proxy_import = "from ae_cloud_proxy import COMPAT_PROFILE, MODEL, CloudConfig, normalize_request"
    _require(text, new_proxy_import)
    text = text.replace(new_proxy_import, old_proxy_import)
    new_agent = """        profile = profile_of(self.cloud_config)
        need(set(body) <= declared_cloud_fields(profile), "undeclared_cloud_request_field")
        missing = []
        check_declared_transport_fields(body, profile, missing)
        need(not missing, missing[0] if missing else "declared_transport_field_missing")
        need("seed" not in body and "chat_template_kwargs" not in body,"""
    old_agent = """        need(set(body) <= CLOUD_FIELDS, "undeclared_cloud_request_field")
        need("seed" not in body and "chat_template_kwargs" not in body,"""
    _require(text, new_agent)
    text = text.replace(new_agent, old_agent)
    new_change = """        need(limit == PINNED["max_output_tokens"], "agent_output_limit_not_2048")
        reasons = cloud_change_reasons(call["changes"], body, profile, limit)
        need(not reasons, reasons[0] if reasons else "unexpected_cloud_change")"""
    old_change = """        need(limit == PINNED["max_output_tokens"], "agent_output_limit_not_2048")
        need(all(c.get("operation") == "rename" for c in call["changes"]), "unexpected_cloud_change")
        need(len(call["changes"]) <= 1, "unexpected_cloud_change_count")
        if call["changes"]:
            change = call["changes"][0]
            need(change.get("from") == "max_completion_tokens" and change.get("to") == "max_tokens"
                 and change.get("value") == limit, "unexpected_cloud_rename")"""
    _require(text, new_change)
    text = text.replace(new_change, old_change)
    new_aux = """                reasons = cloud_change_reasons(match["changes"], match["body"],
                                               profile_of(self.cloud_config), limit)
                need(not reasons, reasons[0] if reasons else "unexpected_cloud_change")"""
    old_aux = """                need(all(c.get("operation") == "rename" for c in match["changes"]),
                     "unexpected_cloud_change")"""
    _require(text, new_aux)
    text = text.replace(new_aux, old_aux)
    return text


RECONSTRUCTORS = {"ae_cloud_audit": reconstruct_ae_cloud_audit,
                  "ae_cloud_re_input_audit": reconstruct_ae_cloud_re_input_audit}


def baseline_for(spec, name: str):
    """Return (before_bytes, baseline_kind, note) for one FILES entry."""
    kind, argument = spec[0], spec[1]
    if kind == "new":
        return None, "new in r3", "no pre-image exists; first shipped here"
    if kind == "r2":
        path = R2 / "after" / name
        if not path.is_file():
            raise SystemExit(f"the r2 delivery has no {name!r} pre-image")
        return path.read_bytes(), "r2 delivery copy", "the r2 delivery's own after/ copy"
    if kind == "stored":
        path, note = argument, spec[2]
        if not Path(path).is_file():
            raise SystemExit(f"the stored baseline is absent: {path}")
        return Path(path).read_bytes(), "stored capture", note
    if kind == "reconstruct":
        text = (ROOT / (
            "ae_cloud_audit.py" if argument == "ae_cloud_audit"
            else "ae_cloud_re_input_audit.py")).read_text(encoding="utf-8")
        before = RECONSTRUCTORS[argument](text)
        return before.encode("utf-8"), "reconstructed pre-image", (
            "the recorded r3 edits reversed, guarded by an exact-match assertion")
    raise SystemExit(f"unknown baseline kind {kind!r}")


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists")
    for name, source, _baseline in FILES:
        if not (ROOT / source).is_file():
            raise SystemExit(f"the source file is absent: {source}")
    for directory in ("after", "diffs", "evidence"):
        (OUT / directory).mkdir(parents=True, exist_ok=False)
    table = []
    for index, (name, source, baseline) in enumerate(FILES, start=1):
        path = ROOT / source
        after = path.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        before, baseline_kind, note = baseline_for(baseline, name)
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if before is None:
            diff_path.write_text(f"# {name}: NEW or first shipped here\n"
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
        {"schema": "ae-deepseek-re-integration-diff-3",
         "note": ("r3 corrects the three defects the r2 audit found, and nothing else. "
                  "(1) The 0.4 audit branch is REACHABLE through the public "
                  "`audit_re_multiturn_inputs`: the schema tuple, the multicall/stack "
                  "evidence selection, the transport's declared request shape and the "
                  "protocol's own pinned limits/pacing are all handled, and a complete "
                  "offline 0.4 capture over the real dataset reaches the final VALID "
                  "verdict with 108 byte-gate records and both arms clarified per stage. "
                  "(2) The service byte policy is MODEL-BOUND: the DeepSeek profile declares "
                  "its wire model, its required non-thinking field and the pinned service's "
                  "pass-through fields, the proxy refuses a request that omits or rewrites "
                  "the mode, an undeclared field, or a Qwen/unknown model, and the launcher "
                  "passes the policy to the child. (3) The delivery narrative is corrected: "
                  "the historical receipt `transfers/ae-stack-startup-20260914-r1/` is a "
                  "FROZEN past record, so it must keep rejecting the current tree; the "
                  "current-patch fixture is verified separately. The sealed 0.1/0.2/0.3 "
                  "protocols, their constants and their count bases are NOT loosened."),
         "delivered_from": "the working tree at staging time; verified by SHA256SUMS",
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} ({row['baseline_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
