"""``coord app-drive`` (#3590): the sanctioned, runnable entry point a
bugbash lane worker drives the real app through — this module is the
shared plumbing new in this issue; the CLI surface lives in
:mod:`coord.commands.app_drive`.

**Why this exists.** #3566 added a HARD RULE telling a lane worker to
"drive the app ONLY through this lane's own driver" and never improvise an
input-injection helper — but named three Python *module* paths
(``coord.mac_native_driver`` etc.) that are not tools, not CLI commands,
and not reachable from a worker's Bash tool at all. Every real bugbash run
since then came back with zero journeys driven: workers correctly refused
to improvise, and correctly found nothing resembling the named modules in
their own tool list. This module (plus
:mod:`coord.tui_pty_driver`'s ``TuiPtySession``,
:mod:`coord.mac_native_driver`'s ``MacNativeSession``,
:mod:`coord.win_native_driver`'s ``WinNativeSession``,
:mod:`coord.gtk_native_driver`'s ``GtkNativeSession``) is that missing
entry point.

**Design choice: a persistent per-session daemon + a short-lived control
connection per verb.** A worker's Bash tool runs one command at a time —
there is no way to keep a single process's stdin open across separate tool
calls, so "keep a session alive across calls" requires the session itself
to live in its OWN process, outside the lifetime of any one ``coord
app-drive`` invocation. ``open`` spawns that process (:mod:`coord
.app_drive_daemon`) once; it listens on an ephemeral localhost TCP port
(works identically on Linux/macOS/the dell64 win-native WSL bridge — no
``AF_UNIX`` availability gamble on Windows) for one-shot JSON-line
connections, each carrying exactly one verb. ``send``/``screen``/``probe``/
``close`` are each a fresh, short connection; the app being driven (a real
pty child, or a real native GUI process) stays open underneath for the
WHOLE session, held by the daemon.

**Teardown is guaranteed two ways, not one:**

1. ``close`` sends the daemon a ``"close"`` op, which tears its backend
   down (the exact same confirmed terminate-then-kill :meth:`PtyChild.close`
   already gives every other tui-pty caller, armed with the #3583
   kernel-level parent-death signal on Linux; a plain, confirmed
   ``kill(pid)`` for a native session) and then exits — :func:`close_session`
   does not report success until it has OBSERVED the daemon process
   actually gone (#2096: a sent signal is not a confirmed death).
2. Absent an explicit ``close`` (the worker's own session ends, a crash,
   a network partition), the daemon self-expires after *idle_timeout*
   seconds with no command (default 30 minutes) — see
   :mod:`coord.app_drive_daemon`'s accept-loop — tearing its backend down
   the same way. This is deliberately NOT process-group-based: the daemon
   is spawned detached (:func:`open_session` passes `start_new_session=True`
   on POSIX, `CREATE_NEW_PROCESS_GROUP` on Windows) so it survives
   independently of whatever process group the ``coord app-drive open``
   invocation itself ran in — exactly what "session alive across calls"
   requires when a Bash tool reaps its own command's process group on
   completion/timeout (a common sandbox configuration, and the reason
   #3583 exists at all: killing that group must not take the daemon with
   it). The idle self-expiry is what prevents that same detachment from
   leaking a session forever if nobody ever calls ``close``.
3. :func:`close_session` does not stop at observing the *daemon's* own
   pid — it also re-observes (and, if necessary, directly signals) the
   real app/pty-child pid the daemon reported at ``open`` time
   (``SessionHandle.app_pid``), so a daemon that had to be SIGKILLed
   before its own ``finally: backend.close()`` ever ran does not leave
   the driven app itself running underneath (#3590 review: "unconfirmed
   success is a defect", epic #2096).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from coord.platform_paths import default_coord_dir

#: Every lane kind this module knows how to drive — kept in sync BY HAND
#: with :data:`coord.bugbash.LANE_DRIVER_KINDS` (this module intentionally
#: does not import :mod:`coord.bugbash` — the dependency runs the other way,
#: :mod:`coord.bugbash`/:mod:`coord.commands.bugbash` name a command this
#: module serves, not the reverse).
APP_DRIVE_KINDS: tuple[str, ...] = ("tui-pty", "win-native", "mac-native", "gtk-native")

#: Default: a session nobody ever explicitly `close`s is torn down this many
#: seconds after its last command — long enough for a thorough exploration
#: (the #3569 incident this repo already tolerates for a whole bugbash lane
#: is measured in tens of minutes), short enough that an abandoned session
#: from a crashed/killed worker doesn't orphan a real GUI process/pty child
#: for the life of the host.
DEFAULT_IDLE_TIMEOUT = 1800.0


class AppDriveError(Exception):
    """Raised for any ``coord app-drive`` client-side failure: a session
    file that doesn't exist or is stale, a daemon that refused/dropped the
    connection, or a backend verb that itself raised."""


class AppDriveUnavailableError(AppDriveError):
    """``open`` observed a locked/absent session (#3510) or a missing
    permission grant (#3566) BEFORE launching anything — the native-lane
    environment condition the bugbash HARD RULE tells a worker to report
    as unavailable, never improvise past. Distinct from every other
    :class:`AppDriveError` so a caller (``coord app-drive <kind> open``)
    can print the exact ``reason`` text a worker folds straight into its
    own ```` ```bugbash-unavailable ```` fence.

    This is the ONE definition — :mod:`coord.app_drive_daemon` imports it
    from here rather than keeping its own copy, so a future in-process
    caller of :func:`coord.app_drive_daemon._build_backend` can never end
    up catching the wrong class for what is, on the wire, the exact same
    signal (a ready-file ``{"error": "unavailable", ...}``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _sessions_dir() -> Path:
    d = default_coord_dir() / "app-drive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_file(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.json"


@dataclass(frozen=True)
class SessionHandle:
    """What :func:`open_session` hands back, and what every later verb
    (:func:`send_command`/:func:`close_session`) re-reads from disk by
    *session_id* — never trusted as a long-lived in-memory object, since
    each CLI invocation is a fresh process with no memory of the last one.

    ``app_pid`` is the backend's own best-known pid for the real app/pty
    child it launched (``getattr(backend, "pid", None)`` in
    :mod:`coord.app_drive_daemon`'s ready-file write) — distinct from
    ``pid``, the app-drive *daemon's* own pid. ``None`` when a backend has
    no such pid to report. :func:`close_session` re-observes (and, if
    necessary, directly signals) THIS pid too, not just the daemon's, so a
    SIGKILLed daemon cannot silently leave the real app running (#3590
    review). ``token`` authenticates every command sent to the daemon's
    control socket (#3590 review: the socket has no other authentication,
    and the port is already world-readable from this very session file —
    a stolen token is no bigger a leak than a stolen port, but it does
    stop an unrelated local process from merely *guessing* the port and
    injecting commands).

    ``bridge`` is ``True`` only for a ``win-native`` session opened on a
    WSL host (#3611) — the daemon this session names was spawned on the
    real Windows-side interpreter via :mod:`coord.win_native_bridge`, so
    ``pid``/``app_pid`` are genuine Windows PIDs, not Linux ones.
    Everything that signals/probes those pids (:func:`_pid_alive`,
    :func:`close_session`) must branch on this — a Windows PID is not a
    Linux PID, so a POSIX ``os.kill``/``/proc`` probe against one is not
    merely wrong, it can read as a false "alive" (collision with an
    unrelated, actually-alive Linux PID sharing the same small
    integer)."""

    session_id: str
    kind: str
    pid: int
    port: int
    token: str
    app_pid: int | None = None
    bridge: bool = False


def _write_session_file(handle: SessionHandle) -> None:
    path = _session_file(handle.session_id)
    # #3590 review (nit): a distinct suffix from the daemon's own
    # `<id>.ready` -> `<id>.tmp` so the two atomic-write temp files can
    # never collide even if their write windows ever overlapped.
    tmp = path.with_suffix(".session.tmp")
    tmp.write_text(json.dumps({
        "session_id": handle.session_id, "kind": handle.kind,
        "pid": handle.pid, "port": handle.port, "token": handle.token,
        "app_pid": handle.app_pid, "bridge": handle.bridge,
    }))
    tmp.replace(path)


def load_session(session_id: str) -> SessionHandle:
    """Read back a previously `open`ed session's registry entry.

    Raises :class:`AppDriveError` — never a bare ``FileNotFoundError``/
    ``KeyError`` — when *session_id* is unknown or its file is corrupt, so
    every CLI verb gets one consistent "no such session" message."""
    path = _session_file(session_id)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise AppDriveError(f"no such app-drive session {session_id!r}: {e}") from e
    try:
        return SessionHandle(
            session_id=raw["session_id"], kind=raw["kind"],
            pid=int(raw["pid"]), port=int(raw["port"]),
            token=raw.get("token", ""),
            app_pid=int(raw["app_pid"]) if raw.get("app_pid") is not None else None,
            # `.get(..., False)`: a session file written before #3611 has
            # no `bridge` key at all — absence means "not a bridge
            # session" (the only meaning it could have had before this
            # field existed), not a parse error.
            bridge=bool(raw.get("bridge", False)),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise AppDriveError(f"corrupt app-drive session file for {session_id!r}: {e}") from e


def _forget_session(session_id: str) -> None:
    try:
        _session_file(session_id).unlink()
    except OSError:
        pass


def _pid_alive(pid: int, *, bridge: bool = False) -> bool:
    """Whether *pid* is still a live process right now — an OBSERVATION
    (#2096), never inferred from "we haven't been told otherwise".

    *bridge* (#3611) routes the probe through
    :func:`coord.win_native_bridge.windows_pid_alive` instead of every
    POSIX/Windows branch below — *pid* is a genuine Windows PID there (a
    bridge-spawned ``win-native`` daemon runs on the real Windows-side
    interpreter, see :class:`SessionHandle`), and none of
    ``os.waitpid``/``os.kill``/``ctypes``'s ``OpenProcess`` mean anything
    against a pid from a different OS's pid namespace — `os.kill` against
    an arbitrary small integer on THIS (WSL/Linux) host would either
    always report "dead" (no such Linux pid) or, worse, collide with an
    unrelated, actually-alive Linux process that happens to reuse the
    same number.

    First tries a non-blocking reap (``waitpid(pid, WNOHANG)``): when
    *pid* is a direct child of THIS process (true for a caller that opened
    the session and is now closing it within the same long-lived process —
    a test, or any in-process caller; NOT true for the common real case of
    separate ``coord app-drive open``/``close`` CLI invocations, which are
    unrelated processes to begin with) and has already exited, the kernel
    keeps it as a zombie — still a valid ``/proc`` entry, and
    ``kill(pid, 0)`` still succeeds against it — until something calls
    ``wait()`` on it. Reaping it here first (and treating a successfully
    reaped pid as dead) avoids exactly that false "still alive" reading
    rather than leaking the question to whatever POSIX returns for a
    zombie. ``ChildProcessError`` (not our child at all — the common case)
    falls through to the real liveness probe.

    POSIX's null-signal probe (``kill(pid, 0)``) is a read-only existence
    check. Windows has no such thing: ``os.kill(pid, 0)`` there maps
    straight to ``TerminateProcess(handle, 0)`` — a *literal* kill with
    exit code 0, not a probe — so this branches to a real, non-destructive
    ``OpenProcess`` existence check via ``ctypes`` instead of reusing the
    POSIX call under a different OS.
    """
    if bridge:
        # Deferred: this module has no other reason to import
        # `coord.win_native_bridge` (every non-bridge session never
        # touches it) — module-level would add a needless import for the
        # common (non-WSL) case.
        from coord.win_native_bridge import windows_pid_alive  # noqa: PLC0415

        return windows_pid_alive(pid)
    if sys.platform != "win32":
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
            if reaped_pid == pid:
                return False
        except ChildProcessError:
            pass  # not our child — fall through to the real probe below
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415 — Windows-only path

        # PROCESS_QUERY_LIMITED_INFORMATION (0x1000) is enough to confirm
        # the handle opens at all; it grants no control over the process,
        # so this can never accidentally affect it.
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else (or a racing setuid) — still alive.
        return True
    except OSError:
        return False
    return True


def send_command(handle: SessionHandle, command: dict, *, timeout: float = 30.0) -> dict:
    """Send one JSON *command* to *handle*'s daemon over a fresh TCP
    connection and return its one JSON-line reply.

    Raises :class:`AppDriveError` on a connection failure (the daemon is
    gone/unreachable — a stale session, never silently treated as "the verb
    succeeded") or a reply the daemon itself flagged as an error. Stamps
    *handle*'s own ``token`` onto *command* (#3590 review: the only
    authentication the control socket has — see :class:`SessionHandle`)."""
    command = {**command, "token": handle.token}
    try:
        with socket.create_connection(("127.0.0.1", handle.port), timeout=timeout) as sock:
            sock.sendall((json.dumps(command) + "\n").encode("utf-8"))
            sock.settimeout(timeout)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks).decode("utf-8", errors="replace")
    except OSError as e:
        raise AppDriveError(
            f"could not reach app-drive session {handle.session_id!r} on port "
            f"{handle.port}: {e}"
        ) from e
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    try:
        reply = json.loads(line)
    except ValueError as e:
        raise AppDriveError(f"app-drive daemon sent an unparseable reply: {e}") from e
    if not isinstance(reply, dict):
        raise AppDriveError(f"app-drive daemon reply was not a JSON object: {reply!r}")
    if reply.get("error"):
        raise AppDriveError(str(reply["error"]))
    return reply


def open_session(
    kind: str, *, launch: str, cwd: str, cols: int = 80, rows: int = 24,
    width: int | None = None, height: int | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT, ready_timeout: float = 30.0,
    python: str | None = None,
) -> SessionHandle:
    """Spawn :mod:`coord.app_drive_daemon` for *kind* and wait for it to
    confirm it's actually listening before returning (#2096: "opened" is an
    observation of the daemon's own ready-file, never the mere fact that
    ``Popen`` didn't raise).

    The daemon is spawned genuinely DETACHED — ``start_new_session=True``
    on POSIX (``setsid()``: a new session AND a new process group),
    ``CREATE_NEW_PROCESS_GROUP`` on Windows — so it is never a member of
    whatever process group this ``coord app-drive open`` invocation itself
    ran in. This is load-bearing, not cosmetic (#3590 review): an agent
    Bash tool that reaps its own command's process group on
    completion/timeout (a common sandbox configuration, and the reason
    #3583 exists at all) would otherwise take the daemon down with the
    very CLI call that started it, failing the very next ``send``. The
    idle self-expiry (:mod:`coord.app_drive_daemon`) remains the backstop
    for the case nobody ever calls ``close`` at all.

    **The WSL case (#3611).** ``win-native``'s own backend
    (:class:`coord.win_native_driver.Win32Calls`) raises at construction on
    any host where ``os.name != "nt"`` — including a WSL-hosted ``windows``-
    capability agent (dell64, :mod:`coord.win_native_bridge`'s own module
    docstring), since ``ctypes.windll``/``comtypes`` have no meaning there
    no matter what gets pip-installed into that (Linux) venv. On
    :func:`coord.win_native_bridge.is_wsl_host`, this spawns the SAME
    :mod:`coord.app_drive_daemon` module but on the REAL Windows-side
    interpreter (:func:`coord.win_native_bridge.ensure_windows_win_native_venv`)
    reached through WSL interop — exactly the mechanism
    :func:`coord.acceptance_drivers._run_win_native` already uses for the
    whole-spec ``run-spec``/smoke path, just spawning a long-lived daemon
    instead of a one-shot spec run. *cwd* and *ready_file* are translated to
    their Windows-visible form (:func:`coord.win_native_bridge
    .translate_to_windows_path` — the Windows-side process can resolve a
    ``\\\\wsl.localhost\\...`` UNC path back into this same WSL session's
    own filesystem, so this function's own *ready_file* polling loop below
    needs no change at all) before being handed to the bridge python;
    *launch* is passed through unchanged, same as the smoke bridge — it
    names the real Windows exe to launch, which is the caller's own
    responsibility to express in a form the Windows side can resolve. The
    resulting :class:`SessionHandle` is marked ``bridge=True`` so
    :func:`close_session` knows its ``pid``/``app_pid`` are genuine
    Windows PIDs, not Linux ones.
    """
    if kind not in APP_DRIVE_KINDS:
        raise AppDriveError(f"unknown app-drive kind {kind!r} — expected one of {APP_DRIVE_KINDS}")

    session_id = uuid.uuid4().hex[:12]
    token = secrets.token_hex(16)
    ready_file = _sessions_dir() / f"{session_id}.ready"
    if ready_file.exists():
        ready_file.unlink()

    bridge_mode = False
    spawn_argv0 = python or sys.executable
    spawn_cwd = cwd
    spawn_ready_file = str(ready_file)

    if kind == "win-native":
        from coord.win_native_bridge import is_wsl_host  # noqa: PLC0415 — see `_pid_alive`'s own deferred import

        if is_wsl_host():
            from coord.win_native_bridge import (  # noqa: PLC0415
                WinNativeBridgeError,
                ensure_windows_win_native_venv,
                translate_to_windows_path,
                windows_path_to_wsl_path,
            )

            bridge_mode = True
            try:
                windows_python = python or ensure_windows_win_native_venv()
                spawn_argv0 = windows_path_to_wsl_path(windows_python)
                spawn_cwd = translate_to_windows_path(cwd)
                spawn_ready_file = translate_to_windows_path(str(ready_file))
            except WinNativeBridgeError as e:
                raise AppDriveError(f"win-native WSL bridge could not be prepared: {e}") from e

    argv = [
        spawn_argv0, "-m", "coord.app_drive_daemon",
        "--kind", kind, "--launch", launch, "--cwd", spawn_cwd,
        "--cols", str(cols), "--rows", str(rows),
        "--idle-timeout", str(idle_timeout), "--ready-file", spawn_ready_file,
        "--token", token,
    ]
    if width is not None:
        argv += ["--width", str(width)]
    if height is not None:
        argv += ["--height", str(height)]
    detach_kwargs: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(  # noqa: S603 — *launch* is the lane worker's own, not untrusted input
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, **detach_kwargs,
    )

    try:
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if ready_file.exists():
                try:
                    ready = json.loads(ready_file.read_text())
                    ready_file.unlink()
                except (OSError, ValueError):
                    time.sleep(0.05)
                    continue
                if ready.get("error") == "unavailable":
                    # #3510/#3566: the backend's own session-available/
                    # permission precheck failed BEFORE launching anything
                    # — surface this as the dedicated unavailable signal,
                    # not a generic open failure, so a worker's own
                    # open-time check can fold `reason` straight into its
                    # unavailable fence.
                    raise AppDriveUnavailableError(ready.get("reason", "lane unavailable"))
                if ready.get("error"):
                    raise AppDriveError(
                        f"app-drive daemon for kind {kind!r} failed to open: "
                        f"{ready.get('reason', ready['error'])}"
                    )
                handle = SessionHandle(
                    session_id=session_id, kind=kind,
                    pid=int(ready["pid"]), port=int(ready["port"]), token=token,
                    app_pid=int(ready["app_pid"]) if ready.get("app_pid") is not None else None,
                    bridge=bridge_mode,
                )
                _write_session_file(handle)
                return handle
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                raise AppDriveError(
                    f"app-drive daemon for kind {kind!r} exited before becoming "
                    f"ready (code {proc.returncode}): {stderr.strip()}"
                )
            time.sleep(0.05)
        proc.kill()
        raise AppDriveError(
            f"app-drive daemon for kind {kind!r} did not become ready within "
            f"{ready_timeout:.0f}s"
        )
    finally:
        # #3590 review (nit): close the stderr pipe we opened above on
        # every exit path — the daemon keeps running past this function
        # returning on the success path, so there is nothing to `wait()`
        # for, but the now-unread pipe FD itself must still be closed or
        # a long-lived in-process caller accumulates `ResourceWarning`s.
        if proc.stderr is not None:
            proc.stderr.close()


def _target_pids(handle: SessionHandle) -> tuple[int, ...]:
    """Every pid a confirmed teardown of *handle* must observe dead — the
    daemon's own pid, PLUS the real app/pty-child pid it reported at open
    time (``app_pid``), when that's known and distinct (#3590 review: a
    ``closed: true`` that only proves the daemon died is not teardown
    confirmation for the app it was driving)."""
    if handle.app_pid is not None and handle.app_pid != handle.pid:
        return (handle.pid, handle.app_pid)
    return (handle.pid,)


def _all_dead(handle: SessionHandle, pids: tuple[int, ...]) -> bool:
    return all(not _pid_alive(pid, bridge=handle.bridge) for pid in pids)


def close_session(handle: SessionHandle, *, timeout: float = 15.0) -> bool:
    """Tear *handle* down and confirm it (#2096) — only reports success once
    BOTH the daemon process AND the real app/pty-child it was driving
    (``handle.app_pid``, when known) are OBSERVED gone, escalating from
    "ask nicely" to SIGTERM to SIGKILL against each rather than trusting
    any one step — or the daemon's own cooperation — blindly. Removes the
    on-disk session file only once confirmed dead (or already gone).

    This closes the gap #3590's review flagged: the escalation path used
    to SIGKILL only the daemon. If that SIGKILL fires before the daemon's
    own ``finally: backend.close()`` ever runs, the real app survived
    while this function still reported ``closed: true`` — now this
    function independently re-observes, and if necessary directly
    signals, ``app_pid`` too, so that gap cannot reopen regardless of
    whether the daemon cooperates.

    Returns ``False`` — never raises — if any target pid is still alive
    after every escalation: a gate that reports teardown success must be
    able to actually fail that report (epic #2096), not default to "assume
    it worked"."""
    targets = _target_pids(handle)
    try:
        send_command(handle, {"op": "close"}, timeout=min(timeout, 10.0))
    except AppDriveError:
        pass  # already gone, or refused — the pid-based confirmation below is authoritative

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _all_dead(handle, targets):
            _forget_session(handle.session_id)
            return True
        time.sleep(0.1)

    if handle.bridge:
        # #3611: `targets` are genuine Windows PIDs here (a bridge-spawned
        # `win-native` daemon runs on the real Windows-side interpreter —
        # see `SessionHandle.bridge`) — `os.kill` cannot address them at
        # all from this (WSL/Linux) side. `taskkill.exe`'s own `/F` is the
        # exact same weaker-then-stronger escalation `SIGTERM`/`SIGKILL`
        # gives every other kind, just reached through WSL interop instead
        # of a POSIX signal.
        from coord.win_native_bridge import kill_windows_pid  # noqa: PLC0415 — see `_pid_alive`'s own deferred import

        for force in (False, True):
            for pid in targets:
                if _pid_alive(pid, bridge=True):
                    kill_windows_pid(pid, force=force)
            escalate_deadline = time.monotonic() + 3.0
            while time.monotonic() < escalate_deadline:
                if _all_dead(handle, targets):
                    _forget_session(handle.session_id)
                    return True
                time.sleep(0.1)
        return False

    # `SIGKILL` doesn't exist on Windows (`signal` there defines no POSIX
    # kill signals beyond `SIGTERM`/`SIGBREAK`) — `os.kill(pid, SIGTERM)`
    # there already maps to a hard `TerminateProcess`, so there is no
    # weaker-then-stronger escalation to make; just retry the one signal
    # Windows actually has.
    escalation = (signal.SIGTERM, signal.SIGKILL) if hasattr(signal, "SIGKILL") else (signal.SIGTERM, signal.SIGTERM)
    for sig in escalation:
        for pid in targets:
            if not _pid_alive(pid):
                continue
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        escalate_deadline = time.monotonic() + 3.0
        while time.monotonic() < escalate_deadline:
            if _all_dead(handle, targets):
                _forget_session(handle.session_id)
                return True
            time.sleep(0.1)

    return False
