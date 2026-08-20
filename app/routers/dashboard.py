import csv
import io
import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, cast, func, or_
from sqlmodel import Session, select

from app import assistant, template_filters, valuation
from app.asset_children import delete_asset_cascade
from app.asset_merge import merge_asset_into
from app.asset_search import asset_search_filter
from app.auth import require_admin, require_same_origin
from app.backup_status import backup_age_label, backup_status
from app.clock import utcnow_naive
from app.correlate import (
    SAME_DEVICE,
    dismiss_same_device_candidate,
    find_same_device_candidates,
    link_assets,
    remove_same_device_link,
    unlink_assets,
)
from app.db import get_session
from app.models import (
    Asset,
    AssetInterface,
    AssetNote,
    AssetService,
    AssetType,
    ChangeProposal,
    ChatMessage,
    CIRelationship,
    Criticality,
    DiscoveryRun,
    Finding,
    FindingStatus,
    LifecycleStatus,
    Location,
    ProbeResult,
    Severity,
)
from app.readme_render import render_readme_html
from probes.base import Probe, ProbeOutcome
from probes.ping import PROBE as PING_PROBE
from probes.registry import applicable_probes

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_same_origin)])
templates = Jinja2Templates(directory="app/templates")

logger = logging.getLogger(__name__)
templates.env.filters["localdt"] = template_filters.localdt
templates.env.filters["money"] = template_filters.money

# Cache-busts static assets (see base.html) so a new deploy always forces a
# fresh fetch, regardless of how aggressively a given browser caches -- some
# (e.g. Chrome) won't revalidate a stale stylesheet on a normal reload, only
# a hard reload, unless the URL itself changes.
_STATIC_VERSION = str(int(Path("app/static/style.css").stat().st_mtime))
templates.env.globals["static_version"] = _STATIC_VERSION


def _parse_date_field(value: str) -> date | None:
    """<input type="date"> posts "" when left blank -- treat that (and any
    unparseable value) as "no date," matching the `x or None` convention
    already used for every other optional field on this form, rather than
    raising and 500ing the whole save."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_money_field(value: str) -> Decimal | None:
    """Accepts a plain "1234.56" (the browser's own <input type="number">
    format) or a hand-typed value with a currency symbol/thousands
    separator; unparseable input is treated as "no value" rather than
    raising, for the same reason as _parse_date_field."""
    value = (value or "").strip().lstrip("£$€").replace(",", "")
    if not value:
        return None
    try:
        parsed = Decimal(value).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    # Decimal("NaN").quantize(...) returns Decimal('NaN') without raising --
    # the one input this function's except clause doesn't catch. Postgres's
    # numeric column stores NaN happily, which then poisons any SUM() over
    # it (the Valuables page's totals render as NaN for the whole estate).
    # A negative amount is equally never a real purchase price/replacement
    # value, so treat both the same as unparseable: "no value".
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _parse_location_id(value: str | None) -> int | None:
    """A location_id arrives as a form/query string: the empty string when
    nothing is selected, or a numeric id. Anything non-numeric is treated as
    "no location" rather than raising -- an unparseable value (a malformed
    query param, a hand-crafted request) would otherwise be an int() ValueError
    surfacing as a 500. Same lenient contract as _parse_money_field."""
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_enum_filter(value: str | None, valid_values: list[str]) -> str | None:
    """Same lenient contract as _parse_location_id, for the asset_type/
    criticality/lifecycle_status/status filters below: comparing an
    unrecognised string straight against a Postgres enum column raises
    InvalidTextRepresentation (a 500), and a stale bookmark or a saved link
    from before an enum value was renamed is exactly how a real request
    ends up with one. Drop it -- treat it as "no filter" -- rather than
    raise."""
    if value and value in valid_values:
        return value
    return None


def _autofill_replacement_value(asset: Asset) -> None:
    """Fill replacement_value from purchase price/date when the user left it
    blank. A no-op if it's already set, or if valuation can't produce a figure
    (missing/invalid inputs, non-insurable type) -- valuation.replacement_value
    returns None there, so assigning it changes nothing. The estate-wide
    `revalue` command (discovery/revaluation.py) remains the canonical
    recompute; this only saves hand-typing the obvious figure at entry time."""
    if asset.replacement_value is None:
        asset.replacement_value = valuation.replacement_value(
            asset.purchase_price, asset.purchase_date, asset.asset_type
        )


def _autofill_model_number(session: Session, asset: Asset) -> None:
    """When an asset is well-documented (vendor + serial + model + purchase date
    all set) but has no model_number, ask Claude for a best guess and fill it,
    marked unverified. A synchronous one-shot LLM call -- assistant.guess_model_number
    returns None on any failure / no API key, so the save is never blocked or
    broken. Fills only when empty (never overwrites), skips a locked identity,
    and records provenance as an AssetNote. `asset.id` must be set (call after
    the row is flushed) for the note. The serial gates but isn't sent to the API.

    Runs at most once per asset: model_number_guess_attempted_at is stamped on
    any real attempt (including a no-answer miss), so a guess that comes back
    "unknown" isn't re-run on every subsequent save. Not stamped when the API
    isn't configured yet, or when the AI budget/kill-switch currently blocks
    spending (assistant.budget_block_reason) -- both are temporary
    conditions, so a later save should still get its one guess once either
    clears, the same way it already does for "not configured"."""
    if asset.model_number or asset.identity_locked or not assistant.is_configured():
        return
    if asset.model_number_guess_attempted_at is not None:
        return  # already tried once -- don't pay for another LLM call on every save
    if not (asset.vendor and asset.serial_number and asset.model and asset.purchase_date):
        return
    if assistant.budget_block_reason(session):
        return
    asset.model_number_guess_attempted_at = utcnow_naive()
    session.add(asset)
    guess = assistant.guess_model_number(asset, session)
    if not guess:
        return
    if guess == "N/A":
        asset.model_number = "N/A"
        body = "model_number set to N/A -- no distinct model number for this device (auto, Claude)."
    else:
        asset.model_number = f"{guess} (unverified)"
        body = (
            f"model_number auto-guessed as '{guess}' from vendor/model/purchase date "
            "(Claude). Unverified -- confirm on the device."
        )
    session.add(asset)
    session.add(AssetNote(asset_id=asset.id, author="claude", body=body))


_WARRANTY_LEAVING_SOON_DAYS = 90

# Chat attachments (receipts/screenshots/warranty PDFs) are analyzed once by
# the assistant and never written to disk -- see app/assistant.py's
# run_chat_turn docstring. These bound what gets read into memory and sent
# to the API per chat turn.
_CHAT_ATTACHMENT_MAX_FILES = 5
_CHAT_ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024  # 15MB -- comfortably under the API's 32MB base64 request cap with room for several files
_CHAT_ATTACHMENT_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"}
_CHAT_ATTACHMENT_MAGIC = {  # first-bytes sanity check -- don't trust the browser-supplied content_type alone
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),  # WEBP's real marker is bytes 8-12; RIFF prefix is a cheap first check
    "application/pdf": (b"%PDF-",),
}


def warranty_state(warranty_expiry: date | None) -> tuple[str, str]:
    """Returns (state, label) for an asset's warranty_expiry, mirroring
    Mactracker's own In/Leaving/Out Of Warranty buckets: "in" (still
    comfortably covered), "leaving" (expires within 90 days), "out" (already
    expired), or "unknown" (no warranty_expiry recorded)."""
    if warranty_expiry is None:
        return "unknown", "—"
    days_left = (warranty_expiry - date.today()).days
    if days_left < 0:
        return "out", f"Out of warranty (expired {warranty_expiry})"
    if days_left <= _WARRANTY_LEAVING_SOON_DAYS:
        return "leaving", f"Leaving warranty — {warranty_expiry} ({days_left}d left)"
    return "in", f"In warranty until {warranty_expiry}"


ASSET_TYPES = [t.value for t in AssetType]
CRITICALITIES = [c.value for c in Criticality]
LIFECYCLE_STATUSES = [s.value for s in LifecycleStatus]
FINDING_STATUSES = [s.value for s in FindingStatus]
SEVERITY_ORDER = {Severity.critical: 0, Severity.high: 1, Severity.medium: 2, Severity.low: 3}

# Cast native enum columns to text so ORDER BY sorts alphabetically by value
# rather than by the enum's declaration order in Postgres.
ASSET_SORT_COLUMNS = {
    "id": Asset.id,
    "hostname": Asset.hostname,
    "asset_type": cast(Asset.asset_type, String),
    "vendor": Asset.vendor,
    "owner": Asset.owner,
    "criticality": cast(Asset.criticality, String),
    "lifecycle_status": cast(Asset.lifecycle_status, String),
    "last_seen": Asset.last_seen,
    "location": Location.name,
}

# Same idea as ASSET_SORT_COLUMNS, for the Valuables table's own column set.
# "warranty" sorts by the underlying warranty_expiry date -- the on-screen
# in/leaving/out-of-warranty label is derived from that same field by
# warranty_state() below, so this is the direct DB-level equivalent rather
# than a separate derived-value sort.
VALUABLES_SORT_COLUMNS = {
    "hostname": Asset.hostname,
    "asset_type": cast(Asset.asset_type, String),
    "vendor": Asset.vendor,
    "model": Asset.model,
    "model_number": Asset.model_number,
    "serial_number": Asset.serial_number,
    "location": Location.name,
    "purchase_date": Asset.purchase_date,
    "purchase_price": Asset.purchase_price,
    "replacement_value": Asset.replacement_value,
    "warranty": Asset.warranty_expiry,
}


@router.get("/")
def index():
    return RedirectResponse(url="/assets")


@router.get("/readme")
def readme(request: Request):
    md_text = Path("README.md").read_text()
    rendered = render_readme_html(md_text)
    return templates.TemplateResponse(
        request, "readme.html", {"content": rendered.html, "toc": rendered.toc}
    )


@router.get("/assets")
def assets_list(
    request: Request,
    session: Session = Depends(get_session),
    asset_type: str | None = None,
    criticality: str | None = None,
    lifecycle_status: str | None = None,
    location_id: str | None = None,
    q: str | None = None,
    sort: str = "last_seen",
    direction: str = "desc",
):
    asset_type = _clean_enum_filter(asset_type, ASSET_TYPES)
    criticality = _clean_enum_filter(criticality, CRITICALITIES)
    lifecycle_status = _clean_enum_filter(lifecycle_status, LIFECYCLE_STATUSES)

    query = select(Asset)
    if asset_type:
        query = query.where(Asset.asset_type == asset_type)
    if criticality:
        query = query.where(Asset.criticality == criticality)
    if lifecycle_status:
        query = query.where(Asset.lifecycle_status == lifecycle_status)
    location_filter = _parse_location_id(location_id)
    if location_filter is not None:
        query = query.where(Asset.location_id == location_filter)
    q = (q or "").strip()
    if q:
        query = query.where(asset_search_filter(q))
    if sort == "location":
        # Location.name isn't a column on Asset -- outerjoin so it can be
        # sorted on directly, and outer (not inner) so assets with no
        # location assigned still show up rather than being dropped.
        query = query.outerjoin(Location, Asset.location_id == Location.id)

    sort_column = ASSET_SORT_COLUMNS.get(sort, Asset.last_seen)
    if sort not in ASSET_SORT_COLUMNS:
        sort = "last_seen"
    order = sort_column.desc() if direction == "desc" else sort_column.asc()
    assets = session.exec(query.order_by(order)).all()
    total_count = session.exec(select(func.count()).select_from(Asset)).one()
    locations = _all_locations(session)
    locations_by_id = {loc.id: loc for loc in locations}
    return templates.TemplateResponse(
        request,
        "assets_list.html",
        {
            "assets": assets,
            "asset_types": ASSET_TYPES,
            "criticalities": CRITICALITIES,
            "lifecycle_statuses": LIFECYCLE_STATUSES,
            "locations": locations,
            "locations_by_id": locations_by_id,
            "filters": {
                "asset_type": asset_type or "",
                "criticality": criticality or "",
                "lifecycle_status": lifecycle_status or "",
                "location_id": location_id or "",
                "q": q,
            },
            "filters_active": bool(
                asset_type or criticality or lifecycle_status or location_filter or q
            ),
            "sort": sort,
            "direction": direction if direction == "asc" else "desc",
            "total_count": total_count,
        },
    )


@router.get("/assets/count-badge")
def assets_count_badge(request: Request, session: Session = Depends(get_session)):
    total_count = session.exec(select(func.count()).select_from(Asset)).one()
    return templates.TemplateResponse(
        request, "_asset_count_badge.html", {"total_count": total_count}
    )


def _all_locations(session: Session) -> list[Location]:
    return session.exec(select(Location).order_by(Location.sort_order, Location.name)).all()


def _valuables_query(
    session: Session, show_all: bool, sort: str = "hostname", direction: str = "asc"
) -> list[Asset]:
    """The asset register for support calls / insurance claims. Default
    scope is assets that already carry identity/purchase/cover data, plus
    any high-criticality asset regardless -- that combination surfaces both
    "here's what's documented" and "here's what's still worth documenting" --
    minus anything explicitly marked not is_valuable (a cheap smart plug can
    legitimately be high criticality as an attack-surface concern while
    being worth nothing for insurance; that flag is the one way to say so
    without misrepresenting its actual criticality). ?all=1 shows every
    asset, is_valuable included -- that mode means literally everything."""
    query = select(Asset)
    if not show_all:
        query = query.where(
            or_(
                Asset.serial_number.is_not(None),
                Asset.model_number.is_not(None),
                Asset.model_identifier.is_not(None),
                Asset.purchase_date.is_not(None),
                Asset.purchase_price.is_not(None),
                Asset.replacement_value.is_not(None),
                Asset.warranty_expiry.is_not(None),
                Asset.criticality == Criticality.high,
            )
        ).where(Asset.is_valuable.is_(True))
    if sort == "location":
        # Location.name isn't a column on Asset -- outerjoin so it can be
        # sorted on directly, and outer (not inner) so assets with no
        # location assigned still show up rather than being dropped. Same
        # trick as assets_list()'s own location sort, above.
        query = query.outerjoin(Location, Asset.location_id == Location.id)
    sort_column = VALUABLES_SORT_COLUMNS.get(sort, Asset.hostname)
    order = sort_column.desc() if direction == "desc" else sort_column.asc()
    return session.exec(query.order_by(order)).all()


@router.get("/valuables")
def valuables_page(
    request: Request,
    session: Session = Depends(get_session),
    all: str | None = None,
    sort: str = "hostname",
    direction: str = "asc",
):
    show_all = bool(all)
    if sort not in VALUABLES_SORT_COLUMNS:
        sort = "hostname"
    direction = "asc" if direction == "asc" else "desc"
    assets = _valuables_query(session, show_all, sort, direction)
    locations_by_id = {loc.id: loc for loc in _all_locations(session)}

    rows = []
    total_purchase = Decimal(0)
    total_replacement = Decimal(0)
    for asset in assets:
        state, label = warranty_state(asset.warranty_expiry)
        rows.append(
            {
                "asset": asset,
                "location": locations_by_id.get(asset.location_id),
                "warranty_state": state,
                "warranty_label": label,
            }
        )
        if asset.purchase_price:
            total_purchase += asset.purchase_price
        if asset.replacement_value:
            total_replacement += asset.replacement_value

    return templates.TemplateResponse(
        request,
        "valuables.html",
        {
            "rows": rows,
            "show_all": show_all,
            "total_purchase": total_purchase,
            "total_replacement": total_replacement,
            "sort": sort,
            "direction": direction,
        },
    )


_VALUABLES_CSV_HEADER = [
    "ID", "Hostname", "Type", "Vendor", "Model", "Model number", "Model identifier",
    "Serial number", "Location", "Purchase date", "Purchase price (GBP)",
    "Replacement value (GBP)", "Warranty expiry", "Identity locked",
]


@router.get("/valuables.csv")
def valuables_csv(session: Session = Depends(get_session), all: str | None = None):
    assets = _valuables_query(session, bool(all))
    locations_by_id = {loc.id: loc for loc in _all_locations(session)}

    # Read every field into plain values *before* returning -- the
    # StreamingResponse body below is produced after this function returns,
    # by which point get_session()'s request-scoped session may already be
    # tearing down. Touching ORM objects from inside the generator would be
    # relying on session lifetime by accident.
    rows = []
    for asset in assets:
        location = locations_by_id.get(asset.location_id)
        rows.append(
            [
                asset.id,
                asset.hostname or "",
                asset.asset_type.value,
                asset.vendor or "",
                asset.model or "",
                asset.model_number or "",
                asset.model_identifier or "",
                asset.serial_number or "",
                location.name if location else "",
                asset.purchase_date.isoformat() if asset.purchase_date else "",
                f"{asset.purchase_price:.2f}" if asset.purchase_price is not None else "",
                f"{asset.replacement_value:.2f}" if asset.replacement_value is not None else "",
                asset.warranty_expiry.isoformat() if asset.warranty_expiry else "",
                "yes" if asset.identity_locked else "no",
            ]
        )

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        yield "\ufeff"  # UTF-8 BOM -- otherwise Excel mangles the £ symbols on Windows
        writer.writerow(_VALUABLES_CSV_HEADER)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for row in rows:
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    filename = f"asset-register-{date.today():%Y-%m-%d}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/locations")
def locations_list(request: Request, session: Session = Depends(get_session)):
    locations = _all_locations(session)
    counts = {}
    for loc in locations:
        counts[loc.id] = session.exec(
            select(func.count()).select_from(Asset).where(Asset.location_id == loc.id)
        ).one()
    return templates.TemplateResponse(
        request, "locations.html", {"locations": locations, "counts": counts}
    )


@router.post("/locations/new")
def location_create(
    name: str = Form(...),
    description: str = Form(""),
    session: Session = Depends(get_session),
):
    if name.strip():
        session.add(Location(name=name.strip(), description=description or None))
        session.commit()
    return RedirectResponse(url="/locations", status_code=303)


@router.get("/locations/{location_id}")
def location_detail(location_id: int, request: Request, session: Session = Depends(get_session)):
    location = session.get(Location, location_id)
    if not location:
        return RedirectResponse(url="/locations")
    assets = session.exec(
        select(Asset).where(Asset.location_id == location_id).order_by(Asset.hostname)
    ).all()
    return templates.TemplateResponse(
        request, "location_detail.html", {"location": location, "assets": assets}
    )


@router.post("/locations/{location_id}/rename")
def location_rename(
    location_id: int,
    name: str = Form(...),
    description: str = Form(""),
    session: Session = Depends(get_session),
):
    location = session.get(Location, location_id)
    if location and name.strip():
        location.name = name.strip()
        location.description = description or None
        session.add(location)
        session.commit()
    return RedirectResponse(url=f"/locations/{location_id}", status_code=303)


@router.post("/locations/{location_id}/delete")
def location_delete(location_id: int, session: Session = Depends(get_session)):
    location = session.get(Location, location_id)
    if location:
        in_use = session.exec(
            select(func.count()).select_from(Asset).where(Asset.location_id == location_id)
        ).one()
        if in_use == 0:
            session.delete(location)
            session.commit()
    return RedirectResponse(url="/locations", status_code=303)


@router.get("/assets/new")
def asset_new_form(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "asset_form.html",
        {
            "asset": None,
            "asset_types": ASSET_TYPES,
            "criticalities": CRITICALITIES,
            "lifecycle_statuses": LIFECYCLE_STATUSES,
            "locations": _all_locations(session),
        },
    )


@router.post("/assets/new")
def asset_create(
    hostname: str = Form(""),
    asset_type: AssetType = Form(...),
    vendor: str = Form(""),
    vendor_locked: str | None = Form(None),
    model: str = Form(""),
    model_number: str = Form(""),
    model_identifier: str = Form(""),
    serial_number: str = Form(""),
    identity_locked: str | None = Form(None),
    purchase_date: str = Form(""),
    purchase_price: str = Form(""),
    replacement_value: str = Form(""),
    warranty_expiry: str = Form(""),
    owner: str = Form(""),
    custodian: str = Form(""),
    criticality: Criticality = Form(...),
    classification: str = Form(""),
    lifecycle_status: LifecycleStatus = Form(...),
    is_internet_facing: str | None = Form(None),
    hostname_locked: str | None = Form(None),
    is_valuable: str | None = Form(None),
    location_id: str = Form(""),
    position: str = Form(""),
    session: Session = Depends(get_session),
):
    asset = Asset(
        hostname=hostname or None,
        hostname_locked=bool(hostname_locked),
        vendor=vendor or None,
        vendor_locked=bool(vendor_locked),
        model=model or None,
        model_number=model_number or None,
        model_identifier=model_identifier or None,
        serial_number=serial_number or None,
        identity_locked=bool(identity_locked),
        purchase_date=_parse_date_field(purchase_date),
        purchase_price=_parse_money_field(purchase_price),
        replacement_value=_parse_money_field(replacement_value),
        warranty_expiry=_parse_date_field(warranty_expiry),
        asset_type=asset_type,
        owner=owner or None,
        custodian=custodian or None,
        criticality=criticality,
        classification=classification or None,
        lifecycle_status=lifecycle_status,
        is_internet_facing=bool(is_internet_facing),
        is_valuable=bool(is_valuable),
        location_id=_parse_location_id(location_id),
        position=position or None,
        source="manual",
    )
    _autofill_replacement_value(asset)
    session.add(asset)
    session.commit()
    session.refresh(asset)
    # After the row has an id, so the guess's provenance AssetNote can reference it.
    _autofill_model_number(session, asset)
    session.commit()
    return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)


@router.get("/assets/triage")
def triage_queue(request: Request, session: Session = Depends(get_session)):
    assets = session.exec(
        select(Asset)
        .where(Asset.lifecycle_status == LifecycleStatus.discovered)
        .order_by(Asset.first_seen.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "triage_queue.html",
        {"assets": assets},
    )


@router.post("/assets/{asset_id}/confirm")
def asset_confirm(asset_id: int, session: Session = Depends(get_session)):
    asset = session.get(Asset, asset_id)
    if asset:
        asset.lifecycle_status = LifecycleStatus.active
        session.add(asset)
        session.commit()
    return RedirectResponse(url="/assets/triage", status_code=303)


@router.post("/assets/triage/confirm-all")
def asset_confirm_all(session: Session = Depends(get_session)):
    assets = session.exec(
        select(Asset).where(Asset.lifecycle_status == LifecycleStatus.discovered)
    ).all()
    for asset in assets:
        asset.lifecycle_status = LifecycleStatus.active
        session.add(asset)
    session.commit()
    return RedirectResponse(url="/assets/triage", status_code=303)


@router.get("/assets/duplicates")
def duplicates_page(request: Request, session: Session = Depends(get_session)):
    dup_hostnames = session.exec(
        select(Asset.hostname)
        .where(Asset.hostname.is_not(None))
        .group_by(Asset.hostname)
        .having(func.count(Asset.id) > 1)
    ).all()
    groups = []
    for hostname in dup_hostnames:
        assets = session.exec(
            select(Asset).where(Asset.hostname == hostname).order_by(Asset.first_seen)
        ).all()
        members = []
        for a in assets:
            iface = session.exec(
                select(AssetInterface).where(AssetInterface.asset_id == a.id)
            ).first()
            members.append((a, iface))
        groups.append({"hostname": hostname, "members": members})

    # Assets already linked as the same physical device (via Investigate or the
    # assistant) but never merged -- they stay as two separate rows by design,
    # so surface them here as merge candidates. The link is stored as a mirrored
    # pair (A->B and B->A), so dedupe on a frozenset and keep each pair once,
    # ordered by id for stable rendering.
    linked_pairs = []
    seen_pairs: set[frozenset] = set()
    for rel in session.exec(
        select(CIRelationship).where(CIRelationship.relationship_type == SAME_DEVICE)
    ).all():
        key = frozenset((rel.asset_id, rel.related_asset_id))
        if len(key) < 2 or key in seen_pairs:
            continue
        seen_pairs.add(key)
        a = session.get(Asset, rel.asset_id)
        b = session.get(Asset, rel.related_asset_id)
        if a and b:
            first, second = sorted((a, b), key=lambda x: x.id)
            linked_pairs.append((first, second))

    # Scored near-duplicate candidates (similar-but-distinct names) are
    # deliberately NOT surfaced here -- in this inventory they're almost all
    # genuinely separate devices that merely share a naming convention, so the
    # noise isn't worth it. That scoring still drives the Investigate page,
    # which is the right home for a weighed, non-destructive "maybe" list.
    return templates.TemplateResponse(
        request,
        "duplicates.html",
        {"groups": groups, "linked_pairs": linked_pairs},
    )


@router.post("/assets/duplicates/merge")
def duplicates_merge(survivor_id: int = Form(...), session: Session = Depends(get_session)):
    survivor = session.get(Asset, survivor_id)
    if survivor and survivor.hostname:
        duplicates = session.exec(
            select(Asset).where(Asset.hostname == survivor.hostname, Asset.id != survivor_id)
        ).all()
        for dup in duplicates:
            merge_asset_into(session, survivor_id=survivor_id, duplicate_id=dup.id)
        session.commit()
    return RedirectResponse(url="/assets/duplicates", status_code=303)


@router.post("/assets/duplicates/merge-pair")
def duplicates_merge_pair(
    survivor_id: int = Form(...),
    id_a: int = Form(...),
    id_b: int = Form(...),
    session: Session = Depends(get_session),
):
    # Unlike duplicates_merge (which merges everything sharing a hostname), this
    # merges one explicitly-chosen pair -- used for same-device links and scored
    # candidates, whose two rows have *different* hostnames. The form posts both
    # member ids plus the radio-chosen survivor; the duplicate is the other one.
    if survivor_id not in (id_a, id_b):
        return RedirectResponse(url="/assets/duplicates", status_code=303)
    duplicate_id = id_a if survivor_id == id_b else id_b
    if survivor_id != duplicate_id:
        survivor = session.get(Asset, survivor_id)
        duplicate = session.get(Asset, duplicate_id)
        if survivor and duplicate:
            merge_asset_into(session, survivor_id=survivor_id, duplicate_id=duplicate_id)
            session.commit()
    return RedirectResponse(url="/assets/duplicates", status_code=303)


@router.post("/assets/duplicates/dismiss-pair")
def duplicates_dismiss_pair(
    id_a: int = Form(...), id_b: int = Form(...), session: Session = Depends(get_session)
):
    # "Not the same device": drop the same-physical-device link so the pair
    # stops appearing here, AND record a dismissal so it doesn't immediately
    # reappear as a fresh candidate on /assets/investigate -- removing the
    # link alone used to do exactly that, since find_same_device_candidates()
    # only excludes *linked* pairs. Non-destructive -- both assets are kept;
    # re-link via Investigate or the assistant if it turns out they are the
    # same.
    unlinked = remove_same_device_link(session, id_a, id_b)
    dismissed = dismiss_same_device_candidate(session, id_a, id_b)
    if unlinked or dismissed:
        session.commit()
    return RedirectResponse(url="/assets/duplicates", status_code=303)


@router.get("/findings")
def findings_list(
    request: Request,
    session: Session = Depends(get_session),
    status: str | None = "open",
):
    status = _clean_enum_filter(status, FINDING_STATUSES)
    query = select(Finding)
    if status:
        query = query.where(Finding.status == status)
    findings = session.exec(query).all()
    findings.sort(
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.sla_due_date or datetime.max)
    )
    asset_ids = {f.asset_id for f in findings}
    assets_by_id = {
        a.id: a for a in session.exec(select(Asset).where(Asset.id.in_(asset_ids))).all()
    } if asset_ids else {}
    vuln_ids = {f.vulnerability_id for f in findings if f.vulnerability_id}
    from app.models import Vulnerability

    vulns_by_id = {
        v.id: v
        for v in session.exec(select(Vulnerability).where(Vulnerability.id.in_(vuln_ids))).all()
    } if vuln_ids else {}
    now = utcnow_naive()
    return templates.TemplateResponse(
        request,
        "findings_list.html",
        {
            "findings": findings,
            "assets_by_id": assets_by_id,
            "vulns_by_id": vulns_by_id,
            "now": now,
            "status_filter": status or "",
            "finding_statuses": FINDING_STATUSES,
        },
    )


@router.post("/findings/{finding_id}/status")
def finding_update_status(
    finding_id: int, status: FindingStatus = Form(...), session: Session = Depends(get_session)
):
    finding = session.get(Finding, finding_id)
    if finding:
        finding.status = status
        if status == FindingStatus.closed:
            finding.closed_date = utcnow_naive()
        session.add(finding)
        session.commit()
    return RedirectResponse(url="/findings", status_code=303)


@router.get("/discovery")
def discovery_page(request: Request, session: Session = Depends(get_session)):
    runs = session.exec(
        select(DiscoveryRun).order_by(DiscoveryRun.started_at.desc()).limit(20)
    ).all()
    return templates.TemplateResponse(request, "discovery.html", {"runs": runs})


def _discovery_already_running(session: Session) -> bool:
    """Best-effort guard against two discovery runs racing -- a second
    browser tab, or an impatient re-click during a slow enrichment run.
    Without this, two concurrent reconcile_into_db calls can each do "MAC
    not found -> create Asset" before either commits (there's no unique
    constraint on AssetInterface.mac), silently creating the same "new"
    device twice.

    NOT airtight: this is a check-then-act race, not a lock -- a true fix
    needs a DB-level advisory lock or a unique partial index. What this
    does buy: it narrows the window from "however long a full run takes"
    (minutes) down to the gap between this check and the collector's own
    _tracked_run insert (milliseconds), which is the realistic trigger
    (an impatient re-click) rather than a determined race."""
    return (
        session.exec(select(DiscoveryRun).where(DiscoveryRun.status == "running")).first()
        is not None
    )


@router.post("/discovery/run/unifi")
def discovery_run_unifi(session: Session = Depends(get_session)):
    if _discovery_already_running(session):
        return RedirectResponse(url="/discovery", status_code=303)
    from discovery.cli import run_unifi_discovery

    try:
        run_unifi_discovery()
    except Exception:
        # The DiscoveryRun row records status=failed + str(exc), but not the
        # traceback -- and a failure before _tracked_run opens the row (e.g. in
        # UnifiClient construction) isn't recorded anywhere without this.
        logger.exception("unifi discovery failed")
    return RedirectResponse(url="/discovery", status_code=303)


@router.post("/discovery/run/nmap")
def discovery_run_nmap(session: Session = Depends(get_session)):
    if _discovery_already_running(session):
        return RedirectResponse(url="/discovery", status_code=303)
    from discovery.cli import run_nmap_discovery

    try:
        run_nmap_discovery()
    except Exception:
        logger.exception("nmap discovery failed")
    return RedirectResponse(url="/discovery", status_code=303)


@router.post("/discovery/run/nmap-privileged")
def discovery_run_nmap_privileged(session: Session = Depends(get_session)):
    if _discovery_already_running(session):
        return RedirectResponse(url="/discovery", status_code=303)
    from discovery.cli import run_nmap_discovery

    try:
        run_nmap_discovery(use_sudo=True)
    except Exception:
        logger.exception("privileged nmap discovery failed")
    return RedirectResponse(url="/discovery", status_code=303)


@router.post("/discovery/run/local-mac")
def discovery_run_local_mac(session: Session = Depends(get_session)):
    if _discovery_already_running(session):
        return RedirectResponse(url="/discovery", status_code=303)
    from discovery.cli import run_local_host_discovery

    try:
        run_local_host_discovery()
    except Exception:
        logger.exception("local-mac discovery failed")
    return RedirectResponse(url="/discovery", status_code=303)


@router.post("/discovery/run/enrich")
def discovery_run_enrich(session: Session = Depends(get_session)):
    if _discovery_already_running(session):
        return RedirectResponse(url="/discovery", status_code=303)
    from discovery.cli import run_enrichment

    try:
        run_enrichment()
    except Exception:
        logger.exception("vulnerability enrichment failed")
    return RedirectResponse(url="/discovery", status_code=303)


@router.post("/discovery/run/sonos")
def discovery_run_sonos(session: Session = Depends(get_session)):
    if _discovery_already_running(session):
        return RedirectResponse(url="/discovery", status_code=303)
    from discovery.cli import run_sonos_discovery

    try:
        run_sonos_discovery(dry_run=False)
    except Exception:
        logger.exception("sonos discovery failed")
    return RedirectResponse(url="/discovery", status_code=303)


@router.post("/discovery/run/all")
def discovery_run_all(session: Session = Depends(get_session)):
    if _discovery_already_running(session):
        return RedirectResponse(url="/discovery", status_code=303)
    from discovery.cli import run_all_discovery

    run_all_discovery()  # each collector already records its own DiscoveryRun outcome
    return RedirectResponse(url="/discovery", status_code=303)


@router.get("/summary")
def summary_page(request: Request, session: Session = Depends(get_session)):
    from datetime import timedelta

    now = utcnow_naive()
    assets = session.exec(select(Asset)).all()
    total_assets = len(assets)
    active_assets = [a for a in assets if a.lifecycle_status == LifecycleStatus.active]
    discovered_assets = [a for a in assets if a.lifecycle_status == LifecycleStatus.discovered]
    recently_seen = [a for a in assets if (now - a.last_seen) <= timedelta(days=30)]
    coverage_pct = round(100 * len(recently_seen) / total_assets, 1) if total_assets else 0.0

    open_findings = session.exec(
        select(Finding).where(Finding.status == FindingStatus.open)
    ).all()
    open_by_severity = {sev.value: 0 for sev in Severity}
    for f in open_findings:
        open_by_severity[f.severity.value] += 1
    overdue = [
        f for f in open_findings if f.sla_due_date and f.sla_due_date < now
    ]

    latest_runs = {}
    for run in session.exec(
        select(DiscoveryRun).order_by(DiscoveryRun.started_at.desc())
    ).all():
        latest_runs.setdefault(run.source, run)

    backup = backup_status()

    return templates.TemplateResponse(
        request,
        "summary.html",
        {
            "total_assets": total_assets,
            "active_count": len(active_assets),
            "discovered_count": len(discovered_assets),
            "coverage_pct": coverage_pct,
            "open_findings_total": len(open_findings),
            "open_by_severity": open_by_severity,
            "overdue_count": len(overdue),
            "latest_runs": latest_runs,
            "backup_age": backup_age_label(backup),
            "backup_stale": backup["backup_stale"],
        },
    )


@router.get("/assets/investigate")
def assets_investigate(request: Request, session: Session = Depends(get_session)):
    # Registered before /assets/{asset_id} below -- if this literal segment
    # came after that catch-all, FastAPI would match "investigate" as an
    # asset_id and 422 instead of routing here.
    candidates = find_same_device_candidates(session)
    return templates.TemplateResponse(
        request, "investigate.html", {"candidates": candidates}
    )


@router.post("/assets/investigate/link")
def assets_investigate_link(
    asset_id_a: int = Form(...),
    asset_id_b: int = Form(...),
    detail: str = Form(""),
    session: Session = Depends(get_session),
):
    link_assets(session, asset_id_a, asset_id_b, detail=detail or None)
    note_body = f"Linked to asset #{asset_id_b} as the same physical device.{' ' + detail if detail else ''}"
    session.add(AssetNote(asset_id=asset_id_a, author="user", body=note_body))
    note_body_b = f"Linked to asset #{asset_id_a} as the same physical device.{' ' + detail if detail else ''}"
    session.add(AssetNote(asset_id=asset_id_b, author="user", body=note_body_b))
    session.commit()
    return RedirectResponse(url="/assets/investigate", status_code=303)


@router.post("/assets/investigate/dismiss")
def assets_investigate_dismiss(
    asset_id_a: int = Form(...),
    asset_id_b: int = Form(...),
    session: Session = Depends(get_session),
):
    if dismiss_same_device_candidate(session, asset_id_a, asset_id_b):
        session.commit()
    return RedirectResponse(url="/assets/investigate", status_code=303)


@router.post("/assets/investigate/dismiss-all")
def assets_investigate_dismiss_all(session: Session = Depends(get_session)):
    # Dismisses exactly what's currently on screen -- recomputes the same
    # list assets_investigate() renders rather than trusting posted ids, so
    # this can't be tricked into dismissing a pair the user never saw.
    for c in find_same_device_candidates(session):
        dismiss_same_device_candidate(session, c.asset_a.id, c.asset_b.id)
    session.commit()
    return RedirectResponse(url="/assets/investigate", status_code=303)


@router.get("/assets/{asset_id}")
def asset_detail(asset_id: int, request: Request, session: Session = Depends(get_session)):
    asset = session.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/assets")
    interfaces = session.exec(
        select(AssetInterface).where(AssetInterface.asset_id == asset_id)
    ).all()
    services = session.exec(
        select(AssetService).where(AssetService.asset_id == asset_id)
    ).all()
    relationships = session.exec(
        select(CIRelationship).where(CIRelationship.asset_id == asset_id)
    ).all()
    related_assets_by_id = {}
    related_ids = {r.related_asset_id for r in relationships}
    if related_ids:
        related_assets_by_id = {
            a.id: a for a in session.exec(select(Asset).where(Asset.id.in_(related_ids))).all()
        }
    findings = session.exec(
        select(Finding).where(
            Finding.asset_id == asset_id, Finding.status == FindingStatus.open
        )
    ).all()
    notes = session.exec(
        select(AssetNote)
        .where(AssetNote.asset_id == asset_id)
        .order_by(AssetNote.created_at.desc())
    ).all()
    location = session.get(Location, asset.location_id) if asset.location_id else None
    probe_results = session.exec(
        select(ProbeResult)
        .where(ProbeResult.asset_id == asset_id)
        .order_by(ProbeResult.ran_at.desc())
    ).all()
    probe_results_view = []
    for pr in probe_results:
        probe_results_view.append({
            "row": pr,
            "facts": json.loads(pr.facts_json) if pr.facts_json else {},
            "suggestions": json.loads(pr.suggestions_json) if pr.suggestions_json else [],
        })
    # Ping (probes/ping.py) applies to any asset with a known IP via
    # ALWAYS_PROBES, so applicable_probes() is never empty once an IP
    # exists -- this reduces to "has at least one IP," which also gates the
    # Ping button, not just "Run identification probe."
    can_probe = bool(applicable_probes(asset, interfaces, services)) and any(
        i.ip for i in interfaces
    )
    warranty_state_value, warranty_label = warranty_state(asset.warranty_expiry)
    context = {
        "asset": asset,
        "interfaces": interfaces,
        "services": services,
        "relationships": relationships,
        "related_assets_by_id": related_assets_by_id,
        "findings": findings,
        "notes": notes,
        "location": location,
        "probe_results": probe_results_view,
        "can_probe": can_probe,
        "warranty_state": warranty_state_value,
        "warranty_label": warranty_label,
    }
    context.update(_chat_panel_context(session, asset))
    return templates.TemplateResponse(
        request,
        "asset_detail.html",
        context,
    )


@router.get("/assets/{asset_id}/edit")
def asset_edit_form(asset_id: int, request: Request, session: Session = Depends(get_session)):
    asset = session.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/assets")
    return templates.TemplateResponse(
        request,
        "asset_form.html",
        {
            "asset": asset,
            "asset_types": ASSET_TYPES,
            "criticalities": CRITICALITIES,
            "lifecycle_statuses": LIFECYCLE_STATUSES,
            "locations": _all_locations(session),
        },
    )


@router.post("/assets/{asset_id}/edit")
def asset_update(
    asset_id: int,
    hostname: str = Form(""),
    asset_type: AssetType = Form(...),
    vendor: str = Form(""),
    vendor_locked: str | None = Form(None),
    model: str = Form(""),
    model_number: str = Form(""),
    model_identifier: str = Form(""),
    serial_number: str = Form(""),
    identity_locked: str | None = Form(None),
    purchase_date: str = Form(""),
    purchase_price: str = Form(""),
    replacement_value: str = Form(""),
    warranty_expiry: str = Form(""),
    owner: str = Form(""),
    custodian: str = Form(""),
    criticality: Criticality = Form(...),
    classification: str = Form(""),
    lifecycle_status: LifecycleStatus = Form(...),
    is_internet_facing: str | None = Form(None),
    hostname_locked: str | None = Form(None),
    is_valuable: str | None = Form(None),
    location_id: str = Form(""),
    position: str = Form(""),
    session: Session = Depends(get_session),
):
    asset = session.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/assets")
    asset.hostname = hostname or None
    asset.hostname_locked = bool(hostname_locked)
    asset.vendor = vendor or None
    asset.vendor_locked = bool(vendor_locked)
    asset.model = model or None
    asset.model_number = model_number or None
    asset.model_identifier = model_identifier or None
    asset.serial_number = serial_number or None
    asset.identity_locked = bool(identity_locked)
    asset.purchase_date = _parse_date_field(purchase_date)
    asset.purchase_price = _parse_money_field(purchase_price)
    asset.replacement_value = _parse_money_field(replacement_value)
    asset.warranty_expiry = _parse_date_field(warranty_expiry)
    asset.asset_type = asset_type
    asset.owner = owner or None
    asset.custodian = custodian or None
    asset.criticality = criticality
    asset.classification = classification or None
    asset.lifecycle_status = lifecycle_status
    asset.is_internet_facing = bool(is_internet_facing)
    asset.is_valuable = bool(is_valuable)
    asset.location_id = _parse_location_id(location_id)
    asset.position = position or None
    asset.last_seen = utcnow_naive()
    _autofill_replacement_value(asset)
    _autofill_model_number(session, asset)
    session.add(asset)
    session.commit()
    return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)


@router.post("/assets/{asset_id}/notes")
def asset_add_note(
    asset_id: int, body: str = Form(...), session: Session = Depends(get_session)
):
    asset = session.get(Asset, asset_id)
    if asset and body.strip():
        session.add(AssetNote(asset_id=asset_id, author="user", body=body.strip()))
        session.commit()
    return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)


def _run_probes_and_record(
    session: Session, asset_id: int, probes: list[Probe], ips: list[str]
) -> None:
    """Runs each of `probes` against each of `ips`, inline (matching the
    synchronous convention every other discovery/enrichment button already
    uses -- see probes/), and records a ProbeResult per (probe, ip).

    For a probe with replaces_prior_results=True (currently just ping --
    see probes/base.py), any earlier result for that same (probe, ip) is
    deleted first, so a cheap, frequently-re-run check doesn't accumulate
    history and bury identification evidence in the asset detail page's
    probe panel. Does not commit -- callers share a transaction with
    whatever else they write (e.g. the "no known IP" note).
    """
    for probe in probes:
        for ip in ips:
            if probe.replaces_prior_results:
                for prior in session.exec(
                    select(ProbeResult).where(
                        ProbeResult.asset_id == asset_id,
                        ProbeResult.probe_name == probe.name,
                        ProbeResult.target_ip == ip,
                    )
                ).all():
                    session.delete(prior)
            try:
                outcome: ProbeOutcome = probe.run(ip)
            except Exception as exc:  # a probe must never take the request down
                outcome = ProbeOutcome(ok=False, summary=f"Probe raised an unexpected error: {exc}")
            session.add(ProbeResult(
                asset_id=asset_id,
                probe_name=probe.name,
                target_ip=ip,
                ok=outcome.ok,
                summary=outcome.summary,
                facts_json=json.dumps(outcome.facts) if outcome.facts else None,
                suggestions_json=json.dumps(outcome.suggestions) if outcome.suggestions else None,
                raw=outcome.raw,
            ))


@router.post("/assets/{asset_id}/probe")
def asset_probe(asset_id: int, session: Session = Depends(get_session)):
    """Runs every applicable read-only identification probe (plus ping --
    see probes/registry.py's ALWAYS_PROBES) against this asset's known IPs.
    See probes/."""
    asset = session.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/assets")

    interfaces = session.exec(
        select(AssetInterface).where(AssetInterface.asset_id == asset_id)
    ).all()
    services = session.exec(
        select(AssetService).where(AssetService.asset_id == asset_id)
    ).all()
    ips = [i.ip for i in interfaces if i.ip]

    if not ips:
        session.add(AssetNote(
            asset_id=asset_id, author="system",
            body="Probe requested but this asset has no known IP address to probe.",
        ))
        session.commit()
        return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)

    probes = applicable_probes(asset, interfaces, services)
    _run_probes_and_record(session, asset_id, probes, ips)
    session.commit()
    return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)


@router.post("/assets/{asset_id}/ping")
def asset_ping(asset_id: int, session: Session = Depends(get_session)):
    """Fast standalone reachability check -- just the ping probe, without
    the slower identification probes. See probes/ping.py."""
    asset = session.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/assets")

    interfaces = session.exec(
        select(AssetInterface).where(AssetInterface.asset_id == asset_id)
    ).all()
    ips = [i.ip for i in interfaces if i.ip]

    if not ips:
        session.add(AssetNote(
            asset_id=asset_id, author="system",
            body="Ping requested but this asset has no known IP address to ping.",
        ))
        session.commit()
        return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)

    _run_probes_and_record(session, asset_id, [PING_PROBE], ips)
    session.commit()
    return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)


@router.post("/assets/{asset_id}/probe/apply-suggestion")
def asset_probe_apply_suggestion(
    asset_id: int,
    field_name: str = Form(...),
    value: str = Form(...),
    session: Session = Depends(get_session),
):
    # Allowlisted server-side, independent of what probes/Claude describe --
    # a probe result is untrusted device-supplied data (see probes/base.py),
    # so it must never be able to write outside this fixed set of fields.
    allowed_fields = {"position", "model", "firmware_version", "vendor"}
    asset = session.get(Asset, asset_id)
    if asset and field_name in allowed_fields and value.strip():
        old_value = getattr(asset, field_name) or "—"
        setattr(asset, field_name, value.strip())
        session.add(asset)
        session.add(AssetNote(
            asset_id=asset_id, author="user",
            body=f"Applied probe suggestion: {field_name} \"{old_value}\" → \"{value.strip()}\".",
        ))
        session.commit()
    return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)


def _pending_proposals(
    session: Session, asset_id: int, origin_asset_id: int | None = None
) -> list[ChangeProposal]:
    """The asset's pending proposals in the order the chat panel lists them.
    Shared by the panel render and the apply-all route so both agree on which
    rows the "Apply all (N)" count and action cover. `origin_asset_id` scopes to
    proposals that came from a particular chat -- used when applying a
    cross-asset group from the originating panel."""
    query = select(ChangeProposal).where(
        ChangeProposal.asset_id == asset_id, ChangeProposal.status == "pending"
    )
    if origin_asset_id is not None:
        query = query.where(ChangeProposal.origin_asset_id == origin_asset_id)
    return session.exec(query.order_by(ChangeProposal.created_at)).all()


def _other_asset_proposals(session: Session, origin_asset_id: int) -> list[dict]:
    """Pending proposals this asset's chat produced that TARGET other assets --
    e.g. a multi-asset invoice analysed here. Grouped by target asset so the
    panel can show 'Also proposed -- <asset>' sections, keeping cross-asset
    proposals from being stranded on a page the user isn't looking at."""
    rows = session.exec(
        select(ChangeProposal)
        .where(
            ChangeProposal.origin_asset_id == origin_asset_id,
            ChangeProposal.asset_id != origin_asset_id,
            ChangeProposal.status == "pending",
        )
        .order_by(ChangeProposal.asset_id, ChangeProposal.created_at)
    ).all()
    groups: dict[int, dict] = {}
    for p in rows:
        group = groups.get(p.asset_id)
        if group is None:
            group = {"asset": session.get(Asset, p.asset_id), "proposals": []}
            groups[p.asset_id] = group
        group["proposals"].append({"row": p, "description": assistant.describe_proposal(p)})
    return list(groups.values())


def _chat_panel_context(session: Session, asset: Asset, error: str | None = None) -> dict:
    configured = assistant.is_configured()
    return {
        "asset": asset,
        "configured": configured,
        "transcript": assistant.chat_transcript(session, asset.id) if configured else [],
        "proposals": [
            {"row": p, "description": assistant.describe_proposal(p)}
            for p in _pending_proposals(session, asset.id)
        ] if configured else [],
        "other_asset_proposals": _other_asset_proposals(session, asset.id) if configured else [],
        # Shown even when not "configured" via an API key -- the toggle and
        # spend figures are meaningful (and the toggle still clickable) the
        # moment a key IS added, so there's no reason to hide them earlier.
        "spend": assistant.spend_summary(session),
        "error": error,
    }


def _resolve_panel_asset(session: Session, panel_asset_id: str, fallback: Asset | None) -> Asset | None:
    """The asset whose chat panel the action was triggered from -- so applying a
    cross-asset proposal re-renders the panel the user is looking at (the
    origin) rather than the target's. Falls back to the target when the hidden
    field is absent (a non-htmx post, or an older cached page)."""
    if panel_asset_id:
        try:
            panel = session.get(Asset, int(panel_asset_id))
        except ValueError:
            panel = None
        if panel:
            return panel
    return fallback


@router.post("/assets/{asset_id}/chat")
async def asset_chat(
    asset_id: int, request: Request, message: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    asset = session.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/assets")

    files = [f for f in files if f.filename]  # an untouched file picker still posts one empty UploadFile
    error = None
    attachments: list[tuple[str, str, bytes]] = []
    total_bytes = 0
    if len(files) > _CHAT_ATTACHMENT_MAX_FILES:
        error = f"Attach at most {_CHAT_ATTACHMENT_MAX_FILES} files at once."
    else:
        for f in files:
            # Check the declared size before reading, so an oversized upload
            # isn't pulled fully into memory just to be rejected. f.size comes
            # from the multipart part's own count; the len(data) check below is
            # the authoritative backstop for a client that lies about it.
            if f.size is not None and f.size > _CHAT_ATTACHMENT_MAX_BYTES:
                error = f"'{f.filename}' is larger than {_CHAT_ATTACHMENT_MAX_BYTES // (1024 * 1024)}MB."
                break
            data = await f.read()
            if len(data) > _CHAT_ATTACHMENT_MAX_BYTES:
                error = f"'{f.filename}' is larger than {_CHAT_ATTACHMENT_MAX_BYTES // (1024 * 1024)}MB."
                break
            total_bytes += len(data)
            if total_bytes > _CHAT_ATTACHMENT_MAX_BYTES:
                error = f"Attachments total more than {_CHAT_ATTACHMENT_MAX_BYTES // (1024 * 1024)}MB."
                break
            if f.content_type not in _CHAT_ATTACHMENT_ALLOWED_TYPES:
                error = f"'{f.filename}' is a {f.content_type or 'unknown'} file -- only JPEG/PNG/WebP/GIF images and PDFs are accepted."
                break
            if not any(data.startswith(sig) for sig in _CHAT_ATTACHMENT_MAGIC[f.content_type]):
                error = f"'{f.filename}' doesn't look like a valid {f.content_type} file."
                break
            attachments.append((f.filename, f.content_type, data))

    if not error and (message.strip() or attachments):
        error = assistant.run_chat_turn(session, asset, message.strip(), attachments=attachments)
    return templates.TemplateResponse(
        request, "_chat_panel.html", _chat_panel_context(session, asset, error)
    )


@router.post("/assets/{asset_id}/chat/clear")
def asset_chat_clear(asset_id: int, request: Request, session: Session = Depends(get_session)):
    asset = session.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/assets")
    for row in session.exec(select(ChatMessage).where(ChatMessage.asset_id == asset_id)).all():
        session.delete(row)
    session.commit()
    return templates.TemplateResponse(request, "_chat_panel.html", _chat_panel_context(session, asset))


@router.post("/settings/ai-assistant/toggle")
def toggle_ai_assistant(
    request: Request, asset_id: int = Form(...), enabled: str = Form(...),
    session: Session = Depends(get_session),
):
    """Global on/off switch for the assistant (assistant.set_ai_assistant_enabled)
    -- applies to every asset's chat panel and the model-number auto-guess
    alike, not just the one this was clicked from. asset_id is only which
    panel to re-render, same role as panel_asset_id on the proposal routes."""
    asset = session.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/assets")
    assistant.set_ai_assistant_enabled(session, enabled == "true")
    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "_chat_panel.html", _chat_panel_context(session, asset))
    return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)


@router.post("/assets/{asset_id}/proposals/apply-all")
def proposals_apply_all(
    asset_id: int, request: Request, panel_asset_id: str = Form(""),
    session: Session = Depends(get_session),
):
    asset = session.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/assets")
    panel = _resolve_panel_asset(session, panel_asset_id, asset)
    # When applying a cross-asset group from another asset's panel, scope to the
    # proposals that panel actually showed (origin == that panel), not every
    # pending proposal targeting this asset.
    origin = panel.id if panel and panel.id != asset_id else None
    # apply_proposal never commits and is self-correcting (an un-appliable one
    # discards itself with a note rather than raising), so a plain loop + one
    # commit is safe -- same per-proposal behaviour as clicking Apply on each.
    for proposal in _pending_proposals(session, asset_id, origin_asset_id=origin):
        assistant.apply_proposal(session, proposal)
    # Applied proposals may have just supplied purchase price + date, so derive
    # the replacement value the same way the manual form does (only when blank).
    _autofill_replacement_value(asset)
    _autofill_model_number(session, asset)
    session.commit()
    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "_chat_panel.html", _chat_panel_context(session, panel or asset))
    return RedirectResponse(url=f"/assets/{(panel or asset).id}", status_code=303)


@router.post("/proposals/{proposal_id}/apply")
def proposal_apply(
    proposal_id: int, request: Request, panel_asset_id: str = Form(""),
    session: Session = Depends(get_session),
):
    proposal = session.get(ChangeProposal, proposal_id)
    if not proposal:
        return RedirectResponse(url="/assets")
    asset = session.get(Asset, proposal.asset_id)
    assistant.apply_proposal(session, proposal)
    if asset:
        # Same derive-when-blank rule as the form: a just-applied purchase
        # price/date proposal should fill the replacement value too.
        _autofill_replacement_value(asset)
        _autofill_model_number(session, asset)
    session.commit()
    # Re-render the panel the action came from (the origin, for a cross-asset
    # proposal), so the user stays where they are.
    panel = _resolve_panel_asset(session, panel_asset_id, asset)
    if request.headers.get("hx-request") and panel:
        return templates.TemplateResponse(request, "_chat_panel.html", _chat_panel_context(session, panel))
    return RedirectResponse(url=f"/assets/{panel.id if panel else proposal.asset_id}", status_code=303)


@router.post("/proposals/{proposal_id}/discard")
def proposal_discard(
    proposal_id: int, request: Request, panel_asset_id: str = Form(""),
    session: Session = Depends(get_session),
):
    proposal = session.get(ChangeProposal, proposal_id)
    if not proposal:
        return RedirectResponse(url="/assets")
    asset = session.get(Asset, proposal.asset_id)
    if proposal.status == "pending":
        proposal.status = "discarded"
        session.add(proposal)
        session.commit()
    panel = _resolve_panel_asset(session, panel_asset_id, asset)
    if request.headers.get("hx-request") and panel:
        return templates.TemplateResponse(request, "_chat_panel.html", _chat_panel_context(session, panel))
    return RedirectResponse(url=f"/assets/{panel.id if panel else proposal.asset_id}", status_code=303)


@router.post("/assets/{asset_id}/relationships/{relationship_id}/unlink")
def asset_unlink(asset_id: int, relationship_id: int, session: Session = Depends(get_session)):
    unlink_assets(session, asset_id, relationship_id)
    session.commit()
    return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)


@router.post("/assets/{asset_id}/delete")
def asset_delete(asset_id: int, session: Session = Depends(get_session)):
    asset = session.get(Asset, asset_id)
    if asset:
        # Dependent rows must go first -- there's no ON DELETE CASCADE on
        # these foreign keys, so deleting the asset alone hits a foreign key
        # violation (surfaced to the user as a bare "internal server error")
        # whenever it has any interfaces/services/findings/relationships.
        # See app/asset_children.py for the single shared list of child models.
        delete_asset_cascade(session, asset_id)
        session.commit()
    return RedirectResponse(url="/assets", status_code=303)
