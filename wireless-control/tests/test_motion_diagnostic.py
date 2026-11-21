import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cli
import motion_diagnostic
import service

NONCE = "0123456789abcdef"
RUN = 27


def success_lines(nonce=NONCE):
    points = [
        "LOOK_MOTOR result=event action=survey run_id=27 point=start state=observed requested_yaw_raw=-1 requested_pitch_raw=-1 measured_yaw_raw=256 measured_pitch_raw=512",
        "LOOK_MOTOR result=event action=survey run_id=27 point=centre state=settled requested_yaw_raw=256 requested_pitch_raw=512 measured_yaw_raw=256 measured_pitch_raw=512",
        "LOOK_MOTOR result=event action=survey run_id=27 point=view1 state=settled requested_yaw_raw=224 requested_pitch_raw=480 measured_yaw_raw=225 measured_pitch_raw=480",
        "LOOK_MOTOR result=event action=survey run_id=27 point=view2 state=settled requested_yaw_raw=288 requested_pitch_raw=480 measured_yaw_raw=287 measured_pitch_raw=480",
        "LOOK_MOTOR result=event action=survey run_id=27 point=return state=settled requested_yaw_raw=256 requested_pitch_raw=512 measured_yaw_raw=256 measured_pitch_raw=512",
        "LOOK_MOTOR result=event action=survey run_id=27 point=cleanup state=verified return=verified idle=verified_before_release torque_release=verified rail_off=verified",
        f"MOTION_END {nonce} ok=1",
    ]
    return [f"MOTION_BEGIN {nonce}", *points]


class FakeStream:
    def __init__(self, control, lines, *, change_generation=False):
        self.control = control
        self.lines = list(lines)
        self.writes = []
        self.change_generation = change_generation

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def readline(self, **_kwargs):
        if self.change_generation:
            self.control.generation += 1
            self.change_generation = False
        if not self.lines:
            raise TimeoutError("peer disconnected before terminal")
        return (self.lines.pop(0) + "\n").encode("ascii")


class MotionDiagnosticTests(unittest.TestCase):
    def test_parser_returns_private_raw_targets_and_measurements(self):
        result = motion_diagnostic.parse_motion_diagnostic(success_lines(), NONCE)
        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], RUN)
        self.assertEqual(result["events"][2]["requested"], {"yaw_raw": 224, "pitch_raw": 480})
        self.assertEqual(result["events"][2]["measured"], {"yaw_raw": 225, "pitch_raw": 480})
        self.assertIsNone(result["events"][0]["requested"])

    def test_parser_rejects_malformed_partial_loss_and_wrong_nonce(self):
        good = success_lines()
        cases = [
            good[:-1],
            good[:3] + good[4:],
            [*good[:2], good[2].replace("measured_pitch_raw=512", "measured_pitch_raw=x"), *good[3:]],
            [*good[:-1], f"MOTION_END {'f'*16} ok=1"],
            [*good[:2], good[3], good[2], *good[4:]],
            [*good[:2], good[2].replace("run_id=27", "run_id=28"), *good[3:]],
        ]
        for lines in cases:
            with self.subTest(lines=lines):
                with self.assertRaises(motion_diagnostic.MotionDiagnosticError):
                    motion_diagnostic.parse_motion_diagnostic(lines, NONCE)

    def _active(self, control, stream):
        control.connected = True
        control.current = stream
        control.peer = "simstim-device"
        control.device_id = "a" * 64
        control.generation = 4
        control._owner = "wifi"
        identity = control._identity()
        item = {"session": stream, "deadline": 10**12, "expected_identity": identity}
        return item

    def test_pinned_actions_report_disconnected_direct_dispatch(self):
        control = service.Control()
        identity = {"peer_identity":"simstim-device", "device_elf_sha256":"a"*64,
                    "transport":"wifi", "generation":1}
        for command in ("motion-diagnostic", "verify-cue online", "home"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(service.WirelessError, "no verified device"):
                    control._submit(command, expected_identity=identity)

    def test_actual_command_sends_one_nonce_once_and_returns_bound_evidence(self):
        control = service.Control()
        stream = FakeStream(control, success_lines())
        item = self._active(control, stream)
        control._active_item = item
        with patch.object(service.secrets, "token_hex", return_value=NONCE):
            result = control.execute(stream, "motion-diagnostic")
        self.assertEqual(stream.writes, [f"LOOK_MOTION {NONCE}\n".encode()])
        self.assertTrue(result["ok"])
        self.assertEqual(result["events"][1]["requested"], {"yaw_raw": 256, "pitch_raw": 512})
        self.assertEqual(result["device_elf_sha256"], "a" * 64)
        self.assertEqual(result["generation"], 4)
        self.assertEqual(item["session"], stream)

    def test_actual_command_rejects_malformed_partial_loss_wrong_nonce_and_changed_session_without_replay(self):
        for defect in ("malformed", "partial", "lost", "wrong_nonce", "session"):
            with self.subTest(defect=defect):
                control = service.Control()
                lines = success_lines()
                if defect == "malformed":
                    lines[2] = lines[2].replace("measured_pitch_raw=512", "measured_pitch_raw=x")
                elif defect == "partial":
                    lines = lines[:3] + lines[4:]
                elif defect == "lost":
                    lines = []
                elif defect == "wrong_nonce":
                    lines[-1] = f"MOTION_END {'f' * 16} ok=1"
                stream = FakeStream(control, lines, change_generation=(defect == "session"))
                item = self._active(control, stream)
                control._active_item = item
                with patch.object(service.secrets, "token_hex", return_value=NONCE):
                    with self.assertRaises(service.WirelessError):
                        control.execute(stream, "motion-diagnostic")
                self.assertEqual(stream.writes, [f"LOOK_MOTION {NONCE}\n".encode()])

    def test_actual_cli_routes_motion_to_one_shot_transport(self):
        calls = []
        output = io.StringIO()
        with patch.object(cli, "request_once", side_effect=lambda *args: calls.append(args) or {"ok": True, "result": {"evidence": "private"}}), \
             patch.object(cli, "request", side_effect=AssertionError("retrying request path used")), \
             patch.object(sys, "argv", ["simstim-wireless", "--socket", "/tmp/test.sock", "motion-diagnostic",
                                         "--expected-peer", "simstim-device", "--expected-elf", "a"*64,
                                         "--expected-transport", "wifi", "--expected-generation", "4"]), \
             contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:3], ("motion-diagnostic", 195))
        self.assertEqual(calls[0][3], {"peer_identity":"simstim-device", "device_elf_sha256":"a"*64,
                                       "transport":"wifi", "generation":4})
        self.assertIn('"evidence": "private"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
