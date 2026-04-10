import unittest

from src.reporter import build_html_report
from src.scorer import MIN_BET_USDC, score_alert


def make_trade(
    timestamp,
    side,
    outcome,
    price,
    usdc_size,
    wallet="0x" + "1" * 40,
    market_id="m1",
    market_name="Test Market",
    category="Politics",
    market_end="2026-04-07T12:00:00Z",
    liquidity=40000,
    spike_ratio=1.2,
):
    return {
        "timestamp": timestamp,
        "side": side,
        "outcome": outcome,
        "price": price,
        "size": usdc_size / price if price else 0,
        "proxyWallet": wallet,
        "_market_address": market_id,
        "_market_name": market_name,
        "_market_liquidity": liquidity,
        "_market_end": market_end,
        "_market_category": category,
        "_spike_ratio": spike_ratio,
        "usdcSize": usdc_size,
    }


def make_market(
    market_id="m1",
    market_name="Test Market",
    category="Politics",
    market_end="2026-04-07T12:00:00Z",
    liquidity=40000,
    spike_ratio=1.2,
):
    return {
        "market_id": market_id,
        "market_name": market_name,
        "category": category,
        "market_end": market_end,
        "market_liquidity": liquidity,
        "spike_ratio": spike_ratio,
    }


class StrategyRefactorTests(unittest.TestCase):
    def test_html_report_surfaces_unhealthy_run_banner(self):
        html = build_html_report(
            [],
            "Tuesday, April 08 2026 · 00:00 UTC",
            {"flagged_alerts": 0, "candidate_alerts": 0, "insider_watchlist": 0, "sports_watchlist": 0, "momentum_watchlist": 0, "contrarian_watchlist": 0},
            arb_alerts=[],
            run_health={
                "status": "unhealthy",
                "reason": "market_bootstrap_failed: DNS resolution failed",
                "request_health": {"successful_calls": 0, "failed_calls": 6, "attempt_failures": 18},
            },
        )

        self.assertIn("Run Unhealthy", html)
        self.assertIn("market_bootstrap_failed", html)
        self.assertIn("Failed calls:</b> 6", html)

    def test_html_report_moves_thin_edge_follow_alerts_to_warning_section(self):
        alert = {
            "wallet_address": "0x" + "9" * 40,
            "market_name": "High Edge Warning Market",
            "best_bucket": "insider",
            "best_score": 62,
            "candidate_score": 54,
            "category": "Politics",
            "market_end": "2026-04-07T12:00:00Z",
            "review_status": "pending",
            "entity_label": "Unknown",
            "entity_type": "unknown",
            "recommended_action": "follow",
            "suggested_outcome": "NO",
            "thin_edge_follow": True,
            "core_reasons": ["One-sided exposure into NO"],
            "caution_flags": ["Entry is too close to 1.00 to leave much remaining edge"],
            "funding_warnings": [],
            "recent_trades": [],
            "shared_features": {"market_liquidity": 100000, "capital_impact_pct": 15.0},
            "historical_record": {"overall_win_rate": None, "total_wins": 0, "total_resolved": 0, "longshot_win_rate": None},
            "active_exposure": {"dominant_usdc": 9000, "dominant_outcome": "NO", "entry_price": 0.97, "hedge_ratio": 0.0},
        }
        html = build_html_report(
            [alert],
            "Tuesday, April 08 2026 · 00:00 UTC",
            {"flagged_alerts": 1, "candidate_alerts": 1, "insider_watchlist": 1, "sports_watchlist": 0, "momentum_watchlist": 0, "contrarian_watchlist": 0},
        )

        self.assertIn("Thin-Edge Follow Alerts", html)
        self.assertIn("High Edge Warning Market", html)
        self.assertIn("Insider Strategy · Thin Edge", html)

    def test_buy_yes_and_buy_no_counts_as_balanced_but_buy_then_sell_does_not(self):
        market = make_market()
        balanced_trades = [
            make_trade("2026-04-05T10:00:00Z", "BUY", "YES", 0.55, 7000),
            make_trade("2026-04-05T10:15:00Z", "BUY", "NO", 0.42, 6500),
        ]
        balanced = score_alert(
            address="0x" + "1" * 40,
            market=market,
            alert_trades=balanced_trades,
            market_trades=balanced_trades,
            polymarket_activity=[],
            positions=[],
            polygon_data={},
            arkham_data={},
            dune_whale_list=[],
            dune_new_wallet_list=[],
        )

        exit_trades = [
            make_trade("2026-04-05T10:00:00Z", "BUY", "YES", 0.55, 8000),
            make_trade("2026-04-05T10:30:00Z", "SELL", "YES", 0.58, 2000),
        ]
        exited = score_alert(
            address="0x" + "2" * 40,
            market=market,
            alert_trades=exit_trades,
            market_trades=exit_trades,
            polymarket_activity=[],
            positions=[],
            polygon_data={},
            arkham_data={},
            dune_whale_list=[],
            dune_new_wallet_list=[],
        )

        self.assertTrue(balanced["shared_features"]["balanced_outcomes"])
        self.assertFalse(balanced["shared_features"]["directional_conviction"])
        self.assertFalse(exited["shared_features"]["balanced_outcomes"])
        self.assertTrue(exited["shared_features"]["directional_conviction"])

    def test_timing_uses_trade_time_to_market_end(self):
        market = make_market(market_end="2026-04-07T12:00:00Z")
        alert = [
            make_trade("2026-04-06T12:30:00Z", "BUY", "YES", 0.48, MIN_BET_USDC + 1000),
        ]
        scored = score_alert(
            address="0x" + "3" * 40,
            market=market,
            alert_trades=alert,
            market_trades=alert,
            polymarket_activity=[],
            positions=[],
            polygon_data={},
            arkham_data={},
            dune_whale_list=[],
            dune_new_wallet_list=[],
        )

        self.assertTrue(scored["shared_features"]["true_timing"])
        self.assertLess(scored["shared_features"]["timing_hours_to_resolution"], 72)

    def test_follow_alert_with_very_high_entry_price_is_flagged_but_not_blocked(self):
        market = make_market(
            market_name="High Confidence Market",
            category="Politics",
            market_end="2026-04-07T12:00:00Z",
            liquidity=30000,
        )
        alert = [
            make_trade("2026-04-06T18:00:00Z", "BUY", "NO", 0.97, 9000, category="Politics", market_end="2026-04-07T12:00:00Z", liquidity=30000),
        ]
        scored = score_alert(
            address="0x" + "e" * 40,
            market=market,
            alert_trades=alert,
            market_trades=alert,
            polymarket_activity=[],
            positions=[{"outcome": "won", "cashPnl": 100, "avgPrice": 0.18} for _ in range(9)],
            polygon_data={"tx_count": 20},
            arkham_data={"label": "Alpha Capital", "type": "fund", "cluster_size": 4, "related_addresses": ["0x" + "f" * 40]},
            dune_whale_list=[],
            dune_new_wallet_list=[],
        )

        self.assertTrue(scored["shared_features"]["low_remaining_edge"])
        self.assertEqual(scored["recommended_action"], "follow")
        self.assertTrue(scored["passes_strategy_threshold"])
        self.assertTrue(scored["thin_edge_follow"])
        self.assertEqual(scored["strategy_blockers"], [])
        self.assertIn("Entry is too close to 1.00 to leave much remaining edge", scored["caution_flags"])

    def test_quick_flip_penalizes_without_exploding(self):
        market = make_market()
        alert = [
            make_trade("2026-04-05T10:00:00Z", "BUY", "YES", 0.50, 7000),
        ]
        baseline = score_alert(
            address="0x" + "4" * 40,
            market=market,
            alert_trades=alert,
            market_trades=alert,
            polymarket_activity=[],
            positions=[],
            polygon_data={},
            arkham_data={},
            dune_whale_list=[],
            dune_new_wallet_list=[],
        )

        activity = [
            make_trade("2026-04-05T10:00:00Z", "BUY", "YES", 0.50, 7000),
            make_trade("2026-04-05T12:00:00Z", "SELL", "YES", 0.56, 7000),
            make_trade("2026-04-05T12:15:00Z", "SELL", "YES", 0.57, 7000),
        ]
        with_flip = score_alert(
            address="0x" + "4" * 40,
            market=market,
            alert_trades=alert,
            market_trades=alert,
            polymarket_activity=activity,
            positions=[],
            polygon_data={},
            arkham_data={},
            dune_whale_list=[],
            dune_new_wallet_list=[],
        )

        self.assertTrue(with_flip["shared_features"]["quick_flip"])
        self.assertEqual(len(with_flip["shared_features"]["quick_flips"]), 1)
        self.assertLess(with_flip["candidate_score"], baseline["candidate_score"])

    def test_bucket_routing_covers_all_four_strategies(self):
        insider_market = make_market(category="Politics", market_end="2026-04-06T12:00:00Z", liquidity=30000)
        insider_alert = [
            make_trade("2026-04-05T18:00:00Z", "BUY", "YES", 0.35, 9000, category="Politics", liquidity=30000, market_end="2026-04-06T12:00:00Z"),
        ]
        insider = score_alert(
            address="0x" + "5" * 40,
            market=insider_market,
            alert_trades=insider_alert,
            market_trades=insider_alert,
            polymarket_activity=[],
            positions=[{"outcome": "won", "cashPnl": 100, "avgPrice": 0.18} for _ in range(9)],
            polygon_data={
                "funding_flags": ["⚠️ Funded by known mixer"],
                "usdc_inflows": [{"timestamp": 1775410200}],
                "tx_count": 20,
            },
            arkham_data={"label": "Alpha Capital", "type": "fund", "cluster_size": 4, "related_addresses": ["0x" + "6" * 40]},
            dune_whale_list=[],
            dune_new_wallet_list=[],
        )

        sports_market = make_market(
            market_id="m2",
            market_name="Sports Market",
            category="Sports",
            market_end="2026-04-05T20:00:00Z",
            liquidity=25000,
        )
        sports_alert = [
            make_trade("2026-04-05T18:30:00Z", "BUY", "YES", 0.48, 7000, wallet="0x" + "7" * 40, market_id="m2", market_name="Sports Market", category="Sports", market_end="2026-04-05T20:00:00Z", liquidity=25000),
        ]
        sports_market_trades = sports_alert + [
            make_trade("2026-04-05T19:00:00Z", "BUY", "YES", 0.56, 2000, wallet="0x" + "8" * 40, market_id="m2", market_name="Sports Market", category="Sports", market_end="2026-04-05T20:00:00Z", liquidity=25000),
        ]
        sports = score_alert(
            address="0x" + "7" * 40,
            market=sports_market,
            alert_trades=sports_alert,
            market_trades=sports_market_trades,
            polymarket_activity=[],
            positions=[],
            polygon_data={},
            arkham_data={},
            dune_whale_list=[],
            dune_new_wallet_list=[],
            swarm_cluster_size=4,
        )

        momentum_market = make_market(market_id="m3", market_name="Momentum Market", category="Crypto", liquidity=40000, spike_ratio=3.5)
        momentum_alert = [
            make_trade("2026-04-05T10:00:00Z", "BUY", "YES", 0.55, 7000, wallet="0x" + "9" * 40, market_id="m3", market_name="Momentum Market", category="Crypto", spike_ratio=3.5),
            make_trade("2026-04-05T11:00:00Z", "BUY", "YES", 0.60, 4000, wallet="0x" + "9" * 40, market_id="m3", market_name="Momentum Market", category="Crypto", spike_ratio=3.5),
        ]
        momentum_market_trades = [
            make_trade("2026-04-05T08:00:00Z", "BUY", "YES", 0.40, 1000, wallet="0x" + "a" * 40, market_id="m3", market_name="Momentum Market", category="Crypto", spike_ratio=3.5),
            make_trade("2026-04-05T09:00:00Z", "BUY", "YES", 0.46, 1000, wallet="0x" + "a" * 40, market_id="m3", market_name="Momentum Market", category="Crypto", spike_ratio=3.5),
            *momentum_alert,
            make_trade("2026-04-05T12:00:00Z", "BUY", "YES", 0.68, 1500, wallet="0x" + "b" * 40, market_id="m3", market_name="Momentum Market", category="Crypto", spike_ratio=3.5),
            make_trade("2026-04-05T13:00:00Z", "BUY", "YES", 0.72, 1500, wallet="0x" + "c" * 40, market_id="m3", market_name="Momentum Market", category="Crypto", spike_ratio=3.5),
        ]
        momentum = score_alert(
            address="0x" + "9" * 40,
            market=momentum_market,
            alert_trades=momentum_alert,
            market_trades=momentum_market_trades,
            polymarket_activity=[],
            positions=[],
            polygon_data={},
            arkham_data={},
            dune_whale_list=[],
            dune_new_wallet_list=[],
        )

        contrarian_market = make_market(market_id="m4", market_name="Contrarian Market", category="Crypto", liquidity=40000, spike_ratio=3.5)
        contrarian_alert = [
            make_trade("2026-04-05T11:30:00Z", "BUY", "YES", 0.72, 7000, wallet="0x" + "d" * 40, market_id="m4", market_name="Contrarian Market", category="Crypto", spike_ratio=3.5),
        ]
        contrarian_market_trades = [
            make_trade("2026-04-05T08:00:00Z", "BUY", "YES", 0.40, 1000, wallet="0x" + "e" * 40, market_id="m4", market_name="Contrarian Market", category="Crypto", spike_ratio=3.5),
            make_trade("2026-04-05T09:00:00Z", "BUY", "YES", 0.52, 1000, wallet="0x" + "e" * 40, market_id="m4", market_name="Contrarian Market", category="Crypto", spike_ratio=3.5),
            make_trade("2026-04-05T10:00:00Z", "BUY", "YES", 0.64, 1000, wallet="0x" + "e" * 40, market_id="m4", market_name="Contrarian Market", category="Crypto", spike_ratio=3.5),
            *contrarian_alert,
            make_trade("2026-04-05T12:30:00Z", "BUY", "YES", 0.58, 1500, wallet="0x" + "f" * 40, market_id="m4", market_name="Contrarian Market", category="Crypto", spike_ratio=3.5),
            make_trade("2026-04-05T13:00:00Z", "BUY", "YES", 0.55, 1500, wallet="0x" + "f" * 40, market_id="m4", market_name="Contrarian Market", category="Crypto", spike_ratio=3.5),
        ]
        contrarian = score_alert(
            address="0x" + "d" * 40,
            market=contrarian_market,
            alert_trades=contrarian_alert,
            market_trades=contrarian_market_trades,
            polymarket_activity=[],
            positions=[],
            polygon_data={},
            arkham_data={},
            dune_whale_list=[],
            dune_new_wallet_list=[],
        )

        self.assertEqual(insider["best_bucket"], "insider")
        self.assertEqual(sports["best_bucket"], "sports_news")
        self.assertEqual(momentum["best_bucket"], "momentum")
        self.assertEqual(contrarian["best_bucket"], "contrarian")

    def test_html_report_renders_all_bucket_sections(self):
        stats = {
            "flagged_alerts": 4,
            "candidate_alerts": 4,
            "insider_watchlist": 1,
            "sports_watchlist": 1,
            "momentum_watchlist": 1,
            "contrarian_watchlist": 1,
        }
        watchlist = []
        for idx, bucket in enumerate(["insider", "sports_news", "momentum", "contrarian"], start=1):
            watchlist.append({
                "wallet_address": f"0x{idx:040x}",
                "market_name": f"Market {idx}",
                "market_end": "2026-04-07T12:00:00Z",
                "category": "Politics" if bucket != "sports_news" else "Sports",
                "best_bucket": bucket,
                "best_score": 60,
                "candidate_score": 32,
                "active_exposure": {
                    "dominant_usdc": 7000,
                    "dominant_outcome": "YES",
                    "hedge_ratio": 0.0,
                    "entry_price": 0.52,
                },
                "core_reasons": ["Reason one", "Reason two"],
                "caution_flags": ["Caution one"],
                "recent_trades": [{"timestamp": "2026-04-05T10:00:00+00:00", "side": "BUY", "outcome": "YES", "amount_usdc": 7000, "price": 0.52}],
                "shared_features": {"market_liquidity": 40000, "capital_impact_pct": 17.5},
                "entity_label": "Unknown",
                "entity_type": "unknown",
                "historical_record": {"overall_win_rate": None, "longshot_win_rate": None, "total_wins": 0, "total_resolved": 0},
                "review_status": "pending",
                "recommended_action": "follow" if bucket != "contrarian" else "fade",
                "suggested_outcome": "YES" if bucket != "contrarian" else "NO",
                "funding_warnings": [],
            })

        html = build_html_report(watchlist, "Sunday, April 05 2026 · 12:00 UTC", stats, arb_alerts=[])
        self.assertIn("Insider Strategy", html)
        self.assertIn("Sports News Strategy", html)
        self.assertIn("Momentum Strategy", html)
        self.assertIn("Contrarian Strategy", html)


if __name__ == "__main__":
    unittest.main()
