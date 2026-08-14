"""Pins the documented false-positive carve-outs in app/correlate.py --
"the densest logic in the repo, zero I/O" per the review that added this
suite, and every carve-out here is a false positive someone hit and fixed.
Nothing currently stops a re-tune from silently undoing one of these.
"""

from conftest import make_asset, make_interface
from sqlmodel import select

from app.correlate import (
    _BRAND_STOPWORDS,
    SAME_DEVICE,
    _score_pair,
    find_same_device_candidates,
    link_assets,
    remove_same_device_link,
)
from app.models import Asset, AssetInterface, AssetType, CIRelationship


def _asset(**overrides):
    defaults = dict(asset_type=AssetType.end_user_device)
    defaults.update(overrides)
    return Asset(**defaults)


def _iface(**overrides):
    defaults = dict(asset_id=1)
    defaults.update(overrides)
    return AssetInterface(**defaults)


def test_network_device_pairs_never_score():
    """Mesh APs/switches of the same model are the expected normal case,
    not evidence of being the same physical unit -- skip entirely."""
    a = _asset(asset_type=AssetType.network_device, hostname="U7-Pro-Wall-1", vendor="Ubiquiti")
    b = _asset(asset_type=AssetType.network_device, hostname="U7-Pro-Wall-1", vendor="Ubiquiti")
    ifaces_a = [_iface(mac="24:5a:4c:00:00:01")]
    ifaces_b = [_iface(mac="24:5a:4c:00:00:02")]

    score, signals = _score_pair(a, b, ifaces_a, ifaces_b, set())

    assert score == 0
    assert signals == []


def test_locally_administered_macs_excluded_from_adjacency():
    """Randomized/private MAC addresses (locally-administered bit set) can
    coincidentally land close together -- that's not evidence of a shared
    physical device, so they must never earn the MAC-adjacency signal."""
    a = _asset()
    b = _asset()
    ifaces_a = [_iface(mac="02:11:22:33:44:55")]
    ifaces_b = [_iface(mac="02:11:22:33:44:56")]

    score, signals = _score_pair(a, b, ifaces_a, ifaces_b, set())

    assert score == 0
    assert signals == []


def test_mac_adjacency_scores_when_not_locally_administered():
    """Sanity check for the positive case the exclusion above is carved out
    of: a real OUI with numerically adjacent MACs is the classic wired+
    wireless dual-NIC signal."""
    a = _asset()
    b = _asset()
    ifaces_a = [_iface(mac="24:5a:4c:00:00:01", connection_type="wired")]
    ifaces_b = [_iface(mac="24:5a:4c:00:00:02", connection_type="wireless")]

    score, signals = _score_pair(a, b, ifaces_a, ifaces_b, set())

    # +45 MAC adjacency, +20 wired-vs-wireless corroboration (only unlocked
    # because score is already > 0).
    assert score == 65
    labels = [s.label for s in signals]
    assert any("wired+wireless" in label for label in labels)
    assert any("wired" in label and "wireless" in label for label in labels)


def test_brand_and_owner_words_stripped_prevents_false_match():
    """"Apple Watch Alex" and "Alex Mac Mini" share two words -- both are
    brand/owner vocabulary, not device identity, so stripping them must
    leave nothing to match on."""
    a = _asset(hostname="Apple Watch Alex")
    b = _asset(hostname="Alex Mac Mini")
    base_stopwords = _BRAND_STOPWORDS | {"alex"}

    score, signals = _score_pair(a, b, [], [], base_stopwords)

    assert score == 0
    assert signals == []


def test_role_tokens_stripped_enables_match_across_interface_qualifiers():
    """"texe-eth" and "texe-wifi" name the same device's two NICs -- the
    interface/role qualifier must be stripped so the shared "texe" still
    counts as a hostname match, rather than the differing suffix hiding it."""
    a = _asset(hostname="texe-eth")
    b = _asset(hostname="texe-wifi")

    score, signals = _score_pair(a, b, [], [], set())

    assert score == 30
    assert any("Hostnames match" in s.label for s in signals)


def test_same_ip_penalty_reduces_score():
    """Two interfaces sharing one IP right now is much more likely to be a
    reconciliation artifact than a genuine dual-NIC device, and must pull
    the score down rather than being ignored."""
    a = _asset()
    b = _asset()
    ifaces_a = [_iface(mac="24:5a:4c:00:00:01", ip="192.168.1.50")]
    ifaces_b = [_iface(mac="24:5a:4c:00:00:03", ip="192.168.1.50")]

    score, signals = _score_pair(a, b, ifaces_a, ifaces_b, set())

    assert score == 45 - 40
    assert any("reconciliation artifact" in s.label for s in signals)


def test_hostname_containment_across_distinct_nics_scores():
    """A laptop discovered twice -- once via its built-in Wi-Fi, once via an
    external Ethernet NIC -- shares no MAC and no IP. Its two hostnames differ
    only by qualifiers ("zeta laptop" vs "Dana's zeta laptop"), and both "zeta"
    and "laptop" recur across the household, so the old path strips them to
    nothing. The containment signal must still fire on the {zeta, laptop}
    combination because one name's words are a subset of the other's and the
    two rows are different NIC types."""
    a = _asset(hostname="zeta laptop", vendor="HP")
    b = _asset(hostname="Dana's zeta laptop", vendor="HP")
    ifaces_a = [_iface(connection_type="wired")]
    ifaces_b = [_iface(connection_type="wireless")]
    # base_stopwords strips exactly what the live inventory would ("zeta" is
    # also an owner value, "laptop" recurs >= 3 times), proving the fix works
    # despite the stripping that currently zeroes this pair.
    base_stopwords = {"zeta", "laptop", "dana"}

    score, signals = _score_pair(a, b, ifaces_a, ifaces_b, base_stopwords)

    # +30 containment, +20 wired-vs-wireless corroboration, +10 same vendor.
    assert score == 60
    assert any("fully contain" in s.label for s in signals)


def test_containment_requires_distinct_nic_types():
    """The connection-type mismatch is the guard against the documented Echo-
    Dot false positive: two *different* IoT units with near-identical names are
    each a single wireless interface, so hostname containment must NOT link
    them on name alone."""
    a = _asset(hostname="kitchen echo dot")
    b = _asset(hostname="kitchen echo dot clock")
    ifaces_a = [_iface(connection_type="wireless")]
    ifaces_b = [_iface(connection_type="wireless")]

    _, signals = _score_pair(a, b, ifaces_a, ifaces_b, set())

    assert not any("fully contain" in s.label for s in signals)


def test_find_candidates_links_laptop_split_across_nics(session):
    """End-to-end regression for the reported bug: one physical laptop seen as
    two rows (wired + wireless NIC) must surface as a same-device candidate,
    while a phone that merely shares the household "zeta" tag and a different
    laptop that merely shares "laptop" must not be linked to it."""
    laptop_wired = make_asset(session, hostname="zeta laptop", vendor="HP", owner="Dana")
    laptop_wifi = make_asset(session, hostname="Dana's zeta laptop", vendor="HP", owner="zeta")
    phone = make_asset(
        session, hostname="Samsung Galaxy zeta phone", vendor="Samsung",
        owner="Dana", asset_type=AssetType.mobile,
    )
    other_laptop = make_asset(session, hostname="Win11 PC laptop Owl", vendor="HP", owner="Ellis")
    # MACs below are fabricated -- their only role here is to be distinct so no
    # two rows share a NIC; correlation in this test is driven by hostname/vendor.
    make_interface(session, laptop_wired.id, mac="aa:bb:cc:00:00:01", connection_type="wired")
    make_interface(session, laptop_wifi.id, mac="aa:bb:cc:00:00:02", connection_type="wireless")
    make_interface(session, phone.id, mac="aa:bb:cc:00:00:03", connection_type="wireless")
    make_interface(session, other_laptop.id, mac="aa:bb:cc:00:00:04", connection_type="wireless")

    candidates = find_same_device_candidates(session, min_score=20)
    pairs = {frozenset((c.asset_a.id, c.asset_b.id)) for c in candidates}

    assert frozenset((laptop_wired.id, laptop_wifi.id)) in pairs
    for other in (phone.id, other_laptop.id):
        assert frozenset((laptop_wired.id, other)) not in pairs
        assert frozenset((laptop_wifi.id, other)) not in pairs


def test_common_word_threshold_excludes_household_vocabulary(session):
    """A word recurring across >= 3 assets' hostnames (a room name, a
    repeated product-category word) is household vocabulary, not a device
    identifier, and must not itself qualify a pair -- while a word unique to
    exactly one pair still should."""
    for hostname in ("kitchen-echo", "kitchen-plug", "kitchen-monitor"):
        make_asset(session, hostname=hostname)
    gaming_a = make_asset(session, hostname="gaming-pc-wifi")
    gaming_b = make_asset(session, hostname="gaming-pc-eth")
    make_interface(session, gaming_a.id, connection_type="wireless")
    make_interface(session, gaming_b.id, connection_type="wired")

    candidates = find_same_device_candidates(session, min_score=20)
    pairs = [frozenset((c.asset_a.id, c.asset_b.id)) for c in candidates]

    kitchen_ids = {a.id for a in session.exec(
        select(Asset).where(Asset.hostname.like("kitchen-%"))
    ).all()}
    assert not any(pair <= kitchen_ids for pair in pairs)
    assert frozenset((gaming_a.id, gaming_b.id)) in pairs


# -- remove_same_device_link (the Duplicates page "Dismiss" action) ----------


def _same_device_rows(session):
    return session.exec(
        select(CIRelationship).where(CIRelationship.relationship_type == SAME_DEVICE)
    ).all()


def test_remove_same_device_link_deletes_both_mirror_rows(session):
    a = make_asset(session)
    b = make_asset(session)
    link_assets(session, a.id, b.id, detail="linked in error")
    session.commit()
    assert len(_same_device_rows(session)) == 2  # A->B and B->A

    removed = remove_same_device_link(session, a.id, b.id)
    session.commit()

    assert removed is True
    assert _same_device_rows(session) == []


def test_remove_same_device_link_returns_false_when_unlinked(session):
    a = make_asset(session)
    b = make_asset(session)

    assert remove_same_device_link(session, a.id, b.id) is False
    assert _same_device_rows(session) == []


def test_remove_same_device_link_leaves_other_links_intact(session):
    a = make_asset(session)
    b = make_asset(session)
    c = make_asset(session)
    link_assets(session, a.id, b.id)
    link_assets(session, a.id, c.id)
    session.commit()

    remove_same_device_link(session, a.id, b.id)  # dismiss only a<->b
    session.commit()

    remaining = {frozenset((r.asset_id, r.related_asset_id)) for r in _same_device_rows(session)}
    assert remaining == {frozenset((a.id, c.id))}  # a<->c survives
