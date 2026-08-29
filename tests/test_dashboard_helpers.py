"""Tests for the small pure helpers in app/routers/dashboard.py: warranty
bucketing, the tolerant date/money form parsers, and _valuables_query's
sorting (the testable seam behind the Valuables page's sortable columns --
there's no route-level TestClient anywhere in this app, see conftest.py).
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from conftest import make_asset
from sqlmodel import select

from app.assistant import apply_proposal
from app.models import (
    AssetNote,
    AssetType,
    ChangeProposal,
    Criticality,
    DiscoveryRun,
    Location,
)
from app.routers.dashboard import (
    _WARRANTY_LEAVING_SOON_DAYS,
    _autofill_model_number,
    _autofill_replacement_value,
    _chat_panel_context,
    _clean_enum_filter,
    _csv_safe,
    _discovery_already_running,
    _parse_date_field,
    _parse_money_field,
    _pending_proposals,
    _valuables_query,
    warranty_state,
)

# -- warranty_state ---------------------------------------------------------------


def test_warranty_state_unknown_when_no_expiry():
    assert warranty_state(None) == ("unknown", "—")


def test_warranty_state_in_warranty():
    expiry = date.today() + timedelta(days=_WARRANTY_LEAVING_SOON_DAYS + 1)
    state, label = warranty_state(expiry)
    assert state == "in"
    assert "In warranty" in label


def test_warranty_state_leaving_soon_at_exact_boundary():
    expiry = date.today() + timedelta(days=_WARRANTY_LEAVING_SOON_DAYS)
    state, label = warranty_state(expiry)
    assert state == "leaving"
    assert "Leaving warranty" in label


def test_warranty_state_leaving_soon_one_day_inside_boundary():
    expiry = date.today() + timedelta(days=1)
    state, _ = warranty_state(expiry)
    assert state == "leaving"


def test_warranty_state_out_of_warranty():
    expiry = date.today() - timedelta(days=1)
    state, label = warranty_state(expiry)
    assert state == "out"
    assert "Out of warranty" in label


def test_warranty_state_out_of_warranty_today_is_still_in():
    """days_left == 0 is not negative, so it falls into "leaving," not
    "out" -- the boundary condition worth pinning."""
    expiry = date.today()
    state, _ = warranty_state(expiry)
    assert state == "leaving"


# -- _parse_date_field --------------------------------------------------------------


def test_parse_date_field_blank_string_is_none():
    assert _parse_date_field("") is None
    assert _parse_date_field("   ") is None


def test_parse_date_field_unparseable_is_none():
    assert _parse_date_field("not-a-date") is None


def test_parse_date_field_valid_iso_date():
    assert _parse_date_field("2026-03-05") == date(2026, 3, 5)


# -- _parse_money_field --------------------------------------------------------------


def test_parse_money_field_plain_numeric():
    assert _parse_money_field("1234.56") == Decimal("1234.56")


def test_parse_money_field_currency_symbol_prefixed():
    assert _parse_money_field("£1234.56") == Decimal("1234.56")
    assert _parse_money_field("$1234.56") == Decimal("1234.56")
    assert _parse_money_field("€1234.56") == Decimal("1234.56")


def test_parse_money_field_thousands_separator():
    assert _parse_money_field("1,234.56") == Decimal("1234.56")


def test_parse_money_field_unparseable_is_none():
    assert _parse_money_field("not-a-price") is None


def test_parse_money_field_nan_is_none():
    """Regression test: Decimal("NaN").quantize(...) returns Decimal('NaN')
    without raising -- the one input the except clause doesn't catch.
    Postgres stores NaN happily, poisoning any SUM() over the column."""
    assert _parse_money_field("NaN") is None
    assert _parse_money_field("nan") is None


def test_parse_money_field_infinity_is_none():
    assert _parse_money_field("Infinity") is None


def test_parse_money_field_negative_is_none():
    assert _parse_money_field("-50.00") is None


# -- _clean_enum_filter -------------------------------------------------------
# Regression coverage: comparing an unrecognised string straight against a
# Postgres enum column (asset_type/criticality/lifecycle_status/status) used
# to raise InvalidTextRepresentation -- a 500 -- for something as mundane as
# a stale bookmark from before a value was renamed.


def test_clean_enum_filter_keeps_a_recognised_value():
    assert _clean_enum_filter("critical", ["critical", "high", "medium", "low"]) == "critical"


def test_clean_enum_filter_drops_an_unrecognised_value():
    assert _clean_enum_filter("urgent", ["critical", "high", "medium", "low"]) is None


def test_clean_enum_filter_drops_empty_and_none():
    assert _clean_enum_filter("", ["critical"]) is None
    assert _clean_enum_filter(None, ["critical"]) is None


# -- _discovery_already_running -----------------------------------------------


def test_discovery_already_running_false_with_no_runs(session):
    assert _discovery_already_running(session) is False


def test_discovery_already_running_true_while_a_run_is_in_progress(session):
    session.add(DiscoveryRun(source="nmap"))  # status defaults to "running"
    session.commit()
    assert _discovery_already_running(session) is True


def test_discovery_already_running_false_once_completed_or_failed(session):
    session.add(DiscoveryRun(source="nmap", status="completed"))
    session.add(DiscoveryRun(source="unifi", status="failed"))
    session.commit()
    assert _discovery_already_running(session) is False


# -- _autofill_replacement_value --------------------------------------------
# The save routes call this once price/date/type are assigned. end_user_device
# has zero drift, so the expected figure is exactly purchase_price (floored) --
# deterministic regardless of today's date, no need to pin as_of.


def test_autofill_sets_blank_replacement_from_price_and_date(session):
    asset = make_asset(
        session, asset_type=AssetType.end_user_device,
        purchase_price=Decimal("1099.00"), purchase_date=date(2022, 1, 1),
        replacement_value=None,
    )

    _autofill_replacement_value(asset)

    assert asset.replacement_value == Decimal(1099)  # zero drift -> == price


def test_autofill_leaves_an_existing_value_untouched(session):
    asset = make_asset(
        session, asset_type=AssetType.end_user_device,
        purchase_price=Decimal("1099.00"), purchase_date=date(2022, 1, 1),
        replacement_value=Decimal("800.00"),
    )

    _autofill_replacement_value(asset)

    assert asset.replacement_value == Decimal("800.00")  # manual figure kept


def test_autofill_is_noop_when_date_missing(session):
    asset = make_asset(
        session, asset_type=AssetType.end_user_device,
        purchase_price=Decimal("1099.00"), purchase_date=None,
        replacement_value=None,
    )

    _autofill_replacement_value(asset)

    assert asset.replacement_value is None  # can't value without a date


def test_autofill_is_noop_for_non_insurable_type(session):
    asset = make_asset(
        session, asset_type=AssetType.software,
        purchase_price=Decimal("99.00"), purchase_date=date(2022, 1, 1),
        replacement_value=None,
    )

    _autofill_replacement_value(asset)

    assert asset.replacement_value is None  # software isn't insurable contents


# -- _pending_proposals + the apply-all loop --------------------------------
# proposals_apply_all is a thin route: `for p in _pending_proposals(...):
# apply_proposal(session, p)`, then one commit. These pin that logic (the app
# has no route-level TestClient -- see this module's docstring).


def _set_field(session, asset_id, field_name, value, created_at):
    p = ChangeProposal(
        asset_id=asset_id, kind="set_field", created_at=created_at,
        payload_json=f'{{"field_name": "{field_name}", "value": "{value}"}}',
    )
    session.add(p)
    session.commit()
    return p


def test_pending_proposals_returns_only_pending_in_created_order(session):
    asset = make_asset(session)
    base = datetime(2026, 1, 1, 12, 0, 0)
    p1 = _set_field(session, asset.id, "vendor", "Apple", base)
    p2 = _set_field(session, asset.id, "model", "MacBook", base + timedelta(seconds=1))
    applied = _set_field(session, asset.id, "owner", "Alex", base + timedelta(seconds=2))
    applied.status = "applied"
    session.add(applied)
    session.commit()

    pending = _pending_proposals(session, asset.id)

    assert [p.id for p in pending] == [p1.id, p2.id]  # ordered, applied excluded


def test_apply_all_loop_applies_every_pending_proposal(session):
    asset = make_asset(session)
    base = datetime(2026, 1, 1, 12, 0, 0)
    _set_field(session, asset.id, "vendor", "Apple", base)
    _set_field(session, asset.id, "model", "MacBook Pro", base + timedelta(seconds=1))
    _set_field(session, asset.id, "purchase_price", "612.50", base + timedelta(seconds=2))

    for proposal in _pending_proposals(session, asset.id):
        apply_proposal(session, proposal)
    session.commit()

    session.refresh(asset)
    assert asset.vendor == "Apple"
    assert asset.model == "MacBook Pro"
    assert asset.purchase_price == Decimal("612.50")
    assert _pending_proposals(session, asset.id) == []  # list cleared


def test_apply_all_loop_survives_an_invalid_proposal(session):
    """One un-appliable proposal must not sink the batch: it self-discards, the
    valid ones still apply, and nothing raises (what a 500-free Apply all needs)."""
    asset = make_asset(session)
    base = datetime(2026, 1, 1, 12, 0, 0)
    good = _set_field(session, asset.id, "vendor", "Apple", base)
    bad = _set_field(session, asset.id, "criticality", "extremely high", base + timedelta(seconds=1))

    for proposal in _pending_proposals(session, asset.id):
        apply_proposal(session, proposal)
    session.commit()

    session.refresh(asset)
    session.refresh(good)
    session.refresh(bad)
    assert asset.vendor == "Apple"
    assert good.status == "applied"
    assert bad.status == "discarded"
    assert _pending_proposals(session, asset.id) == []


# The assistant fills purchase price/date via apply_proposal (not the form), so
# the apply routes must run the same auto-fill afterwards. These replicate that
# route sequence: apply the proposals, then _autofill_replacement_value.


def test_applied_purchase_proposals_then_autofill_populates_replacement(session):
    asset = make_asset(
        session, asset_type=AssetType.network_device,  # zero drift -> replacement == price
        purchase_price=None, purchase_date=None, replacement_value=None,
    )
    base = datetime(2026, 1, 1, 12, 0, 0)
    _set_field(session, asset.id, "purchase_price", "360.00", base)
    _set_field(session, asset.id, "purchase_date", "2025-12-29", base + timedelta(seconds=1))

    for proposal in _pending_proposals(session, asset.id):
        apply_proposal(session, proposal)
    _autofill_replacement_value(asset)  # what the apply routes now do before commit
    session.commit()

    session.refresh(asset)
    assert asset.replacement_value == Decimal(360)


def _cross(session, target_id, origin_id, field="vendor", value="X"):
    p = ChangeProposal(
        asset_id=target_id, origin_asset_id=origin_id, kind="set_field",
        payload_json=f'{{"field_name": "{field}", "value": "{value}"}}',
    )
    session.add(p)
    session.commit()
    return p


def test_pending_proposals_origin_filter_scopes_to_one_chat(session):
    a = make_asset(session)  # chat A
    b = make_asset(session)  # target
    c = make_asset(session)  # chat C
    from_a = _cross(session, b.id, a.id)
    from_c = _cross(session, b.id, c.id)

    # unfiltered: every pending proposal targeting b
    assert {p.id for p in _pending_proposals(session, b.id)} == {from_a.id, from_c.id}
    # origin-scoped: only the ones A's chat produced (what "Apply all for b"
    # from A's panel must apply)
    assert [p.id for p in _pending_proposals(session, b.id, origin_asset_id=a.id)] == [from_a.id]


def test_chat_panel_context_separates_own_and_cross_asset_proposals(session, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")  # make is_configured() true
    a = make_asset(session)  # the panel we're viewing (origin)
    b = make_asset(session)  # a second device the invoice also covered
    own = _cross(session, a.id, a.id, value="own")        # A's own proposal
    cross = _cross(session, b.id, a.id, value="cross")    # from A's chat, targets B

    ctx = _chat_panel_context(session, a)

    own_ids = [pd["row"].id for pd in ctx["proposals"]]
    assert own.id in own_ids and cross.id not in own_ids  # own only in `proposals`
    groups = ctx["other_asset_proposals"]
    assert len(groups) == 1 and groups[0]["asset"].id == b.id
    assert [pd["row"].id for pd in groups[0]["proposals"]] == [cross.id]


def test_chat_panel_context_includes_spend_summary_even_when_not_configured(session, monkeypatch):
    """The budget/kill-switch display and toggle are meaningful (and the
    toggle stays clickable) whether or not an API key is currently set --
    unlike transcript/proposals, spend isn't gated behind `configured`."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    asset = make_asset(session)

    ctx = _chat_panel_context(session, asset)

    assert ctx["configured"] is False
    assert ctx["spend"]["enabled"] is True  # kill switch defaults to on
    assert ctx["spend"]["day_spend"] == Decimal(0)


def test_autofill_after_applying_only_price_is_still_blank(session):
    """Price applied but date not yet -- the auto-fill run after that single
    apply must stay a no-op until the date is present too."""
    asset = make_asset(
        session, asset_type=AssetType.network_device,
        purchase_price=None, purchase_date=None, replacement_value=None,
    )
    base = datetime(2026, 1, 1, 12, 0, 0)
    price = _set_field(session, asset.id, "purchase_price", "360.00", base)

    apply_proposal(session, price)
    _autofill_replacement_value(asset)
    session.commit()

    session.refresh(asset)
    assert asset.replacement_value is None


def test_parse_money_field_blank_is_none():
    assert _parse_money_field("") is None


def test_parse_money_field_quantizes_to_cents():
    assert _parse_money_field("10") == Decimal("10.00")
    assert _parse_money_field("10.999") == Decimal("11.00")


# -- _valuables_query sorting --------------------------------------------------------


def test_valuables_query_sorts_by_purchase_price_ascending(session):
    make_asset(session, hostname="Cheap", purchase_price=Decimal("10.00"))
    make_asset(session, hostname="Pricey", purchase_price=Decimal("999.00"))

    rows = _valuables_query(session, show_all=True, sort="purchase_price", direction="asc")

    assert [r.hostname for r in rows] == ["Cheap", "Pricey"]


def test_valuables_query_sorts_by_purchase_price_descending(session):
    make_asset(session, hostname="Cheap", purchase_price=Decimal("10.00"))
    make_asset(session, hostname="Pricey", purchase_price=Decimal("999.00"))

    rows = _valuables_query(session, show_all=True, sort="purchase_price", direction="desc")

    assert [r.hostname for r in rows] == ["Pricey", "Cheap"]


def test_valuables_query_sorts_by_location_without_dropping_unassigned(session):
    """Location.name isn't a column on Asset -- sorting by it needs an
    outerjoin, and outer (not inner) so an asset with no location assigned
    still shows up rather than being silently dropped."""
    kitchen = Location(name="Kitchen")
    session.add(kitchen)
    session.commit()
    session.refresh(kitchen)
    make_asset(session, hostname="Placed", location_id=kitchen.id)
    make_asset(session, hostname="Unplaced", location_id=None)

    rows = _valuables_query(session, show_all=True, sort="location", direction="asc")

    assert {r.hostname for r in rows} == {"Placed", "Unplaced"}


def test_valuables_query_invalid_sort_key_falls_back_to_hostname(session):
    make_asset(session, hostname="Bravo", purchase_price=Decimal("1.00"))
    make_asset(session, hostname="Alpha", purchase_price=Decimal("2.00"))

    rows = _valuables_query(session, show_all=True, sort="not-a-real-column", direction="asc")

    assert [r.hostname for r in rows] == ["Alpha", "Bravo"]


def test_valuables_query_excludes_high_criticality_asset_marked_not_valuable(session):
    """A smart plug can legitimately be high criticality (attack-surface
    concern) while being worth nothing for insurance -- is_valuable=False
    overrides the criticality-high inclusion rule, not just supplements it."""
    make_asset(
        session, hostname="Smart plug", criticality=Criticality.high, is_valuable=False
    )

    assert _valuables_query(session, show_all=False) == []
    assert [r.hostname for r in _valuables_query(session, show_all=True)] == ["Smart plug"]


def test_valuables_query_excludes_priced_asset_marked_not_valuable(session):
    """is_valuable=False overrides real purchase data too, not just the
    criticality-high shortcut."""
    make_asset(
        session, hostname="Priced but excluded",
        purchase_price=Decimal("50.00"), is_valuable=False,
    )
    make_asset(session, hostname="Priced and included", purchase_price=Decimal("50.00"))

    rows = _valuables_query(session, show_all=False)

    assert [r.hostname for r in rows] == ["Priced and included"]


def test_valuables_query_default_is_valuable_still_shows_up(session):
    """The common case: an asset that never touched this field at all (the
    schema default) behaves exactly as it did before this field existed."""
    make_asset(session, hostname="Ordinary", purchase_price=Decimal("50.00"))

    rows = _valuables_query(session, show_all=False)

    assert [r.hostname for r in rows] == ["Ordinary"]


# -- _autofill_model_number (auto model-number guess on save) ----------------
# The LLM call (assistant.guess_model_number) and is_configured are monkeypatched,
# so these test only the orchestration/gating -- no real API call.
# guess_model_number is now called as (asset, session), hence the `s=None` on
# every stub below -- budget_block_reason() itself is exercised for real
# against the empty in-memory AiUsage/AppSetting tables (default budgets are
# well above $0 spent, and the kill switch defaults to on), except in the
# tests that specifically target it further down.

def _qualifying(session, **kw):
    d = dict(vendor="Amazon", model="Echo", serial_number="SER1",
             purchase_date=date(2019, 11, 22), model_number=None)
    d.update(kw)
    return make_asset(session, **d)


def _notes(session, asset_id):
    return session.exec(select(AssetNote).where(AssetNote.asset_id == asset_id)).all()


def test_autofill_model_number_fills_guess_with_unverified_marker(session, monkeypatch):
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.assistant.guess_model_number", lambda a, s=None: "Echo (3rd Gen)")
    asset = _qualifying(session)

    _autofill_model_number(session, asset)
    session.commit()

    assert asset.model_number == "Echo (3rd Gen) (unverified)"
    assert any("auto-guessed" in n.body for n in _notes(session, asset.id))


def test_autofill_model_number_writes_na_cleanly(session, monkeypatch):
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.assistant.guess_model_number", lambda a, s=None: "N/A")
    asset = _qualifying(session)

    _autofill_model_number(session, asset)

    assert asset.model_number == "N/A"  # no "(unverified)" on a definitive N/A


def test_autofill_model_number_noop_when_guess_none(session, monkeypatch):
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.assistant.guess_model_number", lambda a, s=None: None)
    asset = _qualifying(session)

    _autofill_model_number(session, asset)

    assert asset.model_number is None
    assert _notes(session, asset.id) == []


def test_autofill_model_number_skips_when_already_set(session, monkeypatch):
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.assistant.guess_model_number",
                        lambda a, s=None: (_ for _ in ()).throw(AssertionError("should not be called")))
    asset = _qualifying(session, model_number="EXISTING")

    _autofill_model_number(session, asset)

    assert asset.model_number == "EXISTING"


def test_autofill_model_number_skips_locked_identity(session, monkeypatch):
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.assistant.guess_model_number",
                        lambda a, s=None: (_ for _ in ()).throw(AssertionError("should not be called")))
    asset = _qualifying(session, identity_locked=True)

    _autofill_model_number(session, asset)

    assert asset.model_number is None


def test_autofill_model_number_skips_when_a_field_missing(session, monkeypatch):
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.assistant.guess_model_number",
                        lambda a, s=None: (_ for _ in ()).throw(AssertionError("should not be called")))
    asset = _qualifying(session, serial_number=None)  # not fully documented

    _autofill_model_number(session, asset)

    assert asset.model_number is None


def test_autofill_model_number_skips_when_not_configured(session, monkeypatch):
    monkeypatch.setattr("app.assistant.is_configured", lambda: False)
    monkeypatch.setattr("app.assistant.guess_model_number",
                        lambda a, s=None: (_ for _ in ()).throw(AssertionError("should not be called")))
    asset = _qualifying(session)

    _autofill_model_number(session, asset)

    assert asset.model_number is None
    # Not configured must NOT burn the one attempt -- configuring the API later
    # should still let the guess run.
    assert asset.model_number_guess_attempted_at is None


def test_autofill_model_number_skips_when_budget_blocked(session, monkeypatch):
    """Same "don't burn the attempt" rule as not-configured: a budget/kill-switch
    block is a temporary condition, so a later save should still get its guess
    once the block clears."""
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.assistant.budget_block_reason", lambda s: "Today's AI budget is used up.")
    monkeypatch.setattr("app.assistant.guess_model_number",
                        lambda a, s=None: (_ for _ in ()).throw(AssertionError("should not be called")))
    asset = _qualifying(session)

    _autofill_model_number(session, asset)

    assert asset.model_number is None
    assert asset.model_number_guess_attempted_at is None


def test_autofill_model_number_does_not_retry_after_a_miss(session, monkeypatch):
    """A guess that comes back unknown stamps the attempt timestamp, so a later
    save doesn't pay for another ~10s LLM call. This is the re-fire bug fix."""
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)
    calls = {"n": 0}

    def _guess(a, s=None):
        calls["n"] += 1
        return None

    monkeypatch.setattr("app.assistant.guess_model_number", _guess)
    asset = _qualifying(session)

    _autofill_model_number(session, asset)
    assert calls["n"] == 1
    assert asset.model_number is None
    assert asset.model_number_guess_attempted_at is not None

    _autofill_model_number(session, asset)  # a subsequent save of the same asset
    assert calls["n"] == 1  # not re-invoked


def test_autofill_model_number_stamps_attempt_on_success(session, monkeypatch):
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.assistant.guess_model_number", lambda a, s=None: "Echo (3rd Gen)")
    asset = _qualifying(session)

    _autofill_model_number(session, asset)

    assert asset.model_number_guess_attempted_at is not None


# -- _csv_safe ---------------------------------------------------------------


def test_csv_safe_neutralizes_leading_formula_characters():
    """hostname/vendor/model/model_number can be device-supplied (discovery/
    probes) and land in the Valuables CSV export, which is explicitly built
    for Excel -- any of these leading characters would otherwise be read as
    a formula by Excel/LibreOffice/Sheets on open."""
    assert _csv_safe("=cmd|'/C calc'!A0") == "'=cmd|'/C calc'!A0"
    assert _csv_safe("+1+1") == "'+1+1"
    assert _csv_safe("-1-1") == "'-1-1"
    assert _csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert _csv_safe("\t=evil") == "'\t=evil"


def test_csv_safe_leaves_ordinary_values_untouched():
    assert _csv_safe("Living Room Echo") == "Living Room Echo"
    assert _csv_safe("") == ""
