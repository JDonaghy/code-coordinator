"""#2323: `coord retry` (and `_reassign`, the function it shares with
`auto_reassign`) must resolve the provider a retry dispatches through the
same way a first dispatch does.

Root cause: `_reassign`'s own `guard_unattended_dispatch` call never passed
`issue_labels`, so a `harness:opencode`-labelled issue's retry silently
resolved to `claude` (the repo/global default) instead of consulting
`providers.labels` the way `coord/dispatch.py:548` does for a first
dispatch — an explicit, silent provider swap, compounded by a model
escalation that walked the claude tier ladder even though the failed run
never touched a claude model.

`coord drive` resumes a `failed` work row by shelling out `coord retry
<aid>` (`coord.drive.Driver.run_coord` -> `subprocess.run([..., "retry",
aid, ...])`) — the exact same CLI command exercised here, already pinned by
`tests/test_drive.py::
test_failed_work_retries_through_the_cli_then_stops_at_the_cap` (asserts
the produced `Action.command == ("retry", "w1")`). There is no
drive-specific retry implementation to separately test: fixing (and
covering) `coord retry` covers both entry points the issue calls out —
the manual command AND the unattended drive-queue resume.

Fix, verified below:

- `_resolve_retry_provider` threads `issue_labels` into
  `guard_unattended_dispatch` (gated to `failed.type == "work"`, exactly as
  a first dispatch gates it), and raises `RetryProviderMismatch` — refusing
  rather than substituting — when the resolution disagrees with the
  provider the failed run actually used (`Assignment.provider_name`).
- `_reassign` stamps the resolved provider onto the wire payload
  (`payload["provider"]`, mirroring `coord.dispatch`'s
  `_wire_payload_needs_provider_field` byte-identical-for-vanilla-claude
  rule) and the retry `Assignment.provider_name`, instead of resolving it
  purely for a TOS check and discarding it.
- `auto_reassign` (the passive `reconcile()` tick) passes the issue's
  cached labels through too, and skips (rather than crashing the whole
  tick) on a `RetryProviderMismatch`.
- `coord retry`'s CLI walks `cfg.models.next_model`'s claude escalation
  ladder only when the resolved provider is claude-family
  (`coord.config.IMPLICIT_PROVIDER_TYPES`), and echoes the resolved
  provider both up front and in the final "Retried:" summary line.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import (
    Config,
    ConcurrencyConfig,
    ModelsConfig,
    ProviderDef,
    ProvidersConfig,
)
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import (
    RetryProviderMismatch,
    _reassign,
    _resolve_retry_provider,
    describe_retry_provider_mismatch,
    reconcile,
)
from coord.state import get_connection

from .conftest import output_and_stderr


def _seed_issue(
    repo_name: str = "api", number: int = 1, labels: list[str] | None = None,
) -> None:
    """Insert a minimal issue row into the test DB so `get_cached_issue_labels`
    has something to return — mirrors the identical helper in
    `tests/test_cli_issue_create_label.py` (each test module keeps its own
    tiny copy rather than sharing an import across test files)."""
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at)
        VALUES (?, ?, 'Test issue', '', 'open', ?, ?)
        ON CONFLICT (repo_name, number) DO NOTHING
        """,
        (repo_name, number, json.dumps(labels or []), time.time()),
    )
    conn.commit()


def _cfg_with_opencode_label(*, repo_provider: str | None = None) -> Config:
    # #1711: both machines declare `provider:opencode` — `_reassign`'s
    # capability gate (mirroring a first dispatch's
    # `guard_provider_machine_capability`) excludes any machine that
    # doesn't; the gate itself is covered by TestReassignCapabilityGate.
    return Config(
        repos=[Repo(name="api", github="acme/api", provider=repo_provider)],
        machines=[
            Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
                capabilities=["provider:opencode"],
            ),
            Machine(
                name="server", host="server.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
                capabilities=["provider:opencode"],
            ),
        ],
        models=ModelsConfig(default="sonnet"),
        providers=ProvidersConfig(
            default="claude",
            definitions={
                "claude": ProviderDef(type="claude"),
                "opencode": ProviderDef(type="opencode"),
            },
            labels={"harness:opencode": "opencode"},
        ),
    )


def _failed(**overrides) -> Assignment:
    base = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Playable core",
        briefing="b",
        assignment_id="failedid",
        status="failed",
        type="work",
        model="sonnet",
        branch="issue-1-x",
    )
    base.update(overrides)
    return Assignment(**base)


# ── Unit: _resolve_retry_provider ───────────────────────────────────────────


class TestResolveRetryProvider:
    def test_labelled_issue_resolves_through_the_label(self) -> None:
        failed = _failed(provider_name="opencode")
        resolved = _resolve_retry_provider(
            failed, _cfg_with_opencode_label(), ["harness:opencode"],
        )
        assert resolved == "opencode"

    def test_mismatch_raises_naming_both_providers(self) -> None:
        """The failed run actually ran on opencode, but the label is
        unavailable at retry time (removed, or the issue isn't cached) —
        label-blind resolution falls through to the claude default, which
        disagrees with the recorded provider. Refuse, don't substitute."""
        failed = _failed(provider_name="opencode")
        with pytest.raises(RetryProviderMismatch) as exc_info:
            _resolve_retry_provider(failed, _cfg_with_opencode_label(), [])
        assert exc_info.value.failed_provider == "opencode"
        assert exc_info.value.resolved_provider == "claude"

    def test_non_work_type_ignores_labels_matching_first_dispatch_behavior(
        self,
    ) -> None:
        """mock-author/test-author never consulted providers.labels on
        their original dispatch either (coord/dispatch.py:548 gates label
        routing to type=="work") — a retry must resolve the same way, not
        suddenly start consulting the label just because it's a retry."""
        failed = _failed(type="mock-author", provider_name="claude")
        resolved = _resolve_retry_provider(
            failed, _cfg_with_opencode_label(), ["harness:opencode"],
        )
        assert resolved == "claude"

    def test_missing_issue_labels_falls_back_label_blind_not_a_mismatch(
        self,
    ) -> None:
        """issue_labels=None (an uncached issue) can't attempt the label
        match at all — that alone must not read as a mismatch as long as
        the label-blind resolution agrees with what the failed row used."""
        failed = _failed(provider_name="claude")
        resolved = _resolve_retry_provider(failed, _cfg_with_opencode_label(), None)
        assert resolved == "claude"


class TestDescribeRetryProviderMismatch:
    def test_names_both_providers(self) -> None:
        msg = describe_retry_provider_mismatch(
            RetryProviderMismatch("opencode", "claude")
        )
        assert "opencode" in msg
        assert "claude" in msg


# ── Unit: _reassign ─────────────────────────────────────────────────────────


class TestReassignThreadsProvider:
    @patch("coord.reconcile.httpx.post")
    def test_opencode_retry_dispatches_through_opencode(
        self, mock_post: MagicMock,
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()
        failed = _failed(provider_name="opencode", model="opencode/glm-5.2")

        result = _reassign(
            failed, board, _cfg_with_opencode_label(),
            issue_labels=["harness:opencode"],
        )

        assert result is not None
        assert result.provider_name == "opencode"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["provider"] == "opencode"

    @patch("coord.reconcile.httpx.post")
    def test_mismatch_refuses_before_any_dispatch(
        self, mock_post: MagicMock,
    ) -> None:
        board = Board()
        # The failed row actually ran on opencode, but the label isn't
        # available this time -> label-blind resolution would land on
        # claude, which disagrees.
        failed = _failed(provider_name="opencode")

        with pytest.raises(RetryProviderMismatch):
            _reassign(failed, board, _cfg_with_opencode_label(), issue_labels=[])

        mock_post.assert_not_called()

    @patch("coord.reconcile.httpx.post")
    def test_vanilla_claude_retry_omits_provider_field_unchanged(
        self, mock_post: MagicMock,
    ) -> None:
        """Control: an ordinary claude-default retry (no providers: block)
        must not start sending a `provider` field on the wire — #324's
        documented byte-identical-payload guarantee for the uncustomized
        case must survive #2323's changes."""
        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp"}),
            ],
        )
        board = Board()
        failed = _failed()

        result = _reassign(failed, board, cfg)

        assert result is not None
        assert result.provider_name == "claude"
        payload = mock_post.call_args.kwargs["json"]
        assert "provider" not in payload

    @patch("coord.reconcile.httpx.post")
    def test_a_model_dropped_from_the_ladder_is_not_inherited_forever(
        self, mock_post: MagicMock,
    ) -> None:
        """#2383: `failed.model` names a model an operator has since
        removed from `models.escalation` — the auto-reassign call site
        never passes `model=` explicitly (reconcile.py's `newly_failed`
        loop), so without a clamp this would be inherited on EVERY future
        retry of the same lineage, forever, regardless of what the current
        ladder says."""
        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp"}),
            ],
        )
        assert "fable" not in cfg.models.escalation  # sanity: not on the ladder
        board = Board()
        failed = _failed(model="fable")

        result = _reassign(failed, board, cfg)

        assert result is not None
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] != "claude-fable-5"
        assert payload["model"] == cfg.models.resolve(cfg.models.escalation[-1])

    @patch("coord.reconcile.httpx.post")
    def test_an_explicit_model_override_is_never_clamped(
        self, mock_post: MagicMock,
    ) -> None:
        """The clamp is scoped to the INHERITED (`model=None`) path only —
        a caller that explicitly asks for a specific model (e.g. #1291's
        semantic-conflict escalation) must get exactly that model, even one
        off the ordinary ladder."""
        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp"}),
            ],
        )
        board = Board()
        failed = _failed(model="sonnet")

        result = _reassign(failed, board, cfg, model="opus")

        assert result is not None
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == cfg.models.resolve("opus")


# ── #1711: the provider-availability gate, applied to retry routing ────────


class TestReassignCapabilityGate:
    """`_reassign` must apply the same #1711 structural
    provider-availability gate a first dispatch applies
    (`coord.dispatch.dispatch` → `guard_provider_machine_capability`): an
    `opencode` retry must never route to a machine that hasn't declared
    `provider:opencode` in coordinator.yml `machines[].capabilities` — that
    combination only failed at spawn time inside the agent, minutes in."""

    def _cfg(self, server_caps: list[str], laptop_caps: list[str]) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(
                    name="laptop", host="l", repos=["api"],
                    repo_paths={"api": "/tmp/api"}, capabilities=laptop_caps,
                ),
                Machine(
                    name="server", host="s", repos=["api"],
                    repo_paths={"api": "/tmp/api"}, capabilities=server_caps,
                ),
            ],
            models=ModelsConfig(default="sonnet"),
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "opencode": ProviderDef(type="opencode"),
                },
                labels={"harness:opencode": "opencode"},
            ),
        )

    @patch("coord.reconcile.httpx.post")
    def test_skips_machine_lacking_the_capability(
        self, mock_post: MagicMock,
    ) -> None:
        """server can't run opencode, laptop (the machine that failed) can
        — the fallback pass lands the retry back on laptop rather than
        routing to server for an ENOENT at spawn time."""
        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp
        cfg = self._cfg(server_caps=[], laptop_caps=["provider:opencode"])
        failed = _failed(provider_name="opencode", model="opencode/glm-5.2")

        result = _reassign(
            failed, Board(), cfg, issue_labels=["harness:opencode"],
        )

        assert result is not None
        assert result.machine_name == "laptop"
        assert result.provider_name == "opencode"

    @patch("coord.reconcile.httpx.post")
    def test_returns_none_when_no_machine_declares_the_capability(
        self, mock_post: MagicMock,
    ) -> None:
        cfg = self._cfg(server_caps=[], laptop_caps=[])
        failed = _failed(provider_name="opencode", model="opencode/glm-5.2")

        result = _reassign(
            failed, Board(), cfg, issue_labels=["harness:opencode"],
        )

        assert result is None
        mock_post.assert_not_called()

    def test_no_candidate_diagnostic_names_the_missing_capability(
        self,
    ) -> None:
        """describe_no_candidate_machines mirrors the filter: a machine
        excluded for lacking the provider capability is named as such
        instead of reading as free (which would misdirect the operator to
        the network-error explanation)."""
        from coord.reconcile import describe_no_candidate_machines

        cfg = self._cfg(server_caps=[], laptop_caps=[])
        failed = _failed(provider_name="opencode", model="opencode/glm-5.2")

        msg = describe_no_candidate_machines(
            failed, Board(), cfg, issue_labels=["harness:opencode"],
        )

        assert "cannot run provider 'opencode'" in msg
        assert "laptop" in msg and "server" in msg

    @patch("coord.reconcile.httpx.post")
    def test_claude_family_retry_needs_no_declared_capability(
        self, mock_post: MagicMock,
    ) -> None:
        """Control: claude/claude-pty are IMPLICIT_PROVIDER_TYPES — every
        machine supports them without declaring anything, so an ordinary
        claude retry still routes exactly as before #1711 was applied
        here."""
        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp
        cfg = self._cfg(server_caps=[], laptop_caps=[])
        failed = _failed()  # provider_name=None -> implicit claude

        result = _reassign(failed, Board(), cfg)

        assert result is not None
        assert result.machine_name == "server"


# ── CLI: `coord retry` ───────────────────────────────────────────────────


def _config_file_with_opencode_label(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n"
        "  - name: laptop\n    host: l\n    repos: [api]\n"
        "    repo_paths:\n      api: /tmp/api\n"
        "    capabilities: [provider:opencode]\n"
        "  - name: server\n    host: s\n    repos: [api]\n"
        "    repo_paths:\n      api: /tmp/api\n"
        "    capabilities: [provider:opencode]\n"
        "models:\n  default: sonnet\n  escalation: [haiku, sonnet, opus]\n"
        "providers:\n"
        "  default: claude\n"
        "  definitions:\n"
        "    opencode:\n      type: opencode\n"
        "  labels:\n    harness:opencode: opencode\n"
    )
    return p


class TestCliRetryProviderRouting:
    """#2323 acceptance: `coord retry` on a `harness:opencode` work row
    dispatches through `opencode` — asserted on the resolved provider name
    in the dispatch payload, never on process argv."""

    @patch("coord.reconcile.httpx.post")
    def test_direct_retry_dispatches_through_opencode(
        self, mock_post: MagicMock, tmp_path: Path, coord_db,
    ) -> None:
        config_file = _config_file_with_opencode_label(tmp_path)
        _seed_issue(number=1, labels=["harness:opencode"])

        board = Board(completed=[
            _failed(
                assignment_id="workid", provider_name="opencode",
                model="opencode/glm-5.2",
            ),
        ])
        resp = MagicMock()
        resp.json.return_value = {"id": "retry1"}
        resp.raise_for_status = lambda: None
        mock_post.return_value = resp

        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
        ):
            result = CliRunner().invoke(
                main, ["retry", "workid", "--config", str(config_file)],
            )

        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        payload = mock_post.call_args.kwargs["json"]
        assert payload["provider"] == "opencode"
        assert "provider: opencode" in out
        assert "provider=opencode" in out
        # #2323: the claude model ladder must never be walked for an
        # opencode retry — the reported bug escalated sonnet -> opus (then
        # opus -> fable on the next drive-queue retry) for a run that never
        # touched a claude model tier.
        assert "escalating model" not in out

    @patch("coord.reconcile.httpx.post")
    def test_retry_refuses_instead_of_moving_to_claude(
        self, mock_post: MagicMock, tmp_path: Path, coord_db,
    ) -> None:
        """The failed run actually ran on opencode (recorded
        `provider_name`), but the issue isn't in the local label cache at
        retry time — label-blind resolution would fall through to the
        claude default. #1796's rule applied at dispatch: refuse rather
        than silently move the work, don't just leave it undocumented."""
        config_file = _config_file_with_opencode_label(tmp_path)
        # Deliberately no _seed_issue call: get_cached_issue_labels(...) is
        # None for this issue.

        board = Board(completed=[
            _failed(assignment_id="workid", provider_name="opencode"),
        ])

        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
        ):
            result = CliRunner().invoke(
                main, ["retry", "workid", "--config", str(config_file)],
            )

        out = output_and_stderr(result)
        assert result.exit_code == 1, out
        assert "opencode" in out
        assert "claude" in out
        mock_post.assert_not_called()

    @patch("coord.reconcile.httpx.post")
    def test_ordinary_claude_retry_still_escalates(
        self, mock_post: MagicMock, tmp_path: Path, coord_db,
    ) -> None:
        """Regression control: an ordinary claude-provider retry (no
        `providers:` block at all) must keep escalating exactly as
        before — #2323 must not disable escalation universally, only for a
        retry that resolves to a non-claude-family provider."""
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: laptop\n    host: l\n    repos: [api]\n"
            "    repo_paths:\n      api: /tmp/api\n"
            "  - name: server\n    host: s\n    repos: [api]\n"
            "    repo_paths:\n      api: /tmp/api\n"
            "models:\n  default: sonnet\n  escalation: [haiku, sonnet, opus]\n"
        )
        board = Board(completed=[
            _failed(assignment_id="workid2", issue_number=2),
        ])
        resp = MagicMock()
        resp.json.return_value = {"id": "retry2"}
        resp.raise_for_status = lambda: None
        mock_post.return_value = resp

        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
        ):
            result = CliRunner().invoke(
                main, ["retry", "workid2", "--config", str(config_file)],
            )

        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "escalating model: sonnet → opus" in out
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "opus"
        assert "provider" not in payload


# ── auto_reassign: the passive reconcile() tick shares _reassign too ───────


class TestAutoReassignProviderMismatch:
    @patch("coord.reconcile._query_agent")
    @patch("coord.reconcile.httpx.post")
    def test_skips_a_provider_mismatch_instead_of_crashing_the_tick(
        self, mock_post: MagicMock, mock_query: MagicMock, coord_db,
    ) -> None:
        """auto_reassign shares `_reassign` with `coord retry` — a
        RetryProviderMismatch must not propagate out of the passive
        reconcile() tick (that would take down the whole daemon loop over
        one row); it leaves the row failed for a human `coord retry`
        instead, same as every other auto_reassign skip condition."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            concurrency=ConcurrencyConfig(auto_reassign=True),
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "opencode": ProviderDef(type="opencode"),
                },
                labels={"harness:opencode": "opencode"},
            ),
        )
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=1,
                issue_title="Fix", assignment_id="a1", status="running",
                type="work", briefing="do it", provider_name="opencode",
            ),
        ])
        mock_query.return_value = {
            "active": [],
            "completed": [{"id": "a1", "status": "failed", "finished_at": 100.0}],
        }
        # No cached issue labels -> label-blind resolution falls to
        # claude, disagreeing with the row's own recorded "opencode".

        changed = reconcile(board, cfg)

        assert "a1" in changed  # the failure itself is still recorded
        assert not any("[retry]" in a.issue_title for a in board.active)
        mock_post.assert_not_called()


# ── the drive-queue entry point (#2323's unattended path) ─────────────────


class TestDriveResumeOfAFailedWorkRow:
    """#2323 acceptance, second entry point: `coord drive` resuming a
    `failed` work row.

    Drive has no retry implementation of its own — `decide()` returns an
    `Action(kind=RUN, command=("retry", <aid>))` and `Driver.run_coord`
    shells that straight out as `coord retry <aid> --config …`. This test
    stitches the two halves together rather than asserting them apart:
    take the command drive actually decided on, run *that* through the
    CLI, and assert the dispatch payload names `opencode`. Without the
    fix this is the transcript in the issue — drive's own header prints
    `provider: opencode` and four seconds later dispatches claude.
    """

    @patch("coord.reconcile.httpx.post")
    def test_drives_failed_work_row_retry_dispatches_through_opencode(
        self, mock_post: MagicMock, tmp_path: Path, coord_db,
    ) -> None:
        from coord.drive import RUN, DriveCounters, DriveOptions, decide
        from coord.drive_state import IssueState

        # 1. What does `coord drive` do with a failed work row?
        action = decide(
            IssueState(
                repo="api", issue=1, repo_github="acme/api",
                work_aid="workid", work_status="failed",
                work_failure_reason="boom",
            ),
            DriveOptions(machine="laptop", max_work_retries=2),
            DriveCounters(),
            MagicMock(),
            machine="laptop",
            oracle=None,
            gate_checker=MagicMock(),
        )
        assert action.kind == RUN
        assert action.command == ("retry", "workid")

        # 2. Run exactly that command — the same argv `Driver.run_coord`
        #    builds — and assert on the resolved provider in the payload.
        config_file = _config_file_with_opencode_label(tmp_path)
        _seed_issue(number=1, labels=["harness:opencode"])
        board = Board(completed=[
            _failed(
                assignment_id="workid", provider_name="opencode",
                model="opencode/glm-5.2",
            ),
        ])
        resp = MagicMock()
        resp.json.return_value = {"id": "retry1"}
        resp.raise_for_status = lambda: None
        mock_post.return_value = resp

        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
        ):
            result = CliRunner().invoke(
                main, [*action.command, "--config", str(config_file)],
            )

        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        payload = mock_post.call_args.kwargs["json"]
        assert payload["provider"] == "opencode"
        # The drive transcript's compounding defect: each unattended
        # failure walked the claude ladder one more rung (sonnet -> opus
        # -> fable) for a run that never used a claude model.
        assert "escalating model" not in out
        assert payload["model"] == "opencode/glm-5.2"


# ── #2565: a semantic conflict-fix give-up must not be read as success ──────


class TestReconcileConflictFixSemanticMarker:
    """A `claude -p` conflict-fix worker ends its turn (exit 0, agent-
    reported status "done") the same way whether it actually resolved the
    conflict or judged it SEMANTIC and gave up — the worker has no way to
    set its own exit code, so `reconcile()`'s "done" branch cannot trust a
    clean completion alone. The `coord:conflict=semantic` marker in the
    worker's own transcript is the only reliable signal; when it's present,
    `reconcile()` must route the merge entry to the same outcome a
    `succeeded=False` semantic give-up already gets — never silently reset
    it to PENDING, which would just re-dispatch a `coord merge` retry
    against the identical, already-diagnosed conflict."""

    @patch("coord.reconcile._query_agent")
    def test_done_conflict_fix_with_semantic_marker_does_not_reset_to_pending(
        self, mock_query: MagicMock, tmp_path: Path, coord_db,
    ) -> None:
        from coord import merge_queue as mq
        from coord.conflict_fix import SEMANTIC_STUCK_MARKER
        from coord.merge_queue import CONFLICT, HUMAN_REQUIRED, PENDING, QueuedMerge

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        mq.save_queue([
            QueuedMerge(
                assignment_id="merge-1",
                repo_name="api",
                repo_github="acme/api",
                branch="issue-7-thing",
                target_branch="main",
                issue_number=7,
                issue_title="Do the thing",
                state=CONFLICT,
                error="Merge conflict in foo.py",
            ),
        ])

        log = tmp_path / "worker.log"
        log.write_text(
            "STATUS: rebase started\n"
            f"STUCK: {SEMANTIC_STUCK_MARKER} src/foo.py:1-9 — both sides "
            "rewrote parse_args() differently\n"
        )

        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=7,
                issue_title="[conflict-fix] Do the thing",
                assignment_id="fix-1", status="running",
                type="conflict-fix", review_of_assignment_id="merge-1",
            ),
        ])
        mock_query.return_value = {
            "active": [],
            "completed": [{
                "id": "fix-1", "status": "done", "finished_at": 100.0,
                "log_path": str(log),
            }],
        }

        reconcile(board, cfg)

        entry = mq.load_queue()[0]
        assert entry.state != PENDING
        assert entry.state == HUMAN_REQUIRED
        assert "Manual rebase required" in (entry.error or "")

    @patch("coord.reconcile._query_agent")
    def test_done_conflict_fix_without_marker_still_resets_to_pending(
        self, mock_query: MagicMock, tmp_path: Path, coord_db,
    ) -> None:
        """The overwhelming common case — a real fix, no marker in the log —
        is unaffected: `reconcile()` still re-enqueues the merge entry."""
        from coord import merge_queue as mq
        from coord.merge_queue import CONFLICT, PENDING, QueuedMerge

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        mq.save_queue([
            QueuedMerge(
                assignment_id="merge-1",
                repo_name="api",
                repo_github="acme/api",
                branch="issue-7-thing",
                target_branch="main",
                issue_number=7,
                issue_title="Do the thing",
                state=CONFLICT,
                error="Merge conflict in foo.py",
            ),
        ])

        log = tmp_path / "worker.log"
        log.write_text("STATUS: rebase started\nSTATUS: pushed\n")

        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=7,
                issue_title="[conflict-fix] Do the thing",
                assignment_id="fix-1", status="running",
                type="conflict-fix", review_of_assignment_id="merge-1",
            ),
        ])
        mock_query.return_value = {
            "active": [],
            "completed": [{
                "id": "fix-1", "status": "done", "finished_at": 100.0,
                "log_path": str(log),
            }],
        }

        reconcile(board, cfg)

        entry = mq.load_queue()[0]
        assert entry.state == PENDING
        assert entry.error is None
