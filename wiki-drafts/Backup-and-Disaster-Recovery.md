# Backup & Disaster Recovery

The database is the only thing in this app that isn't reconstructable from
git — every asset, note, and investigation you've recorded lives in one
Postgres volume on one Mac. This page is the *design*: what's protected, why
it's shaped this way, and exactly what AWS is doing with your data. For the
step-by-step setup commands and the restore runbook, see the README's
["Off-site database backups"](https://github.com/rohitafish/home-asset-manager#off-site-database-backups)
section — this page won't duplicate those verbatim.

## What's protected, and the trade-off accepted

Time Machine backing up the host does **not** protect the database: Postgres
runs inside a Colima VM, so Time Machine only ever sees one large opaque VM
disk image, not the database files inside it — a snapshot taken mid-write has
no crash-consistency guarantee. `scripts/backup-db.sh` exists specifically to
get a real, restorable copy off that Mac and into S3.

This is deliberately a *durability* answer, not an *availability* one: after
losing the host the app is down until you rebuild it — it has to run on
the LAN for nmap to work at all, so there was never a version of this that
kept the dashboard reachable through a lost host. What this protects is the
data, not uptime.

**Recovery point: up to 24 hours of edits can be lost.** The backup job ticks
three times a day (below), but only the *first successful* tick each
calendar day does real work — it's idempotent and skips once that day's
off-site copy exists, so the extra ticks are catch-up for a missed run, not
additional recovery points. This is not point-in-time recovery, and it's a
reasonable trade for how this app is actually used (a home inventory edited
in short bursts) — but worth knowing plainly rather than discovering it
during a restore.

**What it costs:** at the database's current size (well under 9 MB), thirty
daily dumps plus six monthly ones add up to well under $0.01/month on S3.

## Storage

| | |
|---|---|
| **Service** | Amazon S3, one bucket, versioning enabled |
| **Storage class** | Standard — deliberately, for every object |
| **Object keys** | `daily/assetmgt-YYYY-MM-DD.dump`, `monthly/assetmgt-YYYY-MM-DD.dump` |
| **Public access** | Fully blocked (`put-public-access-block`, all four settings on) |

Glacier/IA is deliberately skipped: at the database's size (1-2 MB per dump),
a cheaper storage class costs *more* in practice once per-object minimums and
retrieval fees are factored in. Standard is both simpler and cheaper here.

Each dump is a single `pg_dump -Fc` (custom format — compressed, and a single
transactionally-consistent snapshot) taken from *inside* the Postgres
container, never per-table: there's no `ON DELETE CASCADE` anywhere in this
schema, so foreign-key integrity depends on the dump being one atomic
snapshot. Before it's promoted to a real filename or uploaded, `pg_restore
-l` on the raw dump proves the archive's table of contents actually parses —
a dump nobody can read is worse than no dump, because it looks like success.

## Encryption

- **At rest:** SSE-S3 (AES256) — set as the bucket's *default* encryption
  (`put-bucket-encryption`) and *also* passed explicitly on every upload
  (`--server-side-encryption AES256`), so it doesn't depend on the bucket
  default alone. No KMS, no client-side encryption — AES256 is sufficient
  for a personal inventory dump and keeps the setup to one `aws s3api` call.
- **In transit:** TLS, implicitly — the AWS CLI talks to the S3 API over
  HTTPS only; there's no plain-HTTP path to disable.

## Backup schedule

A `launchd` LaunchAgent (`com.assetmgt.backup`, installed from
`scripts/com.assetmgt.backup.plist`) ticks **three times a day, local time**:

| Tick | Purpose |
|---|---|
| 03:15 | The preferred nightly run |
| 12:15 | Catch-up, if 03:15 was missed |
| 19:15 | Catch-up, if 12:15 was also missed |

The extra ticks exist for one specific, observed failure mode: the Mac
powered off or FileVault-locked (unattended reboot with no one logged in)
straight through 03:15. `StartCalendarInterval` — not `StartInterval` — is
used specifically because it re-runs a tick missed during *sleep* at the next
wake; the daytime ticks cover the case sleep-catchup can't (power loss, not
just sleep), and conveniently also sidestep a boot-time Docker-not-ready race.
(The original Aug 2026 outage that motivated this turned out to be a
sub-second power-transfer cutover rather than a real extended outage — see
README's "Power outages and unattended restart". A UPS would close that hole,
but pricing one found every suitable model out of stock or badly marked up,
so this deployment still reboots on a cutover and waits at the FileVault
pre-boot screen for someone to walk over and unlock it — exactly the
"unattended reboot with no one logged in" case these ticks exist for, not a
mitigated one. These ticks are the correct mitigation for that case, and for
a genuine unattended power-off, either way.)

The job is **idempotent**: it `head-object`s that day's `daily/` key first
and exits immediately if it's already there. That's what makes three ticks a
day safe rather than wasteful — whichever tick first finds the Mac up and
Colima ready does the day's backup, and the rest no-op. It also has to be
idempotent for a structural reason, not just efficiency: the daily object is
under Object Lock (below), so a same-key re-upload would be *rejected*, not
merely redundant.

There is deliberately **no `RunAtLoad`** — on a reboot there's no ordering
guarantee that Colima is up before LaunchAgents run, so `RunAtLoad` would
produce spurious "cannot connect to the Docker daemon" log entries after
every restart, which is exactly the kind of noise that makes an error log
stop being trusted. The daytime ticks are the catch-up mechanism instead.

## Reliability: what happens when the upload fails

Every production failure of this job so far has been the same shape: the
upload dying mid-body with a closed connection, then the identical upload
succeeding by hand seconds later — a transient worth retrying, not an outage
worth escalating on the first failure.

- **Up to 3 attempts** (`BACKUP_UPLOAD_ATTEMPTS`, default 3 — an advanced
  override, and like `BACKUP_KEEP_LOCAL` it's read from the *process*
  environment, so setting it in `.env` is a silent no-op; override it in the
  plist instead).
- **Backoff: 30s, then 120s** (quartic — each delay is 4× the last).
- Before each retry, a `head-object` check — not belt-and-braces, but
  required for correctness. "Connection closed before the response" means
  the outcome is genuinely unknown: S3 may have committed the object and
  only lost the reply. Since the key is under COMPLIANCE Object Lock, a
  blind same-key retry after a false failure would be *rejected*, turning an
  upload that actually succeeded into a hard failure. Checking first turns
  that case into the success it actually was.
- If every attempt is exhausted, a diagnostic probe runs once (dump size,
  reachability of the exact S3 endpoint, and general internet egress) so the
  failure log says *why*, not just *that* it failed.

## Retention and archiving

Two mechanisms work together, not against each other:

**S3 Object Lock (Compliance mode)** is what actually stops early deletion —
enforced by S3 itself, so nothing (this credential, a compromised host, the
AWS account owner, even AWS support) can delete or overwrite a locked object
before its retention date. It's set per-object at upload time
(`--object-lock-mode COMPLIANCE --object-lock-retain-until-date ...`), and
it's a genuine one-way door: Object Lock can only be *enabled* at bucket
creation, and locking an individual object cannot be undone or shortened for
the length of its retention.

**S3 Lifecycle** is what actually *reclaims* storage once Object Lock's
protection has lapsed — it doesn't fight Object Lock, it picks up after it:

| Prefix | Object Lock retention | Lifecycle expiration |
|---|---|---|
| `daily/` | 30 days | 30 days |
| `monthly/` | 186 days | 186 days |

On a versioned bucket, `Expiration.Days` alone never deletes anything — it
writes a delete marker and makes the old copy *noncurrent*; a
`NoncurrentVersionExpiration` rule (7 days here) is what actually reclaims
that noncurrent copy. An `AbortIncompleteMultipartUpload` rule (7 days) and a
"clean up expired delete markers" rule round out the policy so nothing
accumulates that neither mechanism was meant to keep.

**`monthly/` is the archive tier.** It's a second, independent upload of the
same dump (not an S3-side copy of the daily object), created on an
*ensure*-based rule rather than "only on the 1st": the first successful daily
of any calendar month with no monthly copy yet creates one, and every day
after that finds it already present and skips. That specifically survives an
outage spanning the 1st of the month, which "only on the 1st" would silently
lose.

Nothing is kept forever, and there's no infinite-retention tier — after the
Lifecycle window, an object is gone. The local copies kept in `backups/` on
the host (the 7 most recent) are a convenience fast-path only, not the real
backup history; a restore should always pull from S3, not from the local
directory, since that's the only copy proven to have left the host.

## Monitoring for silent failure

Because retention is age-based, **a silently failing job doesn't go stale —
it loses the entire backup history within that window** once enough days
pass without a successful upload. Three things exist specifically to catch
that before it becomes invisible:

1. **`backups/last-success`** — a plain UTC timestamp file, refreshed both at
   the end of a successful upload *and* on the idempotent skip path (a day
   whose off-site copy already exists still counts as "there is a current
   backup," even if this particular run didn't do the uploading — without
   that second refresh point, a day whose copy arrived by another route
   left the marker frozen and downstream checks reporting a gap that didn't
   exist).
2. **`/health`**'s `last_backup`/`backup_age_hours`/`backup_stale` fields —
   informational only; a stale backup never flips `/health`'s status code,
   so it can't make an unrelated deploy look broken, but `backup_stale` is
   there for anything that wants to check.
3. **The Summary page's "Since last DB backup" card**, which turns to a
   warning state once `backup_stale` is true.

`backup_stale` trips past **26 hours**, derived from the actual tick
schedule rather than a round number: normal operation never lets the marker
go older than the ~8-9 hour gap between ticks, so the threshold is really
set by how long the host can *legitimately* go without a successful tick —
the documented powered-off/FileVault case can swallow a whole day's ticks,
which is roughly 24h from the previous evening's run, plus slack. (It used
to be 36 hours, on the older assumption of "one nightly run plus a day's
grace" — before the daytime catch-up ticks existed and before the marker was
refreshed on the skip path. That number let three consecutive real failed
ticks sit at just under 31 hours without ever tripping the alert.)

On a plain dev checkout, all of this permanently reads "never"/stale — that's
correct, not a bug; only the deployed instance runs the nightly job.

## What isn't backed up

**`.env` itself.** It holds `APP_ADMIN_PASSWORD`, the UniFi/NVD/AI API keys,
and the `BACKUP_AWS_*` credentials that make this whole system work — and
it's gitignored *and* excluded from the deploy sync, so after losing the host
it exists nowhere. A restore brings back all the *data*, but the app won't
start (and the next backup can't run) without `.env` recreated from
somewhere else. There's no code fix for this: copy its contents into a
password manager, and keep that copy current.

## See also

- [Configuration Reference](Configuration-Reference) — every `BACKUP_*`
  environment variable, its default, and whether it's required.
- [Troubleshooting / FAQ](Troubleshooting-FAQ) — what a stale `/health`
  response actually means and how to check on it.
- The README's ["Off-site database backups"](https://github.com/rohitafish/home-asset-manager#off-site-database-backups)
  section — the one-time AWS setup commands, installing the LaunchAgent, and
  the full restore runbook (including the "verify it actually works" drill).
