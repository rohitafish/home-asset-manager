"""Tests for discovery/reconcile.py -- the merge helpers (pure) and
reconcile_into_db (DB-backed integration test of the match-by-MAC/match-by-IP/
create-new flow and the locked-field semantics).
"""

from conftest import make_asset, make_interface

from app.models import Asset, AssetInterface, AssetService, LifecycleStatus
from discovery.normalize import DiscoveredDevice
from discovery.reconcile import (
    _default_owner,
    _source_priority,
    merge_by_ip,
    merge_by_mac,
    reconcile_into_db,
)

# -- merge_by_ip ----------------------------------------------------------------


def test_merge_by_ip_fills_missing_fields_only():
    a = DiscoveredDevice(ip="192.168.1.50", mac=None, hostname="from-nmap", source="nmap")
    b = DiscoveredDevice(ip="192.168.1.50", mac="24:5a:4c:00:00:01", hostname="from-unifi", source="unifi_client")

    merged = merge_by_ip([a], [b])

    assert len(merged) == 1
    device = merged[0]
    assert device.mac == "24:5a:4c:00:00:01"  # filled from b, a had none
    assert device.hostname == "from-nmap"  # a's existing value kept, not overwritten by b
    assert device.source == "nmap+unifi_client"


def test_merge_by_ip_dedups_by_ip_and_combines_source():
    a = DiscoveredDevice(ip="192.168.1.50", source="nmap")
    b = DiscoveredDevice(ip="192.168.1.50", source="nmap")

    merged = merge_by_ip([a], [b])

    assert len(merged) == 1
    assert merged[0].source == "nmap"  # same source appearing twice is not duplicated


def test_merge_by_ip_keeps_standalone_devices_without_ip_separate():
    a = DiscoveredDevice(ip=None, hostname="no-ip-device", source="nmap")
    b = DiscoveredDevice(ip="192.168.1.50", hostname="has-ip", source="nmap")

    merged = merge_by_ip([a, b])

    assert len(merged) == 2
    assert a in merged


def test_merge_by_ip_extends_services_and_updates_extra():
    a = DiscoveredDevice(ip="192.168.1.50", source="nmap", services=[{"port": 22}], extra={"x": 1})
    b = DiscoveredDevice(ip="192.168.1.50", source="nmap", services=[{"port": 80}], extra={"y": 2})

    merged = merge_by_ip([a], [b])

    assert merged[0].services == [{"port": 22}, {"port": 80}]
    assert merged[0].extra == {"x": 1, "y": 2}


# -- merge_by_mac ----------------------------------------------------------------


def test_merge_by_mac_fills_missing_fields_only():
    a = DiscoveredDevice(mac="24:5a:4c:00:00:01", model=None, source="unifi_device")
    b = DiscoveredDevice(mac="24:5a:4c:00:00:01", model="UDM Pro", serial_number="ABC123", source="unifi_device_legacy")

    merged = merge_by_mac([a], [b])

    assert len(merged) == 1
    device = merged[0]
    assert device.model == "UDM Pro"
    assert device.serial_number == "ABC123"
    assert device.source == "unifi_device+unifi_device_legacy"


def test_merge_by_mac_keeps_standalone_devices_without_mac_separate():
    a = DiscoveredDevice(mac=None, hostname="no-mac", source="nmap")
    b = DiscoveredDevice(mac="24:5a:4c:00:00:01", hostname="has-mac", source="unifi_device")

    merged = merge_by_mac([a, b])

    assert len(merged) == 2
    assert a in merged


# -- _default_owner ---------------------------------------------------------------


def test_default_owner_uses_default_env_var(monkeypatch):
    monkeypatch.setenv("DEFAULT_OWNER", "Alex")
    monkeypatch.delenv("SECONDARY_OWNER_NAME", raising=False)
    monkeypatch.delenv("SECONDARY_OWNER_HOSTNAME_KEYWORD", raising=False)

    assert _default_owner("some-hostname") == "Alex"


def test_default_owner_matches_secondary_by_hostname_keyword_case_insensitive(monkeypatch):
    monkeypatch.setenv("DEFAULT_OWNER", "Alex")
    monkeypatch.setenv("SECONDARY_OWNER_NAME", "Sam")
    monkeypatch.setenv("SECONDARY_OWNER_HOSTNAME_KEYWORD", "sam-laptop")

    assert _default_owner("SAM-LAPTOP-2024") == "Sam"


def test_default_owner_falls_back_to_default_when_keyword_does_not_match(monkeypatch):
    monkeypatch.setenv("DEFAULT_OWNER", "Alex")
    monkeypatch.setenv("SECONDARY_OWNER_NAME", "Sam")
    monkeypatch.setenv("SECONDARY_OWNER_HOSTNAME_KEYWORD", "sam-laptop")

    assert _default_owner("random-device") == "Alex"


def test_default_owner_falls_back_when_hostname_missing(monkeypatch):
    monkeypatch.setenv("DEFAULT_OWNER", "Alex")
    monkeypatch.setenv("SECONDARY_OWNER_NAME", "Sam")
    monkeypatch.setenv("SECONDARY_OWNER_HOSTNAME_KEYWORD", "sam-laptop")

    assert _default_owner(None) == "Alex"


# -- _source_priority ---------------------------------------------------------------


def test_source_priority_none_or_empty_is_zero():
    assert _source_priority(None) == 0
    assert _source_priority("") == 0


def test_source_priority_single_source():
    assert _source_priority("nmap") == 1
    assert _source_priority("unifi_client") == 2


def test_source_priority_combined_source_takes_max():
    assert _source_priority("nmap+unifi_client") == 2


def test_source_priority_unknown_source_is_zero():
    assert _source_priority("some_future_source") == 0


# -- reconcile_into_db ------------------------------------------------------------


def test_reconcile_into_db_creates_new_asset(session, monkeypatch):
    monkeypatch.setenv("DEFAULT_OWNER", "Alex")
    device = DiscoveredDevice(
        mac="24:5a:4c:00:00:01", ip="192.168.1.50", hostname="new-device",
        asset_type="end_user_device", vendor="Apple", source="nmap",
    )

    result = reconcile_into_db(session, [device])

    assert result == {"created": 1, "updated": 0, "total": 1}
    from sqlmodel import select

    assets = session.exec(select(Asset)).all()
    assert len(assets) == 1
    assert assets[0].hostname == "new-device"
    assert assets[0].owner == "Alex"
    assert assets[0].lifecycle_status == LifecycleStatus.discovered

    ifaces = session.exec(select(AssetInterface)).all()
    assert len(ifaces) == 1
    assert ifaces[0].mac == "24:5a:4c:00:00:01"


def test_reconcile_into_db_matches_existing_asset_by_mac_and_updates(session):
    asset = make_asset(session, hostname="old-name", vendor="OldVendor", lifecycle_status=LifecycleStatus.discovered)
    make_interface(session, asset.id, mac="24:5a:4c:00:00:01")

    device = DiscoveredDevice(
        mac="24:5a:4c:00:00:01", ip="192.168.1.50", hostname="new-name",
        asset_type="iot", vendor="NewVendor", source="unifi_client",
    )

    result = reconcile_into_db(session, [device])

    assert result == {"created": 0, "updated": 1, "total": 1}
    session.refresh(asset)
    assert asset.hostname == "new-name"
    assert asset.vendor == "NewVendor"
    assert asset.asset_type == "iot"


def test_reconcile_into_db_respects_hostname_locked(session):
    asset = make_asset(session, hostname="locked-name", hostname_locked=True, lifecycle_status=LifecycleStatus.discovered)
    make_interface(session, asset.id, mac="24:5a:4c:00:00:01")

    device = DiscoveredDevice(mac="24:5a:4c:00:00:01", hostname="attempted-overwrite", source="unifi_client")
    reconcile_into_db(session, [device])

    session.refresh(asset)
    assert asset.hostname == "locked-name"


def test_reconcile_into_db_respects_vendor_locked(session):
    asset = make_asset(session, vendor="LockedVendor", vendor_locked=True, lifecycle_status=LifecycleStatus.discovered)
    make_interface(session, asset.id, mac="24:5a:4c:00:00:01")

    device = DiscoveredDevice(mac="24:5a:4c:00:00:01", vendor="AttemptedOverwrite", source="unifi_client")
    reconcile_into_db(session, [device])

    session.refresh(asset)
    assert asset.vendor == "LockedVendor"


def test_reconcile_into_db_respects_identity_locked(session):
    asset = make_asset(session, serial_number="ORIGINAL-SERIAL", identity_locked=True, lifecycle_status=LifecycleStatus.discovered)
    make_interface(session, asset.id, mac="24:5a:4c:00:00:01")

    device = DiscoveredDevice(mac="24:5a:4c:00:00:01", serial_number="OVERWRITE-ATTEMPT", source="unifi_client")
    reconcile_into_db(session, [device])

    session.refresh(asset)
    assert asset.serial_number == "ORIGINAL-SERIAL"


def test_reconcile_into_db_falls_back_to_ip_match_when_no_mac(session):
    asset = make_asset(session, hostname="matched-by-ip", lifecycle_status=LifecycleStatus.discovered)
    make_interface(session, asset.id, ip="192.168.1.50")

    device = DiscoveredDevice(mac=None, ip="192.168.1.50", hostname="renamed", source="nmap")
    result = reconcile_into_db(session, [device])

    assert result["updated"] == 1
    from sqlmodel import select

    assert len(session.exec(select(Asset)).all()) == 1


def test_reconcile_into_db_attaches_per_vlan_gateway_ip_to_router_asset(session):
    router = make_asset(session, hostname="router", lifecycle_status=LifecycleStatus.discovered)
    make_interface(session, router.id, mac="24:5a:4c:00:00:01", ip="192.168.1.1")

    # Discovered on a different VLAN with no usable MAC -- must attach to the
    # known router asset rather than create a second "router" per VLAN.
    device = DiscoveredDevice(mac=None, ip="10.0.20.1", hostname="gateway", source="nmap")
    result = reconcile_into_db(
        session, [device],
        gateway_ips={"10.0.20.1"},
        gateway_mac="24:5a:4c:00:00:01",
    )

    assert result == {"created": 0, "updated": 1, "total": 1}
    from sqlmodel import select

    assert len(session.exec(select(Asset)).all()) == 1
    ifaces = session.exec(select(AssetInterface).where(AssetInterface.asset_id == router.id)).all()
    assert {i.ip for i in ifaces} == {"192.168.1.1", "10.0.20.1"}


def test_reconcile_into_db_creates_and_updates_services(session):
    device = DiscoveredDevice(
        mac="24:5a:4c:00:00:01", ip="192.168.1.50", source="nmap",
        services=[{"port": 80, "protocol": "tcp", "product": "nginx"}],
    )
    reconcile_into_db(session, [device])

    from sqlmodel import select

    services = session.exec(select(AssetService)).all()
    assert len(services) == 1
    assert services[0].product == "nginx"

    device2 = DiscoveredDevice(
        mac="24:5a:4c:00:00:01", ip="192.168.1.50", source="nmap",
        services=[{"port": 80, "protocol": "tcp", "product": "nginx", "version": "1.20"}],
    )
    reconcile_into_db(session, [device2])

    services = session.exec(select(AssetService)).all()
    assert len(services) == 1  # same port+protocol updates in place, doesn't duplicate
    assert services[0].version == "1.20"
