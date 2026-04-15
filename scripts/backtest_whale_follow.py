#!/usr/bin/env python3
"""
backtest_whale_follow.py — Base-rate analysis of "follow the biggest trades" strategy.

For each recently-closed Polymarket market:
  1. Fetch its trade history.
  2. Identify the N biggest buy trades (by USDC value) within a time window
     after the market opened.
  3. Check which side each big trade was on (YES or NO).
  4. Compare to the market's actual resolution.
  5. Compute win rate for "copy the biggest early whales."

This is a PROXY for the tracker's insider/momentum/sports_news buckets,
which all boil down to "the tracker flags big trades, and we follow them."
If whales are systematically right, the proxy wins > 55%. If whales are
random, proxy wins ~50%. If whales are reverse-indicators, < 45%.

Usage:
    python3 scripts/backtest_whale_follow.py [--limit 200] [--min-usdc 1000]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fetchers import _get, GAMMA_API, DATA_API

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def fetch_closed_markets(limit: int) -> list[dict]:
    markets = []
    offset = 0
    while len(markets) < limit:
        batch = _get(f"{GAMMA_API}/markets", params={
            "closed": "true", "active": "false",
            "limit": 50, "offset": offset,
            "order": "endDate", "ascending": "false",
        })
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
    return markets[:limit]


def fetch_raw_trades(condition_id: str, limit: int = 500) -> list[dict]:
    result = _get(f"{DATA_API}/trades", params={"market": condition_id, "limit": limit})
    if isinstance(result, dict):
        result = result.get("data") or result.get("results") or []
    if not isinstance(result, list):
        return []
    return result


def extract_resolution(market: dict) -> str | None:
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        p = [float(x) for x in prices]
    except Exception:
        return None
    if len(p) != 2:
        return None

    outcomes_raw = market.get("outcomes")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    except Exception:
        outcomes = ["Yes", "No"]
    if len(outcomes) != 2:
        return None

    for outcome, price in zip(outcomes, p):
        if price >= 0.99 and isinstance(outcome, str):
            return outcome.upper()
    return None


def find_biggest_early_buys(
    trades: list[dict],
    min_usdc: float,
    top_n: int,
    max_price: float = 0.90,
) -> list[dict]:
    """
    Return the N biggest BUY trades by USDC value from the earliest portion
    of the trade history.

    Filters:
      - Only BUY trades (SELL would be profit-taking)
      - Price < max_price (exclude near-resolution trades)
      - USDC value >= min_usdc
    """
    candidates = []
    for t in trades:
        side = (t.get("side") or "").upper()
        if side != "BUY":
            continue
        try:
            price = float(t.get("price") or 0)
            size = float(t.get("size") or 0)  # share count
        except (ValueError, TypeError):
            continue
        if price <= 0 or price >= max_price:
            continue
        usdc = size * price
        if usdc < min_usdc:
            continue
        outcome = (t.get("outcome") or "").upper()
        if outcome not in ("YES", "NO"):
            continue
        ts = int(t.get("timestamp") or 0)
        candidates.append({
            "timestamp": ts,
            "outcome": outcome,
            "price": price,
            "size": size,
            "usdc": usdc,
        })

    # Sort oldest first, take the earliest large ones (price discovery phase)
    candidates.sort(key=lambda c: c["timestamp"])
    return candidates[:top_n]


def simulate_follow(whale_outcome: str, whale_price: float, winner: str) -> tuple[float, bool]:
    """
    Simulate following a whale into the trade:
    - Buy the same side at the whale's entry price (optimistic — no slippage)
    - Resolve at 1.00 if winning side, 0.00 otherwise
    """
    won = (whale_outcome == winner)
    payout = 1.0 if won else 0.0
    pnl = payout - whale_price
    return pnl, won


def run_backtest(limit: int, min_usdc: float, top_n: int, exclude_fdv: bool, verbose: bool) -> dict:
    log.info(f"Fetching {limit} most recently closed markets…")
    markets = fetch_closed_markets(limit)
    log.info(f"  → got {len(markets)} markets")
    if exclude_fdv:
        pre = len(markets)
        markets = [m for m in markets if "fdv" not in (m.get("question") or "").lower()
                                          and "launch" not in (m.get("question") or "").lower()]
        log.info(f"  → after FDV filter: {len(markets)} markets ({pre - len(markets)} excluded)")

    stats = {
        "markets_checked": 0,
        "markets_with_whales": 0,
        "whales_found": 0,
        "whale_wins": 0,
        "whale_losses": 0,
        "total_pnl": 0.0,
        "by_entry_price": defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0}),
        "by_usdc_bucket": defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0}),
        "sample_wins": [],
        "sample_losses": [],
    }

    for m in markets:
        stats["markets_checked"] += 1
        cid = m.get("conditionId")
        if not cid:
            continue
        winner = extract_resolution(m)
        if not winner:
            continue
        trades = fetch_raw_trades(cid, limit=500)
        if not trades:
            continue

        whales = find_biggest_early_buys(trades, min_usdc=min_usdc, top_n=top_n)
        if not whales:
            continue
        stats["markets_with_whales"] += 1

        for w in whales:
            pnl, won = simulate_follow(w["outcome"], w["price"], winner)
            stats["whales_found"] += 1
            stats["total_pnl"] += pnl
            if won:
                stats["whale_wins"] += 1
            else:
                stats["whale_losses"] += 1

            bucket = f"{round(w['price'], 2):.2f}"
            stats["by_entry_price"][bucket]["count"] += 1
            stats["by_entry_price"][bucket]["pnl"] += pnl
            if won:
                stats["by_entry_price"][bucket]["wins"] += 1

            if w["usdc"] >= 10000:
                usdc_bucket = "10K+"
            elif w["usdc"] >= 5000:
                usdc_bucket = "5K-10K"
            elif w["usdc"] >= 2000:
                usdc_bucket = "2K-5K"
            else:
                usdc_bucket = "1K-2K"
            stats["by_usdc_bucket"][usdc_bucket]["count"] += 1
            stats["by_usdc_bucket"][usdc_bucket]["pnl"] += pnl
            if won:
                stats["by_usdc_bucket"][usdc_bucket]["wins"] += 1

            q = (m.get("question") or "?")[:55]
            ex = f"{w['outcome']}@{w['price']:.3f} ${w['usdc']:,.0f} winner={winner} | {q}"
            if won and len(stats["sample_wins"]) < 5:
                stats["sample_wins"].append(ex)
            if not won and len(stats["sample_losses"]) < 5:
                stats["sample_losses"].append(ex)

    return stats


def print_summary(stats: dict) -> None:
    print("\n" + "=" * 70)
    print("  WHALE FOLLOW BACKTEST — Copy the biggest early buy trades")
    print("=" * 70)
    print(f"Markets checked:       {stats['markets_checked']}")
    print(f"Markets with whales:   {stats['markets_with_whales']}")
    print(f"Whale trades found:    {stats['whales_found']}")
    if stats['whales_found'] == 0:
        print("No whales — nothing to tally.")
        return

    total = stats['whales_found']
    wr = stats['whale_wins'] / total * 100
    avg_pnl = stats['total_pnl'] / total
    print()
    print(f"Wins:         {stats['whale_wins']} ({wr:.1f}%)")
    print(f"Losses:       {stats['whale_losses']}")
    print(f"Average P&L:  {avg_pnl:+.4f} ({avg_pnl*100:+.2f}%) per $1 of whale exposure")
    print()

    # Breakeven is 50% for symmetric outcomes
    edge = wr - 50.0
    verdict = "VIABLE" if edge > 5 else ("MARGINAL" if edge > 0 else "NOT VIABLE")
    print(f"Edge vs 50% coin flip: {edge:+.1f} pp → {verdict}")
    print()

    print("BY ENTRY PRICE:")
    print(f"  {'entry':<8} {'count':>6} {'wins':>6} {'win%':>7} {'avg pnl':>10}")
    for bucket in sorted(stats["by_entry_price"].keys()):
        d = stats["by_entry_price"][bucket]
        if d["count"] == 0:
            continue
        wr_b = d["wins"] / d["count"] * 100
        avg = d["pnl"] / d["count"]
        print(f"  {bucket:<8} {d['count']:>6} {d['wins']:>6} {wr_b:>6.1f}% {avg:>+9.4f}")

    print()
    print("BY WHALE SIZE (USDC):")
    print(f"  {'usdc':<10} {'count':>6} {'wins':>6} {'win%':>7} {'avg pnl':>10}")
    for bucket in ["10K+", "5K-10K", "2K-5K", "1K-2K"]:
        d = stats["by_usdc_bucket"].get(bucket)
        if not d or d["count"] == 0:
            continue
        wr_b = d["wins"] / d["count"] * 100
        avg = d["pnl"] / d["count"]
        print(f"  {bucket:<10} {d['count']:>6} {d['wins']:>6} {wr_b:>6.1f}% {avg:>+9.4f}")

    print()
    if stats["sample_wins"]:
        print("SAMPLE WINS:")
        for s in stats["sample_wins"]:
            print(f"  {s}")
    if stats["sample_losses"]:
        print("\nSAMPLE LOSSES:")
        for s in stats["sample_losses"]:
            print(f"  {s}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--min-usdc", type=float, default=1000.0,
                        help="Minimum USDC value for a trade to count as a whale")
    parser.add_argument("--top-n", type=int, default=3,
                        help="How many earliest big trades to sample per market")
    parser.add_argument("--exclude-fdv", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    stats = run_backtest(
        limit=args.limit, min_usdc=args.min_usdc, top_n=args.top_n,
        exclude_fdv=args.exclude_fdv, verbose=args.verbose,
    )
    print_summary(stats)


if __name__ == "__main__":
    main()
