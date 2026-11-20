#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
VENV="${SIMSTIM_BLE_VENV:-$HOME/.local/share/simstim/ble-fallback/venv}"
if [ ! -x "$VENV/bin/python" ]; then
    mkdir -p "$(dirname -- "$VENV")"
    python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/ble-fallback/requirements-host.txt"
export PYTHONPATH="$ROOT/tools${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV/bin/python" "$ROOT/ble-fallback/host/dual_transport_daemon.py" "$@"
