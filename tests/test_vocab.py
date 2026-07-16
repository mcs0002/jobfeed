"""The non-redundancy contract: scrape-time scope must stay strictly more
permissive than filter.py's negative gates. If a scope term named something
filter.py drops, we'd be losing roles at scrape time that the web app should
have shown. Assert no front-office vocab term collides with a drop set."""
import unittest

import filter as jobfilter
import vocab


class VocabNonRedundancyTest(unittest.TestCase):
    def _drop_terms(self):
        terms = set()
        for name in ("BACK_OFFICE_DROPS", "TECH_DROPS", "NON_FINANCE_DROPS",
                     "LOCATION_DROPS"):
            terms |= {t.strip().lower() for t in getattr(jobfilter, name)}
        return terms

    def test_no_scope_term_is_a_drop(self):
        drops = self._drop_terms()
        scope = {t.strip().lower() for t in vocab.FRONT_OFFICE_KEYWORDS}
        collisions = scope & drops
        self.assertFalse(
            collisions,
            f"scope terms collide with filter.py drops: {sorted(collisions)}",
        )

    def test_vocab_is_non_empty(self):
        self.assertTrue(vocab.FRONT_OFFICE_KEYWORDS)


if __name__ == "__main__":
    unittest.main()
