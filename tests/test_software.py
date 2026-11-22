"""Software inventory: collectors' parsers, the importer, the RAG grading
rules, the endoflife.date enrichment and the Software tab's routes.

Identifiers are fabricated (00:1a:2b:3c:4d:xx MACs, generic hostnames), per
AGENTS.md's PII rules.
"""

import gzip
import importlib.util
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import make_asset, make_interface
from sqlmodel import select
from typer.testing import CliRunner

from app.asset_children import delete_asset_cascade
from app.models import DiscoveryRun, SoftwareHost, SoftwareInstall, SoftwareItem
from app.software_risk import (
    AMBER,
    EOL_UNKNOWN_DATE,
    GREEN,
    RED,
    UNRATED,
    grade_install,
    upstream_version,
    version_tuple,
    worst,
)
from discovery import cli, software_eol
from discovery.software_import import (
    SoftwareImportError,
    classify,
    import_inventory,
    software_key,
)

TODAY = date(2026, 11, 22)
REPO = Path(__file__).resolve().parent.parent


def _load_collector():
    spec = importlib.util.spec_from_file_location(
        "collect_unix", REPO / "scripts/software-inventory/collect_unix.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load_collector()


# ------------------------------------------------------------ match keys


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Mozilla Firefox (x64 en-GB)", "firefox"),
        ("Firefox.app", "firefox"),
        ("firefox", "firefox"),
        ("python@3.12", "python 3.12"),
        ("python3.12", "python 3.12"),
        ("Python 3.12.4 (64-bit)", "python 3.12"),
        ("7-Zip 24.08 (x64)", "7-zip"),
        ("openssl@3", "openssl 3"),
        ("Google Chrome", "google chrome"),
        ("google-chrome-stable", "google chrome"),
        ("code", "visual studio code"),
        ("Git", "git"),
        ("node@20", "node 20"),
    ],
)
def test_software_key_matches_across_sources(name, key):
    assert software_key(name) == key


# ------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("rec", "expected"),
    [
        ({"source": "apt", "name": "linux-image-9.9.9-1-generic"}, None),
        ({"source": "apt", "name": "linux-firmware"}, None),
        ({"source": "apt", "name": "base-files", "priority": "required"}, None),
        ({"source": "apt", "name": "kali-linux-headless"}, None),
        ({"source": "apt", "name": "bash", "essential": True}, "system"),
        ({"source": "apt", "name": "systemd", "priority": "important"}, "system"),
        ({"source": "apt", "name": "libssl3t64", "section": "libs"}, "library"),
        ({"source": "apt", "name": "python3-requests", "section": "python"}, "library"),
        ({"source": "apt", "name": "fonts-dejavu-core", "section": "fonts"}, "library"),
        ({"source": "apt", "name": "libreoffice-writer", "section": "editors"}, "application"),
        ({"source": "apt", "name": "libvpx-tools", "section": "video"}, "application"),
        ({"source": "apt", "name": "nmap", "section": "net"}, "application"),
        ({"source": "apt", "name": "git", "priority": "optional", "section": "vcs"}, "application"),
        ({"source": "brew", "name": "git", "explicit": True}, "application"),
        ({"source": "brew", "name": "gmp", "explicit": False}, "library"),
        ({"source": "windows", "name": "Microsoft Windows 11 Home"}, None),
        ({"source": "windows", "name": "Microsoft Visual C++ 2015-2022 Redistributable (x64)"}, "library"),
        ({"source": "windows", "name": "Mozilla Firefox (x64 en-GB)"}, "application"),
        ({"source": "snap", "name": "core22", "category_hint": "system"}, "system"),
        ({"source": "windows", "name": "Microsoft Update Health Tools"}, "system"),
        ({"source": "appx", "name": "Microsoft.BingWeather", "publisher": "Microsoft Corporation"}, "system"),
        ({"source": "appx", "name": "26720RandomSaladGamesLLC.SimpleSolitaire", "publisher": "Random Salad"}, "application"),
        ({"source": "app", "name": "Firefox"}, "application"),
    ],
)
def test_classify(rec, expected):
    assert classify(rec) == expected


# ------------------------------------------------------------ grading


def _inst(**kw):
    defaults = dict(
        source="brew", version="1.0", published_date=None, update_available=False,
        latest_version=None, security_update=False, deprecated=False, disabled=False,
        orphaned=False, intel_only=False, vulns_json=None, eol_cycle=None, eol_date=None,
        upstream_latest=None, upstream_latest_date=None, category="application",
        installed_date=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_current_brew_formula_is_green():
    g = grade_install(_inst(published_date=date(2026, 6, 1), vulns_json="[]"), TODAY)
    assert g.rag == GREEN


def test_high_vulnerability_is_red_and_names_it():
    vulns = json.dumps([{"id": "CVE-2026-0001", "severity": "HIGH", "fixed": True}])
    g = grade_install(_inst(vulns_json=vulns), TODAY)
    assert g.rag == RED
    assert "CVE-2026-0001" in g.reasons[0] and "fixed release" in g.reasons[0]


def test_medium_vulnerability_is_amber():
    vulns = json.dumps([{"id": "OSV-2026-1", "severity": "MEDIUM", "fixed": False},
                        {"id": "OSV-2026-2", "severity": "UNKNOWN", "fixed": False}])
    g = grade_install(_inst(vulns_json=vulns), TODAY)
    assert g.rag == AMBER
    assert g.reasons[0].startswith("2 known vulnerabilities")


def test_security_update_is_red():
    g = grade_install(_inst(source="apt", update_available=True, security_update=True,
                            version="1.0-1", latest_version="1.0-2"), TODAY)
    assert g.rag == RED
    assert "Security update pending" in g.reasons[0]


def test_plain_update_is_amber():
    g = grade_install(_inst(update_available=True, latest_version="2.0"), TODAY)
    assert (g.rag, g.reasons) == (AMBER, ("Update available: 1.0 -> 2.0",))


@pytest.mark.parametrize(
    ("published", "expected"),
    [(date(2025, 6, 1), GREEN), (date(2023, 6, 1), AMBER), (date(2020, 6, 1), RED)],
)
def test_age_thresholds(published, expected):
    assert grade_install(_inst(source="app", published_date=published), TODAY).rag == expected


@pytest.mark.parametrize(
    ("published", "expected"),
    [(date(2023, 6, 1), GREEN), (date(2020, 6, 1), AMBER)],
)
def test_distro_packages_get_the_age_allowance(published, expected):
    assert grade_install(_inst(source="apt", published_date=published), TODAY).rag == expected


def test_end_of_life_is_red_but_amber_for_distro_packages():
    past = date(2026, 1, 1)
    assert grade_install(_inst(source="app", eol_date=past, eol_cycle="3.9"), TODAY).rag == RED
    distro = grade_install(_inst(source="apt", eol_date=past), TODAY)
    assert distro.rag == AMBER and "backport" in distro.reasons[0]


def test_end_of_life_without_a_date_reads_cleanly():
    g = grade_install(_inst(source="app", eol_date=EOL_UNKNOWN_DATE), TODAY)
    assert g.rag == RED and g.reasons == ("End of life",)


def test_end_of_life_soon_is_amber():
    g = grade_install(_inst(source="app", eol_date=date(2027, 2, 1)), TODAY)
    assert g.rag == AMBER and "2027-02-01" in g.reasons[0]


@pytest.mark.parametrize(
    ("field", "expected"),
    [("disabled", RED), ("deprecated", AMBER), ("orphaned", AMBER), ("intel_only", AMBER)],
)
def test_supportability_flags(field, expected):
    assert grade_install(_inst(**{field: True}), TODAY).rag == expected


def test_newer_release_in_series_is_amber_for_non_distro():
    g = grade_install(_inst(source="windows", version="3.12.1", upstream_latest="3.12.8"), TODAY)
    assert g.rag == AMBER and "3.12.8" in g.reasons[0]
    # A distribution's backport carries an old upstream number on purpose.
    assert grade_install(_inst(source="apt", version="3.12.1-1", upstream_latest="3.12.8"), TODAY).rag == GREEN


def test_install_date_gives_a_minimum_age():
    g = grade_install(_inst(source="windows", installed_date=date(2018, 9, 29)), TODAY)
    assert g.rag == RED and "at least 8.1 years old" in g.reasons[0]
    assert grade_install(_inst(source="windows", installed_date=date(2026, 9, 1)), TODAY).rag == GREEN


def test_nothing_to_judge_by_is_unrated():
    assert grade_install(_inst(source="snap"), TODAY).rag == UNRATED


def test_worst_and_versions():
    assert worst([GREEN, AMBER, UNRATED]) == AMBER
    assert worst([]) == UNRATED
    assert version_tuple("3.12.4rc1") == (3, 12, 4)
    assert upstream_version("1:9.8.7-1ubuntu1.2", "apt") == "9.8.7"
    assert upstream_version("1.4+dfsg-2", "apt") == "1.4"
    assert upstream_version("1.11.1_4", "brew") == "1.11.1"
    assert upstream_version(None, "app") is None


# ------------------------------------------------------------ importer


def _doc(packages, macs=("00:1a:2b:3c:4d:01",), hostname="testhost", platform="linux"):
    return {
        "collector_version": 1,
        "platform": platform,
        "hostname": hostname,
        "os": {"name": "Linux Mint", "version": "22.3"},
        "macs": list(macs),
        "packages": packages,
        "notes": ["apt package lists last refreshed 2026-11-22"],
    }


GIT_APT = {"source": "apt", "name": "git", "version": "1:9.8.7-1ubuntu1.2", "section": "vcs",
           "priority": "optional", "published": "2026-10-16",
           "published_source": "distro package changelog"}


def test_import_matches_by_mac_and_drops_the_os(session):
    asset = make_asset(session, hostname="Server")
    make_interface(session, asset.id, mac="00:1a:2b:3c:4d:01")
    summary = import_inventory(session, _doc([
        GIT_APT,
        {"source": "apt", "name": "linux-image-9.9.9-1-generic", "version": "9.9.9-1"},
    ]))
    assert summary["asset_id"] == asset.id
    assert (summary["imported"], summary["excluded_os"]) == (1, 1)
    inst = session.exec(select(SoftwareInstall)).one()
    assert inst.published_date == date(2026, 10, 16)
    host = session.exec(select(SoftwareHost)).one()
    assert (host.asset_id, host.os_name, host.package_count) == (asset.id, "Linux Mint", 1)


def test_same_software_on_two_machines_is_one_item(session):
    a = make_asset(session, hostname="A")
    b = make_asset(session, hostname="B")
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    make_interface(session, b.id, mac="00:1a:2b:3c:4d:02")
    import_inventory(session, _doc([GIT_APT]))
    import_inventory(session, _doc(
        [{"source": "brew", "name": "git", "version": "2.47.1", "explicit": True,
          "vulns": [{"id": "CVE-2026-9", "severity": "LOW", "fixed": False}]}],
        macs=["00:1a:2b:3c:4d:02"], platform="darwin",
    ))
    items = session.exec(select(SoftwareItem)).all()
    assert [i.key for i in items] == ["git"]
    installs = session.exec(select(SoftwareInstall)).all()
    assert {i.asset_id for i in installs} == {a.id, b.id}
    brew = next(i for i in installs if i.source == "brew")
    assert json.loads(brew.vulns_json) == [{"id": "CVE-2026-9", "severity": "LOW", "fixed": False}]


def test_reimport_removes_uninstalled_software_and_keeps_first_seen(session):
    a = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    nmap = {"source": "apt", "name": "nmap", "version": "7.94", "section": "net"}
    import_inventory(session, _doc([GIT_APT, nmap]))
    git_first = session.exec(select(SoftwareInstall).where(SoftwareInstall.package_name == "git")).one()
    first_seen = git_first.first_seen
    git_first.eol_cycle = "9.8"
    session.add(git_first)
    session.commit()

    newer = dict(GIT_APT, version="1:9.8.7-1ubuntu1.3")
    summary = import_inventory(session, _doc([newer]))
    assert (summary["removed"], summary["items_pruned"]) == (1, 1)
    git = session.exec(select(SoftwareInstall)).one()
    assert git.first_seen == first_seen
    assert git.eol_cycle is None  # the old version's release cycle no longer applies
    assert [i.key for i in session.exec(select(SoftwareItem)).all()] == ["git"]


def test_implausible_dates_are_treated_as_unknown(session):
    a = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    import_inventory(session, _doc([
        {"source": "app", "name": "Zeroed", "version": "1", "published": "1980-01-01T00:00:00Z"},
        {"source": "app", "name": "Future", "version": "1", "published": "2999-01-01"},
        {"source": "app", "name": "Garbled", "version": "1", "published": "not-a-date"},
    ], platform="darwin"))
    assert {i.published_date for i in session.exec(select(SoftwareInstall)).all()} == {None}


def test_store_apps_get_readable_names(session):
    a = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    import_inventory(session, _doc([
        {"source": "appx", "name": "26720RandomSaladGamesLLC.SimpleSolitaire", "version": "7.5"},
        {"source": "appx", "name": "AppleInc.iCloud", "display_name": "iCloud", "version": "15.10"},
    ], platform="windows"))
    assert sorted(i.name for i in session.exec(select(SoftwareItem)).all()) == ["Simple Solitaire", "iCloud"]


def test_display_name_prefers_the_capitalised_form(session):
    a = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    import_inventory(session, _doc([
        {"source": "brew-cask", "name": "firefox", "version": "148.0"},
        {"source": "app", "name": "Firefox", "version": "148.0", "published": "2026-11-01"},
    ], platform="darwin"))
    assert session.exec(select(SoftwareItem)).one().name == "Firefox"


def test_explicit_asset_id_and_hostname_fallback(session):
    a = make_asset(session)
    import_inventory(session, _doc([GIT_APT], macs=[]), asset_id=a.id)
    # Next time, no MAC matches (a rotated private address), but the
    # hostname recorded by the first import does.
    summary = import_inventory(session, _doc([GIT_APT], macs=["00:1a:2b:3c:4d:99"]))
    assert summary["asset_id"] == a.id


def test_import_refuses_to_guess(session):
    a = make_asset(session)
    b = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    make_interface(session, b.id, mac="00:1a:2b:3c:4d:02")
    with pytest.raises(SoftwareImportError, match="several assets"):
        import_inventory(session, _doc([], macs=["00:1a:2b:3c:4d:01", "00:1a:2b:3c:4d:02"]))
    with pytest.raises(SoftwareImportError, match="no asset matches"):
        import_inventory(session, _doc([], macs=["00:1a:2b:3c:4d:77"], hostname="nowhere"))
    with pytest.raises(SoftwareImportError, match="no asset with id"):
        import_inventory(session, _doc([]), asset_id=9999)


@pytest.mark.parametrize("bad", [[], {"platform": "beos", "packages": []}, {"platform": "linux"}])
def test_import_validates_shape(session, bad):
    with pytest.raises(SoftwareImportError):
        import_inventory(session, bad)


def test_import_skips_junk_records(session):
    a = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    summary = import_inventory(session, _doc(["x", {"source": "apt"}, {"source": "rpm", "name": "x"}, GIT_APT, GIT_APT]))
    assert (summary["skipped"], summary["imported"]) == (3, 1)


def test_deleting_an_asset_removes_its_software(session):
    a = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    import_inventory(session, _doc([GIT_APT]))
    delete_asset_cascade(session, a.id)
    session.commit()
    assert session.exec(select(SoftwareInstall)).all() == []
    assert session.exec(select(SoftwareHost)).all() == []


def test_cli_software_import_reads_stdin_and_tracks_a_run(session, engine, monkeypatch):
    monkeypatch.setattr(cli, "engine", engine)
    a = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    result = CliRunner().invoke(cli.app, ["software-import"], input=json.dumps(_doc([GIT_APT])))
    assert result.exit_code == 0, result.output
    assert session.exec(select(DiscoveryRun)).one().source == "software"

    bad = CliRunner().invoke(cli.app, ["software-import"], input=json.dumps(_doc([], macs=["00:1a:2b:3c:4d:55"], hostname="x")))
    assert bad.exit_code == 1
    unreadable = CliRunner().invoke(cli.app, ["software-import"], input="not json")
    assert unreadable.exit_code == 2


# ------------------------------------------------------------ endoflife.date

PYTHON_CYCLES = [
    {"cycle": "3.13", "releaseDate": "2024-10-07", "eol": "2029-10-31", "latest": "3.13.9", "latestReleaseDate": "2026-10-01"},
    {"cycle": "3.12", "releaseDate": "2023-10-02", "eol": "2028-10-31", "latest": "3.12.12", "latestReleaseDate": "2026-10-01"},
    {"cycle": "3.8", "releaseDate": "2019-10-14", "eol": True, "latest": "3.8.20", "latestReleaseDate": "2024-09-06"},
]


def test_candidate_products_and_cycle_match():
    products = {"python", "chrome", "ubuntu", "nodejs"}
    assert software_eol.candidate_products("python 3.12", products) == ["python"]
    assert software_eol.candidate_products("google chrome", products) == ["chrome"]
    assert software_eol.candidate_products("node", products) == ["nodejs"]
    assert software_eol.candidate_products("ubuntu", products) == []
    assert software_eol.match_cycle("3.12.4", PYTHON_CYCLES)["cycle"] == "3.12"
    assert software_eol.match_cycle("3.1.0", PYTHON_CYCLES) is None
    assert software_eol.match_cycle(None, PYTHON_CYCLES) is None


def test_apply_cycle_fills_a_missing_publication_date():
    inst = SoftwareInstall(software_id=1, asset_id=1, source="windows", package_name="Python",
                           version="3.12.12")
    assert software_eol.apply_cycle(inst, PYTHON_CYCLES[1])
    assert (inst.eol_date, inst.upstream_latest) == (date(2028, 10, 31), "3.12.12")
    assert inst.published_date == date(2026, 10, 1)
    assert inst.published_source == "endoflife.date release date"
    eol = SoftwareInstall(software_id=1, asset_id=1, source="app", package_name="x", version="3.8.0")
    software_eol.apply_cycle(eol, PYTHON_CYCLES[2])
    assert eol.eol_date == EOL_UNKNOWN_DATE and eol.published_date == date(2019, 10, 14)
    # Unmatched later: the borrowed date goes with the cycle.
    software_eol.apply_cycle(inst, None)
    assert inst.eol_date is None and inst.published_date is None


def test_apply_cycle_corrects_a_bundle_date_older_than_the_release():
    inst = SoftwareInstall(software_id=1, asset_id=1, source="app", package_name="Chrome",
                           version="3.12.5", published_date=date(2021, 1, 1),
                           published_source="app bundle date (approx.)")
    software_eol.apply_cycle(inst, PYTHON_CYCLES[1])
    assert inst.published_date == date(2023, 10, 2)
    assert inst.published_source == "endoflife.date: release of this series"
    exact = SoftwareInstall(software_id=1, asset_id=1, source="brew", package_name="python@3.12",
                            version="3.12.5", published_date=date(2021, 1, 1),
                            published_source="Homebrew formula date")
    software_eol.apply_cycle(exact, PYTHON_CYCLES[1])
    assert exact.published_date == date(2021, 1, 1)  # not an approximation: left alone


def test_enrich_software_eol_against_a_fake_api(session, monkeypatch):
    monkeypatch.setattr(software_eol, "REQUEST_PACING_SECONDS", 0)
    a = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    import_inventory(session, _doc([
        {"source": "apt", "name": "python3.12", "version": "3.12.3-1ubuntu0.8", "section": "python"},
        {"source": "apt", "name": "nmap", "version": "7.94", "section": "net"},
    ]))
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path.endswith("/all.json"):
            return httpx.Response(200, json=["python", "nmap-not-here"])
        if request.url.path.endswith("/python.json"):
            return httpx.Response(200, json=PYTHON_CYCLES)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = software_eol.enrich_software_eol(session, client=client)
    assert summary["items_matched"] == 1 and summary["fetch_errors"] == 0
    py = session.exec(select(SoftwareInstall).where(SoftwareInstall.package_name == "python3.12")).one()
    assert (py.eol_cycle, py.eol_date) == ("3.12", date(2028, 10, 31))
    assert session.exec(select(SoftwareItem).where(SoftwareItem.key == "python 3.12")).one().eol_product == "python"
    assert requested == ["/api/all.json", "/api/python.json"]


def test_enrich_survives_a_failed_product_fetch(session, monkeypatch):
    monkeypatch.setattr(software_eol, "REQUEST_PACING_SECONDS", 0)
    a = make_asset(session)
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    import_inventory(session, _doc([{"source": "apt", "name": "python3.12", "version": "3.12.3-1", "section": "python"}]))

    def handler(request):
        if request.url.path.endswith("/all.json"):
            return httpx.Response(200, json=["python"])
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = software_eol.enrich_software_eol(session, client=client)
    assert summary["fetch_errors"] == 1 and summary["items_matched"] == 0


# ------------------------------------------------------------ collector parsers


def test_parse_dpkg_keeps_held_packages_and_skips_removed():
    text = (
        "git\t1:9.8.7-1\tgit\toptional\tvcs\tno\tii \tfast, scalable VCS\n"
        "example-dkms\t1.2-1\texample\toptional\tkernel\tno\thi \tdriver\n"
        "oldthing\t1.0\t\toptional\tmisc\tno\trc \tgone\n"
        "libc6\t2.39-0\tglibc (2.39-0)\trequired\tlibs\tno\tii \tGNU C Library\n"
        "libc6\t2.39-0\tglibc (2.39-0)\trequired\tlibs\tno\tii \tGNU C Library\n"
    )
    pkgs = collector.parse_dpkg(text)
    assert set(pkgs) == {"git", "example-dkms", "libc6"}
    assert pkgs["libc6"]["source_package"] == "glibc"


def test_parse_apt_lists():
    up = collector.parse_apt_upgradable(
        "Listing...\n"
        "examplessl/stable-updates,stable-security 9.9.1-1example2 amd64 [upgradable from: 9.9.1-1example1]\n"
        "examplefw/stable-updates 20990101.1 amd64 [upgradable from: 20990101.0]\n"
    )
    assert up == {"examplessl": ("9.9.1-1example2", True),
                  "examplefw": ("20990101.1", False)}
    local = collector.parse_apt_installed_local(
        "Listing...\nfoo/now 1.0 amd64 [installed,local]\nbar/stable,now 2.0 amd64 [installed]\n"
    )
    assert local == {"foo"}


def test_changelog_date_reads_only_the_date(tmp_path):
    pkg = tmp_path / "git"
    pkg.mkdir()
    with gzip.open(pkg / "changelog.Debian.gz", "wt") as fh:
        fh.write("git (1:9.8.7-1ubuntu1.2) stable-security; urgency=medium\n\n  * fix\n\n"
                 " -- A Maintainer <maintainer@example.com>  Fri, 16 Oct 2026 10:11:45 -0400\n")
    assert collector.changelog_date("git", doc_dir=str(tmp_path)) == "2026-10-16"
    assert collector.changelog_date("missing", doc_dir=str(tmp_path)) is None


def test_parse_brew_info_and_vulns():
    vulns = collector.parse_brew_vulns({"findings": [
        {"formula": "examplelib", "vulnerabilities": [
            {"id": "OSV-2099-0001", "severity": "UNKNOWN", "fixed_versions": []}],
         "patched": [{"id": "CVE-2099-0002", "severity": "HIGH", "fixed_versions": ["abc"]}]},
    ]})
    assert vulns == {"examplelib": [{"id": "OSV-2099-0001", "severity": "UNKNOWN", "fixed": False}]}
    info = {
        "formulae": [
            {"name": "examplelib", "desc": "C library", "homepage": "https://example.org",
             "versions": {"stable": "1.11.1"}, "outdated": True, "deprecated": False, "disabled": False,
             "installed": [{"version": "1.2.3_4", "time": 1700000000, "installed_on_request": False}]},
            {"name": "git", "versions": {"stable": "2.47.1"}, "outdated": False,
             "installed": [{"version": "2.47.1", "installed_on_request": True}]},
        ],
        "casks": [{"token": "firefox", "name": ["Firefox"], "installed": "148.0", "version": "149.0",
                   "outdated": True, "installed_time": 1710000000}],
    }
    recs = collector.parse_brew_info(info, {"examplelib": {"source_modified_time": 1690000000}}, vulns)
    ssh, git, cask = recs
    assert (ssh["published"], ssh["explicit"], ssh["update_available"]) == ("2023-07-22", False, True)
    assert ssh["vulns"][0]["id"] == "OSV-2099-0001" and git["vulns"] == []
    assert (cask["source"], cask["display_name"], cask["latest_version"]) == ("brew-cask", "Firefox", "149.0")
    # No `brew vulns` at all: formulae carry no vulns key ("not scanned").
    assert "vulns" not in collector.parse_brew_info(info)[0]


def test_parse_macos_apps_keeps_installed_apps_only():
    apps = [
        {"_name": "Firefox", "path": "/Applications/Firefox.app", "version": "148.0",
         "lastModified": "2026-11-01T10:00:00Z",
         "signed_by": ["Developer ID Application: Mozilla Corporation (43AQ936H96)"],
         "arch_kind": "arch_arm_i64"},
        {"_name": "OldTool", "path": "/Users/someone/Applications/OldTool.app", "version": "1.0",
         "arch_kind": "arch_i64"},
        {"_name": "Helper", "path": "/Applications/Firefox.app/Contents/Helper.app"},
        {"_name": "Mail", "path": "/System/Applications/Mail.app"},
    ]
    recs = collector.parse_macos_apps(apps, home="/Users/someone")
    assert [r["name"] for r in recs] == ["Firefox", "OldTool"]
    assert recs[0]["publisher"] == "Mozilla Corporation" and recs[0]["published"] == "2026-11-01"
    assert recs[1]["intel_only"] is True


def test_parse_snap_and_flatpak():
    snaps = collector.parse_snap_list(
        "Name    Version   Rev   Tracking       Publisher   Notes\n"
        "core22  20240111  1122  latest/stable  canonical✓  base\n"
        "vlc     3.0.20    3777  latest/stable  videolan✓   -\n"
    )
    assert [(s["name"], s["category_hint"]) for s in snaps] == [("core22", "system"), ("vlc", None)]
    assert snaps[1]["publisher"] == "videolan"
    flat = collector.parse_flatpak_list("org.mozilla.firefox\tFirefox\t148.0\n\n")
    assert flat == [{"source": "flatpak", "name": "org.mozilla.firefox", "display_name": "Firefox", "version": "148.0"}]


# ------------------------------------------------------------ routes


@pytest.fixture()
def populated_software(session):
    a = make_asset(session, hostname="Laptop A")
    b = make_asset(session, hostname="Server B")
    make_interface(session, a.id, mac="00:1a:2b:3c:4d:01")
    make_interface(session, b.id, mac="00:1a:2b:3c:4d:02")
    import_inventory(session, _doc([
        {"source": "brew", "name": "git", "version": "2.40.0", "explicit": True,
         "published": "2026-05-01", "update_available": True, "latest_version": "2.47.1", "vulns": []},
        {"source": "app", "name": "=cmd", "version": "1.0", "published": "2019-01-01"},
        {"source": "brew", "name": "gmp", "version": "6.3.0", "explicit": False, "vulns": []},
    ], platform="darwin"))
    import_inventory(session, _doc([GIT_APT], macs=["00:1a:2b:3c:4d:02"]))
    return a, b


def test_software_page_empty(admin_client, session):
    resp = admin_client.get("/software")
    assert resp.status_code == 200
    assert "No software collected yet" in resp.text


def test_software_page_lists_one_row_per_item_with_machines(admin_client, populated_software):
    resp = admin_client.get("/software")
    assert resp.status_code == 200
    body = resp.text
    assert body.count("<strong>git</strong>") == 1
    assert "Laptop A" in body and "Server B" in body
    assert "Update available: 2.40.0 -&gt; 2.47.1 (Laptop A)" in body
    assert "gmp" not in body  # a library: hidden by default
    assert "gmp" in admin_client.get("/software?show=all").text


def test_software_page_filters(admin_client, populated_software):
    _a, b = populated_software
    only_b = admin_client.get(f"/software?machine={b.id}").text
    assert "<strong>git</strong>" in only_b and "=cmd" not in only_b
    red = admin_client.get("/software?rag=red").text
    assert "=cmd" in red and "<strong>git</strong>" not in red
    assert "=cmd" not in admin_client.get("/software?q=git").text
    for sort in ("name", "published", "machines", "bogus"):
        assert admin_client.get(f"/software?sort={sort}&rag=nonsense&machine=x").status_code == 200


def test_software_csv_neutralises_formulas(admin_client, populated_software):
    resp = admin_client.get("/software.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "'=cmd" in resp.text and "=cmd," not in resp.text.replace("'=cmd", "")


def test_refresh_eol_route(admin_client, session, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "run_software_eol", lambda: called.append(1) or {})
    resp = admin_client.post("/software/refresh-eol", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/software?eol=done"
    assert called == [1]
    assert "End-of-life data refreshed" in admin_client.get("/software?eol=done").text

    session.add(DiscoveryRun(source="unifi", status="running"))
    session.commit()
    busy = admin_client.post("/software/refresh-eol", follow_redirects=False)
    assert busy.headers["location"] == "/software?eol=busy"


def test_refresh_eol_route_reports_failure(admin_client, monkeypatch):
    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(cli, "run_software_eol", boom)
    resp = admin_client.post("/software/refresh-eol", follow_redirects=False)
    assert resp.headers["location"] == "/software?eol=failed"
