from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import Column, Numeric
from sqlmodel import Field, SQLModel

from app.clock import utcnow_naive


class AssetType(str, Enum):
    end_user_device = "end_user_device"
    mobile = "mobile"
    server = "server"
    network_device = "network_device"
    iot = "iot"
    removable_media = "removable_media"
    cloud_service = "cloud_service"
    software = "software"


class Criticality(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class LifecycleStatus(str, Enum):
    discovered = "discovered"
    active = "active"
    decommissioned = "decommissioned"


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class Exposure(str, Enum):
    internet_facing = "internet_facing"
    internal = "internal"


class FindingStatus(str, Enum):
    open = "open"
    mitigated = "mitigated"
    accepted = "accepted"
    closed = "closed"


class Location(SQLModel, table=True):
    """A physical room/space an asset can be assigned to (e.g. "Kitchen",
    "Lounge"). Deliberately flat -- not a building/floor/room hierarchy --
    since a single home doesn't need that much structure."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: str | None = None
    sort_order: int = 0


class Asset(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    asset_type: AssetType
    hostname: str | None = None
    hostname_locked: bool = False  # if True, discovery won't overwrite hostname
    hostname_source: str | None = None  # which collector last set hostname, for priority
    vendor: str | None = None
    vendor_locked: bool = False  # if True, discovery won't overwrite vendor
    model: str | None = None  # e.g. UniFi's device model, or a probe's identification
    firmware_version: str | None = None
    serial_number: str | None = Field(default=None, index=True)
    model_number: str | None = None  # e.g. Apple's "MGNR3B/A" or a UniFi SKU like "UDRULT"
    # When the model_number auto-guess last ran for this asset (success OR a
    # no-answer miss). Gates _autofill_model_number so a guess that comes back
    # "unknown" isn't re-attempted -- a fresh ~10s LLM call -- on every save.
    model_number_guess_attempted_at: datetime | None = None
    model_identifier: str | None = None  # e.g. Apple's "Macmini9,1"
    identity_locked: bool = False  # if True, discovery won't overwrite serial_number/model_number/model_identifier
    purchase_date: date | None = None
    purchase_price: Decimal | None = Field(default=None, sa_column=Column(Numeric(12, 2)))
    replacement_value: Decimal | None = Field(default=None, sa_column=Column(Numeric(12, 2)))
    warranty_expiry: date | None = None
    owner: str | None = None
    custodian: str | None = None
    criticality: Criticality = Criticality.medium
    classification: str | None = None
    lifecycle_status: LifecycleStatus = LifecycleStatus.discovered
    is_internet_facing: bool = False
    # Independent of criticality -- a cheap smart plug can legitimately be
    # high criticality (attack-surface concern) while being worth nothing
    # for insurance. Gates the Valuables page's default view
    # (_valuables_query in app/routers/dashboard.py); doesn't affect
    # anything else. Defaults True so an existing/new asset is visible
    # there until someone explicitly says otherwise.
    is_valuable: bool = True
    first_seen: datetime = Field(default_factory=utcnow_naive)
    last_seen: datetime = Field(default_factory=utcnow_naive)
    source: str | None = None
    location_id: int | None = Field(default=None, foreign_key="location.id", index=True)
    position: str | None = None  # free-text detail, e.g. "socket behind the sofa"


class AssetInterface(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    mac: str | None = Field(default=None, index=True)
    ip: str | None = Field(default=None, index=True)
    vlan: int | None = None
    network_name: str | None = None  # e.g. "Sky", "Sunshine" -- from UniFi's /networks
    connection_type: str | None = None  # wired / wireless
    uplink_name: str | None = None
    vendor: str | None = None  # MAC OUI vendor, from nmap's host discovery
    last_seen: datetime = Field(default_factory=utcnow_naive)


class AssetService(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    port: int
    protocol: str = "tcp"
    product: str | None = None
    version: str | None = None
    banner: str | None = None
    last_seen: datetime = Field(default_factory=utcnow_naive)


class CIRelationship(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    related_asset_id: int = Field(foreign_key="asset.id", index=True)
    relationship_type: str  # e.g. "connected_via_ap", "uplinked_to_switch_port", "same_physical_device"
    detail: str | None = None


class AssetNote(SQLModel, table=True):
    """Append-only investigation log entry for an asset. Replaces the old
    single overwritable asset.notes field so the history of an investigation
    (yours or Claude's) is never lost."""

    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    created_at: datetime = Field(default_factory=utcnow_naive)
    author: str = "user"  # "user" | "claude" | "imported"
    body: str


class ProbeResult(SQLModel, table=True):
    """Raw evidence from a read-only identification probe (see probes/) run
    against one of an asset's interfaces. facts/suggestions are stored as
    JSON text -- no JSON column type is used elsewhere in this schema, so a
    plain string (parsed with json.loads by callers) keeps this consistent
    with the rest of the app rather than introducing a new column style."""

    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    probe_name: str
    target_ip: str | None = None
    ran_at: datetime = Field(default_factory=utcnow_naive)
    ok: bool
    summary: str
    facts_json: str | None = None
    suggestions_json: str | None = None
    raw: str | None = None


class ChatMessage(SQLModel, table=True):
    """One turn of the per-asset Claude investigation chat. content_json is
    the verbatim Anthropic content-block list (text/tool_use/tool_result/
    thinking blocks), not just extracted text -- replaying the raw blocks is
    what makes tool_use/tool_result pairing round-trip correctly on the next
    turn."""

    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    role: str  # "user" | "assistant"
    content_json: str
    created_at: datetime = Field(default_factory=utcnow_naive)


class ChangeProposal(SQLModel, table=True):
    """A change Claude drafted but did not make. Nothing here is applied to
    the asset until a human clicks Apply (see app/assistant.py)."""

    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)  # the target of the change
    # The asset whose chat produced this proposal. Differs from asset_id when a
    # multi-asset document (e.g. one invoice covering several devices) was
    # analysed on one asset's page but proposes changes to others -- the origin
    # is what lets that asset's panel surface the cross-asset proposals.
    origin_asset_id: int | None = Field(default=None, foreign_key="asset.id", index=True)
    kind: str  # "set_field" | "add_note" | "set_location" | "link_same_device"
    payload_json: str
    rationale: str | None = None
    status: str = "pending"  # "pending" | "applied" | "discarded"
    created_at: datetime = Field(default_factory=utcnow_naive)
    applied_at: datetime | None = None


class DiscoveryRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str  # unifi / nmap
    started_at: datetime = Field(default_factory=utcnow_naive)
    finished_at: datetime | None = None
    status: str = "running"  # running / completed / failed
    summary: str | None = None


class Vulnerability(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    cve_id: str = Field(unique=True, index=True)
    cvss_score: float | None = None
    severity: Severity | None = None
    epss_score: float | None = None
    kev_flag: bool = False
    description: str | None = None
    published_date: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow_naive)


class Finding(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    vulnerability_id: int | None = Field(default=None, foreign_key="vulnerability.id")
    ad_hoc_title: str | None = None
    severity: Severity
    exposure: Exposure = Exposure.internal
    detected_date: datetime = Field(default_factory=utcnow_naive)
    sla_due_date: datetime | None = None
    status: FindingStatus = FindingStatus.open
    closed_date: datetime | None = None
    evidence: str | None = None
    source: str | None = None
