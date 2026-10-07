"""Pre-flight GUI-lane readiness, per GUI-capable host (#3651).

**The incident.** macmini's `displaysleep 10` plus an immediate
`screenLock delay` means ten minutes after an operator leaves, the screen
locks and every `mac-native` smoke step starts failing — indistinguishable,
to everything downstream, from an app bug. The one-time Accessibility (AX)
trust grant can also sit unanswered behind a hidden prompt, which fails the
exact same way. dell64's `win-native`/Windows-Terminal runs need its
Windows desktop session unlocked, or every step fails the same
indistinguishable way.

`coord.mac_native_driver.NativeRunner.run` / `coord.win_native_driver.
NativeRunner.run` / `coord.gtk_native_driver.NativeRunner.run` already solve
half of this (#3510/#3566): each checks the real session/display (and, for
mac, `AXIsProcessTrusted()`) BEFORE any spec step runs, and reports a
distinct `status="unavailable"` verdict rather than an ordinary `"fail"` —
see `coord.release_gate`'s module docstring for how that distinction stays
visible all the way to the release gate. What that leaves missing is an
ANSWER BEFORE DISPATCH: "can the GUI lane run tonight?", one command, for
every GUI-capable host — so a dead run is caught at `coord doctor` time,
not discovered after a worker/smoke leg was already spent on it.

This probe asks exactly the question each driver's own precheck asks for
the display/session half — calling the SAME `session_available()` method
the real `MacOSCalls`/`Win32Calls`/`LinuxGtkCalls` implementations already
use (#2096 "one question, one answer": a second, hand-rolled lock check
here would risk silently disagreeing with the driver's own verdict) — on
THIS host, for each GUI lane capability this machine's own `coordinator.yml`
entry declares (`macos` -> `mac-native`, `windows` -> `win-native`, `gtk` ->
`gtk-native`, the same mapping `coord.acceptance_drivers.
LANE_CAPABILITY_EXTRAS` already owns).

The AX-trust half of the `macos` lane is NOT asked by calling
`MacOSCalls.ax_trust_available()` in-process. `AXIsProcessTrusted()` is a
per-PROCESS-IDENTITY question (`coord/prereqs.py`'s module comment above
`_AX_TRUST_SCRIPT`, written for #3566): asking it from the long-lived
`coord agent` process — which is exactly what calling it from inside this
registry would do, since `/health` runs `run_all()` in that same process
(`coord/agent.py::_cached_local_health`) — answers "does the AGENT have
trust", not "does a dispatched WORKER have trust", and #3566's own incident
was precisely agent-trusted/worker-untrusted. `/health` already answers
"does this mac have AX trust" correctly, for the right identity, via the
`macos-accessibility-trust` prereq (`coord/prereqs.py::
_probe_macos_accessibility_trust`, a fresh `sys.executable` subprocess —
never the agent's own cached TCC state). This probe reuses that EXACT same
seam (`_ax_trust_probe` below just calls it) rather than re-asking the
question a second, independently-implemented way — so a single `coord
doctor` run can never print this check's OK next to that prereq's FAIL for
the same host.

A host with no GUI capability declared reports nothing at all (`None`) —
never a false "clean" for a capability this host never claimed, mirroring
every other probe's "absence of the thing to check is not a finding"
convention in this package.

A failing lane is reported CRIT with the exact remedy named in `detail` —
never a bare "unavailable" an operator has to go spelunking for — so `coord
doctor`'s per-host report (`coord/commands/status.py::_gui_lane_preflight_
lines`) can print the fix text verbatim rather than just a locked/dead
verdict.

Caveats worth knowing before reading an OK row as a guarantee:

* The OK `headroom` claims only what was actually observed for that lane
  (see `_READY_HEADROOM` below) — `gtk-native`'s only observation is
  whether `$DISPLAY`/`$WAYLAND_DISPLAY` is *set*, not that a live
  Xvfb/Wayland session answers on it, and no lane observes the display is
  *awake* (vs. merely unlocked) the way `displaysleep` can blank it without
  tripping `CGSSessionScreenIsLocked`.
* `/health` serves this from a cache refreshed on a timer
  (`COORD_AGENT_HEALTH_INTERVAL`, default 300s) — an OK row can be up to
  five minutes stale against a host whose screen lock just fired.
"""

from __future__ import annotations

from collections.abc import Callable

from coord.acceptance_drivers import LANE_CAPABILITY_EXTRAS
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check

CHECK_ID = "gui_lane_preflight"


def _mac_calls_factory() -> object:
    from coord.mac_native_driver import MacOSCalls  # noqa: PLC0415

    return MacOSCalls()


def _win_calls_factory() -> object:
    from coord.win_native_driver import Win32Calls  # noqa: PLC0415

    return Win32Calls()


def _gtk_calls_factory() -> object:
    from coord.gtk_native_driver import LinuxGtkCalls  # noqa: PLC0415

    return LinuxGtkCalls()


# capability (coordinator.yml `capabilities:` entry) -> the module-level
# factory's OWN NAME (not the function object — see `_factory_for` below for
# why that indirection matters) that constructs the real `*Calls()` for it.
_CALLS_FACTORY_NAME: dict[str, str] = {
    "macos": "_mac_calls_factory",
    "windows": "_win_calls_factory",
    "gtk": "_gtk_calls_factory",
}


def _factory_for(capability: str) -> Callable[[], object]:
    """Resolve *capability*'s `*Calls()` constructor via `globals()` (not a
    dict of function objects frozen at import time) so the standard
    `monkeypatch.setattr(gui_lane_preflight, "_mac_calls_factory", fake)`
    seam actually takes effect."""
    return globals()[_CALLS_FACTORY_NAME[capability]]


# Exact, host-specific remedies (#3651's "report as INFRA, with the fix
# named" requirement) — matched on a substring of the driver's own
# `session_available()` reason, so the fix text names the SPECIFIC thing
# that's wrong rather than a generic "fix your host" line. `_MAC_AX_FIX`
# below is separate: it's used directly (not substring-matched), since
# `_ax_trust_probe`'s one failure mode only has one fix.
_MAC_FIXES: tuple[tuple[str, str], ...] = (
    (
        "no gui session is active",
        "no display/GUI session at all — if this mac is headless, plug in "
        "a dummy HDMI adapter (headless Macs blank the GPU with nothing "
        "attached, which reads identically to no session); otherwise log "
        "in on the console",
    ),
    (
        "screen is locked",
        "the screen is locked — unlock it, then keep it unlocked for the "
        "run window with `caffeinate -d` (or disable display-sleep/screen "
        "lock in System Settings -> Lock Screen) rather than relying on "
        "the operator to babysit it",
    ),
    (
        "not on the console",
        "fast-user-switched away from the console — switch back to this "
        "session on the console (a background/non-console session cannot "
        "drive the GUI)",
    ),
)
_MAC_AX_FIX = (
    "Accessibility (AX) trust is not granted to this process identity — "
    "grant it once in System Settings -> Privacy & Security -> "
    "Accessibility, then relaunch the agent (#3566)"
)
_WIN_FIXES: tuple[tuple[str, str], ...] = (
    (
        "openinputdesktop failed",
        "no interactive input desktop — this session is locked or "
        "non-interactive; configure auto-logon and disable the lock "
        "screen for dell64's run window, then unlock the session",
    ),
    (
        "no active console",
        "no active console session — log in on the console (a service-"
        "only session cannot drive the GUI)",
    ),
    (
        "logonui.exe is running",
        "the Windows desktop is locked (LogonUI.exe) — unlock it, and "
        "configure auto-logon / disable the lock screen for the run "
        "window so it does not lock again",
    ),
)
_GTK_FIXES: tuple[tuple[str, str], ...] = (
    (
        "$display",
        "neither $DISPLAY nor $WAYLAND_DISPLAY is set — start (or reattach "
        "to) the persistent Xvfb/Wayland session this host's gtk-native "
        "lane depends on",
    ),
)


def _fix_for(lane: str, reason: str) -> str:
    reason_l = reason.lower()
    if lane == "mac-native":
        table = _MAC_FIXES
    elif lane == "win-native":
        table = _WIN_FIXES
    elif lane == "gtk-native":
        table = _GTK_FIXES
    else:
        table = ()
    for needle, fix in table:
        if needle in reason_l:
            return fix
    # No table entry matched (a reason string the driver hasn't been seen
    # to produce yet) — never fabricate a specific fix for an unrecognized
    # reason, not even on gtk where today there's only one table entry;
    # echo it back so an operator still has something to act on.
    return f"unlock/attach this host's GUI session, then re-run ({reason})"


# Shared by both the CRIT and OK results so a `coord health --json` reader
# sees the grading rule on every row, not just the failing ones (nit raised
# in #3651 review round 1).
_THRESHOLD = "crit when the pre-flight session/display/permission check fails"

# What each lane's OK result actually observed — claim only that, never more
# (#3651 review round 1 non-blocking finding 1). `gtk-native` in particular
# only confirms an env var is *set*, not that a live Xvfb/Wayland session
# answers on it, and no lane confirms the display is *awake* vs. merely
# unlocked.
_READY_HEADROOM: dict[str, str] = {
    "mac-native": (
        "ready — display/session unlocked, AX trust present for the "
        "identity the driver runs as"
    ),
    "win-native": "ready — desktop session unlocked",
    "gtk-native": (
        "ready — $DISPLAY or $WAYLAND_DISPLAY is set (not a liveness check "
        "of the session behind it)"
    ),
}


def _ax_trust_probe() -> tuple[bool, str]:
    """Answer "is AX trust granted to the identity the mac-native driver
    runs as" by calling the SAME subprocess-based seam the `/health`
    `macos-accessibility-trust` prereq already uses
    (`coord.prereqs._probe_macos_accessibility_trust`) — a fresh
    `sys.executable` subprocess, never `MacOSCalls.ax_trust_available()`
    in-process, which would ask the long-lived `coord agent` process's own
    identity rather than a dispatched worker's (#3566; see this module's
    docstring for why asking it from here is the wrong seam). Returns
    ``(trusted, reason)``, mirroring `MacOSCalls.ax_trust_available()`'s own
    shape so `_lane_result` doesn't need two different result shapes."""
    from coord.prereqs import (  # noqa: PLC0415
        CAPABILITY_PREREQS,
        DEFAULT_PROBE_TIMEOUT,
        _probe_macos_accessibility_trust,
    )

    prereq = next(p for p in CAPABILITY_PREREQS if p.tool == "macos-accessibility-trust")
    result = _probe_macos_accessibility_trust(prereq, DEFAULT_PROBE_TIMEOUT)
    if result.found:
        return True, ""
    return False, result.what_breaks


def _crit(lane: str, *, headroom: str, detail: str) -> CheckResult:
    return CheckResult(
        check_id=CHECK_ID,
        scope="machine",
        subject=lane,
        severity=Severity.CRIT,
        headroom=f"INFRA: {headroom}",
        threshold=_THRESHOLD,
        detail=detail,
    )


def _lane_result(capability: str, lane: str) -> CheckResult:
    factory = _factory_for(capability)
    try:
        calls = factory()
    except Exception as exc:  # noqa: BLE001 — a probe failure IS the verdict
        return _crit(
            lane,
            headroom=f"could not construct the {lane} driver's own calls: {exc}",
            detail=(
                f"the {lane} driver failed to initialize on this host — "
                "see the exact error above; this is a host-provisioning "
                "problem, never an app bug"
            ),
        )

    try:
        available, reason = calls.session_available()
    except Exception as exc:  # noqa: BLE001 — ditto
        return _crit(
            lane,
            headroom=f"session_available() probe raised: {exc}",
            detail=_fix_for(lane, str(exc)),
        )
    if not available:
        return _crit(lane, headroom=reason or "no usable GUI session", detail=_fix_for(lane, reason))

    if capability == "macos":
        try:
            trusted, trust_reason = _ax_trust_probe()
        except Exception as exc:  # noqa: BLE001 — ditto
            return _crit(
                lane,
                headroom=f"AX trust probe raised: {exc}",
                detail=_MAC_AX_FIX,
            )
        if not trusted:
            return _crit(
                lane,
                headroom=trust_reason or "AXIsProcessTrusted() is False",
                detail=_MAC_AX_FIX,
            )

    return CheckResult(
        check_id=CHECK_ID,
        scope="machine",
        subject=lane,
        severity=Severity.OK,
        headroom=_READY_HEADROOM.get(lane, "ready"),
        threshold=_THRESHOLD,
    )


@check(
    id=CHECK_ID,
    scope="machine",
    title="GUI lane pre-flight",
    order=60,
    description=(
        "Display/session unlocked and AX/UIA permission present for this "
        "host's GUI acceptance lane(s) (#3651) — the same precheck the "
        "real driver runs before any spec step, asked up front so a dead "
        "host is caught before dispatch, not after. Served from /health's "
        "cache (default 300s TTL), so an OK row can be up to five minutes "
        "stale against a host whose screen just locked."
    ),
)
def probe_gui_lane_preflight(ctx: HealthContext) -> list[CheckResult] | None:
    from coord.config import resolve_local_machine  # noqa: PLC0415

    config = ctx.config
    if config is None or not getattr(config, "machines", None):
        return None
    machine = resolve_local_machine(config)
    if machine is None:
        return None

    capabilities = set(machine.capabilities or ())
    gui_capabilities = sorted(cap for cap in capabilities if cap in _CALLS_FACTORY_NAME)
    if not gui_capabilities:
        # Not a GUI-capable host at all -- nothing to report, same
        # "absence is not a finding" convention as every sibling probe.
        return None

    return [
        _lane_result(cap, LANE_CAPABILITY_EXTRAS[cap]) for cap in gui_capabilities
    ]
