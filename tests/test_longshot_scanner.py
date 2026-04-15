"""Tests for the longshot fade + resolution proximity short scanners."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src import longshot_scanner
from src.longshot_scanner import (
    scan_market_for_longshot,
    scan_market_for_resolution_short,
    batch_scan_longshot,
    batch_scan_resolution_short,
)


def _make_market(
    *,
    yes_token="yes-tok-1",
    no_token="no-tok-1",
    liquidity=10_000.0,
    days_to_end=10.0,
    question="Will X happen by April 30?",
    condition_id="cond-001",
    category="Politics",
):
    end_dt = datetime.now(timezone.utc) + timedelta(days=days_to_end)
    return {
        "conditionId": condition_id,
        "question": question,
        "liquidity": liquidity,
        "end_date_iso": end_dt.isoformat(),
        "_detected_category": category,
        "tokens": [
            {"outcome": "YES", "token_id": yes_token},
            {"outcome": "NO", "token_id": no_token},
        ],
    }


def _make_book(best_ask: float, size: float = 1000.0):
    return {
        "asks": [{"price": str(best_ask), "size": str(size)}],
        "bids": [],
    }


def _mock_clob(yes_ask: float, no_ask: float):
    """Return a fetch_clob_book mock that routes yes/no based on token_id."""
    def _inner(token_id: str):
        if "yes" in token_id:
            return _make_book(yes_ask)
        return _make_book(no_ask)
    return _inner


# ─── Longshot Fade ─────────────────────────────────────────────────────────────


class TestLongshotFade(unittest.TestCase):
    def test_returns_none_when_neither_side_is_longshot(self):
        market = _make_market()
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.45, 0.56)):
            result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_returns_opportunity_when_yes_is_deep_longshot(self):
        """YES is a deep longshot at 0.08 → we buy NO."""
        market = _make_market()
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.08, 0.93)):
            result = scan_market_for_longshot(market)
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "longshot_fade")
        self.assertEqual(result["best_bucket"], "longshot_fade")
        self.assertEqual(result["longshot_side"], "YES")
        self.assertEqual(result["suggested_outcome"], "NO")
        self.assertEqual(result["fade_entry_price"], 0.93)
        self.assertAlmostEqual(result["shared_features"]["remaining_edge_pct"], 0.07, places=4)

    def test_returns_opportunity_when_no_is_deep_longshot(self):
        """NO is a deep longshot at 0.10 → we buy YES."""
        market = _make_market()
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.91, 0.10)):
            result = scan_market_for_longshot(market)
        self.assertIsNotNone(result)
        self.assertEqual(result["longshot_side"], "NO")
        self.assertEqual(result["suggested_outcome"], "YES")
        self.assertEqual(result["fade_entry_price"], 0.91)

    def test_skips_illiquid_markets(self):
        market = _make_market(liquidity=500.0)  # below $5k default
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.08, 0.93)):
            result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_skips_near_resolution_markets(self):
        """days_to_end < 1 is below longshot_fade minimum."""
        market = _make_market(days_to_end=0.5)
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.08, 0.93)):
            result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_skips_too_distant_markets(self):
        """days_to_end > 30 is above longshot_fade maximum."""
        market = _make_market(days_to_end=60.0)
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.08, 0.93)):
            result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_skips_broken_books_with_both_sides_longshot(self):
        """If both YES and NO asks are below threshold, the book is broken."""
        market = _make_market()
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.05, 0.08)):
            result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_deterministic_alert_id_for_same_market(self):
        """Re-scanning the same market returns the same alert_id so paper_trader dedupes."""
        market = _make_market()
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.08, 0.93)):
            r1 = scan_market_for_longshot(market)
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.09, 0.92)):
            r2 = scan_market_for_longshot(market)
        self.assertEqual(r1["alert_id"], r2["alert_id"])

    def test_synthetic_alert_shape_has_all_paper_trader_fields(self):
        """Scanner output must be usable directly as a paper_trader alert."""
        market = _make_market()
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.08, 0.93)):
            result = scan_market_for_longshot(market)
        # paper_trader.open_positions requires these fields
        self.assertIn("alert_id", result)
        self.assertIn("best_bucket", result)
        self.assertIn("best_score", result)
        self.assertIn("market_name", result)
        self.assertIn("market_id", result)
        self.assertIn("market_end", result)
        self.assertIn("suggested_outcome", result)
        self.assertIn("tokens", result)
        self.assertIn("active_exposure", result)
        self.assertIn("entry_price", result["active_exposure"])
        self.assertIn("shared_features", result)
        self.assertIn("remaining_edge_pct", result["shared_features"])


# ─── Resolution Proximity Short ────────────────────────────────────────────────


class TestResolutionShort(unittest.TestCase):
    def test_skips_outside_time_window(self):
        """days_to_end outside [0.5, 1.5] is rejected."""
        # Too far
        market_far = _make_market(days_to_end=5.0)
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.08, 0.93)):
            self.assertIsNone(scan_market_for_resolution_short(market_far))
        # Too close (about to expire)
        market_close = _make_market(days_to_end=0.2)
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.08, 0.93)):
            self.assertIsNone(scan_market_for_resolution_short(market_close))

    def test_fires_inside_window_and_below_threshold(self):
        """Deep longshot in the last 12–36h fires."""
        market = _make_market(days_to_end=1.0)
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.10, 0.91)):
            result = scan_market_for_resolution_short(market)
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "resolution_short")
        self.assertEqual(result["best_bucket"], "resolution_short")

    def test_skips_inside_window_above_threshold(self):
        """Inside time window but longshot side ≥ 0.15 is rejected."""
        market = _make_market(days_to_end=1.0)
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.20, 0.81)):
            result = scan_market_for_resolution_short(market)
        self.assertIsNone(result)

    def test_resolution_short_uses_separate_alert_id_from_longshot_fade(self):
        """The same market in both scanners produces different alert_ids (different kinds)."""
        # 1.2 days is inside both longshot_fade (>= 1) and resolution_short ([0.5, 1.5]) ranges
        market = _make_market(days_to_end=1.2)
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.08, 0.93)):
            lf = scan_market_for_longshot(market)
            rs = scan_market_for_resolution_short(market)
        self.assertIsNotNone(lf)
        self.assertIsNotNone(rs)
        # Their alert IDs must not collide even though the market is the same
        self.assertNotEqual(lf["alert_id"], rs["alert_id"])


# ─── Batch Scanners ────────────────────────────────────────────────────────────


class TestBatchScanners(unittest.TestCase):
    def test_batch_longshot_sorts_and_limits(self):
        markets = [
            _make_market(condition_id="a", yes_token="a-yes", no_token="a-no"),
            _make_market(condition_id="b", yes_token="b-yes", no_token="b-no"),
        ]
        markets[0]["volume24hr"] = 100
        markets[1]["volume24hr"] = 200

        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.10, 0.91)):
            results = batch_scan_longshot(markets, limit=10)
        self.assertEqual(len(results), 2)

    def test_batch_resolution_short_returns_empty_when_no_opportunities(self):
        markets = [_make_market(days_to_end=5.0)]  # outside window
        with patch.object(longshot_scanner, "fetch_clob_book", side_effect=_mock_clob(0.10, 0.91)):
            results = batch_scan_resolution_short(markets, limit=10)
        self.assertEqual(len(results), 0)


if __name__ == "__main__":
    unittest.main()
