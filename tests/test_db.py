import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import JobDB


def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _create_legacy_db(self):
        conn = sqlite3.connect(self.path)
        conn.execute("""
            CREATE TABLE seen_jobs (
                id TEXT PRIMARY KEY,
                company TEXT,
                title TEXT,
                url TEXT,
                first_seen TEXT
            )
        """)
        conn.execute(
            "INSERT INTO seen_jobs VALUES (?, ?, ?, ?, ?)",
            ("legacy-1", "Old Bank", "Graduate Analyst", "https://x", "2026-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()

    def test_legacy_db_gains_new_columns_and_keeps_rows(self):
        self._create_legacy_db()
        db = JobDB(self.path)
        cur = db.conn.execute("PRAGMA table_info(seen_jobs)")
        columns = {row[1] for row in cur.fetchall()}
        for expected in ("category", "location", "posted", "last_seen", "status",
                         "description", "description_fetched_at"):
            self.assertIn(expected, columns)
        cur = db.conn.execute(
            "SELECT company, title, url, first_seen, status FROM seen_jobs WHERE id = ?",
            ("legacy-1",),
        )
        company, title, url, first_seen, status = cur.fetchone()
        self.assertEqual(company, "Old Bank")
        self.assertEqual(title, "Graduate Analyst")
        self.assertEqual(url, "https://x")
        self.assertEqual(first_seen, "2026-01-01T00:00:00")
        self.assertEqual(status, "new")

    def test_legacy_db_dedup_unchanged(self):
        self._create_legacy_db()
        db = JobDB(self.path)
        self.assertTrue(db.seen("legacy-1"))
        self.assertFalse(db.seen("never-seen"))

    def test_reopening_migrated_db_is_idempotent(self):
        self._create_legacy_db()
        JobDB(self.path)
        db = JobDB(self.path)
        self.assertEqual(db.total_seen(), 1)


class MarkSeenTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def test_stores_full_record(self):
        self.db.mark_seen(
            "job-1",
            company="Jane Street",
            title="Graduate Trader",
            url="https://example.com/job-1",
            category="Trading",
            location="London",
            posted="2026-06-01",
        )
        cur = self.db.conn.execute(
            "SELECT company, title, url, category, location, posted, status FROM seen_jobs WHERE id = ?",
            ("job-1",),
        )
        self.assertEqual(
            cur.fetchone(),
            ("Jane Street", "Graduate Trader", "https://example.com/job-1",
             "Trading", "London", "2026-06-01", "new"),
        )

    def test_first_seen_set_and_last_seen_updates_on_repeat_sighting(self):
        self.db.mark_seen("job-1", company="Acme", title="Analyst")
        cur = self.db.conn.execute(
            "SELECT first_seen, last_seen FROM seen_jobs WHERE id = ?", ("job-1",)
        )
        first_seen, last_seen = cur.fetchone()
        self.assertEqual(first_seen, last_seen)

        # Simulate the original sighting having happened earlier.
        self.db.conn.execute(
            "UPDATE seen_jobs SET first_seen = ?, last_seen = ? WHERE id = ?",
            ("2026-01-01T00:00:00", "2026-01-01T00:00:00", "job-1"),
        )
        self.db.conn.commit()

        self.db.mark_seen("job-1", company="Acme", title="Analyst")
        cur = self.db.conn.execute(
            "SELECT first_seen, last_seen FROM seen_jobs WHERE id = ?", ("job-1",)
        )
        first_seen, last_seen = cur.fetchone()
        self.assertEqual(first_seen, "2026-01-01T00:00:00")
        self.assertGreater(last_seen, first_seen)

    def test_repeat_sighting_does_not_duplicate_or_overwrite_fields(self):
        self.db.mark_seen("job-1", company="Acme", title="Analyst", location="Paris")
        self.db.set_status("job-1", "applied")
        self.db.mark_seen("job-1", company="Acme", title="Analyst", location="Paris")
        self.assertEqual(self.db.total_seen(), 1)
        cur = self.db.conn.execute(
            "SELECT status, location FROM seen_jobs WHERE id = ?", ("job-1",)
        )
        self.assertEqual(cur.fetchone(), ("applied", "Paris"))

    def test_touch_seen_refreshes_last_seen_only(self):
        # Drives silent-zero detection: a still-posting role must bump last_seen
        # without disturbing first_seen, status, or other fields.
        self.db.mark_seen("job-1", company="Acme", title="Analyst", location="Paris")
        self.db.set_status("job-1", "applied")
        self.db.conn.execute(
            "UPDATE seen_jobs SET last_seen = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", "job-1"),
        )
        self.db.conn.commit()
        self.assertTrue(self.db.touch_seen("job-1"))
        cur = self.db.conn.execute(
            "SELECT first_seen, last_seen, status, location FROM seen_jobs "
            "WHERE id = ?", ("job-1",)
        )
        first_seen, last_seen, status, location = cur.fetchone()
        self.assertGreater(last_seen, "2000-01-01T00:00:00+00:00")
        self.assertGreater(last_seen, first_seen)
        self.assertEqual((status, location), ("applied", "Paris"))

    def test_touch_seen_unknown_id_returns_false(self):
        self.assertFalse(self.db.touch_seen("nope"))


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def test_set_status(self):
        self.db.mark_seen("job-1", company="Acme", title="Analyst")
        self.assertTrue(self.db.set_status("job-1", "applied"))
        cur = self.db.conn.execute(
            "SELECT status FROM seen_jobs WHERE id = ?", ("job-1",)
        )
        self.assertEqual(cur.fetchone()[0], "applied")

    def test_set_status_unknown_id_returns_false(self):
        self.assertFalse(self.db.set_status("missing", "applied"))

    def test_applied_at_stamped_once(self):
        self.db.mark_seen("job-1", company="Acme", title="Analyst")
        self.assertIsNone(self.db.get_job("job-1")["applied_at"])
        self.db.set_status("job-1", "applied")
        first = self.db.get_job("job-1")["applied_at"]
        self.assertIsNotNone(first)
        # Moving through later stages must NOT overwrite the original applied date.
        self.db.set_status("job-1", "interview")
        self.db.set_status("job-1", "applied")
        self.assertEqual(self.db.get_job("job-1")["applied_at"], first)

    def test_non_applied_status_does_not_stamp(self):
        self.db.mark_seen("job-1", company="Acme", title="Analyst")
        self.db.set_status("job-1", "queued")
        self.assertIsNone(self.db.get_job("job-1")["applied_at"])

    def test_set_notes_roundtrip(self):
        self.db.mark_seen("job-1", company="Acme", title="Analyst")
        self.assertTrue(self.db.set_notes("job-1", "recruiter: Jane; deadline 30 Jun"))
        self.assertEqual(self.db.get_job("job-1")["notes"], "recruiter: Jane; deadline 30 Jun")
        self.db.set_notes("job-1", "")  # clears
        self.assertIsNone(self.db.get_job("job-1")["notes"])

    def test_set_notes_unknown_id_returns_false(self):
        self.assertFalse(self.db.set_notes("missing", "x"))


class DescriptionTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def test_mark_seen_stores_description_when_supplied(self):
        self.db.mark_seen("a1", company="X", title="Analyst", url="https://x",
                          description="Run the desk.")
        row = self.db.conn.execute(
            "SELECT description, description_fetched_at FROM seen_jobs WHERE id = ?",
            ("a1",),
        ).fetchone()
        self.assertEqual(row[0], "Run the desk.")
        self.assertIsNotNone(row[1])

    def test_mark_seen_leaves_description_null_when_blank(self):
        self.db.mark_seen("a2", company="X", title="Analyst", url="https://x",
                          description="")
        row = self.db.conn.execute(
            "SELECT description, description_fetched_at FROM seen_jobs WHERE id = ?",
            ("a2",),
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])

    def test_set_description_fills_in_after_the_fact(self):
        self.db.mark_seen("a3", company="X", title="Analyst", url="https://x")
        ok = self.db.set_description("a3", "Cover the rates book.")
        self.assertTrue(ok)
        row = self.db.conn.execute(
            "SELECT description FROM seen_jobs WHERE id = ?", ("a3",),
        ).fetchone()
        self.assertEqual(row[0], "Cover the rates book.")

    def test_jobs_missing_description_lists_only_unenriched(self):
        self.db.mark_seen("with-desc", company="X", title="A", url="https://x",
                          description="filled")
        self.db.mark_seen("without-desc", company="Y", title="B", url="https://y")
        rows = self.db.jobs_missing_description(limit=10)
        ids = {r["id"] for r in rows}
        self.assertIn("without-desc", ids)
        self.assertNotIn("with-desc", ids)


class EnrichQueueTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)
        self.db.mark_seen("beesite_1", company="DB", title="A", url="u1")
        self.db.mark_seen("beesiteX_2", company="X", title="B", url="u2")
        self.db.mark_seen("gh_3", company="HRT", title="C", url="u3")

    def test_exclude_prefixes_skips_unenrichable(self):
        rows = self.db.jobs_missing_description(
            limit=10, exclude_prefixes=("beesite_",))
        ids = {r["id"] for r in rows}
        # literal prefix only: 'beesiteX_' must NOT be swept up by the LIKE
        # '_' wildcard, and other ATSes stay in the queue.
        self.assertEqual(ids, {"beesiteX_2", "gh_3"})

    def test_fill_description_if_missing(self):
        self.assertTrue(self.db.fill_description_if_missing("gh_3", "full text"))
        self.assertEqual(self.db.get_job("gh_3")["description"], "full text")
        # never overwrites
        self.assertFalse(self.db.fill_description_if_missing("gh_3", "other"))
        self.assertEqual(self.db.get_job("gh_3")["description"], "full text")
        # empty text is a no-op
        self.assertFalse(self.db.fill_description_if_missing("beesite_1", ""))


class PurgeGuardTests(unittest.TestCase):
    """Both hard-delete paths must protect any row with status history, not
    just applied ones — applied_at is only stamped on 'applied', so a
    queued/interview/offer row would otherwise be deleted (e.g. by renaming
    its company in targets.json).

    Also covers: grace-period enforcement (purge_delisted_other), and
    favorite-flag protection on both purge paths."""

    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)
        for jid, status in [("j-new", "new"), ("j-queued", "queued"),
                            ("j-interview", "interview"), ("j-applied", "applied")]:
            # Distinct urls — fetch_jobs collapses duplicate roles on url.
            self.db.mark_seen(jid, company="OldCo", title=f"T {jid}",
                              url=f"https://x/{jid}", category="Banks", location="")
            if status != "new":
                self.db.set_status(jid, status)

    def test_orphan_purge_keeps_status_history(self):
        deleted = self.db.purge_orphaned_companies({"SomeOtherCo"})
        self.assertEqual(deleted, 1)  # only j-new
        remaining = {r["id"] for r in self.db.fetch_jobs(limit=50)}
        self.assertEqual(remaining, {"j-queued", "j-interview", "j-applied"})

    def test_delisted_other_purge_keeps_status_history(self):
        for jid in ("j-new", "j-queued", "j-interview", "j-applied"):
            self.db.set_tags(jid, area="other")
        self.db.mark_delisted(["j-new", "j-queued", "j-interview", "j-applied"])
        # Backdate delisted_at so every row is past the 3-day grace window.
        self.db.conn.execute(
            "UPDATE seen_jobs SET delisted_at = '2000-01-01T00:00:00+00:00'"
        )
        self.db.conn.commit()
        purged = self.db.purge_delisted_other()
        self.assertEqual(purged, 1)  # only j-new
        remaining = {r["id"] for r in self.db.fetch_jobs(limit=50)}
        self.assertEqual(remaining, {"j-queued", "j-interview", "j-applied"})

    # --- Grace period (Bug 1) ---

    def test_delisted_recently_survives_grace_period(self):
        """A row delisted moments ago must NOT be purged in the same run."""
        self.db.mark_seen("g-new", company="OldCo", title="T g-new",
                          url="https://x/g-new", category="Banks", location="")
        self.db.set_tags("g-new", area="other")
        self.db.mark_delisted(["g-new"])  # delisted_at = now
        purged = self.db.purge_delisted_other(grace_days=3)
        self.assertEqual(purged, 0)
        self.assertIsNotNone(self.db.get_job("g-new"))

    def test_delisted_4_days_ago_is_purged(self):
        """A row delisted 4 days ago (past the 3-day grace window) is deleted."""
        self.db.mark_seen("g-old", company="OldCo", title="T g-old",
                          url="https://x/g-old", category="Banks", location="")
        self.db.set_tags("g-old", area="other")
        self.db.mark_delisted(["g-old"])
        # Rewind delisted_at to 4 days ago.
        from datetime import timedelta, timezone as tz
        from datetime import datetime as dt
        old_ts = (dt.now(tz.utc) - timedelta(days=4)).isoformat()
        self.db.conn.execute(
            "UPDATE seen_jobs SET delisted_at = ? WHERE id = ?", (old_ts, "g-old")
        )
        self.db.conn.commit()
        purged = self.db.purge_delisted_other(grace_days=3)
        self.assertEqual(purged, 1)
        self.assertIsNone(self.db.get_job("g-old"))

    # --- Favorite protection (Bug 2) ---

    def test_orphan_purge_spares_favorited_new_role(self):
        """A status='new' role that is favorited must survive orphan purge."""
        self.db.mark_seen("fav-new", company="OldCo", title="T fav-new",
                          url="https://x/fav-new", category="Banks", location="")
        self.db.set_favorite("fav-new", True)
        deleted = self.db.purge_orphaned_companies({"SomeOtherCo"})
        # j-new should be deleted, fav-new should survive.
        self.assertIsNotNone(self.db.get_job("fav-new"))
        # The unfavorited new role is gone.
        self.assertIsNone(self.db.get_job("j-new"))

    def test_delisted_other_purge_spares_favorited_new_role(self):
        """A status='new' role that is favorited and area='other' must survive
        purge_delisted_other even when past the grace window."""
        self.db.mark_seen("fav-other", company="OldCo", title="T fav-other",
                          url="https://x/fav-other", category="Banks", location="")
        self.db.set_tags("fav-other", area="other")
        self.db.set_favorite("fav-other", True)
        self.db.mark_delisted(["fav-other", "j-new"])
        self.db.set_tags("j-new", area="other")
        # Backdate both so they're past the grace window.
        self.db.conn.execute(
            "UPDATE seen_jobs SET delisted_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id IN ('fav-other', 'j-new')"
        )
        self.db.conn.commit()
        purged = self.db.purge_delisted_other()
        # j-new purged; fav-other spared.
        self.assertIsNone(self.db.get_job("j-new"))
        self.assertIsNotNone(self.db.get_job("fav-other"))


class KochFailLoudTests(unittest.TestCase):
    """Koch scraper must RAISE on missing/expired cookies. Returning [] would
    read downstream as "board is empty" and delist every Koch row on each
    ~weekly cookie expiry; an exception keeps the company out of the delist
    pass while the rest of the run proceeds (main.py catches per-company)."""

    def test_raises_when_cookies_file_missing(self):
        from scrapers import koch_avature
        with self.assertRaisesRegex(RuntimeError, "KOCH_COOKIES_MISSING"):
            koch_avature.scrape({"cookies_path": "/nonexistent/path.json"})

    def test_raises_when_cookies_file_lacks_aws_waf_token(self):
        import json as _json
        from scrapers import koch_avature
        path = temp_db_path() + ".json"
        with open(path, "w") as f:
            _json.dump({"JSESSIONID": "x"}, f)  # missing aws-waf-token
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with self.assertRaisesRegex(RuntimeError, "KOCH_COOKIES_MISSING"):
            koch_avature.scrape({"cookies_path": path})


class TagsAndFetchTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)
        # Two roles, different function/region; one tagged, one left bare.
        self.db.mark_seen("a", company="GS", title="FX Trader", url="u1",
                          category="Banks", location="London")
        self.db.mark_seen("b", company="BlackRock", title="M&A Analyst", url="u2",
                          category="Asset Managers", location="New York")

    def test_set_tags_roundtrip(self):
        ok = self.db.set_tags("a", area="markets", desk="trading",
                              seniority="graduate", job_type="job",
                              loc_city="London", loc_country="United Kingdom",
                              loc_region="Europe", work_mode="onsite")
        self.assertTrue(ok)
        job = self.db.get_job("a")
        self.assertEqual(job["area"], "markets")
        self.assertEqual(job["desk"], "trading")
        self.assertEqual(job["seniority"], "graduate")
        self.assertEqual(job["loc_region"], "Europe")
        # tagged_at is internal (not a display column) — verify it was stamped.
        ts = self.db.conn.execute(
            "SELECT tagged_at FROM seen_jobs WHERE id = ?", ("a",)
        ).fetchone()[0]
        self.assertIsNotNone(ts)

    def test_fetch_jobs_filters(self):
        self.db.set_tags("a", area="markets", desk="trading", loc_region="Europe")
        self.db.set_tags("b", area="ibd", loc_region="Americas")
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(area="markets")], ["a"])
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(desk="trading")], ["a"])
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(loc_region="Americas")], ["b"])
        self.assertEqual(len(self.db.fetch_jobs()), 2)

    def test_hide_other(self):
        self.db.set_tags("a", area="markets", desk="trading")
        self.db.set_tags("b", area="other")
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(hide_other=True)], ["a"])
        self.assertEqual(len(self.db.fetch_jobs(hide_other=False)), 2)

    def test_hide_yoe(self):
        self.db.set_tags("a", area="markets")
        self.db.set_tags("b", area="markets")
        self.db.set_yoe("a", 0)   # entry-level
        self.db.set_yoe("b", 5)   # disguised senior
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(hide_yoe=True)], ["a"])
        self.assertEqual(len(self.db.fetch_jobs(hide_yoe=False)), 2)
        self.assertEqual(self.db.get_job("b")["min_yoe"], 5)

    def test_hide_internships(self):
        self.db.set_tags("a", area="markets", job_type="job")
        self.db.set_tags("b", area="markets", job_type="internship")
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(hide_internships=True)], ["a"])
        self.assertEqual(len(self.db.fetch_jobs(hide_internships=False)), 2)

    def test_hide_associates(self):
        self.db.set_tags("a", area="markets", seniority="analyst")
        self.db.set_tags("b", area="markets", seniority="associate")
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(hide_associates=True)], ["a"])
        self.assertEqual(len(self.db.fetch_jobs(hide_associates=False)), 2)

    def test_fetch_jobs_free_text_and_company_set(self):
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(q="trader")], ["a"])
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(companies=["BlackRock"])], ["b"])
        self.assertEqual(self.db.fetch_jobs(companies=[]), [])

    def test_free_text_search_matches_title_and_company_only(self):
        # The search bar's contract is "Search title or company" — q must NOT
        # match location or description, or a city-name search silently turns
        # into a broken location filter (row 'a' is located in London but has
        # no 'London' in title/company; 'c' mentions London only in its body).
        self.db.mark_seen("c", company="UniCredit", title="M&A Intern", url="u3",
                          category="Banks", location="Munich",
                          description="Work with the London coverage team.")
        hits = [j["id"] for j in self.db.fetch_jobs(q="London")]
        self.assertEqual(hits, [])
        # ...while a company-name hit still works.
        self.assertEqual([j["id"] for j in self.db.fetch_jobs(q="UniCredit")], ["c"])

    def test_distinct_facets_and_whitelist(self):
        self.db.set_tags("a", area="markets")
        self.assertIn("markets", self.db.distinct("area"))
        with self.assertRaises(ValueError):
            self.db.distinct("url")  # not a facet column

    def test_prune_preserves_applied_descriptions(self):
        # Backdate both rows so they're outside the window, give both a desc.
        old = "2000-01-01T00:00:00+00:00"
        for jid in ("a", "b"):
            self.db.conn.execute(
                "UPDATE seen_jobs SET first_seen = ?, description = 'body' WHERE id = ?",
                (old, jid),
            )
        self.db.conn.commit()
        self.db.set_status("a", "applied")  # 'a' applied, 'b' still new
        self.db.prune_old_descriptions(max_age_days=60)
        self.assertEqual(self.db.get_job("a")["description"], "body")  # kept
        self.assertIsNone(self.db.get_job("b")["description"])         # pruned


class FetchDedupTests(unittest.TestCase):
    """fetch_jobs() collapses exact-duplicate roles stored under different ids.

    Root cause: some ATSes (Glencore) re-emit the same opening on every scan
    under a fresh internal id while the canonical posting url is unchanged, so
    mark_seen()'s id-dedup never catches it. Query-time dedup keys on the
    stable url (falling back to company+normalized-title+location)."""

    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def test_same_url_different_ids_collapse_to_one(self):
        # Glencore-style: same posting url, three churned ids, location even
        # captured inconsistently across scans. Should surface ONCE.
        self.db.mark_seen("glencore_1", company="Glencore", title="Quant Analyst",
                          url="https://glencore.com/jobs/999", location="")
        self.db.mark_seen("glencore_2", company="Glencore", title="Quant Analyst",
                          url="https://glencore.com/jobs/999", location="Baar")
        self.db.mark_seen("glencore_3", company="Glencore", title="Quant Analyst",
                          url="https://glencore.com/jobs/999", location="Baar, Switzerland")
        rows = self.db.fetch_jobs(company="Glencore")
        self.assertEqual(len(rows), 1)
        # raw storage is untouched — dedup is read-side only.
        self.assertEqual(self.db.total_seen(), 3)

    def test_different_urls_are_kept_separate(self):
        # Same title, genuinely different reqs (distinct urls) — not collapsed.
        self.db.mark_seen("a", company="Acme", title="Trader",
                          url="https://acme.com/1", location="London")
        self.db.mark_seen("b", company="Acme", title="Trader",
                          url="https://acme.com/2", location="London")
        self.assertEqual(len(self.db.fetch_jobs(company="Acme")), 2)

    def test_title_whitespace_and_case_normalized_when_no_url(self):
        # No url -> fall back to (company, normalized title, location).
        self.db.mark_seen("x", company="Co", title="Risk  Analyst", url="",
                          location="Zug")
        self.db.mark_seen("y", company="Co", title="risk analyst", url="",
                          location="Zug")
        self.assertEqual(len(self.db.fetch_jobs(company="Co")), 1)

    def test_different_location_not_collapsed_when_no_url(self):
        # Different cities are different openings — the no-url fallback keeps
        # them apart so we don't merge genuinely distinct reqs.
        self.db.mark_seen("x", company="Co", title="Risk Analyst", url="",
                          location="Zug")
        self.db.mark_seen("y", company="Co", title="Risk Analyst", url="",
                          location="London")
        self.assertEqual(len(self.db.fetch_jobs(company="Co")), 2)

    def test_acted_on_duplicate_is_the_representative(self):
        # When duplicates exist, an applied/ignored row must win over a 'new'
        # one so the apply-tracking marking is never hidden.
        self.db.mark_seen("d1", company="Co", title="Analyst",
                          url="https://co.com/1")
        self.db.mark_seen("d2", company="Co", title="Analyst",
                          url="https://co.com/1")
        self.db.set_status("d2", "applied")
        rows = self.db.fetch_jobs(company="Co")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "applied")
        self.assertEqual(rows[0]["id"], "d2")

    def test_limit_applies_after_dedup(self):
        # Three copies of one role + one distinct role. limit=2 must return two
        # DISTINCT roles, not two copies of the same one.
        for i in range(3):
            self.db.mark_seen(f"dup{i}", company="Co", title="Same",
                              url="https://co.com/same")
        self.db.mark_seen("other", company="Co", title="Other",
                          url="https://co.com/other")
        rows = self.db.fetch_jobs(company="Co", limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["url"] for r in rows}), 2)


class IdChurnTests(unittest.TestCase):
    """ID-churn guard: some ATSes re-emit the same logical role under a fresh
    id every scan while the canonical url is stable. find_active_by_url lets the
    scan loop touch/forward-fill the existing row instead of inserting a
    duplicate whose empty description could hide the enriched original."""

    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def test_find_active_by_url_matches_other_id(self):
        self.db.mark_seen("glencore_1", company="Glencore", title="Quant",
                          url="https://glencore.com/jobs/999",
                          description="Full enriched body.")
        found = self.db.find_active_by_url("https://glencore.com/jobs/999",
                                           exclude_id="glencore_2")
        self.assertEqual(found, "glencore_1")

    def test_find_active_by_url_excludes_self(self):
        self.db.mark_seen("only", company="Co", title="T",
                          url="https://co.com/1")
        self.assertIsNone(self.db.find_active_by_url("https://co.com/1", "only"))

    def test_find_active_by_url_blank_url_is_none(self):
        self.assertIsNone(self.db.find_active_by_url("", "x"))
        self.assertIsNone(self.db.find_active_by_url("   ", "x"))

    def test_find_active_by_url_prefers_live_over_delisted(self):
        self.db.mark_seen("dead", company="Co", title="T",
                          url="https://co.com/1")
        self.db.mark_seen("live", company="Co", title="T",
                          url="https://co.com/1")
        self.db.mark_delisted(["dead"])
        found = self.db.find_active_by_url("https://co.com/1", exclude_id="new_id")
        self.assertEqual(found, "live")

    def test_dedup_prefers_longest_description_when_none_acted_on(self):
        # Two churned copies of one role, same url, neither acted-on. The empty
        # fresh copy sorts first under 'recent' but must NOT be the
        # representative — the enriched copy wins on description length.
        self.db.mark_seen("old_enriched", company="Co", title="Quant",
                          url="https://co.com/9",
                          description="A long enriched description body.")
        self.db.mark_seen("new_empty", company="Co", title="Quant",
                          url="https://co.com/9")
        rows = self.db.fetch_jobs(company="Co")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "old_enriched")

    def test_dedup_acted_on_beats_longer_description(self):
        # An acted-on row wins even if the 'new' duplicate has a longer body —
        # the apply-tracking marking must never be hidden.
        self.db.mark_seen("applied_short", company="Co", title="Q",
                          url="https://co.com/8", description="short")
        self.db.mark_seen("new_long", company="Co", title="Q",
                          url="https://co.com/8",
                          description="a much much longer description body here")
        self.db.set_status("applied_short", "applied")
        rows = self.db.fetch_jobs(company="Co")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "applied_short")


class SchemaReadyGateTests(unittest.TestCase):
    """M2: repeat JobDB constructions for the same path skip the DDL + commit
    (web/app.py builds one per request). The gate must still create the schema
    on first open, and an in-memory DB must always init (fresh per connection)."""

    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_schema_created_on_first_open_and_reopen_works(self):
        db = JobDB(self.path)
        db.mark_seen("j1", company="Co", title="T", url="https://co.com/1")
        # Second construction for the same path skips _init but must still work.
        db2 = JobDB(self.path)
        self.assertTrue(db2.seen("j1"))
        self.assertEqual(db2.total_seen(), 1)

    def test_in_memory_db_always_initializes(self):
        # Each :memory: connection is a distinct empty DB — must not be gated.
        a = JobDB(":memory:")
        a.mark_seen("x", company="Co", title="T")
        b = JobDB(":memory:")
        self.assertFalse(b.seen("x"))  # separate DB, but schema present
        self.assertEqual(b.total_seen(), 0)


class DescFacetTests(unittest.TestCase):
    """NULL-vs-'' semantics on the description-derived facets, the re-tag
    selection query, and the lang_req CONTAINS filter + split facet."""

    def setUp(self):
        self.path = temp_db_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.db = JobDB(self.path)

    def _seed(self, jid, **kw):
        self.db.mark_seen(jid, company=kw.get("company", "Co"),
                          title=kw.get("title", jid),
                          url=f"https://co.com/{jid}",
                          description=kw.get("description", ""))

    def test_null_vs_empty_distinguishes_pre_description_tag(self):
        # Row tagged WITHOUT a description: pass None → columns stay NULL.
        self._seed("pre", description="")
        self.db.set_tags("pre", area="markets", lang_req=None,
                         education=None, start_date=None)
        # Row tagged WITH a description, genuinely no requirement: pass '' → ''.
        self._seed("with", description="A full description here.")
        self.db.set_tags("with", area="markets", lang_req="",
                         education="", start_date="")
        pre = self.db.get_job("pre")
        wit = self.db.get_job("with")
        self.assertIsNone(pre["lang_req"])
        self.assertIsNone(pre["education"])
        self.assertIsNone(pre["start_date"])
        self.assertEqual(wit["lang_req"], "")
        self.assertEqual(wit["education"], "")
        self.assertEqual(wit["start_date"], "")

    def test_retag_query_selects_only_tagged_pre_description_now_with_desc(self):
        # (a) tagged pre-desc, now has a desc → SELECTED
        self._seed("a", description="Now enriched with a real body.")
        self.db.set_tags("a", area="markets", lang_req=None,
                         education=None, start_date=None)
        # (b) tagged WITH a desc already (lang_req='') → NOT selected
        self._seed("b", description="Had a body at tag time.")
        self.db.set_tags("b", area="markets", lang_req="",
                         education="", start_date="")
        # (c) never tagged (tagged_at NULL) → NOT selected (that's plain backfill)
        self._seed("c", description="Body but never tagged.")
        # (d) tagged pre-desc, STILL no desc → NOT selected (nothing to read)
        self._seed("d", description="")
        self.db.set_tags("d", area="markets", lang_req=None,
                         education=None, start_date=None)
        # (e) qualifies like (a) but DELISTED → NOT selected (hidden from
        # browsing by default; nightly quota goes to live rows only)
        self._seed("e", description="Enriched but the posting is gone.")
        self.db.set_tags("e", area="markets", lang_req=None,
                         education=None, start_date=None)
        self.db.conn.execute(
            "UPDATE seen_jobs SET delisted_at = '2026-07-01' WHERE id = 'e'")
        self.db.conn.commit()
        rows = self.db.rows_needing_desc_facet_retag(
            ["id", "title", "description"], limit=300)
        ids = {r["id"] for r in rows}
        self.assertEqual(ids, {"a"})

    def test_retag_query_respects_limit(self):
        for i in range(5):
            self._seed(f"r{i}", description="Enriched body.")
            self.db.set_tags(f"r{i}", area="markets", lang_req=None,
                             education=None, start_date=None)
        rows = self.db.rows_needing_desc_facet_retag(["id"], limit=2)
        self.assertEqual(len(rows), 2)

    def test_lang_req_contains_filter_matches_multi_value(self):
        self._seed("de_fr", description="x")
        self.db.set_tags("de_fr", area="markets", lang_req="de,fr")
        self._seed("nl", description="x")
        self.db.set_tags("nl", area="markets", lang_req="nl")
        self._seed("none", description="x")
        self.db.set_tags("none", area="markets", lang_req="")
        # 'fr' must match the "de,fr" row (CONTAINS, not exact).
        fr = {r["id"] for r in self.db.fetch_jobs(lang_req="fr")}
        self.assertEqual(fr, {"de_fr"})
        de = {r["id"] for r in self.db.fetch_jobs(lang_req="de")}
        self.assertEqual(de, {"de_fr"})
        nl = {r["id"] for r in self.db.fetch_jobs(lang_req="nl")}
        self.assertEqual(nl, {"nl"})

    def test_lang_req_distinct_splits_combos_into_codes(self):
        self._seed("x", description="d")
        self.db.set_tags("x", area="markets", lang_req="de,fr")
        self._seed("y", description="d")
        self.db.set_tags("y", area="markets", lang_req="fr,it")
        self.assertEqual(self.db.distinct("lang_req"), ["de", "fr", "it"])

    def test_education_exact_filter(self):
        self._seed("m", description="d")
        self.db.set_tags("m", area="markets", education="master")
        self._seed("b", description="d")
        self.db.set_tags("b", area="markets", education="bachelor")
        got = {r["id"] for r in self.db.fetch_jobs(education="master")}
        self.assertEqual(got, {"m"})

    def test_llm_min_yoe_written_regex_left_alone_when_none(self):
        self._seed("j", description="d")
        # Simulate the regex fallback having set min_yoe first.
        self.db.set_yoe("j", 4)
        # A tag pass with min_yoe=None must NOT clobber the regex value.
        self.db.set_tags("j", area="markets", min_yoe=None)
        self.assertEqual(self.db.get_job("j")["min_yoe"], 4)
        # A tag pass WITH a value wins.
        self.db.set_tags("j", area="markets", min_yoe=6)
        self.assertEqual(self.db.get_job("j")["min_yoe"], 6)


if __name__ == "__main__":
    unittest.main()
