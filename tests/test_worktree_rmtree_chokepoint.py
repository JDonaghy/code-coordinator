"""#1693 — the base checkout must never be recursively deleted.

Background: #1659 removed the ``shutil.rmtree`` fallback from
``_free_branch_in_worktrees`` after it destroyed precision's base checkout.
It did not touch ``_git_worktree_add`` 60 lines below, which carries the same
idiom *and* scrapes its target path out of git's error text.  On 2026-08-02
that sibling recursively deleted ``~/src/claude-coordinator`` on dellserver.

Two kinds of test live here:

* behavioural regressions for the collision path and the shared
  ``_safe_remove_worktree`` chokepoint, and
* a **source-level** guard asserting that no module grows a fifth ad-hoc
  ``shutil.rmtree`` on a worktree path.  Without that last one the fix drifts
  again exactly as #1659 -> #1693 did.

Nothing here removes a real checkout: every "base checkout" is a throwaway
repo under ``tmp_path``, and the destructive assertions are about what does
*not* happen.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import coord.agent as agent_mod
from coord.agent import (
    _GitError,
    _classify_worktree,
    _git_worktree_add,
    _is_strictly_inside,
    _safe_remove_worktree,
)


def _init_local_repo(path: Path) -> Path:
    """A local-only git repo with one commit — no remote, no network."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=str(path), check=True, capture_output=True
    )
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(path), check=True, capture_output=True
    )
    return path


# ── The direct regression ───────────────────────────────────────────────────


def test_worktree_add_collision_on_base_checkout_raises_and_keeps_it(
    tmp_path: Path,
) -> None:
    """#1693: a collision naming the BASE CHECKOUT raises and deletes nothing.

    This is the exact dellserver sequence: the base checkout is parked on the
    branch the new worktree wants, so `git worktree add` fails with "already
    used by worktree at '<base checkout>'", the retry path scrapes that path
    out of the error, `git worktree remove` refuses it as a main working tree,
    and the old fallback rmtree'd it.

    Before the fix this test deletes the whole repo — `.git` and all.
    """
    repo = _init_local_repo(tmp_path / "repo")
    # Park the base checkout on the branch the add below wants (#1623 state).
    subprocess.run(
        ["git", "checkout", "-b", "issue-1693-parked"],
        cwd=str(repo), check=True, capture_output=True,
    )
    sentinel = repo / "sentinel.txt"
    sentinel.write_text("must survive", encoding="utf-8")

    log = tmp_path / "worker.log"
    log.write_text("", encoding="utf-8")

    with pytest.raises(_GitError) as excinfo:
        _git_worktree_add(
            repo,
            ["-B", "issue-1693-parked", str(tmp_path / "new-wt"), "HEAD"],
            log_path=str(log),
        )

    # The error has to be actionable — it must name the branch AND the path.
    message = str(excinfo.value)
    assert "issue-1693-parked" in message, message
    assert str(repo) in message, message

    assert repo.exists(), "base checkout directory was deleted (#1693)"
    assert (repo / ".git").exists(), "base checkout .git was deleted (#1693)"
    assert sentinel.read_text(encoding="utf-8") == "must survive", (
        "base checkout contents were destroyed (#1693)"
    )
    # Still a usable checkout, still on its branch.
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    assert branch == "issue-1693-parked"
    # And the log says why, rather than claiming it force-removed something.
    assert "BASE CHECKOUT" in log.read_text(encoding="utf-8")


def test_worktree_add_collision_on_base_checkout_never_calls_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1693: assert on the *patched symbol*, not just the end state.

    Checking only that the directory survives would still pass if a future
    refactor rmtree'd something else, or rmtree'd this path with
    ``ignore_errors=True`` against an open handle.  Patch `shutil.rmtree` and
    require that it is never reached at all.
    """
    repo = _init_local_repo(tmp_path / "repo")
    subprocess.run(
        ["git", "checkout", "-b", "issue-1693-armed"],
        cwd=str(repo), check=True, capture_output=True,
    )

    calls: list[str] = []

    def _explode(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append(str(path))

    monkeypatch.setattr(agent_mod.shutil, "rmtree", _explode)

    with pytest.raises(_GitError):
        _git_worktree_add(
            repo, ["-B", "issue-1693-armed", str(tmp_path / "new-wt"), "HEAD"]
        )

    assert calls == [], f"shutil.rmtree was reached for a main worktree: {calls}"


def test_safe_remove_worktree_refuses_the_main_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chokepoint itself refuses the base checkout, whoever calls it."""
    repo = _init_local_repo(tmp_path / "repo")
    calls: list[str] = []
    monkeypatch.setattr(
        agent_mod.shutil, "rmtree", lambda p, *a, **k: calls.append(str(p))
    )

    assert _safe_remove_worktree(repo, repo) is False
    assert calls == []
    assert (repo / ".git").exists()


def test_safe_remove_worktree_refuses_symlinked_base_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sides are resolved: a symlink to the base checkout is still it.

    dellserver really does have ``~/src/<repo>.broken-backup-… ->
    ~/.coord/worktrees/<id>``, so ``~/src/<repo>`` being (or being reached
    through) a symlink is not hypothetical on this fleet.
    """
    repo = _init_local_repo(tmp_path / "repo")
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)

    calls: list[str] = []
    monkeypatch.setattr(
        agent_mod.shutil, "rmtree", lambda p, *a, **k: calls.append(str(p))
    )

    assert _safe_remove_worktree(repo, alias) is False
    assert calls == []
    assert (repo / ".git").exists()


def test_safe_remove_worktree_refuses_unregistered_path_outside_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory git knows nothing about is not removable by default."""
    repo = _init_local_repo(tmp_path / "repo")
    stranger = tmp_path / "not-a-worktree"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("keep", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(
        agent_mod.shutil, "rmtree", lambda p, *a, **k: calls.append(str(p))
    )

    assert _safe_remove_worktree(repo, stranger) is False
    assert _safe_remove_worktree(
        repo, stranger, sandbox_root=tmp_path / "worktrees"
    ) is False
    assert calls == []
    assert (stranger / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_safe_remove_worktree_removes_sandboxed_orphan(tmp_path: Path) -> None:
    """An orphaned directory inside the sandbox is still swept (no leak)."""
    repo = _init_local_repo(tmp_path / "repo")
    sandbox = tmp_path / "worktrees"
    orphan = sandbox / "abc123"
    orphan.mkdir(parents=True)
    (orphan / "junk.txt").write_text("junk", encoding="utf-8")

    assert _safe_remove_worktree(repo, orphan, sandbox_root=sandbox) is True
    assert not orphan.exists()


def test_safe_remove_worktree_refuses_symlink_escaping_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`worktrees/<id> -> ~/src/<repo>` resolves outside and is refused."""
    repo = _init_local_repo(tmp_path / "repo")
    sandbox = tmp_path / "worktrees"
    sandbox.mkdir()
    escape = sandbox / "escape"
    escape.symlink_to(repo, target_is_directory=True)

    calls: list[str] = []
    monkeypatch.setattr(
        agent_mod.shutil, "rmtree", lambda p, *a, **k: calls.append(str(p))
    )

    assert _safe_remove_worktree(None, escape, sandbox_root=sandbox) is False
    assert calls == []
    assert (repo / ".git").exists()


# ── The behaviour that must NOT regress ─────────────────────────────────────


def test_worktree_add_collision_on_linked_worktree_still_retries_once(
    tmp_path: Path,
) -> None:
    """#460 Part 2 must survive #1693: a real linked worktree is still evicted."""
    repo = _init_local_repo(tmp_path / "repo")
    conflict_wt = tmp_path / "conflict-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-55-retry", str(conflict_wt), "HEAD"],
        cwd=str(repo), check=True, capture_output=True,
    )

    new_wt = tmp_path / "new-wt"
    log = tmp_path / "test.log"
    log.write_text("", encoding="utf-8")

    adds: list[tuple[str, ...]] = []
    real_git = agent_mod._git

    def _counting_git(path: Path, *args: str, **kwargs: object) -> str:
        if args[:2] == ("worktree", "add"):
            adds.append(args)
        return real_git(path, *args, **kwargs)

    agent_mod._git = _counting_git  # type: ignore[assignment]
    try:
        _git_worktree_add(
            repo, ["-B", "issue-55-retry", str(new_wt), "HEAD"], log_path=str(log)
        )
    finally:
        agent_mod._git = real_git  # type: ignore[assignment]

    assert new_wt.exists(), "retry did not create the new worktree"
    assert not conflict_wt.exists(), "conflicting linked worktree was not removed"
    assert len(adds) == 2, f"expected exactly one retry, got {len(adds)} adds"
    assert "collision" in log.read_text(encoding="utf-8")


def test_classify_worktree_labels_main_linked_and_unregistered(
    tmp_path: Path,
) -> None:
    """The shared predicate behind every guard, exercised directly."""
    repo = _init_local_repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature", str(linked), "HEAD"],
        cwd=str(repo), check=True, capture_output=True,
    )
    stranger = tmp_path / "stranger"
    stranger.mkdir()

    assert _classify_worktree(repo, repo) == "main"
    assert _classify_worktree(repo, linked) == "linked"
    assert _classify_worktree(repo, stranger) == "unregistered"
    # No repo to ask → the strictest answer short of "main".
    assert _classify_worktree(None, linked) == "unregistered"


def test_is_strictly_inside_excludes_the_root_itself(tmp_path: Path) -> None:
    """The sweep root is never itself a removable worktree."""
    root = tmp_path / "worktrees"
    (root / "a").mkdir(parents=True)
    assert _is_strictly_inside(root / "a", root) is True
    assert _is_strictly_inside(root, root) is False
    assert _is_strictly_inside(tmp_path / "elsewhere", root) is False


# ── The source-level chokepoint guard ───────────────────────────────────────

# Every `shutil.rmtree(...)` allowed to exist in these modules, keyed by the
# enclosing function and the source of its first argument.  #1693: a worktree
# path may only be rmtree'd inside `_safe_remove_worktree`.  The two other
# entries are artifact-stash directories under `<state_dir>`, which are not
# worktrees and have never been implicated.
#
# If this test fails you have added a new recursive delete.  Route it through
# `coord.agent._safe_remove_worktree` instead of widening this table — that is
# the whole point of the table.
_ALLOWED_RMTREE: dict[str, set[tuple[str, str]]] = {
    "coord/agent.py": {
        ("_safe_remove_worktree", "target"),
        ("stash_artifacts_for_branch", "stash_dir"),
        ("_gc_artifacts", "branch_dir"),
    },
    "coord/interactive.py": set(),
    "coord/commands/sessions.py": set(),
}


def _rmtree_sites(source: str) -> set[tuple[str, str]]:
    """(enclosing function name, first-arg source) for each `shutil.rmtree`."""
    tree = ast.parse(source)
    parents: dict[ast.AST, str] = {}

    def _walk(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = enclosing
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            parents[child] = name
            _walk(child, name)

    parents[tree] = "<module>"
    _walk(tree, "<module>")

    sites: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "rmtree"
            and isinstance(func.value, ast.Name)
            and func.value.id == "shutil"
        ):
            continue
        arg = ast.unparse(node.args[0]) if node.args else "<no-arg>"
        sites.add((parents.get(node, "<module>"), arg))
    return sites


@pytest.mark.parametrize("module_rel", sorted(_ALLOWED_RMTREE))
def test_no_new_rmtree_site_outside_the_chokepoint(module_rel: str) -> None:
    """#1693: freeze the set of recursive deletes so a fifth can't appear.

    #1659 fixed one deleter and missed its sibling 60 lines below; that miss
    cost a base checkout. An end-state assertion cannot catch a *new* call
    site, so this reads the source.
    """
    repo_root = Path(__file__).resolve().parent.parent
    module_path = repo_root / module_rel
    found = _rmtree_sites(module_path.read_text(encoding="utf-8"))
    expected = _ALLOWED_RMTREE[module_rel]
    unexpected = found - expected
    assert not unexpected, (
        f"{module_rel} grew a new `shutil.rmtree` call: {sorted(unexpected)}. "
        f"Worktree deletions must go through "
        f"`coord.agent._safe_remove_worktree` (#1693)."
    )
    missing = expected - found
    assert not missing, (
        f"{module_rel} no longer has the expected `shutil.rmtree` site(s) "
        f"{sorted(missing)} — update _ALLOWED_RMTREE if that was deliberate."
    )
