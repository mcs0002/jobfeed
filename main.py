#!/usr/bin/env python3
"""
Job scraper — checks career pages of financial firms for graduate/junior roles.
Stores + tags new roles in jobs.db for the web app (the only surface).

Usage:
  python3 main.py              # new jobs only (default)
  python3 main.py --all        # all current matching jobs (ignore DB state)
  python3 main.py --dry-run    # run but don't update DB
  python3 main.py --verify     # test all slugs and report which ones work
"""
import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGETS_FILE = os.path.join(ROOT, "targets.json")


def _load_dotenv() -> None:
    """Minimal .env loader — populate the environment from .env so downstream
    code sees the keys (e.g. WEB_PASSWORD, JOBS_DB) without python-dotenv."""
    env_path = Path(ROOT) / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()
DB_FILE = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))

sys.path.insert(0, ROOT)

from db import JobDB
from scrapers.enrich import detail_enricher, enrich_one
# Inline-lane enrichers (routed explicitly in _enrich_new_jobs; the rest are
# reached via the DETAIL_ENRICHERS registry).
from scrapers.enrich import (
    balyasny_enrich, csod_enrich, goldman_enrich, oracle_enrich,
    talentbrew_enrich, workable_enrich,
)
from filter import has_experience_wall, is_relevant
import notify
import tag
from tag import tag_jobs
from scrapers import HANDLERS


def load_targets():
    with open(TARGETS_FILE) as f:
        return json.load(f)


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


# Heavy boards run one-process-per-company via heavy_scrape.py so a hung
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
                [sys.executable, os.path.join(ROOT, "heavy_scrape.py"),
                 "--company", name],
                capture_output=True, text=True, timeout=HEAVY_TIMEOUT,
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


def verify_mode(targets, workers=6):
    """Test every configured target and report what works."""
    print("=== SLUG VERIFICATION RUN ===\n")
    results = {"ok": [], "fail": [], "skip": []}
    configured = [company for company in targets if company.get("ats", "unknown") not in ("unknown", "manual")]
    results["skip"].extend(
        company["name"] for company in targets
        if company.get("ats", "unknown") in ("unknown", "manual")
    )

    light, heavy = _split_heavy(configured)
    scraped = scrape_targets(light, workers=workers) + scrape_heavy_targets(heavy)
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
        backfill_descriptions.py mutates it on the module."""
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

    # Route each job to its first matching lane. Order matters in two places:
    # Workday routes by config (its public page is a JS shell), and TalentBrew
    # routes by id PREFIX before any URL matcher — its /job/<loc>/<slug>/<id>
    # URLs collide with the broad SuccessFactors matcher, which would return ""
    # and leave every TalentBrew firm un-enriched. Everything else is a JS
    # shell/SPA with a first-party detail API; the plain-HTML scrape is the
    # fallback lane.
    lanes: dict[str, list[dict]] = {
        k: [] for k in ("workday", "talentbrew", "oracle", "workable",
                        "goldman", "csod", "balyasny", "detail", "http")}
    for j in targets:
        url = j.get("url", "")
        if j.get("_wd"):
            lane = "workday"
        elif talentbrew_enrich.is_talentbrew(j.get("id", "")):
            lane = "talentbrew"
        elif oracle_enrich.is_oracle(url):
            lane = "oracle"
        elif workable_enrich.is_workable(url):
            lane = "workable"
        elif goldman_enrich.is_goldman(url):
            lane = "goldman"
        elif csod_enrich.is_csod(url):
            lane = "csod"
        elif balyasny_enrich.is_balyasny(url):
            lane = "balyasny"
        elif detail_enricher(url):
            lane = "detail"
        else:
            lane = "http"
        lanes[lane].append(j)

    _pool(lanes["http"],
          lambda j, s: enrich_one(j["url"], s, timeout=_enrich_timeout(j)))
    _pool(lanes["oracle"],
          lambda j, s: oracle_enrich.description(j["url"], s, timeout=ENRICH_TIMEOUT_SECONDS))
    _pool(lanes["workable"],
          lambda j, s: workable_enrich.description(j["url"], s, timeout=ENRICH_TIMEOUT_SECONDS))
    _pool(lanes["goldman"],
          lambda j, s: goldman_enrich.description(j["url"], s, timeout=ENRICH_TIMEOUT_SECONDS))
    _pool(lanes["detail"],
          lambda j, s: detail_enricher(j["url"])(j["url"], s, timeout=ENRICH_TIMEOUT_SECONDS))
    _pool(lanes["talentbrew"],
          lambda j, s: talentbrew_enrich.description(j["url"], s, timeout=SLOW_ENRICH_TIMEOUT_SECONDS))

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
                     j["_wd"].get("applied_facets")))


# Source-health thresholds — kept identical to selfcheck.py so the scan-written
# state and the weekly selfcheck agree on what "broken" means.
HEALTH_MIN_BASELINE = 5      # must have hit this once before a collapse counts
HEALTH_COLLAPSE_RATIO = 0.2  # at/below this fraction of baseline = degraded


def _write_health_state(raw_counts: dict[str, int], error_names: set[str]) -> None:
    """Refresh verify_state.json from this scan's results so the /sources
    'stalled' indicator reflects the latest run, not just the weekly selfcheck.

    Same schema + logic as selfcheck.py: ``failing`` = the scraper errored,
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
    baseline = dict(prev.get("baseline", {}))
    # Compute degraded BEFORE raising the baseline, so today's low count can't
    # lift its own floor.
    degraded = {
        name for name, n in raw_counts.items()
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
        name for name, n in raw_counts.items()
        if n == 0 and baseline.get(name, 0) > 0
    }
    for name, n in raw_counts.items():
        if n > baseline.get(name, 0):
            baseline[name] = n
    try:
        # Preserve keys owned by other writers (selfcheck's selfcheck_*
        # transition snapshot) and write atomically — a torn read parses as
        # {} and silently resets every rolling baseline.
        prev.update({
            "failing": sorted(error_names),
            "degraded": sorted(degraded),
            "baseline": dict(sorted(baseline.items())),
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
    parser.add_argument("--no-tag", action="store_true", help="Skip the Haiku tagging pass (tag.py)")
    args = parser.parse_args()
    scan_t0 = time.monotonic()

    targets = load_targets()

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

    runnable = []
    for company in targets:
        ats = company.get("ats", "unknown")
        name = company["name"]

        if ats in ("unknown", "manual"):
            # unknown = research candidate not yet wired; manual = wanted firm
            # that can't be scraped reliably (see manual_check.py). Both skipped.
            skipped.append(name)
            continue

        if not args.include_unverified and not company.get("verified"):
            skipped.append(name)
            continue
        runnable.append(company)

    light, heavy = _split_heavy(runnable)
    scraped = (scrape_targets(light, workers=max(1, args.workers))
               + scrape_heavy_targets(heavy))
    for company, jobs, err in scraped:
        name = company["name"]
        category = company.get("category", "Other")

        if err:
            errors.append((name, err))
            continue

        checked += 1
        raw_counts[name] = len(jobs)
        raw_ids_by_company[name] = {job["id"] for job in jobs}

        # Workday descriptions can't be enriched from the public URL (JS shell);
        # they need the tenant/board config + a primed session. Carry the config
        # on each new job so _enrich_new_jobs can route it to WorkdayEnricher.
        wd_cfg = company.get("workday") if company.get("ats") == "workday" else None

        # Per-source noise terms (e.g. a firm whose bulk the global lists don't
        # know). Kept on the target so one global list isn't a single point of
        # failure when we point the heavy executor at a new noisy board.
        extra_drops = set(company.get("noise_terms", [])) or None

        for job in jobs:
            if not is_relevant(job, category=category, extra_drops=extra_drops):
                continue
            is_new = args.all or not db.seen(job["id"])
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
                existing_id = db.find_active_by_url(job.get("url", ""), job["id"])
                if existing_id is not None:
                    db.touch_seen(existing_id)
                    if job.get("description"):
                        db.upgrade_description_if_better(existing_id, job["description"])
                    is_new = False
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
                    )
            elif not args.dry_run:
                # Already-stored role still on the board: refresh last_seen so the
                # Sources/Stats silent-zero detection (company_recent_volume,
                # which keys off last_seen) can tell a still-posting firm from a
                # dead board. Without this, last_seen froze at first_seen because
                # mark_seen only ran for brand-new rows.
                db.touch_seen(job["id"])
                # Forward-fill / upgrade: ATSes that ship the description in the
                # listing payload (Greenhouse content=true, Ashby, Kernel,
                # wp_job) heal rows stored before that capability existed —
                # including rows frozen on a short JS-shell-title STUB (a plain
                # NULL-only fill left those stuck forever). Never shrinks a real
                # (>=800 char) description.
                if job.get("description"):
                    db.upgrade_description_if_better(job["id"], job["description"])

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
        # The circuit breaker (tag.py) detects a dead `claude` CLI — expired
        # OAuth is the recurring case — but until now only logged it, so every
        # auth decay silently stored a night's roles with blank tags. Email
        # immediately: the rows self-repair via the nightly re-tag hook once
        # the CLI is re-authed, but re-authing needs a human.
        if tag.LAST_RUN_HEALTH.get("cli_down") and not args.dry_run:
            notify.send_alert(
                "job-scan: tagger CLI down (auth expired?)",
                f"tag.py circuit breaker tripped: the claude CLI returned "
                f"fully blank batches"
                + (" and the ANTHROPIC_TAG_API_KEY fallback also failed"
                   if tag.LAST_RUN_HEALTH.get("api_fallback") else "")
                + f". {len(new_jobs)} new roles were stored "
                f"with blank tags (invisible to area filters until re-tagged).\n\n"
                f"Fix: ssh m1, run `claude` and /login to re-auth, then\n"
                f"  .venv/bin/python backfill_tags.py --workers 4\n"
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
            for job in new_jobs:
                # The description-derived facets (lang_req/education/start_date)
                # and the LLM min_yoe are only meaningful when the tagger saw a
                # description. On a blanked (CLI-failure) or no-description tag
                # tag.py leaves them as None — passed straight through so
                # set_tags writes SQL NULL ('tagged pre-description'), which the
                # nightly re-tag hook re-visits once a description lands. min_yoe
                # None here means "leave the regex value below to stand".
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
                )

    # Years-of-experience walls — deterministic regex on the description. The
    # LLM min_yoe (set above) WINS when the tagger saw a description; this regex
    # is the FALLBACK for rows tagged without one (job.get('min_yoe') is None) —
    # a junior-titled role with "minimum 5 years" buried in the body is really
    # senior. Runs on EVERY stored role, but only overwrites when the LLM had no
    # description to read (so we never clobber the smarter LLM value with the
    # coarser regex). Runs even under --no-tag (min_yoe never set by the LLM).
    if not args.dry_run:
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
            raw_counts, {name for name, _ in errors}) or set()

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
        delistable_ids = {name: ids for name, ids in raw_ids_by_company.items()
                          if name not in skip_delist}
        if skip_delist:
            print(f"delist: skipping {len(skip_delist)} degraded/zero source(s): "
                  f"{', '.join(sorted(skip_delist))}", flush=True)
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
        orphaned = db.purge_orphaned_companies({t["name"] for t in targets})
        if orphaned:
            print(f"purged {orphaned} roles from sources no longer in "
                  f"targets.json", flush=True)

    # The web app is the only surface — store broad, filter/apply on the site.
    print(f"scan: {len(new_jobs)} new roles stored, {checked} firms checked, "
          f"{len(errors)} errors", flush=True)

    if _LIGHT_BATCH_TIMED_OUT:
        # See _LIGHT_BATCH_TIMED_OUT: a wedged scraper thread would block the
        # interpreter's exit-time thread join forever. Every DB write above is
        # already committed per-call, so hard-exiting loses nothing. Exit 3 (not
        # 0): the results are PARTIAL (some light-batch companies never
        # finished), so deliver.sh must skip the downstream enrichment/tagging
        # steps that would otherwise run on incomplete data — exit 0 read as a
        # clean scan and defeated that guard.
        print("scan: light batch timed out — hard-exiting (code 3) past the "
              "wedged scraper thread", flush=True)
        os._exit(3)


if __name__ == "__main__":
    main()
