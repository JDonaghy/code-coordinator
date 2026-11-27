/**
 * Tranche workflow template (#4128).
 *
 * The canned implement -> integrate -> review -> fix recipe for a pilot
 * "tranche": a batch of sibling widget-contract children that share one
 * base SHA, run through coord's own Test -> Review -> Merge as ONE pull
 * request instead of N serial Work/Review/Merge cycles.
 *
 * This file is embedded VERBATIM into a tranche leg's system prompt
 * (`coord.agent._tranche_leg_system_prompt_suffix`) — the same "single
 * source of truth embedded as literal text, not re-described from memory"
 * pattern that function already uses for `REVIEWER_SYSTEM_PROMPT`. It is
 * never imported or executed by this (Python) repo; it is read as a
 * template to hand to the Claude Code `Workflow` tool, with every
 * `<<PLACEHOLDER>>` filled in from the specific issue being worked.
 *
 * Fill in before use:
 *   - BASE_SHA        — one pinned commit every implementer worktree starts
 *                        from. Never "whatever HEAD is when each agent
 *                        happens to start" — that is how four implementers
 *                        each re-diagnose the same base clippy debt.
 *   - CHILDREN         — the list of child issues/widgets in this tranche,
 *                        exactly as enumerated in the epic.
 *   - PREP_PACK        — computed ONCE, before any implementer starts: the
 *                        recipe to follow, precedent commits to mirror, and
 *                        failures the base already has (so no implementer
 *                        spends its first 16-24% of cost rediscovering
 *                        them, the #2827 lesson this template exists to
 *                        encode).
 *   - COST_CEILING_USD — the per-tranche spend ceiling (operator-configured
 *                        via `budget.ceiling_for("work")` in
 *                        coordinator.yml — this template does not invent a
 *                        second ceiling mechanism).
 */

// ---------------------------------------------------------------------------
// Phase 1 — implement (parallel, isolated)
// ---------------------------------------------------------------------------
//
// One agent per child, each in its OWN worktree, all off the identical
// <<BASE_SHA>>. No implementer edits another implementer's files or
// branch. No implementer may run `git push` to a shared branch — that is
// the integrator's job alone, in phase 2.
//
// Each implementer MUST end with exactly one machine-parseable JSON result
// (not prose) on its own line, matching this contract:
//
//   {
//     "child": "<issue-or-widget-id>",
//     "status": "done" | "broken" | "deviated",
//     "deviations": ["<what differs from the recipe/prep pack, and why>"],
//     "broken": ["<what could not be made to work, and why>"],
//     "sealed_amendment_needed": ["<a sealed/driver surface this child
//                                   needs changed, which the implementer
//                                   itself must NOT edit>"],
//     "diff_ref": "<branch or patch the integrator applies in phase 2>"
//   }
//
// FAIL CLOSED: a result line that does not parse as this JSON shape, or is
// missing, is treated as "broken" for that child — never silently skipped,
// never treated as "done". The integrator (phase 2) must not fold an
// unparseable child's diff into the tranche.
const implementPhase = {
  name: "implement",
  isolation: "none", // #3730: never "worktree"/"remote" — see the three
                     // guardrails in _workflow_leg_system_prompt_suffix.
  baseSha: "<<BASE_SHA>>",
  prepPack: "<<PREP_PACK>>",
  // <<CHILDREN>> is filled in with a real JSON array literal (e.g.
  // ["widget-a", "widget-b"]) before this file is handed to the Workflow
  // tool, so it is written unquoted here — a quoted "<<CHILDREN>>" would
  // substitute to a *string*, which has no .map() method.
  agents: <<CHILDREN>>.map((child) => ({
    role: `implementer:${child}`,
    instructions:
      "Implement exactly this child against the shared prep pack and " +
      "base SHA above. Do not touch another child's files. End with the " +
      "structured result JSON described above, on its own line.",
  })),
};

// ---------------------------------------------------------------------------
// Phase 2 — integrate (single agent, runs once)
// ---------------------------------------------------------------------------
//
// One agent merges every "done" or "deviated" implementer's diff onto one
// integration branch, re-derives any generated/integration surface ONCE
// (not once per child), and resolves cross-child conflicts. "deviated" is
// information (see `deviations`), not a failure, so it still merges — only
// a child reported "broken" or whose result failed to parse is excluded
// from the merge, not force-fit.
const integratePhase = {
  name: "integrate",
  isolation: "none",
  agents: [
    {
      role: "integrator",
      instructions:
        "Merge every implementer result whose status is \"done\" or " +
        "\"deviated\" onto one branch off the same base SHA, surfacing " +
        "each \"deviated\" child's reported deviations in the merge " +
        "notes. Exclude \"broken\" and unparseable children and say " +
        "which were excluded and why. Re-derive generated/integration " +
        "surfaces exactly once for the whole tranche.",
    },
  ],
};

// ---------------------------------------------------------------------------
// Phase 3 — review (three independent focus areas, not one generic pass)
// ---------------------------------------------------------------------------
//
// Each lens grades the SAME integrated diff against coord's real reviewer
// rules (the review step's system prompt/lens is the identical
// REVIEWER_SYSTEM_PROMPT text coord's own dispatched Review leg uses — see
// _workflow_leg_system_prompt_suffix, embedded separately from this file),
// narrowed to one focus per agent:
const reviewPhase = {
  name: "review",
  isolation: "none",
  agents: [
    {
      role: "review:recipe-completeness",
      focus:
        "Does every child in CHILDREN actually land in the integrated " +
        "diff, matching the prep pack's recipe? Anything dropped, " +
        "stubbed, or silently excluded during integration?",
    },
    {
      role: "review:behaviour-parity",
      focus:
        "Does each child behave the same as its pre-tranche equivalent " +
        "(or the recipe's documented intent) for callers outside this " +
        "tranche? Any behavior change that isn't an explicitly declared " +
        "deviation?",
    },
    {
      role: "review:ci-gate-risk",
      focus:
        "Will this diff pass Test/Review/Merge as filed? Sealed-path " +
        "touches, missing black-box coverage for user-visible behavior " +
        "change, warnings left unfixed, anything that reads as an " +
        "unconfirmed success per this repo's own CLAUDE.md.",
    },
  ],
};

// ---------------------------------------------------------------------------
// Phase 4 — fix (single agent, addresses all three lenses' findings)
// ---------------------------------------------------------------------------
const fixPhase = {
  name: "fix",
  isolation: "none",
  agents: [
    {
      role: "fixer",
      instructions:
        "Address every finding from all three review lenses above on the " +
        "integrated branch. Do not re-open phase 1/2 — fix forward on the " +
        "integration branch.",
    },
  ],
};

// ---------------------------------------------------------------------------
// Phase 5 — one PR
// ---------------------------------------------------------------------------
//
// The tranche produces exactly ONE pull request for the whole batch,
// carrying every child that made it through phases 1-4 — never one PR per
// child. This is the structural change the pilot is measuring: fewer legs
// and fewer review/rebase cycles per merged child than the serial baseline.
const trancheWorkflow = {
  baseSha: "<<BASE_SHA>>",
  children: <<CHILDREN>>,
  costCeilingUsd: "<<COST_CEILING_USD>>",
  phases: [implementPhase, integratePhase, reviewPhase, fixPhase],
  onComplete: "open exactly one PR for the whole tranche",
};

module.exports = { trancheWorkflow };
