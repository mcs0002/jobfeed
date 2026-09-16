import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import JobDB
from scrapers.enrich import (
    DETAIL_ENRICHERS, detail_enricher, eib_enrich, guidecom_enrich,
    jibe_enrich, successfactors_enrich, uniper_enrich,
)


class UpgradeDescriptionTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd); os.unlink(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def _seed(self, jid, desc):
        self.db.mark_seen(jid, company="C", title="T", url="u")
        if desc is not None:
            self.db.set_description(jid, desc)

    def test_fills_null(self):
        self._seed("a", None)
        self.assertTrue(self.db.upgrade_description_if_better("a", "x" * 500))
        self.assertEqual(self.db.get_job("a")["description"], "x" * 500)

    def test_upgrades_stub(self):
        # A short JS-shell-title stub gets replaced by the full body.
        self._seed("b", "Data Analyst @ Kraken")
        self.assertTrue(self.db.upgrade_description_if_better("b", "y" * 4000))
        self.assertEqual(self.db.get_job("b")["description"], "y" * 4000)

    def test_never_shrinks_real_description(self):
        self._seed("c", "z" * 3000)  # already a real body (>=800)
        self.assertFalse(self.db.upgrade_description_if_better("c", "short"))
        self.assertEqual(self.db.get_job("c")["description"], "z" * 3000)

    def test_no_churn_when_not_longer(self):
        self._seed("d", "w" * 400)  # stub, but incoming isn't longer
        self.assertFalse(self.db.upgrade_description_if_better("d", "w" * 400))


class NewEnricherRoutingTests(unittest.TestCase):
    def test_uniper_matches_and_precedes_successfactors(self):
        url = "https://careers.uniper.energy/job/City-Title-(w/m/d)/89168"
        self.assertTrue(uniper_enrich.is_uniper(url))
        self.assertTrue(successfactors_enrich.is_successfactors(url))  # greedy
        # Registry order must resolve uniper first, not SF.
        self.assertIs(detail_enricher(url), uniper_enrich.description)
        mods = [is_fn.__module__ for is_fn, _ in DETAIL_ENRICHERS]
        self.assertLess(mods.index(uniper_enrich.is_uniper.__module__),
                        mods.index(successfactors_enrich.is_successfactors.__module__))

    def test_jibe_matches_both_hosts_not_lookalikes(self):
        self.assertTrue(jibe_enrich.is_jibe("https://careers.ice.com/jobs/12817"))
        self.assertTrue(jibe_enrich.is_jibe("https://careers.sig.com/jobs/10969"))
        self.assertFalse(jibe_enrich.is_jibe("https://careers.axpo.com/jobs/1"))

    def test_guidecom_and_eib_matchers(self):
        self.assertTrue(guidecom_enrich.is_guidecom(
            "https://connect.guidecom.de/jobportal/helaba/viewAusschreibung/2026-057.html"))
        self.assertFalse(guidecom_enrich.is_guidecom("https://helaba.com/x"))
        self.assertTrue(eib_enrich.is_eib(
            "https://erecruitment.eib.org/psc/hr/x.GBL?JobOpeningId=111402&PostingSeq=1"))
        self.assertFalse(eib_enrich.is_eib("https://erecruitment.eib.org/search"))


if __name__ == "__main__":
    unittest.main()
