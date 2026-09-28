"""Guards on the paths that delete or hide stored roles.

Each test pins a guard whose absence has cost, or would cost, real rows:

- a partial `--company` run must never make global delete decisions. On
  2026-09-02 one did, and purge_orphaned_companies hard-deleted 51,810 rows;
- a source that errored, returned zero, or is degraded must not be delisted;
- rows retired by the age floor must stay out of the irreversible 'other'
  purge, as must internships and every real category.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobfeed import main
from jobfeed.db import JobDB


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


class OtherPurgeGuardTests(unittest.TestCase):
    def setUp(self):
        self.db = JobDB(":memory:")

    def _delisted(self, id_, area="other", job_type="job", by_age=0):
        self.db.mark_seen(id_, company="Co", title=id_, url=f"https://x/{id_}")
        self.db.set_tags(id_, area=area, job_type=job_type)
        self.db.conn.execute(
            "UPDATE seen_jobs SET delisted_at = ?, delisted_by_age = ? WHERE id = ?",
            (_days_ago(10), by_age, id_))
        self.db.conn.commit()

    def _alive(self, id_):
        return self.db.get_job(id_) is not None

    def test_plain_other_past_grace_is_purged(self):
        # Positive control: without it every guard test below passes vacuously.
        self._delisted("noise")
        self.assertEqual(self.db.purge_delisted_other(), 1)
        self.assertFalse(self._alive("noise"))

    def test_grace_period_is_three_days(self):
        # Pins the boundary, not just "old is purged, new is kept": a 4-day
        # row is purged under a 3- or a 4-day grace alike.
        self._delisted("early")
        self._delisted("late")
        for id_, hours in (("early", 70), ("late", 74)):
            self.db.conn.execute(
                "UPDATE seen_jobs SET delisted_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(), id_))
        self.db.conn.commit()
        self.assertEqual(self.db.purge_delisted_other(), 1)
        self.assertTrue(self._alive("early"))
        self.assertFalse(self._alive("late"))

    def test_age_delisted_rows_are_never_purged(self):
        # "We stopped looking" must not become "delete it".
        self._delisted("aged", by_age=1)
        self.assertEqual(self.db.purge_delisted_other(), 0)
        self.assertTrue(self._alive("aged"))

    def test_internships_are_never_purged(self):
        self._delisted("intern", job_type="internship")
        self.assertEqual(self.db.purge_delisted_other(), 0)
        self.assertTrue(self._alive("intern"))

    def test_real_categories_are_never_purged(self):
        self._delisted("trader", area="markets")
        self.assertEqual(self.db.purge_delisted_other(), 0)
        self.assertTrue(self._alive("trader"))


class DelistPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.db = JobDB(":memory:")
        for id_, company in [("a1", "A"), ("a2", "A"), ("b1", "B")]:
            self.db.mark_seen(id_, company=company, title=id_, url=f"https://x/{id_}")

    def test_find_delistable_only_judges_companies_passed_in(self):
        # B is absent from the dict (errored or skipped): none of its rows may
        # be reported missing, however empty its board looked.
        self.assertEqual(self.db.find_delistable({"A": {"a1"}}), ["a2"])

    def test_reappearing_role_is_relisted_and_loses_age_flag(self):
        self.db.mark_delisted_by_age(["a1"])
        self.db.touch_seen("a1")
        delisted_at, by_age = self.db.conn.execute(
            "SELECT delisted_at, delisted_by_age FROM seen_jobs WHERE id = 'a1'"
        ).fetchone()
        self.assertIsNone(delisted_at)
        self.assertEqual(by_age, 0)


class ScanDeleteGuardTests(unittest.TestCase):
    """Drive main() end to end against a temp database."""

    TARGETS = [
        {"name": n, "ats": "greenhouse", "slug": n.lower(), "category": "Banks",
         "verified": True}
        for n in ("Healthy", "Errored", "Empty", "Collapsed")
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "jobs.db")
        db = JobDB(self.db_path)
        rows = [
            ("h-live", "Healthy"), ("h-gone", "Healthy"),
            ("e-1", "Errored"), ("z-1", "Empty"), ("c-1", "Collapsed"),
            ("o-1", "Removed Source"),  # in the DB, absent from targets.json
        ]
        for id_, company in rows:
            db.mark_seen(id_, company=company, title=f"Analyst {id_}",
                         url=f"https://x/{id_}")
        db.conn.close()

    def _results(self, names):
        board = {
            "Healthy": ([{"id": "h-live", "title": "Analyst h-live",
                          "url": "https://x/h-live", "location": "London"}], None),
            "Errored": ([], "HTTPError: 500"),
            "Empty": ([], None),
            "Collapsed": ([{"id": "c-new", "title": "Analyst c-new",
                            "url": "https://x/c-new", "location": "London"}], None),
        }
        return [(t, *board[t["name"]]) for t in self.TARGETS if t["name"] in names]

    def _run(self, argv, names):
        out = io.StringIO()
        with patch.object(main, "DB_FILE", self.db_path), \
             patch.object(main, "load_targets", return_value=list(self.TARGETS)), \
             patch.object(main, "scrape_targets", return_value=self._results(names)), \
             patch.object(main, "scrape_heavy_targets", return_value=[]), \
             patch.object(main, "_enrich_new_jobs"), \
             patch.object(main, "_report_skipped_delists"), \
             patch.object(main, "_write_health_state", return_value={"Collapsed"}), \
             patch.object(sys, "argv", ["main.py", "--no-tag", *argv]), \
             redirect_stdout(out):
            main.main()
        return out.getvalue(), JobDB(self.db_path)

    def test_full_run_delists_only_what_it_can_vouch_for(self):
        _, db = self._run([], {t["name"] for t in self.TARGETS})
        self.addCleanup(db.conn.close)
        self.assertIsNotNone(db.get_job("h-gone")["delisted_at"])
        self.assertIsNone(db.get_job("h-live")["delisted_at"])
        for kept in ("e-1", "z-1", "c-1"):  # errored, clean zero, degraded
            self.assertIsNone(db.get_job(kept)["delisted_at"], kept)
        self.assertIsNone(db.get_job("o-1"))  # genuinely removed source

    def test_partial_run_never_purges_orphans(self):
        # The 2026-09-02 incident: every source outside --company was treated
        # as removed from targets.json and hard-deleted.
        out, db = self._run(["--company", "Healthy"], {"Healthy"})
        self.addCleanup(db.conn.close)
        self.assertIn("skipping orphan purge", out)
        for id_ in ("o-1", "e-1", "z-1", "c-1"):
            self.assertIsNotNone(db.get_job(id_), id_)
            self.assertIsNone(db.get_job(id_)["delisted_at"], id_)


if __name__ == "__main__":
    unittest.main()
