"""Tests for operational defaults baked into the coordinator.

Covers:
- WORKER_SYSTEM_PROMPT audit step and forbidden-files instruction
- Default ReviewsConfig checklist (platform-neutrality)
- coordinator_only_files parsing and dispatch injection
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coord.agent import (
    WORKER_SYSTEM_PROMPT,
    AssignmentSpec,
    _count_graphify_invocations,
    default_worker_command,
)
from coord.config import ConfigError, ReviewsConfig, load
from coord.models import Machine, Proposal, Repo


# ── WORKER_SYSTEM_PROMPT audit step ─────────────────────────────────────────


def test_worker_system_prompt_contains_audit_step() -> None:
    """Workers must verify a feature doesn't already exist before coding."""
    assert "already implemented" in WORKER_SYSTEM_PROMPT


def test_worker_system_prompt_contains_forbidden_files_instruction() -> None:
    """Workers must be told not to read or modify forbidden files."""
    assert "forbidden files" in WORKER_SYSTEM_PROMPT
    assert "do NOT read or modify them" in WORKER_SYSTEM_PROMPT


def test_worker_system_prompt_requires_clean_build_before_done() -> None:
    """Workers must run the build, fix warnings, and not silently ship them.

    Motivated by smoke testing quadraui#233 — the build emitted warnings the
    worker should have fixed before declaring done, but the prompt didn't
    require it.  The human ended up cleaning up after the worker.
    """
    assert "Before declaring done" in WORKER_SYSTEM_PROMPT
    assert "warnings" in WORKER_SYSTEM_PROMPT
    assert "FIX THEM" in WORKER_SYSTEM_PROMPT
    # Escape hatch for genuinely unfixable warnings — workers must call them
    # out explicitly, not silently leave them.
    assert "explicitly call it out" in WORKER_SYSTEM_PROMPT


# ── #2212: graph-first navigation instruction ───────────────────────────────


def test_worker_system_prompt_contains_graph_first_instruction() -> None:
    """#2212: the rule that workers should query the graphify graph before
    grepping for structure lived only in CLAUDE.md (39.9KB, one paragraph)
    and never survived contact with a real task — #2180's leg ran 48 greps
    and 0 graph queries. The rule must live in the short, always-read
    worker system prompt, name the concrete CLI entry point, and say what
    grep/read ARE still for (exact-string/line confirmation) so this isn't
    read as a blanket grep ban."""
    assert 'graphify query "<question>"' in WORKER_SYSTEM_PROMPT
    assert "query the codebase graph first" in WORKER_SYSTEM_PROMPT
    assert "confirming an exact string or line" in WORKER_SYSTEM_PROMPT


def test_worker_system_prompt_states_no_graph_fallback() -> None:
    """A repo/worktree with no built graph must not stall or STUCK a
    worker — the instruction must say what to do when the graph is
    absent: fall back to grep, silently."""
    assert "graphify-out/graph.json" in WORKER_SYSTEM_PROMPT
    assert "Skip straight to grep" in WORKER_SYSTEM_PROMPT


def test_worker_system_prompt_graph_instruction_present_regardless_of_repo() -> None:
    """The instruction is baked into the static system prompt, not derived
    per-repo — assert it survives `default_worker_command` for two
    differently-named repos/specs, the same shape a real dispatch uses."""
    for repo_name in ("api", "some-other-repo"):
        spec = AssignmentSpec(
            repo_name=repo_name,
            repo_path=f"/tmp/{repo_name}",
            issue_number=1,
            issue_title="t",
            briefing="b",
        )
        argv = default_worker_command(spec)
        idx = argv.index("--system-prompt")
        assert 'graphify query "<question>"' in argv[idx + 1]


def test_default_worker_command_allowed_tools_permits_graph_query_path() -> None:
    """The graph is queried via `graphify query ...` over Bash — the
    worker's --allowedTools must still include Bash so that path is
    reachable (today's allowlist is Read,Edit,Write,Bash,Monitor)."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--allowedTools")
    assert "Bash" in argv[idx + 1].split(",")


def test_worker_system_prompt_hard_rules_unchanged_by_graph_instruction() -> None:
    """The #2212 addition must not displace any of the existing hard
    rules — assert each is still present so a future prompt edit can't
    silently drop one of these while touching the nearby graph bullet."""
    assert "Do NOT run gh commands" in WORKER_SYSTEM_PROMPT
    assert "Work only inside your current working directory" in WORKER_SYSTEM_PROMPT
    assert "NEVER commit or push to main or develop directly" in WORKER_SYSTEM_PROMPT
    assert "This session is ONE-SHOT and non-interactive (#1394)" in WORKER_SYSTEM_PROMPT
    assert (
        "Background-task completion notifications will NEVER reach you"
        in WORKER_SYSTEM_PROMPT
    )
    assert "ALWAYS `git add`, `git commit`, and `git push origin HEAD` BEFORE your" in (
        WORKER_SYSTEM_PROMPT
    )


# ── #2192: free pre-review "missing test" self-check ─────────────────────────


def test_worker_system_prompt_contains_missing_test_self_check() -> None:
    """#2192: #2132 measured 18.5% (5/27) of this repo's blocking reviews as
    "missing required black-box test only" — code that was correct, but
    shipped a user-visible diff with zero test files, a rule CLAUDE.md
    already states. Catching that in the worker's own already-running
    session is free (no new paid leg); the reviewer catching it costs a
    full review + fix + re-review round trip. The self-check must live in
    the "Before declaring done" checklist, name the CLAUDE.md rule, and
    preserve the existing pure-refactor/internal-only escape hatch (say so
    in the final message) rather than re-litigating it."""
    assert "Before declaring done" in WORKER_SYSTEM_PROMPT
    flat = " ".join(WORKER_SYSTEM_PROMPT.split())
    assert "#2192" in flat
    assert "change user-visible behavior AND add/modify zero test files" in flat
    assert "18.5%" in flat
    assert "#2132" in flat
    assert (
        "pure refactor / internal-only change (CLAUDE.md's existing exemption)"
        in flat
    )
    assert "say so explicitly in your final message" in flat


# ── #2212: graphify-invocation counter (measurability) ──────────────────────


def test_count_graphify_invocations_counts_command_tokens() -> None:
    assert _count_graphify_invocations(['graphify query "where is X handled"']) == 1
    assert _count_graphify_invocations(["cd repo && graphify update ."]) == 1
    assert _count_graphify_invocations(["grep -rn foo .", "rg bar ."]) == 0


def test_count_graphify_invocations_ignores_path_substring() -> None:
    """A command that merely mentions `graphify-out/` as a path (not the
    `graphify` binary as a command token) must not be counted — otherwise
    the counter over-reports and the "0 across N legs" signal is unreliable."""
    assert _count_graphify_invocations(["cat graphify-out/graph.json"]) == 0


def test_count_graphify_invocations_empty_list() -> None:
    assert _count_graphify_invocations([]) == 0


def test_count_graphify_invocations_counts_newline_separated_commands() -> None:
    """Claude Code's Bash tool very commonly issues multi-line command
    strings (`cd` then a command on the next line, multi-step scripts) not
    joined with `&&`/`;`. A bare newline must count as a separator, or a
    worker that genuinely queried the graph gets silently undercounted to
    0 — undermining the "0 across N legs" landing signal (#2212 review)."""
    assert _count_graphify_invocations(['cd repo\ngraphify query "foo"']) == 1
    assert _count_graphify_invocations(["set -e\ngraphify update .\n"]) == 1
    assert _count_graphify_invocations(["cd repo\n  graphify query x"]) == 1


# ── Default ReviewsConfig checklist ─────────────────────────────────────────


def test_default_reviews_checklist_includes_platform_neutrality() -> None:
    cfg = ReviewsConfig()
    assert any(
        "platform-specific" in item for item in cfg.checklist
    ), f"expected platform-neutrality check in default checklist, got: {cfg.checklist}"


# ── coordinator_only_files config parsing ────────────────────────────────────


def _minimal_yaml(extra_repo_fields: str = "") -> str:
    return (
        "repos:\n"
        f"  - name: api\n    github: acme/api\n{extra_repo_fields}"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
    )


def test_coordinator_only_files_parsed_from_config(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    coordinator_only_files:\n"
        "      - CLAUDE.md\n"
        "      - coordinator.yml\n"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    repo = cfg.repo("api")
    assert repo is not None
    assert repo.coordinator_only_files == ["CLAUDE.md", "coordinator.yml"]


def test_coordinator_only_files_empty_by_default(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(_minimal_yaml())
    cfg = load(p)
    repo = cfg.repo("api")
    assert repo is not None
    assert repo.coordinator_only_files == []


def test_coordinator_only_files_invalid_type_raises_config_error(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    coordinator_only_files: not-a-list\n"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="coordinator_only_files must be a list of strings"):
        load(p)


# ── dispatch() injects coordinator_only_files into files_forbidden ───────────


def _make_proposal(**overrides) -> Proposal:
    base = dict(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=10,
        issue_title="Fix auth",
        rationale="best fit",
        files_likely=["auth.py"],
        briefing="Fix the auth module",
    )
    base.update(overrides)
    return Proposal(**base)


def _make_config(repo: Repo) -> object:
    from coord.config import Config, ModelsConfig

    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            ),
        ],
        models=ModelsConfig(default="sonnet"),
    )


def test_dispatch_includes_coordinator_only_files_in_forbidden() -> None:
    from coord.dispatch import dispatch

    repo = Repo(
        name="api",
        github="acme/api",
        coordinator_only_files=["CLAUDE.md", "coordinator.yml"],
    )
    cfg = _make_config(repo)
    proposal = _make_proposal()

    with patch("coord.dispatch.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["files_forbidden"] == ["CLAUDE.md", "coordinator.yml"]


def test_dispatch_with_no_coordinator_only_files_still_forbids_claude_md() -> None:
    """#2966: coordinator_only_files was set by zero repos fleet-wide, so
    files_forbidden must not depend on it to protect the repo's own
    rulebook — see coord.models.coordinator_owned_docs. The source list is
    never actually empty any more, unlike pre-#2966."""
    from coord.dispatch import dispatch

    repo = Repo(name="api", github="acme/api")
    cfg = _make_config(repo)
    proposal = _make_proposal()

    with patch("coord.dispatch.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["files_forbidden"] == ["CLAUDE.md"]
