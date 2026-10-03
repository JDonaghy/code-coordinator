"""#2121: the active venv colour is immutable, and every install is recorded.

The incident these test: on 2026-08-11 ``coord-agent`` on dellserver had
been executing from ``~/.coord-venv.green`` since 02:32. At 09:44 something
wrote ``~/.coord-venv.blue`` and flipped the symlink onto it; at 09:58 the
same something rebuilt **green** — the colour the live daemon was running
out of — and flipped back. The agent spent the next six hours holding
already-imported modules at 0.5.32 and loading everything else at 0.5.36,
both colours ended up on 0.5.36 so there was no rollback generation left,
and nothing anywhere recorded who had done it.

Each class below fails against the pre-fix code (#2096's rule):

* ``TestActiveColourIsImmutable`` — pre-fix, ``perform_update`` only ever
  refused when the *caller's own* ``sys.executable`` was in the target
  slot; a third-party updater rebuilt a live agent's colour unopposed.
* ``TestInactiveColourIsTheOneThatMoved`` — pre-fix nothing asserted which
  colour a successful update actually wrote.
* ``TestEveryInstallIsAudited`` — pre-fix there was no audit row at all.
* ``TestTestsCannotReachTheLiveInstall`` — pre-fix ``perform_update``
  happily installed into ``~/.coord-venv`` from inside pytest.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.agent_update import (
    LiveInstallGuardError,
    assert_not_live_install,
    cli_initiator,
    current_slot,
    ensure_symlink_layout,
    perform_update,
    processes_holding_slot,
    rollback,
)
from coord.audit import query_audit_log
from coord.platform_paths import venv_bin, venv_exe, venv_pip, venv_python

@pytest.fixture
def proc_root(tmp_path: Path) -> Path:
    """An empty stand-in for ``/proc``.

    Every test that isn't about holder detection uses this: the scan finds
    nothing, which is the "no live process is running from that slot" case.
    Passing it explicitly (rather than letting the default ``/proc`` through)
    also keeps the suite from grading itself against whatever happens to be
    running on the machine.
    """
    root = tmp_path / "proc"
    root.mkdir()
    return root


def _fake_proc(root: Path, pid: int, argv: list[str], environ: dict | None = None) -> None:
    """Write a ``/proc/<pid>`` well enough for :func:`processes_holding_slot`.

    Real ``/proc`` gives NUL-separated, NUL-terminated ``cmdline`` and
    ``environ`` blobs; reproduce that exactly rather than a convenient
    approximation, since the parser's job is to survive that shape.
    """
    entry = root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
    env = environ or {}
    (entry / "environ").write_bytes(
        ("\0".join(f"{k}={v}" for k, v in env.items()) + "\0").encode() if env else b""
    )


def _run_stub(*, version: str = "9.9.9", calls: list | None = None):
    """Fake ``python -m venv`` / ``pip install`` / smoke check.

    The venv branch records *version* into the slot it creates, so a test
    can read back which version each colour holds — the thing acceptance
    item 2 is actually about.
    """

    def _run(cmd, **kwargs):
        cmd = list(cmd)
        if calls is not None:
            calls.append(cmd)
        if "-m" in cmd and "venv" in cmd:
            slot = Path(cmd[-1])
            venv_bin(slot).mkdir(parents=True, exist_ok=True)
            for target in (venv_python(slot), venv_pip(slot), venv_exe(slot, "coord")):
                target.write_text("#!/bin/sh\n")
                target.chmod(0o755)
            (slot / "VERSION").write_text(version)
            return subprocess.CompletedProcess(cmd, 0, "created\n", "")
        if Path(cmd[0]) == venv_pip(Path(cmd[0]).parent.parent) and "install" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "Successfully installed\n", "")
        if Path(cmd[0]) == venv_python(Path(cmd[0]).parent.parent) and "-c" in cmd:
            slot = Path(cmd[0]).parent.parent
            return subprocess.CompletedProcess(cmd, 0, f"{_slot_version(slot)}\n", "")
        if Path(cmd[0]) == venv_exe(Path(cmd[0]).parent.parent, "coord") and "--version" in cmd:
            slot = Path(cmd[0]).parent.parent
            return subprocess.CompletedProcess(
                cmd, 0, f"coord, version {_slot_version(slot)}\n", ""
            )
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    return _run


def _slot_version(slot: Path) -> str:
    marker = slot / "VERSION"
    return marker.read_text().strip() if marker.exists() else "0.0.0"


def _update(venv_dir: Path, proc_root: Path, version: str, **kw):
    with patch(
        "coord.agent_update.subprocess.run", side_effect=_run_stub(version=version)
    ):
        return perform_update(
            venv_dir, "pkg", target_version=version, proc_root=proc_root, **kw
        )


# ── holder detection ────────────────────────────────────────────────────


class TestProcessesHoldingSlot:
    def test_finds_the_process_whose_argv0_is_inside_the_slot(
        self, tmp_path: Path
    ) -> None:
        """The literal shape of the 2026-08-11 evidence: a coord-agent
        started as ``~/.coord-venv.green/bin/python3.12 ~/.coord-venv/bin/coord
        agent ...``."""
        proc = tmp_path / "proc"
        green = tmp_path / ".coord-venv.green"
        green.mkdir()
        _fake_proc(
            proc,
            4242,
            [
                str(green / "bin" / "python3.12"),
                str(tmp_path / ".coord-venv" / "bin" / "coord"),
                "agent",
                "--config",
                "/home/john/coordinator.yml",
            ],
        )

        holders = processes_holding_slot(green, proc_root=proc)

        assert [h.pid for h in holders] == [4242]
        assert holders[0].source == "argv"
        assert str(green) in holders[0].evidence

    def test_finds_a_process_via_virtual_env_when_argv_says_nothing(
        self, tmp_path: Path
    ) -> None:
        proc = tmp_path / "proc"
        blue = tmp_path / ".coord-venv.blue"
        blue.mkdir()
        _fake_proc(proc, 77, ["python3", "-m", "http.server"],
                   environ={"VIRTUAL_ENV": str(blue), "HOME": "/home/john"})

        holders = processes_holding_slot(blue, proc_root=proc)

        assert [(h.pid, h.source) for h in holders] == [(77, "environ")]

    def test_does_not_confuse_the_other_colour(self, tmp_path: Path) -> None:
        """``.blue`` must not match ``.blue-something`` or ``.green``, and a
        process running out of the *symlink* path names no colour at all."""
        proc = tmp_path / "proc"
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"
        blue.mkdir()
        green.mkdir()
        _fake_proc(proc, 1, [str(green / "bin" / "python3")])
        _fake_proc(proc, 2, [str(tmp_path / ".coord-venv.blueprint" / "bin" / "python3")])
        _fake_proc(proc, 3, [str(tmp_path / ".coord-venv" / "bin" / "coord"), "status"])

        assert processes_holding_slot(blue, proc_root=proc) == []
        assert [h.pid for h in processes_holding_slot(green, proc_root=proc)] == [1]

    def test_missing_proc_is_empty_not_an_exception(self, tmp_path: Path) -> None:
        assert processes_holding_slot(tmp_path / "slot", proc_root=tmp_path / "nope") == []

    def test_excludes_requested_pids(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc"
        green = tmp_path / ".coord-venv.green"
        green.mkdir()
        _fake_proc(proc, 99, [str(green / "bin" / "python3")])

        assert processes_holding_slot(green, proc_root=proc, exclude_pids=(99,)) == []


# ── acceptance 1: the active colour is immutable ────────────────────────


class TestActiveColourIsImmutable:
    def test_refuses_when_a_live_process_holds_the_target_slot(
        self, tmp_path: Path
    ) -> None:
        """The incident, reproduced. An agent has been running out of green
        since it started; the symlink has since flipped to blue, so green
        is the colour the *next* update would rebuild. Pre-fix that update
        proceeded — the updater's own ``sys.executable`` was elsewhere, so
        the #2140 guard had nothing to say."""
        proc = tmp_path / "proc"
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"

        # gen0 -> blue (migration), first update writes green and swaps.
        _update(venv_dir, proc, "0.5.32")
        assert current_slot(venv_dir) == green
        # ... a long-lived agent starts from green ...
        agent_argv = [str(green / "bin" / "python3.12"),
                      str(venv_dir / "bin" / "coord"), "agent"]
        _fake_proc(proc, 3131, agent_argv)
        # ... a second update writes blue and swaps onto it; the agent is
        # still executing green, which is now the inactive colour.
        _update(venv_dir, proc, "0.5.36")
        assert current_slot(venv_dir) == blue
        green_before = _slot_version(green)

        # The third update targets green — the slot the live agent holds.
        result = _update(venv_dir, proc, "0.5.37")

        assert result.ok is False
        assert result.swapped is False
        assert "refusing to update" in (result.error or "")
        assert "3131" in (result.error or "")
        assert str(green) in (result.error or "")
        # Green is byte-for-byte what the live process is still executing.
        assert _slot_version(green) == green_before
        assert current_slot(venv_dir) == blue

    def test_refusal_names_the_holding_process_so_an_operator_can_act(
        self, tmp_path: Path
    ) -> None:
        proc = tmp_path / "proc"
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        _update(venv_dir, proc, "1.0.0")
        blue = tmp_path / ".coord-venv.blue"
        _fake_proc(proc, 8080, [str(blue / "bin" / "python3.12"),
                                str(venv_dir / "bin" / "coord"), "agent"])

        result = _update(venv_dir, proc, "2.0.0")

        error = result.error or ""
        assert "pid 8080" in error
        assert "coord agent" in error
        assert "systemctl --user restart coord-agent" in error

    def test_a_differently_spelled_symlink_target_is_still_the_active_colour(
        self, tmp_path: Path, proc_root: Path
    ) -> None:
        """``_other_slot`` does string arithmetic on ``venv_dir``, while the
        active colour comes from reading a symlink — two paths from
        different places. When they spell the same directory differently (a
        symlinked ``$HOME``, a link written through an alias), a ``==``
        between them says "not the active one" and the *live* colour gets
        picked as the rebuild target. Pre-fix, blue below was rmtree'd."""
        real = tmp_path / "real"
        real.mkdir()
        blue = real / ".coord-venv.blue"
        blue.mkdir()
        (blue / "marker").write_text("the live install\n")
        # `alias` is another name for `real`, so `alias/.coord-venv.blue` and
        # `real/.coord-venv.blue` are one directory reached two ways.
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        venv_dir = alias / ".coord-venv"
        venv_dir.symlink_to(real / ".coord-venv.blue", target_is_directory=True)

        result = _update(venv_dir, proc_root, "2.0.0")

        assert result.ok is True
        # green was built; the live blue install was never touched.
        assert result.slot == alias / ".coord-venv.green"
        assert (blue / "marker").read_text() == "the live install\n"

    def test_refuses_outright_if_the_target_is_the_active_colour(
        self, tmp_path: Path, proc_root: Path, monkeypatch
    ) -> None:
        """Belt and braces for the above: whatever path arithmetic produces
        the "other" colour, the colour ``venv_dir`` resolves to right now is
        never the one that gets deleted. Asserted rather than assumed —
        #2121 is the incident where the environment a live process was
        executing from got rebuilt anyway."""
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        active = ensure_symlink_layout(venv_dir)
        monkeypatch.setattr(
            "coord.agent_update._other_slot", lambda _venv, _active: active
        )
        (active / "marker").write_text("live\n")

        result = _update(venv_dir, proc_root, "2.0.0")

        assert result.ok is False
        assert "currently resolves to" in (result.error or "")
        assert (active / "marker").read_text() == "live\n"

    def test_proceeds_when_nothing_is_running_from_the_target(
        self, tmp_path: Path, proc_root: Path
    ) -> None:
        """The guard must not wedge a fleet that is behaving: an inactive
        colour with no live holder is exactly what an update is for."""
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        assert _update(venv_dir, proc_root, "1.0.0").ok is True
        assert _update(venv_dir, proc_root, "2.0.0").ok is True


# ── acceptance 2: the INACTIVE colour is the one that moved ─────────────


class TestInactiveColourIsTheOneThatMoved:
    def test_two_colours_hold_different_versions_after_an_upgrade(
        self, tmp_path: Path, proc_root: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"

        _update(venv_dir, proc_root, "0.5.32")
        assert current_slot(venv_dir) == green

        active_before = current_slot(venv_dir)
        inactive_before = blue
        result = _update(venv_dir, proc_root, "0.5.36")

        assert result.ok is True
        # The colour that was INACTIVE is the one that moved...
        assert result.slot == inactive_before
        assert _slot_version(inactive_before) == "0.5.36"
        # ...and the one that was live is untouched, still one generation
        # back — a real rollback target, which #2121 lost.
        assert result.previous_slot == active_before
        assert _slot_version(active_before) == "0.5.32"
        # The two colours therefore disagree, which is the whole property.
        assert _slot_version(blue) != _slot_version(green)

    def test_rollback_has_somewhere_to_go_after_an_upgrade(
        self, tmp_path: Path, proc_root: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        _update(venv_dir, proc_root, "0.5.32")
        _update(venv_dir, proc_root, "0.5.36")

        with patch(
            "coord.agent_update.subprocess.run", side_effect=_run_stub()
        ):
            result = rollback(venv_dir, initiator="test")

        assert result.ok is True
        assert result.new_version == "0.5.32"


# ── acceptance 3: every install is on the audit trail ───────────────────


def _venv_rows() -> list[dict]:
    return query_audit_log(event_type="venv_install", limit=100)["entries"]


class TestEveryInstallIsAudited:
    def test_successful_swap_writes_an_entry_naming_the_initiator(
        self, tmp_path: Path, proc_root: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        _update(venv_dir, proc_root, "0.5.36",
                initiator="coord release propagate (john@dellserver pid 991)")

        rows = _venv_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["actor"] == "coord release propagate (john@dellserver pid 991)"
        assert row["details"]["outcome"] == "swapped"
        assert row["details"]["new_version"] == "0.5.36"
        assert row["details"]["slot"].endswith(".green")
        assert row["details"]["previous_slot"].endswith(".blue")

    def test_a_refusal_is_recorded_too(self, tmp_path: Path) -> None:
        """#2096: a surface that only records successes is a surface that
        reports a roll it did not confirm."""
        proc = tmp_path / "proc"
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        _update(venv_dir, proc, "1.0.0", initiator="first")
        blue = tmp_path / ".coord-venv.blue"
        _fake_proc(proc, 5150, [str(blue / "bin" / "python3.12"), "coord", "agent"])

        _update(venv_dir, proc, "2.0.0", initiator="the-worker")

        refusals = [r for r in _venv_rows() if r["details"]["outcome"] == "refused"]
        assert len(refusals) == 1
        assert refusals[0]["actor"] == "the-worker"
        assert "5150" in refusals[0]["summary"]
        assert refusals[0]["details"]["holders"][0]["pid"] == 5150

    def test_an_install_that_names_nobody_is_recorded_as_unattributed(
        self, tmp_path: Path, proc_root: Path
    ) -> None:
        """Never guess an actor: "we do not know who did this" is the
        finding, and it belongs in the row rather than laundered into a
        plausible-looking name."""
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        _update(venv_dir, proc_root, "1.0.0")

        assert _venv_rows()[0]["actor"] == "unattributed"

    def test_a_failed_install_is_recorded(self, tmp_path: Path, proc_root: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        def _boom(cmd, **kwargs):
            return subprocess.CompletedProcess(list(cmd), 1, "", "venv creation failed\n")

        with patch("coord.agent_update.subprocess.run", side_effect=_boom):
            result = perform_update(
                venv_dir, "pkg", target_version="1.0.0",
                proc_root=proc_root, initiator="someone",
            )

        assert result.ok is False
        rows = _venv_rows()
        assert [r["details"]["outcome"] for r in rows] == ["failed"]
        assert rows[0]["actor"] == "someone"

    def test_rollback_is_recorded(self, tmp_path: Path, proc_root: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        _update(venv_dir, proc_root, "0.5.32")
        _update(venv_dir, proc_root, "0.5.36")

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub()):
            rollback(venv_dir, initiator="operator@laptop")

        rolled = [r for r in _venv_rows() if r["details"]["outcome"] == "rolled_back"]
        assert len(rolled) == 1
        assert rolled[0]["actor"] == "operator@laptop"

    def test_cli_initiator_names_user_host_and_pid(self) -> None:
        text = cli_initiator("coord agent update")
        assert text.startswith("coord agent update (")
        assert f"pid {os.getpid()}" in text


# ── acceptance: a test cannot reach the live install ────────────────────


class TestTestsCannotReachTheLiveInstall:
    @pytest.mark.parametrize("suffix", ["", ".blue", ".green"])
    def test_refuses_the_real_agent_venv_under_pytest(self, suffix: str) -> None:
        live = Path.home() / f".coord-venv{suffix}"

        with pytest.raises(LiveInstallGuardError) as exc:
            assert_not_live_install(live)

        assert "sacrificial_venv_root" in str(exc.value)

    def test_perform_update_refuses_before_touching_anything(
        self, proc_root: Path
    ) -> None:
        """The guard has to sit ahead of the migration/rmtree, not after —
        `ensure_symlink_layout` alone would already have renamed the live
        install into a slot."""
        live = Path.home() / ".coord-venv"

        with patch("coord.agent_update.subprocess.run") as run:
            with pytest.raises(LiveInstallGuardError):
                perform_update(live, "pkg", target_version="1.0.0", proc_root=proc_root)

        run.assert_not_called()

    def test_a_sacrificial_root_is_a_legitimate_target(
        self, sacrificial_venv_root: Path, proc_root: Path
    ) -> None:
        """The fixture is the sanctioned alternative: the real code path,
        against a real tree, that is not ``~/.coord-venv``."""
        assert_not_live_install(sacrificial_venv_root)

        result = _update(sacrificial_venv_root, proc_root, "1.2.3")

        assert result.ok is True
        assert current_slot(sacrificial_venv_root) == (
            sacrificial_venv_root.parent / ".coord-venv.green"
        )

    def test_outside_pytest_the_guard_is_a_noop(self, monkeypatch) -> None:
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        assert_not_live_install(Path.home() / ".coord-venv")

    def test_rollback_refuses_before_touching_the_live_symlink(self) -> None:
        """`rollback` is the other production-mutating entry point reachable
        from `/rollback` with the same `_venv_dir()`-resolved path, and it
        moves the same live symlink via the same `_atomic_swap` `perform_update`
        uses — it must be guarded exactly like `perform_update`, not just
        exempted on the "nothing is deleted" reasoning that only applies to
        `processes_holding_slot`."""
        live = Path.home() / ".coord-venv"

        with pytest.raises(LiveInstallGuardError):
            rollback(live, initiator="someone")

    def test_a_differently_spelled_path_to_the_live_venv_still_trips_the_guard(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Same reasoning as `_other_slot`'s `_same_path` fix: the guard
        compares against `Path.home() / ".coord-venv"` by string, and a
        `venv_dir` that names the identical directory through a symlinked
        `$HOME` (or any other alias) must not slip past a `!=` on the
        unresolved spelling."""
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        alias_home = tmp_path / "alias-home"
        alias_home.symlink_to(real_home, target_is_directory=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: alias_home))

        live_via_real_path = real_home / ".coord-venv"

        with pytest.raises(LiveInstallGuardError):
            assert_not_live_install(live_via_real_path)


# ── the pre-existing migration path still works under the new guards ────


class TestMigrationUnaffected:
    def test_plain_directory_still_migrates_to_blue(
        self, tmp_path: Path, proc_root: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        (venv_dir / "marker").write_text("gen0\n")

        assert ensure_symlink_layout(venv_dir) == tmp_path / ".coord-venv.blue"
        assert _update(venv_dir, proc_root, "1.0.0").ok is True
        assert (tmp_path / ".coord-venv.blue" / "marker").read_text() == "gen0\n"
