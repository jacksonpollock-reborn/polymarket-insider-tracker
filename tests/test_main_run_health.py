import unittest
import importlib
import sys
import types
from unittest.mock import patch

fake_requests = types.ModuleType("requests")


class _FakeSession:
    def __init__(self):
        self.headers = {}

    def get(self, *args, **kwargs):
        raise RuntimeError("network disabled in test")

    def post(self, *args, **kwargs):
        raise RuntimeError("network disabled in test")


fake_requests.Session = _FakeSession
sys.modules.setdefault("requests", fake_requests)

main = importlib.import_module("main")


class MainRunHealthTests(unittest.TestCase):
    def test_geopolitical_conflict_market_is_not_misclassified_as_sports(self):
        category = main._detect_market_category({
            "question": "Iran x Israel/US conflict ends by April 15?",
            "tags": None,
        })
        self.assertEqual(category, "Politics")

    def test_market_bootstrap_dns_failure_exits_non_zero_and_marks_run_unhealthy(self):
        captured = {}

        with (
            patch("main.reset_request_health"),
            patch("main.BOOTSTRAP_RETRY_COUNT", 0),
            patch("main.fetch_markets_by_tags", return_value=[]),
            patch("main.fetch_active_markets", return_value=[]),
            patch("main.get_request_health", return_value={
                "successful_calls": 0,
                "attempt_failures": 6,
                "failed_calls": 2,
                "last_error": "[Errno 8] nodename nor servname provided, or not known",
            }),
            patch("main.load_review_log", return_value=[]),
            patch("main.summarize_review_log", return_value={}),
            patch("main._write_output", side_effect=lambda payload: captured.setdefault("payload", payload)),
            patch("main.write_tuning_artifacts"),
            patch("main.send_email", return_value=False),
            patch("main.send_telegram_alerts", return_value=False),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main.run()

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(captured["payload"]["run_health"]["status"], "unhealthy")
        self.assertIn("market_bootstrap_failed", captured["payload"]["run_health"]["reason"])

    def test_market_bootstrap_retries_once_and_recovers(self):
        captured = {}
        market = {
            "conditionId": "m1",
            "question": "Will X happen?",
            "endDateIso": "2026-04-20T12:00:00Z",
            "volume24hr": 1000,
            "volume": 7000,
            "liquidity": 100000,
            "tags": ["politics"],
        }

        with (
            patch("main.reset_request_health"),
            patch("main.fetch_markets_by_tags", side_effect=[[], [market]]),
            patch("main.fetch_active_markets", side_effect=[[], []]),
            patch("main.get_request_health", side_effect=[
                {
                    "successful_calls": 0,
                    "attempt_failures": 6,
                    "failed_calls": 2,
                    "last_error": "[Errno 8] nodename nor servname provided, or not known",
                },
                {
                    "successful_calls": 2,
                    "attempt_failures": 6,
                    "failed_calls": 2,
                    "last_error": "[Errno 8] nodename nor servname provided, or not known",
                },
                {
                    "successful_calls": 2,
                    "attempt_failures": 6,
                    "failed_calls": 2,
                    "last_error": "[Errno 8] nodename nor servname provided, or not known",
                },
                {
                    "successful_calls": 2,
                    "attempt_failures": 6,
                    "failed_calls": 2,
                    "last_error": "[Errno 8] nodename nor servname provided, or not known",
                },
            ]),
            patch("main.time.sleep"),
            patch("main.batch_scan_arb", return_value=[]),
            patch("main.flag_suspicious_markets", return_value=[]),
            patch("main.load_review_log", return_value=[]),
            patch("main.summarize_review_log", return_value={}),
            patch("main._write_output", side_effect=lambda payload: captured.setdefault("payload", payload)),
            patch("main.write_tuning_artifacts"),
            patch("main.write_html_report"),
            patch("main.send_email", return_value=True),
            patch("main.send_telegram_alerts", return_value=False),
        ):
            main.run()

        self.assertEqual(captured["payload"]["run_health"]["status"], "healthy")
        self.assertEqual(captured["payload"]["stats"]["markets_scanned"], 1)

    def test_empty_report_email_failure_exits_non_zero(self):
        captured = {}
        market = {
            "conditionId": "m1",
            "question": "Will X happen?",
            "endDateIso": "2026-04-20T12:00:00Z",
            "volume24hr": 1000,
            "volume": 7000,
            "liquidity": 100000,
            "tags": ["politics"],
        }

        with (
            patch("main.reset_request_health"),
            patch("main.fetch_markets_by_tags", return_value=[market]),
            patch("main.fetch_active_markets", return_value=[]),
            patch("main.get_request_health", return_value={
                "successful_calls": 2,
                "attempt_failures": 0,
                "failed_calls": 0,
                "last_error": None,
            }),
            patch("main.batch_scan_arb", return_value=[]),
            patch("main.flag_suspicious_markets", return_value=[]),
            patch("main.load_review_log", return_value=[]),
            patch("main.summarize_review_log", return_value={}),
            patch("main._write_output", side_effect=lambda payload: captured.setdefault("payload", payload)),
            patch("main.write_tuning_artifacts"),
            patch("main.send_email", return_value=False),
            patch("main.send_telegram_alerts", return_value=False),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main.run()

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(captured["payload"]["run_health"]["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
