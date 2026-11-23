"""Imports one machine's software inventory -- the JSON printed by
scripts/software-inventory/collect_unix.py or collect_windows.ps1 -- into
SoftwareItem/SoftwareInstall/SoftwareHost.

Three decisions live here rather than in the collectors, so they are made in
one place, tested, and changed without redeploying anything to the machines:

  * What is the operating system itself, and so never listed: kernels,
    firmware and OS-release packages on Linux, Windows' own entries. The OS
    is recorded against the hardware/VM asset instead.
  * What is an application, a library/component, or a base-system package
    (the Software tab shows applications by default).
  * Which entries from different machines and package managers are the
    same software: software_key() is the cross-source match key.

Each import replaces that machine's installs wholesale (first_seen carried
over), so software removed from a machine drops off its next import.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from sqlalchemy import func
from sqlmodel import Session, select

from app.clock import utcnow_naive
from app.models import (
    Asset,
    AssetInterface,
    SoftwareHost,
    SoftwareInstall,
    SoftwareItem,
    SoftwareNote,
)
from app.software_risk import version_tuple

logger = logging.getLogger(__name__)

PLATFORMS = {"darwin", "linux", "windows"}
SOURCES = {"app", "brew", "brew-cask", "apt", "snap", "flatpak", "windows", "appx"}

APPLICATION = "application"
LIBRARY = "library"
SYSTEM = "system"


class SoftwareImportError(ValueError):
    """The document can't be imported as given (bad shape, or no single
    asset matches the machine). The message says what to do about it."""


# ------------------------------------------------------------ match keys

# Names the generic rules below can't reconcile across sources. Keys and
# values are post-normalization (lowercase, parentheticals and trailing
# version numbers already removed).
KEY_ALIASES = {
    "mozilla firefox": "firefox",
    "firefox-esr": "firefox esr",
    "mozilla firefox esr": "firefox esr",
    "mozilla thunderbird": "thunderbird",
    "google-chrome": "google chrome",
    "google-chrome-stable": "google chrome",
    "code": "visual studio code",
    "visual-studio-code": "visual studio code",
    "microsoft visual studio code": "visual studio code",
    "microsoft visual studio code insiders": "visual studio code insiders",
    "vlc media player": "vlc",
    "docker.io": "docker",
    "docker-ce": "docker",
    "libreoffice-core": "libreoffice",
    "keepassxc-full": "keepassxc",
    "7-zip": "7-zip",
    "powershell@preview": "powershell preview",
    "powershell-preview": "powershell preview",
    "nodejs": "node",
}

_PAREN = re.compile(r"\s*[(\[][^)\]]*[)\]]")
_TRAILING_VERSION = re.compile(r"[\s_-]+v?\d+(?:\.\d+)+[a-z0-9.+-]*$")
_AT_VERSION = re.compile(r"^([a-z][a-z0-9+-]*)@(\d+(?:\.\d+)?)$")
_PYTHON = re.compile(r"^python\s*@?\s*(3\.\d+)(?:\.\d+)*$")


def software_key(name: str) -> str:
    """Cross-source match key: "Mozilla Firefox (x64 en-GB)", Homebrew's
    "firefox" cask and the "Firefox" app all become "firefox"; Homebrew's
    "python@3.12", apt's "python3.12" and Windows' "Python 3.12.4 (64-bit)"
    all become "python 3.12". Best-effort by design -- an unmatched pair just
    shows as two rows, which is visible and harmless."""
    n = name.strip().lower()
    n = re.sub(r"\.app$", "", n)
    n = _PAREN.sub("", n).strip()
    m = _PYTHON.match(n)
    if m:
        return f"python {m.group(1)}"
    m = _AT_VERSION.match(n)
    if m:
        n = f"{m.group(1)} {m.group(2)}"
    else:
        n = _TRAILING_VERSION.sub("", n)
    n = re.sub(r"[\s_]+", " ", n).strip()
    return KEY_ALIASES.get(n, n) or name.strip().lower()


# ------------------------------------------------------------ classification

# The operating system itself -- recorded on the asset, never listed here.
_LINUX_OS_PACKAGE = re.compile(
    r"^(linux-(image|headers|modules|modules-extra|objects|signatures|buildinfo|"
    r"tools|cloud-tools|generic|hwe|signed|firmware|libc-dev|base|sound-base|"
    r"source|kbuild|support)\b.*"
    r"|base-files|lsb-release|os-release|distro-info-data"
    r"|mint-meta-.*|mint-info-.*|mintsystem|linuxmint-keyring"
    r"|kali-linux-.*|kali-desktop-.*|kali-defaults.*|kali-archive-keyring"
    r"|ubuntu-minimal|ubuntu-standard|ubuntu-keyring|debian-archive-keyring"
    r"|intel-microcode|amd64-microcode|firmware-.*)$"
)
_WINDOWS_OS_ENTRY = re.compile(r"^(microsoft )?windows (\d+|server|xp|vista)\b", re.IGNORECASE)

_APT_LIBRARY_SECTION = re.compile(r"(^|/)(libs|oldlibs|libdevel|fonts|localization|doc|introspection)$")
_APT_LIBRARY_NAME = re.compile(
    r"^(lib(?!reoffice($|-)|vpx-tools).*|python3?-.*|ruby-.*|node-.*|golang-.*|gir1\.2-.*|fonts?-.*)$"
    r"|.*-(dev|data|common|doc|l10n|locale|dbg|perl)$"
)
_APT_LIBRARY_EXCEPTION = re.compile(r".*-(bin|tools|utils|progs|clients?)$")
# Parts of Windows that show up as programs: graded, but filed with the base
# system rather than as something someone installed.
_WINDOWS_SYSTEM = re.compile(
    r"update health tools|application compatibility|windows pc health check|"
    r"microsoft edge update|windows driver package|gaming services",
    re.IGNORECASE,
)
_WINDOWS_LIBRARY = re.compile(
    r"visual c\+\+ .*redistributable|\.net .*runtime|desktop runtime|windows (app|software) "
    r"(runtime|development kit)|webview2 runtime|directx|vcredist",
    re.IGNORECASE,
)


def classify(rec: dict) -> str | None:
    """application / library / system, or None for the OS itself (dropped)."""
    source = rec.get("source")
    name = rec.get("name") or ""
    if source == "apt":
        if _LINUX_OS_PACKAGE.match(name):
            return None
        if rec.get("essential") or rec.get("priority") in ("required", "important"):
            return SYSTEM
        section = rec.get("section") or ""
        if not _APT_LIBRARY_EXCEPTION.match(name) and (
            _APT_LIBRARY_SECTION.search(section) or _APT_LIBRARY_NAME.match(name)
        ):
            return LIBRARY
        return APPLICATION
    if source == "brew":
        return APPLICATION if rec.get("explicit") else LIBRARY
    if source in ("windows", "appx"):
        if _WINDOWS_OS_ENTRY.match(name):
            return None
        if _WINDOWS_SYSTEM.search(name):
            return SYSTEM
        # Microsoft's own Store apps (Weather, the codec extensions, ...) ship
        # with Windows and update with it.
        if source == "appx" and (rec.get("publisher") or "").startswith("Microsoft Corporation"):
            return SYSTEM
        return LIBRARY if _WINDOWS_LIBRARY.search(name) else APPLICATION
    if source == "snap":
        return SYSTEM if rec.get("category_hint") == SYSTEM else APPLICATION
    return APPLICATION


def _appx_display(package_name: str) -> str:
    """A readable name from a Store package id when the manifest gave none:
    "26720RandomSaladGamesLLC.SimpleSolitaire" -> "Simple Solitaire",
    "Microsoft.BingWeather" -> "Bing Weather"."""
    tail = package_name.split(".", 1)[1] if "." in package_name else package_name
    tail = tail.replace(".", " ")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tail).strip() or package_name


# ------------------------------------------------------------ asset match


def _normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def find_asset(session: Session, doc: dict, asset_id: int | None = None) -> Asset:
    """The asset this inventory belongs to. In order: an explicit asset id;
    the collector's MACs against AssetInterface; its hostname against a
    previous import (SoftwareHost.hostname) or Asset.hostname. Never guesses
    between two candidates: listing a machine's software against the wrong
    asset is worse than refusing."""
    if asset_id is not None:
        asset = session.get(Asset, asset_id)
        if asset is None:
            raise SoftwareImportError(f"no asset with id {asset_id}")
        return asset

    macs = {_normalize_mac(m) for m in doc.get("macs") or [] if isinstance(m, str)}
    if macs:
        ids = set(
            session.exec(
                select(AssetInterface.asset_id).where(AssetInterface.mac.in_(macs))
            ).all()
        )
        if len(ids) == 1:
            return session.get(Asset, ids.pop())
        if len(ids) > 1:
            raise SoftwareImportError(
                f"the machine's MAC addresses match several assets {sorted(ids)}; "
                "pass --asset-id to choose"
            )

    hostname = (doc.get("hostname") or "").strip().lower()
    if hostname:
        ids = set(
            session.exec(
                select(SoftwareHost.asset_id).where(func.lower(SoftwareHost.hostname) == hostname)
            ).all()
        ) or set(
            session.exec(select(Asset.id).where(func.lower(Asset.hostname) == hostname)).all()
        )
        if len(ids) == 1:
            return session.get(Asset, ids.pop())
        if len(ids) > 1:
            raise SoftwareImportError(
                f"the machine's hostname matches several assets {sorted(ids)}; "
                "pass --asset-id to choose"
            )
    raise SoftwareImportError(
        "no asset matches this machine's MAC addresses or hostname; "
        "pass --asset-id (the number in the asset's /assets/<id> URL)"
    )


# ------------------------------------------------------------ import


# Some builds zero their file timestamps (a 1980-01-01 bundle date is the
# ZIP epoch, not a release date); anything that early, or in the future, is
# treated as unknown rather than graded as decades old.
_EARLIEST_PLAUSIBLE = date(1995, 1, 1)


def _date(value) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return None
    if parsed < _EARLIEST_PLAUSIBLE or parsed > date.today() + timedelta(days=2):
        return None
    return parsed


def _text(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _clean_vulns(vulns) -> str | None:
    if vulns is None:
        return None
    out = []
    for v in vulns if isinstance(vulns, list) else []:
        if isinstance(v, dict) and v.get("id"):
            out.append(
                {
                    "id": str(v["id"]),
                    "severity": str(v.get("severity") or "UNKNOWN").upper(),
                    "fixed": bool(v.get("fixed")),
                }
            )
    return json.dumps(out)


def validate(doc) -> None:
    if not isinstance(doc, dict):
        raise SoftwareImportError("inventory must be a JSON object")
    if doc.get("platform") not in PLATFORMS:
        raise SoftwareImportError(f"unknown platform {doc.get('platform')!r}")
    if not isinstance(doc.get("packages"), list):
        raise SoftwareImportError("inventory has no packages list")


def import_inventory(session: Session, doc: dict, asset_id: int | None = None) -> dict:
    """Writes one machine's inventory and commits. Returns a summary."""
    validate(doc)
    asset = find_asset(session, doc, asset_id)
    now = utcnow_naive()

    items = {item.key: item for item in session.exec(select(SoftwareItem)).all()}
    existing: dict[tuple, SoftwareInstall] = {}
    stale: list[SoftwareInstall] = []
    for inst in session.exec(select(SoftwareInstall).where(SoftwareInstall.asset_id == asset.id)).all():
        k = (inst.software_id, inst.source, inst.package_name)
        if k in existing:
            stale.append(inst)  # a duplicate left by an asset merge
        else:
            existing[k] = inst

    kept: set[int] = set()
    imported = excluded = skipped = items_created = 0
    for rec in doc["packages"]:
        if not isinstance(rec, dict):
            skipped += 1
            continue
        name = _text(rec.get("name"))
        source = rec.get("source")
        if not name or source not in SOURCES:
            skipped += 1
            continue
        category = classify(rec)
        if category is None:
            excluded += 1
            continue

        display = _text(rec.get("display_name")) or (
            _appx_display(name) if source == "appx" else name
        )
        key = software_key(display)
        item = items.get(key)
        if item is None:
            item = SoftwareItem(key=key, name=display, first_seen=now)
            session.add(item)
            session.flush()
            items[key] = item
            items_created += 1
        elif item.name == item.name.lower() and display != display.lower():
            item.name = display  # prefer "Firefox" over "firefox"
        for field in ("publisher", "description", "homepage"):
            if not getattr(item, field) and _text(rec.get(field)):
                setattr(item, field, _text(rec.get(field)))
        session.add(item)

        inst = existing.get((item.id, source, name))
        version = _text(rec.get("version"))
        repeat = inst is not None and inst.id in kept
        if repeat and version_tuple(version) <= version_tuple(inst.version):
            continue  # the same package twice in one document: the newest copy wins
        if inst is None:
            inst = SoftwareInstall(
                software_id=item.id, asset_id=asset.id, source=source,
                package_name=name, first_seen=now,
            )
        elif inst.version != version:
            # endoflife.date facts belong to the old version's release cycle.
            inst.eol_cycle = inst.eol_date = inst.upstream_latest = inst.upstream_latest_date = None
        inst.category = category
        inst.version = version
        inst.published_date = _date(rec.get("published"))
        inst.published_source = _text(rec.get("published_source")) if inst.published_date else None
        inst.installed_date = _date(rec.get("installed"))
        inst.update_available = bool(rec.get("update_available"))
        inst.latest_version = _text(rec.get("latest_version")) if inst.update_available else None
        inst.security_update = bool(rec.get("security_update")) and inst.update_available
        inst.deprecated = bool(rec.get("deprecated"))
        inst.disabled = bool(rec.get("disabled"))
        inst.orphaned = bool(rec.get("orphaned"))
        inst.intel_only = bool(rec.get("intel_only"))
        inst.vulns_json = _clean_vulns(rec["vulns"]) if "vulns" in rec else None
        inst.last_seen = now
        session.add(inst)
        session.flush()
        existing[(item.id, source, name)] = inst
        kept.add(inst.id)
        if not repeat:
            imported += 1

    removed = 0
    for inst in list(existing.values()) + stale:
        if inst.id not in kept:
            session.delete(inst)
            removed += 1

    hosts = session.exec(select(SoftwareHost).where(SoftwareHost.asset_id == asset.id)).all()
    host = hosts[0] if hosts else SoftwareHost(asset_id=asset.id, platform=doc["platform"])
    for extra in hosts[1:]:
        session.delete(extra)
    os_info = doc.get("os") if isinstance(doc.get("os"), dict) else {}
    host.collected_at = now
    host.hostname = _text(doc.get("hostname"))
    host.platform = doc["platform"]
    host.os_name = _text(os_info.get("name"))
    host.os_version = _text(os_info.get("version"))
    host.package_count = imported
    notes = [str(n) for n in doc.get("notes") or [] if n]
    host.notes_json = json.dumps(notes) if notes else None
    session.add(host)
    session.flush()

    # Items no machine has any more -- unless someone wrote a review note on
    # one: that finding should still be there if the software comes back.
    used = set(session.exec(select(SoftwareInstall.software_id).distinct()).all())
    used |= set(session.exec(select(SoftwareNote.software_id).distinct()).all())
    pruned = 0
    for item in items.values():
        if item.id not in used:
            session.delete(item)
            pruned += 1

    session.commit()
    return {
        "asset_id": asset.id,
        "platform": doc["platform"],
        "imported": imported,
        "excluded_os": excluded,
        "skipped": skipped,
        "removed": removed,
        "items_created": items_created,
        "items_pruned": pruned,
    }
