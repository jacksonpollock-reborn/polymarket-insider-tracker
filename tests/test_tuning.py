import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.tuning import build_tuning_summary, render_tuning_checklist


def sample_watchlist_payload():
    return {
        "generated_at": "2026-04-06T00:00:00+00:00",
        "stats": {
            "markets_scanned": 140,
            "flagged_markets": 38,
            "large_trades": 7,
            "wallets_evaluated": 6,
            "alerts_scored": 6,
            "candidate_alerts": 6,
            "flagged_alerts": 3,
        },
        "bucket_thresholds": {
            "insider": 40,
            "sports_news": 32,
            "momentum": 35,
            "contrarian": 35,
        },
        "candidate_pool": [
            {
                "best_bucket": "insider",
                "shared_features": {
                    "quick_flip": False,
                    "balanced_outcomes": False,
                    "coordinated_swarm": False,
                    "true_timing": True,
                    "price_context": {"late_chaser": False},
                },
            },
            {
                "best_bucket": "insider",
                "shared_features": {
                    "quick_flip": False,
                    "balanced_outcomes": False,
                    "coordinated_swarm": True,
                    "true_timing": True,
                    "price_context": {"late_chaser": False},
                },
            },
            {
                "best_bucket": "momentum",
                "shared_features": {
                    "quick_flip": False,
                    "balanced_outcomes": False,
                    "coordinated_swarm": False,
                    "true_timing": False,
                    "price_context": {"late_chaser": True},
                },
            },
            {
                "best_bucket": "contrarian",
                "shared_features": {
                    "quick_flip": True,
                    "balanced_outcomes": True,
                    "coordinated_swarm": False,
                    "true_timing": False,
                    "price_context": {"late_chaser": True},
                },
            },
        ],
        "watchlist": [
            {"best_bucket": "insider"},
            {"best_bucket": "momentum"},
            {"best_bucket": "contrarian"},
        ],
        "review_log_path": "review_log.json",
    }


def make_review_entry(
    alert_id,
    bucket,
    generated_at,
    status,
    entry_price=0.5,
    price_1h=None,
    price_6h=None,
    price_24h=None,
    price_res=None,
    mfe=None,
    mae=None,
    feature_tags=None,
):
    return {
        "alert_id": alert_id,
        "bucket": bucket,
        "generated_at": generated_at,
        "entry_price": entry_price,
        "price_after_1h": price_1h,
        "price_after_6h": price_6h,
        "price_after_24h": price_24h,
        "price_at_resolution": price_res,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "review_status": status,
        "feature_tags": feature_tags or [],
    }


class TuningTests(unittest.TestCase):
    def test_tuning_summary_is_built_from_existing_artifacts_only(self):
        payload = sample_watchlist_payload()
        review_entries = [
            make_review_entry("a1", "insider", "2026-04-06T00:00:00+00:00", "resolved_win", price_1h=0.55, price_6h=0.58, price_24h=0.62, price_res=1.0, mfe=0.3, mae=-0.05, feature_tags=["true_timing"]),
            make_review_entry("a2", "momentum", "2026-04-05T00:00:00+00:00", "resolved_loss", price_1h=0.48, price_6h=0.44, price_24h=0.40, price_res=0.0, mfe=0.02, mae=-0.25, feature_tags=["late_chaser"]),
        ]
        summary = build_tuning_summary(payload, review_entries, now=datetime(2026, 4, 6, tzinfo=timezone.utc))

        self.assertEqual(summary["source_artifacts"]["watchlist"], "watchlist.json")
        self.assertEqual(summary["source_artifacts"]["review_log"], "review_log.json")
        self.assertEqual(summary["run_health"]["alerts_scored"], 6)
        self.assertIn("bucket_funnel", summary)
        self.assertIn("bucket_performance", summary)

    def test_checklist_renders_all_required_sections(self):
        summary = build_tuning_summary(sample_watchlist_payload(), [], now=datetime(2026, 4, 6, tzinfo=timezone.utc))
        checklist = render_tuning_checklist(summary)
        self.assertIn("## 1. Run Health", checklist)
        self.assertIn("## 2. Bucket Funnel", checklist)
        self.assertIn("## 3. Bucket Performance", checklist)
        self.assertIn("## 4. Risk Flags", checklist)
        self.assertIn("## 5. Suggested Actions", checklist)
        self.assertIn("## 6. Hold / Observe / Tune Decisions", checklist)

    def test_recommendation_rules_return_hold_observe_and_tune(self):
        payload = sample_watchlist_payload()
        payload["candidate_pool"] = payload["candidate_pool"] * 3
        payload["watchlist"] = payload["watchlist"] * 3

        review_entries = []
        for idx in range(12):
            review_entries.append(
                make_review_entry(
                    f"insider-{idx}",
                    "insider",
                    f"2026-04-{idx % 7 + 1:02d}T00:00:00+00:00",
                    "resolved_win" if idx < 6 else "resolved_loss",
                    price_24h=0.52,
                    price_res=1.0 if idx < 6 else 0.0,
                    mfe=0.1,
                    mae=-0.08,
                )
            )
        for idx in range(12):
            review_entries.append(
                make_review_entry(
                    f"momentum-{idx}",
                    "momentum",
                    f"2026-04-{idx % 7 + 1:02d}T00:00:00+00:00",
                    "resolved_loss" if idx < 8 else "resolved_win",
                    price_24h=0.40 if idx < 8 else 0.56,
                    price_res=0.0 if idx < 8 else 1.0,
                    mfe=0.04,
                    mae=-0.18,
                )
            )
        for idx in range(12):
            review_entries.append(
                make_review_entry(
                    f"contrarian-{idx}",
                    "contrarian",
                    f"2026-04-{idx % 7 + 1:02d}T00:00:00+00:00",
                    "resolved_win" if idx < 8 else "resolved_loss",
                    price_24h=0.62 if idx < 8 else 0.47,
                    price_res=1.0 if idx < 8 else 0.0,
                    mfe=0.12,
                    mae=-0.04,
                )
            )

        summary = build_tuning_summary(payload, review_entries, now=datetime(2026, 4, 6, tzinfo=timezone.utc))
        decisions = summary["tuning_recommendations"]["bucket_decisions"]

        self.assertEqual(decisions["insider"]["action"], "Hold")
        self.assertEqual(decisions["momentum"]["action"], "Tune Threshold Up")
        self.assertEqual(decisions["contrarian"]["action"], "Observe")

    def test_threshold_recommendation_waits_for_minimum_resolved_sample(self):
        payload = sample_watchlist_payload()
        review_entries = [
            make_review_entry("m1", "momentum", "2026-04-01T00:00:00+00:00", "resolved_loss", price_res=0.0)
            for _ in range(11)
        ]
        summary = build_tuning_summary(payload, review_entries, now=datetime(2026, 4, 6, tzinfo=timezone.utc))
        self.assertEqual(summary["tuning_recommendations"]["bucket_decisions"]["momentum"]["action"], "Hold")

    def test_feature_recommendations_ignore_tiny_samples(self):
        payload = sample_watchlist_payload()
        review_entries = []
        for idx in range(19):
            review_entries.append(
                make_review_entry(
                    f"i-{idx}",
                    "insider",
                    "2026-04-01T00:00:00+00:00",
                    "resolved_win" if idx < 10 else "resolved_loss",
                    price_res=1.0 if idx < 10 else 0.0,
                    feature_tags=["true_timing"],
                )
            )
        summary = build_tuning_summary(payload, review_entries, now=datetime(2026, 4, 6, tzinfo=timezone.utc))
        self.assertEqual(summary["tuning_recommendations"]["feature_actions"], [])

    def test_readme_and_agents_match_current_model(self):
        repo_root = Path(__file__).resolve().parents[1]
        readme = (repo_root / "README.md").read_text()
        agents = (repo_root / "AGENTS.md").read_text()
        status = (repo_root / "STATUS.md").read_text()

        self.assertIn("four-bucket", readme.lower())
        self.assertIn("wallet+market", readme)
        self.assertIn("tuning_summary.json", readme)
        self.assertIn("tuning_checklist.md", readme)
        self.assertIn("sports_news", agents)
        self.assertIn("review_log.json", agents)
        self.assertIn("Current Thresholds", status)
        self.assertIn(".codex/worktrees", status)
        self.assertIn("scripts/run_tracker.sh", status)


if __name__ == "__main__":
    unittest.main()
