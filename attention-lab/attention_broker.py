#!/usr/bin/env python3
"""Host-only pending-result attention broker; no device I/O."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_STATE = Path(os.environ.get("SIMSTIM_ATTENTION_STATE", str(Path.home() / ".local/state/simstim-attention/state.json"))).expanduser()
MAX_AGE = 120.0
MAX_SUMMARY = 500


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != 1 or not isinstance(data.get("items"), dict):
            raise ValueError("invalid state schema")
        return data
    except FileNotFoundError:
        return {"schema": 1, "items": {}, "seen_events": {}}


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".attention-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _event_id(event):
    # Upstream does not provide an event UUID; this identifies a replay by
    # the complete immutable source event tuple.
    return "|".join(str(event.get(k, "")) for k in
                    ("event_type", "subtype", "duration_ms", "ts", "ts_unix", "session_id"))


def run(args):
    state_path = Path(args.state).expanduser()
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load(state_path)
        now = args.now if args.now is not None else time.time()
        if args.command == "put":
            summary = args.public_summary.strip()
            if not summary or len(summary) > MAX_SUMMARY or "\n" in summary:
                print("error: public summary must be 1–500 characters on one line", file=sys.stderr)
                return 2
            key = args.id.strip()
            if not key:
                print("error: --id is required", file=sys.stderr); return 2
            item = data["items"].get(key)
            candidate = {"summary": summary, "status": "pending", "created_at": now,
                         "cue_event": None, "retrieved_at": None, "reviewed": bool(args.reviewed)}
            if item:
                if item["summary"] != summary:
                    print("error: idempotency key already used for a different result", file=sys.stderr)
                    return 2
                print(json.dumps({"status": "already_exists", "id": key})); return 0
            data["items"][key] = candidate
            _save(state_path, data)
            print(json.dumps({"status": "stored", "id": key})); return 0

        if args.command == "claim-cue":
            key = args.id.strip()
            item = data["items"].get(key)
            if not item or item.get("reviewed") is not True or item.get("status") != "pending":
                print(json.dumps({"status": "rejected", "reason": "absent_duplicate_or_unreviewed"})); return 2
            created = item.get("created_at")
            if not isinstance(created, (int, float)) or isinstance(created, bool) or now-created > MAX_AGE or created > now+2:
                print(json.dumps({"status": "rejected", "reason": "stale"})); return 2
            newer = any(v.get("status") == "pending" and isinstance(v.get("created_at"), (int, float)) and
                        v["created_at"] > created for other, v in data["items"].items() if other != key)
            if newer:
                print(json.dumps({"status": "rejected", "reason": "superseded"})); return 2
            item["status"] = "cued"
            item["cued_at"] = now
            item["cue_event"] = "producer:" + key
            item["cue_session_id"] = args.session_id
            _save(state_path, data)
            print(json.dumps({"status": "claimed", "id": key})); return 0

        if args.command == "retry":
            key = args.id.strip()
            item = data["items"].get(key)
            if not item or item["status"] != "retrieved":
                print(json.dumps({"status": "unchanged", "reason": "item_not_claimed", "id": key})); return 2
            # Deliberate operator action only; never invoked by the consumer.
            item["status"] = "pending"
            item["cue_event"] = None
            item["retrieved_at"] = None
            # A deliberate retry authorizes one fresh consumer claim. Remove
            # every prior tap receipt so the real tap reader can see it again.
            for marker in ("tap_consumed_at", "tap_request_id", "request_event",
                           "cued_at", "cue_session_id"):
                item.pop(marker, None)
            _save(state_path, data)
            print(json.dumps({"status": "pending_for_deliberate_retry", "id": key})); return 0

        if args.command == "nearby":
            e = json.loads(Path(args.event_file).read_text(encoding="utf-8"))
            if (e.get("event_type") != "nearby" or e.get("affirmative") is not True or
                not isinstance(e.get("session_id"), str) or not e["session_id"]):
                print(json.dumps({"status": "quiet", "reason": "unknown_or_nonaffirmative"})); return 0
            ts = e.get("ts_unix")
            eid = _event_id(e)
            if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts > now + 2 or now-ts > MAX_AGE:
                print(json.dumps({"status": "quiet", "reason": "stale_or_invalid"})); return 0
            if eid in data.get("seen_events", {}):
                print(json.dumps({"status": "quiet", "reason": "replay"})); return 0
            data.setdefault("seen_events", {})[eid] = now
            eligible = next((k for k, v in data["items"].items() if v["status"] == "pending" and v.get("reviewed") is True), None)
            quiet = args.quiet or not args.attentive or args.unknown_context
            if quiet or eligible is None:
                _save(state_path, data)
                why = "quiet_condition" if quiet else "no_pending_result"
                print(json.dumps({"status": "quiet", "reason": why})); return 0
            item = data["items"][eligible]
            if item["cue_event"] is not None:
                _save(state_path, data)
                print(json.dumps({"status": "quiet", "reason": "already_cued"})); return 0
            item["cue_event"] = eid
            item["cued_at"] = now
            item["status"] = "cued"
            item["cue_session_id"] = e["session_id"]
            _save(state_path, data)
            # Cue content is fixed and contains no result/personal information.
            print(json.dumps({"status": "cue", "cue": "one discreet visual cue", "id": eligible}))
            return 0

        if args.command == "tap":
            e = json.loads(Path(args.event_file).read_text(encoding="utf-8"))
            # Match upstream stackchan-event JSONL envelope, including both
            # firmware uptime ts and gateway wall-clock ts_unix/session_id.
            if (e.get("event_type") != "touch" or e.get("subtype") != "tap" or
                not isinstance(e.get("session_id"), str) or not e["session_id"] or
                not isinstance(e.get("ts"), int) or isinstance(e.get("ts"), bool) or
                not isinstance(e.get("duration_ms"), int) or isinstance(e.get("duration_ms"), bool) or
                e["duration_ms"] < 0):
                print(json.dumps({"status": "quiet", "reason": "unknown_event"})); return 0
            ts = e.get("ts_unix")
            eid = _event_id(e)
            if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts > now + 2 or now-ts > MAX_AGE:
                print(json.dumps({"status": "quiet", "reason": "stale_or_invalid"})); return 0
            if eid in data.get("seen_events", {}):
                print(json.dumps({"status": "quiet", "reason": "replay"})); return 0
            data.setdefault("seen_events", {})[eid] = now
            eligible = next(((k, v) for k, v in data["items"].items()
                             if v["status"] == "cued" and v.get("reviewed") is True and
                             v.get("cue_session_id") == e["session_id"] and
                             isinstance(v.get("cued_at"), (int, float)) and 0 <= ts-v["cued_at"] <= MAX_AGE and
                             isinstance(v.get("created_at"), (int, float)) and 0 <= ts-v["created_at"] <= MAX_AGE), None)
            if args.quiet or not args.attentive or args.unknown_context or eligible is None:
                _save(state_path, data)
                print(json.dumps({"status": "quiet", "reason": "quiet_condition" if args.quiet or not args.attentive or args.unknown_context else "no_pending_result"})); return 0
            key, item = eligible
            # Tap-only policy: the tap requests this approved public status.
            # Claim atomically before any synthesis/delivery to prevent replay.
            item["status"] = "retrieved"
            item["retrieved_at"] = now
            item["request_event"] = eid
            _save(state_path, data)
            print(json.dumps({"status": "retrieved", "id": key, "public_summary": item["summary"]}, ensure_ascii=False))
            return 0
    return 0


def main():
    p = argparse.ArgumentParser(description="Host-only pending-result attention broker (no hardware/audio)")
    p.add_argument("--state", default=str(DEFAULT_STATE), help="persistent state JSON path")
    p.add_argument("--now", type=float, help=argparse.SUPPRESS, default=None)
    sub = p.add_subparsers(dest="command", required=True)
    put = sub.add_parser("put", help="store a non-sensitive completed public result")
    put.add_argument("--id", required=True, help="stable idempotency key")
    put.add_argument("--public-summary", required=True)
    put.add_argument("--reviewed", action="store_true", help="operator attests this is public and reviewed")
    claim = sub.add_parser("claim-cue", help="atomically claim one fresh reviewed item for a cue")
    claim.add_argument("--id", required=True)
    claim.add_argument("--session-id", required=True)
    retry = sub.add_parser("retry", help="explicitly re-queue a claimed item after operator review")
    retry.add_argument("--id", required=True)
    for name in ("nearby", "tap"):
        q = sub.add_parser(name, help=f"process synthetic {name} event JSON")
        q.add_argument("--event-file", required=True)
        q.add_argument("--quiet", action="store_true", help="quiet condition is active")
        q.add_argument("--not-attentive", dest="attentive", action="store_false", help="attention unavailable")
        q.add_argument("--unknown-context", action="store_true", help="context is unknown")
        q.set_defaults(attentive=True)
    args = p.parse_args()
    try:
        return run(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
