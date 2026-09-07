# Disaster recovery — the daemon host is gone

> **Read the first section only.** Everything below it is reference. If dellserver is down right
> now, start at *[Right now](#right-now)* and do not read ahead.
>
> Epic [#3117](https://github.com/JDonaghy/code-coordinator/issues/3117). Companion to
> [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md) (the unit inventory) and
> [`EPHEMERAL_WORKERS.md`](EPHEMERAL_WORKERS.md) (the Azure lane this borrows).
>
> **This file is on GitHub.** That is deliberate — you can read it from a phone while the fleet is
> dark. Do not move the recovery-critical facts into a place that dies with the daemon host.

## Right now

> **Use `~/.coord-venv/bin/coord`, not bare `coord`.** The shim on `$PATH` can resolve to a stale
> or partial install that reports `version 0+unknown` and **silently fails writes** rather than
> erroring. Every `coord` below means the venv binary. Same for `python3` — use
> `~/.coord-venv/bin/python3`.


**1. Confirm it is actually down.** Two daemons writing two restored copies is a split brain with
no reconciliation path — `coord serve` is the sole writer by design (#584). Never start a second
one on a hunch.

```bash
curl -sS --max-time 5 http://dellserver:7435/health ; echo " <- exit $?"
ssh -o ConnectTimeout=5 dellserver true ; echo "ssh exit $?"
```

If `/health` answers, the board is alive — you have a different problem. Go to
[`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md), not this file.

**2. Decide which failure you are in.**

| What happened | Where to go | Realistic time |
|---|---|---|
| Disk / OS / machine dead, **house is fine** | [Domain A](#domain-a--machine-loss) — promote precision | ~1 h |
| Fire, theft, flood, long power or ISP outage | [Domain B](#domain-b--site-loss) — Azure | ~2–4 h |
| Machine fine, database corrupt or a bad migration | [Local restore](#local-restore--corruption-not-loss) | ~10 min |

**3. Nothing is lost that you have not already lost.** Before you touch anything: the board's state
exists in **three** independent places. Losing the machine costs you at most the last hour.

| Copy | Where | Survives machine loss? | Survives site loss? |
|---|---|---|---|
| Live `~/.coord/coord.db` | dellserver, ~726 MB | ✗ | ✗ |
| Hourly `VACUUM INTO` snapshots | `/media/crucial/` — **USB SSD in the same chassis** | ✗ | ✗ |
| **Hourly restic snapshots** | **Azure Blob, East US** | **✓** | **✓** |

**4. Work already pushed to GitHub is safe.** Branches, PRs, issues and comments are untouched by
any of this. What lives only in `coord.db` is test verdicts, review verdicts, merge-queue ordering
and the audit log — `reconcile()` rebuilds in-flight assignment state and none of the rest.

---

## Domain A — machine loss

Promote **precision** (`100.116.209.7`). It is already on the tailnet, already has the repo
checkouts, and has `coord` installed at the same version.

> **⚠ Precision is not pre-staged.** As of 2026-09-05 it has no `restic`, no `~/.coord/backup.env`
> and no daemon unit files. Steps 1–2 below exist only because of that. Run
> [Pre-stage precision](#pre-stage-precision-do-this-before-you-need-it) on a calm day and this
> becomes a three-command recovery.

### 1. Get the credentials back

```bash
ssh precision
az login --use-device-code --tenant ccd69457-0339-4baa-8816-3007a603f8fc \
         --scope "https://management.core.windows.net//.default"
```

> **The az token on these boxes expires often** (90 days idle). `az account show` still answers
> from cache while every ARM call 401s. If you see `AADSTS700082`, run `az logout && az account
> clear` **first** — otherwise the stale token shadows the new one and the login appears to succeed
> while nothing works.

```bash
V=kv-coord-jd-prod
umask 077 && install -m 0600 /dev/null ~/.coord/backup.env
{
  echo "COORD_BACKUP_REPOSITORY=azure:coord-backups:/dellserver"
  echo "RESTIC_PASSWORD=$(az keyvault secret show --vault-name $V -n restic-repo-password --query value -o tsv)"
  echo "AZURE_ACCOUNT_NAME=$(az keyvault secret show --vault-name $V -n backup-storage-account --query value -o tsv)"
  echo "AZURE_ACCOUNT_KEY=$(az keyvault secret show --vault-name $V -n backup-storage-key --query value -o tsv)"
} > ~/.coord/backup.env
```

### 2. Install restic (no sudo needed)

```bash
VER=0.17.3; cd "$(mktemp -d)"
curl -fsSL -O https://github.com/restic/restic/releases/download/v${VER}/restic_${VER}_linux_amd64.bz2
curl -fsSL -O https://github.com/restic/restic/releases/download/v${VER}/SHA256SUMS
grep "restic_${VER}_linux_amd64.bz2" SHA256SUMS | sha256sum -c -   # must print OK
bunzip2 -f restic_${VER}_linux_amd64.bz2
install -m 0755 restic_${VER}_linux_amd64 ~/.local/bin/restic
```

`~/.local/bin` is already on the units' `PATH`, so nothing needs editing.

### 3. Restore the store

```bash
set -a; . ~/.coord/backup.env; set +a
coord backup list | head -5                      # newest first; note the top id
coord backup restore <snapshot-id> --into ~/.coord/coord.db
sqlite3 ~/.coord/coord.db 'PRAGMA integrity_check; SELECT COUNT(*) FROM assignments;'
```

`restore` refuses to overwrite an existing file without `--force`. On a machine that has never been
the daemon there is nothing to overwrite; if there is, look at it before forcing.

### 4. Get the config

`coordinator.yml` is **not** in the backup — it lives in the `coord-settings` repo.

```bash
cd ~/src/coord-settings && git pull --ff-only
ln -sfn ~/src/coord-settings/coord/coordinator.yml ~/.coord/coordinator.yml
```

If that repo is behind or dirty, **stop and look**. A fleet running on a stale config is a subtler
outage than a stopped one. (`coord doctor`'s `config_drift` check exists to stop this rotting —
#3120.)

### 5. Start serving

Install and enable the daemon-role units. The authoritative list is `ROLE_UNITS[ROLE_DAEMON]` in
[`coord/deploy_manifest.py`](../coord/deploy_manifest.py) — it is in code precisely so it does not
die with the machine:

```
coord-agent.service   coord-serve.service   coord-web.service
coord-web-dist-build.timer   coord-notify.timer   coord-drive-queue.timer
coord-release-propagate.timer   coord-db-backup.timer
coord-backup.timer   coord-dr-verify.timer
```

```bash
SRC=$(~/.coord-venv/bin/python3 -c "import coord,os;print(os.path.join(os.path.dirname(coord.__file__),'deploy'))")
cp $SRC/coord-{serve,web,agent}.service $SRC/coord-{notify,drive-queue,backup,dr-verify}.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now coord-serve.service coord-web.service
curl -sS http://localhost:7435/health
```

**Start `coord-serve` first and confirm it answers before enabling the timers.** A drive-queue tick
against a half-restored board will dispatch work you do not want.

> `coord-db-backup.timer` is the **local SSD** lane and belongs to dellserver's hardware. Do not
> enable it on precision unless you have attached the SSD.

### 6. Point the fleet at the new board — the part that is still manual

This is the pin that is not yet cut (rung D5 / #1824). Until it is, a promoted standby is
unreachable by everything until you do two edits:

- **Every client** pins the board in `~/.coord/client.toml`:
  ```
  board_service = "http://dellserver:7435"
  ```
  Change `dellserver` → `precision` on each machine you drive from.
- **The tailnet ACL** hardcodes `dst: ["dellserver:7435"]` with a `hosts:` entry mapping that name
  to a fixed IP — see [`scripts/azure-workers/tailnet-acl.hujson`](../scripts/azure-workers/tailnet-acl.hujson).
  Ephemeral workers cannot reach any other host. Edit it in the Tailscale admin console; its
  `tests:` block will reject an edit that locks you out.

**The fastest alternative:** rename the node in the Tailscale admin console so `precision` answers
to `dellserver`. Nothing else changes. Beware the collision trap documented in
[`EPHEMERAL_WORKERS.md`](EPHEMERAL_WORKERS.md) — if the dead node still holds the name, Tailscale
will hand you `dellserver-1` instead, silently.

### 7. Confirm you are actually recovered

Serving `/board` is not recovery. The credential half matters just as much — a board that cannot
merge a PR looks fine and is not:

```bash
coord status                       # machines + assignments
coord drive-queue status           # queue state
coord gates <some-open-issue>      # can it read the forge?
```

Then merge something small, or post a comment, and watch it land. That is the real test.

---

## Domain B — site loss

Same restore, different host. The Azure lane exists and is proven for **workers**
([`EPHEMERAL_WORKERS.md`](EPHEMERAL_WORKERS.md)) — golden image, Key Vault, cloud-init, tailnet
join, teardown.

**The server role is not built yet** (rung D4 / [#3130](https://github.com/JDonaghy/code-coordinator/issues/3130)).
Until it lands, Domain B is: bring up any Ubuntu VM, join it to the tailnet, install `coord` from
PyPI, then follow **Domain A steps 1–7 unchanged**. Slower, entirely manual, and it works — the
backup is in Azure already, so the data is next to wherever you stand the VM up.

If your laptop survived and the house did not, precision is gone too and this is the only path.

---

## Local restore — corruption, not loss

The machine is fine; the database is not. Use the **local** lane, which is far faster than pulling
from Azure:

```bash
systemctl --user stop coord-serve
ls -lh /media/crucial/coord-backups/ | tail -5
sqlite3 /media/crucial/coord-backups/coord.db.latest 'PRAGMA integrity_check; SELECT COUNT(*) FROM assignments;'
cp ~/.coord/coord.db ~/.coord/coord.db.before-restore-$(date +%s)   # keep the corrupt one
cp /media/crucial/coord-backups/coord.db.latest ~/.coord/coord.db
systemctl --user start coord-serve
```

Snapshots named `*.REJECTED` failed verification and are kept deliberately — never restore one.

---

## Where everything is

| Thing | Value |
|---|---|
| Azure subscription | `22ad856b-f038-488d-b942-4d329976e2e4` |
| Tenant | `ccd69457-0339-4baa-8816-3007a603f8fc` |
| Resource group / region | `rg-coord-shared` / `eastus` |
| Storage account | `stcoordjdbackup` (Standard_LRS, **Hot**) |
| Container | `coord-backups` |
| restic repository | `azure:coord-backups:/dellserver` |
| Key Vault | `kv-coord-jd-prod` |
| Secrets | `restic-repo-password`, `backup-storage-key`, `backup-storage-account` |
| Config repo | `git@github.com:JDonaghy/coord-settings.git` |
| Local snapshots | `/media/crucial/coord-backups/` (dellserver only) |
| Machines | dellserver `100.97.107.88` · precision `100.116.209.7` · elitebook `100.85.221.28` |

**Hot tier, not Cool, on purpose.** Retention prunes hourly snapshots at 48 h and Cool carries a
30-day early-deletion penalty — it would bill pruned chunks as if kept a month. Cool would cost
*more* for this policy.

### The one secret that must live outside Azure

`restic-repo-password` decrypts every backup. It is in Key Vault — off the machine it protects,
which is the point — but if you ever lose access to the *subscription*, the backups become
undecryptable ciphertext.

```bash
az keyvault secret show --vault-name kv-coord-jd-prod -n restic-repo-password --query value -o tsv
```

**Keep a copy somewhere that is neither Azure nor the house.** A password manager is fine. This is
the single worst thing to lose in the whole system.

---

## What is automated, and what is not

Honest state as of 2026-09-05 — do not assume more than this at 3am.

| | State |
|---|---|
| Hourly off-site backup, verified before it counts | ✅ live ([#3118](https://github.com/JDonaghy/code-coordinator/issues/3118)) |
| Continuous proof the backup restores | ⚠️ live but **red** — see below ([#3119](https://github.com/JDonaghy/code-coordinator/issues/3119)) |
| Config drift detection | ✅ live ([#3120](https://github.com/JDonaghy/code-coordinator/issues/3120)) |
| `coord doctor` notices the DR lane is missing on a daemon host | ❌ [#3128](https://github.com/JDonaghy/code-coordinator/issues/3128) |
| One-command promote (`coord dr promote`) | ❌ [#3129](https://github.com/JDonaghy/code-coordinator/issues/3129) — Domain A is manual |
| Azure server role (`dr-up.sh`) | ❌ [#3130](https://github.com/JDonaghy/code-coordinator/issues/3130) — Domain B is manual |
| Board endpoint survives a move | ❌ [#1824](https://github.com/JDonaghy/code-coordinator/issues/1824) — step 6 is manual |
| Rehearsed drill with measured RTO | ❌ rung D6 — **these times are estimates, not measurements** |

> **`coord dr verify` is currently red on every run, and it is a false alarm**
> ([#3135](https://github.com/JDonaghy/code-coordinator/issues/3135)). Its parity step compares the
> restored store's raw row count (4948) against what `/board` serves (369) — `/board` filters, so
> it can never match. Every other step passes. Until #3135 lands, read the detail rather than the
> verdict, or run `coord dr verify --no-parity`, which is strictly weaker but honest.

---

## Proving it works before you need it

```bash
coord dr status                    # is the last verify recent enough to believe?
coord backup list | head -3        # is there a snapshot from the last hour?
cat ~/.coord/last_verify.json      # read `steps`, not just `outcome` (see #3135)
```

`last_verify.json` carries `restore_seconds` — **11.6 s** as measured on 2026-09-05. That is the
real input to the Domain-A recovery time, and it is small. The hour in the table above is
dominated by the manual steps, not by moving data.

### Pre-stage precision (do this before you need it)

Steps 1–2 of Domain A are pure friction: installing software and doing an interactive cloud login
during an outage. Doing them now removes both, and costs nothing while idle:

1. Install `restic` on precision (Domain A step 2, verbatim).
2. Write `~/.coord/backup.env` on precision (Domain A step 1, verbatim).
3. Copy the daemon unit files into `~/.config/systemd/user/` **without enabling them**.

The tradeoff is deliberate and worth stating: this puts a storage-account key on a second machine.
That widens the blast radius of the credential in exchange for removing an interactive Azure login
from the critical path of an outage. Rotate the key (`az storage account keys renew`) if precision
is ever compromised — and update both the Key Vault secret and both `backup.env` files when you do.

### A drill you can run in ten minutes, safely

```bash
coord dr verify --scratch /tmp        # restores off-site data, boots a throwaway daemon, tears down
```

This touches nothing live. If it passes every step but parity, your backup is good and restorable
today.
