"""How many stated deadlines do the rules miss?

Read-only measurement, run on the M1. Deadlines come only from rules
(structured fields plus jobfeed/deadline_text.py's labelled phrases), which
are exact but only as complete as their phrase list. This samples live rows
the rules left undated, asks the configured structured model (the tagger's
TAG_API_* settings) for an application deadline WITH the sentence it came
from, and keeps an answer only if code can verify it, the same contract as
application limits and the inbox pass:

  1. the quote occurs verbatim in the description (whitespace/case forgiven);
  2. the date's year is written in that quote;
  3. the date passes plausible_deadline (future, within 365 days).

Anything the model says that fails a check is counted as rejected, never as a
find. For each verified find it prints the quote, and whether deadline_text
would have read that sentence on its own: "phrase" means the rules lack that
wording (add a label), "context" means the rules would read the sentence but
missed it in the full text.

    .venv/bin/python -m jobfeed.deadline_llm_check              # 150 rows
    .venv/bin/python -m jobfeed.deadline_llm_check --sample 400

Writes nothing. Cost at Gemini 2.5 Flash rates is a few cents per 150 rows.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sqlite3

from applications.mail_llm import _verbatim
from jobfeed import tag
from jobfeed.deadline_text import stated_deadline
from scrapers.enrich.descriptions import plausible_deadline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_CHARS = 12000  # the full description, unlike the tagger's excerpt
TIMEOUT = 60

SYSTEM = """You read one job posting and report its APPLICATION DEADLINE: the last date by which a candidate must apply, as the posting itself states it.

Rules:
- Copy, never infer. Report a date only if the text explicitly states a closing date / application deadline / "apply by" date for this role.
- NOT a deadline: start dates, programme dates, interview or assessment-centre dates, posting or publication dates, "rolling basis", "as soon as possible", a date with no year you would have to guess.
- `quote` must be copied character for character from the posting and must contain the date as written.
- If there is no stated application deadline, return {"deadline": "", "quote": ""}.
- `deadline` is YYYY-MM-DD."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["deadline", "quote"],
    "properties": {"deadline": {"type": "string"}, "quote": {"type": "string"}},
}


def ask(cfg: dict, company: str, title: str, text: str) -> dict | None:
    import requests

    body = {
        "model": cfg["model"],
        "max_tokens": 400,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"FIRM: {company}\nTITLE: {title}\n\n{text}"}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "deadline", "strict": True, "schema": SCHEMA}},
    }
    body.update(cfg.get("extra") or {})
    try:
        resp = requests.post(f"{cfg['base_url']}/chat/completions",
                             headers={"Authorization": f"Bearer {cfg['api_key']}",
                                      "content-type": "application/json"},
                             json=body, timeout=TIMEOUT)
        resp.raise_for_status()
        out = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        reading = json.loads(out)
        return reading if isinstance(reading, dict) else None
    except Exception:
        return None


def verify(reading: dict | None, text: str) -> tuple[str, str, str]:
    """(verdict, date, quote). verdict: none | found | error | rejected:<why>."""
    if reading is None:
        return "error", "", ""
    when = str(reading.get("deadline", "")).strip()
    quote = str(reading.get("quote", "")).strip()
    if not when and not quote:
        return "none", "", ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
        return "rejected:bad-date", when, quote
    if not quote or not _verbatim(quote, text):
        return "rejected:quote-not-in-text", when, quote
    if when[:4] not in quote:
        return "rejected:year-not-in-quote", when, quote
    if not plausible_deadline(when):
        return "rejected:past-or-far", when, quote
    return "found", when, quote


def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--db", default=os.path.join(ROOT, "jobs.db"))
    args = ap.parse_args()
    cfg = tag._openai_cfg()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        raise SystemExit("TAG_API_BASE_URL / TAG_API_KEY / TAG_API_MODEL are not set.")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    undated = conn.execute(
        "SELECT count(*) FROM seen_jobs WHERE deadline IS NULL AND delisted_at IS NULL"
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT company, title, description FROM jobs_with_description "
        "WHERE deadline IS NULL AND delisted_at IS NULL "
        "AND description IS NOT NULL AND length(description) > 300 "
        "ORDER BY random() LIMIT ?", (args.sample,)).fetchall()
    print(f"Asking {cfg['model']} about {len(rows)} random undated live rows "
          f"(of {undated:,} undated)…", flush=True)

    def one(row):
        company, title, desc = row
        text = desc[:MAX_CHARS]
        return company, title, text, verify(ask(cfg, company, title, text), text)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for i, res in enumerate(ex.map(one, rows), 1):
            results.append(res)
            if i % 25 == 0:
                print(f"  … {i}/{len(rows)}", flush=True)

    tally: dict[str, int] = {}
    for _c, _t, _x, (verdict, _w, _q) in results:
        tally[verdict] = tally.get(verdict, 0) + 1
    found = [r for r in results if r[3][0] == "found"]
    print(f"\n{len(results)} rows read")
    for k in sorted(tally):
        print(f"  {k:28} {tally[k]}")
    if results:
        share = len(found) / len(results)
        print(f"\nVerified deadlines the rules missed: {len(found)} of {len(results)} "
              f"({share:.1%}); across {undated:,} undated rows that is roughly "
              f"{round(share * undated):,}.")
    if found:
        print("\n== verified finds (why = would deadline_text read this sentence alone?)")
        for company, title, _text, (_v, when, quote) in found:
            why = "context" if stated_deadline(quote)[0] else "phrase"
            print(f"  {when}  {why:7}  {company[:22]:22} {title[:34]:34} | {_one_line(quote)[:120]}")
    rejected = [r for r in results if r[3][0].startswith("rejected")]
    if rejected:
        print("\n== rejected model answers (shown so the checks can be judged too)")
        for company, title, _text, (verdict, when, quote) in rejected[:10]:
            print(f"  {verdict:28} {when:10} {company[:22]:22} | {_one_line(quote)[:100]}")


if __name__ == "__main__":
    main()
