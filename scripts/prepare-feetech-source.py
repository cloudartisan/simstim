#!/usr/bin/env python3
"""Fetch or verify the pinned Feetech upstream source and extract WritePos."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.request

LOCK = Path(__file__).resolve().parents[1] / "k151-standalone" / "feetech-source.lock"


def load_lock(path: Path = LOCK) -> dict[str, str]:
    lock = json.loads(path.read_text(encoding="utf-8"))
    if set(lock) != {"url", "commit", "sha256", "filename"}:
        raise ValueError("invalid Feetech source lock fields")
    if not re.fullmatch(r"[0-9a-f]{40}", lock["commit"]):
        raise ValueError("upstream commit must be a full immutable SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", lock["sha256"]):
        raise ValueError("upstream source hash must be a full SHA-256")
    if lock["commit"] not in lock["url"]:
        raise ValueError("upstream URL must include the pinned commit")
    if Path(lock["filename"]).name != lock["filename"]:
        raise ValueError("unsafe upstream filename")
    return lock


def verify_source(data: bytes, expected: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"upstream source SHA-256 mismatch: expected {expected}, got {actual}")


def extract_write_pos(text: str) -> str:
    signature = "int FeetechScs::WritePos(uint8_t id, uint16_t position, uint16_t time_ms, uint16_t speed)"
    start = text.find(signature)
    if start < 0:
        raise ValueError("pinned upstream source has no expected WritePos signature")
    brace = text.find("{", start + len(signature))
    if brace < 0:
        raise ValueError("WritePos definition has no body")
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1].strip() + "\n"
    raise ValueError("WritePos definition has an unterminated body")


def prepare(destination: Path, mirror: Path | None, offline: bool) -> None:
    lock = load_lock()
    if offline and mirror is None:
        raise ValueError("offline mode requires --mirror with the verified upstream source file")
    if mirror is not None:
        source_path = mirror / lock["filename"] if mirror.is_dir() else mirror
        data = source_path.read_bytes()
        verify_source(data, lock["sha256"])
    else:
        request = urllib.request.Request(lock["url"], headers={"User-Agent": "simstim-pinned-source-preparer/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
        verify_source(data, lock["sha256"])
    extracted = extract_write_pos(data.decode("utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    notice = (LOCK.parent / "main/vendor/feetech/LICENSE").read_text(encoding="utf-8")
    temporary.write_text("/*\n" + notice.rstrip() + "\n*/\n" + extracted, encoding="utf-8", newline="\n")
    os.replace(temporary, destination)
    print(f"Verified {lock['url']} (sha256 {lock['sha256']}); prepared {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--mirror", type=Path, help="upstream source file or directory containing it")
    parser.add_argument("--offline", action="store_true", help="refuse network access; requires --mirror")
    args = parser.parse_args()
    try:
        prepare(args.destination, args.mirror, args.offline)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"prepare-feetech-source: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
