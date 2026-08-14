"""Orchestration tests for discovery.cli.run_all_discovery -- the one seam not
covered by the per-collector test modules. No TestClient / no real network:
run_all_discovery looks its run_* wrappers up as module globals, so
monkeypatching them on the discovery.cli namespace is enough."""
import logging

import pytest

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
