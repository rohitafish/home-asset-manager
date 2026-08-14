# Contributing

Thanks for your interest. This is a personal, self-hosted project, but issues
and pull requests are welcome. A few things are specific to this repo — the
privacy guard in particular — so please read this before your first commit.

`AGENTS.md` is the authoritative, in-depth guide to deployment, git workflow,
and the privacy rules. This file is the short version.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env        # then fill in real values (see "One-time setup" in the README)
```

The app refuses to boot with an unset or placeholder `APP_ADMIN_PASSWORD`, so
set one before running it.

## Privacy: read this first

Real personal data has leaked into this repo's history before (household names,
a real hostname and LAN IP, a real device MAC used as an "illustrative
example"). Preventing a repeat is a hard requirement, and it is partly
automated:

- **`scripts/check-pii.sh`** scans the commits you're about to push — and,
  with `--full`, all of history — for known real values, structural PII
  (emails, GPS coordinates, non-private IPs, SSN-like numbers), and leaked
  credentials. It scans both file contents **and commit messages**, and
  normalises MAC/hex values so a device id matches regardless of formatting.
- **`.pii-denylist`** (repo root, gitignored, per-machine) is where the exact
  real values live — one literal string per line. It is the *only* sanctioned
  place for them, same as `.env` for secrets. It is never committed. A fresh
  clone starts without it; the structural and secret checks still run.
- **Install the pre-push hook once per clone** (git does not clone hooks):

  ```bash
  cp scripts/hooks/pre-push .git/hooks/pre-push
  chmod +x .git/hooks/pre-push
  ```

  It blocks any push that trips `check-pii.sh` (and runs the tests + linter).

### The one rule the tooling can't enforce

**Never use a real household detail as an example** — not in code, comments,
test fixtures, or commit messages. A brand-new real name isn't on any denylist
yet and is indistinguishable from any other word, so no check can catch it. Use
obviously fabricated values (`aa:bb:cc:...`, `RINCON_AABBCCDDEEFF...`, invented
names). If you do learn of a real value that must never reappear, add it to your
local `.pii-denylist`.

## Tests and linting

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```

Both must pass; the pre-push hook runs them too. Please add or update tests for
behaviour you change — the suite is thorough, including a dedicated
`tests/test_check_pii.py` for the privacy guard itself.

## Pull requests

- Keep commits small and focused, with a clear message (remember: **messages are
  scanned and become public** — no real personal data in them).
- Describe what changed and why, and how you verified it.
- If you touch security-relevant code (`app/auth.py`, the probes, anything that
  handles device-supplied data), call that out so it gets a closer look.

For security vulnerabilities, do **not** open a public issue — see
[SECURITY.md](SECURITY.md).
