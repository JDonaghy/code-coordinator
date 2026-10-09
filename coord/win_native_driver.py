"""``win-native`` acceptance driver — Tier 2's Windows-native tier (#3484).

``tui-pty`` (#3483) proved that an in-process harness can't see real terminal
byte behaviour. This module is the next rung down: on 2026-09-29 vimcode's
Windows window had a real native menu attached (``GetMenu`` -> 7 items) that
was invisible because quadraui#1199's custom caption left 0 px of
non-client area, and the window had no min/max/close — both passed *every*
test at the ``tui-tuidriver``/``tui-pty`` tiers, because neither ever asks
the real OS "is there a menu bar here?" or "what does a mouse click at this
pixel actually hit?". Those are Win32 questions, answerable only by asking
Win32.

This driver launches the driven repo's real compiled GUI/TUI exe, finds its
real top-level window, and probes it with real OS calls:

- ``GetMenu``/``GetMenuItemCount``/``GetMenuString`` — does a native menu
  bar exist, and what's on it.
- ``WM_NCHITTEST`` — what does the OS think is at a given screen point
  (``HTCLOSE``, ``HTCAPTION``, ``HTCLIENT``, ...).
- the UI Automation tree — focusable/named elements by role+name, the same
  way a screen reader (or a real user tabbing through the app) would see
  it.
- ``SendInput``/``PostMessage`` — real input, not an in-process event queue.
- ``PrintWindow`` — captures the window even when it's covered by another
  one, attached as evidence to every *failing* step (see
  :meth:`NativeRunner._attach_capture_if_possible`).

**Injectable OS-call seam.** Every actual Win32/UIA call is one method on
the :class:`WinCalls` protocol, implemented for real by :class:`Win32Calls`
(Windows-only, ``ctypes`` + the optional ``comtypes`` UI Automation client —
the ``win-native`` extra). :class:`NativeRunner` — the spec-step executor —
never calls a Win32 API directly; it only calls through ``WinCalls``. This
is the same seam :mod:`coord.tui_pty_driver` uses for ``PtyChild``: it is
what makes the spec-to-Win32 *translation* logic (parsing, step sequencing,
pass/fail, capture-on-failure) unit-testable on any platform, with a scripted
fake standing in for the OS (see ``tests/test_win_native_driver.py``) — a
real run against a real exe on real Windows hardware (dell64) is out of
reach for this repo's own test suite and is exercised at the operator level,
the same split :mod:`coord.tui_pty_driver`'s own docstring calls out for
ConPTY.

**Safety: kill only the PID this driver itself launched.** :meth:`WinCalls.kill`
takes a ``pid: int`` — the exact process id :meth:`WinCalls.launch`/
:meth:`WinCalls.launch_in_terminal` returned — and nothing in this module
ever looks a process up by its executable's own filename to terminate it.
Windows Terminal and conhost are both things an operator is very likely
also running their *own* session in; a teardown that matched on a shared
host process's filename would be one bad assumption away from killing the
operator's own work, not just this driver's child. See
``tests/test_win_native_driver.py``'s
``test_no_image_name_kill_path_exists_in_the_module`` — a source-level
regression guard, not just a behavioral one.

**Safety: a PID-based kill still is not enough for ``windows-terminal``
(#3663).** Even with the by-PID discipline above, ``launch_in_terminal``'s
``wt.exe {command}`` hands off to an ALREADY-RUNNING ``WindowsTerminal.exe``
via COM instead of spawning a fresh one, whenever one is already open —
every window of every tab/pane across every session on the box lives in
that SAME single process. If this driver's own ``wt.exe`` call is the one
that starts the first ``WindowsTerminal.exe`` instance, that process is a
genuine (if indirect) descendant of the pid `launch_in_terminal` returned —
so a later `kill()` call's own descendant-process walk would, by the
ordinary rules, be entitled to terminate it. :meth:`Win32Calls.
launch_in_terminal` therefore snapshots every ``WindowsTerminal.exe`` pid
already running the instant it is called (before spawning anything) and
:meth:`Win32Calls.kill`/:meth:`Win32Calls.find_top_window` both subtract
that recorded set unconditionally — a pre-existing host is a process this
driver never launched, and must never be torn down (or mistaken for the
window being waited on) no matter what the live process-tree walk returns.

**UNC ``cwd`` (#3543).** A WSL-hosted agent's repo worktree translates
(``coord.win_native_bridge.translate_to_windows_path``) to a UNC path
(``\\wsl.localhost\\Ubuntu-24.04\\...``) — the normal case for dell64, this
fleet's only WSL-hosted ``windows``-capability agent. ``cmd.exe`` (what
``subprocess.Popen(..., shell=True)`` always launches on Windows)
categorically refuses a UNC current directory at its own startup and
silently falls back to ``%windir%`` instead, breaking every relative path
in ``run_command``. :func:`_popen_command_and_cwd` folds cmd.exe's own
``pushd`` UNC-to-drive-letter workaround into the launched command for a
UNC ``cwd`` rather than ever handing cmd.exe one directly; see
:class:`Win32Calls`'s :meth:`~Win32Calls.launch`/
:meth:`~Win32Calls.launch_in_terminal`.

**Local-filesystem staging (#3617).** ``pushd``-into-a-UNC-path (above)
only stops cmd.exe's own startup refusal — it does nothing about the cost
of actually *running* from one. Once launched, Windows reads the exe and
every file it touches (a route's fixture/working files) over ``\\wsl$``'s
9P protocol, which the dell64 operator found "horrible" (2026-10-05) next
to real local NTFS. Whenever ``cwd`` is a UNC path AND the launch command
is a shape this driver can parse with confidence (:func:`_plan_staging`'s
own docstring names exactly which two shapes qualify — a bare leading exe
token, optionally preceded by one ``cd <fixture-dir> && ``, the fleet's
own convention for entering a route's working directory first), both
:meth:`Win32Calls.launch` and :meth:`~Win32Calls.launch_in_terminal` copy
the resolved exe (plus that fixture dir, and/or a conventional ``.smoke``
one, when present) onto the real local Windows filesystem — a fresh
``%LOCALAPPDATA%\\Temp\\coord-app-drive\\<session>\\`` directory — and
launch from there instead, with ``cwd`` rewritten to match. An exe token
that is itself absolute is staged too when it's reachable only over the
network — a UNC path, a drive letter mapped to one, or a `/`-rooted
WSL-style token normalized onto the UNC form (#3633 — see below) — and
left alone only when it's already genuinely local. Anything more complex
(a second shell operator) is left alone, falling back to the ``pushd``
wrap above unchanged — this driver only ever rewrites the ONE leading
executable token of a command it can parse with confidence, never an
opaque shell pipeline.

**#3637: isolating the launched app's own ``%APPDATA%``.** A previously-
documented win-native lane setup step staged a project-local ``.vimcode``-
style fixture next to the opened file, assuming the real app would read
settings from there — it never does; a real Windows app resolves its own
config through ``%APPDATA%\\<app>\\...`` unconditionally, so every lane
following that step was silently reading and writing ONE real, shared,
persistent settings file on the bridge host no matter what it staged.
Whenever the staging above actually engages, :meth:`Win32Calls.launch`/
:meth:`~Win32Calls.launch_in_terminal` now also point the launched
process's own ``%APPDATA%`` (via ``env=``, on the child only — never this
driver's own environment) at that same staged ``.smoke`` directory — the
fleet's existing convention for a route's own "sample/settings working
files" — so a route that wants, say, ``vimcode``'s Nerd Fonts setting off
stages ``.smoke/vimcode/settings.json`` (mirroring the real
``%APPDATA%\\vimcode\\settings.json`` shape, not a project-local
``.vimcode`` one) and it lands exactly where the launched exe actually
looks, isolated and disposed of with the rest of the session
(:meth:`Win32Calls.kill`). Skipped (ambient ``%APPDATA%`` left untouched)
whenever staging itself is — see :meth:`Win32Calls._stage_if_needed`.

**#3633: a route's own relative exe path can disagree with where the
fleet actually built it.** A ``run:`` naming ``../target/<triple>/
release/X.exe`` assumes cargo's default in-tree ``target/``, but this
fleet builds with a shared, per-repo ``CARGO_TARGET_DIR``
(``coord.cargo_cache``, #1402) — so that relative path resolves to
nothing, and a worker substitutes the real, absolute build path instead.
Nothing in this repo normalizes that substituted text — it is whatever
the worker typed — and it does NOT reliably come out as a UNC path: the
issue's own dell64 evidence showed every launch running from
``Z:\\home\\john\\.coord\\cargo-target\\...``, a drive letter MAPPED to
the WSL tree, which ``ntpath.isabs``/:func:`_is_unc_path` alone cannot
tell apart from a genuinely local ``C:\\...`` exe. Launching straight off
either unstaged shape pays the exact same ``\\wsl$`` 9P cost #3617 exists
to avoid — and pre-#3633, :func:`_plan_staging` declined to stage ANY
absolute exe token, so this case silently never staged at all.
:func:`_plan_staging` now stages an absolute exe token whenever
:func:`_is_remote_exe_token` says it's reachable only over the network
(UNC outright, or a drive letter :func:`_get_drive_type` reports as
mapped/remote — real Windows' own ``GetDriveTypeW``, injectable for
testing), after first normalizing a bare `/`-rooted WSL-style token
(``/home/...``) onto the ``\\wsl.localhost\\<distro>\\...`` UNC shape
(:func:`_normalize_posix_exe_token`, borrowing *cwd*'s own distro
prefix). Staged by basename alone, rewriting *command*'s own leading
token to the staged copy; left alone only when the token is already
genuinely local, or is a `/`-rooted one with no distro to borrow from
*cwd* (nothing to safely guess either way).

Whether a token counts as "absolute" at all is decided by
:func:`_is_rooted_or_drive_qualified`, **not** :func:`ntpath.isabs` —
3.13 narrowed the latter's meaning and reports ``False`` for exactly the
``/``-rooted WSL token above, which on that interpreter alone dropped it
into the relative-exe path and staged it to the drive root. See that
function's docstring.

:meth:`Win32Calls.kill` deletes the staged session directory it created,
once it has signalled that launch's own pid — the normal (``close``/
``NativeRunner`` teardown) path. There is no Windows analogue of #3583's
``PR_SET_PDEATHSIG`` for "also delete a directory" on an abnormal (e.g.
SIGKILLed) exit, so :meth:`~Win32Calls.launch`/
:meth:`~Win32Calls.launch_in_terminal` additionally sweep (and delete) any
session directory older than a day on every call — the same "idle
self-expiry is the backstop, the explicit teardown is the normal path"
shape :data:`coord.app_drive.DEFAULT_IDLE_TIMEOUT` already uses, just
applied to a directory instead of a process.

**Process-tree teardown, including on an abnormal exit of THIS process
(#3634).** A 2026-10-05 bugbash lane left 4 simultaneous ``vimcode.exe``
processes running on an operator's desktop: every failed/retried launch's
own process survived, because :meth:`Win32Calls.kill` used to terminate
only the exact pid it was handed — ``cmd.exe``'s pid for a ``shell=True``
launch (#3542), never the real app grandchild cmd.exe itself never owns a
window for — and only walked the whole descendant tree for a *staged*
session's own cleanup, never as ``kill``'s general contract. Two fixes:

1. :meth:`Win32Calls.kill` now ALWAYS terminates *pid*'s whole descendant
   tree (:meth:`Win32Calls._descendant_pids`), not just when there's a
   staged directory to clean up afterward — so every existing caller
   (:meth:`NativeRunner._teardown`, :meth:`WinNativeSession.close`) that
   already called ``kill`` now actually reaches the real app, not just
   its shell wrapper.
2. :class:`WinNativeSession`'s constructor now kills the pid it just
   launched before re-raising if ``find_top_window``/``move_window``
   fails — previously, a launch whose window never appeared left that
   constructor's caller (:mod:`coord.app_drive_daemon`'s ``serve``) with
   no object to ever call ``close``/``kill`` on at all, so a daemon that
   retries ``open`` after a failed window discovery leaked one more
   process per attempt — exactly the bugbash's 4-stray-window evidence.

Neither of those alone covers the case Windows has no POSIX analogue for:
*this* process (the ``win-native`` daemon) itself crashing or being
force-killed before its own ``kill()``/``close()`` ever runs — there is
no cooperating parent left to walk a descendant tree with. For that,
:meth:`Win32Calls.launch`/:meth:`~Win32Calls.launch_in_terminal` assign
every freshly-launched process to a fresh Windows **Job Object** with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` set
(:meth:`Win32Calls._assign_kill_on_close_job`), and keep the one handle to
it open for the life of the session. Windows itself — not this process —
tears down every process still assigned to that job, including a
``cmd.exe /c`` grandchild, the instant its last handle closes; closing it
ourselves is what :meth:`Win32Calls.kill` now does FIRST (before the
``_descendant_pids`` walk, which stays as a backstop for a host/fake where
job-object assignment wasn't available at launch time), and an abnormal
exit of this very process closes it automatically too, since Windows
closes every handle a terminated process held. Best-effort throughout:
job-object creation/assignment never blocks or fails a launch — a host
where it's unavailable just falls back to the weaker (but still
code-path-correct) ``_descendant_pids``-only teardown.

**Spec steps** (:func:`parse_native_spec`, YAML — the ``win-native``
sibling of ``tui-pty``'s smoke spec):

- ``launch`` — start the exe (or, in terminal-hosted mode, the exe inside a
  real terminal host — see below), find its real top-level window, and size
  it deterministically via ``MoveWindow``.
- ``key: <name>`` / ``click: {x, y, button}`` — real input at *screen*
  pixel coordinates relative to the window's client origin, via
  :meth:`WinCalls.send_key`/:meth:`WinCalls.send_click`. ``key:`` is parsed
  under the grammar shared by all four drivers (:mod:`coord.key_spec`,
  #3639): any combination of ``ctrl``/``alt``/``shift``/``cmd``(``win``)
  modifiers, a named key or any single printable character (including
  punctuation, sent via ``KEYEVENTF_UNICODE`` — #3635), and a
  space-separated chord sequence (``ctrl+k ctrl+w``) — see
  :func:`_win_key_encodings`.
- ``wait: {ms}`` — a plain deterministic pause.
- ``capture`` — an explicit ``PrintWindow`` evidence snapshot, attached to
  this step's own result (pass or fail) as ``capture_b64``.
- ``expect_menu: {items, exact}`` — ``GetMenu`` must return a real native
  menu (not ``NULL``) whose item labels contain (or, with ``exact: true``,
  exactly equal) *items* — the vimcode#1199/#1228 regression check.
- ``expect_hit: {x, y, ht}`` — ``WM_NCHITTEST`` at ``(x, y)`` must equal
  *ht* (one of the named ``HT*`` hit-test codes, e.g. ``HTCLOSE``).
- ``expect_a11y: {role, name}`` — the UI Automation tree must contain a
  visible element matching *role* (exact, case-insensitive) and *name*
  (substring, case-insensitive) right now.
- ``expect_a11y_within: {role, name, timeout_ms}`` — the same match, but
  polled repeatedly until it appears or *timeout_ms* elapses.
- ``expect_closed: {timeout_ms}`` — the window must actually stop existing
  (``IsWindow`` re-polled until false) within *timeout_ms*. This is the
  "Close actually closes the window" check (quadraui#1228) — per #2096, a
  click is confirmed closed by *observing the window gone afterward*, never
  by the mere fact that the click was sent without an exception.
- ``type_text: {text}`` (#3650) — types *text* through real OS input (one
  ``SendInput``/``KEYEVENTF_UNICODE`` down/up pair per character — the same
  real-input call ``key:`` already uses for an unmapped character) — never
  by injecting into the app's own in-memory buffer, or a vimcode#1825-class
  "keystrokes went to the launching terminal instead of the app" bug would
  stay invisible to this driver too. Like ``key:``, this calls
  ``SetForegroundWindow`` on the launched hwnd first — so a spec asserting
  ``expect_frontmost`` should order it BEFORE any ``type_text``/``key``
  step, not after: a later ``expect_frontmost`` would then pass because
  this driver forced the window to the front, not because the app did.
- ``expect_file: {path, timeout_ms, contains}`` (#3650) — *path* must exist
  within *timeout_ms* (default 5000), optionally containing the substring
  *contains*. Delegates entirely to :mod:`coord.native_fs_wait` — the one
  shared implementation every Tier-2 native driver calls through (#2096
  "one question, one answer"), since a filesystem check has no Win32
  -specific behaviour to add.
- ``expect_frontmost: {}`` (#3650) — the launched app's window must be
  the real Win32 foreground window right now, compared at the OWNING
  -PID level (``GetForegroundWindow`` -> ``GetWindowThreadProcessId``),
  not raw hwnd identity — a popup/dialog/second top-level window of the
  SAME app in front still counts, matching the mac driver's own pid-level
  semantics. The vimcode#1824 "app never becomes frontmost" class of bug.
- ``expect_region_not_uniform: {x, y, width, height, tolerance}`` (#3650) —
  the named pixel rectangle of a ``PrintWindow`` capture must NOT be a
  single colour (± *tolerance*, default 24, per channel) — vimcode#1676's
  uniform black-bar minimap and #1828's blank panel are exactly this.
  Decodes via :func:`coord.native_pixels.decode_bmp`, judged by the one
  shared :func:`coord.native_pixels.region_not_uniform`.
- ``expect_no_tofu: {x, y, width, height}`` (#3650) — the named pixel
  rectangle must NOT look like a missing-glyph "tofu" placeholder box
  (:func:`coord.native_pixels.looks_like_tofu` — see that function's own
  docstring for the heuristic's scope and documented false-positive risk;
  it is deliberately coarse).

**Terminal-hosted mode** (added 2026-09-30, vimcode#1634-#1636: three bugs
that all pass under a raw ConPTY — ``tui-pty``'s own tier — yet reproduce
for an operator in a real terminal *window*, because the remaining cause
lives in the terminal emulator layer, which only a window-level driver can
see). ``mode: terminal`` plus ``terminal_app: windows-terminal|conhost``
launches the TUI binary inside a real Windows Terminal or legacy conhost
window rather than probing the exe's own top-level window directly, and adds
three more steps:

- ``expect_idle_stable: {ms, interval_ms}`` — ``PrintWindow`` the terminal
  window repeatedly every *interval_ms* (default 100) across a full *ms*
  window (default 5000) and fail if any two consecutive captures differ —
  the vimcode#1634 idle-flicker oracle: a genuinely idle terminal produces
  byte-identical repaints; a ~1 Hz flicker does not.
- ``expect_menu_latency: {x, y, button, role, name, max_ms}`` — real-click
  (default ``button: right``) at ``(x, y)``, then time until a matching UI
  Automation element (default ``role: MenuItem``) appears, failing if it
  never does within *max_ms* — the vimcode#1635 right-click-menu-latency
  oracle.
- ``expect_panel_switch: {x, y, role, name, timeout_ms}`` — real-click at
  ``(x, y)`` (an activity-bar icon), then assert a matching UI Automation
  element (the switched-to panel) appears within *timeout_ms* — the
  vimcode#1636 dead-activity-bar-click oracle.

Both of the latter two perform their own triggering click as part of the
step — the latency/switch being measured starts at that exact ``SendInput``
call, not at some earlier unrelated step, since a separately-timed
``click`` step would leave an unbounded, unmeasured gap between the input
and the start of the timing window.

**Locked/absent session precheck (#3510).** On dell64, vimcode#1629's real-
Windows check could not verify its visual criteria because the session was
locked (``GetForegroundWindow() == NULL``, ``LogonUI`` running in session 1);
vimcode#1558/#1561/#1622 record the same. A locked desktop is an
environment condition, not an app bug, so before :meth:`NativeRunner.run`
launches anything it calls :meth:`WinCalls.session_available` — real
checks: ``OpenInputDesktop`` succeeds, ``WTSGetActiveConsoleSessionId``
reports an active console session, and no ``LogonUI.exe`` is running in
that session. When unavailable, the run returns a single
``status="unavailable"`` result — never a ``"fail"`` — and no step (not
even ``launch``) runs. :func:`coord.bugbash.run_bugbash` skips such a lane
rather than exploring it, and :mod:`coord.release_gate` reports it as
blocking-but-unavailable rather than failed.
"""

from __future__ import annotations

import base64
import logging
import ntpath
import os
import re
import shutil
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import ctypes
import ctypes.wintypes as _wintypes

import yaml

from coord.key_spec import KeyChord, KeySpecError, UnsupportedKey, parse_key_spec
from coord.native_fs_wait import wait_for_file
from coord.native_pixels import NativePixelError, decode_bmp, looks_like_tofu, region_not_uniform

#: `SendInput`'s `INPUT`/`KEYBDINPUT` structs, module-level (#3639 review
#: nit — these used to be rebuilt on every `send_key` call). Defining them
#: needs only `ctypes`/`ctypes.wintypes`, both pure-Python and importable
#: on any platform; only actually CALLING a Win32 API (`ctypes.windll.*`)
#: requires Windows, and that still happens lazily inside
#: :class:`Win32Calls.__init__`.
_ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", _wintypes.WORD), ("wScan", _wintypes.WORD),
        ("dwFlags", _wintypes.DWORD), ("time", _wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", _wintypes.DWORD), ("ki", _KEYBDINPUT),
        ("padding", ctypes.c_ubyte * 8),
    ]


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_LWIN = 0x10, 0x11, 0x12, 0x5B

#: Windows Job Object plumbing for #3634's process-tree teardown — see the
#: module docstring's "Process-tree teardown" paragraph and
#: :meth:`Win32Calls._assign_kill_on_close_job`. Pure ``ctypes`` struct/
#: constant definitions, same as ``_INPUT``/``_KEYBDINPUT`` above: these
#: import fine on any platform; only actually calling a Job Object API
#: through ``self._kernel32`` requires Windows.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", _wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", _wintypes.DWORD),
        ("SchedulingClass", _wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_log = logging.getLogger(__name__)


class WinNativeSpecError(Exception):
    """Raised for a malformed native spec: invalid YAML, a missing/empty
    ``steps:`` list, an unknown step ``type``, a missing required field, an
    unrecognized button/hit-test-code/mode/terminal_app, or a ``mode:
    terminal`` spec missing ``terminal_app:``."""


class WinNativeRuntimeError(Exception):
    """Raised when the native driver itself can't run: not on Windows, a
    missing optional dependency (the ``win-native`` extra), the launched
    process/window never appearing, or a step referencing a window before
    any ``launch`` step ran."""


# ── native spec model ───────────────────────────────────────────────────────

_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "launch": (),
    "key": ("key",),
    "click": ("x", "y"),
    "wait": ("ms",),
    "capture": (),
    "expect_menu": ("items",),
    "expect_hit": ("x", "y", "ht"),
    "expect_a11y": ("role", "name"),
    "expect_a11y_within": ("role", "name"),
    "expect_closed": (),
    "expect_idle_stable": (),
    "expect_menu_latency": ("x", "y"),
    "expect_panel_switch": ("x", "y", "role", "name"),
    # #3650: real OS input (SendInput), never injected into the app's own
    # buffer — see :mod:`coord.native_fs_wait`/:mod:`coord.native_pixels` for
    # why `expect_file`/`expect_region_not_uniform`/`expect_no_tofu` are
    # shared, not win-native-specific, logic.
    "type_text": ("text",),
    "expect_file": ("path",),
    "expect_frontmost": (),
    "expect_region_not_uniform": ("x", "y", "width", "height"),
    "expect_no_tofu": ("x", "y", "width", "height"),
}

_VALID_BUTTONS = ("left", "right", "middle")
_VALID_MODES = ("window", "terminal")
_VALID_TERMINAL_APPS = ("windows-terminal", "conhost")

# Named `WM_NCHITTEST` return codes a spec author can reference by name
# rather than a raw integer — the actual Win32 `HT*` constants, both
# directions of this mapping live in :data:`_HT_CODES` below (shared with
# :class:`Win32Calls`'s real translation).
_HT_CODES: dict[str, int] = {
    "HTERROR": -2, "HTTRANSPARENT": -1, "HTNOWHERE": 0, "HTCLIENT": 1,
    "HTCAPTION": 2, "HTSYSMENU": 3, "HTGROWBOX": 4, "HTSIZE": 4, "HTMENU": 5,
    "HTHSCROLL": 6, "HTVSCROLL": 7, "HTMINBUTTON": 8, "HTMAXBUTTON": 9,
    "HTLEFT": 10, "HTRIGHT": 11, "HTTOP": 12, "HTTOPLEFT": 13,
    "HTTOPRIGHT": 14, "HTBOTTOM": 15, "HTBOTTOMLEFT": 16, "HTBOTTOMRIGHT": 17,
    "HTBORDER": 18, "HTREDUCE": 8, "HTZOOM": 9, "HTSIZEFIRST": 10,
    "HTSIZELAST": 17, "HTOBJECT": 19, "HTCLOSE": 20, "HTHELP": 21,
}
_HT_CODES_BY_VALUE: dict[int, str] = {v: k for k, v in reversed(list(_HT_CODES.items()))}


@dataclass(frozen=True)
class NativeStep:
    """One parsed step of a win-native spec. Mirrors
    :class:`coord.tui_pty_driver.SmokeStep`'s "every unused field keeps its
    default" shape — callers never have to branch on ``kind`` before reading
    one."""

    kind: str
    index: int
    id: str = ""
    key: str = ""
    x: int = 0
    y: int = 0
    button: str = ""
    ms: int = 0
    interval_ms: int = 100
    timeout_ms: int = 5000
    max_ms: int = 2000
    ht: str = ""
    role: str = ""
    name: str = ""
    items: tuple[str, ...] = ()
    exact: bool = False
    text: str = ""
    path: str = ""
    contains: str = ""
    width: int = 0
    height: int = 0
    tolerance: int = 24

    @property
    def step_id(self) -> str:
        return self.id or f"{self.index:03d} {self.kind}"


@dataclass(frozen=True)
class NativeSpec:
    name: str
    width: int
    height: int
    mode: str
    terminal_app: str
    steps: tuple[NativeStep, ...]


def _int_default(value, default: int) -> int:
    """``int(value)``, falling back to *default* only when *value* is
    absent (``None``) — an explicit literal ``0`` in the YAML is honored
    rather than silently treated as "absent" (mirrors
    :func:`coord.tui_pty_driver._int_default`)."""
    return default if value is None else int(value)


def parse_native_spec(yaml_text: str) -> NativeSpec:
    """Parse a win-native spec YAML document into a :class:`NativeSpec`.

    Top level: ``name:`` (optional), ``width:``/``height:`` (optional,
    default 1024x768 — the ``MoveWindow`` target size), ``mode:``
    (``window`` (default) or ``terminal``), ``terminal_app:`` (required
    when ``mode: terminal`` — ``windows-terminal`` or ``conhost``),
    ``steps:`` — a non-empty list of mappings each carrying a ``type:``
    from :data:`_REQUIRED_FIELDS`.

    Raises :class:`WinNativeSpecError` — never returns a partially-parsed
    spec — for: invalid YAML, a non-mapping document, a missing/empty/
    non-list ``steps:``, a step that isn't a mapping, an unknown ``type:``,
    a step missing one of its type's required fields, an unrecognized
    ``button:``/``ht:``/``mode:``/``terminal_app:``, an ``expect_menu``
    with an empty ``items:`` list, or ``mode: terminal`` with no
    ``terminal_app:`` given.
    """
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise WinNativeSpecError(f"native spec is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise WinNativeSpecError("native spec must be a YAML mapping at the top level")

    mode = str(raw.get("mode", "window") or "window")
    if mode not in _VALID_MODES:
        raise WinNativeSpecError(
            f"unrecognized mode {mode!r} — expected one of {', '.join(_VALID_MODES)}"
        )
    terminal_app = str(raw.get("terminal_app", "") or "")
    if mode == "terminal":
        if not terminal_app:
            raise WinNativeSpecError(
                "mode: terminal requires 'terminal_app:' (windows-terminal or conhost)"
            )
        if terminal_app not in _VALID_TERMINAL_APPS:
            raise WinNativeSpecError(
                f"unrecognized terminal_app {terminal_app!r} — expected one "
                f"of {', '.join(_VALID_TERMINAL_APPS)}"
            )

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise WinNativeSpecError("native spec must have a non-empty 'steps:' list")

    steps: list[NativeStep] = []
    for i, entry in enumerate(steps_raw):
        if not isinstance(entry, dict):
            raise WinNativeSpecError(f"steps[{i}] must be a mapping")
        kind = entry.get("type")
        if kind not in _REQUIRED_FIELDS:
            raise WinNativeSpecError(
                f"steps[{i}]: unknown step type {kind!r} — expected one of "
                f"{', '.join(sorted(_REQUIRED_FIELDS))}"
            )
        missing = [f for f in _REQUIRED_FIELDS[kind] if entry.get(f) in (None, "")]
        if missing:
            raise WinNativeSpecError(
                f"steps[{i}] (type={kind!r}) is missing required field(s): "
                f"{', '.join(missing)}"
            )

        button = str(entry.get("button", "") or "")
        if button and button not in _VALID_BUTTONS:
            raise WinNativeSpecError(
                f"steps[{i}]: unrecognized button {button!r} — expected one "
                f"of {', '.join(_VALID_BUTTONS)}"
            )

        ht = str(entry.get("ht", "") or "")
        if kind == "expect_hit" and ht not in _HT_CODES:
            raise WinNativeSpecError(
                f"steps[{i}]: unrecognized hit-test code {ht!r} — expected "
                f"one of {', '.join(sorted(_HT_CODES))}"
            )

        items_raw = entry.get("items")
        if kind == "expect_menu":
            if not isinstance(items_raw, list) or not items_raw:
                raise WinNativeSpecError(
                    f"steps[{i}] (type='expect_menu') must have a non-empty "
                    f"'items:' list"
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
            interval_ms=_int_default(entry.get("interval_ms"), 100),
            timeout_ms=_int_default(entry.get("timeout_ms"), 5000),
            max_ms=_int_default(entry.get("max_ms"), 2000),
            ht=ht,
            role=str(entry.get("role", "") or ""),
            name=str(entry.get("name", "") or ""),
            items=tuple(str(i) for i in items_raw) if isinstance(items_raw, list) else (),
            exact=bool(entry.get("exact", False)),
            text=str(entry.get("text", "") or ""),
            path=str(entry.get("path", "") or ""),
            contains=str(entry.get("contains", "") or ""),
            width=_int_default(entry.get("width"), 0),
            height=_int_default(entry.get("height"), 0),
            tolerance=_int_default(entry.get("tolerance"), 24),
        ))

    return NativeSpec(
        name=str(raw.get("name", "") or ""),
        width=int(raw.get("width", 1024) or 1024),
        height=int(raw.get("height", 768) or 768),
        mode=mode,
        terminal_app=terminal_app,
        steps=tuple(steps),
    )


# ── the injectable OS-call seam ─────────────────────────────────────────────

class WinCalls(Protocol):
    """The minimal set of real-OS operations :class:`NativeRunner` drives a
    native app through — implemented for real by :class:`Win32Calls`
    (Windows-only), and by a scripted fake in
    ``tests/test_win_native_driver.py`` so the spec-to-Win32 *translation*
    logic is testable on any platform.

    ``launch``/``launch_in_terminal`` return the PID of the process THIS
    call started; :meth:`kill` must accept only that same PID back — never
    an image name — so teardown can never take down a process it didn't
    itself launch (see the module docstring's safety note).
    """

    def launch(self, command: str, cwd: str) -> int: ...

    def launch_in_terminal(self, command: str, cwd: str, terminal_app: str) -> int: ...

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        """Poll for *pid*'s (or one of its descendant processes') real
        top-level window, returning its handle once found. Raises
        :class:`WinNativeRuntimeError` if none appears within *timeout_s* —
        this is the "launch" step's own confirmation that the process
        didn't just start, but actually produced a window (#2096).

        *pid* is not always the real app's own pid: ``subprocess.Popen(...,
        shell=True)`` on Windows spawns ``cmd.exe`` as the immediate child
        and returns *its* pid, while the real app (e.g. ``vimcode.exe``) is
        a grandchild with a different pid that ``cmd.exe`` itself never
        owns a window for (#3542). Implementations must search *pid*'s
        whole descendant-process tree, not just *pid* itself."""
        ...

    def move_window(self, hwnd: int, x: int, y: int, width: int, height: int) -> None: ...

    def is_window_alive(self, hwnd: int) -> bool: ...

    def get_menu_items(self, hwnd: int) -> list[str] | None:
        """``GetMenu``'s item labels, or ``None`` when the window has no
        native menu attached at all (a ``NULL`` HMENU)."""
        ...

    def hit_test(self, hwnd: int, x: int, y: int) -> str:
        """The named ``HT*`` result of ``WM_NCHITTEST`` at screen point
        ``(x, y)``."""
        ...

    def send_click(self, hwnd: int, x: int, y: int, button: str) -> None: ...

    def send_key(self, hwnd: int, key: str) -> None: ...

    def type_text(self, hwnd: int, text: str) -> None:
        """Type *text* through real OS input (``SendInput`` with
        ``KEYEVENTF_UNICODE``, one down/up pair per character) — never by
        injecting into the app's own in-memory input buffer, or a
        vimcode#1825-class "keystrokes went to the launching terminal
        instead of the app" bug would stay invisible to this driver too
        (#3650)."""
        ...

    def is_frontmost(self, hwnd: int) -> tuple[bool, int]:
        """``(True, actual_foreground_hwnd)`` when the process OWNING
        *hwnd* also owns the real foreground window right now
        (``GetForegroundWindow`` -> ``GetWindowThreadProcessId``, compared
        against *hwnd*'s own owning pid — not hwnd identity, so a
        popup/dialog/second top-level window of the same app still
        counts); ``(False, actual_foreground_hwnd)`` otherwise — the
        vimcode#1824 "app never becomes frontmost" class of bug (#3650).
        Never raises — a probe failure here reads as "not confirmed
        frontmost", never a crash."""
        ...

    def uia_elements(self, hwnd: int) -> list[dict]:
        """Every element in the window's UI Automation tree right now, each
        as ``{"role": str, "name": str, "visible": bool}``."""
        ...

    def capture(self, hwnd: int) -> bytes:
        """A ``PrintWindow`` capture of *hwnd* right now (works even when
        covered by another window) — raises :class:`WinNativeRuntimeError`
        on failure rather than returning empty bytes, since a capture step
        exists specifically to produce evidence and has nothing to report
        if it can't."""
        ...

    def kill(self, pid: int) -> None: ...

    def session_available(self) -> tuple[bool, str]:
        """``(True, "")`` when an unlocked interactive Windows session is
        present for this driver to launch into; ``(False, reason)`` when it
        is locked or absent (#3510) — checked by :meth:`NativeRunner.run`
        BEFORE any step (including ``launch``) runs, so a locked/absent
        session is reported as ``status="unavailable"`` rather than a failed
        step. Never raises — a probe failure here is itself an
        "unavailable" verdict, not a crash."""
        ...


def _find_a11y_match(elements: list[dict], role: str, name: str) -> dict | None:
    """The first *elements* entry whose ``role`` matches exactly
    (case-insensitive) and ``name`` matches as a substring
    (case-insensitive), and which is not explicitly marked invisible — or
    ``None`` if nothing matches. An empty *name* matches any name."""
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
    """Drives one :class:`NativeSpec` against an injected :class:`WinCalls`,
    producing coord's normalized ``{"id", "status", "message"}`` verdict
    list (plus ``capture_b64`` on failing steps when a capture could be
    taken) — the same shape :func:`coord.tui_pty_driver.run_smoke_spec`
    already produces.
    """

    def __init__(
        self, calls: WinCalls, command: str, cwd: str, *, deadline: float | None = None,
    ) -> None:
        self._calls = calls
        self._command = command
        self._cwd = cwd
        self._deadline = deadline
        self._spec: NativeSpec | None = None
        self._pid: int | None = None
        self._hwnd: int | None = None
        # Set by a handler that already has the exact failing capture in
        # hand (e.g. `expect_idle_stable`'s differing frame) so
        # `_attach_capture_if_possible` doesn't take a second, less
        # representative capture after the fact.
        self._pending_failure_capture: bytes | None = None

    def run(self, spec: NativeSpec) -> list[dict]:
        """Run *spec*, first checking :meth:`WinCalls.session_available`
        (#3510). A locked or absent interactive session is an environment
        condition, not an app bug: when unavailable, this returns a single
        ``status="unavailable"`` entry and runs NO step at all (not even
        ``launch``) — never folding it into an ordinary ``"fail"``."""
        self._spec = spec
        available, reason = self._calls.session_available()
        if not available:
            return [{
                "id": "session",
                "status": "unavailable",
                "message": reason or "no interactive Windows session is available",
            }]
        results: list[dict] = []
        try:
            for step in spec.steps:
                if self._deadline is not None and time.monotonic() >= self._deadline:
                    results.append({
                        "id": step.step_id, "status": "fail",
                        "message": "aborted: win-native driver-level timeout exceeded",
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
            "expect_menu": self._do_expect_menu,
            "expect_hit": self._do_expect_hit,
            "expect_a11y": self._do_expect_a11y,
            "expect_a11y_within": self._do_expect_a11y_within,
            "expect_closed": self._do_expect_closed,
            "expect_idle_stable": self._do_expect_idle_stable,
            "expect_menu_latency": self._do_expect_menu_latency,
            "expect_panel_switch": self._do_expect_panel_switch,
            "type_text": self._do_type_text,
            "expect_file": self._do_expect_file,
            "expect_frontmost": self._do_expect_frontmost,
            "expect_region_not_uniform": self._do_expect_region_not_uniform,
            "expect_no_tofu": self._do_expect_no_tofu,
        }
        entry: dict = {"id": step.step_id, "status": "pass", "message": ""}
        try:
            extra = handlers[step.kind](step)
            if extra:
                entry.update(extra)
        except (
            WinNativeSpecError, WinNativeRuntimeError, UnsupportedKey,
            AssertionError, NativePixelError,
        ) as e:
            entry["status"] = "fail"
            entry["message"] = str(e)
            self._attach_capture_if_possible(entry)
        return entry

    def _attach_capture_if_possible(self, entry: dict) -> None:
        """Best-effort ``PrintWindow`` evidence attached to *entry* — never
        raises, and never masks the real failure reason in ``message`` if
        the capture itself can't be taken."""
        image = self._pending_failure_capture
        self._pending_failure_capture = None
        if image is None and self._hwnd is not None:
            try:
                image = self._calls.capture(self._hwnd)
            except Exception as e:  # noqa: BLE001 — evidence is best-effort
                entry["capture_error"] = str(e)
                return
        if image:
            entry["capture_b64"] = base64.b64encode(image).decode("ascii")

    def _require_hwnd(self) -> int:
        if self._hwnd is None:
            raise WinNativeRuntimeError(
                "no window — spec has no 'launch' step before this one"
            )
        return self._hwnd

    # -- action steps --

    def _do_launch(self, step: NativeStep) -> dict | None:
        spec = self._spec
        assert spec is not None
        if spec.mode == "terminal":
            pid = self._calls.launch_in_terminal(self._command, self._cwd, spec.terminal_app)
        else:
            pid = self._calls.launch(self._command, self._cwd)
        self._pid = pid
        timeout_s = (step.timeout_ms or 10000) / 1000
        hwnd = self._calls.find_top_window(pid, timeout_s)
        self._hwnd = hwnd
        self._calls.move_window(hwnd, 0, 0, spec.width, spec.height)
        return None

    def _do_key(self, step: NativeStep) -> None:
        self._calls.send_key(self._require_hwnd(), step.key)

    def _do_click(self, step: NativeStep) -> None:
        self._calls.send_click(self._require_hwnd(), step.x, step.y, step.button or "left")

    def _do_type_text(self, step: NativeStep) -> None:
        """#3650: real OS input (``SendInput``/``KEYEVENTF_UNICODE``) —
        never injected into the app's own buffer."""
        self._calls.type_text(self._require_hwnd(), step.text)

    def _do_wait(self, step: NativeStep) -> None:
        time.sleep(step.ms / 1000)

    def _do_capture(self, step: NativeStep) -> dict:
        image = self._calls.capture(self._require_hwnd())
        return {"capture_b64": base64.b64encode(image).decode("ascii")}

    # -- assertion steps --

    def _do_expect_menu(self, step: NativeStep) -> None:
        items = self._calls.get_menu_items(self._require_hwnd())
        if items is None:
            raise AssertionError(
                "GetMenu returned no native menu (NULL) — window has no "
                "menu bar attached"
            )
        if step.exact:
            if list(items) != list(step.items):
                raise AssertionError(
                    f"expected menu items {list(step.items)!r} exactly, got {items!r}"
                )
        else:
            missing = [i for i in step.items if i not in items]
            if missing:
                raise AssertionError(
                    f"expected menu to contain {missing!r}; actual menu "
                    f"items: {items!r}"
                )

    def _do_expect_hit(self, step: NativeStep) -> None:
        actual = self._calls.hit_test(self._require_hwnd(), step.x, step.y)
        if actual != step.ht:
            raise AssertionError(
                f"WM_NCHITTEST at ({step.x},{step.y}) expected {step.ht}, got {actual}"
            )

    def _do_expect_a11y(self, step: NativeStep) -> None:
        elements = self._calls.uia_elements(self._require_hwnd())
        if _find_a11y_match(elements, step.role, step.name) is None:
            raise AssertionError(
                f"no UI Automation element found with role={step.role!r} "
                f"name={step.name!r}; tree had: {_summarize_elements(elements)}"
            )

    def _do_expect_a11y_within(self, step: NativeStep) -> dict:
        hwnd = self._require_hwnd()
        start = time.monotonic()
        deadline = start + step.timeout_ms / 1000
        while True:
            elements = self._calls.uia_elements(hwnd)
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
        """#2096: confirmed by re-polling `IsWindow` until it actually
        reports gone — never by the mere absence of an exception from an
        earlier click step."""
        hwnd = self._require_hwnd()
        deadline = time.monotonic() + (step.timeout_ms or 5000) / 1000
        while True:
            if not self._calls.is_window_alive(hwnd):
                return
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"window still alive {step.timeout_ms}ms after "
                    f"expect_closed — it did not actually close"
                )
            time.sleep(0.02)

    def _do_expect_idle_stable(self, step: NativeStep) -> None:
        """vimcode#1634's idle-flicker oracle: repeated ``PrintWindow``
        captures across a real-time window must be byte-identical. Only
        answerable by actually taking the captures and comparing them
        afterward (#2096) — nothing here can raise mid-wait, so the mere
        absence of an exception during the loop proves nothing on its own;
        the comparison after each new capture is the actual check."""
        hwnd = self._require_hwnd()
        interval_s = (step.interval_ms or 100) / 1000
        deadline = time.monotonic() + (step.ms or 5000) / 1000
        prev = self._calls.capture(hwnd)
        elapsed_captures = 1
        while time.monotonic() < deadline:
            time.sleep(interval_s)
            current = self._calls.capture(hwnd)
            elapsed_captures += 1
            if current != prev:
                self._pending_failure_capture = current
                raise AssertionError(
                    f"capture #{elapsed_captures} differs from the previous "
                    f"one taken {step.interval_ms}ms earlier while the app "
                    f"should have been idle (flicker)"
                )
            prev = current

    def _do_expect_menu_latency(self, step: NativeStep) -> dict:
        """vimcode#1635's right-click-menu-latency oracle: the timing
        window starts at THIS step's own click, not an earlier one, so
        there's no unmeasured gap between the real input and the start of
        the clock."""
        hwnd = self._require_hwnd()
        role = step.role or "MenuItem"
        start = time.monotonic()
        self._calls.send_click(hwnd, step.x, step.y, step.button or "right")
        deadline = start + (step.max_ms or 2000) / 1000
        while True:
            elements = self._calls.uia_elements(hwnd)
            if _find_a11y_match(elements, role, step.name) is not None:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return {"message": f"menu appeared {elapsed_ms}ms after right-click"}
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"no menu (role={role!r}) appeared within {step.max_ms}ms "
                    f"of clicking ({step.x},{step.y})"
                )
            time.sleep(0.01)

    def _do_expect_panel_switch(self, step: NativeStep) -> dict:
        """vimcode#1636's dead-activity-bar-click oracle — same
        click-starts-the-clock reasoning as :meth:`_do_expect_menu_latency`."""
        hwnd = self._require_hwnd()
        start = time.monotonic()
        self._calls.send_click(hwnd, step.x, step.y, step.button or "left")
        deadline = start + (step.timeout_ms or 5000) / 1000
        while True:
            elements = self._calls.uia_elements(hwnd)
            if _find_a11y_match(elements, step.role, step.name) is not None:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return {"message": f"panel appeared {elapsed_ms}ms after activity-bar click"}
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"activity-bar click at ({step.x},{step.y}) never "
                    f"switched to role={step.role!r} name={step.name!r} "
                    f"within {step.timeout_ms}ms"
                )
            time.sleep(0.02)

    def _do_expect_file(self, step: NativeStep) -> None:
        """#3650: delegates to the one shared filesystem check — see
        :mod:`coord.native_fs_wait`'s own docstring (#2096 "one question,
        one answer").

        Review finding: ``getattr``-based, same reasoning as
        :attr:`WinNativeSession.staging_warning` — only a real
        :class:`Win32Calls` actually sets ``launched_appdata_dir`` on
        itself (and only non-``None`` when launch staging isolated
        ``%APPDATA%``, #3637); a scripted test fake simply has none, and
        this step's own ``$env:APPDATA`` resolution then falls back to
        the ambient environment exactly as it did before this fix."""
        appdata_dir = getattr(self._calls, "launched_appdata_dir", None)
        env_overrides = {"APPDATA": appdata_dir} if appdata_dir is not None else None
        ok, reason = wait_for_file(
            step.path, step.timeout_ms, step.contains or None, env_overrides=env_overrides,
        )
        if not ok:
            raise AssertionError(reason)

    def _do_expect_frontmost(self, step: NativeStep) -> None:
        """#3650: the vimcode#1824 "app never becomes frontmost" class of
        bug — confirmed by actually asking :meth:`WinCalls.is_frontmost`
        right now, never by the mere absence of an exception from an
        earlier ``launch`` step (#2096)."""
        hwnd = self._require_hwnd()
        is_front, actual_hwnd = self._calls.is_frontmost(hwnd)
        if not is_front:
            raise AssertionError(
                f"hwnd={hwnd} is not the foreground window right now "
                f"(GetForegroundWindow reports hwnd={actual_hwnd})"
            )

    def _do_expect_region_not_uniform(self, step: NativeStep) -> dict:
        """#3650: decodes this driver's own ``PrintWindow`` BMP, then judges
        the region through the ONE shared implementation
        (:func:`coord.native_pixels.region_not_uniform`) — see that
        module's docstring for why the judgment itself is not
        win-native-specific logic."""
        hwnd = self._require_hwnd()
        image = decode_bmp(self._calls.capture(hwnd))
        is_not_uniform, message = region_not_uniform(
            image, step.x, step.y, step.width, step.height, tolerance=step.tolerance,
        )
        if not is_not_uniform:
            raise AssertionError(message)
        return {"message": message}

    def _do_expect_no_tofu(self, step: NativeStep) -> dict:
        """#3650: see :func:`coord.native_pixels.looks_like_tofu`'s own
        docstring for the heuristic's scope and documented false-negative
        risk."""
        hwnd = self._require_hwnd()
        image = decode_bmp(self._calls.capture(hwnd))
        is_tofu, message = looks_like_tofu(image, step.x, step.y, step.width, step.height)
        if is_tofu:
            raise AssertionError(message)
        return {"message": message}

    # -- teardown --

    def _teardown(self) -> None:
        """Kills only the PID this run itself launched (see the module
        docstring's safety note) — never by image name, and never if
        `launch` never ran (`self._pid` stays `None`)."""
        if self._pid is not None:
            try:
                self._calls.kill(self._pid)
            except Exception:  # noqa: BLE001 — teardown must not mask the real result
                pass


# ── real Win32 implementation (Windows-only) ────────────────────────────────

_NAMED_VKEYS: dict[str, int] = {
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
    "backspace": 0x08, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "delete": 0x2E, "insert": 0x2D,
    # VK_F1 == 0x70, and VK_F2..VK_F24 continue contiguously from there —
    # the one native table of the four where a formula (rather than an
    # explicit per-key literal) is actually correct.
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}

#: ``VK_OEM_*`` codes for US-layout punctuation keys. Each physical key
#: produces two characters (unshifted/shifted); both map to the SAME vk —
#: the bool says whether Shift must be held for the posted event to
#: actually produce *that* character. Review finding (#3639): without this
#: table, ``ctrl+/`` (and any other modifier+punctuation chord) went
#: through ``KEYEVENTF_UNICODE`` with ``wVk=0`` while Ctrl/Alt/Win was
#: held — Windows does not raise a WM_*KEYDOWN-driven accelerator from
#: that combination, so the chord silently did nothing while still being
#: reported as a pass.
_VKEY_OEM_PUNCT: dict[str, tuple[int, bool]] = {
    ";": (0xBA, False), ":": (0xBA, True),
    "=": (0xBB, False), "+": (0xBB, True),
    ",": (0xBC, False), "<": (0xBC, True),
    "-": (0xBD, False), "_": (0xBD, True),
    ".": (0xBE, False), ">": (0xBE, True),
    "/": (0xBF, False), "?": (0xBF, True),
    "`": (0xC0, False), "~": (0xC0, True),
    "[": (0xDB, False), "{": (0xDB, True),
    "\\": (0xDC, False), "|": (0xDC, True),
    "]": (0xDD, False), "}": (0xDD, True),
    "'": (0xDE, False), '"': (0xDE, True),
}

#: The digit-row symbols produced with Shift on a US layout — each maps to
#: the SAME vk as the underlying digit (ASCII ``'0'``-``'9'``), always with
#: Shift held.
_SHIFTED_DIGIT_SYMBOLS: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
}


@dataclass(frozen=True)
class WinKeyEncoding:
    """One physical key-down/key-up pair for :meth:`Win32Calls.send_key` to
    post via ``SendInput``. Exactly one of ``vk``/``unicode_char`` is not
    ``None`` — punctuation and any other character outside the ASCII
    letter/digit range goes through ``unicode_char``
    (``KEYEVENTF_UNICODE``), never a vk guess and never a silent no-op
    (closes #3635). ``win`` is the Cmd/Super/Meta modifier — Windows calls
    its own version of that key ``VK_LWIN``."""

    vk: int | None
    unicode_char: str | None
    shift: bool
    ctrl: bool
    alt: bool
    win: bool


def _encode_win_chord(chord: KeyChord) -> WinKeyEncoding:
    """The :class:`WinKeyEncoding` for one already-parsed :class:`KeyChord`.
    Raises :class:`UnsupportedKey` (naming ``"win-native"``) for a named key
    with no ``VK_*`` code at all — never a silent no-op (#3639)."""
    shift = "shift" in chord.modifiers
    ctrl = "ctrl" in chord.modifiers
    alt = "alt" in chord.modifiers
    win = "cmd" in chord.modifiers

    if not chord.is_char:
        vk = _NAMED_VKEYS.get(chord.base)
        if vk is None:
            raise UnsupportedKey("win-native", chord.base, "no VK_* code for this named key")
        return WinKeyEncoding(vk=vk, unicode_char=None, shift=shift, ctrl=ctrl, alt=alt, win=win)

    ch = chord.base
    if len(ch) == 1 and ch.isascii() and ch.isalnum():
        # Windows VK codes for '0'-'9'/'A'-'Z' equal their own ASCII code
        # points — a bare uppercase letter with no explicit `shift` implies
        # Shift was physically held (pre-#3639 convention preserved).
        if ch.isalpha() and ch.isupper():
            shift = True
        return WinKeyEncoding(vk=ord(ch.upper()), unicode_char=None, shift=shift, ctrl=ctrl, alt=alt, win=win)

    punct = _VKEY_OEM_PUNCT.get(ch)
    if punct is not None:
        vk, shift_implied = punct
        if shift_implied:
            shift = True
        return WinKeyEncoding(vk=vk, unicode_char=None, shift=shift, ctrl=ctrl, alt=alt, win=win)

    digit = _SHIFTED_DIGIT_SYMBOLS.get(ch)
    if digit is not None:
        return WinKeyEncoding(
            vk=ord(digit), unicode_char=None, shift=True, ctrl=ctrl, alt=alt, win=win
        )

    # No VK_* code for this character. `KEYEVENTF_UNICODE` with a held
    # Ctrl/Alt/Win does not generate a Windows accelerator — the chord
    # would be posted but do nothing while still reporting a pass (#3639,
    # the win-native shape of the same bug the punctuation table above
    # fixes for the mapped characters). A bare character with no
    # ctrl/alt/win can still go through `KEYEVENTF_UNICODE` for literal
    # insertion.
    if ctrl or alt or win:
        raise UnsupportedKey(
            "win-native", chord.base,
            "no VK_* code for this character — KEYEVENTF_UNICODE does not "
            "produce an accelerator combined with ctrl/alt/win",
        )
    return WinKeyEncoding(vk=None, unicode_char=ch, shift=shift, ctrl=ctrl, alt=alt, win=win)


def _win_key_encodings(key: str) -> list[WinKeyEncoding]:
    """Parse *key* — one chord, or a space-separated chord sequence
    (``ctrl+k ctrl+w``) — under the shared grammar
    (:mod:`coord.key_spec`) and encode each chord in order. Raises
    :class:`WinNativeSpecError` for a spec that doesn't parse under the
    grammar at all, :class:`UnsupportedKey` for a chord Windows genuinely
    cannot deliver."""
    try:
        event = parse_key_spec(key)
    except KeySpecError as e:
        raise WinNativeSpecError(f"unrecognized key {key!r}: {e}") from e
    return [_encode_win_chord(chord) for chord in event.chords]


def _is_unc_path(path: str) -> bool:
    """True for a UNC path (``\\\\server\\share\\...``) — the shape
    :func:`coord.win_native_bridge.translate_to_windows_path` returns for a
    WSL-hosted repo (``\\\\wsl.localhost\\<distro>\\...``, #3543)."""
    return path.startswith("\\\\")


#: Win32 ``GetDriveTypeW``'s own ``DRIVE_REMOTE`` constant — a drive letter
#: mapped to a network share (``net use Z: \\wsl$\Ubuntu...``), the exact
#: shape #3633's own dell64 evidence showed (every staged-looking exe still
#: running from ``Z:\home\john\.coord\cargo-target\...``): ``ntpath.isabs``
#: reports ``True`` for it and :func:`_is_unc_path` reports ``False``, so
#: neither of #3617's original checks ever caught it — it was mislabelled
#: "already genuinely local" and the 9P cost staging exists to avoid was
#: paid anyway.
_DRIVE_REMOTE = 4


def _is_rooted_or_drive_qualified(path: str) -> bool:
    r"""True when *path* is anything other than a plain, *cwd*-relative
    path — i.e. it carries a root (``\foo``, ``/foo``), a UNC prefix
    (``\\server\share\...``, ``//server/share/...``) or a drive letter
    (``C:\foo``, and even the drive-relative ``C:foo``).

    **This exists instead of :func:`ntpath.isabs` because `ntpath.isabs`
    is not stable across the Python versions this repo supports (#3633
    CI).** Python 3.13 rewrote it to mean "absolute" strictly — only a
    drive-plus-root (``C:\``) or a UNC prefix — so a ``/``-rooted token
    like ``/home/john/.coord/cargo-target/.../vimcode.exe``, the exact
    WSL-style shape #3633 exists to normalize and stage, reports
    ``True`` on 3.12 and ``False`` on 3.13.

    That difference is not cosmetic: on 3.13 such a token fell past
    :func:`_plan_staging`'s absolute-exe branch into the *relative*-exe
    one, where ``ntpath.join(session_root, "\\home\\me\\...")`` discards
    ``session_root`` wholesale (joining a rooted path replaces the
    root) and yields ``dest_exe = C:\home\me\...`` — staging the exe to
    the *drive root*, outside this session's own directory, which is
    precisely what the ``..``-escape guard further down exists to
    prevent. So the relative branch must only ever see genuinely
    relative tokens, on every supported interpreter; deciding that from
    the string itself rather than from ``ntpath``'s version-dependent
    notion of absoluteness is what makes that true.

    A drive-relative ``C:foo`` (no root — ``ntpath.isabs`` is ``False``
    for it on *both* versions) is deliberately included: it is not
    *cwd*-relative either, ``ntpath.join`` treats it specially too, and
    the absolute branch's own classification safely declines to stage
    anything it cannot prove is remote.
    """
    if not path:
        return False
    if path[0] in ("\\", "/"):
        return True
    return bool(ntpath.splitdrive(path)[0])


def _get_drive_type(path: str) -> int:
    """``GetDriveTypeW`` for the drive letter *path* starts with — a real
    (cheap, read-only) Win32 call, injectable via :func:`_is_remote_exe_token`'s
    ``get_drive_type`` kwarg so the decision logic built on top of it stays
    pure/testable with fabricated path strings on any host OS, the same
    shape :func:`_local_app_data_via_known_folder`'s caller
    (:meth:`Win32Calls._staging_root`) already uses its own
    ``known_folder_resolver`` kwarg for. Returns ``0`` (``DRIVE_UNKNOWN`` —
    never ``DRIVE_REMOTE``, so callers never mistake "couldn't ask" for "is
    local") on any non-Windows platform, a path with no drive letter at
    all (a UNC path, or one with no root), or any failure. The real call
    only ever happens on a real Windows host, where ``os.path`` already IS
    ``ntpath``.
    """
    if os.name != "nt":
        return 0
    drive = ntpath.splitdrive(path)[0]
    if not drive:
        return 0
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
    except Exception:  # noqa: BLE001 — best-effort only, never worth crashing a launch over
        return 0


#: The ``\\wsl.localhost\<distro>`` prefix :func:`coord.win_native_bridge
#: .translate_to_windows_path` puts on a translated ``cwd`` (#3543) —
#: reused by :func:`_normalize_posix_exe_token` to turn a bare `/`-rooted
#: exe token into that same UNC shape, without ever having to guess a
#: distro name from nowhere.
_WSL_LOCALHOST_PREFIX_RE = re.compile(r"^\\\\wsl\.localhost\\[^\\]+", re.IGNORECASE)


def _normalize_posix_exe_token(exe_token: str, cwd: str) -> str | None:
    """A `/`-rooted POSIX/WSL-style exe token (``/home/john/...``) that
    reached the Windows side unmapped — #3633's fix option (b) names this
    shape explicitly alongside the UNC one. It is never a real Windows
    path: Windows path APIs resolve a bare leading ``/`` against the
    *current drive's* root directory, not WSL's filesystem, so launching
    (or even just ``isfile``-checking) it unchanged would silently fail
    or — worse — resolve to an unrelated file on whatever drive happens
    to be current.

    Rewrites it onto the ``\\wsl.localhost\\<distro>\\...`` UNC shape
    instead, borrowing *cwd*'s own ``\\wsl.localhost\\<distro>`` prefix —
    *cwd* is already known to be exactly that shape whenever this is
    reached (the :func:`_is_unc_path` gate at the top of
    :func:`_plan_staging`) — rather than fabricating a distro name from
    nothing. Returns ``None`` when *cwd* doesn't carry that prefix (a
    same-host, non-WSL UNC share, e.g. ``\\fileserver\\tools\\...``, has
    no WSL distro to borrow one from); the caller then leaves the token
    alone rather than guess.
    """
    match = _WSL_LOCALHOST_PREFIX_RE.match(cwd)
    if match is None:
        return None
    return match.group(0) + exe_token.replace("/", "\\")


def _is_remote_exe_token(exe_token: str, *, get_drive_type=_get_drive_type) -> bool:
    """True when *exe_token* — already known to be an absolute path,
    and already run through :func:`_normalize_posix_exe_token` when it
    started life as a `/`-rooted one — is reachable only over the network,
    i.e. genuinely worth staging onto the local filesystem (#3633):

    - a UNC path (:func:`_is_unc_path` — ``\\\\server\\share\\...`` /
      ``\\\\wsl.localhost\\...``), or
    - a drive-letter path whose drive is itself a mapped network drive
      (*get_drive_type* reports :data:`_DRIVE_REMOTE` — dell64's own
      observed shape, ``Z:\\home\\...``).

    False for a drive-letter path on a genuinely local (fixed/removable)
    drive — nothing to stage for an exe already on local NTFS."""
    if _is_unc_path(exe_token):
        return True
    return get_drive_type(exe_token) == _DRIVE_REMOTE


def _popen_command_and_cwd(command: str, cwd: str) -> tuple[str, str | None]:
    """Resolve the ``command``/``cwd`` pair to actually hand
    ``subprocess.Popen(..., shell=True)`` (#3543).

    ``shell=True`` on Windows always runs *command* via ``cmd.exe /c`` —
    and cmd.exe's own startup categorically refuses a UNC current
    directory: confirmed directly (``cmd.exe /c "cd \\\\wsl.localhost\\...
    && dir"`` prints "CMD does not support UNC paths as current
    directories.") and **silently** falls back to ``%windir%``
    (``C:\\Windows\\System32``) instead of raising — this is cmd.exe's own
    initialization check, not a ``CreateProcess``/``lpCurrentDirectory``
    limitation (that accepts a UNC path fine, which is exactly why the
    failure is silent: nothing downstream of ``Popen`` ever sees an error,
    every relative path in *command* just silently resolves against the
    wrong directory instead).

    For a UNC *cwd* this folds cmd.exe's own standard UNC workaround —
    ``pushd`` (maps an unused drive letter to the UNC path, cds into it) —
    into *command* itself, and returns ``cwd=None`` so ``Popen`` never
    hands cmd.exe a UNC starting directory it cannot use in the first
    place. A non-UNC (drive-letter) *cwd* is returned unchanged — the
    common case, where ``Popen``'s own ``cwd=`` already works correctly.
    """
    if cwd and _is_unc_path(cwd):
        return f'pushd "{cwd}" && {command}', None
    return command, (cwd or None)


# ── local-filesystem launch staging (#3617) ────────────────────────────────
#
# See the module docstring's own "#3617" section for the why. Split into a
# pure planning step (`_plan_staging`, Windows-path string math only, via
# `ntpath` — never touches a real filesystem, so it's testable with
# fabricated paths on any host OS) and a filesystem-executing step
# (`_execute_staging`, every real I/O call injectable and defaulting to the
# real `os`/`shutil` one) — the same "injectable OS-call seam" shape
# `WinCalls`/`Win32Calls` already use one level up, just for this one
# narrower concern.

#: Where staged sessions live, relative to `%LOCALAPPDATA%\Temp\` — real
#: local NTFS on every Windows host, never a UNC path.
_STAGING_ROOT_DIRNAME = "coord-app-drive"

#: The fleet's own convention (coordinator.yml's win-native `routes:`) for
#: a route's sample/settings working files — copied opportunistically
#: whenever present under `cwd`, regardless of whether the launch command
#: itself names it via a `cd` prefix.
_STAGING_FIXTURE_DIRNAME = ".smoke"

#: `%LOCALAPPDATA%` is only ever read for this one purpose.
_LOCALAPPDATA_ENV = "LOCALAPPDATA"

#: #3637: the env var every launched app's own per-user settings/config
#: directory resolves through (``%APPDATA%\<app>\...`` — e.g. vimcode's
#: ``vimcode_config_dir()``) — overridden to an isolated, per-session
#: directory on the launched process ONLY, never on this driver's own
#: environment, so a bugbash lane never reads or writes the real, shared,
#: persistent profile on the bridge host. See :func:`_isolated_env`.
_APPDATA_ENV = "APPDATA"


def _isolated_env(appdata_dir: str | None) -> dict[str, str] | None:
    """The ``env=`` kwarg :meth:`Win32Calls.launch`/
    :meth:`~Win32Calls.launch_in_terminal` must pass to ``subprocess.Popen``
    (#3637) — ``None`` (the same as not passing ``env=`` at all, i.e. the
    child inherits this process's own environment unchanged) when
    *appdata_dir* is ``None`` — every :meth:`Win32Calls._stage_if_needed`
    skip case (a non-UNC ``cwd``, unparseable command, ``%LOCALAPPDATA%``
    unavailable, ...), where there is no isolated directory to point at —
    otherwise a full copy of this process's own environment with
    :data:`_APPDATA_ENV` overridden to *appdata_dir*.

    A full copy (not a single-key dict) because the launched app still
    needs everything else a normal Windows process expects
    (``PATH``/``SystemRoot``/``USERPROFILE``/...) — only ``%APPDATA%``
    itself is being redirected away from the real, shared profile that
    motivated this fix; nothing else about the launch environment
    changes."""
    if appdata_dir is None:
        return None
    env = dict(os.environ)
    env[_APPDATA_ENV] = appdata_dir
    return env


def _local_app_data_via_known_folder() -> str | None:
    """Ask Windows itself for the current user's local-appdata root via
    ``SHGetKnownFolderPath(FOLDERID_LocalAppData)`` — the fallback for
    when this process's own *environment* doesn't carry
    ``%LOCALAPPDATA%`` (#3617 review).

    On the dell64 bridge path, the Windows-side Python this driver
    actually runs in (:class:`Win32Calls`) is started through WSL interop
    with no ``env=`` at all (:mod:`coord.app_drive`'s
    ``subprocess.Popen(argv, ...)``, :mod:`coord.win_native_bridge`'s
    ``run(exec_argv, ...)``), so it inherits the *Linux* agent's
    environment, translated by interop — ``LOCALAPPDATA`` is a Windows
    per-user variable, not a WSL one, and nothing threads it through
    ``WSLENV``. This mirrors the same problem (and the same "don't trust
    an inherited env var, ask the OS directly" fix) this module's sibling
    :mod:`coord.win_native_bridge` already applies for Python discovery —
    see its own ``_STANDARD_WINDOWS_PYTHON_GLOBS`` docstring.
    ``SHGetKnownFolderPath`` queries the OS's own per-user profile
    registration instead of any inherited variable, so it resolves
    correctly no matter how this process was spawned.

    Returns ``None`` (never raises) on any non-Windows platform or any
    failure — :meth:`Win32Calls._staging_root` folds that into the same
    "best-effort, fall back to the slow UNC path" contract it already
    documents for a missing env var.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_LocalAppData — {F1B32785-6FBA-4FCF-9D55-7B8E7F157091}
        folder_id = _GUID(
            0xF1B32785, 0x6FBA, 0x4FCF,
            (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
        )
        path_ptr = ctypes.c_wchar_p()
        hresult = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr)
        )
        if hresult != 0 or not path_ptr.value:
            return None
        resolved = path_ptr.value
        ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        return resolved
    except Exception:  # noqa: BLE001 — best-effort only, never worth crashing a launch over
        return None


#: A command containing any of these (beyond the one recognized `cd <dir>
#: && ` prefix `_strip_cd_prefix` already peels off) is an opaque shell
#: pipeline this driver will not guess at rewriting — `_plan_staging`
#: leaves it, and its UNC `cwd`, completely alone (falling back to
#: `_popen_command_and_cwd`'s own `pushd` wrap).
_SHELL_METACHARACTERS = ("&&", "&", "|", ">", "<", ";")

#: #3617 review nit: excludes every :data:`_SHELL_METACHARACTERS` member,
#: not just `&` — `cd a>b && x.exe` previously matched with
#: ``cd_dir = "a>b"`` even though the metacharacter guard elsewhere in
#: this module reads as if it covered the whole command. Harmless in
#: practice (`cd_dir` only ever becomes a path component, never shell
#: text, so this is defense in depth rather than a fix for an observed
#: failure), but worth being consistent about.
_CD_PREFIX_RE = re.compile(r'^cd\s+"?([^"&|><;]+?)"?\s*&&\s*', re.IGNORECASE)


def _strip_cd_prefix(command: str) -> tuple[str, str]:
    """``("", command)`` unless *command* starts with a plain ``cd <dir>
    && `` prefix (case-insensitive, optionally quoted) — the fleet's own
    convention for entering a route's fixture/working directory before
    launching (e.g. ``cd .smoke && ../target/release/vimcode.exe
    sample.txt``) — in which case returns ``(dir, remainder)``."""
    m = _CD_PREFIX_RE.match(command)
    if not m:
        return "", command
    return m.group(1), command[m.end():]


def _looks_shell_composed(command: str) -> bool:
    return any(token in command for token in _SHELL_METACHARACTERS)


def _leading_token(command: str) -> tuple[str, str]:
    """*command*'s leading whitespace-delimited token (optionally
    double-quoted, to allow an embedded space) and the unparsed remainder
    — e.g. ``'"My App.exe" sample.txt'`` -> ``('My App.exe',
    'sample.txt')``. ``("", "")`` for an empty/all-whitespace *command*."""
    stripped = command.strip()
    if not stripped:
        return "", ""
    if stripped[0] == '"':
        end = stripped.find('"', 1)
        if end != -1:
            return stripped[1:end], stripped[end + 1:].strip()
    parts = stripped.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


@dataclass(frozen=True)
class _StagingPlan:
    """What :func:`_plan_staging` decided — ``staged=False`` means "leave
    *command*/*cwd* exactly as given", the caller's cue to fall back to
    :func:`_popen_command_and_cwd`'s own UNC ``pushd`` wrap unchanged.

    ``command`` is ``None`` (the default) whenever the original *command*
    text stays correct unchanged — the common (relative-exe) case, where
    only ``cwd`` moves and every relative reference in *command* keeps
    resolving against the new root exactly as it did against the old one
    (see the function docstring). It is set only for the #3633
    absolute-exe case below, where the exe token itself must be rewritten
    to point at its staged copy; :meth:`Win32Calls._stage_if_needed`
    treats ``None`` as "use the *command* I was called with unchanged"
    (#3617/#3633 review: a plain ``str`` field defaulting to ``""`` made
    "unchanged" and "a literal empty command" indistinguishable by
    construction — ``None`` says the same thing without that footgun).

    ``skip_reason`` is set (and non-empty) on EVERY ``staged=False``
    return — never on a ``staged=True`` one — so
    :meth:`Win32Calls._stage_if_needed` can log/warn something more
    useful than one generic "could not parse" message for every skip
    reason, including the deliberate "parsed fine, already genuinely
    local, nothing to gain" case (#3633 review nit)."""

    staged: bool
    cwd: str = ""
    command: str | None = None
    source_exe: str = ""
    dest_exe: str = ""
    skip_reason: str = ""
    #: ``(source_dir, dest_dir)`` pairs to copy wholesale when the source
    #: exists — never an error when one doesn't (an optional fixture dir
    #: nobody provided this time).
    #:
    #: #3617 review (non-blocking): only ``dest_exe`` and these fixture
    #: dirs are copied — a sibling DLL/resource the exe needs that lives
    #: elsewhere under ``target/<triple>/release/`` is NOT staged, which
    #: would surface as a subtly broken staged copy (missing dependency)
    #: rather than a loud "exe not found". None of the fleet's current
    #: routes need one; worth revisiting if one ever does.
    #:
    #: Also (non-blocking): writes the staged app makes into a fixture dir
    #: (e.g. ``sample.txt``, or ``vimcode/settings.json`` under the
    #: ``.smoke`` dir that :meth:`Win32Calls._stage_if_needed` also hands
    #: the launched process as its isolated ``%APPDATA%``, #3637) land in
    #: the staged COPY, not the original WSL-tree fixture — a real
    #: behaviour change from pre-#3617, where the app edited the tree
    #: directly. No current spec asserts on post-run fixture content, but
    #: a future one that does must read it from the staged dir, not the
    #: WSL tree.
    #:
    #: Also (#3633 review, non-blocking): the absolute-exe case re-roots
    #: ``cwd`` to ``session_root`` for a command that, pre-#3633, launched
    #: unstaged against the real repo tree — but only ``cd_dir``/
    #: ``.smoke`` are copied here. A route like
    #: ``\\wsl...\x.exe docs/sample.txt`` (a relative DATA argument not
    #: under either) silently resolves to a file that was never staged.
    #: Pre-existing #3617 design for a relative exe; this just extends the
    #: exposure to a shape (absolute exe) that was previously never
    #: staged at all.
    fixture_copies: tuple[tuple[str, str], ...] = ()


def _plan_staging(
    command: str, cwd: str, *, session_root: str, get_drive_type=_get_drive_type,
) -> _StagingPlan:
    """Decide whether/how to stage *command*'s exe (plus its fixture
    dir(s)) from *cwd* into *session_root* instead of launching straight
    off *cwd* (#3617).

    Only engages when *cwd* is a UNC path (:func:`_is_unc_path`) — a
    same-host (non-WSL) ``win-native`` agent's ``cwd`` is already local,
    nothing to stage. Recognizes exactly two *command* shapes: a bare
    leading executable token (optionally quoted) with trailing arguments,
    and that same shape preceded by one ``cd <fixture-dir> && ``. Declines
    (``staged=False``) for anything else containing a shell metacharacter
    (:data:`_SHELL_METACHARACTERS` — a second ``&&``, a pipe, a
    redirection, ...) this driver cannot safely rewrite with confidence,
    an already-absolute-AND-already-local exe token (nothing of *cwd*'s
    own tree to stage for it — see the #3633 paragraph below for the
    absolute-but-still-UNC case, which IS staged), or an exe whose path
    relative to ``cwd``/``cd_dir`` climbs ABOVE ``cwd`` itself without a
    ``cd_dir`` to cancel it back out (staged at the same offset from
    ``session_root``, it would escape this session's own directory into
    the shared parent every other session's own root also lives under).

    *command* itself is returned unchanged (``plan.command is None``, the
    caller's cue to keep using the *command* it already has) in the
    common, relative-exe case — ``cwd`` moves to ``session_root`` ALWAYS,
    even when a ``cd <fixture-dir> && ...`` prefix was recognized —
    because *command* still carries that same ``cd <fixture-dir>`` prefix
    unchanged, and it is *command* (run from the new ``cwd``) that does
    the navigating into the fixture dir, exactly as it did from the old
    (UNC) ``cwd``. (#3617 review: an earlier revision set ``cwd`` to
    ``session_root/<fixture-dir>`` *itself* — i.e. pre-navigated — while
    leaving command's own ``cd <fixture-dir> &&`` in place too, so cmd.exe
    ran the ``cd`` a SECOND time from a directory that already had no
    further ``<fixture-dir>`` child staged under it, failed with a
    nonzero errorlevel, and `&&` short-circuited the whole launch before
    the exe ever ran.) The exe itself is copied to the SAME path, relative
    to *session_root*, that it already held relative to *cwd*
    (:func:`ntpath.normpath` applied to ``cd_dir`` + the exe token), so
    every relative reference in *command* — the ``cd`` itself, a ``../``
    the exe token carries, a trailing argument resolved against the
    post-``cd`` directory — keeps resolving correctly against the new,
    local root exactly as it did against the old, UNC one.

    **#3633: an absolute exe token reachable only over the network is
    staged too, not left alone.** A route's ``run:`` naming a relative
    exe path (e.g. ``../target/<triple>/release/vimcode.exe``) assumes
    cargo's default in-tree ``target/`` — but this fleet builds with a
    shared, per-repo ``CARGO_TARGET_DIR`` (``coord.cargo_cache``, #1402),
    so that relative path resolves to nothing and a worker substitutes
    the real, absolute build path instead. *That* substituted text is
    whatever the worker typed — nothing in this repo normalizes it — and
    #3633's own dell64 evidence showed it does NOT reliably come out as a
    UNC path: ``Get-Process`` there showed every launch running from
    ``Z:\\home\\john\\.coord\\cargo-target\\...``, a drive letter MAPPED
    to the WSL tree (``net use Z: \\wsl$\\Ubuntu...``) — ``ntpath.isabs``
    is ``True`` for that and :func:`_is_unc_path` is ``False``, so it
    still paid the exact same ``\\wsl$`` 9P cost staging exists to avoid,
    mislabelled "already genuinely local". :func:`_is_remote_exe_token`
    (UNC, or a drive letter :data:`_get_drive_type` reports as
    :data:`_DRIVE_REMOTE`) is what actually decides now, and a bare
    `/`-rooted WSL-style token (``/home/...`` — the issue's other named
    shape) is rewritten onto the ``\\wsl.localhost\\<distro>\\...`` UNC
    form first (:func:`_normalize_posix_exe_token`, borrowing *cwd*'s own
    distro prefix) before that same check runs. An absolute token that is
    already genuinely LOCAL (e.g. a real ``C:\\...`` exe on a fixed
    drive) is still left alone — nothing to gain from staging something
    already on local NTFS — and so is a `/`-rooted token when *cwd*
    itself isn't a ``\\wsl.localhost\\...`` UNC path to borrow a distro
    prefix from (nothing to safely guess). Staged by basename alone, at
    the top of *session_root* — an absolute token carries no meaningful
    position relative to *cwd* to preserve the way a relative one does —
    and *command*'s own leading token is rewritten (``plan.command``,
    always set — never ``None`` — here) to the staged, quoted destination
    path, with
    any recognized ``cd <fixture-dir> && `` prefix and trailing arguments
    carried through unchanged.
    """
    if not _is_unc_path(cwd):
        return _StagingPlan(staged=False, skip_reason="cwd is not a UNC path — already local")
    cd_dir, remainder = _strip_cd_prefix(command)
    if cd_dir and _is_rooted_or_drive_qualified(cd_dir):
        # A non-relative `cd_dir` (`cd C:\foo && ...`, `cd \foo && ...`)
        # would collapse `ntpath.join(session_root, cd_dir)` down to
        # `cd_dir` alone — or onto `session_root`'s drive ROOT —
        # discarding `session_root` entirely and landing `source_exe`/
        # `dest_exe` on the SAME absolute path (a `shutil.copy2`
        # `SameFileError`) — the `..`-escape guard below only ever
        # assumed a relative `cd_dir`, same as `exe_token`'s own check
        # just below. #3633 CI: this deliberately uses
        # `_is_rooted_or_drive_qualified` rather than `ntpath.isabs`,
        # whose meaning differs between 3.12 and 3.13 (see that
        # function's docstring).
        return _StagingPlan(
            staged=False,
            skip_reason=f"could not parse: cd prefix {cd_dir!r} is not cwd-relative",
        )
    if _looks_shell_composed(remainder):
        return _StagingPlan(
            staged=False,
            skip_reason="could not parse: command contains more than one shell operator",
        )
    exe_token, rest = _leading_token(remainder)
    if not exe_token:
        return _StagingPlan(staged=False, skip_reason="could not parse: command is empty")

    fixture_dirnames: list[str] = []
    for name in (cd_dir, _STAGING_FIXTURE_DIRNAME):
        if name and name not in fixture_dirnames:
            fixture_dirnames.append(name)
    fixture_copies = tuple(
        (ntpath.join(cwd, name), ntpath.join(session_root, name))
        for name in fixture_dirnames
    )

    if _is_rooted_or_drive_qualified(exe_token):
        # #3633 CI: NOT `ntpath.isabs` — that would route a `/`-rooted
        # WSL-style token here on 3.12 and into the relative branch
        # below on 3.13 (which stages it to the drive root, escaping
        # `session_root`). See `_is_rooted_or_drive_qualified`.
        resolved_token = exe_token
        if exe_token.startswith("//"):
            # An altsep-spelled UNC token (`//wsl.localhost/Ubuntu/...`)
            # — the same path, just not in Windows' own spelling. Respell
            # it rather than letting the `/`-rooted branch below treat it
            # as a distro-relative path and graft a second UNC prefix in
            # front of it.
            resolved_token = exe_token.replace("/", "\\")
        elif exe_token.startswith("/"):
            # #3633: a bare `/`-rooted WSL/POSIX-style token — never a
            # real Windows path as-is (a leading `/` resolves against
            # whatever drive happens to be CURRENT, not WSL's
            # filesystem). Rewrite it onto the `\\wsl.localhost\<distro>`
            # UNC shape by borrowing `cwd`'s own distro prefix; if `cwd`
            # isn't that shape there's no distro to borrow, and this
            # token is left alone rather than guessed at.
            normalized = _normalize_posix_exe_token(exe_token, cwd)
            if normalized is None:
                return _StagingPlan(
                    staged=False,
                    skip_reason=(
                        f"exe token {exe_token!r} is a /-rooted WSL-style path, "
                        "but cwd has no \\\\wsl.localhost\\<distro> prefix to "
                        "borrow a distro name from"
                    ),
                )
            resolved_token = normalized
        if not _is_remote_exe_token(resolved_token, get_drive_type=get_drive_type):
            # Already local (or a root-relative `\foo` shape with no
            # drive to classify at all) — nothing of cwd's own tree to
            # stage for it.
            return _StagingPlan(
                staged=False,
                skip_reason=(
                    f"exe token {resolved_token!r} is not on a drive we can "
                    "see as remote — already local, nothing to gain from "
                    "staging it"
                ),
            )
        # #3633: reachable only over the network (UNC, or a drive letter
        # `get_drive_type` reports as mapped/remote), just not resolved
        # through `cwd` — stage it by basename alone and rewrite
        # `command`'s own leading token to point at the staged copy.
        dest_exe = ntpath.join(session_root, ntpath.basename(resolved_token))
        cd_prefix = f'cd "{cd_dir}" && ' if cd_dir else ""
        rest_suffix = f" {rest}" if rest else ""
        new_command = f'{cd_prefix}"{dest_exe}"{rest_suffix}'
        return _StagingPlan(
            staged=True,
            cwd=session_root,
            command=new_command,
            source_exe=resolved_token,
            dest_exe=dest_exe,
            fixture_copies=fixture_copies,
        )

    exe_rel = ntpath.normpath(ntpath.join(cd_dir, exe_token)) if cd_dir else ntpath.normpath(exe_token)
    if exe_rel == ".." or exe_rel.startswith(f"..{ntpath.sep}"):
        # The exe's path, relative to *cwd* (after any `cd_dir`), climbs
        # ABOVE `cwd` itself (e.g. a bare `../target/release/vimcode.exe`
        # with no `cd` to cancel it out) — staged at the same relative
        # offset from `session_root`, it would land OUTSIDE this
        # session's own directory, in the shared parent every concurrent
        # session's staging root lives under: a leak `kill`'s own
        # (session-scoped) delete would never reach, and a collision with
        # another session's identically-named escape. Left alone rather
        # than risked — the `cd .smoke && ../target/...` shape above
        # cancels its own `..` back inside `cwd` and is unaffected by
        # this guard.
        return _StagingPlan(
            staged=False,
            skip_reason=(
                f"relative exe {exe_token!r} resolves to {exe_rel!r}, which "
                "climbs above cwd itself with no cd prefix to cancel it back "
                "out — staging it would escape this session's own directory"
            ),
        )
    # #3617 review: always `session_root` — NEVER `session_root/cd_dir`.
    # `command` still carries its own (unmodified) `cd <cd_dir> && `
    # prefix, which does the navigating into the staged fixture dir once
    # launched from here; pre-navigating `cwd` itself on top of that is
    # the double-`cd` bug this comment's sibling above explains.
    new_cwd = session_root

    return _StagingPlan(
        staged=True,
        cwd=new_cwd,
        source_exe=ntpath.join(cwd, exe_rel),
        dest_exe=ntpath.join(session_root, exe_rel),
        fixture_copies=fixture_copies,
    )


def _execute_staging(
    plan: _StagingPlan,
    *,
    isfile=os.path.isfile,
    isdir=os.path.isdir,
    makedirs=os.makedirs,
    dirname=os.path.dirname,
    copy_file=shutil.copy2,
    copy_tree=shutil.copytree,
) -> None:
    """The filesystem side of a staging *plan* (#3617) — split from
    :func:`_plan_staging` so the decision logic there stays testable with
    fabricated Windows-style path strings alone; every kwarg here
    defaults to the real ``os``/``shutil`` call (what :class:`Win32Calls`
    actually runs) and is injectable for a test using plain local paths.
    A no-op for a ``staged=False`` plan.

    Raises :class:`WinNativeRuntimeError` when the resolved exe doesn't
    exist — a copy step that silently skipped it would hand back a
    session dir with no exe in it, surfacing as a confusing failure much
    later at ``launch`` itself rather than here, where the real cause is
    actually known (#2096: a gate must be able to fail with an answer
    that actually says why). :meth:`Win32Calls._stage_if_needed` is the
    one caller, and treats this raise as "staging isn't viable for THIS
    launch" — falling back to the pre-#3617 ``pushd``/UNC launch rather
    than letting it fail the whole launch (:func:`_plan_staging`'s own
    leading-token guess is not infallible — e.g. a ``HOME=$PWD/home
    ...exe`` prefix some routes use — and a wrong guess must degrade
    gracefully, not turn a previously-working launch into a hard error).

    *plan*'s paths are built by :func:`_plan_staging` with ``ntpath``
    (correct on a real Windows host, where ``os.path`` already IS
    ``ntpath``) — this function then hands them to plain ``os``/
    ``shutil`` calls, which is only correct for that same reason; a test
    exercising this function with fabricated Windows-style strings on a
    non-Windows host must pass genuinely local (``tmp_path``-rooted)
    paths instead, as every test in this module already does.

    Also raises when *dest_exe* still doesn't exist immediately AFTER
    ``copy_file`` returns (#3633 review) — pre-#3633, a bad/incomplete
    copy still left a correct, if slow, UNC command behind (staging only
    ever moved ``cwd``); now that the absolute-exe case REWRITES
    *command* itself to point at ``dest_exe``
    (:attr:`_StagingPlan.command`), an exe that silently isn't actually
    there any more degrades to a confusing, late ``find_top_window``
    timeout instead of the documented "staging is a performance
    optimization, not a correctness requirement" fallback
    (:meth:`Win32Calls._stage_if_needed` catches this exactly like the
    pre-copy ``source_exe`` check, falling back to the pre-#3617
    ``pushd``/UNC launch).
    """
    if not plan.staged:
        return
    if not isfile(plan.source_exe):
        raise WinNativeRuntimeError(
            f"win-native launch staging (#3617): exe not found at "
            f"{plan.source_exe!r} — nothing to copy onto the local "
            "Windows filesystem"
        )
    makedirs(dirname(plan.dest_exe), exist_ok=True)
    copy_file(plan.source_exe, plan.dest_exe)
    if not isfile(plan.dest_exe):
        raise WinNativeRuntimeError(
            f"win-native launch staging (#3617): copy to {plan.dest_exe!r} "
            "reported success but the file isn't there afterward — "
            "refusing to launch a command rewritten to point at it"
        )
    for source_dir, dest_dir in plan.fixture_copies:
        if isdir(source_dir):
            copy_tree(source_dir, dest_dir, dirs_exist_ok=True)


def _remove_staged_dir(path: str) -> None:
    """Best-effort recursive delete — never raises, mirroring every other
    teardown step in this module (#2096: cleanup must not mask whatever
    real result it's running alongside)."""
    shutil.rmtree(path, ignore_errors=True)


#: #3617: a session whose owning `Win32Calls` got killed/crashed before its
#: own `kill()` ever ran has no Windows analogue of #3583's
#: `PR_SET_PDEATHSIG` to delete its staged directory for it — swept once per
#: `launch`/`launch_in_terminal` call instead (the same "idle self-expiry is
#: the backstop, not the primary path" shape `coord.app_drive
#: .DEFAULT_IDLE_TIMEOUT` already uses), generous enough that a normal
#: `kill()`-driven delete (the primary path) is always what actually cleans
#: up a session that closed normally.
_STALE_SESSION_MAX_AGE_S = 24.0 * 3600.0


#: #3544: every kwarg :meth:`Win32Calls.launch`/:meth:`~Win32Calls.launch_in_terminal`
#: must pass to ``subprocess.Popen`` so the launched ``cmd.exe`` (and whatever
#: GUI-subsystem grandchild it execs) never inherits this *process's own*
#: stdin/stdout/stderr handles.
#:
#: Without this, a plain ``subprocess.Popen(command, shell=True, cwd=...)``
#: (no ``stdin=``/``stdout=``/``stderr=``) hands the child Python's own
#: standard handles unchanged — on Windows that's an inheritance, not a
#: dup/close-on-exec situation. When this `Win32Calls` lives inside the
#: `win-native` bridge runner (:data:`coord.win_native_bridge._BRIDGE_RUNNER_SRC`,
#: a Windows-side ``python -c ...`` whose own stdout is a pipe the WSL-side
#: ``subprocess.run(capture_output=True)`` is reading), that pipe write-end
#: handle gets duplicated into the launched GUI exe. A GUI-subsystem process
#: (``vimcode.exe``) never exits on its own — a human closes the window, or a
#: spec step does — so it holds that handle open indefinitely. A pipe's
#: read end only sees EOF once *every* write-end handle is closed; with the
#: orphaned GUI exe still holding one, the WSL-side read blocks past the
#: whole bridge script's own completion, for the full
#: ``bridge_timeout``, even though the bridge runner itself finished and
#: exited normally. ``Get-Process`` then shows the launched exe still
#: running, orphaned, after the bridge has already timed out and raised.
#:
#: Redirecting all three standard streams to ``DEVNULL`` severs that
#: inheritance: the child gets its own private, already-closed-on-the-
#: parent-side handles, never a dup of this process's own pipe — nothing in
#: this driver ever reads a launched app's stdout/stderr (the whole point
#: of `win-native` is observing the real OS/window state, not console
#: text), so there is no output to lose.
#:
#: **Mechanism caveat (review finding on #3544, unresolved).** CPython's
#: own Windows ``subprocess._execute_child`` computes ``bInheritHandles``
#: as ``int(not close_fds)``, and ``close_fds`` defaults to ``True``
#: (Python 3.7+) and is never overridden anywhere in this module — so by
#: that code path alone, a bare ``Popen(command, shell=True, cwd=...)``
#: with no ``stdin=``/``stdout=``/``stderr=`` should *already* pass
#: ``bInheritHandles=False`` to ``CreateProcess``, meaning CPython's own
#: handle-inheritance bookkeeping is probably *not* the actual leak
#: mechanism. The hang itself is real and reproduced (``Get-Process``
#: showing the launched exe still running minutes after a timed-out bridge
#: call — see #3544's repro), but the likelier remaining culprit is
#: something outside CPython's control for this specific topology: the
#: bridge runner is itself started from WSL via ``wsl.exe``/interop, not a
#: plain native parent process, and WSL interop's own console/job-object
#: handling for the Windows-side process tree it creates can share or
#: re-parent handles differently than a same-OS parent/child pair would.
#:
#: This redirect is still the right thing to do regardless of which exact
#: mechanism turns out to be responsible: explicit ``DEVNULL`` handles are
#: a private pair nothing downstream can keep open, so it closes off every
#: plausible inheritance path at once rather than betting on one theory of
#: the leak being correct. Treat it as the best available fix, **not** a
#: hardware-confirmed root-cause diagnosis — it has not been re-run against
#: the issue's own two repro scripts on a real WSL<->Windows pairing
#: (dell64). ``tests/test_win_native_driver.py``'s
#: ``TestLaunchPipeInheritanceRealSubprocess`` is the closest reproduction
#: available without that hardware: a real (unmocked) OS pipe + subprocess
#: tree showing a long-lived grandchild can hold a parent's un-redirected
#: stdout pipe open past the parent's own exit, and that the real
#: ``Win32Calls.launch`` no longer does so once its grandchild's handles
#: are redirected.
#:
#: ``launch_in_terminal`` already passes ``creationflags=CREATE_NEW_CONSOLE``
#: for its spawned process, which per Windows' own docs already gives the
#: child its own console instead of sharing/inheriting the parent's — so
#: applying this redirect there too is defensive-in-depth, not evidence
#: that call site was equally exposed to the leak this fix targets.
_NO_HANDLE_INHERITANCE: dict = {
    "stdin": subprocess.DEVNULL,
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
}


class Win32Calls:
    """The real :class:`WinCalls` implementation — ``ctypes`` for window
    management, input injection, menu/hit-test probing and ``PrintWindow``;
    the optional ``comtypes``-based UI Automation client (the ``win-native``
    extra) for the accessibility tree.

    Windows-only: raises :class:`WinNativeRuntimeError` at construction on
    any other platform, mirroring
    :class:`coord.tui_pty_driver.WindowsConPtyChild`'s own platform guard.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise WinNativeRuntimeError(
                "Win32Calls requires Windows — the win-native driver only "
                "runs on a real Windows host (e.g. dell64)"
            )
        import ctypes  # noqa: PLC0415
        import ctypes.wintypes  # noqa: PLC0415

        self._ctypes = ctypes
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        #: #3617: pid -> the local session directory `launch`/
        #: `launch_in_terminal` staged for it, so `kill` can delete it once
        #: that pid has actually been signalled.
        #:
        #: Non-blocking review nit: keyed by pid alone, so a `kill` that's
        #: never called for a given session (daemon crash before its own
        #: `close()` ran) leaks that entry if Windows later reuses the
        #: same pid for an unrelated, staged launch — bounded by
        #: `_sweep_stale_sessions`'s 24h backstop either way, but the dict
        #: itself grows unboundedly in a long-lived process that never
        #: calls `kill`.
        self._staged_session_dirs: dict[int, str] = {}
        #: #3634: pid -> the open handle to the ``KILL_ON_JOB_CLOSE`` Job
        #: Object `launch`/`launch_in_terminal` assigned it to (see
        #: `_assign_kill_on_close_job`) — absent for a pid whose job
        #: assignment failed/was unavailable (a scripted test fake, or in
        #: principle a very old Windows missing one of these exports).
        #: `kill` pops and closes this FIRST, before its own
        #: `_descendant_pids` walk/`TerminateProcess` backstop.
        self._job_handles: dict[int, int] = {}
        #: #3663: pid -> the set of ``WindowsTerminal.exe`` pids that were
        #: ALREADY RUNNING, before `launch_in_terminal` ever spawned
        #: anything, the moment a ``terminal_app="windows-terminal"``
        #: launch started (empty for every other launch). ``wt.exe`` hands
        #: off to an already-running ``WindowsTerminal.exe`` via COM rather
        #: than spawning a fresh one — when that happens, the shared host
        #: process predates this launch and must never be torn down by
        #: this driver's own teardown, no matter what the live
        #: `_descendant_pids` walk says at `kill` time (a future process-
        #: tree change — Windows reparenting, or the walk itself drifting —
        #: must not silently make that possible again). See `kill` and
        #: `find_top_window`, which both subtract this set from whatever
        #: pids they'd otherwise treat as "ours".
        self._pre_existing_wt_pids: dict[int, frozenset[int]] = {}
        #: #3617 review: non-``None`` after `_stage_if_needed` skipped
        #: staging for a UNC `cwd` (as opposed to the common, unremarkable
        #: case of a same-host, already-local `cwd` where staging never
        #: even applies) — so a silent fallback to the slow UNC launch is
        #: observable rather than surfacing only much later as a
        #: `find_top_window` timeout. See `WinNativeSession.staging_warning`.
        #:
        #: Reset (to ``None``) at the top of every `_stage_if_needed` —
        #: which is only reached for a UNC `cwd`, so an instance reused for
        #: a LATER non-UNC launch keeps the earlier warning. There is
        #: exactly one `Win32Calls` per daemon/session
        #: (`coord.app_drive_daemon` builds it once, `coord.app_drive
        #: .SessionHandle` reads the warning once) and a given session's
        #: `cwd` never changes UNC-ness mid-flight, so that cannot happen
        #: today; clearing it in `launch` itself is deliberately NOT done
        #: because the non-UNC path must stay `self`-independent (see
        #: `launch`'s own comment). `tests/test_win_native_driver.py`'s
        #: `_make_win32_calls` initialises this too, so a test fake and a
        #: real instance agree on the starting value.
        self.staging_warning: str | None = None

        #: #3650 review (non-blocking): the per-session ``%APPDATA%`` this
        #: launch isolated the child into (#3637), when staging engaged —
        #: ``None`` otherwise (no isolation, ambient ``%APPDATA%`` applies).
        #: Read by :meth:`NativeRunner._do_expect_file` so an
        #: ``expect_file: {path: '$env:APPDATA\\...'}`` step resolves
        #: against the directory the launched app itself was given, not
        #: this process's own ambient profile — see :meth:`launch`'s own
        #: comment on why `appdata_dir` isn't otherwise visible outside
        #: the method that computes it.
        self.launched_appdata_dir: str | None = None

    # -- process lifecycle --

    def launch(self, command: str, cwd: str) -> int:
        # #3617: the UNC check is done BEFORE ever touching `self` — the
        # overwhelming common (non-UNC) case must stay exactly as cheap
        # (and as `self`-independent — see
        # `TestLaunchPipeInheritanceRealSubprocess`'s own unbound
        # `Win32Calls.launch(None, ...)` call) as it was pre-#3617. This is
        # the AUTHORITATIVE guard — `_plan_staging` re-checks the same
        # thing internally (so it stays correct when called directly, as
        # the tests do), not a sign of split logic between the two.
        staged_dir = None
        appdata_dir = None
        if _is_unc_path(cwd):
            command, cwd, staged_dir, appdata_dir = self._stage_if_needed(command, cwd)
        full_command, popen_cwd = _popen_command_and_cwd(command, cwd)
        proc = subprocess.Popen(
            full_command, shell=True, cwd=popen_cwd,
            env=_isolated_env(appdata_dir), **_NO_HANDLE_INHERITANCE,
        )
        # #3634: same unbound-`self`-safety requirement as above —
        # `TestLaunchPipeInheritanceRealSubprocess` calls this method
        # completely unbound (`self=None`) to drive a REAL subprocess tree
        # without needing a real (Windows-only) `Win32Calls` instance. Job
        # Object assignment needs a real `self._kernel32`, so it's skipped
        # whenever there's no `self` to assign through — exactly the case
        # where nothing tracks this pid for `kill` to reach later either,
        # so there is no regression in what gets torn down vs. before.
        if self is not None:
            if staged_dir is not None:
                self._staged_session_dirs[proc.pid] = staged_dir
            self.launched_appdata_dir = appdata_dir
            job = self._assign_kill_on_close_job(proc.pid)
            if job is not None:
                self._job_handles[proc.pid] = job
        return proc.pid

    def launch_in_terminal(self, command: str, cwd: str, terminal_app: str) -> int:
        staged_dir = None
        appdata_dir = None
        if _is_unc_path(cwd):
            command, cwd, staged_dir, appdata_dir = self._stage_if_needed(command, cwd)
        create_new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        if terminal_app == "windows-terminal":
            full_command = f"wt.exe {command}"
            # #3663: snapshot BEFORE spawning — `wt.exe` hands off to an
            # already-running `WindowsTerminal.exe` via COM rather than
            # spawning a fresh one, so any such process already exists at
            # this exact point or not at all. Recording it now (keyed by
            # the pid about to be returned) lets `kill`/`find_top_window`
            # refuse to ever touch it, regardless of whether it later
            # shows up in this pid's own descendant-process walk.
            pre_existing_wt_pids = frozenset(
                pid for pid, _ppid, name in self._snapshot_processes()
                if name.lower() == "windowsterminal.exe"
            )
        else:
            full_command = command
            pre_existing_wt_pids = frozenset()
        full_command, popen_cwd = _popen_command_and_cwd(full_command, cwd)
        proc = subprocess.Popen(
            full_command, shell=True, cwd=popen_cwd, creationflags=create_new_console,
            env=_isolated_env(appdata_dir), **_NO_HANDLE_INHERITANCE,
        )
        if terminal_app == "windows-terminal":
            self._pre_existing_wt_pids[proc.pid] = pre_existing_wt_pids
        if staged_dir is not None:
            self._staged_session_dirs[proc.pid] = staged_dir
        self.launched_appdata_dir = appdata_dir
        job = self._assign_kill_on_close_job(proc.pid)
        if job is not None:
            self._job_handles[proc.pid] = job
        return proc.pid

    def _assign_kill_on_close_job(self, pid: int) -> int | None:
        """#3634: wrap *pid* (the pid `launch`/`launch_in_terminal` just
        returned — ``cmd.exe``'s own pid for a ``shell=True`` launch, see
        :meth:`_descendant_pids`'s docstring, #3542) in a fresh Windows Job
        Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` set, and return
        the handle for the caller to hold onto (:attr:`_job_handles`) —
        ``None`` on any failure (`CreateJobObjectW` returning a null
        handle, `SetInformationJobObject`/`OpenProcess`/
        `AssignProcessToJobObject` returning false/null, or a scripted
        test fake with no Job Object support at all, caught as
        ``AttributeError``).

        As long as the returned handle stays open, Windows itself — not
        this process — tears down EVERY process still assigned to the job,
        including a ``cmd.exe /c`` grandchild this driver never otherwise
        tracks, the instant the job's LAST handle closes: either
        explicitly, when :meth:`kill` closes it, or automatically, if
        *this very process* (the ``win-native`` daemon holding the handle)
        crashes or is force-killed before its own ``kill()``/``close()``
        ever runs — Windows closes every handle a terminated process held,
        which is exactly the guarantee #3634 needs and that a
        `_descendant_pids`-walking `TerminateProcess` loop alone cannot
        give, since that walk needs a live, cooperating process to run it.

        Best-effort and silent on failure: a host where Job Objects are
        unavailable still gets the weaker (but still code-path-correct for
        every explicit `kill()` call) `_descendant_pids`-only teardown —
        this never blocks or fails a launch over it."""
        try:
            job = self._kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self._kernel32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                self._ctypes.byref(info), self._ctypes.sizeof(info),
            ):
                self._kernel32.CloseHandle(job)
                return None
            process_handle = self._kernel32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid,
            )
            if not process_handle:
                self._kernel32.CloseHandle(job)
                return None
            try:
                if not self._kernel32.AssignProcessToJobObject(job, process_handle):
                    self._kernel32.CloseHandle(job)
                    return None
            finally:
                self._kernel32.CloseHandle(process_handle)
            return job
        except AttributeError:
            # A scripted test fake (or, in principle, a very old Windows
            # missing one of these exports) with no Job Object support at
            # all — not a failure, just "unavailable"; see the docstring's
            # best-effort note.
            return None

    def kill(self, pid: int) -> None:
        # By PID (plus its own descendants) only — see the module
        # docstring's safety note. No image-name-based lookup exists
        # anywhere in this class.
        PROCESS_TERMINATE = 0x0001
        staged_dir = self._staged_session_dirs.pop(pid, None)
        # #3663: whatever `WindowsTerminal.exe` process(es) already
        # existed BEFORE this *pid*'s own `launch_in_terminal` call —
        # never terminated below, no matter what the live
        # `_descendant_pids` walk returns. `wt.exe` hands off to an
        # already-running host via COM rather than spawning a fresh one,
        # so a pre-existing host is a process this driver never launched
        # and must never tear down, even if some future process-tree
        # quirk (reparenting, pid reuse) made the walk below mistake it
        # for one of *pid*'s own descendants.
        protected_wt_pids = self._pre_existing_wt_pids.pop(pid, frozenset())
        # #3634: close OUR last handle to *pid*'s KILL_ON_JOB_CLOSE Job
        # Object FIRST, if one was successfully assigned at launch time —
        # this alone tears down the WHOLE descendant tree (including a
        # `cmd.exe /c` grandchild, #3542) regardless of whether this
        # process can still see/walk it. See
        # `_assign_kill_on_close_job`'s docstring. Not a hazard for
        # *protected_wt_pids*: `AssignProcessToJobObject` was only ever
        # called on *pid* itself at launch time (see
        # `_assign_kill_on_close_job`), and a pre-existing external
        # process reached via COM hand-off — rather than spawned as a
        # child of *pid* after assignment — never joins that job.
        job = self._job_handles.pop(pid, None)
        if job is not None:
            self._kernel32.CloseHandle(job)
        # #3634: ALWAYS terminate the whole descendant tree, not only when
        # there's a staged dir to clean up afterward — a 2026-10-05
        # bugbash lane showed this `kill` previously left every grandchild
        # (the real app under `cmd.exe`, #3542) running in the
        # overwhelmingly common (non-staged) case, since only a staged
        # session's own cleanup needed the walk to delete its directory.
        # Redundant with the Job Object close above whenever that
        # succeeded, but it's the ONLY teardown on a host/fake where job
        # assignment was unavailable, so it always runs regardless.
        #
        # #3663: *protected_wt_pids* subtracted unconditionally — see above.
        targets = self._descendant_pids(pid) - protected_wt_pids
        for target in targets:
            handle = self._kernel32.OpenProcess(PROCESS_TERMINATE, False, target)
            if handle:
                try:
                    self._kernel32.TerminateProcess(handle, 0)
                finally:
                    self._kernel32.CloseHandle(handle)
        # #3617: the normal (non-abnormal-exit) cleanup path for a staged
        # session directory — see `_sweep_stale_sessions` for the backstop.
        # Still best-effort (`_remove_staged_dir` never raises): even a
        # freshly-terminated process can hold a file handle open for a
        # brief window after `TerminateProcess` returns, in which case the
        # delete silently fails here and the 24h sweep is what actually
        # reclaims it.
        if staged_dir is not None:
            _remove_staged_dir(staged_dir)

    # -- local-filesystem launch staging (#3617) --

    def _staging_root(
        self, *, known_folder_resolver=_local_app_data_via_known_folder,
    ) -> str:
        """``%LOCALAPPDATA%\\Temp\\coord-app-drive`` — real local NTFS on
        every Windows host, never a UNC path. Tries this process's own
        environment first (the common, cheap case), then
        *known_folder_resolver* (:func:`_local_app_data_via_known_folder`
        by default — injectable for a test, since the real resolver's
        ``ctypes.windll`` call only exists on Windows) when that's unset —
        the dell64 bridge path starts this very process through WSL
        interop with no ``env=`` at all, so it never carries
        ``%LOCALAPPDATA%`` to read in the first place (never
        hardcoded/guessed either way — the issue's own requirement).
        Raises :class:`WinNativeRuntimeError` when BOTH fail;
        :meth:`_stage_if_needed` treats that as "best-effort staging
        unavailable" and falls back to the pre-#3617 ``pushd`` wrap rather
        than failing the whole launch over it — but records why via
        :attr:`staging_warning`, so that fallback is never silent."""
        local_app_data = os.environ.get(_LOCALAPPDATA_ENV) or known_folder_resolver()
        if not local_app_data:
            raise WinNativeRuntimeError(
                f"win-native launch staging (#3617) needs %{_LOCALAPPDATA_ENV}% "
                "to pick a per-session directory on the local Windows "
                "filesystem, but it is not set in this process's "
                "environment and SHGetKnownFolderPath could not resolve it "
                "either"
            )
        return os.path.join(local_app_data, "Temp", _STAGING_ROOT_DIRNAME)

    def _sweep_stale_sessions(self) -> None:
        """Best-effort GC backstop for an abandoned staged session (#3617,
        see the module docstring) — deletes any direct child of
        :meth:`_staging_root` whose mtime is older than
        :data:`_STALE_SESSION_MAX_AGE_S`. Never raises: a missing/unreadable
        root, or an entry that disappears mid-sweep (another process
        already cleaned it up), is not an error here.

        Non-blocking review nit: keys off the session DIRECTORY's own
        mtime, which does NOT update when the running app writes into
        ``<session>/.smoke`` underneath it — a session alive longer than
        :data:`_STALE_SESSION_MAX_AGE_S` could in principle have its
        working directory swept out from under it. Unlikely at the
        current (24h) timeout; stamping the dir itself (or checking the
        newest descendant mtime) would close the gap if it's ever
        tightened."""
        try:
            root = self._staging_root()
            entries = os.listdir(root)
        except (OSError, WinNativeRuntimeError):
            return
        now = time.time()
        for name in entries:
            path = os.path.join(root, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age > _STALE_SESSION_MAX_AGE_S:
                _remove_staged_dir(path)

    def _stage_if_needed(
        self, command: str, cwd: str,
    ) -> tuple[str, str, str | None, str | None]:
        """Returns ``(command, cwd, staged_dir, appdata_dir)`` — *command*/
        *cwd* rewritten per :func:`_plan_staging` (with the staging
        actually performed) when it decided to, or *command*/*cwd*
        UNCHANGED (and ``staged_dir=None``, ``appdata_dir=None``) in every
        skip case: a non-UNC ``cwd``, an unparseable/opaque *command*,
        ``%LOCALAPPDATA%`` itself being unavailable right now, or the
        resolved exe not actually existing where :func:`_plan_staging`
        guessed (:func:`_execute_staging`'s own raise — a route whose
        ``run:`` doesn't match either shape ``_plan_staging`` recognizes,
        e.g. a leading ``HOME=$PWD/home`` env-var assignment some routes
        use ahead of the real exe token). Staging is a performance
        optimization, not a correctness requirement — a caller that can't
        stage still gets a working (if UNC-slow) launch via
        :func:`_popen_command_and_cwd`'s own ``pushd`` wrap, rather than
        this call failing outright.

        Every skip case EXCEPT the non-UNC one (the only one this is ever
        called for — see the ``_is_unc_path`` gate at both call sites)
        records why on :attr:`staging_warning` and logs it — #3617 review:
        a silent fallback here would otherwise surface only much later, as
        a confusing ``find_top_window`` timeout with no indication staging
        was ever involved.

        #3633: the returned *command* is ``plan.command`` when the plan set
        one (the absolute-exe case, where the exe token itself had to be
        rewritten to its staged copy) or the original *command* when it
        didn't (``plan.command is None`` — the common relative-exe case,
        where *command* already resolves correctly against the staged
        ``cwd`` unchanged).

        **#3637: ``appdata_dir`` is the per-session directory the caller
        must point the launched process's own ``%APPDATA%`` at.** A
        project-local ``.vimcode``-style fixture next to the opened file
        (the previously-documented win-native lane setup step) is silently
        ignored — the real app reads ``%APPDATA%\\<app>\\settings.json``
        unconditionally (see ``coord.win_native_driver``'s module
        docstring and the issue itself), so without this every win-native
        launch read/wrote ONE real, shared, persistent settings file on
        the bridge host no matter what a route staged next to the exe.
        Reuses the SAME staged :data:`_STAGING_FIXTURE_DIRNAME` (``.smoke``)
        directory this function already stages fixture/working files
        into — already documented above as a route's own "sample/settings
        working files" convention — as the literal, isolated ``%APPDATA%``
        root: a route that wants ``vimcode``'s Nerd Fonts setting off now
        stages ``.smoke/vimcode/settings.json`` (mirroring the real
        ``%APPDATA%\\vimcode\\settings.json`` shape exactly, not a
        project-local ``.vimcode`` one) and it lands exactly where the
        launched exe actually looks. ``None`` only when staging itself
        didn't happen (every skip case above) — the caller then leaves
        ``%APPDATA%`` at its ambient, unisolated value, same as before
        this fix, and :attr:`staging_warning` already reports why staging
        itself was skipped."""
        self.staging_warning = None
        try:
            root = self._staging_root()
        except WinNativeRuntimeError as exc:
            self.staging_warning = str(exc)
            _log.warning("%s", exc)
            return command, cwd, None, None
        session_root = os.path.join(root, uuid.uuid4().hex[:12])
        plan = _plan_staging(command, cwd, session_root=session_root)
        if not plan.staged:
            # #3633 review nit: `plan.skip_reason` distinguishes an
            # actually-unparseable command/cwd shape from the deliberate
            # "parsed fine, exe is already genuinely local, nothing to
            # gain" skip — a prior, single generic "could not parse"
            # message here claimed the latter never parsed at all, which
            # is misleading for anyone diagnosing a lane from the log.
            self.staging_warning = (
                f"win-native launch staging (#3617/#3633): {plan.skip_reason} "
                f"— launching from the UNC path {cwd!r} unstaged (slow "
                "\\\\wsl$ I/O), and %APPDATA% is NOT isolated either (#3637)"
            )
            _log.warning("%s", self.staging_warning)
            return command, cwd, None, None
        self._sweep_stale_sessions()
        try:
            _execute_staging(plan)
        except WinNativeRuntimeError as exc:
            self.staging_warning = str(exc)
            _log.warning("%s", exc)
            return command, cwd, None, None
        # #3637: created even when no `.smoke` fixture exists on the route
        # side at all — the launched process must find SOME directory at
        # `%APPDATA%`, even an empty one, rather than a dangling path (and
        # an empty, isolated one is still strictly better than the real,
        # shared, persistent profile this whole fix exists to avoid
        # touching).
        appdata_dir = os.path.join(session_root, _STAGING_FIXTURE_DIRNAME)
        os.makedirs(appdata_dir, exist_ok=True)
        return (
            (plan.command if plan.command is not None else command),
            plan.cwd, session_root, appdata_dir,
        )

    # -- session precheck (#3510) --

    def session_available(self) -> tuple[bool, str]:
        """Real check: ``OpenInputDesktop`` succeeds (an interactive
        desktop exists to open at all), ``WTSGetActiveConsoleSessionId``
        reports a real console session, and no ``LogonUI.exe`` is running
        in that session (the lock-screen host process). Any one of these
        failing means the host's desktop is locked or absent.

        Defensive by design: this gates every ``win-native`` run (#3510), so
        a probe that raises (e.g. a lookup error on a function that doesn't
        live where expected — #3521) must report "unavailable, here's why"
        rather than take the whole lane down with an uncaught exception."""
        try:
            return self._session_available_unchecked()
        except Exception as exc:  # noqa: BLE001 - must never crash the lane
            return False, f"session_available probe failed: {exc}"

    def _session_available_unchecked(self) -> tuple[bool, str]:
        ctypes = self._ctypes
        DESKTOP_READOBJECTS = 0x0001
        hdesk = self._user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not hdesk:
            return False, (
                "OpenInputDesktop failed — no interactive input desktop is "
                "available on this session (locked or non-interactive)"
            )
        self._user32.CloseDesktop(hdesk)

        # WTSGetActiveConsoleSessionId is exported by kernel32, not
        # wtsapi32 (#3521) — it's the one WTS* function that lives there;
        # the rest of the WTS family (WTSQuerySessionInformation etc.) is
        # genuinely in wtsapi32, which is the likely source of the mix-up.
        INVALID_SESSION_ID = 0xFFFFFFFF
        session_id = self._kernel32.WTSGetActiveConsoleSessionId()
        if session_id == INVALID_SESSION_ID:
            return False, (
                "WTSGetActiveConsoleSessionId reports no active console "
                "session on this host"
            )

        if self._logonui_running_in_session(session_id):
            return False, (
                f"LogonUI.exe is running in session {session_id} — the "
                "desktop is locked"
            )
        return True, ""

    def _logonui_running_in_session(self, session_id: int) -> bool:
        """``True`` iff ``LogonUI.exe`` (the Windows lock-screen host
        process) is running in *session_id* — walked via
        :meth:`_snapshot_processes`, the same mechanism Task Manager itself
        uses, rather than anything that could be confused by a differently-
        named process (the module docstring's "kill only the PID this
        driver itself launched" safety note applies equally here: this is
        read-only enumeration, never a kill)."""
        ctypes = self._ctypes
        for pid, _ppid, name in self._snapshot_processes():
            if name.lower() == "logonui.exe":
                proc_session = ctypes.wintypes.DWORD()
                self._kernel32.ProcessIdToSessionId(pid, ctypes.byref(proc_session))
                if proc_session.value == session_id:
                    return True
        return False

    def _snapshot_processes(self) -> list[tuple[int, int, str]]:
        """Every currently running process as ``(pid, parent_pid,
        exe_name)``, via a single ``CreateToolhelp32Snapshot`` walk — the
        same mechanism Task Manager itself uses. Read-only enumeration,
        shared by :meth:`_logonui_running_in_session` (#3510) and
        :meth:`_descendant_pids` (#3542); never used to kill (see the
        module docstring's safety note)."""
        ctypes = self._ctypes
        kernel32 = self._kernel32
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == -1:
            return []
        out: list[tuple[int, int, str]] = []
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return []
            while True:
                name = entry.szExeFile.decode("mbcs", errors="ignore")
                out.append((entry.th32ProcessID, entry.th32ParentProcessID, name))
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        return out

    def _descendant_pids(self, root_pid: int) -> set[int]:
        """*root_pid* plus every process it transitively spawned (child,
        grandchild, ...) — a fresh :meth:`_snapshot_processes` walk each
        call, since the real app may not have started yet the first time
        this is polled.

        #3542: ``launch``/``launch_in_terminal`` don't always return the
        real app's own pid — ``subprocess.Popen(..., shell=True)`` returns
        ``cmd.exe``'s pid, with the real app (``vimcode.exe``) a grandchild
        that ``cmd.exe`` itself never owns a window for. Searching the
        whole descendant tree finds the real app's window regardless of
        how many shell/console hosts sit between the returned pid and it.
        """
        children: dict[int, list[int]] = {}
        for pid, ppid, _name in self._snapshot_processes():
            children.setdefault(ppid, []).append(pid)
        result = {root_pid}
        frontier = [root_pid]
        while frontier:
            current = frontier.pop()
            for child in children.get(current, ()):
                if child not in result:
                    result.add(child)
                    frontier.append(child)
        return result

    def find_top_window(self, pid: int, timeout_s: float) -> int:
        ctypes = self._ctypes
        deadline = time.monotonic() + timeout_s
        found: list[int] = []
        candidate_pids: set[int] = {pid}
        # `WINFUNCTYPE` (stdcall) only exists in ctypes on real Windows —
        # real runs always go through it (Win32Calls.__init__ guards
        # off-Windows construction), but falling back to `CFUNCTYPE` off-
        # Windows is what lets this method's descendant-walk logic be
        # exercised by a scripted fake on Linux/macOS too (#3542), the same
        # "unit-testable on any platform" seam the module docstring
        # describes for the rest of this class.
        win_functype = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

        @win_functype(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum_proc(hwnd, _lparam):
            owner_pid = ctypes.wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value in candidate_pids and self._user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        # #3663: never treat a pre-existing `WindowsTerminal.exe` host
        # (recorded by `launch_in_terminal` BEFORE this *pid* ever existed)
        # as a candidate — it's reached via COM hand-off, not spawned as
        # our descendant, but excluding it here too means a window this
        # driver never launched (quite possibly the operator's own) can
        # never be mistaken for the one we're waiting on, regardless of
        # what the live `_descendant_pids` walk returns.
        protected_wt_pids = self._pre_existing_wt_pids.get(pid, frozenset())
        while time.monotonic() < deadline:
            candidate_pids = self._descendant_pids(pid) - protected_wt_pids
            found.clear()
            self._user32.EnumWindows(_enum_proc, 0)
            if found:
                return found[0]
            time.sleep(0.1)
        raise WinNativeRuntimeError(
            f"no visible top-level window appeared for pid={pid} or any of "
            f"its child processes within {timeout_s}s"
        )

    def is_window_alive(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(hwnd))

    def move_window(self, hwnd: int, x: int, y: int, width: int, height: int) -> None:
        self._user32.MoveWindow(hwnd, x, y, width, height, True)

    # -- menu / hit-test probes --

    def get_menu_items(self, hwnd: int) -> list[str] | None:
        hmenu = self._user32.GetMenu(hwnd)
        if not hmenu:
            return None
        count = self._user32.GetMenuItemCount(hmenu)
        MF_BYPOSITION = 0x00000400
        items = []
        buf = self._ctypes.create_unicode_buffer(256)
        for i in range(max(count, 0)):
            self._user32.GetMenuStringW(hmenu, i, buf, 256, MF_BYPOSITION)
            items.append(buf.value)
        return items

    def hit_test(self, hwnd: int, x: int, y: int) -> str:
        WM_NCHITTEST = 0x0084
        lparam = (y << 16) | (x & 0xFFFF)
        result = self._user32.SendMessageW(hwnd, WM_NCHITTEST, 0, lparam)
        # ctypes returns an unsigned value for negative hit-test codes
        # (HTERROR=-2, HTTRANSPARENT=-1) — re-interpret as signed 32-bit.
        if result > 0x7FFFFFFF:
            result -= 0x100000000
        return _HT_CODES_BY_VALUE.get(result, f"HT_UNKNOWN({result})")

    # -- input injection --

    def send_click(self, hwnd: int, x: int, y: int, button: str) -> None:
        rect = self._ctypes.wintypes.RECT()
        self._user32.GetWindowRect(hwnd, self._ctypes.byref(rect))
        screen_x, screen_y = rect.left + x, rect.top + y
        self._user32.SetCursorPos(screen_x, screen_y)
        down, up = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }[button]
        self._user32.mouse_event(down, 0, 0, 0, 0)
        self._user32.mouse_event(up, 0, 0, 0, 0)

    def _declare_send_input_signature(self) -> None:
        """Declares ``SendInput``'s ``argtypes``/``restype`` once per
        instance (#3639 review nit: previously left undeclared, so the call
        rode on ctypes' default conversions). Idempotent and cheap to call
        from every :meth:`send_key` — guards against a fake ``user32`` in
        tests that has no ``argtypes``/``restype`` attributes at all."""
        send_input = self._user32.SendInput
        if getattr(send_input, "argtypes", None) is not None:
            return
        try:
            send_input.argtypes = [
                ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int,
            ]
            send_input.restype = ctypes.c_uint
        except AttributeError:
            # A fake `user32` in a test may not allow attribute assignment
            # on its `SendInput` — fine, the real Windows DLL function
            # always does.
            pass

    def send_key(self, hwnd: int, key: str) -> None:
        """#3639: *key* is parsed under the shared grammar
        (:func:`_win_key_encodings`) and may be a space-separated chord
        sequence (``ctrl+k ctrl+w``) — each chord is sent as its own
        modifier-down, key(s)-down/up, modifier-up sequence, in order, via
        ``SendInput`` (never the legacy ``keybd_event`` the pre-#3639 code
        used — ``SendInput`` is what ``KEYEVENTF_UNICODE`` requires, closing
        #3635's silent no-op for punctuation)."""
        self._user32.SetForegroundWindow(hwnd)
        self._declare_send_input_signature()

        def _one(vk: int, scan: int, flags: int) -> _INPUT:
            return _INPUT(type=_INPUT_KEYBOARD, ki=_KEYBDINPUT(vk, scan, flags, 0, 0))

        def _send(*inputs: _INPUT) -> None:
            if not inputs:
                return
            arr = (_INPUT * len(inputs))(*inputs)
            sent = self._user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
            if sent != len(inputs):
                # #3639: SendInput returns the number of events it actually
                # inserted and returns 0 (or a short count) when the input
                # was blocked — e.g. UIPI, or the target window not owning
                # the foreground/input desktop (see
                # `_logonui_running_in_session`/`OpenInputDesktop` below). An
                # unchecked call here would reproduce #3635's "{"ok": true}"
                # with no effect, just one layer deeper: the process issues
                # the call but the keystroke never lands, and nothing before
                # this point can tell the difference.
                raise WinNativeRuntimeError(
                    f"SendInput delivered only {sent} of {len(inputs)} "
                    f"input event(s) — blocked by UIPI or the input desktop "
                    f"belongs to another session/thread"
                )

        for enc in _win_key_encodings(key):
            mod_downs: list[_INPUT] = []
            mod_ups: list[_INPUT] = []
            for active, vk in (
                (enc.win, _VK_LWIN), (enc.ctrl, _VK_CONTROL),
                (enc.alt, _VK_MENU), (enc.shift, _VK_SHIFT),
            ):
                if active:
                    mod_downs.append(_one(vk, 0, 0))
                    mod_ups.insert(0, _one(vk, 0, _KEYEVENTF_KEYUP))

            if enc.unicode_char is not None:
                code = ord(enc.unicode_char)
                key_down = _one(0, code, _KEYEVENTF_UNICODE)
                key_up = _one(0, code, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)
            else:
                key_down = _one(enc.vk, 0, 0)
                key_up = _one(enc.vk, 0, _KEYEVENTF_KEYUP)

            _send(*mod_downs, key_down, key_up, *mod_ups)

    def type_text(self, hwnd: int, text: str) -> None:
        """#3650: one ``KEYEVENTF_UNICODE`` down/up pair per UTF-16 CODE
        UNIT, via ``SendInput`` — the same real-input call (and the same
        "delivered < sent means blocked" check) :meth:`send_key` already
        uses for its own ``unicode_char`` branch.

        Deliberately iterates UTF-16 code units, not Python characters:
        ``wScan``/``KEYEVENTF_UNICODE`` is a 16-bit field, so a non-BMP
        character (any emoji — exactly the kind of glyph an
        ``expect_no_tofu`` step would want typed) needs a *surrogate
        pair* — two code units, two SendInput events — same as Windows'
        own Unicode keyboard input is itself defined. Encoding to
        ``utf-16-le`` produces that surrogate pair automatically; iterating
        ``ord(ch)`` over the Python string instead would silently truncate
        a code point above 0xFFFF to its low 16 bits (``ctypes`` wraps
        rather than raising) and type the wrong character."""
        self._user32.SetForegroundWindow(hwnd)
        self._declare_send_input_signature()
        encoded = text.encode("utf-16-le")
        code_units = struct.unpack(f"<{len(encoded) // 2}H", encoded)
        for code in code_units:
            key_down = _INPUT(type=_INPUT_KEYBOARD, ki=_KEYBDINPUT(0, code, _KEYEVENTF_UNICODE, 0, 0))
            key_up = _INPUT(
                type=_INPUT_KEYBOARD,
                ki=_KEYBDINPUT(0, code, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, 0),
            )
            arr = (_INPUT * 2)(key_down, key_up)
            sent = self._user32.SendInput(2, arr, ctypes.sizeof(_INPUT))
            if sent != 2:
                raise WinNativeRuntimeError(
                    f"SendInput delivered only {sent} of 2 input event(s) for "
                    f"UTF-16 code unit {code:#06x} — blocked by UIPI or the "
                    f"input desktop belongs to another session/thread"
                )

    def _owning_pid(self, hwnd: int) -> int:
        """``GetWindowThreadProcessId``'s pid output for *hwnd* (0 for an
        invalid/null hwnd) — shared by :meth:`is_frontmost` below and
        :meth:`find_top_window`'s own enum callback."""
        ctypes = self._ctypes
        if not hwnd:
            return 0
        owner_pid = ctypes.wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        return owner_pid.value

    def is_frontmost(self, hwnd: int) -> tuple[bool, int]:
        """#3650: ``GetForegroundWindow`` — the real Win32 notion of "which
        top-level window currently has keyboard focus" — compared at the
        OWNING-PID level, not hwnd identity: ``GetWindowThreadProcessId``
        on both the foreground window and *hwnd* itself, per the issue's
        own "owning pid" wording and the mac driver's own pid-level
        semantics. A popup/dialog/second top-level window belonging to the
        SAME app being in front still counts as frontmost — an hwnd-
        equality check would false-FAIL on exactly that (safe, since it
        fails closed, but noisier than specified) shape."""
        foreground = self._user32.GetForegroundWindow()
        foreground_pid = self._owning_pid(foreground)
        hwnd_pid = self._owning_pid(hwnd)
        return foreground_pid != 0 and foreground_pid == hwnd_pid, foreground

    # -- UI Automation --

    def uia_elements(self, hwnd: int) -> list[dict]:
        client = _import_uia()
        automation = client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            clsctx=1,  # CLSCTX_INPROC_SERVER
        )
        root = automation.ElementFromHandle(hwnd)
        walker = automation.ControlViewWalker
        elements: list[dict] = []

        def _walk(element) -> None:
            try:
                elements.append({
                    "role": element.LocalizedControlType or "",
                    "name": element.CurrentName or "",
                    "visible": not bool(element.CurrentIsOffscreen),
                })
            except Exception:  # noqa: BLE001 — a dead/stale element node
                return
            child = walker.GetFirstChildElement(element)
            while child is not None:
                _walk(child)
                child = walker.GetNextSiblingElement(child)

        _walk(root)
        return elements

    # -- capture --

    def capture(self, hwnd: int) -> bytes:
        ctypes = self._ctypes
        gdi32 = ctypes.windll.gdi32
        rect = ctypes.wintypes.RECT()
        self._user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise WinNativeRuntimeError(
                f"cannot capture hwnd={hwnd} — window rect is {width}x{height}"
            )

        hdc_window = self._user32.GetWindowDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        gdi32.SelectObject(hdc_mem, hbitmap)
        PW_RENDERFULLCONTENT = 0x00000002
        ok = self._user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        try:
            if not ok:
                raise WinNativeRuntimeError(f"PrintWindow failed for hwnd={hwnd}")
            return _bitmap_to_bmp_bytes(ctypes, gdi32, hdc_mem, hbitmap, width, height)
        finally:
            gdi32.DeleteObject(hbitmap)
            gdi32.DeleteDC(hdc_mem)
            self._user32.ReleaseDC(hwnd, hdc_window)


def _bitmap_to_bmp_bytes(ctypes, gdi32, hdc_mem, hbitmap, width: int, height: int) -> bytes:
    """``GetDIBits`` the captured bitmap into raw 24-bit BGR pixel data,
    wrapped in a minimal, self-contained ``.bmp`` file (no extra imaging
    library needed) — sufficient as a failing-step evidence attachment."""
    import struct

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    row_bytes = ((width * 3 + 3) // 4) * 4
    image_size = row_bytes * height
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = height
    header.biPlanes = 1
    header.biBitCount = 24
    header.biCompression = 0  # BI_RGB
    header.biSizeImage = image_size

    buf = (ctypes.c_ubyte * image_size)()
    gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, buf, ctypes.byref(header), 0)

    bmp_header = struct.pack("<2sIHHI", b"BM", 14 + ctypes.sizeof(header) + image_size, 0, 0, 14 + ctypes.sizeof(header))
    return bmp_header + bytes(header) + bytes(buf)


def _import_uia():
    """The optional ``comtypes`` UI Automation client (the ``win-native``
    extra) — guarded the same way
    :func:`coord.tui_pty_driver._import_pyte`/``WindowsConPtyChild`` guard
    their own optional dependencies, so a missing package names the extra
    to install rather than surfacing a bare ``ModuleNotFoundError``."""
    try:
        import comtypes.client as client  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise WinNativeRuntimeError(
            "win-native needs the 'win-native' extra, which is not "
            "installed (missing 'comtypes').\n"
            "  Install it with:  pip install 'code-coordinator[win-native]'"
        ) from exc
    return client


#: PE optional-header ``Subsystem`` values (``winnt.h``'s ``IMAGE_SUBSYSTEM_*``)
#: this driver cares about for #3640's auto mode selection below — a GUI
#: exe's top-level window belongs to its own pid (the existing, unchanged
#: path); a CUI (console) exe's window belongs to whatever conhost.exe/
#: Windows Terminal is hosting it, which `WinCalls.launch`'s plain
#: ``find_top_window(pid, ...)`` wait never finds no matter how long it
#: waits — the root cause of #3640's indefinite `coord app-drive open
#: win-native` hang on a console app (``vcd.exe``).
IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
IMAGE_SUBSYSTEM_WINDOWS_CUI = 3

def _join_relative(base: str, segment: str) -> str:
    """Join *segment* onto *base* with a single ``/`` — deliberately NOT
    :func:`ntpath.join`, which always inserts a ``\\`` at the join point
    even when neither operand already has one. That matters here because,
    unlike :func:`_plan_staging` (pure path-string math whose OUTPUT is
    executed elsewhere, by :func:`_execute_staging`, against separately
    -built strings), this helper's result is handed straight to
    :func:`_pe_subsystem`'s own ``open()`` in the SAME process — so on a
    non-Windows test host (a plain ``tmp_path``, no backslash anywhere in
    sight) an injected ``\\`` would make the join point an illegal
    filename character instead of a separator, and the real on-disk file
    would never be found. Windows' own file APIs accept ``/`` as a
    separator interchangeably with ``\\`` — including mixed into an
    already-``\\``-separated *base* (this driver's only real caller,
    :class:`WinNativeSession`, always gets a ``\\``-separated UNC *cwd*,
    #3543) — so this is safe there too."""
    return f"{base.rstrip('/\\')}/{segment}"


def _guess_exe_path(command: str, cwd: str) -> str | None:
    """Best-effort extraction of the real ``.exe`` *command* (a raw shell
    command string, not a parsed argv — see :func:`coord.app_drive
    .open_session`) launches, resolved against *cwd* when relative.

    Driven off the EXACT same small parsers :func:`_plan_staging` answers
    this identical "which exe does this launch command run, relative to
    which directory" question with — :func:`_strip_cd_prefix`,
    :func:`_leading_token`, :func:`_is_rooted_or_drive_qualified`, and
    :func:`_normalize_posix_exe_token` for a `/`-rooted WSL token — rather
    than a second, independently-written extractor that could (and, pre
    -fix, did: #3640 review) silently disagree with it on a `cd <dir> &&
    ` prefix, a quoted path containing a space, or a `/`-rooted WSL token
    (#2096 "one question, one answer").

    Returns ``None`` (never raises) — exactly like :func:`_plan_staging`
    returning ``staged=False`` for the same shapes — when: *command* has
    no recognizable leading ``.exe`` token at all; its ``cd <dir> && ``
    prefix (if any) is itself rooted/drive-qualified (ambiguous relative
    to *cwd*, same guard :func:`_plan_staging` applies); or *command*
    contains a shell metacharacter beyond that one recognized prefix
    (:func:`_looks_shell_composed`) — an opaque pipeline this driver will
    not guess at. :func:`_detect_console_subsystem` treats every ``None``
    exactly like "PE header unreadable", i.e. "couldn't tell", never an
    error."""
    cd_dir, remainder = _strip_cd_prefix(command)
    if cd_dir and _is_rooted_or_drive_qualified(cd_dir):
        return None
    if _looks_shell_composed(remainder):
        return None
    exe_token, _rest = _leading_token(remainder)
    if not exe_token or not exe_token.lower().endswith(".exe"):
        return None
    if _is_rooted_or_drive_qualified(exe_token):
        if exe_token.startswith("//"):
            # Altsep-spelled UNC token — same shape `_plan_staging` respells.
            return exe_token.replace("/", "\\")
        if exe_token.startswith("/"):
            normalized = _normalize_posix_exe_token(exe_token, cwd)
            return normalized if normalized is not None else exe_token
        return exe_token
    base = _join_relative(cwd, cd_dir) if cd_dir else cwd
    return _join_relative(base, exe_token)


def _pe_subsystem(path: str) -> int | None:
    """The ``Subsystem`` field of *path*'s PE optional header — read
    directly off the ``IMAGE_DOS_HEADER``/``IMAGE_NT_HEADERS`` byte
    layout, no ``ctypes``/Windows API needed, so this is callable (and
    unit-tested) on any platform. ``None`` when *path* doesn't exist,
    isn't readable, or isn't a well-formed PE image (wrong ``MZ``/``PE``
    magic, or truncated before the field) — a best-effort SNIFF for
    :func:`_detect_console_subsystem`'s own caller, never a hard
    requirement, so every failure mode here collapses to "couldn't tell"
    rather than raising.

    The ``Subsystem`` field sits at the IDENTICAL byte offset (68 bytes
    into the optional header, i.e. ``e_lfanew + 4 (PE signature) + 20
    (IMAGE_FILE_HEADER) + 68``) in both the 32-bit
    (``IMAGE_OPTIONAL_HEADER32``) and 64-bit (``..._HEADER64``) shapes —
    the 64-bit header drops the 4-byte ``BaseOfData`` field but widens
    ``ImageBase`` from 4 to 8 bytes, so those two 4-byte deltas cancel out
    before reaching ``Subsystem`` either way."""
    import struct  # noqa: PLC0415 — same "only needed here" convention as `_bitmap_to_bmp_bytes`

    try:
        with open(path, "rb") as f:
            dos_header = f.read(64)
            if len(dos_header) < 64 or dos_header[:2] != b"MZ":
                return None
            pe_offset = struct.unpack_from("<I", dos_header, 60)[0]
            f.seek(pe_offset)
            if f.read(4) != b"PE\x00\x00":
                return None
            f.seek(pe_offset + 4 + 20 + 68)
            subsystem_bytes = f.read(2)
            if len(subsystem_bytes) < 2:
                return None
            return struct.unpack("<H", subsystem_bytes)[0]
    except (OSError, ValueError):
        # `OSError` covers "doesn't exist"/"not readable"; `ValueError`
        # covers `open()` itself rejecting *path* (e.g. an embedded NUL
        # byte, or a name this host's filesystem encoding can't
        # represent) — both are "couldn't tell", matching the docstring's
        # claim that every failure mode here collapses to that rather
        # than raising (#3640 review nit).
        return None


def _detect_console_subsystem(command: str, cwd: str) -> bool | None:
    """``True`` when *command*'s own ``.exe`` is a console-subsystem (CUI)
    executable (#3640) — ``False`` for a GUI-subsystem exe, ``None`` when
    no ``.exe`` token could be found in *command* at all, or the one found
    can't be read/parsed as a PE image from here (missing file, wrong
    magic, a path this host can't yet resolve, ...). ``None`` is a
    "couldn't tell", not a verdict — :class:`WinNativeSession`'s own auto
    mode selection falls back to the pre-#3640 ``mode="window"`` behavior
    on it, so a GUI lane (or any lane this sniff can't read) is never
    affected by this at all."""
    guessed = _guess_exe_path(command, cwd)
    if guessed is None:
        return None
    subsystem = _pe_subsystem(guessed)
    if subsystem is None:
        return None
    return subsystem == IMAGE_SUBSYSTEM_WINDOWS_CUI


# ── top-level entry point ───────────────────────────────────────────────────

class WinNativeSession:
    """Persistent win-native ``coord app-drive`` session (#3590) — mirrors
    :class:`coord.tui_pty_driver.TuiPtySession`'s "sanctioned driver, one
    verb per call" shape: drives the exact same :class:`WinCalls` real-OS
    implementation :class:`NativeRunner`'s own ``launch``/``key``/``click``/
    ``capture`` step handlers use (same PID-only ``kill``, never by image
    name), just invoked on demand rather than against one fixed step list.

    Callers (:mod:`coord.app_drive`'s daemon dispatch) MUST confirm
    :meth:`WinCalls.session_available` themselves BEFORE constructing this
    — exactly like :meth:`NativeRunner.run` only reaches its own per-step
    handlers after that same check; this class assumes an
    already-confirmed-available session.
    """

    def __init__(
        self, launch_command: str, cwd: str, *, width: int = 1024, height: int = 768,
        calls: WinCalls | None = None, timeout_s: float = 10.0,
        mode: str | None = None, terminal_app: str = "",
    ) -> None:
        """*mode* (#3640) selects how *launch_command* is started:
        ``"window"`` launches it directly and waits for a top-level window
        owned by ITS OWN pid tree — the original, GUI-app behavior;
        ``"terminal"`` routes through :meth:`WinCalls.launch_in_terminal`
        instead, required for a console-subsystem exe whose window is
        never its own (it belongs to the conhost.exe/Windows Terminal
        hosting it) — the ``"window"`` path waits the full *timeout_s* and
        then fails for one of these, it never hangs past it, but it can
        also never succeed for one either. ``None`` (the default) is
        neither of those — it means "figure it out":
        :func:`_detect_console_subsystem` sniffs *launch_command*'s own
        ``.exe`` PE header and resolves to ``"terminal"`` only when that
        sniff positively identifies a CUI (console) image; every other
        outcome (a GUI exe, no ``.exe`` token found, the file unreadable
        from here) resolves to ``"window"`` — the exact pre-#3640
        behavior, so a GUI lane is never affected by this. *terminal_app*
        is required whenever the resolved mode is ``"terminal"``; when
        auto-detection itself picked ``"terminal"``, it defaults to
        ``"windows-terminal"`` unless *terminal_app* was already given.

        Raises :class:`WinNativeSpecError` for an invalid *mode*/
        *terminal_app* combination — BEFORE anything is launched, so
        there is nothing for this constructor's own teardown-on-failure
        (below) to need to clean up for that case."""
        self._calls: WinCalls = calls if calls is not None else Win32Calls()
        resolved_mode = mode
        resolved_terminal_app = terminal_app
        if resolved_mode is None:
            if _detect_console_subsystem(launch_command, cwd):
                resolved_mode = "terminal"
                if not resolved_terminal_app:
                    resolved_terminal_app = "windows-terminal"
            else:
                resolved_mode = "window"
        if resolved_mode not in _VALID_MODES:
            raise WinNativeSpecError(
                f"mode must be one of {', '.join(_VALID_MODES)}, got {resolved_mode!r}"
            )
        if resolved_mode == "terminal":
            if not resolved_terminal_app:
                raise WinNativeSpecError(
                    "mode='terminal' requires terminal_app ('windows-terminal' or 'conhost')"
                )
            if resolved_terminal_app not in _VALID_TERMINAL_APPS:
                raise WinNativeSpecError(
                    f"unrecognized terminal_app {resolved_terminal_app!r} — expected one "
                    f"of {', '.join(_VALID_TERMINAL_APPS)}"
                )
            self._pid = self._calls.launch_in_terminal(launch_command, cwd, resolved_terminal_app)
        else:
            self._pid = self._calls.launch(launch_command, cwd)
        try:
            self._hwnd = self._calls.find_top_window(self._pid, timeout_s)
            self._calls.move_window(self._hwnd, 0, 0, width, height)
        except Exception:
            # #3634: this constructor never returns a usable session when
            # `find_top_window`/`move_window` fails, so there is no
            # `WinNativeSession` object for anyone to ever call
            # `close()`/`kill()` on — without this, the process `launch`
            # just started (and its real-app grandchild, #3542) leaked on
            # every failed/retried open, exactly the 2026-10-05 bugbash
            # evidence (4 simultaneous stray `vimcode.exe`). Kill it HERE,
            # before re-raising, so a failed open — and every retry of
            # one — cleans up after itself instead of accumulating one
            # more stray window per attempt.
            try:
                self._calls.kill(self._pid)
            except Exception:  # noqa: BLE001 — teardown-on-failure must not mask the real error
                pass
            raise

    @property
    def pid(self) -> int:
        """The pid :meth:`WinCalls.launch` returned (#3590) — read by
        :mod:`coord.app_drive_daemon` for its ready-file's ``app_pid`` so
        :func:`coord.app_drive.close_session` can re-observe/re-signal the
        process this session itself launched, not just the daemon.

        See :meth:`_descendant_pids`'s own docstring (#3542): this is
        ``cmd.exe``'s pid, not the real app's — ``Win32Calls.launch``'s
        ``shell=True`` makes the real app a grandchild this pid never
        owns a window for. :meth:`WinCalls.kill` (#3634) now always
        terminates the whole descendant tree for whatever pid it's given,
        so killing this one pid alone no longer leaves that grandchild
        running."""
        return self._pid

    @property
    def staging_warning(self) -> str | None:
        """#3617 review: non-``None`` when launch staging onto the local
        Windows filesystem was skipped for THIS session (and why) — read
        by :mod:`coord.app_drive_daemon` for its ready-file, and from
        there :class:`coord.app_drive.SessionHandle`, so a silent
        fallback to the slow ``\\\\wsl$`` launch is observable
        end-to-end rather than only visible as a `find_top_window`
        timeout much later. ``None`` on a same-host, non-UNC ``cwd``
        (staging never applies) and also ``None`` on a UNC ``cwd`` when
        staging DID engage successfully. ``getattr``-based — only
        :class:`Win32Calls` actually sets ``staging_warning`` on itself; a
        scripted test fake simply has none."""
        return getattr(self._calls, "staging_warning", None)

    def send_key(self, key: str) -> None:
        self._calls.send_key(self._hwnd, key)

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        self._calls.send_click(self._hwnd, x, y, button)

    def capture(self) -> bytes:
        return self._calls.capture(self._hwnd)

    def probe(self, name: str, **kwargs: Any) -> Any:
        if name == "uia_elements":
            return self._calls.uia_elements(self._hwnd)
        if name == "is_window_alive":
            return self._calls.is_window_alive(self._hwnd)
        if name == "get_menu_items":
            return self._calls.get_menu_items(self._hwnd)
        if name == "hit_test":
            return self._calls.hit_test(self._hwnd, int(kwargs.get("x", 0)), int(kwargs.get("y", 0)))
        if name == "find_a11y":
            elements = self._calls.uia_elements(self._hwnd)
            match = _find_a11y_match(elements, kwargs.get("role", ""), kwargs.get("name", ""))
            return {
                "found": match is not None, "element": match,
                "tree": _summarize_elements(elements),
            }
        raise ValueError(
            f"unknown probe {name!r} — expected one of: uia_elements, "
            "is_window_alive, get_menu_items, hit_test, find_a11y"
        )

    def is_alive(self) -> bool:
        return self._calls.is_window_alive(self._hwnd)

    def close(self) -> None:
        """Kills only the PID this session itself launched (see the module
        docstring's safety note)."""
        try:
            self._calls.kill(self._pid)
        except Exception:  # noqa: BLE001 — teardown must not raise
            pass


def run_native_spec(
    spec_text: str, *, launch_command: str, cwd: str,
    calls: WinCalls | None = None, timeout: float | None = None,
) -> list[dict]:
    """Parse *spec_text* and run it against *calls* (a real
    :class:`Win32Calls` by default) launching *launch_command* in *cwd* —
    the top-level entry point
    :func:`coord.acceptance_drivers._run_win_native` calls.

    *timeout*, when given, is an overall wall-clock budget in seconds for
    the whole spec — mirrors :func:`coord.tui_pty_driver.run_smoke_spec`'s
    own ``timeout``: each step already carries its own bounded per-step
    budget, but their sum can still exceed it, in which case every
    remaining step fails explicitly rather than the run truncating or
    blocking past it.

    Raises :class:`WinNativeSpecError` for a malformed spec. Runtime
    failures (the process never launches, a window never appears, an
    assertion fails) do NOT raise — they're folded into the returned list
    as a ``status="fail"`` entry, the same "partial results, not a crash"
    contract :func:`coord.tui_pty_driver.run_smoke_spec` already gives.

    When *calls* reports a locked or absent interactive session
    (:meth:`WinCalls.session_available`, #3510), the returned list is a
    single ``status="unavailable"`` entry and no step runs — a distinct
    verdict from ``"fail"``, since a locked desktop is an environment
    condition, not an app bug.
    """
    spec = parse_native_spec(spec_text)
    resolved_calls = calls if calls is not None else Win32Calls()
    deadline = time.monotonic() + timeout if timeout is not None else None
    runner = NativeRunner(resolved_calls, launch_command, cwd, deadline=deadline)
    return runner.run(spec)
