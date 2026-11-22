"""End-of-life lookup for the software inventory, from endoflife.date.

For every SoftwareItem whose key maps to an endoflife.date product, finds the
release cycle each installed version belongs to and records on the install:
the cycle's end-of-life date, its newest release, and that release's date.
When the installed version *is* that newest release (or the cycle's first),
and the collector had no publication date for it -- Windows programs, snaps,
Store apps -- the release date fills that gap.

Only the product list and each matched product's cycles are fetched: one
request for the list plus one per matched product, capped by MAX_PRODUCTS.
Nothing about this inventory is sent -- the requests carry product names that
are endoflife.date's own.

  API: https://endoflife.date/api/all.json, https://endoflife.date/api/<product>.json
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date

import httpx
from sqlmodel import Session, select

from app.models import SoftwareInstall, SoftwareItem
from app.software_risk import EOL_UNKNOWN_DATE, upstream_version, version_tuple

logger = logging.getLogger(__name__)

EOL_API = "https://endoflife.date/api"
MAX_PRODUCTS = 200
REQUEST_PACING_SECONDS = 0.2
USER_AGENT = "home-asset-manager software inventory"

# Keys (software_import.software_key) whose endoflife.date slug differs.
PRODUCT_ALIASES = {
    "google chrome": "chrome",
    "chromium": "chrome",
    "node": "nodejs",
    "firefox esr": "firefox",
    "docker": "docker-engine",
    "visual studio code": "visual-studio-code",
    "microsoft edge": "microsoft-edge",
    "powershell preview": "powershell",
    "apache2": "apache-http-server",
    "openjdk": "oracle-jdk",
    "postgresql": "postgresql",
    "mariadb-server": "mariadb",
    "mysql-server": "mysql",
    "redis-server": "redis",
    "golang": "go",
    "thunderbird": "thunderbird",
}

# endoflife.date products that are operating systems or hardware: the OS is
# graded on its asset, not here, and a package that merely shares the name
# must not inherit its dates.
EXCLUDED_PRODUCTS = {
    "ubuntu", "debian", "linuxmint", "macos", "windows", "windows-server",
    "linux", "kali", "ios", "ipados", "android", "fedora", "centos", "rhel",
    "almalinux", "rocky-linux", "alpine-linux", "opensuse", "sles",
    "iphone", "ipad", "mac", "pixel", "samsung-mobile",
}


def candidate_products(key: str, products: set[str]) -> list[str]:
    """endoflife.date slugs to try for an item key, best first."""
    out = []
    if key in PRODUCT_ALIASES:
        out.append(PRODUCT_ALIASES[key])
    out.append(key.replace(" ", "-"))
    # "python 3.12", "postgresql-16", "openssl 3" -> "python", "postgresql", "openssl"
    m = re.match(r"^([a-z][a-z.+-]*?)[\s-]?\d+(?:\.\d+)*$", key)
    if m:
        base = m.group(1).rstrip("-.")
        out.append(PRODUCT_ALIASES.get(base, base))
    seen = []
    for slug in out:
        if slug in products and slug not in EXCLUDED_PRODUCTS and slug not in seen:
            seen.append(slug)
    return seen


def _as_date(value) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def match_cycle(version: str | None, cycles: list[dict]) -> dict | None:
    """The release cycle a version belongs to: the longest cycle name that
    equals the version or is a dotted prefix of it ("3.12" for "3.12.4")."""
    if not version:
        return None
    best = None
    for c in cycles:
        name = str(c.get("cycle", ""))
        if not name:
            continue
        matches = version == name or version.startswith(name + ".")
        if matches and (best is None or len(name) > len(str(best["cycle"]))):
            best = c
    return best


def apply_cycle(inst: SoftwareInstall, cycle: dict | None) -> bool:
    """Writes one cycle's facts onto an install. Returns True if anything changed."""
    before = (inst.eol_cycle, inst.eol_date, inst.upstream_latest, inst.upstream_latest_date,
              inst.published_date, inst.published_source)
    if cycle is None:
        inst.eol_cycle = inst.eol_date = inst.upstream_latest = inst.upstream_latest_date = None
        if (inst.published_source or "").startswith("endoflife.date"):
            inst.published_date = inst.published_source = None
    else:
        eol = cycle.get("eol")
        inst.eol_cycle = str(cycle.get("cycle"))
        if eol is True:
            inst.eol_date = EOL_UNKNOWN_DATE
        else:
            inst.eol_date = _as_date(eol)  # False/absent -> None (still supported)
        inst.upstream_latest = str(cycle["latest"]) if cycle.get("latest") else None
        inst.upstream_latest_date = _as_date(cycle.get("latestReleaseDate"))
        if inst.published_date is None:
            installed = version_tuple(upstream_version(inst.version, inst.source))
            if installed and installed == version_tuple(inst.upstream_latest):
                inst.published_date = inst.upstream_latest_date
            elif installed and installed in (
                version_tuple(inst.eol_cycle), (*version_tuple(inst.eol_cycle), 0)
            ):
                inst.published_date = _as_date(cycle.get("releaseDate"))
            if inst.published_date is not None:
                inst.published_source = "endoflife.date release date"
        # A file-timestamp date ("approx.") can't be older than the release
        # of the series the version belongs to: Chrome updates inside its
        # bundle and leaves the bundle's own date years behind.
        cycle_released = _as_date(cycle.get("releaseDate"))
        if (
            cycle_released
            and inst.published_date is not None
            and "approx" in (inst.published_source or "")
            and inst.published_date < cycle_released
        ):
            inst.published_date = cycle_released
            inst.published_source = "endoflife.date: release of this series"
    after = (inst.eol_cycle, inst.eol_date, inst.upstream_latest, inst.upstream_latest_date,
             inst.published_date, inst.published_source)
    return before != after


def enrich_software_eol(session: Session, client: httpx.Client | None = None) -> dict:
    """Looks every item up on endoflife.date and updates its installs.
    Commits. A failed product fetch is logged and skipped, never fatal."""
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT})
    try:
        resp = client.get(f"{EOL_API}/all.json")
        resp.raise_for_status()
        products = set(resp.json())

        items = session.exec(select(SoftwareItem)).all()
        cache: dict[str, list[dict] | None] = {}
        fetched = errors = matched_items = changed = 0
        for item in items:
            slugs = candidate_products(item.key, products)
            installs = session.exec(
                select(SoftwareInstall).where(SoftwareInstall.software_id == item.id)
            ).all()
            chosen = None
            for slug in slugs:
                if slug not in cache:
                    if fetched >= MAX_PRODUCTS:
                        break
                    try:
                        time.sleep(REQUEST_PACING_SECONDS)
                        r = client.get(f"{EOL_API}/{slug}.json")
                        r.raise_for_status()
                        data = r.json()
                        cache[slug] = data if isinstance(data, list) else None
                    except (httpx.HTTPError, ValueError):
                        logger.warning("endoflife.date fetch failed for %s", slug, exc_info=True)
                        cache[slug] = None
                        errors += 1
                    fetched += 1
                cycles = cache.get(slug)
                if cycles and any(
                    match_cycle(upstream_version(i.version, i.source), cycles) for i in installs
                ):
                    chosen = slug
                    break
            item.eol_product = chosen
            session.add(item)
            if chosen:
                matched_items += 1
            for inst in installs:
                cycle = (
                    match_cycle(upstream_version(inst.version, inst.source), cache[chosen])
                    if chosen else None
                )
                if apply_cycle(inst, cycle):
                    changed += 1
                    session.add(inst)
        session.commit()
        return {
            "items": len(items),
            "items_matched": matched_items,
            "products_fetched": fetched,
            "fetch_errors": errors,
            "installs_changed": changed,
        }
    finally:
        if own_client:
            client.close()
