"""Backfill Asset.replacement_value from app/valuation.py's new-for-old rule.

Plan-then-apply, mirroring discovery/account_import.py: a pure
plan_revaluation() that never writes, a format_plan() for the dry-run diff,
and apply_revaluation() that setattrs and writes one AssetNote(author=
"valuation") per actual change. Dry-run is the default at the CLI.

Recompute-everything by design (see the plan): every run overwrites, including
a hand-entered figure -- the AssetNote trail is what makes that safe, since the
previous value is always recoverable from the asset timeline. A run only writes
where the value actually changes, so repeated runs on the same day are
idempotent and don't spam the timeline.

This lives under discovery/ only because that package owns the CLI machinery
(discovery/cli.py); valuation is not itself a discovery collector, and the
revalue command deliberately does not open a DiscoveryRun.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlmodel import Session, select

from app.clock import utcnow_naive
from app.models import Asset, AssetNote
from app.valuation import _EXCLUDED_TYPES, needs_review, replacement_value

AUTHOR = "valuation"
BASIS = "new-for-old (replacement as new)"


@dataclass
class PlannedRevaluation:
    asset: Asset
    old_value: Decimal | None
    new_value: Decimal | None  # None => skipped, see skipped_reason
    changed: bool = False
    skipped_reason: str | None = None
    review: bool = False
    gap: bool = False  # has a purchase_date but no price -- an actionable gap


def _skip_reason(asset: Asset, as_of: date) -> str:
    """Human explanation for why an asset can't be valued -- mirrors the guard
    order in app/valuation.replacement_value()."""
    if asset.asset_type in _EXCLUDED_TYPES:
        return f"type {asset.asset_type.value} is not insurable contents"
    missing = []
    if asset.purchase_price is None:
        missing.append("price")
    if asset.purchase_date is None:
        missing.append("date")
    if missing:
        return "missing purchase " + " and ".join(missing)
    if asset.purchase_price is not None and asset.purchase_price <= 0:
        return "purchase price is not positive"
    if asset.purchase_date is not None and asset.purchase_date > as_of:
        return "purchase date is in the future"
    return "no defensible figure"  # should be unreachable


def plan_revaluation(session: Session, as_of: date | None = None) -> list[PlannedRevaluation]:
    as_of = as_of or date.today()
    plan: list[PlannedRevaluation] = []
    for asset in session.exec(select(Asset).order_by(Asset.id)).all():
        new = replacement_value(
            asset.purchase_price, asset.purchase_date, asset.asset_type, as_of
        )
        if new is None:
            gap = (
                asset.purchase_date is not None
                and asset.purchase_price is None
                and asset.asset_type not in _EXCLUDED_TYPES
            )
            plan.append(
                PlannedRevaluation(
                    asset=asset,
                    old_value=asset.replacement_value,
                    new_value=None,
                    skipped_reason=_skip_reason(asset, as_of),
                    gap=gap,
                )
            )
            continue
        plan.append(
            PlannedRevaluation(
                asset=asset,
                old_value=asset.replacement_value,
                new_value=new,
                changed=asset.replacement_value != new,
                review=needs_review(asset.purchase_date, as_of),
            )
        )
    return plan


def _note_body(change: PlannedRevaluation) -> str:
    a = change.asset
    lines = [
        f"Replacement value set by valuation rule: {BASIS}.",
        f"  replacement_value: {change.old_value!r} -> {change.new_value!r}",
        f"  from purchase_price {a.purchase_price} on {a.purchase_date}"
        f" (type {a.asset_type.value}).",
    ]
    if change.review:
        lines.append(
            "  NOTE: item is >8 years old -- no like-for-like current model;"
            " confirm against a real quote."
        )
    return "\n".join(lines)


def apply_revaluation(session: Session, plan: list[PlannedRevaluation]) -> dict[str, int]:
    updated = 0
    for change in plan:
        if change.new_value is None or not change.changed:
            continue
        asset = change.asset
        asset.replacement_value = change.new_value
        session.add(asset)
        session.add(
            AssetNote(
                asset_id=asset.id,
                created_at=utcnow_naive(),
                author=AUTHOR,
                body=_note_body(change),
            )
        )
        updated += 1
    session.commit()
    return {"updated": updated}


def format_plan(plan: list[PlannedRevaluation]) -> str:
    lines: list[str] = []

    changes = [c for c in plan if c.new_value is not None and c.changed]
    noops = [c for c in plan if c.new_value is not None and not c.changed]
    gaps = [c for c in plan if c.gap]
    other_skips = [c for c in plan if c.new_value is None and not c.gap]

    for c in changes:
        flag = "  [review >8yr]" if c.review else ""
        lines.append(
            f"CHANGE   asset id={c.asset.id} ({c.asset.model or c.asset.hostname!r}): "
            f"{c.old_value!r} -> {c.new_value!r}{flag}"
        )
    for c in noops:
        lines.append(
            f"no-op    asset id={c.asset.id} ({c.asset.model or c.asset.hostname!r}): "
            f"already {c.new_value!r}"
        )

    if gaps:
        lines.append("")
        lines.append("GAP -- has a purchase date but no price, so cannot be valued:")
        for c in gaps:
            lines.append(
                f"  asset id={c.asset.id} ({c.asset.model or c.asset.hostname!r}), "
                f"purchased {c.asset.purchase_date}"
            )

    if other_skips:
        # These are the uninteresting many (no purchase data, or not contents);
        # summarise by reason rather than listing every row.
        from collections import Counter

        counts = Counter(c.skipped_reason for c in other_skips)
        lines.append("")
        lines.append("skipped (no purchase data / not insurable contents):")
        for reason, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {n:>3}  {reason}")

    return "\n".join(lines)


def run_revaluation(
    dry_run: bool = True,
    session: Session | None = None,
    as_of: date | None = None,
) -> dict:
    owns_session = session is None
    if owns_session:
        from app.db import engine

        session = Session(engine)
    try:
        plan = plan_revaluation(session, as_of)
        summary = {
            "assets": len(plan),
            "to_change": sum(1 for c in plan if c.new_value is not None and c.changed),
            "gaps": sum(1 for c in plan if c.gap),
            "plan": format_plan(plan),
        }
        if dry_run:
            summary["applied"] = False
            return summary
        summary.update(apply_revaluation(session, plan))
        summary["applied"] = True
        return summary
    finally:
        if owns_session:
            session.close()
