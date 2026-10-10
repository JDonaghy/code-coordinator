"""Tests for graphify graph freshness + the worktree-bootstrap hook.

Two tiers:

* **Black-box** — the acceptance bar for the user-visible behaviour here.
  ``.githooks/post-checkout`` is driven through *real git*: a real repo, a
  real ``git worktree add``, asserting on what actually lands on disk.  The
  hook is a shell script git invokes; unit-testing around it would prove
  nothing about whether git runs it or whether it does the right thing when
  it does.
* **Unit** — :mod:`coord.graph_health`'s parsing/comparison logic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coord.graph_health import (
    GraphStatus,
    fast_forward_base_checkout,
    graph_status,
    hooks_file_present,
    hooks_path_status,
    read_built_sha,
)

HOOK_SRC = Path(__file__).resolve().parents[1] / ".githooks" / "post-checkout"


# ── helpers ──────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _make_repo(root: Path, *, with_hooks: bool = True) -> Path:
    """A real git repo with the versioned hook wired up via core.hooksPath.

    The hook is **committed**, not just written to the worktree.  Git resolves
    a relative ``core.hooksPath`` against the directory the git command runs
    in, so an uncommitted ``.githooks/`` still fires for a ``git worktree add``
    issued from the base — but NOT for a checkout issued from inside the
    worktree, where ``.githooks/`` wouldn't exist.  Committing it is what the
    real repo does and is what keeps the in-worktree tests below honest
    (without it they pass vacuously, asserting on a hook that never ran).
    """
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", ".", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    if with_hooks:
        # Install the REAL hook set, not just post-checkout: the hooks source
        # _lib.sh, and copying one file in isolation would test a shape that
        # does not exist.
        shutil.copytree(HOOK_SRC.parent, root / ".githooks")
        for p in (root / ".githooks").iterdir():
            if not p.name.endswith(".sh"):
                p.chmod(0o755)
        _git("add", "-f", ".githooks", cwd=root)
        _git("commit", "-q", "-m", "add versioned hooks", cwd=root)
        _git("config", "core.hooksPath", ".githooks", cwd=root)
    # graphify-out/.gitignore is tracked in the real repo REGARDLESS of
    # whether a graph has been built yet — it's what makes graphify-out/
    # self-ignoring.  Fixtures that skip this pass vacuously on any
    # `git status` assertion, which is exactly how #1617 shipped.
    out = root / "graphify-out"
    out.mkdir(exist_ok=True)
    (out / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    _git("add", "-f", "graphify-out/.gitignore", cwd=root)
    _git("commit", "-q", "-m", "track graphify-out/.gitignore", cwd=root)
    return root


def _seed_graph(repo: Path, *, built_sha: str = "deadbeef", manifest: bool = True) -> Path:
    """A minimal but realistic graphify-out/ (graph.json + report)."""
    out = repo / "graphify-out"
    out.mkdir(exist_ok=True)
    (out / "graph.json").write_text('{"nodes": []}', encoding="utf-8")
    (out / "GRAPH_REPORT.md").write_text(
        "# Graph Report\n\n"
        "## Graph Freshness\n"
        f"- Built from commit: `{built_sha}`\n",
        encoding="utf-8",
    )
    if manifest:
        (out / "manifest.json").write_text("{}", encoding="utf-8")
    return out


def _head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


# ── black-box: the hook, driven through real git ─────────────────────────────


def test_the_checked_in_hook_is_executable() -> None:
    """Git SILENTLY ignores a non-executable hook — it prints an advice hint at
    most, then carries on as if no hook existed.  This shipped broken once
    already (the file was created without the bit; every test still passed
    because the fixture chmod's its own copy), so assert on the real file and
    on the mode git has recorded for it."""
    assert HOOK_SRC.is_file(), f"{HOOK_SRC} is missing"
    assert os.access(HOOK_SRC, os.X_OK), (
        f"{HOOK_SRC} is not executable — git will ignore it entirely"
    )
    repo_root = HOOK_SRC.parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "-s", ".githooks/post-checkout"],
        cwd=str(repo_root), capture_output=True, text=True, timeout=10.0,
    ).stdout.strip()
    if tracked:  # skip the assertion until the file is first committed
        assert tracked.startswith("100755"), (
            f"git has the hook recorded as {tracked.split()[0]}, not mode 100755 — "
            "it will be checked out non-executable and silently ignored"
        )


def test_worktree_add_links_graphify_out_to_the_base_graph(tmp_path: Path) -> None:
    """The core behaviour: `git worktree add` leaves the worktree's
    graphify-out/ populated with symlinks into the base checkout's graph, so
    an agent working there can query it — and, per #1617, `graphify-out/`
    itself stays a real, git-clean directory rather than becoming a
    machine-local symlink."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)

    wt = tmp_path / "wt"
    res = _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    assert res.returncode == 0, res.stderr

    out = wt / "graphify-out"
    assert out.is_dir() and not out.is_symlink(), (
        "graphify-out/ must stay a real directory, not become a symlink"
    )
    graph_link = out / "graph.json"
    assert graph_link.is_symlink(), "graph.json should be linked to the base graph"
    assert graph_link.resolve() == (base / "graphify-out" / "graph.json").resolve()
    # And it is actually usable — the graph resolves through the link.
    assert graph_link.is_file()


def test_worktree_add_git_status_is_empty(tmp_path: Path) -> None:
    """The acceptance bar for #1617: a fresh linked worktree must be
    `git status` clean immediately after `git worktree add` runs the hook.
    The original bug showed up here as a deleted tracked
    `graphify-out/.gitignore` plus a new untracked, absolute-path symlink —
    both invisible to any check that only looks at what the symlink points
    to, which is why this specific assertion is the whole point of the
    issue."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)

    status = _git("status", "--porcelain", cwd=wt)
    assert status.returncode == 0, status.stderr
    assert status.stdout == "", (
        f"expected a clean worktree, got:\n{status.stdout}"
    )


def test_worktree_link_preserves_the_tracked_gitignore(tmp_path: Path) -> None:
    """`graphify-out/.gitignore` is tracked, so `git worktree add` materialises
    a non-empty stub directory.  The hook must add symlinks alongside it
    without ever deleting or shadowing the tracked file."""
    base = _make_repo(tmp_path / "base")  # tracks graphify-out/.gitignore
    _seed_graph(base)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)

    gitignore = wt / "graphify-out" / ".gitignore"
    assert gitignore.is_file() and not gitignore.is_symlink()
    assert "!.gitignore" in gitignore.read_text(encoding="utf-8")
    tracked = _git("ls-files", "graphify-out/.gitignore", cwd=wt).stdout.strip()
    assert tracked == "graphify-out/.gitignore"
    assert (wt / "graphify-out" / "graph.json").is_file()


def test_no_link_when_the_base_checkout_has_no_graph(tmp_path: Path) -> None:
    """Nothing to borrow — the hook must not create a dangling symlink."""
    base = _make_repo(tmp_path / "base")  # no _seed_graph

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)

    graph_link = wt / "graphify-out" / "graph.json"
    assert not graph_link.exists()
    assert not graph_link.is_symlink()
    # And the worktree is still clean — nothing half-linked left behind.
    status = _git("status", "--porcelain", cwd=wt)
    assert status.stdout == ""


def test_a_real_graph_in_the_worktree_is_never_clobbered(tmp_path: Path) -> None:
    """If a worktree somehow has its own built graph, the hook leaves it be
    rather than replacing real data with a link."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    # Replace the linked graph.json with a real one, then switch branches to
    # re-fire the hook.
    own = wt / "graphify-out"
    graph_link = own / "graph.json"
    if graph_link.is_symlink():
        graph_link.unlink()
    graph_link.write_text('{"nodes": ["mine"]}', encoding="utf-8")

    _git("checkout", "-q", "-b", "feat2", cwd=wt)

    assert not graph_link.is_symlink()
    assert "mine" in graph_link.read_text(encoding="utf-8")
    assert own.is_dir() and not own.is_symlink()


def test_worktree_remove_leaves_the_base_graph_intact(tmp_path: Path) -> None:
    """#1617/#1295: the per-entry symlinks must never cause worktree cleanup
    to reach into the base checkout — `git worktree remove` (and, by
    extension, coord's `shutil.rmtree` cleanup sweep, which unlinks rather
    than recurses into directory symlinks) must leave the base graph
    untouched."""
    base = _make_repo(tmp_path / "base")
    out = _seed_graph(base)
    (out / "cache").mkdir(exist_ok=True)
    (out / "cache" / "foo").write_text("x", encoding="utf-8")

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    assert (wt / "graphify-out" / "cache" / "foo").is_file()

    res = _git("worktree", "remove", "--force", str(wt), cwd=base)
    assert res.returncode == 0, res.stderr

    assert (out / "graph.json").is_file()
    assert (out / "cache" / "foo").is_file()
    assert not out.is_symlink()


def test_main_worktree_chains_to_the_machine_local_graphify_hook(
    tmp_path: Path,
) -> None:
    """The versioned hook must not swallow graphify's own hook: in the MAIN
    worktree it execs $GIT_COMMON_DIR/hooks/post-checkout, which is where
    `graphify hook install` writes its machine-pinned block."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)
    marker = tmp_path / "chained.txt"
    local_hook = base / ".git" / "hooks" / "post-checkout"
    local_hook.parent.mkdir(parents=True, exist_ok=True)
    local_hook.write_text(f"#!/bin/sh\necho chained >> {marker}\n", encoding="utf-8")
    local_hook.chmod(0o755)

    _git("checkout", "-q", "-b", "other", cwd=base)

    assert marker.is_file(), "main-worktree checkout should chain to the local hook"
    assert "chained" in marker.read_text(encoding="utf-8")


def test_linked_worktree_does_not_chain_to_the_rebuild_hook(tmp_path: Path) -> None:
    """A rebuild inside a worktree would overwrite the SHARED base graph from a
    feature-branch tree (and can die on a reaped worktree).  The hook must exit
    before chaining when it is in a linked worktree."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)
    marker = tmp_path / "chained.txt"
    local_hook = base / ".git" / "hooks" / "post-checkout"
    local_hook.parent.mkdir(parents=True, exist_ok=True)
    local_hook.write_text(f"#!/bin/sh\necho chained >> {marker}\n", encoding="utf-8")
    local_hook.chmod(0o755)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    _git("checkout", "-q", "-b", "feat2", cwd=wt)

    assert not marker.exists(), "a linked worktree must never trigger a rebuild"


def test_the_hook_actually_runs_on_an_in_worktree_checkout(tmp_path: Path) -> None:
    """Anti-vacuity guard for the two tests above.

    Both assert that something does NOT happen after a checkout issued from
    inside a worktree — which is trivially true if the hook never ran at all.
    Git resolves a relative ``core.hooksPath`` against the invoking directory,
    so that is exactly what happens when ``.githooks/`` is not committed.
    Prove the hook is reachable there before trusting those assertions.
    """
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    assert (wt / ".githooks" / "post-checkout").is_file(), (
        "the worktree must carry the hook, or the in-worktree tests are vacuous"
    )

    # Remove the link, then switch branches from inside the worktree: the hook
    # must run and recreate it.
    (wt / "graphify-out" / "graph.json").unlink()
    _git("checkout", "-q", "-b", "feat3", cwd=wt)
    assert (wt / "graphify-out" / "graph.json").is_symlink(), (
        "hook did not run in the worktree"
    )


# ── unit: freshness detection ────────────────────────────────────────────────


def test_read_built_sha_parses_the_report_line(tmp_path: Path) -> None:
    report = tmp_path / "GRAPH_REPORT.md"
    report.write_text(
        "# Graph Report - x\n\n## Graph Freshness\n"
        "- Built from commit: `5be69d08`\n"
        "- Run `git rev-parse HEAD` and compare.\n",
        encoding="utf-8",
    )
    assert read_built_sha(report) == "5be69d08"


def test_read_built_sha_returns_none_when_absent(tmp_path: Path) -> None:
    report = tmp_path / "GRAPH_REPORT.md"
    report.write_text("# Graph Report\nno freshness line here\n", encoding="utf-8")
    assert read_built_sha(report) is None
    assert read_built_sha(tmp_path / "missing.md") is None


def test_graph_status_reports_in_sync(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    _seed_graph(repo, built_sha=_head(repo)[:8])

    st = graph_status(repo)
    assert st.present is True
    assert st.in_sync is True
    assert st.stale is False


def test_graph_status_reports_stale_after_a_commit(tmp_path: Path) -> None:
    """The drift the hooks cannot catch (rebase / reset --hard / a failed
    background rebuild) shows up as a SHA mismatch."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    out = _seed_graph(repo, built_sha=_head(repo)[:8])
    _git("commit", "-q", "--allow-empty", "-m", "moves HEAD", cwd=repo)
    # graphify has not re-checked the tree since that commit.
    os.utime(out / "manifest.json", (0, 0))

    st = graph_status(repo)
    assert st.present is True
    assert st.in_sync is False
    assert st.stamp_behind is True
    assert st.verified_current is False
    assert st.stale is True


def test_a_stamp_behind_head_is_not_stale_when_verified_since(
    tmp_path: Path,
) -> None:
    """`graphify update` re-extracts, and when topology is unchanged it prints
    "outputs left untouched" — leaving GRAPH_REPORT.md's "Built from commit"
    stamp behind forever while still refreshing manifest.json.  Reporting that
    as STALE on every run would make the check cry wolf permanently (this is
    exactly the state ~/src/vimcode was in)."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    out = _seed_graph(repo, built_sha=_head(repo)[:8])
    _git("commit", "-q", "--allow-empty", "-m", "moves HEAD", cwd=repo)
    # graphify re-verified against the tree AFTER that commit landed.
    os.utime(out / "manifest.json", (time.time() + 60, time.time() + 60))

    st = graph_status(repo)
    assert st.stamp_behind is True, "the stamp really is behind"
    assert st.verified_current is True
    assert st.stale is False, "verified-since means the content is current"


def test_missing_manifest_falls_back_to_the_stamp(tmp_path: Path) -> None:
    """No manifest.json → no evidence of verification → trust the stamp."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    _seed_graph(repo, built_sha=_head(repo)[:8], manifest=False)
    _git("commit", "-q", "--allow-empty", "-m", "moves HEAD", cwd=repo)

    st = graph_status(repo)
    assert st.verified_at is None
    assert st.verified_current is False
    assert st.stale is True


def test_graph_status_missing_graph_is_not_reported_as_stale(tmp_path: Path) -> None:
    """Absent != drifted — an unbuilt graph must not inflate the stale count."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)

    st = graph_status(repo)
    assert st.present is False
    assert st.stale is False
    assert "never built" in (st.unknown_reason or "")


def test_symlinked_worktree_freshness_is_judged_against_the_base(
    tmp_path: Path,
) -> None:
    """A worktree is on a feature branch by definition; comparing the shared
    graph against the WORKTREE's HEAD would report permanent false drift.
    Freshness must be judged against the checkout that owns the graph."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base, built_sha=_head(base)[:8])
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    # Move the worktree's HEAD away from the base's.
    _git("commit", "-q", "--allow-empty", "-m", "worktree-only commit", cwd=wt)

    st = graph_status(wt)
    assert st.is_symlink is True
    assert st.in_sync is True, "should compare against the base, not the worktree"
    assert st.stale is False


def test_unknown_freshness_is_not_counted_as_stale(tmp_path: Path) -> None:
    st = GraphStatus(repo_path=tmp_path, present=True, built_sha=None, head_sha="abc")
    assert st.stale is False


# ── #2211: HEAD vs origin/<default_branch> ───────────────────────────────────


def _repo_behind_origin(tmp_path: Path, *, behind_by: int) -> Path:
    """A repo whose graph matches its OWN HEAD, but whose HEAD sits
    *behind_by* commits behind ``origin/main`` — never fetched-and-pulled,
    only fetched (the agent's own behaviour, see module docstring)."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    _seed_graph(repo, built_sha=_head(repo)[:8])
    caught_up_at = _head(repo)
    bare = tmp_path / "origin.git"
    _git("init", "-q", "-b", "main", "--bare", str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=repo)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=repo)
    if behind_by:
        for i in range(behind_by):
            _git("commit", "-q", "--allow-empty", "-m", f"extra-{i}", cwd=repo)
        _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=repo)
        _git("reset", "-q", "--hard", caught_up_at, cwd=repo)
    _git("fetch", "-q", "origin", cwd=repo)
    return repo


def test_graph_status_reports_origin_behind_when_head_matches_the_graph(
    tmp_path: Path,
) -> None:
    """The exact #2211 regression: graph == HEAD (``in_sync`` True, ``stale``
    False) tells you nothing about HEAD vs origin — that's a separate axis
    this must expose, not fold into ``stale``."""
    repo = _repo_behind_origin(tmp_path, behind_by=5)

    st = graph_status(repo, "main")
    assert st.in_sync is True, "graph really does match this checkout's own HEAD"
    assert st.stale is False, "stale is strictly the graph<->HEAD comparison, unchanged"
    assert st.commits_behind_origin == 5
    assert st.origin_behind is True


def test_graph_status_origin_in_sync_when_head_matches_origin_too(
    tmp_path: Path,
) -> None:
    repo = _repo_behind_origin(tmp_path, behind_by=0)

    st = graph_status(repo, "main")
    assert st.in_sync is True
    assert st.commits_behind_origin == 0
    assert st.origin_behind is False


def test_graph_status_origin_behind_is_unknown_not_false_without_a_remote(
    tmp_path: Path,
) -> None:
    """No ``origin`` remote at all (a local-only repo, e.g. most fixtures in
    this file) must not be reported as "0 commits behind" — that would be a
    fabricated fact. It must stay unproven, hence ``origin_behind`` False for
    a different reason: we simply couldn't check."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    _seed_graph(repo, built_sha=_head(repo)[:8])

    st = graph_status(repo, "main")
    assert st.origin_sha is None
    assert st.commits_behind_origin is None
    assert st.origin_behind is False


def test_graph_status_makes_no_writes(tmp_path: Path) -> None:
    """Read-only by design (#2211's acceptance bar): no fetch, no pull — HEAD
    must be exactly what it was before the check ran."""
    repo = _repo_behind_origin(tmp_path, behind_by=5)
    before = _head(repo)

    graph_status(repo, "main")

    assert _head(repo) == before
    # Working tree/index likewise untouched.
    status = _git("status", "--porcelain", cwd=repo).stdout
    assert status == ""


def test_hooks_path_status_flags_an_unset_hooks_path(tmp_path: Path) -> None:
    """The repo ships the bootstrap but this checkout hasn't opted in."""
    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    _git("config", "--unset", "core.hooksPath", cwd=repo)
    ok, detail = hooks_path_status(repo)
    assert ok is False
    assert "core.hooksPath is unset" in detail
    assert "config core.hooksPath .githooks" in detail


def test_hooks_path_status_does_not_advise_pointing_at_a_missing_dir(
    tmp_path: Path,
) -> None:
    """A repo with no .githooks/ must NOT be told to set core.hooksPath at it —
    that would silently disable every hook for the checkout."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    ok, detail = hooks_path_status(repo)
    assert ok is False
    assert "no .githooks/post-checkout in this repo" in detail
    assert "config core.hooksPath" not in detail


def test_hooks_path_status_ok_when_configured(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    ok, detail = hooks_path_status(repo)
    assert ok is True
    assert ".githooks" in detail


def test_hooks_path_status_flags_a_missing_hook_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    (repo / ".githooks" / "post-checkout").unlink()
    ok, detail = hooks_path_status(repo)
    assert ok is False
    assert "missing" in detail


def test_hooks_file_present_true_when_hook_shipped(tmp_path: Path) -> None:
    """#2236: the "does this repo ship .githooks/post-checkout" question has
    exactly one implementation — hooks_path_status uses it internally, and
    coord.repo_onboard.gather_graph_facts consumes this instead of
    recomputing the same .is_file() check independently."""
    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    assert hooks_file_present(repo) is True


def test_hooks_file_present_false_when_never_shipped(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    assert hooks_file_present(repo) is False


def test_gather_graph_facts_hooks_shipped_matches_hooks_file_present(
    tmp_path: Path,
) -> None:
    """coord.repo_onboard.gather_graph_facts's `hooks_shipped` field used to
    recompute the same `.githooks/post-checkout` `.is_file()` check that
    `hooks_path_status` already does internally — two independent
    implementations of one question (#2236 review). Now both read off
    `hooks_file_present`; this pins them together against a real repo in
    both states so they can't quietly drift again."""
    from coord.repo_onboard import gather_graph_facts

    shipped = _make_repo(tmp_path / "shipped", with_hooks=True)
    facts = gather_graph_facts(shipped)
    assert facts.hooks_shipped is True
    assert facts.hooks_shipped == hooks_file_present(shipped)

    unshipped = _make_repo(tmp_path / "unshipped", with_hooks=False)
    facts = gather_graph_facts(unshipped)
    assert facts.hooks_shipped is False
    assert facts.hooks_shipped == hooks_file_present(unshipped)


# ── the core.hooksPath replacement hazard ────────────────────────────────────


def test_orphaned_hooks_detects_a_locally_installed_hook_with_no_shim(
    tmp_path: Path,
) -> None:
    """core.hooksPath makes git ignore .git/hooks ENTIRELY — no merge, no
    fallback.  A graphify hook with no shim in .githooks/ stops running with
    no error and no log line.  This shipped exactly once (only post-checkout
    had a shim, silently killing the commit/merge rebuilds)."""
    from coord.graph_health import orphaned_hooks

    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    # Simulate the shipped-broken state: only post-checkout has a shim.
    for name in ("post-commit", "post-merge"):
        (repo / ".githooks" / name).unlink()
    local = repo / ".git" / "hooks"
    local.mkdir(parents=True, exist_ok=True)
    for name in ("post-commit", "post-merge"):
        (local / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (local / name).chmod(0o755)

    assert orphaned_hooks(repo) == ["post-commit", "post-merge"]

    ok, detail = hooks_path_status(repo)
    assert ok is False
    assert "SILENTLY DISABLED" in detail


def test_orphaned_hooks_is_empty_once_every_hook_has_a_shim(tmp_path: Path) -> None:
    from coord.graph_health import orphaned_hooks

    # _make_repo installs the real .githooks/, which already ships shims for
    # every hook kind graphify installs.
    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    local = repo / ".git" / "hooks"
    local.mkdir(parents=True, exist_ok=True)
    for name in ("post-commit", "post-merge"):
        (local / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (local / name).chmod(0o755)

    assert orphaned_hooks(repo) == []
    ok, _ = hooks_path_status(repo)
    assert ok is True


def test_orphaned_hooks_ignores_samples_and_backups(tmp_path: Path) -> None:
    """graphify leaves .pre-guard-upgrade.bak copies next to its hooks, and git
    ships .sample files — neither is ever executed."""
    from coord.graph_health import orphaned_hooks

    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    local = repo / ".git" / "hooks"
    local.mkdir(parents=True, exist_ok=True)
    (local / "post-commit.pre-guard-upgrade.bak").write_text("x", encoding="utf-8")
    (local / "pre-push.sample").write_text("x", encoding="utf-8")

    assert orphaned_hooks(repo) == []


def test_every_graphify_hook_in_this_repo_has_a_versioned_shim() -> None:
    """Guard on the REAL .githooks/: adding a graphify hook kind without a shim
    silently disables it for everyone who opted into core.hooksPath."""
    versioned = HOOK_SRC.parent
    present = {p.name for p in versioned.iterdir() if p.is_file()}
    for required in ("post-checkout", "post-commit", "post-merge"):
        assert required in present, f".githooks/{required} is missing"
        assert os.access(versioned / required, os.X_OK), (
            f".githooks/{required} is not executable — git will ignore it"
        )


# ── fast_forward_base_checkout: keeping the base checkout itself current ─────
#
# Real git throughout: a bare origin, the base checkout cloned from it, and a
# second clone that pushes new commits, so "behind origin" is a genuine state
# rather than a stubbed answer.


def _ff_commit(repo: Path, name: str, content: str = "x\n") -> str:
    (repo / name).write_text(content)
    _git("add", name, cwd=repo)
    _git("commit", "-q", "-m", f"add {name}", cwd=repo)
    return _head(repo)


def _clean_base_behind_origin(
    tmp_path: Path, n: int, *, branch: str = "main"
) -> tuple[Path, Path]:
    """``(base, pusher)``: *base* is a clean clone on *branch*, and *pusher*
    has pushed *n* further commits to origin/*branch* since."""
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", branch, str(origin), cwd=tmp_path)
    seed = tmp_path / "seed"
    _git("init", "-q", "-b", branch, str(seed), cwd=tmp_path)
    _ff_commit(seed, "README", "init\n")
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "-q", "origin", branch, cwd=seed)
    base = tmp_path / "base"
    _git("clone", "-q", "-b", branch, str(origin), str(base), cwd=tmp_path)
    for i in range(n):
        _ff_commit(seed, f"f{i}.txt")
    if n:
        _git("push", "-q", "origin", branch, cwd=seed)
    return base, seed


def test_ff_clean_base_behind_origin_is_fast_forwarded(tmp_path: Path) -> None:
    base, pusher = _clean_base_behind_origin(tmp_path, 2)
    before = _head(base)
    res = fast_forward_base_checkout(base, ("main",))
    assert res.outcome == "fast_forwarded", res.detail
    assert res.refreshed
    assert res.head_before == before
    assert res.head_after == _head(pusher) == _head(base)
    assert "2 commits" in res.detail


def test_ff_already_current_is_up_to_date(tmp_path: Path) -> None:
    base, _ = _clean_base_behind_origin(tmp_path, 0)
    before = _head(base)
    res = fast_forward_base_checkout(base, ("main",))
    assert res.outcome == "up_to_date"
    assert not res.refreshed
    assert _head(base) == before


def test_ff_dirty_tracked_file_is_left_untouched(tmp_path: Path) -> None:
    base, _ = _clean_base_behind_origin(tmp_path, 2)
    before = _head(base)
    (base / "README").write_text("local edit\n")
    res = fast_forward_base_checkout(base, ("main",))
    assert res.outcome == "skipped_dirty"
    assert "README" in res.detail
    assert _head(base) == before
    assert (base / "README").read_text() == "local edit\n"


def test_ff_untracked_file_does_not_block(tmp_path: Path) -> None:
    base, pusher = _clean_base_behind_origin(tmp_path, 1)
    (base / ".pytest.out").write_text("junk\n")
    res = fast_forward_base_checkout(base, ("main",))
    assert res.outcome == "fast_forwarded", res.detail
    assert _head(base) == _head(pusher)
    assert (base / ".pytest.out").read_text() == "junk\n"


def test_ff_off_branch_is_left_untouched(tmp_path: Path) -> None:
    base, _ = _clean_base_behind_origin(tmp_path, 2)
    _git("checkout", "-q", "-b", "feature", cwd=base)
    before = _head(base)
    res = fast_forward_base_checkout(base, ("main",))
    assert res.outcome == "skipped_off_branch"
    assert "feature" in res.detail
    assert _head(base) == before
    branch = _git("symbolic-ref", "--short", "HEAD", cwd=base).stdout.strip()
    assert branch == "feature"


def test_ff_detached_head_is_left_untouched(tmp_path: Path) -> None:
    base, _ = _clean_base_behind_origin(tmp_path, 1)
    _git("checkout", "-q", "--detach", cwd=base)
    before = _head(base)
    res = fast_forward_base_checkout(base, ("main",))
    assert res.outcome == "skipped_off_branch"
    assert _head(base) == before


def test_ff_local_unpushed_commit_is_diverged_and_untouched(tmp_path: Path) -> None:
    base, _ = _clean_base_behind_origin(tmp_path, 2)
    local = _ff_commit(base, "local.txt")
    res = fast_forward_base_checkout(base, ("main",))
    assert res.outcome == "skipped_diverged"
    assert _head(base) == local


def test_ff_linked_worktree_is_skipped(tmp_path: Path) -> None:
    base, _ = _clean_base_behind_origin(tmp_path, 1)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "main-wt", str(wt), cwd=base)
    before = _head(wt)
    res = fast_forward_base_checkout(wt, ("main", "main-wt"))
    assert res.outcome == "skipped_linked_worktree"
    assert _head(wt) == before


def test_ff_index_lock_is_in_progress_and_untouched(tmp_path: Path) -> None:
    base, _ = _clean_base_behind_origin(tmp_path, 1)
    before = _head(base)
    (base / ".git" / "index.lock").write_text("")
    res = fast_forward_base_checkout(base, ("main",))
    assert res.outcome == "skipped_in_progress"
    assert "index.lock" in res.detail
    assert _head(base) == before


def test_ff_develop_integration_branch_is_fast_forwarded(tmp_path: Path) -> None:
    base, pusher = _clean_base_behind_origin(tmp_path, 3, branch="develop")
    res = fast_forward_base_checkout(base, ("main", "develop"))
    assert res.outcome == "fast_forwarded", res.detail
    assert res.branch == "develop"
    assert _head(base) == _head(pusher)


def test_ff_no_origin_remote(tmp_path: Path) -> None:
    repo = tmp_path / "lonely"
    _git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    _ff_commit(repo, "README")
    res = fast_forward_base_checkout(repo, ("main",))
    assert res.outcome == "no_origin"


def test_ff_fetch_failure_is_reported_and_untouched(tmp_path: Path) -> None:
    base, _ = _clean_base_behind_origin(tmp_path, 1)
    before = _head(base)
    shutil.rmtree(tmp_path / "origin.git")
    res = fast_forward_base_checkout(base, ("main",))
    assert res.outcome == "fetch_failed"
    assert res.detail
    assert _head(base) == before
