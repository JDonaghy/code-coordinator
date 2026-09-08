"""Declared, machine-checkable UAT assertions (#3198).

The UAT gate (`coord.merge_queue.requires_uat`/`evaluate_uat_verdict`,
#2687/#2948) previously had exactly two ways to be satisfied: an operator
runs `coord uat <id> --passed`, or a customer approves the preview in the
portal (#3188). Both are humans, so a repo that opts in gets a human gate on
EVERY issue forever -- including a slice with no user-visible surface at
all (the motivating case: `format-converter#2`, a build scaffold whose
preview was a placeholder page; every assertion an operator was asked to
confirm -- 200 with no Cloudflare Access redirect, a `content-security-
policy` header, the page rendering -- was answerable with `curl`).

This module is the third path: a small, deliberately non-Turing-complete
vocabulary of assertions a repo (or one issue) can declare in
`coordinator.yml`, run automatically against the SAME preview URL
`coord.merge_queue._resolve_uat_preview_url` already resolves. On an
all-pass verdict the caller (`coord.merge_queue`) records the exact same
`uat_state`/`uat_reason` `coord uat --passed` would have written, attributed
to `actor="checker"` instead of a person -- so
`coord.merge_queue.evaluate_uat_verdict` needs no changes at all: every path
that can satisfy the UAT gate converges on the one representation it
already reads (#2096, "one question, one answer").

The boundary that keeps this from becoming "more tests" (and therefore
belonging in CI, not here): a check declared here may only assert things
that exist ONLY after deployment -- is the preview reachable, is it gated,
does the edge actually serve the headers it's configured to. Anything
checkable from the repo alone at build time belongs in CI, where it is
cheaper and earlier. `expected_status`/`headers_present`/`headers_absent`/
`body_contains` against one live HTTP response is deliberately the entire
vocabulary: no loops, no variables, nothing composable enough to grow into
a test suite -- if it needs more than that, it IS a test suite, and test
suites belong in the repo.

`coord.config` parses the `uat_checks:` YAML block into `UatCheckConfig`
(repo-wide `UatChecks`, an `exempt` issue-number list, and optional
per-issue `UatChecks` overrides) and hangs it off `Repo.uat_checks`.
`coord.merge_queue` is the only caller of `evaluate_uat_checks` -- see its
`_run_declared_uat_checks`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

#: Default per-request timeout for a declared-checks fetch (seconds). Kept
#: short and non-configurable-per-repo on purpose -- a UAT check that hangs
#: should fail fast into "block as today", not stall a merge attempt.
DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True)
class HeaderAssertion:
    """One `headers_present` entry: a header that must appear in the
    preview response, optionally with a required substring in its value.

    `contains=None` means "the header need only be present, any value" --
    the common case for a boolean marker header (e.g. a security header
    whose mere presence is the thing being confirmed). `contains` set means
    the header's value must contain that exact substring -- e.g.
    `content-security-policy` must contain `connect-src 'none'`.
    """

    name: str
    contains: str | None = None


@dataclass(frozen=True)
class UatChecks:
    """One declared-checks assertion set -- a repo-wide default or a
    per-issue override (see `UatCheckConfig`).

    Deliberately NOT a scripting language: no loops, no variables, just a
    fixed set of assertions evaluated against one HTTP response.
    """

    expected_status: int | None = None
    headers_present: tuple[HeaderAssertion, ...] = ()
    headers_absent: tuple[str, ...] = ()
    body_contains: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        """True when this assertion set declares nothing to check.

        The #3198 safe default lives here: "no declared checks" (an unset
        `uat_checks:` block, or one that parses to an empty assertion set)
        means today's behaviour -- human required -- never a silent
        auto-pass. `UatCheckConfig.resolve_for_issue` treats an empty
        `UatChecks` identically to "nothing declared at all".
        """
        return (
            self.expected_status is None
            and not self.headers_present
            and not self.headers_absent
            and not self.body_contains
        )


@dataclass(frozen=True)
class UatCheckConfig:
    """A repo's full declared-checks configuration (#3198): the repo-wide
    default, per-issue overrides, and the per-issue exemption list.

    `exempt_issues` mirrors `tests/acceptance/ms-NN/manifest.yml`'s
    `exempt:` list (#1138) -- the same shape already used to excuse an
    issue with no user-visible surface from the acceptance-slice gate,
    reused here for the identical shape of problem rather than inventing a
    second, differently-shaped mechanism. An exempt issue needs neither a
    passing check NOR a human verdict: `coord.merge_queue.requires_uat`
    treats it as not requiring the UAT gate at all.
    """

    checks: UatChecks = field(default_factory=UatChecks)
    issue_checks: dict[int, UatChecks] = field(default_factory=dict)
    exempt_issues: frozenset[int] = frozenset()

    def is_exempt(self, issue_number: int | None) -> bool:
        return issue_number is not None and issue_number in self.exempt_issues

    def resolve_for_issue(self, issue_number: int | None) -> UatChecks | None:
        """The assertion set to attempt for *issue_number*, or `None` when
        there is nothing to run.

        `None` covers both "this issue is exempt" and "nothing declared" --
        callers must treat both identically (no autopass attempt, gate
        stays as it was), never confuse "exempt" with "an empty check that
        trivially passes": exemption skips the gate entirely, it does not
        fabricate a passing verdict.
        """
        if self.is_exempt(issue_number):
            return None
        if issue_number is not None:
            override = self.issue_checks.get(issue_number)
            if override is not None and not override.is_empty():
                return override
        return None if self.checks.is_empty() else self.checks


@dataclass(frozen=True)
class UatCheckResult:
    """Outcome of running one `UatChecks` against a live preview URL.

    `ok=False` always carries `failing` -- the name of the one assertion
    that stopped evaluation (#2096: a gate must be able to name why it
    failed, not just that it did) -- and a human-readable `summary` a
    caller can fold straight into a merge-block message. `ok=True` carries
    the same evidence in `summary`/`evidence`, for the `uat_reason` written
    alongside the auto-recorded verdict.
    """

    ok: bool
    summary: str
    evidence: tuple[str, ...] = ()
    failing: str | None = None


#: A callable taking the preview URL and returning an `httpx.Response`-shaped
#: object. Quoted so the alias resolves to a `ForwardRef` at runtime instead of
#: touching the (deliberately lazily imported) `httpx` module -- see the
#: import-cost note on `_default_fetcher`.
Fetcher = Callable[[str], "httpx.Response"]


def evaluate_uat_checks(
    url: str,
    checks: UatChecks,
    *,
    fetch: Fetcher | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> UatCheckResult:
    """Fetch *url* once and evaluate *checks* against the response.

    Assertions are evaluated in a fixed order -- status, then
    `headers_present`, then `headers_absent`, then `body_contains` -- and
    evaluation stops at the first failure, which becomes `failing`/
    `summary`. This never raises: a transport failure (timeout, DNS, TLS,
    connection refused) is reported as an ordinary failing result naming
    the fetch itself as the failing assertion (#2096 -- "the network was
    down" is not license to treat an unconfirmed check as a pass), and
    `follow_redirects=False` by default so a redirect to a sign-in page
    (the exact no-sign-in case #3198 exists to catch) shows up as a status
    mismatch rather than being silently followed through to a 200 on the
    login page.

    *fetch* is an injection point for tests -- a callable taking the URL and
    returning an `httpx.Response`-shaped object -- so a check can be
    evaluated with zero real network I/O.
    """
    # `httpx` is imported HERE, not at module scope, on purpose. This module
    # is reached from `coord.models` -> `coord.config`, and config *parsing*
    # runs on a bare `python3` with only this checkout on `sys.path` in the
    # epic-up/epic-down remote registration block (see
    # tests/test_epic_up_down_symlinked_config_1887.py) -- a module-scope
    # `import httpx` there turns "validate the YAML I just wrote" into a
    # `ModuleNotFoundError`. Actually running a check is a network operation
    # and always happens inside a full install, so the cost lands where the
    # dependency is genuinely needed. Same pattern as `coord.board_service` /
    # `coord.progress`.
    import httpx  # noqa: PLC0415 — keep config-only import paths httpx-free

    getter = fetch or (
        lambda u: httpx.get(u, timeout=timeout, follow_redirects=False)
    )
    try:
        response = getter(url)
    except httpx.HTTPError as exc:
        return UatCheckResult(
            ok=False,
            summary=f"could not fetch {url}: {exc}",
            failing=f"fetch {url}",
        )

    evidence: list[str] = [f"GET {url} -> {response.status_code}"]

    if (
        checks.expected_status is not None
        and response.status_code != checks.expected_status
    ):
        return UatCheckResult(
            ok=False,
            summary=(
                f"expected HTTP {checks.expected_status}, got "
                f"{response.status_code}"
            ),
            evidence=tuple(evidence),
            failing=f"expected_status={checks.expected_status}",
        )

    headers = response.headers  # case-insensitive lookup (httpx.Headers)

    for assertion in checks.headers_present:
        value = headers.get(assertion.name)
        if value is None:
            return UatCheckResult(
                ok=False,
                summary=f"header {assertion.name!r} missing",
                evidence=tuple(evidence),
                failing=f"headers_present: {assertion.name}",
            )
        if assertion.contains is not None and assertion.contains not in value:
            return UatCheckResult(
                ok=False,
                summary=(
                    f"header {assertion.name!r} present but does not "
                    f"contain {assertion.contains!r} (got {value!r})"
                ),
                evidence=tuple(evidence),
                failing=(
                    f"headers_present: {assertion.name}: {assertion.contains}"
                ),
            )
        evidence.append(
            f"header {assertion.name}: present"
            + (f" (contains {assertion.contains!r})" if assertion.contains else "")
        )

    for name in checks.headers_absent:
        if headers.get(name) is not None:
            return UatCheckResult(
                ok=False,
                summary=f"header {name!r} present but must be absent",
                evidence=tuple(evidence),
                failing=f"headers_absent: {name}",
            )
        evidence.append(f"header {name}: absent (as required)")

    if checks.body_contains:
        body = response.text
        for needle in checks.body_contains:
            if needle not in body:
                return UatCheckResult(
                    ok=False,
                    summary=f"response body does not contain {needle!r}",
                    evidence=tuple(evidence),
                    failing=f"body_contains: {needle}",
                )
            evidence.append(f"body contains {needle!r}")

    return UatCheckResult(ok=True, summary="; ".join(evidence), evidence=tuple(evidence))
