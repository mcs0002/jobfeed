#!/usr/bin/env bash
# One-time M1 setup for the roles web app. Idempotent — safe to re-run.
#   ssh m1 'cd ~/projects/job_scraper && git pull --ff-only && bash setup_web.sh'
set -euo pipefail
cd "$(dirname "$0")"

# Non-interactive SSH strips PATH — make sure the usual install dirs are found.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# 1. Dependencies into the project venv — from the LOCK, matching deliver.sh's
#    reproducible-deploy doctrine (requirements.txt is range pins for dev).
VIRTUAL_ENV="$PWD/.venv" uv pip sync -q requirements.lock

# 2. Ensure WEB_PASSWORD + WEB_SECRET exist in .env (generate if missing).
touch .env
if ! grep -q '^WEB_PASSWORD=' .env; then
  PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(9))")
  echo "WEB_PASSWORD=$PW" >> .env
  echo ">>> GENERATED WEB_PASSWORD=$PW   (use this to log in)"
fi
grep -q '^WEB_SECRET=' .env || \
  echo "WEB_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")" >> .env

# 3. Generate + (re)load the launchd web service on :8080.
#    Generated from the real paths here (M4 and M1 have different usernames,
#    so the shipped .disabled plist's hardcoded paths can't be used directly).
PLIST="$HOME/Library/LaunchAgents/com.example.jobscan-web.plist"
PROJ="$PWD"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.example.jobscan-web</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PROJ}/.venv/bin/uvicorn</string>
        <string>web.app:app</string>
        <!-- Loopback only: Tailscale Funnel/serve proxies to 127.0.0.1:8080,
             so nothing needs a wider bind. 0.0.0.0 also answered in cleartext
             on the LAN interface, password included. Access is via the
             Funnel URL (https://<host>.example.net:8443). -->
        <string>--host</string><string>127.0.0.1</string>
        <string>--port</string><string>8080</string>
        <!-- No access log: the app is publicly tunneled, so scanner noise
             would grow web.out.log without bound (KeepAlive = no rotation). -->
        <string>--no-access-log</string>
    </array>
    <key>WorkingDirectory</key><string>${PROJ}</string>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>${PROJ}/web.out.log</string>
    <key>StandardErrorPath</key><string>${PROJ}/web.err.log</string>
</dict>
</plist>
PLISTEOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo ">>> web service loaded on :8080"

# 4. Print the Funnel URL to open (the app listens on loopback only; direct
#    http://<ip>:8080 no longer answers — go through the Funnel/serve URL).
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
HOSTNAME_TS=$($TS status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)
[ -n "$HOSTNAME_TS" ] && echo ">>> open https://$HOSTNAME_TS:8443" || echo ">>> open the Funnel URL (tailscale serve status)"
