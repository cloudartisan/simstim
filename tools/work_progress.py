"""Room-safe fallback speech based only on recent Kanban transitions.

The Kanban database is opened read-only. No task titles, prose, summaries,
comments, or customer data are selected or emitted. Progress claims are
consumed in a small atomic ledger beside the attention broker state.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

FRESH_SECONDS = 24 * 60 * 60
TARGET_STATUSES = frozenset({"done", "review", "blocked"})


def _default_db_path() -> Path:
    """The shared Hermes default-board DB (not the current named board)."""
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    return root / "kanban.db"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_events(db_path: Path, now: float) -> list[tuple[int, str]]:
    if not db_path.is_file():
        raise FileNotFoundError("default Kanban database is unavailable")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        conn.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "task_events" not in tables:
            raise sqlite3.DatabaseError("default Kanban event table is unavailable")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_events)")}
        required = {"id", "kind", "payload", "created_at"}
        if not required.issubset(columns):
            raise sqlite3.DatabaseError("default Kanban event schema is unsupported")
        # Select only event identity, kind, timestamp, and the status enum from
        # status events. Never select task IDs or free-form payload text.
        rows = conn.execute(
            """
            SELECT id, kind, created_at,
                   CASE WHEN kind = 'status' AND json_valid(payload)
                        THEN json_extract(payload, '$.status') END AS target
              FROM task_events
             WHERE created_at >= ? AND created_at <= ?
               AND (kind IN ('completed', 'review_requested', 'blocked')
                    OR (kind = 'status' AND json_valid(payload)
                        AND json_extract(payload, '$.status') IN ('done','review','blocked')))
             ORDER BY id
            """,
            (int(now - FRESH_SECONDS), int(now)),
        ).fetchall()
        events: list[tuple[int, str]] = []
        for event_id, kind, _created, target in rows:
            category = target if kind == "status" else {
                "completed": "done",
                "review_requested": "review",
                "blocked": "blocked",
            }.get(kind)
            if category in TARGET_STATUSES:
                events.append((int(event_id), category))
        return events
    finally:
        conn.close()


def _service_events() -> list[tuple[int, str]]:
    import socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect('/run/simstim-progress.sock')
        with sock.makefile('rb') as stream:
            raw = stream.readline(131073)
    if not raw.endswith(b'\n') or len(raw) > 131072:
        raise ValueError('incomplete progress projection')
    rows = json.loads(raw).get('events')
    if (not isinstance(rows, list) or len(rows) > 4096 or
            not all(isinstance(r, list) and len(r) == 2 and type(r[0]) is int and
                    r[0] > 0 and r[1] in TARGET_STATUSES for r in rows)):
        raise ValueError('unavailable or invalid progress projection')
    return [(i, category) for i, category in rows]


def _spoken(categories: list[str]) -> str:
    counts = {name: categories.count(name) for name in TARGET_STATUSES}
    pieces: list[str] = []
    if counts["done"]:
        pieces.append("finished a task" if counts['done'] == 1 else "recorded several completions")
    if counts["review"]:
        pieces.append("sent work for review")
    if counts["blocked"]:
        pieces.append("reported a blocker" if counts['blocked'] == 1 else "reported blockers")
    # Report what happened, not whether an earlier blocker is still open.
    clauses = ', '.join(pieces[:-1]) + ' and ' + pieces[-1] if len(pieces) > 1 else pieces[0]
    text = 'In the past day the team ' + clauses + '.'
    if not (1 <= len(text.split()) <= 32 and len(text) <= 180
            and re.fullmatch(r"[A-Za-z][A-Za-z ,.'?!’\-]*", text)
            and len(re.findall(r"[.!?]", text)) <= 2):
        raise ValueError("generated progress speech violates the tap spoken-copy limits")
    return text


def _atomic_json(path: Path, data: dict) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temp.unlink(missing_ok=True)


def claim_progress(state_path: Path, request_id: str) -> tuple[str | None, str | None]:
    """Claim fresh, previously unspoken work transitions for one tap fetch.

    Errors opening or interpreting the configured local source propagate so
    callers can distinguish an unavailable source from an empty work queue.
    """
    del request_id  # Event identity, rather than tap identity, provides dedupe.
    state_path = Path(state_path)
    ledger = state_path.with_name(state_path.name + ".work-progress.json")
    lock_path = state_path.with_name(state_path.name + ".work-progress.lock")
    with _locked(lock_path):
        now = time.time()
        if state_path == Path.home() / '.local/state/simstim-attention/state.json':
            events = _service_events()
        else:
            events = _read_events(_default_db_path(), now)
        try:
            state = json.loads(ledger.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"schema": 1, "event_ids": []}
        if not isinstance(state, dict) or state.get("schema") != 1 or not isinstance(state.get("event_ids"), list):
            raise ValueError("work progress ledger has an unsupported schema")
        seen = {int(value) for value in state["event_ids"] if isinstance(value, int) and not isinstance(value, bool)}
        fresh = [(event_id, category) for event_id, category in events if event_id not in seen]
        if not fresh:
            return None, None
        event_ids = [event_id for event_id, _ in fresh]
        key = "progress-" + hashlib.sha256(",".join(map(str, event_ids)).encode("ascii")).hexdigest()[:16]
        speech = _spoken([category for _, category in fresh])
        # Retain only IDs still inside the freshness window plus this claim.
        active_ids = {event_id for event_id, _ in events}
        state = {"schema": 1, "event_ids": sorted((seen & active_ids) | set(event_ids))}
        _atomic_json(ledger, state)
        return key, speech
