# Mac mini — hardware sizing + provisioning runbook

> **Status:** decision + runbook (2026-07-25); **provisioned and verified 2026-09-06**. Sizing analysis for adding a Mac mini (M4) to the
> fleet as the macOS build/test/attended-session machine. The *port* itself is scoped in
> [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) (milestone #39 / epic #1160) — this doc is only about the
> **hardware**: what to buy, why not to rent, and how to provision it once it lands.

## The decision

**Buy an M4 Mac mini with 512GB of storage. Do not rent.**

- **16GB RAM is enough** for this workload at `concurrency: 1` (2 at a stretch), with
  `CARGO_BUILD_JOBS` capped. 24GB is the safe upgrade if the budget stretches — it is the *only*
  irreversible decision on the box.
- **256GB storage is not enough.** This is the binding constraint, not RAM (evidence below).
- **Renting breaks even in ~4–6 months**, and the mac is a permanent fleet member, not a
  port-sprint rental.

## Why 16GB is enough

The mac is a fleet agent that runs `claude -p` workers doing `cargo build` + `cargo test` on
quadraui, vimcode, and coord-tui, plus human-attended interactive sessions for macOS GUI polish.

Peak footprint for **one** worker mid-build:

| Component | Peak |
|---|---|
| macOS + logged-in GUI session (WindowServer, Spotlight, …) | ~4–5 GB |
| one `claude -p` worker (node; the model is remote, nothing local) | ~0.5–1.5 GB |
| `cargo build -j10` on quadraui/vimcode-sized crate graphs | ~4–8 GB |
| **total** | **~10–14 GB** |

That fits in 16GB with little headroom. **Two concurrent Rust-building workers will swap** — and
swap on a soldered SSD is a wear problem over years of builds, not just a speed problem. Unified
memory is also shared with the GPU, which matters during live GTK4 smoke work.

For reference, the base M4 is 10 cores (4P/6E) against elitebook's 8 — *more* cores and much faster
P-cores, but half the RAM (16 vs 31GB). Compile wall-clock should improve; concurrency must not.

**Mitigations (apply both):**

- `concurrency: 1` for the machine in `coordinator.yml` (2 only after watching real memory pressure).
- `CARGO_BUILD_JOBS=6` so a parallel-codegen spike can't blow past the ceiling.

## Why 256GB is not enough

Measured on elitebook, 2026-07-25 — **one checkout each, no worktrees**:

```
40G   vimcode/target
11G   quadraui/target
5.9G  claude-coordinator/tui/target
────
57G   of Rust build artifacts
```

Against a 256GB base mini, before any of that: macOS (~20GB), Xcode CLT + Homebrew GTK4 stack
(~5GB), the repos themselves, node/claude, and a **per-worktree `target/` dir** for every entry in
`~/.coord/worktrees/`. That configuration is uncomfortable in month one and hostile by month three.

Storage and RAM are both soldered on Apple silicon — neither is fixable later.

**If a 256GB box is already in hand**, the workable mitigations are:

- Put `~/src` and `~/.coord/worktrees` on a Thunderbolt NVMe enclosure (~3GB/s — fine for cargo).
- Set a shared `CARGO_TARGET_DIR`. Cargo locks the directory so concurrent builds serialize, which
  is what `concurrency: 1` wants anyway, and it collapses the per-worktree `target/` multiplier.
- Sweep stale target dirs on a cron.

## Buy vs. rent

**Rough figures as of 2026-07 (estimates — verify current rates before committing):** a dedicated
Apple-silicon mini rents for roughly $100–170/mo; AWS EC2 Mac is much worse because of its 24-hour
minimum allocation (~$450/mo). Three months of rental ≈ the purchase price of the machine, which
then still has resale value. Break-even is ~4–6 months.

Two reasons the purchase wins regardless of the exact rate:

1. **This is not a 3-month job.** Once quadraui / vimcode / coord-tui ship macOS builds, every
   future change to those crates needs a mac to test on — permanently. It is a standing CI +
   capability-routing target (`os:macos`), not a port sprint that ends.
2. **The work is GUI work, and GUI work over VNC is worse.** vimcode is GTK4 (`gtk4-rs` 0.7 +
   pangocairo) on quartz via Homebrew; quadraui needs native polish. Visual/UX iteration here is
   human-attended manual smoke, and driving that over a datacenter VNC session degrades exactly the
   thing the machine is being bought for. The automated `GtkDriver` harness rasterises offscreen and
   would run fine remotely — eyeballing UI would not.

## Provisioning runbook

**`scripts/setup-macmini.sh` does steps 1–8 of this, idempotently.** Copy it to the mac and run it
there in an interactive session (`--with-sudo` for Remote Login + no-sleep, `--with-launchd` to
write the agent plist, `--skip-clones` to make the run ssh-safe). Read this section anyway: the
script automates the steps, not the judgement about what the box should be allowed to do.

Order matters loosely; the coord agent goes last.

1. **macOS setup**
   - **Enable auto-login and Screen Sharing.** macOS GUI apps need a live WindowServer session — a
     plain `ssh` with no console session cannot launch them, so GTK4 live smoke fails without this.
   - Disable sleep (`System Settings → Energy`; `sudo pmset -a sleep 0 disablesleep 1`) — it's a
     server now.
2. **Xcode Command Line Tools only** — `xcode-select --install` (~1.5GB). Rust does not need full
   Xcode (~15GB+); install it only if notarization or Instruments becomes necessary.
3. **Homebrew**, then the GUI stack: `brew install gtk4 pango cairo gdk-pixbuf graphene` (~2–3GB).
   These are vimcode's `gtk4`/`pangocairo` deps — see its `Cargo.toml`.
4. **Rust** via rustup. Set the build-job cap globally in `~/.cargo/config.toml`:
   ```toml
   [build]
   jobs = 6
   ```
5. **Node + Claude Code**, and confirm the binary resolves for non-interactive shells. On the Linux
   fleet `claude` lives at `~/.local/bin/claude` and is zsh-only, which has bitten us over ssh/tmux
   — use absolute paths and verify `ssh <host> 'which claude'` before trusting it. macOS runtime
   parity (`shutil.which("claude")` resolution + launchd) is issue **#1158 / CP-3**.

   **This is not just about `claude` — it applies to every tool the agent shells out to** (#1671).
   What matters is the PATH of the agent's *service* process, which is far narrower than your login
   shell's. On Linux the fix is `Environment=PATH=` in the systemd unit; **the launchd equivalent is
   an `EnvironmentVariables` dict in the plist, so nothing from the Linux fleet's units ports
   across.** Whatever plist this box ends up with must reach `~/.cargo/bin` (rustup, step 4 above)
   and Homebrew's prefix (`/opt/homebrew/bin` on Apple silicon — not on any default PATH systemd or
   launchd hands you). Verify with `coord doctor --machine <name>` before declaring the box ready:
   every capability you give it in step 9 must show a probed version, not "not found".
6. **Tailscale** — join the tailnet; the agent is reached at `:7433` over MagicDNS like any other
   machine.
7. **Clone the repos** into `~/src/` — `quadraui`, `vimcode`, `claude-coordinator`. Remember
   `~/src/<repo>` is the **worker worktree base**; never delete it to "fix" drift.
8. **coord agent last.** Follow [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md) end-to-end — do not
   re-derive it. **INVARIANT: `~/.coord-venv` must be a PyPI install
   (`pip install code-coordinator`), never editable.** Service supervision on mac is launchd, not
   systemd (also #1158 / CP-3) — so `install-agent.sh` and `deploy/coord-agent.service`, which are
   the Linux fleet's source of truth for the agent's PATH (#1671), **do not apply here**. There is
   no checked-in launchd plist yet; writing one, with the PATH entry from step 5, is part of #1158 /
   CP-3. **`scripts/setup-macmini.sh` generates an interim one** (`--with-launchd`) with that PATH
   entry already filled in — it is not the CP-3 deliverable, just a working stopgap. A wrong PATH
   here fails *silently*: the agent starts, `/health` answers, capabilities read "not found", and
   smoke dispatch quietly refuses to route.
9. **Register in `coordinator.yml`** with `coord machine add` — `--max-workers 1`, and an
   `os:macos` capability once #1159 / CP-4 adds the `os:*` convention and the CI matrix (there is
   no such capability rule today, so adding it now routes nothing).

   > **`concurrency: 1` is not a key.** Earlier revisions of this step said so, and it does
   > nothing: `_parse_machines` (`coord/config.py`) reads named keys and never rejects unknown
   > ones, so the line is silently dropped and the box inherits the fleet-wide
   > `concurrency.max_workers` — 8 on this fleet, i.e. the exact two-concurrent-Rust-builds
   > swap scenario the sizing section above argues against. The field is **`max_workers`**.

## What non-macOS work to route there

The mac is not a single-purpose macOS box. Most of the fleet's work is platform-agnostic and can
route to it — and **it is useful before milestone #39 lands**, which is not obvious from
[`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) alone.

**Two findings that establish this (verified 2026-07-25):**

1. **`coord/` contains zero Linux-isms.** No `systemctl`, `/proc/`, `readlink -f`, `apt-get`, or
   `DISPLAY`/`Xvfb` references, and **no `sys.platform` / `platform.system()` branches at all**. The
   package is written POSIX-generic.
2. **macOS is already an intended agent platform.** `coord/interactive.py`'s module docstring states
   the POSIX imports are guarded because "the stdlib `pty` / `termios` / `fcntl` modules are not
   present on Windows, but agent machines are **Linux/macOS only**." All three modules exist on
   macOS — so **CP-1 (#1156) is a Windows concern, not a mac blocker.**

### Routes cleanly

| Work | Why |
|---|---|
| **coord-tui / quadraui TUI-side Rust** | `TuiDriver` renders to ratatui's `TestBackend` — no TTY, no display, fully portable |
| **Reviews** | `claude -p` + `gh pr diff`; zero platform surface |
| **`code-coordinator` Python** | portable in principle (finding 1) — but unverified in practice, see below |
| **webapp (React / Vite / vitest)** | portable; Playwright too, once a `browser` capability is staged |

The tui case is where the mac relieves a *current* bottleneck rather than just adding a body.
`coordinator.yml` annotates dellserver `NOTE 4 cores: fine for the Python suite, poor for tui/**
cargo builds`, and records that the fleet "was deadlocking at two concurrent claude-coordinator
items with only precision + elitebook." An M4 is a strong cargo machine and lands directly on that
constraint.

### Does not route there

**vimcode GTK4 work.** GTK4-on-quartz builds via Homebrew but behaves differently enough that a
`gtk` capability on the mac would be a lie for anything visual. Keep GTK work on precision /
elitebook until macOS GTK is a deliberate target.

### Two caveats

- **Portable-in-principle is not verified.** The coord pytest suite has never run on macOS. Finding
  1 covers the *source*; tests are where environment leaks in (BSD vs GNU userland in anything that
  shells out, tmux/ssh behaviour, path assumptions). Run the suite by hand before trusting dispatch.
- **CI is Linux-only until CP-4 (#1159).** Mac-developed work can pass locally and fail CI, or the
  reverse. That cost is real — but it cuts both ways: routing genuine work to the mac is the
  cheapest continuous de-risking of milestone #39, surfacing portability bugs incrementally instead
  of all at once when the mac milestone is finally picked up.

### Staging

Mirror the discipline already recorded for elitebook's `browser` capability — it was verified
locally *before* being added to the config. Start narrow:

```yaml
- name: macmini
  host: macmini.tailf46ef8.ts.net         # the MagicDNS FQDN, not the short name
  capabilities: [python, rust]            # NOT gtk, NOT browser
  repos: [claude-coordinator, coord-tui]  # hold vimcode until GTK4-on-mac is deliberate
  max_workers: 1
  repo_paths:
    claude-coordinator: ~/src/code-coordinator   # name != directory, on purpose (#2104)
    coord-tui: ~/src/coord-tui
```

**`coord-tui`, not `quadraui`, is the right first Rust repo** — this changed after the paragraph
above was written. quadraui's `test_command` now ends in a
`cargo clippy --target x86_64-pc-windows-msvc` leg, and dell64 is the only machine with that
toolchain, so a quadraui dispatch here fails in the Test stage regardless of how good the mac is
at Rust. coord-tui has pinned quadraui by git rev since #1973, so it needs no sibling checkout and
no symlink. `browser` is held back for a separate reason: node and npm resolve fine, but
`playwright-browsers` is not installed, so the capability would probe unmet.

Then verify by hand (`pytest`; `cargo test` in `tui/`) and widen from there. Note the one real
setup gap: **`coord agent` under launchd is CP-3 (#1158) and unbuilt** — until it lands, run the
agent in a foreground/tmux session or hand-write a plist.

## Provisioning traps found in practice (2026-09-06)

The runbook above is sound; these are the things it did not say, each of which cost time on the
first real provisioning run. **`scripts/setup-macmini.sh` automates steps 1–8 of the runbook and
already handles every trap in this list** — it exists so the second mac does not rediscover them.

**1. The state root is NOT `~/.coord` on macOS.** `coord/platform_paths.py` resolves through
`platformdirs` for `sys.platform in ("win32", "darwin")`, so the mac reads
`~/Library/Application Support/coord/coordinator.yml`. Symlinking the Linux path instead is the
single most expensive mistake available here, because it does not error — the agent starts,
`/health` answers 200, and it publishes `capabilities: []` with a `config_free` notice buried in
the payload. Every config-vs-`/health` cross-check then reads as absence rather than truth. Put
the symlink in the native directory and make `~/.coord` a symlink *to* that directory, so the
fleet's runbook paths keep resolving and there is exactly one state dir rather than two that
drift. (`$COORD_DIR` also overrides on every platform, checked before the `sys.platform` branch —
but setting it only in the agent's plist gives the agent and the interactive CLI different roots,
which is worse than either choice made consistently.)

**2. The login keychain is unreachable from a non-interactive ssh session.** `gh` stores its token
there, and `gh repo clone` leaves an https remote whose credential helper needs it too. Over ssh
you get `gh auth status` failing on a perfectly authenticated gh, and
`git pull` dying with `failed to get: -25308` / `could not read Username for 'https://github.com'`.
Nothing is wrong with the box — run those steps in an interactive session on the mac, or fetch
via an SSH URL with a forwarded agent (`ssh -A`). This is why the setup script only *requires* gh
when it is actually cloning.

**3. `runtime.coord_on_worker_path_missing` is a false CRIT on macOS.** `coord machine doctor
--ssh` reports that a worker cannot resolve `coord`. It can. The probe delegates to
`worker_coord_reachable(base_env=None)`, which resolves against *that process's* `os.environ` —
over ssh, the session PATH. On Debian/Ubuntu the default `.profile` puts `~/.local/bin` there, so
the Linux fleet passes; macOS's non-interactive ssh PATH is `/usr/bin:/bin:/usr/sbin:/sbin` and
the shim is invisible. Confirm by hand before chasing it:

```bash
ssh macmini 'AGENT_PATH="$HOME/.coord-venv/bin:$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
"$HOME/.coord-venv/bin/python3" - "$AGENT_PATH" <<"PY"
import sys, os
from coord.agent import worker_coord_reachable
print(worker_coord_reachable({**os.environ, "PATH": sys.argv[1]}))
PY'
```

On the first mac that printed `(True, "... 'coord' resolves at ~/.local/bin/coord ...")` while the
doctor CRIT stood — the shim was fine all along.

**4. launchd has no `StartLimitBurst`.** The plist mirrors the Linux unit's `Restart=always` with
`KeepAlive`, but `ThrottleInterval` only rate-limits the respawn (one per 10s) — there is no
equivalent of systemd's give-up-and-land-in-`failed`. A genuinely broken agent retries forever and
looks alive in `launchctl list`. Read the `last exit code` line from
`launchctl print gui/$(id -u)/com.jdonaghy.coord-agent`, not the process's existence.

**5. `launchctl bootstrap gui/$(id -u)` works over ssh** when the user is logged in at the console
— which is the arrangement you want anyway, since LaunchAgents start at **login, not boot**.
Without auto-login the agent does not come back after a reboot, and nothing distinguishes that
from the machine being switched off. `loginctl enable-linger` has no macOS equivalent; the linger
probe correctly reports UNKNOWN rather than a defect.

> Verify this rather than assuming it. On the first mac, the agent *did* come back after an OS
> update — but auto-login was off the whole time and someone had simply logged in. The tell:
> `defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser` reports the key "does
> not exist" while the domain itself reads fine, and `/etc/kcpassword` is absent. A reboot that
> works because a human was standing there is not a machine that survives reboots.

**6. Remote Login is off by default and `systemsetup -setremotelogin on` needs Full Disk Access**
for the calling terminal, so it fails silently from a plain shell. Enable it in
System Settings → General → Sharing. Until it is on, `coord machine doctor --ssh`, remote session
inspection (`coord/interactive.py`), and every recovery path have no channel to the box —
note that `coord release propagate` does *not* use ssh (it is an HTTP `POST /update` to the
agent), so ssh is precisely the channel you need when propagation is what stranded the agent.

**7. Two identity CRITs are real, and both need an interactive session on the mac.**
`identity.git_push_missing` (no `ssh -T git@github.com` key — every worker commits, then fails to
push, destroying the session's work) and `identity.claude_oauth_missing` (no
`~/.claude/.credentials.json`, so `claude -p` cannot start and the machine fails every dispatch it
accepts). Neither is visible from `/health`. Clear both before routing work.

**9. Idle sleep will kill a running worker, and nothing in the failure says "power".** A mini
provisioned without `--with-sudo` kept macOS's default sleep behaviour, went to idle sleep
mid-dispatch, and dropped off the tailnet:

```
21:51:02  Entering Sleep state due to 'Idle Sleep': TCPKeepAlive=active  Using AC
22:02:03  DarkWake ... 22:02:10 Wake
```

The work leg failed; the fleet redispatched it to another machine and it completed there, and the
branch survived only because the worker had already pushed. From the board this looks like an
ordinary worker failure — there is no signal pointing at power management, and the machine is
back online and healthy by the time you look.

`sleep 0` alone is not sufficient: `disksleep` and `powernap` produce the same symptom from the
fleet's side. Set all of them, and `autorestart` so a power cut does not leave the box dark:

```bash
sudo pmset -a sleep 0 disablesleep 1 disksleep 0 powernap 0 womp 1 autorestart 1
```

`scripts/setup-macmini.sh` applies these under `--with-sudo`, and — because reading power state
needs no privileges — **reports them even without it**, naming `sleep` explicitly in its residue
rather than a generic "step skipped". That gap is what let this happen: the original script said
only "Remote Login is off" and never mentioned sleep at all.

**8. Cosmetic, but it will make you look twice:** `hostname -s` is `Johns-Mac-mini`, not the
machine name — always pass `--machine` explicitly rather than relying on the Linux installer's
hostname default. And the tailnet peer's `HostName` is the literal "John's Mac mini" with a curly
apostrophe, so `coord machine add`'s short-name tailnet lookup misses and reports
`host_resolution_unknown` (`?`, not `✓`). The FQDN is correct; it writes anyway.

## Ongoing hygiene

- Watch memory pressure before raising concurrency past 1.
- Sweep `target/` dirs periodically — 57GB of artifacts accumulates from *one* checkout each, and
  worktrees multiply it.
- `vimcode` links `quadraui` by relative path (`../quadraui/quadraui`, from vimcode's repo root),
  so the mac needs the same worktree symlink arrangement the Linux agents use — for vimcode
  only. `coord-tui` no longer has this exposure: since #1973 it pins quadraui by git rev in
  `tui/Cargo.toml`, so `cargo build`/`cargo test` in `tui/` fetch quadraui straight from GitHub
  and need no sibling checkout or symlink at all.

## Related

- [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) — the port itself; milestone #39 / epic #1160 (CP-1 #1156
  POSIX-import guards, CP-2 #1157 single-node local mode, CP-3 #1158 macOS runtime parity, CP-4
  #1159 CI matrix + `os:*` capability rules).
- [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md) — agent install, the PyPI-not-editable invariant,
  service restart, `did not come back` triage.
- [`../scripts/setup-macmini.sh`](../scripts/setup-macmini.sh) — the provisioning script for steps
  1–8, with every trap from the section above already handled.
- [`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) — fleet traps that each cost a real dispatch.
