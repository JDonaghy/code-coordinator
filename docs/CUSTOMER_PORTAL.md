# Customer Portal — async intake, design sign-off, and the outbound bridge

> **Status:** design draft / RFC (2026-08-07). Supersedes the *Phase-2 spike* and *"Where the
> customer portal sits"* sections of [`PLATFORM_EVOLUTION.md`](PLATFORM_EVOLUTION.md) — which
> remains the reference for the cloud/multi-engineer platform, but is **no longer the sequencing
> authority for the portal**. Milestone #23, epic #836.
>
> **The one-line change:** the portal was Phase 5, downstream of a Postgres migration, a Cloud API
> extraction, and a standing Azure footprint. Making it **asynchronous** and **outbound-polled**
> removes every one of those dependencies. It is buildable now, in parallel with #1825, for
> roughly the cost of a domain name.
>
> **Not all draft any more (2026-08-23).** The bridge, the design-round push, the verdict consumer
> and the TUI's Approved-work-items panel have shipped. [Running one end to
> end](#running-one-end-to-end--the-operator-runbook) describes **what the code does today**,
> including where it diverges from the customer loop this document originally sketched, and lists
> the known gaps with their issue numbers.

## Intent

A small, authenticated, public web site where a customer describes what they want, and later comes
back to **review a design and sign off on it**. Sign-off produces an **outcome definition** — a
plain-language acceptance contract — plus an epic/milestone decomposition, which is handed to
code-coordinator and driven by the existing `Work → Test → Review → Merge` pipeline. Progress
reports back in git-free language.

The customer never sees git, never sees an issue number, and never talks to a live agent.

## Why this is a different shape from the Phase-2 spike

[`PLATFORM_EVOLUTION.md`](PLATFORM_EVOLUTION.md) designed a customer loop as a **live intake chat**
over a **cloud-hosted coordination API** backed by **Azure Postgres**, reached from the on-prem
daemon through a **standing Tailscale subnet router**. Three decisions change that shape:

| | Phase-2 spike (2026-06-29) | This design |
|---|---|---|
| **Interaction** | live requirements-elicitation chat | **async**: submit a form, get notified, come back to sign off |
| **Unit of intent** | a customer-facing *Feature* abstraction above milestones | **epic/milestone + outcome definition** — the engineer's own units |
| **Topology** | portal is a client of an extracted Cloud API | portal is a **durable inbox**; the daemon polls it **outbound** |

Each earns its place:

**Async deletes the hard infrastructure.** A live chat needs an agent session alive inside the
request path, which is what forced the cloud-hosted API. A form submission is a row in a table. The
LLM work — decomposition, mock generation, acceptance-contract drafting — happens entirely on the
tailnet, on the existing fleet, on the subscription, using the machinery that already exists. The
portal never runs a model.

**The outcome definition is a better artifact than a "Feature."** The spike invented a Project/Feature
layer whose job was to hide git. But the thing worth signing off on is the thing that will later be
*tested* — and the oracle loop ([`ORACLE_LOOP.md`](ORACLE_LOOP.md)) already has a name for that: the
**Gate-A contract**. Making customer sign-off produce the Gate-A input collapses two concepts into
one and means a signed-off design is directly executable, not a document that needs re-derivation.

**Outbound polling deletes the network problem.** `PLATFORM_EVOLUTION.md` costs the Azure step at
~$24–26/mo, most of which is a standing Tailscale subnet router — and flags it as *"a new single
point of failure the entire board depends on."* If the portal is an inbox the daemon reaches out to,
there is no inbound path, no subnet router, no SPOF, and nothing standing to bill.

## Architecture

```
        public internet                    │        tailnet (unchanged)
                                           │
  ┌─────────────────────────────┐          │   ┌────────────────────────────┐
  │  Portal (Cloudflare)        │          │   │  dellserver                │
  │                             │          │   │                            │
  │  Pages ── static site       │          │   │  coord-serve (board daemon)│
  │  Worker ─ JSON API          │◄─────────┼───│    └─ portal sync loop      │
  │  D1 ──── records            │  outbound│   │         (poll + push)      │
  │  R2 ──── design artifacts   │   only   │   │                            │
  │  Access─ auth               │          │   │  coord.db  ·  fleet  ·  git│
  └─────────────────────────────┘          │   └────────────────────────────┘
                                           │
   holds: intake, design rounds,           │   holds: everything that executes
   sign-offs, status mirror                │   git creds, Claude OAuth, dispatch
```

**The portal is a queue with a user interface.** It accepts customer-authored facts, serves
engineer-authored facts back, and stores design artifacts. It holds no credentials that can cause
anything to happen, cannot initiate a connection to the tailnet, and cannot dispatch work.

### The security posture, stated plainly

This is the **first component on the public internet**. Everything today sits behind the tailnet ACL —
[`EPHEMERAL_WORKERS.md`](EPHEMERAL_WORKERS.md) is explicit that `agent_app.py` has no authentication
and *"the tailnet ACL is the security boundary."* The outbound-only design preserves that boundary
exactly: a full compromise of the portal exposes customer intake text, mocks, and status — and grants
**no execution, no repo access, and no path inward**. That property is worth more than any feature in
this document and should not be traded away for convenience later (in particular: do **not** add a
webhook from the portal into the tailnet to reduce latency — poll faster instead).

### The sync bridge (the keystone)

One loop in the daemon, running on the existing `_tick_loop` cadence:

**Pull** — new submissions · sign-off verdicts (approved / changes requested + comments) · answers to
open questions.
**Push** — design rounds (mock bundle → R2, metadata → D1) · up-mapped status per submission ·
open questions · a **heartbeat**.

The heartbeat matters: without it, a dead daemon is indistinguishable from a slow one, and the portal
shows a confidently stale status forever. If the last heartbeat is older than a threshold, the portal
says so.

**Ownership rule — each side is the sole writer of its own fields, and mirrors the other's read-only.**
The portal owns customer-authored facts (intake text, sign-off verdicts, answers). Coord owns
engineer-authored facts (decomposition, status, questions, artifacts). Nothing is co-written, so
there is no merge problem and no split-brain — the failure mode that has bitten this project before
([`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md); the review-rule client/daemon split-brain).

Sync is cursor-based and idempotent. Every record carries a stable id and a monotonic revision; the
daemon replays from its cursor on restart. A submission is never lost because the daemon was down —
it queues, which is the entire point of an inbox.

**Ordering is coord's problem, not the portal's** (#1982, learned from dogfood #835 in production).
Some statuses do not merely display — `awaiting-signoff` *emails the customer* "your design is ready,
go approve it", and `needs-input` announces a question. `status` and `design_round` are separate
fields and **both are coord-owned**, so the portal accepts an announcement with nothing behind it and
mails the customer toward an empty screen; there is nothing it could check. So the push half is a
durable **outbox**, drained one row at a time in per-submission order, and an announcing row is not
sent until the row it announces is *confirmed applied* — not enqueued, not attempted. A crash between
the two retries the announcement; it can never overtake its content.

**Implemented in** `coord/portal_sync.py` (the loop, on `_tick_loop`'s cadence —
`COORD_PORTAL_SYNC_INTERVAL`, default 60 s), `coord/portal_store.py` (the `portal_*` tables), and
`coord/portal_bridge.py` (the HTTP client). `coord portal sync | outbox | events | enqueue-*` is the
operator surface; it is a daemon-host command group because the bridge's cursor lives in the daemon's
`coord.db`.

### Client + project identity — confirmed wire shape (#2586, coord-portal#146)

`coord/approved_work.py`'s `_TEXT_FIELD_ALIASES` table used to guess at coord-portal's real field
spelling (its schema lives in a separate repo). coord-portal#146 pinned it, so this records the
confirmed shape rather than making the next reader re-measure it against a live DB:

- **`client_id`** — an opaque id into coord-portal's `clients` table, `null` until the portal has
  matched this customer to one. **Not** `client` / `client_name` / `clientName` — an earlier round of
  #146 shipped a `client_email` field on `submission.created` and reverted it: ms-2's "coord never sees
  leads" invariant (coord-portal issue #33) forbids a customer's contact address reaching the daemon at
  all, on any field, and the reasoning that a *client account's* address is a different fact from "who
  filed this submission" did not survive that contract. **No email address of any kind crosses this
  bridge**, and no human-readable client name does either — a display label is the portal's to render,
  from its own screens.
- **`project_id`** — an opaque id into coord-portal's `projects` table. This is what
  `portal.project_repos` (`coordinator.yml`) actually keys on, and is the field #2532's original guess
  already had right.
- **No project label/name is sent.** Same posture as the client name above — coord-portal keeps
  human-readable labels off the wire deliberately. `project_label` stays in the alias table as a
  reserved key (renders `""` today) so a future portal addition needs no coord-side code change, not
  because one exists yet.
- `outcome` / `audience` / `done_definition` / `constraints` on `submission.created`'s payload are
  exactly the snake_case guesses `_TEXT_FIELD_ALIASES` already had — also confirmed, not changed.

Both `client_id` and `project_id` are set once, at `submission.created`, and never re-sent by a later
`signoff.*` event — `coord.portal_store.mirror_customer_facts`'s merge-not-replace behavior (the same
mechanism #2585 fixed the envelope-nesting bug for) is what keeps a bare `signoff.approved` payload
(typically just `{"verdict": ..., "round": ..., "comment": ...}`) from wiping them, the same way it
already protected `outcome`/`audience`/`done_definition`/`constraints`.

## The customer loop

```
  Describe  ──▶  In design  ──▶  Awaiting sign-off  ──▶  Signed off  ──▶  [pipeline]  ──▶  Shipped
                     ▲                    │
                     └──── changes ───────┘
                          requested
```

1. **Describe.** An authenticated customer opens a submission and fills a form: what they want, who
   it's for, what "done" looks like, constraints, and (optionally) which project. No chat.
2. **In design.** The daemon pulls it. An agent drafts a **design round**: a plain-language outcome
   definition, an epic/milestone decomposition targeting the right repos (`coordinator.yml` topology
   as context), and **mocks** where the change is visible. Pushed to the portal; the customer is
   notified.
3. **Awaiting sign-off.** The customer reads the outcome definition, clicks through the mocks, and
   either **approves** or **requests changes** with comments. Requesting changes returns to *In
   design* as round N+1 — rounds are versioned and all previous rounds stay readable.
4. **Signed off.** The outcome definition becomes the **Gate-A contract**, the milestone's issues
   are released to run, and **the existing pipeline takes over entirely unchanged.**
5. **Progress** rolls up in git-free language until *Shipped*.

> **That is the customer's view of the order, not the operator's.** As built, the epic + issues are
> created *before* the design round rather than at sign-off: the Gate-A mock bundle is rendered from
> the milestone's own open issues (`coord acceptance mock REPO TRACKING_ISSUE`), so they have to
> exist before there is anything to show. What sign-off gates is the **work**, not the
> **decomposition**. The sequence you actually execute is
> [Running one end to end](#running-one-end-to-end--the-operator-runbook) below.

The engineer gate is preserved: a design round is a **proposal**, and an engineer reviews it before
it reaches the customer. This is the existing `coord plan → coord approve` pattern with the customer
added as a producer at one end and a signer at the other.

### Mocks

The repo already has the pattern — `docs/mocks/web/` holds hand-authored static HTML mocks against a
shared `_tokens.css`. A design round's mock bundle is the same artifact: self-contained static HTML,
stored in R2, served read-only. No build step, no framework, no live data. This is deliberately the
cheapest possible thing that answers *"is this what you meant?"*

### Status vocabulary

Carried forward from [`PLATFORM_EVOLUTION.md`](PLATFORM_EVOLUTION.md) — that analysis holds and is not
re-litigated here — extended with the sign-off states. Only customer-actionable or terminal states
cross the wall; request-changes reviews, merge conflicts, and CI churn never surface.

| Engineer-side reality | Customer sees |
|---|---|
| submitted, not yet designed | **Describing** |
| design round in progress | **In design** |
| design round published, awaiting the customer | **Awaiting your sign-off** ⟵ *demands the customer* |
| signed off, issues ready, not started | **Planned** |
| work in progress (incl. request-changes, rebase, CI churn) | **In progress** (+ % = items shipped / total) |
| testing + review | **Quality check** |
| an open question was raised | **Needs your input** ⟵ *demands the customer* |
| merged / done | **Shipped** |
| no movement past the On-hold threshold | **On hold** |

Precedence for mixed-state submissions, and the business-time On-hold threshold (~1 business day,
clock pauses nights/weekends/holidays), are unchanged from `PLATFORM_EVOLUTION.md`.

### Automatic status push (#2588)

The table above is the design; this section is what actually calls `enqueue_status` today. Until
#2588, nothing did except a human typing `coord portal enqueue-status` — four real submissions shipped
and closed in production while the portal kept showing `describing`/`planned` (measured 2026-08-22).
The gap was a real design problem, not an oversight: coord's pipeline state is tracked **per issue**,
the portal's status is **per submission**, and a submission that decomposed into five issues has no
single stage. This is the fold that answers it.

**The link is the join key.** `coord portal link <repo> <milestone_number> <submission_id>`
(`coord/portal_store.py`'s `link_milestone`, #2507/PDR-1) is the only place coord records which issues
belong to which submission — implicitly, as "every issue under this GitHub milestone in this repo." A
submission with no link recorded is a **no-op with a visible reason**, not a crash and not a silent
skip — and that is the common case today, and will stay common for a while: most milestones predate
`coord portal link`.

**What folds automatically — three statuses, derived with no human judgment call:**

| Fold input (every issue under the linked milestone) | Pushed status |
|---|---|
| every issue closed | **Shipped** |
| ≥1 issue has a `type="work"` assignment ever dispatched, not all closed | **In progress** |
| no issue has started yet | **Planned** |

`coord.portal_sync.fold_submission_status` is the pure fold; `fold_status_for_milestone` wraps it with
the GitHub read (`gh issue list --milestone`, open + closed — deliberately *not* the local `issues`
cache, which only ever holds open issues plus a 7-day grace window on ones that just closed, and would
silently drop long-shipped members from the fold) and the churn guard (below). Two callers, same fold:

* **`coord.merge_queue._maybe_push_status`** — the immediate half. Right after a merged `type="work"`
  PR closes its issue (the same trigger point PDR-3/#2508 already uses for design rounds —
  `coord.merge_queue._maybe_push_design_round`, the pattern #2588 explicitly extends), fold that
  issue's milestone and push if it changed. Fail-open, same posture as every other bridge call in this
  file: no portal config, no milestone on the issue, or no link on file all degrade to silence; only a
  genuine read/enqueue failure surfaces as a `status_push_failed` merge event, and even that never
  undoes the merge.
* **`coord.portal_sync.sync_submission_statuses`** — the self-healing half, run every daemon tick
  (`coord.serve_app._portal_sync_tick`, same `COORD_PORTAL_SYNC_INTERVAL` cadence as the rest of the
  bridge) across **every** linked milestone. This is what catches "work started" — there is no merge to
  hook that transition off of — and anything the merge-time push missed (daemon was down, portal was
  unreachable).

**Churn must not become mail.** A re-fold that lands on the same status as last time must not
re-enqueue — `fold_status_for_milestone` compares against the most recently queued STATUS row for that
submission (`coord.portal_sync._last_queued_status`, any outbox state, not just `applied`) before
calling `enqueue_status` at all. This is on top of, not instead of, the portal's own `applyUpdate`
dedupe — that guard alone only saves the portal a write, it does not save the outbox from filling with
(or the customer from a second identical notification past) an unchanged push.

**A portal outage must never block a merge.** Same posture as every other call in this bridge: the fold
enqueues locally into the same durable outbox `enqueue_status` always has; the drain (`_push`) retries
independently on its own schedule.

**What does not fold automatically, and why:**

* **Describing · In design · Awaiting your sign-off** — these precede the milestone even existing (an
  operator hasn't run `coord milestone assign` yet, so there is nothing to fold over) or are already
  driven by a different, already-wired mechanism: the design-round push (PDR-3/#2508) and its
  `ANNOUNCING_STATUSES` ordering guard (`awaiting-signoff` requires a confirmed-applied `design_round`
  row first — dogfood #835).
* **Quality check** — also an `ANNOUNCING_STATUSES` entry (requires a confirmed `preview` row,
  #2359/coord-portal#107) with its own trigger (a Cloudflare Pages Preview deploy), a different source
  of truth from the issue-closed fold above. Automating *that* push is explicitly out of scope for
  #2588 — see its "Not in scope" section — filed separately if wanted.
* **Needs your input · On hold** — both require a judgment call this fold deliberately does not make:
  "needs-input" needs an actual open question queued (`enqueue_question`, its own `ANNOUNCING_STATUSES`
  entry); "on-hold" needs the business-time threshold `PLATFORM_EVOLUTION.md` defines, which this fold
  has no clock for.

Tested in `tests/test_portal_sync.py` (`fold_submission_status`'s pure cases; the unlinked/no-issues
no-ops; the unchanged-status churn guard; a GitHub read failure surfacing without raising) and
`tests/test_merge_queue.py` (`_maybe_push_status`'s merge-time trigger, mirroring
`TestDesignRoundPushOnMerge`).

## Running one end to end — the operator runbook

**This is the section to read when you have forgotten what to do.** The customer loop above is
what the *customer* experiences; this is the sequence *you* execute, in order, with the commands.
Every step names what it needs from the one before it.

### The two things that are easy to get wrong

**`signoff.approved` means two different things, and coord cannot tell them apart.** It is emitted
both when *you* click **Start work** in the portal — `coord-portal`#132 deliberately reused the
sign-off event shape rather than minting a `work.requested` kind — and when *the customer* approves
a design round. The TUI's **Approved work items** panel keys on that one event, so it is
simultaneously your "start here" inbox and a log of past customer approvals.

**Issues are created BEFORE mocks, not after.** See the callout under the customer loop. The
practical consequence is that a repo, a milestone, a tracking issue and the issues themselves all
have to exist before the customer can be shown anything.

### The sequence

| # | Step | Where | Needs |
|---|---|---|---|
| 1 | Promote lead → request; assign or create the client + project | portal UI | — |
| 1a | Create the repo, if none exists yet | `coord repo add` | — |
| 2 | Map the portal project to that repo | `coordinator.yml` | 1a |
| 3 | Click **Start work** on the submission | portal UI | submission still at `describing` |
| 4 | Pull it into a decomposition session | TUI, or `coord portal decompose-chat` | 2 + 3 |
| 5 | Author the Gate-A mocks | `coord acceptance mock` | the milestone + tracking issue from 4 |
| 6 | Publish the mocks to the customer | PR merge, or `coord portal publish-mocks` | the `coord portal link` from 4 |
| 7 | Customer approves or requests changes | portal UI | — |
| 8 | Pipeline runs; status folds itself | automatic | the link from 4 |

**1a — creating a repo.** `coord repo add <name> --github owner/repo --machines dellserver,elitebook`
writes the `coordinator.yml` entry into the coord-settings checkout, adds the repo to those
machines, creates the `coord` and tier labels, then prints the residue it deliberately did *not* do
(the clone itself, mostly). Follow with `coord repo doctor <repo>` — a repo with no graphify graph
on any machine that runs workers is CRIT and fails that gate.

Do this **before** step 4. `dispatch_decomposition_chat` refuses outright when a submission has no
mapped repo, and refuses again when no *single* machine claims *every* mapped repo — a session that
can only reach some of the repos it is meant to decompose into is treated as worse than no session.

**2 — the project↔repo mapping.** `portal.project_repos`, hand-edited. No `coord` subcommand writes
it:

```yaml
portal:
  project_repos:
    - project_id: "<the portal's opaque project id>"
      repos: [natal-chart]
```

The live file is `~/.coord/coordinator.yml` on the daemon host, which is a **symlink into the
`coord-settings` checkout** (`~/src/coord-settings/coord/coordinator.yml` — see
`coord.fleet_config_health`). Edit it there and commit; editing through the symlink works but
leaves the change untracked on one machine.

`project_id` is the portal's own opaque identifier, carried on `submission.created` and never
re-sent by a later event. There is no human-readable project name on the wire, by design — see
[Client + project identity](#client--project-identity--confirmed-wire-shape-2586-coord-portal146)
above. An unmapped project is a normal state, not an error: the panel renders `— no mapping —` and
keeps the row, precisely so you can see it needs mapping.

**3 — Start work.** The override is gated on the submission still being at `describing`, and the
card disappears from the portal once coord pushes it past that — so if you have already run a
design round, this is not the button you want.

**4 — the decomposition session.** In the TUI: open the **Approved work items** panel (`✓` in the
activity bar), right-click the row, **Pull into decomposition session**. That menu item is a
one-item context menu, greyed with a `no repo mapping` hint until step 2 is done — a disabled item
is inert, so nothing happening on click is the mapping telling you it is missing. CLI equivalent:
`coord portal decompose-chat <submission_id>`.

It dispatches an interactive `type="decomposition-chat"` session — same family as "Chat about
issue" — whose whole job is to write. It decides oracle-loop-shaped or not
([`ORACLE_LOOP.md`](ORACLE_LOOP.md)), then runs `coord milestone create` → `coord issue create` ×N
→ `coord milestone add-child` ×N → `coord drive-queue add` ×N → `coord portal link <repo>
<milestone_number> <submission_id>`.

**That last command is not optional.** The link is the join key for everything downstream (see
[Automatic status push](#automatic-status-push-2588) above, "The link is the join key"): with none recorded, no design round
can be published, no status ever folds, and a `changes_requested` verdict cannot resolve where to
dispatch its amendment. A decomposition that produced a **single one-off issue with no milestone**
cannot be linked at all today — `coord portal link` has no non-milestone form (#2665). The session
is instructed to state that gap explicitly rather than invent a milestone number; if you see it in
the session's summary, that submission's customer loop cannot complete until #2665 lands.

**5 — Gate-A mocks.** `coord acceptance mock <repo> <tracking_issue>` dispatches an independent
`mock-author` agent that writes `tests/acceptance/ms-NN/contract.md` plus `mocks/*.html` and opens a
PR. It is the one dispatch type permitted to write under `tests/acceptance/`. Use `--amend` for a
targeted correction to an already-merged contract rather than falling back to a plain `coord assign`.

**There is no mocks-without-a-repo path.** The bundle is not a free-floating artifact: it is files
inside the repo, written by an agent on a machine that has the repo checked out, rendered from the
milestone's open issues, and published from that checkout.

**6 — getting the mocks in front of the customer.** Two paths, one shared helper
(`coord.portal_sync.push_design_round_bundle`: upload to R2, reshape, queue the D1 metadata):

* **Automatic**, when the `mock-author` PR merges — `coord.merge_queue._maybe_push_design_round`.
  Fails **open**: no portal link on file is a silent "nothing to do yet, run `coord portal link`".
* **On demand** — `coord portal publish-mocks <repo> <tracking_issue>` reads whatever is in *this
  machine's* local checkout under `tests/acceptance/ms-NN/`, uncommitted changes included, no merge
  required. Fails **loud**: a missing link, missing portal config or an empty bundle is a clear
  error naming the fix. This is the one to use while iterating on a mock.

The customer sees **Awaiting your sign-off** only once the `design_round` row is confirmed applied —
the `ANNOUNCING_STATUSES` ordering guard (#835), so the notification never outruns the thing it
announces.

**7 — the verdict.** `changes_requested` is fully automatic: `coord.portal_sync._amend_from_verdict`
dispatches `coord acceptance mock --amend` carrying the customer's comment verbatim, and the event
stays unconsumed (retried every tick) if anything stops that dispatch, so the client's feedback is
never silently dropped. `approved` does **nothing** automatically — whether it should auto-record
`coord gate-a --approved` is the open policy question #2509 deliberately left undecided. It simply
re-lands on the Approved-work-items panel.

**8 — from here the pipeline is unchanged.** Status folds automatically off the linked milestone
(Planned → In progress → Shipped — see [Automatic status push](#automatic-status-push-2588)).
Optionally gate the merge on a real preview build with `coord portal enqueue-preview` and the
`preview.approved` / `preview.changes_requested` events — the
[`portal-followup`](../coord/skills/portal-followup/SKILL.md) skill has that procedure, and is also
where to start when an event "hasn't shown up" (short version: `coord portal
events`/`outbox`/`sync` must run **on the daemon host**).

### Known gaps in this flow, as of 2026-08-23

| Gap | Effect | Issue |
|---|---|---|
| Mirror clobber — `customer_json` keeps only the last event's payload instead of the merged facts | every Approved-panel column renders blank, and `project_id` reads empty so no row can map to a repo | #2585 (cause, fixed on `main`) · #2659 (repair the already-damaged rows) |
| The panel never drops a row | long-shipped submissions still read as "ready to pull" | #2660 |
| Un-started requests never reach the panel | a new request is invisible unless someone clicks **Start work** | #2661 |
| `coord portal link` requires a milestone | a one-off issue decomposition can never be linked | #2665 |

Until #2659 is fixed **and deployed to the daemon host**, step 4 cannot succeed for any real
submission: `project_id` reads empty, `repos_for_project("")` returns `[]`, and the pull action
stays disabled no matter what `portal.project_repos` says.

## Decisions

### Hosting: Cloudflare

| Piece | Choice | Why |
|---|---|---|
| Static site | **Pages** | same platform as the API — one deploy lane, no cross-origin boundary |
| API | **Workers** | same platform, no server, no standing cost |
| Records | **D1** | real SQL over SQLite; the record model is relational and small |
| Artifacts | **R2** | mock bundles + screenshots; no egress fees |
| Auth | **Access** | see below |

**On Durable Objects:** they are the right tool for a strongly-consistent per-object actor with
ordering guarantees and WebSocket push. A poll-based inbox needs neither. Start on D1; DO is the
upgrade path if live updates become worth it. **KV is wrong here** — eventual consistency and a queue
are a bad pair.

Standing cost is a domain name plus almost certainly $0 of Cloudflare's free tiers at this volume;
budget ~$5/mo for headroom. Compare to ~$24–26/mo for the Azure path, most of it buying a SPOF.

### Repo: separate, public, MIT

**Separate.** A public-facing web app has a different release cadence, a different threat model, a
different toolchain, and a different set of secrets from the coordinator. It should not ride the
coordinator's PyPI release lane or its four deploy surfaces. This is the same class of question as
the open **#1850** (*should `tui/` be extracted into its own repo*), and the answer here is clearer
than there: the portal shares no build, no runtime, and no deploy path with `coord`. Only the sync
contract couples them, and a contract is exactly what a repo boundary should carry.

**Public.** The portal is a thin client over a fleet it cannot reach — it holds no credentials that
grant execution, no repo access, and no path inward (see *The security posture*). Its security rests
on the tailnet ACL, the outbound-only bridge, and Access — never on the source being unreadable. So
publishing the sync contract costs nothing, and the reflex to re-close the repo "to be safe" should
be resisted: it would buy obscurity in exchange for a second release lane.

> **Consequence — customer material never enters the repo.** Intake text, design rounds, mocks and
> screenshots live in **D1 and R2 only**. Fixtures, seed data, and E2E specs use synthetic
> submissions. A private repo would have made this a property of the host; a public one makes it a
> rule that has to be held deliberately — which is why it is written down here and carried in #1980
> and #1983. It is also the cleaner boundary: code in git, customer data in storage, nothing that
> lives in both.

**Secrets** follow from public: the Cloudflare API token, account and zone ids, and any provider key
live in GitHub Actions secrets and `wrangler secret put` — never in `wrangler.toml`, never in a
committed `.dev.vars`.

**MIT.** Note the asymmetry, and that it is deliberate: `code-coordinator` is **FSL-1.1-MIT**
(source-available, no competing service for two years, then MIT), while the portal is permissive from
day one. The portal is an intake and sign-off surface for a coordinator fleet — without one it is a
form that talks to nothing. The defensible part is the pipeline it feeds (gates, oracle, review), and
that part keeps its FSL. Matching FSL here would protect nothing that isn't already protected, at the
cost of making the one component we might want others to read or reuse harder to adopt.

**Domain:** a `heurontech.com` subdomain, DNS on Cloudflare — natural once Cloudflare is the host.

### Auth: Cloudflare Access for v1

Customers here are a small, known, contactable set — `PLATFORM_EVOLUTION.md`'s own framing. Access
(Zero Trust) sits in front of Pages and Workers, federates to Google/GitHub or email OTP, is free at
this seat count, and requires **no authentication code in the application at all**. The Worker reads
the verified identity from the injected JWT.

The limit is honest and worth stating: **Access does not do self-serve signup.** The day the portal
needs open registration, this is replaced by a hosted IdP (WorkOS/Clerk/Auth0) or the OIDC path
`PLATFORM_EVOLUTION.md` describes. Until then, paying that complexity buys nothing.

### Notifications: email, digest-first

The loop only works if "come back later" actually reaches the customer. Email via a transactional
provider is the v1 channel — a design round is ready, a question was raised, work shipped. Prefer a
digest to instant sends; a customer does not need to watch the pipeline breathe. Per-recipient
quiet hours are a v2 refinement, not a v1 requirement.

### Deployment artifacts (images / Helm): a different question, deliberately deferred

Raised alongside this design, but it belongs to a different milestone. The portal is serverless —
there is no image and no chart to build for it. The image/Helm question is about **making the
coordinator self-hostable by someone else**, which is `PLATFORM_EVOLUTION.md` Phase 6 and depends on
the Cloud API extraction (Phase 3) that this design was specifically written to *avoid* needing.

Answering it now would drag the entire Azure/Postgres/Cloud-API sequence back in as a portal
prerequisite — which is the exact coupling this document removes. Keep them separate: the portal
proves the product hypothesis cheaply; self-hosting is a packaging decision to make once there is
something worth packaging.

## What v1 does not include

Multi-tenancy beyond a handful of known customers · self-serve signup · billing · a live chat
interface · any customer-visible git · customer-initiated dispatch · SLA or delivery-date commitments
· a mobile app (the site should simply be responsive).

## Risks

**Latency is a product decision, not a bug.** Poll cadence sets the floor on "how long until someone
looks at this." The UI must set expectations honestly rather than implying real-time.

**The daemon is a liveness SPOF.** If dellserver is down, intake queues safely but nothing advances.
The heartbeat makes this visible instead of silent; it does not make it not happen. Note this is
*already* true of the whole system, and #1825 (relocatable daemon) is the work that addresses it.

**Decomposition quality is the actual product risk.** Everything here is plumbing around one
unproven question: *does plain-language intake reliably become a decomposition an engineer accepts
with only light editing?* `PLATFORM_EVOLUTION.md` deferred that question to the very end. This design
pulls it forward — which is the main reason to build it. **#835 is the issue that answers it**, and a
disappointing result there is a successful outcome for this milestone.

**Scope creep toward a live chat.** The async constraint is what makes this cheap. The first "can we
just add a chat box" turns the portal back into Phase 5.

## Open questions

1. **Does the portal store sign-off history durably, or is coord canonical?** The ownership rule says
   the portal owns customer-authored facts — which makes it the system of record for what a customer
   agreed to. That is an audit surface, and it argues for the portal DB being backed up as seriously
   as `coord.db` (#1822).
2. **Where do the portal's own issues live** once the repo exists? Bridge work is coord-side;
   site work is portal-side. Splitting a milestone across repos does not work with
   `coord milestone` today (the work-order validator rejects cross-repo edges).
3. **Can an engineer edit a design round before it reaches the customer**, or only accept/reject it?
   Editing is more useful and much more work.
4. **Does *On hold* surface to customers at all?** Flagged as the most opinionated knob in
   `PLATFORM_EVOLUTION.md`, and still unanswered.

## Work order

See epic **#836**. The keystone is the **sync bridge** — every other issue is either upstream of it
(the record model it moves) or downstream (something that uses it). Build it early and thin, and
prove it moves one row in each direction before building anything on top.
