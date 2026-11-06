#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/orbbec-v1.10.37"
SDK="$CACHE/sdk/OrbbecSDK_v1.10.37/SDK"
if [[ ! -f "$SDK/include/libobsensor/ObSensor.hpp" ]]; then
  echo 'Missing SDK; run python3 cameras/setup.py first.' >&2
  exit 1
fi
mkdir -p "$CACHE"
g++ -std=c++17 -O2 -pthread "$ROOT/cameras/orbbec_capture.cpp" -I"$SDK/include" -L"$SDK/lib" -Wl,-rpath,"$SDK/lib" -lOrbbecSDK -o "$CACHE/capture"
OUTPUT="${1:-$ROOT/results/cameras/$(date +%Y%m%d-%H%M%S)}"
OUTPUT=$(realpath -m "$OUTPUT")
cd "$CACHE"
"$CACHE/capture" "$OUTPUT"
echo "$OUTPUT"
