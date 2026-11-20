#!/usr/bin/env bash
# Configure and build only. This script does not select or open a device.
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
IDF_VERSION=5.5.6
IDF_TOOLS_PATH="${IDF_TOOLS_PATH:-$HOME/.espressif}"
IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
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
export IDF_TOOLS_PATH IDF_PATH
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
  -D "SDKCONFIG=$SDKCONFIG" set-target esp32s3 build
