"""Insurance replacement-value logic for assets.

The basis is **new-for-old** (replacement as new), not depreciation: the
figure answers "what does an equivalent new item cost today", which is what
most UK contents policies actually pay out and what a sum insured must
reflect. A depreciated (indemnity) figure would be lower and dangerous to
insure on -- underinsurance triggers the average clause, which cuts every
payout proportionally, even a small one.

    value = purchase_price x (1 + drift) ^ years_since_purchase
    years = (as_of - purchase_date).days / 365.25

then floored at purchase_price and rounded to whole pounds. The floor is the
anti-underinsurance guard: the figure never drops below documented evidence
of what the item cost.

`drift` (below) models the price of the *current equivalent new model* -- not
wear or condition. Consumer tech holds its price point across generations (the
successor lands at roughly what its predecessor cost) while equivalent-spec
prices fall; the two effects broadly cancel, which is why most categories are
0%. This is intentionally a small, hand-tuned table, not a market feed -- see
README's "Replacement values" for the reasoning a claim would lean on.

Pure module: no DB, no I/O. `discovery/cli.py`'s `revalue` command applies it.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.models import AssetType

# Days in a year including the leap-year quarter -- same figure the rest of
# this app's age arithmetic would use, kept explicit rather than magic.
_DAYS_PER_YEAR = Decimal("365.25")

# Past this age the "equivalent current model" premise breaks down: a 2016
# Sonos Play:1 has no like-for-like successor at a comparable price, so the
# formula's output is a guess worth a human's eye. Still valued, but flagged.
_REVIEW_AFTER_YEARS = 8

# Annual price drift of the current equivalent NEW model, per AssetType.
# Each rate is a claim-defensible judgement, not a measurement -- the comment
# is the justification (house style; cf. ruff.toml's header, check-pii.sh's
# allowlist).
_ANNUAL_DRIFT: dict[AssetType, Decimal] = {
    # Successor models hold the price point; falling equivalent-spec prices
    # (spec deflation) offset CPI, so the net is ~flat.
    AssetType.end_user_device: Decimal("0.00"),
    AssetType.mobile: Decimal("0.00"),
    AssetType.server: Decimal("0.00"),
    AssetType.network_device: Decimal("0.00"),
    AssetType.removable_media: Decimal("0.00"),
    # Blended: this bucket mixes cheap consumer smart devices (flat, like the
    # tech above) with *installed* home-energy kit whose replacement cost
    # includes labour, tracking wage inflation rather than tech pricing.
    AssetType.iot: Decimal("0.02"),
}

# Not insurable physical contents -- a subscription or licence has no
# replacement cost in the contents sense. Skipped, never valued at zero
# (zero would read as "worthless", which is a different and wrong claim).
_EXCLUDED_TYPES: frozenset[AssetType] = frozenset(
    {AssetType.cloud_service, AssetType.software}
)

# Defined but currently mapped to no AssetType -- a home for a future non-tech
# category (general household goods track CPI, ~3%/yr). Referenced here so the
# constant isn't dead, and so the number has a documented origin if adopted.
_GENERAL_GOODS_DRIFT = Decimal("0.03")


def _years_since(purchase_date: date, as_of: date) -> Decimal:
    return Decimal((as_of - purchase_date).days) / _DAYS_PER_YEAR


def replacement_value(
    purchase_price: Decimal | None,
    purchase_date: date | None,
    asset_type: AssetType,
    as_of: date | None = None,
) -> Decimal | None:
    """New-for-old replacement value, rounded to whole pounds, or None when
    the inputs can't support a defensible figure.

    Returns None (skip, don't guess) when: either input is missing; the type
    isn't insurable contents (cloud_service/software); the price is
    non-positive; or the purchase date is in the future.
    """
    if purchase_price is None or purchase_date is None:
        return None
    if asset_type in _EXCLUDED_TYPES:
        return None
    if purchase_price <= 0:
        return None

    as_of = as_of or date.today()
    years = _years_since(purchase_date, as_of)
    if years < 0:  # purchase date in the future -- bad data, not valuable info
        return None

    drift = _ANNUAL_DRIFT.get(asset_type, _GENERAL_GOODS_DRIFT)
    # Decimal ** Decimal isn't defined for a non-integer exponent; go through
    # float for the growth factor only, then return to Decimal immediately so
    # all money arithmetic and rounding stay exact.
    factor = Decimal(str((1 + float(drift)) ** float(years)))
    value = purchase_price * factor

    # Floor at what was paid: never insure below documented cost.
    value = max(value, purchase_price)
    return value.quantize(Decimal(1), rounding=ROUND_HALF_UP)


def needs_review(purchase_date: date | None, as_of: date | None = None) -> bool:
    """True when an item is old enough that the equivalent-current-model
    premise is shaky and a human should sanity-check the figure."""
    if purchase_date is None:
        return False
    as_of = as_of or date.today()
    return _years_since(purchase_date, as_of) > _REVIEW_AFTER_YEARS
