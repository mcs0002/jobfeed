"""Do Workday and Oracle detail payloads carry a real application deadline?

Read-only probe, run on the M1 (the only machine with the live DB and ATS
network access). The enrichers already fetch these payloads for the body and
discard everything else; this reports what the discarded date fields hold,
so we decide from evidence whether to store them.

    .venv/bin/python scripts/probe_ats_deadlines.py            # 40 of each
    .venv/bin/python scripts/probe_ats_deadlines.py --per-ats 80

For each sampled live row it prints every date-looking field the payload
carries (Workday jobPostingInfo.endDate / timeLeftToApply / startDate,
Oracle ExternalPostedEndDate / ExternalPostedStartDate), then a summary:
how often an end date is present, and how far it sits from the posting date.
The warning sign is a gap that is always the same (30 / 60 / 90 days): that
is an automatic posting expiry, not a closing date, and storing it would
hide live roles once it passes (the web app hides past-deadline roles).

Writes nothing: the DB is opened read-only.
"""
import argparse
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import date
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from scrapers.enrich import oracle_enrich, workday_enrich  # noqa: E402
from scrapers.enrich.descriptions import _load_workday_cfgs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATE_HINTS = ("date", "end", "expir", "close", "deadline", "left")


def _dates(obj: dict) -> dict:
    return {k: v for k, v in obj.items()
            if any(h in k.lower() for h in _DATE_HINTS)
            and isinstance(v, (str, int, float)) and str(v).strip()}


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _sample(conn, where: str, n: int) -> list[tuple]:
    return conn.execute(
        "SELECT id, company, url, first_seen FROM seen_jobs "
        f"WHERE delisted_at IS NULL AND {where} ORDER BY first_seen DESC LIMIT ?",
        (n,)).fetchall()


def probe_workday(conn, n: int) -> list[dict]:
    cfgs = _load_workday_cfgs()
    enr = workday_enrich.WorkdayEnricher(timeout=20)
    out = []
    for jid, company, url, first_seen in _sample(
            conn, "(url LIKE '%myworkdayjobs.com%' OR url LIKE '%myworkdaysite.com%')", n * 3):
        if len(out) >= n:
            break
        cfg = cfgs.get(company)
        if not cfg:
            continue
        du = workday_enrich.detail_url(url, cfg["tenant"], cfg["board"])
        base = "{0.scheme}://{0.netloc}".format(urlsplit(url))
        s = enr._session(base, cfg["tenant"], cfg["board"], cfg.get("applied_facets"))
        try:
            r = s.get(du, headers=workday_enrich._HEADERS, timeout=20)
            info = (r.json().get("jobPostingInfo") or {}) if r.ok else {}
        except (requests.RequestException, ValueError):
            info = {}
        out.append({"ats": "workday", "company": company, "first_seen": first_seen,
                    "fields": _dates(info), "end": info.get("endDate"),
                    "start": info.get("startDate") or first_seen, "ok": bool(info)})
        time.sleep(0.3)
    return out


def probe_oracle(conn, n: int) -> list[dict]:
    out = []
    for jid, company, url, first_seen in _sample(conn, "url LIKE '%/sites/%/job/%'", n):
        m = oracle_enrich._URL_RE.match(url or "")
        if not m:
            continue
        base, site, req = m.groups()
        try:
            r = requests.get(
                f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
                params={"expand": "all", "onlyData": "true",
                        "finder": f"ById;Id={req},siteNumber={site}"},
                headers=oracle_enrich._HEADERS, timeout=20)
            items = r.json().get("items", []) if r.ok else []
        except (requests.RequestException, ValueError):
            items = []
        item = items[0] if items else {}
        out.append({"ats": "oracle", "company": company, "first_seen": first_seen,
                    "fields": _dates(item), "end": item.get("ExternalPostedEndDate"),
                    "start": item.get("ExternalPostedStartDate") or first_seen,
                    "ok": bool(item)})
        time.sleep(0.3)
    return out


def summarise(rows: list[dict]) -> None:
    if not rows:
        return
    ats = rows[0]["ats"]
    ok = [r for r in rows if r["ok"]]
    ends = [r for r in ok if _day(r["end"])]
    gaps = Counter()
    today = date.today()
    past = 0
    for r in ends:
        e, s = _day(r["end"]), _day(r["start"])
        if s:
            gaps[(e - s).days] += 1
        past += e < today
    print(f"\n== {ats}: {len(rows)} sampled, {len(ok)} payloads read, "
          f"{len(ends)} with an end date ({past} already past)")
    field_names = Counter(k for r in ok for k in r["fields"])
    print("   date-like fields seen:", ", ".join(f"{k}×{n}" for k, n in field_names.most_common()))
    if gaps:
        print("   end − posted (days) → count:",
              ", ".join(f"{g}d×{n}" for g, n in sorted(gaps.items())))
        top, top_n = gaps.most_common(1)[0]
        if top_n >= 0.5 * len(ends):
            print(f"   ⚠ {top_n}/{len(ends)} end dates sit exactly {top} days after posting:"
                  " looks like an automatic expiry, not a deadline.")
    for r in ends[:8]:
        print(f"   {r['company'][:30]:30} posted {str(r['start'])[:10]}  end {str(r['end'])[:10]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--per-ats", type=int, default=40)
    ap.add_argument("--db", default=os.path.join(ROOT, "jobs.db"))
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    summarise(probe_workday(conn, args.per_ats))
    summarise(probe_oracle(conn, args.per_ats))


if __name__ == "__main__":
    main()
