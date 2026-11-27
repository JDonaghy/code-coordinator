# The Oracle Loop — a tight, framework-pluggable acceptance loop

> **Status:** design + build slice, 2026-07-04. Refines [`PIPELINE_V2.md`](PIPELINE_V2.md)'s
> "Independent acceptance testing" and Gate A. Pipeline v2's merge-bounce, observability, and
> git-model parts are unchanged; this doc replaces *how acceptance testing works* and *how Gate A
> is run*. Build slice + issue map at the bottom.
>
> **See also [`ARCH_SECURITY_GATES.md`](ARCH_SECURITY_GATES.md)** (2026-07-10): the same
> author-once / check-cheaply move applied to **architecture** (a graph-queryable conformance lint)
> and **security** (a dedicated post-work lens), layered on this doc's milestone tier.

## The problem this fixes

The fix/test loop today crosses stage boundaries with **cold starts**. Work implements and
*releases*; a fresh Test agent spins up cold and records a fail; a fresh Fix worker spins up cold,
re-reads the issue + diff + failure to re-acquire context, attempts a fix, *releases*; repeat. Every
round is 2–3 fresh `claude` sessions each paying full context re-acquisition — re-reading the big
files every time. The intelligence resets to zero between attempts. That is the token bleed with no
visible progress, and it is what drives the operator to bail out and drive the branch by hand.

## The core idea — an oracle

An **oracle** is a test suite an agent can **RUN but cannot READ or EDIT**. It answers one question
— *"does the behavior match the contract? yes/no, and which cases fail?"* — and leaks nothing about
its internals. The worker iterates against it **in its own warm session** until green, then
releases. The loop collapses from "cross-stage, cold, 2–3 sessions per round" to "in-session, warm,
near-zero marginal cost per round." Context never resets.

**Independence is preserved by separating *authoring* from *running*.** Independence is a property
of *who wrote the test and when* — **not** of who runs it. A suite authored by a different agent
(`test-author`), from the Gate-A contract, *before the code existed*, is exactly as independent
whether the worker executes it or a separate stage does. So:

- **`test-author` writes** the acceptance suite from the contract, red, before any work.
- **The worker gets it *sealed*** — read-only, run-only. It can call `coord acceptance run` and see
  `2/5 green · test_x expected A got B`, but it cannot open or edit the test files. It iterates to
  green against a suite it did not write and **cannot game because it cannot see inside it**.

**The oracle runs twice, on purpose:**
- **In-session, by the worker** — for *speed* (the warm loop).
- **Externally, by the coordinator** against the pushed SHA — for *trust* (a headless worker can lie
  about "green"; it cannot fake the coordinator's own run). Same suite, two jobs.

**Why this is the lever for >90% UAT.** The oracle is a *proxy* for user acceptance. If it is a
faithful proxy — a function of contract fidelity — then "worker exits green against the oracle" ≈
"UAT will pass." The worker converges to acceptance behavior *before a human ever looks*. UAT stops
being where you **discover** problems and becomes where you **confirm** them; residual UAT failures
are exactly the cases the contract missed, which is a tight feedback signal on the contract.

## This is not waterfall

The waterfall pathology is *discovering you built the wrong thing after building it*. The oracle loop
front-loads discovery to the cheapest possible point and keeps it amendable:

1. **Mock-first Gate A.** Gate A produces a **viewable mock in the target medium**, not a text spec.
   You react to a rendered screen / wireframe *before* the expensive implementation. The mock **is**
   the contract fixture.
2. **The contract is amendable and versioned**, not frozen. When a shipped issue makes you want
   something different, you amend the contract → the test-author updates the affected slice → work
   re-syncs. A normal, cheap, logged operation.
3. **Only one slice is pinned ahead of the work** (just-in-time authoring). The full suite need only
   exist by Gate C.

Structurally: a fast **inner loop** (per-issue, converge-to-oracle) inside a steering **outer loop**
(amend the contract as the milestone takes shape). Iterative development with an *executable* spec.

**Mock granularity is milestone-only, tuned by milestone size (decided 2026-07-04).** The mock is
rendered once, up front, for the whole milestone — the more waterfall-leaning of the two options
(the alternative was a per-issue slice-mock). We deliberately chose milestone-only and treat
**milestone size as the tuning knob** instead of adding per-issue mock ceremony: a small (1–2 issue)
milestone's up-front mock *approximates* a per-issue mock, so shrinking milestones buys tighter,
later-binding UX discovery, while larger ones amortize Gate A. This is itself an empirical bet — we
can't know the right granularity up front, so we ship milestone-only and let observed behavior
(Gate-A overhead vs. mock-drift) tune the size. Revisit per-issue slice-mocks only if size-tuning
proves insufficient.

## Framework drivers — the oracle is framework-dependent

`coord acceptance` is a thin, framework-agnostic orchestrator over a **driver adapter declared per
repo** — the same shape as `smoke_tests.capability_rules` (files → machine capability). The driver
knows how to launch, drive, and assert on the app; the tests use the driver's API.

```yaml
# coordinator.yml
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver              # quadraui TuiDriver / TestBackend
      run: "cargo test --test acceptance -- --format json"
      mock: "*.screen"                 # text screen-grids: mock == assertion fixture
      capability: rust
    webapp:
      kind: web-playwright             # drives Electron too
      setup: "npm ci"                  # provisioning step, run once before `run` (#1733) —
                                        # `coord acceptance record`'s throwaway worktree has
                                        # no node_modules (gitignored); without this, `run`
                                        # fails `exit 127` (playwright not found) before ever
                                        # producing a verdict. Empty/omitted for a driver that
                                        # self-provisions (tui-tuidriver fetches its own cargo
                                        # deps; cli-pytest runs against the ambient env).
      run: "npx playwright test tests/acceptance"
      mock: "*.html"                   # static wireframe: reviewed, then asserted against
      capability: browser
```

| Medium | Mock format (agent-authored, human-reviewed) | Driver / assertion |
|---|---|---|
| **TUI (quadraui)** | `.screen` text grids — **mock == assertion** | `TuiDriver` / `TestBackend` (#690/#691) |
| **Web / Electron** | self-contained `.html` wireframe | Playwright, `browser` capability |
| **Native rich client** | SwiftUI / Compose previews, or a generated image | XCUITest / Espresso / WinAppDriver |

TUIs are the cleanest case: the UI *is* text, so the mock, the contract, and the assertion fixture
are the same artifact. **Known limit (coord-tui):** `TuiDriver` renders to `TestBackend`, so
raw-terminal / ANSI / SGR-mouse / embedded-PTY bugs are out of reach and stay on a thin live smoke
(the quadraui #302 pty+vt100 tier is unbuilt).

## The `coord acceptance` runner — concrete spec

One command, framework-agnostic above the driver:

| Subcommand | Who runs it | What it does |
|---|---|---|
| `coord acceptance mock --milestone NN` | Gate A dispatch (#930) | Dispatch a **mock-author** agent: render the viewable mock + write `contract.md` from it. |
| `coord gate-a --approved \| --changes <repo> <tracking_issue>` | Gate A sign-off (#2063) | Record the **human verdict** on `contract.md`, keyed to its content hash. Nothing downstream dispatches without one. |
| `coord acceptance author --milestone NN [--issue N]` | Gate A / JIT (#931) | Dispatch the independent **`test-author`**: write/extend the red acceptance suite from the contract. |
| `coord acceptance run --issue N` | **worker, in-session** | Run issue N's slice via the repo driver; return **structured** per-test pass/fail. Sealed: verdicts only, no test source. |
| `coord acceptance run --all` | coordinator (Gate C) | Run the full accumulated suite. |
| `coord acceptance run --all --ci` | **repo CI** (#2164) | Same as `--all`, but honors each manifest's `expected_red:` registry — see below. Point your repo's ordinary acceptance test step at this instead of the raw driver command. |
| `coord acceptance record --issue N --sha <sha>` | coordinator, **external** | Re-run the sealed slice against the pushed SHA; **write the verdict to the board** (the Acceptance box). The trust gate. Does **not** touch `expected_red:` itself (#2164 — see below for why and where that clear actually happens). |
| `coord acceptance stall --issue N --tried … --stuck …` | worker, on non-convergence | Emit the structured stall report + push a WIP snapshot → raises `needs-attention` (#846). |
| `coord acceptance expected-red <repo>` | anyone, read-only | List every live `expected_red:` entry on the default branch, via the GitHub API — no checkout needed. Flags an issue that's closed on GitHub but still carries entries as `STUCK` (#2164 acceptance criterion 4 — a long-lived entry is not invisible debt). |
| `coord acceptance propose-amendment --repo R --issue N DIFF_FILE --touches PATH --rationale TEXT` | worker/planner, in-session | Write a **sealed-amendment artifact** (JSON) from a plain unified diff — see "Sealed amendment as data" below. Never applies anything. |
| `coord acceptance verify-amendment --repo R ARTIFACT_FILE` | anyone, read-only | Apply the artifact's diff in a throwaway scratch copy and re-run the sealed suite there; print pass/fail. Never touches the real checkout. |
| `coord acceptance approve-amendment --repo R ARTIFACT_FILE --approved-by NAME` | **human, never a model** | Re-run `verify-amendment` fresh and, only if still green, stamp the artifact approved (who/when/a content fingerprint). |
| `coord acceptance apply-amendment --repo R APPROVED_ARTIFACT_FILE` | `test-author`, in-session | Apply exactly the approved bytes to this checkout as its own commit, then re-run the sealed suite there. |

**Sealing — climb the ladder:**
- **v1 (policy):** the acceptance dir is checked out in the worktree but listed in `files_forbidden`;
  the git-floor / reviewer flags any diff that touches `tests/acceptance/**` (tamper detection).
- **v2 (construction):** the worktree does **not** contain the acceptance source; `coord acceptance
  run` executes against a coordinator/daemon-held copy and returns only structured verdicts.

**Layout (per driver, in-repo, separate target):**
```
tests/acceptance/ms-NN/
  contract.md          # the black-box surface: CLI names, key screen text, API field shapes
  mocks/               # the viewable mocks == the assertion fixtures (*.screen / *.html / …)
    index.html          # HTML drivers only — navigation glue, see below
  <suite files>        # red at Gate A, extended JIT per issue; SEALED to the worker
  manifest.(yml|json)  # test-id → issue-slice mapping (drives --issue N)
                        # + optional expected_red: {issue: [test-id, ...]} — see below
```

**`mocks/index.html` — navigation glue between the flat mock files (#2512).**
A rendered mock set for an HTML driver (`web-playwright` and any future
HTML-based driver — gated on the mock glob ending `.html`, not the driver
name) is a pile of disconnected files: `reports-grid.html`,
`reports-chart.html`, and so on, each self-contained and each only
reachable if you already know its name. This is deliberately **not** the
"clickable mocks" question (deferred — `docs/CUSTOMER_PORTAL.md:152-157`
still holds: mocks stay self-contained static HTML, no build step, no
framework, no live data). It is pure navigation: a plain `<ul>` of
`<a href>` links, one per mock file, labelled from that mock's own
`<title>` tag.

This index is generated by a **deterministic script**
(`scripts/gen_mock_index.py`), not hand-authored by the mock-author LLM —
the same "run a provided script, don't freehand it" posture this repo
already uses for sealed-suite tooling, so every milestone's mock set gets
the identical glue page and nothing drifts by taste between renders.
`coord/mock_author.py`'s seed briefings (`build_mock_author_briefing` for a
fresh render, `build_mock_author_amend_briefing` for `--amend`) tell the
mock-author worker to run it as the last step before committing, whenever
the resolved driver's mock glob is HTML. Because it is a plain `.html` file
under `mocks/`, it needs no special-casing anywhere downstream:
`collect_mock_bundle_files` (PDR-3, #2508) already picks up every
`mocks/*.html` file when reading a merged Gate-A branch back into a design
round, so `index.html` rides along into the portal bundle for free. It is
also what the local-view TUI action (#2501) should open instead of a single
mock file, once that lands.

## Sealed amendment as data (#4178/#4179)

A sealed slice is sometimes *wrong*, not just not-yet-fixed — a deletion a
work issue makes breaks a sealed call site or struct literal
(quadraui#2876's shape: 7 sealed call sites and 2 struct literals broke
when a `Backend` method group was removed; the sealed suite itself needed
new fields it had never heard of). The verifier (#4176) catches this
*ahead* of dispatch by grepping removed/changed symbols against
`AcceptanceConfig.sealed_paths()`, but catching it isn't enough — fixing it
means **editing the sealed suite**, and no worker, test-author included,
may ever freehand that edit; "writing under `sealed_paths` is the job" only
covers a test-author's own JIT authoring, not an on-the-fly patch nobody
reviewed. The amendment flows as **data**, not as a model's own edit,
through four steps:

1. **Propose.** A worker or planner session that hits a sealed break never
   edits the sealed path itself. It emits a `SealedAmendmentArtifact` (a
   plain unified diff + the paths it touches + a rationale) via
   `coord acceptance propose-amendment`.
2. **Verify.** `coord acceptance verify-amendment` applies that diff in a
   throwaway scratch copy — never the real checkout, never the sealed
   suite's actual source — and re-runs the sealed suite there, reporting a
   structured pass/fail. Anyone can run this; it mutates nothing durable.
3. **Approve.** A **human**, never a model, runs
   `coord acceptance approve-amendment`. It re-runs verify-amendment fresh
   (an approval must never rest on a stale verdict) and, only if still
   green, stamps the artifact with who approved it, when, and a content
   fingerprint binding the approval to those exact bytes — editing the
   diff, rationale, or touched paths after approval invalidates the stamp.
4. **Apply.** A `test-author` leg — the one assignment type whose job
   already includes writing under `sealed_paths` — runs
   `coord acceptance apply-amendment` to apply exactly the approved bytes
   to its own checkout, as its own commit (never a closing keyword for the
   issue the amendment unblocks — `Refs #N`, so GitHub never auto-closes
   work that isn't actually done), then re-runs the sealed suite there.

A red suite after step 4's apply is evidence about the *issue the
amendment unblocks* (quadraui#2876's own aftermath: 7 sites repainted, but
4 structural-parity tests still failed because the contract path had
dropped a `register_zone` call — a real library regression the amendment
exposed, not a defect in the amendment) — a **finding**, reported as such,
never grounds to edit the amendment or the sealed suite further to make it
pass. The approved bytes land regardless; the red result routes back to
the work the amendment was unblocking.

## A sealed slice is red by design — the `expected_red` registry (#2164)

A sealed acceptance slice is **red by design** the moment it's authored — its fix doesn't exist yet.
Once its repo's CI runs the acceptance target as part of the *ordinary* test command (exactly what
#1950 requires, so a closed milestone's suite doesn't rot unnoticed), that redness fails CI, and
`coord merge` refuses to merge on a failed check. But the slice **must** land on the default branch
before the fix can dispatch — `issue_oracle_ready` (#1138) reads the manifest from there. Three
constraints, and only two are simultaneously satisfiable:

1. **Ordering** — the slice merges to the default branch *before* Work dispatches for its issue.
2. **Redness** — the slice fails before the fix exists, or it's a vacuous assertion (#1965's thesis).
3. **Cadence** — the suite runs in the ordinary test command every time, or it silently rots (#1950).

The fix relaxes **(3), narrowly**: keep running the suite on every push, but teach the runner which
failures are *expected right now*. Each manifest may carry an `expected_red:` block alongside its
`tests:` mapping:

```yaml
tests:
  ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns: 554

expected_red:
  554:
    - ms11_554_wide_tab_labels::wide_label_paints_every_glyph_in_its_own_columns
    - ms11_554_wide_tab_labels::measured_tab_budget_matches_the_painted_width
    # ascii_label_is_unchanged is deliberately absent — it is the control and must be green now
```

- **`coord acceptance run --all --ci`** is the CI-facing wrapper — point your repo's ordinary
  acceptance-test CI step at it instead of the raw driver command (`cargo test --test acceptance`,
  `npm run test:acceptance`, …). A test-id listed in `expected_red` that **fails** does not fail the
  run (`ci_green` stays true) — that's the designed-in redness. A test-id listed in `expected_red`
  that **passes** is the opposite signal: a hard, loud failure (`ci_green` goes false, with a
  distinct "HARD FAILURE" message naming the id) — the vacuous-assertion case #1965 cares about,
  caught mechanically instead of needing a human to eyeball it.
- **The clear happens after the fix's own PR actually merges — never at `record` time.** The first
  cut of this feature cleared `expected_red` straight out of `coord acceptance record`, the moment
  it observed green. A review caught why that's wrong: `record` runs at Phase-1 step 6, and Test
  (step 7), Review (step 8) and the actual Merge (step 9) all happen *after* it — real wall-clock
  time in a fleet running many issues concurrently. Clearing at step 6 can land on the default
  branch before the fix that earned it, so an unrelated PR's ordinary CI run (the very `--ci`
  wrapper above) executes the sealed suite against code that's *still broken*, no longer covered by
  `expected_red` — a HARD FAILURE indistinguishable from #1965's real vacuous-assertion signal,
  reddening the default branch for a reason that has nothing to do with whatever triggered that CI
  run. That's constraint (1) (Ordering) broken by the fix meant to satisfy it.

  The corrected sequence: `coord.merge_queue.process` (`coord merge`) calls
  `coord.acceptance.clear_expected_red_via_pr` **right after `gh_ops.merge_pr` succeeds** for the
  fix's own `type="work"` entry — i.e. only once the merge that's the whole point has actually
  happened — and only when the board's recorded `acceptance_state` is `"passed"` **at the exact SHA
  that just merged** (a stale record, e.g. new commits pushed after the last `record`, is skipped
  with a warning rather than cleared on faith).
- **The clear goes through a real PR, never a raw push.** The first cut pushed the clearing commit
  straight to `origin/{default_branch}` — this repo's own CLAUDE.md notes that `main` requires
  passing status checks, so a plain `git push origin main` is rejected even for admins, and any repo
  with equivalent branch protection would reject that push every time, silently leaving entries
  stuck forever (a warning that "record" still reported as overall success). `clear_expected_red_via_pr`
  instead opens a small PR (`github_ops.create_pr`) and merges it the normal way (`github_ops.merge_pr`)
  — the same protected path every other change to the default branch takes. It's also pure GitHub-API
  (no local checkout): `coord merge` is a `gh`-only wire layer with no guaranteed local clone, so the
  whole sweep — enumerating `tests/acceptance/ms-*/`, reading/editing the manifest, opening/merging
  the PR — goes through the Contents/PRs API alone.
- **This is an observation, made by the coordinator, never a worker edit** — sealing is unchanged; no
  worker ever touches `tests/acceptance/**`, and the fix's own branch carries the sealed manifest
  untouched throughout.
- **Visibility (acceptance criterion 4):** `coord acceptance expected-red <repo>` lists every live
  entry — see the subcommand table above. Since clearing is now best-effort and can legitimately sit
  pending (a required check not yet green on the clearing PR) or fail outright, this is how an
  operator confirms an entry isn't stuck rather than just pending its next retry.
- **Worker-scoped runs are unaffected.** `coord acceptance run --issue N` (the worker's own in-session
  loop) never applies `expected_red` — that command's whole point is converging *those exact tests*
  to green; suppressing their redness there would defeat the loop. `--ci` is refused without `--all`
  for the same reason.

Alternatives rejected: `#[ignore]`-style skip annotations require editing the sealed suite after the
fix lands (a sealing violation traded for the deadlock); a separate non-blocking CI job trains
everyone to ignore it (the Phase 0 disease) unless paired with exactly this registry anyway; dropping
the acceptance target from CI reopens #1950; landing the slice and the fix in one PR destroys the
independence the whole oracle loop depends on; clearing at `record` time (the first cut of this
feature) reopens exactly the "red default branch" failure #2164 exists to prevent, just relocated
in time — see above.

## The worker briefing contract

Injected at the top of every Work briefing in an oracle-loop milestone:

- Implement to **`tests/acceptance/ms-NN/contract.md`** (the black-box surface).
- You **may not** edit `tests/acceptance/**`. It is the sealed oracle. Touching it fails the gate.
- Run **`coord acceptance run --issue N`** to check yourself; iterate until your slice is green.
- Write your **own unit / internal tests** (that is still your job).
- If your slice will not converge — the failing set churns rather than shrinks across **2** rounds —
  **stop** and run `coord acceptance stall …` with what you tried and the stuck set. Do **not** grind.

## The stall protocol + convergence detector (#846)

The in-session loop still needs a bound — cheaper thrash is still thrash. Two triggers, one exit:

- **Worker self-report (preferred):** the worker watches its own failing-*set* trajectory. Shrinking
  = converging; churning (same count, different members) = whack-a-mole. On churn ≥2 rounds it calls
  `coord acceptance stall …`.
- **Coordinator backstop (headless):** the daemon watches the `acceptance record` history for a SHA
  series; if the external failing set churns across ≥2 runs, it raises `needs-attention` itself.

`needs-attention` is the **escape-hatch trigger**, not a separate epic — it is the stall-exit of the
loop. Headless auto-re-dispatches (or parks for the operator); interactive surfaces to the operator,
who already has the branch. This is all #846 needs to be; the WIP-snapshot handoff (#847) and
takeover-relaunch (#849) are conveniences, not prerequisites.

## Order of operations

Legend: **[indep]** zero-worker-context agent · **[worker]** implementer · **[coord]** external ·
**[human]** you.

**Phase 0 — Milestone kickoff (Gate A), once:**
1. **[indep] `coord acceptance mock`** renders the viewable mock; **[human]** reacts and signs off
   (UX discovery, against a cheap mock). The approved mock + `contract.md` is the pinned surface.
1b. **[human] `coord gate-a --approved <repo> <tracking_issue>`** records that sign-off on the
   board. **Merging the Gate-A PR is not sign-off** (#2063). See "Gate A sign-off is enforced"
   below — nothing dispatches against an unapproved contract.
2. **[indep] `coord acceptance author`** writes the red suite from the contract. Gate A **blocks
   issue dispatch until the contract exists** *and* **carries a recorded human verdict.**

### Gate A sign-off is enforced (#2063)

Gate A used to have two halves, only one of which was a gate: *does `contract.md` exist* (checked
by `coord.milestone_dispatch.gate_a_status`, #930) and *has a human read it* — which was the
convention "merging that PR is what satisfies Gate A". Anything that can merge a PR satisfied it,
including a coordinator session, silently, on CI green. It failed on two consecutive coord-portal
milestones (ms-1/PR #18, ms-2/PR #35): the mocks merged unseen, and by the time the operator asked
the second time an independent `test-author` had already authored a sealed slice against a contract
nobody had approved.

| | Verdict | Enforced |
|---|---|---|
| **Test** | `coord test --passed \| --fail` | yes — review held until a verdict (`PipelineConfig.test_precedes_review`) |
| **Gate A** | `coord gate-a --approved \| --changes` | **yes** — `issue_oracle_ready` refuses Work dispatch without one |

- **Where it is enforced: at the consumer, not at the merge.** The Gate-A PR is merged with
  `gh pr merge`, outside coord entirely, so no coord-side check ever sees it. The refusal lives in
  `coord.milestone_dispatch.issue_oracle_ready` (#1138) — the same guard that already refuses when
  `contract.md` is absent. An unapproved contract merging is therefore harmless: nothing is
  authored and no work dispatches until a verdict exists.
- **The verdict is keyed to the contract's content hash.** `coord acceptance mock --amend`
  changes `contract.md`, which invalidates a prior approval automatically (state `stale`).
  Approving v1 must not silently approve v2 — that is the same failure this gate exists to
  prevent, one level up. Whitespace/line-ending-only differences do not invalidate it.
- **`--changes` is a recorded rejection**, not merely the absence of an approval: the downstream
  refusal quotes your `--note` and points at `coord acceptance mock ... --amend` instead of
  saying "nobody has looked yet".
- **It refuses, it does not kill the queue.** A `coord drive-queue` entry that hits this refusal
  **parks** (re-checked every tick, #1891/#1892) rather than landing in terminal `blocked`, which
  nothing re-evaluates and `coord drive-queue add` cannot clear (#2040). Record the verdict and
  the entry resumes on the next tick with no queue surgery.
- **Opt-out is per-milestone, declared, and reviewable** — same posture as `oracle:exempt`. In
  `tests/acceptance/ms-NN/manifest.yml`:

  ```yaml
  gate_a:
    exempt: true
    reason: no user-visible surface — this milestone is a storage migration
  ```

  Note the issue-level `exempt:` list does **not** bypass this gate: it says "this *issue* doesn't
  consume the sealed suite", which says nothing about whether a human read the contract every
  sibling issue is built against.

Read the current state at any time with `coord gate-a <repo> <tracking_issue>` (no flag).

**Phase 1 — per issue:**
3. **[coord]** Dispatch Work with the briefing contract above (issue + #603 digest + contract slice +
   the sealed `coord acceptance run --issue N` command).
   - **`coord drive` does steps 2–3 as one move ("oracle drive", #1453).** Seeing a merged Gate-A
     contract and a configured `acceptance.drivers` entry, it authors this issue's JIT slice and
     then **lands it** — Test and Review are dispatched by the daemon's own passive tick, and the
     merge is `coord drive`'s own bounded `coord merge --only <slice aid>` — before it will
     `coord assign` anything. It has to: `issue_oracle_ready` (#1138) reads the manifest from the
     **default branch**, so until the slice PR merges the Work dispatch stays refused.
   - **Why the driver merges it rather than waiting** (#2079): the daemon's drain step,
     `_auto_drain_tick`, is gated on `merge.auto_drain`, which is `false` — see
     [`MERGE_AUTO_DRAIN_TRUST_BAR.md`](MERGE_AUTO_DRAIN_TRUST_BAR.md). Nothing else merges a READY
     entry. Before this, oracle-mode drives waited on that drain and burned `2 × --deadline` per
     issue before landing in a terminal `blocked` state.
   - If the slice cannot land — its Test failed, its review requested changes, or `--no-merge` is
     set — the drive **stops immediately and names the command**, rather than idling to the
     deadline. `--no-acceptance` opts the run out of the whole path.
4. **[worker]** Implements; writes unit tests (tier 1).
5. **[worker] in-session oracle loop:** `run --issue N` → fix → repeat (warm, no reset) →
   **converge → push + done**, or **stall → `acceptance stall` → WIP snapshot** (§ above).
   - *Headless:* whole loop in one `claude -p`, **zero human interaction.**
   - *Interactive:* identical; **[human]** present, nudges/ends on stall. Tightness comes from the
     agent self-running the oracle.
6. **[coord] Acceptance box (trust gate):** `acceptance record --issue N --sha <pushed>` — the
   coordinator re-runs the sealed slice externally. **ToS-clean: git + a test run, never the TTY.**
   Green → advance; red → bounce to Fix with the external failure.
7. **[coord] Test box:** the repo's full normal suite (regressions outside the slice) on a
   capability-matched machine.
8. **[indep] Review:** adversarial, zero shared context. Approve → advance; request-changes → Fix.
9. **[coord] Merge box:** rebase → if the artifact changed non-trivially, **re-run `acceptance
   record` + Test + Review on the rebased SHA** → merge into `feature/ms-NN` (Pipeline v2 keystone).

**Phase 2 — milestone close:**
10. **[coord] Gate C:** `acceptance run --all` green on `feature/ms-NN` (integration gaps *between*
    issues). **[indep] Gate B:** built-to-spec review. **Gate D:** ship → `develop`, gated on B + C.
11. **[human] UAT:** a **confirmation**, not a discovery. A residual failure → amend the contract
    (feeds Gate A) + file an issue. That delta measures how far below 100% the oracle sits.

## TUI menu surface (coord-tui)

Phase 0 steps 1–2 and Phase 1 step 6 above are also reachable by right-clicking a Pipeline row in
`coord-tui` — no CLI required. Implemented in `tui/src/app/{dialogs,pipeline}.rs` (#1059/#1060).

**On the epic/tracking-issue row** (the row carrying the `epic` label — gated on
`all_labels.contains("epic")`):
- **"Dispatch Gate A mock"** 🎭 → `coord acceptance mock <repo> <tracking_issue>` — headless (a
  `type=mock-author` `claude -p` worker, dispatched the same way `coord assign` dispatches Work,
  not a live session). The CLI's own claim-detection refuses a duplicate dispatch while one is
  already in flight. This *is* Phase 0 step 1.
- **"View Gate A mock (PR)"** 👁 → opens that worker's PR in the browser so a human can read
  `contract.md` and review/merge the branch. Disabled until a PR exists. **This does not show the
  rendered mock(s)** (#2501) — GitHub's "Files changed" view renders `.html` as a source diff,
  never a live page; use "View Gate A mock (local)" below for that. **Merging that PR does not
  satisfy Gate A** (#2063) — it is only how you review/merge what you are signing off on.
- **"View Gate A mock (local)"** 👁 → the actual mock viewer (#2501). Fast-forward-pulls the local
  checkout's default branch (never forcing, never touching a dirty or diverged tree — aborts with
  a toast instead, same as everywhere else this repo touches git destructively) and then opens
  `tests/acceptance/ms-NN/mocks/` from disk in the OS's default browser, where `.html` actually
  renders. Disabled until that mocks directory exists locally. Opens `mocks/index.html` once #2512
  ships it; falls back to opening the mocks directory itself until then.
- **"Approve Gate A"** ✓ → `coord gate-a --approved <repo> <tracking_issue>` — records the verdict
  that actually satisfies Gate A. Sits directly beside the 👁 on purpose: reviewing and recording
  are one gesture.
- **"Request Gate A changes"** ✎ → opens a "What needs to change?" text-input prompt first
  (`pending_gate_a_changes_note`, #2500); Submit fires `coord gate-a --changes <repo>
  <tracking_issue> --note "<what you typed>"` (blank is fine — falls back to no `--note`), Esc/
  Cancel aborts the whole dispatch. Records a rejection; dispatch stays refused. The note is a
  record of intent, not the fix itself — still amend the contract with
  `coord acceptance mock <repo> <tracking_issue> --amend "<what to change>"`.

**On any ordinary member-issue row of a milestone's `## Work order`** (resolved via
`milestone_tracking_issue_for`; **not** epic-gated — applies per-issue, independent of the
row's lifecycle):
- **"Author acceptance tests"** → `coord acceptance author <repo> <tracking_issue> --issue <N>` —
  the JIT test-author slice for that one issue (Phase 0 step 2, run per-issue rather than once
  up front). Disabled until `tests/acceptance/ms-NN/contract.md` exists on the local checkout —
  i.e. until Gate A's mock PR above is merged.
- **"Record acceptance"** → `coord acceptance record --repo <repo> --issue <N> --sha <sha>` — the
  external trust-gate re-run (Phase 1 step 6). Disabled until there's a completed (`done`)
  `work`/`mock-author` assignment with a branch to resolve a SHA from; the SHA itself is read
  directly off the local checkout's git refs, so the operator never types a commit hash by hand.

The in-session worker loop (Phase 1 steps 4–5, `coord acceptance run --issue N`) has **no** menu
item — it runs inside the worker's own session, not from the operator's TUI.

**Follow-on, blocked — do not treat as ready:** once the reusable `?` cheatsheet/command-palette
help layer lands (quadraui #431 → TUI CC-4 #1124, milestone #38 "Plans panel → rich client"), the
Pipeline panel's help overlay should surface this same right-click sequence so it's discoverable
without reading `tui/src/app/{dialogs,pipeline}.rs`. This is *not* wired as an `after:` DAG edge on
any milestone (cross-milestone edges are rejected by this repo's convention) — it's a plain
prose follow-on, to be picked up only once #1124 has actually shipped.

## ToS posture — "done" is observed as "green," never scraped

The completion signal is **not** "the session ended" or "the model said done" — it is **"the oracle
passes on the pushed SHA,"** observed by the coordinator running the sealed suite itself (git + a
test run). This is ToS-clean in **both** modes because it never reads the terminal. Headless can also
auto-re-dispatch because `claude -p` is the sanctioned automation path (its stdout is program output,
not a scraped TTY). The **one** thing forbidden ToS-clean is auto-continuing a *stalled interactive*
session (needs a human keystroke) — which is exactly where the human belongs anyway.

## Dogfooding + limits

- **Dogfood target: coord-tui the app** — `TuiDriver` + `make_test_app(BoardData)` are real; the
  `.screen` mock == assertion property holds. Rollout does **not** block on quadraui maturing.
- **Excluded: quadraui the library** (it is the framework, still evolving) and **raw-terminal/PTY
  behavior** (out of `TestBackend` reach; thin live smoke, quadraui #302).
- Most consumers will be web / Electron / native — the driver table above is how they plug in; the
  coord-tui slice proves the machinery on the cleanest medium first.

## Build slice — phased, mapped to existing issues

**Slice 1 — the runner + one driver (unblocks everything):**
- `coord acceptance` command skeleton (`run` / `record` / `run --all`) + the `acceptance.drivers`
  config + the `tui-tuidriver` adapter (wraps `cargo test --test acceptance`, parses structured
  verdicts). Sealing v1 (`files_forbidden` + tamper detection). → *new issue; the plumbing #931/#932
  both assume.*
- Worker briefing contract injection for oracle-loop milestones. → *new issue (agent.py; needs a
  release + `coord agent update`).*

**Slice 2 — the in-session loop + trust gate (the tight loop):**
- Wire `coord acceptance run --issue N` for **in-session** worker use + the external
  `acceptance record` **Acceptance box** on the board. → *refits **#932** (adds the in-session half;
  #932 today is post-work only).*
- Deliver the suite **sealed** to the worker; author it framework-driver-aware. → *refits **#931**.*
- Stall protocol (`acceptance stall`) + coord-side churn detection → `needs-attention`. → *refits
  **#846** as the stall-exit of the loop.*

**Slice 3 — mock-first Gate A + amendable contract (kills waterfall):**
- `coord acceptance mock` (mock-author) + `contract.md` at `tests/acceptance/ms-NN/` + amend flow. →
  *refits **#930** (spec-first → mock-first) and the contract-storage open question.*

**Slice 4 — milestone close:**
- Gate C (`run --all`) + Gate B (built-to-spec) + Gate D (ship). → *refits **#932** (Gate C) + #933.*

**Interaction with per-stage mode (#686):** the in-session oracle loop **is** the headless
"zero-touch" Test/Work mode; interactive is the low-touch supervised variant. #686's per-issue
test-mode policy chooses between them.

## Dispatch order (milestone #25)

The slices resolve to one dependency DAG, encoded as the `## Work order` block in the milestone's
**epic tracking issue #947** (machine-readable by `coord milestone order` / `coord milestone
dispatch`, #768/#769 — `coord milestone dispatch` drains this frontier in order):

```
#944 ─┬─► #932 ─┐
      │         ├─► #945 ─┬─► #931
      └─► #846 ─┘         └─► #930
```

| Step | Issue | After | Runtime |
|---|---|---|---|
| 1 | **#944** runner + `tui-tuidriver` + sealing v1 + the `oracle_loop` milestone marker | — | coord-live |
| 2 | **#932** in-session run + external trust gate (Acceptance box) + Gate C | #944 | coord-live |
| 2 | **#846** stall protocol (`coord acceptance stall`) + churn detector | #944 | coord-live |
| 3 | **#945** worker briefing-contract injection | #944, #846 | **needs release** |
| ▶ | **Dogfood checkpoint** — hand-write a `contract.md` + one red acceptance slice for a small coord-tui issue, run *one* issue through the loop before automating authoring/Gate A | | |
| 4 | **#931** independent sealed authoring (`type=test-author`) | #945, #932 | **needs release** |
| 4 | **#930** mock-first Gate A + amendable contract | #945, #932 | **needs release** |

#932 ∥ #846 run concurrently (both only need #944); #931 ∥ #930 run concurrently after the plumbing.
The DAG **gates #931/#930 behind #945 + #932** so authoring + mock automation only begin once the loop
is dogfoodable — the checkpoint is enforced, not just advisory. **"needs release"** = touches
`agent.py` / worker prompts, so it reaches agents only after a PyPI release + `coord agent update`;
#944/#932/#846 are live from the editable install immediately.

**Standalone:** this milestone runs on the *current* flat pipeline — it does **not** depend on
Pipeline v2's Observability (#925–927) or Merge-bounce (#915) phases. Only **Gate C** rides along
(in #932); Gate B/D + the `develop` git model (#933/#934) stay deferred.

## Open questions

- Contract storage: settled here as checked-in `tests/acceptance/ms-NN/contract.md` (closes the
  #930/PIPELINE_V2 open question). Revisit if milestones need cross-repo contracts.
- Sealing v2 (worktree-absent, daemon-held suite) — deferred until v1 tamper-detection proves
  insufficient.
- Web/native driver adapters — specced, unbuilt; land after the coord-tui slice validates the shape.
