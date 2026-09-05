# Agent operations

How to install, upgrade, diagnose, and recover the per-machine agent server.

## Releasing: merge to `main`. That's the whole runbook. (#1835)

**The only human action in a release is merging the PR.** Everything after
the merge — picking the version, tagging, building, publishing, rolling the
fleet, verifying it, rolling back if it's red — happens with no human and no
agent. There is no version to bump (#1238 removed both literals; the git tag
*is* the version), no tag to push, and no `coord agent update` to remember.

This section is the program of record. If you find yourself typing
`git tag` or `coord agent update` as part of a normal release, something is
broken — fix that, don't route around it.

### The two halves, and why they are separate

The pipeline is deliberately cut in two, and the cut is the design:

| | **Publish** | **Propagate** |
|---|---|---|
| trigger | the merge, immediately | the next **quiescent window** |
| runs on | GitHub Actions | `coord-release-propagate.timer`, daemon host |
| touches | PyPI + the GitHub Release | every host's venv, units, `coord-tui` |
| restarts anything? | **no** | **yes — every agent** |

`coord agent update` restarts the agent, and a restart kills every in-flight
headless worker. With overnight drive queues (#56/#1750) the fleet is rarely
idle, so a merge-triggered *fleet upgrade* would routinely destroy in-flight
work — and the better the queue works, the more it destroys. Publishing
touches no running host, so it can happen the instant you merge. Propagation
cannot, so it waits.

### Half 1 — publish, on merge

`.github/workflows/auto-release.yml` fires on every push to `main`, decides
whether the merge ships anything, picks the next `vX.Y.Z`, and pushes that
tag. `.github/workflows/publish.yml` (unchanged, #1242) then turns that one
tag into one GitHub Release carrying the wheel (webapp bundled), the sdist
and the `coord-tui` binaries, and uploads to PyPI.

**Trigger policy** — path-filtered, opt-out by commit message, coalesced.
The judgement lives in `scripts/next_release_tag.py` (with tests in
`tests/test_next_release_tag.py`), not in YAML:

- **Docs-/tests-only merges cut nothing.** A PyPI version is an immutable
  public name; it doesn't get spent on a change no user of the wheel can
  observe. Anything under `coord/`, `tui/`, `deploy/`, `scripts/`,
  `pyproject.toml`, `MANIFEST.in`, `install-agent.sh` or
  `.github/workflows/` ships — and anything *unrecognised* counts as
  shipping, so the failure mode is a superfluous release, never a fix that
  silently never reaches a host.
- **A burst of merges is one release.** The workflow's `concurrency` group
  cancels a superseded run, so five merges in five minutes mint one tag at
  the tip.
- **Opt out with `[no release]`** (or `[skip release]`) in the merge commit
  message. `[minor]` / `[major]` bump those components instead of the patch.

Rehearse a policy change without publishing anything:

```bash
gh workflow run auto-release.yml -f dry_run=true   # prints the decision, pushes nothing
```

**`main` requires passing status checks to push to (#1525)**, which is why
the trigger is a *merge*, not a push. Nothing about that changed; the
release just stopped needing a second human action after it.

### Half 2 — propagate, at the next quiescent window

`coord-release-propagate.timer` on the daemon host fires every 20 minutes
and runs one attempt:

1. **Resolve the target** from PyPI's simple index.
2. **Ask whether there is a window.** Quiescence is the *drive queue's*
   definition, not a second opinion — a queue entry in `running`, or any
   agent with a live `RUNNING`/`PENDING` assignment, means busy. A fired
   `--hold-after` deploy gate (#1757) is **not** busy: it means the queue
   has deliberately stopped *waiting for exactly this deploy*.
3. **If busy: record a deferral and exit 0.** This is the normal outcome
   most of the time and it is not a failure.
4. **If not: roll each lane, daemon host first** (see below).
5. **Run `coord release verify` as the final gate — scoped to the lanes
   this run could actually roll (#2052).** On CRIT *in scope*, roll every
   host this run updated back to its previous venv generation (#1241's
   blue/green keeps exactly one) and exit 2.
6. **On green, release the deploy gates** that were waiting for this deploy
   — the queue starts launching again on its own.

**The gate's scope, and why it has one (#2052).** `coord release verify`
grades lanes propagation *cannot roll*. On 2026-08-09, the first run that
ever reached step 5 did everything it was capable of — three python lanes,
three unit lanes, the one `coord-tui` it could reach — and still came back
red, because verify also counted `~/.coord-cli-venv` (a lane propagation has
no model of), the two *remote* `coord-tui` binaries (which propagation
itself reports have **no remote install path**) and the `coord-serve`
process (whose venv had swapped but whose *process* nothing here restarts).
`--rollback-on-red` then reverted its own good work. That was not a
transient failure: it would have fired on every run, forever.

So findings now split two ways, and `coord release history` shows both:

* **blocking** — a lane this run attempted *and could have moved*. These
  are the only findings `--rollback-on-red` may act on.
* **advisory** — a lane propagation has no channel for. Reported loudly,
  journalled in full, **never** grounds for a rollback. An advisory CRIT is
  a real defect somebody must fix by hand; it is simply not evidence that
  *this roll* was bad.

A lane nobody has classified still blocks — the exemption is an allow-list
of known gaps, not a fallthrough, because a gate that quietly stops gating
is the failure mode all of this exists to prevent. The known gaps today are
`~/.coord-cli-venv`, `webapp bundle`, and any `<unit> spawns` lane other
than `coord-agent` (a python roll re-execs the agent and nothing else, so
`coord-serve` keeps running the generation it started with until you
`systemctl --user restart coord-serve` yourself).

**A rollback restores service state, not just the symlink (#2052).** The
same run left precision's `coord-agent` `inactive (dead)` and never restarted
it — recovery needed a human. `coord release rollback` and propagation's own
revert now wait for `/health` to answer again, escalate once to the
documented SSH `systemctl --user restart coord-agent` (#404/#1568), and only
then give up — naming the host as **DOWN** rather than reporting a tidy
"rolling back". `--wait` tunes the window.

**The daemon host is derived, or the run refuses (#2052).** It is the
machine whose own `/health` reports a running `coord-serve` unit. If nothing
can name it and the fleet has more than one host, propagation **refuses to
roll** rather than falling back to `coordinator.yml` order — that guess
briefly put the daemon *behind* both its callers during a partial revert,
which is precisely the 405 hazard the lane order exists to prevent. Pass
`--daemon-host <machine>` to pin it.

The loop closes: the gate stops the queue for the deploy, propagation
performs the deploy, propagation restarts the queue.

**Lane order, and the skew question.** A fleet mid-roll has hosts at two
versions. That is safe in **one direction only**: a *newer daemon serving
older callers* is the steady state between every release and every fleet
update anyway, but an *older daemon* asked for an endpoint it predates
returns 405 — a documented failure here. So the order is fixed and not
negotiable:

1. the **daemon host**'s Python lane (`~/.coord-venv`) — always first;
2. every other machine's Python lane;
3. each host's **systemd unit** lane — only *after* that host's venv
   swapped, because the reference units ship *inside* the wheel
   (`coord/deploy/`, #1927);
4. **`coord-tui`** last — a pure board-API client, safe at any skew.

Propagation is therefore explicitly **not** all-or-nothing. It is ordered so
every intermediate state is one the protocol already tolerates.

**The units lane (#1831) now has a deploy step.** Each agent serves
`POST /deploy-units`, which rewrites the units this host *already runs* from
the packaged release, renders `<MACHINE_NAME>`/`<PORT>` templates for that
host (#1928), keeps a `.pre-<version>.bak` of each, and `daemon-reload`s.
It **restarts nothing**, so no worker dies. Two things it deliberately will
not do: install a packaged unit this host has never had (which services a
host runs is a topology decision, not a release decision — it reports them
instead), and guess a placeholder value it doesn't know (it skips and says
so). Both show up in `coord release history`.

### Watching it, after the fact

```bash
coord release history            # every attempt: deferrals collapsed, rolls in full
coord release history -v --json  # the raw JSONL records
coord release verify --pypi      # the same gate propagation runs, on demand
```

An **empty** history is itself a finding: it means the timer never ran.
That distinction is the point — on 2026-08-04 a silent no-op was
indistinguishable from a silent success for hours (see
[The post-release step](#the-post-release-step-coord-release-verify-1834)).
The journal is `~/.coord/release_propagation.jsonl`, deliberately a file
rather than a DB table so it stays readable with `tail` *while* the upgrade
it describes is in flight.

### Escape hatches (all of them optional)

```bash
coord release propagate --dry-run          # window verdict + roll plan, changes nothing
coord release propagate --target v0.4.111  # pin the version instead of asking PyPI
coord release propagate --lane units       # one lane only
coord release propagate --force            # roll over a BUSY fleet — KILLS live workers
coord release propagate --cordon-max-deferrals N   # #2240: consecutive deferrals a
                                           # cordon may hold before it self-releases
                                           # (default 2; 0 re-arms the deadlock)
coord release propagate --cordon-cooldown S # #2240: seconds cordoning stays off after
                                           # a self-release (default 1800)
coord release propagate --min-behind N     # #2583: hold below N releases behind PyPI —
                                           # see "Auto-roll threshold gate" below
coord release rollback --yes               # one command: every agent back one generation
```

`--force` is an interactive-operator flag and is deliberately absent from
the systemd unit. `coord release rollback` is the #1560 requirement that
rollback be one command rather than a runbook.

`coord release-preflight` still exists for the manual path (it asserts
you're on a clean `main` matching `origin/main` — a **post-merge, pre-tag**
check). You should not need it in a normal release any more.

**`coord-tui` is the one lane propagation cannot finish remotely.** It's a
per-host binary in `~/.local/bin` with no agent endpoint, so propagation
rolls it on the host it runs on and *reports the others as a gap* rather
than silently omitting them. On any other host: `coord tui update`.

### PyPI timing gotchas (unchanged)

PyPI propagation can lag a minute or two after the workflow goes green.
`pip install --upgrade` (and `coord agent update`) may report
`no_change` until the new version is visible — wait and retry rather
than assuming the publish failed.

**Poll the SIMPLE index, not the JSON API — they flip independently, in
both directions.** `pip` resolves against
`https://pypi.org/simple/<pkg>/`, so that is the only endpoint that
answers the question you actually care about. Measured twice:

| release | JSON API (`/pypi/<pkg>/json`) | simple index | outcome |
|---|---|---|---|
| v0.4.88 (2026-07-29) | already `0.4.88` | still `0.4.87` | `pip install ==0.4.88` failed: *No matching distribution found* |
| v0.4.89 (2026-07-30) | still `0.4.88` | already `0.4.89` | install succeeded while the JSON API looked stale |

Neither one leads reliably. Ask "can pip resolve it?", not "is it
published?":

```bash
curl -s https://pypi.org/simple/code-coordinator/ | grep -q 'code_coordinator-X\.Y\.Z' && echo installable
```

Expect per-machine variation even after that: a mirror/resolver on one
host can still miss the release for a minute or two after another host
installs it cleanly. Retry that machine rather than concluding the
publish broke. **After every install, verify `coord --version` — never
infer success from pip's exit code.**

**Anything that changes `coord/agent.py` (e.g. the worker system
prompts) only takes effect on agents after a release + rollout.**
Coordinator-only Python (CLI, `notify.py`, `merge_queue.py`, parsers) is
live from the editable install the moment it's on disk — but agents run
from PyPI, so agent-side changes reach them only when propagation rolls the
Python lane. That is now automatic; what used to be "plus the rollout below"
is now "plus the next quiescent window".

- **`coord-tui`** is a Rust binary. Since #1239/#1240 it *is* built and
  attached to every release, and `coord tui update` installs it — so
  "rebuild it locally" is now the dev-loop path, not the distribution path.
  Propagation rolls this lane only on the host it runs on (no agent
  endpoint installs a binary remotely); run `coord tui update` on the
  others, and check `coord release history` for which hosts it named.
- **The phone webapp** (`coord/dashboard/webapp/`) is bundled into the
  PyPI wheel as of 0.4.71 (built by the release workflow). No `npm run build`
  is needed on the dashboard host after a `pip install` or `coord agent update`.
- **`deploy/**`** used to be the lane with no deploy step. It has one now —
  see "The units lane" above and the `deploy/coord-release-propagate.*`
  units, which must themselves be installed by hand **once**, on the daemon
  host, to bootstrap the loop.

## INVARIANT: every remote agent's `~/.coord-venv` is a PyPI install, never editable

Moved here from `CLAUDE.md` by #2195 so it sits with the rest of the install runbook. **Read
this section end-to-end before touching any agent install — don't re-derive it.**

This is the single most-recurring fleet failure.

- **PyPI install** → `coord agent update` runs `pip install --upgrade` and a released
  `vX.Y.Z` lands cleanly.
- **Editable install** → the update silently `git pull`s a local checkout instead, so version
  bumps never propagate and the agent often "did not come back."

**Root cause:** someone ran `pip install -e .` into `~/.coord-venv`. The editable **install**
is the problem, **not** the `~/src/<repo>` checkout. #402's PATH-strip only stops *workers'*
bare-pip, not a deliberate editable install.

### DO NOT delete `~/src/<repo>` to "fix" drift

It is the **worker worktree base** — `git worktree add` runs from it, and the worktrees in
`~/.coord/worktrees/` are worktrees *of* it. Deleting it breaks every task for that repo on
that machine. Fix **only the install**.

### Detect

```bash
ssh <host> '~/.coord-venv/bin/pip show code-coordinator | grep -i "editable\|location"'
```

Any `Editable project location:` line ⇒ drift. A PyPI install shows only a site-packages
`Location:`.

### Fix (keeping the checkout)

```bash
# in ~/.coord-venv on the affected host
pip uninstall -y code-coordinator && pip install --upgrade code-coordinator
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-agent
```

The restart is not optional: the `/update` endpoint's `os.execv` self-restart does **not**
take under systemd (#404 — it leaves the same PID and a stale version).

### `✗ did not come back` is usually a FALSE NEGATIVE

The agent is generally online and the restart simply didn't take. Check the running
version/PID first, then choose between a drift-fix and a plain
`systemctl --user restart coord-agent`.

## Install a new agent (first time)

On the target machine:

```bash
curl -sSL https://raw.githubusercontent.com/JDonaghy/code-coordinator/main/install-agent.sh | bash -s -- --machine <name> --port 7433
```

This creates `~/.coord-venv`, installs `code-coordinator` from **PyPI**, writes a `coord-agent` systemd user unit, and starts it. The agent does NOT need a git clone of the repo — the `~/src/claude-coordinator` directory should only exist on the machine where you actually develop the coordinator itself.

Verify:

```bash
curl -s http://<host>:7433/health | python3 -m json.tool
coord doctor --machine <name>     # <-- do NOT skip this; see below
```

The `version` field should match the latest PyPI release, and `coord doctor` must report a probed
version for **every** tool backing a capability you declared for this machine in `coordinator.yml`.

### The agent's PATH is narrower than your login shell (#1671)

**A tool being installed does not mean the agent can run it.** A systemd *user* unit gets a minimal
PATH — it omits everything installed under `$HOME` by rustup, pipx, nvm and friends. The unit
therefore sets `Environment=PATH=` explicitly, and anything missing from that line is invisible to
the agent **and to every worker it spawns** (#402: a worker's PATH is the agent's, with the venv
stripped).

`install-agent.sh` and `deploy/coord-agent.service` both include `~/.cargo/bin` and `~/.local/bin`
as of #1671. **An agent installed before that has a unit without `~/.cargo/bin`** — fix it with a
drop-in rather than replacing the unit:

```bash
mkdir -p ~/.config/systemd/user/coord-agent.service.d
printf '[Service]\nEnvironment=PATH=%h/.coord-venv/bin:%h/.cargo/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin\n' \
    > ~/.config/systemd/user/coord-agent.service.d/path.conf
systemctl --user daemon-reload && systemctl --user restart coord-agent
```

`Environment=PATH=` **replaces** the whole PATH, so read the machine's current value first
(`systemctl --user show -p Environment --value coord-agent.service`) and add to *that* — the order
is not identical across machines, `~/.coord-venv/bin` must stay first so `coord` resolves to the
agent venv, and `~/.local/bin` must keep its slot because that is where `claude` lives.

**What this costs when it's wrong:** the `rust` capability probe reads "cargo not found" even though
cargo is installed, `dispatch_smoke` refuses to route any `tui/**` work to that machine (#1570 D),
and the Test stage retries every 30s forever with **no smoke row and no board-visible reason**. On
2026-08-01 that silently blocked two issues for hours. `coord doctor` is the check that finds it,
which is why it belongs in the verify step above.

Since #1671 the agent also logs its **resolved PATH and install location** at startup, so
`journalctl --user -u coord-agent` answers "what can this agent actually see" without a `pip show` /
`grep` expedition.

## Onboard a new repo (`coord repo add` / `coord repo doctor`, #2220)

Adding a repo to the fleet is ~14 steps across **five layers** — config, every
machine that serves it, GitHub, the repo's own contents, and the graph — and
every one of them fails silently. Nothing reports a half-onboarded repo; it
just behaves like a working one that never quite gets anywhere. `stick-demo`
sat two-thirds onboarded until `stick-demo#1` went terminally `blocked` having
burned both drive attempts, while `coord config`, `coord status` and `coord
assign --dry-run` all reported the dispatch as fine — because all three read
*config*, and config was correct.

**The verifier is the part that matters.** This section is deliberately short
because a runbook is the weakest available answer: `docs/GRAPHIFY_SETUP.md` is
exactly that shape and graphify has still fallen by the wayside more than once,
because nothing checks it.

```bash
coord repo add <name> --github <owner/repo> --machines precision,elitebook,dellserver
```

Does only the mechanical, safely-automatable parts: reads the **real** default
branch from GitHub (never trusts a flag — a wrong `default_branch` silently
routes every worker PR to the wrong base), writes the `repos:` entry into the
**coord-settings checkout** (`~/src/coord-settings/coord/coordinator.yml`, never
the `~/.coord/` symlink — #1832), adds the repo to each named machine's `repos:`
and `repo_paths:`, and creates the `coord` / `tier:small` / `tier:large` labels.
It re-parses its own edit and refuses to write if the result would not load.

Then it **prints the residue it deliberately did not do** — clone, `CLAUDE.md`,
CI workflow, `capability_rules` — rather than pretending completeness. Work
through that list, then:

```bash
coord repo doctor <name>        # exits non-zero on any CRIT, so it can gate
coord repo doctor <name> -v     # show the passing checks too
```

`coord repo doctor` probes all five layers from **live state, not config**:
each agent's `/health` repo list, the clone on each machine, the labels that
exist on GitHub, whether any workflow triggers on `pull_request`, whether a
`CLAUDE.md` is present, and graph freshness. Each defect gets its own named
finding and its own remedy — they are not interchangeable:

| finding | what it means | fix |
| --- | --- | --- |
| `machines.agent_repo_skew` | config declares the repo for this machine; the live agent's `/health` still doesn't serve it | since #2299 agents re-read `coordinator.yml` themselves — **wait one `/health` poll and re-run**. If it persists: that agent's file is stale (`git pull` in `coord-settings` on *that* machine), the edit is malformed (check `journalctl --user -u coord-agent` for `failed to reload`), or the agent predates #2299 (`coord agent update`). A restart is the last resort, not the first |
| `machines.clone_missing` | the agent knows the repo and reports the path absent | clone it; a restart changes nothing |
| `machines.repo_path_missing` | no `repo_paths` entry at all | a config repair |
| `github.coord_label_missing` | issues are live but **invisible to the Pipeline** | `gh label create coord` |
| `github.no_pull_request_trigger` | workflows exist but none triggers on a PR, so `expects_checks()` reads "CI exists" while zero checks arrive and `checks_absent` blocks **every** merge in that repo, forever | add `on: pull_request:` |
| `contents.claude_md_missing` | the Test agent auto-loads `CLAUDE.md` and the adversarial review prompt is assembled from it — without one, reviews enforce nothing | add one |
| `config.default_branch_mismatch` | `coordinator.yml` disagrees with the repo's real default | fix the entry |

`coord doctor` folds in the **live layer only** (the `machines` findings above),
so a half-onboarded repo shows up in the fleet report without anyone
remembering to ask. It costs no extra round trip — it re-reads the `/health`
bodies `coord doctor` already fetched. The GitHub, contents and graph layers
need the dedicated command.

Do not forget the step no config edit can do for you: **commit and push in
`coord-settings`, then `git pull` on every machine that serves the repo** — the
fleet runs the committed config, and an agent can only re-read the file that
actually exists on its own disk.

### Agents re-read `coordinator.yml` themselves (#2299) — what is hot, what is not

Adding a repo used to require `systemctl --user restart coord-agent` on every
machine that should serve it. That is the one action that also **kills live
workers**, so in practice a repo could only be onboarded once the whole fleet
had gone quiet. Worse, the skew was silent and asymmetric: `coord config`,
`coord status` and `coord assign --dry-run` all read the *file* and agreed the
repo was supported, while the agent refused every dispatch for it.

Agents now carry the same mtime-guarded reload the board daemon has had since
#1081 (`coord.config_reload.reload_config_if_stale`), driven off the existing
`/health` poll and the dispatch path — no new timer, no background thread.

| field | behaviour |
| --- | --- |
| `machines[].repos` | **hot** — next `/health` poll or next dispatch |
| `machines[].repo_paths` | **hot** |
| `machines[].capabilities` | **hot** — routing reads the published `/health` list, so a removed capability degrades (stops attracting work) rather than stranding anything |
| `repos[].artifact_paths` | **hot** |
| `repos[].build_command` | **hot** |
| `providers:` | **restart-only** — a live worker holds a provider resolved at dispatch time; swapping the registry underneath would retarget a running session |
| `concurrency.bash_wrap_spawn`, `concurrency.first_output_timeout` | **restart-only** — process tuning `_spawn` has already committed to |
| agent bind host/port | **restart-only** — uvicorn bound the socket at startup |

Two invariants worth knowing when reading `journalctl --user -u coord-agent`:

* **A malformed hand-edit does not take the agent down.** It logs `failed to
  reload ... keeping last-good config until the file is fixed` **once** (the
  tracked mtime advances, so a bad edit is not retried on every poll) and the
  agent keeps serving the pre-edit config. Fix the file and the next poll picks
  it up.
* **A reload never disturbs a running worker.** Any repo with a PENDING or
  RUNNING assignment keeps the `repo_paths` / `artifact_paths` /
  `build_command` values it started with — including their *absence* — until
  that assignment is terminal. The new config governs the next dispatch onward.

`/health` publishes a `config_reload` block (`watching`, `reloads`,
`last_reload_at`) so "did my edit land?" is answerable from `coord doctor`
rather than an SSH. `watching: null` means that agent has no local
`coordinator.yml` at all (config-free / thin-client, `docs/EPHEMERAL_WORKERS.md`)
— for those, a restart *is* still the only way to change the repo list, and
their repos come from the coordinator at dispatch time anyway.

## Install coordinator skills (`coord install-skills`, #319)

Coordinator ships bundled **Claude Code skills** (slash commands available inside
any `claude` session). You do not need to run this manually on a machine running
`coord agent` — every agent's health tick self-heals a missing or stale skill the
same way it self-heals a stale graphify graph (see `AgentServer._self_heal_missing_skills`
in `coord/agent.py`, and `/health`'s `skills_self_heal` block for the pass count and what
was last synced). This closes the gap #319 originally left open: `coord install-skills`
was a real fix nothing in provisioning ever ran, so a skill added or updated in a release
could sit uninstalled on a worker machine indefinitely, unnoticed.

Run the CLI form yourself for the operator's own machine (the health tick only covers
machines running `coord agent`), or to see status/preview without writing anything:

```bash
coord install-skills              # install/update all bundled skills to ~/.claude/skills/
coord install-skills --list       # show bundled skills + installed status (no writes)
coord install-skills --dry-run    # print what would be installed without writing
```

Skills are read directly from the installed PyPI package via `importlib.resources` —
no repo clone required.

### Available skills

Because skills land in `~/.claude/skills/` (user-level, machine-wide) rather than a
repo-scoped `.claude/skills/`, once installed on a machine every one of these is available
in *any* `claude` session there — the operator's interactive coordinator session, a headless
`claude -p` worker leg, or a reviewer leg, in whatever repo that machine happens to be
working in at the time.

| Skill | Trigger | Purpose |
|---|---|---|
| `update-issue` | `/update-issue` | Synthesize what was agreed in a "Chat about issue" session and write it back to the GitHub issue body.  Calls `coord issue edit` after operator confirmation; offers `coord ready` to mark the issue ready for dispatch. |
| `portal-followup` | Investigating a stuck customer-portal signoff/event, or any `coord portal` troubleshooting | Decision tree for `coord portal outbox`/`events`/`sync` — daemon-host-only commands, the `credentials_set` check, `HELD` rows, and the preview-approval pre-merge gate. |
| `merge-stuck-triage` | A story won't merge — "Go" does nothing, `coord merge` skips it | Walks the merge gates in the order they actually block: Test verdict, review approval, CI, PR conflicts, queue clog, post-bounce keying. |
| `pipeline-limbo-triage` | An issue sits in the Pipeline with no assignments and nobody dispatched it | Explains the `status:ready`-with-no-assignment limbo state and the `coord backlog` fix. |
| `drive-queue-preflight` | About to queue >~2 issues on one repo, or reading a `QUEUE: STALLED`/`BLOCKED` alert | The intra-repo staling cascade, the `coord merge --revalidate` drain, and the alert decoder table. |
| `fleet-restart-safety` | About to restart `coord-agent`/`coord-serve`, or edit `coordinator.yml` on a live fleet | Which restart kills which thing, the correct pre-restart check for each, and why `coordinator.remote.yml` reverts on edit. |
| `review-verdict-recovery` | `coord gates` shows `review : ERROR`, or a review reached `END_REVIEW` with no verdict | Recover the verdict from the transcript via `coord report-result --verdict-source recovered` instead of re-dispatching. |
| `tui-quadraui-workflow` | Working in `tui/` and the task needs a quadraui pin bump or an unmerged quadraui branch | Bumping `tui/Cargo.toml`'s pinned `rev` vs. building against a local `~/src/quadraui` checkout via the cargo local-paths override, without touching the pin. |
| `coord-dispatch-verbs` | About to enqueue/dispatch/stage an issue via `coord`, especially if unsure of the exact subcommand, or a dispatch command "succeeded" but nothing showed up where expected | Disambiguates `coord queue` (label only, no dispatch) from `coord drive-queue add` (the real driver queue) and similar near-miss command pairs; the rule to verify effect, not exit code. |

### Usage (inside a "Chat about issue" session)

```
/update-issue
  → agent synthesizes conversation
  → proposes new issue body in-chat
  → operator reviews / requests tweaks
  → agent calls: coord issue edit <repo> <issue> --body-file /tmp/body.md
  → offers: coord ready <repo> <issue>?
```

## Control-center daemon (`coord serve`, #584/#591)

The portable control center runs a **daemon** that fronts the one shared
`~/.coord/coord.db` over Tailscale, so `coord-tui` / `coord status` (and remote
`coord report-result`) on **any** machine render and drive the **same** board.
The daemon listens on **7435** (agent=7433, dashboard=7434). Run it on the
always-on box that owns the DB — **dellserver** for production.

Endpoints: `GET /healthz` (liveness, never auth-gated), `GET /board` (full
projection), `GET /config` (raw `coordinator.yml`), `POST /result` /
`POST /completion` (#590 write path — a remote session's result lands on the
shared DB). A thin client carries no `coord.db`/`coordinator.yml`; it reads both
from the daemon.

### Prerequisites (daemon host)

- **A coord build with `coord serve`.** It ships in the #584/#590 release and
  later. A PyPI install older than that has no `serve` command, so the daemon
  host must be on a release `>=` that cut (or, pre-release, an editable checkout
  of the branch — note the editable-drift caveats elsewhere in this doc).
- **`coordinator.yml` present on the daemon host**, canonically at
  **`~/.coord/coordinator.yml`** (it serves this at `/config`; clients then need
  none — that's the point of #591). Path resolution is `$COORD_CONFIG` →
  `~/.coord/coordinator.yml` → `./coordinator.yml`; the home location means the
  daemon is independent of its CWD and of any repo checkout. (Older installs kept
  it at `~/coordinator.yml`, which only loaded when the daemon's CWD was `$HOME` —
  `mv ~/coordinator.yml ~/.coord/coordinator.yml` to move to the canonical spot.)
  `coord serve` prints the resolved config path on startup.
- **`~/.coord/coord.db` present** (after the one-time cutover below).
- **`gh` >= 2.86.0 on the daemon host.** `coord merge`'s CI gate re-invokes
  itself on the daemon (`COORD_MERGE_ON_DAEMON`), so it's the *daemon's* `gh`
  that runs `gh pr checks --json ...` (`coord/github_ops.py::get_pr_checks`)
  for every production merge — the thin client's `gh` version is irrelevant.
  Fleet versions have diverged badly enough to matter: dellserver's apt
  package (2.45.0) doesn't recognise `pr checks --json` at all (`unknown
  flag: --json`), while elitebook (2.86.0) and precision (2.92.0) both
  support it. An old-enough `gh` now fails loudly with an explicit "gh too
  old" merge refusal naming this floor and the host
  (`coord.github_ops.GhTooOldForJsonChecks`) instead of the generic
  unreadable-CI message (#1564). The floor is a module constant,
  `coord.github_ops.GH_PR_CHECKS_JSON_MIN_VERSION` — update both places
  together if a narrower one is ever confirmed.

### Install the service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/coord-serve.service ~/.config/systemd/user/   # from a checkout, or scp it over
loginctl enable-linger "$USER"          # survive logout / reboot (same as coord-agent)
systemctl --user daemon-reload
systemctl --user enable --now coord-serve
```

### Bearer token (defence-in-depth)

Tailscale ACLs are the real boundary; a shared bearer token is belt-and-braces
(full per-user auth is #282). Set one on the production daemon:

```bash
openssl rand -hex 32 > ~/.coord/serve_token && chmod 600 ~/.coord/serve_token
systemctl --user restart coord-serve     # picks it up via resolve_serve_token()
```

The daemon resolves the token **flag > `$COORD_SERVE_TOKEN` > `~/.coord/serve_token`**.
Prefer the file/env — a `--token` on the command line leaks via `ps`. With no
token the daemon runs **open** (fine for dev; it logs a warning).

### Verify

```bash
curl -s http://<daemon-host>:7435/healthz                 # {"status":"ok",...}
curl -s -H "Authorization: Bearer $(cat ~/.coord/serve_token)" \
  http://<daemon-host>:7435/board | python3 -c 'import sys,json;b=json.load(sys.stdin);print("round",b["round_number"],"assignments",len(b["assignments"]))'
```

### Point clients at it

On every **client** machine (NOT the daemon host) — `~/.coord/client.toml`:

```toml
board_service = "http://<daemon-host>:7435"   # e.g. dellserver's stable tailnet IP/MagicDNS
token = "<the same secret>"                    # omit if the daemon runs open
```

Resolution is **flag > `$COORD_SERVICE_URL`/`$COORD_TOKEN` > `client.toml`**. The
client's `coord` must also be a build with the thin-client code (#584/#590). The
**daemon host must NOT have `client.toml`** (it owns the DB; a stray file would
make it a thin client of itself).

### One-time cutover / ETL (elitebook → dellserver)

**Historical — kept for the DB-copy mechanics only.** This was #591's
one-time move, done before #1779 replaced `coordinator.yml` deploy with
symlink + `git pull` (see `OPERATING_GOTCHAS.md` #14). Do not `scp`
`coordinator.yml` again for routine config changes — that overwrites the live
path with a disconnected regular file exactly like the `sed -i`/editor trap
#14 warns about. It is only appropriate for bootstrapping a *brand-new*
daemon host that has no `coord-settings` checkout and no symlink yet; even
then, prefer cloning the checkout and symlinking it in per #14's recipe.

The board DB currently lives on **elitebook**; #591 moves it to the always-on
**dellserver** and makes every other box a thin client. The DB is a single
SQLite file, so the "ETL" is a file copy + a parity check — do it during a quiet
window (no active dispatch):

```bash
# 1. Quiesce: stop driving the pipeline; let in-flight workers settle.
# 2. Copy the live DB to the daemon host. WAL-checkpoint first so the .db file
#    is self-contained (otherwise also copy coord.db-wal / coord.db-shm).
ssh elitebook '~/.coord-venv/bin/python -c "import sqlite3;c=sqlite3.connect(\"$HOME/.coord/coord.db\");c.execute(\"PRAGMA wal_checkpoint(TRUNCATE)\");c.close()"'
scp elitebook:~/.coord/coord.db dellserver:~/.coord/coord.db
scp elitebook:~/.coord/coordinator.yml dellserver:~/.coord/coordinator.yml  # canonical home (was ~/coordinator.yml) — historical, see note above
# 3. Start the daemon on dellserver (service above), verify /board parity:
#    round_number + assignment count match elitebook's `coord status`.
# 4. Flip every machine (incl. elitebook) to a thin client: write client.toml
#    pointing at dellserver:7435. REMOVE elitebook's client.toml only if it is
#    no longer the daemon host. If dellserver is the sole daemon, elitebook is
#    a client and DOES get a client.toml.
# 5. Verify each machine: `coord status` and `coord-tui` show the dellserver board.
# 6. Retire the per-host DBs only AFTER parity is confirmed (rename, don't rm,
#    until you've lived on the daemon for a bit): mv ~/.coord/coord.db ~/.coord/coord.db.retired
```

Parity check = the daemon's `/board` `round_number` and assignment count equal
the source's `coord status` before the flip. Keep the elitebook DB renamed (not
deleted) until the daemon has run clean for a day.

### Restart / logs

```bash
systemctl --user restart coord-serve
journalctl --user -u coord-serve -f
```

## Web dashboard (`coord web`, #700/#703)

`coord web` serves the board dashboard + the React/Vite **phone PWA** on port
7434. It is the **third always-on user service**, after `coord agent` (every
worker box) and `coord serve` (the DB host). Like the daemon it belongs on
**dellserver only**: `coord web` reads the **local** `~/.coord/coord.db`
directly (`state.load_board`/`build_board`) — it does **not** route through the
`coord serve` daemon — so it must run on the box that owns the DB. On a thin
client it renders an empty board. (Routing it through the daemon so it can run
anywhere, like the CLI/TUI clients, is tracked in **#749**.)

The deployment picture, end to end:

| Service | Port | Host | Unit |
|---|---|---|---|
| `coord agent` | 7433 | every worker machine | `coord-agent.service` (via `install-agent.sh`) |
| `coord serve` | 7435 | dellserver only (owns the DB) | `deploy/coord-serve.service` |
| `coord web` | 7434 | dellserver only (reads the local DB) | `deploy/coord-web.service` |
| `coord notify` (periodic) | n/a (CLI, not a listener) | dellserver only (owns the DB) | `deploy/coord-notify.service` + `deploy/coord-notify.timer` |
| `coord drive-queue tick` (periodic) | n/a (CLI, not a listener) | dellserver only (tmux + repo checkouts) | `deploy/coord-drive-queue.service` + `deploy/coord-drive-queue.timer` |
| `coord-web-dist-build` (periodic, #1543, health-check #1560) | n/a (CLI, not a listener) | dellserver only (feeds `coord web --dist`) | `deploy/coord-web-dist-build.service` + `deploy/coord-web-dist-build.timer` |
| `coord-web-rollback` (on-demand, #1560) | n/a (CLI, run manually to recover) | dellserver only (repoints `coord web --dist`) | `deploy/coord-web-rollback.sh` (no unit — one-shot recovery command) |

The phone is just a browser: open `http://<dellserver-host>:7434` on the tailnet
and Add to Home Screen (the API is same-origin, so no client config).

**Build, run, and phone access:** see [`docs/PHONE_WEBAPP.md`](PHONE_WEBAPP.md).

**#1543 — merged main goes live automatically, decoupled from `~/.coord-venv`.**
`coord-web`, `coord-agent`, and `coord-serve` all `ExecStart` from the SAME
`~/.coord-venv` (see the table above), so upgrading that venv to ship a
webapp change would also upgrade the board daemon and the agent runtime on
that host — and `coord agent update` is already known to kill running
headless workers. As of 2026-08-04, `coord-web.service` runs
`coord web --dist ~/coord-web-dist`, and
[`deploy/coord-web-dist-build.timer`](../deploy/coord-web-dist-build.timer)
rebuilds that directory from `origin/main` every minute in a dedicated
worktree, then atomically repoints the symlink — no `pip install`, no PyPI
release, and (past the very first build) no restart of `coord-web` at all.
Shipping a webapp change this way **never touches the shared venv** — see
"Going live automatically (#1543)" in `docs/PHONE_WEBAPP.md` for the
before/after version proof and the full mechanism.
This corrects an earlier (2026-08-03 and prior) revision of this doc, which
described `~/.coord-venv`'s bundled wheel (#758) as the way a webapp change
reached production; #758 still ships that bundle and it is still the
fallback when `~/coord-web-dist` is absent, but it is no longer the primary
path on dellserver.

**#1560 — a release is health-checked before it ever goes live, and a bad
one is one command away from being un-done.** Before repointing the
symlink, the build timer boots the candidate release as a scratch, loopback-
only `coord web` instance (`--fixture`-backed, so it touches no real DB) and
probes it like a browser would; a release that fails is deleted, never
published. If something still gets through anyway, `~/.local/bin/coord-web-rollback.sh`
repoints `~/coord-web-dist` at the last known GOOD release in one command,
in milliseconds, with no restart. See "Health-check before cutover (#1560)"
and "Rollback: one command, reachable without this issue in hand (#1560)" in
`docs/PHONE_WEBAPP.md` for the full design rationale and a timed recovery
drill transcript.

**The rollback alone is not durable against the 1-minute build timer** —
fixing a bad commit on `main` realistically takes longer than that, so
without a guard the timer's very next tick would rebuild and silently
republish the exact SHA just rolled back from. `coord-web-rollback.sh`
writes a sentinel (`~/.coord-web-releases/.rollback-blocked-sha`) naming
that SHA, and `coord-web-dist-build.sh` refuses to build/publish it again
until `main` moves past it — see "The rollback is not durable against the
1-minute build timer on its own" in `docs/PHONE_WEBAPP.md`. If you need the
timer to simply stop running while you fix `main`, pause it directly:

```bash
systemctl --user stop coord-web-dist-build.timer
systemctl --user start coord-web-dist-build.timer   # resume once main is fixed
```

**Service unit:** [`deploy/coord-web.service`](../deploy/coord-web.service) (full
unit + prereqs in its header). Install + restart:

```bash
cp deploy/coord-web.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now coord-web
# restart over SSH needs the runtime-dir prefix (same #404 caveat as the agent):
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-web
```

**Dist-build unit** (the #1543 timer above):
[`deploy/coord-web-dist-build.service`](../deploy/coord-web-dist-build.service) +
[`deploy/coord-web-dist-build.timer`](../deploy/coord-web-dist-build.timer). Install:

```bash
cp deploy/coord-web-dist-build.sh deploy/coord-web-rollback.sh ~/.local/bin/
chmod +x ~/.local/bin/coord-web-dist-build.sh ~/.local/bin/coord-web-rollback.sh
~/.local/bin/coord-web-dist-build.sh   # first build — do this BEFORE (re)starting coord-web
cp deploy/coord-web-dist-build.service deploy/coord-web-dist-build.timer ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now coord-web-dist-build.timer
```

**Recovery, from anywhere with ssh (a phone included):**

```bash
ssh <dellserver-tailnet-name> ~/.local/bin/coord-web-rollback.sh
```

The script itself prints a `WARNING:` naming the sentinel it just wrote and
the pause command above — read it before you set the phone down.

## Periodic `coord notify` (`coord-notify` timer, #1311)

`coord serve` (above) fronts board **state**; it deliberately does not drive the
pipeline — its own passive tick (`_lifespan`/`_tick_loop`) only flips a
finished/crashed assignment's status, with **no dispatch and no GitHub write**
(a load-bearing invariant: it must never be able to re-introduce the #476/#477
dispatch flood). The command that actually posts completion/failure comments
and triggers the auto-loop (review-on-completion, fix-on-request-changes,
re-review-on-fix-completion) is `coord notify`, and it has to be **fired
periodically** for that to happen at all.

A full (non-thin-client) `coord`/`coord-tui` on the daemon host itself gets this
for free — the TUI auto-fires `coord notify` every 30s while something's
running. But a **thin client** (any machine with `~/.coord/client.toml` pointing
at `board_service`) deliberately suppresses that auto-fire
(`is_remote_board_service()`, `tui/src/app/data.rs`) — it must not shell out
`coord notify` locally against the wrong/absent DB. Net effect without this
timer: a thin-client-dispatched worker can finish or crash and just sit there,
unnoticed, until a human happens to run `coord notify` by hand.

**The fix is a systemd user timer on the daemon host** — the same box that runs
`coord serve`/`coord web` (dellserver in production) — firing `coord notify`
every few minutes. `coord notify`'s own daemon re-route (#906,
`daemon_reroute_target`) means this is safe to run from *any* client and it'll
find the right DB either way, but running it locally on the daemon host (no
`client.toml` there) is simplest: it just executes directly against the local
`coord.db`, no HTTP hop.

**THIS TIMER IS THE SANCTIONED SINGLE DRIVER for thin-client setups.** Do not
also add a hand-rolled `while`/`watch` loop calling `coord notify` alongside it,
and do not re-enable the TUI's thin-client suppression to "help" — two drivers
racing each other is exactly the 2026-06-07 incident (a `request-changes`
verdict got auto-bounced into redundant fix-2/fix-3 workers because two loops
called `notify` concurrently). See `docs/ARCHITECTURE.md`'s "no orchestration
daemon" section for the full incident writeup and the #476 (iteration cap) /
#477 ("TUI owns the loop") fixes that came out of it.

Install (same host as `coord-serve`/`coord-web`):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/coord-notify.service deploy/coord-notify.timer ~/.config/systemd/user/
loginctl enable-linger "$USER"          # survive logout / reboot
systemctl --user daemon-reload
systemctl --user enable --now coord-notify.timer
```

Verify / logs:

```bash
systemctl --user list-timers coord-notify.timer
journalctl --user -u coord-notify.service -f
```

**Known residual gap (not closed by this timer):** the auto-loop's
interactive-awareness is narrow — it skips a headless review/re-review over
work whose *own* completion was interactive (`provider_name == "claude-pty"`),
but nothing checks "is there a live, human-attended `--interactive`/`--chat`/
`--troubleshoot`/`--fix-of`/`--merge-of` pane open on this same issue right now"
before this timer's `coord notify` fires an auto-fix. This is the class of race
#602 fixed narrowly for the TUI's own auto-offer popup, not for `coord notify`'s
auto-loop dispatch path. Low risk today (flat headless-only dispatch has no
`--interactive` involved), and deliberately **deferred as a fast-follow** rather
than blocking this timer — flag it if it ever bites (a fix worker firing while
someone is mid-`--fix-of` on the same issue).

**Phantom-row self-heal rides the same timer (#2536).** Every `coord notify`
pass this timer fires also sweeps the board for a `status='running'` row
whose recorded machine's own `/status` shows nothing active for it — a
session that finished or died without the board ever finding out. Left
alone, that phantom row sits forever (nothing else notices) and — per
`coord/drive_queue.py`'s own capacity comment — holds its repo's *entire*
drive-queue concurrency slot, blocking every other queued issue in that
repo. The sweep only ever acts on a **confirmed**-dead session (never an
"unknown" read — an unresolvable machine, an unreachable agent, a probe
error all count as "leave it alone", same as "live"), and only once the row
is aged well past its own `needs_attention` wall-clock threshold plus a
buffer, so it can never race a session that's merely idle between turns.
When both hold, it runs the exact same non-destructive recovery a human
would via `coord diagnose <repo> <issue> --reset` — branch and commits are
always preserved, the stage becomes re-dispatchable — and posts a GitHub
comment recording that it happened (`coord.diagnose.sweep_dead_running_rows`,
`coord.notify._sweep_phantom_rows`). Governed by
`pipeline.auto_heal_phantom_rows` in `coordinator.yml` (**default `true`**,
unlike most other auto-dispatch flags in this file — see
`PipelineConfig`'s docstring in `coord/config.py` for why); set it `false`
to go back to requiring a human to run `--reset` by hand.

**#2570: this sweep also runs from inside `coord-serve` itself, independent
of this timer.** On 2026-08-22, `coord-notify.timer` and
`coord-drive-queue.service` both `ExecStart` from `~/.coord-venv` and both
died with `ModuleNotFoundError` for 11 hours when that venv broke — the
phantom-row heal, whose whole job is recovering a stuck queue, shared a
failure domain with the queue itself and bought nothing. `coord serve` is
`Type=simple`, not a re-exec'd oneshot: once running, it keeps
`coord.notify`/`coord.diagnose` already imported in its own interpreter, so
its own tick loop (`_phantom_heal_loop` in `coord/serve_app.py`, default
cadence 300s — `COORD_PHANTOM_HEAL_INTERVAL`, `0` disables) keeps calling
the identical sweep (`_phantom_heal_tick`) for as long as the daemon process
itself stays up, even after `~/.coord-venv` breaks under it — exactly the
property that let `coord-serve` keep serving `/board`/`/status` through the
entire 11h outage while both venv-dependent units were down. This does not
replace `coord-notify.timer` (still the sanctioned driver for completion/
failure/review notifications) — it just gives the phantom-row heal
specifically a second, independent path.

## Periodic `coord drive-queue tick` (`coord-drive-queue` timer, #1756)

Same shape as the `coord-notify` timer above — a `Type=oneshot` unit fired on
a cadence by its own timer — but for a different job: draining the operator-
declared `coord drive-queue` work queue (#1750) instead of the pipeline's
completion/failure notifications. Install on the same host (dellserver): the
tick subprocess-launches `coord drive --tmux`, which needs a local tmux
server and the repo checkouts under `SRC_ROOT`, so unlike `coord notify` it
does **not** have a daemon re-route that makes it safe to run from anywhere.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/coord-drive-queue.service deploy/coord-drive-queue.timer \
    ~/.config/systemd/user/
loginctl enable-linger "$USER"          # survive logout / reboot
systemctl --user daemon-reload
systemctl --user enable --now coord-drive-queue.timer
```

Verify / logs:

```bash
systemctl --user list-timers coord-drive-queue.timer
journalctl --user -u coord-drive-queue.service -f
```

`Type=oneshot` plus the tick's own flock means a slow tick cannot stack — a
timer fire while the previous tick is still running exits 0 without touching
the queue. Full runbook, including the enqueue commands, the `QUEUE: STALLED`
vs `QUEUE: BLOCKED` status-bar reading, the pinned-CLI upgrade trap, and the
`#1715` "don't queue more than ~2" caveat: [`docs/DRIVE_QUEUE.md`](DRIVE_QUEUE.md).

### Recording manual recovery (`coord drive-queue log-intervention`, #2540)

`coord drive-queue block-log`'s `human_acted` count only ever recognizes the
queue's own command surface — `remove`/`resume`, a Gate-A sign-off. It has no
way to see a manual git rebase / conflict resolution / `git push
--force-with-lease`, a direct `coord test`/`coord merge --only`/`coord
pr`/`coord fix` run against a specific assignment, or infra-level recovery
(`systemctl`, `coord agent update`, `coord diagnose --reset`) — none of that
touches this process's own write paths. Left unlogged, a night of exactly
that kind of manual recovery reads back as `0 needed a human`, which is the
opposite of what the log is for.

**Make it a habit**: whenever you do one of those by hand against a
`blocked`/`parked` drive-queue entry, log it —

```bash
coord drive-queue log-intervention <repo> <issue> --category git-recovery \
    --note "resolved conflict by hand, force-pushed"
```

`--category` is free text (not a closed enum — see
`INTERVENTION_CATEGORIES` in `coord/block_log.py`), but the documented
starting set is `git-recovery`, `cli-recheck` (a direct `coord test`/`coord
merge --only`/`coord pr`/`coord fix`), and `infra`. It is safe to run at any
point relative to the actual fix landing — mid-recovery or afterward —
`block-log` folds the record onto whichever episode was open at the time, or
the most recently closed one otherwise. Skipping it breaks nothing
mechanically, but it is exactly how real operator effort goes uncounted —
and it stays a manual step: nothing here infers intervention automatically,
so a recovery session where every command *except* `log-intervention` gets
run still reports `0 needed a human`, same as before #2540.

**Run it on the host that recorded the stall.** The block log is per-host by
design (`coord/block_log.py`'s own docstring) and `log-intervention` inherits
that — it only ever reads/writes *this* host's log. Run it from your laptop
against a stall the daemon host actually recorded, and it will print "no
recorded stall on this host's block log yet", write the record to a log file
`coord drive-queue block-log` on the daemon host will never see, and the
intervention will never attach to the episode it was about. In practice that
means: run it on the daemon host (dellserver), the same place
`coord-drive-queue.timer` runs the tick that recorded the stall in the first
place — not wherever you happen to be running `git`/`coord test`/etc. by
hand.

As of #2540, the command also refuses to lie about a write it cannot
confirm: if the append itself fails (full disk, read-only `$HOME`) or the
record can't be read back immediately after, it exits non-zero with a
`failed to log intervention` message instead of printing `logged ...` —
`unconfirmed success` is a bug (epic #2096), and this is exactly the tool
that exists to give operators a durable, trustworthy paper trail.

## Fleet watchdog (`coord-fleet-watchdog`, #2580)

The 2026-08-22 outage (#2569 root cause, #2570 blast radius, #2572
escalation) took the fleet down for 11h because `~/.coord-venv` became an
editable install pointing at a deleted worktree — and every existing
recovery path (the tick, `coord notify`, `coord notifier`) execs from that
exact venv, so all of them died with it. `coord-fleet-watchdog` is a small,
**stdlib-only** repair loop that runs under `/usr/bin/python3` — never
`~/.coord-venv/bin/python` — specifically so it survives the failure class
that took everything else down. It never `import coord`s (a grep test in
`tests/test_fleet_watchdog.py` enforces that) and never parses
`coordinator.yml`.

**Two cadences.** An hourly sweep (`coord-fleet-watchdog.timer`) is the broad
net; `OnFailure=coord-fleet-watchdog.service` on `coord-drive-queue.service`
and `coord-notify.service` (alongside the existing `coord-failure-notify.
service` escalation from #2572) is the tripwire — those units fail every
~3–5 minutes on a genuinely broken fleet, so the tripwire turns "up to 60
minutes of dead fleet" into near-immediate repair.

**The watchdog's own failures page too.** `coord-fleet-watchdog.service`
itself carries `OnFailure=coord-failure-notify.service` — `main()` returns
exit 1 on any unsuppressed Tier-2 finding or rate-limit escalation, which is
a `failed` unit like any other, and #2580's entire premise is that nothing
should depend on an operator thinking to run `systemctl --user status`
unprompted. No cycle: `coord-failure-notify.service` carries no `OnFailure=`
of its own.

**Tier 1 (auto-repaired, precondition re-checked at repair time):** a broken
`~/.coord-venv` with a healthy blue/green sibling slot (rolled back via
`scripts/coord-venv-rollback.sh` — bash, deliberately, so the repair itself
never depends on `coord` being importable — mirrors
`deploy/coord-web-rollback.sh`), `~/.local/bin/coord` no longer symlinked
into the venv (#2314's exact damage), a `coord-*` unit stuck `failed`
(`reset-failed` + restart), a stale `.git/index.lock` with no live holder
(#2206), an expired release cordon still present in
`~/.coord/paused_machines.json`, and an orphaned worktree under
`~/.coord/worktrees` with no live board assignment (deliberately the most
conservative of the six — only ever acts on a *positive* "not live"
confirmation, never on "couldn't check," because a reaped worktree is what
detonated the editable install in the first place).

**Tier 2 (detect and escalate, never repair):** a `coord-*.timer` that is
disabled or masked. This is the one that bites: `coord-release-propagate.
timer` currently reads disabled **on purpose** (manual release rolls until
the release lane stabilises — see "Release rolls stay manual" in the
project's memory / `docs/DRIVE_QUEUE.md`), and a watchdog that "fixed" that
would silently drag the fleet back onto an abandoned lane within the hour.
Version drift, graph/unit-drift, and phantom `running` board rows are
intentionally **not implemented** — see the comment in `scripts/
fleet_watchdog.py` next to `TIER2_CHECKS` for why each is harder than it
looks (an active-assignment check that headless workers need but `coord
sessions --remote` can't see; "review, then pull — not automatic"; a naive
reaper on the wrong host killing a healthy drive it doesn't own).

**The intent sentinel** (`~/.coord/watchdog-suppress.json`, plain JSON —
same shape as `release_cordons` in `paused_machines.json` and
`coord-web-rollback.sh`'s `.rollback-blocked-sha`) is how an operator tells
the watchdog "this is deliberate, not broken":

```json
{ "coord-release-propagate.timer": { "reason": "manual rolls until release lane stabilises",
                                     "set": "2026-08-21", "expires": null } }
```

Anything not covered by a sentinel is reported, never fixed — default to
reporting. Keys are matched against a condition's specific instance (a bare
unit name, a specific worktree's assignment id, `venv-rollback`,
`local-bin-symlink`), not the general category.

**Rate limiting** (`~/.coord/watchdog-state.json`) stops the watchdog from
repairing the identical condition more than 3 runs in a row (`--rate-limit`,
default 3) — past that it escalates instead. Patching a recurring fault
forever is how a root cause survives; this is the exact #2314 → #2569
pattern the watchdog exists to not repeat.

**Install** (every machine that execs `coord`/`coord-agent` out of
`~/.coord-venv` — not just the daemon host):

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
cp scripts/fleet_watchdog.py scripts/coord-venv-rollback.sh ~/.local/bin/
chmod +x ~/.local/bin/fleet_watchdog.py ~/.local/bin/coord-venv-rollback.sh
cp deploy/coord-fleet-watchdog.service deploy/coord-fleet-watchdog.timer ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now coord-fleet-watchdog.timer
```

On any machine that isn't the board-daemon host itself, also set
`~/.config/coord/fleet-watchdog.env` (`COORD_BOARD_URL=http://<daemon-host>:7435`)
— see `deploy/coord-fleet-watchdog.service`'s header for what degrades
without it.

**Deliberately not (yet) in the "Daemon-host unit inventory" table below or
`coord/deploy_manifest.py`'s `ROLE_UNITS`.** Unlike every unit in that table,
this one belongs on *every* agent host (worker and daemon alike) the same
way `coord-agent.service` does, not on the daemon host alone — folding it in
cleanly is left as a follow-up rather than done as a side effect of #2580.
`tests/test_deploy_manifest.py` only asserts every `ROLE_UNITS` entry ships a
real file, not the reverse, so this is a safe, intentional gap, not drift.

## `coord health --fix` (#2581)

`coord health` has always printed a remedy command per failing check; `--fix`
applies it — but only for an **explicit allow-list**, opted in per check via
`fix=` at registration (`coord/health/registry.py`'s `Check.fix` /
`check(..., fix=...)`), never inferred from "has a `detail` string". Today
that's `index_lock`, `worktrees`, `cargo_targets`, and `graph` (each check's
own module carries its `fix_*` function next to the probe it repairs — see
`coord/health/checks/{index_lock,worktrees,cargo_targets,graph}.py`).
Everything else — `agent_version`, `timer_active`/`unit_enablement`,
`unit_drift`, `coord-web ci pin`, a base checkout's own `git pull` — keeps
printing its remedy for a human, exactly as before `--fix` existed; see the
issue for why each of those specifically stays manual.

**Why this isn't just `scripts/fleet_watchdog.py` (#2580).** That script is
deliberately confined to the handful of bootstrap failures that can't depend
on `coord` importing (a broken `~/.coord-venv`, `~/.local/bin/coord` unlinked
from it) — everything else belongs inside `coord`, next to the check that
detects it, so there is exactly one implementation of each remedy rather than
two that drift. In practice `scripts/fleet_watchdog.py`'s Tier 1 already also
auto-repairs a stale `.git/index.lock`, an expired release cordon, and an
orphaned worktree (conditions that predate this split and haven't been pulled
out of the watchdog yet) — `index_lock` and `worktrees` here are the
`coord`-native equivalents, deliberately keyed the same way (see below) so
the two surfaces never disagree about what's suppressed.

**Same intent sentinel as #2580, same file.** `~/.coord/watchdog-suppress.json`
— a fixer never applies a suppressed finding, only reports it. Suppression is
checked at two granularities: the whole `CheckResult` row (`"graph"` or
`"graph:claude-coordinator"`, either suppresses that finding), and, for a
check whose one row can bundle several independently-repairable items
(`index_lock`'s several stale locks, `worktrees`'s several stale worktrees),
per item. `worktrees` checks the exact same key pair
`scripts/fleet_watchdog.py` uses for the identical condition
(`<assignment_id>`, `orphaned-worktree:<assignment_id>`), so one sentinel
entry covers both tools there. `index_lock` checks `stale-git-lock:<path>`
plus the lock's bare directory name — a strict superset of
`fleet_watchdog.py`'s own key for that condition (just
`stale-git-lock:<path>`, since its `check_stale_git_lock` finding doesn't
set `suppress_keys` and so defaults to its signature alone). The extra key
only *widens* what a `fleet_watchdog`-authored sentinel entry can suppress
here, never narrows it, so a `stale-git-lock:<path>` entry from either tool
still covers both — it's just not, strictly, "the exact same keys" for this
one check.

**Idempotent by construction.** `apply_fixes` skips every `OK` row outright —
fix a finding once, the next `coord health` reports it `OK`, and a second
`--fix` never even calls that check's fixer. Each fixer also re-verifies its
own precondition against live state before acting (mirroring
`scripts/fleet_watchdog.py`'s Tier-1 re-check), so calling one directly twice
in a row with the same stale finding is still safe — the second call finds
nothing left to do rather than erroring on an already-repaired condition.

```bash
coord health --fix           # apply allow-listed remedies, report the rest
coord health --fix --json    # same, plus a `"fixes"` array in the JSON contract
coord health                 # re-run afterward to confirm the new state — `--fix` does not re-check
```

`worktrees`' fixer is worth calling out: the *probe* is deliberately mtime-only
(cheap, no board, no network — see the check's own module docstring), but the
*fixer* is not under that constraint. It re-derives the precise
board/tmux-confirmed answer `coord diagnose --orphan-worktrees` already
computes and only ever removes a worktree that sweep positively confirms is
orphaned; anything the mtime probe flagged that the precise sweep can't
confirm (no local checkout to check against, an unreachable board, a live
assignment) is left alone and reported `no_action`, never guessed at.
`cargo_targets`' fixer is scoped to the *shared* cache
(`~/.coord/cargo-target/<repo>`) only — the same thing already GC'd
automatically after every worktree clean — and never touches a per-checkout
`target/` a human may be building in.

## Daemon-host unit inventory — what dellserver runs

**If the daemon host is lost, this is the list you rebuild from.** Which units
a host runs is not derivable from the wheel: `deploy/` ships *all* of them and
`coord release verify` deliberately refuses to infer intent from that
(*"a release does not decide which services a host runs"* — it reports
`10 packaged unit(s) NOT installed here` on workers and correctly does nothing
about it). So the mapping lives here — and, since #2098, also in
[`coord/deploy_manifest.py`](../coord/deploy_manifest.py) as
`ROLE_UNITS`, which is what this table below is transcribed from. Keep the
two in sync; `tests/test_deploy_manifest.py` cross-checks it.

| Unit | Role | Enable step |
|---|---|---|
| `coord-agent.service` | every machine | `install-agent.sh` (the only one it enables) |
| `coord-serve.service` | daemon host | see "Board daemon" above |
| `coord-web.service` | daemon host | see "Phone/web dashboard" above |
| `coord-web-dist-build.timer` | daemon host | see "Web bundle rebuild" above |
| `coord-notify.timer` | daemon host | see "Periodic `coord notify`" above |
| `coord-drive-queue.timer` | daemon host | see "Periodic `coord drive-queue tick`" above |
| `coord-release-propagate.timer` | daemon host | **below** |
| `coord-db-backup.timer` | daemon host | **below** |
| `coord-backup.timer` | daemon host | `deploy/coord-backup.service` header (#3118) |
| `coord-dr-verify.timer` | daemon host | `deploy/coord-dr-verify.service` header (#3119) |

Workers (precision, elitebook) run `coord-agent` **only**.

**`coord-db-backup.timer` and `coord-backup.timer` are two different lanes,
not a duplicate.** The first snapshots `coord.db` onto the USB SSD in
dellserver's own chassis — fast to restore from, useless if the machine is
lost. The second (#3118, closing #1822) pushes a *verified* snapshot **off the
machine** into Azure Blob via restic, with content-chunked dedup so an hourly
push of the 720 MB database transfers the delta rather than the database.
Keep both. `coord-backup` additionally needs `restic` installed and a
mode-0600 `~/.coord/backup.env` holding `COORD_BACKUP_REPOSITORY` and the
repository password — credentials never live in `coordinator.yml` and are
never passed on the command line.

**`coord-dr-verify.timer` is the third lane, and the only one that proves
*recovery* (#3119).** Every 6 h it pulls the newest off-site snapshot back
down through `coord backup restore --into <scratch>` — the real off-site path,
credentials included — and runs four checks against it: `integrity_check`,
per-table row counts against live (a table that is *empty* in the restore
while live is not fails regardless of tolerance), the restored
`schema_version` against what the installed `coord` expects, and a throwaway
`coord serve` booted on an ephemeral port answering `GET /board`. Success is
quiet; failure alerts through the notifier. Each run writes
`~/.coord/last_verify.json` — and **a verify that has not run in 12 h is
itself a failure**: `coord dr status` reports that and exits non-zero without
needing a run to fail. Set `COORD_DR_VERIFY_MIRROR` to a path another machine
can read so that machine can run `coord dr status --record <mirror>` when this
host is the thing that died — an alert originating on the dead host is not an
alert (2026-08-22). The restore duration in `last_verify.json` is #3117's
Domain-A RTO input; read it there rather than estimating it.

**Checkable, not just prose (#2098).** `coord doctor` / `coord health` run
`unit_enablement` (`coord/health/checks/unit_enablement.py`) on every
machine: for each unit in `ROLE_UNITS` that this host has already installed,
it runs `systemctl --user is-enabled` and WARNs if the answer isn't
`enabled` — the exact state that hid the propagate timer, where an installed
unit and a running one produced identical evidence. It does not guess
whether a host *should* install a unit it lacks; that half stays a human
decision, same boundary `unit_drift` and `coord release verify` already
draw. `coord doctor`'s printed report names the unit and the fix
(`_unit_enablement_lines` in `coord/commands/status.py`, mirroring the
`unit_drift` renderer already there) — not just the aggregate per-machine
`severity` that rolls into the "FLEET: WARN" footer / coord-tui indicator;
`coord health` also prints full per-unit detail.

## Fleet version propagation (`coord-release-propagate` timer, #1835/PKG-7)

Publishing a release touches no running host; *propagating* it restarts every
agent, and a restart kills every in-flight headless worker. So publish is a
GitHub Action and propagation is a quiescence-scheduled timer on the daemon
host. It resolves PyPI's latest, rolls the lanes it can reach, runs
`coord release verify`, rolls back on red, and releases drive-queue deploy
gates waiting on that deploy.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/coord-release-propagate.service deploy/coord-release-propagate.timer \
    ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now coord-release-propagate.timer
```

**This step was missing from this document until 2026-08-10, and the timer was
never enabled as a result.** The fleet ran 11 releases behind (0.5.15 vs
0.5.26) for a day with every readout looking normal, because a disabled timer
and a deferring timer produce identical evidence: nothing. Check
`systemctl --user is-enabled coord-release-propagate.timer` before believing
the fleet is current, and `coord release verify` for the truth per lane.

**It may not get a window on its own.** Quiescence is per-host (#2067) — a busy
host defers only itself — but there is one fleet-wide exception: if the
**daemon host** is busy and not already on the target version, the whole run
defers, since no host's python lane may roll ahead of an unrolled daemon (the
documented 405). Because dellserver is both the daemon host *and* a work
machine, any drive running there blocks the entire roll. The drive queue
relaunches on a 3-minute tick and propagation retries every 20, so the roll
waits on a short inter-drive gap coinciding with a propagate tick — which
happens, but can take hours.

**Do not force the window by stopping `coord-drive-queue.timer` by hand.**
That used to be the documented fix here, and on 2026-08-22 an attended manual
roll run through its wrapper (`coord release nightly-window`) sat for the
full 60-minute deadline with the timer stopped, drained nothing, and rolled
nothing — because a continuously-busy queue essentially never reaches
fleet-wide quiescence, and stopping the timer also stops the reconciliation
that would let the queue's true state ever be seen. See "Roll at the next
inter-drive gap (`coord-release-window`, #2112/#2587)" below for the
mechanism that replaced it: `coord release propagate`'s own daemon-busy
deferral, and `coord release nightly-window`, now both just set a
**roll-pending marker** and return immediately — `coord drive-queue tick`
fires the actual roll itself, the instant it notices the queue's own next
inter-drive gap, never stopping any timer to get there.

**#2124 (fixed 2026-08-13):** step 3 used to undo step 1. `coord release
propagate`'s own `deploy/**` lane (`POST /deploy-units`) asserts every
installed timer is enabled (#2082 — a *different* bug: a timer nobody ever
ran `systemctl --user enable` on at all). #2082's fix was `enable --now`,
and `--now` **starts** a timer unconditionally — so step 3 restarted the
timer step 1 had just stopped, on dellserver first (the daemon-leads
invariant), while precision and elitebook were still mid-roll: the exact
gap this sequence exists to hold open. `enable` and `--now` are idempotent,
so this was invisible for every roll where nobody had deliberately stopped
a timer, and only bit the one sequence documented above.

Fixed: `coord.deploy_units.enable_timers` now asks systemd whether a timer
is already enabled *before* deciding what to do. `systemctl --user stop`
never changes a unit's persistent enablement, only its current run state —
so a timer already `enabled` is left exactly as it is, whatever its run
state, and only a timer that was never enabled at all still gets `enable
--now`. Reaching for `systemctl --user mask` instead of `stop` is no longer
necessary; the sequence above is safe to run literally as written. The
`deploy/**` lane's own output now names which timers it started and which
it left alone (`coord release propagate`'s per-lane line for this host), so
"did step 3 leave my stopped timer alone" is directly checkable in the run
output rather than something to reconstruct from journal timestamps.

**#2110 (fixed 2026-08-11):** stopping the timer used to deadlock this
sequence. The reconciler that moves a completed entry from `running` to
`done` runs inside `coord drive-queue tick`; with the timer stopped, no tick
ran, so the last drive's row stayed `running` forever, and because every
drive-queue entry charges the timer host via `launch_host`, that stale row
kept dellserver marked busy and `coord release propagate` deferred
indefinitely — on a drive that had ended hours ago, with every machine idle.
Observed 2026-08-10: `#2085` merged at 01:57Z; its row still read `running`
an hour later with zero live assignments, zero `claude -p` processes and no
drive tmux session anywhere. The only escape was `--force`, which is supposed
to mean *"kill in-flight headless workers"* — overloading it to mean *"ignore
a lying row"* is how an operator learns to distrust its warning.

Two independent fixes closed the trap, and normal operation no longer needs
either workaround:

* **`coord drive-queue tick --reconcile-only`** (equivalently, `--max-parallel
  0`) is the missing primitive named above: it reconciles every `running`
  entry against the board exactly as a normal tick would — a finished one
  moves to `done`, a permanently-refused one to `blocked`, a CI-pending one
  parks — and then stops. No capacity walk, no queue-level alert, no launch.
  Safe to run by hand with the timer stopped, which is what the sequence
  above now does before calling `propagate`.
* **`coord release propagate` itself no longer trusts a `running` row on
  faith.** `assess_quiescence` re-derives the same disproof the tick uses
  (the entry's own issue merged or closed) against the board it already read
  for this run, so even a row nobody drained is excluded from `busy` — it
  cannot be in flight if its issue has landed. A run that ignores a stale row
  this way prints a `note: ignoring stale drive-queue row(s)...` line and
  records it under `quiescence.stale` in the journal, so a self-corrected row
  stays visible instead of just quietly not blocking.

`--force` is back to meaning only what it always meant — kill in-flight
workers — and should not be needed for this sequence anymore.

**#2240 (fixed 2026-08-14): the cordon deadlocked against the review it was
waiting for.** The same family, one layer up, and the first one that took the
*whole fleet* down: on 2026-08-14 the fleet was cordoned and unable to
dispatch for 70 minutes with all three machines reading `online • idle`.

1. `propagate` cordons all three hosts to drain them for v0.5.77;
2. a cordoned host cannot accept new dispatch — **including a review**;
3. an entry that had finished Work and Test was waiting for its review;
   `coord review <aid>` answered `no eligible reviewer machine configured`;
4. so its queue row stayed `running` with no live assignment (between legs),
   which nothing could attribute to a host;
5. an unattributable busy signal blocks every host, so the roll deferred;
6. **a deferred run leaves the cordon in place** → back to (2).

Four consecutive runs, 21 minutes apart, each cordoning all three hosts and
uncordoning none. The `--ttl` safety net could not fire: the propagate timer
renews every 20 minutes, which is shorter than any sane TTL. The only exit
was `coord release cordon --clear --all` by hand, after which the identical
`coord review` dispatched immediately.

Three fixes, and the first is the one that guarantees no unattended repeat:

* **a deferred roll no longer holds a cordon indefinitely.** After
  `--cordon-max-deferrals` consecutive deferrals for one target (default 2,
  i.e. ~40 minutes) `propagate` clears every cordon it set, says so loudly
  (`CORDON RELEASED (#2240): ...`), and does not cordon again for
  `--cordon-cooldown` seconds (default 1800) so the fleet gets a real window
  to finish the work the drain is waiting for. The counter lives in
  `~/.coord/release_propagation.jsonl`, because the process holding it is
  restarted by the very roll it gates.
* **a cordon no longer blocks the completion of work already in flight.** A
  review/smoke/fix dispatch for a running entry is the *tail* of the work the
  cordon is waiting to drain, not new work, so it routes onto a cordoned host
  (`machine_pause.follow_on_paused_set()`). An explicit `coord pause` and a
  quiet-hours window still block it — those are decisions about the machine.
* **a between-legs row is charged to its last known host**, so it holds one
  host instead of the fleet.

**What this looks like now:** `coord status` and `coord release cordon`
append `(deferred N runs — NOT DRAINING)` to a cordon that has stopped
producing windows, so a 70-minute stall no longer renders identically to a
30-second drain. If you see it, you do not need to do anything — the next
`propagate` releases it — but it is the signal that something downstream is
wedged, and the propagation journal names the entry.

A queued entry carrying `--hold-after` (#1757) also creates the gap by design:
the gate's dependents stop themselves (the whole queue too, if the entry was
declared `--scope=fleet` — #2186), propagation sees a *fired* gate as the
opposite of busy, rolls, and releases the hold.

**#2373 (fixed 2026-08-18): a launch-host-only liveness ambiguity had no
machine assigned to resolve it.** `_reconcile_running`'s #1870 cross-host
guard (`coord/drive_queue.py`) is correct and load-bearing: a tick's tmux
read is always LOCAL, so a `running` entry launched on a *different* host
than the one ticking must read as `unknown`, never dead — declaring it dead
from the wrong machine is exactly the false-positive #1870 exists to
prevent. But only `coord-drive-queue.timer` on the daemon host ticks the
queue at all; a machine that only runs `coord-agent.service` (precision,
elitebook) never runs its own tick, so an entry launched there sits
`running`/`unknown` forever if it actually died — there is no periodic
process anywhere whose job is to resolve it. Confirmed live 2026-08-18
(claude-coordinator#2360): an entry launched on elitebook sat `running` for
~17h; every dellserver tick correctly refused to declare it dead; elitebook's
release-cordon (which #2101/#2136 renews on every tick that finds the host
"still busy") kept renewing past its drain deadline and re-escalated with
nothing ever resolving the ambiguity. Running `coord drive-queue tick
--reconcile-only` locally on elitebook resolved it in one call — the entry
moved to `parked` (waiting on a CI re-check, #1891), not actually dead.

The fix closes the loop inside the drain-deadline escalation itself, so it
needs no new timer unit anywhere: before `_apply_cordons` surfaces a
`DrainEscalation` (the loud `DRAIN OVERDUE: ...` message), it first `POST`s
to the escalated host's own agent — `POST /drive-queue-reconcile`
(`coord/agent_app.py`, handled by `AgentServer.reconcile_drive_queue` in
`coord/agent.py`) — which shells out to `coord drive-queue tick
--reconcile-only` *on that machine*, so the #1870 guard's own local-tmux-read
requirement is satisfied by construction: the host asked is the host whose
evidence actually counts. The outcome (`ok`/`detail`) is folded into the SAME
escalation message and escalation-table record rather than a second alert —
"DRAIN OVERDUE: elitebook has been cordoned for 91m ... — asked elitebook's
own agent to run a local reconcile-only tick first (#2373): ok (moved to
parked)". An unreachable agent does not suppress the escalation; the loud
message still goes out, with the failed self-heal attempt named rather than
hidden. Every machine capable of launching a drive already runs
`coord-agent.service`, so no fleet ever needs a new unit installed for this
to work; a machine with no local `coordinator.yml` (thin-client/config-free
mode) refuses the call cleanly (`no local coordinator.yml on this agent`)
since it has nothing to pass as `--config`. See
`tests/test_release_cordon_2101.py`'s `#2373` section for the seeded
end-to-end shape (a `running` entry pinned to a non-daemon host, cordoned
past the drain deadline, resolved without an operator SSH session).

### Roll at the next inter-drive gap (`coord-release-window`, #2112/#2587)

The daemon host is the one case a per-host quiescent window (above) cannot
reach on its own — cordoning does not help, because cordoning only stops NEW
work from *routing* to a host, and it is the daemon host's *own* drive-queue
tick that keeps launching work onto itself. #2112 built a separate mechanism
for exactly this case: `coord release nightly-window`, paired with
`coord-release-window.timer` (03:00 local nightly).

**2026-08-22 incident.** #2112's original mechanism stopped
`coord-drive-queue.timer`, polled a bounded drain (default one hour) for
whatever was already running to finish, rolled, and always restarted the
timer. An attended manual run (`coord release nightly-window`) sat for the
**full 60-minute deadline, drained nothing, rolled nothing** — with
`coord-drive-queue.timer`, and therefore all reconciliation and all new
dispatch, stopped the entire time. The insight: the tick launches at most
one drive per run, so the fleet genuinely goes idle **between** one drive
finishing and the next launching — several times an hour on a busy queue —
but the mechanism was waiting for a DIFFERENT, much rarer event
(fleet-wide idle for the whole drain window) that a continuously-refilling
queue essentially never produces.

**#2587 fix: invert the handoff.** Rather than a command polling for a
window with a deadline, the drive queue — which is the thing that *creates*
the busy state — announces the window it already knows about, every tick
(`coord-drive-queue.timer`, ~3 minutes in production), for free:

1. `coord release propagate` (when it hits the daemon-busy deferral above)
   and `coord release nightly-window` both just set a **roll-pending
   marker** — `~/.coord/roll_pending.json`
   (`coord.drive_queue.RollPending`, written via
   `coord.commands.drive_queue.write_roll_pending`) — naming the target
   version, and return immediately. **No timer is ever stopped.**
2. While the marker is live, `coord drive-queue tick` (`coord/commands/
   drive_queue.py`) behaves exactly as if `--reconcile-only` had been
   passed — reconciliation runs completely normally, launching nothing
   — and `coord notify` dispatches no NEW leg (smoke/review/auto-loop
   fix-or-re-review/stalled-pipeline action), though it keeps posting
   completion/stuck/needs-attention/liveness signals for work already in
   flight.
3. The instant a tick's OWN reconciliation empties the queue out
   (`TickPlan.occupied == 0` — no drive-queue row still holds a live tmux
   session or a live board assignment) — the inter-drive gap — that tick
   fires the roll: `systemctl --user start --no-block
   coord-release-window.service`. `--no-block` matters: the roll swaps the
   venv the tick is executing from, so it must outlive the tick's own
   process tree, never run inline. **The tick never clears the marker
   itself on a fire** — `--no-block` returns the moment the start request is
   *queued*, not once the spawned unit has even begun running, so an
   in-process `clear_roll_pending()` right after it would race the freshly
   spawned process out from under it (it re-resolves its target from a PyPI
   lookup + a fleet health gather before it ever reads the marker). Only the
   spawned `coord release nightly-window` clears it, and only once `coord
   release propagate` has actually confirmed the roll. A `systemctl start`
   against an already-active `Type=oneshot` unit is systemd's own no-op, so
   the tick harmlessly re-requests the fire every ~3 minutes until the
   marker is gone.
4. The marker is bounded two independent ways —
   `ROLL_PENDING_DEFAULT_TTL_SECONDS` (wall-clock, default 1h) and
   `ROLL_PENDING_DEFAULT_MAX_DEFERRALS` (a tick-count ceiling, default 20,
   independent of the clock) — so a pending or failed roll can never hold
   the queue down indefinitely; whichever bound is hit first clears the
   marker, records a loud escalation (`(roll-pending)` in `coord drive
   escalations`, separate from the ordinary queue alert so it cannot be
   silently overwritten by the same tick's own alert handling), and
   launching resumes THAT SAME tick.

**Visible in `coord drive-queue status`** (and its `--json` output) while
live — the whole point is that a held-for-a-roll queue must never read as
broken, so it gets its own line, not the alert channel a cordon or a stalled
entry uses.

**Same unit, two triggers.** `coord-release-window.service`'s `ExecStart=`
is always `coord release nightly-window` — the SAME command handles both its
own nightly timer (resolve the target; if nothing is pending yet, set the
marker) and a tick-triggered `systemctl start` (a marker is already pending
for this target — make one best-effort attempt to fire it via `coord
release propagate`, no `--force`, ever). See
`deploy/coord-release-window.service`'s header for the full walkthrough.

**Diagnosing a stuck roll:** `coord drive-queue status` /
`coord release window-history` — a `roll-pending` status is healthy and
expected; `STATUS_DRAIN_TIMEOUT` no longer occurs (nothing drains any more).
A marker still present well past an hour with the fleet visibly idle is a
bug, not a busy fleet — check `coord-release-window.service`'s own journal
for why the spawned run never confirmed a roll (it, not the tick, is the
one that clears the marker — see point 3 above). Seeing the tick's own
journal log a `systemctl start` request for `coord-release-window.service`
on *every* tick while the fleet stays idle is expected while a fire attempt
is in flight or repeatedly failing to confirm — it costs nothing against an
already-active unit — but the marker itself must still clear well within
the hour; if it does not, that is where to look.

### Auto-roll threshold gate (`propagation.min_releases_behind`, #2583)

Everything above attempts a roll on **any** delta at all — one release
behind is enough to cordon, drain, and restart every agent. That is fine on
a healthy propagation lane, but it means every quiescent moment, however
brief, gets spent on the smallest possible roll instead of accumulating
into a less frequent, larger one. `propagation.min_releases_behind` in
`coordinator.yml` (or `--min-behind` on either command, which overrides it
per-invocation) holds a run below a configured delta instead:

```yaml
propagation:
  min_releases_behind: 10   # operator's stated target
```

**Default is `1` — unset means byte-identical to today.** No `propagation:`
block, or an explicit `min_releases_behind: 1`, changes nothing: any delta
still rolls, subject to the same quiescence/cordon rules as always. Raising
it is opt-in, and specifically opt-in to a *fleet-wide* config value, not a
per-timer flag — both `coord-release-propagate.timer` and
`coord-release-window.timer` read the same `coordinator.yml`, so setting it
once governs both.

**A held run is a REPORTED no-op, never a silent one.** Below the
threshold, `coord release propagate` and `coord release nightly-window`
both print (and journal, as `STATUS_HOLDING`/`status: "holding"`) a line
shaped exactly like:

```
⊖ 2026-08-24 03:00:01  v0.5.226  holding
    holding: 4 behind, threshold 10
```

so a deliberately-held fleet reads unambiguously differently from a dead
timer (`coord release history` / `coord release window-history` — see
#2045, the incident this distinction exists to prevent). **Holding never
touches anything**: `coord release propagate` takes no cordon and calls no
per-host lane executor; `coord release nightly-window` neither sets, fires,
nor clears the #2587 roll-pending marker — an existing marker (set before
the threshold was raised, or by a since-lowered one) is left exactly as it
was for a later, above-threshold run to pick back up.

**The delta is the real PyPI count, not an estimate.** It reuses
`coord.health.pypi.releases_behind` — the same computation `coord health`'s
`agent_version` check already runs on every machine's own `/health` — so
"N behind" here is the actual number of published releases being held
back, never a second, disagreeing implementation. (`coord.release_cordon.
version_drift`, used elsewhere for cordoning and `needs_roll`, is a
deliberately *different*, network-free patch-arithmetic estimate built for
decisions that run on every tick; this gate is the opposite case — an
operator-set threshold checked once per run — so it pays for one extra PyPI
read instead.) An unresolvable delta (the daemon's version unreadable, the
index unreachable) gates OPEN, not held: #1834's rule that missing data is
never evidence of agreement applies here too, so a fleet that genuinely
needs a roll is never silently frozen by a probe failure.

**Prefer `coord-release-window.timer` as the vehicle.** The 20-minute
propagate timer *also* honours this gate (`--min-behind` and the config
key both apply to it directly), but the nightly window is the better fit:
it is the one place a roll is *created* on a schedule you control (03:00),
rather than attempted every 20 minutes against whatever the fleet's busy
state happens to be. See the evidence in issue #2583 itself: dellserver is
both the daemon host and a work machine, so it is rarely quiescent, and a
20-minute timer cordoning it repeatedly without ever landing a window is
pure churn the nightly window avoids by design.

**Do not raise this above `1` on a stale propagation lane.** Between
2026-08-13 and 2026-08-21 at least six defects in this exact lane were
fixed — #2187/#2178 (a verified roll reported as `propagate-failed`),
#2240 (the cordon deadlock above), #2403 (an idle host wrongly deferred),
#2490 (no path back after a cooldown), #2052. A fleet running an OLD build
of this lane is running the buggy version of the exact mechanism a large,
infrequent roll stresses hardest. **Roll the fleet to current by hand
first** (`coord release propagate` / `coord release nightly-window`,
watched), confirm the lane behaves correctly on a fixed codebase, and only
then set `min_releases_behind` above 1 — otherwise the very first automated
threshold roll is the largest delta the lane has ever attempted, against
code known to have bugs in exactly that path.

## `coord.db` backups to the external SSD (interim — #1822 owns the real thing)

`~/.coord/coord.db` on the daemon host is the fleet's canonical state and had
no backup at all until 2026-08-10. This is a stopgap while **#1822**
(continuous backup + *verified restore*, `tier:large`) is unstarted. Ships in
`deploy/` (#2098: `coord-db-backup.sh` + `.service` + `.timer`, the same lane
as every other unit); install on the daemon host:

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
cp deploy/coord-db-backup.sh ~/.local/bin/ && chmod +x ~/.local/bin/coord-db-backup.sh
cp deploy/coord-db-backup.service deploy/coord-db-backup.timer ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now coord-db-backup.timer      # hourly, Persistent=true
```

`coord-db-backup.service` carries an explicit `Environment=PATH=` line for the
same #1831/#2561/#2569 reason as `coord-serve.service`/`coord-notify.service`/
`coord-drive-queue.service` in this directory: a systemd *user* unit's default
PATH does not include `~/.local/bin` (where the `coord` shim lives) or
`~/.coord-venv/bin`, and `coord-db-backup.sh` now shells out to `coord
store-backend` (#3084/#3085) before touching the filesystem — without the
override, `command -v coord` fails and the unit refuses on every fire, even
for the honest `store.backend: sqlite` case. If you ever copy this unit by
hand instead of `cp`-ing `deploy/coord-db-backup.service` verbatim, keep that
line.

| | |
|---|---|
| target | `/media/crucial/coord-backups/` — Crucial X9 2TB SSD, **ext4** |
| cadence | hourly, `Persistent=true` (catches up after a reboot) |
| retention | 168 snapshots ≈ 7 days, ~68 MB each |
| latest | `coord.db.latest` symlink |

`VACUUM INTO`, never `cp`: coord-serve writes continuously, and a plain copy of
a WAL-mode database under load can capture a torn file — the failure you only
discover at restore. Each snapshot is `PRAGMA integrity_check`ed and asserted
non-empty before it counts; a failed check is kept as `.REJECTED` rather than
deleted. The script **refuses to run when the SSD is not mounted**, because
`/media/crucial` is still a perfectly good directory on the root filesystem
when nothing is mounted there, and would otherwise receive "backups" of the
disk they exist to protect.

Do not use `/media/passport` — that is a WD My Passport *spinning* disk
formatted NTFS, a poor target for SQLite snapshots and permissions.

Check it:

```bash
systemctl --user list-timers coord-db-backup.timer
journalctl --user -u coord-db-backup.service --since today
ls -lh /media/crucial/coord-backups/ | tail -5
sqlite3 /media/crucial/coord-backups/coord.db.latest 'PRAGMA integrity_check; SELECT COUNT(*) FROM assignments;'
```

**What this does NOT protect against.** The snapshots live on a disk attached
to the machine they protect. This covers db corruption, a bad migration,
accidental deletion and OS-disk failure — **not** dellserver being lost, stolen
or destroyed. And matching row counts are not a restore drill: nothing has yet
stood a `coord-serve` up against a restored snapshot. Both gaps belong to
#1822; the cheap interim notch is an rsync of `coord.db.latest` to one other
fleet machine over Tailscale.

## Graphify graph: reseed a machine's local clone

`graphify-out/` is **not** tracked in git (claude-coordinator, vimcode, and quadraui all gitignore it as of 2026-06-07). Each repo's knowledge graph is a regenerable, machine-local cache rebuilt by the `post-commit` / `post-checkout` git hooks. PyPI agent installs have no clone and don't need this — it applies only to machines with a **local git checkout** of these repos (the dev machine, and any worker box that builds/tests them).

**One-time migration** — the first time a clone pulls the commit that stopped tracking `graphify-out/`, git wants to delete the now-untracked files, but the hooks keep them dirty, so the pull may abort with *"local changes would be overwritten"*. Discard the (regenerable) cache first, then pull:

```bash
cd <repo>            # e.g. ~/src/quadraui
rm -rf graphify-out  # safe — regenerable cache
git pull             # now clean
```

**Reseed the graph** — one-time per machine per repo. Restores the rich semantic + community graph (AST-only refresh is free on every commit thereafter):

```bash
/graphify            # in a Claude Code session at the repo root
# or headless:  graphify .
```

Then ensure the hooks are installed so the graph stays current after the seed:

```bash
graphify hook install   # idempotent; appends to any existing post-commit hook
```

Without the seed, queries have no `graph.json` to read until the next commit triggers an AST-only rebuild — and `post-checkout` will **not** bootstrap a graph when `graphify-out/` is absent, so the explicit one-time seed is required.

## Routine upgrade (all agents)

From the coordinator machine:

```bash
coord agent update --all
```

This POSTs to `/update` on every machine in `coordinator.yml`, telling each agent exactly which version the coordinator wants (`target_version`). Each agent pins its pip install to that exact release (`pip install --upgrade --no-cache-dir code-coordinator==<target_version>`) and restarts.

**#1886: `target_version` is resolved from PyPI's simple index — the same source `pip install -U` itself resolves against — NOT from this CLI's own `__version__`.** A stale operator install (PyPI already has a newer release than the CLI running `coord agent update`) used to silently pin the whole fleet to that stale, older version while still printing a clean `✓` on every machine. If this CLI's own version is behind PyPI's latest, the command now prints a loud warning and targets the *newer* PyPI release instead — never its own age. Pass `--version X.Y.Z` to pin to something else on purpose (a rollback, a pre-release); that skips the PyPI lookup entirely.

**#1568: success is judged by the version the agent actually reports back, not by the POST being accepted and not by a liveness ping.** The CLI polls `/health` until the reported (running) version equals `target_version` (or `--timeout` elapses) and reports `version_before → version_after` only for machines that actually got there. This closes failure modes that used to all report as "the update worked":

- A stale PyPI index/cache resolving `pip install --upgrade` to the *same* old version while exiting 0 — pinning to `target_version` turns that into a loud pip failure instead of a silent no-op.
- The `os.execv` self-restart not taking under systemd (#404) — the agent's pip step genuinely succeeded but the *old* process kept answering `/health`. When `last_update.result == "upgraded"` but the version hasn't advanced by the end of the poll window, the CLI automatically escalates with a driven `ssh <host> 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-agent'` (see "The `XDG_RUNTIME_DIR=...` prefix is load-bearing" below) and gives it one more short window before reporting failure. As of #1886 the agent itself also prefers a self-driven `systemctl --user restart coord-agent` over `os.execv` when it detects it's running under systemd (`$INVOCATION_ID` set), so this escalation should be needed less often.
- **#1886**: `/health` reports `version` (the *running* process's loaded module — fixed until the process actually restarts) separately from `installed_version` (a disk read that advances the instant `pip` writes to site-packages, restart or not). `coord agent update` only ever matches on `version`, so a process that upgraded on disk but never restarted is reported as a failure — see "installed X but the running process still reports Y" — not a false `✓`. `coord status` surfaces the same drift on every run, without needing an update in flight.

If a machine still doesn't end up on `target_version`, `coord agent update` exits non-zero and prints why (`no change`, `failed`, still-stuck-after-escalation, or never came back online) instead of a bare success.

To target one machine:

```bash
coord agent update --machine precision
```

**`coord agent update` restarts only the agent (`coord-agent`, 7433) — NOT
the `coord-serve` daemon (7435).** On the daemon host (dellserver) both
services run from the same `~/.coord-venv`, so `coord agent update` upgrades
the on-disk code for both, but the already-running `coord-serve` process keeps
executing the *old* code in memory until it is restarted. So whenever a release
changes coordinator/daemon Python (`serve_app.py`, the `/board` handlers, etc.),
the rollout has a **second, separate step** — restart the daemon on its host:

```bash
# same XDG_RUNTIME_DIR / #404 caveat as the agent (an execv self-restart under
# systemd leaves the stale PID); an explicit systemctl restart is what takes.
ssh dellserver 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-serve'
# verify: back up on the new version + board reachable
ssh dellserver 'systemctl --user is-active coord-serve'
coord status            # thin clients should reconnect to the daemon
```

Skip this and the fix ships to the fleet but the daemon silently keeps serving
the old behaviour — the failure mode that stranded #850 (a `serve_app.py` fix)
even though every agent already reported the new version.

### The fourth lane: `~/.coord-cli-venv` (the epic sequencer's `COORD_BIN`)

**`coord agent update --all` does not touch this, and nothing else does either.**

`drive-epic.service` on elitebook runs the unattended epic sequencer with an
explicit binary override:

```
Environment=COORD_BIN=/home/john/.coord-cli-venv/bin/coord
```

That is a **separate, pinned PyPI install** — distinct from the agent venv on the
same machine *and* from the editable CLI in `~/src/claude-coordinator`. It is
upgraded by exactly one thing: someone remembering to do it.

```bash
~/.coord-cli-venv/bin/pip install --upgrade --no-cache-dir code-coordinator==X.Y.Z
~/.coord-cli-venv/bin/coord --version    # verify — never infer from pip's exit code
```

**Why this is the nastiest lane to miss.** The editable CLI in the checkout is live
the instant a PR merges, so every `coord` command *you* type picks up the new code
and the deploy looks complete. But every drive the sequencer launches on its
30-minute timer resolves `coord` through `COORD_BIN` — the stale copy. Hand-testing
confirms the fix while automation keeps hitting the bug, and both are "the same
command."

Found stale on 2026-07-29 at **0.4.84 while the fleet was on 0.4.87** — three
releases behind. Every timer-launched drive in that window had been running without
#1564's CI merge gate, #1565, #1567, and #1568, all of which were believed live.

**Add it to every release.** The complete deploy surface is six lanes:

| # | lane | how it updates | needed when |
|---|---|---|---|
| 1 | `~/.coord-venv` × N (agents) | `coord agent update --all`, or per-machine pip + `systemctl --user restart coord-agent` | any `coord/agent.py` change; always safe to do |
| 2 | `coord serve` (daemon host) — **and the PATH it hands its children** | `systemctl --user restart coord-serve` **after** lane 1 upgraded the on-disk code; the PATH half is lane 6's `cp` + `daemon-reload` | any `serve_app.py` / `state.py` / `review.py` / `merge_queue.py` change. The second half is its own failure: on 2026-08-04 the daemon was current and everything it *spawned* was two releases back (see `coord release verify` below) |
| 3 | `coord-tui` | local `cargo build && cp target/debug/coord-tui ~/.local/bin/` | any `tui/**` change — never PyPI |
| 4 | **`~/.coord-cli-venv`** | manual `pip install --upgrade` + verify `coord --version` | **every release**, because the sequencer drives through it |
| 5 | `.githooks/` hooks | nothing to run — live on the next `git fetch` (given `core.hooksPath` is already set) | any `.githooks/**` change — see below, this lane's failure mode is the opposite of 1–4 |
| 6 | `deploy/*.service`/`*.timer` (systemd units) | manual `cp deploy/<unit> ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user restart <unit>` on each affected host | any `deploy/**` change — see below, `coord doctor`/`coord health` is the only thing that notices when this is skipped |

### The fifth surface: `.githooks/**` — the opposite failure mode

Lanes 1–4 exist to make deploys *slower and deliberate*: a merged fix sits
inert until someone runs `coord agent update`, restarts `coord-serve`,
rebuilds `coord-tui`, or bumps `~/.coord-cli-venv`. `.githooks/**` is the
opposite. It is **repo-tracked and takes effect on the next `git fetch`** on
any machine with `core.hooksPath=.githooks` set — no PyPI release, no daemon
restart, no rebuild. A merged hook is live everywhere immediately, which
means a bad hook is *also* live everywhere immediately.

This bit on 2026-07-30 (#1617): a `.githooks/post-checkout` regression
(`rm -rf graphify-out && ln -sfn ...` deleting the tracked
`graphify-out/.gitignore`) went from merge to affecting **every new worker
worktree on every machine** in about 30 minutes, with no canary and no
rollout to stagger it — because there was no rollout to stagger. The only
practical mitigation was `git config --unset core.hooksPath` on all three
machines, which also disables every *other* hook in `.githooks/`, not just
the broken one. Treat `.githooks/**` changes with the same caution as a
schema migration: they need to be right on merge, because there is no lane 5
"upgrade" step to catch them first.

### The sixth surface: `deploy/**` — reviewed like code, installed by nobody

`deploy/*.service`/`*.timer` is version-controlled, reviewed, and merged the
same as any other file — which reads as "shipping a fix" and is not. **The
release path (bump → PR → merge → tag push → `publish.yml` → PyPI, then
`coord agent update` for the venvs) never touches
`~/.config/systemd/user/`.** A unit is hand-installed once at machine setup
(`cp deploy/coord-serve.service ~/.config/systemd/user/`, per that file's own
install instructions) and then drifts forever — nothing re-copies it, and
nothing notices when it stops matching.

Found on dellserver 2026-08-04 (#1831), immediately after a release: the
installed `coord-serve.service` was **three weeks stale**, and the drift
wasn't cosmetic. Its `Environment=PATH=` still started with an *editable*
checkout of this repo (`~/src/claude-coordinator/.venv/bin`). `coord_argv()`
(`coord/drive.py`) resolves subprocesses via `shutil.which("coord")` — i.e.
from that PATH — so the daemon itself ran the pinned release while
everything it spawned ran whatever stale branch that checkout happened to
sit on. Every version readout (daemon, all three agents, `~/.local/bin/coord`,
the PyPI simple index) said the release was live; none of them looked at what
the daemon's own children would actually resolve.

**Two things close this, together — neither alone is sufficient:**

- **`~/.local/bin` (a symlink onto `~/.coord-venv/bin/coord`, the pinned
  release) must precede any editable checkout's `.venv/bin` on a unit's
  PATH.** This is the structural fix: even a completely stale checkout can
  no longer shadow the release once the release resolves first. See
  `deploy/coord-serve.service`'s own comment for why the repo venv is kept
  on the PATH at all (#1117 — `pytest`/`python3` need its deps) and why it
  goes *after* `~/.local/bin`, not before.
- **`coord doctor` / `coord health`** report unit-file drift as a first-class
  check (`unit_drift` / `fleet_unit_drift`, `coord/health/checks/
  unit_drift.py`, #1831) — STALE content vs. the units packaged with the
  installed release (`coord/deploy/`, kept byte-identical to `deploy/` by
  `tests/test_packaged_deploy_units.py`), and separately, CRIT
  if any unit's PATH lets an editable checkout precede the release. The
  reference is the released artifact rather than the host's checkout on
  purpose (#1927): a checkout nobody pulled goes stale in lockstep with the
  installed unit, so the two agree and the diff reports clean in exactly the
  #1831 case it exists to catch. A host whose reference *is* only a working
  copy (source install, or a wheel predating #1927) now reports UNKNOWN on a
  match rather than OK — an un-annotated green from an unverified reference
  is worse than no check. Run
  `coord doctor` after any `deploy/**` change lands and after any
  `deploy/**`-affecting machine setup, the same way lane 1–4's "needed when"
  column above expects a manual verification step.

This lane deliberately has **no automatic install step** — see this repo's
`coordinator.yml` docs / the #1831 issue's non-goals: writing a `systemctl`
unit on every host automatically is a bigger blast radius than silent drift
is. Detection first; a human runs the `cp` + `systemctl --user restart`.

## The post-release step: `coord release verify` (#1834)

**Run this after every release, once the rollout is done.** It is the one
command that asks whether the whole system is actually at one version, across
every lane in the table above and every host in `coordinator.yml`:

```bash
coord release verify                 # --pypi is the DEFAULT: grade every lane
                                     # against the released version
coord release verify --no-pypi       # no absolute — report skew BETWEEN lanes only
coord release verify --expected 0.4.105 -v
coord release verify --json          # for a script or a dashboard
```

**`--pypi` is on by default (#2052 / #2035 item 4).** Without an expected
version this command compares the fleet **against itself**, so a fleet that
is uniformly *behind* reads as health — demonstrated, not hypothesised: after
#2052's botched propagation reverted every host to 0.5.4 while `main` was
four releases ahead, `coord release verify` reported `crit=0`. A `--no-pypi`
run with no `--expected` therefore now emits an UNKNOWN finding saying so
rather than a clean bill of health; skew between lanes is still CRIT on its
own, exactly as before.

Exit codes mirror `coord health`: **0** clean, **1** something unverified
(a host that did not answer, a lane with no data, a stale `coord-tui`), **2**
a real drift finding. It runs entirely over HTTP — each machine's own
`/health` plus the daemon's `/board` — so it works **from a thin client** with
no checkout and no credentials, and it is **strictly read-only**: safe to run
mid-flight, and it never fixes anything (`coord agent update` owns that lane;
automatically writing `systemctl` units across every host is a far bigger
blast radius than detection).

### Why it exists: 2026-08-04, when every readout said the fleet was fine

Hours after v0.4.105 shipped:

| readout | said |
|---|---|
| PyPI simple index | 0.4.105 |
| `coord status` — all three agents | 0.4.105 |
| `~/.coord-venv/bin/coord version` (daemon host) | 0.4.105 |
| `~/.local/bin/coord version` | 0.4.105 |
| **what the daemon actually spawned** | **0.4.103** |

Nothing was lying. Each readout read a different lane, correctly, and no
command compared them. `coord-serve`'s hand-installed unit began its
`Environment=PATH=` with an editable checkout two releases back, and
`coord_argv()` (`coord/drive.py`) resolves every subprocess with
`shutil.which("coord")` — so the daemon ran the release and everything it
spawned ran the checkout.

Three things follow, and they are why this command is shaped the way it is:

- **It verifies the running *process*, not the venv.** `pip install --upgrade`
  silently no-ops often enough to be a documented fleet gotcha, so a venv
  reporting the right version proves nothing about what executes. The
  `<unit> spawns` lanes read `/proc/<mainpid>/environ` on each host — the PATH
  the kernel is actually holding for the live service — resolve `coord` on it
  with the same `shutil.which` call `coord_argv()` makes, and ask *that* binary
  its version (`coord/health/checks/spawned_coord.py`). This is strictly
  stronger than `unit_drift`'s PATH-shadow check, which reads unit *files* and
  so cannot see a drop-in, an `EnvironmentFile`, `systemctl --user
  set-environment`, or a service started by hand from a shell with a dev venv
  activated. Both checks exist; neither subsumes the other.
- **It reports skew BETWEEN lanes, not staleness within one.** Every
  individual lane was green on 2026-08-04; the defect existed only as a
  relationship. `--expected`/`--pypi` narrow that to "disagreement with a named
  version", but skew alone is already conclusive and is reported without one.
- **Any editable install on a service PATH is a finding on its own** —
  CRIT regardless of the version it currently reports. It is a drift amplifier
  that silently tracks a checkout nothing keeps current, so today's accidental
  agreement is not evidence of anything.

### What it covers

| lane | source | check |
|---|---|---|
| `~/.coord-venv` × N (agents) | each machine's `/health` | `agent_venv` (+ editable detection) |
| `<unit> spawns` × N (live services) | each machine's `/health` | `spawned_coord` — **the 2026-08-04 lane** |
| `coord-serve process` (daemon host) | `/board` → `fleet_deploy_lanes` | daemon's own install (#1806: only introspectable from the process itself) |
| unit files vs the packaged `coord/deploy/**` | each machine's `/health` | `unit_drift` / PATH shadow (#1831, #1927) |
| `~/.coord-cli-venv` | each machine's `/health` | `cli_venv` (#1806) |
| `coord-tui` binary vs `tui/` source | each machine's `/health` | `tui_binary` — until PKG-3/PKG-4 give it a real channel |
| `coord web --dist` bundle vs `coord-web`'s own source | each machine's `/health` | `webapp_bundle` / `fleet_webapp_bundle` — lane 5, staleness only (see below) |

`.githooks/**` (lane 6 above) is deliberately **not** graded here: its failure
mode is the inverse — it is live everywhere at the next `git fetch`, with no
release to be behind — so "is it at the released version?" is not a
well-formed question for it. `~/.coord/coordinator.yml` provenance (lane 7)
has its own sweep: `coord diagnose` (`coord/fleet_config_health.py`, #1779).

**Lane 5, the webapp bundle, is graded on different terms than every other
row above — deliberately, not by oversight.** Every other lane compares
against *the released version*: a pip version string that both an agent venv
and a spawned `coord` can report. The webapp bundle has no such string to
compare against. `deploy/coord-web-dist-build.timer` publishes it
continuously off the `coord-web` repo's `origin/main` SHA (#2470; before
that, this repo's own `coord/dashboard/webapp/`, epic #2002), every 10
minutes (#2122), **decoupled on purpose** from the `~/.coord-venv` release
cadence every other lane rides (#1543) — that decoupling is the entire point
of that pipeline, so folding it into the version-skew map above would
manufacture permanent, meaningless "skew" between a semver string and a git
SHA on every correctly-running fleet, not report a real defect. What
`webapp_bundle` (machine-scope) / `fleet_webapp_bundle` (fleet-scope) check
instead, on its own terms — the same shape `tui_binary`/`fleet_tui_binary`
already use for the locally-built `coord-tui` binary — is whether the live
bundle is *stale relative to the `coord-web` source tree it claims to have
been built from*, and whether two machines both serving `coord web` agree on
which build that is.
A WARN there means "`coord-web-dist-build.timer` has stopped publishing",
which is the failure mode this lane exists to catch; it says nothing about
whether that publish is caught up with the latest tagged release, because
that was never a question this pipeline answers.

A host that does not answer is reported **UNKNOWN, never OK** — "we could not
ask" must not render as "verified", which is the entire thesis of #1834.

## Fleet-wide version check

```bash
coord agent versions --all
```

Prints the coordinator's own version alongside every agent's self-reported
version and flags any mismatch as a **split-brain** (exit non-zero). Run it
before trusting a rule change made against the coordinator's local version,
and after `coord agent update --all` to confirm the rollout actually landed
everywhere — a split-brain fleet is only detectable by comparing versions
directly, not by whether the last `coord agent update` reported success.

**This is a subset of `coord release verify` above, and a strictly weaker
one**: it compares *agent self-reported* versions only, which is exactly the
readout that was green throughout 2026-08-04. Use it for a quick rollout
confirmation; use `coord release verify` before you believe a release is
actually live.

## Making a machine `browser`-capable (Playwright, #1541)

Web smoke and web acceptance both route `coord/dashboard/webapp/**` to the
`browser` capability, so **every** web test in the program lands on a machine
that advertises it. Until 2026-08-02 that was exactly one machine (elitebook,
the dev box), which made one flaky host able to stall a whole milestone. On
2026-08-02 dellserver and precision were both added, so all three machines now
advertise `browser` on the same Chromium build. This is the runbook for a fourth.

Headless Chromium needs **no display** — a headless server is a perfectly good
browser machine, and web tests headless-test far more cleanly than the TUI does.

**Check what is actually missing before installing anything.** The two machines
added on 2026-08-02 were missing *different* halves, and neither was obvious from
`coord doctor`, which showed nothing for both:

| machine | had | missing |
|---|---|---|
| dellserver | `node` v20 in `/usr/bin` | the browsers |
| precision | the browsers (`chromium-1228`, already cached) | any `node` on the agent PATH |

The reason `coord doctor` was blank in both cases is step 6 below: **probes are
capability-driven**. Neither machine declared `browser`, so neither was probed, so
neither reported the half it already had.

**1. Node.** `node`, `npm` and `npx` must resolve from the agent's PATH, not just
your interactive shell. Ubuntu 24.04's packaged `node` (v20) is sufficient.

If the machine uses **nvm**, do not bake the version-stamped path into the unit —
install the run-time shim (`deploy/node-shim.sh`, #1678). precision is the worked
example: nvm had v20.20.2, but only inside an interactive shell, so the agent saw
no Node at all. `~/.local/bin` is already on the agent unit's PATH, so the whole
fix is:

```bash
scp deploy/node-shim.sh <machine>:~/.local/bin/coord-node-shim
ssh <machine> 'chmod +x ~/.local/bin/coord-node-shim
  for c in node npm npx; do ln -sf ~/.local/bin/coord-node-shim ~/.local/bin/$c; done'
```

Confirm it resolves from the *agent's* PATH, not yours:

```bash
ssh <machine> 'env -i HOME=/home/john \
  PATH=/home/john/.coord-venv/bin:/usr/local/bin:/usr/bin:/bin:/home/john/.local/bin \
  sh -c "node --version; npm --version"'
```

**2. Browsers — match the pinned version, do not take latest.** Read the pin from
`coord/dashboard/webapp/package.json` (`@playwright/test`, currently `^1.61.1`) and
install that exact line, so every browser machine runs the same build:

```bash
npx --yes playwright@1.61.1 install chromium
```

This populates `~/.cache/ms-playwright/` with `chromium-<build>` and
`chromium_headless_shell-<build>`. Both existing machines are on build **1228**.
`coord doctor` reports it as `playwright-browsers: <build>`; a mismatch between
machines means two Test runs of the same commit are not the same experiment.

**3. System libraries — check, do not assume you need `--with-deps`.** On Ubuntu
24.04 the required shared libraries are already present and `--with-deps` (which
needs sudo) was **not** necessary. Verify by launching rather than by inspection:

On the target machine, write the probe to a file (do not try to inline it through
an ssh quoting layer — that is how you end up debugging your own escaping instead
of the browser):

```bash
mkdir -p /tmp/pwlaunch && cd /tmp/pwlaunch
npm init -y >/dev/null && npm install --no-audit --no-fund playwright@1.61.1 >/dev/null

cat > launch.js <<'EOF'
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();          // headless by default
  const p = await b.newPage();
  await p.setContent('<h1>hello</h1>');
  console.log('RENDERED:', await p.textContent('h1'));
  await b.close();
  console.log('LAUNCH_OK');
})().catch(e => { console.log('LAUNCH_FAIL:', e.message); process.exit(1); });
EOF
```

Then run it through a **stripped environment**, which is the whole point of the
check:

```bash
ssh <machine> 'env -i HOME=/home/john PATH=/usr/bin:/bin \
  node /tmp/pwlaunch/launch.js'
# expect:  RENDERED: hello
#          LAUNCH_OK
```

A `LAUNCH_FAIL` naming a missing `lib*.so` is the signal that this machine really
does need `npx playwright install --with-deps chromium` (sudo). Clean up
`/tmp/pwlaunch` afterwards.

The `env -i` matters: it reproduces the agent's environment rather than your
interactive PATH. This is the same trap as the elitebook `gh`-blind-workers case
(#1483) — a tool that works when you ssh in by hand can be invisible to the agent.

**4. Declare the capability** in the tracked `coord-settings` checkout
(`~/src/coord-settings/coord/coordinator.yml` on the daemon host — commit +
push there, then `git pull` on the daemon host), **not** by editing the
daemon host's `~/.coord/coordinator.yml` directly: that path is a symlink
into the checkout, and `sed -i`/most editors write-and-rename over it,
silently replacing the symlink with a disconnected regular file (see
`OPERATING_GOTCHAS.md` #14). Also not a local copy — see the config-cache
note in `CLAUDE.md`:

```yaml
  - name: dellserver
    capabilities: [rust, python, browser]
```

**5. Restart, in this order:** the machine's `coord-agent` (so it re-probes and
republishes `/health`), then `coord-serve` on the daemon host (so routing sees the
new capability). Both need the fleet idle — restarting an agent kills any headless
worker running on it.

**6. Verify** with `coord doctor`: the machine should now report `node`, `npm` and
`playwright-browsers`. Note these probes are **capability-driven** — `probe_all()`
only probes what the machine declares, so a machine can have Node installed and
still show nothing until `browser` is in its `capabilities` list. An empty probe
list is not evidence that a tool is missing.

## External-tool prereqs (`coord doctor`, #1570 B/D/E)

coord shells out to external tools it never used to check — `gh` was the
first to bite (#1564: a 3-year-old `gh` on the daemon silently blocked every
merge for a night). `coord/prereqs.py` generalizes that into a manifest:
baseline tools required on every machine (`git`, `gh` — floor is
`coord.github_ops.GH_PR_CHECKS_JSON_MIN_VERSION`, currently `2.86.0`) plus
per-capability tools (`rust` → `cargo`, `gtk` → GTK4 dev libs via
`pkg-config`, `python` → `python3`, `browser` → a Chromium-family binary,
for consuming projects that use the `browser` capability example in
`coordinator.example.yml`).

Each agent self-probes at startup (cached ~5 min — see
`AgentServer._cached_tool_versions`) and publishes the result as
`tool_versions` in its `/health` response — one entry per baseline prereq
plus every prereq backing that machine's declared `capabilities:`. An agent
older than this release simply omits the key; callers must treat a missing
`tool_versions` as "unknown," not as a failure (see below).

```bash
coord doctor                  # whole fleet
coord doctor --machine precision
```

Prints, per machine: online/offline, each probed tool's found/version/floor
status, and any declared `capabilities:` entry whose backing tool the
machine's own probe disagrees with (e.g. `gtk` claimed but `pkg-config
gtk4` isn't found). Exits non-zero if anything is wrong anywhere in the
fleet — this is the one-command answer the #1564 incident took a full night
to reach by hand.

The smoke-test dispatcher (`coord.smoke.dispatch_smoke`) uses the same
manifest to cross-check a candidate machine's live probe against the
capability a diff needs *before* routing to it — a machine that claims
`gtk` in `coordinator.yml` but fails its GTK4 probe is refused with the
specific reason, not silently dispatched to fail 20 minutes into a smoke
run. This fails **open**, not closed, on missing telemetry: a machine
running an agent that predates this feature (no `tool_versions` in
`/health`) is still routable — only an *explicit* probe failure refuses
routing, so a partially-upgraded fleet doesn't go dark on smoke dispatch.

### "not found" usually means "not on the agent's PATH", not "not installed" (#1671)

The probe resolves a **literal binary name** through the *agent process's* PATH. A systemd user
unit's PATH is minimal and set explicitly in the unit, so a rustup toolchain under `~/.cargo/bin`
is invisible to it while being perfectly present on the box and on your login shell's PATH. Before
concluding a tool is missing, check where it actually is:

```bash
coord doctor --machine <name>                                     # what the agent's probe sees
systemctl --user show -p Environment --value coord-agent.service  # what the agent can reach
```

If the binary exists but isn't on that PATH, the fix is in
[Install a new agent](#install-a-new-agent-first-time) — widen the unit's PATH, then restart.

**Do NOT fix this by making the probe search harder.** Symlinking `cargo` onto the existing PATH, or
teaching `prereqs.py` to look in `~/.cargo/bin`, satisfies the probe while `cargo build` still fails
inside the worker — `cargo` shells out to `rustc`, which lives in the same directory the worker
still can't reach. That converts an honest refusal into a **false green**, which is strictly worse:
the capability reports met and the failure resurfaces 20 minutes into a smoke run as an unrelated
error. Probe-side and worker-side resolution must move together, which they only do when the
*agent's own* PATH is widened.

This failure mode is **silent by construction** — the probe is right, the refusal is right, and
nothing surfaces either until someone runs `coord doctor`. Treat that command as part of
provisioning, not as a debugging tool.

## Upgrade via the raw `/update` endpoint (reliable fallback)

`coord agent update` is a thin wrapper over the agent's `POST /update`
HTTP endpoint plus a "poll `/health` until the version matches, escalating
via `systemctl --user restart` if needed" loop (#1568). If a machine is
slower than `--timeout` (default 120 s) to converge even after the
automatic escalation — very slow pip mirrors, a host that isn't running
`coord-agent` under systemd (so the escalation itself can't help), etc. —
drive the endpoint directly and poll `/health` yourself with no artificial
timeout:

```bash
# 1. Fire the upgrade. Returns 202 immediately; the pip install + restart
#    run in a background thread on the agent.
curl -s -X POST http://<host>:7433/update
# → {"status":"updating","mode":"pip install --upgrade"}

# 2. Poll /health until the version advances (the agent drops its socket
#    briefly during the execv restart — `curl` failing for a few seconds
#    is expected).
until [ "$(curl -s http://<host>:7433/health | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version"))' 2>/dev/null)" = "<new-version>" ]; do
  sleep 3
done
echo "agent is on <new-version>"
```

Behaviour worth knowing:

- The endpoint **runs `pip install --upgrade --no-cache-dir
  code-coordinator`** (or `git pull --ff-only` for an editable
  install) in a daemon-less background thread, then `os.execv`-restarts
  **only if the version actually changed**.
- If the installed version is already current it records
  `result: no_change` and does **not** restart — so hitting `/update` on
  an already-up-to-date agent is harmless (it just runs pip and returns).
- The full pip/git output is written to `~/.coord/last_update.log` on
  the agent; a short excerpt plus `mode` / `result` / `version_before` /
  `version_after` / `error` are surfaced under `last_update` in
  `/health`.

**Do not update an agent that is running a worker you care about** — the
`os.execv` restart kills in-flight `claude -p` subprocesses. Check
`curl -s http://<host>:7433/status` for a non-empty `active` list first,
or wait for the work to finish.

## Diagnose a failed upgrade

If `coord agent update` reports `✗ did not come back` or the version doesn't advance, query the machine's `/health` and read `last_update`:

```bash
curl -s http://<host>:7433/health | python3 -c "
import json, sys
d = json.load(sys.stdin)
lu = d.get('last_update', {})
print('version:', d.get('version'))
print('mode:', lu.get('mode'))
print('result:', lu.get('result'))
print('error:', lu.get('error'))
"
```

The `mode` field is the key diagnostic:

- **`pip install --upgrade`** — normal PyPI install. Failures here usually mean PyPI propagation lag (`result: no_change`) or a network issue.
- **`editable (git pull)`** — the agent was installed from a local git clone via `pip install -e .` instead of from PyPI. This is the legacy/dev setup and is the source of most upgrade failures (detached HEAD, missing branch, local commits, conflicts, etc.). **Convert it to a PyPI install** (see below).

## Convert an editable install to PyPI (the most common fix)

When `last_update.mode` is `editable (git pull)`, the agent's venv has a `pip install -e .` pointing at a local clone. To switch to PyPI:

```bash
ssh <host>
~/.coord-venv/bin/pip uninstall -y code-coordinator
~/.coord-venv/bin/pip install --upgrade code-coordinator
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-agent
```

> **The `XDG_RUNTIME_DIR=/run/user/$(id -u)` prefix is load-bearing.** A bare `systemctl --user restart` silently no-ops in a non-interactive / scripted SSH session — and the agent's own `/update` `os.execv` self-restart doesn't take under systemd either (#404: same PID, stale version). Always prefix the restart when driving it over SSH. This is the single most-recurring fleet failure; an `✗ did not come back` is usually this false negative.

After this, the `~/src/claude-coordinator` clone on that machine is no longer used by the agent and can be deleted. The next `coord agent update` will use the `pip install --upgrade` path, which doesn't depend on local git state.

Verify:

```bash
curl -s http://<host>:7433/health | python3 -c "import sys, json; d = json.load(sys.stdin); print(d['version'])"
```

## Manual restart (after editing files in-place)

```bash
systemctl --user restart coord-agent
```

The restart picks up whatever is currently installed in `~/.coord-venv` (re-reads from disk). Use this when the agent process is wedged or holding stale code that a `/update` couldn't replace.

## Watch the agent log

```bash
journalctl --user -u coord-agent -f
```

## Known issues

- **#280** (fixed in 0.4.11) — `/update` would crash on startup if a worktree directory had been cleaned out from under the agent, leaving the process on the old version even though pip succeeded.
- **Editable install on detached HEAD** — `git pull --ff-only` fails because there is no current branch. The fix is to convert to a PyPI install (above); don't try to repair the local git state on an agent machine.

## Adding the conversion to many machines at once

If you have several editable installs to convert, you can script it (assumes SSH is set up):

```bash
for host in precision elitebook dellserver; do
  ssh $host '~/.coord-venv/bin/pip uninstall -y code-coordinator && ~/.coord-venv/bin/pip install --upgrade code-coordinator && XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-agent'
done
```

## Operator Claude Code settings (driving the fleet)

When you drive the pipeline you typically have **several interactive Claude Code
sessions open at once** (one per story / smoke / review). Claude Code's **Agent
View** surfaces a cross-session roster — a `N awaiting input · N working · N
completed` header over "Needs input" / "Completed" lists — and it can **pop up
unprompted in the middle of typing** whenever some *other* session changes stage
(one completes, or starts awaiting input). That interrupt is disorienting
mid-task and easy to mistake for something the current session did.

Turn it off on the operator machine with one key in `~/.claude/settings.json`
(Claude Code's own global config — **not** anything under `~/.coord/`):

```json
{ "disableAgentView": true }
```

Equivalent env var: `CLAUDE_CODE_DISABLE_AGENT_VIEW=1`. **Restart Claude Code**
for it to take effect. This suppresses *only* the cross-session overview; every
other notification is untouched. (The blunter `"preferredNotifChannel":
"notifications_disabled"` silences all notifications if you ever want that.)

This is a Claude-Code-**client** preference: it lives per operator machine and is
**not** part of the coordinator's own config, the board, or the release flow.

## Passwordless SSH between coordinator and agents

`coord pull-artifact` rsyncs built binaries from the agent's
`~/.coord/artifacts/` directory over SSH.  The coordinator machine must be
able to `ssh <agent-host>` without a password prompt for rsync to work.

### One-time setup (per coordinator→agent pair)

**On the coordinator machine** (where you run `coord plan` / `coord pull-artifact`):

```bash
# 1. Generate a key if you don't already have one.
ssh-keygen -t ed25519 -C "coord-coordinator" -f ~/.ssh/id_ed25519_coord
# (or reuse an existing key — just make sure it isn't passphrase-protected,
#  or add it to ssh-agent and keep ssh-agent running)

# 2. Copy the public key to every agent machine.
ssh-copy-id -i ~/.ssh/id_ed25519_coord.pub <agent-host>
# e.g.:
ssh-copy-id -i ~/.ssh/id_ed25519_coord.pub precision
ssh-copy-id -i ~/.ssh/id_ed25519_coord.pub elitebook
```

**Verify:**

```bash
ssh precision true && echo "OK"
```

No password prompt → you're done.

### First-time accept (StrictHostKeyChecking)

On the very first SSH to a new agent, the client may prompt:

```
The authenticity of host 'precision' can't be established.
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

Accept once (`yes`) or pre-accept all coordinator-managed hosts so
unattended `coord pull-artifact` calls never stall:

```bash
# Option A: accept once interactively (safest)
ssh precision true

# Option B: add StrictHostKeyChecking=accept-new to ~/.ssh/config
# so the first connection auto-accepts but rejects changed keys.
cat >> ~/.ssh/config <<'EOF'

Host precision elitebook dellserver
    StrictHostKeyChecking accept-new
EOF
```

### Required file permissions

SSH is strict about key file modes.  If `ssh-copy-id` created the key,
permissions are already correct.  If you manage keys manually:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_ed25519_coord
chmod 644 ~/.ssh/id_ed25519_coord.pub
chmod 600 ~/.ssh/authorized_keys   # on each agent
```

### Design context

For background on why rsync-over-SSH was chosen over direct HTTP download
(signed URL vs key-auth tradeoffs, GC behaviour, TTL defaults), see the
original design in [GitHub issue #305](https://github.com/JDonaghy/code-coordinator/issues/305).
