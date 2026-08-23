"""#1632: ``coord notifier`` — the surfaces that make a silent channel visible.

The notifier's healthy state is silence, which makes it the easiest
subsystem in the fleet to have quietly broken for a month. These commands
are the antidote, so they get the same "must not break" treatment as the
tick itself.
"""

from __future__ import annotations

import json
import textwrap

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.notifier import store

CONFIG_YAML = textwrap.dedent("""
    repos:
      - name: coord
        github: owner/coord
    machines:
      - name: dellserver
        host: dellserver
        repos: [coord]
    notifications:
      enabled: true
      ntfy_url: http://dellserver:7440
      ntfy_topic: coord-fleet
      web_base_url: http://dellserver:7434
      quiet_hours:
        start: "22:00"
        end: "08:00"
        tz: America/Chicago
""")


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "coordinator.yml"
    path.write_text(CONFIG_YAML)
    return str(path)


def run(*args):
    return CliRunner().invoke(main, list(args))


def test_notifier_group_is_registered():
    result = run("notifier", "--help")
    assert result.exit_code == 0
    for sub in ("status", "baselines", "pending", "tick", "test", "urgent"):
        assert sub in result.output


def test_status_reports_target_and_window(config_path):
    result = run("notifier", "status", "--config", config_path)
    assert result.exit_code == 0
    assert "ENABLED" in result.output
    assert "http://dellserver:7440/coord-fleet" in result.output
    assert "22:00–08:00 America/Chicago" in result.output


def test_status_json_names_the_state_file(config_path):
    result = run("notifier", "status", "--config", config_path, "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["enabled"] is True
    assert payload["state_path"] == str(store.state_path())
    assert payload["held_events"] == 0


def test_status_on_a_deployment_with_no_block(tmp_path):
    path = tmp_path / "coordinator.yml"
    path.write_text(textwrap.dedent("""
        repos:
          - name: coord
            github: owner/coord
        machines:
          - name: dellserver
            host: dellserver
            repos: [coord]
    """))
    result = run("notifier", "status", "--config", str(path))
    assert result.exit_code == 0
    assert "disabled" in result.output


def test_baselines_reports_both_p90_and_2x_median(config_path, monkeypatch):
    from coord.notifier import service

    rows = [
        {"repo_name": "coord", "type": "work", "issue_number": 1, "status": "done",
         "dispatched_at": 0.0, "finished_at": 600.0}
        for _ in range(10)
    ]
    monkeypatch.setattr("coord.notifier.collect.history_rows", lambda: rows)
    monkeypatch.setattr("coord.notifier.collect.issue_label_index", lambda: {})
    assert service.compute_baselines  # imported for clarity

    result = run("notifier", "baselines", "--config", config_path)
    assert result.exit_code == 0
    assert "p90" in result.output and "2xmed" in result.output
    assert "coord/work/untiered" in result.output


def test_baselines_json_exposes_the_alternative_for_comparison(config_path, monkeypatch):
    rows = [
        {"repo_name": "coord", "type": "work", "issue_number": 1, "status": "done",
         "dispatched_at": 0.0, "finished_at": float(600 + i)}
        for i in range(10)
    ]
    monkeypatch.setattr("coord.notifier.collect.history_rows", lambda: rows)
    monkeypatch.setattr("coord.notifier.collect.issue_label_index", lambda: {})
    result = run("notifier", "baselines", "--config", config_path, "--json")
    assert result.exit_code == 0
    entry = json.loads(result.output)[0]
    assert entry["p90_secs"] and entry["p2x_median_secs"]
    assert entry["cold"] is False


def test_baselines_with_no_history_says_so(config_path, monkeypatch):
    monkeypatch.setattr("coord.notifier.collect.history_rows", lambda: [])
    monkeypatch.setattr("coord.notifier.collect.issue_label_index", lambda: {})
    result = run("notifier", "baselines", "--config", config_path)
    assert result.exit_code == 0
    assert "every stratum is cold" in result.output


def test_urgent_marks_and_clears_a_drive(config_path):
    result = run("notifier", "urgent", "coord", "42", "--config", config_path)
    assert result.exit_code == 0
    assert store.urgent_keys(store.load_state(), now=0.0) == {"coord#42"}

    cleared = run("notifier", "urgent", "coord", "42", "--clear", "--config", config_path)
    assert cleared.exit_code == 0
    assert store.urgent_keys(store.load_state(), now=0.0) == set()


def test_test_send_reports_a_transport_failure_non_zero(config_path, monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("down"))
    )
    result = run("notifier", "test", "--config", config_path)
    assert result.exit_code == 1
    assert "send failed" in result.output


def test_test_send_reports_success(config_path, monkeypatch):
    import httpx

    class Response:
        status_code = 200

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: Response())
    result = run("notifier", "test", "--config", config_path)
    assert result.exit_code == 0
    assert "sent via ntfy" in result.output


def test_tick_dry_run_delivers_nothing_and_writes_no_ledger(config_path, monkeypatch):
    from coord.notifier.predicate import PipelineSnapshot, WorkerProbe

    # #2609: a probe past its (cold) duration ceiling with fresh output must
    # raise nothing — duration alone no longer pages. Give it a stale
    # `last_output_at` too (past the cold silence threshold) so this test
    # still exercises a real event, via the silence probe, to prove the
    # dry-run path delivers-but-does-not-ledger it.
    now = 3_000_000.0
    monkeypatch.setattr(
        "coord.notifier.collect.collect",
        lambda config, **kw: PipelineSnapshot(
            now=kw["now"],
            probes=[WorkerProbe(assignment_id="a1", repo="coord", issue=42,
                                machine="dellserver", dispatched_at=kw["now"] - 20 * 3600.0,
                                last_output_at=kw["now"] - 40 * 60.0)],
        ),
    )
    monkeypatch.setattr("coord.notifier.collect.history_rows", lambda: [])
    monkeypatch.setattr("coord.notifier.collect.issue_label_index", lambda: {})
    monkeypatch.setattr("time.time", lambda: now)

    result = run("notifier", "tick", "--config", config_path, "--dry-run")
    assert result.exit_code == 0
    assert "output_silence" in result.output
    assert store.load_state().ledger == {}


def test_pending_lists_held_events():
    from coord.notifier.models import NotifyEvent

    state = store.load_state()
    state.deferred.append(
        NotifyEvent(subject="a1", condition="drive_halted", title="coord#42 — drive halted",
                    body="b", created_at=0.0)
    )
    store.save_state(state)
    result = run("notifier", "pending")
    assert result.exit_code == 0
    assert "coord#42" in result.output
