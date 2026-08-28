---
name: merge-stuck-triage
description: "Use when a story won't merge — the TUI Go does nothing, `coord merge` skips it, or the queue box stays grey/pending — walks the merge gates in the order they actually block instead of guessing."
trigger: A story won't merge, or an operator asks why something isn't merging.
---

# merge-stuck-triage skill

**Trigger:** A story won't merge — the TUI "Go" does nothing, `coord merge` skips
it, the queue box stays grey/pending — or an operator asks "why isn't this
merging?"

**Purpose:** Walk the merge gates in the order they actually block, instead of
guessing. Sourced from `docs/ARCHITECTURE.md#when-a-merge-isnt-happening` —
that section is the reference to re-read for the full "why"; this is the
checklist.

---

## Steps

Check in this order — most-likely cause first:

1. **Test gate (the #1 cause).** No review is dispatched until the work's Test
   stage has a verdict. **Symptom:** work is `done`, but no `type="review"`
   assignment exists and `review_state` is null. **Fix:** `coord test
   <work_assignment_id> --passed` (`--skipped` for trivial changes, `--fail
   --reason "…"` for a real failure), then `coord pr <id>` opens/reuses the PR
   and dispatches the review. In the TUI: **P / S / F** on the Test stage.

2. **Review not approved.** The merge gate is `has_approved_review` — needs a
   `type="review"` assignment with `review_verdict="approve"` for the work
   behind this queue entry. No review, or a `request-changes`, and merge
   refuses with *"review required but not approved"*. If a review reached
   `END_REVIEW` with no verdict recorded, don't re-dispatch — use the
   `review-verdict-recovery` skill instead.

3. **CI red.** `coord merge` is gated on `gh pr checks` (#240). A failing or
   still-pending check blocks it — check the queue entry's `error` field.
   `coord merge --force-merge` overrides, deliberately.

4. **PR conflicts.** `mergeable=CONFLICTING` auto-dispatches a
   `type="conflict-fix"` worker to rebase (#241) — this runs invisibly, so
   check for a running `conflict-fix` assignment before assuming nothing
   happened. On success it re-enqueues and merges; on a semantic conflict it
   marks the entry `HUMAN_REQUIRED`.

5. **Queue clog / group halt.** `coord merge` processes each `(repo,
   target_branch)` group together — a queue full of stale entries for
   already-closed issues can stall everything behind them (the closed-issue
   filter only blocks *new* enqueues, it does not prune existing rows).
   - To jump one issue past a clog: `coord merge --repo <r> --order
     <assignment_id>`.
   - To declog for real: delete `merge_queue` rows whose GitHub issue is
     already closed.

6. **Post-bounce keying (#292).** After a review bounce (request-changes → fix
   → approve), the queue entry can still be keyed to the *original*
   request-changes work while the approval sits on the *fix* assignment —
   `has_approved_review` then fails even though a fix was approved. If this is
   the shape you're seeing, re-key `merge_queue.assignment_id` to the approved
   fix assignment.

7. If none of the above explains it, `git pull` the coordinator clone first —
   the merge/review/auto-loop logic (`merge_queue.py`, `auto_loop.py`,
   `reconcile.py`, `cli.py`) runs in fresh CLI invocations, so a fix already
   merged into code-coordinator is live immediately, no agent restart needed.
   (Agent-side code — `agent.py`/`agent_app.py` — does still need a release +
   `coord agent update`; that's the one case a `git pull` alone won't fix.)

## Rules

- Check the gates **in order** — a later cause (queue clog, post-bounce
  keying) can look identical to an earlier one (no review dispatched) from
  the TUI alone. Don't skip ahead on a guess.
- `coord merge --force-merge` bypasses CI — use it deliberately, not as a
  first resort.
- Don't re-dispatch a review that already produced a verdict-bearing
  transcript — see `review-verdict-recovery`.
