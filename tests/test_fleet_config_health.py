"""Unit tests for :mod:`coord.fleet_config_health` — #1779.

Drives real git checkouts (a fake ``coord-settings`` checkout + a fake live
``coordinator.yml`` path) so the symlink/dirty/behind-origin detection is
exercised against real filesystem + git state, not mocks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coord.config import Config, SmokeRule, SmokeTestsConfig
from coord.fleet_config_health import (
    CapabilityRuleFinding,
    ConfigProvenance,
    FeatureCoverageFinding,
    capability_rule_health,
    capability_rule_summary_line,
    config_provenance,
    default_live_config_path,
    default_settings_dir,
    feature_coverage_findings,
    feature_coverage_summary_line,
    format_capability_rule_lines,
    format_feature_coverage_lines,
    format_provenance_lines,
    local_repo_checkouts,
    summary_line,
    unclaimed_capability_requirements,
)
from coord.models import Machine, Repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# ── helpers ──────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _make_checkout(root: Path, *, push: bool = True) -> Path:
    """A coord-settings checkout with a tracked coord/coordinator.yml,
    optionally with an ``origin`` remote-tracking ref already recorded
    locally (as if a prior ``git fetch``/``push`` happened — no network call
    is ever made by the code under test)."""
    checkout = root / "coord-settings"
    checkout.mkdir(parents=True)
    _git("init", "-q", ".", cwd=checkout)
    (checkout / "coord").mkdir()
    (checkout / "coord" / "coordinator.yml").write_text(
        "repos: []\nmachines: []\n", encoding="utf-8"
    )
    _git("add", "-A", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    if push:
        remote = root / "coord-settings-remote.git"
        _git("init", "-q", "--bare", str(remote), cwd=root)
        _git("remote", "add", "origin", str(remote), cwd=checkout)
        _git("push", "-q", "-u", "origin", "HEAD:main", cwd=checkout)
    return checkout


def _make_repo_checkout(root: Path, name: str, files: list[str]) -> Path:
    """A minimal git checkout at ``root/name`` with each of *files* created
    and committed (tracked) — the fixture the #2953 dead-rule detector reads
    via ``git ls-files``."""
    checkout = root / name
    checkout.mkdir(parents=True)
    for rel in files:
        p = checkout / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    _git("init", "-q", ".", cwd=checkout)
    _git("add", "-A", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    return checkout


# ── env-resolution helpers ───────────────────────────────────────────────────


def test_default_settings_dir_honours_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COORD_SETTINGS_DIR", str(tmp_path / "custom"))
    assert default_settings_dir() == tmp_path / "custom"


def test_default_settings_dir_falls_back_to_home_src(monkeypatch) -> None:
    monkeypatch.delenv("COORD_SETTINGS_DIR", raising=False)
    assert default_settings_dir() == Path.home() / "src" / "coord-settings"


def test_default_live_config_path_honours_coord_config_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "coordinator.yml"))
    assert default_live_config_path() == tmp_path / "coordinator.yml"


def test_default_live_config_path_falls_back_to_home_coord(monkeypatch) -> None:
    monkeypatch.delenv("COORD_CONFIG", raising=False)
    assert default_live_config_path() == Path.home() / ".coord" / "coordinator.yml"


# ── config_provenance: the four states ───────────────────────────────────────


def test_no_checkout_is_a_neutral_skip(tmp_path: Path) -> None:
    """#1779 acceptance: a machine with no coord-settings checkout at all
    (every agent/thin-client/ephemeral worker) must report skip, not a
    warning — this is the expected shape almost everywhere."""
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    prov = config_provenance(live_path=live, checkout_dir=tmp_path / "no-such-checkout")

    assert prov.checkout_present is False
    assert prov.skip is True
    assert prov.regression is False
    assert prov.healthy is False  # not healthy, but distinctly not a regression either

    lines = format_provenance_lines(prov)
    assert len(lines) == 1
    assert "no coord-settings checkout" in lines[0]
    assert "✗" not in lines[0] and "⚠" not in lines[0]
    assert summary_line(prov) == "CONFIG_PROVENANCE: checkout=absent skip=true"


def test_live_config_that_is_not_a_symlink_is_a_named_regression(tmp_path: Path) -> None:
    """#1779 acceptance: the highest-value finding. `coord init`, scp, or an
    editor writing-and-renaming silently swaps the symlink for a plain file —
    the fleet is running an untracked config again, and this must be reported
    distinctly and prominently, not folded into a generic 'drift' line."""
    checkout = _make_checkout(tmp_path)
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.write_text("repos: []\n", encoding="utf-8")  # plain file — the regression

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.checkout_present is True
    assert prov.is_symlink is False
    assert prov.in_checkout is False
    assert prov.regression is True
    assert prov.skip is False

    lines = format_provenance_lines(prov)
    assert len(lines) == 1
    assert "REGRESSION" in lines[0]
    assert "not a symlink" in lines[0].lower() or "NOT a symlink" in lines[0]
    assert "REGULAR FILE" in lines[0]
    assert summary_line(prov) == (
        "CONFIG_PROVENANCE: checkout=present symlinked=false dirty=false behind=0 ahead=0"
    )


def test_live_config_missing_entirely_is_also_a_regression(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    live = tmp_path / "home" / ".coord" / "coordinator.yml"  # never created

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.regression is True
    lines = format_provenance_lines(prov)
    assert "does not exist" in lines[0]


@pytest.mark.skipif(shutil.which("sed") is None, reason="sed not available")
def test_sed_i_over_the_symlink_breaks_it_and_is_detected(tmp_path: Path) -> None:
    """#1832: the exact reproduction from the incident. `sed -i` (and most
    editors) do NOT write through a symlink — they write a temp file and
    `rename()` it over the target, which replaces the symlink itself with a
    plain file. Nothing about that operation errors, and the content still
    matches the checkout, so this is the one case that must be caught by
    inspecting the live path's *kind*, not its content."""
    checkout = _make_checkout(tmp_path)
    live = _symlinked_live(tmp_path, checkout)
    assert live.is_symlink()  # sanity: starts as a real symlink

    subprocess.run(
        ["sed", "-i", "s/repos/repos/", str(live)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10.0,
    )

    # The `sed -i`-equivalent write silently swapped the symlink for a
    # regular file with matching content.
    assert live.is_symlink() is False
    assert live.read_text(encoding="utf-8") == (checkout / "coord" / "coordinator.yml").read_text(
        encoding="utf-8"
    )

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.is_symlink is False
    assert prov.in_checkout is False
    assert prov.regression is True

    lines = format_provenance_lines(prov)
    assert len(lines) == 1
    assert "REGRESSION" in lines[0]
    assert "NOT a symlink" in lines[0]
    assert "REGULAR FILE" in lines[0]
    assert summary_line(prov) == (
        "CONFIG_PROVENANCE: checkout=present symlinked=false dirty=false behind=0 ahead=0"
    )


def test_symlink_pointing_outside_the_checkout_is_a_regression(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    other = tmp_path / "unrelated.yml"
    other.write_text("repos: []\n", encoding="utf-8")
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.symlink_to(other)

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.is_symlink is True
    assert prov.in_checkout is False
    assert prov.regression is True
    lines = format_provenance_lines(prov)
    assert "REGRESSION" in lines[0]
    assert "is a symlink" in lines[0]


def _symlinked_live(tmp_path: Path, checkout: Path) -> Path:
    live = tmp_path / "home" / ".coord" / "coordinator.yml"
    live.parent.mkdir(parents=True)
    live.symlink_to(checkout / "coord" / "coordinator.yml")
    return live


def test_clean_in_sync_symlinked_config_is_fully_healthy(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    live = _symlinked_live(tmp_path, checkout)

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.regression is False
    assert prov.dirty is False
    assert prov.behind == 0
    assert prov.ahead == 0
    assert prov.healthy is True

    lines = format_provenance_lines(prov)
    joined = "\n".join(lines)
    assert "symlinked into the checkout" in joined
    assert "checkout is clean" in joined
    assert "in sync with" in joined
    assert summary_line(prov) == (
        "CONFIG_PROVENANCE: checkout=present symlinked=true dirty=false behind=0 ahead=0"
    )


def test_uncommitted_edit_through_the_symlink_is_reported_as_dirty(tmp_path: Path) -> None:
    """#1779: a direct edit to the live path writes THROUGH the symlink into
    the checkout's working tree — recoverable, but must be reported
    distinctly from the not-a-symlink regression and from behind-origin."""
    checkout = _make_checkout(tmp_path)
    live = _symlinked_live(tmp_path, checkout)
    # Edit through the symlink, as an operator hand-editing the live path would.
    live.write_text("repos: []\nmachines: []\n# local edit\n", encoding="utf-8")

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.regression is False
    assert prov.dirty is True
    assert prov.dirty_files
    assert prov.healthy is False

    lines = format_provenance_lines(prov)
    joined = "\n".join(lines)
    assert "uncommitted changes" in joined
    assert "REGRESSION" not in joined
    assert summary_line(prov).startswith("CONFIG_PROVENANCE: checkout=present symlinked=true dirty=true")


def test_checkout_behind_origin_is_reported_distinctly(tmp_path: Path) -> None:
    """#1779: someone pushed a reviewed config change that was never pulled
    onto this host — the reviewed intent and the running fleet disagree.
    Must not require a fetch: the remote-tracking ref from the earlier push
    already recorded it locally."""
    checkout = _make_checkout(tmp_path)
    live = _symlinked_live(tmp_path, checkout)
    # A second, pushed commit the local checkout hasn't pulled.
    (checkout / "coord" / "coordinator.yml").write_text(
        "repos: []\nmachines: []\n# newer\n", encoding="utf-8"
    )
    _git("commit", "-q", "-am", "newer config", cwd=checkout)
    # Explicit refspec: the local branch (whatever `git init`'s default is,
    # e.g. "master") and the remote branch ("main") don't share a name, so a
    # bare `git push` would refuse under push.default=simple/current.
    _git("push", "-q", "origin", "HEAD:main", cwd=checkout)
    _git("reset", "-q", "--hard", "HEAD~1", cwd=checkout)  # local host never pulled it

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.regression is False
    assert prov.dirty is False
    assert prov.behind == 1
    assert prov.ahead == 0
    assert prov.healthy is False

    lines = format_provenance_lines(prov)
    joined = "\n".join(lines)
    assert "behind" in joined
    assert "not yet deployed" in joined
    assert "uncommitted changes" not in joined
    assert "REGRESSION" not in joined
    assert summary_line(prov) == (
        "CONFIG_PROVENANCE: checkout=present symlinked=true dirty=false behind=1 ahead=0"
    )


def test_checkout_ahead_of_origin_is_reported(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    live = _symlinked_live(tmp_path, checkout)
    (checkout / "coord" / "coordinator.yml").write_text(
        "repos: []\nmachines: []\n# not yet pushed\n", encoding="utf-8"
    )
    _git("commit", "-q", "-am", "local only", cwd=checkout)

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.behind == 0
    assert prov.ahead == 1
    lines = format_provenance_lines(prov)
    assert "ahead of" in "\n".join(lines)


def test_no_upstream_configured_reports_sync_unknown_not_healthy(tmp_path: Path) -> None:
    """No remote at all (e.g. a checkout cloned without one, or origin
    removed) must surface as unknown, never as a false 'in sync'."""
    checkout = _make_checkout(tmp_path, push=False)
    live = _symlinked_live(tmp_path, checkout)

    prov = config_provenance(live_path=live, checkout_dir=checkout)

    assert prov.sync_unknown_reason is not None
    assert prov.in_sync is False
    assert prov.healthy is False
    lines = format_provenance_lines(prov)
    assert "sync vs origin unknown" in "\n".join(lines)


def test_remote_tracking_config_is_never_the_live_path() -> None:
    """#1779 acceptance: coordinator.remote.yml (the thin-client GET /config
    cache — coord.client.REMOTE_CONFIG_CACHE) must never be inspected here.
    Static guard: the default live path resolution can never point at it."""
    assert default_live_config_path().name != "coordinator.remote.yml"
    assert default_live_config_path().name == "coordinator.yml"


def test_config_provenance_dataclass_defaults_are_unhealthy_and_not_skipped() -> None:
    """Sanity: a bare ConfigProvenance (as if something forgot to populate
    it) must not silently read as skip=True or healthy=True."""
    prov = ConfigProvenance(live_path=Path("/x"), checkout_dir=Path("/y"))
    assert prov.skip is True  # checkout_present defaults False -> correctly a skip
    assert prov.healthy is False


# ── capability_rule_health: dead/partial capability_rules prefixes (#2953) ──


def _config(repos: list[Repo], rules: list[SmokeRule], *, machines: list[Machine] | None = None) -> Config:
    return Config(
        repos=repos,
        machines=machines if machines is not None else [],
        smoke_tests=SmokeTestsConfig(capability_rules=rules),
    )


def test_capability_rule_health_empty_when_no_rules_configured(tmp_path: Path) -> None:
    cfg = _config([Repo(name="vimcode", github="acme/vimcode")], [])
    assert capability_rule_health(cfg) == []


def test_capability_rule_health_dead_matches_nothing_anywhere(tmp_path: Path) -> None:
    """#1072's exact shape: a stray `**`-suffixed prefix under a plain-prefix
    matcher matches no real path, in any repo — the loudest finding."""
    coordinator = _make_repo_checkout(
        tmp_path, "code-coordinator", ["coord/dashboard/webapp/src/App.tsx"]
    )
    other = _make_repo_checkout(tmp_path, "other-repo", ["src/main.py"])
    cfg = _config(
        [Repo(name="code-coordinator", github="x/code-coordinator"),
         Repo(name="other-repo", github="x/other-repo")],
        [SmokeRule(files=["coord/dashboard/webapp/**"], requires=["browser"])],
    )

    findings = capability_rule_health(
        cfg, repo_checkouts={"code-coordinator": coordinator, "other-repo": other}
    )

    assert len(findings) == 1
    f = findings[0]
    assert f.prefix == "coord/dashboard/webapp/**"
    assert f.dead is True
    assert f.partial is False
    assert f.healthy is False
    assert f.matched_repos == ()
    assert set(f.checked_repos) == {"code-coordinator", "other-repo"}

    lines = format_capability_rule_lines(findings, [])
    joined = "\n".join(lines)
    assert "DEAD" in joined
    assert "coord/dashboard/webapp/**" in joined
    assert capability_rule_summary_line(findings, []) == (
        "CAPABILITY_RULES: prefixes=1 dead=1 partial=0 unclaimed_caps=0"
    )


def test_capability_rule_health_partial_when_prefix_missing_a_directory_level(
    tmp_path: Path,
) -> None:
    """#2953's exact shape: `src/gtk/` matches vimcode's layout but not
    quadraui's, where the GTK backend is nested one level deeper under the
    crate directory. A bare "matches somewhere" check would call this
    healthy; the deeper-occurrence check must not."""
    vimcode = _make_repo_checkout(tmp_path, "vimcode", ["src/gtk/window.c"])
    quadraui = _make_repo_checkout(
        tmp_path, "quadraui", ["quadraui/src/gtk/backend.rs"]
    )
    cfg = _config(
        [Repo(name="vimcode", github="x/vimcode"), Repo(name="quadraui", github="x/quadraui")],
        [SmokeRule(files=["src/gtk/"], requires=["gtk"])],
    )

    findings = capability_rule_health(
        cfg, repo_checkouts={"vimcode": vimcode, "quadraui": quadraui}
    )

    assert len(findings) == 1
    f = findings[0]
    assert f.matched_repos == ("vimcode",)
    assert f.suspect_repos == ("quadraui",)
    assert f.dead is False
    assert f.partial is True
    assert f.healthy is False

    lines = format_capability_rule_lines(findings, [])
    joined = "\n".join(lines)
    assert "PARTIAL" in joined
    assert "vimcode" in joined
    assert "quadraui" in joined
    assert capability_rule_summary_line(findings, []) == (
        "CAPABILITY_RULES: prefixes=1 dead=0 partial=1 unclaimed_caps=0"
    )


def test_capability_rule_health_healthy_when_prefix_matches_everywhere_checked(
    tmp_path: Path,
) -> None:
    """A rule matching in every repo it was checked against is silent — no
    dead/partial finding, matching the acceptance bar: 'A rule whose prefix
    matches in every repo it plausibly targets is silent.'"""
    repo_a = _make_repo_checkout(tmp_path, "repo-a", ["shared/thing.py"])
    repo_b = _make_repo_checkout(tmp_path, "repo-b", ["shared/other.py"])
    cfg = _config(
        [Repo(name="repo-a", github="x/repo-a"), Repo(name="repo-b", github="x/repo-b")],
        [SmokeRule(files=["shared/"], requires=["python"])],
    )

    findings = capability_rule_health(
        cfg, repo_checkouts={"repo-a": repo_a, "repo-b": repo_b}
    )

    assert len(findings) == 1
    f = findings[0]
    assert f.healthy is True
    assert f.dead is False
    assert f.partial is False
    assert set(f.matched_repos) == {"repo-a", "repo-b"}

    lines = format_capability_rule_lines(findings, [])
    joined = "\n".join(lines)
    assert "DEAD" not in joined
    assert "PARTIAL" not in joined
    assert "✓" in joined


def test_capability_rule_health_skips_repos_with_no_local_checkout(tmp_path: Path) -> None:
    """#1779's precedent, applied per repo: a repo with no checkout present
    on this machine is skipped, never counted toward dead — acceptance:
    'Absent checkouts are skipped, not reported as dead.'"""
    vimcode = _make_repo_checkout(tmp_path, "vimcode", ["src/gtk/window.c"])
    cfg = _config(
        [Repo(name="vimcode", github="x/vimcode"), Repo(name="quadraui", github="x/quadraui")],
        [SmokeRule(files=["src/gtk/"], requires=["gtk"])],
    )

    # quadraui deliberately absent from repo_checkouts — no local checkout.
    findings = capability_rule_health(cfg, repo_checkouts={"vimcode": vimcode})

    assert len(findings) == 1
    f = findings[0]
    assert f.matched_repos == ("vimcode",)
    assert f.suspect_repos == ()
    assert f.skipped_repos == ("quadraui",)
    assert f.dead is False
    assert f.healthy is True  # nothing suspicious among the repos we COULD check

    lines = format_capability_rule_lines(findings, [])
    joined = "\n".join(lines)
    assert "no local checkout" in joined
    assert "quadraui" in joined


def test_capability_rule_health_all_repos_absent_is_not_dead(tmp_path: Path) -> None:
    """No checkout ANYWHERE must read as unverifiable, not dead — the
    module's existing 'no checkout at all is not a problem' precedent."""
    cfg = _config(
        [Repo(name="vimcode", github="x/vimcode")],
        [SmokeRule(files=["src/gtk/"], requires=["gtk"])],
    )

    findings = capability_rule_health(cfg, repo_checkouts={})

    assert len(findings) == 1
    f = findings[0]
    assert f.checked_repos == ()
    assert f.dead is False
    assert f.healthy is False
    assert f.skipped_repos == ("vimcode",)


def test_capability_rule_health_clean_miss_is_not_partial(tmp_path: Path) -> None:
    """A rule that legitimately only applies to one repo (its path simply
    doesn't exist, at any depth, in the other repo) must not be flagged
    PARTIAL — only a genuinely SUSPECT deeper occurrence should trip it."""
    repo_a = _make_repo_checkout(tmp_path, "repo-a", ["only/here/thing.py"])
    repo_b = _make_repo_checkout(tmp_path, "repo-b", ["totally/unrelated.py"])
    cfg = _config(
        [Repo(name="repo-a", github="x/repo-a"), Repo(name="repo-b", github="x/repo-b")],
        [SmokeRule(files=["only/here/"], requires=["python"])],
    )

    findings = capability_rule_health(
        cfg, repo_checkouts={"repo-a": repo_a, "repo-b": repo_b}
    )

    f = findings[0]
    assert f.matched_repos == ("repo-a",)
    assert f.suspect_repos == ()
    assert f.clean_miss_repos == ("repo-b",)
    assert f.partial is False
    assert f.healthy is True


def test_capability_rule_health_multiple_prefixes_on_one_rule_tracked_independently(
    tmp_path: Path,
) -> None:
    repo_a = _make_repo_checkout(tmp_path, "repo-a", ["src/gtk/window.c"])
    cfg = _config(
        [Repo(name="repo-a", github="x/repo-a")],
        [SmokeRule(files=["src/gtk/", "src/tui_main/"], requires=["gtk"])],
    )

    findings = capability_rule_health(cfg, repo_checkouts={"repo-a": repo_a})

    assert len(findings) == 2
    by_prefix = {f.prefix: f for f in findings}
    assert by_prefix["src/gtk/"].dead is False
    assert by_prefix["src/tui_main/"].dead is True


def test_local_repo_checkouts_finds_existing_checkouts_and_skips_missing(
    tmp_path: Path,
) -> None:
    vimcode = _make_repo_checkout(tmp_path, "vimcode", ["src/gtk/window.c"])
    cfg = _config(
        [Repo(name="vimcode", github="x/vimcode"), Repo(name="ghost-repo", github="x/ghost")],
        [],
        machines=[
            Machine(
                name="m1", host="m1.tail", capabilities=[],
                repos=["vimcode", "ghost-repo"],
                repo_paths={
                    "vimcode": str(vimcode),
                    "ghost-repo": str(tmp_path / "does-not-exist"),
                },
            ),
        ],
    )

    found = local_repo_checkouts(cfg)

    assert found == {"vimcode": vimcode}


def test_local_repo_checkouts_first_machine_wins_when_repo_path_declared_twice(
    tmp_path: Path,
) -> None:
    vimcode_a = _make_repo_checkout(tmp_path, "vimcode-a", ["src/gtk/window.c"])
    vimcode_b = _make_repo_checkout(tmp_path, "vimcode-b", ["src/gtk/window.c"])
    cfg = _config(
        [Repo(name="vimcode", github="x/vimcode")],
        [],
        machines=[
            Machine(
                name="m1", host="m1.tail", capabilities=[], repos=["vimcode"],
                repo_paths={"vimcode": str(vimcode_a)},
            ),
            Machine(
                name="m2", host="m2.tail", capabilities=[], repos=["vimcode"],
                repo_paths={"vimcode": str(vimcode_b)},
            ),
        ],
    )

    found = local_repo_checkouts(cfg)

    assert found == {"vimcode": vimcode_a}


# ── unclaimed_capability_requirements: `requires:` naming an undeclared cap ──


def test_unclaimed_capability_requirements_flags_capability_no_machine_declares() -> None:
    cfg = _config(
        [Repo(name="vimcode", github="x/vimcode")],
        [SmokeRule(files=["src/gtk/"], requires=["gtk"])],
        machines=[
            Machine(name="m1", host="m1.tail", capabilities=["python"], repos=["vimcode"]),
        ],
    )

    unclaimed = unclaimed_capability_requirements(cfg)

    assert unclaimed == ["gtk"]
    lines = format_capability_rule_lines([], unclaimed)
    joined = "\n".join(lines)
    assert "UNCLAIMED CAPABILITY" in joined
    assert "'gtk'" in joined
    assert capability_rule_summary_line([], unclaimed) == (
        "CAPABILITY_RULES: prefixes=0 dead=0 partial=0 unclaimed_caps=1"
    )


def test_unclaimed_capability_requirements_empty_when_every_capability_is_declared() -> None:
    cfg = _config(
        [Repo(name="vimcode", github="x/vimcode")],
        [SmokeRule(files=["src/gtk/"], requires=["gtk"])],
        machines=[
            Machine(name="m1", host="m1.tail", capabilities=["gtk"], repos=["vimcode"]),
        ],
    )

    assert unclaimed_capability_requirements(cfg) == []


def test_format_capability_rule_lines_reports_nothing_configured_neutrally() -> None:
    lines = format_capability_rule_lines([], [])
    assert lines == ["· no smoke_tests.capability_rules configured — nothing to check"]


def test_capability_rule_finding_dataclass_defaults_are_not_dead_or_healthy() -> None:
    """Sanity: a bare finding (nothing checked at all) must not silently
    read as dead=True (no signal isn't "no match") or healthy=True."""
    f = CapabilityRuleFinding(rule_index=0, prefix="x/")
    assert f.checked_repos == ()
    assert f.dead is False
    assert f.partial is False


# ── feature_coverage_findings: build_command vs effective test command (#2967) ──


def test_test_command_coverage_flags_the_live_quadraui_shape() -> None:
    """The exact live incident (#2967): build_command enables
    tui+gtk+terminal, test_command enables only tui — gtk and terminal are
    reported missing, and the finding says so explicitly."""
    cfg = _config(
        [
            Repo(
                name="quadraui",
                github="acme/quadraui",
                build_command="cargo build --features tui --features gtk --features terminal",
                test_command="cargo test --features tui",
            )
        ],
        [],
    )

    findings = feature_coverage_findings(cfg)

    assert len(findings) == 1
    f = findings[0]
    assert f.repo == "quadraui"
    assert f.build_features == ("gtk", "terminal", "tui")
    assert f.test_features == ("tui",)
    assert f.missing_features == ("gtk", "terminal")
    assert f.gap is True
    assert f.test_command_source == "repos[quadraui].test_command"
    assert f.ci_equivalent is False

    lines = format_feature_coverage_lines(findings)
    joined = "\n".join(lines)
    assert "GAP" in joined
    assert "gtk, terminal" in joined
    assert feature_coverage_summary_line(findings) == (
        "TEST_COMMAND_COVERAGE: repos_checked=1 gap=1"
    )


def test_test_command_coverage_healthy_when_test_covers_every_build_feature() -> None:
    cfg = _config(
        [
            Repo(
                name="quadraui",
                github="acme/quadraui",
                build_command="cargo build --features tui --features gtk",
                test_command="cargo test --features tui,gtk",
            )
        ],
        [],
    )

    findings = feature_coverage_findings(cfg)

    assert len(findings) == 1
    assert findings[0].gap is False
    assert findings[0].missing_features == ()
    joined = "\n".join(format_feature_coverage_lines(findings))
    assert "GAP" not in joined
    assert "✓" in joined
    assert feature_coverage_summary_line(findings) == (
        "TEST_COMMAND_COVERAGE: repos_checked=1 gap=0"
    )


def test_test_command_coverage_prefers_ci_command_over_test_command() -> None:
    """`ci_command` outranks `test_command` for the Test stage's actual
    command (`coord.smoke.resolve_smoke_command`'s precedence) — a repo
    whose `ci_command` closes the gap must report healthy even though its
    bare `test_command` alone would be a gap."""
    cfg = _config(
        [
            Repo(
                name="quadraui",
                github="acme/quadraui",
                build_command="cargo build --features tui --features gtk",
                test_command="cargo test --features tui",
                ci_command="cargo test --features tui --features gtk",
            )
        ],
        [],
    )

    findings = feature_coverage_findings(cfg)

    assert len(findings) == 1
    assert findings[0].gap is False
    assert findings[0].ci_equivalent is True
    assert findings[0].test_command_source == "repos[quadraui].ci_command"


def test_test_command_coverage_skips_repos_with_no_explicit_features() -> None:
    """vimcode/coord-tui shape: plain `cargo build`/`cargo test`, no
    `--features` on either side — nothing to compare, so the repo is
    silently omitted rather than reported as a (vacuous) match."""
    cfg = _config(
        [Repo(name="vimcode", github="acme/vimcode", build_command="cargo build",
              test_command="cargo test")],
        [],
    )

    assert feature_coverage_findings(cfg) == []
    lines = format_feature_coverage_lines([])
    assert lines == [
        "· no repo's build_command names an explicit `--features` value "
        "— nothing to check (this only applies to cargo feature-flagged "
        "builds)"
    ]
    assert feature_coverage_summary_line([]) == "TEST_COMMAND_COVERAGE: repos_checked=0 gap=0"


def test_test_command_coverage_skips_repos_with_no_build_command() -> None:
    cfg = _config(
        [Repo(name="quadraui", github="acme/quadraui", test_command="cargo test --features tui")],
        [],
    )

    assert feature_coverage_findings(cfg) == []


def test_test_command_coverage_unconfigured_test_command_is_a_full_gap() -> None:
    cfg = _config(
        [
            Repo(
                name="quadraui",
                github="acme/quadraui",
                build_command="cargo build --features tui --features gtk",
            )
        ],
        [],
    )

    findings = feature_coverage_findings(cfg)

    assert len(findings) == 1
    f = findings[0]
    assert f.gap is True
    assert f.effective_test_command is None
    assert f.missing_features == ("gtk", "tui")
    joined = "\n".join(format_feature_coverage_lines(findings))
    assert "(unconfigured)" in joined


def test_test_coverage_finding_dataclass_defaults_are_not_a_gap() -> None:
    """Sanity: a bare finding built with no `missing_features` is
    (vacuously) healthy — `gap`/`healthy` key off `missing_features` alone,
    not off whether `build_features`/`test_features` were populated."""
    f = FeatureCoverageFinding(
        repo="quadraui",
        build_command="cargo build --features tui",
        effective_test_command="cargo test --features tui",
        test_command_source="repos[quadraui].test_command",
        ci_equivalent=False,
    )
    assert f.gap is False
    assert f.healthy is True
