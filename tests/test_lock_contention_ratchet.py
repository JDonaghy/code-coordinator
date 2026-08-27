"""#2846: ratchet against the 503-after-successful-write bug class.

#2689 diagnosed and fixed one instance of this shape in
``_create_issue_local``: an irreversible ``github_ops`` write lands, then an
unguarded local SQLite write follows it — a transient ``database is locked``
on that second write used to propagate straight out as a bare
``OperationalError``, which the daemon's blanket ``except Exception`` turned
into a 503 indistinguishable from "nothing happened," inviting a caller
retry that duplicates the (already-successful) upstream write.

#2689 fixed only ``_create_issue_local``. #2846 found the identical gap
still open on ``_update_issue_labels_local`` (the ``/issue-labels`` plural
endpoint's direct caller — ``_apply_issue_labels_local`` had its OWN guard
around the same callee, but that guard didn't help a second caller reaching
the same unguarded function) and ``_record_issue_comment_capture_local``
(reached both from the daemon's own ``/issue-comment`` route and, cross-
module, from ``github_ops.post_issue_comment`` on every remote caller).
That is the third recurrence of one bug shape landing one call site at a
time (#2689, #2802, #2846) — this is the ratchet #2846 asked for so a fourth
recurrence gets caught here instead of in production.

Static, AST-based (matching this repo's existing ratchet convention — see
``tests/test_sql_dialect.py``'s "no raw `?` reaches a driver" tests): for
every function in ``coord/state.py`` that touches ``github_ops`` — directly,
by calling it, or the other direction, by being called FROM a
``github_ops.py`` function that itself calls the low-level ``_gh()`` seam —
walk its callees (within ``coord/state.py``, up to a few hops of
delegation) for a raw write (a ``.commit()`` call) that isn't wrapped in
``coord.db.retry_on_locked``. A function that delegates its actual write to
a helper is fine as long as THAT helper is guarded — the guard doesn't have
to live at the outermost frame, see ``_apply_issue_labels_local`` ->
``_update_issue_labels_local``.

Scope, stated plainly rather than left to be discovered later: this catches
the ``coord/state.py`` <-> ``coord/github_ops.py`` bridge in both
directions, which is where every fixed site (#2689, #2846) actually lives.
It does NOT reach into orchestration one module further out — e.g.
``coord/commands/drive_queue.py``, which does its own GitHub label write
(``apply_pipeline_track_labels_best_effort``) via a completely separate call
path from the board-row write (``enqueue_drive_queue``) it also issues.
Widening this to a full cross-``coord/commands/**`` call-graph walk is a
bigger and separately-scoped effort; the drive-queue local writers
(``_enqueue_drive_queue_local`` and friends) are covered instead by direct
regression tests in ``tests/test_state.py`` verifying they now retry
transient contention via ``coord.db.retry_on_locked``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_COORD_DIR = Path(__file__).resolve().parent.parent / "coord"


def _parse(relpath: str) -> ast.Module:
    return ast.parse((_COORD_DIR / relpath).read_text(), filename=relpath)


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """Module-level (or nested-but-uniquely-named) function defs, keyed by
    name — good enough for ``coord/state.py``/``coord/github_ops.py``, which
    don't reuse a ``_*_local`` name across two different functions."""
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def _has_call_named(node: ast.AST, name: str) -> bool:
    """True if *name* is called anywhere under *node* — as a bare ``name(...)``
    or an attribute call ``x.name(...)`` — including inside a nested closure
    (the established idiom here is exactly ``def _write(): ...; sql.execute
    (...); conn.commit()`` followed by ``retry_on_locked(_write)`` in the
    OUTER scope, so both the write and its guard must be visible to a single
    whole-subtree walk)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name) and f.id == name:
                return True
            if isinstance(f, ast.Attribute) and f.attr == name:
                return True
    return False


def _calls_commit(node: ast.AST) -> bool:
    return _has_call_named(node, "commit")


def _is_guarded(node: ast.AST) -> bool:
    return _has_call_named(node, "retry_on_locked")


def _direct_callees(node: ast.AST) -> set[str]:
    """Bare-``Name`` calls anywhere under *node* — same-module function
    calls, which is how ``coord/state.py`` invokes its own ``_*_local``
    helpers (never via ``self.`` or an imported alias)."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            names.add(sub.func.id)
    return names


def _touches_github_ops(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "github_ops"
        ):
            return True
    return False


def _unguarded_write_reachable(
    name: str,
    funcs: dict[str, ast.FunctionDef],
    *,
    depth: int = 4,
    seen: set[str] | None = None,
) -> bool:
    """True if *name* (defined in *funcs*) performs a raw write — directly,
    or via delegation to a sibling function up to *depth* hops away — that
    isn't guarded by ``retry_on_locked`` at the frame that performs it."""
    if seen is None:
        seen = set()
    if name in seen or depth < 0 or name not in funcs:
        return False
    seen.add(name)
    fn = funcs[name]
    if _calls_commit(fn) and not _is_guarded(fn):
        return True
    return any(
        _unguarded_write_reachable(callee, funcs, depth=depth - 1, seen=seen)
        for callee in _direct_callees(fn)
        if callee != name
    )


def test_no_unguarded_local_write_follows_a_github_ops_call_in_state_py():
    """Direction 1: a ``coord/state.py`` function that calls ``github_ops``
    directly (the upstream write) must not reach an unguarded local write —
    in itself or in any ``_*_local`` helper it delegates to.

    Deliberately introducing a new ``_*_local`` function that calls
    ``github_ops.<mutate>(...)`` and then a bare ``conn.commit()``-style
    write with no ``coord.db.retry_on_locked`` anywhere in its own frame (or
    a helper's) makes this red — this is the #2846 ratchet's first leg.
    """
    state_funcs = _module_functions(_parse("state.py"))
    violations = sorted(
        name
        for name, fn in state_funcs.items()
        if _touches_github_ops(fn) and _unguarded_write_reachable(name, state_funcs)
    )
    assert not violations, (
        "the following coord/state.py function(s) call github_ops (an "
        "irreversible upstream write) and then reach an unguarded local "
        "write — a transient `database is locked` there propagates as a "
        "bare OperationalError, which the daemon's blanket `except "
        "Exception` turns into a 503 even though the upstream write already "
        "succeeded (#2689/#2846). Wrap the write in coord.db.retry_on_locked "
        "the same way the sibling cache-mirror writes are guarded:\n"
        + "\n".join(violations)
    )


def test_no_unguarded_local_write_follows_a_gh_call_from_github_ops_py():
    """Direction 2: a ``coord/github_ops.py`` function that calls the
    low-level ``_gh()`` seam (an upstream write) and then bridges into
    ``coord/state.py`` via ``state.<fn>(...)`` / ``_state.<fn>(...)`` must
    not reach an unguarded local write on that ``state.py`` side either —
    this is exactly the ``_capture_comment_write`` -> ``record_issue_comment
    _capture`` -> ``_record_issue_comment_capture_local`` shape #2846 fixed,
    and the ratchet's second leg (the mirror image of the first: #2689's own
    fixed sites are all called direction-1-style, entirely from within
    ``state.py``; this direction closes the gap a cross-module bridge would
    otherwise leave open).
    """
    state_funcs = _module_functions(_parse("state.py"))
    gh_funcs = _module_functions(_parse("github_ops.py"))

    violations: list[str] = []
    for gh_name, fn in gh_funcs.items():
        if not _has_call_named(fn, "_gh"):
            continue
        targets = {
            sub.func.attr
            for sub in ast.walk(fn)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id in ("state", "_state")
        }
        for target in sorted(targets):
            if _unguarded_write_reachable(target, state_funcs):
                violations.append(f"github_ops.{gh_name} -> state.{target}")

    assert not violations, (
        "the following coord/github_ops.py function(s) perform an upstream "
        "`_gh()` write and then bridge into a coord/state.py function that "
        "reaches an unguarded local write (#2689/#2846 shape, cross-module "
        "leg). Wrap the state.py-side write in coord.db.retry_on_locked:\n"
        + "\n".join(violations)
    )
