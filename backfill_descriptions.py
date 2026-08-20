#!/usr/bin/env python3
"""One-shot PARALLEL description backfill for rows already in seen_jobs.

`enrich_descriptions.py` is the polite, single-threaded, throttled backstop for
the rolling window. This is its bulk cousin: when a new enricher lands (Oracle,
Goldman, CSOD, ...) the existing rows for that ATS sit empty because forward-fill
only touches new jobs. The auto-apply pipeline needs the description on the rows
ALREADY in the DB, so we backfill them in parallel by reusing the scan's own
routing — `main._enrich_new_jobs` already groups by ATS, primes Workday/CSOD
sessions per tenant, and persists each fetched description.

We shape each missing row into the job dict that function expects (id + url +
company, plus the Workday `_wd` config for myworkdayjobs rows) and hand it the
real DB. It filters to rows still lacking a description and fills them.

Targets `description IS NULL OR length < MIN_CHARS` — so failed/partial captures
(empty string, chrome-only stubs) get retried, not just untouched NULLs.

Usage:
  ./backfill_descriptions.py                      # all missing, no age window
  ./backfill_descriptions.py --max-age-days 90    # only rows seen in last 90d
  ./backfill_descriptions.py --prefix oracle gs   # only these id prefixes
  ./backfill_descriptions.py --min-chars 200      # treat <200 chars as missing
  ./backfill_descriptions.py --limit 500          # cap rows this run
  ./backfill_descriptions.py --dry-run            # report counts, don't write
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import main  # noqa: E402
from db import JobDB  # noqa: E402
from scrapers.enrich.descriptions import _load_workday_cfgs  # noqa: E402
from scrapers.enrich.workday_enrich import is_workday  # noqa: E402

DEFAULT_DB = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))


def _missing_rows(db: JobDB, min_chars: int, max_age_days: int | None,
                  prefixes: list[str] | None, limit: int | None) -> list[dict]:
    # Acted-on rows (status != 'new') only qualify while description is NULL:
    # filling nothing is safe, but re-fetching a short-but-real captured
    # description on an applied role can OVERWRITE it with an expired-posting
    # tombstone ("this position has been filled") — the one copy that matters
    # is the one we already have. Mirrors db.prune_old_descriptions' guard.
    sql = ("SELECT id, company, title, url FROM seen_jobs "
           "WHERE url IS NOT NULL AND url != '' "
           "AND (description IS NULL "
           "     OR (length(description) < ? AND status = 'new' "
           "         AND applied_at IS NULL))")
    params: list = [min_chars]
    if max_age_days:  # 0 or None => no window
        sql += " AND last_seen >= date('now', ?)"
        params.append(f"-{max_age_days} days")
    if prefixes:
        # ESCAPE: '_' is a single-char LIKE wildcard — unescaped, --prefix sf
        # also sweeps sfapi_/sfX_ ids.
        ors = " OR ".join("id LIKE ? ESCAPE '\\'" for _ in prefixes)
        sql += f" AND ({ors})"
        params.extend(f"{p}\\_%" for p in prefixes)
    sql += " ORDER BY last_seen DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    cur = db.conn.execute(sql, params)
    return [dict(zip(("id", "company", "title", "url"), r)) for r in cur.fetchall()]


def main_cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--max-age-days", type=int, default=None,
                    help="only rows last_seen within N days (default: no window)")
    ap.add_argument("--min-chars", type=int, default=200,
                    help="treat rows with shorter description as missing (default 200)")
    ap.add_argument("--prefix", nargs="*", default=None,
                    help="restrict to these id prefixes, e.g. oracle gs csod")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch", type=int, default=300,
                    help="rows per parallel batch for progress reporting (default 300)")
    ap.add_argument("--timeout", type=int, default=15,
                    help="per-request enrichment timeout (default 15, vs 5 inline)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = JobDB(args.db)
    rows = _missing_rows(db, args.min_chars, args.max_age_days, args.prefix, args.limit)
    if not rows:
        print("no rows need backfill")
        return 0

    from collections import Counter
    by_pre = Counter(r["id"].split("_")[0] for r in rows)
    print(f"{len(rows)} rows missing description "
          f"(min_chars={args.min_chars}, window={args.max_age_days or 'all'}d)")
    for pre, n in by_pre.most_common():
        print(f"  {pre:<14}{n:>6}")
    if args.dry_run:
        return 0

    # Give the backfill more per-request headroom than the inline scan's 5s —
    # Oracle/CSOD detail APIs are slower than a plain GET and we're not racing a
    # scan clock here.
    main.ENRICH_TIMEOUT_SECONDS = args.timeout
    main.WORKDAY_TIMEOUT_SECONDS = max(main.WORKDAY_TIMEOUT_SECONDS, args.timeout)

    wd_cfgs = _load_workday_cfgs()
    jobs = []
    for r in rows:
        j = {"id": r["id"], "url": r["url"], "company": r["company"]}
        if is_workday(r["url"]):
            cfg = wd_cfgs.get(r["company"])
            if cfg:
                j["_wd"] = cfg
        jobs.append(j)

    filled = 0
    t0 = time.time()
    for i in range(0, len(jobs), args.batch):
        chunk = jobs[i:i + args.batch]
        before = sum(1 for j in chunk if j.get("description"))
        main._enrich_new_jobs(chunk, db, dry_run=False)
        got = sum(1 for j in chunk if j.get("description")) - before
        filled += got
        print(f"  [{min(i + args.batch, len(jobs))}/{len(jobs)}] "
              f"+{got} filled (total {filled}, {time.time() - t0:.0f}s)", flush=True)

    print(f"\nbackfilled {filled}/{len(rows)} rows in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
