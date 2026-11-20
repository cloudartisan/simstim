"""Regression checks for single-owner command and protocol routing."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WIRE = (ROOT / "main/wire_io.c").read_text()
SPEAKER = (ROOT / "main/speaker.c").read_text()
CAMERA = (ROOT / "main/look_camera.c").read_text()
MOTOR = (ROOT / "main/look_motor.c").read_text()


class WireRoutingTests(unittest.TestCase):
    def test_wifi_transaction_requires_and_latches_a_generation(self):
        self.assertIn("owner == WIRE_WIFI && session == 0", WIRE)
        self.assertIn("s_session = owner == WIRE_WIFI ? session : 0", WIRE)
        self.assertIn("wifi_link_read(s_session", WIRE)
        self.assertIn("wifi_link_write(s_session", WIRE)

    def test_camera_frames_and_motor_status_use_central_output(self):
        self.assertIn('wire_printf("LOOK_DATA %s\\n",out)', CAMERA)
        self.assertIn('wire_printf("LOOK_FRAME_BEGIN', CAMERA)
        self.assertIn('wire_printf("LOOK_MOTOR result=', MOTOR)
        self.assertNotRegex(CAMERA, r'(?<!wire_)printf\("LOOK_DATA')

    def test_usb_provisioning_is_only_dispatched_from_usb(self):
        self.assertIn('owner==WIRE_USB&&!strcmp(cmd,"WIFI_PROVISION")', SPEAKER)
        self.assertIn("simstim_wifi_provision_usb()", SPEAKER)

    def test_release_tap_event_has_count_and_millisecond_uptime(self):
        self.assertIn('wire_printf("TAP_EVENT %lu %lu\\n"', SPEAKER)
        self.assertIn("xTaskGetTickCount()*portTICK_PERIOD_MS", SPEAKER)
        self.assertIn('if(!strcmp(cmd,"TAP_CHECK")){tap_refresh();return;}', SPEAKER)

    def test_idle_banners_are_broadcast_atomically_and_usb_uses_stdio(self):
        main = (ROOT / "main/main.c").read_text()
        self.assertIn("wire_broadcast_printf(\"SIMSTIM_STANDALONE", main)
        self.assertIn("wifi_link_session()", WIRE)
        self.assertIn("fwrite(data, 1, length, stdout)", WIRE)
        self.assertIn("xSemaphoreTake(s_wire_lock", WIRE)

    def test_tap_uses_live_wifi_session_and_status_commands_exist(self):
        self.assertIn("tap_session=wifi_link_session()", SPEAKER)
        self.assertIn("tap_owner=tap_session?WIRE_WIFI:WIRE_USB", SPEAKER)
        self.assertIn('!strcmp(cmd,"TOUCH_STATUS")', SPEAKER)
        self.assertIn('!strcmp(cmd,"PROX_STATUS")', SPEAKER)
        self.assertIn('wire_printf("TOUCH %s %u\\n"', SPEAKER)
        self.assertIn('wire_printf("PROX %s %u %u\\n"', SPEAKER)
        self.assertIn("update_owner=wire_owner();update_session=wire_session()", SPEAKER)
        self.assertIn("speak_owner!=wire_owner()", SPEAKER)

    def test_camera_frame_lines_are_bounded_and_actual_lengths_validated(self):
        self.assertIn("#define BASE64_CHUNK_BYTES 54u", CAMERA)
        self.assertIn("b.bytesused < CAM_RAW_BYTES", CAMERA)
        self.assertIn("b.bytesused > s->mapping_len", CAMERA)
        self.assertIn("s->buffer.length < CAM_RAW_BYTES", CAMERA)


if __name__ == "__main__":
    unittest.main()
