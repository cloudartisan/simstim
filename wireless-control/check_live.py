#!/usr/bin/env python3
"""Explicit USB physical acceptance for the pinned K151 standalone ELF.

This is an attended hardware runner, not an offline test. It never discovers a
port, opens Wi-Fi, captures ambient audio, or sends IR/tag data. Keep raw frame
bytes in memory only. Human-only checks are reported separately.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
DEFAULT_ELF = ROOT / "k151-standalone/build/k151_standalone.elf"
READY = re.compile(r"SIMSTIM_STANDALONE sha256=([0-9a-f]{64}) uptime_s=\d+ board_ready=1")
MOTOR = re.compile(r"LOOK_MOTOR result=ok action=([A-Za-z_]+).*?yaw_raw=(-?[0-9]+) yaw_deg=(-?[0-9]+).*?pitch_raw=(-?[0-9]+) pitch_deg=(-?[0-9]+).*?sample=last_valid_per_axis")

class AcceptanceError(RuntimeError): pass

def verify_elf(path: Path, expected: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        raise AcceptanceError("--elf-sha256 must be the full lowercase SHA-256 of the actual ELF")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise AcceptanceError(f"cannot read full ELF: {e}") from e
    if not data.startswith(b"\x7fELF"):
        raise AcceptanceError("ELF file has no ELF magic")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise AcceptanceError("full ELF SHA-256 differs from --elf-sha256")
    return actual

def parse_motor(line: str, action: str) -> tuple[int, int, int, int]:
    m = MOTOR.search(line)
    if not m or m.group(1).lower() != action.lower():
        raise AcceptanceError(f"missing successful {action} readback receipt")
    return tuple(map(int, m.groups()[1:]))

def read_line(port, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = port.readline()
        if raw:
            line = raw.decode("ascii", "strict").strip()
            if line.startswith(("LOOK_ERROR", "SAY_ERROR", "SAY_DENIED")):
                raise AcceptanceError(line[:160])
            if line.startswith(("LOOK_MOTOR ", "LOOK_FRAME_", "LOOK_DATA ", "LOOK_SURVEY", "LOOK_GLANCE ", "TAP_EVENT ", "TAP_FETCH ", "TAP_DONE ", "TAP_CHECK ", "UPDATE_STATUS ", "UPDATE_STAGED", "SIMSTIM_SPEAKER ")):
                return line
    raise AcceptanceError("timed out waiting for device receipt")

def receipt(port, command, prefix="LOOK_MOTOR "):
    port.write((command + "\n").encode("ascii")); port.flush()
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        line=read_line(port, max(.1,deadline-time.monotonic()))
        if line.startswith(prefix): return line
    raise AcceptanceError(f"no {prefix.strip()} receipt for {command}")

def run(args):
    digest=verify_elf(args.elf,args.elf_sha256)
    if args.bearer != "usb":
        raise AcceptanceError("this runner implements USB Serial/JTAG only; Wi-Fi needs a separately verified transport adapter")
    import serial
    from look_room import load_object_detector
    detector=load_object_detector()  # Warm local analysis before acquiring the physical port.
    port=serial.Serial(port=None, baudrate=115200, timeout=.15, write_timeout=5, exclusive=True)
    port.dtr=False; port.rts=False; port.port=args.port
    entered=False
    try:
        with port:
            deadline=time.monotonic()+args.ready_timeout
            while time.monotonic()<deadline:
                line=port.readline()
                if not line: continue
                m=READY.fullmatch(line.decode("ascii","strict").strip())
                if m:
                    if m.group(1)!=digest: raise AcceptanceError("running firmware ELF differs; no commands sent")
                    entered=True; break
            if not entered: raise AcceptanceError("matching board_ready=1 banner not observed; no commands sent")
            print(f"PASS transport=usb elf_sha256={digest}")
            # Each command moves 5 degrees within the firmware's home-relative envelope.
            home=parse_motor(receipt(port,"LOOK_HOME"),"home")
            try:
                for action, axis, sign in (("left",1,-1),("right",1,1),("up",3,1),("down",3,-1)):
                    baseline=parse_motor(receipt(port,"LOOK_HOME"),"home")
                    moved=parse_motor(receipt(port,"LOOK_"+action.upper()),action)
                    if (moved[axis]-baseline[axis])*sign < 3:
                        raise AcceptanceError(f"{action} readback did not move in the expected direction")
                    returned=parse_motor(receipt(port,"LOOK_HOME"),"home")
                    if abs(returned[0]-home[0])>12 or abs(returned[2]-home[2])>12:
                        raise AcceptanceError(f"home readback drift after {action}")
                print("PASS motion readbacks: left/right yaw; up/down pitch")
            finally:
                final=parse_motor(receipt(port,"LOOK_HOME"),"home")
            if abs(final[0]-home[0])>12 or abs(final[2]-home[2])>12:
                raise AcceptanceError("home readback did not return near initial home")
            print("PASS home readback")
            # Device frame is fully drained and checked, but its raw bytes are not emitted or saved.
            port.write(b"LOOK_FRAME\n"); port.flush(); lines=[]
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                line=read_line(port,max(.1,deadline-time.monotonic())); lines.append(line)
                if line=="LOOK_FRAME_END": break
                if len(lines)>5000: raise AcceptanceError("frame line bound exceeded")
            else: raise AcceptanceError("incomplete frame")
            from look_room import read_frame
            frame=read_frame(lines)
            if (frame.width,frame.height,frame.format,len(frame.data))!=(320,240,"RGB565",153600): raise AcceptanceError("unexpected frame geometry/format/size")
            print("PASS complete fresh frame 320x240 RGB565; raw media not retained")
            # Survey requires the existing local detector and includes safe survey motion.
            from look_room import read_survey
            port.write(b"LOOK_SURVEY\n"); port.flush(); survey_lines=[]
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                line=read_line(port,max(.1,deadline-time.monotonic())); survey_lines.append(line)
                if line.startswith("LOOK_SURVEY_END"): break
                if len(survey_lines)>20000: raise AcceptanceError("survey line bound exceeded")
            else: raise AcceptanceError("incomplete survey")
            survey=read_survey(survey_lines,detector=detector)
            if not survey.get("ok") or survey.get("frames")!=3: raise AcceptanceError("survey did not return all three views")
            print("PASS survey/observe inputs: 3 complete views; semantic output only")
            print("HUMAN REQUIRED: confirm screen visible/status and camera cue; listener confirmation is not verifiable from receipts.")
            print("HUMAN REQUIRED: perform a real screen release and confirm a fresh TAP_EVENT; TAP_CHECK is diagnostic only.")
            print("NOT RUN: staged/dynamic audio playback and TAP_CHECK require the authorized tap listener and are intentionally not synthesized by this runner.")
            return 0
    finally:
        # pyserial context closes port; never send a cleanup command on reconnect.
        try:
            if port.is_open: port.close()
        except Exception: pass

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bearer",required=True,choices=("usb","wifi"),help="actual physical bearer; no automatic fallback")
    p.add_argument("--port",help="explicit USB Serial/JTAG path (required for --bearer usb)")
    p.add_argument("--elf",required=True,type=Path,help="full matching standalone ELF")
    p.add_argument("--elf-sha256",required=True,help="full lowercase SHA-256 of that ELF")
    p.add_argument("--ready-timeout",type=float,default=30)
    a=p.parse_args(argv)
    if a.bearer=="usb" and not a.port: p.error("--port is required for USB")
    try: return run(a)
    except Exception as e:
        print(f"check_live: {type(e).__name__}: {str(e)[:200]}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
