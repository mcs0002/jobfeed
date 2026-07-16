"""The --patch-desc-lang retro-sweep: unions the detected posting language into
lang_req on already-tagged rows; leaves NULL (v2-untagged) rows alone."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import JobDB
from backfill_tags import patch_desc_lang
from tests.test_lang_detect import GERMAN, ENGLISH


class PatchDescLangTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = JobDB(self.tmp.name)
        rows = [
            # German ad, tagged '' → must gain 'de'.
            ("g1", GERMAN, ""),
            # German ad already tagged fr → union to 'de,fr'.
            ("g2", GERMAN, "fr"),
            # German ad, lang_req NULL (v2-untagged) → left for the LLM path.
            ("g3", GERMAN, None),
            # English ad tagged '' → untouched.
            ("e1", ENGLISH, ""),
        ]
        for jid, desc, lang in rows:
            self.db.conn.execute(
                "INSERT INTO seen_jobs (id, company, title, url, first_seen,"
                " last_seen, description, lang_req)"
                " VALUES (?, 'TestCo', 'Analyst', ?, '2026-07-01',"
                " '2026-07-01', ?, ?)",
                (jid, f"https://x.test/{jid}", desc, lang),
            )
        self.db.conn.commit()

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.tmp.name)

    def _lang(self, jid):
        return self.db.conn.execute(
            "SELECT lang_req FROM seen_jobs WHERE id = ?", (jid,)).fetchone()[0]

    def test_sweep(self):
        patched = patch_desc_lang(self.db, verbose=False)
        self.assertEqual(patched, 2)
        self.assertEqual(self._lang("g1"), "de")
        self.assertEqual(self._lang("g2"), "de,fr")
        self.assertIsNone(self._lang("g3"))
        self.assertEqual(self._lang("e1"), "")

    def test_idempotent(self):
        patch_desc_lang(self.db, verbose=False)
        self.assertEqual(patch_desc_lang(self.db, verbose=False), 0)


if __name__ == "__main__":
    unittest.main()
