#!/usr/bin/env python3
"""Provision K151 Wi-Fi over an explicitly selected USB port.

This utility never discovers ports, reads NetworkManager, invokes sudo, or
flashes firmware. Credentials come only from an explicitly named private JSON
file or hidden terminal prompts. The device PSK is an explicit private file.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time
import zlib

MAX_SSID = 32
MAX_PASSWORD = 64
DEVICE_KEY_BYTES = 32
MAX_WIRE_BYTES = 130
BOOT_WAIT_SECONDS = 30.0
REPLY_WAIT_SECONDS = 12.0
DEFAULT_ELF = Path(__file__).resolve().parents[1] / "k151-standalone/build/k151_standalone.elf"
BANNER_PREFIX = "SIMSTIM_STANDALONE sha256="


class ProvisionError(Exception):
    """Failure with a deliberately non-sensitive diagnostic."""


def validate_credentials(ssid: str, password: str) -> tuple[bytes, bytes]:
    if not isinstance(ssid, str) or not isinstance(password, str):
        raise ProvisionError("Credential values must be UTF-8 strings.")
    try:
        ssid_bytes, password_bytes = ssid.encode("utf-8"), password.encode("utf-8")
    except UnicodeError as exc:
        raise ProvisionError("Credential values must be UTF-8 strings.") from exc
    if not 1 <= len(ssid_bytes) <= MAX_SSID:
        raise ProvisionError("SSID length must be 1-32 UTF-8 bytes.")
    if not 8 <= len(password_bytes) <= MAX_PASSWORD:
        raise ProvisionError("Wi-Fi password length must be 8-64 UTF-8 bytes.")
    if b"\0" in ssid_bytes or b"\0" in password_bytes:
        raise ProvisionError("Credential values contain unsupported bytes.")
    return ssid_bytes, password_bytes


def _read_private_file(path: Path, max_size: int) -> bytes:
    """Read a caller-selected, current-user-owned mode-0600 regular file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ProvisionError("Could not open the explicitly selected private file.") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > max_size):
            raise ProvisionError("Secret files must be current-user-owned regular files with mode 0600.")
        data = bytearray()
        while len(data) <= max_size:
            block = os.read(fd, min(4096, max_size + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        if len(data) > max_size:
            raise ProvisionError("Secret file is too large.")
        return bytes(data)
    except OSError as exc:
        raise ProvisionError("Could not read the explicitly selected private file.") from exc
    finally:
        os.close(fd)


def parse_json_credentials(path: Path) -> tuple[str, str]:
    try:
        data = json.loads(_read_private_file(path, 16_384).decode("utf-8"))
    except ProvisionError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProvisionError("Credential file must be valid UTF-8 JSON.") from exc
    if (not isinstance(data, dict) or set(data) != {"ssid", "password"} or
            not isinstance(data["ssid"], str) or not isinstance(data["password"], str)):
        raise ProvisionError('JSON must contain exactly string fields "ssid" and "password".')
    validate_credentials(data["ssid"], data["password"])
    return data["ssid"], data["password"]


def read_device_key(path: Path) -> bytes:
    key = _read_private_file(path, DEVICE_KEY_BYTES)
    if len(key) != DEVICE_KEY_BYTES:
        raise ProvisionError("Device PSK file must contain exactly 32 raw bytes.")
    return key


def verify_elf(path: Path) -> str:
    """Return SHA-256 only for a valid 32-bit little-endian Xtensa ELF."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as image:
            header = image.read(52)
            if (len(header) != 52 or header[:7] != b"\x7fELF\x01\x01\x01" or
                    struct.unpack_from("<HH", header, 16) != (2, 94) or
                    struct.unpack_from("<H", header, 40)[0] != 52):
                raise ProvisionError("Firmware image is not a valid 32-bit little-endian Xtensa ELF.")
            digest.update(header)
            for chunk in iter(lambda: image.read(1024 * 1024), b""):
                digest.update(chunk)
    except ProvisionError:
        raise
    except OSError as exc:
        raise ProvisionError("Could not read the firmware ELF.") from exc
    return digest.hexdigest()


def encode_payload(ssid: str, password: str, device_key: bytes) -> bytes:
    ssid_bytes, password_bytes = validate_credentials(ssid, password)
    if len(device_key) != DEVICE_KEY_BYTES:
        raise ProvisionError("Device PSK must contain exactly 32 raw bytes.")
    payload = bytes((len(ssid_bytes), len(password_bytes))) + ssid_bytes + password_bytes + device_key
    if not 43 <= len(payload) <= MAX_WIRE_BYTES:
        raise ProvisionError("Credential payload is outside protocol bounds.")
    return payload


def _read_line(serial, deadline: float) -> str:
    while time.monotonic() < deadline:
        raw = serial.readline(512)
        if raw:
            try:
                return raw.decode("ascii", "strict").strip()
            except UnicodeError as exc:
                raise ProvisionError("Device returned a malformed protocol response.") from exc
    raise ProvisionError("Timed out waiting for the device response.")


def run_usb(port: str, elf_hash: str, ssid: str, password: str, device_key: bytes,
            serial_factory=None) -> None:
    """Send the bounded protocol on exactly `port`; never reset or flash."""
    if not port or port != port.strip():
        raise ProvisionError("Specify the exact USB port path without surrounding whitespace.")
    if not re.fullmatch(r"[0-9a-f]{64}", elf_hash):
        raise ProvisionError("Verified ELF hash is malformed.")
    payload = encode_payload(ssid, password, device_key)
    if serial_factory is None:
        try:
            from serial import Serial
        except ImportError as exc:
            raise ProvisionError("pyserial is required for USB provisioning.") from exc
        serial_factory = Serial
    try:
        serial = serial_factory(None, 115200, exclusive=True, timeout=0.2, write_timeout=5)
        serial.dtr = False
        serial.rts = False
        serial.port = port
        with serial:
            banner_re = re.compile(
                rf"SIMSTIM_STANDALONE sha256={re.escape(elf_hash)} uptime_s=[0-9]+ board_ready=1"
            )
            deadline = time.monotonic() + BOOT_WAIT_SECONDS
            while time.monotonic() < deadline:
                if banner_re.fullmatch(serial.readline(512).decode("ascii", "replace").strip()):
                    break
            else:
                raise ProvisionError("Matching ready firmware ELF not observed; no command or credentials sent.")

            command = b"WIFI_PROVISION\n"
            if serial.write(command) != len(command):
                raise ProvisionError("Could not send USB provisioning request.")
            if _read_line(serial, time.monotonic() + REPLY_WAIT_SECONDS) != "WIFI_HEADER":
                raise ProvisionError("Device did not enter the expected provisioning protocol.")
            header = struct.pack("<II", len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
            if serial.write(header) != len(header):
                raise ProvisionError("Could not send the bounded USB header.")
            if _read_line(serial, time.monotonic() + REPLY_WAIT_SECONDS) != "WIFI_READY":
                raise ProvisionError("Device rejected the provisioning payload length.")
            if serial.write(payload) != len(payload):
                raise ProvisionError("USB credential transfer was incomplete.")
            serial.flush()
            if _read_line(serial, time.monotonic() + REPLY_WAIT_SECONDS) != "WIFI_STORED reboot_required":
                raise ProvisionError("Device did not confirm credential storage.")
    except ProvisionError:
        raise
    except Exception as exc:
        # Driver exception strings may contain received bytes; never expose them.
        raise ProvisionError("USB provisioning failed; diagnostic details withheld.") from exc


def _prompt_credentials() -> tuple[str, str]:
    try:
        import warnings
        with warnings.catch_warnings():
            # Never accept getpass's echo-enabled fallback when no TTY is available.
            warnings.simplefilter('error', getpass.GetPassWarning)
            ssid = getpass.getpass("Wi-Fi SSID (hidden): ")
            password = getpass.getpass("Wi-Fi password (hidden): ")
            confirm = getpass.getpass("Confirm Wi-Fi password (hidden): ")
    except (EOFError, OSError, getpass.GetPassWarning) as exc:
        raise ProvisionError("Could not read credentials from the terminal.") from exc
    if password != confirm:
        raise ProvisionError("Wi-Fi password confirmation did not match.")
    validate_credentials(ssid, password)
    return ssid, password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="exact USB serial device path; never auto-detected")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--credentials", type=Path, help="explicit mode-0600 JSON file with ssid and password")
    source.add_argument("--prompt", action="store_true", help="prompt for credentials without echo (default)")
    parser.add_argument("--device-key", type=Path, required=True,
                        help="explicit current-user-owned mode-0600 file containing 32 raw PSK bytes")
    parser.add_argument("--elf", type=Path, default=DEFAULT_ELF, help="firmware ELF whose running hash must match")
    args = parser.parse_args(argv)
    try:
        # Default to secure prompting; credentials are never sourced implicitly.
        ssid, password = parse_json_credentials(args.credentials) if args.credentials else _prompt_credentials()
        device_key = read_device_key(args.device_key)
        elf_hash = verify_elf(args.elf)
        run_usb(args.port, elf_hash, ssid, password, device_key)
        print("Wi-Fi credentials stored. Reboot the device to activate them.")
        return 0
    except ProvisionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        for name in ("password", "device_key"):
            if name in locals():
                del locals()[name]


if __name__ == "__main__":
    raise SystemExit(main())
