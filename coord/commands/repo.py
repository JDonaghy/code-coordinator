"""``coord repo add`` / ``coord repo create`` / ``coord repo doctor`` —
creating and onboarding a repo, and checking that it actually happened
(#2220, #2747).

Onboarding a repo is ~14 steps across five layers (config, three machines,
GitHub, the repo's own contents, the graph) with no command, no checklist and
no verifier — reconstructed from memory each time, and **every layer fails
silently**. ``stick-demo`` sat two-thirds onboarded until ``stick-demo#1`` died
of it, having burned both drive attempts, while ``coord config``, ``coord
status`` and ``coord assign --dry-run`` all reported the dispatch as fine.

Of the three commands here **``doctor`` is the one that matters most**. A
runbook is the weakest available answer and this codebase already has the
proof: ``docs/GRAPHIFY_SETUP.md`` is exactly that shape, and graphify has
still fallen by the wayside more than once because nothing checks it. What
holds is a checkable gate — ``coord diagnose --graph``, ``coord doctor``,
``coord release verify`` all work because they answer *is it true right now*,
not *did you remember*.

``coord repo add`` therefore does only the mechanical, safely-automatable parts
and **prints the residue it deliberately did not do**, rather than pretending
completeness. The parts it skips (clone, agent restart, CLAUDE.md, CI workflow)
are the ones a wrong guess makes worse, and they are exactly what ``coord repo
doctor`` then verifies.

``coord repo add`` also requires the repo to already exist — the only way to
make one was a raw ``gh repo create``, exactly the habit the backend-agnostic
forge seam (``docs/FORGE_MIGRATION.md``) exists to stop, and one workers can't
do at all (``gh`` is deny-listed for them). ``coord repo create`` (IL-1,
#2747) closes that gap: it creates the remote through the seam
(``coord.github_ops``, never a raw ``gh`` call site outside it) and seeds the
three residue items that are actual traps rather than chores — no CI on
``pull_request`` blocks every merge forever, no CLAUDE.md makes the review
gate structurally empty, no ``.githooks/`` leaves every worktree graph-blind
— then chains into ``coord repo add`` for the rest. What's left afterward is
genuinely 4 items a human must still do (down from ``add``'s 8): clone on
each machine, commit+push the coordinator.yml edit, ``git pull`` the
coord-settings checkout on each machine that serves the repo (not just the
daemon host — the doctor-checked ``machines.agent_repo_skew`` remedy), and
``coord repo doctor --fix``.

``--for-submission SUBMISSION_ID`` (#2861) closes three of those four for the
**portal** case — the case that motivated the epic. Measured doing genesis for
SUB-1EA1D3 on 2026-08-27, ``coord repo create`` was one command and getting
from there to "pullable into a decomposition session" took **nine more
motions**, two of them recovery from a footgun the command itself set up: the
coord-settings checkout was five commits behind origin, so the entry landed on
a stale base and the diff looked perfectly clean. So this module now also
carries:

* an **unconditional stale-checkout guard** (:func:`_guard_settings_fresh`) on
  plain ``repo add``/``repo create`` too — the highest-value line in #2861 and
  independent of everything else; and
* ``--for-submission``, which resolves the submission's ``project_id``, writes
  the ``portal.project_repos`` mapping, commits + pushes coord-settings,
  ``git pull``s it on every machine serving the repo (reporting per machine,
  never aborting the sweep on one host), reconciles each machine's live
  ``~/.coord/coordinator.yml``, and finishes with ``coord repo doctor --fix``.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config

# Reuse the fleet's own definition of where the tracked config lives (#1779) —
# `~/src/coord-settings/coord/coordinator.yml`, NOT the `~/.coord/` symlink
# (#1832): edits must land in the checkout so they can be committed, reviewed
# and pulled onto the daemon host.
from coord.fleet_config_health import (
    TRACKED_CONFIG_REL,
    default_live_config_path,
    default_settings_dir,
)


@click.group("repo", help="Add a repo to the fleet, and verify it is actually onboarded.")
def repo_group() -> None:
    """Repo onboarding (#2220)."""


#: Shared `--for-submission` help text — one definition so `coord repo add`
#: and `coord repo create` cannot drift (#2861).
_FOR_SUBMISSION_HELP = (
    "Also map this portal submission's project to the new repo, then commit, "
    "push and distribute coordinator.yml to every machine serving it, and "
    "finish with `coord repo doctor --fix` (#2861)."
)


# ── Seed content for a freshly created repo (IL-1, #2747) ───────────────────
#
# `coord repo add` prints 8 residue items a human must do by hand; three of
# them are traps, not chores (see the module docstring's #2747 addendum
# below `coord repo create`). This section is what fills those three in
# automatically: a CLAUDE.md skeleton, a CI workflow that triggers on
# `pull_request` (so `expects_checks()` never reads "CI exists" while zero
# checks arrive — the trap that blocks EVERY merge in a repo forever), and
# the `.githooks/` port (so worktrees get a linked graph from commit one
# instead of every worker silently falling back to grep).
#
# The `.githooks/*` bodies are copied verbatim from THIS repo's own
# `.githooks/` — they are generic (no code-coordinator-specific paths or
# names, see docs/GRAPHIFY_SETUP.md) — and embedded here as string literals
# rather than read off disk at runtime: coord ships to worker machines as a
# PyPI install (docs/AGENT_OPERATIONS.md's `~/.coord-venv` invariant), which
# has no `.githooks/` sibling directory to read; only this source checkout
# does. Nothing currently checks the two copies don't drift — if this repo's
# own `.githooks/` changes, update these to match.

_GITHOOKS_LIB_SH = """\
# Shared helpers for the versioned hooks in this directory.  Sourced, not run.
#
# WHY THESE HOOKS EXIST AT ALL: setting `core.hooksPath` REPLACES `.git/hooks`
# wholesale — git stops looking there entirely.  graphify installs post-commit,
# post-checkout, and post-merge into `$GIT_COMMON_DIR/hooks/`, so every one of
# them needs a counterpart here or it is silently disabled.  (Shipping only
# post-checkout killed graphify's commit/merge rebuilds on the operator box for
# about an hour — caught by `coord diagnose --graph` reporting the repo's own
# graph STALE right after a merge.)
#
# Each hook here is a thin shim: skip in linked worktrees, otherwise hand off
# to the machine-local graphify hook, which pins an absolute interpreter path
# and therefore must never be committed.

# Absolute path, whether git handed us a relative one or not.
gfy_abs() {
    case "$1" in
        /*) printf '%s\\n' "$1" ;;
        *)  printf '%s\\n' "$PWD/$1" ;;
    esac
}

gfy_common_dir() {
    gfy_abs "$(git rev-parse --git-common-dir 2>/dev/null || echo .git)"
}

# True in a linked worktree (its per-worktree git dir differs from the common one).
gfy_is_linked_worktree() {
    _gd=$(gfy_abs "$(git rev-parse --git-dir 2>/dev/null || echo .git)")
    [ "$_gd" != "$(gfy_common_dir)" ]
}

# Hand off to the machine-local hook of the same name, if present.
gfy_chain() {
    _name=$1
    shift
    _local="$(gfy_common_dir)/hooks/$_name"
    if [ -x "$_local" ]; then
        exec "$_local" "$@"
    fi
    exit 0
}
"""

_GITHOOKS_POST_CHECKOUT = """\
#!/bin/sh
# Versioned post-checkout hook — enabled per machine with:
#
#     git config core.hooksPath .githooks
#
# Purpose: make the graphify knowledge graph usable from a *linked worktree*.
#
# `graphify-out/` is gitignored by design (only `graphify-out/.gitignore` is
# tracked — the graph is multi-MB, rewritten on every commit, and would
# conflict across parallel worker branches).  So `git worktree add` gives a
# worktree an EMPTY `graphify-out/`, and `graphify query` — which resolves
# `graphify-out/graph.json` strictly relative to cwd, with no upward walk and
# no `--graph` override — fails there.  Every agent working in a worktree is
# graph-blind.
#
# Git runs this hook on `git worktree add` (verified: cwd = the new worktree,
# $3 = 1), which is why the bootstrap lives here rather than in coord's
# dispatch code: one implementation covers coord's remote `worktree add` call
# sites, Claude Code's `.claude/worktrees/`, review worktrees, and anything
# created by hand — on every machine, with no creator-side changes.
#
# The fix is symlinking the base checkout's graph *contents* into the
# worktree's `graphify-out/` — never replacing the directory itself.  A
# worker branch differs from the base by a handful of files, so the base
# graph is the right answer for "where is X handled / what calls this"
# navigation.  It is NOT the right answer for "did my change land" — the
# linked graph reflects the base checkout's HEAD, not the worktree's edits.
#
# #1617: an earlier version of this hook replaced the whole `graphify-out/`
# directory with a symlink to the base checkout.  `git worktree add`
# materialises `graphify-out/` with only the tracked `.gitignore` checked
# out (everything else in the directory is untracked-and-ignored), and
# `rm -rf graphify-out` before `ln -sfn` deleted that tracked file out from
# under git — leaving `git status` showing a deleted tracked file *and* an
# untracked, machine-local, absolute-path symlink.  Every worktree's own
# rescue-commit machinery then committed both.  `graphify-out/.gitignore` is
# `*` / `!.gitignore`, so anything placed *inside* the directory is already
# invisible to git for free — the fix is to keep the directory (and its
# tracked `.gitignore`) and symlink each entry of the base graph into it
# instead of swapping the directory out from under git.
#
# See .githooks/_lib.sh for why every graphify hook needs a shim here.

set -u
. "$(dirname "$0")/_lib.sh"

# $3 == 1 means a branch checkout (not a file checkout).  `git worktree add`
# sets it.  Anything else is not our business.
if [ "${3:-0}" != "1" ]; then
    exit 0
fi

if gfy_is_linked_worktree; then
    # The base checkout is the parent of the common git dir.
    _base=$(CDPATH= cd -- "$(dirname -- "$(gfy_common_dir)")" 2>/dev/null && pwd) || _base=""
    if [ -n "$_base" ] && [ -f "$_base/graphify-out/graph.json" ]; then
        # Only bootstrap when graph.json here is absent, or is itself a
        # symlink from a previous run of this hook (idempotent re-link on a
        # later checkout in the same worktree).  If a REAL graph was built
        # in this worktree, leave it alone.
        if [ ! -e graphify-out/graph.json ] || [ -L graphify-out/graph.json ]; then
            # `git worktree add` already checked out the tracked
            # graphify-out/.gitignore, materialising the directory — never
            # remove it, only add symlinks alongside it.
            mkdir -p graphify-out 2>/dev/null
            _linked=0
            for _entry in "$_base"/graphify-out/* "$_base"/graphify-out/.[!.]*; do
                [ -e "$_entry" ] || continue
                _name=$(basename -- "$_entry")
                # .gitignore is the tracked file that keeps this directory
                # self-ignoring — never shadow it with a symlink to the
                # base's copy.
                [ "$_name" = ".gitignore" ] && continue
                ln -sfn "$_entry" "graphify-out/$_name" 2>/dev/null && _linked=1
            done
            [ "$_linked" = "1" ] &&
                echo "[graphify] linked graphify-out/* -> $_base/graphify-out/* (read-only base graph)"
        fi
    fi
    # Never rebuild in a linked worktree.  Two reasons, both load-bearing:
    #   1. graphify-out/graph.json (and friends) are symlinks to the SHARED
    #      base graph — a rebuild here would overwrite it from a
    #      feature-branch tree.
    #   2. A worktree can be reaped mid-rebuild, which is how the graphify
    #      hook's own "burns a full AST pass and then dies with ENOENT"
    #      comment came to be.
    exit 0
fi

gfy_chain post-checkout "$@"
"""

_GITHOOKS_POST_COMMIT = """\
#!/bin/sh
# Versioned post-commit shim.
#
# core.hooksPath REPLACES .git/hooks wholesale, so without this file
# graphify's post-commit rebuild is silently DISABLED on any machine that opts
# into the versioned hooks.  See .githooks/_lib.sh.
#
# Linked worktrees never rebuild: graphify-out there is a symlink to the base
# checkout's SHARED graph, so a rebuild would overwrite it from a
# feature-branch tree — and a worktree can be reaped mid-rebuild.

set -u
. "$(dirname "$0")/_lib.sh"

if gfy_is_linked_worktree; then
    exit 0
fi

gfy_chain post-commit "$@"
"""

_GITHOOKS_POST_MERGE = """\
#!/bin/sh
# Versioned post-merge shim.
#
# core.hooksPath REPLACES .git/hooks wholesale, so without this file
# graphify's post-merge rebuild is silently DISABLED on any machine that opts
# into the versioned hooks.  See .githooks/_lib.sh.
#
# Linked worktrees never rebuild: graphify-out there is a symlink to the base
# checkout's SHARED graph, so a rebuild would overwrite it from a
# feature-branch tree — and a worktree can be reaped mid-rebuild.

set -u
. "$(dirname "$0")/_lib.sh"

if gfy_is_linked_worktree; then
    exit 0
fi

gfy_chain post-merge "$@"
"""

# #3037: the seeded `post-checkout` hook above documents, as a design
# invariant, that `graphify-out/.gitignore` exists and is tracked ("only
# graphify-out/.gitignore is tracked ... `git worktree add` materialises
# graphify-out/ with only the tracked .gitignore checked out"). But until
# this constant, `_seed_files` never actually wrote that file — a repo
# created by `coord repo create` shipped a hook whose contract was false
# from the first commit. The self-ignoring `*` / `!.gitignore` form is
# seeded (not a root-.gitignore line) because it travels with the directory
# and reaches every clone, including ones that never see the root file
# rewritten. See `coord.repo_onboard` for the doctor-side check, which
# accepts this form OR a root `.gitignore` entry — both are correct, and are
# the two shapes live across the fleet.
_GRAPHIFY_OUT_GITIGNORE = """\
# graphify-out/ is a regenerable, machine-local cache rebuilt by the
# post-commit / post-checkout git hooks — we do NOT commit it:
#   - every machine rebuilds locally (one-time `/graphify` seed, then free
#     AST refresh on each commit), so sharing the built copy is redundant
#   - graph.json is multi-MB and rewritten every commit -> history bloat and
#     guaranteed merge conflicts across parallel worker branches
# Only this .gitignore is tracked, so the rule reaches every clone.
*
!.gitignore
"""

# `--template`/per-stack default for the seeded CI workflow (#2747's
# proposal point 2). `generic` is the default and the only one guaranteed to
# report a GREEN check on a repo with no application code yet — `python`/
# `node` run a real build+test command and are meant for a repo whose stack
# IS decided at creation time (a red first check on an empty repo is a worse
# start than an honest placeholder).
_CI_TEMPLATES: dict[str, str] = {
    "generic": """\
name: CI
on:
  pull_request:
jobs:
  placeholder:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          echo "no build/test command configured yet (coord repo create --template generic)."
          echo "this workflow exists so expects_checks() sees a real, reporting check —"
          echo "replace it with the repo's real CI once the stack is decided."
""",
    "python": """\
name: CI
on:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: pytest
""",
    "node": """\
name: CI
on:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: npm ci
      - run: npm test --if-present
""",
}


# #2748 (IL-2): per-stack `acceptance.drivers.<repo>` entry, keyed by the
# same ``--template`` the CI workflow already uses — the stack decision that
# picks a CI template is the SAME decision that picks an oracle-loop driver
# (`coord.acceptance_drivers.SUPPORTED_KINDS`), so deriving one from the
# other closes IL-2's residue item 6 instead of leaving it as a step nobody
# performs. ``generic`` has no entry (the stack isn't decided yet — a driver
# would be as much of a guess as the CI template deliberately isn't). Each
# ``run:`` uses the ``{ms}`` template (:func:`coord.acceptance_drivers.
# render_run_command`) so ``coord acceptance run --issue N`` scopes to one
# milestone's slice, matching every hand-authored driver in this fleet's own
# ``coordinator.yml``.
_ACCEPTANCE_DRIVER_TEMPLATES: dict[str, dict[str, str]] = {
    "python": {
        "kind": "cli-pytest",
        "run": "pytest tests/acceptance/{ms}",
        "mock": "*.out",
        "capability": "python",
    },
    "node": {
        "kind": "web-playwright",
        "run": "npx playwright test tests/acceptance/{ms}",
        "setup": "npm ci",
        "mock": "*.html",
        "capability": "browser",
    },
}


def _render_claude_md_skeleton(name: str) -> str:
    """A minimal ``CLAUDE.md`` for a just-created repo (#2747).

    Deliberately a skeleton, not a guess: this repo's stack is explicitly out
    of scope for ``coord repo create`` (IL-4 / the intake session decides
    it). But an ABSENT ``CLAUDE.md`` is worse than an empty one — the
    adversarial review prompt is assembled from it, so a repo with none
    enforces nothing while reading as covered. Each ``TODO`` names exactly
    what to fill in and why it matters downstream.
    """
    return f"""\
# {name}

TODO: one paragraph on what this repo is and does.

## Stack

TODO: language(s), framework(s), package manager. Undecided as of
`coord repo create` — this file is what the adversarial review prompt and
the Test agent are assembled from, so fill this in before the first real
feature PR lands, or reviews on this repo enforce nothing.

## Development

TODO: how to install dependencies and run the app locally.

## Testing

TODO: the test command, and where tests live. Every PR that changes
user-visible behavior should ship a black-box test that drives the running
app and asserts on its output, not just unit tests on internals.

## Conventions

TODO: language version, formatter/linter, commit message style.
"""


def _seed_files(name: str, ci_template: str) -> list[tuple[str, str, bool]]:
    """The ``(path, content, executable)`` triples :func:`coord.github_ops.
    create_commit_with_files` seeds into a freshly created repo (#2747):
    ``CLAUDE.md``, a CI workflow that triggers on ``pull_request``, the
    ``.githooks/`` port, and ``graphify-out/.gitignore`` (#3037 — the seeded
    ``post-checkout`` hook depends on that file existing and tracked; without
    it the hook's own documented invariant is false from the first commit).
    The three ``.githooks/*`` shims are executable — everything else is a
    plain file.
    """
    return [
        ("CLAUDE.md", _render_claude_md_skeleton(name), False),
        (".github/workflows/ci.yml", _CI_TEMPLATES[ci_template], False),
        (".githooks/_lib.sh", _GITHOOKS_LIB_SH, False),
        (".githooks/post-checkout", _GITHOOKS_POST_CHECKOUT, True),
        (".githooks/post-commit", _GITHOOKS_POST_COMMIT, True),
        (".githooks/post-merge", _GITHOOKS_POST_MERGE, True),
        ("graphify-out/.gitignore", _GRAPHIFY_OUT_GITIGNORE, False),
    ]


# ── #2861, step 1: refuse to write onto a stale coord-settings checkout ──────
#
# Measured on the live fleet 2026-08-27: `coord repo create` wrote a repos[]
# entry into a coord-settings checkout that was FIVE commits behind origin.
# The resulting diff looked perfectly clean — nothing in `coord repo add` /
# `coord repo create` had ever compared the checkout to its upstream — and the
# recovery (`git checkout --`, `git pull`, re-run the config half) cost two of
# the nine motions the whole issue exists to collapse. This guard is the
# highest-value line in #2861 and is deliberately unconditional: it runs for
# plain `coord repo add` too, not only for `--for-submission`.
#
# It fetches, because the whole failure mode is "the remote moved and nobody
# here noticed" — `coord.fleet_config_health.config_provenance` deliberately
# never fetches (it is a read-only diagnose sweep), so it cannot serve as the
# gate on a WRITE.


@dataclass
class _SettingsFreshness:
    """How the config checkout being written to compares to its upstream."""

    checkout: Path | None = None
    behind: int = 0
    ahead: int = 0
    upstream: str | None = None
    #: Set when the comparison could not be made at all (no checkout, no
    #: upstream ref, unparseable `git rev-list`). Never a refusal — an
    #: unanswerable question is not a "yes" (see `_guard_settings_fresh`).
    unknown_reason: str | None = None
    #: Set when `git fetch` itself failed (offline, no credentials). The
    #: comparison still runs against the existing remote-tracking ref, which
    #: may itself be stale — reported so an operator can tell the difference.
    fetch_error: str | None = None


def _git(cwd: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess | None:
    """Best-effort ``git`` in *cwd*; ``None`` when git could not be run at all."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def _git_toplevel(path: Path) -> Path | None:
    """The git checkout *path* lives in, or ``None`` if it is not in one."""
    start = path if path.is_dir() else path.parent
    if not start.exists():
        return None
    result = _git(start, "rev-parse", "--show-toplevel", timeout=10.0)
    if result is None or result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


def _settings_checkout_of(target: Path) -> Path | None:
    """The coord-settings checkout *target* is the tracked config OF.

    ``None`` when *target* is not a git checkout's ``coord/coordinator.yml``
    — a throwaway file, a dev ``./coordinator.yml``, or anything outside a
    repo. See :func:`_guard_settings_fresh` for why this is deliberately
    narrower than "is it in a git checkout at all".
    """
    checkout = _git_toplevel(target)
    if checkout is None:
        return None
    try:
        same = (checkout / TRACKED_CONFIG_REL).resolve() == target.resolve()
    except OSError:  # pragma: no cover — resolve() on a broken mount
        return None
    return checkout if same else None


def _settings_freshness(checkout: Path, *, fetch: bool = True) -> _SettingsFreshness:
    """Compare *checkout* to its upstream, fetching first.

    Read-only: a fetch updates remote-tracking refs, never the working tree.
    """
    fresh = _SettingsFreshness(checkout=checkout)

    if fetch:
        result = _git(checkout, "fetch", "--quiet", timeout=60.0)
        if result is None:
            fresh.fetch_error = "`git fetch` could not be run"
        elif result.returncode != 0:
            fresh.fetch_error = (
                (result.stderr or result.stdout or "").strip().splitlines()[-1:]
                or ["`git fetch` failed"]
            )[0]

    upstream = _git(checkout, "rev-parse", "--abbrev-ref", "@{upstream}", timeout=10.0)
    if upstream is None or upstream.returncode != 0:
        fresh.unknown_reason = "no upstream tracking ref configured"
        return fresh
    fresh.upstream = upstream.stdout.strip()

    counts = _git(
        checkout, "rev-list", "--left-right", "--count", f"{fresh.upstream}...HEAD",
        timeout=15.0,
    )
    if counts is None or counts.returncode != 0:
        fresh.unknown_reason = f"could not compare HEAD to {fresh.upstream}"
        return fresh
    parts = counts.stdout.split()
    if len(parts) != 2:
        fresh.unknown_reason = (
            f"unexpected `git rev-list` output comparing HEAD to {fresh.upstream}"
        )
        return fresh
    try:
        fresh.behind, fresh.ahead = int(parts[0]), int(parts[1])
    except ValueError:
        fresh.unknown_reason = (
            f"unexpected `git rev-list` output comparing HEAD to {fresh.upstream}"
        )
    return fresh


def _guard_settings_fresh(target: Path, *, enabled: bool) -> None:
    """Refuse to write *target* when its checkout is behind its upstream.

    Keyed off *target*'s own git checkout rather than "is this the default
    coord-settings path", so an explicit ``--config`` pointing INTO a
    coord-settings checkout (an operator habit, and how #2861's live incident
    was re-run) is guarded exactly the same — including a checkout somewhere
    other than ``$COORD_SETTINGS_DIR``.

    Narrowed to a target that IS its checkout's ``coord/coordinator.yml``
    (:data:`TRACKED_CONFIG_REL`), i.e. the coord-settings layout. Without
    that, ``coord repo add --config ./coordinator.yml`` run from any source
    checkout would refuse whenever *that unrelated repo* was behind its own
    origin — a guaranteed false positive with a confusing message, on a file
    whose freshness has nothing to do with the fleet's config.

    Deliberately NOT a refusal when the comparison is merely unanswerable
    (no upstream, no network, git unavailable): failing closed there would
    wedge repo onboarding on an offline machine, and "unknown" is not
    "behind". Those states print and continue.
    """
    if not enabled:
        click.echo(
            "⚠ --skip-freshness-check: not comparing the config checkout to its "
            "upstream. An entry written onto a stale base looks clean in the "
            "diff and is only found later (#2861).",
            err=True,
        )
        return

    checkout = _settings_checkout_of(target)
    if checkout is None:
        return

    fresh = _settings_freshness(checkout)
    if fresh.fetch_error:
        click.echo(
            f"⚠ could not `git fetch` in {checkout} ({fresh.fetch_error}) — "
            "comparing against the existing remote-tracking ref, which may "
            "itself be stale.",
            err=True,
        )
    if fresh.unknown_reason:
        click.echo(
            f"? freshness of {checkout} unknown — {fresh.unknown_reason}. "
            "Writing anyway.",
            err=True,
        )
        return
    if fresh.behind:
        raise click.ClickException(
            f"refusing to write: {checkout} is {fresh.behind} commit(s) behind "
            f"{fresh.upstream}. The entry would land on a STALE base and the "
            "diff would look perfectly clean (#2861 — this cost two recovery "
            f"motions on the live fleet). Nothing was written.\n"
            f"  Fix: git -C {checkout} pull --ff-only\n"
            "  Then re-run. Override (rarely right): --skip-freshness-check."
        )
    if fresh.ahead:
        click.echo(
            f"⚠ {checkout} is {fresh.ahead} commit(s) ahead of {fresh.upstream} "
            "— local config commit(s) not yet pushed. Not a refusal, but the "
            "fleet is not running them yet.",
            err=True,
        )


def _resolve_write_target(explicit: Path | None) -> Path:
    """Where ``coord repo add`` writes.

    Defaults to the coord-settings checkout's tracked file rather than
    whatever ``resolve_config_path()`` returns, because the live
    ``~/.coord/coordinator.yml`` is a *symlink* into that checkout: writing
    through the symlink produces an untracked, uncommitted change to the
    fleet's governing config with nothing to review and nothing to pull
    (#1779/#1832). Refuses rather than falling back when the checkout is
    absent — a machine with no coord-settings checkout is deliberately not
    allowed to edit the fleet's config.
    """
    if explicit is not None:
        return explicit
    tracked = default_settings_dir() / TRACKED_CONFIG_REL
    if not tracked.exists():
        raise click.ClickException(
            f"no coord-settings checkout at {default_settings_dir()} (expected "
            f"{tracked}). `coord repo add` writes the TRACKED config so the "
            "change can be committed, reviewed and pulled — it will not write "
            "through the ~/.coord symlink (#1832). Clone coord-settings, set "
            "$COORD_SETTINGS_DIR, or pass --config explicitly."
        )
    return tracked


@repo_group.command(
    "add",
    help=(
        "Write a new repo's coordinator.yml entry into the coord-settings "
        "checkout, add it to the named machines, create the `coord` and tier "
        "labels — then print the residue it deliberately did NOT do."
    ),
)
@click.argument("name")
@click.option("--github", "github_slug", required=True, help="owner/repo on GitHub.")
@click.option(
    "--machines", "machines_csv", default=None,
    help="Comma-separated machine names that should serve this repo.",
)
@click.option(
    "--repo-path", "repo_path_tmpl", default=None,
    help=(
        "Path to the clone on each machine. Default: ~/src/<name> — the fleet "
        "convention, and the worker WORKTREE BASE, not a convenience checkout."
    ),
)
@click.option(
    "--default-branch", "default_branch_override", default=None,
    help=(
        "Override the default branch instead of reading the real one from "
        "GitHub. Use only when GitHub is unreachable — a default_branch that "
        "disagrees with the repo's real default silently routes worker PRs to "
        "the wrong base."
    ),
)
@click.option("--build-command", default=None, help="repos[].build_command.")
@click.option("--test-command", default=None, help="repos[].test_command.")
@click.option(
    "--labels/--no-labels", "do_labels", default=True, show_default=True,
    help="Create the `coord` and tier:small/tier:large labels on GitHub.",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Print the edited config and the residue without writing anything.",
)
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), default=None,
    help="coordinator.yml to edit. Default: the coord-settings tracked file.",
)
@click.option("--for-submission", "submission_id", default=None, help=_FOR_SUBMISSION_HELP)
@click.option(
    "--refresh-live-config/--no-refresh-live-config", "refresh_live",
    default=True, show_default=True,
    help=(
        "During --for-submission's distribution step, overwrite a machine's "
        "live ~/.coord/coordinator.yml when it is a REGULAR FILE that diverges "
        "from the checkout (a backup is written first)."
    ),
)
@click.option(
    "--skip-freshness-check", "skip_freshness", is_flag=True, default=False,
    help=(
        "Do NOT compare the config checkout to its upstream before writing. "
        "Rarely right: an entry written onto a stale base looks clean in the "
        "diff (#2861)."
    ),
)
@click.option(
    "--ssh-timeout", default=180.0, show_default=True, type=float,
    help="Per-machine timeout for the --for-submission distribution step.",
)
def repo_add(  # noqa: PLR0913 — one option per thing the command can set
    name: str,
    github_slug: str,
    machines_csv: str | None,
    repo_path_tmpl: str | None,
    default_branch_override: str | None,
    build_command: str | None,
    test_command: str | None,
    do_labels: bool,  # noqa: FBT001
    dry_run: bool,  # noqa: FBT001
    config_path: Path | None,
    submission_id: str | None,
    refresh_live: bool,  # noqa: FBT001
    skip_freshness: bool,  # noqa: FBT001
    ssh_timeout: float,
) -> None:
    # #2861: an unknown submission id / an already-mapped project must refuse
    # BEFORE the repos[] entry is written, not after.
    if submission_id and not dry_run:
        _plan_for_submission(
            target=_resolve_write_target(config_path),
            submission_id=submission_id,
            repo_name=name,
            machines=[m.strip() for m in (machines_csv or "").split(",") if m.strip()],
        )
    result = _do_repo_add_core(
        name=name,
        github_slug=github_slug,
        machines_csv=machines_csv,
        repo_path_tmpl=repo_path_tmpl,
        default_branch_override=default_branch_override,
        build_command=build_command,
        test_command=test_command,
        do_labels=do_labels,
        dry_run=dry_run,
        config_path=config_path,
        check_freshness=not skip_freshness,
    )
    if submission_id and dry_run:
        _print_for_submission_plan(
            name=name, submission_id=submission_id, machines=result["machines"],
            target=result["target"], refresh_live=refresh_live,
        )
        return
    if submission_id:
        from coord.config import load as load_config  # noqa: PLC0415

        _run_for_submission(
            name=name, submission_id=submission_id, target=result["target"],
            machines=result["machines"], cfg=load_config(result["target"]),
            refresh_live=refresh_live, ssh_timeout=ssh_timeout, run_doctor=True,
        )
        # `repo add` onboards an EXISTING repo, so unlike `repo create` it
        # seeded nothing — the residue items --for-submission does not cover
        # are still outstanding and must not be silently dropped.
        click.echo("")
        click.echo(
            "NOT DONE — --for-submission covered the config, the push, the "
            "distribution and the doctor. Still outstanding, and unchanged "
            "from `coord repo add`'s usual residue:"
        )
        click.echo(
            f"  · clone the repo to {repo_path_tmpl or f'~/src/{name}'} on "
            f"{', '.join(result['machines']) or '<each machine>'} "
            "— the worker WORKTREE BASE"
        )
        click.echo(
            "  · CLAUDE.md, a `pull_request`-triggered CI workflow, the "
            "`.githooks/` port, and graphify-out/.gitignore in the repo "
            "itself — `coord repo create` seeds these; `coord repo add` "
            "cannot, since the repo already existed"
        )
        click.echo(
            "  · `test_command`/`ci_command`, `smoke_tests.capability_rules`, "
            "and (if it joins the oracle loop) `acceptance.drivers`"
        )
        return
    _print_add_residue(
        target=result["target"], machines=result["machines"],
        repo_path_tmpl=repo_path_tmpl, name=name,
    )


def _do_repo_add_core(  # noqa: PLR0913 — one option per thing the caller can set
    *,
    name: str,
    github_slug: str,
    machines_csv: str | None,
    repo_path_tmpl: str | None,
    default_branch_override: str | None,
    build_command: str | None,
    test_command: str | None,
    do_labels: bool,
    dry_run: bool,
    config_path: Path | None,
    check_freshness: bool = True,
) -> dict:
    """Everything ``coord repo add`` does except printing the residue block —
    write the ``coordinator.yml`` entry, add the repo to its machines, and
    (optionally) create the ``coord``/tier labels.

    Factored out of the ``repo_add`` click command so ``coord repo create``
    (#2747) can reuse the exact same write path — same seatbelt, same
    machine-name validation, same idempotent label creation — while printing
    a SHORTER residue afterward, because it already seeded the three things
    that make up half of ``repo add``'s residue list (CLAUDE.md, the CI
    workflow, ``.githooks/``). ``repo_add`` itself is now a thin wrapper:
    call this, then print the full 8-item residue.

    Returns ``{"target": Path, "machines": list[str], "landed": list[str],
    "default_branch": str}`` — everything either caller's residue printer
    needs. Raises :class:`click.ClickException` on any refusal (unknown repo
    name collision, unknown machine, unreadable default branch, an edit that
    would not parse) — same as the command itself used to.
    """
    from coord.config import load as load_config  # noqa: PLC0415
    from coord.repo_edit import (  # noqa: PLC0415
        RepoEditError,
        add_repo_to_machine,
        insert_repo_entry,
        render_repo_entry,
    )
    from coord.repo_onboard import COORD_LABEL, TIER_LABELS  # noqa: PLC0415

    target = _resolve_write_target(config_path)
    # #2861 step 1 — before ANY read of the file we are about to base an edit
    # on, not just before the write: the whole failure mode is deriving the
    # new content from a stale base.
    _guard_settings_fresh(target, enabled=check_freshness)
    original = target.read_text(encoding="utf-8")

    try:
        cfg = load_config(target)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"{target} does not currently load: {exc}") from exc

    if cfg.repo(name) is not None:
        raise click.ClickException(
            f"repo {name!r} already has a repos[] entry in {target} — use "
            f"`coord repo doctor {name}` to find what is actually missing."
        )

    known = {m.name for m in cfg.machines}
    machines = [m.strip() for m in (machines_csv or "").split(",") if m.strip()]
    unknown = [m for m in machines if m not in known]
    if unknown:
        raise click.ClickException(
            f"unknown machine(s) {unknown} — coordinator.yml has {sorted(known)}"
        )

    # ── The real default branch, read from GitHub rather than trusted ────
    if default_branch_override:
        default_branch = default_branch_override
        branch_source = "--default-branch (NOT verified against GitHub)"
    else:
        from coord import github_ops  # noqa: PLC0415

        try:
            default_branch = github_ops.get_repo_default_branch(github_slug)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(
                f"could not read {github_slug}'s default branch from GitHub: "
                f"{exc}. Fix `gh` auth, or pass --default-branch explicitly "
                "(and know that an unverified value silently routes worker PRs "
                "to the wrong base)."
            ) from exc
        branch_source = f"read from GitHub ({github_slug})"

    entry = render_repo_entry(
        name, github_slug, default_branch,
        build_command=build_command, test_command=test_command,
    )
    try:
        updated = insert_repo_entry(original, entry)
        path_tmpl = repo_path_tmpl or f"~/src/{name}"
        for machine in machines:
            updated = add_repo_to_machine(updated, machine, name, path_tmpl)
    except RepoEditError as exc:
        raise click.ClickException(str(exc)) from exc

    # ── Seatbelt: the edit must produce a config that LOADS and contains
    # what we think it contains. A line-level edit that "worked" but left a
    # repo unroutable is worse than no command at all (#2220's whole thesis).
    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile(
        "w", suffix=".yml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(updated)
        probe_path = Path(fh.name)
    try:
        new_cfg = load_config(probe_path)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"refusing to write: the edited config does not parse ({exc}). "
            f"{target} is unchanged."
        ) from exc
    finally:
        probe_path.unlink(missing_ok=True)

    if new_cfg.repo(name) is None:
        raise click.ClickException(
            f"refusing to write: the edit parsed but repo {name!r} is not in "
            f"the result. {target} is unchanged."
        )
    landed = [m.name for m in new_cfg.machines if name in (m.repos or [])]
    missing = [m for m in machines if m not in landed]
    if missing:
        raise click.ClickException(
            f"refusing to write: the edit parsed but machine(s) {missing} do "
            f"not list {name!r}. {target} is unchanged."
        )

    if dry_run:
        click.echo(f"--dry-run: would write {target}")
        click.echo(updated)
    else:
        target.write_text(updated, encoding="utf-8")
        click.echo(f"✓ wrote repos[{name}] to {target}")
        click.echo(f"  default_branch: {default_branch}  ({branch_source})")
        if landed:
            click.echo(f"  machines: {', '.join(landed)}")

    # ── Labels ───────────────────────────────────────────────────────────
    created: list[str] = []
    label_failures: list[str] = []
    if do_labels and not dry_run:
        from coord import github_ops  # noqa: PLC0415

        for label, colour, desc in (
            (COORD_LABEL, "0e8a16", "Managed by the coord Pipeline"),
            (TIER_LABELS[0], "c2e0c6", "Route to the small/cheap model"),
            (TIER_LABELS[1], "5319e7", "Route to the large model"),
        ):
            try:
                github_ops.create_label(
                    github_slug, label, color=colour, description=desc
                )
                created.append(label)
            except Exception as exc:  # noqa: BLE001
                label_failures.append(f"{label}: {exc}")
        if created:
            click.echo(f"✓ labels ensured on {github_slug}: {', '.join(created)}")
        for failure in label_failures:
            click.echo(f"⚠ label creation failed — {failure}", err=True)

    return {
        "target": target,
        "machines": machines,
        "landed": landed,
        "default_branch": default_branch,
    }


def _write_acceptance_driver_entry(*, target: Path, name: str, ci_template: str) -> bool:
    """Write ``acceptance.drivers.<name>`` into *target* for the stack
    *ci_template* selected (#2748, IL-2), or do nothing for ``generic``
    (stack undecided — a driver would be as much of a guess as the CI
    template deliberately isn't).

    Returns whether an entry was written. Same seatbelt shape as
    :func:`_do_repo_add_core`'s own write: edit, re-parse into a TEMP file,
    confirm the driver actually resolves for *name*, only THEN write
    *target* — a plausible-looking edit that silently failed to parse (or
    landed under the wrong key) is worse than not writing at all, since
    nothing else re-checks this afterward.
    """
    spec = _ACCEPTANCE_DRIVER_TEMPLATES.get(ci_template)
    if spec is None:
        return False

    from coord.config import load as load_config  # noqa: PLC0415
    from coord.repo_edit import (  # noqa: PLC0415
        RepoEditError,
        insert_acceptance_driver_entry,
        render_acceptance_driver_entry,
    )

    original = target.read_text(encoding="utf-8")
    entry = render_acceptance_driver_entry(
        name, spec["kind"], spec["run"],
        setup=spec.get("setup", ""), mock=spec.get("mock", ""),
        capability=spec.get("capability", ""),
    )
    try:
        updated = insert_acceptance_driver_entry(original, entry)
    except RepoEditError as exc:
        raise click.ClickException(str(exc)) from exc

    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile(
        "w", suffix=".yml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(updated)
        probe_path = Path(fh.name)
    try:
        new_cfg = load_config(probe_path)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"refusing to write acceptance.drivers.{name}: the edited config "
            f"does not parse ({exc}). {target} is unchanged — but the repo/"
            f"machine onboarding above already succeeded. Add the driver by "
            f"hand, then `coord repo doctor {name}` to confirm it resolves."
        ) from exc
    finally:
        probe_path.unlink(missing_ok=True)

    driver = new_cfg.acceptance.driver_for(name)
    if driver is None or driver.kind != spec["kind"]:
        raise click.ClickException(
            f"refusing to write acceptance.drivers.{name}: the edit parsed "
            f"but does not resolve to a {spec['kind']!r} driver for {name!r}. "
            f"{target} is unchanged — but the repo/machine onboarding above "
            f"already succeeded. Add the driver by hand, then `coord repo "
            f"doctor {name}` to confirm it resolves."
        )

    target.write_text(updated, encoding="utf-8")
    return True


def _print_add_residue(
    *, target: Path, machines: list[str], repo_path_tmpl: str | None, name: str,
) -> None:
    """The full 8-item residue block ``coord repo add`` prints — what it
    deliberately did NOT do, factored out of the command so ``coord repo
    create`` (#2747) can print its own shorter list instead (see
    :func:`_print_create_residue`) after the same core write.
    """
    click.echo("")
    click.echo("NOT DONE — these need a human, and `coord repo doctor` checks each:")
    tracked = default_settings_dir() / TRACKED_CONFIG_REL
    if target == tracked:
        click.echo(
            f"  1. commit + push in {default_settings_dir()}, then `git pull` on "
            "the daemon host — the fleet runs the COMMITTED config"
        )
    else:
        click.echo(
            f"  1. commit + push {target} wherever it is tracked, then `git "
            "pull` on the daemon host — the fleet runs the COMMITTED config"
        )
    for machine in machines or ["<each machine>"]:
        click.echo(
            f"  2. clone the repo to {repo_path_tmpl or f'~/src/{name}'} on "
            f"{machine} — this is the worker WORKTREE BASE"
        )
    click.echo(
        "  3. `git pull` the settings checkout on each of those machines. "
        "Since #2299 a running coord-agent re-reads its own coordinator.yml "
        "on the next /health poll and serves the repo with NO restart — but "
        "it can only re-read the file that is actually on ITS disk. No "
        "restart needed (and none wanted: it kills live workers). If "
        "`coord repo doctor` still flags agent_repo_skew a poll later, check "
        "`journalctl --user -u coord-agent` for a `failed to reload` line."
    )
    click.echo(
        "  4. add a CLAUDE.md to the repo — the Test agent auto-loads it and "
        "the adversarial review prompt is assembled from it; without one, "
        "reviews enforce nothing."
    )
    click.echo(
        "  5. make sure at least one CI workflow triggers on `pull_request`. "
        "If none does, expects_checks() reads 'CI exists' while zero checks "
        "arrive and `checks_absent` blocks EVERY merge in this repo, forever."
    )
    click.echo(
        "  6. set `test_command`/`ci_command`, `smoke_tests.capability_rules` "
        "for this repo's paths, and (if it joins the oracle loop) "
        "`acceptance.drivers`."
    )
    # #2237: layer 5 used to be one vague line here and nothing else — so
    # every repo onboarded from this command started with no graph, and the
    # only thing that would ever say so was a `repo doctor` nobody is obliged
    # to run. Split into the two halves that have genuinely different owners:
    # the versioned port (a PR against the repo, never automated) and the
    # machine-local half (idempotent, and `--fix` does it on every machine).
    click.echo(
        "  7. GRAPH, versioned half — port `.githooks/` (_lib.sh, "
        "post-checkout, post-commit, post-merge) into the repo and commit "
        "them. Tracked files, so one PR reaches every machine. Without them "
        "worktrees get no linked graph and every worker falls back to grep "
        "(docs/GRAPHIFY_SETUP.md)."
    )
    click.echo(
        "  8. GRAPH, machine-local half — once the repo is cloned on each "
        f"machine: `coord repo doctor {name} --fix` builds the graph "
        "(`graphify update .`) and sets `core.hooksPath .githooks` on every "
        "machine that runs workers. Idempotent; safe to re-run. Do it AFTER "
        "step 7 — pointing core.hooksPath at a .githooks/ that does not "
        "exist silently disables every hook in the checkout, so --fix refuses "
        "until the hooks are ported."
    )
    click.echo("")
    click.echo(f"Then: coord repo doctor {name}")


# ── #2861: `--for-submission` — repo genesis for a portal submission ────────
#
# Steps 3-6 of the issue's sequence (1 is `_guard_settings_fresh`, 2 is the
# create+seed above). Each step fails loudly and leaves the previous ones
# intact, because every one of them is separately recoverable by hand and a
# rollback would be strictly worse than an honest report of where it stopped.
#
# ── The step-5 decision the issue asked to be made deliberately ────────────
#
# "Distribute" has no blessed mechanism today because the daemon's live
# `~/.coord/coordinator.yml` is a COPY on at least one fleet host, while
# `docs/CUSTOMER_PORTAL.md` and `coord.fleet_config_health` both describe it
# as a symlink into the coord-settings checkout.
#
# Decision: **this command owns the copy explicitly, and never converts
# between the two arrangements.** Restoring the symlink everywhere is
# tempting and was rejected: it silently widens the blast radius of every
# future coord-settings commit (a bad push becomes live on the daemon the
# instant someone runs `git pull`, with no separate step to notice it), and
# `coord repo create` is the wrong command to make that fleet-wide policy
# change from. So per machine:
#
#   * symlink into the checkout  → the `git pull` already refreshed it. Done.
#   * regular file, byte-identical → nothing to do.
#   * regular file, differs        → back it up to `<live>.bak-<stamp>` and
#     copy the tracked file over it. The backup is the whole point: #2861's
#     live run found a comment that existed ONLY in the daemon's copy, and a
#     clobber with no backup would have destroyed it silently.
#
# `--no-refresh-live-config` reports the third case instead of acting, for an
# operator who wants to reconcile the divergence by hand first.

#: Distinct per-machine outcomes the distribution snippet reports back.
#: Parsed from `STATE=<x>` on stdout rather than an exit code so one machine's
#: unusual-but-fine state (no checkout at all — the norm on agent-only hosts,
#: see #1779) never reads as a failure.
_DISTRIBUTE_STATES: dict[str, tuple[str, str]] = {
    "no-checkout": (
        "·",
        "no coord-settings checkout here — nothing to pull (expected on every "
        "machine except the daemon host and the operator box, #1779)",
    ),
    "pull-failed": ("✗", "`git pull --ff-only` FAILED — this machine is still on the old config"),
    "symlink": ("✓", "pulled; live config is a symlink into the checkout, so it followed"),
    "live-missing": ("⚠", "pulled, but there is no live ~/.coord/coordinator.yml here"),
    "live-current": ("✓", "pulled; live config is a copy and already matches the checkout"),
    "live-refreshed": (
        "✓",
        "pulled; live config was a DIVERGING copy — backed up to <live>.bak-* and refreshed",
    ),
    "live-refresh-failed": ("✗", "pulled, but refreshing the live copy failed"),
    "live-stale": (
        "⚠",
        "pulled, but the live config is a copy that DIFFERS and "
        "--no-refresh-live-config was given — the daemon is still on the old file",
    ),
}


@dataclass
class _MachineDistribution:
    """One machine's result from the distribution step."""

    machine: str
    state: str
    head: str = ""
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.state in {"pull-failed", "live-refresh-failed", "unreachable"}


@dataclass
class _SubmissionPlan:
    """What ``--for-submission`` resolved before doing anything."""

    submission_id: str
    project_id: str
    repo: str
    already_mapped: bool = False
    machines: list[str] = field(default_factory=list)


def _resolve_submission_project(submission_id: str) -> str:
    """The portal ``project_id`` *submission_id* belongs to.

    Read from the read-only customer mirror
    (``portal_submissions.customer_json``) — the same field
    :func:`coord.approved_work.approved_submissions` resolves ``repos`` from,
    so a mapping written here is guaranteed to be the one that panel reads
    back. Both spellings the portal has used on the wire are accepted, for
    the same reason ``coord.approved_work._TEXT_FIELD_ALIASES`` accepts both.
    """
    from coord import portal_store  # noqa: PLC0415

    record = portal_store.get_submission(submission_id)
    if record is None:
        raise click.ClickException(
            f"unknown submission {submission_id!r} — no row in "
            "`portal_submissions`. Nothing was created or written. Check the "
            "id, and note that submissions only exist on the machine that "
            "runs `coord portal sync` (the daemon host)."
        )

    mirror = record.customer if isinstance(record.customer, dict) else {}
    for key in ("project_id", "projectId"):
        value = mirror.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    raise click.ClickException(
        f"submission {submission_id!r} exists but its customer mirror carries "
        "no project_id — the #2585 mirror-clobber shape. Nothing was created "
        f"or written. Repair it first: coord portal remirror {submission_id} "
        "(on the daemon host), then re-run."
    )


def _plan_for_submission(
    *, target: Path, submission_id: str, repo_name: str, machines: list[str],
) -> _SubmissionPlan:
    """Resolve + validate the mapping BEFORE any side effect.

    Refuses on a project already mapped to a different repo, naming the
    conflict: remapping is a decision with consequences for every linked
    milestone, and silently appending a second entry is impossible anyway
    (``_parse_portal_project_repos`` rejects a duplicate ``project_id`` at
    load, so the config would stop parsing fleet-wide).
    """
    from coord.config import load as load_config  # noqa: PLC0415

    project_id = _resolve_submission_project(submission_id)
    cfg = load_config(target)
    existing = cfg.portal.repos_for_project(project_id)

    if existing and list(existing) != [repo_name]:
        raise click.ClickException(
            f"project {project_id} (submission {submission_id}) is already "
            f"mapped to {existing} in {target}, not [{repo_name!r}]. Refusing "
            "to remap: every `coord portal link` recorded against this "
            "submission resolves through that mapping. Nothing was created or "
            "written. Edit portal.project_repos by hand if the remap is "
            "genuinely what you want."
        )

    return _SubmissionPlan(
        submission_id=submission_id,
        project_id=project_id,
        repo=repo_name,
        already_mapped=bool(existing),
        machines=list(machines),
    )


def _write_project_mapping(*, target: Path, plan: _SubmissionPlan) -> bool:
    """Append ``portal.project_repos``' entry for *plan* to *target*.

    Returns whether anything was written (``False`` when the mapping was
    already exactly right — this command is re-runnable). Same seatbelt shape
    as every other write in this module: edit, re-parse into a TEMP file,
    confirm the mapping actually RESOLVES through the same
    :meth:`PortalConfig.repos_for_project` the board reads, only then write.
    """
    if plan.already_mapped:
        return False

    import tempfile  # noqa: PLC0415

    from coord.config import load as load_config  # noqa: PLC0415
    from coord.repo_edit import (  # noqa: PLC0415
        RepoEditError,
        insert_portal_project_repo_entry,
        render_portal_project_repo_entry,
    )

    original = target.read_text(encoding="utf-8")
    entry = render_portal_project_repo_entry(plan.project_id, [plan.repo])
    try:
        updated = insert_portal_project_repo_entry(original, entry)
    except RepoEditError as exc:
        raise click.ClickException(str(exc)) from exc

    with tempfile.NamedTemporaryFile(
        "w", suffix=".yml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(updated)
        probe_path = Path(fh.name)
    try:
        new_cfg = load_config(probe_path)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"refusing to write portal.project_repos: the edited config does "
            f"not parse ({exc}). {target} is unchanged — but the repo/machine "
            "onboarding above already succeeded. Add the mapping by hand, "
            "then re-run with --for-submission to distribute it."
        ) from exc
    finally:
        probe_path.unlink(missing_ok=True)

    if new_cfg.portal.repos_for_project(plan.project_id) != [plan.repo]:
        raise click.ClickException(
            f"refusing to write portal.project_repos: the edit parsed but "
            f"{plan.project_id} does not resolve to [{plan.repo!r}]. {target} "
            "is unchanged — but the repo/machine onboarding above already "
            "succeeded. Add the mapping by hand."
        )

    target.write_text(updated, encoding="utf-8")
    return True


def _commit_and_push_settings(checkout: Path, *, message: str) -> str:
    """Commit every staged/unstaged config change in *checkout* and push it.

    Returns the pushed short SHA. Raises loudly — an unpushed commit is the
    exact state #2861 exists to stop an operator ending up in without knowing
    it, so a push failure must never read as success.
    """
    add = _git(checkout, "add", "--", str(TRACKED_CONFIG_REL))
    if add is None or add.returncode != 0:
        raise click.ClickException(
            f"could not `git add {TRACKED_CONFIG_REL}` in {checkout}: "
            f"{(add.stderr if add else 'git could not be run').strip()}. The "
            "config edits ARE on disk — commit and push them by hand."
        )

    staged = _git(checkout, "diff", "--cached", "--quiet")
    if staged is not None and staged.returncode == 0:
        click.echo("  · nothing to commit — the config already matches HEAD")
    else:
        commit = _git(checkout, "commit", "-m", message)
        if commit is None or commit.returncode != 0:
            raise click.ClickException(
                f"could not commit in {checkout}: "
                f"{(commit.stderr or commit.stdout if commit else 'git could not be run').strip()}. "
                "The config edits ARE on disk and staged — commit and push by hand."
            )

    push = _git(checkout, "push", timeout=120.0)
    if push is None or push.returncode != 0:
        raise click.ClickException(
            f"could not push {checkout}: "
            f"{(push.stderr or push.stdout if push else 'git could not be run').strip()}. "
            "The commit EXISTS locally but no other machine can see it — push "
            "by hand, then re-run the distribution with `coord repo doctor "
            "--fix` afterwards."
        )

    head = _git(checkout, "rev-parse", "--short", "HEAD")
    return (head.stdout.strip() if head and head.returncode == 0 else "")


def _home_relative(path: Path) -> str:
    """*path* as a ``$HOME``-relative shell word when it is under ``$HOME``.

    The remote machine's ``$HOME`` is not this machine's, so a locally
    expanded absolute path is wrong the moment two hosts have different
    usernames.
    """
    try:
        return f'"$HOME"/{path.relative_to(Path.home())}'
    except ValueError:
        return shlex.quote(str(path))


def _distribute_script(*, refresh_live: bool) -> str:
    """The POSIX-sh snippet run on each machine: pull, then reconcile the live
    config per the step-5 decision documented above this section."""
    return f"""set -u
d={_home_relative(default_settings_dir())}
live={_home_relative(default_live_config_path())}
rel={shlex.quote(str(TRACKED_CONFIG_REL))}
if [ ! -d "$d/.git" ]; then echo "STATE=no-checkout"; exit 0; fi
if ! git -C "$d" pull --ff-only >/dev/null 2>&1; then echo "STATE=pull-failed"; exit 0; fi
echo "HEAD=$(git -C "$d" rev-parse --short HEAD)"
if [ -L "$live" ]; then echo "STATE=symlink"; exit 0; fi
if [ ! -e "$live" ]; then echo "STATE=live-missing"; exit 0; fi
if cmp -s "$live" "$d/$rel"; then echo "STATE=live-current"; exit 0; fi
if [ {"1" if refresh_live else "0"} = 1 ]; then
  if cp -p "$live" "$live.bak-$(date +%Y%m%dT%H%M%S)" && cp "$d/$rel" "$live"; then
    echo "STATE=live-refreshed"
  else
    echo "STATE=live-refresh-failed"
  fi
else
  echo "STATE=live-stale"
fi
"""


def _ssh_run(host: str, script: str, *, timeout: float = 180.0):
    """Run *script* on *host* over SSH. Mirrors ``coord.commands.agent_ops``'
    ``BatchMode``/``ConnectTimeout``/``accept-new`` options so this never
    hangs on a password or a host-key prompt in a non-interactive session."""
    return subprocess.run(  # noqa: S603 — fixed argv, host from coordinator.yml
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            host,
            script,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_local_distribution(*, refresh_live: bool) -> _MachineDistribution:
    """The same reconciliation as :func:`_distribute_script`, for THIS machine.

    Run unconditionally: the operator box is where the commit was just made,
    and — on a single-host fleet — is also the daemon host, so skipping it
    would leave the live config on the old file with nothing saying so.
    """
    import filecmp  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import time  # noqa: PLC0415

    checkout = default_settings_dir()
    live = default_live_config_path()
    tracked = checkout / TRACKED_CONFIG_REL

    if not (checkout / ".git").exists():
        return _MachineDistribution("this machine", "no-checkout")

    pull = _git(checkout, "pull", "--ff-only", timeout=120.0)
    if pull is None or pull.returncode != 0:
        return _MachineDistribution("this machine", "pull-failed")
    head = _git(checkout, "rev-parse", "--short", "HEAD")
    sha = head.stdout.strip() if head and head.returncode == 0 else ""

    if live.is_symlink():
        return _MachineDistribution("this machine", "symlink", head=sha)
    if not live.exists():
        return _MachineDistribution("this machine", "live-missing", head=sha)
    try:
        if filecmp.cmp(live, tracked, shallow=False):
            return _MachineDistribution("this machine", "live-current", head=sha)
    except OSError:
        return _MachineDistribution("this machine", "live-refresh-failed", head=sha)
    if not refresh_live:
        return _MachineDistribution("this machine", "live-stale", head=sha)
    try:
        backup = live.with_name(f"{live.name}.bak-{time.strftime('%Y%m%dT%H%M%S')}")
        shutil.copy2(live, backup)
        shutil.copyfile(tracked, live)
    except OSError as exc:
        return _MachineDistribution(
            "this machine", "live-refresh-failed", head=sha, detail=str(exc)
        )
    return _MachineDistribution(
        "this machine", "live-refreshed", head=sha, detail=f"backup: {backup}"
    )


def _distribute_settings(
    machines: list[str], *, cfg, refresh_live: bool, ssh_timeout: float,
) -> list[_MachineDistribution]:
    """`git pull` coord-settings on every machine in *machines*, plus here.

    One machine's failure never aborts the sweep — the acceptance criterion
    is explicitly "reports per-machine success/failure rather than failing
    silently on one host", and a half-distributed fleet that says so is
    recoverable while one that doesn't is #2861's original nine-motion mess.
    """
    results = [_run_local_distribution(refresh_live=refresh_live)]
    script = _distribute_script(refresh_live=refresh_live)
    by_name = {m.name: m for m in cfg.machines}

    for name in machines:
        machine = by_name.get(name)
        if machine is None:  # pragma: no cover — pre-flight already refused
            results.append(_MachineDistribution(name, "unreachable", detail="not in config"))
            continue
        try:
            proc = _ssh_run(machine.host, script, timeout=ssh_timeout)
        except Exception as exc:  # noqa: BLE001 — one host must not abort the sweep
            results.append(_MachineDistribution(name, "unreachable", detail=str(exc)))
            continue
        state, sha = "", ""
        for line in (proc.stdout or "").splitlines():
            if line.startswith("STATE="):
                state = line.split("=", 1)[1].strip()
            elif line.startswith("HEAD="):
                sha = line.split("=", 1)[1].strip()
        if not state:
            results.append(
                _MachineDistribution(
                    name, "unreachable",
                    detail=(proc.stderr or proc.stdout or "no STATE reported").strip()[:200],
                )
            )
            continue
        results.append(_MachineDistribution(name, state, head=sha))
    return results


def _print_distribution(results: list[_MachineDistribution]) -> None:
    for result in results:
        mark, detail = _DISTRIBUTE_STATES.get(
            result.state, ("✗", f"unreachable or unexpected state {result.state!r}")
        )
        suffix = f" [{result.head}]" if result.head else ""
        click.echo(f"  {mark} {result.machine}{suffix}: {detail}")
        if result.detail:
            click.echo(f"      {result.detail}")


def _run_repo_doctor_fix(name: str) -> None:
    """Step 6: ``coord repo doctor --fix``, in-process.

    ``repo_doctor`` ``sys.exit(1)`` on any CRIT, which is right for a gate and
    wrong as the last step of a longer command — a CRIT here means "the
    genesis got this far and this is what is left", not "the command failed".
    So the exit is caught and reported as findings.
    """
    from coord.config import resolve_config_path  # noqa: PLC0415

    ctx = click.get_current_context(silent=True)
    invoke = ctx.invoke if ctx is not None else (lambda fn, **kw: fn.callback(**kw))
    try:
        invoke(
            repo_doctor,
            name=name,
            config_path=resolve_config_path(),
            timeout=3.0,
            probe_github=True,
            verbose=False,
            do_fix=True,
            fix_timeout=900.0,
        )
    except SystemExit as exc:
        if exc.code:
            click.echo(
                f"⚠ `coord repo doctor {name} --fix` reported CRIT findings "
                "(above). Genesis got this far; those are what is left."
            )
    except Exception as exc:  # noqa: BLE001 — the doctor must not sink the genesis
        click.echo(f"⚠ `coord repo doctor {name} --fix` could not run — {exc}")


def _print_for_submission_plan(
    *, name: str, submission_id: str, machines: list[str], target: Path,
    refresh_live: bool,
) -> None:
    """``--dry-run``'s six-step plan. Deliberately printed WITHOUT resolving
    the submission against the local DB: a dry run on a machine that has no
    portal DB (every machine but the daemon host) must still show the plan.
    """
    checkout = _git_toplevel(target) or default_settings_dir()
    hosts = ", ".join(machines) if machines else "<no --machines given>"
    click.echo("")
    click.echo(f"--dry-run: --for-submission {submission_id} would, in order:")
    click.echo(
        f"  1. refuse if {checkout} is behind its upstream (git fetch + compare)"
    )
    click.echo(f"  2. create + seed the repo, and write repos[{name}] to {target}")
    click.echo(
        f"  3. resolve {submission_id}'s project_id from its customer mirror and "
        f"append portal.project_repos: {{project_id: <resolved>, repos: [{name}]}} "
        "(refusing if that project is already mapped elsewhere)"
    )
    click.echo(f"  4. commit + push {checkout}")
    click.echo(
        f"  5. distribute: `git pull --ff-only` coord-settings on {hosts} and here, "
        + (
            "then refresh any live ~/.coord/coordinator.yml that is a diverging "
            "copy (backing it up first)"
            if refresh_live
            else "reporting — but NOT refreshing — any diverging live copy"
        )
    )
    click.echo(f"  6. run `coord repo doctor {name} --fix` and print its report")
    click.echo("")
    click.echo("Nothing was created, written, pushed or pulled.")


def _run_for_submission(
    *, name: str, submission_id: str, target: Path, machines: list[str], cfg,
    refresh_live: bool, ssh_timeout: float, run_doctor: bool,
) -> None:
    """Steps 3-6 for a completed repo create/add (#2861)."""
    plan = _plan_for_submission(
        target=target, submission_id=submission_id, repo_name=name, machines=machines,
    )

    click.echo("")
    click.echo(
        f"--for-submission {plan.submission_id}: project {plan.project_id}"
    )
    if _write_project_mapping(target=target, plan=plan):
        click.echo(f"✓ mapped {plan.project_id} → [{name}] in portal.project_repos")
    else:
        click.echo(f"· {plan.project_id} was already mapped to [{name}] — left alone")

    checkout = _git_toplevel(target)
    if checkout is None:
        click.echo(
            f"⚠ {target} is not inside a git checkout — skipping the commit, "
            "push and distribution. The config edits are on disk here only.",
            err=True,
        )
        return

    click.echo(f"committing + pushing {checkout}...")
    sha = _commit_and_push_settings(
        checkout,
        message=(
            f"coord repo create: onboard {name} and map {plan.project_id} "
            f"({plan.submission_id})"
        ),
    )
    click.echo(f"✓ pushed{f' {sha}' if sha else ''}")

    click.echo("distributing coord-settings to the machines serving this repo...")
    results = _distribute_settings(
        machines, cfg=cfg, refresh_live=refresh_live, ssh_timeout=ssh_timeout,
    )
    _print_distribution(results)
    failed = [r.machine for r in results if r.failed]
    if failed:
        click.echo(
            f"⚠ distribution incomplete on: {', '.join(failed)}. Those machines "
            "are still serving the OLD config — `coord repo doctor` will report "
            "`agent_repo_skew` for them until a `git pull` lands there.",
            err=True,
        )
    click.echo(
        "  · thin clients re-fetch `GET /config` from the daemon on essentially "
        "every command (coord.client.REMOTE_CONFIG_CACHE), so nothing to force "
        "there — the daemon's own file, refreshed above, is what they read."
    )

    if run_doctor:
        click.echo("")
        _run_repo_doctor_fix(name)


@repo_group.command(
    "create",
    help=(
        "Create a NEW repo through the forge seam, seed it (CLAUDE.md, a "
        "pull_request-triggered CI workflow, .githooks/), then chain into "
        "`coord repo add`. IL-1 (#2747): shrinks `repo add`'s 8-item human "
        "residue down to 4 — the clone on each machine, the coord-settings "
        "commit+push, the per-machine coord-settings `git pull`, and `coord "
        "repo doctor --fix`. Add --for-submission (#2861) to close three of "
        "those four: it maps the submission's project, commits + pushes "
        "coord-settings, distributes it to every machine serving the repo "
        "and runs the doctor, leaving only the clone. Never shells out to "
        "`gh` directly outside coord.github_ops, so a future GitLab backend "
        "is a driver swap, not a rewrite — and workers, for whom `gh` is "
        "deny-listed, can use this too."
    ),
)
@click.argument("name")
@click.option("--github", "github_slug", required=True, help="owner/repo to create on GitHub.")
@click.option(
    "--private", is_flag=True, default=False,
    help="Create the GitHub repo private. Default: public.",
)
@click.option("--description", default=None, help="GitHub repo description.")
@click.option(
    "--template", "ci_template", type=click.Choice(sorted(_CI_TEMPLATES)),
    default="generic", show_default=True,
    help=(
        "Which CI workflow to seed. `generic` reports a green placeholder "
        "check and is right when the stack isn't decided yet; `python`/"
        "`node` run a real build+test command and expect the repo to "
        "already have that stack's project files (a red first check on an "
        "otherwise-empty repo is a worse start than an honest placeholder)."
    ),
)
@click.option(
    "--machines", "machines_csv", default=None,
    help="Comma-separated machine names that should serve this repo.",
)
@click.option(
    "--repo-path", "repo_path_tmpl", default=None,
    help="Path to the clone on each machine. Default: ~/src/<name>.",
)
@click.option("--build-command", default=None, help="repos[].build_command.")
@click.option("--test-command", default=None, help="repos[].test_command.")
@click.option(
    "--labels/--no-labels", "do_labels", default=True, show_default=True,
    help="Create the `coord` and tier:small/tier:large labels on GitHub.",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Create nothing on GitHub or in coordinator.yml — print what would happen.",
)
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), default=None,
    help="coordinator.yml to edit. Default: the coord-settings tracked file.",
)
@click.option("--for-submission", "submission_id", default=None, help=_FOR_SUBMISSION_HELP)
@click.option(
    "--refresh-live-config/--no-refresh-live-config", "refresh_live",
    default=True, show_default=True,
    help=(
        "During --for-submission's distribution step, overwrite a machine's "
        "live ~/.coord/coordinator.yml when it is a REGULAR FILE that diverges "
        "from the checkout (a backup is written first). Off reports the "
        "divergence and leaves it. Symlinked live configs are never touched "
        "either way — the pull already refreshed them."
    ),
)
@click.option(
    "--skip-freshness-check", "skip_freshness", is_flag=True, default=False,
    help=(
        "Do NOT compare the config checkout to its upstream before writing. "
        "Rarely right: an entry written onto a stale base looks clean in the "
        "diff (#2861)."
    ),
)
@click.option(
    "--ssh-timeout", default=180.0, show_default=True, type=float,
    help="Per-machine timeout for the --for-submission distribution step.",
)
def repo_create(  # noqa: PLR0913 — one option per thing the command can set
    name: str,
    github_slug: str,
    private: bool,  # noqa: FBT001
    description: str | None,
    ci_template: str,
    machines_csv: str | None,
    repo_path_tmpl: str | None,
    build_command: str | None,
    test_command: str | None,
    do_labels: bool,  # noqa: FBT001
    dry_run: bool,  # noqa: FBT001
    config_path: Path | None,
    submission_id: str | None,
    refresh_live: bool,  # noqa: FBT001
    skip_freshness: bool,  # noqa: FBT001
    ssh_timeout: float,
) -> None:
    from coord import github_ops  # noqa: PLC0415
    from coord.config import load as load_config  # noqa: PLC0415

    # ── Pre-flight, BEFORE any GitHub side effect ─────────────────────────
    # The same checks `coord repo add` makes, run here FIRST too: a config
    # collision or an unknown machine name must never leave an orphaned,
    # just-created-but-unconfigured GitHub repo behind. `_do_repo_add_core`
    # (called below, after creation) re-checks all of this from scratch —
    # this pre-flight is belt-and-suspenders, not the seatbelt itself.
    target = _resolve_write_target(config_path)
    # #2861 step 1, FIRST: a stale base must refuse before anything is created
    # on GitHub, not just before the config write.
    _guard_settings_fresh(target, enabled=not skip_freshness)
    try:
        cfg = load_config(target)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"{target} does not currently load: {exc}") from exc

    if cfg.repo(name) is not None:
        raise click.ClickException(
            f"repo {name!r} already has a repos[] entry in {target} — use "
            f"`coord repo doctor {name}` to find what is actually missing."
        )

    known = {m.name for m in cfg.machines}
    machines = [m.strip() for m in (machines_csv or "").split(",") if m.strip()]
    unknown = [m for m in machines if m not in known]
    if unknown:
        raise click.ClickException(
            f"unknown machine(s) {unknown} — coordinator.yml has {sorted(known)}"
        )

    # #2861 step 3, hoisted into the pre-flight: an unknown submission id or a
    # project already mapped elsewhere must refuse BEFORE the GitHub repo
    # exists. `_run_for_submission` re-resolves from scratch afterwards.
    if submission_id and not dry_run:
        _plan_for_submission(
            target=target, submission_id=submission_id, repo_name=name,
            machines=machines,
        )

    try:
        exists = github_ops.repo_exists(github_slug)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"could not check whether {github_slug} already exists on GitHub: "
            f"{exc}. Fix `gh` auth and retry."
        ) from exc
    if exists:
        raise click.ClickException(
            f"{github_slug} already exists on GitHub — `coord repo create` is "
            f"for a NEW repo. Use `coord repo add --github {github_slug} ...` "
            "to onboard an existing one instead."
        )

    if dry_run:
        click.echo(
            f"--dry-run: would create {github_slug} "
            f"({'private' if private else 'public'}), seed CLAUDE.md + "
            f"the {ci_template!r} CI workflow + .githooks/, then run the "
            f"equivalent of `coord repo add {name} --github {github_slug}`"
        )
        if submission_id:
            _print_for_submission_plan(
                name=name, submission_id=submission_id, machines=machines,
                target=target, refresh_live=refresh_live,
            )
        return

    # ── Create + seed, through the forge seam only ────────────────────────
    click.echo(
        f"creating {github_slug} on GitHub "
        f"({'private' if private else 'public'})..."
    )
    try:
        created = github_ops.create_repo(
            github_slug, private=private, description=description,
        )
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"could not create {github_slug} on GitHub: {exc}. Nothing was "
            "created — fix the underlying problem (rate-limit, a name "
            "collision, an invalid/forbidden owner, a transient network "
            "blip) and retry."
        ) from exc
    default_branch = created.get("default_branch") or "main"
    click.echo(
        f"✓ created {created.get('url') or github_slug} "
        f"(default branch: {default_branch})"
    )

    click.echo(
        "seeding CLAUDE.md, .github/workflows/ci.yml "
        f"(--template {ci_template}), .githooks/, and graphify-out/.gitignore..."
    )
    files = _seed_files(name, ci_template)
    try:
        github_ops.create_commit_with_files(
            github_slug, default_branch, files,
            message=(
                "coord repo create: seed CLAUDE.md, CI workflow, .githooks, "
                "graphify-out/.gitignore (#2747, #3037)"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"{github_slug} was created but seeding failed: {exc}. The repo "
            "now EXISTS on GitHub but has no CLAUDE.md/CI/.githooks/"
            "graphify-out/.gitignore yet — fix "
            "the underlying problem (likely `gh` auth/rate-limit), then "
            "either seed it by hand or re-run `coord repo create` with the "
            "same --github: none of the seed files exist there yet, so a "
            "retry won't conflict. Finish onboarding yourself with `coord "
            f"repo add {name} --github {github_slug}`."
        ) from exc
    click.echo(f"✓ seeded {len(files)} file(s) on {default_branch}")

    # ── Chain into the exact write path `coord repo add` uses ────────────
    result = _do_repo_add_core(
        name=name,
        github_slug=github_slug,
        machines_csv=machines_csv,
        repo_path_tmpl=repo_path_tmpl,
        default_branch_override=None,
        build_command=build_command,
        test_command=test_command,
        do_labels=do_labels,
        dry_run=False,
        config_path=config_path,
        # Already checked in this command's own pre-flight, before any GitHub
        # side effect — re-fetching here would be a second network round trip
        # answering a question nothing could have changed since.
        check_freshness=False,
    )
    # #2748 (IL-2): a stack-appropriate acceptance.drivers entry, so the repo
    # is oracle-loop-ready on day one instead of residue item 6 nobody
    # performs. `generic` writes nothing — see
    # `_write_acceptance_driver_entry`'s docstring.
    driver_written = _write_acceptance_driver_entry(
        target=result["target"], name=name, ci_template=ci_template,
    )
    if driver_written:
        click.echo(
            f"✓ wrote acceptance.drivers.{name} "
            f"({_ACCEPTANCE_DRIVER_TEMPLATES[ci_template]['kind']})"
        )

    if submission_id:
        # #2861: the four residue items `repo create` prints are exactly what
        # `--for-submission` performs, so printing them here would be a list
        # of things this command is about to do.
        _run_for_submission(
            name=name, submission_id=submission_id, target=result["target"],
            machines=result["machines"], cfg=cfg, refresh_live=refresh_live,
            ssh_timeout=ssh_timeout, run_doctor=True,
        )
        click.echo("")
        click.echo(
            "NOT DONE — 1 thing still needs a human: clone the repo to "
            f"{repo_path_tmpl or f'~/src/{name}'} on "
            f"{', '.join(result['machines']) or '<each machine>'} "
            "— this is the worker WORKTREE BASE, and `coord repo doctor` "
            "reports it until it exists."
        )
        return

    _print_create_residue(
        target=result["target"], machines=result["machines"],
        repo_path_tmpl=repo_path_tmpl, name=name,
        driver_written=driver_written,
    )


def _print_create_residue(
    *, target: Path, machines: list[str], repo_path_tmpl: str | None, name: str,
    driver_written: bool = False,
) -> None:
    """The shrunk residue ``coord repo create`` prints (#2747) — ``repo
    add``'s 8 minus the 3 traps this command already closed (CLAUDE.md, the
    ``pull_request`` CI trigger, ``.githooks/``) and the narration lines that
    were never gating (the #2299 "no restart needed" explainer).

    Still includes the per-machine coord-settings ``git pull`` (item 3
    below): dropping it left every non-daemon machine's agent serving a
    stale ``coordinator.yml`` with ``coord repo doctor``'s
    ``machines.agent_repo_skew`` CRIT and nothing telling the operator to
    expect or fix it (review finding on #2747) — this is a doctor-checked
    remedy, not narration, so unlike the #2299 explainer it stays.
    """
    click.echo("")
    click.echo(
        "NOT DONE — 4 things still need a human (down from `repo add`'s 8 — "
        "CLAUDE.md, the CI workflow, .githooks/, and graphify-out/.gitignore "
        "are already seeded):"
    )
    tracked = default_settings_dir() / TRACKED_CONFIG_REL
    if target == tracked:
        click.echo(
            f"  1. commit + push in {default_settings_dir()}, then `git pull` on "
            "the daemon host — the fleet runs the COMMITTED config"
        )
    else:
        click.echo(
            f"  1. commit + push {target} wherever it is tracked, then `git "
            "pull` on the daemon host — the fleet runs the COMMITTED config"
        )
    for machine in machines or ["<each machine>"]:
        click.echo(
            f"  2. clone the repo to {repo_path_tmpl or f'~/src/{name}'} on "
            f"{machine} — this is the worker WORKTREE BASE"
        )
    for machine in machines or ["<each machine>"]:
        click.echo(
            f"  3. `git pull` the coord-settings checkout on {machine} too — "
            "not just the daemon host. Each agent re-reads its own "
            "coordinator.yml on the next /health poll (#2299, no restart "
            "needed), but only the file actually on ITS disk; a machine "
            "left behind here is exactly `coord repo doctor`'s "
            "`machines.agent_repo_skew` CRIT (docs/AGENT_OPERATIONS.md: "
            "\"if it persists: that agent's file is stale — `git pull` in "
            "coord-settings on that machine\")."
        )
    click.echo(
        f"  4. once cloned everywhere: `coord repo doctor {name} --fix` "
        "builds the graph (`graphify update .`) and sets `core.hooksPath "
        ".githooks` on every machine that runs workers. Idempotent; safe to "
        "re-run."
    )
    if driver_written:
        click.echo(
            "  Also worth doing by hand, not doctor-checked: set "
            "`test_command`/`ci_command` and `smoke_tests.capability_rules` "
            "for this repo's paths. `acceptance.drivers` is already written "
            "(#2748) — `coord repo doctor` reports its readiness as layer 6."
        )
    else:
        click.echo(
            "  Also worth doing by hand, not doctor-checked: set "
            "`test_command`/`ci_command` and `smoke_tests.capability_rules` "
            "for this repo's paths, and (if it joins the oracle loop) "
            "`acceptance.drivers` — `--template python|node` writes one "
            "automatically; `--template generic` leaves it for later since "
            "the stack isn't decided yet (#2748)."
        )
    click.echo("")
    click.echo(f"Then: coord repo doctor {name}")


@repo_group.command(
    "doctor",
    help=(
        "Probe all onboarding layers for a repo and report per-layer "
        "status — the five a repo must clear (config, machines, GitHub, "
        "contents, graph) plus the sixth, optional oracle-loop-readiness "
        "layer (#2748). Reads LIVE state — each agent's /health repo list, "
        "the labels that exist on GitHub, whether any workflow triggers on "
        "pull_request, and (since #2237) each machine's graph readiness "
        "rather than only this one's — not config. Exits non-zero on any "
        "CRIT so it can gate. Use --fix to repair the machine-local half "
        "of the graph layer."
    ),
)
@click.argument("name")
@_CONFIG_OPTION
@click.option(
    "--timeout", default=3.0, show_default=True, type=float,
    help="Per-machine /health timeout (seconds).",
)
@click.option(
    "--github/--no-github", "probe_github", default=True, show_default=True,
    help="Probe GitHub (labels, workflows, CLAUDE.md). Off makes this offline.",
)
@click.option(
    "--verbose", "-v", is_flag=True, default=False,
    help="Show passing checks too, not just the residue.",
)
@click.option(
    "--fix", "do_fix", is_flag=True, default=False,
    help=(
        "Repair graphify's MACHINE-LOCAL half on every machine that clones "
        "this repo: build a missing graph (`graphify update .`) and set "
        "`core.hooksPath .githooks`. Idempotent, never touches a tracked "
        "file, and refuses on a repo that has not ported `.githooks/` — that "
        "port is a PR against the repo, reported here as remaining work."
    ),
)
@click.option(
    "--fix-timeout", default=900.0, show_default=True, type=float,
    help="Per-machine timeout for --fix (a first graph build is minutes).",
)
def repo_doctor(  # noqa: PLR0913 — one option per thing the command can do
    name: str,
    config_path: Path,
    timeout: float,
    probe_github: bool,  # noqa: FBT001
    verbose: bool,  # noqa: FBT001
    do_fix: bool,  # noqa: FBT001
    fix_timeout: float,
) -> None:
    from coord import repo_onboard  # noqa: PLC0415
    from coord.network import check_all  # noqa: PLC0415

    cfg = _load_config(config_path)

    repo = cfg.repo(name)
    if repo is None:
        known = [r.name for r in cfg.repos]
        click.echo(
            f"error: repo {name!r} is not in coordinator.yml (have: {known})",
            err=True,
        )
        # Still render the report — "config.repo_missing" IS the finding, and
        # a caller gating on this deserves the same structured output.
        facts = repo_onboard.RepoFacts(name=name, configured=False)
        for line in repo_onboard.format_report(
            repo_onboard.evaluate(facts), verbose=verbose
        ):
            click.echo(line)
        sys.exit(1)

    machines = [m for m in cfg.machines if name in (m.repos or [])]
    statuses = check_all(machines, timeout=timeout) if machines else []

    if do_fix:
        # Repair BEFORE reporting, so the report an operator reads is the
        # state they are actually leaving behind — a --fix run that printed
        # the pre-fix findings would be indistinguishable from one that did
        # nothing.
        _run_graph_fix(cfg, name, machines, statuses, timeout=fix_timeout)
        statuses = check_all(machines, timeout=timeout) if machines else []

    facts = repo_onboard.gather_facts(
        cfg, name,
        statuses=statuses,
        probe_github=probe_github,
        local_clone=repo_onboard.local_clone_path(cfg, name),
    )
    report = repo_onboard.evaluate(facts)
    for line in repo_onboard.format_report(report, verbose=verbose):
        click.echo(line)
    if not report.ok:
        sys.exit(1)


def _run_graph_fix(cfg, name: str, machines, statuses, *, timeout: float) -> None:
    """``--fix``: graphify's machine-local half, on every machine (#2237).

    Fans out to each machine's agent (``POST /graph-fix``) rather than
    repairing the local clone, because the local clone is the one that
    matters least: workers run on dellserver and precision, the operator runs
    this on elitebook. A fixer that only ever fixed here would recreate
    layer 5's original blind spot one level up.

    The local clone is still repaired directly when this machine has one that
    no agent covers — an operator's laptop is a legitimate place to want a
    graph, it just isn't a place workers run.

    Everything it does is idempotent and machine-local (``graphify update .``,
    ``git config core.hooksPath``); the versioned ``.githooks/`` port is
    reported as remaining work by the report that follows, never performed.
    """
    import httpx  # noqa: PLC0415
    from coord.network import AGENT_PORT  # noqa: PLC0415

    click.echo(f"--fix: repairing graphify's machine-local half for {name}")
    online = {s.machine.name for s in statuses if s.is_online}
    fixed_paths: set[str] = set()

    for machine in machines:
        if machine.name not in online:
            click.echo(
                f"  ⚠ {machine.name}: skipped — agent not reachable, so nothing "
                f"here could be repaired (it is NOT known to be fine)"
            )
            continue
        try:
            resp = httpx.post(
                f"http://{machine.host}:{AGENT_PORT}/graph-fix",
                json={"repo": name, "timeout": timeout},
                timeout=timeout + 30.0,
            )
            resp.raise_for_status()
            result = resp.json()
        except Exception as exc:  # noqa: BLE001 — one machine must not abort the sweep
            click.echo(f"  ✗ {machine.name}: /graph-fix failed — {exc}")
            continue
        for line in _format_fix_result(machine.name, result):
            click.echo(line)
        if result.get("repo_path"):
            fixed_paths.add(str(Path(result["repo_path"]).expanduser()))

    # The local clone, when it is not one of the checkouts just repaired.
    from coord import repo_onboard  # noqa: PLC0415
    from coord.graph_health import apply_local_graph_fix  # noqa: PLC0415

    local = repo_onboard.local_clone_path(cfg, name)
    if local is not None and str(local.expanduser()) not in fixed_paths:
        result = apply_local_graph_fix(local, timeout=timeout).to_dict()
        for line in _format_fix_result("this machine", result):
            click.echo(line)
    click.echo("")


def _format_fix_result(where: str, result: dict) -> list[str]:
    """Render one machine's ``/graph-fix`` result.

    A refusal reads as a refusal, not a failure and emphatically not a
    success: "no `.githooks/` in this repo" means the fixer deliberately did
    nothing, and the operator's next move is a PR, not a re-run.
    """
    if result.get("refused"):
        return [f"  ⊘ {where}: refused — {result['refused']}"]
    lines: list[str] = []
    for step in result.get("steps") or []:
        if step.get("ok") and step.get("changed"):
            mark = "✓"
        elif step.get("ok"):
            mark = "·"  # already in the desired state — the idempotent no-op
        else:
            mark = "✗"
        lines.append(f"  {mark} {where}: {step.get('action')} — {step.get('detail')}")
    if not lines:
        lines.append(f"  · {where}: nothing to do")
    return lines
