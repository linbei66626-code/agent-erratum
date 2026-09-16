#!/usr/bin/env bash
# One foreground diagnostic server; keeps the old 8B launcher and weights intact.
set -Eeuo pipefail
AE_ROOT=/root/rivermind-data/agent-erratum
AE_MODEL="$AE_ROOT/models/Qwen3-4B-Instruct-2507-cdbee75f"
AE_VENV="$AE_ROOT/.venv-vllm"
AE_RUN_ID=${1:-}
if [[ $# != 1 || ! "$AE_RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ || "$(uname -s)" != Linux ]]; then
    printf '%s\n' 'Usage: start_vllm_qwen4b.sh NEW_RUN_ID (authorized Linux server only)' >&2
    exit 2
fi
AE_LOG="$AE_ROOT/deployment-logs/qwen4b-vllm-$AE_RUN_ID.log"
AE_RUNTIME="$AE_ROOT/runtime/qwen4b-$AE_RUN_ID"
[[ ! -e "$AE_LOG" && ! -e "$AE_RUNTIME" && ! -L "$AE_RUNTIME" ]]
mkdir "$AE_RUNTIME"
mkdir "$AE_RUNTIME/cache" "$AE_RUNTIME/config"
set -o noclobber
exec >"$AE_LOG" 2>&1
set +o noclobber
date -u +started_at_utc=%Y-%m-%dT%H:%M:%SZ
printf 'pid=%s\nmodel=%s\ncontext_window=65536\nmax_batched_tokens=8192\nchunked_prefill=true\nnon_thinking_model=true\nreasoning_parser=none\n' "$$" "$AE_MODEL"
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export VLLM_NO_USAGE_STATS=1 VLLM_DO_NOT_TRACK=1 DO_NOT_TRACK=1
export VLLM_CACHE_ROOT="$AE_RUNTIME/cache" VLLM_CONFIG_ROOT="$AE_RUNTIME/config"
AE_IPC_TMP=$(mktemp -d /tmp/ae-4b.XXXXXXXX)
export TMPDIR="$AE_IPC_TMP"
printf 'ipc_tmpdir=%s\n' "$AE_IPC_TMP"
"$AE_VENV/bin/python" - "$AE_MODEL" "$AE_ROOT/scripts/deployment/qwen4b-manifest.json" <<'PY'
import hashlib, importlib.metadata, json, pathlib, sys
expected = {'vllm':'0.10.0+cu126','torch':'2.7.1+cu126','transformers':'4.53.3'}
observed = {p:importlib.metadata.version(p) for p in expected}
print({'expected':expected,'observed':observed}, flush=True)
assert observed == expected
model = pathlib.Path(sys.argv[1])
manifest = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert manifest['revision'] == 'cdbee75f17c01a7cc42f958dc650907174af0554'
for row in manifest['files']:
    path = model / row['name']
    assert path.stat().st_size == row['size'], path.name
    # Weight digests are checked on download; recheck the serving config/template.
    if not path.name.endswith('.safetensors'):
        raw = path.read_bytes()
        h = hashlib.sha256(raw).hexdigest() if row['sha256'] else hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()
        assert h == (row['sha256'] or row['git_blob_sha1']), path.name
config = json.loads((model/'config.json').read_text())
assert config['max_position_embeddings'] == 262144 and config.get('rope_scaling') is None
assert list(model.glob('verified-*.json')), 'No complete download verification record'
print('PINNED_CONFIG_VERIFIED; task competence remains untested', flush=True)
PY
exec "$AE_VENV/bin/vllm" serve "$AE_MODEL" \
    --served-model-name Qwen3-4B-Instruct-2507 \
    --host 127.0.0.1 --port 8180 --tensor-parallel-size 1 \
    --dtype bfloat16 --max-model-len 65536 --max-num-seqs 1 \
    --max-num-batched-tokens 8192 --enable-chunked-prefill \
    --gpu-memory-utilization 0.70 --enforce-eager \
    --enable-auto-tool-choice --tool-call-parser hermes
