#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "Die virtuelle Umgebung fehlt. Bitte die Schritte in LOKAL_STARTEN.md ausfuehren." >&2
  exit 1
fi

cd "$PROJECT_DIR"

# The local .env selects TAG_PROVIDER=codex, so the richer classification uses
# the already authenticated Codex CLI. Pass --no-tag explicitly if an offline
# deterministic fallback is preferred for a particular run.
# Cap best-effort description downloads on a large first run. Every listing is
# stored before this stage; callers can override the cap with --max-enrich N.
exec "$PROJECT_DIR/.venv/bin/python" main.py --max-enrich 500 "$@"
