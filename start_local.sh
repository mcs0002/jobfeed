#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -x "$PROJECT_DIR/.venv/bin/uvicorn" ]]; then
  echo "Die virtuelle Umgebung fehlt. Bitte die Schritte in LOKAL_STARTEN.md ausfuehren." >&2
  exit 1
fi

echo "Jobfeed startet auf http://127.0.0.1:${PORT:-8000}"
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/uvicorn" web.app:app \
  --host 127.0.0.1 \
  --port "${PORT:-8000}"
