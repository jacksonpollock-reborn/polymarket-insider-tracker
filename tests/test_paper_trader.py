"""Tests for the automatic paper trading module."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from src.paper_trader import (
    _empty_portfolio,
    _position_size,
    close_positions,
    load_portfolio,
    open_positions,
    portfolio_summary,
    save_portfolio,
    update_paper_portfolio,
)


def _make_alert(
    alert_id="test-001",
    bucket="insider",
    score=60,
    entry_price=0.65,
    remaining_edge=0.35,
    market_name="Test Market",
    market_id="mkt-001",
    suggested_outcome="YES",
    market_end="2026-05-01",
):
    return {
        "alert_id": alert_id,
        "best_bucket": bucket,
        "best_score": score,
        "market_name": market_name,
        "market_id": market_id,
        "suggested_outcome": suggested_outcome,
        "market_end": market_end,
        "active_exposure": {
            "entry_price": entry_price,
            "dominant_outcome": suggested_outcome,
            "dominant_usdc": 10000,
        },
        "shared_features": {
            "remaining_edge_pct": remaining_edge,
        },
    }


class TestPositionSizing(unittest.TestCase):
    def test_high_conviction(self):
        alert = _make_alert(score=75, remaining_edge=0.35)
        size = _position_size(alert, 100.0)
        self.assertEqual(size, 10.0)

    def test_medium_conviction(self):
        alert = _make_alert(score=55, remaining_edge=0.25)
        size = _position_size(alert, 100.0)
        self.assertEqual(size, 5.0)

    def test_low_conviction(self):
        alert = _make_alert(score=42, remaining_edge=0.18)
        size = _position_size(alert, 100.0)
        self.assertEqual(size, 2.0)

    def test_skip_low_edge(self):
        alert = _make_alert(score=80, remaining_edge=0.10)
        size = _position_size(alert, 100.0)
        self.assertEqual(size, 0.0)

    def test_capped_by_capital(self):
        alert = _make_alert(score=75, remaining_edge=0.35)
        size = _position_size(alert, 5.0)
        self.assertEqual(size, 0.5)


class TestOpenPositions(unittest.TestCase):
    def test_opens_new_position(self):
        portfolio = _empty_portfolio()
        alerts = [_make_alert()]
        opened = open_positions(portfolio, alerts)
        self.assertEqual(opened, 1)
        self.assertEqual(len(portfolio["open_positions"]), 1)
        pos = portfolio["open_positions"][0]
        self.assertEqual(pos["alert_id"], "test-001")
        self.assertEqual(pos["paper_entry_price"], 0.65)
        self.assertEqual(pos["status"], "open")
        self.assertLess(portfolio["current_capital"], 100.0)

    def test_skips_duplicate(self):
        portfolio = _empty_portfolio()
        alerts = [_make_alert()]
        open_positions(portfolio, alerts)
        opened = open_positions(portfolio, alerts)
        self.assertEqual(opened, 0)
        self.assertEqual(len(portfolio["open_positions"]), 1)

    def test_skips_closed_duplicate(self):
        portfolio = _empty_portfolio()
        portfolio["closed_positions"].append({"alert_id": "test-001"})
        alerts = [_make_alert()]
        opened = open_positions(portfolio, alerts)
        self.assertEqual(opened, 0)

    def test_skips_low_edge(self):
        portfolio = _empty_portfolio()
        alerts = [_make_alert(remaining_edge=0.05)]
        opened = open_positions(portfolio, alerts)
        self.assertEqual(opened, 0)


class TestClosePositions(unittest.TestCase):
    def test_close_on_resolution_win(self):
        portfolio = _empty_portfolio()
        portfolio["current_capital"] = 90.0
        portfolio["open_positions"] = [{
            "alert_id": "test-001",
            "bucket": "insider",
            "market_name": "Test",
            "market_id": "mkt-001",
            "suggested_outcome": "YES",
            "whale_entry_price": 0.60,
            "paper_entry_price": 0.65,
            "position_size_usdc": 10.0,
            "shares": 15.38,
            "opened_at": "2026-04-01T00:00:00+00:00",
            "market_end": "2026-04-02",
            "take_profit": 0.90,
            "stop_loss": None,
            "status": "open",
            "exit_price": None,
            "pnl_usdc": None,
            "pnl_pct": None,
            "closed_at": None,
        }]

        trades = [{"price": "0.98", "outcome": "YES", "timestamp": "2026-04-02T12:00:00Z"}]
        cache = {"mkt-001": trades}

        closed = close_positions(portfolio, cache)
        self.assertEqual(closed, 1)
        self.assertEqual(len(portfolio["open_positions"]), 0)
        self.assertEqual(len(portfolio["closed_positions"]), 1)
        pos = portfolio["closed_positions"][0]
        self.assertEqual(pos["status"], "won")
        self.assertGreater(pos["pnl_usdc"], 0)
        self.assertGreater(portfolio["current_capital"], 90.0)

    def test_close_on_resolution_loss(self):
        portfolio = _empty_portfolio()
        portfolio["current_capital"] = 90.0
        portfolio["open_positions"] = [{
            "alert_id": "test-002",
            "bucket": "insider",
            "market_name": "Test Loss",
            "market_id": "mkt-002",
            "suggested_outcome": "YES",
            "whale_entry_price": 0.60,
            "paper_entry_price": 0.65,
            "position_size_usdc": 10.0,
            "shares": 15.38,
            "opened_at": "2026-04-01T00:00:00+00:00",
            "market_end": "2026-04-02",
            "take_profit": 0.90,
            "stop_loss": None,
            "status": "open",
            "exit_price": None,
            "pnl_usdc": None,
            "pnl_pct": None,
            "closed_at": None,
        }]

        trades = [{"price": "0.03", "outcome": "YES", "timestamp": "2026-04-02T12:00:00Z"}]
        cache = {"mkt-002": trades}

        closed = close_positions(portfolio, cache)
        self.assertEqual(closed, 1)
        pos = portfolio["closed_positions"][0]
        self.assertEqual(pos["status"], "lost")
        self.assertLess(pos["pnl_usdc"], 0)


class TestPortfolioSummary(unittest.TestCase):
    def test_empty_portfolio(self):
        portfolio = _empty_portfolio()
        summary = portfolio_summary(portfolio)
        self.assertEqual(summary["starting_capital"], 100.0)
        self.assertEqual(summary["total_pnl"], 0)
        self.assertFalse(summary["ready_for_real"])

    def test_ready_flag(self):
        portfolio = _empty_portfolio()
        portfolio["current_capital"] = 120.0
        portfolio["closed_positions"] = [{"status": "won"}] * 20 + [{"status": "lost"}] * 11
        summary = portfolio_summary(portfolio)
        self.assertTrue(summary["ready_for_real"])
        self.assertGreater(summary["total_pnl"], 0)


class TestPersistence(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            portfolio = _empty_portfolio()
            portfolio["current_capital"] = 95.0
            save_portfolio(portfolio, path)
            loaded = load_portfolio(path)
            self.assertEqual(loaded["current_capital"], 95.0)
        finally:
            os.unlink(path)

    def test_load_missing_file(self):
        portfolio = load_portfolio("/tmp/nonexistent_paper_portfolio.json")
        self.assertEqual(portfolio["starting_capital"], 100.0)


class TestUpdateIntegration(unittest.TestCase):
    def test_full_cycle(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            alerts = [
                _make_alert("a1", score=70, entry_price=0.50, remaining_edge=0.50),
                _make_alert("a2", score=55, entry_price=0.60, remaining_edge=0.40, market_id="mkt-002"),
            ]
            summary = update_paper_portfolio(alerts, path=path)
            self.assertEqual(summary["open_positions"], 2)
            self.assertGreater(summary["total_trades"], 0)
            self.assertLess(summary["current_equity"], 100.01)

            # Second run with same alerts should not re-open
            summary2 = update_paper_portfolio(alerts, path=path)
            self.assertEqual(summary2["open_positions"], 2)
            self.assertEqual(summary2["total_trades"], summary["total_trades"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
