import csv
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import query
from db import JobDB

PYTHON = sys.executable
QUERY = os.path.join(ROOT, "query.py")


def temp_path(suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.unlink(path)
    return path


class QueryToolTests(unittest.TestCase):
    def setUp(self):
        self.db_path = temp_path(".db")
        self.addCleanup(lambda: os.path.exists(self.db_path) and os.unlink(self.db_path))
        self.db = JobDB(self.db_path)
        self.db.mark_seen(
            "job-1", company="Jane Street", title="Graduate Trader",
            url="https://example.com/1", category="Trading", location="London",
            posted="2026-06-01",
        )
        self.db.mark_seen(
            "job-2", company="Acme Bank", title="Markets Analyst",
            url="https://example.com/2", category="Bank", location="Frankfurt",
            posted="2026-06-02",
        )
        self.db.set_status("job-2", "applied")

    def run_query(self, *args):
        env = dict(os.environ, JOBS_DB=self.db_path)
        return subprocess.run(
            [PYTHON, QUERY, *args],
            capture_output=True, text=True, env=env,
        )

    def test_fetch_roles_no_filter(self):
        roles = query.fetch_roles(self.db)
        self.assertEqual(len(roles), 2)
        self.assertEqual(
            [role["id"] for role in roles], ["job-2", "job-1"]
        )  # ordered by company

    def test_fetch_roles_filters(self):
        self.assertEqual(
            [r["id"] for r in query.fetch_roles(self.db, status="applied")],
            ["job-2"],
        )
        self.assertEqual(
            [r["id"] for r in query.fetch_roles(self.db, category="Trad")],
            ["job-1"],
        )
        self.assertEqual(
            [r["id"] for r in query.fetch_roles(self.db, company="Jane")],
            ["job-1"],
        )

    def test_cli_list_prints_table(self):
        result = self.run_query()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Graduate Trader", result.stdout)
        self.assertIn("Markets Analyst", result.stdout)
        self.assertIn("2 role(s)", result.stdout)

    def test_cli_status_filter(self):
        result = self.run_query("--status", "new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Graduate Trader", result.stdout)
        self.assertNotIn("Markets Analyst", result.stdout)

    def test_cli_export_csv(self):
        csv_path = temp_path(".csv")
        self.addCleanup(lambda: os.path.exists(csv_path) and os.unlink(csv_path))
        result = self.run_query("--export", csv_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["job-1"]["location"], "London")
        self.assertEqual(by_id["job-2"]["status"], "applied")

    def test_cli_mark_status(self):
        result = self.run_query("--mark", "job-1", "ignored")
        self.assertEqual(result.returncode, 0, result.stderr)
        cur = self.db.conn.execute(
            "SELECT status FROM seen_jobs WHERE id = ?", ("job-1",)
        )
        self.assertEqual(cur.fetchone()[0], "ignored")

    def test_cli_mark_unknown_id_fails(self):
        result = self.run_query("--mark", "missing", "applied")
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
