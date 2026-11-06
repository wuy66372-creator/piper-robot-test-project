#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV="${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/show-harness-venv"
EXPECTED=c3a133eb111a8100bcb64509b1ea891fdcee1532
if [[ $(git -C "$ROOT/Show-Harness" rev-parse HEAD) != "$EXPECTED" ]]; then
  echo 'Show-Harness version changed; revalidate the adapter before use.' >&2
  exit 1
fi
if [[ ${1:-} == setup ]]; then
  uv venv --python /usr/bin/python3.12 "$ENV"
  uv pip install --python "$ENV/bin/python" -r "$ROOT/show_harness/requirements.lock.txt"
  exit
fi
if [[ ${1:-} == test ]]; then
  cd "$ROOT/show_harness"
  PYTHONPATH="$ROOT:$ROOT/Show-Harness" exec "$ENV/bin/python" -B -m unittest -v test_backend test_gumi test_continuous test_gripper test_gripper_context test_teleop_range test_init_adoption test_browser_camera test_realsense_camera test_model_control test_can_takeover test_model_switch test_collection test_training test_model_profiles test_dual_model_control test_training_initialization
fi
SDK="${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/orbbec-v1.10.37/sdk/OrbbecSDK_v1.10.37/SDK"
BINARY="${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/orbbec-v1.10.37/stream"
g++ -std=c++17 -O2 -pthread "$ROOT/cameras/orbbec_stream.cpp" -I"$SDK/include" -L"$SDK/lib" -Wl,-rpath,"$SDK/lib" -lOrbbecSDK -o "$BINARY"
if [[ ${1:-} == gumi ]]; then
  shift
  exec "$ENV/bin/python" -B "$ROOT/show_harness/gumi_local.py" "$@"
fi
exec "$ENV/bin/python" -B "$ROOT/show_harness/cli.py" "$@"
