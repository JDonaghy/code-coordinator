# code-coordinator

[![PyPI](https://img.shields.io/pypi/v/code-coordinator)](https://pypi.org/project/code-coordinator/)
[![Python](https://img.shields.io/pypi/pyversions/code-coordinator)](https://pypi.org/project/code-coordinator/)
[![Tests](https://github.com/JDonaghy/code-coordinator/actions/workflows/test.yml/badge.svg)](https://github.com/JDonaghy/code-coordinator/actions/workflows/test.yml)
[![License: FSL-1.1-MIT](https://img.shields.io/badge/license-FSL--1.1--MIT-blue)](LICENSE)

Coordinate a fleet of Claude Code workers — and human-attended interactive `claude` sessions —
across multiple repos and machines, from a single board.

Claude Code is excellent at one task at a time. Real projects have dozens of issues in flight,
spread across repos, each wanting its own session, its own scoping, and its own review.
`code-coordinator` runs many workers in parallel — one machine with worktrees, or several over
Tailscale — behind a coordinator that picks the model, avoids file conflicts, routes by
capability, and drives every issue through a gated **Work → Test → Review → Merge** pipeline.
Every stage can run as a cheap headless `claude -p` worker *or* a human-attended interactive
session you launch and steer from the board.

No Anthropic API key: it drives the `claude` CLI itself, billed on your existing Max/Pro
subscription. There's no orchestration daemon deciding what to do — a human approves every
dispatch — but once approved, a single command can carry one issue unattended from a GitHub
issue to a merged, reviewed, tested PR.

## This repo builds itself

`code-coordinator` is developed largely *by* code-coordinator. Every commit below is a worker's
own diff, produced through the Work → Test → Review → Merge pipeline this tool implements:

```
$ git log --oneline -5
14a2d930 fix(#2356 review): make the already-on-origin test actually exercise the fix
0b685d1c fix(#2356): don't FAIL an assignment whose commit already reached origin
35ed648c fix(#2335): route coord milestone dispatch bulk mode through the drive-queue
db3e5a50 fix(#2352): serialize coord acceptance record per (repo, issue)
e4ff1986 docs: propose a WSL2 Windows worker for quadraui/vimcode Win-GUI ports
```

~1,800 commits since the repo opened three months ago — roughly 164k lines of Python and 66k
lines of Rust (plus a 48k-line in-crate acceptance suite for the terminal UI), backed by over
10,000 test functions across 336 test files, with GitHub Actions cutting a new PyPI + binary
release on most merges to `main`. It's also used day to day to coordinate work across several
*other* repos — a Rust TUI framework, a couple of application codebases, and this one.

## The problem

Running one Claude Code session at a time is a bottleneck. You context-switch between issues,
lose session state, and can't parallelize. A complex issue gets one shot; if the session dies
mid-flight, you start over. There's no audit trail, no conflict detection, and no way to see
what happened last Thursday.

## The solution

One config file describes your repos and machines. Workers run in isolated git worktrees so
they never step on each other. The coordinator tracks what's in flight, prevents conflicts,
sequences PRs, and moves each issue through a gated pipeline. You approve the decisions; the
workers do the work.

Works on **one machine** with multiple worktrees. Add more machines over Tailscale for true
parallelism, capability routing (a GTK box, a browser box), or independent review.

## How it works

```
        ~/.coord/coord.db (SQLite)  ·  coordinator.yml  ·  GitHub (issues / PRs / comments)
                                        ▲
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
          coord CLI                coord-tui                 coord web
          (Python)                 (Rust board)              (phone PWA + REST)
              │                         │                         │
              └──────────── coord serve ─┴─ (optional daemon, port 7435) ──┘
                                        │  canonical board for thin clients
                                        │
                                        │  HTTP (port 7433)
                                        ▼
                                ┌────────────────┐
                                │  coord agent   │  one per machine
                                │  (HTTP server) │
                                └───────┬────────┘
                                        │ spawns
                        ┌───────────────┴───────────────┐
                        ▼                               ▼
              claude -p worker                 interactive claude session
              (headless, isolated worktree)    (human-attended, tmux)
```

Three kinds of process:

1. **Coordinator clients** — the CLI (`coord`), the terminal board (`coord-tui`), and the web
   dashboard / phone PWA (`coord web`). They read shared state and dispatch work. All three are
   **peers** of the same state, not layers — use whichever fits the task.
2. **Agent servers** — one `coord agent` per worker machine (port 7433). A dumb dispatcher: it
   spawns and tracks worker processes and owns the worktrees and logs. All the intelligence
   lives in the coordinator.
3. **Workers** — either a headless `claude -p` subprocess in an isolated worktree, or a
   human-attended interactive `claude` session in a named tmux session driven from the board.
   Both report their result through the same board seam.

State lives in SQLite (`~/.coord/`) plus GitHub issue comments — the durable message bus, where
every briefing, completion, failure, and review verdict is a comment carrying a
`<!-- coord:event=... -->` marker. Either can reconstruct the other. An optional control-center
daemon (`coord serve`) fronts one canonical DB so every client on the tailnet renders and drives
the same board.

## The pipeline: Work → Test → Review → Merge

Every issue moves through four gated stages, each runnable headless or interactively:

| Stage | What happens | Automated path | Interactive path |
|-------|--------------|----------------|-------------------|
| **Work** | Read the issue, write code, push a branch | `claude -p` worker | `coord assign --interactive` |
| **Test** | Build + run tests on capability-matched hardware; record a verdict | headless smoke assignment | `--smoke-of` testing agent |
| **Review** | A fresh, zero-context session reviews the diff against the repo's rules | `type="review"` worker on a *different* machine | `--review-of` reviewer |
| **Merge** | Rebase onto the base branch, resolve conflicts, run tests, merge | merge queue + auto-rebase | `--merge-of` merge agent |

Two things make the gate real rather than decorative:

- **Test precedes Review**, and Review is *held* until Test reports a `passed`/`skipped`
  verdict — a work item stuck at Pending Test gets no review and never merges. A green verdict
  is only as trustworthy as the suite behind it, so the Test stage prefers a repo's declared
  `ci_command` over a narrower default, to keep "Test passed" meaning what CI would say.
- **A failed test routes exactly like a request-changes review** — both drop back to a fix on
  the *same* branch, capped at a configurable retry count before escalating to a human, never
  to an orphaned branch.

`coord notify` drives the automated legs (review-on-completion, fix-on-request-changes,
re-review-on-fix) — or `coord drive` (below) does the whole thing as one durable, resumable
run.

## Quick demo

The fastest path — drive one issue unattended, end to end:

```bash
pip install code-coordinator
coord init                                  # detects repos, writes coordinator.yml
coord agent &                               # start the local worker dispatcher (port 7433)

coord drive myrepo 42 --model sonnet
# → dispatches Work, waits for the branch
# → runs Test on capability-matched hardware, records the verdict
# → dispatches an independent Review; on request-changes, loops a fix back through Work
# → rebases, resolves mechanical conflicts, and merges once Test + Review are both green
# exit 0: merged and verified against the remote default branch
```

`coord drive` is a resumable state machine over the board — re-running it on the same issue
picks up wherever the board actually is, so a killed terminal or a crashed daemon loses no
progress. For visibility into (or control over) each step, drive it by hand instead:

```bash
coord assign laptop myrepo 42 --model sonnet --briefing "Fix the auth middleware timeout"
coord watch <id>              # live filtered output (stream-json events)
coord test <id> --passed      # record the Test-gate verdict
coord pr <id>                 # open the PR + auto-dispatch an adversarial review
coord merge                   # once the review approves and CI is green
```

Prefer a terminal UI over raw commands? `coord-tui` launches every stage — automated or
interactive — from a pipeline row's right-click menu; see
[Driving from coord-tui](#driving-from-coord-tui).

## Quick start

### 1. Install

```bash
pip install code-coordinator
```

The `coord` CLI is now on your PATH. The same package provides the agent server
(`coord agent`), so the coordinator side and the worker side share one install.

> **Developing the coordinator itself?** Clone the repo and `pip install -e .`. Reserve
> editable installs for development machines — agent machines should stay on PyPI installs (see
> [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md)).

The terminal board, `coord-tui`, is a separate Rust binary — install a prebuilt release with
`coord tui update` (see [Driving from coord-tui](#driving-from-coord-tui)).

### 2. Configure

```bash
coord init        # interactive wizard: detects repos in cwd and ~/src/, writes coordinator.yml
coord config       # verify it parsed cleanly (prints the resolved config path)
```

Or copy `coordinator.example.yml` and edit by hand. `coordinator.yml` is gitignored — keep
secrets out of version control. Its canonical home is `~/.coord/coordinator.yml`, so the tool
runs on a machine with no repo checkout.

### 3. Start the agent server

```bash
coord agent &     # port 7433; auto-detects the machine from hostname
```

For anything beyond a quick trial, install it as a systemd user service (auto-restart, survives
reboots, separate worker logs) with `install-agent.sh` — see [Scaling up](#scaling-up), which
uses the same script for additional machines.

### 4. Coordinate

Three peer clients drive the same board — pick per task, they don't conflict:

**`coord-tui`** — a terminal board with a live pipeline, right-click actions, and one-key
stage-to-stage handoffs. See [Driving from coord-tui](#driving-from-coord-tui).

**The `coord` CLI directly:**

```bash
coord status                              # machines, assignments, connectivity
coord drive myrepo 42                     # or, step by step:
coord assign laptop myrepo 42 --model sonnet --briefing "Fix the auth bug"
coord watch <id>                          # live filtered output
coord test <id> --passed                  # record the Test-gate verdict
coord pr <id>                             # open the PR + adversarial review
coord notify                              # drive the auto-loop (run periodically)
coord merge                               # open + merge PRs in sequence
```

**The `/coordinator` slash command in Claude Code** — open Claude Code in the repo and type
`/coordinator` for guided setup, triage, dispatch, monitoring, and PR creation.

To share one board across every Tailscale host, run the control-center daemon (`coord serve`,
port 7435) on an always-on machine; the CLI and TUI then read from it as thin clients. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Driving from coord-tui

`coord-tui` is the rich terminal board: a live pipeline view, a machines panel, an embedded
terminal for interactive sessions, SSE log tailing, and a per-issue usage view. Interaction is
**right-click-first** — right-click a pipeline row for its context menu; `?` opens a help
overlay and command palette.

Install or update it with the coordinator itself — no Rust toolchain needed:

```bash
coord tui update      # downloads the release binary matching `coord --version`
coord tui status       # check the installed version against the coordinator's
```

(Building from source is only for developing the TUI itself, and it lives in its own
repo since #2899: `git clone https://github.com/JDonaghy/coord-tui && cd coord-tui && cargo build`.)

The whole lifecycle can also be driven as **human-attended interactive sessions** launched from
a pipeline row's right-click menu — a testing agent, a reviewer, a merge agent, or a fix worker,
each recording its verdict through the same board seam as the headless path. Every stage also
has a `claude -p` automation peer, so attended and headless stages mix freely, and review/fix
can run on a remote machine over ssh+tmux for reviewer independence or capability routing.

Full keyboard reference, right-click menus, and stage-handoff details: press `?` in the TUI, or
see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Milestones & epics

For work bigger than one issue, group issues under an **epic** — a GitHub tracking issue whose
body holds a `## Work order` block: a DAG of child issues (`- #762 {group: A, after: #761}`)
backed by GitHub's native sub-issues API. The issue pipeline above nests inside a **milestone
pipeline** so expensive gates are paid once per milestone, not once per issue:

| Gate | What it checks |
|------|-----------------|
| **A — contract** | A mock-first, black-box acceptance contract exists before any issue dispatches |
| **B — architecture** | An independent review confirms the *assembled* milestone matches the Gate-A contract |
| **C — acceptance** | The full accumulated acceptance suite is green (catches integration gaps between issues) |
| **D — ship** | Merges the milestone's feature branch to `develop`, gated on B (approved) and C (re-run live) |

```bash
coord milestone chat myrepo --new        # steward session: draft the milestone + Work order DAG
coord milestone dispatch myrepo <epic>   # dispatch the ready frontier; drain as deps clear
coord milestone drive myrepo <epic>      # walk all four gates as one durable, resumable run
```

`coord milestone drive` is the milestone-scale analogue of `coord drive` — a board-backed gate
record tracks which gate a milestone is in and what it's waiting on, so a restarted daemon
resumes exactly where it left off rather than re-running a gate it already cleared.

In an **oracle-loop** milestone, acceptance tests are no longer worker-authored: an independent
agent writes them from the Gate-A contract before any code exists, delivers them to the worker
**read-only**, and the worker iterates against them until green — the sealed suite is then the
external trust gate, not a test the worker could have written to pass itself. See
[`docs/PIPELINE_V2.md`](docs/PIPELINE_V2.md) and [`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md).

## Features

- **No API key** — drives `claude -p` on your Max/Pro subscription; billing stays per-seat.
- **Two ways to run every stage** — cheap headless `claude -p` workers *or* human-attended
  interactive sessions launched and steered from the board.
- **Single-machine first** — one agent server, many workers in isolated git worktrees; no
  Tailscale needed. Add machines for parallelism, capability routing, or remote review.
- **Gated pipeline** — Work → Test → Review → Merge, with Test gating Review and CI/review/smoke
  gating Merge.
- **One-command drive** — `coord drive` (one issue) and `coord milestone drive` (a whole
  milestone) walk the full pipeline unattended as durable, resumable state machines.
- **Model tiering** — haiku for docs, sonnet for standard work, opus for architecture; `coord
  fix` auto-escalates on failure.
- **Adversarial review** — a fresh, zero-context session reviews the diff against the repo's
  own rules; request-changes dispatches a fix on the same branch, then re-reviews, capped before
  escalating to a human.
- **Milestones & epics** — group issues under an epic with a `## Work order` DAG; amortize
  architecture and acceptance gates across the milestone; ship as one unit.
- **Merge queue** — dependency-aware sequencing, CI gating, auto-rebase of mechanical conflicts,
  and escalation of semantic ones.
- **Capability-aware testing** — routes platform-specific suites to capable hardware (a GTK box,
  a browser box) via declared machine capabilities.
- **Observability** — per-issue/repo/window cost and token usage, a durable audit trail, named
  reports folded out of it, stream-json log tailing, and live `STATUS:`/`STUCK:` progress lines.
- **Crash recovery** — board state reconciles against live agent and git state; interactive
  tmux sessions survive a TUI crash and are reattachable.
- **Web dashboard + phone PWA** — a board view and a React/Vite phone control-center, served
  over Tailscale.
- **~88 CLI subcommands** (100+ counting subcommand groups) covering the full surface above,
  each documented via `coord <cmd> --help`.

## Why this works (even with one machine)

The tool encodes a pattern from real multi-agent sessions: **separate the tech lead from the
IC.** The coordinator thinks about *what to do next* — priority, dependencies, conflicts, which
machine is idle. Workers think about *how to do this one thing*. Neither is distracted by the
other's concern.

- **Forced scoping.** One issue per worker session. No "while I'm here, let me also refactor
  this."
- **Structured handoffs.** Every assignment is a briefing posted as a GitHub comment. If a
  session dies, a new one resumes from the comment — zero context loss.
- **Persistent record.** Every decision, briefing, verdict, and result lives on GitHub. Review
  what happened a week later; terminal scrollback is gone when the window closes.
- **Fresh eyes.** Each worker starts with no prior context. Adversarial review takes it
  further: a separate session reviews with zero shared context — even on the same machine.
- **Human stays strategic.** You approve assignments and make judgment calls; you don't ferry
  messages between terminals or track who's touching which file.
- **Cost discipline.** Model tiering means no opus prices for a docs fix; auto-escalation
  starts cheap and pays more only when needed.

## Command reference

`coord <cmd> --help` documents every command and flag; `coord --help` lists all ~88. This is
the core workflow — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the rest (recovery,
observability, interactive-session flags, setup and diagnostics).

### Core workflow

| Command | Description |
|---------|-------------|
| `coord drive <repo> <issue>` | Drive one issue Work → Test → Review → Merge, unattended |
| `coord plan [--dry-run]` | Brain proposes assignments for idle machines |
| `coord approve <IDs>` | Dispatch approved proposals (comma-separated) |
| `coord assign <machine> <repo> <issue> [--model haiku\|sonnet\|opus] [--briefing TEXT]` | Direct dispatch, bypasses the brain |
| `coord status [--freshness]` | Machines, assignments, connectivity |
| `coord watch <id>` | Filtered live log (stream-json events) |

### Post-completion & merge

| Command | Description |
|---------|-------------|
| `coord test <id> [--passed\|--skipped\|--fail]` | Run, or record the verdict for, the Test gate |
| `coord pr <id> [--no-review]` | Open a PR (auto-dispatches an adversarial review) |
| `coord fix <id>` | Dispatch a fix-up worker for a failed test (auto-escalates model) |
| `coord notify` | Poll agents, post GitHub comments, drive the auto-loop |
| `coord merge [--dry-run] [--method rebase\|squash\|merge]` | Process the merge queue |

### Milestones & fleet ops

| Command | Description |
|---------|-------------|
| `coord milestone chat\|dispatch\|gate-b\|gate-c\|ship\|drive` | Draft, dispatch, gate, and ship a milestone |
| `coord drive-queue add\|list\|tick` | Durable, board-backed queue for `coord drive` |
| `coord usage` / `coord audit` / `coord report` | Cost + token usage, the durable event log, named reports over it |
| `coord doctor` / `coord repo doctor` | Fleet-wide prereq report — is a machine (or repo, per machine) fit to run work |
| `coord resume` / `coord diagnose` | Reconcile board state after a crash; diagnose a stuck stage |

### Model tiers

| Flag | Use for |
|------|---------|
| `--model haiku` | Docs, config, trivial single-file changes |
| `--model sonnet` | Standard features, bug fixes (default) |
| `--model opus` | Complex multi-file or architectural work |

`coord fix` escalates to the next tier on failure. Configure the ladder in `models.escalation`
and pin exact model ids per alias with `models.versions`.

### Ports

| Port | Service |
|------|---------|
| 7433 | `coord agent` — per-machine worker dispatcher |
| 7434 | `coord web` — web dashboard + phone PWA |
| 7435 | `coord serve` — control-center board daemon |

## Configuration

Minimal single-machine `coordinator.yml`:

```yaml
repos:
  - name: my-project
    github: owner/my-project
    default_branch: main
    build_command: "pytest"
    test_command: "pytest"

machines:
  - name: laptop
    host: localhost              # single machine: localhost works fine
    capabilities: [python]
    repos: [my-project]
    repo_paths:
      my-project: ~/src/my-project

concurrency:
  max_workers: 3                 # how many worker sessions run at once
  stagger_seconds: 30            # delay between dispatches (avoids rate limits)

models:
  default: sonnet
  escalation: [haiku, sonnet, opus]
  labels:                        # assign model by GitHub issue label
    documentation: haiku
    architecture: opus
```

`coordinator.example.yml` in the repo is the full annotated reference — multi-machine setups,
review checklists, smoke-test capability routing, CI gating, and the milestone feature-branch
model. `coordinator.yml` is gitignored; config resolves `$COORD_CONFIG` →
`~/.coord/coordinator.yml` (canonical) → `./coordinator.yml` (dev fallback). `coord config` and
`coord serve` print the resolved path so it's never ambiguous which file loaded.

## Scaling up

1. On the new machine, run the installer (venv + systemd service in one shot):
   ```bash
   curl -sSL https://raw.githubusercontent.com/JDonaghy/code-coordinator/main/install-agent.sh | bash -s -- --machine <name>
   ```
   No git clone needed — it pulls from PyPI.
2. Add the machine to `coordinator.yml` under `machines:` with its Tailscale hostname and
   capabilities.
3. `coord status` from the coordinator machine shows all machines and their connectivity.

For Tailscale setup, see [tailscale.com/kb](https://tailscale.com/kb/). The agent server only
needs port 7433 reachable on the tailnet — there's no authentication beyond the tailnet ACL, so
treat that ACL as the security boundary.

## Requirements

- Python 3.12+
- Claude Code CLI with a Max or Pro subscription
- `gh` CLI (authenticated, for coordinator-side GitHub operations — workers never touch `gh`)
- Tailscale — optional, only for multi-machine setups

## License

[FSL-1.1-MIT](LICENSE) (Functional Source License). Free to use, modify, and self-host for
internal use, non-commercial work, or in the professional services you provide to your own
clients using it. The one thing it restricts is re-packaging the software itself into a
competing product or service. Each release automatically relicenses to plain MIT two years
after publication.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the clients, agents, daemon, and workers
  fit together; the agent HTTP API; the auto-loop end to end; the merge-gate checklist.
- [`docs/PIPELINE_V2.md`](docs/PIPELINE_V2.md) / [`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md) —
  the two-tier milestone pipeline and the sealed-acceptance oracle loop.
- [`docs/DRIVE_QUEUE.md`](docs/DRIVE_QUEUE.md) — queuing multiple issues for unattended
  overnight drives.
- [`docs/PHONE_WEBAPP.md`](docs/PHONE_WEBAPP.md) — build and serve the phone control-center PWA
  over Tailscale.
- [`docs/ADR_COORD_WEB_DIST.md`](docs/ADR_COORD_WEB_DIST.md) — how a built `coord-web` bundle
  reaches the daemon host, and the alternatives rejected.
- [`docs/ADR_COORD_WEB_CI.md`](docs/ADR_COORD_WEB_CI.md) — which `coord` `coord-web`'s CI
  installs to boot `coord web --fixture`, why it tracks latest instead of pinning, and the
  `coord_web_ci_pin` health check that keeps that spec visible.
- [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md) — agent install, upgrade, and
  releasing to PyPI.
- [`docs/OPERATING_GOTCHAS.md`](docs/OPERATING_GOTCHAS.md) — traps that cost a real dispatch or
  real money and aren't visible from the code.
