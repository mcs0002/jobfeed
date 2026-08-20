#!/usr/bin/env python3
"""Re-apply the deterministic tag guards across the whole DB.

_enforce_manager and _enforce_internship are pure functions of the title — no
model, no tokens, no network. But they only run at tag time, so every row tagged
before a guard was tightened keeps whatever label it was given. On 2026-08-20
the Review page surfaced exactly that: "Project Manager", "Trade Coverage" and
"Portfolio Manager - Commercial Banking" all still carried seniority='manager',
which the current guard clears, and the manager label is a HARD GATE — those
roles were invisible in Browse for no reason.

Dry-run by default; --apply writes.

  scripts/reapply_guards.py                # what would change
  scripts/reapply_guards.py --show 40      # with examples
  scripts/reapply_guards.py --apply
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tag  # noqa: E402
from db import JobDB  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "jobs.db"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show", type=int, default=15)
    args = ap.parse_args()

    db = JobDB(args.db)
    cur = db.conn.execute(
        "SELECT id, title, area, seniority, job_type FROM seen_jobs "
        "WHERE COALESCE(area, '') != ''")
    rows = cur.fetchall()

    changes, kinds = [], Counter()
    for jid, title, area, seniority, job_type in rows:
        j = {"title": title or "", "area": area or "",
             "seniority": seniority or "", "job_type": job_type or "job"}
        tag._enforce_internship(j)
        tag._enforce_manager(j)
        if j["seniority"] != (seniority or "") or j["job_type"] != (job_type or "job"):
            kind = (f"seniority {seniority or '-'} -> {j['seniority'] or '-'}"
                    if j["seniority"] != (seniority or "") else
                    f"job_type {job_type} -> {j['job_type']}")
            kinds[kind] += 1
            changes.append((jid, title, kind, j["seniority"], j["job_type"]))

    print(f"{len(rows):,} tagged rows scanned, {len(changes):,} would change\n")
    for kind, n in kinds.most_common():
        print(f"  {n:>6,}  {kind}")

    if args.show and changes:
        print(f"\nexamples ({min(args.show, len(changes))} of {len(changes):,}):")
        import random
        for _, title, kind, _s, _t in random.sample(
                changes, min(args.show, len(changes))):
            print(f"  {kind:<34} {title[:66]}")

    if not args.apply:
        print("\ndry run — pass --apply to write.")
        return 0

    # Write the exact inverse first. This touches thousands of rows in the only
    # live copy of the DB, so "undo" has to be a file, not a restore from
    # yesterday's backup that would also roll back a night of scanning.
    import csv
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    undo = os.path.join(os.path.dirname(os.path.abspath(args.db)),
                        f"reapply_guards_undo_{stamp}.csv")
    before = {jid: None for jid, *_ in changes}
    cur = db.conn.execute(
        "SELECT id, seniority, job_type FROM seen_jobs WHERE id IN "
        f"({','.join('?' * len(before))})", list(before))
    with open(undo, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["id", "seniority", "job_type"])
        w.writerows(cur.fetchall())
    print(f"\nundo written to {undo}")

    for jid, _title, _kind, seniority, job_type in changes:
        db.conn.execute(
            "UPDATE seen_jobs SET seniority = ?, job_type = ? WHERE id = ?",
            (seniority, job_type, jid))
    db.conn.commit()
    print(f"applied to {len(changes):,} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
