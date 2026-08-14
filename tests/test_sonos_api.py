"""Tests for probes/sonos_api.py -- pure XML parsing only. No HTTP is ever
exercised here (fetch_* are thin, never-raising wrappers with nothing to
unit test beyond what httpx itself already guarantees); all fixtures use
fabricated RINCON ids/MACs, never a real device's.

parse_zone_group_state's output is asserted to be unchanged by the
probes/sonos.py refactor -- it's now implemented on top of
parse_zone_group_members() instead of its own XML walk, and this is the
characterization test pinning that the two are equivalent.
"""

import pytest

from probes.sonos_api import (
    SonosPlayer,
    _is_fetchable_lan_ip,
    mac_from_rincon,
    parse_device_description,
    parse_status_zp,
    parse_zone_group_members,
    parse_zone_group_state,
)

# -- parse_device_description ------------------------------------------------

_DEVICE_DESCRIPTION_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <roomName>Living Room</roomName>
    <displayName>Playbar</displayName>
    <modelName>Sonos Playbar</modelName>
    <modelNumber>S9</modelNumber>
    <serialNum>AA-BB-CC-DD-EE-FF:1</serialNum>
    <softwareVersion>86.8-78270</softwareVersion>
    <hardwareVersion>1.9.1.10-2.2</hardwareVersion>
    <MACAddress>AA:BB:CC:DD:EE:FF</MACAddress>
    <UDN>uuid:RINCON_AABBCCDDEEFF01400</UDN>
  </device>
</root>
"""


def test_parse_device_description_extracts_identity():
    facts = parse_device_description(_DEVICE_DESCRIPTION_XML)
    assert facts["room_name"] == "Living Room"
    assert facts["model"] == "Sonos Playbar"
    assert facts["model_number"] == "S9"
    assert facts["serial"] == "AA-BB-CC-DD-EE-FF:1"
    assert facts["software_version"] == "86.8-78270"
    assert facts["mac"] == "AA:BB:CC:DD:EE:FF"
    assert facts["udn"] == "uuid:RINCON_AABBCCDDEEFF01400"


def test_parse_device_description_missing_fields_are_absent():
    facts = parse_device_description("<root><device><roomName>Kitchen</roomName></device></root>")
    assert facts == {"room_name": "Kitchen"}


# -- mac_from_rincon ----------------------------------------------------------


def test_mac_from_rincon_valid():
    assert mac_from_rincon("RINCON_AABBCCDDEEFF01400") == "aa:bb:cc:dd:ee:ff"


def test_mac_from_rincon_with_uuid_prefix():
    assert mac_from_rincon("uuid:RINCON_AABBCCDDEEFF01400") == "aa:bb:cc:dd:ee:ff"


# -- _is_fetchable_lan_ip (SSRF guard for the device-supplied Location host) --


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "172.16.4.9"])
def test_fetchable_lan_ip_accepts_private_addresses(host):
    assert _is_fetchable_lan_ip(host) is True


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "127.0.0.1",          # loopback -- a hostile Location pointing at the app itself
        "169.254.169.254",    # link-local / cloud metadata endpoint
        "8.8.8.8",            # public
        "sonos.example.com",  # a hostname, not a bare LAN IP
        "not-an-ip",
    ],
)
def test_fetchable_lan_ip_rejects_non_lan_hosts(host):
    assert _is_fetchable_lan_ip(host) is False


def test_mac_from_rincon_rejects_non_rincon():
    assert mac_from_rincon("uuid:something-else") is None


def test_mac_from_rincon_rejects_short_hex():
    assert mac_from_rincon("RINCON_AABB01400") is None


# -- parse_zone_group_members / parse_zone_group_state ------------------------

# A Playbar (visible member) bonded to a Sub and two Play:1 satellites --
# structurally the same shape captured live earlier this session, with
# fabricated RINCON ids/IPs. Each RINCON id is 12 hex chars + a fixed
# "01400" suffix, kept identical everywhere that id appears (its own
# UUID attribute and every HTSatChanMapSet entry naming it) so the fixture
# is consistent by construction, not by coincidence.
_COORD = "RINCON_AABBCCDDEEFF01400"
_SAT_LR = "RINCON_111111111111" "01400"
_SAT_RR = "RINCON_222222222222" "01400"
_SAT_SW = "RINCON_333333333333" "01400"
_COORD_HT_MAP = f"{_COORD}:LF,RF;{_SAT_LR}:LR;{_SAT_RR}:RR;{_SAT_SW}:SW"

_ZONE_GROUP_STATE_INNER = (
    "<ZoneGroups>"
    f'<ZoneGroup Coordinator="{_COORD}" ID="{_COORD}:1">'
    f'<ZoneGroupMember UUID="{_COORD}" '
    'Location="http://10.0.0.10:1400/xml/device_description.xml" ZoneName="Living Room" '
    f'HTSatChanMapSet="{_COORD_HT_MAP}" MicEnabled="0">'
    f'<Satellite UUID="{_SAT_LR}" '
    'Location="http://10.0.0.11:1400/xml/device_description.xml" ZoneName="Living Room" '
    f'HTSatChanMapSet="{_COORD_HT_MAP}" Invisible="1" MicEnabled="0"/>'
    f'<Satellite UUID="{_SAT_RR}" '
    'Location="http://10.0.0.12:1400/xml/device_description.xml" ZoneName="Living Room" '
    f'HTSatChanMapSet="{_COORD_HT_MAP}" Invisible="1" MicEnabled="0"/>'
    f'<Satellite UUID="{_SAT_SW}" '
    'Location="http://10.0.0.13:1400/xml/device_description.xml" ZoneName="Living Room" '
    f'HTSatChanMapSet="{_COORD_HT_MAP}" Invisible="1" MicEnabled="0"/>'
    "</ZoneGroupMember>"
    "</ZoneGroup>"
    "</ZoneGroups>"
)


def _wrap_soap(inner_xml: str) -> str:
    # ZoneGroupState is itself escaped XML nested inside the SOAP body --
    # ElementTree unescapes it once when we read .text, so the fixture must
    # be escaped once here to round-trip correctly.
    escaped = inner_xml.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        '<u:GetZoneGroupStateResponse xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
        f"<ZoneGroupState>{escaped}</ZoneGroupState>"
        "</u:GetZoneGroupStateResponse>"
        "</s:Body></s:Envelope>"
    )


_SOAP_XML = _wrap_soap(_ZONE_GROUP_STATE_INNER)


def test_parse_zone_group_members_returns_member_and_satellites():
    players = parse_zone_group_members(_SOAP_XML)

    assert len(players) == 4
    coordinator = next(p for p in players if p.uuid == _COORD)
    assert coordinator.is_satellite is False
    assert coordinator.room_name == "Living Room"
    assert coordinator.mac == "aa:bb:cc:dd:ee:ff"
    assert coordinator.ip == "10.0.0.10"
    assert coordinator.ht_channel == "LF,RF"

    satellites = [p for p in players if p.is_satellite]
    assert len(satellites) == 3
    assert all(p.invisible for p in satellites)
    sub = next(p for p in satellites if p.uuid == _SAT_SW)
    assert sub.ht_channel == "SW"
    assert sub.ip == "10.0.0.13"
    assert sub.mac is not None


def test_parse_zone_group_state_channel_map_unchanged():
    """Characterization test: parse_zone_group_state (used by
    probes/sonos.py) must return the exact same shape it always did, now
    that it's implemented on top of parse_zone_group_members()."""
    facts = parse_zone_group_state(_SOAP_XML)

    assert "ht_channel_map" in facts
    assert facts["ht_channel_map"][_COORD] == "LF,RF"
    assert facts["ht_channel_map"][_SAT_SW] == "SW"
    assert "channel_map" not in facts  # this fixture has no plain ChannelMapSet, only HT


def test_parse_zone_group_members_empty_on_no_zone_group_state():
    envelope = '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/></s:Envelope>'
    assert parse_zone_group_members(envelope) == []


# -- parse_status_zp -----------------------------------------------------------


def test_parse_status_zp_extracts_room_and_serial():
    xml = '<ZPSupportInfo><ZoneName>Kitchen</ZoneName><SerialNumber>AABBCCDDEEFF</SerialNumber></ZPSupportInfo>'
    facts = parse_status_zp(xml)
    assert facts["room_name"] == "Kitchen"
    assert facts["serial"] == "AABBCCDDEEFF"


def test_parse_status_zp_extracts_channel_map_raw_attribute():
    xml = '<Info ChannelMapSet="RINCON_X:LF,RF"/>'
    facts = parse_status_zp(xml)
    assert facts["channel_map_raw"] == "RINCON_X:LF,RF"


# -- SonosPlayer.ip ------------------------------------------------------------


def test_sonos_player_ip_parses_location_url():
    player = SonosPlayer(
        uuid="RINCON_X", room_name=None,
        location_url="http://10.0.0.42:1400/xml/device_description.xml",
        mac=None, channel=None, ht_channel=None, is_satellite=False, invisible=False,
    )
    assert player.ip == "10.0.0.42"


def test_sonos_player_ip_none_when_no_location():
    player = SonosPlayer(
        uuid="RINCON_X", room_name=None, location_url=None,
        mac=None, channel=None, ht_channel=None, is_satellite=False, invisible=False,
    )
    assert player.ip is None
