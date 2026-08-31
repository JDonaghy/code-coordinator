"""Independent, out-of-band confirmation of a Test-stage PASS claim (#2464).

THE DEFECT
----------
The Test stage's verdict was, end to end, *the worker's own report about its
own work*. Both channels are self-reports:

* the printed marker line — `SMOKE: pass` — which :mod:`coord.smoke`'s system
  prompt explicitly elevates above the exit code ("THE VERDICT IS THE LINE YOU
  PRINT, NOT YOUR EXIT CODE", #2244), because for a `claude -p` worker the exit
  code genuinely is not a signal: the session exits 0 whenever it ends normally
  no matter what the suite did;
* the worker calling ``coord test --passed <parent>`` itself (#2217), which
  :func:`coord.notify._record_smoke_verdict` treats as *authoritative* and
  refuses to clobber.

Neither involves anybody observing a test run. #2096 calls this **shape 1,
unconfirmed success**: the pipeline records the outcome of a *claim*, not of an
observation. It has already fired for real — assignment ``8de33c80fcd0`` ran the
full suite, hit 5 real failures, printed ``SMOKE: fail``, and was recorded
``test_state=passed`` anyway; CI found the identical five and blocked the merge
(#2230). #2244 fixed that *specific* accident by parsing the marker, but the
mechanism underneath stayed "trust text the LLM chose to print" — and the
recurring warnings in :mod:`coord.smoke` about partial and backgrounded runs
(#2244/#2272/#2301) are that same shape recurring with no adversarial intent at
all.

THE FIX
-------
:func:`confirm_branch` re-runs the repo's own ``build_command`` /
``test_command`` in a throwaway worktree at ``origin/<branch>`` and reads the
**real exit code**. No LLM, no worker, no tokens — the same mechanical
primitive :mod:`coord.revalidate` already uses for ``coord merge
--revalidate``, which is why this module reuses that module's helpers wholesale
rather than growing a second copy of them. The difference is *where it is
wired*: ``--revalidate`` is opt-in and only ever re-confirms an
**already-passed** verdict at merge time, so it never guarded the initial Test
verdict. This runs before ``test_state`` is recorded at all.

PROVIDER-AGNOSTIC BY CONSTRUCTION
---------------------------------
This runs in the reap path, *outside* whichever provider's process produced the
claim. That is deliberate and is the whole reason it is wired here rather than
in a provider: neither the Claude session's completion signal nor opencode's own
structured verdict (:mod:`coord.providers.opencode` — a stronger signal than a
free-text marker line, but still fundamentally the worker grading itself) is
independently trustworthy. One check placed after both covers both, and covers
any provider added later, with no per-backend logic.

WHAT IT CONFIRMS, AND WHAT IT DELIBERATELY DOES NOT
---------------------------------------------------
**Only PASS claims.** A ``fail`` verdict is already fail-closed, and ``blocked``
/ mute-leg rows already park without merging; spending a full suite run to
confirm bad news costs minutes and changes no gate. The laundering direction —
a *pass* that was never earned — is the one that reaches `main`.

**Not baseline-red.** A ``SMOKE: baseline-red`` claim records ``skipped``, which
does satisfy the merge gate, so it is the same shape in principle. Confirming it
requires running the suite on the merge-base too (what
``scripts/coord-test-runner.sh`` does), which is a second run and a larger
change than #2464 scopes. Called out here so it is a known, deliberate gap
rather than an oversight.

FAIL DIRECTION — THE PART THAT MATTERS
--------------------------------------
This may only ever *strengthen* the gate. It can turn an unearned ``passed``
into ``failed``; it must never turn a machine that simply cannot run the suite
into a wall of false failures. So a refutation requires the strongest possible
evidence — **a build/test command that ran to completion and returned nonzero**
(:data:`REFUTING_KINDS`).

Everything else is *inconclusive*, and inconclusive falls back to the worker's
claim, exactly reproducing pre-#2464 behaviour with a note in ``test_reason``
saying so:

* no local checkout of that repo on this machine, no ``test_command``
  configured, a failed fetch, a branch that is not on the remote
  (:data:`~coord.revalidate.KIND_SETUP`) — the check never started;
* a missing toolchain (:data:`~coord.revalidate.KIND_INFRA`) — #1814's case,
  where `cargo` was absent from the daemon's PATH and a green branch read as a
  red suite. Reusing :func:`~coord.revalidate.is_infrastructure_failure` means
  that lesson is not re-learned here;
* a timeout (:data:`~coord.revalidate.KIND_TIMEOUT`) — a hung or merely slow
  suite says nothing about the branch. Classifying a timeout as a refutation
  would let a too-tight ceiling fail every branch in the fleet, so it does not;
* an external signal killing the confirmation subprocess (:data:`KIND_SIGNAL`,
  #2527) — a negative ``returncode`` from `subprocess.run` (e.g. ``-15`` for
  SIGTERM), most often a `coord-agent`/`coord-serve` restart landing mid-run.
  The command never ran to completion, exactly like a timeout, so it gets the
  identical inconclusive treatment rather than being read as "ran and failed";
* exit 126 (:data:`PERMISSION_DENIED_EXIT`, #2596) — POSIX's "found but not
  executable", the same "toolchain problem, not a branch problem" shape as
  exit 127's "not found";
* a nonzero exit with literally no captured output (:data:`KIND_NO_OUTPUT`,
  #2596) — a crash with no message, an OOM-kill, a runner that swallowed its
  own output. A refutation is only as good as the evidence behind it, and
  there is nothing here to put in a fix briefing.

That asymmetry is the safety property: the worst case of a broken confirmation
environment is that the Test stage behaves exactly as it did before this module
existed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Deliberate intra-package reuse of `revalidate`'s mechanical helpers, including
# the private ones. #2464's whole premise is that the out-of-band primitive
# already exists and is merely wired to the wrong place — copying `_run` /
# `_shell_runner` / `_remove_worktree` / `_tail` into a second module would fork
# the very behaviour (the #1924 daemon-env stripping, the #561 worktree
# discipline, the #1814 infra classification) that makes it trustworthy.
from coord.revalidate import (
    KIND_BASELINE_RED,
    KIND_BUILD,
    KIND_INFRA,
    KIND_OK,
    KIND_SETUP,
    KIND_SUITE,
    KIND_TIMEOUT,
    _Echo,
    _remove_worktree,
    _run,
    _shell_runner,
    _tail,
    is_baseline_red_failure,
    is_infrastructure_failure,
    local_repo_dir,
)

# #2464-review: guards the confirm-worktree lifecycle against two overlapping
# notify passes (see `confirm_lock_path` below for the full hazard this
# closes). Not a deferred/cyclic import — `coord.filelock` only reaches into
# the stdlib, and `coord.drive` already imports it the same way.
from coord.filelock import FileLock, LockBusy

#: Ceiling on one confirmation run — **the build and the suite share it**, it is
#: not per-command. Deliberately tighter than
#: :data:`coord.revalidate.DEFAULT_TIMEOUT_SECONDS` (30 min): that one bounds an
#: operator-initiated merge, this one runs in the reap path where a wedged suite
#: would stall every subsequent notification behind it. Safe to keep tight
#: precisely because a timeout is INCONCLUSIVE, not a refutation — the cost of
#: it firing early is one wasted run and a fall back to the worker's claim, not
#: a falsely-failed branch.
CONFIRM_DEFAULT_TIMEOUT_SECONDS = 60 * 20

#: #2464-review: wall-clock ceiling on **all** confirmations in ONE notify pass.
#:
#: A confirmation runs synchronously inside the notify drain, which holds
#: ``~/.coord/notify.lock`` for its whole duration (see
#: :func:`coord.notify.run_drain`'s "Concurrency" note) — so every second spent
#: confirming one branch is a second no other repo's or machine's notifications
#: advance. Bounding one *run* is not enough: a pass with three completed smoke
#: rows would serialize three runs and the drain's worst case would scale with
#: board activity, which is exactly the unbounded-hold shape.
#:
#: This bounds the whole pass instead. :func:`confirmation_timeout` hands each
#: successive confirmation whatever is left of the budget, and once it is spent
#: further PASS claims in the same pass record UNCONFIRMED — i.e. degrade to
#: pre-#2464 behaviour, the module's standing fallback — rather than extending
#: the hold. That truncation is logged, never silent.
#:
#: Sized so the common case never trips it (this repo's suite is ~6 min serial,
#: so a pass can confirm ~4 branches back to back) while the daemon-side worst
#: case stays a number the thin client can actually be given — see
#: :func:`notify_client_timeout_seconds`.
#:
#: #2975: that "~6 min serial" sizing is calibrated on THIS repo and does not
#: hold everywhere — quadraui's ``cargo test --features tui`` builds every
#: example/test executable (#305 in coordinator.yml) and blows straight past
#: it, and every ceiling in this module (this one, ``CONFIRM_DEFAULT_
#: TIMEOUT_SECONDS``, ``deploy/coord-notify.service``'s ``TimeoutStartSec``)
#: is bigger than ``coord-notify.timer``'s 5-minute cadence, so one such
#: confirmation can span several fires. Two changes close that without
#: shrinking this constant (which would just make the COMMON case degrade to
#: UNCONFIRMED instead): ``coord.notify``'s drain now dispatches pending
#: Test/Review/PR-opens BEFORE running any confirmation each pass, so a slow
#: suite on one repo can no longer queue another repo's dispatch behind it;
#: and :func:`expected_confirmation_seconds` remembers how long a repo's
#: confirmation last took, so :func:`confirmation_timeout` can recognise a
#: repo whose suite structurally cannot fit this budget and skip straight to
#: UNCONFIRMED instead of re-discovering that identical fact — and re-paying
#: the full ceiling for it — on every single PASS claim.
CONFIRM_PASS_BUDGET_SECONDS = 60 * 25

#: Don't *start* a confirmation with less than this left in the pass budget.
#: A run that cannot finish only produces :data:`~coord.revalidate.KIND_TIMEOUT`
#: (inconclusive — the same answer as not running), so starting it buys nothing
#: and spends the tail of the budget holding the lock.
CONFIRM_MIN_RUN_SECONDS = 60

#: What one ``/notify`` drain costs with NO confirmation in it — the pre-#2464
#: thin-client HTTP timeout from ``coord/commands/lifecycle.py``.
NOTIFY_BASE_CLIENT_TIMEOUT_SECONDS = 180.0

#: Operator escape hatch. Set to a falsey value to restore exactly the
#: pre-#2464 behaviour (trust the worker's claim). Exists because this runs
#: unconditionally in the reap path and an operator on a machine that cannot run
#: a repo's suite needs an off-switch that does not require a config edit —
#: though they should not usually need one, since that machine's confirmations
#: come back :data:`~coord.revalidate.KIND_SETUP` (inconclusive) anyway.
DISABLE_ENV_VAR = "COORD_CONFIRM_TEST_VERDICT"

_FALSEY = frozenset({"0", "false", "no", "off", ""})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: #2527: a confirmation subprocess terminated by an external signal (a
#: coord-agent/coord-serve restart, a manual ``kill``, anything that delivers
#: a signal rather than letting the command run to completion) — not
#: :data:`KIND_TIMEOUT` (this module's own deadline) but the same shape from
#: the branch's point of view: the suite never got a chance to finish, so
#: nothing was learned about it. `subprocess.run` surfaces this as a negative
#: ``returncode`` (``-signal.SIGTERM`` == ``-15``) through its normal return
#: path rather than raising, which is exactly what let it slip past the
#: existing ``TimeoutExpired`` carve-out and get misread as "ran and failed".
KIND_SIGNAL = "signal"

#: #2596: POSIX's "found but not executable" — a permission bit dropped on a
#: build script, an environment inconsistency, the identical "toolchain
#: problem, not a branch problem" shape :data:`~coord.revalidate.
#: SHELL_NOT_FOUND_EXIT` (127, "not found") already covers. Handled locally
#: here rather than folded into :func:`~coord.revalidate.
#: is_infrastructure_failure`: that helper is shared with ``coord merge
#: --revalidate``, which #2596 does not touch, and this module already keeps
#: its own signal-kill classification local for the same reason.
PERMISSION_DENIED_EXIT = 126

#: #2596: a build/test command that ran to completion, returned nonzero, and
#: captured NOTHING — no stdout, no stderr. A genuine failure almost always
#: leaves a trace (an assertion, a traceback, a compiler error); zero bytes
#: of output before dying nonzero is more often a crash with no message, an
#: OOM-kill, or a runner that silently swallowed its own output than a real,
#: diagnosable test failure. There is nothing here to extract into a fix
#: briefing (see ``coord/drive.py``'s matching #2596 guard against
#: dispatching a fix worker with no extracted failure), so this stays
#: inconclusive rather than refuting a claim we cannot back up with a
#: single line of evidence.
KIND_NO_OUTPUT = "no_output"

#: The only kinds that may overturn a PASS claim: a build or test command that
#: ran to completion and returned nonzero. Nothing weaker. See the module
#: docstring's fail-direction section — this frozenset IS that safety property.
REFUTING_KINDS = frozenset({KIND_BUILD, KIND_SUITE})

#: #2975: kinds whose elapsed wall-clock time is a trustworthy signal of how
#: long THIS repo's confirmation genuinely takes, worth remembering for next
#: time via :func:`record_confirmation_duration`.
#:
#: ``KIND_OK``/``KIND_BUILD``/``KIND_SUITE``/``KIND_BASELINE_RED``/
#: ``KIND_NO_OUTPUT`` all ran the command to actual completion — a real
#: measurement. ``KIND_TIMEOUT`` ran for at least the ceiling it was given —
#: not exact, but a valid LOWER BOUND, and a lower bound is exactly what lets
#: a chronically-too-slow repo (quadraui's ``cargo test --features tui``
#: builds every example/test executable, #305 in coordinator.yml) be
#: recognised as such after a single attempt instead of re-discovering it on
#: every PASS claim — see :func:`expected_confirmation_seconds`.
#:
#: ``KIND_SETUP``/``KIND_INFRA``/``KIND_SIGNAL`` are deliberately excluded:
#: none of them ran the real command for a representative duration (no
#: checkout, no toolchain, or killed externally mid-run), and recording their
#: near-zero elapsed time would silently erase a previously-learned
#: expectation — exactly the failure mode that would make a single lock
#: contention hiccup or a missing checkout reset a repo's hard-won "this one
#: is slow" memory back to "assume it's fast."
RECORDABLE_DURATION_KINDS = frozenset({
    KIND_OK, KIND_BUILD, KIND_SUITE, KIND_BASELINE_RED, KIND_NO_OUTPUT,
    KIND_TIMEOUT,
})

#: Kinds meaning "the check could not reach a verdict". The caller falls back to
#: the worker's own claim on any of these, which is pre-#2464 behaviour.
INCONCLUSIVE_KINDS = frozenset({
    KIND_SETUP, KIND_INFRA, KIND_TIMEOUT, KIND_SIGNAL, KIND_NO_OUTPUT,
})


# ── Per-pass budget (#2464-review) ──────────────────────────────────────────
#
# Thread-local rather than a module global because the daemon runs drains from
# two places on two threads — the pipeline clock's `_notify_drain_tick` and the
# `/notify` handler's `run_in_threadpool(_run)` — and the latter deliberately
# falls back to running UNLOCKED after a 120 s wait for `notify.lock`
# (`coord/serve_app.py`). So two passes really can overlap, and a shared
# counter would let one pass consume the other's budget. Each thread's pass
# gets its own.
_pass_state = threading.local()


def begin_confirmation_pass(total: float = CONFIRM_PASS_BUDGET_SECONDS) -> None:
    """Open a confirmation budget for one notify pass.

    Called once at the top of :func:`coord.notify.run` and
    :func:`coord.notify._run_drain_locked`. Idempotent by construction — a
    second call just refills the budget — so there is deliberately no
    ``end_confirmation_pass`` and no try/finally to get wrong around either of
    those long functions.

    The budget is **spend-based, not deadline-based**: it is drawn down by
    :func:`spend_confirmation_budget` with time actually spent confirming, not
    by the wall clock. A deadline would decay on its own, so a budget left over
    from a finished pass would silently expire and suppress confirmations in
    whatever ran next on this thread — a pytest process outliving 25 minutes
    would be enough. Spend-based, a stale budget is simply a full one.
    """
    _pass_state.remaining = float(total)


def confirmation_timeout(expected_seconds: float | None = None) -> int | None:
    """Seconds the next confirmation in this pass may take, or ``None``.

    ``None`` means the pass budget is spent (or too nearly spent to be worth
    starting a run — :data:`CONFIRM_MIN_RUN_SECONDS`) and the caller must fall
    back to the worker's own claim.

    Outside a pass — a direct :func:`confirm_branch` call, a unit test — there
    is no budget to draw down and the full per-run ceiling applies.

    *expected_seconds* (#2975) is the caller's best estimate of how long THIS
    repo's confirmation will actually take — see
    :func:`expected_confirmation_seconds`, which reads it back from what
    :func:`record_confirmation_duration` learned last time. When it is
    already at or past what this pass has left to spend, starting the run
    would only spend the remainder re-discovering a fact already on record —
    this repo cannot be confirmed in what is left — so this returns ``None``
    immediately instead of paying for that discovery again. This is what
    turns a repo whose suite structurally cannot fit the ceiling (quadraui's
    ``cargo test --features tui``, #2975) from a repeated worst-case cost —
    every PASS claim spending the full budget to learn the identical lesson —
    into a one-time one: the FIRST attempt still spends up to the full
    ceiling discovering the suite is too slow, and every attempt after that
    skips straight to UNCONFIRMED.
    """
    remaining = getattr(_pass_state, "remaining", None)
    if remaining is None:
        return int(CONFIRM_DEFAULT_TIMEOUT_SECONDS)
    if remaining < CONFIRM_MIN_RUN_SECONDS:
        return None
    ceiling = min(float(CONFIRM_DEFAULT_TIMEOUT_SECONDS), remaining)
    if expected_seconds is not None and expected_seconds >= ceiling:
        return None
    return int(ceiling)


def spend_confirmation_budget(seconds: float) -> None:
    """Charge *seconds* of confirmation work against this pass's budget.

    A no-op outside a pass. Never goes negative, and never charges a negative
    amount — a non-monotonic clock must not *refund* budget.
    """
    remaining = getattr(_pass_state, "remaining", None)
    if remaining is None:
        return
    _pass_state.remaining = max(0.0, remaining - max(0.0, float(seconds)))


#: #2975: filename under ``COORD_DIR`` for the per-repo measured-duration map
#: :func:`record_confirmation_duration` / :func:`expected_confirmation_seconds`
#: read and write. A tiny flat file, not a lock-guarded store: the worst case
#: of a torn read/write is one stale or missing estimate, which degrades to
#: exactly pre-#2975 behaviour (attempt the run, learn the hard way) — never
#: a correctness problem, so it does not warrant the ceremony
#: ``confirm_lock_path`` needs for the worktree lifecycle it guards.
_CONFIRM_HISTORY_FILENAME = "confirm_test_history.json"


def _confirm_history_path() -> Path:
    """Where per-repo measured confirmation durations are persisted (#2975).

    Resolved against ``coord.state.COORD_DIR`` freshly on every call — the
    same discipline :func:`confirm_worktree_path` /
    :func:`write_confirmation_output` already use above — so a test's
    ``monkeypatch.setattr("coord.state.COORD_DIR", ...)`` is honoured rather
    than this module caching a stale path at import time.
    """
    from coord.state import COORD_DIR  # noqa: PLC0415

    return COORD_DIR / _CONFIRM_HISTORY_FILENAME


def _load_confirm_history() -> dict[str, float]:
    """Best-effort read of the persisted per-repo duration map.

    Fail-soft on everything — a missing file, corrupt JSON, an unexpected
    shape, a hand-edited value — because this is purely an optimisation hint
    for :func:`confirmation_timeout`, never a correctness dependency. A file
    this cannot make sense of must read as "nothing learned yet", the same
    posture as :func:`coord.commands.drive_queue.read_roll_pending`.
    """
    import json  # noqa: PLC0415

    try:
        raw = _confirm_history_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, (int, float)) and value > 0:
            out[key] = float(value)
    return out


def record_confirmation_duration(repo_name: str, seconds: float) -> None:
    """Remember how long *repo_name*'s confirmation attempt just took (#2975).

    Callers pass this only results whose ``kind`` is in
    :data:`RECORDABLE_DURATION_KINDS` — see that frozenset's docstring for
    which outcomes are a trustworthy signal of real suite duration.

    Overwrites any previous value for this repo outright, with no averaging:
    the LAST attempt is a better estimate of the CURRENT suite than a rolling
    average across however long ago the repo's test suite was this size.

    Best-effort and atomic (tempfile-then-rename, mirroring
    :func:`coord.commands.drive_queue.write_roll_pending`) — a full disk, an
    unwritable ``COORD_DIR``, or a concurrent writer must not break the
    confirmation this is charging against; the caller already has its real
    result, this is purely a hint for next time.
    """
    if seconds <= 0:
        return
    import json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    path = _confirm_history_path()
    history = _load_confirm_history()
    history[repo_name] = float(seconds)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".confirm_test_history.", suffix=".tmp", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(history, fh)
            os.replace(tmp_name, path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    except OSError:
        pass


def expected_confirmation_seconds(repo_name: str) -> float | None:
    """The most recently measured confirmation duration for *repo_name*.

    ``None`` when this repo has never produced a
    :data:`RECORDABLE_DURATION_KINDS` result — the common case for most
    repos most of the time, and the caller's cue to fall back to the
    un-informed ceiling exactly as before #2975.
    """
    return _load_confirm_history().get(repo_name)


def notify_client_timeout_seconds(
    budget: float = CONFIRM_PASS_BUDGET_SECONDS,
) -> float:
    """HTTP timeout a thin client should give a ``/notify`` POST (#2464-review).

    The exact analogue of :func:`coord.revalidate.client_timeout_seconds`, and
    it exists for the identical reason. ``coord notify`` routes to the board
    daemon (#906), and the daemon's ``post_notify`` handler runs the drain to
    completion under ``notify.lock`` no matter what the client does. If the
    client gives up first, the operator (and ``coord-notify.timer``, the
    project's sanctioned single pipeline driver) sees ``error: notify via
    daemon failed`` for a pass that is still running and will finish fine —
    a false negative on the one command the whole auto-loop is driven by.

    Before #2464 a drain never ran a suite, so the pre-existing 180 s was
    ample. Now a pass can spend up to :data:`CONFIRM_PASS_BUDGET_SECONDS`
    confirming PASS claims *on top of* everything it already did, so the client
    has to outlast that sum. Unlike revalidate's deliberately-generous
    ``MAX_REVALIDATION_BATCH`` ceiling, this one is **enforced**:
    :func:`confirmation_timeout` will not hand out time past the budget, so the
    worst case here is a real bound rather than an estimate.

    ``deploy/coord-notify.service``'s ``TimeoutStartSec`` is the *other* client
    ceiling on the same call and is sized off this value — keep them together.
    """
    return NOTIFY_BASE_CLIENT_TIMEOUT_SECONDS + float(budget)


@dataclass
class ConfirmationResult:
    """Outcome of one out-of-band confirmation run.

    Exactly one of :attr:`confirmed` / :attr:`refuted` / :attr:`baseline_red` /
    :attr:`inconclusive` is true, so a caller can branch on them exhaustively
    without an else-fallthrough that silently means "passed".
    """

    kind: str
    reason: str = ""
    output: str = ""
    command: str = ""
    returncode: int | None = None
    worktree: Path | None = None

    @property
    def confirmed(self) -> bool:
        """The repo's own build+test really did pass at this branch."""
        return self.kind == KIND_OK

    @property
    def refuted(self) -> bool:
        """A command ran to completion and returned nonzero — the claim is wrong."""
        return self.kind in REFUTING_KINDS

    @property
    def baseline_red(self) -> bool:
        """The suite failed identically on the merge-base (#2170)."""
        return self.kind == KIND_BASELINE_RED

    @property
    def inconclusive(self) -> bool:
        """The check could not reach a verdict — fall back to the claim."""
        return self.kind in INCONCLUSIVE_KINDS


def write_confirmation_output(assignment_id: str, result: ConfirmationResult) -> Path | None:
    """Persist a confirmation run's captured output tail for the fix briefing (#2563).

    ``coord fix`` (:mod:`coord.commands.plan_followup`) already prefers
    ``COORD_DIR/test_output/<assignment_id>.txt`` over every other evidence
    source when it composes an escalated fix worker's briefing — `coord test
    --fail --output` (:mod:`coord.commands.test_gate`) has written it there
    since #1337. Before this function existed, a #2464 refutation captured
    ``result.output`` (already bounded to a tail by :func:`_tail` in
    :func:`_classify_failure` / :func:`_timeout_output`) and then only ever
    handed it to ``log.warning`` — the fix worker got a one-line reason and no
    failing test names, no tracebacks, and had to re-derive the diagnosis the
    daemon already held in memory.

    *assignment_id* is the parent **work** row's id (the one the fix leg is
    spawned from), not the confirming smoke transition's id — same key
    :func:`coord.commands.plan_followup.fix` reads back by.

    A no-op, returning ``None``, when there is nothing to write (``KIND_OK``
    and ``KIND_SETUP`` both carry an empty ``.output`` — the former because
    nothing failed, the latter because no command ever ran) or when the write
    itself fails (a read-only ``COORD_DIR``, a full disk, ...); this must
    never break verdict recording, which is why the caller in
    :mod:`coord.notify` treats it as best-effort.
    """
    if not result.output:
        return None
    from coord.state import COORD_DIR  # noqa: PLC0415

    try:
        test_output_dir = COORD_DIR / "test_output"
        test_output_dir.mkdir(parents=True, exist_ok=True)
        stored = test_output_dir / f"{assignment_id}.txt"
        stored.write_text(result.output)
    except OSError:
        return None
    return stored


def confirmation_enabled(config=None) -> bool:
    """Whether Test-stage PASS claims get independently confirmed.

    Default **on** — #2464 specifies the promoted check runs unconditionally,
    and a gate that has to be switched on is the posture that let the original
    defect ship. :data:`DISABLE_ENV_VAR` wins over config so an operator can
    turn it off on one host without editing the shared ``coordinator.yml``.

    *config* is duck-typed (``getattr`` throughout) for the same reason
    :func:`coord.revalidate.revalidate` is: the daemon and the tests pass
    lighter stand-ins than the real ``Config``, and a hard attribute read would
    turn a missing shim into an ``AttributeError`` mid-reap.
    """
    raw = os.environ.get(DISABLE_ENV_VAR)
    if raw is not None:
        value = raw.strip().lower()
        if value in _FALSEY:
            return False
        if value in _TRUTHY:
            return True
    pipeline = getattr(config, "pipeline", None)
    flag = getattr(pipeline, "confirm_test_verdict", None)
    if flag is None:
        return True
    return bool(flag)


def confirm_worktree_path(repo_name: str, branch: str) -> Path:
    """Throwaway worktree for one confirmation run.

    Under ``~/.coord/confirm-worktrees/`` — OUTSIDE the base checkout, for the
    #561 reason :func:`coord.revalidate.revalidation_worktree_path` documents:
    on the daemon host the base checkout doubles as the live editable
    coordinator source, so moving its branch silently downgrades the running
    `coord`. Keyed by (repo, branch) so a re-run reuses and overwrites the same
    path rather than accumulating trees per assignment.
    """
    from coord.state import COORD_DIR  # noqa: PLC0415

    safe = f"{repo_name}-{branch}".replace("/", "-")
    return COORD_DIR / "confirm-worktrees" / safe


def confirm_lock_path(repo_name: str, branch: str) -> Path:
    """Per-``(repo, branch)`` lock guarding one confirm-worktree's lifecycle.

    #2464-review: ``/notify``'s own lock (``coord/serve_app.py``'s
    ``post_notify``) waits up to 120s for ``notify.lock`` and then, on
    ``LockBusy``, **runs the whole drain unlocked anyway** — a reasonable
    fallback when a drain finished in seconds, but a confirmation can now
    legitimately run up to :data:`CONFIRM_DEFAULT_TIMEOUT_SECONDS`. Two notify
    passes really can overlap on two threads (see ``_pass_state`` above), and
    if both happen to confirm the *same* branch — a human's manual ``coord
    notify`` landing mid-drain, ``coord drive``'s stall nudge, another
    machine's daemon-routed call — they would otherwise race on the identical
    :func:`confirm_worktree_path`. ``git worktree add --force --detach`` does
    **not** protect against this: ``--force`` is specifically what lets it
    proceed onto a path another process already has checked out, so one
    process's ``_remove_worktree``/checkout can execute while the other is
    mid build/test — a filesystem race that can fail an in-progress command
    for reasons that have nothing to do with the branch, exactly the
    ``REFUTED``-on-a-working-PR failure the module docstring's "FAIL
    DIRECTION" section forbids.

    So ``confirm_branch`` takes this lock for the whole worktree lifecycle
    (remove → add → build → test → remove) before touching anything on disk.
    A second confirmation for the same ``(repo, branch)`` waits its turn
    instead of racing; a confirmation of a *different* branch (or a different
    repo) is unaffected — this is a per-key lock, not a repo-wide one.

    Deliberately a **separate** lock file from :func:`confirm_worktree_path`
    itself: taking ``flock`` on a path that ``git worktree add`` is about to
    create (and a failed run may leave behind) is not a stable thing to lock
    against, and would tie the lock's lifetime to the worktree's.
    """
    from coord.state import COORD_DIR  # noqa: PLC0415

    safe = f"{repo_name}-{branch}".replace("/", "-")
    return COORD_DIR / "confirm-worktrees" / f"{safe}.lock"


#: #2974: default age (hours) a directory under ``confirm-worktrees/`` may sit
#: unremoved before :func:`sweep_stale_confirm_worktrees` reclaims it outright.
#: Every entry under that root is throwaway by construction (#561) and
#: `confirm_branch`'s own `finally` now removes its worktree on every exit —
#: so anything still here this long survived something worse than an ordinary
#: failure (a hard kill of the whole process, `coord-agent`/`coord-serve`
#: restarting mid-run before #2974's `finally` fix ever ran, a full disk
#: mid-cleanup). Generous relative to :data:`CONFIRM_DEFAULT_TIMEOUT_SECONDS`
#: (20 min) so this never races a confirmation that is still legitimately
#: in flight.
#:
#: Footgun to remember if either ceiling ever moves: the margin here is
#: relative, not enforced. `sweep_stale_confirm_worktrees` doesn't know
#: whether a given `confirm-worktrees/<repo>-<branch>` entry has a
#: `confirm_branch` call still running against it — it reclaims purely on
#: directory mtime. If `CONFIRM_DEFAULT_TIMEOUT_SECONDS` (or a future
#: per-repo override of it) is ever raised to within a few hours of this
#: constant, a still-in-flight confirmation's worktree (and its `.lock`
#: file) could be `rmtree`'d out from under it by this sweep. Keep this
#: comfortably above whatever the longest configured confirmation timeout
#: is, not just the current default.
STALE_WORKTREE_MAX_AGE_HOURS = 6.0


def sweep_stale_confirm_worktrees(
    *,
    max_age_hours: float = STALE_WORKTREE_MAX_AGE_HOURS,
    dry_run: bool = False,
    now: float | None = None,
) -> dict:
    """Reclaim ``confirm-worktrees/`` entries older than *max_age_hours* (#2974).

    Belt-and-suspenders over the `finally`-based cleanup in
    :func:`confirm_branch`: that fix stops the leak going forward, but does
    nothing about the hundreds of directories (189G measured on one host,
    #2974) a build that never reaches this fix — or any future bug in the
    cleanup path itself — can still leave behind. This sweep is the backstop
    that ages them out regardless of cause.

    Deliberately filesystem-only: it reclaims disk with a plain
    ``shutil.rmtree`` keyed on directory mtime, and does **not** attempt a
    ``git worktree prune`` in the originating repo's checkout (that would
    need to map each ``<repo>-<branch>`` directory name back to a configured
    repo, which is ambiguous when a repo or branch name itself contains a
    ``-``). The admin metadata that leaves behind in the base checkout's
    ``.git/worktrees/`` is a few KB per entry — negligible next to the build
    trees this reclaims — and self-heals the next time :func:`confirm_branch`
    or :func:`coord.revalidate.revalidate` runs `git worktree prune` for that
    repo anyway.

    Called from :func:`coord.housekeeping.sweep`'s existing low-cadence
    daemon tick and ``coord housekeeping``, so it needs no new wiring or
    schedule of its own. Returns
    ``{"removed": [names], "dry_run": bool, "max_age_hours": float}`` —
    ``removed`` lists what was (or, for ``dry_run``, would be) deleted.
    Silently a no-op if the directory does not exist yet.
    """
    from coord.state import COORD_DIR  # noqa: PLC0415

    root = COORD_DIR / "confirm-worktrees"
    result: dict = {
        "removed": [],
        "dry_run": dry_run,
        "max_age_hours": max_age_hours,
    }
    if not root.exists():
        return result

    cutoff = (now if now is not None else time.time()) - max_age_hours * 3600.0
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return result

    for entry in entries:
        # Lock files (`confirm_lock_path`) are tiny and self-explanatory by
        # name; only the worktree directories themselves are the disk cost
        # this sweep exists to reclaim. A stray lock with no matching
        # worktree directory is harmless — the next `confirm_branch` call for
        # that (repo, branch) simply re-creates and re-locks it.
        if entry.name.endswith(".lock") or not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        result["removed"].append(entry.name)
        if dry_run:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        lock_path = root / f"{entry.name}.lock"
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    return result


def branch_touched_files(repo_dir: Path, branch: str, base_branch: str) -> list[str]:
    """Paths the branch changes vs its merge-base with *base_branch*.

    Computed with plain local git against the already-fetched ``origin/*`` refs
    — no ``gh``, no network beyond the fetch :func:`confirm_branch` already did.
    Returns ``[]`` on any failure; the caller reads that as "unknown", never as
    "nothing changed".
    """
    try:
        diffed = _run(
            ["git", "diff", "--name-only",
             f"origin/{base_branch}...origin/{branch}"],
            cwd=repo_dir,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if diffed.returncode != 0:
        return []
    return [line.strip() for line in (diffed.stdout or "").splitlines() if line.strip()]


def unmet_confirmation_capabilities(
    config, repo_name: str, branch: str, repo_dir: Path,
) -> list[str]:
    """Capabilities this branch's diff needs that THIS machine does not have.

    #2464-review: the confirmation subprocess runs wherever the notify drain
    runs — in production the always-on daemon host — which is emphatically not
    the machine the Test stage itself would have picked. This repo's whole
    answer to "some suites only pass on the right hardware" is
    ``smoke_tests.capability_rules`` (CLAUDE.md, "Smoke tests validate on
    capable hardware"), and :func:`coord.smoke.dispatch_smoke` routes the
    original run through them. Running the *confirmation* without consulting
    them would let a headless daemon re-run a GTK or browser suite, watch it
    fail for want of a display, and **refute a perfectly good branch** — the
    one direction :mod:`coord.confirm_test` promises never to fail in.

    So the same rules gate the confirmation, and an unmet capability is
    :data:`~coord.revalidate.KIND_SETUP` (inconclusive → the worker's claim
    stands, pre-#2464 behaviour) rather than a run on the wrong hardware.

    Benefit of the doubt in every ambiguous case, mirroring
    :func:`coord.acceptance.capability_gap` (#966):

    * no ``capability_rules`` configured, or the diff hits none of them → ``[]``
      (nothing extra is required, so any machine will do — the #1426 rule);
    * this host is not a recognized machine in ``coordinator.yml`` → ``[]``
      (a dev box outside the fleet may well have everything installed);
    * the diff could not be computed → ``[]`` (the gate cannot be applied, so
      it is not applied; a wrong-hardware run still usually lands on
      :data:`~coord.revalidate.KIND_INFRA`, which is inconclusive too).
    """
    smoke_cfg = getattr(config, "smoke_tests", None)
    rules = getattr(smoke_cfg, "capability_rules", None) or []
    if not rules:
        return []

    from coord.smoke import match_rules  # noqa: PLC0415 — avoid an import cycle
    from coord.test_orchestrator import local_machine  # noqa: PLC0415

    here = local_machine(config)
    if here is None:
        return []

    repo_cfg = config.repo(repo_name) if config is not None else None
    base_branch = getattr(repo_cfg, "default_branch", None) or "main"
    touched = branch_touched_files(repo_dir, branch, base_branch)
    if not touched:
        return []

    have = set(getattr(here, "capabilities", None) or [])
    return [cap for cap in match_rules(touched, rules) if cap not in have]


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    """Partial stdout/stderr captured before a confirmation command timed out.

    ``subprocess.run(..., capture_output=True, timeout=...)`` fills ``.stdout``
    / ``.stderr`` in on `TimeoutExpired` — the child had already written
    *something* by the deadline — but the two `except subprocess.TimeoutExpired`
    arms in :func:`confirm_branch` discarded it entirely, so a TIMEOUT verdict
    carried strictly less evidence than BASELINE_RED/SIGNAL did even though the
    same partial output was sitting right there on the exception (#2563).
    Guards for ``None``/bytes since a test's stand-in *runner* seam may raise a
    bare ``TimeoutExpired(cmd, timeout)`` with neither attribute set.
    """
    out = getattr(exc, "stdout", None) or ""
    err = getattr(exc, "stderr", None) or ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    if isinstance(err, bytes):
        err = err.decode("utf-8", errors="replace")
    if not out and not err:
        return ""
    return _tail(out + "\n" + err)


def _classify_failure(
    stage: str, returncode: int, output: str, worktree: Path, command: str,
) -> ConfirmationResult:
    """Turn a nonzero build/test exit into the right kind.

    Order matters and mirrors :func:`coord.revalidate.revalidate`: signal-kill
    first (#2527 — the command never ran to completion at all, so there is
    nothing left to classify), then infra (the suite never ran), then
    baseline-red (it ran, but the branch is not at fault), and only what is
    left over is a genuine refutation.
    """
    if returncode < 0:
        # #2527: a negative returncode means `subprocess.run` reports the
        # child was killed BY a signal, not that it ran and exited nonzero —
        # e.g. `-15` for SIGTERM from a `coord-agent`/`coord-serve` restart
        # landing mid-confirmation, or a manual `kill`. Same fail-direction
        # as `subprocess.TimeoutExpired` just above this function's callers:
        # a command that never finished says nothing about the branch, so
        # this must stay inconclusive — never `REFUTING_KINDS` — no matter
        # how "nonzero" the raw exit code looks.
        return ConfirmationResult(
            kind=KIND_SIGNAL,
            reason=(
                f"confirmation {stage} command was killed by signal "
                f"{-returncode} (exit {returncode}) rather than running to "
                "completion — like a timeout, a command that never finished "
                "says nothing about the branch, so this is not a refutation "
                "(#2527)"
            ),
            output=_tail(output),
            command=command,
            returncode=returncode,
            worktree=worktree,
        )
    infra = returncode == PERMISSION_DENIED_EXIT or is_infrastructure_failure(
        returncode, output,
    )
    baseline = not infra and is_baseline_red_failure(returncode, output)
    if infra:
        return ConfirmationResult(
            kind=KIND_INFRA,
            reason=(
                f"confirmation could not run the {stage} command (exit "
                f"{returncode}): the toolchain is missing on this machine, so "
                "the suite never executed and NOTHING was learned about the "
                "branch — falling back to the worker's own claim (#1814)"
            ),
            output=_tail(output),
            command=command,
            returncode=returncode,
            worktree=worktree,
        )
    if baseline:
        return ConfirmationResult(
            kind=KIND_BASELINE_RED,
            reason=(
                f"confirmation ran the {stage} command and it failed (exit "
                f"{returncode}), but every failure reproduces on the merge-base "
                "— the baseline is red, so the branch made nothing worse "
                "(#2170)"
            ),
            output=_tail(output),
            command=command,
            returncode=returncode,
            worktree=worktree,
        )
    if not (output or "").strip():
        # #2596: ran to completion, returned nonzero, and captured literally
        # nothing — no stdout, no stderr. Same fail-direction reasoning as
        # every other arm above: a refutation is only as good as the
        # evidence behind it, and there is not one line of it here (a crash
        # with no message, an OOM-kill, a runner that swallowed its own
        # output). Never REFUTING_KINDS on evidence this thin.
        return ConfirmationResult(
            kind=KIND_NO_OUTPUT,
            reason=(
                f"the independently-run {stage} command exited nonzero (exit "
                f"{returncode}) but produced NO captured output — there is "
                "nothing here to diagnose as a branch failure, so this is "
                "not treated as a refutation (#2596)"
            ),
            command=command,
            returncode=returncode,
            worktree=worktree,
        )
    return ConfirmationResult(
        kind=KIND_BUILD if stage == "build" else KIND_SUITE,
        reason=(
            f"the independently-run {stage} command FAILED (exit {returncode}) "
            "at this branch — the Test-stage worker's pass claim is not "
            "supported by an actual run (#2464)"
        ),
        output=_tail(output),
        command=command,
        returncode=returncode,
        worktree=worktree,
    )


def confirm_branch(
    repo_name: str,
    branch: str | None,
    config,
    *,
    timeout: int = CONFIRM_DEFAULT_TIMEOUT_SECONDS,
    runner=None,
    echo=None,
    clock=None,
) -> ConfirmationResult:
    """Run the repo's real build+test at ``origin/<branch>`` and report the truth.

    Writes **nothing** — no verdict, no board state, no GitHub. It answers one
    question ("does this branch actually build and pass?") and hands the answer
    back for the caller to act on. That separation is what makes it safe to call
    from the reap path and trivial to test.

    Which command: ``ci_command`` when the repo declares one, else
    ``test_command`` — identical to :func:`coord.revalidate.revalidate`'s #2091
    choice, and for the same reason. A verdict is only worth what the suite
    behind it is worth, so when a repo has said what CI runs, confirm with that.

    *timeout* bounds the **whole** confirmation, build and suite together, not
    each command separately (#2464-review). A per-command ceiling made the real
    worst case ``2 × timeout`` while every docstring and every caller sizing a
    budget off it read it as one — and this runs inside the notify drain, where
    that factor of two is time the fleet's ``notify.lock`` is held. One deadline
    is taken at the top and both commands draw down from it.

    *runner* is the testing seam, same shape as
    :func:`coord.revalidate._shell_runner`: ``runner(command, cwd, timeout)``
    returning something with ``returncode`` / ``stdout`` / ``stderr``. *clock*
    is the matching seam for the shared deadline.
    """
    echo = echo or _Echo()
    clock = clock or time.monotonic
    deadline = clock() + float(timeout)

    if not branch:
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=(
                "no branch recorded for the Test-stage assignment, so there is "
                "nothing to check out and confirm"
            ),
        )

    repo_cfg = config.repo(repo_name) if config is not None else None
    if repo_cfg is None:
        return ConfirmationResult(
            kind=KIND_SETUP, reason=f"no repo config for {repo_name!r}",
        )

    test_command = (
        getattr(repo_cfg, "ci_command", None) or ""
    ).strip() or getattr(repo_cfg, "test_command", None)
    if not test_command:
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=(
                f"no test_command configured for {repo_name!r} — there is no "
                "suite to confirm against"
            ),
        )

    repo_dir = local_repo_dir(config, repo_name)
    if repo_dir is None or not repo_dir.exists():
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=(
                f"no local checkout for {repo_name!r} on this machine "
                f"({repo_dir or 'no repo_path configured'}) — confirmation runs "
                "the suite locally, so it can only run where the repo lives"
            ),
        )

    wt_path = confirm_worktree_path(repo_name, branch)

    # #2464-review: the whole worktree lifecycle below (remove -> add ->
    # build -> test -> remove) must never run twice concurrently for the same
    # (repo, branch) — see `confirm_lock_path` for the full hazard. `/notify`'s
    # own lock gives up after 120s and runs unlocked, so this is the actual
    # guarantee. Bounded by whatever is left of the shared deadline: lock
    # contention that cannot clear before the deadline is exactly as
    # informative as a run that cannot finish before it, so it gets the same
    # answer — inconclusive, never a refutation (the module's "FAIL
    # DIRECTION" invariant).
    lock = FileLock(confirm_lock_path(repo_name, branch))
    try:
        lock.acquire(timeout=max(0.0, deadline - clock()))
    except LockBusy:
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=(
                f"another confirmation for {repo_name}/{branch} is already "
                "in progress on this machine — the confirm-worktree lifecycle "
                "is not safe to run twice concurrently, so this one steps "
                "aside rather than racing it; falling back to the worker's "
                "own claim (#2464-review)"
            ),
        )
    except OSError as exc:
        return ConfirmationResult(
            kind=KIND_SETUP,
            reason=(
                f"could not take the confirmation lock for {repo_name}/"
                f"{branch}: {exc}"
            ),
        )

    try:
        _remove_worktree(repo_dir, wt_path)
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            fetched = _run(["git", "fetch", "origin", "--prune"], cwd=repo_dir)
        except (subprocess.SubprocessError, OSError) as exc:
            return ConfirmationResult(
                kind=KIND_SETUP, reason=f"git fetch failed: {exc}",
            )
        if fetched.returncode != 0:
            return ConfirmationResult(
                kind=KIND_SETUP,
                reason=f"git fetch failed: {(fetched.stderr or '').strip()}",
            )

        # #2464-review: is THIS machine allowed to judge THIS diff? Checked
        # after the fetch (the diff needs fresh `origin/*` refs) and before
        # the checkout (a wrong-hardware confirmation should not even pay for
        # a worktree).
        missing_caps = unmet_confirmation_capabilities(
            config, repo_name, branch, repo_dir,
        )
        if missing_caps:
            return ConfirmationResult(
                kind=KIND_SETUP,
                reason=(
                    f"this machine does not advertise {', '.join(missing_caps)}, "
                    f"which `smoke_tests.capability_rules` require for the files "
                    f"origin/{branch} touches — confirming here would judge the "
                    "branch on hardware the Test stage would never have routed "
                    "it to, so nothing is concluded (#2464)"
                ),
            )

        added = _run(
            ["git", "worktree", "add", "--force", "--detach",
             str(wt_path), f"origin/{branch}"],
            cwd=repo_dir,
        )
        if added.returncode != 0:
            # Most often: the worker never pushed, so `origin/<branch>` does
            # not exist. Inconclusive rather than a refutation — "we could
            # not find the branch" is not "the branch is red". The
            # pushed-nothing case has its own detector (`push_failure_reason`,
            # #1797).
            return ConfirmationResult(
                kind=KIND_SETUP,
                reason=(
                    f"could not check out origin/{branch} for confirmation: "
                    f"{(added.stderr or '').strip()}"
                ),
            )

        run_cmd = runner or _shell_runner

        def _left() -> int:
            """Whole seconds left on the shared build+suite deadline (min 1)."""
            return max(1, int(deadline - clock()))

        build_command = getattr(repo_cfg, "build_command", None)
        if build_command:
            echo(f"    confirming build: {build_command}")
            try:
                built = run_cmd(build_command, wt_path, _left())
            except subprocess.TimeoutExpired as exc:
                return ConfirmationResult(
                    kind=KIND_TIMEOUT,
                    reason=(
                        f"confirmation build timed out after {timeout}s — a "
                        "suite that did not finish says nothing about the "
                        "branch"
                    ),
                    output=_timeout_output(exc),
                    command=build_command,
                    worktree=wt_path,
                )
            if built.returncode != 0:
                return _classify_failure(
                    "build",
                    built.returncode,
                    (built.stdout or "") + "\n" + (built.stderr or ""),
                    wt_path,
                    build_command,
                )

        if deadline - clock() <= 0:
            # The build consumed the whole shared window. Same answer as a
            # suite timeout — inconclusive — but reported without spending
            # another second of the drain's lock hold on a run that cannot
            # finish.
            return ConfirmationResult(
                kind=KIND_TIMEOUT,
                reason=(
                    f"the confirmation build used the whole {timeout}s "
                    "window, leaving no time to run the suite — nothing was "
                    "learned about the branch"
                ),
                command=test_command,
                worktree=wt_path,
            )

        echo(f"    confirming tests: {test_command}")
        try:
            tested = run_cmd(test_command, wt_path, _left())
        except subprocess.TimeoutExpired as exc:
            return ConfirmationResult(
                kind=KIND_TIMEOUT,
                reason=(
                    f"confirmation suite timed out after {timeout}s — a "
                    "suite that did not finish says nothing about the branch"
                ),
                output=_timeout_output(exc),
                command=test_command,
                worktree=wt_path,
            )
        if tested.returncode != 0:
            return _classify_failure(
                "suite",
                tested.returncode,
                (tested.stdout or "") + "\n" + (tested.stderr or ""),
                wt_path,
                test_command,
            )

        # Green, and observed rather than reported. Nothing to inspect —
        # cleanup happens in `finally` below, same as every other exit from
        # this block.
        return ConfirmationResult(
            kind=KIND_OK,
            reason=(
                f"independently re-ran `{test_command}` at origin/{branch} "
                "and it passed"
            ),
            command=test_command,
            returncode=0,
        )
    finally:
        # #2974: EVERY exit from the block above — including KIND_TIMEOUT
        # (a hung/slow suite), KIND_SIGNAL (a coord-agent/coord-serve restart
        # landing mid-run, #2527), and a genuine refutation (KIND_BUILD/
        # KIND_SUITE) — must remove this worktree, not just the green path.
        #
        # `coord.revalidate`'s sibling helper deliberately KEEPS a failed
        # worktree "for inspection" (see its `_remove_worktree` docstring and
        # `format_failure`'s "worktree kept for inspection" line) because
        # `coord merge --revalidate` is opt-in, operator-initiated, and rare —
        # a human is watching and can go look at the tree it names.
        # `confirm_branch` is the opposite shape: it runs unattended, on every
        # PASS claim, inside the automatic reap path, keyed by (repo, branch)
        # — and a branch is rarely revisited, so nothing was ever going to
        # remove a "kept for inspection" tree here. That is exactly how this
        # leaked to 189G / 346 dirs on one host in nine days (#2974): the
        # biggest trees came from the slowest repos, i.e. precisely the
        # KIND_TIMEOUT/KIND_SIGNAL runs that used to fall through this
        # `finally` without cleanup.
        #
        # Nothing of diagnostic value is lost: the captured output tail
        # (`ConfirmationResult.output`) is already persisted to
        # `test_output/<assignment_id>.txt` by `write_confirmation_output`
        # regardless of outcome — the worktree itself was never the thing an
        # operator actually inspected.
        _remove_worktree(repo_dir, wt_path)
        lock.release()


__all__ = [
    "CONFIRM_DEFAULT_TIMEOUT_SECONDS",
    "CONFIRM_MIN_RUN_SECONDS",
    "CONFIRM_PASS_BUDGET_SECONDS",
    "DISABLE_ENV_VAR",
    "INCONCLUSIVE_KINDS",
    "KIND_NO_OUTPUT",
    "KIND_SIGNAL",
    "NOTIFY_BASE_CLIENT_TIMEOUT_SECONDS",
    "PERMISSION_DENIED_EXIT",
    "RECORDABLE_DURATION_KINDS",
    "REFUTING_KINDS",
    "STALE_WORKTREE_MAX_AGE_HOURS",
    "ConfirmationResult",
    "begin_confirmation_pass",
    "branch_touched_files",
    "confirm_branch",
    "confirm_lock_path",
    "confirm_worktree_path",
    "confirmation_enabled",
    "confirmation_timeout",
    "expected_confirmation_seconds",
    "notify_client_timeout_seconds",
    "record_confirmation_duration",
    "spend_confirmation_budget",
    "sweep_stale_confirm_worktrees",
    "unmet_confirmation_capabilities",
    "write_confirmation_output",
]
