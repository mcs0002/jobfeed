"""jobfeed/deadline_backfill.py: stored Workday/Oracle rows gain their end date."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from jobfeed import deadline_backfill
from jobfeed.db import JobDB


class DeadlineBackfillTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.addCleanup(os.unlink, self.path)
        db = JobDB(self.path)
        rows = [("w1", "Wd Co", "https://x.wd3.myworkdayjobs.com/B/job/L/A_R1", None),
                ("o1", "Or Co", "https://x.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/9", None),
                ("o2", "Or Co", "https://x.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/8", "2026-12-01"),
                ("g1", "Gh Co", "https://boards.greenhouse.io/x/jobs/1", None)]
        for jid, co, url, dl in rows:
            db.mark_seen(jid, company=co, title=jid, url=url, category="", location="")
            if dl:
                db.set_deadline(jid, dl)
        db.conn.commit()
        db.conn.close()
        self.when = (date.today() + timedelta(days=10)).isoformat()

    def run_cli(self, *extra):
        def wd(self_, url, tenant, board, facets=None, out=None):
            out["deadline"] = self.when
            return "Body"

        def ora(url, session=None, timeout=20, out=None):
            out["deadline"] = self.when
            return "Body"

        argv = ["deadline_backfill", "--db", self.path, "--throttle", "0", *extra]
        with patch.object(sys, "argv", argv), \
             patch.object(deadline_backfill, "_load_workday_cfgs",
                          lambda: {"Wd Co": {"tenant": "x", "board": "B"}}), \
             patch.object(deadline_backfill.WorkdayEnricher, "description", wd), \
             patch.object(deadline_backfill.oracle_enrich, "description", ora):
            deadline_backfill.main()
        db = JobDB(self.path)
        got = dict(db.conn.execute("SELECT id, deadline FROM seen_jobs"))
        db.conn.close()
        for f in os.listdir(os.path.dirname(self.path)):
            if f.startswith("deadline_backfill_undo_"):
                os.unlink(os.path.join(os.path.dirname(self.path), f))
        return got

    def test_dry_run_writes_nothing(self):
        got = self.run_cli()
        self.assertIsNone(got["w1"])
        self.assertIsNone(got["o1"])

    def test_apply_fills_only_undated_workday_and_oracle_rows(self):
        got = self.run_cli("--apply")
        self.assertEqual(got["w1"], self.when)
        self.assertEqual(got["o1"], self.when)
        self.assertEqual(got["o2"], "2026-12-01")  # already dated: not selected
        self.assertIsNone(got["g1"])               # not Workday/Oracle


    def test_an_interrupted_run_keeps_what_it_stored(self):
        """Dates are saved as found: stopping after the first row (Ctrl+C,
        a closed laptop) must not lose it, and the undo file must list it."""
        calls = []

        def wd(self_, url, tenant, board, facets=None, out=None):
            out["deadline"] = self.when
            return "Body"

        def ora(url, session=None, timeout=20, out=None):
            calls.append(url)
            raise KeyboardInterrupt

        argv = ["deadline_backfill", "--db", self.path, "--throttle", "0", "--apply"]
        with patch.object(sys, "argv", argv), \
             patch.object(deadline_backfill, "_load_workday_cfgs",
                          lambda: {"Wd Co": {"tenant": "x", "board": "B"}}), \
             patch.object(deadline_backfill.WorkdayEnricher, "description", wd), \
             patch.object(deadline_backfill.oracle_enrich, "description", ora):
            # Rows run newest first; make the Workday row the first one.
            db = JobDB(self.path)
            db.conn.execute("UPDATE seen_jobs SET first_seen = '2099-01-01' WHERE id = 'w1'")
            db.conn.commit()
            db.conn.close()
            with self.assertRaises(KeyboardInterrupt):
                deadline_backfill.main()
        db = JobDB(self.path)
        got = dict(db.conn.execute("SELECT id, deadline FROM seen_jobs"))
        db.conn.close()
        self.assertEqual(got["w1"], self.when)
        self.assertIsNone(got["o1"])
        folder = os.path.dirname(self.path)
        undo = [f for f in os.listdir(folder) if f.startswith("deadline_backfill_undo_")]
        self.assertTrue(undo)
        with open(os.path.join(folder, undo[-1])) as fp:
            self.assertIn("w1", fp.read())
        for f in undo:
            os.unlink(os.path.join(folder, f))


if __name__ == "__main__":
    unittest.main()
