#!/usr/bin/env bash
# Foreground local service. No automatic restart or backgrounding.
# Usage: bash scripts/deployment/start_vllm.sh RUN_ID [CONTEXT_WINDOW] [PORT]
# 8192 preserves the original short probe; 32768 is the t4-t5 wiring setting.
set -Eeuo pipefail

AE_ROOT=/root/rivermind-data/agent-erratum
AE_VENV="$AE_ROOT/.venv-vllm"
AE_MODEL=/root/rivermind-data/kv-edit-mechanism/model-cache/Qwen3-8B
AE_RUN_ID=${1:-}
AE_CONTEXT_WINDOW=${2:-8192}
AE_PORT=${3:-8000}
if [[ $# -lt 1 || $# -gt 3 || ! "$AE_RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ || ! "$AE_CONTEXT_WINDOW" =~ ^(8192|32768)$ || ! "$AE_PORT" =~ ^(8000|8180)$ ]]; then
    printf '%s\n' 'Usage: bash start_vllm.sh RUN_ID [8192|32768] [8000|8180]' >&2
    exit 2
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 || ! -d "$AE_ROOT" ]]; then
    printf '%s\n' 'This fixed-path launcher requires the authorized Linux x86_64 server project.' >&2
    exit 2
fi

AE_LOG_DIR="$AE_ROOT/results/deployment"
AE_LOG="$AE_LOG_DIR/vllm-start-$AE_RUN_ID.log"
mkdir -p "$AE_LOG_DIR"
printf 'vllm_log=%s\n' "$AE_LOG"
set -o noclobber
exec >"$AE_LOG" 2>&1
set +o noclobber
trap 'AE_EXIT=$?; printf "launcher_exit_code=%s\n" "$AE_EXIT"; date -u +finished_at_utc=%Y-%m-%dT%H:%M:%SZ' EXIT

date -u +started_at_utc=%Y-%m-%dT%H:%M:%SZ
printf 'context_window=%s\nthinking_mode=model_default_preserved\n' "$AE_CONTEXT_WINDOW"
printf '%s\n' 'reasoning_parser=qwen3 is output parsing, not a switch disabling thinking.'
printf 'model_path=%s\nserved_model_name=Qwen3-8B\nlisten=http://127.0.0.1:%s\n' "$AE_MODEL" "$AE_PORT"
if [[ ! -x "$AE_VENV/bin/vllm" || ! -f "$AE_MODEL/config.json" || ! -f "$AE_MODEL/tokenizer_config.json" ]]; then
    printf '%s\n' 'Dedicated vLLM environment or existing local model files are missing; no download/start fallback.' >&2
    exit 2
fi
AE_RUNTIME="$AE_ROOT/runtime/vllm-$AE_RUN_ID"
if [[ -e "$AE_RUNTIME" || -L "$AE_RUNTIME" ]]; then
    printf '%s\n' 'Refusing an existing runtime directory; use a new run ID after inspection.' >&2
    exit 2
fi
mkdir -p "$AE_ROOT/runtime"
mkdir "$AE_RUNTIME"
mkdir "$AE_RUNTIME/cache" "$AE_RUNTIME/config" "$AE_RUNTIME/tmp"

unset PYTHONPATH PYTHONHOME VIRTUAL_ENV
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export VLLM_NO_USAGE_STATS=1
export VLLM_DO_NOT_TRACK=1
export DO_NOT_TRACK=1
export VLLM_CACHE_ROOT="$AE_RUNTIME/cache"
export VLLM_CONFIG_ROOT="$AE_RUNTIME/config"
# vLLM places Unix-domain IPC sockets below TMPDIR. The data-disk project
# prefix plus a run ID can exceed Linux's 107-byte socket pathname limit.
# Keep caches/evidence on the data disk but give IPC a fresh short directory.
AE_IPC_TMP=$(mktemp -d /tmp/ae-vllm.XXXXXXXX)
export TMPDIR="$AE_IPC_TMP"
printf 'ipc_tmpdir=%s\n' "$AE_IPC_TMP"

"$AE_VENV/bin/python" - "$AE_MODEL/config.json" "$AE_CONTEXT_WINDOW" <<'PY'
import importlib.metadata
import json
import sys

expected = {"vllm": "0.10.0+cu126", "torch": "2.7.1+cu126", "transformers": "4.53.3"}
observed = {name: importlib.metadata.version(name) for name in expected}
print(json.dumps({"expected": expected, "observed": observed}, indent=2))
if observed != expected:
    raise SystemExit("Refusing a different vLLM/torch/transformers stack")
with open(sys.argv[1]) as stream:
    model_config = json.load(stream)
native_window = model_config.get("max_position_embeddings")
print(json.dumps({"native_max_position_embeddings": native_window,
                  "rope_scaling": model_config.get("rope_scaling"), "requested_window": int(sys.argv[2])}))
if not isinstance(native_window, int) or int(sys.argv[2]) > native_window or model_config.get("rope_scaling") is not None:
    raise SystemExit("Refusing an unsupported or RoPE-scaled context setting")
PY
sha256sum "$AE_MODEL/config.json" "$AE_MODEL/tokenizer_config.json"
printf '%s\n' 'Launch is foreground-only. A listening server is not proof that the tool probe passed.'

# Use the model's native chat template and default thinking mode. No /no_think,
# custom template, YaRN, quantization, remote-code trust, or silent truncation.
exec "$AE_VENV/bin/vllm" serve "$AE_MODEL" \
    --served-model-name Qwen3-8B \
    --host 127.0.0.1 --port "$AE_PORT" \
    --tensor-parallel-size 1 \
    --dtype bfloat16 \
    --max-model-len "$AE_CONTEXT_WINDOW" \
    --max-num-seqs 1 \
    --max-num-batched-tokens "$AE_CONTEXT_WINDOW" \
    --gpu-memory-utilization 0.60 \
    --enforce-eager \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --reasoning-parser qwen3
