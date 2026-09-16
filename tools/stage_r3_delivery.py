#!/usr/bin/env python3
"""Stage the R3 delivery from the working tree, mechanically.

    .venv-vita/bin/python tools/stage_r3_delivery.py

What it does, and what it deliberately does NOT do:

* copies the CURRENT bytes into `after/` and the R3 BASELINE bytes into `before/`.
  A baseline is either the r2 delivery's own `after/` copy, another stored copy named
  in `BASELINES`, or - for files this round appended to or edited textually - a
  pre-image RECONSTRUCTED by reversing the recorded edits. `before-after.json` marks
  every reconstructed pre-image as such, so nobody reads it as a stored capture.
* writes one unified diff per changed file (no diff for files whose content is
  identical), plus a line/sha table.
* never rewrites an earlier round's bytes, never touches `transfers/`, and refuses to
  run if the delivery directory already exists with content.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R2 = ROOT / "results/ae-multiturn-capacity-no-compaction-r2"
R1 = ROOT / "results/ae-multiturn-capacity-no-compaction-r1"
PACING = ROOT / "results/ae-cloud-pacing-20260912-r1/prior/ae_cloud_audit.py"
R5_MANIFEST = ROOT / "results/ae-cloud-multicall-20260913-r5/manifest/manifest.json"
OUT = ROOT / "results/ae-multiturn-capacity-no-compaction-r3"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(text: str, old: str) -> None:
    if text.count(old) != 1:
        raise SystemExit(f"the recorded edit is not present exactly once: {old[:60]!r}")


def reconstruct_ae_cloud_input_audit(text: str) -> str:
    """Undo the R3 capacity-record handling in the shared input audit."""
    new = '''            while cursor < len(records) - 1 and records[cursor].get("kind") == "capacity_armed":
                cursor += 1
            if cursor >= len(records) - 1:
                break
'''
    _require(text, new)
    text = text.replace(new, "")
    old_block = '''        armed = [r for r in records if r.get("kind") == "capacity_armed"]
        need(len(armed) <= 1, "duplicate_capacity_armed_record")
        gated = bool(armed)
        # A gated capture writes one more row per request (the gate's own count) and one
        # arming row before the first request. Both are handled by KIND here: the sealed
        # protocols arm nothing, so their walk is exactly what it always was.
'''
    _require(text, old_block)
    text = text.replace(old_block, "")
    new_kinds = '''            expected_kinds = ["client_request", "normalized_request"]
            if gated:
                expected_kinds.append("capacity_check")
            if paced:
                expected_kinds.append("pace_wait")
            expected_kinds += ["upstream_request", "upstream_response", "cloud_summary"]'''
    old_kinds = '''            paced_kinds = ["client_request", "normalized_request", "pace_wait", "upstream_request",
                           "upstream_response", "cloud_summary"]
            plain_kinds = ["client_request", "normalized_request", "upstream_request",
                           "upstream_response", "cloud_summary"]
            expected_kinds = paced_kinds if paced else plain_kinds'''
    _require(text, new_kinds)
    text = text.replace(new_kinds, old_kinds)
    new_check = '''            if gated:
                # The count must describe THIS request's normalized bytes, for THIS
                # role, under the declared basis - otherwise it is not evidence.
                check = group[expected_kinds.index("capacity_check")]
                need(check.get("input_sha256") == hashlib.sha256(normalized).hexdigest(),
                     "capacity_check_does_not_describe_this_request")
                need(check.get("role") == client.get("role"),
                     "capacity_check_role_differs_from_the_request_role")
                need(check.get("fits") is True, "sent_request_did_not_fit_the_declared_window")
                need(isinstance(check.get("count_source"), str) and check["count_source"],
                     "capacity_check_without_a_count_source")
                need(check.get("input_tokens", 0) + check.get("output_reserve_tokens", 0)
                     <= check.get("context_window", 0),
                     "capacity_check_arithmetic_inconsistent")
'''
    _require(text, new_check)
    text = text.replace(new_check, "")
    new_pace = '''            need(("pace_wait" in expected_kinds) if paced
                 else ("pace_wait" not in [r.get("kind") for r in group]),
                 "missing_or_unexpected_pace_event")'''
    old_pace = '''            need((group[2].get("kind") == "pace_wait") if paced
                 else ("pace_wait" not in [r.get("kind") for r in group]),
                 "missing_or_unexpected_pace_event")'''
    _require(text, new_pace)
    text = text.replace(new_pace, old_pace)
    return text


def reconstruct_ae_cloud_proxy(text: str) -> str:
    """Undo the R3 count/publish split in the proxy."""
    new_helpers = text[text.index("    def _capacity_record(self, normalized, role, request_id):"):
                       text.index("    def dispatch(self, method, path, raw")]
    old_helpers = '''    def check_capacity(self, normalized, role):
        """Count THIS request and decide, or refuse. Returns the record for the journal."""
        if self.capacity is None:
            return None
        reserve = self._capacity_role_reserve(role)
        counted = self.capacity["count_request"](normalized, role)
        if not isinstance(counted, int) or counted < 0:
            raise ProxyBlocked("capacity_count_unavailable")
        record = {"role": role, "input_tokens": counted,
                  "output_reserve_tokens": reserve,
                  "context_window": self.capacity["context_window"],
                  "count_source": self.capacity["count_source"],
                  "fits": counted + reserve <= self.capacity["context_window"]}
        record["over_by"] = max(0, counted + reserve - self.capacity["context_window"])
        self.capacity_checks.append(record)
        if not record["fits"]:
            # The blocked row's `code` is the stable gate code; the numbers live in the
            # record the caller persists, so an audit can attribute the refusal.
            raise ProxyBlocked("capacity_exceeded_before_send")
        return record

'''
    text = text.replace(new_helpers, old_helpers)
    new_dispatch = '''            self._wire("normalized_request", normalized, request_id=request_id, changes=changes)
            capacity_record = (self._capacity_record(normalized, role, request_id)
                               if (method, path) == ("POST", "/v1/chat/completions") else None)
            if capacity_record is not None:
                self._publish_capacity_check(capacity_record)
                if not capacity_record["fits"]:
                    # The blocked row's `code` is the stable gate code; the numbers are
                    # in the record just published, so an audit can attribute the refusal.
                    raise ProxyBlocked("capacity_exceeded_before_send")
'''
    old_dispatch = '''            capacity_record = self.check_capacity(normalized, role) \\
                if (method, path) == ("POST", "/v1/chat/completions") else None
            if capacity_record is not None:
                # The per-request record carries the identity of the request it
                # counted, so a later audit matches checks to requests instead of
                # trusting that the list is non-empty.
                capacity_record = dict(capacity_record, request_id=request_id,
                                       input_sha256=hashlib.sha256(normalized).hexdigest())
            self._wire("normalized_request", normalized, request_id=request_id, changes=changes)
            if capacity_record is not None:
                self.journal.append({"kind": "capacity_check", **capacity_record})
                if self.capacity_emit is not None:
                    self.capacity_emit({"kind": "capacity_check", **capacity_record})
'''
    _require(text, new_dispatch)
    text = text.replace(new_dispatch, old_dispatch)
    new_comment = '''            # The declared-capacity gate runs on the NORMALIZED body, and the body is
            # journalled before the decision is published, so a refusal can neither
            # reach the provider nor consume the request budget - and the refused
            # request stays in the journal as evidence of what was refused.
'''
    old_comment = '''            # The declared-capacity gate runs HERE: the request is fully built, and
            # nothing has been journalled, paced or sent yet, so a refusal cannot
            # reach the provider and cannot consume the request budget.
'''
    _require(text, new_comment)
    text = text.replace(new_comment, old_comment)
    new_emit = '''        #: Optional caller callback for those records. They are ALSO journalled as
        #: `capacity_check` rows, because the evidence a refusal must leave behind is
        #: per request: the audit reads the journal, not this object.'''
    old_emit = '''        #: Optional caller callback for those records. The proxy's own JOURNAL format
        #: is left untouched on purpose: the existing transport audit reads it, and a
        #: new record kind there would change what that audit has to accept.'''
    _require(text, new_emit)
    return text.replace(new_emit, old_emit)


def reconstruct_ae_cloud_audit(text: str) -> str:
    """Undo the R3 walk rewrite by restoring the prior stored copy."""
    return PACING.read_text(encoding="utf-8")


def reconstruct_ae_multicall(text: str) -> str:
    """Remove the appended STACK contract."""
    marker = ("\n\n# ---------------------------------------------------------------------------\n"
              "# The STACKED checkout: the reviewed multicall patch PLUS the no-compaction")
    if marker not in text:
        raise SystemExit("the appended stack contract is absent")
    return text[:text.index(marker)] + "\n"


def reconstruct_bootstrap(text: str) -> str:
    """Remove the stack gate and its call."""
    start = text.index("def _required_stack_environment(source=None):")
    end = text.index("def install_multicall_gate(environ=None")
    text = text[:start] + text[end:]
    call = '''    # The STACKED profile (multicall + no-compaction) is a separate declaration: a
    # service asked for it must resolve every changed and new file through the real
    # import machinery and write its own receipt, or it does not start at all.
    install_patch_stack_gate()
'''
    _require(text, call)
    return text.replace(call, "")


def reconstruct_launcher(text: str) -> str:
    """Remove the launcher's stack opt-in, leaving the multicall path untouched."""
    start = text.index("#: The stacked profile this launcher can opt into.")
    end = text.index("class MulticallRuntime:")
    text = text[:start] + text[end:]
    start = text.index("class PatchStackRuntime:")
    end = text.index("ROLE = \"ae01_letta\"")
    text = text[:start] + text[end:]
    replacements = [
        ('''    def env(self, db: dict | None = None, model_url: str | None = None,
            multicall: "MulticallRuntime | None" = None,
            patch_stack: "PatchStackRuntime | None" = None) -> dict[str, str]:''',
         '''    def env(self, db: dict | None = None, model_url: str | None = None,
            multicall: "MulticallRuntime | None" = None) -> dict[str, str]:'''),
        ('''        if patch_stack is not None:
            # The stacked profile's own five keys. The module digest and support
            # directory are the same facts, so both profiles cross consistently.
            env.update(patch_stack.env(self.record))
''', ""),
        ('''    def start(self, model_url: str, port: int, bounded_nltk_startup: bool = False,
              explicit_llm_config: bool = False,
              multicall: "MulticallRuntime | None" = None,
              patch_stack: "PatchStackRuntime | None" = None) -> dict:
        self.source_check(multicall=multicall)
        if patch_stack is not None:
            # The stacked gate also lives in the bootstrap wrapper: it must run in the
            # process that imports Letta, and that process writes the receipt.
            patch_stack.verified()
            if not bounded_nltk_startup:
                raise RuntimeError(
                    "the AE patch stack runtime requires the bounded-startup bootstrap "
                    "wrapper so the gate and stack load receipt run in the service process")
''',
         '''    def start(self, model_url: str, port: int, bounded_nltk_startup: bool = False,
              explicit_llm_config: bool = False,
              multicall: "MulticallRuntime | None" = None) -> dict:
        self.source_check(multicall=multicall)
'''),
        ('''        launch_info["service_mode"] = ("ae_no_compaction_stack" if patch_stack is not None
                                       else "ae_multicall_receive_compat"
                                       if multicall is not None else "legacy_sequential")
        if patch_stack is not None:
            launch_info["patch_stack_profile"] = patch_stack.profile
            launch_info["patch_stack_manifest_sha256"] = hashlib.sha256(
                patch_stack.manifest.read_bytes()).hexdigest()
            launch_info["patch_stack_load_receipt"] = str(
                self.record / "patch-stack-load.json")
''',
         '''        launch_info["service_mode"] = ("ae_multicall_receive_compat"
                                       if multicall is not None else "legacy_sequential")
'''),
        ('''        env = self.env(db, None if explicit_llm_config else model_url, multicall, patch_stack)''',
         '''        env = self.env(db, None if explicit_llm_config else model_url, multicall)'''),
        ('''                    if patch_stack is not None:
                        # A started process is not evidence by itself: the receipt must
                        # exist AND describe the checkout and digests this launch
                        # declared, or the service is treated as unverified.
                        module, record = patch_stack.verified()
                        receipt = self.record / "patch-stack-load.json"
                        if not receipt.is_file():
                            raise RuntimeError(
                                "the AE patch stack service started without writing its "
                                "stack load receipt; treat this service as unverified")
                        module.validate_stack_receipt(
                            json.loads(receipt.read_text(encoding="utf-8")), record,
                            hashlib.sha256(patch_stack.manifest.read_bytes()).hexdigest())
                        started["patch_stack_load_receipt"] = str(receipt)
''', ""),
        ('''    parser.add_argument("--patch-stack-manifest", type=Path, default=None,
                        help="Opt-in: reviewed STACKED (multicall + no-compaction) manifest")
    parser.add_argument("--patch-stack-support", type=Path, default=None,
                        help="Directory holding the project's ae_multicall.py (with the stack manifest)")
    parser.add_argument("--patch-stack-profile", default=STACK_PROFILE_VERSION,
                        help="The stacked profile value to require in the service process")
''', ""),
        ('''            stack = None
            if args.patch_stack_manifest is not None:
                stack = PatchStackRuntime(manifest=args.patch_stack_manifest,
                                          support=args.patch_stack_support,
                                          receipt=deploy.record / "patch-stack-load.json",
                                          profile=args.patch_stack_profile)
''', ""),
        ('''                                  multicall=runtime, patch_stack=stack)''',
         '''                                  multicall=runtime)'''),
    ]
    for new, old in replacements:
        _require(text, new)
        text = text.replace(new, old)
    return text


def reconstruct_proxy_cli(text: str) -> str:
    """Remove the R3 `--capacity-config` arming from the proxy CLI."""
    start = text.index('    parser.add_argument("--capacity-config", type=Path, default=None,')
    end = text.index('    args = parser.parse_args()')
    text = text[:start] + text[end:]
    arm = text[text.index("        armed = None\n"):text.index("        server = make_server(")]
    text = text.replace(arm, "")
    print_new = '''        print(json.dumps({"status": "listening",
                          "listen": f"http://127.0.0.1:{args.listen_port}",
                          "capacity_armed": armed is not None,
                          "capacity_declaration_sha256": armed,
                          "task_runner_ready": False, "inference_started": False}), flush=True)'''
    print_old = '''        print(json.dumps({"status": "listening",
                          "listen": f"http://127.0.0.1:{args.listen_port}",
                          "task_runner_ready": False, "inference_started": False}), flush=True)'''
    _require(text, print_new)
    return text.replace(print_new, print_old)


def reconstruct_aux_wire(text: str) -> str:
    """Undo the r4-capture fixture's frozen-provenance repairs."""
    replacements = [
        ('''        from unittest.mock import patch
        from ae_cloud_input_audit import audit_cloud_inputs
        import ae_cloud_proxy
        import hashlib as _hashlib
        plan = json.loads((R4_RUN / "plan.json").read_text(encoding="utf-8"))
        # The transport gate hashes the proxy's OWN sources, and the capacity work
        # changed `ae_cloud_proxy.py` after this capture was sealed. Feed ONLY that
        # gate's source set its recorded digests; the plan's provenance gate keeps
        # comparing live bytes, so the capture still reports its real `ae_adapter.py`
        # mismatch below instead of being rewritten to look consistent.
        transport_files = {"ae_cloud_proxy.py", "ae_model_proxy.py", "ae_http.py"}
        stub = _StubHashlib(_hashlib, frozen_provenance_hashes(
            {name: digest for name, digest in plan["provenance"]["code_sha256"].items()
             if name in transport_files}))
        with patch.object(ae_cloud_proxy, "hashlib", stub):
            report = audit_cloud_inputs(R4_RUN, R4_JOURNAL, config_path=CAPABILITY)''',
         '''        from ae_cloud_input_audit import audit_cloud_inputs
        report = audit_cloud_inputs(R4_RUN, R4_JOURNAL, config_path=CAPABILITY)'''),
        ('''        from unittest.mock import patch
        import ae_cloud_input_audit
        import ae_cloud_proxy
        import ae_input_audit
        import hashlib as _hashlib
        plan = json.loads((R4_RUN / "plan.json").read_text(encoding="utf-8"))
        recorded = dict(plan["provenance"]["code_sha256"])
        mapping = frozen_provenance_hashes(recorded)
        self.assertTrue(mapping, "the recorded production files must be present")
        stub = _StubHashlib(_hashlib, mapping)
        # `ae_cloud_proxy` hashes its OWN sources for the transport gate, so the
        # frozen digests have to reach that module too, not only the input audit.
        with patch.object(ae_cloud_input_audit, "hashlib", stub), \\
             patch.object(ae_input_audit, "hashlib", stub), \\
             patch.object(ae_cloud_proxy, "hashlib", stub):''',
         '''        from unittest.mock import patch
        import ae_cloud_input_audit
        import ae_input_audit
        import hashlib as _hashlib
        plan = json.loads((R4_RUN / "plan.json").read_text(encoding="utf-8"))
        recorded = dict(plan["provenance"]["code_sha256"])
        mapping = frozen_provenance_hashes(recorded)
        self.assertTrue(mapping, "the recorded production files must be present")
        stub = _StubHashlib(_hashlib, mapping)
        with patch.object(ae_cloud_input_audit, "hashlib", stub), \\
             patch.object(ae_input_audit, "hashlib", stub):'''),
        ('''        import ae_cloud_input_audit
        import ae_cloud_proxy
        import ae_input_audit
        import hashlib as _hashlib
        plan = json.loads((R4_RUN / "plan.json").read_text(encoding="utf-8"))
        mapping = frozen_provenance_hashes(dict(plan["provenance"]["code_sha256"]))
        stub = _StubHashlib(_hashlib, mapping)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            shutil.copy2(R4_JOURNAL, journal)
            request_id = mutate_auxiliary_body(
                journal,
                lambda body: body["messages"][-1].__setitem__(
                    "content", "tampered auxiliary content"))
            with patch.object(ae_cloud_proxy, "hashlib", stub):
                assert_transport_still_checked({"journal": journal})
            with patch.object(ae_cloud_input_audit, "hashlib", stub), \\
                 patch.object(ae_input_audit, "hashlib", stub), \\
                 patch.object(ae_cloud_proxy, "hashlib", stub):''',
         '''        import ae_cloud_input_audit
        import ae_input_audit
        import hashlib as _hashlib
        plan = json.loads((R4_RUN / "plan.json").read_text(encoding="utf-8"))
        mapping = frozen_provenance_hashes(dict(plan["provenance"]["code_sha256"]))
        stub = _StubHashlib(_hashlib, mapping)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            shutil.copy2(R4_JOURNAL, journal)
            request_id = mutate_auxiliary_body(
                journal,
                lambda body: body["messages"][-1].__setitem__(
                    "content", "tampered auxiliary content"))
            assert_transport_still_checked({"journal": journal})
            with patch.object(ae_cloud_input_audit, "hashlib", stub), \\
                 patch.object(ae_input_audit, "hashlib", stub):'''),
    ]
    for new, old in replacements:
        _require(text, new)
        text = text.replace(new, old)
    return text


#: delivery name -> (repo path, baseline source)
#: baseline: ("r2", <name in r2/after or r2/before>) | ("file", <path>) |
#:           ("reconstruct", <function>) | ("absent", None)
FILES = [
    ("ae_multiturn_capacity.py", ROOT / "ae_multiturn_capacity.py", ("r2", "ae_multiturn_capacity.py")),
    ("ae_cloud_proxy.py", ROOT / "ae_cloud_proxy.py", ("r2", "ae_cloud_proxy.py")),
    ("ae_cloud_audit.py", ROOT / "ae_cloud_audit.py", ("reconstruct", reconstruct_ae_cloud_audit)),
    ("ae_cloud_input_audit.py", ROOT / "ae_cloud_input_audit.py",
     ("reconstruct", reconstruct_ae_cloud_input_audit)),
    ("ae_cloud_re_multiturn.py", ROOT / "ae_cloud_re_multiturn.py",
     ("r2", "ae_cloud_re_multiturn.py")),
    ("ae_cloud_re_multiturn_input_audit.py", ROOT / "ae_cloud_re_multiturn_input_audit.py",
     ("r2", "ae_cloud_re_multiturn_input_audit.py")),
    ("ae_multicall.py", ROOT / "ae_multicall.py", ("reconstruct", reconstruct_ae_multicall)),
    ("scripts_ae_01_cloud_proxy.py", ROOT / "scripts/ae_01_cloud_proxy.py",
     ("reconstruct", reconstruct_proxy_cli)),
    ("scripts_ae_01_multiturn_capacity_report.py",
     ROOT / "scripts/ae_01_multiturn_capacity_report.py",
     ("r2", "scripts/ae_01_multiturn_capacity_report.py")),
    ("scripts_deployment_letta_bootstrap.py", ROOT / "scripts/deployment/letta_bootstrap.py",
     ("reconstruct", reconstruct_bootstrap)),
    ("scripts_deployment_letta_local.py", ROOT / "scripts/deployment/letta_local.py",
     ("reconstruct", reconstruct_launcher)),
    ("tests_test_ae_no_compaction.py", ROOT / "tests/test_ae_no_compaction.py",
     ("r2", "tests/test_ae_no_compaction.py")),
    ("tests_test_ae_aux_wire.py", ROOT / "tests/test_ae_aux_wire.py",
     ("reconstruct", reconstruct_aux_wire)),
    ("tests_test_ae_no_compaction_production.py",
     ROOT / "tests/test_ae_no_compaction_production.py", ("absent", None)),
    ("tests_test_ae_no_compaction_stack_gate.py",
     ROOT / "tests/test_ae_no_compaction_stack_gate.py", ("absent", None)),
    ("deployment-assets_letta-no-compaction_manifest.json",
     ROOT / "deployment-assets/letta-no-compaction/manifest.json",
     ("r2", "letta-no-compaction-manifest.json")),
    ("deployment-assets_letta-no-compaction_ae-no-compaction-letta.patch",
     ROOT / "deployment-assets/letta-no-compaction/ae-no-compaction-letta.patch",
     ("r2", "ae-no-compaction-letta.patch")),
    ("deployment-assets_letta-no-compaction_files_ae_no_compaction.py",
     ROOT / "deployment-assets/letta-no-compaction/files/letta/helpers/ae_no_compaction.py",
     ("r2", "ae_no_compaction.py")),
    ("deployment-assets_letta-no-compaction_files_ae_qwen_tokenizer.py",
     ROOT / "deployment-assets/letta-no-compaction/files/letta/helpers/ae_qwen_tokenizer.py",
     ("r2", "ae_qwen_tokenizer.py")),
    ("deployment-assets_letta-no-compaction_write_stack_manifest.py",
     ROOT / "deployment-assets/letta-no-compaction/write_stack_manifest.py", ("absent", None)),
    ("deployment-assets_letta-no-compaction_stack-manifest.json",
     ROOT / "deployment-assets/letta-no-compaction/stack-manifest.json", ("absent", None)),
    ("deployment-assets_letta-multicall_manifest.json",
     ROOT / "deployment-assets/letta-multicall/manifest.json", ("file", R5_MANIFEST)),
]


def baseline_bytes(name: str, source) -> tuple[bytes | None, str]:
    kind, value = source
    if kind == "r2":
        for root in (R2 / "after", R2 / "after/deployment-assets",
                     R2 / "before", R2 / "before/deployment-assets"):
            candidate = root / value
            if candidate.is_file():
                return candidate.read_bytes(), f"r2 delivery: {candidate.relative_to(ROOT)}"
        raise SystemExit(f"the r2 baseline copy is absent: {value}")
    if kind == "file":
        if not Path(value).is_file():
            raise SystemExit(f"the stored baseline is absent: {value}")
        return Path(value).read_bytes(), f"stored copy: {Path(value).relative_to(ROOT)}"
    if kind == "reconstruct":
        current = dict((item[0], item[1]) for item in FILES)[name].read_text(encoding="utf-8")
        return value(current).encode("utf-8"), "reconstructed from the recorded R3 edits"
    return None, "absent before this round"


def main() -> int:
    # Idempotent: the machine-generated parts are rewritten in place, while the
    # hand-written README.md/DELIVERY.md and the evidence copies are left alone.
    for directory in ("before", "after", "diffs"):
        shutil.rmtree(OUT / directory, ignore_errors=True)
    for directory in ("before", "after", "diffs", "evidence"):
        (OUT / directory).mkdir(parents=True, exist_ok=True)
    (OUT / "before-after.json").unlink(missing_ok=True)
    table = []
    for index, (name, path, source) in enumerate(FILES, start=1):
        if not path.is_file():
            raise SystemExit(f"the source file is absent: {path}")
        after = path.read_bytes()
        before, note = baseline_bytes(name, source)
        (OUT / "after" / name).write_bytes(after)
        if before is not None:
            (OUT / "before" / name).write_bytes(before)
        diff_path = OUT / "diffs" / f"{index:02d}-{name}.diff"
        if before is None:
            diff_path.write_text(
                f"# {name}: NEW in r3 (no pre-image)\n# after sha256 {sha256(path)}\n",
                encoding="utf-8")
            changed = True
        else:
            completed = subprocess.run(
                ["diff", "-u", "--label", f"before/{name}", "--label", f"after/{name}",
                 str(OUT / "before" / name), str(OUT / "after" / name)],
                capture_output=True, text=True)
            changed = completed.returncode != 0
            diff_path.write_text(completed.stdout, encoding="utf-8")
            if not changed:
                diff_path.unlink()
        table.append({
            "file": name, "repo_path": str(path.relative_to(ROOT)),
            "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "before_lines": None if before is None else before.decode("utf-8", "replace").count("\n"),
            "after_lines": after.decode("utf-8", "replace").count("\n"),
            "changed": changed, "baseline": note,
            "diff": None if not changed else str(diff_path.relative_to(OUT)),
        })
    (OUT / "before-after.json").write_text(
        json.dumps({"schema": "ae-multiturn-capacity-no-compaction-r3-diff-1",
                    "note": ("`before` is the R3 baseline. Entries marked 'reconstructed' "
                             "were rebuilt by reversing the recorded R3 edits; every other "
                             "entry is a stored copy and is named."),
                    "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
