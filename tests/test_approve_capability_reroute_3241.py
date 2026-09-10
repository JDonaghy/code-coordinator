"""#3241 review: `coord approve`'s own capability-reroute wiring.

`coord.dispatch.dispatch()` reroutes a `type="work"` proposal whose declared
`## Files` match `smoke_tests.capability_rules` to a different machine (see
`coord.dispatch.route_work_by_capability`, tested at the unit level in
`tests/test_dispatch.py::TestRouteWorkByCapability` /
`TestDispatchCapabilityRouting`). Those tests call `dispatch()` directly —
they never exercise `coord approve` (`coord/commands/dispatch.py::approve`),
the actual production caller, which precomputes several things keyed to
`proposal.machine_name` BEFORE calling `dispatch()`:

- the operator-facing echo (`click.echo(f"[{p.id}] {p.machine_name} → ...")`);
- the dependency freshness/staleness check and `pull_repos` list, sourced
  from `machine_repos.get(p.machine_name)`;
- the `busy`/`repo_busy_elsewhere` check and `dispatched_this_batch` map
  (#2804's same-batch collision guard).

A reroute happening only INSIDE `dispatch()` (after all of the above already
ran) would leave every one of those stale — the operator told the wrong
machine, a stale/dirty checkout on the REAL target never pulled, and the
#2804 guard defeated for two proposals in one batch that reroute onto the
same machine. `approve()` now runs the SAME `route_work_by_capability` call
itself, up front, before any of that precomputation — this file exercises
that ordering end-to-end through the CLI.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from coord.cli import main


CONFIG_YAML = """\
repos:
  - name: quadraui
    github: acme/quadraui
    default_branch: main
machines:
  - name: dell64
    host: dell64.tailnet
    repos: [quadraui]
    capabilities: [gtk, windows]
    repo_paths:
      quadraui: /tmp/quadraui-dell64
  - name: macmini
    host: macmini.tailnet
    repos: [quadraui]
    capabilities: [macos]
    repo_paths:
      quadraui: /tmp/quadraui-macmini
smoke_tests:
  capability_rules:
    - files: ["quadraui/src/macos/"]
      requires: [macos]
usage_gate:
  mode: disabled
"""


def _config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _invoke_approve(config_file: Path, *extra_args: str):
    return CliRunner().invoke(
        main, ["approve", "1", "--config", str(config_file), *extra_args]
    )


class TestApproveCapabilityReroute:
    def test_echo_names_the_rerouted_machine_not_the_originally_proposed_one(
        self, tmp_path: Path, coord_db
    ) -> None:
        """quadraui#913's shape: `coord plan` proposed dell64 (it doesn't
        know about capability_rules), but the diff is macOS-only. The
        operator-facing echo must name macmini — the machine the work
        actually lands on — not dell64."""
        from coord.models import Proposal
        from coord.state import save_proposals

        save_proposals(
            [
                Proposal(
                    id=1,
                    machine_name="dell64",
                    repo_name="quadraui",
                    issue_number=913,
                    issue_title="Fix macOS backend",
                    rationale="work",
                    files_likely=["quadraui/src/macos/backend.rs"],
                ),
            ]
        )
        config_file = _config_file(tmp_path)

        with patch(
            "coord.github_ops.get_issue", return_value={"labels": []}
        ), patch(
            "coord.dispatch.dispatch_with_retry", return_value={"id": "f-1"}
        ) as mock_dispatch, patch("coord.dispatch.post_briefing"), patch(
            "coord.claim.find_work_claim", return_value=None
        ), patch(
            "coord.network.fetch_repos",
            return_value={"quadraui": {"sha": "X", "branch": "main", "dirty": False}},
        ):
            result = _invoke_approve(config_file)

        assert result.exit_code == 0, result.output
        assert "capability-rerouted dell64 → macmini (#3241" in result.output
        # The echo line itself (printed AFTER the reroute happens) must
        # name macmini, not the originally proposed dell64.
        assert "[1] macmini → quadraui #913" in result.output
        assert "[1] dell64 → quadraui #913" not in result.output
        # dispatch_with_retry is called with the proposal object, whose
        # machine_name must already be the rerouted target.
        args, _ = mock_dispatch.call_args
        assert args[0].machine_name == "macmini"

    def test_freshness_check_runs_against_the_rerouted_machine(
        self, tmp_path: Path, coord_db
    ) -> None:
        """`machine_repos` must be fetched for the REAL target (macmini),
        not the originally proposed dell64 — otherwise a stale/dirty
        checkout on macmini would never be detected or pulled."""
        from coord.models import Proposal
        from coord.state import save_proposals

        save_proposals(
            [
                Proposal(
                    id=1,
                    machine_name="dell64",
                    repo_name="quadraui",
                    issue_number=913,
                    issue_title="Fix macOS backend",
                    rationale="work",
                    files_likely=["quadraui/src/macos/backend.rs"],
                ),
            ]
        )
        config_file = _config_file(tmp_path)

        fetched_for: list[str] = []

        def _fake_fetch_repos(machine):
            fetched_for.append(machine.name)
            return {"quadraui": {"sha": "X", "branch": "main", "dirty": False}}

        with patch(
            "coord.github_ops.get_issue", return_value={"labels": []}
        ), patch(
            "coord.dispatch.dispatch_with_retry", return_value={"id": "f-1"}
        ), patch("coord.dispatch.post_briefing"), patch(
            "coord.claim.find_work_claim", return_value=None
        ), patch(
            "coord.network.fetch_repos", side_effect=_fake_fetch_repos,
        ):
            result = _invoke_approve(config_file)

        assert result.exit_code == 0, result.output
        # The freshness pre-check fetches repos only for machines actually
        # needed post-reroute — dell64 (the stale pre-reroute name) must
        # never be queried, only macmini.
        assert fetched_for == ["macmini"]

    def test_same_batch_collision_guard_keys_off_the_rerouted_machine(
        self, tmp_path: Path, coord_db
    ) -> None:
        """#2804: two proposals in the SAME batch that both reroute onto
        macmini must see each other in `dispatched_this_batch` — keyed by
        the REAL target, not each proposal's distinct originally-proposed
        machine name."""
        from coord.models import Proposal
        from coord.state import save_proposals

        save_proposals(
            [
                Proposal(
                    id=1,
                    machine_name="dell64",
                    repo_name="quadraui",
                    issue_number=913,
                    issue_title="Fix macOS backend 1",
                    rationale="work",
                    files_likely=["quadraui/src/macos/backend.rs"],
                ),
                Proposal(
                    id=2,
                    machine_name="macmini",
                    repo_name="quadraui",
                    issue_number=914,
                    issue_title="Fix macOS backend 2",
                    rationale="work",
                    files_likely=["quadraui/src/macos/other.rs"],
                ),
            ]
        )
        config_file = _config_file(tmp_path)

        with patch(
            "coord.github_ops.get_issue", return_value={"labels": []}
        ), patch(
            "coord.dispatch.dispatch_with_retry", return_value={"id": "f-1"}
        ) as mock_dispatch, patch("coord.dispatch.post_briefing"), patch(
            "coord.claim.find_work_claim", return_value=None
        ), patch(
            "coord.network.fetch_repos",
            return_value={"quadraui": {"sha": "X", "branch": "main", "dirty": False}},
        ):
            result = CliRunner().invoke(
                main, ["approve", "1,2", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        # Both proposals land on macmini.
        assert mock_dispatch.call_count == 2
        for call in mock_dispatch.call_args_list:
            assert call.args[0].machine_name == "macmini"
