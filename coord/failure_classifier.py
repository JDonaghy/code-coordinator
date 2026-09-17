"""#3360: classify a failed leg's failure text before deciding whether to
escalate the model on retry.

The model-escalation ladder (``ModelsConfig.escalation`` / ``next_model`` in
``coord/config.py``) used to climb on ANY failed leg — every automatic
fix-dispatch door treated a red leg as "the model was not strong enough" and
reached for a pricier one. That is the wrong move for a COMPLIANCE failure: a
ratchet, a lint/formatter check, a ``files_forbidden``/sealed-path violation.
No amount of model capability lets a worker guess a repo-specific fact it was
never told — opus cannot infer a ratchet's pinned count any better than
sonnet can. #3357 paid for exactly this: a tripped ``sqlite3.connect`` ratchet
bought an opus worker that spun 25 turns and committed nothing.

NOT ``coord/failure_class.py`` (#2096 disambiguation). That neighbouring
module also exports a ``classify_failure()`` returning a
``FailureClassification``, but it answers a DIFFERENT question —
"environmental (529 / usage limit / network) vs work" for resume scheduling
and the liveness gate — and nothing here duplicates it. The two are
orthogonal evidence lanes over the same ``failure_reason`` text: a leg can be
environmental *and* compliance-shaped, and each lane is consulted by its own
callers. Keep them separate; do not "unify" them into one verdict.

``classify_failure()`` is the ONE classifier every escalation-gated dispatch
door calls (#2096 "one question, one answer" — two independent
implementations of "is this failure a compliance nit or a real bug" would be
a split-brain waiting to happen). As of #3360 that is every known door:

- ``coord/commands/plan_followup.py``'s ``fix()`` — the Test/CI-failure arm.
- ``coord/commands/dispatch.py``'s ``retry()`` — via
  :func:`failure_text_for_assignment` below, since this door only has the
  board row, not an already-loaded test/review body.
- ``coord/auto_loop.py``'s ``_fix_model_for_iteration`` — the headless
  review→fix bounce reached from both ``coord notify``'s completion
  transition and ``coord fix``'s review-triggered arm
  (``_fix_from_review`` → ``process_review_completion`` →
  ``_dispatch_fix_for_review``), plus the dashboard's "unstick this row"
  button (``coord.review.dispatch_headless_fix``) and the human-attended
  ``coord fix`` CLI (``coord.commands.dispatch_workers``). All three thread
  the review/test findings text through as ``failure_text`` AND the rung the
  previous round actually dispatched at through as ``previous_model``
  (``coord.auto_loop.last_fix_model_for_branch``). Both halves are load
  bearing: gating only the last marginal step of a ladder replayed from the
  iteration counter still climbs unconditionally for every step before it,
  so the gate evaporated from round 3 onwards (#3360 round-2 review).
- ``coord/commands/plan_followup.py``'s ``resume_stuck()`` is the ONE
  exception, deliberately: it is the recovery path for a worker the
  stuck-detector already flagged (turns elapsed, no commit) — the issue's
  own "Spin" category, which is neither a capability nor a compliance
  question, so it never calls ``classify_failure()`` at all and never
  escalates; see the comment at its call site.

NOT an escalation-gated door, and therefore deliberately not a caller:
``coord/ci_fix.py``'s ``dispatch_ci_fix()``. It is the fourth automatic
fix-dispatch path, but it calls ``_dispatch_fix`` with no ``model=`` at all,
so it has never climbed the ladder and there is no escalation decision here
to gate. Flagged in the #3360 review only so the next reader can confirm the
"every escalation-gated door" claim above is exhaustive rather than an
oversight; wiring the classifier in would change nothing about which model
runs.

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

ACCEPTED RISK (flagged in #3360 review, not fixed — the direction is safe).
Two known false-positive shapes, both reviewed and accepted because they only
ever cause a WRONGLY-SKIPPED escalation, never a wrong one:

- A single failure text can carry BOTH a tripped ratchet and a genuine
  behavioural assertion failure; this classifies the whole blob as
  "compliance" and skips escalation for the real bug too. Cheap because the
  same-rung retry is one leg, and once the ratchet is fixed the genuine
  failure surfaces alone on the next iteration.
- A signature (e.g. ``ratchet``, ``clippy``) could in principle appear in an
  unrelated domain's own vocabulary (code that itself implements or tests
  something literally named "ratchet"). None of today's signatures look
  dangerously generic, but a false hit here still only means "wrongly skip
  escalation" — the safe-by-design direction per #3360's own cost model
  (a wrong same-rung retry costs one cheap leg; a wrong escalation costs the
  most expensive rung on the ladder, and #3357 shows that rung can still
  fail outright).
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
        # #3360 review: the inflection group must cover the PLURAL too —
        # `\bratchet\b` never fires on "ratchets" (the word boundary sits
        # between "t" and "s"), and real failure text says "2 ratchets
        # tripped" / "these ratchets" often enough to matter.
        re.compile(r"_ratchet\.py|\bratchet(?:e[sd]|ing|s)?\b", re.IGNORECASE),
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


def failure_text_for_assignment(assignment) -> str:  # noqa: ANN001
    """Best-effort failure text for a board ``Assignment``, for feeding
    :func:`classify_failure`.

    A door that only has the board row (e.g. ``coord retry``, dispatching a
    failed/advisory WORK-like assignment with no CLI-supplied CI story or
    already-loaded review findings) still needs SOME text to classify —
    otherwise it can only ever see ``"unknown"`` and never detect a
    compliance signature at all. This concatenates every reason field the
    board might have recorded, so "which field wins" has one answer (#2096)
    instead of a per-door dialect:

    - ``failure_reason`` — the worker/dispatch-level failure summary.
    - the fuller test-reason text via ``load_assignment_test_reason``
      (falls back to the board-carried ``test_reason``/``smoke_test_reason``
      previews — #1337).
    - ``acceptance_reason`` (#2344) / ``uat_reason`` (#3208) — trust-gate
      verdicts.

    Classification only needs ONE field to carry a compliance signature to
    skip escalation, so concatenating costs nothing: a field that doesn't
    apply to this assignment is simply empty and contributes nothing to the
    scan.
    """
    from coord.state import load_assignment_test_reason  # noqa: PLC0415

    assignment_id = getattr(assignment, "assignment_id", None)
    parts = [
        getattr(assignment, "failure_reason", None),
        load_assignment_test_reason(assignment_id) if assignment_id else None,
        getattr(assignment, "test_reason", None),
        getattr(assignment, "smoke_test_reason", None),
        getattr(assignment, "acceptance_reason", None),
        getattr(assignment, "uat_reason", None),
    ]
    return "\n".join(p for p in parts if p)
