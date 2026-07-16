"""Tests for scrape_company() dispatch in main.py.

Focuses on the unrecognized-ats guard (Bug 3): a typo'd or renamed ats value
in targets.json must surface as an error so the company is excluded from the
delist pass, not treated as a cleanly-scraped empty board.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from main import scrape_company, _write_health_state


class ScrapeCompanyDispatchTests(unittest.TestCase):
    def test_unknown_ats_returns_no_error(self):
        """ats='unknown' is the deliberate research-candidate convention — the
        company is silently skipped with ([], None), NOT an error (main() also
        filters these out before dispatch, so this is belt-and-suspenders)."""
        jobs, err = scrape_company({"name": "Research Co", "ats": "unknown"})
        self.assertEqual(jobs, [])
        self.assertIsNone(err)

    def test_unrecognized_ats_returns_config_error(self):
        """A typo'd or stale ats value (not in HANDLERS, not 'unknown' or
        'manual') must return a ConfigError so the company lands in `errors`
        and is excluded from the delist pass. Before this fix it returned
        ([], None), which was indistinguishable from a cleanly-scraped empty
        board and caused mass-delisting of every stored row for that firm."""
        jobs, err = scrape_company({"name": "Broken Co", "ats": "not_a_real_ats_xyz"})
        self.assertEqual(jobs, [])
        self.assertIsNotNone(err)
        self.assertIn("ConfigError", err)
        self.assertIn("not_a_real_ats_xyz", err)

    def test_recognized_ats_raises_into_error_not_config_error(self):
        """A real handler that raises (e.g. network error) still returns an
        error tuple (caught by scrape_company's except clause), but the error
        message is the exception class, not ConfigError."""
        # We use a real but trivially-broken config to trigger a handler error.
        # 'workday' is a known ats but requires a url_template; it will raise.
        jobs, err = scrape_company({"name": "Broken Workday", "ats": "workday"})
        self.assertEqual(jobs, [])
        self.assertIsNotNone(err)
        self.assertNotIn("ConfigError", err)


class CleanZeroDegradedTests(unittest.TestCase):
    """C1: a previously-productive firm that returns 0 on a clean scrape (no
    error — e.g. selector rot returning [] silently) must be flagged degraded so
    the delist pass skips it, EVEN when its baseline never reached
    HEALTH_MIN_BASELINE (small boards of 2-4 roles never do). Otherwise
    find_delistable reports every stored id as missing and the 'other' purge
    hard-deletes the firm's whole corpus a few days later."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._orig_root = main.ROOT
        main.ROOT = self.tmp
        self.addCleanup(lambda: setattr(main, "ROOT", self._orig_root))

    def _seed_baseline(self, baseline: dict):
        path = os.path.join(self.tmp, "verify_state.json")
        with open(path, "w") as f:
            json.dump({"baseline": baseline}, f)

    def test_small_baseline_clean_zero_is_degraded(self):
        # SmallBoard peaked at 3 (below the 5 floor) then returned 0 today.
        self._seed_baseline({"SmallBoard": 3})
        degraded = _write_health_state({"SmallBoard": 0}, error_names=set())
        self.assertIn("SmallBoard", degraded)

    def test_never_productive_zero_is_not_degraded(self):
        # A firm with no prior baseline (never produced a role) returning 0 is
        # NOT degraded — there's nothing to protect and no collapse to flag.
        self._seed_baseline({})
        degraded = _write_health_state({"BrandNew": 0}, error_names=set())
        self.assertNotIn("BrandNew", degraded)

    def test_productive_nonzero_below_floor_not_degraded(self):
        # Still producing (2 of a 3 baseline) — not a collapse, not zero.
        self._seed_baseline({"SmallBoard": 3})
        degraded = _write_health_state({"SmallBoard": 2}, error_names=set())
        self.assertNotIn("SmallBoard", degraded)

    def test_large_baseline_collapse_still_degraded(self):
        # The original baseline-ratio path is unchanged: a big board collapsing
        # to near-zero is still degraded.
        self._seed_baseline({"BigBoard": 100})
        degraded = _write_health_state({"BigBoard": 3}, error_names=set())
        self.assertIn("BigBoard", degraded)


if __name__ == "__main__":
    unittest.main()
