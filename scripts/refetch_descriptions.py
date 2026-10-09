#!/usr/bin/env python3
"""Re-fetch stored descriptions for one source through its detail enricher.

Forward-fill only touches rows without a description, and
``backfill_descriptions`` only rows shorter than ``--min-chars``. Neither
reaches a row whose description is long chrome: BCG's stored "descriptions"
were ~3,100 characters of careers-site navigation and an expired-job template
(2026-09-29). When a new or fixed enricher lands, this re-reads that source's
rows through it.

Only the URL-matched detail enricher is used, never the generic GET fallback,
and a row is overwritten only when the enricher returns text: a gone posting or
a failed fetch leaves the stored description alone. Rows without a matching
enricher are counted and skipped.

Dry-run by default; --apply writes. Re-tag afterwards with
``python -m jobfeed.backfill_tags --retag-all --company "<name>"``.

  scripts/refetch_descriptions.py --company BCG
  scripts/refetch_descriptions.py --company BCG --company "Oliver Wyman" --apply
"""
import argparse
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobfeed.db import JobDB  # noqa: E402
from scrapers._http import make_session  # noqa: E402
from scrapers.enrich import detail_enricher  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "jobs.db"))
    ap.add_argument("--company", action="append", required=True,
                    help="exact seen_jobs.company; repeatable")
    ap.add_argument("--include-delisted", action="store_true")
    ap.add_argument("--throttle", type=float, default=0.5)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = JobDB(args.db)
    marks = ",".join("?" for _ in args.company)
    sql = (f"SELECT id, company, url, description FROM jobs_with_description "
           f"WHERE company IN ({marks})")
    if not args.include_delisted:
        sql += " AND delisted_at IS NULL"
    rows = db.conn.execute(sql, args.company).fetchall()

    session = make_session()
    outcome = Counter()
    for jid, company, url, old in rows:
        fn = detail_enricher(url or "")
        if fn is None:
            outcome[(company, "no enricher")] += 1
            continue
        text = fn(url, session)
        time.sleep(args.throttle)
        if not text:
            outcome[(company, "empty, kept")] += 1
        elif text == (old or ""):
            outcome[(company, "unchanged")] += 1
        else:
            outcome[(company, "replaced" if args.apply else "would replace")] += 1
            if args.apply:
                db.set_description(jid, text)

    print(f"{len(rows):,} rows{'' if args.apply else ' (dry run)'}")
    for (company, kind), n in sorted(outcome.items()):
        print(f"  {n:>5,}  {company}: {kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
