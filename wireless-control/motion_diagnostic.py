"""Strict parser for the private, one-shot LOOK_MOTION diagnostic protocol."""
from __future__ import annotations

import re


NONCE_RE = re.compile(r"[0-9a-f]{16}")
_EVENT_PREFIX = "LOOK_MOTOR result=event action=survey "
_ORDER = ("start", "centre", "view1", "view2", "return")


class MotionDiagnosticError(ValueError):
    """The device response is malformed, incomplete, or unsafe to accept."""


def _fields(line: str, prefix: str) -> dict[str, str]:
    if not line.startswith(prefix):
        raise MotionDiagnosticError("unexpected motion diagnostic record")
    fields: dict[str, str] = {}
    for token in line[len(prefix):].split(" "):
        if not token or "=" not in token:
            raise MotionDiagnosticError("malformed motion diagnostic field")
        key, value = token.split("=", 1)
        if not re.fullmatch(r"[a-z_]+", key) or not value or key in fields:
            raise MotionDiagnosticError("duplicate or malformed motion diagnostic field")
        fields[key] = value
    return fields


def _int(fields: dict[str, str], key: str, *, minimum: int, maximum: int) -> int:
    value = fields.get(key, "")
    if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        raise MotionDiagnosticError(f"invalid {key}")
    number = int(value)
    if not minimum <= number <= maximum:
        raise MotionDiagnosticError(f"{key} out of range")
    return number


def _raw_pair(fields: dict[str, str], prefix: str, *, require_present: bool = False) -> dict[str, int]:
    yaw = _int(fields, f"{prefix}_yaw_raw", minimum=-1, maximum=65535)
    pitch = _int(fields, f"{prefix}_pitch_raw", minimum=-1, maximum=65535)
    if require_present and not (0 <= yaw <= 1000 and 0 <= pitch <= 1000):
        raise MotionDiagnosticError(f"incomplete {prefix} raw pair")
    return {f"yaw_raw": yaw, f"pitch_raw": pitch}


def parse_motion_diagnostic(lines: list[str], nonce: str) -> dict:
    """Validate terminal and survey events; return raw evidence, no media data."""
    if not NONCE_RE.fullmatch(nonce):
        raise MotionDiagnosticError("invalid request nonce")
    events: list[dict] = []
    terminal = None
    run_id = None
    if not lines or lines[0] != f"MOTION_BEGIN {nonce}":
        raise MotionDiagnosticError("missing matching motion begin")
    for line in lines[1:]:
        if terminal is not None:
            raise MotionDiagnosticError("unexpected data after motion terminal")
        if line.startswith(_EVENT_PREFIX):
            f = _fields(line, _EVENT_PREFIX)
            point = f.get("point")
            state = f.get("state")
            if point in _ORDER:
                expected = {"run_id", "point", "state", "measured_yaw_raw", "measured_pitch_raw"}
                expected |= {"requested_yaw_raw", "requested_pitch_raw"}
                if set(f) != expected or state not in {"observed", "settled", "failed"}:
                    raise MotionDiagnosticError("malformed survey point event")
                this_run = _int(f, "run_id", minimum=1, maximum=0xFFFFFFFF)
                if run_id is None:
                    run_id = this_run
                if this_run != run_id:
                    raise MotionDiagnosticError("survey run changed")
                measured = _raw_pair(f, "measured", require_present=state != "failed")
                requested = _raw_pair(f, "requested", require_present=point != "start" and state != "failed")
                if point == "start":
                    if state != "observed" or tuple(requested.values()) != (-1, -1):
                        raise MotionDiagnosticError("start must be an observation without a target")
                    requested = None
                elif state == "observed":
                    raise MotionDiagnosticError("waypoint was not settled")
                elif state == "settled" and any(abs(measured[k]-requested[k]) > 9 for k in measured):
                    raise MotionDiagnosticError("settled position outside firmware tolerance")
                events.append({"point": point, "state": state, "run_id": this_run,
                               "requested": requested, "measured": measured})
            elif point == "cleanup":
                expected = {"run_id", "point", "state", "return", "idle", "torque_release", "rail_off"}
                if set(f) != expected or state not in {"verified", "failed"}:
                    raise MotionDiagnosticError("malformed cleanup event")
                this_run = _int(f, "run_id", minimum=1, maximum=0xFFFFFFFF)
                if run_id is None:
                    run_id = this_run
                if this_run != run_id:
                    raise MotionDiagnosticError("cleanup run changed")
                if f["return"] not in {"verified", "failed", "not_attempted"} or f["idle"] not in {"verified_before_release", "unverified"} or f["torque_release"] not in {"verified", "failed"} or f["rail_off"] not in {"verified", "failed"}:
                    raise MotionDiagnosticError("invalid cleanup result")
                events.append({"point": "cleanup", "state": state, "run_id": this_run,
                               "return": f["return"], "idle": f["idle"],
                               "torque_release": f["torque_release"], "rail_off": f["rail_off"]})
            else:
                raise MotionDiagnosticError("unknown survey point")
        elif line.startswith("MOTION_END "):
            if terminal is not None:
                raise MotionDiagnosticError("duplicate motion terminal")
            m = re.fullmatch(r"MOTION_END ([0-9a-f]{16}) ok=([01])", line)
            if not m:
                raise MotionDiagnosticError("malformed motion terminal")
            if m.group(1) != nonce:
                raise MotionDiagnosticError("motion terminal nonce mismatch")
            terminal = m.group(2) == "1"
        elif line.startswith("LOOK_ERROR"):
            raise MotionDiagnosticError("firmware rejected motion diagnostic")
        elif line.startswith("LOOK_MOTOR result=event action=survey"):
            raise MotionDiagnosticError("malformed survey event")
        else:
            # Bounded LOOK_MOTOR telemetry may interleave with the event records.
            if not line.startswith("LOOK_MOTOR "):
                raise MotionDiagnosticError("unexpected motion diagnostic response")
    if terminal is None:
        raise MotionDiagnosticError("motion diagnostic ended without matching terminal")
    if not terminal:
        return {"ok": False, "nonce": nonce, "run_id": run_id, "events": events}
    if [event["point"] for event in events] != [*_ORDER, "cleanup"]:
        raise MotionDiagnosticError("incomplete or out-of-order successful survey")
    if any(event["state"] == "failed" for event in events[:5]):
        raise MotionDiagnosticError("successful survey contains a failed point")
    cleanup = events[-1]
    if (cleanup["state"] != "verified" or cleanup["return"] != "verified" or
            cleanup["idle"] != "verified_before_release" or
            cleanup["torque_release"] != "verified" or cleanup["rail_off"] != "verified"):
        raise MotionDiagnosticError("successful survey cleanup was not fully verified")
    return {"ok": True, "nonce": nonce, "run_id": run_id, "events": events}
