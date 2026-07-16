#!/usr/bin/env python3
"""
Backfill / re-tag structured tags onto existing rows.

Tags rows in chunks via tag.tag_jobs and db.set_tags, feeding the tagger the
sector + a cleaned description excerpt (the same signals the live scan uses).
HTML descriptions (old Greenhouse rows) are cleaned to text in place so they're
readable and make good LLM input. Idempotent and resumable — each chunk is
persisted as it completes, so it's safe to stop and re-run.

By default it tags rows that aren't tagged under the current scheme (area = '').
Use --retag-all to re-classify every row (e.g. after a taxonomy change).

--retag-missing-desc-facets is the nightly re-tag hook for the "tagged before
the description arrived" wrinkle: it re-tags rows that WERE tagged (tagged_at
set) but whose description-derived facets (lang_req/education/start_date) are
still NULL because no description existed at tag time — now that one has landed
(via the nightly enrich backstop). Bounded (default 300/run) so the shared Haiku
quota can't blow up; wired into deliver.sh after enrich_descriptions.py.

Usage:
  python3 backfill_tags.py                  # tag rows with no area yet
  python3 backfill_tags.py --retag-all      # re-tag every row (new taxonomy)
  python3 backfill_tags.py --retag-missing-desc-facets  # nightly desc-facet fill (≤300)
  python3 backfill_tags.py --since-days 90  # limit to recent rows
  python3 backfill_tags.py --limit 1000     # cap (quick first pass)
  python3 backfill_tags.py --chunk 30       # rows per Haiku batch
"""
import argparse
import html as _html
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from db import JobDB
from scrapers.enrich.descriptions import _extract_text
from filter import has_experience_wall
import tag

DB_FILE = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))

_COLS = ("id", "title", "company", "location", "category", "description")


# Default per-run bound on the nightly desc-facet re-tag. Keeps the shared Haiku
# quota safe — a large backlog drains over several nights, oldest-first.
RETAG_DESC_FACETS_LIMIT = 300


def patch_desc_lang(db: JobDB, verbose: bool = True) -> int:
    """Pure-Python retro-patch: union the detected posting language into
    lang_req for every already-desc-tagged row (lang_req NOT NULL). No LLM, no
    quota — safe to run over the whole table, delisted included. Rows still
    NULL are left for the LLM re-tag path, which now applies the same merge.
    Returns the number of rows patched."""
    from lang_detect import detect_language
    from web.descfmt import clean_text

    rows = db.conn.execute(
        "SELECT id, title, description, lang_req FROM seen_jobs "
        "WHERE description IS NOT NULL AND description != '' "
        "AND lang_req IS NOT NULL"
    ).fetchall()
    patched = 0
    for i, (job_id, title, desc, lang_req) in enumerate(rows, 1):
        text = clean_text(_extract_text(desc, max_chars=8000), title)
        code = detect_language(text)
        if code and code not in {c for c in lang_req.split(",") if c}:
            merged = tag.merge_detected_lang(lang_req, code)
            db.conn.execute("UPDATE seen_jobs SET lang_req = ? WHERE id = ?",
                            (merged, job_id))
            patched += 1
        if verbose and i % 2000 == 0:
            db.conn.commit()
            print(f"  desc-lang sweep {i}/{len(rows)} (patched {patched})",
                  flush=True)
    db.conn.commit()
    if verbose:
        print(f"desc-lang sweep: {len(rows)} rows scanned, {patched} patched",
              flush=True)
    return patched


def _rows(db: JobDB, retag_all: bool, since_days: int | None,
          limit: int | None, job_type: str | None = None,
          retag_missing_desc_facets: bool = False,
          area: str | None = None,
          title_like: list[str] | None = None) -> list[dict]:
    # Nightly desc-facet re-tag: delegate row selection to the precise DB query
    # (tagged_at set, description present, lang_req still NULL) — bounded so the
    # quota can't blow up. Ignores the other scoping flags by design.
    if retag_missing_desc_facets:
        return db.rows_needing_desc_facet_retag(
            list(_COLS), limit=limit or RETAG_DESC_FACETS_LIMIT
        )
    sql = f"SELECT {', '.join(_COLS)} FROM seen_jobs"
    clauses, params = [], []
    # --job-type scopes a re-tag to one job_type (e.g. re-tag every internship
    # after a taxonomy change) without touching the other 27k rows. It implies
    # retag, so it doesn't also require the untagged (area='') gate.
    if job_type:
        clauses.append("job_type = ?")
        params.append(job_type)
    elif not (retag_all or area or title_like):
        clauses.append("(area IS NULL OR area = '')")
    # Targeted retag scoping (e.g. after a tag_health triage: re-tag the
    # wealth-shaped rows sitting in area='other' once the taxonomy gained a
    # `wealth` value). Both imply retag and are restricted to LIVE rows —
    # burning quota on delisted rows that the UI mostly hides is waste.
    if area:
        clauses.append("area = ?")
        params.append(area)
    if title_like:
        clauses.append("(" + " OR ".join(["title LIKE ?"] * len(title_like)) + ")")
        params.extend(f"%{pat}%" for pat in title_like)
    if area or title_like:
        clauses.append("delisted_at IS NULL")
    if since_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        clauses.append("first_seen >= ?")
        params.append(cutoff)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY first_seen DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    cur = db.conn.execute(sql, params)
    return [dict(zip(_COLS, r)) for r in cur.fetchall()]


def _demojibake(s: str) -> str:
    """Repair historical UTF-8-as-Latin-1 mojibake ('Nestlé' -> 'Nestlé').
    Only touches strings with the tell-tale Ã/Â sequences, and only when the
    round-trip succeeds, so clean text is never corrupted. (Newly scraped rows
    are fixed at the source by scrapers/_http.py; this repairs the backlog.)"""
    if not s or ("Ã" not in s and "Â" not in s):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _clean_text(job: dict) -> bool:
    """If the description is HTML, clean it to text in memory. Returns True if
    it changed (so the caller can persist it). No DB access — thread-safe."""
    desc = job.get("description") or ""
    if desc and ("<" in desc or "&lt;" in desc or "&amp;" in desc):
        cleaned = _extract_text(_html.unescape(desc))
        if cleaned and cleaned != desc:
            job["description"] = cleaned
            return True
    return False


def _tag_chunk(chunk: list[dict]) -> list[dict]:
    """Worker: repair mojibake, clean descriptions, detect YoE walls, then tag.
    No DB writes here — those happen in the main thread (single connection)."""
    for j in chunk:
        orig_title = j.get("title") or ""
        orig_loc = j.get("location") or ""
        orig_desc = j.get("description") or ""
        j["title"] = _demojibake(orig_title)
        j["location"] = _demojibake(orig_loc)
        j["description"] = _demojibake(orig_desc)
        j["_text_changed"] = (j["title"] != orig_title) or (j["location"] != orig_loc)
        _clean_text(j)  # HTML-strip the (now de-mojibake'd) description
        j["_desc_changed"] = (j.get("description") or "") != orig_desc
        _, years = has_experience_wall(j.get("description") or "")
        j["min_yoe"] = years
    tag.tag_jobs(chunk)  # reads category + cleaned description
    return chunk


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill / re-tag Haiku tags")
    ap.add_argument("--retag-all", action="store_true", help="Re-tag every row, not just untagged ones")
    ap.add_argument("--retag-missing-desc-facets", action="store_true",
                    help="Nightly hook: re-tag rows tagged before a description "
                         "arrived (lang_req IS NULL despite description present)")
    ap.add_argument("--job-type", help="Re-tag only rows of this job_type (e.g. 'internship')")
    ap.add_argument("--area", help="Re-tag only live rows currently in this area (e.g. 'other')")
    ap.add_argument("--title-like", action="append",
                    help="Re-tag only live rows whose title contains this substring "
                         "(repeatable, OR-joined; combines with --area)")
    ap.add_argument("--since-days", type=int, help="Only rows first seen in the last N days")
    ap.add_argument("--limit", type=int, help="Cap the number of rows this run")
    ap.add_argument("--chunk", type=int, default=tag.BATCH_SIZE, help="Rows per Haiku batch")
    ap.add_argument("--workers", type=int, default=4, help="Concurrent Haiku batches")
    ap.add_argument("--patch-desc-lang", action="store_true",
                    help="No-LLM sweep: union the detected posting language "
                         "into lang_req on already-tagged rows, then exit")
    args = ap.parse_args()

    db = JobDB(DB_FILE)
    if args.patch_desc_lang:
        patch_desc_lang(db)
        return
    rows = _rows(db, args.retag_all, args.since_days, args.limit, args.job_type,
                 retag_missing_desc_facets=args.retag_missing_desc_facets,
                 area=args.area, title_like=args.title_like)
    total = len(rows)
    mode = (f"re-tag job_type={args.job_type}" if args.job_type
            else "re-tag missing desc-facets" if args.retag_missing_desc_facets
            else f"re-tag area={args.area} title~{args.title_like or '*'}"
            if (args.area or args.title_like)
            else "re-tag ALL" if args.retag_all else "tag untagged")
    print(f"backfill ({mode}): {total} rows, {args.workers} workers, in {DB_FILE}", flush=True)
    if not total:
        return

    chunks = [rows[i:i + args.chunk] for i in range(0, total, args.chunk)]
    done = 0
    other = 0
    skipped_blank = 0
    # In retag modes the rows already have good tags — don't overwrite with blanks
    # from a failed CLI call (quota exhaustion, auth failure, etc.).
    is_retag = bool(args.retag_all or args.job_type
                    or args.retag_missing_desc_facets
                    or args.area or args.title_like)

    # Circuit-breaker: if N consecutive chunks come back ALL untagged, the CLI is
    # dead and we should stop rather than fan out thousands of subprocess spawns.
    _CIRCUIT_BREAKER_THRESHOLD = 3
    consecutive_total_failures = 0

    # Tag chunks concurrently (each spawns its own `claude` subprocess); persist
    # results in this thread as they complete so all SQLite writes are serial.
    aborted = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_tag_chunk, ch) for ch in chunks]
        for fut in as_completed(futures):
            chunk = fut.result()

            # Circuit-breaker check: if every job in this chunk is untagged, the
            # CLI is likely down.  After 3 consecutive total failures, cancel
            # remaining work and abort loudly.
            chunk_all_blank = all(tag._is_untagged(j) for j in chunk)
            if chunk_all_blank:
                consecutive_total_failures += 1
            else:
                consecutive_total_failures = 0

            if consecutive_total_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                print(
                    f"\nERROR: {consecutive_total_failures} consecutive chunks "
                    "returned fully untagged — claude CLI appears to be down "
                    "(quota exhausted, auth failure, or binary missing). "
                    "Cancelling remaining work.",
                    flush=True,
                )
                pool.shutdown(wait=False, cancel_futures=True)
                aborted = True
                done += len(chunk)
                break

            for j in chunk:
                if j.get("_text_changed"):
                    db.conn.execute(
                        "UPDATE seen_jobs SET title = ?, location = ? WHERE id = ?",
                        (j["title"], j["location"], j["id"]),
                    )
                if j.get("_desc_changed"):
                    db.set_description(j["id"], j["description"])
                # Bug 1 fix: in retag mode, skip persisting rows that came back
                # blank so we don't overwrite existing good tags with CLI-failure
                # blanks (quota exhaustion mid-run, auth outage, etc.).
                if is_retag and tag._is_untagged(j):
                    skipped_blank += 1
                    continue
                # min_yoe: the tagger reads it off the (present) description here,
                # so let the LLM value win via set_tags. The regex fallback is
                # only for rows tagged WITHOUT a description (main.py) — every row
                # in a backfill batch has been through tag_jobs with its desc.
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
                    lang_req=j.get("lang_req"),
                    education=j.get("education"),
                    start_date=j.get("start_date"),
                    min_yoe=j.get("min_yoe"),
                )
            done += len(chunk)
            other += sum(1 for j in chunk if j.get("area") == "other")
            skip_msg = f", {skipped_blank} blank skipped (retag guard)" if skipped_blank else ""
            print(f"  {done}/{total}  (other so far: {other}{skip_msg})", flush=True)

    if aborted:
        if skipped_blank:
            print(f"aborted after {done} rows ({skipped_blank} blank skipped)", flush=True)
        else:
            print(f"aborted after {done} rows", flush=True)
        sys.exit(1)

    summary = f"done: {done} processed, {other} -> other"
    if skipped_blank:
        summary += f", {skipped_blank} blank skipped (retag guard)"
    print(summary, flush=True)


if __name__ == "__main__":
    main()
