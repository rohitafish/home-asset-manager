# Software Bill of Materials

Every component this app is actually built on, and what it's for — not a
generic "what's a nice library" list, but grounded in how each one is used
in *this* codebase specifically. Written for a human to read; not a
machine-readable SPDX/CycloneDX document.

The real, versioned source of truth is `requirements.in`/
`requirements-dev.in` in the repo (the direct dependencies, exact `==`
pins) and the `requirements.txt`/`requirements-dev.txt` lockfiles generated
from them by `uv pip compile --universal` — every transitive package too,
hashed for every platform, each with a sha256
hash, installed with `--require-hashes` on the deployed instance and in CI.
See [Configuration Reference](Configuration-Reference) for the same "if this
page and the repo disagree, trust the repo" caveat. This page explains what
each direct pin is *for*; the lockfile is where to look for the full
transitive list.

## Language runtime

| Component | Notes |
|---|---|
| **Python 3.12+** | The deployed instance runs 3.12 — the floor `ruff.toml` targets and CI tests against. The dev machine runs a newer 3.x; both are supported, see `AGENTS.md`'s "Tests" section. |

## Python packages (runtime — `requirements.txt`)

| Component | Version | What it's for, here |
|---|---|---|
| **fastapi** | 0.141.1 | The web framework the whole app is built on — every route in `app/routers/`, and the dependency-injection mechanism `require_admin`/`require_same_origin` (`app/auth.py`) hook into. |
| **uvicorn**[standard] | 0.52.4 | The ASGI server that actually runs the app as an OS process — the literal command in `assetmgt-app.service`'s `ExecStart` (systemd) or `com.assetmgt.app.plist`'s `ProgramArguments` (launchd), which the service manager starts and supervises. `[standard]` pulls in `uvloop`/`httptools` for a faster event loop and HTTP parser. |
| **sqlmodel** | 0.0.39 | Combines SQLAlchemy (the SQL toolkit) with Pydantic validation in one model definition — every table in `app/models.py` (`Asset`, `Finding`, `DiscoveryRun`, ...) is a SQLModel class. |
| **alembic** | 1.19.1 | Database migrations. Every schema change is a file under `migrations/versions/`, applied via `alembic upgrade head` — part of every `redeploy.sh` run. |
| **psycopg**[binary] | 3.3.4 | The PostgreSQL driver SQLAlchemy talks through. `[binary]` bundles a precompiled `libpq`, so there's no separate system Postgres client library to install. |
| **python-dotenv** | 1.2.3 | Loads `.env` into the process environment at startup. |
| **jinja2** | 3.1.6 | The HTML template engine behind every page in `app/templates/`, and the in-app README view (`app/readme_render.py`) that renders this repo's own `README.md` live. |
| **python-multipart** | 0.0.32 | Required by FastAPI to parse multipart form submissions — every plain HTML form POST (asset edit, notes, discovery triggers) and the chat panel's file uploads depend on it. |
| **httpx** | 0.28.1 | The HTTP client behind every other outbound call this app makes: UniFi's API (`discovery/unifi_client.py`), a Sonos player's local UPnP/SOAP API (`probes/sonos_api.py`), and the CVE/KEV/EPSS feeds (`discovery/cve_enrich.py`). No longer used for the Anthropic/OpenRouter API — see **anthropic** below. |
| **typer** | 0.27.1 | The CLI framework behind `discovery/cli.py` (`python -m discovery.cli ...`) — how discovery collectors run standalone or from cron, outside the web UI. |
| **markdown** | 3.10.3 | Renders `README.md` to HTML for the in-app `/readme` route. |
| **nh3** | 0.3.7 | Sanitises that rendered HTML (an allowlist of tags/attributes; drops `<script>`, event handlers, `javascript:` URLs) before the template marks it `\|safe`. Python-Markdown passes raw HTML through untouched, and the README is the one `\|safe` output in the app. |
| **anthropic** | 1.0.0 | The official Anthropic API client, used by the optional investigation assistant (`app/assistant.py`) — and for the OpenRouter fallback too, since OpenRouter exposes an Anthropic-compatible endpoint. As of 1.0.0 it brings its own HTTP transport, **httpx2** (a separate package from **httpx** above, pulled in transitively — not pinned directly in `requirements.txt`), so this is the one outbound call in the app that no longer rides `httpx`. |
| **defusedxml** | 0.7.1 | Safe parsing for anything device-supplied: nmap's `-oX` output, a Sonos player's UPnP/SOAP responses. Closes the standard XML entity-expansion class of attack the stdlib parser doesn't guard against by default — see [Security Model](Security-Model). |

## Frontend

| Component | Version | Notes |
|---|---|---|
| **htmx** | 2.0.3 | The *only* client-side JavaScript library in the app, and vendored locally rather than loaded from a CDN — `app/templates/base.html` documents it as verified byte-for-byte against the official release. Used sparingly (e.g. polling the asset-count badge every 5s); most of the app is plain server-rendered HTML forms, not a JS-heavy SPA. |

## Database

| Component | Version | Notes |
|---|---|---|
| **PostgreSQL** | 16 (`postgres:16-alpine`) | The actual datastore, run via `docker-compose.yml` (native `dockerd` on the Linux host; inside Colima's VM on a Mac). The live household inventory lives here, backed up nightly to S3 (`scripts/backup-db.sh`). |

## System tools (apt/pipx on the Linux host, Homebrew on a Mac — not Python packages; see the README's "Installing on Linux" and "One-time setup")

| Component | Notes |
|---|---|
| **Colima** | macOS only. Runs the Docker daemon inside a lightweight Linux VM — the free, open-source alternative to Docker Desktop this project uses there. Hosts the Postgres container; on Linux, `docker.io`'s native daemon does that with no VM. The one Homebrew formula this project pins (`brew pin colima`) — an upgrade has previously forced a destructive VM recreation; see `AGENTS.md`'s "Deployment topology". |
| **Docker / Docker Compose** | Runs and manages the Postgres container per `docker-compose.yml`. |
| **nmap** | The actual network scanner behind discovery's port/service scanning (`discovery/nmap_scan.py`) — invoked as a subprocess; its XML output is parsed with `defusedxml`, above. |
| **AWS CLI** | Invoked by `scripts/backup-db.sh` to upload the nightly Postgres dump to S3, under Object Lock. |
| **Caddy** | The TLS-terminating reverse proxy in front of the app on the deployed host, serving `fullchain.pem`/`privkey.pem` from certbot and proxying to uvicorn on loopback (`scripts/Caddyfile.example`). Optional — the Tailscale Serve path doesn't use it — but where it is used it terminates TLS for the entire dashboard, so it belongs in any security-relevant inventory. Stock build (apt on Linux, Homebrew on a Mac): the ACM ACME issuance flow needs no DNS-provider module, so no `xcaddy` custom build. On Linux it runs as its own `caddy` user and reads the pair from `/etc/caddy/certs/`, placed there by `scripts/certbot-deploy-hook.sh` on each renewal. |
| **certbot** | Obtains and renews that certificate against ACM's managed ACME endpoint, run daily by `assetmgt-certrenew.timer` (systemd) or `com.assetmgt.certrenew` (launchd). Holds the ACME **account key** under `~/.certbot/config/accounts`, which is what authorises renewal requests for the domain — treat it like any other credential on that host. See [Security Model](Security-Model). |

Versions for this group aren't pinned in the repo the way Python packages
are (package-manager-managed, upgraded independently) — Colima is the one
exception on a Mac, pinned specifically because of the risk above.

## External services (optional — nothing is sent unless configured)

| Component | Notes |
|---|---|
| **Anthropic API**, or **OpenRouter** as a compatible fallback | Powers the optional investigation assistant chat. Inert with no API key set — every call site checks `is_configured()` first. See [Security Model](Security-Model)'s "nothing leaves your network unless you configure this feature". |
| **Amazon S3** | Where the nightly database dump goes. This is the one place a complete copy of the inventory leaves the network by design, so it is the most consequential entry in this table — the bucket is versioned with Object Lock, and the credential that writes it cannot delete (see [Backup & Disaster Recovery](Backup-and-Disaster-Recovery)). Inert with the `BACKUP_*` keys unset. |
| **AWS Certificate Manager** (ACME endpoint) | Issues the 45-day certificate for the dashboard's public hostname. Only reached during issuance and renewal, and only if you use the Caddy path above. |
| **Amazon Route 53** | Hosts the DNS zone for that hostname. Nothing on the deploy host talks to it — the record is managed from the dev machine, and issuance is pre-approved so renewals need no DNS write. |

## Development-only tooling (`requirements-dev.txt` — not installed on the deployed instance)

| Component | Version | Notes |
|---|---|---|
| **pytest** | 9.1.1 | The test suite under `tests/`, run by the pre-push hook and `preflight.sh`. |
| **ruff** | 0.16.4 | Linter — `ruff.toml` pins the exact rule set. Also run by the pre-push hook and CI. |

`scripts/redeploy.sh` only installs `requirements.txt`, so neither of these
ships on the deployed instance — see `AGENTS.md`'s "Tests" section.

Repository-hygiene tooling, not Python packages and not on the deployed
instance either: **gitleaks** (`brew install gitleaks`; the pre-commit and
pre-push hooks fail closed without it, and the `secrets` CI job runs it over
the full history via a SHA-pinned action) and **TruffleHog** (a monthly CI
workflow, `--only-verified`, SHA-pinned). Configuration in `.gitleaks.toml`.
