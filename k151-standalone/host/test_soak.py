import unittest
from pathlib import Path
from observe_soak import Window

SHA='1'*64
def rec(i, sha=SHA, ready=1):
    return f'SIMSTIM_STANDALONE sha256={sha} uptime_s={i} board_ready={ready}'

class SoakTests(unittest.TestCase):
    def test_contiguous(self):
        w=Window(SHA,60)
        for i in range(1,61): self.assertFalse(w.feed(rec(i),120+i))
        self.assertTrue(w.feed(rec(61),181))
    def test_burst_is_not_elapsed(self):
        w=Window(SHA,60)
        for i in range(1,130): self.assertFalse(w.feed(rec(i),0))
    def test_reject(self):
        for line, sec in [(rec(1),2),(rec(3),2),(rec(2,'0'*64),2),(rec(2,ready=0),2),(rec(2),9),('assert failed: x',2),('Loaded app from partition at offset 0x510000',2)]:
            with self.subTest(line=line):
                w=Window(SHA,60); w.feed(rec(1),1)
                with self.assertRaises(ValueError): w.feed(line,sec)

class PowerGateOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parents[1] / 'main' / 'board.c').read_text()

    def test_py32_gate_precedes_all_power_and_panel_bringup(self):
        body = self.source.split('bool board_init(void)', 1)[1].split('\n}', 1)[0]
        calls = ['init_i2c_bus()', 'init_py32_servo_gate()',
                 'init_power_and_io()', 'init_lcd()', 'verify_py32_vm_en_low()']
        positions = [body.index(call) for call in calls]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('if (init_i2c_bus() != ESP_OK) return false;', body)
        self.assertIn('if (!init_py32_servo_gate()) return false;', body)
        self.assertIn('if (!verify_py32_vm_en_low()) return false;', body)

    def test_py32_setup_is_read_modify_write_and_fails_closed(self):
        update = self.source.split('static bool py32_update_bit', 1)[1].split('\n}', 1)[0]
        self.assertIn('value | (1u << bit)', update)
        self.assertIn('value & ~(1u << bit)', update)
        gate = self.source.split('static bool init_py32_servo_gate', 1)[1].split('\n}', 1)[0]
        for operation in ('py32_update_bit(0x05, 0, false)',
                          'py32_update_bit(0x09, 0, false)',
                          'py32_update_bit(0x0b, 0, true)',
                          'py32_update_bit(0x03, 0, true)'):
            self.assertIn(operation, gate)
        self.assertIn('!verify_py32_vm_en_low()) return false;', gate)
        self.assertLess(gate.index('py32_update_bit(0x05'), gate.index('reg_read(s_py32, 0x02'))
        self.assertIn('version unavailable after VM EN low', gate)
        self.assertIn('if (!found)', gate)
