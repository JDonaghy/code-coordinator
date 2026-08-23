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
4. **Signed off.** The outcome definition becomes the **Gate-A contract**. The epic + issues are
   created through the forge seam, labelled `status:ready`, and **the existing pipeline takes over
   entirely unchanged.**
5. **Progress** rolls up in git-free language until *Shipped*.

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
