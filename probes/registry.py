"""The one place that knows about every probe. Adding a new investigation
type later is: write a new probes/<name>.py module exposing a PROBE
instance (see probes/sonos.py for the shape), then add it to
IDENTIFICATION_PROBES (or ALWAYS_PROBES, if it's not identification --
see ping.py) below.
"""

from probes import kasa, ping, sonos, ssdp
from probes.base import Probe

# Runs for every asset with a known IP, regardless of what else matches --
# currently just the ping probe (see probes/ping.py). Kept separate from
# IDENTIFICATION_PROBES below so it can never suppress the SSDP fallback:
# applicable_probes() only treats IDENTIFICATION_PROBES as "specific."
ALWAYS_PROBES: list[Probe] = [ping.PROBE]

# Specific probes are tried first, in this order; ssdp is the generic
# fallback and is only used when none of these claim the asset (see
# applicable_probes below) -- otherwise every device would also get a
# redundant, less-informative SSDP entry alongside its Sonos/Kasa result.
IDENTIFICATION_PROBES: list[Probe] = [sonos.PROBE, kasa.PROBE]
FALLBACK_PROBE: Probe = ssdp.PROBE

PROBES: list[Probe] = [*ALWAYS_PROBES, *IDENTIFICATION_PROBES, FALLBACK_PROBE]


def applicable_probes(asset, interfaces: list, services: list) -> list[Probe]:
    always = [p for p in ALWAYS_PROBES if p.applies_to(asset, interfaces, services)]

    specific = [
        probe
        for probe in IDENTIFICATION_PROBES
        if probe.applies_to(asset, interfaces, services)
    ]
    if not specific and FALLBACK_PROBE.applies_to(asset, interfaces, services):
        specific = [FALLBACK_PROBE]

    # Ping last: asset_probe() (app/routers/dashboard.py) writes ProbeResult
    # rows in this order, and the evidence panel sorts newest-first -- so
    # ping ends up on top, giving reachability context immediately above the
    # identification result(s) it helps explain.
    return specific + always
