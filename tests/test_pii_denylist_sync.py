"""scripts/pii-denylist-sync.sh: the block-rewrite logic, exercised offline
by stubbing `ssh` so the "inventory" is whatever the test says it is.

The invariants worth pinning: hand-written lines above the marker survive
untouched, the auto block is replaced whole (so stale entries drop out),
template defaults never become entries, an empty pull never empties the
file, and the script prints counts but never a value.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pii-denylist-sync.sh"
BEGIN = "# >>> auto-synced from the live inventory by scripts/pii-denylist-sync.sh"


def _run(tmp_path: Path, inventory: str, *args: str, denylist: str | None = None):
    """Runs the script with REPO_DIR = tmp_path and a fake `ssh` that prints
    `inventory` regardless of arguments."""
    (tmp_path / "scripts").mkdir(exist_ok=True)
    script = tmp_path / "scripts" / "pii-denylist-sync.sh"
    script.write_text(SCRIPT.read_text())
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    ssh = fake_bin / "ssh"
    ssh.write_text("#!/bin/sh\ncat <<'EOF'\n" + inventory + "\nEOF\n")
    ssh.chmod(0o755)
    if denylist is not None:
        (tmp_path / ".pii-denylist").write_text(denylist)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    return subprocess.run([str(script), *args], capture_output=True, text=True, env=env)


HAND = "# my own lines\nSome Real Name\n"
INVENTORY = "aa:bb:cc:dd:ee:01\nC02FAKESERIAL\nOwner\nMatilda\n"


def test_writes_pulled_values_below_the_marker_and_keeps_hand_lines(tmp_path):
    r = _run(tmp_path, INVENTORY, denylist=HAND)
    assert r.returncode == 0, r.stderr
    text = (tmp_path / ".pii-denylist").read_text()
    assert text.startswith("# my own lines\nSome Real Name\n")
    assert BEGIN in text
    block = text.split(BEGIN, 1)[1]
    assert (
        "aa:bb:cc:dd:ee:01" in block and "C02FAKESERIAL" in block and "Matilda" in block
    )
    assert "\nOwner\n" not in block  # the .env.example default is not an identifier


def test_the_block_is_replaced_whole_so_stale_entries_drop_out(tmp_path):
    first = _run(tmp_path, INVENTORY, denylist=HAND)
    assert first.returncode == 0
    second = _run(
        tmp_path, "aa:bb:cc:dd:ee:01\n"
    )  # serial and name gone from the inventory
    assert second.returncode == 0, second.stderr
    text = (tmp_path / ".pii-denylist").read_text()
    assert "C02FAKESERIAL" not in text and "Matilda" not in text
    assert "Some Real Name" in text  # hand-written stays


def test_never_prints_a_value(tmp_path):
    r = _run(tmp_path, INVENTORY, denylist=HAND)
    for secret in ("aa:bb:cc:dd:ee:01", "C02FAKESERIAL", "Matilda", "Some Real Name"):
        assert secret not in r.stdout + r.stderr


def test_dry_run_writes_nothing(tmp_path):
    r = _run(tmp_path, INVENTORY, "--dry-run", denylist=HAND)
    assert r.returncode == 0
    assert "dry run" in r.stdout
    assert (tmp_path / ".pii-denylist").read_text() == HAND


def test_empty_pull_refuses_to_touch_the_file(tmp_path):
    r = _run(tmp_path, "\n\n", denylist=HAND)
    assert r.returncode == 1
    assert (tmp_path / ".pii-denylist").read_text() == HAND


def test_the_written_file_is_owner_only(tmp_path):
    _run(tmp_path, INVENTORY, denylist=HAND)
    mode = stat.S_IMODE((tmp_path / ".pii-denylist").stat().st_mode)
    assert mode == 0o600


@pytest.mark.parametrize("flag", ["--bogus"])
def test_unknown_flag_is_a_usage_error(tmp_path, flag):
    assert _run(tmp_path, INVENTORY, flag, denylist=HAND).returncode == 2
