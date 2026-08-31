"""Sonos local API (port 1400): the UPnP/SOAP protocol parsing shared by
`probes/sonos.py` (the interactive, single-player identification probe) and
`discovery/sonos_household.py` (the collector that enumerates a whole
household from one reachable player). Protocol-only -- no `PROBE` instance,
no `applies_to`, no asset/DB knowledge. That's what keeps this importable
from `discovery/` without inverting the layering `probes/base.py` sets up
("probes are investigation aids, not collectors"): this module knows how to
talk to a Sonos player, nothing more, the same way `app/correlate.py`
already imports pure helpers out of `discovery/normalize.py` across that
same boundary in the other direction.

Three endpoints, tried in descending order of durability (Sonos has
progressively locked down the /status/* diagnostic pages on newer S2
firmware, so treat those as bonus data, never a dependency):

  1. GET /xml/device_description.xml -- standard UPnP device description.
     Gives us roomName (the user-assigned zone name), model, serial,
     firmware, MAC, and the player's UDN (its RINCON id).
  2. POST /ZoneGroupTopology/Control (SOAP action GetZoneGroupState) -- a
     read-only topology query (not an actuation call). Each ZoneGroupMember
     (and nested Satellite, for a bonded home-theatre set) carries a UUID
     (its RINCON id -- which is itself MAC-derived, see mac_from_rincon),
     ZoneName, and Location (that player's own device_description.xml URL,
     i.e. its current IP) -- enough to enumerate an entire household from
     a single call to any one reachable player. ChannelMapSet/
     HTSatChanMapSet is what answers "which is the left/right speaker" or
     "which satellite is this".
  3. GET /status/zp -- older diagnostic page, sometimes carries the same
     ChannelMapSet as a fallback when the SOAP call doesn't.

All three are read-only: nothing here ever plays, pauses, or changes volume.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass

import defusedxml.ElementTree as ET  # safe fromstring for device-supplied XML
import httpx

logger = logging.getLogger(__name__)


def _is_fetchable_lan_ip(host: str | None) -> bool:
    """The Location host in a GetZoneGroupState response is device-supplied, so
    enrich_from_device_description below would otherwise fetch
    http://<attacker-chosen-host>:1400/... -- a (port-locked) SSRF. A Sonos
    player is on the LAN and its Location always carries a numeric IP, so accept
    only a private, non-loopback, non-link-local address (the last two are what
    169.254.169.254 and 127.0.0.1 would be). Rejects hostnames and public IPs."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback and not ip.is_link_local

_SOAP_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>"
    '<u:GetZoneGroupState xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1"/>'
    "</s:Body></s:Envelope>"
)
_SOAP_HEADERS = {
    "Content-Type": 'text/xml; charset="utf-8"',
    "SOAPACTION": '"urn:schemas-upnp-org:service:ZoneGroupTopology:1#GetZoneGroupState"',
}


def _local_tag(elem) -> str:
    return elem.tag.rsplit("}", 1)[-1]


def _find_text(root, tag: str) -> str | None:
    for elem in root.iter():
        if _local_tag(elem) == tag and elem.text:
            return elem.text.strip()
    return None


def normalize_sonos_serial(serial: str | None) -> str | None:
    """A Sonos serial is the player's 12 hex-digit MAC plus one trailing
    check character, but the two places this app reads one from print it
    differently: the local API's device_description.xml gives
    "AA-BB-CC-DD-EE-FF:1" (dashes plus a colon before the check character),
    while /status/zp and the Sonos account page both give "AABBCCDDEEFF1"
    (no separators at all -- see discovery/account_import.py's
    sonos_serial_to_mac). Canonicalizes to that separator-less, uppercase
    form so every ingest path and every place a serial is displayed or
    diffed agrees. Returns the input unchanged, never guessing, on anything
    that isn't a 12-hex-digit-plus-one-character string once separators are
    stripped -- that's what keeps this safe to point at non-Sonos serials
    (Amazon's, Apple's, ...) that happen to pass through the same field."""
    if not serial:
        return serial
    stripped = re.sub(r"[-:\s]", "", serial).upper()
    if len(stripped) not in (12, 13):
        return serial
    if not re.fullmatch(r"[0-9A-F]{12}.?", stripped):
        return serial
    return stripped


def parse_device_description(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    facts = {}
    for tag, key in [
        ("roomName", "room_name"),
        ("displayName", "display_name"),
        ("modelName", "model"),
        ("modelNumber", "model_number"),
        ("serialNum", "serial"),
        ("softwareVersion", "software_version"),
        ("hardwareVersion", "hardware_version"),
        ("MACAddress", "mac"),
        ("UDN", "udn"),
    ]:
        value = _find_text(root, tag)
        if value:
            facts[key] = value
    if "serial" in facts:
        facts["serial"] = normalize_sonos_serial(facts["serial"])
    return facts


def parse_zone_group_state(soap_xml: str) -> dict:
    """Channel-map-only view used by probes/sonos.py -- kept byte-identical
    to what it always returned, now implemented on top of
    parse_zone_group_members() rather than duplicating the XML walk."""
    facts: dict = {}
    channel_map = {}
    ht_channel_map = {}
    for player in parse_zone_group_members(soap_xml):
        if player.channel:
            channel_map[player.uuid] = player.channel
        if player.ht_channel:
            ht_channel_map[player.uuid] = player.ht_channel
    if channel_map:
        facts["channel_map"] = channel_map
    if ht_channel_map:
        facts["ht_channel_map"] = ht_channel_map
    return facts


def parse_status_zp(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    facts = {}
    for tag, key in [("ZoneName", "room_name"), ("SerialNumber", "serial")]:
        value = _find_text(root, tag)
        if value:
            facts.setdefault(key, value)
    if "serial" in facts:
        facts["serial"] = normalize_sonos_serial(facts["serial"])
    for attr, key in [
        ("ChannelMapSet", "channel_map_raw"),
        ("HTSatChanMapSet", "ht_channel_map_raw"),
    ]:
        match = re.search(rf'{attr}="([^"]+)"', xml_text)
        if match:
            facts[key] = match.group(1)
    return facts


@dataclass
class SonosPlayer:
    """One player from a GetZoneGroupState response -- either a visible
    ZoneGroupMember or a bonded Satellite (a Sub, a rear surround). A
    satellite's own ZoneName is its *group's* room name, not evidence of
    its own identity -- see is_satellite before using room_name for
    anything naming-related."""

    uuid: str  # bare RINCON id, e.g. "RINCON_AABBCCDDEEFF01400" (fabricated)
    room_name: str | None
    location_url: str | None  # that player's own device_description.xml URL
    mac: str | None  # derived from uuid, see mac_from_rincon
    channel: str | None  # ChannelMapSet entry, e.g. "LF,RF"
    ht_channel: str | None  # HTSatChanMapSet entry, e.g. "LR" / "RR" / "SW"
    is_satellite: bool
    invisible: bool
    model: str | None = None
    model_number: str | None = None
    serial: str | None = None
    software_version: str | None = None

    @property
    def ip(self) -> str | None:
        if not self.location_url:
            return None
        # "http://172.16.1.97:1400/xml/device_description.xml" -> the host
        match = re.match(r"https?://([^:/]+)", self.location_url)
        return match.group(1) if match else None


def mac_from_rincon(uuid_or_udn: str) -> str | None:
    """A RINCON id is itself MAC-derived: "RINCON_AABBCCDDEEFF01400" (or
    prefixed "uuid:RINCON_...", as UDN is) -> "aa:bb:cc:dd:ee:ff" (fabricated
    example). Returns None rather than guessing on anything that doesn't fit
    the pattern."""
    tail = uuid_or_udn.rsplit(":", 1)[-1]
    if not tail.startswith("RINCON_"):
        return None
    hexpart = tail[len("RINCON_"):]
    hexonly = "".join(c for c in hexpart if c in "0123456789abcdefABCDEF")
    if len(hexonly) < 12:
        return None
    return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2)).lower()


def parse_zone_group_members(soap_xml: str) -> list[SonosPlayer]:
    envelope = ET.fromstring(soap_xml)
    inner_text = _find_text(envelope, "ZoneGroupState")
    if not inner_text:
        return []
    # The inner ZoneGroupState is itself XML, escaped inside the SOAP body.
    inner = ET.fromstring(inner_text)

    players: list[SonosPlayer] = []
    for member in inner.iter():
        tag = _local_tag(member)
        if tag not in ("ZoneGroupMember", "Satellite"):
            continue
        uuid = member.attrib.get("UUID")
        if not uuid:
            continue

        def _channel_for(uid: str, cms_attr: str) -> str | None:
            cms = member.attrib.get(cms_attr)
            if not cms:
                return None
            for entry in cms.split(";"):
                entry_uid, _, chan = entry.partition(":")
                if entry_uid == uid and chan:
                    return chan
            return None

        players.append(SonosPlayer(
            uuid=uuid,
            room_name=member.attrib.get("ZoneName"),
            location_url=member.attrib.get("Location"),
            mac=mac_from_rincon(uuid),
            channel=_channel_for(uuid, "ChannelMapSet"),
            ht_channel=_channel_for(uuid, "HTSatChanMapSet"),
            is_satellite=(tag == "Satellite"),
            invisible=member.attrib.get("Invisible") == "1",
        ))
    return players


def fetch_device_description(client: httpx.Client, ip: str) -> str | None:
    """Never raises -- a powered-off or unreachable player is a normal,
    expected outcome for a collector polling several IPs, not an error."""
    try:
        resp = client.get(f"http://{ip}:1400/xml/device_description.xml")
        if resp.status_code == 200:
            return resp.text
    except Exception:
        logger.debug("Sonos device_description fetch from %s failed", ip, exc_info=True)
    return None


def fetch_zone_group_state(client: httpx.Client, ip: str) -> str | None:
    try:
        resp = client.post(
            f"http://{ip}:1400/ZoneGroupTopology/Control", content=_SOAP_BODY, headers=_SOAP_HEADERS
        )
        if resp.status_code == 200:
            return resp.text
    except Exception:
        logger.debug("Sonos GetZoneGroupState from %s failed", ip, exc_info=True)
    return None


def fetch_status_zp(client: httpx.Client, ip: str) -> str | None:
    try:
        resp = client.get(f"http://{ip}:1400/status/zp")
        if resp.status_code == 200:
            return resp.text
    except Exception:
        logger.debug("Sonos status/zp fetch from %s failed", ip, exc_info=True)
    return None


def enrich_from_device_description(players: list[SonosPlayer], timeout: float = 3.0) -> None:
    """Best-effort per-player identity fetch (model/serial/firmware) --
    mutates players in place, filling in what device_description.xml gives
    that the topology call alone doesn't. A player that doesn't respond
    (powered off, or its Location URL has gone stale since the topology
    call) just keeps whatever GetZoneGroupState already gave it (uuid ->
    mac, room name) -- never raises, never drops a player for this."""
    with httpx.Client(timeout=timeout) as client:
        for player in players:
            if not _is_fetchable_lan_ip(player.ip):
                # No IP, or a device-supplied Location host that isn't a plain
                # LAN address -- skip enrichment rather than fetch an SSRF target.
                continue
            xml_text = fetch_device_description(client, player.ip)
            if not xml_text:
                continue
            try:
                facts = parse_device_description(xml_text)
            except Exception:
                logger.debug(
                    "Sonos device_description from %s was unparseable", player.ip, exc_info=True
                )
                continue
            player.model = facts.get("model")
            player.model_number = facts.get("model_number")
            player.serial = facts.get("serial")
            player.software_version = facts.get("software_version")
