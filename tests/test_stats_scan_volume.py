"""Latest-scan acquisition funnel shown on Stats."""
import unittest
import os
import tempfile
from pathlib import Path

os.environ["WEB_PASSWORD"] = "owner-pw-test"
os.environ["WEB_USER"] = "admin"
os.environ["WEB_GUEST_PASSWORD"] = "guest-pw-test"
os.environ["WEB_GUEST_USER"] = "guest"
os.environ.setdefault(
    "JOBS_DB", tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

from jobfeed.db import JobDB
from starlette.testclient import TestClient
import web.app as webapp
from web.app import _scan_volume


class ScanVolumeTests(unittest.TestCase):
    def test_stats_pages_are_top_level_navigation(self):
        root = Path(__file__).resolve().parents[1]
        base = (root / "web/templates/base.html").read_text()
        stats = (root / "web/templates/stats.html").read_text()
        css = (root / "web/static/style.css").read_text()
        app = (root / "web/app.py").read_text()
        hrefs = [item["href"] for item in webapp.NAV_ITEMS]
        self.assertLess(hrefs.index("/applications"), hrefs.index("/stats/applications"))
        self.assertLess(hrefs.index("/stats/applications"), hrefs.index("/stats/technical"))
        self.assertLess(hrefs.index("/stats/technical"), hrefs.index("/sources"))
        self.assertEqual(
            [item["href"] for item in webapp.NAV_ITEMS if item.get("guest")],
            ["/", "/stats/technical", "/sources"],
        )
        self.assertIn("nav_items(request)", base)
        self.assertIn("Application stats", stats)
        self.assertIn("Technical stats", stats)
        self.assertIn('{% if stats_view == "application" %}', stats)
        self.assertNotIn("stats-tabs", stats)
        self.assertNotIn("stats-tab", css)
        self.assertIn('@app.get("/stats/applications"', app)
        self.assertIn('@app.get("/stats/technical"', app)

    def test_each_stats_route_renders_only_its_own_page(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = JobDB(tmp.name)
        db.conn.close()
        saved = (webapp.DB_FILE, webapp.WEB_USER, webapp.WEB_PASSWORD)
        webapp.DB_FILE = tmp.name
        webapp.WEB_USER = "admin"
        webapp.WEB_PASSWORD = "owner-pw-test"
        try:
            client = TestClient(webapp.app, base_url="https://testserver")
            login = client.post("/login", data={"username": "admin",
                                                  "password": "owner-pw-test"},
                                follow_redirects=False)
            self.assertEqual(login.status_code, 303)

            application = client.get("/stats/applications")
            self.assertEqual(application.status_code, 200)
            self.assertIn("Application stats — Jobfeed", application.text)
            self.assertIn("Pipeline", application.text)
            self.assertNotIn("Historical stored", application.text)

            technical = client.get("/stats/technical")
            self.assertEqual(technical.status_code, 200)
            self.assertIn("Technical stats — Jobfeed", technical.text)
            self.assertIn("Historical stored", technical.text)
            self.assertNotIn('<div class="pl-title">Pipeline</div>', technical.text)

            legacy = client.get("/stats")
            self.assertEqual(legacy.status_code, 200)
            self.assertIn("Application stats — Jobfeed", legacy.text)
        finally:
            webapp.DB_FILE, webapp.WEB_USER, webapp.WEB_PASSWORD = saved
            os.unlink(tmp.name)

    def test_every_pipeline_card_opens_its_role(self):
        """Tudor (area 'other') and a delisted J.P. Morgan role opened nothing
        from the board, and Deutsche Bank's programme tick was not on it at
        all (2026-09-17)."""
        import re
        from urllib.parse import unquote
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = JobDB(tmp.name)
        db.mark_seen("tudor", company="Tudor Investment Corp",
                     title="Discretionary Global Macro Application", url="https://x.test/t")
        db.mark_seen("jpm", company="J.P. Morgan", title="Analyst Training Program",
                     url="https://x.test/j")
        db.conn.execute("UPDATE seen_jobs SET area='other' WHERE id='tudor'")
        db.conn.execute("UPDATE seen_jobs SET area='asset-management', "
                        "delisted_at='2026-09-10T00:00:00+00:00' WHERE id='jpm'")
        db.conn.commit()
        db.set_status("tudor", "applied")
        db.set_status("jpm", "oa")
        db.set_campus_state("deutschebank|graduateprogramme", state="applied",
                            firm="Deutsche Bank", programme="Graduate Programme")
        db.set_campus_state("jpmorgan|analyst", state="applied",
                            firm="J.P. Morgan", programme="Analyst Programme")
        db.conn.close()
        saved = (webapp.DB_FILE, webapp.WEB_USER, webapp.WEB_PASSWORD)
        webapp.DB_FILE = tmp.name
        webapp.WEB_USER = "admin"
        webapp.WEB_PASSWORD = "owner-pw-test"
        try:
            client = TestClient(webapp.app, base_url="https://testserver")
            client.post("/login", data={"username": "admin",
                                        "password": "owner-pw-test"})
            page = client.get("/stats/applications").text
            links = [h.replace("&amp;", "&")
                     for h in re.findall(r'class="pl-card" href="([^"]+)"', page)]
            self.assertIn("Deutsche Bank · Grad page", page)
            # A firm whose board row carries the application gets no second card.
            self.assertNotIn("J.P. Morgan · Grad page", page)
            self.assertEqual(3, len(links))
            # Rejections are a lane of their own, after the open stages.
            self.assertIn('class="pl-name">rejected<', page)
            self.assertNotIn('class="pl-name">offer<', page)
            for href in links:
                if href.startswith("/campus"):
                    self.assertEqual(200, client.get(href).status_code)
                    continue
                # The role's own page, as on Applications, so no Browse filter
                # can hide it.
                self.assertTrue(href.startswith("/job/"), href)
                self.assertEqual(200, client.get(href).status_code, href)
        finally:
            webapp.DB_FILE, webapp.WEB_USER, webapp.WEB_PASSWORD = saved
            os.unlink(tmp.name)

    def test_raw_is_before_negative_filter(self):
        state = {
            "last_raw_counts": {"Alpha": 10, "Beta": 7},
            "last_filtered_counts": {"Alpha": 6, "Beta": 5},
        }
        self.assertEqual(_scan_volume(state), {
            "available": True, "raw": 17, "passed": 11, "rejected": 6,
        })

    def test_hidden_sources_are_removed_from_both_sides(self):
        state = {
            "last_raw_counts": {"Alpha": 10, "Beta": 7},
            "last_filtered_counts": {"Alpha": 6, "Beta": 5},
        }
        self.assertEqual(_scan_volume(state, {"Beta"}), {
            "available": True, "raw": 10, "passed": 6, "rejected": 4,
        })

    def test_old_state_waits_for_next_scan(self):
        self.assertFalse(_scan_volume({"last_raw_counts": {"Alpha": 10}})["available"])


if __name__ == "__main__":
    unittest.main()
