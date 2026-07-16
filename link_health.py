#!/usr/bin/env python3
"""Sampled apply-link health check.

The blind spot this closes: a scraper that keeps returning a stale list (dead
tenant serving cached data, or a changed detail-URL scheme) fills the DB with
jobs whose "Open posting" 404s. Counts look healthy, descriptions look healthy,
fail-loud never fires — nothing else can see it.

Per company with live rows, sample up to ``SAMPLE_PER_COMPANY`` random job URLs
and GET them (curl_cffi browser impersonation, so bot-fight walls don't fake a
death). A company is *dead-linked this run* only if EVERY sampled URL comes back
hard-dead (404/410 — anything else, including 403/5xx/timeouts, is inconclusive:
bot wall or transient, not a missing page). A single dead run can be a same-day
removal (posting pulled between the 04:00 scan and this check), so a company is
only *reported* after ``DEAD_STREAK`` consecutive dead runs; the streak lives in
``link_state.json``. Each run samples fresh random URLs, so a 2-run streak is
two independent pieces of evidence.

Known limitation: SPA detail pages (most Workday tenants) return a 200 shell for
any path, so this check cannot see their deaths — it covers the server-rendered
and API-backed URL schemes, which is exactly the class that has actually rotted.

Wired into ``selfcheck.py`` (weekly, Sunday 03:00 on the M1, live DB); also
runnable directly:

    python3 link_health.py            # check now, print per-company verdicts
    python3 link_health.py --alert    # only streak-confirmed dead; exit 1 if any
"""
import argparse
import json
import os
import random
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# BUG 5a: import curl_cffi at module top level so a broken install raises here
# rather than being swallowed per-URL inside _fetch_status. selfcheck catches
# any ImportError from `import link_health` and routes it to the WARN path
# (selfcheck.py ~line 184), so a broken install is visible immediately instead
# of silently making every fetch "inconclusive" -> every verdict "alive" ->
# update_streaks resetting all accumulated dead-streaks.
from curl_cffi import requests as _cffi_requests

ROOT = Path(__file__).parent
DEFAULT_DB = os.environ.get("JOBS_DB", str(ROOT / "jobs.db"))
STATE = ROOT / "link_state.json"

SAMPLE_PER_COMPANY = 2
# Only these statuses mean "the page is gone". 403 = bot wall, 5xx = server
# trouble, timeouts = network — none of them prove the posting vanished.
DEAD_STATUSES = {404, 410}
# Consecutive dead runs before a company is reported (kills the same-day-removal
# false positive: a posting pulled after the morning scan 404s while its row is
# legitimately still live until the next scan delists it).
DEAD_STREAK = 2
TIMEOUT = 15
THREADS = 8


def _fetch_status(url: str) -> int | None:
    """Final HTTP status after redirects, or None on any transport error."""
    # curl_cffi is imported at module top level (BUG 5a); use it directly here.
    # Transport errors (timeouts, connection refused, etc.) are genuinely
    # inconclusive — bot wall or transient, not a missing page — so they still
    # return None rather than a status code.
    try:
        r = _cffi_requests.get(url, impersonate="chrome", timeout=TIMEOUT,
                               allow_redirects=True)
        return r.status_code
    except Exception:
        return None


def is_dead(status: int | None) -> bool:
    return status in DEAD_STATUSES


def company_verdicts(samples: dict[str, list[tuple[str, int | None]]]) -> dict[str, bool]:
    """company -> dead-this-run. Dead only if >=1 sample and ALL samples dead."""
    return {c: bool(pairs) and all(is_dead(s) for _, s in pairs)
            for c, pairs in samples.items()}


def update_streaks(prev: dict[str, int], verdicts: dict[str, bool]) -> dict[str, int]:
    """Advance per-company dead streaks. Alive or no-longer-checked resets."""
    return {c: prev.get(c, 0) + 1 for c, dead in verdicts.items() if dead}


def _sample_urls(db_path: str) -> dict[str, list[str]]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT company, url FROM seen_jobs "
        "WHERE delisted_at IS NULL AND url LIKE 'http%'"
    ).fetchall()
    conn.close()
    by_company: dict[str, list[str]] = {}
    for company, url in rows:
        by_company.setdefault(company, []).append(url)
    return {c: random.sample(urls, min(SAMPLE_PER_COMPANY, len(urls)))
            for c, urls in by_company.items()}


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def run_check(db_path: str = DEFAULT_DB) -> dict:
    """Sample + fetch + advance streaks. Returns
    {"dead": [confirmed companies], "dead_this_run": [...], "checked": n}."""
    sampled = _sample_urls(db_path)
    flat = [(c, u) for c, urls in sampled.items() for u in urls]
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        statuses = list(ex.map(lambda cu: _fetch_status(cu[1]), flat))
    samples: dict[str, list[tuple[str, int | None]]] = {c: [] for c in sampled}
    for (c, u), s in zip(flat, statuses):
        samples[c].append((u, s))

    verdicts = company_verdicts(samples)

    # BUG 5b: if every single fetch returned None the run is globally
    # inconclusive — curl_cffi may be broken or the host is fully network-dark.
    # In that case, do NOT update streak state and do NOT return "alive" verdicts
    # (which would reset all accumulated dead-streaks). Log and return a sentinel
    # result so the caller (selfcheck) sees checked>0 but no state change.
    all_statuses = [s for _, s in (pair for pairs in samples.values() for pair in pairs)]
    if all_statuses and all(s is None for s in all_statuses):
        print(
            f"WARN: link_health: all {len(all_statuses)} fetches returned None "
            f"(network outage or broken curl_cffi?) — skipping streak update",
            file=sys.stderr,
        )
        state = _load_state()
        return {
            "dead": sorted(c for c, s in state.get("streaks", {}).items()
                           if s >= DEAD_STREAK),
            "dead_this_run": [],
            "checked": len(samples),
            "samples": samples,
            "inconclusive": True,
        }

    state = _load_state()
    streaks = update_streaks(state.get("streaks", {}), verdicts)
    state["streaks"] = dict(sorted(streaks.items()))
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE)

    return {
        "dead": sorted(c for c, s in streaks.items() if s >= DEAD_STREAK),
        "dead_this_run": sorted(c for c, d in verdicts.items() if d),
        "checked": len(samples),
        "samples": samples,
    }


def dead_sources(db_path: str = DEFAULT_DB) -> list[str]:
    """Streak-confirmed dead-linked companies — for selfcheck/alerting."""
    return run_check(db_path)["dead"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--alert", action="store_true",
                    help="print only streak-confirmed dead; exit 1 if any")
    args = ap.parse_args()

    res = run_check(args.db)
    if args.alert:
        if not res["dead"]:
            print(f"link health OK — {res['checked']} companies sampled, "
                  f"none streak-dead")
            return 0
        print(f"DEAD-LINKED companies ({len(res['dead'])}):")
        for c in res["dead"]:
            print(f"  {c}")
        return 1

    for c in sorted(res["samples"]):
        pairs = res["samples"][c]
        mark = ("DEAD*" if c in res["dead"]
                else "dead?" if c in res["dead_this_run"] else "ok")
        detail = ", ".join(str(s) if s is not None else "err" for _, s in pairs)
        print(f"{mark:6s} {c}  [{detail}]")
    print(f"\n{res['checked']} companies sampled | "
          f"{len(res['dead_this_run'])} dead this run | "
          f"{len(res['dead'])} streak-confirmed (>= {DEAD_STREAK} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
