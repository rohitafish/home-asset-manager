#!/usr/bin/env python3
"""Software inventory collector for macOS and Linux machines.

Prints one JSON document on stdout describing the software installed on the
machine it runs on; `python -m discovery.cli software-import` reads it on the
app host. scripts/collect-software.sh pipes this file to each machine over
ssh (`ssh host python3 - < collect_unix.py`), so it must stay a single,
dependency-free file that runs on the oldest Python any of them has --
Apple's Command Line Tools Python 3.9 on a Mac. Read-only: it runs package
managers' list/info commands and reads files, and changes nothing (Homebrew
is run with HOMEBREW_NO_AUTO_UPDATE so it doesn't refresh its metadata).

What it reports, per platform:
  macOS  apps under /Applications and ~/Applications (not /System: that is
         the OS), Homebrew formulae and casks, and `brew vulns` findings.
  Linux  every dpkg package (the importer decides what is OS, library or
         application), pending apt updates and whether they come from a
         security pocket, packages no repository offers any more, snaps and
         flatpak apps.

Deliberately not collected: file paths (they carry the user's home folder),
package maintainers (they carry people's email addresses), and anything
about the OS itself beyond its name and version.
"""

from __future__ import annotations

import gzip
import json
import os
import platform
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

COLLECTOR_VERSION = 1


def _run(cmd, timeout=120, env=None, ok_codes=(0,)):
    """stdout of cmd, or None if it is missing, fails or times out."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in ok_codes:
        return None
    return result.stdout


def _iso_date(ts):
    """A Unix timestamp or ISO string as YYYY-MM-DD, or None."""
    if ts in (None, ""):
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


# ---------------------------------------------------------------- macOS


def parse_macos_apps(apps, home=None):
    """system_profiler SPApplicationsDataType entries -> package records.
    Keeps apps a person installed: /Applications and ~/Applications, not
    apps nested inside another bundle (helpers), not /System or /Library."""
    home = home or os.path.expanduser("~")
    out = []
    for app in apps:
        path = app.get("path") or ""
        roots = ("/Applications/", home + "/Applications/")
        if not path.startswith(roots):
            continue
        if path.count(".app") != 1:  # a helper app inside another bundle
            continue
        out.append(
            {
                "source": "app",
                "name": app.get("_name") or os.path.basename(path)[:-4],
                "version": app.get("version"),
                # The bundle's modification time: for a signed download it is
                # the vendor's build date, for an App Store app the update
                # date. Close to, but not exactly, the release date.
                "published": _iso_date(app.get("lastModified")),
                "published_source": "app bundle date (approx.)",
                "publisher": _signer(app.get("signed_by")),
                "intel_only": app.get("arch_kind") == "arch_i64",
                "obtained_from": app.get("obtained_from"),
            }
        )
    return out


def _signer(signed_by):
    if not signed_by:
        return None
    first = signed_by[0]
    m = re.match(r"(?:Developer ID Application|Apple Mac OS Application Signing|Apple Distribution): (.+?)(?: \([A-Z0-9]+\))?$", first)
    if m:
        return m.group(1)
    if first.startswith("Software Signing"):
        return "Apple"
    return None


def _brew_path():
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew", "/home/linuxbrew/.linuxbrew/bin/brew"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _brew_env():
    env = dict(os.environ)
    env.update(
        HOMEBREW_NO_AUTO_UPDATE="1",
        HOMEBREW_NO_ENV_HINTS="1",
        HOMEBREW_NO_ANALYTICS="1",
        HOMEBREW_NO_COLOR="1",
    )
    return env


def parse_brew_info(info, receipts=None, vulns=None):
    """`brew info --json=v2 --installed` -> package records. receipts maps
    formula name -> its INSTALL_RECEIPT.json dict, vulns maps formula name ->
    list of {"id", "severity", "fixed"} (absent = not scanned)."""
    receipts = receipts or {}
    out = []
    for f in info.get("formulae", []):
        installed = (f.get("installed") or [{}])[-1]
        name = f.get("name")
        version = installed.get("version")
        receipt = receipts.get(name) or {}
        rec = {
            "source": "brew",
            "name": name,
            "version": version,
            # The formula file's commit time at the installed version: when
            # Homebrew published that version.
            "published": _iso_date(receipt.get("source_modified_time")),
            "published_source": "Homebrew formula date",
            "installed": _iso_date(installed.get("time")),
            "explicit": bool(installed.get("installed_on_request")),
            "description": f.get("desc"),
            "homepage": f.get("homepage"),
            "update_available": bool(f.get("outdated")),
            "latest_version": (f.get("versions") or {}).get("stable"),
            "deprecated": bool(f.get("deprecated")),
            "disabled": bool(f.get("disabled")),
        }
        if vulns is not None:
            rec["vulns"] = vulns.get(name, [])
        out.append(rec)
    for c in info.get("casks", []):
        names = c.get("name") or []
        out.append(
            {
                "source": "brew-cask",
                "name": c.get("token"),
                "display_name": names[0] if names else None,
                "version": c.get("installed"),
                "installed": _iso_date(c.get("installed_time")),
                "explicit": True,
                "description": c.get("desc"),
                "homepage": c.get("homepage"),
                "update_available": bool(c.get("outdated")),
                "latest_version": c.get("version"),
                "deprecated": bool(c.get("deprecated")),
                "disabled": bool(c.get("disabled")),
            }
        )
    return out


def parse_brew_vulns(report):
    """`brew vulns --json` -> {formula: [{"id", "severity", "fixed"}]}. Only
    the open findings: "patched" ones are fixed by a Homebrew formula patch
    in the build that is installed."""
    out = {}
    for finding in report.get("findings", []):
        items = []
        for v in finding.get("vulnerabilities", []):
            items.append(
                {
                    "id": v.get("id"),
                    "severity": (v.get("severity") or "UNKNOWN").upper(),
                    "fixed": bool(v.get("fixed_versions")),
                }
            )
        out[finding.get("formula")] = items
    return out


def collect_macos(notes):
    packages = []
    raw = _run(["system_profiler", "-json", "SPApplicationsDataType"], timeout=180)
    if raw:
        try:
            packages += parse_macos_apps(json.loads(raw).get("SPApplicationsDataType", []))
        except ValueError:
            notes.append("system_profiler output was not JSON; apps not collected")
    else:
        notes.append("system_profiler failed; apps not collected")

    brew = _brew_path()
    if not brew:
        return packages
    env = _brew_env()
    raw = _run([brew, "info", "--json=v2", "--installed"], timeout=300, env=env)
    if not raw:
        notes.append("brew info failed; Homebrew packages not collected")
        return packages
    info = json.loads(raw)
    cellar = (_run([brew, "--cellar"], env=env) or "").strip()
    receipts = {}
    for f in info.get("formulae", []):
        installed = (f.get("installed") or [{}])[-1]
        path = os.path.join(cellar, f.get("name", ""), installed.get("version") or "", "INSTALL_RECEIPT.json")
        try:
            with open(path) as fh:
                receipts[f["name"]] = json.load(fh)
        except (OSError, ValueError, KeyError):
            pass
    vulns = None
    # Exit status 1 means "found vulnerabilities", not "failed".
    raw_vulns = _run([brew, "vulns", "--json"], timeout=300, env=env, ok_codes=(0, 1))
    if raw_vulns:
        try:
            vulns = parse_brew_vulns(json.loads(raw_vulns))
        except ValueError:
            notes.append("brew vulns output was not JSON; Homebrew vulnerabilities not checked")
    else:
        notes.append("brew vulns unavailable; Homebrew vulnerabilities not checked")
    packages += parse_brew_info(info, receipts, vulns)
    return packages


def macos_os():
    return "macOS", platform.mac_ver()[0] or None


# ---------------------------------------------------------------- Linux

DPKG_FORMAT = "${Package}\t${Version}\t${Source}\t${Priority}\t${Section}\t${Essential}\t${db:Status-Abbrev}\t${binary:Summary}\n"


def parse_dpkg(text):
    """dpkg-query output in DPKG_FORMAT -> {name: record}. Only installed
    packages: status "ii", or "hi" for one held at its version (a pinned
    kernel, say, which still counts). Multi-arch
    duplicates collapse to one."""
    out = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 8 or parts[6][1:2] != "i":
            continue
        name, version, source, priority, section, essential, _status, summary = parts[:8]
        if name in out:
            continue
        out[name] = {
            "source": "apt",
            "name": name,
            "version": version,
            "source_package": (source.split(" ")[0] or name),
            "priority": priority or None,
            "section": section or None,
            "essential": essential == "yes",
            "description": summary or None,
        }
    return out


_UPGRADABLE_RE = re.compile(r"^([^/\s]+)/(\S+)\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]")


def parse_apt_upgradable(text):
    """`apt list --upgradable` -> {name: (candidate_version, is_security)}."""
    out = {}
    for line in text.splitlines():
        m = _UPGRADABLE_RE.match(line.strip())
        if m:
            suites = m.group(2).split(",")
            out[m.group(1)] = (m.group(3), any("security" in s for s in suites))
    return out


def parse_apt_installed_local(text):
    """`apt list --installed` -> names no configured repository offers any
    more ("[installed,local]")."""
    out = set()
    for line in text.splitlines():
        if "[installed,local]" in line:
            out.add(line.split("/", 1)[0])
    return out


def changelog_date(pkg, doc_dir="/usr/share/doc"):
    """Date of the top entry of a package's Debian changelog -- when this
    exact package version was published by the distribution. Only the date
    is read; the maintainer line it sits on is discarded."""
    for name in ("changelog.Debian.gz", "changelog.gz"):
        path = os.path.join(doc_dir, pkg, name)
        try:
            with gzip.open(path, "rt", errors="replace") as fh:
                for _ in range(400):
                    line = fh.readline()
                    if not line:
                        break
                    if line.startswith(" -- "):
                        stamp = line.rsplit("  ", 1)[-1].strip()
                        try:
                            return parsedate_to_datetime(stamp).date().isoformat()
                        except (TypeError, ValueError, IndexError):
                            return None
        except (OSError, EOFError):
            continue
    return None


def parse_snap_list(text):
    out = []
    lines = text.splitlines()
    for line in lines[1:]:
        cols = line.split()
        if len(cols) < 2:
            continue
        notes = cols[5] if len(cols) > 5 else ""
        base = cols[0] in ("snapd", "bare") or cols[0].startswith("core") or "base" in notes
        out.append(
            {
                "source": "snap",
                "name": cols[0],
                "version": cols[1],
                "publisher": cols[4].rstrip("*✓") if len(cols) > 4 else None,
                "category_hint": "system" if base else None,
            }
        )
    return out


def parse_flatpak_list(text):
    out = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) >= 3 and cols[0]:
            out.append({"source": "flatpak", "name": cols[0], "display_name": cols[1] or None, "version": cols[2] or None})
    return out


def collect_linux(notes):
    packages = []
    raw = _run(["dpkg-query", "-W", "-f", DPKG_FORMAT])
    if raw is None:
        notes.append("dpkg-query unavailable; apt packages not collected")
    else:
        pkgs = parse_dpkg(raw)
        upgradable = parse_apt_upgradable(_run(["apt", "list", "--upgradable"]) or "")
        local = parse_apt_installed_local(_run(["apt", "list", "--installed"]) or "")
        for name, rec in pkgs.items():
            rec["published"] = changelog_date(name)
            rec["published_source"] = "distro package changelog"
            if name in upgradable:
                rec["update_available"] = True
                rec["latest_version"], rec["security_update"] = upgradable[name]
            rec["orphaned"] = name in local
            packages.append(rec)
        lists = "/var/lib/apt/lists"
        try:
            newest = max(os.path.getmtime(os.path.join(lists, f)) for f in os.listdir(lists))
            notes.append("apt package lists last refreshed " + _iso_date(newest))
        except (OSError, ValueError):
            pass
    snap = _run(["snap", "list"])
    if snap:
        packages += parse_snap_list(snap)
    flatpak = _run(["flatpak", "list", "--app", "--columns=application,name,version"])
    if flatpak:
        packages += parse_flatpak_list(flatpak)
    return packages


def linux_os():
    info = {}
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    info[k] = v.strip('"')
    except OSError:
        pass
    return info.get("NAME"), info.get("VERSION") or info.get("VERSION_ID")


# ---------------------------------------------------------------- identity

_MAC_RE = re.compile(r"\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b", re.IGNORECASE)


def local_macs():
    """Physical interfaces' MACs, including a private Wi-Fi address if that
    is what the interface is using now (it is what the network sees, and so
    what the inventory's interface rows hold). The importer matches these
    against AssetInterface to find this machine's asset."""
    macs = set()
    if sys.platform == "darwin":
        out = _run(["ifconfig", "-a"]) or ""
        current = None
        for line in out.splitlines():
            if line and not line[0].isspace():
                current = line.split(":", 1)[0]
            elif current and re.match(r"^en\d+$", current):
                m = re.search(r"ether\s+" + _MAC_RE.pattern, line, re.IGNORECASE)
                if m:
                    macs.add(m.group(1).lower())
    else:
        base = "/sys/class/net"
        try:
            for iface in os.listdir(base):
                if not os.path.exists(os.path.join(base, iface, "device")):
                    continue
                try:
                    with open(os.path.join(base, iface, "address")) as fh:
                        macs.add(fh.read().strip().lower())
                except OSError:
                    pass
        except OSError:
            pass
    macs.discard("00:00:00:00:00:00")
    return sorted(m for m in macs if _MAC_RE.fullmatch(m))


def main():
    notes = []
    if sys.platform == "darwin":
        plat = "darwin"
        packages = collect_macos(notes)
        os_name, os_version = macos_os()
    elif sys.platform.startswith("linux"):
        plat = "linux"
        packages = collect_linux(notes)
        os_name, os_version = linux_os()
    else:
        sys.stderr.write("unsupported platform: %s\n" % sys.platform)
        return 2
    doc = {
        "collector_version": COLLECTOR_VERSION,
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": plat,
        "hostname": socket.gethostname().split(".")[0],
        "os": {"name": os_name, "version": os_version},
        "macs": local_macs(),
        "packages": packages,
        "notes": notes,
    }
    json.dump(doc, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
