import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from db import JobDB


def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


class EnrichRoutingTests(unittest.TestCase):
    """The inline-enrichment lane router (main._enrich_new_jobs) must send each
    job to the right enricher and persist what comes back. This pins the
    2026-07 registry refactor against the old eight-block behavior."""

    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def _seed(self, jid, url):
        self.db.mark_seen(jid, company="C", title=f"T {jid}", url=url,
                          category="Banks", location="")
        return {"id": jid, "url": url, "company": "C"}

    def test_lane_routing_and_persistence(self):
        jobs = [
            self._seed("wd_1", "https://x.wd1.myworkdayjobs.com/board/job/1"),
            self._seed("talentbrew_2", "https://careers.x.com/job/loc/slug/2"),
            self._seed("oracle_3", "https://x.oraclecloud.com/job/3"),
            self._seed("plain_4", "https://example.com/careers/4"),
        ]
        jobs[0]["_wd"] = {"tenant": "x", "board": "board"}

        class FakeWd:
            def __init__(self, timeout=None):
                pass

            def description(self, url, tenant, board, facets=None):
                return f"WD:{tenant}/{board}"

        with patch.object(main.talentbrew_enrich, "description",
                          lambda url, s, timeout: "TB"), \
             patch.object(main.oracle_enrich, "is_oracle",
                          lambda url: "oraclecloud" in url), \
             patch.object(main.oracle_enrich, "description",
                          lambda url, s, timeout: "ORA"), \
             patch.object(main, "enrich_one", lambda url, s, timeout: "HTTP"), \
             patch("scrapers.enrich.workday_enrich.WorkdayEnricher", FakeWd):
            main._enrich_new_jobs(jobs, self.db, dry_run=False)

        got = {j["id"]: j.get("description") for j in jobs}
        self.assertEqual(got["wd_1"], "WD:x/board")
        self.assertEqual(got["talentbrew_2"], "TB")
        self.assertEqual(got["oracle_3"], "ORA")
        self.assertEqual(got["plain_4"], "HTTP")
        # persisted, not just set in-memory
        self.assertEqual(self.db.get_job("oracle_3")["description"], "ORA")

    def test_one_failing_lane_does_not_break_others(self):
        jobs = [
            self._seed("oracle_a", "https://x.oraclecloud.com/job/a"),
            self._seed("plain_b", "https://example.com/careers/b"),
        ]

        def boom(url, s, timeout):
            raise RuntimeError("enricher exploded")

        with patch.object(main.oracle_enrich, "is_oracle",
                          lambda url: "oraclecloud" in url), \
             patch.object(main.oracle_enrich, "description", boom), \
             patch.object(main, "enrich_one", lambda url, s, timeout: "HTTP"):
            main._enrich_new_jobs(jobs, self.db, dry_run=False)

        self.assertIsNone(jobs[0].get("description"))
        self.assertEqual(jobs[1]["description"], "HTTP")

    def test_already_described_jobs_skipped(self):
        job = self._seed("plain_c", "https://example.com/c")
        job["description"] = "already there"
        calls = []
        with patch.object(main, "enrich_one",
                          lambda url, s, timeout: calls.append(url) or "X"):
            main._enrich_new_jobs([job], self.db, dry_run=False)
        self.assertEqual(calls, [])
        self.assertEqual(job["description"], "already there")


if __name__ == "__main__":
    unittest.main()
