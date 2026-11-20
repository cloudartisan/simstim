import sys
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service
import cli
import trial_ble_session
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools import look_room
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ble-fallback' / 'host'))
import dual_transport_daemon


class BulkDeadlineTests(unittest.TestCase):
    def test_explicit_budget_hierarchy_supports_measured_ble_rate_and_wifi_restore(self):
        self.assertEqual(service.FRAME_DEADLINE_S, look_room.BLE_FRAME_HARD_S)
        self.assertEqual(service.SURVEY_DEADLINE_S, look_room.BLE_SURVEY_HARD_S)
        self.assertGreaterEqual(service.SURVEY_DEADLINE_S, 460800 * 4 / 3 / 2000)
        self.assertGreater(service.WIFI_SUSPEND_S, service.OPERATION_TIMEOUTS['observe'])
        self.assertEqual(cli.COMMAND_TIMEOUTS['frame'], service.OPERATION_TIMEOUTS['frame'] + 15)
        self.assertEqual(cli.COMMAND_TIMEOUTS['survey'], service.OPERATION_TIMEOUTS['survey'] + 15)

    def test_cli_uses_bulk_wait_and_never_replays_timed_out_bulk(self):
        seen = []
        def once(_path, command, timeout):
            seen.append((command, timeout))
            if command == 'status': return {'ok': True, 'result': {'transport': 'ble'}}
            return {'ok': False, 'error': 'device command timed out'}
        out = cli.request('/operator.sock', 'survey', once=once)
        self.assertFalse(out['ok'])
        self.assertEqual(seen, [('status', 15), ('survey', 630)])

    def test_wifi_bulk_cli_wait_preserves_default(self):
        seen = []
        def once(_path, command, timeout):
            seen.append((command, timeout))
            if command == 'status': return {'ok': True, 'result': {'transport': 'wifi'}}
            return {'ok': False, 'error': 'timed out'}
        cli.request('/operator.sock', 'frame', once=once)
        self.assertEqual(seen, [('status', 15), ('frame', 45)])

    def test_daemon_applies_extended_operation_timeout_only_to_ble_owner(self):
        control = dual_transport_daemon.SharedControl()
        with patch.object(service.Control, '_submit', return_value='ok') as submit:
            control._owner = 'ble'
            control._submit('survey')
            self.assertEqual(submit.call_args.args, ('survey', service.OPERATION_TIMEOUTS['survey']))
            control._owner = 'wifi'
            control._submit('survey')
            self.assertEqual(submit.call_args.args, ('survey', 35))

    def test_trial_suspend_budget_covers_planned_waits_and_caps_to_daemon_limit(self):
        self.assertEqual(trial_ble_session.wifi_suspend_budget(['observe']), 945)
        self.assertEqual(trial_ble_session.wifi_suspend_budget(['frame', 'survey']), 1055)
        self.assertEqual(trial_ble_session.wifi_suspend_budget(['observe'] * 8), 3600)

    def test_only_safe_reads_are_replayed(self):
        calls = []
        def once(_path, command, timeout):
            calls.append((command, timeout))
            return {'ok': False, 'error': 'no verified device'} if len(calls) == 1 else {'ok': True}
        cli.request('/operator.sock', 'prox', once=once, sleep=lambda _: None)
        self.assertEqual([c[0] for c in calls], ['prox', 'prox'])
        calls.clear()
        cli.request('/operator.sock', 'frame', once=once)
        self.assertEqual([c[0] for c in calls], ['status', 'frame'])

    def test_survey_absolute_deadline_with_fake_clock(self):
        now = [0.0]
        def paced_lines():
            for line in ['LOOK_SURVEY_BEGIN', 'LOOK_GLANCE 0']:
                now[0] += 2.0
                yield line
            # Simulate a stream that makes no further progress before its deadline.
            now[0] += 20.0
            yield 'LOOK_FRAME_BEGIN 320 240 RGB565 153600'
        with self.assertRaisesRegex(look_room.LookError, 'hard deadline'):
            look_room.read_survey(paced_lines(), detector=object(),
                                  collection_deadline=10.0, clock=lambda: now[0])

    def test_tls_write_advances_in_chunks_and_obeys_absolute_deadline(self):
        now = [0.0]
        class PacedSocket:
            def __init__(self): self.sent = bytearray(); self.timeout = None
            def settimeout(self, value): self.timeout = value
            def send(self, data):
                n = min(3, len(data)); self.sent.extend(data[:n]); now[0] += .75; return n
        sock = PacedSocket()
        stream = service.TLSStream(sock)
        stream.command_deadline = 2.0
        with patch.object(service.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(TimeoutError, 'deadline'):
                stream.write(b'abcdefghij')
        self.assertEqual(bytes(sock.sent), b'abcdefghi')
        self.assertLessEqual(sock.timeout, 2.0)

    def test_expired_observe_sends_neither_survey_nor_speech(self):
        class Peer:
            def __init__(self): self.writes = []
            def write(self, data): self.writes.append(bytes(data)); return len(data)
            def flush(self): pass
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / 'board.elf'
            elf.write_bytes(b'\x7fELFsynthetic')
            digest = hashlib.sha256(elf.read_bytes()).hexdigest()
            peer = Peer()
            with self.assertRaisesRegex(RuntimeError, 'expired'):
                look_room.survey_and_speak(peer, object(), elf, digest,
                                           detector=object(),
                                           before_survey=lambda: (_ for _ in ()).throw(RuntimeError('expired')))
            self.assertEqual(peer.writes, [])

            peer = Peer()
            full_result = {'ok': True, 'frames': 3, 'frame_bytes': 460800,
                           'views': [{'glance': 0}, {'glance': 1}, {'glance': 2}], 'text': 'synthetic'}
            with patch.object(look_room, 'read_survey', return_value=full_result), \
                 patch.object(look_room, 'synthesize_observation', return_value=b'\x00\x00'):
                with self.assertRaisesRegex(RuntimeError, 'expired'):
                    look_room.survey_and_speak(peer, object(), elf, digest,
                                               detector=object(), before_speech=lambda: (_ for _ in ()).throw(RuntimeError('expired')))
            self.assertEqual(peer.writes, [b'LOOK_SURVEY\n'])

    def test_cancelled_and_stale_items_are_rejected_by_session_loop(self):
        import threading
        class SessionStream:
            def __init__(self, digest): self.calls = 0; self.digest = digest; self.writes = []
            def readline(self, timeout=1):
                self.calls += 1
                if self.calls == 1:
                    return (f'SIMSTIM_STANDALONE sha256={self.digest} uptime_s=1 board_ready=1\n').encode()
                if self.calls == 2: raise TimeoutError()
                raise OSError('end synthetic session')
            def write(self, data): self.writes.append(data); return len(data)
        for replacement in (False, True):
            c = service.Control()
            old, active = SessionStream('a'*64), SessionStream('b'*64)
            if replacement:
                queued = {'session': old, 'deadline': 99, 'command': 'home',
                          'done': threading.Event(), 'result': None}
                c.pending.put_nowait(queued)
            else:
                c.current, c.connected = active, True
                with self.assertRaisesRegex(service.WirelessError, 'not replayed'):
                    c._submit('home', timeout=0.001)
                queued = c.pending.get_nowait()
                c.pending.task_done()
                c.pending.put_nowait(queued)
            with patch.object(service.time, 'monotonic', return_value=1.0):
                with self.assertRaises(OSError): c.session(active)
            self.assertIsInstance(queued['result'], service.WirelessError)
            self.assertEqual(active.writes, [])
            self.assertEqual(c.pending.unfinished_tasks, 0)


if __name__ == '__main__':
    unittest.main()
