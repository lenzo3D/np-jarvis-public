#!/usr/bin/env bash
# One-time (idempotent) setup of a fresh Ubuntu 24.04 VPS for JARVIS. Run as root:
#   sudo bash deploy/bootstrap.sh <git-repo-url-or-local-path>
set -euo pipefail

REPO_SRC=${1:-}
APP=/opt/politemalljarvis
SHARE=jarvis-share

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

echo "== packages"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git curl tmux ufw unattended-upgrades python3 python3-venv python3-pip sqlite3 unzip \
  pandoc libreoffice-writer-nogui fonts-dejavu
dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo "== users and groups"
getent group "$SHARE" >/dev/null || groupadd "$SHARE"
id jarvis-ops >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin jarvis-ops
id jarvis >/dev/null 2>&1 || useradd --create-home --shell /bin/bash jarvis
usermod -aG "$SHARE" jarvis
usermod -aG "$SHARE" jarvis-ops

echo "== firewall (SSH only)"
ufw allow OpenSSH
ufw --force enable

echo "== code"
if [ ! -d "$APP/.git" ]; then
  [ -n "$REPO_SRC" ] || { echo "usage: bootstrap.sh <repo-url-or-path>"; exit 1; }
  git clone "$REPO_SRC" "$APP"
fi
chown -R jarvis-ops:"$SHARE" "$APP"
chmod 755 "$APP"
install -d -o jarvis-ops -g jarvis-ops -m 700 "$APP/state"
install -d -o jarvis -g "$SHARE" -m 2775 "$APP/workspace"
# the blanket chown above would clobber ownership of existing data on a re-run
chown -R jarvis-ops:jarvis-ops "$APP/state"
chown -R jarvis:"$SHARE" "$APP/workspace"
if [ ! -f "$APP/secrets.env" ]; then
  install -o jarvis-ops -g jarvis-ops -m 600 "$APP/secrets.env.example" "$APP/secrets.env"
fi
chmod 600 "$APP/secrets.env"; chown jarvis-ops:jarvis-ops "$APP/secrets.env"

echo "== python env (owned by jarvis-ops, readable by all)"
sudo -u jarvis-ops python3 -m venv "$APP/.venv"
sudo -u jarvis-ops "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u jarvis-ops "$APP/.venv/bin/pip" install -q -e "$APP"
"$APP/.venv/bin/python" -m playwright install-deps chromium
sudo -u jarvis-ops "$APP/.venv/bin/python" -m playwright install chromium

echo "== Claude Code for the jarvis user"
sudo -u jarvis -H bash -lc 'command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash'
sudo -u jarvis -H python3 -m pip install --user --break-system-packages -q python-docx python-pptx pdfplumber openpyxl
install -d -o jarvis -g jarvis -m 700 /home/jarvis/.claude
if [ ! -f /home/jarvis/.claude/settings.json ]; then
  install -o jarvis -g jarvis -m 600 "$APP/deploy/claude-settings.json" /home/jarvis/.claude/settings.json
fi
install -o jarvis -g jarvis -m 644 "$APP/deploy/CLAUDE.md" "$APP/workspace/CLAUDE.md"

echo "== sudo: ops may run only claude as jarvis (for summaries and Do-it runs)"
cat > /etc/sudoers.d/jarvis-ops <<SUDO
jarvis-ops ALL=(jarvis) NOPASSWD: /home/jarvis/.local/bin/claude
SUDO
chmod 440 /etc/sudoers.d/jarvis-ops
visudo -cf /etc/sudoers.d/jarvis-ops

echo "== systemd units"
install -m 644 "$APP"/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

cat <<NEXT

Bootstrap done. Next steps are in deploy/README.md (step 3 onwards):
  - fill $APP/secrets.env (as root: nano $APP/secrets.env)
  - sudo -u jarvis -H /home/jarvis/.local/bin/claude   (then /login)
  - copy your skills, then do the first logins (or copy state/ from the Mac)
  - systemctl enable --now jarvis-ops jarvis-telethon
NEXT
