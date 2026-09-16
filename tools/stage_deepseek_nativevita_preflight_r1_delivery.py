#!/usr/bin/env python3
"""Stage the NativeVita DeepSeek-preflight delivery r1.

    .venv-vita/bin/python tools/stage_deepseek_nativevita_preflight_r1_delivery.py

Baselines are per file and marked in `before-after.json`: the r3 delivery copy, a stored
capture from an earlier round, or a pre-image RECONSTRUCTED by reversing the recorded
edits (guarded by exact-match assertions). The output directory is exclusive: an
existing one is refused, and no earlier delivery is touched.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R3 = ROOT / "results/ae-deepseek-re-multiturn-integration-r3"
PAIR_CLI_BASELINE = ROOT / "results/ae-cloud-multicall-20260913-r5/scripts/ae_01_cloud_re_pair.py"
TASK_RUN_BASELINE = ROOT / "results/ae-task-wiring-evidence-20260911-r1/scripts/ae_01_task_run.py"
OUT = ROOT / "results/ae-deepseek-nativevita-preflight-r1"

FILES = [
    ("ae_vita.py", "ae_vita.py", ("reconstruct", "ae_vita")),
    ("scripts_ae_01_cloud_re_multiturn.py", "scripts/ae_01_cloud_re_multiturn.py",
     ("r3", None)),
    ("scripts_ae_01_cloud_re_pair.py", "scripts/ae_01_cloud_re_pair.py",
     ("stored", PAIR_CLI_BASELINE,
      "stored capture; verified to differ only by this round's edit")),
    ("scripts_ae_01_capability_probe.py", "scripts/ae_01_capability_probe.py",
     ("reconstruct", "ae_01_capability_probe")),
    ("scripts_ae_01_task_run.py", "scripts/ae_01_task_run.py",
     ("stored", TASK_RUN_BASELINE,
      "stored capture; verified to differ only by this round's edit")),
    ("tests_test_ae_deepseek_native_preflight.py",
     "tests/test_ae_deepseek_native_preflight.py", ("new", None)),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(text: str, snippet: str) -> None:
    if text.count(snippet) != 1:
        raise SystemExit(f"the recorded edit is not present exactly once: {snippet[:70]!r}")


def reconstruct_ae_vita(text: str) -> str:
    """Undo the NativeVita DeepSeek-transport wiring."""
    # 1) the added contract helpers and the widened NATIVE_MODELS note
    added_helpers = text[text.index("# Explicit whitelist of inspected local checkpoints"):
                         text.index("_ACTIVE = threading.local()")]
    old_note = '''# Explicit whitelist of inspected local checkpoints for the derived driver. The
# default Qwen3-8B path is unchanged; Qwen3-4B-Instruct-2507 is added only for
# the separate single-task capability probe.
NATIVE_MODELS = ("Qwen3-8B", "Qwen3-4B-Instruct-2507")


'''
    if "def native_model_config(" not in added_helpers:
        raise SystemExit("the added helper block is not where it was staged")
    text = text.replace(added_helpers, old_note)
    # 2) the loader contract
    new_loader = '''def _load_native(source: Path, config_path: Path, model_base: str, model: str,
                 required_request_fields=None):
    """Load only after a caller-created, dummy-key-only local config is checked.

    The accepted entry is built by `native_model_config_entry`, so a model-specific
    transport's required request fields (its mode) must be present in the run-local
    config as `extra_body`; a config that omits them, or adds anything else, is refused
    instead of being loaded and then silently sending a different request shape.
    """
    import yaml
    entry = native_model_config_entry(model=model, model_base=model_base,
                                      required_request_fields=required_request_fields)
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    expected = {"default": {}, "models": [entry]}
    if cfg != expected:
        raise NativeVitaError("Vita config must contain only the selected local model, "
                              "its declared request fields and api_key=EMPTY")'''
    old_loader = '''def _load_native(source: Path, config_path: Path, model_base: str, model: str):
    """Load only after a caller-created, dummy-key-only local config is checked."""
    import yaml
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    expected = {"default": {}, "models": [{"name": model, "base_url": model_base,
                                           "api_key": "EMPTY"}]}
    if cfg != expected:
        raise NativeVitaError("Vita config must contain only the selected local model and api_key=EMPTY")'''
    _require(text, new_loader)
    text = text.replace(new_loader, old_loader)
    new_loaded = '''    loaded_entry = {key: value for key, value in entry.items() if key != "name"}
    if (Path(config._models_yaml_path).resolve() != config_path
            or config.models != {"default": {}, model: loaded_entry}):'''
    old_loaded = '''    if (Path(config._models_yaml_path).resolve() != config_path
            or config.models != {"default": {}, model: {"base_url": model_base, "api_key": "EMPTY"}}):'''
    _require(text, new_loaded)
    text = text.replace(new_loaded, old_loaded)
    # 3) the constructor's transport/model choice
    new_head = '''        # The transport contract comes from the DECLARED profile: the sealed compatibility
        # profile and the model-specific DeepSeek transport are both cloud egress through
        # the local proxy, while `local-vllm` stays the historical local path. Nothing is
        # disguised: the model must be one the declared profile names, and the fields the
        # profile requires travel with every native call.
        transport = declared_transport(transport_profile)
        cloud = transport is not None
        required_request_fields = (dict(transport.required_request_fields)
                                   if cloud else {})
        # Explicit, bounded scope only.'''
    old_head = '''        from ae_cloud_proxy import COMPAT_PROFILE, MODEL
        cloud = transport_profile == COMPAT_PROFILE
        if transport_profile not in ("local-vllm", COMPAT_PROFILE):
            raise NativeVitaError("unknown explicit transport profile")
        # Explicit, bounded scope only.'''
    _require(text, new_head)
    text = text.replace(new_head, old_head)
    new_choice = '''        validate_end_turn(end_turn)
        native_model_choice(transport_profile, model_name, language)'''
    old_choice = '''        validate_end_turn(end_turn)
        allowed_models = (MODEL,) if cloud else NATIVE_MODELS
        if model_name not in allowed_models or language != "chinese":
            raise NativeVitaError("native model must be an inspected Qwen3 checkpoint with Chinese prompts")'''
    _require(text, new_choice)
    text = text.replace(new_choice, old_choice)
    # 4) the loader call, the provenance addition and the stored required fields
    new_call = '''        self._native = _load_native(source, config_path, model_base, model_name,
                                    required_request_fields=required_request_fields)'''
    old_call = '''        self._native = _load_native(source, config_path, model_base, model_name)'''
    _require(text, new_call)
    text = text.replace(new_call, old_call)
    new_field = '''                                 "root_persona_equals_t3_profile": True})
        #: The declared transport's own required request fields (its mode). They are
        #: carried by every native call through the run-local model config, exactly like
        #: the agent path carries them through the proxy profile.
        self._required_request_fields = deepcopy(required_request_fields)'''
    old_field = '''                                 "root_persona_equals_t3_profile": True})'''
    _require(text, new_field)
    text = text.replace(new_field, old_field)
    new_prov = '''        if cloud:
            self._args.pop("seed")
            self._provenance.update({
                "transport_profile": transport_profile,
                "generation_seed_sent": False,
                "simulation_seed_metadata_only": seed,
                # What the declared transport REQUIRES on the wire, recorded by field
                # name: the native calls carry exactly this and nothing is inferred.
                "declared_request_fields": sorted(required_request_fields)})'''
    old_prov = '''        if cloud:
            self._args.pop("seed")
            self._provenance.update({"transport_profile": transport_profile,
                                     "generation_seed_sent": False,
                                     "simulation_seed_metadata_only": seed})'''
    _require(text, new_prov)
    text = text.replace(new_prov, old_prov)
    # 5) the auxiliary call arguments
    new_args = '''    def _llm_args(self, role):
        args = dict(deepcopy(self._args), extra_headers={"X-AE-Role": role})
        if self._required_request_fields:
            # The declared transport's own fields (its explicit mode) travel with every
            # native user/judge call through the SAME egress and parameter contract the
            # run-local model config declares - the pinned client merges `extra_body`
            # into the request it sends, so a request missing the mode is never sent.
            args["extra_body"] = deepcopy(self._required_request_fields)
        return args'''
    old_args = '''    def _llm_args(self, role):
        return dict(deepcopy(self._args), extra_headers={"X-AE-Role": role})'''
    _require(text, new_args)
    text = text.replace(new_args, old_args)
    return text


def reconstruct_ae_01_capability_probe(text: str) -> str:
    """Undo this round's model-config writer change in the capability CLI."""
    new = '''        from ae_vita import native_model_config
        save(native_config, native_model_config(config))'''
    old = '''        save(native_config, {"default": {}, "models": [{"name": config["expected_model"],
             "base_url": config["model_origin"] + "/v1", "api_key": "EMPTY"}]})'''
    _require(text, new)
    return text.replace(new, old)


RECONSTRUCTORS = {"ae_vita": reconstruct_ae_vita,
                  "ae_01_capability_probe": reconstruct_ae_01_capability_probe}
SOURCES = {"ae_vita": "ae_vita.py",
           "ae_01_capability_probe": "scripts/ae_01_capability_probe.py"}


def baseline_for(spec, name: str):
    kind, argument = spec[0], spec[1]
    if kind == "new":
        return None, "new in this round", "no pre-image exists; first shipped here"
    if kind == "r3":
        path = R3 / "after" / name
        if not path.is_file():
            raise SystemExit(f"the r3 delivery has no {name!r} pre-image")
        return path.read_bytes(), "r3 delivery copy", "the r3 delivery's own after/ copy"
    if kind == "stored":
        path, note = Path(argument), spec[2]
        if not path.is_file():
            raise SystemExit(f"the stored baseline is absent: {path}")
        return path.read_bytes(), "stored capture", note
    if kind == "reconstruct":
        text = (ROOT / SOURCES[argument]).read_text(encoding="utf-8")
        return (RECONSTRUCTORS[argument](text).encode("utf-8"),
                "reconstructed pre-image",
                "the recorded edits reversed, guarded by an exact-match assertion")
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
        {"schema": "ae-deepseek-nativevita-preflight-diff-1",
         "note": ("The on-site 0.4 deployment succeeded, but the REAL CLI PREFLIGHT stopped "
                  "at `NativeVitaError: unknown explicit transport profile` because "
                  "`ae_vita.NativeVita` accepted only `local-vllm` and the sealed "
                  "compatibility profile. This round wires the DECLARED profile through the "
                  "native initialisation and the auxiliary model config: the transport "
                  "contract (wire model, model set, required request fields) is read from "
                  "the proxy's own profile table - DeepSeek is not disguised as the old "
                  "profile or as a Qwen model - and the run-local `vita-models.json` is "
                  "built by ONE builder that the loader itself checks, so the declared "
                  "non-thinking mode travels with every native call. The historical local "
                  "and compatibility paths are byte-identical, and no service patch, "
                  "manifest, receipt or experiment definition is touched."),
         "files": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(table)} files in {OUT}")
    for row in table:
        print(f"  {'changed' if row['changed'] else 'same   '} {row['file']} ({row['baseline_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
