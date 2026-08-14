"""Pure-function tests for discovery/normalize.py -- vendor/type guessing and
the small ip/mac helpers that feed discovery.reconcile.
"""

from discovery.normalize import (
    build_gateway_ip_set,
    build_subnet_map,
    classify_device_type,
    find_gateway_mac,
    guess_vendor_from_hostname,
    lookup_vlan,
    normalize_mac,
    normalize_nmap_hosts,
    normalize_unifi_clients,
    normalize_unifi_devices,
    normalize_unifi_devices_legacy,
)

# -- guess_vendor_from_hostname ------------------------------------------------


def test_guess_vendor_from_hostname_matches_keyword():
    assert guess_vendor_from_hostname("Alex's iPhone 15 Plus") == "Apple"


def test_guess_vendor_from_hostname_is_case_insensitive():
    assert guess_vendor_from_hostname("ALEX-MACBOOK-PRO") == "Apple"


def test_guess_vendor_from_hostname_no_match_returns_none():
    assert guess_vendor_from_hostname("Living-Room-2 ba:5a") is None


def test_guess_vendor_from_hostname_none_input():
    assert guess_vendor_from_hostname(None) is None


# -- classify_device_type ------------------------------------------------------


def test_classify_device_type_network_keyword_wins_first():
    assert classify_device_type("UDM-Pro", vendor="Sonos") == "network_device"


def test_classify_device_type_iot_keyword():
    assert classify_device_type("Kitchen Smart Plug") == "iot"


def test_classify_device_type_mobile_keyword():
    assert classify_device_type("Alex's iPhone") == "mobile"


def test_classify_device_type_end_user_keyword():
    assert classify_device_type("Alex MacBook Pro") == "end_user_device"


def test_classify_device_type_falls_back_to_vendor_network():
    assert classify_device_type("Living-Room-2 ba:5a", vendor="Ubiquiti") == "network_device"


def test_classify_device_type_falls_back_to_vendor_iot():
    assert classify_device_type("Living-Room-2 ba:5a", vendor="Sonos") == "iot"


def test_classify_device_type_falls_back_to_iot_port():
    assert classify_device_type("Living-Room-2 ba:5a", ports={9100}) == "iot"


def test_classify_device_type_default_end_user_device():
    assert classify_device_type("Living-Room-2 ba:5a") == "end_user_device"


def test_classify_device_type_hostname_keyword_beats_vendor_fallback():
    """Apple's OUI can't distinguish an iPhone from a MacBook -- an explicit
    hostname keyword must win even when a vendor guess would say otherwise."""
    assert classify_device_type("Alex's iPhone", vendor="Ubiquiti") == "mobile"


# -- normalize_mac --------------------------------------------------------------


def test_normalize_mac_colon_separated():
    assert normalize_mac("24:5A:4C:00:00:01") == "24:5a:4c:00:00:01"


def test_normalize_mac_dash_separated():
    assert normalize_mac("24-5A-4C-00-00-01") == "24:5a:4c:00:00:01"


def test_normalize_mac_no_separator():
    assert normalize_mac("245A4C000001") == "24:5a:4c:00:00:01"


def test_normalize_mac_invalid_length_falls_back_to_lowercase():
    assert normalize_mac("not-a-mac") == "not-a-mac"


def test_normalize_mac_none_input():
    assert normalize_mac(None) is None


def test_normalize_mac_empty_string():
    assert normalize_mac("") is None


# -- find_gateway_mac -----------------------------------------------------------


def test_find_gateway_mac_single_match():
    devices = [
        {"model": "UDM Pro", "macAddress": "24:5A:4C:00:00:01"},
        {"model": "U6-Pro", "macAddress": "24:5A:4C:00:00:02"},
    ]
    assert find_gateway_mac(devices) == "24:5a:4c:00:00:01"


def test_find_gateway_mac_no_match_returns_none():
    devices = [{"model": "U6-Pro", "macAddress": "24:5A:4C:00:00:02"}]
    assert find_gateway_mac(devices) is None


def test_find_gateway_mac_ambiguous_multiple_matches_returns_none():
    devices = [
        {"model": "UDM Pro", "macAddress": "24:5A:4C:00:00:01"},
        {"model": "UDR", "macAddress": "24:5A:4C:00:00:03"},
    ]
    assert find_gateway_mac(devices) is None


# -- build_subnet_map / lookup_vlan ---------------------------------------------


def test_build_subnet_map_and_lookup_vlan_match():
    networks = [
        {"host_ip": "192.168.1.1", "prefix_length": 24, "name": "Default", "vlan_id": 1},
        {"host_ip": "10.0.20.1", "prefix_length": 24, "name": "IoT", "vlan_id": 20},
    ]
    subnet_map = build_subnet_map(networks)

    assert lookup_vlan("192.168.1.50", subnet_map) == ("Default", 1)
    assert lookup_vlan("10.0.20.99", subnet_map) == ("IoT", 20)


def test_lookup_vlan_no_match_returns_none_none():
    subnet_map = build_subnet_map([{"host_ip": "192.168.1.1", "prefix_length": 24}])
    assert lookup_vlan("10.0.0.5", subnet_map) == (None, None)


def test_lookup_vlan_missing_ip_returns_none_none():
    subnet_map = build_subnet_map([{"host_ip": "192.168.1.1", "prefix_length": 24}])
    assert lookup_vlan(None, subnet_map) == (None, None)


def test_lookup_vlan_invalid_ip_returns_none_none():
    subnet_map = build_subnet_map([{"host_ip": "192.168.1.1", "prefix_length": 24}])
    assert lookup_vlan("not-an-ip", subnet_map) == (None, None)


def test_build_subnet_map_skips_entries_missing_host_ip_or_prefix():
    networks = [{"host_ip": None, "prefix_length": 24}, {"host_ip": "10.0.0.1", "prefix_length": None}]
    assert build_subnet_map(networks) == []


def test_build_subnet_map_skips_invalid_network():
    networks = [{"host_ip": "not-an-ip", "prefix_length": 24}]
    assert build_subnet_map(networks) == []


# -- build_gateway_ip_set --------------------------------------------------------


def test_build_gateway_ip_set():
    networks = [{"host_ip": "192.168.1.1"}, {"host_ip": "10.0.20.1"}, {"no_host_ip": True}]
    assert build_gateway_ip_set(networks) == {"192.168.1.1", "10.0.20.1"}


# -- normalize_* device-list functions -------------------------------------------


def test_normalize_unifi_clients_maps_connection_type():
    clients = [
        {"type": "WIRELESS", "macAddress": "24:5A:4C:00:00:01", "ipAddress": "192.168.1.50", "name": "Alex's iPhone", "id": "c1"},
        {"type": "WIRED", "macAddress": "24:5A:4C:00:00:02", "ipAddress": "192.168.1.51", "name": "desktop", "id": "c2"},
    ]
    devices = normalize_unifi_clients(clients)

    assert devices[0].connection_type == "wireless"
    assert devices[0].asset_type == "mobile"
    assert devices[0].vendor == "Apple"
    assert devices[1].connection_type == "wired"
    assert devices[0].source == "unifi_client"


def test_normalize_unifi_devices_are_always_network_device_ubiquiti():
    infra = [{"macAddress": "24:5A:4C:00:00:01", "name": "UDM-Pro", "model": "UDM Pro", "firmwareVersion": "3.0.0", "id": "d1"}]
    devices = normalize_unifi_devices(infra)

    assert devices[0].asset_type == "network_device"
    assert devices[0].vendor == "Ubiquiti"
    assert devices[0].connection_type == "wired"
    assert devices[0].model == "UDM Pro"


def test_normalize_unifi_devices_legacy_carries_serial_and_model_number():
    legacy = [{"mac": "24:5A:4C:00:00:01", "name": "udm", "serial": "ABC123", "model": "UDRULT", "displayable_version": "3.0.0"}]
    devices = normalize_unifi_devices_legacy(legacy)

    assert devices[0].serial_number == "ABC123"
    assert devices[0].model_number == "UDRULT"
    assert devices[0].firmware_version == "3.0.0"


def test_normalize_nmap_hosts_prefers_mac_vendor_over_hostname_guess():
    hosts = [{"mac": "24:5A:4C:00:00:01", "hostname": "Alex's iPhone", "vendor": "Some Real OUI Vendor", "ip": "192.168.1.50", "services": [{"port": 22}]}]
    devices = normalize_nmap_hosts(hosts)

    assert devices[0].vendor == "Some Real OUI Vendor"
    assert devices[0].asset_type == "mobile"  # hostname keyword still wins classification


def test_normalize_nmap_hosts_falls_back_to_hostname_vendor_guess_when_nmap_vendor_missing():
    hosts = [{"mac": "02:11:22:33:44:55", "hostname": "Alex's iPhone", "vendor": None, "ip": "192.168.1.50", "services": []}]
    devices = normalize_nmap_hosts(hosts)

    assert devices[0].vendor == "Apple"
