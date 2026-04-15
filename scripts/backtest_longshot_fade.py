#!/usr/bin/env python3
"""
backtest_longshot_fade.py — Historical base-rate analysis of the longshot fade strategy.

For each recently-closed Polymarket market:
  1. Fetch its trade history (skipping resolution redemptions).
  2. Find the earliest trade where one side was in the longshot band [0.05, 0.15].
  3. Simulate a fade: we would have bought the OPPOSITE side at 1 - longshot_price.
  4. Check the actual resolution outcome and tally the result.

Output: win rate, average P&L, breakeven comparison. Bucketed by entry price.

Usage:
    python3 scripts/backtest_longshot_fade.py [--limit 200] [--min-days-to-end 1]

The limit controls how many closed markets to sample (max ~500 before rate limits
become painful). Pass --verbose for per-market breakdowns.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

# Make src imports work when running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fetchers import _get, GAMMA_API, DATA_API

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# Longshot band: same as scanner defaults
MIN_ASK = float(os.environ.get("LONGSHOT_FADE_MIN_ASK", "0.05"))
MAX_ASK = float(os.environ.get("LONGSHOT_FADE_MAX_ASK", "0.15"))


def fetch_closed_markets(limit: int) -> list[dict]:
    """Fetch the most recently closed markets from Gamma API."""
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
    """Fetch trades WITHOUT the redemption filter so we can see early price discovery."""
    result = _get(f"{DATA_API}/trades", params={"market": condition_id, "limit": limit})
    if isinstance(result, dict):
        result = result.get("data") or result.get("results") or []
    if not isinstance(result, list):
        return []
    return result


def extract_resolution(market: dict) -> str | None:
    """Return 'YES', 'NO', or None based on outcomePrices field.

    Resolution convention: outcomePrices is a 2-element list aligned to outcomes.
    [1, 0] means YES won. [0, 1] means NO won. Anything else is ambiguous.
    """
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if len(prices) != 2:
            return None
        p = [float(x) for x in prices]
    except (ValueError, TypeError):
        return None

    outcomes_raw = market.get("outcomes")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    except Exception:
        outcomes = ["Yes", "No"]
    if len(outcomes) != 2:
        return None

    # Pair them and find the winner
    for outcome, price in zip(outcomes, p):
        if price >= 0.99 and isinstance(outcome, str):
            return outcome.upper()
    return None


def find_earliest_longshot_trade(
    trades: list[dict],
    min_ask: float,
    max_ask: float,
    min_days_to_end: float,
    market_end_ts: int | None,
) -> tuple[dict | None, str | None]:
    """
    Find the earliest trade where one side traded in the longshot band.

    Returns (trade, longshot_side) or (None, None).
    Filters:
      - price in [min_ask, max_ask]
      - at least `min_days_to_end` days before market close
    """
    # Exclude redemption trades (>= 0.99)
    clean = [t for t in trades if 0 < float(t.get("price") or 0) < 0.99]

    # Sort oldest first so we simulate entering as soon as the opportunity appears
    clean.sort(key=lambda t: int(t.get("timestamp") or 0))

    for t in clean:
        try:
            price = float(t.get("price") or 0)
            ts = int(t.get("timestamp") or 0)
        except (ValueError, TypeError):
            continue
        if not (min_ask <= price < max_ask):
            continue
        if market_end_ts and min_days_to_end > 0:
            days_to_end = (market_end_ts - ts) / 86400.0
            if days_to_end < min_days_to_end:
                continue

        outcome = (t.get("outcome") or "").upper()
        if outcome not in ("YES", "NO"):
            continue
        return t, outcome

    return None, None


def simulate_fade(longshot_outcome: str, longshot_price: float, winner: str) -> tuple[float, bool]:
    """
    Simulate a fade trade: we bought the OPPOSITE of the longshot side.
    Entry cost: 1 - longshot_price (shares summed to 1 in an ideal market).
    Payout: 1.00 if fade side wins, 0.00 otherwise.

    Returns (pnl_per_share, won).
    """
    fade_side = "NO" if longshot_outcome == "YES" else "YES"
    entry = 1.0 - longshot_price
    won = (fade_side == winner)
    payout = 1.0 if won else 0.0
    pnl = payout - entry
    return pnl, won


def run_backtest(
    limit: int,
    min_days_to_end: float,
    min_ask: float,
    max_ask: float,
    verbose: bool = False,
    exclude_fdv: bool = False,
) -> dict:
    """Main backtest loop. Returns a summary dict."""
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
        "markets_with_trade_data": 0,
        "markets_with_resolution": 0,
        "fade_opportunities_found": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_per_share": 0.0,
        "no_longshot_in_history": 0,
        "no_non_redemption_trades": 0,
        "no_resolution": 0,
        "by_entry_price": defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0}),
        "sample_wins": [],
        "sample_losses": [],
    }

    for m in markets:
        stats["markets_checked"] += 1
        cid = m.get("conditionId")
        if not cid:
            continue

        # Resolution
        winner = extract_resolution(m)
        if not winner:
            stats["no_resolution"] += 1
            continue
        stats["markets_with_resolution"] += 1

        # Trade history
        trades = fetch_raw_trades(cid, limit=500)
        if not trades:
            stats["no_non_redemption_trades"] += 1
            continue
        stats["markets_with_trade_data"] += 1

        # Parse market end timestamp
        end_raw = m.get("endDateIso") or m.get("endDate")
        market_end_ts = None
        if end_raw:
            try:
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                market_end_ts = int(end_dt.timestamp())
            except Exception:
                pass

        # Find fade opportunity
        trade, longshot_side = find_earliest_longshot_trade(
            trades, min_ask=min_ask, max_ask=max_ask,
            min_days_to_end=min_days_to_end, market_end_ts=market_end_ts,
        )
        if not trade:
            stats["no_longshot_in_history"] += 1
            continue

        longshot_price = float(trade["price"])
        pnl, won = simulate_fade(longshot_side, longshot_price, winner)

        stats["fade_opportunities_found"] += 1
        if won:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        stats["total_pnl_per_share"] += pnl

        # Bucket by entry price
        fade_entry = round(1.0 - longshot_price, 2)
        entry_bucket = f"{fade_entry:.2f}"
        stats["by_entry_price"][entry_bucket]["count"] += 1
        if won:
            stats["by_entry_price"][entry_bucket]["wins"] += 1
        stats["by_entry_price"][entry_bucket]["pnl"] += pnl

        # Keep examples
        q = (m.get("question") or "?")[:55]
        example = f"{longshot_side}={longshot_price:.3f} → fade {('NO' if longshot_side=='YES' else 'YES')}@{1-longshot_price:.3f} won={won} | {q}"
        if won and len(stats["sample_wins"]) < 5:
            stats["sample_wins"].append(example)
        if not won and len(stats["sample_losses"]) < 5:
            stats["sample_losses"].append(example)

        if verbose:
            log.info(f"  {'WIN' if won else 'LOSS'} pnl={pnl:+.3f} | {example}")

    return stats


def print_summary(stats: dict, min_ask: float, max_ask: float) -> None:
    print("\n" + "=" * 70)
    print(f"  LONGSHOT FADE BACKTEST — longshot band [{min_ask:.2f}, {max_ask:.2f})")
    print("=" * 70)
    print(f"Markets checked:             {stats['markets_checked']}")
    print(f"  → with resolution data:    {stats['markets_with_resolution']}")
    print(f"  → with trade data:         {stats['markets_with_trade_data']}")
    print(f"  → no longshot in history:  {stats['no_longshot_in_history']}")
    print(f"  → no resolution extract:   {stats['no_resolution']}")
    print()
    total = stats["fade_opportunities_found"]
    print(f"Fade opportunities found:    {total}")
    if total == 0:
        print("No opportunities — cannot compute base rate.")
        return

    wins = stats["wins"]
    losses = stats["losses"]
    win_rate = wins / total * 100
    avg_pnl = stats["total_pnl_per_share"] / total
    print(f"  Wins:  {wins} ({win_rate:.1f}%)")
    print(f"  Losses: {losses}")
    print(f"  Average P&L per share:     {avg_pnl:+.4f} ({avg_pnl*100:+.2f}%)")
    print()

    # Breakeven analysis
    print("BREAKEVEN ANALYSIS:")
    # Compute weighted breakeven based on actual entry prices
    expected_winrate_for_breakeven = 0
    if total > 0:
        # For each bucket, breakeven win rate = entry / (entry + (1-entry)) = entry
        # We need actual win rate >= entry price
        weighted_entry = 0.0
        for bucket, data in stats["by_entry_price"].items():
            weighted_entry += float(bucket) * data["count"]
        expected_winrate_for_breakeven = weighted_entry / total * 100
    print(f"  Breakeven win rate needed: ~{expected_winrate_for_breakeven:.1f}% (weighted by entry prices)")
    print(f"  Actual win rate:            {win_rate:.1f}%")
    edge = win_rate - expected_winrate_for_breakeven
    verdict = "VIABLE" if edge > 2 else ("MARGINAL" if edge > 0 else "NOT VIABLE")
    print(f"  Edge:                       {edge:+.1f} pp → {verdict}")
    print()

    # By bucket
    print("BY ENTRY PRICE BUCKET:")
    print(f"  {'entry':<8} {'count':>6} {'wins':>6} {'win%':>7} {'avg pnl':>10}")
    for bucket in sorted(stats["by_entry_price"].keys()):
        data = stats["by_entry_price"][bucket]
        if data["count"] == 0:
            continue
        wr = data["wins"] / data["count"] * 100
        avg = data["pnl"] / data["count"]
        print(f"  {bucket:<8} {data['count']:>6} {data['wins']:>6} {wr:>6.1f}% {avg:>+9.4f}")

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
    parser.add_argument("--limit", type=int, default=200,
                        help="Max closed markets to sample")
    parser.add_argument("--min-days-to-end", type=float, default=1.0,
                        help="Min days between fade entry and market close")
    parser.add_argument("--min-ask", type=float, default=MIN_ASK,
                        help="Longshot band floor")
    parser.add_argument("--max-ask", type=float, default=MAX_ASK,
                        help="Longshot band ceiling")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-market results")
    parser.add_argument("--exclude-fdv", action="store_true",
                        help="Skip markets with 'FDV' or 'launch' in the question")
    args = parser.parse_args()

    stats = run_backtest(
        limit=args.limit,
        min_days_to_end=args.min_days_to_end,
        min_ask=args.min_ask,
        max_ask=args.max_ask,
        verbose=args.verbose,
        exclude_fdv=args.exclude_fdv,
    )
    print_summary(stats, args.min_ask, args.max_ask)


if __name__ == "__main__":
    main()
