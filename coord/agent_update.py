"""Blue/green, version-pinned venv swap for `coord agent update` (#1241).

`POST /update` used to run `pip install --upgrade` **in place** on the live
`~/.coord-venv` (see the old ``coord.agent_app._do_update``). That leaves a
window — while pip is rewriting site-packages file by file — during which a
concurrent `coord` invocation can observe a *partial* install: some modules
already the new version, others still the old one. This repo hit that for
real: mid-upgrade, ``state.py`` had already been swapped to a version that
imports ``coord.board_service``, but ``board_service.py`` hadn't landed yet,
so a concurrent ``coord report-result`` crashed with ``ModuleNotFoundError``.
An update must be all-or-nothing.

The fix: never write into the venv that's live. Install the target version
into a **fresh** venv — one of two fixed "slots" next to the live one
(``~/.coord-venv.blue`` / ``~/.coord-venv.green``) — smoke-check it, and
only then flip a symlink so ``~/.coord-venv`` always resolves to one
*complete* slot, old or new, never a mix. Rename of a symlink onto an
existing path is atomic on POSIX (same filesystem), so any `coord`
invocation racing the flip sees either the fully-old or the fully-new
install — there is no observable in-between state.

Using exactly two named slots (rather than a fresh directory per release)
also gives rollback for free: the slot that was live before the swap is
left untouched, so it's still there — one generation back — until the
*next* update reuses it. See :func:`rollback`.

#2140: "the slot that's live" means two different things that usually —
but not always — agree: the slot ``venv_dir`` *symlinks to*, and the slot
whatever process is *actually running perform_update* was started from
(``sys.executable``). A swap flips the symlink without restarting anyone,
so the moment that happens the two diverge — normal on a fleet where
restarts are gated on a drain (#2138/#2136), not a rare race. If a second
update then reuses the slot the symlink no longer points at, it is
deleting the running caller's own interpreter and site-packages out from
under it: the subprocess spawn of ``sys.executable`` fails because the
path is now gone, cleanup deletes it a second time for good measure, and
the generation that was the rollback target is destroyed along with it.
Two independent guards below close this: :func:`perform_update` refuses
outright when the slot it would rebuild is the one backing its own
``sys.executable`` (recoverable — the caller just needs a restart first),
and venv creation always uses the *symlinked* slot's python rather than
``sys.executable``, so the tool building the new environment is never the
thing this update is about to delete.

#2121: ``sys.executable`` only ever answers for *this* process, and on
2026-08-11 that was not the process that mattered. ``coord-agent`` on
dellserver had been executing from ``~/.coord-venv.green`` since 02:32;
one update wrote blue and flipped onto it, and a second update — running
from somewhere else entirely, so the ``sys.executable`` guard above saw
nothing — then rebuilt **green**, the slot the live daemon was running
out of, replacing its ``site-packages`` mid-flight. The agent spent the
next six hours as a mixed-version process (0.5.32 in the modules it had
already imported, 0.5.36 in everything it imported later), and both
colours ended up on 0.5.36, so there was no rollback generation left
either. Three things land here as a result:

* :func:`processes_holding_slot` reads ``/proc`` for **any** live process
  running out of a slot, not just this one, and :func:`perform_update`
  refuses on a holder. "Nobody is running from the slot I am about to
  delete" is a property of the machine, not of the caller.
* :func:`_other_slot` compares *resolved* paths, so a ``venv_dir`` whose
  symlink target spells the same slot differently (a symlinked ``$HOME``,
  a relative link) can no longer be mistaken for "the other colour" and
  hand back the **active** slot as the rebuild target. The active colour
  is asserted immutable in :func:`perform_update` regardless.
* Every install — swap, refusal, or failure — writes an audit row naming
  its initiator (#1041). Reconstructing this incident took an hour of
  ``stat`` and journal timestamps precisely because nothing recorded who
  upgraded the box, when, or on whose behalf.

And :func:`assert_not_live_install` keeps a *test* from reaching the live
install at all, on the same principle (and with the same shape) as the
production-database guard in :mod:`coord.db`: an automated run gets a
sacrificial venv root or it gets an exception — not the daemon host's own
``~/.coord-venv``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from coord.platform_paths import venv_exe, venv_pip, venv_python

_log = logging.getLogger(__name__)

#: Suffixes for the two blue/green slots, relative to the live venv dir
#: (e.g. ``~/.coord-venv`` -> ``~/.coord-venv.blue`` / ``~/.coord-venv.green``).
_BLUE_SUFFIX = ".blue"
_GREEN_SUFFIX = ".green"

#: What the smoke check imports to prove the new install actually boots —
#: the two modules whose disagreement caused the ModuleNotFoundError this
#: whole mechanism exists to prevent (state.py -> board_service.py).
_SMOKE_IMPORTS = "coord.state, coord.commands.review"


#: The one venv path that is never a legitimate target for an automated
#: test: the path ``install-agent.sh`` creates and every
#: ``deploy/coord-*.service`` unit hardcodes as its ``ExecStart`` venv. Kept
#: as a bare name rather than a resolved Path so it follows ``$HOME`` in a
#: fixture that relocates it.
_LIVE_VENV_NAME = ".coord-venv"


class LiveInstallGuardError(RuntimeError):
    """A test tried to install into the live ``~/.coord-venv`` (#2121)."""


@dataclass(frozen=True)
class SlotHolder:
    """One live process running out of a blue/green slot (#2121).

    ``evidence`` is the literal ``/proc`` token that named the slot — an
    ``argv`` entry (``~/.coord-venv.green/bin/python3.12 ...``, the shape
    the 2026-08-11 incident was reconstructed from) or a ``VIRTUAL_ENV``
    environment entry — so a refusal can quote the thing it saw rather
    than asserting a conclusion.
    """

    pid: int
    source: str
    evidence: str
    cmdline: str = ""

    #: Max characters of ``cmdline`` to quote in a refusal message.
    _MAX_CMDLINE = 200

    def describe(self) -> str:
        """``pid N (argv: ...)``, short enough to read in an error message.

        Long command lines are elided in the *middle*, not truncated at the
        end: both halves carry the information an operator needs — the head
        names the slot and interpreter, the tail names the subcommand
        (``... coord agent --config ...``), which is what says *which*
        service is holding the slot. Cutting the tail throws that away
        precisely when the paths are longest.
        """
        cmd = self.cmdline or self.evidence
        if len(cmd) > self._MAX_CMDLINE:
            keep = (self._MAX_CMDLINE - 5) // 2
            cmd = f"{cmd[:keep]} ... {cmd[-keep:]}"
        return f"pid {self.pid} ({self.source}: {cmd})"


def _proc_tokens(entry: Path, name: str) -> list[str]:
    """NUL-separated ``/proc/<pid>/{cmdline,environ}``, or ``[]``.

    Best-effort by construction: a pid that exits mid-scan, or one owned by
    another user whose ``environ`` we may not read, must not turn a version
    upgrade into a traceback.
    """
    try:
        raw = (entry / name).read_bytes()
    except (OSError, ValueError):
        return []
    return [tok for tok in raw.decode("utf-8", "replace").split("\0") if tok]


def processes_holding_slot(
    slot: Path,
    *,
    proc_root: Path = Path("/proc"),
    exclude_pids: tuple[int, ...] = (),
) -> list[SlotHolder]:
    """Every live process executing out of *slot*, newest pid last.

    #2121: the question ``perform_update`` has to answer before it
    ``rmtree``s a slot is "is anything running from this directory", and
    ``sys.executable`` answers it only for the calling process. On
    2026-08-11 the caller was not the victim — a long-lived ``coord-agent``
    was — so the guard keyed on ``sys.executable`` never fired.

    Detection reads ``argv`` and ``VIRTUAL_ENV``, deliberately **not**
    ``/proc/<pid>/exe``: for a PEP 405 venv, ``bin/python3.12`` is a
    symlink out to the shared base interpreter, and ``exe`` is the kernel's
    fully-resolved path — ``/usr/bin/python3.12``, which names no slot at
    all. ``argv[0]`` keeps the literal path the process was started with
    (``/home/john/.coord-venv.green/bin/python3.12``), which is exactly the
    evidence the incident was reconstructed from.

    Returns ``[]`` — never raises — when ``/proc`` is absent or unreadable
    (non-Linux, a container without ``hidepid`` access). That is a real
    blind spot and is why it is not the only guard: :func:`perform_update`
    still refuses on its own ``sys.executable`` and on the active colour.
    """
    try:
        entries = sorted(
            (p for p in proc_root.iterdir() if p.name.isdigit()),
            key=lambda p: int(p.name),
        )
    except OSError:
        return []

    prefixes = {str(slot) + os.sep}
    exact = {str(slot)}
    try:
        resolved = str(slot.resolve())
    except OSError:
        resolved = None
    if resolved:
        prefixes.add(resolved + os.sep)
        exact.add(resolved)

    def _hit(token: str) -> bool:
        return token in exact or any(token.startswith(p) for p in prefixes)

    holders: list[SlotHolder] = []
    skip = set(exclude_pids)
    for entry in entries:
        pid = int(entry.name)
        if pid in skip:
            continue
        argv = _proc_tokens(entry, "cmdline")
        if not argv:
            # Kernel threads have an empty cmdline; so does a pid that
            # exited between the listdir and the read.
            continue
        cmdline = " ".join(argv)
        found = next((tok for tok in argv if _hit(tok)), None)
        source = "argv"
        if found is None:
            for env_entry in _proc_tokens(entry, "environ"):
                key, sep, value = env_entry.partition("=")
                if sep and key == "VIRTUAL_ENV" and _hit(value):
                    found, source = env_entry, "environ"
                    break
        if found is not None:
            holders.append(
                SlotHolder(pid=pid, source=source, evidence=found, cmdline=cmdline)
            )
    return holders


def assert_not_live_install(venv_dir: Path) -> None:
    """Refuse, under pytest, to touch the machine's live ``~/.coord-venv``.

    #2121 item 3: *"a test must not be able to reach the live install"*.
    The mechanism is deliberately the one this repo already uses for the
    same class of accident one layer down — :func:`coord.db._open`'s
    ``ProductionDatabaseGuardError`` (#1960), which refuses to open the
    real ``~/.coord/coord.db`` when ``PYTEST_CURRENT_TEST`` is set. Same
    reasoning, same trigger, same shape of message: an automated run that
    resolves the production artifact instead of an isolated one is a bug in
    the test, and it should fail loudly at the moment it reaches for it
    rather than after it has rewritten the daemon host's runtime.

    There is deliberately **no** escape hatch on this path: a test never
    has a legitimate reason to install into the daemon host's own runtime,
    so the answer to "but my test needs a real venv" is a *sacrificial
    target*, not a bypass — a throwaway blue/green root under ``tmp_path``,
    which the ``sacrificial_venv_root`` fixture in ``tests/conftest.py``
    builds and points ``COORD_VENV_DIR`` at. That is the mechanism #2121
    item 3 asks to be named: the real code path runs against a real
    directory tree that is not ``~/.coord-venv``, and reaching for
    ``~/.coord-venv`` raises here instead of succeeding quietly.

    Outside pytest this is a no-op — production upgrades are guarded by the
    active-colour and live-holder checks in :func:`perform_update`, not by
    this.

    Compared with :func:`_same_path` (resolved, not a bare string compare):
    a symlinked ``$HOME`` or an equivalently-but-differently-spelled
    ``venv_dir`` must not let a test slip past this guard the same way
    :func:`_other_slot` must not mistake it for "the other colour" (#2121).
    """
    marker = os.environ.get("PYTEST_CURRENT_TEST")
    if not marker:
        return

    live = Path.home() / _LIVE_VENV_NAME
    blue, green = _slots(live)
    for candidate in (live, blue, green):
        if not _same_path(venv_dir, candidate):
            continue
        raise LiveInstallGuardError(
            f"Refusing to install into the live agent venv at {venv_dir} "
            f"while running under pytest (PYTEST_CURRENT_TEST={marker!r}). "
            "A test (or a subprocess it spawned) resolved this machine's "
            "real ~/.coord-venv instead of an isolated one — that is how a "
            "live coord-agent's site-packages got replaced underneath it on "
            "2026-08-11 (#2121). Fix: use the `sacrificial_venv_root` "
            "fixture (a throwaway blue/green root under tmp_path), or point "
            "COORD_VENV_DIR at your own tmp_path."
        )


def cli_initiator(command: str) -> str:
    """A self-describing initiator string for *command* (#2121 item 2).

    Names the operator, the host they ran from and the pid, so an audit row
    for a fleet roll points back at a specific invocation on a specific box
    rather than at "something POSTed /update". Deliberately built from
    cheap, always-available facts — a missing ``$USER`` or an unresolvable
    hostname degrades a field, never raises.
    """
    try:
        import getpass  # noqa: PLC0415

        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = os.environ.get("USER") or "unknown-user"
    try:
        import socket  # noqa: PLC0415

        host = socket.gethostname()
    except Exception:  # noqa: BLE001
        host = "unknown-host"
    return f"{command} ({user}@{host} pid {os.getpid()})"


def _audit_install(
    *,
    initiator: str | None,
    outcome: str,
    venv_dir: Path,
    summary: str,
    details: dict[str, object],
) -> None:
    """Record one venv install attempt on the audit trail (#1041, #2121 item 2).

    Best-effort, exactly like every other ``record_audit`` call site — a
    version upgrade must not fail because the audit row could not be
    written. But *every* outcome goes through here, including refusals and
    failures: #2096's invariant is that no surface reports a roll it did
    not confirm, and an audit trail that only records successes is a
    surface that does exactly that.

    ``actor`` is the initiator the caller named. When nobody named one it
    is recorded as ``"unattributed"`` rather than guessed — "we do not know
    who did this" is the finding #2121 is about, and it should be legible
    in the row instead of laundered into a plausible-looking name.
    """
    from coord.audit import record_audit  # noqa: PLC0415 — avoid an import cycle

    try:
        import socket  # noqa: PLC0415

        machine = socket.gethostname()
    except Exception:  # noqa: BLE001
        machine = None
    record_audit(
        tier="operational",
        category="deploy",
        event_type="venv_install",
        actor=initiator or "unattributed",
        machine=machine,
        summary=summary,
        details={"outcome": outcome, "venv_dir": str(venv_dir), **details},
    )


@dataclass
class UpdateResult:
    """Outcome of :func:`perform_update` or :func:`rollback`.

    ``new_version`` is read from the new slot *before* it goes live (via the
    smoke check), so it's available even though the caller's own
    ``importlib.metadata`` read of ``~/.coord-venv`` won't reflect it until
    that process's next fresh read after the swap.
    """

    ok: bool
    swapped: bool
    slot: Path | None = None
    previous_slot: Path | None = None
    new_version: str | None = None
    error: str | None = None
    log: str = ""


def _slots(venv_dir: Path) -> tuple[Path, Path]:
    """Return the ``(blue, green)`` sibling directories for *venv_dir*."""
    parent = venv_dir.parent
    name = venv_dir.name
    return parent / f"{name}{_BLUE_SUFFIX}", parent / f"{name}{_GREEN_SUFFIX}"


def current_slot(venv_dir: Path) -> Path | None:
    """Return the slot ``venv_dir`` currently resolves to.

    ``None`` when *venv_dir* doesn't exist yet, or exists as a plain
    directory that hasn't been migrated to the blue/green layout (see
    :func:`ensure_symlink_layout`) — every pre-#1241 install starts this
    way, since ``install-agent.sh`` creates ``~/.coord-venv`` as a real
    directory.
    """
    if not venv_dir.is_symlink():
        return None
    target = venv_dir.readlink()
    if not target.is_absolute():
        target = (venv_dir.parent / target).resolve()
    return target


def ensure_symlink_layout(venv_dir: Path) -> Path:
    """Migrate *venv_dir* to the blue/green symlink layout if it isn't already.

    Idempotent: if *venv_dir* is already a symlink, just returns its current
    target. Otherwise renames the existing plain directory into the
    ``.blue`` slot and replaces *venv_dir* with a symlink pointing at it.
    This is the one-time, one-machine migration every pre-#1241 install
    needs; every update after that stays in the symlink layout, so this
    becomes a no-op for the rest of that machine's life.
    """
    existing = current_slot(venv_dir)
    if existing is not None:
        return existing
    if not venv_dir.exists():
        raise FileNotFoundError(f"no venv at {venv_dir} to migrate")
    blue, _green = _slots(venv_dir)
    if blue.exists():
        # Should be unreachable — `blue`/`green` only ever come into being
        # via this function or `perform_update`, both gated on `venv_dir`
        # not already being a symlink. Refuse rather than clobber whatever
        # is there.
        raise FileExistsError(
            f"{blue} already exists — refusing to migrate {venv_dir} over it"
        )
    venv_dir.rename(blue)
    venv_dir.symlink_to(blue, target_is_directory=True)
    return blue


def _same_path(a: Path, b: Path) -> bool:
    """``a`` and ``b`` name the same directory, symlinks and all.

    #2121: a plain ``==`` on ``Path`` is a *string* comparison, and the two
    sides here do not come from the same place — ``active`` is whatever
    ``~/.coord-venv``'s symlink literally says, while the slots are built
    by string-appending ``.blue``/``.green`` to ``venv_dir``. A symlinked
    ``$HOME``, a ``venv_dir`` passed with a trailing component that
    resolves elsewhere, or a link written with a different but equivalent
    spelling makes those disagree — and :func:`_other_slot` then hands back
    the colour that is *already live* as the one to rebuild, which is the
    exact mutation this whole module exists to prevent.
    """
    if str(a) == str(b):
        return True
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _other_slot(venv_dir: Path, active: Path) -> Path:
    blue, green = _slots(venv_dir)
    return green if _same_path(active, blue) else blue


def _slot_backing_interpreter(venv_dir: Path, interpreter: Path) -> Path | None:
    """Return whichever blue/green slot *interpreter* physically lives under.

    ``None`` if *interpreter* resolves to neither slot (e.g. a dev/editable
    install not using the blue/green layout at all).

    #2140: deliberately keyed off the slot *directories* themselves, not
    off :func:`current_slot` — ``sys.executable`` is the literal path baked
    into a process at start time (e.g. ``~/.coord-venv.blue/bin/python3``)
    and stays pinned to that slot for the process's whole life, even after
    a later swap moves the ``venv_dir`` symlink onto the other slot. Those
    two — "what the symlink currently says" and "what this process is
    actually running from" — regularly disagree for hours on a fleet where
    restarts are gated on a drain (#2138/#2136); this function answers the
    second question, which is the one that matters before deleting a slot.

    #2140 review: this must NOT call ``.resolve()`` on *interpreter*
    wholesale. ``bin/python3`` inside a real ``python -m venv`` slot is
    itself a symlink chain (PEP 405) ending at the shared base
    interpreter — e.g. ``~/.coord-venv.blue/bin/python3 -> ... ->
    /usr/bin/python3.12``. Fully resolving that follows the chain straight
    out of the slot to a path that is neither blue nor green, so the
    comparison below would silently never match and this refuse-guard
    would never fire against a real venv — verified empirically against
    real ``python3 -m venv`` output. Only the *directory* the interpreter
    lives in is resolved (to normalize e.g. a symlinked home directory
    somewhere above the slot); the interpreter's own filename is kept
    exactly as given, so the comparison stays anchored inside the slot
    that path actually names instead of following through to whatever
    that file ultimately points at.
    """
    try:
        resolved = interpreter.parent.resolve() / interpreter.name
    except OSError:
        return None
    blue, green = _slots(venv_dir)
    for slot in (blue, green):
        try:
            resolved.relative_to(slot.resolve())
        except (OSError, ValueError):
            continue
        return slot
    return None


def running_slot(venv_dir: Path) -> Path | None:
    """Public wrapper: which blue/green slot is *this process* — the one
    calling this function, via its own ``sys.executable`` — actually
    running from (#2139).

    Exists so callers outside this module (the idle self-restart watcher in
    :mod:`coord.agent_app`) can ask the same question
    :func:`_slot_backing_interpreter` already answers for
    :func:`perform_update`'s own #2140 guard, without reaching into a
    private helper. Compare the result against :func:`current_slot`: equal
    means this process is already running the slot the symlink points at;
    different means a swap landed since this process started and it has a
    newer (or at least *other*) generation staged and waiting.
    """
    return _slot_backing_interpreter(venv_dir, Path(sys.executable))


def _atomic_swap(venv_dir: Path, new_slot: Path) -> None:
    """Flip *venv_dir* to point at *new_slot* in one filesystem operation.

    Builds the new symlink at a temp path next to *venv_dir* and renames it
    directly onto *venv_dir* — ``rename()`` replacing an existing path is
    atomic on POSIX when both are on the same filesystem (true here: both
    are siblings under the same parent directory), so any `coord`
    invocation racing this always sees either the old, complete slot or the
    new, complete slot — never a half-updated ``venv_dir``.
    """
    tmp_link = venv_dir.parent / f".{venv_dir.name}.next-link"
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    tmp_link.symlink_to(new_slot, target_is_directory=True)
    tmp_link.replace(venv_dir)


def _smoke_check(slot: Path, *, target_version: str | None) -> tuple[bool, str | None, str]:
    """Run the two smoke checks against a freshly-installed *slot*.

    Returns ``(ok, detected_version, log)``. ``detected_version`` is parsed
    out of the import-check's own ``importlib.metadata`` read (the same
    mechanism :func:`_installed_version` uses) so a single subprocess call
    covers both "does it import" and "what version is this."

    #2103/#2106: the version print resolves via
    ``coord.dist_name.resolve_installed`` rather than a hardcoded
    ``m.version('claude-coordinator')`` — *slot* was just installed from a
    pkg spec that itself resolves via the same module (see
    ``coord.agent_app._agent_pkg_spec``), so a name mismatch here would
    raise inside the new slot's own interpreter for no reason. Raising when
    the name isn't installed at all is still the right behavior here (not
    caught) — that means the fresh install is genuinely broken, which is
    exactly what a failed smoke check should report.
    """
    python = venv_python(slot)
    coord_bin = venv_exe(slot, "coord")
    lines: list[str] = []

    try:
        result = subprocess.run(
            [
                str(python),
                "-c",
                f"import {_SMOKE_IMPORTS}\n"
                "from coord.dist_name import resolve_installed\n"
                "print(resolve_installed().version)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, None, f"smoke import check raised {type(exc).__name__}: {exc}"

    lines.append(
        f"$ python -c 'import {_SMOKE_IMPORTS}'\n{result.stdout}{result.stderr}"
    )
    if result.returncode != 0:
        return False, None, "\n".join(lines)
    detected_version = result.stdout.strip() or None

    try:
        result = subprocess.run(
            [str(coord_bin), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        lines.append(f"coord --version raised {type(exc).__name__}: {exc}")
        return False, detected_version, "\n".join(lines)

    lines.append(f"$ coord --version\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        return False, detected_version, "\n".join(lines)

    if target_version and target_version not in (result.stdout + result.stderr):
        lines.append(
            f"version mismatch: expected {target_version!r} in `coord --version` output"
        )
        return False, detected_version, "\n".join(lines)

    return True, detected_version, "\n".join(lines)


def perform_update(
    venv_dir: Path,
    pkg_spec: str,
    *,
    target_version: str | None = None,
    pip_timeout: float = 180.0,
    initiator: str | None = None,
    proc_root: Path = Path("/proc"),
) -> UpdateResult:
    """Install ``pkg_spec`` (optionally pinned to *target_version*) into a
    fresh slot, smoke-check it, and atomically swap it into place.

    Never mutates *venv_dir*'s currently-live slot. On any failure — venv
    creation, pip, or the smoke check — the half-built next slot is removed
    and *venv_dir* is left exactly as it was; the caller's process keeps
    running the old code with no restart needed. Returns a failed
    :class:`UpdateResult` rather than raising, except for genuinely
    unexpected setup errors (e.g. *venv_dir* doesn't exist at all).

    #2140: also refuses — before touching anything — if *next_slot* (the
    one about to be rebuilt) is the slot backing this very process's own
    ``sys.executable``. That happens when a previous swap flipped the
    symlink without a restart following it; proceeding would delete the
    caller's own running interpreter and site-packages, and destroy the
    rollback generation along with it. A refusal here is recoverable (the
    caller just needs restarting first); reaching into that slot is not.

    #2121 widens that from "this process" to "any process on this machine",
    and adds an explicit assertion that *next_slot* is never the colour
    ``venv_dir`` currently resolves to. Both refusals are recorded on the
    audit trail alongside the successes, named to *initiator*.
    """
    log_parts: list[str] = []
    assert_not_live_install(venv_dir)
    active = ensure_symlink_layout(venv_dir)
    next_slot = _other_slot(venv_dir, active)

    def _refuse(error: str, **details: object) -> UpdateResult:
        _log.error("perform_update refused: %s", error)
        _audit_install(
            initiator=initiator,
            outcome="refused",
            venv_dir=venv_dir,
            summary=f"venv install REFUSED: {error}",
            details={
                "pkg_spec": pkg_spec,
                "target_version": target_version,
                "active_slot": str(active),
                "next_slot": str(next_slot),
                **details,
            },
        )
        return UpdateResult(ok=False, swapped=False, error=error)

    # ── the active colour is immutable ───────────────────────────────────
    # #2121 item 1. `_other_slot` is *supposed* to make this unreachable,
    # but the whole incident is that the environment a live process was
    # executing from got rebuilt anyway, so this is asserted rather than
    # assumed: whatever path arithmetic happens above, the colour
    # `venv_dir` resolves to right now is never the colour we rmtree.
    if _same_path(next_slot, active):
        return _refuse(
            f"refusing to update: {next_slot} is the colour {venv_dir} "
            f"currently resolves to ({active}). An upgrade writes the "
            "INACTIVE colour and then moves the symlink — rebuilding the "
            "active one replaces the environment live processes are "
            "executing from and destroys the rollback generation (#2121)."
        )

    running_slot = _slot_backing_interpreter(venv_dir, Path(sys.executable))
    if running_slot is not None and _same_path(running_slot, next_slot):
        return _refuse(
            f"refusing to update: this process's own interpreter "
            f"({sys.executable}) is running from {next_slot}, the slot "
            "this update would delete and rebuild. venv_dir currently "
            f"symlinks to {active}, so a prior swap flipped it without "
            "this process restarting — restart the caller (or wait for "
            "idle self-restart, #2139) and retry (#2140)."
        )

    # ── nothing else may be running from the slot either ─────────────────
    # #2121: this is the guard that was missing on 2026-08-11. The victim
    # was a `coord-agent` that had been executing from `~/.coord-venv.green`
    # since 02:32; the updater was a different process entirely, so the
    # `sys.executable` check above saw nothing to object to and green was
    # rebuilt underneath a live daemon.
    holders = processes_holding_slot(next_slot, proc_root=proc_root)
    if holders:
        listed = "; ".join(h.describe() for h in holders[:5])
        if len(holders) > 5:
            listed += f"; ... and {len(holders) - 5} more"
        return _refuse(
            f"refusing to update: {len(holders)} live process(es) are "
            f"running out of {next_slot}, the slot this update would delete "
            f"and rebuild — {listed}. Replacing a running process's "
            "site-packages leaves it executing a mix of two versions (the "
            "2026-08-11 dellserver incident, #2121). Restart or stop those "
            "processes — `systemctl --user restart coord-agent` for the "
            "agent — and retry.",
            holders=[
                {"pid": h.pid, "source": h.source, "cmdline": h.cmdline}
                for h in holders[:20]
            ],
        )

    # Always build fresh — a stale, possibly half-built slot left over from
    # an interrupted update two generations back must never be reused.
    if next_slot.exists():
        shutil.rmtree(next_slot, ignore_errors=True)

    def _fail(error: str) -> UpdateResult:
        shutil.rmtree(next_slot, ignore_errors=True)
        _audit_install(
            initiator=initiator,
            outcome="failed",
            venv_dir=venv_dir,
            summary=f"venv install FAILED: {error}",
            details={
                "pkg_spec": pkg_spec,
                "target_version": target_version,
                "active_slot": str(active),
                "next_slot": str(next_slot),
            },
        )
        return UpdateResult(ok=False, swapped=False, error=error, log="\n".join(log_parts))

    # #2140: build with the *symlinked* slot's python, not sys.executable —
    # `active` is guaranteed to differ from `next_slot` (they're the two
    # distinct blue/green slots), so the interpreter doing the building can
    # never be the thing this update is about to rmtree, regardless of
    # which slot the calling process itself happens to be running from.
    builder_python = venv_python(active)
    if not builder_python.exists():
        builder_python = Path(sys.executable)
    try:
        result = subprocess.run(
            [str(builder_python), "-m", "venv", str(next_slot)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        return _fail(f"venv creation timed out: {exc}")
    log_parts.append(f"$ python -m venv {next_slot}\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        return _fail(f"venv creation failed (exit {result.returncode})")

    pip = str(venv_pip(next_slot))
    install_spec = f"{pkg_spec}=={target_version}" if target_version else pkg_spec
    try:
        result = subprocess.run(
            [pip, "install", "--no-cache-dir", install_spec],
            capture_output=True,
            text=True,
            timeout=pip_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _fail(f"pip install timed out: {exc}")
    log_parts.append(f"$ pip install --no-cache-dir {install_spec}\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        return _fail(f"pip install failed (exit {result.returncode})")

    ok, new_version, smoke_log = _smoke_check(next_slot, target_version=target_version)
    log_parts.append(smoke_log)
    if not ok:
        return _fail("smoke check failed on the new install; see log")

    previous = active
    _atomic_swap(venv_dir, next_slot)
    _audit_install(
        initiator=initiator,
        outcome="swapped",
        venv_dir=venv_dir,
        summary=(
            f"venv install: {pkg_spec} -> {new_version or target_version or '?'} "
            f"into {next_slot.name}; {venv_dir.name} swapped "
            f"{previous.name} -> {next_slot.name}"
        ),
        details={
            "pkg_spec": pkg_spec,
            "target_version": target_version,
            "new_version": new_version,
            "slot": str(next_slot),
            "previous_slot": str(previous),
        },
    )
    return UpdateResult(
        ok=True,
        swapped=True,
        slot=next_slot,
        previous_slot=previous,
        new_version=new_version,
        log="\n".join(log_parts),
    )


def rollback(venv_dir: Path, *, initiator: str | None = None) -> UpdateResult:
    """Flip *venv_dir* back onto the previous generation, if one exists.

    The previous slot is smoke-checked before the swap — a rollback that
    would land on a broken install is refused, leaving the current
    (presumably also broken, but at least known) slot in place rather than
    trading one failure for another.

    Nothing is deleted here (only the symlink moves), so there is no
    live-process *deletion* hazard to guard the way :func:`perform_update`
    must — but :func:`assert_not_live_install` is a separate guard with a
    separate job (per its own docstring: keep *any* test, destructive or
    not, from reaching the machine's real ``~/.coord-venv``), and this
    function is reachable the exact same way ``perform_update`` is — the
    ``/rollback`` HTTP handler calls it with the same ``_venv_dir()``-
    resolved path, and it mutates the live symlink via the same
    :func:`_atomic_swap`. So it gets the same guard, first line, same as
    ``perform_update``. The swap still changes which code the machine's
    *next* process start runs, so it is audited on the same waist as an
    install (#2121 item 2).
    """
    assert_not_live_install(venv_dir)
    active = current_slot(venv_dir)
    if active is None:
        return UpdateResult(
            ok=False, swapped=False, error=f"{venv_dir} is not a migrated blue/green venv"
        )
    previous = _other_slot(venv_dir, active)
    if not previous.exists():
        return UpdateResult(
            ok=False, swapped=False, error=f"no previous generation at {previous}"
        )

    ok, version, log = _smoke_check(previous, target_version=None)
    if not ok:
        return UpdateResult(
            ok=False,
            swapped=False,
            error=f"previous slot {previous} fails its smoke check — refusing to roll back onto it",
            log=log,
        )

    _atomic_swap(venv_dir, previous)
    _audit_install(
        initiator=initiator,
        outcome="rolled_back",
        venv_dir=venv_dir,
        summary=(
            f"venv rollback: {venv_dir.name} swapped {active.name} -> "
            f"{previous.name} (now {version or '?'})"
        ),
        details={
            "new_version": version,
            "slot": str(previous),
            "previous_slot": str(active),
        },
    )
    return UpdateResult(
        ok=True,
        swapped=True,
        slot=previous,
        previous_slot=active,
        new_version=version,
        log=log,
    )
