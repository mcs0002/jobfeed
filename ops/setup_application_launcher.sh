#!/usr/bin/env bash
# Install the narrow, per-user Jobfeed Accessibility helper on the M1.
set -euo pipefail
cd "$(dirname "$0")/.."

APP="$HOME/Applications/Jobfeed Antigravity Launcher.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
SUPPORT="$HOME/Library/Application Support/Jobfeed"
PLIST="$HOME/Library/LaunchAgents/com.example.jobfeed-antigravity-launcher.plist"
mkdir -p "$MACOS" "$SUPPORT/application-launcher-queue" "$HOME/Library/LaunchAgents"
chmod 700 "$SUPPORT" "$SUPPORT/application-launcher-queue"

/usr/bin/swiftc -O -framework AppKit -framework ApplicationServices \
  ops/macos/JobfeedAntigravityLauncher.swift -o "$MACOS/JobfeedAntigravityLauncher"

cat > "$CONTENTS/Info.plist" <<'PLISTEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>JobfeedAntigravityLauncher</string>
<key>CFBundleIdentifier</key><string>com.example.jobfeed-antigravity-launcher</string>
<key>CFBundleName</key><string>Jobfeed Antigravity Launcher</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLISTEOF
/usr/bin/codesign --force --deep --sign - "$APP"

TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
PUBLIC_HOST=$($TS status --json 2>/dev/null | /usr/bin/python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)
if [ -n "$PUBLIC_HOST" ] && ! grep -q '^WEB_PUBLIC_BASE_URL=' .env; then
  printf '\nWEB_PUBLIC_BASE_URL=https://%s:8443\n' "$PUBLIC_HOST" >> .env
fi

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.example.jobfeed-antigravity-launcher</string>
<key>ProgramArguments</key><array>
<string>${MACOS}/JobfeedAntigravityLauncher</string><string>${PWD}</string>
</array>
<key>WorkingDirectory</key><string>${PWD}</string>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>/dev/null</string>
<key>StandardErrorPath</key><string>/dev/null</string>
</dict></plist>
PLISTEOF
launchctl bootout "gui/$(id -u)/com.example.jobfeed-antigravity-launcher" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/com.example.jobfeed-antigravity-launcher"
echo ">>> installed Jobfeed Antigravity Launcher.app"
echo ">>> grant Accessibility to $APP if macOS prompts"
