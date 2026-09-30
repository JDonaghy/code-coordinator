"""#1707: `coord status` surfaces the resolved provider (already persisted
at dispatch time — Assignment.provider_name, #324) so a mixed
claude/opencode fleet is legible without having to run `coord gates` per
issue.

Two independent surfaces:
  - "Completed work assignments:" rows read the persisted board row's
    ``provider_name`` directly.
  - The per-machine "busy — ..." line reads ``provider`` off the live
    agent's ``/status`` response ``spec`` (which only carries the field
    when the resolved provider differs from the implicit "claude" default —
    see coord/dispatch.py's dispatch()).

Both omit the tag entirely for the plain "claude" case so the common path
stays uncluttered — only a non-default backend earns the tag.
"""

from __future__ import annotations

import logging
from pathlib import Path

import coord.network as network_mod
import pytest
from click.testing import CliRunner

from coord.commands.status import status as status_cmd
from coord.models import Assignment, Board
from coord.network import MachineStatus, StatusResult
from coord.state import save_board
from tests.conftest import VALID_CONFIG


def _work(
    aid: str = "w1",
    *,
    issue_number: int = 1707,
    provider_name: str | None = None,
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="Mixed-fleet widget",
        assignment_id=aid,
        type="work",
        status="done",
        branch=f"issue-{issue_number}-{aid}",
        review_state="pending",
        provider_name=provider_name,
    )


def _run_status(valid_config_path, monkeypatch) -> str:
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: [])
    runner = CliRunner()
    result = runner.invoke(
        status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_completed_row_shows_non_default_provider(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    save_board(Board(completed=[_work("w1", provider_name="opencode")]))
    output = _run_status(valid_config_path, monkeypatch)
    assert "[provider=opencode]" in output, output


def test_completed_row_omits_tag_for_plain_claude(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """The overwhelming majority case (provider_name="claude" or unset)
    must NOT get a tag — only a genuine mixed-fleet difference is worth the
    extra text."""
    save_board(Board(completed=[_work("w1", provider_name="claude")]))
    output = _run_status(valid_config_path, monkeypatch)
    assert "[provider=" not in output, output


def test_completed_row_omits_tag_when_provider_name_unset(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    save_board(Board(completed=[_work("w1", provider_name=None)]))
    output = _run_status(valid_config_path, monkeypatch)
    assert "[provider=" not in output, output


def test_mixed_fleet_rows_report_distinct_providers(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """The exact scenario #1707 exists for: two completed work items in the
    same repo, dispatched with different providers, both legible at once."""
    save_board(
        Board(
            completed=[
                _work("w1", issue_number=1, provider_name="opencode"),
                _work("w2", issue_number=2, provider_name="claude"),
            ]
        )
    )
    output = _run_status(valid_config_path, monkeypatch)
    assert "#1: Mixed-fleet widget (api)  [provider=opencode]" in output, output
    # The claude row has no tag at all.
    assert "#2: Mixed-fleet widget (api)  [provider=" not in output, output


def test_busy_line_shows_non_default_provider(valid_config_path, monkeypatch) -> None:
    from coord import config as config_mod

    cfg = config_mod.load(valid_config_path)
    laptop = next(m for m in cfg.machines if m.name == "laptop")
    server = next(m for m in cfg.machines if m.name == "server")

    def fake_check_all(machines, timeout=3.0, max_workers=None):
        return [
            MachineStatus(machine=laptop, state="online", latency_ms=1.0),
            MachineStatus(machine=server, state="offline", reason="connection refused"),
        ]

    def fake_fetch_status(machine, timeout=3.0):
        if machine.name == "laptop":
            return StatusResult(
                data={
                    "active": [
                        {
                            "spec": {
                                "type": "work",
                                "issue_number": 1707,
                                "issue_title": "Mixed-fleet widget",
                                "provider": "opencode",
                            }
                        }
                    ],
                    "completed": [],
                }
            )
        return StatusResult(error="offline")

    monkeypatch.setattr(network_mod, "check_all", fake_check_all)
    monkeypatch.setattr(network_mod, "fetch_status", fake_fetch_status)

    runner = CliRunner()
    result = runner.invoke(
        status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "(provider=opencode)" in result.output, result.output


def test_busy_line_omits_provider_when_absent(valid_config_path, monkeypatch) -> None:
    """The wire payload omits `provider` entirely for the implicit "claude"
    default (coord/dispatch.py's dispatch()) — the busy line must not print
    a stray "(provider=None)" in that case."""
    from coord import config as config_mod

    cfg = config_mod.load(valid_config_path)
    laptop = next(m for m in cfg.machines if m.name == "laptop")
    server = next(m for m in cfg.machines if m.name == "server")

    def fake_check_all(machines, timeout=3.0, max_workers=None):
        return [
            MachineStatus(machine=laptop, state="online", latency_ms=1.0),
            MachineStatus(machine=server, state="offline", reason="connection refused"),
        ]

    def fake_fetch_status(machine, timeout=3.0):
        if machine.name == "laptop":
            return StatusResult(
                data={
                    "active": [
                        {
                            "spec": {
                                "type": "work",
                                "issue_number": 1707,
                                "issue_title": "Mixed-fleet widget",
                            }
                        }
                    ],
                    "completed": [],
                }
            )
        return StatusResult(error="offline")

    monkeypatch.setattr(network_mod, "check_all", fake_check_all)
    monkeypatch.setattr(network_mod, "fetch_status", fake_fetch_status)

    runner = CliRunner()
    result = runner.invoke(
        status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "provider=" not in result.output, result.output


# ── #2305: tier-named providers.definitions must resolve, not degrade ──────


@pytest.fixture
def tier_provider_config_path(tmp_path: Path) -> Path:
    """`valid_config_path` plus a tier-named `providers.definitions` entry —
    the real fleet shape #2305 is about (`oc-mid` etc.), not a builtin
    provider *type* name."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        VALID_CONFIG
        + "\nproviders:\n"
        "  definitions:\n"
        "    oc-mid:\n"
        "      type: opencode\n"
    )
    return p


def test_status_resolves_tier_named_provider_without_warning(
    tier_provider_config_path, monkeypatch, coord_db, caplog: pytest.LogCaptureFixture,
) -> None:
    """#2305 acceptance criterion: `coord status` against a board holding a
    tier-named assignment (`oc-mid`, not a built-in `claude`/`claude-pty`/
    `opencode` type name) must resolve the assignment's own provider
    definition — no `get_provider: unknown provider_name` warning — because
    `build_session_usage()` (feeding the burn-rate line) is now handed
    `cfg`, which carries `providers.definitions`."""
    save_board(Board(completed=[_work("w1", provider_name="oc-mid")]))
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: [])
    runner = CliRunner()
    with caplog.at_level(logging.WARNING, logger="coord.providers"):
        result = runner.invoke(
            status_cmd,
            ["--config", str(tier_provider_config_path)],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
    assert not any(
        "unknown provider_name" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]
