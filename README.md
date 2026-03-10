# Polymarket Insider Tracker

Daily automated scan for wallets exhibiting insider-consistent behavior on Polymarket.
Runs on GitHub Actions and delivers a formatted email report every morning.

---

## What It Does

Every day at 08:00 UTC the tracker:
1. Scans 150 active Polymarket markets for volume anomalies
2. Extracts all trades above $5,000 USDC
3. Groups trades by wallet address
4. Enriches each wallet using Polygonscan + Arkham Intelligence + Dune Analytics
5. Scores each wallet (0–100+) using 11 insider-signal criteria
6. Emails you a formatted HTML report with every flagged wallet

---

## Suspicion Score Criteria

| Signal | Points |
|---|---|
| Wallet age < 30 days on Polygon | +20 |
| Large bet ($5k+) in niche market (<$200k TVL) | +20 |
| Zero hedging (pure YES or pure NO only) | +15 |
| Bet placed within 72h of market resolution | +15 |
| Win rate ≥ 60% on longshot (<20%) markets | +20 |
| Bridge funding within 72h of bet | +10 |
| Mixer/Tornado Cash funding detected | +25 |
| Arkham: linked to project treasury or fund | +20 |
| Coordinated wallet cluster (3+ linked addresses) | +15 |
| Confirmed by Dune whale list | +10 |
| Confirmed by Dune new-wallet list | +10 |

Wallets scoring **≥ 40** appear in the watchlist.

---

## Setup Guide

### Step 1 — Create the GitHub Repository

1. Go to [github.com/new](https://github.com/new)
2. Create a **private** repository (recommended — this contains your API keys logic)
3. Upload or push all files from this folder to the repo root

Your repo structure should look like:
```
your-repo/
├── .github/
│   └── workflows/
│       └── daily_scan.yml
├── src/
│   ├── __init__.py
│   ├── fetchers.py
│   ├── scorer.py
│   └── reporter.py
├── main.py
├── requirements.txt
└── README.md
```

---

### Step 2 — Get Your API Keys

#### Polygonscan
1. Go to [polygonscan.com/apis](https://polygonscan.com/apis)
2. Create a free account → My Profile → API Keys → Add
3. Copy the key

#### Dune Analytics
1. Go to [dune.com/settings/api](https://dune.com/settings/api)
2. Create a free account → Settings → API → Generate new key
3. Copy the key

#### Arkham Intelligence
1. Go to [platform.arkhamintelligence.com](https://platform.arkhamintelligence.com)
2. Create account → Settings → API → Generate key
3. Copy the key

#### Gmail App Password
1. Go to your Google Account → [Security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** (required for App Passwords)
3. Search "App passwords" → Select app: Mail → Device: Other → name it "polymarket-tracker"
4. Google will show you a **16-character password** — copy it immediately (shown only once)

---

### Step 3 — Add Secrets to GitHub

1. Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **"New repository secret"** and add each of the following:

| Secret Name | Value | Where to get it |
|---|---|---|
| `POLYGONSCAN_API_KEY` | Your Polygonscan API key | Step 2 above |
| `DUNE_API_KEY` | Your Dune Analytics key | Step 2 above |
| `ARKHAM_API_KEY` | Your Arkham Intelligence key | Step 2 above |
| `GMAIL_USER` | Your full Gmail address | e.g. `you@gmail.com` |
| `GMAIL_APP_PASSWORD` | The 16-char app password | Step 2 above |
| `EMAIL_TO` | Email to send reports to | Can be same as GMAIL_USER |

---

### Step 4 — Run Your First Test

1. Go to your repo → **Actions** tab
2. Click **"Polymarket Insider Tracker"** in the left sidebar
3. Click **"Run workflow"** → **"Run workflow"**
4. Watch the logs in real time
5. Check your inbox — the report should arrive within ~5 minutes

---

### Step 5 — Adjust the Schedule (Optional)

The default schedule is **08:00 UTC daily**. To change it, edit `.github/workflows/daily_scan.yml`:

```yaml
- cron: "0 8 * * *"   # Change 8 to any hour (0-23 UTC)
```

Examples:
- `"0 6 * * *"` → 06:00 UTC (good for Asia/Taipei: 14:00 TWN)
- `"0 0 * * *"` → midnight UTC
- `"0 8,20 * * *"` → twice a day (08:00 and 20:00 UTC)

For **Taipei timezone (UTC+8)**, use `"0 0 * * *"` to receive the email at 08:00 TWN.

---

### Step 6 — View Saved Watchlists

After each run, a `watchlist.json` file is saved as a GitHub Actions artifact:
1. Go to **Actions** → click any completed run
2. Scroll down to **Artifacts** → download `watchlist-XXXXXX`
3. Open the JSON to see the full structured data for every flagged wallet

---

## Tuning Parameters

Edit `src/scorer.py` to adjust thresholds:

| Variable | Default | Effect |
|---|---|---|
| `MIN_BET_USDC` | 5,000 | Minimum trade size to flag |
| `MAX_NICHE_TVL` | 200,000 | Market TVL below which = "niche" |
| `WALLET_AGE_DAYS` | 30 | Wallets newer than this get flagged |
| `TIMING_HOURS` | 72 | Bet within N hours of resolution = suspicious |
| `LONGSHOT_THRESHOLD` | 0.20 | Probability below which = longshot |
| `MIN_LONGSHOT_WIN_RATE` | 0.60 | Win rate on longshots to trigger flag |

---

## Data Sources

| Source | What It Provides | Required? |
|---|---|---|
| Polymarket CLOB/Gamma API | Markets, trades, positions, wallet history | Yes (free, no key) |
| Polygonscan | USDC funding source, wallet age, mixer detection | Recommended |
| Dune Analytics | Whale wallets, new large bettors | Recommended |
| Arkham Intelligence | Entity labels, wallet clustering | Optional |

The tracker degrades gracefully — it will still run and email you even if some API keys are missing.

---

## Email Report Layout

Each flagged wallet card shows:
- **Suspicion score** (color-coded: red/orange/yellow)
- **Quick-links** to Polymarket profile, Polygonscan, Arkham
- **Stats**: overall win rate, longshot win rate, total resolved markets
- **Active positions**: market name, side, size, entry price, TVL, resolution date
- **Score flags**: which of the 11 criteria were triggered
- **Funding warnings**: mixer or bridge detection results
- **Alert triggers**: what to watch for next
