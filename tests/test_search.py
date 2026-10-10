"""Browse free-text search (jobfeed/search.py): FTS5 over title, company,
location, city and posting text, with city aliases from tag._CITY_CANON.

The box it replaces was removed because "Munich" missed "München"; the alias
tests here are the reason it can come back.
"""
import os
import sqlite3
import tempfile
import unittest

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_GUEST_PASSWORD", "guest-pw-test")
os.environ.setdefault("WEB_GUEST_USER", "guest")
os.environ.setdefault("WEB_SECRET", "application-workflow-test-secret")

from starlette.testclient import TestClient  # noqa: E402

import web.app as webapp  # noqa: E402
from jobfeed import search  # noqa: E402
from jobfeed.db import JobDB  # noqa: E402
from scripts import export_shared  # noqa: E402

MALFORMED = ['"', '""', '(', ')', 'AND', 'OR NOT', 'NEAR(a b', '-', '--', '*', '^x',
             '"unclosed phrase', 'a"b"c', "'; DROP TABLE seen_jobs; --", 'title:x',
             '{a b}', '-"', '- -', '\\', '%', '_', "a" * 500, 'NOT', '+', '😀']


def seed(db: JobDB) -> None:
    db.mark_seen("de1", company="Bank A", title="Markets Analyst", url="u1",
                 location="München, Bayern", description="Join our sales desk.")
    db.mark_seen("de2", company="Bank B", title="Graduate Trader", url="u2",
                 location="Munich, Germany", description="Rates trading.")
    db.mark_seen("de3", company="Bank C", title="Quantitative Analyst", url="u3",
                 location="Bavaria", description="A quant role.")
    db.set_tags("de3", area="quant", loc_city="Munich")
    db.mark_seen("de4", company="Bank D", title="Credit Analyst", url="u4",
                 location="Remote", description="Our team sits in Muenchen and Paris.")
    db.mark_seen("lon", company="Bank E", title="Wealth Management Analyst", url="u5",
                 location="London", description="Private wealth for families.")
    db.mark_seen("par", company="Bank F", title="Equity Research Analyst", url="u6",
                 location="Paris", description="Cover European banks; Bloomberg terminal.")
    for jid in ("de1", "de2", "de4", "lon", "par"):
        db.set_tags(jid, area="markets")


def ids(rows) -> set:
    return {r["id"] for r in rows}


class _DBCase(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.db = JobDB(self.path)
        seed(self.db)

    def tearDown(self):
        self.db.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def build(self):
        search.backfill(self.db.conn, pause=0, log=None)
        self.db._search_ready = None

    def q(self, text):
        return ids(self.db.fetch_jobs(q=text))


class IndexedSearchTests(_DBCase):
    def setUp(self):
        super().setUp()
        self.build()

    def test_every_spelling_of_munich_finds_the_same_rows(self):
        expected = {"de1", "de2", "de3", "de4"}
        for spelling in ("Munich", "München", "Muenchen", "munchen", "MÜNCHEN"):
            self.assertEqual(expected, self.q(spelling), spelling)

    def test_a_description_only_word_is_found(self):
        self.assertEqual({"par"}, self.q("bloomberg"))

    def test_words_and_together_and_match_prefixes(self):
        self.assertEqual({"de3"}, self.q("quant analyst"))
        self.assertEqual({"de3"}, self.q("quantitative"))

    def test_quoted_phrase_and_exclusion(self):
        self.assertEqual({"lon"}, self.q('"wealth management"'))
        analysts = {"de1", "de3", "de4", "lon", "par"}
        self.assertEqual(analysts - {"lon"}, self.q("analyst -wealth"))
        self.assertEqual(analysts - {"lon", "de4"}, self.q('analyst -wealth -"credit analyst"'))
        # An excluded city excludes every spelling of it.
        self.assertEqual({"par"}, self.q("-wealth -sales -muenchen"))

    def test_malformed_input_never_errors(self):
        for text in MALFORMED:
            with self.subTest(text=text):
                self.db.fetch_jobs(q=text)
                self.db.area_counts(q=text)
                self.db.distinct_scoped("loc_city", {"q": text})

    def test_punctuation_only_matches_nothing_rather_than_everything(self):
        self.assertEqual(set(), self.q('"*"'))

    def test_triggers_follow_every_write(self):
        self.db.conn.execute("UPDATE seen_jobs SET title='Structurer' WHERE id='par'")
        self.assertEqual({"par"}, self.q("structurer"))
        self.assertNotIn("par", self.q("equity"))
        self.db.set_description("lon", "Hedge fund sales in Zürich.")
        self.assertEqual({"lon"}, self.q("zurich"))
        self.assertNotIn("lon", self.q("families"))
        self.db.mark_seen("new1", company="Bank G", title="Analyst", url="u7",
                          location="Frankfurt am Main", description="FX options.")
        self.assertEqual({"new1"}, self.q("frankfurt fx"))
        self.db.conn.execute("DELETE FROM seen_jobs WHERE id='new1'")
        self.assertEqual(set(), self.q("frankfurt"))
        # rescope_ids moves a job to a new id; the index follows it.
        self.db.conn.execute("UPDATE seen_jobs SET id='de2b' WHERE id='de2'")
        self.db.conn.execute("UPDATE job_descriptions SET job_id='de2b' WHERE job_id='de2'")
        self.assertEqual({"de2b"}, self.q("rates"))
        n_ids = self.db.conn.execute("SELECT count(*) FROM job_search_ids").fetchone()[0]
        n_jobs = self.db.conn.execute("SELECT count(*) FROM seen_jobs").fetchone()[0]
        self.assertEqual(n_jobs, n_ids)

    def test_backfill_is_resumable_and_never_double_indexes(self):
        self.assertEqual(0, search.backfill(self.db.conn, pause=0, log=None))
        self.assertEqual({"par"}, self.q("bloomberg"))


class FallbackTests(_DBCase):
    def test_before_the_build_location_and_aliases_still_match(self):
        self.assertFalse(self.db.search_index_ready())
        self.assertEqual({"de1", "de2", "de3"}, self.q("Muenchen"))
        self.assertEqual({"de1", "de2", "de3"}, self.q("Munich"))
        # The description is the index's job; a half-built index never answers.
        self.assertEqual(set(), self.q("bloomberg"))
        for text in MALFORMED:
            self.db.fetch_jobs(q=text)

    def test_backfill_on_a_db_that_predates_the_index(self):
        con = self.db.conn
        for name in ("job_search_ai", "job_search_au", "job_search_ad",
                     "job_search_dai", "job_search_dau", "job_search_dad"):
            con.execute(f"DROP TRIGGER {name}")
        con.execute("DROP TABLE job_search")
        con.execute("DROP TABLE job_search_ids")
        con.commit()
        self.build()
        self.assertEqual({"de1", "de2", "de3", "de4"}, self.q("München"))


class WebTests(unittest.TestCase):
    def setUp(self):
        self._saved = (webapp.WEB_PASSWORD, webapp.WEB_USER,
                       webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER, webapp.DB_FILE)
        webapp.WEB_PASSWORD, webapp.WEB_USER = "owner-pw-test", "admin"
        webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER = "guest-pw-test", "guest"
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        webapp.DB_FILE = self.path
        db = JobDB(self.path)
        seed(db)
        db.set_status("de1", "applied")
        db.set_favorite("de2", True) if hasattr(db, "set_favorite") else None
        search.backfill(db.conn, pause=0, log=None)
        db.conn.close()

    def tearDown(self):
        (webapp.WEB_PASSWORD, webapp.WEB_USER, webapp.WEB_GUEST_PASSWORD,
         webapp.WEB_GUEST_USER, webapp.DB_FILE) = self._saved
        os.unlink(self.path)

    def client(self, user, pw):
        c = TestClient(webapp.app, base_url="https://testserver")
        c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
        return c

    def test_browse_searches_and_shows_the_active_search_as_a_chip(self):
        c = self.client(webapp.WEB_USER, webapp.WEB_PASSWORD)
        r = c.get("/", params={"q": "Muenchen"})
        self.assertEqual(200, r.status_code)
        self.assertIn("Search: Muenchen", r.text)
        self.assertIn('id="text-search"', r.text)
        self.assertIn("Markets Analyst", r.text)
        self.assertNotIn("Equity Research Analyst", r.text)
        part = c.get("/partials/jobs", params={"q": "bloomberg"})
        self.assertIn("Equity Research Analyst", part.text)
        self.assertNotIn("Markets Analyst", part.text)

    def test_the_remembered_browse_link_keeps_the_search(self):
        # base.html used to strip q from the Browse nav link while the box
        # was gone; with it back, that would silently drop an active search.
        base = open(os.path.join(os.path.dirname(webapp.__file__),
                                 "templates", "base.html")).read()
        self.assertNotIn('searchParams.delete("q")', base)

    def test_malformed_queries_do_not_500(self):
        c = self.client(webapp.WEB_USER, webapp.WEB_PASSWORD)
        for text in MALFORMED:
            with self.subTest(text=text):
                self.assertEqual(200, c.get("/partials/jobs", params={"q": text}).status_code)
                self.assertEqual(200, c.get("/", params={"q": text}).status_code)

    def test_guest_may_search_but_sees_no_owner_state(self):
        c = self.client(webapp.WEB_GUEST_USER, webapp.WEB_GUEST_PASSWORD)
        r = c.get("/", params={"q": "München"})
        self.assertEqual(200, r.status_code)
        self.assertIn("Search: München", r.text)
        self.assertIn("Markets Analyst", r.text)
        # A status filter rides along on a guest link: stripped, never applied.
        r = c.get("/", params={"q": "analyst", "status": "applied"}, follow_redirects=False)
        self.assertEqual(303, r.status_code)
        self.assertNotIn("status", r.headers["location"])
        self.assertIn("q=analyst", r.headers["location"])
        part = c.get("/partials/jobs", params={"q": "analyst", "status": "applied"})
        self.assertIn("Equity Research Analyst", part.text)
        self.assertNotIn("Status: applied", part.text)


class SharedExportTests(unittest.TestCase):
    def test_export_is_unaffected_by_the_index(self):
        src = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        dest = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        os.unlink(dest)
        try:
            db = JobDB(src)
            seed(db)
            search.backfill(db.conn, pause=0, log=None)
            db.conn.close()
            manifest = export_shared.export(src, dest, with_descriptions=True)
            export_shared.verify(dest)
            self.assertEqual(6, manifest["rows"])
            con = sqlite3.connect(dest)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
            con.close()
            self.assertFalse({t for t in tables if t.startswith("job_search")})
            self.assertNotIn("job_search", manifest["columns"])
        finally:
            for p in (src, dest):
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.unlink(p + suffix)
                    except FileNotFoundError:
                        pass


if __name__ == "__main__":
    unittest.main()
