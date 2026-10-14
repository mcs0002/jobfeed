"""Placeholder pages served as descriptions are never stored (2026-10-14)."""
import os
import tempfile
import unittest

from jobfeed.db import JobDB
from jobfeed.description_junk import is_junk

BOT = ("Quick Check Needed Quick Check Needed We just need to confirm you're a real "
       "person. Please check the box below and then")
LANG_NOTICE = ("The description of this offer is only available in French/Dutch. To view the "
       "description, please adjust the preferred language")
REAL = "Analyst, Rates Trading. " + "You will price and hedge interest rate swaps. " * 40


class IsJunk(unittest.TestCase):
    def test_placeholders(self):
        for text in (BOT, LANG_NOTICE, "Sorry, this position has been filled.",
                     "You can't view this job because it's not available at this time.",
                     "Erreur - Crédit Agricole CIB - Site d'offres d'emploi  DE EN FR",
                     "Le descriptif de cette offre n’est disponible qu'en anglais."):
            self.assertTrue(is_junk(text), text)

    def test_real_text_and_notice_with_body_pass(self):
        self.assertFalse(is_junk(REAL))
        self.assertFalse(is_junk(""))
        self.assertFalse(is_junk(None))
        self.assertFalse(is_junk("Please note this job advertisement is only available "
                                 "in Chinese  在这一职位上，您将： Dealing with cash processing"))
        # A long posting that happens to quote a phrase is still a posting.
        self.assertFalse(is_junk(REAL + " This position has been filled before."))


class WriteGuards(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.db = JobDB(self.path)

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.path)

    def desc(self, jid):
        row = self.db.conn.execute(
            "SELECT description FROM job_descriptions WHERE job_id = ?", (jid,)).fetchone()
        return row[0] if row else None

    def test_mark_seen_skips_junk(self):
        self.db.mark_seen("a", company="X", title="t", url="https://x.test/a", description=BOT)
        self.assertIsNone(self.desc("a"))

    def test_set_description_keeps_old_and_backs_off(self):
        self.db.mark_seen("b", company="X", title="t", url="https://x.test/b")
        self.db.set_description("b", LANG_NOTICE)
        self.assertIsNone(self.desc("b"))
        self.assertEqual(self.db.conn.execute(
            "SELECT attempts FROM description_attempts WHERE job_id = 'b'").fetchone()[0], 1)
        self.db.set_description("b", REAL)
        self.assertEqual(self.desc("b"), REAL)

    def test_fill_and_upgrade_skip_junk(self):
        self.db.mark_seen("c", company="X", title="t", url="https://x.test/c")
        self.assertFalse(self.db.fill_description_if_missing("c", BOT))
        self.assertFalse(self.db.upgrade_description_if_better("c", BOT))
        self.assertIsNone(self.desc("c"))

    def test_health_counts_junk_as_missing(self):
        self.db.mark_seen("f", company="X", title="t", url="https://x.test/f", description=REAL)
        self.db.mark_seen("t", company="X", title="t", url="https://x.test/t", description="Stub")
        self.db.mark_seen("m", company="Y", title="t", url="https://x.test/m")
        self.db.conn.execute("INSERT INTO job_descriptions (job_id, description) VALUES ('m', ?)", (BOT,))
        today = self.db.description_health_daily(3)[-1]
        self.assertEqual((today["full"], today["thin"], today["missing"]), (1, 1, 1))
        gaps = {r["company"]: r for r in self.db.description_gaps_by_source(14)}
        self.assertEqual((gaps["X"]["thin"], gaps["X"]["missing"], gaps["X"]["n"]), (1, 0, 2))
        self.assertEqual(gaps["Y"]["missing"], 1)


if __name__ == "__main__":
    unittest.main()
