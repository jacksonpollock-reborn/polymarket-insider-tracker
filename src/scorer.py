"""
scorer.py — Scores wallet+market alerts across four strategy buckets.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# ── Scan thresholds ────────────────────────────────────────────────────────────
MIN_BET_USDC = 5_000
MAX_NICHE_TVL = 200_000
WALLET_AGE_DAYS = 30
TIMING_HOURS = 72
SPORTS_TIMING_HOURS = 24
LONGSHOT_THRESHOLD = 0.20
MIN_LONGSHOT_WIN_RATE = 0.60
MIN_RESOLVED_HISTORY = 8
MIN_RESOLVED_LONGSHOT_HISTORY = 3
VOLUME_SPIKE_FACTOR = 3.0
DEFAULT_MIN_CANDIDATE_SCORE = 20
DEFAULT_BUCKET_THRESHOLDS = {
    "insider": 40,
    "sports_news": 32,
    "momentum": 35,
    "contrarian": 35,
}

# ── Feature thresholds ─────────────────────────────────────────────────────────
CUMULATIVE_POSITION_MIN = 10_000
DECOY_HEDGE_RATIO = 0.10
BALANCED_HEDGE_RATIO = 0.35
QUICK_FLIP_HOURS = 24
QUICK_FLIP_MIN_USDC = 1_000
CAPITAL_IMPACT_RATIO = 0.10
MIXER_URGENCY_HOURS = 1.0
SWARM_HOURS = 2
SWARM_MIN_WALLETS = 3
MAX_FOLLOW_ENTRY_PRICE = 0.90
MIN_FOLLOW_REMAINING_EDGE = 0.10

BUCKET_LABELS = {
    "insider": "Insider Strategy",
    "sports_news": "Sports News Strategy",
    "momentum": "Momentum Strategy",
    "contrarian": "Contrarian Strategy",
}
BUCKET_PRIORITY = {
    "insider": 0,
    "sports_news": 1,
    "contrarian": 2,
    "momentum": 3,
}
FOLLOW_BUCKETS = {"insider", "sports_news", "momentum"}

TIER_THRESHOLDS = {
    "Tier 3": 76,
    "Tier 2": 56,
    "Tier 1": 40,
}
TIER_SIZING = {
    "Tier 3": ("3–5%", "Take profit at 0.85–0.90, exit 70–80% of position"),
    "Tier 2": ("2–3%", "Take profit at 0.85–0.90, exit 70–80% of position"),
    "Tier 1": ("1–2%", "Observation only — wait for more confirming signals"),
}


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


def _clamp(value, minimum=0, maximum=100):
    return max(minimum, min(maximum, value))


def _trade_usdc(trade: dict) -> float:
    raw = trade.get("usdcSize")
    if raw not in (None, ""):
        try:
            return float(raw)
        except Exception:
            pass

    size = float(trade.get("size") or 0)
    price = float(trade.get("price") or 0)
    if size and price:
        return size * price
    return size


def _normalize_outcome(trade: dict) -> str:
    outcome = str(trade.get("outcome") or trade.get("name") or "").strip().upper()
    return outcome or "UNKNOWN"


def wallet_from_trade(trade: dict) -> str:
    return (
        trade.get("proxyWallet")
        or trade.get("maker")
        or trade.get("transactor")
        or trade.get("address")
        or ""
    ).lower().strip()


def market_from_trade(trade: dict) -> str:
    return (
        trade.get("_market_address")
        or trade.get("conditionId")
        or trade.get("condition_id")
        or ""
    )


def trade_time(trade: dict):
    return _parse_dt(
        trade.get("timestamp")
        or trade.get("createdAt")
        or trade.get("created_at")
    )


def _weighted_price(trades: list[dict]) -> float | None:
    weighted_sum = 0.0
    total_usdc = 0.0
    for trade in trades:
        usdc = _trade_usdc(trade)
        price = float(trade.get("price") or 0)
        if usdc <= 0 or price <= 0:
            continue
        weighted_sum += usdc * price
        total_usdc += usdc
    if total_usdc <= 0:
        return None
    return weighted_sum / total_usdc


def _summarize_recent_trades(trades: list[dict]) -> list[dict]:
    rows = []
    for trade in sorted(trades, key=lambda item: trade_time(item) or datetime.min.replace(tzinfo=timezone.utc)):
        rows.append({
            "timestamp": (trade_time(trade) or datetime.now(timezone.utc)).isoformat(),
            "side": (trade.get("side") or "BUY").upper(),
            "outcome": _normalize_outcome(trade),
            "price": round(float(trade.get("price") or 0), 4),
            "amount_usdc": round(_trade_usdc(trade), 2),
        })
    return rows


def _opposite_outcome(outcome: str | None) -> str | None:
    if not outcome:
        return None
    outcome = outcome.upper()
    if outcome == "YES":
        return "NO"
    if outcome == "NO":
        return "YES"
    return None


def _detect_quick_flips(polymarket_activity: list[dict], market_id: str, dominant_outcome: str | None) -> list[dict]:
    events = []
    for activity in polymarket_activity:
        activity_market = (
            activity.get("conditionId")
            or activity.get("market")
            or activity.get("_market_address")
            or ""
        )
        if activity_market != market_id:
            continue
        dt = _parse_dt(activity.get("timestamp") or activity.get("createdAt") or activity.get("created_at"))
        if not dt:
            continue
        side = (activity.get("side") or activity.get("type") or "").upper()
        amount = _trade_usdc(activity)
        if amount < QUICK_FLIP_MIN_USDC:
            continue
        events.append({
            "dt": dt,
            "side": side,
            "amount": amount,
            "outcome": _normalize_outcome(activity),
        })

    events.sort(key=lambda item: item["dt"])
    open_buys = []
    flip_events = []

    for event in events:
        if event["side"] == "BUY":
            open_buys.append(event)
            continue
        if event["side"] != "SELL":
            continue

        matched = None
        for buy in open_buys:
            if dominant_outcome and buy["outcome"] not in {dominant_outcome, "UNKNOWN"}:
                continue
            if event["outcome"] not in {buy["outcome"], "UNKNOWN"}:
                continue
            hours_held = (event["dt"] - buy["dt"]).total_seconds() / 3600
            if 0 < hours_held <= QUICK_FLIP_HOURS:
                matched = {
                    "hours_held": round(hours_held, 1),
                    "buy_usdc": round(buy["amount"], 2),
                }
                break
        if matched:
            flip_events.append(matched)
            break

    return flip_events


def _extract_wallet_history(positions: list[dict]) -> dict:
    longshot_wins = 0
    longshot_total = 0
    total_wins = 0
    total_resolved = 0

    for position in positions:
        outcome = str(position.get("outcome") or "").lower()
        is_resolved = outcome in {"won", "lost", "redeemed", "expired"}
        if not is_resolved:
            continue

        total_resolved += 1
        cash_pnl = float(position.get("cashPnl") or 0)
        is_won = outcome in {"won", "redeemed"} or cash_pnl > 0
        if is_won:
            total_wins += 1

        avg_price_raw = position.get("avgPrice") or position.get("price")
        try:
            avg_price = float(avg_price_raw) if avg_price_raw not in (None, "") else None
        except Exception:
            avg_price = None

        if avg_price is not None and avg_price <= LONGSHOT_THRESHOLD:
            longshot_total += 1
            if is_won:
                longshot_wins += 1

    longshot_win_rate = (
        longshot_wins / longshot_total
        if longshot_total >= MIN_RESOLVED_LONGSHOT_HISTORY else None
    )
    overall_win_rate = (
        total_wins / total_resolved
        if total_resolved >= MIN_RESOLVED_HISTORY else None
    )

    return {
        "total_resolved": total_resolved,
        "total_wins": total_wins,
        "overall_win_rate": round(overall_win_rate, 2) if overall_win_rate is not None else None,
        "longshot_total": longshot_total,
        "longshot_wins": longshot_wins,
        "longshot_win_rate": round(longshot_win_rate, 2) if longshot_win_rate is not None else None,
        "overall_history_flag": bool(overall_win_rate is not None and overall_win_rate >= 0.62),
        "longshot_flag": bool(
            longshot_win_rate is not None
            and total_resolved >= MIN_RESOLVED_HISTORY
            and longshot_win_rate >= MIN_LONGSHOT_WIN_RATE
        ),
    }


def _max_window_move_pct(price_points: list[tuple[datetime, float]], window_hours: float = 1.0) -> float:
    max_move = 0.0
    for idx, (start_dt, start_price) in enumerate(price_points):
        if start_price <= 0:
            continue
        for end_dt, end_price in price_points[idx + 1:]:
            hours = (end_dt - start_dt).total_seconds() / 3600
            if hours > window_hours:
                break
            max_move = max(max_move, abs(end_price - start_price) / start_price)
    return max_move


def _market_price_context(
    market_trades: list[dict],
    dominant_outcome: str | None,
    first_alert_dt,
    last_alert_dt,
    entry_price: float | None,
) -> dict:
    outcome_trades = []
    for trade in market_trades:
        if dominant_outcome and _normalize_outcome(trade) != dominant_outcome:
            continue
        dt = trade_time(trade)
        price = float(trade.get("price") or 0)
        if dt and price > 0:
            outcome_trades.append((dt, price))

    outcome_trades.sort(key=lambda item: item[0])
    if not outcome_trades:
        return {
            "outcome_trade_count": 0,
            "price_move_pct": 0.0,
            "rapid_move_pct": 0.0,
            "pre_alert_price": None,
            "post_alert_price": None,
            "move_before_entry_pct": None,
            "follow_through_pct": None,
            "max_favorable_excursion_pct": None,
            "max_adverse_excursion_pct": None,
            "price_acceleration": False,
            "late_chaser": False,
            "follow_through": False,
            "mean_reversion": False,
            "overextended_move": False,
        }

    first_price = outcome_trades[0][1]
    last_price = outcome_trades[-1][1]
    price_move_pct = ((last_price - first_price) / first_price) if first_price else 0.0
    rapid_move_pct = _max_window_move_pct(outcome_trades, 1.0)

    pre_alert_points = [item for item in outcome_trades if item[0] < first_alert_dt]
    post_alert_points = [item for item in outcome_trades if item[0] > last_alert_dt]
    pre_alert_price = pre_alert_points[-1][1] if pre_alert_points else first_price
    post_alert_price = post_alert_points[-1][1] if post_alert_points else last_price

    move_before_entry_pct = None
    follow_through_pct = None
    max_favorable_excursion_pct = None
    max_adverse_excursion_pct = None
    if entry_price and entry_price > 0:
        if pre_alert_price:
            move_before_entry_pct = (entry_price - pre_alert_price) / pre_alert_price
        follow_through_pct = (post_alert_price - entry_price) / entry_price
        future_prices = [price for dt, price in outcome_trades if dt >= first_alert_dt]
        if future_prices:
            max_favorable_excursion_pct = (max(future_prices) - entry_price) / entry_price
            max_adverse_excursion_pct = (min(future_prices) - entry_price) / entry_price

    return {
        "outcome_trade_count": len(outcome_trades),
        "first_price": round(first_price, 4),
        "last_price": round(last_price, 4),
        "price_move_pct": round(price_move_pct, 4),
        "rapid_move_pct": round(rapid_move_pct, 4),
        "pre_alert_price": round(pre_alert_price, 4) if pre_alert_price is not None else None,
        "post_alert_price": round(post_alert_price, 4) if post_alert_price is not None else None,
        "move_before_entry_pct": round(move_before_entry_pct, 4) if move_before_entry_pct is not None else None,
        "follow_through_pct": round(follow_through_pct, 4) if follow_through_pct is not None else None,
        "max_favorable_excursion_pct": round(max_favorable_excursion_pct, 4) if max_favorable_excursion_pct is not None else None,
        "max_adverse_excursion_pct": round(max_adverse_excursion_pct, 4) if max_adverse_excursion_pct is not None else None,
        "price_acceleration": rapid_move_pct >= 0.12,
        "late_chaser": bool(move_before_entry_pct is not None and move_before_entry_pct >= 0.12),
        "follow_through": bool(follow_through_pct is not None and follow_through_pct >= 0.06),
        "mean_reversion": bool(follow_through_pct is not None and follow_through_pct <= -0.06),
        "overextended_move": abs(price_move_pct) >= 0.30 or rapid_move_pct >= 0.30,
    }


def _build_shared_features(
    address: str,
    market: dict,
    alert_trades: list[dict],
    market_trades: list[dict],
    polymarket_activity: list[dict],
    positions: list[dict],
    polygon_data: dict,
    arkham_data: dict,
    dune_whale_list: list[str],
    dune_new_wallet_list: list[str],
    swarm_cluster_size: int = 0,
) -> tuple[dict, dict, dict]:
    now = datetime.now(timezone.utc)
    alert_trades = sorted(alert_trades, key=lambda trade: trade_time(trade) or now)
    market_id = market.get("market_id") or market.get("conditionId") or market_from_trade(alert_trades[0])
    category = market.get("category") or alert_trades[0].get("_market_category") or "Other"
    market_name = market.get("market_name") or alert_trades[0].get("_market_name") or market_id
    market_end = market.get("market_end") or alert_trades[0].get("_market_end")
    market_end_dt = _parse_dt(market_end)
    market_liquidity = float(market.get("market_liquidity") or alert_trades[0].get("_market_liquidity") or 0)
    market_spike_ratio = float(market.get("spike_ratio") or alert_trades[0].get("_spike_ratio") or 0)

    outcome_buys = defaultdict(float)
    outcome_net = defaultdict(float)
    dominant_buy_trades = defaultdict(list)
    buy_usdc = 0.0
    sell_usdc = 0.0
    largest_trade_usdc = 0.0
    large_buy_count = 0
    first_trade_dt = None
    last_trade_dt = None
    repeated_adds = 0
    last_buy_dt = None

    for trade in alert_trades:
        dt = trade_time(trade)
        if dt:
            first_trade_dt = dt if first_trade_dt is None or dt < first_trade_dt else first_trade_dt
            last_trade_dt = dt if last_trade_dt is None or dt > last_trade_dt else last_trade_dt

        usdc = _trade_usdc(trade)
        largest_trade_usdc = max(largest_trade_usdc, usdc)
        outcome = _normalize_outcome(trade)
        side = (trade.get("side") or "BUY").upper()
        if side == "BUY":
            buy_usdc += usdc
            if usdc >= MIN_BET_USDC:
                large_buy_count += 1
            outcome_buys[outcome] += usdc
            dominant_buy_trades[outcome].append(trade)
            if last_buy_dt and dt and (dt - last_buy_dt) >= timedelta(minutes=15):
                repeated_adds += 1
            if dt:
                last_buy_dt = dt
            outcome_net[outcome] += usdc
        else:
            sell_usdc += usdc
            outcome_net[outcome] -= usdc

    positive_exposures = {
        outcome: amount
        for outcome, amount in outcome_net.items()
        if amount > 0
    }
    if positive_exposures:
        dominant_outcome, dominant_usdc = max(positive_exposures.items(), key=lambda item: item[1])
        secondary_usdc = sum(amount for outcome, amount in positive_exposures.items() if outcome != dominant_outcome)
    else:
        dominant_outcome, dominant_usdc, secondary_usdc = "UNKNOWN", 0.0, 0.0

    hedge_ratio = (secondary_usdc / dominant_usdc) if dominant_usdc > 0 else 0.0
    directional_conviction = bool(dominant_usdc > 0 and (len(positive_exposures) == 1 or hedge_ratio < DECOY_HEDGE_RATIO))
    balanced_outcomes = bool(dominant_usdc > 0 and hedge_ratio >= BALANCED_HEDGE_RATIO)
    decoy_exposure = bool(dominant_usdc > 0 and 0 < hedge_ratio < DECOY_HEDGE_RATIO and len(positive_exposures) > 1)
    entry_price = _weighted_price(dominant_buy_trades.get(dominant_outcome, []))
    remaining_edge_pct = max(0.0, 1 - entry_price) if entry_price is not None else None
    low_remaining_edge = bool(
        entry_price is not None and (
            entry_price >= MAX_FOLLOW_ENTRY_PRICE
            or remaining_edge_pct < MIN_FOLLOW_REMAINING_EDGE
        )
    )
    capital_impact_ratio = (dominant_usdc / market_liquidity) if market_liquidity > 0 else 0.0

    timing_hours = None
    if first_trade_dt and market_end_dt:
        timing_hours = (market_end_dt - first_trade_dt).total_seconds() / 3600
    true_timing = bool(timing_hours is not None and 0 < timing_hours <= TIMING_HOURS)
    sports_timing = bool(timing_hours is not None and 0 < timing_hours <= SPORTS_TIMING_HOURS)

    quick_flips = _detect_quick_flips(polymarket_activity, market_id, dominant_outcome)
    price_context = _market_price_context(
        market_trades=market_trades,
        dominant_outcome=dominant_outcome,
        first_alert_dt=first_trade_dt or now,
        last_alert_dt=last_trade_dt or now,
        entry_price=entry_price,
    )

    timestamps = []
    for activity in polymarket_activity:
        dt = _parse_dt(activity.get("timestamp") or activity.get("createdAt") or activity.get("created_at"))
        if dt:
            timestamps.append(dt)
    poly_first = polygon_data.get("first_tx_timestamp")
    if poly_first:
        timestamps.append(datetime.fromtimestamp(int(poly_first), tz=timezone.utc))
    wallet_age_days = (now - min(timestamps)).days if timestamps else None
    wallet_age_confident = bool(poly_first) or (0 < len(polymarket_activity) < 500)
    new_wallet = bool(wallet_age_confident and wallet_age_days is not None and wallet_age_days < WALLET_AGE_DAYS)

    historical_record = _extract_wallet_history(positions)

    funding_flags = polygon_data.get("funding_flags", [])
    inflows = polygon_data.get("usdc_inflows", [])
    mixer_flag = any("mixer" in flag.lower() for flag in funding_flags)
    bridge_flag = any("bridge" in flag.lower() for flag in funding_flags)

    mixer_urgency = False
    mixer_urgency_hours = None
    if inflows and mixer_flag and first_trade_dt:
        for inflow in inflows:
            inflow_dt = datetime.fromtimestamp(inflow.get("timestamp", 0), tz=timezone.utc)
            hours_gap = (first_trade_dt - inflow_dt).total_seconds() / 3600
            if 0 <= hours_gap <= MIXER_URGENCY_HOURS:
                mixer_urgency = True
                mixer_urgency_hours = round(hours_gap, 2)
                break

    arkham_label = arkham_data.get("label", "Unknown")
    arkham_type = arkham_data.get("type", "unknown")
    project_types = {"project", "treasury", "developer", "team", "fund", "institution"}
    arkham_project_link = bool(
        str(arkham_type).lower() in project_types
        or (arkham_label != "Unknown" and any(key in arkham_label.lower() for key in ["fund", "capital", "ventures"]))
    )
    coordinated_cluster = bool(arkham_data.get("cluster_size", 0) >= 3)

    addr_lower = address.lower()
    dune_whale_flag = addr_lower in {wallet.lower() for wallet in dune_whale_list if wallet}
    dune_new_wallet_flag = addr_lower in {wallet.lower() for wallet in dune_new_wallet_list if wallet}

    recent_buy_evidence = bool(large_buy_count or buy_usdc >= MIN_BET_USDC or dominant_usdc >= MIN_BET_USDC)
    candidate_score = 0
    if recent_buy_evidence:
        candidate_score += 12
    if largest_trade_usdc >= MIN_BET_USDC and market_liquidity and market_liquidity < MAX_NICHE_TVL:
        candidate_score += 10
    if capital_impact_ratio >= CAPITAL_IMPACT_RATIO:
        candidate_score += 12
    if dominant_usdc >= CUMULATIVE_POSITION_MIN:
        candidate_score += 8
    if directional_conviction:
        candidate_score += 12
    if true_timing:
        candidate_score += 10
    if swarm_cluster_size >= SWARM_MIN_WALLETS:
        candidate_score += 8
    if balanced_outcomes:
        candidate_score -= 10
    if quick_flips:
        candidate_score -= 15

    shared = {
        "trade_count": len(alert_trades),
        "recent_buy_usdc": round(buy_usdc, 2),
        "recent_sell_usdc": round(sell_usdc, 2),
        "gross_trade_usdc": round(buy_usdc + sell_usdc, 2),
        "largest_trade_usdc": round(largest_trade_usdc, 2),
        "large_trade_count": large_buy_count,
        "cumulative_position_usdc": round(dominant_usdc, 2),
        "capital_impact_ratio": round(capital_impact_ratio, 4),
        "capital_impact_pct": round(capital_impact_ratio * 100, 2),
        "market_liquidity": round(market_liquidity, 2),
        "market_spike_ratio": round(market_spike_ratio, 2),
        "directional_conviction": directional_conviction,
        "balanced_outcomes": balanced_outcomes,
        "decoy_exposure": decoy_exposure,
        "hedge_ratio": round(hedge_ratio, 4),
        "dominant_outcome": dominant_outcome,
        "dominant_entry_price": round(entry_price, 4) if entry_price is not None else None,
        "remaining_edge_pct": round(remaining_edge_pct, 4) if remaining_edge_pct is not None else None,
        "low_remaining_edge": low_remaining_edge,
        "timing_hours_to_resolution": round(timing_hours, 2) if timing_hours is not None else None,
        "true_timing": true_timing,
        "sports_event_timing": sports_timing,
        "swarm_cluster_size": swarm_cluster_size,
        "coordinated_swarm": swarm_cluster_size >= SWARM_MIN_WALLETS,
        "quick_flips": quick_flips,
        "quick_flip": bool(quick_flips),
        "wallet_age_days": wallet_age_days,
        "wallet_age_confident": wallet_age_confident,
        "new_wallet": new_wallet,
        "funding_flags": funding_flags,
        "mixer_flag": mixer_flag,
        "bridge_flag": bridge_flag,
        "mixer_urgency": mixer_urgency,
        "mixer_urgency_hours": mixer_urgency_hours,
        "arkham_label": arkham_label,
        "arkham_type": arkham_type,
        "arkham_project_link": arkham_project_link,
        "coordinated_cluster": coordinated_cluster,
        "arkham_cluster": arkham_data.get("cluster_size", 0),
        "related_wallets": arkham_data.get("related_addresses", []),
        "dune_whale_flag": dune_whale_flag,
        "dune_new_wallet_flag": dune_new_wallet_flag,
        "price_context": price_context,
        "candidate_score": candidate_score,
    }
    shared.update(historical_record)

    active_exposure = {
        "dominant_outcome": dominant_outcome,
        "dominant_usdc": round(dominant_usdc, 2),
        "secondary_usdc": round(secondary_usdc, 2),
        "hedge_ratio": round(hedge_ratio, 4),
        "net_outcome_exposures": {key: round(value, 2) for key, value in sorted(outcome_net.items())},
        "entry_price": round(entry_price, 4) if entry_price is not None else None,
        "buy_usdc": round(buy_usdc, 2),
        "sell_usdc": round(sell_usdc, 2),
    }

    metadata = {
        "market_id": market_id,
        "market_name": market_name,
        "category": category,
        "market_end": market_end,
        "market_liquidity": market_liquidity,
        "spike_ratio": market_spike_ratio,
        "generated_at": now.isoformat(),
    }
    return shared, active_exposure, metadata


def _score_insider(category: str, shared: dict) -> int:
    score = shared["candidate_score"]
    if category == "Sports":
        score -= 18
    if shared.get("new_wallet"):
        score += 8
    if shared.get("longshot_flag"):
        score += 15
    if shared.get("overall_history_flag"):
        score += 8
    if shared.get("bridge_flag"):
        score += 5
    if shared.get("mixer_flag"):
        score += 25
    if shared.get("mixer_urgency"):
        score += 15
    if shared.get("arkham_project_link"):
        score += 15
    if shared.get("coordinated_cluster"):
        score += 10
    if shared.get("dune_whale_flag"):
        score += 4
    if shared.get("dune_new_wallet_flag"):
        score += 6
    if shared.get("price_context", {}).get("late_chaser"):
        score -= 10
    if shared.get("price_context", {}).get("mean_reversion"):
        score -= 8
    return _clamp(score)


def _score_sports_news(category: str, shared: dict) -> int:
    if category != "Sports":
        return 0
    score = shared["candidate_score"]
    if shared.get("sports_event_timing"):
        score += 12
    if shared.get("capital_impact_ratio", 0) >= CAPITAL_IMPACT_RATIO:
        score += 8
    if shared.get("coordinated_swarm"):
        score += 8
    if shared.get("directional_conviction"):
        score += 8
    if shared.get("overall_history_flag"):
        score += 6
    if shared.get("price_context", {}).get("follow_through"):
        score += 6
    if shared.get("price_context", {}).get("late_chaser"):
        score -= 8
    if shared.get("balanced_outcomes"):
        score -= 8
    if shared.get("quick_flip"):
        score -= 12
    if shared.get("price_context", {}).get("mean_reversion"):
        score -= 10
    return _clamp(score)


def _score_momentum(shared: dict) -> int:
    score = min(shared["candidate_score"], 20)
    price_context = shared.get("price_context", {})
    if price_context.get("price_acceleration"):
        score += 10
    if shared.get("market_spike_ratio", 0) >= VOLUME_SPIKE_FACTOR:
        score += 8
    if price_context.get("follow_through"):
        score += 10
    if shared.get("trade_count", 0) >= 3:
        score += 6
    if shared.get("directional_conviction"):
        score += 8
    if shared.get("balanced_outcomes"):
        score -= 12
    if shared.get("quick_flip"):
        score -= 8
    if price_context.get("mean_reversion"):
        score -= 10
    if price_context.get("overextended_move") and not price_context.get("follow_through"):
        score -= 8
    return _clamp(score)


def _score_contrarian(category: str, shared: dict) -> int:
    price_context = shared.get("price_context", {})
    score = 0
    if price_context.get("overextended_move"):
        score += 12
    if shared.get("market_spike_ratio", 0) >= VOLUME_SPIKE_FACTOR:
        score += 8
    if price_context.get("late_chaser"):
        score += 10
    if price_context.get("mean_reversion"):
        score += 10
    if not shared.get("directional_conviction"):
        score += 6
    if shared.get("balanced_outcomes"):
        score += 6
    if shared.get("true_timing") and price_context.get("overextended_move"):
        score += 6
    if shared.get("quick_flip"):
        score += 4
    if shared.get("arkham_project_link") or shared.get("mixer_flag"):
        score -= 15
    if shared.get("longshot_flag") or shared.get("overall_history_flag"):
        score -= 8
    if category == "Sports" and shared.get("sports_event_timing") and shared.get("directional_conviction"):
        score -= 10
    if shared.get("coordinated_swarm"):
        score -= 6
    return _clamp(score)


def _choose_bucket(bucket_scores: dict[str, int], thresholds: dict[str, int]) -> tuple[str, int, int]:
    best_bucket = sorted(
        bucket_scores,
        key=lambda bucket: (-bucket_scores[bucket], BUCKET_PRIORITY[bucket]),
    )[0]
    return best_bucket, bucket_scores[best_bucket], thresholds[best_bucket]


def _build_core_reasons(bucket: str, shared: dict, active_exposure: dict) -> list[str]:
    reasons = []
    if shared.get("directional_conviction"):
        reasons.append(f"One-sided exposure into {active_exposure.get('dominant_outcome', 'UNKNOWN')}")
    if shared.get("capital_impact_ratio", 0) >= CAPITAL_IMPACT_RATIO:
        reasons.append(f"Position size is {shared['capital_impact_pct']:.1f}% of market liquidity")
    if shared.get("true_timing"):
        reasons.append(f"Trade landed {shared.get('timing_hours_to_resolution')}h before resolution")
    if shared.get("coordinated_swarm"):
        reasons.append(f"Joined a {shared.get('swarm_cluster_size', 0)}-wallet cluster inside {SWARM_HOURS}h")
    if bucket == "insider" and shared.get("arkham_project_link"):
        reasons.append("Arkham links the wallet to a project/fund-style entity")
    if bucket == "insider" and shared.get("mixer_flag"):
        reasons.append("Funding flow includes mixer-style behavior")
    if bucket == "sports_news" and shared.get("sports_event_timing"):
        reasons.append("Sports entry landed inside the late-news event window")
    if bucket == "sports_news" and shared.get("price_context", {}).get("follow_through"):
        reasons.append("Price continued moving after the entry")
    if bucket == "momentum" and shared.get("price_context", {}).get("price_acceleration"):
        reasons.append("Outcome price accelerated sharply in the last hour")
    if bucket == "momentum" and shared.get("price_context", {}).get("follow_through"):
        reasons.append("The move kept running after the alert entry")
    if bucket == "contrarian" and shared.get("price_context", {}).get("overextended_move"):
        reasons.append("The market already looks overstretched")
    if bucket == "contrarian" and shared.get("price_context", {}).get("late_chaser"):
        reasons.append("Entry came after a large price move")
    if bucket == "contrarian" and shared.get("price_context", {}).get("mean_reversion"):
        reasons.append("Price has started reversing after the entry")
    return reasons[:4]


def _build_caution_flags(shared: dict) -> list[str]:
    cautions = []
    if shared.get("balanced_outcomes"):
        cautions.append("Exposure is split across outcomes")
    if shared.get("quick_flip"):
        cautions.append("Wallet showed quick-flip behavior in this market")
    if not shared.get("wallet_age_confident"):
        cautions.append("Wallet age may be truncated by limited history")
    if shared.get("price_context", {}).get("mean_reversion"):
        cautions.append("Price has already started mean-reverting")
    if shared.get("price_context", {}).get("late_chaser"):
        cautions.append("Entry may be late relative to the move")
    if shared.get("low_remaining_edge"):
        cautions.append("Entry is too close to 1.00 to leave much remaining edge")
    return cautions


def _build_action(bucket: str, dominant_outcome: str | None) -> tuple[str, str | None]:
    opposite = _opposite_outcome(dominant_outcome)
    if bucket in FOLLOW_BUCKETS:
        if dominant_outcome:
            return "follow", dominant_outcome
        return "follow", None
    if opposite:
        return "fade", opposite
    return "fade", None


def _build_alert_id(address: str, market_id: str, first_trade_dt) -> str:
    minute_key = ""
    if first_trade_dt:
        minute_key = first_trade_dt.replace(second=0, microsecond=0).isoformat()
    raw = f"{address.lower()}|{market_id}|{minute_key}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def score_alert(
    address: str,
    market: dict,
    alert_trades: list[dict],
    market_trades: list[dict],
    polymarket_activity: list[dict],
    positions: list[dict],
    polygon_data: dict,
    arkham_data: dict,
    dune_whale_list: list[str],
    dune_new_wallet_list: list[str],
    swarm_cluster_size: int = 0,
    min_candidate_score: int = DEFAULT_MIN_CANDIDATE_SCORE,
    bucket_thresholds: dict[str, int] | None = None,
) -> dict:
    bucket_thresholds = bucket_thresholds or DEFAULT_BUCKET_THRESHOLDS
    shared, active_exposure, metadata = _build_shared_features(
        address=address,
        market=market,
        alert_trades=alert_trades,
        market_trades=market_trades,
        polymarket_activity=polymarket_activity,
        positions=positions,
        polygon_data=polygon_data,
        arkham_data=arkham_data,
        dune_whale_list=dune_whale_list,
        dune_new_wallet_list=dune_new_wallet_list,
        swarm_cluster_size=swarm_cluster_size,
    )

    category = metadata["category"]
    candidate_score = shared["candidate_score"]
    is_candidate = bool(
        active_exposure.get("dominant_usdc", 0) >= MIN_BET_USDC
        and candidate_score >= min_candidate_score
    )

    bucket_scores = {
        "insider": _score_insider(category, shared),
        "sports_news": _score_sports_news(category, shared),
        "momentum": _score_momentum(shared),
        "contrarian": _score_contrarian(category, shared),
    }
    best_bucket, best_score, bucket_threshold = _choose_bucket(bucket_scores, bucket_thresholds)
    strategy_label = BUCKET_LABELS[best_bucket]
    review_status = "pending"
    alert_time = trade_time(alert_trades[0]) if alert_trades else None
    alert_id = _build_alert_id(address, metadata["market_id"], alert_time)

    if best_score >= TIER_THRESHOLDS["Tier 3"]:
        tier = "Tier 3"
    elif best_score >= TIER_THRESHOLDS["Tier 2"]:
        tier = "Tier 2"
    elif best_score >= TIER_THRESHOLDS["Tier 1"]:
        tier = "Tier 1"
    else:
        tier = None
    tier_sizing, tier_exit = TIER_SIZING.get(tier, ("—", "—")) if tier else ("—", "—")

    action_mode, suggested_outcome = _build_action(best_bucket, active_exposure.get("dominant_outcome"))
    review_anchor_price = active_exposure.get("entry_price")
    thin_edge_follow = bool(action_mode == "follow" and shared.get("low_remaining_edge"))
    passes_strategy_threshold = bool(is_candidate and best_score >= bucket_threshold)
    strategy_blockers = []

    return {
        "alert_id": alert_id,
        "wallet_address": address,
        "market_id": metadata["market_id"],
        "market_name": metadata["market_name"],
        "category": category,
        "market_end": metadata["market_end"],
        "generated_at": metadata["generated_at"],
        "review_status": review_status,
        "recent_trades": _summarize_recent_trades(alert_trades),
        "active_exposure": active_exposure,
        "shared_features": shared,
        "score_breakdown": shared,
        "bucket_scores": bucket_scores,
        "best_bucket": best_bucket,
        "best_score": best_score,
        "bucket_threshold": bucket_threshold,
        "candidate_score": candidate_score,
        "candidate_threshold": min_candidate_score,
        "is_candidate": is_candidate,
        "passes_strategy_threshold": passes_strategy_threshold,
        "strategy_blockers": strategy_blockers,
        "thin_edge_follow": thin_edge_follow,
        "strategy_bucket": best_bucket,
        "strategy_label": strategy_label,
        "strategy_score": best_score,
        "strategy_threshold": bucket_threshold,
        "suspicion_score": best_score,
        "core_reasons": _build_core_reasons(best_bucket, shared, active_exposure),
        "caution_flags": _build_caution_flags(shared),
        "entity_label": shared.get("arkham_label", "Unknown"),
        "entity_type": shared.get("arkham_type", "unknown"),
        "historical_record": {
            "total_resolved": shared.get("total_resolved", 0),
            "total_wins": shared.get("total_wins", 0),
            "overall_win_rate": shared.get("overall_win_rate"),
            "longshot_total": shared.get("longshot_total", 0),
            "longshot_wins": shared.get("longshot_wins", 0),
            "longshot_win_rate": shared.get("longshot_win_rate"),
        },
        "polygon_tx_count": polygon_data.get("tx_count", 0),
        "funding_warnings": shared.get("funding_flags", []),
        "tier": tier,
        "tier_sizing": tier_sizing,
        "tier_exit": tier_exit,
        "recommended_action": action_mode,
        "suggested_outcome": suggested_outcome,
        "review_anchor_price": review_anchor_price,
        "review_label": None,
    }


def bucket_label(bucket: str) -> str:
    return BUCKET_LABELS.get(bucket, bucket.title())
