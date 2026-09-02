"""Tests for the portable control-center read path (#584): DAO, daemon, client."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import logging.config
import os
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord import client as coord_client
from coord import merge_queue as mq
from coord import serve_app as serve_app_module
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.serve_app import _reload_config_if_stale, build_app


def _make_file_db(path: Path) -> None:
    """Create a real on-disk coord.db with a couple of representative rows.

    Writer commits and closes before the read-only SqliteStore opens it, so the
    main DB file holds the data (no WAL handshake needed for the test).
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, files_allowed, smoke_tests, "
        "review_findings, briefing) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work1", "laptop", "api", 42, "A work issue", "done", "work",
            '["a.py", "b.py"]', '["run the tests", "click the button"]',
            None, "x" * 5000,  # large briefing — must be dropped from the projection
        ),
    )
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, review_of_assignment_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("rev1", "server", "api", 42, "Review of #42", "done", "review", "work1"),
    )
    conn.execute(
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("laptop", "laptop.tailnet", '["python"]', '["api"]'),
    )
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '7')")
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
        "('pipeline_default_gates', '[\"review\", \"test\", \"merge\"]')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_file_db(p)
    return p


# ── DAO ─────────────────────────────────────────────────────────────────────

def test_dao_decodes_json_drops_briefing_and_reads_meta(file_db: Path):
    proj = SqliteStore(file_db).board_projection()
    assert proj["schema_version"] == 1
    assert proj["round_number"] == 7
    work = next(a for a in proj["assignments"] if a["assignment_id"] == "work1")
    # JSON columns decoded to native objects, not strings.
    assert work["files_allowed"] == ["a.py", "b.py"]
    assert work["smoke_tests"] == ["run the tests", "click the button"]
    # briefing dropped to keep the payload small (TUI never reads it).
    assert "briefing" not in work
    # columns absent from the Assignment dataclass are still served raw.
    assert "exit_code" in work and "test_plan" in work
    assert {m["name"] for m in proj["machines"]} == {"laptop"}
    assert proj["machines"][0]["repos"] == ["api"]
    assert proj["board_meta"]["pipeline_default_gates"] == '["review", "test", "merge"]'


def test_dao_is_read_only(file_db: Path):
    conn = SqliteStore(file_db)._connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO board_meta (key, value) VALUES ('x', 'y')")
    conn.close()


# ── Daemon (serve_app) ────────────────────────────────────────────────────────

def test_serve_endpoints(file_db: Path, valid_config_path: Path):
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        assert cli.get("/healthz").json()["status"] == "ok"
        board = cli.get("/board").json()
        assert board["round_number"] == 7
        assert any(a["assignment_id"] == "work1" for a in board["assignments"])
        cfg_resp = cli.get("/config")
        assert cfg_resp.status_code == 200
        assert "repos:" in cfg_resp.text  # raw coordinator.yml


def test_serve_bearer_auth(file_db: Path, valid_config_path: Path):
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg, token="s3cret")
    with TestClient(app) as cli:
        assert cli.get("/board").status_code == 401
        assert cli.get("/healthz").status_code == 200  # health is exempt
        ok = cli.get("/board", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


# ── Schema negotiation (#1943) ─────────────────────────────────────────────────

def test_schema_absent_is_byte_identical_to_explicit_v1(file_db: Path, valid_config_path: Path):
    """No ``X-Coord-Schema`` header, and an explicit ``1``, must produce the
    exact same bytes -- the golden-fixture requirement from #1943's acceptance
    bar, not eyeballing.  Checked against both /board (JSON) and /healthz."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        no_header = cli.get("/board")
        explicit_v1 = cli.get("/board", headers={"X-Coord-Schema": "1"})
        assert no_header.status_code == explicit_v1.status_code == 200
        assert no_header.content == explicit_v1.content

        h_no_header = cli.get("/healthz")
        h_explicit_v1 = cli.get("/healthz", headers={"X-Coord-Schema": "1"})
        assert h_no_header.status_code == h_explicit_v1.status_code == 200
        assert h_no_header.content == h_explicit_v1.content


def test_schema_too_high_is_refused_naming_the_range(file_db: Path, valid_config_path: Path):
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board", headers={"X-Coord-Schema": "99"})
        assert resp.status_code == 400
        body = resp.json()
        # Exact substring, not just "contains a 1" -- that would also pass
        # for a message that named the wrong value or the wrong range.
        assert "unsupported X-Coord-Schema: 99 (supported: 1-1)" in body["error"]
        assert body["schema_min"] == 1
        assert body["schema_max"] == 1


def test_schema_below_range_is_refused(file_db: Path, valid_config_path: Path):
    """A value below ``MIN_SCHEMA_VERSION`` (e.g. ``0``) is refused the same
    way as one above ``SCHEMA_VERSION`` -- both ends of the range check."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board", headers={"X-Coord-Schema": "0"})
        assert resp.status_code == 400
        body = resp.json()
        assert "unsupported X-Coord-Schema: 0 (supported: 1-1)" in body["error"]
        assert body["schema_min"] == 1
        assert body["schema_max"] == 1


def test_schema_too_high_is_refused_on_mutating_route(file_db: Path, valid_config_path: Path):
    """The middleware applies identically regardless of HTTP method -- an
    unsupported schema on a mutating (non-GET) route is refused the same way
    as on a GET, before the route handler (and any DB write) runs."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        resp = cli.post(
            "/pause",
            headers={"X-Coord-Schema": "99"},
            json={"machine": "does-not-matter"},
        )
        assert resp.status_code == 400
        assert "unsupported X-Coord-Schema: 99 (supported: 1-1)" in resp.json()["error"]


def test_schema_non_integer_is_refused(file_db: Path, valid_config_path: Path):
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board", headers={"X-Coord-Schema": "banana"})
        assert resp.status_code == 400
        assert "integer" in resp.json()["error"]


def test_schema_absent_and_explicit_v1_take_the_same_refusal_path(
    file_db: Path, valid_config_path: Path
):
    """Regression guard for the split-brain the absent-header branch used to
    have: absent used to be special-cased straight to ``MIN_SCHEMA_VERSION``
    instead of running through the shared int()+range-check path an explicit
    header takes. Simulate a retired v1 by monkeypatching
    ``MIN_SCHEMA_VERSION`` above ``SCHEMA_VERSION`` and confirm an absent
    header is refused exactly like an explicit ``X-Coord-Schema: 1`` would
    be -- not silently accepted because it skipped the check.
    """
    import coord.serve_app as serve_app_module

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    original_min = serve_app_module.MIN_SCHEMA_VERSION
    try:
        serve_app_module.MIN_SCHEMA_VERSION = 2  # simulate v1 retired
        with TestClient(app) as cli:
            no_header = cli.get("/board")
            explicit_v1 = cli.get("/board", headers={"X-Coord-Schema": "1"})
        assert no_header.status_code == explicit_v1.status_code == 400
        assert no_header.json() == explicit_v1.json()
    finally:
        serve_app_module.MIN_SCHEMA_VERSION = original_min


def test_healthz_advertises_schema_range(file_db: Path, valid_config_path: Path):
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        body = cli.get("/healthz").json()
        assert body["schema_min"] == 1
        assert body["schema_max"] == 1
        assert body["schema_version"] == 1  # back-compat: == schema_max


def test_schema_negotiation_runs_before_bearer_auth(file_db: Path, valid_config_path: Path):
    """An unsupported schema is refused even without valid auth -- the 4xx
    doesn't depend on the request clearing the bearer-auth layer first."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg, token="s3cret")
    with TestClient(app) as cli:
        resp = cli.get("/board", headers={"X-Coord-Schema": "99"})
        assert resp.status_code == 400


# ── Pause (#1563) ─────────────────────────────────────────────────────────────
#
# `coord pause` on a thin client used to write only to the operator's own
# `~/.coord/paused_machines.json` — a file the daemon (which runs the
# autonomous dispatch tick) never read, so the fleet kept dispatching to
# machines the operator believed were paused. `/pause` below is the fix: it
# always operates on the daemon's own local-only store
# (`coord.machine_pause.local_pause`/`local_paused_set`), so a thin client
# routed through it and the daemon's own tick loop agree on one copy.


def _pause_view(cli) -> dict:
    """`GET /pause`, narrowed to the pause axes these tests are about.

    #2101 added `cordoned`/`cordons` to the same payload (a release cordon is
    the same routing decision with a different owner). They are asserted on in
    `tests/test_release_cordon_2101.py`; here they would only make every
    assertion about pause restate something it does not care about.
    """
    body = cli.get("/pause").json()
    assert body.get("cordons") == [] and body.get("cordoned") == [], (
        "no test in this module cordons anything — see #2101"
    )
    return {k: v for k, v in body.items() if k in ("paused", "quiet")}


def test_pause_endpoints_roundtrip(
    file_db: Path, valid_config_path: Path, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "daemon_home"))
    (tmp_path / "daemon_home" / ".coord").mkdir(parents=True)
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        assert _pause_view(cli) == {"paused": [], "quiet": []}

        resp = cli.post("/pause", json={"machine": "laptop", "action": "pause"})
        assert resp.status_code == 200
        # "laptop" has no `quiet_hours` in this fixture config, so the hand
        # pause never shows up in "quiet" — #1862: that's what lets a thin
        # client (the TUI included) tell a hand pause apart from a
        # quiet-hours one from this same endpoint.
        assert resp.json() == {"paused": ["laptop"], "quiet": [], "changed": True}
        assert _pause_view(cli) == {"paused": ["laptop"], "quiet": []}

        # Pausing an already-paused machine is idempotent: changed=False.
        resp = cli.post("/pause", json={"machine": "laptop", "action": "pause"})
        assert resp.json() == {"paused": ["laptop"], "quiet": [], "changed": False}

        resp = cli.post("/pause", json={"machine": "laptop", "action": "unpause"})
        assert resp.status_code == 200
        # #1862: unpause also reports *why* it changed something — "resumed"
        # (an explicit pause was lifted) vs "quiet_override" — so a thin
        # client can never mistake one for the other. "laptop" has no
        # `quiet_hours` in this fixture config, so it's a plain resume.
        assert resp.json() == {
            "paused": [], "quiet": [], "changed": True, "kind": "resumed",
            "quiet_until": None, "tz": None,
        }


def test_pause_endpoint_validates_body(
    file_db: Path, valid_config_path: Path, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "daemon_home"))
    (tmp_path / "daemon_home" / ".coord").mkdir(parents=True)
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        assert cli.post("/pause", json={"action": "pause"}).status_code == 400
        assert (
            cli.post("/pause", json={"machine": "laptop", "action": "bogus"}).status_code
            == 400
        )
        assert cli.post("/pause", content=b"not json").status_code == 400


def test_pause_endpoints_fold_in_quiet_hours(
    file_db: Path, monkeypatch, tmp_path: Path
) -> None:
    """#1862: `/pause` is the daemon's own view of who's unavailable for new
    dispatch — a quiet-hours-covered machine must show up in it exactly like
    a hand-paused one (this is the "TUI gets it for free" claim: the TUI
    only ever checks set membership over this endpoint), and `coord unpause`
    against it must grant a real override rather than silently no-op.

    Uses a wide (~1h) window centered on the real clock rather than a fixed
    instant so the test isn't racy against wall-clock time.
    """
    from datetime import datetime, timedelta, timezone

    from coord.config import Config
    from coord.models import Machine, QuietHours, Repo

    monkeypatch.setenv("HOME", str(tmp_path / "daemon_home"))
    (tmp_path / "daemon_home" / ".coord").mkdir(parents=True)

    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=30)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(minutes=30)).time().replace(second=0, microsecond=0)
    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(
                name="elitebook", host="elitebook.tail", repos=["api"],
                quiet_hours=QuietHours(start=start, end=end, tz="UTC"),
            ),
            Machine(name="server", host="server.tail", repos=["api"]),
        ],
    )
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        # Quiet-hours-covered machine shows up in /pause with no explicit
        # `coord pause` ever having been called — and, #1862 review finding,
        # it's also named in "quiet" so a thin client (the TUI included)
        # can tell it apart from a hand pause without a second lookup.
        assert _pause_view(cli) == {"paused": ["elitebook"], "quiet": ["elitebook"]}

        # `coord unpause elitebook` grants an override — not a silent no-op,
        # not a lie ("changed" is True and it SAYS what it did, #1563).
        resp = cli.post("/pause", json={"machine": "elitebook", "action": "unpause"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["changed"] is True
        assert body["kind"] == "quiet_override"
        assert body["tz"] == "UTC"
        assert body["paused"] == []
        assert body["quiet"] == []

        # The override sticks on the very next read (the #1563 failure class
        # this closes: `coord unpause` reporting success and then having the
        # machine paused again on the next poll) — and it's no longer
        # reported as quiet-paused either.
        assert _pause_view(cli) == {"paused": [], "quiet": []}

        # A machine with no `quiet_hours:` block is never touched.
        assert "server" not in cli.get("/pause").json()["paused"]
        assert "server" not in cli.get("/pause").json()["quiet"]


def test_pause_on_thin_client_reaches_daemon_and_blocks_dispatch(
    file_db: Path, valid_config_path: Path, monkeypatch, tmp_path: Path
) -> None:
    """Black-box repro + fix verification for #1563.

    Sequence: (1) a "reconcile tick" finds the only capable machine a valid
    reviewer candidate; (2) a "thin client" pauses that machine using the
    REAL `coord.machine_pause.pause()` call site — with a board service
    configured and `coord.client`'s HTTP calls routed to this exact daemon
    instance (not a second, disconnected local file — the literal #1563
    bug); (3) the same daemon process, now acting as its own tick loop (no
    board_service — the real daemon never points at itself), must see the
    pause and refuse to pick that machine, i.e. no review assignment would
    be created.
    """
    from coord.config import Config, ReviewsConfig
    from coord.machine_pause import pause, paused_set
    from coord.models import Board, Machine, Repo
    from coord.review import pick_reviewer_machine

    monkeypatch.setenv("HOME", str(tmp_path / "daemon_home"))
    (tmp_path / "daemon_home" / ".coord").mkdir(parents=True)

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)

    review_cfg = Config(
        repos=[Repo(name="api", github="acme/api", depends_on=[], default_branch="main")],
        machines=[
            Machine(
                name="solo", host="solo.tail", capabilities=["python"],
                repos=["api"], repo_paths={"api": "/work/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )
    board = Board()  # a pending review needs a reviewer for repo "api"

    with TestClient(app) as cli:
        # Before any pause: "solo" is the (only) reviewer candidate.
        choice = pick_reviewer_machine("someone-else", "api", board, review_cfg)
        assert choice is not None and choice.machine.name == "solo"

        # Simulate the operator's thin client: point coord.client's HTTP
        # calls at THIS daemon instance and configure a board service, then
        # call the real pause() — exactly what `coord pause solo` runs.
        monkeypatch.setattr(coord_client.httpx, "get", cli.get)
        monkeypatch.setattr(coord_client.httpx, "post", cli.post)
        monkeypatch.setattr(
            coord_client, "resolve_board_service",
            lambda *a, **k: coord_client.ServiceConfig(url="http://testserver"),
        )

        changed = pause("solo")
        assert changed is True
        # The thin client's own read confirms the daemon accepted it.
        assert paused_set() == {"solo"}

        # Now become "the daemon's own tick loop": no board_service (the
        # real daemon never configures one for itself), so paused_set()
        # reads the local file directly — the SAME file /pause just wrote.
        monkeypatch.setattr(coord_client, "resolve_board_service", lambda *a, **k: None)
        assert paused_set() == {"solo"}

        # The dispatch decision the reconcile tick makes: no candidate left,
        # so no review assignment would be created.
        assert pick_reviewer_machine("someone-else", "api", board, review_cfg) is None


def test_github_backoff_recorded_on_one_host_is_honoured_by_another(
    file_db: Path, valid_config_path: Path, monkeypatch
) -> None:
    """#2934 acceptance: a 403 recorded by one machine is honoured by every
    other machine's next `gh` call, with a real daemon in the loop rather
    than a unit test alone -- the same black-box shape as
    `test_pause_on_thin_client_reaches_daemon_and_blocks_dispatch` above,
    for the sibling daemon-aware seam #2934 adds to `coord.github_throttle`.

    Sequence: (1) "host A" observes a secondary rate limit and calls the
    REAL `coord.github_throttle.record()` with a board service configured
    and `coord.client`'s HTTP calls routed to this exact daemon instance;
    (2) "host B" — no state of its own, just the same board-service routing
    — calls the REAL `consult()` and must see the SAME backoff, proving the
    signal crossed the daemon rather than living in a per-host file; (3) the
    daemon's own in-process view (no board_service — the real daemon never
    points at itself, `coord.machine_pause`'s documented contract) sees it
    too via `current()`, with no extra HTTP hop, because it is literally the
    same file `get_github_backoff` just served from.
    """
    from coord import github_throttle

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)

    with TestClient(app) as cli:
        # Before any hit: nothing to back off for.
        sleep_s, backoff = github_throttle.consult()
        assert (sleep_s, backoff) == (0.0, None)

        # Pin the raw HTTP contract directly, independent of the client-side
        # wrapper functions: `record()`/`consult()` both silently fall back
        # to the local file on ANY exception, including a bug in the daemon
        # handler itself (e.g. an ImportError inside the route), so a test
        # that only ever calls the wrappers cannot tell a working daemon
        # round trip from a broken one that happened to fall back onto the
        # very file this single-process test would read anyway.
        get_before = cli.get("/github-backoff")
        assert get_before.status_code == 200
        assert get_before.json() == {"backoff": None}
        post_resp = cli.post(
            "/github-backoff",
            json={
                "reason": "secondary_rate_limit", "status": 403,
                "request_id": "AE80:1EF17E", "retry_after_s": 90.0,
            },
        )
        assert post_resp.status_code == 200
        posted_backoff = post_resp.json()["backoff"]
        assert posted_backoff is not None
        assert posted_backoff["request_id"] == "AE80:1EF17E"
        get_after = cli.get("/github-backoff")
        assert get_after.status_code == 200
        assert get_after.json()["backoff"]["request_id"] == "AE80:1EF17E"

        # Now the same sequence through the REAL client-side wrappers.
        github_throttle.clear()

        # "host A": a thin client pointed at THIS daemon instance.
        monkeypatch.setattr(coord_client.httpx, "get", cli.get)
        monkeypatch.setattr(coord_client.httpx, "post", cli.post)
        monkeypatch.setattr(
            coord_client, "resolve_board_service",
            lambda *a, **k: coord_client.ServiceConfig(url="http://testserver"),
        )
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id="AE80:1EF17E", retry_after_s=90.0,
        )

        # "host B": same routing, no state of its own -- sees host A's hit.
        sleep_s, backoff = github_throttle.consult()
        assert backoff is not None
        assert backoff.reason == "secondary_rate_limit"
        assert backoff.request_id == "AE80:1EF17E"
        assert sleep_s > 0.0

        # The daemon's own tick loop: no board_service, reads its local file
        # directly -- the SAME file /github-backoff just wrote via host A's
        # POST and served back for host B's GET.
        monkeypatch.setattr(coord_client, "resolve_board_service", lambda *a, **k: None)
        local = github_throttle.current()
        assert local is not None
        assert local.request_id == "AE80:1EF17E"


def test_github_backoff_falls_back_to_local_file_when_daemon_unreachable(
    monkeypatch,
) -> None:
    """#2934 acceptance: a daemon that can't be reached must never degrade
    to "no damping" (nor raise) -- `record()`/`consult()` fall back to the
    per-host file exactly as they did before #2934."""
    import httpx

    from coord import github_throttle

    monkeypatch.setattr(
        coord_client, "resolve_board_service",
        lambda *a, **k: coord_client.ServiceConfig(url="http://unreachable-daemon"),
    )

    def _raise(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(coord_client.httpx, "get", _raise)
    monkeypatch.setattr(coord_client.httpx, "post", _raise)

    github_throttle.record(
        reason="secondary_rate_limit", status=403,
        request_id=None, retry_after_s=45.0, now=1000.0,
    )
    sleep_s, backoff = github_throttle.consult(now=1002.0)
    assert backoff is not None
    assert backoff.until == pytest.approx(1045.0)
    assert sleep_s > 0.0


def test_serve_merge_passes_show_plan_to_callback(file_db: Path, valid_config_path: Path):
    """#684 regression: ``post_merge`` must pass ``show_plan`` to the merge
    callback.  #684 added ``--plan``/``show_plan`` to ``coord merge`` (routing
    ``--plan`` via /board, never /merge) but left the daemon handler invoking
    ``merge_cmd.callback(...)`` without it — so every daemon-routed merge (thin
    client, TUI 'Go', headless drain) crashed with ``merge() missing 1 required
    positional argument: 'show_plan'`` before doing anything.

    A nonexistent ``repo_filter`` keeps the dry-run a hermetic no-op (empty
    queue → no gh/network), so the test asserts only that the signature bug
    does not recur.
    """
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        resp = cli.post("/merge", json={"dry_run": True, "repo_filter": "no-such-repo"})
        assert resp.status_code == 200
        err = resp.json().get("error") or ""
        assert "show_plan" not in err, f"merge handler regressed on show_plan: {err}"
        assert "missing 1 required positional argument" not in err


def test_serve_merge_relays_stderr_usage_errors(
    file_db: Path, valid_config_path: Path, rw_db
):
    """#1251-review blocking finding: ``post_merge``'s ``_run()`` only wrapped
    the callback in ``contextlib.redirect_stdout`` — any ``click.echo(...,
    err=True)`` usage error (e.g. ``--only`` on a non-PENDING entry) resolved
    ``sys.stderr`` fresh and wrote straight into the daemon process's own
    journal, never into the captured buffer. A daemon-routed thin client (the
    dominant deployment mode) then saw exit_code=1 with a totally empty
    output/error — indistinguishable from a crash, and exactly the #1251
    repro. Assert the message now survives the relay.

    #2157 narrowed the not-PENDING guard: MERGED left it (an already-landed
    merge is the caller's postcondition, not a usage error), so the seeded
    state here is HUMAN_REQUIRED — still a genuine exit-1 usage error, which
    is what this test is actually about.

    Uses ``rw_db`` (thread-safe, file-backed) rather than the default autouse
    ``coord_db`` (thread-bound ``:memory:``) because ``post_merge`` runs the
    callback in a worker thread via ``run_in_threadpool``, mirroring
    production's ``check_same_thread=False`` connection.
    """
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    mq.save_queue([
        mq.QueuedMerge(
            assignment_id="m1",
            repo_name="api",
            repo_github="acme/api",
            branch="worker/m1",
            target_branch="main",
            issue_number=1,
            issue_title="t",
            size=None,
            state=mq.HUMAN_REQUIRED,
        )
    ])
    with TestClient(app) as cli:
        resp = cli.post("/merge", json={"only": "m1"})
        body = resp.json()
        assert body["exit_code"] != 0
        assert "not PENDING" in body["output"], (
            f"stderr usage error did not reach the client: {body!r}"
        )


def test_serve_merge_reports_an_already_merged_entry_as_success(
    file_db: Path, valid_config_path: Path, rw_db
):
    """#2157, on the daemon-routed path: a thin client asking the daemon to
    merge an entry that has already merged must get exit_code 0 back, not the
    exit 1 that `coord drive` counted as a failed merge attempt."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    mq.save_queue([
        mq.QueuedMerge(
            assignment_id="m1",
            repo_name="api",
            repo_github="acme/api",
            branch="worker/m1",
            target_branch="main",
            issue_number=1,
            issue_title="t",
            size=None,
            state=mq.MERGED,
            pr_number=60,
        )
    ])
    with TestClient(app) as cli:
        body = cli.post("/merge", json={"only": "m1"}).json()
        assert body["exit_code"] == 0, body
        assert "already merged" in body["output"], body
        assert "PR #60" in body["output"], body


def test_serve_merge_concurrent_requests_do_not_cross_talk(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch: pytest.MonkeyPatch
):
    """#1278/#1400: two concurrent ``POST /merge`` calls must never leak each
    other's captured CLI output, and — since #1400 — must not even run their
    click callbacks concurrently in the first place.

    History: ``post_merge``'s ``_run()`` used to capture output with
    ``contextlib.redirect_stdout(buf)``, which rebinds the process-*global*
    ``sys.stdout``. The callback runs inside a ``run_in_threadpool`` worker
    THREAD though, so two concurrent requests raced on that global: whichever
    swap landed last "won" for both, and each caller's response could end up
    containing (or missing) the OTHER request's output. #1278 fixed the
    *capture* itself (thread-local, see ``_ThreadLocalCapture``) but left the
    underlying merge-queue processing running genuinely in parallel across
    threads — which has its own unprotected process-global state (the
    ``COORD_MERGE_ON_DAEMON`` env-var toggle) and its own read-modify-write
    race (``merge_queue.load_queue()`` -> mutate -> ``save_queue()`` replaces
    the WHOLE table, so a losing writer silently reverts a winning writer's
    just-recorded MERGED state). #1400 closes that by wrapping the whole
    critical section in ``_merge_lock``, a process-wide ``threading.Lock``,
    so a second ``/merge`` request now blocks until the first fully finishes
    rather than running concurrently at all.

    This test drives a real merge (``dry_run=False``) and a dry run
    (``dry_run=True``) concurrently, choreographed with ``threading.Event``
    so the ordering is deterministic rather than left to GIL-scheduling luck:

      1. the real-merge callback signals ``a_started`` as soon as it starts
         (the lock is held and its capture is active at this point).
      2. the test driver waits for that signal, then fires the dry-run
         request and gives it a beat to reach the daemon — if ``_merge_lock``
         were not actually serializing, the dry run's callback would start
         running (and set ``dry_started``) right away, before the real merge
         releases the lock. Assert that does NOT happen.
      3. the test driver then lets the real merge finish (sets
         ``real_may_finish``); only once its callback returns -- and
         ``_merge_lock`` is released -- can the dry run's callback start.
      4. the dry-run callback itself asserts ``real_finished`` is already set
         as a second, in-process check of the same invariant.
    """
    import asyncio as _asyncio
    import threading

    import click
    import httpx

    from coord.cli import merge as merge_cmd

    a_started = threading.Event()
    dry_started = threading.Event()
    real_may_finish = threading.Event()
    real_finished = threading.Event()

    def fake_callback(**kwargs):
        if not kwargs["dry_run"]:
            # Real merge: signal we've started (lock held, capture active),
            # then hold the lock open until the test driver has confirmed the
            # concurrently-fired dry run did NOT start meanwhile.
            a_started.set()
            assert real_may_finish.wait(timeout=5), "test driver never released the real merge"
            click.echo("opened PR #1274")
            click.echo("merged PR #1274")
            real_finished.set()
        else:
            # Dry run: under the #1400 fix this can only run after the real
            # merge's callback has returned and released _merge_lock.
            dry_started.set()
            assert real_finished.is_set(), (
                "dry-run callback started before the concurrent real merge "
                "finished -- /merge is not actually serialized (#1400 regression)"
            )
            click.echo("(dry run) would merge nothing")

    monkeypatch.setattr(merge_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            real_task = _asyncio.create_task(cli.post("/merge", json={"dry_run": False}))
            # Wait for the real-merge callback to actually start (lock held)
            # before firing the dry run.
            while not a_started.is_set():
                await _asyncio.sleep(0.005)
            dry_task = _asyncio.create_task(cli.post("/merge", json={"dry_run": True}))
            # Give the dry run a beat to reach the daemon and attempt the
            # lock -- under the #1400 fix it must block, never run its
            # callback, while the real merge is still holding _merge_lock.
            await _asyncio.sleep(0.2)
            assert not dry_started.is_set(), (
                "dry run's callback started while the real merge still held "
                "_merge_lock -- /merge is not actually serialized (#1400 regression)"
            )
            real_may_finish.set()
            return await _asyncio.gather(real_task, dry_task)

    real_resp, dry_resp = _asyncio.run(_run())
    real_body = real_resp.json()
    dry_body = dry_resp.json()

    assert "(dry run)" in dry_body["output"], dry_body
    assert "merged PR #" not in dry_body["output"], (
        f"dry-run response leaked the concurrent real merge's output: {dry_body!r}"
    )
    assert "merged PR #1274" in real_body["output"], (
        f"real-merge response lost its own output to the concurrent dry run: {real_body!r}"
    )


def test_serve_merge_drop_serializes_on_merge_lock(
    file_db: Path, valid_config_path: Path, rw_db
):
    """#1400-review: the ``--drop`` shortcut in ``post_merge`` must take the
    same ``_merge_lock`` a concurrent real merge holds.

    ``drop_entry()`` issues a direct SQL ``DELETE`` on the row, but a
    concurrent real merge's ``load_queue()`` -> mutate -> ``save_queue()``
    cycle replaces the WHOLE table (see ``merge_queue.save_queue``). If that
    merge already snapshotted the queue (via ``load_queue()``) before this
    drop's ``DELETE`` lands, its own ``save_queue()`` writes the stale
    snapshot back over the table and silently resurrects the just-dropped
    row. Taking ``_merge_lock`` around the drop closes the same gap #1400
    closed for two concurrent ``/merge`` requests.

    Holds ``_merge_lock`` in the main thread and fires the drop request on a
    background thread; asserts the request blocks (the row is still present)
    until the lock is released, then completes and actually removes it.
    """
    import threading

    from coord.serve_app import _merge_lock

    mq.save_queue([
        mq.QueuedMerge(
            assignment_id="m1",
            repo_name="api",
            repo_github="acme/api",
            branch="worker/m1",
            target_branch="main",
            issue_number=1,
            issue_title="t",
            size=None,
            state=mq.PENDING,
        )
    ])
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))

    result: dict = {}
    # Kept open for the whole test: the background thread's request must
    # finish (lock released, response received) before the TestClient's
    # transport is torn down at the end of the ``with`` block.
    with TestClient(app) as cli:
        def _post():
            result["resp"] = cli.post("/merge", json={"drop": "m1"})

        _merge_lock.acquire()
        try:
            t = threading.Thread(target=_post)
            t.start()
            t.join(timeout=0.3)
            assert t.is_alive(), (
                "POST /merge {'drop': ...} returned while _merge_lock was "
                "still held externally -- the drop shortcut is not "
                "serialized against a concurrent merge (#1400 regression)"
            )
            # The lock is still held, so drop_entry() must not have run yet.
            assert any(item.assignment_id == "m1" for item in mq.load_queue()), (
                "the row was removed before _merge_lock was released -- "
                "drop_entry() ran outside the lock (#1400 regression)"
            )
        finally:
            _merge_lock.release()
        t.join(timeout=5)
        assert not t.is_alive(), "drop request did not finish after _merge_lock was released"

    body = result["resp"].json()
    assert body["exit_code"] == 0
    assert "dropped entry m1" in body["output"]
    assert not any(item.assignment_id == "m1" for item in mq.load_queue())


# ── #1081: daemon-side config reload-on-write ───────────────────────────────

def _bump_mtime(path: Path, seconds_ahead: float = 5.0) -> None:
    """Force the on-disk mtime forward so a same-second rewrite is still detected.

    Some filesystems have 1s mtime resolution, so a write immediately followed
    by another write in the same test can produce an identical mtime — which
    would make ``_reload_config_if_stale`` (correctly) treat it as unchanged.
    Tests that rewrite the file mid-test call this to make the "on-disk
    change" unambiguous, mirroring a real hand-edit that happens well after
    the daemon's initial load.
    """
    new_time = path.stat().st_mtime + seconds_ahead
    os.utime(path, (new_time, new_time))


def _disable_reviews(path: Path) -> None:
    """Append a ``reviews: enabled: false`` override onto *path*'s current YAML.

    Reads the fixture's existing content rather than depending on the
    ``VALID_CONFIG`` constant directly (that lives in ``conftest.py`` and
    isn't imported here), so this stays correct if the fixture body changes.
    """
    path.write_text(path.read_text() + "\nreviews:\n  enabled: false\n")


def test_reload_config_if_stale_picks_up_on_disk_change(valid_config_path: Path):
    cfg = load_config(valid_config_path)
    mtime = valid_config_path.stat().st_mtime

    _disable_reviews(valid_config_path)
    _bump_mtime(valid_config_path)

    reloaded, new_mtime = _reload_config_if_stale(cfg, mtime)
    assert reloaded is not cfg
    assert reloaded.reviews.enabled is False
    assert new_mtime > mtime


def test_reload_config_if_stale_noop_when_unchanged(valid_config_path: Path):
    cfg = load_config(valid_config_path)
    mtime = valid_config_path.stat().st_mtime

    same, same_mtime = _reload_config_if_stale(cfg, mtime)
    assert same is cfg  # no stat()-detected change → no reparse, same object
    assert same_mtime == mtime


def test_reload_config_if_stale_noop_when_no_path(valid_config_path: Path):
    cfg = dataclasses.replace(load_config(valid_config_path), path=None)
    same, same_mtime = _reload_config_if_stale(cfg, None)
    assert same is cfg
    assert same_mtime is None


def test_reload_config_if_stale_keeps_last_good_on_invalid_yaml(
    valid_config_path: Path, caplog: pytest.LogCaptureFixture
):
    cfg = load_config(valid_config_path)
    mtime = valid_config_path.stat().st_mtime

    valid_config_path.write_text("not: [valid, yaml, :::")
    _bump_mtime(valid_config_path)

    with caplog.at_level("WARNING", logger="coord.serve"):
        kept, new_mtime = _reload_config_if_stale(cfg, mtime)
    assert kept is cfg  # last-good config preserved, not raised into the caller
    assert new_mtime > mtime  # advances so a bad edit isn't re-parsed every call
    assert "failed to reload" in caplog.text


def test_reload_config_if_stale_keeps_last_good_on_non_config_error(
    valid_config_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A reload failure that ISN'T a ``ConfigError`` must still be swallowed (#1081 review).

    ``coord.config.load`` isn't guaranteed to only raise ``ConfigError`` — a
    TOCTOU race (file deleted/replaced between our ``stat()`` and ``load()``'s
    own read), a permissions change, or a bad-encoding write caught mid-edit
    can all surface a raw ``OSError``/``UnicodeDecodeError``/etc. This must
    never propagate into the ``/board`` handler or (worse) permanently kill
    the bare ``asyncio.create_task(_tick_loop())`` task — it has no supervisor
    to restart it.
    """
    cfg = load_config(valid_config_path)
    mtime = valid_config_path.stat().st_mtime
    _bump_mtime(valid_config_path)

    import coord.config as coord_config_module

    def _boom(_path):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(coord_config_module, "load", _boom)

    with caplog.at_level("WARNING", logger="coord.serve"):
        kept, new_mtime = _reload_config_if_stale(cfg, mtime)
    assert kept is cfg  # last-good config preserved, not raised into the caller
    assert new_mtime > mtime  # advances so a bad edit isn't re-parsed every call
    assert "failed to reload" in caplog.text
    assert "OSError" in caplog.text


def test_serve_board_picks_up_config_hand_edit(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Integration check: a hand-edit to coordinator.yml (no daemon restart) must
    reach the daemon's own internal decisions — not just GET /config's raw
    bytes — the next time ``/board`` runs its merge-plan computation (#1081).

    Invokes the ``/board`` route's endpoint function directly (via
    ``asyncio.run`` in this test's own thread) rather than through
    ``TestClient``.

    With the #<issue> threadpool change, ``board()`` now offloads its body
    (including ``_build_board()``) to a worker thread via ``run_in_threadpool``.
    The autouse ``coord_db`` fixture installs a thread-bound ``:memory:``
    connection, so ``_build_board()`` would fail in the threadpool and the
    fail-open ``except`` would silently skip ``merge_queue.plan`` — same
    cross-thread restriction documented in the original test, now in both
    paths.  Patch ``coord.state.build_board`` to return an empty Board so the
    threadpool doesn't touch the thread-bound connection; the key invariant
    under test (_refresh_config → config swap → plan sees new config) is
    unaffected by this stub.
    """
    from coord.models import Board as _Board

    cfg = load_config(valid_config_path)
    assert cfg.reviews.enabled is True  # sanity: default is on
    app = build_app(SqliteStore(file_db), cfg)
    board_route = next(r for r in app.routes if getattr(r, "path", None) == "/board")

    seen_configs = []

    def _spy_plan(board, config, ci_store=None, gh_ops=None):  # noqa: ANN001, ARG001
        seen_configs.append(config)
        return []

    monkeypatch.setattr("coord.merge_queue.plan", _spy_plan)
    # Stub out build_board() so the threadpool doesn't hit the thread-bound
    # :memory: connection installed by the coord_db fixture.
    monkeypatch.setattr("coord.state.build_board", lambda: _Board())

    class _Req:
        # #1336: board() reads If-None-Match for conditional GETs — a bare
        # headers dict is all this direct-endpoint invocation needs.
        headers: dict = {}

    asyncio.run(board_route.endpoint(_Req()))
    assert seen_configs, "merge_queue.plan was never called — board() didn't reach it"
    assert seen_configs[-1].reviews.enabled is True

    _disable_reviews(valid_config_path)
    _bump_mtime(valid_config_path)

    asyncio.run(board_route.endpoint(_Req()))
    assert seen_configs[-1].reviews.enabled is False, (
        "daemon's internal config did not pick up the on-disk hand-edit"
    )


# ── Client ────────────────────────────────────────────────────────────────────

def test_board_from_payload_matches_local_build(file_db: Path):
    payload = SqliteStore(file_db).board_projection()
    board = coord_client.board_from_payload(payload)
    assert board.round_number == 7
    work = board.find_by_id("work1")
    assert work is not None
    assert work.status == "done" and work.type == "work"
    assert work.files_allowed == ["a.py", "b.py"]
    assert work.briefing == ""  # dropped on the wire → mapper default
    # review_state inferred from the linked review assignment.
    assert work.review_state == "done"


def test_resolve_board_service_precedence(tmp_path: Path, monkeypatch):
    toml = tmp_path / "client.toml"
    toml.write_text('board_service = "http://fromfile:7435"\ntoken = "ft"\n')
    monkeypatch.setattr(coord_client, "CLIENT_TOML", toml)

    # file only
    monkeypatch.delenv("COORD_SERVICE_URL", raising=False)
    monkeypatch.delenv("COORD_TOKEN", raising=False)
    svc = coord_client.resolve_board_service()
    assert svc is not None and svc.url == "http://fromfile:7435" and svc.token == "ft"

    # env beats file
    monkeypatch.setenv("COORD_SERVICE_URL", "http://fromenv:7435/")
    svc = coord_client.resolve_board_service()
    assert svc.url == "http://fromenv:7435"  # trailing slash stripped

    # flag beats env
    svc = coord_client.resolve_board_service(flag_url="http://fromflag:7435")
    assert svc.url == "http://fromflag:7435"


def test_resolve_board_service_unset_returns_none(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(coord_client, "CLIENT_TOML", tmp_path / "nope.toml")
    monkeypatch.delenv("COORD_SERVICE_URL", raising=False)
    assert coord_client.resolve_board_service() is None


# ── #795 Phase 3b: milestone_work_orders in /board payload ───────────────────

_WORK_ORDER_BODY = """\
Tracking issue for the milestone.

## Work order
- [ ] #101  {group: A}
- [ ] #102  {group: A}
- [ ] #103  {after: #101,#102}

## Notes
Not part of the work order.
"""


def _make_work_order_db(path: Path) -> None:
    """Seed a DB with:
    - a tracking issue (label="epic") carrying a ## Work order block
    - two open milestone issues (#101, #102) and one open (#103) blocked on them
    - a machine so build_board() doesn't crash
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute("INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
                 ("laptop", "laptop.tailnet", '["python"]', '["api"]'))
    # Tracking issue: epic label, no milestone of its own (doesn't need one)
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, 0)",
        ("api", 500, "Milestone tracking", _WORK_ORDER_BODY, '["epic", "coord"]'),
    )
    # Open work issues referenced in the work order
    for num, title in [(101, "Issue A1"), (102, "Issue A2"), (103, "Issue B1")]:
        conn.execute(
            "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
            "VALUES (?, ?, ?, '', 'open', '[]', 0)",
            ("api", num, title),
        )
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.commit()
    conn.close()


@pytest.fixture
def work_order_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_work_order_db(p)
    return p


def test_milestone_work_orders_in_board_payload(work_order_db: Path, valid_config_path: Path):
    """#795: /board payload carries milestone_work_orders for each tracking issue.

    Verifies rank, ready/blocked, next_up, and blocked_on for a seeded
    ## Work order block.  #101 and #102 are ready (no deps); #103 is blocked
    on both.
    """
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(work_order_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert "milestone_work_orders" in board, "milestone_work_orders key missing from /board"
    mwos = board["milestone_work_orders"]
    assert len(mwos) == 1, f"expected 1 milestone work order, got {len(mwos)}: {mwos}"

    mwo = mwos[0]
    assert mwo["repo_name"] == "api"
    assert mwo["tracking_issue"] == 500

    nodes_by_num = {n["issue_number"]: n for n in mwo["nodes"]}
    assert set(nodes_by_num) == {101, 102, 103}, f"unexpected node set: {set(nodes_by_num)}"

    # #101 and #102 are at ranks 0 and 1 (group A, no deps) → ready + next_up
    n101 = nodes_by_num[101]
    assert n101["rank"] == 0
    assert n101["ready"] is True
    assert n101["next_up"] is True
    assert n101["blocked_on"] == []

    n102 = nodes_by_num[102]
    assert n102["rank"] == 1
    assert n102["ready"] is True
    assert n102["next_up"] is True
    assert n102["blocked_on"] == []

    # #103 is at rank 2, blocked on #101 and #102 (both still open)
    n103 = nodes_by_num[103]
    assert n103["rank"] == 2
    assert n103["ready"] is False
    assert n103["next_up"] is False
    assert set(n103["blocked_on"]) == {101, 102}, f"unexpected blocked_on: {n103['blocked_on']}"


def test_milestone_work_orders_terminal_issue_excluded(work_order_db: Path, valid_config_path: Path):
    """#795: a work-order node whose issue is closed/absent is excluded from nodes.

    Close #101 and #102 in the DB — they become terminal, so #103's blocked_on
    is empty and it becomes ready/next_up.  Closed nodes are dropped from the
    payload (they're done, not a frontier item).
    """
    # Re-open the DB and mark #101, #102 closed.
    conn = sqlite3.connect(str(work_order_db))
    conn.execute("UPDATE issues SET state='closed' WHERE repo_name='api' AND number IN (101, 102)")
    conn.commit()
    conn.close()

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(work_order_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    mwos = board["milestone_work_orders"]
    assert len(mwos) == 1
    nodes_by_num = {n["issue_number"]: n for n in mwos[0]["nodes"]}

    # #101 and #102 are closed → terminal → excluded from the payload
    assert 101 not in nodes_by_num, "#101 is closed/terminal — must not appear in nodes"
    assert 102 not in nodes_by_num, "#102 is closed/terminal — must not appear in nodes"

    # #103's deps are all terminal → it's now ready
    assert 103 in nodes_by_num, "#103 must appear as a node"
    n103 = nodes_by_num[103]
    assert n103["ready"] is True
    assert n103["next_up"] is True
    assert n103["blocked_on"] == []


def test_milestone_work_orders_claimed_node_ready_but_not_next_up(
    tmp_path: Path, valid_config_path: Path,
):
    """#795 review: a node whose deps are all terminal but which is actively
    CLAIMED (an in-flight assignment elsewhere) must report `ready=True` /
    `next_up=False` with an EMPTY `blocked_on` — not fall through to the
    "waiting on deps" branch, which previously produced a dangling
    `blocked_on` with nothing left in it (`ready_frontier` excludes claimed
    nodes from `ready` for a claim reason, not an unmet-dep reason, and the
    old code recomputed `blocked_on` purely from `node.after`).

    `build_board()` (used by the `/board` handler for claim detection via
    `find_work_claim`) reads through `coord.state.get_connection()` — the
    thread-bound `:memory:` conn the autouse `coord_db` fixture installs,
    which TestClient's worker thread can't touch (see the `rw_db` fixture's
    docstring above). So the claim has to be seeded into a file-backed,
    `check_same_thread=False` override of that same global connection —
    the on-disk `work_order_db` fixture alone (which only backs
    `SqliteStore.board_projection()`) wouldn't be visible to `build_board()`.
    """
    db_path = tmp_path / "coord.db"
    _make_work_order_db(db_path)

    from coord import db as _db

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("work102", "laptop", "api", "acme/api", 102, "Issue A2", "running", "work"),
    )
    conn.commit()
    _db.override_connection(conn)

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(db_path), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    mwos = board["milestone_work_orders"]
    assert len(mwos) == 1
    nodes_by_num = {n["issue_number"]: n for n in mwos[0]["nodes"]}

    # #102 has no unmet deps but IS claimed → ready (deps satisfied) yet not
    # next_up (already spoken for); blocked_on must be empty, not a dangling
    # reference to a phantom dependency.
    n102 = nodes_by_num[102]
    assert n102["ready"] is True, "claimed node with met deps should be ready"
    assert n102["next_up"] is False, "claimed node must not be next_up"
    assert n102["blocked_on"] == [], "claimed node has no unmet deps to report"

    # #101 is unaffected — still ready + next_up (no claim on it).
    n101 = nodes_by_num[101]
    assert n101["ready"] is True
    assert n101["next_up"] is True

    # #103 is still genuinely blocked on both (#102 is claimed, not terminal).
    n103 = nodes_by_num[103]
    assert n103["ready"] is False
    assert n103["next_up"] is False
    assert set(n103["blocked_on"]) == {101, 102}


def test_milestone_work_orders_empty_when_no_tracking_issue(file_db: Path, valid_config_path: Path):
    """#795: fail-open — no epic-labelled issue means milestone_work_orders is []."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    assert board.get("milestone_work_orders") == [], (
        "milestone_work_orders must be [] when no tracking issue is present"
    )


# ── #1195: children in /board payload ─────────────────────────────────────────

_SUB_ISSUES_BODY = """\
Tracking issue for the milestone.

## Sub-issues
- [ ] #101  {group: A}
- [x] #102  {group: A}

## Notes
Not part of the sub-issues block.
"""


def _make_sub_issues_db(path: Path) -> None:
    """Seed a DB with a tracking issue (label="epic") carrying a
    ``## Sub-issues`` block referencing an open (#101) and a checked/closed
    (#102) child."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute("INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
                 ("laptop", "laptop.tailnet", '["python"]', '["api"]'))
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, 0)",
        ("api", 500, "Milestone tracking", _SUB_ISSUES_BODY, '["epic", "coord"]'),
    )
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.commit()
    conn.close()


@pytest.fixture
def sub_issues_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_sub_issues_db(p)
    return p


def test_children_in_board_payload(sub_issues_db: Path, valid_config_path: Path):
    """#1195: /board payload carries a `children` entry per tracking issue,
    parsed from the `## Sub-issues` checklist via the coord.parentage seam's
    MarkdownParentage adapter — #102's checked box reports state='closed',
    #101's unchecked box reports state='open'."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(sub_issues_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert "children" in board, "children key missing from /board"
    entries = board["children"]
    assert len(entries) == 1, f"expected 1 children entry, got {len(entries)}: {entries}"

    entry = entries[0]
    assert entry["repo_name"] == "api"
    assert entry["tracking_issue"] == 500

    by_num = {c["number"]: c for c in entry["children"]}
    assert set(by_num) == {101, 102}
    assert by_num[101]["state"] == "open"
    assert by_num[102]["state"] == "closed"


def test_children_empty_when_no_tracking_issue(file_db: Path, valid_config_path: Path):
    """#1195: fail-open — no epic-labelled issue means children is []."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    assert board.get("children") == [], "children must be [] when no tracking issue is present"


_MALFORMED_SUB_ISSUES_BODY = """\
Tracking issue for a different milestone, hand-edited badly.

## Sub-issues
- [ ] #201  {group: A}
- fix flaky test

## Notes
Not part of the sub-issues block.
"""


def _make_sub_issues_db_with_malformed_epic(path: Path) -> None:
    """Seed a DB with TWO tracking issues (label="epic"): #500 has a
    well-formed `## Sub-issues` block (per :func:`_make_sub_issues_db`), and
    #600 has a hand-editing mistake (a stray `- fix flaky test` line under
    the heading that doesn't match the `- [ ] #N` grammar) that makes
    `parse_sub_issues` raise ``WorkOrderError``. Used to verify that one
    malformed epic's parse failure doesn't blank `children` for every epic
    on the board (review finding #1 on #1195)."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute("INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
                 ("laptop", "laptop.tailnet", '["python"]', '["api"]'))
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, 0)",
        ("api", 500, "Milestone tracking", _SUB_ISSUES_BODY, '["epic", "coord"]'),
    )
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, 0)",
        ("api", 600, "Malformed milestone tracking", _MALFORMED_SUB_ISSUES_BODY, '["epic", "coord"]'),
    )
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.commit()
    conn.close()


def test_children_malformed_epic_does_not_blank_other_epics(tmp_path: Path, valid_config_path: Path):
    """#1195 review finding #1: a malformed `## Sub-issues` block on one epic
    (#600) must not blank `children` for a well-formed epic (#500) elsewhere
    on the board — the per-epic parse failure must be isolated, matching the
    `milestone_work_orders` block's per-tracking-issue try/except/continue."""
    db_path = tmp_path / "coord.db"
    _make_sub_issues_db_with_malformed_epic(db_path)

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(db_path), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    entries = board["children"]
    by_tracking_issue = {e["tracking_issue"]: e for e in entries}

    # #500's valid children must still be present...
    assert 500 in by_tracking_issue, (
        f"well-formed epic #500's children must survive #600's malformed "
        f"block, got entries: {entries}"
    )
    by_num = {c["number"]: c for c in by_tracking_issue[500]["children"]}
    assert set(by_num) == {101, 102}

    # ...and #600 (the malformed one) must simply be absent, not blank the
    # whole `children` list.
    assert 600 not in by_tracking_issue


_WORK_ORDER_ONLY_TRACKING_BODY = """\
Tracking issue for the milestone — predates the #1008 `## Sub-issues`
convention, only ever got a `## Work order` block from `coord milestone
write-order`. Never additionally spliced with `coord milestone add-child`.

## Work order
- [ ] #101  {group: A}
- [x] #102  {group: A}
"""


def _make_work_order_only_db(path: Path) -> None:
    """Seed a DB with a tracking issue (label="epic") carrying ONLY a
    ``## Work order`` block — no ``## Sub-issues`` checklist at all. This is
    the #1197 fix-iteration repro: epic #1200 rendered with no nested
    children on the live board because it predates the #1008 convention."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute("INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
                 ("laptop", "laptop.tailnet", '["python"]', '["api"]'))
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, 0)",
        ("api", 500, "Milestone tracking", _WORK_ORDER_ONLY_TRACKING_BODY, '["epic", "coord"]'),
    )
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.commit()
    conn.close()


@pytest.fixture
def work_order_only_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_work_order_only_db(p)
    return p


def test_children_falls_back_to_work_order_when_no_sub_issues(
    work_order_only_db: Path, valid_config_path: Path,
):
    """#1197 fix-iteration: an epic with only a `## Work order` block (no
    `## Sub-issues` checklist) must still surface `children` in the /board
    payload so the Pipeline nesting feature works on epics that predate the
    #1008 convention, not just newly-seeded ones."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(work_order_only_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    entries = board["children"]
    assert len(entries) == 1, f"expected 1 children entry, got {len(entries)}: {entries}"
    entry = entries[0]
    assert entry["tracking_issue"] == 500
    by_num = {c["number"]: c for c in entry["children"]}
    assert set(by_num) == {101, 102}
    assert by_num[101]["state"] == "open"
    assert by_num[102]["state"] == "closed"


# ── #975: plan_roster in /board payload ──────────────────────────────────────

def _make_plan_roster_db(path: Path) -> None:
    """Seed a DB with two milestones so the plan_roster aggregation has something
    to compute over:

    - milestone #5 ("Substrate"): tracking epic #500 with a ## Work order,
      three open children (#101, #102, #103) — #103 is blocked on the other two
    - milestone #6 ("Follow-up"): no tracking epic yet — should surface with
      `has_work_order=False` and `needs_you=["no_work_order"]`
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("laptop", "laptop.tailnet", '["python"]', '["api"]'),
    )
    # Tracking epic for milestone #5 — the plan-roster aggregation reads its
    # body to find the ## Work order block.  It IS a member of milestone #5
    # (so aggregate_repo_plans can match it back to the milestone entry).
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "milestone_number, milestone_title, synced_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?, ?, 0)",
        ("api", 500, "Substrate epic", _WORK_ORDER_BODY, '["epic", "coord"]', 5, "Substrate"),
    )
    for num, title in [(101, "Node A"), (102, "Node B"), (103, "Node C")]:
        conn.execute(
            "INSERT INTO issues (repo_name, number, title, body, state, labels, "
            "milestone_number, milestone_title, synced_at) "
            "VALUES (?, ?, ?, '', 'open', '[]', ?, ?, 0)",
            ("api", num, title, 5, "Substrate"),
        )
    # A second milestone that never got a tracking epic.  Only surface it if
    # aggregate_repo_plans sees a member issue — put one open issue under it.
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "milestone_number, milestone_title, synced_at) "
        "VALUES (?, ?, ?, '', 'open', '[]', ?, ?, 0)",
        ("api", 200, "Bare follow-up issue", 6, "Follow-up"),
    )
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.commit()
    conn.close()


@pytest.fixture
def plan_roster_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_plan_roster_db(p)
    return p


def test_plan_roster_in_board_payload(plan_roster_db: Path, valid_config_path: Path):
    """#975: /board payload carries a `plan_roster` field — one entry per
    milestone/epic, with ready / blocked / in-flight / done counts sourced
    from `coord.plans.aggregate_repo_plans`.  The TUI "Plans" panel reads
    this to render one row per plan.
    """
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(plan_roster_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert "plan_roster" in board, "plan_roster key missing from /board"
    roster = board["plan_roster"]
    # Two milestones seeded (#5 with epic, #6 without) — both must surface.
    entries_by_ms = {e["milestone_number"]: e for e in roster}
    assert set(entries_by_ms) == {5, 6}, f"unexpected milestones: {set(entries_by_ms)}"

    substrate = entries_by_ms[5]
    assert substrate["repo"] == "api"
    assert substrate["title"] == "Substrate"
    assert substrate["tracking_issue"] == 500
    assert substrate["has_work_order"] is True
    # Two ready-frontier nodes (#101 + #102, no unmet deps); #103 blocked on both.
    assert substrate["ready_frontier"] == 2
    assert substrate["blocked"] == 1
    assert substrate["in_flight"] == 0
    assert substrate["done"] == 0
    assert substrate["total"] == 3
    assert "ready_waiting" in substrate["needs_you"], (
        f"ready_waiting attention signal missing: {substrate['needs_you']}"
    )

    followup = entries_by_ms[6]
    assert followup["has_work_order"] is False
    assert followup["needs_you"] == ["no_work_order"]
    assert followup["total"] == 0


def test_plan_roster_chat_pending_signal(plan_roster_db: Path, valid_config_path: Path):
    """#976: a running `type="milestone-chat"` assignment against a
    milestone's tracking issue surfaces `chat_pending` in `plan_roster`'s
    `needs_you`, alongside whatever other signal already fired.

    Same override-connection dance as
    `test_milestone_work_orders_claimed_node_ready_but_not_next_up` above —
    `build_board()` reads `coord.state.get_connection()`, not the on-disk
    `plan_roster_db` fixture's connection, so the milestone-chat assignment
    has to be seeded through a file-backed override of that same global.
    """
    from coord import db as _db

    conn = sqlite3.connect(str(plan_roster_db), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("chat500", "laptop", "api", "acme/api", 500, "Milestone chat #500", "running", "milestone-chat"),
    )
    conn.commit()
    _db.override_connection(conn)

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(plan_roster_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    entries_by_ms = {e["milestone_number"]: e for e in board["plan_roster"]}
    substrate = entries_by_ms[5]
    assert "chat_pending" in substrate["needs_you"], (
        f"chat_pending signal missing: {substrate['needs_you']}"
    )
    assert "ready_waiting" in substrate["needs_you"], (
        "chat_pending must be additive, not replace the existing signal: "
        f"{substrate['needs_you']}"
    )

    # milestone #6 (no epic, no chat dispatched against it) is unaffected.
    followup = entries_by_ms[6]
    assert "chat_pending" not in followup["needs_you"]


def test_plan_roster_empty_when_no_milestones(file_db: Path, valid_config_path: Path):
    """#975: fail-open — no milestone-tagged issues means plan_roster is []."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    assert board.get("plan_roster") == [], (
        f"plan_roster must be [] when no milestones exist, got {board.get('plan_roster')!r}"
    )
    # #976: `plan_roster_supported` must still be True here — a genuinely
    # empty roster (no milestones) is a different state from "daemon
    # predates plan_roster" and the TUI needs to tell them apart. Only the
    # absence of the field (pre-#975 daemons never set it) should read as
    # unsupported.
    assert board.get("plan_roster_supported") is True, (
        "plan_roster_supported must be True whenever this daemon computes "
        f"plan_roster at all, even when the roster itself is empty; got "
        f"{board.get('plan_roster_supported')!r}"
    )


def test_plan_roster_supported_flag_true_with_populated_roster(
    plan_roster_db: Path, valid_config_path: Path
):
    """#976: the capability flag accompanies a non-empty roster too — it's
    an "I compute this" signal, independent of whether there's data this
    tick."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(plan_roster_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    assert board.get("plan_roster_supported") is True
    assert len(board["plan_roster"]) > 0


# ── #978: goal_header in /board payload ───────────────────────────────────────

def test_goal_header_in_board_payload(file_db: Path, valid_config_path: Path, monkeypatch):
    """#978: /board carries a `goal_header` field sourced from
    `coord.goal.read_goal_header()` — the coord-tui Plans panel pins this
    above the roster as the GOAL.md north-star header.
    """
    import coord.goal as goal_mod

    monkeypatch.setattr(
        goal_mod,
        "read_goal_header",
        lambda: {
            "available": True,
            "headline": "Ship the thing end to end.",
            "last_updated": "2026-07-04",
            "days_since_update": 2,
        },
    )
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert "goal_header" in board, "goal_header key missing from /board"
    header = board["goal_header"]
    assert header["available"] is True
    assert header["headline"] == "Ship the thing end to end."
    assert header["last_updated"] == "2026-07-04"
    assert header["days_since_update"] == 2


def test_goal_header_unavailable_is_fail_open(file_db: Path, valid_config_path: Path, monkeypatch):
    """#978: when GOAL.md can't be found/read (packaged install, no repo
    root, ...), `goal_header` degrades to `{"available": False}` — it must
    never 503 the whole board.
    """
    import coord.goal as goal_mod

    monkeypatch.setattr(goal_mod, "read_goal_header", lambda: {"available": False})
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert board["goal_header"] == {"available": False}


def test_goal_header_failure_does_not_blank_board(file_db: Path, valid_config_path: Path, monkeypatch):
    """#978: a raising `read_goal_header()` must still fail open, not blank
    the rest of the board payload."""
    import coord.goal as goal_mod

    def _boom():
        raise RuntimeError("goal.md parsing exploded")

    monkeypatch.setattr(goal_mod, "read_goal_header", _boom)
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert board["goal_header"] == {"available": False}
    assert board["round_number"] == 7  # rest of the board is untouched


# ── #2608: roll_pending in /board payload ────────────────────────────────────


def test_roll_pending_in_board_payload_when_live(
    file_db: Path, valid_config_path: Path
):
    """#2608: with a live roll-pending marker on this host, /board carries a
    `roll_pending` object shaped like `RollPending.to_dict()` — target
    version, reason, and the deferral/TTL bookkeeping the TUI needs to
    explain the wait, not just flag that one exists.
    """
    from coord.commands.drive_queue import write_roll_pending
    from coord.drive_queue import RollPending

    write_roll_pending(
        RollPending(
            target_version="0.5.235",
            set_at=1000.0,
            reason="nightly-window",
            ttl_seconds=3600.0,
            max_deferrals=20,
            deferrals=3,
        )
    )
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert board["roll_pending"] is not None
    pending = board["roll_pending"]
    assert pending["target_version"] == "0.5.235"
    assert pending["reason"] == "nightly-window"
    assert pending["set_at"] == 1000.0
    assert pending["ttl_seconds"] == 3600.0
    assert pending["max_deferrals"] == 20
    assert pending["deferrals"] == 3


def test_roll_pending_absent_from_board_payload_when_none(
    file_db: Path, valid_config_path: Path
):
    """#2608: with no marker file, `roll_pending` is `null` — the TUI must
    render exactly its pre-#2608 Queue panel in this case, no empty banner,
    no layout shift.
    """
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert "roll_pending" in board
    assert board["roll_pending"] is None


def test_roll_pending_read_failure_does_not_blank_board(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """#2608: a raising `read_roll_pending()` degrades to `roll_pending: null`
    rather than 503ing (or blanking) the rest of the board — same fail-open
    posture as `goal_header` above."""
    import coord.commands.drive_queue as dq_cmd

    def _boom():
        raise RuntimeError("roll_pending.json parsing exploded")

    monkeypatch.setattr(dq_cmd, "read_roll_pending", _boom)
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert board["roll_pending"] is None
    assert board["round_number"] == 7  # rest of the board is untouched


def _make_finished_milestone_db(path: Path) -> None:
    """Seed a milestone (#7, "Wrapped up") whose tracking epic AND every
    work-order child are closed, but the milestone itself is still open on
    GitHub — the exact scenario #974's ``closed_tracking_issues`` plumbing
    exists to handle ("someone tidied up the epic before remembering to
    close the milestone"). Zero *open* issues remain under this milestone,
    so it must be discovered via the closed epic's own milestone_number, not
    via any open-issue branch.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("laptop", "laptop.tailnet", '["python"]', '["api"]'),
    )
    _finished_work_order_body = """\
Tracking issue for the milestone.

## Work order
- [ ] #104  {group: A}
- [ ] #105  {group: A}
"""
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "milestone_number, milestone_title, synced_at) "
        "VALUES (?, ?, ?, ?, 'closed', ?, ?, ?, 0)",
        ("api", 700, "Wrapped up epic", _finished_work_order_body, '["epic", "coord"]', 7, "Wrapped up"),
    )
    for num, title in [(104, "Node D"), (105, "Node E")]:
        conn.execute(
            "INSERT INTO issues (repo_name, number, title, body, state, labels, "
            "milestone_number, milestone_title, synced_at) "
            "VALUES (?, ?, ?, '', 'closed', '[]', ?, ?, 0)",
            ("api", num, title, 7, "Wrapped up"),
        )
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.commit()
    conn.close()


@pytest.fixture
def finished_milestone_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_finished_milestone_db(p)
    return p


def test_plan_roster_surfaces_milestone_with_only_closed_issues(
    finished_milestone_db: Path, valid_config_path: Path
):
    """#975 fix: a milestone whose tracking epic *and* every work-order child
    are closed — but which is still open on GitHub — must still surface in
    plan_roster as a finished plan (done == total), not silently vanish.

    Before the fix, ``_repo_milestones`` was only seeded from open issues, so
    with zero open issues left under the milestone the outer aggregation
    loop never visited it at all.
    """
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(finished_milestone_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    roster = board["plan_roster"]
    entries_by_ms = {e["milestone_number"]: e for e in roster}
    assert 7 in entries_by_ms, (
        f"finished milestone #7 missing from plan_roster entirely: {entries_by_ms}"
    )
    wrapped_up = entries_by_ms[7]
    assert wrapped_up["tracking_issue"] == 700
    assert wrapped_up["has_work_order"] is True
    assert wrapped_up["total"] == 2
    assert wrapped_up["done"] == 2
    assert wrapped_up["needs_you"] == []


# ── Write path (#590): daemon endpoints ──────────────────────────────────────


def _seed_running_assignment(conn, aid: str = "work9", atype: str = "work") -> None:
    """An in-flight row the seam can transition to a terminal state."""
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "owner/api", 7, "An issue", "running", atype),
    )
    conn.commit()


@pytest.fixture
def rw_db(tmp_path: Path):
    """A thread-safe (file-backed, ``check_same_thread=False``) coord.db override.

    The autouse ``coord_db`` fixture installs a thread-bound ``:memory:`` conn,
    which TestClient (running the async handler on a worker thread) can't touch.
    Production ``get_connection`` already uses ``check_same_thread=False`` (a
    file DB), so this fixture mirrors production for the daemon-write endpoints.
    """
    from coord import db
    from coord.db import _ensure_schema

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


def test_serve_post_result_records_terminal_state(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # The daemon's write path posts a GitHub comment via github_ops; stub it so
    # the test never shells out to `gh`.
    posted: list = []
    monkeypatch.setattr(
        "coord.github_ops.post_issue_comment",
        lambda repo, num, body: posted.append((repo, num, body)),
    )
    # #646 invariant: a verdict may only be recorded on a review row.
    _seed_running_assignment(rw_db, atype="review")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/result",
            json={
                "assignment_id": "work9",
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "owner/api",
                "issue_number": 7,
                "status": "done",
                "verdict": "approve",
                "summary": "looks good",
            },
        )
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "done" and out["posted"] is True
    # The shared DB (the daemon's get_connection target) saw the transition.
    row = rw_db.execute(
        "SELECT status, review_state, review_verdict FROM assignments "
        "WHERE assignment_id='work9'"
    ).fetchone()
    assert row["status"] == "done"
    assert row["review_state"] == "pending"
    assert row["review_verdict"] == "approve"
    # Notifications ledger written so `coord notify` won't double-post.
    led = rw_db.execute(
        "SELECT event FROM notifications WHERE assignment_id='work9'"
    ).fetchone()
    assert led is not None
    assert len(posted) == 1  # exactly one GitHub comment


def test_serve_post_completion_records_done(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **k: None)
    _seed_running_assignment(rw_db, aid="work10")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/completion",
            json={
                "assignment_id": "work10",
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "owner/api",
                "issue_number": 7,
                "exit_code": 0,
                "commits_ahead": 2,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    row = rw_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='work10'"
    ).fetchone()
    assert row["status"] == "done"


def test_serve_post_result_rejects_bad_status(file_db: Path, valid_config_path: Path):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/result",
            json={
                "assignment_id": "x", "machine_name": "m", "repo_name": "r",
                "repo_github": "o/r", "issue_number": 1,
                "status": "bogus", "verdict": None, "summary": "",
            },
        )
    assert resp.status_code == 400


def test_serve_post_result_drops_unknown_keys(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # Forward-compat: a newer client may send a field this daemon doesn't know;
    # it must be dropped, not crash reconstruction.
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **k: None)
    _seed_running_assignment(rw_db, aid="work11")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/result",
            json={
                "assignment_id": "work11", "machine_name": "laptop",
                "repo_name": "api", "repo_github": "owner/api", "issue_number": 7,
                "status": "done", "verdict": None, "summary": "ok",
                "future_field_from_a_newer_client": {"nested": 1},
            },
        )
    assert resp.status_code == 200


def test_serve_writes_require_bearer(file_db: Path, valid_config_path: Path):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path), token="s3cret")
    with TestClient(app) as cli:
        assert cli.post("/result", json={}).status_code == 401
        assert cli.post("/completion", json={}).status_code == 401


# ── Write path (#590): seam routing ──────────────────────────────────────────


def test_post_result_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    """When board_service is set, the seam POSTs the record instead of writing
    the local DB."""
    from coord import client as cc
    from coord import issue_store

    _seed_running_assignment(coord_db, aid="work12")
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}

    def fake_post_record(svc, path, payload, **kw):
        captured["path"] = path
        captured["payload"] = payload
        return {"status": "done", "event": "completion", "posted": True, "error": None}

    monkeypatch.setattr(cc, "post_record", fake_post_record)

    outcome = issue_store.post_result(
        issue_store.ResultRecord(
            assignment_id="work12", machine_name="laptop", repo_name="api",
            repo_github="owner/api", issue_number=7, status="done",
            verdict="approve", summary="ok",
        )
    )
    assert outcome.status == "done"
    assert captured["path"] == "/result"
    assert captured["payload"]["assignment_id"] == "work12"
    # Routed → the local DB row was NOT touched (still running).
    row = coord_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='work12'"
    ).fetchone()
    assert row["status"] == "running"


def test_post_completion_remote_failure_is_graceful(monkeypatch):
    """A daemon round-trip failure must not crash the launcher exit path."""
    from coord import client as cc
    from coord import issue_store

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )

    def boom(*a, **k):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(cc, "post_record", boom)
    outcome = issue_store.post_completion(
        issue_store.CompletionRecord(
            assignment_id="w", machine_name="m", repo_name="r", repo_github="o/r",
            issue_number=1, exit_code=0, commits_ahead=1,
        )
    )
    assert outcome.status == "error" and outcome.posted is False
    assert "daemon down" in (outcome.error or "")


# ── Write path (#590 Phase 2): dispatch + test-verdict ────────────────────────


def test_serve_dispatched_records_assignment_row(
    file_db: Path, valid_config_path: Path, rw_db
):
    from coord.models import Assignment

    a = Assignment(
        # #2087: must be a machine VALID_CONFIG actually configures — the
        # daemon's dispatch endpoints now validate machine/repo against the
        # `Config` `build_app` was given (see state._validate_dispatch_target).
        machine_name="laptop", repo_name="api", issue_number=11,
        issue_title="thin-client dispatch", assignment_id="rev99", type="review",
        review_of_assignment_id="work1", branch="issue-11-x",
    )
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/dispatched",
            json={"assignment": __import__("dataclasses").asdict(a), "repo_github": "owner/api"},
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT status, type, review_of_assignment_id FROM assignments "
        "WHERE assignment_id='rev99'"
    ).fetchone()
    assert row["status"] == "running" and row["type"] == "review"
    assert row["review_of_assignment_id"] == "work1"


def test_serve_dispatched_work_records_row(
    file_db: Path, valid_config_path: Path, rw_db
):
    from coord.models import Proposal

    p = Proposal(
        # #2087: must be a machine VALID_CONFIG actually configures.
        id=1, machine_name="laptop", repo_name="api", issue_number=12,
        issue_title="thin-client work", rationale="because",
    )
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/dispatched-work",
            json={
                "assignment_id": "work88",
                "proposal": __import__("dataclasses").asdict(p),
                "repo_github": "owner/api",
                "provider_name": "claude",
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT status, provider_name FROM assignments WHERE assignment_id='work88'"
    ).fetchone()
    assert row["status"] == "running" and row["provider_name"] == "claude"


def test_serve_post_board_upserts_full_board(
    file_db: Path, valid_config_path: Path, rw_db
):
    """#749: POST /board — the generic whole-board upsert endpoint backing
    coord.board_service.write_board() for the client paths that still
    read-modify-write the full board (assign/approve/stop/retry/…)."""
    from coord.client import serialize_board
    from coord.models import Assignment, Board

    board = Board(
        round_number=4,
        completed=[
            Assignment(
                # #2087: must be a machine VALID_CONFIG actually configures —
                # save_board() now validates repo/machine for genuinely-new
                # rows too (see state.save_board's docstring).
                machine_name="laptop", repo_name="api", issue_number=21,
                issue_title="thin-client board write", assignment_id="wb1",
                status="done", branch="issue-21-x",
            ),
        ],
    )
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/board", json=serialize_board(board))
    assert resp.status_code == 200 and resp.json()["ok"] is True

    row = rw_db.execute(
        "SELECT status, branch FROM assignments WHERE assignment_id='wb1'"
    ).fetchone()
    assert row["status"] == "done" and row["branch"] == "issue-21-x"
    meta = rw_db.execute(
        "SELECT value FROM board_meta WHERE key='round_number'"
    ).fetchone()
    assert meta["value"] == "4"


def test_post_board_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    """coord.client.post_board POSTs the serialized board to /board."""
    from coord import client as cc
    from coord.models import Assignment, Board

    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"ok": True},
    )
    board = Board(
        round_number=6,
        completed=[
            Assignment(
                machine_name="m", repo_name="api", issue_number=1,
                issue_title="t", assignment_id="wb2", status="done",
            ),
        ],
    )
    cc.post_board(cc.ServiceConfig("http://d:7435"), board)
    assert captured["path"] == "/board"
    assert captured["payload"]["round_number"] == 6
    assert captured["payload"]["assignments"][0]["assignment_id"] == "wb2"
    # Routed → nothing written to the local DB.
    assert coord_db.execute(
        "SELECT COUNT(*) c FROM assignments WHERE assignment_id='wb2'"
    ).fetchone()["c"] == 0


def test_serve_test_verdict_records(file_db: Path, valid_config_path: Path, rw_db):
    _seed_running_assignment(rw_db, aid="work77")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/test-verdict",
            json={
                "assignment_id": "work77", "test_state": "failed",
                "test_reason": "scroll broke", "smoke_test": "fail",
                "smoke_test_reason": "scroll broke",
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT test_state, test_reason, smoke_test FROM assignments "
        "WHERE assignment_id='work77'"
    ).fetchone()
    assert row["test_state"] == "failed" and row["smoke_test"] == "fail"
    assert row["test_reason"] == "scroll broke"


def test_serve_review_reaffirm_records(file_db: Path, valid_config_path: Path, rw_db):
    # #1488: the daemon write path for `coord review-reaffirm`.
    _seed_running_assignment(rw_db, aid="rev1", atype="review")
    rw_db.execute(
        "UPDATE assignments SET review_head_sha=?, review_patch_id=?, "
        "review_verdict=? WHERE assignment_id=?",
        ("old-sha", "old-patch", "approve", "rev1"),
    )
    rw_db.commit()
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/review-reaffirm",
            json={
                "review_assignment_id": "rev1",
                "new_head_sha": "new-sha",
                "new_patch_id": "new-patch",
                "reason": "conflict resolution: merged filters, suite green",
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT review_head_sha, review_patch_id, review_verdict FROM assignments "
        "WHERE assignment_id='rev1'"
    ).fetchone()
    assert row["review_head_sha"] == "new-sha"
    assert row["review_patch_id"] == "new-patch"
    # The verdict itself is never touched by a reaffirm.
    assert row["review_verdict"] == "approve"
    audit = rw_db.execute(
        "SELECT event_type, category, actor FROM audit_log "
        "WHERE assignment_id='rev1'"
    ).fetchone()
    assert audit["event_type"] == "review_reaffirmed"
    assert audit["category"] == "review"


def test_serve_review_reaffirm_missing_field_400(
    file_db: Path, valid_config_path: Path, rw_db,
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/review-reaffirm", json={"review_assignment_id": "rev1"})
    assert resp.status_code == 400


def test_serve_review_reaffirm_unknown_assignment_404(
    file_db: Path, valid_config_path: Path, rw_db,
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/review-reaffirm",
            json={
                "review_assignment_id": "no-such-id",
                "new_head_sha": "new-sha",
                "reason": "conflict resolution",
            },
        )
    assert resp.status_code == 404


def test_serve_review_reaffirm_non_review_assignment_409(
    file_db: Path, valid_config_path: Path, rw_db,
):
    """#1488 review round 1: this endpoint accepts an arbitrary assignment id
    from any caller with daemon access — a `work` row must be refused, not
    silently stamped with review anchors + a "Review reaffirmed" audit row."""
    _seed_running_assignment(rw_db, aid="w1", atype="work")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/review-reaffirm",
            json={
                "review_assignment_id": "w1",
                "new_head_sha": "new-sha",
                "reason": "conflict resolution",
            },
        )
    assert resp.status_code == 409
    assert "not 'review'" in resp.json()["error"]
    row = rw_db.execute(
        "SELECT review_head_sha FROM assignments WHERE assignment_id='w1'"
    ).fetchone()
    assert row["review_head_sha"] is None
    assert rw_db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE assignment_id='w1'"
    ).fetchone()["c"] == 0


def test_serve_review_reaffirm_passes_conflict_fix_only_through(
    file_db: Path, valid_config_path: Path, rw_db,
):
    """The attribution flag reaches the audit details, so the trail records
    whether coord could attribute the delta or the human vouched alone."""
    _seed_running_assignment(rw_db, aid="rev2", atype="review")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/review-reaffirm",
            json={
                "review_assignment_id": "rev2",
                "new_head_sha": "new-sha",
                "reason": "hand-resolved rebase",
                "conflict_fix_only": False,
            },
        )
    assert resp.status_code == 200
    audit = rw_db.execute(
        "SELECT details_json FROM audit_log WHERE assignment_id='rev2'"
    ).fetchone()
    assert '"conflict_fix_only": false' in audit["details_json"]


def test_serve_acceptance_verdict_records(file_db: Path, valid_config_path: Path, rw_db):
    # #944: /acceptance-verdict mirrors /test-verdict for the oracle loop's
    # Acceptance-gate verdict.
    _seed_running_assignment(rw_db, aid="work78")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/acceptance-verdict",
            json={
                "assignment_id": "work78", "acceptance_state": "failed",
                "acceptance_reason": "ms01::a: expected A got B",
                "acceptance_sha": "deadbeef",
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT acceptance_state, acceptance_reason, acceptance_sha "
        "FROM assignments WHERE assignment_id='work78'"
    ).fetchone()
    assert row["acceptance_state"] == "failed"
    assert row["acceptance_reason"] == "ms01::a: expected A got B"
    assert row["acceptance_sha"] == "deadbeef"


def test_serve_acceptance_verdict_missing_field_400(
    file_db: Path, valid_config_path: Path, rw_db,
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/acceptance-verdict", json={"assignment_id": "work78"})
    assert resp.status_code == 400


def test_record_dispatched_assignment_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state
    from coord.models import Assignment

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.record_dispatched_assignment(
        assignment=Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="zzz", type="review",
        ),
        repo_github="o/api",
    )
    assert captured["path"] == "/dispatched"
    assert captured["payload"]["assignment"]["assignment_id"] == "zzz"
    # Routed → no local row created.
    assert coord_db.execute(
        "SELECT COUNT(*) c FROM assignments WHERE assignment_id='zzz'"
    ).fetchone()["c"] == 0


def test_record_test_verdict_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.record_test_verdict(
        assignment_id="aaa", test_state="passed", smoke_test="pass",
    )
    assert captured["path"] == "/test-verdict"
    assert captured["payload"]["test_state"] == "passed"


def test_record_review_reaffirm_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.record_review_reaffirm(
        review_assignment_id="rev1", new_head_sha="new-sha", new_patch_id="new-patch",
        reason="conflict resolution",
    )
    assert captured["path"] == "/review-reaffirm"
    assert captured["payload"]["new_head_sha"] == "new-sha"
    assert captured["payload"]["reason"] == "conflict resolution"
    # Routed → no local row touched.
    assert coord_db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE assignment_id='rev1'"
    ).fetchone()["c"] == 0


def test_record_acceptance_verdict_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.record_acceptance_verdict(
        assignment_id="aaa", acceptance_state="passed", acceptance_sha="deadbeef",
    )
    assert captured["path"] == "/acceptance-verdict"
    assert captured["payload"]["acceptance_state"] == "passed"
    assert captured["payload"]["acceptance_sha"] == "deadbeef"


def test_record_dispatched_assignment_unset_writes_local(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state
    from coord.models import Assignment

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    state.record_dispatched_assignment(
        assignment=Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="loc1", type="review",
        ),
        repo_github="o/api",
    )
    assert coord_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='loc1'"
    ).fetchone()["status"] == "running"


# ── Write path (#665): assignment-usage (cost / tokens / is_interactive) ──────


def test_update_assignment_cost_routes_when_service_set(coord_db, monkeypatch):
    """update_assignment_cost() PATCHes /assignment/{id} when board_service is set.

    #1946: was ``POST /assignment-usage``; the resource route supersedes it.
    """
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="cu01")
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "request_resource",
        lambda svc, method, path, payload=None, **kw: captured.update(
            method=method, path=path, payload=payload
        ) or {"updated": True},
    )
    state.update_assignment_cost("cu01", 0.42)
    assert (captured["method"], captured["path"]) == ("PATCH", "/assignment/cu01")
    assert captured["payload"]["cost_usd"] == 0.42
    # Routed → the local DB row was NOT touched.
    row = coord_db.execute(
        "SELECT cost_usd FROM assignments WHERE assignment_id='cu01'"
    ).fetchone()
    assert row["cost_usd"] is None


def test_update_assignment_cost_unset_writes_local(coord_db, monkeypatch):
    """update_assignment_cost() writes the local DB when board_service is unset."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="cu02")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    state.update_assignment_cost("cu02", 1.23)
    row = coord_db.execute(
        "SELECT cost_usd FROM assignments WHERE assignment_id='cu02'"
    ).fetchone()
    assert row["cost_usd"] == 1.23


def test_update_assignment_tokens_routes_when_service_set(coord_db, monkeypatch):
    """update_assignment_tokens() PATCHes /assignment/{id} when board_service is set.

    #1946: was ``POST /assignment-usage``.
    """
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="tu01")
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "request_resource",
        lambda svc, method, path, payload=None, **kw: captured.update(
            method=method, path=path, payload=payload
        ) or {"updated": True},
    )
    state.update_assignment_tokens("tu01", input_tokens=100, output_tokens=50)
    assert (captured["method"], captured["path"]) == ("PATCH", "/assignment/tu01")
    assert captured["payload"]["input_tokens"] == 100
    assert captured["payload"]["output_tokens"] == 50
    # Routed → local row untouched.
    row = coord_db.execute(
        "SELECT input_tokens FROM assignments WHERE assignment_id='tu01'"
    ).fetchone()
    assert row["input_tokens"] is None or row["input_tokens"] == 0


def test_update_assignment_tokens_zero_total_skips_route(coord_db, monkeypatch):
    """update_assignment_tokens() with all-zero counts is a no-op (no POST, no local write)."""
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    called = []
    monkeypatch.setattr(cc, "post_record", lambda *a, **k: called.append(a) or {"ok": True})
    state.update_assignment_tokens("tu-noop")  # all defaults are 0
    assert called == []


def test_update_assignment_tokens_unset_writes_local(coord_db, monkeypatch):
    """update_assignment_tokens() writes the local DB when board_service is unset."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="tu02")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    state.update_assignment_tokens("tu02", input_tokens=200, output_tokens=75,
                                   cache_creation_tokens=10, cache_read_tokens=5)
    row = coord_db.execute(
        "SELECT input_tokens, output_tokens FROM assignments WHERE assignment_id='tu02'"
    ).fetchone()
    assert row["input_tokens"] == 200 and row["output_tokens"] == 75


def test_update_assignment_tokens_routes_num_turns_when_service_set(coord_db, monkeypatch):
    """#2786: `num_turns` rides the same assignment write as the four token
    counts when a board service is configured (#1946: now the PATCH)."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="tu03")
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "request_resource",
        lambda svc, method, path, payload=None, **kw: captured.update(
            method=method, path=path, payload=payload
        ) or {"updated": True},
    )
    state.update_assignment_tokens("tu03", input_tokens=100, output_tokens=50, num_turns=12)
    assert (captured["method"], captured["path"]) == ("PATCH", "/assignment/tu03")
    assert captured["payload"]["num_turns"] == 12
    # Routed → local row untouched.
    row = coord_db.execute(
        "SELECT num_turns FROM assignments WHERE assignment_id='tu03'"
    ).fetchone()
    assert row["num_turns"] is None or row["num_turns"] == 0


def test_mark_assignment_interactive_routes_when_service_set(coord_db, monkeypatch):
    """mark_assignment_interactive() PATCHes /assignment/{id} (#1946)."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="ia01")
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "request_resource",
        lambda svc, method, path, payload=None, **kw: captured.update(
            method=method, path=path, payload=payload
        ) or {"updated": True},
    )
    state.mark_assignment_interactive("ia01")
    assert (captured["method"], captured["path"]) == ("PATCH", "/assignment/ia01")
    assert captured["payload"]["is_interactive"] is True
    # Routed → local row untouched.
    row = coord_db.execute(
        "SELECT is_interactive FROM assignments WHERE assignment_id='ia01'"
    ).fetchone()
    assert not row["is_interactive"]


def test_mark_assignment_interactive_unset_writes_local(coord_db, monkeypatch):
    """mark_assignment_interactive() writes the local DB when board_service is unset."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="ia02")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    state.mark_assignment_interactive("ia02")
    row = coord_db.execute(
        "SELECT is_interactive FROM assignments WHERE assignment_id='ia02'"
    ).fetchone()
    assert row["is_interactive"] == 1


def test_serve_assignment_usage_records_cost(file_db, valid_config_path, rw_db):
    """POST /assignment-usage with cost_usd writes cost to the daemon DB."""
    _seed_running_assignment(rw_db, aid="du01")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={"assignment_id": "du01", "cost_usd": 0.55},
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT cost_usd FROM assignments WHERE assignment_id='du01'"
    ).fetchone()
    assert row["cost_usd"] == 0.55


def test_serve_assignment_usage_records_tokens(file_db, valid_config_path, rw_db):
    """POST /assignment-usage with token fields writes tokens to the daemon DB."""
    _seed_running_assignment(rw_db, aid="du02")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={
                "assignment_id": "du02",
                "input_tokens": 300,
                "output_tokens": 120,
                "cache_creation_tokens": 20,
                "cache_read_tokens": 10,
            },
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT input_tokens, output_tokens FROM assignments WHERE assignment_id='du02'"
    ).fetchone()
    assert row["input_tokens"] == 300 and row["output_tokens"] == 120


def test_serve_assignment_usage_records_num_turns(file_db, valid_config_path, rw_db):
    """#2786: POST /assignment-usage with num_turns writes it to the daemon DB
    alongside the token fields."""
    _seed_running_assignment(rw_db, aid="du02b")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={
                "assignment_id": "du02b",
                "input_tokens": 300,
                "output_tokens": 120,
                "cache_creation_tokens": 20,
                "cache_read_tokens": 10,
                "num_turns": 9,
            },
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT num_turns FROM assignments WHERE assignment_id='du02b'"
    ).fetchone()
    assert row["num_turns"] == 9


def test_serve_assignment_usage_records_interactive(file_db, valid_config_path, rw_db):
    """POST /assignment-usage with is_interactive sets the flag on the daemon DB."""
    _seed_running_assignment(rw_db, aid="du03")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={"assignment_id": "du03", "is_interactive": True},
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT is_interactive FROM assignments WHERE assignment_id='du03'"
    ).fetchone()
    assert row["is_interactive"] == 1


def test_serve_assignment_usage_combined(file_db, valid_config_path, rw_db):
    """POST /assignment-usage can set cost + tokens + interactive in one request."""
    _seed_running_assignment(rw_db, aid="du04")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={
                "assignment_id": "du04",
                "cost_usd": 0.10,
                "input_tokens": 50,
                "output_tokens": 25,
                "cache_creation_tokens": 5,
                "cache_read_tokens": 2,
                "is_interactive": True,
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT cost_usd, input_tokens, is_interactive FROM assignments "
        "WHERE assignment_id='du04'"
    ).fetchone()
    assert row["cost_usd"] == 0.10
    assert row["input_tokens"] == 50
    assert row["is_interactive"] == 1


def test_serve_assignment_usage_records_smoke_tests(file_db, valid_config_path, rw_db):
    """#749: POST /assignment-usage also routes the SMOKE_TESTS block —
    coord.state.update_assignment_smoke_tests was previously unrouted, so a
    thin client's `coord notify`/`coord approve-plan` never recorded it."""
    _seed_running_assignment(rw_db, aid="du05")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={"assignment_id": "du05", "smoke_tests": ["click the button"]},
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT smoke_tests FROM assignments WHERE assignment_id='du05'"
    ).fetchone()
    assert row["smoke_tests"] == '["click the button"]'


def test_serve_assignment_usage_records_stop_reason(file_db, valid_config_path, rw_db):
    """#2316: POST /assignment-usage also routes stop_reason — same endpoint,
    same "one round-trip covers all the diagnostic fields" pattern as
    cost/tokens/smoke_tests above."""
    _seed_running_assignment(rw_db, aid="du06")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={"assignment_id": "du06", "stop_reason": "length"},
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT stop_reason FROM assignments WHERE assignment_id='du06'"
    ).fetchone()
    assert row["stop_reason"] == "length"


def test_update_assignment_smoke_tests_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "request_resource",
        lambda svc, method, path, payload=None, **kw: captured.update(
            method=method, path=path, payload=payload
        ) or {"updated": True},
    )
    state.update_assignment_smoke_tests("aid1", ["run the tests"])
    # #1946: was POST /assignment-usage.
    assert (captured["method"], captured["path"]) == ("PATCH", "/assignment/aid1")
    assert captured["payload"]["smoke_tests"] == ["run the tests"]


def test_serve_assignment_usage_missing_id(file_db, valid_config_path):
    """POST /assignment-usage without assignment_id returns 400."""
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/assignment-usage", json={"cost_usd": 1.0})
    assert resp.status_code == 400


# ── Write path (#601): issue-cache (labels + sync) ────────────────────────────


def test_serve_issue_labels_updates_cache(file_db: Path, valid_config_path: Path, rw_db):
    import json as _j
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("api", 586, "x", "", "open", '["coord", "status:ready"]', 1.0),
    )
    rw_db.commit()
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-labels",
            json={"repo_name": "api", "issue_number": 586, "labels": ["coord"]},
        )
    assert resp.status_code == 200 and resp.json()["updated"] is True
    row = rw_db.execute(
        "SELECT labels FROM issues WHERE repo_name='api' AND number=586"
    ).fetchone()
    assert _j.loads(row["labels"]) == ["coord"]  # status:ready stripped on the daemon


def test_serve_issues_sync_upserts(file_db: Path, valid_config_path: Path, rw_db):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issues-sync",
            json={
                "repo_name": "api",
                "issues": [
                    {"number": 7, "title": "issue seven", "body": "b",
                     "labels": [{"name": "coord"}]},
                ],
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT title, state FROM issues WHERE repo_name='api' AND number=7"
    ).fetchone()
    assert row["title"] == "issue seven" and row["state"] == "open"


def test_update_issue_labels_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, state, labels, synced_at) "
        "VALUES ('api', 9, 'x', 'open', '[\"coord\", \"status:ready\"]', 1.0)"
    )
    coord_db.commit()
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "request_resource",
        lambda svc, method, path, payload=None, **kw: captured.update(
            method=method, path=path, payload=payload
        ) or {"updated": True},
    )
    assert state.update_issue_labels("api", 9, ["coord"]) is True
    # #1946: was POST /issue-labels.
    assert (captured["method"], captured["path"]) == ("PATCH", "/issue/api/9")
    assert captured["payload"]["labels"] == ["coord"]
    # Routed → the local issues row is NOT touched (still has status:ready).
    import json as _j
    row = coord_db.execute("SELECT labels FROM issues WHERE number=9").fetchone()
    assert "status:ready" in _j.loads(row["labels"])


def test_upsert_open_issues_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"ok": True},
    )
    state.upsert_open_issues("api", [{"number": 1, "title": "t", "labels": []}])
    assert captured["path"] == "/issues-sync"
    assert captured["payload"]["repo_name"] == "api"
    # Routed → no local issues row created.
    assert coord_db.execute("SELECT COUNT(*) c FROM issues").fetchone()["c"] == 0


def test_serve_issue_edit_writes_backend_and_cache(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # The tracker (gh) write runs on the DAEMON behind the seam — stub it so the
    # test never shells out, and assert it got the github slug + new content.
    calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.edit_issue",
        lambda repo, num, *, title=None, body=None: calls.append((repo, num, title, body)),
    )
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("api", 7, "old title", "old body", "open", "[]", 1.0),
    )
    rw_db.commit()
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-edit",
            json={
                "repo_name": "api",
                "issue_number": 7,
                "title": "new title",
                "body": "new body",
                "repo_github": "owner/api",
            },
        )
    assert resp.status_code == 200 and resp.json()["updated"] is True
    assert calls == [("owner/api", 7, "new title", "new body")]
    # Cache mirrors the edit so the TUI reflects it on the next refresh.
    row = rw_db.execute(
        "SELECT title, body FROM issues WHERE repo_name='api' AND number=7"
    ).fetchone()
    assert row["title"] == "new title" and row["body"] == "new body"


def test_serve_issue_label_writes_backend_and_cache(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """#802 daemon route: POST /issue-label runs the gh write on the daemon
    and mirrors the resulting label set into the local ``issues`` cache —
    the seam counterpart of test_serve_issue_edit_writes_backend_and_cache."""
    import json

    calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.change_issue_labels",
        lambda repo, num, *, add, remove: (
            calls.append((repo, num, add, remove)) or (["bug", "existing"], True)
        ),
    )
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("api", 7, "an issue", "", "open", '["existing"]', 1.0),
    )
    rw_db.commit()
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-label",
            json={
                "repo_name": "api",
                "issue_number": 7,
                "add": ["bug"],
                "remove": [],
                "repo_github": "owner/api",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["labels"] == ["bug", "existing"] and body["changed"] is True
    assert calls == [("owner/api", 7, {"bug"}, set())]
    # Cache mirrors the new label set so the TUI reflects it without a full sync.
    row = rw_db.execute(
        "SELECT labels FROM issues WHERE repo_name='api' AND number=7"
    ).fetchone()
    assert json.loads(row["labels"]) == ["bug", "existing"]


def test_serve_issue_label_gh_not_found_returns_422(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """Fix B: POST /issue-label returns 422 when GhNotFound is raised (label
    doesn't exist in the repo after auto-create attempt), not 503."""
    from coord.github_ops import GhNotFound

    monkeypatch.setattr(
        "coord.github_ops.change_issue_labels",
        lambda *a, **k: (_ for _ in ()).throw(
            GhNotFound(
                "GraphQL: Could not resolve to a Label with the name 'ghost'."
            )
        ),
    )
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("api", 8, "an issue", "", "open", '["existing"]', 1.0),
    )
    rw_db.commit()
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-label",
            json={
                "repo_name": "api",
                "issue_number": 8,
                "add": ["ghost"],
                "remove": [],
                "repo_github": "owner/api",
            },
        )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "label not found"
    assert "detail" in body


def test_serve_issue_label_backend_error_returns_503(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """Fix B: POST /issue-label returns 503 for a genuine backend failure
    (auth/network/rate-limit), not 422."""
    monkeypatch.setattr(
        "coord.github_ops.change_issue_labels",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("gh issue edit 8 failed: HTTP 401: Bad credentials")
        ),
    )
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("api", 8, "an issue", "", "open", '["existing"]', 1.0),
    )
    rw_db.commit()
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-label",
            json={
                "repo_name": "api",
                "issue_number": 8,
                "add": ["coord"],
                "remove": [],
                "repo_github": "owner/api",
            },
        )
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "issue-label write failed"
    assert "detail" in body


def test_serve_issue_create_writes_backend_and_cache(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """#802 daemon route: POST /issue-create runs the gh create on the daemon
    and inserts the new issue into the local ``issues`` cache — the seam
    counterpart of test_serve_issue_edit_writes_backend_and_cache."""
    import json

    calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.create_issue",
        lambda repo, title, body, *, labels=None: (
            calls.append((repo, title, body, labels))
            or {"number": 99, "url": "https://github.com/owner/api/issues/99"}
        ),
    )
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-create",
            json={
                "repo_name": "api",
                "title": "new issue",
                "body": "issue body",
                "labels": ["bug"],
                "repo_github": "owner/api",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"number": 99, "url": "https://github.com/owner/api/issues/99"}
    assert calls == [("owner/api", "new issue", "issue body", ["bug"])]
    # Cache gains the new issue row so the TUI reflects it without a full sync.
    row = rw_db.execute(
        "SELECT title, body, labels FROM issues WHERE repo_name='api' AND number=99"
    ).fetchone()
    assert row is not None
    assert row["title"] == "new issue" and row["body"] == "issue body"
    assert json.loads(row["labels"]) == ["bug"]


class _AlwaysLockedConn:
    """Wraps a real connection, making every ``execute()`` raise
    `database is locked` — simulates sustained contention (a concurrent
    writer that never lets go within the retry budget).

    #2726: `_create_issue_local`'s cache-mirror write now goes through
    `coord.sql.execute()`, which calls `conn.cursor()` then
    `cursor.execute()` rather than the sqlite3 connection-level `.execute()`
    shortcut, so `cursor()` must be implemented too. `__module__` is pinned
    to `"sqlite3"` so `coord.sql.detect_dialect` (keyed off
    `type(conn).__module__`) recognizes this fake as SQLite instead of
    raising `UnsupportedDialectError` before the intended lock error ever
    fires.
    """

    __module__ = "sqlite3"

    def __init__(self, real_conn) -> None:
        self._real = real_conn

    def cursor(self):
        return self

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_serve_issue_create_returns_200_under_sustained_lock_contention(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """#2689 core regression, at the handler level: before the fix, a lock
    that outlasted the cache-mirror write's retry budget propagated out of
    `_create_issue_local` as a bare `OperationalError`, which
    `post_issue_create`'s blanket `except Exception` turned into a 503 —
    even though the GitHub issue had already been created. The natural
    response to that 503 (retry the command) filed a duplicate issue. The
    handler must now return 200 with the issue number, not 503, once the
    upstream GitHub write has already landed."""
    calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.create_issue",
        lambda repo, title, body, *, labels=None: (
            calls.append((repo, title, body, labels))
            or {"number": 99, "url": "https://github.com/owner/api/issues/99"}
        ),
    )
    monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "coord.state.get_connection", lambda: _AlwaysLockedConn(rw_db)
    )
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-create",
            json={
                "repo_name": "api",
                "title": "new issue",
                "body": "issue body",
                "labels": ["bug"],
                "repo_github": "owner/api",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"number": 99, "url": "https://github.com/owner/api/issues/99"}
    assert calls == [("owner/api", "new issue", "issue body", ["bug"])], (
        "GitHub create_issue must be called exactly once — no duplicate filing"
    )


def test_edit_issue_content_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "request_resource",
        lambda svc, method, path, payload=None, **kw: captured.update(
            method=method, path=path, payload=payload
        ) or {"updated": True},
    )
    # When routing to the daemon, the backend write must NOT run client-side.
    def _boom(*a, **k):
        raise AssertionError("backend write must run on the daemon, not the client")

    monkeypatch.setattr("coord.github_ops.edit_issue", _boom)
    assert (
        state.edit_issue_content("api", 9, title="t", repo_github="owner/api") is True
    )
    # #1946: was POST /issue-edit.
    assert (captured["method"], captured["path"]) == ("PATCH", "/issue/api/9")
    assert captured["payload"]["title"] == "t"
    assert captured["payload"]["repo_github"] == "owner/api"


# ── #603: per-issue context store ───────────────────────────────────────────

def test_serve_issue_context_add_get_pin_clear(file_db: Path, valid_config_path: Path, rw_db):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        a = cli.post("/issue-context", json={
            "action": "add", "repo_name": "api", "issue_number": 7,
            "body": "depends on lib #99", "pinned": True, "source": "operator",
        })
        assert a.status_code == 200
        eid = a.json()["entry_id"]
        assert isinstance(eid, int)
        cli.post("/issue-context", json={
            "action": "add", "repo_name": "api", "issue_number": 7,
            "body": "a later note", "source": "test",
        })
        # GET returns both entries, oldest-first.
        g = cli.get("/issue-context", params={"repo_name": "api", "issue_number": 7})
        assert g.status_code == 200
        entries = g.json()["entries"]
        assert [e["body"] for e in entries] == ["depends on lib #99", "a later note"]
        assert entries[0]["pinned"] is True
        # unpin, then clear.
        p = cli.post("/issue-context", json={
            "action": "pin", "repo_name": "api", "issue_number": 7,
            "entry_id": eid, "pinned": False,
        })
        assert p.json()["updated"] is True
        c = cli.post("/issue-context", json={
            "action": "clear", "repo_name": "api", "issue_number": 7,
        })
        assert c.json()["deleted"] == 2
    assert rw_db.execute(
        "SELECT COUNT(*) c FROM issue_context WHERE repo_name='api' AND issue_number=7"
    ).fetchone()["c"] == 0


def test_serve_issue_context_unknown_action_400(file_db: Path, valid_config_path: Path, rw_db):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/issue-context", json={
            "action": "bogus", "repo_name": "api", "issue_number": 7,
        })
    assert resp.status_code == 400


# ── #1505: driver escalation records ────────────────────────────────────────

def test_serve_drive_escalations_record_get_dismiss(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        r = cli.post("/drive-escalations", json={
            "action": "record", "repo_name": "api", "issue_number": 7,
            "stage": "merge", "reason": "merge_status=NEEDS_ATTENTION",
            "gate_readings": "merge_status=NEEDS_ATTENTION | pr_url=(none)",
            "proposed_command": "coord merge --plan --repo api",
            "assignment_id": "w1",
        })
        assert r.status_code == 200
        eid = r.json()["entry_id"]
        assert isinstance(eid, int)

        g = cli.get("/drive-escalations", params={"repo_name": "api", "issue_number": 7})
        assert g.status_code == 200
        entries = g.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["reason"] == "merge_status=NEEDS_ATTENTION"
        assert entries[0]["proposed_command"] == "coord merge --plan --repo api"
        assert entries[0]["assignment_id"] == "w1"

        # A second `record` for the SAME issue replaces, not duplicates.
        cli.post("/drive-escalations", json={
            "action": "record", "repo_name": "api", "issue_number": 7,
            "reason": "still stuck", "proposed_command": "gh pr merge 1 --rebase",
        })
        g2 = cli.get("/drive-escalations", params={"repo_name": "api", "issue_number": 7})
        assert len(g2.json()["entries"]) == 1
        assert g2.json()["entries"][0]["reason"] == "still stuck"

        d = cli.post("/drive-escalations", json={
            "action": "dismiss", "repo_name": "api", "issue_number": 7,
        })
        assert d.json()["deleted"] is True
    assert rw_db.execute(
        "SELECT COUNT(*) c FROM drive_escalations WHERE repo_name='api' AND issue_number=7"
    ).fetchone()["c"] == 0


def test_serve_drive_escalations_list_all_and_by_repo(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        cli.post("/drive-escalations", json={
            "action": "record", "repo_name": "api", "issue_number": 7,
            "reason": "r1", "proposed_command": "c1",
        })
        cli.post("/drive-escalations", json={
            "action": "record", "repo_name": "other", "issue_number": 9,
            "reason": "r2", "proposed_command": "c2",
        })
        everything = cli.get("/drive-escalations").json()["entries"]
        assert {e["repo_name"] for e in everything} == {"api", "other"}
        just_api = cli.get("/drive-escalations", params={"repo_name": "api"}).json()["entries"]
        assert [e["repo_name"] for e in just_api] == ["api"]


def test_serve_drive_escalations_unknown_action_400(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/drive-escalations", json={
            "action": "bogus", "repo_name": "api", "issue_number": 7,
        })
    assert resp.status_code == 400


def test_record_drive_escalation_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"entry_id": 42},
    )
    assert state.record_drive_escalation(
        "api", 7, stage="merge", reason="stuck", gate_readings="",
        proposed_command="coord merge --plan --repo api",
    ) == 42
    assert captured["path"] == "/drive-escalations"
    assert captured["payload"]["action"] == "record"
    # Routed → no local row created.
    assert coord_db.execute("SELECT COUNT(*) c FROM drive_escalations").fetchone()["c"] == 0


def test_dismiss_drive_escalation_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: {"deleted": True},
    )
    assert state.dismiss_drive_escalation("api", 7) is True


def test_get_drive_escalation_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    monkeypatch.setattr(
        cc, "fetch_drive_escalation",
        lambda svc, repo, num: {"reason": "remote stuck", "proposed_command": "c"},
    )
    assert state.get_drive_escalation("api", 7)["reason"] == "remote stuck"


def test_record_drive_escalation_local_upserts_by_repo_and_issue(coord_db):
    from coord import state

    first = state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="first", gate_readings="",
        proposed_command="c1",
    )
    second = state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="second", gate_readings="",
        proposed_command="c2",
    )
    assert first == second  # same row, not a duplicate
    rows = state._list_drive_escalations_local("api")
    assert len(rows) == 1
    assert rows[0]["reason"] == "second"


def test_dismiss_drive_escalation_local_reports_whether_a_row_existed(coord_db):
    from coord import state

    assert state._dismiss_drive_escalation_local("api", 7) is False
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="x", gate_readings="", proposed_command="y",
    )
    assert state._dismiss_drive_escalation_local("api", 7) is True
    assert state._get_drive_escalation_local("api", 7) is None


# ── #1753 (DQ-1): drive queue ───────────────────────────────────────────────

def test_serve_drive_queue_enqueue_list_move_update_dequeue(
    tmp_path: Path, valid_config_path: Path, rw_db
):
    """Black-box: drive the running daemon over HTTP through the whole
    lifecycle and assert on the responses (the #1753 acceptance bar)."""
    app = build_app(SqliteStore(tmp_path / "rw.db"), load_config(valid_config_path))
    with TestClient(app) as cli:
        r = cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "claude-coordinator",
            "issue_number": 1, "machine": "dellserver",
            "after": ["claude-coordinator#2"],
        })
        assert r.status_code == 200
        assert isinstance(r.json()["entry_id"], int)

        # Two more so there's a span to renumber.
        cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "claude-coordinator",
            "issue_number": 2, "after": [],
        })
        cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "api", "issue_number": 9,
        })

        entries = cli.get("/drive-queue").json()["entries"]
        assert [(e["repo_name"], e["issue_number"], e["position"]) for e in entries] == [
            ("claude-coordinator", 1, 0), ("claude-coordinator", 2, 1), ("api", 9, 2),
        ]
        first = entries[0]
        # after_json is a REAL list on the wire, not a JSON string.
        assert first["after_json"] == ["claude-coordinator#2"]
        assert first["machine"] == "dellserver"
        assert first["state"] == "waiting"
        assert first["attempts"] == 0 and first["deferrals"] == 0
        assert entries[2]["machine"] is None  # NULL = let `coord drive` route it

        # move to the head → dense, 0-based, no gaps or collisions.
        m = cli.post("/drive-queue", json={
            "action": "move", "repo_name": "api", "issue_number": 9,
            "to_position": 0,
        })
        assert m.json()["moved"] is True
        moved = cli.get("/drive-queue").json()["entries"]
        assert [(e["repo_name"], e["issue_number"]) for e in moved] == [
            ("api", 9), ("claude-coordinator", 1), ("claude-coordinator", 2),
        ]
        assert [e["position"] for e in moved] == [0, 1, 2]

        u = cli.post("/drive-queue", json={
            "action": "update", "repo_name": "api", "issue_number": 9,
            "fields": {
                "state": "running", "attempts": 1, "session_name": "drive-api-9",
                "launched_at": 1234.5, "last_reason": "",
            },
        })
        assert u.json()["updated"] is True
        one = cli.get("/drive-queue", params={
            "repo_name": "api", "issue_number": 9,
        }).json()["entries"]
        assert len(one) == 1
        assert one[0]["state"] == "running"
        assert one[0]["attempts"] == 1
        assert one[0]["session_name"] == "drive-api-9"
        assert one[0]["launched_at"] == 1234.5

        d = cli.post("/drive-queue", json={
            "action": "dequeue", "repo_name": "api", "issue_number": 9,
        })
        assert d.json()["deleted"] is True
        after = cli.get("/drive-queue").json()["entries"]
        # Removal renumbers what's left back to a dense 0-based run.
        assert [e["position"] for e in after] == [0, 1]
        assert [e["issue_number"] for e in after] == [1, 2]

    assert rw_db.execute(
        "SELECT COUNT(*) c FROM drive_queue WHERE repo_name='api'"
    ).fetchone()["c"] == 0


def test_serve_drive_queue_enqueue_existing_updates_in_place(
    tmp_path: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(tmp_path / "rw.db"), load_config(valid_config_path))
    with TestClient(app) as cli:
        first = cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "api", "issue_number": 7,
            "machine": "laptop", "after": [],
        }).json()["entry_id"]
        second = cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "api", "issue_number": 7,
            "machine": "dellserver", "after": ["api#6"],
        }).json()["entry_id"]
        assert first == second  # same row, not a duplicate
        entries = cli.get("/drive-queue").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["machine"] == "dellserver"
    assert entries[0]["after_json"] == ["api#6"]


def test_serve_drive_queue_filters_by_repo(
    tmp_path: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(tmp_path / "rw.db"), load_config(valid_config_path))
    with TestClient(app) as cli:
        cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "api", "issue_number": 1,
        })
        cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "other", "issue_number": 2,
        })
        everything = cli.get("/drive-queue").json()["entries"]
        assert {e["repo_name"] for e in everything} == {"api", "other"}
        just_api = cli.get("/drive-queue", params={"repo_name": "api"}).json()["entries"]
    assert [e["repo_name"] for e in just_api] == ["api"]


def test_serve_drive_queue_bad_requests_are_400(
    tmp_path: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(tmp_path / "rw.db"), load_config(valid_config_path))
    with TestClient(app) as cli:
        assert cli.post("/drive-queue", json={
            "action": "bogus", "repo_name": "api", "issue_number": 7,
        }).status_code == 400
        assert cli.post("/drive-queue", json={
            "action": "enqueue", "issue_number": 7,
        }).status_code == 400
        assert cli.get("/drive-queue", params={
            "repo_name": "api", "issue_number": "nope",
        }).status_code == 400
        cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "api", "issue_number": 7,
        })
        # `position` is owned by enqueue/move — a tick may not smuggle it in.
        bad = cli.post("/drive-queue", json={
            "action": "update", "repo_name": "api", "issue_number": 7,
            "fields": {"position": 5},
        })
        assert bad.status_code == 400
        assert "position" in bad.json()["error"]


def test_serve_leg_counts(tmp_path: Path, valid_config_path: Path, rw_db):
    """#3060: `GET /leg-counts` — its OWN endpoint, deliberately not folded
    into `/board` or `/drive-queue` (the source is `assignments`, not
    `drive_queue`)."""
    rw_db.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, type) VALUES ('a-1', 'm', 'api', 1, 't', 'work')"
    )
    rw_db.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, type) VALUES ('a-2', 'm', 'api', 1, 't', 'review')"
    )
    rw_db.commit()
    app = build_app(SqliteStore(tmp_path / "rw.db"), load_config(valid_config_path))
    with TestClient(app) as cli:
        r = cli.get("/leg-counts")
        assert r.status_code == 200
        assert r.json() == {"api#1": {"work": 1, "review": 1}}


def test_serve_drive_queue_enqueue_at_explicit_position(
    tmp_path: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(tmp_path / "rw.db"), load_config(valid_config_path))
    with TestClient(app) as cli:
        for n in (1, 2, 3):
            cli.post("/drive-queue", json={
                "action": "enqueue", "repo_name": "api", "issue_number": n,
            })
        cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "api", "issue_number": 4,
            "position": 1,
        })
        entries = cli.get("/drive-queue").json()["entries"]
    assert [e["issue_number"] for e in entries] == [1, 4, 2, 3]
    assert [e["position"] for e in entries] == [0, 1, 2, 3]


def test_board_projection_carries_drive_queue(tmp_path: Path, valid_config_path: Path, rw_db):
    db_path = tmp_path / "rw.db"
    app = build_app(SqliteStore(db_path), load_config(valid_config_path))
    with TestClient(app) as cli:
        cli.post("/drive-queue", json={
            "action": "enqueue", "repo_name": "api", "issue_number": 7,
            "machine": "dellserver", "after": ["api#6"],
        })
    proj = SqliteStore(db_path).board_projection()
    assert [e["issue_number"] for e in proj["drive_queue"]] == [7]
    # Same JSON-column decoding as /drive-queue — a list, never a string.
    assert proj["drive_queue"][0]["after_json"] == ["api#6"]
    assert proj["drive_queue"][0]["position"] == 0


def test_openapi_exposes_board_drive_queue_entry(file_db: Path, valid_config_path: Path):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        spec = cli.get("/openapi.json").json()
    entry = spec["components"]["schemas"]["BoardDriveQueueEntry"]
    assert {"position", "after_json", "state", "enqueued_at"} <= set(entry["properties"])
    assert "/drive-queue" in spec["paths"]
    board_props = (
        spec["paths"]["/board"]["get"]["responses"]["200"]
        ["content"]["application/json"]["schema"]["properties"]
    )
    assert board_props["drive_queue"]["items"]["$ref"].endswith("BoardDriveQueueEntry")


def _routing_svc(monkeypatch):
    from coord import client as cc

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    return cc, captured


def test_drive_queue_writes_route_when_service_set(coord_db, monkeypatch):
    """Thin-client posture: every write routes and touches NO local row."""
    from coord import state

    cc, captured = _routing_svc(monkeypatch)
    replies = {
        "enqueue": {"entry_id": 42},
        "dequeue": {"deleted": True},
        "update": {"updated": True},
        "move": {"moved": True},
    }
    calls: list[dict] = []

    def _post(svc, path, payload, **kw):
        calls.append({"path": path, "payload": payload})
        return replies[payload["action"]]

    monkeypatch.setattr(cc, "post_record", _post)

    assert state.enqueue_drive_queue(
        "api", 7, machine="dellserver", after=["api#6"]
    ) == 42
    assert state.update_drive_queue_entry("api", 7, state="running") is True
    assert state.move_drive_queue_entry("api", 7, 0) is True
    assert state.dequeue_drive_queue("api", 7) is True

    assert {c["path"] for c in calls} == {"/drive-queue"}
    assert [c["payload"]["action"] for c in calls] == [
        "enqueue", "update", "move", "dequeue",
    ]
    assert calls[0]["payload"]["after"] == ["api#6"]
    assert calls[1]["payload"]["fields"] == {"state": "running"}
    # Routed → the local DB was never written.
    assert coord_db.execute("SELECT COUNT(*) c FROM drive_queue").fetchone()["c"] == 0


def test_drive_queue_reads_route_when_service_set(coord_db, monkeypatch):
    from coord import state

    cc, _ = _routing_svc(monkeypatch)
    monkeypatch.setattr(
        cc, "fetch_drive_queue",
        lambda svc, repo=None: [{"repo_name": "api", "issue_number": 7}],
    )
    monkeypatch.setattr(
        cc, "fetch_drive_queue_entry",
        lambda svc, repo, num: {"repo_name": repo, "issue_number": num, "state": "running"},
    )
    assert state.list_drive_queue()[0]["issue_number"] == 7
    assert state.get_drive_queue_entry("api", 7)["state"] == "running"
    assert coord_db.execute("SELECT COUNT(*) c FROM drive_queue").fetchone()["c"] == 0


def test_drive_queue_local_rejects_non_updatable_field(coord_db):
    from coord import state

    state._enqueue_drive_queue_local("api", 7)
    with pytest.raises(ValueError, match="machine"):
        state._update_drive_queue_entry_local("api", 7, machine="laptop")


def test_drive_queue_local_stamps_reason_at_when_last_reason_is_written(coord_db):
    """#2133: `last_reason` is a point-in-time observation, so every write to
    it is dated — the queue's ONE choke point for both the no-daemon local
    path and the daemon's `/drive-queue` update handler
    (`serve_app.post_drive_queue` calls this exact function), so no caller
    can set a reason without also dating it.
    """
    import time as _time

    from coord import state

    state._enqueue_drive_queue_local("api", 7)
    assert state._get_drive_queue_entry_local("api", 7)["reason_at"] is None

    before = _time.time()
    assert state._update_drive_queue_entry_local(
        "api", 7, state="blocked", last_reason="checks_failed"
    )
    after = _time.time()
    entry = state._get_drive_queue_entry_local("api", 7)
    assert entry["reason_at"] is not None
    assert before <= entry["reason_at"] <= after
    first_stamp = entry["reason_at"]

    # A write that never touches `last_reason` must not re-date it — the
    # timestamp names when the REASON was captured, not when the row last
    # changed for any reason.
    assert state._update_drive_queue_entry_local("api", 7, attempts=1)
    entry = state._get_drive_queue_entry_local("api", 7)
    assert entry["reason_at"] == first_stamp
    assert entry["last_reason"] == "checks_failed"

    # Re-writing `last_reason` — even to the identical text — re-dates it: a
    # fresh observation of the same condition is still a fresh observation.
    _time.sleep(0.01)
    assert state._update_drive_queue_entry_local(
        "api", 7, last_reason="checks_failed"
    )
    entry = state._get_drive_queue_entry_local("api", 7)
    assert entry["reason_at"] > first_stamp


def test_drive_queue_daemon_update_handler_also_stamps_reason_at(
    tmp_path: Path, valid_config_path: Path, rw_db,
):
    """The daemon's `/drive-queue` update action must date `last_reason` the
    same way the local (no-daemon) path does — #2133's fix has to hold for a
    thin client too, not just a standalone `coord` invocation. Mirrors
    `test_board_projection_carries_drive_queue`'s use of `rw_db` (a
    thread-safe file-backed override `TestClient`'s worker thread can
    actually see) rather than the autouse `:memory:` `coord_db`.
    """
    import time as _time

    from coord import state

    db_path = tmp_path / "rw.db"
    app = build_app(SqliteStore(db_path), load_config(valid_config_path))
    with TestClient(app) as cli:
        cli.post(
            "/drive-queue",
            json={"action": "enqueue", "repo_name": "api", "issue_number": 7},
        )
        resp = cli.post(
            "/drive-queue",
            json={
                "action": "update",
                "repo_name": "api",
                "issue_number": 7,
                "fields": {"state": "blocked", "last_reason": "checks_failed"},
            },
        )
    assert resp.status_code == 200, resp.text
    entry = state._get_drive_queue_entry_local("api", 7)
    assert entry["reason_at"] is not None
    assert entry["reason_at"] <= _time.time()


def test_drive_queue_local_move_clamps_out_of_range(coord_db):
    from coord import state

    for n in (1, 2, 3):
        state._enqueue_drive_queue_local("api", n)
    assert state._move_drive_queue_entry_local("api", 1, 99) is True
    assert [e["issue_number"] for e in state._list_drive_queue_local()] == [2, 3, 1]
    assert [e["position"] for e in state._list_drive_queue_local()] == [0, 1, 2]
    assert state._move_drive_queue_entry_local("api", 1, -5) is True
    assert [e["issue_number"] for e in state._list_drive_queue_local()] == [1, 2, 3]
    # Nothing to move → False, not an exception.
    assert state._move_drive_queue_entry_local("api", 99, 0) is False


def test_drive_queue_local_decodes_bad_after_json_to_empty_list(coord_db):
    from coord import state

    state._enqueue_drive_queue_local("api", 7, after=["api#6"])
    coord_db.execute("UPDATE drive_queue SET after_json = 'not json'")
    coord_db.commit()
    assert state._get_drive_queue_entry_local("api", 7)["after_json"] == []


# ── #873: durable issue_comments mirror ─────────────────────────────────────

def test_serve_issue_comments_capture_then_get(file_db: Path, valid_config_path: Path, rw_db):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        c = cli.post("/issue-comments", json={
            "action": "capture", "repo_name": "acme/api", "issue_number": 7,
            "body": "<!-- coord:event=completion assignment=a1 machine=m --> done",
            "gh_comment_id": 501,
        })
        assert c.status_code == 200
        assert c.json() == {"ok": True}
        g = cli.get("/issue-comments", params={"repo_name": "acme/api", "issue_number": 7})
        assert g.status_code == 200
        comments = g.json()["comments"]
        assert len(comments) == 1
        assert comments[0]["gh_comment_id"] == 501
        assert comments[0]["coord_event"] == "completion"
        assert comments[0]["coord_assignment_id"] == "a1"
    assert rw_db.execute(
        "SELECT COUNT(*) c FROM issue_comments WHERE gh_comment_id=501"
    ).fetchone()["c"] == 1


def test_serve_issue_comments_capture_upsert_idempotent(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        for body in ("v1", "v2 edited"):
            r = cli.post("/issue-comments", json={
                "action": "capture", "repo_name": "acme/api", "issue_number": 7,
                "body": body, "gh_comment_id": 502,
            })
            assert r.status_code == 200
    rows = rw_db.execute(
        "SELECT body FROM issue_comments WHERE gh_comment_id=502"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["body"] == "v2 edited"


def test_serve_issue_comments_sync(file_db: Path, valid_config_path: Path, rw_db, monkeypatch):
    from coord import github_ops

    monkeypatch.setattr(
        github_ops, "get_issue_comments",
        lambda *a, **k: [{
            "url": "https://github.com/acme/api/issues/7#issuecomment-9",
            "body": "hi", "author": {"login": "x"}, "createdAt": "2026-07-02T00:00:00Z",
        }],
    )
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        r = cli.post("/issue-comments", json={
            "action": "sync", "repo_name": "api", "issue_number": 7,
            "repo_github": "acme/api",
        })
        assert r.status_code == 200
        assert r.json() == {"synced": 1}
    assert rw_db.execute(
        "SELECT COUNT(*) c FROM issue_comments WHERE gh_comment_id=9"
    ).fetchone()["c"] == 1


def test_serve_issue_comments_unknown_action_400(file_db: Path, valid_config_path: Path, rw_db):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/issue-comments", json={
            "action": "bogus", "repo_name": "api", "issue_number": 7,
        })
    assert resp.status_code == 400


def test_serve_issue_comments_missing_field_400(file_db: Path, valid_config_path: Path, rw_db):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/issue-comments", json={"action": "capture", "repo_name": "api"})
    assert resp.status_code == 400


def test_serve_issue_comments_get_missing_params_400(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.get("/issue-comments", params={"repo_name": "api"})
    assert resp.status_code == 400


def test_add_issue_context_entry_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"entry_id": 42},
    )
    assert state.add_issue_context_entry("api", 7, "x", pinned=True) == 42
    assert captured["path"] == "/issue-context"
    assert captured["payload"]["action"] == "add" and captured["payload"]["pinned"] is True
    # Routed → no local row created.
    assert coord_db.execute("SELECT COUNT(*) c FROM issue_context").fetchone()["c"] == 0


def test_list_issue_context_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    monkeypatch.setattr(
        cc, "fetch_issue_context",
        lambda svc, repo, num: [{"id": 1, "pinned": True, "source": None,
                                 "body": "remote note", "created_at": 1.0}],
    )
    assert state.list_issue_context("api", 7)[0]["body"] == "remote note"


def test_add_issue_context_entry_blank_is_noop(coord_db):
    from coord import state
    assert state.add_issue_context_entry("api", 7, "   ") is None
    assert coord_db.execute("SELECT COUNT(*) c FROM issue_context").fetchone()["c"] == 0


def test_render_issue_context_entries_pins_first_then_newest_then_budget():
    from coord import state
    entries = [
        {"id": 1, "pinned": True, "source": "operator", "body": "PIN dep #99", "created_at": 1.0},
        {"id": 2, "pinned": False, "source": "test", "body": "old note", "created_at": 2.0},
        {"id": 3, "pinned": False, "source": "work", "body": "new note", "created_at": 3.0},
    ]
    out = state.render_issue_context_entries(entries)
    lines = out.splitlines()
    assert lines[0].startswith("- 📌 PIN dep #99")  # pinned first
    assert "new note" in lines[1] and "old note" in lines[2]  # newest-first notes
    # Budget: 1 pin + 1 note slot → oldest non-pinned trimmed with a marker.
    capped = state.render_issue_context_entries(entries, max_entries=2)
    assert "PIN dep #99" in capped and "new note" in capped
    assert "old note" not in capped and "trimmed" in capped
    assert state.render_issue_context_entries([]) == ""


def test_issue_context_dropped_on_close(coord_db):
    from coord import state
    state._add_issue_context_entry_local("api", 7, "ctx for closing issue", pinned=True)
    state._add_issue_context_entry_local("api", 8, "ctx for issue staying open")
    coord_db.execute(
        "INSERT INTO issues(repo_name,number,state,synced_at) VALUES('api',8,'open',0)"
    )
    coord_db.commit()
    # #7 absent from the open set → closed → its context dropped; #8 kept.
    state._upsert_open_issues_local("api", [{"number": 8, "title": "t", "body": "", "labels": []}])
    assert state._list_issue_context_local("api", 7) == []
    assert len(state._list_issue_context_local("api", 8)) == 1


def test_record_test_verdict_local_appends_context(coord_db):
    # #603: a test FAILURE auto-appends a durable context entry (source=test).
    from coord import state
    coord_db.execute(
        "INSERT INTO assignments(assignment_id,machine_name,repo_name,issue_number,"
        "issue_title,status,type) VALUES('w1','m','api',7,'t','done','work')"
    )
    coord_db.commit()
    state._record_test_verdict_local(assignment_id="w1", test_state="failed", test_reason="boom")
    ents = state._list_issue_context_local("api", 7)
    assert len(ents) == 1 and ents[0]["source"] == "test"
    assert "Test FAILED: boom" in ents[0]["body"]
    # A pass adds nothing.
    state._record_test_verdict_local(assignment_id="w1", test_state="passed")
    assert len(state._list_issue_context_local("api", 7)) == 1


# ── #1384: the legacy smoke_test mirror is DERIVED when not supplied ──────────


def _seed_verdict_row(conn, aid: str) -> None:
    conn.execute(
        "INSERT INTO assignments(assignment_id,machine_name,repo_name,issue_number,"
        f"issue_title,status,type) VALUES('{aid}','m','api',7,'t','done','work')"
    )
    conn.commit()


def _verdict_cols(conn, aid: str):
    return conn.execute(
        "SELECT test_state, smoke_test, smoke_test_reason FROM assignments "
        "WHERE assignment_id=?",
        (aid,),
    ).fetchone()


def test_verdict_writer_derives_smoke_fail_when_not_supplied(coord_db):
    """#1384: test_state='failed' with no smoke_test= mirrors to 'fail'.

    This is the #1021 headless-smoke call shape (``coord/notify.py``): it
    passes no ``smoke_test=``.  Without derivation the row lands
    ``smoke_test=NULL`` and ``coord fix`` refuses it — the headless
    fail→fix path is a dead end.
    """
    from coord import state
    _seed_verdict_row(coord_db, "hs1")

    state._record_test_verdict_local(
        assignment_id="hs1", test_state="failed", test_reason="headless smoke"
    )

    row = _verdict_cols(coord_db, "hs1")
    assert row["test_state"] == "failed"
    assert row["smoke_test"] == "fail", "the legacy mirror must be derived"
    assert row["smoke_test_reason"] == "headless smoke"


def test_verdict_writer_derives_smoke_pass_when_not_supplied(coord_db):
    """#1384: test_state='passed' with no smoke_test= mirrors to 'pass'."""
    from coord import state
    _seed_verdict_row(coord_db, "hs2")

    state._record_test_verdict_local(
        assignment_id="hs2", test_state="passed", test_reason="headless smoke"
    )

    row = _verdict_cols(coord_db, "hs2")
    assert row["test_state"] == "passed"
    assert row["smoke_test"] == "pass"
    # A pass carries no smoke reason (matches `coord test --passed`).
    assert row["smoke_test_reason"] is None


def test_verdict_writer_leaves_mirror_null_for_skipped(coord_db):
    """#1384: 'skipped' leaves smoke_test NULL — same as `coord test --skipped`.

    review.py's #1076/#1152 Gate-A auto-skip relies on this: `coord fix` only
    gates on a FAILED verdict, so a skip must not manufacture a mirror.
    """
    from coord import state
    _seed_verdict_row(coord_db, "hs3")

    state._record_test_verdict_local(
        assignment_id="hs3", test_state="skipped", test_reason="nothing to smoke"
    )

    row = _verdict_cols(coord_db, "hs3")
    assert row["test_state"] == "skipped"
    assert row["smoke_test"] is None


def test_verdict_writer_explicit_mirror_still_wins(coord_db):
    """#1384: an explicitly supplied mirror is used verbatim (no derivation).

    The `coord test` CLI / dashboard / interactive-review paths all pass
    the mirror explicitly — derivation must not change their behaviour.
    """
    from coord import state
    _seed_verdict_row(coord_db, "hs4")

    state._record_test_verdict_local(
        assignment_id="hs4",
        test_state="failed",
        test_reason="long story",
        smoke_test="fail",
        smoke_test_reason="short reason",
    )

    row = _verdict_cols(coord_db, "hs4")
    assert row["smoke_test"] == "fail"
    assert row["smoke_test_reason"] == "short reason"


def test_serve_test_verdict_derives_mirror_without_smoke_test(
    file_db: Path, valid_config_path: Path, rw_db
):
    """#1384: the daemon /test-verdict route derives the mirror too.

    A thin client (or anything POSTing the endpoint directly) that omits
    ``smoke_test`` must land the same columns as the local writer.
    """
    _seed_running_assignment(rw_db, aid="work79")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/test-verdict",
            json={
                "assignment_id": "work79",
                "test_state": "failed",
                "test_reason": "headless smoke",
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT test_state, smoke_test FROM assignments WHERE assignment_id='work79'"
    ).fetchone()
    assert row["test_state"] == "failed"
    assert row["smoke_test"] == "fail"


def test_post_result_request_changes_appends_context(coord_db, monkeypatch):
    # #603: a request-changes verdict auto-appends a context entry (source=review).
    from coord import issue_store, state
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **k: None)
    coord_db.execute(
        "INSERT INTO assignments(assignment_id,machine_name,repo_name,issue_number,"
        "issue_title,status,type) VALUES('rev1','m','api',7,'t','running','review')"
    )
    coord_db.commit()
    issue_store._post_result_local(issue_store.ResultRecord(
        assignment_id="rev1", machine_name="m", repo_name="api", repo_github="o/api",
        issue_number=7, status="done", verdict="request-changes",
        findings_body="must set is_keyboard_focused", summary="",
    ))
    ents = state._list_issue_context_local("api", 7)
    assert any(e["source"] == "review" and "is_keyboard_focused" in e["body"] for e in ents)


def test_cli_context_add_show_clear(coord_db):
    # #603: the operator-facing `coord context` round-trip.
    from click.testing import CliRunner
    from coord.cli import main
    r = CliRunner()
    out = r.invoke(main, ["context", "add", "api", "7", "depends on lib #9", "--pin"])
    assert out.exit_code == 0 and "added" in out.output
    out = r.invoke(main, ["context", "show", "api", "7"])
    assert out.exit_code == 0 and "depends on lib #9" in out.output and "📌" in out.output
    out = r.invoke(main, ["context", "clear", "api", "7"])
    assert out.exit_code == 0 and "cleared 1" in out.output


def test_cli_context_curate_replaces_entries(coord_db, monkeypatch):
    # #603 Phase 4: curate compresses via claude -p and replaces the entries.
    from coord import state
    for i in range(5):
        state._add_issue_context_entry_local("api", 7, f"note {i}", pinned=(i == 0))
    fake = '```json\n[{"body":"merged critical dep","pinned":true},' \
           '{"body":"one lesson kept","pinned":false}]\n```'
    monkeypatch.setattr("coord.test_orchestrator._call_claude", lambda *a, **k: fake)
    from click.testing import CliRunner
    from coord.cli import main
    out = CliRunner().invoke(main, ["context", "curate", "api", "7"])
    assert out.exit_code == 0 and "5 → 2" in out.output
    ents = state._list_issue_context_local("api", 7)
    assert len(ents) == 2
    assert ents[0]["body"] == "merged critical dep" and ents[0]["pinned"] is True
    assert all(e["source"] == "curated" for e in ents)


def test_cli_context_curate_noop_when_few(coord_db, monkeypatch):
    from coord import state
    state._add_issue_context_entry_local("api", 7, "only note")
    called = []
    monkeypatch.setattr("coord.test_orchestrator._call_claude",
                        lambda *a, **k: called.append(1) or "[]")
    from click.testing import CliRunner
    from coord.cli import main
    out = CliRunner().invoke(main, ["context", "curate", "api", "7"])
    assert out.exit_code == 0 and "nothing to curate" in out.output
    assert called == []  # no metered call for a tiny digest


def test_cli_fix_briefing_includes_context_and_test_story(coord_db, valid_config_path):
    # #603 Phase 5: `coord fix-briefing` prints the context block + the resolved
    # test-failure story (the exact-briefing preview the TUI dialog shows).
    # Pass --config explicitly: coordinator.yml is NOT checked in (gitignored
    # dev config), so the default relative path only resolves when cwd happens
    # to hold a local one — it does on a dev box, but not in CI's fresh
    # checkout, which left this test red on every push since v0.4.40.
    from coord import state
    coord_db.execute(
        "INSERT INTO assignments(assignment_id,machine_name,repo_name,issue_number,"
        "issue_title,status,type,branch,test_state,test_reason) VALUES"
        "('w1','laptop','claude-coordinator',7,'Fix X','done','work','issue-7-x',"
        "'failed','Button does nothing on click')"
    )
    coord_db.commit()
    state._add_issue_context_entry_local(
        "claude-coordinator", 7, "depends on quadraui #368", pinned=True
    )
    from click.testing import CliRunner
    from coord.cli import main
    out = CliRunner().invoke(
        main, ["fix-briefing", "w1", "--config", str(valid_config_path)]
    )
    assert out.exit_code == 0, out.output
    assert "⚠️ Issue context" in out.output  # context block at the top
    assert "depends on quadraui #368" in out.output
    assert "Button does nothing on click" in out.output  # the resolved test story


def test_serve_merge_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # #584: POST /merge runs `coord merge` on the daemon with the recursion
    # guard set, and relays the captured CLI output + exit code.
    import os
    import click
    from coord.cli import merge as merge_cmd

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_MERGE_ON_DAEMON") == "1"  # guard set
        click.echo(f"merged dry_run={kwargs['dry_run']} method={kwargs['method']}")

    monkeypatch.setattr(merge_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/merge", json={"dry_run": True, "method": "squash"})
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert "merged dry_run=True method=squash" in out["output"]
    assert os.environ.get("COORD_MERGE_ON_DAEMON") is None  # restored after


def test_serve_merge_relays_nonzero_exit(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    import sys
    from coord.cli import merge as merge_cmd
    monkeypatch.setattr(merge_cmd, "callback", lambda **k: sys.exit(2))
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/merge", json={})
    assert resp.json()["exit_code"] == 2


def test_serve_merge_relays_traceback_not_bare_message(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch, caplog
):
    """#1353: an unhandled exception from the merge callback used to reach
    the client as a bare ``str(e)`` — e.g. exactly
    ``"Expecting value: line 1 column 1 (char 0)"`` for a ``JSONDecodeError``
    — with no frame, and nothing logged daemon-side either (the incident
    this issue reports left a bare "200 OK" in the journal for the failing
    request). The handler must now put the *traceback* in the client-facing
    ``error`` field, and log the exception (with a frame) daemon-side, so a
    future incident is attributable from either artifact."""
    import json as _json
    from coord.cli import merge as merge_cmd

    def fake_callback(**kwargs):
        raise _json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(merge_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with caplog.at_level("ERROR", logger="coord.serve"):
        with TestClient(app) as cli:
            resp = cli.post("/merge", json={})
    out = resp.json()
    assert out["exit_code"] == 1
    # A bare str(e) would be exactly "Expecting value: line 1 column 1 (char 0)"
    # with no other content -- assert the traceback frame is present too.
    assert "Traceback (most recent call last)" in out["error"]
    assert "JSONDecodeError" in out["error"]
    assert "fake_callback" in out["error"]
    # And the daemon's own journal retains a logged frame, not just the
    # request's 200 OK.
    assert any(
        rec.name == "coord.serve" and rec.exc_info for rec in caplog.records
    )


def test_serve_merge_rejects_client_skip_review(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """#821/#1489: POST /merge with skip_review=True must be rejected outright,
    not silently stripped.

    The daemon still always enforces the review gate regardless of any flag
    the thin client sends (#821 invariant, unchanged) — but it used to do so
    by quietly forcing skip_review=False into the merge callback with no
    signal back to the caller, so an operator's --skip-review looked
    accepted and then did nothing (#1489). Verify: the callback is never
    invoked at all, the response carries a non-zero exit_code and an
    explicit error mentioning the flag, and the review gate itself is still
    unconditionally enforced.
    """
    from coord.cli import merge as merge_cmd

    captured: dict = {}

    def fake_callback(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(merge_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/merge", json={"skip_review": True, "dry_run": True})
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 1
    assert "skip-review" in out["error"]
    assert "#821" in out["error"]
    # The merge pipeline must never have run — this is a rejection, not a
    # stripped-and-proceed no-op.
    assert captured == {}


def test_merge_command_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    # #584: `coord merge` on a thin client POSTs to /merge and relays the output,
    # instead of no-opping against the empty local board.
    from coord import client as cc
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON MERGE OUTPUT\n", "exit_code": 0},
    )
    out = CliRunner().invoke(main, ["merge", "--dry-run", "--repo", "api"])
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/merge"
    assert captured["payload"]["dry_run"] is True
    assert captured["payload"]["repo_filter"] == "api"
    assert "DAEMON MERGE OUTPUT" in out.output


def test_serve_reconcile_merges_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # #584: POST /reconcile-merges runs `coord reconcile-merges` on the daemon
    # with the recursion guard set, and relays the captured CLI output + code.
    import os
    import click
    from coord.cli import reconcile_merges as reconcile_cmd

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_RECONCILE_ON_DAEMON") == "1"  # guard set
        click.echo(
            f"reconciled dry_run={kwargs['dry_run']} repo={kwargs['repo_name']}"
        )

    monkeypatch.setattr(reconcile_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/reconcile-merges", json={"dry_run": True, "repo": "api"})
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert "reconciled dry_run=True repo=api" in out["output"]
    assert os.environ.get("COORD_RECONCILE_ON_DAEMON") is None  # restored after


def test_serve_reconcile_merges_relays_nonzero_exit(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    import sys
    from coord.cli import reconcile_merges as reconcile_cmd
    monkeypatch.setattr(reconcile_cmd, "callback", lambda **k: sys.exit(2))
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/reconcile-merges", json={})
    assert resp.json()["exit_code"] == 2


def test_reconcile_merges_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    # #584: `coord reconcile-merges` on a thin client POSTs to /reconcile-merges
    # and relays the output, instead of no-opping against the empty local board.
    from coord import client as cc
    from coord import cli as coord_cli
    from coord import state as coord_state
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    # routing happens before any local-board work — assert build_board never runs
    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("build_board must not be called on a thin client")

    monkeypatch.setattr(coord_state, "build_board", _boom, raising=False)
    monkeypatch.setattr(coord_state, "save_board", _boom, raising=False)
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON RECONCILE OUTPUT\n", "exit_code": 0},
    )
    out = CliRunner().invoke(main, ["reconcile-merges", "--dry-run", "--repo", "api"])
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/reconcile-merges"
    assert captured["payload"]["dry_run"] is True
    assert captured["payload"]["repo"] == "api"
    assert "DAEMON RECONCILE OUTPUT" in out.output


def test_serve_diagnose_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # POST /diagnose runs `coord diagnose` on the daemon with the recursion
    # guard set, and relays the captured CLI output + exit code.
    import os
    import click
    from coord.cli import diagnose as diagnose_cmd

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_DIAGNOSE_ON_DAEMON") == "1"  # guard set
        click.echo(
            f"diagnosed repo={kwargs['repo']} issue={kwargs['issue']} "
            f"stage={kwargs['stage']} reset={kwargs['reset']}"
        )

    monkeypatch.setattr(diagnose_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/diagnose",
            json={"repo": "api", "issue": 42, "stage": "review", "reset": False},
        )
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert "diagnosed repo=api issue=42 stage=review reset=False" in out["output"]
    assert os.environ.get("COORD_DIAGNOSE_ON_DAEMON") is None  # restored after


def test_serve_diagnose_real_callback_no_orphan_worktrees_crash(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # Regression: POST /diagnose used to raise
    #   TypeError: diagnose() missing 1 required positional argument: 'orphan_worktrees'
    # because serve_app.post_diagnose called diagnose_cmd.callback(...) without
    # passing the orphan_worktrees kwarg.  This test drives the REAL callback
    # (no monkeypatching of .callback) and should FAIL without the serve_app fix.
    from coord import client as cc
    from coord import state as coord_state
    from coord.diagnose import DiagnoseResult
    from coord.models import Board

    # Route to local path (COORD_DIAGNOSE_ON_DAEMON guard takes over inside the
    # endpoint, but resolve_board_service must return None so the callback doesn't
    # try to route again before the guard is set).
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr(coord_state, "build_board", lambda: Board())
    monkeypatch.setattr(
        "coord.diagnose.diagnose_stage",
        lambda *a, **k: DiagnoseResult(
            repo_name="api", issue_number=42, stage="work", recovered=False
        ),
    )

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/diagnose", json={"repo": "api", "issue": 42})

    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0, f"expected exit_code=0, got: {out}"
    assert out["error"] is None, f"expected no error, got: {out['error']}"
    assert "missing" not in (out["error"] or "")
    assert "positional argument" not in (out["error"] or "")


def test_serve_diagnose_relays_nonzero_exit(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    import sys
    from coord.cli import diagnose as diagnose_cmd
    monkeypatch.setattr(diagnose_cmd, "callback", lambda **k: sys.exit(2))
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/diagnose", json={"repo": "api", "issue": 1})
    assert resp.json()["exit_code"] == 2


def test_diagnose_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    # `coord diagnose` on a thin client POSTs to /diagnose and relays the
    # output, instead of no-opping against the empty local board.
    from coord import client as cc
    from coord import state as coord_state
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("build_board must not be called on a thin client")

    monkeypatch.setattr(coord_state, "build_board", _boom, raising=False)
    monkeypatch.setattr(coord_state, "save_board", _boom, raising=False)
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON DIAGNOSE OUTPUT\n", "exit_code": 0},
    )
    out = CliRunner().invoke(
        main, ["diagnose", "api", "42", "--stage", "review", "--reset"]
    )
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/diagnose"
    assert captured["payload"]["repo"] == "api"
    assert captured["payload"]["issue"] == 42
    assert captured["payload"]["stage"] == "review"
    assert captured["payload"]["reset"] is True
    assert "DAEMON DIAGNOSE OUTPUT" in out.output


def test_serve_gates_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # POST /gates runs `coord gates` on the daemon with the recursion guard
    # set, and relays the captured CLI output + exit code. Mirrors
    # test_serve_diagnose_runs_callback_and_captures_output.
    import os
    import click
    from coord.cli import gates as gates_cmd

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_GATES_ON_DAEMON") == "1"  # guard set
        click.echo(
            f"gated repo={kwargs['repo']} issue={kwargs['issue']} "
            f"as_json={kwargs['as_json']}"
        )

    monkeypatch.setattr(gates_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/gates",
            json={"repo": "api", "issue": 42, "as_json": False},
        )
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert "gated repo=api issue=42 as_json=False" in out["output"]
    assert os.environ.get("COORD_GATES_ON_DAEMON") is None  # restored after


def test_serve_gates_real_callback_end_to_end(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # Drives the REAL callback (no monkeypatching of .callback) so a kwarg
    # mismatch between serve_app.post_gates and coord.cli.gates's signature
    # (the exact #diagnose regression this mirrors) would fail loudly here.
    from coord import client as cc
    from coord import github_ops

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr(github_ops, "get_branch_sha", lambda *a, **k: None)
    monkeypatch.setattr(github_ops, "get_branch_patch_id", lambda *a, **k: None)

    rw_db.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type, branch, "
        "test_state, review_state, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work1", "laptop", "api", "acme/api", 7, "An issue", "done",
            "work", "issue-7-foo", "passed", "done", "approve",
        ),
    )
    # A real "review" row is what has_approved_review actually looks for —
    # review_verdict mirrored onto the work row alone isn't evidence.
    rw_db.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type, "
        "review_of_assignment_id, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "review1", "server", "api", "acme/api", 7, "An issue", "done",
            "review", "work1", "approve",
        ),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/gates", json={"repo": "api", "issue": 7, "as_json": True})

    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0, f"expected exit_code=0, got: {out}"
    assert out["error"] is None, f"expected no error, got: {out['error']}"
    payload = json.loads(out["output"])
    assert payload["repo_name"] == "api"
    assert payload["issue_number"] == 7
    assert payload["decisions"][-1]["gate"] == "merge"
    assert payload["decisions"][-1]["ok"] is True


def test_serve_gates_relays_nonzero_exit(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    import sys
    from coord.cli import gates as gates_cmd
    monkeypatch.setattr(gates_cmd, "callback", lambda **k: sys.exit(2))
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/gates", json={"repo": "api", "issue": 1, "as_json": False})
    assert resp.json()["exit_code"] == 2


def test_gates_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    # `coord gates` on a thin client POSTs to /gates and relays the output,
    # instead of no-opping against an empty local board (no live gh either).
    from coord import client as cc
    from coord import state as coord_state
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("build_board must not be called on a thin client")

    monkeypatch.setattr(coord_state, "build_board", _boom, raising=False)
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON GATES OUTPUT\n", "exit_code": 0},
    )
    out = CliRunner().invoke(main, ["gates", "api", "42", "--json"])
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/gates"
    assert captured["payload"]["repo"] == "api"
    assert captured["payload"]["issue"] == 42
    assert captured["payload"]["as_json"] is True
    assert "DAEMON GATES OUTPUT" in out.output


def test_serve_acceptance_record_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # #944: POST /acceptance-record runs `coord acceptance record` on the
    # daemon with the recursion guard set, and relays the captured CLI
    # output + exit code. Mirrors test_serve_diagnose_runs_callback_and_captures_output.
    import os
    import click
    from coord.commands.acceptance import acceptance_record

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_ACCEPTANCE_ON_DAEMON") == "1"  # guard set
        click.echo(
            f"recorded repo={kwargs['repo']} issue={kwargs['issue_number']} "
            f"sha={kwargs['sha']}"
        )

    monkeypatch.setattr(acceptance_record, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/acceptance-record",
            json={"repo": "api", "issue": 944, "sha": "deadbeef"},
        )
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert "recorded repo=api issue=944 sha=deadbeef" in out["output"]
    assert os.environ.get("COORD_ACCEPTANCE_ON_DAEMON") is None  # restored after


def test_serve_acceptance_record_relays_nonzero_exit(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    import sys
    from coord.commands.acceptance import acceptance_record

    monkeypatch.setattr(acceptance_record, "callback", lambda **k: sys.exit(1))
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/acceptance-record",
            json={"repo": "api", "issue": 944, "sha": "deadbeef"},
        )
    assert resp.json()["exit_code"] == 1


def test_serve_acceptance_record_relays_stderr_failure(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """#1733: every LOCAL failure path in ``coord acceptance record``
    (a DriverError from a fresh worktree with no ``node_modules``, a missing
    work assignment, a manifest error, ...) does ``click.echo(f"error:
    {e}", err=True)`` before ``sys.exit(1)`` — that resolves ``sys.stderr``
    fresh at call time, so without capturing stderr too (only
    ``stdout_proxy`` was wrapped before this fix) that message vanished into
    the daemon's own journal and never reached the client. A daemon-routed
    `coord acceptance record` then exited 1 with a totally empty
    output/error — indistinguishable from a hang, exactly the #1733 repro
    (mirrors #1251's identical /merge bug, see
    test_serve_merge_relays_stderr_usage_errors). Assert the message now
    survives the relay.
    """
    import sys
    import click
    from coord.commands.acceptance import acceptance_record

    def fake_callback(**kwargs):
        click.echo(
            "error: web-playwright run wrote no report (exit 127): boom",
            err=True,
        )
        sys.exit(1)

    monkeypatch.setattr(acceptance_record, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/acceptance-record",
            json={"repo": "api", "issue": 944, "sha": "deadbeef"},
        )
    body = resp.json()
    assert body["exit_code"] == 1
    assert "wrote no report" in body["output"], (
        f"stderr failure message did not reach the client: {body!r}"
    )


def test_acceptance_record_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    # `coord acceptance record` on a thin client POSTs to /acceptance-record
    # and relays the output, instead of trying to run against an empty local
    # board / missing repo checkout.
    from coord import client as cc
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON ACCEPTANCE RECORD OUTPUT\n", "exit_code": 0},
    )
    out = CliRunner().invoke(main, [
        "acceptance", "record", "--repo", "api", "--issue", "944", "--sha", "deadbeef",
    ])
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/acceptance-record"
    assert captured["payload"]["repo"] == "api"
    assert captured["payload"]["issue"] == 944
    assert captured["payload"]["sha"] == "deadbeef"
    assert "DAEMON ACCEPTANCE RECORD OUTPUT" in out.output


def test_acceptance_record_daemon_failure_exits_nonzero_and_prints_reason(
    coord_db, monkeypatch
):
    """#1733 client-side half of the fix: a daemon-routed `coord acceptance
    record` failure must not just exit non-zero — it must print WHY on the
    client. Before this, the daemon-relayed payload for a driver crash
    (DriverError from a fresh worktree missing node_modules) carried the
    reason only in ``output`` (stderr wasn't captured server-side — see
    test_serve_acceptance_record_relays_stderr_failure), so a thin client
    saw exit 1 with nothing printed at all — indistinguishable from a hang.
    This asserts the CLI's own stdout (not just the exit code) carries the
    reason once the daemon relays it correctly.
    """
    from coord import client as cc
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: {
            "output": (
                "error: web-playwright run wrote no report (exit 127): "
                "playwright: not found\n"
            ),
            "exit_code": 1,
            "error": None,
        },
    )
    out = CliRunner().invoke(main, [
        "acceptance", "record", "--repo", "api", "--issue", "944", "--sha", "deadbeef",
    ])
    assert out.exit_code == 1
    assert "wrote no report" in out.output, (
        f"daemon failure reason did not reach client-visible output: {out.output!r}"
    )


def test_serve_test_plan_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # #851: POST /test-plan runs `coord test-plan` on the daemon with the
    # recursion guard set, and relays the captured CLI output + exit code.
    # Mirrors test_serve_diagnose_runs_callback_and_captures_output.
    import os
    import click
    from coord.cli import test_plan_cmd

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_TEST_PLAN_ON_DAEMON") == "1"  # guard set
        click.echo(
            f"test-planned assignment_id={kwargs['assignment_id']} "
            f"refresh={kwargs['refresh']} model={kwargs['model']}"
        )

    monkeypatch.setattr(test_plan_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/test-plan",
            json={"assignment_id": "abc123", "refresh": True, "model": "haiku"},
        )
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert (
        "test-planned assignment_id=abc123 refresh=True model=haiku" in out["output"]
    )
    assert os.environ.get("COORD_TEST_PLAN_ON_DAEMON") is None  # restored after


def test_test_plan_routes_to_daemon_when_service_set(coord_db, tmp_path, monkeypatch):
    # #851: `coord test-plan` on a thin client POSTs to /test-plan and relays
    # the output, instead of reporting "not found" against its empty local
    # DB (generate_plan queries the local DB directly and has no daemon-
    # routing of its own). Mirrors test_diagnose_routes_to_daemon_when_service_set.
    from coord import client as cc
    from coord import test_orchestrator
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("generate_plan must not run locally on a thin client")

    monkeypatch.setattr(test_orchestrator, "generate_plan", _boom, raising=False)
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON TEST-PLAN OUTPUT\n", "exit_code": 0},
    )
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos:\n  - name: api\n    github: acme/api\nmachines: []\n")
    out = CliRunner().invoke(
        main,
        ["test-plan", "abc123", "--refresh", "--model", "sonnet", "--config", str(cfg)],
    )
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/test-plan"
    assert captured["payload"] == {
        "assignment_id": "abc123", "refresh": True, "model": "sonnet",
    }
    assert "DAEMON TEST-PLAN OUTPUT" in out.output


def test_test_plan_generation_on_daemon_uses_resolved_claude_path(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """#859: on a cache miss, daemon-side plan generation must invoke the
    resolved ABSOLUTE `claude` path, not bare 'claude' — coord-serve runs
    under systemd --user with a PATH that lacks ~/.local/bin (where the
    binary actually lives), so a bare-name subprocess call fails there even
    though it works from an interactive shell.

    Full round-trip: a thin client's `coord test-plan` (board_service set)
    POSTs to /test-plan; `post_record` is routed into the real Starlette app
    via TestClient (in-process, no live HTTP) so `test_plan_cmd.callback` runs
    for real against `rw_db` and calls the real (unmocked) `generate_plan` →
    `_call_claude`. Only the network/gh/subprocess leaves are stubbed:
    artifact manifest, PR diff, issue body (mirrors TestGeneratePlan's
    mocking) — and `shutil.which`/`subprocess.run` inside `_call_claude`,
    which is exactly what's under test.
    """
    import json
    from unittest.mock import MagicMock

    from click.testing import CliRunner

    from coord import client as cc
    from coord import test_orchestrator
    from coord.cli import main

    _seed_running_assignment(rw_db, aid="work9")

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    daemon_client = TestClient(app)

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    # #1080: _load_config now always fetches on a thin client (never trusts a
    # local file that happens to exist), so stand in for the daemon's /config
    # with the same coordinator.yml already loaded into `app` above.
    monkeypatch.setattr(cc, "fetch_remote_config", lambda svc, **kw: valid_config_path)
    # Route the thin-client POST into the real daemon endpoint in-process
    # instead of over a live HTTP socket.
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: daemon_client.post(path, json=payload).json(),
    )

    # generate_plan's non-claude side calls (gh/network) — stub them out so
    # the only real subprocess left is `_call_claude`'s `claude -p` call.
    monkeypatch.setattr(test_orchestrator, "_fetch_artifact_manifest", lambda *a, **k: None)
    monkeypatch.setattr(test_orchestrator, "_get_pr_diff", lambda *a, **k: "")
    monkeypatch.setattr(test_orchestrator, "_get_issue_body", lambda *a, **k: "")

    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(test_orchestrator.shutil, "which", lambda name: None)  # not on PATH

    captured_cmd: list = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        captured_cmd.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps({"result": json.dumps({"steps": [], "blockers": []})})
        result.stderr = ""
        return result

    monkeypatch.setattr(test_orchestrator.subprocess, "run", fake_run)

    out = CliRunner().invoke(
        main, ["test-plan", "work9", "--config", str(valid_config_path)]
    )
    assert out.exit_code == 0, out.output
    assert len(captured_cmd) == 1, "expected exactly one claude -p subprocess call"
    resolved = captured_cmd[0][0]
    assert resolved != "claude", "must not shell out to bare 'claude' (#859)"
    assert resolved == str(Path.home() / ".local" / "bin" / "claude")
    assert '"steps": []' in out.output


def test_log_falls_back_to_daemon_board_machine_name(coord_db, tmp_path, monkeypatch):
    # #851: `coord log` on a thin client (or any machine that isn't the
    # dispatcher) has no local dispatched-ledger record for a valid remote
    # assignment id and no local log file. Before this fix that fell through
    # to "no log found" and made a healthy id look broken; now it asks the
    # daemon board for the assignment's own machine_name so the operator
    # doesn't have to guess --machine.
    from unittest.mock import patch

    from coord import agent as agent_mod
    from coord import client as cc
    from click.testing import CliRunner
    from coord.cli import main

    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    repos: [api]\n"
        "  - name: server\n"
        "    host: server.tailnet\n"
        "    repos: [api]\n"
    )

    # No local log for this assignment on this machine.
    monkeypatch.setattr(agent_mod, "DEFAULT_STATE_DIR", tmp_path / "state")

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    # #1080: _load_config now always fetches on a thin client (never trusts a
    # local file that happens to exist), so stand in for the daemon's /config
    # with the same coordinator.yml this test already wrote to cfg.
    monkeypatch.setattr(cc, "fetch_remote_config", lambda svc, **kw: cfg)
    monkeypatch.setattr(
        cc,
        "fetch_board_payload",
        lambda svc, **kw: {
            "assignments": [
                {
                    "assignment_id": "remote-only",
                    "machine_name": "server",
                    "repo_name": "api",
                    "status": "done",
                },
            ]
        },
    )

    with patch(
        "coord.network.fetch_log",
        return_value=(200, b"remote log content via daemon board\n"),
    ):
        result = CliRunner().invoke(
            main, ["log", "remote-only", "--config", str(cfg)]
        )

    assert result.exit_code == 0, result.output
    assert "remote log content via daemon board" in result.output


def test_diagnose_cli_never_calls_save_board(valid_config_path: Path, coord_db, monkeypatch):
    # Regression (quadraui #366): the diagnose command must persist ONLY through
    # the issue_store seam (finalize→post_completion, recover→post_result,
    # reconcile→state.update_*).  A save_board would write the STALE in-memory
    # snapshot and clobber those seam writes — flipping a just-finalized phantom
    # back to 'running'.  So save_board must NEVER be called by diagnose.
    from coord import client as cc
    from coord import state as state_mod
    from coord.cli import diagnose as diagnose_cmd
    from coord.diagnose import DiagnoseResult
    from coord.models import Board

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)  # local path
    monkeypatch.setattr(state_mod, "build_board", lambda: Board())
    monkeypatch.setattr(
        "coord.diagnose.diagnose_stage",
        lambda *a, **k: DiagnoseResult(
            repo_name="api", issue_number=42, stage="work", recovered=True
        ),
    )

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("diagnose must not save_board (it clobbers seam writes)")

    monkeypatch.setattr(state_mod, "save_board", _boom, raising=False)
    # Should complete without ever touching save_board.
    diagnose_cmd.callback(
        repo="api", issue=42, stage="work", reset=False, dry_run=False,
        config_path=valid_config_path,
        orphan_worktrees=False,  # #618: new flag; default False for this test
    )


def test_resolve_serve_token_precedence(tmp_path: Path, monkeypatch):
    from coord import serve_app

    tok_file = tmp_path / "serve_token"
    monkeypatch.setattr(serve_app, "SERVE_TOKEN_FILE", tok_file)
    monkeypatch.delenv("COORD_SERVE_TOKEN", raising=False)

    # nothing configured → open daemon
    assert serve_app.resolve_serve_token() is None
    # file source (what systemd uses), trailing whitespace stripped
    tok_file.write_text("filetok\n")
    assert serve_app.resolve_serve_token() == "filetok"
    # env beats file
    monkeypatch.setenv("COORD_SERVE_TOKEN", "envtok")
    assert serve_app.resolve_serve_token() == "envtok"
    # flag beats env; blank flag is treated as unset (falls through)
    assert serve_app.resolve_serve_token("flagtok") == "flagtok"
    assert serve_app.resolve_serve_token("   ") == "envtok"


def test_post_result_unset_writes_local(coord_db, monkeypatch):
    """board_service unset → unchanged local-DB write (no regression)."""
    from coord import client as cc
    from coord import issue_store

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **k: None)
    _seed_running_assignment(coord_db, aid="work13")
    issue_store.post_result(
        issue_store.ResultRecord(
            assignment_id="work13", machine_name="laptop", repo_name="api",
            repo_github="owner/api", issue_number=7, status="done",
            verdict=None, summary="ok",
        )
    )
    row = coord_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='work13'"
    ).fetchone()
    assert row["status"] == "done"


# ── Passive tick (#736 / #217): daemon enqueues approved work on every interval ──


def _seed_approved_done_work(conn, *, aid: str = "work99", branch: str = "issue-7-impl") -> None:
    """Seed an approved + test-passed done work assignment into the shared DB.

    Inserts:
    - A done work assignment on *branch* with ``test_state='passed'``.
    - A done review assignment pointing at it with ``review_verdict='approve'``.

    After these rows are present, ``build_board()`` will include them in
    ``board.completed`` and ``enqueue_approved_work`` should enqueue the work.
    The DB must already have ``board_initialized`` set (coord_db autouse fixture
    sets this via ``_ensure_schema``; for ``rw_db`` we set it explicitly).
    """
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch, test_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "acme/api", 7, "The issue", "done", "work", branch, "passed"),
    )
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, review_of_assignment_id, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            f"rev-{aid}", "server", "api", "acme/api", 7, "Review of issue",
            "done", "review", aid, "approve",
        ),
    )
    conn.commit()


def test_passive_tick_enqueues_approved_work(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#736: _passive_tick() enqueues an approved+tested work assignment into the
    merge queue without a manual ``coord merge`` call.

    This is the key regression guard for the #217 invisible limbo: the daemon
    tick now reliably enqueues approved work on every interval, independent of
    ``pipeline.auto_loop`` or ``coord notify``.
    """
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _passive_tick

    # reconcile_completed_assignments polls the agent HTTP API; stub it to avoid
    # network calls and focus the test on the enqueue path.
    monkeypatch.setattr(
        "coord.reconcile._query_agent",
        lambda host: None,  # agent unreachable → reconcile is a no-op
    )

    _seed_approved_done_work(rw_db)
    cfg = load_config(valid_config_path)

    reconciled, enqueued = _passive_tick(cfg)

    # Reconcile found nothing (we stubbed the agent).
    assert reconciled == []
    # The approved+tested assignment was enqueued by the tick.
    assert enqueued == ["work99"]
    items = mq.load_queue()
    assert len(items) == 1
    assert items[0].assignment_id == "work99"
    assert items[0].branch == "issue-7-impl"
    assert items[0].repo_github == "acme/api"


def test_passive_tick_is_idempotent(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """A second tick with the same approved work produces no further queue changes."""
    from coord.config import load as load_config
    from coord.serve_app import _passive_tick

    monkeypatch.setattr("coord.reconcile._query_agent", lambda host: None)
    _seed_approved_done_work(rw_db)
    cfg = load_config(valid_config_path)

    _passive_tick(cfg)  # first tick — creates the entry
    _, enqueued2 = _passive_tick(cfg)  # second tick — already keyed correctly

    assert enqueued2 == []


def test_passive_tick_writes_operational_audit_rows_for_enqueue(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#1038: the daemon-tick enqueue step writes an operational audit row,
    tagged actor="daemon", separate from any business-tier row."""
    from coord.config import load as load_config
    from coord.serve_app import _passive_tick

    # record_audit's level gate reloads config independently — point it at
    # the same file the test uses (default audit.level="operational").
    monkeypatch.setenv("COORD_CONFIG", str(valid_config_path))
    monkeypatch.setattr("coord.reconcile._query_agent", lambda host: None)
    _seed_approved_done_work(rw_db)
    cfg = load_config(valid_config_path)

    _passive_tick(cfg)

    rows = rw_db.execute(
        "SELECT * FROM audit_log WHERE tier='operational'"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["category"] == "merge_queue"
    assert row["event_type"] == "enqueued"
    assert row["actor"] == "daemon"
    assert row["repo"] == "api"
    assert row["issue"] == 7
    assert row["assignment_id"] == "work99"


def test_passive_tick_suppresses_operational_rows_when_level_is_business(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#1038: audit.level: business drops the operational enqueue row —
    the merge queue write itself is unaffected (best-effort, never blocks)."""
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _passive_tick

    business_config_path = valid_config_path.with_name("business.yml")
    business_config_path.write_text(
        valid_config_path.read_text() + "audit:\n  level: business\n"
    )
    monkeypatch.setenv("COORD_CONFIG", str(business_config_path))
    monkeypatch.setattr("coord.reconcile._query_agent", lambda host: None)
    _seed_approved_done_work(rw_db)
    cfg = load_config(valid_config_path)

    reconciled, enqueued = _passive_tick(cfg)

    assert enqueued == ["work99"]  # the merge-queue write still happens
    assert mq.load_queue()[0].assignment_id == "work99"
    rows = rw_db.execute(
        "SELECT * FROM audit_log WHERE tier='operational'"
    ).fetchall()
    assert rows == []


# ── #775: _reconcile_merges_tick + _sync_issues_tick ─────────────────────────


def _seed_done_work_with_branch(
    conn,
    *,
    aid: str = "work-m1",
    branch: str = "issue-42-impl",
    issue_number: int = 42,
) -> None:
    """Seed a done work assignment that has a branch (eligible for merge reconcile)."""
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')"
    )
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            aid, "laptop", "api", "acme/api", issue_number,
            "The issue", "done", "work", branch,
        ),
    )
    conn.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            aid, "api", "acme/api", branch, "main",
            issue_number, "The issue", "pending",
        ),
    )
    conn.commit()


def test_reconcile_merges_tick_flips_merged_and_prunes_queue(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#775: _reconcile_merges_tick flips a done assignment to 'merged' and
    prunes its stale merge_queue row when the branch is terminal on GitHub.

    This is the black-box acceptance test for the tick path described in the
    issue acceptance criteria.
    """
    from coord import github_ops, merge_queue as mq
    from coord.config import load as load_config
    from coord.serve_app import _reconcile_merges_tick

    # record_audit's level gate reloads config independently (#1038) — pin
    # it to this test's config so the assertions are deterministic
    # regardless of the host's real ~/.coord/coordinator.yml.
    monkeypatch.setenv("COORD_CONFIG", str(valid_config_path))
    # Stub all GitHub probes so we never shell out.
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: True)
    monkeypatch.setattr(
        github_ops, "list_remote_branch_names", lambda repo: set()
    )
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])
    # prune_stale_queue_entries calls issue_is_closed / pr_is_merged.
    monkeypatch.setattr(github_ops, "issue_is_closed", lambda *a: True)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda *a: False)

    _seed_done_work_with_branch(rw_db)
    cfg = load_config(valid_config_path)

    actions = _reconcile_merges_tick(cfg)

    # The reconcile must have reported the flip.
    assert any("mark merged" in a for a in actions), (
        f"Expected 'mark merged' action; got: {actions}"
    )
    # DB must reflect the flip.
    row = rw_db.execute(
        "SELECT status FROM assignments WHERE assignment_id = 'work-m1'"
    ).fetchone()
    assert row is not None and row["status"] == "merged", (
        f"Assignment status should be 'merged', got: {row['status'] if row else None}"
    )
    # The merge_queue row must have been pruned.
    queue = mq.load_queue()
    assert not any(e.assignment_id == "work-m1" for e in queue), (
        f"merge_queue row should have been pruned; queue: {[e.assignment_id for e in queue]}"
    )
    # #1038: one coarse operational row summarizing the tick's actions,
    # separate from the business-tier "merged" row mark_assignment_merged
    # already writes (#1036) regardless of caller.
    op_rows = rw_db.execute(
        "SELECT * FROM audit_log WHERE tier='operational'"
    ).fetchall()
    assert len(op_rows) == 1
    assert op_rows[0]["category"] == "reconcile"
    assert op_rows[0]["event_type"] == "merge_reconcile"
    assert op_rows[0]["actor"] == "daemon"
    business_rows = rw_db.execute(
        "SELECT * FROM audit_log WHERE tier='business' AND category='merge'"
    ).fetchall()
    assert len(business_rows) == 1
    assert business_rows[0]["actor"] == "coordinator"


def test_sync_issues_tick_marks_issues_closed(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#775: _sync_issues_tick propagates issue closures into the DB so the
    board's is_closed flag becomes accurate without a manual 'coord sync'.
    """
    from coord import github_ops
    from coord.config import load as load_config
    from coord.serve_app import _sync_issues_tick

    # Seed an open issue in the DB.
    rw_db.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, state, body, labels) "
        "VALUES (?,?,?,?,?,?)",
        ("api", 42, "An issue", "open", "", "[]"),
    )
    rw_db.commit()

    # GitHub now returns an empty open-issue list (issue 42 was closed).
    # #2858: _sync_issues_tick always calls with force_through_backoff= now.
    monkeypatch.setattr(
        github_ops, "get_open_issues", lambda repo, **kwargs: []
    )

    cfg = load_config(valid_config_path)
    total = _sync_issues_tick(cfg)

    # The sync reported 0 open issues (all repos returned empty lists).
    assert total == 0

    # The issue row must now be marked 'closed' in the DB.
    row = rw_db.execute(
        "SELECT state FROM issues WHERE repo_name = 'api' AND number = 42"
    ).fetchone()
    assert row is not None and row["state"] == "closed", (
        f"Issue should be 'closed' after sync; got: {row['state'] if row else None}"
    )


def test_sync_issues_tick_records_status_per_repo(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#2858: every repo's attempt gets stamped into
    ``coord.issues_sync_status`` — a success advances ``last_success_at``, a
    failure leaves it alone but records the error and still advances
    ``last_attempt_at``. This is the staleness clock
    ``coord.health.checks.issues_sync_staleness`` and the starvation-floor
    bypass both read.
    """
    from coord import github_ops, issues_sync_status
    from coord.serve_app import _sync_issues_tick

    rw_db.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    rw_db.commit()

    def fake_get_open_issues(repo, **kwargs):
        if repo == "acme/api":
            raise github_ops.GhError("gh boom")
        return []

    monkeypatch.setattr(github_ops, "get_open_issues", fake_get_open_issues)

    before = time.time()
    cfg = load_config(valid_config_path)
    _sync_issues_tick(cfg)
    after = time.time()

    api_status = issues_sync_status.status_for("api")
    assert api_status.last_success_at is None
    assert api_status.last_attempt_at is not None
    assert before <= api_status.last_attempt_at <= after
    assert api_status.last_error and "gh boom" in api_status.last_error

    shared_status = issues_sync_status.status_for("shared")
    assert shared_status.last_success_at is not None
    assert before <= shared_status.last_success_at <= after
    assert shared_status.last_error is None


def test_sync_issues_tick_forces_through_backoff_when_starved(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#2858 acceptance: a repo that has not synced successfully in longer
    than ``coord.issues_sync_status.STARVATION_FLOOR_S`` gets
    ``force_through_backoff=True`` on its next fetch — the mechanism that
    lets this tick's slow, fixed 300s cadence escape a shared ``gh`` backoff
    latch continuously re-armed by faster pollers (#2858's incident: 66
    consecutive skip failures over 90 minutes). A repo that synced
    recently does not set the flag — only a genuinely-starved repo is
    entitled to bypass the shared damping.
    """
    from coord import github_ops, issues_sync_status
    from coord.serve_app import _sync_issues_tick

    rw_db.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    rw_db.commit()

    now = time.time()
    # "api" hasn't synced successfully in longer than the starvation floor.
    issues_sync_status.record_success(
        "api", now=now - issues_sync_status.STARVATION_FLOOR_S - 1.0
    )
    # "shared" synced just now — not starved.
    issues_sync_status.record_success("shared", now=now)

    calls: list[tuple[str, bool]] = []

    def fake_get_open_issues(repo, *, force_through_backoff=False):
        calls.append((repo, force_through_backoff))
        return []

    monkeypatch.setattr(github_ops, "get_open_issues", fake_get_open_issues)

    cfg = load_config(valid_config_path)
    _sync_issues_tick(cfg)

    by_repo = dict(calls)
    assert by_repo == {"acme/api": True, "acme/shared": False}


def test_sync_issues_tick_completes_despite_continuously_rearmed_latch(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#2858 acceptance: ``_sync_issues_tick`` completes at least once per M
    minutes even under a shared ``gh`` backoff latch that keeps getting
    re-armed faster than this tick's own cadence.

    End-to-end through the REAL ``coord.github_ops._gh`` (not a stubbed
    ``get_open_issues``): a genuinely active backoff is seeded via
    ``coord.github_throttle.record`` — deep enough that an ordinary call
    would skip the network call entirely and raise, standing in for "every
    plain sample this tick has taken so far landed mid an actively re-armed
    window". Once this repo is starved past the floor, the tick's
    ``force_through_backoff=True`` gets a REAL attempt through despite that
    active window, and it succeeds — exactly the 2026-08-27 incident's own
    observation that a direct ``gh`` call succeeded in under a second the
    whole time the shared latch said otherwise.
    """
    from unittest.mock import MagicMock

    from coord import github_throttle, issues_sync_status
    from coord.serve_app import _sync_issues_tick

    rw_db.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    rw_db.commit()

    now = time.time()
    issues_sync_status.record_success(
        "api", now=now - issues_sync_status.STARVATION_FLOOR_S - 1.0
    )
    issues_sync_status.record_success("shared", now=now)

    github_throttle.record(
        reason="secondary_rate_limit", status=403,
        request_id="orig-request-id", retry_after_s=600.0,
    )

    monkeypatch.setattr("coord.github_ops.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "coord.github_ops.subprocess.run",
        lambda *a, **k: MagicMock(returncode=0, stdout="[]", stderr=""),
    )

    cfg = load_config(valid_config_path)
    total = _sync_issues_tick(cfg)

    assert total == 0  # both repos returned an empty issue list
    # The starved repo ("api") got a real, successful attempt through the
    # still-active backoff. The fresh repo ("shared") was skipped by the
    # ordinary damping (still deep inside the same window) and stayed
    # exactly where it was.
    assert issues_sync_status.status_for("api").last_success_at is not None
    assert issues_sync_status.status_for("api").last_success_at > now
    assert issues_sync_status.status_for("shared").last_success_at == now


# ── #2994: _sync_issues_tick dormant-repo skip ───────────────────────────────


def test_sync_issues_tick_skips_dormant_repo(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#2994: a repo with no open assignment, no drive-queue entry, and no
    coord-authored open PR is skipped once it has had a baseline sweep --
    saving the gh call the normal cadence would otherwise spend on it every
    tick regardless of activity."""
    from coord import github_ops, repo_dormancy
    from coord.serve_app import _sync_issues_tick

    rw_db.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    rw_db.commit()

    # 'shared' has no assignment and no drive-queue entry -- dormant. Give it
    # a baseline sweep so the floor actually applies (a repo that has never
    # been swept is always due -- see
    # coord.repo_dormancy.should_skip_sweep / test_repo_dormancy.py).
    repo_dormancy.record_swept("shared", repo_dormancy.KIND_ISSUES, now=time.time())

    calls: list[str] = []
    monkeypatch.setattr(
        github_ops, "get_open_issues",
        lambda repo, **kwargs: calls.append(repo) or [],
    )

    cfg = load_config(valid_config_path)
    total = _sync_issues_tick(cfg)

    assert total == 0
    assert calls == ["acme/api"]


def test_sync_issues_tick_wakes_dormant_repo_when_work_is_queued(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#2994 acceptance: queuing work for a dormant repo puts it back on the
    normal cadence on the very next tick, not after the floor
    (DORMANT_SWEEP_FLOOR_S) expires."""
    from coord import github_ops, repo_dormancy
    from coord.serve_app import _sync_issues_tick

    rw_db.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    rw_db.commit()

    now = time.time()
    repo_dormancy.record_swept("api", repo_dormancy.KIND_ISSUES, now=now)
    repo_dormancy.record_swept("shared", repo_dormancy.KIND_ISSUES, now=now)

    calls: list[str] = []
    monkeypatch.setattr(
        github_ops, "get_open_issues",
        lambda repo, **kwargs: calls.append(repo) or [],
    )

    cfg = load_config(valid_config_path)
    _sync_issues_tick(cfg)
    # Both repos idle, both just swept -- well inside the floor, both skipped.
    assert calls == []

    # Work gets queued for 'shared' -- an open assignment now on the board.
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("w-shared", "laptop", "shared", "acme/shared", 7, "New work",
         "pending", "work"),
    )
    rw_db.commit()

    calls.clear()
    _sync_issues_tick(cfg)

    assert calls == ["acme/shared"]


def test_reconcile_and_issues_ticks_back_to_back_do_not_starve_issues_sync(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#2994 review (blocking regression guard): drives
    ``_reconcile_merges_tick`` immediately followed by ``_sync_issues_tick``
    for the same fully-dormant repos, exactly the order and proximity
    ``_tick_loop`` uses them in. Before the fix both ticks shared one
    ``{repo_name: last_swept_at}`` clock, so the PR sweep (which always runs
    first) stamped it and the issues sweep, moments later in the same pass,
    saw a just-refreshed timestamp and skipped -- permanently, since it
    never got a first real sweep to see stale. The two sweep kinds must use
    independent floors so this can't happen."""
    from coord import github_ops
    from coord.config import load as load_config
    from coord.serve_app import _reconcile_merges_tick, _sync_issues_tick

    monkeypatch.setenv("COORD_CONFIG", str(valid_config_path))

    rw_db.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    rw_db.commit()
    # No assignments seeded at all -- both 'api' and 'shared' are fully
    # dormant (no open assignment, no drive-queue entry, no open PR).

    pr_calls: list[str] = []
    issue_calls: list[str] = []
    monkeypatch.setattr(
        github_ops, "list_open_prs", lambda repo: pr_calls.append(repo) or []
    )
    monkeypatch.setattr(
        github_ops,
        "get_open_issues",
        lambda repo, **kwargs: issue_calls.append(repo) or [],
    )
    # Sweeps (a)/(b) inside reconcile_board_merges only touch board rows,
    # and the board is empty here, but stub them anyway so this test can't
    # ever shell out to a real `gh`/`git`.
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: False)
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "issue_is_closed", lambda *a: False)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda *a: False)

    cfg = load_config(valid_config_path)

    # Same order + proximity as the real _tick_loop: reconcile-merges tick
    # (stale-PR sweep) immediately followed by the issues-sync tick.
    _reconcile_merges_tick(cfg)
    _sync_issues_tick(cfg)

    # Both repos had never been swept before -- both sweep kinds are due on
    # their own first look, independent of what the other kind just did.
    assert sorted(pr_calls) == ["acme/api", "acme/shared"]
    assert sorted(issue_calls) == ["acme/api", "acme/shared"]


# ── #1220: _clean_worktrees_tick — fleet-wide orphaned-worktree sweep ────────


def test_clean_worktrees_tick_calls_every_machine(
    valid_config_path: Path, monkeypatch
) -> None:
    """#1220: _clean_worktrees_tick POSTs /worktree-clean to every machine in
    config.machines and returns one result dict per machine, keyed by name.

    This is the black-box acceptance test for the automatic sweep described
    in the issue: nothing previously called the existing /worktree-clean
    endpoint on a schedule, so orphaned worktrees piled up fleet-wide.
    """
    from coord import network
    from coord.config import load as load_config
    from coord.serve_app import _clean_worktrees_tick

    calls: list[str] = []

    def fake_clean_worktrees(machine, **kwargs):
        calls.append(machine.name)
        return {"ok": True, "cleaned": 2, "kept": 1, "bytes_freed": 4096, "error": None}

    monkeypatch.setattr(network, "clean_worktrees", fake_clean_worktrees)

    cfg = load_config(valid_config_path)
    results = _clean_worktrees_tick(cfg)

    # valid_config_yaml defines two machines: laptop, server.
    assert sorted(calls) == ["laptop", "server"]
    assert {r["machine"] for r in results} == {"laptop", "server"}
    for r in results:
        assert r["ok"] is True
        assert r["cleaned"] == 2
        assert r["bytes_freed"] == 4096


def test_clean_worktrees_tick_one_machine_unreachable_does_not_block_others(
    valid_config_path: Path, monkeypatch
) -> None:
    """#1220: one unreachable machine must not abort the sweep on the rest of
    the fleet — its result is recorded as an error entry, the others still
    clean normally."""
    from coord import network
    from coord.config import load as load_config
    from coord.serve_app import _clean_worktrees_tick

    def fake_clean_worktrees(machine, **kwargs):
        if machine.name == "server":
            return {
                "ok": False, "cleaned": 0, "kept": 0, "bytes_freed": 0,
                "error": "connection refused",
            }
        return {"ok": True, "cleaned": 5, "kept": 0, "bytes_freed": 999, "error": None}

    monkeypatch.setattr(network, "clean_worktrees", fake_clean_worktrees)

    cfg = load_config(valid_config_path)
    results = _clean_worktrees_tick(cfg)

    by_name = {r["machine"]: r for r in results}
    assert by_name["server"]["ok"] is False
    assert by_name["server"]["error"] == "connection refused"
    assert by_name["laptop"]["ok"] is True
    assert by_name["laptop"]["cleaned"] == 5


def test_clean_worktrees_tick_writes_operational_audit_row_when_something_cleaned(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#1220/#1038: a sweep that actually freed disk space on at least one
    machine writes one operational audit row summarizing the tick."""
    from coord import network
    from coord.config import load as load_config
    from coord.serve_app import _clean_worktrees_tick

    monkeypatch.setenv("COORD_CONFIG", str(valid_config_path))

    def fake_clean_worktrees(machine, **kwargs):
        if machine.name == "laptop":
            return {"ok": True, "cleaned": 3, "kept": 0, "bytes_freed": 1024, "error": None}
        return {"ok": True, "cleaned": 0, "kept": 4, "bytes_freed": 0, "error": None}

    monkeypatch.setattr(network, "clean_worktrees", fake_clean_worktrees)

    cfg = load_config(valid_config_path)
    _clean_worktrees_tick(cfg)

    rows = rw_db.execute(
        "SELECT * FROM audit_log WHERE tier='operational' AND category='worktree_clean'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "swept"
    assert rows[0]["actor"] == "daemon"
    assert "1" in rows[0]["summary"]  # 1 machine reported non-zero cleanup


def test_clean_worktrees_tick_no_audit_row_when_nothing_cleaned(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#1220: an all-empty sweep (every machine reports cleaned=0) is a no-op
    tick and must not write an audit row — mirrors the housekeeping-sweep
    and merge-reconcile ticks' "only log real events" convention."""
    from coord import network
    from coord.config import load as load_config
    from coord.serve_app import _clean_worktrees_tick

    monkeypatch.setenv("COORD_CONFIG", str(valid_config_path))
    monkeypatch.setattr(
        network,
        "clean_worktrees",
        lambda machine, **kwargs: {
            "ok": True, "cleaned": 0, "kept": 2, "bytes_freed": 0, "error": None,
        },
    )

    cfg = load_config(valid_config_path)
    _clean_worktrees_tick(cfg)

    rows = rw_db.execute(
        "SELECT * FROM audit_log WHERE category='worktree_clean'"
    ).fetchall()
    assert rows == []


def test_clean_worktrees_tick_forwards_protect_list_from_board(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#1295: the tick must forward a per-machine ``protect`` list of
    board-known-live assignment_ids to each agent's /worktree-clean POST.

    Seeds a running ``work42`` assignment on ``laptop`` and a done
    ``work99`` on ``server``; the tick should send ``protect=["work42"]``
    to laptop and ``protect=None`` (nothing to protect) to server.  This
    guards the coordinator-side belt-and-braces defense that keeps a
    live worker's worktree even if the agent lost its local record.
    """
    from coord import network
    from coord.config import load as load_config
    from coord.serve_app import _clean_worktrees_tick

    monkeypatch.setenv("COORD_CONFIG", str(valid_config_path))

    # Seed one running (non-terminal) assignment on laptop and one done
    # (terminal) on server — only the running one should be forwarded.
    rw_db.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("work42", "laptop", "api", "owner/api", 42, "live", "running", "work"),
    )
    rw_db.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("work99", "server", "api", "owner/api", 99, "done", "done", "work"),
    )
    rw_db.commit()

    seen: dict[str, list[str] | None] = {}

    def fake_clean_worktrees(machine, *, protect=None, **kw):
        seen[machine.name] = list(protect) if protect else None
        return {"ok": True, "cleaned": 0, "kept": 0, "bytes_freed": 0, "error": None}

    monkeypatch.setattr(network, "clean_worktrees", fake_clean_worktrees)

    cfg = load_config(valid_config_path)
    _clean_worktrees_tick(cfg)

    assert seen.get("laptop") == ["work42"], (
        f"laptop should have received protect=['work42'], got: {seen.get('laptop')}"
    )
    # server had only a terminal assignment → no protect list forwarded.
    assert seen.get("server") is None, (
        f"server should have received no protect list, got: {seen.get('server')}"
    )


# ── #776: merge_plan in /board payload ───────────────────────────────────────


def test_board_payload_has_merge_plan_key(
    file_db: Path, valid_config_path: Path
) -> None:
    """/board always includes a 'merge_plan' key (may be an empty list)."""
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    assert "merge_plan" in board
    assert isinstance(board["merge_plan"], list)


def test_board_merge_plan_contains_correct_fields(
    rw_db, valid_config_path: Path, monkeypatch, tmp_path: Path
) -> None:
    """/board merge_plan entries carry the required #776 fields.

    Seeds a PENDING merge-queue entry and verifies the plan contains
    rank, status, reason, target_branch, enqueued_at, size, milestone.
    """
    from coord import github_ops, merge_queue as mq
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    # Stub GitHub so build_board and plan() never shell out.
    monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda *a: 0)

    # Seed a pending merge-queue entry with a known enqueued_at.
    import time as _time
    ts = _time.time() - 30.0
    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    rw_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state, size, enqueued_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("w1", "api", "acme/api", "issue-1-impl", "main", 1, "t", "pending", 42, ts),
    )
    rw_db.commit()

    cfg = load_config(valid_config_path)
    # #684/#776 regression: read from the SAME db rw_db seeded (its temp
    # rw.db), not the canonical DB_PATH.  SqliteStore opens mode=ro, which
    # errors ("unable to open database file") when the path is absent — so
    # SqliteStore(DB_PATH) failed in CI (no ~/.coord/coord.db) and only
    # "passed" locally where a real coord.db happened to exist.
    app = build_app(SqliteStore(tmp_path / "rw.db"), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert "merge_plan" in board
    assert len(board["merge_plan"]) == 1
    pm = board["merge_plan"][0]

    # Required fields from the #776 spec
    assert pm["assignment_id"] == "w1"
    assert pm["rank"] == 1
    assert pm["status"] in (mq.PLAN_READY, mq.PLAN_BLOCKED)
    assert "reason" in pm
    assert pm["target_branch"] == "main"
    assert pm["size"] == 42
    assert pm["enqueued_at"] is not None
    assert pm["milestone"] is None  # not in issues table


def test_board_merge_plan_does_not_503_on_plan_error(
    file_db: Path, valid_config_path: Path, monkeypatch
) -> None:
    """/board returns 200 even when plan() raises — merge_plan falls back to []."""
    from coord import merge_queue as mq
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    monkeypatch.setattr(mq, "plan", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board")
    assert resp.status_code == 200
    body = resp.json()
    assert body["merge_plan"] == []


# ── #550: issue_stage_projection in /board payload ────────────────────────────


def test_board_payload_has_issue_stage_projection_key(
    file_db: Path, valid_config_path: Path
) -> None:
    """/board always includes an 'issue_stage_projection' key (may be empty)."""
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    assert "issue_stage_projection" in board
    assert isinstance(board["issue_stage_projection"], list)


def test_board_issue_stage_projection_contains_correct_fields(
    rw_db, valid_config_path: Path, tmp_path: Path
) -> None:
    """/board issue_stage_projection carries computed stage badges + has_approved_review.

    Seeds a done work assignment with an approved review — mirrors the shape
    coord-tui's pipeline.rs stage functions currently derive independently.
    """
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title, "
        " status, type, test_state, dispatched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("w1", "laptop", "api", 1, "An issue", "done", "work", "passed", 1.0),
    )
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title, "
        " status, type, review_of_assignment_id, review_verdict, dispatched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("rev1", "server", "api", 1, "An issue", "done", "review", "w1", "approve", 2.0),
    )
    rw_db.commit()

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(tmp_path / "rw.db"), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    proj = {(e["repo_name"], e["issue_number"]): e for e in board["issue_stage_projection"]}
    assert ("api", 1) in proj
    entry = proj[("api", 1)]
    assert entry["has_approved_review"] is True
    assert entry["stages"]["work"] == "done"
    assert entry["stages"]["test"] == "done"
    assert entry["stages"]["review"] == "done"


def test_board_issue_stage_projection_agrees_with_merge_plan_on_superseded_approval(
    rw_db, valid_config_path: Path, tmp_path: Path
) -> None:
    """#2085 black-box regression: the #1966 work chain (an early ``approve``
    superseded by a later ``request-changes`` on newer commits) must read the
    SAME verdict on both surfaces the daemon serves off one ``/board`` build —
    the per-issue stage projection's ``has_approved_review`` flag AND the
    merge queue's own plan/gate status — not the self-contradictory pair
    the issue observed (``stages["review"] == "failed"`` next to
    ``has_approved_review: True``, while the live merge gate separately
    refused as ``review_required``).

    Chain (mirrors #2085's Observed table exactly):
      work c908129d  -> approve review (rev-orig, review_head_sha=sha-a)
      work 8e3eb76e (fix round, same branch) -> request-changes review
        (rev-fix, review_head_sha=sha-b), the LATEST verdict-bearing event.

    No live ``gh_ops`` is wired into this daemon build (``/board`` makes zero
    ``gh`` subprocess calls — see the #1336 invariant in serve_app.py), so the
    merge-queue row's ``branch_head_sha`` is never populated — exactly the
    caller shape #2085 traces the bug to. Before the fix, both the projection
    and the plan read this as approved/READY; after, both must agree it is
    NOT approved.
    """
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch, test_state, dispatched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("c908129d", "laptop", "api", "acme/api", 1966, "Flaky merge gate",
         "done", "work", "issue-1966-fix", "passed", 1.0),
    )
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, review_of_assignment_id, review_verdict, "
        " review_head_sha, dispatched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rev-orig", "server", "api", "acme/api", 1966, "Flaky merge gate",
         "done", "review", "c908129d", "approve", "sha-a", 2.0),
    )
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch, test_state, dispatched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("8e3eb76e", "laptop", "api", "acme/api", 1966, "Flaky merge gate",
         "done", "work", "issue-1966-fix", "passed", 3.0),
    )
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, review_of_assignment_id, review_verdict, "
        " review_head_sha, dispatched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rev-fix", "server", "api", "acme/api", 1966, "Flaky merge gate",
         "done", "review", "8e3eb76e", "request-changes", "sha-b", 4.0),
    )
    # The queue entry is re-keyed to the fix round (as `enqueue_approved_work`
    # would leave it) — no `branch_head_sha` populated, mirroring a daemon
    # `/board` build's zero-`gh`-I/O read path.
    rw_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("8e3eb76e", "api", "acme/api", "issue-1966-fix", "main", 1966,
         "Flaky merge gate", "pending"),
    )
    rw_db.commit()

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(tmp_path / "rw.db"), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    proj = {(e["repo_name"], e["issue_number"]): e for e in board["issue_stage_projection"]}
    assert ("api", 1966) in proj
    entry = proj[("api", 1966)]
    # The general stage dispatcher already keyed off the LATEST review by
    # dispatch order — this is the reference the top-level field must match.
    assert entry["stages"]["review"] == "failed"
    assert entry["has_approved_review"] is False, (
        "has_approved_review must not read an approval superseded by a "
        "later request-changes review as still covering the branch"
    )

    plan_entries = {pm["assignment_id"]: pm for pm in board["merge_plan"]}
    assert "8e3eb76e" in plan_entries
    plan_entry = plan_entries["8e3eb76e"]
    assert plan_entry["status"] == "BLOCKED"
    assert "review" in (plan_entry["reason"] or "").lower()


def test_board_issue_stage_projection_does_not_503_on_error(
    file_db: Path, valid_config_path: Path, monkeypatch
) -> None:
    """/board returns 200 even when the projection raises — falls back to []."""
    from coord import stage_projection as sp
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    monkeypatch.setattr(
        sp,
        "compute_board_stage_projection",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board")
    assert resp.status_code == 200
    body = resp.json()
    assert body["issue_stage_projection"] == []


# ── #781: _auto_drain_tick ────────────────────────────────────────────────────


def _seed_queued_ready_entry(
    conn,
    *,
    aid: str = "work-drain1",
    branch: str = "issue-55-impl",
    issue_number: int = 55,
) -> None:
    """Seed a fully-gated (approved + tested) done work assignment AND a
    corresponding pending merge_queue row.

    After this seed:
    - ``plan()`` sees the work has an approved review + passed test verdict →
      marks the entry ``PLAN_READY`` (all gates pass).
    - ``_auto_drain_tick`` should pick it up and call ``process()``.
    """
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch, test_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "acme/api", issue_number, "The issue", "done", "work", branch, "passed"),
    )
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, review_of_assignment_id, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            f"rev-{aid}", "server", "api", "acme/api", issue_number, "Review of issue",
            "done", "review", aid, "approve",
        ),
    )
    conn.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (aid, "api", "acme/api", branch, "main", issue_number, "The issue", "pending"),
    )
    conn.commit()


def _seed_queued_blocked_entry(
    conn,
    *,
    aid: str = "work-blocked1",
    branch: str = "issue-56-impl",
    issue_number: int = 56,
) -> None:
    """Seed a done work assignment with NO approved review + a pending queue row.

    ``plan()`` marks this entry ``PLAN_BLOCKED`` (review not approved), so
    ``_auto_drain_tick`` must skip it.
    """
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch, test_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "acme/api", issue_number, "The issue", "done", "work", branch, "passed"),
    )
    # No review row — plan() will evaluate review gate → BLOCKED.
    conn.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (aid, "api", "acme/api", branch, "main", issue_number, "The issue", "pending"),
    )
    conn.commit()


def _make_drain_config(tmp_path: "Path", *, auto_drain: bool = True) -> "Path":
    """Write a coordinator.yml with merge.auto_drain set and return its path."""
    content = (
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    capabilities: [python]\n"
        "    repos: [api]\n"
        "\n"
        f"merge:\n"
        f"  auto_drain: {'true' if auto_drain else 'false'}\n"
    )
    p = tmp_path / "coord-drain.yml"
    p.write_text(content)
    return p


def test_auto_drain_config_default_off(valid_config_path: "Path") -> None:
    """#781: merge.auto_drain defaults to False when the merge: block is absent."""
    from coord.config import load as load_config

    cfg = load_config(valid_config_path)
    assert cfg.merge.auto_drain is False
    assert cfg.merge.max_per_tick == 0


def test_auto_drain_ready_entry_merges(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#781: _auto_drain_tick() merges a READY entry when auto_drain is enabled.

    Gate conditions met (approved review + passed test), so plan() marks the
    entry READY and _auto_drain_tick calls process() which merges the PR.
    """
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.merge_queue import MERGED
    from coord.serve_app import _auto_drain_tick

    # Stub out github_ops so process() never shells out.
    monkeypatch.setattr(
        "coord.github_ops.create_pr",
        lambda repo, *, base, head, title, body: {
            "number": 201, "url": "https://gh/201", "existed": False
        },
    )
    monkeypatch.setattr("coord.github_ops.get_pr_size", lambda repo, number: 42)
    monkeypatch.setattr("coord.github_ops.merge_pr", lambda repo, number, method="rebase": (True, "merged"))
    # NoOpCi so CI gate is always a pass (is_available=False).  Patch at the
    # source module — _auto_drain_tick imports build_ci_store as a local import.
    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t, **_kw: _NoOpCi())

    _seed_queued_ready_entry(rw_db)
    drain_config_path = _make_drain_config(tmp_path, auto_drain=True)
    monkeypatch.setenv("COORD_CONFIG", str(drain_config_path))  # #1038 level gate
    cfg = load_config(drain_config_path)
    assert cfg.merge.auto_drain is True

    events = _auto_drain_tick(cfg)

    # At least one "merged" event emitted.
    merge_events = [ev for ev in events if ev.kind == "merged"]
    assert merge_events, f"expected a merged event, got: {[ev.kind for ev in events]}"
    assert merge_events[0].entry.assignment_id == "work-drain1"

    # Queue entry transitioned to MERGED.
    items = mq.load_queue()
    assert any(item.state == MERGED for item in items), (
        f"expected MERGED in queue, got: {[item.state for item in items]}"
    )

    # #1038: one operational row per MergeEvent this auto-drain tick
    # produced (process() emits "opened" then "merged" for a fresh entry).
    op_rows = rw_db.execute(
        "SELECT * FROM audit_log WHERE tier='operational' AND category='merge'"
    ).fetchall()
    assert len(op_rows) == len(events)
    assert {r["event_type"] for r in op_rows} == {f"merge_{ev.kind}" for ev in events}
    assert all(r["actor"] == "daemon" for r in op_rows)
    assert all(r["assignment_id"] == "work-drain1" for r in op_rows)


def test_auto_drain_reconciles_stale_conflict_before_planning(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#1477: a CONFLICT entry whose branch has since become mergeable clears
    itself and merges within the same auto-drain tick — auto-drain has no
    other chance to notice a conflict-fix worker (or a human) repairing the
    branch, since it only ever considers PLAN_READY entries."""
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.merge_queue import MERGED
    from coord.serve_app import _auto_drain_tick

    _seed_queued_ready_entry(
        rw_db, aid="work-conflict1", branch="issue-57-impl", issue_number=57,
    )
    # Flip the seeded (otherwise fully-gated) row to CONFLICT with a PR
    # already open — simulating a previous failed merge attempt whose branch
    # has since been repaired by a conflict-fix worker or by hand.
    rw_db.execute(
        "UPDATE merge_queue SET state='conflict', pr_number=555, error=? "
        "WHERE assignment_id=?",
        ("not mergeable", "work-conflict1"),
    )
    rw_db.commit()

    monkeypatch.setattr("coord.github_ops.check_pr_mergeable", lambda repo, number: True)
    monkeypatch.setattr("coord.github_ops.get_pr_size", lambda repo, number: 42)
    monkeypatch.setattr(
        "coord.github_ops.merge_pr", lambda repo, number, method="rebase": (True, "merged"),
    )
    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t, **_kw: _NoOpCi())

    drain_config_path = _make_drain_config(tmp_path, auto_drain=True)
    monkeypatch.setenv("COORD_CONFIG", str(drain_config_path))
    cfg = load_config(drain_config_path)

    events = _auto_drain_tick(cfg)

    merge_events = [ev for ev in events if ev.kind == "merged"]
    assert merge_events, f"expected a merged event, got: {[ev.kind for ev in events]}"
    items = mq.load_queue()
    assert any(item.state == MERGED for item in items), (
        f"expected MERGED in queue, got: {[item.state for item in items]}"
    )


def test_auto_drain_still_conflicting_entry_stays_parked(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#1477: when GitHub still reports the PR as conflicting, auto-drain
    must leave the entry parked — never speculatively unpark it."""
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _auto_drain_tick

    _seed_queued_ready_entry(
        rw_db, aid="work-conflict2", branch="issue-58-impl", issue_number=58,
    )
    rw_db.execute(
        "UPDATE merge_queue SET state='conflict', pr_number=556, error=? "
        "WHERE assignment_id=?",
        ("not mergeable", "work-conflict2"),
    )
    rw_db.commit()

    monkeypatch.setattr("coord.github_ops.check_pr_mergeable", lambda repo, number: False)
    merge_calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.merge_pr",
        lambda repo, number, method="rebase": merge_calls.append((repo, number)) or (True, "merged"),
    )
    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t, **_kw: _NoOpCi())

    cfg = load_config(_make_drain_config(tmp_path, auto_drain=True))
    events = _auto_drain_tick(cfg)

    assert events == [], f"expected no events, got: {[ev.kind for ev in events]}"
    assert merge_calls == []
    items = mq.load_queue()
    assert any(item.state == "conflict" for item in items), (
        f"expected the entry to stay CONFLICT, got: {[item.state for item in items]}"
    )


def test_auto_drain_blocked_entry_not_touched(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#781: _auto_drain_tick() skips a BLOCKED entry — no merge call, state unchanged.

    The blocked entry has no approved review, so plan() marks it PLAN_BLOCKED.
    _auto_drain_tick should return an empty events list and leave the queue row
    in its original 'pending' state.
    """
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _auto_drain_tick

    # Track any merge calls — there should be none.
    merge_calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.merge_pr",
        lambda repo, number, method="rebase": merge_calls.append((repo, number)) or (True, "merged"),
    )
    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t, **_kw: _NoOpCi())

    _seed_queued_blocked_entry(rw_db)
    cfg = load_config(_make_drain_config(tmp_path, auto_drain=True))

    events = _auto_drain_tick(cfg)

    # No events — BLOCKED entry was skipped entirely.
    assert events == [], f"expected no events for blocked entry, got: {[ev.kind for ev in events]}"
    assert merge_calls == [], "merge_pr must not be called for a BLOCKED entry"

    # Queue row is still pending.
    items = mq.load_queue()
    assert len(items) == 1
    assert items[0].state == "pending"


def test_auto_drain_never_triggers_ci_rerun_for_stale_ci(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#1851: a green-but-CI-stale entry (approved review + passed test, but
    its CI checks predate the base's newest commit) must be BLOCKED by the
    unattended auto-drain tick, and — the point of this test — must never
    trigger `ci_store.rerun_for_pr()`. That remedy is opt-in behind `coord
    merge --revalidate` only; auto-drain starting CI runs on its own
    schedule is the same shape as the suite-runs-on-a-timer behaviour gated
    off after the 2026-06-07 token-burn incident.

    Asserted dynamically via a spy call count on the actual `ci_store` the
    gate consults — not by inspecting `_auto_drain_tick`'s source — per
    #1851's acceptance criteria ("asserted by a test, not just by
    inspection").
    """
    from types import SimpleNamespace

    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _auto_drain_tick

    aid, branch, issue_number = "work-cistale1", "issue-57-impl", 57
    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch, test_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "acme/api", issue_number, "The issue", "done", "work", branch, "passed"),
    )
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, review_of_assignment_id, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            f"rev-{aid}", "server", "api", "acme/api", issue_number, "Review of issue",
            "done", "review", aid, "approve",
        ),
    )
    rw_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state, pr_number) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, "api", "acme/api", branch, "main", issue_number, "The issue", "pending", 501),
    )
    rw_db.commit()

    rerun_calls: list = []
    merge_calls: list = []

    class _FakeCiStore:
        is_available = True

        def list_checks_for_pr(self, repo, number):
            # Green, but started well before the (mocked) base commit time
            # below — the #1851 staleness signal.
            return [SimpleNamespace(
                name="build", status="completed", conclusion="success",
                started_at=500.0, completed_at=None,
            )]

        def rerun_for_pr(self, repo, number):
            rerun_calls.append((repo, number))
            return True

    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t, **_kw: _FakeCiStore())
    monkeypatch.setattr(
        "coord.github_ops.get_branch_commit_timestamp",
        lambda repo, branch: 1000.0,  # newer than the check's started_at=500.0
    )
    monkeypatch.setattr(
        "coord.github_ops.merge_pr",
        lambda repo, number, method="rebase": merge_calls.append((repo, number)) or (True, "merged"),
    )

    cfg = load_config(_make_drain_config(tmp_path, auto_drain=True))
    events = _auto_drain_tick(cfg)

    assert rerun_calls == [], f"auto-drain must never call rerun_for_pr: {rerun_calls}"
    assert merge_calls == [], "a CI-stale entry must not merge from auto-drain"
    merge_events = [ev for ev in events if ev.kind == "merged"]
    assert merge_events == [], f"expected no merged events, got: {[ev.kind for ev in events]}"

    items = mq.load_queue()
    assert len(items) == 1
    assert items[0].state == "pending"


def test_auto_drain_error_isolation(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#781: an error inside _auto_drain_tick propagates cleanly so the tick
    loop's try/except can absorb it without crashing the daemon.

    Verifies two isolation properties:
    1. The error raised by plan() bubbles out of _auto_drain_tick (the caller
       is responsible for catching it — matching the pattern of every other tick
       step in _tick_loop).
    2. The queue is left untouched (no partial writes on error).
    """
    import pytest
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _auto_drain_tick

    _seed_queued_ready_entry(rw_db)
    cfg = load_config(_make_drain_config(tmp_path, auto_drain=True))

    original_items = mq.load_queue()
    assert len(original_items) == 1

    # Simulate a transient CI-lookup failure inside plan().
    monkeypatch.setattr(
        mq, "plan",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ci lookup exploded")),
    )

    with pytest.raises(RuntimeError, match="ci lookup exploded"):
        _auto_drain_tick(cfg)

    # Queue is unchanged — no partial writes occurred.
    after = mq.load_queue()
    assert len(after) == 1
    assert after[0].state == "pending"


def test_auto_drain_serializes_on_merge_lock(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#1400-review: ``_auto_drain_tick``'s load->process->save critical
    section must take the same ``_merge_lock`` a concurrent ``POST /merge``
    holds.

    ``merge_queue.process()`` has exactly two call sites in the codebase:
    the daemon-routed ``coord merge`` callback (already wrapped in
    ``_merge_lock`` by #1400) and this auto-drain tick. Both do a full
    ``load_queue()`` -> mutate -> ``save_queue()`` replace of the WHOLE
    merge-queue table. Without also taking ``_merge_lock`` here, a driver's
    ``/merge`` request and this tick (fired independently every ~30s by
    ``_tick_loop``) can still race and silently clobber each other's
    just-recorded state — the identical hazard #1400 closed for two
    concurrent ``/merge`` requests, just reached via a different pair of
    callers.

    Holds ``_merge_lock`` in the main thread and starts ``_auto_drain_tick``
    on a background thread; asserts the tick blocks (never reaches
    ``merge_queue.process()``) until the lock is released.
    """
    import threading

    from coord.config import load as load_config
    from coord.serve_app import _auto_drain_tick, _merge_lock

    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t, **_kw: _NoOpCi())

    process_called = threading.Event()

    def _fake_process(items, *a, **kw):
        process_called.set()
        return []

    monkeypatch.setattr("coord.merge_queue.process", _fake_process)

    _seed_queued_ready_entry(rw_db)
    cfg = load_config(_make_drain_config(tmp_path, auto_drain=True))

    result: dict = {}

    def _run():
        result["events"] = _auto_drain_tick(cfg)

    _merge_lock.acquire()
    try:
        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=0.3)
        assert t.is_alive(), (
            "_auto_drain_tick returned while _merge_lock was still held "
            "externally -- its critical section is not serialized against "
            "a concurrent /merge (#1400 regression)"
        )
        assert not process_called.is_set(), (
            "_auto_drain_tick called merge_queue.process() before "
            "acquiring _merge_lock (#1400 regression)"
        )
    finally:
        _merge_lock.release()
    t.join(timeout=5)
    assert not t.is_alive(), "_auto_drain_tick did not finish after _merge_lock was released"
    assert process_called.is_set()
    assert result["events"] == []


# ── #2829: _auto_revalidate_tick — the merge.auto_revalidate daemon tick ────


def _make_auto_revalidate_config(
    tmp_path: "Path", *, auto_revalidate: bool = True, max_batch: int = 3,
) -> "Path":
    """Write a coordinator.yml with merge.auto_revalidate set and return its path."""
    content = (
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    capabilities: [python]\n"
        "    repos: [api]\n"
        "\n"
        f"merge:\n"
        f"  auto_revalidate: {'true' if auto_revalidate else 'false'}\n"
        f"  auto_revalidate_max_batch: {max_batch}\n"
    )
    p = tmp_path / "coord-auto-revalidate.yml"
    p.write_text(content)
    return p


def test_auto_revalidate_config_default_off(valid_config_path: "Path") -> None:
    """#2829: merge.auto_revalidate defaults to False when the merge: block
    is absent -- byte-identical to today for anyone who hasn't opted in."""
    from coord.config import load as load_config

    cfg = load_config(valid_config_path)
    assert cfg.merge.auto_revalidate is False
    assert cfg.merge.auto_revalidate_max_batch == 3


def test_auto_revalidate_composite_runs_with_merge_lock_released(
    tmp_path: "Path", rw_db, monkeypatch,
) -> None:
    """#2829's core restructure: `coord.revalidate.revalidate_group`'s
    composite (which can run for up to `DEFAULT_TIMEOUT_SECONDS` and, on a
    red result, `1 + N` times that) must run with `_merge_lock` RELEASED --
    the whole point of moving unattended revalidation out of the daemon
    route's existing lock-everything shape. `_merge_lock` may only be taken
    for the base-SHA recheck and the merge step that follows it.

    Holds `_merge_lock` in the main thread, starts `_auto_revalidate_tick`
    on a background thread, and asserts the (stubbed) composite still runs
    to completion while the lock is held -- then that the function blocks
    from that point on until the lock is released.
    """
    import threading

    from coord import merge_queue as mq
    from coord import revalidate as rv
    from coord.config import load as load_config
    from coord.serve_app import _auto_revalidate_tick, _merge_lock

    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t, **_kw: _NoOpCi())

    entry = mq.QueuedMerge(
        assignment_id="work-reval1", repo_name="api", repo_github="acme/api",
        branch="issue-77-impl", target_branch="main", issue_number=77,
        issue_title="The issue", state=mq.PENDING,
    )
    candidate = mq.RevalidationCandidate(
        entry=entry, work_assignment_id="work-reval1",
        smoke=mq.SmokeVerdictStatus(
            ok=False, kind=mq.SMOKE_STALE, assignment_id="work-reval1",
        ),
    )
    monkeypatch.setattr(mq, "load_queue", lambda: [entry])
    monkeypatch.setattr(mq, "revalidation_candidates", lambda *a, **kw: [candidate])

    composite_started = threading.Event()

    def fake_group(group, cfg, echo=None):
        composite_started.set()
        return rv.BatchRevalidationResult(
            composite=rv.RevalidationResult(
                ok=True, recorded=["work-reval1"], validated_base_sha="irrelevant",
            ),
            recorded=["work-reval1"],
        )

    monkeypatch.setattr(rv, "revalidate_group", fake_group)
    monkeypatch.setattr(rv, "revalidated_base_still_current", lambda *a, **kw: True)
    # Nothing READY once the merge step actually runs -- this test asserts
    # lock ORDERING, not a merge outcome, so `plan()` returning empty is the
    # simplest way to let the function return cleanly once unblocked.
    monkeypatch.setattr(mq, "plan", lambda *a, **kw: [])

    cfg = load_config(_make_auto_revalidate_config(tmp_path))
    assert cfg.merge.auto_revalidate is True

    result: dict = {}

    def _run():
        result["events"] = _auto_revalidate_tick(cfg)

    _merge_lock.acquire()
    try:
        t = threading.Thread(target=_run)
        t.start()
        assert composite_started.wait(timeout=2), (
            "the composite never ran -- _auto_revalidate_tick must not take "
            "_merge_lock before running coord.revalidate.revalidate_group (#2829)"
        )
        t.join(timeout=0.3)
        assert t.is_alive(), (
            "_auto_revalidate_tick returned while _merge_lock was still held "
            "externally -- its base-SHA recheck + merge step must block on it"
        )
    finally:
        _merge_lock.release()
    t.join(timeout=5)
    assert not t.is_alive(), "_auto_revalidate_tick did not finish after _merge_lock was released"
    assert result["events"] == []


def _enable_merge_auto(path: Path) -> None:
    """Append a ``merge:`` block turning both ``auto_revalidate`` and
    ``auto_drain`` on. ``VALID_CONFIG`` (the base fixture content) has no
    ``merge:`` key, so appending is safe -- same trick as ``_enable_portal``
    (defined further below in this file).
    """
    path.write_text(
        path.read_text()
        + "\nmerge:\n"
        "  auto_revalidate: true\n"
        "  auto_drain: true\n"
    )


def test_auto_revalidate_does_not_block_other_tick_loop_steps(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2829 review round 1 (blocking finding): a slow/stuck
    ``_auto_revalidate_tick`` composite must not delay Step 3 auto-drain (or
    any other periodic step) for every OTHER repo. Before this fix,
    ``_auto_revalidate_tick`` ran INLINE inside ``_tick_loop`` -- correctly
    with ``_merge_lock`` released, but still as one more `await
    run_in_threadpool(...)` in that coroutine's single sequential `while
    True` body, ahead of Step 3. A slow composite therefore delayed
    ``_auto_drain_tick`` (and everything after it) regardless of the lock
    fix.

    Drives the REAL live ``_tick_loop`` + ``_auto_revalidate_loop`` machinery
    (not a direct call to either tick function) via ``TestClient``'s
    lifespan, stubs ``_auto_revalidate_tick`` to block indefinitely (a
    stand-in for a composite that never goes green) and ``_auto_drain_tick``
    as a call-counting spy, and asserts auto-drain still fires repeatedly on
    its own fast cadence WHILE the revalidate stub is still stuck -- proving
    the two no longer share a sequential await chain (#2829's
    ``_auto_revalidate_loop`` restructure).
    """
    import threading

    _enable_merge_auto(valid_config_path)
    cfg = load_config(valid_config_path)
    assert cfg.merge.auto_revalidate is True
    assert cfg.merge.auto_drain is True

    revalidate_entered = threading.Event()
    release_revalidate = threading.Event()

    def _blocking_revalidate(config):  # noqa: ANN001, ARG001
        revalidate_entered.set()
        # Stand-in for a composite that runs long past any single
        # `_tick_loop` iteration (or never goes green at all) -- released
        # explicitly below, well after this test has already proven the
        # other tick steps did not wait on it.
        release_revalidate.wait(timeout=5)
        return []

    drain_calls: list = []

    def _drain_spy(config):  # noqa: ANN001
        drain_calls.append(config)
        return []

    monkeypatch.setattr(serve_app_module, "_auto_revalidate_tick", _blocking_revalidate)
    monkeypatch.setattr(serve_app_module, "_auto_drain_tick", _drain_spy)

    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
    _quiet_all_other_tick_intervals(monkeypatch)
    # Set AFTER _quiet_all_other_tick_intervals -- that helper zeroes this
    # same env var by default (it's now a sibling loop cadence), and this
    # test needs it actually running.
    monkeypatch.setenv("COORD_AUTO_REVALIDATE_INTERVAL", "0.05")

    app = build_app(SqliteStore(file_db), cfg)

    def _check_while_still_blocked() -> None:
        assert revalidate_entered.wait(timeout=2), (
            "_auto_revalidate_loop never entered _auto_revalidate_tick"
        )
        assert len(drain_calls) >= 2, (
            f"Step 3 auto-drain should have run repeatedly on its own "
            f"~0.05s cadence while the revalidate stub was stuck blocking, "
            f"got {len(drain_calls)} call(s) -- indicates auto-drain is "
            f"still waiting behind the revalidate composite (#2829 review "
            f"round 1)"
        )
        release_revalidate.set()

    _run_tick_loop_briefly(
        app, settle=0.6, mid_run=_check_while_still_blocked, pre_settle=0.4,
    )


# ── #1038: operational-tier audit hooks ──────────────────────────────────────


def test_audit_reconciled_writes_one_row_per_flip(
    rw_db, monkeypatch, tmp_path
) -> None:
    """#1038: _audit_reconciled (the Step-1 reconcile hook) writes one
    operational row per reconciled assignment."""
    from coord.serve_app import _audit_reconciled

    # No coordinator.yml here — pin $COORD_CONFIG to a definitely-absent
    # path so record_audit's level gate is deterministic (defaults to
    # "operational") regardless of the host's real config.
    monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "nonexistent.yml"))

    _audit_reconciled([
        {
            "assignment_id": "aid-1", "issue_number": 7, "repo": "api",
            "type": "work", "to_status": "done", "plan_captured": False,
        },
        {
            "assignment_id": "aid-2", "issue_number": 9, "repo": "api",
            "type": "work", "to_status": "failed", "plan_captured": False,
        },
    ])
    rows = rw_db.execute(
        "SELECT * FROM audit_log WHERE tier='operational' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["category"] == "reconcile"
    assert rows[0]["event_type"] == "passive_reconcile"
    assert rows[0]["actor"] == "daemon"
    assert rows[0]["assignment_id"] == "aid-1"
    assert rows[0]["issue"] == 7
    assert rows[1]["assignment_id"] == "aid-2"


def test_audit_housekeeping_sweep_writes_summary_row(
    rw_db, monkeypatch, tmp_path
) -> None:
    """#1038: _audit_housekeeping_sweep (the Step-4 housekeeping hook)
    writes one operational row summarizing the archival sweep."""
    import json

    from coord.serve_app import _audit_housekeeping_sweep

    monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "nonexistent.yml"))

    _audit_housekeeping_sweep({
        "archived_assignments": 3, "archived_notifications": 5,
        "dry_run": False, "retention_days": 30,
    })
    rows = rw_db.execute(
        "SELECT * FROM audit_log WHERE tier='operational'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["category"] == "housekeeping"
    assert rows[0]["event_type"] == "sweep"
    assert rows[0]["actor"] == "daemon"
    assert json.loads(rows[0]["details_json"])["archived_assignments"] == 3


# ── #769 Phase 1: _milestone_drain_tick ──────────────────────────────────────


def _make_milestone_config(tmp_path: "Path", *, auto_dispatch: bool = True) -> "Path":
    """Write a coordinator.yml with milestone.auto_dispatch set and two
    machines capable of repo "api", and return its path."""
    content = (
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    repos: [api]\n"
        "    repo_paths:\n"
        "      api: /tmp/api\n"
        "  - name: server\n"
        "    host: server.tailnet\n"
        "    repos: [api]\n"
        "    repo_paths:\n"
        "      api: /tmp/api\n"
        "\n"
        f"milestone:\n"
        f"  auto_dispatch: {'true' if auto_dispatch else 'false'}\n"
    )
    p = tmp_path / "coord-milestone.yml"
    p.write_text(content)
    return p


_MILESTONE_TRACKING_BODY = """\
## Work order
- [ ] #762  {group: A}
- [ ] #765  {after: #762}
"""


def test_milestone_auto_dispatch_config_default_off(valid_config_path: "Path") -> None:
    """#769: milestone.auto_dispatch defaults to False when the milestone:
    block is absent."""
    cfg = load_config(valid_config_path)
    assert cfg.milestone.auto_dispatch is False


def test_milestone_drain_tick_noop_when_no_registrations(
    tmp_path: "Path", rw_db
) -> None:
    from coord.serve_app import _milestone_drain_tick

    cfg = load_config(_make_milestone_config(tmp_path))
    assert _milestone_drain_tick(cfg) == []


def test_milestone_drain_tick_dispatches_and_deregisters_when_complete(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#769 acceptance criteria wiring: a registered milestone whose only
    remaining node is now ready gets dispatched and, once nothing is left
    un-terminal, deregistered by the tick."""
    from coord import state
    from coord.serve_app import _milestone_drain_tick

    state.register_milestone_drain(repo_name="api", tracking_issue=100)

    def get_issue(repo, number):
        if number == 100:
            return {
                "number": 100, "title": "tracking", "body": "## Work order\n- [ ] #762\n",
                "state": "OPEN", "milestone": {"number": 9},
            }
        return {"number": 762, "title": "the work", "body": "", "state": "OPEN",
                "milestone": {"number": 9}, "labels": []}

    monkeypatch.setattr("coord.github_ops.get_issue", get_issue)
    monkeypatch.setattr("coord.github_ops.get_open_issues", lambda repo: [
        {"number": 762, "milestone": {"number": 9}}
    ])
    monkeypatch.setattr("coord.dispatch.dispatch", lambda proposal, config, **kw: {"id": "drain-1"})
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **kw: None)
    monkeypatch.setattr("coord.github_ops.check_branch_exists", lambda *a, **kw: False)

    cfg = load_config(_make_milestone_config(tmp_path))
    outcomes = _milestone_drain_tick(cfg)

    assert len(outcomes) == 1
    assert outcomes[0].ok is True
    assert outcomes[0].assignment_id == "drain-1"

    records = state.load_dispatched()
    assert len(records) == 1
    assert records[0]["issue_number"] == 762

    # #762 is still OPEN on GitHub in this fixture (dispatching doesn't close
    # it), so the milestone context still shows it un-terminal -> the drain
    # registration is intentionally left in place for the next tick, exactly
    # like a manual `coord milestone dispatch` re-run would.
    assert state.list_milestone_drains() == [{"repo_name": "api", "tracking_issue": 100}]


def test_milestone_drain_tick_deregisters_fully_terminal_milestone(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """Once every node in the work order is terminal, the tick deregisters
    the milestone — nothing left to keep re-checking."""
    from coord import state
    from coord.serve_app import _milestone_drain_tick

    state.register_milestone_drain(repo_name="api", tracking_issue=100)

    def get_issue(repo, number):
        if number == 100:
            return {
                "number": 100, "title": "tracking", "body": "## Work order\n- [ ] #762\n",
                "state": "OPEN", "milestone": {"number": 9},
            }
        return {"number": 762, "title": "the work", "body": "", "state": "CLOSED",
                "milestone": {"number": 9}, "labels": []}

    monkeypatch.setattr("coord.github_ops.get_issue", get_issue)
    monkeypatch.setattr("coord.github_ops.get_open_issues", lambda repo: [])

    cfg = load_config(_make_milestone_config(tmp_path))
    outcomes = _milestone_drain_tick(cfg)

    assert outcomes == []  # nothing ready — #762 already terminal
    assert state.list_milestone_drains() == []


def test_milestone_drain_tick_fetch_error_does_not_deregister(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """A transient GitHub fetch error must not silently drop the milestone
    from the registry — it should be retried on the next tick."""
    from coord import state
    from coord.serve_app import _milestone_drain_tick

    state.register_milestone_drain(repo_name="api", tracking_issue=100)
    monkeypatch.setattr(
        "coord.github_ops.get_issue",
        lambda repo, number: (_ for _ in ()).throw(RuntimeError("rate limited")),
    )

    cfg = load_config(_make_milestone_config(tmp_path))
    outcomes = _milestone_drain_tick(cfg)

    assert outcomes == []
    assert state.list_milestone_drains() == [{"repo_name": "api", "tracking_issue": 100}]


# ── #1412 deliverable 2: _milestone_progress_tick ────────────────────────────


def test_milestone_progress_tick_noop_when_no_registrations(
    tmp_path: "Path", rw_db
) -> None:
    from coord.serve_app import _milestone_progress_tick

    cfg = load_config(_make_milestone_config(tmp_path))
    assert _milestone_progress_tick(cfg) == []


def test_milestone_progress_tick_writes_progress_section(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """A registered milestone with one terminal and one ready node gets a
    freshly-rendered `## Progress` section spliced into its tracking-issue
    body — never touching `## Work order`."""
    from coord import state
    from coord.milestone_order import ProgressStatus, parse_progress, parse_work_order
    from coord.serve_app import _milestone_progress_tick

    state.register_milestone_drain(repo_name="api", tracking_issue=100)

    tracking_body = (
        "## Work order\n"
        "- #762  {group: A}\n"
        "- #765  {after: #762}\n"
    )

    def get_issue(repo, number):
        if number == 100:
            return {
                "number": 100, "title": "tracking", "body": tracking_body,
                "state": "OPEN", "milestone": {"number": 9},
            }
        return {
            "number": number, "title": "the work", "body": "",
            "state": "CLOSED" if number == 762 else "OPEN",
            "milestone": {"number": 9}, "labels": [],
        }

    monkeypatch.setattr("coord.github_ops.get_issue", get_issue)
    monkeypatch.setattr(
        "coord.github_ops.get_open_issues",
        lambda repo: [{"number": 765, "milestone": {"number": 9}}],
    )
    updates: list = []
    monkeypatch.setattr(
        "coord.github_ops.update_issue_body",
        lambda repo, issue, body: updates.append((repo, issue, body)),
    )

    cfg = load_config(_make_milestone_config(tmp_path, auto_dispatch=False))
    updated = _milestone_progress_tick(cfg)

    assert updated == ["api#100"]
    assert len(updates) == 1
    call_repo, call_issue, call_body = updates[0]
    assert call_repo == "acme/api"
    assert call_issue == 100
    assert parse_progress(call_body) == (
        ProgressStatus(762, "done", "A"),
        ProgressStatus(765, "ready"),
    )
    assert parse_work_order(call_body) == parse_work_order(tracking_body)


def test_milestone_progress_tick_unchanged_state_is_a_noop(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """Re-running against a body whose `## Progress` already matches live
    state must not touch GitHub at all — not even the timestamp."""
    from coord import state
    from coord.milestone_order import (
        compute_progress,
        parse_work_order,
        ready_frontier,
        render_progress,
        replace_progress_section,
    )
    from coord.models import Board
    from coord.serve_app import _milestone_progress_tick

    state.register_milestone_drain(repo_name="api", tracking_issue=100)

    base_body = "## Work order\n- #762\n"
    wo = parse_work_order(base_body)
    frontier = ready_frontier(
        wo, Board(), repo_name="api", repo_github="acme/api", terminal_issues=set(),
    )
    statuses = compute_progress(wo, frontier, set())
    tracking_body = replace_progress_section(
        base_body, render_progress(statuses, generated_at="2020-01-01T00:00:00Z")
    )

    def get_issue(repo, number):
        if number == 100:
            return {
                "number": 100, "title": "tracking", "body": tracking_body,
                "state": "OPEN", "milestone": {"number": 9},
            }
        return {
            "number": number, "title": "the work", "body": "", "state": "OPEN",
            "milestone": {"number": 9}, "labels": [],
        }

    monkeypatch.setattr("coord.github_ops.get_issue", get_issue)
    monkeypatch.setattr(
        "coord.github_ops.get_open_issues",
        lambda repo: [{"number": 762, "milestone": {"number": 9}}],
    )
    monkeypatch.setattr(
        "coord.github_ops.update_issue_body",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not write")),
    )

    cfg = load_config(_make_milestone_config(tmp_path))
    updated = _milestone_progress_tick(cfg)

    assert updated == []


def test_milestone_progress_tick_runs_even_when_auto_dispatch_is_off(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#1412: refreshing an epic's status is read-only and must not be
    gated on milestone.auto_dispatch — an operator can watch live status
    without opting into auto-dispatch."""
    from coord import state
    from coord.serve_app import _milestone_progress_tick

    state.register_milestone_drain(repo_name="api", tracking_issue=100)

    def get_issue(repo, number):
        if number == 100:
            return {
                "number": 100, "title": "tracking", "body": "## Work order\n- #762\n",
                "state": "OPEN", "milestone": {"number": 9},
            }
        return {
            "number": number, "title": "the work", "body": "", "state": "OPEN",
            "milestone": {"number": 9}, "labels": [],
        }

    monkeypatch.setattr("coord.github_ops.get_issue", get_issue)
    monkeypatch.setattr(
        "coord.github_ops.get_open_issues",
        lambda repo: [{"number": 762, "milestone": {"number": 9}}],
    )
    updates: list = []
    monkeypatch.setattr(
        "coord.github_ops.update_issue_body",
        lambda repo, issue, body: updates.append((repo, issue, body)),
    )

    cfg = load_config(_make_milestone_config(tmp_path, auto_dispatch=False))
    assert cfg.milestone.auto_dispatch is False
    updated = _milestone_progress_tick(cfg)

    assert updated == ["api#100"]
    assert len(updates) == 1


def test_milestone_progress_tick_fetch_error_does_not_crash_other_entries(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """A per-milestone fetch error must not silence progress-sync for the
    other registered milestones (mirrors _milestone_drain_tick's isolation)."""
    from coord import state
    from coord.serve_app import _milestone_progress_tick

    state.register_milestone_drain(repo_name="api", tracking_issue=100)
    monkeypatch.setattr(
        "coord.github_ops.get_issue",
        lambda repo, number: (_ for _ in ()).throw(RuntimeError("rate limited")),
    )

    cfg = load_config(_make_milestone_config(tmp_path))
    updated = _milestone_progress_tick(cfg)

    assert updated == []
    # Fetch failures don't deregister — the milestone stays registered for
    # the next tick's retry, same posture as _milestone_drain_tick.
    assert state.list_milestone_drains() == [{"repo_name": "api", "tracking_issue": 100}]


def test_serve_milestone_drain_registers_row(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/milestone-drain",
            json={"repo_name": "api", "tracking_issue": 100},
        )
    assert resp.status_code == 200
    from coord import state

    assert state.list_milestone_drains() == [{"repo_name": "api", "tracking_issue": 100}]


def test_register_milestone_drain_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.register_milestone_drain(repo_name="api", tracking_issue=100)
    assert captured["path"] == "/milestone-drain"
    assert captured["payload"] == {"repo_name": "api", "tracking_issue": 100}
    # Routed → no local row created.
    assert state.list_milestone_drains() == []


def test_register_milestone_drain_unset_writes_local(coord_db, monkeypatch):
    from coord import state

    state.register_milestone_drain(repo_name="api", tracking_issue=100)
    assert state.list_milestone_drains() == [{"repo_name": "api", "tracking_issue": 100}]
    # Idempotent re-registration.
    state.register_milestone_drain(repo_name="api", tracking_issue=100)
    assert state.list_milestone_drains() == [{"repo_name": "api", "tracking_issue": 100}]


def test_deregister_milestone_drain_removes_only_matching_entry(coord_db):
    from coord import state

    state.register_milestone_drain(repo_name="api", tracking_issue=100)
    state.register_milestone_drain(repo_name="web", tracking_issue=200)
    state.deregister_milestone_drain(repo_name="api", tracking_issue=100)
    assert state.list_milestone_drains() == [{"repo_name": "web", "tracking_issue": 200}]


# ── #1037: GET /audit + audit_recent_count on /board ────────────────────────

def test_serve_get_audit_returns_entries_newest_first(
    file_db: Path, valid_config_path: Path, rw_db
):
    from coord.audit import record_audit

    record_audit(tier="business", category="test", event_type="test_passed", actor="user", summary="a", ts=1000.0)
    record_audit(tier="business", category="test", event_type="test_failed", actor="user", summary="b", ts=1001.0)

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.get("/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["summary"] for e in body["entries"]] == ["b", "a"]
    assert body["has_more"] is False
    assert body["next_cursor"] is None


def test_serve_get_audit_filters_plumb_through(
    file_db: Path, valid_config_path: Path, rw_db
):
    from coord.audit import record_audit

    record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="a", repo="api", issue=1, ts=1000.0)
    record_audit(tier="business", category="test", event_type="test_passed", actor="user", summary="b", repo="web", issue=2, ts=1001.0)

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.get("/audit", params={"category": "merge", "repo": "api"})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["summary"] == "a"


def test_serve_get_audit_pagination_via_cursor(
    file_db: Path, valid_config_path: Path, rw_db
):
    from coord.audit import record_audit

    for i in range(3):
        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary=f"row {i}", ts=1000.0 + i,
        )

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        page1 = cli.get("/audit", params={"limit": 2}).json()
        assert [e["summary"] for e in page1["entries"]] == ["row 2", "row 1"]
        assert page1["has_more"] is True

        page2 = cli.get(
            "/audit", params={"limit": 2, "cursor": page1["next_cursor"]}
        ).json()
        assert [e["summary"] for e in page2["entries"]] == ["row 0"]
        assert page2["has_more"] is False


def test_serve_get_audit_bad_query_param_400(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.get("/audit", params={"issue": "not-an-int"})
    assert resp.status_code == 400


def test_audit_recent_count_in_board_payload(file_db: Path, valid_config_path: Path):
    import time as _time

    conn = sqlite3.connect(str(file_db))
    conn.execute(
        "INSERT INTO audit_log (ts, tier, category, event_type, actor, summary) "
        "VALUES (?, 'business', 'test', 'test_passed', 'user', 'recent')",
        (_time.time(),),
    )
    conn.execute(
        "INSERT INTO audit_log (ts, tier, category, event_type, actor, summary) "
        "VALUES (?, 'business', 'test', 'test_passed', 'user', 'stale')",
        (_time.time() - 100_000,),
    )
    conn.commit()
    conn.close()

    proj = SqliteStore(file_db).board_projection()
    assert proj["audit_recent_count"] == 1

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    assert board["audit_recent_count"] == 1


# ── #<issue> Part 1: threadpool — board() does not block the event loop ──────


def test_board_handler_does_not_block_event_loop(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Part 1 regression: board() offloads its synchronous body to a threadpool
    so the async event loop stays free for concurrent requests.

    Monkeypatches ``SqliteStore.board_projection`` to sleep for SLOW_DELAY
    seconds (simulating a large board with ~465 issues), then fires /board and
    /healthz concurrently via ``httpx.AsyncClient`` sharing a single event loop.
    If board() were still blocking the event loop, /healthz would be serialised
    behind it and take >= SLOW_DELAY.  With the threadpool offload, /healthz
    returns almost immediately while the slow computation runs in a worker
    thread.
    """
    import time as _time

    import httpx

    SLOW_DELAY = 0.3  # seconds — clear signal without making the test slow

    original_projection = SqliteStore.board_projection

    def slow_projection(self):  # noqa: ANN001
        _time.sleep(SLOW_DELAY)
        return original_projection(self)

    monkeypatch.setattr(SqliteStore, "board_projection", slow_projection)
    # Stub build_board() so the threadpool doesn't hit the thread-bound
    # in-memory connection installed by the coord_db fixture (same cross-thread
    # restriction as in test_serve_board_picks_up_config_hand_edit).
    from coord.models import Board as _Board
    monkeypatch.setattr("coord.state.build_board", lambda: _Board())

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            # Start /board — it will sleep SLOW_DELAY in the threadpool.
            board_task = asyncio.create_task(cli.get("/board"))
            # Brief yield so the board request is dispatched and enters
            # run_in_threadpool before we time /healthz.
            await asyncio.sleep(0.05)

            start = _time.monotonic()
            healthz_resp = await cli.get("/healthz")
            elapsed = _time.monotonic() - start

            board_resp = await board_task
        return elapsed, board_resp.status_code, healthz_resp.status_code

    elapsed, board_status, healthz_status = asyncio.run(_run())

    assert board_status == 200
    assert healthz_status == 200
    # /healthz should return almost instantly, not blocked behind SLOW_DELAY.
    # Allow 80% of SLOW_DELAY as threshold to absorb CI scheduling jitter.
    threshold = SLOW_DELAY * 0.8
    assert elapsed < threshold, (
        f"/healthz took {elapsed:.3f}s (threshold {threshold:.3f}s) — "
        f"board() appears to be blocking the event loop"
    )


# ── #<issue> Part 1: payload shape — _build() returns same projection ────────


def test_board_payload_regression(file_db: Path, valid_config_path: Path):
    """Regression: the threaded _build() function must return the same core
    fields as a direct SqliteStore.board_projection() call, plus the
    server-side enrichment keys (merge_plan, issue_stage_projection, etc.).

    Seeded by _make_file_db: one 'done' work assignment, one 'done' review,
    one machine, round_number=7.
    """
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)

    with TestClient(app) as cli:
        board = cli.get("/board").json()

    # ── Core projection fields (produced by SqliteStore.board_projection) ──
    assert board["schema_version"] == 1
    assert board["round_number"] == 7
    work = next((a for a in board["assignments"] if a["assignment_id"] == "work1"), None)
    assert work is not None, "work1 assignment missing from /board payload"
    assert work["status"] == "done"
    assert work["files_allowed"] == ["a.py", "b.py"]
    assert "briefing" not in work, "briefing must be stripped from the wire payload"

    # ── Server-side enrichment (computed by _build() in the threadpool) ────
    # Every key must be present even when its computation short-circuits to []
    # via the fail-open except blocks — the TUI asserts on their presence.
    for key in (
        "merge_plan",
        "merge_staging",
        "sibling_overlap_warnings",
        "issue_stage_projection",
        "milestone_work_orders",
        "children",
        "plan_roster",
        "plan_roster_supported",
        "goal_header",
        "roll_pending",
    ):
        assert key in board, f"server-enrichment key '{key}' missing from /board"
    assert board["plan_roster_supported"] is True


# ── #<issue> Part 2: cache — burst polls reuse cached projection ─────────────


def test_board_cache_ttl_serves_repeated_requests(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Part 2 (cache): repeated GET /board requests within the TTL window must
    return the cached result without calling board_projection() again.

    Counts how many times board_projection() is invoked; asserts it's called
    exactly once for two back-to-back requests (the second hits the cache).
    """
    call_count = 0
    original_projection = SqliteStore.board_projection

    def counting_projection(self):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return original_projection(self)

    monkeypatch.setattr(SqliteStore, "board_projection", counting_projection)
    # Long TTL so the second request definitely hits the cache.
    monkeypatch.setenv("COORD_BOARD_CACHE_TTL", "60")

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        r1 = cli.get("/board")
        r2 = cli.get("/board")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert call_count == 1, (
        f"board_projection() called {call_count}× — expected 1 "
        f"(second request should hit the TTL cache)"
    )
    # Both responses should have the same payload.
    assert r1.json()["round_number"] == r2.json()["round_number"]


def test_board_cache_busted_by_post_board(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch: pytest.MonkeyPatch
):
    """Part 2 (cache bust): POST /board must invalidate the cache so the next
    GET /board recomputes rather than serving stale data.

    Uses a 60 s TTL so the cache is definitively warm between requests, making
    the bust signal unambiguous.
    """
    from coord.client import serialize_board
    from coord.models import Assignment, Board as _Board

    call_count = 0
    original_projection = SqliteStore.board_projection

    def counting_projection(self):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return original_projection(self)

    monkeypatch.setattr(SqliteStore, "board_projection", counting_projection)
    monkeypatch.setenv("COORD_BOARD_CACHE_TTL", "60")

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)

    dummy_board = _Board(
        round_number=99,
        completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=1,
                issue_title="cache bust test", assignment_id="bust1", status="done",
            ),
        ],
    )

    with TestClient(app) as cli:
        # First GET — cache miss, populates the cache (call_count → 1).
        r1 = cli.get("/board")
        assert r1.status_code == 200
        assert call_count == 1

        # POST /board — must bust the cache.
        resp = cli.post("/board", json=serialize_board(dummy_board))
        assert resp.status_code == 200 and resp.json()["ok"] is True

        # Second GET — cache was busted, must recompute (call_count → 2).
        r2 = cli.get("/board")
        assert r2.status_code == 200

    assert call_count == 2, (
        f"board_projection() called {call_count}× — expected 2 "
        f"(initial GET + recompute after POST /board cache bust)"
    )


# ── WAL checkpoint tick ────────────────────────────────────────────────────────


def test_wal_checkpoint_tick_runs_against_wal_db(
    valid_config_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """_wal_checkpoint_tick runs PRAGMA wal_checkpoint(TRUNCATE) against the live
    coord.db (WAL mode) and returns the three integers SQLite reports: busy, log,
    checkpointed.

    This is the black-box acceptance test for the WAL CPU-spike fix: the
    root cause is that SQLite's passive autocheckpoint never truncates the WAL
    file while an open reader exists, so the WAL grows unboundedly under
    continuous /board polling.  PRAGMA wal_checkpoint(TRUNCATE) is the
    specifically-correct remedy — it waits for a quiet moment and then zeros
    the file rather than leaving a filled-but-active region.
    """
    import sqlite3

    from coord import db
    from coord.config import load as load_config
    from coord.db import _ensure_schema
    from coord.serve_app import _wal_checkpoint_tick

    # Use a real file-backed DB so WAL mode is actually engaged.
    wal_db_path = tmp_path / "wal_test.db"
    conn = sqlite3.connect(str(wal_db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    db.override_connection(conn)

    cfg = load_config(valid_config_path)
    result = _wal_checkpoint_tick(cfg)

    # The checkpoint must complete on a quiet DB: busy=0, not skipped, no error.
    assert result.get("skipped") is not True, (
        "_wal_checkpoint_tick skipped on a WAL-mode DB — journal_mode detection is wrong"
    )
    assert result.get("error") is not True, (
        f"_wal_checkpoint_tick reported an error: {result}"
    )
    assert result["busy"] == 0, (
        f"TRUNCATE checkpoint was blocked (busy={result['busy']}) on an idle DB"
    )
    assert "log" in result
    assert "checkpointed" in result


def test_wal_checkpoint_tick_skips_memory_db(
    valid_config_path: Path, monkeypatch
) -> None:
    """_wal_checkpoint_tick must be a no-op against an in-memory (test) database.

    SQLite's :memory: databases don't support WAL mode; calling
    PRAGMA wal_checkpoint there would be a no-op but confusing.  The helper
    detects the journal_mode and returns {skipped: True} so the tick loop
    sees it as a benign non-event.
    """
    from coord.config import load as load_config
    from coord.serve_app import _wal_checkpoint_tick

    # The coord_db autouse fixture has already installed an :memory: connection.
    cfg = load_config(valid_config_path)
    result = _wal_checkpoint_tick(cfg)

    assert result.get("skipped") is True, (
        f"_wal_checkpoint_tick expected skipped=True on :memory: DB, got {result}"
    )
    assert result["busy"] == 0
    assert result["log"] == 0
    assert result["checkpointed"] == 0


def test_wal_checkpoint_tick_honours_zero_interval(
    valid_config_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """COORD_WAL_CHECKPOINT_INTERVAL=0 must disable the checkpoint entirely.

    When the env var is 0 the _tick_loop guard condition (interval > 0) is
    false, so _wal_checkpoint_tick is never called.  This test verifies the
    helper itself isn't special-cased — it still runs if called directly —
    but confirms the env variable is wired up by checking via a monkeypatched
    call counter.
    """
    import sqlite3

    from coord import db
    from coord.config import load as load_config
    from coord.db import _ensure_schema
    from coord.serve_app import _wal_checkpoint_tick

    # Use a real WAL-mode DB so the call itself can succeed.
    wal_db_path = tmp_path / "wal_test2.db"
    conn = sqlite3.connect(str(wal_db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    db.override_connection(conn)

    cfg = load_config(valid_config_path)
    monkeypatch.setenv("COORD_WAL_CHECKPOINT_INTERVAL", "0")

    # The helper itself doesn't read the env var (the _tick_loop guards it)
    # — so a direct call still succeeds.  This confirms the env-tunable
    # lives in the right place (the interval initialization in _lifespan,
    # not inside _wal_checkpoint_tick itself).
    result = _wal_checkpoint_tick(cfg)
    assert result.get("skipped") is not True
    assert result.get("error") is not True


# ── #2824: Step 3d (portal sync) must actually be ENTERED, not just "the
# heartbeat looks fine" ────────────────────────────────────────────────────
#
# #2824's bug report: on a live daemon with portal.enabled=true and the
# default 60s interval, Step 3d never ran — no heartbeat advance, no pull, and
# NOTHING in the journal (not even a failure) since a false guard is
# indistinguishable from a quiet successful pass. A test that only asserts
# "the heartbeat advanced" against a stubbed clock would not have caught that
# class of bug (a `_portal_sync_tick` stub can make the heartbeat "advance"
# trivially without the guard ever running). These tests instead assert Step
# 3d's own body — `_portal_sync_tick` — is actually CALLED, including in the
# specific case #2824 flags as the likely trigger and that nothing else
# covered: after `_refresh_config()` has reloaded the config at least once.

def _enable_portal(path: Path) -> None:
    """Append a fully-configured, enabled ``portal:`` block onto *path*."""
    path.write_text(
        path.read_text()
        + "\nportal:\n"
        "  enabled: true\n"
        '  base_url: "https://intake.example.com"\n'
        '  bridge_client_id: "cid"\n'
        '  bridge_client_secret: "secret"\n'
    )


def _run_tick_loop_briefly(
    app, *, settle: float = 0.3, mid_run=None, pre_settle: float | None = None
) -> None:
    """Start *app*'s lifespan (spawning ``_tick_loop`` for real) and let it
    run a few iterations against a real asyncio event loop, then tear down.

    Every interval this test doesn't care about is silenced via env vars set
    by the caller before this runs — this only sleeps long enough for the
    ~0.05s reconcile/portal intervals under test to fire several times.

    *mid_run*, given, runs partway through the settle window (after
    *pre_settle* seconds, default half of *settle*) while the lifespan is
    still up — e.g. to assert on pre-edit state and then make an on-disk
    config edit — so a caller that needs to interleave something mid-tick
    doesn't have to hand-roll its own ``with TestClient(app): time.sleep(...)``
    block.
    """
    with TestClient(app):
        if mid_run is None:
            time.sleep(settle)
            return
        pre = settle / 2 if pre_settle is None else pre_settle
        time.sleep(pre)
        mid_run()
        time.sleep(settle - pre)


def _quiet_all_other_tick_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero out every ``_tick_loop`` cadence Step 3d isn't testing.

    Keeps the real ``_lifespan``/``_tick_loop`` machinery honest (this is not
    a stub of the loop, just the *other* steps in it) while keeping the test
    fast and free of unrelated side effects (gh calls, notifier fan-out, …).
    """
    for env_var in (
        "COORD_NOTIFY_DRAIN_INTERVAL",
        "COORD_HOUSEKEEPING_INTERVAL",
        "COORD_RECONCILE_MERGES_INTERVAL",
        "COORD_WORKTREE_CLEAN_INTERVAL",
        "COORD_WAL_CHECKPOINT_INTERVAL",
        "COORD_GATE_REFRESH_INTERVAL",
        "COORD_HEALTH_POLL_INTERVAL",
        "COORD_PHANTOM_HEAL_INTERVAL",
        "COORD_NOTIFIER_INTERVAL",
        "COORD_AUTO_REVALIDATE_INTERVAL",
    ):
        monkeypatch.setenv(env_var, "0")


def _stub_portal_sync_tick(
    monkeypatch: pytest.MonkeyPatch,
    *,
    moved: bool = False,
    errors: tuple[str, ...] = (),
    heartbeat_ok: bool = True,
    summary: str = "stub summary",
) -> list:
    """Replace ``_portal_sync_tick`` with a spy and return its call log.

    Stubbing the module-level function (not ``coord.portal_sync.sync_tick``)
    keeps this test scoped to Step 3d's own guard/dispatch in
    ``_tick_loop`` — whether the guard reaches into ``_portal_sync_tick`` at
    all — rather than the sync bridge's internals, which #2824 explicitly
    rules out as the culprit ("`coord/portal_sync.py` is NOT implicated").

    The keyword arguments shape the ``SyncResult``-alike the stub hands back,
    so #2862's tests can drive Step 3d's *reporting* branches (a clean pass
    vs. a pass whose heartbeat failed) without reaching into the bridge
    either.  Defaults reproduce the pre-#2862 stub exactly.
    """
    calls: list = []
    result_errors = list(errors)

    def _stub(config):  # noqa: ANN001
        calls.append(config)

        class _Result:
            pass

        result = _Result()
        result.moved = moved
        result.errors = result_errors
        result.heartbeat_ok = heartbeat_ok
        result.summary = lambda: summary
        return result

    monkeypatch.setattr(serve_app_module, "_portal_sync_tick", _stub)
    return calls


def test_portal_sync_step_3d_is_entered_when_enabled_and_due(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: portal.enabled=true + interval elapsed → Step 3d actually runs.

    Asserts entry into ``_portal_sync_tick`` itself (not just that some
    "heartbeat" value moved), because a false guard and a quiet successful
    pass look identical from the outside otherwise (#2824's whole point).
    """
    _enable_portal(valid_config_path)
    cfg = load_config(valid_config_path)
    assert cfg.portal.enabled is True

    calls = _stub_portal_sync_tick(monkeypatch)
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
    monkeypatch.setenv("COORD_PORTAL_SYNC_INTERVAL", "0.05")
    _quiet_all_other_tick_intervals(monkeypatch)

    app = build_app(SqliteStore(file_db), cfg)
    _run_tick_loop_briefly(app)

    assert calls, "Step 3d (_portal_sync_tick) was never entered"


def test_portal_sync_step_3d_is_entered_after_refresh_config_enables_it(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2824's suspected trigger: Step 3d must fire once ``_refresh_config()``
    has picked up an on-disk edit that flips ``portal.enabled`` on — not just
    when the startup config already had it enabled.

    Before this issue, no test covered ``_tick_loop`` reading `config.portal`
    through an ACTUAL ``_refresh_config()`` reload (every existing config-
    reload test exercised `_refresh_config`/`_reload_config_if_stale`
    directly, never through a live `_tick_loop`), so a regression where the
    guard's `config.portal` read stops tracking the refreshed object would
    have shipped silently.
    """
    cfg = load_config(valid_config_path)
    assert cfg.portal.enabled is False  # no portal: block yet

    calls = _stub_portal_sync_tick(monkeypatch)
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
    monkeypatch.setenv("COORD_PORTAL_SYNC_INTERVAL", "0.05")
    _quiet_all_other_tick_intervals(monkeypatch)

    def _enable_partway_through() -> None:
        assert not calls, (
            "Step 3d ran before portal.enabled was even turned on — "
            "the rest of this test's assertion would be meaningless"
        )
        # The #2824 trigger: enable the portal via an on-disk edit, exactly
        # like an operator turning the feature on against a running daemon —
        # _refresh_config() must pick this up on the daemon's own tick cadence.
        _enable_portal(valid_config_path)
        _bump_mtime(valid_config_path)

    app = build_app(SqliteStore(file_db), cfg)
    _run_tick_loop_briefly(app, settle=0.9, pre_settle=0.3, mid_run=_enable_partway_through)

    assert calls, (
        "Step 3d (_portal_sync_tick) never fired after _refresh_config() "
        "reloaded a config with portal.enabled newly true — a stale `config` "
        "read (or a guard exception swallowed into 'disabled') would look "
        "exactly like this"
    )


# ── #2862: Step 3d went quiet a SECOND time, with #2824's cause ruled out ───
#
# The daemon on dellserver had #2824's fix live, no `~/.coord/client.toml`, the
# right `--config` on argv and `portal.enabled: true` in that exact file — and
# `portal_sync_state.last_heartbeat_at` still only ever moved when a human ran
# `coord portal sync` by hand.  The debugging note at Step 3d asked whoever hit
# this next to check what `config` actually *is*; that check came back clean.
#
# The answer was that the question could not be answered from the journal at
# all: `coord serve` configures logging nowhere, and `uvicorn.run(
# log_level="info")` configures only the `uvicorn*` loggers, so the root logger
# stays handler-less at WARNING and every `log.info(...)` in `_tick_loop` — Step
# 3d's per-pass summary included — is dropped before it is formatted.  "Zero
# portal-related lines in the journal since the daemon started" was true whether
# the bridge ran or not, so it was never evidence of anything.
#
# The tests below pin the three halves of the fix: the logging setup itself, the
# gate announcing its own state, and every pass emitting exactly one line at a
# level that matches whether it worked.


@contextlib.contextmanager
def _isolated_coord_logger():
    """Restore the process-wide ``coord`` logger after a test configures it.

    ``configure_daemon_logging`` mutates global ``logging`` state on purpose
    (that IS the deliverable), so a test that calls it must put the logger
    back or it leaks a stderr handler into every later test in the session.
    """
    log = logging.getLogger(serve_app_module.DAEMON_LOG_LOGGER)
    saved_handlers, saved_level = list(log.handlers), log.level
    saved_propagate = log.propagate
    try:
        yield log
    finally:
        log.handlers[:] = saved_handlers
        log.setLevel(saved_level)
        log.propagate = saved_propagate


def test_daemon_logging_makes_coord_info_records_reach_a_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2862's root cause, asserted directly: before this, `coord.serve` INFO
    records were discarded by `isEnabledFor(INFO) == False` and the root logger
    had no handler to receive them even if they had not been.

    Also asserts the fix survives ``uvicorn.run``'s own ``dictConfig`` call,
    which happens *after* ours and is the reason this is easy to get wrong.
    """
    monkeypatch.delenv(serve_app_module.DAEMON_LOG_LEVEL_ENV, raising=False)
    with _isolated_coord_logger() as coord_log:
        # Baseline: the pre-#2862 world. Nothing configured, root at WARNING.
        coord_log.handlers[:] = []
        coord_log.setLevel(logging.NOTSET)
        assert not logging.getLogger("coord.serve").isEnabledFor(logging.INFO), (
            "the premise of this test — that an unconfigured `coord.serve` "
            "drops INFO — no longer holds; #2862's diagnosis needs revisiting"
        )

        serve_app_module.configure_daemon_logging()

        # uvicorn.run() does this to the logging system right after we run.
        from uvicorn.config import LOGGING_CONFIG  # noqa: PLC0415

        logging.config.dictConfig(LOGGING_CONFIG)

        assert logging.getLogger("coord.serve").isEnabledFor(logging.INFO)
        installed = [
            h
            for h in coord_log.handlers
            if getattr(h, "name", None) == serve_app_module.DAEMON_LOG_HANDLER_NAME
        ]
        assert len(installed) == 1, (
            "uvicorn's dictConfig removed (or duplicated) the daemon handler"
        )


def test_daemon_logging_is_idempotent_and_survives_a_bad_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd ``COORD_LOG_LEVEL`` in a systemd EnvironmentFile must not stop
    the daemon booting, and repeat calls must not stack duplicate handlers
    (which would print every journal line twice)."""
    with _isolated_coord_logger() as coord_log:
        coord_log.handlers[:] = []
        monkeypatch.setenv(serve_app_module.DAEMON_LOG_LEVEL_ENV, "not-a-level")
        serve_app_module.configure_daemon_logging()
        assert coord_log.level == logging.INFO

        monkeypatch.setenv(serve_app_module.DAEMON_LOG_LEVEL_ENV, "WARNING")
        serve_app_module.configure_daemon_logging()
        serve_app_module.configure_daemon_logging()
        assert coord_log.level == logging.WARNING
        assert (
            len(
                [
                    h
                    for h in coord_log.handlers
                    if getattr(h, "name", None)
                    == serve_app_module.DAEMON_LOG_HANDLER_NAME
                ]
            )
            == 1
        )


def test_step_3d_keeps_firing_after_coordinator_yml_is_rewritten_under_it(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2862 acceptance: rewriting ``coordinator.yml`` under a running daemon
    must not silently disable Step 3d.

    The live config file WAS rewritten on disk in the window the issue reports
    (unrelated repo-genesis work), so a reload definitely happened — making
    "the reload swapped in a `config` whose portal is off" the leading suspect
    before the logging cause was found.  The existing #2824 test only covers
    off → on; this covers the on → rewrite → still-on direction, and asserts
    the daemon really did reload (a fresh `Config` object reaches Step 3d)
    rather than trivially passing because nothing changed.
    """
    _enable_portal(valid_config_path)
    cfg = load_config(valid_config_path)
    assert cfg.portal.enabled is True

    calls = _stub_portal_sync_tick(monkeypatch)
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
    monkeypatch.setenv("COORD_PORTAL_SYNC_INTERVAL", "0.05")
    _quiet_all_other_tick_intervals(monkeypatch)

    before: list = []

    def _rewrite_config_mid_run() -> None:
        assert calls, "Step 3d never fired even before the rewrite"
        before.extend(calls)
        # A byte-identical round-trip rewrite — what `coord repo add` does to
        # this file — not an edit that touches the `portal:` block at all.
        valid_config_path.write_text(valid_config_path.read_text())
        _bump_mtime(valid_config_path)

    app = build_app(SqliteStore(file_db), cfg)
    _run_tick_loop_briefly(
        app, settle=1.5, pre_settle=0.5, mid_run=_rewrite_config_mid_run
    )

    after = calls[len(before):]
    assert after, (
        "Step 3d stopped firing once coordinator.yml was rewritten under the "
        "running daemon — exactly the #2862 symptom"
    )
    assert all(c.portal.enabled for c in after), (
        "the reloaded config Step 3d ran against has portal.enabled false, "
        "even though the on-disk file still says true"
    )
    assert any(c is not before[-1] for c in after), (
        "no reloaded Config object ever reached Step 3d — the daemon never "
        "picked the rewrite up, so this test proved nothing about reloads"
    )


def test_step_3d_announces_itself_when_the_gate_is_off(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A `False` boolean gate is silent by design — so #2862 makes the gate log
    its own state.  Announced once on change, NOT once per 30 s tick.
    """
    cfg = load_config(valid_config_path)
    assert cfg.portal.enabled is False  # no portal: block in the fixture

    calls = _stub_portal_sync_tick(monkeypatch)
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
    monkeypatch.setenv("COORD_PORTAL_SYNC_INTERVAL", "0.05")
    _quiet_all_other_tick_intervals(monkeypatch)

    app = build_app(SqliteStore(file_db), cfg)
    with caplog.at_level(logging.INFO, logger="coord.serve"):
        _run_tick_loop_briefly(app, settle=0.4)

    assert not calls, "Step 3d ran with portal.enabled false"
    disabled = [
        r.getMessage() for r in caplog.records if "Step 3d DISABLED" in r.getMessage()
    ]
    assert disabled, (
        "the disabled gate said nothing — which is precisely how #2824 and "
        "#2862 both cost a full debugging round"
    )
    assert len(disabled) == 1, (
        f"the gate re-announced itself on every tick ({len(disabled)}x) — that "
        "is a log flood, not instrumentation"
    )
    assert "portal.enabled is false" in disabled[0]
    assert str(valid_config_path) in disabled[0], (
        "the DISABLED line must name the config file the daemon is actually "
        "running on — that identification is what #2824 turned on"
    )


def test_step_3d_logs_one_line_per_pass_when_it_is_healthy(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The healthy steady state — a pass that heartbeats and moves nothing —
    must be visible, since its *absence* is the whole of #2824 and #2862."""
    _enable_portal(valid_config_path)
    cfg = load_config(valid_config_path)

    _stub_portal_sync_tick(monkeypatch, summary="pulled=0 pushed=0 heartbeat=ok")
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
    monkeypatch.setenv("COORD_PORTAL_SYNC_INTERVAL", "0.05")
    _quiet_all_other_tick_intervals(monkeypatch)

    app = build_app(SqliteStore(file_db), cfg)
    with caplog.at_level(logging.INFO, logger="coord.serve"):
        _run_tick_loop_briefly(app, settle=0.4)

    assert any(
        "Step 3d ENABLED" in r.getMessage() for r in caplog.records
    ), "the daemon never said the bridge was on"
    passes = [
        r for r in caplog.records if "heartbeat=ok" in r.getMessage()
    ]
    assert passes, "a healthy portal-sync pass logged nothing at all"
    assert all(r.levelno == logging.INFO for r in passes), (
        "a healthy pass should be INFO — WARNING is reserved for a pass that "
        "actually failed, or the level stops meaning anything"
    )


def test_a_failed_heartbeat_pass_is_reported_at_warning(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pre-#2862 code could never report this.

    ``coord.portal_sync.sync_tick`` appends to ``SyncResult.errors`` whenever
    the heartbeat raises, so ``errors`` is non-empty for every real heartbeat
    failure — which meant the ``if ... or portal_result.errors: log.info(...)``
    branch always won and the ``elif not portal_result.heartbeat_ok:
    log.warning(...)`` written for exactly this case was unreachable.  Combined
    with INFO being discarded (see the logging tests above), the single most
    important thing Step 3d can say — *the portal is showing a status nothing
    is refreshing* — was structurally unable to reach the journal.
    """
    _enable_portal(valid_config_path)
    cfg = load_config(valid_config_path)

    _stub_portal_sync_tick(
        monkeypatch,
        errors=("heartbeat: 401 Unauthorized",),
        heartbeat_ok=False,
        summary="pulled=0 pushed=0 heartbeat=FAILED errors=1",
    )
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
    monkeypatch.setenv("COORD_PORTAL_SYNC_INTERVAL", "0.05")
    _quiet_all_other_tick_intervals(monkeypatch)

    app = build_app(SqliteStore(file_db), cfg)
    with caplog.at_level(logging.INFO, logger="coord.serve"):
        _run_tick_loop_briefly(app, settle=0.4)

    failed = [r for r in caplog.records if "heartbeat=FAILED" in r.getMessage()]
    assert failed, "a failing portal-sync pass logged nothing at all"
    assert all(r.levelno >= logging.WARNING for r in failed), (
        "a failed heartbeat was reported below WARNING — under the daemon's "
        "real (unconfigured-root) logging that is indistinguishable from "
        "silence, which is #2862"
    )


def test_the_tick_loop_names_the_config_it_is_running_on_at_startup(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Step 3d's debugging note tells the next person to "check what `config`
    actually *is* ... and how the daemon was launched".  #2862 makes the daemon
    answer that itself, once, at startup — the check that took an ssh session
    and a walk through ``/proc/<mainpid>/environ`` last time."""
    _enable_portal(valid_config_path)
    cfg = load_config(valid_config_path)

    _stub_portal_sync_tick(monkeypatch)
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
    monkeypatch.setenv("COORD_PORTAL_SYNC_INTERVAL", "0.05")
    _quiet_all_other_tick_intervals(monkeypatch)

    app = build_app(SqliteStore(file_db), cfg)
    with caplog.at_level(logging.INFO, logger="coord.serve"):
        _run_tick_loop_briefly(app, settle=0.2)

    startup = [
        r.getMessage() for r in caplog.records if "tick loop starting" in r.getMessage()
    ]
    assert startup, "the daemon never said which config its tick loop is using"
    assert str(valid_config_path) in startup[0]
    assert "portal.enabled=True" in startup[0]


def test_a_dead_tick_loop_says_so_instead_of_just_going_quiet(
    file_db: Path, valid_config_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#2862 suspect 3 — "the tick loop itself stalled" — was listed as the
    thing to rule out FIRST and there was no way to do it from the journal.

    ``_tick_loop`` is a bare ``asyncio.create_task`` with no supervisor: an
    exception out of its ``while True`` stops every cadence in it (reconcile,
    notify-drain, auto-drain, milestone, portal sync) for the life of the
    process, and asyncio's own "exception was never retrieved" warning only
    fires when the task is garbage-collected — which, for a task held alive by
    the lifespan's frame, means at process exit if ever.  So it must announce
    its own death.
    """
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated tick-loop death")

    # The one unguarded call at the top of every tick-loop iteration.
    monkeypatch.setattr(serve_app_module, "_reload_config_if_stale", _boom)
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
    monkeypatch.setenv("COORD_PORTAL_SYNC_INTERVAL", "0")
    _quiet_all_other_tick_intervals(monkeypatch)

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with caplog.at_level(logging.ERROR, logger="coord.serve"):
        # The lifespan's shutdown re-awaits the (already failed) task, so the
        # simulated death surfaces again on teardown. Pre-existing behaviour
        # and harmless — the process is on its way out by then — but this test
        # drives teardown explicitly, so swallow it here.
        with contextlib.suppress(RuntimeError):
            _run_tick_loop_briefly(app, settle=0.3)

    dead = [r for r in caplog.records if "STOPPED" in r.getMessage()]
    assert dead, (
        "the tick loop died and the daemon said nothing — indistinguishable "
        "from 'every step in it is somehow a no-op', which is how #2862 got "
        "diagnosed three layers down in a frozen DB column"
    )
    assert "'tick'" in dead[0].getMessage(), (
        "the ERROR line must name WHICH loop died — there are five"
    )
    assert dead[0].exc_info is not None, (
        "no traceback attached, so the operator still can't tell why"
    )


def test_serve_command_never_thin_clients_its_own_bootstrap(
    monkeypatch: pytest.MonkeyPatch, valid_config_path: Path, tmp_path: Path
) -> None:
    """#2824's ACTUAL root cause, found on review round 1: Step 3d's guard
    was never broken — `coord serve`'s own bootstrap was. `serve()` loaded
    its config via the shared `_load_config()`, which treats "a board_service
    is configured" (a stray `~/.coord/client.toml` or `$COORD_SERVICE_URL`
    left on the daemon host — plausible after a migration to a new primary)
    as license to silently fetch and boot on *some other machine's*
    coordinator.yml instead of the `--config` path the operator explicitly
    passed. `_refresh_config()`'s mtime-guarded reload then kept re-reading
    that same wrong file forever, with nothing to say so — exactly why a
    real on-disk `portal.enabled: true` was never seen by the running
    daemon while `coord.config.load()` on the identical path, standalone,
    correctly reported it.

    This must fail against the pre-fix `serve()` (which called bare
    `_load_config(config_path)`): with `resolve_board_service()` mocked to
    return a service, that call fetches and boots on the REMOTE config
    below — a different repo, portal disabled — instead of the local one.
    """
    _enable_portal(valid_config_path)
    local_cfg = load_config(valid_config_path)
    assert local_cfg.portal.enabled is True
    local_repo_name = local_cfg.repos[0].name

    remote_path = tmp_path / "remote-coordinator.yml"
    remote_path.write_text(
        "repos:\n"
        "  - name: REMOTE-WRONG-REPO\n"
        "    github: acme/remote\n"
        "machines:\n"
        "  - name: remote-m\n"
        "    host: remote.tailnet\n"
        "    repos: [REMOTE-WRONG-REPO]\n"
    )

    monkeypatch.setattr(
        coord_client, "resolve_board_service",
        lambda *a, **k: coord_client.ServiceConfig("http://daemon:7435"),
    )

    def _fetch_remote_config_must_not_be_called(svc, **kw):  # noqa: ANN001
        raise AssertionError(
            "coord serve must never call fetch_remote_config — it MINTS the "
            "board's config, it is not a client of another one (#2824)"
        )

    monkeypatch.setattr(
        coord_client, "fetch_remote_config", _fetch_remote_config_must_not_be_called
    )

    captured: dict = {}

    def _fake_build_app(store, cfg, *, token=None):  # noqa: ANN001, ARG001
        captured["cfg"] = cfg
        return object()  # never actually served — uvicorn.run is stubbed below

    monkeypatch.setattr(serve_app_module, "build_app", _fake_build_app)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)  # noqa: ARG005

    from click.testing import CliRunner

    from coord.cli import main

    result = CliRunner().invoke(main, ["serve", "--config", str(valid_config_path)])

    assert result.exit_code == 0, result.output
    cfg = captured["cfg"]
    assert cfg.portal.enabled is True
    assert cfg.repos[0].name == local_repo_name
    assert cfg.repos[0].name != "REMOTE-WRONG-REPO"


# ── #2246: auto-drain sweeps siblings the drain itself just broke ────────────


def test_auto_drain_marks_a_sibling_the_merge_just_conflicted(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#2246: auto-drain lands a PR, which makes a sibling PR on the same base
    CONFLICTING — GitHub knows immediately and, before this, nothing asked.

    Auto-drain (unlike a human running `coord merge`) has no other moment to
    look: `process()` only ever touches PENDING entries the plan marks READY,
    so the sibling would sit on whatever gate reason happened to be red until
    a human went digging. #2246's floor is that the entry report *conflict* as
    its blocking reason; that's what parking it at CONFLICT does.
    """
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _auto_drain_tick

    monkeypatch.setattr(
        "coord.github_ops.create_pr",
        lambda repo, *, base, head, title, body: {
            "number": 201, "url": "https://gh/201", "existed": False
        },
    )
    monkeypatch.setattr("coord.github_ops.get_pr_size", lambda repo, number: 42)
    monkeypatch.setattr(
        "coord.github_ops.merge_pr",
        lambda repo, number, method="rebase": (True, "merged"),
    )
    # The sibling (PR 309) is what GitHub now reports CONFLICTING.
    monkeypatch.setattr(
        "coord.github_ops.check_pr_mergeable", lambda repo, number: number == 201,
    )
    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t, **_kw: _NoOpCi())

    _seed_queued_ready_entry(rw_db)
    # A sibling on the SAME base with an open PR, blocked on something else
    # (no approved review) so the drain itself never touches it.
    rw_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state, pr_number) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("work-sib", "api", "acme/api", "issue-309-impl", "main", 309,
         "Sibling", "pending", 309),
    )
    rw_db.commit()

    drain_config_path = _make_drain_config(tmp_path, auto_drain=True)
    monkeypatch.setenv("COORD_CONFIG", str(drain_config_path))
    cfg = load_config(drain_config_path)

    events = _auto_drain_tick(cfg)

    rows = {x.assignment_id: x for x in mq.load_queue()}
    assert rows["work-drain1"].state == mq.MERGED
    assert rows["work-sib"].state == mq.CONFLICT
    assert mq.classify_conflict(rows["work-sib"].error) == "rebaseable"
    assert "#55" in rows["work-sib"].error
    # The sweep's event joins the tick's stream, so it gets an audit row too.
    assert any(
        ev.kind == "conflict" and ev.entry.assignment_id == "work-sib"
        for ev in events
    )


def test_auto_drain_leaves_a_clean_sibling_alone(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """The sweep only ever acts on an explicit CONFLICTING verdict — a clean
    sibling (and an inconclusive read) stays exactly where it was."""
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _auto_drain_tick

    monkeypatch.setattr(
        "coord.github_ops.create_pr",
        lambda repo, *, base, head, title, body: {
            "number": 201, "url": "https://gh/201", "existed": False
        },
    )
    monkeypatch.setattr("coord.github_ops.get_pr_size", lambda repo, number: 42)
    monkeypatch.setattr(
        "coord.github_ops.merge_pr",
        lambda repo, number, method="rebase": (True, "merged"),
    )
    monkeypatch.setattr("coord.github_ops.check_pr_mergeable", lambda repo, number: True)
    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t, **_kw: _NoOpCi())

    _seed_queued_ready_entry(rw_db)
    rw_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state, pr_number) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("work-sib", "api", "acme/api", "issue-309-impl", "main", 309,
         "Sibling", "pending", 309),
    )
    rw_db.commit()

    drain_config_path = _make_drain_config(tmp_path, auto_drain=True)
    monkeypatch.setenv("COORD_CONFIG", str(drain_config_path))
    _auto_drain_tick(load_config(drain_config_path))

    rows = {x.assignment_id: x for x in mq.load_queue()}
    assert rows["work-sib"].state == mq.PENDING
    assert rows["work-sib"].error is None


# ── #2895: coord-tui's ex-SQLite writers as daemon routes ─────────────────────
#
# `POST /purge` and `POST /issue-upsert` replace the read-write rusqlite
# connection coord-tui used to open against ~/.coord/coord.db (Settings →
# purge, and the `gh issue view` cache refresh). The SQL predicates asserted
# here were previously asserted in Rust, in `tui/src/app/tests.rs`, against an
# in-memory SQLite fixture — they now live on this side of the seam, where
# `coord/sql.py`'s dialect layer can carry them onto Postgres (#2894 Phase D).

def _seed_purge_rows(conn) -> None:
    """Two purgeable assignments, one too fresh, one running; plus issues."""
    for aid, status, finished_at in [
        ("old-done", "done", 100.0),
        ("old-failed", "failed", 100.0),
        ("fresh-done", "done", 500.0),
        ("running", "running", 100.0),
        ("no-finish", "done", None),
    ]:
        conn.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, status, finished_at) VALUES (?,?,?,?,?,?,?)",
            (aid, "laptop", "api", 1, "t", status, finished_at),
        )
    for number, state_, synced_at in [
        (1, "closed", 100.0),
        (2, "closed", 200.0),
        (3, "closed", 500.0),   # too fresh
        (4, "open", 100.0),     # open: never purged
        (5, "closed", None),    # no synced_at: never purged
    ]:
        conn.execute(
            "INSERT INTO issues (repo_name, number, title, state, labels, synced_at) "
            "VALUES (?,?,?,?,?,?)",
            ("api", number, "t", state_, "[]", synced_at),
        )
    conn.commit()


def _purge(cli, *, older_than_secs: float, dry_run: bool):
    return cli.post(
        "/purge", json={"older_than_secs": older_than_secs, "dry_run": dry_run}
    )


def test_serve_purge_dry_run_counts_without_deleting(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    _seed_purge_rows(rw_db)
    # Freeze "now" so the cutoff lands at 300.0 with older_than_secs=200.
    monkeypatch.setattr("coord.state.time.time", lambda: 500.0)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = _purge(cli, older_than_secs=200.0, dry_run=True)
    assert resp.status_code == 200
    # done+failed older than the cutoff only: running excluded, fresh excluded,
    # NULL finished_at excluded.  Issues: closed and old only.
    assert resp.json() == {"assignments": 2, "issues": 2}
    # Nothing actually deleted.
    assert rw_db.execute("SELECT COUNT(*) c FROM assignments").fetchone()["c"] == 5
    assert rw_db.execute("SELECT COUNT(*) c FROM issues").fetchone()["c"] == 5


def test_serve_purge_deletes_exactly_what_the_dry_run_counted(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """The prompt and the toast must never disagree.

    The TUI shows the dry-run number in its confirmation prompt and the delete
    number in the completion toast; a drift between the two predicates is the
    "confirmed Purge 3 rows, got 47 removed" bug this guards.
    """
    _seed_purge_rows(rw_db)
    monkeypatch.setattr("coord.state.time.time", lambda: 500.0)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        counted = _purge(cli, older_than_secs=200.0, dry_run=True).json()
        deleted = _purge(cli, older_than_secs=200.0, dry_run=False).json()
    assert counted == deleted == {"assignments": 2, "issues": 2}
    remaining = {
        r["assignment_id"]
        for r in rw_db.execute("SELECT assignment_id FROM assignments").fetchall()
    }
    assert remaining == {"fresh-done", "running", "no-finish"}
    remaining_issues = {
        r["number"] for r in rw_db.execute("SELECT number FROM issues").fetchall()
    }
    assert remaining_issues == {3, 4, 5}


def test_serve_purge_rejects_bad_older_than_secs(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        assert cli.post("/purge", json={}).status_code == 400
        assert cli.post("/purge", json={"older_than_secs": "soon"}).status_code == 400
        assert cli.post("/purge", json={"older_than_secs": -1}).status_code == 400


def test_serve_issue_upsert_inserts_then_updates_one_row(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    issue = {
        "number": 42,
        "title": "first title",
        "body": "first body",
        "state": "open",
        "labels": ["coord"],
        "milestone_number": 3,
        "milestone_title": "ms-3",
    }
    with TestClient(app) as cli:
        assert cli.post(
            "/issue-upsert", json={"repo_name": "api", "issue": issue}
        ).status_code == 200
        row = rw_db.execute(
            "SELECT title, body, state, labels, milestone_title FROM issues "
            "WHERE repo_name='api' AND number=42"
        ).fetchone()
        assert row["title"] == "first title"
        assert row["state"] == "open"
        assert json.loads(row["labels"]) == ["coord"]
        assert row["milestone_title"] == "ms-3"

        # Second POST for the same (repo, number) updates in place.
        issue["title"] = "second title"
        issue["state"] = "closed"
        assert cli.post(
            "/issue-upsert", json={"repo_name": "api", "issue": issue}
        ).status_code == 200
    rows = rw_db.execute(
        "SELECT title, state FROM issues WHERE repo_name='api' AND number=42"
    ).fetchall()
    assert len(rows) == 1, "upsert must not duplicate the row"
    assert rows[0]["title"] == "second title" and rows[0]["state"] == "closed"


def test_serve_issue_upsert_does_not_close_sibling_issues(
    file_db: Path, valid_config_path: Path, rw_db
):
    """Unlike /issues-sync, a single-row upsert is a refresh, not a sync.

    /issues-sync marks every other issue in the repo closed before upserting
    the fetched set.  The TUI's `gh issue view` refresh fetches exactly ONE
    issue, so routing it through that endpoint would have closed the entire
    rest of the board's backlog.
    """
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, state, labels, synced_at) "
        "VALUES ('api', 7, 'sibling', 'open', '[]', 1.0)"
    )
    rw_db.commit()
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        assert cli.post(
            "/issue-upsert",
            json={"repo_name": "api", "issue": {"number": 42, "title": "new"}},
        ).status_code == 200
    sibling = rw_db.execute("SELECT state FROM issues WHERE number=7").fetchone()
    assert sibling["state"] == "open", "the sibling must be untouched"


def test_serve_issue_upsert_rejects_missing_number(
    file_db: Path, valid_config_path: Path, rw_db
):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        assert cli.post("/issue-upsert", json={"repo_name": "api"}).status_code == 400
        assert cli.post(
            "/issue-upsert", json={"repo_name": "api", "issue": {"title": "x"}}
        ).status_code == 400


def test_purge_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    _seed_purge_rows(coord_db)
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: list = []
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.append((path, payload))
        or {"assignments": 9, "issues": 4},
    )
    assert state.count_purgeable(600.0) == (9, 4)
    assert state.purge_done_assignments_split(600.0) == (9, 4)
    assert [p for p, _ in captured] == ["/purge", "/purge"]
    assert captured[0][1]["dry_run"] is True
    assert captured[1][1]["dry_run"] is False
    # Routed → the thin client's own (wrong) DB is left completely alone.
    assert coord_db.execute("SELECT COUNT(*) c FROM assignments").fetchone()["c"] == 5


def test_upsert_issue_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"ok": True},
    )
    state.upsert_issue("api", {"number": 42, "title": "t"})
    assert captured["path"] == "/issue-upsert"
    assert captured["payload"]["issue"]["number"] == 42
    assert coord_db.execute("SELECT COUNT(*) c FROM issues").fetchone()["c"] == 0


def test_upsert_issue_local_accepts_github_and_plain_label_shapes(coord_db):
    from coord import state

    state._upsert_issue_local("api", {"number": 1, "labels": [{"name": "coord"}]})
    state._upsert_issue_local("api", {"number": 2, "labels": ["coord"]})
    rows = {
        r["number"]: json.loads(r["labels"])
        for r in coord_db.execute("SELECT number, labels FROM issues").fetchall()
    }
    assert rows == {1: ["coord"], 2: ["coord"]}
