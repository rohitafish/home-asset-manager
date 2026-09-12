"""scripts/hooks/pre-push: the range it hands to scripts/check-pii.sh (and
to gitleaks), and that a failing scan blocks the push.

The hook is copied into a throwaway repo's .git/hooks/ (it resolves the repo
from ${BASH_SOURCE[0]}/../..), check-pii.sh is replaced by a stub that
records its arguments, and a no-op `gitleaks` is put first on PATH so the
fail-closed "not installed" branch does not fire. No .venv exists there, so
the pytest/ruff steps report themselves skipped and do not run.

What is pinned: a branch the remote has never seen (remote sha all zeros)
is scanned as `<sha> --not --remotes=<remote>` -- only what the remote
lacks -- rather than the bare `<sha>` the hook once passed, which rev-list
expands to every ancestor and so cost a full-history scan per new branch.
"""

import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "hooks" / "pre-push"
ZERO = "0" * 40


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "README.md").write_text("hi\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "first")

    hook = work / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(HOOK.read_text())
    hook.chmod(0o755)

    (work / "scripts").mkdir()
    stub = work / "scripts" / "check-pii.sh"
    stub.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$(dirname "$0")/../check-pii.log"\n'
        'exit "${CHECK_PII_EXIT:-0}"\n'
    )
    stub.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gitleaks = fake_bin / "gitleaks"
    gitleaks.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$(pwd)/gitleaks.log"\nexit 0\n')
    gitleaks.chmod(0o755)
    return work


def _run_hook(repo: Path, stdin: str, **env: str) -> subprocess.CompletedProcess:
    merged = {**os.environ, "PATH": f"{repo.parent / 'bin'}:{os.environ['PATH']}", **env}
    return subprocess.run(
        ["bash", ".git/hooks/pre-push", "origin", "file:///nowhere"],
        cwd=repo, input=stdin, capture_output=True, text=True, env=merged,
    )


def _scan_args(repo: Path) -> str:
    return (repo / "check-pii.log").read_text()


def test_a_new_branch_scans_only_what_the_remote_lacks(repo):
    sha = _git(repo, "rev-parse", "HEAD")

    proc = _run_hook(repo, f"refs/heads/feature {sha} refs/heads/feature {ZERO}\n")

    assert proc.returncode == 0, proc.stderr
    assert f"--range {sha} --not --remotes=origin" in _scan_args(repo)
    assert f"--log-opts={sha} --not --remotes=origin" in (repo / "gitleaks.log").read_text()


def test_an_existing_branch_scans_the_two_dot_range(repo):
    old = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("more\n")
    _git(repo, "commit", "-qam", "second")
    new = _git(repo, "rev-parse", "HEAD")

    proc = _run_hook(repo, f"refs/heads/main {new} refs/heads/main {old}\n")

    assert proc.returncode == 0, proc.stderr
    assert f"--range {old}..{new}" in _scan_args(repo)


def test_a_branch_deletion_is_not_scanned(repo):
    proc = _run_hook(repo, f"(delete) {ZERO} refs/heads/gone abcdef0123456789abcdef0123456789abcdef01\n")

    assert proc.returncode == 0, proc.stderr
    assert not (repo / "check-pii.log").exists()


def test_a_failing_scan_blocks_the_push(repo):
    sha = _git(repo, "rev-parse", "HEAD")

    proc = _run_hook(repo, f"refs/heads/feature {sha} refs/heads/feature {ZERO}\n", CHECK_PII_EXIT="1")

    assert proc.returncode == 1
    assert "push blocked" in proc.stderr
