from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.asset_children import delete_asset_cascade
from app.auth import require_admin, require_same_origin
from app.clock import utcnow_naive
from app.db import get_session
from app.models import (
    Asset,
    AssetInterface,
    AssetService,
    AssetType,
    CIRelationship,
    Criticality,
    LifecycleStatus,
)
from app.schemas import AssetCreate, AssetUpdate

router = APIRouter(
    prefix="/api/assets",
    tags=["assets"],
    dependencies=[Depends(require_admin), Depends(require_same_origin)],
)


@router.get("", response_model=list[Asset])
def list_assets(
    session: Session = Depends(get_session),
    asset_type: AssetType | None = None,
    criticality: Criticality | None = None,
    lifecycle_status: LifecycleStatus | None = None,
):
    query = select(Asset)
    if asset_type:
        query = query.where(Asset.asset_type == asset_type)
    if criticality:
        query = query.where(Asset.criticality == criticality)
    if lifecycle_status:
        query = query.where(Asset.lifecycle_status == lifecycle_status)
    query = query.order_by(Asset.last_seen.desc())
    return session.exec(query).all()


@router.get("/{asset_id}", response_model=Asset)
def get_asset(asset_id: int, session: Session = Depends(get_session)):
    asset = session.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.post("", response_model=Asset)
def create_asset(payload: AssetCreate, session: Session = Depends(get_session)):
    asset = Asset(**payload.model_dump())
    session.add(asset)
    session.commit()
    session.refresh(asset)
    return asset


@router.patch("/{asset_id}", response_model=Asset)
def update_asset(
    asset_id: int, payload: AssetUpdate, session: Session = Depends(get_session)
):
    asset = session.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(asset, key, value)
    asset.last_seen = utcnow_naive()
    session.add(asset)
    session.commit()
    session.refresh(asset)
    return asset


@router.delete("/{asset_id}")
def delete_asset(asset_id: int, session: Session = Depends(get_session)):
    asset = session.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    delete_asset_cascade(session, asset_id)
    session.commit()
    return {"deleted": asset_id}


@router.get("/{asset_id}/interfaces", response_model=list[AssetInterface])
def get_asset_interfaces(asset_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(AssetInterface).where(AssetInterface.asset_id == asset_id)
    ).all()


@router.get("/{asset_id}/services", response_model=list[AssetService])
def get_asset_services(asset_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(AssetService).where(AssetService.asset_id == asset_id)
    ).all()


@router.get("/{asset_id}/relationships", response_model=list[CIRelationship])
def get_asset_relationships(asset_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(CIRelationship).where(CIRelationship.asset_id == asset_id)
    ).all()
