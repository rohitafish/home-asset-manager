"""USD cost of one Claude API call, from token counts and the model actually
served (`resp.model` -- not necessarily the MODEL constant configured in
app/assistant.py, since the server-side refusal fallback used there can
serve a different model on refusal/capacity; see run_chat_turn).

Rates are Anthropic first-party per-1M-token USD prices. Cache reads bill at
~0.1x the input rate, cache writes at ~1.25x -- see Anthropic's pricing docs
(the same ratios app/assistant.py's _log_usage docstring already explains
for why cache hit rate matters).

An unrecognized model id -- an Anthropic model not yet added to the table
below, or (more likely in practice) anything served via OpenRouter, whose
per-model rates aren't Anthropic's own -- returns None rather than guessing.
A wrong number silently baked into a spend ledger is worse than a visibly
missing one. See app/assistant.py's budget_block_reason() for how a None
cost is handled in aggregate (SQL SUM() already skips NULLs, which gives the
"unknown, not zero" behaviour budget enforcement needs without extra code).
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

# USD per 1,000,000 tokens: (input, output). Keep in sync with the models
# app/assistant.py's MODEL / ANTHROPIC_MODEL can actually select.
_RATES_PER_MILLION: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}

_CACHE_READ_MULTIPLIER = Decimal("0.1")
_CACHE_WRITE_MULTIPLIER = Decimal("1.25")

_MILLION = Decimal(1_000_000)


def cost_usd(
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> Decimal | None:
    """Returns the USD cost of one call, or None if `model` isn't in the
    rate table above (an unrecognized Anthropic model, or any OpenRouter
    call). Missing *token* counts are treated as 0, not as unknown -- only
    the model itself needs to be recognized to price a call; a None token
    count just means that field wasn't reported (see _log_usage's "n/a" vs
    0 distinction in app/assistant.py, which is a separate, log-line-only
    concern from this dollar figure)."""
    if not model or model not in _RATES_PER_MILLION:
        if model:
            logger.warning(
                "ai_pricing: no rate table entry for model=%s -- cost not recorded", model
            )
        return None
    input_rate, output_rate = _RATES_PER_MILLION[model]
    input_n = Decimal(input_tokens or 0)
    output_n = Decimal(output_tokens or 0)
    cache_read_n = Decimal(cache_read_tokens or 0)
    cache_write_n = Decimal(cache_write_tokens or 0)

    cost = (
        input_n * input_rate
        + output_n * output_rate
        + cache_read_n * input_rate * _CACHE_READ_MULTIPLIER
        + cache_write_n * input_rate * _CACHE_WRITE_MULTIPLIER
    ) / _MILLION
    return cost.quantize(Decimal("0.000001"))
