"""Shared contract for "identification probes" -- small, read-only, best-
effort connections to a device's own local API to pull identifying
information a passive network scan can't give us (a Sonos zone name and
stereo-pair channel, a TP-Link plug's user-set alias, a generic UPnP device
description). Modeled after discovery/ -- same on-demand, synchronous,
never-raises philosophy -- but kept as a separate top-level package since
these aren't inventory-reconciliation collectors, they're investigation aids
triggered per-asset from the UI.

Adding a new probe type is: write a new module with a PROBE instance, add it
to PROBES in registry.py. Nothing else needs to change.

Hard rule for every probe: read-only. No probe may send a command that
changes device state (e.g. toggling a smart plug's relay) -- see README.md
for why. Everything a probe returns is a *suggestion* for the user (or
Claude, propose-and-approve) to act on by editing the asset; probes never
write to Asset/AssetInterface/AssetService themselves.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_TIMEOUT = 2.0


@dataclass
class ProbeOutcome:
    ok: bool
    summary: str  # one-line human-readable result, shown in the evidence panel
    facts: dict[str, Any] = field(default_factory=dict)  # structured, shown as a table
    raw: str | None = None  # raw response text/XML, kept as evidence
    suggestions: list[dict] = field(default_factory=list)
    # each suggestion: {"field": "position", "value": "...", "reason": "..."}


class Probe(Protocol):
    name: str
    description: str
    # Identification probes (Sonos, Kasa, SSDP) accumulate ProbeResult rows
    # as a history of evidence -- each run is worth keeping. A probe like
    # ping is cheap and meant to be re-run often, so its old results should
    # be replaced rather than piling up and burying identification evidence
    # in the asset detail page's probe panel. Default False; set True to opt
    # a probe's results into replace-on-rerun (see registry.py / the runner
    # in app/routers/dashboard.py).
    replaces_prior_results: bool

    def applies_to(self, asset, interfaces: list, services: list) -> bool:
        """Cheap, local check -- no network I/O -- for whether this probe is
        worth trying for this asset (matching vendor/hostname keywords or an
        expected open port already recorded by discovery)."""
        ...

    def run(self, ip: str, timeout: float = DEFAULT_TIMEOUT) -> ProbeOutcome:
        """Makes the actual read-only network call(s). Must never raise --
        catch everything and return ProbeOutcome(ok=False, ...) so one flaky
        probe can't break the page or a batch run."""
        ...
