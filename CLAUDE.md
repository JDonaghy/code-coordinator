# code-coordinator

CLI tool + per-machine agent server that coordinates Claude Code workers across multiple machines and repos over Tailscale.

> **Scope of this file (#2195, #2817).** This is the **worker- and reviewer-facing**
> rulebook: it is loaded into every worker leg, every review leg and every coordinator
> session — and re-read on **every turn** of each, so every byte here is a recurring
> fleet-wide cost. It holds only what someone *editing this repo* must act on.
> **If a new rule does not change what a worker does, it belongs in [`docs/`](docs/).**
> `tests/test_claude_md_budget.py` enforces that with a byte cap — move a section out
> rather than raise it.
>
> Not here: operator runbooks ([`docs/OPERATOR_GUIDES.md`](docs/OPERATOR_GUIDES.md)) ·
> settled design rationale
> ([`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#design-decisions--the-settled-rationale)) ·
> the `coord` command reference (`coord <cmd> --help`) · operator cost rules and
> "dispatch, don't do" ([`docs/COST_DISCIPLINE.md`](docs/COST_DISCIPLINE.md)) ·
> the current north-star objective ([`GOAL.md`](GOAL.md) — read it in a coordinator
> session when planning or triaging; a worker or reviewer leg does not need it).

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

`coord <cmd> --help` documents every command and flag — that is the reference, not this
file. The operator-facing core loop (`plan`/`approve`/`assign`/`status`/`merge`/...) is in
[`docs/OPERATOR_GUIDES.md`](docs/OPERATOR_GUIDES.md#the-core-loop). The one `coord` command
a **worker** runs is `coord acceptance run --issue N`, in an oracle-loop round (see Testing
below).

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
tests/test_<module>.py` for a `coord/<module>.py` change. To see what
the Test stage itself would run for your diff (and confirm you're not
missing a suite), `scripts/coord-test-runner.sh <worktree> --print-routing`
computes the routing without actually building or testing anything.

> **#2899: the TUI is not in this repo.** The `coord-tui` crate that used to live
> under `tui/` is now the standalone [`JDonaghy/coord-tui`](https://github.com/JDonaghy/coord-tui)
> repo, checked out at `~/src/coord-tui`. Its rules — the `quadraui` git-rev pin, the
> `TuiDriver` harness, its sealed `tests/acceptance.rs` — live in **its** CLAUDE.md,
> per this file's own scope rule above: a rule that does not change what someone
> editing *this* repo does, does not belong here. If your briefing asks you to change
> Rust, you are in the wrong checkout — say so rather than recreating `tui/`.
>
> Two seams still cross the split, and both run **from coord-tui's CI, pulling
> `code-coordinator` from PyPI** — never the other way round: `scripts/codegen.py --rust`
> (its `src/app/types/generated.rs` wire types) and `scripts/gen_board_fixture.py`
> (its `tests/fixtures/board_sample.json`). Both take `--out PATH` or `$COORD_TUI_SRC`
> and deliberately have **no fallback default**, so a missing checkout is an error
> rather than a gate that passes against nothing.

## Key Design Decisions

These are the ones a **diff** can violate. The full set, with the reasoning behind each, is
in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#design-decisions--the-settled-rationale).

- **`coordinator.yml` is the single source of truth** for repo topology, machine
  capabilities, dependencies, concurrency limits, review settings, smoke-test rules and the
  pipeline gate order (`pipeline.default_gates`). It lives in `~/.coord/`, **not** the repo
  checkout.
- **Conflict rules are inferred, not configured.** There is no `file_groups` /
  `exclusive_files` config and never will be — `coord drive-queue add` compares an issue's
  own `## Files` declaration against the **real diffs** of in-flight branches
  (`coord/overlap_predict.py`, #2247). **It ORDERS, never refuses.** Do not add such config.
- **The pipeline order is `Work → Test → Review → Merge`.** Test precedes Review; the
  headless auto-loop holds review dispatch until there is a `passed`/`skipped` verdict.
- **Adversarial reviews are rule-enforcing, not rubber-stamping.** A fresh `claude -p`
  session on a *different* machine reviews your diff against this file, with **zero shared
  context with you** — so your diff and final message must stand on their own.

## Rules for workers

- **Only the coordinator writes docs.** Workers must **not** update README, CHANGELOG, or shared documentation files — parallel doc edits cause merge conflicts. If a briefing lists docs in `files_forbidden`, respect it. An issue whose *entire* deliverable is a doc edit is coordinator work and should never have been dispatched; say so and stop rather than editing the doc.
- **Never edit the sealed suite — which is `tests/acceptance/**` *plus every declared driver entrypoint*.** Those suites are delivered read-only / run-only; write your own unit and internal tests instead. **In this repo, since #2899 moved `tui/` out, the sealed set is exactly `tests/acceptance/**`** — the `tui-tuidriver` `entrypoint:` that used to widen it (`tui/tests/acceptance.rs`, sealed as a whole file despite living nowhere near `tests/acceptance/`) went to the coord-tui repo with the crate, and is sealed *there*. The entrypoint rule itself has not changed, so do not assume a one-directory sealed set in any other repo. *Any* `type="work"` diff touching a sealed path is an **unconditional, mandatory `request-changes`** (`coord/review.py`), and the additive-only carve-out applies only to `test-author` / `mock-author` dispatches. The authoritative list is `AcceptanceConfig.sealed_paths()` in `coord/config.py` — it is derived from the configured driver `entrypoint:`, never hardcoded, which is why it followed the crate automatically; if a briefing's `## Files` names a sealed path, that briefing is wrong — say so and put the test somewhere else rather than following it.
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

**Not needed to work on this repo.** Every operator runbook is indexed in
[`docs/OPERATOR_GUIDES.md`](docs/OPERATOR_GUIDES.md).
