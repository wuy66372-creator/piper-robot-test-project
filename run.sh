#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_env="${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/piper-venv"
if [[ "${1:-}" == setup ]]; then
    uv venv --python /usr/bin/python3.12 "$task_env"
    uv pip install --python "$task_env/bin/python" -r "$task_root/requirements.txt"
    exit 0
fi
if [[ ! -x "$task_env/bin/python" ]]; then
    echo "Run bash $task_root/run.sh setup first." >&2
    exit 1
fi
if [[ "${1:-}" == test ]]; then
    cd -- "$task_root"
    exec "$task_env/bin/python" -B -m unittest -v test_piper_control
fi
if [[ "${1:-}" == restore ]]; then
    shift
    exec "$task_env/bin/python" -B "$task_root/supported_restore.py" "$@"
fi
exec "$task_env/bin/python" -B "$task_root/piper_control.py" "$@"
