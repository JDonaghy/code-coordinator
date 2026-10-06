"""``mac-native`` acceptance driver — Tier 2's macOS-native tier (#3485).

``tui-pty`` (#3483) and ``win-native`` (#3484) both proved the same point on
two different platforms: an in-process harness can't see real
window-manager/OS behaviour. macOS has the identical blind spot — vimcode's
macOS suite is thin (~22 driver tests against ~277 for GTK) and, like the
Windows build before #3484 landed, has never once asked the real OS "is
there actually an accessible element here?" or "does a real click at this
pixel actually land?". Those are Accessibility/Quartz questions, answerable
only by asking macOS.

This driver launches the driven repo's real compiled ``.app``/binary, finds
its real window, and probes it with real OS calls:

- ``CGEvent`` — real mouse/keyboard input injected at the HID event tap
  (``CGEventPost``), not an in-process event queue.
- the Accessibility (AX) tree (``AXUIElementCreateApplication`` +
  ``AXUIElementCopyAttributeValue``) — the same tree VoiceOver (or a real
  user) sees, covering the ``expect_a11y``/``expect_a11y_within`` steps.
- ``screencapture -l <windowid>`` — a window-specific capture (works even
  when the window is covered by another one), attached as evidence to every
  *failing* step (see :meth:`NativeRunner._attach_capture_if_possible`).
- ``CGWindowListCopyWindowInfo`` — finding the launched process's real
  window and polling whether it still exists (``expect_closed``).

**Injectable OS-call seam.** Every actual Quartz/AX/``screencapture`` call is
one method on the :class:`MacCalls` protocol, implemented for real by
:class:`MacOSCalls` (macOS-only, ``pyobjc`` — the ``mac-native`` extra).
:class:`NativeRunner` — the spec-step executor — never calls a Quartz/AX API
directly; it only calls through ``MacCalls``. This is the same seam
:mod:`coord.win_native_driver` uses for ``WinCalls``: it makes the
spec-to-OS *translation* logic (parsing, step sequencing, pass/fail,
capture-on-failure) unit-testable on any platform, with a scripted fake
standing in for the OS (see ``tests/test_mac_native_driver.py``) — a real run
against a real ``.app`` on real macOS hardware (macmini) is out of reach for
this repo's own test suite and is exercised at the operator level, the same
split :mod:`coord.win_native_driver`'s own docstring calls out for real
Win32/UIA.

**Spec format matches ``win-native``'s core vocabulary on purpose** (#3485's
acceptance bar: "the same spec file as ``tui-pty`` and ``win-native``, with
no macOS-specific spec forks"). ``launch``/``key``/``click``/``wait``/
``capture``/``expect_a11y``/``expect_a11y_within``/``expect_closed`` are the
exact step names and fields :mod:`coord.win_native_driver` already defines
for its ``mode: window`` case (everything except the Win32-only
``expect_menu``/``expect_hit``, which have no macOS analogue — there is no
native ``HMENU``/``WM_NCHITTEST`` on this platform — and the Windows-
Terminal-hosted-mode steps, which are a ConPTY-console concept). A spec
author writing one of these shared steps gets identical behaviour under
either driver, so one spec file can be routed to whichever platform's
``mac-native``/``win-native`` driver is configured for that repo without a
per-OS fork.

**Spec steps** (:func:`parse_native_spec`, YAML):

- ``launch`` — start the command (or ``open -W`` a ``.app`` bundle), find its
  real window, and size/position it deterministically.
- ``key: <name>`` / ``click: {x, y, button}`` — real input at *screen* pixel
  coordinates relative to the window's origin, via
  :meth:`MacCalls.send_key`/:meth:`MacCalls.send_click`. ``key:`` is parsed
  under the grammar shared by all four drivers (:mod:`coord.key_spec`, #3639):
  any combination of ``ctrl``/``alt``(``option``)/``shift``/``cmd`` modifiers,
  a named key or any single printable character (including punctuation), and
  a space-separated chord sequence (``ctrl+k ctrl+w``) — see
  :func:`_mac_key_encodings`.
- ``wait: {ms}`` — a plain deterministic pause.
- ``capture`` — an explicit ``screencapture -l <windowid>`` evidence
  snapshot, attached to this step's own result (pass or fail) as
  ``capture_b64``.
- ``expect_a11y: {role, name}`` — the AX tree must contain a visible element
  matching *role* (exact, case-insensitive against ``AXRole``) and *name*
  (substring, case-insensitive against ``AXTitle``/``AXDescription``/
  ``AXValue``) right now.
- ``expect_a11y_within: {role, name, timeout_ms}`` — the same match, but
  polled repeatedly until it appears or *timeout_ms* elapses.
- ``expect_closed: {timeout_ms}`` — the window must actually stop existing
  (re-polled via ``CGWindowListCopyWindowInfo`` until gone) within
  *timeout_ms*. Per #2096, a click is confirmed closed by *observing the
  window gone afterward*, never by the mere absence of an exception from an
  earlier click step.

**Safety: kill only the PID this driver itself launched.** :meth:`MacCalls.kill`
takes a ``pid: int`` — the exact process id :meth:`MacCalls.launch` returned
— and nothing in this module ever looks a process up by bundle identifier or
executable name to terminate it. An operator's own macmini session is very
likely running other apps concurrently; a teardown that matched by name would
be one bad assumption away from killing something that isn't this driver's
own child. See ``tests/test_mac_native_driver.py``'s
``test_no_name_based_kill_path_exists_in_the_module`` — a source-level
regression guard, mirroring :mod:`coord.win_native_driver`'s own
``test_no_image_name_kill_path_exists_in_the_module``.

**Frontmost refusal, never a blind retry (#3566).** The first live
`mac-native` bugbash attempt found `AXIsProcessTrusted() == False`, then
improvised: Terminal.app `do script`, a hand-rolled key-injection helper,
clicks sent while its own log read `frontmost confirmed: False` with the
actual frontmost pid belonging to the operator's iTerm2. Those landed in the
operator's live session — a context menu, a "terminate running process?"
dialog. :meth:`NativeRunner._do_key`/:meth:`_do_click` now call
:meth:`MacCalls.is_frontmost` immediately before every single key/click and
refuse (:class:`MacNativeRuntimeError`, folded into an ordinary failing step
— never retried into whatever window happens to be in front) unless the
launched pid is frontmost *right now*. :meth:`MacOSCalls.send_click`/
:meth:`send_key` additionally post through `CGEventPostToPid` rather than
the global `CGEventPost(kCGHIDEventTap, ...)` HID tap — input is addressed
to the launched process directly rather than broadcast to whichever window
the real pointer/keyboard focus happens to be on, so even a frontmost-check
race lands on the intended process rather than an arbitrary other one.

**Locked/absent session precheck (#3510).** The same vimcode#1629 class of
problem applies here: a locked screen or no GUI session (headless/SSH-only)
is an environment condition, not an app bug. Before :meth:`NativeRunner.run`
launches anything it calls :meth:`MacCalls.session_available` — real check:
``CGSessionCopyCurrentDictionary`` must return a session, with
``CGSSessionScreenIsLocked`` false and ``kCGSessionOnConsoleKey`` true. When
unavailable, the run returns a single ``status="unavailable"`` result —
never a ``"fail"`` — and no step (not even ``launch``) runs. See
:mod:`coord.win_native_driver`'s own module docstring for the same shape.

**Accessibility-trust precheck, in production, not just in a test fixture
(#3566).** The locked-screen precheck above answers "is there a session to
launch into" — it says nothing about whether THIS process identity actually
holds Accessibility trust, which is the exact incident this driver exists to
prevent: the first live `mac-native` bugbash attempt found
`AXIsProcessTrusted() == False` deep inside a worker and, with nothing in
the driver itself to catch it, improvised unsafe workarounds instead (see
the frontmost-refusal note above). Immediately after `session_available`,
:meth:`NativeRunner.run` also calls :meth:`MacCalls.ax_trust_available` —
real check: `ApplicationServices.AXIsProcessTrusted()`, asked IN-PROCESS
(unlike `coord.prereqs`'s own `/health` probe, which deliberately asks from
a fresh subprocess because it runs inside the long-lived `coord agent`
process — a different identity than the worker that will actually drive
input; here, `NativeRunner.run` already runs INSIDE the worker's own
process, so asking in-process asks the right identity directly). A denied
grant returns a single `status="unavailable"` result — same shape and same
non-"fail" treatment as the session precheck — before `launch` or any other
step runs, so a missing grant fails fast with a real, worker-emitted verdict
instead of every subsequent `send_key`/`send_click` silently (or unsafely)
failing later. The message text deliberately matches
`coord.bugbash._UNAVAILABLE_SIGNATURES`'s `"AXIsProcessTrusted() is False"`
entry, so a worker's raw transcript carries the real driver's own words, not
just a hand-written test string.
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Protocol

import yaml

from coord.key_spec import KeyChord, KeySpecError, UnsupportedKey, parse_key_spec


class MacNativeSpecError(Exception):
    """Raised for a malformed native spec: invalid YAML, a missing/empty
    ``steps:`` list, an unknown step ``type``, a missing required field, or
    an unrecognized ``button``."""


class MacNativeRuntimeError(Exception):
    """Raised when the native driver itself can't run: not on macOS, a
    missing optional dependency (the ``mac-native`` extra), the launched
    process/window never appearing, or a step referencing a window before
    any ``launch`` step ran."""


# ── native spec model ───────────────────────────────────────────────────────

# Deliberately the win-native subset shared across both platforms — see the
# module docstring's "no macOS-specific spec forks" section. `expect_menu`/
# `expect_hit`/the terminal-hosted-mode steps have no macOS analogue and are
# intentionally absent here rather than stubbed out.
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
    """One parsed step of a mac-native spec. Mirrors
    :class:`coord.win_native_driver.NativeStep`'s "every unused field keeps
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
    :func:`coord.win_native_driver._int_default`)."""
    return default if value is None else int(value)


def parse_native_spec(yaml_text: str) -> NativeSpec:
    """Parse a mac-native spec YAML document into a :class:`NativeSpec`.

    Top level: ``name:`` (optional), ``width:``/``height:`` (optional,
    default 1024x768 — the window-resize target), ``steps:`` — a non-empty
    list of mappings each carrying a ``type:`` from :data:`_REQUIRED_FIELDS`.

    Raises :class:`MacNativeSpecError` — never returns a partially-parsed
    spec — for: invalid YAML, a non-mapping document, a missing/empty/
    non-list ``steps:``, a step that isn't a mapping, an unknown ``type:``, a
    step missing one of its type's required fields, or an unrecognized
    ``button:``.
    """
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise MacNativeSpecError(f"native spec is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise MacNativeSpecError("native spec must be a YAML mapping at the top level")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise MacNativeSpecError("native spec must have a non-empty 'steps:' list")

    steps: list[NativeStep] = []
    for i, entry in enumerate(steps_raw):
        if not isinstance(entry, dict):
            raise MacNativeSpecError(f"steps[{i}] must be a mapping")
        kind = entry.get("type")
        if kind not in _REQUIRED_FIELDS:
            raise MacNativeSpecError(
                f"steps[{i}]: unknown step type {kind!r} — expected one of "
                f"{', '.join(sorted(_REQUIRED_FIELDS))}"
            )
        missing = [f for f in _REQUIRED_FIELDS[kind] if entry.get(f) in (None, "")]
        if missing:
            raise MacNativeSpecError(
                f"steps[{i}] (type={kind!r}) is missing required field(s): "
                f"{', '.join(missing)}"
            )

        button = str(entry.get("button", "") or "")
        if button and button not in _VALID_BUTTONS:
            raise MacNativeSpecError(
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

class MacCalls(Protocol):
    """The minimal set of real-OS operations :class:`NativeRunner` drives a
    native app through — implemented for real by :class:`MacOSCalls`
    (macOS-only), and by a scripted fake in
    ``tests/test_mac_native_driver.py`` so the spec-to-OS *translation*
    logic is testable on any platform.

    ``launch`` returns the PID of the process THIS call started;
    :meth:`kill` must accept only that same PID back — never a bundle
    identifier or executable name — so teardown can never take down a
    process it didn't itself launch (see the module docstring's safety
    note).
    """

    def launch(self, command: str, cwd: str) -> int: ...

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        """Poll for the launched process's real window, returning its
        ``CGWindowID`` once found. Raises :class:`MacNativeRuntimeError` if
        none appears within *timeout_s* — this is the "launch" step's own
        confirmation that the process didn't just start, but actually
        produced a window (#2096)."""
        ...

    def move_window(
        self, pid: int, window_id: int, x: int, y: int, width: int, height: int,
    ) -> None: ...

    def is_window_alive(self, window_id: int) -> bool: ...

    def is_frontmost(self, pid: int) -> tuple[bool, int]:
        """``(True, pid)`` when *pid*'s window is frontmost right now;
        ``(False, actual_frontmost_pid)`` otherwise — checked by
        :class:`NativeRunner` immediately before every ``key``/``click``
        step (#3566). Never raises: a probe failure here must read as "not
        confirmed frontmost" (refuse), never crash the step."""
        ...

    def send_click(self, pid: int, window_id: int, x: int, y: int, button: str) -> None: ...

    def send_key(self, pid: int, key: str) -> None: ...

    def ax_elements(self, pid: int) -> list[dict]:
        """Every element in the app's Accessibility tree right now, each as
        ``{"role": str, "name": str, "visible": bool}``."""
        ...

    def capture(self, window_id: int) -> bytes:
        """A ``screencapture -l <window_id>`` capture of *window_id* right
        now (works even when covered by another window) — raises
        :class:`MacNativeRuntimeError` on failure rather than returning
        empty bytes, since a capture step exists specifically to produce
        evidence and has nothing to report if it can't."""
        ...

    def kill(self, pid: int) -> None: ...

    def session_available(self) -> tuple[bool, str]:
        """``(True, "")`` when an unlocked GUI session is present for this
        driver to launch into; ``(False, reason)`` when the screen is
        locked or no GUI session exists (#3510) — checked by
        :meth:`NativeRunner.run` BEFORE any step (including ``launch``)
        runs, so a locked/absent session is reported as
        ``status="unavailable"`` rather than a failed step. Never raises —
        a probe failure here is itself an "unavailable" verdict, not a
        crash."""
        ...

    def ax_trust_available(self) -> tuple[bool, str]:
        """``(True, "")`` when THIS process identity currently holds
        Accessibility trust (``AXIsProcessTrusted()``); ``(False, reason)``
        when it does not (#3566) — checked by :meth:`NativeRunner.run`
        immediately after :meth:`session_available`, BEFORE any step
        (including ``launch``) runs, so a missing grant is reported as
        ``status="unavailable"`` rather than every subsequent key/click
        step failing (or, worse, a worker improvising an unsafe workaround
        around it). Never raises — a probe failure here is itself an
        "unavailable" verdict, not a crash."""
        ...


def _find_a11y_match(elements: list[dict], role: str, name: str) -> dict | None:
    """The first *elements* entry whose ``role`` matches exactly
    (case-insensitive) and ``name`` matches as a substring
    (case-insensitive), and which is not explicitly marked invisible — or
    ``None`` if nothing matches. An empty *name* matches any name. Identical
    to :func:`coord.win_native_driver._find_a11y_match` — same contract,
    different tree source (AX vs UIA)."""
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
    """Drives one :class:`NativeSpec` against an injected :class:`MacCalls`,
    producing coord's normalized ``{"id", "status", "message"}`` verdict
    list (plus ``capture_b64`` on failing steps when a capture could be
    taken) — the same shape :func:`coord.win_native_driver.run_native_spec`/
    :func:`coord.tui_pty_driver.run_smoke_spec` already produce.
    """

    def __init__(
        self, calls: MacCalls, command: str, cwd: str, *, deadline: float | None = None,
    ) -> None:
        self._calls = calls
        self._command = command
        self._cwd = cwd
        self._deadline = deadline
        self._spec: NativeSpec | None = None
        self._pid: int | None = None
        self._window_id: int | None = None

    def run(self, spec: NativeSpec) -> list[dict]:
        """Run *spec*, first checking :meth:`MacCalls.session_available`
        (#3510) and then :meth:`MacCalls.ax_trust_available` (#3566). A
        locked screen, absent GUI session, or missing Accessibility grant is
        an environment condition, not an app bug: when either is
        unavailable, this returns a single ``status="unavailable"`` entry
        and runs NO step at all (not even ``launch``) — never folding it
        into an ordinary ``"fail"``."""
        self._spec = spec
        available, reason = self._calls.session_available()
        if not available:
            return [{
                "id": "session",
                "status": "unavailable",
                "message": reason or "no unlocked GUI session is available",
            }]
        trusted, trust_reason = self._calls.ax_trust_available()
        if not trusted:
            return [{
                "id": "ax-trust",
                "status": "unavailable",
                "message": trust_reason or "AXIsProcessTrusted() is False",
            }]
        results: list[dict] = []
        try:
            for step in spec.steps:
                if self._deadline is not None and time.monotonic() >= self._deadline:
                    results.append({
                        "id": step.step_id, "status": "fail",
                        "message": "aborted: mac-native driver-level timeout exceeded",
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
        except (MacNativeSpecError, MacNativeRuntimeError, UnsupportedKey, AssertionError) as e:
            entry["status"] = "fail"
            entry["message"] = str(e)
            self._attach_capture_if_possible(entry)
        return entry

    def _attach_capture_if_possible(self, entry: dict) -> None:
        """Best-effort ``screencapture`` evidence attached to *entry* — never
        raises, and never masks the real failure reason in ``message`` if
        the capture itself can't be taken."""
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
            raise MacNativeRuntimeError(
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
        self._calls.move_window(pid, window_id, 0, 0, spec.width, spec.height)
        return None

    def _require_frontmost(self, pid: int) -> None:
        """#3566: refuse any key/click unless *pid* is frontmost RIGHT NOW.
        Raises :class:`MacNativeRuntimeError` (folded by :meth:`_run_step`
        into an ordinary failing step — no retry path exists anywhere in
        this runner, so a focus failure can never blindly retry into
        whatever window happens to be in front) rather than letting
        :meth:`MacCalls.send_click`/:meth:`send_key` fire blind."""
        is_front, front_pid = self._calls.is_frontmost(pid)
        if not is_front:
            raise MacNativeRuntimeError(
                f"refusing to send input — pid={pid} is not frontmost right "
                f"now (actual frontmost pid={front_pid}); a focus failure "
                f"is a step failure, never a blind retry into whatever "
                f"window is in front (#3566)"
            )

    def _do_key(self, step: NativeStep) -> None:
        pid, _ = self._require_window()
        self._require_frontmost(pid)
        self._calls.send_key(pid, step.key)

    def _do_click(self, step: NativeStep) -> None:
        pid, window_id = self._require_window()
        self._require_frontmost(pid)
        self._calls.send_click(pid, window_id, step.x, step.y, step.button or "left")

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
                f"no Accessibility element found with role={step.role!r} "
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
        """#2096: confirmed by re-polling the window list until it actually
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
        docstring's safety note) — never by bundle id/name, and never if
        `launch` never ran (`self._pid` stays `None`)."""
        if self._pid is not None:
            try:
                self._calls.kill(self._pid)
            except Exception:  # noqa: BLE001 — teardown must not mask the real result
                pass


# ── real macOS implementation (macOS-only) ──────────────────────────────────

_NAMED_VKEYS: dict[str, int] = {
    # macOS virtual keycodes (US ANSI layout) — not ASCII-ordered, hence the
    # explicit table rather than a formula (mirrors
    # :data:`coord.win_native_driver._NAMED_VKEYS`'s own "named keys a spec
    # author can reference" convention, just with macOS's own numbering).
    # #3627: `delete` (forward-delete, kVK_ForwardDelete) is a DIFFERENT
    # physical key from `backspace` (kVK_Delete) — they must not share a
    # keycode.
    "enter": 0x24, "esc": 0x35, "tab": 0x30,
    "backspace": 0x33, "delete": 0x75, "space": 0x31,
    "up": 0x7E, "down": 0x7D, "left": 0x7B, "right": 0x7C,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
    "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    # F13-F20: real kVK_* codes (standard on extended Mac keyboards). F21-F24
    # have no standard macOS keycode at all — deliberately absent, so
    # :func:`_encode_mac_chord` raises :class:`UnsupportedKey` for them
    # rather than guessing (#3639: never a silent no-op).
    "f13": 0x69, "f14": 0x6B, "f15": 0x71, "f16": 0x6A, "f17": 0x40,
    "f18": 0x4F, "f19": 0x50, "f20": 0x5A,
}

_VKEY_LETTERS: dict[str, int] = {
    "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03,
    "g": 0x05, "h": 0x04, "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25,
    "m": 0x2E, "n": 0x2D, "o": 0x1F, "p": 0x23, "q": 0x0C, "r": 0x0F,
    "s": 0x01, "t": 0x11, "u": 0x20, "v": 0x09, "w": 0x0D, "x": 0x07,
    "y": 0x10, "z": 0x06,
}

_VKEY_DIGITS: dict[str, int] = {
    "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15,
    "5": 0x17, "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
}

#: macOS virtual keycodes (US ANSI layout) for punctuation. Each physical
#: key produces two characters (unshifted/shifted); both map to the SAME
#: vkey — the bool says whether Shift must be held for the posted event to
#: actually produce *that* character (mirrors the uppercase-letter
#: convention above). Review finding (#3639): without this table,
#: ``cmd+[``/``cmd+]``/``cmd+/`` were posted with virtual keycode 0
#: (``kVK_ANSI_A``) plus the Cmd flag — i.e. silently delivered as Cmd+A to
#: any consumer that reads ``keyCode`` rather than
#: ``charactersIgnoringModifiers``, while still reporting success.
_VKEY_PUNCT: dict[str, tuple[int, bool]] = {
    ";": (0x29, False), ":": (0x29, True),
    "[": (0x21, False), "{": (0x21, True),
    "]": (0x1E, False), "}": (0x1E, True),
    "/": (0x2C, False), "?": (0x2C, True),
    ",": (0x2B, False), "<": (0x2B, True),
    ".": (0x2F, False), ">": (0x2F, True),
    "-": (0x1B, False), "_": (0x1B, True),
    "=": (0x18, False), "+": (0x18, True),
    "'": (0x27, False), '"': (0x27, True),
    "\\": (0x2A, False), "|": (0x2A, True),
    "`": (0x32, False), "~": (0x32, True),
}

#: The digit-row symbols produced with Shift on a US ANSI layout — each
#: maps to the SAME vkey as the underlying digit (:data:`_VKEY_DIGITS`),
#: always with Shift held.
_SHIFTED_DIGIT_SYMBOLS: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
}


@dataclass(frozen=True)
class MacKeyEncoding:
    """One physical key-down/key-up pair for :meth:`MacOSCalls.send_key` to
    post. Exactly one of ``vkey``/``unicode_char`` is not ``None`` —
    punctuation and any other character outside the fixed letter/digit
    virtual-keycode tables goes through ``unicode_char``
    (``CGEventKeyboardSetUnicodeString``), never a vkey guess (#3639). The
    four modifier flags are plain booleans — translating them to
    ``quartz.kCGEventFlagMask*`` constants is :meth:`MacOSCalls.send_key`'s
    own job, so this type (and :func:`_encode_mac_chord` below) stays
    importable/testable without ``pyobjc`` installed."""

    vkey: int | None
    unicode_char: str | None
    shift: bool
    ctrl: bool
    alt: bool
    cmd: bool


def _encode_mac_chord(chord: KeyChord) -> MacKeyEncoding:
    """The :class:`MacKeyEncoding` for one already-parsed :class:`KeyChord`.
    Raises :class:`UnsupportedKey` (naming ``"mac-native"``) for a named key
    with no real macOS keycode (e.g. ``f21``-``f24``, #3639) — never a
    silent no-op."""
    shift = "shift" in chord.modifiers
    ctrl = "ctrl" in chord.modifiers
    alt = "alt" in chord.modifiers
    cmd = "cmd" in chord.modifiers

    if not chord.is_char:
        vkey = _NAMED_VKEYS.get(chord.base)
        if vkey is None:
            raise UnsupportedKey(
                "mac-native", chord.base, "no macOS virtual keycode for this named key"
            )
        return MacKeyEncoding(vkey=vkey, unicode_char=None, shift=shift, ctrl=ctrl, alt=alt, cmd=cmd)

    ch = chord.base
    vkey = _VKEY_LETTERS.get(ch.lower(), _VKEY_DIGITS.get(ch.lower()))
    if vkey is not None:
        # A bare uppercase letter with no explicit `shift` modifier implies
        # Shift was physically held — matches the pre-#3639 convention
        # (`_vkey_for("A") == (vkey, True)`), so `key: M` still behaves like
        # `key: shift+m` rather than silently dropping the capital.
        if ch.isalpha() and ch.isupper():
            shift = True
        return MacKeyEncoding(vkey=vkey, unicode_char=None, shift=shift, ctrl=ctrl, alt=alt, cmd=cmd)

    punct = _VKEY_PUNCT.get(ch)
    if punct is not None:
        vkey, shift_implied = punct
        if shift_implied:
            shift = True
        return MacKeyEncoding(vkey=vkey, unicode_char=None, shift=shift, ctrl=ctrl, alt=alt, cmd=cmd)

    digit = _SHIFTED_DIGIT_SYMBOLS.get(ch)
    if digit is not None:
        return MacKeyEncoding(
            vkey=_VKEY_DIGITS[digit], unicode_char=None, shift=True, ctrl=ctrl, alt=alt, cmd=cmd
        )

    # No real macOS keycode for this character. Posting it with vkey=0
    # (kVK_ANSI_A) alongside a non-Shift modifier would silently deliver
    # e.g. Cmd+A instead of the requested chord — exactly the bug this
    # review finding closes. A bare character with no ctrl/alt/cmd held can
    # still go through the Unicode-string event with vkey=0 and no flags
    # (the standard pyobjc idiom for literal character insertion); anything
    # combined with ctrl/alt/cmd genuinely cannot be delivered this way,
    # since most apps resolve a modified keystroke from (keyCode, flags)
    # via the active keyboard layout, not from the Unicode override.
    if ctrl or alt or cmd:
        raise UnsupportedKey(
            "mac-native", chord.base,
            "no macOS virtual keycode for this character — cannot combine "
            "with ctrl/alt/cmd without silently posting the wrong key",
        )
    return MacKeyEncoding(vkey=None, unicode_char=ch, shift=shift, ctrl=ctrl, alt=alt, cmd=cmd)


def _mac_key_encodings(key: str) -> list[MacKeyEncoding]:
    """Parse *key* — one chord, or a space-separated chord sequence
    (``ctrl+k ctrl+w``) — under the shared grammar
    (:mod:`coord.key_spec`) and encode each chord in order. Raises
    :class:`MacNativeSpecError` for a spec that doesn't parse under the
    grammar at all, :class:`UnsupportedKey` for a chord macOS genuinely
    cannot deliver."""
    try:
        event = parse_key_spec(key)
    except KeySpecError as e:
        raise MacNativeSpecError(f"unrecognized key {key!r}: {e}") from e
    return [_encode_mac_chord(chord) for chord in event.chords]


class MacOSCalls:
    """The real :class:`MacCalls` implementation — ``Quartz`` (``pyobjc``)
    for ``CGEvent`` input injection and window discovery, ``ApplicationServices``
    for the Accessibility tree, and the ``screencapture`` CLI (ships with
    every macOS install, no extra dependency) for window captures.

    macOS-only: raises :class:`MacNativeRuntimeError` at construction on any
    other platform, mirroring
    :class:`coord.win_native_driver.Win32Calls`'s own platform guard.
    """

    def __init__(self) -> None:
        if not _is_macos():
            raise MacNativeRuntimeError(
                "MacOSCalls requires macOS — the mac-native driver only "
                "runs on a real macOS host (e.g. macmini)"
            )
        self._quartz = _import_quartz()
        self._ax = _import_ax()

    # -- process lifecycle --

    def launch(self, command: str, cwd: str) -> int:
        proc = subprocess.Popen(command, shell=True, cwd=cwd or None)
        return proc.pid

    def kill(self, pid: int) -> None:
        # By PID only — see the module docstring's safety note. No
        # bundle-identifier/name-based lookup exists anywhere in this class.
        import signal  # noqa: PLC0415
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    # -- session precheck (#3510) --

    def session_available(self) -> tuple[bool, str]:
        """Real check: ``CGSessionCopyCurrentDictionary`` must return an
        actual session dictionary (``None``/empty means no GUI session is
        logged in at all — headless or SSH-only), its
        ``CGSSessionScreenIsLocked`` key must be false, and its
        ``kCGSessionOnConsoleKey`` must be true (fast-user-switched-away
        sessions are not on the console)."""
        quartz = self._quartz
        session_info = quartz.CGSessionCopyCurrentDictionary()
        if not session_info:
            return False, (
                "CGSessionCopyCurrentDictionary returned no session — no "
                "GUI session is active on this host (headless, SSH-only, "
                "or nobody logged in)"
            )
        if bool(session_info.get("CGSSessionScreenIsLocked", False)):
            return False, "the screen is locked (CGSSessionScreenIsLocked)"
        if not bool(session_info.get("kCGSessionOnConsoleKey", True)):
            return False, (
                "the session is not on the console (fast user switched away)"
            )
        return True, ""

    # -- Accessibility-trust precheck (#3566) --

    def ax_trust_available(self) -> tuple[bool, str]:
        """Real check: ``ApplicationServices.AXIsProcessTrusted()``, asked
        IN-PROCESS. Unlike :mod:`coord.prereqs`'s own ``/health`` probe
        (which deliberately shells out to a fresh ``sys.executable``
        subprocess because IT runs inside the long-lived ``coord agent``
        process, a different identity than a dispatched worker), this call
        already runs inside the worker process that will actually drive
        input — so asking in-process asks exactly the identity that
        matters, with no subprocess indirection needed. The message text
        deliberately matches
        :data:`coord.bugbash._UNAVAILABLE_SIGNATURES`'s
        ``"AXIsProcessTrusted() is False"`` entry."""
        ax = self._ax
        try:
            trusted = bool(ax.AXIsProcessTrusted())
        except Exception as e:  # noqa: BLE001 — a probe failure IS the verdict
            return False, f"AXIsProcessTrusted() probe raised: {e}"
        if not trusted:
            return False, (
                "AXIsProcessTrusted() is False for this process identity — "
                "grant Accessibility to it in System Settings -> Privacy & "
                "Security -> Accessibility, then relaunch the agent (#3566)"
            )
        return True, ""

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        quartz = self._quartz
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            info_list = quartz.CGWindowListCopyWindowInfo(
                quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGNullWindowID,
            )
            for info in info_list or []:
                if info.get("kCGWindowOwnerPID") == pid:
                    return int(info["kCGWindowNumber"])
            time.sleep(0.1)
        raise MacNativeRuntimeError(
            f"no on-screen window appeared for pid={pid} within {timeout_s}s"
        )

    def is_window_alive(self, window_id: int) -> bool:
        quartz = self._quartz
        info_list = quartz.CGWindowListCopyWindowInfo(
            quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGNullWindowID,
        )
        return any(info.get("kCGWindowNumber") == window_id for info in info_list or [])

    def is_frontmost(self, pid: int) -> tuple[bool, int]:
        """#3566: `CGWindowListCopyWindowInfo`'s on-screen-only list is
        already ordered front-to-back — the first entry at the normal
        window layer (``kCGWindowLayer == 0``; menu bar items/status icons
        sit at other layers and would otherwise masquerade as "frontmost")
        is the actual frontmost app window right now."""
        quartz = self._quartz
        info_list = quartz.CGWindowListCopyWindowInfo(
            quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGNullWindowID,
        )
        for info in info_list or []:
            if info.get("kCGWindowLayer", 0) != 0:
                continue
            front_pid = int(info.get("kCGWindowOwnerPID", -1))
            return front_pid == pid, front_pid
        return False, -1

    def move_window(
        self, pid: int, window_id: int, x: int, y: int, width: int, height: int,
    ) -> None:
        ax = self._ax
        app = ax.AXUIElementCreateApplication(pid)
        window = _first_ax_window(ax, app)
        if window is None:
            return
        ax.AXUIElementSetAttributeValue(
            window, ax.kAXPositionAttribute, ax.AXValueCreate(ax.kAXValueCGPointType, (x, y)),
        )
        ax.AXUIElementSetAttributeValue(
            window, ax.kAXSizeAttribute,
            ax.AXValueCreate(ax.kAXValueCGSizeType, (width, height)),
        )

    # -- input injection --

    def send_click(self, pid: int, window_id: int, x: int, y: int, button: str) -> None:
        """#3566: posts via ``CGEventPostToPid`` — addressed directly to
        *pid* — rather than the global ``CGEventPost(kCGHIDEventTap, ...)``
        HID tap every other window on the desktop would also receive."""
        quartz = self._quartz
        info_list = quartz.CGWindowListCopyWindowInfo(
            quartz.kCGWindowListOptionIncludingWindow, window_id,
        )
        bounds = (info_list[0]["kCGWindowBounds"] if info_list else {}) or {}
        screen_x = bounds.get("X", 0) + x
        screen_y = bounds.get("Y", 0) + y

        down_type, up_type, cg_button = {
            "left": (quartz.kCGEventLeftMouseDown, quartz.kCGEventLeftMouseUp,
                     quartz.kCGMouseButtonLeft),
            "right": (quartz.kCGEventRightMouseDown, quartz.kCGEventRightMouseUp,
                      quartz.kCGMouseButtonRight),
            "middle": (quartz.kCGEventOtherMouseDown, quartz.kCGEventOtherMouseUp,
                       quartz.kCGMouseButtonCenter),
        }[button]
        point = quartz.CGPointMake(screen_x, screen_y)
        for event_type in (down_type, up_type):
            event = quartz.CGEventCreateMouseEvent(None, event_type, point, cg_button)
            quartz.CGEventPostToPid(pid, event)

    def send_key(self, pid: int, key: str) -> None:
        """#3566: posts via ``CGEventPostToPid`` — see :meth:`send_click`'s
        own docstring for why, same rationale. #3639: *key* is parsed under
        the shared grammar (:func:`_mac_key_encodings`) and may be a
        space-separated chord sequence (``ctrl+k ctrl+w``) — each chord is
        posted as its own down/up pair, in order."""
        quartz = self._quartz
        for enc in _mac_key_encodings(key):
            vkey = enc.vkey if enc.vkey is not None else 0
            down = quartz.CGEventCreateKeyboardEvent(None, vkey, True)
            up = quartz.CGEventCreateKeyboardEvent(None, vkey, False)
            if enc.unicode_char is not None:
                # The conventional pyobjc idiom passes the string itself
                # (length + the str), not a list of codepoint ints — pyobjc's
                # bridge marshals a `str` to `const UniChar *` for us.
                quartz.CGEventKeyboardSetUnicodeString(down, len(enc.unicode_char), enc.unicode_char)
                quartz.CGEventKeyboardSetUnicodeString(up, len(enc.unicode_char), enc.unicode_char)
            flags = 0
            if enc.shift:
                flags |= quartz.kCGEventFlagMaskShift
            if enc.ctrl:
                flags |= quartz.kCGEventFlagMaskControl
            if enc.alt:
                flags |= quartz.kCGEventFlagMaskAlternate
            if enc.cmd:
                flags |= quartz.kCGEventFlagMaskCommand
            if flags:
                quartz.CGEventSetFlags(down, flags)
                quartz.CGEventSetFlags(up, flags)
            quartz.CGEventPostToPid(pid, down)
            quartz.CGEventPostToPid(pid, up)

    # -- Accessibility --

    def ax_elements(self, pid: int) -> list[dict]:
        ax = self._ax
        app = ax.AXUIElementCreateApplication(pid)
        elements: list[dict] = []

        def _walk(element) -> None:
            try:
                role = _ax_attr(ax, element, ax.kAXRoleAttribute) or ""
                name = (
                    _ax_attr(ax, element, ax.kAXTitleAttribute)
                    or _ax_attr(ax, element, ax.kAXDescriptionAttribute)
                    or _ax_attr(ax, element, ax.kAXValueAttribute)
                    or ""
                )
                hidden = bool(_ax_attr(ax, element, "AXHidden") or False)
                elements.append({"role": str(role), "name": str(name), "visible": not hidden})
            except Exception:  # noqa: BLE001 — a dead/stale element node
                return
            children = _ax_attr(ax, element, ax.kAXChildrenAttribute) or []
            for child in children:
                _walk(child)

        _walk(app)
        return elements

    # -- capture --

    def capture(self, window_id: int) -> bytes:
        import tempfile  # noqa: PLC0415
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                ["screencapture", "-x", "-l", str(window_id), tmp_path],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0 or not os.path.exists(tmp_path):
                raise MacNativeRuntimeError(
                    f"screencapture failed for window_id={window_id}: "
                    f"{proc.stderr.strip() if proc.stderr else '(no stderr)'}"
                )
            with open(tmp_path, "rb") as f:
                data = f.read()
            if not data:
                raise MacNativeRuntimeError(
                    f"screencapture produced an empty file for window_id={window_id}"
                )
            return data
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _first_ax_window(ax, app):
    windows = _ax_attr(ax, app, ax.kAXWindowsAttribute) or []
    return windows[0] if windows else None


def _ax_attr(ax, element, attribute: str):
    error, value = ax.AXUIElementCopyAttributeValue(element, attribute, None)
    if error != 0:  # kAXErrorSuccess == 0
        return None
    return value


def _is_macos() -> bool:
    return os.uname().sysname == "Darwin" if hasattr(os, "uname") else False


def _import_quartz():
    """The optional ``pyobjc-framework-Quartz`` dependency (the
    ``mac-native`` extra) — guarded the same way
    :func:`coord.tui_pty_driver._import_pyte`/
    :func:`coord.win_native_driver._import_uia` guard their own optional
    dependencies, so a missing package names the extra to install rather
    than surfacing a bare ``ModuleNotFoundError``."""
    try:
        import Quartz  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise MacNativeRuntimeError(
            "mac-native needs the 'mac-native' extra, which is not "
            "installed (missing 'pyobjc-framework-Quartz').\n"
            "  Install it with:  pip install 'code-coordinator[mac-native]'"
        ) from exc
    return Quartz


def _import_ax():
    """The optional ``pyobjc-framework-ApplicationServices`` dependency (the
    Accessibility half of the ``mac-native`` extra) — same guard convention
    as :func:`_import_quartz`."""
    try:
        import ApplicationServices  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise MacNativeRuntimeError(
            "mac-native needs the 'mac-native' extra, which is not "
            "installed (missing 'pyobjc-framework-ApplicationServices').\n"
            "  Install it with:  pip install 'code-coordinator[mac-native]'"
        ) from exc
    return ApplicationServices


class MacNativeSession:
    """Persistent mac-native ``coord app-drive`` session (#3590) — mirrors
    :class:`coord.tui_pty_driver.TuiPtySession`'s "sanctioned driver, one
    verb per call" shape: drives the exact same :class:`MacCalls` real-OS
    implementation :class:`NativeRunner`'s own ``launch``/``key``/``click``/
    ``capture`` step handlers use (same frontmost-before-input check, #3566;
    same PID-only ``kill``, never by bundle id), just invoked on demand
    rather than against one fixed step list.

    Callers (:mod:`coord.app_drive`'s daemon dispatch) MUST confirm
    :meth:`MacCalls.session_available`/:meth:`MacCalls.ax_trust_available`
    themselves BEFORE constructing this — exactly like :meth:`NativeRunner
    .run` only reaches its own per-step handlers after that same check;
    this class assumes an already-confirmed-available session.
    """

    def __init__(
        self, launch_command: str, cwd: str, *, width: int = 1024, height: int = 768,
        calls: MacCalls | None = None, timeout_s: float = 10.0,
    ) -> None:
        self._calls: MacCalls = calls if calls is not None else MacOSCalls()
        self._pid = self._calls.launch(launch_command, cwd)
        self._window_id = self._calls.find_top_window(self._pid, timeout_s)
        self._calls.move_window(self._pid, self._window_id, 0, 0, width, height)

    @property
    def pid(self) -> int:
        """The pid :meth:`MacCalls.launch` returned (#3590) — read by
        :mod:`coord.app_drive_daemon` for its ready-file's ``app_pid`` so
        :func:`coord.app_drive.close_session` can re-observe/re-signal the
        process this session itself launched, not just the daemon.

        NOT guaranteed to be the real app's own pid: :meth:`MacOSCalls
        .launch` is a plain ``subprocess.Popen(command, shell=True)``, so
        this is ``/bin/sh``'s pid whenever *command* forks rather than
        execs into the real binary — the same shell-vs-app gap
        :data:`coord.win_native_driver.WinCalls` already documents on its
        own ``launch``/``kill`` (tracked separately from #3590; the
        tui-pty lane's #3583 exec-wrap fix does not apply here)."""
        return self._pid

    def _require_frontmost(self) -> None:
        is_front, front_pid = self._calls.is_frontmost(self._pid)
        if not is_front:
            raise MacNativeRuntimeError(
                f"refusing to send input — pid={self._pid} is not frontmost "
                f"right now (actual frontmost pid={front_pid}); a focus "
                f"failure is reported, never a blind retry (#3566)"
            )

    def send_key(self, key: str) -> None:
        self._require_frontmost()
        self._calls.send_key(self._pid, key)

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        self._require_frontmost()
        self._calls.send_click(self._pid, self._window_id, x, y, button)

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
        docstring's safety note) — mirrors :meth:`NativeRunner._teardown`'s
        own "teardown must not mask the real result" swallow."""
        try:
            self._calls.kill(self._pid)
        except Exception:  # noqa: BLE001 — teardown must not raise
            pass


# ── top-level entry point ───────────────────────────────────────────────────

def run_native_spec(
    spec_text: str, *, launch_command: str, cwd: str,
    calls: MacCalls | None = None, timeout: float | None = None,
) -> list[dict]:
    """Parse *spec_text* and run it against *calls* (a real
    :class:`MacOSCalls` by default) launching *launch_command* in *cwd* —
    the top-level entry point
    :func:`coord.acceptance_drivers._run_mac_native` calls.

    *timeout*, when given, is an overall wall-clock budget in seconds for
    the whole spec — mirrors :func:`coord.win_native_driver.run_native_spec`'s
    own ``timeout``: each step already carries its own bounded per-step
    budget, but their sum can still exceed it, in which case every
    remaining step fails explicitly rather than the run truncating or
    blocking past it.

    Raises :class:`MacNativeSpecError` for a malformed spec. Runtime
    failures (the process never launches, a window never appears, an
    assertion fails) do NOT raise — they're folded into the returned list
    as a ``status="fail"`` entry, the same "partial results, not a crash"
    contract :func:`coord.win_native_driver.run_native_spec` already gives.

    When *calls* reports a locked screen or no GUI session
    (:meth:`MacCalls.session_available`, #3510), the returned list is a
    single ``status="unavailable"`` entry and no step runs — a distinct
    verdict from ``"fail"``, since a locked screen is an environment
    condition, not an app bug.
    """
    spec = parse_native_spec(spec_text)
    resolved_calls = calls if calls is not None else MacOSCalls()
    deadline = time.monotonic() + timeout if timeout is not None else None
    runner = NativeRunner(resolved_calls, launch_command, cwd, deadline=deadline)
    return runner.run(spec)
