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
import os
import shlex
import sys
from pathlib import Path

import pytest

from coord.acceptance import build_verdict
from coord.acceptance_drivers import (
    DriverError,
    EPHEMERAL_RG_PATTERN,
    FIXTURE_SERVER_DEPENDENT_KINDS,
    SUPPORTED_KINDS,
    VALIDATE_ONLY_KINDS,
    assert_ephemeral_rg,
    parse_conftest_json,
    parse_playwright_json_report,
    parse_pytest_junit_xml,
    parse_terraform_validate_json,
    parse_test_output,
    parse_tflint_json,
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


class TestValidateOnlyKinds:
    """#3232: `coord.repo_onboard`'s oracle-readiness layer reads this set to
    flag a driver that produces a real, deterministic verdict but is
    intrinsically scoped to a compile/syntax check, not a full oracle —
    distinct from `FIXTURE_SERVER_DEPENDENT_KINDS`'s "missing shared
    dependency" gap."""

    def test_terraform_is_validate_only(self) -> None:
        assert "terraform" in VALIDATE_ONLY_KINDS

    def test_only_kinds_this_module_actually_supports_are_listed(self) -> None:
        # Mirrors TestFixtureServerDependentKinds's equivalent check — a kind
        # declared here but not in SUPPORTED_KINDS would be an unreachable
        # warning.
        assert VALIDATE_ONLY_KINDS <= set(SUPPORTED_KINDS)

    def test_other_kinds_are_not_flagged(self) -> None:
        assert "tui-tuidriver" not in VALIDATE_ONLY_KINDS
        assert "cli-pytest" not in VALIDATE_ONLY_KINDS
        assert "web-playwright" not in VALIDATE_ONLY_KINDS

    def test_disjoint_from_fixture_server_dependent_kinds(self) -> None:
        # Two distinct questions -- a missing *shared* dependency (the
        # fixture server) vs. an adapter *intrinsically* narrower in scope
        # -- so no kind should ever answer both at once.
        assert not (VALIDATE_ONLY_KINDS & FIXTURE_SERVER_DEPENDENT_KINDS)


def _fake_terraform(*, init_exit: int = 0, validate_json: str = "", validate_exit: int = 0) -> str:
    """A shell function standing in for the real ``terraform`` binary (not
    installed in this test environment — same trick
    ``TestRunDriverWebPlaywright``'s ``pw()`` fake uses for ``npx
    playwright``). Only understands ``init``/``validate``; ``validate`` only
    ever prints *validate_json* when invoked with ``-json`` as its second
    arg, so a test using this fails loudly if ``_run_terraform`` ever stops
    forcing that flag onto the trailing ``terraform validate``.

    Uses ``return``, not ``exit``, inside the function body — ``exit``
    inside a shell function terminates the whole script/subshell, not just
    that call, which would make ``terraform init && terraform validate``
    stop at ``init`` regardless of its exit code and never reach
    ``validate`` at all.
    """
    return (
        "terraform() { "
        f'if [ "$1" = init ]; then return {init_exit}; '
        'elif [ "$1" = validate ]; then '
        'if [ "$2" = -json ]; then '
        f"echo {shlex.quote(validate_json)}; return {validate_exit}; "
        "else return 9; fi; "
        "fi; }; "
        "terraform init -backend=false && terraform validate"
    )


class TestRunDriverTerraform:
    """#3232: `terraform init -backend=false` + `terraform validate` only —
    no credentials, no state backend, no `terraform plan`. Runnable on any
    machine with the `terraform` binary, which this test environment does
    not have — so, like TestRunDriverWebPlaywright, these fake the binary
    with a shell function rather than skipping the coverage entirely."""

    def test_supported_kinds_tuple_has_terraform(self) -> None:
        assert "terraform" in SUPPORTED_KINDS

    def test_appends_json_flag_and_parses_clean_pass(self, tmp_path) -> None:
        # If `_run_terraform` ever stopped appending `-json`, the fake's
        # `$2 != -json` branch would `exit 9` with empty stdout instead —
        # this test fails loudly rather than silently accepting either.
        validate_json = json.dumps(
            {"valid": True, "error_count": 0, "warning_count": 0, "diagnostics": []}
        )
        run_command = _fake_terraform(validate_json=validate_json)
        result = run_driver("terraform", run_command, cwd=str(tmp_path))
        assert result.exit_code == 0
        assert result.ok is True
        assert result.tests == [
            {"id": "terraform validate", "status": "pass", "message": ""},
        ]

    def test_reports_fail_entry_per_error_diagnostic(self, tmp_path) -> None:
        validate_json = json.dumps({
            "valid": False, "error_count": 1, "warning_count": 0,
            "diagnostics": [{
                "severity": "error",
                "summary": "Unsupported argument",
                "detail": 'An argument named "foo" is not expected here.',
                "range": {"filename": "main.tf"},
            }],
        })
        run_command = _fake_terraform(validate_json=validate_json, validate_exit=1)
        result = run_driver("terraform", run_command, cwd=str(tmp_path))
        assert result.exit_code == 1
        assert result.ok is False
        assert result.tests == [{
            "id": "main.tf: Unsupported argument",
            "status": "fail",
            "message": 'An argument named "foo" is not expected here.',
        }]

    def test_warnings_do_not_fail_validation_or_produce_their_own_entries(
        self, tmp_path
    ) -> None:
        validate_json = json.dumps({
            "valid": True, "error_count": 0, "warning_count": 2,
            "diagnostics": [
                {"severity": "warning", "summary": "deprecated attribute", "detail": "..."},
            ],
        })
        run_command = _fake_terraform(validate_json=validate_json)
        result = run_driver("terraform", run_command, cwd=str(tmp_path))
        assert result.tests == [
            {"id": "terraform validate", "status": "pass", "message": "2 warning(s)"},
        ]

    def test_init_failure_short_circuits_validate(self, tmp_path) -> None:
        # A provider version constraint that can't resolve fails at `init`,
        # before `validate` ever runs — the shell `&&` must never call
        # `validate` in that case, and this driver must report it as a real
        # failure rather than an empty "0 tests found".
        marker = tmp_path / "validate-ran"
        run_command = (
            "terraform() { "
            'if [ "$1" = init ]; then return 1; '
            f'elif [ "$1" = validate ]; then touch {marker}; return 0; '
            "fi; }; "
            "terraform init -backend=false && terraform validate"
        )
        result = run_driver("terraform", run_command, cwd=str(tmp_path))
        assert result.exit_code == 1
        assert not marker.exists()
        assert len(result.tests) == 1
        assert result.tests[0]["status"] == "fail"
        assert result.tests[0]["id"] == "terraform init"

    def test_timeout_raises_driver_error(self, tmp_path) -> None:
        # Trailing "#" comments out coord's own appended `-json` (mirrors
        # TestRunDriverWebPlaywright.test_timeout_raises_driver_error's
        # `sleep 5 #` — an un-commented `-json` arg would make `sleep`
        # itself error out instantly instead of actually sleeping).
        with pytest.raises(DriverError, match="timed out"):
            run_driver("terraform", "sleep 5 #", cwd=str(tmp_path), timeout=1)

    def test_never_runs_terraform_plan(self, tmp_path) -> None:
        # #3232 scope guard: this v1 slice must never invoke `plan` (needs
        # provider credentials -- #3230 child 2). A fake that only responds
        # to init/validate and `return 9`s on anything else proves `plan` is
        # never attempted, since an attempt would surface as that exit code.
        validate_json = json.dumps(
            {"valid": True, "error_count": 0, "warning_count": 0, "diagnostics": []}
        )
        run_command = (
            "terraform() { "
            'if [ "$1" = init ]; then return 0; '
            'elif [ "$1" = validate ] && [ "$2" = -json ]; then '
            f"echo {shlex.quote(validate_json)}; return 0; "
            "else return 9; fi; "
            "}; terraform init -backend=false && terraform validate"
        )
        result = run_driver("terraform", run_command, cwd=str(tmp_path))
        assert result.exit_code == 0
        assert result.tests[0]["status"] == "pass"


class TestParseTerraformValidateJson:
    def test_clean_pass_no_warnings(self) -> None:
        stdout = json.dumps(
            {"valid": True, "error_count": 0, "warning_count": 0, "diagnostics": []}
        )
        assert parse_terraform_validate_json(stdout) == [
            {"id": "terraform validate", "status": "pass", "message": ""},
        ]

    def test_clean_pass_notes_warning_count(self) -> None:
        stdout = json.dumps({
            "valid": True, "error_count": 0, "warning_count": 3,
            "diagnostics": [{"severity": "warning", "summary": "x", "detail": "y"}],
        })
        tests = parse_terraform_validate_json(stdout)
        assert tests == [
            {"id": "terraform validate", "status": "pass", "message": "3 warning(s)"},
        ]

    def test_single_error_diagnostic_id_prefixed_with_filename(self) -> None:
        stdout = json.dumps({
            "valid": False, "error_count": 1, "warning_count": 0,
            "diagnostics": [{
                "severity": "error", "summary": "Missing required argument",
                "detail": "The argument \"ami\" is required.",
                "range": {"filename": "main.tf"},
            }],
        })
        tests = parse_terraform_validate_json(stdout)
        assert tests == [{
            "id": "main.tf: Missing required argument",
            "status": "fail",
            "message": 'The argument "ami" is required.',
        }]

    def test_multiple_error_diagnostics_each_become_an_entry(self) -> None:
        stdout = json.dumps({
            "valid": False, "error_count": 2, "warning_count": 0,
            "diagnostics": [
                {"severity": "error", "summary": "bad a", "detail": "detail a",
                 "range": {"filename": "a.tf"}},
                {"severity": "error", "summary": "bad b", "detail": "detail b",
                 "range": {"filename": "b.tf"}},
            ],
        })
        tests = parse_terraform_validate_json(stdout)
        assert [t["id"] for t in tests] == ["a.tf: bad a", "b.tf: bad b"]
        assert all(t["status"] == "fail" for t in tests)

    def test_invalid_with_no_parseable_diagnostics_still_fails(self) -> None:
        # `"valid": false` must never silently fall through to an empty
        # list -- build_verdict would read that as "nothing to check"
        # rather than "invalid".
        stdout = json.dumps({"valid": False, "error_count": 1, "warning_count": 0, "diagnostics": []})
        tests = parse_terraform_validate_json(stdout)
        assert len(tests) == 1
        assert tests[0]["status"] == "fail"

    def test_empty_stdout_reports_terraform_init_failure(self) -> None:
        tests = parse_terraform_validate_json("", exit_code=1, stderr="Error: no terraform binary\nboom")
        assert len(tests) == 1
        assert tests[0]["id"] == "terraform init"
        assert tests[0]["status"] == "fail"
        assert "no terraform binary" in tests[0]["message"]
        assert "exit 1" in tests[0]["message"]

    def test_non_json_stdout_reports_terraform_validate_failure(self) -> None:
        tests = parse_terraform_validate_json("not json at all", exit_code=1)
        assert len(tests) == 1
        assert tests[0]["id"] == "terraform validate"
        assert tests[0]["status"] == "fail"

    def test_missing_valid_key_treated_as_unrecognized(self) -> None:
        tests = parse_terraform_validate_json(json.dumps({"foo": "bar"}))
        assert len(tests) == 1
        assert tests[0]["status"] == "fail"


class TestParseTflintJson:
    """#3234: `tflint --format=json` -> normalized `{"id", "status",
    "message"}` entries, the same shape `parse_terraform_validate_json`
    already produces so both fold onto one `tests` list."""

    def test_clean_pass_no_issues(self) -> None:
        stdout = json.dumps({"issues": [], "errors": []})
        assert parse_tflint_json(stdout) == [
            {"id": "tflint", "status": "pass", "message": ""},
        ]

    def test_only_nonblocking_issues_still_passes(self) -> None:
        stdout = json.dumps({
            "issues": [{
                "rule": {"name": "terraform_deprecated_interpolation", "severity": "warning"},
                "message": "old interpolation style",
                "range": {"filename": "main.tf", "start": {"line": 3}},
            }],
            "errors": [],
        })
        assert parse_tflint_json(stdout) == [
            {"id": "tflint", "status": "pass", "message": "1 non-blocking issue(s)"},
        ]

    def test_error_severity_issue_reported_as_fail(self) -> None:
        stdout = json.dumps({
            "issues": [{
                "rule": {"name": "terraform_required_version", "severity": "error"},
                "message": "provider version must be pinned",
                "range": {"filename": "main.tf", "start": {"line": 1}},
            }],
            "errors": [],
        })
        assert parse_tflint_json(stdout) == [{
            "id": "main.tf:1: terraform_required_version",
            "status": "fail",
            "message": "provider version must be pinned",
        }]

    def test_multiple_error_issues_each_get_own_entry_warnings_dropped(self) -> None:
        stdout = json.dumps({
            "issues": [
                {"rule": {"name": "rule_a", "severity": "error"}, "message": "a bad",
                 "range": {"filename": "a.tf", "start": {"line": 1}}},
                {"rule": {"name": "rule_b", "severity": "error"}, "message": "b bad",
                 "range": {"filename": "b.tf", "start": {"line": 2}}},
                {"rule": {"name": "rule_c", "severity": "warning"}, "message": "c minor",
                 "range": {"filename": "c.tf", "start": {"line": 3}}},
            ],
            "errors": [],
        })
        tests = parse_tflint_json(stdout)
        assert [t["id"] for t in tests] == ["a.tf:1: rule_a", "b.tf:2: rule_b"]
        assert all(t["status"] == "fail" for t in tests)

    def test_top_level_execution_errors_fail_the_whole_check(self) -> None:
        # An execution error (bad .tflint.hcl, unresolvable plugin) means
        # tflint never got far enough to lint anything -- distinct from a
        # clean "issues": [] run and must not read as one.
        stdout = json.dumps({"issues": [], "errors": [{"message": "failed to load plugin"}]})
        tests = parse_tflint_json(stdout)
        assert len(tests) == 1
        assert tests[0] == {
            "id": "tflint", "status": "fail",
            "message": "tflint reported execution error(s): failed to load plugin",
        }

    def test_empty_stdout_reports_failure_not_empty_pass(self) -> None:
        tests = parse_tflint_json("", exit_code=127, stderr="sh: 1: tflint: not found")
        assert len(tests) == 1
        assert tests[0]["id"] == "tflint"
        assert tests[0]["status"] == "fail"
        assert "not found" in tests[0]["message"]
        assert "exit 127" in tests[0]["message"]

    def test_non_json_stdout_reports_failure(self) -> None:
        tests = parse_tflint_json("not json at all", exit_code=1)
        assert len(tests) == 1
        assert tests[0]["id"] == "tflint"
        assert tests[0]["status"] == "fail"

    def test_missing_issues_key_treated_as_unrecognized(self) -> None:
        tests = parse_tflint_json(json.dumps({"foo": "bar"}))
        assert len(tests) == 1
        assert tests[0]["status"] == "fail"


class TestParseConftestJson:
    """#3234: `conftest test --output=json` -> normalized `{"id", "status",
    "message"}` entries, matching this module's other parse_* functions'
    shape."""

    def test_clean_pass_no_findings(self) -> None:
        stdout = json.dumps([
            {"filename": "main.tf", "namespace": "main", "successes": 3,
             "failures": [], "warnings": [], "exceptions": []},
        ])
        assert parse_conftest_json(stdout) == [
            {"id": "conftest: main.tf", "status": "pass", "message": ""},
        ]

    def test_warnings_do_not_fail_but_are_noted(self) -> None:
        stdout = json.dumps([
            {"filename": "main.tf", "successes": 1, "failures": [], "exceptions": [],
             "warnings": [{"msg": "consider tagging"}]},
        ])
        assert parse_conftest_json(stdout) == [
            {"id": "conftest: main.tf", "status": "pass", "message": "1 warning(s)"},
        ]

    def test_failure_reported_per_violation(self) -> None:
        stdout = json.dumps([
            {"filename": "main.tf", "successes": 0,
             "failures": [{"msg": "required tag 'owner' is missing"}],
             "warnings": [], "exceptions": []},
        ])
        assert parse_conftest_json(stdout) == [{
            "id": "main.tf: required tag 'owner' is missing",
            "status": "fail",
            "message": "required tag 'owner' is missing",
        }]

    def test_policy_exception_fails_distinctly_from_a_violation(self) -> None:
        # A rego evaluation error (undefined function, missing input field
        # a rule assumed existed) means the policy never rendered a real
        # verdict at all -- must not be confused with "no violations found".
        stdout = json.dumps([
            {"filename": "main.tf", "successes": 0, "failures": [],
             "warnings": [], "exceptions": [{"msg": "undefined function foo"}]},
        ])
        tests = parse_conftest_json(stdout)
        assert len(tests) == 1
        assert tests[0]["status"] == "fail"
        assert "policy error" in tests[0]["id"]
        assert tests[0]["message"] == "undefined function foo"

    def test_multiple_files_each_reported(self) -> None:
        stdout = json.dumps([
            {"filename": "a.tf", "successes": 1, "failures": [], "warnings": [], "exceptions": []},
            {"filename": "b.tf", "successes": 0,
             "failures": [{"msg": "no local-exec"}], "warnings": [], "exceptions": []},
        ])
        assert parse_conftest_json(stdout) == [
            {"id": "conftest: a.tf", "status": "pass", "message": ""},
            {"id": "b.tf: no local-exec", "status": "fail", "message": "no local-exec"},
        ]

    def test_empty_array_is_a_real_pass_not_a_crash(self) -> None:
        # A well-formed, genuinely empty report (no matching input files)
        # is a real observation -- distinct from the crash cases below,
        # which must fail rather than read as "nothing to check".
        assert parse_conftest_json("[]") == [
            {"id": "conftest", "status": "pass", "message": "no input files evaluated"},
        ]

    def test_empty_stdout_reports_failure_not_empty_pass(self) -> None:
        tests = parse_conftest_json("", exit_code=127, stderr="sh: 1: conftest: not found")
        assert len(tests) == 1
        assert tests[0]["id"] == "conftest"
        assert tests[0]["status"] == "fail"
        assert "not found" in tests[0]["message"]

    def test_non_list_stdout_reports_failure(self) -> None:
        tests = parse_conftest_json(json.dumps({"not": "a list"}))
        assert len(tests) == 1
        assert tests[0]["status"] == "fail"


def _write_executable(path: Path, script: str) -> None:
    """A fake binary standing in for the real `tflint`/`conftest` (neither
    installed in this test environment, same trick `_fake_terraform`/
    `TestRunDriverWebPlaywright`'s `pw()` use)."""
    path.write_text(script)
    path.chmod(0o755)


class TestRunDriverTerraformPolicyGate:
    """#3234: `_run_terraform` runs an opt-in tflint/conftest policy gate
    after `validate`, gated on `.tflint.hcl`/`policy/` presence in the
    driven repo's cwd, and folds the results onto the same `tests` list."""

    @staticmethod
    def _terraform_ok() -> str:
        validate_json = json.dumps(
            {"valid": True, "error_count": 0, "warning_count": 0, "diagnostics": []}
        )
        return _fake_terraform(validate_json=validate_json)

    def test_no_policy_files_means_no_policy_entries(self, tmp_path) -> None:
        # An un-opted-in repo (neither convention file present) keeps
        # getting exactly the plain validate-only verdict it always did.
        result = run_driver("terraform", self._terraform_ok(), cwd=str(tmp_path))
        assert result.tests == [
            {"id": "terraform validate", "status": "pass", "message": ""},
        ]
        assert result.exit_code == 0

    def test_tflint_runs_when_config_file_present(self, tmp_path, monkeypatch) -> None:
        (tmp_path / ".tflint.hcl").write_text("")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        issues = json.dumps({"issues": [], "errors": []})
        _write_executable(
            bin_dir / "tflint",
            "#!/bin/sh\n"
            f'if [ "$1" = --format=json ]; then echo {shlex.quote(issues)}; else exit 9; fi\n',
        )
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        result = run_driver("terraform", self._terraform_ok(), cwd=str(tmp_path))
        assert result.tests == [
            {"id": "terraform validate", "status": "pass", "message": ""},
            {"id": "tflint", "status": "pass", "message": ""},
        ]
        assert result.exit_code == 0

    def test_tflint_error_issue_fails_the_overall_run(self, tmp_path, monkeypatch) -> None:
        (tmp_path / ".tflint.hcl").write_text("")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        issues = json.dumps({
            "issues": [{
                "rule": {"name": "terraform_required_version", "severity": "error"},
                "message": "provider version must be pinned",
                "range": {"filename": "main.tf", "start": {"line": 1}},
            }],
            "errors": [],
        })
        _write_executable(bin_dir / "tflint", f"#!/bin/sh\necho {shlex.quote(issues)}\n")
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        result = run_driver("terraform", self._terraform_ok(), cwd=str(tmp_path))
        # `validate` itself passed cleanly, but a real policy violation was
        # found -- the driver's own exit_code must reflect that too (#2096:
        # unconfirmed success is a defect; a caller reading exit_code alone
        # must not see this as a clean run).
        assert result.exit_code != 0
        assert {
            "id": "main.tf:1: terraform_required_version",
            "status": "fail",
            "message": "provider version must be pinned",
        } in result.tests

    def test_missing_tflint_binary_fails_rather_than_silently_passing(
        self, tmp_path, monkeypatch
    ) -> None:
        # Opted in (.tflint.hcl present) but the binary isn't -- must not
        # be silently dropped and read as "no violations found".
        (tmp_path / ".tflint.hcl").write_text("")
        monkeypatch.setenv("PATH", str(tmp_path / "no-such-bin-dir"))
        result = run_driver("terraform", self._terraform_ok(), cwd=str(tmp_path))
        tflint_entries = [t for t in result.tests if t["id"] == "tflint"]
        assert len(tflint_entries) == 1
        assert tflint_entries[0]["status"] == "fail"
        assert result.exit_code != 0

    def test_conftest_runs_when_policy_dir_present(self, tmp_path, monkeypatch) -> None:
        (tmp_path / "policy").mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        report = json.dumps([
            {"filename": "main.tf", "successes": 1, "failures": [], "warnings": [], "exceptions": []},
        ])
        _write_executable(bin_dir / "conftest", f"#!/bin/sh\necho {shlex.quote(report)}\n")
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        result = run_driver("terraform", self._terraform_ok(), cwd=str(tmp_path))
        assert result.tests == [
            {"id": "terraform validate", "status": "pass", "message": ""},
            {"id": "conftest: main.tf", "status": "pass", "message": ""},
        ]
        assert result.exit_code == 0

    def test_conftest_failure_fails_the_overall_run(self, tmp_path, monkeypatch) -> None:
        (tmp_path / "policy").mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        report = json.dumps([
            {"filename": "main.tf", "successes": 0,
             "failures": [{"msg": "no local-exec"}], "warnings": [], "exceptions": []},
        ])
        _write_executable(bin_dir / "conftest", f"#!/bin/sh\necho {shlex.quote(report)}\n")
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        result = run_driver("terraform", self._terraform_ok(), cwd=str(tmp_path))
        assert result.exit_code != 0
        assert {
            "id": "main.tf: no local-exec", "status": "fail", "message": "no local-exec",
        } in result.tests

    def test_both_tools_run_and_merge_when_both_opted_in(self, tmp_path, monkeypatch) -> None:
        (tmp_path / ".tflint.hcl").write_text("")
        (tmp_path / "policy").mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        tflint_report = json.dumps({"issues": [], "errors": []})
        conftest_report = json.dumps([
            {"filename": "main.tf", "successes": 1, "failures": [], "warnings": [], "exceptions": []},
        ])
        _write_executable(bin_dir / "tflint", f"#!/bin/sh\necho {shlex.quote(tflint_report)}\n")
        _write_executable(bin_dir / "conftest", f"#!/bin/sh\necho {shlex.quote(conftest_report)}\n")
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        result = run_driver("terraform", self._terraform_ok(), cwd=str(tmp_path))
        assert result.tests == [
            {"id": "terraform validate", "status": "pass", "message": ""},
            {"id": "tflint", "status": "pass", "message": ""},
            {"id": "conftest: main.tf", "status": "pass", "message": ""},
        ]
        assert result.exit_code == 0


class TestAssertEphemeralRg:
    """#3303: the ephemeral-apply probe's teardown safety guard, slice 1.

    No cloud account, no network, pure string validation — the guard must
    raise DriverError on anything that isn't exactly
    ``rg-coord-ephemeral-<8 lowercase hex>``, and must never merely return a
    falsy value that a caller could forget to check.
    """

    def test_accepts_a_valid_ephemeral_name(self) -> None:
        assert_ephemeral_rg("rg-coord-ephemeral-deadbeef") is None

    def test_accepts_various_valid_hex_suffixes(self) -> None:
        for suffix in ("00000000", "ffffffff", "0a1b2c3d", "12345678"):
            assert_ephemeral_rg(f"rg-coord-ephemeral-{suffix}") is None

    def test_rejects_rg_coord_shared(self) -> None:
        """Holds stcoordjdbackup — the off-site restic backup. Must never match."""
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-shared")

    def test_rejects_rg_coord_images(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-images")

    def test_rejects_rg_coord_pilot(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-pilot")

    def test_rejects_rg_coord_prod_tfstate(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-prod-tfstate")

    def test_rejects_a_name_that_merely_contains_a_valid_one(self) -> None:
        """Anchoring must reject this, not just plain substring checks."""
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-shared-rg-coord-ephemeral-deadbeef")

    def test_rejects_a_valid_name_with_trailing_suffix(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-ephemeral-deadbeefx")

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg("")

    def test_rejects_none(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg(None)  # type: ignore[arg-type]

    def test_rejects_non_string(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg(12345)  # type: ignore[arg-type]

    def test_rejects_a_list(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg(["rg-coord-ephemeral-deadbeef"])  # type: ignore[arg-type]

    def test_rejects_leading_whitespace(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg(" rg-coord-ephemeral-deadbeef")

    def test_rejects_trailing_whitespace(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-ephemeral-deadbeef ")

    def test_rejects_trailing_newline(self) -> None:
        """re.match's `$` matches before a trailing newline — fullmatch must not."""
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-ephemeral-deadbeef\n")

    def test_rejects_uppercase_hex(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-ephemeral-DEADBEEF")

    def test_rejects_short_hex_suffix(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-ephemeral-dead")

    def test_rejects_non_hex_suffix(self) -> None:
        with pytest.raises(DriverError):
            assert_ephemeral_rg("rg-coord-ephemeral-zzzzzzzz")

    def test_pattern_is_anchored_at_both_ends(self) -> None:
        assert EPHEMERAL_RG_PATTERN.pattern.startswith("^")
        assert EPHEMERAL_RG_PATTERN.pattern.endswith("$")
