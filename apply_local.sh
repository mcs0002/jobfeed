#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE="$PROJECT_DIR/secrets/applicant_profile.json"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "Die virtuelle Umgebung fehlt. Bitte zuerst LOKAL_STARTEN.md lesen." >&2
  exit 1
fi
for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    cd "$PROJECT_DIR"
    exec "$PROJECT_DIR/.venv/bin/python" apply.py "$@"
  fi
done
if [[ ! -f "$PROFILE" ]]; then
  echo "Bewerberprofil fehlt: $PROFILE" >&2
  echo "Es wird nichts geöffnet oder übertragen." >&2
  exit 2
fi

export JOBS_DB="$PROJECT_DIR/jobs.db"
export PLAYWRIGHT_BROWSERS_PATH="$PROJECT_DIR/.playwright-browsers"
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" apply.py "$@"
