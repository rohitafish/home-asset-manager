"""The Software tab: one row per piece of software across every machine,
which machines have it (and at what version and publication date), and a
red/amber/green cyber-risk grade with its reasons (app/software_risk.py).

Data comes from `python -m discovery.cli software-import`, driven by
scripts/collect-software.sh; grades are computed here, per request, from the
stored facts -- see app/software_risk.py for why they are never stored.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from app.auth import require_admin, require_same_origin
from app.db import get_session
from app.models import Asset, SoftwareHost, SoftwareInstall, SoftwareItem
from app.routers.dashboard import _csv_safe, _discovery_already_running, templates
from app.software_risk import (
    AMBER,
    GREEN,
    RAG_LABELS,
    RAG_ORDER,
    RED,
    SOURCE_COVERAGE,
    SOURCE_LABELS,
    UNRATED,
    grade_install,
    version_tuple,
    worst,
)

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_same_origin)])
logger = logging.getLogger(__name__)

SORTS = ("risk", "name", "published", "machines")
CATEGORY_LABELS = {"application": "Application", "library": "Library/component", "system": "Base system"}


def _asset_label(asset: Asset | None, asset_id: int) -> str:
    if asset is None:
        return f"Asset #{asset_id}"
    return asset.hostname or f"Asset #{asset.id}"


def build_rows(
    session: Session,
    today: date,
    q: str = "",
    machine: int | None = None,
    rag: str = "",
    show_all: bool = False,
    sort: str = "risk",
) -> tuple[list[dict], dict]:
    """The Software tab's rows plus summary counts. Applications only unless
    show_all; restricted to one machine's installs when `machine` is set (the
    grade then reflects that machine alone)."""
    pairs = session.exec(
        select(SoftwareInstall, SoftwareItem).join(
            SoftwareItem, SoftwareItem.id == SoftwareInstall.software_id
        )
    ).all()
    asset_ids = {inst.asset_id for inst, _ in pairs}
    assets = {
        a.id: a for a in session.exec(select(Asset).where(Asset.id.in_(asset_ids))).all()
    } if asset_ids else {}

    grouped: dict[int, dict] = {}
    for inst, item in pairs:
        if machine is not None and inst.asset_id != machine:
            continue
        row = grouped.setdefault(item.id, {"item": item, "installs": []})
        row["installs"].append((inst, grade_install(inst, today)))

    needle = q.strip().lower()
    rows = []
    for row in grouped.values():
        item, installs = row["item"], row["installs"]
        if not show_all and not any(i.category == "application" for i, _ in installs):
            continue
        if needle and needle not in item.name.lower() and needle not in item.key and not any(
            needle in i.package_name.lower() for i, _ in installs
        ):
            continue
        row_rag = worst(g.rag for _, g in installs)
        if rag and row_rag != rag:
            continue

        machine_ids = sorted({i.asset_id for i, _ in installs},
                             key=lambda a: _asset_label(assets.get(a), a).lower())
        multi = len(machine_ids) > 1
        reasons: dict[str, set[str]] = defaultdict(set)
        versions: dict[str, dict] = {}
        for inst, grade in installs:
            label = _asset_label(assets.get(inst.asset_id), inst.asset_id)
            if grade.rag in (RED, AMBER, UNRATED):
                for reason in grade.reasons:
                    reasons[reason].add(label)
            v = versions.setdefault(inst.version or "unknown", {
                "version": inst.version or "unknown",
                "published": None,
                "published_source": None,
                "machines": {},
                "sources": set(),
            })
            v["sources"].add(SOURCE_LABELS.get(inst.source, inst.source))
            if inst.published_date and (v["published"] is None or inst.published_date < v["published"]):
                v["published"], v["published_source"] = inst.published_date, inst.published_source
            prev = v["machines"].get(inst.asset_id)
            if prev is None or RAG_ORDER[grade.rag] < RAG_ORDER[prev["rag"]]:
                v["machines"][inst.asset_id] = {"id": inst.asset_id, "label": label, "rag": grade.rag}

        version_list = sorted(
            versions.values(),
            key=lambda v: (version_tuple(v["version"]), v["version"]),
            reverse=True,
        )
        for v in version_list:
            v["machines"] = sorted(v["machines"].values(), key=lambda m: m["label"].lower())
            v["sources"] = sorted(v["sources"])
        reason_list = []
        for reason, machines in reasons.items():
            if multi and len(machines) < len(machine_ids):
                reason_list.append(f"{reason} ({', '.join(sorted(machines))})")
            else:
                reason_list.append(reason)
        published = [i.published_date for i, _ in installs if i.published_date]
        categories = {i.category for i, _ in installs}
        rows.append({
            "item": item,
            "rag": row_rag,
            "reasons": sorted(reason_list),
            "versions": version_list,
            "machine_count": len(machine_ids),
            "machine_labels": [_asset_label(assets.get(a), a) for a in machine_ids],
            "oldest_published": min(published) if published else None,
            "category": CATEGORY_LABELS.get(
                "application" if "application" in categories else min(categories), ""
            ),
        })

    if sort == "name":
        rows.sort(key=lambda r: r["item"].name.lower())
    elif sort == "published":
        rows.sort(key=lambda r: (r["oldest_published"] is None, r["oldest_published"] or date.max,
                                 r["item"].name.lower()))
    elif sort == "machines":
        rows.sort(key=lambda r: (-r["machine_count"], r["item"].name.lower()))
    else:
        rows.sort(key=lambda r: (RAG_ORDER[r["rag"]], r["item"].name.lower()))

    counts = dict.fromkeys((RED, AMBER, GREEN, UNRATED), 0)
    for r in rows:
        counts[r["rag"]] += 1
    return rows, counts


def _hosts(session: Session) -> list[dict]:
    hosts = session.exec(select(SoftwareHost).order_by(SoftwareHost.collected_at.desc())).all()
    out, seen = [], set()
    for h in hosts:
        if h.asset_id in seen:
            continue
        seen.add(h.asset_id)
        asset = session.get(Asset, h.asset_id)
        out.append({
            "host": h,
            "label": _asset_label(asset, h.asset_id),
            "notes": json.loads(h.notes_json) if h.notes_json else [],
        })
    out.sort(key=lambda x: x["label"].lower())
    return out


def _params(q: str, machine: str, rag: str, show: str, sort: str):
    machine_id = int(machine) if machine.isdigit() else None
    rag = rag if rag in RAG_ORDER else ""
    sort = sort if sort in SORTS else "risk"
    return q, machine_id, rag, show == "all", sort


@router.get("/software")
def software_page(
    request: Request,
    session: Session = Depends(get_session),
    q: str = "",
    machine: str = "",
    rag: str = "",
    show: str = "",
    sort: str = "risk",
    eol: str = "",
):
    q, machine_id, rag, show_all, sort = _params(q, machine, rag, show, sort)
    rows, counts = build_rows(session, date.today(), q, machine_id, rag, show_all, sort)
    return templates.TemplateResponse(
        request,
        "software.html",
        {
            "rows": rows,
            "counts": counts,
            "hosts": _hosts(session),
            "q": q,
            "machine": machine_id,
            "rag": rag,
            "show_all": show_all,
            "sort": sort,
            "rag_labels": RAG_LABELS,
            "coverage": SOURCE_COVERAGE,
            "source_labels": SOURCE_LABELS,
            "eol": eol,
        },
    )


@router.get("/software.csv")
def software_csv(
    session: Session = Depends(get_session),
    q: str = "",
    machine: str = "",
    rag: str = "",
    show: str = "",
    sort: str = "risk",
):
    q, machine_id, rag, show_all, sort = _params(q, machine, rag, show, sort)
    rows, _ = build_rows(session, date.today(), q, machine_id, rag, show_all, sort)
    buf = io.StringIO()
    buf.write("﻿")  # Excel needs the BOM to read UTF-8
    writer = csv.writer(buf)
    writer.writerow(["Risk", "Software", "Category", "Version", "Published", "Date source",
                     "Machines", "Reasons"])
    for r in rows:
        for v in r["versions"]:
            writer.writerow([_csv_safe(x) for x in (
                RAG_LABELS[r["rag"]],
                r["item"].name,
                r["category"],
                v["version"],
                v["published"].isoformat() if v["published"] else "",
                v["published_source"] or "",
                ", ".join(m["label"] for m in v["machines"]),
                "; ".join(r["reasons"]),
            )])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="software.csv"'},
    )


@router.post("/software/refresh-eol")
async def software_refresh_eol(session: Session = Depends(get_session)):
    """Refresh end-of-life dates from endoflife.date (a few seconds to a
    minute). Shares the discovery page's one-run-at-a-time guard."""
    if _discovery_already_running(session):
        return RedirectResponse(url="/software?eol=busy", status_code=303)
    from discovery.cli import run_software_eol

    try:
        await run_in_threadpool(run_software_eol)
    except Exception:
        logger.exception("software end-of-life refresh failed")
        return RedirectResponse(url="/software?eol=failed", status_code=303)
    return RedirectResponse(url="/software?eol=done", status_code=303)
