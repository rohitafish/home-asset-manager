#!/usr/bin/env bash
# Replicate .env *structure* -- key names, their order, and .env.example's own
# comment blocks -- across this Mac, the deploy host, and the tracked template,
# WITHOUT ever moving a value.
#
# The hard invariant, because .env holds PII as well as secrets (SCAN_SUBNETS is
# the real home subnet, DEFAULT_OWNER/SECONDARY_OWNER_NAME are household names,
# and the *_KEY/*_SECRET/PASSWORD keys are live credentials):
#
#   A value from a real .env is never read into the template, never sent over
#   ssh, and never printed. Comments flow OUT of .env.example only, never in.
#
# This is the supported way to reconcile the Mini's .env with the template --
# redeploy.sh deliberately excludes .env from its rsync, so the Mini's .env is
# an island and drift there is otherwise invisible (see README's ".env").
#
# Usage:
#   scripts/env-structure.sh                 # three-way report (remote = $DEPLOY_HOST, else "mini")
#   scripts/env-structure.sh --host HOST     # report against a specific host
#   scripts/env-structure.sh --no-remote     # local .env vs .env.example only
#   scripts/env-structure.sh --strict        # report mode, exit 1 on any key-set drift
#   scripts/env-structure.sh --write-example [--from local|remote] [--dry-run] [--prune]
#
# Exit codes:  0 ok  |  1 a FAIL (drift under --strict, or a refused write, or
# no .env for --write-example)  |  2 usage error  |  3 remote unreachable.
#
# Deliberately no `set -e` -- same one-pass reasoning as check-pii.sh/preflight.sh.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

EXAMPLE="$REPO_DIR/.env.example"
ENV_FILE="$REPO_DIR/.env"

ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; }

HOST="${DEPLOY_HOST:-mini}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-~/claudecode/assetmgt}"
MODE="report"
USE_REMOTE=1
STRICT=0
DRY_RUN=0
PRUNE=0
FROM="local"

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --no-remote) USE_REMOTE=0; shift ;;
    --strict) STRICT=1; shift ;;
    --write-example) MODE="write"; shift ;;
    --from) FROM="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --prune) PRUNE=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

# --- key extraction --------------------------------------------------------
# `^[A-Za-z_][A-Za-z0-9_]*=` NOT `^[A-Z_]+=`: the latter drops digit-bearing
# names like BACKUP_S3_BUCKET, which is the bug this file also fixes in
# preflight.sh. Values are never captured -- only the name before the first `=`.
keys_of() { grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$1" 2>/dev/null | tr -d '=' | sort -u; }

# Remote: fetch key NAMES only, over ssh, then RE-VALIDATE locally. The remote
# grep is a convenience, not a guarantee -- a mistyped REMOTE_DIR, an ssh banner,
# or a wrong-shaped .env could otherwise stream file content to this terminal.
# The anchored `=$` filter here is the actual guarantee that no value returns.
remote_keys() {
  ssh -o BatchMode=yes -n "$HOST" "cd $REMOTE_DIR && grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' .env" 2>/dev/null \
    | grep -E '^[A-Za-z_][A-Za-z0-9_]*=$' | tr -d '=' | sort -u
}

# --- report mode -----------------------------------------------------------
report() {
  echo "== .env structure =="
  local local_keys example_keys rkeys union
  example_keys="$(keys_of "$EXAMPLE")"
  if [ -f "$ENV_FILE" ]; then local_keys="$(keys_of "$ENV_FILE")"; else
    warn "local .env not found -- showing template only for the 'local' column"
    local_keys=""
  fi

  rkeys=""
  local remote_col=1 remote_reachable=1
  if [ "$USE_REMOTE" -eq 1 ]; then
    if ! ssh -o BatchMode=yes -n "$HOST" true 2>/dev/null; then
      warn "deploy host '$HOST' unreachable over ssh -- reporting local vs template only"
      remote_reachable=0
    else
      rkeys="$(remote_keys)"
    fi
  else
    remote_col=0
  fi

  union="$(printf '%s\n%s\n%s\n' "$local_keys" "$rkeys" "$example_keys" | grep -v '^$' | sort -u)"

  if [ "$remote_col" -eq 1 ]; then
    printf '  %-34s %-7s %-7s %-7s\n' KEY local "$HOST" example
  else
    printf '  %-34s %-7s %-7s\n' KEY local example
  fi
  local k lc rc ec
  while IFS= read -r k; do
    [ -n "$k" ] || continue
    printf '%s\n' "$local_keys"   | grep -qx "$k" && lc="y" || lc="-"
    printf '%s\n' "$example_keys" | grep -qx "$k" && ec="y" || ec="-"
    if [ "$remote_col" -eq 1 ]; then
      printf '%s\n' "$rkeys" | grep -qx "$k" && rc="y" || rc="-"
      printf '  %-34s %-7s %-7s %-7s\n' "$k" "$lc" "$rc" "$ec"
    else
      printf '  %-34s %-7s %-7s\n' "$k" "$lc" "$ec"
    fi
  done <<< "$union"

  echo
  local drift=0
  _report_missing "local .env" "$local_keys" "$example_keys" && drift=1
  if [ "$remote_col" -eq 1 ] && [ "$remote_reachable" -eq 1 ]; then
    _report_missing "$HOST:.env" "$rkeys" "$example_keys" && drift=1
    _report_extra "$HOST:.env" "$rkeys" "$example_keys" && drift=1
  fi
  _report_extra "local .env" "$local_keys" "$example_keys" && drift=1
  [ "$drift" -eq 0 ] && ok "every key set matches .env.example"

  if [ "$USE_REMOTE" -eq 1 ] && [ "$remote_reachable" -eq 0 ]; then return 3; fi
  if [ "$STRICT" -eq 1 ] && [ "$drift" -eq 1 ]; then return 1; fi
  return 0
}

# echo a WARN and return 0 (drift) when $2 is missing keys that $3 has.
_report_missing() {
  local label="$1" have="$2" want="$3" miss
  miss="$(comm -13 <(printf '%s\n' "$have" | grep -v '^$') <(printf '%s\n' "$want" | grep -v '^$'))"
  if [ -n "$miss" ]; then
    warn "$label is missing key(s) present in .env.example: $(echo $miss)"
    return 0
  fi
  return 1
}

# echo a WARN and return 0 (drift) when $2 has keys the template ($3) lacks --
# the likelier way the template rots (a key added to a real .env, never here).
_report_extra() {
  local label="$1" have="$2" want="$3" extra
  extra="$(comm -23 <(printf '%s\n' "$have" | grep -v '^$') <(printf '%s\n' "$want" | grep -v '^$'))"
  if [ -n "$extra" ]; then
    warn "$label has key(s) NOT in .env.example (template may be out of date): $(echo $extra)"
    return 0
  fi
  return 1
}

# --- write mode ------------------------------------------------------------
# Regenerate .env.example from a source .env's key SET, reusing the template's
# existing (already-vetted) comment block + line for every key it already
# documents, and emitting an empty `KEY=` + a TODO for any new key. No value or
# comment ever comes from the real .env.
write_example() {
  # Refuse on the deploy host: .env.example edited there is clobbered by the
  # next rsync --delete and can't be pushed. Detect it by the `deployed` branch
  # redeploy.sh commits to -- never by hard-coding a hostname (itself PII).
  if [ "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" = "deployed" ]; then
    fail "on the 'deployed' branch -- this looks like the deploy host, where .env.example edits are lost. Run this on a dev machine."
    return 1
  fi

  local source_keys
  case "$FROM" in
    local)
      [ -f "$ENV_FILE" ] || { fail ".env not found -- nothing to take a key set from"; return 1; }
      source_keys="$(keys_of "$ENV_FILE")" ;;
    remote)
      ssh -o BatchMode=yes -n "$HOST" true 2>/dev/null || { fail "deploy host '$HOST' unreachable"; return 3; }
      source_keys="$(remote_keys)" ;;
    *) echo "Unknown --from: $FROM (want local|remote)" >&2; return 2 ;;
  esac

  # stale temp from a previously killed run, then a fresh one in the repo root
  # (same filesystem => the final mv is an atomic rename).
  rm -f "$REPO_DIR"/.env.example.tmp.* 2>/dev/null
  # NOT `local`: the EXIT trap fires after this function returns, when a local
  # would be out of scope (leaving the temp behind). $$ keeps concurrent runs
  # from colliding.
  tmp="$REPO_DIR/.env.example.tmp.$$"
  trap 'rm -f "$tmp"' EXIT INT TERM

  _build_example "$source_keys" > "$tmp"

  if ! _verify_candidate "$tmp"; then
    fail "generated .env.example failed its safety check -- original left untouched"
    return 1
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "== .env.example (--dry-run, not written) =="
    diff -u "$EXAMPLE" "$tmp" || true
    ok "dry run only -- nothing written"
    return 0
  fi

  chmod 644 "$tmp"                 # mktemp/umask would leave 600 on a public file
  mv -f "$tmp" "$EXAMPLE"
  trap - EXIT INT TERM
  echo "== .env.example updated -- review before committing =="
  git --no-pager diff -- "$EXAMPLE" || true
  ok "wrote .env.example ($(keys_of "$EXAMPLE" | grep -c . ) keys); values were not copied"
  return 0
}

# Merge a source .env's key SET into the template. The template is preserved
# in its own order and byte-for-byte for every key it already documents (so a
# no-new-key run is a perfect no-op); genuinely new keys are APPENDED as an
# empty `KEY=` + TODO, just before the template's trailer comment. --prune
# drops template keys the source lacks. SOURCE_KEYS is passed via the
# environment, not awk -v, because it is newline-separated (awk -v forbids a
# literal newline in the value).
_build_example() {
  SOURCE_KEYS="$1" PRUNE="$PRUNE" awk '
    BEGIN {
      n=split(ENVIRON["SOURCE_KEYS"], sk, "\n")
      for (i=1;i<=n;i++) if (sk[i]!="") { src[sk[i]]=1; src_order[++sn]=sk[i] }
      prune=ENVIRON["PRUNE"]
    }
    # A key line closes the comment block accumulated above it.
    /^[A-Za-z_][A-Za-z0-9_]*=/ {
      k=$0; sub(/=.*/,"",k)
      block=""; for (i=0;i<pend_n;i++) block=block pend[i] "\n"; pend_n=0
      rec[k]=block $0 "\n"; order[++rn]=k; have[k]=1
      next
    }
    { pend[pend_n++]=$0 }
    END {
      # 1. template records in template order (verbatim); optionally prune
      #    keys the source no longer has. pend now holds the trailing block.
      for (i=1;i<=rn;i++) {
        k=order[i]
        if (prune=="1" && !(k in src)) continue
        printf "%s", rec[k]
      }
      # 2. new keys (in source, not in template) appended in source order,
      #    value never copied.
      for (j=1;j<=sn;j++) {
        k=src_order[j]; if (k in have) continue
        printf "\n# TODO: document this key -- added by scripts/env-structure.sh.\n"
        printf "#   Its value was deliberately not copied; add a publish-safe\n"
        printf "#   placeholder and delete this TODO.\n"
        printf "%s=\n", k
      }
      # 3. template trailer (e.g. the "NOT read from here" note) stays last.
      for (i=0;i<pend_n;i++) printf "%s\n", pend[i]
    }
  ' "$EXAMPLE"
}

# The output is safe iff every line is either byte-identical to a line already
# in the current .env.example, OR a bare `KEY=` (new empty key), OR blank, OR
# one of the fixed TODO comment lines this script emits. Nothing from a real
# .env can satisfy that shape. Belt-and-braces, also reject any line matching a
# vendor secret format or a .pii-denylist term.
_verify_candidate() {
  local f="$1" line
  # secret formats (kept in sync with check-pii.sh; a test asserts they match)
  local secret_re='sk-ant-[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
  if grep -nE "$secret_re" "$f" >/dev/null 2>&1; then
    warn "candidate contains a secret-shaped string"; return 1
  fi
  if [ -f "$REPO_DIR/.pii-denylist" ]; then
    while IFS= read -r term; do
      case "$term" in ''|'#'*) continue ;; esac
      if grep -iFq -- "$term" "$f"; then warn "candidate contains a .pii-denylist term"; return 1; fi
    done < "$REPO_DIR/.pii-denylist"
  fi
  # structural whitelist
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    grep -Fqx -- "$line" "$EXAMPLE" && continue             # verbatim from template
    case "$line" in
      '# TODO: document this key'*) continue ;;
      '#   Its value was deliberately'*) continue ;;
      '#   placeholder and delete this TODO.') continue ;;
    esac
    printf '%s\n' "$line" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*=$' && continue  # bare KEY=
    warn "candidate has an unexpected line (not from the template, not a bare KEY=): $line"
    return 1
  done < "$f"
  return 0
}

# --- dispatch --------------------------------------------------------------
if [ "$MODE" = "write" ]; then
  write_example
  exit $?
else
  report
  exit $?
fi
