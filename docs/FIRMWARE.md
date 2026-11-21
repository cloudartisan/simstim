# K151 standalone dual-radio firmware

This portable ESP-IDF application targets the ESP32-S3 K151/CoreS3 board. The default `dual` build includes Wi-Fi TLS and BLE GATT/TLS fallback; `wifi` omits NimBLE. The application keeps board startup fail-closed: it first confirms the servo power gate is configured and read back low before declaring the board ready. The project does not flash, select a serial device, or activate firmware as part of a build.

## Source and third-party notices

Original SIMSTIM firmware is MIT-licensed by the repository `LICENSE`. This revision no longer bundles the Feetech source excerpt. Earlier revisions still contain the previously attributed copy. The build prepares only the `WritePos` function from the immutable upstream revision recorded in `k151-standalone/feetech-source.lock`; the complete fetched source is checked against its pinned SHA-256 before extraction. Prepared files live in a sibling generated dependency directory adjacent to the selected build directory, not in the source tree or CMake build directory. ESP-IDF and Espressif components are **not bundled**: they are resolved from the pinned ESP-IDF version and `dependencies.lock`. Their licenses/notices remain those of their respective distributions.

Three build-generated Kokoro cues (`Online`, `No update`, and `Check failed`) are linked into the firmware from generated C arrays. Their model and voice assets are not bundled; see [CUES.md](CUES.md) for exact upstream asset URLs, hashes and setup. Host-streamed speech/audio is unchanged. No SDK-generated config, ELF/BIN, flash dump, credential or device record belongs in this source tree.

## Pinned dependencies and clean-clone build

- ESP-IDF: **5.5.6** (exact; `dependencies.lock` pins component versions and native content hashes).
- Direct managed components: `espressif/esp_video` 2.6.0 and `espressif/esp_cam_sensor` 2.7.0. The full resolved dependency set is in `dependencies.lock`.
- Target: `esp32s3`; board defaults retain the K151 camera/PSRAM, flash mode/size, partition layout, TLS-PSK and USB Serial/JTAG settings.

The project does not bundle Espressif component source. The IDF Component Manager downloads those dependencies using the lock file. An offline mirror is optional; when preparing one, the helper invokes the active IDF Component Manager's native directory-hash verifier against each lock hash before and after copying. It does not treat `.component_hash` as proof by itself. This requires the Python environment installed by ESP-IDF 5.5.6, so source `export.sh` before running the helper. Unsafe, broken, looping, absolute, or escaping symlinks are rejected.

### Pinned Feetech source preparation

`build.sh` downloads the full upstream `feetech_scs.cpp` from the immutable commit and checks its complete SHA-256 against `feetech-source.lock`. It then extracts only the `WritePos` definition into the sibling generated dependency directory adjacent to the selected build directory. A mirror is the original upstream file, not the generated excerpt; the same hash check runs for both network and mirror input, so changing the source while leaving any mirror marker untouched is rejected. The `WritePos` implementation and command regression are compiled by the host motor tests. To make a verified mirror, obtain the file from the locked URL and verify it before storing it outside the checkout:

```sh
mkdir -p "$HOME/.cache/simstim-upstream/feetech"
curl --fail --location --output "$HOME/.cache/simstim-upstream/feetech/feetech_scs.cpp" \
  'https://raw.githubusercontent.com/kisaragi-mochi/stackchan-mcp/03fbd92774c37844e11ea097e632df28f2a5f4e5/firmware/components/feetech_scs/feetech_scs.cpp'
printf '%s  %s\n' \
  'c5309d884091328456237e5c62486b892a6192e6ea272895b364f765f0b31f38' \
  "$HOME/.cache/simstim-upstream/feetech/feetech_scs.cpp" | sha256sum --check
SIMSTIM_FEETECH_MIRROR="$HOME/.cache/simstim-upstream/feetech" \
SIMSTIM_OFFLINE=1 SIMSTIM_BUILD_DIR="$HOME/.cache/simstim-k151-offline" ./build.sh dual
```

Without a mirror, `build.sh` fetches from the pinned URL. `SIMSTIM_OFFLINE=1` requires `SIMSTIM_FEETECH_MIRROR`; it fails instead of reaching the network if no mirror is supplied. The preparer verifies bytes itself on every invocation, regardless of a checksum file or marker alongside the mirror.

### Clean checkout: install and build from scratch

Use a new checkout and install ESP-IDF 5.5.6 using Espressif's [versioned setup guide](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32s3/get-started/index.html). A standard Linux install can be prepared with these commands (install required OS packages as listed in the guide first):

```sh
mkdir -p "$HOME/esp"
git clone --recursive --branch v5.5.6 https://github.com/espressif/esp-idf.git "$HOME/esp/esp-idf-v5.5.6"
cd "$HOME/esp/esp-idf-v5.5.6"
./install.sh esp32s3
. ./export.sh
```

Then use a new project checkout. The build installs no dependencies into the repository: cue-renderer dependencies/assets are external, and IDF components are pinned in `dependencies.lock`. Only this checkout and those declared dependencies are needed; no sibling project source or pre-populated `managed_components` is required. The online build resolves components against the committed lock.

```sh
git clone https://github.com/cloudartisan/simstim.git simstim-clean
cd simstim-clean/k151-standalone
export IDF_PATH="$HOME/esp/esp-idf-v5.5.6"   # set to your ESP-IDF 5.5.6 installation
. "$IDF_PATH/export.sh"
cd ..
python3 -m pip install --user uv
uv venv "$HOME/.local/share/simstim/voice/render"
uv pip install --python "$HOME/.local/share/simstim/voice/render/bin/python" \
  --require-hashes -r k151-standalone/host/requirements-cues.lock
mkdir -p "$HOME/.local/share/simstim/voice"
curl --fail --location --output "$HOME/.local/share/simstim/voice/kokoro-v1.0.onnx" \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl --fail --location --output "$HOME/.local/share/simstim/voice/voices-v1.0.bin" \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
cd k151-standalone
unset IDF_COMPONENT_MANAGER SIMSTIM_COMPONENTS_DIR
SIMSTIM_BUILD_DIR="$HOME/.cache/simstim-k151-dual" ./build.sh dual
```

For Wi-Fi-only configuration, select `wifi` explicitly; both modes can safely share the same output root:

```sh
SIMSTIM_BUILD_DIR="$HOME/.cache/simstim-k151" ./build.sh wifi
```

Each invocation creates a new mode-labelled candidate directory beneath `SIMSTIM_BUILD_DIR` (default: `k151-standalone/build`). It passes that candidate's absolute `sdkconfig` explicitly to IDF, so generated configuration and build products are not reused across modes or overwritten on later invocations. Existing configs and outputs in the chosen build root are left untouched; keep generated candidate directories out of source control. Builds compile only; they do not select a serial port, flash, or activate firmware. Provisioning-specific variables are unnecessary for the default compile and no live endpoint or secret is supplied here.

### Optional component mirror

To prepare an offline mirror, resolve dependencies in a new online build first. Keep the resolver output and mirror outside the checkout.

```sh
cd simstim-clean/k151-standalone
export IDF_PATH="$HOME/esp/esp-idf-v5.5.6"
. "$IDF_PATH/export.sh"
unset IDF_COMPONENT_MANAGER
idf.py -B "$HOME/.cache/simstim-idf-deps-resolve" set-target esp32s3 reconfigure
python3 ../scripts/prepare-firmware-components.py --source "$HOME/.cache/simstim-idf-deps-resolve/managed_components" --destination "$HOME/.cache/simstim-idf-components"
```

Then build offline using the mirror:

```sh
cd simstim-clean/k151-standalone
export IDF_PATH="$HOME/esp/esp-idf-v5.5.6"
. "$IDF_PATH/export.sh"
export SIMSTIM_COMPONENTS_DIR="$HOME/.cache/simstim-idf-components"
export IDF_COMPONENT_MANAGER=0
SIMSTIM_BUILD_DIR="$HOME/.cache/simstim-k151-offline" ./build.sh dual
```

The resolver needs registry access. Do not substitute similarly named components or refresh `dependencies.lock` during routine setup; review lock-file changes as dependency updates.

### Historical patch disposition and provenance

The local history audit identified five patch artifacts with no source URL/sign-off provenance. They are **not redistributed** or applied by this project. The public firmware is the standalone implementation in this directory; the table records whether each historical behavior is already present, intentionally absent, or superseded so the old patches are not mistaken for build inputs.

| Historical artifact | Disposition in this public firmware | Public implementation / boundary |
|---|---|---|
| `simstim-k151-source.patch` | Superseded as a whole; it modifies a different StackChan/Xiaozhi application tree. | Standalone K151 app, board startup, wire protocol, host link and screen are implemented in `main/`; no alternate application patch is required. |
| `simstim-k151-tls-unprovisioned.patch` | Superseded as a patch; its secure local-link intent is independently implemented. | `wifi_link.c` and `ble_channel.c` use TLS-PSK; no production endpoint/key is embedded. Public defaults are deliberately unusable. See configuration/provisioning docs. |
| `simstim-k151-offline-trial.patch` | Historical trial patch, not a public patch input. | Standalone firmware independently includes read-only proximity status and camera/motor commands; proximity does not autonomously trigger speech or movement. No trial image, selector operation or device-specific calibration is included. |
| `ltr553-proximity.patch` | Patch superseded; sensor readout behavior is independently implemented. | `board.c` validates the LTR-553 identity/configuration and exposes sampled counts/status. Missing or invalid sensor state is reported unavailable; this does not establish calibrated distance or presence. |
| `k151-clock-commissioning.delta.patch` | Superseded commissioning delta; not part of the standalone release. | The public app reports uptime, not wall-clock time. No RTC/NTP/clock commissioning behavior is claimed or included. Add a separately reviewed clock feature only if a public use case requires it. |

For motion timing, the `WritePos` definition is fetched from [kisaragi-mochi/stackchan-mcp at `03fbd92774c37844e11ea097e632df28f2a5f4e5`](https://github.com/kisaragi-mochi/stackchan-mcp/tree/03fbd92774c37844e11ea097e632df28f2a5f4e5/firmware/components/feetech_scs); the exact raw-file URL and SHA-256 are in `k151-standalone/feetech-source.lock`. SIMSTIM's echo-aware servo transport, validation, test harness, and regression coverage are local code. The upstream component's license at that pinned revision is MIT (see its [LICENSE](https://github.com/kisaragi-mochi/stackchan-mcp/blob/03fbd92774c37844e11ea097e632df28f2a5f4e5/firmware/components/feetech_scs/LICENSE)); the original notice is retained at `k151-standalone/main/vendor/feetech/LICENSE`. Keep that notice with generated copies of the upstream code. The source excerpt is generated only in the sibling dependency directory adjacent to the selected build directory and is not tracked. Do not copy vendor patches or calibration values from a particular device into a public build.

For host-native source checks, run from the repository root:

```sh
python3 -m unittest discover -s k151-standalone/host -p 'test_*.py' -v
```

These host checks and a successful compile do not prove physical Wi-Fi/BLE operation, board safety on an arbitrary clone, or real actuator/media acceptance. This staged copy has not been flashed or activated.

## Configuration contract (firmware and host worker)

The firmware build reads these environment variables when CMake configures. Defaults are deliberately non-production: `example.invalid`, port `1`, identity/hostname `simstim-device`, and BLE peer `00:00:00:00:00:00`. The default endpoint and peer cannot be treated as a deployable configuration.

| Variable | Used by | Meaning |
|---|---|---|
| `SIMSTIM_LINK_HOST` | firmware build | DNS hostname or IPv4 address the device connects to for TLS. Must route to the configured host-worker listener. |
| `SIMSTIM_LINK_PORT` | firmware build | Device-side TCP port (1–65535); set equal to host `SIMSTIM_PORT`. |
| `SIMSTIM_LINK_IDENTITY` | firmware build | Device TLS-PSK identity for Wi-Fi and BLE; set equal to host `SIMSTIM_PEER_IDENTITY`. |
| `SIMSTIM_DEVICE_HOSTNAME` | firmware build | Device DHCP hostname (one DNS label). |
| `SIMSTIM_BLE_PEER_ADDRESS` | firmware build | The host BLE gateway adapter address, six colon-separated octets. Firmware uses the BLE public-address type; supply the host adapter's public address, not a random/private BLE address. NimBLE byte ordering is converted in firmware. |
| `SIMSTIM_BIND` | host worker (`wireless-control/service.py`) | Local address on which the TLS-PSK service listens; expose only on an authorized interface/network. |
| `SIMSTIM_PORT` | host worker | TLS-PSK listener port; set equal to firmware `SIMSTIM_LINK_PORT`. |
| `SIMSTIM_PEER_IDENTITY` | host worker | Expected TLS-PSK peer identity; set equal to firmware `SIMSTIM_LINK_IDENTITY`. |
| `SIMSTIM_KEY_PATH` | host worker / BLE gateway | Path to the protected per-device PSK key file. The provisioning workflow must install the matching secret on the device; never put the key in source, build variables, logs or receipts. |
| `SIMSTIM_SERIAL_PORT` | optional host-side physical observation tool | Authorized USB Serial/JTAG device path; defaults to `/dev/ttyUSB0`, with no device-specific ID in source. Not used by the build. |
| `SIMSTIM_WIFI_SSID` / `SIMSTIM_WIFI_PASSWORD` | private provisioning caller (if used) | Inputs to the bounded USB Wi-Fi provisioning protocol, not firmware build variables. This source tree does not implement an environment-variable provisioning CLI. Keep these values in a secret-safe provisioning system; never commit them. |

### Per-unit motor calibration

Motor motion is disabled in an unconfigured build. Before the first CMake configure, a device owner must provide **all ten** calibration values below via the build environment; a partial set is rejected. These are device-specific measurements, not defaults to copy from another unit. The CMake checks reject malformed/out-of-range values, and firmware repeats runtime validation. If calibration is missing or invalid, every motor-capable `LOOK_*` operation (including `LOOK_SURVEY` and `LOOK_PROBE`) is rejected in the command dispatcher before cue playback, camera setup/capture, servo power, or other operation work. Camera-only `LOOK_FRAME` remains independent. A sampled starting pose outside the calibrated axis limits is rejected without an automatic correction move.

| Variable | Meaning |
|---|---|
| `SIMSTIM_MOTOR_YAW_ZERO_RAW` / `SIMSTIM_MOTOR_PITCH_ZERO_RAW` | Raw servo position measured at that axis's chosen 0-degree reference (0–1023). |
| `SIMSTIM_MOTOR_RAW_PER_DEG_NUM` / `SIMSTIM_MOTOR_RAW_PER_DEG_DEN` | Positive rational raw-counts-per-degree scale (each 1–1024). |
| `SIMSTIM_MOTOR_YAW_DIRECTION` / `SIMSTIM_MOTOR_PITCH_DIRECTION` | Sign mapping from positive logical degrees to raw counts; each must be `1` or `-1`. |
| `SIMSTIM_MOTOR_YAW_MIN_DEG` / `SIMSTIM_MOTOR_YAW_MAX_DEG` | Inclusive safe logical yaw range, within -180..180 degrees. |
| `SIMSTIM_MOTOR_PITCH_MIN_DEG` / `SIMSTIM_MOTOR_PITCH_MAX_DEG` | Inclusive safe logical pitch range, within -180..180 degrees. |

Calibrate with the actuator mechanically supported and the approved device-specific procedure; determine the reference, direction, scale and safe limits from that unit rather than inferred/example values. Never use a source checkout or build to discover calibration by moving an uncalibrated device. Calibration enables only the existing bounded motion behavior; it does not establish that the measured range is mechanically safe. Survey and relative-look spans (30-degree pan and 8-degree tilt) are behavior limits, additionally clipped by the configured logical limits and start-pose checks; they are not calibration measurements.

Example variable names only (replace every placeholder with measurements for the target unit; these are not values):

```sh
export SIMSTIM_MOTOR_YAW_ZERO_RAW='<measured integer>'
export SIMSTIM_MOTOR_PITCH_ZERO_RAW='<measured integer>'
export SIMSTIM_MOTOR_RAW_PER_DEG_NUM='<measured numerator>'
export SIMSTIM_MOTOR_RAW_PER_DEG_DEN='<measured denominator>'
export SIMSTIM_MOTOR_YAW_DIRECTION='<1 or -1>'
export SIMSTIM_MOTOR_PITCH_DIRECTION='<1 or -1>'
export SIMSTIM_MOTOR_YAW_MIN_DEG='<measured safe minimum>'
export SIMSTIM_MOTOR_YAW_MAX_DEG='<measured safe maximum>'
export SIMSTIM_MOTOR_PITCH_MIN_DEG='<measured safe minimum>'
export SIMSTIM_MOTOR_PITCH_MAX_DEG='<measured safe maximum>'
```

Set firmware endpoint variables in the build worker's protected environment **before first CMake configure**; changing them requires reconfiguration/rebuild. Configure host-worker `SIMSTIM_BIND`, `SIMSTIM_PORT`, `SIMSTIM_PEER_IDENTITY`, and `SIMSTIM_KEY_PATH` in its protected service environment. `SIMSTIM_LINK_HOST` must resolve/reach the host listener; `SIMSTIM_LINK_PORT` must match `SIMSTIM_PORT`; and the TLS identity and PSK must match on both ends. Provision Wi-Fi credentials and the PSK through an authorized caller of the bounded USB provisioning protocol (credentials are stored in NVS); provisioning is separate from compilation and is not performed here. The source never contains Wi-Fi credentials, a live endpoint, a device BLE address, or a production PSK.

Example for a private test environment (replace placeholders in a secret-safe shell/session; do not commit them):

```sh
export SIMSTIM_LINK_HOST='firmware-host.example'
export SIMSTIM_LINK_PORT='18767'
export SIMSTIM_LINK_IDENTITY='simstim-device'
export SIMSTIM_DEVICE_HOSTNAME='simstim-device'
export SIMSTIM_BLE_PEER_ADDRESS="${HOST_BLE_PUBLIC_ADDRESS:?set to the authorized host BLE public address}"
./build.sh dual
```

On the host worker, set `SIMSTIM_BIND`, `SIMSTIM_PORT`, `SIMSTIM_PEER_IDENTITY`, and `SIMSTIM_KEY_PATH` in its protected service environment; use the same port/identity and matching secret as the device. Only non-secret values belong in build logs. Network credentials and PSKs must stay out of shell history and generated/public files. No production values are prescribed here.

## Hardware / safety boundary

This is board-specific code for the K151/CoreS3 ESP32-S3 design, not a generic ESP32 image. Keep the original board power gate, I2C/SPI/I2S pin assignments, flash layout and actuator bounds intact when adapting. The build does not select an attached device or write flash. Do not infer that compile-time checks validate the actual module revision, wiring, servo calibration, transport peer, or safe operation; perform separate authorized preflight and physical acceptance before any installation.
