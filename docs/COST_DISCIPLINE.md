# Cost Discipline

**Audience: the operator (and the coordinator session).** Moved out of `CLAUDE.md` by #2195 —
no worker acts on any of it, and it was being loaded into every worker leg, every review leg,
and every coordinator session.

The one rule from this section that *is* worker-facing — **only the coordinator writes docs** —
stays in `CLAUDE.md`, because workers must obey it. It is restated at the bottom here for the
operator's half of the contract.

## The basic economics

The coordinator session (typically Opus) costs **~10x more per token** than Sonnet workers.
Minimize direct code work in the coordinator — instead, write a good briefing and dispatch it.

- **Dispatch, don't do.** If a task can be described in a briefing, send it to a worker.
  Reserve the coordinator session for triage, review, and decisions.
- **Workers are not cheap — dispatch is still right, but it costs dollars, not cents.** Measured
  2026-08-08→11 (394 legs, $907 total): a **work** leg averages **$4.64** on sonnet (median
  $3.61, ~23 min), **$7.30** on opus, **$7.89** on fable; a **review** leg averages **$1.61**, a
  **smoke** leg **$0.44**. The figure this line carried for a year — "$0.30–0.90 per task" — was
  stale by 5–15x, and every triage call was being made against it. An hour of Opus coordinator
  time still costs $40+, so "dispatch, don't do" holds; what changes is that a dispatched task is
  no longer free at the margin, so a *second* leg on the same issue is a real decision (see the
  rework note below).
- **Rework, not model choice, is the dominant cost.** In the same window 32 of 78 issues needed
  more than one work leg — 48 extra legs, **$187 (21% of all spend)** — and there were more
  review legs (133) than work legs (127), with 54 of those re-reviews (**$77**). Together the
  request-changes→fix loop is ~29% of spend, far outweighing the sonnet-vs-opus gap. Prefer one
  well-briefed leg over two cheap ones: the `--briefing` you skip is paid back at ~$4.64 plus a
  $1.61 re-review.
- **Compact aggressively.** Long coordinator sessions balloon cache reads. Use `/compact` when
  switching topics or after completing a batch of work.
- **Parallel workers, serial coordinator.** Dispatch multiple workers in parallel, then review
  results. Don't do two things at once in the coordinator session.

## Don't re-do the work you paid for

- **Trust the adversarial review.** When a review completes, read only the review comment — do
  not re-read the full PR diff to form an independent opinion. Summarize the reviewer's
  findings and ask the user how to proceed. Only read the diff if the review seems wrong or
  incomplete.
- **Audit before dispatching.** Include a step in briefings: "Before coding, verify this isn't
  already implemented." Workers have wasted full sessions building features that already
  existed.

## Dispatch traps that cost a whole round-trip

- **Never dispatch reviews via `coord assign`.** Workers have `gh` on the deny-list, so a
  worker dispatched with `coord assign` cannot run `gh pr diff` or `gh pr review`. Reviews must
  go through the review pipeline (`coord review`, or auto-dispatch on completion), which uses
  `type="review"` and grants GitHub access.
- **Catch platform violations at review time.** The adversarial reviewer should check for
  platform-specific code in shared/cross-platform paths. Catching after merge costs an entire
  round-trip.
- **One backend per issue for multi-backend repos (quadraui, vimcode).** A cross-backend
  feature becomes a **seam issue** (shared trait methods + one headless backend, typically TUI)
  plus **one issue per backend** (GTK, macOS, Windows). The seam issue touches only
  `backend.rs`, `testing/`, the TUI painter, and `todo!()`-style stubs in the other backends
  — never real painters for GTK/macOS/win. Each backend issue is verified only on its own
  host. Filing a single issue that spans all backends triggers the full smoke fanout matrix,
  balloons scope past 6 files, and has burned 3+ fix rounds without landing (quadraui#1539,
  2026-10-14; quadraui#1476, 2026-10-10). **Check the target repo's CLAUDE.md before filing**
  — quadraui's encodes this rule.

## Operator sessions: dispatch, don't do

If you're the interactive coordinator session — not a dispatched `type=work`/`review` leg — and
the user asks for a change in any repo listed in `coordinator.yml`, **don't edit that repo's tree
yourself**: file a GitHub issue and `coord drive-queue add` it. That gets the change tested,
independently reviewed, merged, and deployed; hand-editing it in the session skips all four.

Doc-only edits to **code-coordinator itself** are the documented exception for *who* does the
work (see the next section) — **not** for *how* it lands: every commit on `main`, doc-only ones
included, is squash-merged from a branch's PR, never pushed to `main` directly. Use
`EnterWorktree`, branch, commit, push, open the PR, let it merge, then `ExitWorktree`.

Caught 2026-08-21: this exact rule used to live inline in `CLAUDE.md`, got moved to this doc by
#2195, and was then skipped — an operator session hand-implemented a natal-chart feature directly
instead of dispatching it. It is restated at the top of `CLAUDE.md` as a one-line pointer for
exactly that reason. If you're a worker or reviewer leg, this section isn't for you.

## Only the coordinator writes docs — the operator's half

Workers must not update README, CHANGELOG, or shared documentation files; parallel doc edits
cause merge conflicts. Add docs to `files_forbidden` in briefings, and handle doc updates in
the coordinator session.

**The corollary is a dispatch rule: an issue whose entire deliverable is a doc edit can never
be completed by a worker, and must not be queued.** Workers read `CLAUDE.md`, so a worker
handed a doc-only task correctly refuses, exits clean with **0 commits**, and is reaped as
`advisory` — which the drive queue reads as a failed attempt. It burns both attempts and lands
in terminal `blocked`, and it will do so again on every retry.

Observed on **#2195 itself** (2026-08-14): worker `b1a090a5e011` spent 3 turns and $0.11 to
conclude that the rule forbidding the work "exists verbatim at CLAUDE.md line 156, and the
issue itself explicitly says this." That was the correct answer, and it would be the correct
answer every time. Doc-only issues are coordinator work by policy — do them in the operator
session, or split them so the worker does the code half and the coordinator does the doc half.

## Rules for the coordinator session

- **Prefer `coord issue` / `coord repo` / `coord milestone` over raw `gh`.** Workers can't reach
  `gh` at all (deny-listed — see `CLAUDE.md`'s "Rules for workers"); an interactive coordinator
  session technically can, but should still route issue/PR/milestone reads and writes through the
  `coord` seam wherever a subcommand covers it — that's what keeps the backend-agnostic forge seam
  ([`FORGE_MIGRATION.md`](FORGE_MIGRATION.md)) actually exercised instead of quietly
  bypassed by the one class of session most likely to reach for `gh` out of habit. Reads are
  covered too — `coord issue list` and `coord issue view` shipped in #2484, so a plain issue
  listing/search or an issue-plus-comments lookup has no excuse to reach for `gh`. Where no
  `coord` subcommand covers what's needed — the seam is wide but not total, so check
  `coord <group> --help` before concluding either way — falling back to `gh` for that one call is
  fine, but say so and treat it as a gap to flag or file, not a silent workaround.

## Where the money actually goes

Measuring fleet spend is its own trap — bare `coord usage` undercounts, and estimated columns
are not authoritative. See [`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) for the measurement
rules and #17 for the #2132 review-verdict cost analysis.

**The dominant line item is cache reads, not output** (measured 2026-08-25, 7-day window). Across
`type='work'` legs: 3,483.6M cache-read tokens ($1,045) against 15.7M output ($236) and 46.1M
cache-creation ($173) — a 75:1 read-to-create ratio, so prompt caching is working nearly
perfectly and cache *misses* are not the problem. Cache-read cost is `context size × turns per
leg`: every turn re-reads the whole context. That is why `CLAUDE.md`'s size is a fleet-wide cost
lever and why this file exists — see #2787. `num_turns` is not yet persisted (#2786), so
"long context" and "many turns" cannot yet be told apart from the ledger.
