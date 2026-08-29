"""Route-level tests for app/routers/dashboard.py.

The pure helpers this router is built from are covered exhaustively in
tests/test_dashboard_helpers.py. What could not be covered there is
everything that only exists once a request runs: that the Jinja templates
render at all, that _csv_safe is applied to every field rather than merely
existing, that the destructive routes delete what they claim to and nothing
more, and that each discovery trigger actually consults the
already-running guard it repeats.

Scope note -- the smoke tests below assert status codes and a couple of
landmark strings, not page layout. They exist to catch the failure this app
had no defence against at all: 16 templates and ~1000 lines of Jinja that
nothing rendered, so a typo'd variable, a filter that stopped being
registered, or a renamed model attribute reached the Mini as a 500 on a page
nobody opened until they needed it.
"""

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from conftest import make_asset, make_interface
from sqlmodel import select

from app.models import (
    Asset,
    AssetInterface,
    AssetNote,
    AssetType,
    ChangeProposal,
    Criticality,
    DiscoveryRun,
    Exposure,
    Finding,
    FindingStatus,
    LifecycleStatus,
    Location,
    Severity,
)


@pytest.fixture()
def populated(session):
    """One asset carrying enough related data that the detail/summary pages
    exercise their loops rather than their empty states."""
    location = Location(name="Study")
    session.add(location)
    session.commit()
    session.refresh(location)

    asset = make_asset(
        session,
        hostname="study-nas",
        vendor="Synology",
        model="DS220+",
        asset_type=AssetType.server,
        criticality=Criticality.high,
        location_id=location.id,
        purchase_price=Decimal("399.99"),
        purchase_date=date(2024, 3, 1),
        replacement_value=Decimal("430.00"),
        warranty_expiry=date(2027, 3, 1),
        last_seen=datetime(2026, 8, 1, 12, 0),
    )
    make_interface(session, asset.id, ip="192.168.1.20", mac="aa:bb:cc:dd:ee:ff")
    session.add(
        Finding(
            asset_id=asset.id,
            severity=Severity.high,
            exposure=Exposure.internal,
            detected_date=datetime(2026, 7, 1),
            status=FindingStatus.open,
        )
    )
    session.add(
        AssetNote(
            asset_id=asset.id, created_at=datetime(2026, 7, 2, 9, 30), author="test", body="A note"
        )
    )
    session.commit()
    return asset


# -- template rendering -------------------------------------------------------

PAGES = [
    "/readme",
    "/assets",
    "/assets/count-badge",
    "/assets/new",
    "/assets/triage",
    "/assets/duplicates",
    "/assets/investigate",
    "/valuables",
    "/locations",
    "/findings",
    "/discovery",
    "/summary",
]


@pytest.mark.parametrize("path", PAGES)
def test_page_renders_with_data(admin_client, populated, path):
    response = admin_client.get(path)
    assert response.status_code == 200, f"{path} failed to render: {response.text[:400]}"


@pytest.mark.parametrize("path", PAGES)
def test_page_renders_on_an_empty_database(admin_client, session, path):
    """Empty states go through different template branches than populated
    ones, and a brand-new install sees these first."""
    response = admin_client.get(path)
    assert response.status_code == 200, f"{path} failed to render: {response.text[:400]}"


def test_asset_detail_and_edit_render(admin_client, populated):
    detail = admin_client.get(f"/assets/{populated.id}")
    assert detail.status_code == 200
    assert "study-nas" in detail.text

    edit = admin_client.get(f"/assets/{populated.id}/edit")
    assert edit.status_code == 200
    assert "DS220+" in edit.text


def test_location_detail_renders(admin_client, populated, session):
    location = session.exec(select(Location)).one()
    response = admin_client.get(f"/locations/{location.id}")
    assert response.status_code == 200
    assert "study-nas" in response.text


def test_asset_detail_for_an_unknown_id_redirects_to_the_list(admin_client, session):
    """Not a 404: these are browser pages, and there is no error template --
    a stale bookmark or a link to a since-deleted asset lands back on the
    asset list rather than on a bare Starlette error page."""
    response = admin_client.get("/assets/9999", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/assets"


def test_index_redirects_to_the_asset_list(admin_client):
    response = admin_client.get("/", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/assets"


def test_money_and_date_filters_render_through_a_real_page(admin_client, populated):
    """app/template_filters.py's money() is registered as a Jinja filter and
    reached only through a template -- this is the one place the registration
    itself is exercised. A filter that failed to register raises
    TemplateAssertionError at render time, not import time."""
    response = admin_client.get("/valuables")
    assert response.status_code == 200
    assert "£399.99" in response.text


# -- /valuables.csv -----------------------------------------------------------
# _csv_safe is unit-tested in tests/test_dashboard_helpers.py. What that
# can't see is whether it's actually *applied*, and to which columns: the
# route hand-writes fourteen fields, five of them device-supplied free text.
# A new column added without the wrapper reintroduces the spreadsheet-formula
# injection the helper exists to stop, and no helper-level test would notice.


def _csv_rows(response):
    body = response.text.lstrip("﻿")
    return list(csv.reader(io.StringIO(body)))


def test_csv_export_neutralizes_a_formula_in_every_device_supplied_field(
    admin_client, session
):
    """hostname, vendor, model, model_number, model_identifier and
    serial_number can all be written by a discovery collector or probe from
    what a LAN device reported -- so all six are treated as hostile here."""
    make_asset(
        session,
        hostname="=cmd|'/c calc'!A1",
        vendor="+SUM(1+1)",
        model="-2+3",
        model_number="@echo",
        model_identifier="\rcarriage",
        serial_number="\ttab",
        purchase_price=Decimal("100.00"),
        asset_type=AssetType.iot,
    )

    rows = _csv_rows(admin_client.get("/valuables.csv"))

    assert len(rows) == 2, "expected a header and exactly one data row"
    for value in rows[1]:
        assert not value.startswith(("=", "+", "-", "@", "\t", "\r")), (
            f"{value!r} would be evaluated as a formula when opened in a spreadsheet"
        )
    assert rows[1][1] == "'=cmd|'/c calc'!A1"


def test_csv_export_leaves_ordinary_values_alone(admin_client, populated):
    rows = _csv_rows(admin_client.get("/valuables.csv"))

    data = rows[1]
    assert data[1] == "study-nas"
    assert data[3] == "Synology"
    assert data[8] == "Study"
    assert data[10] == "399.99"


def test_csv_export_starts_with_a_utf8_bom_and_the_header(admin_client, populated):
    """The BOM is what stops Excel mangling the £ symbols on Windows -- it's
    the reason this export exists in this shape at all."""
    response = admin_client.get("/valuables.csv")

    assert response.text.startswith("﻿")
    assert _csv_rows(response)[0][0] == "ID"


def test_csv_export_is_served_as_a_download(admin_client, populated):
    response = admin_client.get("/valuables.csv")

    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "asset-register-" in response.headers["content-disposition"]


def test_csv_export_all_parameter_widens_the_selection(admin_client, session):
    """Without ?all, the export is the insurance-relevant subset; with it,
    everything. Both are linked from the Valuables page."""
    make_asset(session, hostname="valuable", purchase_price=Decimal("500.00"),
               asset_type=AssetType.iot)
    make_asset(session, hostname="unpriced", asset_type=AssetType.iot,
               criticality=Criticality.low)

    default_rows = _csv_rows(admin_client.get("/valuables.csv"))
    all_rows = _csv_rows(admin_client.get("/valuables.csv?all=1"))

    assert len(all_rows) > len(default_rows)
    assert any(r[1] == "unpriced" for r in all_rows[1:])
    assert not any(r[1] == "unpriced" for r in default_rows[1:])


# -- destructive routes -------------------------------------------------------


def test_delete_removes_the_asset_and_its_children(admin_client, populated, session):
    """There is no ON DELETE CASCADE on these foreign keys -- the route has
    to clear the children itself or Postgres rejects the delete. SQLite's
    foreign_keys pragma is on in conftest, so a missed child model fails
    here the same way it would in production."""
    asset_id = populated.id

    response = admin_client.post(f"/assets/{asset_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/assets"
    assert session.get(Asset, asset_id) is None
    assert session.exec(select(Finding).where(Finding.asset_id == asset_id)).all() == []
    assert session.exec(select(AssetNote).where(AssetNote.asset_id == asset_id)).all() == []


def test_delete_of_an_unknown_asset_is_a_harmless_redirect(admin_client, session):
    response = admin_client.post("/assets/9999/delete", follow_redirects=False)
    assert response.status_code == 303


def test_merge_pair_folds_the_duplicate_into_the_survivor(admin_client, session):
    survivor = make_asset(session, hostname="keeper")
    duplicate = make_asset(session, hostname="goner")
    make_interface(session, duplicate.id, ip="10.0.0.9", mac="11:22:33:44:55:66")

    response = admin_client.post(
        "/assets/duplicates/merge-pair",
        data={"survivor_id": survivor.id, "id_a": survivor.id, "id_b": duplicate.id},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert session.get(Asset, duplicate.id) is None
    moved = session.exec(
        select(AssetInterface).where(AssetInterface.asset_id == survivor.id)
    ).all()
    assert [i.ip for i in moved] == ["10.0.0.9"], "the duplicate's interface should move, not vanish"


def test_merge_pair_refuses_a_survivor_that_is_not_in_the_pair(admin_client, session):
    """The survivor comes from a radio button and the pair from hidden form
    fields -- a mismatched post must delete nothing rather than pick one."""
    a = make_asset(session, hostname="a")
    b = make_asset(session, hostname="b")
    unrelated = make_asset(session, hostname="bystander")

    admin_client.post(
        "/assets/duplicates/merge-pair",
        data={"survivor_id": unrelated.id, "id_a": a.id, "id_b": b.id},
        follow_redirects=False,
    )

    assert session.get(Asset, a.id) is not None
    assert session.get(Asset, b.id) is not None
    assert session.get(Asset, unrelated.id) is not None


def test_merge_by_hostname_keeps_one_row_per_hostname(admin_client, session):
    survivor = make_asset(session, hostname="twin")
    other = make_asset(session, hostname="twin")
    make_interface(session, other.id, ip="10.0.0.4", mac="aa:00:00:00:00:01")

    admin_client.post(
        "/assets/duplicates/merge",
        data={"survivor_id": survivor.id},
        follow_redirects=False,
    )

    remaining = session.exec(select(Asset).where(Asset.hostname == "twin")).all()
    assert [a.id for a in remaining] == [survivor.id]


# -- proposal application -----------------------------------------------------


def _proposal(session, asset_id, field_name="model", value="Proposed", **overrides):
    defaults = dict(
        asset_id=asset_id,
        origin_asset_id=asset_id,
        kind="set_field",
        payload_json=json.dumps({"field_name": field_name, "value": value}),
        created_at=datetime(2026, 8, 1, 10, 0),
    )
    defaults.update(overrides)
    proposal = ChangeProposal(**defaults)
    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    return proposal


def test_apply_a_single_proposal_writes_it_and_marks_it_applied(
    admin_client, populated, session
):
    proposal = _proposal(session, populated.id, value="DS923+")

    response = admin_client.post(f"/proposals/{proposal.id}/apply", follow_redirects=False)

    assert response.status_code == 303
    session.refresh(populated)
    session.refresh(proposal)
    assert populated.model == "DS923+"
    assert proposal.status == "applied"


def test_discard_a_proposal_leaves_the_asset_untouched(admin_client, populated, session):
    proposal = _proposal(session, populated.id, value="Never applied")

    admin_client.post(f"/proposals/{proposal.id}/discard", follow_redirects=False)

    session.refresh(populated)
    session.refresh(proposal)
    assert populated.model == "DS220+"
    assert proposal.status == "discarded"


def test_apply_all_applies_every_pending_proposal_for_the_asset(
    admin_client, populated, session
):
    _proposal(session, populated.id, field_name="model", value="Applied A")
    _proposal(session, populated.id, field_name="vendor", value="Applied B")

    response = admin_client.post(
        f"/assets/{populated.id}/proposals/apply-all", follow_redirects=False
    )

    assert response.status_code == 303
    session.refresh(populated)
    assert populated.model == "Applied A"
    assert populated.vendor == "Applied B"
    assert all(
        p.status == "applied" for p in session.exec(select(ChangeProposal)).all()
    )


# -- discovery triggers -------------------------------------------------------
# Every one of these seven routes repeats the same guard:
#   if _discovery_already_running(session): return RedirectResponse(...)
# _discovery_already_running itself is unit-tested; that each route actually
# calls it is not something a helper test can show, and a new trigger route
# copied from a sibling is exactly where the check gets dropped. Starting a
# second scan while one is in flight is what this prevents -- on the Mini
# that means two nmap runs competing over the same subnets.

DISCOVERY_TRIGGERS = [
    ("/discovery/run/unifi", "run_unifi_discovery"),
    ("/discovery/run/nmap", "run_nmap_discovery"),
    ("/discovery/run/nmap-privileged", "run_nmap_discovery"),
    ("/discovery/run/local-mac", "run_local_host_discovery"),
    ("/discovery/run/enrich", "run_enrichment"),
    ("/discovery/run/sonos", "run_sonos_discovery"),
    ("/discovery/run/all", "run_all_discovery"),
]


@pytest.fixture()
def collector_spy(monkeypatch):
    """Replaces every collector on discovery.cli with a recorder. The routes
    import them inside the function body, so patching the module attribute is
    enough -- and is what keeps this suite off the network and away from
    nmap/sudo."""
    from discovery import cli

    calls = []
    for _, name in DISCOVERY_TRIGGERS:
        monkeypatch.setattr(
            cli, name, lambda *a, _n=name, **kw: calls.append(_n) or {"ran": _n}
        )
    return calls


@pytest.mark.parametrize(("path", "collector"), DISCOVERY_TRIGGERS)
def test_discovery_trigger_starts_its_collector(admin_client, collector_spy, path, collector):
    response = admin_client.post(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/discovery"
    assert collector_spy == [collector]


@pytest.mark.parametrize(("path", "collector"), DISCOVERY_TRIGGERS)
def test_discovery_trigger_refuses_while_a_run_is_in_flight(
    admin_client, session, collector_spy, path, collector
):
    session.add(DiscoveryRun(source="nmap", status="running", started_at=datetime(2026, 8, 1)))
    session.commit()

    response = admin_client.post(path, follow_redirects=False)

    assert response.status_code == 303
    assert collector_spy == [], f"{path} started a second concurrent run"


def test_a_failing_collector_does_not_500_the_page(admin_client, monkeypatch):
    """A collector blowing up is reported through the DiscoveryRun row and
    the log, not by handing the user a stack trace -- the route swallows it
    and redirects."""
    from discovery import cli

    def boom():
        raise RuntimeError("controller unreachable")

    monkeypatch.setattr(cli, "run_unifi_discovery", boom)

    response = admin_client.post("/discovery/run/unifi", follow_redirects=False)

    assert response.status_code == 303


# -- the asset form -----------------------------------------------------------
# The form routes are where the tolerant parsers and the autofill helpers are
# actually wired together. tests/test_dashboard_helpers.py covers each piece
# in isolation; what only a request shows is that the route passes the raw
# form strings through the parsers at all, and calls the autofill helpers in
# the order they need (replacement value before the insert, model-number
# guess after it, so the provenance note can reference a row that has an id).


def _asset_form(**overrides):
    """The required fields of asset_form.html, so a test can vary one thing."""
    form = {
        "hostname": "form-asset",
        "asset_type": "iot",
        "criticality": "medium",
        "lifecycle_status": "active",
    }
    form.update(overrides)
    return form


def test_create_from_the_form_parses_money_and_dates(admin_client, session):
    response = admin_client.post(
        "/assets/new",
        data=_asset_form(purchase_price="£1,234.56", purchase_date="2024-03-01"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    asset = session.exec(select(Asset).where(Asset.hostname == "form-asset")).one()
    assert asset.purchase_price == Decimal("1234.56")
    assert asset.purchase_date == date(2024, 3, 1)
    assert response.headers["location"] == f"/assets/{asset.id}"


def test_create_from_the_form_keeps_unparseable_input_out_of_the_database(
    admin_client, session
):
    """The parsers return None rather than raising, so a typo saves the rest
    of the form instead of 500ing -- but it must not store a junk value."""
    response = admin_client.post(
        "/assets/new",
        data=_asset_form(purchase_price="about fifty quid", purchase_date="last tuesday"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    asset = session.exec(select(Asset).where(Asset.hostname == "form-asset")).one()
    assert asset.purchase_price is None
    assert asset.purchase_date is None


def test_create_from_the_form_autofills_the_replacement_value(admin_client, session):
    """_autofill_replacement_value runs on create -- a new insurable asset
    with a price and a date shouldn't need a separate revaluation run before
    it counts towards the household total."""
    admin_client.post(
        "/assets/new",
        data=_asset_form(
            asset_type="iot", purchase_price="1358.31", purchase_date="2025-01-14"
        ),
        follow_redirects=False,
    )

    asset = session.exec(select(Asset).where(Asset.hostname == "form-asset")).one()
    assert asset.replacement_value is not None


def test_create_from_the_form_marks_the_source_as_manual(admin_client, session):
    """Discovery reconciliation treats manually-created rows differently from
    ones it created itself."""
    admin_client.post("/assets/new", data=_asset_form(), follow_redirects=False)

    asset = session.exec(select(Asset).where(Asset.hostname == "form-asset")).one()
    assert asset.source == "manual"


def test_form_checkboxes_become_booleans(admin_client, session):
    """An unchecked HTML checkbox is absent from the POST body entirely, not
    "false" -- these fields are `str | None = Form(None)` and rely on
    bool() to convert."""
    admin_client.post(
        "/assets/new",
        data=_asset_form(hostname="locked-asset", identity_locked="on", hostname_locked="on"),
        follow_redirects=False,
    )
    admin_client.post(
        "/assets/new", data=_asset_form(hostname="unlocked-asset"), follow_redirects=False
    )

    locked = session.exec(select(Asset).where(Asset.hostname == "locked-asset")).one()
    unlocked = session.exec(select(Asset).where(Asset.hostname == "unlocked-asset")).one()
    assert locked.identity_locked is True
    assert locked.hostname_locked is True
    assert unlocked.identity_locked is False
    assert unlocked.hostname_locked is False


def test_edit_updates_the_asset(admin_client, populated, session):
    response = admin_client.post(
        f"/assets/{populated.id}/edit",
        data=_asset_form(hostname="renamed-nas", asset_type="server", criticality="low"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    session.refresh(populated)
    assert populated.hostname == "renamed-nas"
    assert populated.criticality == Criticality.low


def test_edit_of_an_unknown_asset_does_not_500(admin_client, session):
    response = admin_client.post(
        "/assets/9999/edit", data=_asset_form(), follow_redirects=False
    )
    assert response.status_code in (302, 303, 307)


# -- triage, findings, notes, locations ---------------------------------------


def test_confirm_promotes_a_discovered_asset_to_active(admin_client, session):
    asset = make_asset(session, hostname="new-find",
                       lifecycle_status=LifecycleStatus.discovered)

    response = admin_client.post(f"/assets/{asset.id}/confirm", follow_redirects=False)

    assert response.headers["location"] == "/assets/triage"
    session.refresh(asset)
    assert asset.lifecycle_status == LifecycleStatus.active


def test_confirm_all_promotes_only_the_discovered_ones(admin_client, session):
    a = make_asset(session, hostname="d1", lifecycle_status=LifecycleStatus.discovered)
    b = make_asset(session, hostname="d2", lifecycle_status=LifecycleStatus.discovered)
    retired = make_asset(session, hostname="old",
                         lifecycle_status=LifecycleStatus.decommissioned)

    admin_client.post("/assets/triage/confirm-all", follow_redirects=False)

    for asset in (a, b, retired):
        session.refresh(asset)
    assert a.lifecycle_status == LifecycleStatus.active
    assert b.lifecycle_status == LifecycleStatus.active
    assert retired.lifecycle_status == LifecycleStatus.decommissioned


def test_closing_a_finding_stamps_the_closed_date(admin_client, populated, session):
    finding = session.exec(select(Finding)).one()

    response = admin_client.post(
        f"/findings/{finding.id}/status", data={"status": "closed"}, follow_redirects=False
    )

    assert response.headers["location"] == "/findings"
    session.refresh(finding)
    assert finding.status == FindingStatus.closed
    assert finding.closed_date is not None


def test_reopening_a_finding_leaves_the_closed_date_alone(admin_client, populated, session):
    """Only the closed transition stamps the date -- moving a finding to any
    other status must not."""
    finding = session.exec(select(Finding)).one()

    admin_client.post(
        f"/findings/{finding.id}/status", data={"status": "mitigated"}, follow_redirects=False
    )

    session.refresh(finding)
    assert finding.status == FindingStatus.mitigated
    assert finding.closed_date is None


def test_adding_a_note_records_it_against_the_asset(admin_client, populated, session):
    response = admin_client.post(
        f"/assets/{populated.id}/notes",
        data={"body": "  Replaced the drive  "},
        follow_redirects=False,
    )

    assert response.headers["location"] == f"/assets/{populated.id}"
    notes = session.exec(
        select(AssetNote).where(AssetNote.author == "user")
    ).all()
    assert [n.body for n in notes] == ["Replaced the drive"], "the body should be stripped"


def test_a_whitespace_only_note_is_not_recorded(admin_client, populated, session):
    admin_client.post(
        f"/assets/{populated.id}/notes", data={"body": "   "}, follow_redirects=False
    )

    assert session.exec(select(AssetNote).where(AssetNote.author == "user")).all() == []


def test_create_rename_and_delete_a_location(admin_client, session):
    admin_client.post(
        "/locations/new", data={"name": "  Loft  ", "description": "Top floor"},
        follow_redirects=False,
    )
    location = session.exec(select(Location).where(Location.name == "Loft")).one()

    admin_client.post(
        f"/locations/{location.id}/rename", data={"name": "Attic", "description": ""},
        follow_redirects=False,
    )
    session.refresh(location)
    assert location.name == "Attic"
    assert location.description is None

    admin_client.post(f"/locations/{location.id}/delete", follow_redirects=False)
    assert session.get(Location, location.id) is None


def test_a_location_still_in_use_is_not_deleted(admin_client, populated, session):
    """Deleting it would leave every asset in that room pointing at a missing
    row -- the route counts first and declines."""
    location = session.exec(select(Location)).one()

    admin_client.post(f"/locations/{location.id}/delete", follow_redirects=False)

    assert session.get(Location, location.id) is not None


def test_a_blank_location_name_is_rejected(admin_client, session):
    admin_client.post("/locations/new", data={"name": "   "}, follow_redirects=False)

    assert session.exec(select(Location)).all() == []
