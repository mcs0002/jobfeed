#!/usr/bin/env python3
"""Per-ATS description-quality monitor.

The coverage guard (``scrapers/enrich/coverage.py``) proves every source *has* a
declared description path. This proves the path actually WORKS: it reads the
live DB and, per ATS, measures how many stored rows have a real body vs a
stub/empty one. A source that quietly degrades to nav-chrome stubs (as TAL.net
did) shows up here as a high stub rate — the tagger silently mis-labels those
rows, and nothing else would flag it.

Pure Python, no LLM — cheap to run idle. Wired into ``selfcheck.py`` so the
weekly health run alerts on degraded sources; also runnable directly:

    python3 description_health.py            # full per-ATS table
    python3 description_health.py --alert    # only degraded sources; exit 1 if any
"""
import argparse
import json
import os
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DEFAULT_DB = os.environ.get("JOBS_DB", str(ROOT / "jobs.db"))

# A description shorter than this is treated as a stub (nav chrome / cookie
# banner, not a posting). The healthy medians in the DB are 2k-7k chars; broken
# sources sit under a few hundred, so 800 cleanly separates them.
STUB_CHARS = 800
# Only judge a source once it has enough rows to be meaningful.
MIN_ROWS = 8
# Fraction of a source's rows that may be stub/null before it's "degraded".
DEGRADED_RATIO = 0.5


def _company_to_ats(root: Path) -> dict:
    with open(root / "targets.json") as f:
        targets = json.load(f)
    return {t["name"]: t.get("ats", "unknown") for t in targets}


def per_ats_health(db_path: str = DEFAULT_DB, root: Path = ROOT) -> list[dict]:
    """Return per-ATS description stats, worst (highest stub rate) first.

    Each entry: {ats, rows, median_len, null_pct, stub_pct, degraded}.
    Rows whose company isn't in targets.json are bucketed under '?unmapped'."""
    comp2ats = _company_to_ats(root)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT company, length(coalesce(description, '')) AS dl "
        "FROM seen_jobs WHERE delisted_at IS NULL"
    ).fetchall()
    conn.close()

    buckets: dict[str, list[int]] = {}
    for r in rows:
        ats = comp2ats.get(r["company"], "?unmapped")
        buckets.setdefault(ats, []).append(r["dl"])

    out = []
    for ats, lengths in buckets.items():
        n = len(lengths)
        stub = sum(1 for x in lengths if x < STUB_CHARS)
        nul = sum(1 for x in lengths if x == 0)
        stub_pct = stub / n
        out.append({
            "ats": ats,
            "rows": n,
            "median_len": int(statistics.median(lengths)),
            "null_pct": round(100 * nul / n, 1),
            "stub_pct": round(100 * stub_pct, 1),
            "degraded": n >= MIN_ROWS and stub_pct >= DEGRADED_RATIO,
        })
    out.sort(key=lambda d: (-d["stub_pct"], -d["rows"]))
    return out


def degraded_sources(db_path: str = DEFAULT_DB, root: Path = ROOT) -> list[dict]:
    """The subset of per_ats_health flagged degraded — for selfcheck/alerting."""
    return [h for h in per_ats_health(db_path, root) if h["degraded"]]


def _fmt(h: dict, strategy: dict) -> str:
    strat = strategy.get(h["ats"], "?")
    flag = "  <== DEGRADED" if h["degraded"] else ""
    return ("%-24s %-9s %6d rows  median %6d  null %5.1f%%  stub %5.1f%%%s"
            % (h["ats"], strat, h["rows"], h["median_len"],
               h["null_pct"], h["stub_pct"], flag))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--alert", action="store_true",
                    help="print only degraded sources; exit 1 if any are found")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from scrapers.enrich.coverage import DESCRIPTION_STRATEGY

    health = per_ats_health(args.db)
    degraded = [h for h in health if h["degraded"]]

    if args.alert:
        if not degraded:
            print("description health OK — no degraded sources")
            return 0
        print(f"DEGRADED description sources ({len(degraded)}):")
        for h in degraded:
            print("  " + _fmt(h, DESCRIPTION_STRATEGY))
        return 1

    for h in health:
        print(_fmt(h, DESCRIPTION_STRATEGY))
    print(f"\n{len(degraded)} degraded of {len(health)} sources "
          f"(stub<{STUB_CHARS} chars, >={int(DEGRADED_RATIO*100)}%, "
          f">={MIN_ROWS} rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
