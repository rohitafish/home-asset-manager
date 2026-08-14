#!/usr/bin/env bash
# Nightly off-site backup of the Postgres database to S3. Run once a day by
# ~/Library/LaunchAgents/com.assetmgt.backup.plist (installed from
# scripts/com.assetmgt.backup.plist -- see README).
#
# pg_dump runs *inside* the db container via `docker compose exec`, not on
# the host, for three reasons: (1) the container's own environment already
# has POSTGRES_USER/POSTGRES_DB, so no credential has to be duplicated here;
# (2) the port is bound to 127.0.0.1 only, so a host-side dump gains nothing
# anyway; (3) a host-installed pg_dump would need to match the server's major
# version exactly -- exec'ing into postgres:16-alpine guarantees that by
# construction. DATABASE_URL in .env is NOT usable here: its
# `postgresql+psycopg://` SQLAlchemy dialect prefix is not a libpq URI.
#
# Custom format (-Fc), not plain SQL: it's compressed, and -- critically --
# a single transactionally-consistent snapshot. There is no ON DELETE CASCADE
# anywhere in this schema (see AGENTS.md), so FK integrity depends on never
# taking per-table dumps.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$REPO_DIR/backups"
KEEP_LOCAL="${BACKUP_KEEP_LOCAL:-7}"

# Reads a single KEY=VALUE line out of .env without sourcing the whole file
# -- sourcing would execute it as shell and export every unrelated secret in
# it (UniFi, Anthropic, NVD, the admin password) into this process for no
# reason. AWS credentials for the backup job are an existing, full-S3-access
# IAM identity (s3-user), by deliberate choice -- see README/AGENTS.md for
# why that's offset by S3 Object Lock further down rather than a
# least-privilege IAM policy.
_env_var() {
  [ -f "$REPO_DIR/.env" ] || return 0
  grep -m1 "^${1}=" "$REPO_DIR/.env" | cut -d= -f2-
}

export AWS_ACCESS_KEY_ID="$(_env_var BACKUP_AWS_ACCESS_KEY_ID)"
export AWS_SECRET_ACCESS_KEY="$(_env_var BACKUP_AWS_SECRET_ACCESS_KEY)"
export AWS_DEFAULT_REGION="$(_env_var BACKUP_AWS_REGION)"
BUCKET="$(_env_var BACKUP_S3_BUCKET)"

: "${AWS_ACCESS_KEY_ID:?BACKUP_AWS_ACCESS_KEY_ID is not set in .env}"
: "${AWS_SECRET_ACCESS_KEY:?BACKUP_AWS_SECRET_ACCESS_KEY is not set in .env}"
: "${AWS_DEFAULT_REGION:?BACKUP_AWS_REGION is not set in .env}"
: "${BUCKET:?BACKUP_S3_BUCKET is not set in .env}"

STAMP="$(date -u +%Y-%m-%d)"
NAME="assetmgt-$STAMP.dump"
DEST="$BACKUP_DIR/$NAME"
TMP="$BACKUP_DIR/.$NAME.partial"

# Fail loudly: set -e alone exits silently on e.g. Docker not running,
# leaving nothing in logs/backup.error.log to act on.
trap 'echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup-db.sh FAILED at line $LINENO" >&2' ERR
# Never leave a truncated dump behind that a later restore could mistake for good.
trap 'rm -f "$TMP"' EXIT

mkdir -p "$BACKUP_DIR"
cd "$REPO_DIR"   # `docker compose exec` resolves the project from the cwd

# Idempotency / catch-up guard. If today's off-site daily already exists, this
# run is a duplicate -- a catch-up tick (see the plist's multiple
# StartCalendarInterval entries) or a manual re-run -- so skip the dump and,
# critically, skip re-uploading: the daily object is under COMPLIANCE Object
# Lock and a same-key put would be rejected. This is what makes running several
# times a day safe: whichever tick first finds the Mac up does that day's
# backup; the rest no-op here. A head-object failure (e.g. no network) falls
# through to attempt the backup rather than silently skip a needed one.
if aws s3api head-object --bucket "$BUCKET" --key "daily/$NAME" >/dev/null 2>&1; then
  exit 0
fi

docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$TMP"

if [ ! -s "$TMP" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup-db.sh: pg_dump produced an empty file" >&2
  exit 1
fi

# Cheap integrity check: prove the archive's table of contents parses before
# promoting it to a real filename and shipping it off-site. A dump nobody
# can read is worse than no dump at all, because it looks like success.
docker compose exec -T db pg_restore -l < "$TMP" > /dev/null

mv "$TMP" "$DEST"

# `aws s3 cp` doesn't expose Object Lock at all -- only the lower-level
# `put-object` does (--object-lock-mode / --object-lock-retain-until-date).
# S3 itself then refuses to delete or overwrite these objects before the
# retain-until date, regardless of what IAM permissions the calling
# credential has -- since s3-user has full S3 access, this (not an IAM
# policy) is what actually protects backup history here. Output is
# redirected to /dev/null since put-object prints a JSON response on
# success, unlike `cp --only-show-errors`.
#
# Retention windows match the S3 Lifecycle rules on the bucket (30d/186d):
# Lifecycle is what eventually cleans an object up once Object Lock's
# retention has lapsed, so the two are complementary, not redundant.
# `date -v` is BSD/macOS syntax (this only ever runs on the Mini) --
# GNU `date -d "+30 days"` would silently fail here on Linux.
RETAIN_UNTIL_DAILY="$(date -u -v+30d +%Y-%m-%dT%H:%M:%SZ)"

aws s3api put-object \
  --bucket "$BUCKET" --key "daily/$NAME" --body "$DEST" \
  --server-side-encryption AES256 \
  --object-lock-mode COMPLIANCE --object-lock-retain-until-date "$RETAIN_UNTIL_DAILY" \
  > /dev/null

# Monthly retention: one longer-lived copy per calendar month. Ensure-based
# ("does this month already have one?") rather than "only on the 1st", so an
# outage spanning the 1st -- the Mac powered off with FileVault blocking
# unattended login, see README's "Power outages and unattended restart" --
# doesn't skip the whole month. The first successful daily of any month with no
# monthly copy yet creates it; later days that month find it present and skip.
# Still a second *upload*, not an S3-side copy: no ordering dependency on the
# daily upload, and its own (longer) Object Lock retention.
YEAR_MONTH="$(date -u +%Y-%m)"
if [ -z "$(aws s3 ls "s3://$BUCKET/monthly/assetmgt-$YEAR_MONTH-" 2>/dev/null)" ]; then
  RETAIN_UNTIL_MONTHLY="$(date -u -v+186d +%Y-%m-%dT%H:%M:%SZ)"
  aws s3api put-object \
    --bucket "$BUCKET" --key "monthly/$NAME" --body "$DEST" \
    --server-side-encryption AES256 \
    --object-lock-mode COMPLIANCE --object-lock-retain-until-date "$RETAIN_UNTIL_MONTHLY" \
    > /dev/null
fi

# Success marker -- the only thing the app reads back (see /health and /summary).
date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP_DIR/last-success"

# Local copies are a convenience fast-path only; S3 holds the real history.
# Filenames sort chronologically because they're ISO dates, so `sort -r` is
# newest-first without touching mtimes. `find ... -print` (not `ls`) so a
# zero-match case exits 0 rather than tripping pipefail.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'assetmgt-*.dump' -print \
  | sort -r \
  | tail -n +$((KEEP_LOCAL + 1)) \
  | while IFS= read -r old; do rm -f "$old"; done
