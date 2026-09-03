"""ICMP reachability probe -- "is this thing even on right now?"

Unlike the other probes here, this identifies nothing about the device; it
answers a narrower, more general question that qualifies every other probe's
result. A Sonos probe reporting "no response" is ambiguous -- wrong protocol?
device asleep? wrong VLAN? -- until you know whether the host answers ICMP at
all. See registry.py for how this is threaded into every asset's probe run
without displacing the identification/SSDP-fallback logic.

Shells out to the system `ping` binary rather than opening a raw ICMP socket,
matching this codebase's existing preference (see discovery/nmap_scan.py) for
an explicit, auditable subprocess call over a library that hides what's
actually sent. On macOS this needs no elevated privileges -- `/sbin/ping` is
not setuid; the kernel grants ICMP via SOCK_DGRAM to any user -- so, unlike
nmap, no elevated privileges are required (see README).

A silent host is NOT proof the device is off: plenty of IoT gear and
Wi-Fi clients in power-save mode never answer ICMP, and this network has
multiple VLANs a probe might not be able to reach. Summaries are worded to
reflect that ambiguity rather than asserting the device is down.
"""

import re
import shutil
import subprocess

from probes.base import DEFAULT_TIMEOUT, ProbeOutcome

_RTT_RE = re.compile(r"time=([\d.]+)\s*ms")
_TTL_RE = re.compile(r"ttl=(\d+)")
_LOSS_RE = re.compile(r"([\d.]+)% packet loss")


def applies_to(asset, interfaces, services) -> bool:
    # No network I/O, no vendor/hostname sniffing needed -- reachability is
    # worth checking for literally any asset with a known IP.
    return any(i.ip for i in interfaces)


def run(ip: str, timeout: float = DEFAULT_TIMEOUT) -> ProbeOutcome:
    if ":" in ip:
        # Discovery is IPv4-only today (see discovery/nmap_scan.py /
        # SCAN_SUBNETS) -- an IPv6 literal here would mean `ping` silently
        # tries to resolve it as a hostname instead. Fail clearly.
        return ProbeOutcome(ok=False, summary=f"Skipped: {ip} looks like IPv6, not supported by this probe.")

    ping_path = shutil.which("ping")
    if not ping_path:
        return ProbeOutcome(ok=False, summary="`ping` binary not found on PATH.")

    # -c 1: a single echo request -- this is a quick liveness check, not a
    # loss/jitter measurement. -W is milliseconds on macOS (seconds on Linux
    # -- this app is macOS-only, see README, so don't "fix" this for Linux).
    # -n: skip reverse DNS so a slow/broken resolver can't stall the request.
    #
    # Empirically (macOS 15/26, /sbin/ping), a no-reply host takes roughly
    # `waittime + 1000ms` wall-clock to return, not just `waittime` -- e.g.
    # -W 1000 took ~2.0s, -W 2000 took ~3.0s. The subprocess timeout below
    # needs enough headroom over that or a slow-but-legitimate "no reply"
    # gets killed as a TimeoutExpired instead of ping's own clean exit.
    timeout_ms = max(1, int(timeout * 1000))
    try:
        result = subprocess.run(
            [ping_path, "-c", "1", "-W", str(timeout_ms), "-n", ip],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            check=False,
        )
    except Exception as exc:  # a probe must never raise -- see probes/base.py
        return ProbeOutcome(ok=False, summary=f"Could not run ping against {ip}: {exc}")

    output = (result.stdout or "") + (result.stderr or "")
    return _parse_ping_output(output, result.returncode, ip)


def _parse_ping_output(output: str, returncode: int, ip: str) -> ProbeOutcome:
    # Pure string -> ProbeOutcome transformation, split out from run() so it's
    # testable against a captured macOS `ping` transcript with no subprocess
    # at all -- see tests/test_ping.py, which is the first place this repo
    # keeps such a transcript on disk (tests/fixtures/), rather than as
    # another inline string constant.
    ok = returncode == 0

    if not ok:
        return ProbeOutcome(
            ok=False,
            summary=(
                f"No ICMP reply from {ip} -- the device may be powered off, "
                "asleep, filtering ICMP, or simply not reachable from this "
                "host (e.g. a different VLAN). Not proof it's down."
            ),
            facts={"packet_loss": "100%"},
            raw=output.strip() or None,
        )

    rtt_match = _RTT_RE.search(output)
    ttl_match = _TTL_RE.search(output)
    loss_match = _LOSS_RE.search(output)
    facts = {}
    if rtt_match:
        facts["rtt_ms"] = rtt_match.group(1)
    if ttl_match:
        facts["ttl"] = ttl_match.group(1)
    if loss_match:
        facts["packet_loss"] = f"{loss_match.group(1)}%"

    summary_bits = []
    if rtt_match:
        summary_bits.append(f"Replied in {rtt_match.group(1)} ms")
    else:
        summary_bits.append("Replied")
    if ttl_match:
        summary_bits.append(f"(ttl {ttl_match.group(1)})")
    summary = " ".join(summary_bits) + "."

    return ProbeOutcome(ok=True, summary=summary, facts=facts, raw=output.strip() or None)


class PingProbe:
    name = "ping"
    description = "Sends a single ICMP echo request to check whether the device is reachable right now. Identifies nothing; a no-reply is not proof the device is off."
    applies_to = staticmethod(applies_to)
    run = staticmethod(run)
    # A ping is cheap and expected to be re-run often -- keep only the latest
    # result per asset+IP rather than accumulating history the way
    # identification probes do (see registry.py / dashboard.py's probe runner).
    replaces_prior_results = True


PROBE = PingProbe()
