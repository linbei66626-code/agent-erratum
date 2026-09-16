#!/usr/bin/env bash
# Server-only installer. No model inference, background jobs, retries, or cleanup.
# Usage: bash scripts/deployment/install_vllm.sh RUN_ID EXPECTED_WHEEL_SHA256
set -Eeuo pipefail

AE_ROOT=/root/rivermind-data/agent-erratum
AE_UV="$AE_ROOT/.venv-bootstrap/bin/uv"
AE_VENV="$AE_ROOT/.venv-vllm"
AE_WHEEL="$AE_ROOT/deployment-assets/vllm-0.10.0+cu126-cp38-abi3-manylinux1_x86_64.whl"
AE_RUN_ID=${1:-}
AE_EXPECTED_SHA=${2:-}

if [[ $# -ne 2 || ! "$AE_RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ || ! "$AE_EXPECTED_SHA" =~ ^[a-f0-9]{64}$ ]]; then
    printf '%s\n' 'Usage: bash install_vllm.sh RUN_ID EXPECTED_WHEEL_SHA256' >&2
    exit 2
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 || ! -d "$AE_ROOT" ]]; then
    printf '%s\n' 'This fixed-path installer requires the authorized Linux x86_64 server project.' >&2
    exit 2
fi

AE_LOG_DIR="$AE_ROOT/results/deployment"
AE_LOG="$AE_LOG_DIR/vllm-install-$AE_RUN_ID.log"
mkdir -p "$AE_LOG_DIR"
printf 'install_log=%s\n' "$AE_LOG"
set -o noclobber
exec >"$AE_LOG" 2>&1
set +o noclobber
trap 'AE_EXIT=$?; printf "installer_exit_code=%s\n" "$AE_EXIT"; date -u +finished_at_utc=%Y-%m-%dT%H:%M:%SZ' EXIT

date -u +started_at_utc=%Y-%m-%dT%H:%M:%SZ
printf 'purpose=connectivity_environment_install_only\nvenv=%s\nwheel=%s\n' "$AE_VENV" "$AE_WHEEL"
if [[ ! -x "$AE_UV" ]]; then
    printf '%s\n' 'Bootstrap uv is missing; no fallback installer or automatic bootstrap.' >&2
    exit 2
fi
if [[ -e "$AE_VENV" || -L "$AE_VENV" ]]; then
    printf '%s\n' 'Refusing existing .venv-vllm; inspect it before any manual recovery.' >&2
    exit 2
fi
if [[ ! -f "$AE_WHEEL" || -L "$AE_WHEEL" ]]; then
    printf '%s\n' 'Expected uploaded regular wheel file is missing; no remote wheel fallback.' >&2
    exit 2
fi
AE_ACTUAL_SHA=$(sha256sum "$AE_WHEEL" | awk '{print $1}')
printf 'wheel_sha256=%s\nexpected_wheel_sha256=%s\n' "$AE_ACTUAL_SHA" "$AE_EXPECTED_SHA"
if [[ "$AE_ACTUAL_SHA" != "$AE_EXPECTED_SHA" ]]; then
    printf '%s\n' 'Wheel SHA256 mismatch; nothing has been installed.' >&2
    exit 2
fi
df -h "$AE_ROOT"
AE_FREE_KIB=$(df -Pk "$AE_ROOT" | awk 'NR == 2 {print $4}')
# Engineering headroom only, not a scientific gate. Keep old files untouched.
if [[ ! "$AE_FREE_KIB" =~ ^[0-9]+$ || "$AE_FREE_KIB" -lt 15728640 ]]; then
    printf '%s\n' 'Need at least 15 GiB free on the target filesystem for this wheel-only installation; no cleanup is attempted.' >&2
    exit 2
fi

AE_TEMP=$(mktemp -d "$AE_ROOT/.tmp-vllm-install.XXXXXXXX")
printf 'temporary_directory=%s\n' "$AE_TEMP"
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV
export TMPDIR="$AE_TEMP"
export UV_HTTP_RETRIES=0
export UV_HTTP_TIMEOUT=60
export UV_NO_CACHE=1
export UV_NO_CONFIG=1

"$AE_UV" --version
"$AE_UV" venv --python python3.11 --no-python-downloads --no-config "$AE_VENV"

# These three binaries come only from the official CUDA 12.6 index. Deliberately
# install no dependencies in this first stage; the complete resolution follows.
"$AE_UV" pip install \
    --python "$AE_VENV/bin/python" --no-config --no-cache \
    --only-binary :all: --no-deps \
    --index-url https://download.pytorch.org/whl/cu126 \
    'torch==2.7.1+cu126' 'torchaudio==2.7.1+cu126' 'torchvision==0.22.1+cu126'

# Ordinary dependencies use one explicit mirror, not a mixed extra-index pool.
# The already installed CUDA binaries must retain their exact local versions.
# If the resolver cannot satisfy this, stop; do not swap CUDA or build sources.
"$AE_UV" pip install \
    --python "$AE_VENV/bin/python" --no-config --no-cache \
    --only-binary :all: \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    "$AE_WHEEL" 'transformers==4.53.3' \
    'torch==2.7.1+cu126' 'torchaudio==2.7.1+cu126' 'torchvision==0.22.1+cu126'

"$AE_UV" pip check --python "$AE_VENV/bin/python" --no-config
"$AE_VENV/bin/python" - <<'PY'
import importlib.metadata
import json
import sys

expected = {
    "vllm": "0.10.0+cu126",
    "torch": "2.7.1+cu126",
    "torchaudio": "2.7.1+cu126",
    "torchvision": "0.22.1+cu126",
    "transformers": "4.53.3",
}
observed = {name: importlib.metadata.version(name) for name in expected}
print(json.dumps({"python": sys.version, "expected": expected, "observed": observed}, indent=2))
if observed != expected or sys.version_info[:2] != (3, 11):
    raise SystemExit("installed versions differ from this deployment specification")
PY
"$AE_UV" pip freeze --python "$AE_VENV/bin/python" --no-config
printf '%s\n' 'INSTALL_CHECK_PASS: versions/dependencies only; no model or Letta request was run.'
printf '%s\n' 'The new venv and temporary directory are retained; no old environment was changed.'
