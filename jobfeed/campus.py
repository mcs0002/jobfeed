#!/usr/bin/env python3
"""Read the latest campus sweep into rows the web app can render.

WHY THIS EXISTS
`jobfeed/campus_sweep.py` reads every graduate-programme page the ATS scrape cannot
see and writes `campus_sweep/<season>/results.jsonl` plus a 240 KB markdown
report. The report is the wrong surface for actually working the list: it goes
stale the moment the next sweep runs, it cannot be ticked off, and a file on
one machine is not somewhere you check from a phone. This module turns the
same results into rows, and the web app pairs each row with a tick that lives
in `jobs.db`.

Nothing here is a second copy of the data. The sweep output is the only source
for what a programme is and what its page said; `campus_state` in the DB holds
only the user's own marks against it. Re-run the sweep and the page is fresh
without touching a tick.

The status arithmetic (`reconcile_window`) is imported from `campus_sweep`
rather than reimplemented — a window that reads "open" in the report and
"closed" here would be worse than having no page at all.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from jobfeed.campus_sweep import (OUT_ROOT, STATUS_ORDER, _norm, load_walls,
                          reconcile_window)

# A sweep older than this is not wrong, but it is no longer evidence about
# today — autumn windows close inside a month. The page says so out loud
# rather than presenting a stale read as current.
STALE_AFTER_DAYS = 45


def latest_season(root: Path | None = None) -> Path | None:
    """The most recent sweep directory that actually produced results."""
    root = root or OUT_ROOT
    if not root.is_dir():
        return None
    seasons = [d for d in root.iterdir()
               if d.is_dir() and (d / "results.jsonl").is_file()]
    return max(seasons, key=lambda d: d.name) if seasons else None


def _key(firm: str, programme: str, locations: str = "") -> str:
    """Stable id for one programme at one firm, across sweeps.

    Location is part of the identity, not decoration: Virtu runs the same 2027
    quant internship in Dublin, Singapore and Austin as three separate
    applications, and one tick standing for all three would say he had applied
    where he had not. Firms whose caps count locations separately (the
    `/limits` page) make that distinction load-bearing.

    Programme and location text comes from a model reading a page, so wording
    can shift between seasons and a shifted key costs one re-tick. That is the
    right way round — silently carrying a tick onto a different programme, or
    a different city, would be worse."""
    parts = [_norm(programme) or "programme"]
    if locations:
        parts.append(_norm(locations)[:60])
    return f"{_norm(firm)}|" + "|".join(parts)


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _deadline_date(text: str) -> date | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    if not m:
        return None
    try:
        return date(*(int(g) for g in m.groups()))
    except ValueError:
        return None


_CACHE: dict = {"stamp": None, "data": None}


def load_rows_cached() -> dict:
    """`load_rows` for the web app, re-parsed only when the sweep changes.

    643 programmes out of a 400 KB jsonl is cheap but not free, and every tick
    re-renders one row. Keyed on the season directory and the results file's
    mtime, so a sweep finishing mid-session is picked up on the next request
    without a restart."""
    season = latest_season()
    today = date.today()
    stamp = ((season.name, (season / "results.jsonl").stat().st_mtime, today)
             if season else (None, None, today))
    if _CACHE["stamp"] != stamp or _CACHE["data"] is None:
        _CACHE["data"] = load_rows(season, today)
        _CACHE["stamp"] = stamp
    return _CACHE["data"]


def load_rows(season: Path | None = None, today: date | None = None) -> dict:
    """Every programme found by the latest sweep, one row per programme.

    Firms whose page carried no programme, and firms that are known browser
    walls, are counted but not returned as rows: there is nothing to tick.
    They stay in the counts so the page never implies the sweep saw less than
    it did."""
    season = season or latest_season()
    # Local date, not UTC: "37 days left" changing to 36 at 02:00 Berlin
    # because the countdown is computed in UTC is a wrong answer to the only
    # question this column is asked.
    today = today or date.today()
    results = season / "results.jsonl" if season else None
    if results is None or not results.is_file():
        # No sweep has run on this machine, or the season directory lost its
        # results between the scan and the read. Either way the page says so
        # rather than 500ing — an empty grad list is a true statement about
        # what we know, a stack trace is not.
        return {"season": season.name if season else "", "swept_on": "",
                "rows": [], "walls": [], "counts": {}, "n_firms": 0,
                "stale_days": None}

    walls = load_walls()
    rows: list[dict] = []
    wall_rows: list[dict] = []
    counts = {"open": 0, "opens_later": 0, "unclear": 0, "closed": 0,
              "none": 0, "wall": 0, "error": 0}
    n_firms = 0
    swept_on = ""

    for line in results.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        n_firms += 1
        swept_on = max(swept_on, (r.get("read_at") or "")[:10])
        wall = walls.get(_norm(r.get("name", "")))
        if r.get("error") or r.get("result", {}).get("parse_error"):
            if wall:
                counts["wall"] += 1
                wall_rows.append({"firm": r.get("name", ""), "url": r.get("url", ""),
                                  "reason": wall.get("reason", "")})
            else:
                counts["error"] += 1
            continue
        progs = r.get("result", {}).get("programmes") or []
        if not progs:
            counts["none"] += 1
            continue
        for prog in progs:
            prog, derived = reconcile_window(prog, today)
            status = prog.get("status")
            status = status if status in STATUS_ORDER else "unclear"
            counts[status] += 1
            deadline = _clean(prog.get("deadline"))
            due = _deadline_date(deadline)
            # Some firms apply by email (Walter Scott's internship is a
            # mailto:). The link filter strips any non-http scheme, so a
            # mailto would silently render as a dead link — show the address
            # as text instead and point the link at the programme page.
            apply_url = _clean(prog.get("apply_url"))
            http = apply_url.lower().startswith(("http://", "https://"))
            link = apply_url if http else r.get("url", "")
            apply_via = "" if http else apply_url
            rows.append({
                "key": _key(r.get("name", ""), _clean(prog.get("name")),
                            _clean(prog.get("locations"))),
                "firm": r.get("name", ""),
                "category": r.get("category", ""),
                "programme": _clean(prog.get("name")) or "Programme",
                "status": status,
                "deadline": deadline,
                "days_left": (due - today).days if due else None,
                "start": _clean(prog.get("start")),
                "intake": _clean(prog.get("intake_year")),
                "locations": _clean(prog.get("locations")),
                "tracks": _clean(prog.get("tracks")),
                "quote": _clean(prog.get("quote")),
                "notes": _clean(r.get("result", {}).get("notes")),
                "derived_note": derived,
                "url": link,
                "apply_via": apply_via,
                "page_url": r.get("url", ""),
                "why": r.get("why", ""),
            })

    rows.sort(key=lambda x: (STATUS_ORDER[x["status"]],
                             x["days_left"] if x["days_left"] is not None else 9999,
                             x["firm"].lower()))
    stale = None
    if swept_on:
        d = _deadline_date(swept_on)
        stale = (today - d).days if d else None
    return {"season": season.name, "swept_on": swept_on, "rows": rows,
            "walls": sorted(wall_rows, key=lambda w: w["firm"].lower()),
            "counts": counts, "n_firms": n_firms, "stale_days": stale}


if __name__ == "__main__":
    data = load_rows()
    print(f"season {data['season']} swept {data['swept_on']} "
          f"({data['stale_days']} days ago) — {data['n_firms']} firms, "
          f"{len(data['rows'])} programmes")
    print(data["counts"])
    for row in data["rows"][:5]:
        print(f"  [{row['status']}] {row['firm']} — {row['programme']} "
              f"({row['deadline'] or 'no date'}) {row['key']}")
