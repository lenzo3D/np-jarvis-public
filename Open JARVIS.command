#!/usr/bin/env bash
# Double-click to open the JARVIS control panel. The first time, it installs everything for you.
cd "$(dirname "$0")"
URL=http://127.0.0.1:8765
if [ ! -x .venv/bin/python ]; then
  exec ./install.sh
fi
if curl -s -o /dev/null --max-time 1 "$URL/" -H "Host: 127.0.0.1:8765"; then
  open "$URL"
else
  echo "Starting the JARVIS control panel. Keep this window open, or run scripts/install_launchagent.sh once"
  echo "so it starts by itself at login."
  JARVIS_UI_OPEN=1 exec .venv/bin/python -m ui
fi
