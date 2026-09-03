# Security Policy

## Scope and context

This is a self-hosted home asset- and vulnerability-management application. It
is designed to run on your own network, not to be exposed to the public
internet (see the README's deployment notes). The app itself listens on
loopback only, behind HTTP Basic auth, and is reached through a TLS proxy on
the same host (Tailscale Serve, or Caddy); it answers only for the hostnames
you configure. That deployment model is part of its security posture: several
controls assume the network boundary and the TLS layer are doing real work.

## Known, accepted limitations

Found by the project's own security audit and left as they are, on purpose --
reports about them are welcome if you think the reasoning is wrong:

- `/health` is unauthenticated and reports backup freshness. It is the deploy
  and liveness gate for `scripts/redeploy.sh` and launchd, which cannot
  present credentials; with the app on loopback it is not reachable from
  the network except through the proxy you configured.
- Discovery runs (`/discovery/run/*`) execute synchronously inside the
  request, and list pages have no pagination. A single admin user can tie
  up a worker for the length of an nmap scan; only that user can trigger it.
- The CSRF guard admits an unsafe-method request that carries none of
  `Sec-Fetch-Site`, `Origin` or `Referer` -- the shape of a bare API client.
  Every current browser sends `Sec-Fetch-Site` on a cross-site request, so
  the residual exposure is a browser old enough to send none of the three.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report privately through GitHub's [**Report a vulnerability**](https://github.com/rohitafish/home-asset-manager/security/policy)
button (under this repository's **Security** tab, if you're navigating there
directly), which opens a private security advisory visible only to the
maintainer. Include:

- what the issue is and where in the code it lives,
- how to reproduce it (a minimal case is ideal),
- the impact you think it has, and
- any suggested fix.

This is a personal, unfunded project maintained on a best-effort basis:

- **No bug bounty.** There is no monetary reward.
- **Acknowledgement** of a valid report within about a week.
- **Fixes** are prioritised by severity and by whether the issue is reachable in
  the intended LAN-only deployment versus only in an exposed or forked one. This
  distinction is triage information, not a dismissal — please report either way.

Please give a reasonable window to address an issue before disclosing it
publicly.

## Supported versions

Only the latest commit on the default branch is supported. There are no
long-lived release branches; fixes land on `main`.

## Handling of secrets and personal data

If a report involves a leaked credential or personal data in the code or git
history, say so explicitly and **do not quote the secret value** in the advisory
— point to the file and location instead. The repository ships
`scripts/check-pii.sh` as a pre-push guard against exactly this class of leak;
if you find a gap in it, that itself is worth reporting.
