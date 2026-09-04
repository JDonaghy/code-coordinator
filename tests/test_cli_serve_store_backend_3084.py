"""#3084: the `coord serve` startup banner names the resolved storage
backend, and — critically — never names the `DB_PATH` it will not open once
`store.backend: postgres` is configured.

Before this fix, `coord/commands/lifecycle.py:serve()` built
`SqliteStore(DB_PATH)` and printed `db={DB_PATH}` unconditionally; under
`backend: postgres`, `SqliteStore._connect()` (`coord/dao.py`) ignores that
path entirely (it re-resolves the backend itself via the same
`coord.db._resolve_store_target()` `resolve_store_backend()` wraps), so the
old banner named a file the daemon never actually opens (#2096).

`serve()` ends in a blocking `uvicorn.run(...)` — mocked out here so the
command returns immediately after printing the banner, exactly the way its
own module comment (a `SqliteStore`/`build_serve_app` construction with no
DB access until a real HTTP request lands) makes safe to do without ever
touching a real database.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import coord.db as db_mod
from coord.commands.lifecycle import serve


@pytest.fixture(autouse=True)
def _mock_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)


def test_sqlite_banner_is_unchanged_in_substance(
    valid_config_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Today's fleet (SQLite, no `store:` block) keeps naming `DB_PATH` --
    this pins that nothing regressed for the common case."""
    monkeypatch.setattr(db_mod, "resolve_store_backend", lambda: ("sqlite", None))
    result = CliRunner().invoke(
        serve, ["--config", str(valid_config_path)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "backend=sqlite" in result.output
    assert f"db={db_mod.DB_PATH}" in result.output
    assert "target=" not in result.output


def test_postgres_banner_never_names_db_path(
    valid_config_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2096: shown red before the fix -- the old unconditional
    `db={DB_PATH}` string used to appear here regardless of backend."""
    monkeypatch.setattr(
        db_mod, "resolve_store_backend", lambda: ("postgres", "postgresql://dbhost:5432/coorddb")
    )
    result = CliRunner().invoke(
        serve, ["--config", str(valid_config_path)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "backend=postgres" in result.output
    assert "target=postgresql://dbhost:5432/coorddb" in result.output
    assert str(db_mod.DB_PATH) not in result.output
    assert "db=" not in result.output


def test_postgres_banner_never_leaks_a_raw_dsn(
    valid_config_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3084 acceptance: no raw DSN (password included) reaches the banner
    -- exercises the real `resolve_store_backend()` (only the config
    resolution underneath it is mocked), so a redaction regression would
    fail this test, not just the mocked-value test above."""
    from coord import sql

    monkeypatch.setattr(
        db_mod,
        "_resolve_store_target",
        lambda: db_mod._StoreTarget(
            backend=sql.DIALECT_POSTGRES,
            dsn="postgresql://admin:s3cret-password@dbhost:5432/coorddb",
        ),
    )
    result = CliRunner().invoke(
        serve, ["--config", str(valid_config_path)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "s3cret-password" not in result.output
    assert "admin:" not in result.output
    assert "backend=postgres" in result.output
