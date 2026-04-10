import unittest
from unittest.mock import patch

from src import reporter


class ReporterRetryTests(unittest.TestCase):
    def test_send_email_retries_once_and_succeeds(self):
        attempts = {"count": 0}

        class FakeSMTP:
            def __init__(self, host, port):
                attempts["count"] += 1

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def login(self, user, password):
                if attempts["count"] == 1:
                    raise OSError("[Errno 8] nodename nor servname provided, or not known")

            def sendmail(self, from_addr, to_addr, message):
                return None

        with (
            patch.object(reporter, "GMAIL_USER", "sender@example.com"),
            patch.object(reporter, "GMAIL_PASSWORD", "secret"),
            patch.object(reporter, "EMAIL_TO", "dest@example.com"),
            patch.object(reporter, "EMAIL_RETRY_COUNT", 1),
            patch.object(reporter, "EMAIL_RETRY_DELAY_SECONDS", 0),
            patch("src.reporter.time.sleep"),
            patch("src.reporter.smtplib.SMTP_SSL", FakeSMTP),
        ):
            ok = reporter.send_email([], {"flagged_alerts": 0}, arb_alerts=[], run_health={"status": "unhealthy"})

        self.assertTrue(ok)
        self.assertEqual(attempts["count"], 2)

    def test_build_html_report_escapes_market_text(self):
        alert = {
            "wallet_address": "0xabc",
            "market_name": '<script>alert("x")</script>',
            "market_end": "2026-04-20T12:00:00Z",
            "best_bucket": "insider",
            "best_score": 55,
            "candidate_score": 30,
            "category": 'Politics<script>',
            "review_status": "pending",
            "entity_label": "<b>Entity</b>",
            "entity_type": "unknown",
            "recommended_action": "follow",
            "suggested_outcome": "YES",
            "core_reasons": ['High <edge>'],
            "caution_flags": ['Watch "late" move'],
            "funding_warnings": ['Mixer <risk>'],
            "historical_record": {},
            "active_exposure": {
                "dominant_usdc": 5000,
                "dominant_outcome": "YES",
                "hedge_ratio": 0.0,
                "entry_price": 0.71,
            },
            "shared_features": {
                "market_liquidity": 100000,
                "capital_impact_pct": 5.0,
            },
            "recent_trades": [
                {
                    "timestamp": '2026-04-10T10:00:00Z<script>',
                    "side": "BUY",
                    "outcome": 'YES<img>',
                    "amount_usdc": 5000,
                    "price": 0.71,
                }
            ],
        }

        html = reporter.build_html_report([alert], "Test Run", {"flagged_alerts": 1})

        self.assertNotIn('<script>alert("x")</script>', html)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html)
        self.assertIn("High &lt;edge&gt;", html)
        self.assertIn("Mixer &lt;risk&gt;", html)
        self.assertIn("YES&lt;img&gt;", html)


if __name__ == "__main__":
    unittest.main()
