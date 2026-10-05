"""``python -m coord.app_drive_daemon`` (#3590): the long-lived process one
``coord app-drive <kind> open`` spawns and that every later ``send``/
``screen``/``probe``/``wait-idle``/``close`` call (each its own short-lived
``coord app-drive`` invocation) talks to over a fresh TCP connection.

Not meant to be run by hand — :func:`coord.app_drive.open_session` is the
supported way to start one. See :mod:`coord.app_drive`'s module docstring
for the full design rationale (why a daemon at all, the two-layer teardown
guarantee).

Wire protocol: one JSON object per connection, one line
(``json.dumps(...) + "\n"``), one JSON object back, then the connection
closes. Never multiplexes multiple commands over one connection — this
keeps a half-open/duplicated client trivially recoverable (it just reconnects
for the next verb) rather than needing its own framing/pipelining logic.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from coord.app_drive import AppDriveUnavailableError


def _build_backend(
    kind: str, launch: str, cwd: str, cols: int, rows: int,
    *, width: int | None = None, height: int | None = None,
) -> Any:
    """Construct the kind-specific session backend — the ONE place that
    maps an app-drive ``kind`` string to the real driver class
    (:class:`coord.tui_pty_driver.TuiPtySession` /
    :class:`coord.mac_native_driver.MacNativeSession` /
    :class:`coord.win_native_driver.WinNativeSession` /
    :class:`coord.gtk_native_driver.GtkNativeSession`), so a new kind is
    one branch here, not a change scattered across the CLI/registry.

    Raises :class:`AppDriveUnavailableError` when the backend's own
    session-availability precheck (#3510/#3566 — a locked/absent GUI
    session, a missing Accessibility grant) fails, BEFORE launching
    anything — the native-lane analogue of :class:`coord.bugbash
    .ExploreOutcome.unavailable`: an environment condition, never an
    ordinary open failure.

    *width*/*height* (native kinds only) are an explicit pixel size for
    the launched window; when omitted (``None``) this falls back to the
    previous ``cols * 10 or 1024`` / ``rows * 20 or 768`` derivation
    (#3590 review: at this CLI's own ``--cols 80 --rows 24`` defaults,
    that derivation is always 800x480 — the ``or 1024``/``or 768``
    fallbacks are unreachable dead code unless ``--cols``/``--rows`` is
    explicitly 0 — so a caller that actually wants 1024x768, or any other
    real size, should pass *width*/*height* directly rather than fight
    the terminal-geometry multiplier).
    """
    if kind == "tui-pty":
        from coord.tui_pty_driver import TuiPtySession  # noqa: PLC0415

        return TuiPtySession(launch, cwd, cols=cols, rows=rows)

    native_width = width if width is not None else (cols * 10 or 1024)
    native_height = height if height is not None else (rows * 20 or 768)

    if kind == "mac-native":
        from coord.mac_native_driver import MacNativeSession, MacOSCalls  # noqa: PLC0415

        calls = MacOSCalls()
        available, reason = calls.session_available()
        if not available:
            raise AppDriveUnavailableError(reason or "no unlocked GUI session is available")
        trusted, reason = calls.ax_trust_available()
        if not trusted:
            raise AppDriveUnavailableError(reason or "AXIsProcessTrusted() is False")
        return MacNativeSession(launch, cwd, width=native_width, height=native_height, calls=calls)

    if kind == "win-native":
        from coord.win_native_driver import Win32Calls, WinNativeSession  # noqa: PLC0415

        calls = Win32Calls()
        available, reason = calls.session_available()
        if not available:
            raise AppDriveUnavailableError(reason or "no interactive Windows session is available")
        return WinNativeSession(launch, cwd, width=native_width, height=native_height, calls=calls)

    if kind == "gtk-native":
        from coord.gtk_native_driver import GtkNativeSession, LinuxGtkCalls  # noqa: PLC0415

        calls = LinuxGtkCalls()
        available, reason = calls.session_available()
        if not available:
            raise AppDriveUnavailableError(reason or "no usable display is available")
        return GtkNativeSession(launch, cwd, width=native_width, height=native_height, calls=calls)

    raise ValueError(f"unknown app-drive kind {kind!r}")


def _dispatch(backend: Any, kind: str, command: dict) -> dict:
    """One command -> one reply, never raising — every backend failure
    (a bad verb, a backend method raising) is folded into
    ``{"error": "..."}`` so the daemon's accept loop never dies from a
    single bad request."""
    op = command.get("op")
    args = command.get("args") or {}
    try:
        if op == "close":
            # Acked here; the actual teardown + process exit happens in
            # `serve`'s own loop right after this reply is sent (it checks
            # `command.get("op") == "close"` independently of this
            # dispatch), so a client never has to guess whether its ack
            # means "about to shut down" or "already gone".
            return {"ok": True}
        if op == "send_key":
            backend.send_key(args["key"])
            return {"ok": True}
        if op == "send_text":
            if kind != "tui-pty":
                return {"error": f"send_text is tui-pty-only (kind={kind!r})"}
            backend.send_text(args["text"])
            return {"ok": True}
        if op == "send_click":
            if kind == "tui-pty":
                backend.send_click(int(args["row"]), int(args["col"]), args.get("button", "left"))
            else:
                backend.send_click(int(args["x"]), int(args["y"]), args.get("button", "left"))
            return {"ok": True}
        if op == "wait_idle":
            if kind != "tui-pty":
                return {"error": f"wait_idle is tui-pty-only (kind={kind!r})"}
            settled = backend.wait_idle(
                ms=int(args.get("ms", 500)), timeout_ms=int(args.get("timeout_ms", 5000)),
            )
            return {"ok": True, "settled": settled}
        if op == "screen":
            if kind != "tui-pty":
                return {"error": f"screen is tui-pty-only (kind={kind!r}) — use capture"}
            return {"ok": True, "text": backend.screen_text(args.get("region"))}
        if op == "capture":
            if kind == "tui-pty":
                return {"ok": True, "text": backend.screen_text(args.get("region"))}
            import base64  # noqa: PLC0415
            return {"ok": True, "image_b64": base64.b64encode(backend.capture()).decode("ascii")}
        if op == "probe":
            if kind == "tui-pty":
                return {"error": "probe is not supported for tui-pty — use screen"}
            return {"ok": True, "result": backend.probe(args["name"], **args.get("kwargs", {}))}
        if op == "is_alive":
            return {"ok": True, "alive": backend.is_alive()}
        return {"error": f"unknown op {op!r}"}
    except Exception as e:  # noqa: BLE001 — a bad request/backend error must become a reply, never kill the daemon
        return {"error": f"{type(e).__name__}: {e}"}


def _write_ready_file(ready_file: Path, payload: dict) -> None:
    """Write *payload* to *ready_file* atomically (#3590 review nit): the
    success path already did this via a ``.tmp`` + ``replace()``; the
    error paths used a plain ``write_text`` instead, an avoidable
    asymmetry — :func:`coord.app_drive.open_session`'s retry loop papers
    over a torn read either way, but there's no reason to rely on that."""
    tmp = ready_file.with_suffix(".ready.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(ready_file)


def serve(
    kind: str, launch: str, cwd: str, cols: int, rows: int,
    *, idle_timeout: float, ready_file: Path, token: str,
    width: int | None = None, height: int | None = None,
) -> int:
    """Build *kind*'s backend, bind an ephemeral localhost port, announce
    readiness via *ready_file*, then serve one JSON command per connection
    until ``close`` arrives or *idle_timeout* elapses with none. Always
    tears the backend down before returning — the SAME guarantee whichever
    path got it there (explicit close, idle self-expiry, or an exception
    while opening/serving).

    Every command must carry ``"token": token`` to be dispatched (#3590
    review: the control socket otherwise has no authentication at all,
    and the port is already discoverable from the world-readable session
    file) — an unauthenticated connection gets an ``{"error": ...}`` reply
    and is never passed to :func:`_dispatch`, so it can never fire
    ``send_text``/``close`` against the app being driven."""
    try:
        backend = _build_backend(kind, launch, cwd, cols, rows, width=width, height=height)
    except AppDriveUnavailableError as e:
        _write_ready_file(ready_file, {"error": "unavailable", "reason": str(e)})
        return 1
    except Exception as e:  # noqa: BLE001 — surface the failure via the ready file, not a bare traceback
        _write_ready_file(ready_file, {"error": "open_failed", "reason": f"{type(e).__name__}: {e}"})
        return 1

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]

    # #3590 review: the real app/pty-child pid this backend launched, so
    # a client-side `close_session` can re-observe (and if necessary
    # directly signal) it too, not just this daemon's own pid — see
    # `coord.app_drive.SessionHandle.app_pid`.
    app_pid = getattr(backend, "pid", None)
    _write_ready_file(ready_file, {"pid": os.getpid(), "port": port, "app_pid": app_pid})

    stop = threading.Event()
    last_activity = [time.monotonic()]

    try:
        sock.settimeout(1.0)
        while not stop.is_set():
            if time.monotonic() - last_activity[0] > idle_timeout:
                break
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(30.0)
                try:
                    chunks = []
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        if b"\n" in chunk:
                            break
                    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
                    command = json.loads(raw) if raw else {}
                except (OSError, ValueError) as e:
                    try:
                        conn.sendall((json.dumps({"error": f"bad request: {e}"}) + "\n").encode())
                    except OSError:
                        pass
                    continue
                last_activity[0] = time.monotonic()
                if command.get("token") != token:
                    try:
                        conn.sendall((json.dumps({"error": "unauthorized"}) + "\n").encode())
                    except OSError:
                        pass
                    continue
                reply = _dispatch(backend, kind, command)
                try:
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                except OSError:
                    pass
                if command.get("op") == "close":
                    stop.set()
    finally:
        try:
            backend.close()
        except Exception:  # noqa: BLE001 — teardown must not raise past this point
            pass
        sock.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m coord.app_drive_daemon")
    parser.add_argument("--kind", required=True, choices=("tui-pty", "win-native", "mac-native", "gtk-native"))
    parser.add_argument("--launch", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--cols", type=int, default=80)
    parser.add_argument("--rows", type=int, default=24)
    parser.add_argument("--idle-timeout", type=float, default=1800.0)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--width", type=int, default=None, help="Native-kind window width in pixels.")
    parser.add_argument("--height", type=int, default=None, help="Native-kind window height in pixels.")
    ns = parser.parse_args(argv)
    return serve(
        ns.kind, ns.launch, ns.cwd, ns.cols, ns.rows,
        idle_timeout=ns.idle_timeout, ready_file=Path(ns.ready_file), token=ns.token,
        width=ns.width, height=ns.height,
    )


if __name__ == "__main__":
    sys.exit(main())
