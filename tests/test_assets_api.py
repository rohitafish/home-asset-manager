"""Route-level tests for app/routers/assets.py -- the JSON API under
/api/assets.

This router had no tests of any kind. It is small and mostly CRUD, but it is
also the one part of the app where a mistake is silent: the dashboard's HTML
routes get looked at by a human every time they're used, whereas nothing
renders these, so a filter that quietly stops filtering or a PATCH that
writes a field it shouldn't would sit there unnoticed.

The two things worth pinning beyond "it returns rows": DELETE goes through
delete_asset_cascade (the FK-violation bug class app/asset_children.py
exists for), and PATCH is partial -- exclude_unset is what stops a caller
that sends one field from nulling every other one.
"""

from datetime import date, datetime
from decimal import Decimal

from conftest import make_asset, make_interface
from sqlmodel import select

from app.models import (
    Asset,
    AssetNote,
    AssetService,
    AssetType,
    Criticality,
    Exposure,
    Finding,
    FindingStatus,
    LifecycleStatus,
    Severity,
)


def test_list_returns_every_asset(admin_client, session):
    make_asset(session, hostname="one")
    make_asset(session, hostname="two")

    response = admin_client.get("/api/assets")

    assert response.status_code == 200
    assert {a["hostname"] for a in response.json()} == {"one", "two"}


def test_list_filters_by_asset_type(admin_client, session):
    make_asset(session, hostname="a-server", asset_type=AssetType.server)
    make_asset(session, hostname="a-laptop", asset_type=AssetType.end_user_device)

    response = admin_client.get("/api/assets", params={"asset_type": "server"})

    assert [a["hostname"] for a in response.json()] == ["a-server"]


def test_list_filters_by_criticality(admin_client, session):
    make_asset(session, hostname="critical", criticality=Criticality.high)
    make_asset(session, hostname="ordinary", criticality=Criticality.low)

    response = admin_client.get("/api/assets", params={"criticality": "high"})

    assert [a["hostname"] for a in response.json()] == ["critical"]


def test_list_filters_by_lifecycle_status(admin_client, session):
    make_asset(session, hostname="live", lifecycle_status=LifecycleStatus.active)
    make_asset(session, hostname="gone", lifecycle_status=LifecycleStatus.decommissioned)

    response = admin_client.get("/api/assets", params={"lifecycle_status": "decommissioned"})

    assert [a["hostname"] for a in response.json()] == ["gone"]


def test_list_combines_filters_rather_than_replacing_them(admin_client, session):
    """Each filter is a separate `if` appending to one query -- an early
    return or a reassigned query would make the last one win."""
    make_asset(session, hostname="match", asset_type=AssetType.server,
               criticality=Criticality.high)
    make_asset(session, hostname="wrong-criticality", asset_type=AssetType.server,
               criticality=Criticality.low)
    make_asset(session, hostname="wrong-type", asset_type=AssetType.iot,
               criticality=Criticality.high)

    response = admin_client.get(
        "/api/assets", params={"asset_type": "server", "criticality": "high"}
    )

    assert [a["hostname"] for a in response.json()] == ["match"]


def test_list_is_ordered_by_last_seen_descending(admin_client, session):
    make_asset(session, hostname="stale", last_seen=datetime(2026, 1, 1))
    make_asset(session, hostname="fresh", last_seen=datetime(2026, 8, 1))

    response = admin_client.get("/api/assets")

    assert [a["hostname"] for a in response.json()][:2] == ["fresh", "stale"]


def test_list_rejects_an_unknown_enum_value(admin_client, session):
    """The filters are typed as enums, so a typo is a 422 rather than a
    silently empty result set."""
    assert admin_client.get("/api/assets", params={"asset_type": "toaster"}).status_code == 422


def test_get_returns_one_asset(admin_client, session):
    asset = make_asset(session, hostname="wanted")

    response = admin_client.get(f"/api/assets/{asset.id}")

    assert response.status_code == 200
    assert response.json()["hostname"] == "wanted"


def test_get_an_unknown_asset_is_404(admin_client, session):
    response = admin_client.get("/api/assets/9999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Asset not found"


def test_create_persists_the_asset(admin_client, session):
    response = admin_client.post(
        "/api/assets",
        json={"asset_type": "server", "hostname": "new-box", "purchase_price": "250.00"},
    )

    assert response.status_code == 200
    created = session.get(Asset, response.json()["id"])
    assert created.hostname == "new-box"
    assert created.purchase_price == Decimal("250.00")


def test_create_applies_the_schema_defaults(admin_client, session):
    """asset_type is the only required field; the rest of the record has to
    come out of AssetCreate's defaults rather than as NULLs."""
    response = admin_client.post("/api/assets", json={"asset_type": "iot"})

    created = session.get(Asset, response.json()["id"])
    assert created.criticality == Criticality.medium
    assert created.lifecycle_status == LifecycleStatus.active
    assert created.is_valuable is True
    assert created.identity_locked is False


def test_create_without_an_asset_type_is_rejected(admin_client, session):
    assert admin_client.post("/api/assets", json={"hostname": "no-type"}).status_code == 422


def test_patch_updates_only_the_fields_sent(admin_client, session):
    """The point of exclude_unset: a caller correcting one field must not
    blank out everything it didn't mention."""
    asset = make_asset(
        session, hostname="keep-me", vendor="Synology", model="DS220+",
        purchase_price=Decimal("399.99"),
    )

    response = admin_client.patch(f"/api/assets/{asset.id}", json={"model": "DS923+"})

    assert response.status_code == 200
    session.refresh(asset)
    assert asset.model == "DS923+"
    assert asset.hostname == "keep-me"
    assert asset.vendor == "Synology"
    assert asset.purchase_price == Decimal("399.99")


def test_patch_can_still_set_a_field_to_null_explicitly(admin_client, session):
    """exclude_unset distinguishes "absent" from "sent as null" -- clearing a
    field has to stay possible."""
    asset = make_asset(session, hostname="clear-me", vendor="Acme")

    admin_client.patch(f"/api/assets/{asset.id}", json={"vendor": None})

    session.refresh(asset)
    assert asset.vendor is None
    assert asset.hostname == "clear-me"


def test_patch_refreshes_last_seen(admin_client, session):
    asset = make_asset(session, hostname="touched", last_seen=datetime(2020, 1, 1))

    admin_client.patch(f"/api/assets/{asset.id}", json={"owner": "someone"})

    session.refresh(asset)
    assert asset.last_seen > datetime(2020, 1, 2)


def test_patch_an_unknown_asset_is_404(admin_client, session):
    assert admin_client.patch("/api/assets/9999", json={"model": "x"}).status_code == 404


def test_patch_rejects_an_unknown_enum_value(admin_client, session):
    asset = make_asset(session, hostname="typed")

    response = admin_client.patch(f"/api/assets/{asset.id}", json={"criticality": "urgent"})

    assert response.status_code == 422
    session.refresh(asset)
    assert asset.criticality == Criticality.medium


def test_delete_removes_the_asset_and_all_its_children(admin_client, session):
    """There is no ON DELETE CASCADE on these foreign keys, so the route has
    to clear children first. conftest turns SQLite's foreign_keys pragma on,
    so a child model missing from delete_asset_cascade fails here rather
    than as a ForeignKeyViolation against Postgres in production."""
    asset = make_asset(session, hostname="doomed")
    make_interface(session, asset.id, ip="10.0.0.3", mac="00:11:22:33:44:55")
    session.add(AssetService(asset_id=asset.id, port=443, protocol="tcp"))
    session.add(
        Finding(
            asset_id=asset.id, severity=Severity.low, exposure=Exposure.internal,
            detected_date=datetime(2026, 7, 1), status=FindingStatus.open,
        )
    )
    session.add(
        AssetNote(asset_id=asset.id, created_at=datetime(2026, 7, 1), author="t", body="b")
    )
    session.commit()
    asset_id = asset.id

    response = admin_client.delete(f"/api/assets/{asset_id}")

    assert response.status_code == 200
    assert response.json() == {"deleted": asset_id}
    assert session.get(Asset, asset_id) is None
    assert session.exec(select(Finding).where(Finding.asset_id == asset_id)).all() == []
    assert session.exec(select(AssetService).where(AssetService.asset_id == asset_id)).all() == []


def test_delete_an_unknown_asset_is_404(admin_client, session):
    assert admin_client.delete("/api/assets/9999").status_code == 404


def test_interfaces_services_and_relationships_are_scoped_to_the_asset(
    admin_client, session
):
    """All three sub-resources are the same one-line query with a different
    model -- the risk they share is a missing WHERE returning another
    asset's rows."""
    mine = make_asset(session, hostname="mine")
    theirs = make_asset(session, hostname="theirs")
    make_interface(session, mine.id, ip="10.0.0.1", mac="00:00:00:00:00:01")
    make_interface(session, theirs.id, ip="10.0.0.2", mac="00:00:00:00:00:02")
    session.add(AssetService(asset_id=mine.id, port=22, protocol="tcp"))
    session.add(AssetService(asset_id=theirs.id, port=80, protocol="tcp"))
    session.commit()

    interfaces = admin_client.get(f"/api/assets/{mine.id}/interfaces").json()
    services = admin_client.get(f"/api/assets/{mine.id}/services").json()
    relationships = admin_client.get(f"/api/assets/{mine.id}/relationships").json()

    assert [i["ip"] for i in interfaces] == ["10.0.0.1"]
    assert [s["port"] for s in services] == [22]
    assert relationships == []


def test_sub_resources_of_an_unknown_asset_are_empty_not_an_error(admin_client, session):
    """These three don't check the asset exists first -- they just return no
    rows. Pinned so the behaviour is a decision rather than an accident."""
    assert admin_client.get("/api/assets/9999/interfaces").json() == []
    assert admin_client.get("/api/assets/9999/services").json() == []
    assert admin_client.get("/api/assets/9999/relationships").json() == []


def test_dates_and_decimals_round_trip_through_json(admin_client, session):
    """Numeric(12, 2) and date columns cross the JSON boundary twice here --
    once as strings on the way in, once serialized on the way out."""
    response = admin_client.post(
        "/api/assets",
        json={
            "asset_type": "end_user_device",
            "hostname": "round-trip",
            "purchase_date": "2024-03-01",
            "purchase_price": "1234.56",
            "warranty_expiry": "2027-03-01",
        },
    )

    body = response.json()
    assert body["purchase_date"] == "2024-03-01"
    assert Decimal(str(body["purchase_price"])) == Decimal("1234.56")

    stored = session.get(Asset, body["id"])
    assert stored.purchase_date == date(2024, 3, 1)
    assert stored.warranty_expiry == date(2027, 3, 1)
