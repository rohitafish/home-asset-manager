#!/usr/bin/env bash
# certbot --deploy-hook for scripts/certbot-renew.sh: put the renewed
# certificate where Caddy reads it and reload Caddy. certbot sets
# RENEWED_LINEAGE to the live/ directory of the certificate it just renewed.
#
# Mac: Caddy (brew services) runs as the same user and reads the certbot
#      live/ paths directly (scripts/Caddyfile.example), so a restart is all.
# Linux: Caddy (apt) runs as its own `caddy` user and cannot read
#      ~/.certbot, so the pair is copied to /etc/caddy/certs/<name>/ as
#      root:caddy 0640, then Caddy is reloaded. Both steps are sudo -n: the
#      app user on the Linux host has passwordless sudo, and this never
#      prompts.
set -euo pipefail

case "$(uname -s)" in
  Darwin)
    brew services restart caddy
    ;;
  Linux)
    : "${RENEWED_LINEAGE:?certbot did not set RENEWED_LINEAGE}"
    NAME="$(basename "$RENEWED_LINEAGE")"
    DEST="/etc/caddy/certs/$NAME"
    sudo -n install -d -o root -g caddy -m 750 /etc/caddy/certs "$DEST"
    sudo -n install -o root -g caddy -m 640 "$RENEWED_LINEAGE/fullchain.pem" "$DEST/fullchain.pem"
    sudo -n install -o root -g caddy -m 640 "$RENEWED_LINEAGE/privkey.pem" "$DEST/privkey.pem"
    sudo -n systemctl reload caddy
    ;;
  *)
    echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
