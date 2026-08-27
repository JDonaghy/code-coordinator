"""``coord merge --revalidate`` — the merge lane's stale-verdict resolution (#1769).

#1738 gave ``coord drive`` an arm for a STALE-but-``passed`` smoke verdict: it
re-dispatches the Test stage against the current base instead of escalating to
a human. That arm lives in :mod:`coord.drive` and only ever fires while a live
drive is watching the issue. Every *other* merge path — ``coord merge``, its
``--only`` form, the auto-drain, the TUI merge action, the daemon ``/merge``
route — still had only escalate-or-block, so a branch that finishes and sits in
the merge queue with no live drive stays stuck: the next merge moves the base,
stales its verdict, and nobody is watching. Measured on 2026-08-03: three
stale-verdict stalls in one session, #1738's arm could fire on exactly one.

**This module is the resolution for that lane**, and it is deliberately
*opt-in*. ``coord merge`` with no flag is byte-identical to before — nothing
here runs unless the operator typed ``--revalidate``. An unattended dispatcher
firing test runs from inside the merge path is the shape that was gated off
after the 2026-06-07 auto-loop token-burn incident (it is why
``merge.auto_drain`` defaults to ``false``), so the auto-drain and the daemon's
own periodic drain pass ``revalidate=False`` and always will.

STRATEGY — batch composite (#1715 Option 3)
-------------------------------------------
What the operator does by hand — three times in the session above — is: make a
worktree at the current base, compose every stale branch onto it, run the suite
**once**, and let them all through on that single result. That is what
:func:`revalidate` does, and it is what removes the *cascade*: N approved
branches against one base used to cost N−1 full suite runs, because the first
merge staled everything behind it. One composite run costs 1.

**The honest trade, stated plainly (#1715):** a composite run validates the
*composite*, not each branch alone. A green composite does not prove each
branch is green in isolation. That is acceptable here for one specific reason
— every branch in the set **already carries its own ``passed`` verdict**
against an earlier base, so the composite is re-confirming that those verdicts
still hold *together* against the current base. It is a re-confirmation, not a
first proof, which is exactly why :func:`coord.merge_queue.
revalidation_candidates` refuses to include an entry that never had a verdict
(``SMOKE_MISSING``), or one blocked on review/CI/conflict.

It is also a *truer* claim than it first looks: :func:`coord.merge_queue.
process` snapshots ``target_branch_head_sha`` **once per (repo, target_branch)
group**, so the whole batch merges against the same base the composite was
built on. The tree the composite validated is the tree that ends up on the
base branch.

FAILURE DOES NOT POISON THE BATCH (#1715)
-----------------------------------------
A red composite is the hard part: the naive version blocks all N on one
branch's fault. :func:`revalidate_group` is the resolution — on a red
composite it merges **nothing**, marks **nothing** failed, and falls back to
re-running each candidate **individually** against the current base. The
culprit is named by its own failing run; the innocent branches get a genuine
solo verdict and still merge in the same invocation.

Cost: a green composite (the overwhelmingly common case) is **one** run total.
A red composite is 1 + N in the worst case, which is the bound #1715 specifies.

Per-entry narrowing was chosen over a bisect: a bisect is only cheaper when
there is exactly **one** culprit, degrades as soon as there are two, and its
bookkeeping is subtle. The per-entry pass is flat O(N), identifies *every*
culprit rather than the first, and — the part that actually matters — leaves
each survivor with a verdict earned by a run that validated **that branch
alone against the current base**, which is a strictly stronger claim than
"was a member of some green subset". N here is a merge queue's depth (2–5),
so the constant factor is not worth the ambiguity.

Nothing here dispatches a worker or spends tokens: it is `git` + the repo's own
``build_command``/``test_command``, run locally, exactly like ``coord test``
(#561's throwaway-worktree discipline included — the base checkout is never
moved, because on the daemon host it doubles as the live editable coordinator
source).

BOUNDED
-------
One composite run, plus at most one solo run per candidate, per ``coord merge
--revalidate`` invocation. There is no retry loop and no second composite: a
branch whose *solo* run fails terminates blocked with the failure quoted, and
the operator (or the next invocation) decides. That is the merge lane's
analogue of #1738's ``fix_rounds`` budget — a branch that cannot pass
terminates with a clear reason rather than spinning.

OPT-IN, ALWAYS
--------------
None of *this module's own functions* start running on a schedule — ``coord
merge --revalidate`` is the only caller that invokes :func:`revalidate` /
:func:`revalidate_group` directly, and the daemon's ``_auto_drain_tick``
passes ``revalidate=False`` permanently, exactly as before.

#2829 adds a SIBLING unattended caller instead of changing that:
:func:`coord.serve_app._auto_revalidate_tick`, gated on its own
``merge.auto_revalidate`` flag (default ``false``, its own trust bar in
``docs/MERGE_AUTO_DRAIN_TRUST_BAR.md``). The argument for treating this
differently from the 2026-06-07 token-burn shape that keeps ``auto_drain``
off: nothing here dispatches a worker or spends a token either way — see the
module-level paragraph above — so the guard that incident justifies does not
apply to this code path. What DOES apply, and what makes this "not a
one-line flag flip", is the lock-hold hazard: calling ``coord merge
--revalidate``'s existing plumbing (``_apply_revalidation`` →
``revalidate_group`` → ``process()``, all under ``_merge_lock`` on the daemon
route) from an unattended tick would hold that lock for up to
``DEFAULT_TIMEOUT_SECONDS × (1 + N)`` on every tick, wedging every other
merge in the fleet for as long as a sleeping operator doesn't notice.
``_auto_revalidate_tick`` therefore does NOT reuse that call chain — it runs
:func:`revalidate_group` with **no lock held at all**, and takes
``_merge_lock`` only immediately before merging, after
:func:`revalidated_base_still_current` confirms the base the composite
validated is still current. See that function's docstring for why the
recheck is the correctness crux, not an optional extra.

Acknowledged, not fixed (#2829 review round 1, non-blocking): with no lock
held at all, this function's own ``git fetch``/``git worktree add`` against
the shared, non-throwaway ``repo_dir`` (below) can now run at the same moment
an attended ``coord merge --revalidate`` (still under ``_merge_lock`` for its
whole duration) does the same thing against that identical checkout — an
overlap that could never happen before this module had two independent
unlocked callers. Git's own locking (``index.lock``, ref locks) generally
turns a clash into a clean failure rather than corruption, and this class of
unguarded concurrent local-checkout access already exists elsewhere in this
codebase (``coord test`` via ``confirm_test.py``), so it is left unguarded
here too — a repo-scoped mutex around the checkout is the fix if it ever
bites in practice.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from coord.merge_queue import RevalidationCandidate

# Hard ceiling on the composite suite run. A hung test binary must not wedge a
# `coord merge` invocation forever — and on the daemon route this runs inside a
# request handler that holds `_merge_lock`, so every other merge in the fleet
# waits behind it. Generous but bounded: the claude-coordinator suite is ~6 min
# serial and the tui `cargo test` leg is slower still, so 30 min covers a cold
# composite with room to spare while still terminating a wedged run.
# `_merge_via_daemon` sizes the thin client's HTTP timeout off this value.
DEFAULT_TIMEOUT_SECONDS = 60 * 30

# #1715-review: `revalidate_group`'s red-composite fallback is 1 (composite) +
# N (one solo re-test per candidate) serial suite runs — see that function's
# docstring. #1769 sized the thin client's HTTP timeout for exactly ONE run;
# this module's own worst case is now 1 + N, and the client has to outlast it
# or the operator sees "error: merge via daemon failed" for a batch that
# actually finished (the daemon keeps running under `_merge_lock` regardless —
# see `_merge_via_daemon`'s docstring).
#
# The client posts to ``/merge`` before any candidate is known — computing the
# real N would mean re-implementing `merge_queue.revalidation_candidates`'s
# whole eligibility policy (board state, CI lookups) on a thin client, which
# is exactly what #584 routes to the daemon to avoid. So this is a documented,
# deliberately generous ceiling rather than a measured count: comfortably
# above the "merge queue depth (2-5)" this module's STRATEGY section cites, so
# a real-world batch never gets close to it. A batch that somehow exceeds it
# just gets the pre-#1715 false-negative report back — the daemon still
# finishes the merge either way.
MAX_REVALIDATION_BATCH = 10


def client_timeout_seconds(revalidate: bool) -> float:
    """HTTP timeout ``_merge_via_daemon`` should give a ``/merge`` POST.

    A plain merge gets the pre-#1769 900s ceiling. A ``--revalidate`` run can
    execute the whole suite up to ``1 + MAX_REVALIDATION_BATCH`` times (see
    that constant) before the daemon responds, so it gets a window sized off
    that worst case instead of a single :data:`DEFAULT_TIMEOUT_SECONDS`.

    #1925 adds a second, independent cost on the same request: the CI arm
    (``_apply_ci_revalidation`` in ``coord/commands/merge.py``) now waits,
    per CI-stale candidate, up to :data:`coord.ci_store.
    CI_RERUN_MAX_WAIT_SECONDS` for a just-triggered re-run to settle before
    ``process()`` evaluates it — bounded polling, not a suite run, but still
    wall-clock the daemon holds the request open for. Worst case is every
    candidate in the batch needing the full CI wait, serially, so the same
    :data:`MAX_REVALIDATION_BATCH` ceiling scales this term too.
    """
    if not revalidate:
        return 900.0
    from coord.ci_store import CI_RERUN_MAX_WAIT_SECONDS  # noqa: PLC0415

    return (
        float(DEFAULT_TIMEOUT_SECONDS) * (1 + MAX_REVALIDATION_BATCH)
        + float(CI_RERUN_MAX_WAIT_SECONDS) * MAX_REVALIDATION_BATCH
        + 300.0
    )


# How much of a failing run's output to quote back. The whole point of the
# "a failed re-test leaves the entry blocked with the failure quoted" rule is
# that the operator can act on it without going hunting, but a full pytest
# log has no business being echoed into a merge summary.
_OUTPUT_TAIL_CHARS = 4000


def _tail(text: str, limit: int = _OUTPUT_TAIL_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…(truncated)…\n" + text[-limit:]


# Why a composite failed, which decides whether splitting it up can help.
#
# SETUP failures are *common-mode*: no test command, no local checkout, a
# failed fetch, candidates spanning two bases. Every candidate would hit the
# identical wall, so a per-entry fallback would just reproduce the same error
# N times for nothing. COMPOSE/BUILD/SUITE/TIMEOUT are per-branch-attributable
# — that is precisely what the fallback exists to narrow down.
#
# INFRA (#1814) is a third thing again, and the distinction it draws is the
# point of that issue: the suite did not FAIL, it did not RUN. The daemon that
# executes this is a systemd user unit whose PATH never saw ~/.cargo/bin, so
# `cargo` was simply not there and the shell's "command not found" was being
# reported as a red suite for a branch CI had already proven green. Like SETUP
# it is common-mode (never narrowable — every solo run hits the identical
# wall), but unlike SETUP it happened *after* the composite was built, so the
# worktree is kept and the operator-facing wording must say "could not run",
# never "SUITE FAILED".
KIND_OK = "ok"
KIND_SETUP = "setup"
KIND_COMPOSE = "compose"
KIND_BUILD = "build"
KIND_SUITE = "suite"
KIND_TIMEOUT = "timeout"
KIND_INFRA = "infra"
#: The suite RAN and failed, but identically on the merge-base (#2170) — so it
#: is a statement about the machine's baseline, not about any branch. Distinct
#: from :data:`KIND_INFRA` (where the suite never ran at all) and from
#: :data:`KIND_SUITE` (where the branch broke something): the operator action is
#: "fix the baseline", not "fix the runner environment" or "fix the branch".
KIND_BASELINE_RED = "baseline_red"

#: Composite failure kinds a per-entry pass can actually narrow (#1715).
NARROWABLE_KINDS = frozenset({
    KIND_COMPOSE, KIND_BUILD, KIND_SUITE, KIND_TIMEOUT,
})

#: Kinds where no suite was actually executed, so "how many suite runs did
#: that cost" is zero and no verdict may be inferred in either direction.
NO_SUITE_RAN_KINDS = frozenset({KIND_SETUP, KIND_INFRA})

#: Exit code ``scripts/coord-test-runner.sh`` reserves for "the suite could not
#: run" (a missing toolchain). See that script's header. Documented here, but
#: deliberately NOT trusted on its own — see :func:`is_infrastructure_failure`.
RUNNER_INFRA_EXIT = 3

#: 127 is every POSIX shell's "command not found" — an arbitrary repo's own
#: ``test_command`` that dies this way never started a suite either. Unlike a
#: small integer, this one is reserved by the shell rather than chosen by the
#: command, so it carries the same meaning for a command we did not write.
SHELL_NOT_FOUND_EXIT = 127

#: What ``coord-test-runner.sh`` prints when it cannot find a toolchain. THIS
#: is the signal, not the exit code: a repo's own build/test command is free
#: to exit 3 for a perfectly genuine failure (this repo's own test suite has
#: a ``build_command = "exit 3"`` case), so keying on the number alone would
#: relabel real red builds as infrastructure — the dangerous direction.
INFRA_OUTPUT_MARKERS = ("TOOLCHAIN MISSING", "RESULT: INFRA")

#: What ``coord-test-runner.sh`` prints when every failure in a suite reproduces
#: on the merge-base (#2170), and the exit code it pairs with it.
#:
#: Same "the marker, not the number" discipline as :data:`INFRA_OUTPUT_MARKERS`
#: above, and the same line-start anchoring: an arbitrary repo's command is free
#: to exit 4 for a real failure (pytest itself uses 4 for a usage error), so the
#: number alone must never be enough to excuse a red suite.
BASELINE_RED_OUTPUT_MARKER = "RESULT: BASELINE-RED"

#: Exit code ``scripts/coord-test-runner.sh`` reserves for baseline-red.
#: Documented, and — like :data:`RUNNER_INFRA_EXIT` — deliberately not trusted
#: on its own.
RUNNER_BASELINE_RED_EXIT = 4


def is_infrastructure_failure(returncode: int, output: str) -> bool:
    """True when a build/test command never actually ran the suite (#1814).

    Two signals, either of which is enough:

    * a bare shell ``command not found`` (:data:`SHELL_NOT_FOUND_EXIT`) — the
      universal one, and the exact shape of the bug that motivated this
      (``cargo: command not found`` inside the ``coord-serve`` daemon);
    * one of :data:`INFRA_OUTPUT_MARKERS` in the output — the runner's own
      explicit, deliberately unmistakable statement that it could not run.

    Note what is *not* a signal: :data:`RUNNER_INFRA_EXIT` on its own. Our
    runner always prints a marker alongside it, and an arbitrary repo's
    command may already use 3 for a real failure, so the number adds nothing
    and risks laundering a red build into "could not run".

    Deliberately narrow in the same spirit: this never guesses from a generic
    substring like "not found", which appears in ordinary assertion messages.
    Misclassifying a real failure is the worse error of the two — it is the
    one that could eventually launder a merge — so the ambiguous cases all
    fall through to "this is a verdict".

    The marker check is anchored to the START of a line, not a bare substring
    search over the whole blob (#1814 review). `coord-test-runner.sh`'s own
    ``say()`` always emits a marker as the first characters of a line it
    prints — but for `claude-coordinator` itself, ``test_command`` is that
    runner's full ``pytest`` arm, which (as of this fix) contains tests whose
    literal assertion text and parametrize IDs embed these exact marker
    strings (see ``tests/test_coord_test_runner_toolchain.py`` and
    ``tests/test_revalidate.py``). If any of those specific tests ever fails
    for an unrelated reason, pytest's ``FAILED tests/...::test[MARKER...]``
    summary line and ``E     assert 'MARKER...' in '...'`` diff both contain
    the marker text too — but never at the start of a line: pytest indents
    diff lines with ``E   ``/spaces and prefixes summary lines with
    ``FAILED ``, and the runner's own re-run dumps
    (``coord-test-runner.sh``'s ``tail -n 40 ... | sed 's/^/      /'``) are
    explicitly indented before being echoed. A bare substring match would
    misclassify that unrelated Python failure as infrastructure and hide it
    behind "fix the runner environment"; anchoring to line-start does not.
    """
    if returncode == SHELL_NOT_FOUND_EXIT:
        return True
    lines = (output or "").splitlines()
    return any(
        line.startswith(marker) for line in lines for marker in INFRA_OUTPUT_MARKERS
    )


def is_baseline_red_failure(returncode: int, output: str) -> bool:
    """True when a test command failed but every failure also fails on the
    merge-base (#2170) — i.e. the machine's baseline is red.

    ONE signal only, and it is the runner's own explicit statement: a
    :data:`BASELINE_RED_OUTPUT_MARKER` line. :data:`RUNNER_BASELINE_RED_EXIT` is
    NOT a signal on its own, for the same reason exit 3 isn't for infrastructure
    — an arbitrary repo's `test_command` may exit 4 for a perfectly genuine
    failure (pytest's own usage-error code is 4), and keying on the number would
    relabel real red suites as "not the branch's fault". That is the dangerous
    direction: it is the one that could eventually launder a regression into a
    merge, so it must require the runner to have said so in words.

    Line-anchored rather than a bare substring search, exactly as
    :func:`is_infrastructure_failure` documents at length: for this repo the
    ``test_command`` IS that runner's pytest arm, so the suite contains tests
    (``tests/test_coord_test_runner_baseline.py``, and this module's own) whose
    assertion text and parametrize ids embed this marker verbatim. Those only
    ever appear indented (``E   ``/spaces) or prefixed (``FAILED ``), never at
    the start of a line.

    ``returncode`` is accepted and ignored for signature symmetry with
    :func:`is_infrastructure_failure`; callers pass what they have.
    """
    lines = (output or "").splitlines()
    return any(line.startswith(BASELINE_RED_OUTPUT_MARKER) for line in lines)


def _baseline_red_reason(stage: str, returncode: int) -> str:
    """Operator-facing wording for a red baseline (#2170).

    Must not say "SUITE FAILED" (the operator would debug a branch that is
    fine), must not say "could not run" (it ran, and told us something), and
    must name the BASELINE as the thing to fix. The failure that motivated it
    read as a branch problem for months and was not one.
    """
    return (
        f"revalidation reports a RED BASELINE — the {stage} command failed "
        f"(exit {returncode}), but every failure reproduces on the merge-base "
        "in this environment, so the branches made nothing worse and NONE of "
        "them was judged. Nothing merged, nothing marked failed, no verdict "
        "changed. Fix the baseline (or this machine's environment) and re-run: "
        "a machine whose suite cannot go green cannot produce a Test verdict "
        "for any branch (#2170)"
    )


@dataclass
class RevalidationResult:
    """Outcome of one composite revalidation run.

    ``ok`` is the only thing the merge path branches on: ``True`` means fresh
    ``passed`` verdicts were recorded for every candidate and ``process()`` may
    now find their smoke gate satisfied; ``False`` means every candidate is
    left exactly as it was — still blocked, never merged.

    ``kind`` classifies a failure (#1715) so :func:`revalidate_group` can tell
    "this branch broke it" from "nothing here could ever have run".

    ``validated_base_sha`` (#2829) is ``origin/<target_branch>``'s SHA at the
    moment this run's worktree was built — ``None`` when no worktree was ever
    created (every :data:`KIND_SETUP` return). It is the anchor an unattended
    caller re-checks immediately before merging (see
    :func:`revalidated_base_still_current`): a caller that takes no lock
    while the suite runs must confirm, right before it acts on ``recorded``,
    that this SHA is still what the base actually is — otherwise it would be
    merging a tree the suite never validated.
    """

    ok: bool
    reason: str = ""
    output: str = ""
    composed: list[str] = field(default_factory=list)
    recorded: list[str] = field(default_factory=list)
    worktree: Path | None = None
    kind: str = KIND_OK
    validated_base_sha: str | None = None

    def __bool__(self) -> bool:  # pragma: no cover — convenience only
        return self.ok

    @property
    def narrowable(self) -> bool:
        """True when re-running the candidates one at a time could help."""
        return not self.ok and self.kind in NARROWABLE_KINDS


@dataclass
class BatchRevalidationResult:
    """Outcome of one ``(repo, target_branch)`` group's revalidation (#1715).

    A green composite is the whole story: ``composite.ok`` and ``per_entry``
    empty, ``suite_runs == 1`` however many candidates there were — that count
    is the entire point of the feature and the black-box tests assert it
    directly.

    A red composite fills ``per_entry`` with one solo result per candidate.
    ``recorded`` then holds only the survivors' assignment ids, and
    ``culprits`` names the branches whose own run failed.

    ``conflicted`` (#2231) is the subset of the culprits whose run died at
    :data:`KIND_COMPOSE` — the branch does not merge onto its current base at
    all. That is categorically different from every other culprit: no verdict
    is wrong, no suite is red, and no amount of re-testing can change it. It
    carries the *candidate* (not just its label) precisely so a caller can act
    on it — see :func:`coord.commands.merge._dispatch_revalidation_conflicts`,
    which turns it into #241's ``conflict-fix`` dispatch. Before #2231 this
    diagnosis was formatted for a human and then dropped on the floor.
    """

    composite: RevalidationResult
    per_entry: list[tuple[str, RevalidationResult]] = field(default_factory=list)
    recorded: list[str] = field(default_factory=list)
    culprits: list[str] = field(default_factory=list)
    suite_runs: int = 0
    conflicted: list[tuple[RevalidationCandidate, RevalidationResult]] = field(
        default_factory=list,
    )

    @property
    def ok(self) -> bool:
        """True when every candidate came out with a fresh verdict."""
        return self.composite.ok

    @property
    def fell_back(self) -> bool:
        return bool(self.per_entry)


class _Echo:
    """Null echo so the library is usable without a Click context."""

    def __call__(self, msg: str = "") -> None:  # pragma: no cover
        return None


def local_repo_dir(config, repo_name: str) -> Path | None:
    """Resolve the base checkout for *repo_name*.

    Same resolution ``coord test`` uses (``coord.commands.test_gate.
    _local_repo_dir``): this machine's ``repo_paths`` first, then any machine
    in the config that knows the repo. Returns an expanded :class:`Path`, or
    ``None`` when no path is configured.
    """
    import socket

    hostname = socket.gethostname().split(".")[0]
    local_machine = next(
        (
            m for m in getattr(config, "machines", [])
            if m.name == hostname or m.host.split(".")[0] == hostname
        ),
        None,
    )
    repo_path = None
    if local_machine is not None:
        repo_path = local_machine.repo_path(repo_name)
    if repo_path is None:
        for m in getattr(config, "machines", []):
            repo_path = m.repo_path(repo_name)
            if repo_path:
                break
    return Path(repo_path).expanduser() if repo_path else None


def revalidation_worktree_path(
    repo_name: str, target_branch: str, slug: str | None = None,
) -> Path:
    """Throwaway worktree for a composite revalidation run.

    Under ``~/.coord/revalidate-worktrees/`` — OUTSIDE the base checkout, for
    the #561 reason: the base checkout doubles as the live editable coordinator
    source on the daemon host, so moving its branch silently downgrades the
    running ``coord`` until somebody restores it.

    *slug* (#1715) distinguishes the per-entry fallback runs from the composite
    they follow. Without it every solo run would reuse — and therefore delete —
    the failed composite's worktree, which :func:`format_failure` has just
    told the operator was "kept for inspection".
    """
    from coord.state import COORD_DIR

    name = f"{repo_name}-{target_branch.replace('/', '-')}"
    if slug:
        name += f"--{slug.replace('/', '-')}"
    return COORD_DIR / "revalidate-worktrees" / name


def _run(
    args: list[str], *, cwd: Path, timeout: int | None = 300
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
    )


def _rev_parse(repo_dir: Path, ref: str) -> str | None:
    """Resolve *ref* to a full SHA in *repo_dir*, or ``None``."""
    try:
        res = _run(["git", "rev-parse", ref], cwd=repo_dir, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    sha = res.stdout.strip()
    return sha or None


def _remove_worktree(repo_dir: Path, wt_path: Path) -> None:
    """Best-effort removal (+ prune of the admin refs), mirroring ``coord test``.

    Falls back to a plain directory delete: the path can survive as an
    orphaned tree when the worktree was registered against a *different* base
    checkout (a re-cloned repo, a moved ``repo_path``), and ``git worktree
    add`` refuses a path that already exists — which would wedge every future
    revalidation for that (repo, target) pair.
    """
    for args in (
        ["git", "worktree", "remove", "--force", str(wt_path)],
        ["git", "worktree", "prune"],
    ):
        try:
            _run(args, cwd=repo_dir, timeout=60)
        except (subprocess.SubprocessError, OSError):
            pass
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)


def describe_candidates(candidates: list[RevalidationCandidate]) -> list[str]:
    """One operator-readable line per candidate, for ``--dry-run`` output."""
    lines: list[str] = []
    for c in candidates:
        e = c.entry
        lines.append(
            f"  revalidate: {e.repo_name} #{e.issue_number} ({e.branch} → "
            f"{e.target_branch}) — "
            f"{c.smoke.short_reason or 'verdict no longer covers the base'}"
        )
    return lines


def group_candidates(
    candidates: list[RevalidationCandidate],
) -> list[tuple[tuple[str, str], list[RevalidationCandidate]]]:
    """Split *candidates* into the batches that will each cost one suite run.

    Keyed by ``(repo_name, target_branch)`` — one composite can only be built
    per base, so that pair is exactly the batch boundary. Sorted so the
    ``--dry-run`` preview and the real run enumerate the batches identically.
    """
    groups: dict[tuple[str, str], list[RevalidationCandidate]] = {}
    for c in candidates:
        groups.setdefault(
            (c.entry.repo_name, c.entry.target_branch), [],
        ).append(c)
    return sorted(groups.items())


def describe_batches(
    candidates: list[RevalidationCandidate],
) -> list[str]:
    """``--dry-run`` preview: the batches, their members, and the run count.

    #1715 requires the dry run to "name the batch members and state plainly
    that one composed run will validate all of them" — an operator has to be
    able to see, *before* committing 7 minutes, exactly which branches are
    about to be composed together and that they cost one suite run rather
    than one each.
    """
    lines: list[str] = []
    batches = group_candidates(candidates)
    for (repo_name, target_branch), group in batches:
        n = len(group)
        if n == 1:
            lines.append(
                f"  --revalidate: (dry run) {repo_name} → {target_branch}: "
                "1 entry, 1 suite run against the current base:"
            )
        else:
            lines.append(
                f"  --revalidate: (dry run) {repo_name} → {target_branch}: "
                f"BATCH of {n} — all {n} branches would be composed onto "
                f"origin/{target_branch} together and validated by ONE "
                f"composed suite run (not {n}):"
            )
        lines.extend(describe_candidates(group))
    total = len(candidates)
    lines.append(
        f"  --revalidate: (dry run) {total} entry(ies) in "
        f"{len(batches)} batch(es) — {len(batches)} suite run(s), "
        "then merge. Nothing has been run and no verdict written."
    )
    if any(len(g) > 1 for _, g in batches):
        lines.append(
            "  --revalidate: (dry run) a composed run validates the "
            "COMPOSITE, not each branch alone — every member already holds "
            "its own passed verdict, so this re-confirms they still hold "
            "together against the current base. If the composite fails, "
            "nothing merges and each branch is then re-tested alone to find "
            "the culprit."
        )
    return lines


def revalidate(
    candidates: list[RevalidationCandidate],
    config,
    *,
    echo=None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    runner=None,
    worktree_slug: str | None = None,
) -> RevalidationResult:
    """Compose every candidate branch onto the current base, run the suite once.

    On success, records a fresh ``passed`` Test-gate verdict for each
    candidate's work assignment. ``coord.state.record_test_verdict`` re-stamps
    the #1479 freshness anchors (``test_base_sha``/``test_head_sha``/
    ``test_patch_id``) as part of that write, so the verdict is anchored to the
    base the composite was actually validated against — which is the whole
    point, and is why this cannot be done by hand-editing ``test_state``.

    On **any** failure — a branch that will not compose, a build failure, a
    test failure, a timeout, a missing local checkout or an unconfigured test
    command — **no verdict is written at all** and every candidate is left
    blocked. There is no partial credit: this must never become a laundering
    path for a verdict that would not pass against the current base.

    All candidates must share one ``(repo_name, target_branch)`` pair — the
    caller groups them (``coord merge`` already processes the queue in exactly
    those groups). A mixed list is refused rather than silently validating a
    composite that means nothing.

    *runner* (testing seam) replaces the ``build``/``test`` command execution:
    ``runner(command: str, cwd: Path) -> subprocess.CompletedProcess``-alike
    with ``returncode`` and ``stdout``/``stderr``. Defaults to a real
    ``subprocess.run(shell=True)``.

    *worktree_slug* (#1715) namespaces the throwaway worktree, so a per-entry
    fallback run does not delete the failed composite's kept-for-inspection
    tree.
    """
    echo = echo or _Echo()
    if not candidates:
        return RevalidationResult(ok=True, reason="no revalidation candidates")

    repos = {c.entry.repo_name for c in candidates}
    targets = {c.entry.target_branch for c in candidates}
    if len(repos) != 1 or len(targets) != 1:
        return RevalidationResult(
            ok=False,
            kind=KIND_SETUP,
            reason=(
                "revalidation candidates span more than one "
                f"(repo, target_branch): repos={sorted(repos)} "
                f"targets={sorted(targets)} — refusing to validate a "
                "composite that spans bases"
            ),
        )

    # Every candidate must name the row whose verdict we would re-record,
    # checked BEFORE the suite runs. Discovering this afterwards used to abort
    # mid-write, having already recorded a fresh verdict for the candidates
    # ahead of the bad one — a partial write that contradicts the "on any
    # failure, no verdict is written at all" contract three paragraphs up.
    # Failing here also saves the operator a ~7-minute suite run that could
    # never have been banked.
    for c in candidates:
        if not c.work_assignment_id:
            return RevalidationResult(
                ok=False,
                kind=KIND_SETUP,
                reason=(
                    f"{c.entry.repo_name} #{c.entry.issue_number}: the stale "
                    "verdict names no work assignment, so no fresh verdict "
                    "can be recorded — entry stays blocked"
                ),
            )
    repo_name = repos.pop()
    target_branch = targets.pop()

    repo_cfg = config.repo(repo_name) if config is not None else None
    if repo_cfg is None:
        return RevalidationResult(
            ok=False, kind=KIND_SETUP,
            reason=f"no repo config for {repo_name!r}",
        )
    # #2091: revalidation WRITES a test verdict, so it is bound by the same
    # rule as the Test stage — a verdict is only worth what the suite behind
    # it is worth. When the repo declares what CI runs, re-verify with THAT;
    # otherwise nothing changes (`ci_command` is unset for every repo that
    # has not opted in). Deliberately narrower than
    # `coord.smoke.resolve_smoke_command`: this path has never consulted
    # `smoke_tests.default_command` and #2091 is not the issue to change that.
    # `getattr`: *config* here is duck-typed (`coord merge --revalidate` passes
    # the real Config; tests and the daemon pass lighter stand-ins), so a
    # hard attribute read would turn a missing shim into an AttributeError
    # mid-revalidation.
    test_command = (
        getattr(repo_cfg, "ci_command", None) or ""
    ).strip() or repo_cfg.test_command
    if not test_command:
        # Refusing here is the safe direction: with nothing to run, "passed"
        # would be a claim about a suite that never executed — the exact lie
        # #1738's escalation wording goes out of its way not to invite.
        return RevalidationResult(
            ok=False,
            kind=KIND_SETUP,
            reason=(
                f"no test_command configured for {repo_name!r} — cannot "
                "revalidate (recording a verdict for a suite that never ran "
                "is never correct)"
            ),
        )

    repo_dir = local_repo_dir(config, repo_name)
    if repo_dir is None or not repo_dir.exists():
        return RevalidationResult(
            ok=False,
            kind=KIND_SETUP,
            reason=(
                f"no local checkout for {repo_name!r} on this machine "
                f"({repo_dir or 'no repo_path configured'}) — revalidation "
                "runs the suite locally, so it must run where the repo lives "
                "(the daemon host, via `coord merge --revalidate`)"
            ),
        )

    branches = [c.entry.branch for c in candidates]
    echo(
        f"  --revalidate: composing {len(branches)} branch(es) onto "
        f"origin/{target_branch} and running the suite once (#1715 option 3)"
    )

    wt_path = revalidation_worktree_path(repo_name, target_branch, worktree_slug)
    _remove_worktree(repo_dir, wt_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        fetched = _run(["git", "fetch", "origin", "--prune"], cwd=repo_dir)
    except (subprocess.SubprocessError, OSError) as e:
        return RevalidationResult(
            ok=False, kind=KIND_SETUP, reason=f"git fetch failed: {e}",
        )
    if fetched.returncode != 0:
        return RevalidationResult(
            ok=False, kind=KIND_SETUP,
            reason=f"git fetch failed: {fetched.stderr.strip()}",
        )

    added = _run(
        ["git", "worktree", "add", "--force", "--detach",
         str(wt_path), f"origin/{target_branch}"],
        cwd=repo_dir,
    )
    if added.returncode != 0:
        return RevalidationResult(
            ok=False,
            kind=KIND_SETUP,
            reason=(
                f"could not create the revalidation worktree at "
                f"origin/{target_branch}: {added.stderr.strip()}"
            ),
        )

    # #2829: the base every return from here on actually validated against —
    # stamped on every `RevalidationResult` below (success or failure) so an
    # unattended caller that runs this whole function with no lock held can,
    # right before it acts on a green result, re-check this SHA is still
    # current (see `revalidated_base_still_current`). Read once, right after
    # the worktree is built from it, rather than re-derived later — this IS
    # the base the composed tree and the suite run actually saw.
    base_sha = _rev_parse(repo_dir, f"origin/{target_branch}")

    composed: list[str] = []
    # `git merge` (not rebase) with an explicit commit: we only need a tree
    # that contains every candidate's content on top of the current base.
    # Nothing here is ever pushed — the worktree is thrown away below and
    # `coord merge` still does the real merge through `gh` afterwards.
    for branch in branches:
        merged = _run(
            ["git", "merge", "--no-ff", "--no-edit", f"origin/{branch}"],
            cwd=wt_path,
        )
        if merged.returncode != 0:
            _run(["git", "merge", "--abort"], cwd=wt_path)
            return RevalidationResult(
                ok=False,
                kind=KIND_COMPOSE,
                reason=(
                    f"branch {branch!r} does not compose onto "
                    f"origin/{target_branch} (conflict) — resolve the "
                    "conflict before revalidating"
                ),
                output=_tail(merged.stdout + "\n" + merged.stderr),
                composed=list(composed),
                worktree=wt_path,
                validated_base_sha=base_sha,
            )
        composed.append(branch)
        echo(f"    composed {branch}")

    run_cmd = runner or _shell_runner
    build_command = getattr(repo_cfg, "build_command", None)
    if build_command:
        echo(f"    running build: {build_command}")
        try:
            built = run_cmd(build_command, wt_path, timeout)
        except subprocess.TimeoutExpired:
            return RevalidationResult(
                ok=False,
                kind=KIND_TIMEOUT,
                reason=f"revalidation build timed out after {timeout}s",
                composed=list(composed),
                worktree=wt_path,
                validated_base_sha=base_sha,
            )
        if built.returncode != 0:
            build_output = (built.stdout or "") + "\n" + (built.stderr or "")
            infra = is_infrastructure_failure(built.returncode, build_output)
            baseline_red = not infra and is_baseline_red_failure(
                built.returncode, build_output
            )
            return RevalidationResult(
                ok=False,
                kind=(
                    KIND_INFRA if infra
                    else KIND_BASELINE_RED if baseline_red
                    else KIND_BUILD
                ),
                reason=(
                    _infra_reason("build", built.returncode)
                    if infra
                    else _baseline_red_reason("build", built.returncode)
                    if baseline_red
                    else (
                        "revalidation BUILD FAILED against the current base "
                        f"(exit {built.returncode}) — every candidate stays "
                        "blocked"
                    )
                ),
                output=_tail(build_output),
                composed=list(composed),
                worktree=wt_path,
                validated_base_sha=base_sha,
            )

    echo(f"    running tests: {test_command}")
    try:
        tested = run_cmd(test_command, wt_path, timeout)
    except subprocess.TimeoutExpired:
        return RevalidationResult(
            ok=False,
            kind=KIND_TIMEOUT,
            reason=f"revalidation suite timed out after {timeout}s",
            composed=list(composed),
            worktree=wt_path,
            validated_base_sha=base_sha,
        )
    if tested.returncode != 0:
        test_output = (tested.stdout or "") + "\n" + (tested.stderr or "")
        infra = is_infrastructure_failure(tested.returncode, test_output)
        baseline_red = not infra and is_baseline_red_failure(
            tested.returncode, test_output
        )
        return RevalidationResult(
            ok=False,
            kind=(
                KIND_INFRA if infra
                else KIND_BASELINE_RED if baseline_red
                else KIND_SUITE
            ),
            reason=(
                _infra_reason("suite", tested.returncode)
                if infra
                else _baseline_red_reason("suite", tested.returncode)
                if baseline_red
                else (
                    "revalidation SUITE FAILED against the current base "
                    f"(exit {tested.returncode}) — every candidate stays "
                    "blocked, nothing merged"
                )
            ),
            output=_tail(test_output),
            composed=list(composed),
            worktree=wt_path,
            validated_base_sha=base_sha,
        )

    # ── Suite green: record the fresh verdicts ──────────────────────────────
    from coord.state import record_test_staleness_anchor, record_test_verdict

    # The commits this run ACTUALLY validated, read from the local refs the
    # worktree was built from — not re-discovered from GitHub afterwards. See
    # `record_test_staleness_anchor`'s docstring for why that distinction is
    # load-bearing rather than an optimisation. Same value as `base_sha`
    # above (nothing has fetched again since); reuse it rather than
    # re-deriving so the RESULT's `validated_base_sha` and the ANCHOR written
    # to the DB can never disagree.
    validated_base_sha = base_sha

    recorded: list[str] = []
    composite_note = (
        "revalidated by `coord merge --revalidate` — composite of "
        + ", ".join(composed)
        + f" onto origin/{target_branch}"
    )
    for c in candidates:
        # Non-empty for every candidate: checked up front, before the suite
        # ran, precisely so this loop cannot abort part-way through having
        # already written some of the verdicts.
        aid = c.work_assignment_id
        record_test_verdict(assignment_id=aid, test_state="passed")
        record_test_staleness_anchor(
            assignment_id=aid,
            test_head_sha=_rev_parse(repo_dir, f"origin/{c.entry.branch}"),
            test_base_sha=validated_base_sha,
            # #1475's patch-id is GitHub's compare diff hashed — reproducing it
            # from a local `git diff` is not guaranteed byte-identical, and a
            # WRONG patch-id would read as "content unchanged" for content that
            # did change. NULL is the fail-closed value the gate already
            # understands ("cannot confirm identical content"), so a branch that
            # moves after this run re-blocks on SHA alone, exactly as a
            # pre-#1475 row does.
            test_patch_id=None,
        )
        recorded.append(aid)
        echo(
            f"    recorded fresh Test verdict for {c.entry.repo_name} "
            f"#{c.entry.issue_number} ({aid})"
        )

    _remove_worktree(repo_dir, wt_path)
    return RevalidationResult(
        ok=True,
        reason=composite_note,
        composed=composed,
        recorded=recorded,
        worktree=None,
        validated_base_sha=validated_base_sha,
    )


def _infra_reason(stage: str, returncode: int) -> str:
    """Operator-facing wording for a run that never happened (#1814).

    Every clause here is load-bearing. It must not contain the words "SUITE
    FAILED" (the operator would go and debug a branch that is fine), it must
    say out loud that the branch is unjudged rather than bad, and it must name
    the environment as the thing to fix — because the failure that motivated
    it (``cargo: command not found`` inside the ``coord-serve`` systemd user
    unit) reads like a branch problem and is not one.

    ``returncode`` alone does not always mean "exited immediately without
    running anything" — :func:`is_infrastructure_failure` can also classify
    on an :data:`INFRA_OUTPUT_MARKERS` hit at a returncode that isn't
    :data:`SHELL_NOT_FOUND_EXIT` (e.g. the runner's own ``RESULT: INFRA``
    line at exit 3, or a wrapped/nonstandard exit). The wording branches on
    that so it never overclaims "without running anything" for a run whose
    own output says it merely couldn't complete.
    """
    if returncode == SHELL_NOT_FOUND_EXIT:
        run_desc = f"the {stage} command exited {returncode} without running anything"
    else:
        run_desc = (
            f"the {stage} command's own output reported it could not run "
            f"(exit {returncode})"
        )
    return (
        f"revalidation COULD NOT RUN — {run_desc} (missing toolchain / broken "
        "runner environment, NOT a test failure). This says nothing about the "
        "branches: they keep their existing verdicts and stay blocked, and "
        "nothing merged. Fix the runner environment and re-run — a systemd "
        "user unit's PATH is not a login shell's (see #1814)"
    )


def _label(candidate: RevalidationCandidate) -> str:
    e = candidate.entry
    return f"{e.repo_name} #{e.issue_number} ({e.branch})"


def revalidate_group(
    candidates: list[RevalidationCandidate],
    config,
    *,
    echo=None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    runner=None,
) -> BatchRevalidationResult:
    """Revalidate one ``(repo, target_branch)`` group: composite, then narrow.

    This is the #1715 entry point and the one ``coord merge --revalidate``
    calls. It is a thin policy layer over :func:`revalidate`:

    1. **Compose all N and run the suite once.** Green — which is the
       overwhelmingly common case, since every candidate already holds a
       ``passed`` verdict from an earlier base — and the group is done at a
       cost of exactly **one** suite run, however large N was. That single
       number is the whole feature.

    2. **Red composite: merge nothing, fail nothing, narrow.** No verdict was
       written (:func:`revalidate` is all-or-nothing), so no entry can merge
       off the back of it, and no entry is marked failed either — a composite
       failure is evidence about the *set*, not a verdict on any member. Each
       candidate is then re-run **alone** against the current base. A branch
       that passes solo earns a real verdict and merges; a branch that fails
       solo is the culprit and stays blocked with its own failure quoted.

    N = 1 never falls back: the "composite" already *was* that single branch,
    so a second run would be the identical run twice. That keeps this path
    byte-identical to #1769's shipped single-entry behaviour.

    A composite that failed for a **common-mode** reason (no ``test_command``,
    no local checkout, a dead ``git fetch``, candidates spanning two bases, or
    a missing toolchain — #1814's :data:`KIND_INFRA`) never falls back either
    — see :data:`NARROWABLE_KINDS`. Every solo run would hit the same wall, so
    narrowing would turn one clear error into N identical ones. For the INFRA
    case that matters twice over: N solo runs would each print "could not
    run", making a broken daemon environment look like N broken branches.

    Worst case is therefore 1 + N runs, the bound #1715 specifies, and it is
    reached only when a composite genuinely fails on a real build/test/merge
    problem.
    """
    echo = echo or _Echo()
    if not candidates:
        return BatchRevalidationResult(
            composite=RevalidationResult(
                ok=True, reason="no revalidation candidates",
            ),
        )

    composite = revalidate(
        candidates, config, echo=echo, timeout=timeout, runner=runner,
    )
    # A setup refusal never reached a build/test command, so it did not cost a
    # suite run; an INFRA failure reached it but the suite still never
    # executed (#1814). Everything else did (or died trying), and the
    # operator's mental model of "how many suites did that just run" should
    # match.
    ran = 0 if composite.kind in NO_SUITE_RAN_KINDS else 1
    batch = BatchRevalidationResult(
        composite=composite,
        recorded=list(composite.recorded),
        suite_runs=ran,
    )

    if composite.ok or len(candidates) == 1 or not composite.narrowable:
        # #2231: a single-candidate composite IS that candidate's own run, so
        # a COMPOSE failure here names it as surely as a solo run would. The
        # N>1 case is attributed in the per-entry loop below instead — the
        # composite stops at the FIRST branch that won't merge, which says
        # nothing about the ones after it.
        if (
            len(candidates) == 1
            and composite.kind == KIND_COMPOSE
        ):
            batch.conflicted.append((candidates[0], composite))
        return batch

    echo(
        f"  --revalidate: the composite of {len(candidates)} branches FAILED — "
        "nothing merges on that result. Re-running each branch on its own "
        "against the current base to find the culprit (#1715); branches that "
        "pass alone still merge."
    )

    for c in candidates:
        label = _label(c)
        echo(f"  --revalidate: re-testing {label} alone")
        solo = revalidate(
            [c], config, echo=echo, timeout=timeout, runner=runner,
            # Its own worktree: the failed composite's tree was just advertised
            # as "kept for inspection", and reusing the path would delete it.
            worktree_slug=c.work_assignment_id or c.entry.branch,
        )
        if solo.kind != KIND_SETUP:
            batch.suite_runs += 1
        batch.per_entry.append((label, solo))
        if solo.ok:
            batch.recorded.extend(solo.recorded)
            echo(f"  --revalidate: {label} PASSES alone — cleared to merge")
        else:
            batch.culprits.append(label)
            echo(f"  --revalidate: {label} FAILS alone — {solo.reason}")
            if solo.kind == KIND_COMPOSE:
                # #2231: not a verdict problem at all — this branch cannot
                # merge. Recorded structurally so the caller can dispatch
                # #241's conflict-fix instead of leaving the entry parked
                # behind a stale-verdict gate it can never clear.
                batch.conflicted.append((c, solo))

    if not batch.culprits:
        # Every branch is green by itself, yet together they are not. That is a
        # genuine cross-branch interaction (two branches that each compile
        # against the old base but not against each other), and it is the one
        # case where the per-entry pass is *less* conservative than the
        # composite it replaced. Say so out loud rather than letting a clean
        # per-entry sweep quietly imply the composite was a fluke.
        echo(
            "  --revalidate: WARNING — every branch passes alone but the "
            "composite of all of them failed. That points at an interaction "
            "between these branches rather than at any one of them; they are "
            "merging on their solo verdicts. Re-run the suite on the base "
            "afterwards."
        )

    return batch


def recorded_validated_base_shas(
    batch: BatchRevalidationResult,
    candidates: list[RevalidationCandidate],
) -> dict[str, str | None]:
    """Map each RECORDED candidate to the base SHA the run that actually
    cleared it validated against (#2829).

    Only :data:`coord.serve_app._auto_revalidate_tick` — the unattended
    caller — needs this: an attended ``coord merge --revalidate`` acts on
    ``batch.recorded`` immediately, under the same lock the whole call was
    already made under, so there is no gap for the base to move in. The
    unattended tick runs the composite (and any per-entry fallback) with NO
    lock held at all, so before it merges anything it must re-confirm, via
    :func:`revalidated_base_still_current`, that each recorded id's base is
    still what THIS mapping says was validated.

    A clean composite validates every candidate against the SAME base
    (``batch.composite.validated_base_sha``). A composite that fell back to
    per-entry narrowing (#1715) took one run per candidate, each
    conceivably (if rarely) against a slightly later ``git fetch`` — so
    each survivor gets its OWN solo run's SHA instead. ``batch.per_entry``
    is built by :func:`revalidate_group` iterating *candidates* in order,
    appending exactly one entry per candidate whether it passed or failed
    (never skipped, never reordered) — so zipping the two by position
    recovers the correspondence without re-deriving the ``_label`` string
    matching :func:`format_batch` uses for display.

    Only recorded (merge-eligible) candidates appear in the result — a
    candidate that never got a fresh verdict has nothing for a caller to
    gate before merging.
    """
    recorded = set(batch.recorded)
    result: dict[str, str | None] = {}
    if not batch.fell_back:
        for c in candidates:
            if c.work_assignment_id in recorded:
                result[c.work_assignment_id] = batch.composite.validated_base_sha
        return result
    for c, (_label, solo) in zip(candidates, batch.per_entry):
        if solo.ok and c.work_assignment_id in recorded:
            result[c.work_assignment_id] = solo.validated_base_sha
    return result


def revalidated_base_still_current(
    config, repo_name: str, target_branch: str, validated_base_sha: str | None,
) -> bool:
    """True when *validated_base_sha* is still ``origin/<target_branch>``'s
    HEAD — the base-SHA recheck an unattended caller must run immediately
    before merging anything a lock-free composite just validated (#2829).

    THE CORRECTNESS CRUX. :func:`coord.serve_app._auto_revalidate_tick` runs
    the (potentially 30-minute) composite with `_merge_lock` released, so
    another merge can move the base while it runs. A green composite is a
    claim about the tree it was BUILT from, not about whatever
    ``origin/<target_branch>`` happens to be by the time the suite finishes.
    Skipping this check would trade the liveness bug (a sleeping fleet
    wedged for 30+ minutes behind one composite) for a soundness bug
    (merging a tree the suite never actually ran against) — which is worse,
    because it fails silently instead of just being slow.

    Fetches fresh (mirrors :func:`revalidate`'s own first step) rather than
    trusting whatever the caller's board/queue snapshot says, since the
    whole point is to catch a move that happened AFTER that snapshot was
    taken. Fail-closed: a ``None`` SHA (nothing was ever validated — e.g. a
    :data:`KIND_SETUP` result), a missing/unreachable local checkout, or a
    failed fetch all return ``False``. "Cannot confirm the base is
    unchanged" must never be treated as "confirmed unchanged" on a path
    that is about to merge with nobody watching — the caller's response to
    ``False`` is simply to skip merging this tick; the next tick's
    :func:`coord.merge_queue.plan` will see the recorded verdict's anchor no
    longer matches the (now different) base, call it stale again, and
    ``_auto_revalidate_tick`` will re-revalidate it against the new base —
    exactly the "discard the result and let the next tick retry" behaviour
    the issue calls for, with no explicit revert needed.
    """
    if not validated_base_sha:
        return False
    repo_dir = local_repo_dir(config, repo_name)
    if repo_dir is None or not repo_dir.exists():
        return False
    try:
        fetched = _run(["git", "fetch", "origin", "--prune"], cwd=repo_dir)
    except (subprocess.SubprocessError, OSError):
        return False
    if fetched.returncode != 0:
        return False
    current = _rev_parse(repo_dir, f"origin/{target_branch}")
    return current is not None and current == validated_base_sha


def compose_conflict_error(entry) -> str:
    """The ``entry.error`` text for a branch that will not compose (#2231).

    Deliberately worded to be classified correctly by the two consumers that
    read merge prose rather than structured state:

    * :func:`coord.merge_queue.classify_conflict` must return ``"rebaseable"``
      — it is what decides whether ``coord merge`` dispatches #241's
      conflict-fix worker — so the text carries the ``merge conflict`` signal
      that function already keys on.
    * :func:`coord.merge_queue.is_stale_smoke_reason` (and ``coord drive``'s
      ``_SMOKE_GATE_MARKERS``) must NOT match, or the driver would read this
      as the #1738 stale-verdict shape and spend a fix round re-testing a
      branch that cannot merge — the exact loop #2231 reports. So the stale
      verdict is *described* here in wording that matches none of
      :data:`coord.merge_queue.STALE_SMOKE_MARKERS` (this module must not
      carry a copy of those strings either — see
      ``TestSingleStaleDetector``). ``tests/test_revalidate.py`` asserts both
      classifications directly, since the wording is load-bearing rather than
      cosmetic.
    """
    return (
        f"merge conflict: branch {entry.branch!r} does not compose onto "
        f"origin/{entry.target_branch} — `git merge` reported a content "
        "conflict when --revalidate composed it against the current base, so "
        "no test verdict (fresh or otherwise) can clear this entry until the "
        "branch is rebased (#2231)"
    )


def format_batch(batch: BatchRevalidationResult) -> list[str]:
    """Operator-facing OUTCOME lines for one group's revalidation (#1715).

    Deliberately excludes the composite's own failure report — that is
    :func:`format_failure`'s job and belongs on stderr, whereas "…and here is
    what merged anyway" is ordinary stdout. Keeping them separate is why the
    caller does not have to route a "PASSED alone — merging" line to stderr
    just because the composite that preceded it was red.

    A per-entry (solo) failure is different: it names the actual culprit, and
    the worktree :func:`revalidate` kept for it (per ``worktree_slug``) is the
    one an operator would actually inspect — the composite's own kept
    worktree is a different tree entirely. #1715-review: that pointer used to
    be silently dropped here, even though :func:`format_failure` already knew
    how to print it. Reuse it (skipping its leading reason line, which
    :func:`format_batch` already renders with the branch label attached).
    """
    lines: list[str] = []
    if batch.composite.ok:
        lines.append(f"  --revalidate: PASSED — {batch.composite.reason}")
        return lines

    if batch.composite.kind == KIND_INFRA:
        # #1814: the one failure mode that is not about the branches at all.
        # Say so on stdout too — the reason line goes to stderr, and an
        # operator skimming the merge summary must not be left with a red
        # composite and no explanation that it judged nothing.
        lines.append(
            "  --revalidate: INFRASTRUCTURE FAILURE — the suite could not "
            "run, so no branch was judged. Nothing merged, nothing marked "
            "failed, no verdict changed; every candidate is exactly as it "
            "was. Fix the runner environment, then re-run --revalidate."
        )
        return lines

    if batch.composite.kind == KIND_BASELINE_RED:
        # #2170: the OTHER failure mode that is not about the branches. Same
        # stdout-as-well-as-stderr treatment as INFRA above and for the same
        # reason — but a different instruction, because the fix is in the
        # baseline (or this machine's environment), not the runner's toolchain.
        lines.append(
            "  --revalidate: RED BASELINE — the suite failed, but identically "
            "on the merge-base, so no branch was judged. Nothing merged, "
            "nothing marked failed, no verdict changed; every candidate is "
            "exactly as it was. Fix the baseline, then re-run --revalidate."
        )
        return lines

    if not batch.fell_back:
        return lines

    for label, solo in batch.per_entry:
        if solo.ok:
            lines.append(f"  --revalidate: {label}: PASSED alone — merging")
        else:
            lines.append(f"  --revalidate: {label}: BLOCKED — {solo.reason}")
            lines.extend(format_failure(solo)[1:])
    if batch.culprits:
        lines.append(
            "  --revalidate: culprit(s): " + ", ".join(batch.culprits)
        )
    lines.append(f"  --revalidate: {batch.suite_runs} suite run(s) total")
    return lines


# #1924: every guard var `serve_app.py` sets on *itself* to keep a daemon
# command handler from re-routing its own request back to the daemon (see
# ``daemon_reroute_target()`` in board_service.py and its call sites in
# commands/merge.py, commands/status.py, commands/acceptance.py,
# commands/gates.py, commands/lifecycle.py). These are process-global — set
# with a plain ``os.environ[...] = "1"`` around the handler body, not scoped
# to the request — so when `coord merge --revalidate` is invoked from a thin
# client, routed to the daemon, and its composed-suite subprocess inherits
# the parent's environment by default, the suite sees whichever of these
# happened to be set on `coord serve`'s own process at the time (in
# particular `COORD_MERGE_ON_DAEMON`, set for the very request that is
# running this revalidation). The suite is supposed to behave exactly like a
# clean checkout's test run; a leaked guard var makes tests that assert on
# these vars fail regardless of what the branch under test contains. Kept as
# an explicit tuple (not a dynamic "*_ON_DAEMON" glob over os.environ) so
# adding a new guard var in serve_app.py is a visible, deliberate edit here
# too, rather than something that's silently swept up or silently missed.
_DAEMON_GUARD_ENV_VARS = (
    "COORD_MERGE_ON_DAEMON",
    "COORD_RECONCILE_ON_DAEMON",
    "COORD_DIAGNOSE_ON_DAEMON",
    "COORD_GATES_ON_DAEMON",
    "COORD_TEST_PLAN_ON_DAEMON",
    "COORD_HOUSEKEEPING_ON_DAEMON",
    "COORD_NOTIFY_ON_DAEMON",
    "COORD_ACCEPTANCE_ON_DAEMON",
)


def _suite_subprocess_env() -> dict[str, str]:
    """``os.environ`` minus the daemon-internal routing guards (#1924).

    The composed-suite subprocess should look like a clean checkout's test
    run irrespective of whether the parent process invoking it happens to be
    a bare shell or `coord serve` mid-request. See ``_DAEMON_GUARD_ENV_VARS``.
    """
    return {
        k: v for k, v in os.environ.items() if k not in _DAEMON_GUARD_ENV_VARS
    }


def _shell_runner(command: str, cwd: Path, timeout: int):
    """Run *command* through the shell in *cwd*, capturing output.

    Same shape as ``coord test``'s build/test step (``subprocess.run(cmd,
    shell=True, cwd=worktree)``) — the repo's own configured command, run in
    the composite worktree, inheriting the environment — MINUS the
    daemon-internal routing guards (#1924), which must never leak into a
    subprocess that is supposed to behave like a clean checkout.
    """
    return subprocess.run(
        command, shell=True, cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, env=_suite_subprocess_env(),
    )


def format_failure(result: RevalidationResult) -> list[str]:
    """Operator-facing lines for a failed revalidation (blocked, not merged)."""
    lines = [f"  --revalidate: {result.reason}"]
    if result.output:
        lines.append("  --revalidate: output tail:")
        lines.extend(
            "      " + ln for ln in result.output.splitlines()
        )
    if result.worktree is not None:
        lines.append(
            f"  --revalidate: worktree kept for inspection: {result.worktree}"
        )
    return lines


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "KIND_BUILD",
    "KIND_COMPOSE",
    "KIND_BASELINE_RED",
    "KIND_INFRA",
    "KIND_OK",
    "KIND_SETUP",
    "KIND_SUITE",
    "KIND_TIMEOUT",
    "MAX_REVALIDATION_BATCH",
    "NARROWABLE_KINDS",
    "NO_SUITE_RAN_KINDS",
    "RUNNER_BASELINE_RED_EXIT",
    "RUNNER_INFRA_EXIT",
    "SHELL_NOT_FOUND_EXIT",
    "BatchRevalidationResult",
    "RevalidationResult",
    "client_timeout_seconds",
    "compose_conflict_error",
    "describe_batches",
    "describe_candidates",
    "format_batch",
    "format_failure",
    "group_candidates",
    "is_baseline_red_failure",
    "is_infrastructure_failure",
    "local_repo_dir",
    "recorded_validated_base_shas",
    "revalidate",
    "revalidate_group",
    "revalidated_base_still_current",
    "revalidation_worktree_path",
]
