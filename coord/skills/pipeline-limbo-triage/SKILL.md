---
name: pipeline-limbo-triage
description: "Use when an issue sits in the Pipeline with no dispatch or visible activity — explains that Board vs Pipeline membership is label-driven, not assignment-driven, and gives the one-command fix."
trigger: An issue shows up in the Pipeline but nobody dispatched it, or a Pipeline card looks stuck.
---

# pipeline-limbo-triage skill

**Trigger:** An issue shows up in the Pipeline but nobody dispatched it, a
Pipeline card looks "stuck" with no visible activity, or an operator asks "why
is this issue in the Pipeline — I never assigned it?"

**Purpose:** Explain the one non-obvious cause — Board vs Pipeline membership
is **label-driven, not assignment-driven** — and give the one-command fix.
Sourced from
`docs/ARCHITECTURE.md#when-an-issue-is-sitting-in-the-pipeline-you-never-dispatched`.

---

## The state table

| State | Signal | Where it shows |
|---|---|---|
| Backlog | `coord` label, no `status:*` label, no assignments | Board sidebar |
| Refining | `status:refining` | Board sidebar |
| Refined / Ready | `status:ready`, no assignments | **Pipeline** (a "pending / ready-to-dispatch" card) |
| In-progress | has a `type="work"` assignment | Pipeline |
| Done | merged | Pipeline (Done group) |

## Steps

1. Confirm the issue has **zero assignments** (`coord status` or the board).
   If it already has one, this isn't the limbo case — look elsewhere (try
   `merge-stuck-triage` instead if the symptom is "isn't merging").
2. Check its labels. If it carries `status:ready` and nothing else, that's the
   answer: it's an open issue that finished refinement and got marked ready,
   but nobody has actually dispatched work against it. It *looks* dispatched
   (it's in the Pipeline) but isn't.
3. This usually happens silently: the refinement chat and new-issue chat flows
   finalize by flipping `status:refining → status:ready` (via `coord ready`)
   — the same action that makes an issue dispatchable — and nothing
   distinguishes "ready and waiting for a human to dispatch" from "ready and
   about to be forgotten."
4. **To drop it back to the Board** (if it wasn't meant to sit in the Pipeline
   yet): `coord backlog <repo> <issue>` strips `status:refining`/`status:ready`
   (and `status:queued`, if present), returning it to unscoped Backlog.
   Symmetric with `coord refine`/`coord ready`. The TUI's right-click "Drop to
   Backlog" fires the same command.
5. If it genuinely is ready to work, dispatch it normally — `coord plan` /
   `coord approve`, or `coord drive-queue add` (see the `coord-dispatch-verbs`
   skill if you're unsure which dispatch command is the right one).

## Known gap (#359)

Refinement/plan-only issues get stranded in the Pipeline this exact way; the
intended fix is for the chat dialogs to route straight to Plan or Work instead
of leaving an issue in the `status:ready` limbo stage. Until that lands, this
is expected behavior, not a bug to chase.
