"""Unit tests for the per-machine ``cli_venv``/``tui_binary`` checks (#1806).

These two machine-scope checks replace the daemon-host-only ``os.stat``
gathering :mod:`coord.health.fleet_snapshot` used to do for the CLI-venv
version and the tui/ binary-vs-source comparison — both facts about
whichever machine the operator actually put them on, not about the daemon
host. See ``coord/health/checks/deploy_lane_facts.py``'s module docstring
for the full story, and ``tests/test_fleet_health_probes.py`` for the
fleet-scope aggregation this feeds.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from coord.config import HealthConfig
from coord.health.checks import agent_install, deploy_lane_facts as dlf
from coord.health.models import Checkout, HealthContext, Severity

NOW = 1_800_000_000.0


def make_ctx(tmp_path: Path, **kwargs) -> HealthContext:
    thresholds = kwargs.pop("thresholds", None) or HealthConfig()
    home = kwargs.pop("home", tmp_path)
    return HealthContext(
        thresholds=thresholds,
        home=home,
        coord_dir=kwargs.pop("coord_dir", home / ".coord"),
        now=kwargs.pop("now", NOW),
        checkouts=kwargs.pop("checkouts", ()),
        config=kwargs.pop("config", None),
        allow_network=kwargs.pop("allow_network", True),
    )


def _pip_show(monkeypatch, stdout: str, returncode: int = 0) -> None:
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(agent_install.subprocess, "run", _run)


PYPI_SHOW = (
    "Name: code-coordinator\n"
    "Version: 0.4.91\n"
    "Location: /home/x/.coord-cli-venv/lib/python3.12/site-packages\n"
)
EDITABLE_SHOW = (
    "Name: code-coordinator\n"
    "Version: 0.4.92\n"
    "Location: /home/x/.coord-cli-venv/lib/python3.12/site-packages\n"
    "Editable project location: /home/x/src/claude-coordinator\n"
)


# ── cli_venv ─────────────────────────────────────────────────────────────────


def test_cli_venv_absent_is_ok_not_unknown(tmp_path) -> None:
    """The common case — most machines never had this venv created — must
    not read as a fault."""
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "not present on this machine"
    assert result.values == {
        "python": str(tmp_path / ".coord-cli-venv" / "bin" / "python3"),
        "present": False,
        "version": None,
    }


def test_cli_venv_present_pypi_install_is_ok(tmp_path, monkeypatch) -> None:
    python = tmp_path / ".coord-cli-venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()
    _pip_show(monkeypatch, PYPI_SHOW)
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "pypi 0.4.91"
    assert result.values["present"] is True
    assert result.values["version"] == "0.4.91"
    assert result.values["editable"] is False


def test_cli_venv_present_editable_is_still_ok_but_flagged(tmp_path, monkeypatch) -> None:
    """Unlike the agent's own ``~/.coord-venv`` (CRIT if editable), this
    check only reports the fact — the fleet lane check judges skew, not
    install hygiene, for this lane."""
    python = tmp_path / ".coord-cli-venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()
    _pip_show(monkeypatch, EDITABLE_SHOW)
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.values["version"] == "0.4.92"
    assert result.values["editable"] is True


def test_cli_venv_present_but_not_a_real_install_is_unknown(tmp_path, monkeypatch) -> None:
    python = tmp_path / ".coord-cli-venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()
    _pip_show(monkeypatch, "", returncode=1)
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert result.values["version"] is None


def test_cli_venv_pip_failure_is_unknown(tmp_path, monkeypatch) -> None:
    python = tmp_path / ".coord-cli-venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()

    def _run(cmd, **kwargs):
        raise OSError("no such interpreter")

    monkeypatch.setattr(agent_install.subprocess, "run", _run)
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert "no such interpreter" in (result.error or "")


def test_resolve_cli_venv_python_prefers_the_configured_path(tmp_path) -> None:
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(cli_venv_python="~/custom/bin/python")
    )
    assert dlf.resolve_cli_venv_python(ctx) == tmp_path / "custom" / "bin" / "python"


# ── tui_binary ───────────────────────────────────────────────────────────────


def test_tui_binary_absent_is_ok_not_unknown(tmp_path) -> None:
    result = dlf.probe_tui_binary(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "not present on this machine"
    assert result.values["present"] is False


def test_tui_binary_present_no_source_tree_is_ok(tmp_path) -> None:
    binary = tmp_path / ".local" / "bin" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    result = dlf.probe_tui_binary(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert "not found to compare" in result.headroom
    assert result.values["present"] is True
    assert "source_mtime" not in result.values


def test_tui_binary_newer_than_source_is_ok(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    src = checkout / "tui" / "src"
    src.mkdir(parents=True)
    old = src / "main.rs"
    old.write_text("")
    os.utime(old, (NOW - 3600, NOW - 3600))

    binary = tmp_path / ".local" / "bin" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    os.utime(binary, (NOW, NOW))

    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),)
    )
    result = dlf.probe_tui_binary(ctx)
    assert result.severity is Severity.OK
    assert result.headroom == "up to date with coord-tui source"
    assert result.values["source_mtime"] == pytest.approx(NOW - 3600)


def test_tui_binary_older_than_source_is_warn(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    src = checkout / "tui" / "src"
    src.mkdir(parents=True)
    new = src / "main.rs"
    new.write_text("")
    os.utime(new, (NOW, NOW))

    binary = tmp_path / ".local" / "bin" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    os.utime(binary, (NOW - 9000, NOW - 9000))  # 2.5h before the source

    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),)
    )
    result = dlf.probe_tui_binary(ctx)
    assert result.severity is Severity.WARN
    assert "2.5h older" in result.headroom
    assert "rebuild" in result.detail


def test_tui_binary_exactly_equal_mtimes_is_ok_not_warn(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    src = checkout / "tui" / "src"
    src.mkdir(parents=True)
    rs = src / "main.rs"
    rs.write_text("")
    os.utime(rs, (NOW, NOW))

    binary = tmp_path / ".local" / "bin" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    os.utime(binary, (NOW, NOW))

    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),)
    )
    assert dlf.probe_tui_binary(ctx).severity is Severity.OK


def test_resolve_tui_binary_path_prefers_the_configured_path(tmp_path) -> None:
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(tui_binary_path="~/custom/coord-tui")
    )
    assert dlf.resolve_tui_binary_path(ctx) == tmp_path / "custom" / "coord-tui"


def test_resolve_tui_source_dir_prefers_the_configured_path(tmp_path) -> None:
    configured = tmp_path / "elsewhere" / "src"
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(tui_source_dir=str(configured))
    )
    assert dlf.resolve_tui_source_dir(ctx) == configured


# ── #2899: the crate moved to its own repo ───────────────────────────────────
#
# Before #2899 this lane's only answer was `<checkout>/tui/src`. The crate is
# now the `coord-tui` repo, so the primary answer is that checkout's `src/` —
# with the in-repo layout kept as a fallback, because a machine can still have
# a `claude-coordinator` checkout parked on a pre-split commit and an
# UNKNOWN there would be a loss of signal for no gain.


def test_resolve_tui_source_dir_finds_the_checkout_named_coord_tui(
    tmp_path,
) -> None:
    """A checkout NAMED coord-tui wins, and it is that checkout's `src/` —
    not its root. Rooting the mtime walk at the checkout would sweep
    `target/`, which on a built crate is multi-GB."""
    checkout_a = tmp_path / "a"
    checkout_a.mkdir()
    checkout_b = tmp_path / "coord-tui"
    (checkout_b / "src").mkdir(parents=True)
    ctx = make_ctx(
        tmp_path,
        checkouts=(
            Checkout(name="a", path=checkout_a),
            Checkout(name="coord-tui", path=checkout_b),
        ),
    )
    assert dlf.resolve_tui_source_dir(ctx) == checkout_b / "src"


def test_resolve_tui_source_dir_falls_back_to_the_structural_marker(
    tmp_path,
) -> None:
    """A coord-tui checkout under a different directory name is still found.

    A marker that stops matching turns the lane OFF, and an off lane is
    indistinguishable from a healthy one — the exact failure this whole
    module exists to prevent.
    """
    checkout = tmp_path / "renamed-tui"
    (checkout / "src" / "app").mkdir(parents=True)
    (checkout / dlf.COORD_TUI_MARKER).write_text("// data.rs")
    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="renamed-tui", path=checkout),)
    )
    assert dlf.resolve_tui_source_dir(ctx) == checkout / "src"


def test_resolve_tui_source_dir_marker_does_not_match_a_plain_rust_checkout(
    tmp_path,
) -> None:
    """The marker is a file unique to THIS crate, never a bare `Cargo.toml`.

    Every Rust checkout on the machine (quadraui, vimcode) has a
    `Cargo.toml`; matching on one would point the lane at the wrong tree and
    MANUFACTURE staleness rather than report it.
    """
    other = tmp_path / "quadraui"
    (other / "src").mkdir(parents=True)
    (other / "Cargo.toml").write_text("[package]\nname = \"quadraui\"\n")
    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="quadraui", path=other),))
    assert dlf.resolve_coord_tui_checkout(ctx) is None
    assert dlf.resolve_tui_source_dir(ctx) is None


def test_resolve_coord_tui_checkout_prefers_the_configured_path(tmp_path) -> None:
    configured = tmp_path / "elsewhere"
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(coord_tui_checkout=str(configured))
    )
    assert dlf.resolve_coord_tui_checkout(ctx) == configured


def test_resolve_tui_source_dir_falls_back_to_the_pre_split_layout(
    tmp_path,
) -> None:
    """#2899's back-compat half: a `claude-coordinator` checkout parked on a
    pre-split commit still answers, via `<checkout>/tui/src`."""
    checkout_a = tmp_path / "a"
    checkout_a.mkdir()
    checkout_b = tmp_path / "b"
    (checkout_b / "tui" / "src").mkdir(parents=True)
    ctx = make_ctx(
        tmp_path,
        checkouts=(
            Checkout(name="a", path=checkout_a),
            Checkout(name="b", path=checkout_b),
        ),
    )
    assert dlf.resolve_tui_source_dir(ctx) == checkout_b / "tui" / "src"


def test_resolve_tui_source_dir_prefers_coord_tui_over_the_pre_split_layout(
    tmp_path,
) -> None:
    """Both present — a live coord-tui checkout AND a stale in-repo one — must
    resolve to coord-tui. Comparing the binary against the abandoned tree is
    how this lane reports the OPPOSITE of the truth (#1806)."""
    legacy = tmp_path / "claude-coordinator"
    (legacy / "tui" / "src").mkdir(parents=True)
    current = tmp_path / "coord-tui"
    (current / "src").mkdir(parents=True)
    ctx = make_ctx(
        tmp_path,
        checkouts=(
            Checkout(name="claude-coordinator", path=legacy),
            Checkout(name="coord-tui", path=current),
        ),
    )
    assert dlf.resolve_tui_source_dir(ctx) == current / "src"


def test_resolve_tui_source_dir_is_none_when_no_checkout_has_one(tmp_path) -> None:
    ctx = make_ctx(tmp_path, checkouts=())
    assert dlf.resolve_tui_source_dir(ctx) is None


# ── _newest_rust_source_mtime (moved from coord.health.fleet_snapshot) ───────


def test_newest_rust_source_walk_finds_the_newest_rs_file(tmp_path: Path) -> None:
    src = tmp_path / "tui" / "src"
    (src / "widgets").mkdir(parents=True)
    old = src / "main.rs"
    old.write_text("fn main() {}")
    os.utime(old, (NOW - 10_000, NOW - 10_000))
    new = src / "widgets" / "board.rs"
    new.write_text("pub struct Board;")
    os.utime(new, (NOW, NOW))

    assert dlf._newest_rust_source_mtime(src) == pytest.approx(NOW)


def test_newest_rust_source_walk_skips_target_and_hidden_dirs(tmp_path: Path) -> None:
    """A `tui_source_dir` pointed at a crate root must not let a multi-GB
    `target/` dominate the mtime (or the walk's cost)."""
    src = tmp_path / "tui"
    src.mkdir()
    real = src / "lib.rs"
    real.write_text("")
    os.utime(real, (NOW - 10_000, NOW - 10_000))
    for junk_dir in ("target", ".git"):
        d = src / junk_dir / "deep"
        d.mkdir(parents=True)
        junk = d / "generated.rs"
        junk.write_text("")
        os.utime(junk, (NOW, NOW))

    assert dlf._newest_rust_source_mtime(src) == pytest.approx(NOW - 10_000)


def test_newest_rust_source_walk_on_a_missing_or_empty_dir_is_none(
    tmp_path: Path,
) -> None:
    assert dlf._newest_rust_source_mtime(tmp_path / "nope") is None
    (tmp_path / "empty").mkdir()
    assert dlf._newest_rust_source_mtime(tmp_path / "empty") is None


# ── webapp_bundle (#1834 lane 5) ───────────────────────────────────────────
#
# Same shape as tui_binary above, deliberately: a `dist/` bundle vs. the
# webapp/ source tree it was built from, never a version comparison (see
# this module's docstring for why version is the wrong question here).


def test_webapp_bundle_absent_is_ok_not_warn(tmp_path) -> None:
    """The common case — most machines never run `coord web --dist` — must
    not read as a fault."""
    result = dlf.probe_webapp_bundle(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "not present on this machine"
    assert result.values["present"] is False


def test_webapp_bundle_present_no_source_tree_is_ok(tmp_path) -> None:
    dist = tmp_path / "coord-web-dist"
    dist.mkdir()
    (dist / "index.html").write_text("")
    result = dlf.probe_webapp_bundle(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert "not found to compare" in result.headroom
    assert result.values["present"] is True
    assert "source_mtime" not in result.values


def test_webapp_bundle_newer_than_source_is_ok(tmp_path) -> None:
    checkout = tmp_path / "src" / "coord-web"
    src = checkout / "src"
    src.mkdir(parents=True)
    old = src / "App.tsx"
    old.write_text("")
    os.utime(old, (NOW - 3600, NOW - 3600))

    dist = tmp_path / "coord-web-dist"
    dist.mkdir()
    os.utime(dist, (NOW, NOW))

    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coord-web", path=checkout),)
    )
    result = dlf.probe_webapp_bundle(ctx)
    assert result.severity is Severity.OK
    assert result.headroom == "up to date with webapp/ source"
    assert result.values["source_mtime"] == pytest.approx(NOW - 3600)


def test_webapp_bundle_older_than_source_is_warn(tmp_path) -> None:
    checkout = tmp_path / "src" / "coord-web"
    src = checkout / "src"
    src.mkdir(parents=True)
    new = src / "App.tsx"
    new.write_text("")
    os.utime(new, (NOW, NOW))

    dist = tmp_path / "coord-web-dist"
    dist.mkdir()
    os.utime(dist, (NOW - 9000, NOW - 9000))  # 2.5h before the source

    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coord-web", path=checkout),)
    )
    result = dlf.probe_webapp_bundle(ctx)
    assert result.severity is Severity.WARN
    assert "2.5h older" in result.headroom
    assert "coord-web-dist-build.timer" in result.detail


def test_webapp_bundle_exactly_equal_mtimes_is_ok_not_warn(tmp_path) -> None:
    checkout = tmp_path / "src" / "coord-web"
    src = checkout / "src"
    src.mkdir(parents=True)
    ts = src / "App.tsx"
    ts.write_text("")
    os.utime(ts, (NOW, NOW))

    dist = tmp_path / "coord-web-dist"
    dist.mkdir()
    os.utime(dist, (NOW, NOW))

    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coord-web", path=checkout),)
    )
    assert dlf.probe_webapp_bundle(ctx).severity is Severity.OK


def test_webapp_bundle_source_walk_skips_node_modules_and_dist(tmp_path) -> None:
    """A stale `dist/` sitting inside the source checkout, or a huge
    `node_modules/`, must never be allowed to masquerade as "source"."""
    checkout = tmp_path / "src" / "coord-web"
    src = checkout / "src"
    src.mkdir(parents=True)
    real = src / "App.tsx"
    real.write_text("")
    os.utime(real, (NOW - 10_000, NOW - 10_000))
    for junk_dir in ("node_modules", "dist"):
        d = src / junk_dir / "deep"
        d.mkdir(parents=True)
        junk = d / "generated.js"
        junk.write_text("")
        os.utime(junk, (NOW, NOW))

    assert dlf._newest_webapp_source_mtime(src) == pytest.approx(NOW - 10_000)


def test_webapp_bundle_reports_the_live_sha_from_the_symlink_target(tmp_path) -> None:
    releases = tmp_path / "releases"
    release_dir = releases / "abc123"
    release_dir.mkdir(parents=True)
    dist = tmp_path / "coord-web-dist"
    dist.symlink_to(release_dir)

    result = dlf.probe_webapp_bundle(make_ctx(tmp_path))
    assert result.values["present"] is True
    assert result.values["sha"] == "abc123"


def test_resolve_webapp_dist_path_prefers_the_configured_path(tmp_path) -> None:
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(webapp_dist_path="~/custom/dist")
    )
    assert dlf.resolve_webapp_dist_path(ctx) == tmp_path / "custom" / "dist"


def test_resolve_webapp_source_dir_prefers_the_configured_path(tmp_path) -> None:
    configured = tmp_path / "elsewhere" / "src"
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(webapp_source_dir=str(configured))
    )
    assert dlf.resolve_webapp_source_dir(ctx) == configured


def test_resolve_webapp_source_dir_finds_the_checkout_named_coord_web(
    tmp_path,
) -> None:
    """#2470: discovery defers to the same `coord-web`-checkout resolution
    `coord_web_ci_pin` uses -- a checkout literally named `coord-web` wins,
    same as that check."""
    checkout_a = tmp_path / "a"
    checkout_a.mkdir()
    checkout_b = tmp_path / "coord-web"
    (checkout_b / "src").mkdir(parents=True)
    ctx = make_ctx(
        tmp_path,
        checkouts=(
            Checkout(name="a", path=checkout_a),
            Checkout(name="coord-web", path=checkout_b),
        ),
    )
    assert dlf.resolve_webapp_source_dir(ctx) == checkout_b / "src"


def test_resolve_webapp_source_dir_falls_back_to_the_playwright_marker(
    tmp_path,
) -> None:
    """A `coord-web` checkout under a different directory/repo name is still
    found via the structural marker `coord_web_ci_pin` already uses
    (`playwright.acceptance.config.ts` at the checkout root) -- a rename
    must not silently turn this lane off."""
    checkout = tmp_path / "webui"
    (checkout / "src").mkdir(parents=True)
    (checkout / "playwright.acceptance.config.ts").write_text("")
    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="webui", path=checkout),)
    )
    assert dlf.resolve_webapp_source_dir(ctx) == checkout / "src"


def test_resolve_webapp_source_dir_is_none_when_no_checkout_has_one(tmp_path) -> None:
    ctx = make_ctx(tmp_path, checkouts=())
    assert dlf.resolve_webapp_source_dir(ctx) is None


def test_resolve_webapp_source_dir_is_none_when_coord_web_checkout_has_no_src(
    tmp_path,
) -> None:
    """The `coord-web` checkout is found, but it has no `src/` (e.g. not yet
    cloned deep enough, or a genuinely different layout) -- absent, not a
    crash, same as every other lane in this module."""
    checkout = tmp_path / "coord-web"
    checkout.mkdir(parents=True)
    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coord-web", path=checkout),)
    )
    assert dlf.resolve_webapp_source_dir(ctx) is None


def test_resolve_webapp_source_dir_falls_back_to_the_pre_split_layout(
    tmp_path,
) -> None:
    """#2009: no checkout is named/marked `coord-web`, but a
    `claude-coordinator` checkout still parked on a pre-split commit has the
    old in-repo layout -- this lane must still find it rather than regress
    to UNKNOWN just because the machine hasn't pulled the split yet."""
    checkout = tmp_path / "claude-coordinator"
    (checkout / "coord" / "dashboard" / "webapp" / "src").mkdir(parents=True)
    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="claude-coordinator", path=checkout),)
    )
    assert (
        dlf.resolve_webapp_source_dir(ctx)
        == checkout / "coord" / "dashboard" / "webapp" / "src"
    )


# ── webapp_build_heartbeat (#2122) ─────────────────────────────────────────
#
# coord-web-dist-build.sh's up-to-date tick deliberately stopped logging
# (see that script's header) so the timer's journal footprint doesn't grow
# unbounded at its own cadence -- but that means the journal alone can no
# longer answer "is the timer still firing?". This heartbeat file, written
# on every tick regardless of outcome, is the surface that closes that gap.


def _heartbeat_path(tmp_path: Path) -> Path:
    return tmp_path / ".coord-web-releases" / ".last-run-at"


def test_webapp_build_heartbeat_absent_is_ok_not_warn(tmp_path) -> None:
    """The overwhelming common case: most machines never run
    coord-web-dist-build.timer at all -- absent must read as OK, same
    convention as every other lane in this module, not as a fault."""
    ctx = make_ctx(tmp_path)
    result = dlf.probe_webapp_build_heartbeat(ctx)
    assert result.severity is Severity.OK
    assert result.values["present"] is False


def test_webapp_build_heartbeat_recent_up_to_date_is_ok(tmp_path) -> None:
    path = _heartbeat_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(f"{NOW - 60} up-to-date c339ced8\n")

    ctx = make_ctx(tmp_path)
    result = dlf.probe_webapp_build_heartbeat(ctx)
    assert result.severity is Severity.OK
    assert "1m ago" in result.headroom
    assert result.values["status"] == "up-to-date"
    assert result.values["sha"] == "c339ced8"


def test_webapp_build_heartbeat_recent_published_is_ok(tmp_path) -> None:
    path = _heartbeat_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(f"{NOW - 30} published deadbeef\n")

    ctx = make_ctx(tmp_path)
    result = dlf.probe_webapp_build_heartbeat(ctx)
    assert result.severity is Severity.OK
    assert "published" in result.headroom


def test_webapp_build_heartbeat_past_warn_threshold_is_warn(tmp_path) -> None:
    path = _heartbeat_path(tmp_path)
    path.parent.mkdir(parents=True)
    # 45min old; default warn is 30min.
    path.write_text(f"{NOW - 45 * 60} up-to-date c339ced8\n")

    ctx = make_ctx(tmp_path)
    result = dlf.probe_webapp_build_heartbeat(ctx)
    assert result.severity is Severity.WARN
    assert "coord-web-dist-build.timer" in result.detail


def test_webapp_build_heartbeat_past_crit_threshold_is_crit(tmp_path) -> None:
    path = _heartbeat_path(tmp_path)
    path.parent.mkdir(parents=True)
    # 4 hours old; default crit is 180min (3h).
    path.write_text(f"{NOW - 4 * 3600} error c339ced8\n")

    ctx = make_ctx(tmp_path)
    result = dlf.probe_webapp_build_heartbeat(ctx)
    assert result.severity is Severity.CRIT
    assert "has not fired" in result.detail


def test_webapp_build_heartbeat_respects_configured_thresholds(tmp_path) -> None:
    """A dead trigger must be detectable on a schedule that matches
    whatever cadence THIS install actually configured, not just the
    shipped default -- an operator who widens the timer's own cadence
    (or narrows it) needs the heartbeat thresholds to move with it."""
    path = _heartbeat_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(f"{NOW - 5 * 60} up-to-date c339ced8\n")

    ctx = make_ctx(
        tmp_path,
        thresholds=HealthConfig(
            webapp_build_heartbeat_warn_minutes=1.0,
            webapp_build_heartbeat_crit_minutes=2.0,
        ),
    )
    result = dlf.probe_webapp_build_heartbeat(ctx)
    assert result.severity is Severity.CRIT


def test_webapp_build_heartbeat_unparseable_is_unknown(tmp_path) -> None:
    path = _heartbeat_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not a heartbeat line\n")

    ctx = make_ctx(tmp_path)
    result = dlf.probe_webapp_build_heartbeat(ctx)
    assert result.severity is Severity.UNKNOWN


def test_webapp_build_heartbeat_empty_file_is_unknown(tmp_path) -> None:
    path = _heartbeat_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("")

    ctx = make_ctx(tmp_path)
    result = dlf.probe_webapp_build_heartbeat(ctx)
    assert result.severity is Severity.UNKNOWN


def test_resolve_webapp_build_heartbeat_path_default(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    assert dlf.resolve_webapp_build_heartbeat_path(ctx) == _heartbeat_path(tmp_path)


def test_resolve_webapp_build_heartbeat_path_prefers_the_configured_path(
    tmp_path,
) -> None:
    configured = tmp_path / "elsewhere" / ".last-run-at"
    ctx = make_ctx(
        tmp_path,
        thresholds=HealthConfig(webapp_build_heartbeat_path=str(configured)),
    )
    assert dlf.resolve_webapp_build_heartbeat_path(ctx) == configured
