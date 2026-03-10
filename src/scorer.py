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
}


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
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

    # ── 3. Zero-hedge: pure YES or pure NO across all current trades ───────────
    sides = set()
    for t in recent_trades:
        s = (t.get("side") or t.get("outcome") or "").upper()
        if s in ("YES", "BUY"):
            sides.add("YES")
        elif s in ("NO", "SELL"):
            sides.add("NO")

    flags["sides_traded"] = list(sides)
    if len(sides) == 1 and len(recent_trades) >= 2:
        score += WEIGHTS["zero_hedge"]
        flags["zero_hedge"] = True
    else:
        flags["zero_hedge"] = False

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

    # ── Build active positions summary ────────────────────────────────────────
    active_positions = []
    for t in recent_trades:
        active_positions.append({
            "market_name":      t.get("_market_name", "Unknown"),
            "market_address":   t.get("_market_address", ""),
            "side":             (t.get("side") or t.get("outcome") or "?").upper(),
            "amount_usdc":      float(t.get("usdcSize") or t.get("size") or 0),
            "entry_price":      float(t.get("price") or 0),
            "market_liquidity": float(t.get("_market_liquidity") or 0),
            "market_end":       t.get("_market_end"),
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
