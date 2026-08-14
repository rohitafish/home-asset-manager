"""Generic SSDP/UPnP identification probe -- the fallback for anything not
covered by a more specific probe (Sonos, Kasa, ...).

Deliberately unicast, not multicast: SSDP's usual discovery mode broadcasts
to 239.255.255.250:1900, which does not cross VLAN boundaries -- and this
network has several (see discovery/normalize.py's per-VLAN gateway
handling). A multicast sweep run from wherever this app happens to be
deployed would silently see nothing on any other VLAN. Sending the M-SEARCH
directly to the asset's own IP on port 1900 routes normally regardless of
VLAN and is exactly as read-only as the broadcast form.
"""

import logging
import re
import socket
from urllib.parse import urlparse

import httpx

from probes.base import DEFAULT_TIMEOUT, ProbeOutcome

logger = logging.getLogger(__name__)

# Cap the LOCATION fetch: a UPnP device_description.xml is a few KB, and this is
# a device we don't trust (it's the host that just answered an M-SEARCH). Stop
# reading well before an unbounded/hostile body can exhaust memory.
_MAX_LOCATION_BYTES = 256 * 1024


def _safe_location_url(location: str, expected_ip: str) -> bool:
    """SSRF guard for the device-supplied LOCATION header. SSDP is the
    unconditional fallback probe, so this fetch runs against every unclaimed
    asset and the URL is entirely attacker-controlled -- without this, a
    hostile device can point LOCATION at http://127.0.0.1:8000/... or any other
    internal service and have the server fetch it. A UPnP device describes
    *itself*, so the only legitimate target is the IP we just probed, over
    http(s). Anything else -> don't fetch."""
    try:
        parsed = urlparse(location)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return parsed.hostname == expected_ip

_MSEARCH = (
    b"M-SEARCH * HTTP/1.1\r\n"
    b"HOST: {ip}:1900\r\n"
    b'MAN: "ssdp:discover"\r\n'
    b"MX: 1\r\n"
    b"ST: ssdp:all\r\n"
    b"\r\n"
)


def applies_to(asset, interfaces, services) -> bool:
    # Last-resort fallback -- the registry only tries this when no
    # more-specific probe claimed the asset (see registry.applicable_probes).
    return True


def _parse_headers(response: str) -> dict:
    headers = {}
    for line in response.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().upper()] = value.strip()
    return headers


def run(ip: str, timeout: float = DEFAULT_TIMEOUT) -> ProbeOutcome:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(_MSEARCH.replace(b"{ip}", ip.encode()), (ip, 1900))
        try:
            data, _ = sock.recvfrom(4096)
        except TimeoutError:
            return ProbeOutcome(ok=False, summary=f"No SSDP response from {ip}:1900.")
    except OSError as exc:
        return ProbeOutcome(ok=False, summary=f"Could not send SSDP request to {ip}:1900 ({exc}).")
    finally:
        sock.close()

    headers = _parse_headers(data.decode(errors="replace"))
    facts = {
        k.lower(): v
        for k, v in headers.items()
        if k in ("SERVER", "ST", "USN", "LOCATION")
    }

    location = headers.get("LOCATION")
    if location and _safe_location_url(location, ip):
        try:
            # follow_redirects stays False (httpx's default, pinned here for
            # intent) so a 3xx can't bounce this off the validated host onto an
            # internal one. Read at most _MAX_LOCATION_BYTES rather than trusting
            # the body size.
            with (
                httpx.Client(timeout=timeout) as client,
                client.stream("GET", location, follow_redirects=False) as resp,
            ):
                if resp.status_code == 200:
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) >= _MAX_LOCATION_BYTES:
                            break
                    text = body[:_MAX_LOCATION_BYTES].decode(errors="replace")
                    for tag, key in [
                        ("friendlyName", "friendly_name"),
                        ("manufacturer", "manufacturer"),
                        ("modelName", "model"),
                    ]:
                        match = re.search(rf"<{tag}>([^<]+)</{tag}>", text)
                        if match:
                            facts[key] = match.group(1).strip()
        except Exception:
            # LOCATION fetch is bonus detail on top of the M-SEARCH headers we
            # already have; an unreachable/slow device is expected here.
            logger.debug("SSDP LOCATION fetch from %s failed", location, exc_info=True)
    elif location:
        logger.debug("SSDP LOCATION %r from %s rejected by SSRF guard", location, ip)

    if not facts:
        return ProbeOutcome(ok=False, summary=f"No usable SSDP/UPnP data from {ip}.")

    suggestions = []
    if facts.get("friendly_name"):
        suggestions.append(
            {
                "field": "position",
                "value": facts["friendly_name"],
                "reason": f'UPnP friendlyName reported as "{facts["friendly_name"]}".',
            }
        )
    if facts.get("model"):
        suggestions.append({"field": "model", "value": facts["model"], "reason": "Reported by the device."})

    summary = facts.get("friendly_name") or facts.get("server") or "UPnP/SSDP device identified"
    return ProbeOutcome(ok=True, summary=summary, facts=facts, raw=str(headers), suggestions=suggestions)


class SsdpProbe:
    name = "ssdp"
    description = "Generic UPnP/SSDP identification (friendlyName, manufacturer, model) -- last-resort fallback probe."
    applies_to = staticmethod(applies_to)
    run = staticmethod(run)
    replaces_prior_results = False


PROBE = SsdpProbe()
