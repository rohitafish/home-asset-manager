"""Pins the privilege posture of the launchd templates in scripts/.

The UPS monitor is a LaunchDaemon: it runs as root every 60 seconds. A root
process must never execute anything the app user can write to, and on an
Apple-silicon Homebrew install that includes /opt/homebrew/bin and every
binary in it. Two mistakes would each hand root to anything running as the
app user (the web app included), and both are one plausible "tidy-up" edit
away from coming back:

  * pointing ProgramArguments at scripts/ups-shutdown.sh inside the checkout
    (the pattern the three LaunchAgent plists legitimately use), or
  * copying the LaunchAgents' PATH, which lists /opt/homebrew/bin first.

The script itself gets the same treatment: system PATH, absolute paths for
every binary, so a stray `pmset` can't resolve to a user-owned file.
"""

import plistlib
import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
DAEMON_PLIST = SCRIPTS / "com.assetmgt.upsmonitor.plist"
DAEMON_SCRIPT = SCRIPTS / "ups-shutdown.sh"
AGENT_PLISTS = sorted(
    p for p in SCRIPTS.glob("com.assetmgt.*.plist") if p != DAEMON_PLIST
)

SYSTEM_DIRS = {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}


def _load(path: Path) -> dict:
    return plistlib.loads(path.read_bytes())


def test_daemon_runs_a_root_owned_copy_not_the_checkout():
    args = _load(DAEMON_PLIST)["ProgramArguments"]
    assert args == ["/bin/bash", "/usr/local/libexec/assetmgt/ups-shutdown.sh"]
    assert "__ASSETMGT_DIR__" not in DAEMON_PLIST.read_text(), (
        "the daemon plist must not take the checkout path -- root would run a "
        "user-writable script"
    )
    assert "WorkingDirectory" not in _load(DAEMON_PLIST)


def test_daemon_path_is_system_directories_only():
    path = _load(DAEMON_PLIST)["EnvironmentVariables"]["PATH"]
    assert set(path.split(":")) <= SYSTEM_DIRS, path
    assert "/opt/homebrew" not in path


def test_daemon_has_no_user_name_key_so_it_really_is_root():
    """The whole point of the daemon is `shutdown`, which needs root. If a
    UserName key ever appears the PATH/ownership rules above would be moot
    -- but so would the script. Pin the assumption the other tests rest on."""
    assert "UserName" not in _load(DAEMON_PLIST)


def test_agents_still_take_the_checkout_placeholder():
    """The counterweight: the three user-level agents legitimately run out of
    the checkout via the __ASSETMGT_DIR__ sed step, and preflight.sh relies on
    that placeholder to detect an unsubstituted install. Don't let the
    daemon's stricter rule leak into them by accident."""
    assert len(AGENT_PLISTS) == 3, AGENT_PLISTS
    for plist in AGENT_PLISTS:
        assert "__ASSETMGT_DIR__" in plist.read_text(), plist.name


def test_script_pins_a_system_path_before_doing_anything():
    body = DAEMON_SCRIPT.read_text()
    match = re.search(r"^PATH=([^\n]+)$", body, re.MULTILINE)
    assert match, "ups-shutdown.sh must set PATH explicitly"
    assert set(match.group(1).split(":")) <= SYSTEM_DIRS, match.group(1)
    assert body.index("PATH=") < body.index("DEFAULT_USER="), "PATH must be set first"


COMMANDS = ["dscl", "awk", "id", "pmset", "head", "grep", "tr", "cut", "date", "launchctl", "su", "shutdown"]


@pytest.mark.parametrize("command", COMMANDS)
def test_script_calls_every_binary_by_absolute_path(command):
    """A bare `pmset` resolves through PATH; pinning PATH above already covers
    that, but absolute paths make the intent survive someone 'simplifying'
    the PATH line away. Comments are ignored; the `su -c '...'` string runs
    as the app user, not root, so its contents are out of scope here."""
    code_lines = [
        line for line in DAEMON_SCRIPT.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in code_lines:
        if "/usr/bin/su " in line:
            line = line.split("-c", 1)[0]
        for hit in re.finditer(rf"(?<![\w/.-]){re.escape(command)}(?=\s)", line):
            # A match preceded by a slash is an absolute path -- the lookbehind
            # excludes it, so any remaining hit is a bare command name.
            pytest.fail(f"bare `{command}` in ups-shutdown.sh: {line.strip()!r} (col {hit.start()})")


def test_script_calls_the_expected_absolute_binaries():
    """Positive counterpart to the bare-name check: the binaries it does use
    live where macOS ships them, and nowhere under a user-owned prefix."""
    body = DAEMON_SCRIPT.read_text()
    for path in ["/usr/bin/pmset", "/usr/bin/dscl", "/bin/launchctl", "/usr/bin/su", "/sbin/shutdown"]:
        assert path in body, path
    root_calls = re.findall(r"(?m)^[^#]*?(/opt/homebrew/[^\s'\"]+)", body)
    assert all("brew shellenv" in line for line in [body[body.index(c) - 40:body.index(c) + 40] for c in root_calls]), (
        "the only Homebrew path the script may touch is `brew shellenv` inside the su -c string"
    )
