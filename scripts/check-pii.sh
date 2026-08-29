#!/usr/bin/env bash
# Scans commits for PII before they reach GitHub: known real values from
# .pii-denylist (case-insensitive, literal, plus a hex-normalised pass so a MAC
# matches regardless of separators/length), across both the file *trees* and the
# commit *messages*; plus generic structural patterns (emails, GPS coordinates,
# non-private IPs, SSN-like numbers) as defense in depth. See AGENTS.md's
# "PII / privacy" section -- this exists because two real leaks already happened
# before it did: real household names/hostname/IP sitting in old commits even
# after later commits scrubbed the *current* files (never rewrote history), and
# real names reused as "illustrative examples" in a later, unrelated commit. The
# hex-normalisation and message passes each close a specific later miss: a real
# Sonos MAC that the literal denylist entry didn't match, and real names that
# only ever lived in commit messages (which the tree-only rules never saw).
#
# Deliberately no `set -e`, same reasoning as preflight.sh: report every
# problem in one pass, don't stop at the first. Exits 1 on any FAIL.
#
# Usage:
#   scripts/check-pii.sh                  # commits about to be pushed
#                                          # (upstream..HEAD, or origin/main..HEAD)
#   scripts/check-pii.sh --range A..B     # a specific range (the pre-push
#                                          # hook passes what git gives it)
#   scripts/check-pii.sh --full           # every commit reachable from any
#                                          # ref -- the whole history, not
#                                          # just what's about to move
#
# What this can't catch: a *new* real name used for the first time as an
# example. It isn't in the denylist yet (nothing is, until someone notices
# and adds it), and a name is syntactically indistinguishable from any other
# word, so no pattern can flag it. The denylist and the patterns below are a
# backstop for *known* values and *structural* PII, not a substitute for
# never inventing illustrative examples from real household details in the
# first place -- see AGENTS.md.

# Every `git grep` below passes --no-color explicitly, regardless of the
# caller's gitconfig -- `color.ui=always` (as opposed to the default `auto`)
# makes git emit real ANSI escape codes even when output is piped/captured
# into a variable, not just on a terminal. Without --no-color, those escape
# bytes land inside the captured string and silently break exact-match logic
# further down (e.g. extracting "the text after the last colon" to compare
# an IP against a private-range list) -- confirmed by hand while writing
# this script: 127.0.0.1 wasn't being excluded until this was added.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

FAILS=0

ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }

MODE="range"
RANGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --full) MODE="full"; shift ;;
    --range)
      # Without this check, --range as the LAST argument makes `shift 2`
      # silently fail (only one argument left to shift) and return non-zero
      # -- with no `set -e` here, $# never reaches 0 and the while loop
      # above spins forever burning a CPU core, with no output and no hint
      # what happened.
      [ $# -ge 2 ] || { echo "--range requires an argument" >&2; exit 2; }
      RANGE="$2"
      shift 2
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$MODE" = "range" ] && [ -z "$RANGE" ]; then
  UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  if [ -n "$UPSTREAM" ]; then
    RANGE="$UPSTREAM..HEAD"
  elif git rev-parse --verify origin/main >/dev/null 2>&1; then
    RANGE="origin/main..HEAD"
  else
    warn "no upstream branch and no origin/main found -- checking full history instead"
    MODE="full"
  fi
fi

# .pii-denylist is gitignored and dev-machine-only -- the one sanctioned
# place these real values live in plaintext, same logic as .env for
# secrets. Its absence is a WARN, not a FAIL: a fresh clone hasn't
# populated it yet, and the generic pattern checks below still run.
DENYLIST_FILE="$REPO_DIR/.pii-denylist"
DENYLIST_TERMS=()
if [ -f "$DENYLIST_FILE" ]; then
  # `|| [ -n "$line" ]`: plain `read` returns non-zero on EOF without a
  # trailing newline, which would otherwise silently drop the file's last
  # line -- and the most recently added term (added *because* it just
  # leaked) is exactly the one most likely to be on a newline-less last
  # line in a hand-edited file.
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    DENYLIST_TERMS+=("$line")
  done < "$DENYLIST_FILE"
  if [ "${#DENYLIST_TERMS[@]}" -eq 0 ]; then
    warn ".pii-denylist exists but has no terms in it"
  fi
else
  warn ".pii-denylist not found -- skipping known-value checks; generic pattern checks still run. See AGENTS.md's \"PII / privacy\" section to populate it."
fi

if [ "$MODE" = "full" ]; then
  COMMITS="$(git rev-list --all 2>&1)"
  REV_LIST_STATUS=$?
  LABEL="full history"
else
  COMMITS="$(git rev-list "$RANGE" 2>&1)"
  REV_LIST_STATUS=$?
  LABEL="range $RANGE"
fi

echo "== PII check: $LABEL =="

# An unresolvable range (git rev-list exits non-zero, e.g. a local sha the
# remote advertised but this checkout hasn't fetched) is NOT the same thing
# as a genuinely empty range -- treating them the same used to mean a push
# whose range couldn't be resolved sailed through with zero PII/secret
# scanning while printing a green "ok ... nothing to check" line. Fail loud
# instead: this is exactly the "reported clean while carrying real values"
# shape this script exists to prevent, just one level up.
if [ "$REV_LIST_STATUS" -ne 0 ]; then
  fail "$LABEL: could not resolve commit range ($COMMITS)"
  echo
  echo "== Summary =="
  echo "  $FAILS FAIL(s)"
  exit 1
fi

if [ -z "$COMMITS" ]; then
  ok "$LABEL: nothing to check (no commits in range)"
  echo
  echo "== Summary =="
  echo "  $FAILS FAIL(s)"
  exit 0
fi

HIT=0

# Known-value denylist terms -- exact real strings, always a FAIL.
if [ "${#DENYLIST_TERMS[@]}" -gt 0 ]; then
  for term in "${DENYLIST_TERMS[@]}"; do
    matches="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -i -F -l -e "$term" {} -- 2>/dev/null)"
    if [ -n "$matches" ]; then
      HIT=1
      while IFS= read -r m; do
        fail "denylist term '$term' found in $m"
      done <<< "$matches"
    fi
  done
fi

# Generic structural patterns -- defense in depth for things not yet known
# to the denylist. Email addresses exclude this repo's own intentionally
# public pseudonym/service addresses.
EMAIL_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' {} -- 2>/dev/null \
  | grep -v -E 'noreply@anthropic\.com|users\.noreply\.github\.com|@anthropic\.com|console\.anthropic\.com|example\.com')"
if [ -n "$EMAIL_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "possible email address in $m"
  done <<< "$EMAIL_HITS"
fi

GPS_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '[0-9]{1,3}\.[0-9]{4,},[[:space:]]*-?[0-9]{1,3}\.[0-9]{4,}' {} -- 2>/dev/null)"
if [ -n "$GPS_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "possible GPS coordinate pair in $m"
  done <<< "$GPS_HITS"
fi

# No \b here -- git grep -E is POSIX ERE, which doesn't support \b (it
# silently matches nothing, rather than erroring, so this is easy to get
# wrong without testing). The pattern is specific enough without it.
SSN_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '[0-9]{3}-[0-9]{2}-[0-9]{4}' {} -- 2>/dev/null)"
if [ -n "$SSN_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "SSN-like number in $m"
  done <<< "$SSN_HITS"
fi

# Secrets -- API keys, tokens, private keys. This is a different threat from
# the PII above: not "a real household detail leaked", but "a live
# credential leaked", so the whole class is a FAIL. Two rules cover it:
# (a) here, vendor-prefixed formats that are unmistakable regardless of which
# file they land in; (b) below, this deployment's *own* secret values read
# from .env, for the keys whose format has no recognisable prefix.
#
# Unlike every rule above, these DELIBERATELY DO NOT ECHO THE MATCH. A leaked
# credential printed into terminal scrollback or a CI log is a second copy of
# the thing we're trying to contain, so `git grep -l` lists only the
# commit:path -- never the secret itself. (The PII rules echo their match on
# purpose: the matched text is the thing that shouldn't be there, and seeing
# it is how you find it. For a secret the location is enough to act on.)
#
# Patterns validated against this repo's full history: zero matches on all
# tracked content, and confirmed to match the real Anthropic / OpenRouter /
# AWS keys this app uses. `-e` guards the leading dash of the PRIVATE KEY
# alternative from being read as an option.
SECRET_RE='sk-ant-[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
SECRET_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -lE -e "$SECRET_RE" {} -- 2>/dev/null)"
if [ -n "$SECRET_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "credential matching a known key format in $m -- value withheld; rotate it and purge from history"
  done <<< "$SECRET_HITS"
fi

# (b) This deployment's own secrets, for keys whose format (a) can't fingerprint
# -- APP_ADMIN_PASSWORD, UNIFI_API_KEY, NVD_API_KEY, BACKUP_AWS_SECRET_ACCESS_KEY
# and the like. Same mechanism as the .pii-denylist loop above: read the real
# values (from the gitignored .env, the one place they legitimately sit in
# plaintext -- never scanned itself, only used as a source of needles) and
# literal-grep the commit trees for them.
#
# Guards against crying wolf: (1) only keys whose NAME looks secret-ish are
# used -- DATABASE_URL embeds the password "assetmgt", which appears in nearly
# every file in the repo, and it must not turn every commit red; (2) the known
# placeholders "change-me"/"assetmgt" and any value under 12 chars are skipped,
# since a short or default value isn't a real leaked secret and would match far
# too broadly. A rule that fires on non-secrets teaches people to reach for
# `git push --no-verify`, which is exactly how a real leak then slips out.
# The FAIL names the key and file, never the value.
ENV_FILE="$REPO_DIR/.env"
# Parallel arrays (bash 3.2 on macOS has no associative arrays -- see the
# pre-push hook's own note on this) of every qualifying .env (key, value)
# pair, populated below alongside the tree scan and reused by the commit-
# MESSAGE pass further down -- without this, a secret pasted into a commit
# message rather than a file was invisible to rule (b) entirely, since only
# the tree scan ever compared against these values.
ENV_SECRET_KEYS=()
ENV_SECRET_VALS=()
if [ -f "$ENV_FILE" ]; then
  # Same "don't drop a newline-less last line" fix as the denylist read
  # above -- the newest key in a hand-edited .env is exactly the one most
  # likely to be on that last line.
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    val="${val%$'\r'}"                   # tolerate a .env last-edited on another box
    # Match python-dotenv's own parsing (app/db.py's load_dotenv() is what
    # actually resolves these values at runtime): single quotes are just as
    # valid as double, and an unquoted value truncates at ` #` (an inline
    # comment) -- without this, a single-quoted value or a value with a
    # trailing comment builds a needle that can never match the plain
    # secret as it's actually committed, and the scan reports "clean"
    # having never really compared anything.
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
      *) val="${val%%[[:space:]]#*}" ;;
    esac
    # Only keys whose NAME marks the value as sensitive. Two families:
    #  - secrets (no recognisable prefix, so rule (a) can't see them);
    #  - structured PII that the IP rule structurally cannot catch --
    #    SCAN_SUBNETS / *BASE_URL hold the real home subnet and gateway,
    #    which are private-range and so excluded from the IP check above.
    # DEFAULT_OWNER / SECONDARY_OWNER_NAME are DELIBERATELY not here: a
    # household first name is short and literal-grepping it across the repo
    # reproduces the "chase" verb-vs-name collision that got the old
    # machine-wide guardrail removed (see AGENTS.md). Names go in
    # .pii-denylist, where a human vets them, not into an automatic scan.
    case "$key" in
      *API_KEY*|*APIKEY*|*PASSWORD*|*SECRET*|*TOKEN*|*ACCESS_KEY*|*SUBNET*|*BASE_URL*) ;;
      *) continue ;;
    esac
    [ -n "$val" ] || continue
    # A value that already appears in the tracked template is one we publish
    # on purpose -- change-me, admin, 192.168.1.0/24, the assetmgt dev
    # password inside DATABASE_URL -- so by definition not a leak. This
    # replaces a hand-kept placeholder list that would rot.
    grep -qF -- "$val" "$REPO_DIR/.env.example" 2>/dev/null && continue
    # Below the floor a value is too short/word-like to literal-scan without
    # repo-wide false positives. Skipping it silently would be indistinguish-
    # able from "checked and clean", so say so, naming the key not the value.
    if [ "${#val}" -lt 12 ]; then
      warn "$key's value in .env is under 12 chars -- too short to scan safely, so NOT checked here"
      continue
    fi
    # Case-sensitive (no -i, unlike the denylist name match): a case-variant
    # of a credential is not that credential, and -i is what turns a word-ish
    # password into a repo-wide false FAIL.
    matches="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -F -l -e "$val" {} -- 2>/dev/null)"
    if [ -n "$matches" ]; then
      HIT=1
      while IFS= read -r m; do
        fail "the value of $key (from .env) appears in $m -- value withheld; rotate it and purge from history"
      done <<< "$matches"
    fi
    ENV_SECRET_KEYS+=("$key")
    ENV_SECRET_VALS+=("$val")
  done < "$ENV_FILE"
fi

# (c) .env committed at all -- the .gitignore is the only thing keeping every
# secret above out of git, and `git add -f .env` silently defeats it. Flag any
# tracked secret-config file in the range. .env.example is the tracked template
# and must stay allowed, so match the bare/base names only, not the .example
# suffix.
ENV_TRACKED="$(echo "$COMMITS" | xargs -I{} git --no-pager ls-tree -r --name-only {} 2>/dev/null \
  | grep -E '(^|/)\.env$|(^|/)\.env\.(local|production|prod)$' | sort -u)"
if [ -n "$ENV_TRACKED" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "a secrets file is tracked in git: $m -- it must stay gitignored (git rm --cached, then purge from history)"
  done <<< "$ENV_TRACKED"
fi

# (d) There is deliberately NO generic "(API_KEY|SECRET|TOKEN|PASSWORD)=value"
# keyword rule. Tested against the current tree it matches ten legitimate,
# must-keep lines -- .env.example's own `change-me`, README's documented
# `export AWS_...=$(grep ... .env)` recipes, docker-compose's local
# `POSTGRES_PASSWORD: assetmgt`, backup-db.sh's `${AWS_...:?msg}` guards -- so
# as a FAIL it would block every push and as a WARN it would print ten lines to
# scroll past on every push. Either way it trains people to ignore this script,
# per the same reasoning as the IP_ALLOWLIST note below. Rules (a)-(c) catch
# the real thing (a value that is actually a live credential) without the noise.
# If you're tempted to add it back, add a targeted (a)-style prefix instead.

# Dotted-quad strings that are confirmed NOT IP addresses, so the warning
# below doesn't re-fire on them at every push. Format: <path>|<literal>, one
# per line -- deliberately scoped to the file the string appears in, so the
# same digits showing up anywhere else still warn.
#
# The bar for adding an entry is "confirmed not an IP", not "probably fine"
# (see AGENTS.md's "PII / privacy" section). This lives in the script, not
# the gitignored .pii-denylist, because -- unlike the denylist -- nothing
# here is secret, and a fresh clone should inherit the same confirmations
# rather than re-flagging them for someone else to re-investigate.
#
# Suppressing a confirmed false positive is the point: a WARN that fires
# forever is one people learn to scroll past, which is how a real leak gets
# missed later.
# Note the second entry: writing a literal here puts that dotted quad into
# this file too, so the allowlist has to cover its own source or the check
# reports itself. That's honest rather than circular -- the entry is still
# file-scoped, so these digits anywhere else are still flagged. The third
# entry is the same Sonos version string again, quoted as a Python constant
# in test_check_pii.py's own fixture data (see that file's ALLOWLISTED_VALUE)
# -- same value, same reasoning, just a third file it happens to appear in.
#
# The next two entries are a different case: genuine IP addresses (not
# lookalikes like 1.9.1.10 above), but standard, reserved, or well-known
# public ones used deliberately as test fixtures -- 8.8.8.8 is Google Public
# DNS (test_sonos_api.py's SSRF-guard test uses it as an obvious "reject this
# public address" case); 203.0.113.7 is inside 203.0.113.0/24, the IANA
# TEST-NET-3 block reserved by RFC 5737 specifically for documentation/
# examples and never assignable to a real host (test_check_pii.py's own
# fixture data, confirming the "genuine public address" WARN path still
# fires). Neither can be a real home IP.
IP_ALLOWLIST='tests/test_sonos_api.py|1.9.1.10
scripts/check-pii.sh|1.9.1.10
tests/test_check_pii.py|1.9.1.10
tests/test_sonos_api.py|8.8.8.8
tests/test_check_pii.py|203.0.113.7
scripts/check-pii.sh|8.8.8.8
scripts/check-pii.sh|203.0.113.7
scripts/check-pii.sh|203.0.113.0'

# Non-private IPv4 addresses -- WARN not FAIL, since a legitimate public
# endpoint (an API host, a documentation example) can trigger this
# harmlessly. Excludes RFC1918 private ranges, loopback, link-local, and
# multicast (this app's own SSDP code has a legitimate multicast address),
# plus IP_ALLOWLIST above.
# Checks the actual matched IP (the text after the last ':' in git grep's
# tree:file:line:match output), not the whole line -- a naive "exclude
# lines containing :192.168." check misses matches with other text (a URL
# scheme, a key=value prefix) between the field separator and the IP.
IP_RAW="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '([0-9]{1,3}\.){3}[0-9]{1,3}' {} -- 2>/dev/null)"
IP_HITS=""
if [ -n "$IP_RAW" ]; then
  while IFS= read -r line; do
    ip="${line##*:}"
    case "$ip" in
      10.*|172.16.*|172.17.*|172.18.*|172.19.*|172.2[0-9].*|172.30.*|172.31.*|192.168.*|127.*|169.254.*|0.0.0.0|22[4-9].*|23[0-9].*)
        continue
        ;;
    esac
    # File-scoped allowlist. Match the path between git grep's commit: and
    # :line: separators so a bare value can't be waved through elsewhere.
    allowed=""
    while IFS= read -r entry; do
      [ -n "$entry" ] || continue
      case "$line" in
        *":${entry%%|*}:"*) [ "$ip" = "${entry##*|}" ] && allowed=1 ;;
      esac
    done <<< "$IP_ALLOWLIST"
    [ -n "$allowed" ] && continue
    IP_HITS="${IP_HITS}${IP_HITS:+$'\n'}${line}"
  done <<< "$IP_RAW"
fi
if [ -n "$IP_HITS" ]; then
  while IFS= read -r m; do
    warn "non-private-looking IP address in $m -- confirm it's a legitimate public endpoint, not a real home IP"
  done <<< "$IP_HITS"
fi

# Hex/MAC normalisation -- the literal denylist loop at the top is punctuation-
# and length-sensitive: a denylist entry like "AABBCCDDEEFF0" never matched the
# *same* MAC as it appears in source, e.g. "RINCON_AABBCCDDEEFF01400" (no
# separators, extra suffix digits from the RINCON id -- fabricated example here,
# deliberately not the real value). That exact miss shipped a real device id to
# GitHub and is the reason this rule exists. Fix by comparing both sides with
# all of [:.-] stripped and lower-cased: reduce each denylist entry to its hex
# and, if >= 12 hex chars remain, take the FIRST 12 (the MAC itself, dropping any
# RINCON/serial suffix) as a needle; then pull every MAC-shaped token out of the
# commit trees, normalise it the same way, and FAIL on a substring hit. One grep
# pass over the trees, same cost shape as the IP rule below. Entries that reduce
# to < 12 hex chars (names, addresses, IPs) never become needles, so this can't
# fire on non-MAC denylist lines. Reports the location only, never the value.
HEX_NEEDLES=()
if [ "${#DENYLIST_TERMS[@]}" -gt 0 ]; then
  for term in "${DENYLIST_TERMS[@]}"; do
    hex="$(printf '%s' "$term" | tr -cd '0-9A-Fa-f')"
    [ "${#hex}" -ge 12 ] || continue
    HEX_NEEDLES+=("$(printf '%s' "${hex:0:12}" | tr 'A-F' 'a-f')")
  done
fi
if [ "${#HEX_NEEDLES[@]}" -gt 0 ]; then
  MAC_RAW="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -oE '[0-9A-Fa-f]{2}([:.-]?[0-9A-Fa-f]{2}){5,}' {} -- 2>/dev/null)"
  if [ -n "$MAC_RAW" ]; then
    HEX_REPORTED=""
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      # git grep -oE (no -n) prints tree:file:match. File paths are colon-free in
      # this repo, so peel off the first two colon-delimited fields; the rest is
      # the MAC, whose own colons must stay intact until normalisation.
      loc_rest="${line#*:}"
      loc="${line%%:*}:${loc_rest%%:*}"     # tree:file
      match="${loc_rest#*:}"
      norm="$(printf '%s' "$match" | tr -cd '0-9A-Fa-f' | tr 'A-F' 'a-f')"
      for needle in "${HEX_NEEDLES[@]}"; do
        case "$norm" in
          *"$needle"*)
            # De-dup: one FAIL per (location, needle), not one per occurrence.
            case "$HEX_REPORTED" in *"|$loc@$needle|"*) : ;; *)
              HIT=1
              fail "denylisted MAC/hex value (normalised match) found in $loc -- value withheld"
              HEX_REPORTED="${HEX_REPORTED}|$loc@$needle|"
            ;; esac
            break
            ;;
        esac
      done
    done <<< "$MAC_RAW"
  fi
fi

# Commit MESSAGES -- every rule above greps commit *trees* (file content), so a
# real name or a secret written into a commit *message* slipped through
# untouched, and `--full` then reported "clean" while several messages carried
# real household names. Scan each in-scope commit's message body for the same
# known-value denylist and the structural secret/SSN/GPS patterns. Deliberately
# NOT the email rule and NOT author name/email (%an/%ae): those are git identity
# metadata present on every commit by design, so flagging them would fire
# forever -- the cry-wolf failure the notes above warn against. Names go through
# the human-curated denylist. Secret matches withhold the value, as elsewhere.
while IFS= read -r sha; do
  [ -n "$sha" ] || continue
  body="$(git --no-pager log -1 --format='%B' "$sha" 2>/dev/null)"
  [ -n "$body" ] || continue
  if [ "${#DENYLIST_TERMS[@]}" -gt 0 ]; then
    for term in "${DENYLIST_TERMS[@]}"; do
      if printf '%s' "$body" | grep -qiF -- "$term"; then
        HIT=1
        fail "denylist term '$term' in commit message of $sha"
      fi
    done
  fi
  # Same rule (b) .env values the tree scan above already checks against
  # file content -- a real secret pasted into a commit MESSAGE rather than
  # a file was invisible here until now. Value withheld, as elsewhere.
  for i in "${!ENV_SECRET_KEYS[@]}"; do
    if printf '%s' "$body" | grep -qF -- "${ENV_SECRET_VALS[$i]}"; then
      HIT=1
      fail "the value of ${ENV_SECRET_KEYS[$i]} (from .env) appears in the commit message of $sha -- value withheld; rotate it and rewrite the message"
    fi
  done
  if printf '%s' "$body" | grep -qE '[0-9]{3}-[0-9]{2}-[0-9]{4}'; then
    HIT=1
    fail "SSN-like number in commit message of $sha"
  fi
  if printf '%s' "$body" | grep -qE '[0-9]{1,3}\.[0-9]{4,},[[:space:]]*-?[0-9]{1,3}\.[0-9]{4,}'; then
    HIT=1
    fail "possible GPS coordinate pair in commit message of $sha"
  fi
  if printf '%s' "$body" | grep -qE -e "$SECRET_RE"; then
    HIT=1
    fail "credential matching a known key format in commit message of $sha -- value withheld; rotate it and rewrite the message"
  fi
done <<< "$COMMITS"

if [ "$HIT" -eq 0 ]; then
  ok "$LABEL: clean"
fi

echo
echo "== Summary =="
echo "  $FAILS FAIL(s)"
if [ "$FAILS" -gt 0 ]; then
  exit 1
fi
exit 0
