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
    enumerate_household,
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


def test_to_discovered_devices_carries_canonical_serial():
    # SonosPlayer.serial arrives already canonicalized by the time it gets
    # here -- enrich_from_device_description populates it from
    # parse_device_description, which normalizes via
    # probes.sonos_api.normalize_sonos_serial (see tests/test_sonos_api.py).
    # This just pins that to_discovered_devices carries it through verbatim
    # onto DiscoveredDevice.serial_number, rather than re-mangling it.
    member = SonosPlayer(
        uuid="RINCON_X", room_name="Kitchen", location_url=_LAN_LOCATION,
        mac="aa:bb:cc:dd:ee:04", channel=None, ht_channel=None,
        is_satellite=False, invisible=False, model="Sonos One",
        serial="AABBCCDDEEFF1",
    )
    devices = to_discovered_devices([member])
    assert devices[0].serial_number == "AABBCCDDEEFF1"


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


# -- enumerate_household -----------------------------------------------------


def test_enumerate_household_prefers_a_multi_player_seed(monkeypatch):
    # Regression test: a Move running as its own standalone Sonos system
    # answers with only itself; returning on that first non-empty answer
    # hid a real bonded household whose seed came later in the list.
    responses = {"10.0.0.1": _fake_soap_xml(), "10.0.0.2": _fake_soap_xml(_HOUSEHOLD_MEMBERS)}
    monkeypatch.setattr(sonos_household, "fetch_zone_group_state", lambda client, ip: responses[ip])

    players, error = enumerate_household(["10.0.0.1", "10.0.0.2"])

    assert error is None
    assert {p.room_name for p in players} == {"Office", "Living Room"}


def test_enumerate_household_stops_at_the_first_multi_player_seed(monkeypatch):
    queried: list[str] = []

    def fake_fetch(client, ip):
        queried.append(ip)
        if ip != "10.0.0.2":
            raise AssertionError(f"queried {ip} after a household seed already answered")
        return _fake_soap_xml(_HOUSEHOLD_MEMBERS)

    monkeypatch.setattr(sonos_household, "fetch_zone_group_state", fake_fetch)

    players, error = enumerate_household(["10.0.0.2", "10.0.0.3"])

    assert error is None
    assert len(players) == 2
    assert queried == ["10.0.0.2"]


def test_enumerate_household_falls_back_to_a_single_player_result(monkeypatch):
    # No seed can see more than itself -- a one-player household is a real,
    # if minimal, answer, not a no_response. Bounded: one try per seed.
    queried: list[str] = []

    def fake_fetch(client, ip):
        queried.append(ip)
        return _fake_soap_xml()

    monkeypatch.setattr(sonos_household, "fetch_zone_group_state", fake_fetch)

    players, error = enumerate_household(["10.0.0.1", "10.0.0.2"])

    assert error is None
    assert len(players) == 1
    assert queried == ["10.0.0.1", "10.0.0.2"]  # every seed tried once, no retries


def test_enumerate_household_reports_no_response_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(sonos_household, "fetch_zone_group_state", lambda client, ip: None)

    players, error = enumerate_household(["10.0.0.1", "10.0.0.2"])

    assert players == []
    assert error is not None and "No Sonos player responded" in error


def test_enumerate_household_skips_an_unparseable_seed(monkeypatch):
    responses = {"10.0.0.1": "not xml at all", "10.0.0.2": _fake_soap_xml(_HOUSEHOLD_MEMBERS)}
    monkeypatch.setattr(sonos_household, "fetch_zone_group_state", lambda client, ip: responses[ip])

    players, error = enumerate_household(["10.0.0.1", "10.0.0.2"])

    assert error is None
    assert len(players) == 2


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


_DEFAULT_MEMBERS = [("RINCON_AABBCCDDEEFF01400", "10.0.0.20", "Office")]
_HOUSEHOLD_MEMBERS = [
    ("RINCON_AABBCCDDEEFF01400", "10.0.0.20", "Office"),
    ("RINCON_111111111111", "10.0.0.21", "Living Room"),
]


def _fake_soap_xml(members: list[tuple[str, str, str]] | None = None) -> str:
    """Each member is (uuid, ip, zone_name), one ZoneGroup apiece --
    deliberately not bonded (Satellite nesting is already covered by
    tests/test_sonos_api.py); this file only cares about player *count*,
    which is what enumerate_household branches on."""
    groups = "".join(
        f'<ZoneGroup Coordinator="{uuid}" ID="{uuid}:1">'
        f'<ZoneGroupMember UUID="{uuid}" '
        f'Location="http://{ip}:1400/xml/device_description.xml" ZoneName="{zone}" '
        'MicEnabled="0"/>'
        "</ZoneGroup>"
        for uuid, ip, zone in (members or _DEFAULT_MEMBERS)
    )
    inner = f"<ZoneGroups>{groups}</ZoneGroups>"
    escaped = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        '<u:GetZoneGroupStateResponse xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
        f"<ZoneGroupState>{escaped}</ZoneGroupState>"
        "</u:GetZoneGroupStateResponse>"
        "</s:Body></s:Envelope>"
    )
