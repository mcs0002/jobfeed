"""Repository layout guards (2026-09-24 relayout into packages).

Every module that resolves files relative to itself must land on the repo
root: a ROOT one directory off silently creates a fresh, empty jobs.db beside
the module instead of failing (scrapers/enrich, July 2026). The absolute-path
entry points in bin/ are what Antigravity's permission grants and the macOS
launcher name, so they must exist and import cleanly."""
import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

MODULES_WITH_ROOT = sorted(
    f"{pkg}.{p.stem}"
    for pkg in ("jobfeed", "applications")
    for p in (REPO / pkg).glob("*.py")
    if "\nROOT = " in p.read_text(encoding="utf-8")
)


class LayoutTests(unittest.TestCase):
    def test_every_root_is_the_repo_root(self):
        self.assertGreater(len(MODULES_WITH_ROOT), 20)
        for name in MODULES_WITH_ROOT:
            with self.subTest(module=name):
                root = importlib.import_module(name).ROOT
                self.assertEqual(Path(root).resolve(), REPO)

    def test_handoff_command_names_an_existing_entry_point(self):
        from applications import handoff
        self.assertEqual(Path(handoff.ROOT_HINT).resolve(), REPO)
        self.assertTrue((REPO / "bin" / "application-status").exists())

    def test_bin_entry_points_resolve_their_main(self):
        for entry in sorted((REPO / "bin").iterdir()):
            with self.subTest(entry=entry.name):
                self.assertTrue(os.access(entry, os.X_OK))
                module = entry.read_text().split("from applications.", 1)[1].split()[0]
                self.assertTrue(callable(importlib.import_module(f"applications.{module}").main))

    def test_package_entry_point_parses_flags(self):
        out = subprocess.run([sys.executable, "-m", "jobfeed", "--help"], cwd=REPO,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--verify", out.stdout)

    def test_root_holds_no_tracked_python_modules(self):
        # Tracked files only: the M1 checkout carries untracked one-off scripts.
        tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO,
                                 capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual([f for f in tracked if "/" not in f], [])


if __name__ == "__main__":
    unittest.main()
