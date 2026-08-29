"""#1946 (Store Service Phase B): the **Python client** on the resource routes.

#1944 added the resource-shaped routes; #1945 measured who still calls the RPC
ones.  This issue flips the clients over, one deploy lane at a time, and this
file is the Python CLI's leg of that — the lane that migrates first because it
is editable on the operator box and trivially revertible.

Three things are measured here, and only the first is "did the URL change":

1. **End to end against the real daemon.**  Each ``coord.state`` seam wrapper
   is driven with a board service configured, its HTTP call routed into a live
   Starlette app in-process, and the assertion is on the resulting **DB row** —
   not on a mock.  A payload that renamed a field wrong (``reason`` →
   ``failure_reason``, ``milestone_number`` → ``milestone``) would still "call
   the right URL" and would still be a silent data-loss bug; only reading the
   row back catches it.

2. **The 405 trap.**  ``docs/STORE_SERVICE.md``: *"Endpoint and caller must
   never change in one commit ... the alternative is the 405 trap where a new
   client meets an old daemon."*  ``coord serve`` is long-running, so an
   operator who pulls this commit into an editable checkout has a new client
   against an old daemon until they restart it.  Every write must survive that
   window by falling back to the RPC route it superseded.

3. **What must NOT be read as deploy lag.**  A 409 (open children) is a real
   answer from a new daemon, not a missing route, and must propagate.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

import coord.client as cc
from coord import board_service
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.serve_app import build_app

ISSUE = 42
AID = "aid-1946"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    """Backing file for the daemon's read-only store."""
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def rw_db(tmp_path: Path):
    """The DB the daemon's handlers actually write through (thread-safe).

    TestClient runs the handler on a worker thread, which the autouse
    ``coord_db`` ``:memory:`` connection cannot serve — same twin as
    ``tests/test_serve.py`` / ``tests/test_serve_rest_routes.py``.
    """
    from coord import db

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


@pytest.fixture
def thin_client(monkeypatch, file_db: Path, valid_config_path: Path, rw_db):
    """A thin client whose daemon calls land in a real in-process daemon.

    Both transports are routed into the same Starlette app, so a fallback to
    the RPC route is exercised for real rather than simulated — and so the
    *only* thing distinguishing "migrated" from "not migrated" in these tests
    is which route the daemon actually received.
    """
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    daemon = TestClient(app)
    seen: list[tuple[str, str]] = []

    monkeypatch.setattr(
        cc, "resolve_board_service",
        lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
    )

    def _post_record(svc, path, payload, **kw):
        seen.append(("POST", path))
        resp = daemon.post(path, json=payload)
        resp.raise_for_status()
        return resp.json()

    def _request_resource(svc, method, path, payload=None, **kw):
        seen.append((method, path))
        resp = daemon.request(method, path, json=payload)
        resp.raise_for_status()
        return resp.json()

    monkeypatch.setattr(cc, "post_record", _post_record)
    monkeypatch.setattr(cc, "request_resource", _request_resource)
    daemon.seen = seen  # type: ignore[attr-defined]
    return daemon


@pytest.fixture
def no_gh(monkeypatch) -> list[tuple]:
    """Stub every tracker call the daemon-side handlers make, recording them."""
    calls: list[tuple] = []

    def _rec(name):
        def _inner(*args, **kwargs):
            calls.append((name, args, kwargs))
            return None
        return _inner

    for fn in (
        "edit_issue", "close_issue", "reopen_issue", "assign_issue_milestone",
        "unassign_issue_milestone", "post_issue_comment",
    ):
        monkeypatch.setattr(f"coord.github_ops.{fn}", _rec(fn))
    def _labels(slug, number, add=(), remove=()):
        calls.append(("change_issue_labels", slug, number, sorted(add), sorted(remove)))
        # Mirrors the real helper's `(new_labels, changed)` contract.
        return sorted({"existing", *add} - set(remove)), bool(add or remove)

    monkeypatch.setattr("coord.github_ops.change_issue_labels", _labels)
    return calls


def _seed_issue(conn, number: int = ISSUE, labels: str = '["existing"]') -> None:
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "milestone_number, milestone_title, synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("api", number, "an issue", "the body", "open", labels, None, None, 1.0),
    )
    conn.commit()


def _seed_assignment(conn, aid: str = AID) -> None:
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) VALUES (?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "owner/api", ISSUE, "An issue", "running", "work"),
    )
    conn.commit()


def _issue(conn, number: int = ISSUE):
    return conn.execute(
        "SELECT * FROM issues WHERE repo_name='api' AND number=?", (number,)
    ).fetchone()


def _assignment(conn, aid: str = AID):
    return conn.execute(
        "SELECT * FROM assignments WHERE assignment_id=?", (aid,)
    ).fetchone()


# ══════════════════════════════════════════════════════════════════════════
# 1. The migrated seams reach the resource routes AND land the right data
# ══════════════════════════════════════════════════════════════════════════


def test_assignment_field_setters_all_reach_the_assignment_patch(thin_client, rw_db):
    """The ten ``/assignment-usage``-family writes become one PATCH each.

    Asserting the resulting row, not the mock, is the point: three of these
    fields are renamed on the resource route, and a rename that silently
    dropped the value would pass a URL-only assertion.
    """
    from coord import state

    _seed_assignment(rw_db)

    state.update_assignment_cost(AID, 0.42)
    state.update_assignment_tokens(AID, input_tokens=100, output_tokens=50, num_turns=12)
    state.mark_assignment_interactive(AID)
    state.update_assignment_smoke_tests(AID, ["poke the thing"])
    state.update_assignment_completion_summary(AID, "did the thing")
    state.update_assignment_stop_reason(AID, "end_turn")
    state.update_assignment_claude_session_id(AID, "ses-abc")

    assert thin_client.seen == [("PATCH", f"/assignment/{AID}")] * 7, (
        "every assignment field-setter must use the resource route (#1946)"
    )
    row = _assignment(rw_db)
    assert row["cost_usd"] == 0.42
    assert (row["input_tokens"], row["output_tokens"], row["num_turns"]) == (100, 50, 12)
    assert row["is_interactive"]
    assert json.loads(row["smoke_tests"]) == ["poke the thing"]
    assert row["completion_summary"] == "did the thing"
    assert row["stop_reason"] == "end_turn"
    assert row["claude_session_id"] == "ses-abc"


def test_failure_reason_survives_the_field_rename(thin_client, rw_db):
    """``/assignment-failure-reason``'s ``reason`` is ``failure_reason`` on PATCH.

    The rename is the whole risk in this one: a payload still spelled
    ``reason`` is an *unknown field*, which the resource handler answers 400
    to — so the row would keep its old status and the operator would never be
    told the worker failed.
    """
    from coord import state

    _seed_assignment(rw_db)
    state.set_assignment_failure_reason(AID, "worktree add failed")

    assert thin_client.seen == [("PATCH", f"/assignment/{AID}")]
    row = _assignment(rw_db)
    assert row["failure_reason"] == "worktree add failed"
    assert row["status"] == "failed", "a failure reason also flips the row terminal"


def test_issue_writes_all_reach_the_issue_patch(thin_client, rw_db, no_gh):
    """The seven ``/issue-*`` mutations become one PATCH each, in one place."""
    from coord import state

    _seed_issue(rw_db)

    assert state.edit_issue_content(
        "api", ISSUE, title="new title", repo_github="owner/api"
    ) is True
    state.assign_issue_milestone(
        "api", ISSUE, 7, milestone_title="v1.0", repo_github="owner/api"
    )
    assert _issue(rw_db)["milestone_number"] == 7
    state.unassign_issue_milestone("api", ISSUE, repo_github="owner/api")
    state.close_issue("api", ISSUE, comment="done", repo_github="owner/api")
    state.reopen_issue("api", ISSUE, comment="my bad", repo_github="owner/api")

    assert thin_client.seen == [("PATCH", f"/issue/api/{ISSUE}")] * 5
    row = _issue(rw_db)
    assert row["title"] == "new title"
    # `milestone: null` CLEARS — omitting the key would have left 7 in place,
    # which is exactly the bug an absent-vs-null mistake produces.
    assert row["milestone_number"] is None
    assert row["milestone_title"] is None
    names = [c[0] for c in no_gh]
    assert names == [
        "edit_issue", "assign_issue_milestone", "unassign_issue_milestone",
        "close_issue", "reopen_issue",
    ], "the tracker write still runs daemon-side, once per call"


def test_label_writes_reach_the_issue_patch_and_keep_their_return_contract(
    thin_client, rw_db, no_gh
):
    """``add_labels``/``remove_labels`` (tracker) and ``labels`` (cache replace).

    ``apply_issue_labels`` returns ``(labels, changed)``; the resource route
    spells the second half ``labels_changed`` where the RPC route spelled it
    ``changed``, so the return contract is asserted, not just the route.
    """
    from coord import state

    _seed_issue(rw_db)

    labels, changed = state.apply_issue_labels(
        "api", ISSUE, add={"bug"}, remove=set(), repo_github="owner/api"
    )
    assert changed is True
    assert "bug" in labels
    assert ("change_issue_labels", "owner/api", ISSUE, ["bug"], []) in no_gh

    assert state.update_issue_labels("api", ISSUE, ["only-this"]) is True

    assert thin_client.seen == [("PATCH", f"/issue/api/{ISSUE}")] * 2
    assert json.loads(_issue(rw_db)["labels"]) == ["only-this"]


def test_comment_seams_reach_the_comments_subresource(
    thin_client, rw_db, no_gh, monkeypatch
):
    """``comment_on_issue`` / ``sync_issue_comments`` use the sub-resource."""
    from coord import state

    _seed_issue(rw_db)
    state.comment_on_issue("api", ISSUE, "a correction", repo_github="owner/api")

    monkeypatch.setattr(
        "coord.github_ops.get_issue_comments",
        lambda *a, **k: [
            {"url": "https://github.com/owner/api/issues/42#issuecomment-9001",
             "body": "hi", "author": {"login": "someone"},
             "createdAt": "2026-01-01T00:00:00Z"},
        ],
    )
    assert state.sync_issue_comments("api", ISSUE, repo_github="owner/api") == 1

    assert thin_client.seen == [("POST", f"/issue/api/{ISSUE}/comments")] * 2
    assert ("post_issue_comment", ("owner/api", ISSUE, "a correction"), {}) in no_gh
    assert rw_db.execute(
        "SELECT COUNT(*) c FROM issue_comments WHERE gh_comment_id=9001"
    ).fetchone()["c"] == 1


def test_reads_use_the_resource_gets_not_the_rpc_projections(thin_client, rw_db):
    """#1944 pointed the two read-only RPC routes at resource GETs; use them.

    ``get_issue_test_mode`` derives the policy client-side from the issue's
    labels (``coord.models.test_mode_from_labels``, the single reading of
    those labels per #2024) rather than asking the daemon to name it.
    """
    from coord import state

    _seed_issue(rw_db, labels='["coord", "test-mode:smoke"]')
    _seed_assignment(rw_db)
    rw_db.execute(
        "UPDATE assignments SET test_plan=? WHERE assignment_id=?",
        (json.dumps({"steps": ["a"], "blockers": []}), AID),
    )
    rw_db.commit()

    assert state.get_issue_test_mode("api", ISSUE) == "smoke"
    assert state.get_test_plan(AID) == {"steps": ["a"], "blockers": []}
    assert state.get_issue_test_mode("api", 999) is None, (
        "an unknown issue is not a policy"
    )
    for _method, path in thin_client.seen:
        assert path not in ("/issue-test-mode", "/assignment-test-plan"), (
            "#1946: the deprecated read projections must no longer be called"
        )


# ══════════════════════════════════════════════════════════════════════════
# 2. The 405 trap: a new client against a daemon that predates #1944
# ══════════════════════════════════════════════════════════════════════════


class _OldDaemon:
    """Answers every resource route the way a pre-#1944 daemon would.

    ``PATCH /issue/{r}/{n}`` and ``PATCH /assignment/{id}``: those paths exist
    for GET on an old daemon, so Starlette answers **405**.
    ``POST /issue/{r}/{n}/comments``: the path did not exist at all → **404**.
    """

    def __init__(self):
        self.resource_attempts: list[tuple[str, str]] = []
        self.rpc_calls: list[tuple[str, dict]] = []

    def request_resource(self, svc, method, path, payload=None, **kw):
        self.resource_attempts.append((method, path))
        status = 404 if path.endswith("/comments") else 405
        request = httpx.Request(method, f"http://daemon:7435{path}")
        raise httpx.HTTPStatusError(
            str(status),
            request=request,
            response=httpx.Response(status, request=request),
        )

    def post_record(self, svc, path, payload, **kw):
        self.rpc_calls.append((path, payload))
        return {"ok": True, "updated": True, "labels": ["bug"], "changed": True,
                "synced": 0}


@pytest.fixture
def old_daemon(monkeypatch) -> _OldDaemon:
    d = _OldDaemon()
    monkeypatch.setattr(
        cc, "resolve_board_service",
        lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
    )
    monkeypatch.setattr(cc, "request_resource", d.request_resource)
    monkeypatch.setattr(cc, "post_record", d.post_record)
    return d


@pytest.mark.parametrize(
    ("call", "rpc_path"),
    [
        (lambda s: s.update_assignment_cost(AID, 1.5), "/assignment-usage"),
        (lambda s: s.set_assignment_failure_reason(AID, "boom"),
         "/assignment-failure-reason"),
        (lambda s: s.update_assignment_claude_session_id(AID, "ses"),
         "/assignment-session-id"),
        (lambda s: s.edit_issue_content("api", ISSUE, title="t"), "/issue-edit"),
        (lambda s: s.update_issue_labels("api", ISSUE, ["x"]), "/issue-labels"),
        (lambda s: s.apply_issue_labels("api", ISSUE, add={"bug"}, remove=set()),
         "/issue-label"),
        (lambda s: s.assign_issue_milestone("api", ISSUE, 7), "/issue-milestone"),
        (lambda s: s.unassign_issue_milestone("api", ISSUE),
         "/issue-milestone-remove"),
        (lambda s: s.close_issue("api", ISSUE), "/issue-close"),
        (lambda s: s.reopen_issue("api", ISSUE), "/issue-reopen"),
        (lambda s: s.comment_on_issue("api", ISSUE, "hi"), "/issue-comment"),
        (lambda s: s.sync_issue_comments("api", ISSUE), "/issue-comments"),
    ],
)
def test_every_migrated_write_survives_an_old_daemon(old_daemon, coord_db, call, rpc_path):
    """A 404/405 falls back to the superseded RPC route instead of erroring.

    Without this, merging #1946 breaks every label / close / comment write on
    the operator box in the window between `git pull` and `coord serve`'s
    restart — the exact 405 trap `docs/STORE_SERVICE.md` warns about.
    """
    from coord import state

    call(state)

    assert len(old_daemon.resource_attempts) == 1, "the resource route is tried first"
    assert [p for p, _ in old_daemon.rpc_calls] == [rpc_path], (
        "and a 404/405 falls back to exactly the route #1944 says it supersedes"
    )


def test_the_old_daemon_verdict_is_memoized_per_route(old_daemon, coord_db):
    """One doomed round trip per route per process, not one per write.

    Memoizing matters because `coord notify` can make dozens of assignment
    writes in a single invocation; paying a 405 on each would turn a stale
    lane into a latency problem on top of a correctness one.
    """
    from coord import state

    for _ in range(4):
        state.update_assignment_cost(AID, 1.5)

    assert old_daemon.resource_attempts == [("PATCH", f"/assignment/{AID}")]
    assert len(old_daemon.rpc_calls) == 4


def test_the_memo_does_not_leak_across_route_families(old_daemon, coord_db):
    """An issue route's 405 must not un-migrate the assignment routes.

    They are separate deploys of nothing — the same daemon serves both — but
    keying the memo per route family is what keeps one family's *addressing*
    problem (see `board_service.resource_addressable`) from silently reverting
    the rest of the migration.
    """
    from coord import state

    state.edit_issue_content("api", ISSUE, title="t")
    state.update_assignment_cost(AID, 1.5)

    assert old_daemon.resource_attempts == [
        ("PATCH", f"/issue/api/{ISSUE}"),
        ("PATCH", f"/assignment/{AID}"),
    ]


def test_a_409_is_not_deploy_lag(monkeypatch, coord_db):
    """Only 404/405 mean "old daemon"; a real refusal must reach the caller.

    #1196's open-children guard answers 409 from the *new* route. Treating
    that as a missing route would retry it on the RPC route, get the same 409,
    and — worse — make the guard look flaky rather than deliberate.
    """
    from coord import state
    from coord.github_ops import IssueHasOpenChildrenError

    monkeypatch.setattr(
        cc, "resolve_board_service",
        lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
    )

    def _409(svc, method, path, payload=None, **kw):
        request = httpx.Request(method, f"http://daemon:7435{path}")
        raise httpx.HTTPStatusError(
            "409", request=request,
            response=httpx.Response(
                409,
                json={"error": "open children",
                      "detail": "refusing to close: open children #1039"},
                request=request,
            ),
        )

    monkeypatch.setattr(cc, "request_resource", _409)
    monkeypatch.setattr(
        cc, "post_record",
        lambda *a, **k: pytest.fail("a 409 must not fall back to the RPC route"),
    )

    with pytest.raises(IssueHasOpenChildrenError, match="open children #1039"):
        state.close_issue("api", 1041, repo_github="owner/api")


# ══════════════════════════════════════════════════════════════════════════
# 3. The seam that cannot migrate, and must not be mistaken for one that didn't
# ══════════════════════════════════════════════════════════════════════════


def test_a_slugged_repo_name_never_attempts_the_resource_route(monkeypatch, coord_db):
    """``owner/repo`` is not addressable as one path segment — skip the probe.

    ``record_issue_comment_capture`` is reached from
    ``github_ops.post_issue_comment`` with the ``gh --repo`` slug as its
    ``repo_name`` (``issue_comments`` is keyed on the slug; ``issues`` on the
    short name). Attempting ``POST /issue/owner/repo/42/comments`` would 404 —
    on a perfectly current daemon — and, taken as deploy lag, would un-migrate
    the *other* comment seam for the rest of the process.
    """
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service",
        lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
    )
    monkeypatch.setattr(
        cc, "request_resource",
        lambda *a, **k: pytest.fail("must not probe an unaddressable resource path"),
    )
    seen: list[str] = []
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: seen.append(path) or {"ok": True},
    )

    state.record_issue_comment_capture(
        repo_name="owner/api", issue_number=ISSUE, body="x", gh_comment_id=1,
    )
    assert seen == ["/issue-comments"]

    # ...and the short-named comment seam is untouched by that decision.
    assert board_service.resource_addressable("api") is True
    assert board_service.resource_addressable("owner/api") is False
