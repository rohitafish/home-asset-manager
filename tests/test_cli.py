"""Orchestration tests for discovery.cli.run_all_discovery -- the one seam not
covered by the per-collector test modules. No real network: run_all_discovery
looks its run_* wrappers up as module globals, so monkeypatching them on the
discovery.cli namespace is enough."""
import logging

import pytest
from sqlmodel import select

from app.models import Asset, AssetType, DiscoveryRun
from discovery import cli


def _stub_all(monkeypatch, **overrides):
    """Replace every run_* wrapper with a no-op returning a marker dict, so a
    single test can assert orchestration without touching the network or DB.
    Pass overrides (e.g. run_unifi_discovery=raiser) to change one of them."""
    defaults = {
        "run_unifi_discovery": lambda: {"ran": "unifi"},
        "run_nmap_discovery": lambda: {"ran": "nmap"},
        "run_local_host_discovery": lambda: {"ran": "local_host"},
        "run_sonos_discovery": lambda dry_run=True: {"ran": "sonos", "dry_run": dry_run},
        "run_enrichment": lambda: {"ran": "cve_enrich"},
    }
    defaults.update(overrides)
    for name, fn in defaults.items():
        monkeypatch.setattr(cli, name, fn)


def test_run_all_includes_sonos_and_applies(monkeypatch):
    seen = {}
    _stub_all(
        monkeypatch,
        run_sonos_discovery=lambda dry_run=True: seen.setdefault("dry_run", dry_run)
        or {"ran": "sonos"},
    )

    results = cli.run_all_discovery()

    assert results["sonos"] == {"ran": "sonos"}
    # run_all must write, not dry-run -- otherwise no DiscoveryRun is recorded.
    assert seen["dry_run"] is False


def test_run_all_isolates_a_failing_collector(monkeypatch):
    def boom():
        raise RuntimeError("unifi exploded")

    _stub_all(monkeypatch, run_unifi_discovery=boom)

    results = cli.run_all_discovery()

    # The failure is captured for its own collector...
    assert results["unifi"] == {"error": "unifi exploded"}
    # ...and does not abort the collectors that follow it.
    assert results["sonos"]["ran"] == "sonos"
    assert results["cve_enrich"] == {"ran": "cve_enrich"}


def test_tracked_run_logs_start_and_completion_breadcrumbs(session, caplog):
    """_tracked_run wraps every tracked collector, so a start line and a
    completion line carrying the run's summary land in the log for all of
    them -- that's what lets logs/app.log narrate a run without the DB."""
    with (
        caplog.at_level(logging.INFO, logger="discovery.cli"),
        cli._tracked_run("nmap", session) as run,
    ):
        run.summary = "created=2 updated=1"

    msgs = [r.getMessage() for r in caplog.records]
    assert any("discovery run started" in m and "source=nmap" in m for m in msgs)
    completed = [m for m in msgs if "discovery run completed" in m]
    assert completed and "created=2 updated=1" in completed[0]


def test_tracked_run_logs_failure_as_warning_and_reraises(session, caplog):
    """A failed run's boundary is WARNING (so it reaches app.error.log beside
    the caller's traceback) and carries the exception text; the exception still
    propagates so the caller's own handling runs."""
    with (
        caplog.at_level(logging.INFO, logger="discovery.cli"),
        pytest.raises(RuntimeError),
        cli._tracked_run("unifi", session),
    ):
        raise RuntimeError("boom")

    failed = [r for r in caplog.records if "discovery run failed" in r.getMessage()]
    assert failed, "expected a failure breadcrumb"
    assert failed[0].levelno == logging.WARNING
    assert "boom" in failed[0].getMessage()


def test_tracked_run_rolls_back_partial_work_on_failure(session):
    """Regression test: without session.rollback() in the except block, a
    partially-completed collector's uncommitted work would get flushed by
    the finally block's own bookkeeping commit right along with it, as if a
    half-finished reconcile batch were a complete one. Also confirms the
    DiscoveryRun row itself lands on status="failed" rather than a
    PendingRollbackError from a poisoned session replacing the real one and
    leaving it stuck "running" forever."""
    with pytest.raises(RuntimeError), cli._tracked_run("nmap", session) as run:
        session.add(Asset(asset_type=AssetType.end_user_device, hostname="half-done"))
        raise RuntimeError("collector exploded mid-batch")

    session.refresh(run)
    assert run.status == "failed"
    assert "collector exploded mid-batch" in run.summary
    # The partial work must not have landed alongside the failure bookkeeping.
    assert session.exec(select(Asset).where(Asset.hostname == "half-done")).first() is None


class _LeakyUnifiClientStub:
    """Stands in for UnifiClient: tracks whether it was closed, and its
    first call raises -- reproducing "an exception from resolve_site_id/
    list_*", the exact trigger for the httpx-client leak this pins."""
    closed_count = 0

    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        _LeakyUnifiClientStub.closed_count += 1

    def resolve_site_id(self, site):
        raise RuntimeError("UniFi controller unreachable")


def test_get_network_context_closes_the_client_even_on_failure(monkeypatch):
    """Regression test: client.close() used to be called only at the end of
    the happy path, so an exception from resolve_site_id/list_* silently
    leaked the pooled httpx.Client's connections on every failed attempt --
    the try/except around the whole function swallows the exception, so
    nothing ever surfaced the leak."""
    _LeakyUnifiClientStub.closed_count = 0
    monkeypatch.setattr(cli, "UnifiClient", _LeakyUnifiClientStub)

    result = cli._get_network_context()  # must not raise -- degrades to empty

    assert result == ([], set(), None)
    assert _LeakyUnifiClientStub.closed_count == 1


# -- the --apply -> dry_run polarity ------------------------------------------
# Three CLI wrappers translate a user-facing `--apply` flag into the
# `dry_run` keyword their importer expects, and two of them do it by
# negation: `_run_revaluation(dry_run=not apply, ...)`. That negation is the
# entire safety boundary between "print a diff" and "rewrite the live
# Postgres" -- and it lives in the one layer the importers' own dry-run tests
# (see tests/test_revaluation.py et al) can't see, because they call the
# importer directly.
#
# These assert the translation itself, with the importer stubbed: the concern
# here is the flag's polarity at this seam, not what the importer does with
# it. cli.engine is repointed at the in-memory engine because each wrapper
# opens its own Session(engine) rather than being handed one.


@pytest.fixture()
def _local_engine(engine, monkeypatch):
    monkeypatch.setattr(cli, "engine", engine)


def _spy(recorder):
    def stub(*args, dry_run=True, session=None, **kwargs):
        recorder.append(dry_run)
        return {"applied": not dry_run, "path": "x", "records": 0, "matched": 0,
                "unmatched": 0, "updated": 0}

    return stub


@pytest.mark.parametrize(("apply_flag", "expected_dry_run"), [(False, True), (True, False)])
def test_revalue_maps_apply_to_dry_run(monkeypatch, _local_engine, apply_flag, expected_dry_run):
    seen = []
    monkeypatch.setattr(cli, "_run_revaluation", _spy(seen))

    cli.run_revaluation(apply=apply_flag)

    assert seen == [expected_dry_run]


@pytest.mark.parametrize(("apply_flag", "expected_dry_run"), [(False, True), (True, False)])
def test_resync_exposure_maps_apply_to_dry_run(
    monkeypatch, _local_engine, apply_flag, expected_dry_run
):
    seen = []
    monkeypatch.setattr(cli, "_run_exposure_resync", _spy(seen))

    cli.run_exposure_resync(apply=apply_flag)

    assert seen == [expected_dry_run]


@pytest.mark.parametrize(("apply_flag", "expected_dry_run"), [(False, True), (True, False)])
def test_account_import_maps_apply_to_dry_run(
    monkeypatch, _local_engine, apply_flag, expected_dry_run
):
    seen = []
    monkeypatch.setattr(cli, "_run_account_import", _spy(seen))

    cli.run_account_import("accounts.json", apply=apply_flag)

    assert seen == [expected_dry_run]


def test_account_import_dry_run_opens_no_discovery_run(monkeypatch, _local_engine, session):
    """A dry run writes nothing, so tracking it as a DiscoveryRun would be
    noise -- and a crashed one would leave a stray 'running' row behind.
    Documented in run_account_import's docstring; pinned here."""
    monkeypatch.setattr(cli, "_run_account_import", _spy([]))

    cli.run_account_import("accounts.json", apply=False)

    assert session.exec(select(DiscoveryRun)).all() == []


def test_account_import_apply_does_open_a_discovery_run(monkeypatch, _local_engine, session):
    """The counterweight: the applying path is tracked, so the dashboard's
    discovery history shows an import that actually changed data."""
    monkeypatch.setattr(cli, "_run_account_import", _spy([]))

    cli.run_account_import("accounts.json", apply=True)

    runs = session.exec(select(DiscoveryRun)).all()
    assert len(runs) == 1
    assert runs[0].source == "account_import"
    assert runs[0].status == "completed"
