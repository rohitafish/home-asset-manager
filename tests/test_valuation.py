"""Tests for app/valuation.py's pure new-for-old replacement-value logic.

Pure-function style (cf. tests/test_dashboard_helpers.py): every case pins an
explicit `as_of` so nothing depends on today's date, and asserts on Decimal
inputs directly rather than round-tripping through the DB (the suite's SQLite
engine doesn't handle Numeric natively).
"""

from datetime import date
from decimal import Decimal

from app.models import AssetType
from app.valuation import needs_review, replacement_value

AS_OF = date(2026, 8, 9)


def test_zero_drift_category_returns_purchase_price():
    # end_user_device drifts 0%/yr, so new-for-old == what was paid.
    value = replacement_value(
        Decimal("1099.00"), date(2025, 4, 9), AssetType.end_user_device, AS_OF
    )
    assert value == Decimal(1099)


def test_iot_uplift_over_known_interval():
    # iot drifts 2%/yr; ~1.03 years from 2025-07-28 => a few percent up.
    value = replacement_value(
        Decimal("7794.00"), date(2025, 7, 28), AssetType.iot, AS_OF
    )
    assert value == Decimal(7955)


def test_result_is_whole_pounds():
    value = replacement_value(
        Decimal("1358.31"), date(2025, 1, 14), AssetType.iot, AS_OF
    )
    assert value == value.quantize(Decimal(1))
    assert value == Decimal(1401)


def test_floor_holds_when_drift_would_lower_value():
    # A zero-drift item bought today: factor is exactly 1, and the floor
    # guarantees the figure never dips below the documented purchase price.
    value = replacement_value(
        Decimal("500.00"), AS_OF, AssetType.mobile, AS_OF
    )
    assert value == Decimal(500)


def test_excluded_types_return_none():
    for excluded in (AssetType.software, AssetType.cloud_service):
        assert (
            replacement_value(Decimal("100.00"), date(2024, 1, 1), excluded, AS_OF)
            is None
        )


def test_missing_price_returns_none():
    assert replacement_value(None, date(2025, 1, 1), AssetType.iot, AS_OF) is None


def test_missing_date_returns_none():
    assert replacement_value(Decimal("100.00"), None, AssetType.iot, AS_OF) is None


def test_non_positive_price_returns_none():
    assert replacement_value(Decimal("0.00"), date(2025, 1, 1), AssetType.iot, AS_OF) is None
    assert replacement_value(Decimal("-5.00"), date(2025, 1, 1), AssetType.iot, AS_OF) is None


def test_future_purchase_date_returns_none():
    assert (
        replacement_value(Decimal("100.00"), date(2027, 1, 1), AssetType.iot, AS_OF)
        is None
    )


def test_needs_review_just_inside_eight_years_is_false():
    # 8 years minus a day old -- still within the like-for-like window.
    just_under = date(AS_OF.year - 8, AS_OF.month, AS_OF.day + 1)
    assert needs_review(just_under, AS_OF) is False


def test_needs_review_past_eight_years_is_true():
    old = date(2016, 2, 20)  # a real ~10-year-old Sonos date from the data
    assert needs_review(old, AS_OF) is True


def test_needs_review_missing_date_is_false():
    assert needs_review(None, AS_OF) is False
