#!/usr/bin/env bash
# Configure and build only. This script does not select or open a device.
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
IDF_VERSION=5.5.6
IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
if [[ -z "${IDF_TOOLS_PATH:-}" ]]; then
  if [[ -d "$HOME/.cache/esphome/idf/tools" && "$IDF_PATH" == "$HOME/.cache/esphome/idf/frameworks/"* ]]; then
    IDF_TOOLS_PATH="$HOME/.cache/esphome/idf"
  else
    IDF_TOOLS_PATH="$HOME/.espressif"
  fi
fi
BUILD_DIR="${SIMSTIM_BUILD_DIR:-$ROOT/build}"
MODE="${1:-dual}"
case "$MODE" in
  dual) DEFAULTS="$ROOT/sdkconfig.defaults;$ROOT/sdkconfig.ble.defaults" ;;
  wifi) DEFAULTS="$ROOT/sdkconfig.defaults" ;;
  *) printf 'usage: %s [dual|wifi]\n' "$0" >&2; exit 2 ;;
esac
if [[ ! -f "$IDF_PATH/export.sh" ]]; then
  printf 'ESP-IDF %s not found at %s (set IDF_PATH to an installed %s tree)\n' "$IDF_VERSION" "$IDF_PATH" "$IDF_VERSION" >&2
  exit 2
fi
if [[ -z "${IDF_PYTHON_ENV_PATH:-}" && -x "$IDF_TOOLS_PATH/penvs/$IDF_VERSION/bin/python" ]]; then
  IDF_PYTHON_ENV_PATH="$IDF_TOOLS_PATH/penvs/$IDF_VERSION"
fi
export IDF_TOOLS_PATH IDF_PATH IDF_PYTHON_ENV_PATH
mkdir -p "$BUILD_DIR"
BUILD_DIR="$(cd -- "$BUILD_DIR" && pwd)"
export SDKCONFIG="$BUILD_DIR/sdkconfig"
export SDKCONFIG_DEFAULTS="$DEFAULTS"
# Source the pinned framework environment, then target an out-of-source build.
# shellcheck disable=SC1090
source "$IDF_PATH/export.sh"
ACTUAL_IDF_VERSION="$("$IDF_PATH/tools/idf.py" --version)"
if [[ "$ACTUAL_IDF_VERSION" != "ESP-IDF v$IDF_VERSION" ]]; then
  printf 'Expected ESP-IDF v%s, got: %s\n' "$IDF_VERSION" "$ACTUAL_IDF_VERSION" >&2
  exit 2
fi
"$IDF_PATH/tools/idf.py" -C "$ROOT" -B "$BUILD_DIR" \
  -D "SDKCONFIG=$SDKCONFIG" \
  -D "SIMSTIM_CUE_MODEL=${SIMSTIM_CUE_MODEL:-$HOME/.local/share/simstim/voice/kokoro-v1.0.onnx}" \
  -D "SIMSTIM_CUE_VOICES=${SIMSTIM_CUE_VOICES:-$HOME/.local/share/simstim/voice/voices-v1.0.bin}" \
  -D "SIMSTIM_CUE_PYTHON=${SIMSTIM_CUE_PYTHON:-$HOME/.local/share/simstim/voice/render/bin/python}" \
  set-target esp32s3 build
