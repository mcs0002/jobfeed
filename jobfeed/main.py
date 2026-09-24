#!/usr/bin/env python3
"""
Job scraper — checks career pages of financial firms for graduate/junior roles.
Stores + tags new roles in jobs.db for the web app (the only surface).

Usage:
  python3 -m jobfeed              # new jobs only (default)
  python3 -m jobfeed --all        # all current matching jobs (ignore DB state)
  python3 -m jobfeed --dry-run    # run but don't update DB
  python3 -m jobfeed --verify     # test all slugs and report which ones work
"""
import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from jobfeed.envfile import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS_FILE = os.path.join(ROOT, "targets.json")




load_dotenv()
DB_FILE = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))

from jobfeed.db import JobDB
from scrapers.enrich import (ENRICH_LANES, detail_then_http, enrich_lane,
                             enrich_one)
# Inline-lane enrichers (routed explicitly in _enrich_new_jobs; the rest are
# reached via the DETAIL_ENRICHERS registry).
from scrapers.enrich import (
    balyasny_enrich, csod_enrich, goldman_enrich, oracle_enrich,
    talentbrew_enrich, workable_enrich,
)
from jobfeed.deadline_text import stated_deadline
from jobfeed.filter import has_experience_wall, is_relevant
from jobfeed import notify
from jobfeed import tag
from jobfeed.tag import tag_jobs
from scrapers import HANDLERS


# ATSes whose vacancy numbers are per tenant. A target without an id scope
# (see scrapers/talnet.py, scrapers/icims.py) emits a bare `<ats>_<n>` id that
# a second tenant can also emit; the scan then reads the second firm's role as
# already seen and never stores it.
PER_TENANT_ID_ATS = frozenset({"talnet", "icims"})


def foreign_tenant(job_url: str, stored_url: str | None) -> bool:
    """True when a board emitted an id already stored under another tenant's
    host: the cross-tenant collision an unscoped id allows. Unknown hosts on
    either side are not evidence, so they never count."""
    job_host = urlparse(job_url or "").hostname
    stored_host = urlparse(stored_url or "").hostname
    return bool(job_host and stored_host and job_host != stored_host)


def load_targets():
    with open(TARGETS_FILE) as f:
        return json.load(f)


def _select_targets(targets: list[dict], names: list[str]) -> list[dict]:
    """Restrict a run to exact company names, preserving targets.json order."""
    if not names:
        return targets
    wanted = set(names)
    known = {target.get("name", "") for target in targets}
    missing = sorted(wanted - known)
    if missing:
        raise ValueError("unknown company: " + ", ".join(missing))
    return [target for target in targets if target.get("name") in wanted]


def scrape_company(company: dict) -> tuple[list[dict], str | None]:
    """Returns (jobs, error_message). error_message is None on success."""
    ats = company.get("ats", "unknown")
    name = company.get("name", "")
    try:
        handler = HANDLERS.get(ats)
        if handler is not None:
            jobs = handler(company)
        elif ats == "unknown":
            return [], None  # unknown — silently skip (research candidate)
        else:
            # Unrecognized ats (typo/stale value in targets.json): treat as an
            # error so this company is excluded from the delist pass. Returning
            # ([], None) would be indistinguishable from a cleanly-scraped empty
            # board and would mass-delist (and purge) every stored row for this
            # firm on the same run the misconfiguration is first noticed.
            return [], f"ConfigError: unrecognized ats {ats!r}"
        return jobs, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


COMPANY_TIMEOUT = 60  # seconds per company before we give up

# Set when the light-batch wall clock fires: a worker thread is still wedged
# inside a scraper. CPython's concurrent.futures atexit hook JOINS every pool
# thread at interpreter exit, so a normal return from main() would hang the
# process (and launchd's schedule) forever despite the timeout backstop —
# main() checks this after the summary and hard-exits instead.
_LIGHT_BATCH_TIMED_OUT = False


def scrape_targets(targets, workers=6):
    """Scrape companies concurrently while returning results in config order."""
    if workers <= 1:
        results = []
        for company in targets:
            try:
                jobs, error = scrape_company(company)
            except Exception as exc:
                jobs, error = [], f"{type(exc).__name__}: {exc}"
            results.append((company, jobs, error))
        return results

    # IMPORTANT: ThreadPoolExecutor threads are NOT killable. The old code did
    # `future.result(timeout=COMPANY_TIMEOUT)` inside an `as_completed` loop, but
    # as_completed only yields ALREADY-FINISHED futures, so that per-future
    # timeout could never fire — a single hung scraper blocked the whole scan
    # forever (and `shutdown(wait=True)` then blocked again on exit). The real
    # guarantees that keep a light board bounded live in the scrapers: every
    # request carries a hard timeout (scrapers/_http.py) and every paginator has
    # a MAX_PAGES cap. This batch-level wall-clock budget is the backstop: if the
    # whole light batch overruns it we record the unfinished companies as
    # timeouts and tear the pool down WITHOUT waiting, so the scan always makes
    # progress instead of wedging. Slow/JS boards belong on the killable
    # subprocess path (scrape_heavy_targets), not here.
    batches = (len(targets) + workers - 1) // workers
    batch_budget = COMPANY_TIMEOUT * max(1, batches) + 60
    results = [None] * len(targets)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            executor.submit(scrape_company, company): index
            for index, company in enumerate(targets)
        }
        try:
            for future in concurrent.futures.as_completed(futures, timeout=batch_budget):
                index = futures[future]
                company = targets[index]
                try:
                    jobs, error = future.result()
                except Exception as exc:
                    jobs, error = [], f"{type(exc).__name__}: {exc}"
                results[index] = (company, jobs, error)
        except concurrent.futures.TimeoutError:
            global _LIGHT_BATCH_TIMED_OUT
            _LIGHT_BATCH_TIMED_OUT = True
            for future, index in futures.items():
                if results[index] is None:
                    future.cancel()
                    results[index] = (
                        targets[index], [],
                        f"TimeoutError: light batch exceeded {batch_budget}s "
                        f"(a scraper is still running — check its request "
                        f"timeout / page cap, or move it to the heavy "
                        f"subprocess path)",
                    )
    finally:
        # Don't block the scan on a stuck thread; cancel what hasn't started.
        executor.shutdown(wait=False, cancel_futures=True)
    return results


# Heavy boards run one-process-per-company via jobfeed/heavy_scrape.py so a hung
# scraper is killable (a ThreadPoolExecutor thread is not). The timeout is
# generous because that's the whole point — these are the boards that can't
# fit COMPANY_TIMEOUT (JPM's 7k-role tenant, BNP's paced Akamai sweep).
HEAVY_TIMEOUT = 1200  # seconds per heavy company (BNP's paced sweep measured ~960s)


def scrape_heavy_targets(targets):
    """Run each heavy company in its own subprocess, sequentially.

    Returns the same (company, jobs, error) tuples as scrape_targets so the
    caller can merge the two lists transparently. A subprocess that overruns
    HEAVY_TIMEOUT is killed and reported as an error — it cannot wedge the run.
    """
    results = []
    for company in targets:
        name = company["name"]
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "jobfeed.heavy_scrape", "--company", name],
                capture_output=True, text=True, timeout=HEAVY_TIMEOUT, cwd=ROOT,
            )
        except subprocess.TimeoutExpired:
            results.append(
                (company, [], f"TimeoutError: heavy scrape exceeded {HEAVY_TIMEOUT}s")
            )
            continue
        try:
            payload = json.loads(proc.stdout)
            results.append((company, payload.get("jobs", []), payload.get("error")))
        except (ValueError, TypeError):
            tail = (proc.stderr or proc.stdout or "").strip()[-200:]
            results.append((company, [], f"HeavyScrapeError: {tail or 'no output'}"))
    return results


def _split_heavy(targets):
    """Partition into (light, heavy) by the targets.json `heavy` flag."""
    light = [c for c in targets if not c.get("heavy")]
    heavy = [c for c in targets if c.get("heavy")]
    return light, heavy


def scrape_all(targets, workers=6):
    """Scrape every target: light boards on the thread pool, heavy boards on
    their subprocess path at the same time. Results come back light first,
    then heavy, the order callers always saw.

    The heavy boards used to start only after the whole light pool finished,
    so the scan paid for both back to back (30-34 min in September 2026, BNP's
    paced sweep alone ~16 min). They share no hosts with the light pool bar
    one Workday tenant (RBC and RBC Campus), and heavy boards still run one
    after another, so BNP's Akamai pacing is untouched. The heavy thread only
    waits on child processes, each killed at HEAVY_TIMEOUT, so joining it is
    bounded; it is joined before returning, so the light-batch hard exit in
    main() never leaves a heavy child orphaned."""
    light, heavy = _split_heavy(targets)
    heavy_results: list = []
    heavy_error: list = []

    def _heavy():
        try:
            heavy_results.extend(scrape_heavy_targets(heavy))
        except Exception as exc:  # never lose the light results over it
            heavy_error.append(exc)

    thread = threading.Thread(target=_heavy, name="heavy-scrape")
    thread.start()
    try:
        light_results = scrape_targets(light, workers=workers)
    finally:
        thread.join()
    if heavy_error:
        exc = heavy_error[0]
        heavy_results = [(c, [], f"{type(exc).__name__}: {exc}") for c in heavy]
    return light_results + heavy_results


def verify_mode(targets, workers=6):
    """Test every configured target and report what works."""
    print("=== SLUG VERIFICATION RUN ===\n")
    results = {"ok": [], "fail": [], "skip": []}
    configured = [company for company in targets if company.get("ats", "unknown") not in ("unknown", "manual")]
    results["skip"].extend(
        company["name"] for company in targets
        if company.get("ats", "unknown") in ("unknown", "manual")
    )

    scraped = scrape_all(configured, workers=workers)
    for company, jobs, err in scraped:
        ats = company.get("ats", "unknown")
        name = company["name"]
        if err:
            results["fail"].append((name, ats, err))
            print(f"  FAIL  {name} [{ats}] — {err}")
        else:
            results["ok"].append((name, ats, len(jobs)))
            print(f"  OK    {name} [{ats}] — {len(jobs)} jobs found")

    print(f"\nResults: {len(results['ok'])} OK | {len(results['fail'])} FAIL | {len(results['skip'])} not configured")
    return results


ENRICH_TIMEOUT_SECONDS = 5
# Workday needs a prime POST + detail GET per request; give it a little more
# headroom than the generic single-GET path.
WORKDAY_TIMEOUT_SECONDS = 12
ENRICH_WORKERS = 6
# Some hosts serve heavy client-rendered career pages (talentbrew/Radancy SPAs
# like Cargill, Citi) that routinely take longer than the default 5s budget, so
# they always timed out inline and only filled on the nightly backstop. Give
# those id prefixes a longer inline timeout so they enrich on the scan itself.
SLOW_ENRICH_TIMEOUT_SECONDS = 15
SLOW_ENRICH_PREFIXES = ("talentbrew_",)


def _enrich_timeout(job: dict) -> int:
    """Inline enrichment timeout for a job — longer for known-slow SPA hosts."""
    if job.get("id", "").startswith(SLOW_ENRICH_PREFIXES):
        return SLOW_ENRICH_TIMEOUT_SECONDS
    return ENRICH_TIMEOUT_SECONDS


def _enrich_new_jobs(new_jobs: list[dict], db: JobDB, dry_run: bool) -> None:
    """Fetch descriptions concurrently for new_jobs that don't already have
    one, in place. Persists each fetched description so the scheduled
    enrichment pass doesn't re-fetch later. Errors per-job are swallowed —
    enrichment is best-effort and must never break the scan.

    Parallelism note: ENRICH_WORKERS concurrent HTTP fetches across
    different ATSes. Per-thread Session (requests.Session is not safe to
    share across threads). With the negative-only filter (Phase F) letting
    100-300 jobs/scan reach this stage, sequential enrichment was the
    dominant scan-latency component (~30 min). Parallel brings it to a
    handful of minutes.

    Workday is special: its public page is a JS shell, so those jobs go
    through WorkdayEnricher (cxs JSON API + tenant/board config + a primed
    session), one session per tenant, parallelised across tenants."""
    targets = [j for j in new_jobs if not j.get("description") and j.get("url")]
    if not targets:
        return

    def _persist(job: dict, text: str) -> None:
        # A closing date the page stated rides in on the job dict (enrich_one's
        # `out`), and is persisted even when the description itself failed:
        # the two come from different parts of the same page. Failing that,
        # the fetched body may say it in words.
        if not job.get("deadline") and text:
            job["deadline"] = stated_deadline(text)[0]
        if not dry_run and job.get("deadline"):
            try:
                db.set_deadline(job["id"], job["deadline"])
            except Exception:
                pass
        if not text:
            return
        job["description"] = text
        if not dry_run:
            try:
                db.set_description(job["id"], text)
            except Exception:
                pass

    def _pool(jobs: list[dict], fetch) -> None:
        """Run fetch(job, session) across a thread pool with one Session per
        worker thread (requests.Session is not thread-safe to share). Any
        per-job exception becomes '' — enrichment never breaks the scan.
        NB: fetch lambdas read ENRICH_TIMEOUT_SECONDS by name at call time —
        jobfeed/backfill_descriptions.py mutates it on the module."""
        if not jobs:
            return
        tl = threading.local()

        def _one(job: dict) -> tuple[dict, str]:
            s = getattr(tl, "s", None)
            if s is None:
                s = tl.s = requests.Session()
            try:
                return job, fetch(job, s)
            except Exception:
                return job, ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
            for job, text in ex.map(_one, jobs):
                _persist(job, text)

    def _grouped(jobs: list[dict], key, make_enricher, fetch) -> None:
        """Tenant-session enrichers (Workday, CSOD): one enricher (primed
        session) per key(job) group, groups in parallel, rows within a group
        sequential on the shared session."""
        if not jobs:
            return
        groups: dict = {}
        for j in jobs:
            groups.setdefault(key(j), []).append(j)

        def _do(items: list[dict]) -> list[tuple[dict, str]]:
            enr = make_enricher()
            out = []
            for j in items:
                try:
                    out.append((j, fetch(enr, j)))
                except Exception:
                    out.append((j, ""))
            return out

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(ENRICH_WORKERS, len(groups))) as ex:
            for res in ex.map(_do, groups.values()):
                for job, text in res:
                    _persist(job, text)

    # Routing lives in scrapers.enrich.enrich_lane, shared with the backstop.
    # Workday routes by the tenant config the scan carried on the job.
    lanes: dict[str, list[dict]] = {k: [] for k in ENRICH_LANES}
    for j in targets:
        lanes[enrich_lane(j, workday=bool(j.get("_wd")))].append(j)

    _pool(lanes["http"],
          lambda j, s: enrich_one(j["url"], s, timeout=_enrich_timeout(j), out=j))
    _pool(lanes["oracle"],
          lambda j, s: oracle_enrich.description(j["url"], s, timeout=ENRICH_TIMEOUT_SECONDS,
                                                 out=j))
    _pool(lanes["workable"],
          lambda j, s: workable_enrich.description(j["url"], s, timeout=ENRICH_TIMEOUT_SECONDS))
    _pool(lanes["goldman"],
          lambda j, s: goldman_enrich.description(j["url"], s, timeout=ENRICH_TIMEOUT_SECONDS))
    _pool(lanes["detail"],
          lambda j, s: detail_then_http(j, s, timeout=ENRICH_TIMEOUT_SECONDS,
                                        http_timeout=_enrich_timeout(j)))
    _pool(lanes["talentbrew"],
          lambda j, s: talentbrew_enrich.description(j["url"], s, timeout=SLOW_ENRICH_TIMEOUT_SECONDS,
                                                     out=j))

    _grouped(lanes["csod"],
             key=lambda j: (m.group(1) if (m := csod_enrich._URL_RE.match(j.get("url", ""))) else ""),
             make_enricher=lambda: csod_enrich.CsodEnricher(timeout=ENRICH_TIMEOUT_SECONDS),
             fetch=lambda enr, j: enr.description(j["url"]))

    # Balyasny (Salesforce Aura): one enricher, sequential — the first call
    # primes a shared req->recordId map and the board is small.
    if lanes["balyasny"]:
        enr = balyasny_enrich.BalyasnyEnricher(timeout=ENRICH_TIMEOUT_SECONDS)
        for job in lanes["balyasny"]:
            try:
                text = enr.description(job["url"])
            except Exception:
                text = ""
            _persist(job, text)

    if lanes["workday"]:
        from scrapers.enrich.workday_enrich import WorkdayEnricher
        _grouped(lanes["workday"],
                 key=lambda j: (j["_wd"]["tenant"], j["_wd"]["board"]),
                 make_enricher=lambda: WorkdayEnricher(timeout=WORKDAY_TIMEOUT_SECONDS),
                 fetch=lambda enr, j: enr.description(
                     j["url"], j["_wd"]["tenant"], j["_wd"]["board"],
                     j["_wd"].get("applied_facets"), out=j))


# Source-health thresholds — kept identical to jobfeed/selfcheck.py so the scan-written
# state and the weekly selfcheck agree on what "broken" means.
HEALTH_MIN_BASELINE = 5      # must have hit this once before a collapse counts
HEALTH_COLLAPSE_RATIO = 0.2  # at/below this fraction of baseline = degraded
# Age floor for delisting: a role nobody has seen for this long is marked
# delisted regardless of its source's health, because the health guards
# otherwise preserve the final roles of any board that empties. Flagged
# delisted_by_age so the 'other' purge leaves them alone.
AGE_DELIST_DAYS = 21


def _report_skipped_delists(skipped: set[str], zero_now: set[str],
                            raw_counts: dict[str, int]) -> None:
    """Say which skipped sources are empty boards and which are collapses.

    Both are correctly excluded from the delist pass, but only one is a job to
    do, and the old line merged them: on 2026-09-16 it printed seventeen names
    under "degraded/zero", of which eleven were boutiques whose own ATS
    reported zero roles — several empty for all sixty logged runs — while a
    real collapse would have sat unnoticed in the same list. An empty board
    carries how long it has been empty so a long-settled firm reads as settled;
    a collapse carries the drop that triggered it."""
    try:
        state = json.loads(Path(os.path.join(ROOT, "verify_state.json")).read_text())
    except (OSError, ValueError):
        state = {}
    empty_runs, baseline = state.get("empty_runs", {}), state.get("baseline", {})
    empty = sorted(n for n in skipped if n in zero_now)
    collapsed = sorted(n for n in skipped if n not in zero_now)
    if empty:
        print(f"delist: skipping {len(empty)} empty board(s), scraped clean with "
              f"0 roles: " + ", ".join(
                  f"{n} ({empty_runs.get(n, 1)} run(s) empty, best {baseline.get(n, 0)})"
                  for n in empty), flush=True)
    if collapsed:
        print(f"delist: skipping {len(collapsed)} collapsed source(s): " + ", ".join(
            f"{n} ({raw_counts.get(n, 0)} of {baseline.get(n, 0)})"
            for n in collapsed), flush=True)


def _write_health_state(raw_counts: dict[str, int], error_names: set[str],
                        unique_counts: dict[str, int] | None = None,
                        filtered_counts: dict[str, int] | None = None,
                        targets: list[dict] | None = None) -> None:
    """Refresh verify_state.json from this scan's results so the /sources
    'stalled' indicator reflects the latest run, not just the weekly selfcheck.

    Same schema + logic as jobfeed/selfcheck.py: ``failing`` = the scraper errored,
    ``degraded`` = it succeeded but collapsed far below its rolling ``baseline``
    (max jobs ever seen). The scheduled scan covers exactly the verified set
    selfcheck would, so we rewrite failing/degraded wholesale; a recovered
    source simply drops out. Caller guards on a non-empty run so a crashed scan
    can't wipe baselines. Best-effort — never raises into the scan.

    Returns the degraded set: the delist pass excludes those companies, so a
    scraper that silently collapses (returns []/near-[] without raising) can't
    mass-delist its stored rows — and can't trigger the hard-delete of its
    'other'-tagged ones — in the same run the collapse is first observed."""
    path = Path(os.path.join(ROOT, "verify_state.json"))
    try:
        prev = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        prev = {}
    unique_counts = unique_counts or raw_counts
    baseline = dict(prev.get("baseline", {}))

    # Deliberate narrowing is indistinguishable from breakage to the collapse
    # guard, and the baseline only ever ratchets UP (see the loop below), so a
    # source that is intentionally scoped down stays "degraded" forever and is
    # excluded from the delist pass permanently. R+V sat at 1 role against a
    # baseline of 103 for two weeks that way, its 82 superseded rows unable to
    # clear. Every source scoped on 2026-09-02 would have joined it.
    #
    # A target may therefore declare `scoped_at: "YYYY-MM-DD"`. The first run
    # that sees a NEW value drops that source's baseline so it re-learns from
    # the narrowed board. Idempotent: the date is recorded, so a re-run does
    # not keep resetting, and bumping the date is how you request another reset.
    seen_scopes = dict(prev.get("scoped_seen", {}))
    for target in (targets or []):
        name, scoped_at = target.get("name"), target.get("scoped_at")
        if name and scoped_at and seen_scopes.get(name) != scoped_at:
            baseline.pop(name, None)
            seen_scopes[name] = scoped_at
            print(f"health: baseline reset for {name} (scoped_at {scoped_at})",
                  flush=True)
    # Compute degraded BEFORE raising the baseline, so today's low count can't
    # lift its own floor.
    degraded = {
        name for name, n in unique_counts.items()
        if baseline.get(name, 0) >= HEALTH_MIN_BASELINE
        and n <= max(0, int(baseline.get(name, 0) * HEALTH_COLLAPSE_RATIO))
    }
    # Clean-zero backstop, independent of the baseline floor: a firm that
    # returned 0 on a clean scrape (no error — e.g. selector rot returning []
    # silently) but was previously productive is treated as degraded even if
    # its baseline never reached HEALTH_MIN_BASELINE (small boards of 2-4 roles
    # never do). Without this the delist pass would stamp EVERY stored row for
    # such a firm and the 'other' purge would hard-delete them a few days later.
    # A board that legitimately went to zero self-heals: it just stays out of
    # the delist pass, and the rows age out via purge_orphaned_companies only if
    # the source is actually removed from targets.json. `previously productive`
    # = has a prior baseline > 0 (it produced roles on some earlier run).
    degraded |= {
        name for name, n in unique_counts.items()
        if n == 0 and baseline.get(name, 0) > 0
    }
    # How many consecutive runs each board has come back clean and empty. A
    # firm with genuinely nothing open is indistinguishable from a collapsed
    # scraper at the moment it happens, and both are correctly skipped by the
    # delist pass — but they need different responses, and the run that has
    # been empty since July is not the one to investigate. Checked 2026-09-16:
    # of 17 skipped sources, 11 were boutiques whose ATS itself reported zero
    # (Greenhouse `"total":0`, Lever `[]`, "No open positions"), several of
    # them for all 60 logged runs.
    empty_runs = dict(prev.get("empty_runs", {}))
    for name, n in unique_counts.items():
        if n == 0:
            empty_runs[name] = empty_runs.get(name, 0) + 1
        else:
            empty_runs.pop(name, None)

    for name, n in unique_counts.items():
        if n > baseline.get(name, 0):
            baseline[name] = n
    try:
        # Preserve keys owned by other writers (selfcheck's selfcheck_*
        # transition snapshot) and write atomically — a torn read parses as
        # {} and silently resets every rolling baseline.
        prev.update({
            "failing": sorted(error_names),
            "degraded": sorted(degraded),
            "empty_runs": dict(sorted(empty_runs.items())),
            "baseline": dict(sorted(baseline.items())),
            "scoped_seen": dict(sorted(seen_scopes.items())),
            "last_raw_counts": dict(sorted(raw_counts.items())),
            "last_unique_counts": dict(sorted(unique_counts.items())),
            # Rows that survived is_relevant()'s irreversible pre-filter.
            # Kept separately from raw/unique board volume so Stats can show
            # the real acquisition funnel rather than conflating scraping
            # with storage.
            **({"last_filtered_counts": dict(sorted(filtered_counts.items()))}
               if filtered_counts is not None else {}),
            "last_scan_at": datetime.now(timezone.utc).isoformat(),
            "last_clean_scan": {
                **prev.get("last_clean_scan", {}),
                **{name: datetime.now(timezone.utc).isoformat()
                   for name in unique_counts
                   if name not in error_names and name not in degraded},
            },
        })
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(prev, indent=2))
        os.replace(tmp, path)
    except OSError as e:
        print(f"WARN: could not write health state: {e}", file=sys.stderr)
    return degraded


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Report all current openings, not just new ones")
    parser.add_argument("--dry-run", action="store_true", help="Don't update DB")
    parser.add_argument("--verify", action="store_true", help="Test all slugs and report")
    parser.add_argument("--verified-only", action="store_true", help="Only run verified firms (default; retained for cron compatibility)")
    parser.add_argument("--include-unverified", action="store_true", help="Also run configured sources that have not been verified")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent company scrapes (default: 6)")
    parser.add_argument("--no-tag", action="store_true", help="Skip the tagging pass (jobfeed/tag.py)")
    parser.add_argument("--company", action="append", default=[],
                        help="Run one exact target name (repeatable); useful for a quick smoke test")
    args = parser.parse_args()
    scan_t0 = time.monotonic()

    targets = load_targets()
    # Captured BEFORE any filtering. purge_orphaned_companies hard-deletes every
    # company absent from the set it is handed, so it must always see the whole
    # of targets.json and never a subset narrowed by --company. See its call
    # site for what happened when it did not.
    all_target_names = {t["name"] for t in targets}
    try:
        targets = _select_targets(targets, args.company)
    except ValueError as exc:
        parser.error(str(exc))

    if args.verify:
        if args.verified_only:
            targets = [company for company in targets if company.get("verified")]
        verify_mode(targets, workers=max(1, args.workers))
        # verify_mode() calls scrape_targets() / scrape_heavy_targets(), which
        # can set _LIGHT_BATCH_TIMED_OUT when a scraper thread wedges. All
        # verify output is printed (and flushed) by verify_mode before it
        # returns, so the results are complete. Hard-exit for the same reason as
        # the scan path below: CPython's concurrent.futures atexit hook would
        # join the wedged thread forever, hanging the interpreter — and selfcheck
        # would hang with it (BUG 1).
        if _LIGHT_BATCH_TIMED_OUT:
            print("verify: light batch timed out — hard-exiting past the wedged "
                  "scraper thread", flush=True)
            os._exit(0)
        return

    db = JobDB(DB_FILE)

    new_jobs = []
    errors = []
    skipped = []
    checked = 0
    # raw_count per successfully-scraped company (no error). Used after the
    # scrape loop to surface silent-zero alerts: companies that historically
    # had jobs but returned 0 today.
    raw_counts: dict[str, int] = {}
    # Board ids per successfully-scraped company (no error), used after the
    # scrape loop to find stored roles that fell off the board (delisted).
    raw_ids_by_company: dict[str, set] = {}
    # Unique board ids that passed the irreversible pre-filter. This is the
    # bridge between raw source volume and rows eligible for storage; Stats
    # surfaces the aggregate after each completed scan.
    filtered_ids_by_company: dict[str, set] = {}
    # Board ids dropped only by a source's noise_terms; subtracted from the
    # delist comparison (see the scrape loop).
    noise_ids_by_company: dict[str, set] = {}
    # (company, id, board url, stored url) for per-tenant ids that hit another
    # tenant's stored row. Alerted after the loop; see PER_TENANT_ID_ATS.
    id_collisions: list[tuple[str, str, str, str]] = []

    runnable = []
    for company in targets:
        ats = company.get("ats", "unknown")
        name = company["name"]

        if ats in ("unknown", "manual"):
            # unknown = research candidate not yet wired; manual = wanted firm
            # that can't be scraped reliably (see jobfeed/manual_check.py). Both skipped.
            skipped.append(name)
            continue

        if not args.include_unverified and not company.get("verified"):
            skipped.append(name)
            continue
        runnable.append(company)

    scraped = scrape_all(runnable, workers=max(1, args.workers))
    for company, jobs, err in scraped:
        name = company["name"]
        category = company.get("category", "Other")

        if err:
            errors.append((name, err))
            continue

        checked += 1
        raw_counts[name] = len(jobs)
        raw_ids_by_company[name] = {job["id"] for job in jobs}
        filtered_ids_by_company[name] = set()

        # Workday descriptions can't be enriched from the public URL (JS shell);
        # they need the tenant/board config + a primed session. Carry the config
        # on each new job so _enrich_new_jobs can route it to WorkdayEnricher.
        wd_cfg = company.get("workday") if company.get("ats") == "workday" else None

        # Per-source noise terms (e.g. a firm whose bulk the global lists don't
        # know). Kept on the target so one global list isn't a single point of
        # failure when we point the heavy executor at a new noisy board.
        extra_drops = set(company.get("noise_terms", [])) or None

        if not args.dry_run:
            db.begin_batch()
        for job in jobs:
            if not is_relevant(job, category=category, extra_drops=extra_drops):
                # A role dropped only by this source's noise_terms is still on
                # the board, so its raw id kept any copy stored before the term
                # was added active forever (Axpo, ADM, EnBW: 180 rows at
                # 2026-09-14, none tagged anything but 'other'). Take it out of
                # the delist comparison so that copy retires. Deliberately NOT
                # extended to the global filter: the same measurement showed
                # 426 non-'other' rows it rejects today, including front-office
                # titles, and delisting those would hide jobs. Kept separate
                # from raw_ids_by_company, whose sizes feed the health baseline
                # and the age floor's "produced" set.
                if extra_drops and is_relevant(job, category=category):
                    noise_ids_by_company.setdefault(name, set()).add(job["id"])
                continue
            filtered_ids_by_company[name].add(job["id"])
            stored_id = job["id"]
            is_new = args.all or not db.seen(job["id"])
            if (not is_new and not args.all
                    and company.get("ats") in PER_TENANT_ID_ATS):
                stored_url = db.stored_url(job["id"])
                if foreign_tenant(job.get("url", ""), stored_url):
                    # Leave the other firm's row alone and surface the loss;
                    # the fix is an id scope on the colliding target.
                    id_collisions.append(
                        (name, job["id"], job.get("url", ""), stored_url))
                    continue
            # ID-churn guard: some ATSes (Glencore) re-emit the same logical
            # opening under a fresh internal id every scan while the canonical
            # `url` stays constant. The id-keyed db.seen() reads each as brand-
            # new, so without this we'd insert a duplicate row every run — the
            # old (enriched) copy goes delist-eligible and the fresh empty one
            # can hide it at display time. If an existing active row already
            # carries this url under a different id, touch it (bump last_seen /
            # clear delisted_at) and forward-fill any description instead of
            # inserting a duplicate. One indexed lookup per otherwise-new job.
            if is_new and not args.all and not args.dry_run:
                existing_id = db.find_active_by_url(
                    job.get("url", ""), job["id"], company=name,
                    title=job.get("title", ""),
                )
                if existing_id is not None:
                    # The delist pass compares STORED ids against this set. The
                    # board emitted the fresh churned id, but we deliberately
                    # keep the existing row, so register that existing id as
                    # live too or it is falsely delisted later in this run.
                    raw_ids_by_company[name].add(existing_id)
                    stored_id = existing_id
                    is_new = False
            # A listing that ships its body (Greenhouse, Ashby, …) can state
            # its closing date only in prose; read it before either branch
            # below stores job["deadline"].
            if not job.get("deadline") and job.get("description"):
                job["deadline"] = stated_deadline(job["description"])[0]
            if is_new:
                new_jobs.append({
                    **job,
                    "company": name,
                    "category": category,
                    **({"_wd": wd_cfg} if wd_cfg else {}),
                })
                if not args.dry_run:
                    db.mark_seen(
                        job["id"],
                        company=name,
                        title=job.get("title", ""),
                        url=job.get("url", ""),
                        category=category,
                        location=job.get("location", ""),
                        posted=job.get("posted", ""),
                        description=job.get("description", ""),
                        deadline=job.get("deadline", ""),
                    )
            elif not args.dry_run:
                # Already-stored role still on the board: refresh last_seen so the
                # Sources/Stats silent-zero detection (company_recent_volume,
                # which keys off last_seen) can tell a still-posting firm from a
                # dead board. Without this, last_seen froze at first_seen because
                # mark_seen only ran for brand-new rows.
                db.touch_seen(stored_id)
                # Forward-fill / upgrade: ATSes that ship the description in the
                # listing payload (Greenhouse content=true, Ashby, Kernel,
                # wp_job) heal rows stored before that capability existed —
                # including rows frozen on a short JS-shell-title STUB (a plain
                # NULL-only fill left those stuck forever). Never shrinks a real
                # (>=800 char) description.
                if job.get("description"):
                    db.upgrade_description_if_better(stored_id, job["description"])
                # A board can state a closing date after the role was first
                # stored, or move it. Rows already in the database would
                # otherwise keep the NULL deadline they were created with.
                if job.get("deadline"):
                    db.set_deadline(stored_id, job["deadline"])
        if not args.dry_run:
            db.end_batch()

    if id_collisions:
        lines = "\n".join(f"  {n}: {i} board {u} stored {su}"
                          for n, i, u, su in id_collisions)
        print(f"scan: {len(id_collisions)} cross-tenant id collisions "
              f"(roles not stored):\n{lines}", flush=True)
        if not args.dry_run:
            notify.send_alert(
                "job-scan: cross-tenant job id collision",
                f"{len(id_collisions)} role(s) carry an id already stored for "
                f"another tenant, so they were not stored:\n\n{lines}\n\n"
                "Fix: give the colliding target an id scope (talnet_id_scope "
                "or icims.id_scope in targets.json). Scoping a target that "
                "already has stored rows re-keys all of them, so migrate its "
                "stored ids in the same change or its status, stars and "
                "applications detach.",
            )

    # Inline description enrichment. Many ATSes (Oracle HCM, most Workday
    # tenants) don't include the description in the listing payload; the web
    # app's job-detail view, the YoE-wall detector, and the tagger all need it.
    # Bounded by len(new_jobs). With the relaxed "store the rest" filter this can
    # be larger than before, but it's still best-effort and never breaks the scan.
    _enrich_new_jobs(new_jobs, db, dry_run=args.dry_run)

    # Tagging pass — runs on EVERY stored role (title+company+location only,
    # cheap). Populates the function/seniority/type/location facets the web UI
    # filters on. Independent of relevance scoring below.
    if not args.no_tag:
        tag_jobs(new_jobs)
        # The circuit breaker (jobfeed/tag.py) detects a dead `claude` CLI — expired
        # OAuth is the recurring case — but until now only logged it, so every
        # auth decay silently stored a night's roles with blank tags. Email
        # immediately: the rows self-repair via the nightly re-tag hook once
        # the CLI is re-authed, but re-authing needs a human.
        if tag.LAST_RUN_HEALTH.get("api_transport_down") and not args.dry_run:
            if tag.LAST_RUN_HEALTH.get("api_fallback"):
                used = "the Anthropic Messages API"
            elif (tag.LAST_RUN_HEALTH.get("cli_path")
                  and not tag.LAST_RUN_HEALTH.get("cli_down")):
                used = "the claude CLI"
            else:
                used = "no working fallback"
            notify.send_alert(
                "job-scan: primary tag API failed",
                f"The configured TAG_PROVIDER API transport failed during "
                f"tagging. This run fell back to {used}; inspect tag_debug.log "
                f"and tag_runs.jsonl before the next scan. "
                f"{tag.LAST_RUN_HEALTH.get('jobs_tagged', 0)}/"
                f"{len(new_jobs)} new roles were tagged.",
            )
        elif tag.LAST_RUN_HEALTH.get("cli_down") and not args.dry_run:
            notify.send_alert(
                "job-scan: tagger CLI down (auth expired?)",
                f"jobfeed/tag.py circuit breaker tripped: the claude CLI returned "
                f"fully blank batches"
                + (" and the ANTHROPIC_TAG_API_KEY fallback also failed"
                   if tag.LAST_RUN_HEALTH.get("api_fallback") else "")
                + f". {len(new_jobs)} new roles were stored "
                f"with blank tags (invisible to area filters until re-tagged).\n\n"
                f"Fix: ssh m1, run `claude` and /login to re-auth, then\n"
                f"  .venv/bin/python -m jobfeed.backfill_tags --workers 4\n"
                f"or wait for the nightly re-tag hook to drain the backlog.",
            )
        elif tag.LAST_RUN_HEALTH.get("api_fallback") and not args.dry_run:
            notify.send_alert(
                "job-scan: tagger fell back to paid API (OAuth dead)",
                "The claude CLI on the M1 stopped answering (expired OAuth?) "
                "mid-run; tagging continued on the direct-API fallback "
                "(ANTHROPIC_TAG_API_KEY, billed to the Console account), so "
                "tags are intact. Re-auth the CLI when convenient: ssh m1, "
                "run `claude`, /login. Until then every scan bills the API.",
            )
        if not args.dry_run:
            provenance = tag.tag_provenance()
            with db.transaction():
                for job in new_jobs:
                    # Description-derived facets retain NULL-vs-empty semantics.
                    db.set_tags(
                        job["id"],
                        area=job.get("area", ""),
                        desk=job.get("desk", ""),
                        seniority=job.get("seniority", ""),
                        job_type=job.get("job_type", "job"),
                        loc_city=job.get("loc_city", ""),
                        loc_country=job.get("loc_country", ""),
                        loc_region=job.get("loc_region", ""),
                        work_mode=job.get("work_mode", ""),
                        lang_req=job.get("lang_req"),
                        education=job.get("education"),
                        start_date=job.get("start_date"),
                        min_yoe=job.get("min_yoe"),
                        **provenance,
                    )

    # Years-of-experience walls — deterministic regex on the description. The
    # LLM min_yoe (set above) WINS when the tagger saw a description; this regex
    # is the FALLBACK for rows tagged without one (job.get('min_yoe') is None) —
    # a junior-titled role with "minimum 5 years" buried in the body is really
    # senior. Runs on EVERY stored role, but only overwrites when the LLM had no
    # description to read (so we never clobber the smarter LLM value with the
    # coarser regex). Runs even under --no-tag (min_yoe never set by the LLM).
    if not args.dry_run:
        with db.transaction():
            for job in new_jobs:
                if job.get("min_yoe") is not None:
                    continue  # LLM already set it from a description — don't clobber
                _, years = has_experience_wall(job.get("description", ""))
                db.set_yoe(job["id"], years)

    if not args.dry_run:
        db.log_run(
            new_jobs=len(new_jobs),
            firms_checked=checked,
            errors=len(errors),
            duration_s=time.monotonic() - scan_t0,
        )

    # Refresh source-health state from this run (feeds the /sources "stalled"
    # indicator). Only on a real, non-empty scan — a crashed/empty run must not
    # rewrite baselines (mirrors selfcheck's guard). Skipped for --verify (that
    # path returns earlier) and dry-runs.
    degraded_now: set = set()
    if not args.dry_run and checked > 0:
        degraded_now = _write_health_state(
            raw_counts, {name for name, _ in errors},
            {name: len(ids) for name, ids in raw_ids_by_company.items()},
            {name: len(ids) for name, ids in filtered_ids_by_company.items()},
            targets=targets,
        ) or set()

    # Delisting: a stored role missing from a company's fresh board is
    # presumably taken down. Only evaluated for companies that scraped
    # cleanly this run (raw_ids_by_company excludes errored/skipped
    # companies), so a flaky source can't look like a mass delisting.
    # Degraded companies (count collapsed vs baseline — the silent-empty
    # failure mode, e.g. selector rot returning [] with no error) are also
    # excluded: their "missing" rows are far more likely a broken scraper
    # than a mass takedown, and the 'other' purge below is irreversible.
    # 'other'-tagged noise is purged outright; real categories keep the row
    # with delisted_at set so the web app can badge it.
    if not args.dry_run and checked > 0:
        # Clean-zero backstop (belt to the baseline suspenders in
        # _write_health_state): NEVER delist a company that scraped cleanly but
        # returned 0 roles. find_delistable would report every stored id as
        # missing, mark_delisted stamps them all, and purge_delisted_other
        # hard-deletes its 'other' rows a few days later — catastrophic for a
        # small board whose selector quietly rotted. A board that legitimately
        # emptied loses nothing: its rows simply aren't delisted this run, and a
        # genuinely removed source is swept by purge_orphaned_companies instead.
        # (_write_health_state already flags most of these via the baseline; this
        # also covers firms with no baseline entry yet.)
        zero_now = {name for name, n in raw_counts.items() if n == 0}
        skip_delist = degraded_now | zero_now
        delistable_ids = {name: ids - noise_ids_by_company.get(name, set())
                          for name, ids in raw_ids_by_company.items()
                          if name not in skip_delist}
        if skip_delist:
            _report_skipped_delists(skip_delist, zero_now, raw_counts)
        to_delist = db.find_delistable(delistable_ids)
        db.mark_delisted(to_delist)
        purged = db.purge_delisted_other()
        if to_delist or purged:
            print(f"delisted {len(to_delist)} roles no longer on their board "
                  f"({purged} 'other'-tagged purged)", flush=True)

        # A source can also be removed from targets.json entirely (e.g. Booz
        # Allen — wired then dropped same day). Those companies never get
        # scraped again, so find_delistable above never sees them; sweep them
        # out here by name instead. Every configured name counts, not just
        # runnable ones — an unverified/manual/unknown source is still
        # "current", just not actively scraped.
        #
        # 2026-09-02: this used to pass {t["name"] for t in targets}, which is
        # the list AFTER --company filtering. `jobfeed/main.py --company "BIS"` therefore
        # declared all 410 other configured sources orphaned and hard-deleted
        # 51,810 rows in one run. Two guards now, because one was not enough:
        # a partial run never purges at all, and the set is the pre-filter one.
        # The row guards inside purge_orphaned_companies held — everything with
        # a status or a favourite survived — but scraped history has no such
        # protection and only existed again because of that night's backup.
        if args.company:
            print("skipping orphan purge: partial run (--company) must not make "
                  "global delete decisions", flush=True)
        else:
            orphaned = db.purge_orphaned_companies(all_target_names)
            if orphaned:
                print(f"purged {orphaned} roles from sources no longer in "
                      f"targets.json", flush=True)

        # Age floor. The delist pass above only sees companies that scraped
        # cleanly, and degraded sources are excluded from it — both correct,
        # but together they mean a source whose board legitimately empties
        # keeps its last roles live forever. It returns 0, trips the clean-zero
        # backstop, is marked degraded, and is never delistable again. On
        # 2026-09-02 that left Muzinich's `Trader` and `High Yield Credit
        # Analyst` showing as live seven weeks after that board went to zero.
        #
        # 21 days is far beyond any normal cadence (the scan is nightly), so
        # this only fires when nobody has confirmed a role for three weeks.
        # Reversible: touch_seen() re-lists a role the moment it reappears, and
        # the rows are flagged delisted_by_age so purge_delisted_other will not
        # hard-delete them — a long outage must not turn "we stopped looking"
        # into "delete it".
        # Only sources that produced nothing this run. See the docstring:
        # unrestricted, this caught 886 rows including 295 J.P. Morgan ones
        # that may well still be live.
        produced = {name for name, ids in raw_ids_by_company.items() if ids}
        aged = db.find_age_delistable(AGE_DELIST_DAYS, exclude_companies=produced)
        if aged:
            n = db.mark_delisted_by_age(aged)
            print(f"delisted {n} roles unseen for {AGE_DELIST_DAYS}+ days "
                  f"(age floor; not eligible for the 'other' purge)", flush=True)

    # The web app is the only surface — store broad, filter/apply on the site.
    print(f"scan: {len(new_jobs)} new roles stored, {checked} firms checked, "
          f"{len(errors)} errors", flush=True)

    if _LIGHT_BATCH_TIMED_OUT:
        # See _LIGHT_BATCH_TIMED_OUT: a wedged scraper thread would block the
        # interpreter's exit-time thread join forever. Every DB write above is
        # already committed per-call, so hard-exiting loses nothing. Exit 3 (not
        # 0): the results are PARTIAL (some light-batch companies never
        # finished), so ops/deliver.sh must skip the downstream enrichment/tagging
        # steps that would otherwise run on incomplete data — exit 0 read as a
        # clean scan and defeated that guard.
        print("scan: light batch timed out — hard-exiting (code 3) past the "
              "wedged scraper thread", flush=True)
        os._exit(3)


if __name__ == "__main__":
    main()
