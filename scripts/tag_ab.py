#!/usr/bin/env python3
"""A/B a candidate tagging provider against the tags already in the DB.

the user's own rule (vault doctrine): validate on the exact rows before any
mass re-tag. This re-tags a random sample of ALREADY-TAGGED rows through the
OpenAI-compatible transport and reports how often it agrees with the labels
currently stored, field by field.

It writes NOTHING. Run it on the M1, where the live DB and the .env live:

    TAG_PROVIDER=api \\
    TAG_API_BASE_URL=https://api.deepseek.com/v1 \\
    TAG_API_MODEL=deepseek-v4-pro \\
    .venv/bin/python scripts/tag_ab.py --n 200

Agreement is not the same as correctness — both sides can be wrong together.
Read the disagreement samples; that is the part that tells you something.
"""
import argparse
import copy
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tag  # noqa: E402

# Fields worth comparing. `area` is the one the UI is built on, so it leads.
FIELDS = ("area", "desk", "seniority", "job_type", "loc_country", "work_mode")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200, help="rows to sample")
    ap.add_argument("--db", default="jobs.db")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--show", type=int, default=25, help="disagreements to print")
    ap.add_argument("--field", default="area", choices=FIELDS,
                    help="which field's disagreements to list (default: area)")
    args = ap.parse_args()

    if tag._provider() != "api":
        print("TAG_PROVIDER is not the API transport — nothing to compare against.",
              file=sys.stderr)
        return 2
    cfg = tag._openai_cfg()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        print("TAG_API_BASE_URL / _API_KEY / _MODEL must all be set.",
              file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(
        """SELECT title, company, location, category, description,
                  area, desk, seniority, job_type, loc_country, work_mode
           FROM seen_jobs
           WHERE area IS NOT NULL AND area <> ''
             AND description IS NOT NULL AND description <> ''
             AND delisted_at IS NULL"""
    ).fetchall()
    random.seed(args.seed)
    rows = random.sample(rows, min(args.n, len(rows)))
    print(f"comparing {len(rows)} already-tagged rows against "
          f"{cfg['model']} @ {cfg['base_url']}\n")

    jobs = [{"title": r[0], "company": r[1], "location": r[2],
             "category": r[3], "description": r[4]} for r in rows]
    baseline = [dict(zip(FIELDS, r[5:])) for r in rows]

    t0 = time.monotonic()
    tag.tag_jobs(jobs)
    elapsed = time.monotonic() - t0
    h = tag.LAST_RUN_HEALTH

    tagged = sum(1 for j in jobs if j.get("area"))
    print(f"tagged {tagged}/{len(jobs)} in {elapsed:.0f}s "
          f"({h['batches_ok']}/{h['batches_total']} batches ok)")
    if h["failure_reasons"]:
        print("failures:", h["failure_reasons"][:3])

    tin, tout, tcache = h["tokens_in"], h["tokens_out"], h["tokens_cached"]
    if tin:
        print(f"\ntokens: {tin:,} in ({tcache:,} cached = "
              f"{100 * tcache / tin:.0f}%), {tout:,} out")
        # Divide by rows ATTEMPTED, not rows tagged — dividing by `tagged`
        # inflates the figure by exactly the failure rate and made a healthy
        # ~745 tok/role read as 5,218.
        attempted = h["batches_total"] * tag.BATCH_SIZE
        print(f"        ~{tin / max(attempted, 1):,.0f} input tokens/role "
              f"(over {attempted} attempted)")

    print("\n=== agreement with the stored baseline labels ===")
    for f in FIELDS:
        pairs = [(b[f] or "", j.get(f) or "")
                 for b, j in zip(baseline, jobs) if j.get("area")]
        if not pairs:
            continue
        same = sum(1 for a, b in pairs if a == b)
        print(f"  {f:14s} {100 * same / len(pairs):5.1f}%  ({same}/{len(pairs)})")

    # The two numbers that actually decide a switch. Agreement is a proxy;
    # these are the consequences.
    live = [(b, j) for b, j in zip(baseline, jobs) if j.get("area")]
    if live:
        b_hid = sum(1 for b, _ in live if b["seniority"] == "manager")
        c_hid = sum(1 for _, j in live if j.get("seniority") == "manager")
        b_oth = sum(1 for b, _ in live if b["area"] == "other")
        c_oth = sum(1 for _, j in live if j.get("area") == "other")
        print(f"\n=== consequences over {len(live)} rows ===")
        print(f"  hidden by the manager gate : baseline {b_hid:3d}  ->  candidate {c_hid:3d}")
        print(f"  dumped into area='other'   : baseline {b_oth:3d}  ->  candidate {c_oth:3d}")
        lost = [(b, j) for b, j in live
                if b["area"] not in ("other", "") and j.get("area") == "other"]
        gained = [(b, j) for b, j in live
                  if b["area"] == "other" and j.get("area") not in ("other", "")]
        print(f"  finance -> other (RECALL LOSS): {len(lost)}")
        print(f"  other -> finance (recall gain): {len(gained)}")

    f = args.field
    print(f"\n=== {f} disagreements (first {args.show}) ===")
    shown = 0
    for b, j in zip(baseline, jobs):
        if not j.get("area") or (b[f] or "") == (j.get(f) or ""):
            continue
        shown += 1
        if shown > args.show:
            break
        print(f"  baseline={b[f] or '-':18s} candidate={j.get(f) or '-':18s} "
              f"{j['company'][:22]}: {j['title'][:56]}")
    if shown == 0:
        print("  none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
