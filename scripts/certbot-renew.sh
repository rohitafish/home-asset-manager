#!/usr/bin/env bash
# Renews the assets.rohita.com certificate and reloads Caddy on change. Run
# daily by ~/Library/LaunchAgents/com.assetmgt.certrenew.plist on a Mac, or
# assetmgt-certrenew.timer (scripts/systemd/) on Linux -- see AGENTS.md's
# "Deployment topology" for the ACM ACME endpoint setup this issues against.
# The deploy hook (certbot-deploy-hook.sh) is what differs per OS.
#
# certbot's state lives under $HOME/.certbot, deliberately outside this repo
# -- redeploy.sh's rsync --delete would otherwise wipe it on every deploy,
# same reasoning as .env being excluded there.
set -euo pipefail

CERTBOT_DIR="$HOME/.certbot"

# The renewal window is the whole cost lever (AGENTS.md, "What issuance
# costs"): the endpoint's certificates last 45 days and every issuance is
# billed, so certbot's 30-day default renews every 15 days, more than twice
# as often as the 10 days chosen here. certbot only ever writes this setting
# as a commented-out default, so it exists only if someone adds it by hand,
# and a re-issued or migrated renewal config quietly goes without it. That
# happened: every renewal from 2026-09-23 to 2026-10-20 ran at the default,
# and the status check read the comment as the setting. So enforce it on
# every run instead of trusting the file.
RENEW_BEFORE_EXPIRY="10 days"

# Sets renew_before_expiry in each renewal config that lacks it or differs.
# Returns non-zero on a write failure so the caller can warn; it must never
# stop the renewal itself -- an expired certificate costs more than an extra
# issuance does.
enforce_renew_window() {
  local conf current
  for conf in "$CERTBOT_DIR"/config/renewal/*.conf; do
    [ -f "$conf" ] || continue
    # macOS AppleDouble litter (._name.conf) from a copied config, not a config.
    case "$(basename "$conf")" in ._*) continue ;; esac
    # A single sed, not `grep | head`: under pipefail a producer killed by
    # SIGPIPE fails the pipeline. The anchored pattern skips the commented
    # default, which is exactly the line that fooled the status check.
    current="$(sed -nE '/^renew_before_expiry *=/{s/^renew_before_expiry *= *//;p;q;}' "$conf")" || return 1
    [ "$current" = "$RENEW_BEFORE_EXPIRY" ] && continue
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] certbot-renew.sh: $(basename "$conf") had renew_before_expiry=${current:-unset (the 30-day certbot default)}; setting $RENEW_BEFORE_EXPIRY" >&2
    if [ -n "$current" ]; then
      sed -i.bak -E "s/^renew_before_expiry *=.*/renew_before_expiry = $RENEW_BEFORE_EXPIRY/" "$conf" || return 1
    elif grep -qE '^# *renew_before_expiry' "$conf"; then
      sed -i.bak -E "s/^# *renew_before_expiry *=.*/renew_before_expiry = $RENEW_BEFORE_EXPIRY/" "$conf" || return 1
    else
      sed -i.bak "1i\\
renew_before_expiry = $RENEW_BEFORE_EXPIRY
" "$conf" || return 1
    fi
    rm -f "$conf.bak"
  done
}

enforce_renew_window \
  || echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] certbot-renew.sh: could not enforce renew_before_expiry=$RENEW_BEFORE_EXPIRY; renewing anyway" >&2

certbot renew \
  --config-dir "$CERTBOT_DIR/config" \
  --work-dir "$CERTBOT_DIR/work" \
  --logs-dir "$CERTBOT_DIR/logs" \
  --issuance-timeout 120 \
  --deploy-hook "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/certbot-deploy-hook.sh" \
  --quiet
