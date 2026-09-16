#!/usr/bin/env python3
"""Read-only health report for the scraper corpus.

This exists because the failures that hurt are the ones that look fine. Every
check here was written after a real incident, and each is mechanical on purpose:
a paragraph telling a future reader to be careful decays, a check that prints
CRITICAL does not.

  CORPUS      a purge or a bad migration silently emptying the table.
              2026-09-02: `main.py --company "BIS"` passed the *filtered* target
              list to purge_orphaned_companies and hard-deleted 51,810 rows. The
              row guards held (nothing with a status or a favourite was touched)
              so the damage was invisible in the application tables.

  UNIFORM     a scraper returning the same text for every role. 2026-09-02: the
              first BIS rewrite selected the largest [class*=content] element,
              which was the site's global navigation — four vacancies, four
              byte-identical 7,261-character descriptions, all headed for the
              tagger. Fluent, plausible, wrong.

  IDCASE      id-format churn. BIS ids are stored `bis_JR100469`; the rebuilt
              site exposes the requisition lower case. Taking it verbatim would
              have delisted every live row and re-inserted it as new.

  STALE       a source that quietly stopped producing while the run reported
              success.

  SCOPE       sources whose roles are overwhelmingly `other`. NOT a tagger
              blind spot, which is what the older monitor's wording claimed —
              sampling in 2026-09-02 showed the tags were correct and the
              sources were scraping entire corporate careers boards (Enel
              returning wind technicians, MKS PAMP optics coaters). It is a
              scope problem, and it costs one LLM tagging call per junk row.

Nothing here writes to jobs.db. The state file records only a high-water row
count, so CORPUS has something to compare against.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, "projects/job_scraper/jobs.db")
DEFAULT_STATE = os.path.join(HOME, "projects/job_scraper/.source_health_state.json")
DEFAULT_TARGETS = os.path.join(HOME, "projects/job_scraper/targets.json")

# A drop below this fraction of the high-water mark is treated as data loss
# rather than normal delisting churn.
CORPUS_FLOOR = 0.90
# Sources below this many active rows are too small for a percentage to mean
# anything.
MIN_ROWS_FOR_RATE = 15
SCOPE_THRESHOLD = 85.0
UNIFORM_MIN_SHARERS = 3
STALE_DAYS = 3

findings: list[tuple[str, str, str]] = []


def add(level: str, check: str, message: str) -> None:
    findings.append((level, check, message))


def connect(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        sys.exit(f"ABORT: no database at {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def check_corpus(con: sqlite3.Connection, state: dict) -> None:
    rows = con.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
    companies = con.execute(
        "SELECT COUNT(DISTINCT company) FROM seen_jobs"
    ).fetchone()[0]
    high = state.get("high_water_rows", 0)

    if high and rows < high * CORPUS_FLOOR:
        add("CRITICAL", "CORPUS",
            f"{rows:,} rows, down from a high-water mark of {high:,} "
            f"({100 * rows / high:.0f}%). Normal delisting does not do this. "
            f"Check for an unintended purge before running anything that writes.")
    else:
        add("OK", "CORPUS", f"{rows:,} rows across {companies} companies "
                            f"(high-water {max(high, rows):,})")

    state["high_water_rows"] = max(high, rows)
    state["last_rows"] = rows
    state["last_run"] = datetime.now(timezone.utc).isoformat()


def check_uniform_descriptions(con: sqlite3.Connection) -> None:
    """A scraper grabbing page furniture instead of the posting body.

    Calibration matters more than the idea here. The first cut flagged any
    description shared by 3+ roles and fired on J.P. Morgan, Goldman and 37
    others: a large employer legitimately posts one text across many locations,
    so shared descriptions are normal at scale. The breakage signal is not
    "duplicates exist", it is "ONE description covers essentially the whole
    source" — BIS returned a single navigation blob for 4 of 4 vacancies.

    So measure the dominant description's share, not the count of duplicate
    groups. A noisy check is worse than no check, because it trains the reader
    to skip the report.
    """
    sql = """
        WITH grouped AS (
            SELECT company, description, COUNT(*) c
            FROM seen_jobs
            WHERE delisted_at IS NULL
              AND description IS NOT NULL
              AND LENGTH(description) > 300
            GROUP BY company, description
        ),
        totals AS (
            SELECT company, SUM(c) total, MAX(c) top FROM grouped GROUP BY company
        )
        SELECT company, total, top, ROUND(100.0 * top / total, 0) pct
        FROM totals
        WHERE (total >= 3 AND 100.0 * top / total >= 90)
           OR (total >= 20 AND 100.0 * top / total >= 50)
        ORDER BY pct DESC, total DESC
    """
    hits = con.execute(sql).fetchall()
    if not hits:
        add("OK", "UNIFORM", "no source is dominated by a single description")
        return
    for company, total, top, pct in hits:
        level = "CRITICAL" if pct >= 90 else "WARN"
        add(level, "UNIFORM",
            f"{company}: one description covers {top}/{total} described roles "
            f"({pct:.0f}%). Check the scraper is selecting the posting body and "
            f"not page furniture — this text is being fed to the tagger.")


def check_id_case(con: sqlite3.Connection) -> None:
    """The same requisition stored under two id spellings."""
    sql = """
        SELECT LOWER(id) lid, COUNT(*) c, GROUP_CONCAT(id, ' | ') ids
        FROM seen_jobs GROUP BY lid HAVING c > 1 AND COUNT(DISTINCT id) > 1
        LIMIT 12
    """
    hits = con.execute(sql).fetchall()
    if not hits:
        add("OK", "IDCASE", "no case-variant duplicate ids")
        return
    for _lid, _c, ids in hits:
        add("CRITICAL", "IDCASE",
            f"same id in two spellings: {ids}. A scraper changed id case; "
            f"every live row for that source will be delisted and re-inserted.")


def manual_sources(targets_path: str) -> set[str]:
    """Sources with ats='manual' are never scraped, so they cannot go stale.

    Flagging them is a false positive by construction: Rokos Capital showed up
    in the first STALE list purely because nothing has ever scraped it.
    """
    try:
        with open(targets_path) as fh:
            return {t.get("name") for t in json.load(fh)
                    if t.get("ats") == "manual" and t.get("name")}
    except (OSError, ValueError):
        return set()


def check_stale(con: sqlite3.Connection, manual: set[str]) -> None:
    newest = con.execute("SELECT MAX(last_seen) FROM seen_jobs").fetchone()[0]
    if not newest:
        add("WARN", "STALE", "no last_seen values at all")
        return
    try:
        ref = datetime.fromisoformat(newest)
    except ValueError:
        add("WARN", "STALE", f"unparseable last_seen: {newest!r}")
        return
    cutoff = (ref - timedelta(days=STALE_DAYS)).isoformat()

    sql = """
        SELECT company, MAX(last_seen) ls, COUNT(*) n
        FROM seen_jobs WHERE delisted_at IS NULL
        GROUP BY company HAVING ls < ? ORDER BY ls
    """
    hits = [h for h in con.execute(sql, (cutoff,)).fetchall() if h[0] not in manual]
    if not hits:
        add("OK", "STALE", f"every active source seen within {STALE_DAYS}d of "
                           f"the newest row")
        return
    for company, ls, n in hits:
        add("WARN", "STALE",
            f"{company}: {n} active rows, last seen {ls[:10]} while the corpus "
            f"reached {newest[:10]}. Source may have stopped producing.")


def check_scope(con: sqlite3.Connection) -> None:
    sql = """
        SELECT company, COUNT(*) n,
               ROUND(100.0 * SUM(CASE WHEN area = 'other' THEN 1 ELSE 0 END)
                     / COUNT(*), 0) pct
        FROM seen_jobs WHERE delisted_at IS NULL
        GROUP BY company HAVING n >= ? AND pct >= ?
        ORDER BY n DESC LIMIT 20
    """
    hits = con.execute(sql, (MIN_ROWS_FOR_RATE, SCOPE_THRESHOLD)).fetchall()
    if not hits:
        add("OK", "SCOPE", "no high-volume source is overwhelmingly 'other'")
        return
    waste = sum(int(n * pct / 100) for _c, n, pct in hits)
    add("WARN", "SCOPE",
        f"{len(hits)} source(s) are >={SCOPE_THRESHOLD:.0f}% 'other', roughly "
        f"{waste:,} rows. These tags are usually CORRECT: the source is "
        f"scraping a whole corporate careers board. Narrow the source, do not "
        f"retag. Every one of these rows costs an LLM call.")
    for company, n, pct in hits:
        add("WARN", "SCOPE", f"  {company}: {n} active, {pct:.0f}% other")


def check_descriptions(con: sqlite3.Connection) -> None:
    sql = """
        SELECT
          SUM(CASE WHEN description IS NULL OR TRIM(description) = ''
                   THEN 1 ELSE 0 END),
          SUM(CASE WHEN description IS NOT NULL
                    AND LENGTH(TRIM(description)) BETWEEN 1 AND 199
                   THEN 1 ELSE 0 END),
          COUNT(*)
        FROM seen_jobs WHERE delisted_at IS NULL
    """
    empty, stub, total = con.execute(sql).fetchone()
    if not total:
        return
    bad = (empty or 0) + (stub or 0)
    level = "WARN" if bad > total * 0.05 else "OK"
    add(level, "DESCS",
        f"{empty or 0} empty, {stub or 0} stub (<200 chars) of {total:,} active "
        f"({100 * bad / total:.1f}%)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--targets", default=DEFAULT_TARGETS)
    ap.add_argument("--no-state", action="store_true",
                    help="do not update the high-water mark")
    args = ap.parse_args()

    state: dict = {}
    if os.path.exists(args.state):
        try:
            with open(args.state) as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}

    con = connect(args.db)
    try:
        check_corpus(con, state)
        check_uniform_descriptions(con)
        check_id_case(con)
        check_stale(con, manual_sources(args.targets))
        check_descriptions(con)
        check_scope(con)
    finally:
        con.close()

    order = {"CRITICAL": 0, "WARN": 1, "OK": 2}
    findings.sort(key=lambda f: order.get(f[0], 3))
    width = max(len(c) for _l, c, _m in findings)
    for level, check, message in findings:
        print(f"{level:<8} {check:<{width}}  {message}")

    crit = sum(1 for l, _c, _m in findings if l == "CRITICAL")
    warn = sum(1 for l, _c, _m in findings if l == "WARN")
    print(f"\n{crit} critical, {warn} warning")

    # Only advance the high-water mark when nothing critical fired, so a purge
    # cannot quietly become the new baseline on the next run.
    if not args.no_state and not crit:
        try:
            with open(args.state, "w") as fh:
                json.dump(state, fh, indent=2)
        except OSError as exc:
            print(f"(could not write state: {exc})")

    sys.exit(1 if crit else 0)


if __name__ == "__main__":
    main()
