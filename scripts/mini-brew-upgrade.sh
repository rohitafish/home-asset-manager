#!/usr/bin/env bash
# Safe `brew upgrade` for the Mini -- the machine running the live app.
#
# Why this exists: a bare `brew upgrade` here once broke the app while it
# kept running. `python@3.12` got upgraded (3.12.13_4 -> 3.12.14) and
# Homebrew's cleanup deleted the old Cellar directory while uvicorn was
# still using it. Jinja2 compiles templates lazily, on first render -- so
# the first request to a template that worker hadn't rendered yet hit a
# codec lookup against files that had just vanished
# (jinja2.exceptions.TemplateSyntaxError: unicode-escape, rendering
# assets_list.html). It self-healed at the next restart, which is exactly
# the "briefly broke, then fine" shape the upgrade produced.
#
# The fix isn't "never upgrade" or "pin more formulae" -- python@3.x is
# deliberately NOT pinned; that would just forfeit interpreter security
# patches while postponing this same problem to whenever it's unpinned.
# The fix is: never leave the app running across an upgrade. This script
# stops it first, defers Homebrew's cleanup until after it's back up (so
# nothing it's using gets deleted mid-upgrade), and verifies with
# preflight.sh before declaring success. `colima` stays the one pinned
# formula (see AGENTS.md) -- it's checked below, not touched here.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
eval "$(/opt/homebrew/bin/brew shellenv)"

AUTO_YES=0
RUN_PREFLIGHT=1
for arg in "$@"; do
  case "$arg" in
    -y|--yes) AUTO_YES=1 ;;
    --no-preflight) RUN_PREFLIGHT=0 ;;
    *)
      echo "Usage: $0 [-y|--yes] [--no-preflight]" >&2
      exit 1
      ;;
  esac
done

APP_LABEL="com.assetmgt.app"
APP_PLIST="$HOME/Library/LaunchAgents/$APP_LABEL.plist"

echo "==> Pre-flight checks"
# This script stops/restarts the live app service, so it only makes sense on
# the machine that runs it. Mirror image of redeploy.sh's refusal to run ON
# the Mini -- here the guard is the opposite direction.
if [ ! -f "$APP_PLIST" ]; then
  echo "!!! $APP_PLIST not found." >&2
  echo "!!! This upgrades the machine running the live service -- run it on" >&2
  echo "!!! the Mini (ssh mini), not from a dev checkout." >&2
  exit 1
fi

echo "==> Checking for available upgrades"
brew update
OUTDATED="$(brew outdated --quiet)"
if [ -z "$OUTDATED" ]; then
  echo "Nothing to upgrade."
  exit 0
fi
echo "The following formulae have updates available:"
echo "$OUTDATED" | sed 's/^/  /'

if [ "$AUTO_YES" != "1" ]; then
  read -p "Upgrade these and restart the app? [y/N] " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted. Nothing changed."
    exit 1
  fi
fi

# colima hosts the live Postgres data; an unnoticed unpin would let this
# script upgrade -- and potentially recreate -- it. See AGENTS.md and
# the pin recorded when this was set up.
if ! brew list --pinned | grep -qx colima; then
  echo "!!! WARNING: colima is not pinned (brew list --pinned doesn't show it)." >&2
  echo "!!! An upgrade may recreate its VM, which can be destructive to the" >&2
  echo "!!! live Postgres data. Re-pin it (brew pin colima) unless this is" >&2
  echo "!!! intentional." >&2
  if [ "$AUTO_YES" != "1" ]; then
    read -p "Continue anyway? [y/N] " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
  fi
fi

# Both the trap below and the normal restart in step 6 call this -- retrying
# past launchd's own teardown race, not just wrapping the call. `bootout`
# returns as soon as it *requests* removal, before the prior instance has
# necessarily finished deregistering; a `bootstrap` that lands in that window
# fails with "Bootstrap failed: 5: Input/O error", not the "already running"
# kind of error a retry would be pointless against. Found empirically: an
# upgrade that fails fast (before the wait-loop below even matters) fires the
# trap into exactly this window. A few short retries ride it out without
# guessing a fixed sleep long enough for every case.
bootstrap_app() {
  local i err
  # mktemp, not a fixed /tmp name: a predictable path in a shared directory
  # is a symlink target for any other account on the machine.
  err="$(mktemp -t mini-brew-upgrade-bootstrap)"
  for i in 1 2 3 4 5; do
    if launchctl bootstrap "gui/$(id -u)" "$APP_PLIST" 2>"$err"; then
      rm -f "$err"
      return 0
    fi
    sleep 0.5
  done
  echo "!!! launchctl bootstrap failed after retries:" >&2
  cat "$err" >&2 2>/dev/null || true
  rm -f "$err"
  return 1
}

echo "==> Stopping the app"
# bootout, not `launchctl stop` -- the agent sets KeepAlive: true
# (scripts/com.assetmgt.app.plist), so `stop` alone just gets respawned.
# The EXIT trap is what makes stopping safe: a failed upgrade, a set -e
# abort, or a Ctrl-C here all still bring the service back. Only the app
# agent is touched -- backup/logrotate don't hold a long-lived interpreter,
# so they're not part of this hazard.
launchctl bootout "gui/$(id -u)/$APP_LABEL" 2>/dev/null || true
# bootout is asynchronous (see bootstrap_app's comment above) -- wait for the
# job to actually disappear rather than assuming it's gone the instant the
# command returns, so nothing races it back in below.
for i in $(seq 1 20); do
  launchctl list "$APP_LABEL" >/dev/null 2>&1 || break
  sleep 0.25
done
trap 'bootstrap_app || true' EXIT

echo "==> Upgrading"
# NO_INSTALL_CLEANUP defers deletion of old kegs until the explicit
# `brew cleanup` at the end, after the app is back up on the new
# interpreter -- this is the other half of the fix: nothing a running
# process might still touch gets removed out from under it mid-upgrade.
HOMEBREW_NO_INSTALL_CLEANUP=1 brew upgrade

echo "==> Restarting the app"
# bootstrap_app called explicitly BEFORE clearing the trap, not after: the
# whole point of `trap 'bootstrap_app || true' EXIT` above is that a Ctrl-C
# or failure anywhere in the restart window still brings the app back --
# clearing the trap first would reopen exactly that window for the one
# operation it exists to protect. Only remove the safety net once the
# restart attempt has actually happened (regardless of its own outcome --
# bootstrap_app already retries and reports on failure internally).
bootstrap_app
trap - EXIT

# If python@3.x was among the upgraded formulae, .venv/pyvenv.cfg's
# recorded `executable=` path may now point at a deleted Cellar directory
# (this is what happened in the original incident: 3.12.13_4 was recorded,
# then deleted on upgrade to 3.12.14). Harmless in practice --
# .venv/bin/python3.12 resolves via the stable
# /opt/homebrew/opt/python@3.12 symlink, which points at whatever is
# currently linked -- but it's the one durable trace of this class of
# problem, so flag it rather than silently leaving it stale.
if echo "$OUTDATED" | grep -q '^python@'; then
  PYVENV_CFG="$REPO_DIR/.venv/pyvenv.cfg"
  if [ -f "$PYVENV_CFG" ]; then
    VENV_PY="$(grep -m1 '^executable *= *' "$PYVENV_CFG" | cut -d= -f2- | xargs)"
    if [ -n "$VENV_PY" ] && [ ! -e "$VENV_PY" ]; then
      echo "!!! WARN: .venv/pyvenv.cfg's recorded interpreter no longer exists:" >&2
      echo "!!!   $VENV_PY" >&2
      echo "!!! .venv/bin/python3.12 still resolves fine via Homebrew's stable" >&2
      echo "!!! opt-symlink, so this is informational, not broken -- but if" >&2
      echo "!!! .venv ever needs rebuilding: rm -rf .venv && python3 -m venv .venv" >&2
      echo "!!!   && source .venv/bin/activate && pip install --require-hashes -r requirements.txt" >&2
      [ -f "$REPO_DIR/requirements-dev.txt" ] && echo "!!!   && pip install --require-hashes -r requirements-dev.txt" >&2
    fi
  fi
fi

sleep 2
echo "==> Health check"
# `curl ... && echo` as a bare statement is exempt from set -e's abort-on-
# failure: bash's && / || exemption covers every command in the list EXCEPT
# the one following the final &&/||, and curl here is that non-final
# command. So a failed curl (app didn't come back up) used to silently skip
# `echo` and fall straight through to preflight.sh/brew cleanup/"Done" with
# no indication anything was wrong -- exactly the "upgrade broke the app
# while it kept running" failure mode this whole script exists to catch.
# redeploy.sh's health check uses the same explicit-check shape for the
# same reason.
if ! { curl -sf http://127.0.0.1:8000/health && echo; }; then
  echo "!!! Health check failed -- the app did not come back up cleanly after" >&2
  echo "!!! the upgrade. Check logs/app.error.log and logs/app.log." >&2
  exit 1
fi

if [ "$RUN_PREFLIGHT" = "1" ]; then
  echo "==> Running preflight.sh"
  # Reuses the project's existing doctor rather than re-implementing its
  # checks -- confirms the venv imports, Colima/Postgres, migration state,
  # and runs pytest + ruff, so it catches a broken interpreter that a bare
  # /health can't. Its exit code becomes this script's exit code.
  ./scripts/preflight.sh
else
  echo "==> Skipping preflight.sh (--no-preflight)"
fi

echo "==> Finalizing (brew cleanup)"
brew cleanup

echo "==> Done"
