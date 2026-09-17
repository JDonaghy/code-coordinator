"""#3360: classify a failed leg's failure text before deciding whether to
escalate the model on retry.

The model-escalation ladder (``ModelsConfig.escalation`` / ``next_model`` in
``coord/config.py``) climbs on ANY failed leg today — every automatic
fix-dispatch door (``coord/commands/plan_followup.py``'s ``fix()``, the
Test/CI-failure arm) treats a red leg as "the model was not strong enough"
and reaches for a pricier one. That is the wrong move for a COMPLIANCE
failure: a ratchet, a lint/formatter check, a ``files_forbidden``/sealed-path
violation. No amount of model capability lets a worker guess a repo-specific
fact it was never told — opus cannot infer a ratchet's pinned count any
better than sonnet can. #3357 paid for exactly this: a tripped
``sqlite3.connect`` ratchet bought an opus worker that spun 25 turns and
committed nothing.

``classify_failure()`` is the ONE classifier every escalation-gated dispatch
door calls (#2096 "one question, one answer" — two independent
implementations of "is this failure a compliance nit or a real bug" would be
a split-brain waiting to happen).

Classification
--------------
- ``"compliance"`` — the failure text matches a known repo-policy signature
  (a ratchet test, a lint/formatter check, a ``files_forbidden``/sealed-path
  violation). ``should_escalate`` is ``False``: re-dispatch at the SAME model
  rung, with the failure text folded into the brief so the worker learns the
  fact it was missing.
- ``"capability"`` — failure text is present and matches none of the known
  compliance signatures, i.e. it reads like an ordinary behavioural
  assertion failure. ``should_escalate`` is ``True`` — climb the ladder, same
  as today.
- ``"unknown"`` — there is no failure text to classify at all (empty,
  whitespace-only, or the door was reached with no recorded verdict — e.g. a
  ``--force`` dispatch with no test/CI/acceptance evidence). #3360's
  acceptance bar is explicit here: **default to NOT escalating**. The cost of
  a wrongly-skipped escalation is one cheap same-rung retry; the cost of a
  wrong escalation is the most expensive rung on the ladder — and #3357
  shows that rung can still fail outright (opus, 25 turns, 0 commits).

This is deliberately the "cheap first cut" the issue calls out as sufficient
to have kept #3357 on sonnet: classify by known compliance signatures, and
default everything else (including "no evidence") away from escalation
rather than assuming capability difficulty. It does not attempt full natural
-language failure-cause classification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Each entry is (signature name, compiled pattern). Matched
#: case-insensitively against the full failure text (test output, CI story,
#: acceptance/UAT reason — whatever a caller has on hand). First match wins;
#: the name is reported so callers/logs/PRs can say which classifier fired.
_COMPLIANCE_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ratchet",
        # Matches the repo's own ratchet-test naming convention
        # (`tests/test_*_ratchet.py`, e.g. #3357's
        # `test_sqlite_connect_ratchet.py`) as well as the bare word, which
        # is rare enough outside that context to be a safe signal.
        re.compile(r"_ratchet\.py|\bratchet(?:ed|ing)?\b", re.IGNORECASE),
    ),
    (
        "lint-or-format",
        re.compile(
            r"\bruff\b|\bflake8\b|\bmypy\b|\bpylint\b|\bisort\b|\beslint\b|"
            r"\bshellcheck\b|\bclippy\b|\brustfmt\b|\bcargo fmt\b|"
            r"black would reformat|would reformat \d+ file",
            re.IGNORECASE,
        ),
    ),
    (
        "files-forbidden-or-sealed",
        re.compile(
            r"files_forbidden|sealed[_ ]path|sealed suite|ownership boundary",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class FailureClassification:
    """The verdict :func:`classify_failure` renders on one failure text."""

    #: ``"compliance"`` | ``"capability"`` | ``"unknown"``
    category: str
    #: Whether the caller should climb the model-escalation ladder.
    should_escalate: bool
    #: The signature name that matched (see ``_COMPLIANCE_SIGNATURES``), or
    #: ``None`` for ``"capability"``/``"unknown"``.
    matched: str | None
    #: Human-readable justification, safe to surface in CLI output or a
    #: worker briefing.
    reason: str


def classify_failure(text: str | None) -> FailureClassification:
    """Classify a failed leg's failure text for the escalation decision.

    Pure function — no board/config/network reads — so every dispatch door
    that gates escalation on it stays unit-testable without a live board.
    See the module docstring for the three categories and their
    ``should_escalate`` defaults.
    """
    stripped = (text or "").strip()
    if not stripped:
        return FailureClassification(
            category="unknown",
            should_escalate=False,
            matched=None,
            reason=(
                "no failure text to classify (#3360) — defaulting to NOT "
                "escalating: a wrong same-rung retry costs one cheap leg, a "
                "wrong escalation costs the most expensive rung on the "
                "ladder"
            ),
        )
    for name, pattern in _COMPLIANCE_SIGNATURES:
        if pattern.search(stripped):
            return FailureClassification(
                category="compliance",
                should_escalate=False,
                matched=name,
                reason=(
                    f"failure text matches the {name!r} compliance signature "
                    "(#3360) — re-dispatching at the same model rung; no "
                    "model-capability difference fixes a repo-specific "
                    "policy fact the worker was never told"
                ),
            )
    return FailureClassification(
        category="capability",
        should_escalate=True,
        matched=None,
        reason=(
            "failure text does not match any known compliance signature "
            "(#3360) — treating it as a genuine behavioural failure and "
            "escalating the model rung"
        ),
    )
