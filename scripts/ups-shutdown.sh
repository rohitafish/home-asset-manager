#!/usr/bin/env bash
# Watches for a sustained UPS discharge and shuts the Mini down gracefully
# before the UPS itself is exhausted -- avoiding the abrupt "emergency
# shutdown" that `pmset -u haltlevel` would otherwise trigger on its own,
# which can leave Colima's lima/vz guest half-torn-down and unable to
# restart (see AGENTS.md's "Colima can still fail to come back"). This
# script is the manual pre-reboot runbook from README's "Shutting down or
# rebooting the host gracefully", automated and run every 60s by
# /Library/LaunchDaemons/com.assetmgt.upsmonitor.plist (installed from
# scripts/com.assetmgt.upsmonitor.plist -- see README).
#
# RUNS AS ROOT, so it must never execute anything the app user can edit:
#   * launchd runs a root-owned COPY at /usr/local/libexec/assetmgt/
#     ups-shutdown.sh (`sudo install -o root -g wheel -m 755`, see README),
#     never this file inside the user-owned checkout. Edit here, re-install
#     there; scripts/preflight.sh warns when the two drift.
#   * PATH is pinned to the system directories below and every binary is
#     called by absolute path. /opt/homebrew/bin is owned by the app user on
#     Apple silicon, so a root process that searched it first would run a
#     user-supplied `pmset`/`grep`/`shutdown` as root every 60 seconds.
#   The one Homebrew tool involved, colima, is run via `su` AS THE APP USER,
#   which is exactly the privilege that user already has.
#
# Household power comes from a Tesla Powerwall, which already rides out real
# outages for hours. This UPS exists purely to bridge the Powerwall
# Gateway's own grid-to-battery cutover time (on the order of a few hundred
# milliseconds), during which the Mini's internal PSU alone briefly browns
# out -- that's what was actually causing unexplained reboots, not power
# loss itself. So in the overwhelming common case this script sees the Mini
# on UPS power for at most a few seconds before the Gateway finishes
# switching over; the shutdown path below is the rare exception (a longer
# outage than the Powerwall covers, or a dying UPS battery), not the normal
# outcome of a cutover.
#
# Deliberately a LaunchDaemon rather than a LaunchAgent like the other three
# jobs in this repo (app/logrotate/backup): it must run whether or not
# anyone is logged in -- in particular right after `fdesetup authrestart`,
# when the Mini sits at the login window with none of the user's
# LaunchAgents started yet -- and it needs to call `shutdown` directly,
# which a user-level agent can't do without a sudoers grant.
set -euo pipefail

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

# No hardcoded account name here -- match README's convention of only ever
# using a placeholder/absolute-path style value that's filled in per host
# (see __ASSETMGT_DIR__ in the plists). Defaults to the one non-system
# admin account (UniqueID >= 500, the standard macOS cutoff -- matches
# `fdesetup list`/Secure Token behaviour on a single-user Mini), overridable
# via ASSETMGT_USER for a host with more than one real account. Split out of
# the ${VAR:-...} default rather than nested inside it -- bash's parser and
# an awk script's own `{`/`}` don't mix well when nested that way.
DEFAULT_USER="$(/usr/bin/dscl . -list /Users UniqueID 2>/dev/null | /usr/bin/awk '$2 >= 500 {print $1; exit}')"
APP_USER="${ASSETMGT_USER:-$DEFAULT_USER}"
: "${APP_USER:?could not determine which account runs the app -- set ASSETMGT_USER explicitly}"
APP_UID="$(/usr/bin/id -u "$APP_USER")"

# Percent / minutes-remaining below which this treats the outage as long
# enough that the UPS itself might not last it out. Deliberately kept ABOVE
# `pmset -u haltlevel`'s own threshold (see README/AGENTS.md) -- that OS-native
# backstop must stay lower than this so the graceful path here always wins,
# and the emergency halt only ever fires if this script itself failed outright.
HALT_PERCENT="${UPS_HALT_PERCENT:-30}"
HALT_MINUTES="${UPS_HALT_MINUTES:-5}"

STATUS="$(/usr/bin/pmset -g ps)"

if echo "$STATUS" | /usr/bin/head -1 | /usr/bin/grep -q "'AC Power'"; then
  exit 0
fi

# Not on AC. Pull the battery percentage and, once discharging long enough
# for pmset to produce an estimate, minutes remaining, out of the second
# line. Format varies by UPS vendor, so this only depends on the two things
# every UPS reports to pmset: an "NN%" and an "H:MM remaining" -- pmset's
# time format is hours:minutes, NOT minutes:seconds, so "0:04 remaining" is
# 4 minutes, not 4 seconds -- both parts have to be converted to a single
# total-minutes figure below, not just the hours field taken on its own
# (that would silently compare "hours" against a "minutes" threshold,
# reading anything under an hour as "0" regardless of the actual minutes).
# `|| true` throughout -- under `set -o pipefail`, grep finding no match
# would otherwise trip `set -e` and exit before the checks below ever run.
PERCENT="$(echo "$STATUS" | /usr/bin/grep -oE '[0-9]+%' | /usr/bin/head -1 | /usr/bin/tr -d '%' || true)"
REMAINING_HM="$(echo "$STATUS" | /usr/bin/grep -oE '[0-9]+:[0-9]+ remaining' | /usr/bin/head -1 | /usr/bin/cut -d' ' -f1 || true)"
REMAINING=""
if [ -n "$REMAINING_HM" ]; then
  RH="${REMAINING_HM%%:*}"
  RM="${REMAINING_HM##*:}"
  # 10#$RM: without the explicit base, bash arithmetic treats a zero-padded
  # minutes field like "08" or "09" as an invalid octal literal and errors.
  REMAINING=$((RH * 60 + 10#$RM))
fi

SHOULD_HALT=0
if [ -n "$PERCENT" ] && [ "$PERCENT" -le "$HALT_PERCENT" ]; then
  SHOULD_HALT=1
fi
if [ -n "$REMAINING" ] && [ "$REMAINING" -le "$HALT_MINUTES" ]; then
  SHOULD_HALT=1
fi

if [ "$SHOULD_HALT" -eq 0 ]; then
  # On UPS but still comfortably above threshold -- the expected shape of a
  # Gateway cutover, not a failure. Logged anyway so a real outage leaves a
  # trail; see the plist's StandardErrorPath note on why this being
  # non-silent-on-success is fine here (unlike rotate-logs.sh/backup-db.sh).
  echo "[$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)] ups-shutdown.sh: on UPS power (${PERCENT:-?}%, ${REMAINING:-?}m remaining) -- below halt threshold, not shutting down" >&2
  exit 0
fi

echo "[$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)] ups-shutdown.sh: UPS at ${PERCENT:-?}% / ${REMAINING:-?}m remaining -- initiating graceful shutdown" >&2

# Stop the app first so it isn't crash-looping against a vanishing DB once
# Colima goes down (same order as the manual runbook in AGENTS.md/README).
# `launchctl asuser` is the documented way for a root process to reach into
# another user's GUI launchd domain -- this daemon runs as root, and the
# app's LaunchAgent lives in gui/$APP_UID, not the system domain. `|| true`
# on this and the colima stop below: after an `authrestart` with nobody
# logged in yet, neither has started, so "nothing to stop" is the expected
# outcome here, not a failure worth aborting the shutdown over.
#
# NOT independently verified against a logged-in session as of writing --
# this is the one part of the whole plan that needs confirming for real
# (see the plan's "Residual risks"/"Verification"). If this specific call
# turns out not to reach the user's domain from root, the fallback is
# simpler than it looks: skipping it just means `launchctl kickstart -k`
# restarts the app once against a live DB after the reboot -- not silent
# data loss -- so this is a nice-to-have ordering step, not load-bearing.
/bin/launchctl asuser "$APP_UID" /bin/launchctl bootout "gui/$APP_UID/com.assetmgt.app" 2>/dev/null || true

# Runs as $APP_USER, not root -- colima's state lives under that user's
# ~/.colima, and `colima stop` run as root would look in root's own home
# directory and find nothing to stop. No time limit here, unlike a plain
# `shutdown`'s few-second SIGTERM window -- this is what actually prevents
# the "vz driver is running but host agent is not" stale-state bug (see
# AGENTS.md). `su` needs no password when the caller is already root.
/usr/bin/su "$APP_USER" -c 'eval "$(/opt/homebrew/bin/brew shellenv)"; colima stop' 2>/dev/null || true

/sbin/shutdown -h now
