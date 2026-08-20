#!/usr/bin/env bash
# Push local source changes to the always-on host and restart the running service.
# Run this from your dev machine; it refuses to run on the deploy host itself.
# Override HOST/REMOTE_DIR via DEPLOY_HOST / DEPLOY_REMOTE_DIR env vars.
set -euo pipefail

HOST="${DEPLOY_HOST:-mini}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-~/claudecode/assetmgt}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Pre-flight checks"
# Everything below assumes LOCAL_DIR and REMOTE_DIR live on *different* machines:
# the rsync --delete and the `deployed`-branch commit would both misbehave if
# `ssh $HOST` reached this same box. Resolve the deploy host's identity up front
# and bail on the two ways that assumption breaks.
#
# Empty result = the host is unreachable over ssh. That's also what happens when
# you run this ON the Mini (its ssh has no `mini` alias to loop back through), so
# the message names that case explicitly -- it's the most likely reason a human
# lands here. Either way there's nothing to deploy to, so stop before rsync
# rather than failing later with a raw rsync/ssh error.
REMOTE_HOST="$(ssh "$HOST" hostname -s 2>/dev/null || true)"
if [ -z "$REMOTE_HOST" ]; then
  echo "!!! Could not reach the deploy host '$HOST' over ssh." >&2
  echo "!!! If you're running this ON the Mini: don't -- redeploy.sh deploys FROM a" >&2
  echo "!!! dev machine TO '$HOST'. Edit in place here, then back-port the diff to" >&2
  echo "!!! your dev machine (see AGENTS.md's \"Git / GitHub\")." >&2
  echo "!!! Otherwise check your SSH config / DEPLOY_HOST -- the host may just be down." >&2
  exit 1
fi
# Non-empty but equal to our own hostname = a mis-set DEPLOY_HOST that ssh-loops
# back to this machine. Same underlying hazard, distinct enough to name.
if [ "$(hostname -s)" = "$REMOTE_HOST" ]; then
  echo "!!! '$HOST' resolves back to this machine ($REMOTE_HOST) -- redeploy.sh" >&2
  echo "!!! deploys FROM a dev machine TO the Mini, not onto itself. Check DEPLOY_HOST." >&2
  exit 1
fi

# Abort if the Mini's working tree is dirty. After a deploy the `deployed`-branch
# commit below leaves it clean, so a dirty tree here means a real, unexpected
# edit made on the Mini -- which rsync --delete would silently overwrite.
# git status --porcelain ignores gitignored paths (.env, devices/, .pii-denylist),
# so this only fires on tracked or genuinely-untracked content.
#
# Explicit `if !` around the assignment, not a bare `DIRTY=$(...)`: under
# set -e, a plain assignment's own exit status IS the substitution's --
# unlike `export VAR=$(...)`, whose exit status masks it (see backup-db.sh's
# _env_var for the same distinction). A missing/renamed $REMOTE_DIR made the
# `cd` inside fail and killed the script right here with no message, unlike
# every other failure mode this preflight section goes out of its way to name.
if ! DIRTY="$(ssh "$HOST" "cd $REMOTE_DIR && git status --porcelain")"; then
  echo "!!! Could not check the working tree at $HOST:$REMOTE_DIR -- does that" >&2
  echo "!!! directory exist? (DEPLOY_REMOTE_DIR defaults to ~/claudecode/assetmgt.)" >&2
  exit 1
fi
if [ -n "$DIRTY" ]; then
  if [ "${ALLOW_DIRTY_DEPLOY:-0}" = "1" ]; then
    echo "!!! ALLOW_DIRTY_DEPLOY=1 -- discarding the Mini's uncommitted changes:" >&2
    echo "$DIRTY" | sed 's/^/!!!   /' >&2
  else
    echo "!!! The working tree on $HOST:$REMOTE_DIR has uncommitted changes:" >&2
    echo "$DIRTY" | sed 's/^/!!!   /' >&2
    echo "!!!" >&2
    echo "!!! rsync --delete below would overwrite these. If they're real edits made" >&2
    echo "!!! on the Mini, back-port them to your dev machine first:" >&2
    echo "!!!   ssh $HOST 'cd $REMOTE_DIR && git diff' > /tmp/mini-fix.patch" >&2
    echo "!!!   git apply /tmp/mini-fix.patch   # on your dev machine" >&2
    echo "!!! then commit/push from there and redeploy. To discard them and deploy" >&2
    echo "!!! anyway: ALLOW_DIRTY_DEPLOY=1 ./scripts/redeploy.sh" >&2
    exit 1
  fi
fi

# Warn before clobbering the Mini's .pii-denylist. It's gitignored but rsynced
# on purpose (the check needs it on both machines), which makes the sync one-way:
# a term added on the Mini is overwritten by the next deploy. Can't merge it
# automatically, but shouldn't do it silently.
if [ -f "$LOCAL_DIR/.pii-denylist" ]; then
  LOCAL_PII="$(shasum "$LOCAL_DIR/.pii-denylist" | cut -d' ' -f1)"
  REMOTE_PII="$(ssh "$HOST" "test -f $REMOTE_DIR/.pii-denylist && shasum $REMOTE_DIR/.pii-denylist | cut -d' ' -f1" || true)"
  if [ -n "$REMOTE_PII" ] && [ "$LOCAL_PII" != "$REMOTE_PII" ]; then
    echo "!!! WARNING: $HOST's .pii-denylist differs from this machine's and will be" >&2
    echo "!!! overwritten by the rsync below. If a term was added on the Mini, copy it" >&2
    echo "!!! into this machine's .pii-denylist first or it's lost." >&2
  fi
fi

echo "==> Syncing code to $HOST:$REMOTE_DIR"
# --exclude='.git': the Mini has had its own git repo since 2026-08-02 (fetch-only,
# via a read-only deploy key -- see AGENTS.md's "Git / GitHub"), independent of this
# rsync. Without this exclusion, --delete would wipe its .git on every deploy: the
# deploy key, the installed pre-push hook, and its checked-out commit state.
rsync -avz --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='logs' \
  --exclude='backups' \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='.claude' \
  -e ssh "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"

echo "==> Recording the deployed tree in the Mini's git"
# rsync deploys a working *tree*, not a commit, so the only truthful record of
# what's now running on the Mini is a commit of that tree. Capture it on a
# local-only `deployed` branch -- the Mini's deploy key is read-only, so this
# never reaches origin. This is what keeps `git status` on the Mini clean after
# a deploy, which is what makes a *dirty* status there mean a real, unexpected
# edit (see the pre-flight dirty check above and AGENTS.md's "Git / GitHub").
# We can't just `git reset --mixed origin/main` like this used to: that's only
# truthful when this machine's HEAD == origin/main and the deployed files match
# that commit, which isn't guaranteed (unpushed local commits, or a preview of
# uncommitted work) -- it would then leave the Mini reporting a clean tree that
# doesn't match what's running. Committing the actual tree is truthful either way.
# `git add -A` respects .gitignore, so .env / devices/ / .pii-denylist stay
# untracked; --allow-empty keeps a no-op redeploy successful. `git fetch` still
# runs (for origin/main as a reference) but is non-fatal. The whole step is
# non-fatal (`|| echo`) for the same reason the old reset was: a git failure
# here shouldn't abort a deploy that already succeeded via rsync.
DEPLOY_MSG="deploy: $(git -C "$LOCAL_DIR" rev-parse --short HEAD) $(date -u +%Y-%m-%dT%H:%M:%SZ)"
ssh "$HOST" "cd $REMOTE_DIR && { git fetch origin --quiet || true; } && git checkout -q -B deployed && git add -A && git commit -q --allow-empty -m '$DEPLOY_MSG'" \
  || echo "!!! Could not record the deployed tree in the Mini's git -- deploy still succeeded via rsync, but 'git status' there may look dirty until this is fixed." >&2

echo "==> Checking for an in-progress discovery run"
# Deliberately BEFORE the pip install/migration steps below, not after: this
# check runs against the Mini's EXISTING venv/code (still valid -- it doesn't
# need anything from the deploy that's about to happen), so declining here
# leaves the schema untouched too, not just the running process. It used to
# run after migrations had already been applied, so declining still left the
# live (pre-restart) process running against a schema it no longer matched.
RUNNING=$(ssh "$HOST" "eval \"\$(/opt/homebrew/bin/brew shellenv)\" && cd $REMOTE_DIR && source .venv/bin/activate && python -c \"
from sqlmodel import Session, select
from app.db import engine
from app.models import DiscoveryRun
with Session(engine) as session:
    r = session.exec(select(DiscoveryRun).where(DiscoveryRun.status == 'running')).first()
    print(r.source if r else '')
\"")
if [ -n "$RUNNING" ]; then
  echo "!!! A '$RUNNING' discovery run is currently in progress on the Mini."
  echo "!!! Restarting the service now will kill it mid-scan (it'll be marked"
  echo "!!! failed automatically, but you'll lose the results)."
  read -p "Restart anyway? [y/N] " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted. Code is synced (and the deployed-tree commit above already"
    echo "happened), but dependencies were not installed, migrations were not"
    echo "run, and the service was not restarted."
    exit 1
  fi
fi

echo "==> Installing any new/updated dependencies"
ssh "$HOST" "eval \"\$(/opt/homebrew/bin/brew shellenv)\" && cd $REMOTE_DIR && source .venv/bin/activate && pip install -q -r requirements.txt"

echo "==> Running any new migrations"
ssh "$HOST" "eval \"\$(/opt/homebrew/bin/brew shellenv)\" && cd $REMOTE_DIR && source .venv/bin/activate && alembic upgrade head"

echo "==> Restarting the app service"
ssh "$HOST" "launchctl kickstart -k gui/\$(id -u)/com.assetmgt.app"

sleep 2
echo "==> Health check"
# -f (not plain -s): curl exits 0 for ANY HTTP status without it, including a
# 500 from a dead Postgres/Colima (see AGENTS.md) -- the deploy would then
# print the 500 body and "==> Done" as if it were healthy. mini-brew-upgrade.sh
# already uses -sf for this identical check; this brings the two in line.
if ! ssh "$HOST" "curl -sf http://127.0.0.1:8000/health && echo"; then
  echo "!!! Health check failed -- the app did not come back up cleanly after" >&2
  echo "!!! the restart. Check logs/app.error.log and logs/app.log on the Mini." >&2
  exit 1
fi

echo "==> Done"
