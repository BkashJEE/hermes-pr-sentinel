# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-02

### Added
- Six tools: `pr_queue`, `pr_context`, `pr_post_review`, `pr_autofix`, `pr_mark_finding`, `pr_scorecard`.
- `pr-sentinel` skill with the review rules and a cron recipe.
- `hermes pr-sentinel-score` CLI command.
- Draft-only reviews and no autofix unless the operator opts in; fork PRs and untested fixes always refused.
