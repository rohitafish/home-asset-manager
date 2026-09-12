"""One-off collector for the hardware this app happens to be running on --
serial number, model identifier and model number, read from the OS.

On macOS that is `system_profiler` -- the same data Mactracker's My Models
shows for a Mac (Mactracker's own database turned out to be unreadable for
this purpose: its bundled model DB is encrypted, and its My Models sync
files are behind macOS TCC over SSH -- see README). On Linux it is the DMI
tables under /sys/class/dmi/id, which on Apple hardware carry the same
model identifier (e.g. "MacBookPro11,1") and serial; the marketing model
number ("MGX72B/A") is not in DMI, so that field stays whatever it was.

Unlike the UniFi collector, this can only ever describe *this* host -- there
is no way to query hardware identity for a machine elsewhere on the network.
In practice that means running this on the dev machine reports the dev
machine's own hardware, not the deployed host's; the useful run is the one
triggered on the always-on host itself, where the app actually lives.

Finding "this host"'s existing asset row is a matching problem, not a given
-- see find_this_host_asset().
"""

import json
import logging
import platform
import re
import subprocess
from pathlib import Path

from sqlmodel import Session, select

from app.clock import utcnow_naive
from app.models import Asset
from discovery.reconcile import _find_asset_by_mac

logger = logging.getLogger(__name__)

# Module-level so tests can point them at a fixture tree.
_DMI_DIR = Path("/sys/class/dmi/id")
_NET_DIR = Path("/sys/class/net")


def _collect_darwin() -> dict | None:
    try:
        result = subprocess.run(
            ["system_profiler", "-json", "SPHardwareDataType"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        hw = json.loads(result.stdout)["SPHardwareDataType"][0]
    except Exception:
        # Reaching here means system_profiler timed out / errored / changed
        # shape. Log so those aren't indistinguishable from a legitimately-
        # absent read.
        logger.warning("system_profiler hardware read failed", exc_info=True)
        return None
    return {
        "serial_number": hw.get("serial_number"),
        "model_identifier": hw.get("machine_model"),  # e.g. "Macmini9,1"
        "model_number": hw.get("model_number"),  # e.g. "MGNR3B/A"
        "model": hw.get("machine_name"),  # e.g. "Mac mini"
    }


def _dmi(name: str) -> str | None:
    """One DMI attribute, or None if unreadable. product_serial is root-only
    (mode 0400) on every distribution; when it is, fall back to dmidecode
    under a non-interactive sudo, which the always-on host permits and a dev
    laptop typically refuses -- either way this never prompts and never
    raises."""
    try:
        value = (_DMI_DIR / name).read_text().strip()
        return value or None
    except PermissionError:
        if name != "product_serial":
            return None
        try:
            result = subprocess.run(
                ["sudo", "-n", "dmidecode", "-s", "system-serial-number"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            return result.stdout.strip() or None
        except Exception:
            return None
    except OSError:
        return None


_APPLE_FAMILIES = {
    "MacBookPro": "MacBook Pro",
    "MacBookAir": "MacBook Air",
    "MacBook": "MacBook",
    "Macmini": "Mac mini",
    "MacPro": "Mac Pro",
    "MacStudio": "Mac Studio",
    "iMac": "iMac",
    "iMacPro": "iMac Pro",
}


def _collect_linux() -> dict | None:
    model_identifier = _dmi("product_name")
    if not model_identifier:
        logger.warning("no DMI product_name under %s; no hardware collected", _DMI_DIR)
        return None
    vendor = _dmi("sys_vendor") or ""
    # DMI has no marketing name. On Apple hardware the identifier's letters
    # are the family ("MacBookPro11,1" -> "MacBook Pro"); elsewhere the
    # vendor + product is the best available label.
    if vendor.startswith("Apple"):
        family = re.sub(r"\d.*$", "", model_identifier)
        model = _APPLE_FAMILIES.get(family, family) or None
    else:
        model = " ".join(p for p in (vendor, model_identifier) if p) or None
    return {
        "serial_number": _dmi("product_serial"),
        "model_identifier": model_identifier,
        "model_number": None,
        "model": model,
    }


def collect_local_hardware() -> dict | None:
    """Returns {"serial_number", "model_identifier", "model_number", "model"}
    for the machine this runs on, or None if the platform is unsupported or
    the read failed for any reason. Never raises."""
    system = platform.system()
    if system == "Darwin":
        return _collect_darwin()
    if system == "Linux":
        return _collect_linux()
    return None


_ETHER_RE = re.compile(r"ether\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.IGNORECASE)


def _is_real_mac(mac: str) -> bool:
    if mac == "00:00:00:00:00:00":
        return False
    first_octet = int(mac.split(":")[0], 16)
    return not (first_octet & 0x02)  # locally administered bit -- synthetic/randomized


def _local_macs_darwin() -> list[str]:
    try:
        result = subprocess.run(
            ["ifconfig", "-a"], capture_output=True, text=True, timeout=10, check=True
        )
    except Exception:
        logger.warning("ifconfig read failed; no local MACs collected", exc_info=True)
        return []
    return [
        m.group(1).lower()
        for m in _ETHER_RE.finditer(result.stdout)
        if _is_real_mac(m.group(1).lower())
    ]


def _local_macs_linux() -> list[str]:
    """Physical interfaces only. Linux's synthetic set is Docker's bridge
    (docker0), every container veth, compose's br-* bridges, tunnels and
    the like -- none of which will ever appear in UniFi or nmap data, and
    Docker's bridge MAC becoming a host identity is exactly the false join
    this function exists to prevent. The reliable tell is the `device`
    symlink: a real NIC has one, a virtual interface does not."""
    macs = []
    try:
        entries = sorted(_NET_DIR.iterdir())
    except OSError:
        logger.warning(
            "%s unreadable; no local MACs collected", _NET_DIR, exc_info=True
        )
        return []
    for iface in entries:
        if not (iface / "device").exists():
            continue
        try:
            mac = (iface / "address").read_text().strip().lower()
        except OSError:
            continue
        if re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac) and _is_real_mac(mac):
            macs.append(mac)
    return macs


def local_macs() -> list[str]:
    """This host's own MAC addresses, lowercase colon-separated, excluding
    locally-administered/randomized addresses and virtual interfaces. macOS
    synthesizes many of these (anpiN, awdlN, llwN, bridge0, private Wi-Fi
    addressing); Linux adds Docker's. None will ever appear in UniFi or nmap
    data, so matching on them would only produce false negatives, never a
    real join."""
    system = platform.system()
    if system == "Darwin":
        return _local_macs_darwin()
    if system == "Linux":
        return _local_macs_linux()
    return []


def find_this_host_asset(
    session: Session, serial: str | None
) -> tuple[Asset | None, list[int]]:
    """Finds the Asset row for the host this code is running on. Returns
    (asset, candidate_ids): asset is None whenever the lookup didn't resolve
    to exactly one row, and candidate_ids then lists whatever ambiguous
    matches were found (empty if there were none at all).

    Priority order:
      1. An existing serial_number match -- exact, and self-confirming once
         a previous run has already set it.
      2. This host's own MACs against AssetInterface -- how it bootstraps
         the first time, since this host is already in the DB via UniFi/nmap
         discovery under its network identity.

    Deliberately does not guess on more than one candidate: writing a serial
    onto the wrong asset is worse than writing none at all.
    """
    if serial:
        asset = session.exec(select(Asset).where(Asset.serial_number == serial)).first()
        if asset:
            return asset, [asset.id]

    candidate_ids: set[int] = set()
    for mac in local_macs():
        asset = _find_asset_by_mac(session, mac)
        if asset:
            candidate_ids.add(asset.id)

    if len(candidate_ids) == 1:
        asset_id = next(iter(candidate_ids))
        return session.get(Asset, asset_id), [asset_id]
    return None, sorted(candidate_ids)


def run_local_host_discovery(session: Session) -> dict:
    """Collects this host's own hardware identity and writes it onto its
    matching Asset row, if exactly one can be found. Never raises -- a
    unsupported platform, a failed hardware read, or an unresolved match are
    all reported in the returned summary rather than treated as an error, so a
    developer running this from a laptop that isn't in the inventory sees a
    clean no-op rather than a failed run."""
    hw = collect_local_hardware()
    if hw is None:
        return {
            "status": "skipped",
            "reason": "unsupported platform, or the hardware read failed",
        }

    asset, candidate_ids = find_this_host_asset(session, hw.get("serial_number"))
    if asset is None:
        reason = (
            "no asset found matching this host's MAC addresses"
            if not candidate_ids
            else f"ambiguous match: candidate asset ids {candidate_ids}"
        )
        return {"status": "no_match", "reason": reason, "hardware": hw}

    if not asset.identity_locked:
        if hw.get("serial_number"):
            asset.serial_number = hw["serial_number"]
        if hw.get("model_number"):
            asset.model_number = hw["model_number"]
        if hw.get("model_identifier"):
            asset.model_identifier = hw["model_identifier"]
    if hw.get("model"):
        asset.model = hw["model"]
    asset.last_seen = utcnow_naive()
    session.add(asset)
    session.commit()
    return {"status": "updated", "asset_id": asset.id, "locked": asset.identity_locked}
