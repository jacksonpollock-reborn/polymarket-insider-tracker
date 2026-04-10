# Polymarket Tracker — Current Status

Last updated: 2026-04-09 HKT

## Architecture Status
- The tracker is now wallet+market based, not wallet-only.
- The alert engine scores four buckets and picks one best bucket per alert:
  - `insider`
  - `sports_news`
  - `momentum`
  - `contrarian`
- Shared features are computed once per alert and reused across all buckets.
- Follow-style alerts with entry prices too close to `1.00` are still shown, but they are flagged as thin-edge and separated from the main actionable list in the report.
- Text-based category detection now uses token-aware matching to avoid substring bugs like `nfl` matching inside `conflict`.
- The canonical scheduled runner is [scripts/run_tracker.sh](scripts/run_tracker.sh).
- The canonical runner now performs one delayed rerun for DNS-style failures and posts a local desktop notification when it retries or finally fails.
- The canonical runner now self-reexecs under `/bin/zsh` if launched via `bash`, which protects scheduled runs from shell-specific breakage such as `pipestatus` failures.

## What Is Working
- Four-bucket strategy scoring is implemented.
- Shared feature extraction, bucket routing, and watchlist generation are implemented.
- Review logging is implemented in `review_log.json`.
- Zero-cost tuning artifacts are implemented:
  - `tuning_summary.json`
  - `tuning_checklist.md`
- `AGENTS.md` is the Codex project reference.
- Codex schedules have been pointed back to the canonical repo and should no longer treat stale worktrees as authoritative.
- Unhealthy empty runs now fail non-zero when market bootstrap or flagged-market fetches collapse due to request errors.
- Empty-report email failures now also fail non-zero instead of looking like a successful quiet run.
- The shell runner now retries once after longer DNS/bootstrap or SMTP DNS failures, so transient morning/evening outages get one second chance without changing strategy logic.
- The runner script is executable again, so automations can invoke it directly instead of forcing `bash scripts/run_tracker.sh`.
- The Codex AM/PM automations have been switched from `execution_environment = "worktree"` to `execution_environment = "local"` because archived worktree runs were launched with `network_access: false`.

## Canonical Source Of Truth
- Repo path: root of this repository
- Canonical runner: `scripts/run_tracker.sh`
- Canonical outputs:
  - `watchlist.json`
  - `review_log.json`
  - `tuning_summary.json`
  - `tuning_checklist.md`
  - `report.html`
  - bucketed HTML email report

## Known Traps
- Do not use `.codex/worktrees` as tracker truth.
- Do not use `.claude/worktrees` as tracker truth.
- Do not switch the AM/PM automations back to `execution_environment = "worktree"` unless you also verify the resulting scheduled sessions still have network access.
- Do not judge strategy quality from stale Desktop-local reports produced by old worktree runs.
- Do not retune from one run or from unresolved alerts.

## Current Thresholds
- `MIN_CANDIDATE_SCORE = 20`
- `insider = 40`
- `sports_news = 32`
- `momentum = 35`
- `contrarian = 35`

## Current Config State
- `EXCLUDED_CATEGORIES` is empty, so sports are re-enabled.
- `PREFERRED_TAGS` currently remain `politics,crypto,economics,science,culture`, so sports are included in the broader market scan but not explicitly prioritized in the preferred-tag pass.

## Current Evidence Status
- Sample is still too small to retune.
- Bucket threshold changes should wait until a bucket has at least 12 resolved alerts.
- Feature weight changes should wait until a bucket has at least 20 resolved alerts.
- The latest tuning checklist still says `Hold` across all buckets because evidence is not mature yet.

## Next Review Triggers
- After tomorrow’s canonical scheduled run:
  - verify the report format is the new four-bucket format
  - verify artifacts are written from the canonical repo path
  - verify no stale Desktop-local old-format report is treated as authoritative
- After the first 12 resolved alerts in any bucket:
  - review that bucket for threshold tuning
- After the first 20 resolved alerts in any bucket:
  - allow feature-weight review if needed

## Next Likely Improvements
- If `sports_news` still produces zero or near-zero alerts after multiple canonical runs, tune sports recall carefully.
- If `momentum` starts losing, inspect whether `late_chaser` is concentrated in those losses.
- If `contrarian` stays absent across multiple runs, decide whether it is correctly selective or too strict.
- Watch whether `quick_flip`, `balanced_outcomes`, and `late_chaser` cluster in losses before changing any weights.
- Prefer threshold tuning before weight tuning, and change only one variable per bucket per review cycle.
