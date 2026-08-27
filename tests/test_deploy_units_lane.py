"""The `deploy/**` lane's deploy step (#1831, wired up by #1835).

#1831 shipped a *detector* — `unit_drift` diffs each host's installed unit
against the units packaged in the wheel — and a remedy a human then typed.
#1835 cannot claim "the fleet reaches that version" while a whole lane needs
a human with `cp` and `systemctl`, so `coord/deploy_units.py` applies what
the detector reports.

Every test here defends one of the three safety properties that make an
*automatic* unit install acceptable at all:

1. **Only units this host already runs get refreshed.** Which services a
   host runs is a topology decision, not a release decision. Installing
   `coord-web.service` onto a machine that never wanted a web server, purely
   because a release contained the file, is worse than a human running `cp`.
2. **Templates are rendered, never copied verbatim (#1928).** A verbatim copy
   installs `<MACHINE_NAME>` as literal text and the unit then refuses to
   start.
3. **The previous content is kept**, so this lane's rollback is a file the
   operator can see and `diff`.

Nothing here needs systemd, a fleet, or root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coord import deploy_units as du

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def reference(tmp_path: Path) -> Path:
    """A stand-in for `coord/deploy/` inside an installed wheel."""
    ref = tmp_path / "packaged"
    ref.mkdir()
    (ref / "coord-serve.service").write_text("[Service]\nExecStart=new\n")
    (ref / "coord-agent.service").write_text(
        "[Service]\nExecStart=coord agent --machine <MACHINE_NAME> --port <PORT>\n"
    )
    (ref / "coord-web.service").write_text("[Service]\nExecStart=web\n")
    (ref / "coord-serve.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    # Not a unit — must be ignored by the glob, same as unit_drift.
    (ref / "coord-web-dist-build.sh").write_text("#!/bin/sh\n")
    return ref


@pytest.fixture()
def installed(tmp_path: Path) -> Path:
    dest = tmp_path / "systemd-user"
    dest.mkdir()
    (dest / "coord-serve.service").write_text("[Service]\nExecStart=old\n")
    (dest / "coord-agent.service").write_text(
        "[Service]\nExecStart=coord agent --machine dellserver --port 7433\n"
    )
    return dest


def _by_name(report: du.InstallReport) -> dict[str, du.UnitOutcome]:
    return {u.name: u for u in report.units}


# ── property 1: only refresh what this host already runs ─────────────────


def test_a_drifted_unit_is_refreshed(reference, installed):
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert report.ok
    outcome = _by_name(report)["coord-serve.service"]
    assert outcome.action == du.ACTION_UPDATED
    assert (installed / "coord-serve.service").read_text() == "[Service]\nExecStart=new\n"


def test_a_packaged_unit_this_host_does_not_run_is_never_installed(reference, installed):
    """A release must not decide which services a host runs."""
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    outcome = _by_name(report)["coord-web.service"]
    assert outcome.action == du.ACTION_NEW
    assert not (installed / "coord-web.service").exists()
    # ...and it is *reported*, not silently dropped, so the human action is
    # visible rather than implicit.
    assert "install and enable it by hand" in outcome.detail


def test_new_units_do_not_make_the_report_unhealthy(reference, installed):
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert report.ok


def test_an_already_current_unit_is_untouched(reference, installed):
    (installed / "coord-serve.service").write_text("[Service]\nExecStart=new\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert _by_name(report)["coord-serve.service"].action == du.ACTION_UNCHANGED


def test_nothing_changed_means_no_daemon_reload_needed(reference, installed):
    (installed / "coord-serve.service").write_text("[Service]\nExecStart=new\n")
    (installed / "coord-agent.service").write_text(
        "[Service]\nExecStart=coord agent --machine dellserver --port 7433\n"
    )
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert not report.changed


def test_non_unit_files_are_ignored(reference, installed):
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert "coord-web-dist-build.sh" not in _by_name(report)


# ── property 2: templates are rendered, never copied verbatim ────────────


def test_a_template_is_rendered_for_this_host(reference, installed):
    (installed / "coord-agent.service").write_text("[Service]\nExecStart=stale\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="macmini", port=7433)
    text = (installed / "coord-agent.service").read_text()
    assert "<MACHINE_NAME>" not in text
    assert "--machine macmini" in text
    assert "--port 7433" in text
    assert report.ok


def test_a_template_with_no_value_is_skipped_not_guessed(reference, installed):
    """#1928: copying it verbatim installs `<MACHINE_NAME>` as literal text
    and the unit refuses to start. Refusing loudly beats guessing."""
    (installed / "coord-agent.service").write_text("[Service]\nExecStart=stale\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name=None, port=7433)
    outcome = _by_name(report)["coord-agent.service"]
    assert outcome.action == du.ACTION_SKIPPED
    assert "<MACHINE_NAME>" in outcome.detail
    assert (installed / "coord-agent.service").read_text() == "[Service]\nExecStart=stale\n"
    # A skip is not a failure — the rest of the lane still deployed.
    assert report.ok


def test_render_unit_leaves_placeholderless_text_alone():
    text = "[Service]\nExecStart=x\n"
    rendered, note = du.render_unit(text, machine_name="a", port=1)
    assert rendered == text
    assert note == ""


def test_render_unit_refuses_an_unknown_placeholder():
    rendered, note = du.render_unit("x=<WHO_KNOWS>\n", machine_name="a", port=1)
    assert rendered is None
    assert "WHO_KNOWS" in note


# ── property 3: the previous content is kept ─────────────────────────────


def test_the_previous_unit_is_backed_up(reference, installed):
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433, version="0.4.111")
    outcome = _by_name(report)["coord-serve.service"]
    backup = Path(outcome.backup)
    assert backup.exists()
    assert backup.name == "coord-serve.service.pre-0.4.111.bak"
    assert backup.read_text() == "[Service]\nExecStart=old\n"


# ── masked units are left alone, not silently unmasked (#2812) ───────────
#
# `systemctl --user mask <unit>` replaces the unit's own file with a symlink
# to `/dev/null` — the mask IS the file's content. Before this fix,
# `install_units` read through that symlink (empty text), saw it didn't
# match the packaged content, and happily `os.replace()`-d real unit text
# over the mask — silently un-masking `coord-release-window.timer`, which
# had been masked on purpose to stop it re-arming a #2607 roll-pending
# marker. Three propagate runs in one afternoon each undid a by-hand
# re-mask.


def test_a_masked_unit_is_left_alone_not_overwritten(reference, installed):
    (installed / "coord-serve.service").unlink()
    (installed / "coord-serve.service").symlink_to("/dev/null")

    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)

    outcome = _by_name(report)["coord-serve.service"]
    assert outcome.action == du.ACTION_MASKED
    assert "masked" in outcome.detail
    # The symlink itself must survive byte-for-byte — no backup, no write.
    target = installed / "coord-serve.service"
    assert target.is_symlink()
    assert os.readlink(target) == "/dev/null"
    assert not list(installed.glob("*.bak"))


def test_a_masked_unit_does_not_make_the_report_unhealthy(reference, installed):
    (installed / "coord-serve.service").unlink()
    (installed / "coord-serve.service").symlink_to("/dev/null")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert report.ok
    assert not report.changed


def test_a_masked_timer_is_excluded_from_enable_timers_candidates(reference, installed):
    """The install-side guard alone is enough to keep `enable_timers` from
    ever trying to touch a masked timer — `ACTION_MASKED` is not one of
    `enable_timers`'s `_PRESENT_ACTIONS`, so a masked `.timer` never even
    reaches its candidate list, let alone a live `systemctl show`."""
    (installed / "coord-serve.timer").symlink_to("/dev/null")

    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert _by_name(report)["coord-serve.timer"].action == du.ACTION_MASKED

    fake = _FakeRun()
    result = du.enable_timers(report, runner=fake)
    assert result == {}
    assert fake.calls == []


# ── dry run ──────────────────────────────────────────────────────────────


def test_a_dry_run_writes_nothing(reference, installed):
    before = (installed / "coord-serve.service").read_text()
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433, dry_run=True)
    assert _by_name(report)["coord-serve.service"].action == du.ACTION_UPDATED
    assert (installed / "coord-serve.service").read_text() == before
    assert not list(installed.glob("*.bak"))


# ── enable_timers (#2082 + #2124) ──────────────────────────────────────────
#
# #2082: `coord-release-propagate.timer` reached three hosts' installed
# directory and sat there disabled for a day — `install_units` refreshed its
# CONTENT every release; nothing ever ran `systemctl --user enable` on it.
# The fix that shipped was `enable --now` on every *installed* `.timer`,
# every deploy, regardless of whether content changed this run.
#
# #2124: `--now` also **starts** a timer, and an operator who ran `systemctl
# --user stop coord-drive-queue.timer` to open a deploy window got it
# restarted by the very deploy running inside that window. `enable` and
# `--now` had to be pulled apart: persistent enablement (`UnitFileState`,
# untouched by `stop`) is what #2082 needed asserted; current run state
# (`ActiveState`, exactly what `stop` changes) is what #2124 says a deploy
# must never override.
#
# Both defects get tests in this one file, deliberately, so a future change
# to one cannot silently regress the other.


class _FakeRun:
    """Records every `systemctl` invocation `enable_timers` makes. Answers
    every call — including the `show` state query `enable_timers` issues
    first — with an empty, no-data result, i.e. "this test does not model
    prior state"; see `_FakeSystemd` below for tests that do."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))

        class _Proc:
            returncode = 0 if self.ok else 1
            stdout = ""
            stderr = "" if self.ok else "Failed to enable"

        return _Proc()


class _FakeSystemd:
    """A minimal in-memory systemd, keyed by unit name — just enough to
    answer `systemctl --user show` (the state query `enable_timers` makes
    first, #2124) and to actually apply an `enable`/`enable --now` call to
    its own state, the same two operations the real systemd exposes.

    Construct with each unit's starting `UnitFileState`/`ActiveState` — the
    two real, independent facts that separate the #2082 case (never
    enabled: `UnitFileState=disabled`) from the #2124 one (enabled, but an
    operator ran `stop`: `UnitFileState=enabled`, `ActiveState=inactive`).
    """

    def __init__(self, states: dict[str, dict[str, str]]):
        self.states = {name: dict(fields) for name, fields in states.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        if len(argv) > 2 and argv[2] == "show":
            names = [a for a in argv[3:] if not a.startswith("--")]
            blocks = []
            for name in names:
                fields = self.states.get(name, {})
                lines = [f"Id={name}"]
                lines += [f"{k}={v}" for k, v in fields.items()]
                blocks.append("\n".join(lines))
            _Proc.stdout = "\n\n".join(blocks)
        elif len(argv) > 2 and argv[2] == "enable":
            name = argv[-1]
            state = self.states.setdefault(name, {})
            state["UnitFileState"] = "enabled"
            if "--now" in argv:
                state["ActiveState"] = "active"
                state["SubState"] = "running"
        return _Proc()


def test_enable_timers_touches_every_installed_timer(reference, installed):
    """The dellserver repro: `coord-serve.timer` is installed (unchanged
    content) — it must still get `enable --now`, because enable state is
    independent of content and nothing else asserts it. `_FakeRun`'s state
    query answers "no data", which this function must treat the same as
    "never enabled" (#2082) — see `_FakeSystemd` tests below for the
    state-aware #2124 behaviour."""
    (installed / "coord-serve.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert _by_name(report)["coord-serve.timer"].action == du.ACTION_UNCHANGED

    fake = _FakeRun()
    result = du.enable_timers(report, runner=fake)
    assert result == {"coord-serve.timer": (True, True, "enabled")}
    # A state query precedes the actual enable call (#2124) — the point is
    # asking before acting, not acting blind the way #2082's fix did.
    assert fake.calls[0][:3] == ["systemctl", "--user", "show"]
    assert fake.calls[-1] == ["systemctl", "--user", "enable", "--now", "coord-serve.timer"]


def test_enable_timers_leaves_an_operator_stopped_timer_stopped(reference, installed):
    """#2124's exact shape: an operator ran `systemctl --user stop
    coord-serve.timer` to open a deploy window. `UnitFileState` stays
    `enabled` — `stop` never touches persistent enablement — so that is the
    signal this function must read, not `ActiveState`, to know this is not
    the #2082 case. The timer must come out of this call still inactive."""
    (installed / "coord-serve.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    fake = _FakeSystemd({
        "coord-serve.timer": {
            "UnitFileState": "enabled", "ActiveState": "inactive", "SubState": "dead",
        },
    })

    result = du.enable_timers(report, runner=fake)

    ok, changed, detail = result["coord-serve.timer"]
    assert ok
    assert not changed
    assert "already enabled" in detail
    assert "ActiveState=inactive" in detail
    # The load-bearing assertion: nothing this call did moved the timer out
    # of the state the operator put it in.
    assert fake.states["coord-serve.timer"]["ActiveState"] == "inactive"
    assert all(argv[2] != "enable" for argv in fake.calls)


def test_enable_timers_still_enables_a_never_enabled_timer(reference, installed):
    """#2082 regression guard, pinned in the same file as the #2124 test
    above so the two cannot silently drift apart: a timer that has never
    been enabled at all (`install_units` just wrote its content for the
    first time this deploy) still gets `enable --now` — there is no
    operator intent to preserve for a timer that has never run."""
    (installed / "coord-serve.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    fake = _FakeSystemd({
        "coord-serve.timer": {"UnitFileState": "disabled", "ActiveState": "inactive"},
    })

    result = du.enable_timers(report, runner=fake)

    ok, changed, detail = result["coord-serve.timer"]
    assert ok
    assert changed
    assert fake.states["coord-serve.timer"]["UnitFileState"] == "enabled"
    assert fake.states["coord-serve.timer"]["ActiveState"] == "active"
    assert ["systemctl", "--user", "enable", "--now", "coord-serve.timer"] in fake.calls


def test_enable_timers_leaves_a_masked_timer_masked(reference, installed):
    """#2812: `coord-release-window.timer` was masked deliberately (four
    `/dev/null` symlinks, dated and documented against #2607) and a
    propagate run re-enabled it anyway — three times in one afternoon. A
    live `masked` state must never reach `enable --now`: systemd refuses
    it, but attempting it reports the refusal as a per-deploy failure
    instead of respecting a signal stronger than the plain-`disabled`
    #2082 case (contrast `test_enable_timers_still_enables_a_never_enabled_
    timer` above, which must still fire for a unit nobody ever masked)."""
    (installed / "coord-serve.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    fake = _FakeSystemd({
        "coord-serve.timer": {"UnitFileState": "masked", "ActiveState": "inactive"},
    })

    result = du.enable_timers(report, runner=fake)

    ok, changed, detail = result["coord-serve.timer"]
    assert ok
    assert not changed
    assert "masked" in detail
    assert fake.states["coord-serve.timer"]["UnitFileState"] == "masked"
    assert all(argv[2] != "enable" for argv in fake.calls)


def test_enable_timers_leaves_a_masked_runtime_timer_masked(reference, installed):
    """`masked-runtime` (`systemctl --user mask --runtime`) is the same
    operator signal as a persistent mask — must not fall through to the
    #2082 enable branch just because its `UnitFileState` spelling differs.
    """
    (installed / "coord-serve.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    fake = _FakeSystemd({
        "coord-serve.timer": {"UnitFileState": "masked-runtime", "ActiveState": "inactive"},
    })

    result = du.enable_timers(report, runner=fake)

    ok, changed, _detail = result["coord-serve.timer"]
    assert ok
    assert not changed
    assert all(argv[2] != "enable" for argv in fake.calls)


def test_enable_timers_skips_a_unit_this_host_does_not_run(reference, installed):
    """`coord-serve.timer` is packaged but was never installed here (safety
    property 1: a release does not decide which services a host runs) —
    `install_units` reports it ACTION_NEW, and enabling it would violate the
    exact rule that makes the content-refresh side of this module safe."""
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert _by_name(report)["coord-serve.timer"].action == du.ACTION_NEW

    fake = _FakeRun()
    result = du.enable_timers(report, runner=fake)
    assert result == {}
    assert fake.calls == []


def test_enable_timers_ignores_service_units(reference, installed):
    """Only `.timer` units are touched — a `.service`'s enablement is a
    one-time topology choice (install-agent.sh, or a human at setup), not
    something a routine content refresh should override."""
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    fake = _FakeRun()
    du.enable_timers(report, runner=fake)
    assert all(not argv[-1].endswith(".service") for argv in fake.calls)


def test_enable_timers_reports_a_failure_without_crashing(reference, installed):
    (installed / "coord-serve.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    fake = _FakeRun(ok=False)
    result = du.enable_timers(report, runner=fake)
    ok, changed, detail = result["coord-serve.timer"]
    assert not ok
    # A failed attempt is not a confirmed change (#2124 item 3/4) — the
    # deploy's own output must never claim to have moved a state it didn't.
    assert not changed
    assert "Failed to enable" in detail


def test_enable_timers_degrades_without_systemd():
    def _boom(*_a, **_k):
        raise FileNotFoundError("systemctl")

    report = du.InstallReport(units=[du.UnitOutcome("x.timer", du.ACTION_UPDATED)])
    result = du.enable_timers(report, runner=_boom)
    ok, changed, detail = result["x.timer"]
    assert not ok
    assert not changed
    assert "no systemd" in detail


def test_enable_timers_is_a_noop_with_no_timers_in_the_report(reference, installed):
    (installed / "coord-web.service").write_text("[Service]\nExecStart=old\n")
    report = du.install_units(target_dir=installed, reference_dir=reference,
                              machine_name="dellserver", port=7433)
    assert du.enable_timers(report, runner=_FakeRun()) == {}


# ── degradation ──────────────────────────────────────────────────────────


def test_a_wheel_with_no_packaged_units_reports_rather_than_crashes(tmp_path):
    """An install predating #1927 ships no `coord/deploy/`. There is nothing
    to deploy from, and saying so is the whole answer."""
    # reference_dir=None falls back to the real packaged dir, which exists in
    # this checkout — so drive the empty case explicitly instead.
    empty = tmp_path / "empty"
    empty.mkdir()
    report = du.install_units(target_dir=tmp_path, reference_dir=empty)
    assert report.units == []
    assert "nothing packaged" in report.summary()


def test_daemon_reload_without_systemd_degrades_to_a_message():
    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("systemctl")

    ok, detail = du.daemon_reload(runner=_boom)
    assert not ok
    assert "no systemd" in detail


def test_daemon_reload_reports_a_nonzero_exit():
    class _Proc:
        returncode = 1
        stderr = "Failed to reload"
        stdout = ""

    ok, detail = du.daemon_reload(runner=lambda *a, **k: _Proc())
    assert not ok
    assert "Failed to reload" in detail


def test_daemon_reload_success():
    class _Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    ok, detail = du.daemon_reload(runner=lambda *a, **k: _Proc())
    assert ok
    assert detail


# ── the real packaged set ────────────────────────────────────────────────


def test_the_real_packaged_units_are_reachable():
    """Guards the guard: if `packaged_unit_dir()` ever stops finding
    `coord/deploy/`, this whole lane silently becomes a no-op that reports
    success — the 2026-08-04 shape."""
    from coord.health.checks.unit_drift import packaged_unit_dir

    assert packaged_unit_dir() is not None


def test_the_propagation_units_ship_in_the_wheel():
    """#1835's own units must ride the lane they created, or the timer that
    propagates every future release can never itself be updated."""
    packaged = REPO_ROOT / "coord" / "deploy"
    assert (packaged / "coord-release-propagate.service").exists()
    assert (packaged / "coord-release-propagate.timer").exists()
