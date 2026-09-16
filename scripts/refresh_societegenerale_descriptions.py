#!/usr/bin/env python3
"""Replace active Société Générale descriptions with structured DOM text.

Dry-run is the default. Pass ``--apply`` to fetch and update the selected rows.
This exists to repair rows stored before the dedicated SG enricher was added;
new rows use that enricher in the normal scan path.
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import JobDB  # noqa: E402
from scrapers.enrich.societegenerale_enrich import description  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="fetch and write descriptions (default: report only)")
    parser.add_argument("--include-delisted", action="store_true",
                        help="also refresh roles no longer on the source board")
    parser.add_argument("--limit", type=int, default=0,
                        help="maximum rows to process; 0 means all selected rows")
    parser.add_argument("--workers", type=int, default=4,
                        help="bounded concurrent fetches (default: 4)")
    parser.add_argument("--db", default=os.environ.get(
        "JOBS_DB", os.path.join(ROOT, "jobs.db")))
    args = parser.parse_args()

    if not os.path.exists(args.db) or os.path.getsize(args.db) == 0:
        print(f"ERROR: DB not found or empty at {args.db}", file=sys.stderr)
        return 1

    db = JobDB(args.db)
    sql = "SELECT id, title, url FROM seen_jobs WHERE id LIKE ?"
    params: list[object] = ["socgen_%"]
    if not args.include_delisted:
        sql += " AND delisted_at IS NULL"
    sql += " ORDER BY first_seen DESC"
    if args.limit > 0:
        sql += " LIMIT ?"
        params.append(args.limit)
    rows = [
        {"id": row[0], "title": row[1], "url": row[2]}
        for row in db.conn.execute(sql, params).fetchall()
    ]

    scope = "all" if args.include_delisted else "active"
    if not args.apply:
        print(
            f"[dry-run] would refresh {len(rows)} {scope} "
            "Société Générale descriptions"
        )
        return 0

    updated = failed = 0
    workers = max(1, min(args.workers, 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        texts = pool.map(lambda row: description(row["url"]), rows)
        results = zip(rows, texts)
        for index, (row, text) in enumerate(results, 1):
            if text and db.set_description(row["id"], text):
                updated += 1
                print(f"[{index}/{len(rows)}] OK   {row['id']}: {row['title'][:60]}")
            else:
                failed += 1
                print(f"[{index}/{len(rows)}] SKIP {row['id']}: {row['title'][:60]}")

    print(f"updated {updated} | skipped {failed} | total {len(rows)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
