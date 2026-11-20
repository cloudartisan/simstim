"""Offline safeguards and framed-protocol tests; no real port or credentials."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("provision_wireless", ROOT / "tools" / "provision_wireless.py")
assert _SPEC is not None and _SPEC.loader is not None
provision = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(provision)

FICTIONAL_SSID = "ExampleLab"
FICTIONAL_PASSWORD = "fictional-pass-42"
FICTIONAL_KEY = bytes(range(32))
FAKE_ELF_HASH = "a" * 64


class FakeSerial:
    def __init__(self, *, banner_hash=FAKE_ELF_HASH, header_reply=b"WIFI_HEADER\n",
                 ready_reply=b"WIFI_READY\n", final_reply=b"WIFI_STORED reboot_required\n"):
        self.lines = [f"SIMSTIM_STANDALONE sha256={banner_hash} uptime_s=7 board_ready=1\n".encode(),
                      header_reply, ready_reply, final_reply]
        self.writes = []
        self.dtr = None
        self.rts = None
        self.port = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def readline(self, _limit):
        return self.lines.pop(0) if self.lines else b""

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass


class ProvisionWirelessTests(unittest.TestCase):
    def make_private_file(self, directory: Path, name: str, contents: bytes) -> Path:
        path = directory / name
        path.write_bytes(contents)
        path.chmod(0o600)
        return path

    def test_json_credentials_require_explicit_private_mode_0600_file(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = self.make_private_file(folder, "fictional.json", json.dumps(
                {"ssid": FICTIONAL_SSID, "password": FICTIONAL_PASSWORD}).encode())
            self.assertEqual(provision.parse_json_credentials(path),
                             (FICTIONAL_SSID, FICTIONAL_PASSWORD))
            path.chmod(0o640)
            with self.assertRaises(provision.ProvisionError):
                provision.parse_json_credentials(path)

    def test_private_file_rejects_symlink_and_extra_json_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            real = self.make_private_file(folder, "real.json", json.dumps(
                {"ssid": FICTIONAL_SSID, "password": FICTIONAL_PASSWORD, "extra": "ignored"}).encode())
            with self.assertRaises(provision.ProvisionError):
                provision.parse_json_credentials(real)
            link = folder / "link.json"
            link.symlink_to(real)
            with self.assertRaises(provision.ProvisionError):
                provision.parse_json_credentials(link)

    def test_credentials_are_bounded_by_encoded_utf8_bytes(self):
        with self.assertRaises(provision.ProvisionError):
            provision.validate_credentials("", FICTIONAL_PASSWORD)
        with self.assertRaises(provision.ProvisionError):
            provision.validate_credentials("x", "short")
        with self.assertRaises(provision.ProvisionError):
            provision.validate_credentials("é" * 17, FICTIONAL_PASSWORD)

    def test_payload_lengths_and_crc_header_match_firmware_wire_format(self):
        payload = provision.encode_payload(FICTIONAL_SSID, FICTIONAL_PASSWORD, FICTIONAL_KEY)
        self.assertEqual(payload[:2], bytes((10, 17)))
        self.assertEqual(payload[2:12], FICTIONAL_SSID.encode())
        self.assertEqual(payload[12:29], FICTIONAL_PASSWORD.encode())
        self.assertEqual(payload[29:], FICTIONAL_KEY)
        framed = struct.pack("<II", len(payload), zlib.crc32(payload) & 0xffffffff)
        self.assertEqual(struct.unpack("<II", framed),
                         (len(payload), zlib.crc32(payload) & 0xffffffff))
        self.assertLessEqual(len(payload), provision.MAX_WIRE_BYTES)

    def test_successful_fake_handshake_sends_exact_port_and_expected_frames(self):
        fake = FakeSerial()
        ports = []

        def factory(*args, **kwargs):
            ports.append((args, kwargs))
            return fake

        provision.run_usb("/dev/fake-provision-port", FAKE_ELF_HASH, FICTIONAL_SSID,
                          FICTIONAL_PASSWORD, FICTIONAL_KEY, serial_factory=factory)
        payload = provision.encode_payload(FICTIONAL_SSID, FICTIONAL_PASSWORD, FICTIONAL_KEY)
        self.assertEqual(fake.port, "/dev/fake-provision-port")
        self.assertEqual(fake.dtr, False)
        self.assertEqual(fake.rts, False)
        self.assertTrue(fake.closed)
        self.assertEqual(fake.writes[0], b"WIFI_PROVISION\n")
        self.assertEqual(fake.writes[1], struct.pack("<II", len(payload), zlib.crc32(payload) & 0xffffffff))
        self.assertEqual(fake.writes[2], payload)
        self.assertEqual(len(fake.writes), 3)

    def test_wrong_ready_elf_or_port_fails_before_secrets_are_sent(self):
        wrong_ready = FakeSerial(ready_reply=b"WIFI_ERROR length\n")
        with self.assertRaises(provision.ProvisionError):
            provision.run_usb("/dev/fake", FAKE_ELF_HASH, FICTIONAL_SSID,
                              FICTIONAL_PASSWORD, FICTIONAL_KEY,
                              serial_factory=lambda *_a, **_k: wrong_ready)
        self.assertEqual(len(wrong_ready.writes), 2)
        self.assertEqual(wrong_ready.writes[0], b"WIFI_PROVISION\n")
        self.assertNotIn(provision.encode_payload(FICTIONAL_SSID, FICTIONAL_PASSWORD, FICTIONAL_KEY),
                         wrong_ready.writes)

        mismatched = FakeSerial(banner_hash="b" * 64)
        with mock.patch.object(provision, "BOOT_WAIT_SECONDS", 0.01):
            with self.assertRaises(provision.ProvisionError):
                provision.run_usb("/dev/fake", FAKE_ELF_HASH, FICTIONAL_SSID,
                                  FICTIONAL_PASSWORD, FICTIONAL_KEY,
                                  serial_factory=lambda *_a, **_k: mismatched)
        self.assertEqual(mismatched.writes, [])

        with self.assertRaises(provision.ProvisionError):
            provision.run_usb(" /dev/fake ", FAKE_ELF_HASH, FICTIONAL_SSID,
                              FICTIONAL_PASSWORD, FICTIONAL_KEY,
                              serial_factory=lambda *_a, **_k: self.fail("must not open port"))

    def test_device_key_requires_explicit_private_exact_32_byte_file(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = self.make_private_file(folder, "fictional.key", FICTIONAL_KEY)
            self.assertEqual(provision.read_device_key(path), FICTIONAL_KEY)
            path.write_bytes(FICTIONAL_KEY[:-1])
            with self.assertRaises(provision.ProvisionError):
                provision.read_device_key(path)

    def test_elf_validation_and_hash_are_pinned_to_file_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fake.elf"
            header = bytearray(52)
            header[:7] = b"\x7fELF\x01\x01\x01"
            struct.pack_into("<HH", header, 16, 2, 94)
            struct.pack_into("<H", header, 40, 52)
            path.write_bytes(header + b"synthetic ELF payload")
            self.assertEqual(provision.verify_elf(path), hashlib.sha256(path.read_bytes()).hexdigest())
            path.write_bytes(b"not an ELF")
            with self.assertRaises(provision.ProvisionError):
                provision.verify_elf(path)

    def test_driver_exception_text_is_never_exposed(self):
        secret = FICTIONAL_PASSWORD

        class BrokenSerial(FakeSerial):
            def readline(self, _limit):
                raise RuntimeError("driver leaked " + secret)

        with self.assertRaises(provision.ProvisionError) as caught:
            provision.run_usb("/dev/fake", FAKE_ELF_HASH, FICTIONAL_SSID,
                              secret, FICTIONAL_KEY, serial_factory=lambda *_a, **_k: BrokenSerial())
        self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
