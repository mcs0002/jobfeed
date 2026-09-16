import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scrapers.enrich import coverage


class EnrichCoverageTests(unittest.TestCase):
    """Completeness guard: every ATS we actively scrape must declare where its
    job body comes from. This is what would have caught the TAL.net listing-only
    scraper shipping stub descriptions — a new source with no description path
    fails here instead of silently mis-tagging."""

    def _target_ats(self):
        with open(os.path.join(ROOT, "targets.json")) as f:
            targets = json.load(f)
        return {t.get("ats", "unknown") for t in targets}

    def test_every_scraped_ats_has_a_declared_strategy(self):
        missing = coverage.undeclared(self._target_ats())
        self.assertEqual(
            missing, set(),
            f"ATS with no declared description strategy (add them to "
            f"scrapers/enrich/coverage.DESCRIPTION_STRATEGY): {sorted(missing)}",
        )

    def test_no_stale_strategy_entries(self):
        # Every declared ATS should still exist in targets.json (or be a known
        # variant) — keeps the registry from rotting as sources are removed.
        declared = set(coverage.DESCRIPTION_STRATEGY)
        live = self._target_ats()
        stale = declared - live
        self.assertEqual(
            stale, set(),
            f"coverage.DESCRIPTION_STRATEGY lists ATS no longer in targets.json: "
            f"{sorted(stale)}",
        )

    def test_strategy_values_are_valid(self):
        valid = {coverage.ENRICHER, coverage.SCRAPER, coverage.HTTP, coverage.NONE}
        bad = {a: s for a, s in coverage.DESCRIPTION_STRATEGY.items() if s not in valid}
        self.assertEqual(bad, {}, f"invalid strategy labels: {bad}")


if __name__ == "__main__":
    unittest.main()
