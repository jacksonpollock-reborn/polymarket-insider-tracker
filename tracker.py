"""
Polymarket Insider Tracker
Runs daily via GitHub Actions. Scans Polymarket for wallets exhibiting
insider-consistent behavior and sends an email report.
"""

import os
import json
import time
import smtplib
import logging
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict

import requests

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API   = "https://gamma-api.polymarket.com"
DATA_API    = "https://data-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"

MIN_SUSPICION_SCORE   = 40          # wallets below this are excluded
MIN_BET_USDC          = 5_000       # ignore trades smaller than this
MAX_MARKET_TVL        = 200_000     # "niche market" threshold in USDC
LONGSHOT_THRESHOLD    = 0.20        # probability considered a "long shot"
MIN_LONGSHOT_WIN_RATE = 0.60        # flag if win rate on longshots exceeds this
TIMING_HOURS          = 72          # flag if bet placed within N hours of resolution
WALLET_AGE_DAYS       = 30          # flag wallets newer than this (days on Polymarket)
MARKETS_TO_SCAN       = 100         # how many active markets to pull
VOLUME_SPIKE_FACTOR   = 3.0         # 24h volume / 7d avg to count as a spike

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── HTTP helpers ───────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "polymarket-insider-tracker/1.0"})

def get(url: str, params: dict = None, retries: int = 3) -> dict | list | None:
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"GET {url} attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None

# ── Step 1: Fetch active markets ───────────────────────────────────────────────
def fetch_active_markets() -> list[dict]:
    log.info("Fetching active markets...")
    markets = []
    offset = 0
    while len(markets) < MARKETS_TO_SCAN:
        batch = get(f"{GAMMA_API}/markets", params={
            "active": "true",
            "closed": "false",
            "limit": 50,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false"
        })
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
    log.info(f"  → {len(markets)} markets fetched")
    return markets[:MARKETS_TO_SCAN]

# ── Step 2: Identify suspicious markets ───────────────────────────────────────
def flag_suspicious_markets(markets: list[dict]) -> list[dict]:
    """
    Flag markets where:
    - Large volume spike relative to 7d average
    - Low TVL (niche market)
    """
    flagged = []
    for m in markets:
        try:
            vol_24h = float(m.get("volume24hr") or 0)
            vol_7d  = float(m.get("volume") or 0)          # cumulative; approximate
            liquidity = float(m.get("liquidity") or 0)
            avg_7d  = vol_7d / 7 if vol_7d else 0

            spike = (vol_24h / avg_7d) if avg_7d > 0 else 0
            is_niche  = 0 < liquidity < MAX_MARKET_TVL
            has_spike = spike >= VOLUME_SPIKE_FACTOR

            if is_niche or has_spike or vol_24h > MIN_BET_USDC:
                m["_spike_ratio"] = round(spike, 2)
                m["_is_niche"]    = is_niche
                flagged.append(m)
        except Exception as e:
            log.debug(f"Skipping market {m.get('id')}: {e}")
    log.info(f"  → {len(flagged)} markets flagged for deeper scan")
    return flagged

# ── Step 3: Pull recent large trades per market ────────────────────────────────
def fetch_large_trades(markets: list[dict]) -> list[dict]:
    """Return a flat list of large individual trades across all flagged markets."""
    large_trades = []
    for m in markets:
        condition_id = m.get("conditionId") or m.get("condition_id")
        if not condition_id:
            continue
        activity = get(f"{DATA_API}/activity", params={"market": condition_id, "limit": 50})
        if not activity:
            continue
        for trade in activity:
            try:
                usdc_size = float(trade.get("usdcSize") or trade.get("size") or 0)
                if usdc_size >= MIN_BET_USDC:
                    trade["_market_name"]      = m.get("question") or m.get("title") or condition_id
                    trade["_market_address"]   = condition_id
                    trade["_market_liquidity"] = float(m.get("liquidity") or 0)
                    trade["_market_end"]       = m.get("endDate") or m.get("end_date_iso")
                    trade["_spike_ratio"]      = m.get("_spike_ratio", 0)
                    large_trades.append(trade)
            except Exception:
                continue
        time.sleep(0.15)   # gentle rate-limit
    log.info(f"  → {len(large_trades)} large trades found")
    return large_trades

# ── Step 4: Group trades by wallet ────────────────────────────────────────────
def group_by_wallet(trades: list[dict]) -> dict[str, list[dict]]:
    wallet_trades = defaultdict(list)
    for t in trades:
        addr = (t.get("maker") or t.get("proxyWallet") or t.get("transactor") or "").lower()
        if addr and addr.startswith("0x"):
            wallet_trades[addr].append(t)
    log.info(f"  → {len(wallet_trades)} unique wallets")
    return wallet_trades

# ── Step 5: Fetch wallet history & positions ──────────────────────────────────
def fetch_wallet_history(address: str) -> dict:
    history = {"positions": [], "pnl": None, "activity": []}

    positions = get(f"{DATA_API}/positions", params={"user": address, "sizeThreshold": "0"})
    if positions:
        history["positions"] = positions if isinstance(positions, list) else positions.get("data", [])

    activity = get(f"{DATA_API}/activity", params={"user": address, "limit": 100})
    if activity:
        history["activity"] = activity if isinstance(activity, list) else []

    pnl = get(f"{DATA_API}/value", params={"user": address})
    if pnl:
        history["pnl"] = pnl

    time.sleep(0.2)
    return history

# ── Step 6: Score each wallet ──────────────────────────────────────────────────
def score_wallet(address: str, trades: list[dict], history: dict) -> dict:
    score = 0
    breakdown = {}
    now = datetime.now(timezone.utc)

    # ── Criterion 1: Wallet age on Polymarket
    all_activity = history.get("activity", [])
    if all_activity:
        timestamps = []
        for a in all_activity:
            ts = a.get("timestamp") or a.get("createdAt") or a.get("created_at")
            if ts:
                try:
                    timestamps.append(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
                except Exception:
                    pass
        if timestamps:
            first_seen = min(timestamps)
            age_days = (now - first_seen).days
            breakdown["wallet_age_days"] = age_days
            if age_days < WALLET_AGE_DAYS:
                score += 20
                breakdown["new_wallet_flag"] = True
            else:
                breakdown["new_wallet_flag"] = False
        else:
            breakdown["wallet_age_days"] = None
            breakdown["new_wallet_flag"] = False
    else:
        breakdown["wallet_age_days"] = None
        breakdown["new_wallet_flag"] = False

    # ── Criterion 2: Large bet in niche market
    niche_bets = [t for t in trades if t.get("_market_liquidity", MAX_MARKET_TVL + 1) < MAX_MARKET_TVL]
    breakdown["niche_market_bets"] = len(niche_bets)
    if niche_bets:
        score += 20
        breakdown["large_bet_niche_flag"] = True
    else:
        breakdown["large_bet_niche_flag"] = False

    # ── Criterion 3: Zero hedging — only YES or only NO across all positions
    sides = set()
    for t in trades:
        side = (t.get("side") or t.get("outcome") or "").upper()
        if side in ("YES", "BUY"):
            sides.add("YES")
        elif side in ("NO", "SELL"):
            sides.add("NO")
    breakdown["sides_traded"] = list(sides)
    if len(sides) == 1:
        score += 15
        breakdown["zero_hedge_flag"] = True
    else:
        breakdown["zero_hedge_flag"] = False

    # ── Criterion 4: Immaculate timing — bet within TIMING_HOURS of resolution
    timing_hits = []
    for t in trades:
        end_raw = t.get("_market_end")
        if not end_raw:
            continue
        try:
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            hours_to_end = (end_dt - now).total_seconds() / 3600
            if 0 < hours_to_end < TIMING_HOURS:
                timing_hits.append(t.get("_market_name"))
        except Exception:
            pass
    breakdown["timing_hits"] = timing_hits
    if timing_hits:
        score += 15
        breakdown["timing_flag"] = True
    else:
        breakdown["timing_flag"] = False

    # ── Criterion 5: Win rate on longshot markets
    positions = history.get("positions", [])
    longshot_wins   = 0
    longshot_total  = 0
    total_wins      = 0
    total_resolved  = 0

    for p in positions:
        outcome = (p.get("outcome") or "").lower()
        price   = float(p.get("avgPrice") or p.get("curPrice") or 0.5)
        redeemed = float(p.get("cashPnl") or 0)

        if outcome in ("won", "redeemed", "yes") or