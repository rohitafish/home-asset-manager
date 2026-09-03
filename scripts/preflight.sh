#!/usr/bin/env bash
# One-command sanity check for this app's install: Mac toolchain, Docker/
# Colima, Python version, .env configuration, Postgres/migration state, and
# the LaunchAgents. Run it after "One-time setup" or "Installing on a new
# Mac" (see README), and any time something isn't working to narrow down why.
#
# Deliberately does NOT use `set -e` -- unlike every other script here, this
# one's whole job is to report every problem it finds in one pass, not stop
# at the first. Exits 1 if any check FAILs, 0 if only WARNs (or nothing).
#
# Never prints a secret's value -- only whether a key is set, and its length
# where that's useful. Reads .env the same way scripts/backup-db.sh does
# (grep/cut, not `source`), so no other secret in that file is ever
# exported into this process for no reason.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

FAILS=0
WARNS=0

ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; WARNS=$((WARNS + 1)); }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }

_env_var() {
  [ -f "$REPO_DIR/.env" ] || return 0
  grep -m1 "^${1}=" "$REPO_DIR/.env" | cut -d= -f2-
}

echo "== Toolchain =="
for bin in colima docker nmap aws python3; do
  path="$(command -v "$bin" 2>/dev/null || true)"
  if [ -n "$path" ]; then
    ok "$bin -> $path"
  else
    fail "$bin not found on PATH -- see README's \"One-time setup\" (brew install colima docker docker-compose nmap awscli)"
  fi
done

echo
echo "== Docker / Colima =="
if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then
    ok "docker compose plugin resolves"
  else
    fail "docker compose plugin not found -- see README's Docker CLI plugin config step (cliPluginsExtraDirs in ~/.docker/config.json)"
  fi
  if docker info >/dev/null 2>&1; then
    ok "Docker daemon reachable (Colima running)"
  else
    fail "Docker daemon not reachable -- is Colima running? (brew services start colima)"
  fi
else
  warn "skipping Docker checks -- docker not on PATH"
fi

echo
echo "== Python =="
PY="python3"
if [ -x "$REPO_DIR/.venv/bin/python3" ]; then
  PY="$REPO_DIR/.venv/bin/python3"
fi
# Three tiers, not a single cutoff: 3.10+ is recommended (nothing to say),
# 3.9 genuinely works -- verified by actually running this app on a real
# Xcode CLT /usr/bin/python3 3.9.6, unmodified requirements.txt and all --
# but it's past its own upstream end-of-life (Oct 2025), so it's a WARN, not
# a silent ok. Below 3.9 is untested territory and stays a FAIL.
PY_VERSION="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
if [ -n "$PY_VERSION" ]; then
  MAJOR="${PY_VERSION%%.*}"
  MINOR="${PY_VERSION##*.}"
  if [ "$MAJOR" -gt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 10 ]; }; then
    ok "$PY is $PY_VERSION (recommended)"
  elif [ "$MAJOR" -eq 3 ] && [ "$MINOR" -eq 9 ]; then
    warn "$PY is $PY_VERSION -- this app runs fine on 3.9 (verified), but CPython 3.9 itself passed upstream end-of-life in October 2025 and gets no security patches; upgrade when convenient (brew install python3 gives you a current one)"
  else
    fail "$PY is $PY_VERSION -- need 3.9+ (untested below that). macOS's /usr/bin/python3 (Xcode CLT) is commonly 3.9.x already; brew install python3 for a current one before creating .venv"
  fi
else
  fail "no usable Python interpreter found at '$PY'"
fi

if [ -d "$REPO_DIR/.venv" ]; then
  if "$REPO_DIR/.venv/bin/python3" -c 'import fastapi, sqlmodel, alembic' >/dev/null 2>&1; then
    ok ".venv has fastapi/sqlmodel/alembic installed"
  else
    fail ".venv exists but fastapi/sqlmodel/alembic aren't importable -- run: source .venv/bin/activate && pip install -r requirements.txt"
  fi
else
  fail ".venv does not exist -- see README's \"One-time setup\""
fi

echo
echo "== Tests =="
# Dev-only tooling (requirements-dev.txt) -- scripts/redeploy.sh only
# installs requirements.txt, so a *fresh* checkout anywhere can legitimately
# lack pytest; a missing pytest is a WARN, not a FAIL, for that reason. The
# Mini's venv has it installed by hand today, so this does run there -- and
# a failing suite is a FAIL regardless of which host this runs on.
#
# Run under `coverage`, not a bare pytest, when coverage is installed --
# same reasoning as scripts/hooks/pre-push: CI enforces .coveragerc's
# fail_under, and until this ran under coverage too, that gate existed only
# in CI. Already `cd`-ed to $REPO_DIR at the top of this script, so
# .coveragerc's `source =` paths resolve correctly with no extra handling.
if [ -x "$REPO_DIR/.venv/bin/coverage" ]; then
  if [ -x "$REPO_DIR/.venv/bin/pytest" ]; then
    if "$REPO_DIR/.venv/bin/coverage" run -m pytest -q >/tmp/assetmgt-preflight-pytest.log 2>&1; then
      if "$REPO_DIR/.venv/bin/coverage" report >>/tmp/assetmgt-preflight-pytest.log 2>&1; then
        ok "pytest suite passes, coverage above threshold ($(tail -1 /tmp/assetmgt-preflight-pytest.log))"
      else
        fail "coverage dropped below the .coveragerc fail_under threshold -- see /tmp/assetmgt-preflight-pytest.log"
      fi
    else
      fail "pytest suite failed -- see /tmp/assetmgt-preflight-pytest.log"
    fi
    rm -f /tmp/assetmgt-preflight-pytest.log
  else
    warn "pytest not installed in .venv -- run: source .venv/bin/activate && pip install -r requirements-dev.txt"
  fi
elif [ -x "$REPO_DIR/.venv/bin/pytest" ]; then
  if "$REPO_DIR/.venv/bin/pytest" -q >/tmp/assetmgt-preflight-pytest.log 2>&1; then
    ok "pytest suite passes ($(tail -1 /tmp/assetmgt-preflight-pytest.log))"
  else
    fail "pytest suite failed -- see /tmp/assetmgt-preflight-pytest.log"
  fi
  rm -f /tmp/assetmgt-preflight-pytest.log
  warn "coverage not installed in .venv -- ran pytest without the coverage gate (run: source .venv/bin/activate && pip install -r requirements-dev.txt)"
else
  warn "pytest not installed in .venv -- run: source .venv/bin/activate && pip install -r requirements-dev.txt"
fi

echo
echo "== Lint =="
# Same dev-only-tooling reasoning as == Tests == above: missing ruff is a
# WARN, a real lint failure is a FAIL, regardless of which host this runs on.
if [ -x "$REPO_DIR/.venv/bin/ruff" ]; then
  if "$REPO_DIR/.venv/bin/ruff" check "$REPO_DIR" --cache-dir "$REPO_DIR/.ruff_cache" >/tmp/assetmgt-preflight-ruff.log 2>&1; then
    ok "ruff check passes"
  else
    fail "ruff check failed -- see /tmp/assetmgt-preflight-ruff.log"
  fi
  rm -f /tmp/assetmgt-preflight-ruff.log
else
  warn "ruff not installed in .venv -- run: source .venv/bin/activate && pip install -r requirements-dev.txt"
fi

echo
echo "== Directories =="
if [ -d "$REPO_DIR/logs" ]; then
  ok "logs/ exists"
else
  fail "logs/ does not exist -- launchd can't create a log file's parent directory itself; run: mkdir -p logs"
fi

echo
echo "== Guardrails =="
# git never clones or syncs hooks, so the pre-push gate (PII/test/lint) has to
# be copied into place per machine and can silently drift from the tracked
# template if that's later updated -- exactly the ambient-machine-state class of
# problem this script exists to catch. WARN, not FAIL: a fresh checkout that
# only ever pulls is legitimately without it, same reasoning as pytest/ruff above.
HOOK_INSTALLED="$REPO_DIR/.git/hooks/pre-push"
HOOK_TEMPLATE="$REPO_DIR/scripts/hooks/pre-push"
if [ ! -f "$HOOK_INSTALLED" ]; then
  warn "pre-push hook not installed -- pushes skip the PII/test/lint gate. Install it: cp scripts/hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push"
elif ! cmp -s "$HOOK_INSTALLED" "$HOOK_TEMPLATE"; then
  warn ".git/hooks/pre-push differs from scripts/hooks/pre-push -- the installed hook has drifted from the tracked template. Re-copy it: cp scripts/hooks/pre-push .git/hooks/pre-push"
else
  ok "pre-push hook installed and matches the tracked template"
fi

# .pii-denylist is gitignored (dev-machine-only), so a fresh clone lacks it and
# check-pii.sh silently downgrades its strongest check (literal known values) to
# a WARN, leaving only the generic structural patterns.
if [ -f "$REPO_DIR/.pii-denylist" ]; then
  ok ".pii-denylist present"
else
  warn ".pii-denylist not found -- check-pii.sh runs only its generic PII patterns, not the denylist of known real values (see AGENTS.md's \"PII / privacy\")"
fi

echo
echo "== .env =="
if [ -f "$REPO_DIR/.env" ]; then
  ok ".env exists"

  # Key names only, never values -- .env.example is a verified-complete
  # canonical list of every env var this app reads. Many keys are
  # legitimately blank (UNIFI_API_KEY, NVD_API_KEY, ...), so presence is all
  # this checks; specific required keys get value checks below.
  #
  # ANTHROPIC_API_KEY/ANTHROPIC_MODEL are excluded from this generic diff and
  # checked separately below: ANTHROPIC_MODEL always has a code-level default
  # (see app/assistant.py), and ANTHROPIC_API_KEY has a legitimate
  # either/or alternative in OPENROUTER_API_KEY -- flagging either as
  # "missing" when OpenRouter is the configured provider is a false alarm.
  # LOG_LEVEL is likewise excluded: it has a code-level default (INFO, see
  # app/logging_config.py) and is genuinely optional, so an existing .env
  # that predates it -- including the Mini's, which isn't rsynced -- shouldn't
  # be flagged. AI_DAILY_BUDGET_USD/AI_MONTHLY_BUDGET_USD/
  # CVE_ENRICH_MAX_KEYWORDS are the same shape (code-level defaults in
  # app/assistant.py / discovery/cve_enrich.py, added after this file's
  # .env would already exist).
  # `[A-Za-z0-9_]` not `[A-Z_]`: the latter has no digits, so a key like
  # BACKUP_S3_BUCKET is silently invisible to this drift check on both sides
  # of the comm. (scripts/env-structure.sh and check-pii.sh use the same
  # corrected class.)
  MISSING_KEYS="$(comm -23 \
    <(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$REPO_DIR/.env.example" | tr -d '=' | sort -u) \
    <(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$REPO_DIR/.env" | tr -d '=' | sort -u) \
    | grep -vE '^(ANTHROPIC_API_KEY|ANTHROPIC_MODEL|LOG_LEVEL|AI_DAILY_BUDGET_USD|AI_MONTHLY_BUDGET_USD|CVE_ENRICH_MAX_KEYWORDS)$' || true)"
  if [ -z "$MISSING_KEYS" ]; then
    ok "every key in .env.example is present in .env"
  else
    while IFS= read -r key; do
      [ -n "$key" ] && warn ".env is missing the key '$key' (present in .env.example)"
    done <<< "$MISSING_KEYS"
  fi

  # Either key configures the investigation assistant (app/assistant.py's
  # is_configured()); a direct ANTHROPIC_API_KEY gets full feature support,
  # OPENROUTER_API_KEY is a supported fallback that routes the same calls
  # through OpenRouter -- both are an "ok", not a warning.
  if [ -n "$(_env_var ANTHROPIC_API_KEY)" ]; then
    ok "ANTHROPIC_API_KEY is set -- investigation assistant uses the direct Anthropic API"
  elif [ -n "$(_env_var OPENROUTER_API_KEY)" ]; then
    ok "OPENROUTER_API_KEY is set -- investigation assistant is configured via the OpenRouter fallback"
  else
    warn "neither ANTHROPIC_API_KEY nor OPENROUTER_API_KEY is set -- the investigation assistant is an optional feature and will show as 'not configured'"
  fi

  ADMIN_PW="$(_env_var APP_ADMIN_PASSWORD)"
  if [ -z "$ADMIN_PW" ] || [ "$ADMIN_PW" = "change-me" ]; then
    fail "APP_ADMIN_PASSWORD is unset or still 'change-me' -- the app now refuses to start until this is a real password (see README)"
  else
    ok "APP_ADMIN_PASSWORD is set"
  fi

  for key in BACKUP_S3_BUCKET BACKUP_AWS_ACCESS_KEY_ID BACKUP_AWS_SECRET_ACCESS_KEY BACKUP_AWS_REGION; do
    val="$(_env_var "$key")"
    if [ -z "$val" ]; then
      warn "$key is not set -- nightly off-site backups won't run (expected on a dev checkout; not on the deployed instance)"
    else
      ok "$key is set"
    fi
  done

  PERMS="$(stat -f%OLp "$REPO_DIR/.env" 2>/dev/null || stat -c%a "$REPO_DIR/.env" 2>/dev/null || true)"
  if [ -n "$PERMS" ] && [ "$PERMS" != "600" ]; then
    warn ".env permissions are $PERMS, not 600 -- it holds several secrets: chmod 600 .env"
  fi
else
  fail ".env does not exist -- cp .env.example .env, then fill it in by hand (see README)"
fi

echo
echo "== Postgres =="
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if docker compose exec -T db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
    ok "Postgres is reachable"

    if [ -x "$REPO_DIR/.venv/bin/alembic" ]; then
      CURRENT_LINE="$("$REPO_DIR/.venv/bin/alembic" current 2>/dev/null | head -1)"
      HEAD_LINE="$("$REPO_DIR/.venv/bin/alembic" heads 2>/dev/null | head -1)"
      if [ -z "$CURRENT_LINE" ]; then
        warn "alembic reports no current revision -- has the schema been created or restored yet? (docker compose up -d, then restore the dump or run alembic upgrade head)"
      elif [ "${CURRENT_LINE%% *}" != "${HEAD_LINE%% *}" ]; then
        warn "database is not at the latest migration -- run: alembic upgrade head"
      else
        ok "database is at the latest migration"
      fi
    fi
  else
    warn "Postgres not reachable via docker compose -- run: docker compose up -d"
  fi
else
  warn "skipping Postgres checks -- Docker daemon not reachable"
fi

echo
echo "== LaunchAgents =="
for label in app logrotate backup; do
  plist="$HOME/Library/LaunchAgents/com.assetmgt.$label.plist"
  if [ -f "$plist" ]; then
    if grep -q '__ASSETMGT_DIR__' "$plist"; then
      fail "$plist still has the unsubstituted __ASSETMGT_DIR__ placeholder -- launchd fails to spawn it with no visible error. Re-run the sed step from README."
    else
      ok "com.assetmgt.$label is installed"
    fi
    if [ "$label" = app ] && grep -q '0\.0\.0\.0' "$plist"; then
      fail "$plist binds uvicorn to 0.0.0.0 -- HTTP Basic credentials and the whole inventory cross the LAN in the clear. Re-install from the current scripts/com.assetmgt.app.plist (loopback) and front it with TLS: README's \"Reaching it over HTTPS\""
    fi
  else
    warn "com.assetmgt.$label is not installed (~/Library/LaunchAgents/com.assetmgt.$label.plist not found)"
  fi
done

echo
echo "== Privilege boundaries =="
# The app runs as an ordinary user and must stay that way. Anything that lets
# a process running as that user become root turns "someone got into the
# web app" into "someone owns the Mini", so the known ways are checked here
# by name. A NOPASSWD sudoers rule for a Homebrew nmap is one: on Apple
# silicon /opt/homebrew/bin and the binary itself are user-owned, so the rule
# amounts to "run anything as root". See README's "Nmap privileges".
NMAP_SUDOERS="/etc/sudoers.d/nmap-assetmgt"
if [ -e "$NMAP_SUDOERS" ]; then
  fail "$NMAP_SUDOERS exists -- a passwordless sudo rule on a user-owned nmap binary is a root escalation for anything running as this user. Remove it: sudo rm $NMAP_SUDOERS (privileged scans now prompt for a password from a terminal: python -m discovery.cli nmap --sudo)"
else
  ok "no passwordless nmap sudoers rule"
fi

# The UPS LaunchDaemon runs as root every 60s. It must execute a root-owned
# copy of scripts/ups-shutdown.sh (not the one in this user-writable
# checkout) with a PATH that never searches a user-owned directory. See
# README's "Running the Mini headless".
UPSMONITOR_PLIST="/Library/LaunchDaemons/com.assetmgt.upsmonitor.plist"
UPS_INSTALLED_SCRIPT="/usr/local/libexec/assetmgt/ups-shutdown.sh"
if [ -f "$UPSMONITOR_PLIST" ]; then
  if grep -q '__ASSETMGT_DIR__' "$UPSMONITOR_PLIST" || grep -q "$REPO_DIR/scripts/ups-shutdown.sh" "$UPSMONITOR_PLIST"; then
    fail "$UPSMONITOR_PLIST runs the script out of this checkout -- root executing a user-writable file. Re-install from the current template (README, \"Running the Mini headless\"): it must point at $UPS_INSTALLED_SCRIPT"
  elif ! grep -q "$UPS_INSTALLED_SCRIPT" "$UPSMONITOR_PLIST"; then
    warn "$UPSMONITOR_PLIST does not point at $UPS_INSTALLED_SCRIPT -- an unexpected ProgramArguments; compare it with scripts/com.assetmgt.upsmonitor.plist"
  else
    ok "com.assetmgt.upsmonitor runs the installed root-owned copy, not the checkout"
  fi
  if grep -q '/opt/homebrew' "$UPSMONITOR_PLIST"; then
    fail "$UPSMONITOR_PLIST puts a Homebrew directory on root's PATH -- /opt/homebrew/bin is user-owned, so root would run whatever this user drops there. Re-install from the current template."
  else
    ok "com.assetmgt.upsmonitor PATH is system directories only"
  fi
  if [ -f "$UPS_INSTALLED_SCRIPT" ]; then
    OWNER="$(stat -f%Su "$UPS_INSTALLED_SCRIPT" 2>/dev/null || stat -c%U "$UPS_INSTALLED_SCRIPT" 2>/dev/null || true)"
    MODE="$(stat -f%OLp "$UPS_INSTALLED_SCRIPT" 2>/dev/null || stat -c%a "$UPS_INSTALLED_SCRIPT" 2>/dev/null || true)"
    if [ "$OWNER" != "root" ] || [ -z "$MODE" ] || [ $((8#$MODE & 8#022)) -ne 0 ]; then
      fail "$UPS_INSTALLED_SCRIPT is owner=$OWNER mode=$MODE -- root runs it, so it must be root-owned and writable by nobody else: sudo install -o root -g wheel -m 755 scripts/ups-shutdown.sh $UPS_INSTALLED_SCRIPT"
    else
      ok "$UPS_INSTALLED_SCRIPT is root-owned and not writable by others"
    fi
    if cmp -s "$UPS_INSTALLED_SCRIPT" "$REPO_DIR/scripts/ups-shutdown.sh"; then
      ok "installed ups-shutdown.sh matches scripts/ups-shutdown.sh"
    else
      warn "installed $UPS_INSTALLED_SCRIPT differs from scripts/ups-shutdown.sh -- redeploy.sh does not update the root-owned copy. Re-run: sudo install -o root -g wheel -m 755 scripts/ups-shutdown.sh $UPS_INSTALLED_SCRIPT"
    fi
  else
    fail "$UPSMONITOR_PLIST is installed but $UPS_INSTALLED_SCRIPT is missing -- the daemon can't run. Install it: sudo install -d -o root -g wheel -m 755 /usr/local/libexec/assetmgt && sudo install -o root -g wheel -m 755 scripts/ups-shutdown.sh $UPS_INSTALLED_SCRIPT"
  fi
fi

echo
echo "== Headless / UPS posture =="
# These settings have no other visible symptom when they silently regress --
# unlike a crashed process, a dead UPS battery or a disabled Screen Sharing
# daemon looks identical to a working one right up until the Mini reboots
# and there's no way back in. See README's "Running the Mini headless" and
# "Power outages and unattended restart".
if [ -f "$UPSMONITOR_PLIST" ]; then
  ok "com.assetmgt.upsmonitor is installed (its privilege posture is checked under \"Privilege boundaries\" above)"
  if command -v launchctl >/dev/null 2>&1 && launchctl print system/com.assetmgt.upsmonitor >/dev/null 2>&1; then
    ok "com.assetmgt.upsmonitor is loaded"
  else
    warn "com.assetmgt.upsmonitor.plist is present but not loaded -- sudo launchctl load $UPSMONITOR_PLIST"
  fi
else
  warn "com.assetmgt.upsmonitor is not installed ($UPSMONITOR_PLIST not found) -- see README's \"Running the Mini headless\""
fi

if command -v system_profiler >/dev/null 2>&1; then
  if system_profiler SPPowerDataType 2>/dev/null | grep -q 'UPS Installed:[[:space:]]*Yes'; then
    ok "a UPS is visible to the system"
  else
    warn "no UPS attached (system_profiler SPPowerDataType reports 'UPS Installed: No') -- expected on both a dev laptop and the deployed Mini: this deployment has no UPS (pricing one found every suitable model out of stock/marked up, see README's \"Power outages and unattended restart\"), so a Powerwall Gateway cutover still browns out the Mini and the spare keyboard/monitor kept on-site is the accepted recovery path"
  fi
fi

# autorestart-after-power-failure and the pmset -u UPS thresholds are
# desktop/server-only settings -- they're simply absent from `pmset -g` on a
# laptop (verified: this MacBook Air's output has no `autorestart` line at
# all), not present-but-disabled. So absence downgrades to a WARN ("not
# applicable here, expected on a dev laptop") while a *wrong value* on a
# machine that does support the setting is a real FAIL -- same
# present-vs-applicable distinction preflight.sh already draws for
# BACKUP_S3_BUCKET and the LaunchAgents above.
if command -v pmset >/dev/null 2>&1; then
  PG_SETTINGS="$(pmset -g 2>/dev/null || true)"
  if echo "$PG_SETTINGS" | grep -q 'autorestart'; then
    if echo "$PG_SETTINGS" | grep -qE 'autorestart[[:space:]]+1'; then
      ok "autorestart is enabled (pmset -g)"
    else
      fail "autorestart is present but disabled -- sudo pmset -a autorestart 1, or the Mini won't power back on after a real outage at all"
    fi
  else
    warn "autorestart is not a supported pmset setting on this machine (expected on a laptop) -- skipping"
  fi

  if echo "$PG_SETTINGS" | grep -qE 'sleep[[:space:]]+0'; then
    ok "sleep is disabled"
  else
    warn "sleep is not disabled (expected on a dev laptop) -- on the deployed instance: sudo pmset -a sleep 0, disksleep 0, or the Mini can go to sleep and stop answering health checks"
  fi

  if echo "$PG_SETTINGS" | grep -q 'haltlevel'; then
    ok "pmset -u haltlevel backstop is set"
  else
    warn "no pmset -u haltlevel/haltremain backstop is set -- see README's \"Running the Mini headless\". com.assetmgt.upsmonitor is the primary graceful-shutdown path; this is only the OS-native fallback if that script fails outright"
  fi
else
  warn "pmset not found -- skipping power settings checks (expected off-Mac)"
fi

if command -v launchctl >/dev/null 2>&1 && launchctl print system/com.apple.screensharing >/dev/null 2>&1; then
  ok "Screen Sharing is loaded -- the Mini is administrable with no display attached"
else
  warn "Screen Sharing is not loaded -- with no display attached there is no way to reach the login window after a remote reboot (fdesetup authrestart). Enable it in System Settings > General > Sharing before removing the monitor"
fi

echo
echo "== Summary =="
echo "  $FAILS FAIL(s), $WARNS WARN(s)"
if [ "$FAILS" -gt 0 ]; then
  exit 1
fi
exit 0
