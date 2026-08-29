"""Tests for discovery/exposure_resync.py -- the Finding.exposure/
sla_due_date backfill for findings whose stored exposure has drifted from
their asset's current is_internet_facing flag.

DB-touching, using the in-memory session fixture and make_asset, mirroring
tests/test_revaluation.py's plan/apply/idempotency shape. The SLA-days
maths itself is covered by discovery/cve_enrich.py's own sla_due_date; here
the concern is plan/apply behaviour: what gets changed, the audit note, and
idempotency.
"""

from datetime import datetime

from conftest import make_asset
from sqlmodel import select

from app.models import AssetNote, Exposure, Finding, FindingStatus, Severity
from discovery.cve_enrich import sla_due_date
from discovery.exposure_resync import (
    apply_exposure_resync,
    plan_exposure_resync,
    run_exposure_resync,
)


def _make_finding(session, asset, **overrides):
    defaults = dict(
        asset_id=asset.id,
        severity=Severity.medium,
        exposure=Exposure.internal,
        detected_date=datetime(2026, 7, 1),
        status=FindingStatus.open,
    )
    defaults.update(overrides)
    finding = Finding(**defaults)
    session.add(finding)
    session.commit()
    session.refresh(finding)
    return finding


def test_plan_marks_drifted_finding_as_changed(session):
    asset = make_asset(session, is_internet_facing=True)
    finding = _make_finding(session, asset, exposure=Exposure.internal)

    plan = plan_exposure_resync(session)
    change = next(c for c in plan if c.finding.id == finding.id)

    assert change.changed is True
    assert change.new_exposure == Exposure.internet_facing
    assert change.new_due_date == sla_due_date(
        Severity.medium, Exposure.internet_facing, finding.detected_date
    )


def test_plan_leaves_matching_finding_unchanged(session):
    asset = make_asset(session, is_internet_facing=False)
    finding = _make_finding(session, asset, exposure=Exposure.internal)

    plan = plan_exposure_resync(session)
    change = next(c for c in plan if c.finding.id == finding.id)

    assert change.changed is False
    assert change.new_exposure == Exposure.internal


def test_closed_finding_is_not_touched(session):
    asset = make_asset(session, is_internet_facing=True)
    _make_finding(session, asset, exposure=Exposure.internal, status=FindingStatus.mitigated)

    plan = plan_exposure_resync(session)

    assert plan == []  # only open findings are ever planned


def test_apply_writes_exposure_due_date_and_one_note(session):
    asset = make_asset(session, is_internet_facing=True)
    finding = _make_finding(session, asset, exposure=Exposure.internal)

    result = apply_exposure_resync(session, plan_exposure_resync(session))

    session.refresh(finding)
    assert finding.exposure == Exposure.internet_facing
    assert finding.sla_due_date == sla_due_date(
        Severity.medium, Exposure.internet_facing, finding.detected_date
    )
    assert result["updated"] == 1
    notes = session.exec(select(AssetNote).where(AssetNote.asset_id == asset.id)).all()
    assert len(notes) == 1
    assert notes[0].author == "exposure-resync"


def test_apply_is_idempotent(session):
    asset = make_asset(session, is_internet_facing=True)
    _make_finding(session, asset, exposure=Exposure.internal)

    apply_exposure_resync(session, plan_exposure_resync(session))
    second = apply_exposure_resync(session, plan_exposure_resync(session))

    assert second["updated"] == 0
    notes = session.exec(select(AssetNote)).all()
    assert len(notes) == 1  # no second note on the no-op re-run


def test_sla_due_date_recomputed_from_original_detected_date_not_now(session):
    # Same rule the severity-triggered rescore path already follows --
    # re-syncing exposure must not also silently grant extra time by
    # resetting the SLA clock to today.
    asset = make_asset(session, is_internet_facing=True)
    old_detected = datetime(2020, 1, 1)
    finding = _make_finding(session, asset, exposure=Exposure.internal, detected_date=old_detected)

    apply_exposure_resync(session, plan_exposure_resync(session))

    session.refresh(finding)
    assert finding.detected_date == old_detected
    assert finding.sla_due_date == sla_due_date(Severity.medium, Exposure.internet_facing, old_detected)


# -- format_plan + run_exposure_resync: the dry-run gate -----------------------
# Same gap, and the same stakes, as tests/test_revaluation.py's dry-run
# section: plan and apply were covered, the wrapper deciding whether apply
# runs at all was not. discovery/cli.py reaches this as
# `_run_exposure_resync(dry_run=not apply, ...)`, so an inverted flag turns
# `resync-exposure` with no --apply into a silent rewrite of every open
# finding's exposure and SLA due date.


def test_run_dry_run_reports_the_change_without_making_it(session):
    asset = make_asset(session, is_internet_facing=True)
    finding = _make_finding(session, asset, exposure=Exposure.internal)

    summary = run_exposure_resync(dry_run=True, session=session)

    assert summary["applied"] is False
    assert summary["to_change"] == 1, "the dry run should still report the pending change"
    session.refresh(finding)
    assert finding.exposure == Exposure.internal, "a dry run must not write"
    assert session.exec(select(AssetNote)).all() == []


def test_run_defaults_to_a_dry_run(session):
    asset = make_asset(session, is_internet_facing=True)
    finding = _make_finding(session, asset, exposure=Exposure.internal)

    summary = run_exposure_resync(session=session)

    assert summary["applied"] is False
    session.refresh(finding)
    assert finding.exposure == Exposure.internal


def test_run_with_dry_run_false_actually_applies(session):
    asset = make_asset(session, is_internet_facing=True)
    finding = _make_finding(session, asset, exposure=Exposure.internal)

    summary = run_exposure_resync(dry_run=False, session=session)

    assert summary["applied"] is True
    assert summary["updated"] == 1
    session.refresh(finding)
    assert finding.exposure == Exposure.internet_facing


def test_format_plan_shows_the_exposure_and_due_date_transition(session):
    asset = make_asset(session, is_internet_facing=True, hostname="edge-box")
    _make_finding(session, asset, exposure=Exposure.internal)

    plan_text = run_exposure_resync(dry_run=True, session=session)["plan"]

    assert "CHANGE" in plan_text
    assert "edge-box" in plan_text
    assert "internal -> internet_facing" in plan_text


def test_format_plan_counts_already_matching_findings(session):
    """The reassuring half of the diff: someone running this after a previous
    apply needs to see that the untouched findings were considered, not
    skipped by a filter bug."""
    asset = make_asset(session, is_internet_facing=False)
    _make_finding(session, asset, exposure=Exposure.internal)

    plan_text = run_exposure_resync(dry_run=True, session=session)["plan"]

    assert "1 open finding(s) already match their asset's current exposure" in plan_text
