import os
import tempfile
import unittest

from jobfeed.db import JobDB
from jobfeed.dedup import group_postings


def job(i, company="Example Bank", title="Client Associate", location="New York", **kw):
    return {"id": i, "company": company, "title": title, "location": location,
            "status": "new", "favorite": 0, **kw}


class GroupPostingsTests(unittest.TestCase):
    def test_campus_and_lateral_copies_collapse_into_one_card(self):
        out = group_postings([job("a", "PwC", "Statistician", "Makati"),
                              job("b", "PwC (Campus)", "Statistician", "Makati")])
        self.assertEqual([j["id"] for j in out], ["a"])
        self.assertEqual([j["id"] for j in out[0]["also"]], ["b"])

    def test_the_marked_posting_leads_its_group(self):
        out = group_postings([job("a"), job("b", status="ignored"), job("c")])
        self.assertEqual(out[0]["id"], "b")
        self.assertEqual({j["id"] for j in out[0]["also"]}, {"a", "c"})

    def test_different_place_or_title_stays_separate(self):
        out = group_postings([job("a"), job("b", location="Zurich"),
                              job("c", title="Client Advisor")])
        self.assertEqual(len(out), 3)
        self.assertTrue(all(not j["also"] for j in out))


class RepostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = JobDB(self.tmp.name)

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.tmp.name)

    def _old(self, i, company="Example Bank", status="ignored", title="M&A Analyst",
             location="Paris"):
        self.db.mark_seen(i, company=company, title=title, url="u", location=location)
        self.db.conn.execute("UPDATE seen_jobs SET status=?, delisted_at=datetime('now') "
                             "WHERE id=?", (status, i))

    def _row(self, i):
        status, repost_of = self.db.conn.execute(
            "SELECT status, repost_of FROM seen_jobs WHERE id=?", (i,)).fetchone()
        return {"status": status, "repost_of": repost_of}

    def test_repost_links_and_keeps_a_dismissal(self):
        self._old("old")
        self.db.mark_seen("new", company="Example Bank (Students)", title="M&A Analyst",
                          url="u", location="Paris")
        self.assertEqual(self._row("new"), {"status": "ignored", "repost_of": "old"})

    def test_an_application_is_linked_but_never_copied(self):
        self._old("old", status="applied")
        self.db.mark_seen("new", company="Example Bank", title="M&A Analyst", url="u",
                          location="Paris")
        self.assertEqual(self._row("new"), {"status": "new", "repost_of": "old"})

    def test_another_city_or_firm_is_not_a_repost(self):
        self._old("old")
        self.db.mark_seen("x", company="Example Bank", title="M&A Analyst", url="u",
                          location="London")
        self.db.mark_seen("y", company="Rothschild & Co", title="M&A Analyst", url="u",
                          location="Paris")
        self.assertIsNone(self._row("x")["repost_of"])
        self.assertIsNone(self._row("y")["repost_of"])

    def test_a_rescan_of_a_known_row_does_not_relink(self):
        self.db.mark_seen("live", company="Example Bank", title="M&A Analyst", url="u",
                          location="Paris")
        self._old("old")
        self.db.mark_seen("live", company="Example Bank", title="M&A Analyst", url="u",
                          location="Paris")
        self.assertIsNone(self._row("live")["repost_of"])

    def test_siblings_are_the_other_live_postings(self):
        for i, co in (("a", "PwC"), ("b", "PwC (Campus)"), ("c", "PwC")):
            self.db.mark_seen(i, company=co, title="Statistician", url=f"u{i}",
                              location="Makati")
        self.db.conn.execute("UPDATE seen_jobs SET delisted_at=datetime('now') WHERE id='c'")
        sibs = self.db.sibling_postings({"id": "a", "company": "PwC",
                                         "title": "Statistician", "location": "Makati"})
        self.assertEqual([s["id"] for s in sibs], ["b"])


if __name__ == "__main__":
    unittest.main()
