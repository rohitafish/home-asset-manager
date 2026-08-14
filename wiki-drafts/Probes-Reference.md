# Probes Reference

Probes are triggered **per-asset from the UI** (or by the AI assistant's
`run_probe` tool — see [The LLM Assistant, Explained](The-LLM-Assistant-Explained))
to ask one specific device a direct question via its own local API. See
[Architecture Deep-Dive](Architecture-Deep-Dive#probes-on-demand-investigation-not-collection)
for how probes differ from the discovery collectors.

**Every probe is read-only.** None of the modules below contains a code path
that changes device state — no toggling a plug, no playback control. Every
finding is a *suggestion* for a human (or the AI assistant, under its own
propose-and-approve rules) to act on by editing the asset.

## Dispatch order

`probes/registry.py` decides which probes run for a given asset:

1. **Specific identification probes** (Sonos, Kasa) are tried first, based on
   a cheap, no-network `applies_to()` check (vendor/hostname keywords, or an
   expected open port already on record).
2. If none of those claim the asset, the **generic SSDP fallback** runs
   instead — so every device isn't also cluttered with a redundant,
   less-informative SSDP entry alongside its Sonos/Kasa result.
3. **`ping`** always runs alongside, for any asset with a known IP — it
   identifies nothing, but qualifies every other probe's result (a "no
   response" from Sonos is ambiguous until you know whether the host answers
   ICMP at all).

## The probes

| Probe | Targets | Reads | Notes |
|---|---|---|---|
| `sonos` | Port 1400 / hostname or vendor contains "sonos" | Zone/room name, model, firmware, stereo-pair channel | Shares its protocol layer with the [Sonos household collector](Discovery-Collectors-Guide#sonos-household) |
| `kasa` | Port 9999, or hostname/vendor suggests TP-Link/Kasa/Tapo | App-set alias, model, firmware, relay state | **Legacy protocol only** — see below |
| `ssdp` | Fallback — any asset nothing else claimed | UPnP `friendlyName`, manufacturer, model | The device-supplied `LOCATION` fetch is SSRF-hardened — see [Security Model](Security-Model) |
| `ping` | Any asset with a known IP | Reachability, RTT, TTL | Identifies nothing; a non-reply is *not* proof the device is off |

### Sonos

Reads a player's zone name and (for a stereo pair) which channel it is —
left or right — from its local UPnP/SOAP API. All requests are read-only:
nothing here ever plays, pauses, or changes volume.

### Kasa

Speaks the **legacy** TP-Link Kasa protocol on TCP 9999 — a JSON API
obfuscated (not encrypted) with a simple rolling-XOR cipher, no real
authentication. Sends exactly one request,
`{"system":{"get_sysinfo":{}}}` — read-only, with no actuation command
anywhere in the module.

**Important limitation:** newer Kasa firmware and all Tapo devices
(P100/P110/…) speak **KLAP** instead — an HTTP-based, cloud-credential
handshake on a different port — and simply won't answer on 9999 at all. When
that happens the probe reports it plainly (`protocol: klap_or_unknown`)
rather than pretending the device isn't there.

Also strips a handful of fields before ever persisting a result — geocoded
home-location coordinates and cloud account identifiers that Kasa's own
onboarding flow embeds in `get_sysinfo`'s response.

### SSDP

The generic fallback for anything the more specific probes didn't claim.
Sends a unicast M-SEARCH directly to the asset's own IP on port 1900 (not
the usual multicast broadcast — this network's VLANs don't carry multicast,
same reasoning as the Sonos household collector), then optionally follows the
device-reported `LOCATION` URL for a friendlyName/manufacturer/model.

That follow-up fetch is the one place a probe reaches for a URL a device
supplies rather than one this app already knows — see
[Security Model](Security-Model#probes-are-read-only-and-ssrf-hardened) for
the guards on it (host must match the probed IP, no redirects, capped body).

### ping

A single ICMP echo request, shelling out to the system `ping` binary (an
explicit, auditable subprocess call, same preference as the nmap collector) —
no elevated privileges needed on macOS. Answers a narrower, more general
question than the others: "is this thing even on right now?" A silent host is
*not* proof it's off — plenty of IoT gear and Wi-Fi clients in power-save mode
never answer ICMP, and a probe might simply be unable to reach a device on a
different VLAN.

## Adding a new probe

Write a module exposing a `PROBE` instance with `applies_to(asset, interfaces,
services)` (cheap, no network I/O) and `run(ip, timeout)` (the actual
read-only call — must never raise; catch everything and return a failed
`ProbeOutcome` instead), then add it to `IDENTIFICATION_PROBES` (or
`ALWAYS_PROBES`, if it's not identification, like `ping`) in
`probes/registry.py`. Nothing else needs to change.
