"""Single source of truth for "now" in UTC.

datetime.utcnow() is deprecated since Python 3.12 -- it's the actual source
of the "datetime.datetime.utcnow() is deprecated" warnings seen throughout
this app's test output. Every datetime/date column in this schema is a
naive TIMESTAMP (see migrations/versions/ -- no timezone=True anywhere),
and app/template_filters.py's `localdt` filter plus every staleness/age
comparison in this app assumes that naive-UTC contract. Moving to real
timezone-aware columns is a separate, larger piece of work (schema
migration, an audit of every comparison site, a localdt rewrite) -- not
something to fold into a deprecation fix. This helper returns the exact
same naive-UTC value datetime.utcnow() always did, just without calling
the deprecated method.
"""
from datetime import UTC, datetime


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
