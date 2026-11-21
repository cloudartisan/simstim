"""Native vertical-slice test of the production speaker touch/stage path."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("native_touch_harness.c")
FAKES = Path(__file__).with_name("native_touch_fakes")


class NativeStagedTouchTests(unittest.TestCase):
    def test_staged_update_plays_once_on_touch_and_event_parses(self):
        pcm = bytes([0x00,0x10, 0x00,0xf0, 0xff,0x7f, 0x00,0x80,
                    0x34,0x12, 0xcc,0xed, 0x01,0x00, 0xff,0xff]) + bytes(304)
        crc = zlib.crc32(pcm) & 0xffffffff
        self.assertEqual(len(pcm), 320)
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "native-touch"
            subprocess.run([
                os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-I", str(FAKES), str(HARNESS), "-o", str(binary),
            ], check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary), f"{crc:08x}"], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=5, check=True)
        out = result.stdout
        self.assertEqual([s for s in out.splitlines() if s.startswith("UPDATE_STATUS ")],
                         ["UPDATE_STATUS bytes=0 crc=00000000", f"UPDATE_STATUS bytes=320 crc={crc:08x}",
                          "UPDATE_STATUS bytes=0 crc=00000000"])
        event = re.search(r"^TAP_EVENT (\d+) (\d+)$", out, re.M)
        speaker = re.search(r"^SIMSTIM_SPEAKER result=played stereo_bytes=640 status=[0-9a-f]{4} reset_off=1$", out, re.M)
        self.assertIsNotNone(event, out)
        self.assertIsNotNone(speaker, out)
        self.assertEqual(len(re.findall(r"^TAP_EVENT ", out, re.M)), 1, out)
        self.assertNotRegex(out, r"(?m)^TAP_FETCH ", msg=out)
        self.assertLess(event.start(), speaker.start(), out)
        self.assertEqual(len(re.findall(r"^SIMSTIM_SPEAKER result=played ", out, re.M)), 1, out)
        self.assertRegex(out, r"NATIVE_ASSERT events=1 fetches=0 wire_bytes=328 pcm_bytes=320 stereo_bytes=640 payload_ok=1 i2s_enables=1 i2s_writes=2 reset_held=1 amp_volume=4003")

        # Feed the exact generated wire event through the production host parser.
        import sys
        from unittest.mock import patch
        from importlib import import_module
        with patch.object(sys, "path", [str(ROOT / "wireless-control"), *sys.path]):
            service = import_module("service")
        self.assertEqual(Path(service.__file__ or "").resolve(), ROOT / "wireless-control" / "service.py")

        class EventStream:
            def readline(self, timeout=None):
                return (event.group(0) + "\n").encode("ascii")

        control = service.Control()
        self.assertIsNone(control._readline(EventStream()))
        self.assertEqual(control.last_event["count"], int(event.group(1)))
        self.assertEqual(control.last_event["uptime_ms"], int(event.group(2)))


if __name__ == "__main__":
    unittest.main()
