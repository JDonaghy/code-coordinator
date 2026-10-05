"""``coord app-drive`` (#3590): CLI wiring for the sanctioned entry point a
bugbash lane worker drives the real app through (a worker's Bash tool,
exploring a repo's catalogue journeys, never a home-made input-injection
helper — see :mod:`coord.bugbash`'s HARD RULE and :mod:`coord.app_drive`'s
own module docstring for the full design rationale).

Subcommands:

- ``open --kind K --launch CMD --cwd DIR`` — spawn a session; prints
  ``{"session_id": ...}``. Kept alive by a background daemon
  (:mod:`coord.app_drive_daemon`) until ``close`` or its own idle timeout.
- ``send --session ID --key K|--text T|--click R,C[,BUTTON]`` — one input
  event against the live session.
- ``wait-idle --session ID [--ms N] [--timeout-ms N]`` — tui-pty only.
- ``screen --session ID`` / ``capture --session ID`` — current rendered
  text (tui-pty) or a PNG/BMP/XWD-style image capture (native kinds),
  base64-encoded.
- ``probe --session ID --name NAME [--kwargs JSON]`` — a native kind's own
  accessibility-tree/window probes (``ax_elements``/``uia_elements``/
  ``is_window_alive``/...). Not available for tui-pty — use ``screen``.
- ``close --session ID`` — guaranteed, CONFIRMED teardown (#2096 — see
  :func:`coord.app_drive.close_session`).
- ``run-spec KIND SPEC_FILE --launch CMD --cwd DIR`` — run a whole Tier-2
  smoke/native spec end to end in ONE command, no open/close bookkeeping
  needed — the same :func:`coord.acceptance_drivers.run_driver` the oracle
  loop's own ``coord acceptance run`` calls, so a spec run through either
  command is answered by the exact same code (#2096 "one question, one
  answer").
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from coord.app_drive import (
    APP_DRIVE_KINDS,
    AppDriveError,
    AppDriveUnavailableError,
    DEFAULT_IDLE_TIMEOUT,
    close_session,
    load_session,
    open_session,
    send_command,
)


@click.group("app-drive")
def app_drive_group() -> None:
    """Drive a real app under a bugbash lane's own sanctioned driver —
    ``tui-pty``/``win-native``/``mac-native``/``gtk-native`` (#3590)."""


_KIND_ARG = click.argument("kind", type=click.Choice(APP_DRIVE_KINDS))


@app_drive_group.command("open")
@_KIND_ARG
@click.option("--launch", required=True, help="Shell command that launches the app.")
@click.option("--cwd", required=True, help="Working directory to launch it in.")
@click.option("--cols", type=int, default=80, show_default=True, help="tui-pty terminal columns.")
@click.option("--rows", type=int, default=24, show_default=True, help="tui-pty terminal rows.")
@click.option(
    "--width", type=int, default=None,
    help="Native-kind window width in pixels (mac-native/win-native/gtk-native only). "
    "Defaults to a size DERIVED from --cols (#3590 review: the derived default is "
    "800x480 at this command's own --cols/--rows defaults, not the 1024x768 a bare "
    "reading of the driver's fallback might suggest) — pass this explicitly for a "
    "real pixel size instead.",
)
@click.option(
    "--height", type=int, default=None,
    help="Native-kind window height in pixels (mac-native/win-native/gtk-native only). "
    "See --width.",
)
@click.option(
    "--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT, show_default=True,
    help="Seconds of no command before the session self-tears-down even without an explicit close.",
)
def app_drive_open(
    kind: str, launch: str, cwd: str, cols: int, rows: int,
    width: int | None, height: int | None, idle_timeout: float,
) -> None:
    """Open a new KIND session. Prints ``{"session_id": ...}`` on success."""
    try:
        handle = open_session(
            kind, launch=launch, cwd=cwd, cols=cols, rows=rows,
            width=width, height=height, idle_timeout=idle_timeout,
        )
    except AppDriveUnavailableError as e:
        click.echo(json.dumps({"status": "unavailable", "reason": e.reason}))
        sys.exit(3)
    except AppDriveError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    click.echo(json.dumps({"session_id": handle.session_id, "kind": handle.kind}))


def _resolve(session: str):
    try:
        return load_session(session)
    except AppDriveError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)


@app_drive_group.command("send")
@click.option("--session", "session_id", required=True)
@click.option("--key", default=None, help="A named key or single character (tui-pty/native).")
@click.option("--text", default=None, help="Literal text to type (tui-pty only).")
@click.option(
    "--click", "click_spec", default=None,
    help="'ROW,COL[,BUTTON]' (tui-pty) or 'X,Y[,BUTTON]' (native kinds).",
)
def app_drive_send(session_id: str, key: str | None, text: str | None, click_spec: str | None) -> None:
    """Send exactly one input event to an open session (--key, --text, or --click)."""
    chosen = [v for v in (key, text, click_spec) if v is not None]
    if len(chosen) != 1:
        click.echo("error: pass exactly one of --key / --text / --click", err=True)
        sys.exit(2)
    handle = _resolve(session_id)
    try:
        if key is not None:
            reply = send_command(handle, {"op": "send_key", "args": {"key": key}})
        elif text is not None:
            reply = send_command(handle, {"op": "send_text", "args": {"text": text}})
        else:
            parts = click_spec.split(",")
            if len(parts) not in (2, 3):
                click.echo("error: --click expects 'A,B' or 'A,B,BUTTON'", err=True)
                sys.exit(2)
            a, b = int(parts[0]), int(parts[1])
            button = parts[2] if len(parts) == 3 else "left"
            if handle.kind == "tui-pty":
                reply = send_command(handle, {"op": "send_click", "args": {"row": a, "col": b, "button": button}})
            else:
                reply = send_command(handle, {"op": "send_click", "args": {"x": a, "y": b, "button": button}})
    except AppDriveError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    click.echo(json.dumps(reply))


@app_drive_group.command("wait-idle")
@click.option("--session", "session_id", required=True)
@click.option("--ms", type=int, default=500, show_default=True, help="Quiet window required.")
@click.option("--timeout-ms", type=int, default=5000, show_default=True)
def app_drive_wait_idle(session_id: str, ms: int, timeout_ms: int) -> None:
    """Block until the tui-pty session's output stream has settled."""
    handle = _resolve(session_id)
    try:
        reply = send_command(handle, {"op": "wait_idle", "args": {"ms": ms, "timeout_ms": timeout_ms}})
    except AppDriveError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    click.echo(json.dumps(reply))
    if not reply.get("settled", True):
        sys.exit(1)


@app_drive_group.command("screen")
@click.option("--session", "session_id", required=True)
def app_drive_screen(session_id: str) -> None:
    """Current rendered screen text (tui-pty only)."""
    handle = _resolve(session_id)
    try:
        reply = send_command(handle, {"op": "screen", "args": {}})
    except AppDriveError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    click.echo(json.dumps(reply))


@app_drive_group.command("capture")
@click.option("--session", "session_id", required=True)
def app_drive_capture(session_id: str) -> None:
    """A base64-encoded image capture (native kinds) or screen text
    (tui-pty, same as ``screen``)."""
    handle = _resolve(session_id)
    try:
        reply = send_command(handle, {"op": "capture", "args": {}})
    except AppDriveError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    click.echo(json.dumps(reply))


@app_drive_group.command("probe")
@click.option("--session", "session_id", required=True)
@click.option("--name", required=True, help="e.g. ax_elements / uia_elements / is_window_alive / find_a11y.")
@click.option("--kwargs", "kwargs_json", default=None, help="JSON object of extra probe arguments.")
def app_drive_probe(session_id: str, name: str, kwargs_json: str | None) -> None:
    """Run one of a native session's own accessibility/window probes."""
    handle = _resolve(session_id)
    kwargs = {}
    if kwargs_json:
        try:
            kwargs = json.loads(kwargs_json)
        except ValueError as e:
            click.echo(f"error: --kwargs is not valid JSON: {e}", err=True)
            sys.exit(2)
    try:
        reply = send_command(handle, {"op": "probe", "args": {"name": name, "kwargs": kwargs}})
    except AppDriveError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    click.echo(json.dumps(reply))


@app_drive_group.command("close")
@click.option("--session", "session_id", required=True)
def app_drive_close(session_id: str) -> None:
    """Tear the session down — CONFIRMED (#2096), not just requested."""
    handle = _resolve(session_id)
    ok = close_session(handle)
    click.echo(json.dumps({"closed": ok}))
    if not ok:
        sys.exit(1)


@app_drive_group.command("run-spec")
@_KIND_ARG
@click.argument("spec_file", type=click.Path(exists=True))
@click.option("--launch", required=True, help="Shell command that launches the app.")
@click.option("--cwd", required=True, help="Working directory to launch it in.")
@click.option("--timeout", type=int, default=900, show_default=True, help="Whole-spec wall-clock budget, seconds.")
def app_drive_run_spec(kind: str, spec_file: str, launch: str, cwd: str, timeout: int) -> None:
    """Run SPEC_FILE end to end in one command — the same
    :func:`coord.acceptance_drivers.run_driver` ``coord acceptance run``
    itself calls for this kind, just addressed directly by path instead of
    through an issue's acceptance-driver config."""
    from coord.acceptance_drivers import DriverError, run_driver  # noqa: PLC0415

    try:
        result = run_driver(
            kind, launch, cwd=cwd, timeout=timeout,
            entrypoint=str(Path(spec_file).resolve()),
        )
    except DriverError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(json.dumps(result.tests, indent=2))
    # #2096: judged from the tests' own reported verdicts, not merely the
    # run command's exit code (`result.ok`) — a driver can exit 0 while
    # individually reporting a failing/unavailable step (see
    # `DriverResult.ok`'s own docstring). An EMPTY `result.tests` must not
    # pass either (#3590 review) — a gate reporting success against zero
    # observations is exactly the "unconfirmed success" epic #2096 exists
    # to catch, not a real pass.
    if not result.tests:
        click.echo("error: run-spec reported zero tests — treating as a failure, not a pass", err=True)
        sys.exit(1)
    if not result.ok or any(t.get("status") in ("fail", "unavailable") for t in result.tests):
        sys.exit(1)
