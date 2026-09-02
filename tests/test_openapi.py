"""#757: every HTTP surface serves an OpenAPI 3 spec + Swagger UI docs page,
and the spec can never silently drift from the real route table.

Covers the three hand-rolled Starlette apps:
- coord/agent_app.py   (port 7433)
- coord/serve_app.py   (port 7435)
- coord/dashboard/server.py (port 7434)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord.agent import AgentServer
from coord.agent_app import build_app as build_agent_app
from coord.config import Config
from coord.dao import SqliteStore
from coord.dashboard.server import build_app as build_dashboard_app
from coord.db import _ensure_schema
from coord.models import Machine, Repo
from coord.openapi import declared_routes, spec_routes, validate_json_schema
from coord.serve_app import build_app as build_serve_app
from scripts.gen_board_fixture import fixture_json_text


# ── agent ─────────────────────────────────────────────────────────────────

def _agent_client(tmp_path: Path) -> TestClient:
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"api": str(tmp_path)},
    )
    return TestClient(build_agent_app(server))


def test_agent_openapi_matches_declared_routes(tmp_path: Path) -> None:
    client = _agent_client(tmp_path)
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert declared_routes(client.app.routes) == spec_routes(spec)


def test_agent_openapi_fully_specifies_assign(tmp_path: Path) -> None:
    client = _agent_client(tmp_path)
    spec = client.get("/openapi.json").json()
    assign = spec["paths"]["/assign"]["post"]
    req_schema = assign["requestBody"]["content"]["application/json"]["schema"]
    assert req_schema["$ref"] == "#/components/schemas/AssignmentSpec"
    resp_schema = assign["responses"]["202"]["content"]["application/json"]["schema"]
    assert resp_schema["$ref"] == "#/components/schemas/AgentAssignment"
    assignment_spec = spec["components"]["schemas"]["AssignmentSpec"]
    assert assignment_spec["properties"]["repo_name"] == {"type": "string"}
    assert assignment_spec["properties"]["issue_number"] == {"type": "integer"}
    agent_assignment = spec["components"]["schemas"]["AgentAssignment"]
    # spec: AssignmentSpec is a nested dataclass field -> $ref, not inlined.
    assert agent_assignment["properties"]["spec"] == {
        "$ref": "#/components/schemas/AssignmentSpec"
    }


def test_agent_docs_page_served(tmp_path: Path) -> None:
    client = _agent_client(tmp_path)
    r = client.get("/docs")
    assert r.status_code == 200
    assert "swagger-ui" in r.text.lower()


def test_agent_assign_schema_validates_a_real_request_and_response(
    tmp_path: Path,
) -> None:
    """#757 acceptance: the /assign schema is validated against a real
    payload, not just declared — dispatch a real assignment and check both
    the request body and the response against the generated schemas."""
    client = _agent_client(tmp_path)
    spec = client.get("/openapi.json").json()
    components = spec["components"]["schemas"]
    request_body = {
        "repo_name": "api",
        "repo_path": str(tmp_path),
        "issue_number": 1,
        "issue_title": "do thing",
        "briefing": "fix the bug",
        "files_allowed": [],
        "files_forbidden": [],
        "branch": "main",
    }
    assert validate_json_schema(
        request_body, {"$ref": "#/components/schemas/AssignmentSpec"}, components
    ) == []

    r = client.post("/assign", json=request_body)
    assert r.status_code == 202
    assert validate_json_schema(
        r.json(), {"$ref": "#/components/schemas/AgentAssignment"}, components
    ) == []


# ── daemon (coord serve) ─────────────────────────────────────────────────

def _serve_db(path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')"
    )
    conn.commit()
    conn.close()


def _serve_client(tmp_path: Path, *, token: str | None = None) -> TestClient:
    db_path = tmp_path / "coord.db"
    _serve_db(db_path)
    cfg = Config(repos=[], machines=[])
    return TestClient(build_serve_app(SqliteStore(db_path), cfg, token=token))


def test_serve_openapi_matches_declared_routes(tmp_path: Path) -> None:
    client = _serve_client(tmp_path)
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert declared_routes(client.app.routes) == spec_routes(spec)


def test_serve_openapi_fully_specifies_board(tmp_path: Path) -> None:
    client = _serve_client(tmp_path)
    spec = client.get("/openapi.json").json()
    board_schema = spec["paths"]["/board"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assignments_item = board_schema["properties"]["assignments"]["items"]
    assert assignments_item["$ref"] == "#/components/schemas/BoardAssignment"
    board_assignment = spec["components"]["schemas"]["BoardAssignment"]
    # Declared DTO fields show up with real types, and `briefing` — absent
    # from coord.board_schema.BoardAssignment — is off the wire.
    assert board_assignment["properties"]["assignment_id"] == {
        "type": "string", "nullable": True,
    }
    assert board_assignment["properties"]["issue_number"]["type"] == "integer"
    assert "briefing" not in board_assignment["properties"]
    # A JSON-decoded column (typed `list[str]` on the DTO) is an array,
    # not a raw string.
    assert board_assignment["properties"]["files_allowed"]["type"] == "array"


def test_serve_openapi_board_schema_validates_golden_fixture(tmp_path: Path) -> None:
    """#757 acceptance: the /board schema is validated against the #748
    golden fixture payload, not just declared.

    #2899: that fixture is no longer a file in this repo. The committed copy
    moved to coord-tui (`tests/fixtures/board_sample.json`, beside the Rust
    round-trip test that reads it), so this test now validates the schema
    against what `scripts/gen_board_fixture.py` produces *right now* — the
    exact bytes coord-tui commits and its CI freshness gate diffs against.
    Same subject, one indirection removed; reading a path that does not exist
    in this checkout would only ever have been a hard error.
    """
    fixture = json.loads(fixture_json_text())

    client = _serve_client(tmp_path)
    spec = client.get("/openapi.json").json()
    board_schema = spec["paths"]["/board"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    errors = validate_json_schema(fixture, board_schema, spec["components"]["schemas"])
    assert errors == [], f"golden /board fixture fails the generated schema: {errors}"


def test_serve_openapi_and_docs_require_bearer_token_when_configured(
    tmp_path: Path,
) -> None:
    client = _serve_client(tmp_path, token="s3cret")
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401
    ok = client.get("/openapi.json", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    # /healthz stays auth-exempt.
    assert client.get("/healthz").status_code == 200


# ── dashboard ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_spa_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the legacy dashboard so the dynamic SPA catch-all route (only
    added when coord/dashboard/webapp/dist/ exists) never joins the route
    table — keeps the drift-test's route inventory deterministic regardless
    of whether the React bundle happens to be built on this machine."""
    monkeypatch.setattr(
        "coord.dashboard.server.WEBAPP_DIST", Path("/nonexistent/dist")
    )


def _dashboard_client() -> TestClient:
    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )
    return TestClient(build_dashboard_app(cfg))


def test_dashboard_openapi_matches_declared_routes() -> None:
    client = _dashboard_client()
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert declared_routes(client.app.routes) == spec_routes(spec)


def test_dashboard_openapi_fully_specifies_board_and_pipeline() -> None:
    client = _dashboard_client()
    spec = client.get("/openapi.json").json()
    board_schema = spec["paths"]["/api/board"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert board_schema["properties"]["active"]["items"] == {
        "$ref": "#/components/schemas/Assignment"
    }
    pipeline_schema = spec["paths"]["/api/pipeline"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert pipeline_schema["items"] == {"$ref": "#/components/schemas/PipelineView"}
    assert "Assignment" in spec["components"]["schemas"]
    assert "PipelineView" in spec["components"]["schemas"]


def test_dashboard_docs_page_served() -> None:
    client = _dashboard_client()
    r = client.get("/docs")
    assert r.status_code == 200
    assert "swagger-ui" in r.text.lower()


# ── validator unit tests (proves the checker itself goes red) ──────────────

def test_validate_json_schema_detects_a_deliberate_mismatch() -> None:
    """Mirrors tests/test_board_fixture.py's
    test_find_integer_bool_mismatches_detects_a_deliberate_mismatch: proves
    validate_json_schema() actually rejects a payload that violates the
    schema, rather than being a rubber stamp."""
    components = {
        "Widget": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["name"],
        }
    }
    ref = {"$ref": "#/components/schemas/Widget"}
    assert validate_json_schema({"name": "a", "count": 3}, ref, components) == []
    assert validate_json_schema({"count": "not an int"}, ref, components) != []
    errors = validate_json_schema({"count": "not an int"}, ref, components)
    assert any("missing required property 'name'" in e for e in errors)
    assert any("expected integer" in e for e in errors)


def test_validate_json_schema_detects_an_undeclared_extra_field() -> None:
    """#3050: a payload that invents a field the schema never declared is
    rejected — the specific failure mode a fixture impersonating a route's
    shape produces (e.g. a `/api/machines` fixture claiming `severity`,
    which only `/api/machines/health` actually emits)."""
    components = {
        "Widget": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }
    }
    ref = {"$ref": "#/components/schemas/Widget"}
    assert validate_json_schema({"name": "a"}, ref, components) == []
    errors = validate_json_schema({"name": "a", "severity": "unknown"}, ref, components)
    assert any("unexpected property 'severity'" in e for e in errors)


def test_validate_json_schema_check_unknown_properties_can_be_disabled() -> None:
    components = {"Widget": {"type": "object", "properties": {"name": {"type": "string"}}}}
    ref = {"$ref": "#/components/schemas/Widget"}
    assert validate_json_schema(
        {"name": "a", "extra": 1}, ref, components, check_unknown_properties=False
    ) == []


def test_validate_json_schema_open_object_schema_stays_permissive() -> None:
    """A bare `{"type": "object"}` (no `properties`) describes a genuinely
    free-form value — unknown-property checking must not fire on it."""
    assert validate_json_schema({"anything": 1, "goes": 2}, {"type": "object"}, {}) == []


def test_validate_json_schema_check_required_can_be_disabled() -> None:
    """#3050: fixture-mode payload validation deliberately does not demand
    field completeness — a fixture may legitimately seed a partial payload
    to exercise a degraded state."""
    components = {
        "Widget": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
    }
    ref = {"$ref": "#/components/schemas/Widget"}
    assert validate_json_schema({}, ref, components) != []
    assert validate_json_schema({}, ref, components, check_required=False) == []


def test_validate_json_schema_nullable_sibling_alongside_ref_is_respected() -> None:
    """Some hand-built dashboard schemas spell an optional nested object as
    `{**some_ref, "nullable": True}` — a sibling key alongside `$ref` rather
    than a `nullable` flag on the target itself. `null` must validate."""
    components = {"Widget": {"type": "object", "properties": {"name": {"type": "string"}}}}
    schema = {"$ref": "#/components/schemas/Widget", "nullable": True}
    assert validate_json_schema(None, schema, components) == []
    assert validate_json_schema({"name": "a"}, schema, components) == []


def test_validate_json_schema_type_array_dialect_is_supported() -> None:
    """`{"type": ["string", "null"]}` (plain JSON Schema dialect, used by a
    few hand-built dashboard schemas like `session_response`) is accepted
    alongside this module's own `nullable: true` convention."""
    schema = {"type": ["string", "null"]}
    assert validate_json_schema("x", schema, {}) == []
    assert validate_json_schema(None, schema, {}) == []
    errors = validate_json_schema(5, schema, {})
    assert any("expected string" in e for e in errors)
