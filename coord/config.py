"""Parse and validate coordinator.yml."""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from dataclasses import dataclass, field, fields
from datetime import time
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from coord.liveness_auditor import (
    DEFAULT_DEBOUNCE_SECONDS as DEFAULT_LIVENESS_DEBOUNCE_SECONDS,
    DEFAULT_MODEL as DEFAULT_LIVENESS_MODEL,
    DEFAULT_STRIKES as DEFAULT_LIVENESS_STRIKES,
    DEFAULT_TIMEOUT_SECONDS as DEFAULT_LIVENESS_TIMEOUT_SECONDS,
)
from coord.models import Machine, QuietHours, Repo, WorkerPermissionsConfig
from coord.platform_paths import default_coord_dir
from coord.sql import DIALECT_POSTGRES, DIALECT_SQLITE


DEFAULT_CONFIG_PATH = Path("coordinator.yml")

# Canonical config home — works on a machine that has no repo checkout, mirroring
# where ``~/.coord/coord.db`` and ``~/.coord/client.toml`` already live.  This is
# the recommended location; ``./coordinator.yml`` stays a development fallback.
#
# USER_CONFIG_PATH is resolved lazily via __getattr__ below (#2781), not bound
# here at import time -- see that function's docstring.


def __getattr__(name: str) -> Path:
    """PEP 562 lazy fallback for ``USER_CONFIG_PATH`` (#2781).

    Pre-#2781 this was bound eagerly at import time, so ``$COORD_DIR`` set
    *after* this module was first imported -- e.g. by a pytest fixture --
    never reached it, unlike :func:`default_coord_dir` itself which is
    "computed fresh on every call" by design. This only engages when the
    name hasn't been bound directly in this module's namespace, so
    ``monkeypatch.setattr(coord.config, "USER_CONFIG_PATH", ...)`` (used
    throughout the test suite) still takes priority exactly as before:
    Python calls ``__getattr__`` only when normal attribute lookup fails.
    """
    if name == "USER_CONFIG_PATH":
        return default_coord_dir() / "coordinator.yml"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def resolve_config_path() -> Path:
    """Resolve which ``coordinator.yml`` to load when no explicit path is given.

    Search order (first existing file wins):

    1. ``$COORD_CONFIG`` (if set) — explicit override.
    2. ``~/.coord/coordinator.yml`` — the canonical home (no repo checkout needed).
    3. ``./coordinator.yml`` — CWD, for development / the repo checkout.

    When none exist the canonical home path is returned so the "not found" error
    points operators at the recommended location rather than at the CWD.
    """
    env = os.environ.get("COORD_CONFIG")
    if env:
        return Path(env).expanduser()
    user_config_path = sys.modules[__name__].USER_CONFIG_PATH
    for candidate in (user_config_path, DEFAULT_CONFIG_PATH):
        if candidate.exists():
            return candidate
    return user_config_path


def is_canonical_config_path(path: Path) -> bool:
    """Whether *path* is what :func:`resolve_config_path` resolves to *right now*.

    #2208: a coord command run with an explicit ``--config <file>`` pointed
    somewhere other than the fleet's real config — a scratch fixture, a
    CI-only stub someone wrote specifically so ``coord.config.load()`` has
    something to parse — is not a request to redefine the fleet. This gates
    the shared machines/pipeline DB snapshot (``_save_config_snapshot`` in
    ``coord/commands/_common.py``): only a *path* that matches the ordinary
    resolution order ($COORD_CONFIG → ``~/.coord/coordinator.yml`` →
    ``./coordinator.yml``) counts as canonical and is allowed to overwrite
    that shared table. An explicit ``--config`` that happens to name the
    same file the default resolution would have picked still counts as
    canonical — this only catches a genuine *override*.
    """
    candidate = Path(path).expanduser().resolve()
    canonical = resolve_config_path().expanduser().resolve()
    return candidate == canonical


# Safety-by-default: repos without explicit worker_permissions get this deny-list.
#
# #2314: every ``Bash(<verb> <flag> *)`` entry only matches *flag* IMMEDIATELY
# after *verb* — a worker that inserted one more flag first (or swapped a
# combined short flag's letter order) sailed straight through undetected. The
# `git push`/`rm` entries below each pair the original adjacent form with a
# reordering-safe one (an interior ``*`` before the flag, or — for `rm` — the
# `-fr` letter-swap of `-rf`); this is the "audit other Bash(...) deny
# patterns for the same positional weakness" half of #2314, not exhaustive
# (it does not attempt to catch shell chaining/subshells, which is a
# different, much larger problem than argv flag position).
DEFAULT_DENY_COMMANDS: list[str] = [
    "Bash(gh *)",
    "Bash(git push --force *)",
    "Bash(git push -f *)",
    "Bash(git push * --force *)",
    "Bash(git push * -f *)",
    "Bash(git reset --hard *)",
    "Bash(git reset * --hard *)",
    "Bash(git branch -D *)",
    "Bash(git branch * -D *)",
    "Bash(git checkout -- .)",
    "Bash(git clean -f *)",
    "Bash(git clean * -f *)",
    "Bash(rm -rf *)",
    "Bash(rm -fr *)",
    # #2314: a worker ran `pip install --break-system-packages -e .` — an
    # editable install of coord's own source re-links (or, combined with
    # `--user`/`--break-system-packages`, outright shadows) the interpreter
    # `coord` itself is running under, out from under this and every other
    # session on the box (see coord/cli.py's
    # `_warn_if_source_install_drift`/`_editable_checkout_drift`). The old
    # single `Bash(pip install -e *)` entry only matched `-e` IMMEDIATELY
    # after `install`, so putting any other flag first evaded it entirely.
    # Every entry below is duplicated with a leading `*` (covers `python -m
    # pip install ...` / `python3 -m pip install ...`, which reach the exact
    # same installer) and, for `-e`/`--editable` specifically, ALSO with an
    # interior `*` before the flag (covers it appearing anywhere in argv,
    # not just first) — the adjacent form alone still catches
    # `pip install -e --break-system-packages .` (the flag being pushed
    # AFTER `-e` doesn't break the immediate `install -e` adjacency), but
    # not `pip install --break-system-packages -e .` (something pushed
    # BEFORE it).
    "Bash(pip install -e *)",
    "Bash(*pip install -e *)",
    "Bash(pip install * -e *)",
    "Bash(*pip install * -e *)",
    "Bash(pip install --editable *)",
    "Bash(*pip install --editable *)",
    "Bash(pip install * --editable *)",
    "Bash(*pip install * --editable *)",
    # `--break-system-packages` / `--user` are denied INDEPENDENTLY of
    # `-e`/`--editable` — either flag alone, on a perfectly ordinary
    # non-editable `pip install`, still lets a worker write into (or
    # reconfigure) the interpreter coord itself runs under. No adjacency
    # requirement at all: both are boolean flags a real `pip install`
    # invocation can place anywhere after `install`.
    "Bash(pip install *--break-system-packages*)",
    "Bash(*pip install *--break-system-packages*)",
    "Bash(pip install *--user*)",
    "Bash(*pip install *--user*)",
]


class ConfigError(Exception):
    """Raised when coordinator.yml is missing, malformed, or fails validation."""


@dataclass
class HooksConfig:
    on_round_complete: list[str] = field(default_factory=list)
    on_session_end: list[str] = field(default_factory=list)


@dataclass
class ReviewsConfig:
    """Adversarial code review settings.

    `enabled=True` by default. When enabled, `coord pr` auto-dispatches an
    adversarial review to a different machine after the PR worker is sent.
    Completion of a "work" assignment via reconciliation also triggers review
    dispatch automatically (see coord/review.py). Set `enabled: false` in
    coordinator.yml to opt out.
    """

    enabled: bool = True
    auto_dispatch: bool = True
    require_approval: bool = False
    # #1811: optional provider override for the REVIEW dispatch, independent
    # of the repo's own worker provider (`Repo.provider`). Threaded as
    # `spec_provider` at both `guard_unattended_dispatch` call sites in
    # coord/review.py — same precedence seam `resolve_provider_name` already
    # implements (spec > repo > providers.default), just given a review-only
    # entry point. `None` (the default) inherits `repo.provider` exactly as
    # before this field existed — no existing deployment's behavior changes.
    # Without this, the only way to review a repo pinned to a second backend
    # (e.g. `provider: opencode`) is to let the review silently inherit that
    # same backend — sharing the worker's model family removes the "zero
    # shared context" independence adversarial review depends on. Validated
    # against `providers.definitions` at parse time in `_parse_reviews` (an
    # unknown name is a config error, not a dispatch-time surprise); the
    # `human_attended_only` TOS gate (#437) still applies to whatever this
    # resolves to, same as `repo.provider`.
    provider: str | None = None
    reviewer_prompt: str = ""
    checklist: list[str] = field(default_factory=lambda: [
        "Check for platform-specific code in shared/cross-platform paths",
    ])
    repo_overrides: dict[str, list[str]] = field(default_factory=dict)
    # Flood guard (incident 2026-06-08): bound *bulk* review dispatch so a
    # backlog "unmasking" (e.g. removing a gate that had been suppressing
    # reviews) can't fire hundreds of metered `claude -p` reviews in one pass.
    # See coord.review.dispatch_pending_reviews.
    max_auto_dispatch_per_pass: int = 5  # cap reviews dispatched per reconcile/notify pass (0 = unbounded)
    flood_threshold: int = 12  # if more rows than this are pending review in one pass, refuse all (0 = no surge gate)
    allow_review_flood: bool = False  # override the surge gate (or set env COORD_ALLOW_REVIEW_FLOOD=1)
    # #1488: sanity bound (additions+deletions) for `coord review-reaffirm` —
    # the audited escape hatch that re-points a stale-but-content-changed
    # approval's `review_head_sha` to the branch's current head instead of
    # requiring a full re-review. A mechanical conflict-resolution delta is
    # tens of lines; anything past this is refused outright (no override
    # flag) so the command can never be used to wave through a genuine
    # rewrite. 0 disables the bound (not recommended).
    reaffirm_max_diff_lines: int = 300


@dataclass
class ConcurrencyConfig:
    max_workers: int = 2
    stagger_seconds: float = 30.0
    backoff_base: float = 60.0
    max_retries: int = 3
    auto_reassign: bool = False
    stale_threshold: int = 3
    # Spawn `claude -p` through a transient `bash -c 'exec ...'` parent so the
    # immediate parent of claude is a short-lived shell. This is the upstream
    # headline fix for the daemon-spawn freeze (anthropics/claude-code#56268).
    bash_wrap_spawn: bool = True
    # First-output (TTFT) watchdog: if a worker produces zero output within
    # this many seconds, kill its process group and fail the assignment so the
    # auto_reassign path re-dispatches it. 0 disables the watchdog. This only
    # catches truly silent hangs — a rate-limited worker still emits output and
    # therefore passes the check.
    first_output_timeout: float = 600.0
    # Remote interactive-session staleness timeout (#588).  After a remote
    # ``claude-pty`` assignment has been running for longer than this many
    # hours, each reconcile pass probes the remote tmux session via SSH.  If
    # the session is dead (tmux has-session exits 1) the coordinator calls
    # ``finalize_remote_interactive_exit`` to push any commits and release the
    # machine slot.  If SSH is unreachable, a warning is emitted instead.
    # Default is 12 hours — generous enough that a genuinely long session is
    # never interrupted, but tight enough to catch orphaned rows from crashed
    # sessions overnight.  Set to 0 to disable the sweep entirely.
    interactive_session_timeout_hours: float = 12.0
    # #2638: wall-clock (NOT monotonic — see coord.agent's module comment on
    # `_wait_for_proc_or_result`) ceiling on how long a single leg's process
    # may run before it is killed and failed with a `runtime_ceiling_reason`.
    # A suspended/asleep host (a laptop lid closed mid-leg) held a gate for
    # 10.5h with nothing noticing before this existed. Generous default (6h,
    # matching `coord.agent._DEFAULT_RUNTIME_CEILING_S` — kept as a literal
    # here rather than imported, to avoid a config<->agent import cycle; keep
    # the two in sync by hand) — some legs legitimately run for hours. 0/None
    # disables the ceiling fleet-wide (pre-#2638 behaviour). RESTART-ONLY,
    # same class as `first_output_timeout`/`bash_wrap_spawn` above — read
    # once at `coord agent` startup, not refreshed by the config reload.
    runtime_ceiling_s: float = 6.0 * 60.0 * 60.0


@dataclass
class SmokeRule:
    """When a worker's diff touches any of `files`, the smoke machine must
    have all capabilities in `requires`.

    `files` patterns match by prefix against the relative paths returned by
    `gh pr view --json files`. A trailing `/` makes the prefix explicit; bare
    paths match if the touched path starts with the rule path (so `src/gtk`
    catches `src/gtk/foo.c` and `src/gtk_helpers.c`). Use `src/gtk/` to scope
    strictly to the directory.

    `command` (#3056) is an optional override of the Test-stage command for
    a diff this rule matches — routing to the one machine with a capability
    is useless if the command that runs there is the same repo-wide command
    every other machine would run. When set, it OUTRANKS `repos[].ci_command`,
    `smoke_tests.default_command`, and `repos[].test_command` for a matched
    diff (see `coord.smoke.resolve_smoke_command`'s precedence). Absent
    (`None`, the default), a rule behaves exactly as before #3056 — it only
    contributes to `requires` routing, never to command selection. When more
    than one matching rule declares a `command`, the first one in
    `capability_rules` declaration order wins (`coord.smoke.resolve_rule_command`).
    """

    files: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    command: str | None = None


@dataclass
class SmokeTestsConfig:
    """Smoke-test orchestration. Off by default — opt-in per project.

    `default_command` is the shell command the smoke agent runs (e.g.
    `make smoke` or `pytest tests/smoke`). Per-repo overrides flow through
    `Repo.test_command` already; this is the fallback when none is set.
    """

    auto_queue: bool = False
    default_command: str | None = None
    timeout_seconds: int = 600
    capability_rules: list[SmokeRule] = field(default_factory=list)


@dataclass
class AcceptanceDriverConfig:
    """One entry under ``acceptance.drivers.<repo_name>`` in coordinator.yml
    (#944, docs/ORACLE_LOOP.md), OR one entry in that repo's ``routes:`` list
    (#1125, in-repo path routing — see :class:`AcceptanceConfig.driver_for`).

    ``kind`` selects the framework-specific adapter that knows how to launch,
    drive, and parse a repo's sealed acceptance suite (``tui-tuidriver`` and
    ``cli-pytest`` are implemented; other kinds are declared here but
    rejected at run time by :mod:`coord.acceptance_drivers` until their
    issues land). ``run`` is the shell command that executes the suite and
    must print structured (JSON) verdicts to stdout — it may reference the
    ``{ms}`` template (substituted with the ``ms-NN`` milestone dirname by
    :func:`coord.acceptance_drivers.render_run_command`) to point at a
    milestone-scoped suite dir. ``mock`` is a glob (relative to the
    acceptance dir) for the viewable mock/assertion fixtures — ``*.screen``
    for ``tui-tuidriver``, ``*.out`` (expected CLI stdout) for
    ``cli-pytest`` — informational today, consumed by the future mock-author
    (#930). ``capability`` is the machine capability required to run this
    driver, intended to be routed the same way ``smoke_tests.capability_rules``
    routes smoke tests.

    ``setup`` (#1733) is an optional shell command run once, before ``run``,
    to provision whatever a driver needs that a bare ``git checkout``
    doesn't provide — e.g. ``npm ci`` for ``web-playwright``. It exists
    because ``coord acceptance record``'s whole design is a throwaway ``git
    worktree add --detach`` the worker never touches: that worktree has no
    ``node_modules`` (gitignored, never checked out) and no way to get one
    short of an explicit install step, so a JS driver's ``run`` failed with
    a bare ``exit 127`` (playwright not found) before ever producing a
    parsed verdict. ``tui-tuidriver`` (cargo fetches its own deps) and
    ``cli-pytest`` (runs against the ambient env) happen to self-provision,
    which is exactly why this went unnoticed until the first JS driver ran.
    Left empty (the default), no provisioning step runs — unchanged
    behaviour for every driver that doesn't need one. A non-zero exit from
    ``setup`` is reported as a distinct "provisioning failed" error rather
    than being folded into "tests failed"/"wrote no report" (see
    :func:`coord.acceptance_drivers.run_driver`).

    ``entrypoint`` (#1552) is the repo-root-relative file this driver's
    ``run`` command links its slices through — the sealed oracle's *crate
    root*, for a driver whose framework discovers tests via an entry point
    rather than by walking a directory. ``tui-tuidriver``'s
    ``cargo test --test acceptance`` cannot see
    ``tests/acceptance/ms-NN/slice.rs`` at all until
    ``tui/tests/acceptance.rs`` ``include!``s it, so that file is part of
    the oracle and must be declared here: it is folded into the sealed set
    (:meth:`AcceptanceConfig.sealed_paths`) so a ``test-author`` registering
    its slice there is expected rather than a scope violation, and a
    ``type="work"`` worker touching it still trips oracle tamper. Leave it
    empty for a directory-discovered suite — ``cli-pytest``'s
    ``pytest tests/acceptance/{ms}`` legitimately has no entry point, which
    is exactly why #1175's blanket refusal only ever broke the Rust route.

    ``match`` and ``routes`` implement #1125's in-repo path routing: a repo
    entry with a non-empty ``routes`` list is a *router* — its own
    ``kind``/``run``/``mock``/``capability``/``setup``/``entrypoint`` are
    unused and each element of ``routes`` is itself an
    ``AcceptanceDriverConfig`` with ``match`` set (a repo-root-relative
    glob, e.g. ``"coord/**"``). A route entry's own ``routes`` is always
    empty — nesting one level is the whole feature, not a recursive router.
    See :meth:`AcceptanceConfig.driver_for` for the resolution rule.

    ``capability`` IS consulted (#966): both ``coord acceptance run --all``
    and ``coord acceptance record`` preflight-check it against the invoking
    host via :func:`coord.acceptance.acceptance_capability_gap` and refuse
    loudly, naming a capable machine, rather than silently running on
    hardware that may not support the driver. #966 deliberately stopped
    there rather than building actual remote-exec routing (the way ``coord
    test``'s ``pick_smoke_machine``/``match_rules`` route smoke runs to
    capable hardware) — that's real new plumbing, unjustified until a driver
    with an *unroutable* capability mismatch actually exists; "fail loud
    instead of silently running wrong" was enough to unblock #944's only
    driver at the time. A daemon host must still satisfy every declared
    driver's capability itself, since ``record`` always executes wherever
    the daemon is (see ``coord.commands.acceptance._acceptance_record_via_daemon``)
    — that is a real, operator-facing constraint on which machine can be the
    daemon, not something this preflight check can route around.
    """

    kind: str = ""
    run: str = ""
    mock: str = ""
    capability: str = ""
    setup: str = ""
    entrypoint: str = ""
    match: str = ""
    routes: list["AcceptanceDriverConfig"] = field(default_factory=list)


# #944 sealing v1: the sealed acceptance tree, relative to the repo root.
# Kept here (rather than imported from `coord.acceptance`) so config parsing
# stays dependency-free; `coord.acceptance.ACCEPTANCE_DIRNAME` is the same
# directory without the trailing slash.
SEALED_ACCEPTANCE_DIR = "tests/acceptance/"


def entrypoint_sibling_acceptance_dir(entrypoint: str) -> str:
    """The ``acceptance/`` directory an entrypoint-linked driver ``include!``s
    its JIT-authored slices from (#2896), derived from the entrypoint path
    rather than hardcoded: ``"tui/tests/acceptance.rs"`` -> ``"tui/tests/
    acceptance/"``, ``"tests/acceptance.rs"`` -> ``"tests/acceptance/"``.

    Public (no leading underscore) — :mod:`coord.acceptance` imports this to
    resolve which directory a resolved driver's manifests/contracts actually
    live under (:func:`coord.acceptance.acceptance_root_for_driver`), the
    same derivation :meth:`AcceptanceConfig.sealed_paths` uses to seal it.

    ``_acceptance_entrypoint`` already rejects an ``entrypoint:`` with no
    basename or a directory (trailing slash), so *entrypoint* is always at
    least a bare filename here.
    """
    directory, _, _ = entrypoint.rpartition("/")
    prefix = f"{directory}/" if directory else ""
    return f"{prefix}acceptance/"


@dataclass
class AcceptanceConfig:
    """``acceptance.drivers`` — repo name -> :class:`AcceptanceDriverConfig`."""

    drivers: dict[str, AcceptanceDriverConfig] = field(default_factory=dict)

    def entrypoints(self, repo_name: str) -> list[str]:
        """Every ``entrypoint:`` declared by *repo_name*'s acceptance driver
        (#1552), deduped, declaration order preserved.

        Path-independent by design, exactly like :meth:`has_driver` and for
        the same reason: the callers (sealing, the reviewer's scope rule,
        ``dispatch``'s forbid list) are deciding what the *whole repo's*
        oracle covers, not which single route a given file resolves to. A
        routed repo contributes one entry per route that declares one; a
        flat repo contributes at most its own. Repos with no driver — and
        drivers whose suite is directory-discovered (``cli-pytest``) —
        return ``[]``.
        """
        entry = self.drivers.get(repo_name)
        if entry is None:
            return []
        out: list[str] = []
        for cfg in [entry, *entry.routes]:
            ep = cfg.entrypoint.strip()
            if ep and ep not in out:
                out.append(ep)
        return out

    def sealed_paths(self, repo_name: str) -> list[str]:
        """The full sealed-oracle path set for *repo_name* (#944 sealing v1,
        #1552, #2896) — ``[]`` when the repo has no acceptance driver at all.

        Two kinds of entry, distinguished by the trailing slash:

        - a directory prefix (``"tests/acceptance/"``, and, per below, each
          entrypoint's own sibling ``.../acceptance/`` dir) — everything
          under it is sealed.
        - each declared driver ``entrypoint`` (e.g.
          ``"tui/tests/acceptance.rs"``) — an exact file.

        #1552: before this was derived, the set was a single hardcoded
        literal in ``coord.review``, which happened to fit ``cli-pytest``
        (pytest walks the directory) and was structurally unsatisfiable for
        ``tui-tuidriver`` (cargo needs a crate root that ``include!``s each
        slice). A ``test-author`` on the Rust route could either wire its
        slice in and trip a mandatory ``request-changes``, or leave it
        unwired and ship 476 lines of dead code. Deriving the set from the
        driver definition lets each route declare its own entry point
        instead.

        #2896: the repo-root ``tests/acceptance/`` tree no longer holds
        every milestone's slices — an entrypoint-linked driver's JIT-authored
        slices now live beside its own entrypoint (``tui/tests/
        acceptance.rs`` wires in ``tui/tests/acceptance/ms-NN/*.rs``, moved
        out of the repo root so the crate is self-contained), not under the
        shared root used by directory-discovered drivers like ``cli-pytest``
        (whose ``ms-37`` slices are still exactly there). So each entrypoint
        also seals its own sibling ``acceptance/`` directory — derived from
        the entrypoint path, not a second hardcoded literal, since a repo
        whose entrypoint already sits at the tree root (e.g. a future
        standalone ``coord-tui`` repo's flat ``tests/acceptance.rs``) has
        that sibling collapse onto ``SEALED_ACCEPTANCE_DIR`` itself — see the
        dedup below.
        """
        if not self.has_driver(repo_name):
            return []
        out = [SEALED_ACCEPTANCE_DIR]
        for ep in self.entrypoints(repo_name):
            if ep not in out:
                out.append(ep)
            sibling = entrypoint_sibling_acceptance_dir(ep)
            if sibling not in out:
                out.append(sibling)
        return out

    def acceptance_search_roots(self, repo_name: str) -> list[str]:
        """Every directory (repo-relative, trailing-slash) that could hold
        *repo_name*'s milestone-scoped acceptance slices (#2896) — the
        directory entries of :meth:`sealed_paths`, i.e. that method's output
        with each declared ``entrypoint:`` FILE filtered back out, leaving
        just the repo-root shared tree (``tests/acceptance/`` — still where
        a directory-discovered driver's slices, e.g. ``cli-pytest``'s
        ms-37, live) plus each entrypoint-linked driver's own sibling
        ``acceptance/`` dir (e.g. ``tui/tests/acceptance/``, where a
        relocated slice like ms-65 now lives instead). ``[]`` when the repo
        has no acceptance driver at all — mirrors :meth:`sealed_paths`.

        Path-independent by design, like :meth:`has_driver`/
        :meth:`entrypoints`: for a caller that knows WHICH milestone/issue
        it's after but not (yet) which route governs it — Gate A signoff,
        oracle readiness, worker-briefing injection, JIT test-author
        dispatch (#2896 review) — there is no single path in hand to feed
        :meth:`driver_for`, so it must search every place that milestone
        could live rather than guess one. A milestone's slice lives under
        exactly one of these; callers try each until one has it.
        """
        return [p for p in self.sealed_paths(repo_name) if p.endswith("/")]

    def driver_for(
        self, repo_name: str, path: str | None = None,
    ) -> AcceptanceDriverConfig | None:
        """Resolve *repo_name*'s acceptance driver, optionally routed by
        *path* (#1125, repo-root-relative — e.g. ``"coord/acceptance.py"``).

        - Unknown repo -> ``None``.
        - Repo entry has no ``routes`` (today's flat single-driver form,
          back-compat) -> the entry itself, regardless of *path*.
        - Repo entry has ``routes`` -> the **first** route whose ``match``
          glob matches *path* (``fnmatch`` semantics, e.g. ``"coord/**"``
          matches ``"coord/acceptance.py"``); first-match wins when more
          than one route's glob matches. ``path=None`` against a routed
          entry can't select a route, so it returns ``None`` rather than
          guessing one — callers that know they're driving a specific file
          (or an issue's manifest-mapped path) must pass it.

        Resolution rule when a milestone/issue's slice spans more than one
        route (#1125 review finding 4): this method makes no attempt to
        detect or merge across routes for a single call — it resolves
        exactly one *path* to exactly one route (or ``None``). A caller
        whose work spans multiple routes (e.g. a full-stack issue touching
        both ``coord/**`` and ``tui/**``) must pick ONE representative path
        for the invocation (or invoke once per route) rather than expect
        this method to fan out; callers driving a whole repo/milestone with
        no single path in hand (Gate A, sealing, briefing-injection) should
        use :meth:`has_driver` instead, which is path-independent by design.
        """
        entry = self.drivers.get(repo_name)
        if entry is None:
            return None
        if not entry.routes:
            return entry
        if path is None:
            return None
        for route in entry.routes:
            if fnmatch.fnmatch(path, route.match):
                return route
        return None

    def has_driver(self, repo_name: str) -> bool:
        """Path-independent "does this repo participate in the oracle loop
        at all" predicate (#1125 review finding 1).

        True when *repo_name* has ANY acceptance driver configured — flat
        or routed — regardless of which route a given path would resolve
        to. Use this for existence-only checks that must not silently flip
        the moment a repo adopts ``routes:`` — Gate A
        (``coord.milestone_dispatch.gate_a_status``), the ``tests/acceptance/``
        sealing/forbid list (``coord.dispatch.dispatch``), and the
        oracle-loop briefing-contract injection (``coord.dispatch.dispatch``)
        all only need "yes/no", never a concrete driver to run — use
        :meth:`driver_for` (with a *path*) for that.
        """
        return repo_name in self.drivers


# #1430: plan-worker ESTIMATE -> escalation rung (see ModelsConfig.model_for_estimate).
_ESTIMATE_RUNG: dict[str, int] = {"trivial": 0, "small": 0, "medium": 1, "large": 2}


@dataclass
class ModelsConfig:
    """Model tier selection and escalation ladder for workers.

    `default` is the model passed to ``claude -p`` when an assignment doesn't
    specify one.  `escalation` is an ordered list of model aliases (low →
    high); when a worker fails or gets stuck, the coordinator escalates to
    the next entry via `next_model`.  `labels` is a per-issue-label override
    (e.g. ``documentation: haiku``) resolved via :meth:`model_for_labels` —
    consulted by every ``type="work"`` dispatch site (``coord plan`` /
    ``approve``, ``coord assign``, ``coord milestone dispatch``); plan-stage
    and review-stage dispatches deliberately stay on ``default`` (#1430).

    `versions` pins an alias to an exact model id, e.g.
    ``{sonnet: claude-sonnet-4-6, opus: claude-opus-4-7}``.  When set, the
    coordinator translates the alias to the exact id before passing it to
    ``claude -p --model`` on the worker.  Aliases not present in the map
    pass through unchanged, so ``claude -p`` falls back to its CLI default
    (which today is whatever the installed claude-cli treats as latest).
    """

    default: str = "sonnet"
    escalation: list[str] = field(
        default_factory=lambda: ["haiku", "sonnet", "opus"]
    )
    labels: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def next_model(self, current: str) -> str:
        """Return the next model in the escalation ladder.

        If *current* is already at the top of the ladder, or isn't on the
        ladder at all, return *current* unchanged.
        """
        try:
            idx = self.escalation.index(current)
        except ValueError:
            return current
        if idx + 1 < len(self.escalation):
            return self.escalation[idx + 1]
        return current

    def resolve(self, alias: str | None) -> str | None:
        """Resolve an alias to its pinned exact model id, if configured.

        Returns *alias* unchanged when no mapping exists, and ``None`` when
        *alias* is ``None`` (preserves the "omit --model" code path).
        """
        if alias is None:
            return None
        return self.versions.get(alias, alias)

    def model_for_labels(self, issue_labels: list[str]) -> str | None:
        """Resolve an issue's GitHub labels to a model alias via ``labels``.

        #1430: ``labels`` used to be validated at parse time and read by
        nothing — every dispatch ran ``default`` regardless of the issue's
        tier/category label. This is the resolver every dispatch site now
        calls before falling back to ``default`` itself.

        Precedence when an issue carries several configured labels (e.g.
        both ``bug`` and ``tier:large``) — see
        :meth:`model_for_labels_with_reason` for the full rule (#1633).

        Returns ``None`` (never ``default``) when no configured label is
        present on the issue, or ``labels`` itself is empty — mirroring the
        None-passthrough style of :meth:`resolve`. Callers are expected to
        fall back to ``default`` themselves, e.g.::

            model = override or config.models.model_for_labels(issue_labels) or config.models.default
        """
        return self.model_for_labels_with_reason(issue_labels)[0]

    def model_for_labels_with_reason(
        self, issue_labels: list[str]
    ) -> tuple[str | None, str | None, list[str]]:
        """Like :meth:`model_for_labels`, but also returns the label that
        matched and any configured labels it shadowed.

        Returns ``(model, matched_label, shadowed_labels)``, or
        ``(None, None, [])`` under the same conditions
        :meth:`model_for_labels` returns ``None``.

        #1633: precedence used to be decided by *issue-label order* — the
        order GitHub happens to return the issue's own labels in, which
        nothing in this repo controls. That made ``tier:small``/
        ``tier:large`` no-ops on any issue that also carried a type label
        (``bug``/``enhancement``/...), and let the same issue re-route
        just by having a label removed and re-added. Precedence is now
        deterministic and config-driven instead:

        1. ``tier:*`` entries are checked first — they are documented as
           size-tier *overrides* over the type-label entries.
        2. All other entries are checked next.

        Within each group, ties are broken by ``labels``'s own iteration
        order (insertion order from ``coordinator.yml``), not the issue's
        label order — so the same issue + config always resolves to the
        same model, regardless of what order GitHub reports the issue's
        labels in.

        #1454: a silent fall-through to ``default`` (stale/missing label)
        looks identical to an intentional default from the CLI output
        alone. Callers use the matched label to print *why* a model was
        chosen, and *shadowed_labels* (every other configured label also
        present on the issue, in resolution order) to print what lost —
        see :func:`describe_model_choice`.
        """
        if not self.labels:
            return None, None, []
        present_in_config_order = [
            label for label in self.labels if label in issue_labels
        ]
        if not present_in_config_order:
            return None, None, []
        tier_candidates = [
            label for label in present_in_config_order if label.startswith("tier:")
        ]
        other_candidates = [
            label for label in present_in_config_order if not label.startswith("tier:")
        ]
        ordered_candidates = tier_candidates + other_candidates
        matched = ordered_candidates[0]
        shadowed = ordered_candidates[1:]
        return self.labels[matched], matched, shadowed

    def model_for_estimate(self, estimate: str | None) -> str | None:
        """Map a plan worker's ``ESTIMATE`` to a model alias via ``escalation``.

        #1430: once a plan has run, its ``ESTIMATE`` (trivial | small |
        medium | large — derived from actually reading the code) is a
        better-informed signal than the label chosen at issue-creation time,
        so ``approve_plan`` uses this to override the label-derived model
        for the work assignment it dispatches.

        ``trivial``/``small`` resolve to the lowest rung of ``escalation``,
        ``medium`` to the middle, ``large`` to the top — clamped to
        ``len(escalation) - 1`` so a short/custom ladder doesn't index out
        of range. Returns ``None`` for an empty/unrecognised estimate or an
        empty ``escalation`` list — callers fall back to the label-derived
        or default model themselves.
        """
        if not estimate or not self.escalation:
            return None
        idx = _ESTIMATE_RUNG.get(estimate.strip().lower())
        if idx is None:
            return None
        idx = min(idx, len(self.escalation) - 1)
        return self.escalation[idx]


def describe_model_choice(
    *,
    resolved_model: str,
    explicit_reason: str | None = None,
    matched_label: str | None = None,
    shadowed_labels: list[str] | None = None,
) -> str:
    """Format a one-line explanation of why *resolved_model* was chosen.

    #1454: dispatch used to print just the bare model name, so a silent
    mis-route to ``models.default`` (e.g. a tier label that hadn't been
    picked up yet) read identically to an intentional default — the exact
    ambiguity that made the stale-label-cache bug expensive to notice.

    *explicit_reason*, when set, wins outright (e.g. ``"explicit --model"``
    or ``"resolved at plan time"``) — the caller already knows the model
    didn't come from a fresh label match. Otherwise *matched_label* (from
    :meth:`ModelsConfig.model_for_labels_with_reason`) selects between the
    "via label" and "default; no label match" phrasings.

    #1633: when the issue carried more than one configured label,
    *shadowed_labels* names the ones that lost, so a route that might look
    surprising (e.g. ``tier:large`` winning over ``enhancement``) is
    self-explaining at dispatch time instead of reading like the older,
    order-dependent bug.
    """
    if explicit_reason:
        return f"{resolved_model} ({explicit_reason})"
    if matched_label:
        if shadowed_labels:
            shadowed_str = ", ".join(repr(label) for label in shadowed_labels)
            return (
                f"{resolved_model} (via label {matched_label!r}, "
                f"shadowing {shadowed_str})"
            )
        return f"{resolved_model} (via label {matched_label!r})"
    return f"{resolved_model} (default; no label match)"


@dataclass
class DispatchConfig:
    """Smart task-splitting configuration.

    When ``auto_split`` is ``True`` (the default), the ``coord approve``
    command analyses each proposal's ``files_likely`` list.  If the file
    count exceeds ``max_files_per_worker``, the work is shown to the user
    split into parallel/sequential chunks for confirmation before dispatch.

    Set ``auto_split: false`` to disable the splitting analysis entirely.

    When ``require_plan`` is ``True``, ``coord assign`` defaults to
    ``--plan-only`` behaviour — the worker reads the codebase and produces a
    structured plan without writing any code.  The user then runs
    ``coord approve-plan`` or ``coord reject-plan`` to act on the plan.
    Pass ``--no-plan`` to ``coord assign`` to override this default and
    dispatch a work assignment directly.  Assignments of type ``review``,
    ``smoke``, or ``plan`` are never affected by this setting.
    """

    max_files_per_worker: int = 8
    auto_split: bool = True
    require_plan: bool = False


@dataclass
class UsageGateConfig:
    """Pre-flight gate on the account's Max-plan 5h/weekly usage windows
    (#1466).  ``coord drive``'s ``preflight()`` and the ``coord approve``
    batch path probe ``claude -p "/usage"`` (see ``coord.usage_limits``) and
    consult this before dispatching, so a run doesn't start work that's
    certain to run straight into a 5-hour or weekly wall — the first sign of
    which was previously a worker dying mid-task with the branch stranded.

    ``mode`` is both the on/off switch and the enforcement level:

    - ``"disabled"``   — never probe, never gate. Pre-#1466 behaviour.
    - ``"warn"``  — probe and print a warning above threshold, but never
      refuse a dispatch. **The default** — until the probe's prose parse
      (``.result`` is NOT a stable contract, see ``coord.usage_limits``'s
      docstring) has enough field mileage to trust for blocking real work.
    - ``"block"`` — refuse to dispatch above threshold.

    A probe that fails or returns "unknown" (no OAuth subscription session,
    unparseable output, timeout, ...) NEVER blocks or warns regardless of
    ``mode`` — see ``coord.usage_limits.evaluate_usage_gate``.

    CAVEAT: Anthropic announced ``claude -p``/Agent SDK usage moving off the
    subscription windows onto a separate monthly credit pool; that rollout
    is paused as of 2026-06-15, so today this gate correctly predicts a
    headless worker running into the same session/weekly walls ``/usage``
    reports. If the rollout resumes, this gate stops being predictive and
    would need to switch to tracking credit balance instead.
    """

    mode: str = "warn"  # "disabled" | "warn" | "block"
    session_threshold_pct: float = 85.0
    week_threshold_pct: float = 90.0


@dataclass
class BudgetConfig:
    """``budget:`` block (#2131) — the per-leg spend ceiling.

    A single worker leg that runs away is the fleet's most expensive failure
    mode: over 2026-08-08 → 08-11 the twelve legs costing more than $10 were
    3% of all legs and 19% of the entire bill.  This block bounds that tail.

    ``per_leg_ceiling_usd`` is the ceiling in dollars applied to every
    assignment type that has no ``type_ceilings`` entry.  **``0.0`` — the
    default, and what an absent ``budget:`` block yields — means NO CEILING**,
    i.e. exactly today's behaviour, so upgrading can never start killing an
    existing deployment's legs.

    ``type_ceilings`` overrides it per assignment ``type``.  This matters:
    the same window's medians were $3.61 for a sonnet work leg, $6.61 for an
    opus one, $1.61 for a review and $0.44 for a smoke — an $8 ceiling that
    is right for ``work`` is wildly wrong for ``smoke``.  An explicit ``0``
    for a type disables the ceiling for that type alone.

    Resolution is :meth:`ceiling_for`.  The value is carried to the agent on
    the ``POST /assign`` wire (``AssignmentSpec.cost_ceiling_usd``) rather
    than read from an agent's own config, so a config-free agent
    (docs/EPHEMERAL_WORKERS.md) is covered too.

    **Headless legs only.**  Enforcement reads the worker's stream-json
    transcript, and interactive legs are precisely the logs that are not
    stream-json (#1710) — see :mod:`coord.spend_ceiling` for the full
    statement of that limit and of what the mid-flight number actually is.
    """

    per_leg_ceiling_usd: float = 0.0
    type_ceilings: dict[str, float] = field(default_factory=dict)

    def ceiling_for(self, assignment_type: str | None) -> float | None:
        """Ceiling (USD) for *assignment_type*, or ``None`` for no ceiling.

        A ``type_ceilings`` entry always wins — including an explicit ``0``,
        which disables the ceiling for that type rather than falling back to
        the global one.  Absent that, the global ``per_leg_ceiling_usd``
        applies, and ``0`` there means no ceiling at all.
        """
        if assignment_type and assignment_type in self.type_ceilings:
            value = self.type_ceilings[assignment_type]
            return value if value > 0 else None
        return self.per_leg_ceiling_usd if self.per_leg_ceiling_usd > 0 else None


# #846: default wall-clock thresholds (seconds) an assignment of a given
# `type` may run before `coord.notify.detect_needs_attention` flags it.
# Deliberately generous — this is a "human should glance at this" signal,
# not a kill switch (detection + surfacing only, see issue #846).
#
# These are all *headless* types — a `claude -p` worker converging toward a
# result with no one attending it live, so "running way longer than usual"
# is a meaningful stuck signal. `plan`/`mock-author`/`test-author` are
# lighter-weight than `work` (no code-writing convergence loop) but still
# headless, so they get their own explicit (rather than work-fallback)
# tuning. `conflict-fix` is dual-purpose — the automated #241 worker *and*
# the interactive `--merge-of` session share this type (see
# `coord.reconcile.is_interactive_merge_session`) — so it gets a little more
# headroom than `work` to cover a human resolving a semantic conflict.
#
# #1137 audit note: the #1133 follow-up asked whether `merge`/`fix` (the two
# types named in the original #846 ask but left unhandled by #1133) need
# their own entry. `merge` does NOT — there is no literal `type="merge"`
# (a dedicated value was tried and reverted, see
# `is_interactive_merge_session`'s docstring / tests/test_reap_merged_sessions.py
# DISCRIMINATOR NOTE); the interactive `--merge-of` session already shares
# `conflict-fix` above and is covered by its 60m threshold. `fix` (the
# interactive `--fix-of`/`--rework-of` human-attended session) DOES need
# handling — it shares `type="work"` with headless coding workers, so it
# can't get its own entry here either. Instead `attention_threshold_for`
# recognizes it via the same compound discriminator shape as
# `is_interactive_merge_session` — `provider_name="claude-pty"` +
# `review_of_assignment_id` set on a `type="work"` row — and reuses
# `conflict-fix`'s threshold (the same "human resolving someone else's
# feedback" scenario).
#
# #1144 audit note: the same dual-purpose shape exists for `review` and
# `smoke`. Headless auto-review (`coord.review`, no `provider_name`) and
# headless smoke (`coord.smoke`, no `provider_name`) share their types with
# the interactive `--review-of`/`--smoke-of` sessions
# (`coord.commands.dispatch_workers`, `provider_name="claude-pty"` +
# `review_of_assignment_id` set) - plain `review`/`smoke` are only 15m/20m,
# so a human reading a diff or babysitting a smoke run for that long is
# normal, not stuck. `attention_threshold_for` extends the same compound
# discriminator to `assignment_type in ("work", "review", "smoke")` and
# defers all three to `conflict-fix`'s 60m threshold rather than giving
# review/smoke their own tuned value - one interactive threshold for every
# "human attending a claude-pty session tied to an earlier assignment" case
# is easier to reason about than three, and 60m is already generous enough
# to cover a human reading a diff or watching a smoke run.
_DEFAULT_ATTENTION_THRESHOLDS: dict[str, float] = {
    "work": 45 * 60.0,
    "review": 15 * 60.0,
    "smoke": 20 * 60.0,
    "plan": 30 * 60.0,
    "mock-author": 30 * 60.0,
    "test-author": 30 * 60.0,
    "conflict-fix": 60 * 60.0,
}

# #1133: assignment types that are human-attended interactive sessions — a
# developer reading/thinking/typing at a live `claude` TTY (driven via
# `POST /inject/{id}` from the TUI), not a headless worker converging toward
# a result. These have no wall-clock "stuck" concept: a human legitimately
# spending hours reading an issue, chatting through a plan, or validating a
# diff is normal, not stalled (the #846 wall-clock check exists to catch a
# headless worker silently burning budget — see `attention_signal`'s
# docstring for the #448 motivation, which doesn't apply here). Exempt from
# the wall-clock signal unconditionally in `attention_threshold_for` —
# *not* merely by omission from `_DEFAULT_ATTENTION_THRESHOLDS` — so a user
# who overrides `pipeline.attention_thresholds.work` in `coordinator.yml`
# can't accidentally re-arm this check for a chat session via the
# fallback-to-"work" behaviour (see that method's docstring). A user who
# explicitly configures a threshold for one of these types still wins —
# this is a default exemption, not an unconditional one.
INTERACTIVE_SESSION_TYPES: frozenset[str] = frozenset({
    "chat",
    "troubleshoot",
    "audit",
    "milestone-chat",
    "refinement",
    "new-issue-chat",
    "test-chat",
})


# #2649: per-assignment-type overrides for `coord drive`'s OWN stall
# detector (`Driver._loop`'s "no state change in Nm" nudge) — a different
# signal from `_DEFAULT_ATTENTION_THRESHOLDS` above despite the similar
# shape. That one measures wall clock since a row was *dispatched*
# (`status="running"`); this one measures minutes since the drive's board
# *fingerprint* last changed while a stage sits active — the two can and do
# diverge (a worker can be actively producing output for well over an
# attention threshold without the board fingerprint moving at all).
#
# Seeded from one measured run (claude-coordinator#2572, 2026-08-23,
# dellserver): under the flat 20m default, a `work` stage completed at
# ~26m and the Test-stage (board `type="smoke"`) completed at ~22m — both
# routine, both false positives. `work`/`smoke` get raised, evidenced
# entries here; every other type (`review` at ~8m, `conflict-fix` at ~9m in
# that same run, plus `merge`/`plan`/`mock-author`/`test-author`/... with no
# counter-evidence at all) is intentionally left OFF this table and keeps
# falling back to the drive's own `--stall` value exactly as before —
# see `PipelineConfig.stall_threshold_secs`.
_DEFAULT_STALL_THRESHOLDS: dict[str, float] = {
    "work": 35 * 60.0,
    "smoke": 35 * 60.0,
}


@dataclass
class LivenessAuditorConfig:
    """#2048: cheap, independent per-turn liveness auditor tunables.

    ``enabled`` (default ``False``) — the auditor ships dark. It costs a
    ``claude -p`` subprocess spawn per debounced audit, so it earns trust
    the same way ``auto_dispatch_stalled``/``escalate_semantic_conflicts``
    did before it: off until an operator turns it on. Unlike a
    daemon-side setting, a config change here does NOT need a
    ``coord-serve`` restart: ``detect_liveness_stall`` only runs inside
    ``coord notify``'s ``run()``, invoked as a fresh CLI process by the
    ``coord-notify.timer`` systemd unit (or by ``coord drive``'s stall
    nudge) — each invocation loads this config fresh off disk, so an edit
    takes effect on the next timer tick (≤5 min), independent of any
    long-running daemon.

    ``strikes`` — consecutive ``blocked`` verdicts required before an
    ``EVENT_LIVENESS_STALL`` is raised. One bad turn is normal; this many
    in a row is a stall. Default 3 (matches OpenChamber's Session Goals,
    the prior art this borrows from).

    ``debounce_seconds`` — minimum wall-clock gap between audits for the
    same assignment. A stall is a multi-minute phenomenon; auditing every
    turn buys no extra signal and pays for a process spawn each time.
    Default 60s.

    ``model`` — the model the audit subprocess runs. Default
    ``"claude-haiku-4-5"``, the cheapest current model — the audit's whole
    design rests on a fixed ~1k-token context, so the cost stays flat
    regardless of session length (see ``coord/liveness_auditor.py``'s
    module docstring for the numbers).

    ``timeout_seconds`` — subprocess timeout per audit call. Default 30s.

    ``claude_bin`` — override the ``claude`` binary path/name, mirroring
    other subprocess-spawning config in this file. ``None`` (default) uses
    the CLI's own resolution of ``claude`` on ``$PATH``.
    """

    enabled: bool = False
    strikes: int = DEFAULT_LIVENESS_STRIKES
    debounce_seconds: float = DEFAULT_LIVENESS_DEBOUNCE_SECONDS
    model: str = DEFAULT_LIVENESS_MODEL
    timeout_seconds: float = DEFAULT_LIVENESS_TIMEOUT_SECONDS
    claude_bin: str | None = None


@dataclass
class PipelineConfig:
    """Assignment lifecycle gate configuration.

    ``default_gates`` is the list of approval steps required for every work
    assignment unless overridden by an issue label.  ``labels`` maps GitHub
    issue label names to gate lists, allowing per-label overrides — e.g.
    a ``hotfix`` label could bypass review with ``hotfix: [merge]``.

    ``auto_loop`` enables the automated review → fix → re-review cycle.
    When ``True`` (default), a review that requests changes automatically
    dispatches a fix worker.  The fix worker then receives a fresh review,
    and the cycle continues until the review approves or
    ``max_review_iterations`` is reached.

    ``max_review_iterations`` is the maximum number of fix rounds before
    the auto-loop stops and posts a notice asking for manual intervention.
    Default is 5.

    ``escalate_fix_model`` controls whether auto-dispatched fix workers
    escalate the model on each bounce iteration.  When ``True`` (default),
    the first fix stays on ``models.default`` and each subsequent fix
    iteration climbs one rung up ``models.escalation`` (capped at the top).
    When ``False``, fix dispatches set no model (today's behaviour: the
    agent falls back to ``claude -p``'s default).

    ``attention_thresholds`` (#846) maps assignment ``type`` (``"work"``,
    ``"review"``, ``"smoke"``, ...) to a wall-clock duration (seconds) that
    an assignment may sit in ``status="running"`` before
    ``coord.notify.detect_needs_attention`` flags it. A type not present in
    the mapping falls back to ``_DEFAULT_ATTENTION_THRESHOLDS``, *unless*
    it's a human-attended interactive type (#1133,
    :data:`INTERACTIVE_SESSION_TYPES` — ``"chat"``, ``"troubleshoot"``,
    ``"audit"``, ``"milestone-chat"``, ``"refinement"``,
    ``"new-issue-chat"``, ``"test-chat"``), which is exempt from the
    wall-clock check entirely by default — see
    :meth:`attention_threshold_for`. An interactive ``--fix-of``/
    ``--rework-of`` session (#1137) is also recognized there, by
    ``provider_name``/``review_of_assignment_id`` rather than ``type``
    (it shares ``type="work"`` with headless coding workers), and reuses
    ``conflict-fix``'s threshold. The interactive ``--review-of`` and
    ``--smoke-of`` sessions (#1144) are recognized the same way for
    ``type="review"``/``type="smoke"`` and also reuse ``conflict-fix``'s
    threshold, rather than plain review's 15m or smoke's 20m.

    ``escalate_semantic_conflicts`` (#1291) controls whether a conflict-fix
    worker that gives up on a **semantic** conflict (it emits the
    ``coord:conflict=semantic`` marker on its ``STUCK:`` line) gets ONE
    second attempt from a stronger model
    (``semantic_conflict_model``, default ``"opus"``) before the merge
    entry is parked ``HUMAN_REQUIRED``.  **Defaults to ``False``** — this
    ships dark until it earns trust on real conflicts.  The escalated
    attempt consumes the existing one-per-entry conflict-fix retry cap, so
    a second semantic failure goes to ``HUMAN_REQUIRED`` exactly as today;
    there is no loop.  It is still subject to every merge gate (tests,
    ``coord verify-merge``, CI, review) — nothing is force-merged.

    #2566: shipping dark by default means the tier this flag guards can
    look, from the outside, like it silently failed rather than never ran.
    When a conflict-fix worker's SEMANTIC verdict is read (see #2565) and
    this flag is off, ``coord.reconcile.on_conflict_fix_done`` says so
    explicitly in the parked entry's error and in the GitHub comment —
    :func:`coord.conflict_fix.semantic_escalation_disabled` is the
    predicate. That closes the *legibility* half of the gap; turning the
    tier on for real is still a ``coordinator.yml`` decision an operator
    makes deliberately, not something this repo's code can default for
    every fleet.

    ``convergence_rounds`` (#846) is the number of fix/review rounds
    (``Assignment.review_iteration``) an assignment may accumulate without
    reaching a green test verdict + approved review before it is flagged as
    non-converging (thrashing). Default 3.

    ``auto_dispatch_stalled`` (#1478) controls whether
    ``coord.notify.detect_stalled_pipeline`` (#1441) gets a dispatch arm in
    addition to its diagnostic GitHub comment. Detection + narration is
    always on (there is no flag for that half — it is cheap and has shipped
    since #1441); this flag gates only the *action*: enqueueing an
    ``approved_not_queued`` row for merge, dispatching a conflict-fix for a
    merge entry stuck ``CONFLICT``, or dispatching the fix/review a
    review-completion transition would have. **Defaults to ``False``** —
    ships dark, same posture as ``escalate_semantic_conflicts``, until
    unattended dispatch on a stalled row earns trust. The one-shot
    ``notified`` ledger keyed by ``_stalled_notified_key`` (shared with the
    comment) still applies, so a row is acted on once per tick-cycle, not
    every 5 minutes.

    ``liveness_auditor`` (#2048) configures the cheap, independent,
    per-turn liveness auditor — see :class:`LivenessAuditorConfig`. Off by
    default; gates nothing even when enabled (see
    :func:`coord.notify.detect_liveness_stall`).

    ``confirm_test_verdict`` (#2464) controls whether a Test-stage PASS
    claim is independently re-run before it is recorded — see
    :mod:`coord.confirm_test`. **Defaults to ``True``**, unlike the
    ships-dark flags above, and deliberately so: the thing it guards is a
    verdict the worker issued about its own work, and a correctness gate
    that must be switched on is the posture that let the defect ship. It is
    also cheap to leave on where it cannot apply — a machine with no local
    checkout of the repo returns "inconclusive" and the pre-#2464 behaviour
    stands. Costs one real suite run per Test-stage completion where it
    *can* apply; set ``false`` (or ``COORD_CONFIRM_TEST_VERDICT=0``) to opt
    back out.

    ``auto_heal_phantom_rows`` (#2536) controls whether
    ``coord.notify._sweep_phantom_rows`` gets to act, on
    ``coord-notify.timer``'s existing cadence, on a ``running`` board row
    its own recorded machine confirms is dead (see
    ``coord.diagnose.sweep_dead_running_rows``). **Defaults to ``True``**,
    the same posture as ``confirm_test_verdict`` and for the same reason:
    this closes a real bug (a phantom row silently holding its repo's
    entire drive-queue concurrency slot indefinitely — #2536) rather than
    adding a new capability, and every action it takes is gated behind a
    CONFIRMED-dead liveness read (never an ambiguous "unknown" one — #1870)
    plus a wall-clock buffer past the row's own ``needs_attention``
    threshold, so it never races a session that is merely idle between
    turns. Unlike ``auto_dispatch_stalled``/``escalate_semantic_conflicts``,
    it never dispatches new work and never touches a branch — the recovery
    is the same non-destructive one ``coord diagnose --reset`` already
    performs by hand (branch/commits preserved, stage re-dispatchable). Set
    ``false`` to require a human to run ``coord diagnose --reset``
    themselves, as before #2536.

    ``auto_heal_stuck_test_state`` (#2803) controls whether
    ``coord.notify._sweep_stuck_test_state`` gets to act, on the daemon's
    own tick (``coord.notify.run_drain``), on a work row wedged at
    ``test_state="running"`` well past its Test-stage child's own
    resolution — see ``coord.diagnose.sweep_stuck_test_state_rows``.
    **Defaults to ``True``**, the same posture as ``auto_heal_phantom_rows``
    and for the same reason: this closes a real bug (a lost Test-stage
    verdict write silently blocking the whole pipeline, and every
    ``after=``-chained drive-queue entry behind it, until a 240-minute drive
    deadline gives up with a misleading cause — vimcode#555) rather than
    adding a new capability. Every action it takes is gated on the Test-stage
    child having already reached a TERMINAL status (or never having existed
    at all) plus a fixed grace window past that — it never touches a child
    that is still genuinely running, which stays
    ``auto_heal_phantom_rows``'/``coord.notify.detect_needs_attention``'s
    job. It never fabricates a pass/fail verdict either: recovery always
    goes through ``coord.reconcile.propagate_smoke_terminal_failure``, the
    same environmental-vs-work classification the manual ``coord diagnose
    --stage test`` recovery already uses. Set ``false`` to require a human
    to run that command themselves, as before #2803.

    ``max_fix_rounds`` (#2604) is the fleet-wide default for a
    **tick-launched** ``coord drive --tmux --max-fix-rounds`` — set once here
    instead of on every ``coord drive-queue add``. ``None`` (the default)
    leaves each tick-launched drive at
    ``coord.drive_queue.DEFAULT_TICK_MAX_FIX_ROUNDS`` (2), deliberately lower
    than interactive ``coord drive``'s own default of 3: see that constant's
    docstring for why an unattended round costs more than an attended one.
    An entry's own ``coord drive-queue add --max-fix-rounds`` always wins
    over this — see ``coord.drive_queue.effective_max_fix_rounds`` for the
    full resolution order. This does **not** change interactive ``coord
    drive``'s own default; it is read only by the drive-queue tick.

    ``max_parallel_per_repo`` (#2573) is the fleet-wide default for
    ``coord drive-queue tick``'s per-repo concurrency ceiling (#1972,
    ``--max-parallel-per-repo`` — see ``docs/DRIVE_QUEUE.md`` §9). It exists
    so that value can live in ``coordinator.yml`` instead of a systemd
    drop-in: a drop-in has to restate the packaged unit's ENTIRE
    ``ExecStart=`` to change one flag, and that copy silently drifts —
    #2573 found dellserver's live drop-in reverting #2314's pinned-venv
    ``ExecStart`` path back to a worker-overwritable one, purely as a side
    effect of the drop-in also carrying this setting. ``None`` (the
    default) leaves the ceiling at
    ``coord.drive_queue.DEFAULT_MAX_PARALLEL_PER_REPO`` (1). An explicit
    ``coord drive-queue tick --max-parallel-per-repo N`` on the command line
    always wins over this fleet default — see
    ``coord.commands.drive_queue.drive_queue_tick`` for the resolution
    order, which mirrors ``max_fix_rounds`` above.

    ``stall_thresholds`` (#2649) maps assignment ``type`` to a per-type
    override (seconds) for ``coord drive``'s OWN stall detector — see
    :data:`_DEFAULT_STALL_THRESHOLDS` for the shape and the evidence behind
    its two seeded entries, and :meth:`stall_threshold_secs` for how a type
    absent from the table falls back to the drive's flat ``--stall`` value.
    """

    default_gates: list[str] = field(default_factory=lambda: ["test", "review", "merge"])
    labels: dict[str, list[str]] = field(default_factory=dict)
    auto_loop: bool = True
    max_review_iterations: int = 5
    escalate_fix_model: bool = True
    # #1291 — DEFAULT OFF. See the class docstring.
    escalate_semantic_conflicts: bool = False
    semantic_conflict_model: str = "opus"
    # #1478 — DEFAULT OFF. See the class docstring.
    auto_dispatch_stalled: bool = False
    attention_thresholds: dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_ATTENTION_THRESHOLDS)
    )
    convergence_rounds: int = 3
    # #2048 — DEFAULT OFF. See LivenessAuditorConfig's docstring.
    liveness_auditor: LivenessAuditorConfig = field(default_factory=LivenessAuditorConfig)
    # #2464 — DEFAULT ON. See the class docstring.
    confirm_test_verdict: bool = True
    # #2536 — DEFAULT ON. See the class docstring.
    auto_heal_phantom_rows: bool = True
    # #2803 — DEFAULT ON. See the class docstring.
    auto_heal_stuck_test_state: bool = True
    # #2604 — None means "use coord.drive_queue.DEFAULT_TICK_MAX_FIX_ROUNDS".
    # See the class docstring.
    max_fix_rounds: int | None = None
    # #2573 — None means "use coord.drive_queue.DEFAULT_MAX_PARALLEL_PER_REPO".
    # See the class docstring.
    max_parallel_per_repo: int | None = None
    # #2649 — per-type overrides for `coord drive`'s own stall detector.
    # See the class docstring / _DEFAULT_STALL_THRESHOLDS.
    stall_thresholds: dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_STALL_THRESHOLDS)
    )

    def attention_threshold_for(
        self,
        assignment_type: str,
        *,
        provider_name: str | None = None,
        review_of_assignment_id: str | None = None,
    ) -> float:
        """Wall-clock threshold (seconds) for *assignment_type*.

        Checked in order:

        1. **Interactive session sharing a headless type** (#1137/#1144):
           ``assignment_type in ("work", "review", "smoke")`` with
           ``provider_name == "claude-pty"`` and
           ``review_of_assignment_id`` set (the optional keyword-only args,
           passed by callers that have the full assignment record; both
           default to ``None`` so existing callers that only know the type
           are unaffected). This mirrors
           :func:`coord.reconcile.is_interactive_merge_session`'s compound
           discriminator — a dedicated ``type="fix"``/``type="review-of"``/
           ``type="smoke-of"`` was deliberately not introduced, for the same
           reason a dedicated ``type="merge"`` was reverted (see that
           function's docstring): each of these three shares its type with
           a headless counterpart (``work`` with headless coding workers,
           ``review`` with headless auto-review, ``smoke`` with headless
           smoke). A matching row defers to
           ``attention_threshold_for("conflict-fix")`` — the same "human
           attending a live session, not a worker silently converging"
           scenario that earned conflict-fix its extra headroom in #1133 —
           so an explicit user override of ``conflict-fix`` (but *not* of
           plain ``work``/``review``/``smoke``) still applies. Checked
           *before* the plain ``attention_thresholds`` lookup below for the
           same reason :data:`INTERACTIVE_SESSION_TYPES` is:
           ``attention_thresholds`` always carries built-in ``"work"``/
           ``"review"``/``"smoke"`` entries (the dataclass default copies
           the whole ``_DEFAULT_ATTENTION_THRESHOLDS`` dict), so checking
           that dict first would make this branch unreachable.
        2. **Explicit override** — an ``attention_thresholds`` entry for
           *this exact* ``assignment_type`` (built-in default or
           user-configured) always wins.
        3. **Interactive session type** (#1133,
           :data:`INTERACTIVE_SESSION_TYPES`) — human-attended
           chat/troubleshoot/review-style sessions with no
           headless-convergence concept — exempted (``inf``, never flagged)
           rather than inheriting a headless-worker threshold.
        4. **Fallback to this config's own ``"work"`` entry** (so a user who
           only overrides ``work`` gets that value applied to unlisted
           *headless* types too, not the hardcoded default) — and only
           reaches for the hardcoded default when even ``"work"`` was never
           configured. This fallback is deliberately scoped to headless
           types by the ``INTERACTIVE_SESSION_TYPES`` check above it: unlike
           an unlisted headless type (probably work-like), an unlisted
           interactive type has no wall-clock-stuck concept at all, so
           silently reusing ``"work"``'s threshold for it would be a
           category error, not a reasonable guess.
        """
        if (
            assignment_type in ("work", "review", "smoke")
            and provider_name == "claude-pty"
            and review_of_assignment_id is not None
        ):
            return self.attention_threshold_for("conflict-fix")
        if assignment_type in self.attention_thresholds:
            return self.attention_thresholds[assignment_type]
        if assignment_type in INTERACTIVE_SESSION_TYPES:
            return float("inf")
        return self.attention_thresholds.get(
            "work", _DEFAULT_ATTENTION_THRESHOLDS["work"]
        )

    def stall_threshold_secs(
        self, active_types: Iterable[str], *, default_secs: float
    ) -> float:
        """Seconds of no board-fingerprint-change before ``coord drive``
        nudges a stall, for a stage whose currently-active assignment types
        are *active_types* (``IssueState.active_types`` — usually one type,
        occasionally several when a Test-stage child overlaps its parent).

        A type present in ``stall_thresholds`` (built-in default or
        user-configured — see :data:`_DEFAULT_STALL_THRESHOLDS`) always
        wins over *default_secs*. When more than one active type has an
        entry, the LARGEST wins — a stage is not stalled while ANY of its
        concurrently-active types would still call it normal, so nudging on
        the smaller of two thresholds would defeat the point of the larger
        one. A type with no entry — including ``active_types`` being empty,
        e.g. no assignment is running and the board simply has not moved —
        falls back to *default_secs*, the caller's own ``--stall`` value:
        unlisted types keep exactly today's behaviour.
        """
        candidates = [
            self.stall_thresholds[t] for t in active_types if t in self.stall_thresholds
        ]
        if candidates:
            return max(candidates)
        return default_secs

    def tracked_labels(self) -> list[str]:
        """Return the GitHub issue labels considered part of the pipeline.

        Always includes ``'coord'`` so normal coordinator-tagged issues appear
        in the pipeline panel regardless of per-label gate configuration.
        Additional labels come from the ``labels`` dict keys, sorted for
        stable ordering.
        """
        if not self.labels:
            return ["coord"]
        keys = sorted(self.labels.keys())
        if "coord" not in keys:
            keys = ["coord"] + keys
        return keys

    def gates_for_label(self, label: str | None) -> list[str]:
        """Return the gate list for a specific label, falling back to defaults.

        ``label`` may be ``None`` (no matching tracked label found on the
        issue) — in that case the configured ``default_gates`` are returned.

        #2687: ``"uat"`` is a recognised gate name here alongside ``"test"``/
        ``"review"``/``"merge"`` — it may appear in ``default_gates`` or any
        label's gate list like any other gate. Ordered between ``"review"``
        and ``"merge"`` when present. Unlike the other three, its
        *enforcement* additionally requires the specific repo to have
        ``Repo.uat_preview`` configured (see
        ``coord.merge_queue.requires_uat``) — so listing ``"uat"`` here is
        necessary but not sufficient; it is the fleet-wide half of a
        two-part per-repo opt-in.
        """
        if label and label in self.labels:
            return list(self.labels[label])
        return list(self.default_gates)

    def test_precedes_review(self) -> bool:
        """True when the ``test`` gate is ordered *before* ``review`` in the
        default gate list — i.e. the smoke/test verdict gates review dispatch
        (Work → Test → Review), rather than gating only the merge.

        When both gates are present and ``test`` comes first, automatic review
        dispatch waits for a ``passed``/``skipped`` test verdict (see
        ``coord.review.dispatch_pending_reviews``); when ``review`` comes first
        (or either gate is absent) review fires on work completion as before.
        Consulted on the *default* policy only — this governs headless
        review *dispatch* timing (``coord.review.dispatch_pending_reviews``),
        which does not consult per-label overrides. This differs from the
        merge gate's ``requires_smoke``/``requires_review`` (`coord/
        merge_queue.py`), which *do* honour a work item's resolved
        ``required_gates`` (falling back to this default list) since #1213 —
        so a ``["merge"]``-only label can bypass the merge-time review/test
        gates even though a review may still be auto-dispatched under the
        default policy here.
        """
        gates = self.default_gates or []
        if "test" not in gates or "review" not in gates:
            return False
        return gates.index("test") < gates.index("review")


@dataclass
class MergeConfig:
    """Merge behaviour configuration.

    ``auto_drain`` enables automatic draining of READY merge-queue entries on
    each daemon passive tick.  **Default-off** — with no ``merge:`` block in
    ``coordinator.yml`` the daemon never merges automatically and existing
    behaviour is unchanged.

    When enabled, after the enqueue step in ``_tick_loop`` the daemon calls
    :func:`coord.serve_app._auto_drain_tick`, which evaluates the plan
    (review + smoke + CI gates) and merges exactly the entries marked
    ``READY``, in true ``sequence()`` order.  ``BLOCKED`` and terminal
    entries are never touched.  Every auto-merge is logged so the operator
    can audit what drained (#781).

    Set ``max_per_tick`` to cap how many merges the daemon may perform in a
    single tick (default ``0`` = unlimited).

    ``sibling_overlap_aging_hours`` (#920) gates
    :func:`coord.merge_queue.find_sibling_overlaps` — the "these approved
    branches will conflict if merged out of order or late" warning shown by
    ``coord status`` / ``coord merge --plan``. It's the number of hours the
    oldest entry in a file-overlapping cluster of approved (PENDING) queue
    entries must have been waiting before the warning fires. ``0`` disables
    the warning entirely. Default ``24.0``.

    ``auto_revalidate`` (#2829) lets the daemon's own tick resolve a
    stale-but-``passed`` verdict the same way an operator does by hand with
    ``coord merge --revalidate``: compose the candidate(s) onto the current
    base, run the suite once, merge on green. **Default-off**, and a
    strictly larger grant than ``auto_drain`` — it *starts test runs*, not
    just merges pre-approved ones — so it gets its own sibling trust bar
    (tracked in ``docs/MERGE_AUTO_DRAIN_TRUST_BAR.md``) rather than riding
    in on ``auto_drain``'s track record. See
    :func:`coord.serve_app._auto_revalidate_tick` for the lock-hold
    restructure this required: the composite runs OUTSIDE ``_merge_lock``
    and the lock is taken only to re-check the base hasn't moved and then
    merge, so an unattended composite (up to
    ``coord.revalidate.DEFAULT_TIMEOUT_SECONDS`` × ``1 +
    auto_revalidate_max_batch`` worst case) never wedges the whole fleet's
    merge lane the way running it inside the lock would.

    ``auto_revalidate_max_batch`` caps how many candidates one unattended
    composite may validate together — deliberately well under
    :data:`coord.revalidate.MAX_REVALIDATION_BATCH` (10), since a RED
    composite's 1+N solo-run fallback is what actually holds the lock once
    the merge step runs, and that cost must stay small when nobody is
    watching it happen. Default ``3``. Combined with the "at most one
    composite per tick, never a burst" rule ``_auto_revalidate_tick``
    enforces in code (not a config knob — there is nothing to tune), this
    is the per-tick ceiling the trust bar calls for, analogous to
    ``auto_drain``'s ``max_per_tick``.
    """

    auto_drain: bool = False
    max_per_tick: int = 0
    auto_reap_merged: bool = True
    sibling_overlap_aging_hours: float = 24.0
    auto_revalidate: bool = False
    auto_revalidate_max_batch: int = 3


@dataclass
class PropagationConfig:
    """``propagation:`` — the #2583 min-releases-behind auto-roll gate.

    ``min_releases_behind`` holds `coord release propagate` and `coord
    release nightly-window` as a REPORTED no-op (see each command's own
    ``--min-behind`` flag, which overrides this per-invocation) until the
    fleet has fallen at least this many releases behind PyPI's latest —
    counted the same way ``coord health``'s ``agent_version`` check counts
    it (:func:`coord.health.pypi.releases_behind`), never a second
    version-comparison path.

    Default **1**: any delta at all is enough, which is exactly today's
    behaviour — an absent ``propagation:`` block (or an explicit
    ``min_releases_behind: 1``) changes nothing. Raising it is opt-in, and
    deliberately not done by flipping this default: see
    ``docs/AGENT_OPERATIONS.md``'s "Auto-roll threshold gate" section for
    why the *current* fleet must take one manual roll onto a fixed
    propagation lane before this is ever set above 1 — the old, buggy lane
    is what is running until that happens, and raising the threshold first
    would aim it at the largest delta it has ever seen.
    """

    min_releases_behind: int = 1


@dataclass
class MilestoneConfig:
    """Milestone-driven-workflow configuration (#767 / #769 Phase 1).

    ``auto_dispatch`` enables the daemon's tick loop to keep draining a
    milestone's declared work order after ``coord milestone dispatch``
    registers it: as issues reach a merged/terminal state, the newly-
    unblocked ready frontier is recomputed and dispatched automatically —
    no further human approval per issue, since the *declared* work order
    (the `## Work order` block) was the one-time approval unit.
    **Default-off** — with no ``milestone:`` block in ``coordinator.yml``
    the daemon never auto-dispatches and existing behaviour is unchanged;
    `coord milestone dispatch` still works as a one-shot manual drain.

    When enabled, :func:`coord.serve_app._milestone_drain_tick` runs on
    each daemon tick (after the reconcile step) for every milestone
    registered via a non-dry-run `coord milestone dispatch` call, and
    deregisters a milestone once its whole work order reaches a terminal
    state.

    Editing this wiring requires a **daemon restart** to take effect — the
    tick loop's closures are captured at ``coord serve`` startup time.
    """

    auto_dispatch: bool = False


@dataclass
class CiStoreConfig:
    """Backend selection for CI check visibility (#240).

    ``type`` is one of ``github`` (shell out to ``gh pr checks``),
    ``gitlab`` (#1897: GitLab Pipelines API via ``httpx``), or ``none``
    (always-empty :class:`coord.ci_store.NoOpCi`).  When the block is absent
    we default to ``github`` since it's a no-op upgrade for users who
    already have ``gh`` configured.  Future backends (Buildkite) add new
    ``type`` values without breaking existing configs.

    ``host``/``token_env`` are consumed only by ``type: gitlab`` — see
    :class:`coord.ci_gitlab.GitLabCi`. ``token_env`` names an environment
    variable holding a GitLab personal/project access token; the token
    itself is deliberately never accepted in this config (coordinator.yml
    is checked in / shared across machines, an env var is not).
    """

    type: str = "github"
    host: str = "gitlab.com"
    token_env: str = "GITLAB_TOKEN"


@dataclass
class StoreConfig:
    """``store:`` block (#827) — which database backend ``coord.db``/
    ``coord.dao`` open their connections against, via ``coord.sql``'s
    dialect-aware connection factory (``coord.sql.connect``).

    Absent block == ``backend: sqlite`` == today's behaviour, byte-for-byte:
    ``coord.db`` resolves its own on-disk path (``DB_PATH`` —
    ``~/.coord/coord.db`` by default, see ``coord/platform_paths.py``)
    completely independently of this config, so leaving this block out (or
    writing it with ``backend: sqlite`` explicitly) changes nothing about
    where an existing deployment's data lives or how it's opened.

    ``dsn`` is required, and only consulted, when ``backend: postgres`` — a
    libpq-style connection string (e.g.
    ``"postgresql://user:pass@host:5432/dbname"``) passed straight to
    ``psycopg`` (an optional dependency not declared anywhere in
    ``pyproject.toml`` — see ``coord/sql.py``'s ``row_factory_for`` — so a
    deployment that never sets ``backend: postgres`` never needs it
    installed).

    This is deliberately a **server-side-only** choice
    (``docs/STORE_SERVICE.md`` §4 — "Why a server-side feature flag is the
    wrong mechanism" carves out exactly this one exception): which storage
    engine is live is a deployment property, not something an individual
    client negotiates per request, so every machine pointed at the same
    ``coord serve`` daemon's database must set the same ``store:`` block (or
    none). Nothing in this repo currently cross-checks that across machines
    (#829 territory).

    Setting this block chooses the connection target only — it says nothing
    about whether the schema has actually been migrated to Postgres (#828)
    or whether any machine in the fleet is pointed at one yet (#829).
    ``backend: postgres`` with no real server behind ``dsn`` just makes every
    DB open fail loudly at connect time, the same as a bad DSN always would;
    this block does not make Postgres "live" by itself.
    """

    backend: str = DIALECT_SQLITE
    dsn: str | None = None


@dataclass
class AuditConfig:
    """``audit:`` block (#1036/#1038) — the append-only ``audit_log``
    table's tunables.

    ``max_rows`` is a future retention cap, not a pruning sweep: when set
    above the default ``0`` (unlimited), :func:`coord.audit.record_audit`
    opportunistically deletes the oldest rows past that count after every
    insert.  ``0`` means keep everything forever — the default for this
    milestone, since retention policy is explicitly out of scope (see the
    issue's "Out of scope" section).

    ``level`` (#1038) selects how much of the audit taxonomy is captured:
    ``"business"`` records only real board transitions (dispatch, verdicts,
    merge, ...); ``"operational"`` (the default) additionally records the
    daemon-tick's autonomic actions (passive reconcile, merge-queue
    enqueue/drain, conflict-fix dispatch, housekeeping sweeps) tagged
    ``tier="operational"``, ``actor="daemon"``.  Business-tier rows are
    always recorded regardless of ``level`` — this only gates the
    operational tier.
    """

    max_rows: int = 0
    level: str = "operational"


@dataclass
class ModelRates:
    """Per-1M-token USD rates for one canonical model (#1118 ``pricing:`` block).

    Consumed by :mod:`coord.usage_rollup`'s cost estimator for legs that have
    no captured ``cost_usd``. All four fields default to ``0.0`` so a
    partially-specified override (e.g. only ``input``) still produces a
    valid (if incomplete) rate rather than raising.
    """

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_creation: float = 0.0


def _default_pricing() -> dict[str, ModelRates]:
    """Built-in per-1M-token rates for the four canonical model tiers.

    Official Anthropic list pricing at time of writing (Sonnet/Opus/Haiku/
    Fable input+output list price; cache_read = 0.1x input, cache_creation =
    1.25x input, the standard 5-minute-TTL cache economics) — pinned exactly
    by ``test_pricing_absent_defaults_to_builtin_rates`` in
    ``tests/test_config_pricing.py`` so it can't silently drift again. A
    ``pricing:`` block in coordinator.yml overrides or extends any of these.

    #1290: the Opus row previously carried ``15.00/75.00`` (Opus 3 / 4.0 /
    4.1 era pricing, mistakenly pinned as "verified" at #1118 review).
    Current Opus (4.6 through 4.8) list price is ``5.00/25.00`` — corrected
    here. Sonnet and Haiku were already correct.
    """
    return {
        "sonnet": ModelRates(input=3.00, output=15.00, cache_read=0.30, cache_creation=3.75),
        "opus": ModelRates(input=5.00, output=25.00, cache_read=0.50, cache_creation=6.25),
        "haiku": ModelRates(input=1.00, output=5.00, cache_read=0.10, cache_creation=1.25),
        "fable": ModelRates(input=10.00, output=50.00, cache_read=1.00, cache_creation=12.50),
    }


@dataclass
class PricingConfig:
    """``pricing:`` block (#1118) — per-canonical-model per-1M-token USD rates.

    ``models`` maps a canonical model key (``"sonnet"``, ``"opus"``,
    ``"haiku"``, or any operator-added key) to its :class:`ModelRates`. An
    absent ``pricing:`` block in coordinator.yml still yields the built-in
    defaults via :func:`_default_pricing`. A model key with no entry here
    (e.g. ``"(unknown)"``, or a genuinely unrecognized model string) has no
    rate — :mod:`coord.usage_rollup` treats that as "no estimate possible"
    and flags the group rather than silently reporting $0.
    """

    models: dict[str, ModelRates] = field(default_factory=_default_pricing)

    def rates_for(self, canonical_model: str) -> ModelRates | None:
        """Look up rates for a canonical model key, or ``None`` if unpriced."""
        return self.models.get(canonical_model)


@dataclass
class ProviderDef:
    """Definition of a single named worker-command provider.

    Corresponds to one entry under ``providers.definitions`` in
    ``coordinator.yml``.  All fields except ``type`` are optional.

    Attributes:
        type: Provider backend type.  Currently supported values are
            ``"claude"`` (legacy ``claude -p`` stream-json worker, the
            default) and ``"claude-pty"`` (interactive ``claude`` spawned
            inside a PTY for subscription-billed runs — see #425).  The
            authoritative list of registered backends is built by
            :func:`coord.providers.build_provider`.
        binary: Override the worker binary path/name.  ``None`` means the
            provider uses its own default (``"claude"`` for the claude
            backend).
        model: Pin this provider to a specific model id or alias.  Used as
            the ``--model`` fallback in the provider's ``build_command``
            when neither an explicit per-call ``resolved_model`` nor
            ``AssignmentSpec.model`` is set. For definitions whose ``type``
            is **not** ``"claude"``/``"claude-pty"`` (e.g. ``"opencode"``),
            ``coord/dispatch.py``'s model resolution is provider-aware
            (#1706 review fix): when a dispatch has no explicit ``--model``
            and no label-routed model, and the effective provider's
            definition pins a ``model`` here, ``models.default`` is *not*
            applied and ``AssignmentSpec.model`` is left unset — so this
            field wins for the common "pin opencode to a model once" case.
            For definitions whose ``type`` IS ``claude``/``claude-pty``
            (regardless of what name they're registered under — see the
            ``fast-claude`` example in ``coordinator.example.yml``),
            ``models.default`` still always wins over this field when no
            explicit override is given, because ``AssignmentSpec.model``
            for those backends must go through ``models.resolve()``'s
            alias -> exact-id translation (``models.versions``), which a
            raw ``model`` value here would bypass.
        attach_url: Reserved for future attach-mode providers.
        env: Extra environment variables for the worker subprocess.
            Values may contain ``${VAR}`` placeholders which are expanded
            from :data:`os.environ` at parse time.
        extra_args: Additional command-line arguments appended to the
            worker argv.
    """

    type: str
    binary: str | None = None
    model: str | None = None
    attach_url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class ProvidersConfig:
    """Global provider registry.

    Parsed from the optional ``providers:`` block in ``coordinator.yml``.
    When the block is absent, ``default == "claude"`` and an implicit
    ``"claude"`` definition is present in ``definitions``.

    Attributes:
        default: The provider name used when no per-spec, per-label, or
            per-repo override is set.  Defaults to ``"claude"``.
        definitions: Named provider definitions keyed by provider name.
            An implicit ``"claude"`` entry is always materialised if absent.
        labels: Per-issue-label provider override (#1889), e.g.
            ``{"harness:opencode": "opencode"}`` — mirrors
            :attr:`ModelsConfig.labels`' shape and precedent, including its
            provenance reporting (:meth:`model_for_labels_with_reason`
            here becomes :meth:`provider_for_labels_with_reason`). Resolved
            via :func:`coord.providers.resolve_provider_name`'s
            ``issue_labels`` param, slotted into the precedence chain
            between *spec_provider* and *repo_provider* — see that
            function's docstring for the full chain. Values are validated
            against ``definitions`` at parse time in ``_parse_providers``
            (mirrors ``reviews.provider``, #1811): an unknown provider name
            here is a config-load error, not a dispatch-time surprise
            discovered at 2am. Every dispatch site gates this to
            ``type="work"`` proposals only, the same restriction
            ``models.labels`` uses (#1430) — plan/review/smoke dispatches
            must not inherit a harness-eval label meant for the eventual
            work dispatch.
    """

    default: str = "claude"
    definitions: dict[str, ProviderDef] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Always ensure the implicit "claude" definition exists so callers
        # can look it up by name without checking for its presence.
        if "claude" not in self.definitions:
            self.definitions["claude"] = ProviderDef(type="claude")

    def provider_for_labels(self, issue_labels: list[str]) -> str | None:
        """Resolve an issue's GitHub labels to a provider name via ``labels``.

        #1889: mirrors :meth:`ModelsConfig.model_for_labels` — see
        :meth:`provider_for_labels_with_reason` for the full precedence
        rule. Returns ``None`` (never *default* or *repo_provider*) when no
        configured label is present on the issue, or ``labels`` itself is
        empty. Callers are expected to fall back to *repo_provider*/
        ``default`` themselves (:func:`coord.providers.resolve_provider_name`
        does this).
        """
        return self.provider_for_labels_with_reason(issue_labels)[0]

    def provider_for_labels_with_reason(
        self, issue_labels: list[str]
    ) -> tuple[str | None, str | None, list[str]]:
        """Like :meth:`provider_for_labels`, but also returns the label that
        matched and any configured labels it shadowed.

        Returns ``(provider, matched_label, shadowed_labels)``, or
        ``(None, None, [])`` under the same conditions
        :meth:`provider_for_labels` returns ``None``.

        #1889: mirrors :meth:`ModelsConfig.model_for_labels_with_reason`
        (#1633)'s provenance shape, minus the ``tier:*`` grouping —
        ``providers.labels`` has no size-tier concept, so precedence among
        several configured labels present on the same issue (e.g. both
        ``harness:opencode`` and ``harness:claude``) is simply ``labels``'s
        own declaration order in ``coordinator.yml``, exactly like
        ``models.labels``' non-tier group. Ties are NOT broken by the
        issue's own label order (GitHub-controlled, not config-controlled)
        for the same reason #1633 fixed that for models: the same issue +
        config must always resolve to the same provider, regardless of
        what order GitHub reports the issue's labels in.
        """
        if not self.labels:
            return None, None, []
        present_in_config_order = [
            label for label in self.labels if label in issue_labels
        ]
        if not present_in_config_order:
            return None, None, []
        matched = present_in_config_order[0]
        shadowed = present_in_config_order[1:]
        return self.labels[matched], matched, shadowed


# #1711: provider-availability capability vocabulary.
#
# A machine advertises support for a given provider *backend type* the same
# way it advertises "rust"/"gtk"/"browser" — one more string in
# `machines[].capabilities`, e.g. `"provider:opencode"`. A dedicated
# `Machine.provider` field was considered and rejected: provider and machine
# are already orthogonal in this data model (a machine has no provider
# opinion of its own — `Repo.provider`/`providers.default` decide that), and
# "can this machine run backend X" is exactly the shape `smoke_tests.
# capability_rules` and `coord.prereqs` already solve for "rust"/"gtk"/
# "browser". Reusing that machinery (rather than inventing a parallel
# provider-routing concept) means `coord doctor`'s declared-vs-probed report
# and the `coordinator.yml` capability list both fall out for free.
#
# Keyed off `ProviderDef.type` (not the definition's arbitrary registered
# NAME) because type is what actually determines which binary a machine
# needs installed. An operator-named alias of a claude-backed provider (e.g.
# `fast-claude` in `coordinator.example.yml`, `type: claude`) never needs a
# capability declared — it still just runs the `claude` CLI. Only a
# genuinely different backend TYPE (today: `opencode`) needs one.
#
# `claude` and `claude-pty` are the IMPLICIT baseline: every machine is
# assumed to already have the `claude` CLI (this predates #1711, and
# probing for the `claude` binary itself is out of this issue's scope —
# see its non-goals), so neither needs a capability declared. This is what
# keeps every existing no-``providers:``-block deployment unaffected.
IMPLICIT_PROVIDER_TYPES: frozenset[str] = frozenset({"claude", "claude-pty"})


def provider_capability(provider_type: str) -> str:
    """The ``capabilities:`` string a machine advertises to declare it can
    run *provider_type* (#1711).

    ``provider_capability("opencode") == "provider:opencode"``. Single
    source of truth for the naming convention — every caller that declares,
    checks, or probes provider availability (`coord doctor`'s prereq
    manifest, `coord.providers.guard_provider_machine_capability`, `coord
    plan`'s proposal filter) calls this rather than hand-formatting the
    ``"provider:" + name`` string, so the convention can't drift between
    call sites.
    """
    return f"provider:{provider_type}"


def model_plausible_for_provider_type(model: str, provider_type: str) -> bool:
    """Namespace-shape sanity check: could *model* plausibly belong to
    *provider_type* (#1798)?

    A cheap, syntax-only heuristic — NOT a live catalog lookup (no network
    call, no per-provider model list to keep in sync). Every model
    identifier this coordinator actually resolves falls into one of two
    shapes, mirroring the two namespaces ``coord.dispatch.
    resolve_dispatch_model_alias`` already reasons about:

    * Claude aliases (``sonnet``, ``opus``, ``haiku``) and exact ids
      resolved via ``models.versions`` (``claude-sonnet-4-6``) — never
      contain a ``/``.
    * OpenCode Zen model strings are always ``provider/model``
      (``opencode/glm-5.2``, ``deepseek/deepseek-chat`` — see
      ``docs/OPENCODE_VERIFICATION.md``) — always contain a ``/``.

    That's enough signal to catch #1798's actual failure mode — a Claude
    alias handed to an opencode-type backend (or vice versa) — before a
    dispatch spends a worktree and a network round-trip discovering the
    backend rejects it mid-run. ``claude``/``claude-pty``
    (:data:`IMPLICIT_PROVIDER_TYPES`) require NO ``/``; every other
    (non-implicit) provider type requires one.
    """
    has_namespace = "/" in model
    if provider_type in IMPLICIT_PROVIDER_TYPES:
        return not has_namespace
    return has_namespace


# #1628: default disk mount points the health engine probes.  "/" and "/home"
# are usually the two that matter (and were the two that mattered on
# 2026-07-30 — elitebook's /home hit 0 bytes free); "~/.coord" is separate
# because on some layouts the coordinator state dir is its own filesystem.
# Probing the same *device* twice is deduped at run time, so a machine with
# one big root filesystem still reports one line.
_DEFAULT_HEALTH_DISK_PATHS = ("/", "/home", "~/.coord")

# The systemd *user* units `spawned_coord` (#1834) introspects. Duplicated
# from `coord.health.checks.spawned_coord.DEFAULT_UNITS` rather than imported,
# same reason as AGENT_PORT in coord/commands/_common.py: config must not
# import the check registry (probes import config, not the other way round).
# tests/test_release_verify.py pins the two lists together.
_DEFAULT_SPAWNED_COORD_UNITS = (
    "coord-serve",
    "coord-agent",
    "coord-web",
    "coord-drive-queue",
    "coord-notify",
)


@dataclass
class HealthConfig:
    """Thresholds for the fleet-health check engine (#1628, ``coord health``).

    Every number here is a *default that would have caught the 2026-07-30
    incidents* — see ``tests/test_health_incident_regression.py``, which
    replays the recorded values against these defaults.  Loosening one is
    therefore a decision to stop catching a class of failure that has
    already happened once, not a tuning preference; the regression test is
    there to make that trade explicit rather than accidental.

    Disk thresholds are expressed as **percent free remaining** (headroom),
    not percent used, because headroom is the question the engine answers.
    """

    # Master switch.  False makes `coord health` report nothing rather than
    # being removed from the CLI — a disabled check must still be visible.
    enabled: bool = True
    # Check ids to skip, e.g. ["plan_usage"] on a machine with no OAuth login.
    disabled_checks: list[str] = field(default_factory=list)

    # ── disk free ─────────────────────────────────────────────────────────
    disk_paths: list[str] = field(
        default_factory=lambda: list(_DEFAULT_HEALTH_DISK_PATHS)
    )
    disk_warn_free_pct: float = 15.0
    disk_crit_free_pct: float = 7.0

    # ── cargo target/ total across known dirs ─────────────────────────────
    cargo_target_warn_gb: float = 40.0
    cargo_target_crit_gb: float = 60.0
    # Extra target dirs to total beyond the shared per-machine cache and each
    # known checkout's own target/.
    cargo_target_extra_dirs: list[str] = field(default_factory=list)
    # Walking a 78G target dir is not free.  The probe totals what it can in
    # this many seconds and reports a partial scan rather than blowing the
    # ~2s registry budget; a partial total is a *lower bound*, so a CRIT
    # derived from one is still trustworthy.
    cargo_scan_budget_secs: float = 1.5

    # ── stale worktrees under ~/.coord/worktrees ──────────────────────────
    # "Stale" here is deliberately DB-free (see the probe's docstring): a
    # worktree directory untouched for this long.  Counting live assignments
    # would need board state, which is H-3's job, not this child's.
    worktree_stale_hours: float = 48.0
    worktree_warn_count: int = 3
    worktree_crit_count: int = 10

    # ── stale .git/index.lock in a known checkout (#2206) ──────────────────
    # A few minutes is generous: real git index operations are sub-second,
    # so a lock this old is either a live operation on an enormous repo (rare
    # and worth a look anyway) or, far more often, a killed git process that
    # will never clean up after itself.  A lock younger than this is never
    # flagged regardless of holder; a lock held by a live process is never
    # flagged regardless of age.
    index_lock_stale_minutes: float = 10.0

    # ── agent install ─────────────────────────────────────────────────────
    # Absolute path to the agent venv's python.  None → autodetect
    # (~/.coord-venv/bin/python3, else the running interpreter).
    agent_venv_python: str | None = None
    agent_version_warn_behind: int = 1
    agent_version_crit_behind: int = 2
    # The *simple index*, not the JSON API: they flip independently in both
    # directions and only the simple index is what pip actually resolves
    # against, so a JSON-API answer can say "current" while pip disagrees.
    pypi_index_url: str = "https://pypi.org/simple"
    network_timeout_secs: float = 3.0

    # ── graphify graph freshness ──────────────────────────────────────────
    # A stale graph whose checkout has hooks disabled is CRIT regardless of
    # age: it structurally cannot self-heal, so time only makes it worse.
    graph_stale_warn_hours: float = 24.0
    graph_stale_crit_hours: float = 72.0

    # ── Max-plan usage windows (wraps coord.usage_limits) ─────────────────
    plan_usage_warn_pct: float = 85.0
    plan_usage_crit_pct: float = 95.0

    # ── fleet deploy lanes, daemon host only (#1630) ──────────────────────
    # These three name the two deploy lanes that live on the *daemon* host
    # rather than on an agent: the operator's CLI venv, and the locally-built
    # coord-tui binary.  All three follow `agent_venv_python`'s convention —
    # ``None`` means "use the documented default location", NOT "disable the
    # lane" — so the lanes are live on a stock install with no config at all,
    # and an operator only sets them when their layout differs.
    #
    # Absolute path to the operator CLI venv's python.  None →
    # ~/.coord-cli-venv/bin/python3 (what the install docs create).  This lane
    # exists because it was found three releases stale on 2026-07-29.
    cli_venv_python: str | None = None
    # Absolute path to the built coord-tui binary.  None → ~/.local/bin/coord-tui
    # (coord-tui's README: `cargo build && cp target/debug/coord-tui
    # ~/.local/bin/coord-tui`).
    tui_binary_path: str | None = None
    # Directory holding the coord-tui Rust sources the binary was built from
    # (#2899; before that split the crate lived at `tui/` inside THIS repo).
    # None → `<checkout>/src` for this machine's `coord-tui` checkout,
    # discovered the same way `coord_tui_checkout` is (see
    # `coord.health.checks.deploy_lane_facts.resolve_coord_tui_checkout`),
    # falling back to the pre-split `<checkout>/tui/src`.
    # Deliberately points at `src/`, not the crate root: rooting the mtime walk
    # above `target/` would sweep a multi-GB build dir on every refresh.
    tui_source_dir: str | None = None
    # Root of a local `coord-tui` checkout (#2899). None → discover it from
    # `repo_paths`/`ctx.checkouts` (a checkout named `coord-tui`, else one
    # carrying `src/app/data.rs`).  Same convention as the lanes above: None
    # means "discover it", never "disable the lane".
    coord_tui_checkout: str | None = None
    # Absolute path to the live `coord web --dist` bundle (#1834 lane 5).
    # None → ~/coord-web-dist — the symlink `deploy/coord-web-dist-build.timer`
    # atomically repoints at each new release (#1543).
    webapp_dist_path: str | None = None
    # Directory holding the `coord-web` sources the bundle was built from
    # (#2470; before epic #2002 split the webapp out into its own repo, this
    # was `coord/dashboard/webapp/` inside THIS repo).  None → `<checkout>/src`
    # for this machine's `coord-web` checkout, discovered the same way
    # `coord_web_checkout` is (see
    # `coord.health.checks.coord_web_ci_pin.resolve_coord_web_checkout`).
    # Same `src/`-not-root reasoning as `tui_source_dir`: rooting at the
    # webapp package root would sweep `node_modules`/`dist` were they not
    # already skipped by name.
    webapp_source_dir: str | None = None
    # Absolute path to the heartbeat coord-web-dist-build.sh writes on EVERY
    # invocation, whether or not there was anything to build (#2122). None →
    # ~/.coord-web-releases/.last-run-at — the sibling of $BLOCKED_SHA_FILE
    # in that same script. This is what lets `webapp_build_heartbeat`
    # distinguish "up to date" from "has not run since <time>": the timer
    # deliberately stopped logging its no-op tick to keep the journal quiet,
    # so this file is the only remaining proof of a live trigger.
    webapp_build_heartbeat_path: str | None = None
    # Age (minutes) past which a stale heartbeat is worth a look — 3x
    # coord-web-dist-build.timer's 10-minute cadence (#2122), so one or two
    # missed/overlapping ticks (flock contention, a slow npm build) never
    # trips this; a timer that is actually disabled or wedged does.
    webapp_build_heartbeat_warn_minutes: float = 30.0
    # Age (minutes) past which the timer has almost certainly stopped firing
    # altogether, not just missed a tick or two — 3 hours, ~18 missed fires
    # in a row at the 10-minute cadence.
    webapp_build_heartbeat_crit_minutes: float = 180.0
    # Root of a local `coord-web` checkout, whose CI workflow YAML names the
    # `coord` this fleet's frontend is proven against (#2006, epic #2002).
    # None → discover it from `repo_paths`/`ctx.checkouts` (a checkout named
    # `coord-web`, else one carrying `playwright.acceptance.config.ts` at its
    # root).  Same convention as the lanes above: None means "discover it",
    # never "disable the lane" — see
    # `coord.health.checks.coord_web_ci_pin` and docs/ADR_COORD_WEB_CI.md.
    coord_web_checkout: str | None = None

    # ── systemd unit-file drift (#1831) ────────────────────────────────────
    # `deploy/*.service`/`*.timer` is version-controlled and reviewed but
    # nothing installs it — a unit hand-copied at machine setup drifts
    # forever from what's checked in. These two follow the same convention
    # as the deploy-lane paths above: None means "use the documented default
    # location", not "disable the check".
    #
    # The reference directory — a FALLBACK only, since #1927. The check now
    # diffs against `coord/deploy/` inside the installed distribution (the
    # released artifact for the running version, which cannot drift with
    # this host); both this setting and the checkout scan below apply only
    # when the installed wheel ships no units of its own, and whatever they
    # point at is reported as an unverified working copy.
    # None -> `<checkout>/deploy` for the first configured local checkout
    # that has one (normally the code-coordinator checkout in repo_paths).
    deploy_dir: str | None = None
    # Where systemd user units actually live. None -> ~/.config/systemd/user.
    systemd_user_dir: str | None = None

    # ── what a running service actually spawns (#1834) ────────────────────
    # The systemd user units whose LIVE process environment `spawned_coord`
    # reads to predict which `coord` binary their subprocesses will get.
    # Unlike the path-ish options above, an EMPTY list here really does mean
    # "off" — the unit names are the check's entire subject, so there is no
    # documented default to fall back to once they are cleared.
    spawned_coord_units: list[str] = field(
        default_factory=lambda: list(_DEFAULT_SPAWNED_COORD_UNITS)
    )


@dataclass
class NotificationsConfig:
    """The phone-push channel (#1632, ``coord notifier``).

    Fires when the pipeline **has stopped, or is stalled, and will not
    advance without a human** — not when "something bad happened". The
    auto-loop already handles failed tests, request-changes reviews and
    mechanical merge conflicts; pushing those is the noise that trains an
    operator to mute the channel. In normal operation this fires
    approximately never.

    ``enabled`` defaults to **False**: a coordinator that starts pushing to
    a phone the moment it is upgraded, without anyone asking, is a worse
    failure than one that stays silent. Every deployment with no
    ``notifications:`` block behaves exactly as it did before.

    The whole subsystem is advisory and isolated — an unreachable ntfy
    server must not affect dispatch, routing, the board or any verdict.
    That is enforced in :mod:`coord.notifier.transport` and
    :mod:`coord.notifier.service`, not here, but it is the reason none of
    these settings are consulted by anything on the dispatch path.
    """

    # Master switch. False makes the tick a no-op; the CLI stays available
    # so `coord notifier status` can still explain why nothing is arriving.
    enabled: bool = False

    # ── transport ─────────────────────────────────────────────────────────
    # "ntfy" (self-hosted, over Tailscale) or "none" (predicate runs, the
    # ledger updates, nothing is delivered — useful for shaking out false
    # positives before pointing it at a phone).
    transport: str = "ntfy"
    # Server root, e.g. "http://dellserver:7440". Nothing leaves the
    # tailnet: event text carries repo names, issue titles and failure
    # detail, which is exactly why the server is self-hosted.
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    # Optional; a tailnet-only server often has nothing to authenticate.
    ntfy_token: str | None = None
    timeout_secs: float = 5.0

    # Origin of the `coord web` PWA, e.g. "http://dellserver:7434".
    # Notifications must be actionable from a phone, so every event that
    # names an issue links straight to that issue's pipeline view.
    web_base_url: str | None = None

    # ── quiet hours ───────────────────────────────────────────────────────
    # A DEFERRAL window, not a filter: events raised inside it are held,
    # coalesced, and delivered as one digest when it closes. Nothing is
    # discarded. No severity level pierces it — the only exception is a
    # drive the operator explicitly marked `--urgent`, which is a deadline
    # rather than a severity, and which expires with that drive.
    quiet_hours: QuietHours | None = None
    # How long a `coord drive --urgent` opt-out lasts. Scoped and expiring
    # so a forgotten flag cannot make every future night loud.
    urgent_ttl_hours: float = 12.0

    # ── baselines ─────────────────────────────────────────────────────────
    # Under this many completed legs a stratum has NO baseline and falls
    # back to a generous absolute ceiling, which the notification text says
    # out loud. Never fire off a population of one.
    min_samples: int = 5
    # Which percentile of the stratified population is "far too long".
    # p90 is the #1632 proposal; 2x median is the documented alternative,
    # reported alongside it by `coord notifier baselines` so the two can be
    # compared against real fleet data before either is committed to.
    percentile: float = 90.0
    # Silence threshold as a fraction of the stratum's median leg (clamped
    # in coord.notifier.baseline). A repo whose test suite takes 20 minutes
    # legitimately goes quiet; a fixed value would either spam that repo or
    # never fire on a fast one.
    silence_fraction: float = 0.5
    # How long a stall must survive `drive`'s nudge before the notifier
    # believes it. `drive` owns the definition of "stalled" (#1593) — this
    # only says how long to wait for the nudge to work.
    stall_grace_mins: float = 20.0
    # Cold-start ceilings, MINUTES, keyed by assignment type. Merged over
    # coord.notifier.baseline.DEFAULT_COLD_CEILINGS.
    cold_ceiling_mins: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PortalProjectRepo:
    """One portal-project → coord-repo(s) mapping (ms-67 contract §2).

    ``project_id`` is the portal's own opaque identifier: coord never mints
    one and never validates its *shape*, only that it is non-empty and
    declared at most once. ``repos`` must each name a configured
    ``repos[].name`` — the same cross-reference posture
    :func:`_validate_dependencies` already applies to ``depends_on``.
    """

    project_id: str
    repos: list[str]


#: Every outbox row kind :mod:`coord.portal_sync` can enqueue. Duplicated
#: here as literals rather than imported from ``coord.portal_sync``'s
#: ``KIND_*`` constants: that module imports ``coord.portal_store`` which
#: imports ``coord.db``, and config must stay importable by everything.
#: ``tests/test_portal_config.py`` pins the two lists together so the copy
#: cannot silently drift.
PORTAL_OUTBOX_KINDS = (
    "status", "design_round", "question", "preview",
    # #2987: a relayed answer (#2986) pushed outbound for the client to
    # confirm/correct — see `coord.portal_sync.KIND_RELAYED_ANSWER`.
    "relayed_answer",
)

#: The draft gate's default policy (#2903, phase 1 of #2902): the two kinds
#: that carry **agent-authored prose to a customer** are held for an
#: operator; the two mechanical kinds are not.
#:
#: ``status`` and ``preview`` are deliberately ungated. A status is one of a
#: pinned vocabulary and a preview is a URL — neither is prose anybody would
#: read and rewrite, and gating them would produce a rubber-stamp queue of
#: "In progress" ×N that trains the operator to approve without reading,
#: which is the failure mode this gate exists to prevent (see epic #2902).
#: #2987: a relayed answer is prose an operator typed on the client's
#: behalf — "the most worth reading before it sends" (the issue's own
#: framing) — so it is gated like the other two prose kinds, not left
#: ungated like `status`/`preview`.
DEFAULT_PORTAL_APPROVAL: dict[str, bool] = {
    "status": False,
    "design_round": True,
    "question": True,
    "preview": False,
    "relayed_answer": True,
}


@dataclass
class PortalApprovalConfig:
    """Which outbox kinds land in ``draft`` instead of ``pending`` (#2903).

    An absent ``portal.approval`` block means :data:`DEFAULT_PORTAL_APPROVAL`
    exactly. A present block is **merged over** those defaults rather than
    replacing them, so ``approval: {question: false}`` still gates
    ``design_round`` — an operator relaxing one kind has not silently opened
    every other one.
    """

    kinds: dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_PORTAL_APPROVAL)
    )

    def gates(self, kind: str) -> bool:
        """True when *kind* must be operator-approved before it can be sent.

        An unknown kind is **not** gated: a kind this build has never heard
        of cannot be one of the two prose kinds the gate is for, and failing
        closed on it would wedge a queue on an unrecognised row nobody can
        approve (there is no CLI verb for a kind that does not exist).
        """
        return bool(self.kinds.get(kind, False))


@dataclass
class PortalConfig:
    """The outbound client to coord-portal's sync bridge (#2179, ``docs/CUSTOMER_PORTAL.md``).

    Absent block == disabled == coord never talks to the portal — unchanged
    behaviour for every existing deployment, and the correct default until
    ``BRIDGE_CLIENT_ID``/``BRIDGE_CLIENT_SECRET`` actually exist in production
    (they do not, as of #2179: ``wrangler secret list`` on the portal has no
    entry for either).

    The portal is a third party from coord's perspective — a push failure
    must be retried and surfaced, never fatal to a merge or a dispatch.
    Nothing in :mod:`coord.portal_bridge` sits on the dispatch path; it is a
    client other code calls, not a check anything blocks on.
    """

    enabled: bool = False
    # e.g. "https://intake.heurontech.com". No default: a client pointed at
    # nothing by accident is worse than one that refuses to start.
    base_url: str | None = None
    # The Cloudflare Access service-token pair coord-portal's
    # ``isBridgeAuthorized`` requires as a matched, non-empty pair (half a
    # credential is not a credential, and it fails closed on exactly that).
    # Support ``${VAR}`` expansion (mirrors ``providers.definitions[*].env``)
    # so the real secret lives in the environment, never committed in
    # coordinator.yml.
    bridge_client_id: str | None = None
    bridge_client_secret: str | None = None
    timeout_secs: float = 10.0
    # Retries are for transient/5xx failures only — see
    # coord.portal_bridge.PortalBridgeClient. A 401 never retries: a bad
    # credential does not become a good one on attempt two.
    max_retries: int = 2
    # ms-67 contract §2: portal project ↔ coord repo(s). Absent == nothing
    # mapped == every lookup returns [], which the "Approved work items"
    # panel renders as its literal "— no mapping —" placeholder rather than
    # a blank cell. Mapping is *operator* knowledge (which repo a client's
    # project lands in), so it lives here and not in the portal's schema.
    project_repos: list[PortalProjectRepo] = field(default_factory=list)
    # #2903 — the draft gate. Absent block == DEFAULT_PORTAL_APPROVAL ==
    # design_round/question held for an operator, status/preview straight
    # through exactly as before.
    approval: PortalApprovalConfig = field(default_factory=PortalApprovalConfig)

    def repos_for_project(self, project_id: str) -> list[str]:
        """The coord repo name(s) *project_id* maps to — ``[]`` if unmapped.

        **Never raises.** An unmapped project is a valid, common state (a
        brand-new portal project the operator has not routed yet), not an
        error: the caller renders "no mapping", it does not fail. Returns a
        fresh list so a caller cannot mutate the parsed config.
        """
        wanted = (project_id or "").strip()
        if not wanted:
            return []
        for entry in self.project_repos:
            if entry.project_id == wanted:
                return list(entry.repos)
        return []


@dataclass
class Config:
    repos: list[Repo]
    machines: list[Machine]
    hooks: HooksConfig = field(default_factory=HooksConfig)
    reviews: ReviewsConfig = field(default_factory=ReviewsConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    smoke_tests: SmokeTestsConfig = field(default_factory=SmokeTestsConfig)
    acceptance: AcceptanceConfig = field(default_factory=AcceptanceConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    usage_gate: UsageGateConfig = field(default_factory=UsageGateConfig)
    # #2131 — absent block == no ceiling == today's behaviour.
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    ci_store: CiStoreConfig = field(default_factory=CiStoreConfig)
    # #827 — absent block == backend="sqlite" == today's behaviour untouched.
    store: StoreConfig = field(default_factory=StoreConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    # #2583 — absent block == min_releases_behind=1 == today's behaviour
    # (any delta at all rolls, subject to quiescence/cordon as before).
    propagation: PropagationConfig = field(default_factory=PropagationConfig)
    milestone: MilestoneConfig = field(default_factory=MilestoneConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    # #1632 — absent block == disabled == today's behaviour (silence).
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    # #2179 — absent block == disabled == coord never talks to coord-portal.
    portal: PortalConfig = field(default_factory=PortalConfig)
    path: Path | None = None
    # #2783 — non-fatal parse-time warnings (e.g. an unrecognised repos[] key)
    # surfaced to an operator via `coord config` / `coord diagnose`. Never
    # gates loading — a warning is advisory, not a broken config.
    warnings: list[str] = field(default_factory=list)

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)


def load(path: str | Path | None = None) -> Config:
    """Load and validate a coordinator.yml file.

    When ``path`` is None the location is resolved via
    :func:`resolve_config_path` (``$COORD_CONFIG`` → ``~/.coord/coordinator.yml``
    → ``./coordinator.yml``), so the tool works on a machine without a repo
    checkout.
    """
    p = Path(path).expanduser() if path is not None else resolve_config_path()
    if not p.exists():
        raise ConfigError(
            f"Config file not found: {p}. Create it at {sys.modules[__name__].USER_CONFIG_PATH} "
            f"(recommended — works without a repo checkout), pass --config <path>, "
            f"or set $COORD_CONFIG."
        )

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {p}: {e}") from e

    if raw is None:
        raise ConfigError(f"Config file is empty: {p}")

    return parse_mapping(raw, path=p)


def parse_mapping(raw: Any, *, path: Path | None = None) -> Config:
    """Validate an already-decoded coordinator.yml *mapping* into a :class:`Config`.

    :func:`load` is this plus "read the YAML off disk first". Split out (#1538)
    so callers that already hold the mapping — notably the ``coord web
    --fixture`` seeded-board server, whose fixture JSON may carry an inline
    ``config`` block — get the identical parsing/validation instead of a
    second, drifting hand-rolled Config builder.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"Top-level config must be a mapping, got {type(raw).__name__}")

    p = path
    repos, repo_warnings = _parse_repos(raw.get("repos"))
    machines = _parse_machines(raw.get("machines"), repos)
    _validate_dependencies(repos)
    hooks = _parse_hooks(raw.get("hooks"))
    # #1811: providers parsed before reviews so `reviews.provider` can be
    # validated against `providers.definitions` at parse time — mirrors how
    # `reviews.repo_overrides` validates against `repo_names` above.
    providers = _parse_providers(raw.get("providers"))
    reviews = _parse_reviews(
        raw.get("reviews"), {r.name for r in repos}, set(providers.definitions),
    )
    concurrency = _parse_concurrency(raw.get("concurrency"))
    smoke_tests = _parse_smoke_tests(raw.get("smoke_tests"))
    acceptance = _parse_acceptance(raw.get("acceptance"))
    models = _parse_models(raw.get("models"))
    pipeline = _parse_pipeline(raw.get("pipeline"))
    dispatch = _parse_dispatch(raw.get("dispatch"))
    usage_gate = _parse_usage_gate(raw.get("usage_gate"))
    budget = _parse_budget(raw.get("budget"))
    ci_store = _parse_ci_store(raw.get("ci_store"))
    store = _parse_store(raw.get("store"))
    merge = _parse_merge(raw.get("merge"))
    propagation = _parse_propagation(raw.get("propagation"))
    milestone = _parse_milestone(raw.get("milestone"))
    audit = _parse_audit(raw.get("audit"))
    pricing = _parse_pricing(raw.get("pricing"))
    health = _parse_health(raw.get("health"))
    notifications = _parse_notifications(raw.get("notifications"))
    portal = _parse_portal(raw.get("portal"), {r.name for r in repos})

    return Config(
        repos=repos,
        machines=machines,
        hooks=hooks,
        reviews=reviews,
        concurrency=concurrency,
        smoke_tests=smoke_tests,
        acceptance=acceptance,
        models=models,
        pipeline=pipeline,
        dispatch=dispatch,
        usage_gate=usage_gate,
        budget=budget,
        ci_store=ci_store,
        store=store,
        merge=merge,
        propagation=propagation,
        milestone=milestone,
        providers=providers,
        audit=audit,
        pricing=pricing,
        health=health,
        notifications=notifications,
        portal=portal,
        path=p,
        warnings=repo_warnings,
    )


# #2783 — every key `_parse_repos` actually reads. Anything in a repos[] entry
# outside this set validates clean today (`entry.get(...)` silently returns
# None for a typo'd or dead key) but is read by no code in coord/ — see
# docs/ARCHITECTURE.md's `file_groups`/`exclusive_files` note for the
# motivating example. Kept as a warning, never an error: a forward-looking or
# hand-typed key is not a broken config, and every agent, the board daemon,
# and the drive-queue tick all load this same file.
_KNOWN_REPO_KEYS = frozenset(
    {
        "name",
        "github",
        "depends_on",
        "default_branch",
        "develop_branch",
        "build_command",
        "test_command",
        "ci_command",
        "run_cmd",
        "worker_permissions",
        "housekeeping",
        "coordinator_only_files",
        "reference_repos",
        "new_issue_guidance",
        "artifact_paths",
        "provider",
        "uat_preview",
    }
)


def _parse_repos(raw: Any) -> tuple[list[Repo], list[str]]:
    if raw is None:
        raise ConfigError("Config must define 'repos'")
    if not isinstance(raw, list):
        raise ConfigError("'repos' must be a list")
    if not raw:
        raise ConfigError("'repos' must contain at least one repo")

    repos: list[Repo] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"repos[{i}] must be a mapping, got {type(entry).__name__}")
        name = entry.get("name")
        github = entry.get("github")
        if not name or not isinstance(name, str):
            raise ConfigError(f"repos[{i}].name is required (string)")
        if not github or not isinstance(github, str):
            raise ConfigError(f"repos[{i}].github is required (string, 'owner/repo')")
        if "/" not in github:
            raise ConfigError(
                f"repos[{i}].github must be 'owner/repo', got {github!r}"
            )
        if name in seen:
            raise ConfigError(f"duplicate repo name: {name!r}")
        seen.add(name)

        unknown_keys = sorted(set(entry) - _KNOWN_REPO_KEYS)
        for key in unknown_keys:
            warnings.append(
                f"repos[{i}] ({name!r}): unrecognised key {key!r} — not read by "
                "any code in coord/ (see docs/ARCHITECTURE.md); check for a typo "
                "or drop it"
            )

        depends_on = entry.get("depends_on", []) or []
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            raise ConfigError(f"repos[{i}].depends_on must be a list of repo names")

        default_branch = entry.get("default_branch", "main")
        if not isinstance(default_branch, str):
            raise ConfigError(f"repos[{i}].default_branch must be a string")

        # #934: develop_branch — opt-in to the develop + feature-branch-
        # per-milestone git model (docs/PIPELINE_V2.md "Git model"). Absent
        # (None) by default so existing repos are unaffected.
        develop_branch = entry.get("develop_branch")
        if develop_branch is not None and not isinstance(develop_branch, str):
            raise ConfigError(f"repos[{i}].develop_branch must be a string")

        build_command = entry.get("build_command")
        if build_command is not None and not isinstance(build_command, str):
            raise ConfigError(f"repos[{i}].build_command must be a string")
        test_command = entry.get("test_command")
        if test_command is not None and not isinstance(test_command, str):
            raise ConfigError(f"repos[{i}].test_command must be a string")
        # #2091: ci_command — the command this repo's CI actually runs.  The
        # Test stage prefers it over `test_command` so a green Test verdict
        # is CI-equivalent instead of "some subset of CI passed".
        ci_command = entry.get("ci_command")
        if ci_command is not None and not isinstance(ci_command, str):
            raise ConfigError(f"repos[{i}].ci_command must be a string")
        if isinstance(ci_command, str) and not ci_command.strip():
            raise ConfigError(
                f"repos[{i}].ci_command must be a non-empty string (omit the "
                "key entirely to fall back to test_command)"
            )
        # #296: run_cmd — optional shell command to launch the app for manual
        # smoke testing.  Surfaced in the TUI Test stage detail panel.
        run_cmd = entry.get("run_cmd")
        if run_cmd is not None and not isinstance(run_cmd, str):
            raise ConfigError(f"repos[{i}].run_cmd must be a string")

        worker_permissions = _parse_worker_permissions(entry.get("worker_permissions"), i)

        housekeeping = entry.get("housekeeping", []) or []
        if not isinstance(housekeeping, list) or not all(isinstance(h, str) for h in housekeeping):
            raise ConfigError(f"repos[{i}].housekeeping must be a list of strings")

        coordinator_only_files = entry.get("coordinator_only_files", []) or []
        if not isinstance(coordinator_only_files, list) or not all(isinstance(f, str) for f in coordinator_only_files):
            raise ConfigError(f"repos[{i}].coordinator_only_files must be a list of strings")

        # #268: reference_repos — sibling repos a worker may reference
        # for context but doesn't actually build against.
        reference_repos = entry.get("reference_repos", []) or []
        if not isinstance(reference_repos, list) or not all(isinstance(r, str) for r in reference_repos):
            raise ConfigError(f"repos[{i}].reference_repos must be a list of repo names")

        # #316: new_issue_guidance — inline markdown or repo-relative file path.
        new_issue_guidance = entry.get("new_issue_guidance")
        if new_issue_guidance is not None and not isinstance(new_issue_guidance, str):
            raise ConfigError(f"repos[{i}].new_issue_guidance must be a string")

        # #305: artifact_paths — glob patterns for build artifacts to stash.
        artifact_paths_raw = entry.get("artifact_paths", []) or []
        if not isinstance(artifact_paths_raw, list):
            raise ConfigError(f"repos[{i}].artifact_paths must be a list of strings")
        for j, p in enumerate(artifact_paths_raw):
            if not isinstance(p, str):
                raise ConfigError(
                    f"repos[{i}].artifact_paths[{j}] must be a string, "
                    f"got {type(p).__name__}"
                )
        artifact_paths: list[str] = list(artifact_paths_raw)

        # #323: optional per-repo provider override.
        repo_provider = entry.get("provider")
        if repo_provider is not None and not isinstance(repo_provider, str):
            raise ConfigError(f"repos[{i}].provider must be a string")

        # #2687: uat_preview — per-PR preview URL template, and this repo's
        # opt-in into the pre-merge UAT gate (`coord.merge_queue.
        # requires_uat`). Absent (None) means "not opted in" regardless of
        # whether "uat" appears in `pipeline.default_gates` — see
        # `Repo.uat_preview`'s docstring.
        uat_preview = entry.get("uat_preview")
        if uat_preview is not None and not isinstance(uat_preview, str):
            raise ConfigError(f"repos[{i}].uat_preview must be a string")
        if isinstance(uat_preview, str) and not uat_preview.strip():
            raise ConfigError(
                f"repos[{i}].uat_preview must be a non-empty string (omit "
                "the key entirely to leave the UAT gate off for this repo)"
            )

        repos.append(
            Repo(
                name=name,
                github=github,
                depends_on=depends_on,
                default_branch=default_branch,
                develop_branch=develop_branch,
                build_command=build_command,
                test_command=test_command,
                ci_command=ci_command,
                run_cmd=run_cmd,
                worker_permissions=worker_permissions,
                housekeeping=housekeeping,
                coordinator_only_files=coordinator_only_files,
                reference_repos=reference_repos,
                new_issue_guidance=new_issue_guidance,
                artifact_paths=artifact_paths,
                provider=repo_provider,
                uat_preview=uat_preview,
            )
        )
    return repos, warnings


def _parse_worker_permissions(raw: Any, repo_index: int) -> WorkerPermissionsConfig:
    """Parse the ``worker_permissions`` block for a single repo.

    When *raw* is ``None`` (key absent from YAML), the default deny-list is
    applied — safety by default.  An explicit ``deny: []`` clears restrictions.
    """
    if raw is None:
        return WorkerPermissionsConfig(deny=list(DEFAULT_DENY_COMMANDS))

    if not isinstance(raw, dict):
        raise ConfigError(
            f"repos[{repo_index}].worker_permissions must be a mapping"
        )

    allow = raw.get("allow", []) or []
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise ConfigError(
            f"repos[{repo_index}].worker_permissions.allow must be a list of strings"
        )

    deny = raw.get("deny", []) or []
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise ConfigError(
            f"repos[{repo_index}].worker_permissions.deny must be a list of strings"
        )

    return WorkerPermissionsConfig(allow=allow, deny=deny)


# #1862: 24h "HH:MM" — the only shape `quiet_hours.start`/`.end` accept.
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(raw: Any, *, field_path: str) -> time:
    if not isinstance(raw, str):
        raise ConfigError(
            f"{field_path} must be a 24h 'HH:MM' string, got {type(raw).__name__}"
        )
    m = _HHMM_RE.match(raw)
    if not m:
        raise ConfigError(f"{field_path} must be 24h 'HH:MM' (e.g. '23:00'), got {raw!r}")
    return time(int(m.group(1)), int(m.group(2)))


def parse_quiet_hours_window(
    start: Any, end: Any, tz: Any, *, prefix: str = "quiet_hours"
) -> QuietHours:
    """Validate a raw ``start``/``end``/``tz`` triple into a `QuietHours`.

    #2146: THE definition of "a valid quiet-hours window", shared by the
    YAML path (`_parse_quiet_hours_block` below, i.e.
    ``machines[i].quiet_hours`` and ``notifications.quiet_hours``) and by
    the operator-set store path (`coord.machine_pause.local_set_quiet_hours`,
    written by `coord quiet-hours` / the daemon's `/pause` endpoint).

    The two sources must never diverge on what they accept: a window a
    `coord quiet-hours` call takes but `coordinator.yml` rejects (or vice
    versa) would make `--print-yaml`'s promotion path — the whole point of
    which is "make this temporary window permanent" — emit a block that
    fails to load. One validator, one answer.

    Raises `ConfigError` (with *prefix* naming the offending field) on
    anything invalid; the daemon endpoint relays that message verbatim in
    its 400 so an operator sees the real reason, not "bad request".
    """
    parsed_start = _parse_hhmm(start, field_path=f"{prefix}.start")
    parsed_end = _parse_hhmm(end, field_path=f"{prefix}.end")

    if not tz or not isinstance(tz, str):
        raise ConfigError(
            f"{prefix}.tz is required (IANA zone name, e.g. 'America/Chicago') — "
            "quiet hours never default to the daemon's own UTC clock, since that "
            "would silently fire at the wrong local hour"
        )
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ConfigError(f"{prefix}.tz {tz!r} is not a known IANA zone name: {e}") from e

    if parsed_start == parsed_end:
        raise ConfigError(
            f"{prefix}: start and end must differ ('always quiet' is ambiguous — "
            "use `coord pause` to take a machine out of rotation indefinitely instead)"
        )

    return QuietHours(start=parsed_start, end=parsed_end, tz=tz)


def _parse_quiet_hours_block(raw: Any, *, prefix: str) -> QuietHours | None:
    """Parse a ``{start, end, tz}`` quiet-hours mapping. ``None`` → ``None``.

    Shared by ``machines[i].quiet_hours`` (#1862, a *dispatch* window) and
    ``notifications.quiet_hours`` (#1632, a *deferral* window). The two
    windows mean different things, but "what a quiet-hours block looks
    like and which of its fields are mandatory" must not fork — a second,
    independently-drifting parser is how a fleet ends up with two clocks
    that disagree.

    ``tz`` is REQUIRED and validated against the IANA database: `coord
    serve` runs on UTC, so a naive time-of-day compared against the
    daemon's own clock would silently fire hours off from what a non-UTC
    operator wrote — a quiet-hours feature that activates early is worse
    than none, because it silently pulls the machine out of the fleet
    during the working day. Fail loudly here rather than default quietly.

    #2146: the field-level rules themselves now live in
    `parse_quiet_hours_window` above, so the operator-set store validates
    identically to this YAML path.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{prefix} must be a mapping")

    return parse_quiet_hours_window(
        raw.get("start"), raw.get("end"), raw.get("tz"), prefix=prefix
    )


def _parse_quiet_hours(raw: Any, *, machine_index: int, machine_name: str) -> QuietHours | None:
    """Parse ``machines[i].quiet_hours`` (#1862) — see
    :func:`_parse_quiet_hours_block` for the shape and the ``tz`` rule."""
    return _parse_quiet_hours_block(
        raw, prefix=f"machines[{machine_index}] ({machine_name!r}).quiet_hours"
    )


# #2915: appended to both "unknown repo" refusals in `_parse_machines`.
#
# Onboarding dell64 on 2026-08-28, a `repo_paths` KEY named the checkout's
# DIRECTORY (`claude-coordinator`) instead of the fleet's REPO NAME
# (`code-coordinator`). The two differ by design here — see the #2104 note in
# coordinator.example.yml — so the mistake is easy and the blast radius is
# total: this raise aborts the whole load, which takes `coordinator.yml` down
# for EVERY machine, not just the one with the typo. The old message named the
# bad key and stopped there, leaving an operator to work out that keys and
# values are drawn from different vocabularies. `coord machine add` now
# validates this before writing; this hint is for every config edited by hand.
_REPO_NAME_HINT = (
    "In `repo_paths`, the KEY is the repo NAME from this file's own `repos:` "
    "block and the VALUE is the on-disk path — they routinely differ (a repo "
    "renamed on GitHub keeps its old checkout directory). Note this error "
    "aborts the ENTIRE config load, so it takes every machine down, not just "
    "this one. `coord machine add` validates repo names before writing; "
    "`coord machine doctor <name>` checks the rest."
)


def _parse_machines(raw: Any, repos: list[Repo]) -> list[Machine]:
    if raw is None:
        raise ConfigError("Config must define 'machines'")
    if not isinstance(raw, list):
        raise ConfigError("'machines' must be a list")
    if not raw:
        raise ConfigError("'machines' must contain at least one machine")

    repo_names = {r.name for r in repos}
    machines: list[Machine] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"machines[{i}] must be a mapping, got {type(entry).__name__}")
        name = entry.get("name")
        host = entry.get("host")
        if not name or not isinstance(name, str):
            raise ConfigError(f"machines[{i}].name is required (string)")
        if not host or not isinstance(host, str):
            raise ConfigError(f"machines[{i}].host is required (string, tailscale hostname)")
        if name in seen:
            raise ConfigError(f"duplicate machine name: {name!r}")
        seen.add(name)

        capabilities = entry.get("capabilities", []) or []
        if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
            raise ConfigError(f"machines[{i}].capabilities must be a list of strings")

        machine_repos = entry.get("repos", []) or []
        if not isinstance(machine_repos, list) or not all(isinstance(r, str) for r in machine_repos):
            raise ConfigError(f"machines[{i}].repos must be a list of repo names")

        unknown = [r for r in machine_repos if r not in repo_names]
        if unknown:
            raise ConfigError(
                f"machines[{i}] ({name!r}) references unknown repos: {unknown} "
                f"— configured repos are {sorted(repo_names)}. {_REPO_NAME_HINT}"
            )

        repo_paths = entry.get("repo_paths", {}) or {}
        if not isinstance(repo_paths, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in repo_paths.items()
        ):
            raise ConfigError(f"machines[{i}].repo_paths must be a mapping of repo name → local path")
        unknown_paths = [r for r in repo_paths if r not in repo_names]
        if unknown_paths:
            raise ConfigError(
                f"machines[{i}] ({name!r}) repo_paths references unknown repos: "
                f"{unknown_paths} — configured repos are {sorted(repo_names)}. "
                f"{_REPO_NAME_HINT}"
            )

        # #1417: optional per-machine capacity override. `None` (unset)
        # means "use concurrency.max_workers" — see Machine.max_workers.
        machine_max_workers = entry.get("max_workers")
        if machine_max_workers is not None:
            if isinstance(machine_max_workers, bool) or not isinstance(machine_max_workers, int):
                raise ConfigError(f"machines[{i}].max_workers must be an integer")
            if machine_max_workers < 1:
                raise ConfigError(f"machines[{i}].max_workers must be at least 1")

        quiet_hours = _parse_quiet_hours(
            entry.get("quiet_hours"), machine_index=i, machine_name=name,
        )

        machines.append(
            Machine(
                name=name,
                host=host,
                capabilities=capabilities,
                repos=machine_repos,
                repo_paths=repo_paths,
                max_workers=machine_max_workers,
                quiet_hours=quiet_hours,
            )
        )
    return machines


KNOWN_HOOKS = {"close_merged_issues", "summary_report"}


def _parse_hooks(raw: Any) -> HooksConfig:
    if raw is None:
        return HooksConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'hooks' must be a mapping")
    hooks = HooksConfig()
    for event_name in ("on_round_complete", "on_session_end"):
        entries = raw.get(event_name)
        if entries is None:
            continue
        if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
            raise ConfigError(f"hooks.{event_name} must be a list of hook names")
        unknown = [e for e in entries if e not in KNOWN_HOOKS]
        if unknown:
            raise ConfigError(
                f"hooks.{event_name} references unknown hooks: {unknown}. "
                f"Known: {sorted(KNOWN_HOOKS)}"
            )
        setattr(hooks, event_name, entries)
    return hooks


def _parse_reviews(
    raw: Any, repo_names: set[str], provider_names: set[str] | None = None,
) -> ReviewsConfig:
    if raw is None:
        return ReviewsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'reviews' must be a mapping")

    cfg = ReviewsConfig()

    # #1811: reviews.provider — validated against providers.definitions the
    # same way reviews.repo_overrides is validated against repo_names below,
    # so an unknown name is a config error at parse time, not a silent
    # dispatch-time fallback.
    if "provider" in raw:
        value = raw["provider"]
        if not isinstance(value, str) or not value:
            raise ConfigError("reviews.provider must be a non-empty string")
        if provider_names is not None and value not in provider_names:
            raise ConfigError(
                f"reviews.provider references unknown provider: {value!r}"
            )
        cfg.provider = value

    for bool_field in ("enabled", "auto_dispatch", "require_approval", "allow_review_flood"):
        if bool_field in raw:
            value = raw[bool_field]
            if not isinstance(value, bool):
                raise ConfigError(f"reviews.{bool_field} must be a boolean")
            setattr(cfg, bool_field, value)

    for int_field in ("max_auto_dispatch_per_pass", "flood_threshold", "reaffirm_max_diff_lines"):
        if int_field in raw:
            value = raw[int_field]
            # bool is a subclass of int — reject it explicitly so a stray
            # `flood_threshold: true` doesn't silently become 1.
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"reviews.{int_field} must be a non-negative integer")
            setattr(cfg, int_field, value)

    if "reviewer_prompt" in raw:
        value = raw["reviewer_prompt"]
        if not isinstance(value, str):
            raise ConfigError("reviews.reviewer_prompt must be a string")
        cfg.reviewer_prompt = value

    checklist = raw.get("checklist", []) or []
    if not isinstance(checklist, list) or not all(isinstance(c, str) for c in checklist):
        raise ConfigError("reviews.checklist must be a list of strings")
    cfg.checklist = checklist

    overrides = raw.get("repo_overrides", {}) or {}
    if not isinstance(overrides, dict):
        raise ConfigError("reviews.repo_overrides must be a mapping of repo → list of strings")
    for repo_name, items in overrides.items():
        if not isinstance(repo_name, str):
            raise ConfigError("reviews.repo_overrides keys must be repo names")
        if repo_name not in repo_names:
            raise ConfigError(
                f"reviews.repo_overrides references unknown repo: {repo_name!r}"
            )
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ConfigError(
                f"reviews.repo_overrides[{repo_name}] must be a list of strings"
            )
    cfg.repo_overrides = overrides
    return cfg


def _parse_concurrency(raw: Any) -> ConcurrencyConfig:
    if raw is None:
        return ConcurrencyConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'concurrency' must be a mapping")
    cfg = ConcurrencyConfig()
    for key in (
        "max_workers", "stagger_seconds", "backoff_base", "max_retries",
        "stale_threshold", "first_output_timeout", "interactive_session_timeout_hours",
        "runtime_ceiling_s",
    ):
        val = raw.get(key)
        if val is None:
            continue
        if key in ("max_retries", "max_workers", "stale_threshold"):
            if not isinstance(val, int) or val < 0:
                raise ConfigError(f"concurrency.{key} must be a non-negative integer")
        else:
            # bool is a subclass of int — reject it explicitly for numeric keys.
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
                raise ConfigError(f"concurrency.{key} must be a non-negative number")
        setattr(cfg, key, val)
    if "auto_reassign" in raw:
        val = raw["auto_reassign"]
        if not isinstance(val, bool):
            raise ConfigError("concurrency.auto_reassign must be a boolean")
        cfg.auto_reassign = val
    if "bash_wrap_spawn" in raw:
        val = raw["bash_wrap_spawn"]
        if not isinstance(val, bool):
            raise ConfigError("concurrency.bash_wrap_spawn must be a boolean")
        cfg.bash_wrap_spawn = val
    return cfg


def _parse_smoke_tests(raw: Any) -> SmokeTestsConfig:
    if raw is None:
        return SmokeTestsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'smoke_tests' must be a mapping")

    cfg = SmokeTestsConfig()
    if "auto_queue" in raw:
        value = raw["auto_queue"]
        if not isinstance(value, bool):
            raise ConfigError("smoke_tests.auto_queue must be a boolean")
        cfg.auto_queue = value

    if "default_command" in raw:
        value = raw["default_command"]
        if value is not None and not isinstance(value, str):
            raise ConfigError("smoke_tests.default_command must be a string")
        cfg.default_command = value

    if "timeout_seconds" in raw:
        value = raw["timeout_seconds"]
        if not isinstance(value, int) or value <= 0:
            raise ConfigError("smoke_tests.timeout_seconds must be a positive integer")
        cfg.timeout_seconds = value

    rules_raw = raw.get("capability_rules", []) or []
    if not isinstance(rules_raw, list):
        raise ConfigError("smoke_tests.capability_rules must be a list")
    rules: list[SmokeRule] = []
    for i, entry in enumerate(rules_raw):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}] must be a mapping"
            )
        files = entry.get("files", []) or []
        requires = entry.get("requires", []) or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].files must be a list of strings"
            )
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].requires must be a list of strings"
            )
        if not files:
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].files must be non-empty"
            )
        if not requires:
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].requires must be non-empty"
            )
        command = entry.get("command")
        if command is not None:
            if not isinstance(command, str):
                raise ConfigError(
                    f"smoke_tests.capability_rules[{i}].command must be a string"
                )
            if not command.strip():
                raise ConfigError(
                    f"smoke_tests.capability_rules[{i}].command must be non-empty"
                )
        rules.append(SmokeRule(files=files, requires=requires, command=command))
    cfg.capability_rules = rules
    return cfg


def _parse_acceptance(raw: Any) -> AcceptanceConfig:
    """Parse the ``acceptance:`` block (#944, docs/ORACLE_LOOP.md).

    ``acceptance.drivers`` maps a local repo name (as declared under
    ``repos:``) to its driver config. Absent entirely -> no repo has a sealed
    acceptance suite, and ``coord acceptance run/record`` refuses with a
    clear error rather than guessing a default.
    """
    if raw is None:
        return AcceptanceConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'acceptance' must be a mapping")

    drivers_raw = raw.get("drivers", {}) or {}
    if not isinstance(drivers_raw, dict):
        raise ConfigError(
            "acceptance.drivers must be a mapping of repo name -> driver config"
        )

    drivers: dict[str, AcceptanceDriverConfig] = {}
    for repo_name, entry in drivers_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"acceptance.drivers[{repo_name!r}] must be a mapping")

        routes_raw = entry.get("routes")
        if routes_raw is not None:
            # #1125 review finding 5: a routed entry's flat kind/run/mock/
            # capability fields are unused (each route carries its own) — an
            # operator who sets both almost certainly meant one or the
            # other, so reject it rather than silently discarding the flat
            # fields.
            flat_fields = [
                f for f in ("kind", "run", "mock", "capability", "setup", "entrypoint")
                if entry.get(f)
            ]
            if flat_fields:
                raise ConfigError(
                    f"acceptance.drivers[{repo_name!r}] sets both 'routes' "
                    f"and flat field(s) {flat_fields!r} — a routed entry's "
                    "driver is entirely per-route; remove the flat fields "
                    "(they would otherwise be silently ignored)"
                )
            drivers[repo_name] = AcceptanceDriverConfig(
                routes=_parse_acceptance_routes(repo_name, routes_raw),
            )
            continue

        kind = entry.get("kind")
        if not kind or not isinstance(kind, str):
            raise ConfigError(f"acceptance.drivers[{repo_name!r}].kind is required")

        run = entry.get("run")
        if not run or not isinstance(run, str):
            raise ConfigError(f"acceptance.drivers[{repo_name!r}].run is required")

        mock = entry.get("mock", "") or ""
        if not isinstance(mock, str):
            raise ConfigError(f"acceptance.drivers[{repo_name!r}].mock must be a string")

        capability = entry.get("capability", "") or ""
        if not isinstance(capability, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].capability must be a string"
            )

        setup = entry.get("setup", "") or ""
        if not isinstance(setup, str):
            raise ConfigError(f"acceptance.drivers[{repo_name!r}].setup must be a string")

        entrypoint = _acceptance_entrypoint(
            entry, f"acceptance.drivers[{repo_name!r}].entrypoint"
        )

        drivers[repo_name] = AcceptanceDriverConfig(
            kind=kind, run=run, mock=mock, capability=capability, setup=setup,
            entrypoint=entrypoint,
        )

    return AcceptanceConfig(drivers=drivers)


def _parse_acceptance_routes(
    repo_name: str, routes_raw: Any,
) -> list[AcceptanceDriverConfig]:
    """Parse ``acceptance.drivers.<repo_name>.routes`` (#1125) into a list of
    ``AcceptanceDriverConfig`` route entries, each with ``match`` set.

    Each element is validated the same way as a flat driver entry
    (``kind``/``run`` required, ``mock``/``capability``/``setup`` optional
    strings), plus a required ``match`` glob.
    """
    if not isinstance(routes_raw, list) or not routes_raw:
        raise ConfigError(
            f"acceptance.drivers[{repo_name!r}].routes must be a non-empty list"
        )

    routes: list[AcceptanceDriverConfig] = []
    for i, route_entry in enumerate(routes_raw):
        if not isinstance(route_entry, dict):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}] must be a mapping"
            )

        match = route_entry.get("match")
        if not match or not isinstance(match, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].match is required"
            )

        kind = route_entry.get("kind")
        if not kind or not isinstance(kind, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].kind is required"
            )

        run = route_entry.get("run")
        if not run or not isinstance(run, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].run is required"
            )

        mock = route_entry.get("mock", "") or ""
        if not isinstance(mock, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].mock must be a string"
            )

        capability = route_entry.get("capability", "") or ""
        if not isinstance(capability, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].capability must be a string"
            )

        setup = route_entry.get("setup", "") or ""
        if not isinstance(setup, str):
            raise ConfigError(
                f"acceptance.drivers[{repo_name!r}].routes[{i}].setup must be a string"
            )

        entrypoint = _acceptance_entrypoint(
            route_entry, f"acceptance.drivers[{repo_name!r}].routes[{i}].entrypoint"
        )

        routes.append(
            AcceptanceDriverConfig(
                kind=kind, run=run, mock=mock, capability=capability, setup=setup,
                match=match, entrypoint=entrypoint,
            )
        )

    return routes


def _acceptance_entrypoint(entry: dict, label: str) -> str:
    """Validate an acceptance driver's optional ``entrypoint:`` (#1552).

    Must be a repo-root-relative *file* path — it is folded into the sealed
    set as an exact-match entry (:meth:`AcceptanceConfig.sealed_paths`), so
    an absolute path or a trailing-slash directory would silently seal
    nothing at all. Reject both here rather than at review time, where the
    only symptom would be a `test-author` bounced for a scope violation it
    cannot fix.
    """
    raw = entry.get("entrypoint", "") or ""
    if not isinstance(raw, str):
        raise ConfigError(f"{label} must be a string")
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("/") or value.startswith("~"):
        raise ConfigError(
            f"{label} must be repo-root-relative, not an absolute path "
            f"(got {value!r})"
        )
    if value.endswith("/"):
        raise ConfigError(
            f"{label} must name a FILE (the driver's crate-root/entry point), "
            f"not a directory (got {value!r})"
        )
    return value


def _parse_models(raw: Any) -> ModelsConfig:
    if raw is None:
        return ModelsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'models' must be a mapping")

    cfg = ModelsConfig()
    if "default" in raw:
        value = raw["default"]
        if not isinstance(value, str) or not value:
            raise ConfigError("models.default must be a non-empty string")
        cfg.default = value

    if "escalation" in raw:
        value = raw["escalation"]
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ConfigError("models.escalation must be a list of non-empty strings")
        cfg.escalation = list(value)

    if "labels" in raw:
        value = raw["labels"]
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise ConfigError(
                "models.labels must be a mapping of label name → model alias"
            )
        cfg.labels = dict(value)

    if "versions" in raw:
        value = raw["versions"]
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v
            for k, v in value.items()
        ):
            raise ConfigError(
                "models.versions must be a mapping of alias → exact model id"
            )
        cfg.versions = dict(value)

    return cfg


def _parse_pipeline(raw: Any) -> PipelineConfig:
    if raw is None:
        return PipelineConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'pipeline' must be a mapping")

    cfg = PipelineConfig()

    if "default_gates" in raw:
        value = raw["default_gates"]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError("pipeline.default_gates must be a list of strings")
        cfg.default_gates = list(value)

    if "labels" in raw:
        value = raw["labels"]
        if not isinstance(value, dict):
            raise ConfigError("pipeline.labels must be a mapping of label → list of strings")
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConfigError("pipeline.labels keys must be strings")
            if not isinstance(v, list) or not all(isinstance(g, str) for g in v):
                raise ConfigError(
                    f"pipeline.labels[{k!r}] must be a list of gate name strings"
                )
        cfg.labels = {k: list(v) for k, v in value.items()}

    if "auto_loop" in raw:
        value = raw["auto_loop"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.auto_loop must be a boolean")
        cfg.auto_loop = value

    if "confirm_test_verdict" in raw:
        value = raw["confirm_test_verdict"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.confirm_test_verdict must be a boolean")
        cfg.confirm_test_verdict = value

    if "auto_heal_phantom_rows" in raw:
        value = raw["auto_heal_phantom_rows"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.auto_heal_phantom_rows must be a boolean")
        cfg.auto_heal_phantom_rows = value

    if "auto_heal_stuck_test_state" in raw:
        value = raw["auto_heal_stuck_test_state"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.auto_heal_stuck_test_state must be a boolean")
        cfg.auto_heal_stuck_test_state = value

    if "max_review_iterations" in raw:
        value = raw["max_review_iterations"]
        if not isinstance(value, int) or value < 1:
            raise ConfigError("pipeline.max_review_iterations must be a positive integer")
        cfg.max_review_iterations = value

    if "escalate_fix_model" in raw:
        value = raw["escalate_fix_model"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.escalate_fix_model must be a boolean")
        cfg.escalate_fix_model = value

    if "escalate_semantic_conflicts" in raw:
        value = raw["escalate_semantic_conflicts"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.escalate_semantic_conflicts must be a boolean")
        cfg.escalate_semantic_conflicts = value

    if "semantic_conflict_model" in raw:
        value = raw["semantic_conflict_model"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("pipeline.semantic_conflict_model must be a non-empty string")
        cfg.semantic_conflict_model = value.strip()

    if "auto_dispatch_stalled" in raw:
        value = raw["auto_dispatch_stalled"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.auto_dispatch_stalled must be a boolean")
        cfg.auto_dispatch_stalled = value

    if "attention_thresholds" in raw:
        value = raw["attention_thresholds"]
        if not isinstance(value, dict):
            raise ConfigError(
                "pipeline.attention_thresholds must be a mapping of "
                "assignment type -> duration (e.g. '45m', '15m', or seconds)"
            )
        parsed: dict[str, float] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConfigError("pipeline.attention_thresholds keys must be strings")
            parsed[k] = _parse_duration_seconds(
                v, context=f"pipeline.attention_thresholds[{k!r}]"
            )
        cfg.attention_thresholds = parsed

    if "convergence_rounds" in raw:
        value = raw["convergence_rounds"]
        if not isinstance(value, int) or value < 1:
            raise ConfigError("pipeline.convergence_rounds must be a positive integer")
        cfg.convergence_rounds = value

    if "liveness_auditor" in raw:
        cfg.liveness_auditor = _parse_liveness_auditor(raw["liveness_auditor"])

    if "max_fix_rounds" in raw:
        value = raw["max_fix_rounds"]
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            raise ConfigError(
                "pipeline.max_fix_rounds must be a positive integer or null"
            )
        cfg.max_fix_rounds = value

    if "max_parallel_per_repo" in raw:
        value = raw["max_parallel_per_repo"]
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ConfigError(
                "pipeline.max_parallel_per_repo must be a non-negative "
                "integer or null (0 disables the per-repo ceiling)"
            )
        cfg.max_parallel_per_repo = value

    if "stall_thresholds" in raw:
        value = raw["stall_thresholds"]
        if not isinstance(value, dict):
            raise ConfigError(
                "pipeline.stall_thresholds must be a mapping of "
                "assignment type -> duration (e.g. '35m', '20m', or seconds)"
            )
        parsed_stall: dict[str, float] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConfigError("pipeline.stall_thresholds keys must be strings")
            parsed_stall[k] = _parse_duration_seconds(
                v, context=f"pipeline.stall_thresholds[{k!r}]"
            )
        cfg.stall_thresholds = parsed_stall

    return cfg


def _parse_liveness_auditor(raw: Any) -> LivenessAuditorConfig:
    if raw is None:
        return LivenessAuditorConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'pipeline.liveness_auditor' must be a mapping")

    cfg = LivenessAuditorConfig()

    if "enabled" in raw:
        value = raw["enabled"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.liveness_auditor.enabled must be a boolean")
        cfg.enabled = value

    if "strikes" in raw:
        value = raw["strikes"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError("pipeline.liveness_auditor.strikes must be a positive integer")
        cfg.strikes = value

    if "debounce_seconds" in raw:
        cfg.debounce_seconds = _parse_duration_seconds(
            raw["debounce_seconds"], context="pipeline.liveness_auditor.debounce_seconds"
        )

    if "model" in raw:
        value = raw["model"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("pipeline.liveness_auditor.model must be a non-empty string")
        cfg.model = value.strip()

    if "timeout_seconds" in raw:
        cfg.timeout_seconds = _parse_duration_seconds(
            raw["timeout_seconds"], context="pipeline.liveness_auditor.timeout_seconds"
        )

    if "claude_bin" in raw:
        value = raw["claude_bin"]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(
                "pipeline.liveness_auditor.claude_bin must be a non-empty string or null"
            )
        cfg.claude_bin = value.strip() if isinstance(value, str) else None

    return cfg


_DURATION_UNIT_SECONDS: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


def _parse_duration_seconds(value: Any, *, context: str) -> float:
    """Parse a duration into seconds. Accepts a bare number (seconds) or a
    string like ``"45m"``, ``"15m"``, ``"2h"``, ``"90s"``. Used for
    ``pipeline.attention_thresholds`` (#846)."""
    if isinstance(value, bool):
        raise ConfigError(f"{context} must be a number of seconds or a duration string")
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ConfigError(f"{context} must be a positive duration")
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text and text[-1] in _DURATION_UNIT_SECONDS and text[:-1].strip():
            number_part = text[:-1].strip()
            try:
                number = float(number_part)
            except ValueError:
                pass
            else:
                if number <= 0:
                    raise ConfigError(f"{context} must be a positive duration")
                return number * _DURATION_UNIT_SECONDS[text[-1]]
        try:
            number = float(text)
        except ValueError:
            raise ConfigError(
                f"{context} must be a number of seconds or a duration string "
                f"like '45m', '15m', '2h' (got {value!r})"
            ) from None
        if number <= 0:
            raise ConfigError(f"{context} must be a positive duration")
        return number
    raise ConfigError(f"{context} must be a number of seconds or a duration string")


def _parse_dispatch(raw: Any) -> DispatchConfig:
    if raw is None:
        return DispatchConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'dispatch' must be a mapping")

    cfg = DispatchConfig()

    if "max_files_per_worker" in raw:
        value = raw["max_files_per_worker"]
        if not isinstance(value, int) or value < 1:
            raise ConfigError("dispatch.max_files_per_worker must be a positive integer")
        cfg.max_files_per_worker = value

    if "auto_split" in raw:
        value = raw["auto_split"]
        if not isinstance(value, bool):
            raise ConfigError("dispatch.auto_split must be a boolean")
        cfg.auto_split = value

    if "require_plan" in raw:
        value = raw["require_plan"]
        if not isinstance(value, bool):
            raise ConfigError("dispatch.require_plan must be a boolean")
        cfg.require_plan = value

    return cfg


def _parse_usage_gate(raw: Any) -> UsageGateConfig:
    if raw is None:
        return UsageGateConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'usage_gate' must be a mapping")

    cfg = UsageGateConfig()

    if "mode" in raw:
        value = raw["mode"]
        if value not in ("disabled", "warn", "block"):
            raise ConfigError("usage_gate.mode must be one of: disabled, warn, block")
        cfg.mode = value

    for key in ("session_threshold_pct", "week_threshold_pct"):
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 <= value <= 100):
                raise ConfigError(f"usage_gate.{key} must be a number between 0 and 100")
            setattr(cfg, key, float(value))

    return cfg


def _parse_budget(raw: Any) -> BudgetConfig:
    """Parse the optional ``budget:`` block (#2131).

    An absent block yields the all-zero default, which
    :meth:`BudgetConfig.ceiling_for` reports as "no ceiling" — so an existing
    deployment upgrading into this release keeps today's behaviour exactly.
    """
    if raw is None:
        return BudgetConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'budget' must be a mapping")

    cfg = BudgetConfig()

    if "per_leg_ceiling_usd" in raw:
        value = raw["per_leg_ceiling_usd"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(
                "budget.per_leg_ceiling_usd must be a non-negative number "
                "(0 disables the ceiling)"
            )
        cfg.per_leg_ceiling_usd = float(value)

    overrides = raw.get("type_ceilings", {}) or {}
    if not isinstance(overrides, dict):
        raise ConfigError(
            "budget.type_ceilings must be a mapping of assignment type → USD ceiling"
        )
    parsed: dict[str, float] = {}
    for type_name, value in overrides.items():
        if not isinstance(type_name, str) or not type_name:
            raise ConfigError("budget.type_ceilings keys must be assignment type names")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(
                f"budget.type_ceilings[{type_name!r}] must be a non-negative "
                "number (0 disables the ceiling for that type)"
            )
        parsed[type_name] = float(value)
    cfg.type_ceilings = parsed

    return cfg


# #1628: (field name, minimum) for every numeric knob in the `health:` block.
# A single table rather than a per-field branch, because the whole point of
# the block is that adding a check adds a threshold — and if adding one meant
# hand-writing another eight-line validator, the "adding a check touches one
# file" property would rot on the config side instead.
_HEALTH_FLOAT_FIELDS: dict[str, float] = {
    "disk_warn_free_pct": 0.0,
    "disk_crit_free_pct": 0.0,
    "cargo_target_warn_gb": 0.0,
    "cargo_target_crit_gb": 0.0,
    "cargo_scan_budget_secs": 0.0,
    "worktree_stale_hours": 0.0,
    "index_lock_stale_minutes": 0.0,
    "network_timeout_secs": 0.0,
    "graph_stale_warn_hours": 0.0,
    "graph_stale_crit_hours": 0.0,
    "plan_usage_warn_pct": 0.0,
    "plan_usage_crit_pct": 0.0,
    "webapp_build_heartbeat_warn_minutes": 0.0,
    "webapp_build_heartbeat_crit_minutes": 0.0,
}
_HEALTH_INT_FIELDS: tuple[str, ...] = (
    "worktree_warn_count",
    "worktree_crit_count",
    "agent_version_warn_behind",
    "agent_version_crit_behind",
)
_HEALTH_STR_LIST_FIELDS: tuple[str, ...] = (
    "disabled_checks",
    "disk_paths",
    "cargo_target_extra_dirs",
    "spawned_coord_units",
)
# Path-ish overrides: a string, or null to mean "use the documented default".
# Table-driven for the same reason as the numeric fields above — a new deploy
# lane should not need its own hand-written eight-line validator.
_HEALTH_OPT_STR_FIELDS: tuple[str, ...] = (
    "agent_venv_python",
    "cli_venv_python",
    "tui_binary_path",
    "tui_source_dir",
    "coord_tui_checkout",
    "deploy_dir",
    "systemd_user_dir",
    "webapp_dist_path",
    "webapp_source_dir",
    "webapp_build_heartbeat_path",
    "coord_web_checkout",
)
# Pairs that must not be inverted.  A config where warn is stricter than crit
# silently makes the crit level unreachable — the check keeps reporting WARN
# for a machine that is actually on fire, which is exactly the failure this
# milestone exists to prevent.  Reject it at load rather than at 3am.
_HEALTH_ORDERED_PAIRS: tuple[tuple[str, str, str], ...] = (
    # (warn_field, crit_field, direction) — "asc": crit must be >= warn.
    ("cargo_target_warn_gb", "cargo_target_crit_gb", "asc"),
    ("graph_stale_warn_hours", "graph_stale_crit_hours", "asc"),
    ("plan_usage_warn_pct", "plan_usage_crit_pct", "asc"),
    ("worktree_warn_count", "worktree_crit_count", "asc"),
    ("agent_version_warn_behind", "agent_version_crit_behind", "asc"),
    (
        "webapp_build_heartbeat_warn_minutes",
        "webapp_build_heartbeat_crit_minutes",
        "asc",
    ),
    # Disk thresholds are *headroom* percentages, so crit must be the lower
    # number: warn at 15% free, crit at 7% free.
    ("disk_warn_free_pct", "disk_crit_free_pct", "desc"),
)


def _parse_health(raw: Any) -> HealthConfig:
    """Parse the optional ``health:`` block from coordinator.yml (#1628).

    An absent block returns :class:`HealthConfig` defaults — the thresholds
    that would have fired on the 2026-07-30 incidents.
    """
    if raw is None:
        return HealthConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'health' must be a mapping")

    cfg = HealthConfig()
    known = {f.name for f in fields(HealthConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            f"unknown health option(s): {', '.join(unknown)} "
            f"(valid: {', '.join(sorted(known))})"
        )

    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ConfigError("health.enabled must be a boolean")
        cfg.enabled = raw["enabled"]

    for key in _HEALTH_OPT_STR_FIELDS:
        if key in raw:
            value = raw[key]
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"health.{key} must be a string or null")
            # An empty/whitespace string is an operator typo, not "disabled":
            # accepting it would silently resolve the lane to the CWD.
            if isinstance(value, str) and not value.strip():
                raise ConfigError(f"health.{key} must be a non-empty string or null")
            setattr(cfg, key, value.strip() if isinstance(value, str) else None)

    if "pypi_index_url" in raw:
        value = raw["pypi_index_url"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("health.pypi_index_url must be a non-empty string")
        # Canonicalise the trailing slash here so the value that ends up in
        # the check's reported `values["index_url"]` is the same string
        # whichever way the operator wrote it.
        cfg.pypi_index_url = value.strip().rstrip("/")

    for key in _HEALTH_STR_LIST_FIELDS:
        if key in raw:
            value = raw[key]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"health.{key} must be a list of strings")
            setattr(cfg, key, list(value))

    for key, minimum in _HEALTH_FLOAT_FIELDS.items():
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"health.{key} must be a number")
            if value < minimum:
                raise ConfigError(f"health.{key} must be >= {minimum}")
            if key.endswith("_pct") and value > 100:
                raise ConfigError(f"health.{key} must be between 0 and 100")
            setattr(cfg, key, float(value))

    for key in _HEALTH_INT_FIELDS:
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(f"health.{key} must be a non-negative integer")
            setattr(cfg, key, value)

    for warn_key, crit_key, direction in _HEALTH_ORDERED_PAIRS:
        warn_value = getattr(cfg, warn_key)
        crit_value = getattr(cfg, crit_key)
        inverted = crit_value < warn_value if direction == "asc" else crit_value > warn_value
        if inverted:
            relation = ">=" if direction == "asc" else "<="
            raise ConfigError(
                f"health.{crit_key} ({crit_value}) must be {relation} "
                f"health.{warn_key} ({warn_value}) — otherwise the crit level "
                f"is unreachable and a failing machine only ever reports WARN"
            )

    return cfg


#: Transports `notifications.transport` accepts.  Adding one here is not
#: enough — `coord.notifier.transport.build_transport` must know it too.
_NOTIFICATION_TRANSPORTS = ("ntfy", "none")

_NOTIFICATIONS_STR_FIELDS = ("ntfy_url", "ntfy_topic", "ntfy_token", "web_base_url")
_NOTIFICATIONS_FLOAT_FIELDS: dict[str, tuple[float, float | None]] = {
    # name -> (minimum, maximum or None)
    "timeout_secs": (0.1, 120.0),
    "urgent_ttl_hours": (0.1, 24.0 * 14),
    "percentile": (50.0, 100.0),
    "silence_fraction": (0.05, 10.0),
    "stall_grace_mins": (0.0, 24.0 * 60),
}


def _parse_notifications(raw: Any) -> NotificationsConfig:
    """Parse the optional ``notifications:`` block from coordinator.yml (#1632).

    An absent block returns a **disabled** :class:`NotificationsConfig` —
    unchanged behaviour for every existing deployment.
    """
    if raw is None:
        return NotificationsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'notifications' must be a mapping")

    cfg = NotificationsConfig()
    known = {f.name for f in fields(NotificationsConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            f"unknown notifications option(s): {', '.join(unknown)} "
            f"(valid: {', '.join(sorted(known))})"
        )

    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ConfigError("notifications.enabled must be a boolean")
        cfg.enabled = raw["enabled"]

    if "transport" in raw:
        value = raw["transport"]
        if value not in _NOTIFICATION_TRANSPORTS:
            raise ConfigError(
                f"notifications.transport must be one of "
                f"{', '.join(_NOTIFICATION_TRANSPORTS)}, got {value!r}"
            )
        cfg.transport = value

    for key in _NOTIFICATIONS_STR_FIELDS:
        if key in raw:
            value = raw[key]
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"notifications.{key} must be a string or null")
            # An empty string is an operator typo, not "unset": accepting it
            # would produce an ntfy URL like "/topic" and a silent 404 on
            # every send, which is the hardest possible failure to notice on
            # a channel whose healthy state is silence.
            if isinstance(value, str) and not value.strip():
                raise ConfigError(f"notifications.{key} must be a non-empty string or null")
            setattr(cfg, key, value.strip().rstrip("/") if isinstance(value, str) else None)

    if "min_samples" in raw:
        value = raw["min_samples"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 2:
            raise ConfigError(
                "notifications.min_samples must be an integer >= 2 — a baseline "
                "derived from a population of one is not a baseline"
            )
        cfg.min_samples = value

    for key, (minimum, maximum) in _NOTIFICATIONS_FLOAT_FIELDS.items():
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"notifications.{key} must be a number")
            if value < minimum or (maximum is not None and value > maximum):
                bound = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
                raise ConfigError(f"notifications.{key} must be {bound}")
            setattr(cfg, key, float(value))

    if "cold_ceiling_mins" in raw:
        value = raw["cold_ceiling_mins"]
        if not isinstance(value, dict):
            raise ConfigError(
                "notifications.cold_ceiling_mins must be a mapping of "
                "assignment type -> minutes"
            )
        ceilings: dict[str, float] = {}
        for atype, minutes in value.items():
            if not isinstance(atype, str) or not atype.strip():
                raise ConfigError(
                    "notifications.cold_ceiling_mins keys must be assignment "
                    "type names (strings)"
                )
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
                raise ConfigError(
                    f"notifications.cold_ceiling_mins[{atype!r}] must be a positive number "
                    "of minutes"
                )
            ceilings[atype.strip()] = float(minutes)
        cfg.cold_ceiling_mins = ceilings

    cfg.quiet_hours = _parse_quiet_hours_block(
        raw.get("quiet_hours"), prefix="notifications.quiet_hours"
    )

    if cfg.enabled and cfg.transport == "ntfy" and not (cfg.ntfy_url and cfg.ntfy_topic):
        raise ConfigError(
            "notifications.enabled is true with transport 'ntfy' but "
            "ntfy_url/ntfy_topic are not both set — a notifier that silently "
            "delivers nothing is indistinguishable from a healthy fleet, which "
            "is the one failure this feature exists to prevent"
        )

    return cfg


def _parse_ci_store(raw: Any) -> CiStoreConfig:
    if raw is None:
        return CiStoreConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'ci_store' must be a mapping")

    cfg = CiStoreConfig()
    if "type" in raw:
        value = raw["type"]
        if not isinstance(value, str) or value not in ("github", "gitlab", "none"):
            raise ConfigError("ci_store.type must be one of: github, gitlab, none")
        cfg.type = value
    if "host" in raw:
        value = raw["host"]
        if not isinstance(value, str) or not value:
            raise ConfigError("ci_store.host must be a non-empty string")
        cfg.host = value
    if "token_env" in raw:
        value = raw["token_env"]
        if not isinstance(value, str) or not value:
            raise ConfigError("ci_store.token_env must be a non-empty string")
        cfg.token_env = value
    return cfg


def _parse_store(raw: Any) -> StoreConfig:
    if raw is None:
        return StoreConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'store' must be a mapping")

    cfg = StoreConfig()
    if "backend" in raw:
        value = raw["backend"]
        if not isinstance(value, str) or value not in (DIALECT_SQLITE, DIALECT_POSTGRES):
            raise ConfigError(
                f"store.backend must be one of: {DIALECT_SQLITE}, {DIALECT_POSTGRES}"
            )
        cfg.backend = value
    if "dsn" in raw:
        value = raw["dsn"]
        if value is not None and (not isinstance(value, str) or not value):
            raise ConfigError("store.dsn must be a non-empty string (or omitted)")
        cfg.dsn = value
    if cfg.backend == DIALECT_POSTGRES and not cfg.dsn:
        raise ConfigError("store.dsn is required when store.backend is 'postgres'")
    return cfg


def _parse_merge(raw: Any) -> MergeConfig:
    """Parse the optional ``merge:`` block from coordinator.yml.

    An absent block returns ``MergeConfig()`` — ``auto_drain=False`` —
    preserving existing behaviour: the daemon never merges automatically.
    """
    if raw is None:
        return MergeConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'merge' must be a mapping")

    cfg = MergeConfig()
    if "auto_drain" in raw:
        value = raw["auto_drain"]
        if not isinstance(value, bool):
            raise ConfigError("merge.auto_drain must be a boolean")
        cfg.auto_drain = value
    if "max_per_tick" in raw:
        value = raw["max_per_tick"]
        if not isinstance(value, int) or value < 0:
            raise ConfigError("merge.max_per_tick must be a non-negative integer")
        cfg.max_per_tick = value
    if "auto_reap_merged" in raw:
        value = raw["auto_reap_merged"]
        if not isinstance(value, bool):
            raise ConfigError("merge.auto_reap_merged must be a boolean")
        cfg.auto_reap_merged = value
    if "sibling_overlap_aging_hours" in raw:
        value = raw["sibling_overlap_aging_hours"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(
                "merge.sibling_overlap_aging_hours must be a non-negative number"
            )
        cfg.sibling_overlap_aging_hours = float(value)
    if "auto_revalidate" in raw:
        value = raw["auto_revalidate"]
        if not isinstance(value, bool):
            raise ConfigError("merge.auto_revalidate must be a boolean")
        cfg.auto_revalidate = value
    if "auto_revalidate_max_batch" in raw:
        value = raw["auto_revalidate_max_batch"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(
                "merge.auto_revalidate_max_batch must be a positive integer"
            )
        cfg.auto_revalidate_max_batch = value
    return cfg


def _parse_propagation(raw: Any) -> PropagationConfig:
    """Parse the optional ``propagation:`` block from coordinator.yml (#2583).

    An absent block returns ``PropagationConfig()`` — ``min_releases_behind=1``
    — preserving existing behaviour: `coord release propagate` and `coord
    release nightly-window` act on any delta at all, exactly as before this
    gate existed.
    """
    if raw is None:
        return PropagationConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'propagation' must be a mapping")

    known = {f.name for f in fields(PropagationConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            f"unknown propagation option(s): {', '.join(unknown)} "
            f"(valid: {', '.join(sorted(known))})"
        )

    cfg = PropagationConfig()
    if "min_releases_behind" in raw:
        value = raw["min_releases_behind"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(
                "propagation.min_releases_behind must be an integer >= 1"
            )
        cfg.min_releases_behind = value
    return cfg


def _parse_milestone(raw: Any) -> MilestoneConfig:
    """Parse the optional ``milestone:`` block from coordinator.yml.

    An absent block returns ``MilestoneConfig()`` — ``auto_dispatch=False`` —
    preserving existing behaviour: the daemon never auto-drains a milestone's
    work order; `coord milestone dispatch` still dispatches the ready
    frontier once per invocation.
    """
    if raw is None:
        return MilestoneConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'milestone' must be a mapping")

    cfg = MilestoneConfig()
    if "auto_dispatch" in raw:
        value = raw["auto_dispatch"]
        if not isinstance(value, bool):
            raise ConfigError("milestone.auto_dispatch must be a boolean")
        cfg.auto_dispatch = value
    return cfg


_VALID_AUDIT_LEVELS = ("business", "operational")


def _parse_audit(raw: Any) -> AuditConfig:
    """Parse the optional ``audit:`` block from coordinator.yml (#1036/#1038).

    An absent block returns ``AuditConfig()`` — ``max_rows=0`` (unlimited)
    and ``level="operational"`` — preserving existing behaviour:
    ``coord.audit.record_audit`` never trims and captures both tiers.
    """
    if raw is None:
        return AuditConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'audit' must be a mapping")

    cfg = AuditConfig()
    if "max_rows" in raw:
        value = raw["max_rows"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError("audit.max_rows must be a non-negative integer")
        cfg.max_rows = value
    if "level" in raw:
        value = raw["level"]
        if not isinstance(value, str) or value not in _VALID_AUDIT_LEVELS:
            raise ConfigError(
                f"audit.level must be one of {_VALID_AUDIT_LEVELS!r}, got {value!r}"
            )
        cfg.level = value
    return cfg


_PRICING_RATE_FIELDS = ("input", "output", "cache_read", "cache_creation")


def _parse_pricing(raw: Any) -> PricingConfig:
    """Parse the optional ``pricing:`` block from coordinator.yml (#1118).

    An absent block returns ``PricingConfig()`` — the built-in sonnet/opus/
    haiku defaults from :func:`_default_pricing`. Each entry under
    ``pricing:`` overrides or extends a canonical model key; unspecified
    rate fields on an *existing* key (e.g. ``opus``) keep the built-in
    default rather than being zeroed, so an operator can bump just
    ``pricing.opus.output`` without restating the other three rates. A
    wholly new model key starts from ``ModelRates()`` (all zero) and is
    filled in from whatever fields are given.
    """
    models = _default_pricing()
    if raw is None:
        return PricingConfig(models=models)
    if not isinstance(raw, dict):
        raise ConfigError("'pricing' must be a mapping of model name -> rates")

    for model_key, entry in raw.items():
        if not isinstance(model_key, str) or not model_key:
            raise ConfigError("pricing keys must be non-empty strings")
        if not isinstance(entry, dict):
            raise ConfigError(f"pricing[{model_key!r}] must be a mapping")

        base = models.get(model_key, ModelRates())
        rates = ModelRates(
            input=base.input,
            output=base.output,
            cache_read=base.cache_read,
            cache_creation=base.cache_creation,
        )
        for rate_field in _PRICING_RATE_FIELDS:
            if rate_field not in entry:
                continue
            value = entry[rate_field]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ConfigError(
                    f"pricing[{model_key!r}].{rate_field} must be a non-negative number"
                )
            setattr(rates, rate_field, float(value))
        models[model_key] = rates

    return PricingConfig(models=models)


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(value: str) -> str:
    """Expand ``${VAR}`` placeholders in *value* using :data:`os.environ`.

    Unset variables are left as-is (e.g. ``${MISSING}`` stays
    ``"${MISSING}"``).  Only ``${VAR}`` syntax is supported — bare ``$VAR``
    is not expanded.
    """

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        var = m.group(1)
        return os.environ.get(var, m.group(0))

    return _ENV_VAR_RE.sub(_replace, value)


def has_unexpanded_env_var(value: str | None) -> bool:
    """Whether *value* still contains an unexpanded ``${VAR}`` placeholder.

    #2336: :func:`_expand_env_vars` leaves an unset variable's placeholder
    untouched — ``${MISSING}`` stays the literal string ``"${MISSING}"`` —
    so a plain ``bool(value)`` check on the *result* can't tell "this
    resolved to a real secret" from "the env var was never set in this
    process's environment": both are non-empty strings. This is the
    "actually resolved" check the config's own env-var expansion needs a
    caller to run afterward — e.g. ``coord portal status``'s
    ``credentials_set`` used to report ``true`` for
    ``portal.bridge_client_id: "${BRIDGE_CLIENT_ID}"`` even when
    ``BRIDGE_CLIENT_ID`` was never exported into the shell the command ran
    in, which read as "credentials fine" while troubleshooting what was
    actually a missing-env-var problem.

    ``None`` is treated as not-a-placeholder (``False``) — an unset field is
    a separate "not configured" state, distinct from "configured but the
    env var didn't resolve".
    """
    if value is None:
        return False
    return bool(_ENV_VAR_RE.search(value))


#: Matches exactly what opencode's own ``OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX``
#: parser (#2321) accepts as a *string form* of a number: one or more ASCII
#: digits, nothing else — no sign, no decimal point, no whitespace, no
#: underscore digit-grouping. Combined with the ``> 0`` check below this
#: mirrors opencode's own rule (integer, ``> 0``) closely enough to reject
#: every value opencode is documented to silently discard: ``"131072 "``
#: (trailing whitespace), ``"131_072"`` (underscore grouping — Python's
#: ``int()`` would accept this but JS ``Number()`` coercion does not),
#: ``"0"`` (fails ``> 0``), ``"1.5"`` (decimal point), and ``"unlimited"``
#: (non-numeric).
_OPENCODE_OUTPUT_TOKEN_MAX_RE = re.compile(r"[0-9]+")

#: The env var name gated by :data:`_OPENCODE_OUTPUT_TOKEN_MAX_RE` — kept as
#: a constant rather than a literal so config.py and opencode.py can be
#: grepped for the exact same string (deliberately NOT imported from
#: coord.providers.opencode: config.py must not depend on provider modules).
OPENCODE_OUTPUT_TOKEN_MAX_ENV_VAR = "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"


def _validate_opencode_output_token_max_env(value: str, *, context: str) -> None:
    """Reject an ``OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`` override that
    opencode's own parser would silently discard (#2321).

    opencode's env-var parser demands an integer ``> 0``; anything else
    (a non-numeric string, a decimal, a value with surrounding whitespace
    or underscore digit-grouping, zero or negative) maps to ``undefined``
    internally, which silently restores opencode's 32,000-token default
    with **no warning anywhere** — exactly the failure mode #2321 exists
    to end. Config-parse time is the only point where "silently reverted
    to 32,000" can be turned into a loud, actionable error instead of a
    truncated worker run discovered hours later.

    Raises:
        ConfigError: when *value* is not a bare positive-integer string.
    """
    if not _OPENCODE_OUTPUT_TOKEN_MAX_RE.fullmatch(value) or int(value) <= 0:
        raise ConfigError(
            f"{context} must be a positive integer string with no "
            "surrounding whitespace, sign, decimal point, or underscores "
            "(opencode's OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX parser "
            "silently discards anything else and reverts to its 32000 "
            f"default) — got {value!r}"
        )


_PORTAL_PLAIN_STR_FIELDS = ("base_url",)
_PORTAL_SECRET_STR_FIELDS = ("bridge_client_id", "bridge_client_secret")


def _parse_portal(raw: Any, repo_names: set[str] | None = None) -> PortalConfig:
    """Parse the optional ``portal:`` block from coordinator.yml (#2179).

    An absent block returns a **disabled** :class:`PortalConfig` — unchanged
    behaviour for every existing deployment, and the correct default until
    ``BRIDGE_CLIENT_ID``/``BRIDGE_CLIENT_SECRET`` exist in production.

    *repo_names* (ms-67 contract §2) is the set of configured ``repos[].name``
    that ``project_repos[*].repos`` entries are cross-checked against, the
    same way ``reviews.repo_overrides`` already is. ``None`` skips that one
    check (for callers that parse the block in isolation); every other
    validation still applies.
    """
    if raw is None:
        return PortalConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'portal' must be a mapping")

    cfg = PortalConfig()
    known = {f.name for f in fields(PortalConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            f"unknown portal option(s): {', '.join(unknown)} "
            f"(valid: {', '.join(sorted(known))})"
        )

    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ConfigError("portal.enabled must be a boolean")
        cfg.enabled = raw["enabled"]

    for key in _PORTAL_PLAIN_STR_FIELDS:
        if key in raw:
            value = raw[key]
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"portal.{key} must be a string or null")
            if isinstance(value, str) and not value.strip():
                raise ConfigError(f"portal.{key} must be a non-empty string or null")
            setattr(cfg, key, value.strip().rstrip("/") if isinstance(value, str) else None)

    for key in _PORTAL_SECRET_STR_FIELDS:
        if key in raw:
            value = raw[key]
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"portal.{key} must be a string or null")
            if isinstance(value, str):
                # ${VAR} expansion (mirrors providers.definitions[*].env) so the
                # real secret lives in the environment, never committed here —
                # coordinator.yml is public-repo-adjacent operator config, and
                # these two values are exactly what coord-portal is public
                # about NOT holding on its own side of the boundary.
                value = _expand_env_vars(value).strip()
                if not value:
                    raise ConfigError(f"portal.{key} must be a non-empty string or null")
            setattr(cfg, key, value if isinstance(value, str) else None)

    if "timeout_secs" in raw:
        value = raw["timeout_secs"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError("portal.timeout_secs must be a positive number")
        cfg.timeout_secs = float(value)

    if "max_retries" in raw:
        value = raw["max_retries"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError("portal.max_retries must be a non-negative integer")
        cfg.max_retries = value

    if "project_repos" in raw:
        cfg.project_repos = _parse_portal_project_repos(raw["project_repos"], repo_names)

    if "approval" in raw:
        cfg.approval = _parse_portal_approval(raw["approval"])

    if cfg.enabled and not (cfg.base_url and cfg.bridge_client_id and cfg.bridge_client_secret):
        # Half a credential is not a credential — coord-portal's
        # isBridgeAuthorized takes the identical position and fails closed on
        # exactly this. Refuse at parse time rather than 401-looping forever.
        raise ConfigError(
            "portal.enabled is true but base_url/bridge_client_id/bridge_client_secret "
            "are not all set"
        )

    return cfg


def _parse_portal_approval(raw: Any) -> PortalApprovalConfig:
    """Parse ``portal.approval`` — the draft gate's per-kind policy (#2903).

    ``None`` (key present but empty) and an absent key both mean
    :data:`DEFAULT_PORTAL_APPROVAL`. Anything present is merged OVER those
    defaults — see :class:`PortalApprovalConfig` for why merge and not
    replace. Every key must name a real outbox kind
    (:data:`PORTAL_OUTBOX_KINDS`) and every value must be a bool: a typo'd
    kind that parsed silently would read as "gated: no" and quietly send
    agent prose to a customer, which is precisely the outcome this block
    exists to prevent.
    """
    if raw is None:
        return PortalApprovalConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'portal.approval' must be a mapping of kind -> boolean")

    unknown = sorted(set(raw) - set(PORTAL_OUTBOX_KINDS))
    if unknown:
        raise ConfigError(
            f"unknown portal.approval kind(s): {', '.join(unknown)} "
            f"(valid: {', '.join(PORTAL_OUTBOX_KINDS)})"
        )

    kinds = dict(DEFAULT_PORTAL_APPROVAL)
    for kind, value in raw.items():
        if not isinstance(value, bool):
            raise ConfigError(f"portal.approval.{kind} must be a boolean")
        kinds[kind] = value
    return PortalApprovalConfig(kinds=kinds)


def _parse_portal_project_repos(
    raw: Any, repo_names: set[str] | None
) -> list[PortalProjectRepo]:
    """Parse ``portal.project_repos`` (ms-67 contract §2).

    Rejects at LOAD, never at use — the same posture every other
    ``portal.*`` field takes, and the reason
    :meth:`PortalConfig.repos_for_project` can promise never to raise.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("portal.project_repos must be a list of mappings")

    parsed: list[PortalProjectRepo] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"portal.project_repos[{i}] must be a mapping")
        unknown = sorted(set(entry) - {"project_id", "repos"})
        if unknown:
            raise ConfigError(
                f"unknown portal.project_repos[{i}] option(s): {', '.join(unknown)} "
                "(valid: project_id, repos)"
            )
        project_id = entry.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ConfigError(
                f"portal.project_repos[{i}].project_id must be a non-empty string"
            )
        project_id = project_id.strip()
        if project_id in seen:
            raise ConfigError(
                f"portal.project_repos has duplicate project_id {project_id!r}"
            )
        seen.add(project_id)

        repos_raw = entry.get("repos")
        if (
            not isinstance(repos_raw, list)
            or not repos_raw
            or not all(isinstance(r, str) and r.strip() for r in repos_raw)
        ):
            raise ConfigError(
                f"portal.project_repos[{i}].repos must be a non-empty list of repo names"
            )
        repos = [r.strip() for r in repos_raw]
        if repo_names is not None:
            unknown_repos = [r for r in repos if r not in repo_names]
            if unknown_repos:
                raise ConfigError(
                    f"portal.project_repos[{i}] ({project_id}) references unknown "
                    f"repos: {unknown_repos}"
                )
        parsed.append(PortalProjectRepo(project_id=project_id, repos=repos))
    return parsed


def _parse_providers(raw: Any) -> ProvidersConfig:
    """Parse the optional ``providers:`` block from coordinator.yml.

    An absent block returns ``ProvidersConfig()`` — ``default == "claude"``
    and an implicit ``"claude"`` definition present.  An explicit block may
    override ``default`` and/or add named definitions.  Values in
    ``definitions[*].env`` undergo ``${VAR}`` expansion against
    :data:`os.environ`.
    """
    if raw is None:
        return ProvidersConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'providers' must be a mapping")

    cfg = ProvidersConfig()

    if "default" in raw:
        value = raw["default"]
        if not isinstance(value, str) or not value:
            raise ConfigError("providers.default must be a non-empty string")
        cfg.default = value

    defs_raw = raw.get("definitions", {}) or {}
    if not isinstance(defs_raw, dict):
        raise ConfigError("providers.definitions must be a mapping")

    for def_name, def_raw in defs_raw.items():
        if not isinstance(def_name, str):
            raise ConfigError("providers.definitions keys must be strings")
        if not isinstance(def_raw, dict):
            raise ConfigError(
                f"providers.definitions[{def_name!r}] must be a mapping"
            )

        ptype = def_raw.get("type")
        if not ptype or not isinstance(ptype, str):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].type is required (string)"
            )

        binary = def_raw.get("binary")
        if binary is not None and not isinstance(binary, str):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].binary must be a string"
            )

        model = def_raw.get("model")
        if model is not None and not isinstance(model, str):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].model must be a string"
            )

        attach_url = def_raw.get("attach_url")
        if attach_url is not None and not isinstance(attach_url, str):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].attach_url must be a string"
            )

        env_raw = def_raw.get("env", {}) or {}
        if not isinstance(env_raw, dict):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].env must be a mapping"
            )
        for k, v in env_raw.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ConfigError(
                    f"providers.definitions[{def_name!r}].env must map strings to strings"
                )
        # Expand ${VAR} in env values.
        env: dict[str, str] = {k: _expand_env_vars(v) for k, v in env_raw.items()}

        # #2321: an operator override of opencode's output-token-cap knob
        # must be a value opencode's own parser will actually honour — see
        # _validate_opencode_output_token_max_env for why a bad value here
        # (e.g. a trailing space) is worse than a normal type error: it
        # doesn't fail, it silently reverts to the 32000 default.
        if OPENCODE_OUTPUT_TOKEN_MAX_ENV_VAR in env:
            _validate_opencode_output_token_max_env(
                env[OPENCODE_OUTPUT_TOKEN_MAX_ENV_VAR],
                context=(
                    f"providers.definitions[{def_name!r}].env"
                    f"[{OPENCODE_OUTPUT_TOKEN_MAX_ENV_VAR!r}]"
                ),
            )

        extra_args_raw = def_raw.get("extra_args", []) or []
        if not isinstance(extra_args_raw, list) or not all(
            isinstance(a, str) for a in extra_args_raw
        ):
            raise ConfigError(
                f"providers.definitions[{def_name!r}].extra_args must be a list of strings"
            )
        extra_args: list[str] = list(extra_args_raw)

        cfg.definitions[def_name] = ProviderDef(
            type=ptype,
            binary=binary,
            model=model,
            attach_url=attach_url,
            env=env,
            extra_args=extra_args,
        )

    # Belt-and-suspenders: ProvidersConfig.__post_init__ already
    # materialises the implicit "claude" entry when ProvidersConfig() is
    # constructed above (line ~904), so this branch is unreachable under
    # current code.  Kept as a guard against future refactors that might
    # construct ProvidersConfig differently (e.g. via dict-update or
    # bypassing __post_init__ with object.__new__) — the invariant
    # "definitions always contains 'claude'" is load-bearing for
    # resolve_provider_name() callers that look up the definition
    # without checking presence first.
    if "claude" not in cfg.definitions:
        cfg.definitions["claude"] = ProviderDef(type="claude")

    # #1889: providers.labels — an issue-level lever mirroring
    # models.labels (label name -> provider name), parsed AFTER
    # cfg.definitions above so it can be validated against the very
    # registry it references. Validated at parse time, the same pattern
    # reviews.provider uses (#1811) — an unknown provider name here is a
    # config-load error, not a dispatch-time surprise discovered at 2am.
    labels_raw = raw.get("labels", {}) or {}
    if not isinstance(labels_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in labels_raw.items()
    ):
        raise ConfigError(
            "providers.labels must be a mapping of label name → provider name"
        )
    unknown_providers = sorted(set(labels_raw.values()) - set(cfg.definitions))
    if unknown_providers:
        raise ConfigError(
            f"providers.labels references unknown provider(s): {unknown_providers}"
        )
    cfg.labels = dict(labels_raw)

    return cfg


def _validate_dependencies(repos: list[Repo]) -> None:
    from coord.deps import detect_cycles

    repo_names = {r.name for r in repos}
    for r in repos:
        unknown = [d for d in r.depends_on if d not in repo_names]
        if unknown:
            raise ConfigError(
                f"repo {r.name!r} depends_on unknown repos: {unknown}"
            )
        if r.name in r.depends_on:
            raise ConfigError(f"repo {r.name!r} cannot depend on itself")

        # #268: reference_repos go through the same name-resolution as
        # depends_on but DO NOT feed into the cycle detector — the
        # intent is precisely to allow back-references (vimcode →
        # quadraui in depends_on; quadraui → vimcode in reference_repos)
        # that would be cycles if treated as build deps.
        unknown_ref = [r2 for r2 in r.reference_repos if r2 not in repo_names]
        if unknown_ref:
            raise ConfigError(
                f"repo {r.name!r} reference_repos unknown repos: {unknown_ref}"
            )
        if r.name in r.reference_repos:
            raise ConfigError(
                f"repo {r.name!r} cannot reference itself"
            )

    cycles = detect_cycles(repos)
    if cycles:
        cycle_str = " → ".join(cycles[0])
        raise ConfigError(f"circular dependency detected: {cycle_str}")
