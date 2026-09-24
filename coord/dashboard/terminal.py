"""PTY <-> WebSocket terminal bridge (#1065).

Human-attended attach to a live ``coord-<assignment_id>`` tmux session from
the phone dashboard. This module is deliberately split from
``coord/dashboard/server.py`` so the ssh/tmux spawn machinery — the part that
needs a real terminal and can't run in CI — sits behind a small,
independently-testable seam (:class:`SessionAttacher`).

ToS §3.7 / #437: this bridge only ever relays a *live human* — keystrokes in,
PTY output out, plus resize. It performs no autonomous injection, scraping,
or decision-making of its own.
"""

from __future__ import annotations

import asyncio
import os
import struct
import subprocess
from pathlib import Path
from typing import Protocol

from coord.config import Config
from coord.interactive import TmuxHost, tmux_session_name
from coord.models import Board

# Server-side bearer token sources for `coord web`, in precedence order.
# Mirrors coord.serve_app.resolve_serve_token()/SERVE_TOKEN_ENV/SERVE_TOKEN_FILE
# exactly, but kept distinct (COORD_WEB_TOKEN / web_token) so a box running
# both `coord serve` and `coord web` doesn't share -- or accidentally leak --
# one secret to the other's surface.
WEB_TOKEN_ENV = "COORD_WEB_TOKEN"
WEB_TOKEN_FILE = Path.home() / ".coord" / "web_token"


def resolve_web_token(flag_token: str | None = None) -> str | None:
    """Resolve `coord web`'s bearer token: flag > ``COORD_WEB_TOKEN`` > file.

    Returns ``None`` when none is configured -- the dashboard (and the
    ``/ws/terminal`` bridge specifically) then runs open, same convention as
    :func:`coord.serve_app.resolve_serve_token`. A blank/whitespace token is
    treated as unset.
    """
    for src in (flag_token, os.environ.get(WEB_TOKEN_ENV)):
        if src and src.strip():
            return src.strip()
    if WEB_TOKEN_FILE.exists():
        try:
            from_file = WEB_TOKEN_FILE.read_text().strip()
        except OSError:
            from_file = ""
        if from_file:
            return from_file
    return None


def resolve_session_target(
    session_id: str, board: Board, config: Config
) -> tuple[str | None, str] | None:
    """Resolve *session_id* (== assignment_id) to ``(ssh_host, tmux_name)``.

    ``ssh_host`` is ``None`` when the session lives on this machine (attach
    locally); otherwise it is the target machine's ``host`` for an ssh
    attach, matching the fleet's named-tmux model (#486/#487).

    This is an interim substitute for the not-yet-built ``GET /api/sessions``
    endpoint (a sibling issue in the #1064 epic) -- it reads the board
    directly instead of going through that API. Whoever builds
    ``/api/sessions`` should consider routing this resolver through it so the
    two paths can't drift.

    Returns ``None`` when *session_id* doesn't match any active assignment.
    """
    assignment = next(
        (a for a in board.active if a.assignment_id == session_id), None
    )
    if assignment is None:
        return None

    session_name = tmux_session_name(session_id)
    machine_name = assignment.machine_name
    if not machine_name:
        return None, session_name

    machine = next((m for m in config.machines if m.name == machine_name), None)
    if machine is None:
        return None, session_name

    from coord.config import resolve_local_machine  # noqa: PLC0415

    resolved_local = resolve_local_machine(config)
    is_local = resolved_local is not None and resolved_local.name == machine.name
    return (None if is_local else machine.host), session_name


class AttachedPty(Protocol):
    """One live PTY-backed attach -- the seam the WS handler drives.

    Fakeable in tests so the acceptance test needs no real ssh/tmux (#1065).
    """

    async def read(self) -> bytes:
        """Return the next chunk of PTY output, or ``b""`` on EOF/closed."""
        ...

    def write(self, data: bytes) -> None:
        """Write browser keystrokes to the PTY (stdin of the attached process)."""
        ...

    def resize(self, cols: int, rows: int) -> None:
        """Propagate a terminal resize to the PTY (``TIOCSWINSZ``)."""
        ...

    def detach(self) -> None:
        """End *this* client's attach. MUST NOT kill the tmux session."""
        ...

    async def copy_mode(self, action: str) -> None:
        """Drive tmux copy-mode server-side via ``tmux send-keys -X`` (#1299).

        This is prefix- and mode-key-agnostic: ``send-keys -X`` targets the
        tmux *server* directly, so the client does not need to know the user's
        ``prefix`` binding or whether ``mode-keys`` is set to ``vi`` or
        ``emacs``.

        ToS §3.7 / #437: every action here is a *human tapping a button* in
        their own attached session. No autonomous injection, scraping, or
        decision-making -- same guarantee as the ``write`` relay path.

        Supported *action* values::

            "enter"     -- enter copy-mode (``copy-mode -t <session>``)
            "exit"      -- leave copy-mode (``send-keys -X cancel``)
            "page-up"   -- scroll a page up
            "page-down" -- scroll a page down
            "top"       -- jump to oldest history line
            "bottom"    -- jump to newest line (live pane)

        Unknown or unmapped *action* values are silently ignored -- never
        raise.
        """
        ...


class SessionAttacher(Protocol):
    """Seam over the ssh/tmux attach spawn (#1065 acceptance: injectable so
    tests need no real ssh)."""

    async def attach(self, host: str | None, session_name: str) -> AttachedPty:
        ...


class TmuxSessionAttacher:
    """Real :class:`SessionAttacher`: spawns ``tmux attach-session`` (local)
    or ``ssh <host> -tt tmux attach-session`` (remote) behind a PTY.

    Ending the resulting :class:`AttachedPty` (:meth:`AttachedPty.detach`)
    only terminates this *local* attach/ssh client process -- the tmux
    session, and whatever is running inside its pane (typically ``claude``),
    are unaffected. That is exactly the "detach, don't kill" guarantee
    ``coord.interactive._launch_via_tmux`` already relies on: a tmux session
    outlives any one attached client.
    """

    async def attach(self, host: str | None, session_name: str) -> AttachedPty:
        import fcntl  # stdlib, Unix-only -- deferred for platform safety
        import pty  # stdlib, Unix-only -- deferred for platform safety
        import termios  # stdlib, Unix-only -- deferred for platform safety

        host_obj = TmuxHost(ssh_target=host)

        # ── Resize-to-client hardening ────────────────────────────────────
        # Without these two steps, attaching from a phone causes a visible
        # resize glitch: the PTY defaults to 80×24, ``tmux attach-session``
        # sees an 80×24 client and immediately shrinks the running window
        # (and the claude process inside it) *before* the browser's first
        # resize frame arrives.
        #
        # Fix 1: query the session's current window size and prime the PTY
        # at that size so ``tmux attach-session`` registers us as a client
        # that already matches the window — no downsize on connect.
        # The browser's first WS resize frame (sent from onopen) will arrive
        # within milliseconds and TIOCSWINSZ the PTY to the phone's real
        # viewport, which tmux then propagates via SIGWINCH.
        # Both calls below are blocking (each can be a real ssh round-trip
        # for a remote host, worst case the full 5s timeout) and this
        # `attach()` runs directly on the dashboard's single asyncio event
        # loop (awaited from the WS route handler in server.py) -- calling
        # `subprocess.run` here would stall every other connection the
        # dashboard is serving (other attached terminals, other websockets,
        # REST endpoints) for up to ~10s. Route them through the loop's
        # executor, matching the existing pattern (server.py's
        # `loop.run_in_executor(None, _fetch_agent_status, ...)`) instead of
        # blocking inline.
        loop = asyncio.get_running_loop()

        initial_cols, initial_rows = 80, 24  # safe fallback
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    host_obj.cmd(
                        [
                            "display-message",
                            "-t",
                            session_name,
                            "-p",
                            "#{window_width} #{window_height}",
                        ]
                    ),
                    capture_output=True,
                    text=True,
                    timeout=5,
                ),
            )
            parts = result.stdout.strip().split()
            if len(parts) == 2:
                w, h = int(parts[0]), int(parts[1])
                if w > 0 and h > 0:
                    initial_cols, initial_rows = w, h
        except Exception:
            pass

        # Fix 2: enable aggressive-resize for this window so that when
        # TIOCSWINSZ fires with the phone's viewport, the tmux server adopts
        # the new (sole) client's size rather than remaining pinned to a
        # previously-attached smaller client's footprint.
        try:
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    host_obj.cmd(
                        [
                            "set-window-option",
                            "-t",
                            session_name,
                            "aggressive-resize",
                            "on",
                        ]
                    ),
                    capture_output=True,
                    timeout=5,
                ),
            )
        except Exception:
            pass

        master_fd, slave_fd = pty.openpty()

        # Prime the PTY at the session's current window size.  tmux reads the
        # client's PTY dimensions when the attach handshake completes; a
        # correctly-sized PTY prevents the attach from triggering a downsize.
        try:
            fcntl.ioctl(
                master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", initial_rows, initial_cols, 0, 0),
            )
        except OSError:
            pass

        argv = host_obj.cmd(["attach-session", "-t", session_name], tty=True)
        # ``TERM`` may be absent when ``coord web`` runs as a systemd user
        # service (no controlling TTY, no ``Environment=TERM=...`` in the unit
        # file).  Without it, anything inside the attached pane that probes
        # terminal capabilities (e.g. ``claude`` on start) fails with "terminal
        # does not support clear" (#1229).  Mirror the same guard already used
        # in ``coord/agent.py`` for interactive-launch subprocesses.
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        try:
            proc = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                preexec_fn=os.setsid,
                close_fds=True,
                env=env,
            )
        finally:
            os.close(slave_fd)
        return _RealAttachedPty(proc, master_fd, host_obj=host_obj, session_name=session_name)


# Maps the wire ``action`` value from a copy-mode control frame to the tmux
# argument list (the ``-t <session_name>`` target is inserted at call time so
# the mapping itself stays tidy and session-agnostic).
_COPY_MODE_TMUX_ARGS: dict[str, list[str]] = {
    "enter":      ["copy-mode"],
    "exit":       ["send-keys", "-X", "cancel"],
    "page-up":    ["send-keys", "-X", "page-up"],
    "page-down":  ["send-keys", "-X", "page-down"],
    "top":        ["send-keys", "-X", "history-top"],
    "bottom":     ["send-keys", "-X", "history-bottom"],
}


class _RealAttachedPty:
    def __init__(
        self,
        proc: subprocess.Popen,
        master_fd: int,
        *,
        host_obj: "TmuxHost",
        session_name: str,
    ) -> None:
        self._proc = proc
        self._master_fd = master_fd
        self._host_obj = host_obj
        self._session_name = session_name

    async def read(self) -> bytes:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, os.read, self._master_fd, 65536)
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        try:
            os.write(self._master_fd, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        import fcntl  # stdlib, Unix-only -- deferred for platform safety
        import termios  # stdlib, Unix-only -- deferred for platform safety

        try:
            fcntl.ioctl(
                self._master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    def detach(self) -> None:
        # Kill only the local attach/ssh client -- see class docstring above.
        try:
            self._proc.terminate()
        except OSError:
            pass
        try:
            os.close(self._master_fd)
        except OSError:
            pass

    async def copy_mode(self, action: str) -> None:
        """See :meth:`AttachedPty.copy_mode`."""
        tmux_fragment = _COPY_MODE_TMUX_ARGS.get(action)
        if tmux_fragment is None:
            return  # unknown action -- no-op, never raise

        # Insert the target flag immediately after the tmux sub-command so the
        # command reads: ``copy-mode -t <session>`` or
        # ``send-keys -t <session> -X <action>``.
        sub_cmd = tmux_fragment[0]
        rest = tmux_fragment[1:]
        tmux_args = [sub_cmd, "-t", self._session_name, *rest]

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    self._host_obj.cmd(tmux_args),
                    capture_output=True,
                    timeout=5,
                ),
            )
        except Exception:
            pass  # best-effort; a stale copy-mode state is not fatal
