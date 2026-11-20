import unittest

from parse_progress import parse_progress


SHA = "0123456789abcdef" * 4


class ParseProgressTests(unittest.TestCase):
    def test_ready_record(self):
        self.assertEqual(
            parse_progress(f"SIMSTIM_STANDALONE sha256={SHA} uptime_s=9 board_ready=1\n"),
            {"sha256": SHA, "uptime_s": 9, "board_ready": True},
        )

    def test_not_ready_at_boot(self):
        self.assertEqual(
            parse_progress(f"SIMSTIM_STANDALONE sha256={SHA} uptime_s=1 board_ready=0"),
            {"sha256": SHA, "uptime_s": 1, "board_ready": False},
        )

    def test_rejects_wrong_prefix_or_field_order(self):
        for line in (
            f"OTHER sha256={SHA} uptime_s=9 board_ready=1",
            f"SIMSTIM_STANDALONE uptime_s=9 sha256={SHA} board_ready=1",
        ):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_progress(line)

    def test_rejects_non_sha256_or_invalid_flags(self):
        for line in (
            "SIMSTIM_STANDALONE sha256=abc uptime_s=9 board_ready=1",
            f"SIMSTIM_STANDALONE sha256={SHA.upper()} uptime_s=9 board_ready=1",
            f"SIMSTIM_STANDALONE sha256={SHA} uptime_s=09 board_ready=1",
            f"SIMSTIM_STANDALONE sha256={SHA} uptime_s=9 board_ready=true",
            f"SIMSTIM_STANDALONE sha256={SHA} uptime_s=9 board_ready=1 extra",
        ):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_progress(line)


if __name__ == "__main__":
    unittest.main()
