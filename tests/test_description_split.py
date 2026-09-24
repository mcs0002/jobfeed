"""The 2026-09-23 move of seen_jobs.description into job_descriptions."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jobfeed.db import JobDB  # noqa: E402


def _legacy_db(path: str) -> None:
    """A pre-split database: text in seen_jobs, one row without any."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE seen_jobs (id TEXT PRIMARY KEY, company TEXT, "
                "title TEXT, url TEXT, first_seen TEXT, description TEXT, "
                "description_fetched_at TEXT)")
    con.executemany("INSERT INTO seen_jobs VALUES (?,?,?,?,?,?,?)", [
        ("a", "Co", "Analyst", "u1", "2026-09-01", "Trade rates.", "2026-09-02"),
        ("b", "Co", "Quant", "u2", "2026-09-01", None, None),
        ("c", "Co", "Sales", "u3", "2026-09-01", "", None),
    ])
    con.commit()
    con.close()


class SplitScriptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "jobs.db")
        _legacy_db(self.path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "split_descriptions.py"),
             "--db", self.path, *extra], capture_output=True, text=True)

    def test_jobdb_refuses_a_presplit_database(self):
        with self.assertRaises(RuntimeError):
            JobDB(self.path)

    def test_split_moves_text_keeps_rows_and_backs_up(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        backups = [f for f in os.listdir(self.dir) if f.startswith("jobs.pre-split-")]
        self.assertEqual(len(backups), 1)
        old = sqlite3.connect(os.path.join(self.dir, backups[0]))
        self.assertEqual(old.execute(
            "SELECT description FROM seen_jobs WHERE id='a'").fetchone()[0], "Trade rates.")
        old.close()

        db = JobDB(self.path)
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(seen_jobs)")}
        self.assertNotIn("description", cols)
        self.assertNotIn("description_fetched_at", cols)
        self.assertEqual(db.total_seen(), 3)
        self.assertEqual(db.get_job("a")["description"], "Trade rates.")
        self.assertIsNone(db.get_job("b")["description"])
        self.assertIsNone(db.get_job("c")["description"])   # '' is no text
        self.assertEqual(db.conn.execute(
            "SELECT description_fetched_at FROM jobs_with_description WHERE id='a'"
        ).fetchone()[0], "2026-09-02")
        self.assertEqual({r["id"] for r in db.jobs_missing_description(limit=10)},
                         {"b", "c"})

    def test_second_run_is_a_no_op(self):
        self.assertEqual(self._run("--no-vacuum").returncode, 0)
        proc = self._run()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("already split", proc.stdout)


class SplitSchemaTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def _side_rows(self, table):
        return self.db.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_purge_takes_description_rows_with_it(self):
        self.db.mark_seen("x", company="Gone Firm", title="A", url="u",
                          description="text")
        self.db.record_description_miss("x")
        self.db.mark_seen("y", company="Kept", title="B", url="v",
                          description="kept text")
        self.assertEqual(self.db.purge_orphaned_companies({"Kept"}), 1)
        self.assertEqual(self._side_rows("job_descriptions"), 1)
        self.assertEqual(self._side_rows("description_attempts"), 0)

    def test_list_rows_carry_no_text_unless_asked(self):
        self.db.mark_seen("x", company="Co", title="A", url="u", description="body")
        self.assertNotIn("description", self.db.fetch_jobs()[0])
        self.assertEqual(self.db.fetch_jobs(with_description=True)[0]["description"],
                         "body")

    def test_dedup_still_prefers_the_fuller_duplicate(self):
        # Same company + url + title under two ids: the ID-churn case.
        self.db.mark_seen("new", company="Co", title="Role", url="u")
        self.db.mark_seen("old", company="Co", title="Role", url="u",
                          description="the enriched copy")
        self.db.conn.execute("UPDATE seen_jobs SET first_seen='2000-01-01' WHERE id='old'")
        self.db.conn.commit()
        self.assertEqual([r["id"] for r in self.db.fetch_jobs()], ["old"])

    def test_upgrade_replaces_only_a_stub(self):
        self.db.mark_seen("x", company="Co", title="A", url="u", description="stub")
        self.assertTrue(self.db.upgrade_description_if_better("x", "much longer text"))
        self.assertFalse(self.db.upgrade_description_if_better("x", "short"))
        self.db.set_description("x", "y" * 900)
        self.assertFalse(self.db.upgrade_description_if_better("x", "z" * 2000))
        self.assertFalse(self.db.upgrade_description_if_better("missing", "text"))

    def test_fill_never_overwrites(self):
        self.db.mark_seen("x", company="Co", title="A", url="u")
        self.assertTrue(self.db.fill_description_if_missing("x", "first"))
        self.assertFalse(self.db.fill_description_if_missing("x", "second"))
        self.assertEqual(self.db.get_job("x")["description"], "first")


if __name__ == "__main__":
    unittest.main()
