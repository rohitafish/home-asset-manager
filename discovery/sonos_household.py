"""Sonos household discovery: enumerates every player in the household from
one GetZoneGroupState call against a reachable Sonos player, falling back
across seeds until one describes the whole household (see
enumerate_household), using the same local port-1400 protocol probes/sonos.py
already speaks (shared
parsing lives in probes/sonos_api.py, a protocol-only module deliberately
kept independent of asset/DB knowledge -- see its docstring for why that's
safe to import from here).

Deliberately does not use multicast/mDNS SSDP discovery -- this network's
VLANs don't carry multicast, the same reason probes/ssdp.py's M-SEARCH is
unicast (see that module's docstring). Instead this seeds from Sonos IPs
already known to the DB (any asset whose vendor/hostname mentions "sonos",
or with an AssetService on port 1400) -- the same substring check
probes/sonos.py's applies_to() already uses.

Goes through discovery.reconcile.reconcile_into_db, unlike
discovery/account_import.py: this *is* live network evidence (last_seen
should move, and a genuinely new player -- one UniFi has never associated a
name with -- should become a new asset, not just a report).

Bonded satellites (a Sub, rear surrounds) report their *group's* room name
in their own zone data, not their own identity -- so hostname is
deliberately left unset for a satellite here; only the group's primary/
visible player gets a hostname suggestion, and even then only via
reconcile_into_db's own create-only hostname-write path (this module's
source is deliberately absent from _HOSTNAME_SOURCE_PRIORITY, so it can
never overwrite an existing hostname -- it can only ever name a genuinely
new asset). This is the same class of naming bug this app has hit twice by
hand this session (three "Amazon Echo Dot Clock"s, then needing to manually
work out the Play:1 Left/Right Rear split) -- the design here avoids
recreating it automatically.
"""

from __future__ import annotations

import logging

import httpx
from sqlmodel import Session, select

from app.models import Asset, AssetInterface, AssetService
from discovery.normalize import DiscoveredDevice
from discovery.reconcile import reconcile_into_db
from probes.sonos_api import (
    SonosPlayer,
    _is_fetchable_lan_ip,
    enrich_from_device_description,
    fetch_zone_group_state,
    parse_zone_group_members,
)

logger = logging.getLogger(__name__)

SOURCE = "sonos_household"
_TIMEOUT = 3.0


def candidate_seed_ips(session: Session) -> list[str]:
    """Every IP that looks like it might already be a Sonos player, most-
    recently-seen first -- reaching one *joined* player is enough to
    enumerate the whole household via GetZoneGroupState, but a player
    running as its own standalone system answers with only itself, so
    enumerate_household works down this list rather than stopping at the
    first seed that answers at all."""
    sonos_asset_ids: set[int] = set()
    for asset in session.exec(select(Asset)).all():
        haystack = f"{asset.hostname or ''} {asset.vendor or ''}".lower()
        if "sonos" in haystack:
            sonos_asset_ids.add(asset.id)
    for svc in session.exec(select(AssetService).where(AssetService.port == 1400)).all():
        sonos_asset_ids.add(svc.asset_id)

    if not sonos_asset_ids:
        return []

    ifaces = session.exec(
        select(AssetInterface)
        .where(AssetInterface.asset_id.in_(sonos_asset_ids), AssetInterface.ip.is_not(None))
        .order_by(AssetInterface.last_seen.desc())
    ).all()
    seen: set[str] = set()
    ips: list[str] = []
    for iface in ifaces:
        if iface.ip not in seen:
            seen.add(iface.ip)
            ips.append(iface.ip)
    return ips


def enumerate_household(ips: list[str], timeout: float = _TIMEOUT) -> tuple[list[SonosPlayer], str | None]:
    """Tries every seed IP for the *household*, not just the first that
    answers at all: a player properly joined to the household describes
    every bonded zone group in its GetZoneGroupState response, not just its
    own, but a player currently running as its own standalone Sonos system
    (e.g. a Move used away from home) answers with only itself. Returning
    on that first non-empty answer used to hide a real multi-player
    household whose seed came later in the list -- observed in production,
    where a most-recently-seen Move shadowed a bonded 4-player Living Room
    group. So the first seed reporting more than one player wins; a
    single-player answer is remembered as a fallback rather than returned
    immediately, in case a later seed does better.

    Bounded by the seed list, not a search: each IP is tried at most once,
    with no retries and no rescans -- if every seed ever reports only
    itself, that single-player answer *is* the answer (a household that
    genuinely has one Sonos player), not a reason to keep looking.

    Returns (players, error) -- error is None on success. Never raises: a
    seed that's powered off or unreachable is an expected outcome, not a
    failure, as long as *some* seed answers."""
    fallback: list[SonosPlayer] | None = None
    with httpx.Client(timeout=timeout) as client:
        for ip in ips:
            soap_xml = fetch_zone_group_state(client, ip)
            if not soap_xml:
                continue
            try:
                players = parse_zone_group_members(soap_xml)
            except Exception:
                # Expected while scanning seeds -- a malformed/partial response
                # from one IP just means try the next. Debug, not warning.
                logger.debug("Sonos seed %s returned unparseable topology", ip, exc_info=True)
                continue
            if len(players) > 1:
                return players, None
            if players and fallback is None:
                logger.debug("Sonos seed %s reported only itself, trying the next seed", ip)
                fallback = players
    if fallback is not None:
        return fallback, None
    return [], "No Sonos player responded on any known IP -- they may be powered off."


def to_discovered_devices(players: list[SonosPlayer]) -> list[DiscoveredDevice]:
    devices = []
    for p in players:
        if not p.mac:
            continue
        if not _is_fetchable_lan_ip(p.ip):
            # Each player entry in a GetZoneGroupState response is
            # self-reported by whatever device answered the seed IP, not
            # independently verified by a fresh connection to p.ip itself --
            # a compromised/spoofed responder can claim arbitrary "other
            # players" with attacker-chosen ip values, and reconcile_into_db
            # would otherwise merge that straight into the asset inventory
            # as real evidence. Reusing the same LAN-IP guard
            # fetch_device_description's caller already applies to this
            # field (probes/sonos_api.py) closes the same trust boundary for
            # this second consumer of it.
            logger.debug(
                "Sonos player %s has an unfetchable/spoofed-looking ip=%r, skipping", p.mac, p.ip,
            )
            continue
        hostname = None
        if not p.is_satellite and p.room_name:
            model_label = p.model or "Sonos"
            hostname = (
                model_label if p.room_name.lower() in model_label.lower()
                else f"{model_label} ({p.room_name})"
            )
        devices.append(DiscoveredDevice(
            mac=p.mac,
            ip=p.ip,
            hostname=hostname,
            asset_type="iot",
            vendor="Sonos",
            model=p.model,
            firmware_version=p.software_version,
            serial_number=p.serial,
            model_number=p.model_number,
            source=SOURCE,
        ))
    return devices


def run_sonos_household_discovery(session: Session, dry_run: bool = False) -> dict:
    """Never raises -- every failure mode (no seed IPs known yet, nothing
    responded) is reported in the returned summary rather than treated as
    an error, matching discovery/local_host.py's contract."""
    seeds = candidate_seed_ips(session)
    if not seeds:
        return {"status": "no_seed_ips", "detail": "No Sonos-looking asset with a known IP yet."}

    players, error = enumerate_household(seeds, timeout=_TIMEOUT)
    if error:
        return {"status": "no_response", "detail": error}

    enrich_from_device_description(players, timeout=_TIMEOUT)
    devices = to_discovered_devices(players)

    if dry_run:
        return {
            "status": "ok",
            "dry_run": True,
            "players": [
                {
                    "mac": d.mac, "ip": d.ip, "hostname": d.hostname, "model": d.model,
                    "model_number": d.model_number, "serial_number": d.serial_number,
                    "firmware_version": d.firmware_version,
                }
                for d in devices
            ],
        }

    summary = reconcile_into_db(session, devices)
    summary["status"] = "ok"
    return summary
