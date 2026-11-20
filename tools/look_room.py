#!/usr/bin/env python3
"""On-demand host-side SIMSTIM frame protocol and semantic local object labels.

The operator entrypoint is tools/look_operator.py; this module also supports
transport-neutral parsing/tests. Received image bytes are never persisted/logged.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import re
import struct
import sys
import time
import zlib
from itertools import chain
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

COMMANDS = ("LOOK_FRAME", "LOOK_LEFT", "LOOK_RIGHT", "LOOK_UP", "LOOK_DOWN", "LOOK_HOME", "LOOK_SURVEY")
MAX_SURVEY_FRAMES = 8
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_SERIAL_LINE_BYTES = 20000
BLE_FRAME_HARD_S = 300
BLE_SURVEY_HARD_S = 600
BLE_GLANCE_HARD_S = 180


class LookError(ValueError):
    """Invalid protocol, frame, or firmware identity."""


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    format: str
    data: bytes


class ChunkLineReader:
    """Bounded newline reader that amortizes serial reads across protocol lines."""
    def __init__(self, stream, chunk_size: int = 4096):
        self.stream = stream
        self.chunk_size = chunk_size
        self.buffer = bytearray()

    def write(self, payload):
        return self.stream.write(payload)

    def flush(self):
        return self.stream.flush()

    def readline(self, timeout: float = 18.0) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[:newline + 1])
                del self.buffer[:newline + 1]
                return line
            if len(self.buffer) >= MAX_SERIAL_LINE_BYTES:
                raise LookError("serial protocol line exceeds byte limit")
            if time.monotonic() >= deadline:
                raise LookError("timed out awaiting a device protocol line")
            chunk = self.stream.read(min(self.chunk_size, MAX_SERIAL_LINE_BYTES - len(self.buffer)))
            if chunk:
                self.buffer.extend(chunk)


def verify_board_elf(path: Path, expected_sha256: str) -> str:
    """Require the caller's pinned board ELF before emitting any board action."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256 or ""):
        raise LookError("--elf-sha256 must be the 64-hex digest of the matching board ELF")
    try:
        with path.open("rb") as f:
            header = f.read(4)
            if header != b"\x7fELF":
                raise LookError("board ELF is not an ELF executable")
            h = hashlib.sha256(header)
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        raise LookError(f"cannot read board ELF: {e}") from e
    actual = h.hexdigest()
    if actual.lower() != expected_sha256.lower():
        raise LookError("board ELF digest does not match the pinned expected digest")
    return actual


def emit_command(command: str, elf: Path, expected_sha256: str, out: TextIO = sys.stdout) -> None:
    if command not in COMMANDS:
        raise LookError(f"unsupported command: {command}")
    verify_board_elf(elf, expected_sha256)
    print(command, file=out)


def parse_motor_receipt(line: str) -> str | None:
    """Recognize tolerant human-printed motor receipts without interpreting movement."""
    s = line.strip()
    if not s.startswith("LOOK_MOTOR"):
        return None
    # Firmware revisions may add/change fields; preserve only the line, bounded and
    # control-character-free, rather than imposing a brittle receipt grammar.
    return " ".join(s.split())[:240]


def read_frame(lines: Iterable[str]) -> Frame:
    """Parse one bounded BEGIN/DATA/END frame; each DATA line is padded base64."""
    it = iter(lines)
    try:
        begin = next(it).strip().split()
    except StopIteration as e:
        raise LookError("missing LOOK_FRAME_BEGIN") from e
    if len(begin) != 5 or begin[0] != "LOOK_FRAME_BEGIN":
        raise LookError("expected LOOK_FRAME_BEGIN <width> <height> <format> <bytes>")
    try:
        width, height, fmt, size = int(begin[1]), int(begin[2]), begin[3].upper(), int(begin[4])
    except ValueError as e:
        raise LookError("invalid frame header values") from e
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise LookError("frame dimensions out of bounds")
    if fmt not in {"JPEG", "RGB565"}:
        raise LookError("unsupported frame format; expected JPEG or RGB565")
    expected = width * height * 2 if fmt == "RGB565" else size
    if not (1 <= size <= MAX_FRAME_BYTES) or (fmt == "RGB565" and size != expected):
        raise LookError("invalid frame byte count")
    raw = bytearray()
    data_lines = 0
    ended = False
    for rawline in it:
        line = rawline.strip()
        if line == "LOOK_FRAME_END":
            ended = True
            break
        if not line.startswith("LOOK_DATA "):
            raise LookError("expected LOOK_DATA or LOOK_FRAME_END")
        data_lines += 1
        payload = line[len("LOOK_DATA "):].strip()
        if not payload or len(payload) > 16384 or data_lines > 65536:
            raise LookError("LOOK_DATA line or count exceeds protocol bounds")
        try:
            part = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as e:
            raise LookError("invalid individually padded LOOK_DATA base64") from e
        raw.extend(part)
        if len(raw) > size:
            raise LookError("frame data exceeds declared byte count")
    if not ended:
        raise LookError("missing LOOK_FRAME_END")
    if len(raw) != size:
        raise LookError("frame byte count does not match header")
    if fmt == "JPEG" and not (raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9")):
        raise LookError("JPEG markers are missing")
    return Frame(width, height, fmt, bytes(raw))


def _decode_rgb(frame: Frame):
    """Decode a frame into a Pillow image, never writing it to disk."""
    try:
        from PIL import Image
    except ImportError as e:
        raise LookError("local decoding requires Pillow (install tools/look-requirements.txt)") from e
    if frame.format == "JPEG":
        try:
            image = Image.open(io.BytesIO(frame.data))
            image.load()
            if image.size != (frame.width, frame.height):
                raise LookError("JPEG dimensions disagree with frame header")
            return image.convert("RGB")
        except LookError:
            raise
        except Exception as e:
            raise LookError("invalid JPEG frame") from e
    # Protocol RGB565 is little-endian, RGB bit layout 5/6/5.
    px = bytearray(frame.width * frame.height * 3)
    for i in range(frame.width * frame.height):
        value = frame.data[2 * i] | frame.data[2 * i + 1] << 8
        r, g, b = (value >> 11) & 31, (value >> 5) & 63, value & 31
        px[3 * i:3 * i + 3] = bytes(((r * 255 + 15) // 31, (g * 255 + 31) // 63, (b * 255 + 15) // 31))
    return Image.frombytes("RGB", (frame.width, frame.height), bytes(px))


def load_object_detector():
    """Load and warm the verified local CPU semantic detector before motion."""
    try:
        if __package__:
            from .local_objects import load_detector
        else:
            from local_objects import load_detector
        return load_detector()
    except Exception as e:
        raise LookError(str(e)) from e


def detect_objects(frame: Frame, detector=None) -> list[tuple[str, float]]:
    """Run local COCO object detection only; no color, face, OCR, or identity path."""
    detector = detector or load_object_detector()
    try:
        return detector.detect(_decode_rgb(frame))
    except Exception as e:
        raise LookError(f"local object detection failed: {e}") from e


def recognize_scene(frame: Frame, detector=None) -> str:
    """Describe detected generic COCO objects; never identify faces or text."""
    if __package__:
        from .local_objects import describe_detections
    else:
        from local_objects import describe_detections
    return describe_detections(detect_objects(frame, detector))


def read_survey(lines: Iterable[str], started: float | None = None, detector=None,
                collection_deadline: float | None = None, clock=time.monotonic) -> dict:
    """Read device-choreographed survey including LOOK_GLANCE <index> markers."""
    it = iter(lines)
    start = time.monotonic() if started is None else started
    motor_receipts = []
    while True:
        if collection_deadline is not None and clock() >= collection_deadline:
            raise LookError("survey collection hard deadline exceeded")
        try:
            first = next(it).strip()
        except StopIteration as e:
            raise LookError("missing LOOK_SURVEY_BEGIN") from e
        motor = parse_motor_receipt(first)
        if motor is not None:
            if len(motor_receipts) < 16:
                motor_receipts.append(motor)
            continue
        if first != "LOOK_SURVEY_BEGIN":
            raise LookError("expected LOOK_SURVEY_BEGIN")
        break
    first_frame_at = None
    frames: list[Frame] = []
    markers = []
    pending = None
    frame_bytes = 0
    count = 0
    glance_deadline = None
    while True:
        now = clock()
        if collection_deadline is not None and now >= collection_deadline:
            raise LookError("survey collection hard deadline exceeded")
        if glance_deadline is not None and now >= glance_deadline:
            raise LookError("survey glance hard deadline exceeded")
        try:
            line = next(it).strip()
        except StopIteration as e:
            raise LookError("missing LOOK_SURVEY_END") from e
        if line.startswith("LOOK_SURVEY_END"):
            fields = line.split()
            if fields == ["LOOK_SURVEY_END"]:
                ok = True  # Existing camera firmware emits this successful terminator.
            elif len(fields) == 2 and fields[1] in {"ok=0", "ok=1"}:
                ok = fields[1] == "ok=1"
            else:
                raise LookError("invalid LOOK_SURVEY_END; expected bare terminator or ok=1/ok=0")
            break
        if line.startswith("LOOK_GLANCE "):
            if not re.fullmatch(r"LOOK_GLANCE [0-9]+", line):
                raise LookError("invalid LOOK_GLANCE marker")
            if pending is not None: raise LookError("glance without frame")
            pending = int(line.split()[1])
            if collection_deadline is not None:
                glance_deadline = min(clock() + BLE_GLANCE_HARD_S, collection_deadline)
            continue
        if line.startswith("LOOK_SURVEY_FRAMES "):
            if not re.fullmatch(r"LOOK_SURVEY_FRAMES [0-8]", line):
                raise LookError("invalid LOOK_SURVEY_FRAMES count")
            continue
        motor = parse_motor_receipt(line)
        if motor is not None:
            if len(motor_receipts) < 16:
                motor_receipts.append(motor)
            continue
        if line == "LOOK_SURVEY_BEGIN":
            raise LookError("unexpected nested survey begin")
        count += 1
        if count > MAX_SURVEY_FRAMES:
            raise LookError(f"survey exceeds {MAX_SURVEY_FRAMES} frames")
        if first_frame_at is None and line.startswith("LOOK_FRAME_BEGIN "):
            first_frame_at = time.monotonic()
        frame_lines = iter(chain((line,), it))
        def bounded_frame_lines():
            for frame_line in frame_lines:
                current = clock()
                if collection_deadline is not None and current >= collection_deadline:
                    raise LookError("survey collection hard deadline exceeded")
                if glance_deadline is not None and current >= glance_deadline:
                    raise LookError("survey glance hard deadline exceeded")
                yield frame_line
        frame = read_frame(bounded_frame_lines())
        frames.append(frame)
        markers.append(pending)
        pending = None
        frame_bytes += len(frame.data)
    # Finish transport draining before running potentially slow local inference.
    observations: dict[str, int] = {}
    views = []
    for index,frame in zip(markers,frames):
        detections = detect_objects(frame, detector)
        from PIL import Image
        quad = list(_decode_rgb(frame).convert('L').resize((2,2),Image.Resampling.BOX).getdata())
        names = ('upper left','upper right','lower left','lower right')
        feature = names[quad.index(max(quad))] if max(quad)-min(quad)>=32 and sorted(quad)[-1]-sorted(quad)[-2]>=12 else None
        views.append({'glance':index, 'objects':[(label,round(float(score),2)) for label,score in detections], 'brightest_region':feature,'quadrant_luma':quad})
        for label, _score in detections:
            observations[label] = observations.get(label, 0) + 1
    if count == 0:
        ok = False
    if __package__:
        from .local_objects import describe_detections
    else:
        from local_objects import describe_detections
    robust = sorted(((n, label) for label, n in observations.items() if n >= (2 if count > 1 else 1)), reverse=True)
    if not ok:
        sentence = "I couldn't complete a reliable room survey, so I can't describe the scene confidently."
    elif robust:
        chosen = [(label, 1.0) for _, label in robust[:3]]
        sentence = describe_detections(chosen)
    elif len(views)==3 and [v['glance'] for v in views]==[0,1,2] and views[1]['brightest_region'] and views[2]['brightest_region'] and views[1]['brightest_region']!=views[2]['brightest_region']:
        sentence = f"Looking left, the light is strongest at the {views[1]['brightest_region']}; looking right, it's strongest at the {views[2]['brightest_region']}."
    elif count:
        sentence = "I got a few views, but couldn't identify a clear object confidently."
    else:
        sentence = "The survey returned no usable views, so I can't describe the scene."
    ended = time.monotonic()
    return {
        "ok": bool(ok and count),
        "frames": count,
        "frame_bytes": frame_bytes,
        "first_response_ms": round((first_frame_at - start) * 1000, 1) if first_frame_at else None,
        "total_ms": round((ended - start) * 1000, 1),
        "text": sentence,
        "motor_receipts": motor_receipts,
        "views": views,
    }


def _session_line(session, deadline: float | None = None, clock=time.monotonic, line_timeout=15) -> str:
    reader = session if isinstance(session, ChunkLineReader) else None
    line_deadline=min(clock()+18,deadline) if deadline is not None else clock()+18
    while clock()<line_deadline:
        remaining=line_deadline-clock()
        if reader:
            value=reader.readline(max(0.001,remaining))
        else:
            try: value=session.readline(timeout=min(line_timeout,remaining))
            except TypeError: value=session.readline()
        if not value: continue
        line=value.decode('ascii',errors='replace').strip() if isinstance(value,bytes) else str(value).strip()
        if line.startswith(('LOOK_', 'SAY_', 'SIMSTIM_SPEAKER ')):
            if line.startswith('LOOK_MOTOR ') and re.fullmatch(r'[A-Za-z0-9_ =-]{1,256}',line): print(line,file=sys.stderr,flush=True)
            return line
        if re.match(r'^(?:\x1b\[[0-9;]*m)?[EW] \(\d+\) (?:esp_video|gc0308|cam_|look_camera|esp_cam|dvp|i2c)',line): print(line[:250],file=sys.stderr,flush=True)
    if deadline is not None and clock() >= deadline:
        raise LookError('survey collection hard deadline exceeded')
    raise LookError('timed out awaiting a device protocol line')


def _session_write(session, payload: bytes) -> None:
    written = session.write(payload)
    if written is not None and written != len(payload):
        raise LookError("short session write")
    flush = getattr(session, "flush", None)
    if flush:
        flush()


SPEECH_HEADER_WAIT_S = 15.0
SPEECH_READY_WAIT_S = 5.0
SPEECH_PCM_WAIT_S = 6.0  # pinned firmware receive window; never extend it on host
SPEECH_RECEIPT_WAIT_S = 20.0
SPEECH_CHATTER_MAX = 16


def _speech_write_pcm(session, pcm: bytes, deadline: float, clock, ensure_active=None) -> None:
    """Send PCM once, in bounded chunks, respecting the firmware receive window."""
    prior_deadline = getattr(session, "command_deadline", None)
    prior_stall = getattr(session, "write_stall_timeout", None)
    if prior_deadline is not None:
        session.command_deadline = min(prior_deadline, deadline)
    if prior_stall is not None:
        session.write_stall_timeout = min(prior_stall, max(0.05, deadline - clock()))
    try:
        for offset in range(0, len(pcm), 4096):
            if ensure_active is not None:
                ensure_active()
            if clock() >= deadline:
                raise LookError("speech PCM transfer deadline exceeded")
            _session_write(session, pcm[offset:offset + 4096])
        if ensure_active is not None:
            ensure_active()
        if clock() >= deadline:
            raise LookError("speech PCM transfer deadline exceeded")
    finally:
        if prior_deadline is not None:
            session.command_deadline = prior_deadline
        if prior_stall is not None:
            session.write_stall_timeout = prior_stall


def _speech_receipt(session, phase: str, deadline: float, clock, ensure_active=None) -> str:
    """Read one ordered speech receipt, skipping only bounded motor telemetry."""
    chatter = 0
    while clock() < deadline:
        if ensure_active is not None:
            ensure_active()
        remaining = deadline - clock()
        # Poll so cancellation/session replacement is noticed promptly, even when
        # the underlying TLS or serial stream otherwise permits long reads.
        try:
            if isinstance(session, ChunkLineReader):
                value = session.readline(min(0.2, remaining))
            else:
                try:
                    value = session.readline(timeout=min(0.2, remaining))
                except TypeError:
                    value = session.readline()
        except TimeoutError:
            continue
        if not value:
            continue
        try:
            line = value.decode("ascii", "strict").strip() if isinstance(value, bytes) else str(value).strip()
        except UnicodeError as exc:
            raise LookError("non-ASCII speech protocol receipt") from exc
        if line == "LOOK_MOTOR" or (line.startswith("LOOK_MOTOR ") and
                re.fullmatch(r"LOOK_MOTOR [A-Za-z0-9_ =-]{1,240}", line)):
            chatter += 1
            if chatter > SPEECH_CHATTER_MAX:
                raise LookError("speech protocol chatter limit exceeded")
            continue
        if line == "SAY_DENIED":
            raise LookError("device denied observation speech")
        error = re.fullmatch(r"SAY_ERROR (header|length|memory|transfer)", line)
        if error:
            raise LookError(f"device speech {error.group(1)} error")
        expected = {"header": "SAY_HEADER", "ready": "SAY_READY"}.get(phase)
        if phase == "receipt":
            if line.startswith("SIMSTIM_SPEAKER "):
                return line
        elif line == expected:
            return line
        # Do not echo protocol lines: future firmware text can contain sensitive
        # content. Unknown, malformed, and out-of-order receipts all fail closed.
        raise LookError(f"unexpected speech receipt during {phase} phase")
    if ensure_active is not None:
        ensure_active()
    raise LookError(f"speech {phase} receipt deadline exceeded")


def load_kokoro_voice():
    """Load and warm the installed offline bm_daniel model before survey motion."""
    root = Path.home() / ".local/share/simstim/voice"
    if not root.is_dir():  # Compatibility with earlier development installs.
        root = Path.home() / ".hermes/cache/scratch/simstim-bm-daniel"
    model, voices = root / "kokoro-v1.0.onnx", root / "voices-v1.0.bin"
    if not model.is_file() or not voices.is_file():
        raise LookError("local Kokoro model/voice files are missing")
    try:
        from kokoro_onnx import Kokoro
        engine = Kokoro(str(model), str(voices))
        # Exercise the local phonemizer/model path before any motion is requested.
        engine.create("Ready.", voice="bm_daniel", lang="en-gb", speed=1.0)
        return engine
    except Exception as e:
        raise LookError(f"could not load/warm local Kokoro bm_daniel en-gb: {e}") from e


def synthesize_observation(engine, text: str) -> bytes:
    """Render a short sentence to bounded mono S16_LE, 16-kHz PCM in memory."""
    if len(text) > 200:
        raise LookError("observation sentence exceeds the short-speech limit")
    try:
        from array import array
        audio, rate = engine.create(text, voice="bm_daniel", lang="en-gb", speed=1.0)
        if int(rate) != 24000 or len(audio) == 0:
            raise LookError("unexpected Kokoro sample rate or empty audio")
        count = max(1, round(len(audio) * 16000 / int(rate)))
        pcm_values = array("h")
        for i in range(count):
            position = i * (len(audio) - 1) / max(1, count - 1)
            left = int(position)
            fraction = position - left
            sample = float(audio[left]) * (1.0 - fraction) + float(audio[min(left + 1, len(audio) - 1)]) * fraction
            pcm_values.append(max(-32768, min(32767, round(max(-1.0, min(1.0, sample)) * 32767.0))))
        if sys.byteorder != "little":
            pcm_values.byteswap()
        pcm = pcm_values.tobytes()
    except LookError:
        raise
    except Exception as e:
        raise LookError(f"local Kokoro synthesis failed: {e}") from e
    if not 0 < len(pcm) <= 320000:
        raise LookError("rendered speech exceeds the 320000-byte PCM limit")
    return pcm


def survey_and_speak(session, engine, elf: Path, expected_sha256: str, started: float | None = None, detector=None,
                     collection_deadline: float | None = None, clock=time.monotonic,
                     before_speech=None, before_survey=None, line_timeout=15,
                     ensure_active=None, operation_deadline: float | None = None) -> dict:
    """Use one already-open session: one choreographed survey, then one reply."""
    verify_board_elf(elf, expected_sha256)
    detector = detector or load_object_detector()
    start = clock() if started is None else started
    deadline = collection_deadline
    if before_survey is not None:
        before_survey()
    _session_write(session, b"LOOK_SURVEY" + bytes((10,)))
    def survey_lines():
        while True:
            if deadline is not None and clock() >= deadline:
                raise LookError("survey collection hard deadline exceeded")
            yield _session_line(session, deadline, clock, line_timeout)
    result = read_survey(survey_lines(), start, detector, deadline, clock)
    if not result['ok'] or result['frames'] != 3 or result['frame_bytes'] != 460800 or [v['glance'] for v in result['views']] != [0,1,2]:
        result['ok'] = False
        return result
    # Record transport completion, never room contents, in service diagnostics.
    print('SURVEY_CAPTURE complete frames=3 bytes=460800',file=sys.stderr,flush=True)
    pcm = synthesize_observation(engine, result["text"])
    if before_speech is not None:
        before_speech()
    if ensure_active is not None:
        ensure_active()
    if operation_deadline is not None and clock() >= operation_deadline:
        raise LookError("observation expired before speech")
    _session_write(session, b"SAY_OBSERVATION" + bytes((10,)))
    header_deadline = clock() + SPEECH_HEADER_WAIT_S
    if operation_deadline is not None:
        header_deadline = min(header_deadline, operation_deadline)
    _speech_receipt(session, "header", header_deadline, clock, ensure_active)
    if ensure_active is not None:
        ensure_active()
    _session_write(session, struct.pack("<II", len(pcm), zlib.crc32(pcm) & 0xffffffff))
    ready_deadline = clock() + SPEECH_READY_WAIT_S
    if operation_deadline is not None:
        ready_deadline = min(ready_deadline, operation_deadline)
    _speech_receipt(session, "ready", ready_deadline, clock, ensure_active)
    if ensure_active is not None:
        ensure_active()
    pcm_deadline = clock() + SPEECH_PCM_WAIT_S
    if operation_deadline is not None:
        pcm_deadline = min(pcm_deadline, operation_deadline)
    _speech_write_pcm(session, pcm, pcm_deadline, clock, ensure_active)
    receipt_deadline = clock() + SPEECH_RECEIPT_WAIT_S
    if operation_deadline is not None:
        receipt_deadline = min(receipt_deadline, operation_deadline)
    receipt = _speech_receipt(session, "receipt", receipt_deadline, clock, ensure_active)
    stereo_bytes = 2 * len(pcm)
    if receipt != f"SIMSTIM_SPEAKER result=played stereo_bytes={stereo_bytes} status=0311 reset_off=1":
        raise LookError("speaker receipt did not confirm exact PCM playback and safe amp state")
    result["speech"] = "played"
    result["stereo_bytes"] = stereo_bytes
    result["total_ms"] = round((time.monotonic() - start) * 1000, 1)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="op", required=True)
    cmd = sub.add_parser("command", help="emit one command after matching-ELF verification")
    cmd.add_argument("command", choices=COMMANDS)
    cmd.add_argument("--elf", required=True, type=Path)
    cmd.add_argument("--elf-sha256", required=True)
    sub.add_parser("analyze-stdin", help="semantically analyze one received frame protocol on stdin")
    sub.add_parser("survey-stdin", help="process one LOOK_SURVEY_BEGIN/END result from stdin")
    args = parser.parse_args(argv)
    try:
        if args.op == "command":
            emit_command(args.command, args.elf, args.elf_sha256)
        elif args.op == "survey-stdin":
            result = read_survey(sys.stdin)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0 if result["ok"] else 1
        else:
            frame = read_frame(sys.stdin)
            print(recognize_scene(frame))
        return 0
    except LookError as e:
        print(f"look_room: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
