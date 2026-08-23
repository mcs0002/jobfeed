import unittest
import json
import os
import sys
import tempfile
from unittest.mock import patch

import main
from db import JobDB
from main import select_companies


class CompanySelectionTests(unittest.TestCase):
    def setUp(self):
        self.targets = [
            {"name": "Optiver"},
            {"name": "BlackRock"},
            {"name": "Blackstone"},
        ]

    def test_no_query_keeps_all_targets(self):
        self.assertIs(select_companies(self.targets, []), self.targets)

    def test_matching_is_case_insensitive_and_supports_substrings(self):
        self.assertEqual(
            [target["name"] for target in select_companies(self.targets, ["black"])],
            ["BlackRock", "Blackstone"],
        )

    def test_multiple_queries_are_combined(self):
        self.assertEqual(
            [target["name"] for target in
             select_companies(self.targets, ["optiver", "rock"])],
            ["Optiver", "BlackRock"],
        )

    def test_unknown_query_returns_empty_list(self):
        self.assertEqual(select_companies(self.targets, ["missing"]), [])


class SelectiveScanSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "jobs.db")
        self.targets = [
            {"name": "Selected", "ats": "greenhouse", "slug": "selected",
             "category": "Banks", "verified": True},
            {"name": "Unselected", "ats": "greenhouse", "slug": "unselected",
             "category": "Banks", "verified": True},
        ]
        db = JobDB(self.db_path)
        db.mark_seen("keep-me", company="Unselected", title="Role",
                     url="https://example.test/keep")
        db.conn.close()

    def test_company_scan_does_not_purge_unselected_company(self):
        result = [(self.targets[0], [], None)]
        with patch.object(main, "DB_FILE", self.db_path), \
             patch.object(main, "load_targets", return_value=self.targets), \
             patch.object(main, "scrape_targets", return_value=result), \
             patch.object(main, "scrape_heavy_targets", return_value=[]), \
             patch.object(main, "_enrich_new_jobs"), \
             patch.object(main, "_write_health_state", return_value=set()), \
             patch.object(sys, "argv", ["main.py", "--company", "Selected", "--no-tag"]):
            main.main()
        db = JobDB(self.db_path)
        self.assertIsNotNone(db.get_job("keep-me"))
        db.conn.close()


if __name__ == "__main__":
    unittest.main()
