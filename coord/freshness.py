"""Compare agent-reported local repo state against GitHub HEADs.

Pure logic — no IO. Callers pass in the data they fetched from agents and
GitHub; this module decides what's stale, current, dirty, or unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from coord.config import Config
from coord.deps import build_dep_graph, transitive_deps
from coord.models import Board, Proposal

CURRENT = "current"
STALE = "stale"
DIRTY = "dirty"
MISSING = "missing"
UNKNOWN = "unknown"

# #268: kind of cross-repo relationship a freshness entry came from.
# `build` = transitive `depends_on` — required for the build.
# `reference` = `reference_repos` — pulled for context only.
BUILD = "build"
REFERENCE = "reference"


@dataclass
class RepoFreshness:
    repo_name: str
    state: str  # one of the constants above
    local_sha: str | None = None
    remote_sha: str | None = None
    branch: str | None = None
    dirty: bool = False
    error: str | None = None
    # #268: where this entry came from in `coordinator.yml`.  Drives the
    # briefing addendum so the worker knows whether to actually USE the
    # pulled code or just READ it for context.  Defaults to `BUILD` for
    # back-compat with existing call sites that don't supply a kind.
    kind: str = BUILD

    @property
    def needs_pull(self) -> bool:
        return self.state == STALE


def compare(
    repo_name: str,
    local_info: dict | None,
    remote_sha: str | None,
    *,
    kind: str = BUILD,
) -> RepoFreshness:
    """Classify one repo given its agent-reported local info and the remote SHA.

    `local_info` is the dict returned by `AgentServer.list_repos()[repo_name]`
    (or None if the agent had no entry). `remote_sha` is the full SHA on
    GitHub's default branch (or None if lookup failed).

    `kind` (#268) tags the result as either a build dep or a reference
    repo so the briefing addendum can label each entry appropriately.
    """
    if local_info is None:
        return RepoFreshness(repo_name=repo_name, state=MISSING, remote_sha=remote_sha,
                             error="agent did not report this repo", kind=kind)

    error = local_info.get("error")
    if error:
        return RepoFreshness(
            repo_name=repo_name,
            state=MISSING,
            remote_sha=remote_sha,
            error=error,
            kind=kind,
        )

    local_sha = local_info.get("sha")
    branch = local_info.get("branch")
    dirty = bool(local_info.get("dirty"))

    if remote_sha is None:
        return RepoFreshness(
            repo_name=repo_name,
            state=UNKNOWN,
            local_sha=local_sha,
            branch=branch,
            dirty=dirty,
            error="remote head not available",
            kind=kind,
        )

    if dirty:
        return RepoFreshness(
            repo_name=repo_name,
            state=DIRTY,
            local_sha=local_sha,
            remote_sha=remote_sha,
            branch=branch,
            dirty=True,
            kind=kind,
        )

    state = CURRENT if local_sha == remote_sha else STALE
    return RepoFreshness(
        repo_name=repo_name,
        state=state,
        local_sha=local_sha,
        remote_sha=remote_sha,
        branch=branch,
        kind=kind,
    )


def relevant_repos(proposal: Proposal, config: Config) -> list[tuple[str, str]]:
    """#268: compute the cross-repo set to freshen for *proposal*.

    Returns ``[(repo_name, kind)]`` pairs covering:

    - **Transitive `depends_on`** of the proposal's repo (tagged `BUILD`).
      These walk the graph because A depending on B which depends on C
      means a stale C poisons A's build.
    - **Direct `reference_repos`** of the proposal's repo (tagged
      `REFERENCE`).  Reference entries do NOT walk transitively — they
      describe a flat "you may want to look at these" list, not a build
      dependency.

    Entries that appear both as a build dep and a reference are
    de-duplicated and kept as `BUILD` (the stricter constraint wins).
    Sorted by name for stable output.
    """
    graph = build_dep_graph(config.repos)
    build_set = transitive_deps(proposal.repo_name, graph)

    own = config.repo(proposal.repo_name)
    ref_set: set[str] = set()
    if own is not None:
        # Drop refs that are already build deps so they aren't tagged twice.
        ref_set = {r for r in own.reference_repos if r not in build_set}

    pairs: list[tuple[str, str]] = []
    for name in sorted(build_set):
        pairs.append((name, BUILD))
    for name in sorted(ref_set):
        pairs.append((name, REFERENCE))
    return pairs


def dependency_freshness(
    proposal: Proposal,
    config: Config,
    repo_heads: dict[str, dict],
    github_heads: dict[str, str | None],
) -> list[RepoFreshness]:
    """Freshness for every cross-repo entry relevant to *proposal*.

    Covers the transitive `depends_on` set (build deps) AND the direct
    `reference_repos` set (context, #268).  ``repo_heads`` is what the
    target agent returned from /repos; ``github_heads`` maps repo_name
    to remote SHA (None when the lookup failed).
    """
    return [
        compare(name, repo_heads.get(name), github_heads.get(name), kind=kind)
        for name, kind in relevant_repos(proposal, config)
    ]


def stale_or_dirty(freshness: Iterable[RepoFreshness]) -> list[RepoFreshness]:
    """Filter to entries that need user attention."""
    return [f for f in freshness if f.state in (STALE, DIRTY, MISSING)]


def repo_busy_elsewhere(
    dep_repo_name: str,
    machine_name: str,
    config: Config,
    board: Board,
    *,
    also_running: Iterable[str] = (),
) -> bool:
    """True if *dep_repo_name*'s single shared checkout on *machine_name* is
    a build input some OTHER assignment may be relying on right now (#2804).

    A repo "relies on" *dep_repo_name* if it EITHER is *dep_repo_name* —
    someone is actively working the dependency itself — OR *dep_repo_name*
    is in its transitive ``depends_on`` set, meaning that repo's build reads
    *dep_repo_name*'s on-disk checkout directly (a relative sibling path,
    e.g. vimcode's `quadraui-pin.txt` convention, #638/#625). Either way,
    that checkout is not this dispatch's alone to `git pull`/`checkout` —
    doing so changes a concurrent build's inputs mid-flight, which is
    exactly the scheduling-dependent phantom pin-mismatch #2804 is about:
    the fix worker rebuilding against a *moved* `~/src/quadraui` reported a
    red that had nothing to do with its own branch.

    Checks the board's own ``active`` assignments with ``status ==
    "running"`` on *machine_name*, plus *also_running* — repo names for
    sibling proposals in the SAME dispatch batch that have not reached the
    board yet (a `coord approve` of several proposals at once can put two
    dependents of the same repo on one machine before either is recorded
    there).

    Pure/read-only: this only answers "would touching it be unsafe right
    now" — the callers (`coord approve` / `coord assign`) are the ones that
    decide to skip the pull.
    """
    graph = build_dep_graph(config.repos)

    def _touches(repo_name: str) -> bool:
        return repo_name == dep_repo_name or dep_repo_name in transitive_deps(repo_name, graph)

    for a in board.active:
        if a.status == "running" and a.machine_name == machine_name and _touches(a.repo_name):
            return True
    return any(_touches(name) for name in also_running)


def format_briefing_addendum(
    freshness: list[RepoFreshness],
    *,
    busy: Iterable[str] = (),
) -> str:
    """Markdown block to append to a worker's briefing when deps are stale.

    #268: build deps are tagged with **(build dep)** so the worker knows
    to actually pull + build against them; reference repos are tagged
    **(reference)** so the worker knows it can read them for context but
    shouldn't expect them to be part of the build.

    #2804: *busy* names build deps whose single shared checkout some OTHER
    assignment on this machine may be building against right now (see
    :func:`repo_busy_elsewhere`). Those entries must never be told to pull
    — the checkout isn't this worker's alone, and moving it is exactly the
    scheduling-dependent phantom #2804 is about. They get a distinct
    "do not touch" instruction instead of the ordinary pull line.
    """
    needs = stale_or_dirty(freshness)
    if not needs:
        return ""
    busy_set = set(busy)
    lines = [
        "",
        "### Stale dependencies",
        "Entries tagged *(reference)* are not part of this repo's build — "
        "they're siblings the worker may want to read for context. "
        "Otherwise, pull before building — UNLESS an entry says not to "
        "(#2804): some are a single checkout shared with a concurrent "
        "assignment on this machine, and pulling one out from under that "
        "assignment is not this worker's call to make.",
    ]
    for f in needs:
        tag = " *(reference)*" if f.kind == REFERENCE else " *(build dep)*"
        if f.kind == BUILD and f.repo_name in busy_set:
            lines.append(
                f"- `{f.repo_name}`{tag}: stale, but another assignment on "
                "this machine may be building against its shared checkout "
                "right now — do NOT `git pull`/`checkout`/symlink-swap it "
                "(#2804). Build against whatever is currently checked out "
                "there as-is; a pin-mismatch you hit anyway is a real "
                "disagreement to report, not an artifact to fix by moving "
                "the shared checkout."
            )
        elif f.state == STALE:
            lines.append(
                f"- `{f.repo_name}`{tag} (local {f.local_sha[:7] if f.local_sha else '?'} "
                f"→ remote {f.remote_sha[:7] if f.remote_sha else '?'})"
            )
        elif f.state == DIRTY:
            lines.append(
                f"- `{f.repo_name}`{tag} is **dirty** on the agent — "
                "uncommitted changes; do not pull, surface to operator"
            )
        else:
            lines.append(f"- `{f.repo_name}`{tag}: {f.error or 'unknown state'}")
    return "\n".join(lines)
