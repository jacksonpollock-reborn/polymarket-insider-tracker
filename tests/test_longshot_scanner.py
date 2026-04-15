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
    yes_price: float = 0.50,
    no_price: float = 0.50,
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
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{yes_price}", "{no_price}"]',
    }


def _make_book(best_ask: float, size: float = 1000.0):
    """Legacy helper kept for the regression test that still uses CLOB shape."""
    return {
        "asks": [{"price": str(best_ask), "size": str(size)}],
        "bids": [],
    }


def _mock_clob(yes_ask: float, no_ask: float):
    """Return a fetch_clob_book mock that routes yes/no based on token_id.

    Note: the current longshot scanner uses outcomePrices directly and does
    NOT call fetch_clob_book. This mock is kept for backward compatibility
    with older tests; new tests should use `yes_price`/`no_price` on
    `_make_market` instead.
    """
    def _inner(token_id: str):
        if "yes" in token_id:
            return _make_book(yes_ask)
        return _make_book(no_ask)
    return _inner


# ─── Longshot Fade ─────────────────────────────────────────────────────────────


class TestLongshotFade(unittest.TestCase):
    def test_returns_none_when_neither_side_is_longshot(self):
        market = _make_market(yes_price=0.45, no_price=0.56)
        result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_returns_opportunity_when_yes_is_in_longshot_band(self):
        """YES at 0.08 is inside [0.05, 0.15) longshot band → we buy NO."""
        market = _make_market(yes_price=0.08, no_price=0.92)
        result = scan_market_for_longshot(market)
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "longshot_fade")
        self.assertEqual(result["best_bucket"], "longshot_fade")
        self.assertEqual(result["longshot_side"], "YES")
        self.assertEqual(result["suggested_outcome"], "NO")
        self.assertEqual(result["fade_entry_price"], 0.92)
        self.assertAlmostEqual(result["shared_features"]["remaining_edge_pct"], 0.08, places=4)

    def test_skips_when_longshot_below_min_ask(self):
        """Longshots below 0.05 get rejected — fade entry would be >= 0.95 (too thin)."""
        market = _make_market(yes_price=0.02, no_price=0.98)
        result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_skips_when_longshot_above_max_ask(self):
        """Prices >= 0.15 are not longshots."""
        market = _make_market(yes_price=0.20, no_price=0.80)
        result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_fade_entry_stays_within_tradeable_band(self):
        """For any valid longshot hit, fade_entry_price must be in [0.85, 0.95]."""
        for longshot_side, longshot_price in [("YES", 0.06), ("YES", 0.10), ("YES", 0.14),
                                                ("NO", 0.06), ("NO", 0.10), ("NO", 0.14)]:
            if longshot_side == "YES":
                market = _make_market(yes_price=longshot_price, no_price=1 - longshot_price)
            else:
                market = _make_market(yes_price=1 - longshot_price, no_price=longshot_price)
            result = scan_market_for_longshot(market)
            self.assertIsNotNone(result, f"Failed to fire on {longshot_side}={longshot_price}")
            fade_entry = result["fade_entry_price"]
            self.assertGreaterEqual(fade_entry, 0.85,
                f"Fade entry {fade_entry} below 0.85 for {longshot_side}={longshot_price}")
            self.assertLess(fade_entry, 0.96,
                f"Fade entry {fade_entry} above 0.95 for {longshot_side}={longshot_price}")
            # Must also pass the paper_trader floor (remaining_edge >= 0.05)
            self.assertGreaterEqual(result["shared_features"]["remaining_edge_pct"], 0.05,
                f"Remaining edge too low for paper_trader at {longshot_side}={longshot_price}")

    def test_returns_opportunity_when_no_is_deep_longshot(self):
        """NO is a deep longshot at 0.10 → we buy YES."""
        market = _make_market(yes_price=0.91, no_price=0.10)
        result = scan_market_for_longshot(market)
        self.assertIsNotNone(result)
        self.assertEqual(result["longshot_side"], "NO")
        self.assertEqual(result["suggested_outcome"], "YES")
        self.assertEqual(result["fade_entry_price"], 0.91)

    def test_skips_illiquid_markets(self):
        market = _make_market(liquidity=500.0, yes_price=0.08, no_price=0.93)
        result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_skips_near_resolution_markets(self):
        """days_to_end < 1 is below longshot_fade minimum."""
        market = _make_market(days_to_end=0.5, yes_price=0.08, no_price=0.93)
        result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_skips_too_distant_markets(self):
        """days_to_end > 180 is above longshot_fade maximum."""
        market = _make_market(days_to_end=200.0, yes_price=0.08, no_price=0.92)
        result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_skips_broken_prices_with_both_sides_longshot(self):
        """If both YES and NO prices are below threshold, something is wrong."""
        market = _make_market(yes_price=0.05, no_price=0.08)
        result = scan_market_for_longshot(market)
        self.assertIsNone(result)

    def test_deterministic_alert_id_for_same_market(self):
        """Re-scanning the same market returns the same alert_id so paper_trader dedupes."""
        market1 = _make_market(yes_price=0.08, no_price=0.93)
        market2 = _make_market(yes_price=0.09, no_price=0.92)
        r1 = scan_market_for_longshot(market1)
        r2 = scan_market_for_longshot(market2)
        self.assertEqual(r1["alert_id"], r2["alert_id"])

    def test_parses_real_gamma_api_shape(self):
        """Real Polymarket API returns clobTokenIds, outcomes, outcomePrices as JSON strings."""
        end_dt = datetime.now(timezone.utc) + timedelta(days=10)
        market = {
            "conditionId": "0xabc",
            "question": "Will X happen by April 30?",
            "liquidity": 10_000.0,
            "endDateIso": end_dt.isoformat(),
            # This is the EXACT shape the real Gamma API returns
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.08", "0.93"]',
            "clobTokenIds": '["yes-token-12345", "no-token-67890"]',
        }
        result = scan_market_for_longshot(market)
        self.assertIsNotNone(result)
        self.assertEqual(result["longshot_side"], "YES")
        self.assertEqual(result["suggested_outcome"], "NO")
        self.assertEqual(result["yes_token_id"], "yes-token-12345")
        self.assertEqual(result["no_token_id"], "no-token-67890")
        self.assertEqual(result["fade_entry_price"], 0.93)

    def test_synthetic_alert_shape_has_all_paper_trader_fields(self):
        """Scanner output must be usable directly as a paper_trader alert."""
        market = _make_market(yes_price=0.08, no_price=0.93)
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
        market_far = _make_market(days_to_end=5.0, yes_price=0.08, no_price=0.93)
        self.assertIsNone(scan_market_for_resolution_short(market_far))
        market_close = _make_market(days_to_end=0.2, yes_price=0.08, no_price=0.93)
        self.assertIsNone(scan_market_for_resolution_short(market_close))

    def test_fires_inside_window_and_below_threshold(self):
        """Deep longshot in the last 12–36h fires."""
        market = _make_market(days_to_end=1.0, yes_price=0.10, no_price=0.91)
        result = scan_market_for_resolution_short(market)
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "resolution_short")
        self.assertEqual(result["best_bucket"], "resolution_short")

    def test_skips_inside_window_above_threshold(self):
        """Inside time window but longshot side ≥ 0.15 is rejected."""
        market = _make_market(days_to_end=1.0, yes_price=0.20, no_price=0.81)
        result = scan_market_for_resolution_short(market)
        self.assertIsNone(result)

    def test_resolution_short_uses_separate_alert_id_from_longshot_fade(self):
        """The same market in both scanners produces different alert_ids (different kinds)."""
        # 1.2 days is inside both longshot_fade (>= 1) and resolution_short ([0.5, 1.5]) ranges
        market = _make_market(days_to_end=1.2, yes_price=0.08, no_price=0.93)
        lf = scan_market_for_longshot(market)
        rs = scan_market_for_resolution_short(market)
        self.assertIsNotNone(lf)
        self.assertIsNotNone(rs)
        self.assertNotEqual(lf["alert_id"], rs["alert_id"])


# ─── Batch Scanners ────────────────────────────────────────────────────────────


class TestBatchScanners(unittest.TestCase):
    def test_batch_longshot_sorts_and_limits(self):
        markets = [
            _make_market(condition_id="a", yes_token="a-yes", no_token="a-no",
                         yes_price=0.10, no_price=0.91),
            _make_market(condition_id="b", yes_token="b-yes", no_token="b-no",
                         yes_price=0.10, no_price=0.91),
        ]
        markets[0]["volume24hr"] = 100
        markets[1]["volume24hr"] = 200

        results = batch_scan_longshot(markets, limit=10)
        self.assertEqual(len(results), 2)

    def test_batch_resolution_short_returns_empty_when_no_opportunities(self):
        markets = [_make_market(days_to_end=5.0, yes_price=0.10, no_price=0.91)]  # outside window
        results = batch_scan_resolution_short(markets, limit=10)
        self.assertEqual(len(results), 0)


if __name__ == "__main__":
    unittest.main()
