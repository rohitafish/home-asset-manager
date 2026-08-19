"""Pins _run_nmap's subprocess/timeout handling in isolation from real nmap
or sudo -- see discovery/nmap_scan.py's _run_nmap docstring comment for the
bug this replaces: plain subprocess.run(..., timeout=...) doesn't actually
bound the caller's wait time on the use_sudo=True path, because killing the
`sudo` direct child can't kill the root `nmap` it spawned, and run()'s
post-timeout cleanup calls an UNTIMED communicate() that then blocks on that
orphan. These tests use `sleep`/`sh` as a stand-in child process -- they
exercise the same Popen/communicate/kill mechanics without needing a real
nmap binary or root privileges in CI.
"""

import subprocess
import time

import pytest

from discovery import nmap_scan


def test_run_nmap_returns_stdout_on_success(monkeypatch):
    monkeypatch.setattr(nmap_scan, "_require_nmap", lambda: "/bin/echo")
    out = nmap_scan._run_nmap(["hello"])
    assert out.strip() == "hello"


def test_run_nmap_raises_runtime_error_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(nmap_scan, "_require_nmap", lambda: "/bin/sh")
    with pytest.raises(RuntimeError, match="nmap failed"):
        nmap_scan._run_nmap(["-c", "echo boom >&2; exit 3"])


def test_run_nmap_bounds_wait_time_on_timeout(monkeypatch):
    """The core regression test: a child that runs long must not be allowed
    to block the caller beyond timeout + the (here, shortened) grace period,
    regardless of whether the kill takes effect instantly."""
    monkeypatch.setattr(nmap_scan, "_require_nmap", lambda: "/bin/sleep")
    monkeypatch.setattr(nmap_scan, "_KILL_GRACE_SECONDS", 1)

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        nmap_scan._run_nmap(["30"], timeout=1)
    elapsed = time.monotonic() - start

    # timeout(1) + grace(1) + generous scheduling slack -- nowhere near the
    # 30s the child was told to sleep for, which is what the pre-fix
    # untimed-communicate() path would have actually waited out.
    assert elapsed < 10


def test_run_nmap_kills_the_child_process(monkeypatch):
    """After a timeout, the direct child must actually be dead -- not just
    detached from -- confirming proc.kill() plus the bounded drain leaves no
    dangling process this module is still responsible for."""
    monkeypatch.setattr(nmap_scan, "_require_nmap", lambda: "/bin/sleep")
    monkeypatch.setattr(nmap_scan, "_KILL_GRACE_SECONDS", 1)

    with pytest.raises(subprocess.TimeoutExpired):
        nmap_scan._run_nmap(["30"], timeout=1)

    # subprocess.Popen doesn't hand back a reference here (by design -- the
    # function's return type is just the stdout string), so confirm via `ps`
    # that no leftover `sleep 30` from this test is still running.
    ps = subprocess.run(["pgrep", "-f", "sleep 30"], capture_output=True, text=True)
    assert ps.stdout.strip() == "", f"leftover sleep process(es): {ps.stdout!r}"
