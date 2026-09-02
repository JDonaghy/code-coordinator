# Choosing a data store — D1, Supabase, or managed Postgres

**This is a reasoning guide, not an approved-vendor list.** It records *how* to
choose so the choice can be argued, not *what* to choose so the choice can be
skipped.

That distinction is deliberate and load-bearing. `house_stack_context()`
(`coord/decomposition_chat.py`, #2997) already tells an intake session what the
fleet actually runs, and it derives that **mechanically from each repo's tracked
files** precisely so no one has to maintain a blessed-stack list — because such a
list rots, and because it suppresses the reasoning the decision archive exists to
capture. Nothing in this file should ever be read by code. If you find yourself
adding a vendor here so that a session will pick it, you want a tracked file in a
repo, not a paragraph here.

## The comparison people actually ask for

D1 and Supabase get compared as though they were the same kind of thing. They are
not, and that is the most useful thing to know about them.

- **D1 is a database binding for Cloudflare compute.** It is reachable
  ergonomically only from a Worker or a Pages Function. That is the premise, not a
  limitation to route around. On Cloudflare it is nearly free and has no
  connection-pooling story to get wrong, because there are no connections. Off
  Cloudflare it is close to irrelevant.
- **Supabase is a backend platform that happens to be Postgres.** Its real product
  is row-level security plus a client SDK, which lets the browser talk to the
  database directly and deletes your API tier.

That second point is the one that decides most cases: **if you are building an API
tier anyway, Supabase's central advantage evaporates**, and you are paying platform
opinions for a Postgres you could get anywhere.

## The axis that decides it

Not the feature matrix. Two questions, in this order:

1. **Where does the compute already live?** The store should sit next to it. This
   is why grocery-list is on D1 and why that was right — the alternative was
   adding a vendor, an account, a secrets lane and a deploy path to a shop that
   already runs Cloudflare end to end.
2. **Do clients talk to the store directly?** If yes, Supabase's RLS model is
   genuinely differentiated. If no, it is a Postgres with extra steps.

Only if both of those leave it open does the feature comparison matter.

## When each earns it

| | Reach for it when | Don't when |
|---|---|---|
| **D1** | Already on Workers/Pages. Modest dataset. Relational shape. No realtime requirement. Spiky or low traffic — billed per row, not per hour. | You need Postgres extensions, realtime subscriptions, or the data will outgrow a single-digit-GB ceiling. |
| **Supabase** | You want Postgres features *and* to skip building an API — RLS carries authorization. Realtime matters. Auth + storage + DB as one product. Portable; self-hostable later. | You are building an API tier regardless. |
| **Managed Postgres + your own API**<br>(Azure Flexible, RDS, Cloud SQL) | Already in that cloud estate. Network isolation, compliance boundaries, data residency, or directory integration. Extensions or scale past a PaaS ceiling. You have an ops function. | It is a side project or a single-household app. Always-on instance cost plus patching, pooling, backups and monitoring, for no benefit. |

Ops burden runs zero → low → real across those three, and cost runs the same
direction. For a household grocery list the third option is orders of magnitude of
overhead for nothing, which is why SUB-1EA1D3's decision [13] rejected it without
much agonising.

## The fact that stops D1 being a trap

**Cloudflare Hyperdrive gives a Worker pooled, cached connections to any
Postgres** — Neon, Supabase, Azure, RDS. "We are on Cloudflare" therefore does
*not* commit a repo to D1 for life. If an app outgrows D1 you keep the compute and
swap the store.

So D1 is not a bet to agonise over. It is the cheap default you graduate from, and
the graduation path is short. Weigh it that way rather than as a one-way door.

## Two options worth knowing about

- **Neon** — serverless Postgres, scale-to-zero, a database branch per PR. Often
  the right answer when you want real Postgres without a platform layer on top.
  Pairs naturally with Hyperdrive.
- **Turso** — libSQL: D1's shape, multi-region, not Cloudflare-bound. Relevant if
  the SQLite model fits but the vendor tie does not.

Neither is in use here today. They are listed because "D1 or Supabase" is a false
binary, and an intake session that only knows those two will argue itself into one
of them.

## The limitation most likely to bite first

Not size. **D1 has no realtime subscriptions.** Any feature described with the
words "sees the other person's change appear" needs polling, Durable Objects, or a
different store. SUB-1EA1D3 conceded exactly this — two people shopping in
different aisles will not see each other's check-offs live — and it is the single
most likely reason a future app in this fleet moves off D1.

Check current D1 size and pricing limits before relying on a specific number;
Cloudflare has been moving them.

## Related

- [`CUSTOMER_FACING_APPS.md`](CUSTOMER_FACING_APPS.md) — the preview/UAT pattern
  that a hosting choice has to support, and the reason the deploy lane is part of
  a stack decision rather than a consequence of one.
- [`CUSTOMER_PORTAL.md`](CUSTOMER_PORTAL.md) — where stack decisions get recorded
  and confirmed (`coord portal decision`), which is the archive this guide exists
  to make arguable rather than to replace.
