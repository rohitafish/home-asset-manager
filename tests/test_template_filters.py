"""Tests for app/template_filters.py.

Both filters were at 0% body coverage -- reached only from Jinja, and
nothing rendered a template. tests/test_dashboard_routes.py now exercises
them through a real page, which proves they are registered; these pin what
they actually produce, including the case the module exists for.

That case is the BST/GMT transition. Every datetime in this schema is naive
UTC (see app/clock.py), and the dashboard is read in Europe/London, so
rendering with a fixed offset would be right for half the year and an hour
out for the other half -- with no error, on timestamps ("last seen", a probe
run, a note) whose whole value is telling someone when something happened.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.template_filters import localdt, money

# -- localdt ------------------------------------------------------------------


def test_localdt_renders_gmt_unchanged_in_winter():
    """January: Europe/London is UTC+0, so the wall clock matches the stored
    naive-UTC value."""
    assert localdt(datetime(2026, 1, 15, 9, 30)) == "2026-01-15 09:30"


def test_localdt_shifts_an_hour_forward_in_summer():
    """July: BST is UTC+1. A fixed-offset implementation would get one of
    these two tests right and the other silently wrong."""
    assert localdt(datetime(2026, 7, 15, 9, 30)) == "2026-07-15 10:30"


@pytest.mark.parametrize(
    ("stored_utc", "expected"),
    [
        # 2026's transitions: BST begins 29 March 01:00 UTC, ends 25 October
        # 02:00 local (01:00 UTC).
        (datetime(2026, 3, 29, 0, 59), "2026-03-29 00:59"),
        (datetime(2026, 3, 29, 1, 0), "2026-03-29 02:00"),
        (datetime(2026, 10, 25, 0, 59), "2026-10-25 01:59"),
        (datetime(2026, 10, 25, 1, 0), "2026-10-25 01:00"),
    ],
)
def test_localdt_is_correct_either_side_of_a_transition(stored_utc, expected):
    assert localdt(stored_utc) == expected


def test_localdt_renders_a_dash_for_none():
    """Most of these columns are nullable -- a device discovered but never
    seen again, a finding with no due date."""
    assert localdt(None) == "—"


def test_localdt_accepts_a_custom_format():
    """Called as `|localdt("%Y-%m-%d")` in asset_detail.html and with
    seconds in discovery.html."""
    assert localdt(datetime(2026, 7, 15, 9, 30, 45), "%Y-%m-%d") == "2026-07-15"
    assert (
        localdt(datetime(2026, 7, 15, 9, 30, 45), "%Y-%m-%d %H:%M:%S")
        == "2026-07-15 10:30:45"
    )


def test_localdt_does_not_mutate_the_value_it_is_given():
    """It renders ORM attributes straight out of a live session -- replacing
    tzinfo in place would corrupt the object being displayed."""
    stored = datetime(2026, 7, 15, 9, 30)

    localdt(stored)

    assert stored == datetime(2026, 7, 15, 9, 30)
    assert stored.tzinfo is None


def test_localdt_raises_on_a_plain_date():
    """Documented in the module's own comment: purchase_date and
    warranty_expiry are `date` columns -- calendar facts off a receipt with
    no timezone meaning -- and are deliberately rendered with Jinja's default
    str() instead. This pins why they can't just be piped through here."""
    with pytest.raises(TypeError):
        localdt(date(2026, 7, 15))


# -- money --------------------------------------------------------------------


def test_money_renders_pounds_with_two_decimal_places():
    assert money(Decimal("1234.56")) == "£1,234.56"


def test_money_groups_thousands():
    assert money(Decimal("1234567.89")) == "£1,234,567.89"


def test_money_pads_a_whole_number_to_cents():
    """The column is Numeric(12, 2) but a hand-entered value can arrive as
    a bare integer -- "£430" in a valuables table beside "£1,234.56" reads
    as a different kind of number."""
    assert money(Decimal(430)) == "£430.00"


def test_money_renders_a_dash_for_none():
    """An unpriced asset must not render as "£0.00" -- that's a claim about
    its value, and this table is used for insurance."""
    assert money(None) == "—"


def test_money_renders_zero_as_zero_not_a_dash():
    """The counterweight: a genuine zero is a value, and `if value is None`
    rather than `if not value` is what keeps the two distinguishable."""
    assert money(Decimal("0.00")) == "£0.00"
