#!/usr/bin/env python3
"""Propose per-source noise_terms, and refuse any term that could cost a keeper.

noise_terms feed is_relevant() as a PRE-STORE DROP: a dropped role is never
stored, so a wrong term hides real jobs invisibly and forever. The only
defensible way to pick them is mechanically, against the source's own history:

  candidate  = a token/bigram appearing in >= MIN_HITS of this company's
               'other' titles
  admissible = that candidate appears in ZERO keeper titles ANYWHERE in the
               corpus, not merely at this company

The corpus-wide gate is the point. Validating against one source's keepers
is an artifact of sample size: R+V has a single keeper, so 'risk' passed a
local check at an insurer, where risk roles are precisely the target. A word
used in any real finance title across 411 companies is not noise.

Terms are then chosen greedily by how much noise they cover. Anything that
touches a keeper is discarded, whatever its coverage.

Read-only. Prints a proposal; writes nothing.
"""
import re
import sqlite3
import sys
from collections import Counter

DB = __import__("os").path.expanduser("~/projects/job_scraper/jobs.db")
MIN_HITS = 3
MAX_TERMS = 14

WORD = re.compile(r"[a-zäöüßáéíóúàèìòùâêîôûçñ0-9&]+", re.I)


def tokens(title: str) -> set[str]:
    w = WORD.findall(title.lower())
    out = set(t for t in w if len(t) > 3)
    out |= {f"{a} {b}" for a, b in zip(w, w[1:]) if len(a) > 2 and len(b) > 2}
    return out


def global_keeper_tokens(con) -> set[str]:
    """Every token appearing in a non-'other' title anywhere in the corpus."""
    out: set[str] = set()
    for (t,) in con.execute(
        "SELECT title FROM seen_jobs WHERE delisted_at IS NULL "
        "AND area <> 'other' AND title IS NOT NULL"
    ):
        out |= tokens(t)
    return out


def propose(con, company: str, global_keep: set[str]) -> None:
    rows = con.execute(
        "SELECT title, area FROM seen_jobs "
        "WHERE company = ? AND delisted_at IS NULL AND title IS NOT NULL",
        (company,),
    ).fetchall()
    noise = [t for t, a in rows if a == "other"]
    keep = [t for t, a in rows if a != "other"]
    if not noise:
        return

    keep_tokens: set[str] = set()
    for t in keep:
        keep_tokens |= tokens(t)

    counts: Counter = Counter()
    for t in noise:
        for tok in tokens(t):
            counts[tok] += 1

    # Admissible = frequent in noise, absent from every keeper title.
    cands = [(c, n) for c, n in counts.items()
             if n >= MIN_HITS and c not in keep_tokens and c not in global_keep]
    cands.sort(key=lambda x: -x[1])

    chosen: list[str] = []
    uncovered = set(range(len(noise)))
    noise_tok = [tokens(t) for t in noise]
    for cand, _n in cands:
        if len(chosen) >= MAX_TERMS:
            break
        gain = {i for i in uncovered if cand in noise_tok[i]}
        if len(gain) < MIN_HITS:
            continue
        chosen.append(cand)
        uncovered -= gain

    covered = len(noise) - len(uncovered)
    print(f"\n=== {company}")
    print(f"    {len(noise)} noise / {len(keep)} keepers")
    if not chosen:
        print("    no admissible terms (every frequent token also appears in a keeper)")
        return
    print(f"    covers {covered}/{len(noise)} noise ({100*covered/len(noise):.0f}%), "
          f"0 keepers at risk, corpus-wide")
    print(f"    \"noise_terms\": {chosen}")
    # Independent re-check rather than trusting the construction above.
    at_risk = [t for t in keep if any(c in tokens(t) for c in chosen)]
    if at_risk:
        print(f"    !! VERIFY FAILED, would drop keepers: {at_risk[:5]}")
    left = [noise[i] for i in sorted(uncovered)][:6]
    if left:
        print(f"    still noise: {left}")


def main() -> None:
    companies = sys.argv[1:]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        if not companies:
            companies = [
                r[0] for r in con.execute(
                    "SELECT company FROM seen_jobs WHERE delisted_at IS NULL "
                    "GROUP BY company HAVING COUNT(*) >= 15 AND "
                    "100.0*SUM(CASE WHEN area='other' THEN 1 ELSE 0 END)/COUNT(*) >= 85 "
                    "ORDER BY COUNT(*) DESC"
                )
            ]
        global_keep = global_keeper_tokens(con)
        print(f"corpus keeper vocabulary: {len(global_keep):,} tokens")
        for c in companies:
            propose(con, c, global_keep)
    finally:
        con.close()


if __name__ == "__main__":
    main()
