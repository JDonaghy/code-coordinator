# Current Goal — North Star

> **The living, cross-repo / cross-machine objective for the coordinator and every agent it dispatches.**
> This is *meta-level*: above any single issue, repo, or session. Both humans and agents may edit it as
> priorities evolve — keep it short, current, and re-date the Status line. `coordinator.yml` is the source
> of truth for *topology*; **this file is the source of truth for *intent*.**
>
> _Last updated: 2026-09-11_

## 🎯 North star

**File an epic, and have it decomposed into issues and worked through to merge without an operator.**
One command in, working merged software out — decomposition, dispatch, test, review
and merge all unattended, across the fleet.

This replaces the previous north star (*"make human-attended interactive `claude` sessions
drivable end-to-end from the coord-tui board"*, 2026-06/07). That lifecycle was built and
merged — interactive Work/Plan/Review/Fix/Smoke launch from the board, tmux-resilient,
verdicts via `coord report-result` — and it is **no longer the direction**. Interactive is
a debugging and steering tool now, not the primary path. `claude -p` workers driven by
`coord drive` / `coord drive-queue` are the primary path. (The June-15 metering change that
originally motivated interactive-first was **paused** by Anthropic on 2026-06-19 and never
returned; `claude -p` still draws on the subscription.)

## Why this matters

Two reasons, and they are not the same reason:

1. **It is the scale constraint on everything else.** Every hour spent nursing a stalled
   queue is an hour not spent on the work the queue exists to do.
2. **It is the product.** The intended commercial motion is a **small-fee pilot**: a client
   describes what they want, and gets back real, working, tested software **in their own
   repo and their own cloud subscription** — not a sandbox demo. What converts them to a
   real engagement is seeing that artifact and saying *"that's nice, but what I really
   want is…"*. Autonomy is not what the client sees; **autonomy is what makes running
   twenty of those affordable.** So the funnel can open before the autonomy is finished,
   and the autonomy is what decides whether it has margin.

Corollary: do not try to beat the instant-demo app builders at instant demos. The
differentiator is that the client owns a real asset from week one — repo, tests,
deployment automation, keys — which a sandbox cannot give them.

## Where this actually stands — measured, not estimated (2026-09-11)

21-day window, **1,424 legs dispatched**: 857 `done`, 424 `merged`, 134 `failed`.

- **The 9.4% headline failure rate is one bug.** 111 of the 134 failures (83%) are a single
  issue (claude-coordinator#3230) spinning the Test stage against a branch that was never
  pushed. Strip it and the fleet's real failure rate is **1.6%** — thirteen failures across
  twelve active days. **The queue is working.**
- **Almost nothing that fails is pipeline logic.** Of the ~23 genuine failures, the
  identifiable causes are kill and environment signatures: `exit 143` (SIGTERM, killed
  mid-leg) ×4, `exit 137` (SIGKILL) ×1, `exit 127` (command not found — PATH) ×1, and 7
  fast-failing reviews on one host inside a 40-minute window (expired auth). **Zero are
  clearly defects in Work→Test→Review→Merge.**
- **The dominant failure class is the fleet, not the pipeline**: coord updates/rolls, and
  per-host environment drift.

**So the governing insight is this:** environment failures do not currently fail *cleanly* —
they corrupt pipeline state. A worker killed mid-leg is recorded `done` with zero commits
(#3305, and the original #1534 incident). A deferred restart scores CRIT and makes
`--rollback-on-red` revert the hosts that *succeeded*. A host that goes away should produce
a clean retry elsewhere; instead it produces a false completion the pipeline then builds on.
**That conversion — fleet event into corrupted state — is the thing standing between here
and an unattended epic.** It is a small, specific class of fix, and it is worth more than
any individual bug in the drive loop.

## Working rules that follow from the above

1. **A roll is the most dangerous routine operation on the fleet, and it is optional.**
   The delivery mechanism for reliability fixes is currently a top source of unreliability
   (2026-09-11: one day produced a fleet inversion via `--rollback-on-red`, a blue/green
   orphan, a symlink flipped without a restart, and an agent crashlooped into systemd's
   rate limit — none of them pipeline bugs). Stop `coord-drive-queue.timer` before rolling;
   use `--drain` and `--no-rollback-on-red`, never `--force`.
2. **Freeze the version for the duration of a client pilot.** Client work does not need the
   bleeding edge of coord; it needs one that works. Pin a known-good release, run the epic,
   roll afterwards. An epic's exposure is *duration × fleet-event rate* — and the dominant
   fleet event is one we choose to perform.
3. **Fleet events must fail clean.** Any change that turns a killed/unreachable worker into
   a clean retry instead of a false `done` outranks feature work on the drive loop.
4. **Instrument the failures.** 118 of 134 failed legs carry **no recorded reason at all**,
   and 120 exited `0`. What cannot be classified cannot be prioritised.
5. **Epic reliability compounds.** Unattended completion of an N-child epic is roughly
   per-issue reliability to the Nth power. Prefer smaller epics and shorter windows over
   heroic per-issue reliability.

## Near-term priority — prove it on a real project

The open question is no longer *can it run* — it is **whether what it produces is good
enough to sell, without a human in the loop**. That will not be settled on small
self-referential stories inside the tool that implements it.

- **Dogfood vehicle:** the [coord web control center](docs/WEB_CONTROL_CENTER.md) — a
  responsive React app (phone → 32" monitor) growing toward `coord-tui` parity and becoming
  the primary surface for anyone who is not the author. The web app is the **deliverable**;
  the **confidence** is the outcome. A dogfood vehicle that is a toy proves nothing, and a
  product built without instrumentation teaches nothing.
- **The greenfield asymmetry is real and underused.** Every epic-decompose leg so far has
  run against code-coordinator itself — sealed acceptance suites, oracle loops, capability
  routing across five machines, a merge queue, cross-repo codegen gates. A greenfield
  client web app has none of that. The plumbing bugs transfer; the per-child failure rate
  does not. **The client funnel may clear the autonomy bar well before this repo does** —
  so do not gate the funnel on this repo's numbers.
- **Standing protocol:** a dogfood story that surfaces a coord process bug **halts** — file
  it, fix it **with a test**, then resume. Shipping around a known process bug forfeits the
  evidence, which is half the point.
- **Scorecard:** first-pass acceptance rate, **human interventions per issue** (the number
  that matters most now), cost + wall-clock, escaped defects by stage.

## Horizon

- **Postgres** (#282) — the storage-agnostic DAO makes it a contained swap; sequencing
  against the board-loading redesign is an open decision.
- **Board loading redesign** ([`docs/BOARD_LOADING_REDESIGN.md`](docs/BOARD_LOADING_REDESIGN.md))
  — Stage 0 shipped (ETag 0/7 → 15/15 304s, cold `/board` 0.63s → 0.079s); Stage 1 dropped.
- **GitLab / pluggable issue stores** (#183) — lands inside the daemon.
- **Multi-tenant service** — monetization TBD; the small-fee pilot above is the nearer
  commercial step.

## How to use this doc

- **Agents / coordinator brain:** treat this as the standing objective behind all planning
  and triage. Bias proposals toward the north star above; don't silently drift to unrelated
  backlog.
- **Humans:** edit freely as priorities shift; keep it short, re-date the Status line. Commit
  + push so every machine and every agent picks it up (it propagates via git, like all
  coordinator state).
