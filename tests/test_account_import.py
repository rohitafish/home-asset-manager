"""Tests for discovery/account_import.py -- the vendor-account importer.
Uses fabricated serials throughout (never the real ones from devices/
accounts.json -- see AGENTS.md's PII section: a value used for the first
time in a fixture isn't caught by .pii-denylist, so this file must never
carry a real one)."""

import json
from datetime import date

import pytest
from conftest import make_asset, make_interface
from sqlmodel import select

from app.models import AssetNote, Location
from discovery.account_import import (
    AccountDevice,
    apply_changes,
    load_account_file,
    plan_changes,
    resolve_asset,
    run_account_import,
    sonos_serial_to_mac,
)

# -- load_account_file --------------------------------------------------------
# Regression coverage: a typo in this hand-maintained JSON file (a missing
# key, a malformed date) used to exit with a bare KeyError/ValueError naming
# neither the vendor nor which record was at fault.


def test_load_account_file_names_the_offending_record_on_a_missing_key(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({
        "amazon": {
            "source_document": "test",
            "devices": [
                {"account_name": "Echo Dot"},
                {"model": "Fire TV"},  # missing required account_name
            ],
        },
    }))

    with pytest.raises(ValueError) as exc:
        load_account_file(path)
    assert "amazon.devices[1]" in str(exc.value)
    assert str(path) in str(exc.value)


def test_load_account_file_names_the_offending_record_on_a_bad_date(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({
        "sonos": {
            "source_document": "test",
            "devices": [
                # strptime's %m/%d are lenient about missing zero-padding
                # ("2024-3-1" parses fine) -- this needs a date that's
                # actually unparseable to exercise the error path.
                {"account_name": "Living Room", "registered": "1st March 2024"},
            ],
        },
    }))

    with pytest.raises(ValueError) as exc:
        load_account_file(path)
    assert "sonos.devices[0]" in str(exc.value)


def test_load_account_file_parses_well_formed_entries(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({
        "amazon": {
            "source_document": "test",
            "devices": [{"account_name": "Echo Dot", "registered": "2024-03-01"}],
        },
    }))

    devices = load_account_file(path)

    assert len(devices) == 1
    assert devices[0].account_name == "Echo Dot"
    assert devices[0].registered == date(2024, 3, 1)


# -- sonos_serial_to_mac ----------------------------------------------------


def test_sonos_serial_to_mac_handles_account_display_format():
    assert sonos_serial_to_mac("AABBCCDDEEFF1") == "aa:bb:cc:dd:ee:ff"


def test_sonos_serial_to_mac_handles_probe_display_format():
    assert sonos_serial_to_mac("AA-BB-CC-DD-EE-FF:1") == "aa:bb:cc:dd:ee:ff"


def test_sonos_serial_to_mac_rejects_short_serial():
    assert sonos_serial_to_mac("AABBCC") is None


def test_sonos_serial_to_mac_rejects_none():
    assert sonos_serial_to_mac(None) is None


# -- resolve_asset ------------------------------------------------------------


def _device(**overrides) -> AccountDevice:
    defaults = dict(
        vendor="amazon",
        account_name="Test Device",
        model=None,
        model_number=None,
        serial=None,
        registered=None,
        asset_id=None,
        hostname=None,
        source_document="test.png",
        location=None,
    )
    defaults.update(overrides)
    return AccountDevice(**defaults)


def test_resolve_asset_pinned_asset_id_wins(session):
    asset = make_asset(session, hostname="Kitchen Echo")
    device = _device(asset_id=asset.id)

    resolved, reason = resolve_asset(session, device)

    assert resolved.id == asset.id
    assert reason is None


def test_resolve_asset_pinned_asset_id_missing_reports_unmatched(session):
    device = _device(asset_id=999999)

    resolved, reason = resolve_asset(session, device)

    assert resolved is None
    assert "999999" in reason


def test_resolve_asset_sonos_matches_by_serial_derived_mac(session):
    asset = make_asset(session, hostname="Sonos Move")
    make_interface(session, asset.id, mac="aa:bb:cc:dd:ee:ff")
    device = _device(vendor="sonos", serial="AABBCCDDEEFF1")

    resolved, reason = resolve_asset(session, device)

    assert resolved.id == asset.id
    assert reason is None


def test_resolve_asset_sonos_unmatched_serial_returns_none(session):
    device = _device(vendor="sonos", serial="AABBCCDDEEFF1")

    resolved, reason = resolve_asset(session, device)

    assert resolved is None
    assert "aa:bb:cc:dd:ee:ff" in reason


def test_resolve_asset_amazon_serial_not_decoded_as_sonos_mac(session):
    """Regression test: resolve_asset unconditionally ran every unpinned
    device's serial through sonos_serial_to_mac (which just strips non-hex
    characters from whatever it's given) -- an unpinned Amazon serial that
    happens to contain >=12 hex-valid characters could decode into a real
    MAC and silently match a totally unrelated asset. Same serial the Sonos
    matching test above uses (and the same asset/interface it matches), but
    here on an unpinned Amazon device: must NOT match."""
    asset = make_asset(session, hostname="Sonos Move")
    make_interface(session, asset.id, mac="aa:bb:cc:dd:ee:ff")
    device = _device(vendor="amazon", serial="AABBCCDDEEFF1")

    resolved, reason = resolve_asset(session, device)

    assert resolved is None
    assert "amazon" in reason


# -- plan_changes / apply_changes --------------------------------------------


def test_plan_changes_never_writes(session):
    asset = make_asset(session, hostname="Kitchen Echo", model=None)
    device = _device(asset_id=asset.id, model="Echo", serial="FAKE0000000A")

    plan_changes(session, [device])
    session.rollback()
    refreshed = session.get(type(asset), asset.id)

    assert refreshed.model is None


def test_apply_fills_serial_and_model_number(session):
    asset = make_asset(session, hostname="Kitchen Echo")
    device = _device(asset_id=asset.id, model="Echo", model_number="2nd generation", serial="FAKE0000000A")

    plan = plan_changes(session, [device])
    apply_changes(session, plan)
    session.refresh(asset)

    assert asset.serial_number == "FAKE0000000A"
    assert asset.model_number == "2nd generation"
    assert asset.model == "Echo"


def test_apply_skips_serial_and_model_number_when_identity_locked(session):
    asset = make_asset(session, hostname="Kitchen Echo", identity_locked=True, model="Echo (old)")
    device = _device(asset_id=asset.id, model="Echo", model_number="2nd generation", serial="FAKE0000000A")

    plan = plan_changes(session, [device])
    apply_changes(session, plan)
    session.refresh(asset)

    assert asset.serial_number is None
    assert asset.model_number is None
    assert asset.model == "Echo"  # model is not identity data -- still refreshed


def test_apply_does_not_overwrite_existing_purchase_date(session):
    asset = make_asset(session, hostname="Kitchen Echo", purchase_date=date(2018, 1, 1))
    device = _device(asset_id=asset.id, registered=date(2019, 11, 22))

    plan = plan_changes(session, [device])
    apply_changes(session, plan)
    session.refresh(asset)

    assert asset.purchase_date == date(2018, 1, 1)


def test_apply_fills_missing_purchase_date(session):
    asset = make_asset(session, hostname="Kitchen Echo", purchase_date=None)
    device = _device(asset_id=asset.id, registered=date(2019, 11, 22))

    plan = plan_changes(session, [device])
    apply_changes(session, plan)
    session.refresh(asset)

    assert asset.purchase_date == date(2019, 11, 22)


def test_apply_renames_locked_hostname_and_keeps_it_locked(session):
    asset = make_asset(session, hostname="Amazon Echo Dot Clock", hostname_locked=True)
    device = _device(asset_id=asset.id, hostname="Amazon Echo Dot Clock (Office)")

    plan = plan_changes(session, [device])
    apply_changes(session, plan)
    session.refresh(asset)

    assert asset.hostname == "Amazon Echo Dot Clock (Office)"
    assert asset.hostname_locked is True
    assert asset.hostname_source == "vendor_account"


def test_apply_does_not_touch_last_seen(session):
    asset = make_asset(session, hostname="Kitchen Echo")
    before = asset.last_seen
    device = _device(asset_id=asset.id, model="Echo")

    plan = plan_changes(session, [device])
    apply_changes(session, plan)
    session.refresh(asset)

    assert asset.last_seen == before


def test_apply_writes_one_imported_note_per_changed_asset(session):
    asset = make_asset(session, hostname="Kitchen Echo")
    device = _device(asset_id=asset.id, model="Echo", serial="FAKE0000000A")

    plan = plan_changes(session, [device])
    apply_changes(session, plan)

    notes = session.exec(select(AssetNote).where(AssetNote.asset_id == asset.id)).all()
    assert len(notes) == 1
    assert notes[0].author == "imported"


def test_apply_is_idempotent(session):
    asset = make_asset(session, hostname="Kitchen Echo")
    device = _device(asset_id=asset.id, model="Echo", serial="FAKE0000000A", registered=date(2019, 11, 22))

    apply_changes(session, plan_changes(session, [device]))
    second_plan = plan_changes(session, [device])
    apply_changes(session, second_plan)

    assert all(not change.fields for change in second_plan)
    notes = session.exec(select(AssetNote).where(AssetNote.asset_id == asset.id)).all()
    assert len(notes) == 1  # no duplicate note from the no-op second run


def test_apply_unmatched_device_creates_no_asset_and_no_note(session):
    device = _device(vendor="sonos", serial="FAKE0000000A")

    plan = plan_changes(session, [device])
    summary = apply_changes(session, plan)

    assert summary["unmatched"] == 1
    assert summary["updated"] == 0
    assert session.exec(select(AssetNote)).all() == []


# -- location ------------------------------------------------------------------


def test_apply_sets_location_when_unset(session):
    loc = Location(name="Living Room")
    session.add(loc)
    session.commit()
    asset = make_asset(session, hostname="Sonos Sub", location_id=None)
    device = _device(vendor="sonos", asset_id=asset.id, location="Living Room")

    plan = plan_changes(session, [device])
    apply_changes(session, plan)
    session.refresh(asset)

    assert asset.location_id == loc.id


def test_apply_skips_location_when_already_set(session):
    living_room = Location(name="Living Room")
    kitchen = Location(name="Kitchen")
    session.add(living_room)
    session.add(kitchen)
    session.commit()
    asset = make_asset(session, hostname="Sonos Sub", location_id=kitchen.id)
    device = _device(vendor="sonos", asset_id=asset.id, location="Living Room")

    plan = plan_changes(session, [device])
    apply_changes(session, plan)
    session.refresh(asset)

    assert asset.location_id == kitchen.id
    assert "location: already set" in plan[0].skipped


def test_apply_skips_location_when_name_not_found(session):
    asset = make_asset(session, hostname="Sonos Sub", location_id=None)
    device = _device(vendor="sonos", asset_id=asset.id, location="Nonexistent Room")

    plan = plan_changes(session, [device])
    apply_changes(session, plan)
    session.refresh(asset)

    assert asset.location_id is None
    assert any("no Location named" in s for s in plan[0].skipped)


# -- format_plan + run_account_import: the dry-run gate ------------------------
# The third of the three plan-then-apply importers (see the matching sections
# in tests/test_revaluation.py and tests/test_exposure_resync.py). Same shape,
# same risk: plan_changes and apply_changes were covered, the wrapper holding
# the `if dry_run: return` gate was not.
#
# This one also goes through load_account_file, so unlike its two siblings it
# needs a real file on disk -- which is the point: `account-import` without
# --apply is the command someone runs to check a freshly transcribed
# accounts.json, on the promise that it only reads.


def _accounts_file(tmp_path, asset_id):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({
        "amazon": {
            "source_document": "test",
            "devices": [{
                "account_name": "Kitchen Echo",
                "model": "Echo",
                "model_number": "2nd generation",
                "serial": "FAKE0000000A",
                "asset_id": asset_id,
            }],
        },
    }))
    return path


def test_run_dry_run_reports_the_change_without_making_it(session, tmp_path):
    asset = make_asset(session, hostname="Kitchen Echo")
    path = _accounts_file(tmp_path, asset.id)

    summary = run_account_import(path, dry_run=True, session=session)

    assert summary["applied"] is False
    assert summary["records"] == 1
    session.refresh(asset)
    assert asset.serial_number is None, "a dry run must not write"
    assert asset.model_number is None
    assert session.exec(select(AssetNote)).all() == []


def test_run_defaults_to_a_dry_run(session, tmp_path):
    asset = make_asset(session, hostname="Kitchen Echo")
    path = _accounts_file(tmp_path, asset.id)

    summary = run_account_import(path, session=session)

    assert summary["applied"] is False
    session.refresh(asset)
    assert asset.serial_number is None


def test_run_with_dry_run_false_actually_applies(session, tmp_path):
    asset = make_asset(session, hostname="Kitchen Echo")
    path = _accounts_file(tmp_path, asset.id)

    summary = run_account_import(path, dry_run=False, session=session)

    assert summary["applied"] is True
    assert summary["matched"] == 1
    assert summary["updated"] == 1
    session.refresh(asset)
    assert asset.serial_number == "FAKE0000000A"


def test_format_plan_shows_each_field_transition_for_an_update(session, tmp_path):
    asset = make_asset(session, hostname="Kitchen Echo")
    path = _accounts_file(tmp_path, asset.id)

    plan_text = run_account_import(path, dry_run=True, session=session)["plan"]

    assert "UPDATE" in plan_text
    assert "Kitchen Echo" in plan_text
    assert "serial_number: None -> 'FAKE0000000A'" in plan_text


def test_format_plan_explains_why_a_record_went_unmatched(session, tmp_path):
    """An unmatched record is the common case on a first import and the whole
    reason to read the diff -- it has to say which record and why, not just
    drop the row."""
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({
        "amazon": {
            "source_document": "test",
            "devices": [{"account_name": "Unpinned Echo"}],
        },
    }))

    plan_text = run_account_import(path, dry_run=True, session=session)["plan"]

    assert "UNMATCHED" in plan_text
    assert "Unpinned Echo" in plan_text
    assert "must be matched by hand" in plan_text
