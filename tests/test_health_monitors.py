"""Unit tests for the DB-side quality monitors (tag_health, link_health).

Network and live-DB access are out of scope here: these pin the pure
aggregation/threshold logic so the selfcheck wiring can trust it.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import link_health
import tag_health


def _tmp_db(rows):
    """rows: (company, area, url, delisted_at) tuples -> path to a seed DB."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = sqlite3.connect(f.name)
    conn.execute(
        "CREATE TABLE seen_jobs (company TEXT, area TEXT, url TEXT, "
        "delisted_at TEXT)")
    conn.executemany("INSERT INTO seen_jobs VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return f.name


class TagHealthTest(unittest.TestCase):
    def test_other_heavy_thresholds(self):
        rows = (
            # PIC-style: 5 tagged, all other -> flagged
            [("PIC", "other", "http://x", None)] * 5
            # Below MIN_TAGGED: 4 all-other -> not flagged
            + [("Tiny", "other", "http://x", None)] * 4
            # 80% exactly (4/5) -> flagged (>= ratio)
            + [("Edge", "other", "http://x", None)] * 4
            + [("Edge", "ibd", "http://x", None)]
            # Healthy mix -> not flagged
            + [("Bank", "ibd", "http://x", None)] * 3
            + [("Bank", "other", "http://x", None)]
            # Untagged rows must be ignored entirely
            + [("Blank", None, "http://x", None)] * 10
            + [("Blank", "", "http://x", None)] * 10
            # Delisted rows must be ignored
            + [("Gone", "other", "http://x", "2026-01-01")] * 10
        )
        db = _tmp_db(rows)
        heavy = {h["company"] for h in tag_health.other_heavy_sources(db)}
        self.assertEqual(heavy, {"PIC", "Edge"})

    def test_share_other(self):
        self.assertEqual(tag_health.share_other([]), 0.0)
        self.assertEqual(tag_health.share_other(["other", "ibd"]), 0.5)


class LinkHealthTest(unittest.TestCase):
    def test_dead_statuses(self):
        self.assertTrue(link_health.is_dead(404))
        self.assertTrue(link_health.is_dead(410))
        # Inconclusive is NOT dead: bot walls, server trouble, transport errors.
        for status in (200, 301, 403, 500, 503, None):
            self.assertFalse(link_health.is_dead(status))

    def test_company_verdicts_all_samples_must_die(self):
        verdicts = link_health.company_verdicts({
            "AllDead": [("u1", 404), ("u2", 410)],
            "HalfDead": [("u1", 404), ("u2", 200)],   # same-day removal case
            "BotWalled": [("u1", 403), ("u2", 403)],
            "NoSamples": [],
        })
        self.assertEqual(
            verdicts,
            {"AllDead": True, "HalfDead": False, "BotWalled": False,
             "NoSamples": False})

    def test_streaks_advance_and_reset(self):
        prev = {"A": 1, "B": 3, "C": 2}
        # A dead again -> 2; B recovered -> dropped; C absent (no live rows)
        # -> dropped; D newly dead -> 1.
        nxt = link_health.update_streaks(
            prev, {"A": True, "B": False, "D": True})
        self.assertEqual(nxt, {"A": 2, "D": 1})

    def test_all_none_run_leaves_streak_state_untouched(self):
        """BUG 5b: when every sampled fetch returns None the run is globally
        inconclusive. Streak state must not be updated and no 'alive' verdicts
        must reset accumulated dead-streaks."""
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "link_state.json"
            # Seed an existing streak so we can confirm it's preserved.
            state_path.write_text(json.dumps({"streaks": {"BrokenBank": 1}}))

            # Patch out network calls: two companies, both fetches return None.
            samples_fixture = {
                "AlphaBank": [("http://a/1", None), ("http://a/2", None)],
                "BetaFund": [("http://b/1", None)],
            }

            def _fake_sample_urls(_db_path):
                return {c: [u for u, _ in pairs]
                        for c, pairs in samples_fixture.items()}

            fetch_results = [None, None, None]  # all None

            with mock.patch.object(link_health, "_sample_urls",
                                   side_effect=_fake_sample_urls), \
                 mock.patch.object(link_health, "_fetch_status",
                                   side_effect=fetch_results), \
                 mock.patch.object(link_health, "STATE", state_path):
                result = link_health.run_check("dummy.db")

            # The state file must be untouched — streaks preserved from seed.
            after = json.loads(state_path.read_text())
            self.assertEqual(after["streaks"], {"BrokenBank": 1},
                             "all-None run must not modify streak state")

            # The result must carry the inconclusive flag and an empty
            # dead_this_run list (no verdict upgrades).
            self.assertTrue(result.get("inconclusive"),
                            "all-None run must set inconclusive=True")
            self.assertEqual(result["dead_this_run"], [],
                             "all-None run must not mark any company dead")


if __name__ == "__main__":
    unittest.main()
