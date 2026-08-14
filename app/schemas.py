from datetime import date
from decimal import Decimal

from sqlmodel import SQLModel

from app.models import AssetType, Criticality, LifecycleStatus


class AssetCreate(SQLModel):
    asset_type: AssetType
    hostname: str | None = None
    hostname_locked: bool = False
    vendor: str | None = None
    vendor_locked: bool = False
    model: str | None = None
    firmware_version: str | None = None
    serial_number: str | None = None
    model_number: str | None = None
    model_identifier: str | None = None
    identity_locked: bool = False
    purchase_date: date | None = None
    purchase_price: Decimal | None = None
    replacement_value: Decimal | None = None
    warranty_expiry: date | None = None
    owner: str | None = None
    custodian: str | None = None
    criticality: Criticality = Criticality.medium
    classification: str | None = None
    lifecycle_status: LifecycleStatus = LifecycleStatus.active
    is_internet_facing: bool = False
    location_id: int | None = None
    position: str | None = None


class AssetUpdate(SQLModel):
    asset_type: AssetType | None = None
    hostname: str | None = None
    hostname_locked: bool | None = None
    vendor: str | None = None
    vendor_locked: bool | None = None
    model: str | None = None
    firmware_version: str | None = None
    serial_number: str | None = None
    model_number: str | None = None
    model_identifier: str | None = None
    identity_locked: bool | None = None
    purchase_date: date | None = None
    purchase_price: Decimal | None = None
    replacement_value: Decimal | None = None
    warranty_expiry: date | None = None
    owner: str | None = None
    custodian: str | None = None
    criticality: Criticality | None = None
    classification: str | None = None
    lifecycle_status: LifecycleStatus | None = None
    is_internet_facing: bool | None = None
    location_id: int | None = None
    position: str | None = None
