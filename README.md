# Polymarket Strategy Tracker

Twice-daily automated scan for wallet+market alerts on Polymarket.

The tracker now uses a four-bucket strategy model and scores each alert into one best-fit strategy bucket:
- `insider`
- `sports_news`
- `momentum`
- `contrarian`

It runs on GitHub Actions or locally, saves structured artifacts, and emails a bucketed HTML report.

## Project Reference

For Codex work, the repo-level instruction source of truth is [AGENTS.md](AGENTS.md).

Use `README.md` for human-facing setup and usage.

## What The Tracker Does

Each run:
1. Fetches active Polymarket markets
2. Flags markets with niche-liquidity or volume-spike behavior
3. Pulls recent market trades and builds wallet+market alerts
4. Enriches selected wallets with Polymarket, Polygonscan, Arkham, and optional Dune context
5. Computes shared alert features once
6. Scores all four buckets and assigns one best bucket per alert
7. Writes artifacts and sends an email report

## Strategy Buckets

### `insider`
For politics, finance, crypto-event, and other asymmetric-information setups.

### `sports_news`
For sports-only, news/event-driven edges such as lineup, injury, weather, or other late-breaking information.

### `momentum`
For price-discovery and continuation setups.

### `contrarian`
For overstretched moves and reversal/fade setups.

## Main Artifacts

Each run writes:
- `STATUS.md`
- `watchlist.json`
- `review_log.json`
- `tuning_summary.json`
- `tuning_checklist.md`

### `STATUS.md`
Short-term continuity file for current architecture state, current thresholds, known traps, and next review triggers.
Use this together with `AGENTS.md` when resuming work in a fresh Codex or Claude session.

### `watchlist.json`
The main structured run output.

Includes:
- `stats`
- `bucket_thresholds`
- `candidate_pool`
- `watchlist`
- `review_summary`
- paths to the review and tuning artifacts

### `review_log.json`
Durable alert review history.

Tracks:
- `alert_id`
- generated time
- bucket
- entry price
- 1h / 6h / 24h moves when available
- resolution outcome when available
- review status

### `tuning_summary.json`
Machine-readable tuning snapshot built from the current `watchlist.json` payload and `review_log.json`.

Includes:
- run coverage
- bucket funnel
- bucket performance
- risk-shape indicators
- feature-combo leaderboard
- review backlog
- fixed-rule tuning recommendations

### `tuning_checklist.md`
Human-readable run checklist with these sections:
1. Run Health
2. Bucket Funnel
3. Bucket Performance
4. Risk Flags
5. Suggested Actions
6. Hold / Observe / Tune Decisions

## Setup

### Local

Create a virtualenv and install requirements:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Set runtime secrets in `.env` or your shell:
- `POLYGONSCAN_API_KEY`
- `DUNE_API_KEY`
- `ARKHAM_API_KEY`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
- `EMAIL_TO`

Run:

```bash
. .venv/bin/activate
python main.py
```

### GitHub Actions

Store the same values as repository secrets and run the workflow manually first to validate email delivery.

## Key Runtime Controls

Environment variables:
- `MIN_CANDIDATE_SCORE`
- `MIN_INSIDER_CONFIDENCE`
- `MIN_SPORTS_CONFIDENCE`
- `MIN_MOMENTUM_SCORE`
- `MIN_CONTRARIAN_SCORE`
- `MAX_WALLETS_TO_SCORE`
- `MARKET_TRADE_LIMIT`
- `PREFERRED_TAGS`
- `EXCLUDED_CATEGORIES`

Default preferred tags include sports.

## Tuning Workflow

The tracker is designed for low-cost, evidence-based tuning:
- per run: inspect `tuning_checklist.md`
- weekly: consider one threshold or one weight change per bucket at most
- monthly: review bucket health and artifact quality

Guiding rules:
- do not tune from one run
- prefer threshold changes before weight changes
- use resolved evidence from `review_log.json`
- keep sports in `sports_news`, not inside the generic insider lane

## Email Report

The HTML report now shows bucketed sections instead of one generic insider list:
- Insider Strategy
- Sports News Strategy
- Momentum Strategy
- Contrarian Strategy

Each alert card shows:
- bucket score
- candidate score
- core reasons
- caution flags
- recent trades
- active exposure summary
- review status

## Tests

Run:

```bash
python3 -m unittest discover -s tests
```

The test suite covers:
- shared feature logic
- bucket routing
- report rendering
- tuning summary generation
- tuning recommendation rules
