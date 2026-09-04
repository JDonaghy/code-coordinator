"""Tests for coord.config — YAML loading and validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coord.config import (
    ConfigError,
    PipelineConfig,
    ProviderDef,
    ProvidersConfig,
    StoreConfig,
    _parse_concurrency,
    _parse_merge,
    _parse_store,
    load,
)


def test_load_valid_config(valid_config_path: Path) -> None:
    cfg = load(valid_config_path)
    assert [r.name for r in cfg.repos] == ["api", "shared"]
    assert cfg.repo("api").depends_on == ["shared"]
    assert cfg.repo("api").default_branch == "main"
    assert [m.name for m in cfg.machines] == ["laptop", "server"]
    assert cfg.machines[0].repos == ["api", "shared"]


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "missing.yml")


def test_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load(p)


def test_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text("repos: [\n  - name: api\n")  # unterminated
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load(p)


def test_missing_repos(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text("machines: []\n")
    with pytest.raises(ConfigError, match="repos"):
        load(p)


def test_missing_machines(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text("repos:\n  - name: a\n    github: acme/a\n")
    with pytest.raises(ConfigError, match="machines"):
        load(p)


def test_repo_missing_github(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="github"):
        load(p)


def test_repo_bad_github_format(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: not-a-slug\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="owner/repo"):
        load(p)


def test_duplicate_repo(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "  - name: api\n    github: b/b\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="duplicate repo"):
        load(p)


def test_machine_references_unknown_repo(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [ghost]\n"
    )
    with pytest.raises(ConfigError, match="unknown repos"):
        load(p)


def test_machine_max_workers_parsed(tmp_path: Path) -> None:
    """#1417: machines[].max_workers overrides the fleet-wide
    concurrency.max_workers for capacity checks (e.g. `coord retry`)."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: tiny\n    host: h\n    repos: [api]\n    max_workers: 2\n"
        "  - name: big\n    host: h2\n    repos: [api]\n"
    )
    cfg = load(p)
    by_name = {m.name: m for m in cfg.machines}
    assert by_name["tiny"].max_workers == 2
    # Unset stays None — callers fall back to concurrency.max_workers.
    assert by_name["big"].max_workers is None


def test_machine_max_workers_rejects_non_integer(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n    max_workers: \"lots\"\n"
    )
    with pytest.raises(ConfigError, match="max_workers must be an integer"):
        load(p)


def test_machine_max_workers_rejects_zero_or_negative(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n    max_workers: 0\n"
    )
    with pytest.raises(ConfigError, match="max_workers must be at least 1"):
        load(p)


# ── #1862: per-machine quiet hours ──────────────────────────────────────────


def test_machine_quiet_hours_parsed(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: elitebook\n    host: h\n    repos: [api]\n"
        "    quiet_hours:\n"
        "      start: \"23:00\"\n"
        "      end: \"08:00\"\n"
        "      tz: America/Chicago\n"
        "  - name: server\n    host: h2\n    repos: [api]\n"
    )
    cfg = load(p)
    by_name = {m.name: m for m in cfg.machines}
    qh = by_name["elitebook"].quiet_hours
    assert qh is not None
    assert (qh.start.hour, qh.start.minute) == (23, 0)
    assert (qh.end.hour, qh.end.minute) == (8, 0)
    assert qh.tz == "America/Chicago"
    # No `quiet_hours:` block at all → None, unchanged pre-#1862 behaviour.
    assert by_name["server"].quiet_hours is None


def test_machine_quiet_hours_requires_tz(tmp_path: Path) -> None:
    """#1862: tz must never default silently — a naive comparison against
    the daemon's own UTC clock fires at the wrong wall-clock hour for any
    non-UTC operator."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "    quiet_hours:\n"
        "      start: \"23:00\"\n"
        "      end: \"08:00\"\n"
    )
    with pytest.raises(ConfigError, match="tz is required"):
        load(p)


def test_machine_quiet_hours_rejects_unknown_tz(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "    quiet_hours:\n"
        "      start: \"23:00\"\n"
        "      end: \"08:00\"\n"
        "      tz: Not/AZone\n"
    )
    with pytest.raises(ConfigError, match="not a known IANA zone"):
        load(p)


@pytest.mark.parametrize("bad", ["25:00", "9:00", "09:60", "abc", "9am"])
def test_machine_quiet_hours_rejects_malformed_time(tmp_path: Path, bad: str) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "    quiet_hours:\n"
        f"      start: \"{bad}\"\n"
        "      end: \"08:00\"\n"
        "      tz: UTC\n"
    )
    with pytest.raises(ConfigError, match="HH:MM"):
        load(p)


def test_machine_quiet_hours_rejects_equal_start_end(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "    quiet_hours:\n"
        "      start: \"08:00\"\n"
        "      end: \"08:00\"\n"
        "      tz: UTC\n"
    )
    with pytest.raises(ConfigError, match="start and end must differ"):
        load(p)


def test_machine_quiet_hours_rejects_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "    quiet_hours: \"nope\"\n"
    )
    with pytest.raises(ConfigError, match="quiet_hours.*must be a mapping"):
        load(p)


def test_machine_quiet_hours_accepts_non_wrapping_window(tmp_path: Path) -> None:
    """13:00 -> 14:00: start < end, same-day window."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "    quiet_hours:\n"
        "      start: \"13:00\"\n"
        "      end: \"14:00\"\n"
        "      tz: UTC\n"
    )
    cfg = load(p)
    qh = cfg.machines[0].quiet_hours
    assert qh is not None
    assert (qh.start.hour, qh.end.hour) == (13, 14)


def test_unknown_dependency(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n    depends_on: [ghost]\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="depends_on unknown repos"):
        load(p)


def test_self_dependency(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n    depends_on: [api]\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="cannot depend on itself"):
        load(p)


def test_repo_unknown_key_warns_but_does_not_raise(tmp_path: Path) -> None:
    """#2783: an unrecognised repos[] key (e.g. the dead `exclusive_files`)
    must not fail config loading — it is unread, not invalid."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    bogus_key: [x]\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)  # must not raise ConfigError
    assert len(cfg.warnings) == 1
    assert "api" in cfg.warnings[0]
    assert "bogus_key" in cfg.warnings[0]


def test_repo_known_keys_produce_no_warnings(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    depends_on: []\n"
        "    default_branch: main\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.warnings == []


def test_config_cmd_surfaces_unknown_repo_key(tmp_path: Path) -> None:
    """Black-box: `coord config` names the unrecognised key without extra flags."""
    from click.testing import CliRunner  # noqa: PLC0415

    from coord.cli import main  # noqa: PLC0415

    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    bogus_key: [x]\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    result = CliRunner().invoke(main, ["config", "--config", str(p)])
    assert result.exit_code == 0
    assert "bogus_key" in result.output
    assert "api" in result.output


def test_store_backend_cmd_defaults_to_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3085: `coord store-backend` -- the accessor `deploy/coord-db-backup.sh`
    consumes -- must answer "sqlite" when no `store:` block is configured at
    all, same fail-open contract as `coord.db.resolve_store_backend()`."""
    from click.testing import CliRunner  # noqa: PLC0415

    from coord.cli import main  # noqa: PLC0415

    monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "no-such-coordinator.yml"))
    result = CliRunner().invoke(main, ["store-backend"])
    assert result.exit_code == 0
    assert result.output.strip() == "sqlite"


def test_store_backend_cmd_reports_postgres_with_redacted_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured postgres backend prints its name plus a host/dbname-only
    target -- never the raw DSN (the password must not leak into a log a
    shell script might echo)."""
    from click.testing import CliRunner  # noqa: PLC0415

    from coord.cli import main  # noqa: PLC0415

    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos: []\nmachines: []\n"
        "store:\n  backend: postgres\n  dsn: postgresql://user:sekret@dbhost:5432/coord\n"
    )
    monkeypatch.setenv("COORD_CONFIG", str(p))
    result = CliRunner().invoke(main, ["store-backend"])
    assert result.exit_code == 0
    assert result.output.startswith("postgres")
    assert "sekret" not in result.output
    assert "dbhost" in result.output


def test_store_backend_cmd_fails_loud_on_malformed_store_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit-but-invalid `store:` block must exit non-zero rather than
    silently answering "sqlite" -- the one config problem
    `resolve_store_backend()` deliberately lets raise. This is what makes
    `deploy/coord-db-backup.sh` refuse instead of guessing when the config is
    simply broken."""
    from click.testing import CliRunner  # noqa: PLC0415

    from coord.cli import main  # noqa: PLC0415

    p = tmp_path / "coordinator.yml"
    p.write_text("repos: []\nmachines: []\nstore:\n  backend: not-a-real-backend\n")
    monkeypatch.setenv("COORD_CONFIG", str(p))
    result = CliRunner().invoke(main, ["store-backend"])
    assert result.exit_code != 0


def test_store_backend_cmd_prints_curated_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3085 fix-review: a malformed `store:` block must still exit
    non-zero (see the test above), but the failure a shell script's `fail()`
    embeds verbatim should be one curated line, not a raw Python traceback --
    a `deploy/coord-db-backup.sh` failure ends up in the systemd journal an
    on-call engineer reads."""
    from click.testing import CliRunner  # noqa: PLC0415

    from coord.cli import main  # noqa: PLC0415

    p = tmp_path / "coordinator.yml"
    p.write_text("repos: []\nmachines: []\nstore:\n  backend: not-a-real-backend\n")
    monkeypatch.setenv("COORD_CONFIG", str(p))
    result = CliRunner().invoke(main, ["store-backend"])
    assert result.exit_code != 0
    assert "Traceback (most recent call last)" not in result.output
    assert "store-backend" in result.output


_EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "coordinator.yml"


@pytest.mark.skipif(not _EXAMPLE_CONFIG.exists(), reason="coordinator.yml is gitignored")
def test_example_config_at_repo_root() -> None:
    """The committed coordinator.yml must parse cleanly."""
    cfg = load(_EXAMPLE_CONFIG)
    assert len(cfg.repos) > 0
    assert len(cfg.machines) > 0


def test_repo_housekeeping_parsed(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    housekeeping:\n"
        "      - pip install -e .\n"
        "      - make build\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").housekeeping == ["pip install -e .", "make build"]


def test_repo_housekeeping_default_empty(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").housekeeping == []


def test_repo_housekeeping_invalid_type(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    housekeeping: not-a-list\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="housekeeping must be a list of strings"):
        load(p)


def test_repo_housekeeping_invalid_element(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    housekeeping:\n"
        "      - 42\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="housekeeping must be a list of strings"):
        load(p)


# ── artifact_paths (#305) ──────────────────────────────────────────────────


def test_artifact_paths_parsed(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    artifact_paths:\n"
        "      - target/debug/examples/gui_*\n"
        "      - target/debug/mybin\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").artifact_paths == [
        "target/debug/examples/gui_*",
        "target/debug/mybin",
    ]


def test_artifact_paths_default_empty(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").artifact_paths == []


def test_artifact_paths_not_a_list(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    artifact_paths: target/debug/mybin\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="artifact_paths must be a list"):
        load(p)


def test_artifact_paths_non_string_element(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    artifact_paths:\n"
        "      - 42\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="artifact_paths\\[0\\] must be a string"):
        load(p)


# ── uat_preview (#2687) ──────────────────────────────────────────────────────


def test_uat_preview_parsed(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    uat_preview: 'https://{pr_branch_slug}.natal-chart-3ew.pages.dev/'\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").uat_preview == (
        "https://{pr_branch_slug}.natal-chart-3ew.pages.dev/"
    )


def test_uat_preview_default_none(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").uat_preview is None


def test_uat_live_preview_parsed(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n    uat_live_preview: true\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").uat_live_preview is True


def test_uat_live_preview_default_false(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").uat_live_preview is False


def test_uat_live_preview_non_bool_rejected(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n    uat_live_preview: 'yes'\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="uat_live_preview must be a boolean"):
        load(p)


def test_uat_preview_non_string_rejected(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n    uat_preview: 42\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="uat_preview must be a string"):
        load(p)


def test_uat_preview_empty_string_rejected(tmp_path: Path) -> None:
    # An explicit but blank uat_preview is almost certainly a copy-paste
    # mistake, not "opt out" — omitting the key entirely is how a repo opts
    # out, so a blank value is rejected loudly instead of silently disabling
    # the gate.
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n    uat_preview: ''\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="uat_preview must be a non-empty string"):
        load(p)


def test_uat_preview_resolve_url_pr_branch_slug_no_longer_substituted() -> None:
    """#2948: `{pr_branch_slug}` was removed as a substitution variable —
    confirmed live against natal-chart that Cloudflare Pages publishes no
    branch-alias URL at all (not even for `main`), so no algorithm on the
    branch name alone could ever produce a working link. It is now just an
    unknown placeholder, left verbatim like any other typo (see
    ``test_uat_preview_resolve_url_unknown_placeholder_left_verbatim``) —
    a template author who still has it in `coordinator.yml` gets a visibly
    broken URL rather than a silently dead one.
    """
    from coord.models import Repo

    repo = Repo(
        name="natal-chart", github="acme/natal-chart",
        uat_preview="https://{pr_branch_slug}.natal-chart-3ew.pages.dev/",
    )
    url = repo.resolve_uat_preview_url(branch="issue-42-fix-chart-colors")
    assert url == "https://{pr_branch_slug}.natal-chart-3ew.pages.dev/"


def test_uat_preview_resolve_url_returns_none_when_unset() -> None:
    from coord.models import Repo

    repo = Repo(name="api", github="acme/api")
    assert repo.resolve_uat_preview_url(branch="worker/w1") is None


def test_uat_preview_resolve_url_other_substitutions() -> None:
    from coord.models import Repo

    repo = Repo(
        name="api", github="acme/api",
        uat_preview="https://preview/{repo}/{issue_number}/{pr_number}/{branch}",
    )
    url = repo.resolve_uat_preview_url(branch="b1", issue_number=42, pr_number=7)
    assert url == "https://preview/api/42/7/b1"


def test_uat_preview_resolve_url_unknown_placeholder_left_verbatim() -> None:
    # A typo in the template must not crash the merge gate — see
    # Repo.uat_preview's docstring.
    from coord.models import Repo

    repo = Repo(
        name="api", github="acme/api",
        uat_preview="https://{typo_field}.example.pages.dev/",
    )
    url = repo.resolve_uat_preview_url(branch="b1")
    assert url == "https://{typo_field}.example.pages.dev/"


# ── Config path resolution (~/.coord/coordinator.yml) ────────────────────────


def test_resolve_config_path_prefers_env(tmp_path, monkeypatch) -> None:
    from coord import config as cfgmod

    env_file = tmp_path / "env.yml"
    env_file.write_text("x")
    monkeypatch.setenv("COORD_CONFIG", str(env_file))
    assert cfgmod.resolve_config_path() == env_file


def test_resolve_config_path_prefers_user_home_over_cwd(tmp_path, monkeypatch) -> None:
    from coord import config as cfgmod

    monkeypatch.delenv("COORD_CONFIG", raising=False)
    home_cfg = tmp_path / "home.yml"
    home_cfg.write_text("x")
    cwd_cfg = tmp_path / "coordinator.yml"
    cwd_cfg.write_text("x")
    monkeypatch.setattr(cfgmod, "USER_CONFIG_PATH", home_cfg)
    monkeypatch.setattr(cfgmod, "DEFAULT_CONFIG_PATH", cwd_cfg)
    assert cfgmod.resolve_config_path() == home_cfg


def test_resolve_config_path_falls_back_to_cwd(tmp_path, monkeypatch) -> None:
    from coord import config as cfgmod

    monkeypatch.delenv("COORD_CONFIG", raising=False)
    home_cfg = tmp_path / "absent_home.yml"  # does NOT exist
    cwd_cfg = tmp_path / "coordinator.yml"
    cwd_cfg.write_text("x")
    monkeypatch.setattr(cfgmod, "USER_CONFIG_PATH", home_cfg)
    monkeypatch.setattr(cfgmod, "DEFAULT_CONFIG_PATH", cwd_cfg)
    assert cfgmod.resolve_config_path() == cwd_cfg


def test_resolve_config_path_defaults_to_user_home_when_none_exist(
    tmp_path, monkeypatch
) -> None:
    from coord import config as cfgmod

    monkeypatch.delenv("COORD_CONFIG", raising=False)
    home_cfg = tmp_path / "absent_home.yml"  # absent
    cwd_cfg = tmp_path / "absent_cwd.yml"  # absent
    monkeypatch.setattr(cfgmod, "USER_CONFIG_PATH", home_cfg)
    monkeypatch.setattr(cfgmod, "DEFAULT_CONFIG_PATH", cwd_cfg)
    # None exist → the canonical home path is returned so the error points there.
    assert cfgmod.resolve_config_path() == home_cfg


def test_load_with_no_arg_resolves_default(tmp_path, monkeypatch) -> None:
    from coord import config as cfgmod

    monkeypatch.delenv("COORD_CONFIG", raising=False)
    cfg_file = tmp_path / "home.yml"
    cfg_file.write_text(
        "repos:\n  - name: api\n    github: a/a\n"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
    )
    monkeypatch.setattr(cfgmod, "USER_CONFIG_PATH", cfg_file)
    cfg = cfgmod.load()  # no arg → resolves to USER_CONFIG_PATH
    assert cfg.path == cfg_file
    assert [r.name for r in cfg.repos] == ["api"]


# ── is_canonical_config_path (#2208) ─────────────────────────────────────────


def test_is_canonical_config_path_true_for_the_resolved_default(tmp_path, monkeypatch) -> None:
    from coord import config as cfgmod

    monkeypatch.delenv("COORD_CONFIG", raising=False)
    home_cfg = tmp_path / "home.yml"
    home_cfg.write_text("x")
    monkeypatch.setattr(cfgmod, "USER_CONFIG_PATH", home_cfg)
    monkeypatch.setattr(cfgmod, "DEFAULT_CONFIG_PATH", tmp_path / "absent-cwd.yml")

    assert cfgmod.is_canonical_config_path(home_cfg) is True
    # An equivalent (non-normalized) spelling of the same file still counts.
    assert cfgmod.is_canonical_config_path(tmp_path / "." / "home.yml") is True


def test_is_canonical_config_path_false_for_a_scratch_override(tmp_path, monkeypatch) -> None:
    """#2208: a throwaway --config pointed elsewhere is NOT canonical, even
    though coord.config.load() parses it fine."""
    from coord import config as cfgmod

    monkeypatch.delenv("COORD_CONFIG", raising=False)
    home_cfg = tmp_path / "home.yml"
    home_cfg.write_text("x")
    monkeypatch.setattr(cfgmod, "USER_CONFIG_PATH", home_cfg)
    monkeypatch.setattr(cfgmod, "DEFAULT_CONFIG_PATH", tmp_path / "absent-cwd.yml")

    scratch = tmp_path / "mini2.yml"
    scratch.write_text("machines:\n  - name: ci-runner\n    host: ci-runner\n")

    assert cfgmod.is_canonical_config_path(scratch) is False


def test_is_canonical_config_path_respects_coord_config_env(tmp_path, monkeypatch) -> None:
    """When $COORD_CONFIG is set, THAT is the canonical path for this
    environment — matching it counts as canonical, same as any other
    ordinary (non-override) resolution."""
    from coord import config as cfgmod

    env_file = tmp_path / "env.yml"
    env_file.write_text("x")
    monkeypatch.setenv("COORD_CONFIG", str(env_file))

    assert cfgmod.is_canonical_config_path(env_file) is True
    assert cfgmod.is_canonical_config_path(tmp_path / "other.yml") is False


# ── PipelineConfig helpers ──────────────────────────────────────────────────


def test_pipeline_test_precedes_review() -> None:
    """test_precedes_review() is True only when both gates are present and
    'test' is ordered before 'review' (the new default)."""
    assert PipelineConfig().test_precedes_review()  # new default is test-first
    assert PipelineConfig(
        default_gates=["test", "review", "merge"]
    ).test_precedes_review()
    assert not PipelineConfig(
        default_gates=["review", "test", "merge"]
    ).test_precedes_review()
    # Either gate absent → not gated.
    assert not PipelineConfig(default_gates=["review", "merge"]).test_precedes_review()
    assert not PipelineConfig(default_gates=["test", "merge"]).test_precedes_review()
    assert not PipelineConfig(default_gates=[]).test_precedes_review()


def test_pipeline_tracked_labels_defaults_to_coord(tmp_path: Path) -> None:
    """When pipeline.labels is unset, tracked_labels() returns ['coord']."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.pipeline.tracked_labels() == ["coord"]


def test_pipeline_tracked_labels_from_labels_keys(tmp_path: Path) -> None:
    """tracked_labels() always includes 'coord' plus sorted configured keys."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  labels:\n"
        "    hotfix: [merge]\n"
        "    feature: [review, merge]\n"
    )
    cfg = load(p)
    # 'coord' is always first; configured keys follow alphabetically.
    assert cfg.pipeline.tracked_labels() == ["coord", "feature", "hotfix"]


def test_pipeline_tracked_labels_coord_not_duplicated(tmp_path: Path) -> None:
    """When 'coord' is explicitly in labels, it is not duplicated."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  labels:\n"
        "    coord: [review, merge]\n"
        "    hotfix: [merge]\n"
    )
    cfg = load(p)
    assert cfg.pipeline.tracked_labels() == ["coord", "hotfix"]


def test_pipeline_gates_for_label_uses_override(tmp_path: Path) -> None:
    """gates_for_label() returns the override list when the label matches."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  labels:\n"
        "    hotfix: [merge]\n"
    )
    cfg = load(p)
    assert cfg.pipeline.gates_for_label("hotfix") == ["merge"]


def test_pipeline_gates_for_label_falls_back_to_default(tmp_path: Path) -> None:
    """When the label is not in labels, gates_for_label() returns default_gates."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    # Default default_gates: Test comes before Review (smoke before PR/review).
    assert cfg.pipeline.gates_for_label("coord") == ["test", "review", "merge"]
    assert cfg.pipeline.gates_for_label(None) == ["test", "review", "merge"]


# ── #846: attention_thresholds / convergence_rounds ─────────────────────────


def test_pipeline_attention_thresholds_default() -> None:
    cfg = PipelineConfig()
    assert cfg.attention_threshold_for("work") == 45 * 60.0
    assert cfg.attention_threshold_for("review") == 15 * 60.0
    assert cfg.attention_threshold_for("smoke") == 20 * 60.0
    # #1133: headless types now have their own explicit defaults rather
    # than silently inheriting "work"'s threshold.
    assert cfg.attention_threshold_for("mock-author") == 30 * 60.0
    assert cfg.attention_threshold_for("test-author") == 30 * 60.0
    assert cfg.attention_threshold_for("plan") == 30 * 60.0
    assert cfg.attention_threshold_for("conflict-fix") == 60 * 60.0
    # A genuinely unlisted headless type still falls back to "work".
    assert cfg.attention_threshold_for("some-future-headless-type") == 45 * 60.0
    assert cfg.convergence_rounds == 3


def test_pipeline_attention_thresholds_interactive_types_exempt() -> None:
    """#1133: human-attended chat-style sessions never trip the wall-clock
    signal by default — they have no headless-convergence concept, so a
    multi-hour live session (the exact scenario that motivated #1133) must
    not be flagged as stuck.
    """
    cfg = PipelineConfig()
    for assignment_type in (
        "chat",
        "troubleshoot",
        "audit",
        "milestone-chat",
        "refinement",
        "new-issue-chat",
        "test-chat",
    ):
        assert cfg.attention_threshold_for(assignment_type) == float("inf")


def test_pipeline_attention_thresholds_explicit_override_wins_over_interactive_exemption() -> None:
    """An explicit user-configured threshold for an interactive type still
    applies — the #1133 exemption is a default, not unconditional.
    """
    cfg = PipelineConfig(attention_thresholds={"chat": 120.0})
    assert cfg.attention_threshold_for("chat") == 120.0
    # Other interactive types remain exempt.
    assert cfg.attention_threshold_for("troubleshoot") == float("inf")


def test_pipeline_attention_thresholds_interactive_fix_session_gets_conflict_fix_threshold() -> None:
    """#1137: an interactive ``--fix-of``/``--rework-of`` session shares
    ``type="work"`` with headless coding workers, so it's recognized by the
    compound discriminator (``provider_name="claude-pty"`` +
    ``review_of_assignment_id`` set) instead of a dedicated type — mirroring
    ``coord.reconcile.is_interactive_merge_session`` — and reuses
    ``conflict-fix``'s 60m threshold rather than plain ``work``'s 45m.
    """
    cfg = PipelineConfig()
    assert cfg.attention_threshold_for(
        "work", provider_name="claude-pty", review_of_assignment_id="rev-1",
    ) == 60 * 60.0


def test_pipeline_attention_thresholds_plain_work_unaffected_by_discriminator_kwargs() -> None:
    """A headless work assignment (no provider_name / no review_of_assignment_id)
    still gets plain ``work``'s 45m threshold — only the compound match bumps it.
    """
    cfg = PipelineConfig()
    # Headless — no provider_name at all.
    assert cfg.attention_threshold_for("work") == 45 * 60.0
    # Interactive but a FRESH session (#437), not continuing existing work.
    assert cfg.attention_threshold_for(
        "work", provider_name="claude-pty", review_of_assignment_id=None,
    ) == 45 * 60.0
    # review_of_assignment_id set but NOT the interactive provider (e.g. a
    # headless auto-loop fix dispatch, which also sets review_of_assignment_id).
    assert cfg.attention_threshold_for(
        "work", provider_name=None, review_of_assignment_id="rev-1",
    ) == 45 * 60.0


def test_pipeline_attention_thresholds_interactive_fix_explicit_conflict_fix_override_wins() -> None:
    """An explicit user-configured ``conflict-fix`` threshold applies to an
    interactive fix session too, since #1137 delegates to
    ``attention_threshold_for("conflict-fix")``. Mirrors #1133's
    ``INTERACTIVE_SESSION_TYPES`` precedent: overriding plain ``work`` does
    NOT re-arm/change the fix-session bump (it's checked first, exactly like
    the interactive-type exemption), only overriding the type it defers to
    does.
    """
    cfg = PipelineConfig(attention_thresholds={"conflict-fix": 5.0})
    assert cfg.attention_threshold_for(
        "work", provider_name="claude-pty", review_of_assignment_id="rev-1",
    ) == 5.0

    # Overriding "work" alone (leaving conflict-fix at its built-in default)
    # has no effect on the interactive-fix bump — same precedence rule as
    # the #1133 interactive-type exemption: only an override of the type
    # actually consulted (conflict-fix) changes the outcome.
    cfg2 = PipelineConfig(
        attention_thresholds={"work": 5.0, "conflict-fix": 60 * 60.0}
    )
    assert cfg2.attention_threshold_for(
        "work", provider_name="claude-pty", review_of_assignment_id="rev-1",
    ) == 60 * 60.0
    # Plain headless "work" (no discriminator match) still honors the override.
    assert cfg2.attention_threshold_for("work") == 5.0


@pytest.mark.parametrize("assignment_type", ["review", "smoke"])
def test_pipeline_attention_thresholds_interactive_session_gets_conflict_fix_threshold(
    assignment_type: str,
) -> None:
    """#1144: an interactive ``--review-of``/``--smoke-of`` session shares
    ``type="review"``/``type="smoke"`` with headless auto-review/smoke, so
    it's recognized by the same compound discriminator
    (``provider_name="claude-pty"`` + ``review_of_assignment_id`` set) that
    #1137 gave the interactive fix session — mirroring
    ``coord.reconcile.is_interactive_merge_session`` — and reuses
    ``conflict-fix``'s 60m threshold rather than plain review's 15m or
    smoke's 20m.
    """
    cfg = PipelineConfig()
    assert cfg.attention_threshold_for(
        assignment_type, provider_name="claude-pty", review_of_assignment_id="rev-1",
    ) == 60 * 60.0


@pytest.mark.parametrize("assignment_type", ["review", "smoke"])
def test_pipeline_attention_thresholds_plain_review_smoke_unaffected_by_discriminator_kwargs(
    assignment_type: str,
) -> None:
    """A headless review/smoke assignment (no provider_name / no
    review_of_assignment_id) still gets its own plain threshold — only the
    compound match bumps it to conflict-fix's.
    """
    cfg = PipelineConfig()
    plain_threshold = cfg.attention_threshold_for(assignment_type)
    assert plain_threshold in (15 * 60.0, 20 * 60.0)
    # Headless — no provider_name at all.
    assert cfg.attention_threshold_for(assignment_type) == plain_threshold
    # review_of_assignment_id set (headless auto-review/smoke also set this
    # to link back to the work assignment) but NOT the interactive provider.
    assert cfg.attention_threshold_for(
        assignment_type, provider_name=None, review_of_assignment_id="rev-1",
    ) == plain_threshold
    # Interactive provider but no review_of_assignment_id (shouldn't happen
    # in practice for review/smoke, but the discriminator requires both).
    assert cfg.attention_threshold_for(
        assignment_type, provider_name="claude-pty", review_of_assignment_id=None,
    ) == plain_threshold


@pytest.mark.parametrize("assignment_type", ["review", "smoke"])
def test_pipeline_attention_thresholds_interactive_review_smoke_explicit_conflict_fix_override_wins(
    assignment_type: str,
) -> None:
    """An explicit user-configured ``conflict-fix`` threshold applies to an
    interactive review/smoke session too, since #1144 delegates to
    ``attention_threshold_for("conflict-fix")`` the same way #1137 did for
    the interactive fix session. Overriding plain ``review``/``smoke`` alone
    does NOT change the interactive bump — only overriding the type it
    defers to (conflict-fix) does.
    """
    cfg = PipelineConfig(attention_thresholds={"conflict-fix": 5.0})
    assert cfg.attention_threshold_for(
        assignment_type, provider_name="claude-pty", review_of_assignment_id="rev-1",
    ) == 5.0

    cfg2 = PipelineConfig(
        attention_thresholds={assignment_type: 5.0, "conflict-fix": 60 * 60.0}
    )
    assert cfg2.attention_threshold_for(
        assignment_type, provider_name="claude-pty", review_of_assignment_id="rev-1",
    ) == 60 * 60.0
    # Plain headless review/smoke (no discriminator match) still honors the
    # override of its own type.
    assert cfg2.attention_threshold_for(assignment_type) == 5.0


def test_pipeline_attention_thresholds_parsed_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  attention_thresholds:\n"
        "    work: 90m\n"
        "    review: 600\n"
        "  convergence_rounds: 5\n"
    )
    cfg = load(p)
    assert cfg.pipeline.attention_threshold_for("work") == 90 * 60.0
    assert cfg.pipeline.attention_threshold_for("review") == 600.0
    # smoke wasn't overridden — falls back to this config's own "work"
    # value (the user's intent), not the hardcoded built-in default.
    assert cfg.pipeline.attention_threshold_for("smoke") == 90 * 60.0
    assert cfg.pipeline.convergence_rounds == 5


def test_pipeline_attention_thresholds_rejects_bad_duration(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  attention_thresholds:\n"
        "    work: not-a-duration\n"
    )
    with pytest.raises(ConfigError, match="attention_thresholds"):
        load(p)


# ── #2048: liveness_auditor ──────────────────────────────────────────────────


def test_liveness_auditor_defaults_off() -> None:
    """Ships dark — see LivenessAuditorConfig's docstring."""
    cfg = PipelineConfig()
    assert cfg.liveness_auditor.enabled is False
    assert cfg.liveness_auditor.strikes == 3
    assert cfg.liveness_auditor.debounce_seconds == 60.0
    assert cfg.liveness_auditor.model == "claude-haiku-4-5"
    assert cfg.liveness_auditor.claude_bin is None


def test_liveness_auditor_parsed_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  liveness_auditor:\n"
        "    enabled: true\n"
        "    strikes: 5\n"
        "    debounce_seconds: 2m\n"
        "    model: claude-haiku-4-5\n"
        "    timeout_seconds: 45\n"
        "    claude_bin: /usr/local/bin/claude\n"
    )
    cfg = load(p)
    la = cfg.pipeline.liveness_auditor
    assert la.enabled is True
    assert la.strikes == 5
    assert la.debounce_seconds == 120.0
    assert la.model == "claude-haiku-4-5"
    assert la.timeout_seconds == 45.0
    assert la.claude_bin == "/usr/local/bin/claude"


def test_liveness_auditor_absent_block_keeps_defaults(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.pipeline.liveness_auditor.enabled is False


def test_liveness_auditor_rejects_bad_strikes(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  liveness_auditor:\n"
        "    strikes: 0\n"
    )
    with pytest.raises(ConfigError, match="strikes"):
        load(p)


def test_liveness_auditor_rejects_non_bool_enabled(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  liveness_auditor:\n"
        "    enabled: yes-please\n"
    )
    with pytest.raises(ConfigError, match="enabled"):
        load(p)


def test_pipeline_convergence_rounds_rejects_non_positive(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
        "pipeline:\n"
        "  convergence_rounds: 0\n"
    )
    with pytest.raises(ConfigError, match="convergence_rounds"):
        load(p)


# ── concurrency: daemon-spawn stall mitigations (#299) ───────────────────────

def test_concurrency_defaults() -> None:
    cfg = _parse_concurrency(None)
    assert cfg.bash_wrap_spawn is True
    assert cfg.first_output_timeout == 600.0
    # #2638: generous 6h default, matching `coord.agent._DEFAULT_RUNTIME_CEILING_S`.
    assert cfg.runtime_ceiling_s == 6.0 * 60.0 * 60.0


def test_concurrency_bash_wrap_spawn_parses() -> None:
    assert _parse_concurrency({"bash_wrap_spawn": False}).bash_wrap_spawn is False
    assert _parse_concurrency({"bash_wrap_spawn": True}).bash_wrap_spawn is True


def test_concurrency_bash_wrap_spawn_rejects_non_bool() -> None:
    with pytest.raises(ConfigError, match="bash_wrap_spawn must be a boolean"):
        _parse_concurrency({"bash_wrap_spawn": "yes"})


def test_concurrency_first_output_timeout_parses() -> None:
    assert _parse_concurrency({"first_output_timeout": 0}).first_output_timeout == 0
    assert _parse_concurrency({"first_output_timeout": 120}).first_output_timeout == 120
    assert _parse_concurrency({"first_output_timeout": 90.5}).first_output_timeout == 90.5


def test_concurrency_first_output_timeout_rejects_negative() -> None:
    with pytest.raises(ConfigError, match="first_output_timeout must be a non-negative number"):
        _parse_concurrency({"first_output_timeout": -1})


def test_concurrency_first_output_timeout_rejects_bool() -> None:
    with pytest.raises(ConfigError, match="first_output_timeout must be a non-negative number"):
        _parse_concurrency({"first_output_timeout": True})


# ── concurrency: wall-clock runtime ceiling (#2638) ───────────────────────────

def test_concurrency_runtime_ceiling_s_parses() -> None:
    assert _parse_concurrency({"runtime_ceiling_s": 0}).runtime_ceiling_s == 0
    assert _parse_concurrency({"runtime_ceiling_s": 3600}).runtime_ceiling_s == 3600
    assert _parse_concurrency({"runtime_ceiling_s": 1800.5}).runtime_ceiling_s == 1800.5


def test_concurrency_runtime_ceiling_s_rejects_negative() -> None:
    with pytest.raises(ConfigError, match="runtime_ceiling_s must be a non-negative number"):
        _parse_concurrency({"runtime_ceiling_s": -1})


def test_concurrency_runtime_ceiling_s_rejects_bool() -> None:
    with pytest.raises(ConfigError, match="runtime_ceiling_s must be a non-negative number"):
        _parse_concurrency({"runtime_ceiling_s": True})


# ── run_cmd per repo (#296) ────────────────────────────────────────────────────

def test_repo_run_cmd_absent(tmp_path: Path) -> None:
    """run_cmd defaults to None when omitted."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: acme/api\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").run_cmd is None


def test_repo_run_cmd_present(tmp_path: Path) -> None:
    """run_cmd is parsed and stored on the Repo when provided."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: ui\n"
        "    github: acme/ui\n"
        "    run_cmd: 'cargo run --example gtk_panel --features gtk'\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [ui]\n"
    )
    cfg = load(p)
    assert cfg.repo("ui").run_cmd == "cargo run --example gtk_panel --features gtk"


def test_repo_run_cmd_non_string_rejected(tmp_path: Path) -> None:
    """run_cmd must be a string; non-string values raise ConfigError."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: acme/api\n    run_cmd: 42\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="run_cmd must be a string"):
        load(p)


# ── Repo.resolve_new_issue_guidance (#316) ───────────────────────────────────


def test_resolve_guidance_returns_default_when_none(tmp_path: Path) -> None:
    """When new_issue_guidance is None, a generic default is returned."""
    from coord.models import Repo

    repo = Repo(name="r", github="o/r", new_issue_guidance=None)
    guidance = repo.resolve_new_issue_guidance(tmp_path)
    assert "Title" in guidance
    assert "Acceptance" in guidance


def test_resolve_guidance_returns_inline_text(tmp_path: Path) -> None:
    """When the value doesn't look like a path, it is returned verbatim."""
    from coord.models import Repo

    text = "**Required:** Title (≤80 chars), What, Acceptance criteria"
    repo = Repo(name="r", github="o/r", new_issue_guidance=text)
    assert repo.resolve_new_issue_guidance(tmp_path) == text


def test_resolve_guidance_reads_file_when_path_exists(tmp_path: Path) -> None:
    """When the value is a path and the file exists, the file contents are returned."""
    from coord.models import Repo

    guidance_dir = tmp_path / "docs"
    guidance_dir.mkdir()
    (guidance_dir / "ISSUE_GUIDANCE.md").write_text("## Guidance\n- Step 1", encoding="utf-8")
    repo = Repo(name="r", github="o/r", new_issue_guidance="docs/ISSUE_GUIDANCE.md")
    result = repo.resolve_new_issue_guidance(tmp_path)
    assert "## Guidance" in result
    assert "Step 1" in result


def test_resolve_guidance_falls_back_to_inline_when_file_missing(tmp_path: Path) -> None:
    """When the value looks like a path but the file is absent, return the value verbatim."""
    from coord.models import Repo

    repo = Repo(name="r", github="o/r", new_issue_guidance="docs/MISSING.md")
    result = repo.resolve_new_issue_guidance(tmp_path)
    # File doesn't exist — value is returned as-is (path string).
    assert result == "docs/MISSING.md"


def test_resolve_guidance_txt_extension_treated_as_path(tmp_path: Path) -> None:
    """A .txt path is also resolved as a file."""
    from coord.models import Repo

    (tmp_path / "GUIDANCE.txt").write_text("Plain text guidance", encoding="utf-8")
    repo = Repo(name="r", github="o/r", new_issue_guidance="GUIDANCE.txt")
    result = repo.resolve_new_issue_guidance(tmp_path)
    assert result == "Plain text guidance"


def test_resolve_guidance_rejects_absolute_path(tmp_path: Path) -> None:
    """#316: an absolute path like `/etc/passwd.md` must not escape `repo_path`.

    `Path("/repo") / "/etc/passwd.md"` silently discards the base, so the
    repo-root confinement has to be enforced separately from the relative
    `../` check.  We expect the value to fall through to the inline branch
    rather than reading the absolute file.
    """
    from coord.models import Repo, _GUIDANCE_PATH_RE

    # Regex-level: absolute paths must not match the path-shaped pattern.
    assert not _GUIDANCE_PATH_RE.match("/etc/passwd.md")
    assert not _GUIDANCE_PATH_RE.match("/home/user/file.md")
    assert not _GUIDANCE_PATH_RE.match("\\windows\\system32\\config.md")

    # Behaviour: even if a future regex regression let the value through, the
    # `Path.resolve()` containment check inside `resolve_new_issue_guidance`
    # still prevents reading the absolute file.  Verify the public method
    # returns the value verbatim (inline-text path) rather than file contents.
    repo = Repo(name="r", github="o/r", new_issue_guidance="/etc/hostname.md")
    result = repo.resolve_new_issue_guidance(tmp_path)
    assert result == "/etc/hostname.md"


def test_resolve_guidance_rejects_symlink_escape(tmp_path: Path) -> None:
    """#316: a symlink under `repo_path` pointing outside must not be read.

    The `Path.resolve()` + `relative_to(base)` check inside
    `resolve_new_issue_guidance` catches symlink escapes that the regex alone
    cannot see.
    """
    from coord.models import Repo

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "SECRET.md"
    secret.write_text("top secret", encoding="utf-8")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    link = repo_root / "leak.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    repo = Repo(name="r", github="o/r", new_issue_guidance="leak.md")
    result = repo.resolve_new_issue_guidance(repo_root)
    # Symlink resolves outside repo_root — treated as inline, NOT read.
    assert "top secret" not in result
    assert result == "leak.md"


def test_new_issue_guidance_loaded_from_config(tmp_path: Path) -> None:
    """new_issue_guidance is parsed from coordinator.yml and stored on Repo."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    new_issue_guidance: 'Title, What, Acceptance'\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").new_issue_guidance == "Title, What, Acceptance"


def test_new_issue_guidance_non_string_rejected(tmp_path: Path) -> None:
    """new_issue_guidance must be a string; non-string raises ConfigError."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: acme/api\n    new_issue_guidance: 42\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="new_issue_guidance must be a string"):
        load(p)


# ── providers block (#323) ────────────────────────────────────────────────────


_MIN_CONFIG = (
    "repos:\n"
    "  - name: api\n    github: a/a\n"
    "machines:\n"
    "  - name: m\n    host: h\n    repos: [api]\n"
)


def test_providers_absent_block_defaults() -> None:
    """When 'providers' is absent, default='claude' and implicit 'claude' entry present."""
    cfg = ProvidersConfig()
    assert cfg.default == "claude"
    assert "claude" in cfg.definitions
    assert cfg.definitions["claude"].type == "claude"


def test_providers_absent_in_config_file(tmp_path: Path) -> None:
    """Loading a config without a 'providers' block produces the same defaults."""
    p = tmp_path / "coordinator.yml"
    p.write_text(_MIN_CONFIG)
    cfg = load(p)
    assert cfg.providers.default == "claude"
    assert "claude" in cfg.providers.definitions
    assert cfg.providers.definitions["claude"].type == "claude"


def test_providers_explicit_default_overrides_claude(tmp_path: Path) -> None:
    """providers.default can override the default provider name."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  default: fast-claude\n"
        "  definitions:\n"
        "    fast-claude:\n"
        "      type: claude\n"
        "      binary: fast-claude-cli\n"
    )
    cfg = load(p)
    assert cfg.providers.default == "fast-claude"
    defn = cfg.providers.definitions["fast-claude"]
    assert defn.type == "claude"
    assert defn.binary == "fast-claude-cli"


def test_providers_all_fields_parsed(tmp_path: Path) -> None:
    """All ProviderDef fields are parsed and stored correctly."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    my-provider:\n"
        "      type: claude\n"
        "      binary: /usr/local/bin/claude\n"
        "      model: sonnet\n"
        "      attach_url: http://localhost:9999\n"
        "      env:\n"
        "        FOO: bar\n"
        "        BAZ: qux\n"
        "      extra_args:\n"
        "        - --dangerously-skip-permissions\n"
        "        - --max-turns\n"
        "        - '100'\n"
    )
    cfg = load(p)
    defn = cfg.providers.definitions["my-provider"]
    assert defn.type == "claude"
    assert defn.binary == "/usr/local/bin/claude"
    assert defn.model == "sonnet"
    assert defn.attach_url == "http://localhost:9999"
    assert defn.env == {"FOO": "bar", "BAZ": "qux"}
    assert defn.extra_args == ["--dangerously-skip-permissions", "--max-turns", "100"]


def test_providers_env_var_expansion(tmp_path: Path, monkeypatch) -> None:
    """${VAR} placeholders in env values are expanded from os.environ."""
    monkeypatch.setenv("COORD_TEST_TOKEN", "secret-token-xyz")
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    remote:\n"
        "      type: claude\n"
        "      env:\n"
        "        API_TOKEN: '${COORD_TEST_TOKEN}'\n"
        "        STATIC_VAL: plain-value\n"
    )
    cfg = load(p)
    env = cfg.providers.definitions["remote"].env
    assert env["API_TOKEN"] == "secret-token-xyz"
    assert env["STATIC_VAL"] == "plain-value"


def test_providers_env_var_expansion_unset_var_left_as_is(tmp_path: Path, monkeypatch) -> None:
    """When ${VAR} is not set in os.environ, the literal placeholder is kept."""
    monkeypatch.delenv("COORD_DEFINITELY_UNSET_VAR", raising=False)
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    p:\n"
        "      type: claude\n"
        "      env:\n"
        "        KEY: '${COORD_DEFINITELY_UNSET_VAR}'\n"
    )
    cfg = load(p)
    # Unset var → placeholder stays as-is
    assert cfg.providers.definitions["p"].env["KEY"] == "${COORD_DEFINITELY_UNSET_VAR}"


def test_providers_implicit_claude_always_present(tmp_path: Path) -> None:
    """Even when definitions is supplied without 'claude', the implicit entry is added."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    other:\n"
        "      type: claude\n"
    )
    cfg = load(p)
    assert "claude" in cfg.providers.definitions
    assert cfg.providers.definitions["claude"].type == "claude"


def test_providers_not_a_mapping_raises(tmp_path: Path) -> None:
    """'providers' must be a mapping; a list raises ConfigError."""
    p = tmp_path / "coordinator.yml"
    p.write_text(_MIN_CONFIG + "providers: [a, b]\n")
    with pytest.raises(ConfigError, match="providers.*mapping"):
        load(p)


def test_providers_default_non_string_raises(tmp_path: Path) -> None:
    """providers.default must be a non-empty string."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  default: 42\n"
    )
    with pytest.raises(ConfigError, match="providers.default must be a non-empty string"):
        load(p)


def test_providers_definition_missing_type_raises(tmp_path: Path) -> None:
    """Each definition must have a 'type' field."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    notype:\n"
        "      binary: claude-bin\n"
    )
    with pytest.raises(ConfigError, match="type is required"):
        load(p)


def test_providers_definition_env_non_string_value_raises(tmp_path: Path) -> None:
    """Env values must be strings."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    p:\n"
        "      type: claude\n"
        "      env:\n"
        "        KEY: 42\n"
    )
    with pytest.raises(ConfigError, match="env must map strings to strings"):
        load(p)


# ── providers.definitions[*].env OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX (#2321) ─


def test_providers_output_token_max_valid_value_accepted(tmp_path: Path) -> None:
    """A bare positive-integer string is accepted and stored verbatim."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    oc:\n"
        "      type: opencode\n"
        "      env:\n"
        "        OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX: '131072'\n"
    )
    cfg = load(p)
    env = cfg.providers.definitions["oc"].env
    assert env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] == "131072"


@pytest.mark.parametrize(
    "bad_value",
    [
        "131072 ",  # trailing whitespace — opencode's parser silently discards
        " 131072",  # leading whitespace
        "131_072",  # underscore digit-grouping — Number() coercion rejects
        "0",  # not > 0
        "-1",  # not > 0, has a sign
        "1.5",  # decimal point
        "unlimited",  # non-numeric
        "",  # empty
    ],
)
def test_providers_output_token_max_bad_value_raises(
    tmp_path: Path, bad_value: str
) -> None:
    """A value opencode's own parser would silently discard is rejected
    loudly at config-parse time (#2321) instead of silently reverting to
    the 32000 default with no warning."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    oc:\n"
        "      type: opencode\n"
        "      env:\n"
        f"        OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX: {bad_value!r}\n"
    )
    with pytest.raises(ConfigError, match="OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"):
        load(p)


def test_providers_output_token_max_absent_does_not_raise(tmp_path: Path) -> None:
    """Not setting the key at all is fine — env() supplies its own default."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    oc:\n"
        "      type: opencode\n"
    )
    cfg = load(p)
    assert "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX" not in cfg.providers.definitions["oc"].env


def test_providers_output_token_max_validated_after_var_expansion(
    tmp_path: Path, monkeypatch
) -> None:
    """The value is validated AFTER ${VAR} expansion — an expanded bad
    value is caught, not just a literal one."""
    monkeypatch.setenv("COORD_TEST_BAD_TOKEN_MAX", "not-a-number")
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    oc:\n"
        "      type: opencode\n"
        "      env:\n"
        "        OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX: '${COORD_TEST_BAD_TOKEN_MAX}'\n"
    )
    with pytest.raises(ConfigError, match="OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"):
        load(p)


def test_providers_extra_args_non_string_element_raises(tmp_path: Path) -> None:
    """extra_args elements must be strings."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    p:\n"
        "      type: claude\n"
        "      extra_args:\n"
        "        - 99\n"
    )
    with pytest.raises(ConfigError, match="extra_args must be a list of strings"):
        load(p)


# ── providers.labels (#1889) ──────────────────────────────────────────────────


def test_providers_labels_absent_defaults_to_empty(tmp_path: Path) -> None:
    """No 'labels' key -> ProvidersConfig.labels is an empty dict."""
    p = tmp_path / "coordinator.yml"
    p.write_text(_MIN_CONFIG)
    cfg = load(p)
    assert cfg.providers.labels == {}


def test_providers_labels_parsed_when_provider_defined(tmp_path: Path) -> None:
    """A label -> provider mapping is parsed when the provider is a known
    definition (implicit 'claude' or an explicit entry)."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    opencode:\n"
        "      type: opencode\n"
        "  labels:\n"
        "    harness:opencode: opencode\n"
        "    harness:claude: claude\n"
    )
    cfg = load(p)
    assert cfg.providers.labels == {
        "harness:opencode": "opencode",
        "harness:claude": "claude",
    }


def test_providers_labels_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(_MIN_CONFIG + "providers:\n  labels: [a, b]\n")
    with pytest.raises(ConfigError, match="providers.labels must be a mapping"):
        load(p)


def test_providers_labels_non_string_value_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  labels:\n"
        "    harness:opencode: 42\n"
    )
    with pytest.raises(ConfigError, match="providers.labels must be a mapping"):
        load(p)


def test_providers_labels_unknown_provider_raises(tmp_path: Path) -> None:
    """#1889 acceptance: a providers.labels value naming an undefined
    provider fails coord.config.load — the same parse-time posture
    reviews.provider uses (#1811), so a typo'd label mapping is a
    config-load error, not a 2am dispatch-time surprise."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  labels:\n"
        "    harness:totally-unregistered: totally-unregistered\n"
    )
    with pytest.raises(
        ConfigError, match="providers.labels references unknown provider"
    ):
        load(p)


def test_providers_labels_validated_against_own_block_definitions(tmp_path: Path) -> None:
    """A provider defined in the SAME providers: block (not just the
    implicit 'claude') is a valid labels target — labels are validated
    after definitions are parsed."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        _MIN_CONFIG
        + "providers:\n"
        "  definitions:\n"
        "    fast-claude:\n"
        "      type: claude\n"
        "  labels:\n"
        "    tier:small: fast-claude\n"
    )
    cfg = load(p)
    assert cfg.providers.labels == {"tier:small": "fast-claude"}


# ── Repo.provider (#323) ──────────────────────────────────────────────────────


def test_repo_provider_absent_defaults_to_none(tmp_path: Path) -> None:
    """When 'provider' is absent from a repo entry, Repo.provider is None."""
    p = tmp_path / "coordinator.yml"
    p.write_text(_MIN_CONFIG)
    cfg = load(p)
    assert cfg.repo("api").provider is None


def test_repo_provider_parsed(tmp_path: Path) -> None:
    """Repo.provider is parsed and stored when present."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: a/a\n"
        "    provider: fast-claude\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    assert cfg.repo("api").provider == "fast-claude"


def test_repo_provider_non_string_raises(tmp_path: Path) -> None:
    """repos[i].provider must be a string when present."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: a/a\n    provider: 42\n"
        "machines:\n"
        "  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="provider must be a string"):
        load(p)


# ── ProvidersConfig standalone tests ─────────────────────────────────────────


def test_providers_config_default_constructor() -> None:
    """ProvidersConfig() produces default='claude' with implicit claude entry."""
    cfg = ProvidersConfig()
    assert cfg.default == "claude"
    assert "claude" in cfg.definitions
    assert isinstance(cfg.definitions["claude"], ProviderDef)
    assert cfg.definitions["claude"].type == "claude"


def test_providers_config_explicit_claude_entry_not_duplicated() -> None:
    """When 'claude' is supplied explicitly, __post_init__ does not add a second one."""
    custom_def = ProviderDef(type="claude", binary="my-claude")
    cfg = ProvidersConfig(definitions={"claude": custom_def})
    assert cfg.definitions["claude"] is custom_def


def test_provider_def_defaults() -> None:
    """ProviderDef optional fields default to None / empty."""
    defn = ProviderDef(type="claude")
    assert defn.binary is None
    assert defn.model is None
    assert defn.attach_url is None
    assert defn.env == {}
    assert defn.extra_args == []


# ── #920: merge.sibling_overlap_aging_hours ─────────────────────────────────

def test_merge_defaults() -> None:
    cfg = _parse_merge(None)
    assert cfg.auto_drain is False
    assert cfg.sibling_overlap_aging_hours == 24.0


def test_merge_sibling_overlap_aging_hours_parses() -> None:
    assert _parse_merge({"sibling_overlap_aging_hours": 6}).sibling_overlap_aging_hours == 6.0
    assert _parse_merge({"sibling_overlap_aging_hours": 0}).sibling_overlap_aging_hours == 0.0
    assert _parse_merge(
        {"sibling_overlap_aging_hours": 12.5}
    ).sibling_overlap_aging_hours == 12.5


def test_merge_sibling_overlap_aging_hours_rejects_negative() -> None:
    with pytest.raises(
        ConfigError, match="sibling_overlap_aging_hours must be a non-negative number"
    ):
        _parse_merge({"sibling_overlap_aging_hours": -1})


def test_merge_sibling_overlap_aging_hours_rejects_bool() -> None:
    with pytest.raises(
        ConfigError, match="sibling_overlap_aging_hours must be a non-negative number"
    ):
        _parse_merge({"sibling_overlap_aging_hours": True})


def test_merge_sibling_overlap_aging_hours_rejects_non_number() -> None:
    with pytest.raises(
        ConfigError, match="sibling_overlap_aging_hours must be a non-negative number"
    ):
        _parse_merge({"sibling_overlap_aging_hours": "lots"})


# ── #2829: merge.auto_revalidate ────────────────────────────────────────────

def test_merge_auto_revalidate_defaults() -> None:
    """An absent merge: block (or one that omits auto_revalidate) means the
    daemon never starts a suite run on its own -- byte-identical to today."""
    cfg = _parse_merge(None)
    assert cfg.auto_revalidate is False
    assert cfg.auto_revalidate_max_batch == 3


def test_merge_auto_revalidate_parses_true() -> None:
    assert _parse_merge({"auto_revalidate": True}).auto_revalidate is True
    assert _parse_merge({"auto_revalidate": False}).auto_revalidate is False


def test_merge_auto_revalidate_rejects_non_bool() -> None:
    with pytest.raises(ConfigError, match="merge.auto_revalidate must be a boolean"):
        _parse_merge({"auto_revalidate": "true"})


def test_merge_auto_revalidate_max_batch_parses() -> None:
    assert _parse_merge({"auto_revalidate_max_batch": 1}).auto_revalidate_max_batch == 1
    assert _parse_merge({"auto_revalidate_max_batch": 5}).auto_revalidate_max_batch == 5


def test_merge_auto_revalidate_max_batch_rejects_non_positive() -> None:
    with pytest.raises(
        ConfigError, match="auto_revalidate_max_batch must be a positive integer"
    ):
        _parse_merge({"auto_revalidate_max_batch": 0})


def test_merge_auto_revalidate_max_batch_rejects_bool() -> None:
    with pytest.raises(
        ConfigError, match="auto_revalidate_max_batch must be a positive integer"
    ):
        _parse_merge({"auto_revalidate_max_batch": True})


def test_merge_auto_revalidate_max_batch_rejects_non_int() -> None:
    with pytest.raises(
        ConfigError, match="auto_revalidate_max_batch must be a positive integer"
    ):
        _parse_merge({"auto_revalidate_max_batch": "lots"})


# ── #827: store.backend / store.dsn ──────────────────────────────────────────


def test_store_defaults_to_sqlite_with_no_dsn() -> None:
    """Absent `store:` block == today's behaviour, byte-for-byte: SQLite,
    with `coord.db` resolving its own default path independently."""
    cfg = _parse_store(None)
    assert cfg == StoreConfig(backend="sqlite", dsn=None)


def test_store_explicit_sqlite_backend_needs_no_dsn() -> None:
    cfg = _parse_store({"backend": "sqlite"})
    assert cfg.backend == "sqlite"
    assert cfg.dsn is None


def test_store_postgres_backend_with_dsn_parses() -> None:
    cfg = _parse_store({"backend": "postgres", "dsn": "postgresql://user:pw@host:5432/db"})
    assert cfg.backend == "postgres"
    assert cfg.dsn == "postgresql://user:pw@host:5432/db"


def test_store_postgres_backend_without_dsn_rejected() -> None:
    with pytest.raises(ConfigError, match="store.dsn is required when store.backend is 'postgres'"):
        _parse_store({"backend": "postgres"})


def test_store_rejects_unknown_backend() -> None:
    with pytest.raises(ConfigError, match="store.backend must be one of"):
        _parse_store({"backend": "mysql"})


def test_store_rejects_non_string_backend() -> None:
    with pytest.raises(ConfigError, match="store.backend must be one of"):
        _parse_store({"backend": 1})


def test_store_rejects_empty_dsn() -> None:
    with pytest.raises(ConfigError, match="store.dsn must be a non-empty string"):
        _parse_store({"backend": "postgres", "dsn": ""})


def test_store_rejects_non_string_dsn() -> None:
    with pytest.raises(ConfigError, match="store.dsn must be a non-empty string"):
        _parse_store({"backend": "postgres", "dsn": 123})


def test_store_rejects_non_mapping() -> None:
    with pytest.raises(ConfigError, match="'store' must be a mapping"):
        _parse_store(["backend", "postgres"])


def test_store_dsn_ignored_when_backend_is_sqlite() -> None:
    """`dsn` is only consulted when `backend: postgres` (see StoreConfig's
    docstring) -- setting it alongside `backend: sqlite` is accepted, not an
    error, and simply carried through unused."""
    cfg = _parse_store({"backend": "sqlite", "dsn": "postgresql://unused@host/db"})
    assert cfg.backend == "sqlite"
    assert cfg.dsn == "postgresql://unused@host/db"


def test_full_config_wires_store_block_through(tmp_path: Path) -> None:
    """End-to-end: `store:` in a real coordinator.yml reaches `Config.store`
    via `load()`, not just the `_parse_store` unit."""
    config_path = tmp_path / "coordinator.yml"
    config_path.write_text(
        "repos:\n  - name: a\n    github: x/a\n"
        "machines:\n  - name: m\n    host: h\n    repos: [a]\n"
        "store:\n  backend: postgres\n  dsn: postgresql://user@host/db\n"
    )
    cfg = load(config_path)
    assert cfg.store == StoreConfig(backend="postgres", dsn="postgresql://user@host/db")


def test_full_config_defaults_store_when_block_absent(valid_config_path: Path) -> None:
    cfg = load(valid_config_path)
    assert cfg.store == StoreConfig()


# ── smoke_tests.capability_rules[].command (#3056) ──────────────────────────


def _write_smoke_config(tmp_path: Path, capability_rules_yaml: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "machines:\n"
        "  - name: dell64\n"
        "    host: dell64.tail\n"
        "    repos: [quadraui]\n"
        "smoke_tests:\n"
        "  capability_rules:\n" + capability_rules_yaml
    )
    return p


def test_capability_rule_command_absent_is_none(tmp_path: Path) -> None:
    """No `command` key: byte-identical to pre-#3056 — the rule only routes."""
    cfg = load(
        _write_smoke_config(
            tmp_path,
            "    - files: ['src/win/']\n      requires: [windows]\n",
        )
    )
    rule = cfg.smoke_tests.capability_rules[0]
    assert rule.files == ["src/win/"]
    assert rule.requires == ["windows"]
    assert rule.command is None


def test_capability_rule_command_round_trips(tmp_path: Path) -> None:
    cfg = load(
        _write_smoke_config(
            tmp_path,
            "    - files: ['src/win/']\n"
            "      requires: [windows]\n"
            "      command: 'cargo xwin test --target x86_64-pc-windows-msvc'\n",
        )
    )
    rule = cfg.smoke_tests.capability_rules[0]
    assert rule.command == "cargo xwin test --target x86_64-pc-windows-msvc"


def test_capability_rule_command_rejects_non_string(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError,
        match=r"capability_rules\[0\]\.command must be a string",
    ):
        load(
            _write_smoke_config(
                tmp_path,
                "    - files: ['src/win/']\n"
                "      requires: [windows]\n"
                "      command: 42\n",
            )
        )


def test_capability_rule_command_rejects_empty_string(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError,
        match=r"capability_rules\[0\]\.command must be non-empty",
    ):
        load(
            _write_smoke_config(
                tmp_path,
                "    - files: ['src/win/']\n"
                "      requires: [windows]\n"
                "      command: ''\n",
            )
        )


def test_capability_rule_command_rejects_whitespace_only_string(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ConfigError,
        match=r"capability_rules\[0\]\.command must be non-empty",
    ):
        load(
            _write_smoke_config(
                tmp_path,
                "    - files: ['src/win/']\n"
                "      requires: [windows]\n"
                "      command: '   '\n",
            )
        )


def test_capability_rule_command_index_named_for_second_rule(tmp_path: Path) -> None:
    """Index in the error message matches the rule's position, so a config
    with several rules points the operator at the right one."""
    with pytest.raises(
        ConfigError,
        match=r"capability_rules\[1\]\.command must be a string",
    ):
        load(
            _write_smoke_config(
                tmp_path,
                "    - files: ['src/gtk/']\n"
                "      requires: [gtk]\n"
                "    - files: ['src/win/']\n"
                "      requires: [windows]\n"
                "      command: false\n",
            )
        )
