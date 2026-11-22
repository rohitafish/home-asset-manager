#!/usr/bin/env bash
# Remote command strings below are built client-side on purpose (REMOTE_DIR
# is validated, the rest are fixed strings), as in redeploy.sh.
# shellcheck disable=SC2029
#
# Collect the software inventory of every machine and import it into the app.
#
# Runs on a dev machine that can ssh to each machine and to the app host. For
# each target it runs the matching collector (scripts/software-inventory/) --
# piped over ssh, nothing is installed on the machine -- then pipes the JSON
# to `python -m discovery.cli software-import` on the app host, and finally
# refreshes end-of-life dates there (`software-eol`). Read-only on every
# machine; the only writes are to the app's database.
#
# Usage:
#   scripts/collect-software.sh                 every target in software-hosts.conf
#   scripts/collect-software.sh mini laptop     only these (as named in the conf)
#   scripts/collect-software.sh --no-eol ...    skip the end-of-life refresh
#
# software-hosts.conf (repo root, gitignored -- host aliases are household
# details) has one target per line:
#   <ssh-alias or "local">  <unix|windows>  [asset-id]
# e.g.
#   local    unix
#   mini     unix
#   laptop   windows  12
# The asset id is optional: without it the importer matches the machine by
# its MAC addresses, then by hostname, and refuses to guess between two.
#
# A machine that is off or unreachable is skipped with a warning; the others
# still import. Exit status is non-zero if any target failed.
#
# Override the app host with DEPLOY_HOST / DEPLOY_REMOTE_DIR, as for
# scripts/redeploy.sh.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COLLECTORS="$REPO_DIR/scripts/software-inventory"
CONF="${SOFTWARE_HOSTS_CONF:-$REPO_DIR/software-hosts.conf}"
APP_HOST="${DEPLOY_HOST:-mint}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-~/claudecode/assetmgt}"
case "$REMOTE_DIR" in
  *[!A-Za-z0-9_./~-]*)
    echo "!!! DEPLOY_REMOTE_DIR contains characters outside [A-Za-z0-9_./~-] -- refusing to hand it to a remote shell." >&2
    exit 2 ;;
esac
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)

run_eol=1
wanted=()
for arg in "$@"; do
  case "$arg" in
    --no-eol) run_eol=0 ;;
    -h|--help) sed -n '6,37p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) wanted+=("$arg") ;;
  esac
done

if [[ ! -f "$CONF" ]]; then
  echo "!!! $CONF not found -- create it (see the comment at the top of this script)." >&2
  exit 2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# PowerShell -EncodedCommand takes base64 of UTF-16LE.
ps_encoded="$(iconv -f UTF-8 -t UTF-16LE "$COLLECTORS/collect_windows.ps1" | base64 | tr -d '\n')"

failed=()
imported=0
while read -r target kind asset_id _rest; do
  [[ -z "${target:-}" || "$target" == \#* ]] && continue
  if (( ${#wanted[@]} )); then
    match=0
    for w in "${wanted[@]}"; do [[ "$w" == "$target" ]] && match=1; done
    (( match )) || continue
  fi
  case "$target" in *[!A-Za-z0-9_.-]*)
    echo "!!! skipping '$target': not a plain ssh alias" >&2; failed+=("$target"); continue ;;
  esac
  case "${asset_id:-}" in ''|*[!0-9]*) asset_id="" ;; esac
  out="$tmp/$target.json"
  echo "-- $target: collecting"
  case "${kind:-unix}" in
    unix)
      if [[ "$target" == "local" ]]; then
        python3 "$COLLECTORS/collect_unix.py" < /dev/null > "$out" 2> "$tmp/$target.err"
      else
        ssh "${SSH_OPTS[@]}" "$target" 'python3 -' < "$COLLECTORS/collect_unix.py" > "$out" 2> "$tmp/$target.err"
      fi ;;
    windows)
      ssh "${SSH_OPTS[@]}" "$target" \
        "powershell -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand $ps_encoded" \
        < /dev/null > "$out" 2> "$tmp/$target.err" ;;
    *)
      echo "!!! $target: unknown kind '$kind' (use unix or windows)" >&2; failed+=("$target"); continue ;;
  esac
  status=$?
  if (( status != 0 )) || ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$out" 2>/dev/null; then
    echo "!!! $target: collection failed (exit $status) -- off, asleep or unreachable? $(head -c 300 "$tmp/$target.err")" >&2
    failed+=("$target")
    continue
  fi
  echo "-- $target: importing"
  import_args="--file -"
  [[ -n "$asset_id" ]] && import_args="$import_args --asset-id $asset_id"
  if ssh "${SSH_OPTS[@]}" "$APP_HOST" \
      "cd $REMOTE_DIR && .venv/bin/python -m discovery.cli software-import $import_args" < "$out"; then
    imported=$((imported + 1))
  else
    echo "!!! $target: import failed" >&2
    failed+=("$target")
  fi
done < "$CONF"

if (( imported && run_eol )); then
  echo "-- refreshing end-of-life dates on $APP_HOST"
  ssh "${SSH_OPTS[@]}" "$APP_HOST" "cd $REMOTE_DIR && .venv/bin/python -m discovery.cli software-eol" \
    || failed+=("software-eol")
fi

echo "== imported $imported machine(s)${failed[*]:+; failed: ${failed[*]}}"
(( ${#failed[@]} == 0 ))
