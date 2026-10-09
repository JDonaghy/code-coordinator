"""The nightly real-platform smoke results store (#3660).

#3652 shipped the decision core (:mod:`coord.nightly_smoke`) and the
release-gate step (:mod:`coord.release_gate`'s :class:`NightlyArtifactResult`)
but nothing that actually PERSISTED a run — the gate's only reader was
``coord release gate --from-json``, a hand-authored file an operator had to
build by hand every time. This module is the production store :func:`coord
.nightly_runner.run_nightly_smoke` writes to and
``coord release gate`` reads from when ``--from-json`` is omitted (#3660
acceptance: "``coord release gate --repo vimcode`` reads them without
``--from-json``").

One flat JSON file per repo, under ``<coord-dir>/nightly_results/<repo>.json``
— a plain list of records, each carrying everything
:func:`coord.release_gate.evaluate_release_gate`'s ``_nightly_artifact_step``
needs (``artifact``, ``sha``, ``passed``, ``detail``, ``checked_at``,
``unavailable``) plus enough provenance (``spec``, ``step``, ``host``,
``evidence``) that a human browsing the file — or a future ``coord status``
surface — can see WHICH step of WHICH spec actually produced a given
artifact-level verdict, without a second store.

Append-only from the writer's point of view: :func:`record_nightly_result`
never mutates or removes an existing row. The release gate's own staleness
rule (#2096: "a result at some OTHER sha is too stale to certify this
release") needs the FULL history to pick the most recent ``(artifact, sha)``
row from — exactly the same selection
:func:`coord.release_gate._nightly_artifact_step` already does for the
``--from-json`` seam (:func:`nightly_artifact_results_for_release_gate`
hands every row through unfiltered and lets that existing, already-tested
logic decide, so the two reading paths can never silently disagree about
what "the latest nightly result" means — #2096 "one question, one answer").

**#3660 review round 1: a re-run must be able to CLEAR a poisoned
``(artifact, sha)``.** Every :class:`NightlyResultRecord` carries a
``run_id`` — one fresh value per :func:`coord.nightly_runner.
run_nightly_smoke` invocation, shared by every step THAT run persists.
:func:`nightly_artifact_results_for_release_gate` groups by
``(artifact, sha, run_id)``, not just ``(artifact, sha)`` — so two runs at
the same unmoved release SHA (an operator re-running after unlocking a
host, or simply two nightly ticks against a release that hasn't cut yet)
produce TWO separate :class:`~coord.release_gate.NightlyArtifactResult`
entries, each carrying its own run's ``checked_at``, and
``_nightly_artifact_step``'s already-tested "most recently checked wins"
logic genuinely has something to pick between. Before ``run_id`` existed,
every row at a given ``(artifact, sha)`` collapsed into ONE group forever —
a single ``unavailable=True`` row from a locked host on run 1 kept the
gate red even after a clean run 2, because there was nothing in the stored
data to tell the two runs apart.

**#3660 review round 2: a group must be known to be a COMPLETE run before
it may certify anything.** Per-run grouping alone made a *truncated* run
dangerous: :func:`coord.nightly_runner.run_nightly_smoke` persists one row
per step, so a 2-step run whose second step died mid-flight (a
``RuntimeError`` out of the filing call, a Ctrl-C, a kill) left a group
holding only its *passing* first row, which reduced to ``passed=True,
"1 step(s) passed"`` with a fresh ``checked_at`` and outranked the complete
red run at the same SHA. Every row therefore carries ``steps_total`` — the
step count the run knew before its loop started — and
:func:`nightly_artifact_results_for_release_gate` refuses to report
``passed=True`` for a group holding fewer rows than that (#2096: "a gate
must be able to fail"; an unfinished run is reported as
``unavailable`` — "the run did not finish, re-run it" — never as a pass).

A :class:`coord.filelock.FileLock` guards the read-modify-write cycle: more
than one ``coord smoke nightly`` run (different repos, different hosts, a
cron timer racing a human's on-demand invocation) can be in flight at once,
and a lost update here would silently drop a real observation — exactly the
kind of "unconfirmed success" #2096 exists to rule out.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from coord.filelock import FileLock
from coord.release_gate import NightlyArtifactResult


def _store_dir() -> Path:
    from coord.platform_paths import default_coord_dir  # noqa: PLC0415

    return default_coord_dir() / "nightly_results"


def _store_path(repo: str) -> Path:
    return _store_dir() / f"{repo}.json"


def _lock_path(store_path: Path) -> Path:
    """The lock file for *store_path*, next to it — never derived with
    ``Path.with_suffix`` (#3660 review nit): that replaces the LAST suffix
    of the *repo name* itself, so a repo literally named ``a.b`` would
    collide with a repo named ``a`` (both lock at ``a.lock``). Appending
    (never replacing) is collision-free for every repo name."""
    return store_path.parent / (store_path.name + ".lock")


def _tmp_path(store_path: Path) -> Path:
    """Same append-not-replace reasoning as :func:`_lock_path`, for the
    temp-file-then-``rename`` swap's scratch file."""
    return store_path.parent / (store_path.name + ".tmp")


@dataclass(frozen=True)
class NightlyResultRecord:
    """One persisted nightly-smoke observation: :class:`~coord.release_gate.
    NightlyArtifactResult`'s fields plus the provenance a human browsing the
    store (or a future status surface) wants — which spec/step produced it,
    which host ran it, and where its evidence lives.

    ``checked_at`` has no default (mirrors :class:`coord.nightly_smoke.
    NightlyStepObservation`'s own deliberate choice — see that class's
    docstring) — #2096 "unconfirmed success is a defect" applies just as
    much to a persisted record as to an in-memory one: a caller that forgot
    to timestamp an observation must get a loud ``TypeError``, never a
    plausible-looking row with a silently-defaulted "now".
    """

    repo: str
    artifact: str
    sha: str
    passed: bool
    checked_at: float
    detail: str = ""
    unavailable: bool = False
    spec: str = ""
    step: str = ""
    host: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)
    #: One fresh value per :func:`coord.nightly_runner.run_nightly_smoke`
    #: invocation, shared by every step that run persists — the grouping
    #: key :func:`nightly_artifact_results_for_release_gate` needs to tell
    #: "two runs at the same (artifact, sha)" apart (#3660 review round 1).
    #: Defaults to ``""`` (never required) so a row persisted before this
    #: field existed still reads back — it just groups with any OTHER
    #: ``run_id=""`` row for the same ``(artifact, sha)``, which is exactly
    #: the (already-shipped) pre-fix behaviour for that legacy data.
    run_id: str = ""
    #: How many steps the run that produced this row was going to persist
    #: in total — known to :func:`coord.nightly_runner.run_nightly_smoke`
    #: from ``len(observations)`` BEFORE its act/persist loop starts, and
    #: stamped identically onto every row of that run. The completeness
    #: marker :func:`nightly_artifact_results_for_release_gate` needs to
    #: tell "every step of this run passed" apart from "the only step this
    #: run managed to persist before dying passed" (#3660 review round 2).
    #: ``0`` means "unstated" — a legacy row written before this field
    #: existed, or a writer that genuinely doesn't know; such a group is
    #: graded exactly as it was before this field existed (no completeness
    #: check), because inventing a step count for it would be a guess.
    steps_total: int = 0
    #: The issue :func:`coord.nightly_smoke.process_nightly_step` filed or
    #: updated for THIS step, once acting on the already-persisted
    #: observation above has actually happened — ``None`` until then (a
    #: clean step, a dry run, or a row whose acting step hasn't completed
    #: yet/crashed before reporting back). Deliberately NOT part of the
    #: persist-before-act write `record_nightly_result` makes (#3660 review
    #: round 2's crash-safety property): the observation itself (``passed``/
    #: ``detail``/``unavailable``) is known and must survive a crash BEFORE
    #: the filing call ever runs, but the issue number is only known AFTER
    #: it returns. :func:`set_nightly_issue_number` anneals it onto the
    #: already-written row as a separate, best-effort step — #3661's status
    #: surface (:mod:`coord.nightly_status`) wants "which issue(s)" for a
    #: red result, and losing this annotation to a crash between acting and
    #: annealing only costs that one extra link, never the underlying
    #: pass/fail verdict, which the gate already had durably.
    issue_number: int | None = None

    def to_nightly_artifact_result(self) -> NightlyArtifactResult:
        """The exact shape :func:`coord.release_gate.evaluate_release_gate`
        grades — never re-derived ad hoc at a call site (#2096 "one
        question, one answer")."""
        return NightlyArtifactResult(
            artifact=self.artifact,
            sha=self.sha,
            passed=self.passed,
            detail=self.detail,
            checked_at=self.checked_at,
            unavailable=self.unavailable,
        )


def _load_raw(repo: str) -> list[dict]:
    path = _store_path(repo)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        # A corrupt/unreadable store degrades to "no history recorded" —
        # never a crash on a timer-driven writer, and never silently
        # treated as "this artifact passed" either: an empty history makes
        # `_nightly_artifact_step` report "no result recorded", a FAILING
        # step (#2096: a gate must be able to fail).
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def record_nightly_result(record: NightlyResultRecord) -> None:
    """Append *record* to its repo's store, durably.

    Read-modify-write under a :class:`~coord.filelock.FileLock` (the
    store's own lock file, next to the JSON — never the JSON file itself,
    so a reader never observes a lock's own empty/placeholder bytes), and
    written via a temp-file-then-``rename`` swap so a reader never observes
    a partially-written file.

    Deliberately ONE write per observation, not one batched write per run
    (#3660 review round 2): the whole point of persisting before acting is
    that an observation already taken survives whatever the acting step
    does next, and a batched end-of-run write would hand that back — a run
    killed mid-loop would persist nothing at all. The cost is N
    read-modify-write cycles for an N-step spec, on a file holding one
    small row per step, written once a night.
    """
    path = _store_path(record.repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(_lock_path(path))
    with lock:
        rows = _load_raw(record.repo)
        rows.append(asdict(record))
        tmp = _tmp_path(path)
        tmp.write_text(json.dumps(rows, indent=2, sort_keys=True))
        tmp.replace(path)


def read_nightly_results(repo: str) -> list[NightlyResultRecord]:
    """Every record ever persisted for *repo*, oldest-first, skipping any
    row that doesn't actually parse as one (#2096: a malformed row is
    dropped, never guessed at or allowed to crash every other reader of a
    shared file the rest of the fleet keeps writing to)."""
    out: list[NightlyResultRecord] = []
    for row in _load_raw(repo):
        try:
            out.append(
                NightlyResultRecord(
                    repo=row["repo"],
                    artifact=row["artifact"],
                    sha=row["sha"],
                    passed=bool(row["passed"]),
                    checked_at=float(row["checked_at"]),
                    detail=row.get("detail", ""),
                    unavailable=bool(row.get("unavailable", False)),
                    spec=row.get("spec", ""),
                    step=row.get("step", ""),
                    host=row.get("host", ""),
                    evidence=tuple(row.get("evidence") or ()),
                    run_id=row.get("run_id", ""),
                    steps_total=int(row.get("steps_total", 0) or 0),
                    issue_number=(
                        int(row["issue_number"])
                        if row.get("issue_number") not in (None, "")
                        else None
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def set_nightly_issue_number(
    *, repo: str, run_id: str, spec: str, step: str, issue_number: int,
) -> bool:
    """Anneal *issue_number* onto the already-persisted row(s) matching
    ``(repo, run_id, spec, step)`` — a best-effort, lock-guarded
    read-modify-write distinct from :func:`record_nightly_result`'s
    append (#3661).

    Never invents a row: if nothing matches (a legacy run with no
    ``run_id``, or a store that was cleared between the observation and
    this call), this is a no-op that returns ``False`` rather than
    appending a fabricated record — the caller already has a durable,
    correctly-graded row from the persist-before-act write; this only ever
    adds provenance to it, never a substitute for it. Updates every
    matching row (normally exactly one — one row per ``(spec, step)`` per
    run) so a retried/duplicated call stays idempotent.
    """
    if not run_id:
        # A legacy/unset run_id groups with every other such row for this
        # (artifact, sha) — annealing onto ONE of them would be a guess
        # about which, so this deliberately does nothing rather than
        # picking at random.
        return False
    path = _store_path(repo)
    lock = FileLock(_lock_path(path))
    with lock:
        rows = _load_raw(repo)
        matched = False
        for row in rows:
            if (
                row.get("run_id") == run_id
                and row.get("spec") == spec
                and row.get("step") == step
            ):
                row["issue_number"] = issue_number
                matched = True
        if not matched:
            return False
        tmp = _tmp_path(path)
        tmp.write_text(json.dumps(rows, indent=2, sort_keys=True))
        tmp.replace(path)
        return True


def nightly_artifact_results_for_release_gate(repo: str) -> list[NightlyArtifactResult]:
    """``coord release gate``'s seam (#3660 acceptance): *repo*'s persisted
    nightly-smoke history, reduced to one :class:`~coord.release_gate.
    NightlyArtifactResult` per ``(artifact, sha)`` run — the exact
    per-artifact granularity :func:`coord.release_gate.
    evaluate_release_gate`'s ``_nightly_artifact_step`` already grades (it
    answers "did the artifact pass", not "did this one step pass", and
    picks the single most-recently-checked result among whatever it's
    given for a ``(artifact, sha)`` pair — see that function's own
    docstring).

    :func:`record_nightly_result` persists ONE ROW PER STEP (the richer
    granularity #3660's own Persist requirement asks for: "per repo, SHA,
    artifact, step, with timestamps and evidence paths" — useful for a
    human/future status surface debugging WHICH step broke). This function
    is the one place that collapses a ``(artifact, sha)`` run's steps back
    into the single pass/fail/unavailable verdict the gate's own
    ``_nightly_artifact_step`` expects (#2096 "one question, one answer" —
    this grouping happens exactly once, here, rather than once per caller
    that reads the raw per-step rows):

    - any step in the group recorded ``unavailable=True`` (a locked/absent
      GUI session or missing display, #3510) -> the group reports
      ``unavailable=True, passed=False`` — an environment condition, not
      an app bug, named in ``detail``;
    - else any step failed -> ``passed=False``, ``detail`` names the
      failing step(s);
    - else the group holds FEWER rows than its own ``steps_total`` says
      the run had -> ``unavailable=True, passed=False`` — "the run did not
      finish", an environment/interruption condition rather than an app
      bug, and emphatically not a pass (#3660 review round 2, #2096 "a
      gate must be able to fail"). This is the case a mid-run crash
      produces: the runner persists each observation BEFORE acting on it,
      so a step whose filing call raised still leaves its real (red) row
      behind — but a run killed before a later step was ever OBSERVED
      leaves only earlier rows, and "some steps passed" must never be read
      as "all steps passed". A group whose rows all say ``steps_total=0``
      (legacy data, or a writer that doesn't know) is graded without this
      check — the pre-#3660-round-2 behaviour, since there is nothing to
      compare against and guessing a count would be worse;
    - else (every step of a complete run passed) -> ``passed=True``.

    ``checked_at`` is the group's LATEST step timestamp — so a run that
    hasn't finished all its steps yet is never mistaken, by a caller
    comparing timestamps, for one that finished more recently than it
    actually did.

    Multiple runs of the same ``(artifact, sha)`` (a re-run after a fix, or
    simply two nightly ticks against an unmoved release SHA) each produce
    their OWN group/result here — grouped by ``(artifact, sha, run_id)``,
    not just ``(artifact, sha)`` (#3660 review round 1: without ``run_id``
    every run at one SHA collapsed into a single group forever, so one
    ``unavailable``/failing row from an earlier run could never be cleared
    by a later clean one). ``_nightly_artifact_step``'s own
    most-recently-checked-wins logic (already tested, untouched) is what
    picks among the resulting entries, exactly as it already does for the
    ``--from-json`` seam's possibly-multiple entries for one lane/artifact.
    A legacy row with no ``run_id`` (persisted before this fix) groups with
    every other such row for the same ``(artifact, sha)`` — the same
    (imperfect, but no worse than before) behaviour that data always had.

    Two final, deliberate rules about picking BETWEEN a SHA's groups
    (#3660 review round 2's non-blocking items — both about a run that
    never observed anything outranking one that did):

    - a group that reports ``unavailable`` (a locked host, or an
      unfinished run) is dropped entirely when some OTHER group at the
      same ``(artifact, sha)`` is a complete pass. The SHA is the same
      artifact, bit for bit, so a completed passing observation of it
      stays true; a never-ran tick tonight must not flip a genuinely
      verified artifact to "unlock the host and re-run". When no complete
      pass exists at that SHA, the ``unavailable`` entry survives
      untouched and still blocks the gate.
    - entries are returned newest-``checked_at``-first, and among equal
      timestamps the NOT-passing one first — so
      ``_nightly_artifact_step``'s ``max(..., key=checked_at)`` (which
      keeps the FIRST maximal element it sees) resolves a tie
      conservatively and deterministically, rather than by whichever row
      happened to be written to the file first.
    """
    groups: dict[tuple[str, str, str], list[NightlyResultRecord]] = {}
    for record in read_nightly_results(repo):
        groups.setdefault((record.artifact, record.sha, record.run_id), []).append(record)

    out: list[NightlyArtifactResult] = []
    for (artifact, sha, _run_id), rows in groups.items():
        checked_at = max(r.checked_at for r in rows)
        verdict = _classify_group(rows)
        out.append(NightlyArtifactResult(
            artifact=artifact, sha=sha, passed=verdict.passed,
            unavailable=verdict.unavailable, detail=verdict.detail,
            checked_at=checked_at,
        ))
    return _ranked(out)


@dataclass(frozen=True)
class _GroupVerdict:
    """The pass/fail/unavailable judgement for one ``(artifact, sha,
    run_id)`` group's rows — factored out of
    :func:`nightly_artifact_results_for_release_gate` so
    :func:`latest_nightly_runs` (#3661's status surface) grades a group
    exactly the same way rather than re-deriving the rule (#2096 "one
    question, one answer": these are two different QUESTIONS — "does this
    sha pass" vs. "what did the most recent run observe" — but the same
    sub-question, "given these rows, what's the verdict", must have one
    answer)."""

    passed: bool
    unavailable: bool
    detail: str


def _classify_group(rows: list[NightlyResultRecord]) -> _GroupVerdict:
    unavailable_rows = [r for r in rows if r.unavailable]
    failing_rows = [r for r in rows if not r.passed and not r.unavailable]
    steps_total = max(r.steps_total for r in rows)
    if unavailable_rows:
        detail = "; ".join(sorted({
            f"{r.spec}::{r.step}: {r.detail or 'unavailable'}" for r in unavailable_rows
        }))
        return _GroupVerdict(passed=False, unavailable=True, detail=detail)
    if failing_rows:
        detail = "; ".join(sorted({
            f"{r.spec}::{r.step}: {r.detail or 'failed'}" for r in failing_rows
        }))
        return _GroupVerdict(passed=False, unavailable=False, detail=detail)
    if steps_total and len(rows) < steps_total:
        # The run died (an exception out of the filing call, a Ctrl-C, a
        # kill) before persisting every step it set out to observe.
        # Reported as "did not finish" — blocking, environment-flavoured
        # (never an app-bug red), and never a pass (#3660 review round 2 /
        # #2096 "a gate must be able to fail").
        return _GroupVerdict(
            passed=False, unavailable=True,
            detail=(
                f"nightly run did not finish: only {len(rows)} of "
                f"{steps_total} step(s) were observed and recorded — a "
                "partial run can never certify this artifact; re-run it"
            ),
        )
    return _GroupVerdict(
        passed=True, unavailable=False, detail=f"{len(rows)} step(s) passed",
    )


@dataclass(frozen=True)
class NightlyRunSummary:
    """The most recently OBSERVED nightly run for one ``(repo, artifact)``
    — #3661's status surface (:mod:`coord.nightly_status`) input.

    Deliberately NOT scoped to a particular ``sha`` (unlike
    :class:`~coord.release_gate.NightlyArtifactResult`, which
    :func:`nightly_artifact_results_for_release_gate` answers "does THIS
    sha have a passing result" for): ``coord status`` and the ``GET
    /board`` status-bar segment want "what did the fleet last actually
    observe for this artifact, whatever sha it ran at, and how long ago" —
    a different question, answered by :func:`latest_nightly_runs` below,
    which reuses the SAME per-group judgement (:func:`_classify_group`)
    rather than re-deriving it.
    """

    repo: str
    artifact: str
    sha: str
    passed: bool
    unavailable: bool
    detail: str
    checked_at: float
    host: str
    #: Every distinct, non-``None`` issue number any row in the winning
    #: group carries (annealed by :func:`set_nightly_issue_number`) —
    #: sorted ascending, deduped. Empty when nothing was ever filed/known
    #: (a clean pass, or a filing call whose annealing never landed).
    issue_numbers: tuple[int, ...] = field(default_factory=tuple)
    #: How many of the group's rows are real (non-unavailable) failures —
    #: the "count" half of "red with the count and issue links" (#3661).
    failing_step_count: int = 0


def latest_nightly_runs(repo: str) -> dict[str, NightlyRunSummary]:
    """The single most recently-observed run per artifact for *repo*,
    across every sha this repo has ever been nightly-smoked at.

    One flat scan, grouped by ``(artifact, sha, run_id)`` exactly like
    :func:`nightly_artifact_results_for_release_gate`, but picking the
    group with the LATEST ``checked_at`` per artifact rather than the
    group matching a caller-given sha — the release gate asks "is THIS sha
    good", this asks "what's the latest thing the fleet observed, and how
    long ago" (#3661's staleness question). An artifact with no rows at
    all is simply absent from the returned mapping — the caller (
    :mod:`coord.nightly_status`) decides what "never ran" means, this
    module only reports what it found.
    """
    groups: dict[tuple[str, str, str], list[NightlyResultRecord]] = {}
    for record in read_nightly_results(repo):
        groups.setdefault((record.artifact, record.sha, record.run_id), []).append(record)

    best: dict[str, NightlyRunSummary] = {}
    for (artifact, sha, _run_id), rows in groups.items():
        checked_at = max(r.checked_at for r in rows)
        existing = best.get(artifact)
        if existing is not None and existing.checked_at >= checked_at:
            continue
        verdict = _classify_group(rows)
        newest_row = max(rows, key=lambda r: r.checked_at)
        issue_numbers = tuple(sorted({
            r.issue_number for r in rows if r.issue_number is not None
        }))
        failing_step_count = sum(
            1 for r in rows if not r.passed and not r.unavailable
        )
        best[artifact] = NightlyRunSummary(
            repo=repo, artifact=artifact, sha=sha, passed=verdict.passed,
            unavailable=verdict.unavailable, detail=verdict.detail,
            checked_at=checked_at, host=newest_row.host,
            issue_numbers=issue_numbers, failing_step_count=failing_step_count,
        )
    return best


def _ranked(results: list[NightlyArtifactResult]) -> list[NightlyArtifactResult]:
    """Apply the two between-group rules documented at the end of
    :func:`nightly_artifact_results_for_release_gate`: an ``unavailable``
    (never-ran / unfinished) entry is dropped where a COMPLETE PASS for
    the same ``(artifact, sha)`` exists, and the surviving entries are
    ordered newest-first with not-passing ahead of passing on a tie — the
    order :func:`coord.release_gate._nightly_artifact_step`'s ``max(...,
    key=checked_at)`` resolves ties by."""
    passing_keys = {(r.artifact, r.sha) for r in results if r.passed}
    kept = [
        r for r in results
        if not (r.unavailable and (r.artifact, r.sha) in passing_keys)
    ]
    kept.sort(key=lambda r: (-(r.checked_at or 0.0), r.passed))
    return kept
