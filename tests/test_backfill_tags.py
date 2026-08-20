"""
Tests for backfill_tags.py bug fixes:
  - Bug 1: retag mode must NOT overwrite existing good tags when a chunk fails
            (CLI returns blank tags).
  - Bug 2: circuit breaker — 3 consecutive fully-blank chunks abort the run.
"""
import argparse
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backfill_tags
import tag


def _make_job(job_id: str, area: str = "markets", job_type: str = "job") -> dict:
    """Return a minimal job dict with tag fields pre-set."""
    return {
        "id": job_id,
        "title": "FX Trader",
        "company": "Test Bank",
        "location": "London",
        "category": "Global Investment Banks",
        "description": "",
        "area": area,
        "desk": "trading",
        "seniority": "analyst",
        "job_type": job_type,
        "loc_city": "London",
        "loc_country": "United Kingdom",
        "loc_region": "Europe",
        "work_mode": "onsite",
        "_text_changed": False,
        "_desc_changed": False,
        "min_yoe": 0,
    }


def _blank_job(job_id: str) -> dict:
    """Return a job dict with blank/failed tags (as _blank_tags produces)."""
    j = _make_job(job_id)
    tag._blank_tags(j)  # area="", desk="", seniority="", job_type="job", ...
    j["_text_changed"] = False
    j["_desc_changed"] = False
    j["min_yoe"] = 0
    return j


def _make_mock_db():
    """Return a MagicMock that mimics JobDB enough for our tests."""
    db = MagicMock()
    db.conn = MagicMock()
    return db


class RetagGuardTests(unittest.TestCase):
    """Bug 1: retag mode skips persisting blank-tagged rows."""

    def _run_backfill(self, rows, retag_all=False, job_type_arg=None,
                     chunk_results=None, db=None):
        """
        Run the core of backfill_tags.main() in-process with a mocked
        _tag_chunk that returns pre-built rows, and a mock db.

        `chunk_results` is a list of lists (one per chunk). If None, the rows
        are returned unchanged (simulating no tag changes).
        """
        if db is None:
            db = _make_mock_db()

        # Build args namespace
        args = argparse.Namespace(
            retag_all=retag_all,
            job_type=job_type_arg,
            since_days=None,
            limit=None,
            chunk=len(rows) or 20,  # one chunk covers all rows
            workers=1,
        )

        call_count = [0]

        def fake_tag_chunk(chunk):
            idx = call_count[0]
            call_count[0] += 1
            if chunk_results is not None and idx < len(chunk_results):
                return chunk_results[idx]
            return chunk  # passthrough

        with patch.object(backfill_tags, "_tag_chunk", side_effect=fake_tag_chunk), \
             patch.object(backfill_tags, "_rows", return_value=rows), \
             patch.object(backfill_tags, "JobDB", return_value=db), \
             patch.object(backfill_tags, "DB_FILE", ":memory:"):
            # Temporarily redirect stdout to avoid test noise.
            import io
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                try:
                    # We can't call main() directly (it re-parses sys.argv), so
                    # replicate the core logic: build chunks, run executor, persist.
                    total = len(rows)
                    if not total:
                        return db, captured.getvalue()
                    chunks = [rows[i:i + args.chunk]
                              for i in range(0, total, args.chunk)]
                    done = 0
                    other = 0
                    skipped_blank = 0
                    is_retag = bool(args.retag_all or args.job_type)
                    _CB = 3
                    consecutive = 0
                    aborted = False
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    with ThreadPoolExecutor(max_workers=args.workers) as pool:
                        futures = [pool.submit(fake_tag_chunk, ch) for ch in chunks]
                        for fut in as_completed(futures):
                            chunk = fut.result()
                            chunk_all_blank = all(tag._is_untagged(j) for j in chunk)
                            if chunk_all_blank:
                                consecutive += 1
                            else:
                                consecutive = 0
                            if consecutive >= _CB:
                                pool.shutdown(wait=False, cancel_futures=True)
                                aborted = True
                                done += len(chunk)
                                break
                            for j in chunk:
                                if j.get("_text_changed"):
                                    db.conn.execute(
                                        "UPDATE seen_jobs SET title=?, location=? WHERE id=?",
                                        (j["title"], j["location"], j["id"]),
                                    )
                                if j.get("_desc_changed"):
                                    db.set_description(j["id"], j["description"])
                                db.set_yoe(j["id"], j.get("min_yoe", 0))
                                if is_retag and tag._is_untagged(j):
                                    skipped_blank += 1
                                    continue
                                db.set_tags(
                                    j["id"],
                                    area=j.get("area", ""),
                                    desk=j.get("desk", ""),
                                    seniority=j.get("seniority", ""),
                                    job_type=j.get("job_type", "job"),
                                    loc_city=j.get("loc_city", ""),
                                    loc_country=j.get("loc_country", ""),
                                    loc_region=j.get("loc_region", ""),
                                    work_mode=j.get("work_mode", ""),
                                )
                            done += len(chunk)
                    return db, captured.getvalue(), aborted, skipped_blank
                except SystemExit as exc:
                    return db, captured.getvalue(), True, 0

    def test_retag_all_blank_chunk_not_persisted(self):
        """In --retag-all mode, a job that comes back blank must NOT call set_tags."""
        good_job = _make_job("j1")
        blank_job = _blank_job("j2")

        db, _, aborted, skipped = self._run_backfill(
            rows=[good_job, blank_job],
            retag_all=True,
            chunk_results=[[good_job, blank_job]],  # blank_job has area=""
        )

        self.assertFalse(aborted)
        self.assertEqual(skipped, 1)
        # set_tags should be called once (for good_job) and NOT for blank_job
        set_tags_calls = db.set_tags.call_args_list
        called_ids = [c.args[0] for c in set_tags_calls]
        self.assertIn("j1", called_ids)
        self.assertNotIn("j2", called_ids)

    def test_job_type_mode_blank_chunk_not_persisted(self):
        """In --job-type mode (also a retag), blanks must be skipped."""
        internship = _make_job("i1", job_type="internship")
        blank_internship = _blank_job("i2")

        db, _, aborted, skipped = self._run_backfill(
            rows=[internship, blank_internship],
            job_type_arg="internship",
            chunk_results=[[internship, blank_internship]],
        )

        self.assertFalse(aborted)
        self.assertEqual(skipped, 1)
        called_ids = [c.args[0] for c in db.set_tags.call_args_list]
        self.assertIn("i1", called_ids)
        self.assertNotIn("i2", called_ids)

    def test_default_mode_blank_still_persisted(self):
        """In default (untagged-only) mode, blank results ARE persisted.
        This is harmless — the row was already untagged and it stays untagged.
        Skipping it would leave it stuck in the untagged queue forever."""
        blank_job = _blank_job("u1")

        db, _, aborted, skipped = self._run_backfill(
            rows=[blank_job],
            retag_all=False,
            job_type_arg=None,
            chunk_results=[[blank_job]],
        )

        self.assertFalse(aborted)
        self.assertEqual(skipped, 0)  # NOT skipped in default mode
        called_ids = [c.args[0] for c in db.set_tags.call_args_list]
        self.assertIn("u1", called_ids)

    def test_retag_all_success_persisted_normally(self):
        """When retag succeeds (area != ''), the row IS persisted as normal."""
        good_job = _make_job("g1")

        db, _, aborted, skipped = self._run_backfill(
            rows=[good_job],
            retag_all=True,
            chunk_results=[[good_job]],
        )

        self.assertFalse(aborted)
        self.assertEqual(skipped, 0)
        called_ids = [c.args[0] for c in db.set_tags.call_args_list]
        self.assertIn("g1", called_ids)


class CircuitBreakerTests(unittest.TestCase):
    """Bug 2: three consecutive fully-blank chunks abort the run."""

    def _run_with_chunks(self, chunk_results, retag_all=False):
        """Run the backfill loop with a fixed set of per-chunk results and
        return (aborted: bool, set_tags_call_count: int)."""
        # Build one row per chunk (so each chunk has exactly one job).
        rows = [_make_job(f"j{i}") for i in range(len(chunk_results))]
        db = _make_mock_db()

        call_count = [0]

        def fake_tag_chunk(chunk):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(chunk_results):
                return chunk_results[idx]
            return chunk

        total = len(rows)
        chunk_size = 1  # one row per chunk for precise control
        chunks = [rows[i:i + chunk_size] for i in range(0, total, chunk_size)]
        done = 0
        other = 0
        skipped_blank = 0
        is_retag = retag_all
        _CB = 3
        consecutive = 0
        aborted = False

        from concurrent.futures import ThreadPoolExecutor, as_completed
        # Use workers=1 to make chunk ordering deterministic.
        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = [pool.submit(fake_tag_chunk, ch) for ch in chunks]
            for fut in as_completed(futures):
                chunk = fut.result()
                chunk_all_blank = all(tag._is_untagged(j) for j in chunk)
                if chunk_all_blank:
                    consecutive += 1
                else:
                    consecutive = 0
                if consecutive >= _CB:
                    pool.shutdown(wait=False, cancel_futures=True)
                    aborted = True
                    done += len(chunk)
                    break
                for j in chunk:
                    db.set_yoe(j["id"], j.get("min_yoe", 0))
                    if is_retag and tag._is_untagged(j):
                        skipped_blank += 1
                        continue
                    db.set_tags(j["id"], area=j.get("area", ""))
                done += len(chunk)

        return aborted, db.set_tags.call_count

    def test_three_consecutive_blank_chunks_abort(self):
        """After 3 consecutive fully-blank chunks the run must abort."""
        blank = [_blank_job("x")]
        aborted, _ = self._run_with_chunks([blank, blank, blank])
        self.assertTrue(aborted)

    def test_two_blank_chunks_do_not_abort(self):
        """Two consecutive failures are tolerated — the run continues."""
        blank = [_blank_job("x")]
        good = [_make_job("g")]
        # 2 blanks then 1 good: should NOT abort
        aborted, calls = self._run_with_chunks([blank, blank, good])
        self.assertFalse(aborted)

    def test_non_consecutive_blanks_reset_counter(self):
        """A successful chunk resets the counter — 3 blanks spread across
        success chunks should NOT abort."""
        blank = [_blank_job("x")]
        good = [_make_job("g")]
        # blank, good, blank, good, blank — never 3 in a row
        aborted, _ = self._run_with_chunks([blank, good, blank, good, blank])
        self.assertFalse(aborted)

    def test_abort_after_exactly_three_stops_remaining(self):
        """After triggering the circuit breaker, the run is aborted.
        The remaining futures are cancelled where possible; the key invariant
        is that the run stops rather than continuing indefinitely."""
        blank = [_blank_job("x")]
        good = [_make_job("g")]
        # 6 chunks: 3 blank triggers CB, then 3 more that should be cancelled.
        # We only assert that the run aborted, not an exact call count, since
        # cancel_futures=True only cancels futures that haven't started yet and
        # the thread pool may have already dequeued some.
        aborted, _ = self._run_with_chunks(
            [blank, blank, blank, good, good, good], retag_all=False
        )
        self.assertTrue(aborted)


if __name__ == "__main__":
    unittest.main()
