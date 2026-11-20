"""One live BLE session, checked around each command. Never replay an action."""
import argparse, time
from cli import request_once, OPSOCK

REQUEST_TIMEOUTS = {'wifi-suspend': 45, 'wifi-resume': 45, 'frame': 330,
                    'survey': 630, 'observe': 850}
WIFI_RESTORE_CAP_S = 3600

def wifi_suspend_budget(commands):
    # Include BLE acquisition (65 s), every caller wait, and 30 s for the
    # explicit resume receipt. Daemon auto-restore remains the hard backstop.
    return min(WIFI_RESTORE_CAP_S, 65 + sum(REQUEST_TIMEOUTS.get(cmd,45) for cmd in commands) + 30)


def run(pin, commands):
    def call(cmd):
        timeout=REQUEST_TIMEOUTS.get(cmd,45)
        out=request_once(OPSOCK,cmd,timeout)
        if not out.get('ok'): raise RuntimeError(out.get('error','operator failed'))
        return out['result']
    def state(bearer):
        s=call('status')
        return s if s['ok'] and s['peer_identity']==service.IDENTITY and s['device_elf_sha256']==pin and s['transport']==bearer else None
    def wait(name,seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            s=state(name)
            if s: return s
            time.sleep(.25)
        raise RuntimeError('no authenticated '+name)
    if not (state('wifi') or state('ble')):raise RuntimeError('no pinned starting peer')
    try:
        call(f'wifi-suspend {wifi_suspend_budget(commands)}');s=wait('ble',65);g=s['generation'];print('BLE',pin,'generation',g,flush=True)
        for cmd in commands:
            if (state('ble') or {}).get('generation')!=g:raise RuntimeError('link changed before '+cmd)
            v=call(cmd)
            if (state('ble') or {}).get('generation')!=g:raise RuntimeError('link changed during '+cmd)
            if isinstance(v,dict):
                if v.get('ok') is False:
                    raise RuntimeError(f"{cmd} failed: frames={v.get('frames')} motor={v.get('motor_receipts', [])}")
                if cmd=='observe' and (v.get('frames')!=3 or v.get('frame_bytes')!=460800 or v.get('speech')!='played'):
                    raise RuntimeError('incomplete three-view spoken survey')
                v={k:v[k] for k in ('ok','receipt','width','height','format','bytes','retained','speech','frames','frame_bytes','stereo_bytes') if k in v and isinstance(v[k],(int,str,bool))}
            print(cmd,v,flush=True)
    finally:
        call('wifi-resume'); print('Wi-Fi listener enabled',flush=True)

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('pin');ap.add_argument('commands',nargs='+',choices=['touch','prox','staged-status','probe','left','right','up','down','home','speak','frame','tap-check','observe']);args=ap.parse_args();run(args.pin,args.commands)
