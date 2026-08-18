"""Tests for app/ai_pricing.py's cost_usd() -- the USD cost calculation
behind the AiUsage ledger (see app/assistant.py's _log_usage) and the
budget enforcement that reads it (budget_block_reason)."""

import logging
from decimal import Decimal

from app.ai_pricing import cost_usd


def test_cost_usd_known_model_input_and_output_only():
    # 1000 input tokens @ $5/1M + 500 output tokens @ $25/1M
    # = 0.005 + 0.0125 = 0.0175
    assert cost_usd("claude-opus-5", 1000, 500) == Decimal("0.0175")


def test_cost_usd_includes_cache_read_and_write_tiers():
    # input 1000 @ $5/1M = 0.005
    # output 500 @ $25/1M = 0.0125
    # cache_read 200 @ $5/1M * 0.1 = 0.0001
    # cache_write 100 @ $5/1M * 1.25 = 0.000625
    # total = 0.018225
    got = cost_usd("claude-opus-5", 1000, 500, cache_read_tokens=200, cache_write_tokens=100)
    assert got == Decimal("0.018225")


def test_cost_usd_different_rate_per_model():
    opus = cost_usd("claude-opus-5", 1_000_000, 0)
    sonnet = cost_usd("claude-sonnet-5", 1_000_000, 0)
    haiku = cost_usd("claude-haiku-4-5", 1_000_000, 0)
    assert opus == Decimal("5.00")
    assert sonnet == Decimal("3.00")
    assert haiku == Decimal("1.00")


def test_cost_usd_missing_token_counts_treated_as_zero():
    assert cost_usd("claude-opus-5", None, None) == Decimal(0)
    assert cost_usd("claude-opus-5", 1000, None) == Decimal("0.005")


def test_cost_usd_unknown_model_returns_none_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="app.ai_pricing"):
        result = cost_usd("some-openrouter-model", 1000, 500)

    assert result is None
    assert any("no rate table entry" in r.getMessage() for r in caplog.records)


def test_cost_usd_none_model_returns_none_without_warning(caplog):
    """A None model (e.g. a fake/malformed response with no .model attribute)
    is a different case from an unrecognized-but-present model name -- no
    model name to log, so no warning is logged either."""
    with caplog.at_level(logging.WARNING, logger="app.ai_pricing"):
        result = cost_usd(None, 1000, 500)

    assert result is None
    assert caplog.records == []


def test_cost_usd_never_guesses_a_price_for_an_unknown_model():
    """The whole point of returning None instead of a number: nothing here
    should ever fall back to some other model's rate."""
    assert cost_usd("claude-opus-4-9-hypothetical-future-model", 1_000_000, 1_000_000) is None
