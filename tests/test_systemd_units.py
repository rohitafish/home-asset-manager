"""Pins the posture of the systemd unit templates in scripts/systemd/, the
Linux counterparts of the launchd plists tests/test_launchd_plists.py covers.

The app must bind loopback only (HTTP Basic crosses the wire on every
request, so a TLS terminator on the same host fronts it), must trust
X-Forwarded-* from loopback alone, and must run as the app user, never root.
Placeholders are the three install-systemd-units.sh knows how to fill.
"""

import re
from pathlib import Path

import pytest

UNIT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "systemd"
UNITS = sorted(UNIT_DIR.glob("assetmgt-*"))
SERVICES = [u for u in UNITS if u.suffix == ".service"]
TIMERS = [u for u in UNITS if u.suffix == ".timer"]
KNOWN_PLACEHOLDERS = {"__ASSETMGT_DIR__", "__ASSETMGT_USER__", "__ASSETMGT_HOME__"}


def _settings(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")):
            continue
        key, _, value = line.partition("=")
        out.setdefault(key.strip(), []).append(value.strip())
    return out


def test_the_expected_units_exist():
    names = {u.name for u in UNITS}
    assert {
        "assetmgt-app.service",
        "assetmgt-backup.service",
        "assetmgt-backup.timer",
    } <= names
    assert "assetmgt-logrotate.timer" in names and "assetmgt-certrenew.timer" in names


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_only_known_placeholders(unit):
    found = set(re.findall(r"__ASSETMGT_[A-Z]+__", unit.read_text()))
    assert found <= KNOWN_PLACEHOLDERS, found - KNOWN_PLACEHOLDERS


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_services_run_as_the_app_user_from_the_checkout(service):
    s = _settings(service)
    assert s["User"] == ["__ASSETMGT_USER__"]
    assert s["WorkingDirectory"] == ["__ASSETMGT_DIR__"]
    assert all("__ASSETMGT_DIR__" in e for e in s["ExecStart"])


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_no_homebrew_paths_leak_into_linux_units(service):
    assert "/opt/homebrew" not in service.read_text()


def test_app_binds_loopback_and_trusts_only_loopback_proxy_headers():
    exec_start = _settings(UNIT_DIR / "assetmgt-app.service")["ExecStart"][0]
    assert "--host 127.0.0.1" in exec_start
    assert "0.0.0.0" not in exec_start
    assert "--proxy-headers" in exec_start
    assert "--forwarded-allow-ips 127.0.0.1" in exec_start


def test_app_restarts_with_a_floor_not_a_tight_loop():
    s = _settings(UNIT_DIR / "assetmgt-app.service")
    assert s["Restart"] == ["always"]
    assert int(s["RestartSec"][0]) >= 10
    assert s.get("StartLimitIntervalSec") == ["0"]


def test_app_logs_go_where_rotate_logs_expects():
    s = _settings(UNIT_DIR / "assetmgt-app.service")
    assert s["StandardOutput"] == ["append:__ASSETMGT_DIR__/logs/app.log"]
    assert s["StandardError"] == ["append:__ASSETMGT_DIR__/logs/app.error.log"]


@pytest.mark.parametrize("timer", TIMERS, ids=lambda p: p.name)
def test_timers_are_installable(timer):
    assert _settings(timer)["WantedBy"] == ["timers.target"]


@pytest.mark.parametrize("name", ["backup", "certrenew"])
def test_calendar_timers_catch_up_after_downtime(name):
    s = _settings(UNIT_DIR / f"assetmgt-{name}.timer")
    assert "OnCalendar" in s and s["Persistent"] == ["true"]


def test_backup_runs_after_docker():
    s = _settings(UNIT_DIR / "assetmgt-backup.service")
    assert "docker.service" in " ".join(s["After"])
