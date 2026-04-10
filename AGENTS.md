# Polymarket Tracker — Codex Project Reference

## Project Goal
Maximize signal quality across four strategy buckets:
- `insider`
- `sports_news`
- `momentum`
- `contrarian`

The tracker is optimized for repeatable, evidence-based tuning rather than one-off intuition.

## Hard Constraints
- No recurring paid tooling beyond the user’s GPT/Codex subscription and the project’s existing free/optional APIs.
- Do not add hosted databases, analytics vendors, or paid observability products.
- Prefer local files and GitHub Actions artifacts as the system of record.

## Canonical Outputs
- `watchlist.json`
- `review_log.json`
- `tuning_summary.json`
- `tuning_checklist.md`
- `STATUS.md`
- HTML email report

## Strategy Definitions
### `insider`
Use for politics, finance, crypto-event, and other asymmetric-information setups.
Good looks like:
- one-sided conviction
- real timing edge
- credible funding/entity evidence
- acceptable resolved win rate and non-negative expectancy

### `sports_news`
Use for sports-only, event-driven edges such as lineup, injury, weather, or late news.
Good looks like:
- late event timing
- meaningful capital impact
- one-sided positioning
- useful follow-through after entry

### `momentum`
Use for price-discovery and follow-through.
Good looks like:
- acceleration plus continuation
- acceptable 24h expectancy
- limited late-chasing and mean-reversion damage

### `contrarian`
Use for overstretched moves and reversal/fade setups.
Good looks like:
- selective alert volume
- good reversal expectancy
- not firing constantly on ordinary noise

## Tuning Policy
- Do not retune from a single run.
- Tune only from aggregated resolved evidence.
- Adjust thresholds in small steps only.
- Prefer threshold changes before weight changes.
- Never change more than one variable per bucket in a single review cycle.
- Weight changes should only happen during a weekly review, never ad hoc mid-week.

## Review Cadence
- Per run: inspect `tuning_checklist.md`
- Weekly: decide whether to change thresholds or one bucket weight
- Monthly: review bucket health, artifact quality, and whether any lane should be narrowed or expanded

## Working Rules
- Keep the tracker wallet+market based; do not regress to wallet-only scoring.
- Keep sports in `sports_news`, not mixed into generic insider scoring.
- Preserve soft-compatible JSON fields when feasible.
- Prefer evidence from `review_log.json` and `tuning_summary.json` over memory when discussing performance.
- Treat `STATUS.md` as the short-term execution memory for current state, next review triggers, and known traps.
- Canonical tracker source path: the root of this repository
- Do not use `.codex/worktrees` or `.claude/worktrees` as tracker truth.
