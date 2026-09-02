# ADR: Which `coord` does `coord-web`'s CI install?

**Status:** Accepted
**Date:** 2026-08-19
**Issue:** #2006 (UX-4, Phase 1 of milestone #62, epic #2002). Depends on
#2005 (the `coord-web` repo exists) and #2004
([`docs/ADR_COORD_WEB_DIST.md`](ADR_COORD_WEB_DIST.md), how a built bundle
reaches the daemon host).

## Context

`coord-web` is nominally a **pure HTTP client** of the coord daemon — that
framing is most of why epic #2002 could split it out of this repo at all.
Its CI mostly agrees: `.github/workflows/ci.yml`'s `checks` job (`npm run
typecheck`, `npm test`, `npm run build`) needs nothing from here.

Its Playwright jobs do not agree. `live-update-fixture.spec.ts` and the
sealed acceptance config (`playwright.acceptance.config.ts`) declare a
Playwright `webServer` that boots a **real** `coord web --fixture <file>
--dist dist` process (#1818) rather than Vite dev — deliberately, because
the thing under test is the frontend against the *server it will actually
talk to*, not against a mock of it. So `coord-web`'s CI must put a `coord`
binary on the runner, and in doing so it encodes a cross-repo contract:
**which `coord` this frontend is proven against.**

That is a version spec living in another repo's workflow YAML, which is
precisely the shape of fact this fleet has been burned by twice:

- `~/.coord-cli-venv` was found **three releases stale** on 2026-07-29 —
  silently, because nothing measured it. That incident is why
  `coord.health.checks.deploy_lane_facts.probe_cli_venv` exists at all.
- vimcode#615 (#1629): CI built on rustc 1.97.1 while every fleet machine
  was months behind; six snapshot tests were green on every machine and red
  in CI, and the recorded Test verdict said `passed`. That is why
  `coord.health.checks.toolchain` learned to parse a repo's workflow YAML.

The issue names this directly: *"A pinned version that drifts is the
`~/.coord-cli-venv` failure all over again. Whatever is pinned must be
visible and assertable."*

## Decision

**Track latest — `pip install 'code-coordinator[server]'`, never an exact
pin — and make the spec assertable from this repo.**

Three parts, all load-bearing:

### 1. Track latest, not `==`

The question `coord-web`'s Playwright jobs exist to answer is *"does this
frontend work against the coord people actually have?"*. An exact pin
answers a different and much less useful question — *"does this frontend
work against the coord we froze in March?"* — and answers it **green**
while real users are broken. The failure mode of an exact pin here is not
"CI is annoying", it is "CI is confidently wrong", which is the
`~/.coord-cli-venv` failure with a different filename.

Tracking latest inverts that: when `coord web`'s flags or API change
incompatibly, `coord-web`'s CI goes red on the *next PR*, not on a user's
phone. **A red `e2e`/`acceptance` job in `coord-web` after an unrelated
frontend change is a signal about `coord`, not about that PR** — see "What
a red means", below, which is the whole point of writing this down.

The costs are real and accepted:

- **Non-reproducible.** The same `coord-web` commit can go green today and
  red tomorrow with no `coord-web` change. That is correct — the contract
  it verifies genuinely changed — but it means a red must be *attributable*
  (part 3).
- **Third-party blast radius.** A broken `code-coordinator` release reddens
  every open `coord-web` PR. Accepted because both repos are this fleet's
  own, the release cadence is ours, and `coord release verify` already
  guards the publish side. It would not be accepted for a vendor package.

`~=` and `==` are both rejected: on a single `0.x` line `~=0.4.90` freezes
the series just as effectively.

### 2. `[server]`, not the bare name — and `code-coordinator`, not the tombstone

Two spelling traps, both silent-ish and both worth asserting:

- **The extra.** Since the base/`[server]` split (#1237) the bare
  distribution installs a *client-only* coord. `coord web` is a **server**
  command — uvicorn/Starlette live behind the extra. CI needs
  `code-coordinator[server]`.
- **The name.** #2006's own text says `claude-coordinator[server]`; that is
  now wrong. Per #2106 (epic #2096's rename), `claude-coordinator` is a
  permanent PyPI **tombstone** — PyPI cannot rename a project, so it will
  never gain another release. CI asking for it does not get a *stale*
  coord, it gets that tombstone's last-ever version, forever, with no
  upgrade path and no error. The live name is **`code-coordinator`**.

A `>=` floor is *allowed and encouraged* (`code-coordinator[server]>=X.Y.Z`)
to document the oldest `coord web` whose flags the fixture config relies on.
It does not constrain what CI resolves — pip takes latest either way — so it
is documentation with an assertion attached, not a pin.

### 3. The spec is assertable from this repo: `coord_web_ci_pin`

A decision that lives only in a document is the thing that rotted last time.
`coord/health/checks/coord_web_ci_pin.py` (machine scope, `coord health`)
reads a local `coord-web` checkout's `.github/workflows/*.yml`, extracts
every `pip install` requirement naming this distribution, and grades it:

| finding | severity | why |
|---|---|---|
| no `coord-web` checkout on this machine | OK | absence is the common case, not a fault — same convention as `cli_venv`/`tui_binary` |
| no `pip install` of coord in any workflow | WARN | the Playwright `webServer` cannot boot `coord web --fixture` |
| installs `claude-coordinator` | CRIT | the PyPI tombstone (#2106) — frozen forever, no upgrade path |
| missing the `[server]` extra | CRIT | client-only coord; `coord web` cannot serve (#1237) |
| `==` / `===` / `~=` exact pin | WARN | the silent-rot shape this ADR rejects |
| `>=` floor newer than this machine's coord | WARN | floor names an unreleased version, or this box is behind |
| otherwise | OK | headroom prints the literal spec string and where it came from |

It is **local-filesystem only** — no `gh`, no network — so it is cheap
enough for the health-poll tick, and it follows the module's standing rule:
**annotate, don't gate.** Nothing here blocks a dispatch, a routing
decision, or a merge; the severities say "go look at `coord-web`'s
`ci.yml`", never "stop".

The checkout is discovered from `repo_paths` (a checkout named `coord-web`),
falling back to any checkout carrying `playwright.acceptance.config.ts` at
its root — the marker that *is* the coupling. `health.coord_web_checkout`
overrides. Per this repo's standing convention, `None` means "discover it",
never "disable the lane": a silently-off lane is indistinguishable from a
healthy one, which is the failure being guarded against.

## What a red `coord-web` CI job means

This is the part that has to be an understood signal rather than a mystery,
so state it plainly. When `coord-web`'s `e2e`/`acceptance` job fails and the
PR under it touched no relevant frontend code:

1. **`coord web`'s contract changed.** A flag was renamed, an endpoint's
   shape moved, SSE framing changed. This is a genuine contract break and
   the red is *correct*. Fix forward in whichever repo owns the break — and
   note that the same break would have reached a phone within ~10 minutes
   via `coord-web-dist-build.timer` (#1543, see
   [ADR_COORD_WEB_DIST](ADR_COORD_WEB_DIST.md)), so CI catching it first is
   the system working.
2. **A bad `code-coordinator` release.** Yank/fix the release. Do **not**
   exact-pin `coord-web` to route around it — that trades a loud, dated,
   one-afternoon outage for the silent, undated kind.

The one thing that is *not* an acceptable response to either is silencing
the job (`continue-on-error`, deleting the install step, pinning to the last
known-green version and walking away). That is exactly how ms-51 rotted
unnoticed through #1547–#1818 (#1950), and it is why
`.github/workflows/acceptance-web.yml` in this repo carries the same warning
in its failure handler.

## Where the acceptance suite lives is still open

#2006 scoped three jobs: `webapp-types`, `e2e`, and `acceptance`.
`coord-web`'s `ci.yml` (landed with the split, #2005) already carries the
first two — `checks` covers `npm run typecheck` (plus unit tests and a
production build), and `e2e` runs the Playwright suite *including* the specs
that boot a real `coord web --fixture` through `e2e/fixtureServer.ts`
(`live-update-fixture`, `available-gates-terminal`, `realtime`), installing
`code-coordinator[server]` from PyPI exactly as decided above. So the
acceptance criterion's "demonstrably booting the fixture server rather than
Vite dev" is already met by `e2e` today: those specs `spawn()` the CLI on a
free port and fail loudly (`coord web --fixture never became ready`) rather
than silently falling back to `npm run dev`, which is what the rest of the
suite uses.

The **sealed oracle-loop `acceptance` job** (`tests/acceptance/ms-NN/`,
driven by `coord acceptance run --all --ci`) is deliberately *not*
duplicated into `coord-web` yet: where the sealed suite lives post-split is
an explicitly open question tracked as #2007 (UX-5), and `coord-web`'s
`ci.yml` header says so. Standing that job up in `coord-web` before #2007
decides would mean either a second copy of the suite or a `coord-web` CI job
that reaches back into this repo's `tests/acceptance/` tree — both of which
#2007 exists to avoid choosing by accident. Once #2007 lands, the job it
adds inherits this ADR's install spec unchanged, and `coord_web_ci_pin`
grades it the same way it grades `e2e`'s.

The `generated.ts` drift gate is likewise deferred, for the same class of
reason, as #2258. #3045 fixed the packaging half of that story: the
generator now lives at `coord/codegen.py` — a real module of the `coord`
package — rather than at `scripts/codegen.py`, which `[tool.setuptools.
packages.find]` never shipped (`include = ["coord*"]`; `scripts/` is not a
package). Before #3045, "installs `code-coordinator[server]` from PyPI ...
to get this script" a few paragraphs up was aspirational — a consumer repo's
CI had no way to actually reach it. The invocation `coord-web`'s CI should
use, once #2258 stands the job up, is:

    python -m coord.codegen --check --out src/api/generated.ts

(or the equivalent `coord codegen --check --out ...`, once a `coord` binary
is on the runner's `PATH`) — no checkout of this repo required, just the
`[server]` extra this ADR already mandates.

## Rejected alternatives

**Exact-pin to a released PyPI version.** Reproducible, and the intuitive
default. Rejected because it makes `coord-web`'s CI answer the wrong
question and answer it green — see part 1. Its one genuine advantage
(bisectable CI history) is not worth a gate that is confidently wrong by
construction, and the drift is exactly the `~/.coord-cli-venv` shape.

**Exact-pin, plus a bot that bumps the pin.** Gets reproducibility *and*
freshness, but only as fresh as the bot, and it adds a bump-PR-per-release
to a two-repo fleet's review load. Revisit if the track-latest reds ever
become frequent enough to be noise — the `coord_web_ci_pin` check's data is
what would tell us, since it records the spec on every health tick.

**Install `coord` from a git ref of this repo instead of PyPI.** Would test
against unreleased `main`, catching breaks even earlier. Rejected: it makes
`coord-web`'s CI red for changes that never ship, needs credentials or a
public clone step, and tests a coord no user has. PyPI-latest is the coord
users actually get (`pip install code-coordinator[server]`), which is the
population the contract is about.

**Assert the pin in `coord-web`'s CI instead of via `coord health`.** A
self-check inside `coord-web` (grep your own YAML) cannot compare against
anything this fleet knows — not the released version, not what the machines
run. And it only runs when someone opens a `coord-web` PR, which is exactly
when the pin *isn't* rotting. The health tick runs regardless.

## Consequences

- `coord-web`'s CI install line is `pip install 'code-coordinator[server]'`
  — unpinned, `[server]`, live distribution name. A `>=` floor may be added
  to document a minimum without changing what resolves.
- `coord health` gains a `coord_web_ci_pin` row on any machine with a
  `coord-web` checkout; every other machine reports OK/absent.
- A `coord-web` CI red on an unrelated PR is a **contract signal**, with the
  two legitimate responses enumerated above and one forbidden one.
- The sealed acceptance job and the `generated.ts` drift gate remain open
  follow-ups (#2007, #2258), not silent omissions.
- The `coord-web`↔`coord-serve` runtime compatibility floor named as an open
  question by [ADR_COORD_WEB_DIST](ADR_COORD_WEB_DIST.md) is **not** solved
  here. This ADR pins what CI *tests against*; that one is about what a
  phone loads at runtime. They are separate, and the second is still open.
