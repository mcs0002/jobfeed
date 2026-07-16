#!/bin/zsh
# Run the daily job scan. Stores + tags + YoE-flags roles for the web app,
# which is the only surface (no Telegram, no relevance scoring).
#
# PRODUCTION deploy script — runs on the M1 via launchd. Keep it robust.
# -e: abort on any unchecked error; -u: error on unset vars;
# -o pipefail: a failure anywhere in a pipeline fails the pipeline.
set -euo pipefail
cd "${0:A:h}"

LOG=deliver.log

# Failure alert (best-effort, never fails the run). Before this existed every
# hard failure below only went to deliver.log — a diverged pull or a wedged
# scan could kill the nightly run for days with nobody noticing until the
# weekly selfcheck. notify.py reads SMTP creds from .env.
alert() {
  .venv/bin/python notify.py "$1" "$2" >>"$LOG" 2>&1 || true
}

# stat portability: production is the M1 (BSD/macOS stat, -f), but a Linux
# cloner has GNU stat (-c) — the BSD flags silently return nothing there,
# breaking log rotation and stale-lock reclaim. Probe once and pick the syntax.
if stat -f%z "$0" >/dev/null 2>&1; then
  stat_size() { stat -f%z "$1" 2>/dev/null || echo 0; }   # BSD
  stat_mtime() { stat -f %m "$1" 2>/dev/null || echo 0; }
else
  stat_size() { stat -c%s "$1" 2>/dev/null || echo 0; }   # GNU
  stat_mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }
fi

# Log rotation: keep deliver.log from growing without bound. If it exceeds ~5MB,
# rotate the current file to deliver.log.1 (overwriting the previous archive) and
# start fresh. Guarded so a missing log on first run is fine.
if [[ -f "$LOG" ]]; then
  size=$(stat_size "$LOG")
  if (( size > 5 * 1024 * 1024 )); then
    mv -f "$LOG" "$LOG.1"
  fi
fi

# Concurrent-run guard: two overlapping runs (slow heavy sweep + manual run,
# or an edited schedule) mean two writers on jobs.db, a git pull yanking code
# mid-scan, interleaved verify_state.json writes, and doubled Haiku quota
# burn. mkdir is atomic and needs no flock(1) (absent on stock macOS). A
# stale lock whose owner PID is dead (crash/reboot) is reclaimed.
LOCK=.deliver.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  owner=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  # kill -0 alone is fooled by PID reuse after a reboot (the recycled PID
  # belongs to some unrelated live process), silently skipping every run
  # thereafter. No legitimate run lasts 12h, so an old lock is stale
  # regardless of what kill -0 says.
  lock_mtime=$(stat_mtime "$LOCK")
  lock_age=$(( $(date +%s) - lock_mtime ))
  if [[ -n "$owner" ]] && kill -0 "$owner" 2>/dev/null && (( lock_age < 12 * 3600 )); then
    echo "[$(date -u +%FT%TZ)] another run is active (pid $owner) — skipping" >>"$LOG"
    exit 0
  fi
  echo "[$(date -u +%FT%TZ)] reclaiming stale lock (pid ${owner:-unknown} dead or lock older than 12h)" >>"$LOG"
  rm -rf "$LOCK"
  mkdir "$LOCK"
fi
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM

# Runs every day (nightly at 04:00) so each run covers exactly one day.
# Pull latest from GitHub so edits pushed from the dev machine (MacBook Air)
# take effect on the next scheduled run. Fail loud if the M1 has diverged.
echo "[$(date -u +%FT%TZ)] git pull" >>"$LOG"
# NB: the `|| { ...; exit 1; }` guard means set -e does not pre-empt our own
# logging — on divergence we still log the reason and exit 1 ourselves.
git pull --ff-only origin main >>"$LOG" 2>&1 || {
  echo "[$(date -u +%FT%TZ)] git pull failed — aborting run" >>"$LOG"
  alert "job-scan: git pull failed" \
    "git pull --ff-only aborted on the M1 — the working tree has diverged or an untracked file collides. Every nightly run is dead until this is resolved. See deliver.log."
  exit 1
}

# Production deps are pinned in requirements.lock (generated from
# requirements.txt via `uv pip compile`). To refresh the M1 environment after a
# dependency bump, run a one-off install from the lock — NOT requirements.txt —
# so the install is reproducible and matches dev exactly:
#     uv pip sync requirements.lock      # or: uv pip install -r requirements.lock
# The scheduled run intentionally does NOT install deps every night (the env is
# stable between bumps); re-sync manually when requirements.lock changes.

# Run the scan. Capture its exit code explicitly (the `|| rc=$?` keeps set -e
# from killing the script before we can log, and preserves the real code). A
# failed scan must NOT proceed to enrichment on possibly-partial data.
rc=0
.venv/bin/python main.py >>"$LOG" 2>&1 || rc=$?
# rc=3 is the wedged-scan hard-exit (main.py os._exit(3)): the light batch timed
# out mid-run, so results are partial. Log it distinctly but treat it like any
# other failure — skip the downstream enrichment/tagging so they don't run on
# incomplete data. The committed-so-far rows are still live for the web app.
if (( rc == 3 )); then
  echo "[$(date -u +%FT%TZ)] scan wedged, partial results — skipping enrichment/tagging" >>"$LOG"
  alert "job-scan: scan wedged (exit 3)" \
    "main.py hard-exited mid-run (light batch timed out). Results are partial; enrichment/tagging skipped. See deliver.log tail."
  exit "$rc"
elif (( rc != 0 )); then
  echo "[$(date -u +%FT%TZ)] scan failed (main.py exit $rc) — skipping enrichment" >>"$LOG"
  alert "job-scan: scan failed (exit $rc)" \
    "main.py exited $rc on the nightly run; enrichment/tagging skipped. See deliver.log tail."
  exit "$rc"
fi

# Backstop enrichment. Inline enrichment in main.py is best-effort with a tight
# 5s timeout, so a transiently slow detail API (Oracle/CSOD/Goldman/Workday)
# leaves a row description-less. This mops those up the same night.
#
# --max-age-days 0 disables BOTH the enrichment-scope window AND the
# description-pruning step that flag also controls (they share one arg —
# learned the hard way 2026-07-01, when --max-age-days 1 for a supposed
# enrichment-scope test instead nulled ~23k descriptions DB-wide). Keep
# descriptions permanently: at 242MB total DB / ~120MB of description text,
# there's no real storage pressure, and losing captured text for delisted
# roles defeats the point of badging them instead of deleting them.
.venv/bin/python enrich_descriptions.py --max-age-days 0 --limit 400 >>"$LOG" 2>&1

# Desc-facet re-tag hook. Many ATSes (Oracle/Workday) ship no description in the
# listing payload, so the scan's tag pass ran BEFORE the description existed —
# leaving the description-derived facets (lang_req/education/start_date) NULL.
# The enrich backstop above just filled some of those descriptions; re-tag the
# rows that gained one so their new facets populate. Bounded (≤300/run in the
# script) so a large backlog drains over several nights without blowing the
# shared Haiku quota. Best-effort — a failure here must not fail the nightly run.
.venv/bin/python backfill_tags.py --retag-missing-desc-facets >>"$LOG" 2>&1 || \
  echo "[$(date -u +%FT%TZ)] desc-facet re-tag failed (non-fatal)" >>"$LOG"
