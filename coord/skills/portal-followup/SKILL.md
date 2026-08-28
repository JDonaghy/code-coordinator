---
name: portal-followup
description: "Use when investigating a customer-portal signoff/event that hasn't shown up, or troubleshooting `coord portal` state — gets a straight answer out of outbox/events/sync instead of confusing local-machine state with the bridge's own."
trigger: Investigating a stuck customer-portal signoff/event, or any `coord portal` state troubleshooting.
---

# portal-followup skill

**Trigger:** Investigating a customer-portal signoff/event that "hasn't shown up"
— a submission stuck on an old status, a customer event (e.g.
`signoff.approved`) that doesn't seem to have registered, or any troubleshooting
of `coord portal` state.

**Purpose:** Get a straight answer out of `coord portal outbox`/`events`/`sync`
without re-discovering #2336 the hard way — a normal-looking "nothing pending"
from these commands used to be indistinguishable from "the bridge genuinely has
nothing pending," because they read/write **the local machine's**
`~/.coord/coord.db` directly, and only the daemon host's copy is real.

---

## The one thing to know

`coord portal status` / `heartbeat` / `push` touch only the config + the portal
API — safe to run from anywhere.

`coord portal sync` / `outbox` / `events` / `enqueue-*` / `requeue` touch the
daemon's own `~/.coord/coord.db`. **Run them on the daemon host directly**
(`ssh <daemon-host>` first — check `~/.coord/client.toml`'s `board_service` for
which host that is, or ask `coord status` on this machine, which already routes
through it).

Since #2336, running one of these from a thin client (any machine with
`board_service` configured) no longer silently reads that machine's empty local
DB — it refuses outright:

```
Error: coord portal outbox must run on the daemon host (dellserver) — this
machine's ~/.coord/coord.db is not where the bridge lives (board_service is
configured in ~/.coord/client.toml, making this a thin client). Run it over
`ssh` on the daemon host instead. See coord/skills/portal-followup/SKILL.md.
```

If you see that error, you have your answer: SSH to the named host and re-run
the same command there.

## Steps

1. **Identify the daemon host.** `cat ~/.coord/client.toml` (if present) shows
   `board_service = "http://<host>:<port>"` — that host is where the real
   `coord.db` lives. If this machine has no `client.toml` at all, it may *be*
   the daemon host already; try the command locally first.

2. **Run the portal command on that host** (over `ssh`, or directly if you're
   already on it):

   ```
   ssh <daemon-host> 'coord portal outbox --all'
   ssh <daemon-host> 'coord portal events'
   ssh <daemon-host> 'coord portal sync --json'
   ```

3. **Cross-check credentials separately if a push/heartbeat looks like a 401.**
   `coord portal status --json` reports `credentials_set: false` whenever
   `BRIDGE_CLIENT_ID`/`BRIDGE_CLIENT_SECRET` are unset in *this shell's*
   environment, even if `coordinator.yml` has non-empty `${VAR}` strings for
   them (#2336) — a bare interactive `ssh <host> '...'` does not source
   `~/.coord/coord-serve.env`, but the actual `coord-serve` systemd unit does
   via `EnvironmentFile=`. A 401 from a manual `ssh` + `coord portal heartbeat`
   check does not necessarily mean the credential itself is bad — check
   `credentials_set` first before assuming the secret rotated.

4. **If a submission's outbox row is `HELD`,** that's the #835 ordering guard
   working as intended (announcing a status like `awaiting-signoff` before its
   design round applied would email the customer toward an empty screen), not
   a bug — resolve the blocking condition rather than trying to force the push.

5. **If a row is terminal (retired) after burning its retry budget,** fix the
   underlying cause first, then `coord portal requeue <submission_id> <seq>`
   (find the seq with `coord portal outbox --all`) — also a daemon-host command.

## Step 2 — per event kind

`coord portal events` shows whatever `event_type` the portal reported,
verbatim — there is no closed enum on this side (`coord/portal_bridge.py`'s
`pull()` returns the JSON as-is), so a new event kind the portal starts
sending shows up here for free, with no coord-side code change. What each
kind means for what you do next:

- **`signoff.approved`** — the customer accepted the design round. Move the
  submission toward `planned`/`in-progress` as normal.
- **`signoff.changes_requested`** — read the customer's comment, decide
  whether it's a quick fix to the current round or needs a fresh design
  round, then act (and `coord portal enqueue-design-round` again if it's a
  new round).
- **`preview.approved`** (#2359, coord-portal#107) — the customer approved
  the real preview build. Read the comment/confirm it's really a go, then
  `coord portal enqueue-status <id> shipped` **when actually ready** — same
  manual discipline already used for `shipped` today. Nothing auto-fires off
  this event; it is not wired into `coord merge` or the merge queue.
- **`preview.changes_requested`** (#2359, coord-portal#107) — read the
  customer's comment and decide whether it's a quick fix or a new round,
  exactly like `signoff.changes_requested` above — no new logic, same manual
  judgment call.

## Pre-merge: queue the preview build (#2359)

Before `shipped` meant "my wife looked at it and said it's fine." This gate
replaces that with a real, tracked customer approval of an actual preview
build. For any submission with a Cloudflare Pages preview deployment (most —
skip this entirely for issues with no preview build; the gate is opt-in by
construction, since a submission only enters `quality-check` if you queue a
preview for it):

1. **After Test and Review both pass, before running `coord merge` for the
   issue**, fetch the PR's own **Preview** (never Production) deployment URL.
   `cloudflare/pages-action` already creates a real GitHub Deployment per PR
   (natal-chart's `deploy-cloudflare.yml` needs no changes for this):

   ```
   gh api repos/OWNER/REPO/deployments?ref=<branch> --jq '.[0].id'
   gh api repos/OWNER/REPO/deployments/<id>/statuses --jq '.[0].environment_url'
   ```

   Confirm the deployment's `environment` reads `"<project> (Preview)"`, not
   `"<project> (Production)"` — a `main`-branch deployment is Production and
   must never be what the customer is shown for sign-off.

2. **Queue it and announce it, in that order:**

   ```
   coord portal enqueue-preview <submission_id> <preview_url>
   coord portal enqueue-status <submission_id> quality-check
   ```

   `quality-check` is now an announcing status (`ANNOUNCING_STATUSES` maps it
   to `preview`), so the same #835 ordering guard that protects
   `awaiting-signoff`/`design_round` applies here for free: the status push
   HELDs until the preview row is confirmed applied, so the customer can never
   land on a sign-off screen with nothing to look at.

3. **Wait for `preview.approved`** (via `coord portal events`) before running
   `coord merge` for the issue. This is operator discipline, not a mechanical
   gate — `coord merge`/`coord.merge_queue` do not check for it and will
   happily merge without it if you run them anyway. That's deliberate for this
   pass (see the issue for why); treat skipping this wait the same as shipping
   without your wife's verbal today.

## Rules

- Never try to work around the "must run on the daemon host" error by editing
  `~/.coord/client.toml` to unset `board_service` on a thin client — that
  changes what *every other* `coord` command on that machine reads too, not
  just `portal`.
- Prefer `coord portal enqueue-status`/`enqueue-design-round`/`enqueue-question`/`enqueue-preview`
  over `coord portal push` for anything the customer will see — `push` bypasses
  the #835 ordering guard entirely.
