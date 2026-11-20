"""Observe one physical boot; never replay logs or reopen a failed USB session."""
import argparse
import os
import re
import time
from parse_progress import parse_progress

# Set SIMSTIM_SERIAL_PORT to the authorised USB Serial/JTAG port for this board.
PORT = os.environ.get('SIMSTIM_SERIAL_PORT', '/dev/ttyUSB0')

class Window:
    def __init__(self, digest, seconds):
        self.digest, self.seconds = digest, seconds
        self.first = self.start = self.last = self.prev = None

    def feed(self, line, now):
        if any(s in line for s in ('assert failed:', 'Guru Meditation Error', 'Brownout detector', 'abort() was called')):
            raise ValueError('device reported a fault')
        if 'Loaded app from partition at offset' in line:
            if self.first is not None or not re.search(r'offset 0x20000\b', line):
                raise ValueError('unexpected boot')
        if not line.startswith('SIMSTIM_STANDALONE'):
            return False
        r = parse_progress(line)
        n = r['uptime_s']
        if r['sha256'] != self.digest or not r['board_ready']:
            raise ValueError('wrong image or board not ready')
        if self.first is None:
            if not 1 <= n <= 5:
                raise ValueError('start of boot missed')
            self.first, self.start = n, now
        elif n != self.last + 1 or now - self.prev > 4:
            raise ValueError('uptime discontinuity')
        self.last, self.prev = n, now
        return n - self.first >= self.seconds and now - self.start >= self.seconds - 1

def main():
    import serial
    from esptool.reset import HardReset
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument('--elf-sha', required=True)
    a.add_argument('--seconds', type=int, default=120)
    a.add_argument('--reset-after-verified-flash', action='store_true')
    args = a.parse_args()
    if not re.fullmatch('[0-9a-f]{64}',args.elf_sha) or not 60 <= args.seconds <= 1800:
        a.error('require exact ELF digest and a 60–1800 second window')
    w = Window(args.elf_sha,args.seconds)
    p = serial.Serial(port=None, baudrate=115200, timeout=.4, exclusive=True)
    p.dtr = p.rts = False
    p.port = PORT
    with p:
        p.reset_input_buffer()
        if args.reset_after_verified_flash:
            HardReset(p,uses_usb=True).reset()
        start = time.monotonic()
        buf = b''
        while time.monotonic()-start < args.seconds+35:
            buf += p.read(min(max(1,p.in_waiting),4096))
            while b'\n' in buf:
                line,buf = buf.split(b'\n',1)
                now=time.monotonic()
                if w.feed(line.decode('ascii','replace').strip(),now):
                    print(f'Physical USB: ELF {w.digest}; uptime {w.first}..{w.last}; elapsed {now-w.start:.1f}s; board initialisation reported ready. No screen observation.',flush=True)
                    return
            if len(buf)>8192: raise SystemExit('unterminated input; no soak receipt')
            if w.prev is not None and time.monotonic()-w.prev>4: raise SystemExit('progress stopped; no soak receipt')
        raise SystemExit('observation timed out; no soak receipt')

if __name__=='__main__': main()
