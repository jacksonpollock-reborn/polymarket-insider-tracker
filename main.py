"""
main.py — Orchestrates the full strategy-alert pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from src.fetchers import (
    batch_scan_arb,
    fetch_active_markets,
    fetch_arkham_entity,
    fetch_clob_book,
    fetch_dune_new_large_bettors,
    fetch_dune_whale_wallets,
    fetch_market_trades,
    fetch_markets_by_tags,
    fetch_polygon_tx_history,
    fetch_wallet_activity,
    fetch_wallet_positions,
    get_request_health,
    reset_request_health,
)
from src.reporter import send_email, send_telegram_alerts, write_html_report
from src.review import (
    DEFAULT_REVIEW_LOG_PATH,
    load_review_log,
    summarize_review_log,
    sync_review_log,
)
from src.scorer import (
    DEFAULT_BUCKET_THRESHOLDS,
    DEFAULT_MIN_CANDIDATE_SCORE,
    MAX_NICHE_TVL,
    MIN_BET_USDC,
    SWARM_HOURS,
    SWARM_MIN_WALLETS,
    VOLUME_SPIKE_FACTOR,
    market_from_trade,
    score_alert,
    trade_time,
    wallet_from_trade,
)
from src.tuning import (
    TUNING_CHECKLIST_PATH,
    TUNING_SUMMARY_PATH,
    write_tuning_artifacts,
)
from src.paper_trader import update_paper_portfolio, portfolio_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

MIN_CANDIDATE_SCORE = int(os.environ.get("MIN_CANDIDATE_SCORE", str(DEFAULT_MIN_CANDIDATE_SCORE)))
MAX_WALLETS_TO_SCORE = int(os.environ.get("MAX_WALLETS_TO_SCORE", "60"))
MARKET_TRADE_LIMIT = int(os.environ.get("MARKET_TRADE_LIMIT", "100"))
BOOTSTRAP_RETRY_COUNT = int(os.environ.get("BOOTSTRAP_RETRY_COUNT", "1"))
BOOTSTRAP_RETRY_DELAY_SECONDS = int(os.environ.get("BOOTSTRAP_RETRY_DELAY_SECONDS", "90"))

BUCKET_THRESHOLDS = {
    "insider": int(os.environ.get("MIN_INSIDER_CONFIDENCE", str(DEFAULT_BUCKET_THRESHOLDS["insider"]))),
    "sports_news": int(os.environ.get("MIN_SPORTS_CONFIDENCE", str(DEFAULT_BUCKET_THRESHOLDS["sports_news"]))),
    "momentum": int(os.environ.get("MIN_MOMENTUM_SCORE", str(DEFAULT_BUCKET_THRESHOLDS["momentum"]))),
    "contrarian": int(os.environ.get("MIN_CONTRARIAN_SCORE", str(DEFAULT_BUCKET_THRESHOLDS["contrarian"]))),
}

_raw_preferred = os.environ.get("PREFERRED_TAGS", "politics,crypto,economics,science,culture,sports")
_raw_excluded = os.environ.get("EXCLUDED_CATEGORIES", "")
PREFERRED_TAGS = [t.strip() for t in _raw_preferred.split(",") if t.strip()]
EXCLUDED_CATEGORIES = {c.strip() for c in _raw_excluded.split(",") if c.strip()}


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


def flag_suspicious_markets(markets: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=30)
    flagged = []

    for market in markets:
        try:
            end_raw = market.get("endDateIso") or market.get("endDate")
            if not end_raw:
                continue
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt <= now or end_dt > cutoff:
                continue

            volume_24h = float(market.get("volume24hr") or 0)
            volume_total = float(market.get("volume") or 0)
            liquidity = float(market.get("liquidity") or 0)
            avg_7d = volume_total / 7 if volume_total else 0
            spike = (volume_24h / avg_7d) if avg_7d > 0 else 0

            if (
                (liquidity < MAX_NICHE_TVL and volume_24h > MIN_BET_USDC)
                or spike >= VOLUME_SPIKE_FACTOR
                or volume_24h > MIN_BET_USDC * 5
            ):
                market["_spike_ratio"] = round(spike, 2)
                market["_is_niche"] = liquidity < MAX_NICHE_TVL
                market["_days_to_end"] = round((end_dt - now).total_seconds() / 86400, 1)
                flagged.append(market)
        except Exception:
            continue
    return flagged


SPORTS_KEYWORDS = {
    "nba", "nfl", "nhl", "mlb", "ncaa", "soccer", "tennis",
    "football", "basketball", "baseball", "hockey", "golf", "ufc", "mma",
    "esports", "match", "game", "vs.", "vs ", "spread", "o/u", "over/under",
    "champions league", "premier league", "bundesliga", "serie a", "la liga",
    "world cup", "super bowl", "playoffs", "tournament", "race",
}
CRYPTO_KEYWORDS = {"bitcoin", "btc", "eth", "ethereum", "crypto", "token", "sol", "price"}
POLITICS_KEYWORDS = {
    "election", "president", "senate", "congress", "vote", "trump", "biden",
    "resign", "impeach", "policy", "war", "military", "iran", "israel",
    "ukraine", "russia", "china", "fed", "interest rate",
}


def _text_has_keyword(text: str, keyword: str) -> bool:
    keyword = keyword.lower()
    if any(ch in keyword for ch in {" ", "/", ".", "-", "+"}):
        return keyword in text
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def _contains_any_keyword(text: str, keywords: set[str]) -> bool:
    return any(_text_has_keyword(text, keyword) for keyword in keywords)


def _detect_market_category(market: dict) -> str:
    tags = [t.lower() for t in (market.get("tags") or [])]
    question = (market.get("question") or market.get("title") or "").lower()
    text = question + " " + " ".join(tags)

    for tag in tags:
        if tag in {"sports", "nba", "nfl", "nhl", "mlb", "ncaa", "soccer", "tennis", "esports", "basketball", "football", "baseball", "hockey", "golf", "ufc"}:
            return "Sports"
        if tag in {"crypto", "bitcoin", "ethereum", "defi"}:
            return "Crypto"
        if tag in {"politics", "elections", "us-politics", "geopolitics"}:
            return "Politics"
        if tag in {"finance", "economics", "fed", "rates"}:
            return "Finance"

    if _contains_any_keyword(text, POLITICS_KEYWORDS):
        return "Politics"
    if _contains_any_keyword(text, CRYPTO_KEYWORDS):
        return "Crypto"
    if _contains_any_keyword(text, SPORTS_KEYWORDS):
        return "Sports"
    return "Other"


def _annotate_trade(trade: dict, market: dict) -> dict:
    row = dict(trade)
    row["_market_name"] = market.get("question") or market.get("title") or market.get("conditionId")
    row["_market_address"] = market.get("conditionId") or market.get("condition_id")
    row["_market_liquidity"] = float(market.get("liquidity") or 0)
    row["_market_end"] = market.get("endDateIso") or market.get("endDate")
    row["_spike_ratio"] = market.get("_spike_ratio", 0)
    row["_market_category"] = market.get("_detected_category") or _detect_market_category(market)
    row["usdcSize"] = _trade_usdc(row)
    return row


def _build_alert_candidates(market_trade_cache: dict[str, list[dict]]) -> tuple[dict[tuple[str, str], list[dict]], dict[str, float], int]:
    grouped = {}
    wallet_rank = defaultdict(float)
    large_trade_count = 0

    for market_id, trades in market_trade_cache.items():
        by_wallet = defaultdict(list)
        for trade in trades:
            addr = wallet_from_trade(trade)
            if not addr.startswith("0x") or len(addr) != 42:
                continue
            by_wallet[(addr, market_id)].append(trade)
            if _trade_usdc(trade) >= MIN_BET_USDC:
                large_trade_count += 1

        for key, wallet_trades in by_wallet.items():
            buy_usdc = sum(
                _trade_usdc(trade)
                for trade in wallet_trades
                if (trade.get("side") or "BUY").upper() == "BUY"
            )
            largest_buy = max(
                (
                    _trade_usdc(trade)
                    for trade in wallet_trades
                    if (trade.get("side") or "BUY").upper() == "BUY"
                ),
                default=0,
            )
            if buy_usdc < MIN_BET_USDC and largest_buy < MIN_BET_USDC:
                continue
            grouped[key] = sorted(wallet_trades, key=lambda item: trade_time(item) or datetime.min.replace(tzinfo=timezone.utc))
            wallet_rank[key[0]] += max(buy_usdc, largest_buy)

    return grouped, wallet_rank, large_trade_count


def _detect_swarm_clusters(alert_candidates: dict[tuple[str, str], list[dict]]) -> dict[tuple[str, str], int]:
    market_entries = defaultdict(list)
    for (wallet, market_id), trades in alert_candidates.items():
        first_dt = next((trade_time(trade) for trade in trades if trade_time(trade)), None)
        if first_dt:
            market_entries[market_id].append((wallet, first_dt))

    swarm_sizes = {}
    for market_id, entries in market_entries.items():
        entries.sort(key=lambda item: item[1])
        for idx, (start_wallet, start_dt) in enumerate(entries):
            cluster = {start_wallet}
            for wallet, dt in entries[idx + 1:]:
                hours = (dt - start_dt).total_seconds() / 3600
                if hours > SWARM_HOURS:
                    break
                cluster.add(wallet)
            if len(cluster) >= SWARM_MIN_WALLETS:
                for wallet in cluster:
                    swarm_sizes[(wallet, market_id)] = max(swarm_sizes.get((wallet, market_id), 0), len(cluster))
    return swarm_sizes


def _market_payload(market: dict) -> dict:
    return {
        "market_id": market.get("conditionId") or market.get("condition_id"),
        "market_name": market.get("question") or market.get("title") or "",
        "market_end": market.get("endDateIso") or market.get("endDate"),
        "market_liquidity": float(market.get("liquidity") or 0),
        "category": market.get("_detected_category") or _detect_market_category(market),
        "spike_ratio": float(market.get("_spike_ratio") or 0),
    }


def _fetch_market_universe() -> list[dict]:
    targeted_markets = fetch_markets_by_tags(PREFERRED_TAGS, per_tag_limit=50) if PREFERRED_TAGS else []
    targeted_ids = {m.get("conditionId") or m.get("condition_id") or m.get("id") for m in targeted_markets}

    top_volume_markets = fetch_active_markets(limit=200)
    for market in top_volume_markets:
        market_id = market.get("conditionId") or market.get("condition_id") or market.get("id")
        if market_id and market_id not in targeted_ids:
            targeted_markets.append(market)
            targeted_ids.add(market_id)

    for market in targeted_markets:
        market["_detected_category"] = _detect_market_category(market)
    return targeted_markets


def _apply_market_filters(markets: list[dict]) -> list[dict]:
    pre_filter_count = len(markets)
    if EXCLUDED_CATEGORIES:
        markets = [market for market in markets if market["_detected_category"] not in EXCLUDED_CATEGORIES]
        log.info(f"  → Dropped {pre_filter_count - len(markets)} excluded markets")
    return markets


def _build_output_payload(
    generated_at: str,
    stats: dict,
    candidate_pool: list[dict],
    watchlist: list[dict],
    review_summary: dict,
    run_health: dict | None = None,
) -> dict:
    return {
        "generated_at": generated_at,
        "stats": stats,
        "run_health": run_health or {"status": "healthy", "reason": None, "request_health": get_request_health()},
        "bucket_thresholds": BUCKET_THRESHOLDS,
        "candidate_pool": candidate_pool,
        "watchlist": watchlist,
        "review_summary": review_summary,
        "review_log_path": DEFAULT_REVIEW_LOG_PATH,
        "tuning_summary_path": TUNING_SUMMARY_PATH,
        "tuning_checklist_path": TUNING_CHECKLIST_PATH,
        "html_report_path": "report.html",
    }


def _write_output(payload: dict) -> None:
    output_path = "watchlist.json"
    with open(output_path, "w") as handle:
        json.dump(payload, handle, indent=2)
    log.info(f"  → Saved to {output_path}")


def _current_run_health(status: str = "healthy", reason: str | None = None) -> dict:
    return {
        "status": status,
        "reason": reason,
        "request_health": get_request_health(),
    }


def _finalize_empty_run(
    *,
    run_started_at: str,
    stats: dict,
    review_entries: list[dict],
    arb_alerts: list[dict] | None = None,
    unhealthy_reason: str | None = None,
) -> None:
    arb_alerts = arb_alerts or []
    payload = _build_output_payload(
        generated_at=run_started_at,
        stats=stats,
        candidate_pool=[],
        watchlist=[],
        review_summary=summarize_review_log(review_entries),
        run_health=_current_run_health(
            status="unhealthy" if unhealthy_reason else "healthy",
            reason=unhealthy_reason,
        ),
    )
    _write_output(payload)
    write_tuning_artifacts(payload, review_entries)
    write_html_report([], stats, arb_alerts=arb_alerts, run_health=payload["run_health"])
    email_ok = send_email([], stats, arb_alerts=arb_alerts, run_health=payload["run_health"])
    send_telegram_alerts([], stats, arb_alerts=arb_alerts)
    if unhealthy_reason:
        log.error(f"Run marked unhealthy: {unhealthy_reason}")
        sys.exit(1)
    if not email_ok:
        log.error("Empty-report delivery failed")
        sys.exit(1)


def run():
    run_started_at = datetime.now(timezone.utc).isoformat()
    reset_request_health()
    log.info("═══════════════════════════════════════════════════════")
    log.info("  Polymarket Strategy Tracker — starting scan")
    log.info("═══════════════════════════════════════════════════════")
    log.info(f"  Candidate threshold : {MIN_CANDIDATE_SCORE} pts")
    log.info(f"  Bucket thresholds   : {BUCKET_THRESHOLDS}")
    log.info(f"  Max wallets to score: {MAX_WALLETS_TO_SCORE}")

    stats = {
        "markets_scanned": 0,
        "flagged_markets": 0,
        "large_trades": 0,
        "wallets_evaluated": 0,
        "alerts_scored": 0,
        "candidate_alerts": 0,
        "flagged_alerts": 0,
        "candidate_wallets": 0,
        "flagged_wallets": 0,
        "insider_watchlist": 0,
        "sports_watchlist": 0,
        "momentum_watchlist": 0,
        "contrarian_watchlist": 0,
        "data_sources_active": 0,
        "arb_opportunities": 0,
        "bucket_watchlist_counts": {
            "insider": 0,
            "sports_news": 0,
            "momentum": 0,
            "contrarian": 0,
        },
    }

    log.info("\n[1/7] Fetching active Polymarket markets…")
    log.info(f"  Preferred tags : {PREFERRED_TAGS}")
    log.info(f"  Excluded cats  : {EXCLUDED_CATEGORIES}")

    markets = []
    for attempt in range(BOOTSTRAP_RETRY_COUNT + 1):
        if attempt:
            log.warning(
                f"  → Retrying market bootstrap in {BOOTSTRAP_RETRY_DELAY_SECONDS}s "
                f"(attempt {attempt + 1}/{BOOTSTRAP_RETRY_COUNT + 1})"
            )
            time.sleep(BOOTSTRAP_RETRY_DELAY_SECONDS)
        markets = _apply_market_filters(_fetch_market_universe())
        stats["markets_scanned"] = len(markets)
        log.info(f"  → {len(markets)} total unique markets fetched")

        request_health = get_request_health()
        if markets or request_health["failed_calls"] == 0 or attempt >= BOOTSTRAP_RETRY_COUNT:
            break
        log.warning(
            "  → Market bootstrap returned 0 markets after upstream request failures; "
            "waiting before retrying once."
        )

    request_health = get_request_health()
    if not markets and request_health["failed_calls"] > 0:
        log.error("Market bootstrap failed due to upstream request errors; writing unhealthy empty run.")
        review_entries = load_review_log(DEFAULT_REVIEW_LOG_PATH)
        _finalize_empty_run(
            run_started_at=run_started_at,
            stats=stats,
            review_entries=review_entries,
            unhealthy_reason=f"market_bootstrap_failed: {request_health.get('last_error') or 'unknown request error'}",
        )
        return

    log.info("\n[1b/7] Scanning CLOB for arbitrage opportunities…")
    arb_alerts = batch_scan_arb(markets, limit=80)
    stats["arb_opportunities"] = len(arb_alerts)
    if arb_alerts:
        log.info(f"  → {len(arb_alerts)} arb opportunities found")
    else:
        log.info("  → No arb gaps detected")

    flagged_markets = flag_suspicious_markets(markets)
    stats["flagged_markets"] = len(flagged_markets)
    log.info(f"  → {len(flagged_markets)} markets flagged for deep scan")

    if not flagged_markets and not arb_alerts:
        log.warning("No watchlist candidates or arb opportunities today. Sending empty report.")
        review_entries = load_review_log(DEFAULT_REVIEW_LOG_PATH)
        _finalize_empty_run(
            run_started_at=run_started_at,
            stats=stats,
            review_entries=review_entries,
            arb_alerts=[],
        )
        return

    stats["data_sources_active"] += 1

    log.info("\n[2/7] Querying Dune Analytics…")
    dune_whale_list = fetch_dune_whale_wallets()
    dune_new_wallet_list = fetch_dune_new_large_bettors()
    if dune_whale_list or dune_new_wallet_list:
        stats["data_sources_active"] += 1
        log.info(f"  → {len(dune_whale_list)} whale wallets, {len(dune_new_wallet_list)} new large bettors")
    else:
        log.warning("  → Dune returned no data")

    log.info("\n[3/7] Fetching market trades for flagged markets…")
    market_lookup = {}
    market_trade_cache = {}
    for market in flagged_markets:
        market_id = market.get("conditionId") or market.get("condition_id")
        if not market_id:
            continue
        market_lookup[market_id] = _market_payload(market)
        trades = fetch_market_trades(market_id, limit=MARKET_TRADE_LIMIT)
        market_trade_cache[market_id] = [_annotate_trade(trade, market) for trade in trades]

    alert_candidates, wallet_rank, large_trade_count = _build_alert_candidates(market_trade_cache)
    swarm_sizes = _detect_swarm_clusters(alert_candidates)
    stats["large_trades"] = large_trade_count
    log.info(f"  → {large_trade_count} large trades and {len(alert_candidates)} wallet+market alert candidates")

    request_health = get_request_health()
    trade_fetch_failed = (
        stats["flagged_markets"] > 0
        and request_health["failed_calls"] > 0
        and all(not trades for trades in market_trade_cache.values())
    )
    if trade_fetch_failed and not arb_alerts:
        log.error("Flagged market deep scan failed due to upstream request errors; writing unhealthy empty run.")
        review_entries = load_review_log(DEFAULT_REVIEW_LOG_PATH)
        _finalize_empty_run(
            run_started_at=run_started_at,
            stats=stats,
            review_entries=review_entries,
            arb_alerts=[],
            unhealthy_reason=f"market_trade_fetch_failed: {request_health.get('last_error') or 'unknown request error'}",
        )
        return

    if not alert_candidates and not arb_alerts:
        log.warning("No wallet+market candidates today. Sending report with arb only if available.")
        review_entries = load_review_log(DEFAULT_REVIEW_LOG_PATH)
        _finalize_empty_run(
            run_started_at=run_started_at,
            stats=stats,
            review_entries=review_entries,
            arb_alerts=arb_alerts,
        )
        return

    ranked_wallets = sorted(
        wallet_rank,
        key=lambda wallet: wallet_rank[wallet],
        reverse=True,
    )[:MAX_WALLETS_TO_SCORE]
    selected_wallets = set(ranked_wallets)
    stats["wallets_evaluated"] = len(selected_wallets)
    log.info(f"  → {len(selected_wallets)} wallets selected for enrichment")

    log.info("\n[4/7] Enriching selected wallets…")
    wallet_context = {}
    poly_active = False
    arkham_active = False
    for idx, wallet in enumerate(ranked_wallets, 1):
        log.info(f"  [{idx}/{len(ranked_wallets)}] {wallet[:10]}…")
        activity = fetch_wallet_activity(wallet)
        positions = fetch_wallet_positions(wallet)
        polygon = fetch_polygon_tx_history(wallet)
        arkham = fetch_arkham_entity(wallet)
        wallet_context[wallet] = {
            "activity": activity,
            "positions": positions,
            "polygon": polygon,
            "arkham": arkham,
        }
        if activity or positions:
            poly_active = True
        if arkham.get("label") and arkham.get("label") != "Unknown":
            arkham_active = True

    if poly_active:
        stats["data_sources_active"] += 1
    if arkham_active:
        stats["data_sources_active"] += 1

    log.info("\n[5/7] Scoring wallet+market alerts across four buckets…")
    candidate_pool = []
    watchlist = []
    candidate_wallets = set()

    scored_keys = sorted(
        [key for key in alert_candidates if key[0] in selected_wallets],
        key=lambda key: (
            -sum(_trade_usdc(trade) for trade in alert_candidates[key]),
            key[0],
            key[1],
        ),
    )

    for wallet, market_id in scored_keys:
        context = wallet_context[wallet]
        record = score_alert(
            address=wallet,
            market=market_lookup[market_id],
            alert_trades=alert_candidates[(wallet, market_id)],
            market_trades=market_trade_cache.get(market_id, []),
            polymarket_activity=context["activity"],
            positions=context["positions"],
            polygon_data=context["polygon"],
            arkham_data=context["arkham"],
            dune_whale_list=dune_whale_list,
            dune_new_wallet_list=dune_new_wallet_list,
            swarm_cluster_size=swarm_sizes.get((wallet, market_id), 0),
            min_candidate_score=MIN_CANDIDATE_SCORE,
            bucket_thresholds=BUCKET_THRESHOLDS,
        )
        record["generated_at"] = run_started_at
        record["run_id"] = run_started_at
        candidate_pool.append(record)
        if record["is_candidate"]:
            candidate_wallets.add(wallet)
        if record["passes_strategy_threshold"]:
            watchlist.append(record)
            stats["bucket_watchlist_counts"][record["best_bucket"]] += 1
            if record["best_bucket"] == "insider":
                stats["insider_watchlist"] += 1
            elif record["best_bucket"] == "sports_news":
                stats["sports_watchlist"] += 1
            elif record["best_bucket"] == "momentum":
                stats["momentum_watchlist"] += 1
            elif record["best_bucket"] == "contrarian":
                stats["contrarian_watchlist"] += 1

    stats["alerts_scored"] = len(candidate_pool)
    stats["candidate_alerts"] = sum(1 for alert in candidate_pool if alert["is_candidate"])
    stats["flagged_alerts"] = len(watchlist)
    stats["candidate_wallets"] = len(candidate_wallets)
    stats["flagged_wallets"] = len(watchlist)

    log.info(
        f"  → {stats['candidate_alerts']} stage-1 candidates · "
        f"{stats['flagged_alerts']} watchlist alerts"
    )

    log.info("\n[6/7] Updating durable review log…")
    review_entries, review_summary = sync_review_log(
        alerts=watchlist,
        market_trade_cache=market_trade_cache,
        fetch_market_trades=fetch_market_trades,
        path=DEFAULT_REVIEW_LOG_PATH,
    )
    review_map = {entry["alert_id"]: entry for entry in review_entries}
    for alert in candidate_pool:
        if alert["alert_id"] in review_map:
            alert["review_status"] = review_map[alert["alert_id"]]["review_status"]
    for alert in watchlist:
        if alert["alert_id"] in review_map:
            alert["review_status"] = review_map[alert["alert_id"]]["review_status"]

    candidate_pool.sort(key=lambda alert: (-alert["best_score"], -alert["candidate_score"]))
    watchlist.sort(key=lambda alert: (-alert["best_score"], -alert["candidate_score"]))

    payload = _build_output_payload(
        generated_at=run_started_at,
        stats=stats,
        candidate_pool=candidate_pool,
        watchlist=watchlist,
        review_summary=review_summary,
        run_health=_current_run_health(),
    )
    _write_output(payload)
    write_tuning_artifacts(payload, review_entries)
    write_html_report(watchlist, stats, arb_alerts=arb_alerts, run_health=payload["run_health"])

    log.info("\n[7/7] Sending reports…")
    ok = send_email(watchlist, stats, arb_alerts=arb_alerts, run_health=payload["run_health"])
    if ok:
        log.info("  → Email delivered successfully")
    else:
        log.error("  → Email failed")
        sys.exit(1)

    tg_ok = send_telegram_alerts(watchlist, stats, arb_alerts=arb_alerts)
    if tg_ok:
        log.info("  → Telegram alerts sent")
    else:
        log.warning("  → Telegram skipped")

    log.info("\n[Paper] Updating paper trading portfolio…")
    paper_summary = update_paper_portfolio(
        watchlist, market_trade_cache=market_trade_cache, fetch_clob_book=fetch_clob_book,
    )
    stats["paper_portfolio"] = paper_summary

    log.info("\n═══════════════════════════════════════════════════════")
    log.info(
        f"  Done. {stats['candidate_alerts']} candidates · {stats['flagged_alerts']} watchlist · "
        f"{stats['insider_watchlist']} insider · {stats['sports_watchlist']} sports/news · "
        f"{stats['momentum_watchlist']} momentum · {stats['contrarian_watchlist']} contrarian."
    )
    if paper_summary:
        log.info(
            f"  Paper: ${paper_summary['current_equity']:.2f} equity · "
            f"PnL ${paper_summary['total_pnl']:+.2f} · "
            f"{paper_summary['open_positions']} open · "
            f"{paper_summary['closed_trades']} closed · "
            f"{'READY' if paper_summary['ready_for_real'] else 'NOT READY'}"
        )
    log.info("═══════════════════════════════════════════════════════")


if __name__ == "__main__":
    run()
