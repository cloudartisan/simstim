# Portable host gateway and operator tools

This tree contains the host-side TLS-PSK gateway, optional BlueZ BLE transport, operator Unix-socket CLI, and tap/attention tools. It is independent of Hermes: the daemon and CLI communicate through documented sockets and JSON, and can be run by an ordinary user or systemd. No device firmware, model weights, recordings, or keys are included.

## Install and configure

Python 3.11+ is required. The minimal Wi-Fi gateway uses the Python standard library plus OpenSSL 3 with TLS-PSK support. Install optional features only as needed:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r ble-fallback/requirements-host.txt  # optional BlueZ/USB tap support
python -m pip install -r tools/requirements-audio-vision.txt  # optional local audio/vision features
```

Dependencies are deliberately split by feature:

- **Core Wi-Fi gateway and operator CLI:** Python 3.11+ standard library; the Python `ssl` module must be backed by OpenSSL 3 with TLS-PSK support. No pip package or Hermes installation is required.
- **BlueZ GATT transport:** `dbus-next==0.2.3` from `ble-fallback/requirements-host.txt`.
- **USB tap listener/provisioning:** `pyserial==3.5` from `ble-fallback/requirements-host.txt`.
- **Local audio/vision:** install `tools/requirements-audio-vision.txt`; NumPy, SoundFile, Kokoro ONNX, ONNX Runtime, phonemizer and `espeak-ng` runtime support are only needed by those optional paths. Model/voice assets are separate and are not included.

The pinned requirements files are optional feature groups; installing them does not provision keys, BlueZ, eSpeak system binaries, or model assets.

Set configuration in the environment or a protected environment file. `SIMSTIM_*` variables are intentionally used for compatibility:

| Variable | Default | Purpose |
|---|---|---|
| `SIMSTIM_BIND` | `127.0.0.1` | TCP bind address; use an explicitly selected LAN address only when required |
| `SIMSTIM_PORT` | `18767` | TCP port |
| `SIMSTIM_PEER_IDENTITY` | `simstim-device` | Exact TLS-PSK client identity; must equal firmware `SIMSTIM_LINK_IDENTITY` |
| `SIMSTIM_KEY_PATH` | `~/.local/state/simstim-wireless/device.psk` | 32 raw PSK bytes, regular file mode 0600 |
| `SIMSTIM_OPERATOR_SOCKET` | `~/.local/state/simstim-wireless/operator.sock` | Local JSON-line operator socket |
| `SIMSTIM_ATTENTION_STATE` | `~/.local/state/simstim-attention/state.json` | Reviewed tap state |
| `SIMSTIM_RELEASE_ELF_PATH` | legacy fixed build path | New builds use a unique directory. Set this explicitly to the matching candidate ELF; the host does not auto-select a recent build. |
| `SIMSTIM_ELF_SHA256` | unset | Expected lowercase firmware ELF digest |
| `SIMSTIM_SERIAL_PORT` | `/dev/ttyUSB0` | USB serial device for the optional tap listener |

Provision PSK out of band. Never put it in command arguments, source control, logs, or chat. Binding to loopback is the safe default; PSK TLS is not a substitute for network segmentation or firewall policy.

## Run without Hermes

With the daemon running under the configured account, use the CLI directly:

```sh
python wireless-control/cli.py --socket "$SIMSTIM_OPERATOR_SOCKET" status
python wireless-control/cli.py --socket "$SIMSTIM_OPERATOR_SOCKET" tap-status
python wireless-control/cli.py --socket "$SIMSTIM_OPERATOR_SOCKET" motion-diagnostic --expected-peer "$PEER" --expected-elf "$ELF" --expected-transport "$BEARER" --expected-generation "$GENERATION"
```

Set `PEER`, `ELF`, `BEARER` and `GENERATION` from the authenticated status you intend to test. Confirm the peer and firmware against your deployment record, select the intended Wi-Fi (`wifi`) or Bluetooth (`ble`) session, and do not silently refresh the pin on reconnect.

`motion-diagnostic` issues exactly one nonce-bound `LOOK_MOTION` request on the current authenticated device session. It is explicitly non-retrying: timeout, disconnect, malformed evidence, or a session change returns failure rather than replaying motion. Successful output contains only structured survey point targets, measured raw positions, cleanup verification, and authenticated session identity; it does not access cameras, microphones, or speech.

A plain Python client can also use the versioned transport shape (one newline-terminated JSON request and one JSON response):

```python
import json, os, socket
path = os.environ.get("SIMSTIM_OPERATOR_SOCKET", os.path.expanduser("~/.local/state/simstim-wireless/operator.sock"))
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(5)
    client.connect(path)
    client.sendall(b'{"command":"status"}\n')
    print(json.loads(client.makefile("rb").readline()))
```

The socket is local-only and mode 0600. Commands that actuate a device or transfer media are not retried. Only the CLI's explicitly allowlisted read-only status queries may be retried.

## Dual-radio host deployment

The live dual-radio entry point is `ble-fallback/host/dual_transport_daemon.py`; it owns both the Wi-Fi TCP listener and the BlueZ GATT application in one process. The service template uses that actual entry point and an isolated Python runtime. It is deliberately **not installed, reloaded, enabled, or started** by tests. The sample paths below target a checkout at `~/Projects/simstim-delivery`; edit the unit's `WorkingDirectory`, interpreter and script path if yours differs.

### Prepare the isolated runtime

Install only the pinned host dependencies into a dedicated venv (not the system Python and not the repo's optional audio/vision environment):

```sh
python3 -m venv "$HOME/.local/share/simstim/host-venv"
"$HOME/.local/share/simstim/host-venv/bin/python" -m pip install \
  -r "$HOME/Projects/simstim-delivery/ble-fallback/requirements-host.txt"
"$HOME/.local/share/simstim/host-venv/bin/python" -c \
  'from importlib.metadata import version; print(version("dbus-next"))'
```

`dbus-next==0.2.3` is required for BlueZ; `pyserial==3.5` is included for the optional USB tap feature. This runtime setup does not connect to D-Bus, power an adapter, advertise, or start the daemon.

### Minimal protected configuration

Choose the host's explicit private-LAN address (not `0.0.0.0`) and allow the selected TCP port only from the device/management network in the host firewall. BLE advertisement is independently controlled by daemon startup and BlueZ registration. Create directories and a protected environment file:

```sh
install -d -m 0700 "$HOME/.config/simstim" \
  "$HOME/.local/state/simstim-wireless" "$HOME/.local/state/simstim-attention" \
  "$HOME/.local/state/simstim"
install -m 0600 /secure/provisioned/device.psk \
  "$HOME/.local/state/simstim-wireless/device.psk"
cat > "$HOME/.config/simstim/host.env" <<'EOF'
SIMSTIM_BIND=REPLACE_WITH_PRIVATE_LAN_ADDRESS
SIMSTIM_PORT=18767
SIMSTIM_PEER_IDENTITY=simstim-device
SIMSTIM_KEY_PATH=/home/REPLACE_WITH_ACCOUNT/.local/state/simstim-wireless/device.psk
SIMSTIM_OPERATOR_SOCKET=/home/REPLACE_WITH_ACCOUNT/.local/state/simstim-wireless/operator.sock
SIMSTIM_STATE_PATH=/home/REPLACE_WITH_ACCOUNT/.local/state/simstim/state.json
SIMSTIM_ATTENTION_STATE=/home/REPLACE_WITH_ACCOUNT/.local/state/simstim-attention/state.json
SIMSTIM_RELEASE_ELF_PATH=/absolute/path/to/the/exact-mode-candidate/k151_standalone.elf
SIMSTIM_ELF_SHA256=REPLACE_WITH_VERIFIED_64_HEX_FIRMWARE_DIGEST
SIMSTIM_BLUEZ_ADAPTER=/org/bluez/hci0
EOF
chmod 0600 "$HOME/.config/simstim/host.env"
```

Replace the sample LAN address, account paths and digest with values verified for the host and paired firmware. The PSK is exactly 32 provisioned raw bytes; never create it in shell history or put it in the environment file. Keep the identity equal to the firmware's `SIMSTIM_LINK_IDENTITY`. The default identity is `simstim-device` on both Wi-Fi and BLE; both paths now share the same default. Leave `SIMSTIM_ELF_SHA256` unset only if intentionally accepting any otherwise-valid ready banner; pinning the exact ELF digest is safer. Do not expose TCP/18767 to untrusted networks.

The template file `deploy/simstim-wireless-control@.service` is a **user-service template**. It reads `%h/.config/simstim/<instance>.env`, invokes the actual dual-transport daemon via the isolated venv, and does not touch the board's network configuration. The installer only copies the template:

```sh
cd "$HOME/Projects/simstim-delivery"
./deploy/install-user-service.sh
```

Review the unit paths, permissions, D-Bus/BlueZ policy, firewall and environment file before activation. Only when deliberately activating it, run:

```sh
systemctl --user daemon-reload
systemctl --user enable --now simstim-wireless-control@host.service
```

These activation commands are documented procedures, not executed by this repository or covered by mock tests. This task does not install the template on a host, change a service, or test physical radios. `systemctl --user` requires a user manager with access to the system BlueZ D-Bus and adapter; satisfy those host policies explicitly rather than broadening unit privileges by default.


## Tests

From the repository root:

```sh
python3 -m unittest discover -s wireless-control/tests -v
python3 -m unittest discover -s attention-lab -p 'test_*.py' -v
python3 -m unittest discover -s ble-fallback/tests -p 'test_*.py' -v
python3 -m unittest discover -s tests -p 'test_host_deployment.py' -v
bash ble-fallback/tests/run_firmware_tests.sh
```

For a clean host-only run without accidental imports from sibling checkouts, export the repository to an empty temporary directory and run the three host suites plus deployment checks there. For example, in a disposable Git worktree or source export, set `PYTHONPATH` empty and `TMPDIR` to a private temporary directory outside all checkouts. Host tests use synthetic peers and local sockets; the C firmware-stub tests are not device tests.


## Host feature and wire-parity checklist

Use this checklist when pairing a host release with a firmware release. Passing host tests means simulated protocol behavior is exercised; it does **not** certify the corresponding firmware, BlueZ controller, or physical device.

| Capability | Host-side coverage | Pairing/acceptance still required |
|---|---|---|
| TLS transport | TLS 1.2 PSK server, fixed cipher, exact configured identity, and test peer round trips | Firmware must use the same PSK identity, cipher/TLS version, and 32-byte key; verify against the exact firmware build |
| Session startup | `SIMSTIM_STANDALONE` banner parser, ELF digest pin, readiness and uptime handling | Firmware banner fields and digest must match; confirm board readiness on hardware |
| Control protocol | Operator commands and returned device receipts through Wi-Fi and BLE bridge test paths | Compare command names, line grammar, bounds, and receipts with the paired firmware revision; physical command/receipt acceptance remains outstanding |
| Async tap records | `TAP_EVENT`, `TAP_FETCH`, and `TAP_DONE` host handling | Confirm event sequencing, fetch token and completion semantics on paired firmware |
| BLE stream framing | Host fragment length/sequence checks, MTU limits, ordered bidirectional large-payload/backpressure tests | Firmware framing, sequence wrap, ATT MTU negotiation and reconnect behavior must be wire-identical; no radio test is implied |
| Operator API | Local newline-delimited JSON socket, bounded operations and safe read-only retries | Confirm client compatibility and filesystem ownership/socket permissions on the deployment host |
| Optional features | Unit-level audio/vision/tap pathways with declared feature dependencies | Model assets, audio hardware, sensors, camera, motors and physical outcomes need separate acceptance |

Before declaring wire parity, record the firmware revision/image digest and the corresponding host test results, then run a paired-device acceptance covering handshake/authentication, startup banner, each command and receipt, async event flow, payload boundaries, disconnect/reconnect, and BLE fragmentation if enabled. Do not infer parity from matching names or from a synthetic peer alone.

## Scope and deployment

`wireless-control/service.py` is the Wi-Fi TLS-PSK host. `ble-fallback/host/dual_transport_daemon.py` adds BlueZ GATT BLE while sharing the same authenticated device protocol and operator socket. Deploy one deliberate host configuration; the example systemd unit is a user-service template and is not installed or enabled by this repository. No concrete private address, hostname, PSK, serial identifier, model weights, recordings, or operational state is shipped.

`tools/tap_refresh.py` and `attention-lab/attention_broker.py` provide local reviewed-summary queuing and the optional USB tap path. The audio/vision path imports local dependencies only when invoked. No model weights, proprietary media, or keys are included.
