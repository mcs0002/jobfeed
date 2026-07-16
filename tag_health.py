#!/usr/bin/env python3
"""Per-company tag-quality monitor: the area='other' share.

The blind spot this closes: a firm whose every job the tagger dumps into
``area='other'`` becomes invisible — Browse hides the Other area by default and
the Sources deeplink only renders for firms with >=1 non-other job. This is how
PIC sat fully scraped yet unreachable until spotted by eye (its
pension/actuarial roles predated the ``actuarial`` area). The same check
catches tagger regressions after prompt/model changes: a batch that starts
snapping to 'other' shows up here as companies crossing the threshold.

A company is *other-heavy* when it has at least ``MIN_TAGGED`` live tagged rows
and ``OTHER_RATIO`` or more of them are ``area='other'``. That is not proof of
a bug — some firms are genuinely miscellaneous — it is a prompt to look, once:
selfcheck alerts only on the transition into the state.

Pure Python, no LLM. Wired into ``selfcheck.py``; also runnable directly:

    python3 tag_health.py            # full per-company table (worst first)
    python3 tag_health.py --alert    # only other-heavy companies; exit 1 if any
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DEFAULT_DB = os.environ.get("JOBS_DB", str(ROOT / "jobs.db"))

# Only judge a company once it has enough tagged rows to be meaningful.
MIN_TAGGED = 5
# Fraction of tagged rows in area='other' at/above which it's flagged.
OTHER_RATIO = 0.8


def share_other(areas: list[str]) -> float:
    """Fraction of area values that are 'other'. Empty list -> 0.0."""
    return sum(1 for a in areas if a == "other") / len(areas) if areas else 0.0


def per_company_health(db_path: str = DEFAULT_DB) -> list[dict]:
    """Per-company tag stats over live, tagged rows — worst first.

    Each entry: {company, tagged, other, other_pct, other_heavy}. Untagged rows
    (blank/NULL area — tagging failed or hasn't run) are excluded: they say
    nothing about where the tagger *puts* a firm's jobs."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT company, area FROM seen_jobs "
        "WHERE delisted_at IS NULL AND area IS NOT NULL AND TRIM(area) != ''"
    ).fetchall()
    conn.close()

    buckets: dict[str, list[str]] = {}
    for company, area in rows:
        buckets.setdefault(company, []).append(area)

    out = []
    for company, areas in buckets.items():
        n = len(areas)
        ratio = share_other(areas)
        out.append({
            "company": company,
            "tagged": n,
            "other": sum(1 for a in areas if a == "other"),
            "other_pct": round(100 * ratio, 1),
            "other_heavy": n >= MIN_TAGGED and ratio >= OTHER_RATIO,
        })
    out.sort(key=lambda d: (-d["other_pct"], -d["tagged"]))
    return out


def other_heavy_sources(db_path: str = DEFAULT_DB) -> list[dict]:
    """The subset flagged other-heavy — for selfcheck/alerting."""
    return [h for h in per_company_health(db_path) if h["other_heavy"]]


def _fmt(h: dict) -> str:
    flag = "  <== OTHER-HEAVY" if h["other_heavy"] else ""
    return ("%-40s %4d tagged  %4d other  %5.1f%%%s"
            % (h["company"], h["tagged"], h["other"], h["other_pct"], flag))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--alert", action="store_true",
                    help="print only other-heavy companies; exit 1 if any")
    args = ap.parse_args()

    health = per_company_health(args.db)
    heavy = [h for h in health if h["other_heavy"]]

    if args.alert:
        if not heavy:
            print("tag health OK — no other-heavy companies")
            return 0
        print(f"OTHER-HEAVY companies ({len(heavy)}):")
        for h in heavy:
            print("  " + _fmt(h))
        return 1

    for h in health:
        print(_fmt(h))
    print(f"\n{len(heavy)} other-heavy of {len(health)} companies "
          f"(>= {int(OTHER_RATIO * 100)}% area=other, >= {MIN_TAGGED} tagged rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
