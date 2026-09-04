#!/usr/bin/env bash
# Renews the assets.rohita.com certificate and reloads Caddy on change. Run
# daily by ~/Library/LaunchAgents/com.assetmgt.certrenew.plist (installed
# from scripts/com.assetmgt.certrenew.plist -- see AGENTS.md's "Deployment
# topology" for the ACM ACME endpoint setup this issues against).
#
# certbot's state lives under $HOME/.certbot, deliberately outside this repo
# -- redeploy.sh's rsync --delete would otherwise wipe it on every deploy,
# same reasoning as .env being excluded there.
set -euo pipefail

CERTBOT_DIR="$HOME/.certbot"

certbot renew \
  --config-dir "$CERTBOT_DIR/config" \
  --work-dir "$CERTBOT_DIR/work" \
  --logs-dir "$CERTBOT_DIR/logs" \
  --issuance-timeout 120 \
  --deploy-hook 'brew services restart caddy' \
  --quiet
