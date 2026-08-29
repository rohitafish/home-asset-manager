"""Tests for discovery/revaluation.py -- the replacement-value backfill.

DB-touching, using the in-memory session fixture and make_asset. Pins an
explicit as_of so figures are stable. The valuation maths itself is covered by
tests/test_valuation.py; here the concern is plan/apply behaviour: what gets
changed, the audit note, idempotency, and overwrite-of-manual.
"""

from datetime import date
from decimal import Decimal

from conftest import make_asset
from sqlmodel import select

from app.models import AssetNote, AssetType
from discovery.revaluation import apply_revaluation, plan_revaluation, run_revaluation

AS_OF = date(2026, 8, 9)


def test_plan_marks_valuable_asset_as_changed(session):
    make_asset(
        session,
        purchase_price=Decimal("7794.00"),
        purchase_date=date(2025, 7, 28),
        asset_type=AssetType.iot,
    )
    plan = plan_revaluation(session, AS_OF)
    change = next(c for c in plan if c.new_value is not None)
    assert change.changed is True
    assert change.new_value == Decimal(7955)


def test_apply_writes_value_and_one_note(session):
    asset = make_asset(
        session,
        purchase_price=Decimal("1358.31"),
        purchase_date=date(2025, 1, 14),
        asset_type=AssetType.iot,
    )
    plan = plan_revaluation(session, AS_OF)
    result = apply_revaluation(session, plan)

    session.refresh(asset)
    assert asset.replacement_value == Decimal(1401)
    assert result["updated"] == 1
    notes = session.exec(select(AssetNote).where(AssetNote.asset_id == asset.id)).all()
    assert len(notes) == 1
    assert notes[0].author == "valuation"


def test_apply_is_idempotent_same_day(session):
    make_asset(
        session,
        purchase_price=Decimal("1099.00"),
        purchase_date=date(2025, 4, 9),
        asset_type=AssetType.end_user_device,
    )
    apply_revaluation(session, plan_revaluation(session, AS_OF))
    second = apply_revaluation(session, plan_revaluation(session, AS_OF))
    assert second["updated"] == 0
    notes = session.exec(select(AssetNote)).all()
    assert len(notes) == 1  # no second note on the no-op re-run


def test_apply_overwrites_a_manual_value(session):
    # "Recompute everything": a hand-set figure is replaced by the formula,
    # and the old value survives in the note for recovery.
    asset = make_asset(
        session,
        purchase_price=Decimal("1099.00"),
        purchase_date=date(2025, 4, 9),
        asset_type=AssetType.end_user_device,
        replacement_value=Decimal("1500.00"),
    )
    apply_revaluation(session, plan_revaluation(session, AS_OF))
    session.refresh(asset)
    assert asset.replacement_value == Decimal(1099)
    note = session.exec(select(AssetNote).where(AssetNote.asset_id == asset.id)).one()
    assert "1500" in note.body  # old value recorded


def test_dated_but_unpriced_asset_is_flagged_as_gap(session):
    make_asset(
        session,
        purchase_date=date(2020, 8, 26),
        asset_type=AssetType.iot,
    )
    plan = plan_revaluation(session, AS_OF)
    gap = next(c for c in plan if c.gap)
    assert gap.new_value is None
    assert "missing purchase price" in gap.skipped_reason


def test_excluded_type_is_skipped_not_gap(session):
    make_asset(
        session,
        purchase_price=Decimal("50.00"),
        purchase_date=date(2024, 1, 1),
        asset_type=AssetType.software,
    )
    plan = plan_revaluation(session, AS_OF)
    entry = plan[0]
    assert entry.new_value is None
    assert entry.gap is False
    assert "not insurable contents" in entry.skipped_reason


# -- format_plan + run_revaluation: the dry-run gate ---------------------------
# plan_revaluation and apply_revaluation are both covered above, but the
# wrapper that decides *whether apply runs at all* was not, and neither was
# the diff a dry run prints -- the only thing a human reads before deciding
# to pass --apply.
#
# discovery/cli.py reaches this as `_run_revaluation(dry_run=not apply, ...)`.
# Invert that polarity anywhere along the chain and the documented-as-safe
# default rewrites every replacement_value in the live Postgres, with the
# AssetNote trail the only way back. The gate is pinned in both directions
# below: a test that only asserted dry_run=True wrote nothing would pass just
# as happily against a run_revaluation() that never wrote at all.


def _valuable_asset(session):
    """An asset the plan will definitely want to change, so "nothing was
    written" can't be confused with "there was nothing to write"."""
    return make_asset(
        session,
        purchase_price=Decimal("1358.31"),
        purchase_date=date(2025, 1, 14),
        asset_type=AssetType.iot,
    )


def test_run_dry_run_reports_the_change_without_making_it(session):
    asset = _valuable_asset(session)

    summary = run_revaluation(dry_run=True, session=session, as_of=AS_OF)

    assert summary["applied"] is False
    assert summary["to_change"] == 1, "the dry run should still report the pending change"
    session.refresh(asset)
    assert asset.replacement_value is None, "a dry run must not write"
    assert session.exec(select(AssetNote)).all() == []


def test_run_defaults_to_a_dry_run(session):
    """The default is what a bare `revalue` (no --apply) gets, so it carries
    the safety property rather than the caller having to remember it."""
    asset = _valuable_asset(session)

    summary = run_revaluation(session=session, as_of=AS_OF)

    assert summary["applied"] is False
    session.refresh(asset)
    assert asset.replacement_value is None


def test_run_with_dry_run_false_actually_applies(session):
    asset = _valuable_asset(session)

    summary = run_revaluation(dry_run=False, session=session, as_of=AS_OF)

    assert summary["applied"] is True
    assert summary["updated"] == 1
    session.refresh(asset)
    assert asset.replacement_value == Decimal(1401)


def test_format_plan_shows_the_old_and_new_value_for_a_change(session):
    _valuable_asset(session)

    plan_text = run_revaluation(dry_run=True, session=session, as_of=AS_OF)["plan"]

    assert "CHANGE" in plan_text
    assert "1401" in plan_text


def test_format_plan_calls_out_a_priced_gap_separately_from_noise(session):
    """A missing price on an asset that has a purchase date is actionable --
    someone can go and find the receipt -- so it gets its own listed section
    rather than being folded into the counted "skipped" summary."""
    make_asset(
        session,
        purchase_date=date(2025, 3, 3),
        asset_type=AssetType.iot,
        hostname="gap-asset",
    )
    make_asset(session, asset_type=AssetType.software, hostname="not-contents")

    plan_text = run_revaluation(dry_run=True, session=session, as_of=AS_OF)["plan"]

    assert "GAP -- has a purchase date but no price" in plan_text
    assert "gap-asset" in plan_text
    assert "not insurable contents" in plan_text
    assert "not-contents" not in plan_text, "uninteresting skips are counted, not listed"
