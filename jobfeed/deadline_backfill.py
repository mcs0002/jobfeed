"""Give stored Workday and Oracle roles the closing date their board states.

The enrichers read Workday `endDate` and Oracle `ExternalPostedEndDate` since
2026-09-24, but only for roles they fetch from then on; rows stored earlier
keep a NULL deadline. This re-reads the same detail payloads for live,
undated rows and stores only the date (the description is not touched),
through the same plausible_deadline gate.

    .venv/bin/python -m jobfeed.deadline_backfill              # dry run, 200 rows
    .venv/bin/python -m jobfeed.deadline_backfill --limit 0 --apply

Dry run by default; --apply saves each date as it is found and appends it to
an undo CSV beside the DB, so stopping midway keeps what was stored and a
rerun picks up the rows still undated. Progress prints every 100 rows.
Throttled per request, since it hits live boards.
"""
import argparse
import csv
import os
import time
from datetime import datetime

import requests

from jobfeed.db import JobDB
from scrapers.enrich import oracle_enrich
from scrapers.enrich.descriptions import _load_workday_cfgs
from scrapers.enrich.workday_enrich import WorkdayEnricher, is_workday

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the dates (default: dry run)")
    ap.add_argument("--limit", type=int, default=200, help="rows to check, 0 for all")
    ap.add_argument("--throttle", type=float, default=0.3, help="seconds between requests")
    ap.add_argument("--db", default=os.path.join(ROOT, "jobs.db"))
    args = ap.parse_args()

    db = JobDB(args.db)
    sql = ("SELECT id, company, title, url FROM seen_jobs "
           "WHERE deadline IS NULL AND delisted_at IS NULL "
           "AND (url LIKE '%myworkdayjobs.com%' OR url LIKE '%myworkdaysite.com%' "
           "     OR url LIKE '%/sites/%/job/%') "
           "ORDER BY first_seen DESC")
    rows = db.conn.execute(sql + (" LIMIT ?" if args.limit else ""),
                           (args.limit,) if args.limit else ()).fetchall()
    cfgs = _load_workday_cfgs()
    wd = WorkdayEnricher(timeout=20)
    session = requests.Session()
    total = len(rows)
    print(f"{total} undated Workday/Oracle rows selected"
          f"{'' if args.apply else ' (dry run: nothing is written)'}.", flush=True)

    # --apply saves each date as it is found, with its undo line written
    # first, so an interrupted run keeps what it stored and a rerun resumes:
    # it selects only rows still undated.
    undo = writer = None
    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        undo = os.path.join(os.path.dirname(os.path.abspath(args.db)),
                            f"deadline_backfill_undo_{stamp}.csv")
        undo_fp = open(undo, "w", newline="")
        writer = csv.writer(undo_fp)
        writer.writerow(["id", "deadline_before", "deadline_after"])
        undo_fp.flush()
        print(f"Undo file: {undo}", flush=True)

    found = written = checked = 0
    try:
        for i, (jid, company, title, url) in enumerate(rows, 1):
            if i % 100 == 0:
                print(f"  … {i}/{total} rows, {found} dates found", flush=True)
            out: dict = {}
            try:
                if is_workday(url):
                    cfg = cfgs.get(company) or {}
                    if not (cfg.get("tenant") and cfg.get("board")):
                        continue
                    wd.description(url, cfg["tenant"], cfg["board"],
                                   cfg.get("applied_facets"), out=out)
                elif oracle_enrich.is_oracle(url):
                    oracle_enrich.description(url, session, out=out)
                else:
                    continue
            except Exception as exc:  # one bad payload costs one row
                print(f"  ERR {company}: {type(exc).__name__}: {exc}", flush=True)
                continue
            checked += 1
            when = out.get("deadline")
            if when:
                found += 1
                print(f"{when}  {company[:28]:28}  {title[:60]}", flush=True)
                if writer:
                    writer.writerow([jid, "", when])
                    undo_fp.flush()
                    written += bool(db.set_deadline(jid, when))
            time.sleep(args.throttle)
    finally:
        if writer:
            undo_fp.close()
        print(f"\n{found} of {checked} checked rows state a closing date "
              f"({total} selected).", flush=True)
        if args.apply:
            print(f"Wrote {written} deadlines. Undo: set deadline back to NULL "
                  f"for the ids in {undo}", flush=True)
        else:
            print("Dry run: nothing written. --apply to store them.", flush=True)


if __name__ == "__main__":
    main()
