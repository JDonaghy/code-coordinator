# Fleet notifier — phone push when nobody is coming (#1632)

You kick off an epic in the morning, hoping it is demo-ready tomorrow. You go
out. You have your phone and no terminal. **The epic dies a quarter of the way
through and you find out at 6pm.**

Every other surface — `coord status`, the TUI, `coord diagnose`, `coord web` —
requires you to already be sitting at a terminal, which is precisely where you
are not. This is the away-from-terminal channel.

Deliberately **separate from the fleet-health milestone** (#53 / epic #1625):
health checks are one *producer* of events, drive/pipeline state is another, and
both want the same phone-reachable, quiet-hours-aware channel. Building the
channel inside #53 would have forced the drive-event work to duplicate it.

---

## The predicate

> **Notify when the pipeline has stopped, or is stalled, and will not advance
> without a human.**

Explicitly **not** "something bad happened". A failed test, a request-changes
review, a mechanical merge conflict — the auto-loop already handles all of
those, and pushing them is the noise that trains an operator to mute the
channel. **In normal operation this fires approximately never.** It does not
ping progress either: no "3/7 done" messages.

| condition | signal | confidence |
|---|---|---|
| `STUCK:` emitted by a worker | explicit, self-reported | highest — no baseline involved |
| drive halted | a `drive_escalations` row: the drive exited and a human must decide | terminal |
| gate parked `HUMAN_REQUIRED` | merge-queue state; the queue will not retry | terminal |
| fleet CRIT invalidating in-flight work | disk mainly (#1625) — a verdict recorded under disk pressure is worse than a red one | terminal-ish |
| stalled past its nudge | `drive` nudged a stalled stage (#1593) and it is *still* stalled | strong |
| output silence | no new log line / `STATUS:` for longer than the stratum's learned threshold | strong |
| total elapsed vs baseline | past the stratum's p90 **and** (silent past the silence threshold **or** silence unconfirmable because the agent is *confirmed* unreachable) | weakest, fires last |

**The three worker probes are ranked, not averaged.** A worker that printed
`STUCK:` is reported as stuck, not as "stuck and also somewhat over its p90".
One worker, at most one notification.

**Duration alone must never page (#2609).** Early operation showed
`over_baseline` firing on workers that were still actively emitting output —
a worker 20 minutes into a 21-minute run, mid-progress, paged and then
finished on its own a minute later. That is structural, not a tuning miss: a
p90 threshold trips on 10% of *all* healthy work by construction, and no
percentile choice removes that — it only moves which 10%. So `over_baseline`
is now **gated on output silence**, not a substitute for it: it only fires
when the leg is past its duration baseline *and* has gone quiet past the
same silence threshold the output-silence probe uses — **or** quietness
could not be confirmed because the owning agent is *confirmed* unreachable
(it did not answer `/status` at all). That second branch matters: "we don't
know" is deliberately treated the same as "silent" **only** in that one
case, because an unreachable agent is the single scenario this channel's
contract names most literally — nobody is coming because there is no agent
left to come — and it is the one case none of the other probes can catch
(`STUCK:` and output silence both need the same agent-status data a dead
agent can't supply).

**"We don't know" is not the same as "it's gone" (#2657).** An earlier
version of this gate conflated two different reasons `last_output_at` can
come back `None`: the agent never answered at all, versus the agent
answered fine but the assignment had simply finished and no longer appears
in its `active` list — the board's own `in_progress` row lags the agent by
up to two notifier ticks (`coord-notify.timer`'s reconcile cadence is 5
minutes; this channel ticks every 120s). The second case is not silence at
all, it is the *strongest possible evidence the leg is done* — and the old
code paged it as `over_baseline` with the wording "Output could not be
confirmed (agent unreachable)" even though the agent was up and had
answered. Two structural fixes close this: the collector now checks the
agent's `completed` list too, and drops the probe **entirely** for any id
the agent reports finished, regardless of what the board still says; and
`WorkerProbe.agent_reachable` (`True`/`False`/`None` for "never asked")
lets the predicate's escape hatch require a *confirmed* unreachable agent
(`agent_reachable is False`) rather than merely an absent `last_output_at`.
A reachable agent that does not list an id now produces no event at all,
and the "agent unreachable" wording is reserved for when the collector
actually failed to reach that machine.

Whenever `last_output_at` IS known and quiet, that gate condition is a
strict subset of the silence probe's own condition, so the silence probe
already fired and — being ranked stronger — is what actually gets reported;
`over_baseline` only surfaces on its own for the "duration exceeded, agent
confirmed unreachable" case. A worker that is slow but still confirmed
talking (or simply finished) is not evidence nobody is coming, and is not
this channel's business — a pure duration/cost signal belongs in `coord
status` or the digest, not the push transport.

---

## "Far too long" is learned, never a fixed timeout

A constant is wrong the day it is written and rots silently as models, repos and
hardware change. Milestone #37 already ships per-leg duration on every
assignment row, so **no new instrumentation was needed** — only a baseline
computed over records that are already there.

**Stratified by `(repo, assignment type, tier)`.** A `work` leg on a
`tier:large` vimcode issue and a `review` on a small coord issue are not the same
population; an unstratified fleet-wide mean fires constantly on the slow tail and
never on the fast one. `tier` comes from the issue's `tier:*` label (it lives on
the issue, never on the assignment row, so the baseline joins against the cached
`issues` table).

**Cold start is a real state, not an edge case.** Under `min_samples` (default
5) completed legs a stratum has *no* baseline: it falls back to a generous
absolute ceiling for that assignment type, **and the notification says so**.
"Over the ceiling for a stratum we have never measured" is a much weaker claim
than "over the p90 of 40 comparable legs", and the message has to make that
difference visible on a phone screen. A population of one never fires.

**p90 vs 2× median.** p90 is what the predicate uses. `coord notifier baselines`
prints both, side by side, per stratum — #1632 asks for the alternative to be
evaluated against real fleet data before either is committed to permanently,
which is impossible if only the chosen one is ever computed.

**The silence threshold is baselined too.** A repo whose test suite takes 20
minutes legitimately goes quiet; a fixed value would either spam that repo or
never fire on a fast one. Today it is derived from the same duration population
(`silence_fraction` × the stratum's median, clamped to `[10m, 45m]`), because
nothing in the fleet records per-leg silence gaps yet.
`baseline.build_baselines(..., silence_samples=...)` is the seam for real
samples when they exist — the call sites do not change.

Caution (#1593): **a quiet pane is not evidence of progress, and a busy one is
not either.** Silence is a *suspicion*, which is why this is a notification and
never an action.

---

## Fire once, escalate once

* **Once per subject per condition, for ever.** A genuinely slow job must not
  re-notify on every tick. The ledger is on disk, so a `coord serve` redeploy
  does not resurrect it either.
* **Never downgrade.** Once a subject has reported a strong condition, a weaker
  one for the same subject is silence, not news.
* **Escalate on a state change.** A subject that reported a suspicion
  (running-slow / silent / stalled) and then genuinely *stopped* re-notifies
  exactly once, carrying the earlier notice's condition so the second message
  reads as an escalation rather than a duplicate.

---

## Quiet hours: a deferral window, not a filter

Events raised between `start` and `end` are **held, coalesced, and delivered as
one digest** the moment the window closes. Nothing is discarded — you are not
disturbed, you are still told. A daemon that missed the exact 08:00 tick still
flushes on the next one; the failure mode of an edge-triggered flush is silence,
which is the one outcome this feature exists to prevent.

**No severity level pierces quiet hours.** Severity is assigned by the sender,
and a receiver who gets woken by things that did not warrant it mutes the channel
within a month — at which point the whole feature is dead. This is a hard design
rule: `digest.partition()` takes no priority input at all, and the ntfy priority
`to_message()` sets is cosmetic.

**The exception is a deadline, not a severity.** You know when something is
time-critical and the system does not. `coord drive --urgent` opts *that drive*
out of quiet hours for its duration — opt-in, scoped to one issue, and it
expires on its own (`notifications.urgent_ttl_hours`) so a forgotten flag cannot
make every future night loud. `coord notifier urgent <repo> <issue>` does the
same thing for a drive already running.

The window shares `machines[i].quiet_hours`' parser and `QuietHours` value type
(#1862), including the required IANA `tz` — `coord serve` runs on UTC, so a naive
`"22:00"` would defer at the wrong local hour.

---

## Transport

A self-hosted **ntfy** on the daemon host, reachable over Tailscale. The
operator is on Android, so the ntfy client holds an instant-delivery connection
straight to that server — no relay, no third party. **Nothing leaves the
tailnet**, which matters because event text carries repo names, issue titles and
failure detail.

Delivery is one HTTP POST, and the notifier's coupling to ntfy is confined to
`transport.NtfyTransport`. Pushover / web-push / e-mail can be added by
implementing `transport.Transport` without touching the predicate.

Every notification that can name an issue carries a deep link to its `coord web`
PWA view (`{web_base_url}/pipeline/{repo}/{issue}`) — it has to be actionable
from a phone, not just prose.

---

## Advisory and isolated

> An unreachable ntfy server must not affect dispatch, routing, the board, or any
> verdict.

Same rule and same reason as **#1485**, where `/health` data was read as
authoritative and silently degraded review routing. Structurally:

* `Transport.send()` returns a bool and never raises; `safe_send()` contains even
  a third-party transport that ignores that contract.
* `service.tick()` catches everything and returns a report. The daemon's
  `_tick_loop` wraps it again anyway.
* Every collector source fails open to "nothing" — an unreachable agent, a
  missing table, a slow board.
* State lives in its own JSON file (`~/.coord/notifier.json`), not in
  `coord.db`, so a corrupt notifier state cannot lock or perturb the database
  dispatch and the board depend on.
* A **failed send is not ledgered** — the condition re-derives next tick, so an
  ntfy server down for an hour costs a delayed notification, never a lost one.
* `tests/test_notifier_isolation.py` asserts all of the above, including that
  `coord.dispatch` / `coord.brain` / `coord.review` / `coord.merge_queue` /
  `coord.reconcile` do not import this package at all.

---

## Where it runs

On **#1616's daemon clock** (`coord serve`'s `_tick_loop`), on its own slower
cadence (`COORD_NOTIFIER_INTERVAL`, default 120 s; `0` disables). It does not
ship a clock of its own — two independent clocks is exactly how a fleet ends up
with two that disagree.

For the same reason it does not define "stalled" a second time: `drive`'s
existing stall branch (#1593) **publishes** each nudge into the notifier store,
and the predicate only asks whether the stall survived it.

---

## Configuring it

Off by default. Omit the `notifications:` block entirely and nothing changes.
See the fully-commented block at the end of `coordinator.example.yml`.

```yaml
notifications:
  enabled: true
  transport: ntfy                        # or `none` to run the predicate silently
  ntfy_url: http://dellserver:7440
  ntfy_topic: coord-fleet
  web_base_url: http://dellserver:7434
  quiet_hours:
    start: "22:00"
    end: "08:00"
    tz: America/Chicago                  # REQUIRED — the daemon runs on UTC
```

Enabling `transport: ntfy` without both `ntfy_url` and `ntfy_topic` is a
config-parse error: a notifier that silently delivers nothing is
indistinguishable from a healthy fleet, which is the one failure this feature
exists to prevent.

---

## Operating it

```bash
coord notifier status                    # on? where does it send? what is held?
coord notifier baselines                 # p90 vs 2x median, per stratum, cold flagged
coord notifier pending                   # what quiet hours is holding
coord notifier tick --dry-run            # run the predicate, deliver nothing
coord notifier test                      # prove the transport end to end
coord notifier urgent <repo> <issue>     # pierce quiet hours for one drive
coord drive --urgent <repo> <issue>      # ...or declare it at launch
```

`coord notifier test` matters more than it looks: this channel's healthy state is
silence, so a broken transport has **no other symptom**. Run it after any change
to the ntfy server, the tailnet, or the config.

---

## Out of scope

* A dedicated pure-duration/cost surface in `coord status` or the digest
  (#2609 named this as where an "over baseline, still working" observation
  belongs if it's ever wanted). Not built — `over_baseline` today is simply
  gated on silence (confirmed or unconfirmed) rather than rerouted anywhere;
  it still never pages on duration alone while output is confirmed recent.
* Progress pings / periodic "still going" messages — exceptional events only.
* Escalation policies, acknowledgement flows, on-call rotation. One operator.
* Anything that **acts** on a stall (pausing dispatch, killing a worker).
  Detect and tell first; a circuit breaker is a separate, explicitly-decided
  call and conflicts with the advisory-only rule above.
* Replacing the terminal surfaces in #1631 — this is the away-from-terminal
  channel, not a substitute for them.
