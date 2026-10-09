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
    """
    path = _store_path(record.repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(path.with_suffix(".lock"))
    with lock:
        rows = _load_raw(record.repo)
        rows.append(asdict(record))
        tmp = path.with_suffix(".tmp")
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
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


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
    - else (every step in the group passed) -> ``passed=True``.

    ``checked_at`` is the group's LATEST step timestamp — so a run that
    hasn't finished all its steps yet is never mistaken, by a caller
    comparing timestamps, for one that finished more recently than it
    actually did.

    Multiple runs of the same ``(artifact, sha)`` (a re-run after a fix, or
    simply two nightly ticks against an unmoved release SHA) each produce
    their OWN group/result here — ``_nightly_artifact_step``'s own
    most-recently-checked-wins logic (already tested, untouched) is what
    picks among them, exactly as it already does for the ``--from-json``
    seam's possibly-multiple entries for one lane/artifact.
    """
    groups: dict[tuple[str, str], list[NightlyResultRecord]] = {}
    for record in read_nightly_results(repo):
        groups.setdefault((record.artifact, record.sha), []).append(record)

    out: list[NightlyArtifactResult] = []
    for (artifact, sha), rows in groups.items():
        checked_at = max(r.checked_at for r in rows)
        unavailable_rows = [r for r in rows if r.unavailable]
        failing_rows = [r for r in rows if not r.passed and not r.unavailable]
        if unavailable_rows:
            detail = "; ".join(sorted({
                f"{r.spec}::{r.step}: {r.detail or 'unavailable'}" for r in unavailable_rows
            }))
            out.append(NightlyArtifactResult(
                artifact=artifact, sha=sha, passed=False, unavailable=True,
                detail=detail, checked_at=checked_at,
            ))
        elif failing_rows:
            detail = "; ".join(sorted({
                f"{r.spec}::{r.step}: {r.detail or 'failed'}" for r in failing_rows
            }))
            out.append(NightlyArtifactResult(
                artifact=artifact, sha=sha, passed=False, detail=detail,
                checked_at=checked_at,
            ))
        else:
            out.append(NightlyArtifactResult(
                artifact=artifact, sha=sha, passed=True,
                detail=f"{len(rows)} step(s) passed", checked_at=checked_at,
            ))
    return out
