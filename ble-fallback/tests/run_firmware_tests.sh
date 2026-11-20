#!/usr/bin/env bash
set -euo pipefail
testdir="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$testdir/.." && pwd)"
build_dir="$(mktemp -d "$root/.firmware-test.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT
common=(-std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer)
includes=(-I"$testdir/firmware_stubs" -I"$root/firmware")

cc "${common[@]}" -pthread "${includes[@]}" \
  "$testdir/test_firmware_integration.c" \
  "$root/firmware/ble_stream.c" "$root/firmware/ble_tls_bio.c" \
  "$root/firmware/transport_selector.c" -o "$build_dir/firmware-integration"
"$build_dir/firmware-integration"

cc "${common[@]}" "${includes[@]}" \
  "$testdir/test_nimble_gatt_client.c" "$root/firmware/nimble_gatt_client.c" \
  "$root/firmware/ble_stream.c" -o "$build_dir/nimble-gatt"
"$build_dir/nimble-gatt"

cc "${common[@]}" -I"$root/firmware" \
  "$testdir/test_ack_pump.c" -o "$build_dir/ack-pump"
"$build_dir/ack-pump"
printf '%s\n' 'firmware BIO, transport selector, NimBLE GATT discovery, and ack_step tests passed'
