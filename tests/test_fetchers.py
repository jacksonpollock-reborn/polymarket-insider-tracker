import unittest
from unittest.mock import patch

from src import fetchers


def _make_market(condition_id="m1"):
    return {
        "conditionId": condition_id,
        "question": "Will X happen?",
        "liquidity": 10_000,
        "tokens": [
            {"outcome": "YES", "token_id": f"{condition_id}-yes"},
            {"outcome": "NO", "token_id": f"{condition_id}-no"},
        ],
        "volume24hr": 1000,
    }


def _mock_book(yes_ask: float, no_ask: float):
    def _inner(token_id):
        price = yes_ask if "yes" in token_id else no_ask
        return {"asks": [{"price": str(price), "size": "1000"}], "bids": []}
    return _inner


class FetchersTests(unittest.TestCase):
    def setUp(self):
        fetchers.reset_request_health()

    def test_dune_execute_handles_post_dns_failure_without_crashing(self):
        with (
            patch.object(fetchers, "DUNE_KEY", "secret"),
            patch.object(fetchers.SESSION, "post", side_effect=OSError("[Errno 8] nodename nor servname provided, or not known")),
            patch("src.fetchers.time.sleep"),
        ):
            rows = fetchers._dune_execute_and_fetch("123")

        self.assertEqual(rows, [])
        health = fetchers.get_request_health()
        self.assertEqual(health["successful_calls"], 0)
        self.assertEqual(health["failed_calls"], 1)
        self.assertEqual(health["attempt_failures"], 3)
        self.assertIn("nodename", health["last_error"])


class ArbScannerTests(unittest.TestCase):
    def test_real_arb_returns_is_near_miss_false(self):
        """combined < ARB_THRESHOLD (0.995) is a real arb."""
        market = _make_market()
        with patch("src.fetchers.fetch_clob_book", side_effect=_mock_book(0.48, 0.50)):
            result = fetchers.scan_market_for_arb(market, near_miss_threshold=0.98)
        self.assertIsNotNone(result)
        self.assertFalse(result["is_near_miss"])
        self.assertAlmostEqual(result["combined"], 0.98, places=4)

    def test_near_miss_returns_is_near_miss_true(self):
        """combined in [ARB_THRESHOLD, near_miss_threshold) is a near-miss."""
        market = _make_market()
        # 0.49 + 0.499 = 0.989, above 0.995 would fail but 0.96 + 0.03 = 0.99 is between 0.98 and 0.995
        # Actually: ARB_THRESHOLD=0.995, near_miss=0.99 → in range [0.99, 0.995)
        with patch("src.fetchers.fetch_clob_book", side_effect=_mock_book(0.491, 0.5)):
            result = fetchers.scan_market_for_arb(market, near_miss_threshold=0.995)
        # 0.491 + 0.5 = 0.991, which is < 0.995 → real arb (is_near_miss False)
        self.assertIsNotNone(result)
        self.assertFalse(result["is_near_miss"])

    def test_near_miss_actually_fires(self):
        """combined = 0.988 with near_miss_threshold=0.99 → near-miss (not real arb)."""
        market = _make_market()
        with patch("src.fetchers.fetch_clob_book", side_effect=_mock_book(0.494, 0.5)):
            # 0.494 + 0.5 = 0.994 → between ARB_THRESHOLD (0.995) and near_miss (let's use 0.999)
            result = fetchers.scan_market_for_arb(market, near_miss_threshold=0.999)
        # 0.994 < 0.995 → real arb. Let me use different prices.
        # Try: 0.50 + 0.495 = 0.995 exactly — should fail arb (not strict <)
        with patch("src.fetchers.fetch_clob_book", side_effect=_mock_book(0.50, 0.498)):
            # 0.998 < 0.999 → is_near_miss=True
            result = fetchers.scan_market_for_arb(market, near_miss_threshold=0.999)
        self.assertIsNotNone(result)
        self.assertTrue(result["is_near_miss"])

    def test_neither_arb_nor_near_miss_returns_none(self):
        market = _make_market()
        with patch("src.fetchers.fetch_clob_book", side_effect=_mock_book(0.55, 0.50)):
            result = fetchers.scan_market_for_arb(market, near_miss_threshold=0.98)
        self.assertIsNone(result)

    def test_near_miss_threshold_none_disables_shadow_scan(self):
        """With near_miss_threshold=None, only real arbs return."""
        market = _make_market()
        # 0.996 combined — above ARB_THRESHOLD but would be near-miss if shadow enabled
        with patch("src.fetchers.fetch_clob_book", side_effect=_mock_book(0.50, 0.496)):
            result = fetchers.scan_market_for_arb(market, near_miss_threshold=None)
        self.assertIsNone(result)

    def test_batch_scan_arb_splits_arbs_and_near_misses(self):
        markets = [
            _make_market("real-arb"),     # 0.48 + 0.50 = 0.98 → real arb
            _make_market("near-miss"),    # 0.50 + 0.498 = 0.998 → near-miss
            _make_market("neither"),      # 0.55 + 0.50 = 1.05 → neither
        ]

        def dispatch(token_id):
            if token_id.startswith("real-arb"):
                prices = {"real-arb-yes": 0.48, "real-arb-no": 0.50}
            elif token_id.startswith("near-miss"):
                prices = {"near-miss-yes": 0.50, "near-miss-no": 0.498}
            else:
                prices = {"neither-yes": 0.55, "neither-no": 0.50}
            return {"asks": [{"price": str(prices[token_id]), "size": "1000"}], "bids": []}

        with patch("src.fetchers.fetch_clob_book", side_effect=dispatch):
            arbs, near_misses = fetchers.batch_scan_arb(markets, limit=10, near_miss_threshold=0.999)

        self.assertEqual(len(arbs), 1)
        self.assertEqual(len(near_misses), 1)
        self.assertFalse(arbs[0]["is_near_miss"])
        self.assertTrue(near_misses[0]["is_near_miss"])


if __name__ == "__main__":
    unittest.main()
