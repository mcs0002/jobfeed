import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import description_health as dh


class DescriptionHealthTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        root = Path(self.dir)
        # Minimal targets.json mapping companies to ATSes.
        (root / "targets.json").write_text(json.dumps([
            {"name": "GoodCo", "ats": "greenhouse"},
            {"name": "StubCo", "ats": "talnet"},
        ]))
        self.root = root
        self.db = str(root / "jobs.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE seen_jobs (company TEXT, description TEXT, "
                     "delisted_at TEXT)")
        rows = (
            [("GoodCo", "x" * 4000, None)] * 10          # healthy source
            + [("StubCo", "x" * 120, None)] * 10          # all stubs -> degraded
            + [("StubCo", "x" * 5000, "2026-01-01")]      # delisted -> ignored
        )
        conn.executemany("INSERT INTO seen_jobs VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()

    def test_flags_stub_source_only(self):
        health = {h["ats"]: h for h in dh.per_ats_health(self.db, self.root)}
        self.assertFalse(health["greenhouse"]["degraded"])
        self.assertTrue(health["talnet"]["degraded"])
        self.assertEqual(health["talnet"]["stub_pct"], 100.0)
        # delisted row excluded -> greenhouse and talnet each have 10 live rows
        self.assertEqual(health["greenhouse"]["rows"], 10)
        self.assertEqual(health["talnet"]["rows"], 10)

    def test_degraded_sources_helper(self):
        degraded = {h["ats"] for h in dh.degraded_sources(self.db, self.root)}
        self.assertEqual(degraded, {"talnet"})

    def test_small_source_not_flagged(self):
        # A source under MIN_ROWS stubs is not judged (too little signal).
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO seen_jobs VALUES (?,?,?)",
                         [("TinyCo", "y" * 50, None)] * 3)
        conn.commit(); conn.close()
        (self.root / "targets.json").write_text(json.dumps([
            {"name": "GoodCo", "ats": "greenhouse"},
            {"name": "StubCo", "ats": "talnet"},
            {"name": "TinyCo", "ats": "eploy"},
        ]))
        health = {h["ats"]: h for h in dh.per_ats_health(self.db, self.root)}
        self.assertFalse(health["eploy"]["degraded"])


if __name__ == "__main__":
    unittest.main()
