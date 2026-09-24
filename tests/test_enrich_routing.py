import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobfeed import main
import scrapers.enrich as enrich_pkg
from jobfeed.db import JobDB


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

            def description(self, url, tenant, board, facets=None, out=None):
                return f"WD:{tenant}/{board}"

        with patch.object(main.talentbrew_enrich, "description",
                          lambda url, s, timeout, out=None: "TB"), \
             patch.object(main.oracle_enrich, "is_oracle",
                          lambda url: "oraclecloud" in url), \
             patch.object(main.oracle_enrich, "description",
                          lambda url, s, timeout, out=None: "ORA"), \
             patch.object(main, "enrich_one", lambda url, s, timeout, out=None: "HTTP"), \
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
             patch.object(main, "enrich_one", lambda url, s, timeout, out=None: "HTTP"):
            main._enrich_new_jobs(jobs, self.db, dry_run=False)

        self.assertIsNone(jobs[0].get("description"))
        self.assertEqual(jobs[1]["description"], "HTTP")

    def test_already_described_jobs_skipped(self):
        job = self._seed("plain_c", "https://example.com/c")
        job["description"] = "already there"
        calls = []
        with patch.object(main, "enrich_one",
                          lambda url, s, timeout, out=None: calls.append(url) or "X"):
            main._enrich_new_jobs([job], self.db, dry_run=False)
        self.assertEqual(calls, [])
        self.assertEqual(job["description"], "already there")


class GreedyMatcherFallthroughTests(unittest.TestCase):
    """A URL-matched detail enricher that returns "" must fall through to the
    generic HTTP lane instead of blanking the row.

    The SuccessFactors matcher is `^https?://(?:careers|jobs)\\.[^/]+/(?:[^/]+/)?
    job/[^/]+/[^/]+` — broad enough to claim ANY careers.* host with a /job/
    path. It swallowed every Radancy row (Munich Re, ERGO, MEAG) from July
    2026: the enricher returned "", the row was recorded as skipped, and the
    plain-HTML lane — which reads Radancy's JSON-LD at full length — never got
    a turn. ERGO Group ended up 63/63 rows with no description, so the tagger
    classified the whole firm on title alone (91.8% area='other')."""

    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def _seed(self, jid, url):
        self.db.mark_seen(jid, company="Munich Re", title="Actuarial Associate",
                          url=url, category="Insurers", location="Munich")
        return {"id": jid, "url": url, "company": "Munich Re"}

    def test_empty_detail_enricher_falls_through_to_http(self):
        job = self._seed("radancy_1", "https://careers.munichre.com/en/job/munich/x/1/2")
        with patch.object(enrich_pkg, "detail_enricher",
                          lambda url: (lambda u, s, timeout=0: "")), \
             patch.object(enrich_pkg, "enrich_one", lambda url, s, timeout=0, out=None: "REAL BODY"):
            main._enrich_new_jobs([job], self.db, dry_run=False)
        self.assertEqual(job["description"], "REAL BODY")

    def test_productive_detail_enricher_is_not_refetched(self):
        job = self._seed("radancy_2", "https://careers.munichre.com/en/job/munich/y/1/2")
        http_calls = []
        with patch.object(enrich_pkg, "detail_enricher",
                          lambda url: (lambda u, s, timeout=0: "DETAIL BODY")), \
             patch.object(enrich_pkg, "enrich_one",
                          lambda url, s, timeout=0, out=None: http_calls.append(url) or "HTTP"):
            main._enrich_new_jobs([job], self.db, dry_run=False)
        self.assertEqual(job["description"], "DETAIL BODY")
        self.assertEqual(http_calls, [], "must not double-fetch a productive enricher")

    def test_both_empty_leaves_row_blank_without_raising(self):
        job = self._seed("radancy_3", "https://careers.munichre.com/en/job/munich/z/1/2")
        with patch.object(enrich_pkg, "detail_enricher",
                          lambda url: (lambda u, s, timeout=0: "")), \
             patch.object(enrich_pkg, "enrich_one", lambda url, s, timeout=0, out=None: ""):
            main._enrich_new_jobs([job], self.db, dry_run=False)
        self.assertFalse(job.get("description"))


class SharedLaneTests(unittest.TestCase):
    """main._enrich_new_jobs and the nightly backstop route through one
    function. Precedence is the contract both used to restate separately."""

    def test_workday_flag_wins_over_every_url_matcher(self):
        job = {"id": "talentbrew_1", "url": "https://x.oraclecloud.com/job/1"}
        self.assertEqual(enrich_pkg.enrich_lane(job, workday=True), "workday")

    def test_talentbrew_prefix_beats_the_successfactors_matcher(self):
        url = "https://careers.x.com/job/loc/slug/2"
        self.assertEqual(enrich_pkg.enrich_lane({"id": "sf_2", "url": url},
                                                workday=False), "detail")
        self.assertEqual(enrich_pkg.enrich_lane({"id": "talentbrew_2", "url": url},
                                                workday=False), "talentbrew")

    def test_unmatched_url_is_plain_http(self):
        self.assertEqual(enrich_pkg.enrich_lane(
            {"id": "x", "url": "https://example.com/careers/4"}, workday=False), "http")


if __name__ == "__main__":
    unittest.main()
