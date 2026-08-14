"""Tests for app/backup_status.py. _MARKER_PATH is monkeypatched to a
tmp_path location in every test here -- backups/last-success in the real
repo is a live marker written by the nightly backup job on the deployed
instance, and must never be read, depended on, or (especially) overwritten
by this suite.
"""

from datetime import timedelta

import app.backup_status as backup_status_module
from app.backup_status import backup_age_label, backup_status
from app.clock import utcnow_naive


def _set_marker(monkeypatch, tmp_path, text=None):
    marker = tmp_path / "last-success"
    if text is not None:
        marker.write_text(text)
    monkeypatch.setattr(backup_status_module, "_MARKER_PATH", marker)
    return marker


def test_backup_status_missing_marker_reads_as_never_and_stale(monkeypatch, tmp_path):
    _set_marker(monkeypatch, tmp_path, text=None)  # file not created

    status = backup_status()

    assert status == {"last_backup": None, "backup_age_hours": None, "backup_stale": True}


def test_backup_status_unparseable_marker_reads_as_never_and_stale(monkeypatch, tmp_path):
    _set_marker(monkeypatch, tmp_path, text="not-a-timestamp")

    status = backup_status()

    assert status == {"last_backup": None, "backup_age_hours": None, "backup_stale": True}


def test_backup_status_recent_marker_is_fresh(monkeypatch, tmp_path):
    stamp = (utcnow_naive() - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _set_marker(monkeypatch, tmp_path, text=stamp)

    status = backup_status()

    assert status["last_backup"] == stamp
    assert status["backup_stale"] is False
    assert 5.9 <= status["backup_age_hours"] <= 6.1


def test_backup_status_old_marker_is_stale(monkeypatch, tmp_path):
    stamp = (utcnow_naive() - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _set_marker(monkeypatch, tmp_path, text=stamp)

    status = backup_status()

    assert status["backup_stale"] is True


def test_backup_status_marker_with_trailing_whitespace_is_stripped(monkeypatch, tmp_path):
    stamp = (utcnow_naive() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _set_marker(monkeypatch, tmp_path, text=f"{stamp}\n")

    status = backup_status()

    assert status["last_backup"] == stamp
    assert status["backup_stale"] is False


# -- backup_age_label ---------------------------------------------------------------


def test_backup_age_label_never():
    assert backup_age_label({"last_backup": None, "backup_age_hours": None, "backup_stale": True}) == "never"


def test_backup_age_label_hours():
    status = {"last_backup": "x", "backup_age_hours": 6.0, "backup_stale": False}
    assert backup_age_label(status) == "6h"


def test_backup_age_label_days():
    status = {"last_backup": "x", "backup_age_hours": 50.0, "backup_stale": True}
    assert backup_age_label(status) == "2d"
