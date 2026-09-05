"""Unit tests for the `config_drift` machine-scope health check (#3120).

Rung D1 of epic #3117: the daemon host's `~/.coord/coordinator.yml` is a
symlink into a private `coord-settings` checkout — the fleet's only off-box
copy of its own config — and this check fails loudly when that checkout has
uncommitted or unpushed drift.

Git itself is faked (same convention as `test_health_checks.py`'s
`_fake_git`/`repo_state` tests): every `subprocess.run` call is intercepted
and answered from a table keyed on the git subcommand, so no test here
touches a real git binary. The filesystem side (symlink vs. regular file
resolution) uses real `tmp_path` files, because that resolution is exactly
the behaviour #3120 calls out as fragile.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from coord.config import HealthConfig
from coord.health.checks import config_drift
from coord.health.models import HealthContext, Severity

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


def _fake_git(monkeypatch, responses: dict[tuple[str, ...], tuple[int, str]], calls=None):
    """Answer every `git -C <dir> <args...>` from *responses*, keyed on `<args...>`.

    *calls*, if given, collects every full argv so a test can assert on what
    was actually invoked (the "never pushes" guarantee).
    """

    def _run(cmd, **kwargs):
        if calls is not None:
            calls.append(list(cmd))
        key = tuple(cmd[3:])  # drop ["git", "-C", <path>]
        code, out = responses.get(key, (1, ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(config_drift.subprocess, "run", _run)


_WORK_TREE = {("rev-parse", "--is-inside-work-tree"): (0, "true\n")}
_CLEAN = {("status", "--porcelain"): (0, "")}
_UPSTREAM_OK = {
    ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main\n")
}
_NO_UNPUSHED = {("rev-list", "--count", "@{u}..HEAD"): (0, "0\n")}


def _write_config(tmp_path: Path, coord_dir: Path | None = None) -> Path:
    """A plain (non-symlinked) `coordinator.yml` under `coord_dir`."""
    coord_dir = coord_dir or (tmp_path / ".coord")
    coord_dir.mkdir(parents=True, exist_ok=True)
    cfg = coord_dir / "coordinator.yml"
    cfg.write_text("machines: []\n")
    return cfg


def _write_symlinked_config(tmp_path: Path) -> tuple[Path, Path]:
    """`~/.coord/coordinator.yml` as a symlink into a `coord-settings`-shaped repo.

    Returns (coord_dir, real_repo_dir).
    """
    repo_dir = tmp_path / "src" / "coord-settings" / "coord"
    repo_dir.mkdir(parents=True)
    real_cfg = repo_dir / "coordinator.yml"
    real_cfg.write_text("machines: []\n")
    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    (coord_dir / "coordinator.yml").symlink_to(real_cfg)
    return coord_dir, repo_dir


# ── clean / pushed ───────────────────────────────────────────────────────────


def test_clean_and_pushed_is_ok(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    _fake_git(monkeypatch, {**_WORK_TREE, **_CLEAN, **_UPSTREAM_OK, **_NO_UNPUSHED})
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert "clean" in result.headroom


# ── dirty tree ───────────────────────────────────────────────────────────────


def test_dirty_tree_warns_and_names_file_count(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    _fake_git(
        monkeypatch,
        {
            **_WORK_TREE,
            **_UPSTREAM_OK,
            **_NO_UNPUSHED,
            ("status", "--porcelain"): (0, " M coordinator.yml\n?? scratch.txt\n"),
        },
    )
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.WARN
    assert result.values["dirty_count"] == 2
    assert "2 uncommitted change" in result.headroom


# ── unpushed commit(s) ───────────────────────────────────────────────────────


def test_unpushed_commit_warns_and_names_commit_count(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    recent_ts = int(NOW) - 2 * 3600  # 2h old — under the 24h default crit
    _fake_git(
        monkeypatch,
        {
            **_WORK_TREE,
            **_CLEAN,
            **_UPSTREAM_OK,
            ("rev-list", "--count", "@{u}..HEAD"): (0, "2\n"),
            ("log", "--format=%ct", "@{u}..HEAD"): (0, f"{recent_ts}\n{recent_ts}\n"),
        },
    )
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.WARN
    assert result.values["unpushed_count"] == 2
    assert "2 unpushed commit" in result.headroom


def test_old_unpushed_commit_is_crit(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    old_ts = int(NOW) - 30 * 3600  # 30h old — past the 24h default crit
    _fake_git(
        monkeypatch,
        {
            **_WORK_TREE,
            **_CLEAN,
            **_UPSTREAM_OK,
            ("rev-list", "--count", "@{u}..HEAD"): (0, "1\n"),
            ("log", "--format=%ct", "@{u}..HEAD"): (0, f"{old_ts}\n"),
        },
    )
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.CRIT
    assert "1 unpushed commit" in result.headroom
    assert "30.0h" in result.headroom


def test_crit_age_threshold_is_configurable(tmp_path, monkeypatch) -> None:
    """A 1h-old commit is WARN by default but CRIT under a tighter threshold."""
    _write_config(tmp_path)
    ts = int(NOW) - 3600
    _fake_git(
        monkeypatch,
        {
            **_WORK_TREE,
            **_CLEAN,
            **_UPSTREAM_OK,
            ("rev-list", "--count", "@{u}..HEAD"): (0, "1\n"),
            ("log", "--format=%ct", "@{u}..HEAD"): (0, f"{ts}\n"),
        },
    )
    default_result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert default_result.severity is Severity.WARN

    strict_ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(config_drift_crit_hours=0.5)
    )
    strict_result = config_drift.probe_config_drift(strict_ctx)
    assert strict_result.severity is Severity.CRIT


# ── no git work tree at all ──────────────────────────────────────────────────


def test_config_outside_git_work_tree_is_warn_not_ok(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    _fake_git(monkeypatch, {})  # rev-parse --is-inside-work-tree fails (no entry)
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.WARN
    assert "not inside a git work tree" in result.headroom


# ── no upstream configured ───────────────────────────────────────────────────


def test_no_upstream_is_warn_not_crash_or_clean(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    _fake_git(monkeypatch, {**_WORK_TREE, **_CLEAN})  # @{u} lookup absent -> fails
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.WARN
    assert "no upstream" in result.headroom
    assert result.values["has_upstream"] is False


# ── symlink vs. regular file resolution ─────────────────────────────────────


def test_resolves_symlinked_config_before_asking_git(tmp_path, monkeypatch) -> None:
    coord_dir, repo_dir = _write_symlinked_config(tmp_path)
    assert (coord_dir / "coordinator.yml").is_symlink()
    calls: list[list[str]] = []
    _fake_git(
        monkeypatch,
        {**_WORK_TREE, **_CLEAN, **_UPSTREAM_OK, **_NO_UNPUSHED},
        calls=calls,
    )
    ctx = make_ctx(tmp_path, coord_dir=coord_dir)
    result = config_drift.probe_config_drift(ctx)
    assert result.severity is Severity.OK
    assert result.values["real_path"] == str(repo_dir / "coordinator.yml")
    # git was asked about the resolved repo dir, not the ~/.coord symlink dir.
    assert all(str(coord_dir) not in call for call in calls)
    assert any(str(repo_dir) in call for call in calls)


def test_regular_file_config_behaves_identically_to_symlink(tmp_path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    _fake_git(monkeypatch, {**_WORK_TREE, **_CLEAN, **_UPSTREAM_OK, **_NO_UNPUSHED})
    ctx = make_ctx(tmp_path)
    result = config_drift.probe_config_drift(ctx)
    assert result.severity is Severity.OK
    assert result.values["real_path"] == str(cfg.resolve())


# ── never pushes ─────────────────────────────────────────────────────────────


def test_never_invokes_push_commit_or_add(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    calls: list[list[str]] = []
    old_ts = int(NOW) - 30 * 3600
    _fake_git(
        monkeypatch,
        {
            **_WORK_TREE,
            ("status", "--porcelain"): (0, " M coordinator.yml\n"),
            **_UPSTREAM_OK,
            ("rev-list", "--count", "@{u}..HEAD"): (0, "1\n"),
            ("log", "--format=%ct", "@{u}..HEAD"): (0, f"{old_ts}\n"),
        },
        calls=calls,
    )
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.CRIT  # sanity: the full path actually ran
    assert calls, "expected the probe to invoke git at all"
    for call in calls:
        subcommand = call[3] if len(call) > 3 else None
        assert subcommand not in {"push", "commit", "add"}, call


# ── missing config file ──────────────────────────────────────────────────────


def test_missing_config_file_is_unknown(tmp_path, monkeypatch) -> None:
    _fake_git(monkeypatch, {})
    ctx = make_ctx(tmp_path)  # nothing written under coord_dir
    result = config_drift.probe_config_drift(ctx)
    assert result.severity is Severity.UNKNOWN


# ── failed git calls never read as clean ────────────────────────────────────


def test_failed_git_status_is_unknown_not_clean(tmp_path, monkeypatch) -> None:
    """A `git status` failure (lock contention, timeout, ...) must not be
    silently treated as "nothing dirty" — that would report a false OK on
    exactly the failure modes (lock contention, disk pressure) most likely
    to coincide with real drift on a busy daemon host."""
    _write_config(tmp_path)
    _fake_git(
        monkeypatch,
        {
            **_WORK_TREE,
            ("status", "--porcelain"): (128, "fatal: Unable to create '.git/index.lock'"),
        },
    )
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert result.severity is not Severity.OK
    assert "git status" in result.headroom
    assert result.error


def test_failed_rev_list_is_unknown_not_silently_zero(tmp_path, monkeypatch) -> None:
    """If `rev-list --count` fails, the unpushed count must not silently
    default to 0 (which would combine with a clean tree to report OK)."""
    _write_config(tmp_path)
    _fake_git(
        monkeypatch,
        {
            **_WORK_TREE,
            **_CLEAN,
            **_UPSTREAM_OK,
            ("rev-list", "--count", "@{u}..HEAD"): (1, "fatal: ambiguous argument"),
        },
    )
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert result.values["unpushed_count"] is None
    assert "could not determine unpushed commit count" in result.headroom


def test_failed_log_reports_unknown_age_not_zero(tmp_path, monkeypatch) -> None:
    """If `log --format=%ct` fails after a nonzero unpushed count is known,
    the commit must not be silently graded as 0h old (WARN instead of the
    CRIT it might actually deserve)."""
    _write_config(tmp_path)
    _fake_git(
        monkeypatch,
        {
            **_WORK_TREE,
            **_CLEAN,
            **_UPSTREAM_OK,
            ("rev-list", "--count", "@{u}..HEAD"): (0, "1\n"),
            ("log", "--format=%ct", "@{u}..HEAD"): (1, "fatal: bad revision"),
        },
    )
    result = config_drift.probe_config_drift(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert result.values["unpushed_count"] == 1
    assert result.values["oldest_unpushed_age_hours"] is None
    assert "could not determine age" in result.headroom
