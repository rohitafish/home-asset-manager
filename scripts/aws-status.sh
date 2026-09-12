#!/usr/bin/env bash
# One-command view of the AWS-side state this deployment depends on: the
# off-site backups, the TLS certificate and its renewal path, and the DNS
# records that make the dashboard reachable at all.
#
# Run this from the DEV MAC, not the deploy host. It needs both sides: the
# host's .env holds the S3 credentials, while Route 53 and IAM reads come
# from this machine's `default` profile. The host deliberately holds no AWS
# credential of its own (see AGENTS.md's "Database backups"), so it cannot
# answer the DNS/IAM half by itself. The host may be the Linux laptop
# (systemd, apt Caddy) or the Mac mini (launchd, brew Caddy); the remote
# side below answers the same questions on either.
#
# Deliberately no `set -e`, same as scripts/preflight.sh: the job is to
# report every problem in one pass, not stop at the first. Exits 1 if any
# check FAILs, 0 if only WARNs.
#
# Never prints a secret. Credentials stay on the host and are exported only
# inside the remote shell; what crosses the ssh channel is derived facts --
# an identity ARN, object names, dates. The bucket name is read from .env
# and never echoed, because this repo is public.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

HOST="${DEPLOY_HOST:-mint}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-~/claudecode/assetmgt}"
DOMAIN="${ASSETMGT_DOMAIN:-assets.rohita.com}"
ZONE_ID="${ASSETMGT_ZONE_ID:-Z05906141QTVV2UUOL5D6}"
PROFILE="${AWS_PROFILE:-default}"

FAILS=0
WARNS=0
ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; WARNS=$((WARNS + 1)); }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }

# One ssh round trip, not a dozen. Each connection costs about a second, and
# a status command people won't run is a status command that doesn't help --
# so the remote side emits key=value lines that we parse below.
REMOTE_OUT="$(ssh -o ConnectTimeout=10 "$HOST" bash -s -- "$REMOTE_DIR" "$DOMAIN" <<'REMOTE' 2>/dev/null
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null
export PATH="$HOME/.local/bin:$PATH"   # where pipx puts aws on Linux
DIR="${1/#\~/$HOME}"; DOMAIN="$2"
cd "$DIR" 2>/dev/null || exit 1

# Same grep/cut read backup-db.sh uses, so no other secret in .env is
# exported into this process for no reason.
_env() { grep -m1 "^${1}=" .env 2>/dev/null | cut -d= -f2-; }
export AWS_ACCESS_KEY_ID="$(_env BACKUP_AWS_ACCESS_KEY_ID)"
export AWS_SECRET_ACCESS_KEY="$(_env BACKUP_AWS_SECRET_ACCESS_KEY)"
export AWS_DEFAULT_REGION="$(_env BACKUP_AWS_REGION)"
BUCKET="$(_env BACKUP_S3_BUCKET)"

echo "IDENTITY=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)"

# Prefix daily/assetmgt- deliberately, not bare daily/: anything else parked
# under that prefix (a one-off permission probe, say) is not a backup, and
# sorting the whole prefix by date would quietly report it as the newest one.
# list-objects-v2 rather than `s3 ls` for backup-db.sh's reason: `s3 ls`
# exits 1 both for "empty" and for a denied ListBucket.
echo "DAILY_LATEST=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix daily/assetmgt- \
  --query 'sort_by(Contents,&LastModified)[-1].[Key,LastModified]' --output text 2>/dev/null)"
echo "MONTHLY_THIS=$(aws s3api list-objects-v2 --bucket "$BUCKET" \
  --prefix "monthly/assetmgt-$(date -u +%Y-%m)-" --query 'Contents[0].Key' --output text 2>/dev/null)"
echo "HEALTH=$(curl -s -m 5 http://127.0.0.1:8000/health 2>/dev/null)"

CERTS="$(certbot certificates --config-dir "$HOME/.certbot/config" \
  --work-dir "$HOME/.certbot/work" --logs-dir "$HOME/.certbot/logs" 2>/dev/null)"
# Scan from the matching "Certificate Name:" line, not the whole output --
# certbot lists every certificate it manages, and today's single-cert case
# is the one where a sloppier grep would look correct forever.
_cert_field() { printf '%s\n' "$CERTS" | awk -v d="$DOMAIN" -v pat="$1" \
  '$0 ~ ("Certificate Name: " d) {f=1} f && $0 ~ pat {print; exit}'; }
echo "CERT_EXPIRY=$(_cert_field 'Expiry Date:' | sed -E 's/.*Expiry Date: ([^(]*).*/\1/')"
echo "CERT_DAYS=$(_cert_field 'VALID:' | sed -E 's/.*VALID: ([0-9]+) day.*/\1/')"
echo "CERT_KEYTYPE=$(_cert_field 'Key Type:' | sed -E 's/.*Key Type: *//')"
echo "RENEW_BEFORE=$(grep -m1 renew_before_expiry "$HOME/.certbot/config/renewal/$DOMAIN.conf" 2>/dev/null | sed -E 's/.*= *//')"
echo "CERT_FILE_ENDDATE=$(openssl x509 -in "$HOME/.certbot/config/live/$DOMAIN/fullchain.pem" -noout -enddate 2>/dev/null | cut -d= -f2)"
# The renewal scheduler and Caddy, by whichever service manager this host
# has. RENEW_AGENT is 1/0; CADDY is "started" when running, else the raw
# state the manager reported (so the FAIL message can quote it).
if command -v launchctl >/dev/null 2>&1; then
  echo "RENEW_AGENT=$(launchctl list 2>/dev/null | grep -c com.assetmgt.certrenew)"
  echo "CADDY=$(brew services list 2>/dev/null | awk '$1=="caddy"{print $2}')"
else
  RENEW_STATE="$(systemctl is-enabled assetmgt-certrenew.timer 2>/dev/null)"
  if [ "$RENEW_STATE" = enabled ]; then echo "RENEW_AGENT=1"; else echo "RENEW_AGENT=0"; fi
  CADDY_STATE="$(systemctl is-active caddy 2>/dev/null)"
  if [ "$CADDY_STATE" = active ]; then CADDY_STATE=started; fi
  echo "CADDY=${CADDY_STATE:-absent}"
fi
REMOTE
)"

_val() { printf '%s\n' "$REMOTE_OUT" | grep -m1 "^$1=" | cut -d= -f2-; }

if [ -z "$REMOTE_OUT" ]; then
  echo "!!! Could not reach '$HOST' over ssh, or $REMOTE_DIR is missing there." >&2
  echo "!!! Everything below needs the deploy host. Fix connectivity and re-run." >&2
  exit 1
fi

echo "== Backup identity =="
IDENTITY="$(_val IDENTITY)"
case "$IDENTITY" in
  *:user/assetmgt-backup) ok "backups authenticate as assetmgt-backup (least privilege)" ;;
  *:user/s3-user)         fail "backups still authenticate as s3-user -- the broadly-scoped identity. See AGENTS.md's \"Database backups\"" ;;
  "")                     fail "could not resolve the backup identity -- credentials in the host's .env may be wrong" ;;
  *)                      warn "backups authenticate as an unexpected identity: $IDENTITY" ;;
esac

# Key age matters: this is the credential sitting in a .env on an always-on
# host, so an old one is the thing worth noticing before it's the thing
# worth regretting.
KEY_CREATED="$(aws --profile "$PROFILE" iam list-access-keys --user-name assetmgt-backup \
  --query 'AccessKeyMetadata[0].CreateDate' --output text 2>/dev/null)"
if [ -n "$KEY_CREATED" ] && [ "$KEY_CREATED" != "None" ]; then
  KEY_AGE=$(( ( $(date +%s) - $(date -j -f "%Y-%m-%dT%H:%M:%S" "${KEY_CREATED%%+*}" +%s 2>/dev/null || echo "$(date +%s)") ) / 86400 ))
  if [ "$KEY_AGE" -gt 365 ]; then
    warn "backup access key is $KEY_AGE days old -- consider rotating"
  else
    ok "backup access key is $KEY_AGE days old"
  fi
else
  warn "could not read the backup key's age (needs iam:ListAccessKeys on profile '$PROFILE')"
fi

echo
echo "== Off-site backups =="
DAILY="$(_val DAILY_LATEST)"
if [ -z "$DAILY" ] || [ "$DAILY" = "None" ]; then
  fail "no objects under daily/ -- either the bucket is empty or ListBucket is denied"
else
  DKEY="$(printf '%s' "$DAILY" | awk '{print $1}')"
  DWHEN="$(printf '%s' "$DAILY" | awk '{print $2}')"
  DAGE=$(( ( $(date +%s) - $(date -j -f "%Y-%m-%dT%H:%M:%S" "${DWHEN%%.*}" +%s 2>/dev/null || echo "$(date +%s)") ) / 3600 ))
  if [ "$DAGE" -gt 48 ]; then
    fail "newest daily backup is ${DAGE}h old ($DKEY)"
  elif [ "$DAGE" -gt 26 ]; then
    warn "newest daily backup is ${DAGE}h old ($DKEY) -- a nightly tick may have been missed"
  else
    ok "newest daily backup is ${DAGE}h old ($DKEY)"
  fi
fi

MONTHLY="$(_val MONTHLY_THIS)"
if [ "$MONTHLY" = "None" ] || [ -z "$MONTHLY" ]; then
  warn "no monthly copy yet for $(date -u +%Y-%m) -- expected early in the month; the first successful daily creates it"
else
  ok "monthly copy exists for $(date -u +%Y-%m) ($(basename "$MONTHLY"))"
fi

HEALTH="$(_val HEALTH)"
case "$HEALTH" in
  *'"backup_stale":false'*) ok "/health reports the off-site backup is current" ;;
  *'"backup_stale":true'*)  fail "/health reports backup_stale -- the last-success marker is behind" ;;
  *)                        warn "could not read /health on $HOST" ;;
esac

echo
echo "== TLS certificate =="
CERT_DAYS="$(_val CERT_DAYS)"
RENEW_BEFORE="$(_val RENEW_BEFORE)"
RENEW_DAYS="$(printf '%s' "$RENEW_BEFORE" | awk '{print $1}')"
if [ -z "$CERT_DAYS" ]; then
  fail "certbot reports no certificate for $DOMAIN"
else
  if [ "$CERT_DAYS" -lt 3 ]; then
    fail "certificate expires in $CERT_DAYS days -- renewal has not run"
  elif [ -n "$RENEW_DAYS" ] && [ "$CERT_DAYS" -lt "$RENEW_DAYS" ]; then
    warn "certificate expires in $CERT_DAYS days, inside the ${RENEW_DAYS}-day renewal window -- the next daily tick should renew it"
  else
    ok "certificate valid for $CERT_DAYS more days (renews with ${RENEW_DAYS:-?} to go, so in $(( CERT_DAYS - ${RENEW_DAYS:-0} )) days)"
  fi
fi
[ "$(_val CERT_KEYTYPE)" = "ECDSA" ] \
  && ok "key type is ECDSA (the endpoint allows EC_prime256v1 only)" \
  || fail "key type is $(_val CERT_KEYTYPE), but the ACM ACME endpoint accepts EC_prime256v1 only -- renewal will be rejected"
[ "$(_val RENEW_AGENT)" -gt 0 ] 2>/dev/null \
  && ok "the certificate renewal scheduler is armed (assetmgt-certrenew.timer / com.assetmgt.certrenew)" \
  || fail "the certificate renewal scheduler is NOT armed on $HOST -- nothing will renew this certificate (install-systemd-units.sh on Linux, launchctl load on a Mac)"
[ "$(_val CADDY)" = "started" ] \
  && ok "caddy service is running" \
  || fail "caddy service is not running (the host reports '$(_val CADDY)')"

# The failure this catches: a renewal that succeeded on disk while the
# --deploy-hook failed to reload Caddy, which then serves the OLD cert from
# memory until something restarts it. Nothing else here would notice.
SERVED="$(echo | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null \
  | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
ONDISK="$(_val CERT_FILE_ENDDATE)"
if [ -z "$SERVED" ]; then
  fail "could not complete a TLS handshake with $DOMAIN"
elif [ "$SERVED" = "$ONDISK" ]; then
  ok "the certificate Caddy serves matches the one on disk"
else
  fail "Caddy is serving a certificate that expires '$SERVED' but the file on disk says '$ONDISK' -- the renewal deploy-hook did not reload Caddy"
fi

echo
echo "== DNS =="
A_VALUE="$(aws --profile "$PROFILE" route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --query "ResourceRecordSets[?Name=='$DOMAIN.'&&Type=='A'].ResourceRecords[0].Value" --output text 2>/dev/null)"
[ -n "$A_VALUE" ] && [ "$A_VALUE" != "None" ] \
  && ok "Route 53 has an A record for $DOMAIN" \
  || fail "no A record for $DOMAIN in zone $ZONE_ID"

CAA="$(aws --profile "$PROFILE" route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --query "ResourceRecordSets[?Type=='CAA'].ResourceRecords[].Value" --output text 2>/dev/null)"
case "$CAA" in
  *amazon*) ok "CAA pins issuance to Amazon ($(printf '%s' "$CAA" | tr '\t' ' ' | grep -o 'issue "[^"]*"' | wc -l | tr -d ' ') value(s))" ;;
  "")       warn "no CAA record on the zone -- issuance is unrestricted (works, but anyone who can prove control could use any CA)" ;;
  *)        fail "a CAA record exists but names no Amazon CA -- ACM issuance will fail with CAA_ERROR at the next renewal" ;;
esac

# Both sides must be non-empty before comparing. Two empty strings compare
# equal, so a missing A record paired with a name that resolves nowhere would
# otherwise report a confident "ok" for a domain that does not exist.
# Cloudflare's resolver by name, not by address. A literal public quad here
# would trip check-pii's "is this a real home IP?" warning on every push
# forever, and its allowlist is deliberately only for strings confirmed NOT
# to be addresses -- which this one is. A WARN nobody can clear is a WARN
# people learn to scroll past.
PUBLIC_IP="$(dig +short +time=3 +tries=1 @one.one.one.one "$DOMAIN" 2>/dev/null | tail -1)"
if [ -z "$PUBLIC_IP" ]; then
  fail "public DNS returns nothing for $DOMAIN"
elif [ -z "$A_VALUE" ] || [ "$A_VALUE" = "None" ]; then
  warn "public DNS returns '$PUBLIC_IP' but Route 53 has no A record to compare it against"
elif [ "$PUBLIC_IP" = "$A_VALUE" ]; then
  ok "public DNS resolves $DOMAIN to the Route 53 value"
else
  warn "public DNS returned '$PUBLIC_IP' but Route 53 holds a different value -- propagation, or a stale cache"
fi

# The LAN's own answer. A TTL of 0 means the gateway is answering from its
# local DNS record rather than forwarding upstream -- that record is what
# keeps the dashboard reachable during an internet outage, since the app
# binds loopback and is only reachable by name. See AGENTS.md.
LOCAL_ANS="$(dig +noall +answer +time=3 +tries=1 "$DOMAIN" 2>/dev/null | head -1)"
LOCAL_IP="$(printf '%s' "$LOCAL_ANS" | awk '{print $5}')"
LOCAL_TTL="$(printf '%s' "$LOCAL_ANS" | awk '{print $2}')"
if [ -z "$LOCAL_IP" ]; then
  fail "this machine cannot resolve $DOMAIN at all"
elif [ "$LOCAL_TTL" = "0" ]; then
  ok "resolved locally (TTL 0) -- the gateway's local DNS record is answering"
else
  warn "resolved via upstream DNS (TTL $LOCAL_TTL), not the gateway's local record -- LAN access now depends on the internet being up"
fi

echo
echo "== Summary =="
printf '  %d FAIL(s), %d WARN(s)\n' "$FAILS" "$WARNS"
[ "$FAILS" -eq 0 ]
