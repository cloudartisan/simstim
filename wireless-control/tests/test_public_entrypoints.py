import ctypes as C, os, socket, sys, tempfile, threading, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import service

def test_listener(key_path):
    original_bind,original_port=service.BIND,service.PORT
    service.BIND,service.PORT="127.0.0.1",0
    try:return service.tls_listener(key_path)
    finally:service.BIND,service.PORT=original_bind,original_port

class SimulatedPSKPeer:
    def __init__(self,key,identity):
        self.L=C.CDLL("libssl.so.3");L=self.L
        L.TLS_client_method.restype=C.c_void_p
        L.SSL_CTX_new.argtypes=[C.c_void_p];L.SSL_CTX_new.restype=C.c_void_p
        L.SSL_CTX_set_cipher_list.argtypes=[C.c_void_p,C.c_char_p];L.SSL_CTX_set_cipher_list.restype=C.c_int
        L.SSL_CTX_ctrl.argtypes=[C.c_void_p,C.c_int,C.c_long,C.c_void_p];L.SSL_CTX_ctrl.restype=C.c_long
        L.SSL_new.argtypes=[C.c_void_p];L.SSL_new.restype=C.c_void_p
        L.SSL_set_fd.argtypes=[C.c_void_p,C.c_int];L.SSL_set_fd.restype=C.c_int
        L.SSL_connect.argtypes=[C.c_void_p];L.SSL_connect.restype=C.c_int
        L.SSL_write.argtypes=[C.c_void_p,C.c_void_p,C.c_int];L.SSL_write.restype=C.c_int
        L.SSL_read.argtypes=[C.c_void_p,C.c_void_p,C.c_int];L.SSL_read.restype=C.c_int
        L.SSL_free.argtypes=[C.c_void_p];L.SSL_CTX_free.argtypes=[C.c_void_p]
        L.SSL_CTX_set_psk_client_callback.argtypes=[C.c_void_p,C.c_void_p]
        self.ctx=L.SSL_CTX_new(L.TLS_client_method()); self.key=key;self.identity=identity.encode()
        self.cb=C.CFUNCTYPE(C.c_uint,C.c_void_p,C.c_char_p,C.POINTER(C.c_char),C.c_uint,C.POINTER(C.c_ubyte),C.c_uint)(
            lambda _ssl,hint,identity,maxid,out,maxkey:self._psk(identity,maxid,out,maxkey))
        L.SSL_CTX_set_psk_client_callback(self.ctx,self.cb)
        assert L.SSL_CTX_ctrl(self.ctx,123,0x0303,None)==1
        assert L.SSL_CTX_ctrl(self.ctx,124,0x0303,None)==1
        assert L.SSL_CTX_set_cipher_list(self.ctx,b"PSK-AES128-GCM-SHA256")==1
    def _psk(self,identity,maxid,out,maxkey):
        if maxid<=len(self.identity) or maxkey<len(self.key):return 0
        C.memmove(identity,self.identity,len(self.identity)+1); C.memmove(out,self.key,len(self.key));return len(self.key)
    def connect(self,sock):
        ssl=self.L.SSL_new(self.ctx); self.ssl=ssl; assert self.L.SSL_set_fd(ssl,sock.fileno())==1
        if self.L.SSL_connect(ssl)!=1:raise RuntimeError("synthetic TLS client handshake failed")
        self.ssl=ssl
    def write(self,data):
        b=C.create_string_buffer(data);return self.L.SSL_write(self.ssl,b,len(data))
    def read(self,n=2048):
        b=C.create_string_buffer(n); size=self.L.SSL_read(self.ssl,b,n)
        if size<=0:raise RuntimeError("synthetic peer TLS read failed")
        return b.raw[:size]
    def close(self):self.L.SSL_free(self.ssl);self.L.SSL_CTX_free(self.ctx)

class PublicEntryPointTests(unittest.TestCase):
    def test_tls_listener_authenticates_fixed_identity_suite_and_ready_banner(self):
        key=bytes(range(32))
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            keypath=Path(d)/"device.psk";keypath.write_bytes(key);keypath.chmod(0o600)
            listener,tls=test_listener(keypath)
            seen=[]
            def accept():
                peer,_=listener.accept(); stream=tls.wrap(peer)
                seen.append(stream.readline().decode().strip())
                stream.write(b"ACK\n");stream.close();peer.close()
            th=threading.Thread(target=accept);th.start()
            peer=SimulatedPSKPeer(key,service.IDENTITY)
            client=socket.create_connection(listener.getsockname());peer.connect(client)
            banner="SIMSTIM_STANDALONE sha256="+"a"*64+" uptime_s=7 board_ready=1\n"
            peer.write(banner.encode()); response=peer.read()
            th.join(2);client.close();peer.close();listener.close()
            self.assertIn("ACK",response.decode());self.assertEqual(seen,[banner.strip()])
    def test_psk_identity_must_be_exact(self):
        key=bytes(range(32))
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            p=Path(d)/"key";p.write_bytes(key);p.chmod(0o600)
            listener,tls=test_listener(p)
            result=[]
            def accept():
                s,_=listener.accept()
                try:tls.wrap(s);result.append("accepted")
                except (service.WirelessError,OSError):result.append("rejected")
                s.close()
            th=threading.Thread(target=accept);th.start()
            peer=SimulatedPSKPeer(key,"wrong-device-identity");sock=socket.create_connection(listener.getsockname())
            with self.assertRaises(RuntimeError):peer.connect(sock)
            th.join(2);sock.close();peer.close();listener.close()
            self.assertEqual(result,["rejected"])
    def test_operator_motor_command_roundtrip_over_real_tls_entrypoint(self):
        key=bytes(range(32))
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            p=Path(d)/"key";p.write_bytes(key);p.chmod(0o600)
            listener,tls=test_listener(p);control=service.Control()
            accepted=threading.Event(); errors=[]
            def run_device_edge():
                s,_=listener.accept();stream=tls.wrap(s);accepted.set()
                try:control.session(stream)
                except Exception as exc:errors.append(exc)
                stream.close();s.close()
            th=threading.Thread(target=run_device_edge);th.start()
            peer=SimulatedPSKPeer(key,service.IDENTITY);sock=socket.create_connection(listener.getsockname());peer.connect(sock)
            peer.write(("SIMSTIM_STANDALONE sha256="+"b"*64+" uptime_s=9 board_ready=1\n").encode())
            self.assertTrue(accepted.wait(2))
            import time
            until=time.monotonic()+2
            while not control.connected and time.monotonic()<until: time.sleep(.005)
            result=[]
            op=threading.Thread(target=lambda:result.append(control._submit("home",timeout=5)));op.start()
            cmd=peer.read().decode();self.assertEqual(cmd,"LOOK_HOME\n",repr(errors))
            peer.write(b"LOOK_MOTOR result=ok action=HOME stage=home_centre reason=complete yaw_raw=458 yaw_deg=0 yaw_sample_age_ms=0 pitch_raw=699 pitch_deg=30 pitch_sample_age_ms=0 sample=last_valid_per_axis\n")
            op.join(3);self.assertTrue(result and result[0]["ok"])
            self.assertIn("yaw_raw=458",result[0]["receipt"])
            sock.close();peer.close();listener.close();th.join(2)
    def test_unsolicited_tap_fetch_is_answered_without_operator(self):
        key=bytes(range(32))
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            p=Path(d)/"key";p.write_bytes(key);p.chmod(0o600)
            listener,tls=test_listener(p);control=service.Control(Path(d)/"state.json")
            result=[];ready=threading.Event()
            def serve_fetch():
                s,_=listener.accept();stream=tls.wrap(s);ready.set()
                result.append(control._readline(stream,5));stream.close();s.close()
            th=threading.Thread(target=serve_fetch);th.start()
            peer=SimulatedPSKPeer(key,service.IDENTITY);sock=socket.create_connection(listener.getsockname());peer.connect(sock)
            self.assertTrue(ready.wait(2));peer.write(b"TAP_FETCH 1a2b3c4d\n")
            response=peer.read().decode();self.assertEqual(response,"TAP_NONE 1a2b3c4d\n")
            peer.write(b"TAP_DONE 1a2b3c4d none\n");th.join(3)
            self.assertEqual(result,["TAP_DONE none"])
            sock.close();peer.close();listener.close()
    def test_event_is_latest_only_and_timestamped_monotonically(self):
        control=service.Control()
        self.assertIsNone(control._readline(type("S",(),{"readline":lambda self,**kw:b"TAP_EVENT 41 993\n"})()))
        event=control.last_event
        self.assertEqual((event["count"],event["uptime_ms"]),(41,993))
        self.assertGreaterEqual(event["received_monotonic"],0)

if __name__=="__main__":unittest.main()
