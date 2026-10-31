# code-coordinator

[![PyPI](https://img.shields.io/pypi/v/code-coordinator)](https://pypi.org/project/code-coordinator/)
[![Python](https://img.shields.io/pypi/pyversions/code-coordinator)](https://pypi.org/project/code-coordinator/)
[![Tests](https://github.com/JDonaghy/code-coordinator/actions/workflows/test.yml/badge.svg)](https://github.com/JDonaghy/code-coordinator/actions/workflows/test.yml)
[![License: FSL-1.1-MIT](https://img.shields.io/badge/license-FSL--1.1--MIT-blue)](LICENSE)

**A harness for running Claude Code on real projects, mostly unattended.**

I built this tool to help me develop [vimcode](https://github.com/JDonaghy/vimcode), and later
[quadraui](https://github.com/JDonaghy/quadraui), with Claude Code doing most of the work. I was
delighted by the early results — a basic functioning text editor in a terminal after a day, one
that was already starting to feel like vi (yes, I remember that incarnation, because I had to use
it in my first job and disliked it so much).

Yet after a few weeks of progress I realized things weren't going quite as well anymore. I was
targeting the terminal and GTK on Linux, and I discovered that Claude was reimplementing large
chunks of logic for both targets. I wanted one codebase for all backends, and I wasn't going to
get there by assuming Claude would just know what to do — not unless I was extremely explicit
about what I wanted. Testing wasn't going so well any more either: I would tell Claude what I
wanted and it would often only deliver after multiple rounds of interactive testing. Too painful
to be sustainable.

I realized I needed better tooling if I was ever going to get where I wanted, which was a vim-like
editor as slick as VS Code, that worked in a terminal and could also run as a native client on all
major operating systems. And I wanted a framework that could compete with Electron minus the web
stuff: cross-platform, feels native, highly responsive, and works in a terminal. So
code-coordinator was born from this desire.

Now I have a board of epics and stories with a 24/7 queue that works through them. Each issue gets
an isolated worker, every change runs through **Work → Test → Review → Merge** gates, and I get to
focus on the parts that only I can do: deciding what to build and the judgment calls around that.

I use the `claude` CLI on a Max 20x subscription — no API key, no SDK, though it supports BYOK.

## Where it's gotten me to

The receipts, with Claude Code doing the work and me acting as tech lead:

| Project | | Commits (Claude co-authored) |
|---|---|---|
| [vimcode](https://github.com/JDonaghy/vimcode) | The editor it all started with. [vimcode.org](https://vimcode.org) | 2,718 (2,371) since Feb 2026 |
| [quadraui](https://github.com/JDonaghy/quadraui) | The one-codebase-all-backends UI framework under it. Its [independent audit](https://github.com/JDonaghy/quadraui/blob/develop/quadraui/docs/audits/FRAMEWORK_AUDIT_2026-09-26.md) is published unedited. | 1,810 (1,517) since Apr 2026 |
| code-coordinator | This repository. It builds itself through its own pipeline. | 2,934 (2,463) since May 2026 |
| [coord-tui](https://github.com/JDonaghy/coord-tui) | The Rust terminal board for it, built on quadraui. | 712 (622) since May 2026 |

It ships to PyPI on most merges to `main` — nearly 800 releases so far.

## The problems, and what it does about each

These are the failure modes that showed up running many agents over months. Each mechanism
below is here because the problem actually bit me.

**1. The agent grades its own homework.** A worker that writes both the code and the tests, then
reports "all tests pass", doesn't tell you much.
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
one from a machine that only emulates the platform, is labelled as such on the board rather than
passed off as a clean pass.

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

- **You are still the tech lead.** It doesn't decide what's worth building, and it escalates
  rather than guesses when a gate is ambiguous. Expect to spend your time on triage and design,
  not watching sessions.
- **It still drops things.** Recent examples: an approved PR whose queue row had been removed
  sat unmerged with nothing alerting on it; a library fix lands upstream while the issue to
  adopt it downstream never gets filed. Known traps like these are written up in
  [`docs/OPERATING_GOTCHAS.md`](docs/OPERATING_GOTCHAS.md).
- **Gates are only as good as the tests behind them.** A suite that can't see a bug can't gate
  it, and a platform with no capable test machine can't be gated at all.
- **It's a single-operator tool.** It runs one person's fleet over Tailscale. There's no
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
