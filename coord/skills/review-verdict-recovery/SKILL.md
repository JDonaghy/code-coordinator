---
name: review-verdict-recovery
description: "Use when `coord gates` shows review : ERROR (not BLOCKED), or a headless review completed with no parseable verdict — recovers the verdict already sitting in the transcript instead of burning a full re-review cycle."
trigger: "`coord gates <repo> <issue>` shows review : ERROR, or a review reached END_REVIEW with review_verdict=None on the board."
---

# review-verdict-recovery skill

**Trigger:** `coord gates <repo> <issue>` shows `review : ERROR` (not
`BLOCKED`), or `coord notify`'s log / a GitHub completion comment flags a
review that reached `END_REVIEW` with no parseable verdict — or a headless
review completed with a thorough body but `review_verdict=None` on the board.

**Purpose:** Recover the verdict that's already sitting in the transcript
instead of burning a full review cycle re-deriving a conclusion the reviewer
already reached. Sourced from `docs/OPERATING_GOTCHAS.md` #16 (#1956) — read
it in full for the incident history and the `verdict_source` semantics.

---

## How to tell this apart from a review that hasn't run yet

`coord gates` renders this case as `review : ERROR`, not the generic
`BLOCKED` — `coord notify`'s log also carries a `log.warning` naming the
assignment and quoting the excerpt right before `END_REVIEW`, and the GitHub
completion comment for the review assignment uses distinct wording from the
generic "findings could not be extracted" message.

- Plain `BLOCKED` → the review genuinely hasn't produced anything usable yet.
  Normal case; nothing to recover.
- `ERROR` (or the loud log line / GitHub comment) → the verdict is very
  likely sitting in the transcript already. This skill applies.

A `REVIEW_VERDICT:` marker that IS present but malformed (bolded, mismatched
terminator, …) is the sibling #1348 case — same recovery below, different
detection path.

## Steps

1. **Do NOT re-dispatch the review.** Re-running costs a full cycle to
   re-derive a conclusion already in the log, and there is a documented ~14%
   rate of dropping the header again on the very next attempt.
2. Read the review assignment's transcript. Confirm the verdict the reviewer
   actually reached — look for the reasoning and the `## Blocking findings`
   section (or its absence) to determine `approve` vs `request-changes`.
3. If the verdict is `request-changes`, extract the review body to a file —
   it's required for the relay command below.
4. Relay the verdict through the same seam the reviewer's own
   `REVIEW_VERDICT:` line would have written to:

   ```bash
   coord report-result --assignment <review_assignment_id> \
     --verdict <approve|request-changes> \
     --verdict-source recovered \
     --verdict-reason "REVIEW_VERDICT header missing, recovered from transcript (#1956)" \
     --body-file <extracted-review.md>       # required with --verdict request-changes
   ```

## Rules

- **`--verdict-source recovered` (with a `--verdict-reason`) is not optional
  decoration.** A relayed verdict with no stated provenance is
  indistinguishable from one the reviewer produced itself, everywhere this
  surfaces (`coord gates`, the board, the audit trail).
- Use `--verdict-source overridden` instead of `recovered` **only** when
  recording a *different* verdict than the reviewer actually concluded — a
  deliberate human override. Never use `overridden` for a straightforward
  transcript rescue; the two read differently downstream on purpose
  (`recovered` asserts "the reviewer decided this, we merely restored it";
  `overridden` asserts "the reviewer decided otherwise and a human
  disagreed").
- Don't hand-edit historical rows unless doing the documented one-time
  backfill for rows that predate the `verdict_source` column
  (`docs/OPERATING_GOTCHAS.md` #16 names the two known 2026-08-07 rows) —
  that's an explicit, named operator action, not something to do
  speculatively on an unrelated row.
