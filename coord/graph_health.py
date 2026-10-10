"""Graphify knowledge-graph freshness + worktree-bootstrap health.

The repo ships a graphify graph in ``graphify-out/`` that agents are told to
query first (see CLAUDE.md).  Two things silently break it, and neither is
visible from the code:

**1. Linked worktrees are graph-blind.**  ``graphify-out/`` is gitignored by
design (only its ``.gitignore`` is tracked), so ``git worktree add`` produces
an empty one — and ``graphify query`` resolves ``graphify-out/graph.json``
strictly relative to cwd, with no upward walk and no ``--graph`` override.
``.githooks/post-checkout`` fixes this by symlinking each entry of the base
checkout's graph (``graph.json``, ``manifest.json``, ``cache/``, ...) into
the worktree's ``graphify-out/`` — the directory itself, and its tracked
``.gitignore``, are never touched (#1617: replacing the whole directory with
a symlink deleted the tracked ``.gitignore`` out from under git).  This hook
only runs where ``core.hooksPath`` points at ``.githooks`` — a one-time,
per-machine ``git config`` that nothing enforces.  :func:`hooks_path_status`
checks it.

**2. The graph drifts out of sync with HEAD.**  graphify's own hooks are
best-effort and structurally cannot cover every ref-moving operation:

* ``post-commit``/``post-checkout``/``post-merge`` all ``exit 0`` during a
  rebase, merge, or cherry-pick — so the merge agent's proactive rebase
  (#306), the single most common ref move in the fleet, never rebuilds.
* ``git reset --hard`` fires no rebuild hook at all; git has none.
* Every hook failure path is ``exit 0``, and the rebuild itself is a detached
  background process with a 600s ``SIGALRM`` timeout that logs to
  ``~/.cache/graphify-rebuild.log`` — a timeout, an OOM, or an ENOENT from a
  reaped worktree all fail invisibly.
* Concurrent triggers coalesce ("Rebuild already in progress — changes
  queued").
* The hooks' own ``[ ! -f graphify-out/graph.json ] && exit 0`` guard was a
  permanent off-switch: purge the graph once and they no-op forever.  Since
  #2237 the agent's health-tick self-heal rebuilds an **absent** graph as
  well as a stale one, so a ``rm -rf graphify-out/`` heals itself on the next
  idle poll — but the hooks themselves still no-op, which is why the heal
  cannot be delegated back to them.

So the hooks are an optimization, not the correctness mechanism.  Correctness
comes from a cheap *check*, which is possible because ``GRAPH_REPORT.md``
records the commit it was built from::

    - Built from commit: `5be69d08`

:func:`graph_status` compares that to HEAD.  ``coord diagnose --graph``
surfaces it, so drift shows up in a routine health check instead of being
discovered by a confused agent mid-task.

**3. HEAD itself drifts behind origin.**  A graph can match HEAD exactly and
still describe stale code when the base checkout is never pulled.
:func:`fast_forward_base_checkout` is the agent's periodic remedy: it
fast-forwards a base checkout's integration branch only when the checkout is
clean, on that branch, not mid-operation and not diverged. A dirty, parked or
diverged checkout is left exactly as it is and the reason is reported.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# ``- Built from commit: `5be69d08` `` in GRAPH_REPORT.md.  This is the only
# machine-readable record of the graph's source commit — manifest.json holds
# per-file hashes, not a commit.
_BUILT_FROM_RE = re.compile(r"^-\s*Built from commit:\s*`([0-9a-fA-F]+)`", re.MULTILINE)

# The versioned hooks directory this repo expects core.hooksPath to point at.
HOOKS_PATH = ".githooks"


@dataclass
class GraphStatus:
    """Freshness of one checkout's graphify graph."""

    repo_path: Path
    present: bool = False
    # True when graphify-out/graph.json is a symlink — i.e. a worktree
    # borrowing the base checkout's graph (the .githooks/post-checkout
    # bootstrap ran).  graphify-out/ itself is always a real directory (its
    # tracked .gitignore has to survive — see #1617); only the entries
    # inside it are symlinked.
    is_symlink: bool = False
    # The base checkout's graphify-out/ directory (graph.json's resolved
    # parent), not graph.json itself — kept as a directory path so existing
    # "owner checkout" math (``link_target.parent``) still lands on the base
    # checkout root.
    link_target: Path | None = None
    built_sha: str | None = None
    head_sha: str | None = None
    in_sync: bool = False
    # HEAD vs origin/<default_branch> — the axis graph<->HEAD alone cannot see
    # (#2211).  The base checkout is only fast-forwarded when it is clean and
    # on its integration branch (see module docstring), so HEAD can sit
    # arbitrarily far behind origin while
    # graph == HEAD reports a clean bill of health.  ``default_branch`` is the
    # branch name the comparison used; ``origin_sha`` / ``commits_behind_origin``
    # are ``None`` when it could not be determined (no remote, ref never
    # fetched, or the git call failed) rather than treated as "0 behind".
    default_branch: str | None = None
    origin_sha: str | None = None
    commits_behind_origin: int | None = None
    age_seconds: float | None = None
    # mtime of graphify-out/manifest.json — the last time graphify *checked* the
    # graph against the working tree, whether or not it rewrote anything.
    verified_at: float | None = None
    # Commit timestamp of HEAD, to compare against verified_at.
    head_committed_at: float | None = None
    # Set when we could not determine freshness at all (no report, no git).
    unknown_reason: str | None = None

    @property
    def stamp_behind(self) -> bool:
        """``GRAPH_REPORT.md``'s "Built from commit" differs from HEAD."""
        return bool(self.present and self.built_sha and self.head_sha and not self.in_sync)

    @property
    def verified_current(self) -> bool:
        """graphify has checked the graph against the tree since HEAD landed.

        ``graphify update`` re-extracts, compares topology, and when nothing
        changed prints "No code-graph topology changes detected; outputs left
        untouched" — it deliberately does NOT rewrite ``graph.json`` or
        ``GRAPH_REPORT.md``, so the "Built from commit" stamp stays at whatever
        HEAD was the last time the content actually changed.  It *does* still
        call ``save_manifest``, so ``manifest.json``'s mtime is the honest
        record of "last verified".

        Without this, a checkout whose graph is genuinely current but whose
        stamp is behind would be reported STALE on every run, forever — a
        health check that cries wolf is worse than none.
        """
        if self.verified_at is None or self.head_committed_at is None:
            return False
        return self.verified_at >= self.head_committed_at

    @property
    def stale(self) -> bool:
        """The stamp is behind AND graphify has not verified the graph against
        the tree since HEAD landed.  Deliberately False when freshness is
        *unknown* — an unknown is reported separately, not counted as drift."""
        return self.stamp_behind and not self.verified_current

    @property
    def origin_behind(self) -> bool:
        """HEAD (of the checkout that owns the graph) is behind
        ``origin/<default_branch>`` by at least one commit — i.e. the graph
        may match HEAD exactly and still describe stale code, because HEAD
        itself is stale relative to the remote (#2211).

        False — not True — when this could not be proven (no remote, the ref
        was never fetched, or the git call failed): mirrors ``stale``'s rule
        that an unknown must never be counted as drift.
        """
        return bool(self.commits_behind_origin)


def read_built_sha(report_path: Path) -> str | None:
    """The commit ``GRAPH_REPORT.md`` says the graph was built from."""
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _BUILT_FROM_RE.search(text)
    return m.group(1) if m else None


def _git_out(repo_path: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _head_sha(repo_path: Path) -> str | None:
    return _git_out(repo_path, "rev-parse", "HEAD")


def _head_committed_at(repo_path: Path) -> float | None:
    raw = _git_out(repo_path, "log", "-1", "--format=%ct", "HEAD")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _commits_ahead(repo_path: Path, base: str, ahead_of: str) -> int | None:
    """Count of commits reachable from *ahead_of* that are not in *base*
    (``git rev-list --count base..ahead_of``).

    Best-effort like the rest of this module: ``None`` (never raised) when
    git can't answer — an unproven comparison must not be reported as drift.
    """
    out = _git_out(repo_path, "rev-list", "--count", f"{base}..{ahead_of}")
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def _shas_agree(built: str, head: str) -> bool:
    """Compare on the shorter of the two — the report abbreviates (8 chars by
    default) while ``git rev-parse HEAD`` is full-length."""
    n = min(len(built), len(head))
    if n < 4:  # too short to be a meaningful comparison
        return False
    return built[:n].lower() == head[:n].lower()


def graph_status(repo_path: Path, default_branch: str = "main") -> GraphStatus:
    """Freshness of the graphify graph for the checkout at *repo_path*.

    Read-only and best-effort: a missing graph, a missing report, or a repo
    git can't read all return a populated :class:`GraphStatus` with
    ``unknown_reason`` set rather than raising.

    *default_branch* names the branch to compare HEAD against on
    ``origin`` (#2211) — pass the repo's configured default branch
    (``coordinator.yml``'s ``default_branch``, "main" if unset).  Never
    fetches: it only reads whatever ``origin/<default_branch>`` the last
    ``git fetch`` left behind, so a checkout that was never fetched or has
    no ``origin`` remote simply leaves ``origin_sha``/``commits_behind_origin``
    unset rather than reporting drift it can't prove.
    """
    st = GraphStatus(repo_path=repo_path)
    out_dir = repo_path / "graphify-out"
    graph_file = out_dir / "graph.json"

    # A borrowed graph symlinks graph.json (and friends) individually;
    # graphify-out/ itself is always a real directory (#1617).
    st.is_symlink = graph_file.is_symlink()
    if st.is_symlink:
        try:
            st.link_target = graph_file.resolve().parent
        except OSError:
            st.link_target = None

    # Which checkout OWNS the graph — the base checkout for a symlinked
    # worktree, this one otherwise. Hoisted above the absent-graph return
    # (#2237) so the "never built" branch can still name a HEAD.
    owner = st.link_target.parent if (st.is_symlink and st.link_target) else repo_path

    if not graph_file.is_file():
        st.unknown_reason = "no graphify-out/graph.json (graph never built here)"
        # #2237: HEAD is perfectly knowable with no graph on disk, and the
        # agent-side self-heal keys its once-per-HEAD "don't retry a build
        # that cannot succeed" bookkeeping on exactly this field. Returning
        # None here is what made an ABSENT graph un-healable: the heal would
        # either loop forever or (with the guard) never run at all. The
        # origin comparison is deliberately NOT done — it costs two more git
        # calls to answer a freshness question about a graph that does not
        # exist.
        st.head_sha = _head_sha(owner)
        st.default_branch = default_branch
        return st
    st.present = True

    try:
        st.age_seconds = max(0.0, time.time() - graph_file.stat().st_mtime)
    except OSError:
        st.age_seconds = None

    try:
        st.verified_at = (out_dir / "manifest.json").stat().st_mtime
    except OSError:
        st.verified_at = None

    st.built_sha = read_built_sha(out_dir / "GRAPH_REPORT.md")
    # Freshness is always judged against the checkout that OWNS the graph (see
    # `owner` above).  For a symlinked worktree that's the base checkout, not
    # the worktree's own HEAD — the worktree is on a feature branch by
    # definition and comparing against it would report permanent, meaningless
    # drift.
    st.head_sha = _head_sha(owner)
    st.head_committed_at = _head_committed_at(owner)
    st.default_branch = default_branch

    if not st.built_sha:
        st.unknown_reason = "GRAPH_REPORT.md has no 'Built from commit' line"
    elif not st.head_sha:
        st.unknown_reason = f"could not read HEAD of {owner}"
    else:
        st.in_sync = _shas_agree(st.built_sha, st.head_sha)

    # HEAD vs origin/<default_branch> — independent of the graph<->HEAD
    # comparison above.  Best-effort: no remote, or the ref not fetched yet,
    # just leaves this unset (see docstring).
    if st.head_sha:
        origin_sha = _git_out(owner, "rev-parse", f"origin/{default_branch}")
        if origin_sha:
            st.origin_sha = origin_sha
            st.commits_behind_origin = _commits_ahead(owner, st.head_sha, origin_sha)
    return st


def hooks_file_present(repo_path: Path) -> bool:
    """Does this repo TRACK ``.githooks/post-checkout`` at all?

    Split out of :func:`hooks_path_status` (#2236) so callers that only need
    "does the repo ship the hook file" — as opposed to the full
    ``core.hooksPath`` configuration story — have one place to ask, instead
    of independently re-running the same ``.is_file()`` check (a split-brain
    the two would otherwise be one edit away from disagreeing on).
    """
    return (repo_path / HOOKS_PATH / "post-checkout").is_file()


def hooks_path_status(repo_path: Path) -> tuple[bool, str]:
    """``(ok, detail)`` for this checkout's ``core.hooksPath``.

    The versioned ``.githooks/post-checkout`` bootstrap only runs when
    ``core.hooksPath`` points at it — a one-time per-machine ``git config``.
    Without it, worktrees on this machine stay graph-blind and nothing says so.
    """
    try:
        r = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"could not read core.hooksPath ({exc})"
    value = r.stdout.strip()
    have_hook = hooks_file_present(repo_path)
    if not value:
        if not have_hook:
            # The repo doesn't ship the bootstrap at all — telling the operator
            # to point core.hooksPath at a directory that isn't there would
            # silently disable ALL hooks for that checkout.
            return False, (
                f"no {HOOKS_PATH}/post-checkout in this repo — worktrees here get "
                f"no linked graph (port the hook to this repo to enable it)"
            )
        return False, (
            f"core.hooksPath is unset — worktrees on this machine will NOT get "
            f"a linked graph. Fix: git -C {repo_path} config core.hooksPath {HOOKS_PATH}"
        )
    if Path(value).name != Path(HOOKS_PATH).name:
        return False, (
            f"core.hooksPath is {value!r}, expected {HOOKS_PATH!r} — the worktree "
            f"graph bootstrap will not run"
        )
    if not have_hook:
        return False, (
            f"core.hooksPath={value} but {HOOKS_PATH}/post-checkout is missing "
            f"(stale checkout?)"
        )
    orphaned = orphaned_hooks(repo_path)
    if orphaned:
        return False, (
            f"core.hooksPath={value} but {', '.join(orphaned)} exist(s) only in "
            f".git/hooks — git no longer runs them, so those graphify rebuilds "
            f"are SILENTLY DISABLED. Add a shim in {HOOKS_PATH}/"
        )
    return True, f"core.hooksPath={value}"


def orphaned_hooks(repo_path: Path) -> list[str]:
    """Hooks installed in the machine-local hooks dir with no counterpart in
    :data:`HOOKS_PATH`.

    Setting ``core.hooksPath`` makes git ignore ``.git/hooks`` **entirely** —
    it does not merge or fall back.  So any hook graphify installed there
    (``post-commit``, ``post-checkout``, ``post-merge``) that has no shim in
    the versioned directory stops running, with no error and no log line.
    This shipped exactly once: only ``post-checkout`` had a shim, which
    silently killed graphify's commit- and merge-triggered rebuilds.

    Returns hook names, sorted.  Empty when nothing is orphaned (including
    when ``core.hooksPath`` isn't set — then ``.git/hooks`` is live and
    there is nothing to orphan).
    """
    local_dir = _git_out(repo_path, "rev-parse", "--git-common-dir")
    if not local_dir:
        return []
    common = Path(local_dir)
    if not common.is_absolute():
        common = repo_path / common
    hooks_dir = common / "hooks"
    versioned = repo_path / HOOKS_PATH
    if not hooks_dir.is_dir() or not versioned.is_dir():
        return []

    out: list[str] = []
    for entry in hooks_dir.iterdir():
        name = entry.name
        # graphify keeps .bak copies alongside; only real, executable hooks
        # matter, and only ones git would actually invoke.
        if name.endswith(".sample") or name.endswith(".bak") or "." in name:
            continue
        if not entry.is_file():
            continue
        if not (versioned / name).is_file():
            out.append(name)
    return sorted(out)


# ── The machine-local half: install, build, wire up (#2237) ─────────────────
#
# graphify onboarding has four layers (docs/GRAPHIFY_SETUP.md) and they split
# cleanly by owner:
#
#   VERSIONED   `.githooks/post-checkout` (+ `post-commit`, `post-merge`) are
#               tracked files. Porting them to a repo is a PR against that
#               repo. Report it; never automate it — a tool that silently
#               commits hooks into someone's repo is not a tool anyone wants.
#   MACHINE-LOCAL  `graphify update .` (writes gitignored `graphify-out/`) and
#               `git config core.hooksPath .githooks` (a per-checkout git
#               setting). Both are idempotent and re-runnable with no side
#               effects, which is what makes them safe to automate.
#
# Everything below is the machine-local half, and nothing below writes a
# tracked file.

# The command that BUILDS a graph from nothing is the same one that refreshes
# one: `graphify update .` (AST-only, no LLM, no API key — see
# docs/GRAPHIFY_SETUP.md). There is no `graphify build` subcommand; fix strings
# that named one (#2220's doctor output) told operators to run a command that
# does not exist.
GRAPHIFY_BUILD_HINT = "graphify update ."


@dataclass
class GraphFixStep:
    """One machine-local repair attempt. ``changed=False`` with ``ok=True``
    means "already in the desired state" — the idempotent no-op, which must
    read differently from "repaired it just now"."""

    action: str  # "hooks_path" | "build"
    ok: bool
    changed: bool
    detail: str

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "ok": self.ok,
            "changed": self.changed,
            "detail": self.detail,
        }


@dataclass
class GraphFixResult:
    """Outcome of :func:`apply_local_graph_fix` for one checkout.

    ``refused`` is not a failure and not a success: it is the machine-local
    fixer declining to act because the *versioned* half is missing (see the
    section comment above). Building a graph in a repo whose hooks were never
    ported produces a graph that immediately starts going stale with nothing
    to heal it — a worse state to be in than "obviously absent", because it
    looks fixed.
    """

    repo_path: str
    refused: str | None = None
    steps: list[GraphFixStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.refused is None and all(s.ok for s in self.steps)

    @property
    def changed(self) -> bool:
        return any(s.changed for s in self.steps)

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "refused": self.refused,
            "ok": self.ok,
            "changed": self.changed,
            "steps": [s.to_dict() for s in self.steps],
        }


def graphify_cli_path() -> str | None:
    """Absolute path to the ``graphify`` CLI on this machine, or ``None``.

    Layer 1 of the four (pipx CLI -> built graph -> hooks -> ``core.hooksPath``)
    and the only one whose absence makes every other layer unfixable here:
    with no binary, both the self-heal and ``coord repo doctor --fix`` fail
    per-checkout with "command not found", which is recorded once per HEAD and
    then goes quiet (#2237 item 6). Asking once, at machine scope, turns that
    into a single finding.
    """
    import shutil  # noqa: PLC0415 — one call, keep module import-light

    return shutil.which("graphify")


def run_graphify_update(
    repo_path: Path, *, timeout: float = 600.0
) -> tuple[bool, str]:
    """Run ``graphify update .`` in *repo_path*; return ``(ok, detail)``.

    The one place in coord that shells out to graphify to build or refresh a
    graph — the agent's self-heal (:func:`coord.agent._graphify_update`) and
    ``coord repo doctor --fix`` both come through here, so there is exactly
    one answer to "what command does coord run, with what flags".

    Deliberately the plain, no-flags command graphify's own hooks run —
    **never** ``--force``: that flag exists only to defeat graphify's
    node-count refusal guard, and defeating it automatically is what turns a
    visible stall into silent corruption of the graph agents navigate by
    (#1729 guard 4).

    ``ok`` is False on a non-zero exit, a missing ``graphify`` binary, or a
    timeout — callers treat all three the same way: surface the reason and
    remember this HEAD as attempted, rather than retrying forever.
    """
    try:
        result = subprocess.run(
            ["graphify", "update", "."],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "graphify: command not found on this machine's PATH"
    except subprocess.TimeoutExpired:
        return False, f"graphify update . timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, f"graphify update . failed to start: {exc}"

    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "").strip()
        return False, reason or f"graphify update . exited {result.returncode}"
    return True, (result.stdout or "").strip()


def apply_local_graph_fix(
    repo_path: Path, *, build: bool = True, timeout: float = 600.0
) -> GraphFixResult:
    """Repair the machine-local half of graphify for the checkout at *repo_path*.

    Two idempotent steps, in the order ``docs/GRAPHIFY_SETUP.md`` requires:

    1. ``core.hooksPath`` -> ``.githooks``, so future commits/checkouts keep
       the graph fresh and worktrees get their symlinked copy.
    2. ``graphify update .`` when there is no graph here yet (or *build* is
       forced), so the hooks have something to maintain — their own
       ``[ ! -f graphify-out/graph.json ] && exit 0`` guard means a repo with
       hooks and no graph stays graph-less forever.

    **Refuses** — doing neither step — when the repo does not ship
    ``.githooks/post-checkout``. Pointing ``core.hooksPath`` at a directory
    that does not exist silently disables *all* hooks for the checkout, and
    building a graph nothing will ever refresh just swaps a loud absence for a
    quiet staleness. The versioned port is a PR against that repo; this
    reports it as remaining work (see :class:`GraphFixResult`).

    Never raises, never touches a tracked file, and safe to run repeatedly:
    a checkout already in the desired state comes back ``ok`` with
    ``changed=False``.
    """
    result = GraphFixResult(repo_path=str(repo_path))

    if not (repo_path / ".git").exists():
        result.refused = f"no git checkout at {repo_path}"
        return result

    if not hooks_file_present(repo_path):
        result.refused = (
            f"repo does not ship {HOOKS_PATH}/post-checkout — port the hooks "
            f"first (a PR against that repo; `graphify hook install` then copy "
            f"the shims into {HOOKS_PATH}/). Setting core.hooksPath at a "
            f"directory that does not exist disables ALL hooks for this "
            f"checkout, and a graph nothing refreshes just goes stale quietly."
        )
        return result

    # Step 1 — core.hooksPath. Cheap, and ordered first so that even a failed
    # build leaves the checkout ready to heal itself on the next commit.
    hooks_ok, hooks_detail = hooks_path_status(repo_path)
    if hooks_ok:
        result.steps.append(
            GraphFixStep("hooks_path", ok=True, changed=False, detail=hooks_detail)
        )
    else:
        current = _git_out(repo_path, "config", "--get", "core.hooksPath")
        if current and Path(current).name != Path(HOOKS_PATH).name:
            # Someone pointed this checkout somewhere deliberate. Overwriting
            # another tool's hooks directory is not a repair, it is a
            # hijacking — report and move on.
            result.steps.append(
                GraphFixStep(
                    "hooks_path", ok=False, changed=False,
                    detail=(
                        f"core.hooksPath is {current!r}, not {HOOKS_PATH!r} — left "
                        f"alone (another tool may own it); fix by hand if intended"
                    ),
                )
            )
        else:
            set_ok = _git_out(repo_path, "config", "core.hooksPath", HOOKS_PATH)
            # `git config <k> <v>` prints nothing on success, so `_git_out`
            # returns None either way — re-read to prove it took.
            del set_ok
            now_ok, now_detail = hooks_path_status(repo_path)
            result.steps.append(
                GraphFixStep(
                    "hooks_path", ok=now_ok, changed=now_ok,
                    detail=(
                        f"set core.hooksPath={HOOKS_PATH}" if now_ok else now_detail
                    ),
                )
            )

    # Step 2 — the graph itself.
    status = graph_status(repo_path)
    if status.present and not build:
        result.steps.append(
            GraphFixStep(
                "build", ok=True, changed=False,
                detail=f"graph already present (built from {status.built_sha or '?'})",
            )
        )
        return result
    if status.present and not status.stale:
        result.steps.append(
            GraphFixStep(
                "build", ok=True, changed=False,
                detail=f"graph already current (built from {status.built_sha or '?'})",
            )
        )
        return result

    ok, detail = run_graphify_update(repo_path, timeout=timeout)
    result.steps.append(
        GraphFixStep(
            "build", ok=ok, changed=ok,
            detail=(detail or GRAPHIFY_BUILD_HINT) if not ok else f"ran {GRAPHIFY_BUILD_HINT}",
        )
    )
    return result


def format_status_lines(st: GraphStatus) -> list[str]:
    """Human-readable report lines for *st* (used by ``coord diagnose --graph``)."""
    lines: list[str] = []
    where = str(st.repo_path)
    if not st.present:
        lines.append(f"✗ {where}: {st.unknown_reason}")
        return lines

    if st.is_symlink:
        lines.append(f"↳ {where}/graphify-out → {st.link_target} (linked worktree)")

    # #2211: graph == HEAD only proves the graph matches the checkout's own
    # HEAD — it says nothing about whether that HEAD itself is stale relative
    # to origin (a dirty or parked base checkout is never fast-forwarded;
    # see module docstring).  Shared by both "graph matches HEAD" branches
    # below so a genuinely-current-but-unpushed-tracking checkout doesn't
    # report a false ✓.
    origin_note = ""
    if st.origin_behind:
        n = st.commits_behind_origin or 0
        origin_note = (
            f" — HEAD is {n} commit{'' if n == 1 else 's'} behind "
            f"origin/{st.default_branch}; the graph describes stale code "
            f"(the agent fast-forwards a clean base checkout on its integration "
            f"branch; a dirty or off-branch one is left alone — check "
            f"`coord doctor`'s graph refresh line)"
        )

    if st.unknown_reason:
        lines.append(f"? {where}: freshness unknown — {st.unknown_reason}")
    elif st.in_sync:
        if origin_note:
            lines.append(
                f"⚠ {where}: graph matches HEAD (built from {st.built_sha}){origin_note}"
            )
        else:
            lines.append(f"✓ {where}: graph in sync (built from {st.built_sha})")
    elif st.verified_current:
        # Stamp behind, but graphify has re-checked the tree since HEAD landed
        # and found no topology change — the graph content is current.
        mark = "⚠" if origin_note else "✓"
        lines.append(
            f"{mark} {where}: graph content current — stamp says {st.built_sha} "
            f"(HEAD {(st.head_sha or '')[:8]}), but verified against the tree "
            f"since; graphify leaves outputs untouched when topology is "
            f"unchanged{origin_note}"
        )
    else:
        lines.append(
            f"⚠ {where}: graph is STALE — built from {st.built_sha}, "
            f"HEAD is {(st.head_sha or '')[:8]}"
        )
    if st.age_seconds is not None:
        lines.append(f"    graph.json age: {st.age_seconds / 3600.0:.1f}h")
    return lines


# ── Keeping the base checkout itself current ────────────────────────────────
#
# A graph that matches HEAD exactly still describes stale code when HEAD is
# behind origin, and worktrees borrow the base checkout's graph, so every
# worker on the machine navigates by it. The agent therefore fast-forwards
# each base checkout's integration branch on a timer — but only when that is
# provably a pure ref move nobody could object to. Every guard below fails
# closed and leaves the checkout exactly as it was; the outcome is reported
# instead, so a parked or dirty checkout is visible rather than silently stale.

REFRESH_FAST_FORWARDED = "fast_forwarded"
REFRESH_UP_TO_DATE = "up_to_date"
REFRESH_SKIPPED_LINKED_WORKTREE = "skipped_linked_worktree"
REFRESH_SKIPPED_OFF_BRANCH = "skipped_off_branch"
REFRESH_SKIPPED_IN_PROGRESS = "skipped_in_progress"
REFRESH_SKIPPED_DIRTY = "skipped_dirty"
REFRESH_SKIPPED_DIVERGED = "skipped_diverged"
REFRESH_NO_ORIGIN = "no_origin"
REFRESH_FETCH_FAILED = "fetch_failed"
REFRESH_FF_FAILED = "ff_failed"

# Outcomes where the checkout was deliberately left alone because a human
# (or an in-flight git operation) owns its state. Reported, not failures.
REFRESH_LEFT_UNTOUCHED: frozenset[str] = frozenset({
    REFRESH_SKIPPED_LINKED_WORKTREE,
    REFRESH_SKIPPED_OFF_BRANCH,
    REFRESH_SKIPPED_IN_PROGRESS,
    REFRESH_SKIPPED_DIRTY,
    REFRESH_SKIPPED_DIVERGED,
    REFRESH_NO_ORIGIN,
})

# Outcomes where the refresh was attempted and git refused or errored.
REFRESH_FAILED: frozenset[str] = frozenset({REFRESH_FETCH_FAILED, REFRESH_FF_FAILED})

# Files in the git dir whose presence means a multi-step git operation is
# mid-flight in this checkout; moving HEAD underneath one corrupts it.
_IN_PROGRESS_MARKERS: tuple[str, ...] = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
    "index.lock",
)


@dataclass
class BaseRefreshResult:
    """Outcome of one :func:`fast_forward_base_checkout` call."""

    repo_path: str
    outcome: str
    branch: str | None = None
    head_before: str | None = None
    head_after: str | None = None
    origin_sha: str | None = None
    detail: str = ""
    at: float = field(default_factory=time.time)

    @property
    def refreshed(self) -> bool:
        return self.outcome == REFRESH_FAST_FORWARDED

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "outcome": self.outcome,
            "branch": self.branch,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "origin_sha": self.origin_sha,
            "detail": self.detail,
            "at": self.at,
        }


def _git_run(
    repo_path: Path, *args: str, timeout: float = 10.0
) -> tuple[int, str, str]:
    """``(returncode, stdout, stderr)`` for ``git <args>`` in *repo_path*.

    Unlike :func:`_git_out` this keeps the failure detail, so a refused
    fast-forward can say why. A git that cannot be started or times out is
    reported as returncode ``-1`` with the reason in stderr — never raised.
    """
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"git {' '.join(args)} timed out after {timeout:.0f}s"
    except (subprocess.SubprocessError, OSError) as exc:
        return -1, "", f"git {' '.join(args)} failed to start: {exc}"
    return r.returncode, r.stdout.rstrip(), r.stderr.strip()


def _resolve_git_path(repo_path: Path, raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = repo_path / p
    try:
        return p.resolve()
    except OSError:
        return p


def is_linked_worktree(repo_path: Path) -> bool | None:
    """True for a linked ``git worktree``, False for the checkout that owns
    its ``.git`` directory, ``None`` when git cannot say.

    Same predicate as ``.githooks/_lib.sh``'s ``gfy_is_linked_worktree``: a
    linked worktree's ``--git-dir`` is a subdirectory of ``--git-common-dir``,
    while a base checkout's two are the same path.
    """
    git_dir = _git_out(repo_path, "rev-parse", "--git-dir")
    common_dir = _git_out(repo_path, "rev-parse", "--git-common-dir")
    if not git_dir or not common_dir:
        return None
    return _resolve_git_path(repo_path, git_dir) != _resolve_git_path(repo_path, common_dir)


def fast_forward_base_checkout(
    repo_path: Path,
    home_branches: tuple[str, ...],
    *,
    fetch: bool = True,
    fetch_timeout: float = 60.0,
) -> BaseRefreshResult:
    """Fast-forward the base checkout at *repo_path* to its ``origin`` branch
    when — and only when — that cannot lose or disturb anything.

    *home_branches* are the repo's integration branches (its configured
    ``default_branch``, plus ``develop_branch`` when set). The checkout must
    currently be on one of them; that branch is the one fetched and
    fast-forwarded. Guards, in order, each of which leaves the checkout
    untouched and names itself in the returned ``outcome``:

    * a linked worktree is never a base checkout (``skipped_linked_worktree``);
    * detached HEAD or a branch outside *home_branches* means someone parked
      it there on purpose (``skipped_off_branch``);
    * a merge, rebase, cherry-pick, revert, bisect or ``index.lock`` in the
      git dir means an operation is mid-flight (``skipped_in_progress``);
    * any modified or staged **tracked** file (``skipped_dirty``). Untracked
      files are deliberately allowed: ``git merge --ff-only`` itself refuses
      to overwrite an untracked file, so they cannot be clobbered, and base
      checkouts routinely carry untracked test output;
    * no ``origin`` remote (``no_origin``);
    * the fetch fails or times out (``fetch_failed``);
    * local commits not on origin (``skipped_diverged``) — never reset.

    Only then ``git merge --ff-only origin/<branch>`` runs. No ``--force``,
    ``reset``, ``stash`` or ``checkout`` is ever used. Never raises.
    """
    path_str = str(repo_path)

    def _result(outcome: str, **kw) -> BaseRefreshResult:
        return BaseRefreshResult(repo_path=path_str, outcome=outcome, **kw)

    linked = is_linked_worktree(repo_path)
    if linked is None:
        return _result(REFRESH_FF_FAILED, detail="git cannot read this checkout")
    if linked:
        return _result(
            REFRESH_SKIPPED_LINKED_WORKTREE,
            detail="linked worktree — only base checkouts are refreshed",
        )

    head_before = _head_sha(repo_path)
    rc, ref, _ = _git_run(repo_path, "symbolic-ref", "--quiet", "HEAD")
    branch = ref[len("refs/heads/"):] if rc == 0 and ref.startswith("refs/heads/") else None
    if branch is None:
        return _result(
            REFRESH_SKIPPED_OFF_BRANCH, head_before=head_before, head_after=head_before,
            detail=f"detached HEAD — integration branch is {'/'.join(home_branches)}",
        )
    if branch not in home_branches:
        return _result(
            REFRESH_SKIPPED_OFF_BRANCH, branch=branch,
            head_before=head_before, head_after=head_before,
            detail=f"on {branch!r}, not {'/'.join(home_branches)}",
        )

    def _untouched(outcome: str, detail: str, **kw) -> BaseRefreshResult:
        return _result(
            outcome, branch=branch, head_before=head_before, head_after=head_before,
            detail=detail, **kw,
        )

    git_dir_raw = _git_out(repo_path, "rev-parse", "--git-dir")
    if git_dir_raw:
        git_dir = _resolve_git_path(repo_path, git_dir_raw)
        busy = [m for m in _IN_PROGRESS_MARKERS if (git_dir / m).exists()]
        if busy:
            return _untouched(
                REFRESH_SKIPPED_IN_PROGRESS,
                f"git operation in progress ({', '.join(busy)})",
            )

    rc, porcelain, err = _git_run(
        repo_path, "status", "--porcelain", "--untracked-files=no"
    )
    if rc != 0:
        return _untouched(REFRESH_SKIPPED_DIRTY, f"git status failed: {err}")
    if porcelain:
        changed = [line[3:] for line in porcelain.splitlines() if line.strip()]
        shown = ", ".join(changed[:3])
        more = f" (+{len(changed) - 3} more)" if len(changed) > 3 else ""
        return _untouched(
            REFRESH_SKIPPED_DIRTY, f"uncommitted changes to tracked files: {shown}{more}"
        )

    if _git_out(repo_path, "remote", "get-url", "origin") is None:
        return _untouched(REFRESH_NO_ORIGIN, "no origin remote")

    if fetch:
        rc, _, err = _git_run(
            repo_path, "fetch", "--quiet", "origin", branch, timeout=fetch_timeout
        )
        if rc != 0:
            return _untouched(REFRESH_FETCH_FAILED, err or f"git fetch exited {rc}")

    origin_sha = _git_out(repo_path, "rev-parse", f"refs/remotes/origin/{branch}")
    if not origin_sha:
        return _untouched(REFRESH_FETCH_FAILED, f"origin/{branch} not found after fetch")
    if head_before and head_before == origin_sha:
        return _untouched(
            REFRESH_UP_TO_DATE, f"at origin/{branch}", origin_sha=origin_sha
        )

    rc, _, _ = _git_run(repo_path, "merge-base", "--is-ancestor", "HEAD", origin_sha)
    if rc != 0:
        ahead = _commits_ahead(repo_path, origin_sha, "HEAD")
        return _untouched(
            REFRESH_SKIPPED_DIVERGED,
            f"{ahead if ahead is not None else 'some'} local commit(s) not on "
            f"origin/{branch} — fast-forward impossible, left as is",
            origin_sha=origin_sha,
        )

    rc, _, err = _git_run(repo_path, "merge", "--ff-only", "--quiet", origin_sha)
    if rc != 0:
        return _result(
            REFRESH_FF_FAILED, branch=branch, head_before=head_before,
            head_after=_head_sha(repo_path), origin_sha=origin_sha,
            detail=err or f"git merge --ff-only exited {rc}",
        )
    head_after = _head_sha(repo_path)
    n = _commits_ahead(repo_path, head_before, head_after) if head_before and head_after else None
    return _result(
        REFRESH_FAST_FORWARDED, branch=branch, head_before=head_before,
        head_after=head_after, origin_sha=origin_sha,
        detail=(
            f"fast-forwarded {n} commit{'' if n == 1 else 's'} to origin/{branch}"
            if n is not None else f"fast-forwarded to origin/{branch}"
        ),
    )
