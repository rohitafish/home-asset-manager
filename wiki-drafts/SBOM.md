# Software Bill of Materials

Every component this app is actually built on, and what it's for — not a
generic "what's a nice library" list, but grounded in how each one is used
in *this* codebase specifically. Written for a human to read; not a
machine-readable SPDX/CycloneDX document.

The real, versioned source of truth is `requirements.txt`/
`requirements-dev.txt` in the repo (exact `==` pins — see the header
comment there for why, and [Configuration Reference](Configuration-Reference)
for the same "if this page and the repo disagree, trust the repo" caveat).
This page explains what each pin is *for*.

## Language runtime

| Component | Notes |
|---|---|
| **Python 3.12+** | The Mini (the deployed instance) runs 3.12 — the floor `ruff.toml` targets and CI tests against. The dev machine runs a newer 3.x; both are supported, see `AGENTS.md`'s "Tests" section. |

## Python packages (runtime — `requirements.txt`)

| Component | Version | What it's for, here |
|---|---|---|
| **fastapi** | 0.141.1 | The web framework the whole app is built on — every route in `app/routers/`, and the dependency-injection mechanism `require_admin`/`require_same_origin` (`app/auth.py`) hook into. |
| **uvicorn**[standard] | 0.52.3 | The ASGI server that actually runs the app as an OS process — the literal command in `com.assetmgt.app.plist`'s `ProgramArguments`, which launchd starts and supervises. `[standard]` pulls in `uvloop`/`httptools` for a faster event loop and HTTP parser. |
| **sqlmodel** | 0.0.39 | Combines SQLAlchemy (the SQL toolkit) with Pydantic validation in one model definition — every table in `app/models.py` (`Asset`, `Finding`, `DiscoveryRun`, ...) is a SQLModel class. |
| **alembic** | 1.19.1 | Database migrations. Every schema change is a file under `migrations/versions/`, applied via `alembic upgrade head` — part of every `redeploy.sh` run. |
| **psycopg**[binary] | 3.3.4 | The PostgreSQL driver SQLAlchemy talks through. `[binary]` bundles a precompiled `libpq`, so there's no separate system Postgres client library to install. |
| **python-dotenv** | 1.2.2 | Loads `.env` into the process environment at startup. |
| **jinja2** | 3.1.6 | The HTML template engine behind every page in `app/templates/`, and the in-app README view (`app/readme_render.py`) that renders this repo's own `README.md` live. |
| **python-multipart** | 0.0.32 | Required by FastAPI to parse multipart form submissions — every plain HTML form POST (asset edit, notes, discovery triggers) and the chat panel's file uploads depend on it. |
| **httpx** | 0.28.1 | The HTTP client behind every outbound call this app makes: UniFi's API (`discovery/unifi_client.py`), a Sonos player's local UPnP/SOAP API (`probes/sonos_api.py`), the CVE/KEV/EPSS feeds (`discovery/cve_enrich.py`), and the Anthropic/OpenRouter API (`app/assistant.py`). |
| **typer** | 0.27.1 | The CLI framework behind `discovery/cli.py` (`python -m discovery.cli ...`) — how discovery collectors run standalone or from cron, outside the web UI. |
| **markdown** | 3.10.3 | Renders `README.md` to HTML for the in-app `/readme` route. |
| **anthropic** | 0.122.0 | The official Anthropic API client, used by the optional investigation assistant (`app/assistant.py`) — and for the OpenRouter fallback too, since OpenRouter exposes an Anthropic-compatible endpoint. |
| **defusedxml** | 0.7.1 | Safe parsing for anything device-supplied: nmap's `-oX` output, a Sonos player's UPnP/SOAP responses. Closes the standard XML entity-expansion class of attack the stdlib parser doesn't guard against by default — see [Security Model](Security-Model). |

## Frontend

| Component | Version | Notes |
|---|---|---|
| **htmx** | 2.0.3 | The *only* client-side JavaScript library in the app, and vendored locally rather than loaded from a CDN — `app/templates/base.html` documents it as verified byte-for-byte against the official release. Used sparingly (e.g. polling the asset-count badge every 5s); most of the app is plain server-rendered HTML forms, not a JS-heavy SPA. |

## Database

| Component | Version | Notes |
|---|---|---|
| **PostgreSQL** | 16 (`postgres:16-alpine`) | The actual datastore, run via `docker-compose.yml` inside Colima's VM. The live household inventory lives here, backed up nightly to S3 (`scripts/backup-db.sh`). |

## System tools (Homebrew — not Python packages, see the README's "One-time setup")

| Component | Notes |
|---|---|
| **Colima** | Runs the Docker daemon inside a lightweight Linux VM on macOS — the free, open-source alternative to Docker Desktop this project uses. Hosts the Postgres container. The one Homebrew formula this project pins (`brew pin colima`) — an upgrade has previously forced a destructive VM recreation; see `AGENTS.md`'s "Deployment topology". |
| **Docker / Docker Compose** | Runs and manages the Postgres container per `docker-compose.yml`. |
| **nmap** | The actual network scanner behind discovery's port/service scanning (`discovery/nmap_scan.py`) — invoked as a subprocess; its XML output is parsed with `defusedxml`, above. |
| **AWS CLI** | Invoked by `scripts/backup-db.sh` to upload the nightly Postgres dump to S3, under Object Lock. |

Versions for this group aren't pinned in the repo the way Python packages
are (Homebrew-managed, upgraded independently) — Colima is the one
exception, pinned specifically because of the risk above.

## External services (optional — nothing is sent unless configured)

| Component | Notes |
|---|---|
| **Anthropic API**, or **OpenRouter** as a compatible fallback | Powers the optional investigation assistant chat. Inert with no API key set — every call site checks `is_configured()` first. See [Security Model](Security-Model)'s "nothing leaves your network unless you configure this feature". |

## Development-only tooling (`requirements-dev.txt` — not installed on the deployed instance)

| Component | Version | Notes |
|---|---|---|
| **pytest** | 9.1.1 | The test suite under `tests/`, run by the pre-push hook and `preflight.sh`. |
| **ruff** | 0.16.3 | Linter — `ruff.toml` pins the exact rule set. Also run by the pre-push hook and CI. |

`scripts/redeploy.sh` only installs `requirements.txt`, so neither of these
ships on the deployed instance — see `AGENTS.md`'s "Tests" section.
