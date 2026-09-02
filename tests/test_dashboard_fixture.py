"""#1538: `coord web --fixture` — the deterministic seeded-board server.

The web twin of ``coord-tui``'s ``make_test_app(BoardData)``. What these
tests actually pin down:

* the loader parses the committed ``tests/fixtures/board-pipeline-basic.json``
  and rejects malformed fixtures loudly (at load time, not on first request);
* every read endpoint answers from the fixture with **no database, no network,
  no tmux and no ``gh``** — asserted by making each of those boundaries throw;
* ``/api/pipeline`` is byte-identical across two independent app builds, which
  is the whole point: an acceptance suite on top of this is an oracle, not a
  flake generator;
* the committed fixture really does cover a row in every pipeline stage;
* writes are recorded, never executed — the live dispatch/merge/verdict seams
  are patched to explode, and the endpoints still return their normal success
  shapes;
* live (non-fixture) mode is untouched: no ``/api/fixture/*`` routes exist.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from coord.config import Config
from coord.dashboard.fixture import (
    FixtureError,
    ScriptedEvent,
    load_fixture,
    parse_fixture,
)
from coord.dashboard.server import build_app
from coord.models import Machine, Repo

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "board-pipeline-basic.json"


@pytest.fixture(autouse=True)
def _no_spa_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the legacy dashboard so GET / isn't the built SPA shell."""
    monkeypatch.setattr(
        "coord.dashboard.server.WEBAPP_DIST", Path("/nonexistent/dist")
    )


@pytest.fixture(autouse=True)
def _no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every I/O boundary the live dashboard uses explode.

    This is the real assertion behind "starts with no ~/.coord/coord.db
    present at all": if any fixture-mode request reached the DB, the fleet, or
    ``gh``, these blow up instead of silently succeeding on the dev machine's
    live state.
    """

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("fixture mode touched a live I/O boundary")

    monkeypatch.setattr("coord.db.get_connection", _boom)
    monkeypatch.setattr("coord.board_service.read_board", _boom)
    monkeypatch.setattr("coord.board_service.write_board", _boom)
    monkeypatch.setattr("coord.dashboard.server.read_board", _boom)
    monkeypatch.setattr("coord.dashboard.server.write_board", _boom)
    monkeypatch.setattr("coord.dashboard.server.load_proposals", _boom)
    monkeypatch.setattr("coord.dashboard.server.list_drive_queue", _boom)
    # #3023: GET /api/machines no longer fans out to the fleet at all (live
    # mode now reads the daemon's already-refreshed health snapshot instead
    # of probing `check_all`/`fetch_status`) — these two boundaries assert
    # fixture mode never falls through to THAT read path either.
    monkeypatch.setattr("coord.state.load_machine_health", _boom)
    monkeypatch.setattr("coord.client.fetch_board_payload", _boom)
    monkeypatch.setattr("coord.merge_queue.load_queue", _boom)
    monkeypatch.setattr("coord.state.load_assignment_review_findings", _boom)
    monkeypatch.setattr("coord.github_ops._gh", _boom)
    # #3026: GET /api/machines/health and GET /api/machines/metrics must serve
    # the seeded fleet_health block / metrics series in fixture mode, never
    # the live daemon-proxy (`fetch_machine_metrics`) or co-located
    # (`local_fleet_health_block`) read paths those endpoints use live.
    monkeypatch.setattr("coord.client.fetch_machine_metrics", _boom)
    monkeypatch.setattr("coord.health.aggregate.local_fleet_health_block", _boom)


def _config() -> Config:
    return Config(
        repos=[Repo(name="claude-coordinator", github="JDonaghy/claude-coordinator")],
        machines=[
            Machine(name="precision", host="precision.tailnet",
                    repos=["claude-coordinator"]),
            Machine(name="dellserver", host="dellserver.tailnet",
                    repos=["claude-coordinator"]),
        ],
    )


def _client(fixture=None) -> TestClient:
    fx = fixture if fixture is not None else load_fixture(FIXTURE_PATH)
    return TestClient(build_app(_config(), fixture=fx))


# ── Loader ──────────────────────────────────────────────────────────────────

class TestFixtureLoader:
    def test_loads_the_committed_fixture(self) -> None:
        fx = load_fixture(FIXTURE_PATH)
        board = fx.board()
        assert board.round_number == 7
        assert fx.now == 1750000000.0
        # One running work assignment; everything else terminal.
        assert [a.assignment_id for a in board.active] == ["work-running"]
        assert len(board.completed) == 8
        assert len(fx.merge_queue()) == 2
        assert [p.id for p in fx.proposals()] == [1]
        assert len(fx.events) == 3

    def test_board_is_rebuilt_per_call_so_mutations_cannot_leak(self) -> None:
        fx = load_fixture(FIXTURE_PATH)
        first = fx.board()
        first.active[0].status = "failed"
        first.active[0].files_allowed.append("mutated.py")
        second = fx.board()
        assert second.active[0].status == "running"
        assert second.active[0].files_allowed == []

    def test_accepts_a_top_level_board_payload(self) -> None:
        """A raw daemon GET /board capture drops in with no rewrapping."""
        fx = parse_fixture({"assignments": [], "round_number": 3})
        assert fx.board().round_number == 3

    def test_unknown_keys_are_tolerated(self) -> None:
        fx = parse_fixture({
            "board": {"assignments": []},
            "merge_queue": [{
                "assignment_id": "w1", "repo_name": "r", "repo_github": "o/r",
                "branch": "b", "target_branch": "main", "issue_number": 1,
                "issue_title": "t", "from_a_future_schema": True,
            }],
        })
        assert fx.merge_queue()[0].assignment_id == "w1"

    def test_missing_board_is_an_error(self) -> None:
        with pytest.raises(FixtureError, match="must define a 'board' object"):
            parse_fixture({"merge_queue": []})

    def test_missing_required_merge_queue_field_is_an_error(self) -> None:
        with pytest.raises(FixtureError, match="missing required field"):
            parse_fixture({
                "board": {"assignments": []},
                "merge_queue": [{"assignment_id": "w1"}],
            })

    def test_bad_event_entry_is_an_error(self) -> None:
        with pytest.raises(FixtureError, match=r"events\[0\].type is required"):
            parse_fixture({"board": {"assignments": []}, "events": [{"after": 1}]})

    def test_negative_event_delay_is_an_error(self) -> None:
        with pytest.raises(FixtureError, match="must be >= 0"):
            parse_fixture({
                "board": {"assignments": []},
                "events": [{"type": "x", "after": -1}],
            })

    def test_non_string_diff_is_an_error(self) -> None:
        with pytest.raises(FixtureError, match="'diffs' values must be strings"):
            parse_fixture({"board": {"assignments": []}, "diffs": {"w1": 5}})

    def test_non_string_chat_reply_is_an_error(self) -> None:
        with pytest.raises(FixtureError, match="'chat_reply' must be a string"):
            parse_fixture({"board": {"assignments": []}, "chat_reply": 5})

    def test_invalid_json_is_an_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        with pytest.raises(FixtureError, match="invalid JSON"):
            load_fixture(bad)

    def test_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(FixtureError, match="could not read fixture"):
            load_fixture(tmp_path / "absent.json")

    def test_inline_config_is_parsed_by_the_real_config_parser(self) -> None:
        fx = parse_fixture({
            "board": {"assignments": []},
            "config": {
                "repos": [{"name": "api", "github": "acme/api"}],
                "machines": [{"name": "m1", "host": "m1.tailnet", "repos": ["api"]}],
                "pipeline": {"default_gates": ["merge"]},
            },
        })
        cfg = fx.config()
        assert [r.name for r in cfg.repos] == ["api"]
        assert cfg.pipeline.default_gates == ["merge"]

    def test_config_falls_back_then_defaults(self) -> None:
        fx = parse_fixture({"board": {"assignments": []}})
        fallback = _config()
        assert fx.config(fallback) is fallback
        assert fx.config().repos == []


class TestSeededPayloadSchemaValidation:
    """#3050: a fixture that impersonates a schema it doesn't match fails to
    load, naming the route, the field(s), and (when loaded from a file) the
    path — rather than silently serving whatever shape it was authored with
    under a route whose OpenAPI schema says otherwise."""

    def _machines_fixture(self, machine: dict) -> dict:
        return {"board": {"assignments": []}, "machines": [machine]}

    def test_fields_the_real_handler_never_emits_are_rejected(self) -> None:
        """The exact #3050 repro: a fixture invents `severity` (a
        `/api/machines/health` field, never a `/api/machines` one) and
        other fields `GET /api/machines`'s real handler has never emitted."""
        with pytest.raises(FixtureError, match=r"does not match the /api/machines schema"):
            parse_fixture(self._machines_fixture({
                "name": "m1", "host": "m1.tailnet", "repos": ["api"],
                "state": "online", "reason": "",
                "severity": "unknown",
                "hand_paused": False,
                "headless_workers": 0,
                "is_local": True,
                "quiet_hours_paused": False,
                "reachable": True,
                "release_cordoned": False,
                "active_assignments": [],
                "concurrency_limit": 2,
                "agent_version": "1.0",
                "worktree_bytes": 0,
            }))

    def test_error_names_the_offending_field_and_the_file(self, tmp_path: Path) -> None:
        p = tmp_path / "bad-machines.json"
        p.write_text(json.dumps(self._machines_fixture({
            "name": "m1", "host": "m1.tailnet", "repos": ["api"],
            "state": "online", "reason": "",
            "severity": "unknown",
        })))
        with pytest.raises(FixtureError) as exc_info:
            load_fixture(p)
        msg = str(exc_info.value)
        assert "severity" in msg
        assert str(p) in msg
        assert "/api/machines" in msg

    def test_type_mismatch_is_rejected(self) -> None:
        with pytest.raises(FixtureError, match=r"expected number, got str"):
            parse_fixture(self._machines_fixture({
                "name": "m1", "host": "m1.tailnet", "repos": ["api"],
                "state": "online", "reason": "",
                "latency_ms": "fast",
            }))

    def test_a_partial_payload_missing_optional_fields_is_not_an_error(self) -> None:
        """Not a strict required-field match (#3050): a fixture legitimately
        seeds a degraded/partial state, e.g. a machine with no latency_ms —
        only extra/undeclared fields and type disagreements are rejected."""
        fx = parse_fixture(self._machines_fixture({"name": "m1"}))
        assert fx.machines() == [{"name": "m1"}]

    def test_unvalidated_routes_opts_a_route_out(self) -> None:
        fx = parse_fixture({
            **self._machines_fixture({
                "name": "m1", "host": "m1.tailnet", "repos": ["api"],
                "state": "online", "reason": "", "severity": "unknown",
            }),
            "unvalidated_routes": ["/api/machines"],
        })
        assert fx.machines()[0]["severity"] == "unknown"

    def test_drive_queue_extra_field_is_rejected(self) -> None:
        with pytest.raises(FixtureError, match=r"does not match the /api/drive-queue schema"):
            parse_fixture({
                "board": {"assignments": []},
                "drive_queue": [{
                    "repo_name": "api", "issue_number": 1,
                    "made_up_field_the_real_row_never_has": True,
                }],
            })

    def test_the_committed_fixtures_pass_validation(self) -> None:
        """The two fixture files this repo ships load cleanly — proves the
        check itself isn't so strict it rejects real, well-formed fixtures."""
        load_fixture(FIXTURE_PATH)
        load_fixture(
            Path(__file__).parent / "fixtures" / "board-pipeline-terminal-gates.json"
        )


# ── Reads flow through the real serialization ───────────────────────────────

class TestSeededReads:
    def test_board_endpoint(self) -> None:
        r = _client().get("/api/board")
        assert r.status_code == 200
        body = r.json()
        assert body["round_number"] == 7
        assert [a["assignment_id"] for a in body["active"]] == ["work-running"]

    def test_machines_and_sessions_never_probe_the_fleet(self) -> None:
        client = _client()
        machines = client.get("/api/machines").json()
        assert [m["name"] for m in machines] == ["precision", "dellserver"]
        assert machines[1]["state"] == "offline"
        sessions = client.get("/api/sessions").json()
        assert [s["session_id"] for s in sessions] == ["work-running"]

    def test_proposals_endpoint(self) -> None:
        proposals = _client().get("/api/proposals").json()
        assert [p["issue_number"] for p in proposals] == [4107]

    def test_drive_queue_endpoint(self) -> None:
        """GET /api/drive-queue in fixture mode (#2428 DQW-1).

        The committed fixture seeds three rows across two repos: a running
        claude-coordinator entry, a waiting claude-coordinator entry blocked
        on it via `after=`, and a `quadraui` entry whose fleet-scoped deploy
        gate has fired. That last row is what proves `FixtureServer.drive_queue()`
        is actually wired up — `[]` (the pre-#2428 default) would silently
        pass a naive shape-only assertion but could never produce
        `fleet_held: 1`/`level: "held"`.
        """
        r = _client().get("/api/drive-queue")
        assert r.status_code == 200
        data = r.json()

        entries = data["entries"]
        assert len(entries) == 3
        by_key = {(e["repo_name"], e["issue_number"]): e for e in entries}
        assert by_key[("claude-coordinator", 4201)]["state"] == "running"
        assert by_key[("claude-coordinator", 4202)]["after_json"] == [
            "claude-coordinator#4201",
        ]
        assert by_key[("quadraui", 88)]["hold_state"] == "fired"
        assert by_key[("quadraui", 88)]["hold_scope"] == "fleet"

        summary = data["summary"]
        assert summary["pending"] == 3
        assert summary["running"] == 1
        assert summary["waiting"] == 2
        # claude-coordinator#4202's after= is unsatisfied (#4201 is running,
        # not done), so only quadraui#88 (no after=) is eligible.
        assert summary["eligible"] == 1
        assert summary["held"] == 1
        assert summary["fleet_held"] == 1
        assert summary["level"] == "held"

    def test_drive_queue_repo_filter_narrows_entries_not_summary(self) -> None:
        """``?repo=`` in fixture mode: same fleet-wide-summary contract as live mode."""
        r = _client().get("/api/drive-queue", params={"repo": "claude-coordinator"})
        assert r.status_code == 200
        data = r.json()
        assert {e["repo_name"] for e in data["entries"]} == {"claude-coordinator"}
        assert len(data["entries"]) == 2
        # The quadraui fleet-held gate is invisible in `entries` but must
        # still show up in `summary` — see api_drive_queue's docstring.
        assert data["summary"]["fleet_held"] == 1
        assert data["summary"]["level"] == "held"
        assert data["summary"] == _client().get("/api/drive-queue").json()["summary"]

    def test_diff_is_seeded_not_shelled_out(self) -> None:
        client = _client()
        r = client.get("/api/diff/work-review-approved")
        assert r.status_code == 200
        assert r.json()["source"] == "fixture"
        assert "seeded diff" in r.json()["diff"]
        assert client.get("/api/diff/nope").status_code == 404

    def test_pipeline_covers_every_stage(self) -> None:
        """The #1538 acceptance criterion: a row in each stage."""
        views = _client().get("/api/pipeline").json()
        by_id = {v["assignment_id"]: v for v in views}
        assert by_id["work-running"]["current_stage"] == "coding"
        assert by_id["work-failed"]["current_stage"] == "failed"
        assert by_id["work-test-failed"]["current_stage"] == "done"
        assert by_id["work-test-failed"]["test_verdict"] == "failed"
        assert by_id["work-review-approved"]["current_stage"] == "review_done"
        assert by_id["work-review-approved"]["review_verdict"] == "approve"
        assert by_id["work-merge-queued"]["current_stage"] == "merge_ready"
        assert by_id["work-done"]["current_stage"] == "merged"

    def test_pipeline_carries_seeded_review_findings(self) -> None:
        views = _client().get("/api/pipeline").json()
        by_id = {v["assignment_id"]: v for v in views}
        body = by_id["work-review-approved"]["review_findings_body"]
        assert body is not None and "No blocking issues" in body

    def test_pipeline_uses_the_frozen_clock(self) -> None:
        """The one wall-clock-dependent field is pinned by the fixture's `now`.

        ``work-running`` is seeded 60 minutes past its 45-minute #846
        threshold, so ``needs_attention_detail`` embeds a rendered duration —
        which is exactly the field that would otherwise drift between runs.
        """
        views = _client().get("/api/pipeline").json()
        running = next(v for v in views if v["assignment_id"] == "work-running")
        assert running["needs_attention"] is True
        assert running["needs_attention_reason"] == "wall_clock"
        assert running["needs_attention_detail"] == (
            "Running 60m, past the 45m threshold for type='work'."
        )


class TestMachinesPanelFixtures:
    """Machines-panel seeding for coord-web's Machines panel milestone (#3026).

    ``GET /api/machines`` and ``GET /api/sessions`` were already covered by
    ``TestSeededReads.test_machines_and_sessions_never_probe_the_fleet`` —
    this class covers the three endpoints #3020-#3025 added on top: metrics,
    health, and per-machine work stats. Every one of these must serve seeded
    data and never reach the fleet, mirroring the rest of this module.
    """

    def test_machine_metrics_serves_the_seeded_series(self) -> None:
        """#3021/#3022's response shape, sourced from the fixture (#3026)."""
        r = _client().get("/api/machines/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["schema"] == 1
        assert body["generated_at"] == 1750000000.0
        assert set(body["machines"]) == {"precision", "dellserver"}
        precision = body["machines"]["precision"]
        assert [s["status"] for s in precision] == [
            "ok", "ok", "unknown", "ok", "ok", "ok",
        ]
        # The deliberate gap: no sample between the unknown one at -90s and
        # the next real reading at -30s (a 60s hole a renderer must draw as
        # a break, not interpolate across).
        assert precision[2]["timestamp"] == 1749999910.0
        assert precision[3]["timestamp"] == 1749999970.0
        assert precision[3]["timestamp"] - precision[2]["timestamp"] == 60.0
        # dellserver is unknown end-to-end, matching its offline `machines` row.
        dellserver = body["machines"]["dellserver"]
        assert dellserver and all(s["status"] == "unknown" for s in dellserver)

    def test_machine_metrics_narrows_to_one_machine(self) -> None:
        body = _client().get("/api/machines/metrics", params={"machine": "precision"}).json()
        assert set(body["machines"]) == {"precision"}

    def test_machine_metrics_since_and_resolution_use_the_real_pipeline(self) -> None:
        """``since``/``resolution`` run through the real filter/downsample code
        (``coord.machine_metrics``), not a fixture-only reimplementation."""
        client = _client()
        since_body = client.get(
            "/api/machines/metrics", params={"since": "45s"}
        ).json()
        # now=1750000000.0, so since=now-45s=1749999955.0 keeps only the
        # last two precision samples (-30s and -15s... actually 0s/-15s/-30s
        # are >= 1749999955? recompute: samples at -30s/-15s/0s are all >=).
        assert [s["timestamp"] for s in since_body["machines"]["precision"]] == [
            1749999970.0, 1749999985.0, 1750000000.0,
        ]
        resolution_body = client.get(
            "/api/machines/metrics", params={"resolution": 3}
        ).json()
        assert len(resolution_body["machines"]["precision"]) == 3
        assert len(resolution_body["machines"]["dellserver"]) <= 3

    def test_machine_metrics_bad_resolution_is_a_400(self) -> None:
        r = _client().get("/api/machines/metrics", params={"resolution": "0"})
        assert r.status_code == 400
        assert "resolution" in r.json()["error"]

    def test_machine_metrics_bad_since_is_a_400(self) -> None:
        r = _client().get("/api/machines/metrics", params={"since": "not-a-time"})
        assert r.status_code == 400

    def test_machine_metrics_absent_from_fixture_is_an_empty_series(self) -> None:
        """No ``machine_metrics`` key at all -- every machine reads ``[]``,
        never an error (mirrors the live sampler's "never polled" case)."""
        fx = parse_fixture({"board": {"assignments": []}})
        body = _client(fx).get("/api/machines/metrics").json()
        assert body["machines"] == {}

    def test_machine_health_spans_all_four_severities_plus_a_stale_row(self) -> None:
        r = _client().get("/api/machines/health")
        assert r.status_code == 200
        body = r.json()
        assert body["schema"] == 1
        by_machine = {row["machine"]: row for row in body["machine_health"]}
        assert by_machine["precision"]["severity"] == "ok"
        assert by_machine["dellserver"]["severity"] == "unknown"
        assert by_machine["gpu-box"]["severity"] == "warn"
        assert by_machine["build-runner"]["severity"] == "crit"
        # #1630's honesty contract: `stale` and `severity` are independent —
        # spare-mini's last-known reading was OK, but it is stale, so a
        # renderer must be able to tell "OK" apart from "OK a while ago".
        assert by_machine["spare-mini"]["severity"] == "ok"
        assert by_machine["spare-mini"]["stale"] is True
        assert all(
            row["stale"] is False
            for name, row in by_machine.items()
            if name != "spare-mini"
        )

    def test_machine_health_absent_from_fixture_is_the_empty_block(self) -> None:
        fx = parse_fixture({"board": {"assignments": []}})
        body = _client(fx).get("/api/machines/health").json()
        assert body["machine_health"] == []
        assert body["schema"] == 1

    def test_machine_stats_are_derived_from_the_seeded_board(self) -> None:
        """#3025's endpoint needs no dedicated fixture key -- it reads the
        seeded board + the ``Config`` the app was built with, same as live."""
        r = _client().get("/api/machines/stats")
        assert r.status_code == 200
        by_name = {row["name"]: row for row in r.json()}
        assert set(by_name) == {"precision", "dellserver"}
        # precision has one running assignment (work-running) seeded above.
        assert by_name["precision"]["capacity"]["active"] == 1
        assert by_name["precision"]["counts"]["completed"] >= 1
        assert by_name["dellserver"]["counts"]["failed"] == 1
        assert any(
            j["assignment_id"] == "work-failed"
            for j in by_name["dellserver"]["job_history"]
        )


class TestReportEndpoints:
    """GET /api/report + GET /api/report/{report_id} in fixture mode (#2492 RPT-1).

    Unlike every other seeded read in this file, ``FixtureServer.report_catalogue()``
    falls back to the REAL ``coord.reports.catalogue()`` when a fixture doesn't
    override it — it is pure in-process metadata (no DB read), so that fallback
    stays deterministic and needs no seeding. ``report_result()`` has no such
    fallback: a report id absent from the fixture's ``report_results`` 404s,
    proving the route never reaches for the live DB the ``_no_io`` autouse
    fixture above has already wired to explode.
    """

    def _fixture_with_reports(self):
        return parse_fixture({
            "board": {"assignments": [], "round_number": 0},
            "report_results": {
                "drive-queue-status": {
                    "report_id": "drive-queue-status",
                    "generated_at": 1750000000.0,
                    "window": [1749999000.0, 1750000000.0],
                    "columns": ["repo", "issue", "state"],
                    "rows": [
                        {"repo": "claude-coordinator", "issue": 4201, "state": "running"},
                    ],
                    "notes": ["#2492 fixture note"],
                    "column_meta": [
                        {"id": "repo", "label": "Repo", "kind": "text", "align": "left", "weight": 1.0},
                        {"id": "issue", "label": "Issue", "kind": "int", "align": "right", "weight": 1.0},
                        {"id": "state", "label": "State", "kind": "enum", "align": "left", "weight": 1.0},
                    ],
                    "totals": None,
                    "chart": None,
                },
            },
        })

    def test_catalogue_falls_back_to_the_real_registry_when_not_seeded(self) -> None:
        """No `report_catalogue` key in the fixture -> the real static
        registry, still with no DB/network touched (`_no_io` would explode)."""
        client = _client(self._fixture_with_reports())
        r = client.get("/api/report")
        assert r.status_code == 200
        ids = [rep["id"] for rep in r.json()["reports"]]
        assert "drive-queue-status" in ids
        assert "issue-activity" in ids

    def test_catalogue_override_wins_when_seeded(self) -> None:
        fx = parse_fixture({
            "board": {"assignments": [], "round_number": 0},
            "report_catalogue": {
                "reports": [
                    {
                        "id": "only-one",
                        "title": "Only One",
                        "description": "a fixture-only report",
                        "params": [],
                        "row_identity": None,
                    },
                ],
            },
        })
        client = _client(fx)
        r = client.get("/api/report")
        assert r.status_code == 200
        assert [rep["id"] for rep in r.json()["reports"]] == ["only-one"]

    def test_run_returns_the_seeded_result(self) -> None:
        client = _client(self._fixture_with_reports())
        r = client.get("/api/report/drive-queue-status")
        assert r.status_code == 200
        body = r.json()
        assert body["report_id"] == "drive-queue-status"
        assert body["rows"] == [
            {"repo": "claude-coordinator", "issue": 4201, "state": "running"},
        ]
        assert body["notes"] == ["#2492 fixture note"]

    def test_run_format_csv_over_a_seeded_result(self) -> None:
        client = _client(self._fixture_with_reports())
        r = client.get(
            "/api/report/drive-queue-status", params={"format": "csv"}
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment; filename=" in r.headers["content-disposition"]
        assert "claude-coordinator" in r.text
        assert "# report: drive-queue-status" in r.text

    def test_run_404s_on_a_report_id_absent_from_seeded_results(self) -> None:
        """No live DB fallback in fixture mode — an unseeded report id is a
        404, not a silent hit against the (patched-to-explode) real engine."""
        client = _client(self._fixture_with_reports())
        r = client.get("/api/report/issue-activity")
        assert r.status_code == 404
        assert "drive-queue-status" in r.json()["error"]

    def test_run_unknown_format_is_still_a_400(self) -> None:
        client = _client(self._fixture_with_reports())
        r = client.get(
            "/api/report/drive-queue-status", params={"format": "xlsx"}
        )
        assert r.status_code == 400
        assert "csv" in r.json()["error"]


class TestDeterminism:
    def test_two_runs_produce_byte_identical_pipeline_output(self) -> None:
        """Two independently-built apps, same fixture, identical bytes."""
        first = _client().get("/api/pipeline")
        second = _client().get("/api/pipeline")
        assert first.content == second.content

    def test_repeated_requests_on_one_app_are_identical(self) -> None:
        client = _client()
        assert client.get("/api/pipeline").content == client.get("/api/pipeline").content

    def test_a_recorded_write_does_not_move_the_board(self) -> None:
        client = _client()
        before = client.get("/api/pipeline").content
        r = client.post("/api/pipeline/action", json={
            "assignment_id": "work-running", "action": "unstick",
        })
        assert r.status_code == 200
        assert client.get("/api/pipeline").content == before


# ── Writes are recorded, never executed ─────────────────────────────────────

@pytest.fixture()
def _no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every real side-effect seam the action endpoint could reach, armed."""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("fixture mode executed a write")

    monkeypatch.setattr("coord.review.dispatch_review", _boom)
    monkeypatch.setattr("coord.review.dispatch_headless_fix", _boom)
    monkeypatch.setattr("coord.smoke.dispatch_smoke", _boom)
    monkeypatch.setattr("coord.merge_queue.enqueue", _boom)
    monkeypatch.setattr("coord.merge_queue.process", _boom)
    monkeypatch.setattr("coord.state.record_test_verdict", _boom)
    monkeypatch.setattr("coord.notify._persist_review_findings", _boom)
    monkeypatch.setattr("coord.notify.post_orphaned_review_findings", _boom)
    monkeypatch.setattr("coord.dispatch.dispatch", _boom)


@pytest.mark.usefixtures("_no_dispatch")
class TestRecordedActions:
    def test_action_log_starts_empty(self) -> None:
        assert _client().get("/api/fixture/actions").json() == {"actions": []}

    def test_dispatch_review_is_recorded_with_its_normal_success_shape(self) -> None:
        client = _client()
        r = client.post("/api/pipeline/action", json={
            "assignment_id": "work-test-failed", "action": "dispatch_review",
        })
        assert r.status_code == 200
        assert r.json() == {
            "ok": True,
            "machine_name": "precision",
            "assignment_id": "fixture-review-work-test-failed",
        }
        actions = client.get("/api/fixture/actions").json()["actions"]
        assert len(actions) == 1
        assert actions[0]["seq"] == 1
        assert actions[0]["endpoint"] == "/api/pipeline/action"
        assert actions[0]["action"] == "dispatch_review"
        assert actions[0]["payload"]["assignment_id"] == "work-test-failed"
        assert actions[0]["at"] == 1750000000.0

    def test_merge_is_recorded(self) -> None:
        client = _client()
        r = client.post("/api/pipeline/action", json={
            "assignment_id": "work-merge-queued", "action": "merge",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["events"][0]["kind"] == "merged"
        assert client.get("/api/fixture/actions").json()["actions"][0]["action"] == "merge"

    def test_merge_404s_when_not_in_the_queue(self) -> None:
        client = _client()
        r = client.post("/api/pipeline/action", json={
            "assignment_id": "work-running", "action": "merge",
        })
        assert r.status_code == 404
        # A rejected request is not a recorded action.
        assert client.get("/api/fixture/actions").json()["actions"] == []

    def test_test_verdict_maps_to_the_canonical_state(self) -> None:
        client = _client()
        r = client.post("/api/pipeline/action", json={
            "assignment_id": "work-test-failed", "action": "test-verdict",
            "verdict": "pass",
        })
        assert r.json() == {"ok": True, "test_state": "passed"}

    def test_bad_verdict_is_rejected_the_same_way_as_live(self) -> None:
        client = _client()
        r = client.post("/api/pipeline/action", json={
            "assignment_id": "work-test-failed", "action": "test-verdict",
            "verdict": "maybe",
        })
        assert r.status_code == 400
        assert client.get("/api/fixture/actions").json()["actions"] == []

    def test_record_review_verdict_requires_a_linked_review(self) -> None:
        client = _client()
        r = client.post("/api/pipeline/action", json={
            "assignment_id": "work-test-failed", "action": "record-review-verdict",
            "verdict": "approve", "body": "ok",
        })
        assert r.status_code == 404
        r = client.post("/api/pipeline/action", json={
            "assignment_id": "work-review-approved", "action": "record-review-verdict",
            "verdict": "approve", "body": "ok",
        })
        assert r.status_code == 200 and r.json() == {"ok": True}

    def test_unknown_action_and_unknown_assignment(self) -> None:
        client = _client()
        assert client.post("/api/pipeline/action", json={
            "assignment_id": "work-running", "action": "teleport",
        }).status_code == 400
        assert client.post("/api/pipeline/action", json={
            "assignment_id": "ghost", "action": "enqueue",
        }).status_code == 404

    def test_retry_still_reports_not_implemented(self) -> None:
        r = _client().post("/api/pipeline/action", json={
            "assignment_id": "work-failed", "action": "retry",
        })
        assert r.status_code == 501

    def test_approve_and_reject_are_recorded(self) -> None:
        client = _client()
        r = client.post("/api/approve", json={"ids": [1]})
        assert r.status_code == 200
        assert r.json() == {"results": [{"id": 1, "assignment_id": "fixture-1", "ok": True}]}
        r = client.post("/api/reject", json={"ids": [1]})
        assert r.json() == {"removed": 1, "remaining": 0}
        assert [a["action"] for a in client.get("/api/fixture/actions").json()["actions"]] == [
            "approve", "reject",
        ]

    def test_approve_404s_on_an_unknown_proposal(self) -> None:
        assert _client().post("/api/approve", json={"ids": [999]}).status_code == 404

    def test_chat_never_spawns_a_provider(self) -> None:
        client = _client()
        r = client.post("/api/chat", json={"message": "what is running?"})
        assert r.status_code == 200
        assert "no provider is contacted" in r.text
        assert "[DONE]" in r.text
        assert client.get("/api/fixture/actions").json()["actions"][0]["action"] == "chat"

    def test_action_log_can_be_cleared(self) -> None:
        client = _client()
        client.post("/api/pipeline/action", json={
            "assignment_id": "work-running", "action": "unstick",
        })
        assert client.request("DELETE", "/api/fixture/actions").json() == {"cleared": 1}
        assert client.get("/api/fixture/actions").json()["actions"] == []
        # seq restarts, so assertions after a reset stay simple.
        client.post("/api/pipeline/action", json={
            "assignment_id": "work-running", "action": "unstick",
        })
        assert client.get("/api/fixture/actions").json()["actions"][0]["seq"] == 1


class TestDriveQueueActionRecordedActions:
    """POST /api/drive-queue/action in fixture mode (#2429 DQW-2).

    Uses a small inline fixture (rather than the committed
    ``board-pipeline-basic.json``, which has no ``blocked`` row) so each
    action's guard has something to actually refuse. Mirrors
    ``TestRecordedActions``: the write is recorded, never executed, and a
    rejected request never reaches the log.
    """

    def _fixture(self):
        return parse_fixture({
            "board": {"assignments": [], "round_number": 0},
            "drive_queue": [
                {
                    "repo_name": "claude-coordinator", "issue_number": 1,
                    "position": 0, "machine": "precision", "after_json": [],
                    "state": "blocked", "hold_state": "",
                },
                {
                    "repo_name": "claude-coordinator", "issue_number": 2,
                    "position": 1, "machine": "", "after_json": [],
                    "state": "waiting", "hold_state": "fired",
                    "hold_probes": 3,
                },
            ],
        })

    def test_move_is_recorded(self) -> None:
        client = _client(self._fixture())
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "claude-coordinator", "issue_number": 2,
            "action": "move", "to_position": 0,
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        actions = client.get("/api/fixture/actions").json()["actions"]
        assert len(actions) == 1
        assert actions[0]["endpoint"] == "/api/drive-queue/action"
        assert actions[0]["action"] == "move"
        assert actions[0]["payload"]["to_position"] == 0
        # Recorded, not executed — the seeded queue order is untouched.
        entries = client.get("/api/drive-queue").json()["entries"]
        by_key = {(e["repo_name"], e["issue_number"]): e["position"] for e in entries}
        assert by_key[("claude-coordinator", 1)] == 0
        assert by_key[("claude-coordinator", 2)] == 1

    def test_remove_is_recorded(self) -> None:
        client = _client(self._fixture())
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "claude-coordinator", "issue_number": 2, "action": "remove",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert client.get("/api/fixture/actions").json()["actions"][0]["action"] == "remove"
        # Still there — nothing was actually dequeued.
        assert len(client.get("/api/drive-queue").json()["entries"]) == 2

    def test_unblock_is_recorded(self) -> None:
        client = _client(self._fixture())
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "claude-coordinator", "issue_number": 1, "action": "unblock",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert client.get("/api/fixture/actions").json()["actions"][0]["action"] == "unblock"
        # Recorded, not executed — the row is still `blocked`.
        entries = client.get("/api/drive-queue").json()["entries"]
        by_key = {e["issue_number"]: e for e in entries}
        assert by_key[1]["state"] == "blocked"

    def test_unblock_refuses_a_non_blocked_row(self) -> None:
        client = _client(self._fixture())
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "claude-coordinator", "issue_number": 2, "action": "unblock",
        })
        assert r.status_code == 400
        assert r.json()["ok"] is False
        # A rejected request is not a recorded action.
        assert client.get("/api/fixture/actions").json()["actions"] == []

    def test_resume_is_recorded(self) -> None:
        client = _client(self._fixture())
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "claude-coordinator", "issue_number": 2, "action": "resume",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert client.get("/api/fixture/actions").json()["actions"][0]["action"] == "resume"
        # Recorded, not executed — the gate is still fired.
        entries = client.get("/api/drive-queue").json()["entries"]
        by_key = {e["issue_number"]: e for e in entries}
        assert by_key[2]["hold_state"] == "fired"

    def test_resume_refuses_an_unfired_gate(self) -> None:
        client = _client(self._fixture())
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "claude-coordinator", "issue_number": 1, "action": "resume",
        })
        assert r.status_code == 400
        assert r.json()["ok"] is False
        assert client.get("/api/fixture/actions").json()["actions"] == []

    def test_unknown_entry_and_unknown_action(self) -> None:
        client = _client(self._fixture())
        assert client.post("/api/drive-queue/action", json={
            "repo_name": "claude-coordinator", "issue_number": 999, "action": "remove",
        }).status_code == 404
        assert client.post("/api/drive-queue/action", json={
            "repo_name": "claude-coordinator", "issue_number": 1, "action": "teleport",
        }).status_code == 400
        assert client.get("/api/fixture/actions").json()["actions"] == []


# ── Scripted SSE ────────────────────────────────────────────────────────────

async def _collect_sse(app, *, until: str, timeout: float = 3.0) -> str:
    """Drive ``/events`` as raw ASGI and collect chunks until *until* appears.

    Same technique as ``tests/test_events.py`` and for the same reason:
    Starlette's sync ``TestClient`` buffers a never-ending streaming response,
    so the SSE surface has to be driven directly.  ``Last-Event-ID: 0`` makes
    the read deterministic — already-published events arrive as backfill, so
    the test never has to race a publisher.
    """
    received: list[bytes] = []
    done = asyncio.Event()

    async def receive() -> dict:
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            received.append(message.get("body", b""))
            if until in b"".join(received).decode("utf-8", errors="replace"):
                done.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events",
        "raw_path": b"/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"last-event-id", b"0")],
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    return b"".join(received).decode("utf-8", errors="replace")


async def _post_asgi(app, path: str, body: dict | None = None) -> dict:
    """POST through the ASGI app on the *caller's* event loop.

    ``TestClient`` runs the app on its own portal loop and tears it down when
    the request returns, which would kill the replay's background task before
    a delayed scripted event ever fires.  Driving the app in-loop keeps the
    task alive alongside :func:`_collect_sse`.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(path, json=body if body is not None else {})
        return resp.json()


def _instant_events(fx):  # noqa: ANN001, ANN201
    """Collapse a fixture's scripted delays so tests don't sleep the timeline."""
    fx.events = [ScriptedEvent(type=e.type, data=e.data, after=0.0) for e in fx.events]
    return fx


class TestScriptedEvents:
    @pytest.mark.asyncio
    async def test_ad_hoc_publish_reaches_subscribers(self) -> None:
        app = build_app(_config(), fixture=load_fixture(FIXTURE_PATH))
        published = await _post_asgi(app, "/api/fixture/events", {
            "type": "board_updated", "data": {"n": 1},
        })
        assert published["ok"] is True
        body = await _collect_sse(app, until='"n": 1')
        assert "event: board_updated" in body

    def test_publish_requires_a_type(self) -> None:
        assert _client().post("/api/fixture/events", json={}).status_code == 400

    @pytest.mark.asyncio
    async def test_replay_runs_the_scripted_sequence_in_order(self) -> None:
        app = build_app(
            _config(), fixture=_instant_events(load_fixture(FIXTURE_PATH))
        )
        assert await _post_asgi(app, "/api/fixture/events/replay") == {
            "ok": True, "count": 3,
        }
        body = await _collect_sse(app, until="event: assignment_completed")
        types = [
            line[len("event: "):]
            for line in body.splitlines()
            if line.startswith("event: ")
        ]
        assert types == [
            "board_updated", "assignment_needs_attention", "assignment_completed",
        ]
        # The seeded payloads ride through untouched.
        assert '"reason": "wall_clock"' in body

    @pytest.mark.asyncio
    async def test_scripted_delays_are_honoured(self) -> None:
        """`after` is a per-entry delay, so a script reads as a timeline."""
        fx = parse_fixture({
            "board": {"assignments": []},
            "events": [
                {"type": "board_updated", "data": {"n": 1}},
                {"type": "board_updated", "data": {"n": 2}, "after": 0.05},
            ],
        })
        app = build_app(_config(), fixture=fx)
        await _post_asgi(app, "/api/fixture/events/replay")
        body = await _collect_sse(app, until='"n": 2')
        assert body.index('"n": 1') < body.index('"n": 2')

    def test_no_scripted_events_means_no_startup_task(self) -> None:
        """A fixture with no script still boots (and never polls the fleet)."""
        fx = parse_fixture({"board": {"assignments": []}})
        with TestClient(build_app(_config(), fixture=fx)) as client:
            assert client.get("/api/board").json()["active"] == []

    @pytest.mark.asyncio
    async def test_script_does_not_autoplay_by_default(self) -> None:
        """A timeline racing the client's connect is the flake this mode kills.

        Running the lifespan (``with TestClient(...)``) must publish nothing;
        the committed fixture's three events only arrive on an explicit replay.
        """
        app = build_app(
            _config(), fixture=_instant_events(load_fixture(FIXTURE_PATH))
        )
        with TestClient(app):
            pass
        with pytest.raises(asyncio.TimeoutError):
            await _collect_sse(app, until="event: ", timeout=0.4)

    @pytest.mark.asyncio
    async def test_autoplay_opt_in_plays_on_startup(self) -> None:
        fx = parse_fixture({
            "board": {"assignments": []},
            "autoplay_events": True,
            "events": [{"type": "board_updated", "data": {"n": 1}}],
        })
        assert fx.autoplay_events is True
        app = build_app(_config(), fixture=fx)
        with TestClient(app):
            pass
        body = await _collect_sse(app, until='"n": 1')
        assert "event: board_updated" in body


# ── Live mode is untouched ──────────────────────────────────────────────────

class TestLiveModeUnchanged:
    def test_no_fixture_routes_without_a_fixture(self) -> None:
        app = build_app(_config())
        paths = {getattr(r, "path", None) for r in app.routes}
        assert not any(p and p.startswith("/api/fixture") for p in paths)

    def test_fixture_routes_are_excluded_from_the_openapi_inventory(self) -> None:
        app = build_app(_config(), fixture=load_fixture(FIXTURE_PATH))
        fixture_routes = [
            r for r in app.routes
            if getattr(r, "path", "").startswith("/api/fixture")
        ]
        assert len(fixture_routes) == 3
        assert all(r.include_in_schema is False for r in fixture_routes)


# ── CLI wiring ──────────────────────────────────────────────────────────────

class TestWebCommand:
    def test_fixture_flag_builds_a_seeded_app_without_a_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`coord web --fixture` must start on a box with no coordinator.yml."""
        from click.testing import CliRunner

        from coord.commands.lifecycle import web

        captured: dict = {}

        def _fake_run(app, **kwargs):  # noqa: ANN001, ANN003
            captured["app"] = app
            captured["kwargs"] = kwargs

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", _fake_run)
        monkeypatch.setattr(
            "coord.dashboard.terminal.resolve_web_token", lambda t: t
        )
        # No coordinator.yml anywhere.
        monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "absent.yml"))

        result = CliRunner().invoke(
            web, ["--fixture", str(FIXTURE_PATH), "--port", "7999"]
        )
        assert result.exit_code == 0, result.output
        assert "fixture mode" in result.output
        client = TestClient(captured["app"])
        assert client.get("/api/board").json()["round_number"] == 7

    def test_a_broken_fixture_fails_the_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from click.testing import CliRunner

        from coord.commands.lifecycle import web

        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"merge_queue": []}))
        result = CliRunner().invoke(web, ["--fixture", str(bad)])
        assert result.exit_code != 0
        assert "board" in result.output
