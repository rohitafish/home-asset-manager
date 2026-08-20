"""Covers app/assistant.py's non-network logic: the propose/apply path
(including the Tier-1.2 value coercion that stops an invalid proposal from
500ing at Apply time), the transcript view, and -- via a fake Anthropic
client -- the tool loop itself (tool_use -> tool_result -> text) with no
real API call.
"""

import json
import logging
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from conftest import make_asset
from sqlmodel import select

from app.assistant import (
    _append_message,
    _coerce_proposal_value,
    _log_usage,
    _period_spend,
    _replay,
    _request_safe_blocks,
    _tool_propose_set_field,
    apply_proposal,
    budget_block_reason,
    build_asset_context,
    chat_transcript,
    describe_proposal,
    describe_tool_call,
    guess_model_number,
    is_ai_assistant_enabled,
    run_chat_turn,
    set_ai_assistant_enabled,
    spend_summary,
)
from app.models import (
    AiUsage,
    Asset,
    AssetNote,
    ChangeProposal,
    ChatMessage,
    Criticality,
)

# -- _coerce_proposal_value --------------------------------------------------


def test_coerce_criticality_valid():
    value, error = _coerce_proposal_value("criticality", "high")
    assert error is None
    assert value == Criticality.high


def test_coerce_criticality_invalid():
    value, error = _coerce_proposal_value("criticality", "very high")
    assert value is None
    assert "not a valid criticality" in error


def test_coerce_lifecycle_status_invalid():
    value, error = _coerce_proposal_value("lifecycle_status", "zombie")
    assert value is None
    assert "not a valid lifecycle_status" in error


def test_coerce_is_internet_facing_truthy_and_falsy():
    for truthy in ("1", "true", "yes", "on", "TRUE"):
        value, error = _coerce_proposal_value("is_internet_facing", truthy)
        assert error is None
        assert value is True
    for falsy in ("0", "false", "no", "off"):
        value, error = _coerce_proposal_value("is_internet_facing", falsy)
        assert error is None
        assert value is False


def test_coerce_is_internet_facing_invalid():
    value, error = _coerce_proposal_value("is_internet_facing", "maybe")
    assert value is None
    assert "not a recognizable true/false value" in error


def test_coerce_free_text_field_passes_through():
    value, error = _coerce_proposal_value("hostname", "new-hostname")
    assert error is None
    assert value == "new-hostname"


def test_coerce_purchase_date_valid():
    value, error = _coerce_proposal_value("purchase_date", "2021-06-21")
    assert error is None
    assert value == date(2021, 6, 21)


def test_coerce_purchase_date_invalid():
    value, error = _coerce_proposal_value("purchase_date", "21 June 2021")
    assert value is None
    assert "not a valid ISO date" in error


def test_coerce_warranty_expiry_valid():
    value, error = _coerce_proposal_value("warranty_expiry", "2035-07-28")
    assert error is None
    assert value == date(2035, 7, 28)


def test_coerce_purchase_price_plain():
    value, error = _coerce_proposal_value("purchase_price", "249.99")
    assert error is None
    assert value == Decimal("249.99")


def test_coerce_purchase_price_currency_prefixed_and_thousands():
    value, error = _coerce_proposal_value("replacement_value", "£1,234.5")
    assert error is None
    assert value == Decimal("1234.50")


def test_coerce_purchase_price_invalid():
    value, error = _coerce_proposal_value("purchase_price", "a lot")
    assert value is None
    assert "not a valid amount" in error


# -- build_asset_context --------------------------------------------------------


def test_build_asset_context_includes_identity_and_valuation_fields(session):
    """Regression test: build_asset_context (via _asset_dict) is the only
    way the assistant sees an asset's current values, whether from the
    system prompt or a get_asset tool call -- a field missing here means
    Claude can never tell the user it's already set, discovered live when
    a real upload test had Claude claim a populated serial_number was
    empty because _asset_dict never included it at all."""
    asset = make_asset(
        session,
        serial_number="TG12508600036X",
        model_number="S17",
        model_identifier="1707000-xx-y",
        purchase_date=date(2025, 7, 28),
        purchase_price=Decimal("7794.00"),
        replacement_value=Decimal("8000.00"),
        warranty_expiry=date(2035, 7, 28),
    )

    context = build_asset_context(session, asset)

    assert context["serial_number"] == "TG12508600036X"
    assert context["model_number"] == "S17"
    assert context["model_identifier"] == "1707000-xx-y"
    assert context["purchase_date"] == "2025-07-28"
    assert context["purchase_price"] == "7794.00"
    assert context["replacement_value"] == "8000.00"
    assert context["warranty_expiry"] == "2035-07-28"
    # And it must round-trip through plain json.dumps with no `default=`
    # handler -- _execute_tool's get_asset path does exactly that, so a raw
    # Decimal/date here would crash every get_asset tool call, not just
    # look wrong.
    json.dumps(context)


def test_build_asset_context_unset_fields_are_none(session):
    asset = make_asset(session)

    context = build_asset_context(session, asset)

    assert context["serial_number"] is None
    assert context["purchase_date"] is None
    assert context["purchase_price"] is None


# -- describe_proposal --------------------------------------------------------


def test_describe_proposal_set_field():
    proposal = ChangeProposal(asset_id=1, kind="set_field", payload_json='{"field_name": "criticality", "value": "high"}')
    assert describe_proposal(proposal) == 'Set criticality to "high"'


def test_describe_proposal_add_note():
    proposal = ChangeProposal(asset_id=1, kind="add_note", payload_json='{"body": "looks fine"}')
    assert describe_proposal(proposal) == 'Add note: "looks fine"'


def test_describe_proposal_set_location_with_position():
    proposal = ChangeProposal(
        asset_id=1, kind="set_location",
        payload_json='{"location_name": "Kitchen", "position": "on the counter"}',
    )
    assert describe_proposal(proposal) == 'Set location to "Kitchen", position "on the counter"'


def test_describe_proposal_link_same_device():
    proposal = ChangeProposal(
        asset_id=1, kind="link_same_device",
        payload_json='{"asset_id_a": 1, "asset_id_b": 2, "detail": "same box"}',
    )
    assert describe_proposal(proposal) == "Link asset #1 and #2 as the same device (same box)"


# -- describe_tool_call -------------------------------------------------------


def test_describe_tool_call_set_field_names_the_field():
    out = describe_tool_call(
        "propose_set_field", {"field_name": "purchase_price", "value": "612.50"}
    )
    assert out == 'Proposed: set purchase_price to "612.50"'


def test_describe_tool_call_add_note_previews_body():
    out = describe_tool_call("propose_add_note", {"body": "Invoice from PC World"})
    assert out == 'Proposed note: "Invoice from PC World"'


def test_describe_tool_call_add_note_truncates_long_body():
    body = "x" * 200
    out = describe_tool_call("propose_add_note", {"body": body})
    assert out.startswith('Proposed note: "') and out.endswith('…"')
    assert len(out) < len(body)


def test_describe_tool_call_set_location_with_position():
    out = describe_tool_call(
        "propose_set_location", {"location_name": "Living Room", "position": "by the TV"}
    )
    assert out == 'Proposed location: "Living Room", position "by the TV"'


def test_describe_tool_call_link_same_device():
    out = describe_tool_call(
        "propose_link_same_device", {"asset_id_a": 1, "asset_id_b": 2}
    )
    assert out == "Proposed linking asset #1 and #2 as the same device"


def test_describe_tool_call_read_tools():
    assert describe_tool_call("search_assets", {"query": "sonos"}) == 'Searched the inventory for "sonos"'
    assert describe_tool_call("get_asset", {"asset_id": 12}) == "Looked up asset #12"
    assert describe_tool_call("run_probe", {"asset_id": 7}) == "Ran identification probes on asset #7"


def test_describe_tool_call_missing_input_falls_back_generically():
    # A tool_use block whose input didn't round-trip must not crash the render.
    assert describe_tool_call("propose_set_field", {}) == "Proposed a field change"
    assert describe_tool_call("propose_add_note", {}) == "Proposed a note"
    assert describe_tool_call("propose_set_location", {}) == "Proposed a location"
    assert describe_tool_call("propose_link_same_device", {}) == "Proposed linking two assets"
    assert describe_tool_call("get_asset", {}) == "Looked up an asset"


def test_describe_tool_call_unknown_tool_returns_its_name():
    assert describe_tool_call("some_future_tool", {"x": 1}) == "some_future_tool"


def test_describe_tool_call_notes_a_different_target_asset():
    """A multi-asset invoice proposes against other assets; the transcript line
    must name the target when it isn't the current asset, or the user sees a
    'Proposed…' entry with no matching card."""
    ti = {"asset_id": 7, "field_name": "vendor", "value": "Ubiquiti"}
    same = describe_tool_call("propose_set_field", {**ti, "asset_id": 5}, current_asset_id=5)
    other = describe_tool_call("propose_set_field", ti, current_asset_id=5)
    assert "(asset #" not in same
    assert other.endswith("(asset #7)")


# -- cross-asset proposals (multi-asset invoice) -----------------------------


def test_propose_set_field_records_origin_and_target(session):
    """A proposal made from asset A's chat against asset B stores B as the
    target and A as the origin, so A's panel can surface it."""
    origin = make_asset(session)   # the chat's asset
    target = make_asset(session)   # a different asset the invoice also covers

    result = _tool_propose_set_field(
        session,
        {"asset_id": target.id, "field_name": "vendor", "value": "Ubiquiti", "reason": "invoice"},
        origin.id,
    )

    assert "error" not in result
    proposal = session.get(ChangeProposal, result["proposal_id"])
    assert proposal.asset_id == target.id
    assert proposal.origin_asset_id == origin.id


def test_propose_set_field_same_asset_sets_origin_equal_to_target(session):
    asset = make_asset(session)
    result = _tool_propose_set_field(
        session, {"asset_id": asset.id, "field_name": "vendor", "value": "X", "reason": "r"}, asset.id
    )
    proposal = session.get(ChangeProposal, result["proposal_id"])
    assert proposal.origin_asset_id == asset.id == proposal.asset_id


# -- apply_proposal ------------------------------------------------------------


def test_apply_proposal_set_field_valid(session):
    asset = make_asset(session, criticality=Criticality.medium)
    proposal = ChangeProposal(
        asset_id=asset.id, kind="set_field",
        payload_json='{"field_name": "criticality", "value": "high"}',
    )
    session.add(proposal)
    session.commit()

    apply_proposal(session, proposal)
    session.commit()

    session.refresh(asset)
    assert asset.criticality == Criticality.high
    assert proposal.status == "applied"
    assert proposal.applied_at is not None
    notes = session.exec(select(AssetNote).where(AssetNote.asset_id == asset.id)).all()
    assert len(notes) == 1
    assert "Applied" in notes[0].body


def test_apply_proposal_set_field_purchase_date_and_price(session):
    asset = make_asset(session)
    date_proposal = ChangeProposal(
        asset_id=asset.id, kind="set_field",
        payload_json='{"field_name": "purchase_date", "value": "2021-06-21"}',
    )
    price_proposal = ChangeProposal(
        asset_id=asset.id, kind="set_field",
        payload_json='{"field_name": "purchase_price", "value": "249.99"}',
    )
    session.add(date_proposal)
    session.add(price_proposal)
    session.commit()

    apply_proposal(session, date_proposal)
    apply_proposal(session, price_proposal)
    session.commit()

    session.refresh(asset)
    assert asset.purchase_date == date(2021, 6, 21)
    assert asset.purchase_price == Decimal("249.99")


def test_apply_proposal_set_field_invalid_value_discards(session):
    """Defence in depth: a proposal that reached the table with an invalid
    value (should already be rejected at propose time) must be discarded,
    not left to 500 the flush."""
    asset = make_asset(session, criticality=Criticality.medium)
    proposal = ChangeProposal(
        asset_id=asset.id, kind="set_field",
        payload_json='{"field_name": "criticality", "value": "extremely high"}',
    )
    session.add(proposal)
    session.commit()

    apply_proposal(session, proposal)
    session.commit()

    session.refresh(asset)
    assert asset.criticality == Criticality.medium
    assert proposal.status == "discarded"
    notes = session.exec(select(AssetNote).where(AssetNote.asset_id == asset.id)).all()
    assert len(notes) == 1
    assert "Could not apply" in notes[0].body


def test_apply_proposal_add_note(session):
    asset = make_asset(session)
    proposal = ChangeProposal(asset_id=asset.id, kind="add_note", payload_json='{"body": "hello"}')
    session.add(proposal)
    session.commit()

    apply_proposal(session, proposal)
    session.commit()

    notes = session.exec(select(AssetNote).where(AssetNote.asset_id == asset.id)).all()
    assert [n.body for n in notes] == ["hello"]
    assert proposal.status == "applied"


def test_apply_proposal_set_location_reuses_existing_location_case_insensitively(session):
    from app.models import Location

    asset = make_asset(session)
    session.add(Location(name="Kitchen"))
    session.commit()

    proposal = ChangeProposal(
        asset_id=asset.id, kind="set_location",
        payload_json='{"location_name": "kitchen", "position": null}',
    )
    session.add(proposal)
    session.commit()

    apply_proposal(session, proposal)
    session.commit()

    locations = session.exec(select(Location)).all()
    assert len(locations) == 1  # no duplicate "kitchen" row created
    session.refresh(asset)
    assert asset.location_id == locations[0].id


def test_apply_proposal_link_same_device(session):
    a = make_asset(session)
    b = make_asset(session)
    proposal = ChangeProposal(
        asset_id=a.id, kind="link_same_device",
        payload_json=f'{{"asset_id_a": {a.id}, "asset_id_b": {b.id}, "detail": null}}',
    )
    session.add(proposal)
    session.commit()

    apply_proposal(session, proposal)
    session.commit()

    from app.models import CIRelationship

    rels = session.exec(select(CIRelationship)).all()
    assert len(rels) == 2
    notes = session.exec(select(AssetNote)).all()
    assert len(notes) == 2


def test_apply_proposal_skips_non_pending(session):
    asset = make_asset(session, criticality=Criticality.medium)
    proposal = ChangeProposal(
        asset_id=asset.id, kind="set_field",
        payload_json='{"field_name": "criticality", "value": "high"}',
        status="applied",
    )
    session.add(proposal)
    session.commit()

    apply_proposal(session, proposal)
    session.commit()

    session.refresh(asset)
    assert asset.criticality == Criticality.medium  # unchanged -- already applied


# -- chat_transcript -----------------------------------------------------------


def test_chat_transcript_summarizes_tool_use_and_skips_pure_tool_results(session):
    asset = make_asset(session)
    session.add(ChatMessage(asset_id=asset.id, role="user", content_json='[{"type": "text", "text": "hi"}]'))
    session.add(ChatMessage(
        asset_id=asset.id, role="assistant",
        content_json='[{"type": "tool_use", "id": "t1", "name": "search_assets", "input": {}}]',
    ))
    # A tool_result-only follow-up -- plumbing, not a real user turn.
    session.add(ChatMessage(
        asset_id=asset.id, role="user",
        content_json='[{"type": "tool_result", "tool_use_id": "t1", "content": "[]"}]',
    ))
    session.add(ChatMessage(asset_id=asset.id, role="assistant", content_json='[{"type": "text", "text": "done"}]'))
    session.commit()

    view = chat_transcript(session, asset.id)

    assert [entry["role"] for entry in view] == ["user", "assistant", "assistant"]
    assert view[0]["text"] == "hi"
    assert view[1]["tool_calls"] == ["Searched the inventory"]  # empty input -> generic
    assert view[1]["text"] == ""
    assert view[2]["text"] == "done"


# -- _replay -------------------------------------------------------------------


def test_replay_counts_image_attachment_turn_as_a_turn_start(session):
    """A real user turn carrying an image (or document) block alongside text
    must still be recognized as a turn boundary -- only a tool_result
    follow-up should be excluded. Regression test for the fix from
    "every block is text" to "no block is tool_result"."""
    asset = make_asset(session)
    session.add(ChatMessage(
        asset_id=asset.id, role="user",
        content_json=json.dumps([
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "Zm9v"}},
            {"type": "text", "text": "[Attached 1 file(s): receipt.jpg.]\n\nwhat's the serial?"},
        ]),
    ))
    session.add(ChatMessage(asset_id=asset.id, role="assistant", content_json='[{"type": "text", "text": "TG123"}]'))
    session.commit()

    messages = _replay(session, asset.id)

    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert any(b["type"] == "image" for b in messages[0]["content"])


def test_replay_strips_attachment_from_an_earlier_turn_but_not_the_newest(session):
    """The single biggest cost multiplier this feature had: an attachment used
    to be re-billed on every replay of every later turn for the rest of the
    MAX_REPLAY_TURNS window. Only the newest turn (the one this _replay call is
    for) should still carry its image/document bytes -- an older turn's
    attachment is replaced with a text placeholder."""
    asset = make_asset(session)
    session.add(ChatMessage(
        asset_id=asset.id, role="user",
        content_json=json.dumps([
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "Zm9v"}},
            {"type": "text", "text": "[Attached 1 file(s): receipt.jpg.]\n\nwhat's the serial?"},
        ]),
    ))
    session.add(ChatMessage(asset_id=asset.id, role="assistant", content_json='[{"type": "text", "text": "TG123"}]'))
    session.add(ChatMessage(
        asset_id=asset.id, role="user",
        content_json=json.dumps([
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "Zm9v"}, "title": "warranty.pdf"},
            {"type": "text", "text": "[Attached 1 file(s): warranty.pdf.]\n\nand the warranty end date?"},
        ]),
    ))
    session.commit()

    messages = _replay(session, asset.id)

    assert len(messages) == 3
    older_turn = messages[0]["content"]
    assert not any(b["type"] in ("image", "document") for b in older_turn)
    placeholder = next(b for b in older_turn if b["type"] == "text" and "earlier turn" in b["text"])
    assert "receipt.jpg" not in placeholder["text"]  # image blocks carry no filename to preserve

    newest_turn = messages[2]["content"]
    doc_block = next(b for b in newest_turn if b["type"] == "document")
    assert doc_block["title"] == "warranty.pdf"
    assert doc_block["source"]["data"] == "Zm9v"  # bytes intact for the turn that uploaded them


# -- run_chat_turn tool loop, via a fake Anthropic client ---------------------


class _FakeContentBlock:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self):
        return dict(self.__dict__)


class _FakeMessage:
    def __init__(self, content, stop_reason="end_turn", stop_details=None):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details


def _make_fake_anthropic_class(responses, captured=None):
    """responses is consumed in order, one per client.beta.messages.stream()
    call -- one call per tool-loop iteration in run_chat_turn. If `captured` is
    a list, each call's kwargs are appended to it (used to assert what was sent)."""

    class _FakeStreamCM:
        def __init__(self, message):
            self._message = message

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_final_message(self):
            return self._message

    class _FakeMessagesAPI:
        def stream(self, **kwargs):
            if captured is not None:
                captured.append(kwargs)
            return _FakeStreamCM(responses.pop(0))

    class _FakeBeta:
        def __init__(self):
            self.messages = _FakeMessagesAPI()

    class _FakeAnthropicClient:
        def __init__(self, *args, **kwargs):
            self.beta = _FakeBeta()

    return _FakeAnthropicClient


def test_run_chat_turn_tool_loop_with_fake_client(session, monkeypatch):
    import anthropic

    asset = make_asset(session, hostname="probe-target")

    responses = [
        _FakeMessage(
            content=[_FakeContentBlock(
                type="tool_use", id="tu_1", name="search_assets", input={"query": "probe"},
            )],
            stop_reason="tool_use",
        ),
        _FakeMessage(
            content=[_FakeContentBlock(type="text", text="Found the asset.")],
            stop_reason="end_turn",
        ),
    ]
    monkeypatch.setattr(anthropic, "Anthropic", _make_fake_anthropic_class(responses))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = run_chat_turn(session, asset, "find the probe asset")

    assert result is None

    rows = session.exec(
        select(ChatMessage).where(ChatMessage.asset_id == asset.id).order_by(ChatMessage.created_at)
    ).all()
    assert [r.role for r in rows] == ["user", "assistant", "user", "assistant"]

    tool_result_row = rows[2]
    assert '"is_error": false' in tool_result_row.content_json.replace(" ", "") or \
        '"is_error":false' in tool_result_row.content_json.replace(" ", "")
    assert "<untrusted_device_data>" in tool_result_row.content_json

    final_row = rows[3]
    assert "Found the asset." in final_row.content_json


def test_run_chat_turn_with_attachment_persists_image_block_and_note(session, monkeypatch):
    import anthropic

    asset = make_asset(session, hostname="powerwall")

    responses = [
        _FakeMessage(
            content=[_FakeContentBlock(type="text", text="Serial looks like TG12508600036X.")],
            stop_reason="end_turn",
        ),
    ]
    monkeypatch.setattr(anthropic, "Anthropic", _make_fake_anthropic_class(responses))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = run_chat_turn(
        session, asset, "what's the serial?",
        attachments=[("receipt.jpg", "image/jpeg", b"fake-jpeg-bytes")],
    )

    assert result is None

    rows = session.exec(
        select(ChatMessage).where(ChatMessage.asset_id == asset.id).order_by(ChatMessage.created_at)
    ).all()
    assert [r.role for r in rows] == ["user", "assistant"]

    user_blocks = json.loads(rows[0].content_json)
    assert user_blocks[0]["type"] == "image"
    assert user_blocks[0]["source"]["media_type"] == "image/jpeg"
    text_block = next(b for b in user_blocks if b["type"] == "text")
    assert "receipt.jpg" in text_block["text"]
    assert "what's the serial?" in text_block["text"]

    # chat_transcript needs no attachment-specific handling -- the note is
    # embedded in the text block, so the existing text extraction surfaces it.
    view = chat_transcript(session, asset.id)
    assert "receipt.jpg" in view[0]["text"]


def test_run_chat_turn_not_configured_short_circuits(session, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    asset = make_asset(session)

    result = run_chat_turn(session, asset, "hello")

    assert result is not None
    assert "not configured" in result


# -- replaying stored turns as request input ---------------------------------


def test_request_safe_blocks_strips_only_the_rejected_field():
    """block.model_dump() keeps response-only fields, and replaying a stored
    assistant turn re-sends them. Verified against the live API: of the
    response-only keys that actually appear in stored rows, only
    parsed_output is rejected -- so citations and caller are deliberately
    left in place rather than stripped defensively, and thinking.signature
    MUST survive (thinking blocks have to replay byte-identical).
    """
    cleaned = _request_safe_blocks([
        {"type": "text", "text": "hi", "parsed_output": None, "citations": []},
        {"type": "tool_use", "id": "tu_1", "name": "t", "input": {}, "caller": {"type": "direct"}},
        {"type": "thinking", "thinking": "...", "signature": "CAIS-sig"},
    ])

    text, tool_use, thinking = cleaned
    assert "parsed_output" not in text
    assert text == {"type": "text", "text": "hi", "citations": []}
    assert tool_use["caller"] == {"type": "direct"}
    assert thinking["signature"] == "CAIS-sig"


def test_replay_sanitizes_rows_written_by_earlier_versions(session):
    """Read-side stripping is what makes already-poisoned rows usable again --
    without it, every existing conversation 400s forever and would need a
    data migration to recover.
    """
    asset = make_asset(session)
    _append_message(session, asset.id, "user", [{"type": "text", "text": "q"}])
    _append_message(session, asset.id, "assistant", [
        {"type": "text", "text": "a", "parsed_output": None},
    ])
    session.commit()

    replayed = _replay(session, asset.id)

    assistant_blocks = replayed[1]["content"]
    assert all("parsed_output" not in b for b in assistant_blocks)


# -- token usage logging -----------------------------------------------------


def test_log_usage_distinguishes_unreported_cache_fields_from_zero(caplog):
    """A provider that doesn't report the cache fields must read 'n/a', not
    '0'. Collapsing the two (e.g. `getattr(...) or 0`) would make "this
    endpoint has no cache support" indistinguishable from "the cache missed",
    which are the two diagnoses this log line exists to tell apart.

    Uses caplog, not capsys: _log_usage now logs via logging (see
    app/logging_config.py), and a StreamHandler binds its stream at
    construction, so pytest's capsys stdout replacement wouldn't see it.
    """
    with caplog.at_level(logging.INFO, logger="app.assistant"):
        _log_usage(1, 0, False, SimpleNamespace(
            input_tokens=10, output_tokens=20,
            cache_read_input_tokens=0, cache_creation_input_tokens=999,
        ))
    reported_zero = caplog.records[-1].getMessage()
    assert "cache_read=0" in reported_zero
    assert "cache_write=999" in reported_zero
    assert "via=anthropic" in reported_zero

    # Same call shape, but the provider omitted the cache fields entirely.
    with caplog.at_level(logging.INFO, logger="app.assistant"):
        _log_usage(1, 0, True, SimpleNamespace(input_tokens=10, output_tokens=20))
    unreported = caplog.records[-1].getMessage()
    assert "cache_read=n/a" in unreported
    assert "cache_write=n/a" in unreported
    assert "via=openrouter" in unreported


def test_log_usage_survives_missing_usage_object(caplog):
    """Must not raise -- it runs before the stop_reason branches, so a
    malformed or usage-less response would otherwise take down the whole turn
    on a purely diagnostic line.
    """
    with caplog.at_level(logging.INFO, logger="app.assistant"):
        _log_usage(7, 3, True, None)

    out = caplog.records[-1].getMessage()
    assert "asset=7 iter=3" in out
    assert "in=n/a" in out


# -- _log_usage persisting to the AiUsage ledger ------------------------------


def test_log_usage_persists_ai_usage_row_with_computed_cost(session):
    asset = make_asset(session)
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=500,
        cache_read_input_tokens=200, cache_creation_input_tokens=100,
    )

    _log_usage(
        asset.id, 2, False, usage,
        session=session, call_site="chat", model="claude-opus-5", stop_reason="end_turn",
    )

    rows = session.exec(select(AiUsage).where(AiUsage.asset_id == asset.id)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.call_site == "chat"
    assert row.provider == "anthropic"
    assert row.model == "claude-opus-5"
    assert row.iteration == 2
    assert row.stop_reason == "end_turn"
    assert row.input_tokens == 1000 and row.output_tokens == 500
    # (1000*5 + 500*25 + 200*5*0.1 + 100*5*1.25) / 1_000_000
    # = (5000 + 12500 + 100 + 625) / 1_000_000
    assert row.cost_usd == Decimal("0.018225")


def test_log_usage_unknown_model_persists_null_cost(session):
    """An unpriced model (an Anthropic model not in the rate table, or --
    much more commonly -- an OpenRouter call) must not silently record cost
    0; it needs to stay NULL so budget_block_reason's SUM() treats it as
    unknown, not as free."""
    asset = make_asset(session)
    usage = SimpleNamespace(input_tokens=100, output_tokens=50)

    _log_usage(
        asset.id, 0, True, usage,
        session=session, call_site="chat", model="some-openrouter-model", stop_reason="end_turn",
    )

    row = session.exec(select(AiUsage).where(AiUsage.asset_id == asset.id)).one()
    assert row.cost_usd is None
    assert row.provider == "openrouter"


def test_log_usage_without_session_does_not_persist(session):
    """session is optional -- omitting it (as every pre-existing caller/test
    does) must log without touching the database."""
    asset = make_asset(session)

    _log_usage(asset.id, 0, False, SimpleNamespace(input_tokens=1, output_tokens=1))

    assert session.exec(select(AiUsage)).all() == []


def test_log_usage_db_failure_does_not_raise(session, monkeypatch):
    """The persistence step must not be able to take down a chat turn -- same
    must-not-raise invariant as the log line itself."""
    def _boom(*a, **kw):
        raise RuntimeError("db is down")

    monkeypatch.setattr(session, "commit", _boom)

    _log_usage(  # must not raise
        1, 0, False, SimpleNamespace(input_tokens=1, output_tokens=1),
        session=session, call_site="chat", model="claude-opus-5",
    )


def test_log_usage_rolls_back_a_failed_commit_so_the_session_stays_usable(session):
    """Regression test: unlike the monkeypatched RuntimeError above (which
    never touches the DB, so it can't leave SQLAlchemy in a "pending
    rollback" state), this forces a REAL failed commit -- asset_id 999999
    doesn't exist, violating AiUsage's FK (conftest.py's engine enables
    PRAGMA foreign_keys=ON). Without session.rollback() in _log_usage's
    except block, the session is left pending-rollback, and the caller's
    OWN commit right after this function returns -- the actual chat turn's
    messages, in the real code path -- would raise PendingRollbackError
    instead of succeeding: exactly the "takes down the whole turn" outcome
    this function's docstring says a transient ledger-write failure must
    not cause."""
    _log_usage(  # must not raise
        999999, 0, False, SimpleNamespace(input_tokens=1, output_tokens=1),
        session=session, call_site="chat", model="claude-opus-5",
    )

    # The session must still be usable -- an unrelated commit right after
    # must succeed, not raise PendingRollbackError.
    asset = make_asset(session, hostname="after-the-failed-log-usage")
    session.commit()  # must not raise
    assert session.get(Asset, asset.id) is not None


# -- budget_block_reason / spend_summary / the kill switch -------------------


def _usage_row(session, *, cost, created_at=None, call_site="chat"):
    from app.clock import utcnow_naive
    row = AiUsage(
        asset_id=None, call_site=call_site, provider="anthropic", model="claude-opus-5",
        input_tokens=0, output_tokens=0, cost_usd=cost,
        created_at=created_at or utcnow_naive(),
    )
    session.add(row)
    session.commit()
    return row


def test_budget_block_reason_none_when_under_both_budgets(session, monkeypatch):
    import app.assistant as assistant_module
    monkeypatch.setattr(assistant_module, "AI_DAILY_BUDGET_USD", Decimal("5.00"))
    monkeypatch.setattr(assistant_module, "AI_MONTHLY_BUDGET_USD", Decimal("50.00"))

    assert budget_block_reason(session) is None


def test_budget_block_reason_blocks_at_daily_limit(session, monkeypatch):
    import app.assistant as assistant_module
    monkeypatch.setattr(assistant_module, "AI_DAILY_BUDGET_USD", Decimal("1.00"))
    monkeypatch.setattr(assistant_module, "AI_MONTHLY_BUDGET_USD", Decimal("50.00"))
    _usage_row(session, cost=Decimal("1.00"))  # exactly at the limit -- >= blocks

    reason = budget_block_reason(session)

    assert reason is not None
    assert "Today's AI budget" in reason


def test_budget_block_reason_blocks_at_monthly_limit(session, monkeypatch):
    import app.assistant as assistant_module
    monkeypatch.setattr(assistant_module, "AI_DAILY_BUDGET_USD", Decimal("100.00"))  # day is fine
    monkeypatch.setattr(assistant_module, "AI_MONTHLY_BUDGET_USD", Decimal("2.00"))
    _usage_row(session, cost=Decimal("2.00"))

    reason = budget_block_reason(session)

    assert reason is not None
    assert "month's AI budget" in reason


def test_budget_block_reason_ignores_spend_outside_the_window(session, monkeypatch):
    from datetime import timedelta

    import app.assistant as assistant_module
    from app.clock import utcnow_naive
    monkeypatch.setattr(assistant_module, "AI_DAILY_BUDGET_USD", Decimal("1.00"))
    monkeypatch.setattr(assistant_module, "AI_MONTHLY_BUDGET_USD", Decimal("50.00"))
    _usage_row(session, cost=Decimal("1.00"), created_at=utcnow_naive() - timedelta(days=2))

    assert budget_block_reason(session) is None  # yesterday's spend doesn't count against today


def test_budget_block_reason_null_cost_row_not_treated_as_zero(session):
    """A NULL-cost row (unpriced model) must not silently sum as $0 -- it's
    simply excluded, per app/ai_pricing.py's contract, so it can never by
    itself clear a budget check that a priced call would have failed."""
    from datetime import datetime as _dt

    _usage_row(session, cost=None)
    _usage_row(session, cost=Decimal("1.00"))

    total, has_unknown = _period_spend(session, _dt(2000, 1, 1))

    assert total == Decimal("1.00")  # the NULL row contributes nothing to the sum
    assert has_unknown is True


def test_budget_block_reason_off_when_kill_switch_disabled(session):
    set_ai_assistant_enabled(session, False)

    reason = budget_block_reason(session)

    assert reason == "The investigation assistant is turned off."


def test_kill_switch_defaults_to_enabled(session):
    assert is_ai_assistant_enabled(session) is True


def test_kill_switch_round_trips(session):
    set_ai_assistant_enabled(session, False)
    assert is_ai_assistant_enabled(session) is False

    set_ai_assistant_enabled(session, True)
    assert is_ai_assistant_enabled(session) is True


def test_spend_summary_reflects_the_same_totals_budget_block_reason_uses(session):
    _usage_row(session, cost=Decimal("2.50"))

    summary = spend_summary(session)

    assert summary["day_spend"] == Decimal("2.50")
    assert summary["month_spend"] == Decimal("2.50")
    assert summary["enabled"] is True


# -- run_chat_turn budget enforcement -----------------------------------------


def test_run_chat_turn_blocked_by_budget_never_calls_the_api(session, monkeypatch):
    import app.assistant as assistant_module
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(assistant_module, "budget_block_reason", lambda s: "Today's AI budget is used up.")
    asset = make_asset(session)

    result = run_chat_turn(session, asset, "hello")

    assert result == "Today's AI budget is used up."
    # Blocked before anything was persisted -- same placement as is_configured().
    assert session.exec(select(ChatMessage).where(ChatMessage.asset_id == asset.id)).all() == []


def test_run_chat_turn_stops_mid_loop_when_budget_crosses(session, monkeypatch):
    """A budget crossed between tool-loop iterations (e.g. by a concurrent
    request) must stop the loop before the next API call, leaving the
    transcript on the same resendable state as the MAX_TOOL_ITERATIONS
    exhaustion path -- not mid-response."""
    import anthropic

    import app.assistant as assistant_module

    asset = make_asset(session, hostname="probe-target")
    responses = [
        _FakeMessage(
            content=[_FakeContentBlock(
                type="tool_use", id="tu_1", name="search_assets", input={"query": "probe"},
            )],
            stop_reason="tool_use",
        ),
        # A second response is queued but must never be consumed.
        _FakeMessage(content=[_FakeContentBlock(type="text", text="should not be reached")]),
    ]
    monkeypatch.setattr(anthropic, "Anthropic", _make_fake_anthropic_class(responses))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Allowed for the turn-start check and iteration 0's check (so the first
    # tool_use response IS consumed) -- blocked from iteration 1 onward, so
    # the queued second response must never be reached.
    calls = {"n": 0}

    def _reason(s):
        calls["n"] += 1
        return None if calls["n"] <= 2 else "Today's AI budget is used up."

    monkeypatch.setattr(assistant_module, "budget_block_reason", _reason)

    result = run_chat_turn(session, asset, "find the probe asset")

    assert result is not None and "budget" in result
    assert len(responses) == 1  # the second queued response was never consumed


# -- guess_model_number (auto model-number best guess) -----------------------


def _guess_asset(**kw):
    d = dict(vendor="Apple", model="HomePod mini", purchase_date=date(2021, 1, 1),
             serial_number="SER123")
    d.update(kw)
    return SimpleNamespace(**d)


def _fake_anthropic_text(text):
    return _make_fake_anthropic_class(
        [_FakeMessage(content=[_FakeContentBlock(type="text", text=text)])]
    )


def test_guess_model_number_returns_parsed_value(monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", _fake_anthropic_text("A2374"))

    assert guess_model_number(_guess_asset()) == "A2374"


def test_guess_model_number_maps_na(monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", _fake_anthropic_text("N/A"))

    assert guess_model_number(_guess_asset()) == "N/A"


def test_guess_model_number_unknown_is_none(monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", _fake_anthropic_text("UNKNOWN"))

    assert guess_model_number(_guess_asset()) is None


def test_guess_model_number_none_when_not_configured(monkeypatch):
    """No key -> returns None without ever constructing a client / calling out."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert guess_model_number(_guess_asset()) is None


def test_guess_model_number_does_not_send_the_serial(monkeypatch):
    """The serial gates the feature but must never be transmitted to the API."""
    import json

    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    captured = []
    monkeypatch.setattr(
        anthropic, "Anthropic",
        _make_fake_anthropic_class(
            [_FakeMessage(content=[_FakeContentBlock(type="text", text="A2374")])], captured
        ),
    )

    guess_model_number(_guess_asset(serial_number="SECRETSERIAL999"))

    assert captured, "expected the API to be called"
    assert "SECRETSERIAL999" not in json.dumps(captured[0])
