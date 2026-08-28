---
name: drive-queue-preflight
description: "Use before queuing more than ~2 issues on one repo with `coord drive-queue add`, setting up an overnight/unattended run, or seeing a QUEUE: STALLED/BLOCKED status-bar alert — gives the real cost model and the read-the-alert table."
trigger: About to `coord drive-queue add` deep on one repo, or looking at a QUEUE: STALLED/BLOCKED alert.
---

# drive-queue-preflight skill

**Trigger:** About to `coord drive-queue add` more than ~2 issues on one repo,
setting up an overnight/unattended drive-queue run, or looking at a `QUEUE:
STALLED` / `QUEUE: BLOCKED` status-bar alert.

**Purpose:** Give the real cost model before queuing deep (a queue longer than
~2 issues per repo is *not* an unattended feature), and the read-the-alert
table for when a queue stalls. Sourced from `docs/DRIVE_QUEUE.md`'s "Read this
before queuing more than ~2 issues" (#1715) and its §4 alert table — read that
doc in full for the mechanics; this is the preflight checklist and the alert
decoder.

---

## Before queuing — the cascade to know about

Every merge on a repo invalidates every *other* queued branch's Test verdict
on that same repo (the base moved). Queue N independent issues against one
repo and merging the first stales the other N−1. This is **intra-repo only**
— cross-repo entries in the same queue never stale each other and run in
parallel (the per-repo concurrency ceiling defaults to 1 for exactly this
reason).

**What has NOT changed:** none of the self-repair below is automatic. The
unattended auto-drain always passes `revalidate=False` (deliberately, since
the 2026-06-07 token-burn incident) — an overnight timer with no operator
still parks stale entries.

**What has changed:** clearing a stale queue the next morning is now one
command per shared base, not N−1 by hand:

```bash
coord merge --revalidate --dry-run   # names each batch and its members
coord merge --revalidate             # one composed suite run per base, then merge
```

A composed run validates the batch **together**, not each branch alone — a
re-confirmation of verdicts each member already earned individually, not a
first proof. If the composite fails, nothing merges; each branch is then
re-tested alone so the culprit is named and the innocent branches still land.

**Practical guidance:**
- Genuinely unattended overnight run, nobody coming back to it: keep to 1–2
  entries per repo (or spread across different repos — they don't stale each
  other).
- A run you WILL come back to and drain: queue as deep as you like — the
  morning drain is one command.

## Reading a `QUEUE:` alert

| Segment | Meaning | What to do |
|---|---|---|
| `QUEUE: empty` | nothing queued | nothing |
| `QUEUE: N running · M waiting` | normal operation | nothing |
| `QUEUE: STALLED — N waiting, none eligible` (warn) | capacity is free, but every waiting entry is deferred — usually an unresolved `--after` pre-req | usually self-resolves once the pre-req lands; `coord drive-queue status` names exactly which entries and why |
| `QUEUE: BLOCKED N · M waiting` (warn/crit, outranks a simultaneous stall) | one or more entries are `blocked`: a dependency cycle, a broken `--after`, or a drive that died `attempts` times in a row | usually needs an operator action (below) — but since #2230 some blocked entries self-heal, see next |

**Since #2230, not every `blocked` entry needs a human.** Every tick re-checks
a blocked entry's own merge gate (unless it's one of two permanent causes — a
pre-dispatch refusal, or a genuine dead end) and moves it back to `waiting`
the moment the gate reads clear. If a row is *still* `blocked`, either it
never had a re-evaluable cause, the gate is genuinely still shut, or it
already oscillated past the resume ceiling (3 — `resumes=N/3` in `coord
drive-queue list`).

**Fix for a row that needs a human** — there's deliberately no `reset`;
remove-and-re-add gives a clean `waiting`/`attempts=0` row:

```bash
coord drive-queue remove REPO ISSUE && coord drive-queue add REPO ISSUE
# re-add WITHOUT the bad --after if that was the cause
```

or, from the TUI overlay, select the blocked entry and press `u`.

Treat `last_reason` as history, not a live diagnosis — it's a snapshot taken
the instant it was written and never re-validated; `coord drive-queue list`
shows its age for exactly this reason. Once it's more than a few minutes old,
go check the board/CI/review state directly instead of trusting the text.

## Rules

- Never assume a deep queue is "fine because auto-drain will handle it" —
  auto-drain never revalidates, by design.
- Don't `coord drive-queue add` more than ~2 issues on one repo and walk away
  unless you're planning to run `coord merge --revalidate` yourself later.
- Confirm an issue actually landed in the queue with `coord drive-queue list`
  — see the `coord-dispatch-verbs` skill for why the add command's own exit
  code isn't sufficient proof.
