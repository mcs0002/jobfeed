"""Application limits observed on the application form by an attended run.

The form is the one source the research pass cannot reach, so its observation
outranks the careers site, and a site figure the form did not repeat is shown
as unconfirmed. Point72's Academy form stated its cap on 2026-09-13 with no
field to carry it, which is what this covers.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_GUEST_PASSWORD", "guest-pw-test")
os.environ.setdefault("WEB_GUEST_USER", "guest")
os.environ.setdefault("WEB_SECRET", "application-workflow-test-secret")

from starlette.testclient import TestClient  # noqa: E402

from applications import status as application_status  # noqa: E402
import web.app as webapp  # noqa: E402
from applications.handoff import workflow_token  # noqa: E402
from jobfeed.db import JobDB, quote_states_number  # noqa: E402

QUOTE = ("Please note, you may only submit one application to the Academy "
         "program globally, so please be sure to apply to the region you are "
         "most interested in.")


class FormLimitDBTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.db = JobDB(self.path)

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.path)

    def test_number_gate_accepts_words_and_digits_only_when_they_match(self):
        self.assertTrue(quote_states_number(1, QUOTE))
        self.assertTrue(quote_states_number(3, "No more than 3 applications."))
        self.assertFalse(quote_states_number(2, QUOTE))

    def test_stated_needs_its_number_in_its_own_quote(self):
        with self.assertRaises(ValueError):
            self.db.record_form_limit("P72", verdict="stated", max_per_cycle=2,
                                      quote=QUOTE)
        self.assertEqual("stated", self.db.record_form_limit(
            "P72", verdict="stated", max_per_cycle=1, quote=QUOTE,
            workflow_id="apply_1"))
        row = self.db.get_company_limit("P72")
        self.assertEqual((row["form_verdict"], row["form_max"]), ("stated", 1))

    def test_a_later_silent_form_never_downgrades_a_stated_cap(self):
        self.db.record_form_limit("P72", verdict="stated", max_per_cycle=1,
                                  quote=QUOTE)
        self.assertEqual("", self.db.record_form_limit("P72", verdict="absent"))
        self.assertEqual("stated", self.db.get_company_limit("P72")["form_verdict"])

    def test_a_research_sweep_cannot_erase_form_evidence(self):
        self.db.record_form_limit("P72", verdict="stated", max_per_cycle=1,
                                  quote=QUOTE)
        self.db.set_company_limit("P72", max_per_cycle=None, confidence="unknown",
                                  quote="checked 3 page(s), no cap language found")
        row = self.db.get_company_limit("P72")
        self.assertEqual((row["form_verdict"], row["form_max"]), ("stated", 1))


class FormLimitViewTests(unittest.TestCase):
    def test_form_figure_replaces_the_researched_one(self):
        view = webapp._limit_view({
            "max_per_cycle": 3, "quote": "site says three", "strength": "advisory",
            "form_verdict": "stated", "form_max": 1, "form_quote": QUOTE,
            "form_checked_at": "2026-09-13T11:00:00+00:00"}, 0, 0)
        self.assertEqual((view["cap"], view["proof"], view["quote"]), (1, "form", QUOTE))

    def test_a_form_does_not_flatten_a_regional_rule(self):
        """UBS, 2026-09-15: the Frankfurt form's "one program per academic year"
        covers EMEA without Switzerland and the UK, not the whole firm."""
        view = webapp._limit_view({
            "max_per_cycle": 1, "quote": "It actually depends on the region.",
            "varies_by_region": 1, "cycle": "the same academic year",
            "form_verdict": "stated", "form_max": 1,
            "form_quote": "You can only apply to one program per academic year.",
            "form_checked_at": "2026-09-15T11:50:29+00:00"}, 1, 1)
        self.assertEqual((view["varies"], view["proof"], view["quote"]),
                         (True, "form_region", "It actually depends on the region."))
        self.assertEqual("You can only apply to one program per academic year.", view["form_quote"])

    def test_silent_form_marks_the_site_figure_unconfirmed(self):
        view = webapp._limit_view({"max_per_cycle": 1, "quote": "site",
                                   "form_verdict": "absent"}, 0, 0)
        self.assertEqual((view["cap"], view["proof"]), (1, "unconfirmed"))


class StatusRequestTests(unittest.TestCase):
    def _read(self, obj):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(obj, fh)
        try:
            return application_status._read_request(Path(fh.name))
        finally:
            os.unlink(fh.name)

    def test_limit_rides_along_in_the_same_fixed_file(self):
        req = self._read({"workflow_id": "apply_abc123", "run_id": "run_def456",
                          "status": "review_ready",
                          "application_limit": {"stated": True, "max": 1, "quote": QUOTE}})
        self.assertEqual((req["limit_verdict"], req["limit_max"]), ("stated", "1"))
        self.assertEqual("run_def456", req["run_id"])
        req = self._read({"workflow_id": "apply_abc123", "status": "review_ready",
                          "application_limit": {"stated": False}})
        self.assertEqual(req["limit_verdict"], "absent")

    def test_malformed_limits_are_refused(self):
        for bad in ({"stated": True, "quote": QUOTE}, {"stated": True, "max": 1},
                    {"stated": "yes"}, {"stated": False, "extra": 1}):
            with self.assertRaises(ValueError):
                self._read({"workflow_id": "apply_abc123", "status": "review_ready",
                            "application_limit": bad})

    def test_reporter_forwards_the_limit_fields(self):
        seen = {}

        class Resp:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(url, data=None, timeout=None):
            seen["body"] = data.decode()
            return Resp()

        with mock.patch.dict(os.environ, {"WEB_SECRET": "s", "WEB_PUBLIC_BASE_URL": "https://x"}), \
                mock.patch("urllib.request.urlopen", fake):
            application_status.report({"workflow_id": "apply_abc123", "status": "review_ready",
                                       "run_id": "run_def456", "detail": "",
                                       "limit_verdict": "absent"})
        self.assertIn("limit_verdict=absent", seen["body"])
        self.assertIn("run_id=run_def456", seen["body"])


class FormLimitRouteTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self._saved = webapp.DB_FILE
        webapp.DB_FILE = self.path
        db = JobDB(self.path)
        db.mark_seen("p1", company="Point72 (Campus)", title="Academy",
                     url="https://x.test/p1")
        # Research ran under the other label; the form evidence must land beside it.
        db.set_company_limit("Point72", max_per_cycle=None, confidence="unknown",
                             quote="no cap language found")
        wf, _ = db.create_application_workflow("p1", "https://x.test/p1")
        db.transition_application_workflow(wf["workflow_id"], "in_progress", actor="agent")
        self.wid = wf["workflow_id"]
        db.conn.close()
        self.token = workflow_token(webapp.WEB_SECRET, self.wid)
        self.client = TestClient(webapp.app, base_url="https://testserver")

    def tearDown(self):
        webapp.DB_FILE = self._saved
        os.unlink(self.path)

    def post(self, **data):
        return self.client.post(f"/application/{self.wid}/agent/status",
                                data={"token": self.token, **data})

    def test_review_ready_records_a_stated_cap_against_the_existing_firm_row(self):
        r = self.post(status="review_ready", limit_verdict="stated",
                      limit_max="1", limit_quote=QUOTE)
        self.assertEqual(200, r.status_code)
        row = JobDB(self.path).get_company_limit("Point72")
        self.assertEqual((row["form_verdict"], row["form_max"]), ("stated", 1))
        self.assertIsNone(JobDB(self.path).get_company_limit("Point72 (Campus)"))

    def test_absent_needs_review_ready_but_never_blocks_the_status(self):
        r = self.post(status="needs_user_action", limit_verdict="absent")
        self.assertEqual(200, r.status_code)
        self.assertEqual("needs_user_action",
                         JobDB(self.path).get_application_workflow(self.wid)["status"])
        self.assertEqual("", JobDB(self.path).get_company_limit("Point72")["form_verdict"])

    def test_a_quote_without_its_number_is_discarded_not_fatal(self):
        r = self.post(status="review_ready", limit_verdict="stated",
                      limit_max="2", limit_quote=QUOTE)
        self.assertEqual(200, r.status_code)
        self.assertEqual("", JobDB(self.path).get_company_limit("Point72")["form_verdict"])



class FormLimitRenderTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self._saved = (webapp.DB_FILE, webapp.WEB_PASSWORD, webapp.WEB_USER)
        webapp.DB_FILE = self.path
        webapp.WEB_PASSWORD, webapp.WEB_USER = "owner-pw-test", "admin"
        db = JobDB(self.path)
        for jid, co in (("f1", "FormCo"), ("s1", "SiteCo"), ("n1", "NoneCo")):
            db.mark_seen(jid, company=co, title="Graduate Analyst",
                         url=f"https://x.test/{jid}")
        db.set_company_limit("FormCo", max_per_cycle=3, confidence="stated",
                             strength="advisory", quote="We suggest three at most.")
        db.record_form_limit("FormCo", verdict="stated", max_per_cycle=1, quote=QUOTE)
        db.set_company_limit("SiteCo", max_per_cycle=1, confidence="stated",
                             strength="hard", quote="Only one application per year.")
        db.record_form_limit("SiteCo", verdict="absent")
        db.record_form_limit("NoneCo", verdict="absent")
        db.conn.close()
        self.client = TestClient(webapp.app, base_url="https://testserver")
        self.client.post("/login", data={"username": "admin",
                                         "password": "owner-pw-test"},
                         follow_redirects=False)

    def tearDown(self):
        webapp.DB_FILE, webapp.WEB_PASSWORD, webapp.WEB_USER = self._saved
        os.unlink(self.path)

    def test_limits_page_separates_proof_from_guess(self):
        text = self.client.get("/limits").text
        self.assertIn("form ✓", text)
        self.assertIn("only submit one application to the Academy", text)
        self.assertNotIn("We suggest three at most.", text)
        self.assertIn("none on the form", text)

    def test_detail_pane_and_list_badge_show_the_markers(self):
        self.assertIn("on the form ✓", self.client.get("/job/f1?pane=1").text)
        self.assertIn("not on the form ?", self.client.get("/job/s1?pane=1").text)
        self.assertIn("the application form stated none",
                      self.client.get("/job/n1?pane=1").text)
        listing = self.client.get("/").text
        self.assertIn("0/1 appl ✓", listing)
        self.assertIn("0/1 appl ?", listing)


if __name__ == "__main__":
    unittest.main()
