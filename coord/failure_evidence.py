"""Failure-evidence extractor (#4092).

The Test stage's smoke runner can chain several cargo (or pytest) stages in
one command. `coord.agent._tail_text` keeps only the first ~N and last ~M
characters of that combined log for display — generous enough for a short
run, but a multi-stage log that actually fails somewhere in the MIDDLE loses
exactly the lines that explain why.

This module runs over the FULL log, before any truncation, and returns a
small, structured :class:`FailureEvidence` record: which stage(s) failed,
the error lines that explain it (bounded to a few KB), and a handful of
boolean signals a downstream consumer can branch on without re-parsing text.
It is pure text analysis — no subprocess, no board/DB access — so it can run
in-process right where the full log is still in memory (`coord.agent.
run_plain_runner_command`) and be persisted as a plain dict on the smoke
assignment row for later readers (the fix briefing, `coord log`'s summary,
and the sibling triage pass) to read back without ever re-opening the log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

#: Total budget (characters) for `FailureEvidence.errors` — generous enough
#: to carry a real rustc/pytest failure block (or several short ones), small
#: enough that persisting it alongside the assignment row never approaches
#: the sizes #715/#452 already cap `AgentAssignment` state at.
MAX_ERROR_BYTES = 8192

# ── Stage identification ─────────────────────────────────────────────────

#: The runner's own summary line, printed once at the end of a multi-stage
#: command when one or more stages failed — authoritative when present,
#: since the runner knows its own stage boundaries better than any regex
#: over its output can reconstruct after the fact.
_FAILED_STAGES_LINE_RE = re.compile(
    r"^FAILED STAGES \(stage order, first is likely root\):\s*(?P<stages>.+)$",
    re.MULTILINE,
)

#: Cargo's own per-stage failure markers — used when the runner printed no
#: `FAILED STAGES` summary (e.g. a bare `cargo test` with no wrapping
#: script). Matched in document order, which IS stage order: cargo stages
#: run sequentially, so whichever marker appears first in the log is the
#: root failure.
_CARGO_COULD_NOT_COMPILE_RE = re.compile(r"^error: could not compile `([^`]+)`.*$", re.MULTILINE)
_CARGO_TEST_FAILED_RE = re.compile(r"^error: test failed, to rerun pass `([^`]+)`.*$", re.MULTILINE)

#: pytest prints no per-stage marker at all — a bare `FAILED <nodeid>` short
#: summary line, or its closing tally ("1 failed, 41 passed in 1.23s"), is
#: the only reliable signal that *some* pytest run actually failed, so there
#: is exactly one "stage" to name: the pytest run itself. Deliberately NOT
#: matched on the "short test summary info" banner alone: with `-ra` (this
#: repo's own `addopts`) pytest prints that banner for every non-passed
#: outcome, including a plain SKIPPED — so a banner-only check would name
#: "pytest" as a failed stage on an all-green run that merely skipped tests.
_PYTEST_FAILED_LINE_RE = re.compile(r"^FAILED\s+(?!STAGES\b)\S", re.MULTILINE)
_PYTEST_FAILED_TALLY_RE = re.compile(r"\b[1-9]\d* failed\b")


def _parse_failed_stages(log: str) -> list[str]:
    m = _FAILED_STAGES_LINE_RE.search(log)
    if m:
        stages = [s.strip() for s in m.group("stages").split(",") if s.strip()]
        if stages:
            return stages

    matches = list(_CARGO_COULD_NOT_COMPILE_RE.finditer(log)) + list(
        _CARGO_TEST_FAILED_RE.finditer(log)
    )
    matches.sort(key=lambda mm: mm.start())
    stages: list[str] = []
    for mm in matches:
        name = mm.group(1)
        if name not in stages:
            stages.append(name)
    if stages:
        return stages

    if _PYTEST_FAILED_LINE_RE.search(log) or _PYTEST_FAILED_TALLY_RE.search(log):
        return ["pytest"]

    return []


# ── Error-line extraction ────────────────────────────────────────────────

#: A real per-site rustc diagnostic — excludes the two cargo top-level
#: summary lines above (those name the STAGE, not a specific error site;
#: they get their own one-line block below so they still show up as an
#: anchor without being double-matched here).
_RUSTC_ERROR_RE = re.compile(
    r"^error(\[E\d+\])?:(?!.*(?:could not compile|test failed, to rerun pass)).*$",
    re.MULTILINE,
)
_PANIC_RE = re.compile(r"^thread '.*' panicked at .*$", re.MULTILINE)
_TEST_RESULT_FAILED_RE = re.compile(r"^test result: FAILED\b.*$", re.MULTILINE)
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(?!STAGES\b)\S.*$", re.MULTILINE)
_PYTEST_E_LINE_RE = re.compile(r"^E\s.*$", re.MULTILINE)
_CARGO_STAGE_MARKER_RE = re.compile(
    r"^error: (?:could not compile|test failed, to rerun pass) .*$", re.MULTILINE
)


def _line_offsets(lines: list[str]) -> list[int]:
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def _collect_error_blocks(log: str) -> list[tuple[int, str]]:
    """Return `(offset, block_text)` pairs, one per matched error site, in
    document order — which is stage order, since stages run sequentially.
    """
    lines = log.splitlines()
    offsets = _line_offsets(lines)
    n = len(lines)
    blocks: list[tuple[int, str]] = []
    i = 0
    while i < n:
        line = lines[i]
        if _RUSTC_ERROR_RE.match(line):
            # rustc block: the error line, then up to 5 more lines — always
            # including the `-->` location line the issue calls out by
            # name — stopping at the first blank line once `-->` has been
            # seen (or after the fixed lookahead window, whichever first).
            block_lines = [line]
            j = i + 1
            seen_arrow = False
            limit = min(n, i + 6)
            while j < limit:
                nxt = lines[j]
                if nxt.strip() == "" and seen_arrow:
                    break
                block_lines.append(nxt)
                if nxt.lstrip().startswith("-->"):
                    seen_arrow = True
                j += 1
            blocks.append((offsets[i], "\n".join(block_lines)))
            i = j
        elif _PANIC_RE.match(line):
            # panic block: the `panicked at` line plus the assertion
            # message that follows it, stopping at the first blank line or
            # the `note: run with RUST_BACKTRACE=1...` boilerplate.
            block_lines = [line]
            j = i + 1
            limit = min(n, i + 5)
            while j < limit:
                nxt = lines[j]
                if nxt.strip() == "" or nxt.lstrip().startswith("note:"):
                    break
                block_lines.append(nxt)
                j += 1
            blocks.append((offsets[i], "\n".join(block_lines)))
            i = max(j, i + 1)
        elif (
            _TEST_RESULT_FAILED_RE.match(line)
            or _PYTEST_FAILED_RE.match(line)
            or _PYTEST_E_LINE_RE.match(line)
            or _CARGO_STAGE_MARKER_RE.match(line)
        ):
            blocks.append((offsets[i], line))
            i += 1
        else:
            i += 1
    return blocks


def _apply_budget(blocks: list[tuple[int, str]], budget: int) -> str:
    """Concatenate *blocks* in document order (= stage order) until *budget*
    characters, so a budget cut always keeps the EARLIEST — i.e. first
    failing stage's — errors, never a later stage's.
    """
    ordered = sorted(blocks, key=lambda b: b[0])
    pieces: list[str] = []
    used = 0
    for _, text in ordered:
        sep = "" if not pieces else "\n\n"
        addition = sep + text
        if used + len(addition) > budget:
            remaining = budget - used - len(sep)
            if remaining > 20:
                pieces.append(sep + text[:remaining])
            break
        pieces.append(addition)
        used += len(addition)
    return "".join(pieces)


# ── Signals ───────────────────────────────────────────────────────────────

_RATCHET_RE = re.compile(r"may only shrink|grew from \d+ to \d+")
# Deliberately narrow: a stage merely NAMED "cargo fmt --check"/"rustfmt" in
# a runner's own stage header is not evidence of a lint failure (every
# fixture's clean-pass stage list says "cargo fmt" whether or not it found
# anything) — only an actual clippy lint id or a real rustfmt diff does.
_LINT_RE = re.compile(r"clippy::|^Diff in ", re.MULTILINE)
#: Per-target "ran zero tests" counterparts — every cargo workspace prints
#: one of these per target (a `src/main.rs` bin, a doc-test pass with no
#: examples, an empty integration binary routinely says `running 0 tests`
#: even on an all-green build), so a bare presence check would flag
#: `no_tests_ran` on the overwhelming majority of healthy multi-target runs.
_RUNNING_N_TESTS_RE = re.compile(r"running (\d+) tests?", re.IGNORECASE)
_COLLECTED_N_ITEMS_RE = re.compile(r"collected (\d+) items?", re.IGNORECASE)
_NO_TESTS_RAN_PHRASE_RE = re.compile(r"no tests ran", re.IGNORECASE)


@dataclass(frozen=True)
class FailureSignals:
    """Boolean signals a caller can branch on without re-parsing text."""

    compile_error: bool = False
    test_assertion: bool = False
    ratchet: bool = False
    lint: bool = False
    no_tests_ran: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "compile_error": self.compile_error,
            "test_assertion": self.test_assertion,
            "ratchet": self.ratchet,
            "lint": self.lint,
            "no_tests_ran": self.no_tests_ran,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object] | None) -> "FailureSignals":
        d = d or {}
        return cls(
            compile_error=bool(d.get("compile_error", False)),
            test_assertion=bool(d.get("test_assertion", False)),
            ratchet=bool(d.get("ratchet", False)),
            lint=bool(d.get("lint", False)),
            no_tests_ran=bool(d.get("no_tests_ran", False)),
        )


def _no_tests_ran(log: str) -> bool:
    """True only when NOTHING in the log actually ran a test — i.e. every
    `running N tests` / `collected N items` counterpart found is `N == 0`,
    or the log says so explicitly (`no tests ran`). A single zero-test
    target alongside another target that ran real tests is normal cargo
    workspace output, not a failure signal.
    """
    counts = [int(n) for n in _RUNNING_N_TESTS_RE.findall(log)] + [
        int(n) for n in _COLLECTED_N_ITEMS_RE.findall(log)
    ]
    if counts:
        return all(c == 0 for c in counts)
    return bool(_NO_TESTS_RAN_PHRASE_RE.search(log))


def _compute_signals(log: str) -> FailureSignals:
    compile_error = bool(_RUSTC_ERROR_RE.search(log)) or bool(
        _CARGO_COULD_NOT_COMPILE_RE.search(log)
    )
    test_assertion = bool(
        _PANIC_RE.search(log)
        or _TEST_RESULT_FAILED_RE.search(log)
        or _PYTEST_FAILED_RE.search(log)
        or "AssertionError" in log
    )
    return FailureSignals(
        compile_error=compile_error,
        test_assertion=test_assertion,
        ratchet=bool(_RATCHET_RE.search(log)),
        lint=bool(_LINT_RE.search(log)),
        no_tests_ran=_no_tests_ran(log),
    )


# ── The record ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FailureEvidence:
    """Structured record extracted from a full (pre-truncation) smoke log.

    Empty (`is_empty` True) for a clean pass — `extract_failure_evidence`
    always returns a record, never `None`, so a caller never has to
    special-case "no evidence was even attempted" vs. "evidence was
    attempted and found nothing".
    """

    failed_stages: tuple[str, ...] = ()
    errors: str = ""
    signals: FailureSignals = field(default_factory=FailureSignals)

    @property
    def is_empty(self) -> bool:
        return (
            not self.failed_stages
            and not self.errors.strip()
            and not any(self.signals.to_dict().values())
        )

    def to_dict(self) -> dict:
        return {
            "failed_stages": list(self.failed_stages),
            "errors": self.errors,
            "signals": self.signals.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object] | None) -> "FailureEvidence":
        if not d:
            return cls()
        raw_stages = d.get("failed_stages") or ()
        return cls(
            failed_stages=tuple(raw_stages),  # type: ignore[arg-type]
            errors=str(d.get("errors") or ""),
            signals=FailureSignals.from_dict(d.get("signals")),  # type: ignore[arg-type]
        )


def extract_failure_evidence(
    log: str | None, *, max_error_bytes: int = MAX_ERROR_BYTES
) -> FailureEvidence:
    """Extract a :class:`FailureEvidence` record from the FULL *log* text —
    call this BEFORE any truncation (e.g. `coord.agent._tail_text`) so the
    evidence always reflects content that may otherwise be discarded.
    """
    text = log or ""
    failed_stages = _parse_failed_stages(text)
    blocks = _collect_error_blocks(text)
    errors = _apply_budget(blocks, max_error_bytes)
    signals = _compute_signals(text)
    return FailureEvidence(failed_stages=tuple(failed_stages), errors=errors, signals=signals)


def format_evidence_block(evidence: FailureEvidence) -> str:
    """Render *evidence* as a self-delimited text block suitable for
    splicing into a truncated log display (between the kept head and tail)
    — returns `""` for an empty record so a caller can splice
    unconditionally without an extra `if evidence.is_empty` check at the
    call site.
    """
    if evidence.is_empty:
        return ""
    stages = ", ".join(evidence.failed_stages) if evidence.failed_stages else "(stage not identified)"
    active_signals = [name for name, val in evidence.signals.to_dict().items() if val]
    signal_text = ", ".join(active_signals) if active_signals else "none"
    lines = [
        "--- extracted failure evidence (full log, before truncation) ---",
        f"failed stages (order, first likely root): {stages}",
        f"signals: {signal_text}",
        "",
        evidence.errors.rstrip("\n"),
        "--- end extracted failure evidence ---",
    ]
    return "\n".join(lines)
