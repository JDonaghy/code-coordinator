"""Framework driver adapters for ``coord acceptance`` (#944,
docs/ORACLE_LOOP.md).

``coord acceptance`` is a thin, framework-agnostic orchestrator; this module
is the one seam that varies per medium — TUI (quadraui ``TuiDriver``), CLI
(pytest), web (Playwright), native, etc. Each driver knows how to *run* a
repo's declared acceptance suite and *parse* its raw output into a
normalized list of ``{"id": str, "status": "pass"|"fail"|"skip", "message":
str}`` dicts (``cli-pytest`` additionally carries ``"expected"``/``"got"``
on a failing test — see :func:`parse_pytest_junit_xml`). ``tui-tuidriver``,
``cli-pytest`` (#1125), and ``web-playwright`` (#1539) are implemented;
other ``kind`` values are declared in ``coordinator.yml`` (see
:class:`coord.config.AcceptanceConfig`) but rejected here with a clear "not
yet implemented" error until their issues land (native).

``cli-pytest`` parses pytest's built-in ``--junit-xml`` report (a core
pytest flag, not a plugin — no extra dependency required in the driven
repo, unlike ``pytest-json-report``/``pytest-reportlog``) rather than
stdout, since junit-xml already carries a structured per-test
pass/fail/skip verdict plus each failure's message.

``web-playwright`` parses Playwright Test's built-in ``--reporter=json``
report rather than its built-in ``--reporter=junit`` one — see
:func:`parse_playwright_json_report` for why (short version: junit
collapses retries into one opaque CDATA blob with no per-attempt status,
which loses the "did this flake" signal this driver exists to capture; json
keeps a ``results[]`` entry per attempt).

:func:`run_driver` also runs a driver's optional ``setup:`` provisioning
command (#1733, ``AcceptanceDriverConfig.setup``) once before its suite —
e.g. ``npm ci`` for ``web-playwright``, which otherwise fails with a bare
``exit 127`` (playwright not found) the first time it runs against ``coord
acceptance record``'s throwaway, dependency-less worktree.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Driver kinds this module knows how to run. Keep in sync with the adapters
# implemented below — a kind can be *declared* in coordinator.yml ahead of its
# adapter landing, but running it must fail loudly rather than silently no-op.
SUPPORTED_KINDS = ("tui-tuidriver", "cli-pytest", "web-playwright")

# #2748 (IL-2): driver kinds whose `run` produces a real pass/fail verdict
# but NOT yet a deterministic one, because an input they depend on hasn't
# shipped. `web-playwright` is the one case today — the driver itself
# landed (#1539), but the seeded-board fixture server it needs to run
# against a fixed, known state (#1538) has not, so a run against a live
# fleet observes whatever that fleet happens to be doing rather than a
# pinned fixture (CLAUDE.md's web-playwright section: "a smoke net, not a
# deterministic oracle"). `coord.repo_onboard`'s oracle-readiness layer
# reads this to report the gap explicitly instead of a driver-present repo
# silently reading as fully oracle-ready. Kept here (not in coord.config)
# because it is exactly the kind of "which medium behaves how" fact this
# module already owns — see the module docstring.
FIXTURE_SERVER_DEPENDENT_KINDS = frozenset({"web-playwright"})

# libtest's ``--format json`` per-line test-event stream (`cargo test -- -Z
# unstable-options --format json`) event -> our normalized status.
_LIBTEST_EVENT_STATUS = {"ok": "pass", "failed": "fail", "ignored": "skip"}

# A junit-xml <failure>/<error> "message" attribute for a plain
# ``assert got == expected`` AssertionError has ``assert <got> ==
# <expected>`` on its first line (typically prefixed with the exception
# class, e.g. ``AssertionError: assert 'a' == 'b'``) — this is the common
# shape a cli-pytest test comparing actual CLI stdout to a `*.out` mock
# produces. Anything else (multi-line diffs, non-equality asserts, a raised
# exception with no ``assert``) is left unparsed rather than guessed at.
_ASSERT_EQ_RE = re.compile(r"assert\s+(.*?)\s+==\s+(.*)$")

# Playwright's JSON reporter bakes ANSI color/style codes (SGR sequences)
# straight into `error.message` regardless of whether stdout is a tty or
# `NO_COLOR`/`FORCE_COLOR=0` is set (verified empirically against 1.61 — the
# assertion diff formatter colors unconditionally) — strip them so a stored
# verdict message doesn't carry raw escape bytes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Playwright JSON reporter's per-test `status` (already reconciled against
# retries and `expectedStatus` — e.g. a `test.fail()`-annotated test that
# fails as expected is "expected", not "unexpected") -> our normalized
# status. "flaky" (failed at least once, then passed within `retries`) is a
# "pass" for gating purposes but callers should still care it happened — see
# :func:`parse_playwright_json_report`, which folds that into the message.
_PLAYWRIGHT_STATUS = {
    "expected": "pass",
    "flaky": "pass",
    "unexpected": "fail",
    "skipped": "skip",
}


class DriverError(Exception):
    """Raised when a driver can't run its suite or the ``kind`` is unknown."""


@dataclass
class DriverResult:
    """The outcome of running one driver invocation."""

    exit_code: int
    tests: list[dict] = field(default_factory=list)
    raw_output: str = ""

    @property
    def ok(self) -> bool:
        """True when the run command itself exited 0.

        This is distinct from "all tests passed" — a driver can exit 0 while
        reporting individual test failures (cargo's own exit code already
        reflects failures, but a hand-rolled ``run:`` wrapper might not), so
        callers should judge pass/fail from ``tests`` rather than this alone.
        """
        return self.exit_code == 0


def render_run_command(run_command: str, *, ms: str | None = None) -> str:
    """Substitute the ``{ms}`` template in *run_command* with *ms* (the
    ``ms-NN`` milestone dirname — see :func:`coord.acceptance.ms_dirname`),
    e.g. ``"pytest tests/acceptance/{ms}"`` -> ``"pytest
    tests/acceptance/ms-37"``.

    Left unsubstituted when *ms* is ``None`` — callers that aren't scoping to
    a milestone (or a driver's ``run:`` that never references ``{ms}`` at
    all, e.g. today's single-driver ``tui-tuidriver`` configs) pass the
    command through unchanged.
    """
    if ms is None:
        return run_command
    return run_command.replace("{ms}", ms)


def run_driver(
    kind: str, run_command: str, cwd: str, *, timeout: int = 900, ms: str | None = None,
    setup_command: str = "",
) -> DriverResult:
    """Execute *run_command* in *cwd* and parse its output for *kind*.

    Raises :class:`DriverError` for an unsupported *kind* or a timeout. A
    non-zero exit from the command is NOT raised — it's folded into the
    returned :class:`DriverResult` so callers can still inspect whatever
    partial JSON the suite printed before dying.

    *ms*, when given, renders the ``{ms}`` template in *run_command* first
    (see :func:`render_run_command`).

    *setup_command* (#1733, ``AcceptanceDriverConfig.setup``), when
    non-empty, runs ONCE in *cwd* before *run_command* — the provisioning
    step a driver needs a bare checkout doesn't provide (e.g. ``npm ci`` for
    ``web-playwright`` in ``coord acceptance record``'s throwaway,
    dependency-less worktree). Unlike a non-zero *run_command* exit, a
    failing *setup_command* DOES raise :class:`DriverError` — immediately,
    before *run_command* ever executes — with a message that names it as a
    provisioning failure so it isn't mistaken for a test failure or folded
    into a driver's own "wrote no report" crash message.
    """
    if kind not in SUPPORTED_KINDS:
        raise DriverError(
            f"acceptance driver kind {kind!r} is not implemented yet "
            f"(supported: {', '.join(SUPPORTED_KINDS)}). The native adapter "
            "lands in a later oracle-loop issue — see docs/ORACLE_LOOP.md."
        )

    if setup_command:
        _run_setup(setup_command, cwd, timeout=timeout)

    run_command = render_run_command(run_command, ms=ms)

    if kind == "cli-pytest":
        return _run_cli_pytest(run_command, cwd, timeout=timeout)
    if kind == "web-playwright":
        return _run_web_playwright(run_command, cwd, timeout=timeout)
    return _run_generic(run_command, cwd, timeout=timeout)


def _run_setup(setup_command: str, cwd: str, *, timeout: int) -> None:
    """Run a driver's ``setup:`` provisioning command (#1733) in *cwd*,
    before its suite ever runs.

    Raises :class:`DriverError` — distinctly worded as a provisioning
    failure, not a test failure — for a non-zero exit, a timeout, or the
    command failing to start at all. Callers must not proceed to
    ``run_command`` when this raises: a driver whose dependencies never
    installed cannot produce a meaningful verdict.
    """
    try:
        proc = subprocess.run(
            setup_command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise DriverError(
            f"acceptance driver provisioning timed out after {timeout}s: "
            f"{setup_command!r}"
        ) from e
    except OSError as e:
        raise DriverError(
            f"acceptance driver provisioning failed to start: {setup_command!r}: {e}"
        ) from e

    if proc.returncode != 0:
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        raise DriverError(
            f"acceptance driver provisioning failed (exit {proc.returncode}): "
            f"{setup_command!r}\n{stderr_tail}"
        )


def _run_generic(run_command: str, cwd: str, *, timeout: int) -> DriverResult:
    """The ``tui-tuidriver`` (and any future stdout-native) shape: the
    command itself is responsible for printing structured verdicts to
    stdout — this just runs it and hands the raw stdout to
    :func:`parse_test_output`."""
    try:
        proc = subprocess.run(
            run_command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise DriverError(
            f"acceptance run command timed out after {timeout}s: {run_command!r}"
        ) from e
    except OSError as e:
        raise DriverError(f"acceptance run command failed to start: {e}") from e

    tests = parse_test_output(proc.stdout)
    return DriverResult(
        exit_code=proc.returncode,
        tests=tests,
        raw_output=(proc.stdout or "") + (proc.stderr or ""),
    )


def _run_cli_pytest(run_command: str, cwd: str, *, timeout: int) -> DriverResult:
    """The ``cli-pytest`` shape: append pytest's own built-in
    ``--junit-xml=<path>`` (a core pytest flag — no extra plugin required in
    the driven repo) so structured per-test verdicts are always produced
    regardless of what *run_command* itself prints, then parse that XML
    report with :func:`parse_pytest_junit_xml`.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        report_path = Path(tmp_dir) / "coord-acceptance-junit.xml"
        full_command = f"{run_command} --junit-xml={report_path}"
        try:
            proc = subprocess.run(
                full_command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise DriverError(
                f"acceptance run command timed out after {timeout}s: {full_command!r}"
            ) from e
        except OSError as e:
            raise DriverError(f"acceptance run command failed to start: {e}") from e

        report_text = report_path.read_text() if report_path.exists() else ""
        tests = parse_pytest_junit_xml(report_text)
        return DriverResult(
            exit_code=proc.returncode,
            tests=tests,
            raw_output=(proc.stdout or "") + (proc.stderr or ""),
        )


def _run_web_playwright(run_command: str, cwd: str, *, timeout: int) -> DriverResult:
    """The ``web-playwright`` shape: force Playwright Test's built-in
    ``json`` reporter to a known path via ``--reporter=json`` plus the
    ``PLAYWRIGHT_JSON_OUTPUT_FILE`` env var it honors (the json-reporter
    twin of the documented ``PLAYWRIGHT_JUNIT_OUTPUT_NAME``) — so a
    structured report is always produced at a path we control regardless of
    what reporters the driven repo's own ``playwright.config.ts`` declares,
    the same trick :func:`_run_cli_pytest` plays with ``--junit-xml``.

    Unlike ``_run_cli_pytest`` (which treats "no report file" as a benign
    zero-tests result — see its own crash test), a missing or corrupt
    report here always raises :class:`DriverError`. Per #1539: "a crashed
    run ... must surface as a DriverError or an explicit failure — never as
    an empty pass list." Playwright can die before the json reporter ever
    flushes (a bad config file, a browser that never launches, `--grep`
    matching nothing without `--pass-with-no-tests`) and an empty list from
    that is indistinguishable from "the suite legitimately has zero tests
    right now" — exactly the silent-green failure mode this driver must not
    produce.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        report_path = Path(tmp_dir) / "coord-acceptance-playwright.json"
        full_command = f"{run_command} --reporter=json"
        env = {**os.environ, "PLAYWRIGHT_JSON_OUTPUT_FILE": str(report_path)}
        try:
            proc = subprocess.run(
                full_command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise DriverError(
                f"acceptance run command timed out after {timeout}s: {full_command!r}"
            ) from e
        except OSError as e:
            raise DriverError(f"acceptance run command failed to start: {e}") from e

        if not report_path.exists():
            stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
            raise DriverError(
                f"web-playwright run wrote no report (exit {proc.returncode}): "
                f"{full_command!r}\n{stderr_tail}"
            )
        tests = parse_playwright_json_report(report_path.read_text())
        return DriverResult(
            exit_code=proc.returncode,
            tests=tests,
            raw_output=(proc.stdout or "") + (proc.stderr or ""),
        )


def parse_test_output(output: str) -> list[dict]:
    """Parse a driver's stdout into normalized ``{"id", "status", "message"}``.

    Two shapes are recognized:

    1. A single JSON blob whose whole stdout is one object of the form
       ``{"tests": [{"id": ..., "status": "pass"|"fail"|"skip", "message":
       ...}, ...]}`` — the direct contract for a driver that already speaks
       it natively.
    2. libtest's JSON-lines test-event stream (``cargo test -- -Z
       unstable-options --format json``): one JSON object per line, only
       ``{"type": "test", "event": "ok"|"failed"|"ignored", "name": ...}``
       lines carry a verdict. Non-JSON lines (cargo build progress,
       warnings) and ``"type": "suite"``/``"type": "bench"`` lines are
       skipped.

    Unparsable input returns an empty list rather than raising — a failed
    parse is surfaced by the caller as "0 tests found", not a crash.
    """
    stripped = (output or "").strip()
    if stripped.startswith("{"):
        blob = _try_json(stripped)
        if isinstance(blob, dict) and isinstance(blob.get("tests"), list):
            tests: list[dict] = []
            for t in blob["tests"]:
                if not isinstance(t, dict) or "id" not in t or "status" not in t:
                    continue
                tests.append({
                    "id": str(t["id"]),
                    "status": str(t["status"]),
                    "message": str(t.get("message", "")),
                })
            return tests

    tests = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        obj = _try_json(line)
        if not isinstance(obj, dict) or obj.get("type") != "test":
            continue
        event = obj.get("event")
        name = obj.get("name")
        if not name or event not in _LIBTEST_EVENT_STATUS:
            continue
        entry = {"id": str(name), "status": _LIBTEST_EVENT_STATUS[event], "message": ""}
        stdout_msg = obj.get("stdout")
        if stdout_msg:
            entry["message"] = str(stdout_msg)
        tests.append(entry)
    return tests


def parse_pytest_junit_xml(xml_text: str) -> list[dict]:
    """Parse pytest's built-in ``--junit-xml=<path>`` report (a core pytest
    flag — no extra plugin required in the driven repo, unlike
    ``pytest-json-report``/``pytest-reportlog``) into normalized ``{"id",
    "status", "message", "expected", "got"}`` dicts — the same ``id``/
    ``status`` shape :func:`parse_test_output` returns for
    ``tui-tuidriver``, so :func:`coord.acceptance.build_verdict` /
    ``_scoped_verdict`` / :func:`coord.acceptance.load_manifest` work
    unchanged regardless of which driver kind produced the verdicts.

    Each ``<testcase classname="..." name="...">`` becomes one entry with
    ``id = "{classname}::{name}"``. A ``<failure>`` or ``<error>`` child
    means ``"fail"``; a ``<skipped>`` child means ``"skip"``; otherwise
    ``"pass"``. ``"expected"``/``"got"`` are populated only for a failing
    test, and only when the failure's ``message`` attribute's first line is
    pytest's own plain ``assert <got> == <expected>`` rendering (the shape a
    cli-pytest test comparing actual CLI stdout to a ``*.out`` mock
    produces) — anything else (a raised exception, a multi-line diff with no
    single ``==``) leaves them empty rather than guessing.

    Unparsable / empty input returns an empty list rather than raising —
    mirrors :func:`parse_test_output`'s "0 tests found, not a crash"
    contract.
    """
    text = (xml_text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    tests = []
    for testcase in root.iter("testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        if not name:
            continue
        nodeid = f"{classname}::{name}" if classname else name

        failure = testcase.find("failure")
        if failure is None:
            failure = testcase.find("error")
        skipped = testcase.find("skipped")

        entry = {
            "id": nodeid, "status": "pass", "message": "",
            "expected": "", "got": "",
        }
        if failure is not None:
            entry["status"] = "fail"
            message = failure.get("message", "") or (failure.text or "")
            entry["message"] = message
            first_line = message.splitlines()[0] if message else ""
            m = _ASSERT_EQ_RE.search(first_line)
            if m:
                entry["got"] = m.group(1).strip()
                entry["expected"] = m.group(2).strip()
        elif skipped is not None:
            entry["status"] = "skip"
            entry["message"] = skipped.get("message", "") or (skipped.text or "")
        tests.append(entry)
    return tests


def parse_playwright_json_report(json_text: str) -> list[dict]:
    """Parse Playwright Test's built-in ``--reporter=json`` report (a core
    reporter, not a plugin — no extra npm dependency required in the driven
    repo) into normalized ``{"id", "status", "message"}`` dicts.

    **Why json, not junit** (Playwright ships both as built-ins): junit
    collapses every retry attempt of one test into a single ``<testcase>``
    with no per-attempt status — a test that failed once then passed on
    retry renders as a plain, silent pass, identical to a test that never
    failed at all. That's a real gap verified against Playwright 1.61
    output, not a hypothetical: see the recorded
    ``tests/fixtures/playwright/retry_then_pass.json`` fixture and its junit
    sibling this module's tests compare it against. Since #1539 explicitly
    calls out retries as signal ("flake is signal for this program"), junit
    can't carry the contract this driver needs. json's per-test ``results``
    array keeps one entry per attempt (with its own ``status`` and
    ``errors``), and its per-test ``projectName`` disambiguates the same
    test title run under multiple ``projects:`` entries — junit only
    distinguishes projects via a ``<testsuite hostname="...">`` attribute
    shared by every testcase in that project, not the id.

    **id shape**: ``"[{project}] {file} › {describe path} › {test title}"``
    — stable across reruns (no timestamps/durations), unique across
    multiple ``projects:`` in one config (see above), and matches the
    ``[project] › file:line › describe › title`` shape Playwright's own
    ``list`` reporter prints, so it's recognizable when cross-referencing a
    human's terminal output.

    **status**: taken from Playwright's own reconciled per-test ``status``
    (``"expected"``/``"flaky"``/``"unexpected"``/``"skipped"``) rather than
    re-deriving it from the raw per-attempt results — that field already
    accounts for retries *and* ``test.fail()``-style "expected to fail"
    annotations, so re-implementing it here would just be a worse copy.
    ``"expected"``/``"flaky"`` -> ``"pass"`` (flaky is still a pass for
    gating, but the message says so — see below), ``"unexpected"`` ->
    ``"fail"``, ``"skipped"`` (covers both ``test.skip()`` and
    ``test.fixme()``) -> ``"skip"``.

    **message**: the last attempt's error text for a ``"fail"``; for a
    ``"flaky"`` pass, a summary noting the flake plus the first failed
    attempt's error (the signal #1539 asks this driver to preserve); the
    annotation ``description`` (the ``fixme`` reason, when given) for a
    ``"skip"``; empty for a clean pass. Error text has Playwright's
    baked-in ANSI color codes stripped (see :data:`_ANSI_RE` — verified
    these survive even with ``NO_COLOR``/piped-non-tty stdout, so stripping
    is mandatory, not a courtesy).

    **Never silently empty on a crash** — unlike this module's other parse_*
    functions, which return ``[]`` on unparsable input to keep "0 tests
    found" from ever raising. #1539 requires the opposite here: "a crashed
    run ... must surface as a DriverError ... never as an empty pass list."
    So this raises :class:`DriverError` for: empty/whitespace-only input (a
    truncated-to-nothing or never-written report); invalid JSON (a report
    cut off mid-write, e.g. the process was killed before flushing —
    ``tests/fixtures/playwright/truncated.json`` is a real report truncated
    this way); a JSON body missing the ``"suites"`` list (wrong shape
    entirely); and zero tests parsed *while Playwright's own top-level
    ``"errors"`` is non-empty* — verified empirically to be exactly how a
    thrown ``globalSetup`` hook or a ``--grep`` matching nothing report
    (``tests/fixtures/playwright/global_setup_crash.json``): a
    well-formed, zero-test report that must not be mistaken for "the suite
    is just empty right now". Zero tests with an empty top-level
    ``"errors"`` (Playwright's own ``--pass-with-no-tests`` opt-in) is left
    as a plain ``[]`` — callers (:func:`coord.acceptance.build_verdict`)
    already treat a zero-test list as not-green rather than a false "all
    green", which is the actual guarantee #1539 is protecting.
    """
    text = (json_text or "").strip()
    if not text:
        raise DriverError(
            "web-playwright report is empty — the run crashed before "
            "writing a report"
        )
    try:
        report = json.loads(text)
    except json.JSONDecodeError as e:
        raise DriverError(
            f"web-playwright report is not valid JSON (truncated or "
            f"corrupted run?): {e}"
        ) from e
    if not isinstance(report, dict) or not isinstance(report.get("suites"), list):
        raise DriverError(
            "web-playwright report has an unrecognized shape (missing a "
            "'suites' list) — this reporter version may be incompatible"
        )

    tests: list[dict] = []
    for suite in report["suites"]:
        if isinstance(suite, dict):
            tests.extend(_playwright_specs(suite, []))

    if not tests and report.get("errors"):
        first = report["errors"][0] if isinstance(report["errors"], list) else report["errors"]
        detail = first.get("message", "") if isinstance(first, dict) else str(first)
        raise DriverError(
            f"web-playwright run produced zero tests and reported a "
            f"top-level error (bad config, browser launch failure, or a "
            f"run: command matching no tests): {_strip_ansi(detail)}"
        )
    return tests


def _playwright_specs(suite: dict, ancestors: list[str]) -> list[dict]:
    """Recursively walk one Playwright json-report suite tree, returning one
    normalized dict per ``(spec, project)`` pair.

    A suite nests: the outermost suite per spec file (``title == file``,
    skipped from the id's describe-path since it's redundant with the
    ``file`` already in the id), then one nested suite per ``describe()``
    block, down to leaf ``specs`` (one per ``test()``/``it()``).
    """
    title = suite.get("title", "")
    is_file_suite = bool(suite.get("file")) and title == suite.get("file")
    path = ancestors if is_file_suite else [*ancestors, title]

    tests: list[dict] = []
    for spec in suite.get("specs") or []:
        if isinstance(spec, dict):
            tests.extend(_playwright_spec_entries(spec, path))
    for sub in suite.get("suites") or []:
        if isinstance(sub, dict):
            tests.extend(_playwright_specs(sub, path))
    return tests


def _playwright_spec_entries(spec: dict, ancestors: list[str]) -> list[dict]:
    """One normalized entry per project a leaf ``spec`` (a single
    ``test()``) ran under."""
    spec_title = spec.get("title", "")
    title_path = " › ".join([*ancestors, spec_title]) if spec_title else " › ".join(ancestors)
    file = spec.get("file", "")

    entries = []
    for t in spec.get("tests") or []:
        if not isinstance(t, dict):
            continue
        project = t.get("projectName", "")
        nodeid = f"[{project}] {file} › {title_path}" if project else f"{file} › {title_path}"
        raw_status = t.get("status")
        status = _PLAYWRIGHT_STATUS.get(raw_status, "fail")
        results = t.get("results") or []

        message = ""
        if status == "fail":
            message = _playwright_error_text(results[-1]) if results else ""
        elif raw_status == "flaky":
            failed = [r for r in results if r.get("status") not in ("passed", "skipped")]
            first_failure = _playwright_error_text(failed[0]) if failed else ""
            message = f"flaky: passed after {len(failed)} failed attempt(s)"
            if first_failure:
                message += f" — first failure: {first_failure}"
        elif status == "skip":
            message = _playwright_skip_reason(t.get("annotations") or [])

        entries.append({"id": nodeid, "status": status, "message": message})
    return entries


def _playwright_error_text(result: dict) -> str:
    """The (ANSI-stripped) error message(s) of one ``results[]`` attempt."""
    if not isinstance(result, dict):
        return ""
    errors = result.get("errors") or []
    parts = [str(e.get("message", "")) for e in errors if isinstance(e, dict) and e.get("message")]
    return _strip_ansi("\n".join(parts))


def _playwright_skip_reason(annotations: list) -> str:
    """The ``fixme``/``skip`` annotation's ``description`` (the reason
    string a caller passed, e.g. ``test.fixme(true, "blocked on #1541")``),
    or ``""`` when a bare ``test.skip()``/``.skip(true)`` gave no reason —
    mirrors :func:`parse_pytest_junit_xml`'s "empty message when no reason
    given" convention.
    """
    for a in annotations:
        if isinstance(a, dict) and a.get("type") in ("skip", "fixme") and a.get("description"):
            return str(a["description"])
    return ""


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _try_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
