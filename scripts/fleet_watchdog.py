#!/usr/bin/python3
"""Fleet watchdog: a stdlib-only, coord-independent repair loop (#2580).

On 2026-08-22 the fleet was dead for 11h because ``~/.coord-venv`` became an
editable install pointing at a deleted worktree. Everything that could have
noticed or healed it — the tick, ``coord notify`` (carrying #2536's
phantom-row auto-heal), ``coord notifier`` — execs from that same venv and
died with it (#2569 root cause, #2570 blast radius, #2572 escalation). The
recovery was mechanical and took seconds once diagnosed: repoint
``~/.coord-venv`` at the healthy sibling blue/green slot. Nothing about that
repair needs ``coord`` to be importable, which is exactly why this watchdog
must not live inside ``coord``.

Hard constraints (see #2580 — these are the design, not preferences):

1. Runs under ``/usr/bin/python3`` only — never ``~/.coord-venv/bin/python``.
   Immunity to the failure it repairs must be structural.
2. stdlib only. No ``httpx``, no PyYAML. Board state is read over HTTP with
   ``urllib.request`` + ``json``; ``coordinator.yml`` is never parsed.
3. Never ``import coord`` (enforced by ``tests/test_fleet_watchdog.py``, a
   grep test — nothing here can quietly regress that).
4. Every binary this script shells out to is resolved to an absolute path.
   A cron/systemd PATH is barer than a login shell's, and a watchdog that
   mis-resolves a binary and "repairs" the wrong thing is worse than none.
5. Never ``pip install`` anything. Restoring known-good state and moving
   versions are different jobs with different risk.
6. Never repair while a release cordon is active for this machine.

**The intent sentinel.** A sweep cannot distinguish "broken" from
"deliberately off" — see ``~/.coord/watchdog-suppress.json`` (JSON, read via
:func:`load_suppressions`) and :func:`is_suppressed`. Anything not covered by
a sentinel is reported, never fixed. Default to reporting.

**Rate limiting.** If the same condition gets repaired ``--rate-limit``
(default 3) runs in a row, the watchdog stops repairing it and escalates
instead — see :func:`run_check` / :func:`process_finding` and
``~/.coord/watchdog-state.json``. Repairing a recurring fault forever is how
a root cause survives (the exact #2314 -> #2569 pattern).

Tier 1 (:data:`TIER1_CHECKS`) is safe to auto-repair; each check re-verifies
its own precondition immediately before acting, so a condition that resolved
itself between detection and repair is a no-op, not a mutation. Tier 2
(:data:`TIER2_CHECKS`) only ever detects and reports — see the module
docstring of each ``check_*`` function for why.

**Known limitation: the checkout universe is hardcoded, not discovered.**
Constraint 2 above forbids parsing ``coordinator.yml``, which is where real
repo checkout paths live — so :func:`_candidate_git_locks` (the stale
``.git/index.lock`` scan) only ever looks under ``~/src/*`` plus
``~/.coord/worktrees/*``. A repo cloned somewhere else is invisible to that
check. Acceptable given the constraint, but worth knowing rather than
discovering by surprise.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Absolute binaries (#2580 constraint 4 / #2561). Never resolved via PATH —
# a systemd user unit's PATH is narrower than a login shell's, and `shutil.
# which` would silently trust whatever bare PATH the service happens to have.
# ---------------------------------------------------------------------------


def _first_existing(*candidates: str) -> str | None:
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


SYSTEMCTL = _first_existing("/usr/bin/systemctl", "/bin/systemctl")
TMUX = _first_existing("/usr/bin/tmux", "/usr/local/bin/tmux", "/bin/tmux")

# Overridable in tests; /proc is a platform fact, not an operator preference
# (same posture as coord/health/checks/index_lock.py, which this mirrors).
PROC_ROOT = Path("/proc")

# ``.git/index.lock`` younger than this might be a legitimate in-flight git
# operation — matches index_lock.py's own default.
GIT_LOCK_STALE_SECONDS = 600.0
# A worktree younger than this might still be mid `_setup_worktree` — matches
# the `recent_secs` guard `AgentServer.clean_worktrees` uses for the same race.
WORKTREE_RECENT_SECONDS = 3600.0

DEFAULT_RATE_LIMIT = 3
DEFAULT_HTTP_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class WatchdogContext:
    home: Path
    coord_dir: Path
    venv_dir: Path
    local_bin_coord: Path
    rollback_script: Path
    worktrees_dir: Path
    suppress_path: Path
    state_path: Path
    paused_machines_path: Path
    hostname: str
    now: float
    board_url: str | None
    http_timeout: float = DEFAULT_HTTP_TIMEOUT
    rate_limit: int = DEFAULT_RATE_LIMIT
    dry_run: bool = False
    cordon_active: bool = False
    cordon_reason: str = ""


def build_context(args: argparse.Namespace) -> WatchdogContext:
    home = Path(args.home).expanduser()
    coord_dir = Path(args.coord_dir) if args.coord_dir else home / ".coord"
    venv_dir = Path(args.venv_dir) if args.venv_dir else home / ".coord-venv"
    local_bin_coord = (
        Path(args.local_bin_coord) if args.local_bin_coord else home / ".local" / "bin" / "coord"
    )
    rollback_script = (
        Path(args.rollback_script)
        if args.rollback_script
        else Path(__file__).resolve().parent / "coord-venv-rollback.sh"
    )
    ctx = WatchdogContext(
        home=home,
        coord_dir=coord_dir,
        venv_dir=venv_dir,
        local_bin_coord=local_bin_coord,
        rollback_script=rollback_script,
        worktrees_dir=coord_dir / "worktrees",
        suppress_path=coord_dir / "watchdog-suppress.json",
        state_path=coord_dir / "watchdog-state.json",
        paused_machines_path=coord_dir / "paused_machines.json",
        hostname=args.hostname or socket.gethostname(),
        now=args.now if args.now is not None else time.time(),
        board_url=args.board_url,
        http_timeout=args.http_timeout,
        rate_limit=args.rate_limit,
        dry_run=args.dry_run,
    )
    ctx.cordon_active, ctx.cordon_reason = _read_cordon(ctx)
    return ctx


def _cordon_active(expires_at: float, now: float) -> bool:
    """Ported (not imported, per constraint 3) from
    ``coord.release_cordon.Cordon.active`` — same formula, same "an
    ``expires_at`` of 0 means no expiry, i.e. still active" contract.
    Pulled out to its own function so a test can cross-check it against the
    original directly, rather than only exercising it indirectly through
    :func:`_read_cordon` — the split-brain risk CLAUDE.md's #2096 section
    flags for every mirrored-not-imported piece of this module.
    """
    return not expires_at or now < expires_at


def _read_cordon(ctx: WatchdogContext) -> tuple[bool, str]:
    """Is *this* machine under an active (non-expired) release cordon?

    #2580 constraint 6. Scoped to this machine's own entry only — cleaning up
    an *expired* cordon recorded for some other machine (Tier 1 item 5) is
    unrelated bookkeeping and must not be blocked by this machine's own drain
    state, or vice versa.
    """
    data = _read_json(ctx.paused_machines_path)
    if not data:
        return False, ""
    cordons = data.get("release_cordons") or {}
    entry = cordons.get(ctx.hostname)
    if not isinstance(entry, dict):
        return False, ""
    expires_at = entry.get("expires_at") or 0
    if not _cordon_active(expires_at, ctx.now):
        return False, ""
    reason = entry.get("reason") or f"release cordon active ({entry.get('owner', 'release')})"
    return True, reason


def _read_json(path: Path) -> dict:
    try:
        raw = path.read_text()
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _systemctl_env() -> dict[str, str]:
    """``os.environ`` overlaid with a best-effort ``XDG_RUNTIME_DIR`` default
    for the ``systemctl --user`` calls this module makes.

    ``os.getuid()`` is POSIX-only -- unguarded, this raised ``AttributeError``
    on win32 even though ``SYSTEMCTL`` is already ``None`` there in real
    production runs (there is no ``systemctl`` to resolve), because tests
    patch ``SYSTEMCTL`` directly to exercise the subprocess-calling paths on
    every platform (#2729, same shape as coord/agent_app.py's #2681 fix). The
    default is simply skipped on win32 rather than invented.
    """
    env = dict(os.environ)
    # A bare `systemctl --user` silently no-ops without this over ssh/cron/
    # systemd-timer contexts (#2561's PATH lesson, same shape for XDG).
    if sys.platform != "win32":
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


# ---------------------------------------------------------------------------
# Suppression sentinel
# ---------------------------------------------------------------------------


def load_suppressions(ctx: WatchdogContext) -> dict:
    return _read_json(ctx.suppress_path)


def is_suppressed(
    suppressions: dict, keys: tuple[str, ...], *, now: float
) -> tuple[bool, dict | None]:
    """Is any of *keys* covered by an unexpired sentinel?

    A sentinel with ``"expires": null`` never lapses. One with a malformed
    (non-ISO8601) ``"expires"`` is treated as still-active rather than
    silently ignored — a typo in an operator's suppression file must not
    fail open into "no longer suppressed."
    """
    for key in keys:
        entry = suppressions.get(key)
        if not isinstance(entry, dict):
            continue
        expires = entry.get("expires")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(str(expires))
            except ValueError:
                return True, entry
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if now >= exp_dt.timestamp():
                continue  # lapsed — falls through to normal handling
        return True, entry
    return False, None


# ---------------------------------------------------------------------------
# Rate-limit state
# ---------------------------------------------------------------------------


def load_state(ctx: WatchdogContext) -> dict:
    return _read_json(ctx.state_path)


def save_state(ctx: WatchdogContext, state: dict) -> None:
    if ctx.dry_run:
        return
    _atomic_write_json(ctx.state_path, state)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    condition: str
    signature: str
    tier: int
    summary: str
    suppress_keys: tuple[str, ...] = ()
    repair_fn: Callable[["WatchdogContext"], tuple[bool, str]] | None = None
    repaired: bool = False
    escalated: bool = False
    suppressed: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.suppress_keys:
            self.suppress_keys = (self.signature,)

    def to_dict(self) -> dict:
        return {
            "condition": self.condition,
            "signature": self.signature,
            "tier": self.tier,
            "summary": self.summary,
            "repaired": self.repaired,
            "escalated": self.escalated,
            "suppressed": self.suppressed,
            "error": self.error,
        }


@dataclass
class Check:
    name: str
    tier: int
    detect: Callable[["WatchdogContext"], list[Finding]]


# ---------------------------------------------------------------------------
# Tier 1 — safe to auto-repair
# ---------------------------------------------------------------------------


def _slot_health(python_bin: Path, pip_bin: Path) -> tuple[bool, str]:
    """``(healthy, reason)`` for a blue/green slot.

    Mirrors the two checks ``coord.agent_update._smoke_check`` and the
    documented ``pip show ... | grep -i editable`` detection use — but
    reimplemented here (not imported) per constraint 3.
    """
    if not python_bin.exists():
        return False, f"no interpreter at {python_bin}"
    try:
        result = subprocess.run(
            [str(python_bin), "-c", "import coord.state, coord.commands.review"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"import check raised {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, f"import check failed: {(result.stderr or result.stdout).strip()[-300:]}"

    if pip_bin.exists():
        try:
            shown = subprocess.run(
                [str(pip_bin), "show", "code-coordinator"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if "editable project location" in shown.stdout.lower():
                return False, "editable install"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"pip show raised {type(exc).__name__}: {exc}"
    return True, "ok"


def check_venv_rollback(ctx: WatchdogContext) -> list[Finding]:
    """Tier 1 item 1: broken live venv, healthy sibling slot -> roll back.

    Detection only decides whether this condition is worth a finding at all;
    ``coord-venv-rollback.sh`` re-derives "is it actually still broken" and
    "is the sibling actually healthy" itself at repair time and refuses
    rather than guessing (#2580's explicit requirement for that script).

    Accepted gap: a fully MISSING ``~/.coord-venv`` (as opposed to
    present-but-broken) produces no finding here — the 2026-08-22 incident
    this watchdog targets was an editable install pointing at a deleted
    worktree, not an absent venv directory, and ``coord-venv-rollback.sh``
    itself has nothing to roll back to without a live symlink to inspect.
    """
    venv = ctx.venv_dir
    if not venv.exists():
        return []
    healthy, reason = _slot_health(venv / "bin" / "python3", venv / "bin" / "pip")
    if healthy:
        return []
    return [
        Finding(
            condition="venv-rollback",
            signature="venv-rollback",
            tier=1,
            summary=f"~/.coord-venv is broken ({reason})",
            repair_fn=_repair_venv_rollback,
        )
    ]


def _repair_venv_rollback(ctx: WatchdogContext) -> tuple[bool, str]:
    if not ctx.rollback_script.exists():
        return False, f"rollback script not found at {ctx.rollback_script}"
    try:
        result = subprocess.run(
            [str(ctx.rollback_script)],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "VENV_DIR": str(ctx.venv_dir)},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"rollback script raised {type(exc).__name__}: {exc}"
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, output[-500:] or f"rollback script exited {result.returncode}"
    return True, output[-500:] or "rolled back"


def check_local_bin_symlink(ctx: WatchdogContext) -> list[Finding]:
    """Tier 1 item 2: ``~/.local/bin/coord`` no longer a symlink into the
    venv — #2314's exact damage.

    Accepted gap, same root cause as :func:`check_venv_rollback`'s: when
    ``~/.coord-venv`` itself is fully missing, ``venv_resolved`` is ``None``
    and the "points outside the venv" branch below is skipped — so a
    symlink that (correctly, at the time it was made) points into a venv
    that has since vanished entirely reports nothing. Narrower than it
    sounds: it only bites when the venv disappears out from under an
    otherwise-correct symlink, not the editable-install case #2580 targets.
    """
    target = ctx.local_bin_coord
    venv_resolved = ctx.venv_dir.resolve() if ctx.venv_dir.exists() else None
    broken_reason: str | None = None
    if not target.is_symlink():
        if target.exists():
            broken_reason = "exists but is not a symlink"
        else:
            broken_reason = "missing"
    elif venv_resolved is not None:
        try:
            resolved = target.resolve()
            resolved.relative_to(venv_resolved)
        except (OSError, ValueError):
            broken_reason = "points outside ~/.coord-venv"
    if broken_reason is None:
        return []
    return [
        Finding(
            condition="local-bin-symlink",
            signature="local-bin-symlink",
            tier=1,
            summary=f"~/.local/bin/coord is broken ({broken_reason})",
            repair_fn=_repair_local_bin_symlink,
        )
    ]


def _repair_local_bin_symlink(ctx: WatchdogContext) -> tuple[bool, str]:
    venv_coord = ctx.venv_dir / "bin" / "coord"
    if not venv_coord.exists():
        return False, f"{venv_coord} does not exist — nothing to link to"
    target = ctx.local_bin_coord
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.watchdog-tmp-{os.getpid()}"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(venv_coord, tmp)
    os.replace(tmp, target)
    return True, f"relinked {target} -> {venv_coord}"


def check_failed_units(ctx: WatchdogContext) -> list[Finding]:
    """Tier 1 item 3: a ``coord-*`` unit in ``failed`` state.

    Scoped to ``coord-*`` units only — this watchdog has no business
    restarting arbitrary host services.
    """
    if SYSTEMCTL is None:
        return []
    try:
        result = subprocess.run(
            [
                SYSTEMCTL,
                "--user",
                "list-units",
                "--type=service",
                "--state=failed",
                "--no-legend",
                "--plain",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=_systemctl_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    findings = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if not unit.startswith("coord-") or not unit.endswith(".service"):
            continue
        findings.append(
            Finding(
                condition="failed-unit",
                signature=f"failed-unit:{unit}",
                tier=1,
                summary=f"{unit} is in failed state",
                suppress_keys=(unit, f"failed-unit:{unit}"),
                repair_fn=functools.partial(_repair_failed_unit, unit=unit),
            )
        )
    return findings


def _repair_failed_unit(ctx: WatchdogContext, *, unit: str) -> tuple[bool, str]:
    if SYSTEMCTL is None:
        return False, "systemctl not found at any known absolute path"
    env = _systemctl_env()
    is_failed = subprocess.run(
        [SYSTEMCTL, "--user", "is-failed", unit],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    if is_failed.stdout.strip() != "failed":
        return True, f"{unit} already recovered"
    subprocess.run(
        [SYSTEMCTL, "--user", "reset-failed", unit],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    restart = subprocess.run(
        [SYSTEMCTL, "--user", "restart", unit],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if restart.returncode != 0:
        return False, (restart.stderr or restart.stdout).strip()[-500:]
    return True, f"reset-failed + restarted {unit}"


def has_open_holder(lock_path: Path, *, proc_root: Path | None = None) -> bool | None:
    """Whether a live process holds *lock_path* open right now.

    Ported (not imported, per constraint 3) from
    ``coord.health.checks.index_lock.has_open_holder`` — same algorithm,
    same ``None``-means-"couldn't check" contract.
    """
    root = proc_root or PROC_ROOT
    try:
        pids = [entry.name for entry in root.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    target = str(lock_path)
    deleted_suffix = " (deleted)"
    for pid in pids:
        try:
            fds = list((root / pid / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if link.endswith(deleted_suffix):
                link = link[: -len(deleted_suffix)]
            if link == target:
                return True
    return False


def _candidate_git_locks(ctx: WatchdogContext) -> list[Path]:
    """Known limitation: hardcodes ``~/src/*`` + ``~/.coord/worktrees/*`` as
    the checkout universe, since constraint 2 (module docstring) forbids
    parsing ``coordinator.yml``, the actual source of truth for repo
    checkout paths. A repo cloned somewhere else is invisible to this scan.
    """
    candidates: list[Path] = []
    src_dir = ctx.home / "src"
    if src_dir.is_dir():
        for repo_dir in sorted(src_dir.iterdir()):
            lock = repo_dir / ".git" / "index.lock"
            if lock.exists():
                candidates.append(lock)
    if ctx.worktrees_dir.is_dir():
        for wt in sorted(ctx.worktrees_dir.iterdir()):
            if wt.is_symlink() or not wt.is_dir():
                continue
            dotgit = wt / ".git"
            gitdir = None
            if dotgit.is_file():
                try:
                    text = dotgit.read_text().strip()
                except OSError:
                    text = ""
                if text.startswith("gitdir:"):
                    raw = text.split(":", 1)[1].strip()
                    gitdir = (wt / raw).resolve() if not os.path.isabs(raw) else Path(raw)
            elif dotgit.is_dir():
                gitdir = dotgit
            if gitdir is not None:
                lock = gitdir / "index.lock"
                if lock.exists():
                    candidates.append(lock)
    return candidates


def check_stale_git_lock(ctx: WatchdogContext) -> list[Finding]:
    """Tier 1 item 4: a stale ``.git/index.lock`` with no live holder (#2206)."""
    findings = []
    for lock_path in _candidate_git_locks(ctx):
        try:
            mtime = lock_path.stat().st_mtime
        except OSError:
            continue
        age = ctx.now - mtime
        if age < GIT_LOCK_STALE_SECONDS:
            continue
        holder = has_open_holder(lock_path)
        sig = f"stale-git-lock:{lock_path}"
        if holder is True:
            continue
        if holder is None:
            findings.append(
                Finding(
                    condition="stale-git-lock",
                    signature=sig,
                    tier=1,
                    summary=f"{lock_path} is stale but /proc unavailable to confirm no holder",
                    repair_fn=functools.partial(_repair_stale_git_lock, lock_path=lock_path, verified=False),
                )
            )
        else:
            findings.append(
                Finding(
                    condition="stale-git-lock",
                    signature=sig,
                    tier=1,
                    summary=f"{lock_path} is stale ({age:.0f}s) with no live holder",
                    repair_fn=functools.partial(_repair_stale_git_lock, lock_path=lock_path, verified=True),
                )
            )
    return findings


def _repair_stale_git_lock(ctx: WatchdogContext, *, lock_path: Path, verified: bool) -> tuple[bool, str]:
    if not lock_path.exists():
        return True, "already gone"
    if not verified:
        return False, "cannot confirm no live holder (/proc unavailable) — refusing to delete"
    try:
        age = ctx.now - lock_path.stat().st_mtime
    except OSError:
        return True, "already gone"
    if age < GIT_LOCK_STALE_SECONDS:
        return True, "no longer stale"
    holder = has_open_holder(lock_path)
    if holder in (True, None):
        return False, f"holder check now inconclusive/live (holder={holder}) — refusing"
    lock_path.unlink()
    return True, f"removed stale lock {lock_path}"


def check_expired_cordon(ctx: WatchdogContext) -> list[Finding]:
    """Tier 1 item 5: an expired release cordon still present in
    ``paused_machines.json``."""
    data = _read_json(ctx.paused_machines_path)
    cordons = data.get("release_cordons") or {}
    findings = []
    for machine, entry in cordons.items():
        if not isinstance(entry, dict):
            continue
        expires_at = entry.get("expires_at") or 0
        if _cordon_active(expires_at, ctx.now):
            continue  # no expiry, or not expired yet
        findings.append(
            Finding(
                condition="expired-cordon",
                signature=f"expired-cordon:{machine}",
                tier=1,
                summary=f"release cordon for {machine} expired at {expires_at} and is still present",
                repair_fn=functools.partial(_repair_expired_cordon, machine=machine),
            )
        )
    return findings


def _repair_expired_cordon(ctx: WatchdogContext, *, machine: str) -> tuple[bool, str]:
    data = _read_json(ctx.paused_machines_path)
    cordons = data.get("release_cordons") or {}
    entry = cordons.get(machine)
    if not isinstance(entry, dict):
        return True, f"no cordon for {machine} anymore"
    expires_at = entry.get("expires_at") or 0
    if not expires_at or ctx.now < expires_at:
        return True, f"cordon for {machine} is no longer expired"
    del cordons[machine]
    data["release_cordons"] = cordons
    if ctx.dry_run:
        return True, f"[dry-run] would clear expired cordon for {machine}"
    _atomic_write_json(ctx.paused_machines_path, data)
    return True, f"cleared expired cordon for {machine}"


def _query_board_live_assignment(ctx: WatchdogContext, assignment_id: str) -> bool | None:
    if not ctx.board_url:
        return None
    url = ctx.board_url.rstrip("/") + "/board"
    try:
        with urllib.request.urlopen(url, timeout=ctx.http_timeout) as resp:  # noqa: S310
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    for a in data.get("assignments", []) or []:
        if (
            a.get("assignment_id") == assignment_id
            and a.get("machine_name") == ctx.hostname
            and a.get("status") in ("pending", "running")
        ):
            return True
    return False


def _tmux_session_alive(assignment_id: str) -> bool | None:
    if TMUX is None:
        return None
    try:
        result = subprocess.run(
            [TMUX, "has-session", "-t", f"coord-{assignment_id}"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


def check_orphaned_worktrees(ctx: WatchdogContext) -> list[Finding]:
    """Tier 1 item 6: a worktree with no live assignment.

    Deliberately conservative: skip anything younger than
    ``WORKTREE_RECENT_SECONDS``, skip symlinks, and only ever act on a
    *positive* "not live" confirmation from the board and (if available)
    tmux — never on "board unreachable." A reaped worktree is what
    detonated the editable install this watchdog exists to fix, so this is
    the one Tier-1 repair most worth erring conservative on.
    """
    if not ctx.worktrees_dir.is_dir():
        return []
    findings = []
    for wt in sorted(ctx.worktrees_dir.iterdir()):
        if wt.is_symlink() or not wt.is_dir():
            continue
        try:
            age = ctx.now - wt.stat().st_mtime
        except OSError:
            continue
        if age < WORKTREE_RECENT_SECONDS:
            continue
        assignment_id = wt.name
        live = _query_board_live_assignment(ctx, assignment_id)
        if live is not False:
            continue  # True (still live) or None (unverifiable) -> never act
        tmux_alive = _tmux_session_alive(assignment_id)
        if tmux_alive is True:
            continue
        findings.append(
            Finding(
                condition="orphaned-worktree",
                signature=f"orphaned-worktree:{assignment_id}",
                tier=1,
                summary=f"{wt} has no live assignment or tmux session ({age:.0f}s old)",
                suppress_keys=(assignment_id, f"orphaned-worktree:{assignment_id}"),
                repair_fn=functools.partial(_repair_orphaned_worktree, wt=wt, assignment_id=assignment_id),
            )
        )
    return findings


def _repair_orphaned_worktree(ctx: WatchdogContext, *, wt: Path, assignment_id: str) -> tuple[bool, str]:
    if not wt.is_dir() or wt.is_symlink():
        return True, "already gone"
    try:
        age = ctx.now - wt.stat().st_mtime
    except OSError:
        return True, "already gone"
    if age < WORKTREE_RECENT_SECONDS:
        return False, "no longer old enough — refusing"
    live = _query_board_live_assignment(ctx, assignment_id)
    if live is not False:
        return False, f"liveness re-check is {live!r} (not a confirmed 'not live') — refusing"
    if _tmux_session_alive(assignment_id) is True:
        return False, "tmux session is alive — refusing"
    if ctx.dry_run:
        return True, f"[dry-run] would remove {wt}"
    try:
        shutil.rmtree(wt)
    except OSError as exc:
        return False, f"rmtree raised {type(exc).__name__}: {exc}"
    return True, f"removed orphaned worktree {wt}"


TIER1_CHECKS: list[Check] = [
    Check("venv-rollback", 1, check_venv_rollback),
    Check("local-bin-symlink", 1, check_local_bin_symlink),
    Check("failed-unit", 1, check_failed_units),
    Check("stale-git-lock", 1, check_stale_git_lock),
    Check("expired-cordon", 1, check_expired_cordon),
    Check("orphaned-worktree", 1, check_orphaned_worktrees),
]


# ---------------------------------------------------------------------------
# Tier 2 — detect and escalate, never repair
# ---------------------------------------------------------------------------


def check_disabled_timers(ctx: WatchdogContext) -> list[Finding]:
    """Tier 2: a ``coord-*.timer`` that is disabled or masked.

    Never auto-fixed — see #2580's own worked example:
    ``coord-release-propagate.timer`` reads CRIT/disabled right now, and it
    is disabled *on purpose* (manual release rolls until the lane
    stabilises, see ``docs/AGENT_OPERATIONS.md``). Suppress the specific
    unit in ``watchdog-suppress.json`` to keep this from paging on an
    intentional operator decision.
    """
    if SYSTEMCTL is None:
        return []
    try:
        result = subprocess.run(
            [
                SYSTEMCTL,
                "--user",
                "list-unit-files",
                "--no-legend",
                "--plain",
                "coord-*.timer",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=_systemctl_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    findings = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        unit, state = parts[0], parts[1]
        if state in ("enabled", "static", "generated", "alias"):
            continue
        findings.append(
            Finding(
                condition="timer-disabled",
                signature=unit,
                tier=2,
                summary=f"{unit} is {state}",
                suppress_keys=(unit,),
            )
        )
    return findings


TIER2_CHECKS: list[Check] = [
    Check("timer-disabled", 2, check_disabled_timers),
    # Deliberately not implemented — see #2580's own reasoning for why each
    # is harder than it looks, ported here as an honest seam rather than a
    # check that would rubber-stamp:
    #   - version drift: needs an active-assignment check before any
    #     coord-agent restart, since headless workers are invisible to
    #     `coord sessions --remote` (tmux-only).
    #   - graph staleness / unit drift: the remedy is "review, then pull —
    #     not automatic"; auto-cp'ing units from a checkout that might
    #     itself be stale creates the exact drift it would claim to repair.
    #   - phantom `running` rows: liveness is a local tmux read (#1870) — a
    #     naive reaper on the wrong host kills a healthy drive it doesn't
    #     own. Would need "recorded machine == this machine" scoping to be
    #     safe at all.
]


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------


def process_finding(
    ctx: WatchdogContext, state: dict, suppressions: dict, finding: Finding
) -> Finding:
    suppressed, _entry = is_suppressed(suppressions, finding.suppress_keys, now=ctx.now)
    if suppressed:
        finding.suppressed = True
        state.pop(finding.signature, None)
        return finding

    if finding.tier == 2:
        return finding  # detect + report only, never repaired

    if ctx.cordon_active:
        finding.error = f"release cordon active for {ctx.hostname} ({ctx.cordon_reason}) — repair skipped"
        return finding

    entry = state.get(finding.signature) or {}
    if entry.get("consecutive", 0) >= ctx.rate_limit:
        finding.escalated = True
        return finding

    if finding.repair_fn is None:
        return finding

    if ctx.dry_run:
        finding.summary += " [dry-run: would repair]"
        return finding

    try:
        ok, detail = finding.repair_fn(ctx)
    except Exception as exc:  # noqa: BLE001 — a repair must never crash the sweep
        ok, detail = False, f"repair raised {type(exc).__name__}: {exc}"

    entry["consecutive"] = entry.get("consecutive", 0) + 1
    entry["last_attempt_at"] = ctx.now
    entry["last_ok"] = ok
    state[finding.signature] = entry

    if ok:
        finding.repaired = True
        finding.summary += f" -> {detail}"
    else:
        finding.error = detail
    return finding


def run_check(ctx: WatchdogContext, state: dict, suppressions: dict, check: Check) -> list[Finding]:
    findings = check.detect(ctx)
    seen = {f.signature for f in findings}
    prefix = f"{check.name}:"
    if check.tier == 1:
        for sig in list(state.keys()):
            if (sig == check.name or sig.startswith(prefix)) and sig not in seen:
                state.pop(sig, None)
    return [process_finding(ctx, state, suppressions, f) for f in findings]


def run_sweep(ctx: WatchdogContext) -> list[Finding]:
    suppressions = load_suppressions(ctx)
    state = load_state(ctx)
    findings: list[Finding] = []
    for check in TIER1_CHECKS + TIER2_CHECKS:
        findings.extend(run_check(ctx, state, suppressions, check))
    save_state(ctx, state)
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", default=os.environ.get("HOME", os.path.expanduser("~")))
    parser.add_argument("--coord-dir", default=None)
    parser.add_argument("--venv-dir", default=None)
    parser.add_argument("--local-bin-coord", default=None)
    parser.add_argument("--rollback-script", default=None)
    parser.add_argument("--hostname", default=None)
    parser.add_argument("--board-url", default=os.environ.get("COORD_BOARD_URL", "http://localhost:7435"))
    parser.add_argument("--http-timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--rate-limit", type=int, default=DEFAULT_RATE_LIMIT)
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="print findings as JSON instead of text")
    return parser.parse_args(argv)


def _print_report(findings: list[Finding], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
        return
    if not findings:
        print("fleet watchdog: clean sweep, nothing to report")
        return
    for f in findings:
        tag = "TIER1" if f.tier == 1 else "TIER2"
        if f.suppressed:
            status = "SUPPRESSED"
        elif f.escalated:
            status = "ESCALATED (rate-limited — needs a human)"
        elif f.repaired:
            status = "REPAIRED"
        elif f.tier == 2:
            status = "REPORTED"
        elif f.error:
            status = f"FAILED ({f.error})"
        else:
            status = "SKIPPED"
        print(f"[{tag}] {f.condition} ({f.signature}): {f.summary} — {status}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ctx = build_context(args)
    findings = run_sweep(ctx)
    _print_report(findings, as_json=args.json)
    # Tier 2 findings are never `repaired` (they're report-only by
    # construction), so this flags every unsuppressed tier-2 finding too —
    # which is the point: someone needs to look.
    needs_attention = any(not f.suppressed and not f.repaired for f in findings)
    return 1 if needs_attention else 0


if __name__ == "__main__":
    sys.exit(main())
