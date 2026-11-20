# Host-side pending-result attention broker

A small, local-only CLI prototype. It never connects to a device, monitors a room, or invokes speech/audio. Results are printed only after an explicit tap and only once. Treat the `--public-summary` input as intentionally reviewed, non-sensitive text; the broker does not infer whether arbitrary content is private.

## Integration command

From this directory, run:

```sh
python3 -m unittest -v
```

The suite launches the CLI in separate processes against persisted temporary state, tests idempotent result storage, cue-once behavior, tap-once retrieval, stale/replay/unknown and quiet paths, and reads a synthetic JSONL touch event with the documented fields (`event_type`, `subtype`, `duration_ms`, `ts`, `ts_unix`, `session_id`). All event fixtures are synthetic; no sensor, room monitoring, network, or physical hardware is used.

## Manual host-only flow

```sh
STATE=$(mktemp -u)
printf '%s\n' '{"event_type":"nearby","affirmative":true,"session_id":"demo-session","ts_unix":2000000000,"ts":1}' > /tmp/attention-nearby.json
python3 attention_broker.py --state "$STATE" --now 2000000000 put --id build-42 --public-summary 'Build complete; 12 checks passed.'
python3 attention_broker.py --state "$STATE" --now 2000000000 nearby --event-file /tmp/attention-nearby.json
```

Then provide an upstream-shaped tap JSONL event to `tap --event-file ...`; the integration test demonstrates this with a synthetic JSONL record. In production use the default state path `~/.local/state/simstim-attention/state.json`, or set `--state` explicitly. Quiet gates are `--quiet`, `--not-attentive`, and `--unknown-context`; unknown context is quiet by default only when explicitly supplied, and no event is treated as evidence identifying a person.

The cue response is fixed metadata (`one discreet visual cue`) and never includes the result text. The CLI has no TTS path. The host-side output is a prototype contract, not deployed hardware behavior.
