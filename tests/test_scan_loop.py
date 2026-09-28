"""End-to-end behaviour of one scan through main(), against a temp database.

Written from a mutation run over main(): each test pins behaviour a mutant
changed without any test noticing. The first matters most, because every
investigation doctrine in this repo says to probe a source with --dry-run.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobfeed import main
from jobfeed.db import JobDB

TARGET = {"name": "Healthy", "ats": "greenhouse", "slug": "healthy",
          "category": "Banks", "verified": True}
FIVE_YEARS = "We require a minimum of 5 years of experience in credit trading."


def _role(id_, **extra):
    return {"id": id_, "title": f"Credit Trading Analyst {id_}",
            "url": f"https://x/{id_}", "location": "London", **extra}


class ScanLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "jobs.db")
        db = JobDB(self.db_path)
        db.mark_seen("old", company="Healthy", title="Credit Trading Analyst old",
                     url="https://x/old")
        db.conn.close()

    def _run(self, board, argv=("--no-tag",), tagger=None):
        health = patch.object(main, "_write_health_state", return_value=set())
        patches = [
            health,
            patch.object(main, "DB_FILE", self.db_path),
            patch.object(main, "load_targets", return_value=[TARGET]),
            patch.object(main, "scrape_targets", return_value=[(TARGET, board, None)]),
            patch.object(main, "scrape_heavy_targets", return_value=[]),
            patch.object(main, "_enrich_new_jobs"),
            patch.object(main, "_report_skipped_delists"),
            patch.object(main.tag, "LAST_RUN_HEALTH", {}),
            patch.object(main.tag, "tag_provenance", return_value={
                "provider": "test", "model": "m", "rubric_version": "r"}),
            patch.object(main, "tag_jobs", side_effect=tagger or (lambda jobs: None)),
            patch.object(sys, "argv", ["main.py", *argv]),
        ]
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            mocks = [stack.enter_context(p) for p in patches]
            main.main()
        self.health_calls = mocks[0].call_count
        db = JobDB(self.db_path)
        self.addCleanup(db.conn.close)
        return db

    def _snapshot(self):
        db = JobDB(self.db_path)
        try:
            return (db.conn.execute(
                "SELECT id, last_seen, delisted_at FROM seen_jobs ORDER BY id").fetchall(),
                db.conn.execute("SELECT COUNT(*) FROM runs").fetchone())
        finally:
            db.conn.close()

    def test_dry_run_writes_nothing(self):
        before = self._snapshot()
        self._run([_role("new")], argv=("--no-tag", "--dry-run"))
        self.assertEqual(self._snapshot(), before)  # no insert, touch, delist or run log
        self.assertEqual(self.health_calls, 0)      # nor a health-state rewrite

    def test_new_role_is_stored_with_every_field(self):
        db = self._run([_role("new", posted="2026-09-20", description=FIVE_YEARS)])
        job = db.get_job("new")
        self.assertEqual(
            (job["company"], job["title"], job["url"], job["location"],
             job["posted"], job["category"]),
            ("Healthy", "Credit Trading Analyst new", "https://x/new", "London",
             "2026-09-20", "Banks"))
        self.assertEqual(job["description"], FIVE_YEARS)

    def test_role_still_on_board_is_refreshed_not_duplicated(self):
        db = JobDB(self.db_path)
        stale = "2026-01-01T00:00:00+00:00"
        db.conn.execute("UPDATE seen_jobs SET last_seen = ? WHERE id = 'old'", (stale,))
        db.conn.commit()
        db.conn.close()
        db = self._run([_role("old", description=FIVE_YEARS)])
        job = db.get_job("old")
        self.assertGreater(job["last_seen"], stale)
        self.assertEqual(job["description"], FIVE_YEARS)
        self.assertEqual(db.total_seen(), 1)

    def test_tagger_output_is_persisted(self):
        def tagger(jobs):
            for j in jobs:
                j.update(area="markets", desk="trading", seniority="graduate",
                         job_type="job", loc_city="London", loc_country="United Kingdom",
                         loc_region="Europe", work_mode="hybrid", min_yoe=2)
        db = self._run([_role("new")], argv=(), tagger=tagger)
        job = db.get_job("new")
        self.assertEqual(
            (job["area"], job["desk"], job["seniority"], job["loc_city"],
             job["loc_region"], job["work_mode"], job["min_yoe"]),
            ("markets", "trading", "graduate", "London", "Europe", "hybrid", 2))

    def test_yoe_fallback_fills_only_untagged_rows_and_all_of_them(self):
        def tagger(jobs):
            for j in jobs:
                j.update(area="markets", min_yoe=2 if j["id"] == "llm" else None)
        board = [_role("a", description=FIVE_YEARS),
                 _role("llm", description=FIVE_YEARS),
                 _role("b", description=FIVE_YEARS)]
        db = self._run(board, argv=(), tagger=tagger)
        # The model read the description for "llm": its value wins over the regex.
        self.assertEqual(db.get_job("llm")["min_yoe"], 2)
        self.assertEqual((db.get_job("a")["min_yoe"], db.get_job("b")["min_yoe"]), (5, 5))

    def test_full_scan_purges_confirmed_other_noise(self):
        db = JobDB(self.db_path)
        db.set_tags("old", area="other")
        db.conn.execute("UPDATE seen_jobs SET delisted_at = ? WHERE id = 'old'",
                        ((datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),))
        db.conn.commit()
        db.conn.close()
        db = self._run([_role("new")])
        self.assertIsNone(db.get_job("old"))


if __name__ == "__main__":
    unittest.main()
