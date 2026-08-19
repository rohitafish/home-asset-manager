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
#
# Deliberately always exits 0, even when the key isn't found: under
# set -euo pipefail, `grep -m1 ... | cut ...` exits non-zero when grep finds
# no match, and pipefail propagates that through the pipeline. A plain
# `VAR="$(_env_var KEY)"` assignment (unlike `export VAR="$(...)"`, whose
# own exit status masks the substitution's) then trips `set -e` and exits
# the whole script right there -- BEFORE the `:?` guards below that exist
# specifically to name the missing key, and before the `trap ... ERR` that
# writes to logs/backup.error.log. A missing BACKUP_S3_BUCKET used to kill
# the job silently with a zero-byte error log that reads as a clean run.
# Let the `:?` guards be the only thing that catches a missing value.
_env_var() {
  [ -f "$REPO_DIR/.env" ] || return 0
  grep -m1 "^${1}=" "$REPO_DIR/.env" | cut -d= -f2- || true
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

# Runs only once the upload retries below are exhausted, i.e. a sustained
# outage rather than the transient reset those already absorb -- so these
# probes are here to characterise "still broken minutes later", and they cost
# a few seconds on a path that is already failing.
#
# What each one distinguishes:
#   dump        Body size. The Aug 2026 outage began on the tick where this
#               jumped 2.6M -> 7.4M while small calls in the same windows
#               (head-object, s3 ls) kept working, so size belongs in any
#               report of an upload failure.
#   s3-bucket   DNS + TCP + TLS to the exact endpoint that failed. An
#               unauthenticated GET is expected to return 403/307 -- that is
#               a *success* here, it proves the path is reachable. A curl
#               exit code instead means the network, not S3, is the problem.
#   internet    Separates "S3/AWS is unreachable" from "this host has no
#               working egress at all".
# All are `|| true`: a probe failing is itself the finding, and must never
# replace the original error. Note the ERR trap is deliberately not inherited
# here -- `set -E` is absent -- so a failing probe cannot re-enter the trap.
_diagnose() {
  local endpoint="$BUCKET.s3.$AWS_DEFAULT_REGION.amazonaws.com"
  echo "  diag dump:      $(ls -lh "$DEST" 2>&1 | awk '{print $5}' || echo '(absent)')" >&2
  echo "  diag s3-bucket: $(curl -sS -o /dev/null -m 20 \
    -w 'HTTP %{http_code} dns=%{time_namelookup}s connect=%{time_connect}s total=%{time_total}s' \
    "https://$endpoint/" 2>&1 | tail -1)" >&2
  echo "  diag internet:  $(curl -sS -o /dev/null -m 15 \
    -w 'HTTP %{http_code} total=%{time_total}s' \
    https://checkip.amazonaws.com 2>&1 | tail -1)" >&2
}

# Every production failure of this script so far has been the same one: the
# upload dying mid-body with "Connection was closed before we received a valid
# response from endpoint URL". Three consecutive ticks failed that way over 15
# hours (Aug 2026) and then the very same upload succeeded by hand in 1.4s --
# the definition of a transient worth retrying rather than sleeping through
# until the next tick eight hours later.
#
# The head-object before each retry is not belt-and-braces, it is required for
# correctness. "Connection closed before the response" means exactly that the
# outcome is unknown: S3 may well have committed the object and lost only the
# reply. These keys are under COMPLIANCE Object Lock, so a blind same-key retry
# would then be *rejected* -- turning a succeeded upload into a hard failure.
# Checking first turns that case into the success it actually was.
_put_object_with_retries() {
  local key="$1" retain_until="$2"
  local attempt=1 delay=30
  local max="${BACKUP_UPLOAD_ATTEMPTS:-3}"
  while : ; do
    if aws s3api put-object \
        --bucket "$BUCKET" --key "$key" --body "$DEST" \
        --server-side-encryption AES256 \
        --object-lock-mode COMPLIANCE --object-lock-retain-until-date "$retain_until" \
        > /dev/null
    then
      if [ "$attempt" -gt 1 ]; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup-db.sh: $key uploaded on attempt $attempt" >&2
      fi
      return 0
    fi
    # aws has already written its own error to stderr, above this line.
    if aws s3api head-object --bucket "$BUCKET" --key "$key" >/dev/null 2>&1; then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup-db.sh: $key landed despite the error on attempt $attempt (lost response, not lost upload)" >&2
      return 0
    fi
    if [ "$attempt" -ge "$max" ]; then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup-db.sh: $key failed all $max upload attempts" >&2
      return 1
    fi
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup-db.sh: $key upload attempt $attempt/$max failed, retrying in ${delay}s" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 4))
  done
}

# Fail loudly: set -e alone exits silently on e.g. Docker not running,
# leaving nothing in logs/backup.error.log to act on.
trap 'echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup-db.sh FAILED at line $LINENO" >&2; _diagnose || true' ERR
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
  # Confirming today's off-site copy exists counts as success, so refresh the
  # marker here too. /health reads it to answer "is there a current off-site
  # backup?", not "did this process upload one" -- and only the uploading run
  # reached the write at the end of this script. So any day whose copy got
  # there by another route (a manual re-upload after a failed tick, a
  # restore) left the marker frozen and /health reporting a backup gap that
  # didn't exist, while the object sat safely in S3 the whole time.
  date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP_DIR/last-success"
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

_put_object_with_retries "daily/$NAME" "$RETAIN_UNTIL_DAILY"

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
  _put_object_with_retries "monthly/$NAME" "$RETAIN_UNTIL_MONTHLY"
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
