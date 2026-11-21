#!/usr/bin/env python3
"""Private LAN TLS-PSK bridge for the SIMSTIM K151 line/byte protocol.

Portable host gateway. Bind, peer identity, key, and state paths are configurable. No TLS downgrade, USB, camera
persistence, raw sensor history, or ambient automation is implemented here.
"""
from __future__ import annotations
import ctypes as C
import fcntl, hashlib, json, os, queue, re, select, socket, stat, struct, sys, threading, time, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A configured immutable release path is shared by startup and observe. If it is
# absent, use the standalone build's documented default output path.
def release_elf_path() -> Path:
    configured = os.environ.get("SIMSTIM_RELEASE_ELF_PATH")
    return Path(configured).expanduser() if configured else ROOT / "k151-standalone/build/k151_standalone.elf"

sys.path.insert(0, str(ROOT / "tools"))
import look_room
import tap_refresh

BIND = os.environ.get("SIMSTIM_BIND", "127.0.0.1")
PORT = int(os.environ.get("SIMSTIM_PORT", "18767"))
IDENTITY = os.environ.get("SIMSTIM_PEER_IDENTITY", "simstim-device")
KEYFILE = Path(os.environ.get("SIMSTIM_KEY_PATH", str(Path.home() / ".local/state/simstim-wireless/device.psk"))).expanduser()
OPSOCK = Path(os.environ.get("SIMSTIM_OPERATOR_SOCKET", str(Path.home() / ".local/state/simstim-wireless/operator.sock"))).expanduser()
BANNER = re.compile(r"SIMSTIM_STANDALONE sha256=([0-9a-f]{64}) uptime_s=(\d+) board_ready=1")
LINE_MAX = 20000
# BLE uses acknowledged base64 line writes. At the observed ~3.1 kB/s wire
# rate, a 460800-byte RGB565 survey is ~200 s before camera/motor overhead.
# These are hard wall-clock envelopes (not per-line timeouts).
FRAME_DEADLINE_S = look_room.BLE_FRAME_HARD_S
SURVEY_DEADLINE_S = look_room.BLE_SURVEY_HARD_S
OBSERVE_DEADLINE_S = SURVEY_DEADLINE_S + 220  # bounded PCM transfer + local inference margin
FRAME_STALL_S = 30
SURVEY_STALL_S = 30
OPERATOR_MARGIN_S = 15
WIFI_SUSPEND_S = 900
OPERATION_TIMEOUTS = {"frame": FRAME_DEADLINE_S + OPERATOR_MARGIN_S,
                      "survey": SURVEY_DEADLINE_S + OPERATOR_MARGIN_S,
                      "observe": OBSERVE_DEADLINE_S + OPERATOR_MARGIN_S}
COMMANDS = {"status": "STATUS", "touch": "TOUCH_STATUS", "prox": "PROX_STATUS", "probe": "LOOK_PROBE", "home": "LOOK_HOME", "left": "LOOK_LEFT", "right": "LOOK_RIGHT", "up": "LOOK_UP", "down": "LOOK_DOWN", "frame": "LOOK_FRAME", "survey": "LOOK_SURVEY", "ble-diagnostics": "BLE_DIAGNOSTICS"}
BLE_DIAG_LEGACY = re.compile(r"SIMSTIM_BLE_DIAG generation=(\d{1,10}) cause=(gap|tls|return|fragment|tx|rx|write|unknown) gap=(-?\d{1,11}) rc=(-?\d{1,11}) tx=(\d{1,10}) rx=(\d{1,10}) fragment_reject=(\d{1,10})")
BLE_DIAG_EXTENDED = re.compile(r"SIMSTIM_BLE_DIAG generation=(\d{1,10}) cause=(gap|tls|return|fragment|tx|rx|write|unknown) gap=(-?\d{1,11}) rc=(-?\d{1,11}) tx=(\d{1,10}) rx=(\d{1,10}) fragment_reject=(\d{1,10}) write_acks=(\d{1,10}) write_seq=(\d{1,5}) write_rc=(-?\d{1,11})")


def _parse_ble_diagnostic(line):
    """Parse exactly the legacy or extended bounded firmware diagnostic record."""
    match = BLE_DIAG_EXTENDED.fullmatch(line)
    extended = match is not None
    if match is None:
        match = BLE_DIAG_LEGACY.fullmatch(line)
    if match is None:
        raise WirelessError("malformed BLE diagnostic line")
    groups = match.groups()
    generation, cause, gap, rc, tx, rx, rejects = groups[:7]
    generation, gap, rc, tx, rx, rejects = map(int, (generation, gap, rc, tx, rx, rejects))
    bounds = ((generation, 0, 0xffffffff), (gap, -0x80000000, 0x7fffffff),
              (rc, -0x80000000, 0x7fffffff), (tx, 0, 0xffffffff),
              (rx, 0, 0xffffffff), (rejects, 0, 0xffffffff))
    if any(not low <= value <= high for value, low, high in bounds):
        raise WirelessError("BLE diagnostic value out of range")
    keys = ("generation", "first_cause", "gap_reason", "low_level_return", "tx_bytes", "rx_bytes", "fragment_rejections")
    values = [generation, cause, gap, rc, tx, rx, rejects]
    if extended:
        acks, sequence, write_rc = map(int, groups[7:])
        if acks > 0xffffffff or sequence > 0xffff or not -0x80000000 <= write_rc <= 0x7fffffff:
            raise WirelessError("BLE diagnostic value out of range")
        keys += ("write_acks", "write_seq", "write_rc")
        values.extend((acks, sequence, write_rc))
    return dict(zip(keys, values))

class WirelessError(Exception): pass

class OpenSSLPSK:
    def __init__(self, key, identity):
        import ssl
        self.ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version=self.ctx.maximum_version=ssl.TLSVersion.TLSv1_2
        self.ctx.options |= ssl.OP_NO_TICKET
        self.ctx.set_ciphers('PSK-AES128-GCM-SHA256')
        self.ctx.set_psk_server_callback(lambda offered:key if offered==identity else b'')
    def wrap(self, sock):
        sock.settimeout(5)
        return TLSStream(self.ctx.wrap_socket(sock,server_side=True))

class TLSStream:
    def __init__(self,sock): self.sock=sock; self.buf=bytearray()
    def readline(self,limit=LINE_MAX,timeout=15,deadline=None,no_progress=30):
        deadline=min(deadline,time.monotonic()+timeout) if deadline is not None else time.monotonic()+timeout
        progress_deadline=time.monotonic()+no_progress
        while True:
            i=self.buf.find(b'\n')
            if i>=0:
                if i>=limit: raise WirelessError('protocol line exceeds limit')
                value=bytes(self.buf[:i+1]); del self.buf[:i+1]; return value
            if len(self.buf)>=limit: raise WirelessError('protocol line exceeds limit')
            remaining=min(deadline,progress_deadline)-time.monotonic()
            if remaining<=0: raise TimeoutError('line timed out')
            self.sock.settimeout(remaining)
            chunk=self.sock.recv(min(8192,limit-len(self.buf)))
            if not chunk: raise WirelessError('peer closed')
            self.buf.extend(chunk)
            progress_deadline=time.monotonic()+no_progress
    def write(self,data):
        view=memoryview(data); sent=0
        deadline=getattr(self,"command_deadline",None)
        progress_deadline=time.monotonic()+getattr(self,"write_stall_timeout",8)
        while sent<len(view):
            remaining=min(progress_deadline,deadline if deadline is not None else float("inf"))-time.monotonic()
            if remaining<=0: raise TimeoutError('TLS write made no progress before deadline')
            self.sock.settimeout(remaining)
            count=self.sock.send(view[sent:])
            if not count: raise WirelessError('TLS peer closed during write')
            sent+=count
            progress_deadline=time.monotonic()+getattr(self,"write_stall_timeout",8)
        return sent
    def flush(self): pass
    def close(self): self.sock.close()

class Control:
    def __init__(self, state_path=None):
        self.state_path=Path(state_path or tap_refresh.DEFAULT_STATE)
        self.lock=threading.Lock(); self.current=None; self.connected=False; self.peer="none"
        self.device_id=None; self.uptime=None; self.last_touch=None; self.last_prox=None
        self.expected: str | None = None
        self.last_event=None; self.started=time.monotonic(); self.pending=queue.Queue(maxsize=8)
        self.last_ble_diagnostic=None; self.voice=None
    def status(self):
        now=time.monotonic()
        def sample(value):
            if value is None:return None
            age=max(0,round((now-value["received_monotonic"])*1000))
            return {"value":value["value"],"age_ms":age,"fresh":age<=10000}
        event=None
        if self.last_event:
            event={"count":self.last_event["count"],"uptime_ms":self.last_event["uptime_ms"],
                   "age_ms":max(0,round((now-self.last_event["received_monotonic"])*1000)),
                   "fresh":now-self.last_event["received_monotonic"]<=30}
        return {"ok":self.connected,"transport":"TLS1.2-PSK-AES128-GCM-SHA256" if self.connected else "disconnected","peer_identity":self.peer,"device_elf_sha256":self.device_id,"uptime_s":self.uptime,"touch":sample(self.last_touch),"proximity":sample(self.last_prox),"tap_event":event,"last_ble_diagnostic":self.last_ble_diagnostic}
    def _submit(self,command,timeout=None):
        if command=="status": return self.status()
        if timeout is None: timeout=35
        if not self.connected: raise WirelessError("no verified device")
        item={"session":self.current,"deadline":time.monotonic()+timeout,"command":command,"done":threading.Event(),"result":None}; self.pending.put(item,timeout=1)
        if not item["done"].wait(timeout):
            item["cancelled"]=True
            raise WirelessError("device command timed out; request was not replayed")
        if isinstance(item["result"],Exception): raise item["result"]
        return item["result"]
    def _readline(self,stream,timeout=20):
        deadline=getattr(self,"_active_deadline",None)
        if deadline is not None:
            remaining=deadline-time.monotonic()
            if remaining<=0: raise WirelessError("device command hard deadline exceeded")
            timeout=min(timeout,remaining)
        line=stream.readline(timeout=timeout).decode("ascii","strict").strip()
        # Server-initiated asynchronous protocol records, no arbitrary text.
        ev=re.fullmatch(r"TAP_EVENT (\d+) (\d+)",line)
        if ev:
            self.last_event={"count":int(ev.group(1)),"uptime_ms":int(ev.group(2)),"received_monotonic":time.monotonic()}; return None
        if line.startswith("TAP_FETCH "):
            if not re.fullmatch(r"TAP_FETCH [0-9a-f]{8}",line): raise WirelessError("malformed TAP_FETCH")
            engine=getattr(self,"voice",None)
            outcome=tap_refresh.serve_fetch(stream,line,self.state_path,engine)
            return "TAP_DONE "+outcome
        m=BANNER.fullmatch(line)
        if m:
            if self.device_id and self.device_id!=m.group(1): raise WirelessError("device ELF identity changed within session")
            self.device_id=m.group(1); self.uptime=int(m.group(2)); return None
        m=re.fullmatch(r"(TOUCH|PROX) (.+)",line)
        if m:
            receipt={"value":m.group(2)[:120],"received_monotonic":time.monotonic()}
            if m.group(1)=="TOUCH":self.last_touch=receipt
            else:self.last_prox=receipt
            return line
        return line
    def _expect(self,s,accepted,deadline=20):
        until=time.monotonic()+deadline
        while time.monotonic()<until:
            got=self._readline(s,max(.1,until-time.monotonic()))
            if got is None: continue
            if got.startswith("LOOK_MOTOR "):
                self._receipt=got[:500]; continue
            if got in accepted or any(got.startswith(x) for x in accepted):return got
            if got.startswith(("LOOK_ERROR","SAY_DENIED","SAY_ERROR")):raise WirelessError(got)
        raise WirelessError("device receipt timed out")
    def session(self,stream):
        self.current=stream; self.connected=False; self.device_id=self.uptime=self.last_event=self.last_prox=self.last_touch=None; self.peer="none"
        try:
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                try:line=stream.readline(timeout=.5).decode("ascii").strip()
                except TimeoutError:continue
                m=BANNER.fullmatch(line)
                if m:
                    expected=getattr(self,"expected",None)
                    if expected is not None and (not re.fullmatch(r"[0-9a-fA-F]{64}",expected) or expected.lower()!=m.group(1).lower()):
                        raise WirelessError('unexpected device ELF')
                    self.device_id=m.group(1); self.uptime=int(m.group(2)); self.peer=IDENTITY; self.connected=True; break
            else: raise WirelessError("exact ready banner not observed")
            while True:
                try:
                    line=self._readline(stream,.05)
                except TimeoutError:
                    line=None
                if line and line.startswith("TAP_CHECK "):
                    self.last_tap_check={"receipt":line[:80],"received_monotonic":time.monotonic()}
                    continue
                try:item=self.pending.get(timeout=.01)
                except queue.Empty:continue
                try:
                    if item["session"] is not stream or item.get("cancelled") or time.monotonic()>=item["deadline"]: raise WirelessError("expired command")
                    self._active_deadline=item["deadline"]
                    self._active_item=item
                    if isinstance(stream,TLSStream):
                        stream.command_deadline=item["deadline"]
                        stream.write_stall_timeout=30 if getattr(self,"_owner",None)=="ble" and item["command"] in OPERATION_TIMEOUTS else 8
                    item["result"]=self.execute(stream,item["command"])
                except Exception as e:item["result"]=WirelessError(str(e))
                finally:
                    if isinstance(stream,TLSStream):
                        stream.command_deadline=None
                        stream.write_stall_timeout=8
                    self._active_deadline=None
                    self._active_item=None
                    item["done"].set(); self.pending.task_done()
        finally:
            self.connected=False; self.peer="none"; self.current=None
    def execute(self,s,command):
        active=getattr(self,"_active_item",None)
        def ensure_active():
            if active is not None and (active.get("cancelled") or self.current is not s or time.monotonic()>=active["deadline"]):
                raise WirelessError("device command expired; no further actuation sent")
        ensure_active()
        if command=="ble-diagnostics":
            s.write(b"BLE_DIAGNOSTICS\n")
            line=self._expect(s,["SIMSTIM_BLE_DIAG ","LOOK_ERROR diagnostics_"])
            if line.startswith("LOOK_ERROR"):
                raise WirelessError("BLE diagnostics unavailable")
            if line=="SIMSTIM_BLE_DIAG available=0":
                self.last_ble_diagnostic={"available":False}
                return dict(self.last_ble_diagnostic)
            self.last_ble_diagnostic=_parse_ble_diagnostic(line)
            return dict(self.last_ble_diagnostic)
        if command=="status":
            for k,v in (("touch","TOUCH_STATUS"),("prox","PROX_STATUS")):
                try:self._query(s,v,k)
                except WirelessError:pass
            return self.status()
        if command in COMMANDS:
            cmd=COMMANDS[command]; self._receipt=None; s.write((cmd+"\n").encode());
            if command=="frame":
                lines=[]
                stall=FRAME_STALL_S if getattr(self,"_owner",None)=="ble" else 20
                while True:
                    if time.monotonic()>=self._active_deadline: raise WirelessError("frame hard deadline exceeded")
                    line=self._readline(s,stall)
                    if line is None:continue
                    if line.startswith('LOOK_ERROR'):
                        diag=next((v for v in reversed(lines) if re.fullmatch(r'LOOK_DIAG stage=[a-zA-Z0-9_]+ esp=-?\d+ errno=-?\d+ dma_free=\d+ dma_largest=\d+',v)), '')
                        raise WirelessError((line+' '+diag).strip())
                    lines.append(line)
                    if line=="LOOK_FRAME_END":break
                    if len(lines)>20000:raise WirelessError("frame response exceeds line bound")
                frame=look_room.read_frame(lines)
                # semantic extraction is on-demand, all bytes stay in RAM
                labels=look_room.recognize_scene(frame)
                return {"ok":True,"width":frame.width,"height":frame.height,"format":frame.format,"bytes":len(frame.data),"sha256":hashlib.sha256(frame.data).hexdigest(),"observation":labels,"retained":False}
            if command=="survey":
                lines=[]; glance_deadline=None
                stall=SURVEY_STALL_S if getattr(self,"_owner",None)=="ble" else 20
                while True:
                    if time.monotonic()>=self._active_deadline: raise WirelessError("survey hard deadline exceeded")
                    if glance_deadline is not None and time.monotonic()>=glance_deadline:
                        raise WirelessError("survey glance hard deadline exceeded")
                    line=self._readline(s,stall)
                    if line is None:continue
                    lines.append(line)
                    if re.fullmatch(r"LOOK_GLANCE [0-9]+",line):
                        glance_deadline=min(self._active_deadline,time.monotonic()+look_room.BLE_GLANCE_HARD_S)
                    if line.startswith("LOOK_SURVEY_END"):break
                    if len(lines)>60000:raise WirelessError("survey exceeds protocol bound")
                return look_room.read_survey(lines)
            if command in ("left","right","up","down","home","probe"):
                self._expect(s,["LOOK_ERROR"],25) if False else None
                # Movement receipts are explicit, bounded LOOK_MOTOR fields.
                end=time.monotonic()+25; receipts=[]
                while time.monotonic()<end:
                    try: line=self._readline(s,min(3,max(.1,end-time.monotonic())))
                    except TimeoutError: continue
                    if line is None:continue
                    if line.startswith("LOOK_MOTOR "): receipts.append(line[:500])
                    if line=="LOOK_ERROR operation":raise WirelessError(line)
                    if line.startswith("LOOK_MOTOR result=") and "action=" in line:
                        if "result=ok" not in line:raise WirelessError(line)
                        return {"ok":True,"receipt":line}
                raise WirelessError("motor receipt timed out")
            return self._expect(s,["UPDATE_STATUS","TOUCH ","PROX ","STATUS "])
        if command=="observe":
            # On-demand local room survey followed by the existing Kokoro->PCM protocol.
            detector=look_room.load_object_detector(); voice=look_room.load_kokoro_voice()
            elf_path=getattr(self,"elf_path",None) or release_elf_path(); elf_pin=getattr(self,"expected",os.environ.get("SIMSTIM_ELF_SHA256",""))
            item=getattr(self,"_active_item",None)
            def ensure_session():
                if self.current is not s or (item is not None and
                        (item.get("cancelled") or time.monotonic()>=item["deadline"])):
                    raise WirelessError("observe expired; no further protocol data sent")
            def before_speech():
                ensure_session()
            def before_survey():
                ensure_session()
            started=time.monotonic(); active_deadline=getattr(self,"_active_deadline",started+OBSERVE_DEADLINE_S)
            return look_room.survey_and_speak(s,voice,elf_path,elf_pin,started,detector,
                                              collection_deadline=min(active_deadline,started+SURVEY_DEADLINE_S),
                                              before_speech=before_speech,before_survey=before_survey,
                                              line_timeout=30 if getattr(self,"_owner",None)=="ble" else 15,
                                              ensure_active=ensure_session,operation_deadline=active_deadline)
        if command=="speak":
            s.write(b"SPEAK_ONLINE\n")
            receipt=self._expect(s,["SIMSTIM_SPEAKER result="])
            if "result=played" not in receipt or "reset_off=1" not in receipt:
                raise WirelessError("speaker receipt did not confirm playback and amp reset")
            return {"ok":True,"receipt":receipt[:240]}
        if command.startswith("stage "):
            if self.voice is None: self.voice=look_room.load_kokoro_voice()
            text=tap_refresh.spoken_copy(command[6:]); pcm=look_room.synthesize_observation(self.voice,text)
            crc=zlib.crc32(pcm)&0xffffffff; s.write(b"STAGE_UPDATE\n"); self._expect(s,["SAY_HEADER"]); s.write(struct.pack("<II",len(pcm),crc)); self._expect(s,["SAY_READY"]); s.write(pcm); self._expect(s,["UPDATE_STAGED"])
            s.write(b"UPDATE_STATUS\n"); return self._expect(s,[f"UPDATE_STATUS bytes={len(pcm)} crc={crc:08x}"])
        if command=="staged-status":
            s.write(b"UPDATE_STATUS\n"); return self._expect(s,["UPDATE_STATUS bytes="])
        if command=="tap-check":
            s.write(b"TAP_CHECK\n"); return self._expect(s,["TAP_DONE ","SIMSTIM_SPEAKER result=played"])
        if command=="tap-status":
            lock_path=self.state_path.with_suffix(self.state_path.suffix+".lock")
            lock_path.parent.mkdir(parents=True,exist_ok=True)
            with lock_path.open("a") as lock:
                fcntl.flock(lock,fcntl.LOCK_SH)
                try:data=json.loads(self.state_path.read_text())
                except FileNotFoundError:data={"items":{}}
            items=[(k,v) for k,v in data.get("items",{}).items() if isinstance(v,dict)]
            pending=[(k,v) for k,v in items if v.get("reviewed") is True and v.get("status") in ("pending","cued") and not v.get("tap_consumed_at")]
            return {"prepared":len(pending),"ids":[k for k,_ in pending[:16]],"consume_once":True}
        if command.startswith("tap-publish "):
            _,ident,text=command.split(" ",2); tap_refresh.spoken_copy(text)
            import subprocess
            r=subprocess.run([sys.executable,str(tap_refresh.ATTENTION),"--state",str(self.state_path),"put","--id",ident,"--public-summary",text,"--reviewed"],capture_output=True,text=True,timeout=15)
            if r.returncode:raise WirelessError("publish failed")
            return {"ok":True,"id":ident,"receipt":"prepared"}
        raise WirelessError("unsupported operator command")
    def _query(self,s,cmd,key):
        s.write((cmd+"\n").encode()); value=self._expect(s,["TOUCH ","PROX "])
        target=self.last_touch if key=="touch" else self.last_prox
        return target

def load_key(path=KEYFILE):
    st=path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:raise WirelessError("PSK file must be regular and mode 0600")
    key=path.read_bytes()
    if len(key)!=32:raise WirelessError("PSK file must contain exactly 32 raw bytes")
    return key

def tls_listener(key_path=KEYFILE):
    tls=OpenSSLPSK(load_key(Path(key_path)),IDENTITY)
    sock=socket.socket(); sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); sock.bind((os.environ.get("SIMSTIM_BIND", BIND), int(os.environ.get("SIMSTIM_PORT", str(PORT))))); sock.listen(4); sock.settimeout(.5)
    return sock,tls

def run_operator(control,path=OPSOCK):
    path=Path(path); path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    if path.exists():
        if not stat.S_ISSOCK(path.stat().st_mode):raise WirelessError("operator socket path exists and is not a socket")
        path.unlink()
    srv=socket.socket(socket.AF_UNIX); srv.bind(str(path)); os.chmod(path,0o600); srv.listen(4); srv.settimeout(.5)
    try:
        while True:
            try:client,_=srv.accept()
            except socket.timeout:continue
            with client:
                client.settimeout(max(OPERATION_TIMEOUTS.values())+OPERATOR_MARGIN_S*2)
                try:
                    with client.makefile('rb') as reader: raw=reader.readline(4097)
                    if not raw.endswith(b"\n") or len(raw)>4096:raise WirelessError("one bounded newline-terminated request required")
                    req=json.loads(raw); cmd=req.get("command")
                    if not isinstance(cmd,str):raise WirelessError("command required")
                    result=control._submit(cmd)
                    response=json.dumps({"ok":True,"result":result},separators=(",",":"))
                except Exception as e:
                    response=json.dumps({"ok":False,"error":str(e)[:160]},separators=(",",":"))
                # A caller that timed out must not kill the listener or replay work.
                try: client.sendall((response+'\n').encode())
                except OSError: pass
    finally:srv.close();path.unlink(missing_ok=True)

def serve(key_path=KEYFILE,sock_path=OPSOCK,state_path=None):
    c=Control(state_path); c.elf_path=release_elf_path(); c.expected=os.environ.get("SIMSTIM_ELF_SHA256", "") or None; sock,tls=tls_listener(key_path)
    worker=threading.Thread(target=run_operator,args=(c,sock_path),daemon=True);worker.start()
    try:
        while True:
            try:peer,_=sock.accept()
            except socket.timeout:continue
            peer.settimeout(5.0)
            try:
                stream=tls.wrap(peer); c.session(stream)
            except (WirelessError,OSError,ValueError,TimeoutError) as e:
                print(f"wireless-control: rejected/closed peer ({type(e).__name__})",file=sys.stderr,flush=True)
            finally:
                try:stream.close()
                except Exception:pass
                peer.close()
    finally:sock.close()

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key",type=Path,default=KEYFILE); parser.add_argument("--operator-socket",type=Path,default=OPSOCK)
    parser.add_argument("--state",type=Path,default=None); args=parser.parse_args()
    serve(args.key,args.operator_socket,args.state)
