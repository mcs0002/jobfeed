import importlib.util
import os
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "agaudit", Path(os.path.dirname(__file__)).parent / "scripts" / "agaudit.py")
agaudit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agaudit)


def read(step, second):
    return ({"step_index": step, "created_at": f"2026-10-06T07:00:{second:02d}Z"},
            "view_file",
            {"AbsolutePath": f"/b/.tempmediaStorage/snapshot_full_{step}.txt"})


def click(step):
    return ({"step_index": step, "created_at": "2026-10-06T07:01:00Z"},
            "mcp_chrome_devtools_click", {})


class ReadStreakTests(unittest.TestCase):
    def test_fresh_files_read_without_action_are_one_streak(self):
        calls = [read(i, i) for i in range(10)] + [click(10)]
        self.assertEqual([(0, 9, 10, 9)], agaudit.read_streaks(calls))

    def test_reads_broken_by_actions_are_clean(self):
        calls = []
        for i in range(0, 40, 4):
            calls += [read(i, i), read(i + 1, i + 1), click(i + 2)]
        self.assertEqual([], agaudit.read_streaks(calls))
