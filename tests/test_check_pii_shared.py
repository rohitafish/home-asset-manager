"""scripts/check-pii.sh is a shared engine: byte-identical in this repo and in
the sibling named by PII_SIBLING, with everything repo-specific in
scripts/check-pii.conf beside it.

These tests are the thing that makes that arrangement real rather than
aspirational. The two copies were hand-synced for a while and three fixes
reached one repo and not the other -- including a silent false-clean, the
worst failure this particular tool has -- so "remember to copy it" is known
not to work.

The drift test skips when the sibling is not on this machine, which is the
normal case in CI: a runner clones one repo. That is a real limit and worth
stating plainly -- this check lives on the developer's machine, where both
repos exist and where pushes originate, and the pre-push hook runs the suite.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE = REPO_ROOT / 'scripts' / 'check-pii.sh'
CONF = REPO_ROOT / 'scripts' / 'check-pii.conf'


def _conf_value(name):
    """The value of a plain KEY="value" assignment in the conf."""
    m = re.search(rf'^{name}="([^"]*)"', CONF.read_text(), re.MULTILINE)
    return m.group(1) if m else None


def _sibling_engine():
    rel = _conf_value('PII_SIBLING')
    if not rel:
        return None
    path = (REPO_ROOT / rel).resolve() / 'scripts' / 'check-pii.sh'
    return path if path.is_file() else None


def test_the_conf_names_a_sibling():
    """Anti-vacuity guard: without PII_SIBLING the drift test below would skip
    forever and silently stop protecting anything."""
    assert _conf_value('PII_SIBLING'), 'PII_SIBLING is not set in scripts/check-pii.conf'


def test_the_engine_matches_the_siblings_copy():
    sibling = _sibling_engine()
    if sibling is None:
        pytest.skip('sibling repo not present on this machine (normal in CI)')

    assert ENGINE.read_bytes() == sibling.read_bytes(), (
        f'scripts/check-pii.sh has drifted from {sibling}. The engine is meant '
        'to be byte-identical in both repos -- run scripts/sync-check-pii.sh to '
        'see the diff, then --push or --pull, and run BOTH suites. Do not fix '
        'this by editing one copy to match: the change belongs in both.'
    )


def test_every_setting_the_conf_defines_is_one_the_engine_reads():
    """A typo'd key (PII_UK_RULE=1 for PII_UK_RULES=1) is silent: the conf
    still sources cleanly, the engine keeps its default, and a rule the repo
    believes is on is off. Catch it by name rather than by behaviour."""
    engine = ENGINE.read_text()
    known = set(re.findall(r'^(PII_[A-Z_]+)=', engine, re.MULTILINE))
    assert known, 'no PII_* defaults found in the engine -- has it been restructured?'

    configured = set(re.findall(r'^(PII_[A-Z_]+)=', CONF.read_text(), re.MULTILINE))
    unknown = configured - known

    assert not unknown, (
        f'scripts/check-pii.conf sets {sorted(unknown)}, which the engine never '
        'reads -- a typo here disables a rule silently. Known settings: '
        f'{sorted(known)}'
    )


def test_the_engine_carries_no_literal_that_trips_its_own_rules():
    """The engine is committed to every repo that vendors it, so a public-
    looking IP or a date-shaped string in one of its comments becomes a finding
    in each of their histories -- which is exactly what happened once."""
    text = ENGINE.read_text()

    quads = [
        q for q in re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text)
        if not re.match(r'^(10\.|127\.|192\.168\.|169\.254\.|0\.0\.0\.0)', q)
    ]
    assert not quads, (
        f'public-looking IP literal(s) in the engine: {sorted(set(quads))} -- '
        'describe the address instead, or move it to a conf allowlist entry.'
    )

    # The UK sort-code shape, which repos handling correspondence enable.
    dates = re.findall(r'\b\d{2}-\d{2}-\d{2}\b', text)
    assert not dates, (
        f'date-shaped string(s) in the engine: {sorted(set(dates))} -- these '
        'match the sort-code rule wherever PII_UK_RULES=1. Write the month out.'
    )
