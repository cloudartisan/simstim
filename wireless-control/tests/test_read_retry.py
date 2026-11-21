import unittest,sys
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cli import request

class Retry(unittest.TestCase):
    def test_safe_read_recovers(self):
        f=Mock(side_effect=[{'ok':False,'error':'no verified device'},{'ok':True,'result':'PROX OK 1 0'}]); out=request('x','prox',once=f,sleep=lambda _:None);self.assertEqual(out['attempts'],2);self.assertTrue(out['ok'])
    def test_actions_not_repeated(self):
        for cmd in ('left','home','probe','speak','tap-check','verify-cue online','stage One.','frame','survey','observe'):
            f=Mock(side_effect=lambda _path, command, _timeout: {'ok':False,'error':'peer closed'} if command != 'status' else {'ok':True,'result':{'transport':'wifi'}})
            self.assertFalse(request('x',cmd,once=f)['ok'])
            called=[call.args[1] for call in f.call_args_list]
            self.assertEqual(called.count(cmd),1)
            self.assertNotIn(cmd, called[called.index(cmd)+1:])
    def test_bounded_outage(self):
        t=[0.]; f=Mock(return_value={'ok':False,'error':'no verified device'}); out=request('x','prox',once=f,clock=lambda:t[0],sleep=lambda d:t.__setitem__(0,t[0]+d));self.assertLessEqual(t[0],40);self.assertLessEqual(f.call_count,20);self.assertFalse(out['ok'])
    def test_protocol_failure_not_retried(self):
        f=Mock(return_value={'ok':False,'error':'unexpected device ELF'}); self.assertFalse(request('x','touch',once=f)['ok']);self.assertEqual(f.call_count,1)
