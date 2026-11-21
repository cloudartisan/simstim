import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HostDeploymentTests(unittest.TestCase):
    def test_dual_radio_unit_runs_real_daemon_from_isolated_runtime(self):
        unit = (ROOT / "deploy/simstim-wireless-control@.service").read_text()
        self.assertIn("ble-fallback/host/dual_transport_daemon.py", unit)
        self.assertIn("host-venv/bin/python", unit)
        self.assertIn("--adapter ${SIMSTIM_BLUEZ_ADAPTER}", unit)
        self.assertIn("--key ${SIMSTIM_KEY_PATH}", unit)
        self.assertIn("--operator-socket ${SIMSTIM_OPERATOR_SOCKET}", unit)
        self.assertIn("--state ${SIMSTIM_STATE_PATH}", unit)
        self.assertIn("bluetooth.service", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertNotIn("/home/", unit)
        self.assertNotIn("ExecStartPre=", unit)
        self.assertNotIn("wireless-control/service.py", unit)

    def test_real_dual_daemon_imports_and_cli_help(self):
        daemon = ROOT / "ble-fallback/host/dual_transport_daemon.py"
        result = subprocess.run(
            [sys.executable, str(daemon), "--help"], cwd=ROOT,
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--adapter", result.stdout)
        self.assertIn("--key", result.stdout)
        self.assertIn("--operator-socket", result.stdout)
        self.assertIn("--state", result.stdout)
        self.assertIn("--no-wifi-listener", result.stdout)

    def test_installer_only_installs_and_does_not_activate_service(self):
        script = ROOT / "deploy/install-user-service.sh"
        result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = script.read_text()
        self.assertIn("install -m 0644", source)
        self.assertNotIn("enable --now", source.split("printf '%s\\n'", 1)[0])
        self.assertNotIn("daemon-reload", source.split("printf '%s\\n'", 1)[0])
        with tempfile.TemporaryDirectory() as home:
            env = os.environ.copy()
            env["HOME"] = home
            env["XDG_CONFIG_HOME"] = str(Path(home) / ".config")
            installed = subprocess.run(
                ["sh", str(script)], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            target = Path(home) / ".config/systemd/user/simstim-wireless-control@.service"
            self.assertEqual(target.read_bytes(), (ROOT / "deploy/simstim-wireless-control@.service").read_bytes())

    def test_host_guide_documents_pinned_runtime_and_real_daemon(self):
        guide = (ROOT / "docs/HOST.md").read_text()
        for phrase in ("dbus-next==0.2.3", "pyserial==3.5", "dual_transport_daemon.py", "TLS-PSK", "wire parity", "physical device", "does not", "host-venv"):
            self.assertIn(phrase, guide)


if __name__ == "__main__":
    unittest.main()
