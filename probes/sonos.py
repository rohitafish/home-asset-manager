"""Sonos identification probe.

Sonos players run a small local HTTP/UPnP/SOAP server on TCP 1400 -- the
protocol details (endpoints, XML parsing) live in probes/sonos_api.py,
shared with discovery/sonos_household.py's whole-household enumeration.
This module is just the interactive, single-player identification probe:
applies_to()/run() and the suggestions built from what it finds.

All requests are read-only: nothing here ever plays, pauses, or changes
volume.
"""

from __future__ import annotations

import httpx

from probes.base import DEFAULT_TIMEOUT, ProbeOutcome
from probes.sonos_api import (
    fetch_device_description,
    fetch_status_zp,
    fetch_zone_group_state,
    parse_device_description,
    parse_status_zp,
    parse_zone_group_state,
)


def applies_to(asset, interfaces, services) -> bool:
    haystack = f"{asset.hostname or ''} {asset.vendor or ''}".lower()
    if "sonos" in haystack:
        return True
    return any(s.port == 1400 for s in services)


def run(ip: str, timeout: float = DEFAULT_TIMEOUT) -> ProbeOutcome:
    facts: dict = {}
    raw_parts: list[str] = []
    endpoints_ok: list[str] = []

    try:
        with httpx.Client(timeout=timeout) as client:
            xml_text = fetch_device_description(client, ip)
            if xml_text:
                facts.update(parse_device_description(xml_text))
                raw_parts.append(xml_text)
                endpoints_ok.append("device_description")

            soap_xml = fetch_zone_group_state(client, ip)
            if soap_xml:
                zgs_facts = parse_zone_group_state(soap_xml)
                facts.update(zgs_facts)
                raw_parts.append(soap_xml)
                if zgs_facts:
                    endpoints_ok.append("zone_group_topology")

            if "channel_map" not in facts:
                zp_xml = fetch_status_zp(client, ip)
                if zp_xml:
                    zp_facts = parse_status_zp(zp_xml)
                    facts.update({k: v for k, v in zp_facts.items() if k not in facts})
                    raw_parts.append(zp_xml)
                    endpoints_ok.append("status_zp")
    except Exception as exc:
        return ProbeOutcome(ok=False, summary=f"Could not connect to {ip}:1400 ({exc})")

    if not facts:
        return ProbeOutcome(
            ok=False, summary=f"No response from {ip}:1400 -- device may be off or not a Sonos player."
        )

    facts["endpoints_ok"] = endpoints_ok

    suggestions = []
    if facts.get("room_name"):
        suggestions.append(
            {
                "field": "position",
                "value": facts["room_name"],
                "reason": f"Sonos reports its own zone name as \"{facts['room_name']}\".",
            }
        )
    if facts.get("model") or facts.get("model_number"):
        suggestions.append(
            {
                "field": "model",
                "value": facts.get("model") or facts.get("model_number"),
                "reason": "From the device's own UPnP description.",
            }
        )
    if facts.get("software_version"):
        suggestions.append(
            {
                "field": "firmware_version",
                "value": facts["software_version"],
                "reason": "Sonos software version, from the device description.",
            }
        )

    channel = None
    udn = facts.get("udn")
    if udn and facts.get("channel_map"):
        # UDN is "uuid:RINCON_xxxxxxxxxxxx01400" -- the channel map is keyed
        # by the bare RINCON id.
        rincon = udn.split(":", 1)[-1]
        channel = facts["channel_map"].get(rincon)
    if channel:
        side = {"LF": "left (LF)", "RF": "right (RF)"}.get(channel, channel)
        summary = f"{facts.get('room_name', 'Sonos')} -- stereo pair, {side} channel"
    else:
        summary = facts.get("room_name", "Sonos player identified")

    return ProbeOutcome(ok=True, summary=summary, facts=facts, raw="\n---\n".join(raw_parts), suggestions=suggestions)


class SonosProbe:
    name = "sonos"
    description = "Reads a Sonos player's zone name and stereo-pair channel (left/right) over its local UPnP/SOAP API on port 1400."
    applies_to = staticmethod(applies_to)
    run = staticmethod(run)
    replaces_prior_results = False


PROBE = SonosProbe()
