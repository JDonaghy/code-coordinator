"""#2247: predicted file-overlap ORDERING for the drive queue.

``coord/brain.py``'s planning prompt tells an LLM not to assign two issues
that would touch the same files — but that rule lives on the ``coord plan`` →
``coord approve`` path, which unattended work never takes. The drive queue
dispatches through ``coord drive-queue add`` → ``tick`` → ``coord drive`` and
has never consulted it, so the queue would happily run two issues that edit
the same file; the collision only surfaced after one merged and the other had
burned its attempts (2026-08-14: quadraui #306/#307/#308/#309 all appending to
one test file; claude-coordinator #2230/#2234 both editing
``coord/drive_queue.py``).

This module is the *prediction* half. #1720's :mod:`coord.overlap_fence` is a
sibling but NOT the same thing: that one runs at dispatch, is advisory prose
in the worker's briefing, and never changes what runs when. This one runs at
enqueue and changes the ORDER.

THREE DESIGN RULES, all load-bearing:

1. **ORDER, never REFUSE.** A false-positive prediction that blocked a
   dispatch would be worse than the conflict it prevents — it stops work that
   would have been fine, for a reason nobody can verify. A false negative just
   returns us to today. So an overlap chains the newcomer ``--after`` the work
   already in flight (a flag the queue already has) and says why. Serializing
   costs latency; refusing costs work.

2. **Check a guess against GROUND TRUTH where one exists.** Work already in
   flight has a real diff — one compare call away via
   :func:`coord.github_ops.get_compare_files`, the same seam #1720 uses. So
   only ONE side of the comparison is a guess. Where the other side is *also*
   only queued (no branch yet), this module compares DECLARED file lists only
   — both authors wrote the list down; that is two statements, not two
   guesses. It never infers a footprint from prose.

3. **No prediction is a valid answer.** An issue with no ``## Files`` block
   and no in-flight branch yields an empty :class:`Prediction`, and the caller
   behaves exactly as it did before this module existed. Every fetch here
   fails open to "no prediction" — an unreachable board, an unreadable body, a
   force-pushed branch: none of them may change an enqueue.

DELIBERATELY NOT IMPLEMENTED: graph-derived prediction. The issue lists
``graphify`` as a second source for a candidate's file list, below the
author's own declaration. The seam for it is
:func:`collect_candidate_files`'s ``extra_sources`` argument; nothing wires
one up yet, because rule 3 says a missing prediction is fine and a *bad* one
is not. Ship the cheap, exact source first and let the recorded accuracy
(see :func:`classify_outcome`) justify adding a fuzzier one.

DELIBERATELY NOT IMPLEMENTED (#2601): a hardcoded exclusion list for
"universal" directory tokens (``tests/``, ``docs/``) that every issue touches.
It was considered — #2601 found that a single bare ``tests/`` declaration
chained one issue behind fourteen others that each named a specific,
disjoint file — but excluding it outright would make rule 1 lie: those
fourteen ARE what :func:`paths_overlap`'s directory rule says they are, a
match. The chosen fix is :func:`fanout_warnings`, which leaves the ORDER
alone and instead tells the author their token was too broad to carry much
signal, so they can narrow it on the next ``add``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from coord.drive_state import WORK_LIKE
from coord.drive_queue import entry_key

# Statuses whose branch can no longer collide with a newcomer — see
# :func:`inflight_assignments`. A subset of
# ``coord.drive_state.TERMINAL_STATUSES``: that set counts ``done`` as
# terminal (the tick's question is "is this issue still being worked"), and
# ``done`` is precisely the state a finished-but-unmerged PR sits in.
LANDED_STATUSES = frozenset(
    {"merged", "cancelled", "advisory", "refused_policy", "failed"}
)

# Where one side's file list came from. Recorded on every overlap so a later
# reader can weigh a branch-vs-declared match (half ground truth) differently
# from a declared-vs-declared one (two statements of intent).
SOURCE_BRANCH = "branch"
SOURCE_DECLARED = "declared"

# Audit-log coordinates for the two events this feature emits. Both are
# business tier: an order change to real dispatched work is a board decision,
# not tick housekeeping, and it must survive `audit.level = business`.
AUDIT_CATEGORY = "drive-queue"
EVENT_PREDICTED = "overlap_predicted"
EVENT_SCORED = "overlap_scored"

# Outcome verdicts from :func:`classify_outcome`.
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_FALSE_POSITIVE = "false-positive"
OUTCOME_UNKNOWN = "unknown"


# ── declared-file parsing ────────────────────────────────────────────────────
#
# The one prediction source that is not a guess: an explicit block the issue
# author wrote. Recognised forms, all of which the refinement / new-issue chat
# paths can emit without a schema:
#
#     ## Files                     ### Files touched            Files:
#     - `coord/drive_queue.py`     * coord/drive_queue.py       files: a.py, b.py
#     - coord/state.py               tests/test_x.py
#
# and a fenced block under such a heading. Anything else — prose, a sentence
# that merely mentions a filename — is NOT a declaration and yields nothing.

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*files\b\s*:?\s*(.*)$", re.IGNORECASE)
_INLINE_RE = re.compile(r"^\s{0,3}(?:\*\*)?files(?:\s+\w+)?(?:\*\*)?\s*:\s*(.*)$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s{0,6}[-*+]\s+(.*)$")
_FENCE_RE = re.compile(r"^\s{0,3}```")
_ANY_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")

# Cheap sanity bound: a path token longer than this is prose that happened to
# contain no space, never a repo path.
_MAX_PATH_LEN = 240


def _clean_path(token: str) -> str:
    """Normalise one candidate path token, or ``""`` if it isn't one.

    Conservative on purpose (rule 3): every rejection here costs at most a
    prediction we didn't have before, while every false acceptance would
    serialize unrelated work.
    """
    text = str(token or "").strip()
    # A bullet may carry a trailing explanation: "`a/b.py` — why". Take the
    # first whitespace-delimited token; the rest is prose by construction.
    text = text.split()[0] if text.split() else ""
    # ONE combined strip, not a sequence of them: "`a.py`," needs the comma
    # and the backtick taken off in either order, and two ordered strips only
    # ever get one of those cases right.
    text = text.strip(" \t`\"'()[]<>,;:*")
    text = text.lstrip("/")
    while text.startswith("./"):
        text = text[2:]
    if not text or len(text) > _MAX_PATH_LEN:
        return ""
    if text.lower().startswith(("http://", "https://")):
        return ""
    if "#" in text:  # an issue reference ("#2247"), not a path
        return ""
    # A repo path has a directory separator or an extension. "TODO" doesn't.
    if "/" not in text and "." not in text:
        return ""
    if text in {".", "..", "/"}:
        return ""
    return text


def parse_declared_files(body: str | None) -> list[str]:
    """File paths the issue author explicitly declared, in declaration order.

    Returns ``[]`` for a body with no recognised block — which is the common
    case and an entirely valid answer (rule 3). Never raises: a malformed
    body degrades to "no declaration", not to an exception on the enqueue
    path.
    """
    try:
        lines = str(body or "").splitlines()
    except Exception:  # noqa: BLE001 — a body is never worth an exception here
        return []

    out: list[str] = []
    seen: set[str] = set()

    def take(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            out.append(path)

    def take_inline(rest: str) -> None:
        for chunk in re.split(r"[,\s]+", rest or ""):
            take(_clean_path(chunk))

    index = 0
    while index < len(lines):
        line = lines[index]
        heading = _HEADING_RE.match(line)
        inline = None if heading else _INLINE_RE.match(line)
        if heading is None and inline is None:
            index += 1
            continue

        take_inline((heading or inline).group(1))
        index += 1
        in_fence = False
        # Consume the block: bullets, fenced lines and bare paths. Stop at the
        # next heading or at the first line that is plainly prose, so a "##
        # Files" section followed by a paragraph doesn't swallow the paragraph.
        while index < len(lines):
            entry = lines[index]
            if _FENCE_RE.match(entry):
                in_fence = not in_fence
                index += 1
                continue
            if in_fence:
                take(_clean_path(entry))
                index += 1
                continue
            if not entry.strip():
                index += 1
                continue
            if _ANY_HEADING_RE.match(entry):
                break
            bullet = _BULLET_RE.match(entry)
            if bullet is not None:
                cleaned = _clean_path(bullet.group(1))
                if not cleaned:
                    break
                take(cleaned)
                index += 1
                continue
            cleaned = _clean_path(entry)
            if not cleaned:
                break
            take(cleaned)
            index += 1
    return out


# ── footprints ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Footprint:
    """One side of a comparison: whose files these are and how we know."""

    key: str
    issue_number: int
    files: tuple[str, ...]
    source: str = SOURCE_DECLARED
    branch: str = ""


@dataclass(frozen=True)
class Overlap:
    """A predicted collision with ONE other piece of work."""

    key: str
    source: str
    files: tuple[str, ...]
    branch: str = ""

    def describe(self) -> str:
        shown = ", ".join(f"`{f}`" for f in self.files[:3])
        if len(self.files) > 3:
            shown += f" (+{len(self.files) - 3} more)"
        return f"{self.key} [{self.source}]: {shown}"


@dataclass(frozen=True)
class Prediction:
    """What :func:`predict_overlap` concluded. Empty == "no prediction"."""

    predicted_files: tuple[str, ...] = ()
    overlaps: tuple[Overlap, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.overlaps)

    @property
    def after_keys(self) -> tuple[str, ...]:
        return tuple(o.key for o in self.overlaps)

    @property
    def reason(self) -> str:
        """The sentence recorded on the entry and echoed to the operator.

        Names the files AND the other entry, because "ordered after #123" with
        no cause is exactly the kind of unexplained sequencing an operator
        deletes at 2am.
        """
        if not self.overlaps:
            return ""
        return (
            "predicted file overlap (#2247) — ordered --after "
            + "; ".join(o.describe() for o in self.overlaps)
        )

    def audit_details(self) -> dict[str, Any]:
        """The JSON payload recorded with :data:`EVENT_PREDICTED`.

        Carries both sides' file lists, not just the verdict: scoring the
        prediction later (:func:`classify_outcome`) needs to know what was
        claimed, and a verdict with no claim behind it cannot be checked.
        """
        return {
            "predicted_files": list(self.predicted_files),
            "overlaps": [
                {
                    "key": o.key,
                    "source": o.source,
                    "branch": o.branch,
                    "files": list(o.files),
                }
                for o in self.overlaps
            ],
            "after": list(self.after_keys),
        }


def paths_overlap(left: str, right: str) -> bool:
    """Whether two declared/actual paths name the same work.

    Exact match, plus a directory declaration (``coord/dashboard/``) covering
    everything beneath it — the one generalisation an author can write down
    unambiguously. No globbing: a guess about what ``tests/*`` matches is a
    guess, and this module does not make those.
    """
    if not left or not right:
        return False
    if left == right:
        return True
    if left.endswith("/") and right.startswith(left):
        return True
    return bool(right.endswith("/") and left.startswith(right))


def _intersect(candidate: Sequence[str], other: Sequence[str]) -> tuple[str, ...]:
    hits: list[str] = []
    for path in candidate:
        if any(paths_overlap(path, o) for o in other) and path not in hits:
            hits.append(path)
    return tuple(hits)


def predict_overlap(
    candidate_files: Sequence[str],
    footprints: Iterable[Footprint],
    *,
    exclude_keys: Iterable[str] = (),
) -> Prediction:
    """Pure core: intersect the candidate's predicted files with *footprints*.

    ``exclude_keys`` drops entries the caller must not chain against — the
    candidate itself above all (a redispatch of an issue that already has a
    running branch must never be ordered after itself).
    """
    files = tuple(dict.fromkeys(f for f in (candidate_files or ()) if f))
    if not files:
        return Prediction()
    skip = {str(k) for k in exclude_keys}
    overlaps: list[Overlap] = []
    for fp in footprints or ():
        if not fp or fp.key in skip or not fp.files:
            continue
        hits = _intersect(files, fp.files)
        if hits:
            overlaps.append(
                Overlap(key=fp.key, source=fp.source, files=hits, branch=fp.branch)
            )
    return Prediction(predicted_files=files, overlaps=tuple(overlaps))


# Above this many DISTINCT entries hit by one declared directory token, the
# match is far more likely a token that happens to be common across the whole
# repo (``tests/``, ``docs/``) than a deliberate "this covers one feature's
# tests" declaration. #2601's incident was 14 against a threshold this
# conservative; two or three files genuinely clustered under one directory is
# unremarkable and must not warn.
FANOUT_WARN_THRESHOLD = 3


def fanout_warnings(
    prediction: Prediction, *, threshold: int = FANOUT_WARN_THRESHOLD,
) -> list[str]:
    """Declared directory tokens that matched implausibly many entries (#2601).

    :func:`paths_overlap`'s directory rule — a trailing-slash declaration
    covers everything beneath it — is deliberate: it is the one
    generalisation an author can write down unambiguously. But a token that
    is common across the whole repo (``tests/``, ``docs/``) satisfies that
    rule while carrying almost no signal: #2601 watched one bare ``tests/``
    declaration chain an issue behind fourteen others it never actually
    conflicted with.

    This does NOT change the ORDER — rule 1 ("order, never refuse") holds
    regardless of fanout; every one of those fourteen edges is still applied.
    It only surfaces the count, so an author can tell a token was too broad
    and narrow it to a specific file on the next ``add``.
    """
    counts: dict[str, int] = {}
    for overlap in prediction.overlaps:
        for path in overlap.files:
            if path.endswith("/"):
                counts[path] = counts.get(path, 0) + 1
    return [
        f"warning: `{path}` matched {count} entries — did you mean a "
        "specific file? (#2247 orders against all of them regardless — this "
        "changes nothing, it only flags the token)"
        for path, count in sorted(counts.items())
        if count > threshold
    ]


# ── gathering the two sides ──────────────────────────────────────────────────

# ``(repo_github, base, head) -> changed paths or None`` — same shape as
# :data:`coord.overlap_fence.DiffFilesFetcher`, injectable for tests.
DiffFilesFetcher = Callable[[str, str, str], "list[str] | None"]
# ``(repo_name, issue_number) -> issue body or None``.
BodyFetcher = Callable[[str, int], "str | None"]


def _default_diff_fetcher(repo_github: str, base: str, head: str) -> list[str] | None:
    from coord import github_ops  # noqa: PLC0415

    return github_ops.get_compare_files(repo_github, base, head)


def inflight_assignments(
    board: Any, repo_name: str, *, exclude_issue_number: int | None = None,
) -> list[Any]:
    """Work-like assignments in *repo_name* whose branch is still IN FLIGHT.

    Deliberately WIDER than #1720's fence, which looks only at ``status ==
    "running"``. A worker that has finished but whose PR has not merged is
    `status == "done"` and therefore lives in ``board.completed``, not
    ``board.active`` — and it is exactly the work a newcomer collides with:
    both 2026-08-14 collisions were against branches in that state. So this
    scans both buckets and subtracts only the statuses that mean the branch
    can no longer collide with anything:

    * ``merged`` — it IS the base now;
    * ``cancelled`` / ``advisory`` / ``refused_policy`` — never produced work;
    * ``failed`` — a dead branch. Excluded deliberately: a failed attempt can
      sit on the board indefinitely, and ordering live work behind a corpse
      would be a permanent latency cost with no collision to prevent. A retry
      of that issue creates a fresh non-failed assignment, which this does see.
    """
    out: list[Any] = []
    seen_ids: set[int] = set()
    for bucket in ("active", "completed"):
        for a in list(getattr(board, bucket, ()) or []):
            if id(a) in seen_ids:
                continue
            seen_ids.add(id(a))
            if getattr(a, "type", "") not in WORK_LIKE:
                continue
            if getattr(a, "status", "") in LANDED_STATUSES:
                continue
            if getattr(a, "repo_name", "") != repo_name:
                continue
            if not getattr(a, "branch", ""):
                continue
            if exclude_issue_number is not None and a.issue_number == exclude_issue_number:
                continue
            out.append(a)
    return out


def inflight_footprints(
    repo_name: str,
    repo_github: str,
    base_branch: str,
    *,
    board: Any = None,
    exclude_issue_number: int | None = None,
    diff_files_fetcher: DiffFilesFetcher | None = None,
) -> list[Footprint]:
    """GROUND TRUTH: the real diff of every in-flight branch in *repo_name*.

    Fails open at every layer, exactly as :func:`coord.overlap_fence.
    compute_overlap_fence` does: an unreadable board yields ``[]``, and one
    branch whose compare fails is skipped rather than sinking the rest. The
    worst case is a missed prediction, i.e. today's behaviour.
    """
    try:
        if board is None:
            from coord.board_service import read_board  # noqa: PLC0415

            board = read_board()
        assignments = inflight_assignments(
            board, repo_name, exclude_issue_number=exclude_issue_number
        )
    except Exception:  # noqa: BLE001 — never block an enqueue on a board read
        return []

    fetch = diff_files_fetcher or _default_diff_fetcher
    out: list[Footprint] = []
    seen: set[str] = set()
    for a in assignments:
        try:
            files = fetch(repo_github, base_branch, a.branch)
        except Exception:  # noqa: BLE001 — one bad branch, not all of them
            files = None
        if not files:
            continue
        key = entry_key(repo_name, int(a.issue_number))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Footprint(
                key=key,
                issue_number=int(a.issue_number),
                files=tuple(dict.fromkeys(files)),
                source=SOURCE_BRANCH,
                branch=str(a.branch or ""),
            )
        )
    return out


def declared_footprints(
    candidates: Iterable[tuple[str, int]],
    body_fetcher: BodyFetcher,
    *,
    exclude_keys: Iterable[str] = (),
) -> list[Footprint]:
    """Declared footprints for already-queued ``(repo, issue)`` pairs.

    This is the declared-vs-declared half — used only for work that has no
    branch yet, where there is no ground truth to check against and the
    alternative is no prediction at all. Both sides are the authors' own
    words, never inferred from prose.
    """
    skip = {str(k) for k in exclude_keys}
    out: list[Footprint] = []
    for repo, issue in candidates or ():
        key = entry_key(repo, int(issue))
        if key in skip:
            continue
        skip.add(key)
        try:
            files = parse_declared_files(body_fetcher(repo, int(issue)))
        except Exception:  # noqa: BLE001 — one unreadable body, not all of them
            files = []
        if files:
            out.append(
                Footprint(
                    key=key,
                    issue_number=int(issue),
                    files=tuple(files),
                    source=SOURCE_DECLARED,
                )
            )
    return out


def collect_candidate_files(
    repo_name: str,
    issue_number: int,
    body_fetcher: BodyFetcher,
    *,
    extra_sources: Iterable[Callable[[str, int], "Sequence[str] | None"]] = (),
) -> list[str]:
    """The candidate's predicted file list, cheapest source first.

    Source 1 is the author's own declaration. ``extra_sources`` is the seam a
    graph-derived source would plug into (see the module docstring); it is
    consulted only when the declaration is empty, so a written-down list is
    never overridden by an inferred one.
    """
    try:
        files = parse_declared_files(body_fetcher(repo_name, int(issue_number)))
    except Exception:  # noqa: BLE001 — fail open to "no prediction"
        files = []
    if files:
        return files
    for source in extra_sources or ():
        try:
            extra = source(repo_name, int(issue_number)) or ()
        except Exception:  # noqa: BLE001 — same
            continue
        cleaned = [p for p in (_clean_path(x) for x in extra) if p]
        if cleaned:
            return list(dict.fromkeys(cleaned))
    return []


# ── measuring the predictor ──────────────────────────────────────────────────


def classify_outcome(
    overlap_files: Sequence[str],
    candidate_actual: Sequence[str] | None,
    other_actual: Sequence[str] | None,
) -> str:
    """Score one recorded prediction against what the two branches DID touch.

    Returns :data:`OUTCOME_CONFIRMED` (the two diffs really do intersect —
    serializing was right), :data:`OUTCOME_FALSE_POSITIVE` (they don't — we
    paid latency for nothing), or :data:`OUTCOME_UNKNOWN` when either actual
    diff could not be read, which is NOT a false positive: scoring a
    prediction against a diff we failed to fetch would be exactly the
    "predictor nobody measures" the issue warns about, dressed up as data.

    Note what is and is not claimed. This scores the prediction's own claim —
    "these two file sets will intersect" — against ground truth, not the
    counterfactual "would git have conflicted", which is unknowable once the
    work has been serialized.
    """
    if candidate_actual is None or other_actual is None:
        return OUTCOME_UNKNOWN
    real = _intersect(list(candidate_actual), list(other_actual))
    if real:
        return OUTCOME_CONFIRMED
    # An empty predicted-overlap list should never have been recorded, but if
    # one was, it cannot be confirmed by anything — call it unknown, not a
    # false positive.
    return OUTCOME_FALSE_POSITIVE if overlap_files else OUTCOME_UNKNOWN


@dataclass(frozen=True)
class Accuracy:
    """Precision of the predictor over a set of scored predictions."""

    confirmed: int = 0
    false_positive: int = 0
    unknown: int = 0

    @property
    def scored(self) -> int:
        return self.confirmed + self.false_positive

    @property
    def precision(self) -> float | None:
        return None if not self.scored else self.confirmed / self.scored

    def render(self) -> str:
        if not self.scored:
            return (
                f"no scored predictions yet ({self.unknown} unscoreable) — "
                "precision unknown"
            )
        pct = f"{(self.precision or 0.0) * 100:.0f}%"
        return (
            f"{self.confirmed} confirmed · {self.false_positive} false-positive "
            f"· {self.unknown} unscoreable — precision {pct}"
        )


def tally(outcomes: Iterable[str]) -> Accuracy:
    counts: dict[str, int] = {
        OUTCOME_CONFIRMED: 0, OUTCOME_FALSE_POSITIVE: 0, OUTCOME_UNKNOWN: 0,
    }
    for outcome in outcomes or ():
        if outcome in counts:
            counts[outcome] += 1
    return Accuracy(
        confirmed=counts[OUTCOME_CONFIRMED],
        false_positive=counts[OUTCOME_FALSE_POSITIVE],
        unknown=counts[OUTCOME_UNKNOWN],
    )


def predictions_from_audit(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten :data:`EVENT_PREDICTED` audit rows into one record per overlap.

    One enqueue can predict overlaps against several in-flight branches, and
    each of those is a separate claim that succeeds or fails on its own.
    """
    out: list[dict[str, Any]] = []
    for row in entries or ():
        details = row.get("details") or {}
        repo = row.get("repo") or ""
        issue = row.get("issue")
        if not repo or issue is None:
            continue
        for overlap in details.get("overlaps") or ():
            if not isinstance(overlap, Mapping):
                continue
            out.append(
                {
                    "ts": row.get("ts"),
                    "repo": str(repo),
                    "issue": int(issue),
                    "key": entry_key(str(repo), int(issue)),
                    "other_key": str(overlap.get("key") or ""),
                    "source": str(overlap.get("source") or ""),
                    "files": [str(f) for f in (overlap.get("files") or ())],
                }
            )
    return out
