"""Triage-first failure classification (#4093).

WHY THIS EXISTS. ``coord.drive`` hands every Test-stage failure to the same
fix-round path regardless of WHY it failed. There is nothing that first asks
"is this even the work's fault?" before spending a fix leg on it. This module
is that missing first question: it takes the evidence already extracted by
:mod:`coord.failure_evidence` (#4092) plus a handful of cheap-to-compute
facts about the branch and the failing machine, and returns a
:class:`TriageVerdict` naming WHY — ``infra`` / ``flaky`` / ``mechanical`` /
``substantive`` / ``human`` — so a future caller can route on it. Wiring a
caller in (the drive, a dashboard column, ...) is explicitly out of scope
here; this module has no production caller yet, same posture as
``coord.preflight_size`` before its own dispatch gate landed.

NOT :mod:`coord.failure_class` or :mod:`coord.failure_classifier` (#2096
disambiguation). Both of those neighbours already export a
``classify_failure()``, and neither answers this module's question:

* :mod:`coord.failure_class` answers "environmental (529 / usage limit /
  network / infra / setup) vs. work" for RESUME SCHEDULING and the liveness
  gate. This module CALLS it (see :func:`_infra_verdict`) rather than
  re-deriving its own environmental signal — one question ("is this an
  environmental hit?"), one answer, reused.
* :mod:`coord.failure_classifier` answers "compliance nit vs. capability
  gap" for the MODEL-ESCALATION ladder. Different axis entirely — a failure
  can be ``mechanical`` here and ``capability`` there (a ratchet trip is a
  rote fix, not evidence the model needs to be smarter) and the two must
  never be unified into one verdict.

DETERMINISTIC TIERS FIRST. :func:`triage_failure` tries, in order:

1. **Infra** — :mod:`coord.failure_class`'s own environmental verdict (usage
   limit / API 5xx / network / #3670 infra outage / #4018 setup failure /
   #3912's toolchain-collision signatures, all already folded into that
   module's ``classify_failure``). Any of those means "wait and retry", which
   this taxonomy calls ``infra`` — none of it is evidence about the CODE.
2. **Timeout-under-load** (#4130) — a Test leg killed for hitting its own
   wall-clock timeout (:func:`_timeout_verdict`) never produced a verdict at
   all, so it is ``infra`` too, by the same reasoning as tier 1 — just a
   signature :mod:`coord.failure_class` doesn't carry, matched directly
   against *failure_reason*/*terminal_reason* here instead.
3. **Mechanical** — a ``compile_error``, ``ratchet``, or ``lint`` signal
   (:class:`coord.failure_evidence.FailureSignals`) with a SMALL number of
   error sites, each one confined to a file of the same kind the branch diff
   (or the issue's own ``## Files``) already touches. A rote, well-scoped fix
   needs no model judgement to recognise.
4. Otherwise, **one cheap ``claude -p`` call** (haiku by default, configurable
   — see :func:`triage_failure`'s ``model=``). It sees the already-extracted
   evidence record and the diff stat, NEVER the raw log, and must reply with
   exactly one JSON object matching the verdict schema. A malformed reply, an
   unrecognised bucket, or confidence below :data:`DEFAULT_MIN_CONFIDENCE`
   all fall back to ``substantive`` — today's behaviour, never a worse one.

THE TAXONOMY IS DELIBERATELY LOPSIDED, same posture as ``failure_class.py``'s
own docstring: misclassifying real work as ``flaky`` or ``mechanical`` must
degrade to the existing fix-round path (``substantive``), never to merging
something untested. In particular, :data:`FailureSignals.compile_error
<coord.failure_evidence.FailureSignals.compile_error>` being set
UNCONDITIONALLY rules out ``flaky`` — a compile error is a durable defect in
the code, never noise that clears on retry — enforced as a hard guard on the
model tier's own output (:func:`_run_model_tier`), not just documented.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Mapping, Sequence

from coord.failure_class import classify_failure as _classify_environmental
from coord.failure_evidence import FailureEvidence, FailureSignals

# ── the taxonomy ─────────────────────────────────────────────────────────

BUCKET_INFRA = "infra"
BUCKET_FLAKY = "flaky"
BUCKET_MECHANICAL = "mechanical"
BUCKET_SUBSTANTIVE = "substantive"
BUCKET_HUMAN = "human"

KNOWN_BUCKETS = frozenset(
    {BUCKET_INFRA, BUCKET_FLAKY, BUCKET_MECHANICAL, BUCKET_SUBSTANTIVE, BUCKET_HUMAN}
)

# ── tuning ───────────────────────────────────────────────────────────────

#: "Small" error-site count for the mechanical rule — a handful of rustc
#: diagnostics plus a tripped ratchet/lint, not a wall of failures that
#: actually needs investigation.
MECHANICAL_MAX_ERROR_COUNT = 5

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT_SECONDS = 30.0
#: Low on purpose (issue: "MAX_THINKING_TOKENS low") — this call classifies
#: an already-extracted, bounded evidence record; it does not need to
#: reason at length to do that cheaply.
DEFAULT_MAX_THINKING_TOKENS: int | None = 1024
#: A verdict below this confidence is treated as "the model wasn't sure"
#: and degrades to :data:`BUCKET_SUBSTANTIVE` (#4093's safe default) rather
#: than risking a wrongly-skipped fix round.
DEFAULT_MIN_CONFIDENCE = 0.5

#: Platform-shaped capability tokens this module knows how to read out of a
#: file path or a machine's capability list. Mirrors the capability names
#: ``smoke_tests.capability_rules`` / ``Repo.board_doctor`` already use in
#: ``coordinator.yml`` (``coord/config.py``'s ``SmokeRule``) — not an
#: independent vocabulary.
_PLATFORM_CAPABILITY_TOKENS: tuple[str, ...] = ("macos", "windows", "linux", "gtk", "browser")

#: How much of the (already 8KB-bounded, per #4092) evidence error text to
#: hand the model — defensive, in case a caller passes an evidence record
#: built with a larger budget than this module expects.
_ERRORS_EXCERPT_CHARS = 3000


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …[truncated]"


# ── inputs ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiffStat:
    """A branch diff's footprint, as this module needs it — not a full diff
    parse. ``files`` is every path the diff touches (e.g.
    :func:`coord.github_ops.diff_file_paths`'s output); ``total_lines`` is
    additions+deletions across the whole diff (e.g.
    :func:`coord.github_ops.get_branch_diff_size`'s output). Both already
    exist elsewhere in the repo; this module takes them as plain data rather
    than re-deriving them or touching the network itself — same "no diff
    access of its own" discipline as :mod:`coord.preflight_size`.
    """

    files: tuple[str, ...] = ()
    total_lines: int = 0

    def to_dict(self) -> dict:
        return {"files": list(self.files), "total_lines": self.total_lines}


@dataclass(frozen=True)
class ProposedScope:
    """The verdict's guess at what a fix would touch."""

    files: tuple[str, ...] = ()
    estimated_line_delta: int = 0

    def to_dict(self) -> dict:
        return {"files": list(self.files), "estimated_line_delta": self.estimated_line_delta}


@dataclass(frozen=True)
class TriageVerdict:
    """Why a Test-stage failure happened, and how confident :func:`triage_failure`
    is about it.

    ``via`` names which tier produced the verdict — ``"deterministic:infra:
    <kind>"``, ``"deterministic:mechanical:<signal>"``, ``"model"``, or a
    ``"model:<fallback-reason>"`` when the model tier's own reply was
    discarded in favour of the safe :data:`BUCKET_SUBSTANTIVE` default. Not
    load-bearing for routing — ``bucket`` is — but it is what makes a logged
    verdict auditable without re-running the classifier.
    """

    bucket: str
    diagnosis: str
    repro_capability: str | None
    proposed_scope: ProposedScope
    confidence: float
    via: str

    def to_dict(self) -> dict:
        return {
            "bucket": self.bucket,
            "diagnosis": self.diagnosis,
            "repro_capability": self.repro_capability,
            "proposed_scope": self.proposed_scope.to_dict(),
            "confidence": self.confidence,
            "via": self.via,
        }


def _fallback_substantive(*, reason: str, via: str) -> TriageVerdict:
    """The safe default every failure mode in this module collapses to:
    today's behaviour (an ordinary fix round), never a worse one."""
    return TriageVerdict(
        bucket=BUCKET_SUBSTANTIVE,
        diagnosis=reason,
        repro_capability=None,
        proposed_scope=ProposedScope(),
        confidence=0.0,
        via=via,
    )


#: `via` values meaning the classifier had NOTHING usable to say — a
#: missing/misbehaving ``claude`` binary, an unparseable reply, or a
#: confidence below ``DEFAULT_MIN_CONFIDENCE`` (see ``_run_model_tier``).
#: Every member is produced by :func:`_fallback_substantive`, but NOT every
#: :func:`_fallback_substantive` call belongs here: ``"model:
#: compile_error_guard"`` is a confident, evidence-backed override (a
#: compile error can never be flaky, enforced regardless of what the model
#: answered) — that IS a real verdict about the code, not a "nothing to
#: classify" sentinel, and a caller that conflated the two would retry the
#: exact quadraui#2992 compile-error shape this module exists to stop
#: retrying.
DEGRADED_VIA = frozenset({"model:call_failed", "model:schema_error", "model:low_confidence"})


def is_degraded_verdict(verdict: TriageVerdict) -> bool:
    """True when ``verdict`` is one of :data:`DEGRADED_VIA`'s "classifier is
    unusable" sentinels rather than a real decision about the code.

    The one normalized answer to "is this verdict real" — a caller that
    wants "no usable verdict degrades to some pre-existing default"
    semantics (e.g. :mod:`coord.drive`'s ``_decide_test``) should check this
    instead of re-deriving it from ``confidence``/``via`` itself.
    """
    return verdict.via in DEGRADED_VIA


# ── tier 1a: infra (delegates to coord.failure_class) ───────────────────────


def _infra_verdict(
    *,
    failure_reason: str | None,
    terminal_reason: str | None,
    usage_limit_reason: str | None,
    api_error_status: int | None,
    network_error_state: str | None,
    is_error: bool | None,
    result_text: str | None,
) -> TriageVerdict | None:
    classification = _classify_environmental(
        failure_reason=failure_reason,
        terminal_reason=terminal_reason,
        usage_limit_reason=usage_limit_reason,
        api_error_status=api_error_status,
        network_error_state=network_error_state,
        is_error=is_error,
        result_text=result_text,
    )
    if not classification.is_environmental:
        return None
    diagnosis = (
        f"{classification.reason}. coord.failure_class classified this as "
        f"environmental (kind={classification.kind}), which this taxonomy "
        "reports as infra rather than a verdict on the code. No model call "
        "was needed."
    )
    return TriageVerdict(
        bucket=BUCKET_INFRA,
        diagnosis=diagnosis,
        repro_capability=None,
        proposed_scope=ProposedScope(),
        confidence=1.0,
        via=f"deterministic:infra:{classification.kind}",
    )


# ── tier 1b: timeout-under-load (#4130) ──────────────────────────────────────

#: `coord.agent.format_timeout_load_note`'s exact suffix, embedded in the
#: plain-runner's own `SMOKE: fail timed out, ...` marker and, from there
#: (via `coord.notify`'s "headless smoke: {reason}" headline), in
#: `failure_reason`/`terminal_reason` — a short, coordinator-authored
#: string, never a worker's test-output prose, so safe to scan
#: unconditionally, same discipline as `coord.failure_class`'s own infra/
#: setup signatures. The load group is optional: `format_timeout_load_note`
#: renders `(host load at kill: unknown)` when the host's own
#: `os.getloadavg()` is unavailable (e.g. Windows).
_TIMEOUT_KILL_RE = re.compile(
    r"timed out\b.*?\(host load at kill: (?:1m=([0-9.]+), cores=(\d+)|unknown)\)",
    re.IGNORECASE,
)


def _timeout_verdict(
    failure_reason: str | None, terminal_reason: str | None,
) -> TriageVerdict | None:
    """#4130: a Test leg killed for hitting ITS OWN wall-clock timeout never
    produced a graded verdict — nothing ran to completion, so there is no
    code failure to diagnose, only infrastructure that ran out of time
    (routinely because a concurrent Work/tranche leg was starving the same
    sole-capability host — see `coord.dispatch.sole_test_capabilities`).

    Recognised by :data:`_TIMEOUT_KILL_RE` in *failure_reason* or
    *terminal_reason* — the SAME two coordinator-authored evidence channels
    :func:`_infra_verdict` already scans unconditionally, never a worker's
    own prose. The load figures (when the host could measure them) are
    folded into the diagnosis for operator visibility only; this rule fires
    on the timeout shape alone and never second-guesses it against a load
    threshold — the kill already proved the budget ran out, whatever the
    number says.
    """
    for text in (failure_reason, terminal_reason):
        if not text:
            continue
        m = _TIMEOUT_KILL_RE.search(text)
        if m is None:
            continue
        load1, cores = m.group(1), m.group(2)
        load_note = (
            f"a 1-minute load average of {load1} across {cores} core(s)"
            if load1 and cores
            else "load that could not be measured on that host"
        )
        return TriageVerdict(
            bucket=BUCKET_INFRA,
            diagnosis=(
                "Deterministic infra classification (#4130): the Test leg "
                "was killed for hitting its own wall-clock timeout — no "
                "exit status was ever produced, so there is nothing here "
                f"to grade as a code failure. The host reported {load_note} "
                "at the moment of the kill. No model call was needed."
            ),
            repro_capability=None,
            proposed_scope=ProposedScope(),
            confidence=1.0,
            via="deterministic:infra:timeout",
        )
    return None


# ── tier 1c: mechanical ──────────────────────────────────────────────────────

_RUSTC_ERROR_HEADER_RE = re.compile(r"^error(\[E\d+\])?:", re.MULTILINE)
_ARROW_LOCATION_RE = re.compile(r"^\s*-->\s*(\S+):\d+:\d+", re.MULTILINE)
_PYTEST_FAILED_PATH_RE = re.compile(r"^FAILED\s+(?!STAGES\b)(\S+?)(?:::|\s|$)", re.MULTILINE)


def _count_error_sites(errors: str, signals: FailureSignals) -> int:
    """How many distinct 'error sites' the mechanical rule's 'small count'
    gate measures. ``compile_error`` can fire multiple times (one rustc
    diagnostic per bad call site); ``ratchet``/``lint`` are boolean findings
    (one tripped ratchet IS one finding, regardless of how many lines its
    message spans), so each contributes exactly one. ``max(..., 1)`` so a
    compile error whose per-site header got cut by the evidence extractor's
    own byte budget still counts as "at least one", rather than zero.
    """
    count = 0
    if signals.compile_error:
        count += len(_RUSTC_ERROR_HEADER_RE.findall(errors))
    if signals.ratchet:
        count += 1
    if signals.lint:
        count += 1
    return max(count, 1)


def _implicated_paths(errors: str) -> list[str]:
    """Every file path a rustc ``-->`` location or a pytest ``FAILED``
    nodeid names in *errors*, deduped, order-preserving."""
    paths: list[str] = []
    seen: set[str] = set()
    for pattern in (_ARROW_LOCATION_RE, _PYTEST_FAILED_PATH_RE):
        for m in pattern.finditer(errors):
            p = m.group(1)
            if p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def _paths_match(a: str, b: str) -> bool:
    """Loose path equality: exact match, same basename (tolerates a
    relative-vs-rooted prefix difference between a rustc location and a
    diff's own path), or one is a directory prefix of the other (tolerates
    a ``## Files`` declaration naming a directory rather than every file in
    it)."""
    a = a.strip("/")
    b = b.strip("/")
    if a == b:
        return True
    if a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1]:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def _confined_to_known_files(implicated: Sequence[str], known_files: Sequence[str]) -> bool:
    """True when every path in *implicated* matches one of *known_files* —
    i.e. the failure sits entirely inside files the branch diff (or the
    issue's own ``## Files``) already touches, not somewhere the diff never
    went near. Deliberately NOT an extension/language match (any two ``.rs``
    files would then "confine" each other, which defeats the point of the
    check): the mechanical bucket is for a failure the PR's OWN edit
    plausibly caused in a file it already changed, not merely a failure in
    the same language elsewhere in the tree.

    Vacuously True when *implicated* is empty — a ratchet/lint finding with
    no per-file location to check has nothing to be unconfined FROM. False
    (never classified mechanical) when *implicated* is non-empty but
    *known_files* is empty — there is nothing to confirm confinement
    against, so this falls through to the model tier instead of guessing.
    """
    if not implicated:
        return True
    if not known_files:
        return False
    for path in implicated:
        if any(_paths_match(path, k) for k in known_files):
            continue
        return False
    return True


def _infer_repro_capability(
    paths: Sequence[str], machine_capabilities: Sequence[str]
) -> str | None:
    """Which capability (e.g. ``"macos"``) can reproduce this failure.

    Path-based evidence wins: a platform token appearing as a whole path
    COMPONENT (never a bare substring — a file named ``windowsill.rs`` must
    not match ``"windows"``) names the platform the failing code is
    specific to, regardless of which machine happened to run it. Falls back
    to the failing machine's own capability set only when the paths say
    nothing: if that machine declares exactly one platform-shaped
    capability, it already proved (by failing) that it can reproduce this.
    """
    for path in paths:
        parts = re.split(r"[/_.\-]+", path.lower())
        for token in _PLATFORM_CAPABILITY_TOKENS:
            if token in parts:
                return token
    platform_caps = [
        c.lower() for c in machine_capabilities if c.lower() in _PLATFORM_CAPABILITY_TOKENS
    ]
    if len(platform_caps) == 1:
        return platform_caps[0]
    return None


def _mechanical_verdict(
    evidence: FailureEvidence,
    diff_stat: DiffStat,
    declared_files: Sequence[str],
    machine_capabilities: Sequence[str],
) -> TriageVerdict | None:
    signals = evidence.signals
    which = [
        name
        for name, hit in (
            ("compile_error", signals.compile_error),
            ("ratchet", signals.ratchet),
            ("lint", signals.lint),
        )
        if hit
    ]
    if not which:
        return None

    error_count = _count_error_sites(evidence.errors, signals)
    if error_count > MECHANICAL_MAX_ERROR_COUNT:
        return None

    implicated = _implicated_paths(evidence.errors)
    known_files = tuple(diff_stat.files) + tuple(declared_files)
    if not _confined_to_known_files(implicated, known_files):
        return None
    # A compile error that named no file at all can't have its confinement
    # verified — fall through to the model tier rather than guess.
    if signals.compile_error and not implicated:
        return None

    scope_files = tuple(implicated) if implicated else known_files
    capability = _infer_repro_capability(scope_files, machine_capabilities)
    diagnosis = (
        f"Deterministic mechanical classification (#4093): {error_count} error "
        f"site(s) matching {', '.join(which)}, confined to files the branch "
        "already touches. No model call was needed — the signal and file "
        "confinement are sufficient on their own."
    )
    if capability:
        diagnosis += f" Reproducible on a '{capability}' machine."
    return TriageVerdict(
        bucket=BUCKET_MECHANICAL,
        diagnosis=diagnosis,
        repro_capability=capability,
        proposed_scope=ProposedScope(
            files=scope_files, estimated_line_delta=error_count * 2
        ),
        confidence=1.0,
        via=f"deterministic:mechanical:{'+'.join(which)}",
    )


# ── tier 2: one cheap claude -p call ─────────────────────────────────────────

_TRIAGE_SYSTEM_PROMPT = (
    "You are a cheap, independent failure-triage classifier for a CI "
    "pipeline. You are given structured EVIDENCE already extracted from a "
    "failed test run (never the raw log), the branch's diff stat, the "
    "issue's declared files, the failing machine's capabilities, and "
    "whether the commit SHA changed since the last passing run. Decide WHY "
    "the run failed and reply with EXACTLY ONE JSON object and nothing "
    "else, matching this schema:\n\n"
    '{"bucket": "infra" | "flaky" | "mechanical" | "substantive" | "human", '
    '"diagnosis": "two to four sentences", '
    '"repro_capability": "<capability that can reproduce this, e.g. '
    '\\"macos\\", or null>", '
    '"proposed_scope": {"files": ["path", ...], "estimated_line_delta": '
    '<integer>}, '
    '"confidence": <float between 0.0 and 1.0>}\n\n'
    "Bucket meanings:\n"
    "  infra - the test infrastructure itself failed, not the code under "
    "test\n"
    "  flaky - the failure is non-deterministic and would likely pass on "
    "retry; NEVER use this bucket when a compile error is present — a "
    "compile error is a durable defect, never noise\n"
    "  mechanical - a small, well-scoped, rote fix (a ratchet count, a "
    "lint nit, a sealed-path violation) that needs no design judgement\n"
    "  substantive - a genuine behavioural bug that needs real "
    "investigation\n"
    "  human - the failure reflects a product or policy decision a human "
    "must make, not something a model should decide alone\n\n"
    "If you are not confident, report a LOW confidence value rather than "
    "guessing a bucket — a low-confidence verdict is treated as substantive "
    "by the caller, which is the safe default. Reply with the JSON object "
    "only, no prose before or after it."
)


def _build_triage_user_message(
    evidence: FailureEvidence,
    diff_stat: DiffStat,
    declared_files: Sequence[str],
    machine_capabilities: Sequence[str],
    sha_changed: bool,
) -> str:
    evidence_dict = evidence.to_dict()
    evidence_dict["errors"] = _excerpt(evidence_dict.get("errors", ""), _ERRORS_EXCERPT_CHARS)
    payload = {
        "evidence": evidence_dict,
        "diff_stat": diff_stat.to_dict(),
        "declared_files": list(declared_files),
        "machine_capabilities": list(machine_capabilities),
        "sha_changed_since_last_pass": bool(sha_changed),
    }
    return json.dumps(payload, indent=2)


def _build_triage_command(*, model: str, claude_bin: str | None) -> list[str]:
    from coord.providers.claude import ClaudeProvider  # noqa: PLC0415

    provider = ClaudeProvider(binary=claude_bin)
    cmd = provider.oneshot_command(system_prompt=_TRIAGE_SYSTEM_PROMPT, output_format="json")
    cmd.extend(["--model", model])
    return cmd


def _subprocess_env(max_thinking_tokens: int | None) -> dict[str, str]:
    env = dict(os.environ)
    if max_thinking_tokens is not None:
        env["MAX_THINKING_TOKENS"] = str(max_thinking_tokens)
    return env


def _parse_json_envelope(stdout: str) -> tuple[str, float | None]:
    """Pull ``(result_text, total_cost_usd)`` out of a ``claude -p
    --output-format json`` envelope. Falls back to the raw stdout (and no
    cost) for a malformed/unexpected shape — best-effort, matches
    ``coord.liveness_auditor``'s/``coord.brain.call_claude``'s own
    fallback."""
    try:
        outer = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout.strip(), None
    if not isinstance(outer, dict):
        return stdout.strip(), None
    text = outer.get("result")
    if not isinstance(text, str):
        text = stdout.strip()
    cost = outer.get("total_cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        cost = None
    else:
        cost = float(cost)
    return text, cost


def _parse_verdict_json(text: str) -> dict | None:
    """Pull the verdict JSON object out of the model's reply text.

    Tries a strict parse first; falls back to the substring between the
    first ``{`` and last ``}`` so a model that wraps the object in a
    sentence or markdown fence still parses. Returns ``None`` (never
    raises) for anything that isn't valid JSON or isn't a ``dict`` once
    parsed.
    """
    if not text or not text.strip():
        return None
    candidate = text.strip()
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _verdict_from_json(raw: Mapping[str, object]) -> TriageVerdict | None:
    """Build a :class:`TriageVerdict` from the model's parsed JSON reply, or
    ``None`` if it doesn't match the schema closely enough to trust — a
    missing/unrecognised ``bucket``, a non-numeric or out-of-range
    ``confidence``, or a malformed ``proposed_scope``. Never raises."""
    bucket = raw.get("bucket")
    if not isinstance(bucket, str) or bucket not in KNOWN_BUCKETS:
        return None

    diagnosis = raw.get("diagnosis")
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        return None

    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        return None

    repro_capability = raw.get("repro_capability")
    if repro_capability is not None and not isinstance(repro_capability, str):
        return None

    scope_raw = raw.get("proposed_scope")
    files: tuple[str, ...] = ()
    delta = 0
    if isinstance(scope_raw, Mapping):
        raw_files = scope_raw.get("files")
        if isinstance(raw_files, list) and all(isinstance(f, str) for f in raw_files):
            files = tuple(raw_files)
        raw_delta = scope_raw.get("estimated_line_delta")
        if isinstance(raw_delta, (int, float)) and not isinstance(raw_delta, bool):
            delta = int(raw_delta)
    elif scope_raw is not None:
        return None

    return TriageVerdict(
        bucket=bucket,
        diagnosis=diagnosis.strip(),
        repro_capability=(repro_capability.strip() or None) if repro_capability else None,
        proposed_scope=ProposedScope(files=files, estimated_line_delta=delta),
        confidence=confidence,
        via="model",
    )


@dataclass
class ModelCallOutcome:
    """Raw result of one :func:`_run_triage_model_call`."""

    raw_json: dict | None
    raw_text: str
    cost_usd: float | None = None
    error: str | None = None


def _run_triage_model_call(
    user_message: str,
    *,
    model: str,
    claude_bin: str | None,
    timeout: float,
    max_thinking_tokens: int | None,
) -> ModelCallOutcome:
    """One one-shot ``claude -p`` subprocess call. Never raises: a missing
    binary, a timeout, a non-zero exit, or unparseable JSON all come back as
    ``ModelCallOutcome(raw_json=None, ...)`` with an ``error`` set, so a
    flaky model tier can never crash :func:`triage_failure`."""
    cmd = _build_triage_command(model=model, claude_bin=claude_bin)
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd,
            input=user_message,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_subprocess_env(max_thinking_tokens),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ModelCallOutcome(raw_json=None, raw_text="", error=f"{type(exc).__name__}: {exc}")

    if result.returncode != 0:
        return ModelCallOutcome(
            raw_json=None,
            raw_text=result.stdout or "",
            error=f"exit {result.returncode}: {(result.stderr or '').strip()[:200]}",
        )

    text, cost = _parse_json_envelope(result.stdout)
    verdict_json = _parse_verdict_json(text)
    return ModelCallOutcome(
        raw_json=verdict_json,
        raw_text=text,
        cost_usd=cost,
        error=None if verdict_json is not None else "no valid verdict JSON in reply",
    )


def _run_model_tier(
    evidence: FailureEvidence,
    diff_stat: DiffStat,
    declared_files: Sequence[str],
    machine_capabilities: Sequence[str],
    sha_changed: bool,
    *,
    model: str,
    claude_bin: str | None,
    timeout: float,
    max_thinking_tokens: int | None,
    min_confidence: float,
) -> TriageVerdict:
    user_message = _build_triage_user_message(
        evidence, diff_stat, declared_files, machine_capabilities, sha_changed
    )
    outcome = _run_triage_model_call(
        user_message,
        model=model,
        claude_bin=claude_bin,
        timeout=timeout,
        max_thinking_tokens=max_thinking_tokens,
    )
    if outcome.raw_json is None:
        return _fallback_substantive(
            reason=(
                "model tier call failed or returned no parseable JSON "
                f"(#4093): {outcome.error or 'unknown error'} — defaulting "
                "to substantive, today's behaviour"
            ),
            via="model:call_failed",
        )

    verdict = _verdict_from_json(outcome.raw_json)
    if verdict is None:
        return _fallback_substantive(
            reason=(
                "model tier replied with JSON that does not match the "
                "verdict schema (#4093) — defaulting to substantive"
            ),
            via="model:schema_error",
        )

    if verdict.confidence < min_confidence:
        return _fallback_substantive(
            reason=(
                f"model tier confidence {verdict.confidence:.2f} is below "
                f"the {min_confidence:.2f} floor (#4093) — a low-confidence "
                "guess degrades to the existing fix-round path rather than "
                "risking a wrong flaky/mechanical skip"
            ),
            via="model:low_confidence",
        )

    if verdict.bucket == BUCKET_FLAKY and evidence.signals.compile_error:
        return _fallback_substantive(
            reason=(
                "model tier proposed 'flaky' but the evidence carries a "
                "compile_error signal (#4093) — a compile error is a "
                "durable defect, never noise, so this degrades to "
                "substantive"
            ),
            via="model:compile_error_guard",
        )

    return verdict


# ── entry point ───────────────────────────────────────────────────────────


def triage_failure(
    *,
    evidence: FailureEvidence,
    diff_stat: DiffStat | None = None,
    declared_files: Sequence[str] = (),
    machine_capabilities: Sequence[str] = (),
    sha_changed: bool = True,
    failure_reason: str | None = None,
    terminal_reason: str | None = None,
    usage_limit_reason: str | None = None,
    api_error_status: int | None = None,
    network_error_state: str | None = None,
    is_error: bool | None = None,
    result_text: str | None = None,
    model: str = DEFAULT_MODEL,
    claude_bin: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_thinking_tokens: int | None = DEFAULT_MAX_THINKING_TOKENS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> TriageVerdict:
    """Decide WHY a Test-stage failure happened.

    Tries, in order: the infra rule (delegates to
    :func:`coord.failure_class.classify_failure` over the usual evidence
    channels — *failure_reason*/*terminal_reason*/*usage_limit_reason*/
    *api_error_status*/*network_error_state*/*is_error*/*result_text*, all
    optional and independent, same contract as that function); the
    mechanical rule (*evidence*, *diff_stat*, *declared_files*,
    *machine_capabilities*); and finally one cheap model call (*model*
    default :data:`DEFAULT_MODEL`, haiku — pass ``"sonnet"`` or any other
    resolvable name/alias for a stronger tier). *sha_changed* — whether the
    commit SHA differs from the last passing run — is handed to the model
    tier as context (strong flaky evidence when False) but is not consulted
    by either deterministic rule.

    Never raises: a missing/misbehaving ``claude`` binary degrades the model
    tier to :data:`BUCKET_SUBSTANTIVE`, the same as every other model-tier
    failure mode (see :func:`_run_model_tier`).
    """
    diff_stat = diff_stat or DiffStat()

    infra = _infra_verdict(
        failure_reason=failure_reason,
        terminal_reason=terminal_reason,
        usage_limit_reason=usage_limit_reason,
        api_error_status=api_error_status,
        network_error_state=network_error_state,
        is_error=is_error,
        result_text=result_text,
    )
    if infra is not None:
        return infra

    timeout_infra = _timeout_verdict(failure_reason, terminal_reason)
    if timeout_infra is not None:
        return timeout_infra

    mechanical = _mechanical_verdict(evidence, diff_stat, declared_files, machine_capabilities)
    if mechanical is not None:
        return mechanical

    return _run_model_tier(
        evidence,
        diff_stat,
        declared_files,
        machine_capabilities,
        sha_changed,
        model=model,
        claude_bin=claude_bin,
        timeout=timeout,
        max_thinking_tokens=max_thinking_tokens,
        min_confidence=min_confidence,
    )
