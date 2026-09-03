"""Runs nmap host discovery + service/version scans and parses the XML
output into the common discovery.normalize.normalize_nmap_hosts() shape.

Two phases, matching the plan:
  1. `-sn` ping-sweep across the configured subnets to find hosts that are up
     (this also gives us ARP-resolved MAC addresses for anything on the same
     L2 segment as the Mac running the scan).
  2. `-sV` service/version fingerprinting against the hosts found up.

Raw SYN scans and OS detection need root; default here is `-sT` (TCP connect,
no privileges required) with an opt-in `use_sudo=True` for a fuller `-sS` scan.
The sudo path is CLI-only (`python -m discovery.cli nmap --sudo`, run from a
terminal that can answer sudo's password prompt): there is deliberately no
web route for it and no passwordless sudoers rule. A NOPASSWD rule on a
Homebrew-installed nmap is a root escalation for anything running as the
app user, because /opt/homebrew/bin and the binary itself are user-owned --
swap the binary, run it as root. See README's "Nmap privileges". This module
shells out to the `nmap` binary rather than depending on python-nmap so the
exact command run is explicit and auditable.
"""

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET  # Element type for annotations only

from defusedxml.ElementTree import fromstring  # safe parse of nmap's XML output


class NmapNotFoundError(RuntimeError):
    pass


def _require_nmap() -> str:
    path = shutil.which("nmap")
    if not path:
        raise NmapNotFoundError(
            "nmap binary not found on PATH; install it with `brew install nmap`"
        )
    return path


# Bounded grace period for draining output after a kill -- see _run_nmap.
_KILL_GRACE_SECONDS = 10


def _stdin_is_tty() -> bool:
    """Split out so tests can pin both branches of _sudo_prefix without a
    real terminal."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _sudo_prefix() -> list[str]:
    """argv prefix for a privileged run. On a terminal, plain `sudo` so it can
    prompt for the password (the supported way to get an -sS scan). Anywhere
    else -- launchd, a web request, a pipe -- `sudo -n` so a missing
    credential fails fast with a clear error instead of hanging forever on a
    prompt nobody can answer. No web route calls this any more; the -n branch
    is the backstop for a non-interactive CLI invocation."""
    return ["sudo"] if _stdin_is_tty() else ["sudo", "-n"]


def _run_nmap(args: list[str], use_sudo: bool = False, timeout: int = 1800) -> str:
    nmap_path = _require_nmap()
    cmd = [*_sudo_prefix(), nmap_path, *args] if use_sudo else [nmap_path, *args]
    # Not plain subprocess.run(..., timeout=timeout): on a timeout, run()
    # kills only the direct child and then calls an UNTIMED communicate() to
    # drain output before re-raising. With use_sudo=True the direct child is
    # `sudo`, which cannot propagate SIGKILL to the root `nmap` it spawned --
    # that orphaned root process keeps the inherited stdout/stderr pipes
    # open, so the untimed communicate() blocks until it exits on its own,
    # giving `timeout` no actual effect on this path. A caller in the
    # request path (a sync FastAPI route, run in the shared threadpool)
    # would then hang indefinitely instead of failing after `timeout`
    # seconds, and enough stuck requests exhaust the pool and stop the app
    # serving anything.
    #
    # start_new_session=True + an explicit, bounded second communicate()
    # caps the worst case at timeout + _KILL_GRACE regardless of what the
    # orphaned nmap does. It does NOT fix the orphan itself -- killing a
    # root child from here would need its own sudoers rule (sudo kill),
    # which is a separate, undone piece of hardening; this only guarantees
    # the caller stops waiting.
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass  # gave it a bounded grace period; move on regardless
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"nmap failed ({proc.returncode}): {(stderr or '').strip()}")
    return stdout


def _parse_host_addresses(host_el: ET.Element) -> dict:
    ip = None
    mac = None
    vendor = None
    for addr in host_el.findall("address"):
        addrtype = addr.get("addrtype")
        if addrtype == "ipv4":
            ip = addr.get("addr")
        elif addrtype == "mac":
            mac = addr.get("addr")
            vendor = addr.get("vendor")
    hostname = None
    hostnames_el = host_el.find("hostnames")
    if hostnames_el is not None:
        name_el = hostnames_el.find("hostname")
        if name_el is not None:
            hostname = name_el.get("name")
    return {"ip": ip, "mac": mac, "vendor": vendor, "hostname": hostname}


def ping_sweep(subnets: list[str], use_sudo: bool = False) -> list[dict]:
    """Returns hosts found up: [{ip, mac, vendor, hostname}]."""
    # -T4: "Aggressive" timing -- safe to assume a reliable, low-latency
    # network for a home LAN, and meaningfully faster than the default.
    args = ["-sn", "-T4", "-oX", "-", *subnets]
    xml_out = _run_nmap(args, use_sudo=use_sudo)
    root = fromstring(xml_out)
    hosts = []
    for host_el in root.findall("host"):
        status = host_el.find("status")
        if status is None or status.get("state") != "up":
            continue
        hosts.append(_parse_host_addresses(host_el))
    return hosts


def service_scan(
    ips: list[str], top_ports: int = 1000, use_sudo: bool = False
) -> list[dict]:
    """Returns hosts with detected services:
    [{ip, mac, vendor, hostname, services: [{port, protocol, product, version, banner}]}]
    """
    if not ips:
        return []
    scan_type = "-sS" if use_sudo else "-sT"
    args = [scan_type, "-sV", "-T4", f"--top-ports={top_ports}", "-oX", "-", *ips]
    xml_out = _run_nmap(args, use_sudo=use_sudo)
    root = fromstring(xml_out)
    hosts = []
    for host_el in root.findall("host"):
        status = host_el.find("status")
        if status is None or status.get("state") != "up":
            continue
        base = _parse_host_addresses(host_el)
        services = []
        ports_el = host_el.find("ports")
        if ports_el is not None:
            for port_el in ports_el.findall("port"):
                state_el = port_el.find("state")
                if state_el is None or state_el.get("state") != "open":
                    continue
                service_el = port_el.find("service")
                product = service_el.get("product") if service_el is not None else None
                version = service_el.get("version") if service_el is not None else None
                name = service_el.get("name") if service_el is not None else None
                extrainfo = service_el.get("extrainfo") if service_el is not None else None
                banner = " ".join(filter(None, [name, extrainfo]))
                services.append(
                    {
                        "port": int(port_el.get("portid")),
                        "protocol": port_el.get("protocol", "tcp"),
                        "product": product,
                        "version": version,
                        "banner": banner or None,
                    }
                )
        base["services"] = services
        hosts.append(base)
    return hosts


def discover_network(
    subnets: list[str], use_sudo: bool = False, top_ports: int = 1000
) -> list[dict]:
    """Full two-phase discovery: ping sweep, then service scan on hosts up."""
    up_hosts = ping_sweep(subnets, use_sudo=use_sudo)
    ips = [h["ip"] for h in up_hosts if h["ip"]]
    scanned = service_scan(ips, top_ports=top_ports, use_sudo=use_sudo)
    scanned_by_ip = {h["ip"]: h for h in scanned if h["ip"]}

    merged = []
    for host in up_hosts:
        if host["ip"] in scanned_by_ip:
            enriched = scanned_by_ip[host["ip"]]
            enriched["mac"] = enriched.get("mac") or host.get("mac")
            enriched["vendor"] = enriched.get("vendor") or host.get("vendor")
            enriched["hostname"] = enriched.get("hostname") or host.get("hostname")
            merged.append(enriched)
        else:
            host["services"] = []
            merged.append(host)
    return merged
