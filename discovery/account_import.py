"""Importer for vendor-account exports -- currently Amazon (Echo) and Sonos --
transcribed by hand into devices/accounts.json because neither vendor offers
a consumer API a home user can call (see README's "Vendor account import"
section for why, and probes/sonos.py for the one Sonos API that *is* usable,
the local port-1400 protocol, which a future discovery/sonos.py should use to
replace the Sonos half of this file).

Deliberately does NOT go through discovery.reconcile.reconcile_into_db:

  * reconcile's hostname-write branch requires the new source to score
    >= the existing hostname_source under _HOSTNAME_SOURCE_PRIORITY, and an
    unlisted source always scores 0 -- so it could never rename the three
    "Amazon Echo Dot Clock" duplicates this import exists to fix. Adding
    this source to that map instead would let it win during *ordinary*
    discovery runs too, which is wrong -- a one-off manual import is a
    different kind of trust than a network collector.
  * reconcile creates a new Asset on no match. Here a no-match (e.g. the
    Sonos Sub, which UniFi has never seen because its mesh MAC isn't in any
    AssetInterface yet) is a fact worth reporting, not a phantom asset with
    no network presence to invent.
  * reconcile always bumps last_seen. A vendor account listing is not
    evidence the device was seen on the network just now, and last_seen
    feeds the dashboard's 30-day coverage metric -- so it must never move
    here.

Matching:
  * Amazon has no field in the account export that maps onto anything the
    network side collects (the serial is Amazon's own, not a MAC) --
    devices/accounts.json pins amazon devices to an asset_id by hand,
    written once by a human after confirming the room/model against the
    dashboard.
  * A Sonos serial is deterministic: the player's MAC address plus one
    trailing check character (verified against a live probe of a real
    Sonos Move -- see accounts.json's "sonos" section and probes/sonos.py).
    So Sonos devices match by deriving the MAC and reusing
    discovery.reconcile._find_asset_by_mac, no pin required.

Everything here is read-plan-then-apply: plan_changes() never writes:
run_account_import(dry_run=True) (the default) prints the full diff and
returns without touching the session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlmodel import Session, select

from app.clock import utcnow_naive
from app.models import Asset, AssetNote, Location
from discovery.normalize import normalize_mac
from discovery.reconcile import _find_asset_by_mac

SOURCE = "vendor_account"


@dataclass(frozen=True)
class AccountDevice:
    vendor: str  # "amazon" | "sonos"
    account_name: str
    model: str | None
    model_number: str | None
    serial: str | None
    registered: date | None
    asset_id: int | None  # Amazon: pinned by hand. Sonos: always None, matched by serial.
    hostname: str | None  # only set for entries that should rename their asset
    source_document: str
    location: str | None = None  # exact Location.name to fill in, if currently unset


@dataclass
class PlannedChange:
    device: AccountDevice
    asset: Asset | None
    fields: dict[str, tuple[object, object]] = field(default_factory=dict)  # name -> (old, new)
    skipped: list[str] = field(default_factory=list)  # "field: why not written"
    unmatched_reason: str | None = None
    location_id: int | None = None  # resolved Location.id, when "location" is in fields

    @property
    def will_write(self) -> bool:
        return self.asset is not None and bool(self.fields)


def sonos_serial_to_mac(serial: str | None) -> str | None:
    """A Sonos account serial is the 12 hex-digit MAC plus one trailing check
    character, printed with or without separators (account pages print e.g.
    "AABBCCDDEEFF1"; probes/sonos.py's live serial field prints
    "AA-BB-CC-DD-EE-FF:1"). Strips all non-hex characters, keeps the leading
    12, and normalizes them the same way every other MAC in this schema is
    stored. Returns None rather than guessing if that isn't at least 12 hex
    characters."""
    if not serial:
        return None
    hexonly = "".join(c for c in serial if c in "0123456789abcdefABCDEF")
    if len(hexonly) < 12:
        return None
    return normalize_mac(hexonly[:12])


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_account_file(path: str | Path) -> list[AccountDevice]:
    data = json.loads(Path(path).read_text())
    devices: list[AccountDevice] = []
    for vendor in ("amazon", "sonos"):
        section = data.get(vendor)
        if not section:
            continue
        source_document = section.get("source_document", "")
        for idx, entry in enumerate(section.get("devices", [])):
            try:
                devices.append(
                    AccountDevice(
                        vendor=vendor,
                        account_name=entry["account_name"],
                        model=entry.get("model"),
                        model_number=entry.get("model_number"),
                        serial=entry.get("serial"),
                        registered=_parse_date(entry.get("registered")),
                        asset_id=entry.get("asset_id"),
                        hostname=entry.get("hostname"),
                        source_document=source_document,
                        location=entry.get("location"),
                    )
                )
            except (KeyError, ValueError) as exc:
                # entry["account_name"] is a bare index (not .get()), and
                # _parse_date's strptime has no try/except -- a typo in this
                # hand-maintained file (a missing key, "2024-3-1" instead of
                # zero-padded) used to exit with a bare KeyError/ValueError
                # naming neither the vendor nor which record was malformed.
                raise ValueError(f"{vendor}.devices[{idx}] in {path} is malformed: {exc}") from exc
    return devices


def resolve_asset(session: Session, device: AccountDevice) -> tuple[Asset | None, str | None]:
    """Returns (asset, unmatched_reason). Exactly one of the two is set."""
    if device.asset_id is not None:
        asset = session.get(Asset, device.asset_id)
        if asset is None:
            return None, f"pinned asset_id {device.asset_id} does not exist"
        return asset, None

    # sonos_serial_to_mac's decoding is Sonos-specific (the account serial
    # format IS the MAC, per its docstring) -- an unpinned Amazon device
    # (asset_id is None, per AccountDevice's "Amazon: pinned by hand" /
    # "Sonos: always None, matched by serial" contract) has no such
    # relationship between its serial and any MAC. Without this guard, an
    # Amazon serial that happens to contain >=12 hex-valid characters
    # decoded into a fake MAC that could coincidentally match -- and then
    # write, potentially locked -- an unrelated asset's identity fields.
    if device.vendor != "sonos":
        return None, f"vendor {device.vendor!r} has no asset_id pinned -- Amazon devices must be matched by hand"
    mac = sonos_serial_to_mac(device.serial)
    if mac is None:
        return None, f"serial {device.serial!r} does not decode to a MAC"
    asset = _find_asset_by_mac(session, mac)
    if asset is None:
        return None, f"no asset interface with MAC {mac} (derived from serial {device.serial})"
    return asset, None


def plan_changes(session: Session, devices: list[AccountDevice]) -> list[PlannedChange]:
    plan: list[PlannedChange] = []
    for device in devices:
        asset, unmatched_reason = resolve_asset(session, device)
        change = PlannedChange(device=device, asset=asset, unmatched_reason=unmatched_reason)
        if asset is None:
            plan.append(change)
            continue

        if device.serial and asset.serial_number != device.serial:
            if asset.identity_locked:
                change.skipped.append("serial_number: identity_locked")
            else:
                change.fields["serial_number"] = (asset.serial_number, device.serial)

        if device.model_number and asset.model_number != device.model_number:
            if asset.identity_locked:
                change.skipped.append("model_number: identity_locked")
            else:
                change.fields["model_number"] = (asset.model_number, device.model_number)

        if device.model and asset.model != device.model:
            change.fields["model"] = (asset.model, device.model)

        if device.registered:
            if asset.purchase_date is None:
                change.fields["purchase_date"] = (asset.purchase_date, device.registered)
            else:
                change.skipped.append("purchase_date: already set")

        if device.hostname and asset.hostname != device.hostname:
            change.fields["hostname"] = (asset.hostname, device.hostname)

        if device.location:
            # Exact match only -- unlike app/assistant.py's set_location
            # applier, deliberately does not auto-create a missing Location:
            # this table is a small, curated, human-maintained list (see
            # app/models.py's Location docstring), and a typo in a
            # hand-edited accounts.json shouldn't silently grow it.
            location = session.exec(select(Location).where(Location.name == device.location)).first()
            if location is None:
                change.skipped.append(f"location: no Location named {device.location!r}")
            elif asset.location_id is None:
                change.fields["location"] = (None, location.name)
                change.location_id = location.id
            else:
                change.skipped.append("location: already set")

        plan.append(change)
    return plan


def _note_body(change: PlannedChange) -> str:
    device = change.device
    lines = [
        f"Imported from {device.vendor.title()} account ({device.source_document}).",
        f"Account device name: {device.account_name!r}.",
    ]
    if device.serial:
        lines.append(f"Serial: {device.serial}.")
    if device.registered:
        lines.append(
            f"Registered {device.registered.isoformat()} -- used as purchase_date "
            "(approximate: vendor registration, not a purchase receipt)."
            if "purchase_date" in change.fields
            else f"Registered {device.registered.isoformat()}."
        )
    for name, (old, new) in change.fields.items():
        lines.append(f"  {name}: {old!r} -> {new!r}")
    if change.skipped:
        lines.append("Not written: " + "; ".join(change.skipped))
    return "\n".join(lines)


def apply_changes(session: Session, plan: list[PlannedChange]) -> dict[str, int]:
    matched = 0
    unmatched = 0
    updated = 0
    notes = 0
    for change in plan:
        if change.asset is None:
            unmatched += 1
            continue
        matched += 1
        if not change.fields:
            continue

        asset = change.asset
        for name, (_old, new) in change.fields.items():
            if name == "hostname":
                asset.hostname = new
                asset.hostname_source = SOURCE
                asset.hostname_locked = True
            elif name == "location":
                asset.location_id = change.location_id
            else:
                setattr(asset, name, new)
        session.add(asset)
        session.add(
            AssetNote(
                asset_id=asset.id,
                created_at=utcnow_naive(),
                author="imported",
                body=_note_body(change),
            )
        )
        updated += 1
        notes += 1
    session.commit()
    return {"matched": matched, "unmatched": unmatched, "updated": updated, "notes": notes}


def format_plan(plan: list[PlannedChange]) -> str:
    lines = []
    for change in plan:
        device = change.device
        if change.asset is None:
            lines.append(f"UNMATCHED  {device.vendor}:{device.account_name!r} -- {change.unmatched_reason}")
            continue
        header = f"asset id={change.asset.id} ({change.asset.hostname!r})  <-  {device.vendor}:{device.account_name!r}"
        if not change.fields and not change.skipped:
            lines.append(f"no-op      {header}")
            continue
        lines.append(f"UPDATE     {header}")
        for name, (old, new) in change.fields.items():
            lines.append(f"             {name}: {old!r} -> {new!r}")
        for reason in change.skipped:
            lines.append(f"             skip {reason}")
    return "\n".join(lines)


def run_account_import(path: str | Path, dry_run: bool = True, session: Session | None = None) -> dict:
    devices = load_account_file(path)
    owns_session = session is None
    if owns_session:
        from app.db import engine

        session = Session(engine)
    try:
        plan = plan_changes(session, devices)
        summary = {
            "path": str(path),
            "records": len(devices),
            "plan": format_plan(plan),
        }
        if dry_run:
            summary["applied"] = False
            return summary
        summary.update(apply_changes(session, plan))
        summary["applied"] = True
        return summary
    finally:
        if owns_session:
            session.close()
