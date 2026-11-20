"""Offline integration tests; every Git remote is a disposable local repository."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "scripts/check-upstream"


class CheckUpstreamTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="simstim-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
            GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="Fake Author", GIT_AUTHOR_EMAIL="author@example.invalid",
            GIT_COMMITTER_NAME="Fake Committer", GIT_COMMITTER_EMAIL="committer@example.invalid",
        )
        self.origin = self.root / "bare origin.git"
        self.author = self.root / "author"
        self.verify = self.root / "verification checkout"
        self.deploy = self.root / "deploy checkout"
        self.git(self.root, "init", "--bare", "-b", "main", self.origin)
        self.git(self.root, "clone", self.origin, self.author)
        self.base = self.commit(self.author, "app.txt", "baseline\n", "baseline")
        self.git(self.author, "push", "origin", "main")
        self.git(self.root, "clone", "--no-local", "--no-checkout", self.origin, self.verify)
        self.git(self.root, "clone", "--no-local", self.origin, self.deploy)
        self.git(self.verify, "switch", "--detach", self.base)

    def git(self, where, *args):
        return subprocess.run(
            ["git", *args], cwd=where, env=self.env, text=True,
            capture_output=True, check=True,
        ).stdout.strip()

    def commit(self, where, path, text, message):
        (where / path).write_text(text, encoding="utf-8")
        self.git(where, "add", "--", path)
        self.git(where, "commit", "-m", message)
        return self.git(where, "rev-parse", "HEAD")

    def check_upstream(self, pin=None, where=None):
        self.assertTrue(HELPER.is_file(), "check-upstream helper is missing")
        return subprocess.run(
            [HELPER, self.base if pin is None else pin], cwd=where or self.verify,
            env=self.env, text=True, capture_output=True, timeout=20,
        )

    def contribute(self):
        incoming = self.commit(self.author, "app.txt", "contribution\n", "upstream contribution")
        self.git(self.author, "push", "origin", "main")
        return incoming

    def test_reports_and_repeats_without_touching_deploy(self):
        self.commit(self.deploy, "app.txt", "local deploy fork\n", "local divergence")
        (self.deploy / "app.txt").write_text("unstaged deploy edit\n", encoding="utf-8")
        edit = self.deploy / "local-only.txt"
        edit.write_text("staged local edit\n", encoding="utf-8")
        self.git(self.deploy, "add", "--", edit.name)
        (self.deploy / "notes.txt").write_text("untracked note\n", encoding="utf-8")

        def snapshot():
            return (
                self.git(self.deploy, "show-ref"),
                self.git(self.deploy, "rev-parse", "HEAD"),
                self.git(self.deploy, "status", "--porcelain=v1"),
                self.git(self.deploy, "diff", "--binary"),
                self.git(self.deploy, "diff", "--cached", "--binary"),
                (self.deploy / "notes.txt").read_bytes(),
            )

        before = snapshot()
        verify_refs = self.git(self.verify, "show-ref")
        first = self.check_upstream()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("No incoming changes.", first.stdout)
        incoming = self.contribute()
        for _ in range(2):
            result = self.check_upstream()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Candidate: {incoming}", result.stdout)
            self.assertIn("upstream contribution", result.stdout)
            self.assertIn("app.txt", result.stdout)
            self.assertEqual(self.git(self.verify, "rev-parse", "HEAD"), self.base)
            self.assertEqual(self.git(self.verify, "show-ref"), verify_refs)
            self.assertEqual((self.verify / "app.txt").read_text(), "baseline\n")
            self.assertEqual(snapshot(), before)

        # The guide's manual test gate uses the exact reported SHA, not a branch.
        self.git(self.verify, "switch", "--detach", "--no-overwrite-ignore", incoming)
        self.assertEqual((self.verify / "app.txt").read_text(), "contribution\n")
        self.assertEqual(snapshot(), before)

    def test_dirty_verification_is_refused_before_fetch(self):
        incoming = self.contribute()
        (self.verify / ".git/info/exclude").write_text("scratch.txt\n")
        ignored = self.verify / "scratch.txt"
        ignored.write_text("local scratch\n")
        result = self.check_upstream()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verification checkout is dirty", result.stderr)
        self.assertEqual(ignored.read_text(), "local scratch\n")
        self.assertFalse((self.verify / ".git/FETCH_HEAD").exists())
        self.assertNotEqual(self.git(self.verify, "rev-parse", "HEAD"), incoming)

    def test_attached_deploy_checkout_is_refused(self):
        self.contribute()
        result = self.check_upstream(where=self.deploy)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HEAD must be detached", result.stderr)
        self.assertFalse((self.deploy / ".git/FETCH_HEAD").exists())

    def test_abbreviated_and_moving_pins_are_refused_before_fetch(self):
        for pin in (self.base[:10], "HEAD", "main", "--help"):
            with self.subTest(pin=pin):
                result = self.check_upstream(pin=pin)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("pin must be a full", result.stderr)
                self.assertFalse((self.verify / ".git/FETCH_HEAD").exists())

    def test_divergent_pin_has_no_candidate(self):
        self.contribute()
        fork = self.commit(self.verify, "app.txt", "verification fork\n", "local fork")
        result = self.check_upstream(pin=fork)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an ancestor", result.stderr)
        self.assertNotIn("Candidate:", result.stdout)
        self.assertEqual(self.git(self.verify, "rev-parse", "HEAD"), fork)
        self.assertEqual((self.verify / "app.txt").read_text(), "verification fork\n")

    def test_missing_main_does_not_reuse_previous_fetch(self):
        self.contribute()
        good = self.check_upstream()
        self.assertEqual(good.returncode, 0, good.stderr)
        self.git(self.origin, "update-ref", "-d", "refs/heads/main")
        result = self.check_upstream()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fetch of origin main failed", result.stderr)
        self.assertNotIn("Candidate:", result.stdout)
        self.assertEqual(self.git(self.verify, "rev-parse", "HEAD"), self.base)


if __name__ == "__main__":
    unittest.main()
