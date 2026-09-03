# Discovery Collectors Guide

Four collectors feed the same reconciliation pipeline (see
[Architecture Deep-Dive](Architecture-Deep-Dive)). All are synchronous,
on-demand (`./scripts/discover.sh` or the dashboard's Discovery page — nothing
runs on a schedule), and designed to never raise: a source that's unreachable
or misconfigured reports that in the run summary rather than crashing.

## UniFi

**Module:** `discovery/unifi_client.py` · **Needs:** `UNIFI_BASE_URL`,
`UNIFI_API_KEY` (see [Configuration Reference](Configuration-Reference))

Talks to the local UniFi Network **Integration API** (v1, `X-API-KEY` auth) —
either a self-hosted controller or a UDM/Cloud Gateway's console. Pulls:

- **Clients** — every device UniFi has seen, with UniFi's own name for it.
- **Devices** — UniFi-managed infrastructure (APs, switches, the gateway
  itself).
- **Networks** — used to resolve each device's VLAN/subnet by matching its IP
  against the network's `ipv4Configuration`.

One deliberate second call: the v1 API has **no serial number field
anywhere**, so a second, older endpoint (the legacy username/password-era
Controller API, which happens to also accept the same API key) is queried
just for infrastructure devices' serials and SKU-style model numbers.

**Limitations:** the v1 API doesn't expose per-client VLAN/uplink topology
directly (only the legacy Controller API has that), and `macAddress` on a
client isn't guaranteed to be populated — IP address is the join key back to
nmap's ARP-derived MAC where it's missing. UniFi-sourced hostnames are
trusted over nmap's (see
[Architecture Deep-Dive](Architecture-Deep-Dive#keeping-repeated-runs-from-degrading-data-quality)),
so a wrong name in UniFi itself will propagate here.

## nmap

**Module:** `discovery/nmap_scan.py` · **Needs:** `nmap` on `PATH`,
`SCAN_SUBNETS`

Two phases:

1. **`-sn` ping sweep** across every CIDR in `SCAN_SUBNETS` — finds hosts that
   are up, and (for anything on the same L2 segment as the scanning host)
   their ARP-resolved MAC.
2. **`-sV` service scan** against the hosts found up — port/protocol/product/
   version banners, later fed to
   [CVE matching](CVE-Matching-and-Valuation-Methodology).

Default is `-sT` (TCP connect, no elevated privileges), and that is the only
mode the web UI offers. A fuller `-sS` SYN scan is CLI-only (`python -m
discovery.cli nmap --sudo` from a terminal, where `sudo` prompts for a
password) — there is deliberately no passwordless sudoers rule, because on a
Homebrew install the `nmap` binary is user-owned and such a rule would be a
root escalation for anything running as the app user (see the README's "Nmap
privileges"). Shells out to the real `nmap` binary rather than a wrapper
library, so the exact command run is explicit and auditable.

**Limitations:** works against any network (no UniFi dependency), but device
*naming* is much better with UniFi present — a bare nmap hit often only has a
raw reverse-DNS/mDNS hostname. nmap can't ARP-resolve a MAC for a device on a
different VLAN than the scanning host; per-VLAN gateway addresses are handled
as a special case (see [Architecture Deep-Dive](Architecture-Deep-Dive)), but
other cross-VLAN devices may show up with an IP and no MAC.

## Sonos household

**Module:** `discovery/sonos_household.py` · **Needs:** at least one Sonos
player already known to the DB (from a previous UniFi/nmap run or manual entry)

Doesn't use multicast/mDNS SSDP discovery — this network's VLANs don't carry
multicast. Instead it seeds from IPs already associated with a Sonos-looking
asset, tries each until one answers a `GetZoneGroupState` call, and
**enumerates the entire household from that single response** — every player,
room name, and stereo-pair/bonded-satellite topology in one shot. Player
detail (model, serial, firmware) is then filled in per-player from each
player's own `device_description.xml`.

**Limitations:** needs a seed — if no Sonos-looking asset has a known IP yet
(a brand-new install), this collector is a no-op until UniFi/nmap discovery
finds at least one Sonos device first. A bonded satellite (a Sub, a rear
surround) reports its *group's* room name, not its own identity, so satellite
hostnames are deliberately left unset here rather than guessed — see the
module's docstring for the naming-collision history behind that decision.

## local-host

**Module:** `discovery/local_host.py` · **Needs:** macOS (`Darwin`) — a no-op
everywhere else

A one-off collector for the Mac this app happens to be running on: reads its
own serial number, model identifier, and model number straight from
`system_profiler`, then finds its matching `Asset` row (by an existing serial
match, or by its own MAC addresses against `AssetInterface`) and writes the
hardware identity onto it.

**Limitations:** can only ever describe *this* host — there's no way to query
hardware identity for a Mac elsewhere on the network. In practice, running
this from the dev machine reports the dev machine's own hardware; the useful
run is the one triggered on the machine actually serving the app. It
deliberately won't guess when more than one asset row matches — writing a
serial onto the wrong asset is worse than writing none.
