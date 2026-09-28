#!/usr/bin/env python3
"""One-time move of seen_jobs.description into the job_descriptions table.

Why: a TEXT column in the middle of seen_jobs put every later column (area,
seniority, delisted_at, deadline, ...) behind the description's overflow
pages, so each filter query read the text to reach them. On the live DB
(58.7k rows, 275 MB of text, 2026-09-23) Browse spent 1.5 s of 1.7 s in
SQLite, and the same queries on a table without the text ran 4-8x faster.

This rewrites seen_jobs, so it is deliberate and guarded:

  1. refuses while a nightly run holds .deliver.lock;
  2. writes a full backup with SQLite's online backup API and checks it
     (row counts + quick_check) before touching the source;
  3. copies, drops both columns and verifies the copied count inside ONE
     transaction, rolling back on any mismatch;
  4. VACUUMs so the freed pages are actually returned and the slim table is
     contiguous (skip with --no-vacuum).

Idempotent: a database already split is reported and left alone.

Restore, if ever needed: stop the web app, `mv <backup> jobs.db`, and check
out the commit before the split.

Usage (on the M1, from the repo):
    .venv/bin/python scripts/split_descriptions.py
    .venv/bin/python scripts/split_descriptions.py --db /tmp/copy.db --no-vacuum
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jobfeed.db import JOB_DESCRIPTIONS_DDL  # noqa: E402

NONEMPTY = "description IS NOT NULL AND description != ''"


def _mb(path: str) -> str:
    return f"{os.path.getsize(path) / 1e6:.0f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.path.join(ROOT, "jobs.db"))
    ap.add_argument("--backup", help="default: jobs.pre-split-<timestamp>.db "
                                     "next to --db")
    ap.add_argument("--no-vacuum", action="store_true")
    args = ap.parse_args()

    if os.path.isdir(os.path.join(ROOT, ".deliver.lock")):
        print("ABORT: .deliver.lock exists, a nightly run is active", file=sys.stderr)
        return 2
    if not os.path.exists(args.db):
        print(f"ABORT: no database at {args.db}", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db, isolation_level=None)
    con.execute("PRAGMA busy_timeout=30000")
    cols = {r[1] for r in con.execute("PRAGMA table_info(seen_jobs)")}
    if "description" not in cols:
        print("already split: seen_jobs has no description column; nothing to do")
        return 0

    rows = con.execute("SELECT count(*) FROM seen_jobs").fetchone()[0]
    texts = con.execute(f"SELECT count(*) FROM seen_jobs WHERE {NONEMPTY}").fetchone()[0]
    print(f"{args.db}: {_mb(args.db)}, {rows} rows, {texts} with a description")

    # 1. Backup, verified before anything changes.
    backup = args.backup or os.path.join(
        os.path.dirname(os.path.abspath(args.db)),
        f"jobs.pre-split-{datetime.now():%Y%m%d-%H%M%S}.db")
    if os.path.exists(backup):
        print(f"ABORT: backup path exists: {backup}", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    dst = sqlite3.connect(backup)
    con.backup(dst)
    b_rows = dst.execute("SELECT count(*) FROM seen_jobs").fetchone()[0]
    b_texts = dst.execute(f"SELECT count(*) FROM seen_jobs WHERE {NONEMPTY}").fetchone()[0]
    check = dst.execute("PRAGMA quick_check").fetchone()[0]
    dst.close()
    if (b_rows, b_texts, check) != (rows, texts, "ok"):
        print(f"ABORT: backup does not match the source "
              f"({b_rows}/{rows} rows, {b_texts}/{texts} texts, check={check}); "
              f"source untouched", file=sys.stderr)
        return 1
    print(f"backup verified: {backup} ({_mb(backup)}, {time.monotonic() - t0:.0f}s)")

    # 2. Copy + drop + verify, atomically.
    t0 = time.monotonic()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(JOB_DESCRIPTIONS_DDL)
        fetched = ("description_fetched_at" if "description_fetched_at" in cols
                   else "NULL")
        con.execute(
            "INSERT OR IGNORE INTO job_descriptions (job_id, description, fetched_at) "
            f"SELECT id, description, {fetched} FROM seen_jobs WHERE {NONEMPTY}")
        moved = con.execute("SELECT count(*) FROM job_descriptions").fetchone()[0]
        if moved != texts:
            raise RuntimeError(f"copied {moved} descriptions, expected {texts}")
        con.execute("ALTER TABLE seen_jobs DROP COLUMN description")
        if fetched != "NULL":
            con.execute("ALTER TABLE seen_jobs DROP COLUMN description_fetched_at")
        if con.execute("SELECT count(*) FROM seen_jobs").fetchone()[0] != rows:
            raise RuntimeError("seen_jobs row count changed during the split")
        con.execute("COMMIT")
    except Exception as exc:
        con.execute("ROLLBACK")
        print(f"ABORT: {exc}; rolled back, source unchanged", file=sys.stderr)
        return 1
    print(f"split: {moved} descriptions moved, 2 columns dropped "
          f"({time.monotonic() - t0:.0f}s)")

    # 3. Reclaim and defragment.
    if not args.no_vacuum:
        t0 = time.monotonic()
        con.execute("VACUUM")
        print(f"vacuum: {_mb(args.db)} ({time.monotonic() - t0:.0f}s)")
    check = con.execute("PRAGMA quick_check").fetchone()[0]
    con.close()
    print(f"quick_check: {check}")
    return 0 if check == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
