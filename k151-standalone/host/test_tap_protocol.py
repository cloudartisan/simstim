"""Source-level regression checks for the firmware USB tap protocol."""
from pathlib import Path
import re
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "main" / "speaker.c").read_text()


class TapProtocolTests(unittest.TestCase):
    def test_empty_tap_fetch_and_usb_diagnostic_share_handler(self):
        self.assertIn("static void tap_update(void){tap_refresh();}", SOURCE)
        self.assertIn('if(!strcmp(cmd,"TAP_CHECK")){tap_refresh();return;}', SOURCE)
        self.assertIn('wire_printf("TAP_FETCH %s\\n",id)', SOURCE)

    def test_id_is_boot_random_seeded_and_monotonic(self):
        self.assertIn("tap_boot_nonce=esp_random()", SOURCE)
        self.assertIn("uint32_t request_id=tap_boot_nonce+serial", SOURCE)
        self.assertRegex(SOURCE, r'snprintf\(id,sizeof\(id\),"%08lx"')

    def test_only_matching_none_is_no_update(self):
        self.assertIn('snprintf(expected,sizeof(expected),"TAP_NONE %s",id)', SOURCE)
        self.assertIn('if(!strcmp(line,expected)){bool played=play_pcm(quiet_clip', SOURCE)
        self.assertIn('snprintf(expected,sizeof(expected),"TAP_FAIL %s",id)', SOURCE)
        self.assertIn('tap_done(id,"failed")', SOURCE)

    def test_audio_handshake_has_bounds_crc_and_terminal_result(self):
        for text in (
            '"TAP_AUDIO %s",id', '"TAP_HEADER %s\\n",id',
            '"TAP_READY %s\\n",id', 'len<320||len>320000||len%2',
            'esp_rom_crc32_le(0,pcm,len)!=crc', 'tap_done(id,played?"played":"failed")',
            "#define TAP_WAIT_MS 15000",
        ):
            self.assertIn(text, SOURCE)

    def test_positive_staged_update_and_expiry_remain(self):
        self.assertIn('bool staging=!strcmp(cmd,"STAGE_UPDATE")', SOURCE)
        self.assertIn('if(update_pcm){if(update_owner==wire_owner() && update_session==wire_session())play_pcm(update_pcm,update_bytes);clear_update();return;}', SOURCE)
        self.assertIn('update_owner=wire_owner();update_session=wire_session()', SOURCE)
        self.assertIn('pdMS_TO_TICKS(1800000)', SOURCE)

    def test_response_is_bound_to_request_id(self):
        # Every accepted response is built by exact matching against this request.
        self.assertEqual(len(re.findall(r'snprintf\(expected,sizeof\(expected\),"TAP_(?:NONE|FAIL|AUDIO) %s",id\)', SOURCE)), 3)


if __name__ == "__main__":
    unittest.main()