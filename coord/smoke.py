"""Smoke-test orchestration — auto-queue validation on a capable machine.

When a worker finishes, the work often needs validation hardware the worker
didn't have. Example: a GTK key-routing fix built on a no-GTK server needs a
machine with GTK to actually verify the popup works. This module:

1. Reads the worker's diff (which files changed).
2. Looks at `smoke_tests.capability_rules` — each rule maps a file-path
   prefix to a set of required machine capabilities.
3. Picks a machine that has all required capabilities, preferring one
   different from the worker.
4. Dispatches a `type="smoke"` assignment with a briefing that tells
   `claude -p` to fetch the branch, run the smoke command, and report
   pass/fail through its exit code.

Public entry points:

- `match_rules(touched_files, rules)`  — pure: returns the union of required
  capabilities for any rule whose `files` prefix matches a touched file.
- `resolve_smoke_command(repo, smoke_cfg, touched_files=None)` — pure: picks
  the Test-stage command, provenance included. A matched rule's own
  `command` (#3056) outranks the repo/fleet-wide sources — see
  `resolve_rule_command` for the multiple-matching-rules tiebreak.
- `rank_smoke_machines(required_caps, repo, worker_machine, board, config)` —
  every capability-matched machine, best first (#1672).
- `pick_smoke_machine(required_caps, worker_machine, board, config)` — picks
  a capable machine, preferring the worker's own (its build cache is warm —
  #1402); pass `prefer_worker=False` for the old different-machine-first
  order. Thin wrapper over `rank_smoke_machines` — the head of the ranking.
- `dispatch_smoke(completed, board, config, ...)` — the full path; called
  from reconcile when a work assignment transitions to done.

#1819: the unit a Test run measures is the **(branch, base)** pair, not the
work row that asked for it. Three guards in `dispatch_smoke` follow from that
— a branch-scoped in-flight dedupe, a supersession check that skips a work row
a later row replaced on the same branch, and a refusal to stamp the transient
`running` marker over a verdict that already exists. Together they are what
stops a fix round (which reuses the branch by design, so one branch carries
two `work` rows) from putting two machines on the identical suite and then
looping forever as each re-dispatch retracts the verdict the last one landed.

Why a separate module from `coord/review.py`: smoke tests target machine
capabilities (GTK/terminal/CUDA), not session independence. The selection
algorithm is different — for reviews we want a *different* machine for
independence; for smoke we want a *capable* machine for hardware, and
"different" is only a tie-breaker.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from datetime import datetime

import httpx

from coord import github_ops
from coord.config import Config, SmokeRule, SmokeTestsConfig
from coord.dispatch import AGENT_PORT
from coord.models import WORK_LIKE_TYPES, Assignment, Board, Machine
# #2170: the SAME marker/exit-code convention `scripts/coord-test-runner.sh`
# and `coord.revalidate` use for a red baseline — imported (not re-literalled)
# so the dispatched smoke agent's instructions can never drift from what the
# runner actually emits, or from what `coord.notify` parses back out of the
# agent's own transcript (see `_smoke_baseline_red_reason` there).
from coord.revalidate import BASELINE_RED_OUTPUT_MARKER, RUNNER_BASELINE_RED_EXIT

logger = logging.getLogger("coord.smoke")

# #2024: assignment ids whose `test-mode:smoke` Test-stage skip has already
# been reported this process (see `dispatch_pending_smoke`). Process-lifetime,
# not persisted — the point is one clear statement per daemon run, not an
# every-tick repeat of a refusal that is expected to hold for hours.
#
# Deliberately never pruned (e.g. when an operator clears the block with
# `coord set-test-mode <repo> <issue> auto` or records a verdict by hand):
# membership is a small, immutable assignment-id string per row that ever hit
# this skip, so the set's lifetime cost is bounded by "how many rows stalled
# on this policy while the daemon has been up" — negligible next to the board
# itself, and a daemon restart clears it for free (the next occurrence logs
# again, which is exactly when an operator wants to hear it).
_TEST_MODE_SKIP_LOGGED: set[str] = set()


SMOKE_SYSTEM_PROMPT = f"""\
You are a smoke-test runner dispatched by the coordinator. \
Your only job: pull the branch, run the smoke command, report pass/fail.

Rules:
- Do NOT edit source files. Do NOT push commits. You only validate.
- You MAY perform test-environment SETUP the smoke command needs — creating
  a venv, `pip install`-ing dev deps, writing build artifacts. None of that
  touches the branch's source; it is exactly what the smoke command itself
  does when run locally, so do it without asking.
- A repo with a quadraui sibling PATH dependency (e.g. vimcode's
  `quadraui-pin.txt`, #638/#625) needs a `quadraui` checkout next to yours —
  but `~/src/quadraui` (and any `.../quadraui` symlink you find already in
  place) is ONE mutable checkout SHARED by every concurrent assignment on
  this machine (#2804). NEVER `git checkout`/`switch`/`pull` inside it and
  NEVER repoint a shared `quadraui` symlink — another assignment may be
  relying on it at a different rev *right now*, and moving it out from
  under that assignment is exactly the scheduling-dependent phantom
  red/green #2804 exists to stop. Give yourself a PRIVATE, disposable
  checkout instead: `git -C ~/src/quadraui worktree add --detach <a path
  inside YOUR OWN worktree, e.g. ./quadraui-sibling> <the exact pinned
  rev>`, then point your build's sibling reference at that private path.
  `git worktree add` never touches `~/src/quadraui`'s own HEAD or working
  tree, so it's safe no matter what else is running concurrently — remove
  it with `git -C ~/src/quadraui worktree remove <path>` when you're done,
  but if you can't, it dies with your worktree; either way, never leave
  `~/src/quadraui` itself checked out anywhere but its own branch.
- Do NOT run `gh` commands. The coordinator owns GitHub interactions.
- You MAY run git, build commands, and test commands.
- THE VERDICT IS THE LINE YOU PRINT, NOT YOUR EXIT CODE (#2244). Print \
exactly one final line at the start of a line: `SMOKE: pass`, \
`SMOKE: fail <one-line reason>`, or `SMOKE: baseline-red <one-line reason>` \
(see step 4). An `exit 1` inside a tool call ends that tool call, not this \
session — the coordinator sees exit 0 either way, so a run whose suite failed \
and whose marker is missing is recorded as NO verdict (never a pass) and the \
Test stage has to be re-run. Exit with the matching code as well, for the \
non-headless lanes that do read it.
- NEVER END YOUR TURN WITH THE SUITE STILL RUNNING (#2272). Your smoke \
command can easily exceed the ~600s Bash ceiling; when it does, the harness \
moves it to the BACKGROUND and hands you a task id instead of an exit status. \
That is not a result — it is "no answer yet". Narrating that you kicked the \
suite off and stopping there prints no marker, so the Test stage records NO \
verdict and re-dispatches; because the ceiling is deterministic, every \
re-dispatch does exactly the same thing, which is why this loop bills \
indefinitely. Instead: POLL the backgrounded task to completion from THIS \
same session, in bounded steps, with `BashOutput`/`TaskOutput` and the task \
id, and read its REAL exit status before you report anything. Do NOT use a \
foreground `until ! pgrep ...; do sleep ...; done` wait — it hits the \
identical 600s wall.
- NEVER USE `Monitor` OR ANY OTHER AWAIT-A-NOTIFICATION TOOL TO WAIT FOR A \
RESULT (#2301). Those tools work by ending your turn so the harness can wake \
the model back up once the condition they're watching fires — that only \
resumes anything in an INTERACTIVE session. This is a one-shot `claude -p` \
session: ending your turn ends the session itself, permanently, and no \
notification will ever arrive to resume it. The backgrounded suite you were \
polling is then reaped mid-run by the coordinator, and you have printed no \
verdict marker — exactly the silent failure the previous bullet exists to \
prevent, just reached by a different tool. Only `BashOutput`/`TaskOutput` \
return synchronously without ending your turn; those are the only \
correct way to poll a backgrounded task from here. If you genuinely cannot \
obtain an exit status before you run out of room, print `SMOKE: fail <the \
smoke command never returned an exit status within this session>` rather \
than ending mute: a stated verdict is recoverable, silence is not.

Where you are:
- You are in a dedicated git worktree created for this run. Every git command \
below runs THERE, in your current directory.
- Do NOT `cd` to the machine's shared base checkout (`~/src/<repo>`), and do \
NOT `git checkout` / `git switch` inside it. Leaving that checkout parked on a \
feature branch makes every later dispatch against that branch on this machine \
fail (#1694).

Steps:
1. `git fetch origin && git checkout <branch>` (the branch is in your \
briefing) — in your worktree, never in the base checkout.
2. Run the smoke command from the briefing. Capture stdout/stderr. If the \
harness backgrounds it for exceeding the 600s Bash ceiling, poll that task to \
completion and use ITS exit status below — "I started it" is not an outcome \
(#2272).
3. If it exits 0 → print `SMOKE: pass` and exit 0.
4. If it exits {RUNNER_BASELINE_RED_EXIT} AND its output contains a line \
starting with literal text `{BASELINE_RED_OUTPUT_MARKER}` (#2170) — the smoke \
command already re-ran its own failures against the merge-base in this same \
environment and found every one of them fails there too. That is a statement \
about THIS MACHINE, not the branch: do NOT report it as `SMOKE: fail`, and do \
NOT report it as `SMOKE: pass` either (that would hide a red baseline). Print \
`SMOKE: baseline-red <one-line reason>` (reuse the runner's own \
`{BASELINE_RED_OUTPUT_MARKER}` line as the reason) and exit \
{RUNNER_BASELINE_RED_EXIT} yourself, matching the smoke command's own exit \
code.
5. Otherwise (any other non-zero exit, or a failure with no \
`{BASELINE_RED_OUTPUT_MARKER}` line) → print `SMOKE: fail <short reason>` and \
exit non-zero (any code other than 0 or {RUNNER_BASELINE_RED_EXIT}).
6. If your briefing gives you a `coord test ...` command, run it as your LAST \
step with the flag matching your verdict (#2244). That writes the verdict \
straight to the board and is authoritative; the printed marker is the backup. \
A `baseline-red` verdict has no `coord test` flag — print the marker only.
"""


# ── Rule matching ───────────────────────────────────────────────────────────


def match_rules(touched_files: list[str], rules: list[SmokeRule]) -> list[str]:
    """Return the union of `requires` for any rule that any touched file hits.

    Matching is path-prefix: a rule with `files=["src/gtk/"]` matches
    `src/gtk/foo.c` but not `src/cli.py`. A rule with `files=["src/gtk"]`
    (no slash) catches both `src/gtk/foo.c` and `src/gtk_helpers.c` — use
    the trailing slash form to be strict.

    Returns capabilities in deterministic order (first-seen across rules).
    """
    seen: dict[str, None] = {}
    for path in touched_files:
        for rule in rules:
            if not any(path.startswith(pattern) for pattern in rule.files):
                continue
            for cap in rule.requires:
                seen.setdefault(cap, None)
    return list(seen.keys())


# ── Machine selection ───────────────────────────────────────────────────────


@dataclass
class SmokeMachineChoice:
    machine: Machine
    is_worker: bool
    rationale: str


def _capability_matched_machines(
    required_caps: list[str], repo_name: str, config: Config,
) -> list[Machine]:
    """Every machine that can build *repo_name* and declares every one of
    *required_caps* — capability/repo matching only, no pause/quiet-hours or
    idle/busy filtering.

    Factored out of :func:`rank_smoke_machines` so a caller that sees an
    empty ranking can tell "nothing is capable" apart from "something is
    capable but every candidate is paused or in quiet hours" (#2636) without
    re-deriving the capability filter a second time.
    """
    return [
        m for m in config.machines
        if m.can_work_on(repo_name)
        and all(cap in m.capabilities for cap in required_caps)
    ]


def rank_smoke_machines(
    required_caps: list[str],
    repo_name: str,
    worker_machine_name: str,
    board: Board,
    config: Config,
    *,
    prefer_worker: bool = True,
    now: "datetime | None" = None,
) -> list[SmokeMachineChoice]:
    """Every machine that can smoke-test `repo_name` with all `required_caps`,
    best candidate first (#1672).

    Preference order (#1402):
    1. The worker's own machine, if capable and idle
    2. Idle, capable, different from worker (config order)
    3. The worker's own machine, if capable (busy — smoke will queue)
    4. Busy, capable, different from worker (config order; smoke will queue)

    Every candidate appears exactly once; the head of the list is exactly what
    :func:`pick_smoke_machine` used to return on its own.

    **Why a ranking and not a single pick (#1672).** ``dispatch_smoke`` has to
    reject a candidate *after* choosing it — its live ``/health`` probe can
    contradict the capabilities ``coordinator.yml`` declares for it (#1570 D),
    or it can turn out to have no ``repo_paths`` entry. Returning one machine
    meant a single bad candidate ended the whole Test stage: on 2026-08-01
    (#1678) the router picked the same unhealthy machine every 30 s forever
    while two other machines declared the same capability and were never
    tried. The caller now walks this list.

    **Capability matching is unchanged and is never relaxed.** Only machines
    that genuinely declare every required capability (and can work on the
    repo) are in the list at all — a fallback that dispatched to a machine
    lacking the capability would produce a green verdict from a machine that
    cannot run the suite, which is worse than refusing.

    **Why the worker machine is preferred.** This used to prefer a machine
    *different* from the worker.  That preference is right for **review**,
    where independence from the worker's context is the entire point — but a
    test run needs **capability**, not independence: it re-runs the suite
    against the pushed commit and the verdict is identical wherever it runs.
    Meanwhile the worker's machine is the one with a warm build cache
    (``coord.cargo_cache``, #1402) and, for a Rust repo, that is the
    difference between ~18 s and ~3 min.  Capability rules still bind
    absolutely: a GTK or browser suite goes to a capable machine even when
    the worker ran somewhere else, and a worker machine that lacks a required
    capability is never chosen.

    Pass ``prefer_worker=False`` to restore the different-machine-first
    ordering (used by callers that want independence, and by tests pinning
    the old behaviour).

    Returns an empty list when capabilities can't be matched.

    #2636: candidates are also filtered through ``follow_on_paused_set`` —
    **not** ``paused_set`` — before ranking. A smoke leg is the tail of work
    already in flight (the Test stage that certifies a `done` work row), the
    same shape #2240 established for a review leg, so a release cordon
    ("route no NEW work here") must not filter its host out — that would
    reproduce the 2026-08-14 drain deadlock, just in the Test stage instead
    of Review. An explicit `coord pause` and a `quiet_hours` window both
    still apply in full: the incident this closes was a smoke leg landing on
    elitebook 82 minutes into its declared quiet-hours window, ten minutes
    before the operator suspended it.

    *now* (#2636) is forwarded to ``follow_on_paused_set`` untouched —
    ``None`` (the default, and every production call site) evaluates quiet
    hours against the real clock. The seam exists purely so a test can pin a
    specific wall-clock moment instead of depending on whatever instant the
    suite happens to run at.
    """
    candidates = _capability_matched_machines(required_caps, repo_name, config)
    if not candidates:
        return []

    from coord.machine_pause import follow_on_paused_set  # noqa: PLC0415

    paused = follow_on_paused_set(config.machines, now=now)
    candidates = [m for m in candidates if m.name not in paused]
    if not candidates:
        return []

    busy = {a.machine_name for a in board.active if a.status in ("pending", "running")}

    same = next((m for m in candidates if m.name == worker_machine_name), None)

    ranked: list[SmokeMachineChoice] = []
    seen: set[str] = set()

    def _add(choice: SmokeMachineChoice) -> None:
        if choice.machine.name in seen:
            return
        seen.add(choice.machine.name)
        ranked.append(choice)

    if prefer_worker and same is not None and same.name not in busy:
        _add(SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"chose {same.name} — the worker machine, idle and has "
                f"{required_caps}; its build cache is already warm"
            ),
        ))

    for m in candidates:
        if m.name == worker_machine_name or m.name in busy:
            continue
        _add(SmokeMachineChoice(
            machine=m,
            is_worker=False,
            rationale=(
                f"chose {m.name} — idle and has {required_caps} "
                f"(worker was {worker_machine_name})"
            ),
        ))

    if prefer_worker and same is not None:
        _add(SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"chose {same.name} — the worker machine has {required_caps} and a "
                "warm build cache; capable but busy, smoke will queue"
            ),
        ))

    for m in candidates:
        if m.name == worker_machine_name:
            continue
        _add(SmokeMachineChoice(
            machine=m,
            is_worker=False,
            rationale=(
                f"chose {m.name} — capable but busy; smoke will queue"
            ),
        ))

    if same is not None:
        _add(SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"only the worker machine ({worker_machine_name}) has {required_caps}; "
                "smoke runs on the same machine"
            ),
        ))
    return ranked


def pick_smoke_machine(
    required_caps: list[str],
    repo_name: str,
    worker_machine_name: str,
    board: Board,
    config: Config,
    *,
    prefer_worker: bool = True,
) -> SmokeMachineChoice | None:
    """The single best machine with all `required_caps` for `repo_name`.

    The head of :func:`rank_smoke_machines` — see there for the preference
    order and the reasoning. Returns None when capabilities can't be matched.

    ``dispatch_smoke`` uses the full ranking (#1672); this stays for callers
    that only ever want the first choice.
    """
    ranked = rank_smoke_machines(
        required_caps, repo_name, worker_machine_name, board, config,
        prefer_worker=prefer_worker,
    )
    return ranked[0] if ranked else None


def _capability_probe_reasons(
    machine: Machine,
    required_caps: list[str],
    *,
    http_client: httpx.Client | None = None,
    timeout: float = 5.0,
) -> dict[str, list[str]]:
    """Cross-reference `machine`'s live `/health` tool probes (#1570 B)
    against `required_caps` before routing smoke work to it (#1570 D).

    `pick_smoke_machine` only checks `machine.capabilities` — a hand-written
    claim in `coordinator.yml` that nothing has ever verified (#1570's whole
    point: `gh` was simply the first claim to bite). This asks the machine
    itself.

    Returns `{capability: [reason, ...]}` for any required capability whose
    backing tool the machine's own probe says is missing or too old — empty
    when everything checks out *or* when `/health` doesn't publish
    `tool_versions` yet (an agent that predates #1570 B). The latter fails
    OPEN, not closed: during rollout most of the fleet won't have the probe
    immediately, and refusing every smoke dispatch on missing telemetry
    would be strictly worse than the blind trust this replaces. Only an
    *explicit* probe failure refuses routing.

    #2913: this used to fail open the same way for a config-free agent too
    — `tool_versions` was present (baseline `git`/`gh`) but never covered
    `required_caps`, since a config-free agent has no `capabilities` of its
    own to probe against and `AgentServer._cached_tool_versions` used to
    restrict probing to exactly that empty list. `unmet_capabilities` then
    found nothing to compare and returned `{}`, indistinguishable from
    "probed and clean". Fixed at the source: a config-free agent now probes
    every known capability (`coord.prereqs.ALL_CAPABILITY_NAMES`) regardless
    of what it declares, so `tool_versions` here genuinely covers
    `required_caps` and this function's refusal path works the same for a
    config-free agent as for a fully-configured one.

    Never raises — a connectivity hiccup here just skips the extra check;
    the POST to `/assign` right after this call in `dispatch_smoke` is the
    real reachability test and fails closed on its own if the machine is
    down.
    """
    client = http_client or httpx
    try:
        resp = client.get(f"http://{machine.host}:{AGENT_PORT}/health", timeout=timeout)
        resp.raise_for_status()
        health = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, ValueError):
        # Any connectivity/parsing hiccup here just skips the extra check —
        # not widened to AttributeError: a caller's `http_client` double
        # should implement `.get` (real httpx.Client always does), so a
        # missing-method bug on our side surfaces instead of silently
        # degrading like a genuine reachability problem.
        return {}
    raw_probes = health.get("tool_versions") if isinstance(health, dict) else None
    if not raw_probes:
        return {}

    from coord.prereqs import ToolProbe, unmet_capabilities

    probes = {
        tool: ToolProbe(
            tool=tool,
            capability=info.get("capability"),
            found=bool(info.get("found", False)),
            version=info.get("version"),
            min_version=info.get("min_version"),
            meets_floor=info.get("meets_floor"),
            what_breaks="",
        )
        for tool, info in raw_probes.items()
        if isinstance(info, dict)
    }
    return unmet_capabilities(required_caps, probes)


# ── CI-equivalence of the Test-stage command (#2091) ────────────────────────


@dataclass(frozen=True)
class SmokeCommand:
    """Which command the Test stage will run, and how faithful it is to CI.

    #2091: the Test verdict is what gates review and merge, so what it
    *means* depends entirely on which suite produced it.  coord-portal #14
    recorded ``test_passed`` in 1m51s on a commit whose CI failed
    deterministically after 44m57s — the gate simply never ran the suite
    (``npm run test:e2e``) that catches it.  Resolution is therefore no
    longer an anonymous ``a or b``: the chosen command carries its
    provenance so callers can say out loud whether a green verdict is
    CI-equivalent or merely "some tests passed".

    ``ci_equivalent`` is a *declaration*, not a proof — it is True exactly
    when the operator pointed ``repos[].ci_command`` at what CI runs.  It
    can still be a lie if the config drifts from the workflow file; what it
    buys is that the un-declared case stops looking identical to the
    declared one.

    ``from_rule`` (#3056) is True when this command came from a matched
    ``smoke_tests.capability_rules[].command`` rather than a repo/fleet-wide
    source. A rule command is never ``ci_equivalent`` — it was hand-written
    for one path prefix, not declared as "what CI runs" — but it can be
    *broader* than CI's own leg for that path (e.g. a real device/VM run
    where CI only compiles) while still being *narrower* than the whole CI
    workflow. ``briefing_note`` says this explicitly so a transcript reader
    can tell a rule-command green from a repo-command green.
    """

    command: str | None
    source: str
    ci_equivalent: bool
    from_rule: bool = False

    def briefing_note(self) -> str:
        """One line for the smoke agent's briefing naming the command's origin."""
        if self.from_rule:
            return (
                f"- Suite: `{self.source}` — this comes from a matched "
                "`smoke_tests.capability_rules` entry (#3056), which "
                "outranks `ci_command`/`default_command`/`test_command` for "
                "this diff because it was written for exactly these files. "
                "It is not declared CI-equivalent: it may be BROADER than "
                "CI's own leg for these files, while still being NARROWER "
                "than the whole CI workflow. Passing here does not by "
                "itself prove CI is green."
            )
        if self.ci_equivalent:
            return (
                f"- Suite: `{self.source}` — this is the repo's declared "
                "CI-equivalent command, so this verdict carries the same "
                "weight as a CI run."
            )
        return (
            f"- Suite: `{self.source}` — this repo has not declared a "
            "`ci_command`, so this run may be NARROWER than CI (#2091). "
            "Passing here does not prove CI is green."
        )


def resolve_rule_command(
    touched_files: list[str], rules: list[SmokeRule]
) -> SmokeCommand | None:
    """Return the `SmokeCommand` for the first `capability_rules` entry that
    matches *touched_files* and declares a `command` (#3056), or `None` if
    no matching rule declares one.

    **Multiple matching rules.** A diff can touch files matching more than
    one rule (e.g. a diff touching both a `gtk/` rule and a `win/` rule that
    each declare a `command`) — two commands cannot union, so this resolves
    the ambiguity by **first match wins, in `capability_rules` declaration
    order**. That order is the config author's own explicit list order, not
    dict/set iteration order, so it is stable across runs and processes.
    Rules with no `command` are skipped over (they never candidate here) but
    still participate in `match_rules`' capability routing as before.

    A rule's `SmokeCommand.ci_equivalent` is always False — see
    `SmokeCommand`'s docstring for why a rule command is neither assumed
    CI-equivalent nor assumed narrower; it's simply not the same claim.
    """
    for i, rule in enumerate(rules):
        if not rule.command:
            continue
        if any(
            path.startswith(pattern)
            for path in touched_files
            for pattern in rule.files
        ):
            return SmokeCommand(
                rule.command,
                f"smoke_tests.capability_rules[{i}] (files={rule.files!r})",
                False,
                from_rule=True,
            )
    return None


def resolve_smoke_command(
    repo, smoke_cfg: SmokeTestsConfig, touched_files: list[str] | None = None,
) -> SmokeCommand:
    """Pick the Test-stage command for *repo*, best (most CI-faithful) first.

    Precedence, most-specific first:

    0. A matched `smoke_tests.capability_rules[].command` (#3056) — strictly
       more specific than any repo/fleet-wide command, since it was written
       for exactly the files the diff touches. Only consulted when
       *touched_files* is given (see `resolve_rule_command` for the
       multiple-matching-rules tiebreak); callers with no diff in hand
       (fleet config-health checks, repo onboarding) fall straight through
       to the pre-#3056 precedence below, unchanged.
    1. ``repos[].ci_command`` — what this repo's CI actually runs (#2091).
    2. ``smoke_tests.default_command`` — the fleet-wide fallback.
    3. ``repos[].test_command`` — the local/quick suite.

    ``smoke_tests.default_command`` outranking ``test_command`` is the
    pre-existing #1021 behaviour and is preserved; ``ci_command`` is inserted
    *above* both because a global default cannot possibly be more faithful to
    one repo's CI than that repo's own declaration. The #3056 rule command is
    inserted *above ci_command* because it is strictly narrower still — a
    rule that names exact files beats a repo-wide declaration every time it
    matches.
    """
    if touched_files is not None:
        rule_command = resolve_rule_command(touched_files, smoke_cfg.capability_rules)
        if rule_command is not None:
            return rule_command
    ci_command = (getattr(repo, "ci_command", None) or "").strip() or None
    if ci_command:
        return SmokeCommand(ci_command, f"repos[{repo.name}].ci_command", True)
    if smoke_cfg.default_command:
        return SmokeCommand(
            smoke_cfg.default_command, "smoke_tests.default_command", False
        )
    test_command = getattr(repo, "test_command", None)
    if test_command:
        return SmokeCommand(test_command, f"repos[{repo.name}].test_command", False)
    return SmokeCommand(None, "unconfigured", False)


# ── Briefing ────────────────────────────────────────────────────────────────


def build_smoke_briefing(
    *,
    repo_github: str,
    repo_name: str,
    branch: str,
    issue_number: int,
    issue_title: str,
    smoke_command: str,
    required_caps: list[str],
    timeout_seconds: int,
    is_worker: bool,
    command_source: SmokeCommand | None = None,
    parent_assignment_id: str | None = None,
) -> str:
    """Build the smoke worker's briefing.

    *parent_assignment_id* (#2244) is the WORK assignment this Test stage
    certifies. When given, the briefing tells the worker to record its own
    verdict with ``coord test --passed|--fail <parent>`` — the authoritative
    board write, now reachable from a headless worker since #2217 — with the
    printed ``SMOKE:`` marker as the parseable backup. Omitted (``None``) the
    briefing asks for the marker alone, which is what every caller predating
    this got.
    """
    lines: list[str] = []
    lines.append(f"# Smoke test: {repo_github} branch `{branch}`")
    lines.append("")
    lines.append(
        f"Validate the worker's fix for issue #{issue_number}: {issue_title}"
    )
    lines.append("")
    lines.append("## Context")
    lines.append(f"- Repo: {repo_github} (local name: {repo_name})")
    lines.append(f"- Branch: {branch}")
    if command_source is not None:
        # #2091: name the suite's provenance so a reader of the transcript can
        # tell a CI-equivalent green from a narrower one without going back to
        # coordinator.yml.
        lines.append(command_source.briefing_note())
    if required_caps:
        lines.append(f"- Required capabilities: {', '.join(required_caps)}")
    if is_worker:
        lines.append(
            "- NOTE: only this machine has the required capabilities, so the "
            "smoke test is running on the same machine that built the change. "
            "Test the *built artifact*, not the source — the build step here is "
            "your verification that the change compiles."
        )
    lines.append(f"- Timeout: {timeout_seconds}s")
    lines.append("")
    lines.append("## What to do")
    lines.append("")
    lines.append(
        "Run these in your **worktree** (your current directory). Do NOT "
        "`cd` into the machine's shared base checkout and do NOT "
        "`git checkout` there — leaving it parked on a feature branch breaks "
        "every later dispatch against that branch on this machine (#1694)."
    )
    lines.append("")
    lines.append("```bash")
    lines.append("git fetch origin")
    lines.append(f"git checkout {branch}")
    lines.append("git pull --ff-only origin " + branch)
    lines.append(smoke_command)
    lines.append("```")
    lines.append("")
    # #2272: the ceiling is the deterministic cause of a mute Test stage. A
    # suite this size routinely runs past 600s, the harness backgrounds it,
    # and an agent that narrates "I kicked it off" and stops prints no marker
    # — so the stage records NO verdict and re-dispatches, forever, at ~$0.12
    # a lap. Say it in the briefing as well as the system prompt: the briefing
    # is what the worker re-reads while it is deciding what to do next.
    lines.append("## If the suite runs past the 600s Bash ceiling (#2272)")
    lines.append("")
    lines.append(
        "Claude Code moves any Bash call past ~600s to the **background** and "
        "hands you a task id instead of an exit status. That is not a result. "
        "**Do not end your turn there** — poll the backgrounded task to "
        "completion from this same session (`BashOutput`/`TaskOutput` with "
        "the id, in bounded steps) and read its real exit status before "
        "reporting. A foreground `until ! pgrep ...; do sleep ...; done` wait "
        "hits the identical wall, so don't."
    )
    lines.append("")
    lines.append(
        "A turn that ends with the suite still running prints no marker, the "
        "Test stage records NO verdict, and it re-dispatches — and since the "
        "ceiling is deterministic, every re-dispatch stalls identically. If "
        "you truly cannot get an exit status, print `SMOKE: fail <the smoke "
        "command never returned an exit status within this session>` rather "
        "than ending mute."
    )
    lines.append("")
    lines.append(
        "## Reporting the verdict (#2244)"
    )
    lines.append("")
    lines.append(
        "Print ONE of these as a final line, at the start of the line — this "
        "line IS the verdict the coordinator reads:"
    )
    lines.append("")
    lines.append("- `SMOKE: pass` — the smoke command exited 0")
    lines.append("- `SMOKE: fail <one-line reason>` — it failed")
    lines.append(
        "- `SMOKE: baseline-red <one-line reason>` — every failure reproduces "
        "identically on the merge-base (#2170)"
    )
    lines.append("")
    lines.append(
        "Your process exit code is NOT a verdict: an `exit 1` inside a tool "
        "call ends that tool call, not this session, so the coordinator sees "
        "exit 0 whatever the suite did. A run with no marker line is recorded "
        "as NO verdict (never a pass) and the Test stage gets re-run."
    )
    if parent_assignment_id:
        lines.append("")
        lines.append(
            "Then write the verdict to the board yourself — this is the "
            "authoritative record, the printed marker is the backup:"
        )
        lines.append("")
        lines.append("```bash")
        lines.append(
            f"coord test --passed {parent_assignment_id}"
            "                      # if the suite passed"
        )
        lines.append(
            f'coord test --fail --reason "<one-line reason>" '
            f"{parent_assignment_id}   # if it failed"
        )
        lines.append("```")
        lines.append("")
        lines.append(
            "That id is the WORK assignment being tested, not your own. Skip "
            "this command entirely for a `baseline-red` verdict (it has no "
            "flag — the marker line is the whole signal), and if `coord` "
            "isn't on your PATH or errors, just make sure the marker line is "
            "printed."
        )
    return "\n".join(lines)


# ── Diff lookup (which files did the worker change?) ────────────────────────


def _fetch_touched_files(repo_github: str, branch: str) -> list[str]:
    """Return the list of files changed on `branch` vs the base branch.

    Uses `gh pr view --json files` so the lookup works without a local
    checkout on the coordinator. Returns an empty list on lookup failure —
    the caller treats that as "no rules matched" and skips smoke.
    """
    pr = None
    try:
        pr = github_ops.find_pr_for_branch(repo_github, branch)
    except RuntimeError:
        pr = None
    if pr is None:
        return []
    try:
        raw = github_ops._gh(
            "pr", "view", str(pr["number"]),
            "--repo", repo_github,
            "--json", "files",
        )
    except RuntimeError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    files = data.get("files", []) or []
    return [f.get("path", "") for f in files if f.get("path")]


# ── Unroutable reporting (#1672) ────────────────────────────────────────────


#: ``test_state`` recorded on the parent work row when no capability-matched
#: machine can run the Test stage and the condition will NOT clear on its own
#: (#1672). Deliberately distinct from ``"failed"``: nothing is wrong with the
#: branch, so this must never trigger a fix round — it is a fleet/config fault,
#: and every gate that asks for ``"passed"``/``"skipped"`` keeps the merge shut
#: exactly as it did while the state was NULL.
TEST_STATE_BLOCKED = "blocked"


# ── Mute Test-stage legs: the retry budget (#2244/#2272) ────────────────────
#
# A Test-stage leg that finishes without printing a `SMOKE:` marker records NO
# verdict — never a pass (#2244). That is the right call, and it assumes
# re-running is USEFUL. #2272 is the case where it is not: on 2026-08-15 five
# consecutive legs against one work row each ran the suite past Claude Code's
# 600s Bash ceiling, got backgrounded, and ended mute. Deterministic cause,
# identical every lap, ~$0.12 and ~10 minutes each, and the board showed a
# healthy `running` Test stage throughout. It was stopped by hand.
#
# #2244 already intended a bound — "a SECOND mute run parks the row" — but it
# stored the evidence in the parent's `test_reason` and `dispatch_smoke`'s own
# `running` stamp overwrote that field on the very next dispatch. The counter
# was reset by the retry it was supposed to be counting, so the bound never
# fired. Hence two things here rather than one:
#
#   * a TALLY that is explicit and parseable (`no-verdict (#2244) x2`) rather
#     than a bare marker whose presence is the whole signal, and
#   * `dispatch_smoke` carrying that tally ACROSS its `running` stamp, so the
#     count survives the thing that kept destroying it.
#
# The generalisation matters more than the ceiling: whatever future cause
# leaves a Test stage mute — a dead harness, a machine that reboots mid-run, a
# prompt regression — it now costs a bounded number of legs and then names
# itself on the row, instead of billing forever. A cause the system cannot
# diagnose must still be a cause the system stops paying for.

#: The marker left in the parent work row's ``test_reason`` when a headless
#: Test stage produced no parseable verdict. Suffixed with `` xN`` from the
#: second leg on (see :func:`mute_smoke_tally`).
NO_SMOKE_VERDICT_MARKER = "no-verdict (#2244)"

#: How many CONSECUTIVE mute Test-stage legs one work row may burn before the
#: row is parked terminally instead of re-dispatched (#2272). Two — one lap of
#: genuine self-heal (the #1605 environmental-death shape really does clear on
#: a retry), then stop. Pinned by test: raising it raises the unattended spend
#: ceiling for every no-verdict cause at once, which is exactly the decision
#: that should be deliberate.
MUTE_SMOKE_LEG_BUDGET = 2

#: Matches the marker with or without its `` xN`` tally, so a reason written by
#: a pre-#2272 coordinator (bare marker, no count) still reads back as one leg
#: rather than zero — an upgrade mid-loop must not hand the row a fresh budget.
_MUTE_TALLY_RE = re.compile(
    re.escape(NO_SMOKE_VERDICT_MARKER) + r"(?:\s*x\s*(\d+))?", re.IGNORECASE
)


def mute_smoke_legs(test_reason: str | None) -> int:
    """How many mute Test-stage legs *test_reason* records (0 if none).

    Reads the tally back out of the parent work row's ``test_reason`` — the
    one field that survives from one Test-stage leg to the next. Tolerates:

    * ``None``/empty — no legs (the ordinary first dispatch),
    * the bare pre-#2272 marker with no count — one leg,
    * a truncated ``test_reason`` — the ``/board`` wire carries only a bounded
      preview (:func:`coord.state.load_assignment_test_reason`), so the tally
      is written at the FRONT of the reason where a preview still shows it.
    """
    if not test_reason:
        return 0
    match = _MUTE_TALLY_RE.search(test_reason)
    if match is None:
        return 0
    raw = match.group(1)
    if raw is None:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:  # pragma: no cover — regex only matches digits
        return 1


def mute_smoke_tally(count: int) -> str:
    """The marker for *count* mute legs — ``"no-verdict (#2244) x2"``.

    A single leg renders as the bare marker, so an operator reading a row
    mid-loop sees a count only once there is a count worth seeing.
    """
    if count <= 1:
        return NO_SMOKE_VERDICT_MARKER
    return f"{NO_SMOKE_VERDICT_MARKER} x{count}"


#: Soft (transient) unroutable reports already logged this process, keyed by
#: ``(assignment_id, message)``. Transient conditions — a machine that is
#: merely unreachable right now — are left re-dispatchable so the stage
#: self-heals on a later tick; this memo is what stops the retry from also
#: re-logging every 30 s (#1672). Bounded so a long-lived daemon can't grow it
#: without limit.
_SOFT_REPORTS_SEEN: set[tuple[str, str]] = set()
_SOFT_REPORTS_MAX = 512


@dataclass
class SmokeAttempt:
    """One machine `dispatch_smoke` tried and could not use (#1672)."""

    machine_name: str
    reason: str
    #: True when the reason is expected to clear without operator action (a
    #: connectivity blip). False for durable faults — an explicit `/health`
    #: probe contradiction (#1570 D) or a missing `repo_paths` entry — which
    #: stay broken until somebody fixes the machine or the config.
    transient: bool = False

    def describe(self) -> str:
        return f"{self.machine_name}: {self.reason}"


def _report_unroutable_smoke(
    completed: Assignment,
    required_caps: list[str],
    attempts: list[SmokeAttempt],
    *,
    paused_capable: list[str] | None = None,
) -> None:
    """Report — once — that the Test stage has no machine it can run on.

    #1672/#1678: the old code logged a WARNING and returned. Nothing was
    written anywhere the TUI, `coord gates` or the board could show it, and
    the daemon re-ran the identical refusal every 30 s forever. The Test stage
    simply never started and the only trace was `journalctl` on the daemon
    host — the #1616 failure shape again: the pipeline stops and the product
    says nothing.

    Two outcomes, split on whether the condition can clear by itself:

    * **Durable** (every candidate hard-refused, there were no candidates at
      all, or every capability-matched candidate was paused/quiet-covered —
      see *paused_capable*) — record ``test_state=TEST_STATE_BLOCKED`` with
      the full reason on the parent work row. That is board state: `coord
      gates` prints it, the TUI reads it off the row, and
      `record_test_verdict` writes an ``test_blocked`` audit row. It also
      ends the spin, because `dispatch_pending_smoke` skips rows that
      already carry a verdict — the escalation happens once, not every tick.
    * **Transient** (at least one candidate failed only on connectivity) — do
      NOT poison the row. A machine that is rebooting comes back, and marking
      the row blocked would demand a manual `coord diagnose --reset` for what
      the next tick would have fixed for free. Log it once per process
      instead, and leave the row re-dispatchable.

    *paused_capable* (#2636): machine names that matched capability but were
    filtered out of `rank_smoke_machines`'s ranking by `follow_on_paused_set`
    — an explicit `coord pause` or a `quiet_hours` window — before
    `dispatch_smoke` ever got to try them, so `attempts` is empty for a
    reason that has nothing to do with capability. Naming that here keeps the
    recorded reason from reading as "no capable machine" while capable
    machines are sitting right there, merely unavailable right now — #1678's
    failure mode by a different route. Always durable: cleared the same way
    (`coord diagnose --stage test --reset`) once the machine is unpaused or
    its quiet-hours window ends, same as any other exhausted candidate list.

    Never raises: a board-write failure must not take the caller down.
    """
    transient = any(a.transient for a in attempts)
    caps = ", ".join(required_caps) if required_caps else "(none — any capable machine)"
    if paused_capable:
        message = (
            f"Test stage cannot be routed: every machine that declares "
            f"capability [{caps}] for repo {completed.repo_name!r} is "
            f"paused or inside its quiet-hours window right now: "
            f"{', '.join(paused_capable)}. (#2636)"
        )
    elif attempts:
        message = (
            f"Test stage cannot be routed: every machine that declares "
            f"capability [{caps}] for repo {completed.repo_name!r} refused. "
            f"Tried {len(attempts)} — "
            + "; ".join(a.describe() for a in attempts)
            + "."
        )
    else:
        message = (
            f"Test stage cannot be routed: no configured machine declares "
            f"capability [{caps}] AND can build repo "
            f"{completed.repo_name!r} — the Test stage cannot run for this "
            f"completion until a capable machine is added."
        )

    def _log_once(level: int, text: str) -> None:
        """Log `text` at most once per (row, message) for this process.

        The board row is what normally makes the durable report fire once; a
        transient dead end deliberately does NOT write to the row (it must
        stay re-dispatchable), and a row with no assignment_id has nowhere to
        write at all — so both need this memo instead. Either way the daemon
        journal gets ONE line, never one every 30 s.
        """
        key = (completed.assignment_id or "", text)
        if key in _SOFT_REPORTS_SEEN:
            return
        if len(_SOFT_REPORTS_SEEN) >= _SOFT_REPORTS_MAX:
            _SOFT_REPORTS_SEEN.clear()
        _SOFT_REPORTS_SEEN.add(key)
        logger.log(
            level, "dispatch_smoke: %s#%s — %s",
            completed.repo_name, completed.issue_number, text,
        )

    if transient:
        _log_once(
            logging.WARNING,
            f"{message} Leaving the row re-dispatchable; a later tick "
            "retries. (#1672)",
        )
        return

    reason = (
        f"{message} Fix the machine (or add a capable one) and clear this "
        f"with `coord diagnose {completed.repo_name} "
        f"{completed.issue_number} --stage test --reset` to re-dispatch. "
        "(#1672)"
    )
    if completed.test_state == TEST_STATE_BLOCKED:
        return  # already recorded on the row — the report has been made
    if completed.assignment_id is None:
        # No row to write to (shouldn't happen for a board completion) — the
        # log is the only surface left, so at least don't repeat it forever.
        _log_once(logging.ERROR, reason)
        return
    logger.error(
        "dispatch_smoke: %s#%s — %s", completed.repo_name,
        completed.issue_number, reason,
    )
    try:
        from coord.state import record_test_verdict

        record_test_verdict(
            assignment_id=completed.assignment_id,
            test_state=TEST_STATE_BLOCKED,
            test_reason=reason,
        )
    except Exception:  # noqa: BLE001 — reporting must never break dispatch
        logger.exception(
            "dispatch_smoke: failed to record the blocked Test verdict for %s",
            completed.assignment_id,
        )
        return
    completed.test_state = TEST_STATE_BLOCKED
    completed.test_reason = reason


# ── Dispatch ────────────────────────────────────────────────────────────────


PRLookup = Callable[..., dict | None]
DiffLookup = Callable[[str, str], list[str]]


def dispatch_smoke(
    completed: Assignment,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    diff_lookup: DiffLookup = _fetch_touched_files,
    now: float | None = None,
) -> Assignment | None:
    """Queue a smoke test for a completed work-like assignment (#930: also
    ``type="mock-author"`` — see :data:`coord.models.WORK_LIKE_TYPES`).

    Returns the new smoke `Assignment`, or None when no smoke is needed
    (no rules matched, no capable machine, smoke disabled, etc.). The
    caller is responsible for persisting the board.

    #1672: routing walks the FULL capability-matched candidate list (see
    :func:`rank_smoke_machines`) instead of standing or falling on one
    machine, and a dead end is reported on the row rather than re-logged on
    every daemon tick — see :func:`_report_unroutable_smoke`.
    """
    smoke_cfg = getattr(config, "smoke_tests", SmokeTestsConfig())
    if not smoke_cfg.auto_queue:
        return None
    if completed.type not in WORK_LIKE_TYPES:
        return None
    if completed.status != "done":
        return None
    if not completed.branch:
        return None
    if completed.test_state == TEST_STATE_BLOCKED:
        # #1672: already reported as unroutable, with the reason on the row.
        # Re-probing the same broken fleet on every tick is exactly the spin
        # this issue is about — an operator clears it (`coord diagnose
        # --stage test --reset`) once the fleet is fixed. `dispatch_pending_
        # smoke` already skips rows with a verdict; this covers the callers
        # that hand us a row directly (reconcile).
        return None

    # Dedupe: don't fire a second smoke if one's already in flight.
    from coord.claim import (
        has_active_branch_followup,
        has_active_followup,
        superseding_work_row,
    )

    if has_active_followup(
        board, of_assignment_id=completed.assignment_id, assignment_type="smoke"
    ):
        return None

    # #1819: ...and don't fire one if another row on the SAME BRANCH already
    # has one in flight. The suite measures the branch, not the row that
    # pushed it; after a fix round (`--fix-of` reuses the branch by design)
    # one branch carries two `work` rows, and the row-keyed dedupe above
    # waved the sibling straight through — two machines ran the identical
    # suite on the identical branch and raced to write the verdict (#1797).
    if has_active_branch_followup(
        board,
        repo_name=completed.repo_name,
        branch=completed.branch,
        assignment_type="smoke",
    ):
        return None

    # #1819: a row that a LATER work-like row superseded on the same branch is
    # not a dispatch target at all. It did not produce the branch's current
    # content, so testing it burns a machine on a result the later row's own
    # dispatch already computes, and the verdict lands on a row nothing gates
    # on. This is what keeps the round-1 row (review=request-changes, fixed by
    # round 2) from consuming a machine every time the base moves.
    superseded_by = superseding_work_row(board, completed)
    if superseded_by is not None:
        logger.debug(
            "dispatch_smoke: skipping %s#%s row %s — superseded on branch %s "
            "by the later work row %s (#1819).",
            completed.repo_name, completed.issue_number,
            completed.assignment_id, completed.branch,
            getattr(superseded_by, "assignment_id", None),
        )
        return None

    repo = config.repo(completed.repo_name)
    if repo is None:
        return None

    touched = diff_lookup(repo.github, completed.branch)
    required_caps = match_rules(touched, smoke_cfg.capability_rules)
    # #2091: resolve *with* provenance — the Test verdict this dispatch will
    # produce is only as meaningful as the suite behind it. #3056: pass the
    # touched files so a matching rule's own `command` (routing AND the
    # command that runs, not just routing) outranks the repo-wide sources.
    resolved = resolve_smoke_command(repo, smoke_cfg, touched_files=touched)
    smoke_command = resolved.command

    if not required_caps:
        # #1426: a capability-rule miss used to mean "skip silently" — the
        # exact blocker that kept the Test stage from ever dispatching for
        # any repo/diff not explicitly covered by a `capability_rules` entry
        # (historically only `tui/` and `coord/dashboard/webapp/` were
        # routed; everything else — most `coord/**` Python work included —
        # never got a headless Test-stage dispatch at all).
        #
        # It now means "no EXTRA hardware capability required", not "nothing
        # to test": a `type="work"` completion still dispatches, to any
        # machine that can build/test the repo at all, as long as a real
        # command is configured (`smoke_tests.default_command` or the repo's
        # `test_command`) — see `pick_smoke_machine`, which treats an empty
        # `required_caps` as "any capable-for-repo machine".
        #
        # `mock-author`/`test-author` (#930/#1176) keep the OLD skip-on-miss
        # behavior: #1076/#1152 established that a rule miss for THOSE types
        # means "genuinely nothing to smoke-test" (a Gate-A contract/fixture-
        # only diff), and `dispatch_pending_reviews` back-fills
        # `test_state="skipped"` for them — dispatching a real suite run
        # here would duplicate that and burn a full test run on a diff that
        # never touches source.
        if completed.type != "work" or smoke_command is None:
            return None

    if smoke_command is None:
        logger.warning(
            "dispatch_smoke: %s#%s needs capabilities %s but no smoke "
            "command is configured (repos[].ci_command, "
            "smoke_tests.default_command, or this repo's test_command) — "
            "skipping. Configure one so the Test stage stops silently "
            "no-oping for this repo.",
            completed.repo_name, completed.issue_number, required_caps,
        )
        return None

    if not resolved.ci_equivalent and repo.github:
        # #2091: the repo HAS CI (it has a `github:` slug, so a PR gets
        # checks) but has not declared what CI runs, so the verdict this
        # dispatch produces is "some tests passed", not "the branch is
        # good".  That is exactly the coord-portal #14 shape — a 1m51s green
        # Test verdict on a commit whose 44m57s CI run was red — so say it
        # once per dispatch rather than letting the gap stay invisible.
        logger.warning(
            "dispatch_smoke: %s#%s Test verdict will NOT be CI-equivalent — "
            "running %s (%s) while CI runs whatever %s's workflows say. Set "
            "repos[%s].ci_command to the command CI runs so a green Test "
            "verdict means the branch is good, not just that a subset "
            "passed (#2091).",
            completed.repo_name, completed.issue_number,
            smoke_command, resolved.source, repo.github, repo.name,
        )

    # #1672: the FULL capability-matched candidate list, best first. Picking
    # one machine and giving up on it meant a single bad candidate ended the
    # whole Test stage — #1678, where the router re-chose the same unhealthy
    # machine every 30 s while two other machines declared the same
    # capability and were never tried. Capability matching itself is NOT
    # relaxed: `rank_smoke_machines` only ever yields machines that genuinely
    # declare every required capability.
    candidates = rank_smoke_machines(
        required_caps, completed.repo_name, completed.machine_name, board, config
    )
    # #2636: an empty ranking is ambiguous by itself — capability-empty and
    # every-candidate-paused/quiet both come back as `[]`. Re-derive the
    # capability-only set (cheap: config-only, no network) so a downstream
    # unroutable report can name the real cause instead of the generic
    # "no capable machine" message while capable machines sit idle behind a
    # pause or a quiet-hours window.
    paused_capable: list[str] = []
    if not candidates:
        capable = _capability_matched_machines(
            required_caps, completed.repo_name, config
        )
        if capable:
            from coord.machine_pause import follow_on_paused_set  # noqa: PLC0415

            paused = follow_on_paused_set(config.machines)
            paused_capable = sorted(m.name for m in capable if m.name in paused)
    attempts: list[SmokeAttempt] = []
    client = http_client or httpx
    dispatched: tuple[SmokeMachineChoice, str, dict] | None = None

    for choice in candidates:
        if required_caps:
            unmet = _capability_probe_reasons(
                choice.machine, required_caps, http_client=http_client
            )
            if unmet:
                # #1570 D: the machine *claims* every required capability in
                # `coordinator.yml`, but its own `/health` probe (#1570 B)
                # says otherwise — refuse to route HERE rather than dispatch
                # a worker that fails 20 minutes in with a confusing,
                # unrelated error. #1672: that refusal is per-machine, so
                # keep walking the candidate list instead of ending the
                # stage. Durable, not transient — the probe disagrees until
                # somebody installs the tool.
                logger.warning(
                    "dispatch_smoke: skipping machine %s for %s#%s — its own "
                    "/health probe disagrees with its declared capabilities "
                    "%s — %s — refusing to route (#1570 D). Trying the next "
                    "capability-matched machine (#1672); run `coord doctor` "
                    "to check the fleet.",
                    choice.machine.name, completed.repo_name,
                    completed.issue_number, required_caps, unmet,
                )
                attempts.append(SmokeAttempt(
                    machine_name=choice.machine.name,
                    reason=(
                        f"/health probe contradicts its declared capabilities "
                        f"{required_caps} — {unmet} (#1570 D)"
                    ),
                ))
                continue

        repo_path = choice.machine.repo_path(completed.repo_name)
        if repo_path is None:
            logger.warning(
                "dispatch_smoke: skipping machine %s for %s#%s — it has no "
                "repo_paths entry for %r.",
                choice.machine.name, completed.repo_name,
                completed.issue_number, completed.repo_name,
            )
            attempts.append(SmokeAttempt(
                machine_name=choice.machine.name,
                reason=f"no repo_paths entry for {completed.repo_name!r}",
            ))
            continue

        briefing = build_smoke_briefing(
            repo_github=repo.github,
            repo_name=repo.name,
            branch=completed.branch,
            issue_number=completed.issue_number,
            issue_title=completed.issue_title,
            smoke_command=smoke_command,
            required_caps=required_caps,
            timeout_seconds=smoke_cfg.timeout_seconds,
            is_worker=choice.is_worker,
            command_source=resolved,
            parent_assignment_id=completed.assignment_id,
        )

        # #2168: pin the Test stage's model to avoid the agent falling
        # through to the machine's ambient `claude -p` default (Opus).
        # Mirrors the review path (coord/review.py, coord/gate_b.py):
        # deliberately `config.models.default`, NOT `config.models.labels`
        # — the Test stage's job (checkout, run one command, read an exit
        # code) is a property of the repo's test command, never of the
        # issue's tier label, so label routing (#1798) must not leak in
        # here either.
        smoke_model_alias = config.models.default
        smoke_model_wire = config.models.resolve(smoke_model_alias)

        payload = {
            "repo_name": completed.repo_name,
            "repo_path": repo_path,
            "issue_number": completed.issue_number,
            "issue_title": f"[smoke] {completed.issue_title}",
            "briefing": briefing,
            "files_allowed": [],
            "files_forbidden": [],
            "pull_repos": [],
            "type": "smoke",
            "system_prompt": SMOKE_SYSTEM_PROMPT,
            "review_target": completed.branch,
            # #255: smoke checks out the worker's PR branch but the agent still
            # consults `branch` as the integration base.
            "branch": repo.default_branch or "main",
            "model": smoke_model_wire,
        }

        url = f"http://{choice.machine.host}:{AGENT_PORT}/assign"
        try:
            resp = client.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            agent_response = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            # #1672: TRANSIENT — the machine is capable and its probe agreed,
            # it just didn't answer. Try the next candidate, but if none is
            # left the row stays re-dispatchable rather than blocked: a
            # rebooting machine comes back, and poisoning the row would cost
            # an operator a manual reset for something the next tick fixes.
            logger.warning(
                "dispatch_smoke: POST /assign to %s for %s#%s failed (%s) — "
                "trying the next capability-matched machine (#1672).",
                choice.machine.name, completed.repo_name,
                completed.issue_number, exc,
            )
            attempts.append(SmokeAttempt(
                machine_name=choice.machine.name,
                reason=f"POST /assign failed — {exc}",
                transient=True,
            ))
            continue

        dispatched = (choice, briefing, agent_response)
        break

    if dispatched is None:
        # Every capability-matched machine was tried and none could take it
        # (or there were none at all, including "none — they're all paused
        # or in quiet hours right now", #2636). Report it where the board
        # can show it, exactly once — never the silent 30 s spin of #1678.
        _report_unroutable_smoke(
            completed, required_caps, attempts, paused_capable=paused_capable
        )
        return None

    choice, briefing, agent_response = dispatched

    smoke_assignment = Assignment(
        machine_name=choice.machine.name,
        repo_name=completed.repo_name,
        issue_number=completed.issue_number,
        issue_title=f"[smoke] {completed.issue_title}",
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        branch=completed.branch,
        pr_url=completed.pr_url,
        dispatched_at=now if now is not None else time.time(),
        type="smoke",
        review_target=completed.branch,
        review_of_assignment_id=completed.assignment_id,
        model=smoke_model_alias,
    )
    board.active.append(smoke_assignment)

    from coord.state import record_dispatched_assignment
    repo = config.repo(completed.repo_name)
    if repo is not None:
        record_dispatched_assignment(
            assignment=smoke_assignment,
            repo_github=repo.github,
        )

    # #1395/#1426: mark the PARENT work row's Test verdict "running" the
    # moment the smoke assignment is dispatched — the same marker
    # `coord test --running` set for the old local-subprocess path, so the
    # board/TUI reads the Test box Active for the run's duration instead of
    # idle/Pending. Without this, dispatching the Test stage as a real
    # assignment would silently reopen the #1395 gap it was built to close:
    # `test_state` would stay NULL from dispatch until the terminal verdict
    # lands, indistinguishable from "not started yet".
    #
    # #1819: ...but NEVER over a terminal verdict. `running` is read as "no
    # verdict yet" by every gate (#1395), so stamping it on a row that already
    # says `passed`/`skipped` *un-satisfies a gate that was satisfied* — the
    # merge entry drops out of the queue and the whole cycle restarts. That is
    # the self-sustaining loop observed on #1797: verdict lands → merge
    # enqueues → a re-dispatch a minute later clobbers the gate field back to
    # `running` → the merge never fires → repeat. Dispatching a *fresh* run
    # must never, by itself, retract the previous answer; the new verdict
    # replaces the old one when it actually lands.
    #
    # #2272: ...and this stamp must CARRY THE MUTE-LEG TALLY FORWARD. That is
    # the whole bug behind the five-lap loop: `test_reason` is the only field
    # that survives between Test-stage legs, `_record_smoke_verdict` stores the
    # "this leg came back mute" evidence there, and this write used to replace
    # it with a flat literal. So the counter was erased by the very retry it
    # existed to count, `already_mute_once` read False forever, and #2244's
    # "a second mute run parks the row" bound could never fire. Re-reading and
    # re-stating the tally here also keeps the retry VISIBLE on the board:
    # before this, a row on its fifth mute lap rendered identically to one
    # whose Test stage had just started for the first time, which is why five
    # identical laps looked like progress.
    if completed.assignment_id is not None and completed.test_state not in (
        "passed", "skipped", "failed",
    ):
        from coord.state import load_assignment_test_reason, record_test_verdict

        # Belt and braces: the authoritative single-row read, falling back to
        # the board-carried value when it is unavailable (thin client, remote
        # read failure). Both are bounded previews at worst and the tally is
        # written at the front of the reason, so either still carries it.
        prior_legs = max(
            mute_smoke_legs(load_assignment_test_reason(completed.assignment_id)),
            mute_smoke_legs(getattr(completed, "test_reason", None)),
        )
        running_reason = "dispatched: Test stage running (#1426)"
        if prior_legs:
            running_reason = (
                f"{mute_smoke_tally(prior_legs)} — {running_reason}; "
                f"retry {prior_legs + 1} of {MUTE_SMOKE_LEG_BUDGET} after "
                f"{prior_legs} Test-stage leg(s) produced no verdict (#2272)"
            )

        record_test_verdict(
            assignment_id=completed.assignment_id,
            test_state="running",
            test_reason=running_reason,
        )
        completed.test_state = "running"
        completed.test_reason = running_reason

    return smoke_assignment


# ── Bulk dispatch (#1426) ────────────────────────────────────────────────────


def _promote_advisory_row_if_commits_present(
    completed: Assignment, config: Config
) -> bool:
    """#3099: promote one ``status='advisory'`` row to ``'done'`` in place,
    IFF its branch is confirmed to carry commits ahead of the base branch —
    the #1357 false-positive signature.

    Mutates ``completed`` (the in-memory board row) AND persists the same
    two fields to the DB via :func:`coord.state.promote_advisory_with_commits`
    so the promotion survives even a tick that goes on to skip dispatching
    Test for this row for an unrelated reason (no capability-matched
    machine this pass, a dedupe hit, ...) — mirroring how `dispatch_smoke`
    itself already persists `test_state='running'`/`'blocked'` directly
    rather than relying solely on the caller's `if dispatched: write_board`.

    Returns whether the row is (now) eligible to fall through to the
    ordinary `status == 'done'` handling below — ``False`` for a genuinely
    empty branch (``ahead == 0``) or an unconfirmable one (``ahead is
    None``, e.g. a transient `gh api compare` failure or a repo missing
    from `config`), both of which are left at `'advisory'` for a LATER pass
    (or an operator) to resolve rather than guessed at here.
    """
    if not completed.assignment_id:
        return False
    ahead = github_ops.branch_commits_ahead_for_assignment(completed, config)
    if not (isinstance(ahead, int) and not isinstance(ahead, bool) and ahead > 0):
        return False
    from coord.state import promote_advisory_with_commits

    if not promote_advisory_with_commits(completed.assignment_id):
        # Lost a race — some other writer already moved this row off
        # 'advisory' (e.g. an operator's `coord retry`/`coord assign
        # --force` between this scan starting and this UPDATE running).
        # Don't guess at what it became; the normal `status != 'done'`
        # check right after this call handles every outcome correctly.
        return False
    completed.status = "done"
    if completed.review_state == "advisory":
        completed.review_state = None
    logger.info(
        "dispatch_pending_smoke: promoted %s#%s row %s from 'advisory' to "
        "'done' — branch %r carries %d confirmed commit(s) ahead of base "
        "(the #1357 signature, #3099).",
        completed.repo_name, completed.issue_number, completed.assignment_id,
        completed.branch, ahead,
    )
    return True


def dispatch_pending_smoke(
    board: Board,
    config: Config,
    *,
    now: float | None = None,
) -> list[Assignment]:
    """Bulk Test-stage dispatch — the smoke analogue of
    :func:`coord.review.dispatch_pending_reviews`.

    Scans the FULL completed backlog on `board` (not just rows that just
    transitioned this pass) for work-like completions with no test verdict
    yet, and dispatches a smoke assignment for each eligible one via
    :func:`dispatch_smoke` (which itself enforces `auto_queue`, the #459-style
    dedupe via `has_active_followup`, and capability routing).

    This is the single choke point both `reconcile()` (human-invoked `coord
    resume`) and `coord notify` (the unattended 5-minute timer) route bulk
    Test-stage dispatch through — mirroring `dispatch_pending_reviews`
    exactly. Before this, `dispatch_smoke` was only ever called from
    `reconcile()`'s per-item loop over that pass's newly-done rows, so a
    thin-client/timer-only setup with nobody running `coord resume` never
    dispatched the Test stage at all — the gap `drive-issue.sh` had to paper
    over with a local `scripts/coord-test-runner.sh` subprocess (#1395).

    Returns the list of smoke `Assignment`s actually dispatched. The caller
    is responsible for persisting the board.
    """
    smoke_cfg = getattr(config, "smoke_tests", None)
    if smoke_cfg is None or not smoke_cfg.auto_queue:
        return []

    from coord.state import get_issue_test_mode

    dispatched: list[Assignment] = []
    for completed in board.completed:
        if completed.type not in WORK_LIKE_TYPES:
            continue
        if completed.status == "advisory":
            # #3099: an ADVISORY row whose branch demonstrably carries
            # commits is the #1357 false positive — `coord drive
            # --accept-advisory` already treats it as a good `done` that
            # got mis-flagged and falls through to the Test/Review/Merge
            # gates (`coord.drive._decide_advisory`), but the driver never
            # dispatches those stages itself (this function does, for
            # Test) — and this loop used to skip every `advisory` row
            # unconditionally, right below. That left the row stuck
            # forever: the driver prints "proceeding per
            # --accept-advisory" every poll, and nothing downstream ever
            # moves, because `status` never actually became `'done'`.
            #
            # The daemon has no session-scoped `--accept-advisory` flag to
            # consult (this runs unattended, independent of any particular
            # `coord drive` invocation), so it uses the same OBJECTIVE
            # signal `_decide_advisory` itself gates on: a confirmed,
            # positive commit count ahead of the base branch (#2553's
            # `branch_commits_ahead_for_assignment` — a `gh api compare`
            # call, no local checkout needed, unlike `coord.drive`'s own
            # git-based verifier). `ahead in (0, None)` — genuinely empty,
            # or unconfirmable — is left alone on purpose: those are
            # #2416's bounded `coord retry` loop's and #2426's "could not
            # verify, wait" case's territory respectively, not this one's.
            if not _promote_advisory_row_if_commits_present(completed, config):
                continue
        if completed.status != "done":
            continue
        if completed.test_state is not None:
            # Already has a verdict ("passed"/"failed"/"skipped"), or is
            # "running" — someone (an interactive --smoke-of session, or a
            # smoke assignment already in flight) is already handling it.
            #
            # #1672: this is also what makes the unroutable report fire ONCE.
            # `dispatch_smoke` records `test_state="blocked"` when no
            # capability-matched machine can take the stage, so the next tick
            # lands here and skips instead of re-probing a fleet that is
            # still broken and re-logging the identical refusal every 30 s
            # (#1678). Clearing it (`coord diagnose <repo> <issue> --stage
            # test --reset`) puts the row back in this scan.
            continue

        # #685: per-issue test-mode policy gates auto-smoke dispatch.
        #   test-mode:auto  → headless smoke (auto-dispatch here).
        #   test-mode:smoke → skip; the TUI offers the interactive smoke agent.
        #   no label        → no policy set → respect auto_queue (back-compat).
        test_mode = get_issue_test_mode(completed.repo_name, completed.issue_number)
        if test_mode == "smoke":
            # #2024: say so. This skip is CORRECT (the policy asked for a
            # human-attended Test stage) but it was completely silent, and
            # silence here is what dead-ends an unattended `--fix-of` round:
            # review dispatch is held until this row carries a passed/skipped
            # verdict (`pipeline.test_precedes_review`), and under this policy
            # no automatic component will ever produce one. Round 0 gets
            # attended because a human is watching; rounds 1..N complete at 3am
            # and sit there (vimcode#635: 25 min, then 160 min). The DRIVER now
            # recognises the shape from the same label and escalates instead of
            # counting (`coord.dead_end` shape 3) — this line is the daemon-side
            # half of the same statement, for whoever reads the log first.
            #
            # Deliberately NOT widened to "dispatch anyway for fix rounds":
            # that would silently override an explicit per-issue policy. The
            # answer to "nobody will produce a verdict" is to SAY so, not to
            # take the decision away from the operator who set the label.
            #
            # ONCE per row per process, not once per tick — #1678 is the
            # standing lesson that a refusal re-logged every 30 s against a
            # state that cannot change is its own kind of noise (this row can
            # sit here for hours by design). The daemon is long-lived, so the
            # set is the right lifetime; a restart re-states it, which is
            # exactly when an operator wants to hear it again.
            if completed.assignment_id in _TEST_MODE_SKIP_LOGGED:
                continue
            if completed.assignment_id:
                _TEST_MODE_SKIP_LOGGED.add(completed.assignment_id)
            logger.info(
                "dispatch_pending_smoke: skipping %s#%s row %s — issue is "
                "labelled `test-mode:smoke`, so the headless Test stage is off "
                "for it by policy (#685) and NOTHING automatic will record a "
                "verdict. Review dispatch stays held until one exists: `coord "
                "test %s --passed|--skipped`, or run the attended stage with "
                "`coord assign <machine> %s %s --smoke-of %s --interactive` "
                "(#2024).",
                completed.repo_name, completed.issue_number,
                completed.assignment_id, completed.assignment_id,
                completed.repo_name, completed.issue_number,
                completed.assignment_id,
            )
            continue

        smoke = dispatch_smoke(completed, board, config, now=now)
        if smoke is not None:
            dispatched.append(smoke)

    return dispatched
