#!/usr/bin/env python3
"""Local USB listener for one-shot, reviewed attention-lab status playback.

No network access is used. Stop this foreground listener before running legacy
USB tools, then restart it afterwards; the port is opened exclusively.

Setup (after building the exact standalone image intended for the device):
  sha256sum k151-standalone/build/k151_standalone.elf
  python3 tools/tap_refresh.py --elf-sha256 <lowercase-sha256>
Publish only an intentionally reviewed, public status with:
  python3 tools/tap_refresh.py --publish build-42 'Build complete.'
Exercise the running listener/device exchange from another terminal with:
  python3 tools/tap_refresh.py --check
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ATTENTION = TOOLS.parent / "attention-lab" / "attention_broker.py"
DEFAULT_STATE = Path(os.environ.get("SIMSTIM_ATTENTION_STATE", str(Path.home() / ".local/state/simstim-attention/state.json"))).expanduser()
DEFAULT_PORT = os.environ.get("SIMSTIM_SERIAL_PORT", "/dev/ttyUSB0")
DEFAULT_ELF = Path(os.environ.get("SIMSTIM_RELEASE_ELF_PATH", TOOLS.parent / "k151-standalone/build/k151_standalone.elf")).expanduser()
MAX_AGE = 30 * 60
ID_RE = re.compile(r"[0-9a-f]{8}")


def public_summary(state_path: Path, request_id: str, now: float | None = None) -> tuple[str | None, str | None]:
    """Read current broker state and atomically consume its newest eligible item.

    None is deliberately not persisted: every fetch re-reads broker state, so a
    result published after a negative fetch can be returned on the next fetch.
    """
    now = time.time() if now is None else now
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, None
        if data.get("schema") != 1 or not isinstance(data.get("items"), dict):
            raise ValueError("attention broker state has an unsupported schema")
        candidates = []
        for key, item in data["items"].items():
            created = item.get("created_at")
            if (item.get("reviewed") is True and item.get("status") in ("pending", "cued")
                    and not item.get("tap_consumed_at")
                    and isinstance(created, (int, float)) and not isinstance(created, bool)
                    and 0 <= now - created <= MAX_AGE and isinstance(item.get("summary"), str)):
                candidates.append((created, str(key), item))
        if not candidates:
            return None, None
        _, key, item = max(candidates, key=lambda entry: (entry[0], entry[1]))
        # Never go backwards to an older snapshot after delivering the newest.
        for _, other_key, older in candidates:
            if other_key != key:
                older["status"] = "superseded"
        item["status"] = "retrieved"
        item["retrieved_at"] = now
        item["tap_consumed_at"] = now
        item["tap_request_id"] = request_id
        atomic_json(state_path, data)
        return key, item["summary"]


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def verify_elf(path: Path, expected_sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise ValueError("--elf-sha256 must be the lowercase SHA-256 of the installed ELF")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError("ELF digest mismatch; refusing USB protocol traffic")
    return actual


def spoken_copy(text: str) -> str:
    if not (1 <= len(text.split()) <= 32 and len(text) <= 180 and
            re.fullmatch(r"[A-Za-z][A-Za-z ,.'?!’\-]*", text) and
            len(re.findall(r"[.!?]", text)) <= 2):
        raise ValueError("reviewed summary is not safe short spoken copy")
    return text


def synthesize(engine, text: str) -> bytes:
    from look_room import load_kokoro_voice, synthesize_observation
    if engine is None:
        engine = load_kokoro_voice()
    return synthesize_observation(engine, spoken_copy(text))


def line(stream, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = stream.readline(512)
        if raw:
            return raw.decode("ascii", "strict").strip()
    raise TimeoutError("timed out waiting for USB protocol line")


def expect(stream, *values):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        got = line(stream, max(.01, deadline - time.monotonic()))
        if got in values:
            return got
        if got.startswith(("TAP_DONE ", "TAP_HEADER ", "TAP_READY ")):
            raise RuntimeError("unexpected tap protocol receipt")
    raise TimeoutError("device did not complete tap")


def serve_fetch(stream, request: str, state_path: Path, engine, synth=synthesize, *, progress_source=None) -> str:
    match = re.fullmatch(r"TAP_FETCH ([0-9a-f]{8})", request)
    if not match:
        raise ValueError("malformed TAP_FETCH")
    request_id = match.group(1)
    try:
        key, summary = public_summary(state_path, request_id)
        if summary is None:
            if progress_source is None and state_path == DEFAULT_STATE:
                from work_progress import claim_progress as progress_source
            if progress_source is not None:
                key, summary = progress_source(state_path, request_id)
    except Exception:
        # A failed source lookup must never sound like an empty work queue.
        stream.write(f"TAP_FAIL {request_id}\n".encode()); stream.flush()
        expect(stream, f"TAP_DONE {request_id} failed")
        return "failed"
    if summary is None:
        stream.write(f"TAP_NONE {request_id}\n".encode("ascii"))
        stream.flush()
        if expect(stream, f"TAP_DONE {request_id} none") != f"TAP_DONE {request_id} none":
            raise RuntimeError("invalid TAP_DONE none receipt")
        return "none"
    try:
        pcm = synth(engine, summary)
        if not 320 <= len(pcm) <= 320000 or len(pcm) % 2:
            raise ValueError("invalid PCM size")
    except Exception:
        stream.write(f"TAP_FAIL {request_id}\n".encode("ascii"))
        stream.flush()
        if expect(stream, f"TAP_DONE {request_id} failed") != f"TAP_DONE {request_id} failed":
            raise RuntimeError("invalid TAP_DONE failed receipt")
        return "failed"
    crc = zlib.crc32(pcm) & 0xffffffff
    stream.write(f"TAP_AUDIO {request_id}\n".encode("ascii"))
    stream.flush()
    if expect(stream, f"TAP_HEADER {request_id}") != f"TAP_HEADER {request_id}":
        raise RuntimeError("expected SAY_HEADER after TAP_AUDIO")
    stream.write(struct.pack("<II", len(pcm), crc))
    stream.flush()
    if expect(stream, f"TAP_READY {request_id}") != f"TAP_READY {request_id}":
        raise RuntimeError("expected SAY_READY")
    stream.write(pcm)
    stream.flush()
    done = expect(stream, f"TAP_DONE {request_id} played", f"TAP_DONE {request_id} failed")
    if done not in (f"TAP_DONE {request_id} none", f"TAP_DONE {request_id} failed",
                    f"TAP_DONE {request_id} played"):
        raise RuntimeError("invalid TAP_DONE receipt")
    return done.rsplit(" ", 1)[1]


def local_diagnostic(path: Path, result: dict, stop: threading.Event) -> None:
    """Serve only a filesystem-local Unix socket; no network listener."""
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(path, 0o600)
    sock.listen(2)
    sock.settimeout(.25)
    try:
        while not stop.is_set():
            try:
                client, _ = sock.accept()
            except socket.timeout:
                continue
            with client:
                request = client.recv(128).decode("ascii", "replace").strip()
                if request == "CHECK":
                    result["check_done"].clear()
                    result.pop("check_result", None)
                    result["check_requested"].set()
                    result["check_done"].wait(35)
                    client.sendall((result.get("check_result", "TIMEOUT") + "\n").encode())
                else:
                    client.sendall(b"ERROR\n")
    finally:
        sock.close()
        path.unlink(missing_ok=True)


def diagnostic_request(path: Path) -> int:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(38)
        client.connect(str(path))
        client.sendall(b"CHECK\n")
        response = client.recv(128).decode("ascii", "replace").strip()
    print(response)
    return 0 if response in ("TAP_CHECK played", "TAP_CHECK none") else 1


def run(args) -> int:
    from serial import Serial
    from look_room import load_kokoro_voice

    # Import and warm Kokoro before acquiring the exclusive USB port.
    engine = load_kokoro_voice()
    elf_digest = verify_elf(Path(args.elf), args.elf_sha256)
    serial = Serial(None, 115200, exclusive=True, timeout=.15, write_timeout=5)
    serial.dtr = False
    serial.rts = False
    serial.port = args.port
    stop = threading.Event()
    control = {"check_requested": threading.Event(), "check_done": threading.Event()}
    sock_path = Path(args.control_socket)
    worker = threading.Thread(target=local_diagnostic, args=(sock_path, control, stop), daemon=True)
    worker.start()
    checking = False
    try:
        with serial:
            banner_re = re.compile(r"SIMSTIM_STANDALONE sha256=" + elf_digest + r" uptime_s=\d+ board_ready=1")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                received = line(serial, max(.2, deadline - time.monotonic()))
                if banner_re.fullmatch(received):
                    break
            else:
                raise TimeoutError("matching standalone ELF banner not observed")
            while True:
                if control["check_requested"].is_set():
                    control["check_requested"].clear()
                    control["check_done"].clear()
                    checking = True
                    serial.write(b"TAP_CHECK\n")
                    serial.flush()
                raw = serial.readline(512)
                if not raw:
                    continue
                received = raw.decode("ascii", "strict").strip()
                check = re.fullmatch(r"TAP_CHECK ([0-9a-f]{8})", received)
                if check:
                    control["check_result"] = received
                    control["check_done"].set()
                    continue
                if received.startswith("TAP_FETCH "):
                    if not re.fullmatch(r"TAP_FETCH [0-9a-f]{8}", received):
                        print("tap_refresh: ignored malformed TAP_FETCH", file=sys.stderr, flush=True)
                        continue
                    outcome = serve_fetch(serial, received, Path(args.state), engine)
                    print(f"TAP_DONE {outcome}", flush=True)
                    if checking:
                        checking = False
                        control["check_result"] = "TAP_CHECK " + outcome
                        control["check_done"].set()
    except TimeoutError:
        return 2
    finally:
        stop.set()
        worker.join(timeout=1)
        sock_path.unlink(missing_ok=True)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--elf", type=Path, default=DEFAULT_ELF)
    p.add_argument("--elf-sha256", required=False, default=os.environ.get("SIMSTIM_ELF_SHA256"))
    p.add_argument("--control-socket", type=Path, default=Path.home() / ".local/state/simstim-tap-refresh/control.sock")
    p.add_argument("--publish", nargs=2, metavar=("ID", "PUBLIC_SUMMARY"), help="publish through attention-lab put --reviewed")
    p.add_argument("--check", action="store_true", help="request TAP_CHECK through the local running listener")
    args = p.parse_args(argv)
    if args.check:
        return diagnostic_request(args.control_socket)
    if args.publish:
        ident, summary = args.publish
        spoken_copy(summary)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", ident):
            p.error("publish ID must be 1-64 safe lowercase identifier characters")
        import subprocess
        return subprocess.run([sys.executable, str(ATTENTION), "--state", str(args.state), "put",
                               "--id", ident, "--public-summary", summary, "--reviewed"], check=False).returncode
    if not args.elf_sha256:
        p.error("listener requires --elf-sha256 (or SIMSTIM_ELF_SHA256)")
    try:
        return run(args)
    except Exception as exc:
        print(f"tap_refresh: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
