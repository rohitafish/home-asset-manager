# Security Policy

## Scope and context

This is a self-hosted home asset- and vulnerability-management application. It
is designed to run on a trusted LAN behind HTTP Basic auth, not to be exposed to
the public internet (see the README's deployment notes). That deployment model
is part of its security posture: several controls assume the network boundary is
doing real work.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report privately through GitHub's **Report a vulnerability** button under this
repository's **Security** tab, which opens a private security advisory visible
only to the maintainer. Include:

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
