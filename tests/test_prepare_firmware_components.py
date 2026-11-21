import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare-firmware-components.py"
spec = importlib.util.spec_from_file_location("prepare_firmware_components", SCRIPT)
assert spec is not None and spec.loader is not None
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def run_cli(argv):
    import sys
    with patch.object(sys, "argv", [str(SCRIPT), *argv]):
        return prepare.main()


class PrepareFirmwareComponentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "managed_components"
        self.source.mkdir()
        self.destination = self.root / "mirror"
        self.expected = hashlib.sha256(b"original source\n").hexdigest()
        self.pins = {name: self.expected for name in prepare.COMPONENTS}
        for name in prepare.COMPONENTS:
            component = self.source / f"espressif__{name}"
            component.mkdir()
            (component / ".component_hash").write_text(self.expected + "\n")
            (component / "source.c").write_text("original source\n")

    def run_prepare(self, validator):
        with patch.object(prepare, "pinned_hashes", return_value=self.pins), \
             patch.object(prepare, "_native_hash_validator", return_value=validator):
            return run_cli(["--source", str(self.source), "--destination", str(self.destination)])

    def test_content_verification_detects_modified_file_with_unchanged_marker(self):
        component = self.source / f"espressif__{prepare.COMPONENTS[0]}"
        (component / "source.c").write_text("tampered source\n")

        def native_verify(path, expected):
            content_hash = hashlib.sha256((Path(path) / "source.c").read_bytes()).hexdigest()
            if content_hash != expected:
                raise ValueError("component directory hash differs from lock")

        self.assertEqual(self.run_prepare(native_verify), 2)
        self.assertEqual((component / ".component_hash").read_text().strip(), self.expected)
        self.assertFalse(self.destination.exists())

    def test_success_checks_sources_and_copied_content(self):
        calls = []

        def native_verify(path, expected):
            path = Path(path)
            calls.append(path)
            actual = hashlib.sha256((path / "source.c").read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError("component directory hash differs from lock")

        self.assertEqual(self.run_prepare(native_verify), 0)
        self.assertEqual(len(calls), len(prepare.COMPONENTS) * 2)
        self.assertEqual((self.destination / f"espressif__{prepare.COMPONENTS[0]}" / "source.c").read_text(), "original source\n")

    def test_rejects_symlink_escaping_component(self):
        component = self.source / f"espressif__{prepare.COMPONENTS[0]}"
        outside = self.root / "outside.c"
        outside.write_text("outside\n")
        (component / "escape.c").symlink_to(Path("../../outside.c"))
        called = []
        self.assertEqual(self.run_prepare(lambda *args: called.append(args)), 2)
        self.assertEqual(called, [])
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
