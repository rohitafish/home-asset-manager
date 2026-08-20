"""Tests for discovery/sonos_household.py. HTTP is never exercised --
enumerate_household()/run_sonos_household_discovery() are tested by
monkeypatching the fetch_* functions they call (imported by name into this
module's namespace, same technique test_assistant.py uses for the fake
Anthropic client), so the real XML parsing in probes/sonos_api.py (already
covered by tests/test_sonos_api.py) still runs end to end. All fixtures use
fabricated RINCON ids/MACs, never a real device's.
"""

from conftest import make_asset, make_interface
from sqlmodel import select

import discovery.sonos_household as sonos_household
from app.models import Asset, AssetInterface, AssetService
from discovery.sonos_household import (
    candidate_seed_ips,
    run_sonos_household_discovery,
    to_discovered_devices,
)
from probes.sonos_api import SonosPlayer

# -- candidate_seed_ips ---------------------------------------------------------


def test_candidate_seed_ips_finds_sonos_vendor_asset(session):
    asset = make_asset(session, hostname="Sonos Move", vendor="Sonos")
    make_interface(session, asset.id, ip="10.0.0.5")

    assert candidate_seed_ips(session) == ["10.0.0.5"]


def test_candidate_seed_ips_finds_asset_with_port_1400_service(session):
    asset = make_asset(session, hostname="mystery-box")
    make_interface(session, asset.id, ip="10.0.0.6")
    session.add(AssetService(asset_id=asset.id, port=1400))
    session.commit()

    assert candidate_seed_ips(session) == ["10.0.0.6"]


def test_candidate_seed_ips_excludes_unrelated_assets(session):
    unrelated = make_asset(session, hostname="printer", vendor="HP")
    make_interface(session, unrelated.id, ip="10.0.0.7")

    assert candidate_seed_ips(session) == []


def test_candidate_seed_ips_orders_most_recent_first(session):
    from datetime import datetime

    asset = make_asset(session, hostname="Sonos One", vendor="Sonos")
    make_interface(session, asset.id, ip="10.0.0.8", last_seen=datetime(2026, 1, 1))
    make_interface(session, asset.id, ip="10.0.0.9", last_seen=datetime(2026, 6, 1))

    assert candidate_seed_ips(session) == ["10.0.0.9", "10.0.0.8"]


# -- to_discovered_devices -------------------------------------------------------


def test_to_discovered_devices_skips_players_with_no_mac():
    players = [SonosPlayer(
        uuid="RINCON_X", room_name="Kitchen", location_url=None, mac=None,
        channel=None, ht_channel=None, is_satellite=False, invisible=False,
    )]
    assert to_discovered_devices(players) == []


_LAN_LOCATION = "http://192.168.1.50:1400/xml/device_description.xml"


def test_to_discovered_devices_suppresses_hostname_for_satellite():
    satellite = SonosPlayer(
        uuid="RINCON_X", room_name="Living Room", location_url=_LAN_LOCATION,
        mac="aa:bb:cc:dd:ee:01", channel=None, ht_channel="SW",
        is_satellite=True, invisible=True, model="Sonos Sub",
    )
    devices = to_discovered_devices([satellite])
    assert devices[0].hostname is None
    assert devices[0].mac == "aa:bb:cc:dd:ee:01"
    assert devices[0].source == "sonos_household"


def test_to_discovered_devices_names_visible_member_with_room():
    member = SonosPlayer(
        uuid="RINCON_X", room_name="Kitchen", location_url=_LAN_LOCATION,
        mac="aa:bb:cc:dd:ee:02", channel="LF,RF", ht_channel=None,
        is_satellite=False, invisible=False, model="Sonos One",
    )
    devices = to_discovered_devices([member])
    assert devices[0].hostname == "Sonos One (Kitchen)"


def test_to_discovered_devices_avoids_redundant_room_in_name():
    # A player whose own room name is already part of its model string
    # (e.g. "Move" is itself the room name in the Tesla-Powerwall-adjacent
    # Sonos Move case) shouldn't get a doubled-up "Move (Move)" hostname.
    member = SonosPlayer(
        uuid="RINCON_X", room_name="Move", location_url=_LAN_LOCATION,
        mac="aa:bb:cc:dd:ee:03", channel=None, ht_channel=None,
        is_satellite=False, invisible=False, model="Sonos Move",
    )
    devices = to_discovered_devices([member])
    assert devices[0].hostname == "Sonos Move"


def test_to_discovered_devices_skips_a_player_with_no_ip():
    """A player with no location_url (so p.ip is None) has no LAN address
    to verify -- skip it rather than pass a None ip through to
    reconcile_into_db. Distinct from the spoofed-ip regression test below:
    this is the "absent" case, that one is the "present but untrustworthy"
    case."""
    member = SonosPlayer(
        uuid="RINCON_X", room_name="Kitchen", location_url=None,
        mac="aa:bb:cc:dd:ee:04", channel=None, ht_channel=None,
        is_satellite=False, invisible=False, model="Sonos One",
    )
    assert to_discovered_devices([member]) == []


def test_to_discovered_devices_skips_a_player_with_a_spoofed_looking_ip():
    """Regression test: each player entry in a GetZoneGroupState response is
    self-reported by whatever device answered the seed IP, not
    independently verified -- a compromised/spoofed responder could
    otherwise claim an arbitrary "other player" pointed at, say, localhost
    or a public address, and have it merged into the asset inventory as
    real evidence via reconcile_into_db."""
    for spoofed in (
        "http://127.0.0.1:1400/xml/device_description.xml",       # loopback
        "http://8.8.8.8:1400/xml/device_description.xml",         # public
        "http://169.254.1.1:1400/xml/device_description.xml",     # link-local
    ):
        member = SonosPlayer(
            uuid="RINCON_X", room_name="Kitchen", location_url=spoofed,
            mac="aa:bb:cc:dd:ee:05", channel=None, ht_channel=None,
            is_satellite=False, invisible=False, model="Sonos One",
        )
        assert to_discovered_devices([member]) == [], f"must skip {spoofed}"


# -- run_sonos_household_discovery -----------------------------------------------


def test_run_sonos_household_discovery_no_seed_ips(session):
    result = run_sonos_household_discovery(session)
    assert result["status"] == "no_seed_ips"


def test_run_sonos_household_discovery_no_response(session, monkeypatch):
    asset = make_asset(session, hostname="Sonos Move", vendor="Sonos")
    make_interface(session, asset.id, ip="10.0.0.5")
    monkeypatch.setattr(sonos_household, "fetch_zone_group_state", lambda client, ip: None)

    result = run_sonos_household_discovery(session)

    assert result["status"] == "no_response"


def test_run_sonos_household_discovery_dry_run_does_not_write(session, monkeypatch):
    asset = make_asset(session, hostname="Sonos Move", vendor="Sonos")
    make_interface(session, asset.id, ip="10.0.0.5")

    soap_xml = _fake_soap_xml()
    monkeypatch.setattr(sonos_household, "fetch_zone_group_state", lambda client, ip: soap_xml)
    monkeypatch.setattr(sonos_household, "enrich_from_device_description", lambda players, timeout=3.0: None)

    result = run_sonos_household_discovery(session, dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert len(result["players"]) == 1
    # Nothing new was created -- only the seed asset exists.
    assert len(session.exec(select(Asset)).all()) == 1


def test_run_sonos_household_discovery_apply_creates_new_asset(session, monkeypatch):
    asset = make_asset(session, hostname="Sonos Move", vendor="Sonos")
    make_interface(session, asset.id, ip="10.0.0.5")

    soap_xml = _fake_soap_xml()
    monkeypatch.setattr(sonos_household, "fetch_zone_group_state", lambda client, ip: soap_xml)
    monkeypatch.setattr(sonos_household, "enrich_from_device_description", lambda players, timeout=3.0: None)

    result = run_sonos_household_discovery(session, dry_run=False)

    assert result["status"] == "ok"
    assert result["created"] == 1
    new_iface = session.exec(select(AssetInterface).where(AssetInterface.mac == "aa:bb:cc:dd:ee:ff")).first()
    assert new_iface is not None


def _fake_soap_xml() -> str:
    inner = (
        "<ZoneGroups>"
        '<ZoneGroup Coordinator="RINCON_AABBCCDDEEFF01400" ID="RINCON_AABBCCDDEEFF01400:1">'
        '<ZoneGroupMember UUID="RINCON_AABBCCDDEEFF01400" '
        'Location="http://10.0.0.20:1400/xml/device_description.xml" ZoneName="Office" '
        'MicEnabled="0"/>'
        "</ZoneGroup>"
        "</ZoneGroups>"
    )
    escaped = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        '<u:GetZoneGroupStateResponse xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
        f"<ZoneGroupState>{escaped}</ZoneGroupState>"
        "</u:GetZoneGroupStateResponse>"
        "</s:Body></s:Envelope>"
    )
