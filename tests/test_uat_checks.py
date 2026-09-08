"""Tests for coord.uat_checks — the declared-checks UAT evaluator (#3198)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from coord.uat_checks import (
    HeaderAssertion,
    UatCheckConfig,
    UatChecks,
    evaluate_uat_checks,
)


def _response(
    *, status_code: int = 200, headers: dict | None = None, text: str = ""
) -> httpx.Response:
    return httpx.Response(status_code, headers=headers or {}, text=text)


# ── UatChecks.is_empty ───────────────────────────────────────────────────────


def test_is_empty_true_for_default() -> None:
    assert UatChecks().is_empty() is True


@pytest.mark.parametrize(
    "checks",
    [
        UatChecks(expected_status=200),
        UatChecks(headers_present=(HeaderAssertion(name="x"),)),
        UatChecks(headers_absent=("cf-access-jwt-assertion",)),
        UatChecks(body_contains=("hello",)),
    ],
)
def test_is_empty_false_when_any_assertion_set(checks: UatChecks) -> None:
    assert checks.is_empty() is False


# ── UatCheckConfig.resolve_for_issue ─────────────────────────────────────────


def test_resolve_for_issue_returns_none_when_nothing_declared() -> None:
    cfg = UatCheckConfig()
    assert cfg.resolve_for_issue(1) is None
    assert cfg.resolve_for_issue(None) is None


def test_resolve_for_issue_exempt_returns_none_even_with_a_base_check() -> None:
    cfg = UatCheckConfig(
        checks=UatChecks(expected_status=200), exempt_issues=frozenset({2}),
    )
    assert cfg.resolve_for_issue(2) is None
    assert cfg.resolve_for_issue(3) == UatChecks(expected_status=200)


def test_resolve_for_issue_per_issue_override_wins() -> None:
    override = UatChecks(body_contains=("New Feature",))
    cfg = UatCheckConfig(
        checks=UatChecks(expected_status=200), issue_checks={6: override},
    )
    assert cfg.resolve_for_issue(6) == override
    assert cfg.resolve_for_issue(7) == UatChecks(expected_status=200)


def test_resolve_for_issue_empty_override_falls_back_to_base() -> None:
    # An issue listed under `issues:` with nothing declared for it must not
    # be treated as "run nothing" (that's what `exempt` is for) — it falls
    # back to the repo-wide base check.
    cfg = UatCheckConfig(
        checks=UatChecks(expected_status=200), issue_checks={6: UatChecks()},
    )
    assert cfg.resolve_for_issue(6) == UatChecks(expected_status=200)


def test_resolve_for_issue_none_when_both_base_and_override_empty() -> None:
    cfg = UatCheckConfig(issue_checks={6: UatChecks()})
    assert cfg.resolve_for_issue(6) is None


def test_is_exempt() -> None:
    cfg = UatCheckConfig(exempt_issues=frozenset({2, 3}))
    assert cfg.is_exempt(2) is True
    assert cfg.is_exempt(4) is False
    assert cfg.is_exempt(None) is False


# ── evaluate_uat_checks: status ──────────────────────────────────────────────


def test_expected_status_pass() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(expected_status=200),
        fetch=lambda url: _response(status_code=200),
    )
    assert result.ok is True
    assert result.failing is None


def test_expected_status_fail_names_the_assertion() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(expected_status=200),
        fetch=lambda url: _response(status_code=302),
    )
    assert result.ok is False
    assert result.failing == "expected_status=200"
    assert "302" in result.summary


def test_no_expected_status_declared_never_blocks_on_status() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(body_contains=("hi",)),
        fetch=lambda url: _response(status_code=500, text="hi"),
    )
    assert result.ok is True


# ── evaluate_uat_checks: headers_present ─────────────────────────────────────


def test_headers_present_missing_header_fails() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(headers_present=(HeaderAssertion(name="x-frame-options"),)),
        fetch=lambda url: _response(headers={}),
    )
    assert result.ok is False
    assert result.failing == "headers_present: x-frame-options"


def test_headers_present_any_value_passes_when_header_present() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(headers_present=(HeaderAssertion(name="x-frame-options"),)),
        fetch=lambda url: _response(headers={"X-Frame-Options": "DENY"}),
    )
    assert result.ok is True


def test_headers_present_is_case_insensitive() -> None:
    # httpx.Headers is case-insensitive by construction; confirm the
    # evaluator relies on that rather than an exact-case dict lookup.
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(headers_present=(HeaderAssertion(name="Content-Security-Policy"),)),
        fetch=lambda url: _response(headers={"content-security-policy": "default-src 'self'"}),
    )
    assert result.ok is True


def test_headers_present_contains_substring_pass() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(
            headers_present=(
                HeaderAssertion(name="content-security-policy", contains="connect-src 'none'"),
            )
        ),
        fetch=lambda url: _response(
            headers={"content-security-policy": "default-src 'self'; connect-src 'none'"}
        ),
    )
    assert result.ok is True


def test_headers_present_contains_substring_mismatch_fails() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(
            headers_present=(
                HeaderAssertion(name="content-security-policy", contains="connect-src 'none'"),
            )
        ),
        fetch=lambda url: _response(headers={"content-security-policy": "default-src 'self'"}),
    )
    assert result.ok is False
    assert result.failing == "headers_present: content-security-policy: connect-src 'none'"


# ── evaluate_uat_checks: headers_absent (the no-sign-in check) ──────────────


def test_headers_absent_present_fails() -> None:
    # The motivating case: an Access-gated deployment answers with a
    # redirect and carries a JWT-assertion header the un-gated preview
    # never would.
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(headers_absent=("cf-access-jwt-assertion",)),
        fetch=lambda url: _response(headers={"cf-access-jwt-assertion": "opaque-token"}),
    )
    assert result.ok is False
    assert result.failing == "headers_absent: cf-access-jwt-assertion"


def test_headers_absent_missing_passes() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(headers_absent=("cf-access-jwt-assertion",)),
        fetch=lambda url: _response(headers={}),
    )
    assert result.ok is True


# ── evaluate_uat_checks: body_contains ───────────────────────────────────────


def test_body_contains_pass() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(body_contains=("Format Converter",)),
        fetch=lambda url: _response(text="<html>Format Converter</html>"),
    )
    assert result.ok is True


def test_body_contains_fail_names_the_needle() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(body_contains=("Format Converter",)),
        fetch=lambda url: _response(text="<html>placeholder</html>"),
    )
    assert result.ok is False
    assert result.failing == "body_contains: Format Converter"


def test_body_contains_multiple_needles_all_must_match() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(body_contains=("alpha", "beta")),
        fetch=lambda url: _response(text="alpha only"),
    )
    assert result.ok is False
    assert result.failing == "body_contains: beta"


# ── evaluate_uat_checks: combined / evidence / evaluation order ────────────


def test_all_assertions_pass_returns_ok_with_evidence() -> None:
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(
            expected_status=200,
            headers_present=(HeaderAssertion(name="x-frame-options"),),
            headers_absent=("cf-access-jwt-assertion",),
            body_contains=("hello",),
        ),
        fetch=lambda url: _response(
            status_code=200,
            headers={"X-Frame-Options": "DENY"},
            text="hello world",
        ),
    )
    assert result.ok is True
    assert result.failing is None
    assert result.evidence  # some evidence recorded
    assert "200" in result.summary


def test_empty_checks_always_passes_vacuously() -> None:
    # Never called by coord.merge_queue (it checks `is_empty()` first and
    # skips the fetch entirely), but the evaluator itself must not invent a
    # failure out of nothing declared.
    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(),
        fetch=lambda url: _response(status_code=500),
    )
    assert result.ok is True


def test_status_checked_before_headers() -> None:
    # A redirect to a sign-in page is exactly the case a wrong status must
    # catch before any header assertion gets a chance to pass/fail on the
    # login page's own (irrelevant) headers.
    calls = []

    def fetch(url):
        calls.append(url)
        return _response(status_code=302, headers={"location": "https://sso.example/"})

    result = evaluate_uat_checks(
        "https://preview.example/",
        UatChecks(expected_status=200, headers_present=(HeaderAssertion(name="x-frame-options"),)),
        fetch=fetch,
    )
    assert result.ok is False
    assert result.failing == "expected_status=200"


# ── evaluate_uat_checks: fetch failure (#2096 — a gate must be able to fail) ─


def test_fetch_failure_is_a_failing_result_not_an_exception() -> None:
    def fetch(url):
        raise httpx.ConnectError("connection refused")

    result = evaluate_uat_checks(
        "https://preview.example/", UatChecks(expected_status=200), fetch=fetch,
    )
    assert result.ok is False
    assert "connection refused" in result.summary
    assert result.failing is not None and "fetch" in result.failing


def test_default_fetch_uses_httpx_get_without_following_redirects(monkeypatch) -> None:
    # The no-sign-in check depends on a redirect surfacing as a status
    # mismatch, not being silently followed to a 200 on the login page.
    captured = {}

    def fake_get(url, *, timeout, follow_redirects):
        captured["url"] = url
        captured["follow_redirects"] = follow_redirects
        return _response(status_code=200)

    monkeypatch.setattr(httpx, "get", fake_get)
    result = evaluate_uat_checks("https://preview.example/", UatChecks(expected_status=200))
    assert result.ok is True
    assert captured["url"] == "https://preview.example/"
    assert captured["follow_redirects"] is False


# ── the config-only import path must stay httpx-free ────────────────────────
#
# `coord.config` -> `coord.models` -> `coord.uat_checks`. Config *parsing*
# runs under a bare `python3` in the epic-up/epic-down remote registration
# block (tests/test_epic_up_down_symlinked_config_1887.py), where httpx is
# not importable — a module-scope `import httpx` here broke it outright. The
# checks below run in a SUBPROCESS: by the time this file's body executes,
# pytest collection has already imported httpx into this interpreter, so a
# same-process blocker would never be consulted.

_REPO_ROOT = Path(__file__).resolve().parents[1]

_BLOCK_HTTPX = """
import sys, importlib.abc

class _BlockHttpx(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split('.')[0] == 'httpx':
            raise ModuleNotFoundError("No module named 'httpx'", name='httpx')
        return None

sys.meta_path.insert(0, _BlockHttpx())
"""


def _run_without_httpx(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_HTTPX + script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_REPO_ROOT),
        env=dict(os.environ),
    )


def test_the_blocker_itself_bites() -> None:
    """Guard the guard: if the blocker silently stopped working, the two
    tests below would pass no matter what `coord.uat_checks` imports."""
    result = _run_without_httpx("import httpx")
    assert result.returncode != 0
    assert "No module named 'httpx'" in result.stderr


def test_importing_uat_checks_does_not_need_httpx() -> None:
    result = _run_without_httpx(
        "import coord.uat_checks as m\n"
        "import sys\n"
        "assert 'httpx' not in sys.modules, 'httpx was imported as a side effect'\n"
        "assert m.UatChecks(expected_status=200).is_empty() is False\n"
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


def test_loading_config_does_not_need_httpx(tmp_path: Path) -> None:
    """The end-to-end shape of the regression: validate a real
    coordinator.yml with httpx absent, exactly as the daemon-host
    registration block does."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: demo\n"
        "    github: acme/demo\n"
        "machines:\n"
        "  - name: box\n"
        "    host: box\n"
        "    repos: [demo]\n",
        encoding="utf-8",
    )
    result = _run_without_httpx(
        "import sys\n"
        "from coord.config import load\n"
        f"cfg = load({str(cfg)!r})\n"
        "assert [r.name for r in cfg.repos] == ['demo']\n"
        "assert 'httpx' not in sys.modules, 'httpx was imported as a side effect'\n"
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
