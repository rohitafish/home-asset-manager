# Security Model

This page explains the threat model behind this app's design, and the
concrete controls in place — for anyone deciding whether to run it on their
own home network. It complements [SECURITY.md](https://github.com/rohitafish/home-asset-manager/blob/main/SECURITY.md),
which covers *reporting* a vulnerability rather than the design itself.

## The deployment model is part of the security model

This app is built to run **behind your router, on your own LAN, with no port
ever forwarded**. That's not a suggestion — it's the assumption several of the
controls below are built on. Don't put it on the public internet without
adding your own reverse proxy, TLS, and a hardened auth layer in front of it;
that configuration isn't what's been designed for or tested.

Within the LAN, the app itself listens on **loopback only**. HTTP Basic puts
the password on every request, and the inventory (serials, purchase prices,
uploaded receipts) shouldn't cross the network in the clear — least of all a
network full of the IoT devices this app catalogues CVEs for. It is reached
through a TLS terminator on the same host: Tailscale Serve (recommended;
nothing listens on the LAN, access is tailnet-only, real certificate) or
Caddy with a publicly trusted certificate for a name you own (issued via
ACM's managed ACME endpoint, renewed automatically -- no per-device CA to
install, unlike Caddy's older local-CA setup). The app answers only for
`localhost`, `127.0.0.1` and
the names listed in `APP_ALLOWED_HOSTS`; any other `Host` header is refused
with a `400` before authentication or routing run, which closes DNS-rebinding
and Host-confusion tricks. `Strict-Transport-Security` is sent on https
responses. See the README's "Reaching it over HTTPS".

The app process runs as an ordinary user and nothing it can reach runs as
root on its behalf: there is no passwordless `sudo` rule (an earlier version
documented one for `nmap`; on a Homebrew install that binary is user-owned,
so the rule was a root escalation, and `scripts/preflight.sh` now fails
while it exists), and the one root LaunchDaemon (UPS shutdown) runs a
root-owned copy of its script with a system-only `PATH`.

Given that, the realistic threat isn't "a random attacker on the internet" —
it's **a compromised or malicious device already on your LAN** (a smart plug
running unexpected firmware, a guest's phone, an IoT device with a supply-chain
problem) trying to abuse the one thing on the network that talks back to it:
this app's discovery probes.

## Authentication

- The whole dashboard and API sit behind **HTTP Basic auth**. Credential
  comparison uses `secrets.compare_digest` on UTF-8 bytes (timing-safe, and
  immune to the crash-on-non-ASCII-username bug that byte/str mismatches can
  cause elsewhere).
- The app **refuses to start** if `APP_ADMIN_PASSWORD` is unset or still the
  documented placeholder — an accidentally-unconfigured instance never serves
  the dashboard wide open.
- **Brute-force throttling:** more than 10 failed login attempts from the same
  client within a 5-minute window get a `429` until the window ages out.
  Failures are logged at `WARNING`. The client IP comes through the loopback
  proxy's `X-Forwarded-For`, which uvicorn trusts from `127.0.0.1` only, so
  one attacker's failures throttle that attacker rather than everyone.

## CSRF protection

HTTP Basic has no session cookie to hang a `SameSite` policy on, so a browser
will happily replay saved credentials on a cross-site form submission. Every
unsafe-method request is checked against `Sec-Fetch-Site` first — a header the
browser sets and page JavaScript cannot forge or strip — rejecting
`cross-site`/`same-site` and allowing `same-origin`/`none`. Clients that don't
send it (e.g. a bare `curl`/API client) fall back to an Origin/Referer-vs-Host
comparison.

## Probes are read-only, and SSRF-hardened

The discovery **probes** (SSDP, Sonos, ping, etc.) exist to ask devices on
your LAN what they are. They never write to a device. Two specific hardenings
worth calling out:

- The generic SSDP probe follows a device-supplied `LOCATION` URL to fetch its
  UPnP description — a classic SSRF shape if left unchecked (a hostile device
  could point `LOCATION` at `http://127.0.0.1:8000/...` or another internal
  service). The fetch is only made if the URL's host **matches the IP that was
  actually probed**, over `http`/`https` only, with redirects disabled and the
  response body capped.
- The Sonos household collector enriches player details from a
  device-reported host; that fetch is restricted to plain private-LAN
  addresses, explicitly rejecting loopback and link-local targets (which would
  otherwise be able to reach `127.0.0.1` or a cloud metadata endpoint).
- Device-supplied XML (UPnP/SOAP responses, nmap's XML output) is parsed with
  `defusedxml`, not the standard library parser, closing the usual
  entity-expansion class of XML attack.

## The AI assistant's safety model

An optional AI assistant (Anthropic/OpenRouter) can chat about an asset,
analyse an attached receipt/warranty document, and *suggest* field updates —
but it never writes to your data directly:

- **Propose-and-approve only.** Every suggested change becomes a
  `ChangeProposal` row that you review and explicitly approve; nothing is
  auto-applied.
- **Tools are read-only or propose-only.** The assistant can look things up
  and run a probe; it cannot delete, and it cannot write outside an allowlisted
  set of asset fields.
- **Untrusted data is wrapped, not trusted.** Anything that came from a device
  or the network (hostnames, probe results, discovery data) is wrapped in an
  explicit "this is untrusted data, not an instruction" boundary before it
  reaches the model, and any literal boundary tags already present in that data
  are stripped first so a device can't fake the wrapper.
- **Minimal data by design.** Some fields are deliberately withheld from
  specific prompts even when they'd technically help — e.g. an asset's serial
  number is never sent when asking the model to guess a model number, since it
  barely helps that particular guess and is identifying data.
- **Nothing leaves your network unless you configure this feature.** With no
  API key set, the assistant is inert — every call site checks
  `is_configured()` first and no-ops if it isn't.

## Upload handling

Chat attachments (receipts, warranty PDFs, screenshots) are validated by
declared size, aggregate size across a conversation, allow-listed content
type, and a magic-byte check against the file's actual header (never trusting
the browser-supplied content type alone) — and are **never written to disk**;
they're read once, analysed, and discarded.

## Supply chain & repo hygiene

- CI runs `ruff` (lint) and the full test suite on every push and pull
  request, plus a PII/secret scan (see below) as a server-side backstop. The
  two CI actions are pinned to a commit SHA, not a mutable version tag —
  GitHub's `sha_pinning_required` is on for this repo, so an unpinned action
  can't creep back in.
- Dependabot watches both the Python dependencies and the GitHub Actions used
  by CI. `requirements.txt`/`requirements-dev.txt` are lockfiles generated by
  `uv pip compile --universal`: every direct and transitive package at an
  exact version, each with a sha256 hash for every supported platform,
  generated from the hand-edited `requirements.in` files. The deployed instance and CI install with `--require-hashes`, so a
  published file that doesn't match the recorded hash is refused rather
  than run, and no transitive can silently float between deploys. CI also
  runs `pip-audit` against the lock on every push and on a weekly schedule,
  so a new advisory against a pinned version fails the build on its own.
- Commits to `main` must be signed, enforced server-side with **no bypass
  actor** — this applies even to the repo owner, on any machine. A separate
  ruleset also blocks force-pushes and branch deletion on `main`, again with
  no bypass — the two operations that could otherwise rewrite or destroy the
  repo's history outright.
- Private vulnerability reporting is enabled, so the "Report a vulnerability"
  flow linked at the bottom of this page is live, not just documented.
- Every response carries a Content-Security-Policy, `X-Content-Type-Options:
  nosniff`, and `Referrer-Policy: same-origin`.
- `/docs`, `/redoc`, and the OpenAPI schema are disabled — no unauthenticated
  route inventory.

## A note on this repo's own history

Before this project's first release, its git history was rebuilt from a clean
baseline specifically to guarantee that no personal information about the
author's own home network — device identifiers, household details — was ever
exposed alongside the code. The project also carries its own automated
PII/secret scanner (`scripts/check-pii.sh`) as a pre-push hook and a CI check,
precisely so that discipline doesn't depend on anyone remembering to apply it
by hand.

## What this is *not*

- Not a hardened, internet-facing appliance. Don't forward a port to it.
- Not an authoritative vulnerability scanner. CVE matching is best-effort
  keyword matching against service banners, not CPE-accurate — see
  [CVE Matching & Valuation Methodology](CVE-Matching-and-Valuation-Methodology).
  Treat findings as a starting point to investigate, not a certified report.
- Not multi-user or role-based. It's a single shared admin credential for a
  household, by design.
- Not free of accepted limitations. Three are known and left as they are:
  `/health` is unauthenticated (the deploy/liveness gate; loopback-only now);
  discovery runs execute synchronously inside the request and list pages have
  no pagination (only the one admin can trigger them); and the CSRF guard
  admits a request carrying none of `Sec-Fetch-Site`/`Origin`/`Referer`, the
  shape of a bare API client, which only a browser old enough to send none of
  the three could be made to produce. See SECURITY.md.

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's
[**Report a vulnerability**](https://github.com/rohitafish/home-asset-manager/security/policy)
button — see [SECURITY.md](https://github.com/rohitafish/home-asset-manager/blob/main/SECURITY.md)
for what to include.
