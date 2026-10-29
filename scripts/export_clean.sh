#!/usr/bin/env bash
# Make a copy of the committed files with a brand-new git history, ready to publish as a public repo.
#   scripts/export_clean.sh ../jarvis-public
# The old history is left behind on purpose: it can contain personal data that was removed from the files later.
set -euo pipefail
OUT="${1:?usage: scripts/export_clean.sh <new-folder>}"
[ ! -e "$OUT" ] || { echo "$OUT already exists."; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$OUT"
git -C "$REPO" archive HEAD | tar -xf - -C "$OUT"
cd "$OUT"
git init -q -b main
git add -A
git commit -qm "Initial public release"
echo "Created $OUT with a single commit. Next:"
echo "  cd $OUT && git remote add origin <your new github repo url> && git push -u origin main"
