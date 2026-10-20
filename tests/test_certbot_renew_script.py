"""Pins certbot-renew.sh's renewal-window enforcement.

Every ACME issuance is billed and certificates last 45 days, so
renew_before_expiry sets the cost. certbot only writes it as a commented-out
default, so it was silently absent from 2026-09-23 to 2026-10-20 and every
renewal ran at twice the planned rate. The script now sets it on every run,
and must never let that step stop the renewal itself.

These run the real script against a temporary HOME with a stub `certbot`
first on PATH, so nothing reaches an ACME server.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "certbot-renew.sh"
STATUS = REPO / "scripts" / "aws-status.sh"

CONF_BODY = """version = 2.9.0
archive_dir = /x/archive/example.org
[renewalparams]
authenticator = webroot
"""


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    renewal = home / ".certbot" / "config" / "renewal"
    renewal.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "certbot-called"
    stub = bindir / "certbot"
    stub.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    stub.chmod(0o755)
    run_env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:{os.environ['PATH']}",
    }

    def run():
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return renewal, marker, run


def _active(conf: Path) -> list[str]:
    return [
        line
        for line in conf.read_text().splitlines()
        if line.startswith("renew_before_expiry")
    ]


def test_replaces_certbots_commented_default(env):
    renewal, marker, run = env
    conf = renewal / "example.org.conf"
    conf.write_text("# renew_before_expiry = 30 days\n" + CONF_BODY)
    result = run()
    assert result.returncode == 0, result.stderr
    assert _active(conf) == ["renew_before_expiry = 10 days"]
    assert "# renew_before_expiry" not in conf.read_text()
    assert "setting 10 days" in result.stderr
    assert marker.exists(), "certbot renew must still run"


def test_corrects_a_different_value(env):
    renewal, _, run = env
    conf = renewal / "example.org.conf"
    conf.write_text("renew_before_expiry = 30 days\n" + CONF_BODY)
    assert run().returncode == 0
    assert _active(conf) == ["renew_before_expiry = 10 days"]


def test_inserts_when_the_line_is_missing_entirely(env):
    renewal, _, run = env
    conf = renewal / "example.org.conf"
    conf.write_text(CONF_BODY)
    assert run().returncode == 0
    assert _active(conf) == ["renew_before_expiry = 10 days"]
    assert conf.read_text().endswith(CONF_BODY), "the rest of the config is untouched"


def test_leaves_a_correct_config_byte_for_byte_and_quiet(env):
    renewal, marker, run = env
    conf = renewal / "example.org.conf"
    body = "renew_before_expiry = 10 days\n" + CONF_BODY
    conf.write_text(body)
    result = run()
    assert result.returncode == 0
    assert conf.read_text() == body
    assert result.stderr == ""
    assert not list(renewal.glob("*.bak")), "no sed backup litter"
    assert marker.exists()


def test_skips_macos_appledouble_files(env):
    renewal, _, run = env
    junk = renewal / "._example.org.conf"
    junk.write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X")
    (renewal / "example.org.conf").write_text(
        "renew_before_expiry = 10 days\n" + CONF_BODY
    )
    assert run().returncode == 0
    assert junk.read_bytes() == b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X"


def test_a_failed_write_warns_but_never_blocks_renewal(env):
    """An expired certificate costs more than one extra issuance."""
    renewal, marker, run = env
    conf = renewal / "example.org.conf"
    conf.write_text("# renew_before_expiry = 30 days\n" + CONF_BODY)
    renewal.chmod(stat.S_IRUSR | stat.S_IXUSR)  # sed -i cannot create its temp file
    try:
        if os.access(renewal, os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        result = run()
    finally:
        renewal.chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert "renewing anyway" in result.stderr
    assert marker.exists(), "certbot renew must run even when enforcement fails"


def test_status_check_expects_the_value_the_renew_script_enforces():
    """aws-status.sh FAILs on any other value; the two must not drift apart."""
    enforced = re.search(
        r'^RENEW_BEFORE_EXPIRY="([^"]+)"', SCRIPT.read_text(), re.MULTILINE
    ).group(1)
    status = STATUS.read_text()
    assert f'[ "$RENEW_BEFORE" = "{enforced}" ]' in status


def test_status_check_ignores_the_commented_default():
    """An unanchored grep once read '# renew_before_expiry = 30 days' as live."""
    code = "\n".join(
        line
        for line in STATUS.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "grep -m1 renew_before_expiry" not in code
    assert "/^renew_before_expiry *=/" in code
