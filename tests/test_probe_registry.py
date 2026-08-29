"""Tests for probes/registry.py's applicable_probes -- the dispatch that
decides which probes actually get pointed at a device.

Every probe module was tested; the function choosing between them was not
called by anything in the suite. It is pure (no network, no DB -- just the
asset's vendor/hostname and its recorded open ports) and it carries three
invariants written into the module's own comments, each of which changes
what the app sends onto the LAN if it breaks:

  * the SSDP fallback runs only when no specific probe claims the asset,
    otherwise every Sonos and Kasa device also collects a redundant, less
    informative SSDP result;
  * the ping probe runs for anything with an IP, and is never suppressed by
    a specific probe matching -- a Sonos probe reporting "no response" is
    ambiguous until you know whether the host answers ICMP at all;
  * ping comes last in the returned order, because app/routers/dashboard.py
    writes ProbeResult rows in that order and the evidence panel sorts
    newest-first, so ping ends up on top of the identification result it
    exists to qualify.

The last one is the sort of thing that survives a refactor by luck. Pinned
here rather than left to the comment.
"""

from types import SimpleNamespace

import pytest

from probes import kasa, ping, sonos, ssdp
from probes.registry import applicable_probes


def _asset(hostname=None, vendor=None):
    return SimpleNamespace(hostname=hostname, vendor=vendor)


def _iface(ip="192.168.1.10"):
    return SimpleNamespace(ip=ip)


def _service(port):
    return SimpleNamespace(port=port)


def _names(probes):
    return [p.name for p in probes]


# -- which specific probe claims the asset ------------------------------------


@pytest.mark.parametrize(
    "asset",
    [
        _asset(hostname="Living-Room-Sonos"),
        _asset(vendor="Sonos, Inc."),
        _asset(hostname="SONOS-UPPERCASE"),
    ],
)
def test_sonos_is_claimed_by_hostname_or_vendor(asset):
    chosen = applicable_probes(asset, [_iface()], [])
    assert sonos.PROBE.name in _names(chosen)
    assert ssdp.PROBE.name not in _names(chosen)


def test_sonos_is_claimed_by_its_open_port_alone():
    """A player discovery has not yet named or attributed -- port 1400 is
    what identifies it."""
    chosen = applicable_probes(_asset(), [_iface()], [_service(1400)])
    assert sonos.PROBE.name in _names(chosen)


@pytest.mark.parametrize(
    "asset",
    [
        _asset(vendor="TP-Link Corporation"),
        _asset(hostname="kasa-plug-1"),
        _asset(hostname="tapo-camera"),
        _asset(hostname="kitchen-plug"),
    ],
)
def test_kasa_is_claimed_by_hostname_or_vendor(asset):
    chosen = applicable_probes(asset, [_iface()], [])
    assert kasa.PROBE.name in _names(chosen)
    assert ssdp.PROBE.name not in _names(chosen)


def test_kasa_is_claimed_by_its_open_port_alone():
    chosen = applicable_probes(_asset(), [_iface()], [_service(9999)])
    assert kasa.PROBE.name in _names(chosen)


# -- the SSDP fallback --------------------------------------------------------


def test_ssdp_is_used_when_nothing_specific_claims_the_asset():
    chosen = applicable_probes(_asset(hostname="unknown-box"), [_iface()], [])

    assert ssdp.PROBE.name in _names(chosen)


def test_ssdp_is_suppressed_when_a_specific_probe_claims_the_asset():
    """The whole reason the fallback is kept out of IDENTIFICATION_PROBES:
    otherwise every identified device also collects a redundant SSDP row."""
    chosen = applicable_probes(_asset(vendor="Sonos, Inc."), [_iface()], [])

    assert ssdp.PROBE.name not in _names(chosen)


def test_a_device_matching_two_specific_probes_gets_both_and_no_fallback():
    """Contrived, but it pins that `specific` is a list rather than a
    first-match -- and that one match doesn't shadow another."""
    asset = _asset(hostname="sonos-and-kasa-somehow")
    chosen = applicable_probes(asset, [_iface()], [_service(1400), _service(9999)])

    assert sonos.PROBE.name in _names(chosen)
    assert kasa.PROBE.name in _names(chosen)
    assert ssdp.PROBE.name not in _names(chosen)


# -- the always-on ping probe -------------------------------------------------


def test_ping_runs_for_any_asset_with_an_ip():
    chosen = applicable_probes(_asset(hostname="anything"), [_iface()], [])

    assert ping.PROBE.name in _names(chosen)


def test_ping_is_not_suppressed_by_a_specific_probe_matching():
    """ALWAYS_PROBES is kept separate from IDENTIFICATION_PROBES precisely so
    a matched Sonos/Kasa probe can't displace reachability."""
    chosen = applicable_probes(_asset(vendor="Sonos, Inc."), [_iface()], [])

    assert ping.PROBE.name in _names(chosen)
    assert sonos.PROBE.name in _names(chosen)


def test_nothing_applies_to_an_asset_with_no_ip():
    """No IP means nothing to connect to -- ping's applies_to is the only
    one that checks, and the fallback would otherwise be pointed at nothing."""
    chosen = applicable_probes(_asset(hostname="offline-record"), [], [])

    assert ping.PROBE.name not in _names(chosen)


def test_an_interface_with_a_null_ip_does_not_count_as_reachable():
    """A discovered MAC with no address yet -- AssetInterface.ip is nullable."""
    chosen = applicable_probes(_asset(), [SimpleNamespace(ip=None)], [])

    assert ping.PROBE.name not in _names(chosen)


# -- ordering -----------------------------------------------------------------


def test_ping_is_ordered_last():
    """app/routers/dashboard.py writes ProbeResult rows in this order and the
    evidence panel sorts newest-first, so last here means top of the panel --
    reachability context sitting immediately above the identification result
    it qualifies."""
    chosen = applicable_probes(_asset(vendor="Sonos, Inc."), [_iface()], [])

    assert _names(chosen)[-1] == ping.PROBE.name


def test_ping_is_ordered_last_behind_the_fallback_too():
    chosen = applicable_probes(_asset(hostname="unknown-box"), [_iface()], [])

    assert _names(chosen) == [ssdp.PROBE.name, ping.PROBE.name]
