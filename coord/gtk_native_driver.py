"""``gtk-native`` acceptance driver — Tier 2's Linux/GTK-native tier (#3486).

``tui-pty`` (#3483), ``win-native`` (#3484) and ``mac-native`` (#3485) all
proved the same point on three different platforms: an in-process harness
can't see real window-manager/OS behaviour. The GTK GUI has the identical
blind spot — it is covered only by quadraui's in-process ``GtkDriver``, which
never asks the real Linux desktop "is there actually an accessible element
here?" or "does a real click at this pixel actually land on the real
window?". Those are X11/AT-SPI questions, answerable only by asking the real
desktop.

This driver launches the driven repo's real compiled GTK binary under a real
(if headless) display server, and probes it with real OS calls:

- ``xdotool`` — real mouse/keyboard input injected at the X11 level
  (``xdotool mousemove``/``click``/``key``), not an in-process event queue.
- the AT-SPI accessibility tree (via the GObject-introspection ``Atspi``
  bindings, ``gi.repository.Atspi``) — the same tree Orca (or a real user)
  sees, covering the ``expect_a11y``/``expect_a11y_within`` steps.
- ``xwd -id <windowid>`` — a window-specific capture (works even when the
  window is covered by another one, unlike a full-screen grab), attached as
  evidence to every *failing* step (see
  :meth:`NativeRunner._attach_capture_if_possible`).
- ``xdotool search --onlyvisible --pid`` — finding the launched process's
  real, *mapped* window (``--onlyvisible`` matters: GTK4-on-X11 also creates
  an invisible internal helper top-level with a lower XID than the real
  window — see :meth:`LinuxGtkCalls.find_top_window`, #3605) and polling
  whether it still exists (``expect_closed``).

**Headless by construction.** Unlike ``win-native``/``mac-native`` (which
drive a real, already-running desktop session), a Linux fleet host has no
desktop session to speak of — this driver assumes an X server is already up
on ``$DISPLAY`` (a headless compositor, or, more commonly, a persistent
``Xvfb :99`` session the fleet host runs so the display survives across
runs), rather than launching or tearing one down itself. Standing the
display up is an operator/host-provisioning concern (mirroring
:class:`coord.config.AcceptanceDriverConfig`'s own note that "a daemon host
must still satisfy every declared driver's capability itself") — this
module only ever *uses* ``$DISPLAY``, never starts an ``Xvfb`` of its own,
so two acceptance runs on the same host can never race over who owns the
display.

**Injectable OS-call seam.** Every actual ``xdotool``/AT-SPI/``xwd`` call is
one method on the :class:`GtkCalls` protocol, implemented for real by
:class:`LinuxGtkCalls` (Linux-only, needs ``xdotool``/``xwd`` on ``PATH`` and
the ``Atspi`` GObject-introspection binding — the ``gtk-native`` capability,
not a pip extra; see :func:`_import_atspi`). :class:`NativeRunner` — the
spec-step executor — never calls ``xdotool``/AT-SPI/``xwd`` directly; it
only calls through ``GtkCalls``. This is the same seam
:mod:`coord.win_native_driver`/:mod:`coord.mac_native_driver` use for their
own ``WinCalls``/``MacCalls``: it makes the spec-to-OS *translation* logic
(parsing, step sequencing, pass/fail, capture-on-failure) unit-testable on
any platform, with a scripted fake standing in for the OS (see
``tests/test_gtk_native_driver.py``) — a real run against vimcode's real GTK
build on a real Linux fleet host under Xvfb is out of reach for this repo's
own test suite and is exercised at the operator level, the same split
``win-native``'s/``mac-native``'s own docstrings call out for real
Win32/UIA/Quartz.

**Spec format matches ``win-native``'s/``mac-native``'s core vocabulary on
purpose** (#3486's acceptance bar: "the same spec file as the other native
kinds"). ``launch``/``key``/``click``/``wait``/``capture``/``expect_a11y``/
``expect_a11y_within``/``expect_closed`` are the exact step names and fields
:mod:`coord.mac_native_driver` already defines (the same shared subset of
:mod:`coord.win_native_driver`'s ``mode: window`` case — no Win32-only
``expect_menu``/``expect_hit``, which have no GTK/X11 analogue). A spec
author writing one of these shared steps gets identical behaviour under any
of the three native drivers, so one spec file can be routed to whichever
platform's native driver is configured for that repo without a per-OS fork.

**Capability routing.** This driver needs a display, ``xdotool``/``xwd``,
and the AT-SPI bindings — a plain Linux build host does not have those by
default. It declares no new plumbing of its own: the repo's
``coordinator.yml`` entry sets ``AcceptanceDriverConfig.capability`` (e.g.
``"gtk-native"``) exactly the way every other driver already does, and
:func:`coord.acceptance.acceptance_capability_gap` /
``smoke_tests.capability_rules``-style routing route it to a host advertising
that capability — the same generic, already-built mechanism ``win-native``
and ``mac-native`` rely on (#966/#3241), not a new one.

**Spec steps** (:func:`parse_native_spec`, YAML):

- ``launch`` — start the command, find its real window, and size/position it
  deterministically via ``xdotool windowmove``/``windowsize``.
- ``key: <name>`` / ``click: {x, y, button}`` — real input at *window-local*
  pixel coordinates, via :meth:`GtkCalls.send_key`/:meth:`GtkCalls.send_click`.
- ``wait: {ms}`` — a plain deterministic pause.
- ``capture`` — an explicit ``xwd -id <windowid>`` evidence snapshot,
  attached to this step's own result (pass or fail) as ``capture_b64``.
- ``expect_a11y: {role, name}`` — the AT-SPI tree must contain a visible
  element matching *role* (exact, case-insensitive) and *name* (substring,
  case-insensitive) right now.
- ``expect_a11y_within: {role, name, timeout_ms}`` — the same match, but
  polled repeatedly until it appears or *timeout_ms* elapses.
- ``expect_closed: {timeout_ms}`` — the window must actually stop existing
  (re-polled via ``xdotool getwindowname`` until it fails) within
  *timeout_ms*. Per #2096, a click is confirmed closed by *observing the
  window gone afterward*, never by the mere absence of an exception from an
  earlier click step.

**Safety: kill only the PID this driver itself launched.** :meth:`GtkCalls.kill`
takes a ``pid: int`` — the exact process id :meth:`GtkCalls.launch` returned
— and nothing in this module ever looks a process up by binary/window-class
name to terminate it. A fleet host's Xvfb session is very likely shared
across concurrent acceptance runs; a teardown that matched by name would be
one bad assumption away from killing something that isn't this driver's own
child. See ``tests/test_gtk_native_driver.py``'s
``test_no_name_based_kill_path_exists_in_the_module`` — a source-level
regression guard, mirroring :mod:`coord.win_native_driver`'s
``test_no_image_name_kill_path_exists_in_the_module`` and
:mod:`coord.mac_native_driver`'s own equivalent.

**Missing-display precheck (#3510).** Unlike ``win-native``/``mac-native``,
a missing display here means no ``$DISPLAY``/``$WAYLAND_DISPLAY`` at all —
no Xvfb/compositor session is up on this host. That's a host-provisioning
condition, not an app bug, so before :meth:`NativeRunner.run` launches
anything it calls :meth:`GtkCalls.session_available`. When unavailable, the
run returns a single ``status="unavailable"`` result — never a ``"fail"``
— and no step (not even ``launch``) runs. ``LinuxGtkCalls`` itself no
longer raises at construction time for a missing display (that check moved
into :meth:`LinuxGtkCalls.session_available` so it's always reachable
through the seam, including from a fake in tests) — only a non-Linux host
is still a construction-time :class:`GtkNativeRuntimeError`.
"""

from __future__ import annotations

import base64
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Protocol

import yaml


class GtkNativeSpecError(Exception):
    """Raised for a malformed native spec: invalid YAML, a missing/empty
    ``steps:`` list, an unknown step ``type``, a missing required field, or
    an unrecognized ``button``."""


class GtkNativeRuntimeError(Exception):
    """Raised when the native driver itself can't run: not on Linux, a
    missing ``xdotool``/``xwd`` binary or AT-SPI binding, the launched
    process/window never appearing, or a step referencing a window before
    any ``launch`` step ran. A missing ``$DISPLAY``/``$WAYLAND_DISPLAY`` is
    NOT one of these (#3510) — that is an "unavailable" lane verdict via
    :meth:`GtkCalls.session_available`, never a raised exception."""


# ── native spec model ───────────────────────────────────────────────────────

# Deliberately the same shared subset :mod:`coord.mac_native_driver` uses —
# see the module docstring's "no per-OS spec fork" section. `expect_menu`/
# `expect_hit` have no GTK/X11 analogue and are intentionally absent here
# rather than stubbed out.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "launch": (),
    "key": ("key",),
    "click": ("x", "y"),
    "wait": ("ms",),
    "capture": (),
    "expect_a11y": ("role", "name"),
    "expect_a11y_within": ("role", "name"),
    "expect_closed": (),
}

_VALID_BUTTONS = ("left", "right", "middle")


@dataclass(frozen=True)
class NativeStep:
    """One parsed step of a gtk-native spec. Mirrors
    :class:`coord.mac_native_driver.NativeStep`'s "every unused field keeps
    its default" shape — callers never have to branch on ``kind`` before
    reading one."""

    kind: str
    index: int
    id: str = ""
    key: str = ""
    x: int = 0
    y: int = 0
    button: str = ""
    ms: int = 0
    timeout_ms: int = 5000
    role: str = ""
    name: str = ""

    @property
    def step_id(self) -> str:
        return self.id or f"{self.index:03d} {self.kind}"


@dataclass(frozen=True)
class NativeSpec:
    name: str
    width: int
    height: int
    steps: tuple[NativeStep, ...]


def _int_default(value, default: int) -> int:
    """``int(value)``, falling back to *default* only when *value* is
    absent (``None``) — an explicit literal ``0`` in the YAML is honored
    rather than silently treated as "absent" (mirrors
    :func:`coord.mac_native_driver._int_default`)."""
    return default if value is None else int(value)


def parse_native_spec(yaml_text: str) -> NativeSpec:
    """Parse a gtk-native spec YAML document into a :class:`NativeSpec`.

    Top level: ``name:`` (optional), ``width:``/``height:`` (optional,
    default 1024x768 — the window-resize target), ``steps:`` — a non-empty
    list of mappings each carrying a ``type:`` from :data:`_REQUIRED_FIELDS`.

    Raises :class:`GtkNativeSpecError` — never returns a partially-parsed
    spec — for: invalid YAML, a non-mapping document, a missing/empty/
    non-list ``steps:``, a step that isn't a mapping, an unknown ``type:``, a
    step missing one of its type's required fields, or an unrecognized
    ``button:``.
    """
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise GtkNativeSpecError(f"native spec is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise GtkNativeSpecError("native spec must be a YAML mapping at the top level")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise GtkNativeSpecError("native spec must have a non-empty 'steps:' list")

    steps: list[NativeStep] = []
    for i, entry in enumerate(steps_raw):
        if not isinstance(entry, dict):
            raise GtkNativeSpecError(f"steps[{i}] must be a mapping")
        kind = entry.get("type")
        if kind not in _REQUIRED_FIELDS:
            raise GtkNativeSpecError(
                f"steps[{i}]: unknown step type {kind!r} — expected one of "
                f"{', '.join(sorted(_REQUIRED_FIELDS))}"
            )
        missing = [f for f in _REQUIRED_FIELDS[kind] if entry.get(f) in (None, "")]
        if missing:
            raise GtkNativeSpecError(
                f"steps[{i}] (type={kind!r}) is missing required field(s): "
                f"{', '.join(missing)}"
            )

        button = str(entry.get("button", "") or "")
        if button and button not in _VALID_BUTTONS:
            raise GtkNativeSpecError(
                f"steps[{i}]: unrecognized button {button!r} — expected one "
                f"of {', '.join(_VALID_BUTTONS)}"
            )

        steps.append(NativeStep(
            kind=kind,
            index=i,
            id=str(entry.get("id", "") or ""),
            key=str(entry.get("key", "") or ""),
            x=_int_default(entry.get("x"), 0),
            y=_int_default(entry.get("y"), 0),
            button=button,
            ms=_int_default(entry.get("ms"), 0),
            timeout_ms=_int_default(entry.get("timeout_ms"), 5000),
            role=str(entry.get("role", "") or ""),
            name=str(entry.get("name", "") or ""),
        ))

    return NativeSpec(
        name=str(raw.get("name", "") or ""),
        width=int(raw.get("width", 1024) or 1024),
        height=int(raw.get("height", 768) or 768),
        steps=tuple(steps),
    )


# ── the injectable OS-call seam ─────────────────────────────────────────────

class GtkCalls(Protocol):
    """The minimal set of real-OS operations :class:`NativeRunner` drives a
    native GTK app through — implemented for real by :class:`LinuxGtkCalls`
    (Linux-only), and by a scripted fake in
    ``tests/test_gtk_native_driver.py`` so the spec-to-OS *translation*
    logic is testable on any platform.

    ``launch`` returns the PID of the process THIS call started;
    :meth:`kill` must accept only that same PID back — never a binary or
    window-class name — so teardown can never take down a process it didn't
    itself launch (see the module docstring's safety note).
    """

    def launch(self, command: str, cwd: str) -> int: ...

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        """Poll for the launched process's real, *visible* top-level window,
        returning its X11 window id once found. Raises
        :class:`GtkNativeRuntimeError` if none appears within *timeout_s* —
        this is the "launch" step's own confirmation that the process didn't
        just start, but actually produced a window (#2096).

        Must filter to windows that are actually mapped/viewable (#3605):
        GTK4-on-X11 creates an internal, invisible helper top-level before
        the real app window, with a lower XID, so a plain
        ``xdotool search --pid`` lists it first. Returning that helper
        corrupts every subsequent ``move_window``/``send_click``/
        ``send_key``/``capture`` call against this window id — clicks land
        nowhere a user can see, and ``capture`` fails outright
        (``xwd``/``XGetImage`` can't read an unmapped window)."""
        ...

    def move_window(self, window_id: int, x: int, y: int, width: int, height: int) -> None: ...

    def is_window_alive(self, window_id: int) -> bool: ...

    def send_click(self, window_id: int, x: int, y: int, button: str) -> None: ...

    def send_key(self, window_id: int, key: str) -> None: ...

    def ax_elements(self, pid: int) -> list[dict]:
        """Every element in the app's AT-SPI accessibility tree right now,
        each as ``{"role": str, "name": str, "visible": bool}``."""
        ...

    def capture(self, window_id: int) -> bytes:
        """An ``xwd -id <window_id>`` capture of *window_id* right now
        (works even when covered by another window) — raises
        :class:`GtkNativeRuntimeError` on failure rather than returning
        empty bytes, since a capture step exists specifically to produce
        evidence and has nothing to report if it can't."""
        ...

    def kill(self, pid: int) -> None: ...

    def session_available(self) -> tuple[bool, str]:
        """``(True, "")`` when a usable display is present for this driver
        to launch into; ``(False, reason)`` when neither ``$DISPLAY`` nor
        ``$WAYLAND_DISPLAY`` is set (#3510) — checked by
        :meth:`NativeRunner.run` BEFORE any step (including ``launch``)
        runs, so a missing display is reported as ``status="unavailable"``
        rather than a failed step. Never raises — a probe failure here is
        itself an "unavailable" verdict, not a crash."""
        ...


def _find_a11y_match(elements: list[dict], role: str, name: str) -> dict | None:
    """The first *elements* entry whose ``role`` matches exactly
    (case-insensitive) and ``name`` matches as a substring
    (case-insensitive), and which is not explicitly marked invisible — or
    ``None`` if nothing matches. An empty *name* matches any name. Identical
    to :func:`coord.mac_native_driver._find_a11y_match`/
    :func:`coord.win_native_driver._find_a11y_match` — same contract,
    different tree source (AT-SPI vs AX vs UIA)."""
    role_l = role.lower()
    name_l = name.lower()
    for el in elements:
        if not isinstance(el, dict):
            continue
        if el.get("visible") is False:
            continue
        if str(el.get("role", "")).lower() != role_l:
            continue
        if name_l and name_l not in str(el.get("name", "")).lower():
            continue
        return el
    return None


def _summarize_elements(elements: list[dict]) -> str:
    return ", ".join(
        f"{el.get('role', '?')}:{el.get('name', '')!r}"
        for el in elements if isinstance(el, dict)
    ) or "(empty tree)"


# ── the spec-step executor ──────────────────────────────────────────────────

class NativeRunner:
    """Drives one :class:`NativeSpec` against an injected :class:`GtkCalls`,
    producing coord's normalized ``{"id", "status", "message"}`` verdict
    list (plus ``capture_b64`` on failing steps when a capture could be
    taken) — the same shape
    :func:`coord.mac_native_driver.run_native_spec`/
    :func:`coord.win_native_driver.run_native_spec` already produce.
    """

    def __init__(
        self, calls: GtkCalls, command: str, cwd: str, *, deadline: float | None = None,
    ) -> None:
        self._calls = calls
        self._command = command
        self._cwd = cwd
        self._deadline = deadline
        self._spec: NativeSpec | None = None
        self._pid: int | None = None
        self._window_id: int | None = None

    def run(self, spec: NativeSpec) -> list[dict]:
        """Run *spec*, first checking :meth:`GtkCalls.session_available`
        (#3510). A missing display is a host-provisioning condition, not an
        app bug: when unavailable, this returns a single
        ``status="unavailable"`` entry and runs NO step at all (not even
        ``launch``) — never folding it into an ordinary ``"fail"``."""
        self._spec = spec
        available, reason = self._calls.session_available()
        if not available:
            return [{
                "id": "session",
                "status": "unavailable",
                "message": reason or "no usable display is available",
            }]
        results: list[dict] = []
        try:
            for step in spec.steps:
                if self._deadline is not None and time.monotonic() >= self._deadline:
                    results.append({
                        "id": step.step_id, "status": "fail",
                        "message": "aborted: gtk-native driver-level timeout exceeded",
                    })
                    continue
                results.append(self._run_step(step))
        finally:
            self._teardown()
        return results

    def _run_step(self, step: NativeStep) -> dict:
        handlers = {
            "launch": self._do_launch,
            "key": self._do_key,
            "click": self._do_click,
            "wait": self._do_wait,
            "capture": self._do_capture,
            "expect_a11y": self._do_expect_a11y,
            "expect_a11y_within": self._do_expect_a11y_within,
            "expect_closed": self._do_expect_closed,
        }
        entry: dict = {"id": step.step_id, "status": "pass", "message": ""}
        try:
            extra = handlers[step.kind](step)
            if extra:
                entry.update(extra)
        except (GtkNativeSpecError, GtkNativeRuntimeError, AssertionError) as e:
            entry["status"] = "fail"
            entry["message"] = str(e)
            self._attach_capture_if_possible(entry)
        return entry

    def _attach_capture_if_possible(self, entry: dict) -> None:
        """Best-effort ``xwd`` evidence attached to *entry* — never raises,
        and never masks the real failure reason in ``message`` if the
        capture itself can't be taken."""
        if self._window_id is None:
            return
        try:
            image = self._calls.capture(self._window_id)
        except Exception as e:  # noqa: BLE001 — evidence is best-effort
            entry["capture_error"] = str(e)
            return
        if image:
            entry["capture_b64"] = base64.b64encode(image).decode("ascii")

    def _require_window(self) -> tuple[int, int]:
        if self._pid is None or self._window_id is None:
            raise GtkNativeRuntimeError(
                "no window — spec has no 'launch' step before this one"
            )
        return self._pid, self._window_id

    # -- action steps --

    def _do_launch(self, step: NativeStep) -> dict | None:
        spec = self._spec
        assert spec is not None
        pid = self._calls.launch(self._command, self._cwd)
        self._pid = pid
        timeout_s = (step.timeout_ms or 10000) / 1000
        window_id = self._calls.find_top_window(pid, timeout_s)
        self._window_id = window_id
        self._calls.move_window(window_id, 0, 0, spec.width, spec.height)
        return None

    def _do_key(self, step: NativeStep) -> None:
        _, window_id = self._require_window()
        self._calls.send_key(window_id, step.key)

    def _do_click(self, step: NativeStep) -> None:
        _, window_id = self._require_window()
        self._calls.send_click(window_id, step.x, step.y, step.button or "left")

    def _do_wait(self, step: NativeStep) -> None:
        time.sleep(step.ms / 1000)

    def _do_capture(self, step: NativeStep) -> dict:
        _, window_id = self._require_window()
        image = self._calls.capture(window_id)
        return {"capture_b64": base64.b64encode(image).decode("ascii")}

    # -- assertion steps --

    def _do_expect_a11y(self, step: NativeStep) -> None:
        pid, _ = self._require_window()
        elements = self._calls.ax_elements(pid)
        if _find_a11y_match(elements, step.role, step.name) is None:
            raise AssertionError(
                f"no AT-SPI element found with role={step.role!r} "
                f"name={step.name!r}; tree had: {_summarize_elements(elements)}"
            )

    def _do_expect_a11y_within(self, step: NativeStep) -> dict:
        pid, _ = self._require_window()
        start = time.monotonic()
        deadline = start + step.timeout_ms / 1000
        while True:
            elements = self._calls.ax_elements(pid)
            if _find_a11y_match(elements, step.role, step.name) is not None:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return {"message": f"appeared after {elapsed_ms}ms"}
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"expected role={step.role!r} name={step.name!r} within "
                    f"{step.timeout_ms}ms; never appeared. tree had: "
                    f"{_summarize_elements(elements)}"
                )
            time.sleep(0.02)

    def _do_expect_closed(self, step: NativeStep) -> None:
        """#2096: confirmed by re-polling the window until it actually
        reports gone — never by the mere absence of an exception from an
        earlier click step."""
        _, window_id = self._require_window()
        deadline = time.monotonic() + (step.timeout_ms or 5000) / 1000
        while True:
            if not self._calls.is_window_alive(window_id):
                return
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"window still alive {step.timeout_ms}ms after "
                    f"expect_closed — it did not actually close"
                )
            time.sleep(0.02)

    # -- teardown --

    def _teardown(self) -> None:
        """Kills only the PID this run itself launched (see the module
        docstring's safety note) — never by binary/window-class name, and
        never if `launch` never ran (`self._pid` stays `None`)."""
        if self._pid is not None:
            try:
                self._calls.kill(self._pid)
            except Exception:  # noqa: BLE001 — teardown must not mask the real result
                pass


# ── real Linux/X11 implementation (Linux-only) ──────────────────────────────

_NAMED_KEYSYMS: dict[str, str] = {
    # X11 keysym names a spec author can reference by the same lowercase
    # vocabulary :mod:`coord.mac_native_driver`/:mod:`coord.win_native_driver`
    # already use — translated to xdotool's own (mostly self-explanatory)
    # keysym spelling.
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "backspace": "BackSpace", "delete": "Delete", "space": "space",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5", "f6": "F6",
    "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
}

_BUTTON_NUMBERS: dict[str, int] = {"left": 1, "middle": 2, "right": 3}


def _xdotool_key_for(key: str) -> str:
    """The ``xdotool key``/``keydown`` argument for one spec ``key:`` name.
    Raises :class:`GtkNativeSpecError` for anything unrecognized. Mirrors
    :func:`coord.mac_native_driver._vkey_for`'s/
    :func:`coord.win_native_driver._vkey_for`'s contract, just against
    xdotool's own (mostly pass-through) keysym naming — ``xdotool`` already
    accepts ``ctrl+c``-style modifier combos and single-character keysyms
    (including uppercase, which it shifts for automatically) verbatim, so
    there is no separate needs-shift bit to track here."""
    lowered = key.lower()
    if lowered in _NAMED_KEYSYMS:
        return _NAMED_KEYSYMS[lowered]
    if lowered.startswith("ctrl+") and len(lowered) == 6:
        return f"ctrl+{lowered[5]}"
    if len(key) == 1:
        return key
    raise GtkNativeSpecError(f"unrecognized key {key!r}")


class LinuxGtkCalls:
    """The real :class:`GtkCalls` implementation — ``xdotool`` (a system
    package, not a pip dependency) for window discovery, input injection and
    window management, the ``Atspi`` GObject-introspection binding
    (``gi.repository.Atspi`` — also a system package, ``gir1.2-atspi-2.0`` /
    ``python3-gi``) for the accessibility tree, and ``xwd`` (ships with every
    X11 install) for window captures.

    Linux-only: raises :class:`GtkNativeRuntimeError` at construction on any
    other platform, mirroring :class:`coord.mac_native_driver.MacOSCalls`'s/
    :class:`coord.win_native_driver.Win32Calls`'s own platform guards. A
    missing ``$DISPLAY``/``$WAYLAND_DISPLAY`` is NOT a construction-time
    raise (#3510) — see :meth:`session_available`, checked by
    :class:`NativeRunner` before any step runs. This driver never starts its
    own ``Xvfb`` — see the module docstring's "headless by construction"
    note.
    """

    def __init__(self) -> None:
        if not _is_linux():
            raise GtkNativeRuntimeError(
                "LinuxGtkCalls requires Linux — the gtk-native driver only "
                "runs on a real Linux host with a display (e.g. a fleet "
                "host running a persistent Xvfb session)"
            )
        # Deliberately NOT a construction-time raise for a missing display
        # (#3510) — that is an "unavailable" lane verdict, not a driver
        # construction failure, and is checked (and reachable through the
        # same `GtkCalls` seam a test's fake implements) via
        # :meth:`session_available`, called by `NativeRunner.run` before any
        # step — see the module docstring's "missing-display precheck".

    # -- session precheck (#3510) --

    def session_available(self) -> tuple[bool, str]:
        """Real check: either ``$DISPLAY`` (X11) or ``$WAYLAND_DISPLAY``
        (Wayland) must be set — no headless compositor or Xvfb session
        appears to be running on this host otherwise."""
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            return True, ""
        return False, (
            "neither $DISPLAY nor $WAYLAND_DISPLAY is set — no headless "
            "compositor or Xvfb session appears to be running on this host"
        )

    # -- process lifecycle --

    def launch(self, command: str, cwd: str) -> int:
        proc = subprocess.Popen(command, shell=True, cwd=cwd or None)
        return proc.pid

    def kill(self, pid: int) -> None:
        # By PID only — see the module docstring's safety note. No
        # binary/window-class-based lookup exists anywhere in this class.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        # #3605: `--onlyvisible` is load-bearing. GTK4-on-X11 creates an
        # invisible internal helper top-level *before* the real app window,
        # so it gets a lower XID and a plain `xdotool search --pid` lists it
        # first — `list[0]` would then be the helper, not the real, visible
        # app window, and every subsequent move/click/key/capture call
        # against that window id silently targets an unmapped window
        # (xwd/XGetImage can't even read one). `--onlyvisible` makes
        # xdotool itself filter to windows that are actually mapped
        # (IsViewable), which is exactly the distinction that failed here.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            proc = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--pid", str(pid)],
                capture_output=True, text=True, timeout=10,
            )
            ids = [int(tok) for tok in proc.stdout.split() if tok.strip().isdigit()]
            if ids:
                return ids[0]
            time.sleep(0.1)
        raise GtkNativeRuntimeError(
            f"no visible window appeared for pid={pid} within {timeout_s}s"
        )

    def is_window_alive(self, window_id: int) -> bool:
        proc = subprocess.run(
            ["xdotool", "getwindowname", str(window_id)],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0

    def move_window(self, window_id: int, x: int, y: int, width: int, height: int) -> None:
        subprocess.run(
            ["xdotool", "windowmove", str(window_id), str(x), str(y)],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["xdotool", "windowsize", str(window_id), str(width), str(height)],
            capture_output=True, timeout=10,
        )

    # -- input injection --

    def send_click(self, window_id: int, x: int, y: int, button: str) -> None:
        button_num = _BUTTON_NUMBERS[button]
        subprocess.run(
            [
                "xdotool", "mousemove", "--window", str(window_id), str(x), str(y),
                "click", str(button_num),
            ],
            capture_output=True, timeout=10,
        )

    def send_key(self, window_id: int, key: str) -> None:
        xdotool_key = _xdotool_key_for(key)
        subprocess.run(
            ["xdotool", "key", "--window", str(window_id), xdotool_key],
            capture_output=True, timeout=10,
        )

    # -- AT-SPI --

    def ax_elements(self, pid: int) -> list[dict]:
        atspi = _import_atspi()
        elements: list[dict] = []
        desktop = atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(i)
                app_pid = app.get_process_id()
            except Exception:  # noqa: BLE001 — a dead/unreadable app entry
                continue
            if app_pid != pid:
                continue
            _walk_atspi(atspi, app, elements)
        return elements

    # -- capture --

    def capture(self, window_id: int) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".xwd", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                ["xwd", "-id", str(window_id), "-out", tmp_path, "-silent"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0 or not os.path.exists(tmp_path):
                raise GtkNativeRuntimeError(
                    f"xwd failed for window_id={window_id}: "
                    f"{proc.stderr.strip() if proc.stderr else '(no stderr)'}"
                )
            with open(tmp_path, "rb") as f:
                data = f.read()
            if not data:
                raise GtkNativeRuntimeError(
                    f"xwd produced an empty file for window_id={window_id}"
                )
            return data
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _walk_atspi(atspi, node, elements: list[dict]) -> None:
    """Recursively collect every AT-SPI node under *node* into *elements* —
    the ``gi.repository.Atspi`` analogue of
    :func:`coord.mac_native_driver.MacOSCalls.ax_elements`'s AX-tree walk."""
    try:
        role = node.get_role_name() or ""
        name = node.get_name() or ""
        state_set = node.get_state_set()
        visible = (
            state_set.contains(atspi.StateType.VISIBLE) if state_set else True
        )
        elements.append({"role": str(role), "name": str(name), "visible": bool(visible)})
    except Exception:  # noqa: BLE001 — a dead/stale node
        return
    try:
        count = node.get_child_count()
    except Exception:  # noqa: BLE001
        return
    for i in range(count):
        try:
            child = node.get_child_at_index(i)
        except Exception:  # noqa: BLE001
            continue
        _walk_atspi(atspi, child, elements)


def _is_linux() -> bool:
    return os.uname().sysname == "Linux" if hasattr(os, "uname") else False


def _import_atspi():
    """The optional ``Atspi`` GObject-introspection binding (the
    ``gtk-native`` capability's accessibility half) — guarded the same way
    :func:`coord.mac_native_driver._import_ax`/
    :func:`coord.win_native_driver._import_uia` guard their own optional
    dependencies, so a missing binding names the system packages to install
    rather than surfacing a bare ``ModuleNotFoundError``.

    Unlike ``win-native``'s ``comtypes``/``mac-native``'s ``pyobjc-framework-*``,
    this is deliberately NOT a pip extra: AT-SPI's Python bindings are a
    GObject-introspection binding shipped by the distro (``gir1.2-atspi-2.0``
    + ``python3-gi`` on Debian/Ubuntu), not a package PyPI can resolve —
    installing it is a fleet-host provisioning step, the same category as
    installing ``xdotool``/``xwd`` themselves.
    """
    try:
        import gi  # noqa: PLC0415
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: PLC0415
    except (ModuleNotFoundError, ValueError) as exc:
        raise GtkNativeRuntimeError(
            "gtk-native needs the AT-SPI GObject-introspection binding, "
            "which is not installed (missing 'gi.repository.Atspi').\n"
            "  Install it with (Debian/Ubuntu):  "
            "apt install gir1.2-atspi-2.0 python3-gi"
        ) from exc

    return Atspi


# ── top-level entry point ───────────────────────────────────────────────────

class GtkNativeSession:
    """Persistent gtk-native ``coord app-drive`` session (#3590) — mirrors
    :class:`coord.tui_pty_driver.TuiPtySession`'s "sanctioned driver, one
    verb per call" shape: drives the exact same :class:`GtkCalls` real-OS
    implementation :class:`NativeRunner`'s own ``launch``/``key``/``click``/
    ``capture`` step handlers use (same PID-only ``kill``, never by binary
    or window-class name), just invoked on demand rather than against one
    fixed step list.

    Callers (:mod:`coord.app_drive`'s daemon dispatch) MUST confirm
    :meth:`GtkCalls.session_available` themselves BEFORE constructing this
    — exactly like :meth:`NativeRunner.run` only reaches its own per-step
    handlers after that same check; this class assumes an
    already-confirmed-available session.
    """

    def __init__(
        self, launch_command: str, cwd: str, *, width: int = 1024, height: int = 768,
        calls: GtkCalls | None = None, timeout_s: float = 10.0,
    ) -> None:
        self._calls: GtkCalls = calls if calls is not None else LinuxGtkCalls()
        self._pid = self._calls.launch(launch_command, cwd)
        self._window_id = self._calls.find_top_window(self._pid, timeout_s)
        self._calls.move_window(self._window_id, 0, 0, width, height)

    @property
    def pid(self) -> int:
        """The pid :meth:`GtkCalls.launch` returned (#3590) — read by
        :mod:`coord.app_drive_daemon` for its ready-file's ``app_pid`` so
        :func:`coord.app_drive.close_session` can re-observe/re-signal the
        process this session itself launched, not just the daemon.

        NOT guaranteed to be the real app's own pid: :meth:`LinuxGtkCalls
        .launch` is a plain ``subprocess.Popen(command, shell=True)``, so
        this is the shell's pid whenever *command* forks rather than execs
        into the real binary — the same gap documented on
        :class:`coord.mac_native_driver.MacNativeSession.pid`; tracked
        separately from #3590."""
        return self._pid

    def send_key(self, key: str) -> None:
        self._calls.send_key(self._window_id, key)

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        self._calls.send_click(self._window_id, x, y, button)

    def capture(self) -> bytes:
        return self._calls.capture(self._window_id)

    def probe(self, name: str, **kwargs: Any) -> Any:
        if name == "ax_elements":
            return self._calls.ax_elements(self._pid)
        if name == "is_window_alive":
            return self._calls.is_window_alive(self._window_id)
        if name == "find_a11y":
            elements = self._calls.ax_elements(self._pid)
            match = _find_a11y_match(elements, kwargs.get("role", ""), kwargs.get("name", ""))
            return {
                "found": match is not None, "element": match,
                "tree": _summarize_elements(elements),
            }
        raise ValueError(
            f"unknown probe {name!r} — expected one of: ax_elements, "
            "is_window_alive, find_a11y"
        )

    def is_alive(self) -> bool:
        return self._calls.is_window_alive(self._window_id)

    def close(self) -> None:
        """Kills only the PID this session itself launched (see the module
        docstring's safety note)."""
        try:
            self._calls.kill(self._pid)
        except Exception:  # noqa: BLE001 — teardown must not raise
            pass


def run_native_spec(
    spec_text: str, *, launch_command: str, cwd: str,
    calls: GtkCalls | None = None, timeout: float | None = None,
) -> list[dict]:
    """Parse *spec_text* and run it against *calls* (a real
    :class:`LinuxGtkCalls` by default) launching *launch_command* in *cwd* —
    the top-level entry point
    :func:`coord.acceptance_drivers._run_gtk_native` calls.

    *timeout*, when given, is an overall wall-clock budget in seconds for
    the whole spec — mirrors :func:`coord.mac_native_driver.run_native_spec`'s/
    :func:`coord.win_native_driver.run_native_spec`'s own ``timeout``: each
    step already carries its own bounded per-step budget, but their sum can
    still exceed it, in which case every remaining step fails explicitly
    rather than the run truncating or blocking past it.

    Raises :class:`GtkNativeSpecError` for a malformed spec. Runtime
    failures (the process never launches, a window never appears, an
    assertion fails) do NOT raise — they're folded into the returned list
    as a ``status="fail"`` entry, the same "partial results, not a crash"
    contract :func:`coord.mac_native_driver.run_native_spec` already gives.

    When *calls* reports no usable display
    (:meth:`GtkCalls.session_available`, #3510), the returned list is a
    single ``status="unavailable"`` entry and no step runs — a distinct
    verdict from ``"fail"``, since a missing display is a host-provisioning
    condition, not an app bug.
    """
    spec = parse_native_spec(spec_text)
    resolved_calls = calls if calls is not None else LinuxGtkCalls()
    deadline = time.monotonic() + timeout if timeout is not None else None
    runner = NativeRunner(resolved_calls, launch_command, cwd, deadline=deadline)
    return runner.run(spec)
