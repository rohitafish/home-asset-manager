"""Scores pairs of assets that might be the same physical device (e.g. one
wired NIC and one wireless NIC on the same box), and links/unlinks them via
a non-destructive CIRelationship row.

This is deliberately NOT the same thing as the existing destructive
Duplicates/merge flow (app/asset_merge.py): merging collapses two asset rows
into one and deletes the loser (right when discovery genuinely created two
rows for the same interface); linking keeps both rows and both histories
(right when a device legitimately has two distinct identities -- wired MAC
and wireless MAC -- worth tracking separately).
"""

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from itertools import combinations

from sqlmodel import Session, select

from app.clock import utcnow_naive
from app.models import (
    Asset,
    AssetInterface,
    AssetType,
    CIRelationship,
    SameDeviceDismissal,
)
from discovery.normalize import (
    _HOSTNAME_VENDOR_KEYWORDS,
    _IOT_VENDORS,
    _NETWORK_VENDORS,
)

SAME_DEVICE = "same_physical_device"

# Words that appear in a hostname but say nothing about which *specific*
# device it is: interface/role qualifiers ("texe-eth" vs "texe-wifi"), and
# brand/owner words that are expected to recur across many unrelated
# devices in the same household ("Apple Mac mini Alex" and "Apple Watch
# 10 Alex" share "apple" and "alex" without being remotely the same
# device). Both are excluded before any hostname-similarity signal is
# scored -- see _hostname_tokens.
_ROLE_TOKENS = {
    "wifi", "wlan", "eth", "lan", "wired", "wireless", "2", "5g", "local", "localdomain",
    "vpn", "port",
}

# A word that recurs across this many *different* assets' hostnames is
# household vocabulary (a room name, an annotation like "(confirmed)", a
# generic product-category word like "clock" on multiple Echo Dots) rather
# than anything identifying a specific device -- computed fresh from the
# live inventory each time (see find_same_device_candidates), the same
# principle as the owner-name exclusion but for any recurring word, not
# just names.
_COMMON_WORD_THRESHOLD = 3


def _words(phrase: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", phrase.lower()) if w}


def _brand_stopwords() -> set[str]:
    """Vendor/product words to exclude from hostname comparisons, derived
    from discovery/normalize.py's existing vendor-keyword lists rather than
    a second hand-maintained copy -- that module is already the single
    source of truth for "this word denotes a brand/product line, not a
    specific device"."""
    words: set[str] = set()
    for keyword, vendor in _HOSTNAME_VENDOR_KEYWORDS:
        words |= _words(keyword) | _words(vendor)
    for vendor in (*_IOT_VENDORS, *_NETWORK_VENDORS):
        words |= _words(vendor)
    return words


_BRAND_STOPWORDS = _brand_stopwords()


def _hostname_tokens(hostname: str | None, stopwords: set[str]) -> set[str]:
    if not hostname:
        return set()
    return {w for w in _words(hostname) if w not in stopwords and w not in _ROLE_TOKENS}


def _meaningful_tokens(hostname: str | None, stopwords: set[str]) -> set[str]:
    """Like _hostname_tokens but deliberately does NOT strip common/owner
    words -- only brand/vendor (passed in), role tokens, and pure numbers are
    removed. The containment signal in _score_pair relies on a rare
    *combination* of otherwise-common words (e.g. {uu, laptop}, where each word
    alone recurs across the household but the pair is unique to one device), so
    it must keep words that _hostname_tokens would drop."""
    return {
        w for w in _words(hostname or "")
        if w not in stopwords and w not in _ROLE_TOKENS and not w.isdigit()
    }


def _is_locally_administered(mac: str) -> bool:
    """True for randomized/private MAC addresses (e.g. Apple's per-network
    private Wi-Fi address), which set the locally-administered bit -- see
    discovery/normalize.py's guess_vendor_from_hostname docstring for why
    these can never match a real OUI. Numeric adjacency between two such
    MACs is coincidence, not evidence, so they're excluded from that signal
    entirely rather than scored."""
    try:
        first_octet = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first_octet & 0x02)


def _mac_int(mac: str) -> int | None:
    # discovery/normalize.py's normalize_mac passes non-12-hex-digit input
    # through verbatim rather than rejecting it, so AssetInterface.mac is
    # NOT guaranteed to be canonical hex by the time it reaches here -- a
    # collector artifact (e.g. an incomplete/placeholder MAC-ish string)
    # would otherwise raise ValueError straight out of _score_pair, 500ing
    # the whole /assets/investigate page from one bad row. Same guard shape
    # as _is_locally_administered just above.
    try:
        return int(mac.replace(":", ""), 16)
    except ValueError:
        return None


@dataclass
class Signal:
    label: str
    points: int


@dataclass
class Candidate:
    asset_a: Asset
    asset_b: Asset
    score: int
    signals: list[Signal] = field(default_factory=list)


def _score_pair(
    a: Asset, b: Asset, ifaces_a: list, ifaces_b: list, base_stopwords: set[str]
) -> tuple[int, list[Signal]]:
    # Network infrastructure (APs, switches, gateways) is the one asset
    # category where owning several identical-model units is the *expected*
    # normal case, not a coincidence worth flagging -- a mesh network is
    # built from repeated hardware on purpose (e.g. two "U7-Pro-Wall" APs,
    # one per room). Same model name, same vendor, and even a shared/nearby
    # MAC block (manufacturers often assign near-sequential MACs to units
    # from the same batch purchase) are all just as likely between two
    # entirely separate physical units as within one, so none of that
    # evidence is meaningful here -- unlike an end-user device's wired vs
    # wireless NIC, which genuinely does tend to share an adjacent MAC.
    # Skip same-device scoring for this pair type entirely rather than
    # trying to tune a threshold that only happens to work for however many
    # identical units exist today.
    if a.asset_type == AssetType.network_device and b.asset_type == AssetType.network_device:
        return 0, []

    signals: list[Signal] = []
    score = 0

    macs_a = [i.mac for i in ifaces_a if i.mac]
    macs_b = [i.mac for i in ifaces_b if i.mac]
    for mac_a in macs_a:
        for mac_b in macs_b:
            if _is_locally_administered(mac_a) or _is_locally_administered(mac_b):
                continue
            if mac_a[:8].lower() != mac_b[:8].lower():
                continue
            int_a, int_b = _mac_int(mac_a), _mac_int(mac_b)
            if int_a is None or int_b is None:
                continue
            if abs(int_a - int_b) <= 4:
                signals.append(Signal(
                    f"Same OUI, MACs {mac_a} / {mac_b} differ by ≤4 — the classic "
                    "wired+wireless dual-NIC pattern", 45,
                ))
                score += 45
            else:
                signals.append(Signal(f"Same OUI ({mac_a[:8]}) but not numerically adjacent", 5))
                score += 5

    # Vendor words are stripped per-pair (not just from the global brand
    # list) so a vendor string that isn't in discovery/normalize.py's
    # keyword lists (e.g. "TP-Link Systems") still doesn't get credited as
    # hostname "similarity" -- that overlap is already captured, more
    # appropriately, by the weak +10 "same vendor" signal below.
    pair_stopwords = base_stopwords
    if a.vendor:
        pair_stopwords = pair_stopwords | _words(a.vendor)
    if b.vendor:
        pair_stopwords = pair_stopwords | _words(b.vendor)

    tokens_a = _hostname_tokens(a.hostname, pair_stopwords)
    tokens_b = _hostname_tokens(b.hostname, pair_stopwords)
    # A leftover token like "10" (from "iPad 10" vs "Apple Watch ... 10" --
    # a generation/series number, not an identity) can be all that's left
    # once brand/owner words are stripped, and would otherwise "match
    # exactly" between two totally unrelated devices. Require the same
    # minimum content the ratio comparison below already requires.
    substantial = min(len("".join(tokens_a)), len("".join(tokens_b))) >= 4 if tokens_a and tokens_b else False
    if tokens_a and tokens_b and substantial:
        if tokens_a == tokens_b:
            signals.append(Signal(
                f'Hostnames match once vendor/owner/role words are stripped '
                f'("{a.hostname}" / "{b.hostname}")', 30,
            ))
            score += 30
        else:
            joined_a = "".join(sorted(tokens_a))
            joined_b = "".join(sorted(tokens_b))
            ratio = difflib.SequenceMatcher(None, joined_a, joined_b).ratio()
            if ratio >= 0.6 and min(len(joined_a), len(joined_b)) >= 4:
                points = round(30 * ratio)
                signals.append(Signal(
                    f'Similar hostnames after stripping vendor/owner/role words '
                    f'("{a.hostname}" / "{b.hostname}", {ratio:.0%} match)', points,
                ))
                score += points
            # else: below the similarity bar -- deliberately no fallback credit
            # for merely sharing one word. That fallback used to award +15 for
            # any 4+ letter word in common (e.g. "clock", "office", "confirmed"),
            # which fires on any two hostnames that happen to share one
            # incidental word even when everything else about them differs --
            # exactly the false-positive pattern this scoring is trying to avoid.
    # else: once vendor/owner/role words are stripped, one or both hostnames
    # carry no distinguishing information -- say nothing rather than guess.

    conn_a = {i.connection_type for i in ifaces_a if i.connection_type}
    conn_b = {i.connection_type for i in ifaces_b if i.connection_type}

    # Hostname containment across distinct NICs. The block above strips
    # common/owner words, which zeroes a pair like "acme laptop" / "dana's acme
    # laptop" -- "acme" and "laptop" each recur across the household, so both
    # get stripped, yet their *combination* is unique to this one machine (seen
    # as one wired + one wireless interface). Score that combination instead:
    # keep common/owner words (_meaningful_tokens), and credit the pair only
    # when one hostname's words fully contain the other's *and* the two rows
    # are a different connection type each. The connection-type mismatch is the
    # load-bearing guard against false positives: two *different* IoT units with
    # near-identical names (the "three Echo Dot Clocks" case) are each a single
    # wireless interface, so this never fires for them.
    brand_vendor = _BRAND_STOPWORDS | _words(a.vendor or "") | _words(b.vendor or "")
    mt_a = _meaningful_tokens(a.hostname, brand_vendor)
    mt_b = _meaningful_tokens(b.hostname, brand_vendor)
    shared = mt_a & mt_b
    smaller = mt_a if len(mt_a) <= len(mt_b) else mt_b
    if (
        len(shared) >= 2 and mt_a != mt_b and shared == smaller
        and conn_a and conn_b and conn_a != conn_b
    ):
        signals.append(Signal(
            f'One hostname\'s words fully contain the other\'s, on distinct NICs '
            f'("{a.hostname}" / "{b.hostname}", shared {sorted(shared)})', 30,
        ))
        score += 30

    # Wired-vs-wireless is only meaningful as *corroboration* of some other
    # positive signal above (MAC adjacency, hostname match) -- on its own it
    # is true of most random pairs in any household with a mix of wired and
    # wireless devices, so it must never be able to qualify a pair by itself.
    if score > 0 and conn_a and conn_b and conn_a != conn_b:
        signals.append(Signal(
            f"One wired, one wireless ({', '.join(sorted(conn_a))} vs {', '.join(sorted(conn_b))})", 20,
        ))
        score += 20

    if a.vendor and b.vendor and a.vendor.lower() == b.vendor.lower():
        signals.append(Signal(f"Same vendor ({a.vendor})", 10))
        score += 10

    ips_a = {i.ip for i in ifaces_a if i.ip}
    ips_b = {i.ip for i in ifaces_b if i.ip}
    if ips_a and ips_b and ips_a == ips_b:
        signals.append(Signal(
            "Currently share the same IP — likely a reconciliation artifact, not a dual-NIC device", -40,
        ))
        score -= 40

    return score, signals


def _asset_identity_fingerprint(asset: Asset, ifaces: list[AssetInterface]) -> str:
    macs = sorted((i.mac or "").lower() for i in ifaces if i.mac)
    # Deliberately NOT ip: DHCP churn would otherwise silently re-offer every
    # dismissed pair. Deliberately NOT score: the common-word stopword set in
    # find_same_device_candidates depends on *all* assets' hostnames, so an
    # unrelated third device can shift an untouched pair's score.
    return "\x1e".join([
        asset.asset_type.value,
        (asset.hostname or "").strip().lower(),
        (asset.vendor or "").strip().lower(),
        "|".join(macs),
    ])


def _pair_fingerprint(
    asset_a: Asset, asset_b: Asset, ifaces_a: list[AssetInterface], ifaces_b: list[AssetInterface]
) -> str:
    """Hashes the identity fields the scorer reads for this pair. Ordered by
    asset id so the result doesn't depend on which asset is passed as a/b.
    Used to tell a genuinely-unchanged dismissed pair (SameDeviceDismissal)
    from one worth re-offering after an edit."""
    lo, lo_ifaces, hi, hi_ifaces = (
        (asset_a, ifaces_a, asset_b, ifaces_b) if asset_a.id < asset_b.id
        else (asset_b, ifaces_b, asset_a, ifaces_a)
    )
    combined = (
        _asset_identity_fingerprint(lo, lo_ifaces) + "\x1d" + _asset_identity_fingerprint(hi, hi_ifaces)
    )
    return hashlib.sha256(combined.encode()).hexdigest()


def dismiss_same_device_candidate(session: Session, asset_id_a: int, asset_id_b: int) -> bool:
    """Records that a same-device candidate pair was judged NOT the same
    device, so find_same_device_candidates() stops offering it -- until
    either asset's identity fields change (see SameDeviceDismissal). Upserts:
    re-dismissing an already-dismissed pair refreshes its fingerprint rather
    than hitting the unique constraint. Returns False if either asset id
    doesn't exist, or the two ids are equal. Does not commit."""
    if asset_id_a == asset_id_b:
        return False
    lo, hi = sorted((asset_id_a, asset_id_b))
    asset_a = session.get(Asset, lo)
    asset_b = session.get(Asset, hi)
    if not asset_a or not asset_b:
        return False
    ifaces_a = session.exec(select(AssetInterface).where(AssetInterface.asset_id == lo)).all()
    ifaces_b = session.exec(select(AssetInterface).where(AssetInterface.asset_id == hi)).all()
    fingerprint = _pair_fingerprint(asset_a, asset_b, ifaces_a, ifaces_b)

    existing = session.exec(
        select(SameDeviceDismissal).where(
            SameDeviceDismissal.asset_id_a == lo,
            SameDeviceDismissal.asset_id_b == hi,
        )
    ).first()
    if existing:
        existing.evidence_fingerprint = fingerprint
        existing.dismissed_at = utcnow_naive()
        session.add(existing)
    else:
        session.add(SameDeviceDismissal(asset_id_a=lo, asset_id_b=hi, evidence_fingerprint=fingerprint))
    return True


def find_same_device_candidates(session: Session, min_score: int = 20) -> list[Candidate]:
    assets = session.exec(select(Asset)).all()
    interfaces = session.exec(select(AssetInterface)).all()
    ifaces_by_asset: dict[int, list] = {}
    for iface in interfaces:
        ifaces_by_asset.setdefault(iface.asset_id, []).append(iface)

    # Household member names (e.g. "Alex", "Jordan Lee") show up
    # verbatim in a lot of device hostnames but obviously don't indicate two
    # devices are the same physical box -- pulled from the actual owner
    # values in use rather than hardcoding names, so it stays correct as
    # owners change.
    owner_stopwords: set[str] = set()
    for owner in {a.owner for a in assets if a.owner}:
        owner_stopwords |= _words(owner)

    # Any word that shows up in _COMMON_WORD_THRESHOLD or more distinct
    # assets' hostnames is this household's naming vocabulary, not a device
    # identifier -- e.g. "clock" on three different Echo Dots, "office"/
    # "snug" as room names reused across otherwise-unrelated devices.
    word_to_assets: dict[str, set[int]] = {}
    for asset in assets:
        for word in _words(asset.hostname or ""):
            word_to_assets.setdefault(word, set()).add(asset.id)
    common_word_stopwords = {
        w for w, ids in word_to_assets.items() if len(ids) >= _COMMON_WORD_THRESHOLD
    }

    base_stopwords = (
        _BRAND_STOPWORDS | owner_stopwords | common_word_stopwords
    )  # _hostname_tokens excludes _ROLE_TOKENS too

    already_linked = set()
    for rel in session.exec(
        select(CIRelationship).where(CIRelationship.relationship_type == SAME_DEVICE)
    ).all():
        already_linked.add(frozenset((rel.asset_id, rel.related_asset_id)))

    # Pair -> fingerprint at dismissal time. A pair is only skipped while its
    # current identity fingerprint still matches -- see dismiss_same_device_
    # candidate and _pair_fingerprint.
    dismissed_fingerprints: dict[frozenset, str] = {
        frozenset((d.asset_id_a, d.asset_id_b)): d.evidence_fingerprint
        for d in session.exec(select(SameDeviceDismissal)).all()
    }

    candidates = []
    for a, b in combinations(assets, 2):
        pair_key = frozenset((a.id, b.id))
        if pair_key in already_linked:
            continue
        ifaces_a = ifaces_by_asset.get(a.id, [])
        ifaces_b = ifaces_by_asset.get(b.id, [])
        stored_fingerprint = dismissed_fingerprints.get(pair_key)
        if stored_fingerprint is not None and stored_fingerprint == _pair_fingerprint(
            a, b, ifaces_a, ifaces_b
        ):
            continue
        score, signals = _score_pair(a, b, ifaces_a, ifaces_b, base_stopwords)
        if score >= min_score:
            candidates.append(Candidate(asset_a=a, asset_b=b, score=score, signals=signals))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def link_assets(
    session: Session, asset_id_a: int, asset_id_b: int, detail: str | None = None
) -> bool:
    """Records that two assets are the same physical device without
    deleting either -- writes both directions so the existing
    `where(CIRelationship.asset_id == asset_id)` queries on each asset's
    detail page show the link without needing an OR clause.

    Returns whether the two assets are (now, or already) linked -- False
    only for the invalid asset_id_a == asset_id_b case. Callers that write
    a "Linked to asset #N" note or otherwise report success must check
    this and skip that reporting when it's False: without it, a self-link
    silently no-ops here while the caller still announces a link that was
    never created."""
    if asset_id_a == asset_id_b:
        return False
    existing = session.exec(
        select(CIRelationship).where(
            CIRelationship.asset_id == asset_id_a,
            CIRelationship.related_asset_id == asset_id_b,
            CIRelationship.relationship_type == SAME_DEVICE,
        )
    ).first()
    if existing:
        return True
    session.add(CIRelationship(
        asset_id=asset_id_a, related_asset_id=asset_id_b,
        relationship_type=SAME_DEVICE, detail=detail,
    ))
    session.add(CIRelationship(
        asset_id=asset_id_b, related_asset_id=asset_id_a,
        relationship_type=SAME_DEVICE, detail=detail,
    ))
    return True


def unlink_assets(session: Session, asset_id: int, relationship_id: int) -> None:
    """Deletes both directions of a link, given one of its two
    CIRelationship rows (as rendered on that asset's detail page)."""
    rel = session.get(CIRelationship, relationship_id)
    if not rel or rel.asset_id != asset_id:
        return
    reverse = session.exec(
        select(CIRelationship).where(
            CIRelationship.asset_id == rel.related_asset_id,
            CIRelationship.related_asset_id == rel.asset_id,
            CIRelationship.relationship_type == rel.relationship_type,
        )
    ).first()
    session.delete(rel)
    if reverse:
        session.delete(reverse)


def remove_same_device_link(session: Session, asset_id_a: int, asset_id_b: int) -> bool:
    """Removes the same-physical-device link between two assets (both mirror
    rows), keeping both assets -- to dismiss a suggested duplicate that's
    actually two separate devices. Returns True if a link was found. Matches
    only the a<->b link (both endpoints must be in {a, b}), so a link either
    asset has with some *other* asset is untouched. Does not commit."""
    rel = session.exec(
        select(CIRelationship).where(
            CIRelationship.relationship_type == SAME_DEVICE,
            CIRelationship.asset_id.in_((asset_id_a, asset_id_b)),
            CIRelationship.related_asset_id.in_((asset_id_a, asset_id_b)),
        )
    ).first()
    if not rel:
        return False
    unlink_assets(session, rel.asset_id, rel.id)  # deletes this row + its mirror
    return True
