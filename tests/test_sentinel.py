import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sentinel import Ledger, Sentinel, SentinelError, finding_id, run  # noqa: E402

PR = {"number": 7, "title": "Add f", "author": {"login": "me"}, "headRefName": "feat", "headRefOid": "a" * 40,
      "isCrossRepository": False, "isDraft": False, "url": "https://github.com/o/r/pull/7", "updatedAt": "x"}


class FakeGh:
    """Answers gh calls from a table; everything else goes to the real runner (git, tests)."""

    def __init__(self, prs=(PR,), view=None):
        self.prs, self.view, self.calls = list(prs), view or {}, []

    def __call__(self, cmd, cwd=None, stdin=None, timeout=120):
        if cmd[0] != "gh":
            return run(cmd, cwd=cwd, stdin=stdin, timeout=timeout)
        self.calls.append((cmd, stdin))
        sub = cmd[1:3]
        if sub == ["pr", "list"]:
            return 0, json.dumps(self.prs), ""
        if sub == ["pr", "view"]:
            return 0, json.dumps({**PR, "body": "b", "baseRefName": "main", "files": [{"path": "app.py"}],
                                  "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}], **self.view}), ""
        if sub == ["pr", "diff"]:
            return 0, "diff --git a/app.py b/app.py\n+x\n", ""
        if sub == ["pr", "comment"]:
            return 0, "https://github.com/o/r/pull/7#issuecomment-1\n", ""
        if cmd[1] == "api":
            return (0, "Run the tests.", "") if "AGENTS.md" in cmd[2] else (1, "", "Not Found")
        return 1, "", "unexpected " + " ".join(cmd)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, gh=None, **settings):
        self.gh = gh or FakeGh()
        return Sentinel(self.dir / "data", {"repos": ["o/r"], **settings}, runner=self.gh)


class QueueAndContext(Base):
    def test_needs_repos(self):
        with self.assertRaises(SentinelError):
            Sentinel(self.dir, {}, runner=FakeGh()).queue()

    def test_queue_marks_reviewed_and_drafts(self):
        s = self.make(FakeGh(prs=[PR, {**PR, "number": 8, "isDraft": True}]))
        self.assertEqual(s.queue()["needs_review"], 1)
        s.post_review("o/r", 7, "a" * 40, "ok", [])
        self.assertEqual(s.queue()["needs_review"], 0)

    def test_new_commit_needs_review_again(self):
        s = self.make()
        s.post_review("o/r", 7, "b" * 40, "old head", [])
        self.assertTrue(s.queue()["prs"][0]["needs_review"])

    def test_context_has_guidance_checks_and_autofix_flag(self):
        c = self.make().context("o/r", 7)
        self.assertEqual(c["guidance"], {"AGENTS.md": "Run the tests."})
        self.assertEqual(c["checks"], [{"name": "tests", "state": "SUCCESS"}])
        self.assertFalse(c["autofix_allowed"])
        self.assertFalse(c["diff_truncated"])

    def test_context_truncates_big_diffs(self):
        self.assertTrue(self.make(max_diff_chars=5).context("o/r", 7)["diff_truncated"])


class Posting(Base):
    F = [{"severity": "high", "title": "Off by one", "file": "app.py", "line": 3, "detail": "f(1) returns 3"}]

    def test_draft_by_default_never_calls_comment(self):
        s = self.make()
        out = s.post_review("o/r", 7, "a" * 40, "Adds f.", self.F)
        self.assertFalse(out["posted"])
        self.assertIn("Off by one", out["body"])
        self.assertIn("<!-- pr-sentinel:" + "a" * 40, out["body"])
        self.assertFalse([c for c, _ in self.gh.calls if c[1:3] == ["pr", "comment"]])
        self.assertEqual(out["findings"], [finding_id("o/r", 7, "app.py", "Off by one")])

    def test_posts_when_operator_allows(self):
        s = self.make(post_reviews=True)
        out = s.post_review("o/r", 7, "a" * 40, "Adds f.", self.F)
        self.assertTrue(out["posted"])
        body = [stdin for c, stdin in self.gh.calls if c[1:3] == ["pr", "comment"]][0]
        self.assertIn("**HIGH**", body)

    def test_rejects_unknown_severity(self):
        with self.assertRaises(SentinelError):
            self.make().post_review("o/r", 7, "a" * 40, "x", [{**self.F[0], "severity": "huge"}])


class Scorecard(Base):
    def test_precision_and_interventions(self):
        s = self.make()
        fs = [{"severity": "high", "title": t, "file": "a.py", "detail": "d"} for t in ("one", "two", "three")]
        ids = s.post_review("o/r", 7, "a" * 40, "x", fs)["findings"]
        s.mark(ids[0], "accepted")
        s.mark(ids[1], "rejected")
        card = s.scorecard()
        self.assertEqual((card["findings"], card["landed"], card["rejected"], card["open"]), (3, 1, 1, 1))
        self.assertEqual(card["precision"], 0.5)
        self.assertEqual(card["interventions"], 1)
        self.assertIn("Precision (landed / decided): 50%", card["markdown"])

    def test_empty_scorecard(self):
        card = self.make().scorecard()
        self.assertIsNone(card["precision"])
        self.assertIn("n/a", card["markdown"])

    def test_mark_unknown_finding(self):
        with self.assertRaises(SentinelError):
            self.make().mark("PS-nope", "accepted")

    def test_ledger_skips_corrupt_lines(self):
        led = Ledger(self.dir / "l.jsonl")
        led.add("review", repo="o/r", number=1, sha="s", findings=[])
        with led.path.open("a") as f:
            f.write("{not json\n")
        self.assertEqual(len(led.events()), 1)


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


class Autofix(Base):
    """Real git against a local bare remote; only gh is faked."""

    def setUp(self):
        super().setUp()
        self.remote = self.dir / "remote.git"
        seed = self.dir / "seed"
        git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=self.dir)
        git("clone", "-q", str(self.remote), str(seed), cwd=self.dir)
        (seed / "app.py").write_text("def f():\n    return 3\n")
        git("checkout", "-q", "-b", "feat", cwd=seed)
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "add f", cwd=seed)
        git("push", "-q", "origin", "feat", cwd=seed)
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=seed, capture_output=True,
                                   text=True).stdout.strip()
        self.patch = ("--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 3\n+    return 2\n")
        self.test = f"{sys.executable} -c \"import app; assert app.f() == 2\""

    def sentinel(self, view=None, **settings):
        s = self.make(FakeGh(view={"headRefOid": self.head, **(view or {})}), autofix_repos=["o/r"],
                      test_commands={"o/r": self.test}, **settings)
        s.clone = lambda repo, branch, dest: git("clone", "-q", "-b", branch, str(self.remote), dest, cwd=self.dir)
        # git commit inside autofix needs an identity on CI machines with no global config
        real = s.run
        s.run = lambda cmd, **kw: real(cmd[:1] + ["-c", "user.name=t", "-c", "user.email=t@t"] + cmd[1:]
                                       if cmd[:2] == ["git", "commit"] else cmd, **kw)
        fid = s.post_review("o/r", 7, self.head, "x",
                            [{"severity": "high", "title": "Wrong value", "file": "app.py", "detail": "3 not 2"}])
        return s, fid["findings"][0]

    def remote_app(self):
        return subprocess.run(["git", "--git-dir", str(self.remote), "show", "feat:app.py"], capture_output=True,
                              text=True).stdout

    def test_pushes_only_when_tests_pass(self):
        s, fid = self.sentinel()
        out = s.autofix("o/r", 7, fid, self.patch)
        self.assertTrue(out["ok"], out)
        self.assertIn("return 2", self.remote_app())
        card = s.scorecard()
        self.assertEqual((card["autofix_pushed"], card["landed"]), (1, 1))

    def test_failing_tests_push_nothing(self):
        s, fid = self.sentinel()
        bad = self.patch.replace("return 2", "return 5")
        out = s.autofix("o/r", 7, fid, bad)
        self.assertEqual(out["failure"], "tests_failed")
        self.assertIn("return 3", self.remote_app())
        self.assertEqual(s.scorecard()["interventions"], 1)

    def test_refuses_forks(self):
        s, fid = self.sentinel(view={"isCrossRepository": True})
        self.assertIn("fork", s.autofix("o/r", 7, fid, self.patch)["error"])

    def test_refuses_repo_not_opted_in(self):
        s, fid = self.sentinel()
        s.settings["autofix_repos"] = []
        self.assertIn("autofix_repos", s.autofix("o/r", 7, fid, self.patch)["error"])

    def test_refuses_without_test_command(self):
        s, fid = self.sentinel()
        s.settings["test_commands"] = {}
        self.assertIn("test command", s.autofix("o/r", 7, fid, self.patch)["error"])

    def test_stale_head(self):
        s, fid = self.sentinel(view={"headRefOid": "f" * 40})
        self.assertEqual(s.autofix("o/r", 7, fid, self.patch)["failure"], "stale")

    def test_bad_patch(self):
        s, fid = self.sentinel()
        self.assertEqual(s.autofix("o/r", 7, fid, "--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-x\n+y\n")["failure"],
                         "patch_failed")


if __name__ == "__main__":
    unittest.main()
