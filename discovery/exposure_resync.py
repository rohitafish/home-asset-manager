"""Backfill Finding.exposure/sla_due_date for open findings whose stored
exposure no longer matches their asset's CURRENT is_internet_facing flag.

Plan-then-apply, mirroring discovery/revaluation.py: a pure
plan_exposure_resync() that never writes, a format_plan() for the dry-run
diff, and apply_exposure_resync() that setattrs and writes one
AssetNote(author="exposure-resync") per actual change. Dry-run is the
default at the CLI.

Why this exists as its own tool, not just discovery/cve_enrich.py's own
rescore logic: enrich_findings_from_services() only revisits a Finding
whose originating service+CVE match still recurs in the CURRENT scan's
results (see the docstring inside that loop) -- a Finding whose underlying
service was since removed is never touched by any future enrichment run,
severity or exposure. is_internet_facing is user-editable two ways after a
Finding is created (the asset edit form, and the AI assistant's
propose_set_field), so drift is expected to recur; this tool guarantees
full coverage regardless of whether the originating service still matches,
the same way `revalue` exists alongside app/valuation.py's own inline
autofill for exactly the analogous reason.

This lives under discovery/ only because that package owns the CLI machinery
(discovery/cli.py); like revaluation, it is not itself a discovery
collector and deliberately does not open a DiscoveryRun.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, select

from app.clock import utcnow_naive
from app.models import Asset, AssetNote, Exposure, Finding, FindingStatus
from discovery.cve_enrich import sla_due_date

AUTHOR = "exposure-resync"


@dataclass
class PlannedExposureResync:
    finding: Finding
    asset: Asset
    old_exposure: Exposure
    new_exposure: Exposure
    old_due_date: datetime | None
    new_due_date: datetime | None
    changed: bool = False


def plan_exposure_resync(session: Session) -> list[PlannedExposureResync]:
    plan: list[PlannedExposureResync] = []
    findings = session.exec(
        select(Finding).where(Finding.status == FindingStatus.open).order_by(Finding.id)
    ).all()
    for finding in findings:
        asset = session.get(Asset, finding.asset_id)
        if asset is None:
            # No ON DELETE CASCADE on Finding.asset_id -- app/asset_children.py's
            # delete_asset_cascade always cleans up a deleted asset's findings
            # first, so this should be unreachable, but skip rather than
            # crash if it somehow isn't.
            continue
        new_exposure = Exposure.internet_facing if asset.is_internet_facing else Exposure.internal
        new_due_date = sla_due_date(finding.severity, new_exposure, finding.detected_date)
        plan.append(
            PlannedExposureResync(
                finding=finding,
                asset=asset,
                old_exposure=finding.exposure,
                new_exposure=new_exposure,
                old_due_date=finding.sla_due_date,
                new_due_date=new_due_date,
                changed=finding.exposure != new_exposure,
            )
        )
    return plan


def _note_body(change: PlannedExposureResync) -> str:
    f = change.finding
    return (
        f"Exposure resynced to match the asset's current internet-facing "
        f"status: {change.old_exposure.value} -> {change.new_exposure.value}.\n"
        f"  SLA due date: {change.old_due_date!r} -> {change.new_due_date!r}\n"
        f"  ({f.severity.value} severity, detected {f.detected_date})."
    )


def apply_exposure_resync(session: Session, plan: list[PlannedExposureResync]) -> dict[str, int]:
    updated = 0
    for change in plan:
        if not change.changed:
            continue
        finding = change.finding
        finding.exposure = change.new_exposure
        finding.sla_due_date = change.new_due_date
        session.add(finding)
        session.add(
            AssetNote(
                asset_id=change.asset.id,
                created_at=utcnow_naive(),
                author=AUTHOR,
                body=_note_body(change),
            )
        )
        updated += 1
    session.commit()
    return {"updated": updated}


def format_plan(plan: list[PlannedExposureResync]) -> str:
    lines: list[str] = []
    changes = [c for c in plan if c.changed]
    noops = [c for c in plan if not c.changed]

    for c in changes:
        label = c.asset.model or c.asset.hostname or f"asset {c.asset.id}"
        lines.append(
            f"CHANGE   finding id={c.finding.id} asset id={c.asset.id} ({label!r}): "
            f"exposure {c.old_exposure.value} -> {c.new_exposure.value}, "
            f"due {c.old_due_date!r} -> {c.new_due_date!r}"
        )
    if changes and noops:
        lines.append("")
    lines.append(f"{len(noops)} open finding(s) already match their asset's current exposure")

    return "\n".join(lines)


def run_exposure_resync(dry_run: bool = True, session: Session | None = None) -> dict:
    owns_session = session is None
    if owns_session:
        from app.db import engine

        session = Session(engine)
    try:
        plan = plan_exposure_resync(session)
        summary = {
            "findings": len(plan),
            "to_change": sum(1 for c in plan if c.changed),
            "plan": format_plan(plan),
        }
        if dry_run:
            summary["applied"] = False
            return summary
        summary.update(apply_exposure_resync(session, plan))
        summary["applied"] = True
        return summary
    finally:
        if owns_session:
            session.close()
