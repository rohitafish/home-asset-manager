#!/usr/bin/env bash
# scripts/check-pii.sh is a SHARED ENGINE: byte-identical in assetmgt and
# gmail_labels, with everything repo-specific in scripts/check-pii.conf. This
# moves the engine between the two and is the only way it should travel.
#
#   scripts/sync-check-pii.sh          # report whether they differ (exit 1 if so)
#   scripts/sync-check-pii.sh --push   # copy THIS repo's engine to the sibling
#   scripts/sync-check-pii.sh --pull   # copy the SIBLING's engine to this repo
#
# Why this exists rather than "remember to copy it": the two copies were
# hand-synced for a while, and three fixes reached one repo and not the other.
# One of them was a silent false-clean -- check-pii.sh reporting "nothing to
# check", exit 0, on a range git could not resolve -- which is the worst thing
# this particular tool can do, and it sat that way for two days.
# tests/test_check_pii_shared.py fails while the copies differ, so a divergence
# is caught by the suite the pre-push hook already runs.
#
# The conf is NEVER copied: that is the file that is supposed to differ.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="$REPO_DIR/scripts/check-pii.sh"
CONF="$REPO_DIR/scripts/check-pii.conf"

PII_SIBLING=""
# shellcheck source=/dev/null
[ -f "$CONF" ] && . "$CONF"
[ -n "$PII_SIBLING" ] || { echo "!!! PII_SIBLING is not set in scripts/check-pii.conf" >&2; exit 2; }

SIBLING_DIR="$(cd "$REPO_DIR/$PII_SIBLING" 2>/dev/null && pwd)" \
  || { echo "!!! sibling repo not found at $PII_SIBLING (relative to $REPO_DIR)" >&2; exit 3; }
SIBLING_ENGINE="$SIBLING_DIR/scripts/check-pii.sh"
[ -f "$SIBLING_ENGINE" ] || { echo "!!! no engine at $SIBLING_ENGINE" >&2; exit 3; }

MODE="check"
case "${1:-}" in
  "") ;;
  --push) MODE="push" ;;
  --pull) MODE="pull" ;;
  -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

if cmp -s "$ENGINE" "$SIBLING_ENGINE"; then
  echo "== check-pii engine: in sync with $SIBLING_DIR =="
  exit 0
fi

echo "== check-pii engine: DIFFERS from $SIBLING_DIR =="
diff -u "$SIBLING_ENGINE" "$ENGINE" | sed -n '1,40p' || true
echo "  (--- sibling, +++ this repo; first 40 lines)"

case "$MODE" in
  check)
    echo
    echo "  Nothing copied. Re-run with --push (this repo wins) or --pull (the sibling wins)."
    echo "  Then run BOTH test suites: the engine change has to hold in both repos."
    exit 1
    ;;
  push) cp "$ENGINE" "$SIBLING_ENGINE"; echo; echo "  copied: this repo -> $SIBLING_ENGINE" ;;
  pull) cp "$SIBLING_ENGINE" "$ENGINE"; echo; echo "  copied: $SIBLING_ENGINE -> this repo" ;;
esac
echo "  Now run both repos' test suites before committing either."
