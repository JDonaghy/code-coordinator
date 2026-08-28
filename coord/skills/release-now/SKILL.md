---
name: release-now
description: "Use for an on-demand urgent deploy — finding which release actually contains a fix, distrusting `coord --version` on an agent host, forcing --min-behind past the holding threshold, checking headless workers the right way, and verifying the payload (not just the version banner) actually landed."
trigger: Need a specific fix live on the fleet right now, outside the normal daily/20-minute release cadence.
---

# release-now skill

**Trigger:** An operator needs a specific fix live on the fleet **right now**
— a merged PR that must not wait for the next `coord-release-propagate.timer`
fire or the nightly window — or is trying to confirm whether a fix is
actually deployed anywhere.

**Purpose:** Encode six things that had to be discovered by hand during the
2026-08-27 incident (#2850 was in v0.5.257 while the fleet ran v0.5.254),
so the next urgent deploy is a checklist instead of a rediscovery. Sourced
from that incident and #2866; read `docs/AGENT_OPERATIONS.md`'s
"Auto-roll threshold gate" section for the full `--min-behind` mechanism
this skill leans on.

---

## 1. Which release actually contains the fix

A closed issue and a merged PR say **nothing** about which published release
ships the fix — the merge could still be unreleased, or the fleet could be
several releases behind the one that has it. Answer it directly:

```bash
git tag --contains <merge-sha>       # every tag that already includes this commit
```

The lowest of those tags is the first release carrying the fix. #2850's
merge landed in v0.5.257; the fleet was on v0.5.254 — three releases short,
not "already deployed" the way "issue closed" suggests.

## 2. `coord --version` is useless on these hosts

It reports `0+unknown` on a machine running the pinned agent venv (`coord`
resolves to a non-editable install with no importable package version at
that path). Don't trust it as the ground truth for what's running. Read the
wheel's own dist-info instead:

```bash
ls ~/.coord-venv/lib/python*/site-packages/ | grep -i '^coord-'
cat ~/.coord-venv/lib/python*/site-packages/coord-*.dist-info/METADATA | grep '^Version:'
```

`coord release verify` / `coord release history` are the other honest
sources — they read the same lane data this skill's step 5 cross-checks
against, not a CLI banner.

## 3. `--min-behind 1` is mandatory for an urgent roll

`coord release propagate` and `coord release nightly-window` both hold
below `propagation.min_releases_behind` (5 on a typical fleet,
`coordinator.yml`'s `propagation:` block) — **without an explicit override
this is a reported no-op**: `holding: N behind, threshold 5`, exit 0,
nothing touched. An urgent deploy needs the delta to roll on ANY count:

```bash
coord release propagate --min-behind 1 --target vX.Y.Z --verify --rollback-on-red
```

Never omit `--min-behind 1` here just because the daily window "usually"
handles it — the whole point of this skill is that the daily window's own
cadence is too slow for "right now" (see #2866 for why the daily window
itself now packages `--min-behind 1` by default, which does not help an
urgent roll fired outside its schedule).

## 4. `coord sessions --remote` does not see headless workers

Carried over from the `fleet-restart-safety` skill: an urgent
`coord release propagate` restarts every agent, which kills in-flight
headless `claude -p` workers exactly like any other agent restart. Check
**active assignments**, not interactive sessions, before firing:

```bash
curl -s http://<agent-host>:7433/status | python3 -c \
  "import json,sys; print([(a['id'],a['status']) for a in json.load(sys.stdin).get('active',[])])"
# or, fleet-wide:
coord status
```

`coord sessions --remote` coming back empty is not evidence anything is
safe to restart.

## 5. Verify the payload, not the roll

`✓ vX.Y.Z verified` (from `coord release propagate`/`coord release verify`)
means **that version string is installed on that lane** — it does not mean
your specific fix's code is present, imported, or reachable. Grep the venv
directly for a symbol the fix introduced:

```bash
grep -r "SYMBOL_THE_FIX_ADDS" ~/.coord-venv/lib/python*/site-packages/coord/
```

When the fix spans a producer and a consumer, check **both halves** — a
verified version string proves neither side landed correctly on its own.
Example from #2850: `MERGE_LANDED_MARKER` needed to be present in both
`drive.py` and `models.py`, **and** the `is_merge_landed_reason` import
needed to be present in `drive_queue.py` — three greps, not one, before
calling the roll confirmed.

## 6. A stale `roll_pending.json` is a footgun, not cosmetic

`coord drive-queue status` (or the TUI) shows the live #2587 roll-pending
marker; the raw file is `~/.coord/roll_pending.json` on the daemon host.
A marker naming an **older** version than what is currently deployed is
actively dangerous, not harmless clutter — it can get fired (or, under
`coord release nightly-window --dry-run`, at least *proposed*) as a
backwards roll the next time the fleet goes quiescent. #2866 hardens
`coord release nightly-window` against proposing a target the daemon has
already passed, but a marker can still exist for other reasons (a `propagate`
run that deferred, then got superseded by a manual roll). Check it before
AND after an urgent deploy:

```bash
coord drive-queue status               # shows the marker if one is pending
coord drive-queue cancel-roll          # clears it if it's stale/wrong
```

If `nightly-window --dry-run` ever proposes rolling to a version *older*
than what `coord release verify` reports as currently installed, that is
the #2866 footgun — clear the marker by hand rather than letting the next
quiescent window fire it.

## Checklist for an urgent deploy

1. `git tag --contains <sha>` — confirm which release actually has the fix.
2. Read the venv's dist-info (not `coord --version`) on the target host(s)
   to confirm the CURRENT state, before touching anything.
3. `coord status` / agent `/status` — confirm no headless worker is
   mid-assignment on a host you're about to restart.
4. `coord drive-queue status` — check for a stale roll-pending marker
   before you start.
5. `coord release propagate --min-behind 1 --target vX.Y.Z --verify
   --rollback-on-red` (add `--daemon-host`/`--only` as needed).
6. Grep the venv for a symbol the fix introduced — both producer and
   consumer sides if the fix spans more than one file.
7. `coord drive-queue status` again — confirm nothing was left pending
   pointing at a stale target.

## Incident history

- **2026-08-27 (#2850):** the fix was merged and closed, but the fleet was
  three releases behind it (v0.5.254 vs. the v0.5.257 that shipped it).
  Discovering this required `git tag --contains`, not the issue/PR state.
  The subsequent roll needed `--min-behind 1` because the default
  `min_releases_behind=5` reported a silent no-op. Verifying the roll
  required grepping the venv for `MERGE_LANDED_MARKER` /
  `is_merge_landed_reason` — the version banner alone said nothing about
  whether that specific fix was live.
- **2026-08-28 (#2866):** a `roll_pending.json` marker left over from a
  prior day's deferred `propagate` run named a version (0.5.254) the daemon
  had already passed (it was on 0.5.258 by then). `nightly-window --dry-run`
  read it back and proposed rolling the fleet **backwards**. Cleared by
  hand with `coord drive-queue cancel-roll`; `coord release nightly-window`
  itself was subsequently hardened to refuse this specific shape.

## Rules

- Never trust `coord --version` on an agent host — read the venv's
  dist-info or `coord release verify`/`history` instead.
- Always pass `--min-behind 1` explicitly for an on-demand roll — the
  configured `propagation.min_releases_behind` threshold exists for the
  opportunistic 20-minute timer, not for "I need this live now."
- Always verify a payload symbol the fix introduces, not just the version
  string `coord release propagate`/`verify` reports — check every side of a
  producer/consumer fix.
- Check `coord drive-queue status` for a stale roll-pending marker both
  before and after an urgent deploy; a marker naming an older version than
  what's installed is a backwards-roll hazard, not cosmetic state.
- Check active assignments (`coord status` / agent `/status`), never
  `coord sessions --remote`, before restarting anything that could kill a
  headless worker.
