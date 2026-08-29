"""Tests for probes/sonos.py -- the interactive single-player probe built on
top of probes/sonos_api.py's XML parsing (tested separately in
tests/test_sonos_api.py). fetch_device_description/fetch_zone_group_state/
fetch_status_zp are monkeypatched on the probes.sonos module object (they're
imported by name via `from probes.sonos_api import ...`, so that's where the
call sites actually look them up) -- same spirit as
tests/test_sonos_household.py, no transport-level mocking library.

Reuses tests/test_sonos_api.py's device-description fixture rather than
duplicating it; only the stereo-pair ChannelMapSet fixture is new here, since
sonos_api's own SOAP fixture uses HTSatChanMapSet (a home-theatre satellite
set) and probes/sonos.py's stereo-pair logic specifically reads the plain
ChannelMapSet-derived "channel_map" fact, which that fixture never produces.
"""

from test_sonos_api import _DEVICE_DESCRIPTION_XML, _wrap_soap

import probes.sonos as sonos
from probes.sonos import applies_to, run

# -- applies_to ----------------------------------------------------------


class _Asset:
    def __init__(self, hostname=None, vendor=None):
        self.hostname = hostname
        self.vendor = vendor


class _Service:
    def __init__(self, port):
        self.port = port


def test_applies_to_hostname_match():
    assert applies_to(_Asset(hostname="Living Room Sonos"), [], []) is True


def test_applies_to_vendor_match():
    assert applies_to(_Asset(vendor="Sonos, Inc."), [], []) is True


def test_applies_to_port_1400_match():
    assert applies_to(_Asset(), [], [_Service(1400)]) is True


def test_applies_to_false_with_neither():
    assert applies_to(_Asset(hostname="printer"), [], [_Service(631)]) is False


# -- run() -----------------------------------------------------------------


def _stub_fetches(monkeypatch, device_description=None, zone_group_state=None, status_zp=None):
    monkeypatch.setattr(sonos, "fetch_device_description", lambda client, ip: device_description)
    monkeypatch.setattr(sonos, "fetch_zone_group_state", lambda client, ip: zone_group_state)
    monkeypatch.setattr(sonos, "fetch_status_zp", lambda client, ip: status_zp)


def test_run_reports_no_response_when_all_three_endpoints_are_empty(monkeypatch):
    _stub_fetches(monkeypatch)
    outcome = run("192.168.1.50")
    assert outcome.ok is False
    assert "No response" in outcome.summary


def test_run_reports_connection_failure(monkeypatch):
    def _raise(client, ip):
        raise ConnectionError("refused")

    monkeypatch.setattr(sonos, "fetch_device_description", _raise)
    outcome = run("192.168.1.50")
    assert outcome.ok is False
    assert "Could not connect" in outcome.summary


def test_run_builds_suggestions_from_device_description(monkeypatch):
    _stub_fetches(monkeypatch, device_description=_DEVICE_DESCRIPTION_XML)
    outcome = run("192.168.1.50")

    assert outcome.ok is True
    assert outcome.facts["room_name"] == "Living Room"
    assert outcome.facts["endpoints_ok"] == ["device_description"]
    assert {"field": "position", "value": "Living Room", "reason": 'Sonos reports its own zone name as "Living Room".'} in outcome.suggestions
    assert {"field": "model", "value": "Sonos Playbar", "reason": "From the device's own UPnP description."} in outcome.suggestions
    assert {"field": "firmware_version", "value": "86.8-78270", "reason": "Sonos software version, from the device description."} in outcome.suggestions
    assert outcome.summary == "Living Room"


def test_run_still_suggests_model_when_room_name_is_absent(monkeypatch):
    # No roomName in this description -- the position suggestion must be
    # skipped without preventing the model/firmware suggestions that follow.
    xml = (
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        "<modelName>Sonos One</modelName>"
        "<softwareVersion>80.1-2.3</softwareVersion>"
        "</device></root>"
    )
    _stub_fetches(monkeypatch, device_description=xml)

    outcome = run("192.168.1.50")

    assert outcome.ok is True
    assert "room_name" not in outcome.facts
    assert {"field": "model", "value": "Sonos One", "reason": "From the device's own UPnP description."} in outcome.suggestions
    assert outcome.summary == "Sonos player identified"


def test_run_falls_back_to_status_zp_when_channel_map_missing_from_zgs(monkeypatch):
    # zone_group_state present but with no facts at all (a bare envelope) --
    # "channel_map" not in facts, so the /status/zp fallback must still be
    # queried, per the `if "channel_map" not in facts:` branch.
    empty_envelope = '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/></s:Envelope>'
    zp_xml = '<ZPSupportInfo><ZoneName>Kitchen</ZoneName><SerialNumber>AABBCCDDEEFF</SerialNumber></ZPSupportInfo>'
    _stub_fetches(monkeypatch, zone_group_state=empty_envelope, status_zp=zp_xml)

    outcome = run("192.168.1.50")

    assert outcome.ok is True
    assert outcome.facts["room_name"] == "Kitchen"
    assert "status_zp" in outcome.facts["endpoints_ok"]
    assert "zone_group_topology" not in outcome.facts["endpoints_ok"]  # zgs_facts was empty


def test_run_reports_stereo_pair_channel_from_zone_group_state(monkeypatch):
    # A plain (non-HT) stereo pair: ChannelMapSet, not HTSatChanMapSet.
    inner = (
        "<ZoneGroups>"
        '<ZoneGroup Coordinator="RINCON_AABBCCDDEEFF01400" ID="RINCON_AABBCCDDEEFF01400:1">'
        '<ZoneGroupMember UUID="RINCON_AABBCCDDEEFF01400" '
        'Location="http://192.168.1.50:1400/xml/device_description.xml" ZoneName="Living Room" '
        'ChannelMapSet="RINCON_AABBCCDDEEFF01400:LF;RINCON_112233445566012:RF">'
        '<Satellite UUID="RINCON_112233445566012" '
        'Location="http://192.168.1.51:1400/xml/device_description.xml" ZoneName="Living Room" '
        'ChannelMapSet="RINCON_AABBCCDDEEFF01400:LF;RINCON_112233445566012:RF" Invisible="1"/>'
        "</ZoneGroupMember>"
        "</ZoneGroup>"
        "</ZoneGroups>"
    )
    _stub_fetches(
        monkeypatch,
        device_description=_DEVICE_DESCRIPTION_XML,  # supplies the udn this test keys off
        zone_group_state=_wrap_soap(inner),
    )

    outcome = run("192.168.1.50")

    assert outcome.ok is True
    assert outcome.facts["channel_map"]["RINCON_AABBCCDDEEFF01400"] == "LF"
    assert outcome.summary == "Living Room -- stereo pair, left (LF) channel"


def test_run_falls_back_to_room_name_when_udn_has_no_channel_entry(monkeypatch):
    # channel_map is present but has no entry for this player's own UDN --
    # `channel` stays None, so the summary must fall back to just room_name
    # rather than crash or report a channel it doesn't actually know.
    inner = (
        "<ZoneGroups>"
        '<ZoneGroup Coordinator="RINCON_999999999999012" ID="RINCON_999999999999012:1">'
        '<ZoneGroupMember UUID="RINCON_999999999999012" '
        'Location="http://192.168.1.60:1400/xml/device_description.xml" ZoneName="Office" '
        'ChannelMapSet="RINCON_999999999999012:LF">'
        "</ZoneGroupMember>"
        "</ZoneGroup>"
        "</ZoneGroups>"
    )
    # device_description's udn (RINCON_AABBCCDDEEFF01400) doesn't appear
    # anywhere in this channel_map.
    _stub_fetches(monkeypatch, device_description=_DEVICE_DESCRIPTION_XML, zone_group_state=_wrap_soap(inner))

    outcome = run("192.168.1.50")

    assert outcome.ok is True
    assert outcome.summary == "Living Room"  # room_name, no channel suffix
