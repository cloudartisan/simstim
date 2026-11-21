# SIMSTIM

Local-first device firmware and a companion host for the M5Stack StackChan K151. SIMSTIM connects to a host you run; the host can send commands and the device can send screen releases and other replies on the same connection. The host does not have to be a Hermes Agent.

## How the pieces fit

- **Firmware:** `k151-standalone/` and `ble-fallback/firmware/`. Wi-Fi is tried first. If it cannot establish a control session, the board can use Bluetooth LE. Only one transport carries commands at a time; a healthy Bluetooth session is not interrupted to switch back to Wi-Fi.
- **Host:** `wireless-control/` and `ble-fallback/host/`. On Wi-Fi the device opens a TLS connection over TCP; on Bluetooth it transports TLS over a GATT service. An operator or application talks to the local host rather than an unauthenticated command server on the board.
- **On demand:** screen taps, spoken updates, bounded movement, proximity reads and camera capture. A device playback receipt is not a claim that someone heard a message. A supported command is not proof that every combination of firmware, radio and hardware has been tested.

See [host setup](docs/HOST.md) for declared dependencies, the optional non-activating dual-radio systemd user-service template, isolated host tests, and the wire-parity acceptance checklist. See [firmware setup](docs/FIRMWARE.md) to configure the address, port and device identity. Then follow [USB provisioning](docs/PROVISIONING.md) to install your own network credentials and device key without rebuilding the firmware. Keep Wi-Fi credentials and per-installation keys outside source control. Never flash an unverified image or overwrite your only recovery copy.

## Development and adoption

This repository is intended as the upstream for both the board and its host. Develop on a branch, submit a pull request and test before updating a local installation. A merge must not automatically flash a board or replace a running service. See [Contributing](CONTRIBUTING.md), [upstream adoption](docs/ADOPTION.md) and [Security](SECURITY.md).

Original SIMSTIM code is [MIT licensed](LICENSE). Third-party dependencies retain their own licences; the MIT licence is not a grant for third-party models, voices or vendor firmware.
