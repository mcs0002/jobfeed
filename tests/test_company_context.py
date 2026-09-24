"""Company-context panel in the role detail pane (2026-09-23).

Application history at the normalised firm, a short list of relevant open
roles, and an owner-only boundary: the panel exposes application history, so a
guest must never receive it, not merely not see it."""
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

from jobfeed.db import JobDB  # noqa: E402

CURRENT = dict(area="markets", desk="trading", job_type="graduate-programme",
               loc_city="Zurich", loc_country="Switzerland", loc_region="Europe")


def _row(db, jid, company, title, status="new", applied_at=None, delisted=False,
         first_seen="2026-09-01T00:00:00+00:00", min_yoe=0, **facets):
    db.mark_seen(jid, company=company, title=title, url=f"https://x.test/{jid}")
    tags = {**CURRENT, **facets}
    db.conn.execute(
        "UPDATE seen_jobs SET status=?, applied_at=?, delisted_at=?, first_seen=?,"
        " min_yoe=?, area=?, desk=?, job_type=?, loc_city=?, loc_country=?,"
        " loc_region=?, seniority='graduate' WHERE id=?",
        (status, applied_at, "2026-09-10T00:00:00+00:00" if delisted else None,
         first_seen, min_yoe, tags["area"], tags["desk"], tags["job_type"],
         tags["loc_city"], tags["loc_country"], tags["loc_region"], jid))
    db.conn.commit()


def seed(db):
    # The role being viewed.
    _row(db, "cur", "UBS (Graduate Careers)", "Markets Graduate Programme Zurich")
    # History: applications at both source labels, one of them delisted.
    _row(db, "app1", "UBS", "Global Markets Analyst London", status="applied",
         applied_at="2026-09-12T10:00:00+00:00", loc_city="London",
         loc_country="United Kingdom")
    _row(db, "app2", "UBS (Graduate Careers)", "Markets Programme Frankfurt",
         status="rejected", applied_at="2026-08-01T10:00:00+00:00", delisted=True,
         loc_city="Frankfurt", loc_country="Germany")
    # Not history: an intention, a decision against, an untouched role, and
    # a different firm whose name merely contains the letters.
    _row(db, "q1", "UBS", "Queued Role", status="queued", area="ibd")
    _row(db, "ign", "UBS", "Ignored Role", status="ignored", area="ibd")
    _row(db, "hubs", "Hubs Capital", "Applied Elsewhere", status="applied",
         applied_at="2026-09-20T10:00:00+00:00")
    # Relevance candidates (all open unless stated).
    _row(db, "zrh", "UBS", "Markets Graduate Zurich 2", first_seen="2026-08-01T00:00:00+00:00")
    _row(db, "ldn", "UBS", "Markets Graduate London", loc_city="London",
         loc_country="United Kingdom", first_seen="2026-09-05T00:00:00+00:00")
    _row(db, "nyc", "UBS", "Markets Graduate New York", loc_city="New York",
         loc_country="United States", loc_region="Americas",
         first_seen="2026-09-06T00:00:00+00:00")
    _row(db, "ibd", "UBS", "IBD Graduate Zurich", area="ibd")
    _row(db, "intern", "UBS", "Markets Intern Zurich", job_type="internship")
    _row(db, "dead", "UBS", "Markets Graduate Zurich Delisted", delisted=True)
    _row(db, "other", "UBS", "Facilities Graduate", area="other")
    _row(db, "senior", "UBS", "Markets Graduate (5 yrs)", min_yoe=5)
    _row(db, "cs", "Credit Suisse", "Markets Graduate Zurich CS")


class CompanyContextDBTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)
        seed(self.db)
        self.aliases = self.db.company_aliases("UBS (Graduate Careers)")

    def test_parenthetical_source_labels_normalise_to_one_firm(self):
        self.assertEqual(self.aliases, ["UBS", "UBS (Graduate Careers)"])
        self.assertEqual(self.db.company_aliases("UBS"), self.aliases)
        self.assertNotIn("Hubs Capital", self.aliases)
        self.assertEqual(self.db.company_aliases(""), [])

    def test_history_is_applications_only_newest_first_incl_delisted(self):
        apps = self.db.company_applications(self.aliases)
        self.assertEqual([a["id"] for a in apps], ["app1", "app2"])
        self.assertIsNotNone(apps[1]["delisted_at"])   # delisted stays in history
        ids = {a["id"] for a in apps}
        self.assertFalse(ids & {"q1", "ign", "cur", "hubs"})

    def test_relevant_roles_match_area_and_type_ranked_by_location(self):
        job = {**self.db.get_job("cur"), **CURRENT}
        got = [r["id"] for r in self.db.relevant_company_roles(self.aliases, job)]
        # Same city first, then same region (London) before another region.
        self.assertEqual(got, ["zrh", "ldn", "nyc"])

    def test_relevant_roles_exclude_what_should_not_show(self):
        job = {**self.db.get_job("cur"), **CURRENT}
        got = {r["id"] for r in self.db.relevant_company_roles(self.aliases, job)}
        # current role, delisted, other area/type, 'other', senior, applied,
        # queued/ignored and a different firm never appear.
        self.assertFalse(got & {"cur", "dead", "ibd", "intern", "other", "senior",
                                "app1", "app2", "q1", "ign", "cs"})

    def test_untagged_role_has_nothing_to_match_on(self):
        self.assertEqual(self.db.relevant_company_roles(
            self.aliases, {"id": "x", "area": ""}), [])

    def test_firm_filter_is_exact_across_source_labels(self):
        ids = {r["id"] for r in self.db.fetch_jobs(firm="UBS")}
        self.assertTrue({"cur", "app1", "app2", "zrh"} <= ids)
        self.assertNotIn("hubs", ids)
        self.assertEqual({r["id"] for r in self.db.fetch_jobs(firm="ubs (graduate careers)")},
                         {"cur", "app2"})
        # The collision the substring `company` filter has: "EY" inside "Walleye".
        _row(self.db, "wal", "Walleye Capital", "Quant Analyst")
        _row(self.db, "ey", "EY", "Consultant")
        _row(self.db, "ey2", "EY (Parthenon)", "Associate")
        self.assertEqual({r["id"] for r in self.db.fetch_jobs(firm="EY")}, {"ey", "ey2"})
        self.assertIn("wal", {r["id"] for r in self.db.fetch_jobs(company="EY")})
        # LIKE wildcards in a firm name are literal.
        self.assertEqual(self.db.fetch_jobs(firm="U_S"), [])



class CompanyContextWebTests(unittest.TestCase):
    """The rendered panel, and the guest boundary on both paths that render
    a role: the pane route and the full Browse page. Both are open to guests."""

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
        db.conn.close()
        w = self.webapp
        self._saved = {k: getattr(w, k) for k in
                       ("DB_FILE", "WEB_USER", "WEB_PASSWORD",
                        "WEB_GUEST_USER", "WEB_GUEST_PASSWORD")}
        w.DB_FILE, w.WEB_USER, w.WEB_PASSWORD = self.path, "admin", "owner-pw-test"
        w.WEB_GUEST_USER, w.WEB_GUEST_PASSWORD = "guest", "guest-pw-test"

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.webapp, k, v)
        os.path.exists(self.path) and os.unlink(self.path)

    def _client(self, user, pw):
        c = self.TestClient(self.webapp.app, base_url="https://testserver")
        r = c.post("/login", data={"username": user, "password": pw},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return c

    def test_owner_pane_shows_history_and_relevant_roles(self):
        page = self._client("admin", "owner-pw-test").get("/job/cur?pane=1").text
        self.assertIn("Your applications at UBS", page)
        self.assertIn("Global Markets Analyst London", page)
        self.assertIn("Markets Programme Frankfurt", page)   # delisted history
        self.assertIn(">delisted</span>", page)
        self.assertIn("Relevant open roles (3)", page)
        self.assertIn("Markets Graduate Zurich 2", page)
        self.assertNotIn("IBD Graduate Zurich", page)          # not every opening
        self.assertIn('href="/?firm=UBS"', page)
        # The link's count is what Browse's default view shows for the firm.
        c = self._client("admin", "owner-pw-test")
        shown = c.get("/api/jobs?firm=UBS").json()["count"]
        self.assertIn(f"Show all {shown} open UBS roles", page)
        self.assertNotIn("Applied Elsewhere", page)            # Hubs Capital

    def test_guest_never_receives_the_panel(self):
        c = self._client("guest", "guest-pw-test")
        page = c.get("/?sel=cur").text
        self.assertNotIn("Your applications at", page)
        self.assertNotIn("cc-apps", page)
        self.assertNotIn("Relevant open roles", page)
        # The pane route is open to guests by design (Browse loads it over
        # htmx), so the boundary is server-side in _full_detail, not the route.
        r = c.get("/job/cur?pane=1", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Markets Graduate Programme Zurich", r.text)
        for leak in ("Your applications at", "cc-apps", "Relevant open roles",
                     "Global Markets Analyst London", "Markets Programme Frankfurt"):
            self.assertNotIn(leak, r.text)


if __name__ == "__main__":
    unittest.main()
