#!/usr/bin/env python3
"""Parse the standalone firmware's single-line USB progress record."""

from __future__ import annotations

import re
from typing import TypedDict


_RECORD = re.compile(
    r"^SIMSTIM_STANDALONE sha256=([0-9a-f]{64}) "
    r"uptime_s=(0|[1-9][0-9]*) board_ready=([01])$"
)


class Progress(TypedDict):
    sha256: str
    uptime_s: int
    board_ready: bool


def parse_progress(line: str) -> Progress:
    """Parse exactly one firmware progress line; reject malformed/foreign input."""
    match = _RECORD.fullmatch(line.strip())
    if match is None:
        raise ValueError("not a valid SIMSTIM_STANDALONE progress record")
    sha256, uptime, ready = match.groups()
    return {
        "sha256": sha256,
        "uptime_s": int(uptime),
        "board_ready": ready == "1",
    }


if __name__ == "__main__":
    import json
    import sys

    try:
        print(json.dumps(parse_progress(sys.stdin.readline()), sort_keys=True))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
