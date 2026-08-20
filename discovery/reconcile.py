"""Merges discovered devices from all sources and reconciles them into the
authoritative asset inventory: match by MAC first, fall back to IP, otherwise
create a new 'discovered' asset pending triage.
"""

from __future__ import annotations

import os

from sqlmodel import Session, select

from app.clock import utcnow_naive
from app.models import Asset, AssetInterface, AssetService, LifecycleStatus
from discovery.normalize import DiscoveredDevice


def merge_by_ip(*device_lists: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
    by_ip: dict[str, DiscoveredDevice] = {}
    standalone: list[DiscoveredDevice] = []

    for devices in device_lists:
        for device in devices:
            if not device.ip:
                standalone.append(device)
                continue
            existing = by_ip.get(device.ip)
            if existing is None:
                by_ip[device.ip] = device
                continue
            if existing.mac and device.mac and existing.mac != device.mac:
                # Both entries carry a MAC, and they differ: this is two
                # distinct physical devices that happen to share an IP right
                # now (a stale UniFi client-list entry that hasn't aged out,
                # or a device that moved wired<->wireless), not one device
                # seen twice. Merging them would silently keep whichever MAC
                # `or` picked and discard the other -- the dropped device's
                # last_seen then stops updating, and it starts decaying out
                # of the Summary page's coverage metric as if it had left
                # the network. MAC dominates IP as an identity signal here,
                # same reasoning as merge_by_mac's own docstring.
                standalone.append(device)
                continue
            existing.mac = existing.mac or device.mac
            existing.hostname = existing.hostname or device.hostname
            existing.asset_type = existing.asset_type or device.asset_type
            existing.vendor = existing.vendor or device.vendor
            existing.vlan = existing.vlan if existing.vlan is not None else device.vlan
            existing.network_name = existing.network_name or device.network_name
            existing.connection_type = existing.connection_type or device.connection_type
            existing.services.extend(device.services)
            existing.extra.update(device.extra)
            if device.source not in existing.source.split("+"):
                existing.source = f"{existing.source}+{device.source}"

    return list(by_ip.values()) + standalone


def merge_by_mac(*device_lists: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
    """Merges DiscoveredDevice lists on MAC -- used to combine the UniFi
    Integration API's infra device list (rich fields, e.g. human-readable
    model, no serial) with the legacy stat/device endpoint's list (serial and
    SKU-style model, otherwise sparse) into one device per physical unit.
    MAC is the join key here rather than IP, since it's guaranteed present on
    both -- unlike merge_by_ip below, which exists because nmap results often
    lack a MAC entirely."""
    by_mac: dict[str, DiscoveredDevice] = {}
    standalone: list[DiscoveredDevice] = []

    for devices in device_lists:
        for device in devices:
            if not device.mac:
                standalone.append(device)
                continue
            existing = by_mac.get(device.mac)
            if existing is None:
                by_mac[device.mac] = device
                continue
            existing.ip = existing.ip or device.ip
            existing.hostname = existing.hostname or device.hostname
            existing.asset_type = existing.asset_type or device.asset_type
            existing.vendor = existing.vendor or device.vendor
            existing.connection_type = existing.connection_type or device.connection_type
            existing.model = existing.model or device.model
            existing.firmware_version = existing.firmware_version or device.firmware_version
            existing.serial_number = existing.serial_number or device.serial_number
            existing.model_number = existing.model_number or device.model_number
            existing.model_identifier = existing.model_identifier or device.model_identifier
            existing.extra.update(device.extra)
            if device.source not in existing.source.split("+"):
                existing.source = f"{existing.source}+{device.source}"

    return list(by_mac.values()) + standalone


def _default_owner(hostname: str | None) -> str:
    """Owner assigned to newly discovered assets, configurable via .env (see
    .env.example) rather than hardcoded, since this is a personal detail --
    who's in your household -- not something to bake into tracked source."""
    default_owner = os.environ.get("DEFAULT_OWNER", "Owner")
    secondary_name = os.environ.get("SECONDARY_OWNER_NAME")
    secondary_keyword = os.environ.get("SECONDARY_OWNER_HOSTNAME_KEYWORD")
    if (
        secondary_name
        and secondary_keyword
        and hostname
        and secondary_keyword.lower() in hostname.lower()
    ):
        return secondary_name
    return default_owner


# UniFi's client name is typically curated (user-set, or a friendly name UniFi
# resolves) while nmap's hostname comes from raw reverse-DNS/mDNS lookups,
# which are frequently auto-generated and ugly (e.g. "EPSONF8467B.localdomain").
# Higher priority wins when reconciling the same asset across sources/runs.
_HOSTNAME_SOURCE_PRIORITY = {"unifi_client": 2, "unifi_device": 2, "nmap": 1}


def _source_priority(source: str | None) -> int:
    if not source:
        return 0
    return max((_HOSTNAME_SOURCE_PRIORITY.get(part, 0) for part in source.split("+")), default=0)


def _find_asset_by_mac(session: Session, mac: str) -> Asset | None:
    iface = session.exec(select(AssetInterface).where(AssetInterface.mac == mac)).first()
    if iface:
        return session.get(Asset, iface.asset_id)
    return None


def _find_asset_by_ip(session: Session, ip: str) -> Asset | None:
    # Only matches an interface that has NO MAC of its own. IP is a weak,
    # transient identity signal (DHCP reassigns it) -- a MAC is a strong one.
    # Without this filter, a lease reused by a different physical device
    # (routine on a network with cross-VLAN devices nmap can't ARP, so
    # device.mac is None) would match the OLD device's interface here, and
    # reconcile_into_db's `iface.mac = device.mac or iface.mac` below would
    # then silently overwrite that interface's real MAC with the new
    # device's -- permanently conflating two physical devices into one asset
    # with no record of the mistake. A MAC-less interface has no stronger
    # identity to protect, so it's still fair game for an IP match.
    iface = session.exec(
        select(AssetInterface)
        .where(AssetInterface.ip == ip, AssetInterface.mac.is_(None))
        .order_by(AssetInterface.last_seen.desc())
    ).first()
    if iface:
        return session.get(Asset, iface.asset_id)
    return None


def reconcile_into_db(
    session: Session,
    devices: list[DiscoveredDevice],
    gateway_ips: set[str] | None = None,
    gateway_mac: str | None = None,
) -> dict[str, int]:
    now = utcnow_naive()
    created = 0
    updated = 0
    gateway_ips = gateway_ips or set()

    for device in devices:
        asset = None
        # A network's own gateway address is, by definition, an interface of
        # the site's router -- even if this particular discovery of it came
        # back with no MAC (nmap can't ARP across VLAN boundaries) or an
        # unrelated per-VLAN virtual MAC. Attach it to the known router
        # asset directly rather than letting it become (or stay) a separate
        # asset per VLAN.
        if gateway_mac and device.ip and device.ip in gateway_ips:
            asset = _find_asset_by_mac(session, gateway_mac)
        if asset is None and device.mac:
            asset = _find_asset_by_mac(session, device.mac)
        if asset is None and device.ip:
            asset = _find_asset_by_ip(session, device.ip)

        if asset is None:
            asset = Asset(
                asset_type=device.asset_type or "end_user_device",
                hostname=device.hostname,
                hostname_source=device.source if device.hostname else None,
                vendor=device.vendor,
                model=device.model,
                firmware_version=device.firmware_version,
                serial_number=device.serial_number,
                model_number=device.model_number,
                model_identifier=device.model_identifier,
                owner=_default_owner(device.hostname),
                lifecycle_status=LifecycleStatus.discovered,
                source=device.source,
                first_seen=now,
                last_seen=now,
            )
            session.add(asset)
            session.flush()
            created += 1
        else:
            asset.last_seen = now
            if (
                not asset.hostname_locked
                and device.hostname
                and _source_priority(device.source) >= _source_priority(asset.hostname_source)
            ):
                asset.hostname = device.hostname
                asset.hostname_source = device.source
            if not asset.vendor_locked and device.vendor:
                asset.vendor = device.vendor
            if asset.lifecycle_status == LifecycleStatus.discovered and device.asset_type:
                asset.asset_type = device.asset_type
            if not asset.owner:
                asset.owner = _default_owner(asset.hostname)
            # model/firmware_version are not identity data -- they legitimately
            # change over a device's life (a firmware upgrade, a re-flash), so
            # they're refreshed on every run regardless of identity_locked.
            if device.model:
                asset.model = device.model
            if device.firmware_version:
                asset.firmware_version = device.firmware_version
            if not asset.identity_locked:
                # only fill, never blank: a collector that stops reporting a
                # serial this run must not erase one captured previously
                if device.serial_number:
                    asset.serial_number = device.serial_number
                if device.model_number:
                    asset.model_number = device.model_number
                if device.model_identifier:
                    asset.model_identifier = device.model_identifier
            session.add(asset)
            updated += 1

        iface = None
        if device.mac:
            iface = session.exec(
                select(AssetInterface).where(
                    AssetInterface.asset_id == asset.id, AssetInterface.mac == device.mac
                )
            ).first()
        if iface is None and device.ip:
            iface = session.exec(
                select(AssetInterface).where(
                    AssetInterface.asset_id == asset.id, AssetInterface.ip == device.ip
                )
            ).first()

        if iface is None:
            iface = AssetInterface(asset_id=asset.id)
        iface.mac = device.mac or iface.mac
        iface.ip = device.ip or iface.ip
        iface.connection_type = device.connection_type or iface.connection_type
        iface.vendor = device.vendor or iface.vendor
        iface.vlan = device.vlan if device.vlan is not None else iface.vlan
        iface.network_name = device.network_name or iface.network_name
        iface.last_seen = now
        session.add(iface)

        for svc in device.services:
            existing_svc = session.exec(
                select(AssetService).where(
                    AssetService.asset_id == asset.id,
                    AssetService.port == svc["port"],
                    AssetService.protocol == svc.get("protocol", "tcp"),
                )
            ).first()
            if existing_svc is None:
                existing_svc = AssetService(
                    asset_id=asset.id, port=svc["port"], protocol=svc.get("protocol", "tcp")
                )
            existing_svc.product = svc.get("product") or existing_svc.product
            existing_svc.version = svc.get("version") or existing_svc.version
            existing_svc.banner = svc.get("banner") or existing_svc.banner
            existing_svc.last_seen = now
            session.add(existing_svc)

    session.commit()
    return {"created": created, "updated": updated, "total": len(devices)}
