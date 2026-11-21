#!/bin/sh
# Install the example systemd user unit without enabling or starting it.
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
UNIT_SOURCE="$REPO_ROOT/deploy/simstim-wireless-control@.service"
UNIT_DIR=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user
UNIT_TARGET="$UNIT_DIR/simstim-wireless-control@.service"

if [ ! -r "$UNIT_SOURCE" ]; then
    printf '%s\n' "Missing unit template: $UNIT_SOURCE" >&2
    exit 1
fi
mkdir -p "$UNIT_DIR"
install -m 0644 "$UNIT_SOURCE" "$UNIT_TARGET"
printf 'Installed %s\n' "$UNIT_TARGET"
printf '%s\n' \
  'Review the unit paths and create ~/.config/simstim/<instance>.env with protected settings.' \
  'Then run: systemctl --user daemon-reload' \
  'Enable/start only after reviewing the host configuration: systemctl --user enable --now simstim-wireless-control@host.service'
