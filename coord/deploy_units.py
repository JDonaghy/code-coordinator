"""The `deploy/**` lane's missing *deploy step* (#1831, wired up by #1835).

#1831 gave `deploy/**` a **detector** — ``coord.health.checks.unit_drift``
diffs each host's installed unit under ``~/.config/systemd/user/`` against
the units packaged in the wheel (``coord/deploy/``, #1927) — and a printed
remedy that a human then runs by hand. That was the right first half. It is
not a deploy lane: cutting v0.4.106 still meant installing five files into
``~/.config/systemd/user/`` and ``~/.local/bin/`` on dellserver, plus a
``daemon-reload``, plus retiring a machine-local drop-in.

#1835 cannot claim "the fleet reaches that version" while a whole lane needs
a human with ``cp`` and ``systemctl``, and this is not a corner case: #1543's
actual mechanism was three unit files and a shell script, while its Python
change was a single ``--dist`` flag. A release that propagated only the
Python lane would have shipped the flag and none of the behaviour.

So this module applies what ``unit_drift`` reports.

FOUR SAFETY PROPERTIES, ALL DELIBERATE
---------------------------------------
1. **Refresh only units this host already runs.** Which services a host runs
   is a *topology* decision (``coordinator.yml``), not a release decision. A
   packaged unit with no installed counterpart is reported as ``new`` and
   left alone — installing ``coord-web.service`` onto a machine that never
   wanted a web server, because a release happened to contain the file, is a
   far worse failure than a human running one ``cp``. The report names them
   so the human action is visible rather than implicit.

2. **Templates are rendered, never copied verbatim (#1928).** Several units
   carry ``<MACHINE_NAME>`` / ``<PORT>`` placeholders. Copying one verbatim
   installs the placeholder as literal text and the unit then refuses to
   start — the exact hazard #1928 documented. Placeholders with no known
   substitution abort *that unit* (reported, not written); they never get
   guessed.

3. **The previous content is kept.** Every overwrite writes
   ``<name>.pre-<version>.bak`` next to the unit first, so the rollback for
   this lane is a file copy the operator can see and `diff`, not a re-run of
   an install script whose inputs have moved on.

4. **A masked unit is never touched (#2812).** ``systemctl --user mask``
   replaces a unit's own file with a symlink to ``/dev/null`` — the mask
   itself IS that file's content, unlike ``disable`` (which lives elsewhere,
   under ``wants/``, and leaves the unit's file alone). A content refresh
   that does not know this reads empty text through the symlink, sees it
   doesn't match the packaged content, and overwrites the mask — exactly how
   `coord-release-window.timer`, masked on purpose against #2607, got
   silently re-armed three times in one afternoon. :func:`_is_masked` checks
   before every write; a masked unit is reported (:data:`ACTION_MASKED`) and
   left completely alone, not backed up, not read through, not enabled.

The write itself is atomic per file (temp file + ``os.replace``), so a unit
is never observed half-written by a ``daemon-reload`` racing this.

Pure-ish by construction: every path is a parameter, so the whole thing is
testable against ``tmp_path`` with no systemd, no fleet, and no root.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Reuse #1831's own definitions rather than re-spelling them. Two
# definitions of "which files are units" or "what a placeholder looks like"
# would let the detector and the deployer disagree — and a deployer that
# disagrees with its detector reports clean while shipping nothing.
from coord.health.checks.unit_drift import (
    _KNOWN_PLACEHOLDER_VALUES,
    _PLACEHOLDER_RE,
    _SYSTEMD_USER_DIR,
    _UNIT_GLOBS,
    packaged_unit_dir,
)

# Same reuse rule as above, one module over: `timer_active` is the DETECTOR
# for "is this timer actually enabled" (#2082); `enable_timers` below is the
# FIXER for the identical question (#2124). Querying state through its own
# `_timer_states` — rather than a second `systemctl --user show` parser here
# — is what keeps those two answers from being able to drift apart.
from coord.health.checks.timer_active import _INACTIVE_STATES, _timer_states

#: Placeholder -> how to fill it, given the host facts we actually know.
#: ``unit_drift`` renders these as *shell* text for a copy-pasteable remedy
#: (``$(hostname -s)``); here they must be real values, so the mapping is
#: from placeholder name to the keyword of :func:`install_units`.
_PLACEHOLDER_SOURCES = {
    "MACHINE_NAME": "machine_name",
    "PORT": "port",
}

#: Outcome of one unit's deploy step.
ACTION_UNCHANGED = "unchanged"
ACTION_UPDATED = "updated"
ACTION_NEW = "new"
ACTION_SKIPPED = "skipped"
ACTION_FAILED = "failed"
#: An operator masked this unit (``systemctl --user mask``, a symlink to
#: ``/dev/null``) — content is left untouched, not overwritten (#2812). See
#: :func:`_is_masked`.
ACTION_MASKED = "masked"


@dataclass(frozen=True)
class UnitOutcome:
    name: str
    action: str
    detail: str = ""
    backup: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InstallReport:
    """What the deploy step did, per unit."""

    units: list[UnitOutcome] = field(default_factory=list)
    reference: str | None = None
    error: str | None = None
    #: True once at least one unit's bytes changed — the only case that
    #: needs a ``systemctl --user daemon-reload``.
    @property
    def changed(self) -> bool:
        return any(u.action == ACTION_UPDATED for u in self.units)

    @property
    def ok(self) -> bool:
        return self.error is None and not any(
            u.action == ACTION_FAILED for u in self.units
        )

    def to_dict(self) -> dict:
        return {
            "reference": self.reference,
            "error": self.error,
            "changed": self.changed,
            "ok": self.ok,
            "units": [u.to_dict() for u in self.units],
        }

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for unit in self.units:
            counts[unit.action] = counts.get(unit.action, 0) + 1
        if self.error:
            return f"units: {self.error}"
        if not counts:
            return "units: nothing packaged to install"
        return "units: " + ", ".join(
            f"{count} {action}" for action, count in sorted(counts.items())
        )


def systemd_user_dir(home: Path | None = None) -> Path:
    base = home or Path.home()
    return Path(str(_SYSTEMD_USER_DIR).replace("~", str(base), 1))


def _packaged_units(reference_dir: Path) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for pattern in _UNIT_GLOBS:
        for path in sorted(reference_dir.glob(pattern)):
            if path.name in seen:
                continue
            seen.add(path.name)
            out.append(path)
    return sorted(out, key=lambda p: p.name)


def render_unit(text: str, *, machine_name: str | None, port: int | str | None) -> tuple[str | None, str]:
    """Fill ``<PLACEHOLDER>`` tokens. Returns ``(rendered_or_None, note)``.

    ``None`` means "this unit is a template with a placeholder we cannot
    fill" — the caller must skip it and say so. Guessing a value here is how
    a unit lands with ``<MACHINE_NAME>`` as literal text and then refuses to
    start (#1928).
    """
    names = sorted(set(_PLACEHOLDER_RE.findall(text)))
    if not names:
        return text, ""
    values = {"machine_name": machine_name, "port": port}
    filled: dict[str, str] = {}
    for name in names:
        key = _PLACEHOLDER_SOURCES.get(name)
        value = values.get(key) if key else None
        if value in (None, ""):
            fallback = _KNOWN_PLACEHOLDER_VALUES.get(name)
            return None, (
                f"template placeholder <{name}> has no value for this host"
                + (f" (unit_drift's documented default is {fallback})" if fallback else "")
                + " — refusing to install it verbatim (#1928); install this "
                "unit by hand"
            )
        filled[name] = str(value)

    def _sub(match: re.Match[str]) -> str:
        return filled[match.group(1)]

    return _PLACEHOLDER_RE.sub(_sub, text), f"rendered {', '.join(names)}"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".coord-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _is_masked(path: Path) -> bool:
    """Is *path* a systemd mask symlink (#2812)?

    ``systemctl --user mask <unit>`` replaces the unit's own file with a
    symlink to ``/dev/null`` — the mask *is* the file's content, unlike
    ``disable`` (which only removes symlinks elsewhere, under ``wants/``,
    and leaves the unit's own file alone). That is exactly why a blind
    "refresh this file's bytes" used to defeat a mask silently: reading
    through the symlink returns empty text, which never equals the
    packaged content, so the old code treated it as drifted and happily
    ``os.replace()``-d real unit content over the ``/dev/null`` symlink —
    unmasking `coord-release-window.timer` as a side effect of a routine
    content refresh, three times in one afternoon.

    Filesystem-only, no ``systemctl`` call needed — matching this module's
    "pure filesystem operation" design (see the module docstring).
    """
    try:
        return path.is_symlink() and os.readlink(path) == os.devnull
    except OSError:
        return False


def install_units(
    *,
    target_dir: Path | None = None,
    reference_dir: Path | None = None,
    machine_name: str | None = None,
    port: int | str | None = None,
    version: str | None = None,
    dry_run: bool = False,
    home: Path | None = None,
) -> InstallReport:
    """Refresh this host's installed systemd user units from the wheel.

    *reference_dir* defaults to :func:`~coord.health.checks.unit_drift.
    packaged_unit_dir` — ``coord/deploy/`` inside the *installed*
    distribution, i.e. the released artifact, which cannot drift with the
    host's checkout (#1927). Only units already present in *target_dir* are
    rewritten; see the module docstring for why.
    """
    report = InstallReport()
    ref = reference_dir or packaged_unit_dir()
    if ref is None:
        report.error = (
            "this install ships no coord/deploy/ — it predates #1927, so "
            "there is no released unit set to deploy from. Upgrade the "
            "Python lane first."
        )
        return report
    report.reference = str(ref)

    dest_dir = target_dir or systemd_user_dir(home)
    suffix = f".pre-{version}" if version else ".pre-update"

    for source in _packaged_units(ref):
        installed = dest_dir / source.name
        if not installed.exists():
            report.units.append(
                UnitOutcome(
                    source.name,
                    ACTION_NEW,
                    "packaged but not installed on this host — a release does "
                    "not decide which services a host runs; install and enable "
                    "it by hand if this host should have it",
                )
            )
            continue

        if _is_masked(installed):
            # An operator's explicit "never run this" (#2812) — stronger
            # than the #2124 stopped-timer carve-out below, and content
            # cannot be refreshed here without destroying the very mask
            # that makes it a signal at all (see `_is_masked`). Leave the
            # symlink exactly as it is: no backup, no write, no read
            # through it. Reported as its own action so `enable_timers`
            # (which only ever sees units in `_PRESENT_ACTIONS`) never gets
            # a chance to try re-enabling it either.
            report.units.append(
                UnitOutcome(
                    source.name,
                    ACTION_MASKED,
                    "masked by an operator (systemctl --user mask) — left "
                    "masked, content not refreshed (#2812); "
                    "`systemctl --user unmask " + source.name + "` if this "
                    "unit should run again, then re-run propagate",
                )
            )
            continue

        try:
            source_text = source.read_text(encoding="utf-8")
        except OSError as exc:
            report.units.append(
                UnitOutcome(source.name, ACTION_FAILED, f"unreadable reference: {exc}")
            )
            continue

        rendered, note = render_unit(
            source_text, machine_name=machine_name, port=port
        )
        if rendered is None:
            report.units.append(UnitOutcome(source.name, ACTION_SKIPPED, note))
            continue

        try:
            current = installed.read_text(encoding="utf-8")
        except OSError as exc:
            report.units.append(
                UnitOutcome(source.name, ACTION_FAILED, f"unreadable installed unit: {exc}")
            )
            continue

        if current == rendered:
            report.units.append(UnitOutcome(source.name, ACTION_UNCHANGED, note))
            continue

        if dry_run:
            report.units.append(
                UnitOutcome(source.name, ACTION_UPDATED, f"would rewrite ({note})".strip())
            )
            continue

        backup = installed.with_name(installed.name + suffix + ".bak")
        try:
            shutil.copy2(installed, backup)
            _atomic_write(installed, rendered)
        except OSError as exc:
            report.units.append(
                UnitOutcome(source.name, ACTION_FAILED, f"write failed: {exc}")
            )
            continue
        report.units.append(
            UnitOutcome(
                source.name,
                ACTION_UPDATED,
                note or "content refreshed from the packaged release",
                backup=str(backup),
            )
        )

    return report


def daemon_reload(*, runner=None, timeout: float = 30.0) -> tuple[bool, str]:
    """``systemctl --user daemon-reload``. Returns ``(ok, output)``.

    Split out and injectable so :func:`install_units` stays a pure filesystem
    operation testable without systemd — and so a host with no systemd (a
    macOS worker) degrades to a reported skip rather than a traceback.
    """
    import subprocess  # noqa: PLC0415

    run = runner or subprocess.run
    try:
        proc = run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "systemctl not found (no systemd on this host)"
    except Exception as exc:  # noqa: BLE001 — a reload must never crash a roll
        return False, f"{type(exc).__name__}: {exc}"
    ok = getattr(proc, "returncode", 1) == 0
    out = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
    return ok, out or ("daemon-reload ok" if ok else "daemon-reload failed")


#: Which of :func:`install_units`'s outcomes mean "this unit is actually
#: present on this host right now" — the only units eligible to be enabled
#: below. ``ACTION_NEW`` (never installed here — safety property 1: a
#: release does not decide which services a host runs) and
#: ``ACTION_SKIPPED``/``ACTION_FAILED`` (this write did not land) are
#: deliberately excluded.
_PRESENT_ACTIONS = frozenset({ACTION_UNCHANGED, ACTION_UPDATED})

#: ``UnitFileState`` values that mean "an operator explicitly told systemd
#: never to run this on its own until they say otherwise" (#2812) — strictly
#: stronger than the plain ``disabled``/``linked`` #2082 was about (nobody
#: ever *decided* a freshly-installed timer shouldn't run; it simply never
#: got its first ``enable``). Masking is systemd's own documented mechanism
#: for exactly the "block automation from re-enabling it" case, respected
#: here the same way #2124 respects an operator's ``stop``.
_MASKED_STATES = frozenset({"masked", "masked-runtime"})


def enable_timers(
    report: InstallReport, *, runner=None, timeout: float = 30.0,
) -> dict[str, tuple[bool, bool, str]]:
    """Assert every installed timer in *report* is *enabled* (#2082) —
    without forcing a *stopped* one back to running (#2124).

    #2082: ``coord-release-propagate.timer`` reached three hosts'
    ``~/.config/systemd/user/`` and sat there — :func:`install_units`
    refreshed its *content* on every release, but nothing ever ran
    ``systemctl --user enable`` on it, and nothing noticed because a
    disabled timer's file looks byte-for-byte identical to an active one's
    (see :mod:`coord.health.checks.timer_active`, the detector for exactly
    this). The fix that shipped for #2082 was ``enable --now`` on every
    installed timer, every deploy — and ``--now`` is the collision #2124
    reports: it also **starts** a timer an operator deliberately stopped,
    five minutes earlier, to create the very quiescence window the deploy
    calling this function is running inside of. The daemon host rolls
    first, so that operator's window closes mid-roll, not after it.

    #2082 and #2124 turn out to be two different questions systemd already
    answers separately, and this function's whole fix is asking the right
    one of them *first*:

    * **Persistent enablement** (``UnitFileState`` — ``enabled`` vs.
      ``disabled``/``linked``/``masked``, :data:`_INACTIVE_STATES`) is what
      #2082 was actually missing. It is a one-time fact that only becomes
      false again if something explicitly disables the unit — ``systemctl
      --user stop`` never touches it.
    * **Current run state** (``ActiveState``) is what an operator's
      deliberate ``stop`` changes, and it is *never* this function's business
      to override: a timer already carrying ``UnitFileState=enabled`` has
      already had its #2082 assertion satisfied by some earlier deploy (or
      this one), so nothing here re-starts it. ``enable`` alone is not even
      called in that case — no subprocess invocation touches the unit at
      all, so there is no risk of this idempotent-by-design call itself
      having a side effect an operator did not ask for.

    So: a timer whose queried ``UnitFileState`` is *not* one of
    :data:`_INACTIVE_STATES` is left completely alone, whatever its
    ``ActiveState`` — that is the acceptance case "a timer stopped by an
    operator is still down after a deploy". A timer that *is* in one of
    those states (never enabled at all) is the actual #2082 defect and gets
    ``enable --now`` exactly as before — there is no operator intent to
    preserve for a timer that has never run, and leaving it disabled is the
    invisible-self-heals-never failure #2082 closed.

    ``masked``/``masked-runtime`` (:data:`_MASKED_STATES`) is carved back out
    of that #2082 branch (#2812): unlike plain ``disabled`` — which nobody
    *decided*, it just never got its first ``enable`` — masking is always a
    deliberate operator act, and ``install_units`` already refuses to touch
    a masked unit's content for the identical reason (see
    :func:`_is_masked`). Reaching a live ``masked`` state here at all means
    either that guard didn't apply (this call was handed a report built some
    other way) or systemd's state changed between calls; either way,
    ``enable`` would simply be refused by systemd — attempting it anyway and
    reporting the refusal as a per-deploy failure is not "protecting" the
    mask, it is spamming an ``ok=False`` for a state that is exactly what
    the operator asked for. So this function does not even try: no
    subprocess call, so it can never be what silently un-masks the unit.

    State is queried once, in a single batched ``systemctl --user show``
    (:func:`coord.health.checks.timer_active._timer_states` — the same
    query the detector itself uses, so the detector and this fixer cannot
    silently disagree about what "enabled" means), scoped to units *this*
    report found actually installed (:data:`_PRESENT_ACTIONS`), matching the
    deploy step's own "only touch what this host already runs" rule.

    Returns ``{unit: (ok, changed, detail)}``. ``changed`` is true only when
    this call is confirmed to have moved the unit's state — never true for
    the "already enabled, left alone" branch (no subprocess call was made to
    confirm anything), and never true for a failed ``enable --now`` either
    (an attempt that failed is not a confirmed change) — the deploy's
    caller uses this to name, precisely, which timers it actually touched
    (#2124 item 3) rather than reporting a state it did not confirm.
    """
    import subprocess  # noqa: PLC0415

    run = runner or subprocess.run
    out: dict[str, tuple[bool, bool, str]] = {}
    timers = sorted(
        u.name
        for u in report.units
        if u.name.endswith(".timer") and u.action in _PRESENT_ACTIONS
    )
    if not timers:
        return out

    states = _timer_states(tuple(timers), runner=run, timeout=timeout)

    for name in timers:
        fields = states.get(name) or {}
        file_state = fields.get("UnitFileState", "")
        if file_state and file_state not in _INACTIVE_STATES:
            # Already enabled (#2082's assertion already holds for this
            # unit) — leave its run state exactly as it is. No systemctl
            # call at all, so the report below can only ever say what was
            # actually queried, never what this call assumed.
            active = fields.get("ActiveState") or "unknown"
            out[name] = (
                True,
                False,
                f"already enabled (ActiveState={active}) — left its current "
                "run state alone; a timer an operator stopped stays stopped "
                "across a deploy (#2124)",
            )
            continue

        if file_state in _MASKED_STATES:
            # An operator's explicit "never run this" (#2812) — strictly
            # stronger than the plain-disabled #2082 case below, and
            # `enable` is refused by systemd on a masked unit anyway. Not
            # attempting it (rather than trying and reporting the refusal
            # as a failure) is what actually respects the mask — see the
            # docstring above.
            active = fields.get("ActiveState") or "unknown"
            out[name] = (
                True,
                False,
                f"masked (ActiveState={active}) — left masked, not "
                "overriding an operator's explicit mask (#2812); unmask by "
                "hand (`systemctl --user unmask <unit>`) if this unit "
                "should run again",
            )
            continue

        # Never enabled at all — disabled/linked, or unreadable (no
        # systemd, or the batched query above failed and this unit simply
        # has no entry). This is the actual #2082 defect: no operator ever
        # ran `stop` on a timer that was never running, so forcing a start
        # here has no operator intent to override.
        try:
            proc = run(
                ["systemctl", "--user", "enable", "--now", name],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            out[name] = (False, False, "systemctl not found (no systemd on this host)")
            continue
        except Exception as exc:  # noqa: BLE001 — must never crash a deploy
            out[name] = (False, False, f"{type(exc).__name__}: {exc}")
            continue
        ok = getattr(proc, "returncode", 1) == 0
        detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
        out[name] = (ok, ok, detail or ("enabled" if ok else "enable failed"))
    return out
