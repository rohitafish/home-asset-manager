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


# -- sudo argv -----------------------------------------------------------------
# The privileged (-sS) path is CLI-only. There is no web route for it and no
# passwordless sudoers rule: on a Homebrew install /opt/homebrew/bin and the
# nmap binary are owned by the app user, so `NOPASSWD: /opt/homebrew/bin/nmap`
# is "run anything as root" for every process running as that user. The
# supported way is a terminal where sudo can prompt; off a terminal, -n keeps
# a missing credential from hanging forever on a prompt nobody can answer.


def test_sudo_prefix_prompts_on_a_terminal(monkeypatch):
    monkeypatch.setattr(nmap_scan, "_stdin_is_tty", lambda: True)
    assert nmap_scan._sudo_prefix() == ["sudo"]


def test_sudo_prefix_is_non_interactive_off_a_terminal(monkeypatch):
    monkeypatch.setattr(nmap_scan, "_stdin_is_tty", lambda: False)
    assert nmap_scan._sudo_prefix() == ["sudo", "-n"]


def test_stdin_is_tty_is_false_for_a_pipe(monkeypatch):
    import io

    monkeypatch.setattr(nmap_scan.sys, "stdin", io.StringIO())
    assert nmap_scan._stdin_is_tty() is False


def test_stdin_is_tty_is_false_when_stdin_is_closed(monkeypatch):
    class Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(nmap_scan.sys, "stdin", Closed())
    assert nmap_scan._stdin_is_tty() is False


def test_run_nmap_only_prefixes_sudo_when_asked(monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return ("<nmaprun/>", "")

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(nmap_scan.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(nmap_scan, "_require_nmap", lambda: "/fake/nmap")
    monkeypatch.setattr(nmap_scan, "_stdin_is_tty", lambda: False)

    nmap_scan._run_nmap(["-sn"], use_sudo=True)
    assert captured["cmd"] == ["sudo", "-n", "/fake/nmap", "-sn"]

    nmap_scan._run_nmap(["-sn"])
    assert captured["cmd"] == ["/fake/nmap", "-sn"], (
        "unprivileged runs must never touch sudo"
    )


# -- argv hygiene ---------------------------------------------------------------


def test_ipv4_only_keeps_literal_addresses_and_drops_the_rest(caplog):
    values = [
        "192.168.1.5",
        "--script=evil.nse",
        "printer.local",
        "fe80::1",
        None,
        "10.0.0.256",
        " 10.0.0.1",
    ]
    with caplog.at_level("WARNING"):
        kept = nmap_scan._ipv4_only(values)
    assert kept == ["192.168.1.5"]
    assert caplog.text.count("dropping non-IPv4 scan target") == 6


def test_discover_network_only_hands_ipv4_literals_to_the_service_scan(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        nmap_scan,
        "ping_sweep",
        lambda subnets, use_sudo=False: [
            {"ip": "192.168.1.9", "mac": None, "vendor": None, "hostname": None},
            {"ip": "-oN /tmp/x", "mac": None, "vendor": None, "hostname": None},
            {"ip": None, "mac": "aa:bb:cc:00:00:01", "vendor": None, "hostname": None},
        ],
    )

    def fake_service_scan(ips, top_ports=1000, use_sudo=False):
        seen["ips"] = list(ips)
        return []

    monkeypatch.setattr(nmap_scan, "service_scan", fake_service_scan)

    nmap_scan.discover_network(["192.168.1.0/24"])

    assert seen["ips"] == ["192.168.1.9"]


# ---- neighbour-table MAC fill-in (unprivileged nmap reports no MACs) ------


def _stub_run(stdout):
    class R:
        pass

    r = R()
    r.stdout = stdout
    return lambda *a, **k: r


def test_normalise_mac_pads_and_lowercases():
    assert (
        nmap_scan._normalise_mac("0:1a:2b:3c:4d:4") == "00:1a:2b:3c:4d:04"
    )  # macOS arp style
    assert nmap_scan._normalise_mac("00:1A:2B:3C:4D:01") == "00:1a:2b:3c:4d:01"
    assert nmap_scan._normalise_mac("0:1a:2b:3c:4d") is None  # 5 octets
    assert nmap_scan._normalise_mac("0:1a:2b:3c:4d:4:00") is None  # 7 octets
    assert nmap_scan._normalise_mac("ff:ff:ff:ff:ff:ff") is None
    assert nmap_scan._normalise_mac("01:00:5e:00:00:fb") is None  # multicast
    assert nmap_scan._normalise_mac("(incomplete)") is None


def test_neighbour_macs_linux_skips_failed_entries(monkeypatch):
    monkeypatch.setattr(nmap_scan.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        nmap_scan.subprocess,
        "run",
        _stub_run(
            "172.16.1.46 dev ens9 lladdr 00:1a:2b:3c:4d:01 REACHABLE\n"
            "172.16.1.18 dev ens9 lladdr 00:1a:2b:3c:4d:03 STALE\n"
            "172.16.1.99 dev ens9  FAILED\n"
            "172.16.1.98 dev ens9 lladdr 00:11:22:33:44:55 INCOMPLETE\n"
        ),
    )
    assert nmap_scan.neighbour_macs() == {
        "172.16.1.46": "00:1a:2b:3c:4d:01",
        "172.16.1.18": "00:1a:2b:3c:4d:03",
    }


def test_neighbour_macs_darwin_pads_octets_and_skips_incomplete(monkeypatch):
    monkeypatch.setattr(nmap_scan.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        nmap_scan.subprocess,
        "run",
        _stub_run(
            "? (172.16.1.19) at 0:1a:2b:3c:4d:2 on en0 ifscope [ethernet]\n"
            "? (172.16.1.77) at (incomplete) on en0 ifscope [ethernet]\n"
            "? (172.16.1.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]\n"
        ),
    )
    assert nmap_scan.neighbour_macs() == {"172.16.1.19": "00:1a:2b:3c:4d:02"}


def test_neighbour_macs_is_empty_off_linux_and_mac(monkeypatch):
    monkeypatch.setattr(nmap_scan.platform, "system", lambda: "Windows")
    assert nmap_scan.neighbour_macs() == {}


def test_neighbour_macs_never_raises(monkeypatch):
    monkeypatch.setattr(nmap_scan.platform, "system", lambda: "Linux")

    def boom(*a, **k):
        raise FileNotFoundError("ip")

    monkeypatch.setattr(nmap_scan.subprocess, "run", boom)
    assert nmap_scan.neighbour_macs() == {}


_SWEEP_XML = """<nmaprun>
<host><status state="up"/><address addr="172.16.1.46" addrtype="ipv4"/></host>
<host><status state="up"/><address addr="172.16.1.19" addrtype="ipv4"/>
  <address addr="00:1A:2B:3C:4D:02" addrtype="mac" vendor="Sonos"/></host>
<host><status state="down"/><address addr="172.16.1.50" addrtype="ipv4"/></host>
<host><status state="up"/><address addr="192.168.1.40" addrtype="ipv4"/></host>
</nmaprun>"""


def test_ping_sweep_fills_missing_macs_from_the_neighbour_table(monkeypatch):
    monkeypatch.setattr(nmap_scan, "_run_nmap", lambda *a, **k: _SWEEP_XML)
    monkeypatch.setattr(
        nmap_scan,
        "neighbour_macs",
        lambda: {
            "172.16.1.46": "00:1a:2b:3c:4d:01",
            "172.16.1.19": "ee:ee:ee:ee:ee:ee",
        },
    )
    by_ip = {h["ip"]: h for h in nmap_scan.ping_sweep(["172.16.1.0/24"])}
    assert by_ip["172.16.1.46"]["mac"] == "00:1a:2b:3c:4d:01"  # filled in
    assert by_ip["172.16.1.19"]["mac"] == "00:1A:2B:3C:4D:02"  # nmap's own wins
    assert by_ip["192.168.1.40"]["mac"] is None  # routed subnet: no ARP, stays MAC-less
    assert "172.16.1.50" not in by_ip


def test_ping_sweep_skips_the_neighbour_table_when_nmap_had_every_mac(monkeypatch):
    xml = _SWEEP_XML.replace(
        '<address addr="172.16.1.46" addrtype="ipv4"/>',
        '<address addr="172.16.1.46" addrtype="ipv4"/><address addr="00:1A:2B:3C:4D:01" addrtype="mac"/>',
    ).replace(
        '<host><status state="up"/><address addr="192.168.1.40" addrtype="ipv4"/></host>',
        "",
    )
    monkeypatch.setattr(nmap_scan, "_run_nmap", lambda *a, **k: xml)

    def never():
        raise AssertionError("neighbour table consulted needlessly")

    monkeypatch.setattr(nmap_scan, "neighbour_macs", never)
    assert all(h["mac"] for h in nmap_scan.ping_sweep(["172.16.1.0/24"]))
