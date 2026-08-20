#!/usr/bin/env python3
"""One-off backfill: canonicalize loc_country and re-apply the widened internship
detector to already-stored rows.

The tagger changes only affect rows tagged *after* the deploy. This retrofits the
two cheap fixes onto the existing corpus:

  1. loc_country -> _canon_country()  (collapses USA/US/United States, UK/England,
     multi-country dumps, localized names -> one canonical name; kills the
     duplicate filter options).
  2. title matches _INTERNSHIP_RE but job_type != 'internship'  ->  set
     job_type='internship' (+ seniority='intern' when it's a junior rung),
     mirroring tag._enforce_internship. Recovers spring-week / insight /
     placement / alternance roles currently mislabelled 'job'/'graduate-programme'
     and sitting in the junior view.

Dry-run by default; pass --apply to write. Run on the M1 (the only live DB):
    ssh m1 'cd ~/projects/job_scraper && .venv/bin/python backfill_normalize.py'          # preview
    ssh m1 'cd ~/projects/job_scraper && .venv/bin/python backfill_normalize.py --apply'  # commit
"""
import argparse
import os
import sqlite3
from collections import Counter

from tag import _canon_country, _INTERNSHIP_RE

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "jobs.db")


def backfill(apply: bool) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # --- 1. country canonicalization ---
    country_changes: list[tuple[str, str, int]] = []  # (old, new, id)
    for row in conn.execute(
        "SELECT id, loc_country FROM seen_jobs WHERE loc_country IS NOT NULL AND loc_country != ''"
    ):
        old = row["loc_country"]
        new = _canon_country(old)
        if new != old:
            country_changes.append((old, new, row["id"]))

    # --- 2. internship re-enforcement ---
    intern_changes: list[tuple[str, str, str, int]] = []  # (title, old_type, new_sen, id)
    for row in conn.execute(
        "SELECT id, title, job_type, seniority FROM seen_jobs "
        "WHERE job_type != 'internship' AND title IS NOT NULL"
    ):
        if not _INTERNSHIP_RE.search(row["title"] or ""):
            continue
        new_sen = row["seniority"]
        if new_sen in ("", "graduate", "analyst", "associate"):
            new_sen = "intern"
        intern_changes.append((row["title"], row["job_type"], new_sen, row["id"]))

    # --- report ---
    print(f"DB: {DB_PATH}")
    print(f"\n[1] country canonicalization: {len(country_changes)} rows change")
    top = Counter((o, n) for o, n, _ in country_changes).most_common(20)
    for (o, n), c in top:
        print(f"      {o!r:45} -> {n!r:22} ({c})")

    print(f"\n[2] internship re-enforcement: {len(intern_changes)} rows -> job_type=internship")
    by_type = Counter(old for _, old, _, _ in intern_changes)
    print(f"      from job_type: {dict(by_type)}")
    for title, old, _, _ in intern_changes[:25]:
        print(f"      [{old:17}] {title[:70]}")
    if len(intern_changes) > 25:
        print(f"      ... and {len(intern_changes) - 25} more")

    if not apply:
        print("\nDRY RUN — no writes. Re-run with --apply to commit.")
        conn.close()
        return

    conn.executemany(
        "UPDATE seen_jobs SET loc_country = ? WHERE id = ?",
        [(new, i) for _, new, i in country_changes],
    )
    conn.executemany(
        "UPDATE seen_jobs SET job_type = 'internship', seniority = ? WHERE id = ?",
        [(sen, i) for _, _, sen, i in intern_changes],
    )
    conn.commit()
    conn.close()
    print(f"\nAPPLIED: {len(country_changes)} country + {len(intern_changes)} internship updates committed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    backfill(ap.parse_args().apply)
