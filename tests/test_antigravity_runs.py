import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from applications import runs as ar  # noqa: E402


def run(kind="sub", seconds=600, generations=100, calls=80, started="2026-09-18T10:00:00+00:00", **kw):
    base = {"conv": "c", "workflow": "apply_x", "kind": kind, "started": started, "ended": "",
            "seconds": seconds, "steps": 3 * calls, "generations": generations, "model": "m",
            "tool_calls": calls, "browser_calls": calls - 10, "snapshots_written": 5,
            "snapshot_reads": 4, "snapshot_rereads": 0, "failed_steps": 0, "prompts": 0,
            "outside_searches": 0}
    base.update(kw)
    return base




class WorkflowStatsTests(unittest.TestCase):
    def test_restart_sums_sessions_and_keeps_parent_generations(self):
        stats = ar.workflow_stats(
            [run(seconds=300, generations=50), run(seconds=300, generations=50),
             run(kind="par", seconds=900, generations=20)])
        self.assertEqual(2, stats["sessions"])
        self.assertEqual(600, stats["agent_seconds"])
        self.assertEqual(120, stats["generations"])
        self.assertEqual(6.0, stats["sec_per_generation"])

    def test_overview_windows(self):
        old = ar.workflow_stats([run(started="2026-01-01T00:00:00+00:00")])
        new = ar.workflow_stats([run(), run()])
        from datetime import datetime, timezone
        view = ar.overview([old, new], now=datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.assertEqual(1, view["recent"]["applications"])
        self.assertEqual(2, view["all"]["applications"])
        self.assertEqual("1 (100%)", view["recent"]["restarted"])


if __name__ == "__main__":
    unittest.main()
