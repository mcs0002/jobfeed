#!/bin/zsh
# Publish the shareable subset of jobs.db to Cloudflare R2.
#
# Runs after the nightly scan, not during it: the scan's delisting pass moves
# the row count underneath an export that starts too early (observed
# 2026-09-01, 52,943 -> 51,827 mid-run).
#
# Credentials live in rclone's own config (~/.config/rclone/rclone.conf), never
# here. Configure once with:  rclone config   ->  new remote, type "s3",
# provider "Cloudflare", and the R2 endpoint from the Cloudflare dashboard.
#
# Logs to ~/Library/Logs/jobs-publish.log.

set -uo pipefail

PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

REPO_DIR="$HOME/projects/job_scraper"
EXPORT_DB="$REPO_DIR/jobs_shared.db"
REMOTE="${JOBS_R2_REMOTE:-r2}"
BUCKET="${JOBS_R2_BUCKET:-jobfeed}"

LOG="$HOME/Library/Logs/jobs-publish.log"
mkdir -p "$(dirname "$LOG")"
# tee rather than plain redirect, so a manual run still prints to the
# terminal while launchd runs still leave a trail.
exec > >(tee -a "$LOG") 2>&1

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" }

# Every abort path emails, because this job failed silently for a week
# (2026-09-01 to 09-08): it logged FATAL every night, exited non-zero, and
# nothing read either. A log nobody reads is not a signal. Exit code 3 (a scan
# is mid-flight) is the one exception — that is the script declining to publish
# a moving target, which is correct behaviour and self-corrects tomorrow.
fail() {
    local code="$1"; shift
    log "FATAL: $*"
    if [[ "$code" != "3" && -x "$REPO_DIR/.venv/bin/python" ]]; then
        (cd "$REPO_DIR" && .venv/bin/python -m jobfeed.notify \
            "R2 publish failed" \
            "publish_shared.sh aborted (exit ${code}).

$*

The shared jobs.db on R2 is now stale. Log: ${LOG}" >/dev/null 2>&1) \
            || log "WARN: alert email failed"
    fi
    exit "$code"
}

log "=== publish start ==="

if ! command -v rclone >/dev/null 2>&1; then
    fail 2 "rclone not installed"
fi

# Refuse to publish while a scan is mid-flight, for the reason in the header.
if pgrep -f "job_scraper.*deliver" >/dev/null 2>&1; then
    fail 3 "a scan is still running; not publishing a moving target"
fi

if ! rclone listremotes 2>/dev/null | grep -q "^${REMOTE}:"; then
    fail 2 "rclone remote '${REMOTE}' is not configured. Run: rclone config"
fi

# --- export ---------------------------------------------------------------
# export_shared.py aborts on its own if the schema grew a column it has not
# been told to classify, so a schema change stops the publish rather than
# leaking a new personal field.
#
# Use the venv interpreter, as every other entrypoint here does. Bare `python3`
# is macOS's 3.9.6 with SQLite 3.51, which cannot open a WAL database
# read-only when no -shm file exists; the venv's 3.12 (SQLite 3.53) can. This
# published once on 2026-09-01 and failed every night after, because that run
# happened to coincide with another process holding the DB open and creating
# the -shm. A latent race that became permanent.
log "exporting..."
if ! "$REPO_DIR/.venv/bin/python" "$REPO_DIR/scripts/export_shared.py" \
        --source "$REPO_DIR/jobs.db" \
        --dest "$EXPORT_DB" \
        --gzip 2>&1; then
    fail 4 "export failed; nothing uploaded"
fi

SIZE=$(ls -lh "${EXPORT_DB}.gz" | awk '{print $5}')
log "export ok (${SIZE} gzipped)"

# --- upload ---------------------------------------------------------------
# Upload to a dated key first, then update the stable one, so a consumer
# mid-download never sees a truncated "latest".
STAMP=$(date -u '+%Y%m%d')
log "uploading to ${REMOTE}:${BUCKET}/..."

if ! rclone copyto "${EXPORT_DB}.gz" \
        "${REMOTE}:${BUCKET}/snapshots/jobs_shared_${STAMP}.db.gz" \
        --s3-no-check-bucket 2>&1; then
    fail 5 "upload of dated snapshot failed"
fi

if ! rclone copyto "${REMOTE}:${BUCKET}/snapshots/jobs_shared_${STAMP}.db.gz" \
        "${REMOTE}:${BUCKET}/latest/jobs_shared.db.gz" \
        --s3-no-check-bucket 2>&1; then
    fail 6 "promotion to latest failed (dated snapshot is uploaded)"
fi

# Keep 14 dated snapshots so a bad export can be rolled back from.
rclone delete "${REMOTE}:${BUCKET}/snapshots" \
    --min-age 14d --include "jobs_shared_*.db.gz" 2>&1 | sed 's/^/  /'

rm -f "$EXPORT_DB" "${EXPORT_DB}.gz"

log "=== publish done (${SIZE}) ==="
