"""Explicit cloud execution policy; no network, secrets or upstream patches.

The context window is a local Agent budget, NOT returned server metadata or a
capacity measurement. Legacy LLMConfig is deliberately used on pinned Letta
0.16.8, whose create path accepts it without catalog resolution.
"""
from copy import deepcopy

from ae_cloud_proxy import MODEL, COMPAT_PROFILE
from ae_probe import _origin

TASK_PROFILE = "ae-cloud-capability-0.1"
PROBE_PROFILE = "ae-cloud-connection-0.1"

# The one explicit, reviewed low-frequency pacing combination. Only a capability
# config carrying exactly this marker may use a larger driver I/O timeout; the
# proxy upstream timeout stays 180 and the send interval is 65 seconds. The
# legacy validators and every existing config keep timeout_seconds=180.
PACING_CANDIDATE = {
    "driver_io_timeout_seconds": 900,
    "proxy_upstream_io_timeout_seconds": 180,
    "min_interval_seconds": 65,
}


class CloudExecutionProfile:
    def __init__(self, kind):
        if kind not in ("capability", "connection"):
            raise ValueError("unknown cloud execution kind")
        self.kind = kind

    def validate(self, config):
        # Reuse legacy exact limit checks, but only on a COPY converted to that
        # profile. Execution always uses the explicit original cloud config.
        from ae_capability import validate_config as capability_validate
        from ae_probe import validate_config as probe_validate
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        c = deepcopy(config)
        expected = TASK_PROFILE if self.kind == "capability" else PROBE_PROFILE
        if c.get("schema_version") != expected or c.get("expected_model") != MODEL:
            raise ValueError("wrong cloud schema/model")
        if c.get("model_handle") != "vllm/" + MODEL:
            raise ValueError("wrong explicit legacy handle")
        if c.pop("transport_profile", None) != COMPAT_PROFILE:
            raise ValueError("explicit compatibility transport profile required")
        if c.pop("context_window_source", None) != "local_agent_budget_not_server_measurement":
            raise ValueError("context budget provenance required")
        pacing = c.pop("pacing", None)
        if pacing is not None and (self.kind != "capability" or pacing != PACING_CANDIDATE):
            raise ValueError("pacing must be the explicit reviewed capability combination")
        budget = 65536 if self.kind == "capability" else 8192
        if c["context_window"] != budget:
            raise ValueError("wrong local Agent context budget")
        _origin(c["model_origin"])
        if self.kind == "capability":
            if pacing is not None:
                if c["timeout_seconds"] != PACING_CANDIDATE["driver_io_timeout_seconds"]:
                    raise ValueError("pacing candidate requires its explicit driver I/O timeout")
            elif c["timeout_seconds"] != 180:
                raise ValueError("only the explicit pacing candidate may change the run I/O timeout")
            c.update(schema_version="ae-01-capability-probe-0.1",
                     expected_model="Qwen3-4B-Instruct-2507",
                     model_handle="vllm/Qwen3-4B-Instruct-2507")
            if pacing is not None:
                # Normalize ONLY this explicitly reviewed run timeout on the copy
                # handed to the unchanged legacy validator; the real 900 stays in
                # the returned config, so plan/provenance record the actual value.
                c["timeout_seconds"] = 180
            capability_validate(c)
        else:
            c["schema_version"] = "ae-connection-probe-0.1"
            probe_validate(c)
        return deepcopy(config)

    def payload(self, config, payload):
        p = deepcopy(payload)
        if isinstance(p.get("name"), str):
            p["name"] = p["name"].replace("ae-capability-qwen3-4b-", "ae-capability-cloud-")
        p.pop("model", None)
        p.pop("model_settings", None)
        p.pop("context_window_limit", None)
        p["llm_config"] = {
            "model": MODEL, "handle": config["model_handle"],
            "model_endpoint_type": "openai",
            "model_endpoint": config["model_origin"] + "/v1",
            "provider_name": "vllm", "provider_category": "base",
            "context_window": config["context_window"],
            "max_tokens": config["max_output_tokens"],
            "temperature": config["temperature"],
            "parallel_tool_calls": False, "strict": False,
            "enable_reasoner": False, "put_inner_thoughts_in_kwargs": False,
        }
        return p

    def check_catalog(self, listed, config):
        data = listed.get("data") if isinstance(listed, dict) else None
        if not isinstance(data, list):
            raise ValueError("catalog data must be a list")
        matches = [m for m in data if isinstance(m, dict) and m.get("id") == MODEL]
        if len(matches) != 1:
            raise ValueError("selected cloud model missing or duplicated")
        return deepcopy(matches[0])  # Never insert a max_model_len.

    def annotate(self, result):
        result["schema_version"] = TASK_PROFILE if self.kind == "capability" else PROBE_PROFILE
        result["cloud_execution"] = {
            "transport_profile": COMPAT_PROFILE,
            "context_window_source": "local_agent_budget_not_server_measurement",
            "server_capacity_verified": False, "generation_seed_sent": False,
            "api_compatibility_verified_offline": False,
            "legacy_handle_is_adapter_label_not_local_vllm": True,
            "scientific_result": None,
        }
        # Legacy planning text names the old checkpoint; do not carry that
        # assertion over to a different provider/model.
        if "boundaries" in result:
            result["boundaries"] = [
                b.replace("all three native LLM roles may share Qwen3-4B; judge scores are debug only",
                          "all three native LLM roles share the selected cloud candidate; judge scores are debug only")
                for b in result["boundaries"]]
        return result
