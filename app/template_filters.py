"""Jinja2 filters shared across dashboard templates."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")
LOCAL_TZ = ZoneInfo("Europe/London")


def localdt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Renders a naive UTC datetime (as stored in the DB) in local time,
    correctly handling the BST/GMT transition rather than a fixed offset."""
    if value is None:
        return "—"
    return value.replace(tzinfo=UTC).astimezone(LOCAL_TZ).strftime(fmt)


# Note: purchase_date/warranty_expiry are plain `date` columns, not
# `datetime` -- they're calendar facts off a receipt with no timezone
# meaning, so they're rendered with Jinja's default str() (already
# "YYYY-MM-DD") rather than piped through localdt, which calls
# .replace(tzinfo=...) and would raise on a bare date.


def money(value: Decimal | None) -> str:
    """Renders a Numeric(12, 2) value as "£1,234.56", or "—" if unset."""
    if value is None:
        return "—"
    return f"£{value:,.2f}"
