"""
scorer.py — Computes a Suspicion Score (0–100+) for each wallet.
Uses all available data sources; degrades gracefully if some are missing.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Scoring thresholds ─────────────────────────────────────────────────────────
MIN_BET_USDC           = 5_000
MAX_NICHE_TVL          = 200_000
WALLET_AGE_DAYS        = 30
TIMING_HOURS           = 72
LONGSHOT_THRESHOLD     = 0.20
MIN_LONGSHOT_WIN_RATE  = 0.60
VOLUME_SPIKE_FACTOR    = 3.0

# ── Score weights ──────────────────────────────────────────────────────────────
WEIGHTS = {
    "new_wallet":            20,
    "large_bet_niche":       20,
    "zero_hedge":            15,
    "immaculate_timing":     15,
    "longshot_win_rate":     20,
    "obfuscated_funding":    10,
    "mixer_funding":         25,   # stronger signal
    "arkham_project_link":   20,
    "coordinated_cluster":   15,
    "dune_whale_flag":       10,
    "dune_new_wallet_flag":  10,
    # Anti-evasion signals
    "cumulative_position":   20,   # smurfing: many small bets add up
    "decoy_hedge":           15,   # fake hedge: tiny opposite bet to fool scanner
    "coordinated_swarm":     25,   # multiple new wallets same market same time
    "quick_flip":            15,   # buy then sell within 24h (swing, not conviction)
}

# Anti-evasion thresholds
CUMULATIVE_POSITION_MIN  = 10_000   # total across all bets in same market
DECOY_HEDGE_RATIO        = 0.10     # minority side < 10% of majority = fake hedge
COORDINATED_SWARM_HOURS  = 2        # wallets acting within ±2h = coordinated
COORDINATED_SWARM_MIN    = 3        # min wallets to flag as swarm
QUICK_FLIP_HOURS         = 24       # buy→sell within 24h = swing trade


def _parse_dt(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        except Exception:
            return None


def score_wallet(
    address: str,
    recent_trades: list[dict],        # trades in flagged markets (last 24h)
    polymarket_activity: list[dict],  # full wallet history on Polymarket
    positions: list[dict],
    polygon_data: dict,
    arkham_data: dict,
    dune_whale_list: list[str],
    dune_new_wallet_list: list[str],
    is_swarm_wallet: bool = False,    # flagged by cross-wallet swarm detection
) -> dict:
    """Returns a scored wallet record ready for the watchlist."""

    now   = datetime.now(timezone.utc)
    score = 0
    flags = {}

    # ── 1. Wallet age on Polymarket ────────────────────────────────────────────
    timestamps = []
    for a in polymarket_activity:
        dt = _parse_dt(a.get("timestamp") or a.get("createdAt") or a.get("created_at"))
        if dt:
            timestamps.append(dt)

    # Also use Polygonscan first-tx as a fallback
    poly_first = polygon_data.get("first_tx_timestamp")
    if poly_first:
        timestamps.append(datetime.fromtimestamp(int(poly_first), tz=timezone.utc))

    age_days = None
    if timestamps:
        age_days = (now - min(timestamps)).days
        if age_days < WALLET_AGE_DAYS:
            score += WEIGHTS["new_wallet"]
            flags["new_wallet"] = True
        else:
            flags["new_wallet"] = False
    else:
        flags["new_wallet"] = False

    flags["wallet_age_days"] = age_days

    # ── 2. Large bet in low-liquidity (niche) market ───────────────────────────
    niche_trades = [
        t for t in recent_trades
        if float(t.get("_market_liquidity", MAX_NICHE_TVL + 1)) < MAX_NICHE_TVL
        and float(t.get("usdcSize") or t.get("size") or 0) >= MIN_BET_USDC
    ]
    flags["niche_market_bets"] = len(niche_trades)
    if niche_trades:
        score += WEIGHTS["large_bet_niche"]
        flags["large_bet_niche"] = True
    else:
        flags["large_bet_niche"] = False

    # ── 2b. Cumulative position (smurfing detection) ───────────────────────────
    # Sum all BUY trades per market — many small bets in same market = smurfing
    market_totals: dict[str, float] = {}
    for t in recent_trades:
        if (t.get("side") or "").upper() != "BUY":
            continue
        mkt = t.get("_market_address") or t.get("_market_name") or "unknown"
        market_totals[mkt] = market_totals.get(mkt, 0) + float(t.get("usdcSize") or 0)

    smurf_markets = {m: v for m, v in market_totals.items() if v >= CUMULATIVE_POSITION_MIN}
    flags["smurf_markets"]       = list(smurf_markets.keys())
    flags["cumulative_max_usdc"] = round(max(market_totals.values()), 2) if market_totals else 0

    if smurf_markets:
        score += WEIGHTS["cumulative_position"]
        flags["cumulative_position"] = True
    else:
        flags["cumulative_position"] = False

    # ── 3. Zero-hedge + Decoy-hedge detection ────────────────────────────────
    # Redemptions = any trade at price ~1.00 — not real trading behaviour
    # Real trades never occur at price >= 0.99 (max payout is $1.00 per share)
    real_trades = [
        t for t in recent_trades
        if float(t.get("price") or 0) < 0.99
    ]

    buy_usdc  = sum(float(t.get("usdcSize") or 0) for t in real_trades if (t.get("side") or "").upper() == "BUY")
    sell_usdc = sum(float(t.get("usdcSize") or 0) for t in real_trades if (t.get("side") or "").upper() == "SELL")
    total_usdc = buy_usdc + sell_usdc

    sides = set()
    if buy_usdc  > 0: sides.add("BUY")
    if sell_usdc > 0: sides.add("SELL")

    flags["sides_traded"] = list(sides)
    flags["buy_usdc"]     = round(buy_usdc, 2)
    flags["sell_usdc"]    = round(sell_usdc, 2)

    # True zero-hedge: only one side
    if len(sides) == 1 and len(real_trades) >= 2:
        score += WEIGHTS["zero_hedge"]
        flags["zero_hedge"]   = True
        flags["decoy_hedge"]  = False
    elif len(sides) == 2 and total_usdc > 0:
        # Decoy-hedge: both sides exist but minority is < DECOY_HEDGE_RATIO of majority
        minority = min(buy_usdc, sell_usdc)
        majority = max(buy_usdc, sell_usdc)
        hedge_ratio = minority / majority if majority > 0 else 0
        flags["hedge_ratio"] = round(hedge_ratio, 3)
        if hedge_ratio < DECOY_HEDGE_RATIO:
            # Tiny opposite bet — treating as effectively zero-hedge
            score += WEIGHTS["zero_hedge"]
            score += WEIGHTS["decoy_hedge"]
            flags["zero_hedge"]  = True   # functionally unhedged
            flags["decoy_hedge"] = True   # but tried to fake it
        else:
            flags["zero_hedge"]  = False
            flags["decoy_hedge"] = False
    else:
        flags["zero_hedge"]  = False
        flags["decoy_hedge"] = False

    # ── 4. Immaculate timing: bet within TIMING_HOURS of resolution ────────────
    timing_hits = []
    for t in recent_trades:
        end_dt = _parse_dt(t.get("_market_end"))
        if not end_dt:
            continue
        hours_left = (end_dt - now).total_seconds() / 3600
        if 0 < hours_left < TIMING_HOURS:
            timing_hits.append({
                "market": t.get("_market_name", "?"),
                "hours_to_resolution": round(hours_left, 1),
            })

    flags["timing_hits"] = timing_hits
    if timing_hits:
        score += WEIGHTS["immaculate_timing"]
        flags["immaculate_timing"] = True
    else:
        flags["immaculate_timing"] = False

    # ── 5. Longshot win rate ───────────────────────────────────────────────────
    longshot_wins  = 0
    longshot_total = 0
    total_wins     = 0
    total_resolved = 0

    for p in positions:
        outcome   = (p.get("outcome") or "").lower()
        avg_price = float(p.get("avgPrice") or p.get("curPrice") or 0.5)
        is_won    = outcome in ("won", "redeemed") or float(p.get("cashPnl") or 0) > 0

        if outcome in ("won", "lost", "redeemed", "expired"):
            total_resolved += 1
            if is_won:
                total_wins += 1

        if avg_price <= LONGSHOT_THRESHOLD:
            longshot_total += 1
            if is_won:
                longshot_wins += 1

    longshot_win_rate = (longshot_wins / longshot_total) if longshot_total >= 2 else None
    flags["longshot_total"]    = longshot_total
    flags["longshot_wins"]     = longshot_wins
    flags["longshot_win_rate"] = round(longshot_win_rate, 2) if longshot_win_rate is not None else None
    flags["total_wins"]        = total_wins
    flags["total_resolved"]    = total_resolved
    flags["overall_win_rate"]  = round(total_wins / total_resolved, 2) if total_resolved > 0 else None

    if longshot_win_rate is not None and longshot_win_rate >= MIN_LONGSHOT_WIN_RATE:
        score += WEIGHTS["longshot_win_rate"]
        flags["longshot_flag"] = True
    else:
        flags["longshot_flag"] = False

    # ── 6. Obfuscated / bridge funding (Polygonscan) ──────────────────────────
    funding_flags = polygon_data.get("funding_flags", [])
    flags["funding_flags"] = funding_flags

    if any("mixer" in f.lower() for f in funding_flags):
        score += WEIGHTS["mixer_funding"]
        flags["mixer_flag"] = True
    else:
        flags["mixer_flag"] = False

    if any("bridge" in f.lower() for f in funding_flags):
        score += WEIGHTS["obfuscated_funding"]
        flags["bridge_flag"] = True
    else:
        flags["bridge_flag"] = False

    # ── 7. Arkham entity resolution ────────────────────────────────────────────
    arkham_label = arkham_data.get("label", "Unknown")
    arkham_type  = arkham_data.get("type", "unknown")
    flags["arkham_label"]   = arkham_label
    flags["arkham_type"]    = arkham_type
    flags["arkham_website"] = arkham_data.get("website")
    flags["arkham_cluster"] = arkham_data.get("cluster_size", 0)
    flags["related_wallets"] = arkham_data.get("related_addresses", [])

    project_types = {"project", "treasury", "developer", "team", "fund", "institution"}
    if arkham_type.lower() in project_types or (
        arkham_label != "Unknown" and any(k in arkham_label.lower() for k in ["fund", "capital", "ventures"])
    ):
        score += WEIGHTS["arkham_project_link"]
        flags["arkham_project_link"] = True
    else:
        flags["arkham_project_link"] = False

    if arkham_data.get("cluster_size", 0) >= 3:
        score += WEIGHTS["coordinated_cluster"]
        flags["coordinated_cluster"] = True
    else:
        flags["coordinated_cluster"] = False

    # ── 8. Dune cross-reference ────────────────────────────────────────────────
    addr_lower = address.lower()
    flags["dune_whale_flag"]      = addr_lower in [w.lower() for w in dune_whale_list if w]
    flags["dune_new_wallet_flag"] = addr_lower in [w.lower() for w in dune_new_wallet_list if w]

    if flags["dune_whale_flag"]:
        score += WEIGHTS["dune_whale_flag"]
    if flags["dune_new_wallet_flag"]:
        score += WEIGHTS["dune_new_wallet_flag"]

    # ── 8b. Coordinated swarm (cross-wallet, detected in main.py) ─────────────
    flags["coordinated_swarm"] = is_swarm_wallet
    if is_swarm_wallet:
        score += WEIGHTS["coordinated_swarm"]

    # ── 9. Quick-flip detection (swing trade, not conviction) ────────────────
    # Look in full wallet activity for BUY→SELL in same market within QUICK_FLIP_HOURS
    flip_events = []
    activity_by_market: dict[str, list] = {}
    for a in polymarket_activity:
        mkt = a.get("conditionId") or a.get("market") or ""
        if mkt:
            activity_by_market.setdefault(mkt, []).append(a)

    for mkt, acts in activity_by_market.items():
        buys  = [a for a in acts if (a.get("side") or a.get("type") or "").upper() in ("BUY",)]
        sells = [a for a in acts if (a.get("side") or a.get("type") or "").upper() in ("SELL",)
                 and float(a.get("price") or 0) < 0.99]   # exclude redemptions
        for b in buys:
            b_dt = _parse_dt(b.get("timestamp") or b.get("createdAt"))
            if not b_dt:
                continue
            for s in sells:
                s_dt = _parse_dt(s.get("timestamp") or s.get("createdAt"))
                if not s_dt:
                    continue
                hours_held = (s_dt - b_dt).total_seconds() / 3600
                if 0 < hours_held < QUICK_FLIP_HOURS:
                    flip_events.append({
                        "market":      mkt,
                        "hours_held":  round(hours_held, 1),
                        "buy_usdc":    float(b.get("usdcSize") or b.get("size") or 0),
                    })

    flags["quick_flips"] = flip_events
    if flip_events:
        score += WEIGHTS["quick_flip"]
        flags["quick_flip"] = True
    else:
        flags["quick_flip"] = False

    # ── Build active positions summary (exclude redemptions) ─────────────────
    active_positions = []
    for t in recent_trades:
        price_val = float(t.get("price") or 0)
        side      = (t.get("side") or "BUY").upper()
        # Skip all redemptions — price >= 0.99 means resolved market payout (both BUY and SELL)
        if price_val >= 0.99:
            continue
        outcome = t.get("outcome") or t.get("name") or ""  # e.g. "Overpass", "Yes", "No"
        active_positions.append({
            "market_name":      t.get("_market_name", "Unknown"),
            "market_address":   t.get("_market_address", ""),
            "side":             side,
            "outcome":          outcome,   # ← specific position purchased
            "amount_usdc":      float(t.get("usdcSize") or t.get("size") or 0),
            "entry_price":      float(t.get("price") or 0),
            "market_liquidity": float(t.get("_market_liquidity") or 0),
            "market_end":       t.get("_market_end"),
            "days_to_end":      t.get("_days_to_end"),
            "spike_ratio":      t.get("_spike_ratio", 0),
        })

    # ── Alert triggers ────────────────────────────────────────────────────────
    alert_triggers = []
    if any(p["amount_usdc"] >= 5000 for p in active_positions):
        alert_triggers.append("ALERT_NEW_LARGE_POSITION")
    if any(p["market_liquidity"] < 50_000 for p in active_positions):
        alert_triggers.append("ALERT_NICHE_MARKET")
    if flags.get("immaculate_timing"):
        alert_triggers.append("ALERT_TIMING_SNIPE")
    if flags.get("mixer_flag"):
        alert_triggers.append("ALERT_SUSPICIOUS_FUNDING")
    if flags.get("cumulative_position"):
        alert_triggers.append("ALERT_SMURF_DETECTED")
    if flags.get("decoy_hedge"):
        alert_triggers.append("ALERT_FAKE_HEDGE")
    if flags.get("coordinated_swarm"):
        alert_triggers.append("ALERT_COORDINATED_SWARM")
    if flags.get("quick_flip"):
        alert_triggers.append("ALERT_QUICK_FLIP")

    return {
        "wallet_address":   address,
        "suspicion_score":  score,
        "score_breakdown":  flags,
        "entity_label":     arkham_label,
        "entity_type":      arkham_type,
        "active_positions": active_positions,
        "historical_record": {
            "total_resolved":    total_resolved,
            "total_wins":        total_wins,
            "overall_win_rate":  flags["overall_win_rate"],
            "longshot_total":    longshot_total,
            "longshot_wins":     longshot_wins,
            "longshot_win_rate": flags["longshot_win_rate"],
        },
        "polygon_tx_count":  polygon_data.get("tx_count", 0),
        "funding_warnings":  funding_flags,
        "alert_triggers":    alert_triggers,
        "last_updated":      now.isoformat(),
    }
