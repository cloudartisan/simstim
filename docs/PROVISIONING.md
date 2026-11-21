# USB Wi-Fi provisioning

`tools/provision_wireless.py` is the host-side caller for the standalone firmware's USB-only `WIFI_PROVISION` command. It sends Wi-Fi credentials and the device's 32-byte TLS-PSK key into the firmware's NVS record. The device reports `WIFI_STORED reboot_required`; reboot is a separate, manual action.

## Safety and inputs

- This command **does not flash, reset, or reboot** the board. It only sends the provisioning command after a matching ready banner.
- You must name the USB port explicitly with `--port`. It is opened exactly as supplied; there is no port discovery, fallback, or default. Close any other serial client first.
- The tool hashes the selected ELF and waits up to 30 seconds for `SIMSTIM_STANDALONE sha256=<that exact hash> uptime_s=<n> board_ready=1`. No command or credentials are sent until the complete hash and `board_ready=1` match. Build outputs now use unique candidate directories; pass `--elf` with the exact candidate ELF path you intend to provision. A compile or a different image is not accepted as a match.
- Supply Wi-Fi credentials either through hidden terminal prompts (the default) or one explicitly named JSON file. The JSON file must be owned by the current user, a regular file, and exactly mode `0600`; it must contain only string fields `ssid` and `password`. The tool does not inspect NetworkManager, environment variables, shell history, or other credential sources.
- The per-device TLS-PSK key is also required as an explicitly named, current-user-owned regular file with mode `0600`, containing exactly 32 raw bytes. Coordinate it using your authorized secret-management process; do not put it in this repository or pass it as a command-line value.
- SSID length is 1–32 UTF-8 bytes; password length is 8–64 UTF-8 bytes. The wire body is bounded to 130 bytes and has a little-endian length/CRC32 frame. Only fixed protocol statuses are displayed; serial-driver errors are sanitized.
- The tool does not invoke `sudo`, read active profiles, select another port, or attempt firmware recovery if the readiness check fails.

## Use

From the repository root, after building the intended standalone ELF and arranging the private device-key file:

```sh
python3 tools/provision_wireless.py \
  --port /dev/ttyACM0 \
  --device-key /secure/path/device.psk
```

The command will request the SSID in the terminal and prompt twice for the Wi-Fi password without echo. Replace `/dev/ttyACM0` with the exact port you have independently identified; the example does not select a device automatically. `pyserial` must be installed for the Python interpreter running the tool.

For a provisioned, user-selected JSON credential file instead:

```sh
python3 tools/provision_wireless.py \
  --port /dev/ttyACM0 \
  --credentials /secure/path/wifi.json \
  --device-key /secure/path/device.psk \
  --elf /absolute/path/to/the/exact-mode-candidate/k151_standalone.elf
```

Create the JSON file outside the repository using your secret manager or another secure method, and set its permissions to `0600`. Its format is:

```json
{"ssid":"<your SSID>","password":"<your Wi-Fi password>"}
```

Do not put actual credentials in a command line, environment variable, ticket, log, test fixture, or checked-in file. The examples use only placeholders; offline tests use fictional values and a fake serial peer.

## Offline tests

Run the tool's unit tests without pyserial, an ELF build, a USB device, or network access:

```sh
python3 -m unittest discover -s tests -p 'test_provision_wireless.py' -v
```

The fake-serial tests verify the ready/ELF gate, exact port usage, command/header/body order, length and CRC framing, refusal paths, file permissions, and diagnostic secret redaction. They never open a real serial port or use real credentials. Run the full public-repository Python tests with:

```sh
python3 -m unittest discover -s tests -v
```
