# Customer-facing apps — shipping to someone who judges it by looking

**Operator-facing.** A worker or reviewer does not need this file.

Most repos in this fleet ship to *you*. A few ship to **a real external person
who forms an opinion by looking at the running app**. natal-chart is the first;
there will be more, because the customer portal
([`docs/CUSTOMER_PORTAL.md`](CUSTOMER_PORTAL.md)) exists to turn strangers'
requests into repos.

That difference changes what "done" means, and the standard pipeline does not
account for it. This file is the pattern for the ones that do.

## The test: is this repo customer-facing?

Not "does it have a UI". The question is:

> **When this repo's `main` moves, does a human outside the fleet see the
> result — and would they judge it on how it looks, not just whether it works?**

If yes, this pattern applies. natal-chart: yes. coord-web and the phone webapp:
they have UIs, but the only person who sees them is the operator, who is also
the person who can fix them in ten minutes — so no. vimcode, quadraui,
claude-coordinator: no.

The distinction is not about polish. It is about **who is positioned to catch a
mistake, and what it costs when they do.** You catching a rendering glitch in
your own dashboard is a Tuesday. A customer catching one is a support
conversation about trust, and the fix is no longer free.

## Why the standard pipeline is not enough

The default lane is `Work → Test → Review → Merge`, then whatever deploy the
repo wires up. Every one of those gates is real. None of them looks at the app.

- **Test** runs the suite. A suite asserts what someone thought to assert.
- **Review** reads the diff. A reviewer cannot see a diff that isn't there —
  which is the whole problem with a surface that was *left out* of a change.
- **CI** typechecks and builds. A build that compiles can render nonsense.
- **Merge** fires the deploy.

So for a customer-facing repo the real pipeline, as of 2026-08, is:

```
Work → Test → Review → Merge → auto-deploy → the customer notices
```

The customer is not a backstop. They are the **first human in the loop**, and
they are the one person whose confidence you are spending.

### The incident this file is made of

**natal-chart#42, reported 2026-08-23.** The Zodiacal Releasing tab rendered
zodiac signs as the operating system's colour-emoji badges — coloured circles —
instead of the elegant SVG glyphs used everywhere else in the app. The customer
reported it. Nobody else had.

Three things about it are worth internalising, because each one defeats a
different control you might otherwise trust:

1. **No commit caused it.** `.glyph { font-family: 'IM Fell English', serif; }`
   had been unchanged since the repo's first commit, and the ZR views had
   rendered raw Unicode text since v0.5.0 in March. U+2648–U+2653 carry
   `Emoji_Presentation=Yes`, so once the author's font stack misses them the
   browser substitutes a colour-emoji font. The same code looks correct on
   Windows and wrong on Linux. **A regression can have no diff, and can depend
   on the viewer's machine rather than yours.** "Nothing changed" is not
   evidence that nothing broke.

2. **A five-month detection lag.** The migration onto font-independent SVG
   glyphs landed in v0.11.1 on 2026-03-31 and covered every sign-rendering
   surface except two files. Nothing failed. **A surface left out of a
   migration is invisible to every gate we have** — it is defined by the
   absence of a change.

3. **The near-miss is the damning part.** natal-chart#28, titled *"unify
   PDF/screen symbols"*, edited **both** offending files — deleting their local
   symbol tables and importing the shared one — and passed Test and adversarial
   Review. It unified the **data** and left the **rendering path** alone. Worker
   and reviewer both read "unify" as satisfied because the named symptom was
   gone. **A consolidation PR that converts some consumers is diff-identical to
   one that converts all of them.**

Generic fix for (3) is claude-coordinator#2686 — a default review-checklist item
requiring consolidation PRs to enumerate every consumer. The rest of this file
is the fix for (1) and (2).

## The pattern

Six parts, in the order they act on a change. Each is marked with what actually
exists today — **check these markers before relying on a step.**

### 1. A per-PR preview deploy, built the same way as CI · **shipped (natal-chart)**

Every PR gets its own deployed URL. Non-negotiable: this is the only artifact a
non-technical person can be asked to look at. "Pull the branch and run it" is
not a customer workflow.

The preview must be **the same build CI tested**, not a separate ad-hoc one, or
you are signing off on something other than what ships. natal-chart's
`.github/workflows/deploy-cloudflare.yml` does this deliberately — see its
header comment and coord-portal#107.

**Getting the URL is not as simple as it looks.** Cloudflare Pages preview URLs
are **per-deployment hashes**, not branch slugs:

```
natal-chart (Preview)  ref=issue-39-transit-page-annual-profections-mock-up
    https://ed9f21ad.natal-chart-3ew.pages.dev
```

The hash is not derivable from the branch name, and the branch-alias URL
Cloudflare also publishes truncates at 28 characters, which these branch names
exceed. Read it from the forge instead — `cloudflare/pages-action` creates a
GitHub Deployment per PR whose latest status carries the real URL:

```bash
gh api "repos/{owner}/{repo}/deployments?ref={branch}" --jq '.[0].id'
gh api "repos/{owner}/{repo}/deployments/{id}/statuses" --jq '.[0].environment_url'
```

Match on the **environment name** (`"<project> (Preview)"`), not on recency —
production deploys are hash URLs too and interleave with previews in that list.

### 2. A `uat` gate between Review and Merge · **NOT BUILT — claude-coordinator#2687**

The gap that lets everything else fail. Today `merge.auto_drain: true` with
`max_per_tick: 1` means the daemon merges the moment Test + Review + CI go
green, and the push to `main` fires production. **There is no per-repo,
pre-merge operator hold anywhere in coord**: `coord pause` only stops agent
routing, and drive-queue's `--hold-after` (#1757) fires *after* the merge, which
for an auto-deploying repo is after the customer can already see it.

#2687 adds a `uat` gate ordered before `merge`, a
`coord uat <id> --passed|--failed` verdict modelled on `coord test`, and — the
part that decides whether it gets used or bypassed — **the preview URL surfaced
in `coord status` and the TUI**, next to the command that clears the gate. A
gate that makes the operator go hunting for the link will be routed around
inside a week.

### 3. Sign-off reaches the customer without you relaying it · **partially built**

The portal already models this: a **"Quality check"** status backed by a
confirmed `preview` row, triggered by a Cloudflare Pages Preview deploy
(#2359 / coord-portal#107). What is missing is the push — automating it was
explicitly out of scope for #2588 and never filed.

Until it exists, step 3 is you, by hand, in the runbook below. That is fine at
one customer and does not survive two.

### 4. Visual regression baselines · **NOT BUILT — natal-chart#47**

So the human is the *second* line of defence, not the first. Screenshot the
core screens against a **fixed fixture** — one hardcoded chart, a frozen clock —
and diff them in CI.

The honest caveat, worth stating because it is the trap: pixel diffing is flaky
across font rendering and browser versions, and **font fallback is exactly the
defect this is meant to catch**, so a tolerance loose enough to ignore font
differences would have sailed past #42. Pin the browser version, run in a fixed
container, start tight. If it proves too noisy, fall back to DOM-structure
assertions (*"this cell contains an `<svg aria-label='gemini'>`, not a text
node"*) rather than loosening the threshold until nothing can fail.

### 5. A guard for the specific class, once you have been bitten · **shipped as a pattern — natal-chart#44**

Screenshot baselines catch *what changed*. They do not catch a surface that was
never covered. When a defect turns out to be an instance of a class, add a
source-level guard for the class:

> Fail the build if any component outside `GlyphIcon.tsx` emits a literal in
> U+2648–U+2653.

Cheap, fast, and — the important property — **it does not depend on anyone
remembering to add the new view to a test.** That is precisely how #42 survived
five months of green suites.

Make the failure message name the file and state the fix. A guard whose output
does not explain itself gets deleted by the next person who trips it.

### 6. The acceptance bar says "appearance", explicitly

The fleet-wide rule in [`CLAUDE.md`](../CLAUDE.md#testing--black-box-coverage-is-the-acceptance-bar)
already demands a black-box test that drives the running app for any
behaviour-changing PR. For a customer-facing repo, read "behaviour" as
**including how it looks**. A PR that changes a rendered surface and ships only
unit tests has not met the bar, and the reviewer should say so.

## Deliberate non-choices

**No separate staging → production promotion.** The obvious next move is to make
`main` deploy to staging and add a promotion step for production. Rejected: it
adds a second thing to forget, and it buys nothing the per-PR preview does not
already provide — the preview *is* the exact build, before merge, at a URL you
can send to someone. Gate **before** the merge, not after. Revisit only if a
repo appears where the preview genuinely cannot represent production (real
payment flows, a shared mutable backend).

**No blanket rollout.** The `uat` gate ships off everywhere and is enabled per
repo, deliberately. Adding a human gate to a repo whose only user is the person
running the fleet is pure friction, and friction is what teaches operators to
bypass gates.

**Not a documentation-only answer.** A runbook line saying "remember to check
the preview" is the control that already failed on 2026-08-23. This file
documents a *mechanism*; where the mechanism does not exist yet it says so, in
bold, with the issue number.

## The manual runbook — what to do until #2687 lands

For a customer-facing repo, right now, with no gate to help you:

1. **Queue the work as normal** (`coord drive-queue add`). Nothing special.
2. **Watch for the PR.** The work leg completing is your cue — the branch is
   pushed and the PR opens shortly after.
3. **Get the preview URL** with the two `gh api` calls in step 1 above. The URL
   does not exist the instant the PR opens; the preview build has to finish.
4. **Look at it yourself first.** Walk the screens the change touched. You will
   catch most of it here, and it costs a minute.
5. **Send the customer the URL** and wait for a yes.
6. **Then let it merge.**

Step 6 is the weak one and you should know why: **you cannot reliably hold a
merge today.** `auto_drain` will take the PR as soon as its gates are green,
whether or not you have heard back. The available holds are all bad in
different ways — draft the PR on GitHub (targeted, but strands the merge-queue
entry in `NEEDS_ATTENTION`), or flip `merge.auto_drain: false` in
`~/.coord/coordinator.remote.yml` (a hard guarantee that stalls auto-merge for
every other repo in flight, and is easy to forget to switch back).

**This is exactly why #2687 exists.** Until it lands, on a change where the
customer's opinion genuinely gates the release, the honest options are to pick
one of those two holds consciously, or to accept that the merge may win the
race — as it did on 2026-08-24, when natal-chart#42 merged and deployed to
production during the conversation about how to review it before it shipped.

## Adopting this for a new repo

1. Wire a per-PR preview deploy that reuses the CI build. Verify the URL is
   readable from the forge's deployments API — do not assume the scheme.
2. Confirm production is reachable at a stable alias, distinct from the
   per-deploy hashes.
3. Add the repo's visual-regression harness *before* the first customer-visible
   feature, not after the first complaint. Fixed fixture, frozen clock.
4. Turn on the `uat` gate for the repo once #2687 ships.
5. Point the repo's own `CLAUDE.md` at this file, and make sure its deployment
   section is **true** — natal-chart's said "GitHub Pages" for a week after
   production moved to Cloudflare (fixed in #47).

## Related

- [`docs/CUSTOMER_PORTAL.md`](CUSTOMER_PORTAL.md) — intake, design sign-off, and
  the outbound bridge. **The customer loop this pattern plugs into.**
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — the pipeline these gates sit in.
- [`docs/MERGE_AUTO_DRAIN_TRUST_BAR.md`](MERGE_AUTO_DRAIN_TRUST_BAR.md) — why
  `auto_drain` is on, and the conditions under which it is trusted.
- [`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) — a merged fix is not a
  live fix. For an auto-deploying repo it usually is, which is the point.
