"""Covers scripts/env-structure.sh -- the tool that replicates .env structure
(key names, order, comment blocks) across machines and the tracked template
WITHOUT ever moving a value.

The invariant these tests exist to defend: a value from a real .env is never
read into the template, never sent over ssh, and never printed. So most tests
assert a fake-but-realistically-shaped value is ABSENT from both the written
file and stdout. Every fixture value is synthetic.

A session-scoped guard snapshots the real repo's .env.example and asserts it
is byte-unchanged at teardown -- cheap insurance that no test escaped tmp_path.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "env-structure.sh"
REAL_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

EXAMPLE_FIXTURE = """\
# Postgres
DATABASE_URL=postgresql+psycopg://assetmgt:assetmgt@localhost:5432/assetmgt

# App auth. REQUIRED.
APP_ADMIN_USER=admin
APP_ADMIN_PASSWORD=change-me

# UniFi
UNIFI_API_KEY=

# NOT read from here: a trailing note that must stay last.
"""


@pytest.fixture(scope="session", autouse=True)
def _real_example_untouched():
    before = hashlib.sha256(REAL_EXAMPLE.read_bytes()).hexdigest()
    yield
    after = hashlib.sha256(REAL_EXAMPLE.read_bytes()).hexdigest()
    assert before == after, "a test modified the real .env.example -- it escaped tmp_path"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "env-structure.sh")
    (tmp_path / ".env.example").write_text(EXAMPLE_FIXTURE)
    return tmp_path


def _write_env(repo: Path, text: str) -> None:
    (repo / ".env").write_text(text)


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "scripts/env-structure.sh", "--no-remote", *args],
        cwd=repo, capture_output=True, text=True,
    )


# --- --write-example -------------------------------------------------------

def test_regenerating_from_its_own_keys_is_byte_identical(sandbox):
    """No new keys -> the template must come back unchanged."""
    keys = [
        line.split("=", 1)[0]
        for line in EXAMPLE_FIXTURE.splitlines()
        if line and not line.startswith("#") and "=" in line
    ]
    _write_env(sandbox, "".join(f"{k}=whatever_value_here\n" for k in keys))
    original = (sandbox / ".env.example").read_text()

    proc = _run(sandbox, "--write-example", "--from", "local")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (sandbox / ".env.example").read_text() == original


def test_new_key_is_appended_empty_with_a_todo(sandbox):
    _write_env(sandbox, "APP_ADMIN_USER=admin\nNEW_TOKEN=secretvalue1234567\n")

    proc = _run(sandbox, "--write-example", "--from", "local")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = (sandbox / ".env.example").read_text()
    assert "\nNEW_TOKEN=\n" in text, "new key must be present as an empty value"
    assert "# TODO: document this key" in text


@pytest.mark.parametrize(
    "value",
    ["secretvalue1234567", "sk-ant-" + "A" * 30, "10.44.9.0/24", "Jordan Lee"],
)
def test_no_env_value_reaches_the_written_template(sandbox, value):
    _write_env(sandbox, f"APP_ADMIN_USER=admin\nNEW_SECRET={value}\n")

    proc = _run(sandbox, "--write-example", "--from", "local")

    assert value not in (sandbox / ".env.example").read_text()
    assert value not in proc.stdout


def test_existing_comment_block_is_preserved(sandbox):
    _write_env(sandbox, "APP_ADMIN_USER=admin\nAPP_ADMIN_PASSWORD=x\nUNIFI_API_KEY=y\n")

    _run(sandbox, "--write-example", "--from", "local")

    text = (sandbox / ".env.example").read_text()
    assert "# App auth. REQUIRED." in text
    assert "# NOT read from here: a trailing note that must stay last." in text


def test_template_only_key_is_kept_by_default_and_dropped_with_prune(sandbox):
    # source lacks UNIFI_API_KEY, which the template documents
    _write_env(sandbox, "APP_ADMIN_USER=admin\n")

    _run(sandbox, "--write-example", "--from", "local")
    assert "UNIFI_API_KEY=" in (sandbox / ".env.example").read_text()

    # restore fixture and prune
    (sandbox / ".env.example").write_text(EXAMPLE_FIXTURE)
    _run(sandbox, "--write-example", "--from", "local", "--prune")
    assert "UNIFI_API_KEY=" not in (sandbox / ".env.example").read_text()


def test_dry_run_writes_nothing(sandbox):
    _write_env(sandbox, "APP_ADMIN_USER=admin\nNEW_TOKEN=secretvalue1234567\n")
    original = (sandbox / ".env.example").read_text()

    proc = _run(sandbox, "--write-example", "--from", "local", "--dry-run")

    assert proc.returncode == 0
    assert (sandbox / ".env.example").read_text() == original
    assert "NEW_TOKEN" in proc.stdout  # the diff is shown


def test_verification_refuses_and_leaves_original_intact(sandbox):
    """If a secret-shaped string would end up in the template, abort and leave
    the original byte-identical with no temp file behind.
    """
    poisoned = "# App\nLEAKED=sk-ant-" + "A" * 30 + "\n"
    (sandbox / ".env.example").write_text(poisoned)
    _write_env(sandbox, "LEAKED=sk-ant-" + "A" * 30 + "\n")

    proc = _run(sandbox, "--write-example", "--from", "local")

    assert proc.returncode == 1
    assert (sandbox / ".env.example").read_text() == poisoned
    assert not list(sandbox.glob(".env.example.tmp.*")), "temp file must be cleaned up"


def test_write_without_env_exits_1_and_keeps_file(sandbox):
    original = (sandbox / ".env.example").read_text()

    proc = _run(sandbox, "--write-example", "--from", "local")

    assert proc.returncode == 1
    assert (sandbox / ".env.example").read_text() == original


def test_empty_candidate_refuses_instead_of_writing_a_blank_file(sandbox):
    """Regression test: _verify_candidate's whitelist loop trivially
    "passes" an empty file (its while-read loop body never runs), so a
    failed _build_example -- here, triggered by .env.example going missing
    partway through, the same shape as it being briefly unreadable -- used
    to be able to `mv` a zero-byte candidate over the tracked template while
    still printing "wrote .env.example" as if it had succeeded."""
    _write_env(sandbox, "DATABASE_URL=x\n")
    (sandbox / ".env.example").unlink()  # _build_example's input source

    proc = _run(sandbox, "--write-example", "--from", "local")

    assert proc.returncode == 1, f"an empty candidate must be refused:\n{proc.stdout}\n{proc.stderr}"
    assert "empty" in proc.stdout
    assert not (sandbox / ".env.example").exists(), "must not create a blank file"
    assert not list(sandbox.glob(".env.example.tmp.*")), "temp file must be cleaned up"


# --- report mode -----------------------------------------------------------

def test_report_never_prints_a_value(sandbox):
    _write_env(sandbox, "APP_ADMIN_USER=admin\nAPP_ADMIN_PASSWORD=topsecretpassword\n")

    proc = _run(sandbox)  # report, --no-remote

    assert "topsecretpassword" not in proc.stdout
    assert "APP_ADMIN_PASSWORD" in proc.stdout  # the key name is fine


def test_report_flags_key_missing_from_env(sandbox):
    _write_env(sandbox, "APP_ADMIN_USER=admin\n")  # missing several template keys

    proc = _run(sandbox)

    assert "missing" in proc.stdout
    assert "APP_ADMIN_PASSWORD" in proc.stdout


def test_report_flags_key_absent_from_template(sandbox):
    _write_env(
        sandbox,
        "DATABASE_URL=x\nAPP_ADMIN_USER=admin\nAPP_ADMIN_PASSWORD=x\nUNIFI_API_KEY=x\nSURPRISE_KEY=x\n",
    )

    proc = _run(sandbox)

    assert "NOT in .env.example" in proc.stdout
    assert "SURPRISE_KEY" in proc.stdout


def test_strict_exits_1_on_drift(sandbox):
    _write_env(sandbox, "APP_ADMIN_USER=admin\n")

    proc = _run(sandbox, "--strict")

    assert proc.returncode == 1


def test_unknown_argument_exits_2(sandbox):
    proc = _run(sandbox, "--bogus")

    assert proc.returncode == 2


@pytest.mark.parametrize("flag", ["--host", "--from"])
def test_flag_missing_operand_fails_instead_of_hanging(sandbox, flag):
    """Regression test: --host/--from as the last argument used to make
    `shift 2` silently fail and return non-zero -- with no `set -e`, $#
    never reached 0 and the argument-parsing loop spun forever. A timeout
    here is a correctness assertion, not just test hygiene: a hang would
    otherwise make this test itself hang instead of failing cleanly."""
    proc = subprocess.run(
        ["bash", "scripts/env-structure.sh", flag],
        cwd=sandbox, capture_output=True, text=True, timeout=10,
    )

    assert proc.returncode == 2
    assert "requires an argument" in proc.stderr


# --- the ssh boundary: values must never survive local re-validation -------

def _run_with_stub_ssh(repo: Path, stub_body: str, *args: str) -> subprocess.CompletedProcess:
    """Runs report mode WITH remote, but with a fake `ssh` early on PATH so no
    network is touched and we control exactly what the 'remote' returns.
    """
    bindir = repo / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "ssh"
    stub.write_text("#!/usr/bin/env bash\n" + stub_body)
    stub.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "DEPLOY_HOST": "fakehost"}
    return subprocess.run(
        ["bash", "scripts/env-structure.sh", *args],
        cwd=repo, capture_output=True, text=True, env=env,
    )


def test_hostile_remote_output_is_discarded(sandbox):
    """The single most important safety test: a misbehaving remote that emits a
    value-bearing line and a banner must not leak the value onto the terminal;
    the local `=$` filter keeps only bare KEY= names. A well-formed bare key is
    still counted, proving we didn't just drop everything.
    """
    _write_env(sandbox, "APP_ADMIN_USER=admin\n")
    stub = (
        'case "$*" in\n'
        '  *grep*) printf "Welcome to fakehost banner\\nUNIFI_API_KEY=REALSECRET123456\\nNVD_API_KEY=\\n" ;;\n'
        '  *) exit 0 ;;\n'  # the reachability `ssh ... true` probe
        'esac\n'
    )
    proc = _run_with_stub_ssh(sandbox, stub)

    assert "REALSECRET123456" not in proc.stdout, "a remote value leaked onto the terminal"
    assert "NVD_API_KEY" in proc.stdout, "a well-formed bare key should still be counted"


def test_unreachable_remote_still_reports_and_exits_3(sandbox):
    _write_env(sandbox, "APP_ADMIN_USER=admin\n")
    stub = "exit 255\n"  # ssh failure

    proc = _run_with_stub_ssh(sandbox, stub)

    assert proc.returncode == 3
    assert "unreachable" in proc.stdout
    assert "APP_ADMIN_USER" in proc.stdout  # the local vs template report still printed


def test_secret_regex_matches_check_pii():
    """env-structure.sh's verifier duplicates check-pii.sh's secret regex (a
    sourced helper would be one more thing to break on a fresh clone). Pin that
    the two literals stay identical so a pattern added to one reaches both.
    """
    import re

    def extract(path: Path, var: str) -> str:
        for line in path.read_text().splitlines():
            m = re.match(rf"\s*(?:local\s+)?{var}='([^']*)'", line)
            if m:
                return m.group(1)
        raise AssertionError(f"{var} not found in {path}")

    check_pii = SCRIPT.parent / "check-pii.sh"
    env_struct = SCRIPT
    assert extract(check_pii, "SECRET_RE") == extract(env_struct, "secret_re")
