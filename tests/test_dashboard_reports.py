"""Tests for the generic `/reports` page (#3473).

This page is a build-free static document (``coord/dashboard/reports.html``)
whose only data source is ``GET /api/report`` + ``GET /api/report/{id}`` —
the exact routes already pinned by ``TestReportAPI`` in ``tests/test_dashboard.py``.
Since nothing here executes the shipped JavaScript (no browser; the `browser`
capability probe is UNMET, #1678), the bar mirrors the repo's existing
precedent for this class of page (``TestIndexPage`` in ``tests/test_dashboard.py``
only pins the legacy dashboard's static markup, never simulated DOM state):

* the page renders at all, with the generic (never per-report) containers a
  client needs: a report picker, a table, and a chart container;
* every ``fetch(...)`` call *shipped in the page's own script* targets
  ``/api/report`` or ``/api/report/{id}`` and nothing else — a static-analysis
  check on the exact bytes served, so it can never silently drift onto some
  other endpoint;
* a report whose result carries a :class:`~coord.reports.ChartSpec` serves,
  through the very endpoint the page's `fetchReport()` calls, both a chart
  declaration and the table data it is drawn from — proving the data path
  the chart container depends on actually works end to end.
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from coord.config import Config
from coord.dashboard.fixture import FixtureServer
from coord.dashboard.server import build_app
from coord.models import Machine, Repo
from coord.reports import ChartSpec, ChartSeries, ColumnMeta, ReportResult

REPORTS_HTML_PATH = (
    __import__("pathlib").Path(__file__).parent.parent
    / "coord" / "dashboard" / "reports.html"
)


@pytest.fixture(autouse=True)
def _no_spa_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the legacy dashboard/`/reports` path — isolates from whatever
    `~/coord-web-dist` happens to be on the machine running the suite, same
    reasoning as the identically-named fixture in tests/test_dashboard.py."""
    from pathlib import Path

    monkeypatch.setattr(
        "coord.dashboard.server.WEBAPP_DIST",
        Path("/nonexistent/dist"),
    )


def _config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(
            name="laptop", host="laptop.tailnet", repos=["api"],
            repo_paths={"api": "/tmp/api"},
        )],
    )


def _client(fixture: FixtureServer | None = None) -> TestClient:
    return TestClient(build_app(_config(), fixture=fixture))


def _chart_fixture_result() -> ReportResult:
    return ReportResult(
        report_id="fixture-chart-demo",
        generated_at=1700000000.0,
        window=(1699900000.0, 1700000000.0),
        columns=["bucket", "count"],
        rows=[
            {"bucket": "succeeded", "count": 5},
            {"bucket": "failed", "count": 2},
        ],
        notes=["demo note"],
        column_meta=[
            ColumnMeta(id="bucket", label="Bucket", kind="text"),
            ColumnMeta(id="count", label="Count", kind="int", align="right"),
        ],
        totals={"count": 7},
        chart=ChartSpec(
            kind="bar",
            series=(ChartSeries(label="Entries", column="count"),),
            x="bucket",
            group_by="bucket",
            stacked=True,
            title="Outcomes by bucket",
            y_label="Entries",
        ),
    )


def _chart_fixture_catalogue() -> dict:
    return {
        "reports": [
            {
                "id": "fixture-chart-demo",
                "title": "Fixture Chart Demo",
                "description": "A seeded report used only by this test.",
                "params": [],
                "row_identity": None,
            }
        ]
    }


class TestReportsPageRenders:
    def test_serves_html_with_generic_containers(self) -> None:
        client = _client()
        r = client.get("/reports")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        body = r.text
        assert "coord reports" in body
        # Generic containers — present regardless of which report is
        # selected, never named after one specific report id.
        assert 'id="report-select"' in body
        assert 'id="report-params"' in body
        assert 'id="report-table"' in body
        assert 'id="report-chart"' in body
        assert 'id="export-csv"' in body
        assert 'id="export-xlsx"' in body
        assert 'id="export-ndjson"' in body
        assert 'id="export-md"' in body
        assert 'id="export-png"' in body
        assert 'id="export-svg"' in body

    def test_loads_echarts_from_a_pinned_cdn_url(self) -> None:
        client = _client()
        body = client.get("/reports").text
        # Pinned (an exact version in the URL), not "latest" — a silent
        # upstream bump must never change what ships to users.
        match = re.search(
            r'src="https://[^"]*echarts@([0-9]+\.[0-9]+\.[0-9]+)[^"]*"', body
        )
        assert match, "expected a pinned ECharts <script src> tag"

    def test_no_react_build_artifacts_reintroduced(self) -> None:
        """#2009 removed the React webapp; this page must not bring it back
        — checked by the technical markers a Vite/React build actually
        leaves (a module-script bundle, a hydration root, devtools hooks),
        not the English word "react" (which this very file's own prose
        legitimately uses to explain why it must not reappear)."""
        body = _client().get("/reports").text
        assert "__REACT_DEVTOOLS" not in body
        assert 'id="root"' not in body
        assert '<script type="module"' not in body
        assert "/assets/index-" not in body


class TestReportsPageDataCalls:
    """The page's data calls must hit only /api/report* (issue acceptance).

    Scoped to the shipped <script> *code*, not the whole document — the
    page's own prose (including this module's docstrings) legitimately
    talks about "fetch()" without that being a call site.
    """

    def _script_body(self) -> str:
        html = REPORTS_HTML_PATH.read_text()
        # The inline script is the LAST <script> tag (the CDN <script src=...>
        # tag for ECharts has no body); everything before it is markup/CSS.
        script = html.rsplit("<script>", 1)[1].rsplit("</script>", 1)[0]
        # Strip `//` line comments first — the page's own comments are free
        # to mention "fetch(...)" in prose (as several do, to point future
        # editors at this very test) without that counting as a call site.
        return re.sub(r"//[^\n]*", "", script)

    def _fetch_call_expressions(self) -> list[str]:
        script = self._script_body()
        return re.findall(r"fetch\(\s*([^)]*?)\s*\)", script)

    def test_every_literal_fetch_target_is_api_report(self) -> None:
        script = self._script_body()
        literals = re.findall(r"fetch\(\s*'([^']*)'", script)
        assert literals, "expected at least one literal fetch(...) call"
        for target in literals:
            assert target.startswith("/api/report"), (
                f"reports.html fetches {target!r}, which is not under /api/report"
            )

    def test_url_builder_for_report_run_is_rooted_at_api_report(self) -> None:
        script = self._script_body()
        assert "'/api/report/' + encodeURIComponent(reportId)" in script

    def test_no_other_api_prefix_is_ever_fetched(self) -> None:
        calls = self._fetch_call_expressions()
        assert calls, "expected at least one fetch(...) call in the page's script"
        for call in calls:
            # Every fetch() call expression in this page is either a bare
            # '/api/report...' literal, or `url` — the local variable built
            # exclusively from such a literal (asserted above). No other
            # shape is permitted.
            assert call == "url" or call.startswith("'/api/report"), (
                f"unexpected fetch() call expression: {call!r}"
            )


class TestReportsPageChartFixture:
    """A fixture report with a ChartSpec renders a chart container and its
    table (issue acceptance) — proven end-to-end through the same endpoint
    the page's own `fetchReport()` hits."""

    def _fixture(self) -> FixtureServer:
        return FixtureServer(
            report_catalogue_raw=_chart_fixture_catalogue(),
            report_results_raw={
                "fixture-chart-demo": _chart_fixture_result().to_dict(),
            },
        )

    def test_page_still_renders_chart_and_table_containers_in_fixture_mode(self) -> None:
        client = _client(self._fixture())
        body = client.get("/reports").text
        assert 'id="report-chart"' in body
        assert 'id="report-table"' in body

    def test_catalogue_includes_the_seeded_report(self) -> None:
        client = _client(self._fixture())
        r = client.get("/api/report")
        assert r.status_code == 200
        ids = [rep["id"] for rep in r.json()["reports"]]
        assert ids == ["fixture-chart-demo"]

    def test_report_run_serves_both_chart_spec_and_table_rows(self) -> None:
        """This is exactly what the page's `fetchReport()` + `renderChart()`/
        `renderTable()` consume: `chart` for the ECharts container, `columns`/
        `rows`/`column_meta`/`totals` for the table — both present on one
        response, from the one source of truth."""
        client = _client(self._fixture())
        r = client.get("/api/report/fixture-chart-demo")
        assert r.status_code == 200
        body = r.json()

        # Chart half — enough for buildChartOption() in reports.html to draw
        # a bar chart with the "one series per group" pivot shape.
        assert body["chart"]["kind"] == "bar"
        assert body["chart"]["x"] == "bucket"
        assert body["chart"]["group_by"] == "bucket"
        assert [s["column"] for s in body["chart"]["series"]] == ["count"]

        # Table half — same rows the chart was derived from (#2271: one
        # source of truth, never a second copy of the numbers).
        assert body["columns"] == ["bucket", "count"]
        assert {row["bucket"] for row in body["rows"]} == {"succeeded", "failed"}
        assert body["totals"] == {"count": 7}
        assert [m["id"] for m in body["column_meta"]] == body["columns"]

    def test_export_format_still_works_for_a_chart_bearing_report(self) -> None:
        """CSV export (#3472) is driven by columns/rows/totals only — a
        report that also declares a chart must export identically."""
        client = _client(self._fixture())
        r = client.get(
            "/api/report/fixture-chart-demo", params={"format": "csv"}
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "Bucket,Count" in r.text
        assert "succeeded,5" in r.text and "failed,2" in r.text


class TestReportsPageDeepLinking:
    def test_page_reads_report_and_params_from_the_url(self) -> None:
        body = REPORTS_HTML_PATH.read_text()
        assert "window.location.search" in body
        assert "history.replaceState" in body

    def test_deep_link_does_not_change_response_status(self) -> None:
        # The query string is consumed client-side (JS, which this harness
        # never executes) — the server must simply keep serving the same
        # static page regardless of what is in it, never 404/redirect.
        client = _client()
        r = client.get("/reports", params={"report": "issue-activity", "since": "7d"})
        assert r.status_code == 200
        assert 'id="report-select"' in r.text
