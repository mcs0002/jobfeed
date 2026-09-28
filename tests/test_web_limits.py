"""Web surface for company application limits.

Written in unittest style deliberately: several older web tests in this
directory are pytest-style bare functions, and pytest is not installed in the
venv, so `unittest discover` silently collects none of them. A guard that does
not run is worse than no guard.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_GUEST_PASSWORD", "guest-pw-test")
os.environ.setdefault("WEB_GUEST_USER", "guest")
os.environ.setdefault(
    "JOBS_DB", tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

from starlette.testclient import TestClient  # noqa: E402

import web.app as webapp  # noqa: E402
from jobfeed.db import JobDB      # noqa: E402


def seed(path):
    """Nine starred roles at a firm capped at one — the shape this feature
    exists for (UBS Global Markets, starred in nine locations)."""
    db = JobDB(path)
    for i in range(9):
        db.mark_seen(f"cap{i}", company="CappedCo", title=f"Grad Programme {i}",
                     url=f"https://x.test/cap{i}")
        db.set_favorite(f"cap{i}", True)
    db.mark_seen("free1", company="FreeCo", title="Analyst",
                 url="https://x.test/free1")
    # Same firm under a second scraper label — the cap is recorded once, under
    # the name the research pass ran with.
    db.mark_seen("alias1", company="CappedCo (Campus)", title="Summer Analyst",
                 url="https://x.test/alias1")
    db.set_company_limit(
        "CappedCo", max_per_cycle=1, cycle="recruitment year",
        locations_count_separately=1, confidence="stated", strength="hard",
        quote="You can only apply to one programme in one location each year.",
        source_url="https://x.test/faq", updated_by="research")
    db.set_company_limit("FreeCo", max_per_cycle=None, confidence="unknown",
                         quote="checked 3 page(s), no cap language found")
    db.conn.commit()
    return db


class LimitsUITests(unittest.TestCase):
    def setUp(self):
        # Own throwaway DB per test, pointed at through webapp.DB_FILE (get_db
        # reads the global per request) and restored afterwards — the pattern
        # the other web tests use, so import order between modules cannot make
        # these depend on someone else's fixture. Re-seeded per test because
        # several of them mutate the limit record and unittest orders methods
        # alphabetically.
        # Patch the credential constants rather than the environment: two test
        # modules that sort before this one (test_source_links,
        # test_stats_scan_volume) import web.app with no WEB_PASSWORD set, so
        # by the time this module's env defaults are applied the module-level
        # constants are already frozen empty and every login silently fails.
        self._saved_auth = (webapp.WEB_PASSWORD, webapp.WEB_USER,
                            webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER)
        webapp.WEB_PASSWORD, webapp.WEB_USER = "owner-pw-test", "admin"
        webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER = "guest-pw-test", "guest"
        self._saved_db_file = webapp.DB_FILE
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        webapp.DB_FILE = self.path
        self.db = seed(self.path)

    def tearDown(self):
        (webapp.WEB_PASSWORD, webapp.WEB_USER,
         webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER) = self._saved_auth
        webapp.DB_FILE = self._saved_db_file
        os.unlink(self.path)

    def owner(self):
        c = TestClient(webapp.app, base_url="https://testserver")
        c.post("/login", data={"username": webapp.WEB_USER,
                               "password": webapp.WEB_PASSWORD},
               follow_redirects=False)
        return c

    def test_limits_page_lists_the_cap_and_its_quote(self):
        r = self.owner().get("/limits")
        self.assertEqual(r.status_code, 200)
        self.assertIn("CappedCo", r.text)
        self.assertIn("only apply to one programme", r.text)
        self.assertIn("locations count separately", r.text)

    def test_over_starred_firm_sorts_first(self):
        r = self.owner().get("/limits")
        self.assertLess(r.text.index("CappedCo"), r.text.index("FreeCo"),
                        "a firm with 9 stars against a cap of 1 must lead")

    def test_detail_pane_shows_the_cap_with_its_source(self):
        r = self.owner().get("/job/cap0?pane=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Application limit", r.text)
        self.assertIn("https://x.test/faq", r.text)
        self.assertIn("Each location counts separately", r.text)

    def test_detail_pane_says_so_when_nothing_was_found(self):
        r = self.owner().get("/job/free1?pane=1")
        self.assertIn("No cap found", r.text)
        self.assertIn("no cap language found", r.text)

    def test_browse_list_carries_a_usage_badge(self):
        r = self.owner().get("/")
        self.assertIn("0/1 appl", r.text)

    def test_cap_resolves_across_a_firms_scraper_labels(self):
        """"UBS" and "UBS (Graduate Careers)" are one firm with one policy.
        The cap is stored once and aliased at display time, so a role arriving
        under the other label must not read as uncapped."""
        r = self.owner().get("/job/alias1?pane=1")
        self.assertIn("Application limit", r.text)
        self.assertIn("only apply to one programme", r.text)
        self.assertNotIn("Not researched", r.text)

    def test_owner_can_record_a_limit_seen_on_the_form(self):
        c = self.owner()
        r = c.post("/company/limit", data={
            "company": "FreeCo", "max_per_cycle": "2", "cycle": "calendar year",
            "locations_count_separately": "no", "shared_across_programmes": "yes",
            "source_url": "https://x.test/apply", "quote": "Two per year."})
        self.assertEqual(r.status_code, 200)
        rec = JobDB(self.path).get_company_limit("FreeCo")
        self.assertEqual(rec["max_per_cycle"], 2)
        self.assertEqual(rec["confidence"], "manual")
        self.assertEqual(rec["locations_count_separately"], 0)
        self.assertEqual(rec["shared_across_programmes"], 1)

    def test_blank_number_clears_back_to_unknown(self):
        c = self.owner()
        c.post("/company/limit", data={"company": "FreeCo", "max_per_cycle": "2"})
        c.post("/company/limit", data={"company": "FreeCo", "max_per_cycle": ""})
        rec = JobDB(self.path).get_company_limit("FreeCo")
        self.assertIsNone(rec["max_per_cycle"])
        self.assertEqual(rec["confidence"], "unknown")

    def test_rejects_a_nonsense_limit(self):
        c = self.owner()
        for bad in ("nine", "0", "99", "-1"):
            self.assertEqual(
                c.post("/company/limit",
                       data={"company": "FreeCo", "max_per_cycle": bad}).status_code,
                400, bad)

    def test_guest_cannot_write_a_limit(self):
        c = TestClient(webapp.app, base_url="https://testserver")
        c.post("/login", data={"username": webapp.WEB_GUEST_USER,
                               "password": webapp.WEB_GUEST_PASSWORD},
               follow_redirects=False)
        self.assertEqual(c.get("/limits").status_code, 403)
        self.assertEqual(
            c.post("/company/limit",
                   data={"company": "CappedCo", "max_per_cycle": "5"}).status_code,
            403)


if __name__ == "__main__":
    unittest.main()
