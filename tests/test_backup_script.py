"""Pins how backup-db.sh decides whether this month already has a monthly copy.

Objects land under COMPLIANCE Object Lock, so a wrong "no monthly copy yet"
is not a retryable mistake: it uploads a second 186-day locked object that
nobody -- not the account owner, not AWS support -- can remove early, and it
repeats on every run for as long as the wrong answer persists.

`aws s3 ls` cannot answer the question safely, because it exits 1 both when
the prefix matched nothing and when the call actually failed (a denied
ListBucket, a network error). Pairing that with a discarded stderr, as this
script once did, turns every failure into "upload another one". The
least-privilege assetmgt-backup identity made the denied-ListBucket case
newly plausible -- it is the first credential this job has used that could
lack the permission at all -- so the distinction is pinned here.
"""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup-db.sh"
BODY = SCRIPT.read_text(encoding="utf-8")
# The comments deliberately name the rejected approach to explain why it was
# rejected, so the executable lines are what these assertions look at.
CODE = "\n".join(
    line for line in BODY.splitlines() if not line.lstrip().startswith("#")
)


def test_monthly_check_does_not_use_s3_ls():
    """`aws s3 ls` conflates "empty" with "failed" -- it must not gate the put."""
    assert "aws s3 ls" not in CODE


def test_monthly_check_distinguishes_empty_from_failed():
    """list-objects-v2 exits 0 with `None` when empty, non-zero on a real error."""
    assert "aws s3api list-objects-v2" in CODE
    assert '"$MONTHLY_EXISTING" = "None"' in CODE


def test_a_failed_listing_aborts_rather_than_uploading():
    """The error branch must exit, not fall through to _put_object_with_retries."""
    start = CODE.index("if ! MONTHLY_EXISTING=")
    end = CODE.index('if [ "$MONTHLY_EXISTING" = "None" ]')
    error_branch = CODE[start:end]
    assert "exit 1" in error_branch
    assert "_put_object_with_retries" not in error_branch
