# code-coordinator

CLI tool + per-machine agent server that coordinates Claude Code workers across multiple machines and repos over Tailscale.

> **Scope of this file (#2195).** This is the **worker- and reviewer-facing** rulebook: it is
> loaded into every worker leg, every review leg, and every coordinator session — and re-read on
> every turn of each — so it holds only what someone *editing this repo* must act on. Operator
> runbooks live in [`docs/`](docs/), indexed by
> [`docs/OPERATOR_GUIDES.md`](docs/OPERATOR_GUIDES.md). Settled design rationale lives in
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#design-decisions--the-settled-rationale).
> Keep it that way: **if a new rule does not change what a worker does, it belongs in `docs/`.**
> Operator sessions — the "dispatch, don't do" rule now lives in
> [`docs/COST_DISCIPLINE.md`](docs/COST_DISCIPLINE.md); read it before hand-editing any tracked
> repo. If you're a worker or reviewer leg, that one isn't for you.

## Current Goal — read first

**[`GOAL.md`](GOAL.md) holds the current north-star objective** — the living, cross-repo / cross-machine goal that should bias all planning, triage, and dispatch. It is meta-level (above any single issue, repo, or session) and changes as priorities evolve: read it first, plan against it, and keep it current. `coordinator.yml` is the source of truth for *topology*; `GOAL.md` is the source of truth for *intent*.

## Codebase navigation — query the graph first

This repo ships a **graphify** knowledge graph in `graphify-out/` (`graph.json`,
`GRAPH_REPORT.md`), kept current on a best-effort basis by git hooks. For any
architecture / "where is this handled" / "what calls this" / file-relationship
question, **query the graph first** (the `graphify` skill, or the graphify CLI)
before reaching for grep/Read. Grep/Read are for exact-string or line-level
confirmation — not the first move.

**In a worktree the graph is the base checkout's, not yours.** A worktree's
`graphify-out/` is symlinked at the base checkout's graph, so it reflects the
**base checkout's HEAD, not your edits** — trust it for *"where is X handled"*,
never for *"did my change land"*. Rebuilds are deliberately disabled inside a
worktree (a rebuild there would overwrite the shared graph from a feature
branch).

**The graph drifts, and the hooks cannot prevent it** — treat them as an
optimization, not a guarantee. Check freshness instead: `GRAPH_REPORT.md`
records its source commit, and **`coord diagnose --graph`** compares that to
HEAD for every local checkout. If it reports STALE, `graphify update .` in that
checkout.

Setup, the hook mechanics, self-healing, and `coord repo doctor`:
[`docs/GRAPHIFY_SETUP.md`](docs/GRAPHIFY_SETUP.md).

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

**Never run a bare `pip install` in a worktree as a separate command from venv
creation (#2569).** The line above is ONE chained command on purpose — a worker
that ran only `pip install -e ".[dev]"`, split off from the `venv`/`activate`
step, silently landed it in the live `~/.coord-venv` and cost an 11h fleet
outage. If you re-run `pip install` later in the same session, first confirm
`echo $VIRTUAL_ENV` prints your worktree's `.venv`, not empty — never assume
activation from an earlier command carried forward.

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
- **Conflict rules are inferred, not configured.** There is no `file_groups`/`exclusive_files` config and never will be. `coord drive-queue add` compares an issue's own `## Files` declaration against the **real diffs** of in-flight branches in that repo (`coord/overlap_predict.py`, #2247) and, on an overlap, chains the newcomer `--after` the incumbent. **It ORDERS, never refuses** — a false positive costs latency, a refusal costs work. Accuracy is measured, not assumed: `coord drive-queue overlap-report`.
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
- **Never edit the sealed suite — which is `tests/acceptance/**` *plus every declared driver entrypoint*.** Those suites are delivered read-only / run-only; write your own unit and internal tests instead. **In this repo the sealed set includes `tui/tests/acceptance.rs`** — it is the `tui-tuidriver` driver's `entrypoint:`, and an entrypoint is sealed as a whole file, so it is sealed despite living nowhere near `tests/acceptance/`. Do not be reassured by its prologue calling itself "a seam smoke test": *any* `type="work"` diff touching a sealed path is an **unconditional, mandatory `request-changes`** (`coord/review.py`), and the additive-only carve-out applies only to `test-author` / `mock-author` dispatches. TuiDriver tests belong **in-crate** under `#[cfg(test)]` anyway — see the coord-tui section below. The authoritative list is `AcceptanceConfig.sealed_paths()` in `coord/config.py`; if a briefing's `## Files` names a sealed path, that briefing is wrong — say so and put the test somewhere else rather than following it.
- **Stay in file scope.** If you must touch a file outside your briefing, note it in your final message.
- **Commit and push before your final message** — even if the build is broken or you ran out of time. Uncommitted work is destroyed when the session ends.
- **`gh` is on the deny-list.** The coordinator owns all GitHub interaction; use plain `git`.

The operator-side counterparts to these rules — dispatch economics, what to send to a worker
versus keep in the coordinator session, and the coordinator session's own
`coord`-seam-over-`gh` rule — are in [`docs/COST_DISCIPLINE.md`](docs/COST_DISCIPLINE.md).

## Testing — black-box coverage is the acceptance bar

**Every PR that changes user-visible behavior must ship a black-box test** that drives the *running app* and asserts on its rendered output — not just unit tests on internal functions. The adversarial reviewer reads this file and **rejects behavior-changing PRs that lack one** (pure refactors / internal-only changes are exempt — say so in the PR if that applies). Build the **harness once per repo**; add **tests incrementally, one (or a few) per behavior-changing issue** — do *not* big-bang a full suite. Coverage then grows with churn and ratchets up (PRs add coverage, never remove it). Keep a thin **core smoke set** over the few most-trafficked screens so critical flows stay guarded even by unrelated changes.

> **In an oracle-loop milestone the worker does NOT author the acceptance tests**
> ([`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md)). An independent `test-author` agent writes them
> from a mock-first Gate-A contract, and they are delivered **read-only / run-only** (`coord
> acceptance run --issue N`). You iterate against them **in your own warm session** until green;
> the coordinator then re-runs the sealed suite **externally** against your pushed SHA as the
> trust gate. You still write your **own unit/internal tests** — and must **never** edit
> `tests/acceptance/**`. Which framework drivers actually exist is `SUPPORTED_KINDS` in
> `coord/acceptance_drivers.py`, the source of truth: a kind may be *declared* in
> `coordinator.yml` ahead of its adapter, and `run_driver()` then rejects it rather than
> silently no-opping.

**How it runs:** black-box tests are part of the repo's normal test command, so the **Test stage** executes them on a capability-matched machine — `smoke_tests.capability_rules` route platform-specific suites to capable hardware (a GTK box; a machine with a browser). Favor the automated pre-review gate; the point is to trust the suite so manual/interactive smoke (incl. driving from a phone) is rarely needed.

### coord-tui — quadraui `TuiDriver` (harness shipped: #690 / #691)
- Drives the whole app through the real `event → handle → render` path against ratatui's headless `TestBackend` and asserts on the screen grid. `cargo test`-native, deterministic, no TTY.
- `CoordApp` implements quadraui's `ShellApp`, so use `quadraui::tui::testing::driver_with_shell(app, CoordApp::shell_config(), w, h)`. API: `find("text")` → coords, `click(x, y)`, `press`, `type_char`, `screen()`, `screen_contains(needle)`. **Locate targets with `find` — never hardcode coordinates.**
- **Reuse the existing fixtures** — `make_test_app(data: BoardData) -> CoordApp` (and `make_app_with_assignments`, `make_app_with_one_completed_issue`, …) in `tui/src/app.rs` build a full app from in-memory `BoardData`, no live daemon. Put the tests **in-crate** (`#[cfg(test)]`), **not** in `tui/tests/` — the fixtures are `#[cfg(test)]`/private and an integration-test crate can't see them.
- Limit: `TuiDriver` renders to `TestBackend`, so it does **not** parse real ANSI — terminal-protocol bugs (raw-mode, SGR mouse, the embedded `claude` PTY pane) are out of reach and still need a live smoke. A native pty + vt100 tier is tracked in quadraui#302 (unbuilt).

### coord web (Phone Control Center)
- The phone web app lives in `coord/dashboard/webapp/` (React / Vite / TS PWA, served by `coord/dashboard/server.py`). **Build the bundle before first use** — `dist/` is gitignored: `npm install && npm run build` from `coord/dashboard/webapp/`, re-run after pulling changes to `src/`. The server falls back to the legacy `index.html` when `dist/` is absent.
- **Vitest unit tests** live in `coord/dashboard/webapp/src/components/__tests__/` (`npm test`). **Playwright E2E** specs live in `coord/dashboard/webapp/e2e/` (`npm run test:e2e`) and are the acceptance bar — browsers headless-test more cleanly than terminals, so lean on this gate rather than interactive smoke.
- **Those E2E specs are a smoke net, not a deterministic oracle** — the seeded-board fixture server (#1538) is not shipped, so they run against whatever the live fleet is doing.
- **A webapp change may not be testable at all right now.** `browser` is advertised by one machine and its probe has read UNMET since 2026-08-01 (#1678), so `dispatch_smoke` refuses to route `coord/dashboard/webapp/**` and the Test stage retries forever — check `coord doctor` before assuming otherwise.
- Full runbook — serve, phone access over Tailscale, the `/api/pipeline` surface: [`docs/PHONE_WEBAPP.md`](docs/PHONE_WEBAPP.md).

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

## Operating the fleet — operator-facing

**Not needed to work on this repo.** A worker or reviewer can stop reading here. Every operator
runbook — the pre-flight gotchas, agent installs and releases, the drive queue, the customer
portal, and the two deploy facts that bite most often — is indexed in
[`docs/OPERATOR_GUIDES.md`](docs/OPERATOR_GUIDES.md).
