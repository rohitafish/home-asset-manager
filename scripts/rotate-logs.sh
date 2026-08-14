#!/usr/bin/env bash
# Bounds the size of logs/app.log and logs/app.error.log. Run periodically
# by ~/Library/LaunchAgents/com.assetmgt.logrotate.plist (installed from
# scripts/com.assetmgt.logrotate.plist -- see README).
#
# Rotates by COPY-THEN-TRUNCATE, never by renaming. launchd's
# StandardOutPath/StandardErrorPath (scripts/com.assetmgt.app.plist) holds an
# open O_APPEND file descriptor on these exact inodes for the life of the
# app process. Renaming the live file would leave the running service
# writing into the archived copy while the newly-created app.log stays
# empty until the next restart -- logging would die silently. Truncating
# in place preserves the inode, so the next O_APPEND write lands at offset
# 0 in the same file launchd already has open. See AGENTS.md.
set -euo pipefail

LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../logs" && pwd)"
MAX_BYTES="${ROTATE_MAX_BYTES:-10485760}"   # 10 MiB
KEEP="${ROTATE_KEEP:-5}"

rotate_one() {
  local log="$1"
  [ -f "$log" ] || return 0

  local size
  size="$(stat -f%z "$log")"
  [ "$size" -ge "$MAX_BYTES" ] || return 0

  rm -f "$log.$KEEP.gz"
  local i
  for (( i = KEEP - 1; i >= 1; i-- )); do
    # `if ... ; then ...; fi`, not `[ -f ... ] && mv ...` -- under set -e a
    # false test as a loop body's last command would exit the whole script.
    if [ -f "$log.$i.gz" ]; then
      mv "$log.$i.gz" "$log.$((i + 1)).gz"
    fi
  done

  cp "$log" "$log.1"
  : > "$log"
  gzip -f "$log.1"
}

rotate_one "$LOG_DIR/app.log"
rotate_one "$LOG_DIR/app.error.log"
