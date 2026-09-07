"""Pins the decisions in aws-status.sh that would fail silently if reversed.

A status script has a specific failure mode: it is only ever read when
someone is already worried, and a check that wrongly reports `ok` is worse
than no check, because it ends the investigation. Each assertion here
corresponds to a way this script has already been wrong once.
"""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "aws-status.sh"
BODY = SCRIPT.read_text(encoding="utf-8")
CODE = "\n".join(
    line for line in BODY.splitlines() if not line.lstrip().startswith("#")
)


def test_daily_lookup_is_scoped_to_the_dump_prefix():
    """`daily/` alone would report a non-backup as the newest backup.

    The prefix holds more than dumps: a one-off permission probe was written
    there on 2026-09-07 to prove PutObject, and being the most recent object,
    it was reported as the newest daily backup. Object Lock means such an
    object cannot be removed for 30 days, so the prefix must carry the
    filter rather than the bucket being kept pristine.
    """
    assert "--prefix daily/assetmgt-" in CODE
    assert "--prefix daily/ " not in CODE


def test_object_listing_does_not_use_s3_ls():
    """`s3 ls` exits 1 for both "empty" and "denied" -- see backup-db.sh."""
    assert "list-objects-v2" in CODE
    assert "aws s3 ls" not in CODE


def test_reports_every_problem_rather_than_stopping_at_the_first():
    """Same contract as preflight.sh: one pass, every finding."""
    assert "set -e" not in CODE
    assert 'printf %s "$FAILS"' not in CODE  # summary must count, not guess


def test_never_echoes_a_credential():
    """Credentials are exported inside the remote shell and never printed.

    The script reads .env to authenticate; what crosses the ssh channel must
    stay derived facts. An `echo` of either secret-bearing variable would put
    it in a terminal, a scrollback, and any transcript capturing the run.
    """
    for secret in ("AWS_SECRET_ACCESS_KEY", "BACKUP_AWS_SECRET_ACCESS_KEY"):
        assert f'echo "{secret}' not in CODE
        assert f"echo ${secret}" not in CODE
    assert "BACKUP_S3_BUCKET" not in CODE.split("_env BACKUP_S3_BUCKET")[-1]


def test_dns_comparison_cannot_pass_by_both_sides_being_empty():
    """Two empty strings compare equal.

    A domain with no A record and no public resolution once produced
    `ok  public DNS resolves ... to the Route 53 value` -- the most
    confidently wrong output the script could produce. Both sides are now
    checked for emptiness before they are compared.
    """
    assert '[ -z "$PUBLIC_IP" ]' in CODE
    assert '[ -z "$A_VALUE" ] || [ "$A_VALUE" = "None" ]' in CODE


def test_checks_the_served_certificate_against_the_one_on_disk():
    """Renewal can succeed while the deploy-hook fails to reload Caddy.

    Caddy holds the old certificate in memory and keeps serving it, so every
    file-based check still passes while clients see a certificate marching
    toward expiry. Only comparing what is served against what is on disk
    catches it.
    """
    assert "CERT_FILE_ENDDATE" in CODE
    assert "s_client" in CODE


def test_public_resolver_is_named_not_addressed():
    """A literal resolver address would warn in check-pii on every push.

    check-pii cannot distinguish a public resolver from someone's home IP,
    and its allowlist is deliberately only for strings confirmed NOT to be
    addresses -- which a resolver's address is. So the script names the
    resolver instead.

    This asserts the property rather than the rejected literal, because
    spelling the address out here would simply move the warning into this
    file -- the same self-reference check-pii documents about its own
    allowlist.
    """
    match = re.search(r"dig [^\n]*@(\S+)", CODE)
    assert match, "expected a dig call naming an explicit resolver"
    resolver = match.group(1)
    assert not re.fullmatch(r"[0-9.]+", resolver), (
        f"resolver {resolver!r} is a bare address; name it instead"
    )
    assert resolver == "one.one.one.one"
