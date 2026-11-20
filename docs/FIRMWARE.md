# K151 standalone dual-radio firmware

This portable ESP-IDF application targets the ESP32-S3 K151/CoreS3 board. The default `dual` build includes the Wi-Fi TLS transport and BLE GATT/TLS fallback; `wifi` omits NimBLE. The application keeps board startup fail-closed: it first confirms the servo power gate is configured and read back low before declaring the board ready. The project does not flash, select a serial device, or activate firmware as part of a build.

## Source and third-party notices

Original SIMSTIM firmware is MIT-licensed by the repository `LICENSE`. The Feetech `write_pos.inc` excerpt is included with its accompanying `main/vendor/feetech/LICENSE` notice; do not remove or replace that notice. ESP-IDF and Espressif components are not copied into this repository: they are build dependencies described by the pinned ESP-IDF version and `dependencies.lock`. Their own licenses/notices remain those of their respective distributions.

The three locally bundled upstream cue recordings (`main/online.pcm`, `main/no_update.pcm`, and `main/check_failed.pcm`) are deliberately **not redistributed** because their licensing/provenance has not been established. `speaker.c` uses a short silent cue in their place. Host-streamed speech/audio is unchanged. No SDK-generated config, ELF/BIN, flash dump, credential or device record belongs in this source tree.

## Pinned dependencies and offline build

- ESP-IDF: **5.5.6** (exact; `dependencies.lock` resolves the component versions and hashes).
- Direct managed components: `espressif/esp_video` 2.6.0 and `espressif/esp_cam_sensor` 2.7.0. Transitive component versions/hashes are recorded in `dependencies.lock`.
- Target: `esp32s3`; board defaults retain the K151 camera/PSRAM, flash mode/size, partition layout, TLS-PSK and USB Serial/JTAG settings.

Install ESP-IDF 5.5.6 and populate an offline component mirror from the exact versions/hashes in `dependencies.lock` before building. `SIMSTIM_COMPONENTS_DIR` must point to a directory containing the extracted, pinned component folders (`cmake_utilities`, `esp_cam_sensor`, `esp_sccb_intf`, `esp_video`, and `usb_host_uvc`). Set `IDF_COMPONENT_MANAGER=0` to prevent registry access; the firmware sources and components are not vendored in this repository. The build has no serial-port or device-write step.

```sh
cd k151-standalone
export IDF_PATH=/path/to/esp-idf-v5.5.6
export SIMSTIM_COMPONENTS_DIR=/path/to/offline/components
export IDF_COMPONENT_MANAGER=0
./build.sh dual
# Optional Wi-Fi-only configuration, in a separate build directory:
SIMSTIM_BUILD_DIR="$PWD/build-wifi" ./build.sh wifi
```

`build.sh` sets `SDKCONFIG` under the selected build directory (not in the source tree), uses the checked-in defaults, and asks IDF to build only. Keep `sdkconfig`, `sdkconfig.old`, `build/`, `managed_components/`, and generated binaries out of public source control. For online component-manager builds, leave `IDF_COMPONENT_MANAGER` unset and use the locked `dependencies.lock`.

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
