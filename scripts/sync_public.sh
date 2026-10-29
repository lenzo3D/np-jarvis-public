#!/usr/bin/env bash
# Mirror the current files of this repo into the public repo as one new commit (no private history is copied).
#   scripts/sync_public.sh <public-repo-clone-url-or-path> [source-commit-message]
# Files listed in .publicignore (one path or glob per line) are left out. Used by .github/workflows/sync-public.yml.
set -euo pipefail
TARGET="${1:?usage: scripts/sync_public.sh <public repo url or path> [message]}"
REPO=$(cd "$(dirname "$0")/.." && pwd)
MSG="${2:-$(git -C "$REPO" log -1 --format=%s)}"

# Never publish these, whatever else happens.
for bad in secrets.env state/ workspace/ tests/fixtures/bs/ docs/probe-results.md; do
  if git -C "$REPO" ls-files --error-unmatch "$bad" >/dev/null 2>&1; then
    echo "Refusing to publish: $bad is tracked." >&2; exit 1
  fi
done

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
git clone -q "$TARGET" "$WORK/pub"
cd "$WORK/pub"
git ls-files -z | xargs -0 -r git rm -q --cached -- >/dev/null 2>&1 || true
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +

git -C "$REPO" archive HEAD | tar -xf - -C "$WORK/pub"
if [ -f "$REPO/.publicignore" ]; then
  while IFS= read -r pat; do
    case "$pat" in ""|\#*) continue ;; esac
    rm -rf "$WORK/pub"/$pat
  done < "$REPO/.publicignore"
fi

git add -A
if git diff --cached --quiet; then echo "Public repo is already up to date."; exit 0; fi
git -c user.name="${GIT_AUTHOR_NAME:-JARVIS sync}" -c user.email="${GIT_AUTHOR_EMAIL:-noreply@users.noreply.github.com}" commit -q -m "$MSG"
git push -q origin HEAD
echo "Published: $MSG"
