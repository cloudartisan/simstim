#!/usr/bin/env python3
"""Sole host operator: verify local image/voice + live board identity, survey once."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

if __package__:
    from .look_room import (ChunkLineReader, LookError, load_kokoro_voice, load_object_detector,
                            survey_and_speak, verify_board_elf)
else:
    from look_room import (ChunkLineReader, LookError, load_kokoro_voice, load_object_detector,
                           survey_and_speak, verify_board_elf)

READY_RE = re.compile(r"SIMSTIM_STANDALONE sha256=([0-9a-f]{64}) uptime_s=([0-9]+) board_ready=1")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="explicit serial device path; no auto-selection")
    parser.add_argument("--elf", required=True, type=Path)
    parser.add_argument("--elf-sha256", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--ready-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        local_hash = verify_board_elf(args.elf, args.elf_sha256)
        # Load and warm both CPU models before taking exclusive serial ownership.
        detector = load_object_detector()
        voice = load_kokoro_voice()
        import serial
        port = serial.Serial(port=None, baudrate=args.baud, timeout=0.05, write_timeout=5, exclusive=True)
        port.dtr = False
        port.rts = False
        port.port = args.port
        with port:
            reader = ChunkLineReader(port)
            deadline = time.monotonic() + args.ready_timeout
            ready = False
            while time.monotonic() < deadline:
                raw = reader.readline(max(0.001, deadline-time.monotonic()))
                if not raw:
                    continue
                line = raw.decode("ascii", errors="replace").strip()
                match = READY_RE.fullmatch(line)
                if not match:
                    continue
                if match.group(1) != local_hash:
                    raise LookError("running board ELF SHA-256 differs from the locally verified ELF; no command sent")
                ready = True
                break
            if not ready:
                raise LookError("timed out waiting for SIMSTIM_STANDALONE ELF/board_ready identity; no command sent")
            print(f"board_ready=1 elf_sha256={local_hash}; sending one LOOK_SURVEY", file=sys.stderr, flush=True)
            result = survey_and_speak(reader, voice, args.elf, local_hash, detector=detector)
            # Deliberately omit serial lines, image bytes, base64 and motor receipts.
            safe = {key: result[key] for key in ("ok", "frames", "frame_bytes", "first_response_ms", "total_ms", "text", "speech", "stereo_bytes", "views") if key in result}
            print(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))
            return 0 if result["ok"] else 1
    except (LookError, OSError, RuntimeError, ImportError) as exc:
        print(f"look_operator: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
