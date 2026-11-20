#!/usr/bin/env python3
"""Verify a real radio session using only the protected operator socket (never USB)."""
import argparse
from pathlib import Path
import re
from cli import request
from service import IDENTITY, OPSOCK
from check_live import verify_elf, AcceptanceError


def verify_radio(get, bearer, digest):
    def state():
        s=get('status')
        # Legacy wifi-only service reports the exact TLS suite in transport.
        name={'TLS1.2-PSK-AES128-GCM-SHA256':'wifi'}.get(s['transport'],s['transport'])
        if not s['ok'] or name!=bearer or s['device_elf_sha256']!=digest or s['peer_identity']!=IDENTITY:
            raise AcceptanceError('wrong or unverified physical peer, bearer or ELF')
        return name,s.get('generation'),s['uptime_s']
    first=state()
    for cmd,pattern in [('touch',r'TOUCH (PRESSED|RELEASED) \d+'),('prox',r'PROX OK \d+ [01]'),('staged-status',r'UPDATE_STATUS bytes=\d+ crc=[a-f0-9]{8}')]:
        receipt=get(cmd); last=state()
        if last[:2]!=first[:2] or last[2]<first[2]:raise AcceptanceError('session changed during probe; evidence discarded')
        if not re.fullmatch(pattern,receipt): raise AcceptanceError('missing expected '+cmd+' reply')
        print(bearer,cmd,receipt)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--bearer',choices=['wifi','ble'],required=True);p.add_argument('--elf',type=Path,required=True);p.add_argument('--elf-sha256',required=True);p.add_argument('--socket',type=Path,default=OPSOCK);a=p.parse_args()
    def get(cmd):
        r=request(a.socket,cmd)
        if not r.get('ok'):raise AcceptanceError(r.get('error','operator failed'))
        return r['result']
    verify_radio(get,a.bearer,verify_elf(a.elf,a.elf_sha256))
