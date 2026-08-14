# assetmgt Project Instructions

Instructions for anyone (or anything) working on this codebase — AI coding
assistant or human contributor.

## Deployment topology
- This repo is developed on a dev machine, but the live/always-on instance
  runs on a separate always-on Mac on the same LAN, per the README's launchd
  setup.
- Code is synced to that machine via `ssh mini` (an SSH config alias — set
  this up yourself pointing at your own always-on host; see README).
- **A fix committed/tested locally is NOT live until it's deployed to `mini`.**
  Always deploy with `./scripts/redeploy.sh` from this repo's root — do not
  reinvent the deployment steps manually. It rsyncs the code (excluding
  `.venv`, `.env`, logs, `.git`), installs any new Python dependencies, runs
  `alembic upgrade head`, checks for an in-progress discovery run (prompts
  before restarting if one is running, since a restart kills it mid-scan),
  restarts the service via `launchctl kickstart -k`, and health-checks
  `/health` at the end. It only needs `launchctl unload`/`load` (not
  `kickstart`) if the launchd plist itself changed, which is rare.
- Standard workflow for any change: edit code locally → verify in the local
  preview server (`assetmgt-web` launch config) → run `./scripts/redeploy.sh`
  → spot-check the change against real data on the Mini over `ssh mini`.
- **Previewing on the Mini directly** (e.g. to reproduce something against the
  real data): the `assetmgt-web` launch config is dev-machine-only
  (`.claude/` is gitignored and never rsynced), and its port 8000 is already
  held by the live service there. Start a second copy by hand on another port:
  ```bash
  ssh mini
  cd ~/claudecode/assetmgt && source .venv/bin/activate
  uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
  ```
  **This is not an isolated sandbox.** It reads the live `.env`, so it talks to
  the live Postgres — and `--reload` re-executes on every save, including
  half-finished edits. Use it to spot-check read-only paths; anything that
  writes data belongs on the dev machine against its own DB. (A properly
  isolated preview DB on the Mini would fix this at the root but doesn't exist
  yet — it's a possible follow-up, not something in place today.)
- `redeploy.sh` rsyncs code and reruns migrations, but it does **not**
  install/reload launchd plists (`scripts/*.plist`) -- a plist change (new
  agent, or edits to an existing one) needs a manual `cp`/`sed`/`launchctl
  load` on the Mini, per the README's launchd setup section.
- The Mini cannot recover from a power outage unattended: FileVault is on, and
  `~/Library/LaunchAgents` (the app, Colima, backups) lives on the encrypted
  Data volume, so nothing starts until someone logs in at the console. This is
  an accepted trade-off, not an unfixed bug -- don't "fix" it by proposing a
  system LaunchDaemon (same encrypted volume) or auto-login (mutually
  exclusive with FileVault on macOS). See README's "Power outages and
  unattended restart".
- Even when someone *is* logged in through a reboot (e.g. an OS-upgrade
  restart), Colima can still fail to come back, taking Postgres and the app
  down with it. An abrupt shutdown SIGTERMs Colima without a graceful `colima
  stop`, leaving the lima/vz instance half-torn-down; on reboot the
  `homebrew.mxcl.colima` LaunchAgent autostarts but every attempt dies with
  `vz driver is running but host agent is not` / `exit status 1` (see
  `/opt/homebrew/var/log/colima.log`), and `KeepAlive` just retries onto the
  same stale state forever. Symptom: the app is unreachable and
  `ssh mini 'curl -s http://127.0.0.1:8000/health'` fails with curl exit 7,
  while `logs/app.error.log` shows psycopg `port 5432 ... Connection refused`.
  Recovery is a manual clean restart on the Mini: `colima start`, confirm
  `docker ps` shows `assetmgt-db-1` up on `127.0.0.1:5432`, then
  `launchctl kickstart -k gui/$(id -u)/com.assetmgt.app` and re-check
  `/health`. This is left as a manual runbook, not automated -- a shutdown
  hook to `colima stop` gracefully is unreliable during OS upgrades, and
  auto-recovery here isn't worth the moving parts.
- **When *you* initiate a Mini reboot/shutdown (not a forced OS upgrade), stop
  Colima yourself first so the VM tears down cleanly and can't leave the stale
  state above.** A normal macOS shutdown ( -> Shut Down, or `sudo shutdown
  -r now`) is already graceful, but it only gives each process a few seconds
  after `SIGTERM` before `SIGKILL`, and the lima/vz guest often needs longer.
  So before rebooting: `ssh mini 'eval "$(/opt/homebrew/bin/brew shellenv)";
  colima stop'` (waits for a clean guest teardown, no time limit), optionally
  `launchctl bootout gui/$(id -u)/com.assetmgt.app` so the app isn't
  crash-looping against a vanishing DB (it autostarts on next boot), then
  reboot normally. This is the safe path; the manual recovery above is only
  for reboots you couldn't get ahead of.
- Post-reboot sluggishness on the Mini is **not** this app: measured, the app
  process runs ~0.1% CPU/mem and Colima's VM support processes ~0% CPU. What
  actually spikes after an unclean shutdown is macOS's own housekeeping
  (Spotlight reindex, a deferred Time Machine run, Chrome's login-item
  restore, Apple's update/model-catalog daemons) -- see README's "Keeping the
  host responsive". Don't chase this by tuning the app, Colima's `cpu`/
  `memory` allocation, or the launchd agents' priority; there's no measured
  gain there.

## Authentication
- `app/auth.py`'s `require_admin` is the only thing between a LAN device and
  the whole dashboard/API (see README's "Running it"). `APP_ADMIN_PASSWORD`
  defaulting to empty is not "auth disabled", it's "any password matches" —
  `secrets.compare_digest("", "")` is `True`. Two layers guard against this
  now: `app/main.py`'s `require_admin_password_configured` startup hook
  refuses to boot if the password is unset or still the `.env.example`
  placeholder `change-me`, and `require_admin` itself denies unconditionally
  on an empty expected password as defence in depth for any entry point that
  skips `main.py`'s startup events (e.g. a test harness importing `app.auth`
  directly). **Don't relax either check** — an empty-password bypass has no
  visible symptom short of someone actually trying it; `/health` still
  reports `{"status": "ok"}`.
- Consequence of the startup guard: a genuinely misconfigured host now
  crash-loops on boot instead of running open. `com.assetmgt.app.plist`'s
  `ThrottleInterval` (60s) exists so that loop doesn't hammer the log at the
  10s `KeepAlive` default — the same Colima-ordering hazard
  `com.assetmgt.backup.plist` already handles by skipping `RunAtLoad`
  entirely, applied differently here because the app agent still needs
  `RunAtLoad` for a normal boot.

## Log rotation
- `logs/app.log`/`logs/app.error.log` are launchd's `StandardOutPath`/
  `StandardErrorPath` for the app service (`scripts/com.assetmgt.app.plist`),
  which means launchd holds an **open `O_APPEND` file descriptor on that
  exact inode** for the life of the process. `scripts/rotate-logs.sh`
  (installed as the separate `com.assetmgt.logrotate` LaunchAgent, see
  README) rotates them by **copy-then-truncate-in-place**, never by
  renaming.
- **Do not rotate these logs with `newsyslog`** (or any rename-based
  rotation) -- moving the live file leaves the running service writing into
  the renamed/archived copy while the newly-created file stays empty until
  the next restart. Logging goes silently dark rather than failing loudly.
  macOS `newsyslog` has no copy-truncate mode (that's a Linux `logrotate`
  feature) -- verified against its man page before writing the custom
  script instead.
- **The app must keep logging to stdout/stderr, never to a file.**
  `app/logging_config.py` configures Python logging with `StreamHandler`s
  only (stdout for `<WARNING`, stderr for `WARNING+`), precisely because
  launchd owns those two files per the above. A `logging.FileHandler`/
  `RotatingFileHandler` pointed at `logs/app.log` would rename-rotate it and
  break launchd's writer exactly as the `newsyslog` hazard describes. If a
  genuinely separate app-owned log file is ever needed, it needs its own
  `rotate_one` line in `scripts/rotate-logs.sh` plus the `.gitignore` +
  rsync-exclude treatment `logs/` already has -- don't just point a handler
  at a new path.
- `LOG_LEVEL` (default `INFO`, code-level in `app/logging_config.py`) sets the
  root level for both the app and the discovery CLI. It's optional and
  excluded from `preflight.sh`'s `.env`-completeness check, so an existing
  `.env` -- including the Mini's, which `redeploy.sh` doesn't rsync -- won't
  be flagged for lacking it.

## Database backups
- `scripts/backup-db.sh` runs nightly via a **third** LaunchAgent,
  `com.assetmgt.backup`, dumping Postgres to S3 (see README's "Off-site
  database backups"). Like the other two, `redeploy.sh` does **not**
  install/reload plists — a change to `scripts/com.assetmgt.backup.plist`
  needs a manual `launchctl unload` → overwrite → `launchctl load` on the
  Mini (unload first if a previous version is already loaded — `load` alone
  on an already-loaded label is a no-op).
- **`backups/` is gitignored and excluded from `redeploy.sh`'s rsync,
  exactly like `logs/`.** `redeploy.sh` uses `rsync --delete`; without the
  exclude, every deploy would wipe the Mini's local dump history and its
  `last-success` freshness marker. If you ever add another host-side state
  directory, give it the same two-line treatment (`.gitignore` +
  `redeploy.sh`'s exclude list) in the same commit that introduces it.
- AWS credentials for the backup job (`BACKUP_S3_BUCKET`,
  `BACKUP_AWS_ACCESS_KEY_ID`, `BACKUP_AWS_SECRET_ACCESS_KEY`,
  `BACKUP_AWS_REGION`) live **in `.env`**, not `~/.aws/credentials` —
  deliberately different from the original design, because this uses an
  existing, full-S3-access IAM identity (`s3-user`) shared with other work
  in the account, not a dedicated one scoped to this project. `backup-db.sh`
  reads these directly via `grep`/`cut` (see the script's `_env_var()`
  helper) rather than sourcing `.env`, specifically to avoid exporting every
  *other* secret in that file into the process for no reason.
- `pg_dump`/`pg_restore` always run **inside** the `db` container
  (`docker compose exec -T db sh -c '...'`), never against `DATABASE_URL` —
  its `postgresql+psycopg://` prefix is a SQLAlchemy dialect string, not a
  libpq URI. **The `-T` flag is mandatory** on any `exec` carrying a binary
  dump: without it, Compose may allocate a pseudo-TTY, which performs
  newline translation and silently corrupts the archive.
- Never dump per-table — there's no `ON DELETE CASCADE` anywhere in this
  schema (see "Asset-child tables" below), so FK integrity depends on the
  dump being one transactionally-consistent snapshot.
- Because the backup credential has full S3 access (not least-privilege),
  delete protection is enforced by **S3 Object Lock (Compliance mode)** on
  the bucket itself instead of by an IAM policy — S3 refuses to delete or
  overwrite a locked object before its retention date regardless of what
  permissions the caller has. This is why uploads use `aws s3api put-object
  --object-lock-mode COMPLIANCE --object-lock-retain-until-date ...` rather
  than `aws s3 cp`, which doesn't expose Object Lock at all. **Object Lock
  can only be enabled at bucket *creation*** — it cannot be retrofitted onto
  an existing bucket, and locking an object is a genuine one-way door for
  its retention period (30 days for `daily/`, 186 for `monthly/`), with no
  override, not even for the account owner or AWS support. Don't "simplify"
  this back to `aws s3 sync --delete` — the whole point is that nothing can
  delete these objects early, including a fully compromised Mini.
- Retention is age-based (S3 Lifecycle deletes what Object Lock's retention
  has already released — the two work together, not against each other),
  which means **a silently failing job loses the entire backup history
  within that window** rather than just going stale. That's why the
  `backups/last-success` marker, the Summary page's "Since last DB backup"
  card, and `/health`'s `backup_stale` field exist — don't remove them as
  noise, they're the thing that would surface exactly this failure mode.
- The full rebuild-a-host runbook lives in README's "Installing on a new
  Mac", not here. The one ordering constraint worth repeating: on a fresh
  clone, restore the dump into the empty database `docker compose up -d`
  creates, *then* `alembic upgrade head` — never the other way around, since
  the dump carries its own `alembic_version`.
- `logs/` is tracked in git via `logs/*` + `!logs/.gitkeep` in `.gitignore`
  plus a committed empty `logs/.gitkeep` — a plain `logs/` entry excludes the
  directory itself, which means a `!logs/.gitkeep` negation can't re-include
  anything inside it (verified with `git check-ignore -v`). This exists
  because `logs/` used to be entirely gitignored with nothing tracked, so it
  didn't exist after a fresh clone — and launchd cannot create a log file's
  parent directory, so all three plists' `StandardOutPath`/`StandardErrorPath`
  had nowhere to write. Worst case was `scripts/rotate-logs.sh`'s own
  `cd .../logs` under `set -e` failing immediately, with its failure's only
  output channel being a file inside the directory that didn't exist.
  `backups/` stays fully gitignored — `backup-db.sh` already does its own
  `mkdir -p backups`, so it doesn't need this treatment.
- `scripts/preflight.sh` is a one-command doctor for exactly this class of
  problem: toolchain/Homebrew-prefix mismatches, wrong Python version,
  missing `.venv`/`logs/`, `.env` drift against `.env.example`, Postgres/
  migration state, and unsubstituted `__ASSETMGT_DIR__` placeholders in
  installed plists. It never prints a secret's value, only whether one is
  set — read `.env` the same grep/cut way `backup-db.sh`'s `_env_var()` does
  if you extend it, not by sourcing the file. Deliberately has no `set -e`:
  its job is to report every problem in one pass, not stop at the first.

## Dates and timestamps

- **Every datetime/date column in the schema is naive, storing UTC by
  convention — not timezone-aware.** No `timezone=True` anywhere, in either
  `app/models.py` or any migration. This is deliberate, not an oversight:
  `app/template_filters.py`'s `localdt` filter docstring says outright
  "Renders a naive UTC datetime (as stored in the DB) in local time," and
  `discovery/cve_enrich.py` explicitly strips tzinfo off NVD's
  timezone-aware dates on ingest to normalize into this same contract.
- **Always get "now" from `app/clock.py`'s `utcnow_naive()`, never
  `datetime.utcnow()` directly.** `datetime.utcnow()` is deprecated since
  Python 3.12 — it was the source of a deprecation warning on every single
  row insert before this existed. `utcnow_naive()` returns the identical
  naive-UTC value, just via the non-deprecated `datetime.now(UTC)` call,
  stripped back to naive.
- **Do not switch any column to `timezone=True`, or any call site to a
  timezone-aware datetime, without treating it as its own reviewed
  migration.** It isn't safe to do piecemeal: several places compare an
  in-process "now" against a value just read back from Postgres —
  `app/routers/dashboard.py`'s summary/findings pages,
  `app/backup_status.py`'s staleness check, `findings_list.html`'s overdue
  check — and mixing an aware value into any one of those raises
  `TypeError: can't subtract offset-naive and offset-aware datetimes` at
  request time. Separately, psycopg3 applies the Postgres session's
  `TimeZone` setting when writing an aware value into a naive column, which
  can silently shift the stored wall-clock time — a risk the test suite
  cannot catch, since it runs against in-memory SQLite (built from
  `SQLModel.metadata`, not the real migrations), whose bind processor drops
  tzinfo instead of applying any timezone conversion.
- `ruff.toml` reflects this: it selects `DTZ003` (the deprecated
  `datetime.utcnow()` call) individually rather than the whole `DTZ` family,
  since the rest of that family (`date.today()`, `strptime` without `%z`,
  a `datetime.max` sort sentinel) is *correct* under the naive-UTC
  convention, not debt.

## Money and valuation

- **All money is GBP and `Numeric(12, 2)`.** There is no `currency` column
  and no multi-currency support — `£` is hardcoded in
  `app/template_filters.py`'s `money` filter and the `/valuables.csv` header,
  and both money parsers (`app/routers/dashboard.py`'s `_parse_money_field`
  and `app/assistant.py`'s `_coerce_proposal_value`) *strip* a leading
  `£`/`$`/`€` and store the number as-is. A `$` amount is therefore recorded
  as if it were GBP. Keep amounts in GBP; adding real currency support is its
  own piece of work, not something to fold into a money change.
- **Use `Decimal` end to end, never `float`, for stored amounts**, and
  quantize with an explicit rounding mode (`ROUND_HALF_UP` is what the app
  uses). The one deliberate exception is `app/valuation.py`, which drops into
  `float` *only* for the `(1 + drift) ** years` growth factor — `Decimal`
  has no fractional power — then returns to `Decimal` immediately so all
  actual money arithmetic and rounding stay exact.
- **`Asset.replacement_value` is the insurance figure, and it can be either
  hand-entered or rule-derived — there is no column recording which.** The
  `revalue` command (`discovery/cli.py` → `discovery/revaluation.py`, logic in
  `app/valuation.py`) backfills it new-for-old and recomputes on every run,
  overwriting manual values. What makes that safe is the audit trail, not a
  flag: every write leaves an `AssetNote(author="valuation")` with the old →
  new figures, so a hand-set value the rule overwrote is always recoverable
  from the asset timeline. If you change the valuation rule, that note is the
  only record of what prior runs produced — don't remove it.
- **The valuation basis is new-for-old, deliberately not depreciated** — see
  README's "Replacement values" for the reasoning (the average clause). If
  someone asks for depreciated/indemnity figures, that's a *second* basis, not
  a change to this one; the existing column is the sum-insured number.
- **`revalue` is not a discovery collector**, even though it lives under
  `discovery/` (that package owns the only CLI entry point). It must **not**
  open a `DiscoveryRun` via `_tracked_run()` — that would pollute the
  dashboard's discovery history with a run that scanned nothing.
- **Tests assert on `Decimal` inputs directly, not values read back from the
  DB.** The suite's in-memory SQLite doesn't handle `Numeric` natively (same
  class of divergence as the datetime note above), so round-tripping a money
  value through it isn't a faithful test — see `tests/test_valuation.py`.

## Tests

- `tests/` holds a `pytest` suite covering asset cascade deletion, the
  investigation assistant's tool loop and proposal handling, backup-status
  labeling, device correlation/linking, dashboard helpers, and hostname/
  vendor normalization. Run it with `.venv/bin/pytest -q` (or `pytest -q`
  once the venv is activated).
- Dependencies are in `requirements-dev.txt`, not `requirements.txt` —
  `scripts/redeploy.sh` only installs the latter, so a *fresh* checkout
  anywhere (including a from-scratch Mini rebuild) starts without pytest.
  The Mini's current venv has it anyway, installed by hand rather than by
  `redeploy.sh` — the suite is runnable on both machines today. One-time
  setup on a checkout that lacks it: `pip install -r requirements-dev.txt`
  inside `.venv`.
- Both `scripts/hooks/pre-push` and `scripts/preflight.sh` run the suite if
  `.venv/bin/pytest` exists: pre-push blocks the push on a failing suite the
  same way it blocks on `check-pii.sh`; preflight reports pass/fail
  alongside its other checks. In both, a *missing* pytest is a warning, not
  a failure — it's expected on any checkout that hasn't run the one-time
  setup above — but a suite that's installed and failing blocks the push,
  including on the Mini, since it does have pytest installed.
- Both scripts also run `ruff check` (see `ruff.toml`) under the identical
  WARN-if-missing/FAIL-if-failing split — same `requirements-dev.txt`
  tooling, same reasoning. `--cache-dir` is passed explicitly in both,
  rather than left to ruff's default (relative to the invoking process's
  cwd, not the repo) — confirmed by hand that an absolute-path invocation
  from an unexpected cwd otherwise errors trying to create the cache
  somewhere it can't write.

## Git / GitHub
- This repo has its own git identity, scoped locally (not global): commits
  are authored as `rohitafish <25867278+rohitafish@users.noreply.github.com>`,
  not the user's real name — keep it that way for anything pushed publicly.
- Remote: `origin` → `https://github.com/rohitafish/assetmgt` (private).
- Committing after a verified fix/feature (tested locally, deployed, spot-checked
  on the Mini) is the norm for this project — no need to ask first. Pushing to
  GitHub is different: only push when explicitly asked, never proactively just
  because a commit was made.
- **Before any push, run `scripts/check-pii.sh` (or let the installed
  pre-push hook do it)** — see "PII / privacy" below. This is not optional
  diligence, it's a hard blocker: two real leaks already happened before
  this existed.
- **The Mini also has its own git repo, as of 2026-08-02** (fetch-only) —
  before that, `~/claudecode/assetmgt` there was a plain directory kept in
  sync purely by `redeploy.sh`'s rsync, so an entire local-only Claude
  session's work once lived nowhere but that one unversioned checkout. It
  authenticates to `origin` over SSH with a dedicated, repo-scoped,
  **read-only** deploy key (`~/.ssh/id_ed25519_assetmgt_deploy`, wired in via
  that repo's local `core.sshCommand`, not `~/.ssh/config` — doesn't affect
  any other SSH usage on that machine) — deliberately not a copy of any
  dev-machine credential, which would carry far more scope than a server
  needs. `redeploy.sh` still does the actual deploying via rsync, and
  right after commits the just-deployed working tree onto a local-only
  `deployed` branch on the Mini (`git checkout -B deployed && git add -A &&
  git commit --allow-empty`). It used to `git reset --mixed origin/main`
  instead, but that only tells the truth when the dev machine's `HEAD` equals
  `origin/main` *and* the deployed files match that commit — neither is
  guaranteed (unpushed local commits; a deploy of uncommitted work), and when
  it's wrong the Mini reports a clean tree that doesn't match what's actually
  running. Committing the real tree is truthful either way, and (because
  `add -A` respects `.gitignore`) leaves `.env`/`devices/`/`.pii-denylist`
  untracked. This is what stops the Mini's checkout from silently going stale
  again like the pre-2026-08-02 plain-directory setup did: every redeploy now
  keeps the Mini's git state truthful about what's actually running there, so
  after a deploy a dirty `git status` on the Mini means a real, unexpected
  edit made *on the Mini* — not routine deploy noise. `redeploy.sh` refuses to
  proceed when it sees one (see "Editing on the Mini" below). See
  `redeploy.sh`'s `--exclude='.git'` comment for why the rsync and the git
  commit don't collide with each other.
- **Editing on the Mini, and getting the work back.** Editing directly on the
  Mini is fine — it's where the real data and the live service are, so it's the
  natural place to debug against them. But the Mini's deploy key is
  **read-only by design**, so it can commit locally but never push, and the
  direction of truth stays MacBook → Mini: work becomes permanent only by
  carrying the diff back to a dev machine. `redeploy.sh` enforces this from the
  dev-machine side — it **aborts before rsync if the Mini's tree is dirty**
  (rather than silently overwriting it with `--delete`) and tells you to
  back-port first:
  ```bash
  ssh mini 'cd ~/claudecode/assetmgt && git diff' > /tmp/mini-fix.patch
  git apply /tmp/mini-fix.patch      # on the dev machine (git apply -3 if it's moved on)
  ```
  Then commit and push from the dev machine, where the pre-push hook runs, and
  redeploy. `ALLOW_DIRTY_DEPLOY=1 ./scripts/redeploy.sh` overrides the abort and
  **discards** the Mini's edits — only reach for it when you're sure they're
  disposable. `redeploy.sh` also refuses to run *on* the Mini itself (it
  compares `hostname -s` on both ends), since a self-deploy would rsync the
  repo onto itself.

## PII / privacy
- Real personal data has leaked into this repo twice: household names,
  a real hostname/LAN IP, and an SSH key filename sat in git history for
  weeks after a commit *scrubbed the current files* without rewriting
  history (so the originals were still fully recoverable from earlier
  commits); separately, real names were reused as "illustrative examples"
  in a later, unrelated commit's comments, since the original cleanup pass
  was a one-off manual review, not a repeatable check. Both are now fixed
  (history rewritten with `git filter-repo`, current-tree comments
  corrected) — this section is what stops a third occurrence.
- **`.pii-denylist`** (repo root, gitignored, dev-machine-only) holds the
  exact real values that must never appear in a commit again — one literal
  string per line. This is the one sanctioned place for them, same logic as
  `.env` for secrets. Add a line any time you learn another real personal
  detail (a name, a hostname, an address) — the check picks it up
  automatically, no code change needed.
- **`devices/`** (repo root, gitignored) holds vendor-account exports used by
  `discovery/account_import.py` (see README's "Vendor account import") —
  screenshots/PDFs from Amazon/Sonos and the transcribed `accounts.json`,
  which is nothing but real serials, room names and purchase dates. It's
  gitignored the same way `.env`/`.pii-denylist` are, but unlike those two,
  `scripts/redeploy.sh` still rsyncs it to `mini` on purpose — the import
  needs a copy of the data there. Add any serial you transcribe into it to
  `.pii-denylist` too, same as any other real value.
- **`scripts/check-pii.sh`** checks denylist terms (case-insensitive,
  literal, plus a hex-normalised pass so a MAC matches whatever its
  separators or length — the literal pass alone once missed a real device
  MAC written a different way) across both file **trees** and commit
  **messages** (the tree-only rules once let real names in messages slip
  past a "clean" `--full`), plus generic structural patterns (emails, GPS
  coordinates, non-private IPs, SSN-like numbers) as defense in depth. Two
  modes: default (commits about to be pushed) and `--full` (the entire
  tracked tree and every commit reachable from any ref — rerun this after
  adding to the denylist, or any time you want a full audit, not just at
  push time; note it runs a git grep per commit, so full history takes a
  minute).
  All of these — a denylist hit, email, GPS, SSN, and the secret rules
  below — are a **FAIL** and block the push; a non-private IP is only a
  **WARN** and does not. It echoes the matched text for the PII rules (the
  match *is* the thing that shouldn't be there, and seeing it is how you
  find it), but the secret rules deliberately **never print the value** — a
  leaked credential in scrollback or a CI log is a second copy of the thing
  we're containing, so they report a key name and a location only.
- **Secret detection (three rules, all FAIL).** `check-pii.sh` also catches
  live credentials, a different threat from PII: (a) vendor-prefixed key
  formats (`sk-ant-…`, `sk-or-v1-…`, `AKIA…`, `ghp_…`, GitHub PATs, Slack
  `xox…`, Google `AIza…`, PEM `PRIVATE KEY` headers) by shape; (b) **this
  machine's own** secret and structured-PII values, read from the gitignored
  `.env` and literal-grepped against history — scoped to keys whose *name*
  looks sensitive (`*API_KEY*`/`*PASSWORD*`/`*SECRET*`/`*TOKEN*`/
  `*ACCESS_KEY*`/`*SUBNET*`/`*BASE_URL*`), over 12 chars, and not already
  published in `.env.example`; and (c) a tracked `.env`/`.env.local`/
  `.env.*` file at all (i.e. someone ran `git add -f .env`). Rule (b) is
  machine-scoped: it only knows *this* box's `.env`, so a secret that lives
  only on the Mini isn't a needle here — reconcile with
  `scripts/env-structure.sh` (below). Household *names* (`DEFAULT_OWNER`,
  `SECONDARY_OWNER_NAME`) are deliberately **not** auto-scanned — a first
  name collides with ordinary prose (the "chase" verb-vs-name problem that
  retired the machine-wide guardrail); put those in `.pii-denylist`, where a
  human vets them. There is deliberately **no** generic `KEY=value` keyword
  rule — it false-positives on the README's documented `export …=$(grep …
  .env)` recipes and `docker-compose.yml`'s local password, and an
  unsuppressable permanent WARN just trains people to ignore the check.
- **`scripts/env-structure.sh`** replicates `.env` *structure* — key names,
  order, and `.env.example`'s comment blocks — across a dev machine, the
  Mini, and the tracked template, **without ever moving a value** (values
  are never read into the template, sent over ssh, or printed; the remote is
  queried for key *names* only and re-validated locally). Default is a
  three-way presence report; `--write-example` regenerates the template,
  preserving it byte-for-byte for keys it already documents and appending an
  empty `KEY=` + `TODO` for any new key, then refusing to write if a safety
  re-parse finds anything that isn't template-verbatim or a bare `KEY=`.
  This is the supported way to notice and close Mini-vs-template drift, since
  `redeploy.sh` never syncs `.env`.
- **`IP_ALLOWLIST`** (inside `check-pii.sh`) suppresses dotted-quad strings
  confirmed *not* to be IP addresses — currently just the Sonos test
  fixture's `<hardwareVersion>`, which the pattern can't tell from an
  address. Without it that WARN re-fires on every push touching the file
  forever, and a warning people learn to scroll past is exactly how a real
  one gets missed later. Two rules for it: entries are `<path>|<literal>`
  and **file-scoped on purpose**, so the same digits elsewhere still warn
  (`tests/test_check_pii.py` pins that — don't "simplify" it to a bare list
  of values); and the bar for adding one is **confirmed not an IP**, not
  "probably fine". A real public endpoint that's genuinely meant to be
  there should stay a WARN. Note an entry has to cover `check-pii.sh`
  itself as well, since writing the literal puts that dotted quad into the
  script's own source.
- **Install the pre-push hook once per dev-machine clone** (hooks aren't
  cloned/synced by git):
  ```bash
  cp scripts/hooks/pre-push .git/hooks/pre-push
  chmod +x .git/hooks/pre-push
  ```
  It blocks any push whose commits trip `check-pii.sh`. `git push
  --no-verify` bypasses it deliberately if you're certain something's a
  false positive — don't reach for that reflexively.
- **There is deliberately no machine-wide PII check anymore.** A global
  version (`~/.pii-guardrail/`, wired in via `core.hooksPath` in
  `~/.gitconfig`, taking precedence over every repo's own hook) existed
  briefly but was removed: a single cross-project denylist collided with
  ordinary project content (e.g. a real name on the list also being an
  everyday English word used in unrelated prose here — "chase" the verb vs.
  "Chase" the name) in a way a per-project, hand-tuned `.pii-denylist`
  doesn't. Each project now runs its own local check only, tailored to that
  project's own content — assetmgt's is `scripts/check-pii.sh` +
  `.pii-denylist`, installed per the step above. `~/.pii-guardrail/`'s files
  still exist on disk (nothing deleted) but `core.hooksPath` is unset, so
  git no longer invokes them on any repo.
- **Every `git grep` inside `check-pii.sh` passes `--no-color` explicitly.**
  A gitconfig with `color.ui=always` (not the default `auto`) makes git
  emit real ANSI escape codes even into piped/captured output, not just a
  terminal — this silently broke the private-IP exclusion logic while the
  script was first being written (`127.0.0.1` wasn't being excluded) until
  `--no-color` was added everywhere. Don't drop it from a new `git grep`
  call added later.
- **What none of this can catch**: a *new* real name used for the first
  time as an "illustrative example" in a comment or sample value. It isn't
  in the denylist yet (nothing is, until someone notices and adds it), and
  a name is syntactically indistinguishable from any other word, so no
  pattern-matcher can flag it. The actual rule, for anyone (human or AI)
  writing example data in this codebase: use obviously generic placeholders
  (`Alex`, `Jordan Lee` — the pattern already established here) and never
  repurpose a real name, hostname, or other detail learned about the
  household. The tooling is a backstop for *known* values, not a substitute
  for this.

## Documentation
- `README.md` is the single source of truth for user-facing docs — there is
  no separate generated HTML copy to keep in sync (removed; the in-app
  README page renders `README.md` live on every request via
  `app/readme_render.py`). When a change is user-visible, update `README.md`
  as part of the same piece of work, not as an afterthought.
- If a change also affects deployment steps, gotchas, or gets learned the
  hard way, update *this* file too, not just README — this is what a fresh
  session (agent or human) should read first.

## Asset-child tables (cascade deletion)
- There's no `ON DELETE CASCADE` on any FK pointing at `asset.id`, so
  deleting/merging an asset requires cleaning up every dependent row by
  hand. That cleanup is centralized in `app/asset_children.py`'s
  `ASSET_CHILD_MODELS` list and the `delete_asset_cascade`/
  `reassign_asset_children` helpers it exports — **whenever you add a new
  table with an `asset_id` FK, add it to `ASSET_CHILD_MODELS`**, or asset
  deletion/merge will start hitting a foreign-key violation (surfaced to the
  user as a bare "internal server error") the moment that table has a row.
  This has already shipped as a bug twice before the helper existed — don't
  hand-roll another copy of the cleanup loop at a new call site.

## New environment variables (investigation features)
- `ANTHROPIC_API_KEY` (optional) — enables the per-asset Claude chat panel.
  Unset it and the panel just shows a "not configured" note; nothing else in
  the app depends on it or fails without it.
- `ANTHROPIC_MODEL` (optional) — overrides the default model
  (`claude-opus-5`) the chat panel uses.
- `OPENROUTER_API_KEY` (optional) — fallback credential for the chat panel,
  used only when `ANTHROPIC_API_KEY` is unset. `app/assistant.py`'s
  `_client_kwargs()` points the Anthropic SDK at OpenRouter's
  Anthropic-Messages-API-compatible endpoint (`base_url` override, Bearer
  auth via the SDK's `auth_token=`) instead of `api.anthropic.com` — no
  separate client library. The server-side refusal-fallback beta param
  (`betas=["server-side-fallback-2026-07-01"], fallbacks="default"`) is
  Anthropic-only and has no documented OpenRouter support, so
  `run_chat_turn()` omits it on this path rather than risk it erroring.
