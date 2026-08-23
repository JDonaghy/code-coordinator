"""Tests for scripts/fleet_watchdog.py (#2580).

The watchdog is deliberately NOT part of the ``coord`` package — it must
survive the exact failure class (a broken ``~/.coord-venv``) that takes
``coord`` itself down, so importing it here goes through ``sys.path``
insertion + a plain module import, the same pattern
``tests/test_release_unified_1242.py`` uses for ``scripts/verify_release_
wheel.py``. None of these tests touch the real ``$HOME``/``~/.coord`` —
every context is built with an explicit ``--home`` under ``tmp_path``.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import stat
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FLEET_WATCHDOG_PY = REPO_ROOT / "scripts" / "fleet_watchdog.py"
ROLLBACK_SH = REPO_ROOT / "scripts" / "coord-venv-rollback.sh"

sys.path.insert(0, str(REPO_ROOT))

from scripts import fleet_watchdog  # noqa: E402  - needs the sys.path line above
from scripts.fleet_watchdog import (  # noqa: E402
    Check,
    Finding,
    build_context,
    has_open_holder,
    main,
    parse_args,
    run_check,
    run_sweep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, **overrides):
    argv = [
        "--home",
        str(tmp_path),
        "--board-url",
        overrides.pop("board_url", ""),
    ]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return build_context(parse_args(argv))


def _write_slot(slot_dir: Path, *, healthy: bool, editable: bool = False) -> None:
    """A fake blue/green venv slot: just enough of bin/{python3,pip,coord}
    to drive the health-check subprocess calls, with no real venv/install
    (fast, hermetic, and — importantly — never a real ``import coord``
    anywhere in the test process)."""
    bin_dir = slot_dir / "bin"
    bin_dir.mkdir(parents=True)
    python3 = bin_dir / "python3"
    pip = bin_dir / "pip"
    coord_bin = bin_dir / "coord"

    if healthy:
        python3.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        pip.write_text(
            "#!/usr/bin/env python3\n"
            "print('Name: code-coordinator')\n"
            "print('Location: /fake/site-packages')\n"
        )
        coord_bin.write_text("#!/usr/bin/env python3\nprint('code-coordinator 9.9.9')\n")
    else:
        # Simulates the 2026-08-22 incident: the editable install's source
        # directory is gone, so the import fails too.
        python3.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
        pip_lines = ["#!/usr/bin/env python3", "print('Name: code-coordinator')"]
        if editable:
            pip_lines.append('print("Editable project location: /deleted/worktree")')
        pip.write_text("\n".join(pip_lines) + "\n")
        coord_bin.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")

    for f in (python3, pip, coord_bin):
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class _BoardHandler(http.server.BaseHTTPRequestHandler):
    board_json: bytes = b"{}"

    def do_GET(self):  # noqa: N802 - stdlib signature
        if self.path == "/board":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.board_json)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence test output
        pass


@pytest.fixture(autouse=True)
def _isolate_from_host_binaries(monkeypatch):
    """Never let a bare ``run_sweep()`` reach out to *this* box's real
    ``systemctl``/``tmux`` — determinism regardless of what's installed or
    running in whatever sandbox executes this suite. Tests that specifically
    exercise systemctl/tmux parsing install their own fake binary via
    ``monkeypatch.setattr`` afterward in the test body; the same
    ``monkeypatch`` fixture instance is shared, so the later call wins."""
    monkeypatch.setattr(fleet_watchdog, "SYSTEMCTL", None)
    monkeypatch.setattr(fleet_watchdog, "TMUX", None)


@pytest.fixture()
def fake_board_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _BoardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Acceptance test 1: never imports coord (grep-shaped, cf.
# tests/test_ci_acceptance_gate_1950.py's own reasoning for why this shape
# is the right one — a real workflow can't run inside pytest, and here the
# thing we must not do can't just be caught by "does it currently work",
# since importing coord would frequently still succeed in a dev sandbox).
# ---------------------------------------------------------------------------

_IMPORT_COORD_RE = re.compile(r"(?m)^\s*(import\s+coord(\.\w+)*\b|from\s+coord(\.\w+)*\s+import\b)")


def _strip_module_docstring(source: str) -> str:
    """Drop the first triple-quoted string (the module docstring) so a
    "never does X" test doesn't trip on prose *describing* X — this module's
    own docstring quotes ``pip install`` and ``import coord`` verbatim as
    the things it must never do."""
    match = re.search(r'"""[\s\S]*?"""', source)
    if not match:
        return source
    return source[: match.start()] + source[match.end() :]


class TestNeverImportsCoord:
    def test_fleet_watchdog_py_never_imports_coord(self):
        source = FLEET_WATCHDOG_PY.read_text()
        match = _IMPORT_COORD_RE.search(source)
        assert match is None, f"fleet_watchdog.py imports coord: {match.group(0)!r}"

    def test_fleet_watchdog_py_never_pip_installs_anything(self):
        # Constraint 5 (#2580): never `pip install` anything, ever.
        code_only = _strip_module_docstring(FLEET_WATCHDOG_PY.read_text())
        assert "pip install" not in code_only
        assert "pip3 install" not in code_only

    def test_coord_venv_rollback_sh_never_pip_installs_anything(self):
        # Constraint 5 (#2580): never `pip install` anything, ever — restoring
        # known-good state and moving versions are different jobs. The
        # rollback script only ever *reads* a sibling slot's health (its
        # `"$py" -c 'import coord...'` calls are smoke-checking a TARGET
        # slot's own interpreter, the same thing coord.agent_update.
        # _smoke_check does — not this script's own process importing
        # anything), and flips a symlink.
        source = ROLLBACK_SH.read_text()
        assert "pip install" not in source
        assert "pip3 install" not in source


# ---------------------------------------------------------------------------
# Acceptance test 2: corrupt ~/.coord-venv (editable pointing at a deleted
# dir) and confirm the watchdog restores a working `coord` — without ever
# importing coord itself to do it.
# ---------------------------------------------------------------------------


class TestVenvRollbackRepair:
    def test_editable_venv_pointing_at_deleted_dir_gets_rolled_back(self, tmp_path):
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"
        _write_slot(blue, healthy=False, editable=True)  # the corrupted slot
        _write_slot(green, healthy=True)  # the sibling that survived

        venv = tmp_path / ".coord-venv"
        venv.symlink_to(blue, target_is_directory=True)

        argv = [
            "--home", str(tmp_path),
            "--venv-dir", str(venv),
            "--rollback-script", str(ROLLBACK_SH),
            "--board-url", "",
            "--now", "2000000000",
        ]
        exit_code = main(argv)

        assert exit_code == 0
        assert venv.is_symlink()
        assert venv.resolve() == green.resolve()

        # The restored `coord` genuinely runs now.
        import subprocess

        result = subprocess.run(
            [str(venv / "bin" / "coord"), "--version"], capture_output=True, text=True
        )
        assert result.returncode == 0

        # Rate-limit state recorded one successful repair.
        state = json.loads((tmp_path / ".coord" / "watchdog-state.json").read_text())
        assert state["venv-rollback"]["consecutive"] == 1
        assert state["venv-rollback"]["last_ok"] is True

    def test_no_op_when_venv_already_healthy(self, tmp_path):
        slot = tmp_path / ".coord-venv.blue"
        _write_slot(slot, healthy=True)
        venv = tmp_path / ".coord-venv"
        venv.symlink_to(slot, target_is_directory=True)

        ctx = _ctx(tmp_path, venv_dir=venv)
        findings = run_sweep(ctx)
        assert not any(f.condition == "venv-rollback" for f in findings)
        assert venv.resolve() == slot.resolve()  # untouched

    def test_refuses_when_sibling_also_broken(self, tmp_path):
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"
        _write_slot(blue, healthy=False, editable=True)
        _write_slot(green, healthy=False)  # sibling is ALSO broken

        venv = tmp_path / ".coord-venv"
        venv.symlink_to(blue, target_is_directory=True)

        ctx = _ctx(tmp_path, venv_dir=venv, rollback_script=ROLLBACK_SH)
        findings = run_sweep(ctx)
        [finding] = [f for f in findings if f.condition == "venv-rollback"]
        assert finding.repaired is False
        assert finding.error  # refused, with a reason
        # Refusing must not have mutated the symlink.
        assert venv.resolve() == blue.resolve()


# ---------------------------------------------------------------------------
# Acceptance test 3: a sentinel-suppressed condition is reported, not
# repaired.
# ---------------------------------------------------------------------------


class TestSuppressionSentinel:
    def test_suppressed_tier1_condition_is_not_repaired(self, tmp_path):
        # local-bin-symlink: missing entirely -> normally auto-repaired.
        venv = tmp_path / ".coord-venv"
        _write_slot(venv, healthy=True)  # not a blue/green symlink -> venv-rollback check no-ops

        coord_dir = tmp_path / ".coord"
        coord_dir.mkdir()
        (coord_dir / "watchdog-suppress.json").write_text(
            json.dumps(
                {
                    "local-bin-symlink": {
                        "reason": "test: pretend this is deliberately unlinked",
                        "set": "2026-08-21",
                        "expires": None,
                    }
                }
            )
        )

        ctx = _ctx(tmp_path, venv_dir=venv)
        findings = run_sweep(ctx)
        [finding] = [f for f in findings if f.condition == "local-bin-symlink"]

        assert finding.suppressed is True
        assert finding.repaired is False
        assert not (tmp_path / ".local" / "bin" / "coord").exists()

    def test_release_propagate_timer_disabled_on_purpose_is_reported_not_flagged_actionable(
        self, tmp_path, monkeypatch
    ):
        # #2580's own worked example: coord-release-propagate.timer reads
        # disabled on purpose (manual release rolls). Suppressing it must
        # make the run "clean" from the operator's point of view even
        # though the condition is still detected.
        fake_systemctl = tmp_path / "fake-systemctl"
        fake_systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$*\" == *list-unit-files* ]]; then\n"
            "  echo 'coord-release-propagate.timer disabled'\n"
            "  echo 'coord-db-backup.timer enabled'\n"
            "fi\n"
        )
        fake_systemctl.chmod(0o755)
        monkeypatch.setattr(fleet_watchdog, "SYSTEMCTL", str(fake_systemctl))

        coord_dir = tmp_path / ".coord"
        coord_dir.mkdir()
        (coord_dir / "watchdog-suppress.json").write_text(
            json.dumps(
                {
                    "coord-release-propagate.timer": {
                        "reason": "manual rolls until release lane stabilises",
                        "set": "2026-08-21",
                        "expires": None,
                    }
                }
            )
        )

        ctx = _ctx(tmp_path)
        findings = fleet_watchdog.check_disabled_timers(ctx)
        [finding] = [f for f in findings if "release-propagate" in f.signature]
        suppressions = fleet_watchdog.load_suppressions(ctx)
        suppressed, _ = fleet_watchdog.is_suppressed(suppressions, finding.suppress_keys, now=ctx.now)
        assert suppressed is True

    def test_expired_suppression_falls_through(self, tmp_path):
        venv = tmp_path / ".coord-venv"
        _write_slot(venv, healthy=True)
        coord_dir = tmp_path / ".coord"
        coord_dir.mkdir()
        (coord_dir / "watchdog-suppress.json").write_text(
            json.dumps(
                {
                    "local-bin-symlink": {
                        "reason": "temporary",
                        "set": "2020-01-01",
                        "expires": "2020-02-01T00:00:00+00:00",
                    }
                }
            )
        )
        ctx = _ctx(tmp_path, venv_dir=venv, now=1893456000)  # 2030 — well past expiry
        findings = run_sweep(ctx)
        [finding] = [f for f in findings if f.condition == "local-bin-symlink"]
        assert finding.suppressed is False
        assert finding.repaired is True
        assert (tmp_path / ".local" / "bin" / "coord").is_symlink()


# ---------------------------------------------------------------------------
# Acceptance test 4: the Nth consecutive identical repair escalates instead
# of repairing.
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_nth_consecutive_identical_repair_escalates(self, tmp_path):
        ctx = _ctx(tmp_path, rate_limit=3)
        state: dict = {}
        suppressions: dict = {}

        def detect(_ctx):
            return [
                Finding(
                    condition="always-recurs",
                    signature="always-recurs",
                    tier=1,
                    summary="synthetic condition that never actually resolves",
                    repair_fn=lambda _ctx: (True, "repaired, but it will be back"),
                )
            ]

        check = Check("always-recurs", 1, detect)

        outcomes = [run_check(ctx, state, suppressions, check)[0] for _ in range(5)]

        assert [f.repaired for f in outcomes] == [True, True, True, False, False]
        assert [f.escalated for f in outcomes] == [False, False, False, True, True]

    def test_rate_limit_resets_once_condition_stops_recurring(self, tmp_path):
        ctx = _ctx(tmp_path, rate_limit=2)
        state: dict = {}
        suppressions: dict = {}
        still_broken = {"value": True}

        def detect(_ctx):
            if not still_broken["value"]:
                return []
            return [
                Finding(
                    condition="flaky",
                    signature="flaky",
                    tier=1,
                    summary="flaky condition",
                    repair_fn=lambda _ctx: (True, "repaired"),
                )
            ]

        check = Check("flaky", 1, detect)

        first = run_check(ctx, state, suppressions, check)[0]
        second = run_check(ctx, state, suppressions, check)[0]
        assert first.repaired and second.repaired
        third = run_check(ctx, state, suppressions, check)[0]
        assert third.escalated  # rate_limit=2 reached

        still_broken["value"] = False
        assert run_check(ctx, state, suppressions, check) == []
        assert "flaky" not in state  # resolved -> counter cleared

        still_broken["value"] = True
        fourth = run_check(ctx, state, suppressions, check)[0]
        assert fourth.repaired  # counter reset, not still escalated


# ---------------------------------------------------------------------------
# Supporting coverage: the other Tier-1 checks and the release-cordon gate.
# ---------------------------------------------------------------------------


class TestLocalBinSymlinkRepair:
    def test_dangling_symlink_gets_relinked(self, tmp_path):
        venv = tmp_path / ".coord-venv"
        _write_slot(venv, healthy=True)
        local_bin = tmp_path / ".local" / "bin" / "coord"
        local_bin.parent.mkdir(parents=True)
        local_bin.symlink_to(tmp_path / "nonexistent" / "coord")

        ctx = _ctx(tmp_path, venv_dir=venv)
        findings = run_sweep(ctx)
        [finding] = [f for f in findings if f.condition == "local-bin-symlink"]
        assert finding.repaired is True
        assert local_bin.resolve() == (venv / "bin" / "coord").resolve()


class TestReleaseCordonGate:
    def test_active_cordon_blocks_repair(self, tmp_path):
        venv = tmp_path / ".coord-venv"
        _write_slot(venv, healthy=True)
        local_bin = tmp_path / ".local" / "bin" / "coord"  # missing -> normally repaired
        coord_dir = tmp_path / ".coord"
        coord_dir.mkdir()
        (coord_dir / "paused_machines.json").write_text(
            json.dumps(
                {
                    "paused": [],
                    "release_cordons": {
                        "thishost": {
                            "machine": "thishost",
                            "owner": "release",
                            "reason": "draining for v0.5.210",
                            "expires_at": 0,  # 0 = no expiry, still active
                        }
                    },
                }
            )
        )
        ctx = _ctx(tmp_path, venv_dir=venv, hostname="thishost")
        assert ctx.cordon_active is True
        findings = run_sweep(ctx)
        [finding] = [f for f in findings if f.condition == "local-bin-symlink"]
        assert finding.repaired is False
        assert "cordon" in finding.error
        assert not local_bin.exists()

    def test_cordon_for_a_different_machine_does_not_block(self, tmp_path):
        venv = tmp_path / ".coord-venv"
        _write_slot(venv, healthy=True)
        coord_dir = tmp_path / ".coord"
        coord_dir.mkdir()
        (coord_dir / "paused_machines.json").write_text(
            json.dumps(
                {
                    "paused": [],
                    "release_cordons": {
                        "otherhost": {"machine": "otherhost", "expires_at": 0, "reason": "x"}
                    },
                }
            )
        )
        ctx = _ctx(tmp_path, venv_dir=venv, hostname="thishost")
        assert ctx.cordon_active is False
        findings = run_sweep(ctx)
        [finding] = [f for f in findings if f.condition == "local-bin-symlink"]
        assert finding.repaired is True


class TestExpiredCordonCleanup:
    def test_expired_cordon_gets_cleared(self, tmp_path):
        coord_dir = tmp_path / ".coord"
        coord_dir.mkdir()
        paused_path = coord_dir / "paused_machines.json"
        paused_path.write_text(
            json.dumps(
                {
                    "paused": ["someone"],
                    "release_cordons": {
                        "dellserver": {
                            "machine": "dellserver",
                            "owner": "release",
                            "reason": "draining for v0.5.210",
                            "expires_at": 1000.0,  # long past
                        }
                    },
                }
            )
        )
        ctx = _ctx(tmp_path, now=2000.0)
        findings = run_sweep(ctx)
        [finding] = [f for f in findings if f.condition == "expired-cordon"]
        assert finding.repaired is True

        data = json.loads(paused_path.read_text())
        assert "dellserver" not in data["release_cordons"]
        assert data["paused"] == ["someone"]  # untouched — separate concept

    def test_unexpired_cordon_is_left_alone(self, tmp_path):
        coord_dir = tmp_path / ".coord"
        coord_dir.mkdir()
        paused_path = coord_dir / "paused_machines.json"
        paused_path.write_text(
            json.dumps(
                {
                    "release_cordons": {
                        "dellserver": {"machine": "dellserver", "expires_at": 5000.0}
                    }
                }
            )
        )
        ctx = _ctx(tmp_path, now=2000.0)
        findings = run_sweep(ctx)
        assert not any(f.condition == "expired-cordon" for f in findings)


class TestStaleGitLock:
    def test_stale_lock_with_no_holder_is_removed(self, tmp_path):
        repo = tmp_path / "src" / "code-coordinator" / ".git"
        repo.mkdir(parents=True)
        lock = repo / "index.lock"
        lock.write_text("")
        old = 1_700_000_000.0
        os.utime(lock, (old, old))

        fake_proc = tmp_path / "fake-proc"
        fake_proc.mkdir()  # no pid dirs at all -> has_open_holder returns False
        import scripts.fleet_watchdog as fw

        ctx = _ctx(tmp_path, now=old + 3600)
        old_proc_root = fw.PROC_ROOT
        fw.PROC_ROOT = fake_proc
        try:
            findings = run_sweep(ctx)
        finally:
            fw.PROC_ROOT = old_proc_root

        [finding] = [f for f in findings if f.condition == "stale-git-lock"]
        assert finding.repaired is True
        assert not lock.exists()

    def test_lock_held_by_a_live_process_is_never_touched(self, tmp_path):
        repo = tmp_path / "src" / "code-coordinator" / ".git"
        repo.mkdir(parents=True)
        lock = repo / "index.lock"
        lock.write_text("")
        old = 1_700_000_000.0
        os.utime(lock, (old, old))

        fake_proc = tmp_path / "fake-proc"
        fd_dir = fake_proc / "4242" / "fd"
        fd_dir.mkdir(parents=True)
        (fd_dir / "3").symlink_to(lock)

        assert has_open_holder(lock, proc_root=fake_proc) is True

        ctx = _ctx(tmp_path, now=old + 3600)
        import scripts.fleet_watchdog as fw

        old_proc_root = fw.PROC_ROOT
        fw.PROC_ROOT = fake_proc
        try:
            findings = run_sweep(ctx)
        finally:
            fw.PROC_ROOT = old_proc_root

        assert not any(f.condition == "stale-git-lock" for f in findings)
        assert lock.exists()

    def test_young_lock_is_left_alone(self, tmp_path):
        repo = tmp_path / "src" / "code-coordinator" / ".git"
        repo.mkdir(parents=True)
        lock = repo / "index.lock"
        lock.write_text("")  # mtime is "now" by construction

        ctx = _ctx(tmp_path)
        findings = run_sweep(ctx)
        assert not any(f.condition == "stale-git-lock" for f in findings)


class TestOrphanedWorktrees:
    def test_orphaned_worktree_with_no_board_entry_is_removed(self, tmp_path, fake_board_server, monkeypatch):
        _BoardHandler.board_json = json.dumps({"assignments": []}).encode()
        monkeypatch.setattr(fleet_watchdog, "TMUX", None)

        wt = tmp_path / ".coord" / "worktrees" / "abc123"
        wt.mkdir(parents=True)
        old = 1_700_000_000.0
        os.utime(wt, (old, old))

        port = fake_board_server.server_address[1]
        ctx = _ctx(
            tmp_path,
            now=old + 7200,
            board_url=f"http://127.0.0.1:{port}",
            hostname="thishost",
        )
        findings = run_sweep(ctx)
        [finding] = [f for f in findings if f.condition == "orphaned-worktree"]
        assert finding.repaired is True
        assert not wt.exists()

    def test_worktree_with_a_live_board_entry_is_never_touched(self, tmp_path, fake_board_server, monkeypatch):
        _BoardHandler.board_json = json.dumps(
            {
                "assignments": [
                    {"assignment_id": "abc123", "machine_name": "thishost", "status": "running"}
                ]
            }
        ).encode()
        monkeypatch.setattr(fleet_watchdog, "TMUX", None)

        wt = tmp_path / ".coord" / "worktrees" / "abc123"
        wt.mkdir(parents=True)
        old = 1_700_000_000.0
        os.utime(wt, (old, old))

        port = fake_board_server.server_address[1]
        ctx = _ctx(
            tmp_path,
            now=old + 7200,
            board_url=f"http://127.0.0.1:{port}",
            hostname="thishost",
        )
        findings = run_sweep(ctx)
        assert not any(f.condition == "orphaned-worktree" for f in findings)
        assert wt.exists()

    def test_unreachable_board_never_deletes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fleet_watchdog, "TMUX", None)
        wt = tmp_path / ".coord" / "worktrees" / "abc123"
        wt.mkdir(parents=True)
        old = 1_700_000_000.0
        os.utime(wt, (old, old))

        ctx = _ctx(
            tmp_path,
            now=old + 7200,
            board_url="http://127.0.0.1:1",  # nothing listening
            http_timeout=1,
        )
        findings = run_sweep(ctx)
        assert not any(f.condition == "orphaned-worktree" for f in findings)
        assert wt.exists()

    def test_recent_worktree_is_never_touched(self, tmp_path, fake_board_server, monkeypatch):
        _BoardHandler.board_json = json.dumps({"assignments": []}).encode()
        monkeypatch.setattr(fleet_watchdog, "TMUX", None)
        wt = tmp_path / ".coord" / "worktrees" / "brandnew"
        wt.mkdir(parents=True)  # mtime is "now"

        port = fake_board_server.server_address[1]
        ctx = _ctx(tmp_path, board_url=f"http://127.0.0.1:{port}")
        findings = run_sweep(ctx)
        assert not any(f.condition == "orphaned-worktree" for f in findings)
        assert wt.exists()


class TestFailedUnitsScopedToCoord:
    def test_only_coord_prefixed_units_are_considered(self, tmp_path, monkeypatch):
        fake_systemctl = tmp_path / "fake-systemctl"
        fake_systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$*\" == *list-units* ]]; then\n"
            "  echo 'coord-agent.service loaded failed failed x'\n"
            "  echo 'some-other-thing.service loaded failed failed x'\n"
            "fi\n"
        )
        fake_systemctl.chmod(0o755)
        monkeypatch.setattr(fleet_watchdog, "SYSTEMCTL", str(fake_systemctl))

        ctx = _ctx(tmp_path)
        findings = fleet_watchdog.check_failed_units(ctx)
        assert [f.signature for f in findings] == ["failed-unit:coord-agent.service"]
