# PR Sentinel

A local, open-source PR reviewer for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
It reviews every new commit on your repos with whatever model your Hermes runs, fixes what it finds
behind a test gate, and keeps an honest scorecard of how often it was right.

- **Runs on your machine.** No GitHub App, no webhook, nothing exposed. It polls with your own `gh` login.
- **Any model.** The review is done by your Hermes agent, so it is Claude, GPT, Grok, Hermes or a local model, your call.
- **Fixes, carefully.** `pr_autofix` clones the PR branch, applies one fix, runs your tests and pushes only if they pass.
- **Keeps score.** Every finding is tracked until it lands or is rejected as wrong. `pr_scorecard` reports precision and how often a human had to step in. No other reviewer publishes that.

## Safe by default

| | Default | Who can change it |
|---|---|---|
| Post reviews to GitHub | **off**: you get a draft | operator (`post_reviews`) |
| Push fix commits | **off** for every repo | operator (`autofix_repos`) |
| Fork PRs | autofix always refused: contributor code is never run or pushed to | nobody |
| Untested fixes | always refused: a test command is required and must pass | nobody |
| Tokens | never read: every call goes through `gh` | — |

## Install

```bash
hermes plugins install BkashJEE/hermes-pr-sentinel
hermes plugins enable pr-sentinel
```

Needs the [GitHub CLI](https://cli.github.com/) logged in (`gh auth status`).

Then in `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    pr-sentinel:
      settings:
        repos: [you/your-repo]
        # post_reviews: true                      # once you trust the drafts
        # autofix_repos: [you/your-repo]
        # test_commands:
        #   you/your-repo: python -m unittest discover -s tests
```

## Use

Ask your agent "review my PRs", or schedule it:

```bash
hermes cron create "every 15m" "Run one PR Sentinel review pass and report only PRs with findings." --skill pr-sentinel:pr-sentinel --name pr-sentinel
```

Check the numbers any time:

```bash
hermes pr-sentinel-score --days 7
```

## Tools

| Tool | What it does |
|---|---|
| `pr_queue` | Open PRs on watched repos, and which have an unreviewed head commit |
| `pr_context` | Diff, files, CI checks, the repo's AGENTS.md / CLAUDE.md / CONTRIBUTING.md, earlier findings |
| `pr_post_review` | Records the review; posts it as one comment if allowed, else returns a draft |
| `pr_autofix` | One finding → patch → tests → push, with every refusal above |
| `pr_mark_finding` | `accepted` / `rejected` / `fixed_by_author`, so precision stays honest |
| `pr_scorecard` | Reviews, findings by severity, precision, autofix pushed vs tried, interventions |

The bundled `pr-sentinel` skill tells the agent what counts as a finding: real bugs, security issues
and broken repo rules, each with a concrete failure. Never style nits.

## Develop

```bash
python -m unittest discover -s tests -v
hermes plugins validate .
```

The autofix tests run real `git` against a local bare remote; only `gh` is faked.

## License

MIT
