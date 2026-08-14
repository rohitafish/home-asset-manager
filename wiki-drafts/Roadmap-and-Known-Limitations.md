# Roadmap / Known Limitations

An honest account of what this deliberately doesn't do, and some ideas — not
commitments — for what might come next.

## Deliberately out of scope

This scales enterprise asset/vulnerability-management practice **down** to a
home network; it isn't a shrunk-down enterprise tool trying to grow back up.
Several limits are permanent design choices, not gaps waiting to be filled:

- **Tens of devices, not hundreds.** Built and tuned for a home-network
  scale. Nothing structurally forbids more, but performance and UX haven't
  been validated past that.
- **On-demand discovery only.** Nothing scans continuously in the background
  — every collector runs when you trigger it. No daemon quietly probing your
  network around the clock.
- **LAN-only, by design, not by omission.** No port is ever forwarded; the
  threat model in [Security Model](Security-Model) assumes this. Don't expect
  (or add) internet-facing hardening here — that's a different project.
- **macOS + UniFi required.** nmap needs raw-socket/L2 access Docker can't
  reliably give it on macOS, so the app and its collectors run natively — see
  [Architecture Deep-Dive](Architecture-Deep-Dive). Full device naming/VLAN/
  topology data specifically depends on UniFi.
- **Best-effort vulnerability matching, not authoritative.** Keyword search
  against nmap banners, not CPE-accurate CVE matching — see
  [CVE Matching & Valuation Methodology](CVE-Matching-and-Valuation-Methodology).
  Treat findings as a starting point, always.
- **A single shared admin credential, not multi-user.** One household, one
  login. No roles, no per-user audit trail.
- **New-for-old valuation, not a market-price feed.** A defensible estimate
  for keeping a sum-insured current, not a live pricing engine — see the
  methodology page.

## Ideas for future work

None of these are promised — they're directions that would fit the project's
existing shape if someone (possibly you) wants to build them:

- **More probe types** — Matter/Thread devices, Zigbee via a bridge's local
  API, other smart-plug families beyond legacy Kasa.
- **A safe demo/screenshot mode** — synthetic sample data so the app can be
  demoed or screenshotted without ever touching real household data (see the
  Wiki/site content plan's screenshot prerequisite).
- **An optional findings digest** — a periodic summary of new/changed
  vulnerability findings, still triggered manually or via a user-owned
  schedule, not a background daemon.
- **CPE-based CVE matching**, if a good lightweight open dataset mapping
  nmap-style banners to real CPEs turns up — would meaningfully improve
  precision over keyword search.
- **A read-only "guest view"** for a household member who shouldn't have
  admin access but might want to browse the inventory.

## Contributing an idea

Have a direction that isn't listed? Open an issue to discuss it before
sending a large PR, especially for anything touching the data model, adding a
new required dependency, or changing the security posture — see
[Contributing](Contributing).
