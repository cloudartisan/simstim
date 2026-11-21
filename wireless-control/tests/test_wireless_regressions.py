import json, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import service

class Partial:
    def __init__(self): self.chunks=[b'first\nsec',b'ond\n']
    def settimeout(self,t): pass
    def recv(self,n): return self.chunks.pop(0)

class Peer:
    def __init__(self): self.lines=[b'TAP_FETCH 1234abcd\n']; self.writes=[]
    def readline(self,*args,**kwargs): return self.lines.pop(0)
    def write(self,b):
        self.writes.append(b)
        if b.startswith(b'TAP_AUDIO '): self.lines.append(b'TAP_HEADER 1234abcd\n')
        elif len(b)==8: self.lines.append(b'TAP_READY 1234abcd\n')
        elif len(b)==320: self.lines.append(b'TAP_DONE 1234abcd played\n')
    def flush(self): pass

class Regression(unittest.TestCase):
    def test_release_elf_path_uses_immutable_override_and_standalone_default(self):
        default_release = service.ROOT / "k151-standalone/build/k151_standalone.elf"
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(service.release_elf_path(), default_release)
        with patch.dict("os.environ", {"SIMSTIM_RELEASE_ELF_PATH": "/immutable/releases/full-pin/k151_standalone.elf"}):
            self.assertEqual(service.release_elf_path(), Path("/immutable/releases/full-pin/k151_standalone.elf"))

    def test_observe_uses_configured_release_path_and_full_pin(self):
        configured = Path("/immutable/releases/full-pin/k151_standalone.elf")
        control = service.Control()
        control.elf_path = configured
        control.expected = "a" * 64
        with patch.object(service.look_room, "load_object_detector", return_value=object()):
            with patch.object(service.look_room, "load_kokoro_voice", return_value=object()):
                with patch.object(service.look_room, "survey_and_speak", return_value={"ok": True}) as observe:
                    result = control.execute(object(), "observe")
        self.assertTrue(result["ok"])
        self.assertEqual(observe.call_args.args[2:4], (configured, "a" * 64))

    def test_lines_across_tls_records(self):
        s=service.TLSStream(Partial()); self.assertEqual(s.readline(),b'first\n');self.assertEqual(s.readline(),b'second\n')
    def test_offline_action_cannot_run_after_reconnect(self):
        c=service.Control()
        with self.assertRaises(service.WirelessError): c._submit('left')
        self.assertTrue(c.pending.empty())
    def test_authenticated_ble_diagnostic_command_parses_legacy_and_extended_lines(self):
        c=service.Control()
        class DiagnosticPeer:
            def __init__(self, lines): self.lines=list(lines); self.writes=[]
            def write(self,value): self.writes.append(value)
            def readline(self, **_kwargs): return self.lines.pop(0).encode("ascii")+b"\n"
        legacy='SIMSTIM_BLE_DIAG generation=7 cause=gap gap=19 rc=0 tx=4096 rx=2048 fragment_reject=2'
        extended='SIMSTIM_BLE_DIAG generation=8 cause=write gap=0 rc=13 tx=64 rx=24 fragment_reject=0 write_acks=123 write_seq=65535 write_rc=-7'
        peer=DiagnosticPeer([legacy,extended])
        result=c.execute(peer,'ble-diagnostics')
        self.assertEqual(peer.writes,[b'BLE_DIAGNOSTICS\n'])
        self.assertEqual(result,{'generation':7,'first_cause':'gap','gap_reason':19,
                                 'low_level_return':0,'tx_bytes':4096,'rx_bytes':2048,
                                 'fragment_rejections':2})
        result=c.execute(peer,'ble-diagnostics')
        self.assertEqual(result,{'generation':8,'first_cause':'write','gap_reason':0,
                                 'low_level_return':13,'tx_bytes':64,'rx_bytes':24,
                                 'fragment_rejections':0,'write_acks':123,
                                 'write_seq':65535,'write_rc':-7})
        c.connected=False
        self.assertEqual(c.status()['last_ble_diagnostic'],result)
        with self.assertRaises(service.WirelessError): c._submit('ble-diagnostics')

    def test_ble_diagnostic_rejects_suffixes_and_out_of_range_counters(self):
        base='SIMSTIM_BLE_DIAG generation=8 cause=write gap=0 rc=13 tx=64 rx=24 fragment_reject=0'
        for line in (base+' trailing', base+' write_acks=1 write_seq=65536 write_rc=0',
                     base+' write_acks=4294967296 write_seq=0 write_rc=0',
                     base+' write_acks=1 write_seq=0 write_rc=0 extra=1', base+'x'):
            with self.subTest(line=line):
                peer=type('Peer',(),{'write':lambda self,_data:None,
                                     'readline':lambda self,**_kwargs:line.encode()+b'\n'})()
                with self.assertRaises(service.WirelessError): service.Control().execute(peer,'ble-diagnostics')
    def test_single_positive_tap_claim(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';p.write_text(json.dumps({'schema':1,'items':{'brief':{'summary':'Ready.','status':'pending','created_at':time.time(),'reviewed':True}}}));peer=Peer()
            with patch.object(service.look_room,'load_kokoro_voice',return_value=object()):
                with patch.object(service.look_room,'synthesize_observation',return_value=bytes(320)):
                    self.assertEqual(service.Control(p)._readline(peer),'TAP_DONE played')
            self.assertEqual(peer.writes[0],b'TAP_AUDIO 1234abcd\n');self.assertEqual(json.loads(p.read_text())['items']['brief']['status'],'retrieved')
