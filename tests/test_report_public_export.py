"""Tests for the #3474 public report export — ``coord/reports.py``'s
redaction fold, ``coord report export --public``, and the dashboard's
``GET /api/report/{id}/public``.

Three layers, same split this repo's report tests already use:

* :func:`redact_report_for_public` is a **pure** fold over a fixture
  ``ReportResult`` — no DB, no config load — asserting the acceptance bar
  verbatim: a private repo's name, issue number and title are gone from
  every row, every note, and (once serialised) every byte of the exported
  HTML and CSV; an allowlisted repo's row is untouched.
* :func:`run_public_export` / :func:`assert_public_export_allowed` cover the
  "a public number with no stated basis is not produced" refusal, and
  ``coord.config._parse_reporting`` covers the ``reporting.public.
  allowlist_repos`` parse.
* the CLI (``coord report export``) and the dashboard route
  (``GET /api/report/{id}/public``) are black-boxed end to end, each
  resolving the allowlist off its own config source (``--config`` / the
  app's bound ``Config``) and writing/serving the identical redacted
  bytes.
* ``TestRealReportCatalogueRedaction``/``TestPublicHtmlScriptInjection``
  cover the PR #3541 review findings: a report with no per-row ``repo``
  identity at all (``queue-outcomes``) and two reports whose KEPT rows
  embed a different repo's ``"repo#issue"`` ref in a list column
  (``decisions``' ``downstream``, ``drive-queue-status``'s ``after``), plus
  the inline chart ``<script>`` JSON-breakout.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from coord.cli import main
from coord.config import Config, ConfigError, PublicReportingConfig, _parse_reporting
from coord.dashboard.fixture import FixtureServer
from coord.dashboard.server import build_app
from coord.drive_queue import entry_key
from coord.models import Machine, Repo
from coord.reports import (
    PUBLIC_PRIVATE_REPO_LABEL,
    REPORTS,
    ChartSeries,
    ChartSpec,
    ColumnMeta,
    PublicExportError,
    ReportResult,
    assert_public_export_allowed,
    fold_decisions,
    fold_drive_queue_status,
    fold_queue_outcomes,
    has_basis_note,
    has_cost_columns,
    public_csv_filename,
    public_html_filename,
    redact_report_for_public,
    result_to_csv,
    result_to_public_html,
)

PUBLIC_REPO = "acme-public"
PRIVATE_REPO = "shadowcorp-internal"
PRIVATE_TITLE = "TopSecretProjectZephyr rollout plan"
PRIVATE_ISSUE = 54321
PUBLIC_TITLE = "Fix the flaky retry loop"
PUBLIC_ISSUE = 101

BASIS_NOTE = (
    "Cost basis: `api_equivalent` (see column_meta `basis`) — `cost_usd` is "
    "what `claude -p` reports, an API-list-price equivalent, not necessarily "
    "money billed. Coverage: 2 leg(s) captured (100.0% of the $ shown), 0 "
    "estimated (0.0%), 0 unmeasured (contributes $0, 0% of the $ shown) of 2 "
    "leg(s) total."
)


def _columns() -> list[str]:
    return ["repo", "issue", "title", "cost_total", "machines"]


def _column_meta() -> list[ColumnMeta]:
    return [
        ColumnMeta(id="repo", label="Repo", kind="text"),
        ColumnMeta(id="issue", label="Issue", kind="int", align="right"),
        ColumnMeta(id="title", label="Title", kind="text"),
        ColumnMeta(
            id="cost_total", label="Cost $", kind="money", align="right",
            basis="api_equivalent",
        ),
        ColumnMeta(id="machines", label="Machines", kind="list"),
    ]


def _rows() -> list[dict]:
    return [
        {
            "repo": PUBLIC_REPO,
            "issue": PUBLIC_ISSUE,
            "title": PUBLIC_TITLE,
            "cost_total": 1.25,
            "machines": ["laptop"],
        },
        {
            "repo": PRIVATE_REPO,
            "issue": PRIVATE_ISSUE,
            "title": PRIVATE_TITLE,
            "cost_total": 3.75,
            "machines": ["desk"],
        },
    ]


def _notes() -> list[str]:
    return [
        BASIS_NOTE,
        f"{PRIVATE_REPO}#{PRIVATE_ISSUE}: 4 fix iterations in this window — "
        "the work is not converging on its own.",
    ]


def _fixture_result() -> ReportResult:
    return ReportResult(
        report_id="completed",
        generated_at=1_800_000_000.0,
        window=(1_799_000_000.0, 1_800_000_000.0),
        columns=_columns(),
        rows=_rows(),
        notes=_notes(),
        column_meta=_column_meta(),
    )


class TestRedactReportForPublic:
    def test_allowlisted_repo_passes_through_unchanged(self) -> None:
        redacted = redact_report_for_public(
            _fixture_result(), allowed_repos={PUBLIC_REPO}
        )
        public_rows = [r for r in redacted["rows"] if r["repo"] == PUBLIC_REPO]
        assert len(public_rows) == 1
        assert public_rows[0]["issue"] == PUBLIC_ISSUE
        assert public_rows[0]["title"] == PUBLIC_TITLE
        assert public_rows[0]["cost_total"] == 1.25

    def test_private_repo_collapses_to_one_aggregate_row(self) -> None:
        redacted = redact_report_for_public(
            _fixture_result(), allowed_repos={PUBLIC_REPO}
        )
        private_rows = [
            r for r in redacted["rows"] if r["repo"] == PUBLIC_PRIVATE_REPO_LABEL
        ]
        assert len(private_rows) == 1
        row = private_rows[0]
        assert row["issue"] is None
        assert row["title"] is None
        assert row["cost_total"] == 3.75
        assert row["machines"] == ["desk"]
        assert len(redacted["rows"]) == 2

    def test_private_identifiers_absent_from_every_row_and_note(self) -> None:
        redacted = redact_report_for_public(
            _fixture_result(), allowed_repos={PUBLIC_REPO}
        )
        blob = json.dumps(redacted)
        assert PRIVATE_REPO not in blob
        assert PRIVATE_TITLE not in blob
        assert str(PRIVATE_ISSUE) not in blob
        # the anomaly note naming the private repo is dropped...
        assert not any(PRIVATE_REPO in n for n in redacted["notes"])
        # ...but the basis/coverage note survives VERBATIM — it names no repo.
        assert BASIS_NOTE in redacted["notes"]

    def test_default_redacts_everything(self) -> None:
        """Empty allowlist (coordinator.yml's own default) redacts BOTH
        repos — there is no implicit allow."""
        redacted = redact_report_for_public(_fixture_result(), allowed_repos=())
        assert [r["repo"] for r in redacted["rows"]] == [PUBLIC_PRIVATE_REPO_LABEL]
        blob = json.dumps(redacted)
        assert PUBLIC_REPO not in blob
        assert PRIVATE_REPO not in blob

    def test_no_private_rows_means_no_aggregate_row_at_all(self) -> None:
        redacted = redact_report_for_public(
            _fixture_result(), allowed_repos={PUBLIC_REPO, PRIVATE_REPO}
        )
        assert all(r["repo"] != PUBLIC_PRIVATE_REPO_LABEL for r in redacted["rows"])

    def test_extra_keys_beyond_declared_columns_are_dropped(self) -> None:
        result = _fixture_result()
        result.rows[0]["leaked_internal_field"] = "should never appear"
        redacted = redact_report_for_public(result, allowed_repos={PUBLIC_REPO})
        assert "leaked_internal_field" not in json.dumps(redacted)


class TestPublicHtmlAndCsvExports:
    """Acceptance: every byte of the exported HTML and CSV."""

    def _redacted(self) -> dict:
        return redact_report_for_public(_fixture_result(), allowed_repos={PUBLIC_REPO})

    def test_html_hides_private_identifiers_and_keeps_public_ones(self) -> None:
        html = result_to_public_html(self._redacted())
        assert PRIVATE_REPO not in html
        assert PRIVATE_TITLE not in html
        assert str(PRIVATE_ISSUE) not in html
        assert PUBLIC_REPO in html
        assert PUBLIC_TITLE in html
        assert str(PUBLIC_ISSUE) in html
        assert PUBLIC_PRIVATE_REPO_LABEL in html

    def test_csv_hides_private_identifiers_and_keeps_public_ones(self) -> None:
        csv_text = result_to_csv(self._redacted())
        assert PRIVATE_REPO not in csv_text
        assert PRIVATE_TITLE not in csv_text
        assert str(PRIVATE_ISSUE) not in csv_text
        assert PUBLIC_REPO in csv_text
        assert PUBLIC_TITLE in csv_text
        assert str(PUBLIC_ISSUE) in csv_text

    def test_html_carries_the_basis_and_coverage_note_verbatim(self) -> None:
        html = result_to_public_html(self._redacted())
        assert "Cost basis: `api_equivalent`" in html
        assert "Coverage:" in html
        assert "2 leg(s) captured" in html

    def test_csv_carries_the_basis_and_coverage_note_verbatim(self) -> None:
        csv_text = result_to_csv(self._redacted())
        assert "Cost basis: `api_equivalent`" in csv_text
        assert "Coverage:" in csv_text

    def test_html_is_self_contained_html_document(self) -> None:
        html = result_to_public_html(self._redacted())
        assert html.startswith("<!DOCTYPE html>")
        assert "<table>" in html

    def test_filenames_carry_a_public_marker(self) -> None:
        redacted = self._redacted()
        assert public_html_filename(redacted).endswith(".public.html")
        assert public_csv_filename(redacted).endswith(".public.csv")


class TestCostBasisRefusal:
    def test_has_cost_columns_detects_a_stamped_basis_column(self) -> None:
        assert has_cost_columns(_fixture_result().to_dict()["column_meta"])

    def test_has_basis_note_detects_the_standard_note(self) -> None:
        assert has_basis_note([BASIS_NOTE])
        assert not has_basis_note(["some other note"])

    def test_assert_public_export_allowed_passes_when_basis_note_present(self) -> None:
        redacted = redact_report_for_public(_fixture_result(), allowed_repos={PUBLIC_REPO})
        assert_public_export_allowed(redacted, "completed")  # must not raise

    def test_assert_public_export_allowed_refuses_an_unstated_cost_figure(self) -> None:
        result = _fixture_result()
        result.notes = [n for n in result.notes if not n.startswith("Cost basis:")]
        redacted = redact_report_for_public(result, allowed_repos={PUBLIC_REPO})
        with pytest.raises(PublicExportError, match="no stated basis"):
            assert_public_export_allowed(redacted, "completed")

    def test_a_report_with_no_cost_columns_is_never_refused(self) -> None:
        result = _fixture_result()
        result.column_meta = [
            ColumnMeta(id=c.id, label=c.label, kind=c.kind, align=c.align)
            for c in result.column_meta
        ]  # strip every `basis`
        result.notes = []
        redacted = redact_report_for_public(result, allowed_repos={PUBLIC_REPO})
        assert_public_export_allowed(redacted, "completed")  # must not raise


class TestPublicReportingConfigParsing:
    def test_default_allowlist_is_empty(self) -> None:
        cfg = Config(repos=[], machines=[])
        assert cfg.reporting.public == PublicReportingConfig()
        assert cfg.reporting.public.allowlist_repos == ()

    def test_allowlist_repos_parses_from_the_reporting_block(self) -> None:
        cfg = _parse_reporting({"public": {"allowlist_repos": ["api", "web"]}})
        assert cfg.public.allowlist_repos == ("api", "web")

    def test_absent_public_block_defaults_to_empty_allowlist(self) -> None:
        cfg = _parse_reporting({"cost_basis": "billed"})
        assert cfg.public.allowlist_repos == ()

    def test_non_mapping_public_block_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="reporting.public"):
            _parse_reporting({"public": ["not", "a", "mapping"]})

    def test_non_string_list_allowlist_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="allowlist_repos"):
            _parse_reporting({"public": {"allowlist_repos": [1, 2]}})


class TestCliExport:
    def _patch_run_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import coord.state as state_mod

        monkeypatch.setattr(
            state_mod, "run_report", lambda report_id, params=None: _fixture_result().to_dict()
        )

    def _patch_config(self, monkeypatch: pytest.MonkeyPatch, allowlist: tuple[str, ...]) -> None:
        import coord.config as config_mod

        cfg = Config(repos=[], machines=[])
        cfg.reporting.public = PublicReportingConfig(allowlist_repos=allowlist)
        monkeypatch.setattr(config_mod, "load", lambda path: cfg)

    def test_export_without_public_flag_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(main, ["report", "export", "completed"])
        assert result.exit_code == 2
        assert "--public" in result.output

    def test_export_public_writes_redacted_html_and_csv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        self._patch_run_report(monkeypatch)
        self._patch_config(monkeypatch, (PUBLIC_REPO,))

        result = CliRunner().invoke(
            main,
            ["report", "export", "completed", "--public", "--out", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output

        html_files = list(tmp_path.glob("*.public.html"))
        csv_files = list(tmp_path.glob("*.public.csv"))
        assert len(html_files) == 1
        assert len(csv_files) == 1

        html_text = html_files[0].read_text()
        csv_text = csv_files[0].read_text()
        for blob in (html_text, csv_text):
            assert PRIVATE_REPO not in blob
            assert PRIVATE_TITLE not in blob
            assert str(PRIVATE_ISSUE) not in blob
            assert PUBLIC_REPO in blob
            assert "Cost basis:" in blob

    def test_export_public_refuses_an_unstated_cost_basis(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import coord.state as state_mod

        stripped = _fixture_result()
        stripped.notes = []
        monkeypatch.setattr(
            state_mod, "run_report", lambda report_id, params=None: stripped.to_dict()
        )
        self._patch_config(monkeypatch, (PUBLIC_REPO,))

        result = CliRunner().invoke(
            main,
            ["report", "export", "completed", "--public", "--out", str(tmp_path)],
        )
        assert result.exit_code == 2
        assert "no stated basis" in result.output
        assert not list(tmp_path.glob("*.public.*"))

    def test_export_public_with_unreadable_config_redacts_everything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import coord.config as config_mod

        self._patch_run_report(monkeypatch)

        def _boom(path):
            raise ConfigError("broken coordinator.yml")

        monkeypatch.setattr(config_mod, "load", _boom)

        result = CliRunner().invoke(
            main,
            ["report", "export", "completed", "--public", "--out", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "warning" in result.output
        html_files = list(tmp_path.glob("*.public.html"))
        assert len(html_files) == 1
        html_text = html_files[0].read_text()
        assert PUBLIC_REPO not in html_text
        assert PRIVATE_REPO not in html_text


def _dashboard_config(allowlist_repos: tuple[str, ...] = ()) -> Config:
    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(
            name="laptop", host="laptop.tailnet", repos=["api"],
            repo_paths={"api": "/tmp/api"},
        )],
    )
    cfg.reporting.public = PublicReportingConfig(allowlist_repos=allowlist_repos)
    return cfg


class TestDashboardPublicExportRoute:
    def _fixture_server(self) -> FixtureServer:
        return FixtureServer(
            report_catalogue_raw={
                "reports": [
                    {
                        "id": "fixture-public-demo",
                        "title": "Fixture Public Demo",
                        "description": "Seeded for the #3474 public-export route test.",
                        "params": [],
                        "row_identity": None,
                    }
                ]
            },
            report_results_raw={
                "fixture-public-demo": _fixture_result().to_dict(),
            },
        )

    def _client(self, allowlist_repos: tuple[str, ...]) -> TestClient:
        return TestClient(
            build_app(
                _dashboard_config(allowlist_repos), fixture=self._fixture_server(),
            )
        )

    def test_html_export_redacts_the_private_repo(self) -> None:
        client = self._client((PUBLIC_REPO,))
        r = client.get("/api/report/fixture-public-demo/public")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert PRIVATE_REPO not in r.text
        assert PRIVATE_TITLE not in r.text
        assert str(PRIVATE_ISSUE) not in r.text
        assert PUBLIC_REPO in r.text
        assert "Cost basis:" in r.text

    def test_csv_export_redacts_the_private_repo(self) -> None:
        client = self._client((PUBLIC_REPO,))
        r = client.get("/api/report/fixture-public-demo/public?format=csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert PRIVATE_REPO not in r.text
        assert PUBLIC_REPO in r.text

    def test_empty_allowlist_redacts_every_repo(self) -> None:
        client = self._client(())
        r = client.get("/api/report/fixture-public-demo/public")
        assert r.status_code == 200
        assert PUBLIC_REPO not in r.text
        assert PRIVATE_REPO not in r.text
        assert PUBLIC_PRIVATE_REPO_LABEL in r.text

    def test_unknown_format_is_a_400(self) -> None:
        client = self._client((PUBLIC_REPO,))
        r = client.get("/api/report/fixture-public-demo/public?format=xlsx")
        assert r.status_code == 400

    def test_unknown_report_id_is_a_404(self) -> None:
        client = self._client((PUBLIC_REPO,))
        r = client.get("/api/report/no-such-report/public")
        assert r.status_code == 404


class TestRealReportCatalogueRedaction:
    """PR #3541 review: run the ACTUAL report folds (not just the synthetic
    repo/issue/title/cost_total/machines fixture every other test in this
    file uses) through :func:`redact_report_for_public`, for every report
    the review named as leaking."""

    def test_queue_outcomes_has_no_row_identity_and_no_conventional_repo_column(self) -> None:
        # The bug was reachable BECAUSE this report declares none of the
        # three conventional identity columns `_public_row_identity_columns`
        # falls back to (no `repo`, no `issue`, no `title` column) — assert
        # that premise still holds so the next test keeps meaning what it says.
        report = REPORTS["queue-outcomes"]
        assert report.row_identity is None
        from coord.reports import QUEUE_OUTCOMES_COLUMNS

        assert "repo" not in QUEUE_OUTCOMES_COLUMNS
        assert "issue" not in QUEUE_OUTCOMES_COLUMNS
        assert "title" not in QUEUE_OUTCOMES_COLUMNS

    def test_queue_outcomes_issues_list_is_scrubbed_with_no_row_identity(self) -> None:
        public_key = entry_key(PUBLIC_REPO, PUBLIC_ISSUE)
        private_key = entry_key(PRIVATE_REPO, PRIVATE_ISSUE)
        start, end = 1_799_000_000.0, 1_800_000_000.0
        episodes = [
            {
                "key": public_key,
                "resolved": True,
                "resolved_at": start + 10,
                "human_acted": True,
                "source": "operator",
                "true_cause": "needs_decision",
                "stated_reason": "",
            },
            {
                "key": private_key,
                "resolved": True,
                "resolved_at": start + 20,
                "human_acted": True,
                "source": "operator",
                "true_cause": "needs_decision",
                "stated_reason": "",
            },
        ]
        result = fold_queue_outcomes(episodes, (start, end))
        assert result.report_id == "queue-outcomes"
        # Sanity: before redaction both keys are really there, unscrubbed.
        pre_issues = [set(r["issues"]) for r in result.rows]
        assert any(public_key in s and private_key in s for s in pre_issues)

        redacted = redact_report_for_public(
            result, allowed_repos={PUBLIC_REPO}, report=REPORTS["queue-outcomes"]
        )
        blob = json.dumps(redacted)
        assert PRIVATE_REPO not in blob
        assert str(PRIVATE_ISSUE) not in blob
        assert public_key in blob
        issues = next(r["issues"] for r in redacted["rows"] if public_key in r["issues"])
        assert private_key not in issues
        assert PUBLIC_PRIVATE_REPO_LABEL in issues

    def test_decisions_downstream_list_is_scrubbed_for_a_kept_root_row(self) -> None:
        from coord.drive_queue import STATE_BLOCKED

        public_key = entry_key(PUBLIC_REPO, PUBLIC_ISSUE)
        private_key = entry_key(PRIVATE_REPO, PRIVATE_ISSUE)
        generated_at = 1_800_000_000.0
        escalations = [
            {
                "repo_name": PUBLIC_REPO,
                "issue_number": PUBLIC_ISSUE,
                "reason": "a gate diverged",
                "proposed_command": "coord merge --revalidate",
                "created_at": generated_at,
                "stage": "merge",
            }
        ]
        queue_entries = [
            {
                "repo_name": PRIVATE_REPO,
                "issue_number": PRIVATE_ISSUE,
                "state": STATE_BLOCKED,
                "last_reason": f"queued but blocked — {public_key} it will never satisfy",
                "after_json": [public_key],
                "reason_at": generated_at,
                "enqueued_at": generated_at,
            }
        ]
        result = fold_decisions(escalations, queue_entries, generated_at)
        assert result.report_id == "decisions"
        root = next(r for r in result.rows if r["repo"] == PUBLIC_REPO)
        assert private_key in root["downstream"]  # unredacted: the leak, pre-fix

        redacted = redact_report_for_public(
            result, allowed_repos={PUBLIC_REPO}, report=REPORTS["decisions"]
        )
        blob = json.dumps(redacted)
        assert PRIVATE_REPO not in blob
        assert str(PRIVATE_ISSUE) not in blob
        kept = [r for r in redacted["rows"] if r["repo"] == PUBLIC_REPO]
        assert len(kept) == 1
        assert private_key not in kept[0]["downstream"]
        assert PUBLIC_PRIVATE_REPO_LABEL in kept[0]["downstream"]

    def test_drive_queue_status_after_list_is_scrubbed_for_a_kept_row(self) -> None:
        private_key = entry_key(PRIVATE_REPO, PRIVATE_ISSUE)
        entries = [
            {
                "position": 1,
                "repo_name": PUBLIC_REPO,
                "issue_number": PUBLIC_ISSUE,
                "state": "waiting",
                "machine": "laptop",
                "attempts": 0,
                "deferrals": 0,
                "last_reason": "",
                "after_json": [private_key],
            }
        ]
        result = fold_drive_queue_status(entries, 1_800_000_000.0)
        assert result.report_id == "drive-queue-status"
        row = next(r for r in result.rows if r["repo"] == PUBLIC_REPO)
        assert private_key in row["after"]  # unredacted: the leak, pre-fix

        redacted = redact_report_for_public(
            result, allowed_repos={PUBLIC_REPO}, report=REPORTS["drive-queue-status"]
        )
        blob = json.dumps(redacted)
        assert PRIVATE_REPO not in blob
        assert str(PRIVATE_ISSUE) not in blob
        kept = [r for r in redacted["rows"] if r["repo"] == PUBLIC_REPO]
        assert len(kept) == 1
        assert private_key not in kept[0]["after"]
        assert PUBLIC_PRIVATE_REPO_LABEL in kept[0]["after"]

    def test_list_columns_that_are_not_repo_issue_shaped_are_left_alone(self) -> None:
        # `machines`/`options` are also `kind: list` columns — the scrub must
        # only touch elements that actually PARSE as `repo#issue`, never a
        # plain machine name or an options dict (#3474 review: "never
        # disturbs a column that merely happens to also be kind: list").
        redacted = redact_report_for_public(
            _fixture_result(), allowed_repos={PUBLIC_REPO}
        )
        public_row = next(r for r in redacted["rows"] if r["repo"] == PUBLIC_REPO)
        assert public_row["machines"] == ["laptop"]


class TestPublicHtmlScriptInjection:
    """PR #3541 review: an unescaped `</script>` inside the inlined chart
    JSON must not be able to break out of the `<script>` block."""

    def _chart_result(self, title: str) -> ReportResult:
        return ReportResult(
            report_id="completed",
            generated_at=1_800_000_000.0,
            window=(1_799_000_000.0, 1_800_000_000.0),
            columns=["repo", "issue", "title", "cost_total"],
            column_meta=[
                ColumnMeta(id="repo", label="Repo", kind="text"),
                ColumnMeta(id="issue", label="Issue", kind="int", align="right"),
                ColumnMeta(id="title", label="Title", kind="text"),
                ColumnMeta(id="cost_total", label="Cost $", kind="money", align="right"),
            ],
            rows=[{"repo": PUBLIC_REPO, "issue": PUBLIC_ISSUE, "title": title, "cost_total": 1.0}],
            notes=[],
            chart=ChartSpec(
                kind="bar",
                series=(ChartSeries(label="Cost", column="cost_total"),),
                x="title",
            ),
        )

    def test_a_title_containing_a_script_breakout_cannot_inject_a_new_script_tag(
        self,
    ) -> None:
        malicious = "</script><script>window.__pwned = true;</script>"
        html = result_to_public_html(self._chart_result(malicious))
        # Exactly the two `<script` openers THIS function itself emits (the
        # pinned ECharts CDN tag, and the inline chart-data block) — a third
        # would mean the malicious title broke out of the JSON payload and
        # opened a script tag of its own.
        assert html.count("<script") == 2
        assert "</script><script>" not in html

    def test_the_escaped_payload_still_round_trips_as_the_original_title(self) -> None:
        # The escape must be reversible by the browser's JSON.parse/eval —
        # not a lossy sanitiser that mangles a legitimate title.
        import re

        malicious = "a </script> title"
        html = result_to_public_html(self._chart_result(malicious))
        match = re.search(r"const REPORT = (\{.*\});\n", html)
        assert match is not None
        payload = json.loads(match.group(1))
        assert payload["result"]["rows"][0]["title"] == malicious
