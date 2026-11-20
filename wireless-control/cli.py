#!/usr/bin/env python3
"""Operator client for the protected SIMSTIM wireless-control Unix socket."""
import argparse, errno, json, socket, sys, time

# Only repeat non-actuating state reads. Even LOOK_PROBE powers the servo rail.
SAFE_READS = frozenset(('touch','prox','staged-status'))
COMMAND_TIMEOUTS = {'frame': 330, 'survey': 630, 'observe': 850}
TRANSIENT = frozenset(('no verified device', 'expired command', 'peer closed',
                       '[Errno 104] Connection reset by peer', '[Errno 32] Broken pipe'))

def request_once(path, command, timeout):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(timeout); s.connect(str(path)); s.sendall((json.dumps({'command':command})+'\n').encode())
        data=bytearray()
        while not data.endswith(b'\n'):
            chunk=s.recv(4096)
            if not chunk: raise ConnectionResetError(errno.ECONNRESET,'local operator disconnected')
            data.extend(chunk)
            if len(data)>16384: raise ValueError('operator response exceeds limit')
    return json.loads(data)

def request(path, command, *, once=request_once, clock=time.monotonic, sleep=time.sleep):
    deadline=clock()+40; delay=.3; attempts=0
    bulk_timeout=45
    if command in COMMAND_TIMEOUTS:
        try:
            status=once(path,'status',15)
            transport=status.get('result',{}).get('transport') if status.get('ok') else None
            if transport=='ble': bulk_timeout=COMMAND_TIMEOUTS[command]
        except OSError:
            pass
    while True:
        attempts+=1
        try:
            out=once(path,command,max(.1,deadline-clock()) if command in SAFE_READS else bulk_timeout)
            if out.get('ok') or out.get('error') not in TRANSIENT or command not in SAFE_READS:
                if attempts>1: out['attempts']=attempts
                return out
        except OSError as e:
            if command not in SAFE_READS or e.errno not in (errno.ENOENT,errno.ECONNRESET,errno.EPIPE,errno.ECONNREFUSED): raise
            out={'ok':False,'error':'local operator unavailable'}
        if attempts>=20 or clock()+delay>=deadline:
            return {**out,'attempts':attempts}
        sleep(delay);delay=min(3,delay*2)
from pathlib import Path
from service import OPSOCK

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket",type=Path,default=OPSOCK)
    sub=p.add_subparsers(dest="command",required=True)
    for name in ("status","touch","prox","probe","left","right","up","down","home","frame","survey","observe","speak","tap-check","tap-status","staged-status","ble-diagnostics"):
        sub.add_parser(name)
    pub=sub.add_parser("tap-publish");pub.add_argument("id");pub.add_argument("summary")
    stage=sub.add_parser("stage"); stage.add_argument("summary")
    a=p.parse_args(); command=("tap-publish "+a.id+" "+a.summary) if a.command=="tap-publish" else ("stage "+a.summary if a.command=="stage" else a.command)
    try:
        result=request(a.socket,command)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(f"simstim-wireless: {e}",file=sys.stderr);return 2
if __name__=="__main__":raise SystemExit(main())
