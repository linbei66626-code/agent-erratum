#!/usr/bin/env python3
"""Stage the stacked-runtime RECEIVE-ACTIVATION fix delivery r1.

    .venv-vita/bin/python tools/stage_deepseek_stack_multicall_activation_r1_delivery.py

The output directory is exclusive: no earlier delivery is touched. Baselines are the
delivery copies whose digests are the ones the live service and the sealed r4 record
pinned (see `before-after.json`), so the diff of every changed file contains only this
round's change.
"""
from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/ae-deepseek-stack-multicall-activation-r1"
LOG_DIR = Path("/tmp/ae-r3-2")

#: (delivery name, repo path, baseline path, baseline kind)
FILES = [
    ("ae_multicall.py", "ae_multicall.py",
     ROOT / "results/ae-multiturn-capacity-no-compaction-r3/after/ae_multicall.py",
     "the module digest both live manifests and the sealed r4 record pinned "
     "(d04ff1ef...)"),
    ("scripts_deployment_letta_local.py", "scripts/deployment/letta_local.py",
     ROOT / "results/ae-deepseek-letta-thinking-r1/after/scripts_deployment_letta_local.py",
     "the launcher delivered with the DeepSeek thinking round"),
    ("scripts_deployment_letta_bootstrap.py", "scripts/deployment/letta_bootstrap.py",
     ROOT / "results/ae-multiturn-capacity-no-compaction-r3-launcher-fix"
          "/after/scripts_deployment_letta_bootstrap.py",
     "the deployed bootstrap (launch.json bootstrap_sha256 70d895e1...)"),
    ("letta-multicall_manifest.json", "deployment-assets/letta-multicall/manifest.json",
     Path("/tmp/ae-r3-2/multicall-manifest-before.json"),
     "captured pre-image this round; its digest is the one the sealed r4 record and "
     "the site pinned (c8a80e99...)"),
    ("letta-no-compaction_stack-manifest.json",
     "deployment-assets/letta-no-compaction/stack-manifest.json",
     Path("/tmp/ae-r3-2/stack-manifest-before.json"),
     "captured pre-image this round (the LOCAL manifest; the site regenerates its own "
     "with site paths)"),
    ("letta-no-compaction_manifest.json",
     "deployment-assets/letta-no-compaction/manifest.json",
     ROOT / "deployment-assets/letta-no-compaction/manifest.json",
     "verified unchanged this round (the capacity patch and its staged helpers are "
     "untouched)"),
    ("tests_test_ae_no_compaction_stack_gate.py",
     "tests/test_ae_no_compaction_stack_gate.py",
     ROOT / "results/ae-multiturn-capacity-no-compaction-r3-launcher-fix"
          "/after/tests_test_ae_no_compaction_stack_gate.py",
     "the stack-gate suite delivered with the launcher fix"),
    ("tests_test_ae_stack_receipt_wiring.py", "tests/test_ae_stack_receipt_wiring.py",
     ROOT / "results/ae-deepseek-re-multiturn-integration-r3"
          "/after/tests_test_ae_stack_receipt_wiring.py",
     "the receipt-wiring suite from the integration round"),
    ("tests_test_ae_no_compaction_launcher_entry.py",
     "tests/test_ae_no_compaction_launcher_entry.py",
     ROOT / "results/ae-multiturn-capacity-no-compaction-r3-launcher-source-fix"
          "/after/tests_test_ae_no_compaction_launcher_entry.py",
     "the launcher-entry suite from the source fix"),
    ("tests_test_ae_deepseek_stack_multicall_activation.py",
     "tests/test_ae_deepseek_stack_multicall_activation.py", None, "new in this round"),
]

NOTE = (
    "The sealed r4 re-audit of the completed DeepSeek capture stopped at "
    "`tool_call_count_changed`, and the independent loss diagnostic showed why: of 337 "
    "generation responses, 10 batches returned MORE THAN ONE tool call (39 calls), and "
    "the following history kept only the first call of each batch, so 29 calls never "
    "reached the driver. The service process (PID 11221) had declared the STACKED profile "
    "but not the receive compatibility policy the stack composes, so the patched receive "
    "point took its `not_declared` branch and the pinned upstream code truncated every "
    "multi-call response to its first call, while the pinned serializers also shortened "
    "every surviving id to 29 characters. Loading the right SOURCE is not activating the "
    "POLICY. This round makes one launch switch activate and VERIFY both contracts: the "
    "launcher declares the composed receive profile in the child environment (read from "
    "the support module the stack manifest pins, never restated), the bootstrap gate "
    "requires that declaration and verifies it against the STACKED contract - "
    "cross-checking the multicall manifest the stack names as its receive half - before "
    "Letta is imported, and a declared-but-unestablishable state REFUSES TO START instead "
    "of falling back to truncation. Model, prompt, R/E definition, request budget, "
    "`parallel_tool_calls=false`, serial tool execution and the no-compaction policy are "
    "untouched, and the sealed/legacy path (no declaration) still runs the upsteam "
    "behaviour byte-for-byte. The stacked and multicall-only checkouts are byte-identical "
    "to before: only the policy module the manifests pin changed."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_test_module():
    for entry in (str(ROOT), str(ROOT / "tests")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    spec = importlib.util.spec_from_file_location(
        "ae_stack_activation_evidence",
        ROOT / "tests/test_ae_deepseek_stack_multicall_activation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def chain_evidence(module) -> dict:
    """Run the acceptance chain once and return what it really produced."""
    import ae_multicall
    case = module.TheWholeBatchSurvivesTheStackedRuntimeTests(
        "test_two_memory_updates_reach_the_driver_and_their_whole_ids_reach_the_wire")
    case.setUpClass()
    case.setUp()
    try:
        provider = module._provider_tool_calls()
        kept, logger = case._receive_block(provider)
        messages, continue_stepping, stop_reason = case._client_tool_branch(kept)
        approval = messages[0]
        bridge, transport = case._driver(approval)
        assistant = bridge.exchange(
            [{"role": "user", "type": "message",
              "content": json.dumps({"source": "current_task"})}], {})
        wire, captured = case._next_request_wire(transport.session.tool_returns)
        tool_messages = [message for message in wire["messages"]
                         if message["role"] == "tool"]
        return {
            "launcher_child_environment": {
                key: value for key, value in sorted(case.child_env.items())
                if key.startswith("AE_")},
            "receive_contract": ae_multicall.verify_receive_contract(
                case.child_env, module=ae_multicall),
            "provider_batch": [{"id": call.id, "name": call.function.name,
                                "arguments": call.function.arguments}
                               for call in provider],
            "kept_after_the_real_receive_decision": [call.id for call in kept],
            "receive_decision_log": {"info": logger.info_calls,
                                     "warning": logger.warning_calls},
            "client_tool_branch": {
                "continue_stepping": continue_stepping,
                "stop_reason": stop_reason.stop_reason,
                "approval_tool_calls": approval["tool_calls"]},
            "driver": {"tool_returns": transport.session.tool_returns,
                       "patch_count": len(transport.session.patches),
                       "multicall_batch_evidence": bridge.multicall_batches[0],
                       "assistant_messages": assistant,
                       "posts": [entry for entry in transport.sent
                                 if entry["method"] == "POST"]},
            "next_provider_request": {
                "method": captured["method"], "url": captured["url"],
                "tool_call_ids": [message["tool_call_id"] for message in tool_messages],
                "tool_call_id_lengths": [len(message["tool_call_id"])
                                         for message in tool_messages],
                "pinned_max_tool_call_id_length":
                    ae_multicall.UPSTREAM_TOOL_CALL_ID_MAX_LEN,
                "parallel_tool_calls": wire["parallel_tool_calls"],
                "thinking": wire.get("thinking"),
                "body": wire},
        }
    finally:
        case.doCleanups()


def activation_evidence(module) -> dict:
    """The launcher's declaration, verified - and refused - by the REAL startup gate."""
    import ae_multicall
    case = module.StackLaunchActivationTests(
        "test_the_stack_launch_declares_and_activates_the_composed_receive_policy")
    case.setUpClass()
    case.setUp()
    try:
        runtime = case._runtime()
        env = runtime.env(Path("/tmp/ae-r3-2/evidence-receipt"))
        receipt, events = case._bootstrap_gate(env)
        activation = ae_multicall.verify_receive_contract(env, module=ae_multicall)
        return {
            "note": ("Produced by running the REAL launcher runtime and the REAL bootstrap "
                     "gate on this round's files: the launcher declares the composed "
                     "receive policy in the child environment, and the service-side gate "
                     "verifies it against the stacked contract before Letta is imported. "
                     "The refusal table below is the same gate refusing every "
                     "declared-but-unestablishable state."),
            "launcher_receive_policy_declaration": runtime.receive_policy(),
            "child_environment": {key: value for key, value in sorted(env.items())
                                  if key.startswith("AE_")},
            "receive_contract": activation,
            "bootstrap_report_events": events,
            "stack_load_receipt": receipt,
            "refusals": refusal_evidence(module),
        }
    finally:
        case.doCleanups()


def refusal_evidence(module) -> dict:
    """The real gate's own refusals for every declared-but-unestablishable state."""
    import ae_multicall
    case = module.StackLaunchActivationTests(
        "test_a_stack_that_is_not_activated_refuses_to_start")
    case.setUpClass()
    case.setUp()
    try:
        mutations = (
            ("stack_declared_without_the_receive_policy",
             lambda env: env.pop("AE_LETTA_MULTICALL_PROFILE")),
            ("unknown_receive_version",
             lambda env: env.__setitem__("AE_LETTA_MULTICALL_PROFILE",
                                         "ae-multicall-receive-compat-999")),
            ("unknown_stacked_version",
             lambda env: env.__setitem__("AE_LETTA_PATCH_STACK_PROFILE",
                                         "ae-no-compaction-stack-999")),
            ("mixed_multicall_only_manifest",
             lambda env: env.__setitem__("AE_LETTA_MULTICALL_MANIFEST",
                                         str(module.MULTICALL_MANIFEST))),
            ("stale_module_digest",
             lambda env: env.__setitem__("AE_LETTA_MULTICALL_MODULE_SHA256", "0" * 64)),
            ("absent_stacked_manifest",
             lambda env: env.__setitem__(
                 "AE_LETTA_PATCH_STACK_MANIFEST", "/absent/stack-manifest.json")),
        )
        out = {}
        for label, mutate in mutations:
            env = case._child_env()
            mutate(env)
            try:
                case.bootstrap.install_patch_stack_gate(env, find_spec=case._find_spec())
                refusal = None
            except RuntimeError as exc:
                refusal = str(exc)
            out[label] = {
                "bootstrap_refusal": refusal,
                "receive_contract": ae_multicall.verify_receive_contract(
                    env, module=ae_multicall),
                "receipt_written": Path(env["AE_LETTA_PATCH_STACK_RECEIPT"]).exists()}
        return out
    finally:
        case.doCleanups()


def manifest_evidence() -> dict:
    """The two manifests' before/after digests and the UNCHANGED checkout digests."""
    import ae_multicall
    stack = ae_multicall.read_stack_manifest(
        ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json")
    reviewed = ae_multicall.read_manifest(
        ROOT / "deployment-assets/letta-multicall/manifest.json")
    capacity = ae_multicall.read_capacity_manifest(stack["no_compaction_manifest"])
    before = {"ae_multicall.py": sha256(
                  ROOT / "results/ae-multiturn-capacity-no-compaction-r3"
                       "/after/ae_multicall.py"),
              "deployment-assets/letta-multicall/manifest.json":
                  sha256(LOG_DIR / "multicall-manifest-before.json"),
              "deployment-assets/letta-no-compaction/stack-manifest.json":
                  sha256(LOG_DIR / "stack-manifest-before.json"),
              "deployment-assets/letta-no-compaction/manifest.json":
                  sha256(LOG_DIR / "no-compaction-manifest-before.json")}
    after = {"ae_multicall.py": sha256(ROOT / "ae_multicall.py"),
             "deployment-assets/letta-multicall/manifest.json":
                 sha256(ROOT / "deployment-assets/letta-multicall/manifest.json"),
             "deployment-assets/letta-no-compaction/stack-manifest.json":
                 sha256(ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json"),
             "deployment-assets/letta-no-compaction/manifest.json":
                 sha256(ROOT / "deployment-assets/letta-no-compaction/manifest.json")}
    return {
        "note": ("The policy module's bytes changed, so the two manifests that pin its "
                 "digest were regenerated by their own production generators "
                 "(`write_manifest.py`, `write_stack_manifest.py --rebuild`). The "
                 "capacity manifest and BOTH Letta checkouts are unchanged: every tracked "
                 "file digest below is the one the previous manifests already carried, so "
                 "no site checkout rebuild is required beyond re-running the manifest "
                 "generator."),
        "digests": {"before": before, "after": after,
                    "unchanged": sorted(name for name in before
                                        if before[name] == after[name])},
        "checkout_tracked_digests": {
            "stacked_patched_files": stack["patched_files"],
            "stacked_new_files": stack["new_files"],
            "multicall_patched_files": {name: item["patched_sha256"]
                                        for name, item in reviewed["patched_files"].items()},
            "capacity_patched_files": {name: item["patched_sha256"]
                                       for name, item in capacity["patched_files"].items()},
            "checked_outside_the_manifests":
                {"letta_checkout": stack["letta_checkout"],
                 "multicall_checkout": stack["multicall_checkout"],
                 "baseline_checkout": stack["baseline_checkout"],
                 "all_tracked_files_hash_to_their_manifested_digest":
                     ae_multicall.verify_stack_gate(
                         {"AE_LETTA_PATCH_STACK_PROFILE":
                              ae_multicall.STACK_PROFILE_VERSION,
                          "AE_LETTA_PATCH_STACK_MANIFEST":
                              str(ROOT / "deployment-assets/letta-no-compaction"
                                  "/stack-manifest.json"),
                          "AE_LETTA_MULTICALL_MODULE_SHA256":
                              sha256(ROOT / "ae_multicall.py")},
                         module=ae_multicall)}},
    }


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to stage: {OUT} already exists")
    for _name, source, _baseline, _kind in FILES:
        if not (ROOT / source).is_file():
            raise SystemExit(f"the source file is absent: {source}")
    for _name, _source, baseline, _kind in FILES:
        if baseline is not None and not baseline.is_file():
            raise SystemExit(f"the baseline is absent: {baseline}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for capture in ("multicall-manifest-before.json", "stack-manifest-before.json",
                    "no-compaction-manifest-before.json"):
        if not (LOG_DIR / capture).is_file():
            raise SystemExit(f"the pre-image capture is absent: {LOG_DIR / capture} "
                             "(copy the file BEFORE regenerating anything)")
    for directory in ("after", "before", "diffs", "evidence"):
        (OUT / directory).mkdir(parents=True, exist_ok=False)
    table = []
    for index, (name, source, baseline, kind) in enumerate(FILES, start=1):
        path = ROOT / source
        after = path.read_bytes()
        (OUT / "after" / name).write_bytes(after)
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if baseline is None:
            before = None
            diff_path.write_text(f"# {name}: NEW in this round\n"
                                 f"# after sha256 {sha256(path)}\n", encoding="utf-8")
        else:
            before = baseline.read_bytes()
            (OUT / "before" / name).write_bytes(before)
            diff_path.write_text("".join(difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}")), encoding="utf-8")
        table.append({"file": name, "repo_path": source,
                      "before_sha256": None if before is None
                      else hashlib.sha256(before).hexdigest(),
                      "after_sha256": sha256(path), "changed": before != after,
                      "baseline_kind": kind, "diff": str(diff_path.relative_to(OUT))})
    (OUT / "before-after.json").write_text(json.dumps(
        {"schema": "ae-deepseek-stack-multicall-activation-diff-1", "note": NOTE,
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    module = load_test_module()
    (OUT / "evidence/runtime-activation.json").write_text(json.dumps(
        activation_evidence(module), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "evidence/wire-and-chain.json").write_text(json.dumps(
        chain_evidence(module), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "evidence/manifest-regeneration.json").write_text(json.dumps(
        manifest_evidence(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for source, target in (("targeted-tests.log", "tests-targeted.log"),
                           ("affected-regression.log", "tests-affected-regression.log")):
        path = LOG_DIR / source
        if path.is_file():
            (OUT / "evidence" / target).write_bytes(path.read_bytes())
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} "
              f"({row['baseline_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
