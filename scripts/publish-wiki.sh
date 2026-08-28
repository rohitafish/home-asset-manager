#!/usr/bin/env bash
# Publish wiki-drafts/*.md to the live GitHub Wiki
# (https://github.com/rohitafish/home-asset-manager.wiki.git), a SEPARATE git
# repo from this one -- not part of this repo's history or working tree (see
# AGENTS.md's "Documentation" section).
#
# This script exists because doing the clone/copy/commit/push by hand once
# leaked the operator's real name and machine hostname into a commit on the
# PUBLIC wiki (2026-08-28): a fresh `git clone` anywhere outside this repo
# picks up git's silent username+hostname auto-detection, since this
# machine's GLOBAL git identity is deliberately left unset (this repo's own
# `rohitafish` identity is scoped LOCAL to this repo on purpose -- see
# AGENTS.md's "Git / GitHub"). A wiki scratch clone doesn't inherit that
# local scoping, so it needs the identity set explicitly. That's what this
# script does, plus a same-commit assertion that would have caught the
# original mistake before it reached GitHub.
#
# Usage:
#   ./scripts/publish-wiki.sh              # publish every changed page
#   ./scripts/publish-wiki.sh SBOM.md ...  # publish only the named page(s)
#
# Override the wiki URL via WIKI_REPO_URL (mirrors redeploy.sh's DEPLOY_HOST
# pattern) -- not expected to be needed outside testing this script itself.
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRAFTS_DIR="$LOCAL_DIR/wiki-drafts"
WIKI_REPO_URL="${WIKI_REPO_URL:-https://github.com/rohitafish/home-asset-manager.wiki.git}"

echo "==> Pre-flight checks"

# The draft in wiki-drafts/ is the thing that goes through check-pii.sh and
# the pre-push hook (both git-level, both scoped to THIS repo) -- publishing
# an uncommitted edit would push content that never passed either. Requiring
# a clean tree keeps every published page traceable back to a reviewed commit.
DIRTY="$(cd "$LOCAL_DIR" && git status --porcelain -- wiki-drafts)"
if [ -n "$DIRTY" ]; then
  echo "!!! wiki-drafts/ has uncommitted changes:" >&2
  echo "$DIRTY" | sed 's/^/!!!   /' >&2
  echo "!!! Commit them in this repo first (they still need to pass" >&2
  echo "!!! check-pii.sh / the pre-push hook like any other commit here)," >&2
  echo "!!! then re-run this script." >&2
  exit 1
fi

# The one thing this whole script exists to get right: read the identity
# from THIS repo's own local config (never hardcode it here, never rely on
# global config) so the wiki clone and the main repo can never drift apart.
# Empty result = this repo's local scoping (AGENTS.md's "Git / GitHub") isn't
# set up the way it's supposed to be -- stop rather than fall through to
# git's global/auto-detected identity, which is exactly the original leak.
GIT_NAME="$(git -C "$LOCAL_DIR" config user.name || true)"
GIT_EMAIL="$(git -C "$LOCAL_DIR" config user.email || true)"
if [ -z "$GIT_NAME" ] || [ -z "$GIT_EMAIL" ]; then
  echo "!!! Could not read a local git identity (user.name/user.email) from" >&2
  echo "!!! $LOCAL_DIR -- refusing to publish rather than let a fresh wiki" >&2
  echo "!!! clone fall back to an auto-detected real-name+hostname identity." >&2
  echo "!!! See AGENTS.md's \"Git / GitHub\" section." >&2
  exit 1
fi
# Signing config is optional -- carried over if present so wiki commits match
# this repo's signed-commit convention, but not required to publish (the wiki
# repo carries no require-signed-commits ruleset, unlike main).
GIT_SIGNINGKEY="$(git -C "$LOCAL_DIR" config user.signingkey || true)"
GIT_GPGFORMAT="$(git -C "$LOCAL_DIR" config gpg.format || true)"
GIT_GPGSIGN="$(git -C "$LOCAL_DIR" config commit.gpgsign || true)"
GIT_ALLOWED_SIGNERS="$(git -C "$LOCAL_DIR" config gpg.ssh.allowedSignersFile || true)"

# Default to every page that differs from what's currently in wiki-drafts/,
# or the caller's explicit list -- either way, restricted to files that
# actually exist in wiki-drafts/ (silently ignores stray names rather than
# creating a new wiki page from a typo).
if [ "$#" -gt 0 ]; then
  PAGES=("$@")
else
  PAGES=()
  for f in "$DRAFTS_DIR"/*.md; do
    PAGES+=("$(basename "$f")")
  done
fi

SCRATCH="$(mktemp -d)"
cleanup() { rm -rf "$SCRATCH"; }
trap cleanup EXIT

echo "==> Cloning $WIKI_REPO_URL"
git clone --quiet "$WIKI_REPO_URL" "$SCRATCH/wiki"

echo "==> Setting this clone's identity from $LOCAL_DIR's local config"
git -C "$SCRATCH/wiki" config user.name "$GIT_NAME"
git -C "$SCRATCH/wiki" config user.email "$GIT_EMAIL"
if [ -n "$GIT_SIGNINGKEY" ]; then
  git -C "$SCRATCH/wiki" config user.signingkey "$GIT_SIGNINGKEY"
  git -C "$SCRATCH/wiki" config gpg.format "$GIT_GPGFORMAT"
  git -C "$SCRATCH/wiki" config commit.gpgsign "$GIT_GPGSIGN"
  [ -n "$GIT_ALLOWED_SIGNERS" ] && git -C "$SCRATCH/wiki" config gpg.ssh.allowedSignersFile "$GIT_ALLOWED_SIGNERS"
fi

echo "==> Copying pages"
CHANGED=()
for page in "${PAGES[@]}"; do
  if [ ! -f "$DRAFTS_DIR/$page" ]; then
    echo "!!! Skipping '$page' -- not found in wiki-drafts/." >&2
    continue
  fi
  if ! diff -q "$DRAFTS_DIR/$page" "$SCRATCH/wiki/$page" >/dev/null 2>&1; then
    cp "$DRAFTS_DIR/$page" "$SCRATCH/wiki/$page"
    CHANGED+=("$page")
  fi
done

if [ "${#CHANGED[@]}" -eq 0 ]; then
  echo "==> Nothing to publish -- the wiki already matches wiki-drafts/."
  exit 0
fi

echo
echo "==> Changes to publish:"
(cd "$SCRATCH/wiki" && git --no-pager diff -- "${CHANGED[@]}")

echo
echo "This will publish immediately to the PUBLIC wiki -- no PR/review gate,"
echo "same as any other publish-to-a-public-place action (AGENTS.md)."
read -p "Push the above to the wiki as ${GIT_NAME}? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
  echo "Aborted -- nothing was pushed."
  exit 1
fi

(cd "$SCRATCH/wiki" && git add -- "${CHANGED[@]}" && git commit --quiet -m "Update ${CHANGED[*]}")

echo "==> Pushing"
(cd "$SCRATCH/wiki" && git push --quiet)

# The assertion that would have caught the original leak: confirm the commit
# that's now live on the wiki is actually authored the way we just configured
# it, not silently overridden by something else in the environment (a stray
# GIT_AUTHOR_* env var, an unexpected global config, etc.).
PUSHED_IDENTITY="$(cd "$SCRATCH/wiki" && git log -1 --format='%an <%ae>')"
EXPECTED_IDENTITY="$GIT_NAME <$GIT_EMAIL>"
if [ "$PUSHED_IDENTITY" != "$EXPECTED_IDENTITY" ]; then
  echo "!!! Pushed commit is authored as '$PUSHED_IDENTITY', expected" >&2
  echo "!!! '$EXPECTED_IDENTITY'. The wiki has ALREADY been updated with the" >&2
  echo "!!! wrong identity -- fix this immediately (see AGENTS.md's" >&2
  echo "!!! \"Git / GitHub\" section on rewriting a public commit's authorship)." >&2
  exit 1
fi

echo "==> Done -- published as $PUSHED_IDENTITY: ${CHANGED[*]}"
