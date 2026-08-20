#!/usr/bin/env python3
"""
Weekly source health check. Runs --verify and reports any source that worked
before but is now broken. Pure Python, no model/LLM involved, so it costs
nothing to run idle. State is tracked in verify_state.json.

Two failure classes are detected:

1. HARD FAIL — the scraper raised (bad slug, 4xx/5xx, parse error). verify_mode
   prints `FAIL <name> [...]`. Flagged the first run it appears.

2. SILENT EMPTY — the scraper succeeds but returns 0 jobs (or collapses far
   below its historical high). verify_mode prints `OK <name> [...] — N jobs
   found`; a source that used to return many and now returns 0 is broken in
   practice but never raised. We keep a rolling per-source baseline (max jobs
   ever seen) and alert when a source with a real baseline drops to ~nothing.
   A source that is *always* small/zero (genuinely few public roles) never had
   a baseline, so it never alerts — only regressions do.

3. NEVER PRODUCED — a verified source that has returned 0 with no baseline for
   NEVER_PRODUCED_STREAK consecutive checks. This is the blind spot class 2
   leaves open: a source misconfigured from day one (dead ATS slug, JS-shell
   tenant) never builds a baseline, so the collapse check never fires and it
   rots silently marked verified:true (how E.ON hid as a dead SmartRecruiters
   slug). The streak requirement lets a freshly added source stay quiet until
   it has had a real chance to produce.

Three DB-side quality monitors piggyback on the same run (all transition-
alerted): ``description_health`` (stub/null description rates per ATS),
``tag_health`` (companies whose tagged rows are >=80% area='other' — invisible
in the UI, the PIC blind spot), and ``link_health`` (sampled apply URLs that
404 across consecutive runs — the stale-scrape blind spot no count-based check
can see).

Alerts fire only on the TRANSITION into a broken/degraded state, so a source
stays quiet until it recovers and breaks again. Repair is on-demand: when a
source is flagged, ask Claude to fix it.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PY = str(ROOT / ".venv" / "bin" / "python")
STATE = ROOT / "verify_state.json"

# A source must have returned at least this many jobs at some point before a
# later collapse counts as a regression (keeps genuinely-tiny boutique boards
# from ever alerting).
MIN_BASELINE = 5
# Below this fraction of the rolling baseline counts as a collapse (not only a
# hard zero — e.g. ING going 793 -> 2 should trip even though it isn't 0).
COLLAPSE_RATIO = 0.2
# NEVER-PRODUCED guard: the collapse check only fires for sources that once had
# a real baseline, so a source wired wrong from day one (dead ATS slug, JS-shell
# tenant) sits at 0 forever and never alerts — exactly how E.ON hid as a dead
# SmartRecruiters slug. We flag a verified source that returns 0 with NO baseline
# across this many consecutive checks; the streak requirement keeps a freshly
# added source (still 0 until its first real scan) from alerting prematurely.
NEVER_PRODUCED_STREAK = 2

OK_RE = re.compile(r"^\s*OK\s+(.+?)\s+\[[^\]]+\]\s+—\s+(\d+)\s+jobs found", re.M)
FAIL_RE = re.compile(r"^\s*FAIL\s+(.+?)\s+\[", re.M)

# BUG 4: Scan-freshness threshold. If MAX(last_seen) in jobs.db is older than
# this many days, the nightly deliver.sh scan has been silently dying and nothing
# in the source-count checks could see it (selfcheck scrapes independently and
# finds all sources "OK"). 2 days gives one full missed day before alerting.
FRESHNESS_MAX_DAYS = 2

DB_FILE = os.environ.get("JOBS_DB", str(ROOT / "jobs.db"))


def _check_scan_freshness(db_path: str = DB_FILE) -> str | None:
    """Return a warning string if the DB looks stale, None if fresh or absent.

    Queries MAX(last_seen) from jobs.db (ISO UTC string, same format as
    datetime.now(timezone.utc).isoformat()). If it's older than FRESHNESS_MAX_DAYS
    days, the nightly scan has been failing silently. Returns None (skip, don't
    crash) if the DB file is missing — a fresh checkout has no DB yet.
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone

    if not Path(db_path).exists():
        print(f"NOTE: selfcheck: {db_path} not found — skipping freshness check "
              f"(fresh checkout?)", file=sys.stderr)
        return None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT MAX(last_seen) FROM seen_jobs").fetchone()
        conn.close()
        max_last_seen = row[0] if row else None
        if max_last_seen is None:
            # Empty DB — freshness check not meaningful.
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(days=FRESHNESS_MAX_DAYS)
        # last_seen is stored as an ISO string; fromisoformat handles the UTC
        # offset suffix (+00:00) that isoformat() produces.
        last_seen_dt = datetime.fromisoformat(max_last_seen)
        if last_seen_dt < cutoff:
            age_days = (datetime.now(timezone.utc) - last_seen_dt).days
            return (f"STALE SCAN: MAX(last_seen) in jobs.db is {age_days} day(s) old "
                    f"(last: {max_last_seen[:19]}). "
                    f"The nightly deliver.sh scan may have been silently failing.")
    except Exception as exc:
        print(f"WARN: selfcheck: freshness check failed: {exc!r}", file=sys.stderr)
    return None


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def _alert_verify_failure(subject: str, body: str) -> None:
    """Send an alert email for a verify-run failure, gated on SELFCHECK_EMAIL.

    Never raises — a notify failure must not change selfcheck's exit code (BUG 3).
    """
    if not os.environ.get("SELFCHECK_EMAIL"):
        return
    try:
        import notify
        notify.send_alert(subject, body)
    except Exception as e:
        print(f"WARN: selfcheck: notify failed: {e!r}", file=sys.stderr)


def main() -> int:
    # BUG 2: selfcheck previously called subprocess.run with no timeout, so a
    # wedged verify run would hang selfcheck indefinitely and block launchd from
    # ever scheduling the next weekly run. 3 h is generous (the verify run
    # normally finishes in minutes) and matches the spirit of HEAVY_TIMEOUT.
    VERIFY_TIMEOUT = 3 * 3600
    try:
        proc = subprocess.run(
            [PY, str(ROOT / "main.py"), "--verify", "--verified-only"],
            capture_output=True, text=True, cwd=str(ROOT),
            timeout=VERIFY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        msg = (f"ERROR: verify run timed out after {VERIFY_TIMEOUT}s — "
               f"not rewriting {STATE.name}")
        print(msg, file=sys.stderr)
        # BUG 3: alert on timeout just like a crash (BUG 3 fix applied here too).
        _alert_verify_failure(
            "selfcheck: verify run FAILED",
            f"selfcheck: verify run timed out after {VERIFY_TIMEOUT}s. "
            f"State file not updated.",
        )
        return 2

    out = proc.stdout + proc.stderr

    # Guard against a crashed/empty verify run: an empty result set is
    # meaningless and would falsely report every previously-broken source as
    # recovered while overwriting the baseline. Bail without touching state.
    if proc.returncode != 0 or not out.strip():
        msg = (f"ERROR: verify run failed (exit {proc.returncode}, "
               f"{len(out.strip())} chars output) — not rewriting {STATE.name}")
        print(msg, file=sys.stderr)
        # BUG 3: the bail path previously never called notify even with
        # SELFCHECK_EMAIL=1, so a crashed verify run produced zero signal on an
        # unattended M1. Alert now — wrapped so notify failures can't affect exit.
        _alert_verify_failure(
            "selfcheck: verify run FAILED",
            f"selfcheck: verify run failed (exit {proc.returncode}, "
            f"{len(out.strip())} chars output). State file not updated.",
        )
        return 2

    failing = set(FAIL_RE.findall(out))
    counts = {name: int(n) for name, n in OK_RE.findall(out)}

    state = _load_state()
    # Diff against selfcheck's OWN last-run snapshot, not the shared
    # failing/degraded keys — the nightly scan rewrites those on every run
    # (main._write_health_state), so by the weekly selfcheck any break was
    # already recorded there and newly_broken was always empty: the alert
    # path (exit 1 + optional email) could never fire. First run after this
    # change falls back to the shared keys to avoid a spurious alert flood.
    prev_failing = set(state.get("selfcheck_failing", state.get("failing", [])))
    baseline = dict(state.get("baseline", {}))
    prev_degraded = set(
        state.get("selfcheck_degraded", state.get("degraded", [])))
    prev_never = set(state.get("selfcheck_never_produced", []))
    zero_streaks = dict(state.get("zero_streaks", {}))

    # SILENT EMPTY / COLLAPSE: a source that succeeded but cratered vs its
    # rolling high. Compute BEFORE updating the baseline so today's low count
    # can't raise its own floor.
    degraded = set()
    for name, n in counts.items():
        base = baseline.get(name, 0)
        if base >= MIN_BASELINE and n <= max(0, int(base * COLLAPSE_RATIO)):
            degraded.add(name)

    # NEVER PRODUCED: a source that ran cleanly (in counts, not failing) yet
    # returned 0 and has never once had a positive baseline. Track a streak so a
    # brand-new source doesn't alert on its first empty check. Reset the moment
    # it produces anything or has ever had a baseline.
    for name, n in counts.items():
        if n == 0 and baseline.get(name, 0) == 0:
            zero_streaks[name] = zero_streaks.get(name, 0) + 1
        else:
            zero_streaks.pop(name, None)
    never_produced = {n for n, s in zero_streaks.items()
                      if s >= NEVER_PRODUCED_STREAK} - failing

    # Update rolling baseline (max jobs ever seen) from this run's OK counts.
    for name, n in counts.items():
        if n > baseline.get(name, 0):
            baseline[name] = n

    newly_broken = sorted(failing - prev_failing)
    newly_degraded = sorted(degraded - prev_degraded - failing)
    newly_never = sorted(never_produced - prev_never - failing)
    recovered = sorted((prev_failing - failing) | (prev_degraded - degraded)
                       | (prev_never - never_produced))

    # Description-quality: separate from scrape counts. A source can scrape fine
    # (non-zero, healthy count) yet store only stub descriptions — as TAL.net did
    # — which silently mis-tags every row. description_health reads the live DB;
    # we alert on the TRANSITION into stub-degraded, like the count checks above.
    # Guarded: a DB/import hiccup here must not break the scrape health run.
    prev_stub = set(state.get("stub_degraded", []))
    stub_degraded = set()
    try:
        import description_health
        stub_degraded = {h["ats"] for h in description_health.degraded_sources()}
    except Exception as exc:  # pragma: no cover - best-effort monitor
        print(f"WARN: selfcheck: description health check failed: {exc!r}",
              file=sys.stderr)
    newly_stub = sorted(stub_degraded - prev_stub)

    # Tag-quality: a firm whose live rows are all tagged area='other' is
    # invisible in the UI (Browse hides Other; the Sources deeplink needs one
    # non-other job) — the PIC blind spot. Transition-alert like the above.
    prev_other = set(state.get("other_heavy", []))
    other_heavy = {}
    try:
        import tag_health
        other_heavy = {h["company"]: h["other_pct"]
                       for h in tag_health.other_heavy_sources()}
    except Exception as exc:  # pragma: no cover - best-effort monitor
        print(f"WARN: selfcheck: tag health check failed: {exc!r}",
              file=sys.stderr)
    newly_other = sorted(set(other_heavy) - prev_other)

    # Link-quality: a stale scrape keeps rows live whose apply URLs 404 —
    # counts and descriptions both look healthy, so nothing above sees it.
    # link_health samples random live URLs per company and only reports after
    # DEAD_STREAK consecutive dead runs (streak state in link_state.json).
    prev_dead_links = set(state.get("link_dead", []))
    link_dead = set()
    try:
        import link_health
        link_dead = set(link_health.dead_sources())
    except Exception as exc:  # pragma: no cover - best-effort monitor
        print(f"WARN: selfcheck: link health check failed: {exc!r}",
              file=sys.stderr)
    newly_dead_links = sorted(link_dead - prev_dead_links)

    # Preserve keys owned by other writers; write atomically (temp + replace)
    # so a reader (web app, nightly scan) can't see a torn file — a torn read
    # parses as {} and silently resets every rolling baseline.
    state.update({
        "failing": sorted(failing),
        "degraded": sorted(degraded),
        "baseline": dict(sorted(baseline.items())),
        "selfcheck_failing": sorted(failing),
        "selfcheck_degraded": sorted(degraded),
        "selfcheck_never_produced": sorted(never_produced),
        "zero_streaks": dict(sorted(zero_streaks.items())),
        "stub_degraded": sorted(stub_degraded),
        "other_heavy": sorted(other_heavy),
        "link_dead": sorted(link_dead),
    })
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE)

    # BUG 4: Scan-freshness check. If deliver.sh has been silently dying every
    # night, selfcheck's independent verify run still reports all sources "OK" —
    # the only evidence is a stale MAX(last_seen) in jobs.db. Check it now.
    # Pass DB_FILE explicitly so tests can patch selfcheck.DB_FILE cleanly.
    freshness_warn = _check_scan_freshness(DB_FILE)
    if freshness_warn:
        print(f"WARN: selfcheck: {freshness_warn}", file=sys.stderr)

    alerts = ([(n, "error") for n in newly_broken]
              + [(n, f"empty/collapsed (was ~{baseline.get(n, 0)})")
                 for n in newly_degraded]
              + [(n, "verified but never produced a row (dead since wiring?)")
                 for n in newly_never]
              + [(f"ats:{a}", "descriptions are stubs (tagger runs blind)")
                 for a in newly_stub]
              + [(c, f"{other_heavy[c]}% of tagged rows are area=other "
                     "(invisible in UI — tagger blind spot?)")
                 for c in newly_other]
              + [(c, "sampled apply links 404 across consecutive checks "
                     "(stale scrape / URL scheme changed?)")
                 for c in newly_dead_links])

    if alerts:
        lines = "\n".join(f"  - {n} — {why}" for n, why in alerts)
        total = len(failing) + len(degraded)
        print(
            f"ALERT: {len(alerts)} source(s) newly broken/degraded:\n{lines}\n"
            f"Total currently failing/degraded: {total}",
            file=sys.stderr,
        )
        if recovered:
            print("Recovered since last check: " + ", ".join(recovered),
                  file=sys.stderr)
        # Email alert is OPT-IN (set SELFCHECK_EMAIL=1). Default off: the
        # /sources "stalled" indicator (fed by every scan via verify_state.json)
        # is the primary signal, so the flaky SMTP/keychain push isn't needed.
        # The ALERT line above still lands in healthcheck.log either way. Lazy
        # import + guard so a notify failure can never change the exit code.
        if os.environ.get("SELFCHECK_EMAIL"):
            try:
                import notify
                k = len(alerts)
                subject = f"{k} source{'s' if k != 1 else ''} broke"
                body = (f"{k} source(s) newly broken/degraded on the last verify "
                        f"run:\n\n{lines}\n\nTotal failing/degraded: {total}")
                if recovered:
                    body += "\nRecovered since last check: " + ", ".join(recovered)
                if freshness_warn:
                    body += f"\n\n{freshness_warn}"
                notify.send_alert(subject, body)
            except Exception as e:
                print(f"WARN: selfcheck: notify failed: {e!r}", file=sys.stderr)
    elif freshness_warn:
        # No source alerts, but the DB is stale — send a standalone alert and
        # include it in the exit code (return 1 at minimum per the spec).
        if os.environ.get("SELFCHECK_EMAIL"):
            try:
                import notify
                notify.send_alert("selfcheck: stale scan detected", freshness_warn)
            except Exception as e:
                print(f"WARN: selfcheck: notify failed: {e!r}", file=sys.stderr)

    print(f"verify: {len(failing)} failing | {len(degraded)} degraded | "
          f"{len(never_produced)} never-produced | {len(stub_degraded)} stub-desc | "
          f"{len(other_heavy)} other-heavy | {len(link_dead)} dead-links | "
          f"{len(alerts)} new | {len(recovered)} recovered")
    # Nonzero exit when something newly broke, so a launchd/cron wrapper can
    # detect alerts by exit code alone. A stale DB (freshness_warn set) also
    # returns 1 even when no source transitions fired (BUG 4).
    return 1 if (alerts or freshness_warn) else 0


if __name__ == "__main__":
    sys.exit(main())
