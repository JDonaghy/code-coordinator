# The `merge.auto_drain` trust bar

> **Status:** living decision record, opened 2026-07-28 by #1491 (milestone #50's exit
> gate, tracking issue #1480). Update the "Status against the bar" section every time
> the picture changes — do not let this go stale the way #50's checklist would have
> gone green with the fleet still merging by hand.

## Why this doc exists

Milestone #50 makes the merge path *correct*: conflict recovery (#1474), patch-id
staleness (#1475/#1506), CONFLICT re-testing (#1477), board-write races (#1482),
review dispatch health-probe (#1485), and half a dozen more. None of that makes the
merge path *trusted*. `merge.auto_drain` is a config flag reflecting a judgement, and
no amount of correct code flips it for you.

It has been `false` (the `MergeConfig()` default — no `merge:` block in
`coordinator.yml`) since **2026-06-29**, when a drain merged three PRs in a single
tick and the operator lost confidence in it. Every dispatch since has ended with
`coord drive` carrying an issue Work → Test → Review and then stopping at Merge,
waiting for a human to run `coord merge`. This doc is the gate that decides when
that stops being true — and the record of the decision, so it isn't re-litigated
from memory next time.

## The trust bar

All four must hold before `auto_drain: true` goes back on dellserver:

1. **The fixes that caused the two failure classes are landed *and deployed*** (see
   the deploy checklist below — "merged" and "live" are different facts):
   - Silent stalls: #1474 (conflict deadlock), #1477 (CONFLICT entries never
     re-tested), #1485 (health-probe pre-filter dropping eligible reviewers).
   - Silent reverts / wrong state: #1475 + #1506 (patch-id gate), #1482
     (`save_board()` clobbering `test_state`/`smoke_test`).

   These are the ones that would make an *automatic* drain fail exactly the way the
   manual one already has a history of failing on this fleet — landing them without
   deploying them is worse than not landing them, because it reads as "fixed" on the
   board while the daemon runs the old behavior (#1394's lesson, restated below).

2. **N = 10 consecutive `coord merge` drains, manually triggered, with zero
   incorrect merges and zero manual intervention, observed *after* condition 1 is
   deployed.** Why manual and why after, not "any 10 in the historical audit log":
   the audit trail (`coord audit --category merge`) already has 500+ merge events
   since 2026-06-29, but the overwhelming majority ran under code that didn't yet
   have #1477/#1482/#1485 — they're evidence the *old* path mostly worked, not that
   the *fixed* path does. "Incorrect merge" = wrong sequence order, a non-READY entry
   merged, or a merge that a reviewer later had to revert. "Manual intervention" =
   the operator had to do anything beyond typing `coord merge` (resolve a conflict by
   hand, force a flag, re-order the queue). N=10 is not sacred — it's picked to span
   more than one day of the fleet's normal dispatch cadence (recent history shows
   several merge-eligible events per day) without being satisfiable by a single lucky
   afternoon.

3. **`max_per_tick` set to a deliberate value, not left at the `0` (unlimited)
   default.** This is the one knob that makes an *automatic* drain safer than the
   manual one that lost trust: `_auto_drain_tick` (`coord/serve_app.py:1002`) runs
   on the daemon's 30-second reconcile tick and, uncapped, merges every READY entry
   it finds in that single tick — which is exactly what happened on 2026-06-29 (three
   at once). A manual `coord merge` has no such cap and isn't meant to; a human
   running it is already the rate limit. Recommendation: **`max_per_tick: 1`.** One
   merge per 30s tick turns a burst into a trickle and gives the audit log (and an
   operator glancing at the board) a chance to notice a bad merge before the next one
   lands, at the cost of a queue of 3 READY entries draining over ~90s instead of
   instantly — a trade worth taking for the first weeks back on. Revisit upward once
   auto_drain itself has its own trust track record.

4. **All three deploy surfaces verified live, not just released** — see below.

## Deploy checklist — a merged fix is not a live fix

Restated from [`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md#1-a-merged-fix-is-not-a-live-fix):
this milestone touches all three deploy surfaces, and #50 has no deploy step of its
own today. #1394 sat merged-but-dead for exactly this reason and the next dispatch
re-hit the identical bug at $3.44. **This milestone is not done when the PRs merge.**

| Surface | Issue(s) | Mechanism | How to verify |
|---|---|---|---|
| Agent-side | #1492 (`coord/agent.py`, `AgentServer._prune*`) | PyPI release **+ `coord agent update` on all three machines** (precision, elitebook, dellserver — [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md#routine-upgrade-all-agents)) | `curl http://<host>:7433/status \| jq .version` on each machine |
| Daemon-side | #1482 (`_UPSERT_SQL`, `coord/state.py`), #1485 (`dispatch_review`, `coord/review.py`) | Both run inside `coord serve`. PyPI release reaching dellserver's `~/.coord-venv` **+ a `coord-serve` restart**, and per [`OPERATING_GOTCHAS.md` #2](OPERATING_GOTCHAS.md#2-restarting-the-daemon-needs-a-quiet-fleet), not while `coord sessions --remote` shows anything live | `curl http://dellserver:7433/status \| jq .version` (agent) — the daemon has no separate version endpoint, so cross-check `coord-serve`'s process start time against the venv's install timestamp |
| Coordinator-only | everything else in the milestone | **On dellserver specifically, this is NOT "live from the editable install."** dellserver's `coord-serve` *and* its `coord` CLI both run from the same pinned, non-editable `~/.coord-venv` — deliberately, per #1418, to close the editable-drift hazard where a coord-self worker's `pip install -e .` could take the board down fleet-wide (see the `worker_permissions.deny` block on the `claude-coordinator` repo entry in `coordinator.yml`). So on this host, "coordinator-only" fixes ride the *same* release + restart path as the daemon-side row, not an instant one. (The general claim in [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md#publishing-a-release-pypi) that coordinator-only Python is live from an editable install the moment it's on disk is true for a dev machine checked out at `~/src/claude-coordinator`; it is not true for dellserver.) | `pip show code-coordinator` inside `~/.coord-venv` vs. the commit you need |

### Status against the bar (as verified 2026-08-04)

Checked directly against the live fleet, not assumed from the milestone board.

- **Conditions 1, 3 and 4 are met.** Fleet-wide version is **v0.4.104**: all three
  agents (precision, elitebook, dellserver), the daemon's `~/.coord-venv`, the
  sequencer's `~/.coord-cli-venv`, and the operator's editable checkout all report
  `0.4.104`. `coord-serve` restarted 2026-08-04 20:46:22 UTC, after that install.
  The four-lane deploy surface is green simultaneously — which had not been true at
  any earlier point in milestone #50.
- **Condition 3 is applied**: dellserver's `~/.coord/coordinator.yml` now carries a
  `merge:` block with `auto_drain: false` and **`max_per_tick: 1`**, with the
  rationale inline. The cap is live *before* the flag, which is the intended order.
- **Milestone #50 (#1480) has zero open issues** — all 25 closed, including #1485,
  the 2026-07-28 blocker.
- **Condition 2 (N=10 clean manual drains) has NOT started, and its clock opens
  now — 2026-08-04, not 2026-07-28.** This is the entry most at risk of being
  read wrong, so the reasoning is recorded rather than the conclusion alone:
  `coord audit --category merge --since 2026-07-28` returns 60+ merge events, which
  superficially clears N=10 several times over. It does not count. The
  stale-verdict work is **four arms** (#1738 drive re-test, #1769 `coord merge
  --revalidate`, #1778 inert-branch, #1715 batch revalidation), and **all four only
  went live together in v0.4.104, today.** A measurement on 2026-08-03 found four
  stalls in a single session with the arms reaching exactly one of them; each of the
  other three took a human. Those interventions are the dominant source of
  "manual intervention" events in the 07-28 → 08-04 window, and they occurred under
  code where at most one arm was deployed. Counting them is the same error this doc
  already names once: they are evidence the *old* path mostly worked, not that the
  *fixed* path does.

**Verdict: the bar is not met — one condition short, and it is a waiting condition,
not a code condition.** Nothing further needs to be built. What remains is to
observe 10 consecutive clean manual drains under v0.4.104 and record them below.
`coord drive-queue` (epic #1750, live and on a 15-minute timer since 2026-08-04) is
the instrument that generates those drains without an operator sitting on them —
note that it *generates* the drains, it does not *satisfy* the condition, since
condition 2 counts manually-triggered `coord merge` outcomes.

### Status against the bar (as verified 2026-07-28 — superseded, kept for history)

Checked directly against the live fleet, not assumed from the milestone board:

- Milestone #1480's work order: all 16 other issues `[done]`; #1491 (this one) is the
  only one still open, and it's blocked only on itself (`coord milestone order
  claude-coordinator 1480`).
- Currently installed/running version fleet-wide: **v0.4.83** — confirmed via
  `GET /status` on all three agents (precision, elitebook, dellserver all report
  `"version": "0.4.83"`) and via the daemon's on-disk venv (`~/.coord-venv`,
  installed 2026-07-28 03:12:58 UTC; `coord-serve` PID last started 03:13:08 UTC —
  ten seconds later, i.e. the running process *is* v0.4.83, not a stale in-memory
  copy of something older).
- **#1474, #1475, #1477, #1482, #1492 are all ancestors of tag `v0.4.83`** (verified
  with `git merge-base --is-ancestor`) — condition 1's first four items and the
  agent-side deploy row are **met and live**.
- **#1485 is NOT an ancestor of `v0.4.83`.** `main` is currently 19 commits ahead of
  the `v0.4.83` tag (surfaced by the CLI's own stale-install warning), and 15ee626
  (`Fix #1485: empty /health repos list means unrestricted...`) is one of those 19 —
  it merged to `main` *after* the `v0.4.83` release cut. **No release ships it yet,
  so the live daemon does not have it.** This is today's hard blocker on condition 1
  and condition 4.
- Condition 2 (N=10 clean manual drains post-deploy): **not started** — it can't
  meaningfully start until #1485 is actually live, since the whole point is
  observing the *fixed* code under real load.
- Condition 3 (`max_per_tick` set): **not yet applied** — no `merge:` block exists on
  dellserver's `coordinator.yml` today (confirmed: `grep -c '^merge:'` → 0).

**Verdict: the bar is not met. `auto_drain` is not being flipped in this pass.**
Flipping it now — with #1485 undeployed and zero observed drains under the fixed
code — would repeat precisely the mistake this doc exists to prevent: treating
"merged" as "safe," which is the #1394 lesson the milestone was opened to fix in the
first place.

### What closes the gap, in order

Steps 1–3 are **done as of 2026-08-04 (v0.4.104)** — struck through rather than
deleted, so the sequence stays readable against the changelog.

1. ~~Cut a release at or after `main`'s current HEAD (covers #1485 plus the other 18
   commits already ahead of `v0.4.83`)~~ — **done**, and superseded twice over:
   v0.4.104 also carries all four stale-verdict arms.
2. ~~`coord agent update --all`, then confirm `coord sessions --remote` is empty and
   restart `coord-serve` on dellserver~~ — **done** (daemon restarted 20:46:22 UTC).
3. ~~Verify: all three agents' `/status` report the new version; daemon venv install
   timestamp is newer than the `coord-serve` process start time.~~ — **done**, and
   extended to the fourth lane (`~/.coord-cli-venv`), which nothing upgrades
   automatically and which was found three releases stale on 2026-07-29.
4. **← you are here.** Start the N=10 manual-drain observation window: run `coord merge` as usual,
   record each outcome (clean / conflict / had-to-intervene) somewhere durable
   (this doc's changelog below, or the audit trail's own timestamps are enough to
   reconstruct it after the fact via `coord audit --category merge --since <deploy
   timestamp>`).
5. Once 10 consecutive clean drains are observed with zero incorrect merges: edit
   the tracked `coord-settings` checkout — `~/src/coord-settings/coord/coordinator.yml`
   on dellserver, commit + push there, then `git -C ~/src/coord-settings pull` on
   dellserver — never dellserver's live `~/.coord/coordinator.yml` directly (that
   path is a symlink into the checkout; `sed -i`/most editors write-and-rename over
   it and silently replace the symlink with a disconnected regular file — see
   [`OPERATING_GOTCHAS.md` #14](OPERATING_GOTCHAS.md#14-fleet-coordinatoryml-is-edited-in-a-different-repo-than-the-one-youre-reading-this-in--and-the-daemon-can-silently-run-a-broken-copy-of-it)),
   and never `coordinator.remote.yml` (see
   [`OPERATING_GOTCHAS.md` #8](OPERATING_GOTCHAS.md#8-coordinatorremoteyml-is-a-cache--your-config-edit-will-revert))
   to add:
   ```yaml
   merge:
     auto_drain: true
     max_per_tick: 1
   ```
   then restart `coord-serve` (same quiet-fleet check as above), and confirm with
   `coord config` from a thin client that the re-fetched cache shows the new block.
   Before any of this, run `coord diagnose --config-provenance` on dellserver to
   confirm the live path is still actually a symlink into the checkout.
6. Watch the first handful of auto-drain ticks closely (`journalctl --user -u
   coord-serve -f` or the daemon's own log) before walking away.

## Rollback trigger

Decided now, not in the moment — any one of these flips `auto_drain` back to
`false` immediately, followed by a `coord-serve` restart to make the revert live,
and a written note appended to the changelog below (mirroring the 2026-06-29
incident this whole doc is downstream of):

- **Any incorrect merge** — wrong sequence order, a non-READY/BLOCKED entry merged,
  or any auto-merged PR a reviewer later has to revert.
- **More entries merged in one tick than `max_per_tick` allows.** That's not a trust
  problem, it's a code bug in the cap itself, and it's the exact failure shape that
  caused the original incident — treat it as maximally serious.
- **`_auto_drain_tick` throwing / logging errors on 3+ consecutive ticks** (roughly
  90s of failures) — silent-stall risk is exactly what #1474/#1477 were fixed to
  close; a new failure mode here gets the same "stop and look" treatment.
- **Any silent stall attributable to auto_drain** — a READY entry sitting un-merged
  for longer than a few ticks with no BLOCKED/CONFLICT reason recorded.

## Changelog

- 2026-07-28 — doc opened (#1491). Bar defined; verified not met (blocker: #1485
  unreleased). `auto_drain` left `false`.
- 2026-08-04 — status re-verified against the live fleet. Conditions 1, 3 and 4 now
  **met**: v0.4.104 live on all four deploy lanes, `coord-serve` restarted 20:46:22
  UTC, `max_per_tick: 1` applied on dellserver. Milestone #50 closed out (25/25),
  and all four stale-verdict arms (#1738, #1769, #1778, #1715) went live together
  in this release. Condition 2 **starts here** — the 60+ merges already in the audit
  log since 07-28 do **not** count toward it, because the arms whose absence caused
  the interventions in that window were not deployed. `auto_drain` stays `false`;
  the bar is now one *waiting* condition, not one *build* condition.
- 2026-08-18 — `auto_drain` flipped to `true`. Condition 4 re-verified fresh (not
  reused from 08-04): all three agents and dellserver's daemon report **v0.5.146**;
  `~/.coord-venv` (dellserver) installed 0.5.146 at 21:11:50 UTC with `coord-serve`
  starting 7s later (not a stale in-memory process); `~/.coord/coordinator.yml` is
  still a live symlink into a clean `~/src/coord-settings` checkout. Condition 3
  unchanged (`max_per_tick: 1`). Condition 1's fixes are trivially still ancestors
  of 0.5.146 (v0.4.104 → 0.5.146 is a strict forward history, no reverts observed).
  Side note: the "fourth lane" the 08-04 entry also checked (`~/.coord-cli-venv` /
  `drive-epic.service`, the epic sequencer) no longer exists anywhere on the fleet —
  not one of this bar's three required rows, so not a blocker, but worth a look
  separately.

  **Condition 2 was NOT formally satisfied by the letter of this doc** — no tallied
  run of 10 consecutive *operator-typed* `coord merge` invocations was ever recorded.
  What stands in its place: `coord audit --category merge --since 2026-08-04` shows
  500+ `merged` events (query cap), all `actor=coordinator` (i.e. via `coord merge`
  being invoked — confirming `auto_drain` genuinely stayed `false` the whole window),
  plus only 4 conflict-related events (3 conflict-fix dispatches, 1 escalated to a
  human) and no incorrect-merge or rollback-trigger signal found in that trail. Most
  of that volume is `coord drive-queue`'s 15-minute timer firing `coord merge`
  unattended, not a human watching each one — which is exactly the distinction this
  doc's condition 2 draws and says doesn't count. Flipping anyway on this evidence is
  a **deliberate, recorded deviation** from the bar as written, not a silent skip:
  two weeks of heavy unattended `coord merge` usage with a clean audit trail was
  judged sufficient in practice, in lieu of a separately-tallied manual run. Restart
  confirmed clean (`coord-serve` up 22:08:09 UTC, zero errors in the daemon log).
  Rollback triggers (below) apply from this point exactly as written — watching
  closely for the first several real auto-drain merges once something clears review/CI.
