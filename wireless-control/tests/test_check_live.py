import hashlib, importlib.util, sys, tempfile, types, unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "check_live.py"
spec = importlib.util.spec_from_file_location("check_live", SCRIPT)
check_live = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = check_live
spec.loader.exec_module(check_live)

class FakePort:
    def __init__(self, *args, **kwargs):
        self.is_open = False; self.port = None; self.commands = []; self.lines = [b"SIMSTIM_STANDALONE sha256=" + b"0"*64 + b" uptime_s=1 board_ready=1\n"]
    def __enter__(self): self.is_open=True; return self
    def __exit__(self, *args): self.close()
    def close(self): self.is_open=False
    def readline(self): return self.lines.pop(0) if self.lines else b""
    def write(self, data): self.commands.append(data); return len(data)
    def flush(self): pass

class RunnerOfflineTests(unittest.TestCase):
    def test_requires_full_elf_and_matching_digest(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"board.elf"; p.write_bytes(b"\x7fELFcomplete-image")
            digest=hashlib.sha256(p.read_bytes()).hexdigest()
            self.assertEqual(check_live.verify_elf(p,digest),digest)
            with self.assertRaises(check_live.AcceptanceError): check_live.verify_elf(p,digest[:12])
            p.write_bytes(b"not-elf")
            with self.assertRaises(check_live.AcceptanceError): check_live.verify_elf(p,digest)

    def test_motor_receipt_requires_action_success_and_both_axes(self):
        line="LOOK_MOTOR result=ok action=LEFT stage=complete yaw_raw=400 yaw_deg=-10 yaw_sample_age_ms=0 pitch_raw=699 pitch_deg=30 pitch_sample_age_ms=0 sample=last_valid_per_axis"
        self.assertEqual(check_live.parse_motor(line,"left"),(400,-10,699,30))
        with self.assertRaises(check_live.AcceptanceError): check_live.parse_motor(line.replace("result=ok","result=failed"),"left")
        with self.assertRaises(check_live.AcceptanceError): check_live.parse_motor(line,"right")

    def test_cleanup_closes_port_on_identity_mismatch_without_commands(self):
        fake=FakePort()
        fake.lines=[b"SIMSTIM_STANDALONE sha256=" + b"f"*64 + b" uptime_s=1 board_ready=1\n"]
        fake_serial=types.SimpleNamespace(Serial=lambda **kw: fake)
        args=types.SimpleNamespace(elf=Path("unused"),elf_sha256="0"*64,bearer="usb",port="/dev/fake",ready_timeout=0.1)
        with patch.object(check_live,"verify_elf",return_value="0"*64), patch("look_room.load_object_detector",return_value=object()), patch.dict(sys.modules,{"serial":fake_serial}):
            with self.assertRaises(check_live.AcceptanceError): check_live.run(args)
        self.assertFalse(fake.is_open)
        self.assertEqual(fake.commands,[])

    def test_wifi_is_explicitly_not_fallback(self):
        args=types.SimpleNamespace(elf=Path("unused"),elf_sha256="0"*64,bearer="wifi",port=None,ready_timeout=0.1)
        with patch.object(check_live,"verify_elf",return_value="0"*64):
            with self.assertRaises(check_live.AcceptanceError): check_live.run(args)

if __name__ == "__main__": unittest.main()
