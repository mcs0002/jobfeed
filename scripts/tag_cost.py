#!/usr/bin/env python3
"""Aggregate tag_runs.jsonl into per-night cost and health.

tag_debug.log only records failures, so the question "did tagging get more
expensive, and did it actually use the provider I configured" had no answer
after the fact. This reads the append-only record tag.py writes on every
tag_jobs call and rolls it up by night.

  scripts/tag_cost.py                 # last 14 nights
  scripts/tag_cost.py --days 60
  scripts/tag_cost.py --runs          # one line per process instead of per night
"""
import argparse
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_LOG = os.path.join(ROOT, "tag_runs.jsonl")
sys.path.insert(0, ROOT)
from tag_telemetry import load, price  # noqa: E402

# USD per 1M tokens, as (off_peak, peak). Checked against
# https://api-docs.deepseek.com/quick_start/pricing on 2026-08-20.
# Models absent here are reported in tokens only rather than priced from memory.
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--runs", action="store_true",
                    help="group by process instead of by night")
    args = ap.parse_args()

    records = load(args.days)
    if not records:
        print(f"no records in {RUNS_LOG} for the last {args.days} days.\n"
              "tag_runs.jsonl is written by tag.py from 2026-08-20 on; an "
              "empty file just means no tagging run has happened since.")
        return 0

    groups: dict[str, list] = defaultdict(list)
    for ts, rec in records:
        key = rec.get("run_id", "?") if args.runs else ts.date().isoformat()
        groups[key].append((ts, rec))

    print(f"{'night' if not args.runs else 'run':<22} {'rows':>6} {'tag':>6} "
          f"{'in':>9} {'cached':>8} {'out':>7} {'cache%':>7} {'cost':>8}  notes")
    print("-" * 96)
    grand = 0.0
    unpriced = set()
    for key in sorted(groups):
        rows = groups[key]
        tot = tagged = t_in = t_cache = t_out = 0
        cost = 0.0
        priced = True
        providers, models, flags = set(), set(), set()
        for ts, rec in rows:
            tot += rec.get("jobs_total", 0)
            tagged += rec.get("jobs_tagged", 0)
            t_in += rec.get("tokens_in", 0)
            t_cache += rec.get("tokens_cached", 0)
            t_out += rec.get("tokens_out", 0)
            providers.add(rec.get("provider", "?"))
            models.add(rec.get("model", "?"))
            c = price(rec, ts)
            if c is None:
                priced = False
                unpriced.add(rec.get("model", "?"))
            else:
                cost += c
            for f in ("cli_down", "api_fallback", "api_transport_down"):
                if rec.get(f):
                    flags.add(f)
        grand += cost
        pct = f"{100 * t_cache / t_in:.0f}%" if t_in else "-"
        money = f"${cost:.3f}" if priced and t_in else ("$?" if t_in else "-")
        note = "/".join(sorted(models)) + (
            "  ⚠ " + ",".join(sorted(flags)) if flags else "")
        print(f"{key:<22} {tot:>6} {tagged:>6} {t_in:>9,} {t_cache:>8,} "
              f"{t_out:>7,} {pct:>7} {money:>8}  {note}")

    print("-" * 96)
    nights = len(groups) if not args.runs else len({
        ts.date() for ts, _ in records})
    print(f"total ${grand:.2f} over {nights} night(s)"
          + (f"  →  ~${grand / max(nights, 1) * 30:.2f}/month at this rate"
             if grand else ""))
    if unpriced:
        print(f"note: no rate table for {', '.join(sorted(unpriced))} "
              "— those rows are counted in tokens but not in the cost column.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
