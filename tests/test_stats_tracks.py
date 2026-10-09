"""Application stats track switch: both / full-time / internships (2026-09-23)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault(
    "JOBS_DB", tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

from jobfeed.db import JobDB  # noqa: E402


def _row(db, jid, title, job_type, status):
    db.mark_seen(jid, company="TestCo", title=title, url=f"https://x.test/{jid}")
    db.conn.execute(
        "UPDATE seen_jobs SET job_type=?, status=?, area='markets', applied_at=? "
        "WHERE id=?", (job_type, status, "2026-09-10T00:00:00+00:00", jid))
    db.conn.commit()


def seed(db):
    _row(db, "g1", "Graduate Trader Programme", "graduate-programme", "applied")
    _row(db, "f1", "Markets Analyst", "job", "rejected")
    _row(db, "i1", "Summer Analyst Internship", "internship", "oa")
    # Moved back to 'new' by hand: applied_at stays stamped, but it is not an
    # application any more.
    _row(db, "rev", "Reverted Internship", "internship", "new")


class TrackSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient
        import web.app as webapp
        cls.webapp, cls.TestClient = webapp, TestClient

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        db = JobDB(self.path)
        seed(db)
        self.db = db
        w = self.webapp
        self._saved = {k: getattr(w, k) for k in ("DB_FILE", "WEB_USER", "WEB_PASSWORD")}
        w.DB_FILE, w.WEB_USER, w.WEB_PASSWORD = self.path, "admin", "owner-pw-test"
        self.c = self.TestClient(w.app, base_url="https://testserver")
        self.c.post("/login", data={"username": "admin", "password": "owner-pw-test"})

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.webapp, k, v)
        self.db.conn.close()
        os.path.exists(self.path) and os.unlink(self.path)

    def _page(self, track=""):
        return self.c.get("/stats/applications" + (f"?track={track}" if track else "")).text

    def test_tabs_count_submitted_applications_per_track(self):
        page = self._page()
        self.assertIn('>Both <span class="n">3</span>', page)
        self.assertIn('>Full-time <span class="n">2</span>', page)
        self.assertIn('>Internships <span class="n">1</span>', page)

    def test_switch_filters_the_pipeline(self):
        both = self._page()
        self.assertIn("Graduate Trader Programme", both)
        self.assertIn("Summer Analyst Internship", both)
        full = self._page("fulltime")
        self.assertIn("Graduate Trader Programme", full)      # graduate = full-time
        self.assertNotIn("Summer Analyst Internship", full)
        intern = self._page("intern")
        self.assertIn("Summer Analyst Internship", intern)
        self.assertNotIn("Graduate Trader Programme", intern)
        self.assertNotIn("Markets Analyst", intern)

    def test_unknown_track_means_both(self):
        self.assertIn("Graduate Trader Programme", self._page("bogus"))
        self.assertIn("Summer Analyst Internship", self._page("bogus"))

    def test_funnel_counts_current_status_not_a_stale_stamp(self):
        rows = self.db.application_funnel("area", track="intern")
        self.assertEqual([(r["label"], r["applications"]) for r in rows], [("markets", 1)])
        full = self.db.application_funnel("area", track="fulltime")
        self.assertEqual(full[0]["applications"], 2)

    def test_campus_programmes_classified_by_name(self):
        from web.app import programme_track
        self.assertEqual(programme_track("Graduate Algorithmic Trader 2027"), "fulltime")
        self.assertEqual(programme_track("Students & New Grads"), "fulltime")
        self.assertEqual(programme_track("Summer Analyst Programme 2027"), "intern")
        self.assertEqual(programme_track("Off-Cycle Internship"), "intern")
        self.assertEqual(programme_track("Full-Time Analyst Programme"), "fulltime")


if __name__ == "__main__":
    unittest.main()
