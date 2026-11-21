"""Isolated build candidates preserve prior artifacts and pinned-source inputs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "k151-standalone"
PREPARER = ROOT / "scripts" / "prepare-feetech-source.py"


class BuildConfigIsolationTest(unittest.TestCase):
    def exercise_sequence(self, modes):
        with tempfile.TemporaryDirectory(prefix="simstim-build-isolation-") as temp:
            root = Path(temp)
            checkout = root / "source"
            source = checkout / "k151-standalone"
            (source / "main" / "vendor" / "feetech").mkdir(parents=True)
            (checkout / "scripts").mkdir()
            shutil.copy2(PROJECT / "build.sh", source / "build.sh")
            shutil.copy2(PROJECT / "sdkconfig.defaults", source / "sdkconfig.defaults")
            shutil.copy2(PROJECT / "sdkconfig.ble.defaults", source / "sdkconfig.ble.defaults")
            shutil.copy2(PROJECT / "main" / "vendor" / "feetech" / "LICENSE",
                         source / "main" / "vendor" / "feetech" / "LICENSE")
            shutil.copy2(PREPARER, checkout / "scripts" / "prepare-feetech-source.py")

            # Use a tiny, locally pinned source fixture to exercise the real
            # preparer without network access or bundling upstream source.
            commit = "0123456789abcdef0123456789abcdef01234567"
            upstream = (
                "int FeetechScs::WritePos(uint8_t id, uint16_t position, "
                "uint16_t time_ms, uint16_t speed) {\n"
                "  if (id == 0) { return -1; }\n"
                "  return position + time_ms + speed;\n}\n"
            ).encode()
            mirror = root / "mirror"
            mirror.mkdir()
            (mirror / "feetech_scs.cpp").write_bytes(upstream)
            lock = {
                "url": f"https://example.invalid/{commit}/feetech_scs.cpp",
                "commit": commit,
                "sha256": sha256(upstream).hexdigest(),
                "filename": "feetech_scs.cpp",
            }
            (source / "feetech-source.lock").write_text(json.dumps(lock), encoding="utf-8")

            build_root = root / "artifacts"
            build_root.mkdir()
            preserved = {
                "sdkconfig": b"historic sdkconfig\n",
                "sdkconfig.old": b"historic old sdkconfig\n",
                "simstim.elf": b"historic output\0",
            }
            for name, content in preserved.items():
                (build_root / name).write_bytes(content)

            idf = root / "fake-idf"
            (idf / "tools").mkdir(parents=True)
            (idf / "export.sh").write_text("# simulated IDF export\n", encoding="utf-8")
            call_log = root / "calls.jsonl"
            fake_idf = idf / "tools" / "idf.py"
            fake_idf.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "if sys.argv[1:] == ['--version']:\n"
                " print('ESP-IDF v5.5.6'); raise SystemExit(0)\n"
                "args = sys.argv[1:]\n"
                "def value(prefix): return next(a[len(prefix):] for a in args if a.startswith(prefix))\n"
                "config = pathlib.Path(value('SDKCONFIG='))\n"
                "source = pathlib.Path(value('SIMSTIM_FEETECH_SOURCE_DIR='))\n"
                "prepared = (source / 'write_pos.inc').read_text()\n"
                "assert 'int FeetechScs::WritePos' in prepared\n"
                "assert '-B' in args\n"
                "build = pathlib.Path(args[args.index('-B') + 1])\n"
                "assert config == build / 'sdkconfig'\n"
                "defaults = os.environ['SDKCONFIG_DEFAULTS'].split(';')\n"
                "config.write_text('\\n'.join(pathlib.Path(p).read_text() for p in defaults))\n"
                "with open(os.environ['SIMSTIM_CALL_LOG'], 'a') as f:\n"
                " f.write(json.dumps({'args': args, 'build': str(build), 'source': str(source), 'config': str(config)}) + '\\n')\n",
                encoding="utf-8",
            )
            fake_idf.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "IDF_PATH": str(idf),
                "IDF_TOOLS_PATH": str(root / "tools"),
                "SIMSTIM_BUILD_DIR": str(build_root),
                "SIMSTIM_CALL_LOG": str(call_log),
                "SIMSTIM_FEETECH_MIRROR": str(mirror),
                "SIMSTIM_OFFLINE": "1",
            })
            env.pop("IDF_PYTHON_ENV_PATH", None)

            for mode in modes:
                subprocess.run([str(source / "build.sh"), mode], env=env, check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), len(modes))
            self.assertEqual(len({item["build"] for item in calls}), len(modes))
            self.assertEqual(len({item["config"] for item in calls}), len(modes))
            for item, mode in zip(calls, modes):
                candidate = Path(item["build"])
                args = item["args"]
                self.assertEqual(candidate.parent, build_root)
                self.assertTrue(candidate.name.startswith(f"{mode}-candidate."))
                self.assertIn(f"SDKCONFIG={candidate / 'sdkconfig'}", args)
                self.assertIn(str(candidate), args[args.index("-B") + 1:args.index("-D")])
                self.assertTrue((Path(item["source"]) / "write_pos.inc").is_file())
                config = Path(item["config"]).read_text(encoding="utf-8")
                self.assertIn('CONFIG_IDF_TARGET="esp32s3"', config)
                self.assertEqual("CONFIG_BT_NIMBLE_ENABLED=y" in config, mode == "dual")
            for name, content in preserved.items():
                self.assertEqual((build_root / name).read_bytes(), content)

    def test_wifi_then_dual_uses_fresh_configs_with_offline_feetech_fixture(self):
        self.exercise_sequence(("wifi", "dual"))

    def test_dual_then_wifi_uses_fresh_configs_with_offline_feetech_fixture(self):
        self.exercise_sequence(("dual", "wifi"))


if __name__ == "__main__":
    unittest.main()
