"""
paper_trader.py — Automatic paper trading portfolio for strategy validation.

Simulates a $100 portfolio that auto-enters every watchlist alert at the live
market price, tracks open positions across runs, and auto-exits on resolution,
take-profit, or stop-loss. Zero manual effort required.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from src.scorer import _parse_dt

log = logging.getLogger(__name__)

PORTFOLIO_PATH = os.environ.get("PAPER_PORTFOLIO_PATH", "paper_portfolio.json")
STARTING_CAPITAL = float(os.environ.get("PAPER_STARTING_CAPITAL", "100"))
DEFAULT_TAKE_PROFIT = float(os.environ.get("PAPER_TAKE_PROFIT", "0.90"))

BUCKETS = ["insider", "sports_news", "momentum", "contrarian"]


def _empty_bucket_perf() -> dict:
    return {"trades": 0, "wins": 0, "losses": 0, "expired": 0, "pnl": 0.0}


def _empty_portfolio() -> dict:
    return {
        "starting_capital": STARTING_CAPITAL,
        "current_capital": STARTING_CAPITAL,
        "total_trades": 0,
        "open_positions": [],
        "closed_positions": [],
        "daily_snapshots": [],
        "bucket_performance": {b: _empty_bucket_perf() for b in BUCKETS},
    }


def load_portfolio(path: str = PORTFOLIO_PATH) -> dict:
    if not os.path.exists(path):
        return _empty_portfolio()
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "current_capital" not in data:
            return _empty_portfolio()
        for b in BUCKETS:
            data.setdefault("bucket_performance", {}).setdefault(b, _empty_bucket_perf())
        return data
    except Exception:
        return _empty_portfolio()


def save_portfolio(portfolio: dict, path: str = PORTFOLIO_PATH) -> None:
    with open(path, "w") as f:
        json.dump(portfolio, f, indent=2)


PAPER_MIN_REMAINING_EDGE = float(os.environ.get("PAPER_MIN_REMAINING_EDGE", "0.05"))


def _position_size(alert: dict, capital: float) -> float:
    """Size the paper position based on score and remaining edge.

    Full-signal entries (remaining >= 0.15) get normal sizing (2-10%).
    Exploratory entries (0.05 <= remaining < 0.15) get 1% sizing to
    gather data on whether high-entry trades are profitable.
    """
    score = alert.get("best_score", 0)
    remaining = alert.get("shared_features", {}).get("remaining_edge_pct", 0)

    if remaining < PAPER_MIN_REMAINING_EDGE:
        return 0.0
    # Exploratory tier: high entry price, small size just for data
    if remaining < 0.15:
        return round(min(capital * 0.01, capital), 2)
    if score >= 70 and remaining >= 0.30:
        return round(min(capital * 0.10, capital), 2)
    if score >= 50 and remaining >= 0.20:
        return round(min(capital * 0.05, capital), 2)
    return round(min(capital * 0.02, capital), 2)


def _get_live_price(alert: dict, fetch_clob_book) -> float | None:
    """Fetch current best ask for the suggested outcome from CLOB."""
    if fetch_clob_book is None:
        return None

    tokens = alert.get("tokens") or []
    suggested = (alert.get("suggested_outcome") or "").upper()

    token_id = None
    for t in tokens:
        if str(t.get("outcome", "")).upper() == suggested:
            token_id = t.get("token_id") or t.get("tokenId")
            break

    if not token_id:
        market_id = alert.get("market_id", "")
        active = alert.get("active_exposure", {})
        entry = active.get("entry_price")
        if entry and 0 < entry < 1:
            return float(entry)
        return None

    try:
        book = fetch_clob_book(token_id)
        asks = book.get("asks", [])
        if asks:
            return float(asks[0]["price"])
    except Exception as e:
        log.warning(f"[Paper] Failed to fetch live price: {e}")
    return None


def open_positions(portfolio: dict, watchlist: list[dict], fetch_clob_book=None) -> int:
    """Open paper positions for new watchlist alerts. Returns count of new positions."""
    existing_ids = {p["alert_id"] for p in portfolio["open_positions"]}
    existing_ids |= {p["alert_id"] for p in portfolio["closed_positions"]}

    opened = 0
    for alert in watchlist:
        alert_id = alert.get("alert_id")
        if not alert_id or alert_id in existing_ids:
            continue

        capital = portfolio["current_capital"]
        size = _position_size(alert, capital)
        if size <= 0:
            continue

        whale_price = alert.get("active_exposure", {}).get("entry_price")
        live_price = _get_live_price(alert, fetch_clob_book)
        entry_price = live_price or whale_price

        if not entry_price or entry_price <= 0 or entry_price >= 1:
            continue

        shares = round(size / entry_price, 4)
        remaining = alert.get("shared_features", {}).get("remaining_edge_pct", 0)
        signal_tier = "full" if remaining >= 0.15 else "exploratory"

        position = {
            "alert_id": alert_id,
            "bucket": alert.get("best_bucket", "unknown"),
            "signal_tier": signal_tier,
            "market_name": alert.get("market_name", ""),
            "market_id": alert.get("market_id", ""),
            "suggested_outcome": alert.get("suggested_outcome"),
            "whale_entry_price": whale_price,
            "paper_entry_price": entry_price,
            "position_size_usdc": size,
            "shares": shares,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "market_end": alert.get("market_end"),
            "take_profit": DEFAULT_TAKE_PROFIT,
            "stop_loss": None,
            "status": "open",
            "exit_price": None,
            "pnl_usdc": None,
            "pnl_pct": None,
            "closed_at": None,
        }

        portfolio["open_positions"].append(position)
        portfolio["current_capital"] -= size
        portfolio["total_trades"] += 1
        portfolio["bucket_performance"].setdefault(
            position["bucket"], _empty_bucket_perf()
        )["trades"] += 1
        existing_ids.add(alert_id)
        opened += 1
        log.info(
            f"[Paper] OPENED {position['bucket']} | {position['market_name'][:50]} | "
            f"${size:.2f} @ {entry_price:.4f} ({shares:.2f} shares)"
        )

    return opened


def close_positions(portfolio: dict, market_trade_cache: dict | None = None) -> int:
    """Check open positions for exit conditions. Returns count of closed positions."""
    now = datetime.now(timezone.utc)
    still_open = []
    closed_count = 0

    for pos in portfolio["open_positions"]:
        exit_price = None
        status = None

        market_end = _parse_dt(pos.get("market_end"))
        entry_price = pos["paper_entry_price"]

        # Check trades for current price
        current_price = None
        if market_trade_cache:
            trades = market_trade_cache.get(pos["market_id"], [])
            outcome = (pos.get("suggested_outcome") or "").upper()
            for t in reversed(trades):
                t_outcome = str(t.get("outcome") or t.get("asset_id") or "").upper()
                if outcome and t_outcome and outcome in t_outcome:
                    try:
                        current_price = float(t.get("price") or 0)
                        break
                    except Exception:
                        pass

        # Resolution check
        if current_price is not None:
            if current_price >= 0.95:
                exit_price = current_price
                status = "won"
            elif current_price <= 0.05:
                exit_price = current_price
                status = "lost"
            elif current_price >= pos.get("take_profit", 0.90):
                exit_price = current_price
                status = "won"
            elif pos.get("stop_loss") and current_price <= pos["stop_loss"]:
                exit_price = current_price
                status = "stopped_out"

        # Expiration check
        if status is None and market_end and now >= market_end + timedelta(hours=12):
            exit_price = current_price or entry_price
            status = "expired"

        if status:
            pos["exit_price"] = exit_price
            pos["status"] = status
            pos["closed_at"] = now.isoformat()

            pnl_per_share = (exit_price or 0) - entry_price
            pos["pnl_usdc"] = round(pnl_per_share * pos["shares"], 2)
            pos["pnl_pct"] = round(pnl_per_share / entry_price * 100, 2) if entry_price > 0 else 0

            portfolio["current_capital"] += pos["position_size_usdc"] + pos["pnl_usdc"]
            portfolio["closed_positions"].append(pos)

            bucket = pos.get("bucket", "unknown")
            bp = portfolio["bucket_performance"].setdefault(bucket, _empty_bucket_perf())
            if status == "won":
                bp["wins"] += 1
            elif status in ("lost", "stopped_out"):
                bp["losses"] += 1
            else:
                bp["expired"] += 1
            bp["pnl"] = round(bp["pnl"] + pos["pnl_usdc"], 2)

            closed_count += 1
            log.info(
                f"[Paper] CLOSED {status.upper()} | {pos['market_name'][:50]} | "
                f"PnL ${pos['pnl_usdc']:+.2f} ({pos['pnl_pct']:+.1f}%)"
            )
        else:
            still_open.append(pos)

    portfolio["open_positions"] = still_open
    return closed_count


def _take_snapshot(portfolio: dict) -> None:
    """Record a daily equity snapshot."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    open_value = sum(p["position_size_usdc"] for p in portfolio["open_positions"])
    total_equity = portfolio["current_capital"] + open_value
    total_pnl = total_equity - portfolio["starting_capital"]

    snapshots = portfolio.setdefault("daily_snapshots", [])
    if snapshots and snapshots[-1].get("date") == today:
        snapshots[-1] = {
            "date": today,
            "equity": round(total_equity, 2),
            "cash": round(portfolio["current_capital"], 2),
            "open_positions": len(portfolio["open_positions"]),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / portfolio["starting_capital"] * 100, 2),
        }
    else:
        snapshots.append({
            "date": today,
            "equity": round(total_equity, 2),
            "cash": round(portfolio["current_capital"], 2),
            "open_positions": len(portfolio["open_positions"]),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / portfolio["starting_capital"] * 100, 2),
        })


def portfolio_summary(portfolio: dict) -> dict:
    """Generate a summary for reporting."""
    open_value = sum(p["position_size_usdc"] for p in portfolio["open_positions"])
    total_equity = portfolio["current_capital"] + open_value
    total_pnl = total_equity - portfolio["starting_capital"]

    total_closed = len(portfolio["closed_positions"])
    total_wins = sum(1 for p in portfolio["closed_positions"] if p["status"] == "won")
    total_losses = sum(1 for p in portfolio["closed_positions"] if p["status"] in ("lost", "stopped_out"))
    win_rate = round(total_wins / total_closed * 100, 1) if total_closed > 0 else 0

    ready = total_closed >= 30 and total_pnl > 0

    # Tier breakdown
    all_positions = list(portfolio["open_positions"]) + list(portfolio["closed_positions"])
    full_trades = [p for p in portfolio["closed_positions"] if p.get("signal_tier", "full") == "full"]
    expl_trades = [p for p in portfolio["closed_positions"] if p.get("signal_tier") == "exploratory"]
    full_pnl = sum(p.get("pnl_usdc", 0) or 0 for p in full_trades)
    expl_pnl = sum(p.get("pnl_usdc", 0) or 0 for p in expl_trades)
    full_wins = sum(1 for p in full_trades if p["status"] == "won")
    expl_wins = sum(1 for p in expl_trades if p["status"] == "won")

    return {
        "starting_capital": portfolio["starting_capital"],
        "current_equity": round(total_equity, 2),
        "cash": round(portfolio["current_capital"], 2),
        "open_positions": len(portfolio["open_positions"]),
        "total_trades": portfolio["total_trades"],
        "closed_trades": total_closed,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate_pct": win_rate,
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / portfolio["starting_capital"] * 100, 2),
        "bucket_performance": portfolio.get("bucket_performance", {}),
        "tier_breakdown": {
            "full": {"closed": len(full_trades), "wins": full_wins, "pnl": round(full_pnl, 2)},
            "exploratory": {"closed": len(expl_trades), "wins": expl_wins, "pnl": round(expl_pnl, 2)},
        },
        "ready_for_real": ready,
        "ready_reason": (
            "30+ resolved trades with positive P&L" if ready
            else f"Need {max(0, 30 - total_closed)} more resolved trades"
            + (" and positive P&L" if total_pnl <= 0 and total_closed > 0 else "")
        ),
    }


def update_paper_portfolio(
    watchlist: list[dict],
    market_trade_cache: dict | None = None,
    fetch_clob_book=None,
    path: str = PORTFOLIO_PATH,
) -> dict:
    """Main entry point — called at end of each tracker run."""
    portfolio = load_portfolio(path)

    closed = close_positions(portfolio, market_trade_cache)
    if closed:
        log.info(f"[Paper] Closed {closed} positions")

    opened = open_positions(portfolio, watchlist, fetch_clob_book)
    if opened:
        log.info(f"[Paper] Opened {opened} new positions")

    _take_snapshot(portfolio)
    save_portfolio(portfolio, path)

    summary = portfolio_summary(portfolio)
    log.info(
        f"[Paper] Portfolio: ${summary['current_equity']:.2f} equity | "
        f"{summary['open_positions']} open | "
        f"{summary['closed_trades']} closed | "
        f"PnL ${summary['total_pnl']:+.2f} ({summary['total_pnl_pct']:+.1f}%) | "
        f"{'READY for real trading' if summary['ready_for_real'] else summary['ready_reason']}"
    )

    return summary
