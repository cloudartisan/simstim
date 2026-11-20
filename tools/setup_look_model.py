#!/usr/bin/env python3
"""Fetch the official YOLOX-Nano ONNX artifact into the host model cache."""
from __future__ import annotations
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

if __package__:
    from .local_objects import DEFAULT_MODEL, MODEL_URL, load_detector
else:
    from local_objects import DEFAULT_MODEL, MODEL_URL, load_detector


def main() -> int:
    target = DEFAULT_MODEL
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "SIMSTIM-local-object-setup/1"})
        fd, temp_name = tempfile.mkstemp(prefix="yolox-nano-", suffix=".onnx", dir=target.parent)
        os.close(fd)
        temp = Path(temp_name)
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temp.open("wb") as out:
                if response.geturl().split("/")[2] != "release-assets.githubusercontent.com":
                    raise RuntimeError("model download did not redirect to GitHub release assets")
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 100_000_000:
                        raise RuntimeError("unexpected oversized model artifact")
                    out.write(chunk)
            if total < 100_000:
                raise RuntimeError("downloaded ONNX model is unexpectedly small")
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    detector = load_detector(target)
    print(f"model=YOLOX-Nano provider=CPUExecutionProvider sha256={digest} path={target}")
    del detector
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
