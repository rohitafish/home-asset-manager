"""One-off seed script for the ISOLATED demo database used to capture
screenshots for the wiki/Pages site. Every value here is fabricated -- no
real household data. Run against the demo Postgres container only:

    DATABASE_URL=postgresql+psycopg://demo:demo@localhost:5433/assetmgt_demo \
        .venv/bin/python wiki-drafts/_seed_demo_data.py

Not part of the app; deleted after screenshots are captured.
"""
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Safety: refuse to run against anything that doesn't look like the demo DB.
db_url = os.environ.get("DATABASE_URL", "")
assert "5433" in db_url and "assetmgt_demo" in db_url, (
    f"Refusing to seed -- DATABASE_URL doesn't look like the isolated demo DB: {db_url!r}"
)

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import (  # noqa: E402
    Asset,
    AssetInterface,
    AssetNote,
    AssetService,
    AssetType,
    ChangeProposal,
    ChatMessage,
    Criticality,
    DiscoveryRun,
    LifecycleStatus,
    Location,
)

TODAY = date(2026, 8, 14)


def main():
    with Session(engine) as s:
        # Wipe anything already there (idempotent re-runs while iterating).
        for model in [ChangeProposal, ChatMessage, AssetNote, AssetService,
                      AssetInterface, DiscoveryRun, Asset, Location]:
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()

        locations = {
            name: Location(name=name)
            for name in ["Living Room", "Kitchen", "Office", "Bedroom", "Garage"]
        }
        for loc in locations.values():
            s.add(loc)
        s.commit()
        for loc in locations.values():
            s.refresh(loc)

        def asset(**kw):
            a = Asset(**kw)
            s.add(a)
            s.commit()
            s.refresh(a)
            return a

        def iface(a, mac, ip, connection_type, vlan=1, network_name="Home"):
            s.add(AssetInterface(
                asset_id=a.id, mac=mac, ip=ip, vlan=vlan, network_name=network_name,
                connection_type=connection_type,
            ))

        def service(a, port, product, version, protocol="tcp"):
            s.add(AssetService(asset_id=a.id, port=port, protocol=protocol,
                                product=product, version=version))

        def note(a, body, author="user"):
            s.add(AssetNote(asset_id=a.id, author=author, body=body))

        # ---- Ordinary assets ----------------------------------------------
        echo = asset(
            asset_type=AssetType.iot, hostname="Echo Dot (3rd Gen)", vendor="Amazon",
            model="Echo Dot", model_number="Echo (3rd Gen)", owner="Alex",
            criticality=Criticality.low, lifecycle_status=LifecycleStatus.active,
            location_id=locations["Living Room"].id, position="Bookshelf",
            purchase_date=date(2021, 11, 20), purchase_price=Decimal("39.99"),
            replacement_value=Decimal("40.00"),
            last_seen=datetime(2026, 8, 13, 21, 4),
        )
        iface(echo, "aa:bb:cc:10:00:01", "192.168.1.101", "wireless")

        sonos = asset(
            asset_type=AssetType.iot, hostname="Sonos One (Kitchen)", vendor="Sonos",
            model="One", model_number="S18", owner="Jordan",
            criticality=Criticality.low, lifecycle_status=LifecycleStatus.active,
            location_id=locations["Kitchen"].id,
            purchase_date=date(2022, 3, 4), purchase_price=Decimal("219.00"),
            replacement_value=Decimal("219.00"),
            last_seen=datetime(2026, 8, 14, 8, 30),
        )
        iface(sonos, "aa:bb:cc:10:00:02", "192.168.1.102", "wireless")
        s.add(AssetNote(asset_id=sonos.id, author="claude",
                         body='Sonos reports its own zone name as "Kitchen". Position suggestion recorded.'))

        # Same physical laptop, two NICs -- deliberately NOT linked yet, so it
        # shows up as a candidate on /assets/investigate.
        laptop_wired = asset(
            asset_type=AssetType.end_user_device, hostname="nimbus-macbook", vendor="Apple",
            model="MacBook Pro", model_number="MK1E3B/A", model_identifier="Mac15,6",
            owner="Alex", criticality=Criticality.high, lifecycle_status=LifecycleStatus.active,
            location_id=locations["Office"].id,
            purchase_date=date(2023, 10, 2), purchase_price=Decimal("2149.00"),
            replacement_value=Decimal("2149.00"),
            last_seen=datetime(2026, 8, 14, 9, 15),
        )
        iface(laptop_wired, "aa:bb:cc:10:00:03", "192.168.1.50", "wired")
        service(laptop_wired, 22, "OpenSSH", "9.6")

        laptop_wifi = asset(
            asset_type=AssetType.end_user_device, hostname="Alex's nimbus-macbook", vendor="Apple",
            owner="Alex", criticality=Criticality.high, lifecycle_status=LifecycleStatus.discovered,
            last_seen=datetime(2026, 8, 14, 9, 12),
        )
        iface(laptop_wifi, "aa:bb:cc:10:00:04", "192.168.1.103", "wireless")

        tv = asset(
            asset_type=AssetType.iot, hostname="Living Room TV", vendor="Samsung",
            model="QN90C", owner="Jordan", criticality=Criticality.low,
            lifecycle_status=LifecycleStatus.active, location_id=locations["Living Room"].id,
            purchase_date=date(2023, 12, 26), purchase_price=Decimal("1299.00"),
            replacement_value=Decimal("1299.00"),
            last_seen=datetime(2026, 8, 13, 22, 40),
        )
        iface(tv, "aa:bb:cc:10:00:05", "192.168.1.104", "wireless")

        router = asset(
            asset_type=AssetType.network_device, hostname="UDR", vendor="Ubiquiti",
            model="UniFi Dream Router", model_number="UDR", criticality=Criticality.critical,
            lifecycle_status=LifecycleStatus.active, location_id=locations["Garage"].id,
            is_internet_facing=True,
            purchase_date=date(2022, 6, 1), purchase_price=Decimal("179.00"),
            replacement_value=Decimal("179.00"),
            last_seen=datetime(2026, 8, 14, 9, 0),
        )
        iface(router, "aa:bb:cc:10:00:06", "192.168.1.1", "wired")

        plug = asset(
            asset_type=AssetType.iot, hostname="Kitchen Plug", vendor="TP-Link",
            model="Kasa HS100", owner="Jordan", criticality=Criticality.low,
            lifecycle_status=LifecycleStatus.active, location_id=locations["Kitchen"].id,
            purchase_date=date(2021, 4, 18), purchase_price=Decimal("14.99"),
            replacement_value=Decimal("14.99"),
            last_seen=datetime(2026, 8, 14, 7, 55),
        )
        iface(plug, "aa:bb:cc:10:00:07", "192.168.1.105", "wireless")

        ipad = asset(
            asset_type=AssetType.mobile, hostname="Jordan's iPad", vendor="Apple",
            model="iPad Air", model_number="MM6Q3B/A", owner="Jordan",
            criticality=Criticality.medium, lifecycle_status=LifecycleStatus.active,
            location_id=locations["Bedroom"].id,
            purchase_date=date(2024, 5, 14), purchase_price=Decimal("599.00"),
            replacement_value=Decimal("599.00"), warranty_expiry=date(2026, 5, 14),
            last_seen=datetime(2026, 8, 14, 8, 2),
        )
        iface(ipad, "aa:bb:cc:10:00:08", "192.168.1.106", "wireless")

        nas = asset(
            asset_type=AssetType.server, hostname="home-nas", vendor="Synology",
            model="DS923+", owner="Alex", criticality=Criticality.high,
            lifecycle_status=LifecycleStatus.active, location_id=locations["Office"].id,
            purchase_date=date(2023, 2, 9), purchase_price=Decimal("549.00"),
            replacement_value=Decimal("549.00"),
            last_seen=datetime(2026, 8, 14, 9, 20),
        )
        iface(nas, "aa:bb:cc:10:00:09", "192.168.1.30", "wired")
        service(nas, 5000, "Synology DiskStation", "DSM 7.2")

        roku = asset(
            asset_type=AssetType.iot, hostname="Bedroom Roku", vendor="Roku",
            model="Streaming Stick 4K", owner="Jordan", criticality=Criticality.low,
            lifecycle_status=LifecycleStatus.discovered, location_id=locations["Bedroom"].id,
            last_seen=datetime(2026, 8, 13, 23, 10),
        )
        iface(roku, "aa:bb:cc:10:00:0a", "192.168.1.107", "wireless")

        old_speaker = asset(
            asset_type=AssetType.iot, hostname="Old Sonos Play:1", vendor="Sonos",
            model="Play:1", owner="Alex", criticality=Criticality.low,
            lifecycle_status=LifecycleStatus.active, location_id=locations["Garage"].id,
            purchase_date=date(2016, 1, 3), purchase_price=Decimal("175.00"),
            replacement_value=Decimal("175.00"),  # flagged needs_review (>8yr) in the real UI
            last_seen=datetime(2026, 8, 10, 18, 0),
        )
        iface(old_speaker, "aa:bb:cc:10:00:0b", "192.168.1.108", "wireless")

        # ---- Discovery run history -----------------------------------------
        s.add(DiscoveryRun(
            source="unifi", started_at=datetime(2026, 8, 14, 9, 0, 2),
            finished_at=datetime(2026, 8, 14, 9, 0, 19), status="completed",
            summary="11 clients, 1 device. 0 new, 2 updated.",
        ))
        s.add(DiscoveryRun(
            source="nmap", started_at=datetime(2026, 8, 14, 8, 58, 0),
            finished_at=datetime(2026, 8, 14, 9, 1, 47), status="completed",
            summary="Ping sweep: 11 hosts up. Service scan: 3 hosts with open ports.",
        ))
        s.add(DiscoveryRun(
            source="sonos_household", started_at=datetime(2026, 8, 14, 8, 30, 5),
            finished_at=datetime(2026, 8, 14, 8, 30, 8), status="completed",
            summary="2 players found (Kitchen, Garage).",
        ))
        s.add(DiscoveryRun(
            source="nmap", started_at=datetime(2026, 8, 13, 20, 0, 0),
            finished_at=datetime(2026, 8, 13, 20, 4, 12), status="failed",
            summary="nmap failed (1): sudo: a password is required",
        ))

        # ---- Chat transcript + a pending proposal, for the asset-detail shot
        s.add(ChatMessage(asset_id=laptop_wired.id, role="user", content_json=json.dumps([
            {"type": "text", "text": "What model MacBook Pro is this, roughly?"},
        ])))
        s.add(ChatMessage(asset_id=laptop_wired.id, role="assistant", content_json=json.dumps([
            {"type": "text", "text": (
                "Based on the vendor (Apple), model (MacBook Pro) and purchase date "
                "(October 2023), this matches the 14-inch MacBook Pro (M3, Nov 2023) "
                "line -- model number MK1E3B/A for the UK 8GB/512GB base config. "
                "I've proposed setting model_number accordingly; please confirm "
                "against the About This Mac panel if you'd like it verified rather "
                "than unverified."
            )},
        ])))
        s.add(ChangeProposal(
            asset_id=laptop_wired.id, origin_asset_id=laptop_wired.id,
            kind="set_field",
            payload_json=json.dumps({"field_name": "model_number", "value": "MK1E3B/A (unverified)"}),
            rationale="Matches the 14\" MacBook Pro M3 (Nov 2023) line for this vendor/model/purchase date.",
            status="pending",
        ))

        note(laptop_wired, "Confirmed serial on the base plate -- matches Apple's warranty lookup.")

        s.commit()
        print("Seeded demo data OK.")  # noqa: T201 -- standalone CLI script, not app code


if __name__ == "__main__":
    main()
