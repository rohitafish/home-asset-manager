# Home Asset & Vulnerability Management

**[Project site](https://rohitafish.github.io/home-asset-manager/) · [Wiki](https://github.com/rohitafish/home-asset-manager/wiki) · [Report a security issue](https://github.com/rohitafish/home-asset-manager/security/policy)**

A CMDB-style asset inventory + lightweight vulnerability management tool,
built for asset and vulnerability management at small scale (a home network),
scaled down from the enterprise-grade practices that inspired it.

**Scope and requirements:** this is designed for a small home network (tens
of devices, not hundreds), and has two hard dependencies:

- **macOS** — the app and its discovery collectors run natively on a Mac,
  not on Linux/Windows. Those two specifically can't be containerised:
  nmap needs raw-socket/L2 access to the LAN that a Linux-VM-backed Docker
  runtime on macOS can't reliably give it. Postgres *is* containerised,
  since it only needs a port on localhost — so this is a split, not a
  no-containers rule (see [Architecture](#architecture)).
- **A Ubiquiti UniFi network** (a self-hosted UniFi Network controller, or a
  UDM/Cloud Gateway console) — one of the two discovery collectors talks to
  UniFi's local Integration API directly. The other collector (nmap) works
  against any network, but device naming/VLAN/topology data specifically
  depends on UniFi being present.

Discovery is **on-demand only** — nothing scans continuously in the
background. Everything is designed for **LAN-only access**: no port is ever
forwarded on the router, and the whole dashboard sits behind HTTP Basic auth.

## Architecture

Two of the three moving parts run on the host and one runs in a container —
the split is deliberate, and which side each lands on is forced by whether
it needs real access to the LAN:

- **The FastAPI app and discovery/scanning code run natively** on the Mac in
  a Python venv, *not* in a container. Nmap needs raw-socket/L2 access to the
  LAN, and on macOS every Docker runtime — Colima and Docker Desktop alike —
  puts containers behind a Linux VM's virtualised networking, which can't
  reliably provide that. So these run directly on the host.
- **Postgres** runs in Docker (via [Colima](https://github.com/abiosoft/colima),
  a lightweight CLI-only Docker runtime — no Docker Desktop GUI needed). It
  only needs a TCP port on localhost, never the LAN, so the VM boundary
  costs it nothing and containerising it avoids managing a native install.
- Vulnerability enrichment matches nmap-detected service versions against the
  live **NVD CVE API** (keyword search), then layers on **FIRST EPSS**
  scores and the **CISA KEV** catalogue. The NVD API is used directly rather
  than nmap's own `vulscan`/`vulners` NSE scripts, since it's simpler to
  integrate correctly and to verify results against.

### Reconciliation across discovery sources

Both collectors (UniFi and nmap) feed the same reconciliation pipeline
(`discovery/reconcile.py`), matched primarily by MAC address (falling back to
IP). A few rules keep repeated discovery runs from degrading data quality:

- **Hostname source priority** — UniFi's client/device names (`unifi_client`/
  `unifi_device`) are treated as more trustworthy than nmap's raw reverse-DNS/
  mDNS hostnames, so a later nmap run can't clobber a good UniFi-sourced name
  with something like `EPSONF8467B.localdomain`. The asset's `hostname_source`
  field tracks which collector last set it.
- **Hostname locking** — any asset can have its hostname manually locked
  (`hostname_locked`) from its edit page, e.g. for devices UniFi can't rename
  itself (a VPN-only interface, a UCG gateway VLAN entry). A locked hostname
  is never touched by discovery, regardless of source priority.
- **VLAN/network annotation** — both the UniFi and nmap discovery paths look
  up each device's VLAN ID and network name from UniFi's `/networks` endpoint
  (matched by IP-to-subnet), so `asset_interfaces.vlan`/`network_name` are
  populated consistently no matter which collector found the device.
- **Lifecycle-gated reclassification** — `asset_type` is only auto-updated by
  discovery while an asset's `lifecycle_status` is `discovered`. Once an asset
  is marked `active` (see Triage Queue below), its type is frozen against
  future discovery runs.
- **Per-VLAN gateway addresses attach to the router, not new assets** — a
  multi-VLAN gateway (e.g. a UDM/UCG) has a separate IP per network, and
  nmap can't ARP-resolve a MAC for one on a different VLAN than the scanning
  host. UniFi's API has no field linking a network's gateway IP back to the
  device serving it either, but every routed network does report its own
  gateway address, and a home network has exactly one router. So: if there's
  exactly one UDM/UCG/UDR/USG/UXG-family device on the site, any discovery
  hit at a known network's gateway IP is attached to that device as another
  interface, regardless of what MAC (if any) came back for it. Ambiguous
  setups (zero or more than one such device) fall back to normal per-device
  matching rather than guessing.

## One-time setup

```bash
# 1. System dependencies
brew install colima docker docker-compose nmap awscli

# 2. Docker CLI plugin config (compose)
mkdir -p ~/.docker
# add "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"] to ~/.docker/config.json

# 3. Start the Docker runtime (persists across reboots)
brew services start colima

# 4. Python environment -- Python 3.10+ is recommended, and the brew line
# above already gets you one in practice: both `nmap` and `awscli` pull in
# a current `python@3.x` as a runtime dependency, linked as the unversioned
# `python3` ahead of macOS's own /usr/bin/python3 (Xcode Command Line Tools,
# commonly 3.9.x) once Homebrew's shellenv is on PATH. 3.9 also works if you
# land there some other way, but it's past its own upstream end-of-life
# (Oct 2025) -- not the target to aim for. `./scripts/preflight.sh` below
# checks which one you're actually on.
cd home-asset-manager   # wherever you cloned this repo
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.txt   # a pip-compile lockfile: every wheel hash-checked

# 5. Configure
cp .env.example .env
chmod 600 .env   # holds several secrets (admin password, API keys, AWS creds)
# edit .env: set APP_ADMIN_PASSWORD (the app refuses to start if this is
# still unset or the placeholder "change-me" -- see "Running it"),
# UNIFI_BASE_URL, UNIFI_API_KEY, SCAN_SUBNETS, and DEFAULT_OWNER (+
# optionally SECONDARY_OWNER_NAME/_HOSTNAME_KEYWORD for a second household
# member) -- these control who newly discovered assets are auto-assigned to.
# ANTHROPIC_API_KEY is optional -- see "Investigation features" below.

# 6. Start Postgres and run migrations
docker compose up -d
alembic upgrade head

# 7. Check everything above actually landed correctly
./scripts/preflight.sh
```

### Keeping Homebrew packages current: `scripts/mini-brew-upgrade.sh`

On the always-on host, don't run a bare `brew upgrade` -- Homebrew's own
cleanup step can delete files still open in a running process's memory, and
an interpreter or a lazily-compiled template hitting one of those deleted
files mid-request will break the live app until it's restarted.
`scripts/mini-brew-upgrade.sh` stops the app service first, upgrades with
cleanup deferred, restarts, then runs `preflight.sh` to confirm nothing
broke -- instead of relying on remembering to do all that by hand every
time. `-y` skips the confirmation prompt, `--no-preflight` skips the doctor
run. Only meaningful on a machine running the `com.assetmgt.app`
LaunchAgent -- it refuses to run anywhere else.

### Checking the install: `scripts/preflight.sh`

One command that reports every problem it finds in a single pass, rather
than the usual experience of fixing one thing and re-running to discover the
next: missing Homebrew/Python dependencies (and which of Intel's `/usr/local`
vs Apple silicon's `/opt/homebrew` they resolve from), the Docker CLI plugin
step above (given as prose, not a command, precisely because it can't be
scripted the same way), Python's version (3.10+ recommended, 3.9 works but
warns since it's past upstream end-of-life, below 3.9 fails as untested),
whether `.venv` and `logs/` exist, `.env`'s completeness against
`.env.example` and whether `APP_ADMIN_PASSWORD` is still unset/`change-me`,
whether Postgres is reachable and migrations are current, and whether the
LaunchAgents (if any are installed) still have an unsubstituted
`__ASSETMGT_DIR__` placeholder from a skipped `sed` step.

`FAIL` means something is broken; `WARN` is informational and often expected
(e.g. a 3.9 interpreter that works fine but is worth upgrading when
convenient, the `BACKUP_*` keys being legitimately unset on a dev checkout,
and a LaunchAgent not being installed yet being normal before "Running it").
It never
prints a secret's value, only whether one is set. Safe to re-run any time
something isn't working, not just at install.

### UniFi API key

Requires a local UniFi Network application/console reachable on your LAN
(a self-hosted controller or a UDM/Cloud Gateway console both work — they
expose the same local Integration API). This step trips people up more than
anything else here, mostly for two reasons:

- **Role matters.** Creating an API key requires your UniFi account to have
  the **Super Admin** role specifically. Other roles (View Only, Full
  Management, Site Admin) can look and feel like enough access — you can
  browse most of the console fine — but none of them expose the
  Integrations/API-key screen at all. If you can't find any API/Integrations
  option anywhere, check your account's role first before assuming you're
  looking in the wrong place.
- **The menu location varies by UniFi OS version**, and Ubiquiti has moved it
  more than once. Rather than follow exact click-paths (which may already be
  out of date by the time you read this), look for a section named
  **Integrations** or **API** — it's typically near Settings, or as its own
  top-level entry in newer versions. Searching the console's own settings
  search box for "API" or "Integrations" is often the fastest way to find it
  regardless of version.

Once you find it, create a key and copy it somewhere safe immediately — as
with most API key screens, you may only get to see the value once, at
creation time. Put it in `.env` as `UNIFI_API_KEY`, and set `UNIFI_BASE_URL`
to your console's LAN URL (e.g. `https://192.168.1.1`). Leave
`UNIFI_VERIFY_TLS=false` unless you've installed a real certificate on the
console (UniFi consoles use self-signed certs by default).

**Known limitation:** this API doesn't expose AP/VLAN/uplink topology for
clients, and doesn't always return a MAC address for wireless clients — the
older username/password Controller API has that, but this project
deliberately avoids storing controller credentials. Instead, MAC addresses
missing from UniFi are filled in from nmap's ARP-resolved results by matching
on IP address during each discovery run.

**Serial numbers:** the v1 Integration API above also has no serial number
field on infrastructure devices at all. The UniFi discovery run additionally
calls the older Controller API's `GET /proxy/network/api/s/{site}/stat/device`
endpoint, confirmed to accept the *same* `UNIFI_API_KEY` — so this doesn't
reintroduce the controller-credentials problem noted above, it's just a
second read-only path on the same key, used solely to pick up each device's
serial number and SKU-style model. See "Asset identity and support data"
below.

### Nmap privileges

"Run nmap scan now" uses an unprivileged TCP-connect scan (`-sT`) — no
`sudo` needed, and nothing on the Discovery page runs as root.

A fuller SYN scan (`-sS`, faster and more accurate port-state results) needs
root, and is available **only from a terminal on the host**, where `sudo` can
ask for your password:

```bash
python -m discovery.cli nmap --sudo
```

There is deliberately **no passwordless sudoers rule** for this, and no web
button. Earlier versions documented a `NOPASSWD: /opt/homebrew/bin/nmap`
rule as "narrowly scoped". It wasn't: on an Apple-silicon Homebrew install
`/opt/homebrew/bin` and the `nmap` binary (and its dylibs) are owned by your
user, so anything running as that user — the web app included — could swap
the binary and run it as root. `sudo` checks the *path*, not who owns what's
at it. If you set that rule up on an earlier version, remove it now:

```bash
sudo rm /etc/sudoers.d/nmap-assetmgt
sudo visudo -c   # should print "sudoers files ok"
```

`scripts/preflight.sh` FAILs while that file exists. Off a terminal (launchd,
a pipe) the CLI falls back to `sudo -n`, which fails fast with a clear error
instead of hanging on a prompt nobody can answer.

Avoid the alternatives: running the whole app process as root (it has a
web-exposed attack surface, even if LAN-only), a passwordless rule (above), or
having the app prompt for your Mac password in a web form (that password
would transit the app's HTTP layer).

### NVD API key (optional)

Vulnerability enrichment works without one, but NVD rate-limits
unauthenticated requests heavily (~5 requests/30s), so a full-estate scan can
take several minutes. Get a free key at https://nvd.nist.gov/developers/request-an-api-key
and set `NVD_API_KEY` in `.env` to speed this up substantially.

## Running it

**For local development / one-off use:**

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

If you run this *on your deployment host itself* to reproduce something
against the real data, change `--port 8000` to `8001` — the live service
already holds 8000 — and know that it is **not** an isolated sandbox: it
reads the live `.env` and so talks to the live Postgres, with `--reload`
re-executing on every save. Spot-check read-only paths that way; run
anything that writes data on a dev machine against its own database
instead.

**For always-on LAN access (the intended real deployment):**

The launchd unit is a template — it can't use `~` or environment variables
(launchd reads paths literally), so fill in the absolute path to your clone
in the *copy*, not the repo's tracked file:

```bash
cp scripts/com.assetmgt.app.plist ~/Library/LaunchAgents/
sed -i '' "s|__ASSETMGT_DIR__|$(pwd)|g" ~/Library/LaunchAgents/com.assetmgt.app.plist
launchctl load ~/Library/LaunchAgents/com.assetmgt.app.plist
```

This binds uvicorn to **loopback only** (`127.0.0.1:8000`). Nothing on the
LAN can reach it directly, on purpose: the app uses HTTP Basic auth
(`APP_ADMIN_USER`/`APP_ADMIN_PASSWORD` from `.env`), which puts the password
on every request, and the inventory it serves (serials, purchase prices,
uploaded receipts) is exactly the kind of thing that shouldn't cross a home
network in the clear -- the same network whose IoT devices this app
catalogues CVEs for. To use it from other devices, put a TLS terminator in
front of it on the same host: see "Reaching it over HTTPS" just below. **Do
not forward any port to it on your router** -- reachability should stop at
your home network (or tailnet) boundary.

The plist also passes `--proxy-headers --forwarded-allow-ips 127.0.0.1`, so
the loopback proxy can hand the real client IP and scheme through: the
per-client login throttle then keys on the actual visitor instead of
collapsing everyone into `127.0.0.1` (where one attacker's failures would
lock you out too), and the app can send `Strict-Transport-Security` on
https responses. Only loopback is trusted for those headers.

### Reaching it over HTTPS

The app answers only for `localhost`, `127.0.0.1`, and whatever you list in
`APP_ALLOWED_HOSTS` in `.env` -- any other `Host` header gets a `400` before
auth or routing run. So whichever option below you pick, add the name it
serves to `APP_ALLOWED_HOSTS` and restart the app.

**Recommended: Tailscale Serve.** Nothing listens on the LAN at all; the
dashboard is reachable only from devices on your tailnet, over a real
certificate Tailscale issues for the Mini's tailnet name.

```bash
brew install tailscale && sudo tailscale up      # once; sign in
tailscale cert --help >/dev/null                 # HTTPS certs need MagicDNS + HTTPS enabled in the admin console, once
tailscale serve --bg 8000                        # https://<mini>.<tailnet>.ts.net -> 127.0.0.1:8000
tailscale serve status
```

Then `APP_ALLOWED_HOSTS=<mini>.<tailnet>.ts.net` in `.env`, and
`launchctl kickstart -k gui/$(id -u)/com.assetmgt.app`. `tailscale serve`
persists across reboots on its own. Don't use `tailscale funnel` -- that is
the public internet.

**Alternative: Caddy on the LAN.** For devices that can't run Tailscale.
`brew install caddy`, copy `scripts/Caddyfile.example` to
`/opt/homebrew/etc/Caddyfile`, set the hostname in it and in
`APP_ALLOWED_HOSTS`, point that name at the Mini (router DNS or `/etc/hosts`),
and `brew services start caddy`. It uses Caddy's internal CA (`tls internal`);
trust that root certificate once per client device or browsers will warn.
Caddy runs as your user, not root, and only ever proxies to loopback.

Either way, `scripts/preflight.sh` FAILs if the installed app plist still
binds `0.0.0.0`, and warns if `APP_ALLOWED_HOSTS` is missing from `.env`.

**The app refuses to start if `APP_ADMIN_PASSWORD` is unset or still the
`.env.example` placeholder `change-me`** -- an empty expected password isn't
"no auth", it's "any password works" (`secrets.compare_digest("", "")` is
`True`), and that's not something worth discovering after the fact. If it
won't come up, check `logs/app.error.log` for this before anything else; the
LaunchAgent's `ThrottleInterval` (60s) keeps a misconfigured host from
crash-looping tightly in the meantime.

To stop the persistent service: `launchctl unload ~/Library/LaunchAgents/com.assetmgt.app.plist`

Logs: `logs/app.log` and `logs/app.error.log`. The app configures Python
logging at startup (`app/logging_config.py`) with timestamped, greppable
lines; everything at `INFO`/`DEBUG` goes to stdout (→ `logs/app.log`) and
`WARNING`+ to stderr (→ `logs/app.error.log`), so the error log stays a
signal, not a copy of everything. uvicorn's own access/startup lines flow
through the same config, so they're timestamped too and its routine startup
banner now lands in `app.log` rather than `app.error.log`. Set `LOG_LEVEL` in
`.env` (default `INFO`; `DEBUG` adds httpx/app detail and per-seed probe
misses without a SQLAlchemy query firehose). Nothing writes a log *file*
directly -- the app only ever writes stdout/stderr, and launchd owns the
files (this is deliberate; see the rotation note below).

Install a second, one-shot LaunchAgent to keep these bounded -- otherwise
nothing rotates them, and a sustained failure (e.g. Postgres not yet up at
boot) can write tens of MB an hour into `app.error.log` indefinitely:

```bash
cp scripts/com.assetmgt.logrotate.plist ~/Library/LaunchAgents/
sed -i '' "s|__ASSETMGT_DIR__|$(pwd)|g" ~/Library/LaunchAgents/com.assetmgt.logrotate.plist
launchctl load ~/Library/LaunchAgents/com.assetmgt.logrotate.plist
```

This runs `scripts/rotate-logs.sh` hourly, gzip-archiving and truncating
each log in place once it passes 10 MB, keeping the last 5 archives. It
truncates rather than renames deliberately -- launchd holds an open
append-mode file handle on the exact inode for the life of the app process,
and renaming the live file would leave it writing into the archived copy
while the new file stays empty until the next restart. See the script's
header comment if you're tempted to reach for `newsyslog` instead, which
renames.

Postgres's own container log is capped the same way, via `docker-compose.yml`'s
`logging:` block (`max-size: 10m`, `max-file: 5`) -- Docker's default
`json-file` driver has no size limit otherwise, and under a sustained
Postgres failure that log grows exactly as unbounded as the app's did.

### Power outages and unattended restart

The host can restart itself automatically after a power failure (macOS's
"restart after power failure" setting, `pmset -g` → `autorestart 1`), and
sleep disabled, so the hardware side of unattended recovery is
straightforward.

**But if FileVault is enabled, the app will not come back until someone logs
in at the console.** `com.assetmgt.app`, `homebrew.mxcl.colima`, and everything
else in `~/Library/LaunchAgents` lives on the encrypted Data volume, so none of
it runs until that volume is unlocked by an interactive login -- launchd simply
has nothing to launch yet, and there is no way to do that unlock over the
network, on macOS or otherwise: the pre-boot screen runs before there's a
network stack to reach it with. This isn't fixable by moving the app to a
system LaunchDaemon in `/Library/LaunchDaemons` either -- that's on the same
encrypted volume.

**This deployment keeps FileVault on and accepts that trade-off, deliberately.**
Auto-login is the obvious-looking way around it -- macOS disables the two
together, so turning FileVault off is what unlocks it -- but that swaps a rare
physical trip for a much worse failure mode: the host also holds a signed-in
iCloud session, GitHub deploy keys, `~/.aws/credentials`, and `.env` (including
`APP_ADMIN_PASSWORD` and the off-site backup's AWS credentials), and on Apple
Silicon the SSD is hardware-encrypted by the Secure Enclave regardless of the
FileVault setting -- so what FileVault actually buys here isn't "encrypted at
rest" (you have that either way), it's "can't be opened without
authentication." Auto-login would also write the login password into
`/etc/kcpassword` under trivially-reversible obfuscation, readable from
recoveryOS the moment FileVault is off -- turning a stolen Mini from a brick
into a fully-provisioned session, which is a bad trade for a private-home
deployment holding more than just this app's data.

**What was actually causing this host's outages turned out not to be power
loss at all.** Household power here runs through a battery backup (a Tesla
Powerwall) that already rides out real outages for hours -- but the backup
gateway takes on the order of a few hundred milliseconds to detect grid loss
and switch over, and a Mac Mini's internal PSU only holds up for a few
milliseconds on its own. The host was browning out in that cutover gap, which
also explains the "sometimes it stayed up" pattern: survival came down to how
long that particular cutover happened to take. If your setup has a similar
whole-house backup source, look for this before reaching for anything more
drastic -- a battery big enough to run the house for hours does nothing for a
sub-second transfer glitch.

**The correct fix is a small UPS sized for that gap, not for extended
runtime** -- see "Running the Mini headless" below for sizing and the
graceful-shutdown daemon that backs it up if it ever does run low. In
practice, though, pricing one on Amazon UK (Aug 2026) found every pure-sine,
macOS-compatible model either out of stock or marked up 2-3x normal, which
isn't worth paying for a sub-second glitch. **The accepted mitigation instead
is a spare keyboard and monitor kept where the Mini physically lives** -- a
cutover-induced reboot still lands at FileVault's pre-boot screen exactly as
before, and it takes a walk over to unlock it. This is a real, ongoing cost,
not a rare edge case; revisit the UPS if prices settle or the frequency
becomes annoying. Either way this keeps FileVault on and doesn't trade it
away for a failure mode that was never really about power at all.

If you find the dashboard unreachable after an outage, check `last` and
`sysctl -n kern.boottime` for the actual boot and login times before
diagnosing further -- a handful of Postgres `connection refused` tracebacks
in `logs/app.error.log` right after login is expected (the app racing
Colima's own startup) and isn't a separate bug.

### Shutting down or rebooting the host gracefully

When you deliberately reboot or shut down the host -- a maintenance restart, a
manual OS update, moving the machine -- stop Colima first. A normal macOS
shutdown ( → Shut Down, or `sudo shutdown -r now`) is already graceful in
that it sends every process a `SIGTERM` and flushes disks; it is nothing like
pulling power. But macOS only waits a few seconds after that `SIGTERM` before
escalating to `SIGKILL`, and Colima's lightweight VM (the lima/vz guest that
runs Postgres) often needs longer than that to tear down. If it gets killed
mid-stop, it can be left in a half-stopped state that its LaunchAgent then
refuses to restart into on the next boot, and the app then crash-loops on
`port 5432 ... connection refused` since Postgres never comes up. Recovery is
a manual `colima start` on the host (then `launchctl kickstart -k
gui/$(id -u)/com.assetmgt.app`), which you can avoid entirely by stopping
Colima cleanly up front.

So before an intentional reboot, from your dev machine:

```bash
# Wait for a clean guest teardown (no time limit, unlike the shutdown window).
ssh mini 'eval "$(/opt/homebrew/bin/brew shellenv)"; colima stop'

# Optional: stop the app so it isn't crash-looping against a vanishing DB
# during the shutdown. It autostarts again on the next boot.
ssh mini 'launchctl bootout gui/$(id -u)/com.assetmgt.app'

# Then reboot. Headless (no display attached), use an authenticated restart
# so the reboot skips straight past FileVault's pre-boot screen -- see
# "Running the Mini headless" below for why this doesn't weaken FileVault at
# rest the way disabling it would:
ssh mini 'sudo fdesetup authrestart'

# With a display still attached, a plain reboot is equally fine -- you'd
# just be present to unlock the pre-boot screen anyway:
ssh mini 'sudo shutdown -r now'
```

This only helps for reboots you initiate. A forced OS-upgrade restart or a
power failure (including a Powerwall Gateway cutover, see above) gives you no
chance to run `colima stop` first, so those still fall back to the manual
recovery above (and to the FileVault login wait in the previous section) --
this deployment has no UPS to absorb a cutover before it becomes a reboot,
see below.

### Running the Mini headless

Running with no display or keyboard permanently attached, administered
entirely over SSH and Screen Sharing, while keeping FileVault on and
auto-login off (see "Power outages and unattended restart" above for why
those stay non-negotiable). Two things need to be in place before you
actually pull the monitor, plus one optional piece for later.

**1. Screen Sharing, verified at the login window, not just from a logged-in
session.** `com.apple.screensharing` is a *system* daemon, so it comes up
before anyone logs in -- which is exactly what makes the `fdesetup
authrestart` path above useful headless: the reboot skips the pre-boot
screen, lands at the macOS login window, and you reach that window over VNC
to log in and start the three LaunchAgents, with no monitor involved.
Enable it in **System Settings → General → Sharing → Screen Sharing**, then
prove it reaches the login window specifically: connect, log out (or reboot
via `authrestart`), and confirm the VNC session still shows a login prompt
rather than dropping. If a third-party firewall is installed (Intego,
Little Snitch, etc.), add an explicit inbound allow rule for TCP 5900 --
this is the most likely reason a VNC test fails for a non-obvious reason.

Do this, and confirm it works, **before** removing the display -- there is no
way to fix a Screen Sharing misconfiguration once the only way in is Screen
Sharing itself. Budget for an HDMI dummy plug on the way out: a Mac with no
display attached still serves Screen Sharing, but at a degraded default
resolution without one.

**2. A spare keyboard and monitor, kept where the Mini physically lives.**
This deployment doesn't run a UPS (see "Power outages and unattended
restart" above -- pricing one on Amazon UK found every suitable model out of
stock or marked up 2-3x, not worth it for a sub-second cutover glitch), so a
Powerwall Gateway cutover still reboots the Mini and lands it at FileVault's
pre-boot screen exactly as it always did. Screen Sharing can't reach that
screen -- it runs before macOS, and before there's a network stack to reach
it with -- so recovery is physically walking over, plugging the spare
keyboard and monitor in, and unlocking it there. Once unlocked the Mini boots
to the login window and Screen Sharing takes over from there (log in over
VNC, the three LaunchAgents start as normal). **This is the expected, regular
recovery path for this deployment, not a rare fallback** -- it happens on
every cutover the Powerwall Gateway takes long enough to matter, which is
also why it's worth revisiting the UPS if it becomes annoying or prices
settle down.

A forced OS-upgrade restart or a genuine unattended power-off land here too,
same as any FileVault-on headless Mac.

**3. Optional: a UPS + the graceful-shutdown daemon, `com.assetmgt.upsmonitor`,
if one is added later.** Not currently installed -- there's no UPS on this
deployment for it to monitor (see above). Left here, built and tested, for if
that changes:

If your outages are actually a whole-house battery backup's grid-to-battery
transfer time (see above) rather than extended power loss, the UPS only
needs to bridge about a second, not ride out hours -- an M1 Mac Mini idles
around 7W and peaks under 40W, so even a small 600-650VA line-interactive
unit gives many minutes of headroom against that. The specification actually
worth paying for is **pure sine wave** output; a simulated-sine unit can
cause odd behaviour with Apple power supplies for very little saved cost.
Connect it to the Mini over USB so macOS can see its state natively:

```bash
pmset -g ps        # should list the UPS once connected, not just 'AC Power'
```

If the UPS ever does run down -- a longer outage than the battery backup
covers, or a dying UPS battery -- something needs to shut the Mini down
*before* it just dies, because an abrupt loss of power is indistinguishable
from the abrupt `SIGKILL` shutdown that leaves Colima's lima/vz guest
half-torn-down (see "Shutting down or rebooting the host gracefully" above).
`scripts/ups-shutdown.sh` polls `pmset -g ps` once a minute and, once the
battery drops below a threshold, runs the same `colima stop` →
stop-the-app → `shutdown -h now` sequence you'd run by hand for a planned
reboot.

This one is a **LaunchDaemon**, not a LaunchAgent like the other three jobs in
this repo -- it has to run whether or not anyone is logged in (in particular,
right after an `authrestart`, when the Mini sits at the login window with
none of the user's LaunchAgents started yet), and it needs to call `shutdown`
directly. That means it installs differently: to `/Library/LaunchDaemons`,
with `sudo`, not `~/Library/LaunchAgents`.

Because it runs as root, **nothing it executes may be writable by your user**.
The daemon therefore runs a root-owned *copy* of the script from
`/usr/local/libexec/assetmgt/`, never the one in this checkout, and its
`PATH` is the system directories only (`/opt/homebrew/bin` is owned by your
user on Apple silicon -- a root process that searched it first would run
whatever you, or anything running as you, dropped there). There is no
`__ASSETMGT_DIR__` placeholder in this plist for the same reason.

```bash
cd ~/claudecode/assetmgt
sudo install -d -o root -g wheel -m 755 /usr/local/libexec/assetmgt
sudo install -o root -g wheel -m 755 scripts/ups-shutdown.sh /usr/local/libexec/assetmgt/ups-shutdown.sh
sudo cp scripts/com.assetmgt.upsmonitor.plist /Library/LaunchDaemons/
sudo launchctl load /Library/LaunchDaemons/com.assetmgt.upsmonitor.plist
```

(If a previous version is already loaded, `sudo launchctl unload` it first --
same reasoning as the other three plists' install steps.) **After any edit to
`scripts/ups-shutdown.sh`, re-run the `sudo install` line** -- `redeploy.sh`
syncs the checkout, not the root-owned copy, and `scripts/preflight.sh` warns
when the two have drifted (and FAILs if the installed copy is writable by
anyone but root, or the daemon still points into the checkout).

Verify it the same way as the backup agent -- run it once exactly as launchd
will, rather than trusting a manual `bash scripts/ups-shutdown.sh` run under
your own shell's environment:

```bash
sudo launchctl kickstart -k system/com.assetmgt.upsmonitor
sudo cat /var/log/com.assetmgt.upsmonitor.log   # on AC power, this should stay empty
```

Set an OS-native backstop below this script's own threshold, so `pmset`'s own
emergency shutdown only ever fires if the script itself failed outright --
the graceful path above should always win first:

```bash
sudo pmset -u haltlevel 25 haltremain 3
```

(`scripts/preflight.sh`'s "Headless / UPS posture" section checks all of the
above -- if a UPS is present, whether it's visible to the system, the daemon
is installed and loaded, the `haltlevel` backstop is set -- alongside Screen
Sharing, which always applies. Without a UPS these read as `warn`s, not
`fail`s, by design: no UPS is currently expected on this deployment.)

**What this doesn't cover, even with a UPS installed:** a kernel panic, a
forced OS-upgrade restart that bypasses `authrestart`, or the UPS's own
battery failing outright all still land the Mini at the FileVault pre-boot
screen with no remote way past it -- keep the spare keyboard and monitor from
step 2 regardless.

### Keeping the host responsive

After an unclean shutdown, a host can feel sluggish for several minutes once
someone actually logs back in -- worth understanding rather than assuming the
app itself is the cost.

**It isn't.** Measured at steady state: the app process runs at ~0.1% CPU /
0.1% memory, and Colima's four VM-support processes sit at ~0% CPU / ~0.7%
memory combined. Colima itself is already configured conservatively
(`cpu: 2`, `memory: 1` GB, `vz` + `virtiofs` in `~/.colima/default/colima.yaml`).
Any post-reboot slowdown is macOS's own housekeeping catching up, not this
app:

- **Spotlight** (`mdworker`/`mds`/`mds_stores`) reindexing after an unclean
  shutdown invalidates its index.
- **Time Machine** (`backupd`) catching up on any backup run an outage
  caused it to miss.
- Login Items (browsers, etc.) restoring their previous session.
- Apple's own background services (OS/asset update downloads, on-device
  model catalogs) kicking off at login.
- Antivirus real-time scanning, if installed.

This settles back to normal on its own within several minutes, with no
intervention needed.

Two exclusions are worth making regardless, since they cost nothing to give
up: exclude `~/.colima` and this repo's `.venv` from both Time Machine and
Spotlight. This is the same reasoning as "Off-site database backups" below
applied to CPU/disk cost rather than to restorability -- Time Machine seeing
Colima's VM disk image as one large opaque blob is *already* why it's useless
as a restore path for the database; excluding it from backup and indexing
entirely just stops paying the ongoing cost of copying and indexing that blob
for no benefit:

```bash
tmutil addexclusion ~/.colima
tmutil addexclusion ~/claudecode/assetmgt/.venv
```

(Leave `backups/` itself included in Time Machine -- a handful of small
Postgres dumps costs nothing and is a free extra copy alongside S3.) Add the
same two paths under System Settings -> Spotlight -> Search Privacy to stop
indexing them too. Note this only shrinks a post-crash reindex, it doesn't
prevent one -- the Data volume as a whole still gets re-scanned after an
unclean shutdown regardless of these exclusions. (One thing that turned out
**not** to need fixing: `mdutil -s` reports the Time Machine destination
volume itself as "Indexing enabled", which reads as if Spotlight is indexing
the backup drive too -- but `mdfind -onlyin` against it returns nothing.
macOS already excludes Time Machine volumes from indexing; that flag is
misleading, not a real gap.)

The rest of the list above -- Login Items, automatic update downloads,
on-device model catalogs, antivirus scan scheduling -- are host preferences,
not something this app needs one way or the other. Worth tuning if
login-time responsiveness matters more than having them ready immediately,
but that trade-off belongs to whoever uses the machine, not to this
project.

### Off-site database backups

The database is the only thing here that isn't reconstructable from git --
everything you've recorded (serial numbers, purchase prices, warranty dates,
the whole investigation log) lives in one Postgres volume on one Mac. Time
Machine backing up the host does **not** protect it: Postgres runs inside a
Colima VM, so Time Machine only ever sees one large opaque VM disk image, not
the database files inside it -- a snapshot taken mid-write has no
crash-consistency guarantee and has never been proven restorable. If the
host were lost or damaged at the same time as (or instead of) its Time
Machine drive, the inventory would be gone for good.

This is deliberately a *durability* answer, not an *availability* one: after
a real disaster the app is down until you rebuild a host, and that's fine --
the app has to run on the LAN for nmap to work at all, so there's no way to
keep the dashboard itself reachable through a lost host. What this protects
is the data.

**What it costs:** at the database's current size (under 9 MB), thirty daily
dumps plus six monthly ones add up to well under $0.01/month on S3. Don't
let cost be a reason to put this off.

**What you accept:** backups run once nightly, so up to 24 hours of edits
can be lost in the worst case -- this is not point-in-time recovery. That's
a reasonable trade for how this app is actually used (a home inventory
edited in short bursts), but it's worth knowing plainly rather than
discovering it during a restore.

#### One-time AWS setup

This setup reuses an existing, full-S3-access IAM identity rather than
provisioning a new least-privilege one -- a deliberate choice, offset by S3
**Object Lock** (Compliance mode) instead of a scoped IAM policy. Object
Lock is enforced by S3 itself: once an object is written, *nothing* --
not this credential, not a compromised host, not the AWS account owner, not
AWS support -- can delete or overwrite it before its retention date. That's
a stronger guarantee than an IAM policy gives against a credential with full
access, but it comes with a real constraint: **Object Lock can only be
enabled at bucket creation**, not added to an existing bucket, and locking
an object is a genuine one-way door for the length of its retention period.

Bucket names are globally unique across *all* AWS accounts, and Object Lock
means picking the wrong region/name isn't a quick fix later -- pick
deliberately.

The bucket, versioning, encryption, public access block, and lifecycle
rules are set up via `aws-cli` rather than the console. Run these once real
credentials are in place (see below):

```bash
aws s3api create-bucket --bucket YOUR-BUCKET-NAME --region YOUR-REGION \
  --create-bucket-configuration LocationConstraint=YOUR-REGION \
  --object-lock-enabled-for-bucket
# us-east-1 is a special case: it rejects an explicit LocationConstraint,
# so drop --create-bucket-configuration entirely if using that region.

aws s3api put-bucket-versioning --bucket YOUR-BUCKET-NAME \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption --bucket YOUR-BUCKET-NAME \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block --bucket YOUR-BUCKET-NAME \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-lifecycle-configuration --bucket YOUR-BUCKET-NAME \
  --lifecycle-configuration file://lifecycle.json
```

`lifecycle.json` -- this is the retention policy, so it matters that all
three rules exist, not just the two that sound like "the point":

```json
{
  "Rules": [
    {
      "ID": "expire-daily-after-30-days",
      "Status": "Enabled",
      "Filter": { "Prefix": "daily/" },
      "Expiration": { "Days": 30 },
      "NoncurrentVersionExpiration": { "NoncurrentDays": 7 },
      "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 7 }
    },
    {
      "ID": "expire-monthly-after-6-months",
      "Status": "Enabled",
      "Filter": { "Prefix": "monthly/" },
      "Expiration": { "Days": 186 },
      "NoncurrentVersionExpiration": { "NoncurrentDays": 7 },
      "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 7 }
    },
    {
      "ID": "clean-up-expired-delete-markers",
      "Status": "Enabled",
      "Filter": { "Prefix": "" },
      "Expiration": { "ExpiredObjectDeleteMarker": true }
    }
  ]
}
```

Object Lock and Lifecycle are complementary, not redundant: Object Lock
stops an object being deleted *before* its retention date; Lifecycle is
what actually cleans it up *after*, so nothing accumulates forever. (On a
versioned bucket, `Expiration.Days` alone never deletes anything -- it
writes a delete marker and makes the old copy *noncurrent*, which is what
`NoncurrentVersionExpiration` actually reclaims. Skip any Glacier/IA
transition -- at ~1-2 MB per object it costs *more* than S3 Standard, due
to per-object minimums and retrieval fees.)

**On the deployment host**, add these four values to `.env` (not
`~/.aws/credentials` -- this backup job reads its AWS config directly out of
`.env`, alongside this app's other secrets). You can reuse an existing
full-access identity as described above, or a dedicated, more narrowly
scoped one if you'd rather not share credentials across purposes:

```bash
BACKUP_S3_BUCKET=YOUR-BUCKET-NAME
BACKUP_AWS_ACCESS_KEY_ID=...
BACKUP_AWS_SECRET_ACCESS_KEY=...
BACKUP_AWS_REGION=YOUR-REGION
```

As with every other secret in `.env`, this file is gitignored and excluded
from `redeploy.sh`'s rsync -- add these values directly on the host, not on
a dev machine.

#### Installing the backup LaunchAgent

```bash
cd ~/claudecode/assetmgt
cp scripts/com.assetmgt.backup.plist ~/Library/LaunchAgents/
sed -i '' "s|__ASSETMGT_DIR__|$(pwd)|g" ~/Library/LaunchAgents/com.assetmgt.backup.plist
launchctl load ~/Library/LaunchAgents/com.assetmgt.backup.plist
```

(If a previous version of this plist is already loaded, `launchctl unload`
it first -- `load` on an already-loaded label is a no-op, so an edited
plist needs the unload/reload round-trip to actually take effect.)

Before installing, confirm `aws` and `docker` resolve where the plist
expects them to (`ssh mini 'which -a aws docker'`) -- launchd agents don't
inherit a login shell's `PATH`.

Then verify the *installed* job actually works, not just the script run by
hand -- `launchctl kickstart` runs it exactly as launchd will every night,
which is the only way to catch a `PATH`/environment difference before 03:15
does:

```bash
launchctl kickstart -k gui/$(id -u)/com.assetmgt.backup
cat logs/backup.error.log   # should not exist, or be empty
cat backups/last-success    # should be a timestamp from moments ago
aws s3api list-objects-v2 --bucket YOUR-BUCKET-NAME --prefix daily/
```

It runs at 03:15 local time via `StartCalendarInterval`, with catch-up ticks
at 12:15 and 19:15. If the Mac is asleep at a tick, launchd runs it at the
next wake; if it was powered off / not-logged-in through 03:15 (FileVault --
see "Power outages and unattended restart"), the daytime ticks cover that day
once it's back. The job is idempotent -- it skips as soon as the day's
off-site daily exists -- so at most one tick per day does work and the rest
no-op (this is what makes running three times a day safe against the
COMPLIANCE Object Lock, which would reject a same-key re-upload). Each run
dumps the database with `pg_dump -Fc` from inside the Postgres container,
keeps the last 7 dumps locally in `backups/` (gitignored, and excluded from
`redeploy.sh`'s rsync -- that directory belongs to the host and is never
synced from a dev machine) and uploads to `s3://YOUR-BUCKET-NAME/daily/` with
Object Lock retention 30 days out. It also keeps one longer-lived copy per
calendar month in `monthly/` (locked 186 days): the first successful daily of
any month with no monthly copy yet creates it -- "ensure one per month" rather
than "only on the 1st", so an outage spanning the 1st doesn't skip the whole
month. It's silent on success, same as the log rotator; anything in
`logs/backup.error.log` means a run failed.

**How you know it's working day to day:** the **Since last DB backup** card
on the **Summary** page (turns red past 26 hours), and a `last_backup`/
`backup_stale` field on `/health`'s JSON response (this never affects
`/health`'s status code -- `redeploy.sh` uses it as a deploy gate, and a
stale backup shouldn't make an unrelated deploy look broken). On a plain dev
checkout this permanently reads "never" -- that's correct, not a bug; only
the deployed instance runs the nightly job.

#### Restoring

**Onto the existing host** (e.g. after a bad bulk edit or merge):

```bash
ssh mini && cd ~/claudecode/assetmgt

# Stop the app so nothing writes mid-restore.
launchctl unload ~/Library/LaunchAgents/com.assetmgt.app.plist

# Snapshot the CURRENT (bad) state first -- a restore is destructive, and if
# you picked the wrong dump you want a way back. Reads BACKUP_S3_BUCKET and
# credentials straight out of .env, same as the nightly run.
./scripts/backup-db.sh

# A manual `aws` command, unlike backup-db.sh, doesn't load .env itself --
# load the same three values into this shell first.
export AWS_ACCESS_KEY_ID=$(grep -m1 '^BACKUP_AWS_ACCESS_KEY_ID=' .env | cut -d= -f2-)
export AWS_SECRET_ACCESS_KEY=$(grep -m1 '^BACKUP_AWS_SECRET_ACCESS_KEY=' .env | cut -d= -f2-)
export AWS_DEFAULT_REGION=$(grep -m1 '^BACKUP_AWS_REGION=' .env | cut -d= -f2-)

# Fetch the dump you want.
aws s3 cp s3://YOUR-BUCKET-NAME/daily/assetmgt-YYYY-MM-DD.dump /tmp/restore.dump

# Drop and recreate the database, then restore into it. --force terminates
# any lingering connections; --no-owner tolerates a different POSTGRES_USER.
docker compose exec -T db sh -c 'dropdb   -U "$POSTGRES_USER" --force "$POSTGRES_DB"'
docker compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' \
  < /tmp/restore.dump

# Bring the schema forward if migrations shipped after this dump was taken --
# the dump includes alembic_version, so this applies only what's missing.
# Run this AFTER the restore, never before.
source .venv/bin/activate && alembic upgrade head

launchctl load ~/Library/LaunchAgents/com.assetmgt.app.plist
curl -s http://127.0.0.1:8000/health; echo
```

Drop-and-recreate rather than `pg_restore --clean` into the live database:
`--clean` leaves behind anything that exists now but didn't when the dump
was taken (a column or table added by a migration since), producing a
schema that matches neither the dump nor `alembic upgrade head`.

**On a brand-new machine from zero** -- the actual disaster this exists
for, e.g. the host itself was lost or destroyed: see
["Installing on a new Mac"](#installing-on-a-new-mac) below for the full
runbook. (If you're here because of a bad bulk edit or merge on a host
that's otherwise fine, you want the "Onto the existing host" instructions
above, not this.)

**Test this once, into a disposable database, so "we have backups" is a
verified fact and not a hope:**

```bash
ssh mini && cd ~/claudecode/assetmgt

export AWS_ACCESS_KEY_ID=$(grep -m1 '^BACKUP_AWS_ACCESS_KEY_ID=' .env | cut -d= -f2-)
export AWS_SECRET_ACCESS_KEY=$(grep -m1 '^BACKUP_AWS_SECRET_ACCESS_KEY=' .env | cut -d= -f2-)
export AWS_DEFAULT_REGION=$(grep -m1 '^BACKUP_AWS_REGION=' .env | cut -d= -f2-)

aws s3 cp s3://YOUR-BUCKET-NAME/daily/assetmgt-$(date -u +%F).dump /tmp/verify.dump

docker compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" assetmgt_restoretest'
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d assetmgt_restoretest --no-owner' \
  < /tmp/verify.dump

# Row counts should match the live database.
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d assetmgt_restoretest -c \
  "select (select count(*) from asset) assets, (select count(*) from assetinterface) ifaces;"'
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "select (select count(*) from asset) assets, (select count(*) from assetinterface) ifaces;"'

docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" assetmgt_restoretest'
rm /tmp/verify.dump
```

Download-then-restore is deliberate here rather than restoring the local
`backups/` copy -- that's the only way this test actually proves S3 has
good bytes, not just that the dump was good the moment it was taken. Re-run
this after any Postgres major-version bump in `docker-compose.yml` -- a
`-Fc` dump from one major version does not restore into an older server.
Note that the S3 object downloaded here (`daily/assetmgt-...`) can't itself
be deleted before its Object Lock retention date even if you wanted to --
that's expected, not a bug, and irrelevant to this test since only the
local `/tmp/verify.dump` copy needs cleaning up.

**What isn't backed up:** `.env` -- it holds `APP_ADMIN_PASSWORD`,
`UNIFI_API_KEY`, `NVD_API_KEY`, `ANTHROPIC_API_KEY`/`OPENROUTER_API_KEY`, and
the `BACKUP_AWS_*` credentials themselves, and it's gitignored *and* excluded from
`redeploy.sh`'s rsync, so after losing the host it exists nowhere. A
restore brings back all the *data*, but the app still won't start (and the
next backup can't run) without `.env` recreated from elsewhere. Copy its
contents into your password manager now -- there's no code fix for this,
just something to actually go and do.

**Keeping the host's `.env` and the template in sync.** Because the host's
`.env` is never synced, a key you add there (a new API key, say) is invisible
to your dev machine and to `.env.example`. `scripts/env-structure.sh` closes
that gap safely -- it compares the *key names, order and headings* across your
Mac, the deployment host and `.env.example` and never moves a value (the host
is queried for key names only, over ssh, and re-checked locally so no secret
can come back):

```bash
./scripts/env-structure.sh                        # three-way report
./scripts/env-structure.sh --write-example --from remote --dry-run
```

`--write-example` regenerates `.env.example`: existing keys keep their vetted
placeholder and comment verbatim, and each *new* key is appended as an empty
`KEY=` with a `TODO` for you to document -- its real value is never copied.
Review the printed diff, fill in the `TODO`s with publish-safe placeholders,
then commit. (A heading you edited on the host has to be re-typed here by
hand, by design -- free-text typed on the host is exactly where a stray
secret or personal identifier could hide, so it's never copied into a
tracked file.)

## Installing on a new Mac

This is the runbook for the actual disaster the backup exists for: the host
itself is lost, destroyed, or being replaced, and you're starting from a
brand-new machine with nothing on it but this git repo and an S3 bucket. (If
the existing host is fine and you just need to undo a bad bulk edit or
merge, use ["Onto the existing host"](#restoring) above instead -- this
section assumes there's no existing install to fall back into.)

**Before you start:** the credentials you need to download the backup --
`BACKUP_S3_BUCKET`, `BACKUP_AWS_ACCESS_KEY_ID`, `BACKUP_AWS_SECRET_ACCESS_KEY`,
`BACKUP_AWS_REGION` -- live only in `.env`, which (as noted just above) is
*not* part of the backup and doesn't survive losing the host. Get these, and
everything else `.env` held, out of your password manager first -- this
runbook can't start without them.

### 1. Prerequisites

```bash
brew install colima docker docker-compose nmap awscli
mkdir -p ~/.docker
# add "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"] to ~/.docker/config.json
brew services start colima
```

Same as "One-time setup" above, plus `awscli` -- needed here to pull the dump
down from S3 (the same Python note applies: this line already gets you a
current Python via `nmap`/`awscli`'s own dependencies). Two more things worth
knowing before you're deep into this:

- Every path above assumes Apple silicon's `/opt/homebrew` prefix -- it's
  hardcoded into all three LaunchAgent plists' `PATH` and into
  `redeploy.sh`'s `brew shellenv` call. On an Intel Mac (`/usr/local`),
  those all need editing to match.
- The AWS CLI v2 `.pkg` installer (as opposed to `brew install awscli`) puts
  `aws` in `/usr/local/bin` instead -- the backup plist's `PATH` already
  covers both locations, so either install method works. (This is also the
  route that skips the Python side-effect above -- if you use the `.pkg`
  installer rather than `brew install awscli`, `./scripts/preflight.sh`'s
  Python check is what catches it if you land on an old interpreter.)

### 2. Clone and configure

```bash
git clone https://github.com/rohitafish/home-asset-manager.git
cd home-asset-manager
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.txt
cp .env.example .env
chmod 600 .env
cp scripts/hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

A couple of things a fresh clone doesn't give you that "One-time setup" glosses over:

- **Recreate `.env` by hand**, all of it, using `.env.example` as the
  canonical list of keys -- this file is not part of the backup and never
  has been (see "What isn't backed up" above). `APP_ADMIN_PASSWORD` must be
  a real value, not left unset or as the placeholder `change-me` -- the app
  refuses to start otherwise. `UNIFI_API_KEY` and `NVD_API_KEY` need
  regenerating rather than copying (see their setup sections above); make
  sure `DEFAULT_OWNER`/`SECONDARY_OWNER_NAME`/`SECONDARY_OWNER_HOSTNAME_KEYWORD`
  match what the old instance used, or newly discovered assets start getting
  assigned differently than the restored history implies.
- **Install the pre-push hook** (the last line above) -- git never clones or
  syncs hooks, so a fresh checkout has no PII/test/lint gate on `git push`
  until you copy it into place. `./scripts/preflight.sh` (next) checks for
  this and warns if it's missing or has drifted from the tracked template.

Worth a checkpoint here, before investing effort in the restore below:

```bash
./scripts/preflight.sh
```

```bash
docker compose up -d
```

**Do not run `alembic upgrade head` yet** -- this is the one place this
section deliberately differs from "One-time setup". The dump you're about to
restore carries its own `alembic_version`; migrating this empty database
first and restoring the dump on top of it produces a schema matching
neither.

### 3. Restore the database from S3

A bare `aws` command doesn't read `.env` itself, unlike `backup-db.sh` --
load the same three values into this shell first:

```bash
export AWS_ACCESS_KEY_ID=$(grep -m1 '^BACKUP_AWS_ACCESS_KEY_ID=' .env | cut -d= -f2-)
export AWS_SECRET_ACCESS_KEY=$(grep -m1 '^BACKUP_AWS_SECRET_ACCESS_KEY=' .env | cut -d= -f2-)
export AWS_DEFAULT_REGION=$(grep -m1 '^BACKUP_AWS_REGION=' .env | cut -d= -f2-)
```

See what's actually there before picking one -- useful in general, and the
only way to know whether the daily dumps still cover the outage or you need
a monthly one instead:

```bash
aws s3 ls s3://YOUR-BUCKET-NAME/daily/
aws s3 ls s3://YOUR-BUCKET-NAME/monthly/

aws s3 cp s3://YOUR-BUCKET-NAME/daily/assetmgt-YYYY-MM-DD.dump /tmp/restore.dump
```

`daily/` holds 30 days of dumps, `monthly/` holds 186 -- if the host had
already been down for a while, `monthly/` may be the only copy left.

Unlike restoring onto an existing host, there's no `dropdb`/`createdb` step
here -- `docker compose up -d` above already created an empty `assetmgt`
database, so restore straight into it:

```bash
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' \
  < /tmp/restore.dump
```

`-T` is mandatory, same as everywhere else this appears -- without it,
Compose may allocate a pseudo-TTY, which performs newline translation and
silently corrupts the archive. `--no-owner` tolerates a different
`POSTGRES_USER`. Leave `docker-compose.yml` pinned at `postgres:16-alpine`
for this step -- a `-Fc` dump does not restore into an older major version,
so bump that pin (if you ever do) only after this restore succeeds, never
before.

Only now bring the schema forward:

```bash
source .venv/bin/activate && alembic upgrade head
```

The dump includes `alembic_version`, so this applies only whatever migrations
shipped after the dump was taken -- run this after the restore, never before.

### 4. Install the LaunchAgents

```bash
for p in app logrotate backup; do
  cp scripts/com.assetmgt.$p.plist ~/Library/LaunchAgents/
  sed -i '' "s|__ASSETMGT_DIR__|$(pwd)|g" ~/Library/LaunchAgents/com.assetmgt.$p.plist
  launchctl load ~/Library/LaunchAgents/com.assetmgt.$p.plist
done
```

Same `cp`/`sed`/`launchctl load` pattern as each agent's own section above
("Running it" for `com.assetmgt.app` and `com.assetmgt.logrotate`,
"Installing the backup LaunchAgent" for `com.assetmgt.backup`) -- see those
for what each one does. Before relying on the backup agent, confirm `aws` and
`docker` resolve where its plist expects (`which -a aws docker`) -- launchd
agents don't inherit a login shell's `PATH`.

Privileged (`-sS`) nmap scans need nothing installed: they're a terminal-only
`python -m discovery.cli nmap --sudo` that prompts for your password (see "Nmap
privileges" above). If this host ever had the old `/etc/sudoers.d/nmap-assetmgt`
rule, remove it -- `scripts/preflight.sh` will say so.

### 5. Verify

```bash
./scripts/preflight.sh
curl -s http://127.0.0.1:8000/health; echo
launchctl kickstart -k gui/$(id -u)/com.assetmgt.backup
cat logs/backup.error.log    # should not exist, or be empty
cat backups/last-success     # should be a timestamp from moments ago
```

- `preflight.sh` should report zero `FAIL`s at this point -- all three
  LaunchAgents installed with no leftover `__ASSETMGT_DIR__`, Postgres
  reachable and at the latest migration, `.env` complete.
- `/health` should return `{"status": "ok", ...}`, and the Assets list should
  show the expected count. Spot-check the investigation log and a few
  purchase/warranty fields too -- that's data that exists nowhere else.
- The Summary page's **Since last DB backup** card will read "never" until
  this new host completes its own first nightly run -- `backups/last-success`
  is host-side state, not part of the dump or `redeploy.sh`'s rsync. The
  `kickstart` above is what sets it for the first time on this host.
- The bucket is versioned, so re-uploading under today's UTC date (as the
  nightly job will do tonight) writes a new version rather than colliding
  with the Object-Locked object already sitting at that key -- nothing to
  worry about there.
- Last, re-point your dev machine's `mini` SSH alias at this new host (or set
  `DEPLOY_HOST`), then confirm `./scripts/redeploy.sh` runs clean end to end.

**What this doesn't bring back:** `.env` (covered above), plus host-side state
that was never part of the dump in the first place -- the old `logs/`
directory, the old local `backups/` history, and the old `last-success`
marker. All three are gitignored *and* excluded from `redeploy.sh`'s rsync,
which is exactly why none of them come back on their own.

## Using it

1. Open the dashboard, go to **Discovery**, click **Run UniFi discovery
   now** and/or **Run nmap scan now** (or **Run everything**).
2. Go to **Triage Queue** — every newly discovered device lands here until
   confirmed. Owner and criticality are already auto-assigned (owner
   defaults to you), so click **Confirm** to mark a device `active` in one
   step, or **Confirm all** to clear the whole queue at once if everything
   listed is recognised. Use **Edit** instead if something needs correcting
   first (rename, reclassify, lock the hostname/vendor, mark
   internet-facing) — or delete the entry if it's not your device.
3. Click **Check for vulnerabilities** on the Discovery page to match
   detected service versions against NVD/EPSS/CISA KEV.
4. Check **Vulnerabilities** for the open findings list (grouped by
   severity, with SLA due dates and overdue flags) and **Summary** for
   overall coverage/posture metrics. Marking an asset internet-facing (or
   not) on its **Edit** page updates any open finding's exposure/SLA due
   date on the next vulnerability check automatically -- for a finding
   whose underlying service no longer matches (so it's no longer revisited
   by a normal check), backfill it directly instead:
   ```bash
   python -m discovery.cli resync-exposure          # dry run: prints the diff, writes nothing
   python -m discovery.cli resync-exposure --apply  # writes, one timeline note per change
   ```

Some devices legitimately create more than one asset with no reliable way to
auto-correlate them — e.g. a phone with a "Fixed" private Wi-Fi address gets
a different (but stable) MAC per network, or a device with separate wired/
wireless interfaces. Check the **Duplicates** page occasionally. It surfaces two kinds of
possible-duplicate pairs: assets sharing the **exact same name**, and pairs
already **linked as the same physical device** (via Investigate or the
assistant) but not yet merged. For either, pick which record survives and
**Merge** with one click — the other's interfaces/services/findings fold into
it. Merging is never automatic: a name match alone isn't a safe enough signal.
For a *linked* pair that turns out to be two separate devices (e.g. a control
box and its Wi-Fi module), click **Dismiss — not the same device** instead:
that removes the link, keeping both assets, and also records the pair as
dismissed on Investigate (below) so it doesn't reappear there either.

On the **Assets** list, click any column heading (ID, Hostname, Type, Vendor,
Owner, Criticality, Status, Location, Last seen) to sort by that column; click
again to reverse the direction. Existing type/criticality/status filters are
preserved when you sort. The first column is the asset's **ID** — the number
the investigation assistant and notes use to refer to it (e.g. "asset #33"), so
the list is easy to cross-reference. (Internet-facing isn't a list column; it's
on each asset's detail page and **Edit** form.) Vendor/OEM normally
comes from nmap's MAC OUI lookup, with a hostname-based fallback (e.g.
"Jordan's iPhone" → Apple) for devices where that can never work --
notably anything using a private/randomized Wi-Fi address, which
deliberately doesn't match a real manufacturer OUI. UniFi's own
infrastructure devices (APs, switches, the gateway) are always tagged
"Ubiquiti" directly. Vendor is editable and lockable from an asset's
**Edit** page too, same as hostname — useful if a guess was wrong or a
device has no name-based signal at all; once locked, discovery won't
overwrite it. The total asset count shown
above the filters refreshes automatically every 5 seconds, so it stays
current if a discovery run adds or removes assets while you have the page
open.

The REST API (`/api/assets/...`) is documented at `/docs`.

## Investigation features

Discovery tells you *what* is on the network; these features help you figure
out things it can't -- which physical device a set of interfaces really
belongs to, which room a smart plug or speaker is actually in, which speaker
in a stereo pair is left vs. right.

### Investigation log

Each asset's detail page has an append-only **Investigation log** instead of
a single overwritable notes field -- every note is timestamped and
attributed (you, or `claude` for anything the assistant applied), and nothing
is ever lost to a later edit.

### Locations (rooms)

**Locations** in the nav lets you define rooms (Kitchen, Lounge, ...) and
assign any asset to one from its edit page, plus a free-text **Position**
field for the specific spot ("socket behind the sofa, left of the TV"). Each
location's page lists everything assigned to it -- this is the direct answer
to "which socket is this smart plug actually in."

### Identification probes

An asset's detail page shows a **Run identification probe** button when a
known, read-only probe applies to it (matched by vendor/hostname keywords or
an already-discovered open port). Probes only ever make read-only local
network requests -- **none of them can change a device's state** (no probe
ever toggles a plug or a speaker):

- **Sonos** (port 1400) -- reads the player's own zone name and, for a
  stereo pair, which channel (left/right) it is via the standard UPnP
  device-description and `ZoneGroupTopology` endpoints.
- **TP-Link Kasa** (port 9999, legacy protocol) -- reads the alias you set in
  the Kasa app, which is often already the room/socket name. Does **not**
  work on Tapo devices or newer Kasa firmware, which speak the different
  KLAP protocol over HTTP instead -- the probe reports this plainly rather
  than pretending the device isn't there.
- **Generic SSDP/UPnP** -- a last-resort fallback for anything else that
  answers a unicast UPnP discovery query.

Each probe run is recorded as evidence on the asset page, with any suggested
field changes (room name, model, firmware) offered as one-click **Apply**
buttons -- nothing is changed automatically.

### Ping (reachability)

A separate **Ping** button next to **Run identification probe** sends a
single ICMP echo request to the asset's known IP(s) -- a quick "is this thing
even on right now?" check, independent of whether any identification probe
applies. It also runs automatically as part of **Run identification probe**,
so a "no response" from Sonos/Kasa/SSDP comes with reachability context
explaining whether the device answered ICMP at all. Unlike a privileged nmap
scan, **ping needs no elevated privileges** -- macOS grants
unprivileged ICMP to any user, so there's nothing extra to set up.

A ping result replaces the previous one for that asset+IP rather than
piling up, since it's cheap and meant to be re-run often -- unlike
identification evidence, which accumulates as history. And as with any ICMP
check: **no reply is not proof the device is off.** Plenty of IoT gear and
Wi-Fi clients in power-save mode never answer ICMP, and a device on a
different VLAN than wherever the app is deployed may be reachable to nothing
at all.

### Asset identity and support data

Each asset's detail page has an **Identity & support** section for the data
you'd actually need on a support call or an insurance claim: serial number,
model number, model identifier, purchase date/price, warranty expiry and
replacement value. Two fields are collected automatically, everything else is
manual entry via the asset's **Edit** page:

- **UniFi devices** (the gateway, APs, switches) get their serial number and
  SKU-style model number automatically as part of every **Run UniFi
  discovery now** — see the "Serial numbers" note under UniFi API key above
  for where this data comes from.
- **The Mac this app is running on** gets its own serial number, model
  number (e.g. `MGNR3B/A`) and model identifier (e.g. `Macmini9,1`) via
  **Collect this Mac's hardware identity** on the Discovery page, reading
  `system_profiler` directly. This can only ever describe the host it runs
  on — run it on the deployed instance, not a dev checkout elsewhere, or
  you'll enrich the wrong asset (if any). It resolves "this host's own asset
  row" by serial number first, then by matching this host's real (non-
  randomized) MAC addresses against the inventory; if that's ambiguous or
  finds nothing, it records why in the discovery run history rather than
  guessing.

Everything else — iPhones, iPads, Apple Watch, a connected home battery
system, and any other asset a collector can't reach — is manual entry only.
(Third-party Mac model catalogs like [Mactracker](https://mactracker.ca)
aren't a reliable source for this either — their data is typically locked
behind their own app's sync and permissions model — so `system_profiler`
above is the more direct route for the one asset it can cover.)

Tick **Lock identity** on an asset's Edit page to stop either collector from
overwriting a serial/model you've corrected or entered by hand — it guards
exactly those three fields (serial number, model number, model identifier),
the same way **Lock hostname**/**Lock vendor** already work elsewhere on
that form.

The **Valuables** page in the nav lists every asset with identity, purchase
or cover data recorded, plus any high-criticality asset regardless (so it
also doubles as a worklist of what's still worth filling in), with **Print**
and **Download CSV** buttons — the thing to hand to an insurer or a support
desk. `/valuables?all=1` shows the full inventory instead.

#### Model numbers

`model_number` (the manufacturer part number / SKU, e.g. Apple's `A2374`) is
usually typed by hand. But when you save an asset that has **vendor, serial
number, model, and purchase date all filled** and the `model_number` still
blank, the app asks Claude for a best guess and fills it in — marked
`(unverified)` — with a note recording the guess. The purchase date is what
lets it pin a device's generation (an Amazon Echo bought in 2019 is the 3rd
Gen). It only fills a blank field, never overwrites, skips assets with a locked
identity, and does nothing if there's no API key configured — so it's an opt-in
convenience that disappears if you unset `ANTHROPIC_API_KEY`/`OPENROUTER_API_KEY`.
The serial gates the feature but is **not** sent to the API. Verify a guess off
the device and drop the `(unverified)` marker when you do.

#### Replacement values

`replacement_value` is what you'd insure the item for. You can type it in by
hand on any asset's **Edit** page — or leave it blank and it's filled in for
you on save, computed from the purchase price and date by the same new-for-old
rule below (only when blank, so a figure you type is kept). To value the whole
estate at once, backfill it from the rule with:

```bash
python -m discovery.cli revalue          # dry run: prints the diff, writes nothing
python -m discovery.cli revalue --apply  # writes, one timeline note per change
```

(run it where the database lives, i.e. on the deployment host, not a dev
checkout elsewhere). Each write records an `AssetNote` with the old and new figure, so
a value the rule overwrites is always recoverable from the asset's timeline.

The rule is **new-for-old**: what an equivalent *new* item costs today, which
is what most UK contents policies actually pay out — not a depreciated
("indemnity") figure. Insuring on a depreciated number is the trap to avoid:
underinsurance triggers the **average clause**, which scales down *every*
payout in proportion, even a small one.

```
replacement_value = purchase_price × (1 + drift)^years,  floored at purchase_price
```

`drift` is the annual change in the price of the current equivalent *new*
model — not wear. Consumer tech holds its price point across generations while
equivalent-spec prices fall, so those cancel and most categories sit at 0%:

| Asset type | drift/yr | why |
|---|---|---|
| computers, phones, servers, network gear, media | 0% | successor models hold the price; spec deflation offsets inflation |
| IoT | 2% | blends cheap smart devices with *installed* energy kit whose replacement includes labour |
| cloud service, software | excluded | subscriptions/licences aren't insurable contents |

Items over 8 years old are still valued but flagged for review — past that
there's often no like-for-like current model, so a real quote beats the
formula. The command values only assets that have **both** a purchase price
and date; anything with a date but no price is reported as a gap to fill in.

Two things worth doing before you rely on the numbers for a claim:

1. **Confirm the sum insured is new-for-old**, per above — this is the whole
   point of the figure.
2. **A fixed home battery system usually sits under *buildings* cover, not
   contents**, and is often over a single-article limit, so it may need
   specifying separately. It's also likely among the largest single values
   in your inventory — one call to the insurer to place it correctly is
   worth more than any formula.

### Same-device correlation (Investigate)

**Investigate** in the nav scores pairs of assets that might be the same
physical device -- e.g. one wired NIC and one wireless NIC on the same
laptop -- based on evidence like adjacent MAC addresses sharing an OUI,
similar hostnames, and mismatched wired/wireless connection types, with the
reasoning shown for every candidate. **Link as same device** is
non-destructive: both asset records and both histories are kept, just
related to each other. This is different from the existing **Duplicates**
page, which destructively merges two records into one -- use Duplicates when
discovery genuinely created two rows for the same interface, and Investigate
when a device legitimately has two distinct identities worth tracking
separately.

Several guards keep this from flagging things that only *look* related:
vendor names and household member names (pulled live from each asset's own
`vendor`/`owner` field) never count as hostname evidence on their own, nor
does any word that recurs across three or more of your devices' names (a
room name, a shared product word like "clock" on multiple smart speakers,
an annotation you've added like "(confirmed)"); a wired/wireless mismatch
only counts as *corroborating* some other signal, never as evidence by
itself; and two network-infrastructure devices (APs, switches, gateways)
are never compared against each other at all, since owning several
identical-model units in different rooms is the normal shape of a mesh
network, not a coincidence worth flagging.

If a candidate pair is actually two separate devices, click **Dismiss — not
the same device** (or **Dismiss all shown** to clear the whole current list
at once). A dismissed pair stops being offered here -- but only while the
evidence stays the same: if either asset's hostname, vendor, type, or MACs
later change, it's re-scored and can reappear, since a dismissal is a
judgement about the evidence at the time, not a permanent veto. Dismissing a
pair here and dismissing the equivalent *linked* pair on Duplicates (above)
are kept in sync -- either one is enough to stop both pages suggesting it.

### Investigation assistant (Claude)

Each asset's detail page has a chat panel backed by the Claude API. It can
read full context about the asset (and search/inspect others, and run the
read-only probes above) to help reason through an investigation, but it
**never writes to the inventory directly** -- every change it wants to make
(rename a device, set its location, add a note, link two assets, fill in a
serial number/purchase date/price/warranty expiry) is recorded as a proposal
with an **Apply**/**Discard** button. Nothing changes until you click Apply,
and every applied change is logged to the asset's Investigation log so
there's a clear record of what Claude did and why. When a turn produces several
proposals, an **Apply all (N)** button at the top of the list applies them in
one click.

You can attach a receipt, packaging photo, or warranty PDF to a chat message
(up to 5 files, 15MB each) and ask Claude to read it -- it'll propose the
identity/purchase/cover fields the document supports (serial number, model,
model number, purchase date, price, replacement value, warranty expiry),
which then show up on the Valuables page once applied, same as any manually
entered value. The file itself is never stored -- it's sent to the API for
that one turn and discarded; only the extracted facts, and a note recording
what filename they came from, persist in the database.

An invoice often covers **several devices**. Analysed on one asset's page,
Claude proposes each covered device's fields against the right asset — even
ones you're not currently looking at — and those cross-asset proposals appear
on the same page under an **"Also proposed — <asset>"** group, so you review and
apply the whole invoice from where you uploaded it rather than hunting for each
device. (In the transcript, an action that targets another asset names it, e.g.
`Proposed: set purchase_price to "190.80" (asset #33)`.)

This is optional: set `ANTHROPIC_API_KEY` in `.env` to enable it (get one at
https://console.anthropic.com/); `ANTHROPIC_MODEL` optionally overrides the
default model. Leave both `ANTHROPIC_API_KEY` and `OPENROUTER_API_KEY` unset
and the panel just shows a "not configured" note -- everything else in the
app works fine without it.

If you'd rather not hold a separate Anthropic key, set `OPENROUTER_API_KEY`
instead (get one at https://openrouter.ai/keys) -- it's used as a fallback
only when `ANTHROPIC_API_KEY` is unset, and routes the same requests through
OpenRouter's Anthropic-compatible endpoint rather than api.anthropic.com.
`ANTHROPIC_MODEL` still applies either way.

Note that this sends the asset's data (hostnames, MAC/IP addresses, vendor
info, notes, probe results) -- and, if you attach one, the contents of the
file itself -- to the Anthropic API (or OpenRouter, if that's the credential
in use) to generate a response.

#### What a turn costs, and checking the cache is working

Every API call the assistant makes logs its token usage at INFO, so cost is
visible rather than guesswork:

```bash
ssh mini "grep 'app.assistant' ~/claudecode/assetmgt/logs/app.log"
```

```
YYYY-MM-DD HH:MM:SS INFO  app.assistant asset=12 iter=0 via=anthropic
                          in=2 out=5 cache_read=15257 cache_write=33
```

`via=` shows which credential served the call. The two cache counters are
the ones that matter for cost: the system prompt, tool definitions, and
prior conversation are sent on *every* turn, and cache reads bill at roughly
a tenth of the normal input rate, so a warm cache is about a 10x saving on a
typical turn. One line is emitted per API call, which means several per turn
when Claude uses tools -- each is separately billed.

Reading it:

- **`cache_read` large, `in` tiny** -- working as intended.
- **`cache_read=0` with a matching `cache_write`** on the first turn of a
  conversation is normal; that call is populating the cache.
- **`cache_read=0` on repeat turns for the same asset** means something is
  changing the static prefix between calls and the cache never hits. That is
  a real (and expensive) bug -- the fix is generally to make sure the system
  prompt and tool definitions are built once and reused unchanged across a
  conversation's turns, not rebuilt (even slightly differently) each time.
- **`cache_read=n/a`** means the provider didn't report the field at all,
  which is deliberately distinguished from a genuine zero: it indicates the
  endpoint doesn't support prompt caching, not that the cache missed.

Entries are `INFO`, so they're suppressed if you set `LOG_LEVEL=WARNING`.

## CLI reference

```bash
python -m discovery.cli unifi              # UniFi clients + devices only (incl. serials)
python -m discovery.cli nmap [--sudo]      # nmap ping-sweep + service scan
python -m discovery.cli local-mac          # this host's own hardware identity (macOS only)
python -m discovery.cli enrich             # NVD/EPSS/KEV matching
python -m discovery.cli all                # all four, in order
python -m discovery.cli account-import [--apply]   # see "Vendor account import" below
python -m discovery.cli sonos [--apply]            # see "Sonos household discovery" below
```

### Vendor account import

Some device details — model, serial number, purchase/registration date — only
exist in a vendor's own account, not on the network: neither Amazon nor Sonos
exposes them to a home user via an API (see below), and UniFi only ever sees a
MAC, an IP, and whatever name the device advertised. `discovery/account_import.py`
imports this data from a hand-transcribed file instead.

1. Save an export from each vendor's device list into `devices/` at the repo
   root (a screenshot, a PDF, whatever the vendor's site gives you). This
   directory is gitignored — it holds serials and room names, which belong
   in `.pii-denylist`, not in git history.
2. Transcribe the devices you want imported into `devices/accounts.json`:
   ```jsonc
   {
     "amazon": {
       "source_document": "<filename in devices/>",
       "captured": "<date you captured it, YYYY-MM-DD>",
       "devices": [
         {
           "asset_id": 42,                 // required for amazon: Amazon's serial
                                            // isn't a MAC, so there's no automatic
                                            // way to find the matching asset --
                                            // look it up on the dashboard and pin
                                            // it by hand, once (illustrative
                                            // example values below, not real data)
           "account_name": "Garage Echo Dot",
           "model": "Echo Dot",
           "model_number": "5th generation",
           "serial": "G0EXAMPLE00001A",
           "registered": "2022-01-15",
           "hostname": "Amazon Echo Dot (Garage)"  // omit to leave the name alone
         }
       ]
     },
     "sonos": {
       "source_document": "<filename in devices/>",
       "captured": "<date>",
       "devices": [
         {
           "asset_id": null,               // Sonos devices match automatically --
                                            // a Sonos serial is the player's MAC
                                            // plus one check character, so
                                            // discovery/account_import.py derives
                                            // the MAC and looks it up
           "account_name": "Garage",
           "model": "Sonos One",
           "model_number": null,
           "serial": "AABBCCDDEEFF1",         // canonical form -- see "Sonos
                                               // household discovery" below;
                                               // dashes/colon are stripped
                                               // automatically if present
           "registered": "2022-03-01",
           "hostname": null,
           "location": "Garage"             // optional, any vendor: fill-only --
                                             // must exactly match an existing
                                             // Location.name (never auto-created
                                             // from a typo) and is only written
                                             // when the asset has none yet
         }
       ]
     }
   }
   ```
3. `python -m discovery.cli account-import` (no `--apply`) prints a full
   per-asset diff and writes nothing. Read it before proceeding — an Amazon
   `asset_id` pin is a manual guess and worth double-checking.
4. `python -m discovery.cli account-import --apply` writes the plan: fills
   `serial_number`/`model_number` (skipped if `identity_locked`), refreshes
   `model`, fills `purchase_date` only if it was empty (a vendor's
   registration date is an approximation, never a receipt), and — only for
   entries carrying a `hostname` — renames the asset and leaves it
   `hostname_locked`. Every changed asset gets one `AssetNote(author="imported")`
   recording exactly what was written and why. Deliberately does not go
   through `reconcile_into_db` (see the module docstring for why) — it
   never touches `last_seen`, and it never creates an asset for a device
   with no match; an unmatched Sonos entry (e.g. a player UniFi has never
   seen) is reported, not invented.
5. There is no web route for this on purpose — it needs a file the browser
   can't supply, and a locked-hostname rewrite deserves a human reading the
   dry-run diff first, not a one-click button.

**Why hand-transcribe instead of calling an API?** For Amazon, there isn't
one: the only official API that returns serial/model (Alexa Smart
Properties' *Endpoints* API) requires an Amazon Business account enrolled in
a commercial hospitality/healthcare programme, not available to a home
account. So for Amazon, an occasional re-transcription of the account page
remains the only option. Sonos is different — see below, it now has a real
discovery collector — but `account-import` still has one job that collector
can't do: a *registration/purchase date*, which only the account page shows,
never the device's own local API.

### Sonos household discovery

Unlike Amazon, Sonos players expose a local API (port 1400 — the same one
`probes/sonos.py`'s per-asset identification probe already speaks) that
returns serial/model/model number/firmware/room name directly — no account
page, no OAuth. `discovery/sonos_household.py` calls `GetZoneGroupState` on
an already-known Sonos IP and gets back the **entire household** in one
response, including bonded satellites (a Sub, rear surrounds) that UniFi
may never have resolved a friendly name for.

- Shared XML/SOAP parsing lives in `probes/sonos_api.py` — used by both the
  interactive probe and this collector, so there's exactly one place that
  knows the Sonos wire format.
- A Sonos serial is the player's MAC plus one trailing check character, but
  the local API prints it as `AA-BB-CC-DD-EE-FF:1` while the Sonos account
  page (and `account-import`'s `devices/accounts.json`) prints the same
  value as `AABBCCDDEEFF1`. `probes/sonos_api.py`'s `normalize_sonos_serial`
  canonicalizes to the latter (uppercase, no separators) wherever a serial is
  parsed, so `serial_number` is stored and displayed the same way regardless
  of which importer wrote it — and `account-import` doesn't re-plan the same
  "change" forever comparing two spellings of one serial. MAC addresses
  themselves are untouched by this — they stay colon-separated and
  lowercase, normalized by `discovery/normalize.py`'s `normalize_mac`.
- Seeds from Sonos IPs already in the inventory (any asset whose vendor or
  hostname mentions "sonos", or with a port-1400 `AssetService`) —
  deliberately never multicast/SSDP, since many segmented home networks
  (VLANs, guest networks, Wi-Fi client isolation) don't carry multicast
  traffic reliably (the same reason `probes/ssdp.py`'s M-SEARCH is unicast).
- Seeds are tried in order until one describes a *household*, not just
  until one answers: a portable player currently running as its own
  standalone Sonos system reports only itself, and stopping there once hid
  a real bonded group whose seed came later in the list. The first
  multi-player answer wins; if every seed reports a single player, that's
  used as-is (a genuine one-player household) — each seed is tried at most
  once, so there's no retry loop.
- A bonded satellite reports its *group's* room name in its own zone data,
  not its own identity, so this collector never invents a hostname for one
  — only a visible/primary player gets a hostname suggestion, and even then
  only on first discovery: it can never rename an asset that already has a
  name from any other source.
- `python -m discovery.cli sonos` (no `--apply`) prints what it found
  without writing; `--apply` reconciles it into the inventory the normal way
  (`reconcile_into_db`, tracked as a `DiscoveryRun` like every other
  collector) — `last_seen` moves and a genuinely new player becomes a new
  asset, unlike `account-import`, since this is live network evidence.
- The Discovery page's **Discover Sonos household** button is the same thing as
  `--apply` (it always writes), and Sonos is now part of that page's **Run
  everything** button too. Unlike the other collectors it never fails loudly:
  if no seed player answers, the run is still recorded as `completed` with a
  `status=no_seed_ips`/`status=no_response` summary rather than a `failed` row.

## Development and testing

If you're adapting this app rather than just running it, here's the tooling
that keeps changes honest. None of it runs on the deployed instance —
`scripts/redeploy.sh` installs only `requirements.txt`.

```bash
# dev tooling (pytest + ruff) on top of the runtime deps
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
```

`requirements.txt`/`requirements-dev.txt` are **generated** lockfiles (every
transitive dependency, with sha256 hashes) -- edit `requirements.in` /
`requirements-dev.in` and regenerate instead; see CONTRIBUTING.md's
"Dependencies". CI audits the lock against PyPI's advisory database on every
push and weekly.

### Test suite (`pytest`)

```bash
pytest                     # the whole suite
pytest tests/test_reconcile.py            # one module
pytest -k valuables -q                    # by keyword
```

`pytest.ini` sets `testpaths = tests`, so a bare `pytest` finds everything.
The important property for adapting the app: **the suite needs no Postgres,
no Docker, and no network.** `tests/conftest.py` builds a throwaway in-memory
SQLite database per test (`StaticPool` keeps the single connection alive) and
`chdir`s to the repo root so cwd-relative lookups behave as they do in the
running app. That's a deliberate boundary — the tests exercise application
logic (reconciliation, correlation scoring, discovery normalization, cascade
deletes), not Postgres-specific behaviour — which is what makes them fast and
runnable on a fresh checkout before you've stood up the database.

Patterns you'll reuse when adding tests:

- **`session` / `engine` fixtures** (`conftest.py`) hand you a ready in-memory
  DB; **`make_asset(session, **overrides)`** is the shortcut for seeding an
  `Asset` with sensible defaults.
- **No live collectors.** Discovery/network code is driven by monkeypatching
  the seams — e.g. `tests/test_cli.py` stubs the `run_*` wrappers on the
  `discovery.cli` namespace, and stubs `ssh`/subprocess rather than reaching
  out. Copy that approach for anything that would otherwise hit the LAN, UniFi,
  or an external API.
- **Route logic** is tested with FastAPI's `TestClient` (see
  `tests/test_dashboard_helpers.py`) — no server needs to be running.

### Linting (`ruff`)

```bash
ruff check .               # lint
ruff check . --fix         # auto-fixable issues
```

`ruff.toml` **pins** the rule set explicitly (don't rely on ruff's shifting
defaults) and documents *why* each family is on or off — notably the logging
rules (`G`/`LOG`/`T20`) that keep `print()` out of the runtime, and the
timezone rules deliberately left off because the schema is naive-UTC end to
end (see `app/clock.py`). Tests are exempt from the `print()` ban. Read the
comments there before adding or silencing a rule.

### The pre-push gate

`git push` is gated by a hook that runs **pytest, then ruff, then
`scripts/check-pii.sh`** — a change that breaks tests, fails lint, or would
leak PII/secrets is stopped before it leaves your machine. Hooks aren't
cloned by git, so install it once per clone:

```bash
cp scripts/hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

`scripts/preflight.sh` warns if it's missing or has drifted from the tracked
template. `check-pii.sh` also runs standalone (`scripts/check-pii.sh --full`
audits all history); it detects known PII (`.pii-denylist`), structural PII,
and secrets (vendor-prefixed keys, your own `.env` values, a committed
`.env`). See `scripts/env-structure.sh` for reconciling `.env` structure
across machines without moving values.

## Notes on scope

This deliberately scales down enterprise Asset and Vulnerability management
standards for a home network of a few dozen devices: discovery is
manual/on-demand rather than continuous, there's no dedicated
vulnerability-scanning appliance (Greenbone/OpenVAS), and CVE matching is
best-effort keyword matching on nmap's service-version banners rather than
authoritative CPE matching — treat findings as a prioritized starting point
to investigate, not a certified scan report. Enrichment only runs against
services where nmap reported *both* a product name and a version, not a
bare product name alone. If a finding looks implausible, check its
`evidence` field for the exact service match it was based on before
treating it as real.

## Contributing and security

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md), and read its
privacy section before your first commit. `README.md` is the only copy of this
document — the dashboard's **README** page renders it live from this file on
every request, so there's nothing separate to keep in sync. Contributor- and
deployment-oriented notes that don't belong in this end-user-facing README —
topology, the PII/secret model, git/GitHub conventions, log rotation — live in
[AGENTS.md](AGENTS.md); start there before a non-trivial change. To report a
security vulnerability, use GitHub's
[**Report a vulnerability**](https://github.com/rohitafish/home-asset-manager/security/policy)
button rather than opening a public issue — see [SECURITY.md](SECURITY.md) for what
to include.

For deeper documentation than this README carries — architecture, the security
model, a full configuration reference, and more — see the
[wiki](https://github.com/rohitafish/home-asset-manager/wiki).

## Licence

Released under the [MIT Licence](LICENSE).
