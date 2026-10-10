---
name: run-claude-coordinator
description: Build, run, and drive the claude-coordinator project. Use when asked to start coord-tui, run tests, build the TUI, take a screenshot of the dashboard, verify a TUI change works, or run the coord CLI.
---

This repo has two main deliverables: the **`coord` Python CLI** (coordinator commands)
and the **`coord-tui` Rust TUI binary** (the interactive dashboard). Most PRs touch
the TUI. Drive the TUI via `.claude/skills/run-claude-coordinator/driver.sh` under
tmux; drive the CLI directly with `.venv/bin/coord`.

All paths below are relative to the repo root (`/home/john/src/claude-coordinator/`
or wherever the repo lives).

## Prerequisites

No extra system packages needed on this machine — `tmux`, `rustup`, and `python3`
are already installed. Verify:

```bash
tmux -V          # tmux 3.4
cargo --version  # cargo 1.xx
python3 --version  # 3.12+
```

## Setup

**Python dev install** (required to work on `coord/` source — editable install
so local edits take effect immediately):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/coord --version   # coord, version 0.4.51
```

The system `~/.coord-venv/bin/coord` is the PyPI install used by agents. For
development always use `.venv/bin/coord` so local changes are visible.

## Build

**TUI (Rust):**

```bash
cd tui && cargo build
# binary at: tui/target/debug/coord-tui
```

`cargo build --release` for the optimised binary (slower to build, faster to
run; useful when profiling, not required for TUI logic testing).

**Python CLI:** no build step — `pip install -e .` wires it up.

## Run (agent path) — TUI

The driver wraps `coord-tui` in a tmux session so an agent can send keys and
read the screen without a real terminal. Run all commands from the repo root:

```bash
# Launch TUI in a detached tmux session (waits until BOARD appears, then until
# the data-load spinner clears — returns in ~1-2 seconds on this machine).
.claude/skills/run-claude-coordinator/driver.sh launch

# Read the current screen contents (full 160×48 text grid).
.claude/skills/run-claude-coordinator/driver.sh screen

# Switch views:
.claude/skills/run-claude-coordinator/driver.sh view 1   # Board
.claude/skills/run-claude-coordinator/driver.sh view 2   # Machines
.claude/skills/run-claude-coordinator/driver.sh view 3   # Pipeline
.claude/skills/run-claude-coordinator/driver.sh view 4   # Settings
.claude/skills/run-claude-coordinator/driver.sh view 5   # Terminal
.claude/skills/run-claude-coordinator/driver.sh view 6   # Kanban

# Send an arbitrary key:
.claude/skills/run-claude-coordinator/driver.sh key j    # move down
.claude/skills/run-claude-coordinator/driver.sh key Enter

# Wait until specific text appears (useful after a navigation action):
.claude/skills/run-claude-coordinator/driver.sh wait "Machines" 5

# Quit cleanly (sends 'q', then kills session):
.claude/skills/run-claude-coordinator/driver.sh quit

# Force-kill if hung:
.claude/skills/run-claude-coordinator/driver.sh kill
```

### Key reference

| Key | Action |
|---|---|
| `1` `2` `3` `4` `5` `6` | Switch to Board / Machines / Pipeline / Settings / Terminal / Kanban |
| `j` / `k` | Navigate sidebar list up/down |
| `Enter` | Open/expand selected item |
| `h` / `l` | Tab left/right in detail pane |
| `R` | Refresh board data |
| `L` | Open live-sessions overlay |
| `q` | Quit |

### Ready markers

After `launch`, the data is loaded. To poll for specific content after a
navigation key use `wait`:

```bash
.claude/skills/run-claude-coordinator/driver.sh key 3
.claude/skills/run-claude-coordinator/driver.sh wait "Pipeline" 5
.claude/skills/run-claude-coordinator/driver.sh screen
```

## Run (agent path) — coord CLI

```bash
.venv/bin/coord status
.venv/bin/coord status --freshness
.venv/bin/coord plan --dry-run
.venv/bin/coord --help
```

The CLI talks to the live coordinator via agent HTTP (port 7433) and GitHub.
`--dry-run` on `plan`/`approve`/`assign` is safe to run without triggering
real dispatches.

## Run (human path) — TUI

```bash
tui/target/debug/coord-tui   # opens full-screen TUI in the current terminal, q to quit
```

Only useful in an interactive terminal — no output in a headless bash session.

## Test

**TUI (Rust) — fast, headless, deterministic:**

```bash
cd tui && cargo test
# Expected: ~760 tests pass, ~1s (count grows as coverage is added)
```

These include `TuiDriver` black-box tests that render the full app against an
in-memory `TestBackend` — no tmux or real terminal required.

**Python — full coordinator logic:**

```bash
.venv/bin/pytest tests/ -q
# Expected: ~2470 tests pass, ~2-3 min (count grows as coverage is added)
```

One known warning: `httpx` + `starlette.testclient` deprecation notice. Safe to
ignore — does not fail the suite.

## Gotchas

- **Relative binary path breaks `tmux new-session`** — tmux launches commands
  with HOME as CWD, so relative paths to `coord-tui` silently fail (session
  creates then immediately exits). The driver always resolves an absolute path
  at startup via `cd "$(dirname "$0")/../../.." && pwd`.

- **`loading…` spinner may linger briefly** — the TUI fires a background data
  load thread. `driver.sh launch` waits for the spinner to clear, so by the
  time it returns the board is populated.

- **quadraui path dependency** — `tui/Cargo.toml` depends on `quadraui` via a
  relative path (`../../quadraui/quadraui`). If you need a non-default
  quadraui branch, check it out in `~/src/quadraui` before building and
  restore afterwards.

- **Dev vs. system `coord`** — `~/.coord-venv/bin/coord` is the PyPI install
  (used by agents); `.venv/bin/coord` is the dev install. Local edits to
  `coord/` only affect `.venv/bin/coord`. The warning
  `"coord CLI is running from a non-editable install"` appears when
  `coord` is invoked without the dev venv.

## Troubleshooting

- **`can't find pane: coord-tui-driver`** in the driver: the session exited.
  Check `~/.coord/coord-tui-panic.log` for a panic. Rebuild with
  `cd tui && cargo build` and try again.

- **`timeout` fires on `launch`**: the TUI didn't render `BOARD` within 10s —
  usually means the binary path is wrong or the binary crashed. Run manually:
  `tmux new-session -d -s dbg -x 160 -y 48 /path/to/coord-tui && sleep 2 && tmux capture-pane -t dbg -p`.

- **Python tests fail with `ModuleNotFoundError`**: the dev venv isn't activated.
  Always prefix pytest with `.venv/bin/pytest`, not a bare `pytest`.
