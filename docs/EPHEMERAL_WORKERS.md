# Ephemeral Azure workers

On-demand worker VMs, spun up for an epic and destroyed when it ends. A worker
is a normal fleet member — it appears in `coord status`, takes dispatches, and
runs `claude -p` exactly like precision or dellserver. The difference is that it
exists for hours, costs by the hour, and holds no state worth keeping.

Scripts: [`scripts/azure-workers/`](../scripts/azure-workers/).
Bicep module: `modules/coord-worker-vm/` in the **easy-azure** repo.

## Why this shape

**The worker needs no network access to anything of yours.** It reaches the
board daemon over Tailscale, never over a VNet. So each epic gets a
self-contained VNet, NSG and NAT Gateway, created and destroyed with it. Nothing
to wire up, no shared subnet to mutate, and the NAT Gateway is an hourly cost
that dies with the epic instead of a standing ~$32/mo one.

**Workers run on the metered API, not your subscription.** The worker's
`ANTHROPIC_API_KEY` comes from Key Vault, so fleet usage never competes with
your Max plan's rate limits. This is what makes parallel epics possible at all —
the constraint moves from "5-hour rolling window" to "monthly spend limit",
which is a dial you control.

**The tailnet ACL is the security boundary, not a hardening extra.** The agent's
HTTP API (`coord/agent_app.py`) has **no authentication** — anything on the
tailnet can `POST /assign` to any agent. The ACL is what stops a compromised
worker reaching your machines.

**The toolchain list is shared with the bare-metal lane, not copied from it
(#3139).** [`scripts/lib/provision-core.sh`](../scripts/lib/provision-core.sh)
owns *what a fleet machine needs* — the `gh` version floor, the Node major, the
pinned opencode version, the system-wide Rust location, the base package list,
the repo clone list and the prereq verification that mirrors `coord/prereqs.py`.
Both `scripts/azure-workers/provision-worker.sh` (this lane) and
[`scripts/provision-machine.sh`](../scripts/provision-machine.sh) (bare metal,
#3138) source it; each keeps only the constraints of its own substrate:

| | Azure image | bare metal |
|---|---|---|
| user | a dedicated `coord` user — `waagent -deprovision+user` deletes the *provisioning* user and its home | the operator's own account |
| identity | **none by design** — credentials arrive per-boot from Key Vault | the whole point; interactive, front-loaded |
| tailscale | installed, never `up` | `tailscale up`, interactive |
| privilege | root throughout | `sudo` for packages only |
| rust | system-wide at `/opt/rust` (the agent unit's PATH has no `~/.cargo/bin` — #1671) | per-user rustup; that lane's unit PATH *does* |
| finish | `scrub-and-generalize.sh` | `coord machine doctor` |

Every pinned value below has already drifted once and cost something, so the
rule is enforced rather than documented: `tests/test_provision_core.py` greps
both lanes and **fails on a second literal**, and separately fails when the
shell-side `gh` floor and `coord.github_ops.GH_PR_CHECKS_JSON_MIN_VERSION`
disagree. `build-worker-image.sh` copies the core to the builder alongside
`provision-worker.sh` and the script **hard-fails** if it is absent — never
falls back to inlined defaults, because an image built from half a toolchain
list is exactly the silent drift the core exists to prevent.

## One-time setup

1. **Tailnet ACL** — apply [`scripts/azure-workers/tailnet-acl.hujson`](../scripts/azure-workers/tailnet-acl.hujson).
   Defines `tag:coord-worker` and restricts it to `dellserver:7435` only.
   Defining `acls` flips the tailnet from default-allow to **default-deny**;
   rule 1 preserves your existing device-to-device access. Do not drop it.
2. **Tailscale OAuth client** — `auth_keys` **Write** scope, tagged
   `tag:coord-worker`. The ACL must be applied first or the tag does not exist.
3. **Anthropic API key** in a dedicated workspace with its own spend limit.
4. **GitHub fine-grained PAT** — Contents RW, Pull requests RW, Issues RW,
   Actions R, Commit statuses R. Verify it with
   [`verify-github-token.sh`](../scripts/azure-workers/verify-github-token.sh)
   before it goes anywhere near Key Vault.
5. **`bootstrap-shared.sh`** — Key Vault, the user-assigned identity that reads
   it, the private DNS zone, and the secrets (`anthropic-api-key`,
   `github-token`, `tailscale-oauth-secret`, and — if this epic will dispatch
   **opencode** workers — `opencode-api-key`). Prints resource IDs for
   `~/.coord/epic.env`.
6. **Markers in `coordinator.yml`** on the daemon host, at the end of the
   `machines:` list:
   ```yaml
     # >>> epic-machines (managed by epic-up.sh) >>>
     # <<< epic-machines <<<
   ```
7. **`build-worker-image.sh`** — the golden image (~30 min).

`preflight.sh` checks all of the above plus tooling, RBAC, provider
registration and quota. Run it before anything that creates resources.

## Daily use

```bash
./epic-up.sh   --epic 1537 --repos claude-coordinator,quadraui [--paused]
./epic-down.sh --epic 1537 [--force]
```

`--epic` is only a label — it names the resource group and the machine. It does
**not** scope what the worker picks up: once registered, the machine is a full
pool member and `coord plan` can route any work in its repos to it. Use
`--paused` for a first boot, then `coord unpause` deliberately.

`epic-down` pauses, waits for `active=0` on the agent's `/health`, checks for
live interactive tmux sessions, deregisters, and deletes the resource group.
`--force` skips the drain — unpushed work on the VM is lost.

## Gotchas

These each cost real time or money and are invisible from the code.

Two of them are now **named tests** in `tests/test_provision_core.py`, driven to
their *failing* verdict against a stub `az`/`gh` so they cannot quietly be
weakened into warnings during a refactor:
`test_the_build_hard_fails_on_an_image_definition_without_nvme` (the
`DiskControllerTypes` declaration, both at create time and when reusing an old
definition) and `test_the_gh_floor_hard_fails_on_an_old_or_silent_gh`. The
"build as a non-provisioning user" and "zero identity" gotchas are pinned by
`test_the_image_lane_still_builds_as_a_dedicated_non_provisioning_user` and
`test_the_image_lane_still_installs_zero_identity`. The remaining ones —
Key Vault DNS at boot, `XDG_RUNTIME_DIR`, the user-unit/system-unit ordering —
live outside this repo's reach (cloud-init and `easy-azure`) and stay prose.

**A stale tailnet node silently costs you 15 minutes.** If a node already holds
the hostname, Tailscale names the new VM `<name>-1` and `epic-up` polls the
stale one until timeout. `ephemeral=true` removal only fires on a clean
tailscaled shutdown — `az group delete` yanks the VM out from under it, so nodes
often linger as `offline` for a few minutes. `epic-up` now fails fast on a
collision, but check the admin console if teardown was abrupt.

**Gallery image features are immutable.** `DiskControllerTypes` must be declared
at definition-create time. Omit NVMe and you get a SCSI-only image that v6/v7
SKUs refuse to boot, with `"cannot boot with OS image or disk"` at *deploy*
time — half an hour after the build reported success. It cannot be added later,
and a version cannot be copied into a definition whose features differ. The only
fix is deleting the definition and rebuilding. `build-worker-image.sh` now
declares it and hard-fails if it is reusing a definition that lacks it.

**Key Vault DNS is not ready at boot.** The vault resolves through the linked
private DNS zone, which is not necessarily serving when cloud-init runs — the
lookup returned NXDOMAIN at ~66s into boot and succeeded minutes later. Without
a wait, the whole boot fails with a bare `Could not resolve host` and every
downstream step (including the tailnet join) fails as a consequence.
`coord-fetch-secrets` waits for resolution and retries each secret.

**`systemctl --user` needs `XDG_RUNTIME_DIR` non-interactively.** `install-agent.sh`
calls it, and without the variable it fails with `Failed to connect to bus: No
medium found` — the same #404 trap documented in
[`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md). Lingering is enabled in the image
so `/run/user/<uid>` exists before login.

**A user unit cannot depend on a system unit.** `coord-agent` is a user unit;
`coord-secrets` is a system unit. `Requires=coord-secrets.service` fails with
`Unit not found` and the agent never starts. Ordering comes from cloud-init
sequencing instead.

**Do not build the image as the provisioning user.** `waagent -deprovision+user`
deletes that user *and its home directory* — which is where `~/.coord-venv`,
`~/src` and `~/.npm` live. Everything builds as a separately-created `coord`
user whose home the deprovision leaves alone.

**Cargo must be system-wide.** `install-agent.sh` pins the agent unit's `PATH`
to `$VENV/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin` — no
`~/.cargo/bin`. Workers inherit that, so a per-user rustup install leaves
`cargo` invisible to every dispatched task (this is #1671's root cause). The
image installs to `/opt/rust` and symlinks into `/usr/local/bin`.

**opencode needs a credential wired to a fourth Key Vault secret — and that
wiring is not finished (#1777).** The image installs opencode
(`scripts/azure-workers/provision-worker.sh`, pinned to the fleet's 1.18.11)
and `bootstrap-shared.sh` now prompts for a fourth secret, `opencode-api-key`.
Verified against the real binary (`docs/OPENCODE_VERIFICATION.md`): opencode
authenticates non-interactively from an `OPENCODE_API_KEY` environment
variable alone — no `auth.json`, no login step, nothing baked into the image.
**But nothing on the worker side exports that env var yet.** `coord-secrets`
(the systemd unit cloud-init installs at boot, sourced from the
**easy-azure** repo's `modules/coord-worker-vm/main.bicep`) only fetches and
exports the original three secrets today. Extending it to also export
`OPENCODE_API_KEY` for `opencode-api-key` is a Bicep/cloud-init change in
easy-azure, out of this repo's reach — until that lands, `opencode-api-key`
sits in Key Vault unused and a worker dispatched with `--provider opencode`
will fail at session start exactly like the "silent" failure mode this issue
set out to avoid. Do the easy-azure change before relying on an opencode
worker for real (i.e. before #1708).

**`~/.opencode/bin` is not on the agent's PATH by default — provisioning
symlinks around it instead of patching the unit.** opencode's official
installer always drops the binary at `~/.opencode/bin/opencode`; there is no
flag or env var that redirects it. That is why the **standing** fleet needed a
`20-opencode-path.conf` systemd drop-in on `coord-agent` — nothing else put
that directory on the unit's PATH. `provision-worker.sh` sidesteps the same
problem on ephemeral images by symlinking the binary into `~/.local/bin`
instead, which is already on `coord-agent`'s PATH (the Claude Code CLI install
already depends on landing there). No drop-in needed for these images as a
result — but if a future change moves the Claude CLI off `~/.local/bin`, this
symlink stops being sufficient too; re-check both together.

**`apt install gh` produces a broken image.** Ubuntu's `gh` is far below the
`GH_PR_CHECKS_JSON_MIN_VERSION` floor (2.86.0), and the CI merge gate throws
`GhTooOldForJsonChecks` below it. The image uses the official repo and fails the
build if the floor is not met.

**`coord config --config <file>` is not a validator on a thin client.** It
re-fetches from the daemon and reports *that* config — it "succeeds" against a
deliberately invalid file. `epic-up`/`epic-down` validate by calling
`coord.config.load()` directly through the interpreter that runs `coord` on the
daemon host. This matters because a malformed config is **swallowed** by the
daemon (last-good kept, warning logged), so an unvalidated bad edit silently
no-ops rather than failing loudly.

**Quota bites in two places.** The per-family limit gates the SKU; the regional
`cores` total gates everything at once. A fresh subscription commonly has
`cores=14` — one 8-vCPU VM, and no image rebuild while a worker runs.
`standardDASv5Family` was `0/0` on this subscription; `Standard_D8as_v7` is the
same 8 vCPU / 32 GiB in a family that has cores, at the cost of no temp disk
(cloud-init probes for one and falls back to the OS disk).

## Cost

Roughly **$0.40/hr** all-in per worker (D8as_v7 PAYG + 256 GiB Premium OS disk +
NAT Gateway), so ~$10 for a day-long epic. The golden image costs ~$5/mo in
gallery storage; Key Vault, the managed identity and the DNS zone are pennies.

The binding constraint is usually not infrastructure but the Anthropic workspace
spend limit — run `coord usage` after the first epic for real numbers.

## What is deliberately not there

- **No board-daemon token on the worker.** The daemon calls *out* to the agent
  on 7433; the agent never calls back. So `coord report-result` from a remote
  interactive session does not work on these VMs — headless dispatch only.
  Adding the token would make `dellserver:7435` an escalation path.
- **No `Workflows: Write`** on the PAT. A worker touching
  `.github/workflows/**` gets a push rejection, by design.
- **No inbound WireGuard** (`allowDirectWireGuard: false`). Tailscale relays via
  DERP, so the VM is fully inbound-closed at a little latency cost.
