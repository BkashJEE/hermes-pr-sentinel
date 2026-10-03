"""PR Sentinel core: everything around the review that must be deterministic — which PRs need a look,
what context the reviewer sees, posting, autofix with a test gate, and the ledger the scorecard reads.

The review itself is done by the calling Hermes agent (any model). This module never reads a token:
every GitHub call goes through the user's own `gh` login.
"""

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

GUIDANCE_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")
GUIDANCE_CHARS = 4000
SEVERITIES = ("critical", "high", "medium", "low")
OUTCOMES = ("accepted", "rejected", "fixed_by_author", "autofixed")
MARKER = "<!-- pr-sentinel:{sha} -->"


def real_home() -> str:
    """Hermes points HOME at the calling profile; gh and git need the user's own config."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


def run(cmd, cwd=None, stdin=None, timeout=120):
    env = {**os.environ, "HOME": real_home(), "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


class SentinelError(Exception):
    pass


class Ledger:
    """Append-only JSONL of reviews, findings, marks and autofix attempts."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def add(self, kind: str, **fields):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, "ts": time.time(), **fields}) + "\n")

    def events(self):
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def reviewed_shas(self):
        return {(e["repo"], e["number"], e["sha"]) for e in self.events() if e["kind"] == "review"}

    def findings(self, repo=None, number=None):
        found, status = {}, {}
        for e in self.events():
            if e["kind"] == "finding":
                found[e["id"]] = e
            elif e["kind"] in ("mark", "autofix") and e.get("outcome"):
                status[e["id"]] = e["outcome"]
        rows = [{**f, "status": status.get(i, "open")} for i, f in found.items()]
        return [r for r in rows if (repo is None or r["repo"] == repo) and (number is None or r["number"] == number)]


def finding_id(repo, number, file, title):
    return "PS-" + hashlib.sha1(f"{repo}#{number}:{file}:{title}".encode()).hexdigest()[:7]


class Sentinel:
    def __init__(self, data_dir: Path, settings: dict | None = None, runner=run):
        self.settings = settings or {}
        self.ledger = Ledger(Path(data_dir) / "ledger.jsonl")
        self.run = runner

    # ---- gh helpers -------------------------------------------------------------------------
    def gh(self, *args, stdin=None, timeout=120):
        code, out, err = self.run(["gh", *args], stdin=stdin, timeout=timeout)
        if code != 0:
            raise SentinelError((err or out).strip()[-800:] or f"gh {args[0]} failed")
        return out

    def gh_json(self, *args):
        return json.loads(self.gh(*args) or "null")

    def _repos(self, repo=None):
        repos = [repo] if repo else list(self.settings.get("repos") or [])
        if not repos:
            raise SentinelError("No repos to watch. Set plugins.entries.pr-sentinel.settings.repos "
                                "to a list like ['owner/name'], or pass repo.")
        return repos

    # ---- tools --------------------------------------------------------------------------------
    def queue(self, repo=None):
        seen = self.ledger.reviewed_shas()
        rows = []
        for r in self._repos(repo):
            prs = self.gh_json("pr", "list", "-R", r, "--state", "open", "--limit", "50", "--json",
                               "number,title,author,headRefName,headRefOid,isCrossRepository,isDraft,url,updatedAt")
            for pr in prs or []:
                rows.append({
                    "repo": r, "number": pr["number"], "title": pr["title"], "url": pr["url"],
                    "author": (pr.get("author") or {}).get("login"), "head_sha": pr["headRefOid"],
                    "from_fork": pr.get("isCrossRepository", False), "draft": pr.get("isDraft", False),
                    "needs_review": (r, pr["number"], pr["headRefOid"]) not in seen and not pr.get("isDraft"),
                })
        return {"ok": True, "prs": rows, "needs_review": sum(1 for p in rows if p["needs_review"])}

    def context(self, repo, number):
        pr = self.gh_json("pr", "view", str(number), "-R", repo, "--json",
                          "number,title,body,author,baseRefName,headRefName,headRefOid,isCrossRepository,"
                          "files,url,statusCheckRollup")
        diff = self.gh("pr", "diff", str(number), "-R", repo, timeout=180)
        limit = int(self.settings.get("max_diff_chars") or 60000)
        guidance = {}
        for name in GUIDANCE_FILES:
            try:
                text = self.gh("api", f"repos/{repo}/contents/{name}?ref={pr['baseRefName']}",
                               "-H", "Accept: application/vnd.github.raw")
                guidance[name] = text[:GUIDANCE_CHARS]
            except SentinelError:
                continue
        checks = [{"name": c.get("name") or c.get("context"), "state": c.get("conclusion") or c.get("state")}
                  for c in pr.get("statusCheckRollup") or []]
        return {
            "ok": True, "repo": repo, "number": number, "url": pr["url"], "title": pr["title"],
            "body": (pr.get("body") or "")[:4000], "author": (pr.get("author") or {}).get("login"),
            "base": pr["baseRefName"], "head": pr["headRefName"], "head_sha": pr["headRefOid"],
            "from_fork": pr.get("isCrossRepository", False),
            "files": [f["path"] for f in pr.get("files") or []], "checks": checks, "guidance": guidance,
            "diff": diff[:limit], "diff_truncated": len(diff) > limit,
            "previous_findings": self.ledger.findings(repo, number),
            "autofix_allowed": self._autofix_refusal(repo, pr) is None,
        }

    def post_review(self, repo, number, head_sha, summary, findings):
        rows = []
        for f in findings or []:
            sev = f.get("severity", "medium")
            if sev not in SEVERITIES:
                raise SentinelError(f"severity must be one of {', '.join(SEVERITIES)}")
            fid = finding_id(repo, number, f.get("file", ""), f["title"])
            rows.append({**f, "severity": sev, "id": fid})
        body = render_review(head_sha, summary, rows)
        posted = bool(self.settings.get("post_reviews"))
        url = None
        if posted:
            url = self.gh("pr", "comment", str(number), "-R", repo, "--body-file", "-", stdin=body).strip()
        for r in rows:
            self.ledger.add("finding", repo=repo, number=number, sha=head_sha, id=r["id"], severity=r["severity"],
                            title=r["title"], file=r.get("file"), line=r.get("line"))
        self.ledger.add("review", repo=repo, number=number, sha=head_sha, findings=[r["id"] for r in rows],
                        posted=posted)
        return {"ok": True, "posted": posted, "comment_url": url, "findings": [r["id"] for r in rows],
                "body": None if posted else body,
                "note": None if posted else "Draft only: post_reviews is off. Show the user the body."}

    def mark(self, fid, outcome, note=""):
        if outcome not in OUTCOMES:
            raise SentinelError(f"outcome must be one of {', '.join(OUTCOMES)}")
        if not any(f["id"] == fid for f in self.ledger.findings()):
            raise SentinelError(f"unknown finding {fid}")
        self.ledger.add("mark", id=fid, outcome=outcome, note=note[:500])
        return {"ok": True, "id": fid, "outcome": outcome}

    # ---- autofix ------------------------------------------------------------------------------
    def _autofix_refusal(self, repo, pr):
        if repo not in (self.settings.get("autofix_repos") or []):
            return f"{repo} is not in autofix_repos (an operator setting)."
        if pr.get("isCrossRepository"):
            return "PR comes from a fork: autofix never runs or pushes contributor code."
        if not self.test_command(repo):
            return f"No test command for {repo}: set test_commands['{repo}']. Autofix never pushes untested."
        return None

    def test_command(self, repo):
        return (self.settings.get("test_commands") or {}).get(repo)

    def clone(self, repo, branch, dest):
        code, out, err = self.run(["gh", "repo", "clone", repo, dest, "--", "--branch", branch, "--depth", "50"],
                                  timeout=300)
        if code != 0:
            raise SentinelError(f"clone failed: {(err or out).strip()[-400:]}")

    def autofix(self, repo, number, fid, patch, message=None):
        pr = self.gh_json("pr", "view", str(number), "-R", repo, "--json",
                          "headRefName,headRefOid,isCrossRepository")
        refusal = self._autofix_refusal(repo, pr)
        if refusal:
            return {"ok": False, "error": refusal}
        finding = next((f for f in self.ledger.findings(repo, number) if f["id"] == fid), None)
        if not finding:
            return {"ok": False, "error": f"{fid} is not a recorded finding on {repo}#{number}"}
        with tempfile.TemporaryDirectory(prefix="pr-sentinel-") as tmp:
            work = str(Path(tmp) / "repo")
            self.clone(repo, pr["headRefName"], work)
            _, head, _ = self.run(["git", "rev-parse", "HEAD"], cwd=work)
            if head.strip() != pr["headRefOid"]:
                return self._fix_result(fid, "stale", "Branch moved while fixing; review the new head first.")
            patch_file = Path(tmp) / "fix.patch"
            patch_file.write_text(patch if patch.endswith("\n") else patch + "\n", encoding="utf-8")
            code, _, err = self.run(["git", "apply", "--check", str(patch_file)], cwd=work)
            if code != 0:
                return self._fix_result(fid, "patch_failed", err.strip()[-600:])
            self.run(["git", "apply", str(patch_file)], cwd=work)
            code, out, err = self.run(shlex.split(self.test_command(repo)), cwd=work,
                                      timeout=int(self.settings.get("test_timeout") or 900))
            if code != 0:
                return self._fix_result(fid, "tests_failed", (out + err).strip()[-1500:])
            msg = message or f"Fix {finding['title']}\n\nAutofix for {fid}, tests passed: {self.test_command(repo)}"
            self.run(["git", "add", "-A"], cwd=work)
            code, _, err = self.run(["git", "commit", "-q", "-m", msg], cwd=work)
            if code != 0:
                return self._fix_result(fid, "commit_failed", err.strip()[-600:])
            _, sha, _ = self.run(["git", "rev-parse", "HEAD"], cwd=work)
            code, _, err = self.run(["git", "push", "origin", f"HEAD:{pr['headRefName']}"], cwd=work, timeout=300)
            if code != 0:
                return self._fix_result(fid, "push_failed", err.strip()[-600:])
        self.ledger.add("autofix", id=fid, repo=repo, number=number, outcome="autofixed", commit=sha.strip(),
                        tests="passed")
        return {"ok": True, "id": fid, "commit": sha.strip(), "tests": "passed", "pushed": True}

    def _fix_result(self, fid, failure, detail):
        self.ledger.add("autofix", id=fid, outcome=None, failure=failure)
        return {"ok": False, "id": fid, "failure": failure, "detail": detail, "pushed": False}

    # ---- scorecard ----------------------------------------------------------------------------
    def scorecard(self, days=7):
        since = time.time() - days * 86400
        ev = [e for e in self.ledger.events() if e["ts"] >= since]
        reviews = [e for e in ev if e["kind"] == "review"]
        findings = [f for f in self.ledger.findings() if f["ts"] >= since]
        fixes = [e for e in ev if e["kind"] == "autofix"]
        decided = [f for f in findings if f["status"] != "open"]
        rejected = [f for f in decided if f["status"] == "rejected"]
        landed = [f for f in decided if f["status"] in ("accepted", "fixed_by_author", "autofixed")]
        fix_ok = [e for e in fixes if e.get("outcome") == "autofixed"]
        card = {
            "days": days, "prs_reviewed": len({(r["repo"], r["number"]) for r in reviews}),
            "reviews": len(reviews), "findings": len(findings),
            "by_severity": {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES},
            "decided": len(decided), "landed": len(landed), "rejected": len(rejected),
            "precision": round(len(landed) / len(decided), 3) if decided else None,
            "autofix_attempts": len(fixes), "autofix_pushed": len(fix_ok),
            "autofix_failures": {k: sum(1 for e in fixes if e.get("failure") == k)
                                 for k in sorted({e.get("failure") for e in fixes if e.get("failure")})},
            "interventions": len(rejected) + len(fixes) - len(fix_ok),
            "open": len(findings) - len(decided),
        }
        card["markdown"] = render_scorecard(card)
        return {"ok": True, **card}


def render_review(sha, summary, rows):
    lines = [MARKER.format(sha=sha), "### PR Sentinel review", "", summary.strip(), ""]
    if not rows:
        lines.append("No findings on `%s`." % sha[:7])
    for r in rows:
        where = f"`{r['file']}:{r['line']}`" if r.get("line") else (f"`{r['file']}`" if r.get("file") else "")
        lines += [f"**{r['severity'].upper()}** · {r['title']} · {where} · `{r['id']}`", "", r.get("detail", "").strip()]
        if r.get("suggested_fix"):
            lines += ["", "<details><summary>Suggested fix</summary>", "", "```diff", r["suggested_fix"].strip(),
                      "```", "</details>"]
        lines.append("")
    lines.append(f"<sub>Reviewed `{sha[:7]}` by a local Hermes agent with "
                 "[pr-sentinel](https://github.com/BkashJEE/hermes-pr-sentinel).</sub>")
    return "\n".join(lines)


def render_scorecard(c):
    p = "n/a" if c["precision"] is None else f"{c['precision']:.0%}"
    return "\n".join([
        f"**PR Sentinel, last {c['days']} days**",
        f"- PRs reviewed: {c['prs_reviewed']} ({c['reviews']} review passes)",
        f"- Findings: {c['findings']} (critical {c['by_severity']['critical']}, high {c['by_severity']['high']}, "
        f"medium {c['by_severity']['medium']}, low {c['by_severity']['low']})",
        f"- Landed: {c['landed']} · Rejected as wrong: {c['rejected']} · Still open: {c['open']}",
        f"- Precision (landed / decided): {p}",
        f"- Autofix: {c['autofix_pushed']} pushed of {c['autofix_attempts']} tried",
        f"- Times a human had to step in: {c['interventions']}",
    ])
