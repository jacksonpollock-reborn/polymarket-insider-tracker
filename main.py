"""
main.py — Orchestrates the full insider-detection pipeline.
Run directly or triggered by GitHub Actions.
"""

import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Allow running from project root
sys.path.insert(0, os.path.dirname(__file__))

from src.fetchers import (
    fetch_active_markets,
    fetch_market_trades,
    fetch_wallet_activity,
    fetch_wallet_positions,
    fetch_polygon_tx_history,
    fetch_arkham_entity,
    fetch_dune_volume_spikes,
    fetch_dune_whale_wallets,
    fetch_dune_new_large_bettors,
)
from src.scorer import score_wallet, MIN_BET_USDC, MAX_NICHE_TVL, VOLUME_SPIKE_FACTOR
from src.reporter import send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

MIN_SUSPICION_SCORE = int(os.environ.get("MIN_SUSPICION_SCORE", "40"))
MAX_WALLETS_TO_SCORE = int(os.environ.get("MAX_WALLETS_TO_SCORE", "60"))


def flag_suspicious_markets(markets: list[dict]) -> list[dict]:
    now     = datetime.now(timezone.utc)
    cutoff  = now + timedelta(days=30)   # only keep markets resolving within 30 days
    flagged = []

    for m in markets:
        try:
            # ── Resolution date filter ─────────────────────────────────────────
            end_raw = m.get("endDateIso") or m.get("endDate")
            if not end_raw:
                continue
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt <= now or end_dt > cutoff:
                continue   # skip already-resolved or >30 days away

            # ── Volume / liquidity flags ───────────────────────────────────────
            vol_24h   = float(m.get("volume24hr") or 0)
            vol_total = float(m.get("volume") or 0)
            liquidity = float(m.get("liquidity") or 0)
            avg_7d    = vol_total / 7 if vol_total else 0
            spike     = (vol_24h / avg_7d) if avg_7d > 0 else 0

            if (
                (liquidity < MAX_NICHE_TVL and vol_24h > MIN_BET_USDC)
                or spike >= VOLUME_SPIKE_FACTOR
                or vol_24h > MIN_BET_USDC * 5
            ):
                m["_spike_ratio"] = round(spike, 2)
                m["_is_niche"]    = liquidity < MAX_NICHE_TVL
                m["_days_to_end"] = round((end_dt - now).total_seconds() / 86400, 1)
                flagged.append(m)
        except Exception:
            continue
    return flagged


def group_trades_by_wallet(trades: list[dict]) -> dict[str, list[dict]]:
    wallet_trades = defaultdict(list)
    for t in trades:
        addr = (
            t.get("proxyWallet") or
            t.get("maker") or
            t.get("transactor") or
            t.get("address") or ""
        ).lower().strip()
        if addr.startswith("0x") and len(addr) == 42:
            wallet_trades[addr].append(t)
    return wallet_trades


def run():
    log.info("═══════════════════════════════════════════════════════")
    log.info("  Polymarket Insider Tracker — starting daily scan")
    log.info("═══════════════════════════════════════════════════════")
    log.info(f"  Threshold : {MIN_SUSPICION_SCORE} pts")
    log.info(f"  Max wallets to score: {MAX_WALLETS_TO_SCORE}")

    stats = {
        "markets_scanned": 0,
        "large_trades": 0,
        "wallets_evaluated": 0,
        "flagged_wallets": 0,
        "data_sources_active": 0,
    }

    # ── Step 1: Fetch markets ──────────────────────────────────────────────────
    log.info("\n[1/7] Fetching active Polymarket markets…")
    markets = fetch_active_markets(limit=150)
    stats["markets_scanned"] = len(markets)

    flagged_markets = flag_suspicious_markets(markets)
    log.info(f"  → {len(flagged_markets)} markets flagged for deep scan")

    if not flagged_markets:
        log.warning("No suspicious markets today. Sending empty report.")
        send_email([], stats)
        return

    stats["data_sources_active"] += 1  # Polymarket is up

    # ── Step 2: Pull Dune supplementary data ──────────────────────────────────
    log.info("\n[2/7] Querying Dune Analytics…")
    dune_whale_list      = fetch_dune_whale_wallets()
    dune_new_wallet_list = fetch_dune_new_large_bettors()

    if dune_whale_list or dune_new_wallet_list:
        stats["data_sources_active"] += 1
        log.info(f"  → {len(dune_whale_list)} whale wallets, {len(dune_new_wallet_list)} new large bettors from Dune")
    else:
        log.warning("  → Dune returned no data (check API key / query IDs)")

    # ── Step 3: Extract large trades from flagged markets ─────────────────────
    log.info("\n[3/7] Extracting large trades from flagged markets…")
    all_trades = []
    for m in flagged_markets:
        cid = m.get("conditionId") or m.get("condition_id")
        if not cid:
            continue
        trades = fetch_market_trades(cid)
        for t in trades:
            # data-api returns size=shares, price=per share → USDC = size * price
            size  = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            usdc  = size * price
            if usdc >= MIN_BET_USDC:
                t["_market_name"]      = m.get("question") or m.get("title") or cid
                t["_market_address"]   = cid
                t["_market_liquidity"] = float(m.get("liquidity") or 0)
                t["_market_end"]       = m.get("endDateIso") or m.get("endDate")
                t["_spike_ratio"]      = m.get("_spike_ratio", 0)
                t["usdcSize"]          = usdc
                all_trades.append(t)

    stats["large_trades"] = len(all_trades)
    log.info(f"  → {len(all_trades)} large trades found")

    # ── Step 4: Group by wallet + merge Dune seed wallets ─────────────────────
    log.info("\n[4/7] Grouping trades by wallet…")
    wallet_trades = group_trades_by_wallet(all_trades)

    # Seed in Dune-flagged wallets even if they don't appear in trade scan
    for w in (dune_whale_list + dune_new_wallet_list):
        if w and w.lower() not in wallet_trades:
            wallet_trades[w.lower()] = []

    # Sort by trade count + size for prioritisation
    sorted_wallets = sorted(
        wallet_trades.items(),
        key=lambda x: (len(x[1]), sum(float(t.get("usdcSize") or t.get("size") or 0) for t in x[1])),
        reverse=True,
    )[:MAX_WALLETS_TO_SCORE]

    stats["wallets_evaluated"] = len(sorted_wallets)
    log.info(f"  → {len(sorted_wallets)} unique wallets to evaluate")

    # ── Step 5: Enrich each wallet ─────────────────────────────────────────────
    log.info("\n[5/7] Enriching wallets (Polymarket history + Polygonscan + Arkham)…")
    watchlist = []
    poly_active = False
    arkham_active = False

    for i, (addr, trades) in enumerate(sorted_wallets, 1):
        log.info(f"  [{i}/{len(sorted_wallets)}] {addr[:10]}… ({len(trades)} trades)")

        activity  = fetch_wallet_activity(addr)
        positions = fetch_wallet_positions(addr)
        if activity or positions:
            poly_active = True

        polygon   = fetch_polygon_tx_history(addr)
        arkham    = fetch_arkham_entity(addr)
        if arkham.get("label") and arkham.get("label") != "Unknown":
            arkham_active = True

        record = score_wallet(
            address              = addr,
            recent_trades        = trades,
            polymarket_activity  = activity,
            positions            = positions,
            polygon_data         = polygon,
            arkham_data          = arkham,
            dune_whale_list      = dune_whale_list,
            dune_new_wallet_list = dune_new_wallet_list,
        )

        if record["suspicion_score"] >= MIN_SUSPICION_SCORE:
            watchlist.append(record)
            log.info(f"    ✅ Score {record['suspicion_score']} → ADDED to watchlist")
        else:
            log.info(f"    ➖ Score {record['suspicion_score']} → below threshold")

    if poly_active:
        stats["data_sources_active"] += 1
    if arkham_active:
        stats["data_sources_active"] += 1

    stats["flagged_wallets"] = len(watchlist)
    log.info(f"\n[6/7] {len(watchlist)} wallets added to watchlist")

    # ── Step 6: Save JSON output ───────────────────────────────────────────────
    output_path = "watchlist.json"
    with open(output_path, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": stats,
            "watchlist": sorted(watchlist, key=lambda x: -x["suspicion_score"]),
        }, f, indent=2)
    log.info(f"  → Saved to {output_path}")

    # ── Step 7: Send email ─────────────────────────────────────────────────────
    log.info("\n[7/7] Sending email report…")
    ok = send_email(watchlist, stats)
    if ok:
        log.info("  → Email delivered successfully")
    else:
        log.error("  → Email failed")
        sys.exit(1)

    log.info("\n═══════════════════════════════════════════════════════")
    log.info(f"  Done. {stats['flagged_wallets']} wallets flagged.")
    log.info("═══════════════════════════════════════════════════════")


if __name__ == "__main__":
    run()
