"""Tool schemas — what the LLM sees."""

_REPO = {"type": "string", "description": "GitHub repo as 'owner/name'"}
_NUMBER = {"type": "integer", "description": "pull request number"}

PR_QUEUE = {
    "name": "pr_queue",
    "description": (
        "List open pull requests on the watched repos and say which need a review (new head commit this "
        "plugin has not reviewed yet; drafts are skipped). Start every review pass here. Read-only."
    ),
    "parameters": {"type": "object", "properties": {
        "repo": {**_REPO, "description": "only this repo instead of every watched repo"},
    }},
}

PR_CONTEXT = {
    "name": "pr_context",
    "description": (
        "Everything needed to review one PR: title, body, changed files, the diff, CI check states, the "
        "repo's AGENTS.md / CLAUDE.md / CONTRIBUTING.md, findings from earlier passes with their status, "
        "and whether autofix is allowed. Read-only. Load skill 'pr-sentinel:pr-sentinel' before reviewing."
    ),
    "parameters": {"type": "object", "properties": {"repo": _REPO, "number": _NUMBER},
                   "required": ["repo", "number"]},
}

PR_POST_REVIEW = {
    "name": "pr_post_review",
    "description": (
        "Record a finished review of one PR head commit and, if the operator turned post_reviews on, post "
        "it as one PR comment. Otherwise it returns the comment as a draft for the user. Only include "
        "findings you verified against the code: a real bug, security hole, or broken repo rule. No style "
        "nits. An empty findings list is a valid, good review."
    ),
    "parameters": {"type": "object", "properties": {
        "repo": _REPO, "number": _NUMBER,
        "head_sha": {"type": "string", "description": "head_sha from pr_context — the commit you reviewed"},
        "summary": {"type": "string", "description": "2-4 sentences: what the PR does and your verdict"},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "title": {"type": "string", "description": "the defect in under 10 words"},
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "detail": {"type": "string", "description": "concrete failure: input or state -> wrong result"},
            "suggested_fix": {"type": "string", "description": "optional unified diff"},
        }, "required": ["severity", "title", "detail"]}},
    }, "required": ["repo", "number", "head_sha", "summary", "findings"]},
}

PR_AUTOFIX = {
    "name": "pr_autofix",
    "description": (
        "Fix one recorded finding on the PR branch: clones the branch, applies your unified diff, runs the "
        "repo's configured tests, and pushes one commit only if they pass. Refused unless the operator "
        "listed the repo in autofix_repos, the PR is not from a fork, and a test command is configured. "
        "Only call when the user asked for the fix or pr_context says autofix_allowed and the skill's "
        "rules allow it."
    ),
    "parameters": {"type": "object", "properties": {
        "repo": _REPO, "number": _NUMBER,
        "finding_id": {"type": "string", "description": "PS-xxxxxxx id from pr_post_review"},
        "patch": {"type": "string", "description": "unified diff relative to the repo root (git apply format)"},
        "message": {"type": "string", "description": "commit message; defaults to 'Fix <finding title>'"},
    }, "required": ["repo", "number", "finding_id", "patch"]},
}

PR_MARK_FINDING = {
    "name": "pr_mark_finding",
    "description": (
        "Record what happened to a finding so the scorecard stays honest: 'accepted' (user agreed), "
        "'rejected' (finding was wrong), 'fixed_by_author' (a later commit fixed it). Use when the user "
        "says so, or when re-reviewing shows a later commit fixed it."
    ),
    "parameters": {"type": "object", "properties": {
        "finding_id": {"type": "string"},
        "outcome": {"type": "string", "enum": ["accepted", "rejected", "fixed_by_author"]},
        "note": {"type": "string"},
    }, "required": ["finding_id", "outcome"]},
}

PR_SCORECARD = {
    "name": "pr_scorecard",
    "description": (
        "Measured numbers for the reviewer over the last N days: PRs reviewed, findings by severity, how "
        "many landed vs were rejected as wrong (precision), autofix pushed vs tried, and how often a human "
        "had to step in. Returns JSON plus a ready markdown block. Read-only."
    ),
    "parameters": {"type": "object", "properties": {
        "days": {"type": "integer", "description": "window in days (default 7)"},
    }},
}
