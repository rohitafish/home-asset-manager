"""Turns raw collector output (UniFi API JSON, nmap results) into a common
shape that discovery.reconcile can merge and persist, regardless of source.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

_NETWORK_KEYWORDS = ["gateway", "udm", "usw-", "u6-", "u6+", "u7-", "switch", "access point"]
_IOT_KEYWORDS = [
    "smart plug", "plug", "burglar alarm", "alarm", "thermostat", "air purifier",
    "echo", "sonos", "hub", "printer", "camera", "doorbell", "tv box", "bravia",
    "smart tv", "powerwall", "apple tv",
]
_MOBILE_KEYWORDS = ["iphone", "ipad", "apple watch", "watch", "galaxy", "pixel", "phone"]
_END_USER_KEYWORDS = ["macbook", "mac mini", "imac", "laptop", "chromebook", "mini"]

# Fallback signals used only when the hostname itself gives no match -- e.g.
# generic/DHCP-assigned names like "Living-Room-2 ba:5a". MAC vendor (OUI) and
# well-known IoT ports are both weaker signals than an explicit hostname
# keyword (Apple's OUI alone can't distinguish an iPhone from a MacBook), so
# they're deliberately checked second, not first.
_NETWORK_VENDORS = ["ubiquiti"]
_IOT_VENDORS = [
    "sonos", "amazon technologies", "google", "roku", "nest labs", "ring",
    "philips", "tp-link", "belkin", "wyze", "ecobee", "sonoff", "shelly", "lifx",
]
_IOT_PORTS = {9100, 1400, 8008, 8009, 554}  # printer, sonos, chromecast x2, rtsp camera

# Ordered (keyword, vendor) pairs used to guess a manufacturer from the
# device's name when no MAC-based vendor lookup is possible -- notably
# Apple's private Wi-Fi address deliberately sets the "locally administered"
# bit, so an iPhone/iPad/etc. will *never* match a real Apple OUI no matter
# how good the MAC vendor database is. The hostname is often the only signal
# left in that case (UniFi shows names like "Alex's iPhone 15 Plus").
_HOSTNAME_VENDOR_KEYWORDS = [
    ("iphone", "Apple"),
    ("ipad", "Apple"),
    ("apple watch", "Apple"),
    ("apple tv", "Apple"),
    ("macbook", "Apple"),
    ("mac mini", "Apple"),
    ("imac", "Apple"),
    ("airpods", "Apple"),
    ("galaxy", "Samsung"),
    ("pixel", "Google"),
    ("chromebook", "Google"),
    ("sky q", "Sky"),
    ("sky glass", "Sky"),
    ("sky stream", "Sky"),
    ("echo", "Amazon"),
    ("sonos", "Sonos"),
    ("roku", "Roku"),
    ("powerwall", "Tesla"),
    ("tesla", "Tesla"),
    ("lgwebostv", "LG"),
    ("webos", "LG"),
    ("ubiquiti", "Ubiquiti"),
    ("unifi", "Ubiquiti"),
    ("ucg", "Ubiquiti"),
]


def guess_vendor_from_hostname(hostname: str | None) -> str | None:
    """Best-effort, not authoritative -- a real MAC-based vendor lookup (from
    nmap) always takes priority when one is available; this is only a
    fallback for devices where no such lookup will ever succeed."""
    h = (hostname or "").lower()
    for keyword, vendor in _HOSTNAME_VENDOR_KEYWORDS:
        if keyword in h:
            return vendor
    return None


def classify_device_type(
    hostname: str | None = None,
    vendor: str | None = None,
    ports: set[int] | None = None,
) -> str:
    """Best-effort asset-type guess. The UniFi Integration API doesn't expose
    device-category fingerprinting (no CPE/hardware ID), so this is a
    heuristic, not authoritative -- correct it by hand via the asset edit
    form if wrong. Hostname keywords are checked first and are authoritative
    when they hit (most specific); MAC vendor and known IoT ports (both from
    nmap) are used only as a fallback when the hostname alone is uninformative
    (e.g. a bare "Living-Room-2 ba:5a" DHCP name)."""
    h = (hostname or "").lower()
    if any(k in h for k in _NETWORK_KEYWORDS):
        return "network_device"
    if any(k in h for k in _IOT_KEYWORDS):
        return "iot"
    if any(k in h for k in _MOBILE_KEYWORDS):
        return "mobile"
    if any(k in h for k in _END_USER_KEYWORDS):
        return "end_user_device"

    v = (vendor or "").lower()
    if any(k in v for k in _NETWORK_VENDORS):
        return "network_device"
    if any(k in v for k in _IOT_VENDORS):
        return "iot"
    if ports and ports & _IOT_PORTS:
        return "iot"

    return "end_user_device"


def build_gateway_ip_set(networks: list[dict[str, Any]]) -> set[str]:
    """IPs that are a UniFi network's own gateway address (its host_ip).
    Used to recognize a per-VLAN gateway address discovered separately (e.g.
    by nmap, which can't ARP-resolve a MAC across VLAN boundaries) as another
    interface of the site's router rather than a distinct new asset."""
    return {net["host_ip"] for net in networks if net.get("host_ip")}


# Ubiquiti's gateway/router console product families. Used only to identify
# *which* discovered device is "the router" when a network's gateway IP
# shows up as its own host with no (or an unrelated) MAC -- the Integration
# API has no direct network-to-device link, but a small home network has
# exactly one router in practice, so this stays deliberately conservative:
# it only acts when exactly one such device is present.
_GATEWAY_MODEL_PREFIXES = ("UDM", "UCG", "UDR", "USG", "UXG")


def find_gateway_mac(infra_devices: list[dict[str, Any]]) -> str | None:
    """Returns the site's single router's MAC if exactly one UDM/UCG/UDR/USG/
    UXG-family device is present among infra_devices, else None (ambiguous or
    no gateway device found -- callers should fall back to normal per-device
    matching rather than guess)."""
    matches = [
        d
        for d in infra_devices
        if (d.get("model") or "").upper().startswith(_GATEWAY_MODEL_PREFIXES)
    ]
    if len(matches) == 1:
        return normalize_mac(matches[0].get("macAddress"))
    return None


def build_subnet_map(networks: list[dict[str, Any]]) -> list[tuple]:
    """Turns UnifiClient.list_networks_with_subnets() output into
    (ip_network, name, vlan_id) tuples for IP-based VLAN lookup. The
    Integration API's /networks overview doesn't include a subnet, only the
    per-network detail endpoint does (confirmed live) -- see unifi_client.py."""
    subnet_map = []
    for net in networks:
        host_ip, prefix = net.get("host_ip"), net.get("prefix_length")
        if not host_ip or prefix is None:
            continue
        try:
            network = ipaddress.ip_network(f"{host_ip}/{prefix}", strict=False)
        except ValueError:
            continue
        subnet_map.append((network, net.get("name"), net.get("vlan_id")))
    return subnet_map


def lookup_vlan(
    ip: str | None, subnet_map: list[tuple]
) -> tuple[str | None, int | None]:
    """Returns (network_name, vlan_id) for the subnet containing ip, or
    (None, None) if ip is missing or matches no known subnet."""
    if not ip:
        return None, None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None, None
    for network, name, vlan_id in subnet_map:
        if addr in network:
            return name, vlan_id
    return None, None


def normalize_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    hexonly = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(hexonly) != 12:
        return mac.lower()
    return ":".join(hexonly[i : i + 2] for i in range(0, 12, 2)).lower()


@dataclass
class DiscoveredDevice:
    mac: str | None = None
    ip: str | None = None
    hostname: str | None = None
    asset_type: str | None = None
    vendor: str | None = None  # MAC OUI vendor (nmap) or a hostname-based guess
    vlan: int | None = None  # from IP-to-subnet matching against UniFi's /networks
    network_name: str | None = None  # e.g. "Sky", "Sunshine" -- the UniFi network's own name
    connection_type: str | None = None  # wired / wireless
    model: str | None = None  # human-readable model, e.g. "UCG Ultra"
    firmware_version: str | None = None
    serial_number: str | None = None
    model_number: str | None = None  # SKU-style model, e.g. "UDRULT", "MGNR3B/A"
    model_identifier: str | None = None  # e.g. Apple's "Macmini9,1"
    source: str = "unknown"
    services: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def normalize_unifi_clients(clients: list[dict[str, Any]]) -> list[DiscoveredDevice]:
    devices = []
    for c in clients:
        client_type = (c.get("type") or "").upper()
        connection_type = "wireless" if client_type == "WIRELESS" else (
            "wired" if client_type == "WIRED" else None
        )
        devices.append(
            DiscoveredDevice(
                mac=normalize_mac(c.get("macAddress")),
                ip=c.get("ipAddress"),
                hostname=c.get("name"),
                asset_type=classify_device_type(c.get("name")),
                vendor=guess_vendor_from_hostname(c.get("name")),
                connection_type=connection_type,
                source="unifi_client",
                extra={"unifi_id": c.get("id"), "connected_at": c.get("connectedAt")},
            )
        )
    return devices


def normalize_unifi_devices(infra_devices: list[dict[str, Any]]) -> list[DiscoveredDevice]:
    devices = []
    for d in infra_devices:
        devices.append(
            DiscoveredDevice(
                mac=normalize_mac(d.get("macAddress")),
                ip=d.get("ipAddress"),
                hostname=d.get("name"),
                asset_type="network_device",
                vendor="Ubiquiti",  # UniFi's own infrastructure device list is Ubiquiti hardware by definition
                connection_type="wired",
                model=d.get("model"),  # human-readable, e.g. "UCG Ultra" -- the v1 API has no serial
                firmware_version=d.get("firmwareVersion"),
                source="unifi_device",
                extra={"unifi_id": d.get("id"), "state": d.get("state")},
            )
        )
    return devices


def normalize_unifi_devices_legacy(legacy_devices: list[dict[str, Any]]) -> list[DiscoveredDevice]:
    """Normalizes UnifiClient.list_devices_legacy() output -- the older
    username/password-era Controller API, which (unlike the v1 Integration
    API used by normalize_unifi_devices) exposes each device's serial number
    and SKU-style model. Merge onto normalize_unifi_devices()'s output by MAC
    (see discovery.reconcile.merge_by_mac) to get both the friendly model
    name and the serial/SKU on the same DiscoveredDevice."""
    devices = []
    for d in legacy_devices:
        devices.append(
            DiscoveredDevice(
                mac=normalize_mac(d.get("mac")),
                ip=d.get("ip"),
                hostname=d.get("name"),
                asset_type="network_device",
                vendor="Ubiquiti",
                connection_type="wired",
                serial_number=d.get("serial"),
                model_number=d.get("model"),  # SKU, e.g. "UDRULT" -- not the friendly name
                firmware_version=d.get("displayable_version") or d.get("version"),
                source="unifi_device_legacy",
                extra={"model_in_eol": d.get("model_in_eol")},
            )
        )
    return devices


def normalize_nmap_hosts(hosts: list[dict[str, Any]]) -> list[DiscoveredDevice]:
    devices = []
    for h in hosts:
        # A real MAC-based vendor lookup always wins; hostname-based guessing
        # only fills in devices where nmap's own OUI lookup came back empty
        # (notably any device using a randomized/private MAC address).
        vendor = h.get("vendor") or guess_vendor_from_hostname(h.get("hostname"))
        ports = {s["port"] for s in h.get("services", [])}
        devices.append(
            DiscoveredDevice(
                mac=normalize_mac(h.get("mac")),
                ip=h.get("ip"),
                hostname=h.get("hostname"),
                asset_type=classify_device_type(h.get("hostname"), vendor=vendor, ports=ports),
                vendor=vendor,
                connection_type=None,
                source="nmap",
                services=h.get("services", []),
            )
        )
    return devices
