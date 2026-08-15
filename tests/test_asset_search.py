"""Tests for app/asset_search.py's asset_search_filter -- the shared text
search behind both the Assets page's search box (app/routers/dashboard.py)
and the AI assistant's search_assets tool (app/assistant.py), so both are
covered by testing the one shared filter.
"""

from conftest import make_asset
from sqlmodel import select

from app.asset_search import ASSET_SEARCH_FIELDS, asset_search_filter
from app.models import Asset


def _search(session, q):
    return session.exec(select(Asset).where(asset_search_filter(q))).all()


def test_matches_hostname_substring_case_insensitively(session):
    make_asset(session, hostname="kitchen-speaker")
    assert [a.hostname for a in _search(session, "SPEAKER")] == ["kitchen-speaker"]


def test_matches_model_a_hostname_does_not_contain(session):
    """The motivating case: 'move' should find a Sonos Move even though the
    word never appears in its hostname."""
    make_asset(session, hostname="living-room-1", model="Sonos Move")
    assert len(_search(session, "move")) == 1


def test_matches_model_number_and_model_identifier(session):
    make_asset(session, hostname="unifi-1", model_number="UDR-Ultra")
    make_asset(session, hostname="mini-1", model_identifier="Macmini9,1")
    assert len(_search(session, "ultra")) == 1
    assert len(_search(session, "macmini9")) == 1


def test_matches_owner_custodian_and_position(session):
    make_asset(session, hostname="a", owner="Alex")
    make_asset(session, hostname="b", custodian="Sam")
    make_asset(session, hostname="c", position="socket behind the sofa")
    assert len(_search(session, "alex")) == 1
    assert len(_search(session, "sam")) == 1
    assert len(_search(session, "sofa")) == 1


def test_no_match_returns_empty(session):
    make_asset(session, hostname="kitchen-speaker")
    assert _search(session, "nonexistent-term") == []


def test_does_not_match_unrelated_fields():
    """firmware_version/classification/serial_number aren't in scope --
    ASSET_SEARCH_FIELDS is deliberately the name/model/owner fields a human
    would actually search by, not every text column on Asset."""
    field_names = {f.key for f in ASSET_SEARCH_FIELDS}
    assert field_names == {
        "hostname", "vendor", "model", "model_number",
        "model_identifier", "owner", "custodian", "position",
    }
    assert "firmware_version" not in field_names
    assert "serial_number" not in field_names
    assert Asset.firmware_version.key == "firmware_version"  # sanity: real column
