"""Tests for coord/acceptance_drivers.py — the tui-tuidriver, cli-pytest
(#1125), and web-playwright (#1539) adapters (#944).

Covers ``parse_test_output``'s two accepted shapes (a single JSON blob, and
libtest's `--format json` JSON-lines event stream), ``parse_pytest_report_log``
(pytest's built-in ``--report-log`` JSON-lines shape), ``parse_playwright_json_report``
(Playwright Test's built-in ``--reporter=json`` shape), ``render_run_command``'s
``{ms}`` templating, and ``run_driver``'s unsupported-kind guard + real
subprocess path for all three kinds.

Fixtures under tests/fixtures/playwright/ are CAPTURED REAL OUTPUT (Playwright
Test 1.61.1, Node 24.8.0), not hand-written strings shaped to look like a
Playwright report — each was produced by running ``npx playwright test``
against a small scratch project (``npm init -y && npm install
@playwright/test@1.61.1``) with this ``playwright.config.ts``:

    export default defineConfig({
      testDir: './tests',
      fullyParallel: false,
      workers: 1,
      retries: 1,
      reporter: [['list'], ['json', {outputFile: 'report.json'}],
                 ['junit', {outputFile: 'report.xml'}]],
      projects: [{name: 'chromium'}, {name: 'firefox'}],
    })

None of the scratch spec files reference Playwright's ``page`` fixture, so
none of these runs needed a real browser binary installed — Playwright's
fixtures are lazy and only launch a browser when a test actually asks for
one. How each fixture was produced:

- all_pass.json: `npx playwright test tests/all_pass.spec.ts` — a
  `describe` block with 2 passing tests, run under both `chromium` and
  `firefox` projects (4 total results) — covers multiple `projects:`.
- mixed_fail.json: `npx playwright test tests/mixed.spec.ts --project=chromium`
  — 1 pass + 1 genuine `expect(1+1).toBe(3)` failure, retried once per
  config (both attempts fail) — the failure message carries Playwright's
  baked-in ANSI color codes verbatim (present even though stdout was piped,
  not a tty — confirmed `NO_COLOR=1`/`FORCE_COLOR=0` do not suppress them
  for this formatter).
- skip.json: `npx playwright test tests/skip.spec.ts --project=chromium` —
  1 pass, 1 bare `test.skip('reason', ...)`-style static skip, 1
  `test.fixme(true, 'blocked on #1541 browser capability')`.
- retry_then_pass.json / retry_then_pass.junit.xml: `npx playwright test
  tests/retry.spec.ts --project=chromium` — a test that throws when
  `testInfo.retry === 0` and passes otherwise, captured from the SAME run
  with both the `json` and `junit` reporters active simultaneously (see
  TestParsePlaywrightJsonReport.test_junit_sibling_loses_the_flake_signal
  for why json was chosen over junit — this pair is the evidence).
- global_setup_crash.json: a `globalSetup` hook that
  `throw new Error(...)`, run against `all_pass.spec.ts`. Playwright still
  writes a well-formed report — `"suites": []` and a non-empty top-level
  `"errors"` — exit code 1. The same shape shows up for a `--grep` that
  matches no tests without `--pass-with-no-tests`.
- truncated.json: `head -c 4000 mixed_fail.json` — a real report file cut
  off mid-write, the shape a killed/OOM-killed process leaves behind.

Regenerate by re-running the commands above against an equivalent scratch
project; nothing here depends on a live worktree or a real browser install.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coord.acceptance import build_verdict
from coord.acceptance_drivers import (
    DriverError,
    FIXTURE_SERVER_DEPENDENT_KINDS,
    SUPPORTED_KINDS,
    parse_playwright_json_report,
    parse_pytest_junit_xml,
    parse_test_output,
    render_run_command,
    run_driver,
)

FIXTURES = Path(__file__).parent / "fixtures" / "playwright"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestParseTestOutputBlob:
    def test_single_json_blob(self) -> None:
        output = json.dumps({
            "tests": [
                {"id": "ms01::shows_menu", "status": "pass"},
                {"id": "ms01::selects_item", "status": "fail", "message": "expected A got B"},
            ]
        })
        tests = parse_test_output(output)
        assert tests == [
            {"id": "ms01::shows_menu", "status": "pass", "message": ""},
            {"id": "ms01::selects_item", "status": "fail", "message": "expected A got B"},
        ]

    def test_blob_ignores_malformed_entries(self) -> None:
        output = json.dumps({"tests": [{"id": "ok"}, {"status": "fail"}, "not-a-dict"]})
        assert parse_test_output(output) == []

    def test_blob_without_tests_key_falls_through_to_lines(self) -> None:
        # A single-object blob with no "tests" key isn't a match for shape 1;
        # since it also isn't a valid libtest line-stream, nothing parses.
        assert parse_test_output(json.dumps({"other": 1})) == []


class TestParseTestOutputLibtestJsonLines:
    def test_ok_and_failed_events(self) -> None:
        lines = [
            json.dumps({"type": "suite", "event": "started", "test_count": 2}),
            json.dumps({"type": "test", "event": "started", "name": "ms01::a"}),
            json.dumps({"type": "test", "name": "ms01::a", "event": "ok"}),
            json.dumps({
                "type": "test", "name": "ms01::b", "event": "failed",
                "stdout": "assertion failed: expected 3 got 4",
            }),
            json.dumps({"type": "suite", "event": "failed"}),
        ]
        tests = parse_test_output("\n".join(lines))
        assert tests == [
            {"id": "ms01::a", "status": "pass", "message": ""},
            {
                "id": "ms01::b", "status": "fail",
                "message": "assertion failed: expected 3 got 4",
            },
        ]

    def test_ignored_event_maps_to_skip(self) -> None:
        line = json.dumps({"type": "test", "name": "ms01::c", "event": "ignored"})
        assert parse_test_output(line) == [{"id": "ms01::c", "status": "skip", "message": ""}]

    def test_non_json_noise_lines_skipped(self) -> None:
        lines = [
            "   Compiling coord-tui v0.1.0",
            "warning: unused variable",
            json.dumps({"type": "test", "name": "ms01::a", "event": "ok"}),
            "",
        ]
        tests = parse_test_output("\n".join(lines))
        assert tests == [{"id": "ms01::a", "status": "pass", "message": ""}]

    def test_empty_output_returns_empty(self) -> None:
        assert parse_test_output("") == []
        assert parse_test_output(None) == []  # type: ignore[arg-type]


class TestRunDriver:
    def test_unsupported_kind_raises(self) -> None:
        # "native" is the one kind ORACLE_LOOP.md documents as declarable in
        # coordinator.yml but not yet implemented — see docs/WEB_CONTROL_CENTER.md
        # M-W0. web-playwright landed in #1539 and must NOT raise here anymore
        # (see TestRunDriverWebPlaywright.test_web_playwright_no_longer_raises_not_implemented).
        with pytest.raises(DriverError, match="not implemented yet"):
            run_driver("native", "some-native-runner", cwd=".")

    def test_not_implemented_message_no_longer_lists_web_playwright_as_pending(self) -> None:
        # #1539 acceptance criterion: the "not implemented" message itself
        # must stop describing web-playwright as pending — it's fine (and
        # correct) for the "(supported: ...)" clause to name it now that
        # it's a real, working kind, so only the sentence BEFORE that
        # clause (the "is not implemented yet ... lands in a later issue"
        # part) is checked here.
        with pytest.raises(DriverError) as exc_info:
            run_driver("native", "some-native-runner", cwd=".")
        message = str(exc_info.value)
        pending_clause = message.split("(supported:")[0]
        assert "web-playwright" not in pending_clause

    def test_supported_kinds_tuple_has_tui_tuidriver(self) -> None:
        assert "tui-tuidriver" in SUPPORTED_KINDS

    def test_runs_shell_command_and_parses_stdout(self, tmp_path) -> None:
        blob = json.dumps({"tests": [{"id": "a", "status": "pass"}]})
        result = run_driver("tui-tuidriver", f"echo '{blob}'", cwd=str(tmp_path))
        assert result.exit_code == 0
        assert result.ok is True
        assert result.tests == [{"id": "a", "status": "pass", "message": ""}]

    def test_nonzero_exit_still_returns_partial_parse(self, tmp_path) -> None:
        blob = json.dumps({"tests": [{"id": "a", "status": "pass"}]})
        result = run_driver(
            "tui-tuidriver", f"echo '{blob}'; exit 1", cwd=str(tmp_path),
        )
        assert result.exit_code == 1
        assert result.ok is False
        assert result.tests == [{"id": "a", "status": "pass", "message": ""}]

    def test_timeout_raises_driver_error(self, tmp_path) -> None:
        with pytest.raises(DriverError, match="timed out"):
            run_driver("tui-tuidriver", "sleep 5", cwd=str(tmp_path), timeout=1)


class TestFixtureServerDependentKinds:
    """#2748 (IL-2): `coord repo doctor`'s oracle-readiness layer reads this
    set to report the #1538 gap explicitly instead of a `web-playwright`
    repo silently reading as fully oracle-ready once a driver is declared."""

    def test_web_playwright_is_fixture_server_dependent(self) -> None:
        assert "web-playwright" in FIXTURE_SERVER_DEPENDENT_KINDS

    def test_only_kinds_this_module_actually_supports_are_listed(self) -> None:
        # A kind declared here but not in SUPPORTED_KINDS would be an
        # unreachable warning — repo_onboard would flag a dependency for a
        # driver `run_driver` itself refuses to execute.
        assert FIXTURE_SERVER_DEPENDENT_KINDS <= set(SUPPORTED_KINDS)

    def test_deterministic_kinds_are_not_flagged(self) -> None:
        # tui-tuidriver and cli-pytest run against a real local checkout —
        # no external fixture dependency to flag.
        assert "tui-tuidriver" not in FIXTURE_SERVER_DEPENDENT_KINDS
        assert "cli-pytest" not in FIXTURE_SERVER_DEPENDENT_KINDS


class TestRunDriverSetup:
    """#1733: the `setup:` provisioning step, run once before `run` — the
    fix for `coord acceptance record`'s throwaway worktree having no
    `node_modules` for a JS driver (web-playwright's `run` failed with a
    bare `exit 127` there, before this existed)."""

    def test_no_setup_command_is_unchanged_behavior(self, tmp_path) -> None:
        blob = json.dumps({"tests": [{"id": "a", "status": "pass"}]})
        result = run_driver(
            "tui-tuidriver", f"echo '{blob}'", cwd=str(tmp_path), setup_command="",
        )
        assert result.exit_code == 0
        assert result.tests == [{"id": "a", "status": "pass", "message": ""}]

    def test_setup_runs_before_run_command(self, tmp_path) -> None:
        # `setup` writes a marker file; `run` only succeeds (prints a
        # passing verdict) if that marker exists yet — proves ordering, not
        # just that both commands happened to run somehow.
        marker = tmp_path / "provisioned"
        blob = json.dumps({"tests": [{"id": "a", "status": "pass"}]})
        run_command = f"test -f {marker} && echo '{blob}' || (echo 'MISSING MARKER' && exit 1)"
        result = run_driver(
            "tui-tuidriver", run_command, cwd=str(tmp_path),
            setup_command=f"touch {marker}",
        )
        assert result.exit_code == 0
        assert result.tests == [{"id": "a", "status": "pass", "message": ""}]

    def test_setup_failure_raises_distinct_provisioning_error(self, tmp_path) -> None:
        marker = tmp_path / "should-not-exist"
        with pytest.raises(DriverError, match="provisioning failed") as exc_info:
            run_driver(
                "web-playwright", f"touch {marker}; exit 0", cwd=str(tmp_path),
                setup_command="echo 'npm ci boom' 1>&2; exit 1",
            )
        message = str(exc_info.value)
        assert "npm ci boom" in message
        # `run` must never have executed — a driver whose dependencies
        # never installed cannot produce a meaningful verdict.
        assert not marker.exists()

    def test_setup_failure_message_is_not_mistaken_for_a_test_failure(self, tmp_path) -> None:
        with pytest.raises(DriverError) as exc_info:
            run_driver(
                "web-playwright", "exit 0", cwd=str(tmp_path),
                setup_command="exit 1",
            )
        message = str(exc_info.value)
        assert "wrote no report" not in message
        assert "provisioning failed" in message

    def test_setup_timeout_raises_driver_error(self, tmp_path) -> None:
        with pytest.raises(DriverError, match="provisioning timed out"):
            run_driver(
                "tui-tuidriver", "exit 0", cwd=str(tmp_path),
                setup_command="sleep 5", timeout=1,
            )


class TestRenderRunCommand:
    def test_no_ms_leaves_template_unsubstituted(self) -> None:
        assert (
            render_run_command("pytest tests/acceptance/{ms}")
            == "pytest tests/acceptance/{ms}"
        )

    def test_substitutes_ms(self) -> None:
        assert (
            render_run_command("pytest tests/acceptance/{ms}", ms="ms-37")
            == "pytest tests/acceptance/ms-37"
        )

    def test_command_without_template_is_a_noop(self) -> None:
        assert render_run_command("cargo test", ms="ms-37") == "cargo test"


# A real `--junit-xml` report (pytest 9.1, trimmed) for reference — the shape
# TestParsePytestJunitXml's fixtures below are modeled on:
#
#   <?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">
#   <testsuite name="pytest" errors="0" failures="2" skipped="1" tests="4" ...>
#   <testcase classname="test_sample" name="test_pass" time="0.000" />
#   <testcase classname="test_sample" name="test_fail" time="0.001">
#   <failure message="AssertionError: assert 'got-value' == 'expected-value'&#10; ...">
#   ...</failure></testcase>
#   <testcase classname="test_sample" name="test_skip" time="0.000">
#   <skipped type="pytest.skip" message="nope">...</skipped></testcase>
#   <testcase classname="test_sample" name="test_error" time="0.000">
#   <failure message="RuntimeError: boom">...</failure></testcase>
#   </testsuite></testsuites>


class TestParsePytestJunitXml:
    XML = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests">'
        '<testsuite name="pytest" errors="0" failures="2" skipped="1" tests="4">'
        '<testcase classname="test_sample" name="test_pass" time="0.000" />'
        '<testcase classname="test_sample" name="test_fail" time="0.001">'
        # Real pytest junit-xml escapes embedded newlines in the "message"
        # attribute as `&#10;` character references — a literal raw newline
        # byte there would be collapsed to a space by XML attribute-value
        # normalization (XML 1.0 §3.3.3), which is exactly why pytest itself
        # emits `&#10;` rather than a raw newline.
        "<failure message=\"AssertionError: assert 'got-value' == 'expected-value'"
        "&#10;  &#10;  - expected-value&#10;  + got-value\">body text here</failure>"
        "</testcase>"
        '<testcase classname="test_sample" name="test_skip" time="0.000">'
        '<skipped type="pytest.skip" message="nope">skip body</skipped>'
        "</testcase>"
        '<testcase classname="test_sample" name="test_error" time="0.000">'
        '<failure message="RuntimeError: boom">error body</failure>'
        "</testcase>"
        "</testsuite></testsuites>"
    )

    def test_pass_fail_skip_and_error_all_parsed(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        by_id = {t["id"]: t for t in tests}
        assert set(by_id) == {
            "test_sample::test_pass",
            "test_sample::test_fail",
            "test_sample::test_skip",
            "test_sample::test_error",
        }
        assert by_id["test_sample::test_pass"]["status"] == "pass"
        assert by_id["test_sample::test_skip"]["status"] == "skip"
        assert by_id["test_sample::test_error"]["status"] == "fail"

    def test_assert_eq_failure_surfaces_expected_and_got(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        fail = next(t for t in tests if t["id"] == "test_sample::test_fail")
        assert fail["status"] == "fail"
        assert fail["got"] == "'got-value'"
        assert fail["expected"] == "'expected-value'"

    def test_non_assert_failure_leaves_expected_got_empty(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        error = next(t for t in tests if t["id"] == "test_sample::test_error")
        assert error["message"] == "RuntimeError: boom"
        assert error["expected"] == ""
        assert error["got"] == ""

    def test_skip_message_is_the_skip_reason(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        skip = next(t for t in tests if t["id"] == "test_sample::test_skip")
        assert skip["message"] == "nope"

    def test_pass_has_empty_message(self) -> None:
        tests = parse_pytest_junit_xml(self.XML)
        passed = next(t for t in tests if t["id"] == "test_sample::test_pass")
        assert passed == {
            "id": "test_sample::test_pass", "status": "pass", "message": "",
            "expected": "", "got": "",
        }

    def test_error_tag_treated_same_as_failure(self) -> None:
        xml = (
            '<testsuites><testsuite name="pytest">'
            '<testcase classname="t" name="test_fixture_broke">'
            '<error message="assert 1 == 2">boom</error>'
            "</testcase></testsuite></testsuites>"
        )
        tests = parse_pytest_junit_xml(xml)
        assert tests == [{
            "id": "t::test_fixture_broke", "status": "fail",
            "message": "assert 1 == 2", "expected": "2", "got": "1",
        }]

    def test_no_classname_uses_bare_name(self) -> None:
        xml = (
            '<testsuites><testsuite name="pytest">'
            '<testcase name="test_bare" />'
            "</testsuite></testsuites>"
        )
        assert parse_pytest_junit_xml(xml) == [
            {"id": "test_bare", "status": "pass", "message": "", "expected": "", "got": ""}
        ]

    def test_testcase_without_name_skipped(self) -> None:
        xml = (
            '<testsuites><testsuite name="pytest">'
            '<testcase classname="t" />'
            "</testsuite></testsuites>"
        )
        assert parse_pytest_junit_xml(xml) == []

    def test_empty_input_returns_empty(self) -> None:
        assert parse_pytest_junit_xml("") == []
        assert parse_pytest_junit_xml(None) == []  # type: ignore[arg-type]

    def test_malformed_xml_returns_empty(self) -> None:
        assert parse_pytest_junit_xml("<not valid xml") == []


class TestRunDriverCliPytest:
    def test_supported_kinds_tuple_has_cli_pytest(self) -> None:
        assert "cli-pytest" in SUPPORTED_KINDS

    def test_runs_real_pytest_and_parses_junit_xml(self, tmp_path) -> None:
        """#2170: the inner pytest's rootdir is PINNED to ``tmp_path``.

        The ids asserted below are derived from the JUnit XML ``classname``,
        which pytest computes from the test's nodeid *relative to rootdir*,
        with ``/`` → ``.`` (``_pytest.junitxml.mangle_test_address``). rootdir
        is inferred by walking **upward** from the arg for ``pytest.ini`` /
        ``pyproject.toml`` / ``tox.ini`` / ``setup.cfg`` / ``setup.py`` -- so on
        a machine where any ancestor of ``$TMPDIR`` holds one of those, rootdir
        lands above ``tmp_path`` and the classname grows a directory prefix:
        ``inner.test_sample::test_pass`` instead of ``test_sample::test_pass``,
        and this test dies with a ``KeyError``. Reproduced exactly that way,
        and it is why this failed on `precision` but never in CI.

        ``-c pytest.ini`` and ``--rootdir`` together make it environment-
        independent: ``-c`` pins which ini file is loaded (so a stray ancestor
        ``addopts`` -- ``--cov``, ``-n auto`` -- cannot leak into a run whose
        exit code we assert), and ``--rootdir`` pins the path the nodeids are
        relative to. Either alone fixes the observed KeyError; both together
        also stop the *next* ambient-config surprise.

        (The sibling ``{ms}`` test below asserts with ``endswith`` and so was
        already immune -- which is the tell that the exact-id assertion here
        was the environment-dependent one.)
        """
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "test_sample.py").write_text(
            "def test_pass():\n"
            "    assert True\n"
            "\n"
            "def test_fail():\n"
            "    got = 'got-value'\n"
            "    expected = 'expected-value'\n"
            "    assert got == expected\n"
        )
        result = run_driver(
            "cli-pytest",
            f'"{sys.executable}" -m pytest test_sample.py -p no:cacheprovider '
            f'-c pytest.ini --rootdir="{tmp_path}"',
            cwd=str(tmp_path),
        )
        assert result.exit_code == 1
        assert result.ok is False
        by_id = {t["id"]: t for t in result.tests}
        assert by_id["test_sample::test_pass"]["status"] == "pass"
        fail = by_id["test_sample::test_fail"]
        assert fail["status"] == "fail"
        assert fail["got"] == "'got-value'"
        assert fail["expected"] == "'expected-value'"

    def test_ms_template_rendered_before_running(self, tmp_path) -> None:
        # Two ms dirs; only one contains a test. If `{ms}` weren't
        # substituted, "pytest {ms}" would fail to resolve any path and
        # collect zero tests — so a green single-test result proves the
        # substitution pointed pytest at the right directory.
        (tmp_path / "ms-37").mkdir()
        (tmp_path / "ms-37" / "test_sample.py").write_text(
            "def test_pass():\n    assert True\n"
        )
        (tmp_path / "ms-38").mkdir()
        (tmp_path / "ms-38" / "test_sample.py").write_text(
            "def test_pass():\n    assert False\n"
        )
        # Same ambient-config pin as the test above (#2170). This one asserts
        # `endswith`, so a shifted rootdir alone wouldn't break it -- but an
        # ancestor ini's `addopts` still could (`--cov` with no pytest-cov ⇒
        # exit 4, and this asserts exit 0), so `-c` earns its place here too.
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        result = run_driver(
            "cli-pytest",
            f'"{sys.executable}" -m pytest {{ms}} -p no:cacheprovider '
            f'-c pytest.ini --rootdir="{tmp_path}"',
            cwd=str(tmp_path),
            ms="ms-37",
        )
        assert result.exit_code == 0
        assert len(result.tests) == 1
        test = result.tests[0]
        assert test["status"] == "pass"
        assert test["id"].endswith("test_sample::test_pass")

    def test_crash_before_report_written_returns_no_tests(self, tmp_path) -> None:
        # A command that dies before pytest ever runs (e.g. a typo) leaves no
        # junit-xml file behind — surfaced as "0 tests found", not a crash.
        result = run_driver(
            "cli-pytest", "exit 2", cwd=str(tmp_path),
        )
        assert result.exit_code == 2
        assert result.tests == []


class TestParsePlaywrightJsonReport:
    def test_all_pass_across_two_projects(self) -> None:
        tests = parse_playwright_json_report(_read("all_pass.json"))
        assert len(tests) == 4
        assert all(t["status"] == "pass" for t in tests)
        assert all(t["message"] == "" for t in tests)
        ids = {t["id"] for t in tests}
        # Same two test titles run under both projects must not collide.
        assert len(ids) == 4
        assert any(i.startswith("[chromium]") for i in ids)
        assert any(i.startswith("[firefox]") for i in ids)

    def test_mixed_fail_reports_pass_and_fail(self) -> None:
        tests = parse_playwright_json_report(_read("mixed_fail.json"))
        assert {t["status"] for t in tests} == {"pass", "fail"}
        failing = next(t for t in tests if t["status"] == "fail")
        assert "selects an item" in failing["id"]
        assert "Expected: 3" in failing["message"]
        assert "Received: 2" in failing["message"]
        # Playwright bakes ANSI SGR codes into this message (see the
        # module docstring at the top of this file) — must be stripped.
        assert "\x1b[" not in failing["message"]

    def test_skip_and_fixme_map_to_skip_with_reason(self) -> None:
        tests = parse_playwright_json_report(_read("skip.json"))
        by_title = {t["id"].rsplit(" › ", 1)[-1]: t for t in tests}
        assert by_title["shows issue list"]["status"] == "pass"
        assert by_title["not yet implemented"]["status"] == "skip"
        assert by_title["not yet implemented"]["message"] == ""
        assert by_title["needs staging env"]["status"] == "skip"
        assert (
            by_title["needs staging env"]["message"]
            == "blocked on #1541 browser capability"
        )

    def test_retry_then_pass_is_a_pass_but_notes_the_flake(self) -> None:
        tests = parse_playwright_json_report(_read("retry_then_pass.json"))
        assert len(tests) == 1
        test = tests[0]
        assert test["status"] == "pass"  # eventually green — gate lets it through
        assert "flaky" in test["message"]
        assert "1 failed attempt" in test["message"]
        assert "flaked on first attempt" in test["message"]  # first failure preserved

    def test_junit_sibling_loses_the_flake_signal(self) -> None:
        # The SAME run, same test, captured by Playwright's OTHER built-in
        # reporter — this is the empirical justification for choosing json
        # over junit (see the module docstring / parse_playwright_json_report
        # docstring), not just an assertion in prose. junit collapses every
        # retry attempt into one <testcase> with no failure element at all
        # once the test eventually passes — the flake is invisible.
        tests = parse_pytest_junit_xml(_read("retry_then_pass.junit.xml"))
        assert len(tests) == 1
        assert tests[0]["status"] == "pass"
        assert tests[0]["message"] == ""

    def test_global_setup_crash_raises_not_empty_list(self) -> None:
        # A well-formed report — valid JSON, "suites": [] — must still raise
        # rather than come back as an innocuous empty list, because Playwright's
        # top-level "errors" says the run never actually exercised any tests.
        with pytest.raises(DriverError, match="top-level error"):
            parse_playwright_json_report(_read("global_setup_crash.json"))

    def test_truncated_report_raises(self) -> None:
        with pytest.raises(DriverError, match="not valid JSON"):
            parse_playwright_json_report(_read("truncated.json"))

    def test_empty_input_raises(self) -> None:
        with pytest.raises(DriverError, match="empty"):
            parse_playwright_json_report("")
        with pytest.raises(DriverError, match="empty"):
            parse_playwright_json_report(None)  # type: ignore[arg-type]

    def test_missing_suites_key_raises(self) -> None:
        with pytest.raises(DriverError, match="unrecognized shape"):
            parse_playwright_json_report(json.dumps({"not": "a report"}))

    def test_legitimate_zero_tests_with_no_errors_returns_empty_list(self) -> None:
        # Playwright's own `--pass-with-no-tests` opt-in: valid report,
        # genuinely zero tests, no top-level errors, exit 0. Not a crash —
        # build_verdict() already treats an empty tests list as not-green,
        # so this doesn't need to raise to avoid a false "all green".
        report = json.dumps({"suites": [], "errors": [], "stats": {}})
        assert parse_playwright_json_report(report) == []


class TestRunDriverWebPlaywright:
    def test_web_playwright_no_longer_raises_not_implemented(self, tmp_path) -> None:
        # A crashing fake command still exercises run_driver's kind-routing
        # (proving "web-playwright" is no longer rejected up front) without
        # needing node/playwright installed — the DriverError it does raise
        # is about the missing report, not about the kind being unsupported.
        with pytest.raises(DriverError) as exc_info:
            run_driver("web-playwright", "exit 2", cwd=str(tmp_path))
        assert "not implemented" not in str(exc_info.value)
        assert "wrote no report" in str(exc_info.value)

    def test_supported_kinds_tuple_has_web_playwright(self) -> None:
        assert "web-playwright" in SUPPORTED_KINDS

    def test_appends_reporter_json_flag_and_honors_output_file_env(self, tmp_path) -> None:
        # Proves the two things _run_web_playwright is responsible for
        # wiring: appending `--reporter=json` (so a repo's own
        # playwright.config.ts reporter choice doesn't matter) and setting
        # PLAYWRIGHT_JSON_OUTPUT_FILE (so the report lands at a path coord
        # controls) — without needing a real `npx playwright`/node install
        # in this test environment. This tiny shell function stands in for
        # Playwright: it only copies the fixture into place if it actually
        # received "--reporter=json" as an argument, so the test fails if
        # that flag is ever dropped.
        fixture = FIXTURES / "all_pass.json"
        run_command = (
            'pw() { if [ "$1" = "--reporter=json" ]; then '
            f'cp "{fixture}" "$PLAYWRIGHT_JSON_OUTPUT_FILE"; else exit 9; fi; }}; pw'
        )
        result = run_driver("web-playwright", run_command, cwd=str(tmp_path))
        assert result.exit_code == 0
        assert result.ok is True
        assert len(result.tests) == 4

    def test_runs_shell_command_and_parses_json_report(self, tmp_path) -> None:
        fixture = FIXTURES / "mixed_fail.json"
        # Trailing "#" comments out coord's own appended `--reporter=json`
        # so this simple `cp` command stays valid either way.
        run_command = f'cp "{fixture}" "$PLAYWRIGHT_JSON_OUTPUT_FILE" #'
        result = run_driver("web-playwright", run_command, cwd=str(tmp_path))
        assert result.exit_code == 0
        assert {t["status"] for t in result.tests} == {"pass", "fail"}

    def test_crash_before_report_written_raises(self, tmp_path) -> None:
        # Unlike cli-pytest's equivalent test (a missing report there is a
        # benign "0 tests found"), web-playwright must never treat a
        # crashed run as an empty pass list — see #1539.
        with pytest.raises(DriverError, match="wrote no report"):
            run_driver("web-playwright", "exit 2", cwd=str(tmp_path))

    def test_truncated_report_raises_driver_error(self, tmp_path) -> None:
        fixture = FIXTURES / "truncated.json"
        run_command = f'cp "{fixture}" "$PLAYWRIGHT_JSON_OUTPUT_FILE" #'
        with pytest.raises(DriverError, match="not valid JSON"):
            run_driver("web-playwright", run_command, cwd=str(tmp_path))

    def test_global_setup_crash_report_raises_driver_error(self, tmp_path) -> None:
        fixture = FIXTURES / "global_setup_crash.json"
        run_command = f'cp "{fixture}" "$PLAYWRIGHT_JSON_OUTPUT_FILE" #'
        with pytest.raises(DriverError, match="top-level error"):
            run_driver("web-playwright", run_command, cwd=str(tmp_path))

    def test_timeout_raises_driver_error(self, tmp_path) -> None:
        # Trailing "#" comments out coord's own appended `--reporter=json`,
        # which `sleep` would otherwise reject outright (no timeout needed
        # to observe that failure — this test wants an actual timeout).
        with pytest.raises(DriverError, match="timed out"):
            run_driver("web-playwright", "sleep 5 #", cwd=str(tmp_path), timeout=1)

    def test_ms_template_rendered_before_running(self, tmp_path) -> None:
        # The {ms} substitution mechanics themselves are already covered
        # generically (TestRenderRunCommand) and per-kind for cli-pytest
        # (test_ms_template_rendered_before_running above); this just proves
        # run_driver plumbs ms= through to the web-playwright path too, by
        # asserting the substituted text survives into the executed command
        # (inside a comment, since this fake driver doesn't consume it).
        fixture = FIXTURES / "all_pass.json"
        run_command = f'cp "{fixture}" "$PLAYWRIGHT_JSON_OUTPUT_FILE" # {{ms}}'
        result = run_driver(
            "web-playwright", run_command, cwd=str(tmp_path), ms="ms-37",
        )
        assert result.exit_code == 0
        assert len(result.tests) == 4


class TestZeroTestPlaywrightRunIsAFailureNotAPass:
    """#1540 acceptance criteria: "A zero-test Playwright run is reported as
    a failure, not a pass" — the #1552-shaped wiring bug in Playwright form
    (docs/ORACLE_LOOP.md "Discovery"): a `testDir`/path-filter mismatch makes
    Playwright exit 0 with 0 tests, which must never render as a green
    verdict. #1539 already built each half (this module's DriverError for a
    crash-shaped zero, and ``coord.acceptance.build_verdict``'s ``green =
    failed == 0 and len(tests) > 0`` for a legitimate zero) — these two tests
    are #1540's assertion that wiring the ``run_driver`` -> ``build_verdict``
    path together for ``web-playwright`` actually produces a failing verdict
    in BOTH the "crashed" and the "legitimately found nothing" case, not just
    that each half in isolation behaves.
    """

    def test_legitimate_zero_tests_is_not_green(self, tmp_path) -> None:
        # Well-formed report, genuinely zero specs matched (Playwright's own
        # --pass-with-no-tests shape), no top-level errors — run_driver
        # returns an empty list rather than raising (see
        # test_legitimate_zero_tests_with_no_errors_returns_empty_list
        # above), but that empty list must still fail build_verdict's gate.
        report_src = tmp_path / "zero_tests_report.json"
        report_src.write_text(json.dumps({"suites": [], "errors": [], "stats": {}}))
        run_command = f'cp "{report_src}" "$PLAYWRIGHT_JSON_OUTPUT_FILE" #'
        result = run_driver("web-playwright", run_command, cwd=str(tmp_path))
        assert result.tests == []
        verdict = build_verdict(result.tests, scope="repo")
        assert verdict["green"] is False
        assert verdict["total"] == 0

    def test_crashed_run_never_reaches_build_verdict_as_a_pass(self, tmp_path) -> None:
        # The other zero-tests shape: Playwright dies before ever exercising
        # a spec (bad config, browser launch failure, --grep matching
        # nothing without --pass-with-no-tests). run_driver must raise
        # DriverError here — a caller that let this fall through to
        # build_verdict([], ...) would render it identically to "0 tests,
        # nothing wrong", silently losing the crash signal.
        fixture = FIXTURES / "global_setup_crash.json"
        run_command = f'cp "{fixture}" "$PLAYWRIGHT_JSON_OUTPUT_FILE" #'
        with pytest.raises(DriverError, match="top-level error"):
            run_driver("web-playwright", run_command, cwd=str(tmp_path))
