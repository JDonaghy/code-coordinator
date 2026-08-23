# The drive queue (`coord drive-queue`, #1750)

`coord drive <repo> <issue>` drives **one** issue end-to-end with nothing
watching. Nothing decided *what to drive next* — that used to mean either a
human typing the next `coord drive` by hand, or `scripts/drive-batch.sh`, a
bash loop that is durable for exactly one tmux session and gone the moment it
dies. `coord drive-queue` is the durable, board-backed replacement: declare
the order once (with pins and dependencies), and a periodic `tick` launches
at most one drive per run, first-eligible-wins, never past the concurrency
ceiling.

This is the operator runbook. For the implementation, see `coord/drive_queue.py`
(pure `plan_tick`) and `coord/commands/drive_queue.py` (the CLI/tick shell).

---

## Read this before queuing more than ~2 issues (#1715)

*Mirrored (with §4 below) as a skill: `coord/skills/drive-queue-preflight/SKILL.md` — keep both in sync.*

**A queue longer than about two issues is still not an *unattended* feature —
but clearing it is now one command and one suite run, not *N−1* by hand.**

Every merge on a repo invalidates every *other* queued branch's Test verdict on
that repo (the base moved). Queue *N* independent issues against the same repo
and merging the first one stales the other *N−1*. That is the cascade, and it
used to cost *N−1* human interventions — which defeats the entire point of an
unattended timer.

Note the "on that repo": the cascade is intra-repo, which is why the tick's
second capacity ceiling is **per repo** (§9) and defaults to 1. Cross-repo
entries in the same queue do not stale each other and are launched in parallel.

Three arms have since landed against it:

* **#1738** — `coord drive` re-dispatches the Test stage once, automatically,
  on a STALE (not missing) verdict. Only fires while a **live drive** is
  watching the issue. Measured 2026-08-03: reached 1 of 4 real stalls.
  **#2229** widened what it can see: the arm used to classify the gate from
  the board's `merge_reason` alone, which `merge_queue.plan()` leaves *empty*
  whenever its render-time freshness check has no live SHA data (the #1640
  door / the #1566 "plan says READY, `--only` refuses" split) — so on an
  already-enqueued entry the arm could not arm at all, and the drive spent
  three blind retries and died *printing the refusal it had captured*
  (quadraui#309: 11h blocked on a merge that then landed first try by hand).
  It now falls back to the last `coord merge --only` diagnostic the drive
  itself captured. Still only the STALE arm self-repairs — a *missing*
  verdict is the #1640 lost-write shape and escalates to a human unchanged —
  and, because that diagnostic is a snapshot rather than live state (#2149),
  each capture buys at most one re-test before a real attempt has to
  re-validate it.
* **#1769** — `coord merge --revalidate` re-tests a stale-but-`passed` entry
  against the current base from the *merge* lane, which is where a branch with
  no live drive actually sits.
* **#1715** — that flag **batches**. When several queued entries share a base
  they are composed onto it together and validated by **ONE** suite run, not
  one each. A four-branch group costs ~7 minutes, not ~26.

So the practical cost of a deep queue is now: let it run, then drain it with

```bash
coord merge --revalidate --dry-run   # names each batch and its members
coord merge --revalidate             # one composed suite run per base, then merge
```

**What has *not* changed: none of this is automatic.** `--revalidate` is
strictly opt-in and the unattended auto-drain passes `revalidate=False`
permanently — starting suite runs on a timer is the shape that was gated off
after the 2026-06-07 token-burn incident, and `merge.auto_drain` is `false` by
design. An overnight timer with no operator still parks stale entries; the
difference is that clearing them in the morning is one command and one suite
run rather than *N−1* worktrees by hand.

One honest caveat on the batch: a composed run validates the **composite**, not
each branch alone. Every member already carries its own `passed` verdict from an
earlier base, so the composite re-confirms they still hold *together* against
the current one — a re-confirmation, not a first proof. An entry that never had
a verdict, or that is blocked on review/CI/conflict, is never included. If the
composite fails, **nothing merges**; each branch is then re-tested alone so the
culprit is named and the innocent branches still go through.

Related, narrower version of the same class: **#1738** made the base-freshness
check a little smarter — a base move that only touches `docs/**`,
`scripts/**`, `.github/ISSUE_TEMPLATE/**`, or a top-level `*.md` file is
recognized as content-irrelevant and does **not** stale a green Test verdict.
Everything else still does, and `coord drive` is "biased hard toward staling"
by design (an unreadable diff or any file outside that allowlist keeps the
pre-#1738 behavior). `coord drive` now re-dispatches the Test stage once,
automatically, on a STALE (not missing) verdict, bounded by the same
`fix_rounds` budget as a real test failure — but that budget is shared with
genuine fixes, so a queue that keeps re-staling itself burns it fast and still
lands on the human escalation.

**A fourth, distinct staleness signal (#1851): a green *CI check* can itself be
stale.** GitHub re-runs `pull_request` workflows on head `synchronize` —
never on base movement — so a passing check only proves the composite passed
against the base *as of the last head push*, not as of now. Every merge on the
repo silently widens that gap for every other open PR. `coord merge --dry-run`
now names this ("CI stale: checks predate the current base…"), distinctly
from "CI failed"/"CI running", and `coord merge --revalidate` triggers a
`gh run rerun` for it — strictly cheaper than the local-suite arms above
(CI minutes, not a routed Test-stage agent), and skipped automatically when
the same #1847 disjointness check already proved the base move irrelevant.
Same posture as everything else on this page: reporting is unconditional,
the re-run is opt-in behind `--revalidate`, and auto-drain never triggers it.

**Practical guidance:** queue depth per repo is no longer bounded by the
*arithmetic* — it is bounded by whether anyone is around to type
`coord merge --revalidate` afterwards. For a genuinely unattended overnight
run, still prefer 1–2 entries per repo, or issues in *different* repos (a
merge in one repo does not stale verdicts in another). For a run you will come
back to, queue as deep as you like: the morning drain is one command.

---

## 1. Enqueue

CLI:

```bash
coord drive-queue add REPO ISSUE                        # append to the tail
coord drive-queue add REPO ISSUE --machine dellserver    # pin to one machine
coord drive-queue add REPO ISSUE --after 1750            # wait for #1750 first
coord drive-queue add REPO ISSUE --after 1750,other#42   # comma-separated, repeatable, cross-repo
coord drive-queue add REPO ISSUE --position 0            # insert at the head instead of appending
```

`--machine` pins the drive to one machine (default: let `coord drive` route
it). `--after` names pre-req issues that must land first — a bare number
resolves against `REPO`, `repo#issue` crosses repos; a self-edge or a
dependency cycle is rejected **before** the write, leaving the queue
untouched (the same posture `coord milestone write-order` takes). Re-running
`add` on an already-queued `REPO ISSUE` updates it in place.

```bash
coord drive-queue list             # the queue in run order, with state/attempts/deferrals
coord drive-queue status           # counts by state, plus the current queue-level alert
coord drive-queue remove REPO ISSUE
coord drive-queue move REPO ISSUE --to 0
```

TUI: right-click the status bar's `QUEUE: …` segment → **"Drive queue…"**
(shortcut `q`) opens the overlay — `j`/`k` to move the cursor, `J`/`K` to
reorder the selected entry, `x` to remove it, `u` to unblock a `blocked`
entry in place (the same remove-and-re-add the CLI's suggested fix performs,
without leaving the overlay). To add an issue from the Pipeline: right-click
its row → **"Add to drive queue"** → pick a machine (or "no preference").

## 2. Install the timer

Same host as `coord-serve`/`coord-web`/`coord-notify` — the daemon host that
owns `~/.coord/coord.db` (dellserver in production). The tick subprocess-
launches `coord drive --tmux`, which needs a local tmux server and the repo
checkouts under `SRC_ROOT`, so — like `drive-batch.sh` — it belongs where
those exist, not on a thin client.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/coord-drive-queue.service deploy/coord-drive-queue.timer \
    ~/.config/systemd/user/
loginctl enable-linger "$USER"          # survive logout / reboot
systemctl --user daemon-reload
systemctl --user enable --now coord-drive-queue.timer
```

This is a `Type=oneshot` service activated **by the timer** — do not
`systemctl --user enable coord-drive-queue.service` directly (it has no
`[Install]` section). Verify one tick runs clean before trusting the timer:

```bash
systemctl --user start coord-drive-queue.service
journalctl --user -u coord-drive-queue -n 50
systemctl --user list-timers | grep drive-queue   # next-elapse ~15min out
```

`enable --now` fires a tick and the verification `start` above fires another
seconds later, so this sequence deliberately produces **back-to-back ticks**
against a non-empty queue. That is safe: a drive launched inside the startup
grace window (5 minutes) reconciles as `starting` — occupying its slot, its
`attempts` untouched — rather than as a death (#1794). Run `coord drive-queue
list` after and confirm `state=running attempts=0`, not `attempts=1`.

Logs live in the user journal: `journalctl --user -u coord-drive-queue -f` to
follow live, `-n 50` for the last tick's summary. With an empty queue a tick
logs `capacity: 0/1 occupied, 1 free` / `no launch` and exits 0 — that is the
timer working, not a problem.

### 2a. Verify the launched drive survives the tick (#1830)

The unit ships `KillMode=process` for exactly one reason: without it, systemd's
default `KillMode=control-group` reaps the **entire cgroup** — including a
tmux server the tick's own `coord drive --tmux` had to spawn — the instant the
oneshot tick exits, seconds after launch. Do not remove `KillMode=process` from
`deploy/coord-drive-queue.service`.

**You cannot verify this fix with a terminal open on the box.** If a tmux
server is already running (which an interactive session guarantees — `tmux
ls` will show one), `tmux new-session` hands off to that pre-existing server
instead of spawning its own, and the new session lives outside the tick's
cgroup regardless of `KillMode`. The bug — and therefore the fix — is only
observable when **no tmux server exists yet**. Testing "attended" always
looks fine and proves nothing; this is exactly how the bug shipped invisibly
in the first place (see #1830).

To verify for real:

```bash
# 1. Make sure there is truly no tmux server — kill any that exists.
#    (If you have an interactive session open on this box, this WILL kill
#    it — do this from a box/user with no other tmux usage, or accept that
#    tradeoff deliberately.)
tmux kill-server 2>/dev/null; tmux ls   # must print "no server running"

# 2. Queue something and fire one tick exactly as the timer would.
coord drive-queue add REPO ISSUE
systemctl --user start coord-drive-queue.service

# 3. Confirm the launched session is STILL ALIVE after the (oneshot) unit
#    has already finished — this is the assertion that matters, not the
#    tick's own exit code.
systemctl --user is-active coord-drive-queue.service   # inactive (oneshot, done)
tmux ls                                                 # the coord-drive-* session is listed
coord drive-sessions                                    # shows it live

# 4. Confirm it actually did work, not just "stayed alive" — a session that
#    is merely present but silently dead inside proves nothing either.
coord drive-queue list                                  # state=running, then dispatched
```

If step 3's `tmux ls` comes back empty (or the session is gone within
seconds), the fix regressed — most likely `KillMode=process` was dropped from
the installed unit, or a hand-maintained `~/.config/systemd/user/` copy is
stale relative to `deploy/coord-drive-queue.service`. Re-`cp` it and
`systemctl --user daemon-reload`.

## 3. Stop it

Two different questions, two different commands — the same "hold, don't
kill" distinction `coord pause` draws for routing, and it trips people the
same way when they only know the pause half:

```bash
# 1. Stop LAUNCHING new drives from the queue. Does NOT touch anything
#    already running.
systemctl --user stop coord-drive-queue.timer

# 2. Stop a drive that is ALREADY running: kills its tmux session and
#    releases the per-issue flock instantly (so `coord drive`'s own
#    already-driving guard never sees a stale lock).
coord drive-stop REPO ISSUE
```

Say both, in that order, when you tell someone how to stop the queue.
Stopping the timer alone leaves every currently-launched drive running to
completion (or its deadline); it just stops the tick from starting the *next*
one. `coord drive-sessions` lists every live `coord drive --tmux` session (with
its own attach/stop hints) if you need to see what's actually running before
deciding what to `drive-stop`.

**Stopping the timer also stops reconciliation** — the thing that notices a
drive *finished* and moves its row from `running` to `done` lives inside the
same `coord drive-queue tick` the timer runs (#2110). With the timer stopped,
the last drive's row stays `running` until something ticks again, which can
wedge a caller that treats that row as evidence of in-flight work — see
`docs/AGENT_OPERATIONS.md`'s propagation section for the incident this caused.
If you need the queue's view of reality updated *without* launching anything
— e.g. right before that caller reads it — run:

```bash
coord drive-queue tick --reconcile-only   # same as --max-parallel 0
```

It runs the same reconcile pass as a normal tick (finished → `done`,
permanently-refused → `blocked`, CI-pending → `parked`, a `blocked` entry
whose gate cleared → `waiting`, #2230 — see §4b) and then stops: no capacity
walk, no queue-level alert, no launch. Safe with the timer stopped or running.

### `parked` — and the two ways out of it (#1891 / #2158)

`parked` is the queue's "not your problem" state: the drive died, but the only
thing the board holds against the entry's merge is that CI has not reported
(`CI running: …`) or reported without a verdict (`CI infra: …`, #1892).
Relaunching would just observe the same silence, so the tick parks instead —
**no attempt spent**, no escalation, no operator command needed. Unlike
`blocked`, you are not expected to do anything about it.

That promise needs the release predicate to be refreshable *without* the merge
the park is withholding, which until #2158 it was not: the reading came from
the raw `merge_queue` row's `error` column, which only a live `coord merge`
attempt ever writes — and a parked entry runs none. code-coordinator#2138
(2026-08-12) sat parked **7h25m** over CI that had gone green 41 seconds
*before* the park was written, and moved only when an unrelated merge happened
to rewrite the board. There are now two exits, and a park always has at least
one of them:

1. **The board's own CI rollup.** When `/board`'s `merge_plan` row for the
   entry re-derives clean and its `ci_summary` shows every check finished with
   none failed, that outranks the persisted string and the entry resumes on
   the next tick. Requires a daemon lane (`board_service` set) — the plan is
   computed, not stored, so a tick reading the local DB directly has no
   `merge_plan` section at all.
2. **A ceiling on any reading that cannot refresh itself**
   (`PARK_STALE_SECONDS`, 45 min). That covers the local-DB / no-`merge_plan`
   lane — a tick reading the DB directly (no `board_service`), where exit 1
   above never applies because there is no `merge_plan` section to re-derive
   from. The entry returns to `waiting` and re-enters the normal walk; the resume
   reason says only that the reading went unrefreshable, not that CI passed,
   because nothing on that lane knows whether it did. A park founded on the
   live plan's own objection is exempt — it re-derives every board build and
   goes false by itself, so it is held with no ceiling however long CI takes.

A Gate-A park (#2063) is gated on a human, not on CI, and neither exit
releases it.

## 4. Read the alert — `QUEUE: STALLED` vs `QUEUE: BLOCKED`

The TUI status bar always shows a `QUEUE: …` segment (never blank — silence
reads as "nothing to report" when it might mean "the segment crashed"):

| Segment | Meaning | What to do |
|---|---|---|
| `QUEUE: empty` | nothing queued | nothing |
| `QUEUE: 1 running · 3 waiting` | normal operation | nothing |
| `QUEUE: STALLED — 3 waiting, none eligible` (warn) | capacity is **free**, but every waiting entry is deferred — usually waiting on an `--after` pre-req that hasn't landed yet | usually self-resolves once the pre-req lands; `coord drive-queue status` shows the alert's `gate_readings` detail lines naming exactly which entries are deferred and why |

Entries deferred **only** because their repo is at `--max-parallel-per-repo`
(§9) deliberately raise no alert at all — that is the queue working, not a
stall. A mixed tick (something also deferred on a pre-req, or blocked) still
alerts, and lists the repo-limit deferrals alongside.
| `QUEUE: BLOCKED 2 · 1 waiting` (warn/crit, **outranks a simultaneous stall**) | one or more entries are `blocked`: a dependency cycle, an `--after` pre-req that can't resolve, or a drive session that died `attempts` times in a row (default `DEFAULT_MAX_ATTEMPTS = 2`). Since #2230 (§4b) this is no longer necessarily permanent — a `blocked` entry whose cause is its own merge gate may resume on its own before you get to it | usually needs an operator action — see below, and §4b for the cases that resolve themselves |

`coord drive-queue status` (or the TUI overlay) shows the reason for both.

**Since #2230, not every `blocked` entry needs a human.** Every tick
re-checks a `blocked` entry's own merge gate (unless it blocked for one of
the two PERMANENT causes in §4a, or a cycle/broken `--after`) and, the moment
that gate reads clear, moves it straight back to `waiting` with `attempts`
reset — no remove+add, no operator action. See §4b below for the detail and
the churn bound. If a row is STILL `blocked` by the time you're reading this,
either it never had a re-evaluable cause, its gate is genuinely still shut, or
it has already oscillated past `MAX_BLOCKED_RESUMES` — `coord drive-queue
list` says which. For that entry, the fix is remove-and-re-add — there is
deliberately no `coord drive-queue reset`, because a fresh row is already
`waiting` with `attempts=0` and no stale `--after`:

```bash
coord drive-queue remove REPO ISSUE && coord drive-queue add REPO ISSUE
# re-add WITHOUT the bad --after if that was the cause
```

or, from the TUI overlay, select the blocked entry and press `u`.

`last_reason` is a **snapshot**, taken the instant it was written and never
re-validated (#2133) — the condition it names can resolve minutes or hours
later while the text stays exactly as first written. `coord drive-queue
list` shows its age next to it (`last (3h ago): checks_failed …`) precisely
so it never reads as a live diagnosis; treat the reason as history and go
check the board/CI/review state for what is *actually* blocking now,
especially once the age climbs past a few minutes.

### 4a. Blocked *without* spending an attempt — permanent causes

Two `blocked` reasons are **not** "died `attempts` times". They block on the
**first** tick that observes them, with `attempts` untouched, because a
relaunch is guaranteed to reproduce the same outcome. Both are read from the
drive's own `drive_exited` audit row for that launch, so the reason survives
the tmux session:

| Exit code | `drive-queue` outcome | Means | Fix |
|---|---|---|---|
| `5` (`EXIT_DISPATCH_REFUSED`, #1844) | `refused` | a **pre-dispatch guard** refused the exact dispatch this run attempted (oracle readiness, epic target, …) | the guard's own remedy, quoted verbatim in `last_reason` |
| `6` (`EXIT_DEAD_END`, #2019) | `dead_end` | the **board row is terminal and unactionable**: nothing active on the fleet, every stage finished, no gate transition available to any amount of polling | the recovery command in `last_reason`; also recorded as a `coord escalate` row and posted to the issue |

A dead end is what `coord drive` used to report as `no state change in
140.558m`, forever — see `coord/dead_end.py` for the shapes it recognises and,
just as importantly, the shapes it deliberately does **not** (a Test stage
that has merely not been dispatched *yet* is indistinguishable from one that
never will be, so it is left alone rather than escalated). Elapsed time is not
an input: the predicate refuses to fire while anything is active, so a
legitimately quiet long-running stage can never trip it however long it runs.

### 4b. `blocked` self-heals when the gate clears (#2230)

Before #2230, `blocked` was terminal in the strongest sense: nothing ever
asked again whether the condition that blocked an entry had since cleared on
its own, even when it plainly had — quadraui#309 sat `blocked attempts=2` for
~11h while `coord gates quadraui 309` read `merge: READY` for most of that
window, and the driver had simply exhausted its two attempts against a gate
reading that was no longer true by the time anyone looked.

Every tick now re-examines every `blocked` entry, EXCEPT:

* the two permanent causes in §4a (`refused`/`dead_end`) — a relaunch cannot
  change either outcome, so re-checking would just burn a live gate call to
  re-confirm an answer already on record;
* an entry blocked on a broken `--after` graph (a cycle, a pre-req that is
  itself `blocked`/`failed`, an unknown issue) — that is a QUEUE-graph
  problem, not a merge-gate one, and is not what this sweep re-checks (see
  `coord drive-queue list`'s "unsatisfied" line, refreshed on every read by
  #2183, for that diagnosis instead);
* an entry with no evidence either way — one that never reached the merge
  queue at all has nothing this sweep can cheaply check, and guessing would
  re-burn attempts on entries that provably cannot change, exactly the "worse
  than nothing" sweep the issue that added this warns against.

For everything else — overwhelmingly the `exhausted` outcome, a drive that
died `attempts` times in a row for whatever reason — the tick asks the SAME
question `coord merge --plan`/`--only` would answer right now: is this
entry's merge gate still objecting? A CONFIRMED-clear reading moves the entry
straight back to `waiting`, `attempts` reset to 0, and it re-enters the walk
on this same tick (it can launch immediately if a slot is free). A confirmed
or unreadable "still shut" reading changes nothing — no write, no line in the
render, the row looks exactly as it would have before this feature existed.

**The churn bound.** An entry that gets auto-resumed and reblocked
`MAX_BLOCKED_RESUMES` (3) times in a row stops being auto-resumed — that
pattern (a gate that clears and reblocks repeatedly) is itself the
interesting fact, not something a fourth retry is likely to fix. Once the
ceiling is hit the row stays `blocked`, its `last_reason` is rewritten to say
so explicitly, and a `coord escalate` record is written the same as any other
blocked entry — `coord drive-queue list` shows the running count as
`resumes=N/3` next to `attempts=`/`deferrals=`.

**Where the evidence comes from.** On the daemon host — the only machine
`coord drive-queue tick` ever runs on — the tick reads the local DB directly
and has no live `merge_plan` section to consult for free, so it pays for one
`coord.merge_queue.entry_gate_status` call (the same live backend `coord
merge --plan`/`--only` build) per QUALIFYING blocked entry — bounded to the
entries actually sitting in `blocked` right now (typically 0-2), never the
whole queue. See `coord.commands.drive_queue._fetch_live_blocked_gate`'s
docstring for the full justification; it is the exact same mechanism #2182
already uses to release a `parked` entry on the same lane.

## 5. The pinned-CLI trap

The timer runs a **specific installed `coord`**, not a checkout — it does not
notice a merged fix on `main` until that `coord` is upgraded. `ExecStart`
points at `%h/.coord-venv/bin/coord` **directly** (#2314 — not
`~/.local/bin/coord`, even though on dellserver that is normally a symlink
into the very same venv): the same venv `deploy/coord-agent.service`,
`coord-serve.service`, `coord-notify.service` and `coord-web.service` already
run from (see `install-agent.sh`). Pointing at the venv path directly, rather
than through `~/.local/bin/coord`, closes the gap that path left open: a
worker running `pip install --user` (or an editable `pip install -e .` that
lands its own console-script shim there) can silently overwrite
`~/.local/bin/coord` with something that no longer points at the pinned venv
at all, and the first this tick would know about that is the next `coord
drive-queue tick` running whatever the worker just wrote. Going straight to
`~/.coord-venv/bin/coord` makes that overwrite structurally irrelevant to
this unit. That already satisfies #1523's "the runner's own CLI must not be
rewritable by worker branch churn" requirement, **and** it means this venv is
upgraded by the ordinary, already-documented fleet procedure. It is
deliberately **not** a bespoke pinned venv like the epic sequencer's
`~/.coord-cli-venv` (`docs/AGENT_OPERATIONS.md`'s "fourth lane"), which only
a human remembering to run the upgrade keeps current, and which was found
three releases stale on 2026-07-29.

```bash
coord agent update --machine dellserver   # or --all; upgrades ~/.coord-venv
                                           # in place and restarts coord-agent
~/.coord-venv/bin/coord --version         # VERIFY it took — an upgrade
                                           # silently no-ops more often than
                                           # you would think
```

`coord agent update` restarts `coord-agent`, which kills any headless worker
currently running on dellserver — check for active assignments first (see
`docs/OPERATING_GOTCHAS.md` §2). It does **not** interrupt a `coord drive
--tmux` session the queue already launched: that runs as its own tmux/`coord
drive` process tree, independent of `coord-agent`.

**If you ever install this timer on a machine other than the current daemon
host, verify first that its `~/.coord-venv` is a non-editable install:**

```bash
readlink -f ~/.coord-venv/bin/coord              # must resolve INTO the venv,
                                                  # not out to a checkout
~/.coord-venv/bin/pip show code-coordinator | grep -i editable
                                                  # must print NOTHING
```

The topology is **per-machine, not universal** — a dev box's `~/.coord-venv`
(or, on a box with no such venv at all, whatever a hand-edited unit points
at instead) can be repointed at a checkout, which would let a worker's own
branch churn rewrite the runner mid-run. That is fine for interactive use; it
is not safe under an unattended timer. Install `coord-drive-queue.timer` only
where the verify step above comes back non-editable.

**Belt and suspenders (#2314):** even on a correctly-pinned host, the tick
itself now refuses to launch anything if `coord`'s OWN process happens to be
running from an editable checkout that has drifted off its default branch —
see `coord.drive_queue.plan_tick`'s `editable_drift` parameter and
`coord.cli._editable_checkout_drift`. This used to be an advisory-only
warning printed at CLI startup that nothing unattended ever reads; it is now
a hard refusal, rendered the same way a release cordon is (`no launch — this
host's coord is drifted onto ...`), with reconciliation still running
underneath it exactly as a cordon leaves it.

## 6. The deadline trap (#1660)

An expired `coord drive --deadline` (default 240 minutes; `drive-batch.sh`
uses 120) stops the **observer**, not the work — the fleet carries Test,
Review, and Merge through to completion regardless, exactly as described in
[`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md#9-the-unattended-driver-coord-drive-and-scriptsdrive-batchsh).

The queue's `tick` is deliberately built so this does not cause it to launch
on top of invisible live work: capacity is counted from **board state**
(whether the work is still `ACTIVE`), not from a session count. A drive whose
observer already exited on its deadline still occupies a queue slot — the
tick's reconcile step reports it `held` with reason `"drive session is gone
but work is still ACTIVE on the board (observer deadline, #1660) — still
occupying a machine"`.

The trap for an operator is the opposite direction: **`coord drive-sessions`
only lists live tmux sessions**, so once the observer has exited on its
deadline, that drive is invisible to `coord drive-sessions` even though it is
still occupying a queue slot and the fleet is still actively working it. Do
not use `coord drive-sessions`'s count as "how much of the queue is actually
running" — cross-check `coord drive-queue status` (which reflects board
state) instead.

## 7. The startup window (#1794)

A drive is not *established* the moment `coord drive --tmux` exits 0. #1606's
launch verification proves a tmux session exists and has written to its run
log; it does not prove the drive has registered anywhere the tick can see.
For up to a couple of minutes on a loaded host the entry has **no live
session reading** (`list_drive_sessions()` returns `[]` for "tmux
unavailable" / "no server running" / "the call timed out" exactly as it does
for "no sessions") and **no work on the board** (it has not dispatched yet).

Before #1794 that was indistinguishable from a death, and on 2026-08-03 — the
first unattended run of the timer — a tick 40s after a launch declared a
healthy drive dead, spent a retry attempt, and started a second `coord drive`
for the same issue. Left alone that walks an entry to `attempts=2/2` and
`blocked`, i.e. an unattended queue parks healthy work and reports it failed.

The tick now treats a `running` entry launched within
`DRIVE_STARTUP_GRACE_SECONDS` (5 minutes, ~2.5x the measured startup) as
`starting`: it occupies its slot, keeps its state, and never spends an
attempt. The same window guards the launch decision, so no tick can start a
second drive for an issue whose last launch is that recent — `coord drive`'s
per-issue flock is the last line of defence, not the first.

What this looks like in the journal:

```
  reconcile claude-coordinator#1762: starting — drive is still starting —
      launched 41s ago, inside the 300s startup grace window (#1794);
      not a death, still occupying a machine
  no launch — at capacity (1/1 occupied)
```

Death detection is unchanged past the window: no session, no active work,
nothing landed and a launch older than 5 minutes still reconciles to `retry`
and then to `blocked` at `max_attempts`. The failure signature to watch for
is `retry — drive session died without landing the work` appearing **within
seconds** of a launch; that must never happen again.

## 7a. Retry spacing (#2273)

A `retry` reconcile used to relaunch in the SAME tick — "died and came back"
was one event, by design (§7's whole point is not idling a full interval on
a confidently-dead drive). On 2026-08-15 that same-tick relaunch is exactly
what let quadraui#508 and coord-portal#83 each burn their entire two-attempt
budget inside **six minutes** — a transient `coord assign` failure that had
cleared by the time a human noticed, 18 minutes later. Two attempts fired
minutes apart is not a retry policy against a transient; it is two samples
of the same short window.

So a died entry's NEXT launch is now paced by real wall-clock time, not just
by tick cadence: 1 minute after the first attempt, 5 minutes after the
second, 20 minutes after the third and beyond. Widened further — 5 minutes
minimum — when the died launch never produced a board-visible assignment at
all (`assignment_id` never existed for that run): the cheap, already-recorded
approximation of "this was infrastructure, not a code failure" that does not
need the full transient-vs-real classification (blocked on a stderr-capture
prerequisite this queue does not have yet).

What this looks like in the journal — a died entry that is still pacing its
next attempt, not stalled:

```
  reconcile claude-coordinator#1762: retry — drive session died without
      landing the work, launched 320s ago (attempt 1/2) — requeued at
      position 0
  defer claude-coordinator#1762: retry backoff: the previous attempt failed
      4s ago, next attempt permitted in 56s (60s spacing after 1 attempt(s)
      — #2273, so a transient dispatch failure cannot spend the whole retry
      budget inside one tick cadence)
  no launch — every waiting entry is pacing a retry after a recent failure
      (#2273); it resumes once the backoff elapses
```

This raises no `QUEUE: STALLED` alert (same posture §9's per-repo ceiling
already takes): nothing is wrong with the entry, no operator can make the
clock move faster, and it resumes on its own the moment the backoff elapses.
`coord drive-queue list`/`status` show the pacing directly in `last_reason`
if you want to confirm it is working rather than wedged.

## 8. The cross-host trap (#1870)

Liveness (`coord.drive.list_drive_sessions()`) is always a **local** `tmux
list-sessions` — but `tick` can run from any machine that can reach the board
daemon (see "1. Enqueue" above and `coord drive-queue --help`), and the queue
row itself is board-global. Two live `coord drive` sessions on the same issue
at once, one healthy:

```
elitebook:   launched 01:20Z — work=done, test=running, pushed 941df76
dellserver:  launched 02:07Z — a SECOND drive the timer's own tick started
```

The 02:07Z tick ran on `dellserver` (its documented home, "2. Install the
timer" above); the entry had been launched by hand from `elitebook` 47
minutes earlier. `dellserver`'s tick checked *its own* tmux, found nothing,
and — before #1870 — concluded the drive had died: `"retry — drive session
died without landing the work, launched 2841s ago (attempt 1/2)"`. It had
not died; it was on a different machine, well past #1794's grace window,
which delays a misclassification but cannot prevent one that is not
transient.

Every `drive_queue` row now carries `launch_host` — the short hostname of the
machine whose tick actually ran `coord drive --tmux` for it, stamped at
launch alongside `session_name`/`launched_at`. `tick` compares that against
its own hostname before trusting a `retry` verdict: a mismatch reconciles to
`unknown`, not `retry` — the entry keeps its slot, its state, and its attempt
count untouched, exactly like `starting` (#1794) and `held` (#1660) before
it. Only a tick running on the SAME host that launched an entry may ever
retry or relaunch it. A row with no `launch_host` (predates this column, or a
hand-edited `running` state) degrades to the pre-#1870 behavior exactly —
reconciled locally, same as always.

Practical consequence: the timer still belongs on the daemon host (§2), but
running `tick` by hand from a *different* machine — e.g. to walk a queue
entry forward while debugging — no longer risks a duplicate launch on the
next scheduled tick. It does mean that entry's own reconciliation (and its
eventual `retry`/`done`) now belongs to whichever host launched it, until
that host's tick runs again.

## 9. Per-repo capacity (#1972)

`--max-parallel` is the **global** ceiling. There is a second one:
`--max-parallel-per-repo`, **default 1**.

The hazard that forced serialisation in the first place is strictly
*intra-repo* — a merge stales every other queued branch's Test verdict in
**that** repo, because #1479's freshness keys on the base of the branch's own
repo (the cascade at the top of this document). A vimcode merge cannot stale a
quadraui branch. So repo is precisely the boundary along which extra
parallelism is safe: within a repo is the risky case, across repos is nearly
free.

One global counter conflated the two. With `--max-parallel 3` and a queue of 39
code-coordinator entries followed by one quadraui entry, a tick launched the
same-repo neighbours most likely to stale each other and never reached the
quadraui entry that could have run alongside for free. The only way to get the
wanted behaviour was hand-chaining `--after` across 38 entries — tedious,
fragile, and wrong the moment the queue was reordered.

Now the tick counts occupancy per repo as well as globally, and an entry whose
repo is at the ceiling **defers**: position unchanged, no attempt spent, no
escalation — a "not yet", exactly like an unsatisfied `--after`. The launch
walk is unchanged (it already skipped deferred entries and took the first
eligible one), so it simply lands on the first entry from a repo with headroom:

```
capacity: 1/3 occupied, 2 free
  per-repo: claude-coordinator 1/1 (limit 1/repo, counted from board state —
      a drive whose observer died still holds its repo's slot)
  reconcile claude-coordinator#1650: alive — drive session is live
  defer claude-coordinator#1654: repo claude-coordinator at its limit (1/1) —
      deferring so a different repo can launch
  launch quadraui#302
```

Both ceilings apply, **global first**: a full fleet still launches nothing, for
any repo. `--max-parallel-per-repo 0` turns the per-repo ceiling off entirely
(one global counter, the pre-#1972 behaviour); raising it above 1 is reasonable
now that #1715 batches revalidation, which is why it is a flag rather than a
hardcoded 1.

**Set the fleet-wide value in `coordinator.yml`, not a systemd drop-in
(#2573).** `--max-parallel-per-repo` resolves in order: the flag on this one
invocation (if given) → `pipeline.max_parallel_per_repo` in
`~/.coord/coordinator.yml` → `coord.drive_queue.DEFAULT_MAX_PARALLEL_PER_REPO`
(1). The packaged `deploy/coord-drive-queue.service` deliberately omits the
flag so this resolution can apply.

```yaml
pipeline:
  max_parallel_per_repo: 2
```

Before #2573 the only way to raise this on a running fleet was a
`~/.config/systemd/user/coord-drive-queue.service.d/override.conf` drop-in —
and a drop-in must restate the packaged unit's **entire** `ExecStart=` line to
change even one flag on it. That restated copy then silently drifts from the
packaged unit forever after: dellserver's live drop-in, built solely to carry
`--max-parallel-per-repo 2`, was found to have reset `ExecStart=` back to
`%h/.local/bin/coord` — reverting #2314's pinned-venv `ExecStart=` path (§5
above) right back to a path a worker's stray `pip install --user` can
overwrite, purely as an unnoticed side effect of the drop-in's own existence.
`coordinator.yml` has no such coupling — it is read fresh on every tick, so
there is nothing here to drift. If a host still carries an
`override.conf` that exists only to set this flag, move the value into
`coordinator.yml` and delete the drop-in.

Two things to know:

* **A repo-limited queue raises no alert.** Every remaining entry waiting on
  its own repo's in-flight drive is the queue working, not a stall — same
  posture as being globally at capacity. A tick where *anything* is deferred on
  a pre-req or blocked still raises the usual `QUEUE: STALLED` / `BLOCKED`
  alert, with the repo-limit lines listed alongside.
* **Per-repo occupancy inherits §6.** It is counted from **board state**, not
  live sessions, so a drive whose observer died on its deadline still holds its
  repo's slot until something reconciles it. After #1972 that wedges one repo
  instead of the whole queue — better, but also quieter, which is why the
  breakdown and its provenance are printed on every tick.
* **It is not a machine guarantee.** Two entries for different repos pinned to
  the same machine with `--machine` are both eligible here and can still
  contend downstream. The per-repo counter says nothing about hosts.

## 10. #1715

See "Read this before queuing more than ~2 issues" above, at the top of this
document.

## 11. #1738

See the same section — it's the smaller, already-partially-fixed version of
the same class of problem (a content-irrelevant base move staling a Test
verdict).

---

## See also

- [`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) — the deadline trap and
  the `coord-venv` stale-CLI trap in their general form (this doc is the
  drive-queue-specific instance of both).
- [`docs/AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md) — the service/host/unit
  table `coord-drive-queue` has a row in, and the sibling `coord-notify`
  timer walkthrough this one mirrors.
- `scripts/drive-batch.sh` — the tool this replaces for anything past a
  one-off foreground run; its header explains when to still reach for it.
