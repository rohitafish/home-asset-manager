# Contributing

This wiki page is a short pointer, not a duplicate — the real, maintained
documents live in the repo itself so they stay versioned with the code:

- **[CONTRIBUTING.md](https://github.com/rohitafish/home-asset-manager/blob/main/CONTRIBUTING.md)**
  — setup, running tests/lint, and (read this one first) the privacy workflow
  every contributor needs to know before their first commit: how the
  PII/secret guard works, why real device details must never be used as
  examples, and how to install the pre-commit and pre-push hooks.
- **[SECURITY.md](https://github.com/rohitafish/home-asset-manager/blob/main/SECURITY.md)**
  — how to report a vulnerability privately. **Please don't open a public
  issue for one.**

## What's especially welcome

- Bug reports with a clear reproduction — even better with what
  `./scripts/preflight.sh` reported.
- A new [probe](Probes-Reference) for a device family not yet covered —
  the pattern is small and self-contained by design (see that page's "Adding
  a new probe" section).
- Documentation fixes and additions, here or in the README.
- Test coverage for an existing gap.

## What's worth an issue first

Open an issue to discuss before sending a large PR for:

- Changes to the data model (`app/models.py` + a migration).
- A new required dependency.
- Anything touching authentication, CSRF, or the probe/assistant security
  boundaries described in [Security Model](Security-Model) — not because
  it's unwelcome, just because the reasoning behind the current design is
  worth working through together first.

## Every PR runs the same gate you do locally

CI runs `ruff`, the full test suite, the PII/secret scanner
(`scripts/check-pii.sh`) and a gitleaks history scan on every push and pull
request — the same checks the pre-commit and pre-push hooks run on your own
machine, as a server-side backstop — plus a monthly TruffleHog sweep that
verifies any credential-shaped string against its provider. Two layers, on
purpose: gitleaks knows what an API key looks like; only `check-pii.sh`, with
its gitignored list of this household's real identifiers, can tell a real MAC
address or serial from a fabricated one. Use fabricated values in tests and
examples (`00:1a:2b:3c:4d:xx` MACs, `C02FAKE...` serials), never real ones.
See [Security Model](Security-Model#supply-chain--repo-hygiene).

The hooks are meant to be cheap enough that nobody reaches for
`--no-verify`: the pre-commit scan of the index takes under a second, a
typical push a few seconds, and a full-history audit
(`scripts/check-pii.sh --full`) about twenty seconds. Pushing a new branch
scans only the commits the remote doesn't have yet, not the whole history.
