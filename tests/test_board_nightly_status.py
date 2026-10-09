"""#3661: `GET /board` carries the latest nightly real-platform smoke
verdict per repo+artifact — the ``nightly_status`` key a thin client's
status-bar segment renders. Black-box through the real ASGI app, mirroring
tests/test_approved_work_2532.py's "same GET /board endpoint a thin client
uses" harness, since the daemon-side wiring in coord/serve_app.py has no
other seam a unit test could reach.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.nightly_store import NightlyResultRecord, record_nightly_result, set_nightly_issue_number
from coord.serve_app import build_app
from tests.backends import set_board_meta

CONFIG_YAML = """\
repos:
  - name: vimcode
    github: acme/vimcode

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [vimcode]

release_gate:
  vimcode:
    nightly: required
    nightly_artifacts: [macos-dmg]
"""


@pytest.fixture(autouse=True)
def _coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))


@pytest.fixture
def detail_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    set_board_meta(conn, "round_number", "0")
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _board(detail_db: Path, config_path: Path) -> dict:
    app = build_app(SqliteStore(detail_db), load_config(config_path))
    with TestClient(app) as cli:
        return cli.get("/board").json()


def _record(**kwargs) -> None:
    defaults = dict(
        repo="vimcode", artifact="macos-dmg", sha="deadbeef", passed=True,
        checked_at=time.time(), spec="install.yaml", step="launch",
        run_id="run-1", steps_total=1,
    )
    defaults.update(kwargs)
    record_nightly_result(NightlyResultRecord(**defaults))


class TestBoardNightlyStatusWiring:
    def test_board_carries_the_key_even_with_nothing_recorded(
        self, detail_db, config_path,
    ) -> None:
        (row,) = _board(detail_db, config_path)["nightly_status"]
        assert row["repo"] == "vimcode"
        assert row["artifact"] == "macos-dmg"
        assert row["state"] == "stale"

    def test_a_green_run_reaches_the_board(self, detail_db, config_path) -> None:
        _record(passed=True)
        (row,) = _board(detail_db, config_path)["nightly_status"]
        assert row["state"] == "green"

    def test_a_red_run_carries_the_issue_number(self, detail_db, config_path) -> None:
        _record(passed=False, detail="crashed")
        set_nightly_issue_number(
            repo="vimcode", run_id="run-1", spec="install.yaml", step="launch",
            issue_number=55,
        )
        (row,) = _board(detail_db, config_path)["nightly_status"]
        assert row["state"] == "red"
        assert row["issue_numbers"] == [55]
        assert row["failing_step_count"] == 1

    def test_an_unavailable_run_reaches_the_board_as_infra(
        self, detail_db, config_path,
    ) -> None:
        _record(passed=False, unavailable=True, detail="screen locked", host="elitebook")
        (row,) = _board(detail_db, config_path)["nightly_status"]
        assert row["state"] == "infra"
        assert row["host"] == "elitebook"

    def test_a_stale_run_reaches_the_board_as_stale(self, detail_db, config_path) -> None:
        _record(passed=True, checked_at=1.0)
        (row,) = _board(detail_db, config_path)["nightly_status"]
        assert row["state"] == "stale"

    def test_a_repo_with_no_release_gate_nightly_entry_contributes_nothing(
        self, tmp_path: Path,
    ) -> None:
        plain = tmp_path / "coordinator-plain.yml"
        plain.write_text(
            "repos:\n  - name: vimcode\n    github: acme/vimcode\n"
            "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
            "    capabilities: [python]\n    repos: [vimcode]\n"
        )
        db_path = tmp_path / "coord.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        set_board_meta(conn, "round_number", "0")
        conn.commit()
        conn.close()
        assert _board(db_path, plain)["nightly_status"] == []
