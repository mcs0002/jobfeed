"""Grad-schemes page: sweep data in, ticks out.

unittest style deliberately — pytest is not installed in the venv, so
`unittest discover` collects nothing from pytest-style bare functions.

The guard that matters most here is the boundary: the sweep owns what a
programme is, `campus_state` owns what the user did about it, and neither may
overwrite the other. A re-sweep that wiped a tick, or a tick that survived onto
a different programme, would both make the page lie.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_GUEST_PASSWORD", "guest-pw-test")
os.environ.setdefault("WEB_GUEST_USER", "guest")
os.environ.setdefault(
    "JOBS_DB", tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

from starlette.testclient import TestClient  # noqa: E402

import campus            # noqa: E402
import web.app as webapp  # noqa: E402
from db import JobDB      # noqa: E402

TODAY = date(2026, 9, 10)

RESULTS = [
    {"name": "Openbank", "url": "https://openbank.test/grads",
     "category": "Banks", "why": "programme page, not ATS rows",
     "read_at": "2026-09-09T07:00:00+00:00",
     "result": {"programmes": [
         {"name": "Graduate Programme", "status": "open",
          "deadline": "2026-09-20", "locations": "London",
          "apply_url": "https://openbank.test/apply",
          "quote": "Applications close 20 September 2026."}]}},
    # Same programme, two cities: one tick must not stand for both.
    {"name": "Twocity", "url": "https://twocity.test/grads", "category": "Funds",
     "read_at": "2026-09-09T07:00:00+00:00",
     "result": {"programmes": [
         {"name": "Trading Internship", "status": "open", "locations": "Dublin"},
         {"name": "Trading Internship", "status": "open", "locations": "Singapore"}]}},
    # Applying by email: a mailto is stripped by the href filter, so the link
    # must fall back to the programme page and the address show as text.
    {"name": "Mailfirm", "url": "https://mailfirm.test/careers", "category": "Banks",
     "read_at": "2026-09-09T07:00:00+00:00",
     "result": {"programmes": [
         {"name": "Internship", "status": "open",
          "apply_url": "mailto:grads@mailfirm.test"}]}},
    # The page prints a window that has already passed; the model called it
    # open. Arithmetic wins (reconcile_window), or a closed door reads as open.
    {"name": "Latefirm", "url": "https://latefirm.test/grads", "category": "Banks",
     "read_at": "2026-09-09T07:00:00+00:00",
     "result": {"programmes": [
         {"name": "Spring Week", "status": "open",
          "quote": "Applications open 1 March 2026 – 30 April 2026."}]}},
    {"name": "Nothingfirm", "url": "https://nothing.test/careers",
     "read_at": "2026-09-09T07:00:00+00:00",
     "result": {"programmes": [], "notes": "no early careers content"}},
]


def write_season(root: Path, name: str = "2026-09") -> Path:
    season = root / name
    season.mkdir(parents=True)
    (season / "results.jsonl").write_text(
        "\n".join(json.dumps(r) for r in RESULTS) + "\n")
    return season


class CampusLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.season = write_season(Path(self.tmp.name))
        self.data = campus.load_rows(self.season, TODAY)
        self.by_firm = {}
        for row in self.data["rows"]:
            self.by_firm.setdefault(row["firm"], []).append(row)

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_every_firm_read_not_only_the_ones_with_programmes(self):
        self.assertEqual(self.data["n_firms"], len(RESULTS))
        self.assertEqual(self.data["counts"]["none"], 1)

    def test_deadline_becomes_a_countdown(self):
        row = self.by_firm["Openbank"][0]
        self.assertEqual(row["days_left"], 10)
        self.assertEqual(row["status"], "open")

    def test_same_programme_in_two_cities_gets_two_keys(self):
        keys = {r["key"] for r in self.by_firm["Twocity"]}
        self.assertEqual(len(keys), 2, "a tick in Dublin is not a tick in Singapore")

    def test_mailto_falls_back_to_the_programme_page(self):
        row = self.by_firm["Mailfirm"][0]
        self.assertEqual(row["url"], "https://mailfirm.test/careers")
        self.assertEqual(row["apply_via"], "mailto:grads@mailfirm.test")

    def test_a_window_that_has_passed_is_closed_whatever_the_model_said(self):
        row = self.by_firm["Latefirm"][0]
        self.assertEqual(row["status"], "closed")
        self.assertTrue(row["derived_note"])

    def test_stale_days_is_measured_from_the_sweep(self):
        self.assertEqual(self.data["swept_on"], "2026-09-09")
        self.assertEqual(self.data["stale_days"], 1)

    def test_no_sweep_is_an_empty_page_not_a_crash(self):
        empty = campus.load_rows(Path(self.tmp.name) / "absent", TODAY)
        self.assertEqual(empty["rows"], [])
        self.assertIsNone(empty["stale_days"])
        self.assertIsNone(campus.latest_season(Path(self.tmp.name) / "absent"))


class CampusUITests(unittest.TestCase):
    def setUp(self):
        self._saved_auth = (webapp.WEB_PASSWORD, webapp.WEB_USER,
                            webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER)
        webapp.WEB_PASSWORD, webapp.WEB_USER = "owner-pw-test", "admin"
        webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER = "guest-pw-test", "guest"
        self._saved_db_file = webapp.DB_FILE
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        webapp.DB_FILE = self.path
        JobDB(self.path).conn.commit()
        self.tmp = tempfile.TemporaryDirectory()
        self.season = write_season(Path(self.tmp.name))
        self._saved_load = campus.load_rows_cached
        campus.load_rows_cached = lambda: campus.load_rows(self.season, TODAY)

    def tearDown(self):
        campus.load_rows_cached = self._saved_load
        (webapp.WEB_PASSWORD, webapp.WEB_USER,
         webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER) = self._saved_auth
        webapp.DB_FILE = self._saved_db_file
        self.tmp.cleanup()
        os.unlink(self.path)

    def client(self, guest: bool = False):
        c = TestClient(webapp.app, base_url="https://testserver")
        c.post("/login", data={
            "username": webapp.WEB_GUEST_USER if guest else webapp.WEB_USER,
            "password": webapp.WEB_GUEST_PASSWORD if guest else webapp.WEB_PASSWORD,
        }, follow_redirects=False)
        return c

    def key_of(self, firm: str) -> str:
        return next(r["key"] for r in campus.load_rows(self.season, TODAY)["rows"]
                    if r["firm"] == firm)

    def test_page_lists_open_programmes_with_the_page_s_own_words(self):
        r = self.client().get("/campus")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Graduate Programme", r.text)
        self.assertIn("Applications close 20 September 2026.", r.text)

    def test_closed_programmes_are_off_the_working_list_but_reachable(self):
        live = self.client().get("/campus").text
        self.assertNotIn("Spring Week", live)
        self.assertIn("Spring Week", self.client().get("/campus?show=closed").text)

    def test_tick_persists_and_leaves_the_working_list(self):
        c, key = self.client(), self.key_of("Openbank")
        r = c.post("/campus/state", data={"key": key, "state": "applied",
                                          "firm": "Openbank",
                                          "programme": "Graduate Programme"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Graduate Programme", c.get("/campus").text)
        self.assertIn("Graduate Programme", c.get("/campus?show=applied").text)
        self.assertEqual(JobDB(self.path).all_campus_state()[key]["state"], "applied")

    def test_a_tick_survives_the_next_sweep(self):
        c, key = self.client(), self.key_of("Openbank")
        c.post("/campus/state", data={"key": key, "state": "applied"})
        later = write_season(Path(self.tmp.name), "2027-01")
        campus.load_rows_cached = lambda: campus.load_rows(later, TODAY)
        self.assertIn("Graduate Programme", c.get("/campus?show=applied").text)

    def test_a_key_the_sweep_no_longer_has_is_refused_not_stored(self):
        c = self.client()
        r = c.post("/campus/state", data={"key": "gone|programme", "state": "applied"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(JobDB(self.path).all_campus_state(), {})

    def test_guest_cannot_tick(self):
        r = self.client(guest=True).post(
            "/campus/state", data={"key": self.key_of("Openbank"), "state": "applied"})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertEqual(JobDB(self.path).all_campus_state(), {})

    def test_search_narrows_to_one_firm(self):
        r = self.client().get("/campus?show=all&q=twocity")
        self.assertIn("Trading Internship", r.text)
        self.assertNotIn("Graduate Programme", r.text)


if __name__ == "__main__":
    unittest.main()
