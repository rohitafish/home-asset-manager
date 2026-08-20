"""Command-line entry point for on-demand discovery runs.

Usage:
  python -m discovery.cli unifi
  python -m discovery.cli nmap
  python -m discovery.cli all
  python -m discovery.cli account-import
  python -m discovery.cli sonos
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager

import typer
from sqlmodel import Session

from app.clock import utcnow_naive
from app.db import engine
from app.logging_config import configure_logging
from app.models import DiscoveryRun
from discovery.account_import import run_account_import as _run_account_import
from discovery.cve_enrich import enrich_findings_from_services
from discovery.local_host import run_local_host_discovery as _run_local_host_discovery
from discovery.nmap_scan import discover_network
from discovery.normalize import (
    build_gateway_ip_set,
    build_subnet_map,
    find_gateway_mac,
    lookup_vlan,
    normalize_nmap_hosts,
    normalize_unifi_clients,
    normalize_unifi_devices,
    normalize_unifi_devices_legacy,
)
from discovery.reconcile import merge_by_ip, merge_by_mac, reconcile_into_db
from discovery.revaluation import run_revaluation as _run_revaluation
from discovery.sonos_household import (
    run_sonos_household_discovery as _run_sonos_household_discovery,
)
from discovery.unifi_client import UnifiClient

app = typer.Typer()

logger = logging.getLogger(__name__)


@app.callback()
def _init():
    """On-demand discovery runs. Prints a summary per command; pass --help to
    a subcommand for its options."""
    # Runs before any subcommand: configures logging (app/logging_config.py)
    # so collector failure diagnostics reach the terminal -- and logs/app.log
    # when run on the Mini -- instead of the WARNING+ last-resort handler.
    # Command summaries still go to stdout via typer.echo; this governs only
    # logging.
    configure_logging()


@contextmanager
def _tracked_run(source: str, session: Session):
    run = DiscoveryRun(source=source, started_at=utcnow_naive())
    session.add(run)
    session.commit()
    session.refresh(run)
    # Run-boundary breadcrumbs so logs/app.log narrates each discovery without
    # querying the DiscoveryRun table. INFO on the normal path (start + a
    # completion line carrying the summary each run already builds); the failed
    # completion is WARNING so it lands in logs/app.error.log next to the
    # traceback the caller logs via logger.exception -- the two are
    # complementary (boundary + status here, stack there), not duplicates.
    logger.info("discovery run started: source=%s run_id=%s", source, run.id)
    try:
        yield run
        run.status = "completed"
    except Exception as exc:
        # Without this, the finally block's bookkeeping commit below either
        # (a) flushes whatever the collector had already session.add()-ed
        # before failing, as if a half-finished reconcile batch were a
        # complete one, or (b) itself raises PendingRollbackError -- if the
        # failure was DB-level -- which replaces this exception and leaves
        # `run` stuck at status="running" forever, since nothing ever
        # commits the "failed" status. `run` is already a committed row
        # (the session.commit() at the top of this function), so a fresh
        # SELECT after rollback re-fetches it cleanly.
        session.rollback()
        run.status = "failed"
        run.summary = str(exc)
        raise
    finally:
        run.finished_at = utcnow_naive()
        session.add(run)
        session.commit()
        elapsed = (run.finished_at - run.started_at).total_seconds()
        log = logger.info if run.status == "completed" else logger.warning
        log(
            "discovery run %s: source=%s run_id=%s in %.1fs%s",
            run.status,
            source,
            run.id,
            elapsed,
            f" -- {run.summary}" if run.summary else "",
        )


def _get_network_context() -> tuple[list[tuple], set[str], str | None]:
    """Best-effort UniFi enrichment shared by both discovery paths: VLAN/
    subnet lookup, the set of known network gateway IPs, and the site's
    single router's MAC (if unambiguous -- see find_gateway_mac). Degrades to
    empty/None on any exception (rather than failing the whole discovery
    run) if UniFi isn't reachable/configured -- this matters especially for
    the nmap path, which otherwise has no dependency on UniFi at all and
    should still work standalone."""
    try:
        with UnifiClient() as client:
            site_id = client.resolve_site_id(os.environ.get("UNIFI_SITE", "default"))
            networks = client.list_networks_with_subnets(site_id)
            infra_devices = client.list_devices(site_id)
        return (
            build_subnet_map(networks),
            build_gateway_ip_set(networks),
            find_gateway_mac(infra_devices),
        )
    except Exception:
        # A total UniFi outage degrades to empty VLAN/gateway context that
        # flows into reconcile as if the network were legitimately flat -- log
        # so that's distinguishable from a genuinely empty site.
        logger.exception("UniFi network-context lookup failed; degrading to empty")
        return [], set(), None


def run_unifi_discovery() -> dict:
    with Session(engine) as session, _tracked_run("unifi", session) as run:
        site_name = os.environ.get("UNIFI_SITE", "default")
        with UnifiClient() as client:
            site_id = client.resolve_site_id(site_name)
            clients = client.list_clients(site_id)
            infra_devices = client.list_devices(site_id)
            infra_normalized = normalize_unifi_devices(infra_devices)

            # Serials/SKUs only exist on the legacy Controller API (see
            # unifi_client.py) -- best-effort, since it's a bonus on top of
            # the v1 data the rest of discovery depends on.
            legacy_devices = []
            try:
                legacy_raw = client.list_devices_legacy(site_name)
                legacy_devices = normalize_unifi_devices_legacy(legacy_raw)
                infra_normalized = merge_by_mac(infra_normalized, legacy_devices)
            except Exception:
                # Legacy serial/SKU enrichment is a bonus; a failure here must not
                # sink the run, but shouldn't vanish silently either.
                logger.warning("UniFi legacy serial/SKU fetch failed", exc_info=True)

        devices = merge_by_ip(normalize_unifi_clients(clients), infra_normalized)
        subnet_map, gateway_ips, gateway_mac = _get_network_context()
        for device in devices:
            device.network_name, device.vlan = lookup_vlan(device.ip, subnet_map)

        summary = reconcile_into_db(session, devices, gateway_ips, gateway_mac)
        serials = sum(1 for d in legacy_devices if d.serial_number)
        run.summary = (
            f"unifi clients={len(clients)} devices={len(infra_devices)} "
            f"serials={serials} "
            f"created={summary['created']} updated={summary['updated']}"
        )
        return summary


def run_nmap_discovery(use_sudo: bool = False) -> dict:
    with Session(engine) as session, _tracked_run("nmap", session) as run:
        subnets = [
            s.strip()
            for s in os.environ.get("SCAN_SUBNETS", "").split(",")
            if s.strip()
        ]
        if not subnets:
            raise RuntimeError("SCAN_SUBNETS is not set in .env")

        hosts = discover_network(subnets, use_sudo=use_sudo)
        devices = merge_by_ip(normalize_nmap_hosts(hosts))
        subnet_map, gateway_ips, gateway_mac = _get_network_context()
        for device in devices:
            device.network_name, device.vlan = lookup_vlan(device.ip, subnet_map)

        summary = reconcile_into_db(session, devices, gateway_ips, gateway_mac)
        run.summary = (
            f"nmap subnets={subnets} hosts_up={len(hosts)} "
            f"created={summary['created']} updated={summary['updated']}"
        )
        return summary


def run_local_host_discovery() -> dict:
    with Session(engine) as session, _tracked_run("local_host", session) as run:
        result = _run_local_host_discovery(session)
        run.summary = str(result)
        return result


def run_enrichment() -> dict:
    with Session(engine) as session, _tracked_run("cve_enrich", session) as run:
        summary = enrich_findings_from_services(session)
        run.summary = (
            f"services_checked={summary['services_checked']} "
            f"nvd_queries={summary['nvd_queries']} "
            f"candidate_cves={summary['candidate_cves']} "
            f"vulns_created={summary['vulnerabilities_created']} "
            f"findings_created={summary['findings_created']}"
        )
        return summary


def run_account_import(path: str, apply: bool) -> dict:
    """Import transcribed vendor-account data (see discovery/account_import.py
    for why this doesn't go through reconcile_into_db). A dry run (the
    default) never opens a DiscoveryRun -- it writes nothing, so tracking it
    as a run would be noise, and a crashed dry run would leave a stray
    'running' row behind for no reason."""
    if not apply:
        with Session(engine) as session:
            return _run_account_import(path, dry_run=True, session=session)

    with Session(engine) as session, _tracked_run("account_import", session) as run:
        summary = _run_account_import(path, dry_run=False, session=session)
        run.summary = (
            f"path={summary['path']} records={summary['records']} "
            f"matched={summary['matched']} unmatched={summary['unmatched']} "
            f"updated={summary['updated']}"
        )
        return summary


def run_sonos_discovery(dry_run: bool = True) -> dict:
    """Enumerates the whole Sonos household from one reachable player's
    local API (see discovery/sonos_household.py). A dry run (the default)
    never opens a DiscoveryRun, same reasoning as run_account_import."""
    if dry_run:
        with Session(engine) as session:
            return _run_sonos_household_discovery(session, dry_run=True)

    with Session(engine) as session, _tracked_run("sonos_household", session) as run:
        summary = _run_sonos_household_discovery(session, dry_run=False)
        run.summary = (
            f"status={summary.get('status')} "
            f"created={summary.get('created', 0)} updated={summary.get('updated', 0)}"
        )
        return summary


def run_revaluation(apply: bool) -> dict:
    """Backfill Asset.replacement_value from the new-for-old rule (see
    app/valuation.py, discovery/revaluation.py). Deliberately never opens a
    DiscoveryRun even when applying: valuation isn't a discovery collector, and
    a DiscoveryRun row would pollute the dashboard's discovery history."""
    with Session(engine) as session:
        return _run_revaluation(dry_run=not apply, session=session)


def run_all_discovery() -> dict:
    results = {}
    try:
        results["unifi"] = run_unifi_discovery()
    except Exception as exc:
        results["unifi"] = {"error": str(exc)}
    try:
        results["nmap"] = run_nmap_discovery()
    except Exception as exc:
        results["nmap"] = {"error": str(exc)}
    try:
        results["local_host"] = run_local_host_discovery()
    except Exception as exc:
        results["local_host"] = {"error": str(exc)}
    try:
        results["sonos"] = run_sonos_discovery(dry_run=False)
    except Exception as exc:
        results["sonos"] = {"error": str(exc)}
    try:
        results["cve_enrich"] = run_enrichment()
    except Exception as exc:
        results["cve_enrich"] = {"error": str(exc)}
    return results


@app.command()
def unifi():
    """Pull clients + infrastructure devices from the local UniFi controller."""
    typer.echo(run_unifi_discovery())


@app.command()
def nmap(sudo: bool = typer.Option(False, help="Use sudo for a full -sS SYN scan.")):
    """Ping-sweep + service/version scan the configured SCAN_SUBNETS."""
    typer.echo(run_nmap_discovery(use_sudo=sudo))


@app.command(name="local-mac")
def local_mac():
    """Collect this host's own hardware serial/model via system_profiler (macOS only)."""
    typer.echo(run_local_host_discovery())


@app.command(name="account-import")
def account_import(
    path: str = typer.Option("devices/accounts.json", help="Path to the transcribed vendor-account JSON."),
    apply: bool = typer.Option(False, help="Write the changes. Without this, print the diff and exit."),
):
    """Import Amazon/Sonos account data transcribed into devices/accounts.json.
    Prints a dry-run diff by default -- pass --apply to write."""
    summary = run_account_import(path, apply=apply)
    typer.echo(summary.pop("plan"))
    typer.echo(summary)


@app.command()
def revalue(
    apply: bool = typer.Option(False, help="Write the changes. Without this, print the diff and exit."),
):
    """Backfill insurance replacement values (new-for-old) for assets with a
    purchase price and date. Prints a dry-run diff by default -- pass --apply
    to write. Assets with a purchase date but no price are reported as gaps."""
    summary = run_revaluation(apply=apply)
    typer.echo(summary.pop("plan"))
    typer.echo(summary)


@app.command()
def sonos(apply: bool = typer.Option(False, help="Write the changes. Without this, print what was found and exit.")):
    """Enumerate the whole Sonos household from one reachable player's local
    API (port 1400) and reconcile into the inventory. Prints a dry-run
    summary by default -- pass --apply to write."""
    typer.echo(run_sonos_discovery(dry_run=not apply))


@app.command()
def enrich():
    """Match detected service versions against NVD/EPSS/CISA KEV."""
    typer.echo(run_enrichment())


@app.command(name="all")
def all_():
    """Run the UniFi and nmap collectors, then CVE enrichment."""
    typer.echo(run_all_discovery())


if __name__ == "__main__":
    app()
