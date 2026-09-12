#!/usr/bin/env bash
# Install (or refresh) the app's systemd units on a Linux host. The Linux
# counterpart of the README's cp/sed/launchctl-load step for the plists. Run
# ON the host, as the app user, from the repo root (it fills in the
# ASSETMGT_DIR / ASSETMGT_USER / ASSETMGT_HOME placeholders):
#
#   ./scripts/install-systemd-units.sh
#
# Idempotent: re-run after editing anything under scripts/systemd/. Like the
# plists, redeploy.sh does NOT do this for you -- a unit change needs this
# script run by hand on the host.
#
# Everything is a system unit running as this user (User=), so it starts at
# boot with nobody logged in and needs no loginctl enable-linger. The one
# privileged step is writing to /etc/systemd/system, via sudo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_DIR/scripts/systemd"
USER_NAME="$(id -un)"
HOME_DIR="$HOME"

[ "$(uname -s)" = "Linux" ] || { echo "!!! This installs systemd units; on a Mac use the plists (README)." >&2; exit 1; }
[ -x "$REPO_DIR/.venv/bin/uvicorn" ] || { echo "!!! $REPO_DIR/.venv is missing -- create it first (README, 'Clone and configure')." >&2; exit 1; }
mkdir -p "$REPO_DIR/logs"

echo "==> installing units to /etc/systemd/system (user=$USER_NAME dir=$REPO_DIR)"
for src in "$UNIT_SRC"/assetmgt-*.service "$UNIT_SRC"/assetmgt-*.timer; do
  name="$(basename "$src")"
  sed -e "s|__ASSETMGT_DIR__|$REPO_DIR|g" \
      -e "s|__ASSETMGT_USER__|$USER_NAME|g" \
      -e "s|__ASSETMGT_HOME__|$HOME_DIR|g" "$src" | sudo tee "/etc/systemd/system/$name" >/dev/null
  if grep -qE '__ASSETMGT_(DIR|USER|HOME)__' "/etc/systemd/system/$name"; then
    echo "!!! $name still has an unsubstituted placeholder" >&2; exit 1
  fi
  echo "    $name"
done
sudo systemctl daemon-reload

echo "==> enabling"
sudo systemctl enable --now assetmgt-app.service >/dev/null
for t in backup logrotate certrenew; do sudo systemctl enable --now "assetmgt-$t.timer" >/dev/null; done
# A unit file edit needs a restart to take effect; enable --now on an
# already-running service is a no-op.
sudo systemctl restart assetmgt-app.service

echo "==> state"
systemctl is-active assetmgt-app.service | sed 's/^/    app: /'
systemctl list-timers 'assetmgt-*' --no-legend | awk '{print "    " $NF " next " $1 " " $2 " " $3}'
