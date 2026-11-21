#!/usr/bin/env python3
"""Open-vocabulary ASR on generated firmware PCM and half-volume I2S PCM."""
from __future__ import annotations
import argparse
import pathlib
import struct
import wave

CUES = (("online", "Online"), ("no_update", "No update"), ("check_failed", "Check failed"))
RATE = 16000


def wav(path: pathlib.Path, pcm: bytes, channels: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels); stream.setsampwidth(2); stream.setframerate(RATE); stream.writeframes(pcm)


def halve_stereo(pcm: bytes) -> bytes:
    samples = struct.unpack("<" + "h" * (len(pcm)//2), pcm)
    def half(sample: int) -> int:
        return (abs(sample)//2) * (-1 if sample < 0 else 1)  # C signed division
    return struct.pack("<" + "h" * (len(samples)*2), *(v for s in samples for v in (half(s), half(s))))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clips", type=pathlib.Path, required=True)
    parser.add_argument("--model", required=True, help="local faster-whisper model name or path")
    args = parser.parse_args()
    from faster_whisper import WhisperModel
    import numpy as np
    model = WhisperModel(args.model, device="cpu", compute_type="int8", local_files_only=True)
    ok = True
    for name, expected in CUES:
        pcm = (args.clips / f"{name}.pcm").read_bytes()
        speaker = halve_stereo(pcm)
        raw_path = args.clips / f"{name}-firmware-input.wav"
        speaker_path = args.clips / f"{name}-speaker-half-volume.wav"
        wav(raw_path, pcm, 1); wav(speaker_path, speaker, 2)
        for label, path in (("firmware", raw_path), ("speaker", speaker_path)):
            with wave.open(str(path), "rb") as stream:
                channels = stream.getnchannels()
                frames = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
            if channels == 2:
                frames = frames.reshape(-1, 2).mean(axis=1)
            segments, _ = model.transcribe(frames, beam_size=5, language="en", vad_filter=False)
            text = " ".join(part.text.strip() for part in segments).strip()
            print(f"{expected}: {label}={text!r}")
            ok &= expected.casefold() in text.casefold()
    if not ok:
        print("Open-vocabulary ASR acceptance failed")
        return 1
    print("Open-vocabulary ASR passed for firmware PCM and half-volume stereo speaker path")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
