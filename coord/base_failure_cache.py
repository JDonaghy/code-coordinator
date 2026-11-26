"""Per-base-SHA pre-existing-failure cache (#4166).

**The problem.** A Test-stage failure is briefed to a fix worker as if the
branch caused it. Often it did not: the failure was already red on the base
commit the branch was cut from (a dependency that moved, a ratchet that
tripped on a neighbouring merge, a genuinely flaky suite). The fix leg then
spends a whole session re-diagnosing a defect its branch never introduced,
and usually concludes by "fixing" something unrelated.

**What this module records.** One append-only JSONL line per graded
failure: which repo, which **base SHA** the failing branch was cut from,
which branch it was, and a bounded
:func:`coord.failure_evidence.failure_signature` fingerprint of the failure
itself. Nothing else — no log text, no command (#4166 forbids re-injecting
an echoed ``test_command`` anywhere near a fix briefing), no verdict.

**What a hit means.** :func:`record_failure` returns the earliest prior
record with the SAME signature, at the SAME base SHA, from a DIFFERENT
branch. Two independent branches do not usually introduce the identical
normalized failure, so that pair is good evidence the failure predates both
of them — i.e. it belongs to the base, not to the work.

It is evidence, not proof: two branches cut from the same base can
genuinely make the same mistake (both regenerate the same stale wire types,
both bump the same dependency). So a hit NEVER changes a verdict, a status,
or a routing decision anywhere in coord. Its only effect is an advisory
note line (:func:`note_line`) in the leg's own log, which rides into the fix
briefing's evidence so the worker reads "this was already failing on the
base" before it starts bisecting its own diff. The cost of a false positive
is one misleading sentence in a briefing; the cost of a false negative is
today's behaviour.

**Scope: one machine.** The cache lives in the recording agent's own state
dir, so a hit requires both failures to have been graded on the SAME
machine. That is narrower than "anywhere in the fleet" and deliberately
so — it needs no new wire surface, no board write, and no cross-machine
read on the Test stage's hot path. It is also where the hits actually are:
a capability-partitioned Test stage routes the same partition to the same
capable machine every time (``smoke_tests.capability_rules``), so two
branches' ``gtk`` legs land on the same box by construction. Failures
graded on different machines simply never match, which reads as "no note"
— the existing behaviour.

**Storage.** Append-only JSONL at :func:`cache_path`, rotated at
:data:`MAX_CACHE_BYTES` and read back with an age cutoff
(:data:`MAX_RECORD_AGE_S`) — a base SHA stops being interesting the moment
nothing is still branched off it, and a fingerprint from a month ago says
nothing about today's base. Append-only for the same reason
:mod:`coord.block_log` is: concurrent writers (two machines' agents, two
fan-out legs on one machine) interleave whole records instead of shredding
a rewritten file.

**Never raises.** Every entry point swallows its own errors — the recorder
runs inside an agent's reap path, and an observability append that can fail
a leg is strictly worse than no observability.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: Rotate at 2 MiB — thousands of records at ~200 bytes each, so a whole-file
#: parse stays cheap and no index is needed.
MAX_CACHE_BYTES = 2 * 1024 * 1024

#: Records older than this are ignored on read (and so can never produce a
#: hit). Two weeks: comfortably longer than any branch's useful life, short
#: enough that a long-since-fixed base failure stops being quoted.
MAX_RECORD_AGE_S = 14 * 24 * 3600

#: Prefix of the advisory note a caller writes into a leg's own log on a
#: hit. `coord.smoke._write_fanout_failure_evidence` carries any line with
#: this prefix into the parent's fix-briefing evidence verbatim, so the
#: prefix is a contract between the two, not decoration.
PRE_EXISTING_NOTE_PREFIX = "# pre-existing failure:"


@dataclass(frozen=True)
class PriorFailure:
    """One previously recorded failure at the same base SHA."""

    repo: str
    base_sha: str
    signature: str
    branch: str
    at: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PriorFailure | None":
        try:
            return cls(
                repo=str(d["repo"]),
                base_sha=str(d["base_sha"]),
                signature=str(d["signature"]),
                branch=str(d.get("branch") or ""),
                at=float(d.get("at") or 0.0),
            )
        except (KeyError, TypeError, ValueError):
            return None


def cache_path(state_dir: Path | None = None) -> Path:
    """Where the cache lives.

    A function, not a module constant, for the same reason
    :func:`coord.block_log.block_log_path` is: a constant captured at import
    freezes whatever ``Path.home()`` was at process start, which breaks both
    tests with a relocated ``$HOME`` and a long-lived daemon.
    """
    override = os.environ.get("COORD_BASE_FAILURE_CACHE")
    if override:
        return Path(override)
    base = Path(state_dir) if state_dir is not None else Path.home() / ".coord"
    return base / "base-failures.jsonl"


def note_line(prior: PriorFailure) -> str:
    """The one-line advisory a caller appends to a failing leg's own log.

    Bounded by construction — a repo name, a short SHA, a branch name and a
    date. No log text, no command, nothing that could grow with the size of
    the failure it describes.
    """
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(prior.at))
    return (
        f"{PRE_EXISTING_NOTE_PREFIX} this same failure was already recorded "
        f"for {prior.repo} at base {prior.base_sha[:12]} on branch "
        f"{prior.branch or '(unknown)'} ({when}) — it predates this branch, "
        "so diagnose it as a base/environment failure before changing this "
        "branch's own code."
    )


def read_records(
    *,
    state_dir: Path | None = None,
    path: Path | None = None,
    now: float | None = None,
) -> list[PriorFailure]:
    """Every non-expired record, oldest first. Never raises.

    Unparseable lines are skipped rather than fatal: a half-written final
    line (power loss mid-append) must not make the whole cache unreadable.
    """
    target = cache_path(state_dir) if path is None else path
    cutoff = (time.time() if now is None else now) - MAX_RECORD_AGE_S
    out: list[PriorFailure] = []
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        rec = PriorFailure.from_dict(raw)
        if rec is None or rec.at < cutoff:
            continue
        out.append(rec)
    return out


def lookup(
    repo: str,
    base_sha: str,
    signature: str,
    *,
    branch: str = "",
    state_dir: Path | None = None,
    path: Path | None = None,
    now: float | None = None,
) -> PriorFailure | None:
    """The earliest recorded failure matching *repo*/*base_sha*/*signature*
    that came from a branch OTHER than *branch*.

    Returns ``None`` — never a self-match — when the only prior records are
    this same branch's own earlier runs: a branch failing the same way twice
    is a retry, not evidence about the base. Also ``None`` for an empty
    *signature* or *base_sha*, so a caller can hand through whatever it has
    without guarding first.
    """
    if not signature or not base_sha or not repo:
        return None
    for rec in read_records(state_dir=state_dir, path=path, now=now):
        if (
            rec.repo == repo
            and rec.base_sha == base_sha
            and rec.signature == signature
            and rec.branch != branch
        ):
            return rec
    return None


def record_failure(
    repo: str,
    base_sha: str,
    signature: str,
    *,
    branch: str = "",
    state_dir: Path | None = None,
    path: Path | None = None,
    now: float | None = None,
) -> PriorFailure | None:
    """Record this failure and return the prior one it matches, if any.

    Lookup happens BEFORE the append, so a caller is never handed its own
    record back. Returns what :func:`lookup` would — the earliest same-base,
    same-signature failure from a different branch, or ``None``.

    Never raises: a cache that cannot be written costs the note, not the
    leg.
    """
    prior = lookup(
        repo, base_sha, signature,
        branch=branch, state_dir=state_dir, path=path, now=now,
    )
    if not signature or not base_sha or not repo:
        return prior
    target = cache_path(state_dir) if path is None else path
    record = {
        "repo": repo,
        "base_sha": base_sha,
        "signature": signature,
        "branch": branch,
        "at": time.time() if now is None else now,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target)
        # One `write` of one line, in append mode: on POSIX that is the
        # atomic unit, so two agents appending at the same instant
        # interleave whole records rather than shredding a line.
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        _log.debug("base-failure cache: append failed", exc_info=True)
    return prior


def _rotate_if_needed(target: Path) -> None:
    try:
        if target.stat().st_size < MAX_CACHE_BYTES:
            return
    except OSError:
        return
    # One generation only, and the rotated file is never read back: an
    # expired-by-size record is older than every record still in the live
    # file, and `MAX_RECORD_AGE_S` would have dropped most of it anyway.
    try:
        target.replace(target.with_suffix(target.suffix + ".1"))
    except OSError:  # pragma: no cover — defensive
        _log.debug("base-failure cache: rotation failed", exc_info=True)
