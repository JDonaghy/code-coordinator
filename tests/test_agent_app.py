"""Tests for the Starlette HTTP layer over AgentServer."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from starlette.testclient import TestClient

from coord.agent import AgentServer
from coord.agent_app import build_app

from .conftest import py_worker_argv


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit so worktrees can be created."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _client(
    tmp_path: Path,
    *,
    argv: list[str] | None = None,
    repo_paths: dict[str, str] | None = None,
    repo_path: Path | None = None,
    artifact_paths: dict[str, list[str]] | None = None,
) -> tuple[TestClient, AgentServer]:
    rp = repo_path or _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv or py_worker_argv("print('ok')"),
        repo_paths=repo_paths if repo_paths is not None else {"api": str(rp)},
        artifact_paths=artifact_paths,
    )
    app = build_app(server)
    return TestClient(app), server


def _payload(tmp_path: Path, repo_path: Path | None = None, **overrides) -> dict:
    base = {
        "repo_name": "api",
        "repo_path": str(repo_path or tmp_path),
        "issue_number": 1,
        "issue_title": "do thing",
        "briefing": "fix the bug",
        "files_allowed": [],
        "files_forbidden": [],
        "branch": "main",
    }
    base.update(overrides)
    return base


def test_health_endpoint(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["machine"] == "test"
    assert body["repos"] == ["api"]


def test_assign_then_status(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(tmp_path, repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    assert r.status_code == 202
    aid = r.json()["id"]

    server.wait_for(aid)

    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert len(body["completed"]) == 1
    assert body["completed"][0]["id"] == aid
    # Worker makes no commits → advisory (#448); "done" is also valid
    # for workers that do make commits.
    assert body["completed"][0]["status"] in ("done", "advisory")
    # worktree_path should be present in status response
    assert body["completed"][0]["worktree_path"] is not None
    server.shutdown()


def test_assign_invalid_json(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/assign", content="not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_assign_bad_payload_shape(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/assign", json={"unexpected": "fields only"})
    assert r.status_code == 400
    assert "bad assignment payload" in r.json()["error"]


def test_assign_unknown_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    client, _ = _client(tmp_path, repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo, repo_name="ghost"))
    assert r.status_code == 400


def test_cancel_endpoint(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(
        tmp_path, argv=py_worker_argv("import time; time.sleep(30)"), repo_path=repo
    )
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]

    # Wait until it's running before cancelling
    import time
    for _ in range(50):
        if server.get(aid).status == "running":
            break
        time.sleep(0.02)

    r = client.post(f"/cancel/{aid}")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    server.shutdown()


def test_cancel_unknown_id_returns_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/cancel/missing")
    assert r.status_code == 404


def test_inject_endpoint_delivers_message(tmp_path: Path) -> None:
    """POST /inject/{id} writes the text to the worker's stdin."""
    import time
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(
        tmp_path,
        argv=py_worker_argv(
            "import sys\n"
            "a = sys.stdin.readline().rstrip(chr(10))\n"
            "print('got1=' + a)\n"
            "b = sys.stdin.readline().rstrip(chr(10))\n"
            "print('got2=' + b)\n"
        ),
        repo_path=repo,
    )
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    time.sleep(0.3)  # let stdin wire up + first line drain

    r = client.post(f"/inject/{aid}", json={"text": "injected-content"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "delivered"

    final = server.wait_for(aid, timeout=5.0)
    log = Path(final.log_path).read_text()
    assert "injected-content" in log
    assert "# inject: injected-content" in log


def test_inject_endpoint_unknown_id_returns_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/inject/missing", json={"text": "hi"})
    assert r.status_code == 404


def test_inject_endpoint_finished_assignment_returns_409_or_410(tmp_path: Path) -> None:
    """A finished assignment can no longer be injected into."""
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(tmp_path, argv=["/bin/echo", "done"], repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    server.wait_for(aid)

    r = client.post(f"/inject/{aid}", json={"text": "too late"})
    assert r.status_code in (409, 410)


def test_inject_endpoint_rejects_bad_body(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/inject/anything", json={"wrong_key": "x"})
    assert r.status_code == 400
    r = client.post("/inject/anything", json={"text": ""})
    assert r.status_code == 400


def test_health_drops_repo_with_missing_path(tmp_path: Path) -> None:
    """#1527: `/health` must not advertise a repo whose `repo_path` doesn't
    exist on disk — a stale/missing checkout would otherwise keep looking
    servable (router picks it as least-loaded) while every dispatch to it
    400s. The repo should move to `degraded` with a reason instead."""
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api", "ghost", "unconfigured"],
        state_dir=tmp_path / "state",
        repo_paths={
            "api": str(repo),
            "ghost": str(tmp_path / "does-not-exist"),
            # "unconfigured" has no repo_paths entry at all.
        },
    )
    app = build_app(server)
    client = TestClient(app)

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["repos"] == ["api"]
    assert "ghost" not in body["repos"]
    assert "unconfigured" not in body["repos"]
    assert "does-not-exist" in body["degraded"]["ghost"]
    assert "no repo_path configured" in body["degraded"]["unconfigured"]


def test_health_surfaces_version_and_last_update(tmp_path: Path) -> None:
    """/health includes the running version and any persisted last_update
    payload so the CLI can show a clear before/after delta."""
    import json as _json
    client, server = _client(tmp_path)
    # No last_update file → version present, last_update absent.
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert body["version"]
    assert "last_update" not in body

    # Persist a last_update.json — /health should now include it.
    (server.state_dir / "last_update.json").write_text(
        _json.dumps({
            "mode": "pip install --upgrade",
            "version_before": "0.3.0",
            "version_after": "0.4.0",
            "result": "upgraded",
            "error": None,
        })
    )
    r = client.get("/health")
    body = r.json()
    assert body["last_update"]["result"] == "upgraded"
    assert body["last_update"]["version_after"] == "0.4.0"


def test_logs_endpoint_returns_log_content(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(
        tmp_path, argv=py_worker_argv("print('hello-from-worker')"), repo_path=repo
    )
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    server.wait_for(aid)

    r = client.get(f"/logs/{aid}")
    assert r.status_code == 200
    assert "hello-from-worker" in r.text
    assert "X-Coord-Log-Total" in r.headers
    # Worker makes no commits → advisory (#448); both statuses are terminal.
    assert r.headers["X-Coord-Log-Status"] in ("done", "advisory")
    server.shutdown()


def test_logs_endpoint_supports_since(tmp_path: Path) -> None:
    """The `since` parameter returns bytes starting at the given offset.

    The reap thread may append a few footer lines after the status flips
    (#448 adds an advisory check and a final status line), so the test
    avoids asserting an exact end-of-log boundary — the log can only grow
    monotonically.  Instead it validates the since-offset mechanism by
    choosing an offset well inside the log (not at the very end).
    """
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(
        tmp_path, argv=py_worker_argv("print('line')"), repo_path=repo
    )
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    server.wait_for(aid)

    # Fetch the full log — it must contain the worker's output.
    full = client.get(f"/logs/{aid}").text
    assert "line" in full, "log must contain worker output"

    # since=0 must return at least as many bytes as 'full' (log can only grow).
    head = client.get(f"/logs/{aid}", params={"since": 0}).text
    assert head.startswith(full), "since=0 must return the full log"

    # Pick a stable midpoint offset that is well inside the initial log
    # content and can't race with reap's footer writes.
    mid = max(1, len(full) // 2)
    tail = client.get(f"/logs/{aid}", params={"since": mid}).text
    # The bytes from 'mid' onward must match the full log at that position.
    # Capture the log again at the same moment for a consistent comparison.
    full_now = client.get(f"/logs/{aid}", params={"since": 0}).text
    assert full_now[mid:] == tail, (
        f"since={mid} returned wrong slice; "
        f"expected {full_now[mid:]!r}, got {tail!r}"
    )
    server.shutdown()


def test_logs_endpoint_unknown_id_returns_404(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.get("/logs/missing")
    assert r.status_code == 404


def test_repos_endpoint_reports_per_repo_state(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "fakerepo"
    not_a_repo.mkdir()
    client, _ = _client(tmp_path, repo_paths={"api": str(not_a_repo)})
    r = client.get("/repos")
    assert r.status_code == 200
    body = r.json()
    # api exists but isn't a git repo → returns an error field, not a 500
    assert "api" in body
    assert "error" in body["api"]


def test_logs_endpoint_bad_since_returns_400(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    client, server = _client(tmp_path, repo_path=repo)
    r = client.post("/assign", json=_payload(tmp_path, repo_path=repo))
    aid = r.json()["id"]
    server.wait_for(aid)

    r = client.get(f"/logs/{aid}", params={"since": "not-an-int"})
    assert r.status_code == 400
    server.shutdown()


def test_status_returns_200_with_truncated_log(tmp_path: Path) -> None:
    """GET /status must return HTTP 200 even when the worker log ends mid-line.

    Race condition: /status is polled while the worker is actively writing an
    event to its stream-json log.  The last line is incomplete JSON.  The
    endpoint must never 500.
    """
    import json as _json
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        repos=["api"],
        state_dir=tmp_path / "state",
        repo_paths={"api": str(repo)},
        # Worker writes one complete stream-json event then sleeps so the
        # assignment stays RUNNING while we poll /status.
        worker_command=lambda spec: py_worker_argv(
            "import sys, time\n"
            "sys.stdout.write("
            "'{\"type\":\"system\",\"subtype\":\"init\",\"model\":\"x\",\"session_id\":\"s\"}'"
            " + chr(10))\n"
            "sys.stdout.write('{\"type\":\"assistant\",\"partial')\n"  # truncated last line
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        ),
    )
    app = build_app(server)
    from coord.agent import AssignmentSpec
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=1, issue_title="t", briefing="b",
    )
    a = server.assign(spec)

    import time
    for _ in range(50):
        if server.get(a.id).status == "running":
            break
        time.sleep(0.02)
    time.sleep(0.15)  # let the worker write its partial line

    from starlette.testclient import TestClient
    client = TestClient(app)
    r = client.get("/status")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "active" in body

    server.shutdown(kill_running=True)


# ── /health includes worktree_bytes ──────────────────────────────────────────

def test_health_includes_worktree_bytes(tmp_path: Path) -> None:
    """GET /health must include worktree_bytes."""
    client, _ = _client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "worktree_bytes" in body
    assert isinstance(body["worktree_bytes"], int)


# ── /worktree-clean endpoint ──────────────────────────────────────────────────

def test_worktree_clean_empty(tmp_path: Path) -> None:
    """POST /worktree-clean returns JSON with cleaned/kept/bytes_freed."""
    client, _ = _client(tmp_path)
    r = client.post("/worktree-clean")
    assert r.status_code == 200
    body = r.json()
    assert body["cleaned"] == 0
    assert body["kept"] == 0
    assert body["bytes_freed"] == 0


def test_worktree_clean_removes_orphan(tmp_path: Path) -> None:
    """POST /worktree-clean removes orphaned worktrees (no matching assignment).

    Uses ``recent_secs=0`` to bypass the race-window mtime guard that
    would otherwise keep this just-created directory.
    """
    client, server = _client(tmp_path)
    orphan = server.state_dir / "worktrees" / "no-such-id"
    orphan.mkdir(parents=True)
    (orphan / "data.txt").write_text("hello")

    r = client.post("/worktree-clean", json={"recent_secs": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["cleaned"] == 1
    assert not orphan.exists()


def test_worktree_clean_respects_recent_secs(tmp_path: Path) -> None:
    """recent_secs body param actually protects fresh orphans (race fix)."""
    client, server = _client(tmp_path)
    fresh = server.state_dir / "worktrees" / "racing-id"
    fresh.mkdir(parents=True)
    (fresh / "data.txt").write_text("partial")

    # Large recent_secs → fresh orphan must NOT be deleted.
    r = client.post("/worktree-clean", json={"recent_secs": 600})
    assert r.status_code == 200
    body = r.json()
    assert body["cleaned"] == 0
    assert body["kept"] == 1
    assert fresh.exists()

    # recent_secs=0 → same orphan IS deleted on the next call.
    r = client.post("/worktree-clean", json={"recent_secs": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["cleaned"] == 1
    assert not fresh.exists()


def test_worktree_clean_accepts_optional_protect_field(tmp_path: Path) -> None:
    """#1295: POST /worktree-clean accepts an optional ``protect`` list of
    assignment IDs the coordinator considers non-terminal, and preserves
    them across the sweep even when ``recent_secs=0``.

    Compatibility: the field is fully optional (existing callers pass only
    ``recent_secs``), and unknown/extra keys must NOT 400 — verified by
    the ``xtra`` filler below.
    """
    client, server = _client(tmp_path)
    protected = server.state_dir / "worktrees" / "coord-known-live-aid"
    other = server.state_dir / "worktrees" / "genuinely-orphaned"
    for wt in (protected, other):
        wt.mkdir(parents=True)
        (wt / "data.txt").write_text("x")

    r = client.post(
        "/worktree-clean",
        json={
            "recent_secs": 0,
            "protect": ["coord-known-live-aid"],
            # Extra unknown field must be silently ignored (backward-compat
            # invariant: older callers may send new fields to newer agents
            # and newer callers may send old fields to older agents).
            "xtra": "ignored-please",
        },
    )
    assert r.status_code == 200
    body = r.json()
    # Return shape is additive only and protected entry counts as kept
    # (#1402 adds the shared cargo-cache GC counters alongside).
    assert {"cleaned", "kept", "bytes_freed"} <= set(body)
    assert body["cleaned"] == 1
    assert body["kept"] == 1
    assert protected.exists()
    assert not other.exists()


def test_worktree_clean_no_body_still_works(tmp_path: Path) -> None:
    """#1295 compat: an older client that POSTs no body (or an empty body)
    to a new agent must still work — the ``protect`` field is optional."""
    client, _ = _client(tmp_path)
    # Empty body → default recent_secs, no protect list.
    r = client.post("/worktree-clean", json={})
    assert r.status_code == 200
    # #1402 adds the shared cargo-cache GC counters; the original three keys
    # are unchanged so an older client keeps working.
    assert {"cleaned", "kept", "bytes_freed"} <= set(r.json())


# ── GET /artifact/{repo}/{branch} ─────────────────────────────────────────────


def test_artifact_manifest_404_when_missing(tmp_path: Path) -> None:
    """GET /artifact/repo/branch returns 404 when no stash exists."""
    client, _ = _client(tmp_path)
    r = client.get("/artifact/myrepo/issue-99-some-feature")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body


def test_artifact_manifest_200_with_stash(tmp_path: Path) -> None:
    """GET /artifact/repo/branch returns 200 with manifest when stash exists."""
    client, server = _client(tmp_path)

    # Create a fake stash with one binary-sized file.
    stash_dir = server.state_dir / "artifacts" / "myrepo" / "issue-99-feature"
    stash_dir.mkdir(parents=True, exist_ok=True)
    artifact = stash_dir / "mybin"
    artifact.write_bytes(b"x" * 512)
    (stash_dir / ".assignment_id").write_text("abc-123")

    r = client.get("/artifact/myrepo/issue-99-feature")
    assert r.status_code == 200
    body = r.json()
    assert body["total_bytes"] == 512
    assert len(body["files"]) == 1
    assert body["files"][0]["name"] == "mybin"
    assert body["files"][0]["size"] == 512
    assert body["built_by_assignment_id"] == "abc-123"


def test_artifact_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    """GET /artifact/../.. (or branch=..) returns 404 — not a real directory leak."""
    client, server = _client(tmp_path)

    # Starlette routing won't even match a literal ".." segment in most cases,
    # but the server-side guard should also reject it.  We test what we can
    # reach via the test client.  A branch that encodes ".." in a safe way
    # (dots only) should be caught by the regex guard.
    r = client.get("/artifact/myrepo/..badname")
    # Could be 404 (guard rejected) or 404 (stash missing); either way NOT 200.
    assert r.status_code == 404

    # Verify the guard in artifact_manifest itself rejects ".." strings.
    manifest = server.artifact_manifest("myrepo", "..")
    assert manifest is None

    manifest = server.artifact_manifest("..", "issue-1-branch")
    assert manifest is None

    # Also verify that a valid pair returns None when the stash is genuinely absent.
    manifest = server.artifact_manifest("myrepo", "issue-1-branch")
    assert manifest is None


# ── #914: lazy stash-on-pull + accurate 404 reason ────────────────────────────


def test_artifact_manifest_lazy_stash_from_live_worktree(tmp_path: Path) -> None:
    """A missed finalize (no stash) with the worktree still on disk self-heals.

    Simulates the vimcode #552 scenario: the build succeeded and the branch
    was pushed, but the interactive session ended without a clean `coord
    done`, so nothing ever called stash_artifacts_for_branch. GET
    /artifact/<repo>/<branch> should find the live git worktree still
    checked out to that branch and stash on demand instead of 404ing.
    """
    client, server = _client(
        tmp_path, artifact_paths={"api": ["target/debug/mybinary"]}
    )
    repo_path = tmp_path / "repo"

    wt_path = tmp_path / "state" / "worktrees" / "asgn-914"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-552-fix", str(wt_path)],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    (wt_path / "target" / "debug").mkdir(parents=True)
    (wt_path / "target" / "debug" / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    # No stash exists yet.
    stash_dir = server.state_dir / "artifacts" / "api" / "issue-552-fix"
    assert not stash_dir.exists()

    r = client.get("/artifact/api/issue-552-fix")
    assert r.status_code == 200
    body = r.json()
    assert len(body["files"]) == 1
    assert body["files"][0]["name"] == "mybinary"
    assert body["built_by_assignment_id"] == "asgn-914"
    # The lazy stash actually persisted the file for next time.
    assert (stash_dir / "mybinary").exists()


def test_artifact_manifest_404_when_worktree_present_but_no_files_match(
    tmp_path: Path,
) -> None:
    """A live worktree exists and artifact_paths is configured, but the build
    hasn't produced anything matching the globs yet (build still running,
    build failed, or a wrong glob) — this must 404 with the informative
    "did not produce any files matching artifact_paths" reason, not a 200
    with an empty file list (#914 review: stash_artifacts_for_branch's
    unconditional mkdir must not be mistaken for stash success).

    Also asserts the lazy-stash retry doesn't self-poison: once a real
    build produces the matching file, a later request for the same
    repo/branch succeeds instead of staying stuck on the empty directory.
    """
    client, server = _client(
        tmp_path, artifact_paths={"api": ["target/debug/mybinary"]}
    )
    repo_path = tmp_path / "repo"

    wt_path = tmp_path / "state" / "worktrees" / "asgn-nomatch"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-914-nomatch", str(wt_path)],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    # No target/debug/mybinary in the worktree — the glob matches nothing.

    r = client.get("/artifact/api/issue-914-nomatch")
    assert r.status_code == 404
    error = r.json()["error"]
    assert "did not produce any files matching artifact_paths" in error

    # The lazy-stash attempt must not have poisoned the stash dir with an
    # empty directory that blocks all future retries.
    stash_dir = server.state_dir / "artifacts" / "api" / "issue-914-nomatch"
    assert not any(
        f.is_file() and not f.name.startswith(".") for f in stash_dir.iterdir()
    ) if stash_dir.exists() else True

    # Now the "build" actually lands the file — a later poll must self-heal
    # instead of staying stuck on the earlier empty attempt.
    (wt_path / "target" / "debug").mkdir(parents=True)
    (wt_path / "target" / "debug" / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    r2 = client.get("/artifact/api/issue-914-nomatch")
    assert r2.status_code == 200
    body = r2.json()
    assert [f["name"] for f in body["files"]] == ["mybinary"]


def test_artifact_manifest_404_reason_worktree_present_no_config(
    tmp_path: Path,
) -> None:
    """404 reason names the real cause when a live worktree exists but the
    repo has no artifact_paths configured — not a GC/glob-mismatch guess."""
    client, server = _client(tmp_path)  # no artifact_paths configured
    repo_path = tmp_path / "repo"

    wt_path = tmp_path / "state" / "worktrees" / "asgn-noconfig"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-1-noconfig", str(wt_path)],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )

    r = client.get("/artifact/api/issue-1-noconfig")
    assert r.status_code == 404
    error = r.json()["error"]
    assert "no artifact_paths configured" in error
    # #2251: this used to be `assert "GC" not in error`, checked against a
    # message that interpolates the live worktree's path. `mktemp -d` (used
    # by the populated-home CI job) can legally draw a directory name that
    # happens to contain "GC" — it did, once: `tmp.bWORN7PGCT`. A bare
    # two-letter substring guard against an interpolated path is vacuous
    # (no production branch ever emits the literal string "GC") and can
    # only ever fail falsely. Assert on the wording that actually
    # distinguishes this branch from the other three instead.
    assert "no stash and no live worktree" not in error
    assert "did not produce any files matching" not in error

    reason = server.artifact_absence_reason("api", "issue-1-noconfig")
    assert "no artifact_paths configured" in reason


def test_artifact_manifest_404_reason_worktree_present_no_config_path_has_gc(
    tmp_path: Path,
) -> None:
    """Regression for #2251: the 404 reason must still read correctly when
    the interpolated worktree path happens to contain the old "GC" sentinel
    — proving the assertion above no longer keys on that substring."""
    gc_root = tmp_path / "tmp.bWORN7PGCT"  # the exact draw that broke #2251
    gc_root.mkdir()
    client, server = _client(gc_root)  # no artifact_paths configured
    repo_path = gc_root / "repo"

    wt_path = gc_root / "state" / "worktrees" / "asgn-noconfig-gc"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-1-noconfig-gc", str(wt_path)],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    assert "GC" in str(wt_path)  # sanity: the sentinel really is in the path

    r = client.get("/artifact/api/issue-1-noconfig-gc")
    assert r.status_code == 404
    error = r.json()["error"]
    assert "no artifact_paths configured" in error
    # The interpolated path (and thus "GC") legitimately appears in the
    # message; only the wording that distinguishes the four branches matters.
    assert "GC" in error
    assert "no stash and no live worktree" not in error
    assert "did not produce any files matching" not in error


def test_artifact_manifest_404_reason_genuinely_absent(tmp_path: Path) -> None:
    """404 reason correctly reports 'genuinely absent' when no worktree
    matches at all — distinct from the worktree-present-but-unstashed case."""
    client, server = _client(
        tmp_path, artifact_paths={"api": ["target/debug/mybinary"]}
    )

    r = client.get("/artifact/api/issue-1-never-built")
    assert r.status_code == 404
    error = r.json()["error"]
    assert "already merged" in error or "nothing was ever built" in error

    reason = server.artifact_absence_reason("api", "issue-1-never-built")
    assert "already merged" in reason or "nothing was ever built" in reason


# ─── #207: /metrics endpoint ─────────────────────────────────────────────────

def test_metrics_returns_expected_keys(tmp_path):
    """GET /metrics returns JSON with the five expected numeric keys."""
    pytest = __import__("pytest")
    psutil = pytest.importorskip("psutil")  # skip if psutil not installed
    del psutil  # only needed to confirm availability

    client, _ = _client(tmp_path)
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    assert "cpu_percent" in body
    assert "mem_percent" in body
    assert "mem_used_mb" in body
    assert "mem_total_mb" in body
    assert "timestamp" in body
    # Values must be non-negative numbers.
    assert body["cpu_percent"] >= 0.0
    assert 0.0 <= body["mem_percent"] <= 100.0
    assert body["mem_used_mb"] > 0
    assert body["mem_total_mb"] >= body["mem_used_mb"]


def test_metrics_no_psutil(tmp_path):
    """GET /metrics returns 503 when psutil is unavailable."""
    import sys

    client, _ = _client(tmp_path)

    # Simulate psutil not being importable by blocking it from sys.modules.
    original = sys.modules.get("psutil", None)
    sys.modules["psutil"] = None  # type: ignore[assignment]
    try:
        r = client.get("/metrics")
        # Should return 503 with an error body.
        assert r.status_code == 503
        assert "error" in r.json()
    finally:
        if original is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = original


# ── #2681: XDG_RUNTIME_DIR default must not reach os.getuid() on win32 ───────


def test_systemctl_env_sets_xdg_runtime_dir_on_posix(monkeypatch):
    """#2729: this test fakes ``sys.platform`` to exercise the POSIX branch on
    any host, including a real Windows CI runner — but faking the platform
    doesn't fake away a genuinely missing stdlib attribute. ``os.getuid`` must
    be stubbed too (not just relied on as the real function), or this crashes
    with ``AttributeError`` on win32 despite the platform mock, since both the
    production code *and* the assertion below would otherwise reach the real
    (there: absent) ``os.getuid``."""
    from coord import agent_app

    monkeypatch.setattr(agent_app.sys, "platform", "linux")
    monkeypatch.setattr(agent_app.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    env = agent_app._systemctl_env()
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_systemctl_env_preserves_an_existing_xdg_runtime_dir(monkeypatch):
    """#2729: ``dict.setdefault``'s default argument is evaluated eagerly
    regardless of whether the key is already present, so
    ``f"/run/user/{os.getuid()}"`` still gets built even though this test
    expects it to be discarded — stub ``os.getuid`` for the same reason as
    above rather than relying on the real (possibly absent) function."""
    from coord import agent_app

    monkeypatch.setattr(agent_app.sys, "platform", "linux")
    monkeypatch.setattr(agent_app.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/custom")

    env = agent_app._systemctl_env()
    assert env["XDG_RUNTIME_DIR"] == "/run/user/custom"


def test_systemctl_env_skips_xdg_runtime_dir_on_win32(monkeypatch):
    """Windows has neither the XDG concept nor ``os.getuid`` — the default
    must be skipped outright rather than raising ``AttributeError`` (#2681)."""
    from coord import agent_app

    monkeypatch.setattr(agent_app.sys, "platform", "win32")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delattr(agent_app.os, "getuid", raising=False)

    env = agent_app._systemctl_env()
    assert "XDG_RUNTIME_DIR" not in env
