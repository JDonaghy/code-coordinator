---
name: fleet-restart-safety
description: "Use before restarting coord-agent/coord-serve or editing coordinator.yml on a live fleet — the two restarts kill different things (headless workers vs. interactive finalize) and neither warns you the way you'd expect."
trigger: About to restart coord-agent (incl. `coord agent update`) or coord-serve, or edit coordinator.yml/coordinator.remote.yml on a live fleet.
---

# fleet-restart-safety skill

**Trigger:** About to restart `coord-agent` (including `coord agent update`)
or `coord-serve`, or about to edit `coordinator.yml` / `coordinator.remote.yml`
on a live fleet.

**Purpose:** The two restarts kill different things, and neither warns you the
way you'd expect. Sourced from `docs/OPERATING_GOTCHAS.md` #2 and #8 — read
those in full for the incident history; this is the pre-action checklist.

---

## Before restarting `coord-agent` / running `coord agent update`

This kills **headless workers** — a `claude -p` worker is a subprocess of
`coord-agent`. Restart the agent and the worker dies mid-task, its assignment
flips to `failed`, and any uncommitted work in its worktree is stranded.

**`coord sessions --remote` will NOT warn you here** — it only lists
interactive tmux sessions. Headless workers are invisible to it. (This is
exactly how a v0.4.77 deploy destroyed #1400's in-flight fix worker on
elitebook: `coord sessions --remote` said "No running interactive sessions"
and the update ran anyway.)

Check the right thing instead — **active assignments**, not sessions:

```bash
# per machine — the authoritative check
curl -s http://<agent-host>:7433/status | python3 -c \
  "import json,sys; print([(a['id'],a['status']) for a in json.load(sys.stdin).get('active',[])])"

# or from the board, for every machine at once
coord status                 # look for running assignments, not just machine health
```

Restart only machines with no active assignment. `coord agent update
--machine <name>` lets you update the idle ones and come back for the busy
one.

## Before restarting `coord-serve`

This breaks **interactive finalize** — an interactive session runs a finalize
backstop on exit that POSTs to the daemon to record the branch and terminal
status. Restart the daemon while one is live and that finalize fails,
silently losing the branch, the verdict, or both.

The right check here is the opposite of the one above:

```bash
coord sessions --remote        # MUST be empty before this restart
ssh <daemon-host> 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-serve'
```

## Before editing `coordinator.yml` on a live fleet

On a thin client, `coord config` resolves to `~/.coord/coordinator.remote.yml`
— **that file is a cache**, re-fetched from the daemon's `GET /config` on
nearly every thin-client command and overwritten wholesale. Edit it directly
and the change disappears within seconds — and the command you'd use to
verify the edit is itself a re-fetch, so the disappearance looks like nothing
happened.

Fleet config is edited on the **daemon host**, and not by editing
`~/.coord/coordinator.yml` directly there either — that path is a symlink into
the `coord-settings` checkout, and most editors write-and-rename over
symlinks, silently replacing it with a disconnected regular file. Edit the
checkout itself:

```bash
coord sessions --remote        # MUST be empty — restart breaks interactive finalize
ssh <daemon-host> 'vi ~/src/coord-settings/coord/coordinator.yml'
ssh <daemon-host> 'git -C ~/src/coord-settings commit -am "..." && git -C ~/src/coord-settings push'
ssh <daemon-host> 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-serve'
coord config                   # re-caches from the daemon; new value proves both the edit and the reload
```

The daemon does not hot-reload — without the restart, the file is changed and
nothing uses it.

## Summary table

| Restarting | Kills | Check with |
|---|---|---|
| `coord-agent` (incl. `coord agent update`) | headless workers (subprocesses) | agent `/status` active list, or `coord status` |
| `coord-serve` | interactive finalize (branch/verdict write) | `coord sessions --remote` |

A full deploy needs **both** checks, and often waiting: a machine running a
worker can't be agent-updated, and a fleet with a live interactive session
can't have its daemon restarted.

## Rules

- Never restart `coord-agent` / run `coord agent update --all` without
  checking active assignments first — `coord sessions --remote` coming back
  empty is not sufficient evidence.
- Never edit `~/.coord/coordinator.yml` / `coordinator.remote.yml` directly on
  a thin client or via the daemon-host symlink and expect it to stick — edit
  the `coord-settings` checkout and push.
