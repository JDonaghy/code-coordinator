"""Tests for the shared local-machine resolver's effect on the interactive
launch path (#3440).

The macmini incident: the OS short hostname (macOS's default
``Johns-Mac-mini`` -> ``johns-mac-mini``) matches neither the machine's
``name: macmini`` nor the ``macmini`` first label of its
``host: macmini.tailf46ef8.ts.net``. Before #3440, ``coord assign
--interactive macmini ...`` therefore treated macmini as REMOTE and SSHed
to itself. This file drives ``coord assign --interactive --dry-run``
end to end against that exact shape and asserts the LOCAL TTY launch is
selected — not ``(remote tmux)`` — via both the ``local_hostnames:`` alias
arm and the Tailscale-identity arm of ``coord.config.resolve_local_machine``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main

# ── Config: the macmini shape (#3440) ───────────────────────────────────────
#
# OS short hostname ("johns-mac-mini") == neither `name` ("macmini") nor the
# first label of `host` ("macmini" from "macmini.tailf46ef8.ts.net" — this
# one DOES coincidentally match the label, so the config below additionally
# renames the host's first label to something that does NOT match, forcing
# resolution through the alias/Tailscale tiers exactly like the real
# incident (macOS's hostname has nothing in common with either).

_CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: macmini
    host: macmini.tailf46ef8.ts.net
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: remotebox
    host: remotebox.tailnet
    repos: [api]
    repo_paths:
      api: ~/src/api
"""

_CONFIG_YAML_WITH_ALIAS = _CONFIG_YAML.replace(
    "    host: macmini.tailf46ef8.ts.net\n",
    "    host: macmini.tailf46ef8.ts.net\n    local_hostnames: [johns-mac-mini]\n",
)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(_CONFIG_YAML)
    return p


@pytest.fixture
def config_file_with_alias(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(_CONFIG_YAML_WITH_ALIAS)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestMacminiLocalResolution:
    """#3440 acceptance: the macmini shape resolves to LOCAL, not remote."""

    def test_dry_run_resolves_local_via_alias_arm(
        self, config_file_with_alias: Path, coord_dir: Path
    ) -> None:
        """OS hostname matches neither `name` nor `host`'s label; the
        `local_hostnames:` alias arm is what saves it. `tailscale` is
        unavailable, proving the alias arm alone is sufficient."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix it"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.config._local_short_hostname", return_value="johns-mac-mini"), \
             patch("coord.config._tailscale_self_dns_name", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "macmini", "api", "42",
                    "--config", str(config_file_with_alias),
                    "--interactive", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "local TTY" in result.output, (
            f"Expected the LOCAL TTY launch, got: {result.output!r}"
        )
        assert "(remote tmux)" not in result.output

    def test_dry_run_resolves_local_via_tailscale_arm(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """No alias configured at all; the Tailscale-identity arm (`Self.
        DNSName` == `host:` exactly) is what saves it — the fix's second
        independent resolution path for the same incident shape."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix it"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.config._local_short_hostname", return_value="johns-mac-mini"), \
             patch(
                 "coord.config._tailscale_self_dns_name",
                 return_value="macmini.tailf46ef8.ts.net",
             ):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "macmini", "api", "42",
                    "--config", str(config_file),
                    "--interactive", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "local TTY" in result.output, (
            f"Expected the LOCAL TTY launch, got: {result.output!r}"
        )
        assert "(remote tmux)" not in result.output

    def test_dry_run_without_any_local_signal_falls_back_to_remote(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Sanity check the gate can fail: with no alias, no Tailscale
        match, and an OS hostname that matches nothing, macmini is (still,
        correctly) treated as remote — this is the pre-#3440 failure mode,
        preserved as a fallback rather than a guess."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix it"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.config._local_short_hostname", return_value="johns-mac-mini"), \
             patch("coord.config._tailscale_self_dns_name", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "macmini", "api", "42",
                    "--config", str(config_file),
                    "--interactive", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "(remote tmux)" in result.output
