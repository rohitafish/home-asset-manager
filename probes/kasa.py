"""TP-Link Kasa (legacy) identification probe.

Legacy Kasa smart plugs/switches/bulbs expose a JSON control API on TCP 9999,
obfuscated (not encrypted -- there's no key exchange, no credentials) with a
simple rolling XOR cipher nicknamed "autokey": each plaintext byte is XORed
with a key that starts at 171 and becomes the *previous ciphertext byte* as
you go. A 4-byte big-endian length prefix precedes the ciphertext on TCP
(the UDP broadcast-discovery variant has no such prefix -- not used here).

We only ever send one command: {"system":{"get_sysinfo":{}}} -- a read-only
query. There is no actuation command anywhere in this module, deliberately:
this app never toggles a plug.

Important limitation: this is the *legacy* Kasa protocol. Newer Kasa
firmware and all Tapo devices (P100/P110/...) speak KLAP instead -- an
HTTP-based, cloud-credential-authenticated handshake on port 80/20002 -- and
will simply not answer on 9999 at all. When port 9999 is closed we report
that plainly rather than pretending the device isn't there.
"""

import json
import socket

from probes.base import DEFAULT_TIMEOUT, ProbeOutcome

_INITIAL_KEY = 171
_ALLOWED_REQUEST = {"system": {"get_sysinfo": {}}}

# Fields that leak the geocoded home location (set during Kasa app
# onboarding) or cloud account identifiers -- strip before persisting
# anything, per this repo's PII policy (see git history: "Remove remaining
# PII before public release").
_REDACT_KEYS = {
    "latitude_i", "longitude_i", "latitude", "longitude", "deviceId", "hwId", "oemId",
}


def _encrypt(plaintext: bytes) -> bytes:
    key = _INITIAL_KEY
    out = bytearray()
    for b in plaintext:
        key = b ^ key
        out.append(key)
    return len(out).to_bytes(4, "big") + bytes(out)


def _decrypt(ciphertext: bytes) -> bytes:
    key = _INITIAL_KEY
    out = bytearray()
    for c in ciphertext:
        out.append(c ^ key)
        key = c
    return bytes(out)


def _redact(obj):
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items() if k not in _REDACT_KEYS}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def applies_to(asset, interfaces, services) -> bool:
    haystack = f"{asset.hostname or ''} {asset.vendor or ''}".lower()
    if "tp-link" in haystack or "kasa" in haystack:
        return True
    if "tapo" in haystack or "plug" in haystack:
        return True
    return any(s.port == 9999 for s in services)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed early")
        buf.extend(chunk)
    return bytes(buf)


def run(ip: str, timeout: float = DEFAULT_TIMEOUT) -> ProbeOutcome:
    request = _encrypt(json.dumps(_ALLOWED_REQUEST).encode())
    try:
        with socket.create_connection((ip, 9999), timeout=timeout) as sock:
            sock.sendall(request)
            length = int.from_bytes(_recv_exact(sock, 4), "big")
            if length <= 0 or length > 65536:
                return ProbeOutcome(ok=False, summary="Unexpected response length from port 9999.")
            body = _recv_exact(sock, length)
    except (TimeoutError, ConnectionRefusedError, OSError):
        return ProbeOutcome(
            ok=False,
            summary=(
                f"No response on {ip}:9999 -- likely a Tapo device or newer Kasa "
                "firmware speaking KLAP (HTTP, cloud-credential auth) instead of "
                "the legacy protocol this probe supports."
            ),
            facts={"protocol": "klap_or_unknown"},
        )

    try:
        payload = json.loads(_decrypt(body).decode())
    except (ValueError, UnicodeDecodeError):
        return ProbeOutcome(ok=False, summary="Could not decode response from port 9999.")

    sysinfo = payload.get("system", {}).get("get_sysinfo", {})
    if not sysinfo:
        return ProbeOutcome(ok=False, summary="Device responded but sent no sysinfo.")

    sysinfo = _redact(sysinfo)
    facts = {
        "alias": sysinfo.get("alias"),
        "model": sysinfo.get("model"),
        "dev_name": sysinfo.get("dev_name"),
        "sw_ver": sysinfo.get("sw_ver"),
        "hw_ver": sysinfo.get("hw_ver"),
        "mac": sysinfo.get("mac") or sysinfo.get("mic_mac"),
        "relay_state": sysinfo.get("relay_state"),
        "on_time": sysinfo.get("on_time"),
        "rssi": sysinfo.get("rssi"),
        "protocol": "kasa_legacy_9999",
    }
    if sysinfo.get("children"):
        facts["children"] = [
            {"id": c.get("id"), "alias": c.get("alias"), "state": c.get("state")}
            for c in sysinfo["children"]
        ]
    facts = {k: v for k, v in facts.items() if v is not None}

    suggestions = []
    if facts.get("alias"):
        suggestions.append(
            {
                "field": "position",
                "value": facts["alias"],
                "reason": f"The Kasa app alias for this plug is \"{facts['alias']}\" -- often already the socket/room name.",
            }
        )
    if facts.get("model"):
        suggestions.append({"field": "model", "value": facts["model"], "reason": "Reported by the device."})
    if facts.get("sw_ver"):
        suggestions.append(
            {"field": "firmware_version", "value": facts["sw_ver"], "reason": "Reported by the device."}
        )

    summary = facts.get("alias") or facts.get("model") or "TP-Link Kasa device identified"
    return ProbeOutcome(
        ok=True, summary=summary, facts=facts, raw=json.dumps(sysinfo, indent=2), suggestions=suggestions
    )


class KasaProbe:
    name = "kasa"
    description = "Reads a legacy TP-Link Kasa plug/switch's app-set alias and model over its local port-9999 API. Does not work on Tapo or newer KLAP-only firmware."
    applies_to = staticmethod(applies_to)
    run = staticmethod(run)
    replaces_prior_results = False


PROBE = KasaProbe()
