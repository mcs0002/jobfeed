"""Tests for scrape_company() dispatch in main.py.

Focuses on the unrecognized-ats guard (Bug 3): a typo'd or renamed ats value
in targets.json must surface as an error so the company is excluded from the
delist pass, not treated as a cleanly-scraped empty board.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobfeed import main
from jobfeed.db import JobDB
from jobfeed.main import scrape_company, _select_targets, _write_health_state


class TargetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.targets = [
            {"name": "Alpha", "ats": "greenhouse"},
            {"name": "Beta", "ats": "workday"},
            {"name": "Gamma", "ats": "lever"},
        ]

    def test_no_names_keeps_full_target_list(self):
        self.assertIs(_select_targets(self.targets, []), self.targets)

    def test_exact_names_are_repeatable_and_preserve_registry_order(self):
        selected = _select_targets(self.targets, ["Gamma", "Alpha", "Gamma"])
        self.assertEqual([target["name"] for target in selected], ["Alpha", "Gamma"])

    def test_unknown_name_fails_loud(self):
        with self.assertRaisesRegex(ValueError, "unknown company: Missing"):
            _select_targets(self.targets, ["Missing"])


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

    def test_filtered_counts_are_persisted_for_stats_funnel(self):
        self._seed_baseline({})
        _write_health_state(
            {"Alpha": 10}, error_names=set(), unique_counts={"Alpha": 9},
            filtered_counts={"Alpha": 6},
        )
        with open(os.path.join(self.tmp, "verify_state.json")) as f:
            state = json.load(f)
        self.assertEqual(state["last_filtered_counts"], {"Alpha": 6})


class ScanIdentityIntegrationTests(unittest.TestCase):
    """Pin the scan-loop interactions between URL dedup and delisting."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))
        self.db_path = os.path.join(self.tmp, "jobs.db")
        self.company = {
            "name": "Example Co", "ats": "greenhouse", "slug": "example",
            "category": "Banks", "verified": True,
        }

    def _run(self, jobs):
        result = [(self.company, jobs, None)]
        with patch.object(main, "DB_FILE", self.db_path), \
             patch.object(main, "load_targets", return_value=[self.company]), \
             patch.object(main, "scrape_targets", return_value=result), \
             patch.object(main, "scrape_heavy_targets", return_value=[]), \
             patch.object(main, "_enrich_new_jobs"), \
             patch.object(main, "_write_health_state", return_value=set()), \
             patch.object(sys, "argv", ["main.py", "--no-tag"]):
            main.main()

    def test_churned_existing_id_is_not_delisted(self):
        db = JobDB(self.db_path)
        db.mark_seen("old-id", company="Example Co", title="Quant Analyst",
                     url="https://example.com/jobs/1", description="body")
        db.conn.close()

        self._run([{
            "id": "fresh-id", "title": "Quant Analyst",
            "url": "https://example.com/jobs/1", "location": "London",
        }])

        db = JobDB(self.db_path)
        self.assertEqual(db.total_seen(), 1)
        self.assertIsNone(db.get_job("old-id")["delisted_at"])
        self.assertFalse(db.seen("fresh-id"))
        db.conn.close()

    def test_shared_board_url_does_not_collapse_distinct_roles(self):
        board = "https://example.com/careers"
        self._run([
            {"id": "one", "title": "Quant Analyst", "url": board,
             "location": "London"},
            {"id": "two", "title": "Portfolio Analyst", "url": board,
             "location": "London"},
        ])
        db = JobDB(self.db_path)
        self.assertEqual(db.total_seen(), 2)
        self.assertEqual(len(db.fetch_jobs(company="Example Co")), 2)
        db.conn.close()


if __name__ == "__main__":
    unittest.main()


class SkippedDelistReportTests(unittest.TestCase):
    """An empty board and a collapsed scraper are both skipped by the delist
    pass, but only one is a job to do. Before 2026-09-16 the run printed them
    as one list of 17 'degraded/zero' names, eleven of which were boutiques
    whose own ATS reported zero — so a real collapse had nowhere to stand out."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        Path(self.dir, "verify_state.json").write_text(json.dumps({
            "empty_runs": {"Taula Capital": 60, "First Sentier Investors": 8},
            "baseline": {"Taula Capital": 3, "First Sentier Investors": 40,
                         "Big Bank": 200},
        }))

    def _report(self, skipped, zero_now, raw_counts):
        buf = io.StringIO()
        with patch.object(main, "ROOT", self.dir), redirect_stdout(buf):
            main._report_skipped_delists(skipped, zero_now, raw_counts)
        return buf.getvalue()

    def test_empty_boards_carry_how_long_they_have_been_empty(self):
        out = self._report({"Taula Capital", "First Sentier Investors"},
                           {"Taula Capital", "First Sentier Investors"},
                           {"Taula Capital": 0, "First Sentier Investors": 0})
        self.assertIn("2 empty board(s)", out)
        self.assertIn("Taula Capital (60 run(s) empty, best 3)", out)
        self.assertIn("First Sentier Investors (8 run(s) empty, best 40)", out)
        self.assertNotIn("collapsed", out)

    def test_a_collapse_is_reported_separately_with_its_drop(self):
        out = self._report({"Big Bank"}, set(), {"Big Bank": 4})
        self.assertIn("1 collapsed source(s): Big Bank (4 of 200)", out)
        self.assertNotIn("empty board", out)

    def test_both_kinds_in_one_run_are_two_lines(self):
        out = self._report({"Taula Capital", "Big Bank"}, {"Taula Capital"},
                           {"Taula Capital": 0, "Big Bank": 4})
        self.assertIn("1 empty board(s)", out)
        self.assertIn("1 collapsed source(s)", out)
        self.assertEqual(2, len([l for l in out.splitlines() if l.strip()]))

    def test_a_missing_state_file_still_reports(self):
        Path(self.dir, "verify_state.json").unlink()
        out = self._report({"New Firm"}, {"New Firm"}, {"New Firm": 0})
        self.assertIn("New Firm (1 run(s) empty, best 0)", out)


class ScrapeAllTests(unittest.TestCase):
    """Heavy boards run alongside the light pool, not after it."""

    LIGHT = [{"name": "L1"}, {"name": "L2"}]
    HEAVY = [{"name": "H1", "heavy": True}]

    def test_heavy_starts_before_the_light_pool_finishes(self):
        import threading
        heavy_started = threading.Event()

        def light(targets, workers=6):
            # Blocks until the heavy side has started; a sequential
            # implementation would time out here.
            self.assertTrue(heavy_started.wait(5), "heavy did not overlap light")
            return [(c, [], None) for c in targets]

        def heavy(targets):
            heavy_started.set()
            return [(c, [{"id": "h"}], None) for c in targets]

        with patch.object(main, "scrape_targets", light), \
             patch.object(main, "scrape_heavy_targets", heavy):
            out = main.scrape_all(self.LIGHT + self.HEAVY)
        self.assertEqual([c["name"] for c, _, _ in out], ["L1", "L2", "H1"])
        self.assertEqual(out[2][1], [{"id": "h"}])

    def test_heavy_crash_becomes_per_company_errors(self):
        def heavy(targets):
            raise OSError("fork failed")

        with patch.object(main, "scrape_targets",
                          lambda t, workers=6: [(c, [], None) for c in t]), \
             patch.object(main, "scrape_heavy_targets", heavy):
            out = main.scrape_all(self.LIGHT + self.HEAVY)
        self.assertEqual([e for _, _, e in out[:2]], [None, None])
        self.assertEqual(out[2][2], "OSError: fork failed")
