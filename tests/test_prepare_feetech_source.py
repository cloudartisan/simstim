"""The Feetech preparer rejects changed mirror bytes without network access."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREPARER = ROOT / "scripts" / "prepare-feetech-source.py"
LOCK = ROOT / "k151-standalone" / "feetech-source.lock"


class PrepareFeetechSourceTests(unittest.TestCase):
    def test_lock_uses_immutable_upstream_url_and_full_content_hash(self):
        lock = json.loads(LOCK.read_text(encoding="utf-8"))
        self.assertRegex(lock["commit"], r"^[0-9a-f]{40}$")
        self.assertIn(lock["commit"], lock["url"])
        self.assertRegex(lock["sha256"], r"^[0-9a-f]{64}$")

    def test_modified_mirror_is_rejected_even_with_a_neighbor_marker(self):
        with tempfile.TemporaryDirectory(prefix="simstim-feetech-") as tmp:
            root = Path(tmp)
            mirror = root / "mirror"
            mirror.mkdir()
            (mirror / "feetech_scs.cpp").write_text("modified upstream content\n")
            (mirror / ".sha256").write_text("the original checksum is unchanged\n")
            output = root / "prepared" / "write_pos.inc"
            result = subprocess.run(
                ["python3", str(PREPARER), "--destination", str(output),
                 "--mirror", str(mirror), "--offline"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertFalse(output.exists())

    def test_offline_mode_requires_explicit_mirror(self):
        with tempfile.TemporaryDirectory(prefix="simstim-feetech-") as tmp:
            result = subprocess.run(
                ["python3", str(PREPARER), "--destination", str(Path(tmp) / "out.inc"), "--offline"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("offline mode requires --mirror", result.stderr)


if __name__ == "__main__":
    unittest.main()
