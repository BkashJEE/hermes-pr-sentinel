"""pr-sentinel — a local, open-source PR reviewer for Hermes that fixes what it finds and keeps score."""

import json
from pathlib import Path

from . import schemas
from .sentinel import Sentinel, SentinelError

_SETTINGS = ("repos", "post_reviews", "autofix_repos", "test_commands", "max_diff_chars", "test_timeout")


def _data_dir(ctx) -> Path:
    try:
        return Path(ctx.state.data_dir)
    except Exception:  # older Hermes without plugin state
        from .sentinel import real_home
        return Path(real_home()) / ".hermes" / "plugin-data" / "pr-sentinel"


def register(ctx):
    def sentinel():
        return Sentinel(_data_dir(ctx), {k: ctx.get_config(k, default=None) for k in _SETTINGS})

    def tool(fn):
        def handler(args, **_kw):
            try:
                return json.dumps(fn(sentinel(), args or {}))
            except SentinelError as e:
                return json.dumps({"ok": False, "error": str(e)})
        return handler

    for name, schema, fn, emoji, blurb in (
        ("pr_queue", schemas.PR_QUEUE, lambda s, a: s.queue(a.get("repo")), "📥",
         "List open PRs that need a review"),
        ("pr_context", schemas.PR_CONTEXT, lambda s, a: s.context(a["repo"], int(a["number"])), "🔎",
         "Diff, checks and repo rules for one PR"),
        ("pr_post_review", schemas.PR_POST_REVIEW,
         lambda s, a: s.post_review(a["repo"], int(a["number"]), a["head_sha"], a["summary"], a.get("findings")),
         "📝", "Record a review and post it (or return a draft)"),
        ("pr_autofix", schemas.PR_AUTOFIX,
         lambda s, a: s.autofix(a["repo"], int(a["number"]), a["finding_id"], a["patch"], a.get("message")),
         "🔧", "Fix a finding on the PR branch, push only if tests pass"),
        ("pr_mark_finding", schemas.PR_MARK_FINDING,
         lambda s, a: s.mark(a["finding_id"], a["outcome"], a.get("note", "")), "🏷️",
         "Record whether a finding landed or was wrong"),
        ("pr_scorecard", schemas.PR_SCORECARD, lambda s, a: s.scorecard(int(a.get("days") or 7)), "📊",
         "Measured reviewer precision and interventions"),
    ):
        ctx.register_tool(name=name, toolset="pr_sentinel", schema=schema, handler=tool(fn),
                          emoji=emoji, description=blurb)

    def _score_setup(parser):
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--json", action="store_true", help="machine-readable output")

    def _score_handler(args):
        card = sentinel().scorecard(args.days)
        print(json.dumps(card, indent=2) if args.json else card["markdown"])
        return 0

    try:
        ctx.register_cli_command(name="pr-sentinel-score", help="Print PR Sentinel's measured scorecard",
                                 setup_fn=_score_setup, handler_fn=_score_handler)
    except Exception:  # older Hermes without plugin CLI commands
        pass

    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()):
        if child.is_dir() and (child / "SKILL.md").exists():
            ctx.register_skill(child.name, child / "SKILL.md")
