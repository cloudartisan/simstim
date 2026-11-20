
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CLI = Path(__file__).with_name("attention_broker.py")


def event_file(root, name, payload):
    path = root / name
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


class BrokerE2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.now = 2_000_000_000.0

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(CLI), "--state", str(self.state),
                                 "--now", str(self.now), *map(str, args)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def put(self):
        return self.cli("put", "--id", "result-1", "--public-summary",
                        "Build complete; 12 checks passed.", "--reviewed")

    def test_persisted_idempotent_put_and_single_cue_tap_retrieval(self):
        self.assertEqual(self.put()["status"], "stored")
        self.assertEqual(self.put()["status"], "already_exists")
        # Prove the second CLI invocation loaded durable state.
        self.assertEqual(json.loads(self.state.read_text())["items"]["result-1"]["status"], "pending")
        nearby = event_file(self.root, "nearby.json", {
            "event_type": "nearby", "affirmative": True, "session_id": "synthetic-session",
            "ts_unix": self.now - 1, "ts": 200,
        })
        self.assertEqual(self.cli("nearby", "--event-file", nearby)["status"], "cue")
        self.assertEqual(self.cli("nearby", "--event-file", nearby)["reason"], "replay")
        # Exercise the documented JSONL boundary without a sibling checkout.
        touch = event_file(self.root, "touch.jsonl", {
            "event_type": "touch", "subtype": "tap", "duration_ms": 350,
            "ts": 123456, "session_id": "synthetic-session", "ts_unix": self.now,
        })
        touch_row = json.loads(touch.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(set(touch_row), {
            "event_type", "subtype", "duration_ms", "ts", "ts_unix", "session_id",
        })
        self.assertEqual(self.cli("tap", "--event-file", touch)["public_summary"],
                         "Build complete; 12 checks passed.")
        self.assertEqual(self.cli("tap", "--event-file", touch)["reason"], "replay")
        self.assertEqual(json.loads(self.state.read_text())["items"]["result-1"]["status"], "retrieved")

    def test_stale_unknown_and_quiet_events_do_not_cue_or_retrieve(self):
        self.put()
        stale = event_file(self.root, "stale.json", {
            "event_type": "nearby", "affirmative": True, "session_id": "s",
            "ts_unix": self.now - 121, "ts": 1,
        })
        self.assertEqual(self.cli("nearby", "--event-file", stale)["reason"], "stale_or_invalid")
        unknown = event_file(self.root, "unknown.json", {
            "event_type": "nearby", "affirmative": False, "session_id": "s", "ts_unix": self.now,
        })
        self.assertEqual(self.cli("nearby", "--event-file", unknown)["reason"], "unknown_or_nonaffirmative")
        good = event_file(self.root, "good.json", {
            "event_type": "nearby", "affirmative": True, "session_id": "s", "ts_unix": self.now,
        })
        self.assertEqual(self.cli("nearby", "--event-file", good, "--quiet")["reason"], "quiet_condition")
        fresh_good = event_file(self.root, "fresh-good.json", {
            "event_type": "nearby", "affirmative": True, "session_id": "s", "ts_unix": self.now,
            "ts": 2,
        })
        self.assertEqual(self.cli("nearby", "--event-file", fresh_good)["status"], "cue")
        tap = event_file(self.root, "tap.json", {
            "event_type": "touch", "subtype": "tap", "duration_ms": 100,
            "ts": 2, "ts_unix": self.now, "session_id": "s",
        })
        no_attention = self.cli("tap", "--event-file", tap, "--not-attentive")
        self.assertEqual(no_attention["reason"], "quiet_condition")
        # That event was consumed, so replay remains quiet; a new explicit tap
        # can retrieve only if conditions are still affirmative.
        newer_tap = event_file(self.root, "tap2.json", {
            "event_type": "touch", "subtype": "tap", "duration_ms": 120,
            "ts": 3, "ts_unix": self.now, "session_id": "s",
        })
        self.assertEqual(self.cli("tap", "--event-file", newer_tap)["status"], "retrieved")

    def test_upstream_touch_requires_session_and_both_timestamps(self):
        self.put()
        near = event_file(self.root, "near.json", {
            "event_type": "nearby", "affirmative": True, "session_id": "s", "ts_unix": self.now,
        })
        self.cli("nearby", "--event-file", near)
        incomplete = event_file(self.root, "incomplete.json", {
            "event_type": "touch", "subtype": "tap", "duration_ms": 350, "ts": 3,
        })
        self.assertEqual(self.cli("tap", "--event-file", incomplete)["reason"], "unknown_event")


if __name__ == "__main__":
    unittest.main()
