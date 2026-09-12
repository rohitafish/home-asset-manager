#!/usr/bin/env bash
# Keep .pii-denylist in step with the live inventory, so scripts/check-pii.sh
# can actually do its job. Run from the DEV MAC (the denylist is gitignored
# and dev-machine-only; redeploy.sh copies it to the host afterwards).
#
#   scripts/pii-denylist-sync.sh              # sync from $DEPLOY_HOST (default mint)
#   scripts/pii-denylist-sync.sh --dry-run    # report what would change, write nothing
#   scripts/pii-denylist-sync.sh --host HOST  # a specific host
#
# Why: the denylist is hand-maintained, and on 2026-09-12 it held 1 of the 56
# MAC addresses in the inventory, 11 of 29 serials and 2 of 3 owner names. A
# check that knows a fraction of the real values catches a fraction of the
# leaks -- and the same day, real MACs went to GitHub in a test file because
# nothing knew they were real. The inventory already IS the authoritative
# list of this household's device identifiers; this just reads it.
#
# What it pulls: every interface MAC, every serial number, and the owner and
# custodian names -- from the host's Postgres, over one ssh round trip -- plus
# DEFAULT_OWNER / SECONDARY_OWNER_* from the host's .env (household names).
# Not hostnames (too many are generic product names, "Sonos Move", that
# legitimately appear in docs and tests), not model numbers (public product
# codes), not private IPs/subnets (the docs' illustrative addresses would
# collide). Add those by hand if a specific one matters.
#
# The pulled values are written into a marked block at the END of
# .pii-denylist; everything above the marker is yours and is never touched.
# The block is rewritten whole each run, so a device removed from the
# inventory drops out of it (keep it by moving it above the marker).
#
# Never prints a value: only counts. Same invariant as env-structure.sh.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DENYLIST="$REPO_DIR/.pii-denylist"
HOST="${DEPLOY_HOST:-mint}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-~/claudecode/assetmgt}"
DRY_RUN=0
BEGIN_MARK="# >>> auto-synced from the live inventory by scripts/pii-denylist-sync.sh -- edit ABOVE this line, not below"
END_MARK="# <<< end auto-synced"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --host) HOST="${2:?--host needs a value}"; shift ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# One ssh round trip. The SQL runs inside the db container (same reasoning as
# backup-db.sh); the .env read is the grep/cut idiom every other script uses,
# so no unrelated secret is exported. Output: one value per line, already
# de-duplicated and trimmed, nothing else.
PULLED="$(ssh -o ConnectTimeout=10 "$HOST" REMOTE_DIR="$REMOTE_DIR" bash -s <<'REMOTE'
set -euo pipefail
cd "${REMOTE_DIR/#\~/$HOME}"
docker compose exec -T db psql -U assetmgt -d assetmgt -tA <<'SQL'
select distinct v from (
  select lower(mac) as v from assetinterface where mac is not null
  union select serial_number from asset where serial_number is not null and length(serial_number) >= 6
  union select owner from asset where owner is not null
  union select custodian from asset where custodian is not null
) t where v is not null and length(trim(v)) >= 3 order by v;
SQL
for k in DEFAULT_OWNER SECONDARY_OWNER_NAME SECONDARY_OWNER_HOSTNAME_KEYWORD; do
  grep -m1 "^${k}=" .env 2>/dev/null | cut -d= -f2-
done
REMOTE
)" || { echo "!!! could not read the inventory on '$HOST' -- nothing changed." >&2; exit 3; }

# Drop template defaults and anything too short to be an identifier.
NEW_BLOCK="$(printf '%s\n' "$PULLED" | sed 's/[[:space:]]*$//' | awk 'length($0) >= 3 && tolower($0) != "owner" && tolower($0) != "unknown"' | sort -u)"
NEW_COUNT="$(printf '%s\n' "$NEW_BLOCK" | grep -c . || true)"
[ "$NEW_COUNT" -gt 0 ] || { echo "!!! the inventory returned no identifiers -- refusing to write an empty block." >&2; exit 1; }

# Split the existing file into the hand-written part and the old block.
if [ -f "$DENYLIST" ]; then
  HAND="$(awk -v b="$BEGIN_MARK" '$0 == b {exit} {print}' "$DENYLIST")"
  OLD_BLOCK="$(awk -v b="$BEGIN_MARK" -v e="$END_MARK" '$0 == e {f=0} f {print} $0 == b {f=1}' "$DENYLIST")"
else
  HAND="# Literal strings that must NEVER appear in a commit to this repo.
# One per line. Case-insensitive substring match (scripts/check-pii.sh)."
  OLD_BLOCK=""
fi
OLD_COUNT="$(printf '%s\n' "$OLD_BLOCK" | grep -c . || true)"
ADDED="$(comm -13 <(printf '%s\n' "$OLD_BLOCK" | grep . | sort -u) <(printf '%s\n' "$NEW_BLOCK") | grep -c . || true)"
REMOVED="$(comm -23 <(printf '%s\n' "$OLD_BLOCK" | grep . | sort -u) <(printf '%s\n' "$NEW_BLOCK") | grep -c . || true)"
HAND_COUNT="$(printf '%s\n' "$HAND" | grep -vE '^\s*(#|$)' | grep -c . || true)"

echo "== .pii-denylist sync from $HOST =="
echo "  hand-written entries : $HAND_COUNT (untouched)"
echo "  auto block           : $OLD_COUNT -> $NEW_COUNT entries (+$ADDED / -$REMOVED)"
if [ "$DRY_RUN" -eq 1 ]; then echo "  (dry run -- nothing written)"; exit 0; fi

TMP="$(mktemp "$REPO_DIR/.pii-denylist.XXXXXX")"
{
  printf '%s\n' "$HAND" | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}'
  printf '\n%s\n' "$BEGIN_MARK"
  printf '%s\n' "$NEW_BLOCK"
  printf '%s\n' "$END_MARK"
} > "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$DENYLIST"
echo "  written: $DENYLIST ($(grep -vcE '^\s*(#|$)' "$DENYLIST") entries in total)"
