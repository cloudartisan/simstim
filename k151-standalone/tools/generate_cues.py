#!/usr/bin/env python3
"""Render pinned Kokoro fixed prompts to build-local PCM and firmware C data."""
from __future__ import annotations
import argparse
import hashlib
import pathlib
import struct
import wave

CUES = (("online", "Online"), ("no_update", "No update"), ("check_failed", "Check failed"))
RATE = 16000
PINNED_SHA256 = {
    "model": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}


def check_asset(path: pathlib.Path, kind: str) -> None:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != PINNED_SHA256[kind]:
        raise RuntimeError(f"unexpected {kind} SHA-256 for {path}: {digest}")


def render(model_path: pathlib.Path, voices_path: pathlib.Path) -> list[tuple[str, str, bytes]]:
    from kokoro_onnx import Kokoro
    import numpy as np
    engine = Kokoro(str(model_path), str(voices_path))
    rendered = []
    for name, text in CUES:
        audio, source_rate = engine.create(text, voice="bm_daniel", speed=1.0, lang="en-gb")
        if source_rate != RATE:
            duration = len(audio) / source_rate
            target_len = round(duration * RATE)
            audio = np.interp(np.arange(target_len) / RATE,
                              np.arange(len(audio)) / source_rate, audio)
        audio = np.asarray(audio, dtype=np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak < 0.01:
            raise RuntimeError(f"Kokoro returned silence for {text!r}")
        # Leave headroom; quantization and speaker.c's explicit /2 are fixed.
        audio = np.clip(audio * min(0.88 / peak, 1.0), -1.0, 1.0)
        pcm = np.rint(audio * 32767.0).astype("<i2").tobytes()
        pcm = pcm[:len(pcm) & ~1]
        if len(pcm) < RATE // 5 * 2:
            raise RuntimeError(f"Kokoro clip is unexpectedly short: {text!r}")
        rendered.append((name, text, pcm))
    return rendered


def emit(out: pathlib.Path, clips: list[tuple[str, str, bytes]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    header = ["/* Generated in build directory; do not edit. */", "#pragma once", "#include <stddef.h>", "#include <stdint.h>"]
    source = ['/* Generated in build directory; do not edit. */', '#include "status_cues_data.h"']
    for name, text, pcm in clips:
        samples = struct.unpack("<" + "h" * (len(pcm)//2), pcm)
        header += [f"extern const int16_t cue_{name}[{len(samples)}];", f"#define CUE_{name.upper()}_SAMPLES {len(samples)}u"]
        source += [f"const int16_t cue_{name}[{len(samples)}] = {{"]
        for start in range(0, len(samples), 16):
            source.append("    " + ", ".join(map(str, samples[start:start+16])) + ",")
        source += ["};"]
        with wave.open(str(out / f"{name}.wav"), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE); wav.writeframes(pcm)
        (out / f"{name}.pcm").write_bytes(pcm)
    (out / "status_cues_data.h").write_text("\n".join(header) + "\n")
    (out / "status_cues_data.c").write_text("\n".join(source) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=pathlib.Path, required=True)
    parser.add_argument("--voices", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    check_asset(args.model, "model")
    check_asset(args.voices, "voices")
    emit(args.out, render(args.model, args.voices))
    print(f"Generated {len(CUES)} Kokoro bm_daniel cues at {RATE} Hz in {args.out}")

if __name__ == "__main__":
    main()
