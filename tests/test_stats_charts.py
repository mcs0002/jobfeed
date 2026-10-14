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

from jobfeed.db import JobDB, week_index, week_monday  # noqa: E402
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
        # Fixed calendar weeks: every bar starts on a Monday, the last one this
        # week's, so a past bar never changes as days pass.
        for w in weeks:
            self.assertEqual(datetime.fromisoformat(w["start"]).weekday(), 0)
        monday = datetime.fromisoformat(weeks[-1]["start"]).date()
        expected = [0] * 12
        for ap in (_day(1), _day(10), _day(2)):  # a, c, e
            d = datetime.fromisoformat(ap).date()
            expected[11 - ((monday - d).days + 6) // 7] += 1
        self.assertEqual([w["n"] for w in weeks][-3:], expected[-3:])

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

    def test_weekly_figures_use_calendar_weeks_including_today(self):
        # Monday-to-Sunday weeks (his rule, 2026-10-13); d was stored today and
        # a bare-date upper bound used to drop it.
        today = datetime.now(timezone.utc).date()
        expected = [0] * 8
        for days_ago in (40, 40, 20, 0, 3):  # a, b, c, d, e by first_seen
            w = week_index(today - timedelta(days=days_ago), today)
            if w < 8:
                expected[w] += 1
        self.assertGreaterEqual(expected[0], 1)  # d, stored today
        weeks = self.db.weekly_intake_counts(8)
        self.assertEqual([w["finance"] + w["other_n"] for w in weeks], expected)
        self.assertEqual([w["start"] for w in weeks],
                         [week_monday(today, i).isoformat() for i in range(8)])
        self.assertEqual(datetime.fromisoformat(weeks[0]["start"]).weekday(), 0)
        self.assertEqual([w["total"] for w in self.db.weekly_summary(weeks=8)], expected)

    def test_week_index_is_calendar_based(self):
        monday = datetime(2026, 10, 12).date()
        self.assertEqual(week_index(monday, monday + timedelta(days=6)), 0)   # Sun, same week
        self.assertEqual(week_index(monday - timedelta(days=1), monday), 1)   # Sun before
        self.assertEqual(week_monday(monday + timedelta(days=3), 2), monday - timedelta(days=14))

    def test_noise_share_chart_and_other_inflow(self):
        weeks = self.db.weekly_intake_counts(12)
        self.assertTrue(all("wealth_n" in w for w in weeks))
        chart = webapp._noise_week_chart(weeks)
        self.assertEqual(chart["format"], "pct")
        self.assertEqual(len(chart["points"]), 12)
        self.assertTrue(all(0 <= p["v"] <= 100 for p in chart["points"]))
        rows = self.db.other_inflow(7, 10)
        self.assertTrue(all(r["other"] <= r["n"] for r in rows))

    def test_other_inflow_compares_two_windows(self):
        rows = [("o1", "N", _day(1), "other"), ("o2", "N", _day(2), "markets"),
                ("o3", "N", _day(10), "other"), ("o4", "N", _day(11), "other"),
                ("o5", "N", _day(20), "other")]
        for jid, co, fs, area in rows:
            self.db.conn.execute(
                "INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen, area)"
                " VALUES (?,?,?,?,?,?,?)", (jid, co, jid, f"https://x.test/{jid}", fs, fs, area))
        r = next(r for r in self.db.other_inflow(7, 10) if r["company"] == "N")
        self.assertEqual((r["other"], r["prev"], r["n"]), (1, 2, 2))

    def test_intake_chart_stacks_front_office_then_rest(self):
        days = [{"date": "2026-10-13", "finance": 6, "other_n": 9, "wealth_n": 1,
                 "area": {"markets": 3, "risk": 1, "middle-office": 1, "wealth": 1}}]
        chart = webapp._intake_day_chart(days)
        p = chart["points"][0]
        self.assertEqual(p["v"], 6)
        self.assertEqual(sum(s["v"] for s in p["segs"]), 6)
        self.assertEqual([(s["slug"], s["v"]) for s in p["segs"]], [("fo-markets", 3), ("fo-rest", 3)])
        self.assertIn("Markets 3 (50%)", p["tip"])
        self.assertIn("Rest of finance 3 (50%)", p["tip"])
        self.assertEqual(chart["legend"][-1]["slug"], "fo-rest")

    def test_intake_counts_carry_finance_by_area(self):
        for w in self.db.weekly_intake_counts(8) + self.db.daily_intake_counts(30):
            self.assertEqual(sum(w["area"].values()), w["finance"])

    def test_roles_chart_stacks_finance_under_other(self):
        days = [{"date": "2026-10-13", "finance": 3, "other_n": 5, "wealth_n": 0}]
        p = webapp._roles_split_day_chart(days)["points"][0]
        self.assertEqual((p["v"], p["lo"]), (8, 3))
        w = webapp._roles_week_chart([{"start": "2026-10-12", "finance": 2, "other_n": 1}])
        self.assertEqual((w["points"][0]["v"], w["points"][0]["lo"], w["total"]), (3, 2, 3))

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
    def test_tagger_health_flags_only_runs_that_need_a_look(self):
        def run(ts, tagged=10, failed=0, flags=()):
            return {"ts": ts, "provider": "api", "model": "m", "rubric": "r1", "total": 10,
                    "tagged": tagged, "coverage": 10.0 * tagged, "cache_rate": 0.0,
                    "cost": 0.01, "priced": True, "latency_s": 1.0,
                    "failed_batches": failed, "flags": list(flags)}
        h = webapp._tagger_health([run("2026-10-14T07:00:00+00:00"),
                                    run("2026-10-13T07:00:00+00:00", failed=1),
                                    run("2026-10-12T07:00:00+00:00", tagged=8)])
        self.assertEqual([c["state"] for c in h["cells"]], ["bad", "warn", "ok"])
        self.assertEqual((h["runs"], h["clean"], h["tagged"], h["total"]), (3, 1, 28, 30))
        self.assertEqual([p["state"] for p in h["problems"]], ["warn", "bad"])
        self.assertEqual(webapp._tagger_health([])["runs"], 0)

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
