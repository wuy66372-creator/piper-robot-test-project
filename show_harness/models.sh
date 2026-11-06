#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/hackathon"
VENV="${SHOWHARNESS_VLLM_VENV:-$CACHE/show-harness-vllm-venv}"
HF_VENV="$CACHE/show-harness-hf-venv"
export SHOWHARNESS_MODEL_HOME="${SHOWHARNESS_MODEL_HOME:-$CACHE/show-harness-models}"
export HF_HOME="$SHOWHARNESS_MODEL_HOME/huggingface"
case "${1:-help}" in
  setup)
    [[ -x "$VENV/bin/python" ]] || uv venv --python /usr/bin/python3.12 "$VENV"
    UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=4 uv pip install --python "$VENV/bin/python" -r "$ROOT/show_harness/requirements-vllm.lock.txt"
    ;;
  download)
    [[ -x "$HF_VENV/bin/python" ]] || uv venv --python /usr/bin/python3.12 "$HF_VENV"
    uv pip install --python "$HF_VENV/bin/python" huggingface-hub==1.17.0
    exec "$HF_VENV/bin/python" "$ROOT/show_harness/download_models.py"
    ;;
  verify)
    exec "$HF_VENV/bin/python" "$ROOT/show_harness/download_models.py" --verify-only
    ;;
  serve)
    shift
    exec "$VENV/bin/python" "$ROOT/show_harness/serve_models.py" "$@"
    ;;
  start|stop)
    exec /usr/bin/python3.12 "$ROOT/show_harness/model_service.py" "$@"
    ;;
  check)
    shift
    exec "$CACHE/show-harness-venv/bin/python" "$ROOT/show_harness/check_models.py" "$@"
    ;;
  status)
    nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv
    curl --fail --silent --show-error http://127.0.0.1:8000/v1/models
    ;;
  *)
    echo 'Usage: bash show_harness/models.sh setup|download|verify|serve [PROFILE] [--dry-run]|start [PROFILE]|stop|check [--profile NAME]|status'
    ;;
esac
