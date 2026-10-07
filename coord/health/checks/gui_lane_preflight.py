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

This probe asks exactly the question each driver's own precheck asks —
calling the SAME `session_available()`/`ax_trust_available()` methods the
real `MacOSCalls`/`Win32Calls`/`LinuxGtkCalls` implementations already use
(#2096 "one question, one answer": a second, hand-rolled lock/AX check here
would risk silently disagreeing with the driver's own verdict) — on THIS
host, for each GUI lane capability this machine's own `coordinator.yml`
entry declares (`macos` -> `mac-native`, `windows` -> `win-native`, `gtk` ->
`gtk-native`, the same mapping `coord.acceptance_drivers.
LANE_CAPABILITY_EXTRAS` already owns).

A host with no GUI capability declared reports nothing at all (`None`) —
never a false "clean" for a capability this host never claimed, mirroring
every other probe's "absence of the thing to check is not a finding"
convention in this package.

A failing lane is reported CRIT with the exact remedy named in `detail` —
never a bare "unavailable" an operator has to go spelunking for — so `coord
doctor`'s per-host report (`coord/commands/status.py::_gui_lane_preflight_
lines`) can print the fix text verbatim rather than just a locked/dead
verdict.
"""

from __future__ import annotations

from typing import Callable

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
    """Resolve *capability*'s `*Calls()` constructor by looking up this
    MODULE'S CURRENT attribute (``globals()``), not a reference captured at
    import time. A dict built once at module load (``{"macos":
    _mac_calls_factory, ...}``) would freeze in the ORIGINAL function
    object, so `monkeypatch.setattr(gui_lane_preflight, "_mac_calls_factory",
    fake)` — the standard seam every sibling probe in this package uses —
    would silently have no effect. Re-reading `globals()` on every call is
    what makes that seam actually work here."""
    return globals()[_CALLS_FACTORY_NAME[capability]]


# Exact, host-specific remedies (#3651's "report as INFRA, with the fix
# named" requirement) — matched on a substring of the driver's own
# `session_available()`/`ax_trust_available()` reason, so the fix text names
# the SPECIFIC thing that's wrong rather than a generic "fix your host" line.
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
_GTK_FIX = (
    "neither $DISPLAY nor $WAYLAND_DISPLAY is set — start (or reattach to) "
    "the persistent Xvfb/Wayland session this host's gtk-native lane "
    "depends on"
)


def _fix_for(lane: str, reason: str) -> str:
    reason_l = reason.lower()
    if lane == "mac-native":
        for needle, fix in _MAC_FIXES:
            if needle in reason_l:
                return fix
    elif lane == "win-native":
        for needle, fix in _WIN_FIXES:
            if needle in reason_l:
                return fix
    elif lane == "gtk-native":
        return _GTK_FIX
    # No table entry matched (a reason string the driver hasn't been seen
    # to produce yet) — never fabricate a specific fix for an unrecognized
    # reason; echo it back so an operator still has something to act on.
    return f"unlock/attach this host's GUI session, then re-run ({reason})"


def _crit(lane: str, *, headroom: str, detail: str) -> CheckResult:
    return CheckResult(
        check_id=CHECK_ID,
        scope="machine",
        subject=lane,
        severity=Severity.CRIT,
        headroom=f"INFRA: {headroom}",
        threshold="crit when the pre-flight session/display/permission check fails",
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
            trusted, trust_reason = calls.ax_trust_available()
        except Exception as exc:  # noqa: BLE001 — ditto
            return _crit(
                lane,
                headroom=f"ax_trust_available() probe raised: {exc}",
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
        headroom="ready — display/session unlocked, permission present",
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
        "host is caught before dispatch, not after."
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
