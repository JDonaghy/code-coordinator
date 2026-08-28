---
name: coord-dispatch-verbs
description: "Use before running a `coord` CLI command to enqueue, dispatch, stage, or otherwise move a GitHub issue through the coordinator — disambiguates ~70 top-level commands with similarly-named pairs that silently do the wrong thing instead of erroring."
trigger: About to run a `coord` command to move an issue forward, especially when unsure of the exact subcommand, or a command exited 0 but the expected effect didn't happen.
---

# coord-dispatch-verbs skill

**Trigger:** About to run a `coord` command to enqueue, dispatch, stage, or
otherwise move a GitHub issue forward through the coordinator — from any repo,
including agents working *inside* a coordinator-managed repo, not just the
operator's own coordinator session — especially when unsure of the exact
subcommand name, or when a command exited 0 / printed a success message but
the expected effect (an entry in the queue, an assignment, a dispatch) didn't
show up.

**Purpose:** `coord`'s CLI has roughly 70 top-level commands, and several have
names that sound interchangeable but do very different things — picking the
wrong one usually does **not** error, it silently does something else. This is
the disambiguation table for the pairs that have actually cost real time,
plus the one rule that would catch a wrong guess before it's mistaken for
success.

---

## The incident this skill exists for

An agent working in coord-portal wanted to queue issue #119 for the
coordinator to pick up. It ran:

```
coord queue coord-portal 119
```

This command **exists**, exited 0, and printed a success-looking message — so
nothing signaled a mistake. But `coord queue` is **not** the driver queue. It
only tags the issue with a `status:queued` **label** — "next up" staging,
display + intent only, no dispatch (#1500; it's what the TUI's right-click
"Mark ready" fires). The issue never entered `coord drive-queue`'s actual
`waiting` list, so nothing ever picked it up.

**The command that was needed:** `coord drive-queue add coord-portal 119` —
the durable, board-backed driver queue (#1750) that a periodic tick actually
walks and dispatches from. See `docs/DRIVE_QUEUE.md` and the
`drive-queue-preflight` skill.

## Disambiguation table — commands whose names collide but whose effects don't

| If you want to... | Use | NOT | Why the wrong one is dangerous |
|---|---|---|---|
| Actually get an issue worked, end-to-end, unattended | `coord drive-queue add <repo> <issue>` | `coord queue <repo> <issue>` | `queue` only applies a label — no dispatch, ever. A silent no-op relative to what you wanted. |
| Dispatch one issue's Work stage right now, interactively | `coord assign <machine> <repo> <issue>` | `coord drive <repo> <issue> --tmux` outside the queue, while the queue timer is active | `--tmux` drives correctly but is invisible to the queue panel and can race a live `coord-drive-queue.timer` tick — see `docs/OPERATING_GOTCHAS.md` #9. |
| Un-stage an issue you `coord queue`'d by mistake | `coord unqueue <repo> <issue>` | `coord drive-queue remove <repo> <issue>` | These touch two different systems (a label vs. a queue row) — running the wrong one leaves the other system's state untouched and the symptom persists. |
| Retry a failed **queue entry** | remove + re-add (`coord drive-queue remove` then `add`) | `coord retry <assignment_id>` | `retry` operates on a board *assignment*, not a drive-queue row — there is deliberately no `coord drive-queue reset` (see `drive-queue-preflight` / `docs/DRIVE_QUEUE.md` §4). |
| Check whether an issue is actually queued | `coord drive-queue list` / `coord drive-queue status` | trusting the exit code of whatever command you just ran | `coord queue`, `coord assign`, and `coord drive-queue add` all print a success line on their own happy path — only `coord drive-queue list` proves the entry exists as `waiting`. |

## The one rule

**A `coord` command exiting 0 is proof it did what *that command* does — not
proof it did what you intended.** Before reporting a dispatch/queue/staging
action as done, confirm the effect directly:

```bash
coord drive-queue list          # is my issue actually a waiting/running row?
coord status                    # does an assignment exist for it?
coord gates <repo> <issue>      # what stage does the board think it's in?
```

If unsure which of two similarly-named commands does what you want, run
`coord <command> --help` first — every command's help text states its exact
effect (`coord queue --help` says so explicitly: "no dispatch, display +
intent only"). Guessing from the name alone is how this incident happened.

## Rules

- Never assume `coord queue` enqueues work for dispatch — it only labels. The
  only real dispatch queue is `coord drive-queue`.
- After any dispatch/queue command, verify with `coord drive-queue list` or
  `coord status` before reporting the action as done — don't trust a clean
  exit code alone.
- When unsure between two `coord` subcommands with similar names, run
  `--help` on the one you're about to use before running it.
