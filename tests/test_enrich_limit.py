import unittest
from unittest.mock import Mock, patch

import main


class EnrichLimitTests(unittest.TestCase):
    def test_limit_bounds_optional_description_fetches(self):
        jobs = [
            {"id": "one", "url": "https://example.com/one", "description": ""},
            {"id": "two", "url": "https://example.com/two", "description": ""},
        ]
        db = Mock()

        with patch.object(main, "enrich_one", return_value="description") as fetch:
            main._enrich_new_jobs(jobs, db, dry_run=True, max_jobs=1)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(jobs[0]["description"], "description")
        self.assertEqual(jobs[1]["description"], "")

    def test_zero_skips_optional_description_fetches(self):
        jobs = [
            {"id": "one", "url": "https://example.com/one", "description": ""},
        ]
        db = Mock()

        with patch.object(main, "enrich_one") as fetch:
            main._enrich_new_jobs(jobs, db, dry_run=True, max_jobs=0)

        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
