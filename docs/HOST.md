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

The requirements file is for BLE/audio/vision extras; `dbus-next` is needed only for BlueZ, serial support only for the USB tap listener, and NumPy/SoundFile/Kokoro/ONNX Runtime/phonemizer/eSpeak only for local audio/vision paths. The non-Hermes gateway CLI itself needs no Hermes package.

Set configuration in the environment or a protected environment file. `SIMSTIM_*` variables are intentionally used for compatibility:

| Variable | Default | Purpose |
|---|---|---|
| `SIMSTIM_BIND` | `127.0.0.1` | TCP bind address; use an explicitly selected LAN address only when required |
| `SIMSTIM_PORT` | `18767` | TCP port |
| `SIMSTIM_PEER_IDENTITY` | `simstim-device` | Exact TLS-PSK client identity; must equal firmware `SIMSTIM_LINK_IDENTITY` |
| `SIMSTIM_KEY_PATH` | `~/.local/state/simstim-wireless/device.psk` | 32 raw PSK bytes, regular file mode 0600 |
| `SIMSTIM_OPERATOR_SOCKET` | `~/.local/state/simstim-wireless/operator.sock` | Local JSON-line operator socket |
| `SIMSTIM_ATTENTION_STATE` | `~/.local/state/simstim-attention/state.json` | Reviewed tap state |
| `SIMSTIM_RELEASE_ELF_PATH` | `k151-standalone/build/k151_standalone.elf` | Optional matching firmware ELF for vision checks; set explicitly when `SIMSTIM_BUILD_DIR` changes |
| `SIMSTIM_ELF_SHA256` | unset | Expected lowercase firmware ELF digest |
| `SIMSTIM_SERIAL_PORT` | `/dev/ttyUSB0` | USB serial device for the optional tap listener |

Provision PSK out of band. Never put it in command arguments, source control, logs, or chat. Binding to loopback is the safe default; PSK TLS is not a substitute for network segmentation or firewall policy.

## Run without Hermes

With the daemon running under the configured account, use the CLI directly:

```sh
python wireless-control/cli.py --socket "$SIMSTIM_OPERATOR_SOCKET" status
python wireless-control/cli.py --socket "$SIMSTIM_OPERATOR_SOCKET" tap-status
```

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

## Tests

From the repository root:

```sh
python3 -m unittest discover -s wireless-control/tests -v
python3 -m unittest discover -s attention-lab -p 'test_*.py' -v
python3 -m unittest discover -s ble-fallback/tests -p 'test_*.py' -v
bash ble-fallback/tests/run_firmware_tests.sh
```

These exercise host protocol paths with simulated peers and C firmware stubs; they do not establish compatibility with any physical device. Hardware/BlueZ integration, a matching device firmware build, and real audio/vision model assets are not included.

## Scope and deployment

`wireless-control/service.py` is the Wi-Fi TLS-PSK host. `ble-fallback/host/dual_transport_daemon.py` adds BlueZ GATT BLE while sharing the same authenticated device protocol and operator socket. Run each in a deliberately configured environment; the example systemd unit is a template and is not installed or enabled by this repository. Review its identity, paths, account, network exposure, and permissions before use.

`tools/tap_refresh.py` and `attention-lab/attention_broker.py` provide local reviewed-summary queuing and the optional USB tap path. The audio/vision path imports local dependencies only when invoked. No model weights, proprietary media, concrete private addresses, hostnames, PSKs, or device-specific serial identifiers are shipped.
