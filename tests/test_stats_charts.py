"""The data behind the stats-page charts: weekly applications, daily new
roles, tagging cost per day and the conversion steps."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_GUEST_PASSWORD", "guest-pw-test")
os.environ.setdefault("WEB_GUEST_USER", "guest")
os.environ.setdefault("JOBS_DB", tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

from jobfeed.db import JobDB  # noqa: E402
import web.app as webapp  # noqa: E402


def _day(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class ChartQueries(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.db = JobDB(self.path)
        rows = [
            # id, company, status, first_seen, applied_at, job_type
            ("a", "A", "applied", _day(40), _day(1), "job"),
            ("b", "A", "interview", _day(40), None, "job"),          # no stamp: first_seen
            ("c", "B", "rejected", _day(20), _day(10), "internship"),
            ("d", "B", "new", _day(0), None, "job"),                 # not submitted
            ("e", "Hidden", "applied", _day(3), _day(2), "job"),
        ]
        for jid, co, st, fs, ap, jt in rows:
            self.db.conn.execute(
                "INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen,"
                " status, applied_at, job_type) VALUES (?,?,?,?,?,?,?,?,?)",
                (jid, co, jid, f"https://x.test/{jid}", fs, fs, st, ap, jt))
        self.db.conn.commit()

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.path)

    def test_applications_per_week_counts_submitted_rows_only(self):
        weeks = self.db.applications_per_week(12)
        self.assertEqual(len(weeks), 12)
        self.assertLess(weeks[0]["start"], weeks[-1]["start"])
        # a, c, e inside 12 weeks; b falls back to first_seen (40d ago); d is 'new'.
        self.assertEqual(sum(w["n"] for w in weeks), 4)
        self.assertEqual(weeks[-1]["n"], 2)  # a and e, both in the last 7 days

    def test_applications_per_week_honours_track_and_hidden(self):
        self.assertEqual(sum(w["n"] for w in self.db.applications_per_week(
            12, track="intern")), 1)
        self.assertEqual(sum(w["n"] for w in self.db.applications_per_week(
            12, exclude_companies=["Hidden"])), 3)

    def test_new_roles_per_day_is_zero_filled(self):
        days = self.db.new_roles_per_day(30)
        self.assertEqual(len(days), 30)
        self.assertEqual(days[-1]["n"], 1)  # d
        self.assertEqual(sum(d["n"] for d in days), 3)  # c, d, e; a and b are 40d old

    def test_weekly_windows_include_today(self):
        # d was stored today; a bare-date upper bound used to drop it.
        self.assertEqual(self.db.weekly_summary(weeks=1)[0]["total"], 2)  # d, e
        weeks = self.db.weekly_intake_counts(8)
        self.assertEqual(len(weeks), 8)
        self.assertEqual(weeks[0]["finance"] + weeks[0]["other_n"], 2)
        # c (20d ago) lands in week 2, the same window weekly_summary uses.
        self.assertEqual(weeks[2]["finance"] + weeks[2]["other_n"], 1)
        self.assertEqual(self.db.weekly_summary(weeks=3)[2]["total"], 1)

    def test_deadline_board_splits_dated_and_undated(self):
        soon = (datetime.now(timezone.utc) + timedelta(days=5)).date().isoformat()
        far = (datetime.now(timezone.utc) + timedelta(days=90)).date().isoformat()
        self.db.conn.executemany(
            "INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen,"
            " status, favorite, deadline) VALUES (?,?,?,?,?,?,?,?,?)",
            [("s1", "C", "Soon", "https://x.test/s1", _day(1), _day(0), "new", 1, soon),
             ("s2", "C", "Later", "https://x.test/s2", _day(1), _day(0), "new", 1, far),
             ("s3", "C", "Undated", "https://x.test/s3", _day(1), _day(0), "queued", 0, None),
             ("s4", "C", "Done", "https://x.test/s4", _day(1), _day(0), "applied", 1, soon)])
        self.db.conn.commit()
        board = webapp._deadline_board(self.db, [], {})
        self.assertEqual([r["id"] for r in board["dated"]], ["s1"])
        self.assertEqual(board["dated"][0]["days"], 5)
        self.assertEqual(board["later"], 1)
        self.assertEqual([r["id"] for r in board["undated"]], ["s3"])  # applied s4 excluded


class ChartPayloads(unittest.TestCase):
    def test_conversion_counts_at_least_that_stage(self):
        steps = webapp._conversion_steps(
            {"applied": 5, "oa": 1, "interview": 2, "offer": 1, "rejected": 1})
        self.assertEqual([s["n"] for s in steps], [10, 5, 4, 3, 1])
        self.assertEqual(steps[1]["pct"], 50)

    def test_conversion_with_nothing_submitted(self):
        self.assertTrue(all(s["pct"] == 0 for s in webapp._conversion_steps({})))

    def test_cost_chart_marks_unpriced_days(self):
        today = datetime.now(timezone.utc).date().isoformat()
        chart = webapp._cost_day_chart(
            [{"key": today, "cost": 0.12, "priced": False, "tagged": 40}], days=3)
        self.assertEqual(len(chart["points"]), 3)
        self.assertEqual(chart["unpriced_days"], 1)
        self.assertIn("unpriced", chart["points"][-1]["tip"])
        self.assertTrue(chart["has_data"])


if __name__ == "__main__":
    unittest.main()
