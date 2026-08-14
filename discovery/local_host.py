"""One-off collector for the hardware this app happens to be running on --
serial number, model identifier and model number straight from
`system_profiler`. This is the same data Mactracker's My Models shows for a
Mac, read directly from the OS instead: Mactracker's own database turned out
to be unreadable for this purpose (its bundled model DB is encrypted, and its
My Models sync files are behind macOS TCC over SSH -- see README).

Unlike the UniFi collector, this can only ever describe *this* host -- there
is no way to query hardware identity for a Mac elsewhere on the network. In
practice that means running this on the dev machine reports the dev
machine's own hardware, not the deployed host's; the useful run is the one
triggered on the Mini itself, where the app actually lives.

Finding "this host"'s existing asset row is a matching problem, not a given
-- see find_this_host_asset().
"""

import json
import logging
import platform
import re
import subprocess

from sqlmodel import Session, select

from app.clock import utcnow_naive
from app.models import Asset
from discovery.reconcile import _find_asset_by_mac

logger = logging.getLogger(__name__)


def collect_local_hardware() -> dict | None:
    """Returns {"serial_number", "model_identifier", "model_number", "model"}
    read from this Mac's own system_profiler, or None if this isn't macOS or
    the read failed for any reason. Never raises."""
    if platform.system() != "Darwin":
        return None
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
        # "not macOS" already returned above; reaching here means
        # system_profiler timed out / errored / changed shape. Log so those
        # aren't indistinguishable from a legitimately-absent read.
        logger.warning("system_profiler hardware read failed", exc_info=True)
        return None
    return {
        "serial_number": hw.get("serial_number"),
        "model_identifier": hw.get("machine_model"),  # e.g. "Macmini9,1"
        "model_number": hw.get("model_number"),  # e.g. "MGNR3B/A"
        "model": hw.get("machine_name"),  # e.g. "Mac mini"
    }


_ETHER_RE = re.compile(r"ether\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.IGNORECASE)


def local_macs() -> list[str]:
    """This host's own MAC addresses, lowercase colon-separated, excluding
    locally-administered/randomized addresses. macOS synthesizes many of
    these for internal interfaces (anpiN, awdlN, llwN, bridge0, private Wi-Fi
    addressing) -- none of which will ever appear in UniFi or nmap data, so
    matching on them would only produce false negatives, never a real join."""
    try:
        result = subprocess.run(
            ["ifconfig", "-a"], capture_output=True, text=True, timeout=10, check=True
        )
    except Exception:
        logger.warning("ifconfig read failed; no local MACs collected", exc_info=True)
        return []
    macs = []
    for match in _ETHER_RE.finditer(result.stdout):
        mac = match.group(1).lower()
        if mac == "00:00:00:00:00:00":
            continue
        first_octet = int(mac.split(":")[0], 16)
        if first_octet & 0x02:  # locally administered bit set -- synthetic/randomized
            continue
        macs.append(mac)
    return macs


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
    non-macOS host, a system_profiler failure, or an unresolved match are all
    reported in the returned summary rather than treated as an error, so a
    developer running this from a laptop that isn't in the inventory sees a
    clean no-op rather than a failed run."""
    hw = collect_local_hardware()
    if hw is None:
        return {"status": "skipped", "reason": "not macOS, or system_profiler unavailable"}

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
