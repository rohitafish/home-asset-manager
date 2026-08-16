"""Reads the freshness marker scripts/backup-db.sh writes after a
successful nightly off-site database backup (see README's "Off-site
database backups"). Kept separate from app/db.py since this reads a plain
file, not the database, and separate from template_filters.py since it
isn't Jinja-specific -- both app/main.py's /health and dashboard.py's
/summary read it, and this is the one place the staleness threshold lives
so the two can't drift apart.
"""

from datetime import datetime
from pathlib import Path
from typing import TypedDict

from app.clock import utcnow_naive

# Relative to the process's cwd, same convention dashboard.py already uses
# for app/static/style.css -- the app is always run from the repo root (see
# the launchd plist's WorkingDirectory / the venv activation instructions).
_MARKER_PATH = Path("backups/last-success")

# Derived from the actual tick schedule, not a round number. The backup
# LaunchAgent fires three times a day (03:15 / 12:15 / 19:15 local, see
# scripts/com.assetmgt.backup.plist), and every *successful* tick refreshes
# the marker -- including the one that skips because the day's off-site copy
# already exists. So in normal operation the marker is never older than the
# longest gap between ticks, about 8 hours.
#
# The threshold is therefore set by how long the host can legitimately go
# without a successful tick, not by the backup interval: the documented
# powered-off / FileVault case (README's "Power outages and unattended
# restart") can swallow a whole day's ticks, which is 24h from the previous
# evening's run, plus slack.
#
# Was 36h, on the older premise of "one nightly run plus a day's grace" --
# that predated both the extra daytime ticks and the marker being refreshed
# on the skip path, and meant a full day of failed uploads still read as
# healthy. Observed for real: three consecutive failed ticks sat at 30.9h
# and never tripped it.
_STALE_AFTER_HOURS = 26


class BackupStatus(TypedDict):
    last_backup: str | None
    backup_age_hours: float | None
    backup_stale: bool


def backup_status() -> BackupStatus:
    """On a dev checkout -- no backups/ dir, or the backup LaunchAgent has
    simply never run on this machine -- this reads as "never"/stale. That's
    correct, not a bug: only the deployed instance runs the nightly backup
    job."""
    try:
        text = _MARKER_PATH.read_text().strip()
        # The trailing "Z" is matched as a literal character here, not %z --
        # scripts/backup-db.sh always writes UTC, so this intentionally
        # produces a naive datetime consistent with app/clock.py's
        # naive-UTC-in-DB convention, not a bug to "fix" into a %z/aware parse.
        stamp = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except (OSError, ValueError):
        return {"last_backup": None, "backup_age_hours": None, "backup_stale": True}
    age_hours = (utcnow_naive() - stamp).total_seconds() / 3600
    return {
        "last_backup": text,
        "backup_age_hours": round(age_hours, 1),
        "backup_stale": age_hours > _STALE_AFTER_HOURS,
    }


def backup_age_label(status: BackupStatus) -> str:
    """Human label for the Summary metric card, e.g. "6h", "2d", "never"."""
    if status["last_backup"] is None:
        return "never"
    hours = status["backup_age_hours"] or 0.0
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"
