"""How often is the tagger's start_date what the posting says?

Read-only measurement, run on the M1. start_date is not in the gold set's
scored fields (tag.SCORED_FIELDS), so its accuracy has never been measured.
This samples tagged live rows, looks in the FULL description (the tagger
only sees an excerpt) for a sentence that states a start, and compares:

  agree      the stated start and the tagger's value name the same month/year
  disagree   both exist and differ: the rows worth reading
  missed     the text states a start, the tagger left it empty
  unchecked  the tagger has a value, no start sentence found (often the year
             comes from the title, e.g. "2027 Summer Analyst")

    .venv/bin/python -m jobfeed.start_check              # 400 random rows
    .venv/bin/python -m jobfeed.start_check --sample 1000 --show 25

Seasons are compared the way the tagger's rubric maps them (summer=06,
autumn/fall=09, winter=01, spring=03). Writes nothing: the DB is opened
read-only.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from collections import Counter

from jobfeed.deadline_text import _MONTHS, SENTENCE_END

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SEASONS = {"summer": 6, "sommer": 6, "été": 6, "autumn": 9, "fall": 9, "herbst": 9,
            "automne": 9, "winter": 1, "hiver": 1, "spring": 3, "frühjahr": 3,
            "frühling": 3, "printemps": 3}
_LABEL_RE = re.compile(
    r"\b(?:start(?:ing)?\s+date|start(?:ing)?\s+(?:in|on|from)|expected\s+start|"
    r"(?:programme|program|internship|role|position)\s+(?:starts|begins|commences|will\s+start)|"
    r"commenc(?:e|es|ing)\s+(?:in|on)|joining\s+date|"
    r"eintritt(?:sdatum|stermin)?|startdatum|beginn|ab\s+sofort|"
    r"date\s+de\s+(?:début|debut|démarrage|prise\s+de\s+poste)|à\s+partir\s+de)\b", re.I)
_ASAP_RE = re.compile(r"\b(?:asap|as\s+soon\s+as\s+possible|immediate(?:ly)?|ab\s+sofort|"
                      r"dès\s+que\s+possible)\b", re.I)
_MONTH_WORDS = "|".join(sorted(map(re.escape, list(_MONTHS) + list(_SEASONS)),
                               key=len, reverse=True))
_WHEN_RE = re.compile(
    rf"\b(?P<word>{_MONTH_WORDS})\.?\s+(?:of\s+)?(?P<year>20\d\d)\b"
    r"|\b(?P<m>\d{1,2})[./](?P<y2>20\d\d)\b"
    r"|\b\d{1,2}[./](?P<m3>\d{1,2})[./](?P<y3>20\d\d)\b"
    r"|\b(?P<yonly>20\d\d)\b", re.I)


def stated_start(text: str) -> tuple[str, str]:
    """(normalised start, the sentence) from the first start label that is
    followed in its sentence by a date, season or ASAP; ("", "") if none."""
    for label in _LABEL_RE.finditer(text or ""):
        tail = text[label.end():label.end() + 80]
        stop = SENTENCE_END.search(tail)
        tail = tail[:stop.start()] if stop else tail
        quote = re.sub(r"\s+", " ", (label.group(0) + tail)).strip()[:160]
        if _ASAP_RE.search(label.group(0) + " " + tail):
            return "asap", quote
        m = _WHEN_RE.search(tail)
        if not m:
            continue
        g = m.groupdict()
        if g["word"]:
            w = g["word"].lower()
            month = _SEASONS.get(w) or _MONTHS.get(w)
            return f"{g['year']}-{month:02d}", quote
        if g["m"] and 1 <= int(g["m"]) <= 12:
            return f"{g['y2']}-{int(g['m']):02d}", quote
        if g["m3"] and 1 <= int(g["m3"]) <= 12:
            return f"{g['y3']}-{int(g['m3']):02d}", quote
        if g["yonly"]:
            return g["yonly"], quote
    return "", ""


def compare(tagged: str, stated: str) -> str:
    tagged = (tagged or "").strip()
    if not stated:
        return "unchecked" if tagged else "neither"
    if not tagged:
        return "missed"
    if tagged == stated or (len(tagged) == 4 and stated.startswith(tagged)) \
            or (len(stated) == 4 and tagged.startswith(stated)):
        return "agree"
    return "disagree"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sample", type=int, default=400)
    ap.add_argument("--show", type=int, default=15, help="examples per problem bucket")
    ap.add_argument("--db", default=os.path.join(ROOT, "jobs.db"))
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT company, title, start_date, description FROM jobs_with_description "
        "WHERE delisted_at IS NULL AND tagged_at IS NOT NULL "
        "AND description IS NOT NULL AND length(description) > 300 "
        "ORDER BY random() LIMIT ?", (args.sample,)).fetchall()
    tally: Counter = Counter()
    examples: dict[str, list] = {"disagree": [], "missed": []}
    for company, title, tagged, desc in rows:
        stated, quote = stated_start(desc)
        verdict = compare(tagged, stated)
        tally[verdict] += 1
        if verdict in examples and len(examples[verdict]) < args.show:
            examples[verdict].append((company, title, tagged or "—", stated, quote))
    checkable = tally["agree"] + tally["disagree"]
    print(f"{len(rows)} tagged live rows sampled")
    for k in ("agree", "disagree", "missed", "unchecked", "neither"):
        print(f"  {k:10} {tally[k]}")
    if checkable:
        print(f"\nAgreement where both exist: {tally['agree'] / checkable:.0%} of {checkable}")
    for bucket, rows_ in examples.items():
        if rows_:
            print(f"\n== {bucket}")
            for company, title, tagged, stated, quote in rows_:
                print(f"  tagger {tagged:8} text {stated:8} {company[:22]:22} {title[:40]:40} | {quote}")


if __name__ == "__main__":
    main()
