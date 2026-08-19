# Configuration Reference

Every setting lives in `.env` at the repo root (copied from `.env.example` and
filled in — see the README's
[One-time setup](https://github.com/rohitafish/home-asset-manager#one-time-setup)).
`.env` is gitignored and never leaves your machine; `scripts/redeploy.sh`
deliberately never syncs it to a second machine either, so each deployment
configures its own.

This page is a reference for what each variable does and what happens if it's
missing or wrong. It won't drift from `.env.example` unexpectedly — but if the
two ever disagree, trust `.env.example` in the repo and treat this page as due
for an update.

## Database

| Variable | Default | Required |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://assetmgt:assetmgt@localhost:5432/assetmgt` | No — the default matches `docker-compose.yml` |

Only change this if you're not using the bundled Postgres container. **If
wrong:** the app fails to start, or every DB-touching request errors.

## App authentication (required)

| Variable | Default | Required |
|---|---|---|
| `APP_ADMIN_USER` | `admin` | No |
| `APP_ADMIN_PASSWORD` | *(none — placeholder `change-me` in the template)* | **Yes** |

HTTP Basic auth guards the entire dashboard and API — see
[Security Model](Security-Model). **If `APP_ADMIN_PASSWORD` is left unset or
still `change-me`, the app refuses to start at all**, rather than silently
serving an unauthenticated dashboard.

## Logging

| Variable | Default | Required |
|---|---|---|
| `LOG_LEVEL` | `INFO` | No |

Set `DEBUG` to see `httpx`/app detail and per-seed probe misses; `WARNING` to
quiet routine access logs. Logs go to stdout/stderr; launchd captures them
into `logs/app.log` and `logs/app.error.log` (`WARNING`+) — nothing here
writes a log file directly.

## UniFi

| Variable | Default | Required |
|---|---|---|
| `UNIFI_BASE_URL` | `https://192.168.1.1` (example) | Yes, for UniFi discovery |
| `UNIFI_API_KEY` | *(empty)* | Yes, for UniFi discovery |
| `UNIFI_SITE` | `default` | No |
| `UNIFI_VERIFY_TLS` | `true` | No |

Points at a local UniFi Network controller or a UDM/Cloud Gateway console's
Integration API. **If `UNIFI_API_KEY` is missing:** UniFi discovery fails
(the rest of the app still works). `UNIFI_VERIFY_TLS` defaults to on — the
API key is a bearer credential on a LAN shared with untrusted devices, so an
unverified connection is MITM-able. Set it to `false` only for a self-signed
UDM/controller you trust on your own LAN.

## Network scan

| Variable | Default | Required |
|---|---|---|
| `SCAN_SUBNETS` | `192.168.1.0/24` (example) | Yes, for nmap discovery |

Comma-separated CIDRs nmap will ping-sweep and service-scan. **If wrong:**
nmap discovery finds nothing, or scans a network it shouldn't.

## Vulnerability enrichment

| Variable | Default | Required |
|---|---|---|
| `NVD_API_KEY` | *(empty)* | No |

Optional, but raises the NVD API's rate limit substantially — enrichment
against many services is slow/rate-limited without one.

## AI assistant (optional)

| Variable | Default | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(empty)* | No |
| `ANTHROPIC_MODEL` | `claude-opus-5` | No |
| `OPENROUTER_API_KEY` | *(empty)* | No |

Powers the per-asset "investigation assistant" chat panel — see
[The LLM Assistant, Explained](The-LLM-Assistant-Explained). **If both keys
are unset**, the chat panel just shows a "not configured" note; nothing else
in the app is affected. `OPENROUTER_API_KEY` is a fallback credential, used
**only** when `ANTHROPIC_API_KEY` is unset, routing the same calls through
OpenRouter's Anthropic-compatible endpoint instead of `api.anthropic.com`.
`ANTHROPIC_MODEL` applies to either path.

## Default ownership

| Variable | Default | Required |
|---|---|---|
| `DEFAULT_OWNER` | `Owner` (example) | No, but worth setting |
| `SECONDARY_OWNER_NAME` | *(empty)* | No |
| `SECONDARY_OWNER_HOSTNAME_KEYWORD` | *(empty)* | No |

Every newly-discovered asset gets `DEFAULT_OWNER`. Optionally, a second
household member can be auto-assigned instead when their name/keyword shows
up in a device's hostname (e.g. devices they've personally named). Leave both
`SECONDARY_*` fields blank to skip that and always use `DEFAULT_OWNER`.

## Off-site database backups (optional)

| Variable | Default | Required |
|---|---|---|
| `BACKUP_S3_BUCKET` | *(empty)* | No — only for `scripts/backup-db.sh` |
| `BACKUP_AWS_ACCESS_KEY_ID` | *(empty)* | No |
| `BACKUP_AWS_SECRET_ACCESS_KEY` | *(empty)* | No |
| `BACKUP_AWS_REGION` | *(empty)* | No |

These are expected to belong to an existing, full-S3-access IAM identity by
deliberate choice — not a dedicated least-privilege one — with backup history
protected by **S3 Object Lock** on the bucket itself rather than by a
restrictive IAM policy. See [Backup & Disaster Recovery](Backup-and-Disaster-Recovery)
for the full design (storage class, retention, encryption, schedule), or the
README's "Off-site database backups" section for the setup and restore steps.

> **Not read from `.env`:** `BACKUP_KEEP_LOCAL` (local dump retention count)
> is a `backup-db.sh` shell variable read from the *process* environment —
> under launchd that's just `PATH`, so setting it in `.env` is a silent
> no-op. Override it in `com.assetmgt.backup.plist` instead if you ever need
> to change it.

## If something's misconfigured

Most misconfiguration here fails **soft** — a missing UniFi key means no
UniFi discovery, not a crash; a missing AI key means no chat panel. The one
**hard** failure is `APP_ADMIN_PASSWORD`: unset or left as the placeholder,
and the app won't start at all. See
[Troubleshooting / FAQ](Troubleshooting-FAQ) for specific symptoms.
