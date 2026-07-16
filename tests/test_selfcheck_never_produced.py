"""The NEVER-PRODUCED guard in selfcheck.

Regression cover for the blind spot that let E.ON hide as a dead SmartRecruiters
slug: a verified source that returns 0 with no baseline must eventually alert,
but only after a streak (so a freshly added source stays quiet on its first
empty check), and it must reset the moment it produces.

Also covers:
- BUG 3: bail path (crashed/timed-out verify run) attempts notify when
  SELFCHECK_EMAIL=1.
- BUG 4: freshness check flags a stale DB and affects the exit code.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import selfcheck


def _verify_output(counts):
    """Fake `main.py --verify` stdout: one OK line per (name, count)."""
    return "".join(
        f"OK {name} [workday] — {n} jobs found\n" for name, n in counts.items()
    )


class NeverProducedTests(unittest.TestCase):
    def _run(self, counts, state_path):
        """Run selfcheck.main() once with a mocked verify run + isolated state."""
        fake = mock.Mock(returncode=0, stdout=_verify_output(counts), stderr="")
        with mock.patch.object(selfcheck.subprocess, "run", return_value=fake), \
             mock.patch.object(selfcheck, "STATE", state_path), \
             mock.patch.dict(selfcheck.os.environ, {}, clear=False), \
             mock.patch("builtins.print"):
            # description_health import is best-effort guarded; force it absent
            with mock.patch.dict("sys.modules", {"description_health": None}):
                selfcheck.main()
        return json.loads(state_path.read_text())

    def test_streak_then_alert_then_reset(self):
        with TemporaryDirectory() as d:
            sp = Path(d) / "verify_state.json"

            # Check 1: E.ON at 0, no baseline -> streak=1, NOT yet flagged.
            st = self._run({"E.ON": 0, "Citi": 300}, sp)
            self.assertEqual(st["zero_streaks"].get("E.ON"), 1)
            self.assertNotIn("E.ON", st["selfcheck_never_produced"])

            # Check 2: still 0 -> streak hits threshold, now flagged.
            st = self._run({"E.ON": 0, "Citi": 300}, sp)
            self.assertGreaterEqual(st["zero_streaks"].get("E.ON"), 2)
            self.assertIn("E.ON", st["selfcheck_never_produced"])

            # Check 3: E.ON fixed (returns rows) -> streak cleared, unflagged.
            st = self._run({"E.ON": 24, "Citi": 300}, sp)
            self.assertNotIn("E.ON", st.get("zero_streaks", {}))
            self.assertNotIn("E.ON", st["selfcheck_never_produced"])

    def test_source_with_baseline_never_flagged(self):
        """A source that once produced then drops to 0 is a COLLAPSE, not a
        never-produced — it must not land in the never-produced set."""
        with TemporaryDirectory() as d:
            sp = Path(d) / "verify_state.json"
            self._run({"ING": 800}, sp)          # build baseline
            self._run({"ING": 0}, sp)            # collapses
            st = self._run({"ING": 0}, sp)
            self.assertNotIn("ING", st["selfcheck_never_produced"])


class BailPathAlertTests(unittest.TestCase):
    """BUG 3: selfcheck must send a notify alert when the verify subprocess
    crashes (non-zero exit or empty output) and SELFCHECK_EMAIL=1."""

    def _run_bail(self, returncode, stdout, stderr, state_path, env_email="1"):
        """Simulate a failed subprocess.run result and run selfcheck.main()."""
        fake = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        sent = []

        def _fake_send(subject, body):
            sent.append((subject, body))
            return True

        with mock.patch.object(selfcheck.subprocess, "run", return_value=fake), \
             mock.patch.object(selfcheck, "STATE", state_path), \
             mock.patch.dict(selfcheck.os.environ,
                             {"SELFCHECK_EMAIL": env_email}, clear=False), \
             mock.patch.dict("sys.modules", {"description_health": None}), \
             mock.patch("builtins.print"):
            # Patch notify.send_alert through the module import chain.
            notify_mock = mock.MagicMock()
            notify_mock.send_alert.side_effect = _fake_send
            with mock.patch.dict("sys.modules", {"notify": notify_mock}):
                rc = selfcheck.main()
        return rc, sent

    def test_bail_on_crash_sends_alert_when_email_on(self):
        """Non-zero exit from verify subprocess -> alert sent with SELFCHECK_EMAIL=1."""
        with TemporaryDirectory() as d:
            sp = Path(d) / "verify_state.json"
            rc, sent = self._run_bail(
                returncode=1, stdout="some partial output", stderr="traceback",
                state_path=sp,
            )
        self.assertEqual(rc, 2)
        self.assertTrue(sent, "expected at least one notify call on bail path")
        self.assertIn("FAILED", sent[0][0])

    def test_bail_on_empty_output_sends_alert(self):
        """Empty output from verify subprocess -> alert sent."""
        with TemporaryDirectory() as d:
            sp = Path(d) / "verify_state.json"
            rc, sent = self._run_bail(
                returncode=0, stdout="", stderr="",
                state_path=sp,
            )
        self.assertEqual(rc, 2)
        self.assertTrue(sent, "expected notify call for empty-output bail")

    def test_bail_no_alert_when_email_off(self):
        """With SELFCHECK_EMAIL unset, no notify call should be made."""
        with TemporaryDirectory() as d:
            sp = Path(d) / "verify_state.json"
            rc, sent = self._run_bail(
                returncode=1, stdout="", stderr="crash",
                state_path=sp, env_email="",
            )
        self.assertEqual(rc, 2)
        self.assertEqual(sent, [], "no notify when SELFCHECK_EMAIL not set")

    def test_bail_alert_failure_does_not_raise(self):
        """A crashing notify must not propagate out of selfcheck — exit code 2
        regardless (BUG 3 wraps in try/except)."""
        with TemporaryDirectory() as d:
            sp = Path(d) / "verify_state.json"
            fake = mock.Mock(returncode=1, stdout="", stderr="boom")
            broken_notify = mock.MagicMock()
            broken_notify.send_alert.side_effect = RuntimeError("smtp exploded")
            with mock.patch.object(selfcheck.subprocess, "run", return_value=fake), \
                 mock.patch.object(selfcheck, "STATE", sp), \
                 mock.patch.dict(selfcheck.os.environ,
                                 {"SELFCHECK_EMAIL": "1"}, clear=False), \
                 mock.patch.dict("sys.modules", {"notify": broken_notify,
                                                 "description_health": None}), \
                 mock.patch("builtins.print"):
                rc = selfcheck.main()
        self.assertEqual(rc, 2)


class ScanFreshnessTests(unittest.TestCase):
    """BUG 4: selfcheck must flag a stale DB (MAX(last_seen) older than
    FRESHNESS_MAX_DAYS days) and return exit code 1."""

    def _make_db(self, last_seen_iso: str | None) -> str:
        """Create a temporary jobs.db with a single row at the given last_seen."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        conn = sqlite3.connect(f.name)
        conn.execute(
            "CREATE TABLE seen_jobs (id TEXT, last_seen TEXT)"
        )
        if last_seen_iso is not None:
            conn.execute("INSERT INTO seen_jobs VALUES (?, ?)",
                         ("job1", last_seen_iso))
        conn.commit()
        conn.close()
        return f.name

    def test_fresh_db_returns_none(self):
        """A DB with a recent last_seen must not trigger the freshness warning."""
        recent = datetime.now(timezone.utc).isoformat()
        db = self._make_db(recent)
        result = selfcheck._check_scan_freshness(db)
        self.assertIsNone(result)

    def test_stale_db_returns_warning(self):
        """A DB with last_seen older than FRESHNESS_MAX_DAYS must return a
        non-empty warning string."""
        stale = (datetime.now(timezone.utc)
                 - timedelta(days=selfcheck.FRESHNESS_MAX_DAYS + 1)).isoformat()
        db = self._make_db(stale)
        result = selfcheck._check_scan_freshness(db)
        self.assertIsNotNone(result)
        self.assertIn("STALE", result)

    def test_missing_db_returns_none(self):
        """A missing DB (fresh checkout) must not crash — skip with None."""
        result = selfcheck._check_scan_freshness("/tmp/no_such_jobs.db")
        self.assertIsNone(result)

    def test_empty_db_returns_none(self):
        """An empty table (no rows) must not crash — freshness check is not
        meaningful without data."""
        db = self._make_db(None)
        result = selfcheck._check_scan_freshness(db)
        self.assertIsNone(result)

    def test_stale_db_affects_exit_code(self):
        """A stale DB must make selfcheck.main() return 1 even with no source
        alerts (BUG 4: freshness check affects exit code)."""
        stale_ts = (datetime.now(timezone.utc)
                    - timedelta(days=selfcheck.FRESHNESS_MAX_DAYS + 1)).isoformat()
        db_path = self._make_db(stale_ts)

        verify_out = "OK Citi [workday] — 200 jobs found\n"
        fake = mock.Mock(returncode=0, stdout=verify_out, stderr="")

        with TemporaryDirectory() as d:
            sp = Path(d) / "verify_state.json"
            with mock.patch.object(selfcheck.subprocess, "run", return_value=fake), \
                 mock.patch.object(selfcheck, "STATE", sp), \
                 mock.patch.object(selfcheck, "DB_FILE", db_path), \
                 mock.patch.dict(selfcheck.os.environ, {}, clear=False), \
                 mock.patch.dict("sys.modules", {"description_health": None,
                                                 "tag_health": None,
                                                 "link_health": None}), \
                 mock.patch("builtins.print"):
                rc = selfcheck.main()

        self.assertEqual(rc, 1, "stale DB must yield exit code 1")


if __name__ == "__main__":
    unittest.main()
