"""DB-level tests for the Start-date filter and the lang_req 'none'
("English only") branch, plus the start_years() dropdown source.

NULL vs '' semantics are load-bearing for these facets: NULL = never tagged
with a description present, '' = tagged and genuinely none stated. An active
start / English-only filter excludes NULL rows (untagged) by design.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import JobDB


def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


class StartLangFilterTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)
        rows = [
            # id, start_date, lang_req
            ("j-2026", "2026", ""),
            ("j-2026-09", "2026-09", None),
            ("j-2027", "2027", "de"),
            ("j-asap", "asap", "de,fr"),
            ("j-blank", "", ""),
            ("j-null", None, None),
        ]
        for jid, start, lang in rows:
            self.db.conn.execute(
                "INSERT INTO seen_jobs (id, company, title, url, first_seen, "
                "last_seen, status, start_date, lang_req) "
                "VALUES (?, 'TestCo', ?, ?, '2026-07-01', '2026-07-01', 'new', ?, ?)",
                (jid, f"Analyst {jid}", f"https://x.test/{jid}", start, lang),
            )
        self.db.conn.commit()

    def ids(self, **kw):
        return {j["id"] for j in self.db.fetch_jobs(**kw)}

    # ── start filter ────────────────────────────────────────────────────────
    def test_start_year_matches_by_prefix(self):
        # '2026' covers both the bare year and the YYYY-MM refinement.
        self.assertEqual(self.ids(start="2026"), {"j-2026", "j-2026-09"})

    def test_start_asap_matches_exactly(self):
        self.assertEqual(self.ids(start="asap"), {"j-asap"})

    def test_start_filter_excludes_null_and_blank(self):
        for start in ("2026", "2027", "asap"):
            got = self.ids(start=start)
            self.assertNotIn("j-null", got)
            self.assertNotIn("j-blank", got)

    def test_no_start_filter_keeps_untagged_rows(self):
        got = self.ids()
        self.assertIn("j-null", got)
        self.assertIn("j-blank", got)

    # ── lang_req 'none' (English only) ─────────────────────────────────────
    def test_lang_none_matches_only_empty_string(self):
        # '' = tagged, no extra language. NULL (untagged) is excluded.
        self.assertEqual(self.ids(lang_req="none"), {"j-2026", "j-blank"})

    def test_lang_code_still_token_matches_multivalue(self):
        # The existing behaviour must survive the 'none' special case.
        self.assertEqual(self.ids(lang_req="de"), {"j-2027", "j-asap"})
        self.assertEqual(self.ids(lang_req="fr"), {"j-asap"})

    # ── start_years dropdown source ─────────────────────────────────────────
    def test_start_years_distinct_sorted_numeric_only(self):
        # 'asap', '' and NULL contribute nothing; 2026/2026-09 collapse to 2026.
        self.assertEqual(self.db.start_years(), ["2026", "2027"])

    # ── scoped facets + tab counts accept the new params ────────────────────
    def test_distinct_scoped_honours_start_and_english_only(self):
        self.db.conn.execute(
            "UPDATE seen_jobs SET loc_city = id")  # give each row a facet value
        self.db.conn.commit()
        self.assertEqual(self.db.distinct_scoped("loc_city", {"start": "2027"}),
                         ["j-2027"])
        self.assertEqual(self.db.distinct_scoped("loc_city", {"start": "asap"}),
                         ["j-asap"])
        self.assertEqual(
            self.db.distinct_scoped("loc_city", {"lang_req": "none"}),
            ["j-2026", "j-blank"])

    def test_area_counts_accepts_secondary_facets(self):
        # Regression: /?education=… used to 500 because area_counts didn't
        # accept the newest facet kwargs; start joins the same path.
        self.db.conn.execute("UPDATE seen_jobs SET area = 'markets'")
        self.db.conn.commit()
        self.assertEqual(self.db.area_counts(start="2026"), {"markets": 2})
        self.assertEqual(self.db.area_counts(start="asap"), {"markets": 1})
        self.assertEqual(self.db.area_counts(lang_req="none"), {"markets": 2})
        self.assertEqual(self.db.area_counts(lang_req="de"), {"markets": 2})
        self.assertEqual(self.db.area_counts(education="master"), {})


if __name__ == "__main__":
    unittest.main()
