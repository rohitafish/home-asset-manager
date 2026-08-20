"""Tests for discovery/unifi_client.py's _paginate() -- specifically the
offset-doesn't-advance guard and the hard page cap added alongside the AI
spending-boundaries work (this was the one genuinely unbounded network loop
in the Python code: termination previously depended entirely on
server-supplied `limit`/`totalCount` values).

Network calls are stubbed by monkeypatching the underlying httpx.Client's
.get() on the instance, same spirit as tests/test_sonos_household.py
monkeypatching its fetch_* functions -- no transport-level mocking library
is used anywhere else in this repo.
"""

import logging

from discovery.unifi_client import _MAX_PAGES, UnifiClient


def _client() -> UnifiClient:
    return UnifiClient(base_url="https://unifi.test", api_key="k", verify_tls=False)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_paginate_normal_multi_page(monkeypatch):
    pages = [
        {"data": [{"id": 1}, {"id": 2}], "totalCount": 3, "limit": 2},
        {"data": [{"id": 3}], "totalCount": 3, "limit": 2},
    ]
    calls = {"n": 0}

    def fake_get(url, params=None):
        resp = _FakeResponse(pages[calls["n"]])
        calls["n"] += 1
        return resp

    client = _client()
    monkeypatch.setattr(client._client, "get", fake_get)

    results = client._paginate("/sites/x/clients")

    assert [r["id"] for r in results] == [1, 2, 3]
    assert calls["n"] == 2  # stopped as soon as offset caught up to totalCount


def test_paginate_stops_when_a_later_page_comes_back_empty(monkeypatch):
    """An empty `data` page stops pagination even if totalCount claims more
    remain -- termination shouldn't rely on totalCount being trustworthy."""
    def fake_get(url, params=None):
        if params["offset"] == 0:
            return _FakeResponse({"data": [{"id": 1}], "totalCount": 500, "limit": 200})
        return _FakeResponse({"data": [], "totalCount": 500, "limit": 200})

    client = _client()
    monkeypatch.setattr(client._client, "get", fake_get)

    results = client._paginate("/sites/x/clients")

    assert [r["id"] for r in results] == [1]


def test_paginate_survives_an_explicit_null_limit(monkeypatch):
    """Regression test: `.get("limit", default)` only falls back to
    `default` when the key is ABSENT -- a response with an explicit
    `"limit": null` (key present, value None) still returned None here,
    and `offset + None` raised TypeError before the stall guard could even
    run."""
    pages = [
        {"data": [{"id": 1}, {"id": 2}], "totalCount": 3, "limit": None},
        {"data": [{"id": 3}], "totalCount": 3, "limit": None},
    ]
    calls = {"n": 0}

    def fake_get(url, params=None):
        resp = _FakeResponse(pages[calls["n"]])
        calls["n"] += 1
        return resp

    client = _client()
    monkeypatch.setattr(client._client, "get", fake_get)

    results = client._paginate("/sites/x/clients")  # must not raise

    assert [r["id"] for r in results] == [1, 2, 3]


def test_paginate_stops_and_warns_when_offset_does_not_advance(monkeypatch, caplog):
    """A page reporting "limit": 0 alongside non-empty data must not spin
    forever re-requesting the same page and growing results without bound --
    this is the regression this guard exists for."""
    stall_page = {"data": [{"id": 1}], "totalCount": 999, "limit": 0}

    def fake_get(url, params=None):
        return _FakeResponse(stall_page)

    client = _client()
    monkeypatch.setattr(client._client, "get", fake_get)

    with caplog.at_level(logging.WARNING, logger="discovery.unifi_client"):
        results = client._paginate("/sites/x/clients")

    assert len(results) == 1  # only ever fetched once, not spun forever
    assert any("stalled" in r.getMessage() for r in caplog.records)


def test_paginate_hits_hard_cap_and_warns(monkeypatch, caplog):
    """Even when the offset keeps strictly advancing, a server that always
    reports more remaining than it ever delivers must still terminate --
    via the hard page cap, not by hanging the request thread forever."""
    def fake_get(url, params=None):
        offset = params["offset"]
        # totalCount always stays one item ahead of what's been collected,
        # so "next_offset >= total" never fires on its own.
        return _FakeResponse({"data": [{"id": offset}], "totalCount": offset + 2, "limit": 1})

    client = _client()
    monkeypatch.setattr(client._client, "get", fake_get)

    with caplog.at_level(logging.WARNING, logger="discovery.unifi_client"):
        results = client._paginate("/sites/x/clients")

    assert len(results) == _MAX_PAGES
    assert any("page cap" in r.getMessage() for r in caplog.records)
