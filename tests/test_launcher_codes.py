"""Every failure the Antigravity helper can report must be one the database
accepts. finish_application_launch rejects an unknown code, which would leave
the launch stuck in `running` with the queue waiting on it."""
import re
import unittest
from pathlib import Path

from jobfeed.db import APPLICATION_LAUNCH_ERROR_CODES

SWIFT = Path(__file__).resolve().parents[1] / "ops/macos/JobfeedAntigravityLauncher.swift"


class LauncherCodeTests(unittest.TestCase):
    def test_every_helper_failure_code_is_accepted(self):
        source = SWIFT.read_text()
        enum = re.search(r"enum LaunchFailure: String, Error \{(.*?)\n\}", source, re.S).group(1)
        codes = set(re.findall(r"\b[a-z][a-z_]+\b", re.sub(r"\bcase\b", "", enum)))
        self.assertTrue(codes, "no LaunchFailure cases parsed")
        self.assertEqual(set(), codes - APPLICATION_LAUNCH_ERROR_CODES)


if __name__ == "__main__":
    unittest.main()
