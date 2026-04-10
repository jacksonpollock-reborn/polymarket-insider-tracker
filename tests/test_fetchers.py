import unittest
from unittest.mock import patch

from src import fetchers


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


if __name__ == "__main__":
    unittest.main()
