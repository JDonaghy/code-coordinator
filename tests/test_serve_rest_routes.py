"""#1944 (Store Service Phase B): the resource-shaped daemon routes.

The acceptance bar for this issue is *equivalence*, so that is what the bulk of
this file measures: every new resource route is driven side-by-side with the
RPC route it will eventually replace, against two identical seeded rows, and
the resulting **board state** (the ``issues`` / ``assignments`` rows) plus the
recorded tracker calls must match exactly.

The RPC routes themselves are untouched by #1944 — ``tests/test_serve.py``
covers them and is deliberately not modified.  What this file adds on top of
the equivalence sweep is the resource routes' own surface: validation refusals,
the two error codes that must survive the reshaping (422 label-not-found, 409
open-children), the comments sub-resource, and the OpenAPI deprecation markers.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.serve_app import (
    RPC_SUPERSEDED_BY_RESOURCE,
    build_app,
    openapi_spec,
)

RPC_ISSUE = 101
REST_ISSUE = 202
RPC_AID = "rpc-aid"
REST_AID = "rest-aid"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    """A minimal read-only-store backing file (the daemon's /board source)."""
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe coord.db override — see ``tests/test_serve.py``'s twin.

    TestClient runs the async handler on a worker thread, which the autouse
    ``coord_db`` ``:memory:`` connection cannot serve.
    """
    from coord import db

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


@pytest.fixture
def cli(file_db: Path, valid_config_path: Path):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as client:
        yield client


def _seed_issue(conn, number: int, **overrides) -> None:
    row = {
        "title": "an issue",
        "body": "the body",
        "state": "open",
        "labels": '["existing"]',
        "milestone_number": None,
        "milestone_title": None,
    }
    row.update(overrides)
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "milestone_number, milestone_title, synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "api", number, row["title"], row["body"], row["state"], row["labels"],
            row["milestone_number"], row["milestone_title"], 1.0,
        ),
    )
    conn.commit()


def _seed_assignment(conn, aid: str) -> None:
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) VALUES (?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "owner/api", 7, "An issue", "running", "work"),
    )
    conn.commit()


def _issue_row(conn, number: int) -> dict:
    row = conn.execute(
        "SELECT * FROM issues WHERE repo_name='api' AND number=?", (number,)
    ).fetchone()
    assert row is not None, f"issue {number} vanished"
    out = dict(row)
    out.pop("number")
    return out


def _assignment_row(conn, aid: str) -> dict:
    row = conn.execute(
        "SELECT * FROM assignments WHERE assignment_id=?", (aid,)
    ).fetchone()
    assert row is not None, f"assignment {aid} vanished"
    out = dict(row)
    out.pop("assignment_id")
    return out


@pytest.fixture
def gh_calls(monkeypatch) -> list[tuple]:
    """Record every tracker call, with the issue number normalised away.

    The two legs of an equivalence case act on *different* issue numbers, so
    the number is the one thing that legitimately differs; everything else
    about the recorded call must be identical.
    """
    calls: list[tuple] = []

    def _rec(name):
        def _inner(slug, number, **kwargs):
            calls.append((name, slug, tuple(sorted(kwargs.items()))))
            if name == "change_issue_labels":
                return ["existing", "bug"], True
            return None

        return _inner

    for name in (
        "edit_issue",
        "assign_issue_milestone",
        "unassign_issue_milestone",
        "close_issue",
        "reopen_issue",
        "change_issue_labels",
    ):
        monkeypatch.setattr(f"coord.github_ops.{name}", _rec(name))

    # post_issue_comment / get_issue_comments take positional bodies, so they
    # need their own shims rather than the **kwargs one above.
    monkeypatch.setattr(
        "coord.github_ops.post_issue_comment",
        lambda slug, number, body: calls.append(("post_issue_comment", slug, body)),
    )
    monkeypatch.setattr(
        "coord.github_ops.assign_issue_milestone",
        lambda slug, number, ms: calls.append(("assign_issue_milestone", slug, ms)),
    )
    monkeypatch.setattr(
        "coord.github_ops.unassign_issue_milestone",
        lambda slug, number: calls.append(("unassign_issue_milestone", slug)),
    )
    return calls


# ── equivalence: PATCH /issue/{repo}/{n} vs the seven RPC issue routes ────────


@dataclass
class IssueCase:
    """One RPC route + the PATCH body that must be indistinguishable from it."""

    name: str
    rpc_path: str
    rpc_body: dict
    patch_body: dict
    seed: dict = field(default_factory=dict)


ISSUE_CASES = [
    IssueCase(
        name="issue-edit / title+body",
        rpc_path="/issue-edit",
        rpc_body={"title": "new title", "body": "new body"},
        patch_body={"title": "new title", "body": "new body"},
    ),
    IssueCase(
        name="issue-edit / title only",
        rpc_path="/issue-edit",
        rpc_body={"title": "only the title"},
        patch_body={"title": "only the title"},
    ),
    IssueCase(
        name="issue-label / add+remove",
        rpc_path="/issue-label",
        rpc_body={"add": ["bug"], "remove": ["stale"]},
        patch_body={"add_labels": ["bug"], "remove_labels": ["stale"]},
    ),
    IssueCase(
        name="issue-labels / cache replace",
        rpc_path="/issue-labels",
        rpc_body={"labels": ["ready", "p1"]},
        patch_body={"labels": ["ready", "p1"]},
    ),
    IssueCase(
        name="issue-milestone / assign",
        rpc_path="/issue-milestone",
        rpc_body={"milestone_number": 60, "milestone_title": "Store Service"},
        patch_body={"milestone": 60, "milestone_title": "Store Service"},
    ),
    IssueCase(
        name="issue-milestone-remove / clear",
        rpc_path="/issue-milestone-remove",
        rpc_body={},
        patch_body={"milestone": None},
        seed={"milestone_number": 60, "milestone_title": "Store Service"},
    ),
    IssueCase(
        name="issue-close",
        rpc_path="/issue-close",
        rpc_body={"comment": "done here", "force": True},
        patch_body={"state": "closed", "comment": "done here", "force": True},
    ),
    IssueCase(
        name="issue-reopen",
        rpc_path="/issue-reopen",
        rpc_body={"comment": "not done after all"},
        patch_body={"state": "open", "comment": "not done after all"},
        seed={"state": "closed"},
    ),
]


@pytest.mark.parametrize("case", ISSUE_CASES, ids=lambda c: c.name)
def test_patch_issue_is_equivalent_to_the_rpc_route(
    case: IssueCase, cli, rw_db, gh_calls
):
    """#1944 acceptance: drive both routes, compare the resulting board state.

    Two identically-seeded issues in the same repo; the RPC route mutates one
    and ``PATCH /issue/api/{n}`` mutates the other.  The stored rows must be
    byte-identical (modulo the issue number) and the tracker must have been
    asked to do exactly the same thing.
    """
    _seed_issue(rw_db, RPC_ISSUE, **case.seed)
    _seed_issue(rw_db, REST_ISSUE, **case.seed)

    rpc_resp = cli.post(
        case.rpc_path,
        json={
            "repo_name": "api",
            "issue_number": RPC_ISSUE,
            "repo_github": "owner/api",
            **case.rpc_body,
        },
    )
    assert rpc_resp.status_code == 200, rpc_resp.text
    rpc_gh = list(gh_calls)
    gh_calls.clear()

    rest_resp = cli.patch(
        f"/issue/api/{REST_ISSUE}",
        json={"repo_github": "owner/api", **case.patch_body},
    )
    assert rest_resp.status_code == 200, rest_resp.text
    rest_gh = list(gh_calls)

    assert rest_gh == rpc_gh
    assert _issue_row(rw_db, REST_ISSUE) == _issue_row(rw_db, RPC_ISSUE)
    assert rest_resp.json()["updated"] is True


def test_patch_issue_applies_several_mutations_in_a_fixed_order(cli, rw_db, gh_calls):
    """One PATCH standing in for four RPC round-trips, ordered content →
    labels → milestone → state regardless of request key order."""
    _seed_issue(rw_db, REST_ISSUE)
    resp = cli.patch(
        f"/issue/api/{REST_ISSUE}",
        json={
            "state": "closed",
            "milestone": 60,
            "add_labels": ["bug"],
            "title": "retitled",
            "repo_github": "owner/api",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] == ["content", "labels", "milestone", "state"]
    assert [c[0] for c in gh_calls] == [
        "edit_issue",
        "change_issue_labels",
        "assign_issue_milestone",
        "close_issue",
    ]
    row = _issue_row(rw_db, REST_ISSUE)
    assert row["title"] == "retitled"
    assert sorted(json.loads(row["labels"])) == ["bug", "existing"]
    assert row["milestone_number"] == 60


def test_patch_issue_reports_the_label_set_like_the_rpc_route(cli, rw_db, gh_calls):
    """``/issue-label`` returns ``{labels, changed}``; the PATCH surfaces the
    same two values under ``labels`` / ``labels_changed`` so a migrating caller
    keeps its read-back."""
    _seed_issue(rw_db, RPC_ISSUE)
    _seed_issue(rw_db, REST_ISSUE)
    rpc = cli.post(
        "/issue-label",
        json={"repo_name": "api", "issue_number": RPC_ISSUE, "add": ["bug"]},
    ).json()
    rest = cli.patch(f"/issue/api/{REST_ISSUE}", json={"add_labels": ["bug"]}).json()
    assert rest["labels"] == rpc["labels"]
    assert rest["labels_changed"] == rpc["changed"]


def test_patch_issue_with_no_mutations_is_a_200_noop(cli, rw_db, gh_calls):
    _seed_issue(rw_db, REST_ISSUE)
    before = _issue_row(rw_db, REST_ISSUE)
    resp = cli.patch(f"/issue/api/{REST_ISSUE}", json={})
    assert resp.status_code == 200
    assert resp.json() == {
        "updated": False,
        "applied": [],
        "labels": None,
        "labels_changed": None,
    }
    assert gh_calls == []
    assert _issue_row(rw_db, REST_ISSUE) == before


def test_patch_issue_labels_replace_on_unseeded_row_matches_the_rpc_route(
    cli, rw_db, gh_calls
):
    """``/issue-labels`` reports ``updated: False`` when the cache-replace UPDATE
    touches no row (the issue isn't cached yet — #2846 self-heals on the next
    sync). The PATCH must surface the same "nothing happened" signal instead of
    claiming success just because the call didn't raise."""
    rpc_resp = cli.post(
        "/issue-labels",
        json={"repo_name": "api", "issue_number": RPC_ISSUE, "labels": ["ready"]},
    )
    assert rpc_resp.status_code == 200, rpc_resp.text
    assert rpc_resp.json() == {"updated": False}

    rest_resp = cli.patch(f"/issue/api/{REST_ISSUE}", json={"labels": ["ready"]})
    assert rest_resp.status_code == 200, rest_resp.text
    assert rest_resp.json() == {
        "updated": False,
        "applied": [],
        "labels": None,
        "labels_changed": None,
    }
    assert gh_calls == []
    # No row was inserted by either route — the mutation is a true no-op.
    assert (
        rw_db.execute(
            "SELECT COUNT(*) FROM issues WHERE repo_name='api' AND number IN (?, ?)",
            (RPC_ISSUE, REST_ISSUE),
        ).fetchone()[0]
        == 0
    )


# ── PATCH /issue: refusals ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ({"titel": "typo"}, "unknown field"),
        ({"labels": ["a"], "add_labels": ["b"]}, "mutually exclusive"),
        ({"state": "merged"}, "state must be"),
        ({"comment": "orphan"}, "comment requires a state change"),
    ],
)
def test_patch_issue_refuses_contradictory_bodies(cli, rw_db, body, needle):
    """A PATCH that silently ignored a typo'd key would hand the caller a 200
    for a write that never happened — the one regression a mechanical client
    migration (#1946) cannot see."""
    _seed_issue(rw_db, REST_ISSUE)
    resp = cli.patch(f"/issue/api/{REST_ISSUE}", json=body)
    assert resp.status_code == 400
    assert needle in resp.json()["error"]


def test_patch_issue_non_integer_number_is_404_like_the_get(cli, rw_db):
    assert cli.patch("/issue/api/notanumber", json={"title": "x"}).status_code == 404
    assert cli.get("/issue/api/notanumber").status_code == 404


def test_patch_issue_label_not_found_is_422_like_the_rpc_route(cli, rw_db, monkeypatch):
    """The 422-vs-503 distinction ``/issue-label`` draws survives the reshaping."""
    from coord.github_ops import GhNotFound

    monkeypatch.setattr(
        "coord.github_ops.change_issue_labels",
        lambda *a, **k: (_ for _ in ()).throw(GhNotFound("no such label 'ghost'")),
    )
    _seed_issue(rw_db, RPC_ISSUE)
    _seed_issue(rw_db, REST_ISSUE)
    rpc = cli.post(
        "/issue-label",
        json={"repo_name": "api", "issue_number": RPC_ISSUE, "add": ["ghost"]},
    )
    rest = cli.patch(f"/issue/api/{REST_ISSUE}", json={"add_labels": ["ghost"]})
    assert rest.status_code == rpc.status_code == 422
    assert rest.json()["error"] == rpc.json()["error"] == "label not found"


def test_patch_issue_open_children_is_409_like_the_rpc_route(cli, rw_db, monkeypatch):
    """#1196's intentional close guard must not degrade into a generic 503."""
    from coord.github_ops import IssueHasOpenChildrenError

    monkeypatch.setattr(
        "coord.github_ops.close_issue",
        lambda *a, **k: (_ for _ in ()).throw(IssueHasOpenChildrenError("2 open")),
    )
    _seed_issue(rw_db, RPC_ISSUE)
    _seed_issue(rw_db, REST_ISSUE)
    rpc = cli.post(
        "/issue-close", json={"repo_name": "api", "issue_number": RPC_ISSUE}
    )
    rest = cli.patch(f"/issue/api/{REST_ISSUE}", json={"state": "closed"})
    assert rest.status_code == rpc.status_code == 409
    assert rest.json()["error"] == rpc.json()["error"] == "open children"


def test_patch_issue_reports_partial_progress_on_failure(cli, rw_db, gh_calls, monkeypatch):
    """A multi-field PATCH that dies halfway must say what already landed —
    the failure mode the RPC routes did not have, because each was its own
    round-trip with its own status code."""
    monkeypatch.setattr(
        "coord.github_ops.close_issue",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh exploded")),
    )
    _seed_issue(rw_db, REST_ISSUE)
    resp = cli.patch(
        f"/issue/api/{REST_ISSUE}", json={"title": "retitled", "state": "closed"}
    )
    assert resp.status_code == 503
    assert resp.json()["applied"] == ["content"]
    assert _issue_row(rw_db, REST_ISSUE)["title"] == "retitled"


# ── /issue/{repo}/{n}/comments ───────────────────────────────────────────────


def test_post_comments_default_action_matches_issue_comment(cli, rw_db, gh_calls):
    _seed_issue(rw_db, RPC_ISSUE)
    _seed_issue(rw_db, REST_ISSUE)
    cli.post(
        "/issue-comment",
        json={
            "repo_name": "api",
            "issue_number": RPC_ISSUE,
            "body": "hello",
            "repo_github": "owner/api",
        },
    )
    rpc_gh = list(gh_calls)
    gh_calls.clear()
    resp = cli.post(
        f"/issue/api/{REST_ISSUE}/comments",
        json={"body": "hello", "repo_github": "owner/api"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "action": "post", "synced": None}
    assert gh_calls == rpc_gh == [("post_issue_comment", "owner/api", "hello")]


def test_post_comments_capture_matches_issue_comments_capture(cli, rw_db):
    """``action=capture`` writes the durable mirror, and the resource GET reads
    back exactly what ``GET /issue-comments`` does."""
    cli.post(
        "/issue-comments",
        json={
            "action": "capture",
            "repo_name": "api",
            "issue_number": RPC_ISSUE,
            "body": "mirrored",
            "author": "coord",
        },
    )
    resp = cli.post(
        f"/issue/api/{REST_ISSUE}/comments",
        json={"action": "capture", "body": "mirrored", "author": "coord"},
    )
    assert resp.status_code == 200 and resp.json()["action"] == "capture"

    rpc_read = cli.get(
        "/issue-comments", params={"repo_name": "api", "issue_number": RPC_ISSUE}
    ).json()["comments"]
    rest_read = cli.get(f"/issue/api/{REST_ISSUE}/comments").json()["comments"]
    assert len(rest_read) == len(rpc_read) == 1
    assert [c["body"] for c in rest_read] == [c["body"] for c in rpc_read]
    assert [c["author"] for c in rest_read] == [c["author"] for c in rpc_read]


def test_post_comments_sync_matches_issue_comments_sync(cli, rw_db, monkeypatch):
    monkeypatch.setattr(
        "coord.github_ops.get_issue_comments",
        lambda slug, number: [
            {"id": 1, "author": "a", "body": "one", "created_at": 1.0},
            {"id": 2, "author": "b", "body": "two", "created_at": 2.0},
        ],
    )
    rpc = cli.post(
        "/issue-comments",
        json={"action": "sync", "repo_name": "api", "issue_number": RPC_ISSUE},
    ).json()
    rest = cli.post(f"/issue/api/{REST_ISSUE}/comments", json={"action": "sync"}).json()
    assert rest["synced"] == rpc["synced"]


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ({"body": "x", "actin": "post"}, "unknown field"),
        ({"action": "delete", "body": "x"}, "unknown action"),
        ({"action": "post"}, "body is required"),
        ({"action": "capture"}, "body is required"),
    ],
)
def test_post_comments_refuses_bad_bodies(cli, rw_db, body, needle):
    resp = cli.post(f"/issue/api/{REST_ISSUE}/comments", json=body)
    assert resp.status_code == 400
    assert needle in resp.json()["error"]


# ── equivalence: PATCH /assignment/{id} vs the three RPC field-setters ────────


@dataclass
class AssignmentCase:
    name: str
    rpc_path: str
    rpc_body: dict
    patch_body: dict


ASSIGNMENT_CASES = [
    AssignmentCase(
        name="assignment-usage / cost",
        rpc_path="/assignment-usage",
        rpc_body={"cost_usd": 0.55},
        patch_body={"cost_usd": 0.55},
    ),
    AssignmentCase(
        name="assignment-usage / tokens+turns",
        rpc_path="/assignment-usage",
        rpc_body={
            "input_tokens": 300, "output_tokens": 120,
            "cache_creation_tokens": 20, "cache_read_tokens": 10, "num_turns": 9,
        },
        patch_body={
            "input_tokens": 300, "output_tokens": 120,
            "cache_creation_tokens": 20, "cache_read_tokens": 10, "num_turns": 9,
        },
    ),
    AssignmentCase(
        name="assignment-usage / is_interactive",
        rpc_path="/assignment-usage",
        rpc_body={"is_interactive": True},
        patch_body={"is_interactive": True},
    ),
    AssignmentCase(
        name="assignment-usage / smoke_tests + stop_reason",
        rpc_path="/assignment-usage",
        rpc_body={"smoke_tests": ["poke it"], "stop_reason": "max_turns"},
        patch_body={"smoke_tests": ["poke it"], "stop_reason": "max_turns"},
    ),
    AssignmentCase(
        name="assignment-usage / completion_summary",
        rpc_path="/assignment-usage",
        rpc_body={"completion_summary": "shipped"},
        patch_body={"completion_summary": "shipped"},
    ),
    AssignmentCase(
        name="assignment-session-id",
        rpc_path="/assignment-session-id",
        rpc_body={"claude_session_id": "sess-42"},
        patch_body={"claude_session_id": "sess-42"},
    ),
    AssignmentCase(
        name="assignment-failure-reason",
        rpc_path="/assignment-failure-reason",
        rpc_body={"reason": "worktree add failed"},
        patch_body={"failure_reason": "worktree add failed"},
    ),
]


@pytest.mark.parametrize("case", ASSIGNMENT_CASES, ids=lambda c: c.name)
def test_patch_assignment_is_equivalent_to_the_rpc_route(case: AssignmentCase, cli, rw_db):
    """#1944 acceptance: same two-row, drive-both, compare-board-state proof as
    the issue sweep, for the assignment field-setters."""
    _seed_assignment(rw_db, RPC_AID)
    _seed_assignment(rw_db, REST_AID)

    rpc = cli.post(case.rpc_path, json={"assignment_id": RPC_AID, **case.rpc_body})
    assert rpc.status_code == 200, rpc.text
    rest = cli.patch(f"/assignment/{REST_AID}", json=case.patch_body)
    assert rest.status_code == 200, rest.text
    assert rest.json()["updated"] is True

    # finished_at is a wall-clock stamp the failure-reason path writes, so it
    # legitimately differs between the two legs.
    rpc_row = _assignment_row(rw_db, RPC_AID)
    rest_row = _assignment_row(rw_db, REST_AID)
    for row in (rpc_row, rest_row):
        row.pop("finished_at", None)
    assert rest_row == rpc_row


def test_patch_assignment_sets_several_fields_in_one_round_trip(cli, rw_db):
    _seed_assignment(rw_db, REST_AID)
    resp = cli.patch(
        f"/assignment/{REST_AID}",
        json={"cost_usd": 0.1, "num_turns": 3, "claude_session_id": "s1"},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] == ["cost_usd", "tokens", "claude_session_id"]
    row = _assignment_row(rw_db, REST_AID)
    assert row["cost_usd"] == 0.1
    assert row["num_turns"] == 3
    assert row["claude_session_id"] == "s1"


def test_patch_assignment_empty_body_is_a_noop(cli, rw_db):
    _seed_assignment(rw_db, REST_AID)
    before = _assignment_row(rw_db, REST_AID)
    resp = cli.patch(f"/assignment/{REST_AID}", json={})
    assert resp.status_code == 200
    assert resp.json() == {"updated": False, "applied": []}
    assert _assignment_row(rw_db, REST_AID) == before


def test_patch_assignment_refuses_unknown_fields(cli, rw_db):
    _seed_assignment(rw_db, REST_AID)
    resp = cli.patch(f"/assignment/{REST_AID}", json={"cost_us": 1.0})
    assert resp.status_code == 400
    assert "unknown field" in resp.json()["error"]


def test_patch_assignment_unknown_id_is_200_like_the_rpc_route(cli, rw_db):
    """Deliberate: the three RPC setters issue an UPDATE that matches no rows
    and return 200.  A new 404 would be a trap for the #1946 client migration,
    so the resource route matches them rather than improving on them."""
    rpc = cli.post("/assignment-usage", json={"assignment_id": "ghost", "cost_usd": 1.0})
    rest = cli.patch("/assignment/ghost", json={"cost_usd": 1.0})
    assert rpc.status_code == rest.status_code == 200


# ── the additive guarantee + the spec ────────────────────────────────────────


def test_every_superseded_rpc_route_still_exists_and_is_marked_deprecated(cli):
    """"Old routes are untouched" is the load-bearing half of #1944.  Every
    route named in the supersession table must still be routable *and* be
    flagged in the spec with a pointer at its replacement."""
    spec = openapi_spec()
    declared = {r.path for r in cli.app.routes if hasattr(r, "path")}
    for rpc_path, replacement in RPC_SUPERSEDED_BY_RESOURCE.items():
        assert rpc_path in declared, f"{rpc_path} was removed — #1944 is additive"
        operations = spec["paths"][rpc_path]
        assert operations, rpc_path
        for method, operation in operations.items():
            assert operation["deprecated"] is True, f"{method} {rpc_path}"
            assert replacement in operation["summary"], f"{method} {rpc_path}"


def test_the_new_resource_routes_are_not_deprecated(cli):
    spec = openapi_spec()
    for path, method in (
        ("/issue/{repo_name}/{number}", "patch"),
        ("/issue/{repo_name}/{number}/comments", "post"),
        ("/issue/{repo_name}/{number}/comments", "get"),
        ("/assignment/{assignment_id}", "patch"),
    ):
        assert "deprecated" not in spec["paths"][path][method], f"{method} {path}"


def test_resource_bodies_are_documented_from_the_declared_dtos(cli):
    """#1849's discipline, applied to the new routes: the request/response
    shapes are dataclasses in ``coord/rest_schema.py``, rendered into
    ``components/schemas`` — not hand-written schema literals that can drift."""
    from coord import rest_schema

    schemas = openapi_spec()["components"]["schemas"]
    for cls in (
        rest_schema.IssuePatch,
        rest_schema.IssuePatchResult,
        rest_schema.IssueCommentCreate,
        rest_schema.IssueCommentResult,
        rest_schema.IssueCommentList,
        rest_schema.AssignmentPatch,
        rest_schema.AssignmentPatchResult,
    ):
        assert cls.__name__ in schemas
        assert set(schemas[cls.__name__]["properties"]) == set(
            rest_schema.declared_fields(cls)
        )


def test_supersession_table_only_names_real_routes(cli):
    """``_mark_superseded_rpc_routes`` raises on a stale key rather than
    silently marking nothing; this pins that the table is currently clean."""
    spec_paths = set(openapi_spec()["paths"])
    assert set(RPC_SUPERSEDED_BY_RESOURCE) <= spec_paths


def test_resource_routes_honour_schema_negotiation(cli, rw_db):
    """#1943's middleware sits in front of every route, including these."""
    _seed_assignment(rw_db, REST_AID)
    bad = cli.patch(
        f"/assignment/{REST_AID}",
        json={"cost_usd": 1.0},
        headers={"X-Coord-Schema": "99"},
    )
    assert bad.status_code == 400
    assert "X-Coord-Schema" in bad.text or "schema" in bad.text.lower()
    ok = cli.patch(
        f"/assignment/{REST_AID}",
        json={"cost_usd": 1.0},
        headers={"X-Coord-Schema": "1"},
    )
    assert ok.status_code == 200
