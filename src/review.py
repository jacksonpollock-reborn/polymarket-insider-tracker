"""
review.py — Durable review log for strategy alerts.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.scorer import _normalize_outcome, _parse_dt, trade_time

DEFAULT_REVIEW_LOG_PATH = "review_log.json"


def load_review_log(path: str = DEFAULT_REVIEW_LOG_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as handle:
            payload = json.load(handle)
        if isinstance(payload, list):
            return payload
    except Exception:
        return []
    return []


def _save_review_log(entries: list[dict], path: str = DEFAULT_REVIEW_LOG_PATH) -> None:
    with open(path, "w") as handle:
        json.dump(entries, handle, indent=2)


def _feature_tags(alert: dict) -> list[str]:
    shared = alert.get("shared_features", {})
    price = shared.get("price_context", {})
    tags = []
    if shared.get("directional_conviction"):
        tags.append("directional_conviction")
    if shared.get("capital_impact_ratio", 0) >= 0.10:
        tags.append("capital_impact")
    if shared.get("true_timing"):
        tags.append("true_timing")
    if shared.get("coordinated_swarm"):
        tags.append("coordinated_swarm")
    if shared.get("sports_event_timing"):
        tags.append("sports_event_timing")
    if price.get("price_acceleration"):
        tags.append("price_acceleration")
    if price.get("late_chaser"):
        tags.append("late_chaser")
    if price.get("mean_reversion"):
        tags.append("mean_reversion")
    if shared.get("arkham_project_link"):
        tags.append("arkham_project_link")
    if shared.get("mixer_flag"):
        tags.append("mixer_flag")
    return tags[:5]


def _anchor_entry_price(alert: dict) -> float | None:
    active = alert.get("active_exposure", {})
    dominant_outcome = active.get("dominant_outcome")
    entry_price = active.get("entry_price")
    suggested_outcome = alert.get("suggested_outcome")
    if entry_price is None:
        return None
    if dominant_outcome in {"YES", "NO"} and suggested_outcome in {"YES", "NO"} and suggested_outcome != dominant_outcome:
        return round(1 - float(entry_price), 4)
    return float(entry_price)


def _upsert_entries(existing_entries: list[dict], alerts: list[dict]) -> dict[str, dict]:
    entries_by_id = {entry.get("alert_id"): entry for entry in existing_entries if entry.get("alert_id")}

    for alert in alerts:
        if alert["alert_id"] in entries_by_id:
            continue
        entries_by_id[alert["alert_id"]] = {
            "alert_id": alert["alert_id"],
            "generated_at": alert.get("generated_at"),
            "bucket": alert.get("best_bucket"),
            "wallet_address": alert.get("wallet_address"),
            "market_id": alert.get("market_id"),
            "market_name": alert.get("market_name"),
            "category": alert.get("category"),
            "market_end": alert.get("market_end"),
            "dominant_outcome": alert.get("active_exposure", {}).get("dominant_outcome"),
            "suggested_outcome": alert.get("suggested_outcome"),
            "recommended_action": alert.get("recommended_action"),
            "entry_price": _anchor_entry_price(alert),
            "price_after_1h": None,
            "price_after_6h": None,
            "price_after_24h": None,
            "price_at_resolution": None,
            "max_favorable_excursion": None,
            "max_adverse_excursion": None,
            "resolved_outcome": None,
            "review_label": alert.get("review_label"),
            "review_status": alert.get("review_status", "pending"),
            "feature_tags": _feature_tags(alert),
        }
    return entries_by_id


def _outcome_price_points(trades: list[dict], outcome: str | None) -> list[tuple[datetime, float]]:
    rows = []
    for trade in trades:
        if outcome and _normalize_outcome(trade) != outcome:
            continue
        dt = trade_time(trade)
        price = float(trade.get("price") or 0)
        if dt and price > 0:
            rows.append((dt, price))
    rows.sort(key=lambda item: item[0])
    return rows


def _price_at(points: list[tuple[datetime, float]], target_dt) -> float | None:
    if not points:
        return None
    after = [price for dt, price in points if dt >= target_dt]
    if after:
        return round(after[0], 4)
    before = [price for dt, price in points if dt <= target_dt]
    if before:
        return round(before[-1], 4)
    return None


def _max_excursions(points: list[tuple[datetime, float]], start_dt, entry_price: float | None) -> tuple[float | None, float | None]:
    if entry_price is None or entry_price <= 0:
        return None, None
    future = [price for dt, price in points if dt >= start_dt]
    if not future:
        return None, None
    favorable = (max(future) - entry_price) / entry_price
    adverse = (min(future) - entry_price) / entry_price
    return round(favorable, 4), round(adverse, 4)


def _update_entry(entry: dict, market_trades: list[dict], now) -> dict:
    generated_dt = _parse_dt(entry.get("generated_at"))
    market_end_dt = _parse_dt(entry.get("market_end"))
    outcome = entry.get("suggested_outcome") or entry.get("dominant_outcome")
    entry_price = entry.get("entry_price")
    points = _outcome_price_points(market_trades, outcome)

    if generated_dt:
        if now >= generated_dt + timedelta(hours=1) and entry.get("price_after_1h") is None:
            entry["price_after_1h"] = _price_at(points, generated_dt + timedelta(hours=1))
        if now >= generated_dt + timedelta(hours=6) and entry.get("price_after_6h") is None:
            entry["price_after_6h"] = _price_at(points, generated_dt + timedelta(hours=6))
        if now >= generated_dt + timedelta(hours=24) and entry.get("price_after_24h") is None:
            entry["price_after_24h"] = _price_at(points, generated_dt + timedelta(hours=24))
        if entry.get("max_favorable_excursion") is None or entry.get("max_adverse_excursion") is None:
            favorable, adverse = _max_excursions(points, generated_dt, entry_price)
            entry["max_favorable_excursion"] = favorable
            entry["max_adverse_excursion"] = adverse

    if market_end_dt and now >= market_end_dt and entry.get("price_at_resolution") is None:
        entry["price_at_resolution"] = _price_at(points, market_end_dt)

    resolution_price = entry.get("price_at_resolution")
    if resolution_price is not None:
        if resolution_price >= 0.95:
            entry["resolved_outcome"] = "won"
            entry["review_status"] = "resolved_win"
        elif resolution_price <= 0.05:
            entry["resolved_outcome"] = "lost"
            entry["review_status"] = "resolved_loss"
        elif market_end_dt and now >= market_end_dt + timedelta(hours=12):
            entry["review_status"] = "expired"
    elif market_end_dt and now >= market_end_dt + timedelta(hours=12):
        entry["review_status"] = "expired"

    return entry


def _movement_ratio(entry: dict, key: str) -> float | None:
    base = entry.get("entry_price")
    value = entry.get(key)
    if base is None or value is None or base <= 0:
        return None
    return (value - base) / base


def summarize_review_log(entries: list[dict]) -> dict:
    by_bucket = defaultdict(list)
    for entry in entries:
        by_bucket[entry.get("bucket", "unknown")].append(entry)

    bucket_summary = {}
    combo_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "count": 0})

    for bucket, rows in by_bucket.items():
        resolved = [row for row in rows if row.get("review_status") in {"resolved_win", "resolved_loss"}]
        wins = [row for row in resolved if row.get("review_status") == "resolved_win"]
        returns = []
        moves = []

        for row in resolved:
            if row.get("entry_price") and row.get("price_at_resolution") is not None:
                returns.append((row["price_at_resolution"] - row["entry_price"]) / row["entry_price"])
            move = _movement_ratio(row, "price_after_24h")
            if move is None:
                move = _movement_ratio(row, "price_after_6h")
            if move is None:
                move = _movement_ratio(row, "price_after_1h")
            if move is not None:
                moves.append(move)

            combo = ",".join(sorted(row.get("feature_tags") or [])) or "none"
            combo_stats[combo]["count"] += 1
            if row.get("review_status") == "resolved_win":
                combo_stats[combo]["wins"] += 1
            elif row.get("review_status") == "resolved_loss":
                combo_stats[combo]["losses"] += 1

        bucket_summary[bucket] = {
            "alerts_logged": len(rows),
            "resolved_alerts": len(resolved),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(resolved), 3) if resolved else None,
            "average_return": round(sum(returns) / len(returns), 4) if returns else None,
            "average_move_after_alert": round(sum(moves) / len(moves), 4) if moves else None,
        }

    ranked_combos = []
    for combo, stats in combo_stats.items():
        resolved = stats["wins"] + stats["losses"]
        if resolved == 0:
            continue
        ranked_combos.append({
            "feature_combo": combo.split(",") if combo != "none" else [],
            "resolved": resolved,
            "win_rate": round(stats["wins"] / resolved, 3),
        })
    ranked_combos.sort(key=lambda row: (row["win_rate"], row["resolved"]), reverse=True)

    return {
        "total_logged_alerts": len(entries),
        "bucket_performance": bucket_summary,
        "top_feature_combinations": ranked_combos[:5],
        "worst_feature_combinations": list(reversed(ranked_combos[-5:])),
    }


def sync_review_log(
    alerts: list[dict],
    market_trade_cache: dict[str, list[dict]],
    fetch_market_trades,
    path: str = DEFAULT_REVIEW_LOG_PATH,
) -> tuple[list[dict], dict]:
    now = datetime.now(timezone.utc)
    entries_by_id = _upsert_entries(load_review_log(path), alerts)

    market_ids = {
        entry.get("market_id")
        for entry in entries_by_id.values()
        if entry.get("market_id")
        and entry.get("review_status") not in {"resolved_win", "resolved_loss", "manual_skip"}
    }
    for market_id in market_ids:
        if market_id not in market_trade_cache:
            market_trade_cache[market_id] = fetch_market_trades(market_id)

    updated_entries = []
    for alert_id, entry in entries_by_id.items():
        trades = market_trade_cache.get(entry.get("market_id"), [])
        updated_entries.append(_update_entry(entry, trades, now))

    updated_entries.sort(key=lambda row: row.get("generated_at") or "", reverse=True)
    _save_review_log(updated_entries, path)
    return updated_entries, summarize_review_log(updated_entries)

