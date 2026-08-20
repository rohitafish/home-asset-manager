"""Covers scripts/check-pii.sh's IP allowlist and its secret-detection rules.

The allowlist suppresses dotted-quad strings confirmed not to be IP
addresses, so a WARN doesn't re-fire forever on the same known-benign value
(a warning people learn to scroll past is how a real leak gets missed). It
is deliberately scoped to the file each value appears in -- these tests pin
that scoping, because "simplifying" it to a bare list of values would widen
a privacy control silently and with no visible symptom.

The secret rules (vendor-prefixed key formats, this machine's own .env
values, and a tracked .env file) are the first rules whose *blocking*
behaviour matters, so those tests assert the exit code, not just stdout --
and they assert the secret's value is NEVER echoed, which is the whole
reason those rules use `git grep -l` / a withheld-value message. Every
synthetic secret below is built from a fixed non-credential alphabet, never
a real key.

The script resolves its own repo from ${BASH_SOURCE[0]}/.., so copying it
into a throwaway git repo is enough to run it against fixture commits
rather than this project's real history.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-pii.sh"

# Value and path from the script's own IP_ALLOWLIST. The Sonos fixture's
# <hardwareVersion>1.9.1.10-2.2</hardwareVersion> is a version string, not
# an address, but matches the dotted-quad pattern.
ALLOWLISTED_VALUE = "1.9.1.10"
ALLOWLISTED_PATH = "tests/test_sonos_api.py"
UNLISTED_PATH = "probes/somewhere_else.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo carrying a copy of the script under test."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")

    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check-pii.sh")
    # Ignore .env so a fixture .env (rule (b)'s value source) isn't swept into
    # a commit by _commit_file's `git add -A` -- which would otherwise trip
    # rule (c) in every unrelated test.
    (tmp_path / ".gitignore").write_text(".env\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def _write_env(repo: Path, text: str) -> None:
    """Writes a gitignored .env -- rule (b) reads it as its value source."""
    (repo / ".env").write_text(text)


def _write_env_example(repo: Path, text: str) -> None:
    (repo / ".env.example").write_text(text)


def _commit_file(repo: Path, relpath: str, content: str) -> str:
    """Commits content at relpath; returns the range covering just it."""
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"add {relpath}")
    return "HEAD~1..HEAD"


def _run_proc(repo: Path, rng: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "scripts/check-pii.sh", "--range", rng],
        cwd=repo, capture_output=True, text=True,
    )


def _run(repo: Path, rng: str) -> str:
    return _run_proc(repo, rng).stdout


def test_allowlisted_value_in_its_own_file_does_not_warn(repo):
    rng = _commit_file(
        repo, ALLOWLISTED_PATH,
        f"<hardwareVersion>{ALLOWLISTED_VALUE}-2.2</hardwareVersion>\n",
    )

    out = _run(repo, rng)

    assert ALLOWLISTED_VALUE not in out, (
        f"allowlisted value should be suppressed in {ALLOWLISTED_PATH}:\n{out}"
    )


def test_same_value_in_a_different_file_still_warns(repo):
    """The whole point of file-scoping: the allowlist confirms one specific
    occurrence, not the digits everywhere.
    """
    rng = _commit_file(
        repo, UNLISTED_PATH, f'HOST = "{ALLOWLISTED_VALUE}"\n',
    )

    out = _run(repo, rng)

    assert "WARN" in out and ALLOWLISTED_VALUE in out, (
        f"value outside its allowlisted path must still warn:\n{out}"
    )


def test_private_addresses_are_still_excluded(repo):
    """Regression guard on the refactor that introduced the allowlist: the
    private-range `case` gained a `continue`, so a mistake there would start
    warning on every RFC1918 address in the repo.
    """
    rng = _commit_file(repo, UNLISTED_PATH, 'HOST = "192.168.1.1"\n')

    out = _run(repo, rng)

    assert "192.168.1.1" not in out, f"private address must not warn:\n{out}"


def test_a_genuine_public_address_still_warns(repo):
    """The control still has to do its actual job."""
    rng = _commit_file(repo, UNLISTED_PATH, 'HOST = "203.0.113.7"\n')

    out = _run(repo, rng)

    assert "WARN" in out and "203.0.113.7" in out, f"expected a warning:\n{out}"


# --- Rule (a): vendor-prefixed secret formats ------------------------------

# Synthetic samples, each built to satisfy one pattern without being a real
# credential (fixed filler alphabet, obvious FAKE marker where it fits).
VENDOR_SECRETS = [
    "sk-ant-" + "A" * 30,
    "sk-or-v1-" + "b" * 30,
    "sk-" + "C" * 40,
    "AKIA" + "Z" * 16,
    "ghp_" + "d" * 36,
    "github_pat_" + "E" * 40,
    "xoxb-" + "f" * 20,
    "AIza" + "g" * 35,
    # split so the literal PEM header doesn't sit in this file's own source
    # and trip rule (a) at commit time -- the runtime value is still the full
    # header, which is what the fixture commits and the rule must catch.
    "-----BEGIN RSA " + "PRIVATE KEY-----",
]


@pytest.mark.parametrize("secret", VENDOR_SECRETS)
def test_vendor_prefixed_secret_fails_and_blocks(repo, secret):
    rng = _commit_file(repo, "config.py", f'KEY = "{secret}"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1, f"a known key format must block the push:\n{proc.stdout}"
    assert "FAIL" in proc.stdout


def test_vendor_secret_value_is_never_printed(repo):
    secret = "sk-ant-" + "A" * 30
    rng = _commit_file(repo, "config.py", f'KEY = "{secret}"\n')

    proc = _run_proc(repo, rng)

    assert secret not in proc.stdout, (
        "the secret's value must never reach stdout -- that's a second copy of "
        f"the thing we're containing:\n{proc.stdout}"
    )
    assert "config.py" in proc.stdout, "the location must be reported"


def test_script_does_not_match_its_own_patterns(repo):
    """Every scanned commit's tree contains the copy of check-pii.sh, whose
    source spells out all nine vendor patterns. Each is followed in the
    source by `[`, outside its own character class, so it can't match itself
    -- pin that, or the check would FAIL on any commit touching the script.
    """
    rng = _commit_file(repo, "note.txt", "nothing here\n")

    proc = _run_proc(repo, rng)

    assert proc.returncode == 0, f"the script must not flag its own source:\n{proc.stdout}"
    assert "0 FAIL(s)" in proc.stdout


# --- Rule (b): this machine's own .env values -----------------------------

def test_env_secret_value_in_a_commit_fails(repo):
    secret = "unifivalue" + "1" * 20
    _write_env(repo, f"UNIFI_API_KEY={secret}\n")
    rng = _commit_file(repo, "leaked.py", f'k = "{secret}"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1
    assert "UNIFI_API_KEY" in proc.stdout
    assert secret not in proc.stdout, f"the .env value must be withheld:\n{proc.stdout}"


def test_structured_pii_value_in_a_commit_fails(repo):
    """A real home subnet is private-range, so the IP rule structurally can't
    see it; rule (b) catches it via the *SUBNET* key name.
    """
    subnet = "10.77.3.0/24"
    _write_env(repo, f"SCAN_SUBNETS={subnet}\n")
    rng = _commit_file(repo, "leaked.py", f'net = "{subnet}"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1
    assert "SCAN_SUBNETS" in proc.stdout
    assert subnet not in proc.stdout


def test_non_secret_key_value_is_not_scanned(repo):
    """DATABASE_URL's name doesn't mark it sensitive, so its embedded password
    (which appears all over the repo) never becomes a needle.
    """
    _write_env(
        repo,
        "DATABASE_URL=postgresql+psycopg://assetmgt:assetmgt@localhost:5432/assetmgt\n",
    )
    rng = _commit_file(repo, "db.py", 'URL = "postgresql://assetmgt:assetmgt@x/y"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 0, f"DATABASE_URL must not be scanned:\n{proc.stdout}"


def test_value_present_in_env_example_is_not_scanned(repo):
    """A value we publish in the tracked template is not a secret."""
    shared = "publishedplaceholder123"
    _write_env(repo, f"UNIFI_API_KEY={shared}\n")
    _write_env_example(repo, f"UNIFI_API_KEY={shared}\n")
    rng = _commit_file(repo, "somewhere.py", f'k = "{shared}"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 0, f"a published value must not FAIL:\n{proc.stdout}"


def test_short_env_value_warns_but_does_not_fail(repo):
    _write_env(repo, "NVD_API_KEY=short\n")
    rng = _commit_file(repo, "somewhere.py", 'k = "short"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 0
    assert "WARN" in proc.stdout and "NVD_API_KEY" in proc.stdout


def test_owner_name_key_is_deliberately_not_scanned(repo):
    """DEFAULT_OWNER is excluded on purpose -- literal-grepping a household
    name reproduces the verb-vs-name collision that killed the old
    machine-wide guardrail. Names belong in .pii-denylist.
    """
    _write_env(repo, "DEFAULT_OWNER=Jordan Lee Alexander\n")
    rng = _commit_file(repo, "prose.py", '# thanks to Jordan Lee Alexander\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 0, f"owner name must not be auto-scanned:\n{proc.stdout}"


# --- Rule (c): a tracked .env file ----------------------------------------

def _commit_tracked(repo: Path, relpath: str, content: str) -> str:
    """Force-commits relpath even if gitignored; returns the range for it."""
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", "-f", relpath)
    _git(repo, "commit", "-qm", f"add {relpath}")
    return "HEAD~1..HEAD"


def test_tracked_dotenv_fails(repo):
    rng = _commit_tracked(repo, ".env", "APP_ADMIN_PASSWORD=whatever\n")

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1
    assert ".env" in proc.stdout


@pytest.mark.parametrize("path", [".env.production", ".env.local", "config/.env"])
def test_dotenv_variants_fail(repo, path):
    rng = _commit_tracked(repo, path, "SECRET=x\n")

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1, f"{path} should be flagged as a tracked secrets file"


def test_env_example_is_allowed(repo):
    rng = _commit_file(repo, ".env.example", "APP_ADMIN_PASSWORD=change-me\n")

    proc = _run_proc(repo, rng)

    assert proc.returncode == 0, f".env.example is the tracked template:\n{proc.stdout}"


def test_clean_range_exits_zero(repo):
    rng = _commit_file(repo, "README.md", "# nothing sensitive here\n")

    proc = _run_proc(repo, rng)

    assert proc.returncode == 0
    assert "0 FAIL(s)" in proc.stdout


def test_unresolvable_range_fails_loudly_instead_of_reading_as_clean(repo):
    """Regression test: git rev-list on a range it can't resolve (e.g. a
    remote sha this checkout hasn't fetched -- exactly what the pre-push
    hook builds from) used to be indistinguishable from a genuinely empty
    range, since only the output was checked, never rev-list's exit status.
    That printed "ok ... nothing to check" and exited 0 -- a push going
    through with zero PII/secret scanning, looking like a pass."""
    proc = _run_proc(repo, "this-does-not-exist..HEAD")

    assert proc.returncode == 1, f"an unresolvable range must FAIL, not read as clean:\n{proc.stdout}"
    assert "FAIL" in proc.stdout
    assert "nothing to check" not in proc.stdout


def test_range_flag_missing_operand_fails_instead_of_hanging(repo):
    """Regression test: --range as the last argument used to make `shift 2`
    silently fail and return non-zero -- with no `set -e`, $# never reached
    0 and the argument-parsing loop spun forever. A timeout here is a
    correctness assertion, not just test hygiene: a hang would otherwise
    make this test itself hang instead of failing cleanly."""
    proc = subprocess.run(
        ["bash", "scripts/check-pii.sh", "--range"],
        cwd=repo, capture_output=True, text=True, timeout=10,
    )

    assert proc.returncode == 2
    assert "requires an argument" in proc.stderr


# --- Hex/MAC normalisation -------------------------------------------------
#
# These pin the miss that motivated the rule: the literal denylist grep is
# separator- and length-sensitive, so a MAC written one way in the denylist
# never matched the same MAC written another way in source. The fixture repo
# ships no .pii-denylist, so each test writes one (gitignored, like the real
# machine's) as the rule's needle source.

def _write_denylist(repo: Path, *terms: str) -> None:
    """Writes a gitignored .pii-denylist -- the script's known-value source.
    Appended to .gitignore so _commit_file's `git add -A` never commits it
    (which would move the needle into the tree and confuse what's being
    asserted)."""
    gi = repo / ".gitignore"
    gi.write_text(gi.read_text() + ".pii-denylist\n")
    (repo / ".pii-denylist").write_text("\n".join(terms) + "\n")


def test_denylisted_mac_matches_despite_different_format(repo):
    """A denylist entry in colon form must catch the same MAC embedded, without
    separators and with a suffix, in a RINCON id -- the exact shape that slipped
    a real device id through the literal grep."""
    _write_denylist(repo, "DE:AD:BE:EF:CA:FE")
    rng = _commit_file(repo, "probes/thing.py", 'uuid = "RINCON_DEADBEEFCAFE01400"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1, f"a normalised MAC must be caught:\n{proc.stdout}"
    assert "probes/thing.py" in proc.stdout


def test_hex_mac_value_is_never_printed(repo):
    """Consistent with the secret rules: report the location, never re-emit the
    matched value into logs."""
    _write_denylist(repo, "DE:AD:BE:EF:CA:FE")
    rng = _commit_file(repo, "probes/thing.py", 'mac = "de:ad:be:ef:ca:fe"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1
    assert "de:ad:be:ef:ca:fe" not in proc.stdout, (
        f"the matched MAC must be withheld:\n{proc.stdout}"
    )
    assert "value withheld" in proc.stdout


def test_denylist_term_without_trailing_newline_still_matches(repo):
    """Regression test: `while IFS= read -r line; do ... done < file` drops
    a file's last line when it has no trailing newline (`read` returns
    non-zero on EOF-without-newline). A hand-edited, gitignored
    .pii-denylist is exactly the kind of file that can end up without one --
    and the most recently added term (added *because* it just leaked) is
    the one most likely to be on that last line."""
    gi = repo / ".gitignore"
    gi.write_text(gi.read_text() + ".pii-denylist\n")
    (repo / ".pii-denylist").write_bytes(b"Jordan Lee\nnewest-real-secret-value")  # no trailing \n
    rng = _commit_file(repo, "probes/thing.py", 'token = "newest-real-secret-value"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1, f"the newline-less last term must still be caught:\n{proc.stdout}"
    assert "probes/thing.py" in proc.stdout


def test_short_denylist_entry_never_becomes_a_mac_needle(repo):
    """An entry that reduces to < 12 hex chars (a name, an address) must not
    turn into a MAC needle, or the rule could FAIL on unrelated hex strings."""
    _write_denylist(repo, "Jordan Lee")  # only a couple of hex chars survive
    rng = _commit_file(repo, "probes/thing.py", 'mac = "de:ad:be:ef:00:11"\n')

    proc = _run_proc(repo, rng)

    assert proc.returncode == 0, f"short entries must not become needles:\n{proc.stdout}"


# --- Commit-message scanning ----------------------------------------------
#
# Every other rule greps commit *trees*; a real name or secret living only in a
# commit *message* slipped through, and --full then reported "clean". These pin
# that the message body is scanned with the denylist and the secret patterns,
# and -- deliberately -- that ordinary author metadata does not trip it.

def _commit_with_message(repo: Path, relpath: str, content: str, message: str) -> str:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return "HEAD~1..HEAD"


def test_denylist_term_in_commit_message_fails(repo):
    _write_denylist(repo, "Fabname Fakesurname")
    rng = _commit_with_message(
        repo, "clean.py", "x = 1\n", "Fix the sync bug for Fabname Fakesurname",
    )

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1, f"a denylisted name in a message must FAIL:\n{proc.stdout}"
    assert "commit message" in proc.stdout


def test_secret_in_commit_message_fails_without_echo(repo):
    secret = "sk-ant-" + "A" * 30
    rng = _commit_with_message(repo, "clean.py", "x = 1\n", f"paste from debug run {secret}")

    proc = _run_proc(repo, rng)

    assert proc.returncode == 1, f"a key format in a message must FAIL:\n{proc.stdout}"
    assert secret not in proc.stdout, f"the secret must be withheld:\n{proc.stdout}"


def test_ordinary_commit_message_does_not_fail(repo):
    """No denylist, a mundane message -- the message pass must stay quiet, or it
    would fire on every commit (the cry-wolf failure the script guards against).
    """
    rng = _commit_with_message(repo, "clean.py", "x = 1\n", "Refactor the widget loader")

    proc = _run_proc(repo, rng)

    assert proc.returncode == 0, f"a benign message must not FAIL:\n{proc.stdout}"
    assert "0 FAIL(s)" in proc.stdout
