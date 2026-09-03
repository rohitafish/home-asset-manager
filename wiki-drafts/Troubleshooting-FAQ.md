# Troubleshooting / FAQ

Start here for anything that isn't working: **`./scripts/preflight.sh`** is a
one-command sanity check — Mac toolchain, Docker/Colima, Python version,
`.env` configuration, Postgres/migration state, and the LaunchAgents. It
never prints a secret's value, only whether one is set. Run it after setup,
or any time something's wrong, to narrow down where.

This page will grow with real issues over time — it's seeded with the ones
anticipated from the app's own error messages and design.

---

**The app won't start at all.**
Check `APP_ADMIN_PASSWORD` in `.env` — the app deliberately refuses to boot
if it's unset or still the template's `change-me` placeholder, rather than
silently serving an unauthenticated dashboard. Set a real password and
restart.

---

**The dashboard is unreachable, and `curl` on `/health` fails (e.g. exit
code 7, connection refused).**
Almost always Colima (and therefore Postgres) is down. `colima start`, wait
for it, confirm Postgres is reachable, then restart the app service. A clean
shutdown (`colima stop`) before any reboot/OS update/machine move avoids this
— see the README's Colima section for why an unclean stop can leave the VM
refusing to restart.

---

**`/health` returns ok, but `backup_stale` is true.**
This is informational only — it never affects `/health`'s status code or
`redeploy.sh`'s deploy gate, so it won't block a deploy or make an unrelated
problem look broken. It just means the last off-site backup is older than
26 hours; check `scripts/backup-db.sh`'s logs and your `BACKUP_*` config (see
[Configuration Reference](Configuration-Reference#off-site-database-backups-optional)),
or [Backup & Disaster Recovery](Backup-and-Disaster-Recovery) for how the
staleness threshold and the success marker actually work.

---

**UniFi discovery fails with a TLS/certificate error.**
`UNIFI_VERIFY_TLS` defaults to on, and a self-signed UDM/controller will fail
verification. If you've confirmed it's your own controller on your own LAN,
set `UNIFI_VERIFY_TLS=false` — a deliberate, explicit opt-out, not a default
(see [Security Model](Security-Model)).

---

**nmap discovery finds nothing, or only partial results.**
Check `SCAN_SUBNETS` actually covers the network you expect. A device on a
different VLAN than the machine running the scan may not ARP-resolve a MAC
even if it's pinged successfully. The web UI only runs the more limited `-sT`
connect scan; a full `-sS` SYN scan is CLI-only from a terminal (`python -m
discovery.cli nmap --sudo`, which prompts for your password) — see the
README's "Nmap privileges" for why there is no passwordless rule for it.

---

**The chat panel says "not configured."**
Expected, and not a bug — neither `ANTHROPIC_API_KEY` nor `OPENROUTER_API_KEY`
is set. The assistant is entirely optional; everything else in the app works
without it.

---

**The assistant returns a specific error message — what does it mean?**

| Message | Meaning |
|---|---|
| *"The API rejected the key — check `..._API_KEY` in .env."* | The configured key is invalid/revoked. |
| *"Rate limited by the API. Try again shortly."* | Exactly what it says — wait and retry. |
| *"The API didn't respond in time."* | A slow request; try again, or ask something shorter. |
| *"Couldn't reach the API — check this machine's internet connection."* | Network-level failure reaching the API. |
| *"Model 'X' was not found — check `ANTHROPIC_MODEL` in .env."* | Usually a stale or typo'd model override. |
| *"Claude declined to respond to this request."* | A refusal, not an error — rephrase the request. |
| *"Stopped after several tool calls without a final answer."* | Hit the per-turn tool-call limit; ask a follow-up to continue. |

Whatever's already in the chat stays saved regardless — a failed turn never
corrupts the transcript.

---

**Two rows exist for what's obviously one physical device (e.g. a laptop's
wired and wireless NIC).**
That's what `/assets/investigate` is for — it scores and links same-device
candidates **non-destructively**, keeping both rows and histories. That's
different from the **Duplicates** page, which is for genuinely duplicate
rows and deletes the loser on merge. Don't merge two NICs that are correctly
tracked as one device's two identities — link them instead. See
[Architecture Deep-Dive](Architecture-Deep-Dive#duplicate-detection-two-different-tools-on-purpose).

---

**A discovery run keeps overwriting a hostname/field I set manually.**
Lock it. Any asset's hostname can be manually locked from its edit page;
locked fields are never touched by discovery regardless of source. The
broader identity fields (serial/model number/model identifier) have their
own `identity_locked` flag.

---

**A migration fails during `redeploy.sh`.**
Confirm Postgres is actually up and reachable first (see the Colima entry
above) — most migration failures are really "couldn't connect," not a schema
problem. If it's a genuine migration error, check `alembic current` against
`alembic history` to see where the DB actually is before retrying.

---

**Still stuck?** Open an issue with what `./scripts/preflight.sh` reported.
For a security-relevant problem, see
[SECURITY.md](https://github.com/rohitafish/home-asset-manager/blob/main/SECURITY.md)
instead of a public issue.
