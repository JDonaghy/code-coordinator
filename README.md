# code-coordinator

[![PyPI](https://img.shields.io/pypi/v/code-coordinator)](https://pypi.org/project/code-coordinator/)
[![Python](https://img.shields.io/pypi/pyversions/code-coordinator)](https://pypi.org/project/code-coordinator/)
[![Tests](https://github.com/JDonaghy/code-coordinator/actions/workflows/test.yml/badge.svg)](https://github.com/JDonaghy/code-coordinator/actions/workflows/test.yml)
[![License: FSL-1.1-MIT](https://img.shields.io/badge/license-FSL--1.1--MIT-blue)](LICENSE)

**Run Claude Code as an engineering team, not a chat window.**

One Claude Code session is a capable engineer. Ten of them, left alone, are a mess: they
grade their own work, collide in the same files, forget everything when a session dies,
quietly widen their scope, and spend money without anyone noticing. code-coordinator is the
process around the agents. It gives each issue its own isolated worker, sends every change
through **Work → Test → Review → Merge** gates the worker cannot talk its way past, and keeps
the human where a human is actually needed: deciding what to build and making the judgment
calls.

It drives the `claude` CLI on your existing Max or Pro subscription. There is no API key and no
Anthropic SDK.

## What it has built

This is not a demo on toy tasks. Since May 2026 code-coordinator has been the way all of the
following were developed, by agents, with one human acting as tech lead:

| Project | What it is | Commits (co-authored by Claude) |
|---|---|---|
| [vimcode](https://github.com/JDonaghy/vimcode) | A Vim + VS Code hybrid editor in Rust: GTK4, native macOS, native Windows and terminal front ends, LSP, a debugger (DAP), a git panel, Lua extensions. [vimcode.org](https://vimcode.org) | 2,342 (2,056) since Feb 2026; 1,146 of them since code-coordinator took over in May |
| [quadraui](https://github.com/JDonaghy/quadraui) | The cross-platform UI framework under vimcode: one app codebase rendered to a terminal, GTK4, macOS and Windows. Its [independent audit](https://github.com/JDonaghy/quadraui/blob/develop/quadraui/docs/audits/FRAMEWORK_AUDIT_2026-09-26.md) is published unedited. | 1,314 (1,107) since Apr 2026 |
| code-coordinator | This repository. It builds itself through its own pipeline. | 2,763 (2,403) since May 2026 |
| [coord-tui](https://github.com/JDonaghy/coord-tui) | The Rust terminal board for code-coordinator, built on quadraui. | 710 (671) since May 2026 |

code-coordinator ships to PyPI on most merges to `main`: 512 releases so far, with about 7,500
Python test functions behind them.

## The traps, and what it does about each

These are the failure modes that showed up when running many agents for months. Each mechanism
below exists because the trap actually happened.

**1. The agent grades its own homework.** A worker that writes the code and the tests, then
reports "all tests pass", has told you very little.
- Review is done by a **separate session with zero shared context**, by default on a different
  machine. It reviews the diff against the repo's own `CLAUDE.md` rules and must return a
  structured verdict.
- In an **oracle-loop** milestone, acceptance tests are written by an independent agent from
  the milestone's contract *before* any code exists, and handed to the worker **read-only**.
  Any diff that touches the sealed suite is an automatic request-changes
  ([`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md)).

**2. "Done" is a claim, not a fact.** The Test stage re-runs the suite on hardware that matches
the change (a GTK box, a Windows box, a browser box) and records a verdict. Review is held until
Test passes. A verdict also records *how* it was obtained: one the worker recorded itself, or
one from a machine that only emulates the platform, is labelled as such on the board rather
than passed off as a clean pass.

**3. Agents collide.** Every worker runs in its own git worktree. Before an issue is queued, its
declared files are compared with the **real diffs** of branches already in flight, and the
queue orders overlapping work instead of letting it race. A merge queue rebases, resolves
mechanical conflicts, and escalates semantic ones.

**4. Sessions die, and take their context with them.** Every briefing, completion, failure and
verdict is a GitHub issue comment with a machine-readable marker. Board state lives in SQLite
and can be rebuilt from those comments. `coord drive` is a resumable state machine: kill the
terminal, restart the daemon, and re-running it picks up wherever the board actually is.

**5. Scope creep.** One issue per worker, briefed with the files it may touch and the files it
must not. Workers cannot touch GitHub at all (`gh` is on their deny-list), and only the
coordinator writes shared docs, because parallel doc edits are a merge-conflict factory.

**6. The bill runs away.** Model tiering (Haiku for docs, Sonnet by default, Opus for
architecture) with automatic escalation on failure. Fix rounds are capped before a human is
pulled in. Re-reviews look only at what changed since the last review. A request-changes review
with no blocking findings advances instead of triggering another fix. Per-issue cost and token
usage are recorded. The unattended overnight queue deliberately never re-runs stale test
suites by itself: that rule came from a token-burn incident on 2026-06-07.

**7. The rules are written down and then ignored.** The reviewer reads `CLAUDE.md` and enforces
it, so a rule there is a rule that gets checked. Because every agent re-reads that file on every
turn, it is kept small: a test enforces a byte budget, and anything that does not change what a
worker does lives in `docs/`.

**8. An agent breaks the machine it runs on.** Once, a worker ran `pip install` outside its own
virtualenv, landed in the fleet's live install, and caused an 11-hour outage (#2569). Workers
now start with the fleet's venv stripped from `PATH` and `PIP_REQUIRE_VIRTUALENV=true`, so the
same mistake fails closed.

**9. The human becomes a message bus.** You should not be copying output between terminals or
tracking who is editing which file. The board does that. You approve dispatches, and you see
the decisions that need judgment: escalations, dead ends and blocked queue entries, each with
the reason attached.

## What it does not solve

- **You are still the tech lead.** It does not decide what is worth building, and it escalates
  rather than guesses when a gate is ambiguous. Expect to spend your time on triage and design,
  not on watching sessions.
- **It still drops things.** Recent examples: an approved PR whose queue row had been removed
  sat unmerged with nothing alerting on it; and a library fix lands upstream while the issue to
  adopt it downstream is never filed. Known traps of this kind are written up in
  [`docs/OPERATING_GOTCHAS.md`](docs/OPERATING_GOTCHAS.md).
- **Gates are only as good as the tests behind them.** A suite that cannot see a bug cannot gate
  it, and a platform with no capable test machine cannot be gated at all.
- **It is a single-operator tool today.** It runs one person's fleet over Tailscale. There is no
  multi-user permission model beyond the tailnet's access controls.

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

- **Clients** (the `coord` CLI, the `coord-tui` board, the `coord web` dashboard) are peers over
  the same state; use whichever fits.
- **Agents**, one `coord agent` per machine, are deliberately dumb: they spawn and track workers
  and own the worktrees and logs. The decisions are made by the coordinator.
- **Workers** are either headless `claude -p` processes or human-attended interactive `claude`
  sessions in tmux. Every stage can run either way, and both report through the same board
  seam.

It works on **one machine** with several worktrees. More machines over Tailscale add
parallelism, platform-specific testing, and reviewers that are physically independent of the
worker.

### The pipeline

| Stage | What happens |
|-------|--------------|
| **Work** | A worker reads the issue and briefing, writes the code, and pushes a branch. |
| **Test** | The repo's suite runs on capability-matched hardware; the verdict is recorded. |
| **Review** | A fresh, zero-context session reviews the diff against the repo's rules. |
| **Merge** | The branch is rebased, re-checked against CI, and merged in dependency order. |

A failed test and a request-changes review route the same way: a fix on the *same* branch,
capped, then escalated to a human.

For work bigger than one issue, an **epic** holds a dependency graph of child issues, and a
**milestone pipeline** wraps it with four gates: a black-box acceptance contract before any
issue starts, an architecture review of the assembled result, the full acceptance suite, and a
ship gate. `coord milestone drive` walks all four as one resumable run
([`docs/PIPELINE_V2.md`](docs/PIPELINE_V2.md)).

## Quick start

```bash
pip install code-coordinator
coord init                     # detects your repos, writes ~/.coord/coordinator.yml
coord agent &                  # the local worker dispatcher (port 7433)

coord drive myrepo 42          # one issue, Work → Test → Review → Merge, unattended
```

To queue several issues and let them run, use `coord drive-queue add myrepo 42` and read
[`docs/DRIVE_QUEUE.md`](docs/DRIVE_QUEUE.md) first. To step through the stages by hand, use
`coord assign`, `coord watch`, `coord test`, `coord pr` and `coord merge`.

For a terminal board, install `coord-tui` with `coord tui update` (a prebuilt binary; no Rust
toolchain needed). Every stage, headless or interactive, can be launched from a pipeline row's
right-click menu. In Claude Code itself, `/coordinator` gives a guided setup, triage and dispatch
session.

`coord --help` lists every command, and `coord <cmd> --help` is the reference for each. The
operator's core loop is in [`docs/OPERATOR_GUIDES.md`](docs/OPERATOR_GUIDES.md).

### Minimal configuration

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
  max_workers: 3

models:
  default: sonnet
  escalation: [haiku, sonnet, opus]
  labels:                        # pick a model by issue label
    documentation: haiku
    architecture: opus
```

`coordinator.example.yml` is the full annotated reference: multiple machines, review rules,
capability routing for tests, CI gating and milestone branches. Config resolves from
`$COORD_CONFIG`, then `~/.coord/coordinator.yml`, then `./coordinator.yml`; `coord config` prints
which file it loaded.

### Adding machines

```bash
curl -sSL https://raw.githubusercontent.com/JDonaghy/code-coordinator/main/install-agent.sh | bash -s -- --machine <name>
```

This installs the agent from PyPI as a systemd user service. Add the machine to
`coordinator.yml` with its Tailscale hostname and capabilities, and `coord status` will show it.
The agent listens on port 7433 with no authentication beyond the tailnet, so treat your Tailscale
ACL as the security boundary.

## Requirements

- Python 3.12+
- Claude Code CLI with a Max or Pro subscription
- `gh` CLI, authenticated, on the coordinator machine (workers never use it)
- Tailscale, only for multiple machines

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how clients, agents, the daemon and workers fit together, and the reasoning behind the settled design decisions.
- [`docs/OPERATOR_GUIDES.md`](docs/OPERATOR_GUIDES.md) — index of operator runbooks.
- [`docs/PIPELINE_V2.md`](docs/PIPELINE_V2.md) and [`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md) — the milestone pipeline and the sealed-acceptance oracle loop.
- [`docs/DRIVE_QUEUE.md`](docs/DRIVE_QUEUE.md) — unattended queues, and their real cost model.
- [`docs/COST_DISCIPLINE.md`](docs/COST_DISCIPLINE.md) — what to dispatch and what to keep in the coordinator session.
- [`docs/OPERATING_GOTCHAS.md`](docs/OPERATING_GOTCHAS.md) — traps that cost a real dispatch or real money.
- [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md) — agent install, upgrade and releases.

## License

[FSL-1.1-MIT](LICENSE) (Functional Source License). Free to use, modify and self-host for
internal use, non-commercial work, or professional services you provide to your own clients. It
restricts only re-packaging the software itself as a competing product or service. Each release
becomes plain MIT two years after publication.

---

Built by [John Donaghy](https://github.com/JDonaghy), and by the agents it coordinates.
