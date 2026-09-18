"""#3367: detect and track machine-level faults from a terminal review/work
failure — a THIRD orthogonal question alongside the two existing failure
classifiers in this package.

NOT `coord.failure_class` (environmental vs. work, for resume scheduling and
the liveness-probe backoff — "is it worth waiting for THIS environment to
come back?"). NOT `coord.failure_classifier` (compliance vs. capability, for
model-escalation — "should the next attempt use a pricier model?"). This
module answers: is the MACHINE that ran this leg unable to run anything at
all right now, regardless of what the work needed or which model tier it
used?

WHY THIS EXISTS. A worker that fails at turn 1, in well under a second, for
$0, with an authentication error has told you something about the HOST, not
the work (#3367's own evidence: four identical `precision` dispatches, each
"completed in 0.1s, 1 turns, $0.00"). Before this module that failure was
indistinguishable from a genuine work defect: it consumed the issue's
bounded retry budget, retried onto the SAME machine (nothing removed it from
the routing pool), repeated identically until the budget was exhausted, and
the issue — plus everything queued behind it — went `blocked`. `coord
status` kept reporting the host `online • idle` throughout.

TWO INDEPENDENT SIGNALS, EITHER ONE IS SUFFICIENT:

1. An anchored auth-failure token in the failure text — the literal wording
   `claude`'s own OAuth refresh path emits ("Failed to authenticate", "OAuth
   session expired", "could not be refreshed") plus the OAuth2 error codes a
   dead refresh token returns (`invalid_grant`, `invalid_client`). Scans
   coordinator-authored `failure_reason`/`terminal_reason` unconditionally
   (mirrors `coord.failure_class`'s own convention) and worker-authored
   `result_text` only when `is_error` is truthy, for the same reason that
   module gives: a worker that merely *discusses* authentication in its own
   transcript must not misclassify as a dead host.
2. The "instant, zero-turn, zero-cost" shape itself, regardless of what the
   error text says — see `is_instant_zero_cost_failure`. The issue's own
   framing: four identical near-zero-duration, zero-cost failures is not
   ambiguous, no matter what the message says. This is deliberately generic
   (no text pattern required) because a dead OAuth token is not the only way
   a host can be unable to run anything at all.

Like `coord.failure_class`, deliberately lopsided the SAME direction: a
false negative here (fails to notice a broken machine) just falls through to
the pre-existing bounded work-failure retry — no worse than before #3367. A
false positive (treats a genuine failure as a machine fault) would let a
live, work-capable machine dodge its own retry budget and never surface a
real defect, so `is_instant_zero_cost_failure` requires BOTH turns and cost
to look right, never either alone, and the text scan is anchored on
wire-format tokens that cannot plausibly appear in ordinary work-failure
prose.

CONSECUTIVE-FAULT TRACKING AND AUTO-PAUSE. `record_fault`/`clear_fault`
persist a tiny per-machine counter at `~/.coord/machine_faults.json` — the
same directory every other piece of local coordinator state lives in
(`coord.machine_pause`, `coord.state`). `maybe_auto_pause` pulls a machine
out of the routing pool through the EXISTING `coord.machine_pause.pause()`
primitive once the counter reaches `AUTO_PAUSE_THRESHOLD`, reusing the
fleet's one pause mechanism (already daemon-aware, already rendered by
`coord status`) rather than inventing a second, independent "this machine is
unavailable" concept that could silently disagree with it (#2096: "one
question, one answer").

CALLER CONTRACT, post-review-#1 tightening: `record_fault`/`maybe_auto_pause`
are mechanism only — this module does not decide which classifications are
allowed to drive them, and on its own `classify_machine_fault` returning
`is_machine_fault=True` is NOT sufficient reason to call them. The caller
(`coord.drive`'s `_machine_fault_warnings`) only does so for `signal ==
"auth_failure"`. The `instant_zero_cost` shape signal is DELIBERATELY
excluded from feeding the counter/pause, even though it still earns the
"redispatch without charging the issue's retry budget" treatment: a
pre-launch failure that never got a worker process running at all (bad
`pull_repos`/`repo_path` entry, worktree setup failure, a raw spawn
`OSError` — none of these are a `claude` OAuth problem) lands on
`num_turns=0`/`cost_usd=None` for the exact same reason a genuine dead
credential does (see `is_instant_zero_cost_failure`'s own note below), and a
systemic config defect reproduces IDENTICALLY on every machine that shares
it. Letting the shape signal alone drive `record_fault` would auto-pause the
whole fleet one host at a time as each is tried in turn — the same
wrong-culprit failure class this module exists to fix, just relocated from
"one host's auth" to "any pre-launch failure, fleet-wide". See
`coord.drive._machine_fault_warnings` for the enforcement point.

SCOPE NOTE: the fault *counter* itself is local-only — it lives wherever the
`coord drive` decision loop happens to run, and is not routed through the
daemon the way `coord.machine_pause`'s pause SET is. The PAUSE it triggers
IS fully fleet-wide (routed through `machine_pause.pause()`), so the actual
behavioural fix — stop dispatching to a dead host — applies everywhere;
only the human-facing "why" narrative `coord status` renders locally
(`describe()`) is scoped to the box that recorded the fault. A fleet-wide
fault ledger is future work if that gap ever actually bites.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from coord.platform_paths import default_coord_dir

# ── text-based signal ───────────────────────────────────────────────────────

# Anchored on `claude` CLI's own OAuth-refresh wording (the literal text
# quoted in #3367's evidence) plus the OAuth2 error codes a dead refresh
# token returns — none of these are phrases a coordinator-authored
# work-failure summary, or a worker's ordinary prose, would ever contain.
_AUTH_FAILURE_RE = re.compile(
    r"(Failed to authenticate|OAuth session expired|could not be refreshed"
    r"|invalid_grant|invalid_client)",
    re.IGNORECASE,
)

# How much worker-authored terminal prose to consider — mirrors
# `coord.failure_class._RESULT_TEXT_LIMIT`: anything past this is a
# transcript that should not have been passed here in the first place.
_RESULT_TEXT_LIMIT = 4000


def is_auth_failure_text(text: str | None) -> bool:
    """True when *text* carries the literal OAuth/auth-refresh wording."""
    if not text:
        return False
    return bool(_AUTH_FAILURE_RE.search(text))


# ── shape-based signal ──────────────────────────────────────────────────────

#: A failure at or before the first turn is the "looked dispatched but never
#: actually ran" shape #3367 describes — no text pattern required.
INSTANT_FAILURE_MAX_TURNS = 1


def is_instant_zero_cost_failure(
    *, num_turns: int | None, cost_usd: float | None
) -> bool:
    """True when the leg produced no measurable work product at all.

    Both conditions must hold — `num_turns` at or below
    `INSTANT_FAILURE_MAX_TURNS` AND `cost_usd` falsy (`0` or unmeasured).
    Deliberately not "either": a leg that spent a real turn or a real dollar
    is evidence of an actual attempt, no matter how it then failed, and must
    keep going down the ordinary work-failure retry path.

    `num_turns=None` (never measured) does NOT count as "zero turns" — an
    absent measurement is not evidence either way, unlike `cost_usd=None`
    (unmeasured cost on a terminal-failed row overwhelmingly means "nothing
    was ever captured because nothing ran", the same reasoning
    `coord.failure_class` module docs draw for its own environmental
    signals). If this ever proves too eager, the fix belongs here, not at
    a call site.
    """
    if num_turns is None:
        return False
    turns_ok = 0 <= num_turns <= INSTANT_FAILURE_MAX_TURNS
    cost_ok = cost_usd is None or cost_usd <= 0.0
    return turns_ok and cost_ok


@dataclass(frozen=True)
class MachineFaultClassification:
    """Is this terminal failure a fault in the MACHINE, not the work?"""

    is_machine_fault: bool
    reason: str
    signal: str | None = None

    def to_dict(self) -> dict:
        return {
            "is_machine_fault": self.is_machine_fault,
            "reason": self.reason,
            "signal": self.signal,
        }


def classify_machine_fault(
    *,
    failure_reason: str | None = None,
    terminal_reason: str | None = None,
    result_text: str | None = None,
    is_error: bool | None = None,
    num_turns: int | None = None,
    cost_usd: float | None = None,
) -> MachineFaultClassification:
    """Classify a terminal failure as a machine fault, or not.

    See the module docstring for the two independent signals (checked in
    this order — text first, since it names the specific cause). Anything
    that matches neither is NOT a machine fault, including "no evidence
    supplied at all" — same fail-closed posture as
    `coord.failure_class.classify_failure`.
    """
    haystacks: list[str] = [t for t in (failure_reason, terminal_reason) if t]
    if is_error and result_text:
        haystacks.append(result_text[:_RESULT_TEXT_LIMIT])
    for text in haystacks:
        if is_auth_failure_text(text):
            return MachineFaultClassification(
                is_machine_fault=True,
                reason=(
                    "machine fault (auth): the host's Claude OAuth "
                    "credentials are dead — this is not a defect in the "
                    "work (#3367)"
                ),
                signal="auth_failure",
            )

    if is_instant_zero_cost_failure(num_turns=num_turns, cost_usd=cost_usd):
        turns_str = "?" if num_turns is None else str(num_turns)
        cost_str = f"{cost_usd:.2f}" if cost_usd else "0.00"
        return MachineFaultClassification(
            is_machine_fault=True,
            reason=(
                f"machine fault (instant failure): {turns_str} turn(s), "
                f"${cost_str} — the host produced no work product at all "
                "(#3367)"
            ),
            signal="instant_zero_cost",
        )

    return MachineFaultClassification(
        is_machine_fault=False,
        reason="not a machine fault: no auth/instant-failure signal",
    )


# ── consecutive-fault tracking + auto-pause ─────────────────────────────────

#: How many CONSECUTIVE machine faults on the SAME machine (regardless of
#: which issue/assignment triggered each one) before it is pulled out of the
#: routing pool automatically. #3367's own evidence was 4 identical
#: failures before a human intervened by hand; 3 costs one fewer wasted
#: dispatch than that while still ruling out a single flake.
AUTO_PAUSE_THRESHOLD = 3

_FAULTS_FILENAME = "machine_faults.json"


def _state_path() -> Path:
    """Resolve the fault-state file path.

    ``$COORD_MACHINE_FAULT_STATE`` overrides first — the same seam
    ``coord.github_throttle``'s own ``_state_path`` (`$COORD_GITHUB_BACKOFF_
    STATE`, #2809), ``coord.notifier.store.state_path`` (#1632), and
    several other per-file coordinator state stores use, so a test can
    redirect this with a one-line ``monkeypatch.setenv`` instead of
    patching a private function — see those modules' docstrings, and
    #2101, for why a state file a test *can* write to the operator's real
    ``~/.coord`` is a state file a test *will* eventually write there by
    accident.
    """
    override = os.environ.get("COORD_MACHINE_FAULT_STATE")
    if override:
        return Path(override).expanduser()
    return default_coord_dir() / _FAULTS_FILENAME


def _load_raw() -> dict:
    path = _state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_raw(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tempfile in the same dir then rename, so a crashed
    # writer can never leave a partially-written file in place — same
    # pattern `coord.machine_pause._save_state` uses for its sibling file.
    fd, tmp = tempfile.mkstemp(
        prefix=".machine_faults.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_fault(machine_name: str, reason: str, *, now: float | None = None) -> int:
    """Bump *machine_name*'s consecutive-fault counter and persist *reason*
    as the latest cause. Returns the new consecutive count.
    """
    data = _load_raw()
    raw_entry = data.get(machine_name)
    entry = raw_entry if isinstance(raw_entry, dict) else {}
    consecutive = int(entry.get("consecutive") or 0) + 1
    data[machine_name] = {
        "consecutive": consecutive,
        "reason": reason,
        "updated_at": now if now is not None else time.time(),
    }
    _save_raw(data)
    return consecutive


def clear_fault(machine_name: str) -> None:
    """Reset *machine_name*'s consecutive-fault counter to zero.

    Call this on any terminal outcome for that machine that is NOT itself a
    machine fault (a genuine success, or a genuine work/review failure) —
    without it, a stale count from an unrelated incident days ago could
    combine with one fresh fault to trigger an auto-pause that reads as
    "3 in a row" when it was really "1 fresh + 2 ancient".
    """
    data = _load_raw()
    if machine_name in data:
        del data[machine_name]
        _save_raw(data)


def consecutive_faults(machine_name: str) -> int:
    """The current consecutive-fault count for *machine_name* — `0` when
    it has none recorded (the overwhelmingly common case).
    """
    entry = _load_raw().get(machine_name)
    if not isinstance(entry, dict):
        return 0
    return int(entry.get("consecutive") or 0)


def describe(machine_name: str) -> str | None:
    """Human-readable one-liner for `coord status`, or `None` when
    *machine_name* has no recorded fault.
    """
    entry = _load_raw().get(machine_name)
    if not isinstance(entry, dict):
        return None
    consecutive = int(entry.get("consecutive") or 0)
    if consecutive <= 0:
        return None
    reason = entry.get("reason") or "unknown"
    plural = "" if consecutive == 1 else "s"
    return f"{consecutive} consecutive machine fault{plural} — last: {reason}"


def maybe_auto_pause(
    machine_name: str, *, threshold: int = AUTO_PAUSE_THRESHOLD
) -> tuple[bool, int]:
    """Pause *machine_name* if its consecutive-fault count has reached
    *threshold*.

    Returns `(just_paused, consecutive)`. `just_paused` is `True` only on
    the actual transition — `coord.machine_pause.pause()` itself already
    returns `False` (a no-op) for an already-paused machine, so a caller
    polling this every retry never re-reports the same pause as new.

    Routed through `coord.machine_pause.pause()` — the fleet's one routing-
    pause mechanism (#2096, "one question, one answer") — rather than a
    second, independent notion of "this machine is unavailable" that
    `coord status` and every dispatcher would separately have to learn to
    honour.
    """
    consecutive = consecutive_faults(machine_name)
    if consecutive < threshold:
        return False, consecutive
    from coord.machine_pause import pause  # noqa: PLC0415

    try:
        changed = pause(machine_name)
    except Exception:  # noqa: BLE001
        # `pause()` fails LOUDLY on a thin client's transport error by
        # design (#1563's own contract for explicit user actions) — but
        # auto-pause is not a user pressing a button, it's a drive loop
        # mid-decision, and a transport blip must not crash it. The
        # caller's own warning already names the machine as faulted
        # regardless of whether the pause itself landed; the next terminal
        # failure on this machine tries again.
        changed = False
    return changed, consecutive
