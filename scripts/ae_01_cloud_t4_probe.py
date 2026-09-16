#!/usr/bin/env python3
"""Single-t4 capability screening over a DECLARED cloud chat-completions API.

    .venv-vita/bin/python -B scripts/ae_01_cloud_t4_probe.py \
        --stage plan --config configs/ae-01__cloud-t4__declared-identity.json \
        --vita-source vendor/vita/source --output-dir deployment/runs/ae-cloud-t4-<UTC>

Scope: ONE original t4 (U000828 / sub_U000828_4), no post-hoc specification reminder, no
R/E, no capacity verification. The task construction, the 19 native tools, the native user
conversation and the deterministic private diagnosis are NOT re-implemented here: they are
`scripts/ae_01_local_t4_probe.py`'s, reached through the shared flow in `t4_flow.py`. This
module adds only the cloud transport adapter, the configuration contract and this entry.

What this probe will NOT do:

* it will not assume a third-party gateway is the official API. `base_url`, `wire_model`
  and the reasoning-mode field are DECLARED in the config; a placeholder or an omitted
  source is refused, so nothing here can silently guess an identity or a model mapping;
* it will not reuse the local vLLM field. `chat_template_kwargs` belongs to the local
  deployment; the cloud mode is whatever the config declares. A 400 that looks like a mode
  rejection is recorded verbatim and STOPS - there is no silent fallback to a thinking or
  a non-thinking default;
* it will not pretend to know tokens. A cloud endpoint is not assumed to expose `/tokenize`
  or `models.max_model_len`, so there is NO token capacity claim here: the operational guard
  is an explicitly declared REQUEST-BYTE budget, recorded as cost protection and never as a
  capacity guarantee. `verified` capacity states are not touched;
* it will not read a key outside `--stage run`, and never writes one anywhere: requests are
  saved WITHOUT the Authorization header, and every artifact is checked for the key.

    plan       zero network, no key needed
    preflight  identity probe only (GET models / optional tokenize): ZERO inference
    run        one bounded t4 through the shared flow
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "ae-cloud-t4-probe-0.1"
PURPOSE = "cloud_single_t4_capability_screen_not_capacity_verification"
STATUS_PLAN = "PLAN_FROZEN"
STATUS_PREFLIGHT_READY = "PREFLIGHT_READY"
STATUS_PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
STATUS_CAPACITY_BLOCKED = "REQUEST_BUDGET_BLOCKED"
STATUS_INVALID = "INVALID"
#: An empty string is handled by the explicit non-empty check, NOT as a substring marker
#: (matching "" would flag every value).
UNVERIFIED = ("REPLACE_ME", "UNVERIFIED", "TODO", "TBD")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _local_probe():
    """r3's probe, used as a library: task construction, tools, user chain, diagnosis."""
    return _load_module("ae_local_t4_probe_for_cloud",
                        ROOT / "scripts/ae_01_local_t4_probe.py")


def _flow():
    return _load_module("ae_t4_flow", ROOT / "t4_flow.py")


def _variants():
    """The opt-in case variants; the default path never calls into this."""
    return _load_module("ae_t4_case_variants", ROOT / "t4_case_variants.py")


# --------------------------------------------------------------------------- configuration


class ConfigRejected(ValueError):
    """The declared identity/mode is missing, unverified or incoherent."""


def _declared(value, label: str, *, allow_placeholder: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigRejected(f"{label} must be a nonempty string")
    text = value.strip()
    if not allow_placeholder and any(marker in text.upper() for marker in UNVERIFIED):
        raise ConfigRejected(
            f"{label} is still a placeholder ({text!r}); the API identity must be DECLARED "
            "by the operator from the endpoint's own documentation - this probe does not "
            "guess a base_url, a wire model or a mode field")
    return text


def _public_base_url(value: str, label: str) -> str:
    """https (or an explicit loopback http for a local stand-in), bare origin only."""
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http"):
        raise ConfigRejected(f"{label} must be an http(s) URL, got {value!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigRejected(f"{label} must be a bare origin (no path/query): {value!r}")
    if parts.scheme == "http":
        host = (parts.hostname or "").strip("[]")
        import ipaddress
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ConfigRejected(
                f"{label} uses plain http on a non-numeric host ({host!r}); a remote API "
                "must be https, and plain http is only accepted for a loopback stand-in"
            ) from None
        if not address.is_loopback:
            raise ConfigRejected(f"{label} uses plain http on a non-loopback address")
    if not parts.hostname:
        raise ConfigRejected(f"{label} has no host")
    return value.rstrip("/")


def load_config(path: Path) -> dict:
    """Read and validate the DECLARED identity/mode/budget contract.

    Every identity value must be declared by the operator and traceable to the endpoint's
    own documentation; the reasoning mode must say WHERE it came from. Nothing here is
    defaulted, because a wrong default would silently screen the wrong service.
    """
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ConfigRejected("the config must be a JSON object")
    unknown = set(document) - {"schema_version", "api", "wire_model", "mode", "capacity",
                               "identity", "source_note"}
    if unknown:
        raise ConfigRejected(f"unexpected config keys: {sorted(unknown)}")
    api = document.get("api")
    if not isinstance(api, dict):
        raise ConfigRejected("api must be an object")
    base_url = _public_base_url(_declared(api.get("base_url"), "api.base_url"),
                               "api.base_url")
    chat_path = _declared(api.get("chat_path"), "api.chat_path")
    if not chat_path.startswith("/"):
        raise ConfigRejected("api.chat_path must start with '/'")
    models_path = api.get("models_path")
    if models_path is not None and not str(models_path).startswith("/"):
        raise ConfigRejected("api.models_path must start with '/'")
    tokenize_path = api.get("tokenize_path")
    if tokenize_path is not None and not str(tokenize_path).startswith("/"):
        raise ConfigRejected("api.tokenize_path must start with '/'")
    wire_model = _declared(document.get("wire_model"), "wire_model")

    mode = document.get("mode")
    if not isinstance(mode, dict):
        raise ConfigRejected("mode must be an object describing the DECLARED reasoning mode")
    mode_name = _declared(mode.get("name"), "mode.name")
    mode_extra = mode.get("payload")
    if not isinstance(mode_extra, dict) or not mode_extra:
        raise ConfigRejected("mode.payload must be the nonempty object added to the request")
    if "chat_template_kwargs" in json.dumps(mode_extra):
        raise ConfigRejected(
            "mode.payload must not reuse the local vLLM field; declare the field the "
            "endpoint itself documents")
    mode_source = _declared(mode.get("source"), "mode.source")

    identity_document = document.get("identity") or {}
    if not isinstance(identity_document, dict):
        raise ConfigRejected("identity must be an object")
    identity_probe = identity_document.get("probe", "required")
    if identity_probe not in ("required", "not_exposed"):
        raise ConfigRejected("identity.probe must be 'required' or 'not_exposed'")
    if identity_probe == "not_exposed" and not identity_document.get("why"):
        raise ConfigRejected(
            "identity.probe='not_exposed' needs identity.why: the operator must say how the "
            "declared model was confirmed when the endpoint exposes no listing")
    identity_evidence = identity_document.get("evidence")
    if identity_probe == "required" and not identity_evidence:
        raise ConfigRejected(
            "identity.evidence is required: record WHERE the declared model came from "
            "(documentation URL, console, or the endpoint's own listing)")

    capacity = document.get("capacity") or {}
    if not isinstance(capacity, dict):
        raise ConfigRejected("capacity must be an object")
    if capacity.get("token_counting_available"):
        raise ConfigRejected(
            "this screen does not claim token capacity; leave token_counting_available "
            "false and declare a request-byte budget instead")
    budget = capacity.get("max_request_bytes")
    if type(budget) is not int or budget <= 0:
        raise ConfigRejected("capacity.max_request_bytes must be a positive integer "
                             "(the operational cost guard)")
    window = capacity.get("declared_context_window_tokens")
    if window is not None and (type(window) is not int or window <= 0):
        raise ConfigRejected("capacity.declared_context_window_tokens must be a positive "
                             "integer when present")
    return {
        "schema_version": document.get("schema_version") or SCHEMA_VERSION,
        "identity": {"probe": identity_probe, "why": identity_document.get("why"),
                     "evidence": identity_evidence},
        "api": {"base_url": base_url, "chat_path": chat_path,
                "models_path": models_path, "tokenize_path": tokenize_path},
        "wire_model": wire_model,
        "mode": {"name": mode_name, "payload": mode_extra, "source": mode_source,
                 "is_local_vllm_field": False},
        "capacity": {"token_counting_available": False,
                     "max_request_bytes": budget,
                     "declared_context_window_tokens": window,
                     "declared_context_window_is_verified": False,
                     "basis": "declared_request_byte_budget_not_a_capacity_guarantee"},
        "source_note": document.get("source_note"),
        "config_path": str(path),
    }


# ------------------------------------------------------------------------------- secrets


def read_key(path: Path) -> str:
    """Read the run-time key from a 0600 file; never log it, never copy it anywhere."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"the key file is absent: {path}")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConfigRejected(f"the key file {path} is mode {oct(mode)}; it must be 0600")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise ConfigRejected("the key file is empty")
    return key


def redacted_request(url: str, method: str, body: bytes | None) -> dict:
    """The request record that is safe to keep: NO Authorization header, ever."""
    return {"method": method, "url": url, "body_bytes": None if body is None else len(body),
            "headers": {"Content-Type": "application/json"} if body is not None else {},
            "authorization_header_redacted": True,
            "body_sha256": _sha256_bytes(body) if body is not None else None}


def _sha256_bytes(raw: bytes) -> str:
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def assert_key_absent(root: Path, key: str) -> dict:
    """Prove no artifact under `root` contains the key."""
    hits = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        try:
            if key.encode("utf-8") in path.read_bytes():
                hits.append(str(path))
        except OSError:
            continue
    if hits:
        raise RuntimeError(f"the key appears in artifacts: {hits}")
    return {"key_absent_from_artifacts": True, "files_checked":
            sum(1 for p in Path(root).rglob("*") if p.is_file())}


# ------------------------------------------------------------------------------ transport


class CloudTransport:
    """The counted send gate over a declared cloud chat-completions endpoint.

    The interface is the one the shared flow expects (`send`, `declare_window`,
    `descriptor`, `attempted`, `responses_received`, `by_role`, `journal`, `gate_refusals`),
    so the flow needs no cloud-specific branch.

    There is NO token counting here: a cloud endpoint is not assumed to expose `/tokenize`.
    The operational guard is the DECLARED request-byte budget, reported as cost protection.
    """

    def __init__(self, config: dict, *, api_key: str | None, opener=None,
                 clock=None, timeout: float = 180.0,
                 max_inference_posts: int | None = None, totals: dict | None = None):
        import time
        self.config = config
        self.api_key = api_key
        self.opener = opener if opener is not None else build_opener(ProxyHandler({}))
        self.clock = clock or time.monotonic
        self.timeout = timeout
        totals = totals or {}
        self.max_inference_posts = (max_inference_posts if max_inference_posts is not None
                                    else totals.get("total_max_posts"))
        self.max_request_bytes = config["capacity"]["max_request_bytes"]
        self.journal = []
        self.gate_refusals = []
        self.attempted = 0
        self.responses_received = 0
        self.by_role = {"agent": 0, "user": 0, "models": 0, "tokenize": 0}
        self.request_bytes_total = 0
        self.largest_request_bytes = 0
        self.unverified_mode_fields = True
        self.mode_was_rejected_by_the_service = False
        self.service_window = None
        self.window_source = "unknown_cloud_endpoint_has_no_token_dictionary"
        self.model_listing = None
        self.inference_detail = []

    # ------------------------------------------------------------------ bookkeeping

    @property
    def inference_posts(self) -> int:
        return self.by_role["agent"] + self.by_role["user"]

    def descriptor(self) -> dict:
        return {"service_window_tokens": self.service_window,
                "service_window_source": self.window_source,
                "capacity_basis": "declared_request_byte_budget",
                "capacity_is_a_guarantee": False,
                "token_counting_available": False,
                "request_bytes_total": self.request_bytes_total,
                "largest_request_bytes": self.largest_request_bytes,
                "max_request_bytes": self.max_request_bytes,
                "wire_model": self.config["wire_model"],
                "base_url": self.config["api"]["base_url"],
                "mode": {"name": self.config["mode"]["name"],
                         "payload": self.config["mode"]["payload"],
                         "source": self.config["mode"]["source"]},
                "mode_was_rejected_by_the_service": self.mode_was_rejected_by_the_service,
                "mode_was_verified_against_documentation": False,
                "usage_by_role": dict(self.by_role),
                "inference_detail": self.inference_detail}

    def budget_left(self, *, inference: bool) -> bool:
        if inference and self.max_inference_posts is not None:
            return self.inference_posts < self.max_inference_posts
        return True

    def _raw(self, method, url, body, role, *, auth=True):
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if auth and self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = Request(url, data=body, headers=headers, method=method)
        started = self.clock()
        self.attempted += 1
        self.by_role[role] = self.by_role.get(role, 0) + 1
        record = {"role": role, "method": method, "url": url, "attempted": True,
                  "request": redacted_request(url, method, body),
                  "elapsed_seconds": None, "response_received": False}
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status, raw = response.code, response.read()
        except HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001 - the status alone is still evidence
                raw = b""
            status = exc.code
        except Exception as exc:  # noqa: BLE001 - a transport failure is evidence, no retry
            record.update(error_type=type(exc).__name__, error_detail=str(exc)[:500],
                          elapsed_seconds=round(self.clock() - started, 3))
            self.journal.append(record)
            return None, None, {"error_type": type(exc).__name__,
                                "error_detail": str(exc)[:500]}
        self.responses_received += 1
        record.update(response_received=True, http_status=status,
                      response_sha256=_sha256_bytes(raw),
                      response_bytes=len(raw),
                      elapsed_seconds=round(self.clock() - started, 3))
        self.journal.append(record)
        if role == "agent" or role == "user":
            self.inference_detail.append(dict(record))
        return status, raw, None

    # ------------------------------------------------------------------ identity probe

    def read_model_listing(self) -> dict:
        """GET the model listing (zero inference) to confirm the DECLARED identity."""
        if self.model_listing is not None:
            return self.model_listing
        path = self.config["api"].get("models_path")
        record = {"checked": False, "path": path}
        if path:
            status, raw, failure = self._raw(
                "GET", self.config["api"]["base_url"] + path, None, "models")
            record.update(http_status=status, failure=failure)
            if failure is None and status == 200:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    entries = parsed.get("data")
                    ids = [entry.get("id") for entry in entries
                           if isinstance(entry, dict)] if isinstance(entries, list) else []
                    record.update(checked=True, models=ids,
                                  declared_model_present=self.config["wire_model"] in ids)
                except (ValueError, AttributeError, TypeError) as exc:
                    record.update(parse_error=f"{type(exc).__name__}: {exc}")
            elif failure is None:
                record.update(body_utf8=raw[:500].decode("utf-8", "replace"))
        self.model_listing = record
        return record

    def declare_window(self):
        """There is no token window claim for a cloud endpoint.

        The shared flow calls this before sending; for this transport it only performs the
        identity probe and returns `None` - the operational gate is the declared request-byte
        budget, which `send` enforces.
        """
        self.read_model_listing()
        return None

    # ------------------------------------------------------------------- the send gate

    def send(self, messages, tools, *, role, max_tokens, temperature=0.0):
        """Build -> byte-budget gate -> send. Raises GateRefused BEFORE any generation."""
        probe = _local_probe()
        body = dict(probe.chat_body(messages, tools, max_tokens=max_tokens,
                                    temperature=temperature))
        # The DECLARED mode replaces the local vLLM field: this transport must never send
        # chat_template_kwargs to a cloud API.
        body.pop("chat_template_kwargs", None)
        body["model"] = self.config["wire_model"]
        body.update(self.config["mode"]["payload"])
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        gate = {"role": role, "count": None, "reserve": max_tokens,
                "window": None, "window_source": self.window_source,
                "capacity_basis": "declared_request_byte_budget",
                "request_bytes": len(raw), "max_request_bytes": self.max_request_bytes}
        if len(raw) > self.max_request_bytes:
            gate["fits"] = False
            self.gate_refusals.append(gate)
            raise probe.GateRefused(
                "request_byte_budget_exhausted",
                f"request is {len(raw)} bytes, the declared budget is "
                f"{self.max_request_bytes}")
        if not self.budget_left(inference=True):
            gate["fits"] = False
            self.gate_refusals.append(gate)
            raise probe.GateRefused(
                "total_post_budget_exhausted",
                f"inference posts {self.inference_posts}/{self.max_inference_posts} used")
        gate["fits"] = True
        self.gate_refusals.append(gate)
        self.request_bytes_total += len(raw)
        self.largest_request_bytes = max(self.largest_request_bytes, len(raw))
        url = self.config["api"]["base_url"] + self.config["api"]["chat_path"]
        status, reply, failure = self._raw("POST", url, raw, role)
        if status == 400:
            # A mode field the endpoint rejects is recorded verbatim: there is NO silent
            # fallback to a thinking or a non-thinking default.
            self.mode_was_rejected_by_the_service = True
            self.journal[-1]["possible_mode_rejection"] = True
        return status, reply, failure, gate


# ----------------------------------------------------------------------------------- plan


def build_plan(config: dict, vita_source: Path, *, case_variant: str | None = None) -> dict:
    probe = _local_probe()
    public = probe.prepare_public(vita_source)
    inputs = public["inputs"]
    return {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": STATUS_PLAN,
        "diagnostic_only": True, "network_called": False, "model_called": False,
        "task_success": None, "scientific_result": None,
        "scope": {
            "one_original_t4": case_variant is None,
            "case_variant": case_variant,
            "default_path": case_variant is None,
            "task_number": probe.TASK_NUMBER,
            "subtask_id": probe.TASK_ID,
            "post_hoc_specification_reminder_used": False,
            "full_r_e_started": False,
            "capacity_verification": False,
            "this_is": "single short-task capability screen",
            "this_is_not": ["a 1M/context-capacity verification",
                            "an R/E run", "a benchmark re-run"],
        },
        "api_identity": {
            "declared_by_operator": True,
            "base_url": config["api"]["base_url"],
            "chat_path": config["api"]["chat_path"],
            "models_path": config["api"].get("models_path"),
            "wire_model": config["wire_model"],
            "model_weights_version_claimed": False,
            "config_path": config["config_path"],
            "third_party_gateway_assumed_official": False,
        },
        "reasoning_mode": {
            "declared": True, "name": config["mode"]["name"],
            "payload": config["mode"]["payload"], "source": config["mode"]["source"],
            "local_vllm_field_reused": False,
            "silent_fallback_on_rejection": False,
            "verified_by_this_probe": False,
        },
        "capacity": {"token_counting_available": False,
                     "token_count_is_unknown": True,
                     "max_request_bytes": config["capacity"]["max_request_bytes"],
                     "request_byte_budget_is": "a cost guard, not a capacity guarantee",
                     "declared_context_window_tokens":
                         config["capacity"].get("declared_context_window_tokens"),
                     "declared_window_is_verified": False,
                     "verified_capacity_fields_touched": False,
                     "qwen_tokenizer_reused": False,
                     "bytes_over_four_token_estimate_used": False},
        "case_variant": case_variant,
        "task": {"instruction": inputs["task"]["instruction"],
                 "current_state_execution_diagnostic": True,
                 "dataset_history_sent": False,
                 "facts_current": json.loads(json.dumps(inputs["facts_current"]))},
        "private_oracle": {"injected_into_agent": False,
                           "target_product_id": probe.TARGET_PRODUCT_ID,
                           "expected_sugar": probe.EXPECTED_SUGAR,
                           "work_address": inputs["profile"][probe.WORK_ADDRESS_KEY],
                           "used_for_scoring_only": True},
        "budget": {"agent_max_requests": probe.AGENT_MAX_REQUESTS,
                   "user_simulator_max_exchanges": probe.AUX_MAX_REQUESTS,
                   "total_model_posts_max": probe.TOTAL_MAX_POSTS,
                   "agent_output_tokens": probe.AGENT_OUTPUT_TOKENS,
                   "auxiliary_output_tokens": probe.AUX_OUTPUT_TOKENS,
                   "temperature": probe.AGENT_TEMPERATURE,
                   "serial": True, "transport_retries": 0,
                   "request_timeout_seconds": 180.0,
                   "max_tool_return_chars": probe.MAX_TOOL_RETURN_CHARS,
                   "no_resume_same_directory": True},
        "stop_rules": [
            "non-200, an unanswered request, an empty answer, finish_reason=length and an "
            "uncertain tool outcome each STOP the run; nothing retries",
            "a 400 that looks like a rejected mode field is recorded verbatim and stops; "
            "the mode is never silently changed",
        ],
        "reuse": {"agent_loop": "t4_flow.run_agent_loop",
                  "task_construction": "scripts/ae_01_local_t4_probe.py (imported as a library)",
                  "native_user_simulator": "vita.user.user_simulator.UserSimulator",
                  "diagnosis": "ae_capability.diagnose_orders",
                  "copied_or_reimplemented": False},
        "provenance": {"code_sha256": {
            name: _sha256_bytes((ROOT / name).read_bytes())
            for name in ("t4_flow.py", "scripts/ae_01_cloud_t4_probe.py",
                         "scripts/ae_01_local_t4_probe.py")},
            "python": sys.version},
    }


def preflight(config: dict, *, key: str | None = None, transport=None) -> dict:
    """Identity probe only: ZERO inference, and no token-capacity claim.

    A caller may inject the transport (offline fixtures do); the model listing is then taken
    through the SAME transport the run will use, so the identity the run relies on is the
    identity that was probed.
    """
    transport = transport if transport is not None else CloudTransport(config, api_key=key)
    policy = config["identity"]["probe"]
    listing = transport.read_model_listing() if policy == "required" else {
        "checked": False, "skipped": "the config declares identity.probe='not_exposed'",
        "why": config["identity"]["why"], "evidence": config["identity"]["evidence"]}
    report = {"schema_version": SCHEMA_VERSION, "stage": "preflight",
              "network_called": transport.attempted > 0,
              "model_inference_called": False,
              "base_url": config["api"]["base_url"], "wire_model": config["wire_model"],
              "mode": config["mode"],
              "model_listing": listing,
              "capacity": {"token_counting_available": False,
                           "max_request_bytes": config["capacity"]["max_request_bytes"],
                           "note": "no token count is claimed for a cloud endpoint"},
              "status": STATUS_PREFLIGHT_READY, "missing": [], "warnings": [],
              "claims_token_capacity": False, "claims_full_task_fits": False,
              "transport_journal": [{k: v for k, v in row.items() if k != "request"}
                                    for row in transport.journal]}
    if policy == "not_exposed":
        report["identity_policy"] = "not_exposed"
        report["warnings"].append(
            "the endpoint exposes no listing; the declared model rests on the operator's "
            "declared evidence and is NOT confirmed by this probe")
    elif not listing.get("checked"):
        report["status"] = STATUS_PREFLIGHT_BLOCKED
        report["missing"].append("model_listing")
    elif listing.get("declared_model_present") is not True:
        report["status"] = STATUS_PREFLIGHT_BLOCKED
        report["missing"].append("declared_wire_model_not_listed")
        report["warning_detail"] = (
            "the declared wire model is not in the endpoint's own listing; the identity "
            "must be re-declared by the operator, not remapped here")
    if config["mode"]["source"].lower().startswith(("http", "doc", "见", "文档")):
        report["warnings"].append(
            "the mode source is a citation, not a verification performed by this probe")
    return report


# ------------------------------------------------------------------------------------ run


def run(*, config: dict, vita_source: Path, key: str, output_dir: Path,
        transport=None, case_variant: str | None = None) -> dict:
    """One bounded t4 over the declared endpoint, through the SHARED flow."""
    probe = _local_probe()
    flow = _flow()
    out = probe.ensure_output_dir(output_dir)
    plan = build_plan(config, vita_source, case_variant=case_variant)
    probe.save(out / "plan.json", plan)
    run_transport = transport if transport is not None else CloudTransport(
        config, api_key=key, totals={"total_max_posts": probe.TOTAL_MAX_POSTS})
    readiness = preflight(config, key=key, transport=run_transport)
    probe.save(out / "preflight.json", readiness)
    public = probe.prepare_public(vita_source)
    native = probe.build_native(public, vita_source)
    probe.save(out / "case.json", {
        "task_construction_reused_from": "scripts/ae_01_local_t4_probe.py",
        "agent_system_message": probe.agent_system_message(native, public),
        "agent_user_message": probe.task_user_message(public),
        "exposed_tool_names": sorted(native["bindings"]),
        "api_identity": plan["api_identity"], "reasoning_mode": plan["reasoning_mode"],
        "private_oracle": plan["private_oracle"],
        "post_hoc_specification_reminder_used": False,
    })
    result = {
        "schema_version": SCHEMA_VERSION, "purpose": PURPOSE, "status": STATUS_INVALID,
        "diagnostic_only": True, "stage": "run",
        "base_url": config["api"]["base_url"], "wire_model": config["wire_model"],
        "mode": config["mode"], "task_success": None, "scientific_result": None,
        "oracle_passed": None, "model_called": False, "user_exchanges": 0, "steps": [],
        "baseline_order_ids": [], "created_order_ids": [], "paid_order_ids": [],
        "tool_returns": [], "capacity": [], "errors": [], "gate_refusals": [],
        "service_window_tokens": None, "service_window_source": None,
        "capacity_basis": "declared_request_byte_budget",
        "capacity_is_a_guarantee": False, "token_counting_available": False,
        "usage_by_role": {}, "case_variant": case_variant,
        "started_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "total_model_posts": 0, "total_model_posts_within_budget": True,
        "service_posts_total": 0, "service_posts_including_tokenize": 0, "wire_journal": [],
    }
    if readiness["status"] != STATUS_PREFLIGHT_READY:
        result["status"] = STATUS_PREFLIGHT_BLOCKED
        result["stop_reason"] = "preflight_" + ",".join(readiness["missing"])
        result["preflight"] = readiness
        probe.save(out / "result.json", result)
        return result

    kinds = native["messages"]
    base_system = probe.agent_system_message(native, public)
    tools = native["tools"]
    variant = None
    if case_variant is not None:
        variants = _variants()
        if case_variant != variants.VARIANT_ID:
            raise ValueError(f"unknown case variant {case_variant!r}; "
                             f"this probe implements {variants.VARIANT_ID!r}")
        built = variants.build_variant(native["environment"], base_system, tools)
        base_system, tools, variant = (built["system_message"], built["tools"],
                                       built["record"])
        probe.save(out / "case-variant.json", variant)
    messages = [kinds["SystemMessage"](role="system", content=base_system),
                kinds["UserMessage"](role="user", content=probe.task_user_message(public))]
    baseline = _load_module("ae_native_order_probe_for_cloud",
                            ROOT / "scripts/ae_01_native_order_probe.py").case_snapshot(
        native["environment"])["orders"]
    result["baseline_order_ids"] = sorted(baseline)
    probe.save(out / "initial-database.json",
               _load_module("ae_native_order_probe_for_cloud",
                            ROOT / "scripts/ae_01_native_order_probe.py").case_snapshot(
                   native["environment"]))
    native_user = probe.NativeUser(run_transport, public, vita_source, native=native)
    public_view = dict(public, work_address_key=probe.WORK_ADDRESS_KEY,
                       user_id=probe.USER_ID, target_product_id=probe.TARGET_PRODUCT_ID,
                       expected_sugar=probe.EXPECTED_SUGAR,
                       status_passed=probe.STATUS_PASSED,
                       status_incomplete=probe.STATUS_INCOMPLETE)
    helpers = {"native_module": _load_module(
                   "ae_native_order_probe_for_cloud",
                   ROOT / "scripts/ae_01_native_order_probe.py"),
               "kinds": kinds, "status_invalid": STATUS_INVALID,
               "status_length": probe.STATUS_LENGTH,
               "status_uncertain": probe.STATUS_UNCERTAIN,
               "status_capacity": STATUS_CAPACITY_BLOCKED,
               "gate_refused": probe.GateRefused,
               "guarded_tool_return": probe.guarded_tool_return,
               "total_max_posts": probe.TOTAL_MAX_POSTS, "save": probe.save}
    flow.run_agent_loop(runtime={"transport": run_transport, "native_user": native_user},
                        out=out, result=result, messages=messages, tools=tools,
                        bindings=native["bindings"], helpers=helpers, native=native,
                        public=public_view, max_agent_requests=probe.AGENT_MAX_REQUESTS,
                        aux_max_requests=probe.AUX_MAX_REQUESTS,
                        agent_output_tokens=probe.AGENT_OUTPUT_TOKENS,
                        agent_temperature=probe.AGENT_TEMPERATURE,
                        stop_marker=probe.STOP_MARKER)
    if variant is not None:
        result["case_variant"] = variant["case_variant"]  # same value, now recorded
        result["interventions"] = variant["interventions"]
        result["two_factors_are_changed_together"] = True
        result["can_attribute_to_one_factor_alone"] = False
        result["post_hoc_exploratory_control"] = True
        for marker in variant["superseded_markers_not_inherited"]:
            result.pop(marker, None)
        # the first request as actually sent, without the key, for byte-level comparison
        first = result.get("steps") or [{}]
        probe.save(out / "first-request.json", {
            "case_variant": variant["case_variant"],
            "model": config["wire_model"], "mode": config["mode"],
            "notes": "the first request as the transport built it; no Authorization header",
            "step0": first[0],
            "agent_system_message_sha256": _sha256_bytes(base_system.encode("utf-8")),
            "tools_count": len(tools),
        })
    # The key must not be anywhere in what we keep.
    result["secret_hygiene"] = assert_key_absent(out, key)
    probe.save(out / "result.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=("plan", "preflight", "run"), default="plan")
    parser.add_argument("--config", required=True, type=Path,
                        help="the DECLARED identity/mode/budget contract")
    parser.add_argument("--vita-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--key-file", type=Path,
                        default=ROOT / "deployment/private/deepseek.key",
                        help="read ONLY for preflight/run; never logged or copied")
    parser.add_argument("--case-variant", default=None,
                        help="explicit opt-in case variant; omit for the ORIGINAL default "
                             "path (the only implemented variant is t4-interface-clarified-v1)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.stage == "plan":
            out = _local_probe().ensure_output_dir(args.output_dir)
            plan = build_plan(config, args.vita_source,
                              case_variant=args.case_variant)
            _local_probe().save(out / "plan.json", plan)
            print(f"AE cloud t4 PLAN; no network/model; base={plan['api_identity']['base_url']}; "
                  f"model={plan['api_identity']['wire_model']}; "
                  f"mode={plan['reasoning_mode']['name']}; output={out}")
            return 0
        key = read_key(args.key_file)
        if args.stage == "preflight":
            out = _local_probe().ensure_output_dir(args.output_dir)
            report = preflight(config, key=key)
            _local_probe().save(out / "preflight.json", report)
            print(f"AE cloud t4 PREFLIGHT {report['status']}; "
                  f"inference_called={report['model_inference_called']}; output={out}")
            return 0 if report["status"] == STATUS_PREFLIGHT_READY else 3
        result = run(config=config, vita_source=args.vita_source, key=key,
                     output_dir=args.output_dir, case_variant=args.case_variant)
        print(f"AE cloud t4 {result['status']}; task_success={result['task_success']}; "
              f"posts={result.get('total_model_posts')}; scientific_result=null; "
              f"output={args.output_dir}")
        return 0
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - reported, not hidden
        message = f"{type(exc).__name__}: {exc}"
        print(f"AE cloud t4 INVALID: {message}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
