#!/usr/bin/env bash
# Make the JARVIS control panel start by itself when you log in to this Mac (and restart if it ever quits).
#   scripts/install_launchagent.sh            install / update
#   scripts/install_launchagent.sh --remove   uninstall
# Then bookmark http://127.0.0.1:8765
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
PY="$REPO/.venv/bin/python"
LABEL=com.politemalljarvis.panel
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$REPO/state/logs"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed. The panel will no longer start at login."
  exit 0
fi

[ -x "$PY" ] || { echo "Missing $PY. Create the virtualenv first (see README)."; exit 1; }
mkdir -p "$LOGS" "$(dirname "$PLIST")"

# launchd starts agents with a minimal PATH; include the usual homes of the `claude` command.
AGENT_PATH="$HOME/.local/bin:$HOME/.claude/local:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>-m</string><string>ui</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$AGENT_PATH</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOGS/panel.log</string>
  <key>StandardErrorPath</key><string>$LOGS/panel.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"
echo "Installed. JARVIS panel: http://127.0.0.1:8765  (log: $LOGS/panel.log)"
