---
name: update-issue
description: "Synthesizes what was agreed in a 'Chat about issue' conversation and writes it back to the GitHub issue body, so the next worker or human picks up a refined, accurate scope instead of the original text."
trigger: /update-issue
---

# /update-issue skill

**Trigger:** Operator types `/update-issue` during a "Chat about issue" session.

**Purpose:** Synthesize what was agreed in this conversation and write it back to
the GitHub issue body — so the next worker or human picks up a refined, accurate
scope rather than the original unrefined text.

---

## Steps

### 1 — Locate repo + issue number

Extract `repo` and `issue` from the session briefing/system prompt context.
Look for the pattern `Chat about <repo> #<issue>` or
`[Coordinator chat assignment …] … chat about this issue`.

If neither is visible, ask the operator: `Which repo and issue number? (e.g. claude-coordinator 319)`

### 2 — Read the current issue body

The briefing always includes the issue body — read it from there.  If the body
is genuinely absent (e.g. the skill is used outside a normal briefing context),
ask the operator to paste the current issue body directly into the chat rather
than running any external command.

### 3 — Synthesize what was agreed

Read the **full conversation** above and extract:

- **Scope decisions** — what is explicitly IN and what is explicitly OUT.
- **Acceptance criteria** confirmed by the operator.
- **File / module boundaries** or approach notes that surfaced.
- **Open questions** left for the implementing worker.
- **Anything explicitly ruled out** (so the worker doesn't re-litigate it).

Preserve the structure of the original body (## What, ## Why, ## Acceptance,
## Out of scope, ## Notes, etc.) — tighten the content, don't replace the
headings.  Do not invent scope that was not discussed.

### 4 — Draft the new body

Write the proposed body as a markdown block inside the conversation so the
operator can read it before anything is written.  Open with a brief summary of
what changed, e.g.:

> **Proposed update** — tightened scope based on our chat:
> - Added X to Acceptance (we agreed …)
> - Moved Y to Out of scope (operator said not needed)
> - Noted constraint: Z

Then show the full proposed body.

### 5 — Operator review

Ask: `Does this look right? (yes to write / no to discard / edit to adjust)`

Accept free-form edits: if the operator says "change X to Y", apply the change
and show the updated draft.  Repeat until confirmed.

### 6 — Write the body

On confirmation, write the body to a temp file and call:

```bash
coord issue edit <repo> <issue> --body-file /tmp/coord-issue-<issue>-body.md
```

Report the result.  If the command fails, show the error and leave the temp file
in place so the operator can retry manually.

### 7 — Offer to mark ready

After a successful write, ask:

> The issue body is updated.  Mark it ready for dispatch? (`coord ready <repo> <issue>`)
> This moves it to Pending so the coordinator can pick it up.  (yes / no)

If yes: run `coord ready <repo> <issue>` and confirm.

---

## Rules

- **Never call `gh` directly** — use `coord issue edit` so the write routes
  through the tracker seam.
- **Always confirm before writing** — show the full proposed body in-chat first.
- **Preserve structure** — keep original section headings; only change content.
- **Repo-agnostic** — this skill works for any repo (quadraui, vimcode,
  claude-coordinator, etc.); do not hard-code repo names.
- **Do not edit files in the live checkout** — this is a read/write-issue-only
  skill; no git commits.
