# code-coordinator

CLI tool + per-machine agent server that coordinates Claude Code workers across multiple machines and repos over Tailscale.

> **Scope of this file (#2195).** This is the **worker- and reviewer-facing** rulebook: it is
> loaded into every worker leg, every review leg, and every coordinator session, so it holds
> only what someone *editing this repo* must act on. Operator runbooks live in
> [`docs/`](docs/) — see [Operational guides](#operational-guides--operator-facing) at the
> bottom. Settled design rationale lives in
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#design-decisions--the-settled-rationale).
> Keep it that way: if a new rule does not change what a worker does, it belongs in `docs/`.

> **Operator sessions: dispatch, don't do.** If you're the interactive coordinator session —
> not a dispatched `type=work`/`review` leg — and the user asks for a change in any repo listed
> in `coordinator.yml`, don't edit that repo's tree yourself: file a GitHub issue and `coord
> drive-queue add` it. That gets the change tested, independently reviewed, merged, and
> deployed; hand-editing it here skips all four. (Doc-only edits to *this* repo are the
> documented exception for *who* does the work — see
> [`docs/COST_DISCIPLINE.md`](docs/COST_DISCIPLINE.md) — **not** for *how* it lands: every commit
> on `main`, doc-only ones included, is squash-merged from a branch's PR, never pushed to `main`
> directly. Use `EnterWorktree`, branch, commit, push, open the PR, let it merge, then
> `ExitWorktree`.) Caught 2026-08-21: this exact rule used to live inline here, got moved to
> that linked doc by #2195, and was then skipped — an operator session hand-implemented a
> natal-chart feature directly instead of dispatching it. If you're a worker or reviewer leg,
> this paragraph isn't for you.

## Current Goal — read first

**[`GOAL.md`](GOAL.md) holds the current north-star objective** — the living, cross-repo / cross-machine goal that should bias all planning, triage, and dispatch. It is meta-level (above any single issue, repo, or session) and changes as priorities evolve: read it first, plan against it, and keep it current. `coordinator.yml` is the source of truth for *topology*; `GOAL.md` is the source of truth for *intent*.

## Codebase navigation — query the graph first

This repo ships a **graphify** knowledge graph in `graphify-out/` (`graph.json`,
`GRAPH_REPORT.md`), kept current on a best-effort basis by `post-commit` /
`post-checkout` git hooks. For any architecture / "where is this handled" / "what
calls this" / file-relationship question, **query the graph first** (the `graphify`
skill, or the graphify CLI) before reaching for grep/Read. Grep/Read are for
exact-string or line-level confirmation — not the first move.

**In a worktree the graph is the base checkout's, not yours.** `graphify-out/` is
gitignored (only its `.gitignore` is tracked), so `git worktree add` yields an empty
one — and `graphify query` resolves `graphify-out/graph.json` strictly relative to
cwd, with no upward walk and no `--graph` override. `.githooks/post-checkout`
therefore symlinks the *contents* of a worktree's `graphify-out/` (`graph.json`,
`manifest.json`, `cache/`, ...) at the base checkout's graph — the directory
itself, and its tracked `.gitignore`, are left alone so the worktree stays
`git status`-clean.
Consequences worth knowing while working in one: the graph reflects the **base
checkout's HEAD, not your edits** — trust it for *"where is X handled"*, never for
*"did my change land"* — and rebuilds are deliberately disabled inside worktrees
(a rebuild there would overwrite the shared graph from a feature branch). The
bootstrap only runs where `core.hooksPath` is set: `git config core.hooksPath
.githooks`, once per machine.

**The graph drifts, and the hooks cannot prevent it.** They `exit 0` during
rebase/merge/cherry-pick (so the merge agent's proactive rebase never rebuilds),
nothing fires at all for `git reset --hard`, every failure path is a silent
`exit 0` behind a detached 600s-timeout background process, concurrent triggers
coalesce, and their `[ ! -f graphify-out/graph.json ]` guard is an off-switch
once the graph is purged. Treat the hooks as an optimization and check
freshness instead — `GRAPH_REPORT.md` records its source commit, and
**`coord diagnose --graph`** compares that to HEAD for every local checkout (and
flags an unset `core.hooksPath`). If it reports STALE, `graphify update .` in that
checkout.

**It mostly heals itself now (#1729, #2237).** Each agent's health tick rebuilds
a checkout whose graph is stale *or absent* — so `rm -rf graphify-out/` is no
longer permanent — subject to four guards: idle machines only, base checkouts
only (never a linked worktree), once per HEAD (a build that cannot succeed is
never a retry loop), and never `--force`. What it cannot do is anything
versioned: `.githooks/` are tracked files, so porting them to a repo is a PR
against that repo.

**`coord repo doctor <repo>`** reports graph readiness **per machine** (folded
from each agent's `/health`, no extra round trip) — the operator's laptop is
usually not where the workers run. `--fix` performs the machine-local half
(`graphify update .` where absent, `core.hooksPath` where unset) on every
machine that clones the repo; it is idempotent, never writes a tracked file,
and refuses on a repo whose `.githooks/` were never ported. A repo with no
graph on *any* machine that runs workers is CRIT and fails the gate.

Setting this up on a new machine: [`docs/GRAPHIFY_SETUP.md`](docs/GRAPHIFY_SETUP.md).

## Architecture

```
coordinator.yml           — Single config file: repos, machines, dependencies
coord CLI                  — User-facing commands (plan, approve, assign, status, etc.)
coord agent (per-machine)  — HTTP server (port 7433) that runs claude -p
coord serve                — Board daemon (port 7435): canonical board state for thin clients
coord web                  — Lightweight dashboard (port 7434)
claude -p                  — The actual worker (runs locally on each machine)
GitHub issues              — Work source + message bus (via issue comments)
Tailscale                  — Networking between machines
```

Full walkthrough — the agent HTTP API, where each subcommand actually runs, the auto-loop end
to end: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Project Structure

Query the **graphify graph** (`graphify-out/`) for the full module map + relationships — it's authoritative and auto-updated. Key entry points: `coord/cli.py` (Click CLI + all subcommands), `coord/agent.py` (`AgentServer`: `claude -p` subprocess mgmt) + `coord/agent_app.py` / `coord/serve_app.py` (agent + board-daemon HTTP apps), `coord/brain.py` (planning), `coord/dispatch.py` (routing: POST to agents, briefings), `coord/review.py` (adversarial review), `coord/merge_queue.py` (merge sequencing) + `coord/reconcile.py` (board↔agent), `coord/state.py` (board persistence in `~/.coord/`), `coord/models.py` (dataclasses), `coord/config.py` (`coordinator.yml` parsing), `coord/dashboard/` (web dashboard + `webapp/` phone PWA). Tests: `tests/test_<module>.py` (pytest; fixtures in `conftest.py`).

## Commands

`coord <cmd> --help` documents every command + flags. The core loop:

```bash
coord plan                 # Brain proposes assignments for idle machines
coord approve 1,3          # Dispatch approved proposals (comma-separated IDs)
coord assign <machine> <repo> <issue> [--briefing TEXT | --briefing-file F] [--dry-run]  # Direct dispatch
coord status [--freshness] # Machines, assignments, connectivity (+ repo freshness vs GitHub HEADs)
coord log <id> [-f] [--machine NAME]            # claude -p output (remote logs need --machine)
coord notify               # Poll agents, post completion/failure comments to GitHub
coord test --passed|--fail|--skipped <id>       # Record the Test-gate verdict (bare `coord test <id>` builds+tests locally)
coord merge [--dry-run] [--repo NAME] [--method rebase|squash|merge] [--order IDs] [--force-merge]
coord reconcile-merges     # Backfill missing branches + record out-of-band merges (#609/#611)
coord retry|stop|resume <id>                    # Recovery; `coord done` ends the session
```

Setup / diagnostics (discoverable via `--help`): `coord init`, `coord config`, `coord agent`, `coord serve`, `coord web`, `coord diagnose`, `coord sessions [--remote]`, `coord split`, `coord notifier`, `coord repo add` / `coord repo doctor`.

## Development

Always work in a virtualenv. Agent workers are spawned with the agent's own
pinned venv (`~/.coord-venv` — the live fleet install, not a build artifact)
stripped from `PATH` and `PIP_REQUIRE_VIRTUALENV=true` set (#402, hardened by
#2569), so a `pip install` run **without first creating and activating your
own venv** fails closed instead of silently resolving somewhere unintended
— and must **never** target the agent's runtime venv. Create your own venv
in the checkout (`.venv/` is gitignored):

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
pytest
coord plan --dry-run
coord approve --dry-run 1,2
coord assign --dry-run precision claude-coordinator 42
```

**Never run a bare `pip install` in a worktree as a separate command from
venv creation (#2569).** The line above is ONE chained command
(`venv && activate && install`) on purpose — an 11h fleet outage started
when a worker ran only `pip install -e ".[dev]"` on its own, split off from
the `venv`/`activate` step, and it silently landed in the live
`~/.coord-venv` instead of failing. If you ever need to re-run `pip install`
later in the same session, first confirm you're in an activated venv
(`echo $VIRTUAL_ENV` should print your worktree's `.venv`, not empty) —
never assume activation from an earlier command carried forward.

**Workers: scope your test run to your diff, never `pytest` bare (#2169).**
The full suite exceeds Claude Code's 600s Bash ceiling on this repo and
duplicates the Test stage + CI, which both run it against your pushed SHA
regardless. Run just the file(s) that mirror what you changed — `pytest
tests/test_<module>.py` for a `coord/<module>.py` change, `cargo test` from
`tui/` scoped with a test-name filter for a `tui/**` change. To see what
the Test stage itself would run for your diff (and confirm you're not
missing a suite), `scripts/coord-test-runner.sh <worktree> --print-routing`
computes the routing without actually building or testing anything.

## Working on `tui/` — the `quadraui` pin

**`coord-tui` pins `quadraui` to a git rev in `tui/Cargo.toml`**
(`quadraui = { git = "https://github.com/JDonaghy/quadraui", rev = "<sha>" }`, #1973) —
never edit that `rev` as a shortcut for co-development, and never build `tui/` against
whatever happens to be checked out in `~/src/quadraui`; a quadraui merge broke coord-tui's
build with zero coord-tui commits and no warning once already, which is exactly what the pin
prevents. Bumping the pin deliberately, and building against an unmerged quadraui branch/PR
without touching the pin, are both procedures — see the `tui-quadraui-workflow` skill
(`coord/skills/tui-quadraui-workflow/SKILL.md`) for the steps; install it with `coord
install-skills` if it isn't already on this machine.

## Key Design Decisions

One line each — the reasoning behind them is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#design-decisions--the-settled-rationale).

- **No API key needed.** Everything uses `claude -p`, which runs on a Max/Pro subscription via OAuth.
- **Agent servers are dumb dispatchers.** They spawn `claude -p` and track the subprocess; all intelligence is in the coordinator brain.
- **GitHub issue comments are the message bus.** Briefings, completion notices and failure reports are comments, carrying `<!-- coord:... -->` markers for machine parsing.
- **`coordinator.yml` is the single source of truth** for repo topology, machine capabilities, dependencies, concurrency limits, review settings, smoke-test rules, and the pipeline gate order (`pipeline.default_gates`). It lives in `~/.coord/`, **not** the repo checkout.
- **User approves everything.** `coord plan` proposes, the user reviews, `coord approve` dispatches. No autonomous dispatch.
- **Claim detection prevents duplicate work.** Before dispatching, the coordinator checks the board for active assignments and the remote for `issue-{N}-*` branches.
- **Conflict rules are inferred, not configured.** There is no `file_groups`/`exclusive_files` config and never will be. The `coord plan` path infers overlap via a line in the planning brain's *prompt* (a prompt instruction, not a mechanism). The drive queue bypasses the brain, so it has its own inference: `coord drive-queue add` compares the issue's own `## Files` declaration against the **real diffs** of in-flight branches in that repo (`coord/overlap_predict.py`, #2247) and, on an overlap, chains the newcomer `--after` the incumbent. **It ORDERS, never refuses** — a false positive costs latency, a refusal costs work — and an issue that declares nothing gets exactly the pre-#2247 behaviour. Accuracy is measured, not assumed: `coord drive-queue overlap-report` scores each prediction against what the branches actually touched.
- **Adversarial reviews are rule-enforcing, not rubber-stamping.** On worker completion a fresh `claude -p` session on a *different* machine reviews the PR diff against this file and the review checklist, with zero shared context with the worker — that's the whole point.
- **The pipeline order is `Work → Test → Review → Merge`.** Test precedes Review; the headless auto-loop holds review dispatch until there is a `passed`/`skipped` test verdict.
- **Merge is gated on CI checks (#240).** `coord merge` refuses when a check failed or is still running — and a PR with *zero* reported checks is not automatically clear either (#1904).
- **Mechanical merge conflicts auto-rebase (#241).** A `type="conflict-fix"` worker rebases and resolves additive merges; semantic conflicts are left for a human.
- **Smoke tests validate on capable hardware.** `smoke_tests.capability_rules` map changed files to required machine capabilities (e.g. GTK changes → a machine with GTK).
- **Progress streaming from workers.** Workers emit `STATUS:` and `STUCK:` lines; the coordinator parses these for real-time progress in `coord status` and the dashboard.
- **The notifier tells you when NOBODY IS COMING — and nothing else (#1632).** Advisory, isolated, off by default; it is not an error channel and not a progress feed.

## Review Prompt Assembly

The reviewer gets a prompt built from:
1. **Repo's CLAUDE.md** — the project rules (source of truth, not duplicated)
2. **Generic checklist** — "did you add tests?", "did you stay in file scope?", "any security issues?"
3. **Repo overrides** — project-specific patterns from `coordinator.yml` `reviews.repo_overrides`
4. **The diff** — `gh pr diff` of the worker's branch vs base
5. **The issue** — title and body for intent verification

The reviewer reads the rules and enforces them against the diff. It does not have the worker's session context — genuinely independent.

## Rules for workers

- **Only the coordinator writes docs.** Workers must **not** update README, CHANGELOG, or shared documentation files — parallel doc edits cause merge conflicts. If a briefing lists docs in `files_forbidden`, respect it. An issue whose *entire* deliverable is a doc edit is coordinator work and should never have been dispatched; say so and stop rather than editing the doc.
- **Never edit the sealed suite — which is `tests/acceptance/**` *plus every declared driver entrypoint*.** Those suites are delivered read-only / run-only; write your own unit and internal tests instead. **In this repo the sealed set includes `tui/tests/acceptance.rs`** — it is the `tui-tuidriver` driver's `entrypoint:`, and an entrypoint is sealed as a whole file, so it is sealed despite living nowhere near `tests/acceptance/`. Do not be reassured by its prologue calling itself "a seam smoke test": *any* `type="work"` diff touching a sealed path is an **unconditional, mandatory `request-changes`** (`coord/review.py`), and the additive-only carve-out applies only to `test-author` / `mock-author` dispatches. TuiDriver tests belong **in-crate** under `#[cfg(test)]` anyway — see the coord-tui section above. The authoritative list is `AcceptanceConfig.sealed_paths()` in `coord/config.py`; if a briefing's `## Files` names a sealed path, that briefing is wrong — say so and put the test somewhere else rather than following it.
- **Stay in file scope.** If you must touch a file outside your briefing, note it in your final message.
- **Commit and push before your final message** — even if the build is broken or you ran out of time. Uncommitted work is destroyed when the session ends.
- **`gh` is on the deny-list.** The coordinator owns all GitHub interaction; use plain `git`.

The operator-side counterparts to these rules (dispatch economics, what to send to a worker
versus keep in the coordinator session) are in
[`docs/COST_DISCIPLINE.md`](docs/COST_DISCIPLINE.md).

## Rules for the coordinator session

- **Prefer `coord issue` / `coord repo` / `coord milestone` over raw `gh`.** Workers can't reach
  `gh` at all (deny-listed, above); an interactive coordinator session technically can, but should
  still route issue/PR/milestone reads and writes through the `coord` seam wherever a subcommand
  covers it — that's what keeps the backend-agnostic forge seam
  ([`docs/FORGE_MIGRATION.md`](docs/FORGE_MIGRATION.md)) actually exercised instead of quietly
  bypassed by the one class of session most likely to reach for `gh` out of habit. Reads are
  covered too — `coord issue list` and `coord issue view` shipped in #2484, so a plain issue
  listing/search or an issue-plus-comments lookup has no excuse to reach for `gh`. Where no
  `coord` subcommand covers what's needed — still a real gap in places as of 2026-08 (e.g. #2643:
  `coord issue` can create/edit/label/close but cannot post a plain comment on an issue that stays
  open) — falling back to `gh` for that one call is fine, but say so and treat it as a gap to flag
  or file, not a silent workaround.

## Testing — black-box coverage is the acceptance bar

**Every PR that changes user-visible behavior must ship a black-box test** that drives the *running app* and asserts on its rendered output — not just unit tests on internal functions. The adversarial reviewer reads this file and **rejects behavior-changing PRs that lack one** (pure refactors / internal-only changes are exempt — say so in the PR if that applies). Build the **harness once per repo**; add **tests incrementally, one (or a few) per behavior-changing issue** — do *not* big-bang a full suite. Coverage then grows with churn and ratchets up (PRs add coverage, never remove it). Keep a thin **core smoke set** over the few most-trafficked screens so critical flows stay guarded even by unrelated changes.

> **The acceptance suite is becoming an independent, sealed *oracle* (2026-07-04, [`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md)).** In an oracle-loop milestone the worker **no longer authors** the acceptance tests — an independent `test-author` agent writes them from a mock-first Gate-A contract, and they are delivered to the worker **read-only / run-only** (`coord acceptance run --issue N`). The worker iterates against them **in its own warm session** until green (the tight loop), then the coordinator re-runs the sealed suite **externally** against the pushed SHA as the trust gate. The runner sits above **pluggable framework drivers**, declared per repo in `acceptance.drivers` and routed to a capability-matched machine. **Implemented today: `tui-tuidriver`** (quadraui `TuiDriver`, the coord-tui case below), **`cli-pytest`**, and **`web-playwright`** (#1539) — see `SUPPORTED_KINDS` in `coord/acceptance_drivers.py`, which is the source of truth. **Native is NOT implemented**: a kind may be *declared* in `coordinator.yml` ahead of its adapter, but `run_driver()` rejects it with a "not yet implemented" `DriverError` rather than silently no-opping. What still gates the webapp is **not** the driver but its inputs — the deterministic fixture server (#1538) and a machine whose `browser` capability actually probes as met (#1678); both milestone #51 / epic #1537 ([`docs/WEB_CONTROL_CENTER.md`](docs/WEB_CONTROL_CENTER.md)). Workers still write their **own unit/internal tests**; they must **never** edit `tests/acceptance/**`.

**How it runs:** black-box tests are part of the repo's normal test command, so the **Test stage** executes them on a capability-matched machine — `smoke_tests.capability_rules` route platform-specific suites to capable hardware (a GTK box; a machine with a browser). Favor the automated pre-review gate; the point is to trust the suite so manual/interactive smoke (incl. driving from a phone) is rarely needed.

### coord-tui — quadraui `TuiDriver` (harness shipped: #690 / #691)
- Drives the whole app through the real `event → handle → render` path against ratatui's headless `TestBackend` and asserts on the screen grid. `cargo test`-native, deterministic, no TTY.
- `CoordApp` implements quadraui's `ShellApp`, so use `quadraui::tui::testing::driver_with_shell(app, CoordApp::shell_config(), w, h)`. API: `find("text")` → coords, `click(x, y)`, `press`, `type_char`, `screen()`, `screen_contains(needle)`. **Locate targets with `find` — never hardcode coordinates.**
- **Reuse the existing fixtures** — `make_test_app(data: BoardData) -> CoordApp` (and `make_app_with_assignments`, `make_app_with_one_completed_issue`, …) in `tui/src/app.rs` build a full app from in-memory `BoardData`, no live daemon. Put the tests **in-crate** (`#[cfg(test)]`), **not** in `tui/tests/` — the fixtures are `#[cfg(test)]`/private and an integration-test crate can't see them.
- Limit: `TuiDriver` renders to `TestBackend`, so it does **not** parse real ANSI — terminal-protocol bugs (raw-mode, SGR mouse, the embedded `claude` PTY pane) are out of reach and still need a live smoke. A native pty + vt100 tier is tracked in quadraui#302 (unbuilt).

### coord web (Phone Control Center) — shipped v1 (#700–#703); browser E2E forthcoming
- The phone web app lives in `coord/dashboard/webapp/` (React / Vite / TS PWA, served by `coord/dashboard/server.py`). **v1 milestone (#700–#703) is shipped.** See [`docs/PHONE_WEBAPP.md`](docs/PHONE_WEBAPP.md) for the full runbook (build → serve → phone access over Tailscale).
- **Build the React bundle before first use** — `dist/` is gitignored. From `coord/dashboard/webapp/`: `npm install && npm run build`. Re-run after pulling changes to `src/`. The server falls back to the legacy `index.html` when `dist/` is absent so existing behaviour is unchanged without a build.
- **Run on the always-on host:** `coord web` binds `0.0.0.0:7434`. Reach it from a phone via the Tailscale MagicDNS name: `http://dellserver:7434` (replace with your host's name). Install as a PWA via "Add to Home Screen" in Safari / Chrome.
- **Vitest unit tests** ship in `coord/dashboard/webapp/src/components/__tests__/` — run with `npm test` inside `coord/dashboard/webapp/`.
- **Playwright E2E tests** are the acceptance bar: start the dashboard server against a seeded board (the web parallel of `make_test_app(BoardData)`), drive a real headless browser, assert on the rendered DOM. Browsers headless-test more cleanly than terminals, so the webapp should lean almost entirely on this automated gate rather than interactive smoke.
  - **Shipped:** `playwright.config.ts` + specs in `coord/dashboard/webapp/e2e/` (`npm run test:e2e`), and `smoke_tests.capability_rules` already routes `coord/dashboard/webapp/**` → the `browser` capability, so the **Test stage** lands on a browser-capable machine.
  - **Shipped:** the `web-playwright` acceptance driver (#1539) — it is in `SUPPORTED_KINDS`, so `coord acceptance run/record` can drive the webapp.
  - **Not shipped:** the seeded-board fixture server (#1538) — today's specs run against whatever the live fleet is doing, so they are a smoke net, **not** a deterministic oracle. Milestone #51 / epic #1537.
  - **`browser` is advertised by one machine (elitebook)** until #1541 adds it to dellserver — so all web testing currently funnels through the dev box. **And as of 2026-08-01 that machine's `browser` probe reads UNMET (#1678)**, so `dispatch_smoke` refuses to route any `coord/dashboard/webapp/**` change and the Test stage silently retries forever — check `coord doctor` before assuming a webapp change can be tested at all.

## Conventions

- Python 3.12+, type hints everywhere
- Click for CLI
- httpx for HTTP client, Starlette + uvicorn for HTTP server
- PyYAML for config
- No Anthropic SDK — all Claude interaction is via `claude -p` subprocess
- Tests use pytest with fixtures in conftest.py
- State files go in `~/.coord/` — including `coordinator.yml` (canonical: `~/.coord/coordinator.yml`; override with `$COORD_CONFIG` or `--config`; `./coordinator.yml` is a dev fallback)
- Agent server port: 7433, dashboard port: 7434, board daemon port: 7435
- GitHub issue comments carry `<!-- coord:event=... assignment=... -->` markers for machine parsing

## Operational guides — operator-facing

**Not needed to work on this repo.** These are runbooks for driving the fleet; a worker or
reviewer can stop reading here.

**Read before operating the fleet:**

- [`docs/OPERATING_GOTCHAS.md`](docs/OPERATING_GOTCHAS.md) — **READ BEFORE OPERATING.** Traps that each cost a real dispatch, real money, or real lost work, and are invisible from the code. The headline: **a merged fix is not a live fix.**
- [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md) — releases, propagation, the deploy lanes, agent installs, and the **`~/.coord-venv` must-be-PyPI invariant**. **Read end-to-end before touching any agent install** — don't re-derive it.
- [`docs/DRIVE_QUEUE.md`](docs/DRIVE_QUEUE.md) — the durable, board-backed driver (`coord drive-queue`, #1750). **Read the top section before queuing more than ~2 issues on one repo.**

**Reference:**

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how it all fits together, plus the settled design rationale. Two diagnostic entry points: [why a merge/review isn't happening](docs/ARCHITECTURE.md#when-a-merge-isnt-happening) (**check here first when "Go does nothing"**) and [why an issue is in the Pipeline you never dispatched](docs/ARCHITECTURE.md#when-an-issue-is-sitting-in-the-pipeline-you-never-dispatched).
- [`docs/COST_DISCIPLINE.md`](docs/COST_DISCIPLINE.md) — dispatch economics; what to send to a worker versus keep in the coordinator session.
- [`docs/NOTIFIER.md`](docs/NOTIFIER.md) — the "nobody is coming" push channel (`coord notifier`).
- [`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md) — sealed acceptance suites, Gate-A contracts, the test-author agent.
- [`docs/PHONE_WEBAPP.md`](docs/PHONE_WEBAPP.md) — Phone Control Center v1 runbook and the `/api/pipeline` surface.
- [`docs/GRAPHIFY_SETUP.md`](docs/GRAPHIFY_SETUP.md) — installing the knowledge graph on a new machine (four layers, all of which fail *silently*).
- [`docs/EPHEMERAL_WORKERS.md`](docs/EPHEMERAL_WORKERS.md) — on-demand Azure worker VMs per epic. **The tailnet ACL is the security boundary** — `agent_app.py` has no authentication.
- [`docs/MAC_MINI.md`](docs/MAC_MINI.md) — adding a Mac mini; sizing, provisioning, and what non-macOS work routes there. The port itself is [`docs/CROSS_PLATFORM.md`](docs/CROSS_PLATFORM.md) (milestone #39).
- [`docs/WSL_WINDOWS_WORKER.md`](docs/WSL_WINDOWS_WORKER.md) — using a Tailscale-connected WSL2 box as the Windows worker for quadraui/vimcode's Win-GUI ports. Not the coord-itself Windows port.
- [`docs/FORGE_MIGRATION.md`](docs/FORGE_MIGRATION.md) — surviving a forge outage (cheap) versus leaving a forge (expensive); milestone #58 / epic #1902.

**Two deploy facts that bite most often:**

- **`coord-tui` ships as a locally-built binary, not via PyPI.** After a `tui/` PR merges, rebuild and reinstall locally: `cd tui && cargo build && cp target/debug/coord-tui ~/.local/bin/coord-tui`. Workers should not bump versions for tui-only changes.
- **Turn off Claude Code's Agent View when driving the fleet** — set `"disableAgentView": true` in `~/.claude/settings.json` (the Claude-Code *client* config, not `~/.coord/`; restart to apply), or its cross-session roster pops up mid-type when another session changes stage. Full note in [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md#operator-claude-code-settings-driving-the-fleet).
