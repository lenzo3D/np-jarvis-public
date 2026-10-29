#!/usr/bin/env bash
# One-step installer for JARVIS (macOS or Linux). Safe to run again: it only fills in what is missing.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nProblem: %s\n' "$1" >&2; exit 1; }

PY=""
for cand in python3.13 python3.12 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then PY="$cand"; break; fi
done
[ -n "$PY" ] || fail "Python 3.12 or newer is needed. On a Mac: install Homebrew from https://brew.sh, then run: brew install python@3.12  and run ./install.sh again."

say "Creating the private Python environment (.venv)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
say "Installing JARVIS and its dependencies (a minute or two)"
.venv/bin/python -m pip install --quiet -e .
say "Installing the browser JARVIS uses to sign in to POLITEMall and Teams"
.venv/bin/python -m playwright install chromium

if ! command -v claude >/dev/null 2>&1; then
  printf '\nNote: the Claude Code command ("claude") was not found. JARVIS needs it for chat, briefings and summaries.\n'
  printf 'Install it from https://claude.com/claude-code, then run:  claude   (and sign in once).\n'
fi

chmod +x "Open JARVIS.command" scripts/*.sh 2>/dev/null || true
say "Checking the install"
.venv/bin/python scripts/doctor.py || true

if [ "$(uname)" = "Darwin" ] && [ "${1:-}" != "--no-autostart" ]; then
  printf '\nStart JARVIS automatically when you log in to this Mac? [Y/n] '
  read -r ans || ans=n
  case "${ans:-Y}" in [Nn]*) ;; *) scripts/install_launchagent.sh ;; esac
fi

say "Opening the control panel. The setup wizard will guide you from here."
URL=http://127.0.0.1:8765
if curl -s -o /dev/null --max-time 1 "$URL/" -H "Host: 127.0.0.1:8765"; then
  command -v open >/dev/null && open "$URL" || echo "Open $URL in your browser."
else
  echo "Keep this window open while you use JARVIS (or install auto-start). Close it to stop the panel."
  JARVIS_UI_OPEN=1 exec .venv/bin/python -m ui
fi
