"""
tuning.py — Builds zero-cost tuning artifacts from existing run outputs.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from src.scorer import _parse_dt

TUNING_SUMMARY_PATH = "tuning_summary.json"
TUNING_CHECKLIST_PATH = "tuning_checklist.md"

BUCKETS = ["insider", "sports_news", "momentum", "contrarian"]
BUCKET_LABELS = {
    "insider": "Insider",
    "sports_news": "Sports News",
    "momentum": "Momentum",
    "contrarian": "Contrarian",
}

MIN_RESOLVED_BUCKET_TUNING = 12
MIN_RESOLVED_FEATURE_TUNING = 20
MIN_FEATURE_WINS_OR_LOSSES = 8
THRESHOLD_ADJUSTMENT_STEP = 3
RECENT_RUNS_WINDOW = 7


def _round_or_none(value, digits=4):
    if value is None:
        return None
    return round(value, digits)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percent(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return round(part / whole, 4)


def _ratio(entry: dict, key: str) -> float | None:
    base = entry.get("entry_price")
    value = entry.get(key)
    if base in (None, 0) or value is None:
        return None
    return (value - base) / base


def _run_key(raw: str | None) -> str:
    dt = _parse_dt(raw)
    if not dt:
        return str(raw or "unknown")
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return dt.isoformat()


def _build_bucket_funnel(candidate_pool: list[dict], watchlist: list[dict], review_entries: list[dict]) -> dict:
    candidate_counts = defaultdict(int)
    watchlist_counts = defaultdict(int)
    recent_watchlist_counts = defaultdict(int)

    for alert in candidate_pool:
        candidate_counts[alert.get("best_bucket", "unknown")] += 1
    for alert in watchlist:
        watchlist_counts[alert.get("best_bucket", "unknown")] += 1

    recent_run_keys = sorted(
        {_run_key(entry.get("generated_at")) for entry in review_entries if entry.get("generated_at")},
        reverse=True,
    )[:RECENT_RUNS_WINDOW]
    recent_run_key_set = set(recent_run_keys)
    for entry in review_entries:
        bucket = entry.get("bucket", "unknown")
        if _run_key(entry.get("generated_at")) in recent_run_key_set:
            recent_watchlist_counts[bucket] += 1

    funnel = {}
    for bucket in BUCKETS:
        candidates = candidate_counts.get(bucket, 0)
        watchlist_alerts = watchlist_counts.get(bucket, 0)
        funnel[bucket] = {
            "candidate_winners": candidates,
            "watchlist_alerts": watchlist_alerts,
            "candidate_to_watchlist_conversion": _percent(watchlist_alerts, candidates),
            "watchlist_alerts_last_7_runs": recent_watchlist_counts.get(bucket, 0),
        }
    return funnel


def _build_bucket_performance(review_entries: list[dict]) -> dict:
    grouped = defaultdict(list)
    for entry in review_entries:
        grouped[entry.get("bucket", "unknown")].append(entry)

    performance = {}
    for bucket in BUCKETS:
        rows = grouped.get(bucket, [])
        resolved = [row for row in rows if row.get("review_status") in {"resolved_win", "resolved_loss"}]
        wins = [row for row in resolved if row.get("review_status") == "resolved_win"]

        returns = []
        move_1h = []
        move_6h = []
        move_24h = []
        favorable = []
        adverse = []

        for row in rows:
            one_hour = _ratio(row, "price_after_1h")
            six_hour = _ratio(row, "price_after_6h")
            day_move = _ratio(row, "price_after_24h")
            if one_hour is not None:
                move_1h.append(one_hour)
            if six_hour is not None:
                move_6h.append(six_hour)
            if day_move is not None:
                move_24h.append(day_move)
            if row.get("max_favorable_excursion") is not None:
                favorable.append(row["max_favorable_excursion"])
            if row.get("max_adverse_excursion") is not None:
                adverse.append(row["max_adverse_excursion"])

        for row in resolved:
            base = row.get("entry_price")
            final = row.get("price_at_resolution")
            if base in (None, 0) or final is None:
                continue
            returns.append((final - base) / base)

        performance[bucket] = {
            "alerts_logged": len(rows),
            "resolved_alerts": len(resolved),
            "wins": len(wins),
            "win_rate": _percent(len(wins), len(resolved)),
            "average_return": _round_or_none(_mean(returns)),
            "average_move_1h": _round_or_none(_mean(move_1h)),
            "average_move_6h": _round_or_none(_mean(move_6h)),
            "average_move_24h": _round_or_none(_mean(move_24h)),
            "average_max_favorable_excursion": _round_or_none(_mean(favorable)),
            "average_max_adverse_excursion": _round_or_none(_mean(adverse)),
        }
    return performance


def _risk_shape(candidate_pool: list[dict]) -> dict:
    overall_total = len(candidate_pool)
    grouped = defaultdict(list)
    for alert in candidate_pool:
        grouped[alert.get("best_bucket", "unknown")].append(alert)

    risk_keys = [
        ("quick_flip", "quick_flip_pct"),
        ("balanced_outcomes", "balanced_outcomes_pct"),
        ("late_chaser", "late_chaser_pct"),
        ("coordinated_swarm", "coordinated_swarm_pct"),
        ("true_timing", "true_timing_pct"),
    ]

    def summarize(alerts: list[dict]) -> dict:
        total = len(alerts)
        rows = {}
        for feature_key, label in risk_keys:
            matches = 0
            for alert in alerts:
                shared = alert.get("shared_features", {})
                if feature_key == "late_chaser":
                    value = bool(shared.get("price_context", {}).get("late_chaser"))
                else:
                    value = bool(shared.get(feature_key))
                if value:
                    matches += 1
            rows[label] = _percent(matches, total)
        return rows

    return {
        "overall": summarize(candidate_pool),
        "by_bucket": {bucket: summarize(grouped.get(bucket, [])) for bucket in BUCKETS},
    }


def _feature_combo_leaderboard(review_entries: list[dict]) -> dict:
    combos = defaultdict(lambda: {"wins": 0, "losses": 0, "resolved": 0})
    for entry in review_entries:
        status = entry.get("review_status")
        if status not in {"resolved_win", "resolved_loss"}:
            continue
        combo = tuple(sorted(entry.get("feature_tags") or []))
        combos[combo]["resolved"] += 1
        if status == "resolved_win":
            combos[combo]["wins"] += 1
        else:
            combos[combo]["losses"] += 1

    rows = []
    for combo, stats in combos.items():
        if stats["resolved"] <= 0:
            continue
        rows.append({
            "feature_combo": list(combo),
            "resolved": stats["resolved"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate": round(stats["wins"] / stats["resolved"], 4),
        })
    rows.sort(key=lambda row: (row["win_rate"], row["resolved"]), reverse=True)
    return {
        "best": rows[:5],
        "worst": list(reversed(rows[-5:])),
    }


def _review_backlog(review_entries: list[dict], now: datetime) -> dict:
    buckets = {"<24h": 0, "1-3d": 0, "3-7d": 0, "7d+": 0}
    pending_by_bucket = {bucket: {"pending": 0, "expired": 0} for bucket in BUCKETS}

    for entry in review_entries:
        bucket = entry.get("bucket", "unknown")
        status = entry.get("review_status", "pending")
        if bucket in pending_by_bucket and status in {"pending", "expired"}:
            pending_by_bucket[bucket][status] += 1

        if status != "pending":
            continue
        generated_at = _parse_dt(entry.get("generated_at"))
        if not generated_at:
            continue
        age = now - generated_at.astimezone(timezone.utc)
        if age.total_seconds() < 86400:
            buckets["<24h"] += 1
        elif age.total_seconds() < 3 * 86400:
            buckets["1-3d"] += 1
        elif age.total_seconds() < 7 * 86400:
            buckets["3-7d"] += 1
        else:
            buckets["7d+"] += 1

    return {
        "pending_alert_ages": buckets,
        "pending_by_bucket": pending_by_bucket,
    }


def _feature_recommendations(review_entries: list[dict], bucket_performance: dict, threshold_decisions: dict) -> list[dict]:
    grouped = defaultdict(list)
    for entry in review_entries:
        if entry.get("review_status") in {"resolved_win", "resolved_loss"}:
            grouped[entry.get("bucket", "unknown")].append(entry)

    suggestions = []
    for bucket in BUCKETS:
        if threshold_decisions[bucket]["action"].startswith("Tune Threshold"):
            continue
        rows = grouped.get(bucket, [])
        if len(rows) < MIN_RESOLVED_FEATURE_TUNING:
            continue

        baseline = bucket_performance[bucket].get("win_rate")
        if baseline is None:
            continue

        feature_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "resolved": 0})
        for row in rows:
            status = row.get("review_status")
            for feature in row.get("feature_tags") or []:
                feature_stats[feature]["resolved"] += 1
                if status == "resolved_win":
                    feature_stats[feature]["wins"] += 1
                else:
                    feature_stats[feature]["losses"] += 1

        ranked = []
        for feature, stats in feature_stats.items():
            if stats["resolved"] < MIN_FEATURE_WINS_OR_LOSSES:
                continue
            feature_win_rate = stats["wins"] / stats["resolved"]
            delta = feature_win_rate - baseline
            action = None
            if stats["losses"] >= MIN_FEATURE_WINS_OR_LOSSES and delta <= -0.15:
                action = "Tune Weight Down"
            elif stats["wins"] >= MIN_FEATURE_WINS_OR_LOSSES and delta >= 0.15:
                action = "Tune Weight Up"
            if action:
                ranked.append({
                    "bucket": bucket,
                    "feature": feature,
                    "action": action,
                    "resolved": stats["resolved"],
                    "wins": stats["wins"],
                    "losses": stats["losses"],
                    "feature_win_rate": round(feature_win_rate, 4),
                    "bucket_baseline_win_rate": baseline,
                    "delta_vs_bucket": round(delta, 4),
                })

        ranked.sort(key=lambda row: abs(row["delta_vs_bucket"]), reverse=True)
        if ranked:
            suggestions.append(ranked[0])
    return suggestions


def _bucket_threshold_decisions(funnel: dict, performance: dict, thresholds: dict) -> dict:
    decisions = {}
    for bucket in BUCKETS:
        label = BUCKET_LABELS[bucket]
        resolved = performance[bucket]["resolved_alerts"]
        win_rate = performance[bucket]["win_rate"]
        average_return = performance[bucket]["average_return"]
        adverse = performance[bucket]["average_max_adverse_excursion"]
        conversion = funnel[bucket]["candidate_to_watchlist_conversion"] or 0
        recent_watchlist_count = funnel[bucket]["watchlist_alerts_last_7_runs"]
        current_threshold = thresholds.get(bucket)

        action = "Hold"
        reason = f"{label} has fewer than {MIN_RESOLVED_BUCKET_TUNING} resolved alerts; sample too small."
        suggested_threshold = current_threshold

        if resolved >= MIN_RESOLVED_BUCKET_TUNING:
            if win_rate is not None and win_rate < 0.45 and conversion > 0.50:
                action = "Tune Threshold Up"
                suggested_threshold = current_threshold + THRESHOLD_ADJUSTMENT_STEP if current_threshold is not None else None
                reason = (
                    f"{label} win rate is {win_rate:.0%} with {resolved} resolved alerts and "
                    f"{conversion:.0%} candidate-to-watchlist conversion."
                )
            elif win_rate is not None and win_rate > 0.60 and recent_watchlist_count < 3:
                action = "Tune Threshold Down"
                suggested_threshold = current_threshold - THRESHOLD_ADJUSTMENT_STEP if current_threshold is not None else None
                reason = (
                    f"{label} win rate is {win_rate:.0%} but only {recent_watchlist_count} watchlist alerts "
                    f"landed across the last {RECENT_RUNS_WINDOW} runs."
                )
            elif (
                win_rate is not None
                and 0.45 <= win_rate <= 0.60
                and average_return is not None
                and average_return >= 0
            ):
                action = "Hold"
                reason = f"{label} is inside the target win-rate band with non-negative average return."
            elif (
                win_rate is not None
                and win_rate >= 0.45
                and adverse is not None
                and adverse <= -0.10
            ):
                action = "Observe"
                reason = f"{label} win rate is acceptable, but adverse excursion is worsening at {adverse:.0%}."
            else:
                action = "Observe"
                reason = f"{label} has mixed signals; keep observing before changing thresholds."

        decisions[bucket] = {
            "action": action,
            "reason": reason,
            "current_threshold": current_threshold,
            "suggested_threshold": suggested_threshold,
        }
    return decisions


def _observation_questions(summary: dict) -> list[str]:
    funnel = summary["bucket_funnel"]
    performance = summary["bucket_performance"]
    risk = summary["risk_shape_indicators"]["by_bucket"]
    questions = []

    sports_watchlist = funnel["sports_news"]["watchlist_alerts"]
    sports_resolved = performance["sports_news"]["resolved_alerts"]
    questions.append(
        f"Sports backfill check: sports_news produced {sports_watchlist} watchlist alerts this run and has {sports_resolved} resolved alerts overall."
    )

    momentum_move = performance["momentum"]["average_move_24h"]
    momentum_late = risk["momentum"]["late_chaser_pct"]
    questions.append(
        f"Momentum follow-through check: 24h expectancy is {momentum_move if momentum_move is not None else 'N/A'} with late_chaser on {momentum_late if momentum_late is not None else 'N/A'} of momentum candidates."
    )

    contrarian_watchlist = funnel["contrarian"]["watchlist_alerts"]
    contrarian_resolved = performance["contrarian"]["resolved_alerts"]
    questions.append(
        f"Contrarian selectivity check: contrarian produced {contrarian_watchlist} watchlist alerts this run and has {contrarian_resolved} resolved alerts."
    )

    overall_risk = summary["risk_shape_indicators"]["overall"]
    questions.append(
        "Loss-shape check: monitor whether quick_flip, balanced_outcomes, and late_chaser continue clustering in losing alerts."
    )

    best_bucket = max(
        BUCKETS,
        key=lambda bucket: performance[bucket]["average_move_24h"] if performance[bucket]["average_move_24h"] is not None else float("-inf"),
    )
    best_move = performance[best_bucket]["average_move_24h"]
    questions.append(
        f"24h expectancy check: {BUCKET_LABELS[best_bucket]} currently leads at {best_move if best_move is not None else 'N/A'} average 24h move."
    )

    sparse_buckets = [bucket for bucket in BUCKETS if funnel[bucket]["watchlist_alerts_last_7_runs"] < 3]
    if sparse_buckets:
        questions.append(
            "Threshold scarcity check: " +
            ", ".join(BUCKET_LABELS[bucket] for bucket in sparse_buckets) +
            " has low watchlist volume across the last 7 runs."
        )
    return questions


def build_tuning_summary(watchlist_payload: dict, review_entries: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    candidate_pool = watchlist_payload.get("candidate_pool", [])
    watchlist = watchlist_payload.get("watchlist", [])
    stats = watchlist_payload.get("stats", {})
    thresholds = watchlist_payload.get("bucket_thresholds", {})

    funnel = _build_bucket_funnel(candidate_pool, watchlist, review_entries)
    performance = _build_bucket_performance(review_entries)
    threshold_decisions = _bucket_threshold_decisions(funnel, performance, thresholds)
    feature_actions = _feature_recommendations(review_entries, performance, threshold_decisions)
    risk = _risk_shape(candidate_pool)
    leaderboard = _feature_combo_leaderboard(review_entries)
    backlog = _review_backlog(review_entries, now)

    suggested_actions = []
    for bucket in BUCKETS:
        action = threshold_decisions[bucket]["action"]
        if action.startswith("Tune"):
            target = threshold_decisions[bucket].get("suggested_threshold")
            suggested_actions.append(
                f"{action}: {BUCKET_LABELS[bucket]} ({threshold_decisions[bucket]['reason']})"
                + (f" Suggested threshold: {target}." if target is not None else "")
            )
        elif action == "Observe":
            suggested_actions.append(f"Observe: {threshold_decisions[bucket]['reason']}")
        else:
            suggested_actions.append(f"Hold: {threshold_decisions[bucket]['reason']}")

    for action in feature_actions:
        suggested_actions.append(
            f"{action['action']}: {BUCKET_LABELS[action['bucket']]} feature `{action['feature']}` "
            f"has {action['feature_win_rate']:.0%} win rate vs {action['bucket_baseline_win_rate']:.0%} bucket baseline."
        )

    summary = {
        "generated_at": watchlist_payload.get("generated_at") or now.isoformat(),
        "source_artifacts": {
            "watchlist": "watchlist.json",
            "review_log": watchlist_payload.get("review_log_path", "review_log.json"),
        },
        "decision_policy": {
            "minimum_resolved_for_bucket_tuning": MIN_RESOLVED_BUCKET_TUNING,
            "minimum_resolved_for_feature_tuning": MIN_RESOLVED_FEATURE_TUNING,
            "threshold_adjustment_step": THRESHOLD_ADJUSTMENT_STEP,
            "max_threshold_changes_per_bucket_per_week": 1,
            "weight_changes_only_on_weekly_review": True,
            "prefer_threshold_changes_before_weight_changes": True,
            "one_variable_change_per_bucket_per_cycle": True,
        },
        "run_health": {
            "markets_scanned": stats.get("markets_scanned", 0),
            "flagged_markets": stats.get("flagged_markets", 0),
            "large_trades": stats.get("large_trades", 0),
            "wallets_evaluated": stats.get("wallets_evaluated", 0),
            "alerts_scored": stats.get("alerts_scored", 0),
            "candidate_alerts": stats.get("candidate_alerts", 0),
            "watchlist_alerts": stats.get("flagged_alerts", 0),
        },
        "bucket_funnel": funnel,
        "bucket_performance": performance,
        "risk_shape_indicators": risk,
        "feature_combo_leaderboard": leaderboard,
        "review_backlog": backlog,
        "tuning_recommendations": {
            "bucket_decisions": threshold_decisions,
            "feature_actions": feature_actions,
            "suggested_actions": suggested_actions,
            "observation_questions": _observation_questions({
                "bucket_funnel": funnel,
                "bucket_performance": performance,
                "risk_shape_indicators": risk,
            }),
        },
    }
    return summary


def render_tuning_checklist(summary: dict) -> str:
    run = summary["run_health"]
    funnel = summary["bucket_funnel"]
    performance = summary["bucket_performance"]
    risk = summary["risk_shape_indicators"]
    recommendations = summary["tuning_recommendations"]
    backlog = summary["review_backlog"]["pending_alert_ages"]

    lines = [
        "# Tuning Checklist",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## 1. Run Health",
        f"- Markets scanned: {run['markets_scanned']}",
        f"- Flagged markets: {run['flagged_markets']}",
        f"- Large trades: {run['large_trades']}",
        f"- Wallets evaluated: {run['wallets_evaluated']}",
        f"- Alerts scored: {run['alerts_scored']}",
        f"- Candidate alerts: {run['candidate_alerts']}",
        f"- Watchlist alerts: {run['watchlist_alerts']}",
        "",
        "## 2. Bucket Funnel",
    ]

    for bucket in BUCKETS:
        row = funnel[bucket]
        conversion = row["candidate_to_watchlist_conversion"]
        conversion_text = f"{conversion:.0%}" if conversion is not None else "N/A"
        lines.append(
            f"- {BUCKET_LABELS[bucket]}: {row['candidate_winners']} candidates, "
            f"{row['watchlist_alerts']} watchlist, {conversion_text} conversion, "
            f"{row['watchlist_alerts_last_7_runs']} alerts across last 7 runs"
        )

    lines.extend([
        "",
        "## 3. Bucket Performance",
    ])
    for bucket in BUCKETS:
        row = performance[bucket]
        win_rate = f"{row['win_rate']:.0%}" if row["win_rate"] is not None else "N/A"
        avg_return = f"{row['average_return']:.1%}" if row["average_return"] is not None else "N/A"
        move_24h = f"{row['average_move_24h']:.1%}" if row["average_move_24h"] is not None else "N/A"
        adverse = f"{row['average_max_adverse_excursion']:.1%}" if row["average_max_adverse_excursion"] is not None else "N/A"
        lines.append(
            f"- {BUCKET_LABELS[bucket]}: {row['resolved_alerts']} resolved, {win_rate} win rate, "
            f"{avg_return} average return, {move_24h} average 24h move, {adverse} average adverse excursion"
        )

    lines.extend([
        "",
        "## 4. Risk Flags",
        f"- Overall quick_flip rate: {risk['overall']['quick_flip_pct']:.0%}" if risk["overall"]["quick_flip_pct"] is not None else "- Overall quick_flip rate: N/A",
        f"- Overall balanced_outcomes rate: {risk['overall']['balanced_outcomes_pct']:.0%}" if risk["overall"]["balanced_outcomes_pct"] is not None else "- Overall balanced_outcomes rate: N/A",
        f"- Overall late_chaser rate: {risk['overall']['late_chaser_pct']:.0%}" if risk["overall"]["late_chaser_pct"] is not None else "- Overall late_chaser rate: N/A",
        f"- Overall coordinated_swarm rate: {risk['overall']['coordinated_swarm_pct']:.0%}" if risk["overall"]["coordinated_swarm_pct"] is not None else "- Overall coordinated_swarm rate: N/A",
        f"- Overall true_timing rate: {risk['overall']['true_timing_pct']:.0%}" if risk["overall"]["true_timing_pct"] is not None else "- Overall true_timing rate: N/A",
        f"- Pending review backlog: <24h {backlog['<24h']}, 1-3d {backlog['1-3d']}, 3-7d {backlog['3-7d']}, 7d+ {backlog['7d+']}",
        "",
        "## 5. Suggested Actions",
    ])
    for item in recommendations["suggested_actions"]:
        lines.append(f"- {item}")
    for question in recommendations["observation_questions"]:
        lines.append(f"- Observe next: {question}")

    lines.extend([
        "",
        "## 6. Hold / Observe / Tune Decisions",
    ])
    for bucket in BUCKETS:
        decision = recommendations["bucket_decisions"][bucket]
        threshold_note = ""
        if decision["action"].startswith("Tune Threshold") and decision.get("suggested_threshold") is not None:
            threshold_note = f" Move threshold from {decision.get('current_threshold')} to {decision.get('suggested_threshold')}."
        lines.append(f"- {decision['action']}: {BUCKET_LABELS[bucket]} — {decision['reason']}{threshold_note}")
    for action in recommendations["feature_actions"]:
        lines.append(
            f"- {action['action']}: {BUCKET_LABELS[action['bucket']]} feature `{action['feature']}` "
            f"({action['resolved']} resolved, {action['feature_win_rate']:.0%} win rate)."
        )

    return "\n".join(lines) + "\n"


def write_tuning_artifacts(
    watchlist_payload: dict,
    review_entries: list[dict],
    summary_path: str = TUNING_SUMMARY_PATH,
    checklist_path: str = TUNING_CHECKLIST_PATH,
    now: datetime | None = None,
) -> tuple[dict, str]:
    summary = build_tuning_summary(watchlist_payload, review_entries, now=now)
    checklist = render_tuning_checklist(summary)

    with open(summary_path, "w") as handle:
        json.dump(summary, handle, indent=2)
    with open(checklist_path, "w") as handle:
        handle.write(checklist)

    return summary, checklist

