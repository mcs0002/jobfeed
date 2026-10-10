"""Adding a role by its posting URL (jobfeed/manual_add.py, /add).

Every fetch here goes through the real GuardedSession: only the transport
(requests.Session.request) and DNS (socket.getaddrinfo) are faked, so the SSRF
guard runs on each path, redirects and enricher API calls included.
"""
import io
import json
import os
import re
import socket
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_GUEST_PASSWORD", "guest-pw-test")
os.environ.setdefault("WEB_GUEST_USER", "guest")
os.environ.setdefault("WEB_SECRET", "application-workflow-test-secret")

import requests  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import web.app as webapp  # noqa: E402
from jobfeed import db as dbmod, manual_add, safe_fetch  # noqa: E402
from jobfeed.db import JobDB  # noqa: E402
from jobfeed.filter import drop_reason, is_relevant  # noqa: E402
from scripts import export_shared  # noqa: E402

SOON = (date.today() + timedelta(days=20)).isoformat()
BODY = ("You will join the rates trading desk as a graduate analyst, pricing swaps "
        "and supporting traders across the European session. ") * 8
PRIVATE = {"intranet.test": "10.0.0.7", "meta.test": "169.254.169.254",
           "loop.test": "127.0.0.1", "v6loop.test": "::1",
           "mapped.test": "::ffff:127.0.0.1"}


def fake_dns(host, port, *a, **k):
    ip = PRIVATE.get(host, "93.184.216.34")
    if host.replace(".", "").isdigit() or ":" in host:
        ip = host
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]


def response(url, body="", status=200, headers=None):
    r = requests.Response()
    r.status_code, r.url, r.encoding = status, url, "utf-8"
    r.headers.update(headers or {})
    r.raw = io.BytesIO(body.encode() if isinstance(body, str) else body)
    return r


class Web:
    """Routes for the fake transport: url (prefix) -> Response factory."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    @property
    def method(self):
        """A plain function, so it binds as Session.request does."""
        def request(session, method, url, **kw):
            return self(session, method, url, **kw)
        return request

    def __call__(self, session, method, url, **kw):
        self.calls.append(url)
        for prefix, make in self.routes.items():
            if url.startswith(prefix):
                return make(url)
        return response(url, "not found", 404)


def jsonld_page(**over):
    node = {"@context": "https://schema.org", "@type": "JobPosting",
            "title": "Graduate Analyst, Rates", "description": "<p>" + BODY + "</p>",
            "hiringOrganization": {"@type": "Organization", "name": "Hilltop Capital"},
            "jobLocation": {"@type": "Place", "address": {
                "addressLocality": "London", "addressCountry": "GB"}},
            "datePosted": date.today().isoformat(), "validThrough": SOON}
    node.update(over)
    return (f'<html><head><script type="application/ld+json">{json.dumps(node)}'
            '</script></head><body>app</body></html>')


def stub_tagger(jobs):
    for j in jobs:
        j.update(area="markets", desk="rates", seniority="entry", job_type="job",
                 loc_city="London", loc_country="UK", loc_region="Europe")
    return jobs


class _Case(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.db = JobDB(self.path)
        self.dns = mock.patch.object(socket, "getaddrinfo", fake_dns)
        self.dns.start()

    def tearDown(self):
        self.dns.stop()
        self.db.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def add(self, routes, url, **kw):
        web = Web(routes)
        with mock.patch.object(requests.Session, "request", web.method):
            return manual_add.add(self.db, url, tag_fn=kw.pop("tag_fn", stub_tagger), **kw), web


class SsrfTests(_Case):
    def test_private_loopback_link_local_and_odd_schemes_are_refused(self):
        for url in ("http://127.0.0.1/", "http://10.1.2.3/job", "http://169.254.169.254/latest",
                    "http://[::1]/", "http://intranet.test/jobs/1", "http://meta.test/",
                    "http://loop.test:8080/", "http://v6loop.test/", "http://mapped.test/",
                    "ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)",
                    "http://user:pw@example.com/", "http://0.0.0.0/", "http://192.168.1.1/"):
            with self.subTest(url=url), self.assertRaises(safe_fetch.Refused):
                safe_fetch.check_url(url)
        safe_fetch.check_url("https://careers.example.com/job/1")

    def test_a_redirect_into_the_network_is_refused_and_nothing_is_stored(self):
        routes = {"https://jobs.example.com/": lambda u: response(
            u, "", 302, {"location": "http://intranet.test/admin"})}
        result, web = self.add(routes, "https://jobs.example.com/p/1")
        self.assertEqual("refused", result.outcome)
        self.assertIn("private or local", result.message)
        self.assertNotIn("http://intranet.test/admin", web.calls)
        self.assertEqual(0, self.db.conn.execute("SELECT count(*) FROM seen_jobs").fetchone()[0])

    def test_the_size_cap_holds(self):
        big = "x" * (safe_fetch.MAX_BYTES + 10)
        routes = {"https://jobs.example.com/": lambda u: response(u, big)}
        web = Web(routes)
        with mock.patch.object(requests.Session, "request", web.method), \
                self.assertRaises(safe_fetch.Refused):
            safe_fetch.GuardedSession().get("https://jobs.example.com/huge")


class DuplicateTests(_Case):
    def test_a_stored_url_opens_the_existing_row(self):
        self.db.mark_seen("gh_991", company="Hilltop", title="Analyst",
                          url="https://boards.greenhouse.io/hilltop/jobs/991")
        r, web = self.add({}, "https://boards.greenhouse.io/hilltop/jobs/991?utm_source=li#apply")
        self.assertEqual(("existing", "gh_991"), (r.outcome, r.job_id))
        self.assertEqual([], web.calls, "a duplicate is never fetched")

    def test_the_ats_job_id_finds_a_row_stored_under_another_url(self):
        self.db.mark_seen("wd_acme_Rates-Analyst_R123", company="Acme", title="Rates Analyst",
                          url="https://acme.wd3.myworkdayjobs.com/External/job/London/Rates-Analyst_R123")
        r, _ = self.add({}, "https://acme.wd3.myworkdayjobs.com/en-US/External/job/London/Rates-Analyst_R123")
        self.assertEqual("wd_acme_Rates-Analyst_R123", r.job_id)
        self.db.mark_seen("oracle_CX_1_555", company="Bank", title="Analyst", url="https://x/other")
        r, _ = self.add({}, "https://bank.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/555/?q=1")
        self.assertEqual("oracle_CX_1_555", r.job_id)

    def test_adding_the_same_link_twice_keeps_one_row(self):
        routes = {"https://www.hilltop.example/": lambda u: response(u, jsonld_page())}
        first, _ = self.add(routes, "https://www.hilltop.example/careers/rates-analyst")
        second, _ = self.add(routes, "https://www.hilltop.example/careers/rates-analyst/?utm_campaign=x")
        self.assertEqual(("added", "existing"), (first.outcome, second.outcome))
        self.assertEqual(first.job_id, second.job_id)


class FetchPathTests(_Case):
    def test_json_ld_posting_becomes_a_tagged_row(self):
        routes = {"https://www.hilltop.example/": lambda u: response(u, jsonld_page())}
        r, _ = self.add(routes, "https://www.hilltop.example/careers/rates-analyst")
        self.assertEqual("added", r.outcome, r.message)
        self.assertTrue(r.job_id.startswith(dbmod.MANUAL_ID_PREFIX))
        job = self.db.get_job(r.job_id)
        self.assertEqual(("Graduate Analyst, Rates", "Hilltop Capital", "London, GB"),
                         (job["title"], job["company"], job["location"]))
        self.assertEqual(SOON, job["deadline"])
        self.assertIn("rates trading desk", job["description"])
        self.assertEqual(("markets", "rates"), (job["area"], job["desk"]))
        self.assertEqual(0, job["min_yoe"])
        self.assertIn("JSON-LD", r.message)

    def test_oracle_enricher_path_with_its_end_date(self):
        url = "https://bank.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/777"
        api = {"items": [{"ExternalDescriptionStr": "<p>" + BODY + "</p>",
                          "ExternalPostedStartDate": date.today().isoformat(),
                          "ExternalPostedEndDate": SOON}]}
        page = ('<html><head><title>Rates Analyst</title><meta property="og:title" '
                'content="Rates Analyst - Graduate"><meta property="og:site_name" '
                'content="Example Bank"></head><body></body></html>')
        routes = {"https://bank.fa.em2.oraclecloud.com/hcmRestApi/": lambda u: response(u, json.dumps(api)),
                  "https://bank.fa.em2.oraclecloud.com/hcmUI/": lambda u: response(u, page)}
        r, web = self.add(routes, url, location="Frankfurt")
        self.assertEqual("added", r.outcome, r.message)
        self.assertIn("oracle enricher", r.message)
        job = self.db.get_job(r.job_id)
        self.assertEqual(("Rates Analyst - Graduate", "Example Bank", "Frankfurt", SOON),
                         (job["title"], job["company"], job["location"], job["deadline"]))
        self.assertTrue(any("recruitingCEJobRequisitionDetails" in c for c in web.calls))

    def test_a_login_wall_stores_nothing_and_asks_for_the_text(self):
        wall = "<html><body>Sign in to see this job. Join now.</body></html>"
        routes = {"https://www.linkedin.com/": lambda u: response(u, wall)}
        url = "https://www.linkedin.com/jobs/view/4012345678"
        r, _ = self.add(routes, url)
        self.assertEqual("needs_input", r.outcome)
        self.assertIn("Paste the description", r.message)
        self.assertEqual(0, self.db.conn.execute("SELECT count(*) FROM seen_jobs").fetchone()[0])
        r, _ = self.add(routes, url, title="Rates Analyst", company="Hilltop Capital",
                        description=BODY)
        self.assertEqual("added", r.outcome)
        self.assertEqual(BODY.strip(), self.db.get_job(r.job_id)["description"])

    def test_scan_gates_warn_instead_of_dropping_and_yoe_is_detected(self):
        page = jsonld_page(title="Senior Vice President, Rates Trading",
                           description="<p>" + BODY + " Minimum 7 years of experience in rates.</p>")
        routes = {"https://www.hilltop.example/": lambda u: response(u, page)}

        def no_tags(jobs):
            return jobs
        r, _ = self.add(routes, "https://www.hilltop.example/careers/svp", tag_fn=no_tags)
        self.assertEqual("added", r.outcome)
        joined = " ".join(r.warnings)
        self.assertIn("A scan would have dropped this role (senior title)", joined)
        self.assertIn("7+ years", joined)
        self.assertIn("untagged", joined)
        self.assertEqual(7, self.db.get_job(r.job_id)["min_yoe"])


class LifecycleTests(_Case):
    def test_delist_age_and_orphan_passes_leave_manual_rows_alone(self):
        self.db.mark_seen("manual_abc", company="Hilltop Capital", title="Analyst", url="u1")
        self.db.mark_seen("gh_1", company="Hilltop Capital", title="Analyst 2", url="u2")
        self.db.conn.execute("UPDATE seen_jobs SET last_seen='2020-01-01'")
        self.db.conn.commit()
        self.assertEqual(["gh_1"], self.db.find_delistable({"Hilltop Capital": set()}))
        self.assertEqual(["gh_1"], self.db.find_age_delistable(21))
        self.assertEqual(1, self.db.purge_orphaned_companies(set()))
        self.assertTrue(self.db.seen("manual_abc"))

    def test_drop_reason_is_is_relevants_one_definition(self):
        for title in ("Graduate Analyst", "Senior Vice President", "IT Helpdesk Technician",
                      "Analyst - 5 years experience", "Teller"):
            job = {"title": title, "location": "London"}
            self.assertEqual(is_relevant(job), not drop_reason(job), title)


class ExportTests(_Case):
    def test_shared_columns_are_listed_once(self):
        # Two concurrent commits once both classified ai_tech; the doubled
        # name made the export's CREATE TABLE fail (2026-10-10).
        self.assertEqual(len(export_shared.SHARED_COLUMNS),
                         len(set(export_shared.SHARED_COLUMNS)))

    def test_the_column_check_passes_and_manual_rows_stay_private(self):
        live = [r[1] for r in self.db.conn.execute("PRAGMA table_info(jobs_with_description)")]
        export_shared.classify_columns(live, with_descriptions=True)  # no abort
        self.assertEqual(dbmod.MANUAL_ID_PREFIX, export_shared.MANUAL_ID_PREFIX)
        self.db.mark_seen("manual_abc", company="Hilltop", title="Analyst", url="u1",
                          description=BODY)
        self.db.mark_seen("gh_1", company="Hilltop", title="Analyst", url="u2")
        self.db.mark_seen("manualx_1", company="Other", title="Analyst", url="u3")
        self.db.conn.commit()
        dest = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        os.unlink(dest)
        try:
            manifest = export_shared.export(self.path, dest, with_descriptions=True)
            export_shared.verify(dest)
            ids = {r[0] for r in sqlite3.connect(dest).execute("SELECT id FROM jobs")}
            self.assertEqual({"gh_1", "manualx_1"}, ids)
            self.assertEqual(2, manifest["rows"])
        finally:
            os.unlink(dest)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self._saved = (webapp.WEB_PASSWORD, webapp.WEB_USER, webapp.WEB_GUEST_PASSWORD,
                       webapp.WEB_GUEST_USER, webapp.DB_FILE)
        webapp.WEB_PASSWORD, webapp.WEB_USER = "owner-pw-test", "admin"
        webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER = "guest-pw-test", "guest"
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        webapp.DB_FILE = self.path
        JobDB(self.path).conn.close()
        self.patches = [mock.patch.object(socket, "getaddrinfo", fake_dns),
                        mock.patch("jobfeed.tag.tag_jobs", stub_tagger)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        (webapp.WEB_PASSWORD, webapp.WEB_USER, webapp.WEB_GUEST_PASSWORD,
         webapp.WEB_GUEST_USER, webapp.DB_FILE) = self._saved
        os.unlink(self.path)

    def login(self, user, pw):
        c = TestClient(webapp.app, base_url="https://testserver")
        c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
        return c

    def csrf(self, c):
        return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/add").text).group(1)

    def test_guest_is_denied_the_form_and_the_post(self):
        c = self.login(webapp.WEB_GUEST_USER, webapp.WEB_GUEST_PASSWORD)
        self.assertEqual(403, c.get("/add").status_code)
        self.assertEqual(403, c.post("/add", data={"url": "https://x.example/"}).status_code)
        self.assertNotIn('href="/add"', c.get("/").text)

    def test_owner_adds_a_role_and_lands_on_its_page(self):
        c = self.login(webapp.WEB_USER, webapp.WEB_PASSWORD)
        self.assertIn('href="/add"', c.get("/").text)
        self.assertIn('href="/add"', c.get("/applications").text)
        self.assertEqual(403, c.post("/add", data={"url": "https://x.example/"}).status_code)
        web = Web({"https://www.hilltop.example/": lambda u: response(u, jsonld_page())})
        with mock.patch.object(requests.Session, "request", web.method):
            r = c.post("/add", data={"csrf_token": self.csrf(c),
                                     "url": "https://www.hilltop.example/careers/rates"},
                       follow_redirects=False)
        self.assertEqual(303, r.status_code)
        self.assertTrue(r.headers["location"].startswith("/job/manual_"))
        page = c.get(r.headers["location"])
        self.assertIn("Graduate Analyst, Rates", page.text)
        self.assertIn("Added; description from the JSON-LD.", page.text)
        # The detail pane's attended-application start (button "Auto apply").
        self.assertIn(f'hx-post="{r.headers["location"]}/apply"', page.text)

    def test_a_private_link_is_refused_on_the_form(self):
        c = self.login(webapp.WEB_USER, webapp.WEB_PASSWORD)
        r = c.post("/add", data={"csrf_token": self.csrf(c), "url": "http://intranet.test/"})
        self.assertEqual(400, r.status_code)
        self.assertIn("private or local", r.text)


if __name__ == "__main__":
    unittest.main()
