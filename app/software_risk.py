"""Red/amber/green cyber-risk grade for one installed piece of software.

Computed at render time from the facts stored on SoftwareInstall (never
stored itself), so an age threshold crossed overnight or an end-of-life date
passing shows up without a re-import. Every grade carries its reasons; the
Software tab shows them, because a colour nobody can explain is not useful.

The three things graded, and where each comes from:

  Vulnerabilities  the machine's own scanner. Homebrew formulae: `brew vulns`
                   (OSV), open findings only -- ones a formula patch already
                   fixes are not counted. Linux: a pending update from the
                   distribution's security pocket. Mac apps, casks, snaps,
                   flatpaks and Windows programs have no feed here.
  Supportability   end of life (endoflife.date), Homebrew deprecated or
                   disabled, apt packages no repository offers any more,
                   Intel-only Mac apps, and any pending update.
  Age              publication date of the installed version -- or, where
                   none is known, its install date, which gives a minimum
                   age (a version can't be published after it's installed).

RED      high/critical vulnerability, security update pending, end of life,
         disabled by Homebrew, or published over AGE_RED_YEARS ago.
AMBER    other known vulnerabilities, any other pending update, deprecated,
         orphaned, Intel-only, end of life within EOL_WARN_DAYS, or older
         than AGE_AMBER_YEARS.
GREEN    none of the above, with at least something to judge by.
UNRATED  nothing to judge by at all (no date, no scanner, no update source).

Distribution packages (apt) get two allowances, because the distribution
backports security fixes into old upstream versions: age only raises them to
amber, and only past AGE_RED_YEARS; and an upstream end-of-life is amber,
not red. Their real security signal is the security-pocket update.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

RED = "red"
AMBER = "amber"
GREEN = "green"
UNRATED = "unrated"

# Lower is worse; used to pick an item's worst grade and to sort by risk.
RAG_ORDER = {RED: 0, AMBER: 1, UNRATED: 2, GREEN: 3}
RAG_LABELS = {RED: "Red", AMBER: "Amber", GREEN: "Green", UNRATED: "Unrated"}

AGE_AMBER_YEARS = 2
AGE_RED_YEARS = 5
EOL_WARN_DAYS = 180
HIGH_SEVERITIES = {"CRITICAL", "HIGH"}
DISTRO_SOURCES = {"apt"}
# Sources whose package manager reports pending updates, so "no update
# pending" is itself information.
UPDATE_AWARE_SOURCES = {"apt", "brew", "brew-cask"}
# endoflife.date marks some cycles end-of-life without a date.
EOL_UNKNOWN_DATE = date(1970, 1, 1)

# What each source is checked against -- shown on the Software tab so a
# green row isn't read as "scanned and clean" when it wasn't scanned.
SOURCE_COVERAGE = {
    "brew": "Homebrew formula: vulnerabilities from brew vulns (OSV), updates, deprecation, age",
    "brew-cask": "Homebrew cask: updates, deprecation; no vulnerability feed",
    "app": "Mac app: age, Intel-only, end of life; no vulnerability feed",
    "apt": "Linux package: pending security updates, other updates, orphaned packages, age",
    "snap": "Snap: no update, vulnerability or date data yet",
    "flatpak": "Flatpak app: no update, vulnerability or date data yet",
    "windows": "Windows program: file date, end of life, updates if WinGet is present; no vulnerability feed",
    "appx": "Microsoft Store app: end of life only; no vulnerability feed",
}

SOURCE_LABELS = {
    "brew": "Homebrew",
    "brew-cask": "Homebrew cask",
    "app": "Mac app",
    "apt": "apt",
    "snap": "snap",
    "flatpak": "flatpak",
    "windows": "Windows",
    "appx": "Store app",
}


@dataclass(frozen=True)
class Grade:
    rag: str
    reasons: tuple[str, ...]


def version_tuple(version: str | None) -> tuple[int, ...]:
    """Leading numeric components: "3.12.4rc1" -> (3, 12, 4)."""
    out = []
    for part in (version or "").split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        out.append(int(digits))
        if len(digits) != len(part):
            break
    return tuple(out)


def upstream_version(version: str | None, source: str) -> str | None:
    """The upstream part of a package version: apt "1:2.43.0-1ubuntu7.3" ->
    "2.43.0", Homebrew "1.11.1_4" -> "1.11.1"."""
    if not version:
        return None
    v = version.strip()
    if source in DISTRO_SOURCES:
        if ":" in v:
            v = v.split(":", 1)[1]
        if "-" in v:
            v = v.rsplit("-", 1)[0]
        for sep in ("+", "~"):
            v = v.split(sep, 1)[0]
    elif source == "brew":
        head, _, rev = v.rpartition("_")
        if head and rev.isdigit():
            v = head
    return v or None


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word[:-1]}ies" if word.endswith("y") else f"{n} {word}s"


def _age_years(published: date, today: date) -> float:
    return (today - published).days / 365.25


def grade_install(inst, today: date) -> Grade:
    """Grade one SoftwareInstall (or any object with the same attributes)."""
    red: list[str] = []
    amber: list[str] = []
    distro = inst.source in DISTRO_SOURCES

    vulns = None
    if inst.vulns_json is not None:
        try:
            vulns = json.loads(inst.vulns_json)
        except ValueError:
            vulns = None
    if vulns:
        high = [v for v in vulns if v.get("severity") in HIGH_SEVERITIES]
        other = [v for v in vulns if v.get("severity") not in HIGH_SEVERITIES]
        fixed = any(v.get("fixed") for v in vulns)
        suffix = "; a fixed release exists" if fixed else ""
        if high:
            ids = ", ".join(v["id"] for v in high[:3]) + (" ..." if len(high) > 3 else "")
            red.append(f"{_plural(len(high), 'high/critical vulnerability')} ({ids}){suffix}")
        if other:
            ids = ", ".join(v["id"] for v in other[:3]) + (" ..." if len(other) > 3 else "")
            amber.append(
                f"{_plural(len(other), 'known vulnerability' if not high else 'other known vulnerability')}"
                f" (medium/low/unrated: {ids})" + ("" if high else suffix)
            )

    if inst.security_update:
        red.append(f"Security update pending: {inst.version} -> {inst.latest_version}")
    elif inst.update_available:
        amber.append(
            f"Update available: {inst.version} -> {inst.latest_version}"
            if inst.latest_version else "Update available"
        )

    if inst.disabled:
        red.append("Disabled by Homebrew: no longer maintained there")
    elif inst.deprecated:
        amber.append("Deprecated by Homebrew: due to be disabled")
    if inst.orphaned:
        amber.append("No configured repository offers it any more, so it gets no updates")
    if inst.intel_only:
        amber.append("Intel-only app: runs under Rosetta, which Apple is phasing out")

    if inst.eol_date is not None:
        cycle = f" ({inst.eol_cycle})" if inst.eol_cycle else ""
        if inst.eol_date <= today:
            when = "" if inst.eol_date == EOL_UNKNOWN_DATE else f" since {inst.eol_date.isoformat()}"
            if distro:
                amber.append(f"Upstream end of life{cycle}{when}; the distribution may still backport fixes")
            else:
                red.append(f"End of life{cycle}{when}")
        elif (inst.eol_date - today).days <= EOL_WARN_DAYS:
            amber.append(f"End of life{cycle} on {inst.eol_date.isoformat()}")

    if (
        not distro
        and inst.source != "brew"  # brew compares against its own formula
        and not inst.update_available
        and inst.upstream_latest
    ):
        installed = version_tuple(upstream_version(inst.version, inst.source))
        latest = version_tuple(inst.upstream_latest)
        if len(installed) >= 2 and len(latest) >= 2 and installed < latest:
            amber.append(f"Newer release in the same series: {inst.upstream_latest}")

    # No publication date, but an install date: the version can't have been
    # published after it was installed, so its age is at least this.
    age_from = inst.published_date or getattr(inst, "installed_date", None)
    if age_from is not None:
        years = _age_years(age_from, today)
        if inst.published_date is not None:
            label = f"Installed version published {age_from.isoformat()} ({years:.1f} years ago)"
        else:
            label = (f"Installed {age_from.isoformat()}, so this version is at least "
                     f"{years:.1f} years old")
        if distro:
            if years > AGE_RED_YEARS:
                amber.append(label)
        elif years > AGE_RED_YEARS:
            red.append(label)
        elif years > AGE_AMBER_YEARS:
            amber.append(label)

    if red:
        return Grade(RED, tuple(red + amber))
    if amber:
        return Grade(AMBER, tuple(amber))
    has_basis = (
        age_from is not None
        or vulns is not None
        or inst.eol_date is not None
        or inst.source in UPDATE_AWARE_SOURCES
    )
    if not has_basis:
        return Grade(UNRATED, ("No publication date, vulnerability or update data available",))
    return Grade(GREEN, ("No known issues",))


def worst(rags) -> str:
    rags = list(rags)
    if not rags:
        return UNRATED
    return min(rags, key=lambda r: RAG_ORDER[r])
