"""Tests for worker command deny-list (Phase 1 safety)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.agent import (
    AssignmentSpec,
    WINDOWS_IMAGE_NAME_KILL_DENY_COMMANDS,
    WORKER_SYSTEM_PROMPT,
    bash_deny_pattern_matches,
    build_deny_prompt,
    default_worker_command,
    deny_commands_for_machine,
    find_denying_bash_pattern,
    is_windows_image_name_kill,
    worker_disallowed_tools,
)
from coord.config import DEFAULT_DENY_COMMANDS, load
from coord.models import WorkerPermissionsConfig


# ── build_deny_prompt ────────────────────────────────────────────────────────


class TestBuildDenyPrompt:
    def test_empty_deny_list_returns_empty_string(self) -> None:
        assert build_deny_prompt([]) == ""

    def test_formats_patterns_into_prompt(self) -> None:
        result = build_deny_prompt(["Bash(git push --force *)", "Bash(rm -rf *)"])
        assert "FORBIDDEN COMMANDS" in result
        assert "git push --force *" in result
        assert "rm -rf *" in result
        assert "STUCK:" in result

    def test_strips_bash_wrapper(self) -> None:
        result = build_deny_prompt(["Bash(git reset --hard *)"])
        # The human-readable line should not include the Bash() wrapper.
        assert "- git reset --hard *" in result
        assert "Bash(" not in result

    def test_preserves_plain_patterns(self) -> None:
        result = build_deny_prompt(["rm -rf /"])
        assert "- rm -rf /" in result


# ── default_worker_command includes deny-list ────────────────────────────────


def _spec(**overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="do the thing",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


class TestDefaultWorkerCommand:
    def test_deny_commands_appear_in_system_prompt(self) -> None:
        spec = _spec(deny_commands=["Bash(git push --force *)"])
        argv = default_worker_command(spec)
        # The system prompt is the arg after --system-prompt.
        idx = argv.index("--system-prompt")
        system_prompt = argv[idx + 1]
        assert "FORBIDDEN COMMANDS" in system_prompt
        assert "git push --force *" in system_prompt

    def test_no_deny_commands_no_forbidden_section(self) -> None:
        spec = _spec(deny_commands=[])
        argv = default_worker_command(spec)
        idx = argv.index("--system-prompt")
        system_prompt = argv[idx + 1]
        assert "FORBIDDEN COMMANDS" not in system_prompt
        # The base prompt should still be there.
        assert "Claude Code worker" in system_prompt

    def test_base_prompt_always_present(self) -> None:
        spec = _spec(deny_commands=["Bash(rm -rf *)"])
        argv = default_worker_command(spec)
        idx = argv.index("--system-prompt")
        system_prompt = argv[idx + 1]
        assert WORKER_SYSTEM_PROMPT in system_prompt

    def test_stream_json_flags_present(self) -> None:
        """Workers must launch with stream-json output for observability."""
        spec = _spec()
        argv = default_worker_command(spec)
        assert "--output-format" in argv
        idx = argv.index("--output-format")
        assert argv[idx + 1] == "stream-json"
        assert "--verbose" in argv

    def test_input_format_stream_json_for_injection(self) -> None:
        """Workers must launch with stream-json input so messages can be
        injected mid-session via AgentServer.inject_message."""
        spec = _spec()
        argv = default_worker_command(spec)
        assert "--input-format" in argv
        idx = argv.index("--input-format")
        assert argv[idx + 1] == "stream-json"

    def test_briefing_not_appended_as_positional_arg(self) -> None:
        """Briefing is sent via stdin (first stream-json user message),
        not as a positional argv entry — keeping a positional briefing
        would force claude into text input mode and break injection."""
        spec = _spec(briefing="DO NOT APPEAR IN ARGV")
        argv = default_worker_command(spec)
        assert spec.briefing not in argv


# ── Config parsing ───────────────────────────────────────────────────────────


class TestWorkerPermissionsConfigParsing:
    def test_default_deny_list_when_no_config(self, tmp_path: Path) -> None:
        """Repos without explicit worker_permissions get the default deny-list."""
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        repo = cfg.repo("api")
        assert repo is not None
        assert repo.worker_permissions is not None
        assert repo.worker_permissions.deny == DEFAULT_DENY_COMMANDS

    def test_custom_deny_list(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions:\n"
            "      deny:\n"
            "        - 'Bash(rm -rf /)'\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        repo = cfg.repo("api")
        assert repo is not None
        assert repo.worker_permissions is not None
        assert repo.worker_permissions.deny == ["Bash(rm -rf /)"]

    def test_empty_deny_list_means_no_restrictions(self, tmp_path: Path) -> None:
        """An explicit `deny: []` clears all restrictions."""
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions:\n"
            "      deny: []\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        repo = cfg.repo("api")
        assert repo is not None
        assert repo.worker_permissions is not None
        assert repo.worker_permissions.deny == []

    def test_allow_list_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions:\n"
            "      allow:\n"
            "        - 'Bash(npm install)'\n"
            "      deny: []\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        repo = cfg.repo("api")
        assert repo.worker_permissions.allow == ["Bash(npm install)"]
        assert repo.worker_permissions.deny == []

    def test_invalid_worker_permissions_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions: true\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        with pytest.raises(Exception, match="worker_permissions must be a mapping"):
            load(p)

    def test_invalid_deny_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions:\n"
            "      deny: not-a-list\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        with pytest.raises(Exception, match="deny must be a list"):
            load(p)


# ── Dispatch payload carries deny_commands ───────────────────────────────────


class TestDispatchDenyCommands:
    def test_dispatch_includes_deny_commands_in_payload(self) -> None:
        from unittest.mock import MagicMock, patch

        from coord.config import Config
        from coord.dispatch import dispatch
        from coord.models import Machine, Repo

        repo = Repo(
            name="api",
            github="acme/api",
            worker_permissions=WorkerPermissionsConfig(
                deny=["Bash(git push --force *)"]
            ),
        )
        cfg = Config(
            repos=[repo],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tailnet",
                    repos=["api"],
                    repo_paths={"api": "/home/user/src/api"},
                ),
            ],
        )
        from coord.models import Proposal

        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix auth",
            rationale="best fit",
            files_likely=["auth.py"],
            briefing="Fix the auth module",
        )

        with patch("coord.dispatch.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ok": True}
            mock_post.return_value = mock_resp

            dispatch(proposal, cfg)

            payload = mock_post.call_args.kwargs["json"]
            assert payload["deny_commands"] == ["Bash(git push --force *)"]

    def test_dispatch_default_deny_when_no_permissions(self) -> None:
        """When worker_permissions uses defaults, deny_commands is the default list."""
        from unittest.mock import MagicMock, patch

        from coord.config import Config
        from coord.dispatch import dispatch
        from coord.models import Machine, Repo

        repo = Repo(
            name="api",
            github="acme/api",
            worker_permissions=WorkerPermissionsConfig(
                deny=list(DEFAULT_DENY_COMMANDS)
            ),
        )
        cfg = Config(
            repos=[repo],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tailnet",
                    repos=["api"],
                    repo_paths={"api": "/home/user/src/api"},
                ),
            ],
        )
        from coord.models import Proposal

        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix auth",
            rationale="best fit",
        )

        with patch("coord.dispatch.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ok": True}
            mock_post.return_value = mock_resp

            dispatch(proposal, cfg)

            payload = mock_post.call_args.kwargs["json"]
            assert payload["deny_commands"] == DEFAULT_DENY_COMMANDS

    def test_dispatch_to_a_windows_machine_adds_the_image_name_kill_guard(self) -> None:
        """#3663: a `windows`-capable machine (dell64) gets the extra
        guard added on top of whatever the repo's own `worker_permissions`
        already specify — AND the shared-desktop briefing note."""
        from unittest.mock import MagicMock, patch

        from coord.config import Config
        from coord.dispatch import WINDOWS_SHARED_DESKTOP_BRIEFING_NOTE, dispatch
        from coord.models import Machine, Proposal, Repo

        repo = Repo(
            name="vimcode",
            github="acme/vimcode",
            worker_permissions=WorkerPermissionsConfig(
                deny=["Bash(git push --force *)"]
            ),
        )
        cfg = Config(
            repos=[repo],
            machines=[
                Machine(
                    name="dell64",
                    host="dell64.tailnet",
                    repos=["vimcode"],
                    repo_paths={"vimcode": "/home/user/src/vimcode"},
                    capabilities=["windows"],
                ),
            ],
        )
        proposal = Proposal(
            id=1,
            machine_name="dell64",
            repo_name="vimcode",
            issue_number=10,
            issue_title="Fix the terminal repro",
            rationale="best fit",
            briefing="Reproduce the Windows Terminal bug",
        )

        with patch("coord.dispatch.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ok": True}
            mock_post.return_value = mock_resp

            dispatch(proposal, cfg)

            payload = mock_post.call_args.kwargs["json"]
            assert "Bash(git push --force *)" in payload["deny_commands"]
            for pattern in WINDOWS_IMAGE_NAME_KILL_DENY_COMMANDS:
                assert pattern in payload["deny_commands"]
            assert WINDOWS_SHARED_DESKTOP_BRIEFING_NOTE in payload["briefing"]

    def test_dispatch_to_a_non_windows_machine_has_no_shared_desktop_note(self) -> None:
        """Positive control: the note/guard must not leak onto a dispatch
        the issue has nothing to do with."""
        from unittest.mock import MagicMock, patch

        from coord.config import Config
        from coord.dispatch import WINDOWS_SHARED_DESKTOP_BRIEFING_NOTE, dispatch
        from coord.models import Machine, Proposal, Repo

        repo = Repo(name="api", github="acme/api")
        cfg = Config(
            repos=[repo],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tailnet",
                    repos=["api"],
                    repo_paths={"api": "/home/user/src/api"},
                ),
            ],
        )
        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix auth",
            rationale="best fit",
            briefing="Fix the auth module",
        )

        with patch("coord.dispatch.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ok": True}
            mock_post.return_value = mock_resp

            dispatch(proposal, cfg)

            payload = mock_post.call_args.kwargs["json"]
            for pattern in WINDOWS_IMAGE_NAME_KILL_DENY_COMMANDS:
                assert pattern not in payload["deny_commands"]
            assert WINDOWS_SHARED_DESKTOP_BRIEFING_NOTE not in payload["briefing"]


# ── WorkerPermissionsConfig dataclass ────────────────────────────────────────


class TestWorkerPermissionsConfigDataclass:
    def test_defaults_are_empty_lists(self) -> None:
        wp = WorkerPermissionsConfig()
        assert wp.allow == []
        assert wp.deny == []

    def test_constructed_with_values(self) -> None:
        wp = WorkerPermissionsConfig(
            allow=["Bash(npm install)"],
            deny=["Bash(rm -rf *)"],
        )
        assert wp.allow == ["Bash(npm install)"]
        assert wp.deny == ["Bash(rm -rf *)"]


# ── new_issue_guidance in system prompt ────────────────────────────────────


class TestNewIssueGuidanceSystemPrompt:
    """#352: new_issue_guidance is included in system prompt for new-issue-chat."""

    def test_new_issue_chat_system_prompt_includes_guidance_when_provided(self) -> None:
        """When spec.new_issue_guidance is non-empty, it's appended to the
        NEW_ISSUE_CHAT_SYSTEM_PROMPT."""
        guidance = "Required sections: Title, Description, Acceptance Criteria"
        spec = _spec(
            type="new-issue-chat",
            new_issue_guidance=guidance,
        )
        argv = default_worker_command(spec)
        idx = argv.index("--system-prompt")
        system_prompt = argv[idx + 1]

        # The base prompt should be there.
        assert "new-issue assistant" in system_prompt
        # The guidance should be appended.
        assert guidance in system_prompt
        # The guidance header should be there.
        assert "repo has the following guidance" in system_prompt

    def test_new_issue_chat_system_prompt_omits_guidance_when_empty(self) -> None:
        """When spec.new_issue_guidance is empty, the system prompt has no
        guidance block."""
        spec = _spec(
            type="new-issue-chat",
            new_issue_guidance="",  # explicitly empty
        )
        argv = default_worker_command(spec)
        idx = argv.index("--system-prompt")
        system_prompt = argv[idx + 1]

        # The base prompt should be there.
        assert "new-issue assistant" in system_prompt
        # The guidance header should NOT be there.
        assert "repo has the following guidance" not in system_prompt

    def test_new_issue_chat_system_prompt_has_deny_list(self) -> None:
        """Even with guidance, NEW_ISSUE_CHAT still includes its deny list."""
        guidance = "Some guidance text"
        spec = _spec(
            type="new-issue-chat",
            new_issue_guidance=guidance,
        )
        argv = default_worker_command(spec)
        idx = argv.index("--system-prompt")
        system_prompt = argv[idx + 1]

        # Both the deny list and guidance should be present.
        assert "FORBIDDEN COMMANDS" in system_prompt
        assert guidance in system_prompt
        assert "gh issue create" in system_prompt


# ── #2314: flag-position-insensitive deny matching ──────────────────────────
#
# `Bash(pip install -e *)` only matched `-e` IMMEDIATELY after `install` — a
# worker evaded it just by putting another flag first. These tests use the
# real matcher (`coord.agent.bash_deny_pattern_matches` /
# `find_denying_bash_pattern`) against `DEFAULT_DENY_COMMANDS` so "this deny
# list actually catches that evasion" is a checked fact, not a claim read off
# the pattern text.


class TestPipDenyEvasion:
    def test_editable_flag_immediately_after_install_is_denied(self) -> None:
        """The original, never-evaded case: nothing inserted before `-e`."""
        assert find_denying_bash_pattern(
            "pip install -e .", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_break_system_packages_before_editable_flag_is_denied(self) -> None:
        """#2314's named evasion 1: a flag pushed in BEFORE `-e` broke the
        old pattern's immediate-adjacency requirement."""
        assert find_denying_bash_pattern(
            "pip install --break-system-packages -e .", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_user_before_editable_flag_is_denied(self) -> None:
        """#2314's named evasion 2."""
        assert find_denying_bash_pattern(
            "pip install --user -e .", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_break_system_packages_after_editable_flag_is_denied(self) -> None:
        """#2314's named evasion 3: a flag pushed in AFTER `-e` — this one
        never actually broke the original adjacent pattern (the `-e` is
        still right after `install`), but is pinned as a regression test
        since it is the third form the issue names explicitly."""
        assert find_denying_bash_pattern(
            "pip install -e --break-system-packages .", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_editable_flag_non_adjacent_to_install_generically_denied(self) -> None:
        """Not just the two named flags — ANY flag inserted before `-e`
        must not create a gap in coverage."""
        assert find_denying_bash_pattern(
            "pip install --quiet --no-cache-dir -e .", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_long_editable_flag_spelling_non_adjacent_is_denied(self) -> None:
        assert find_denying_bash_pattern(
            "pip install --user --editable .", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_python_dash_m_pip_prefix_is_still_denied(self) -> None:
        """`python -m pip install -e .` reaches the exact same installer as
        `pip install -e .` and must be denied identically, including with a
        flag inserted first."""
        assert find_denying_bash_pattern(
            "python -m pip install --user -e .", DEFAULT_DENY_COMMANDS
        ) is not None
        assert find_denying_bash_pattern(
            "python3 -m pip install --user -e .", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_break_system_packages_denied_even_without_editable(self) -> None:
        """`--break-system-packages` is independently dangerous — a plain,
        non-editable `pip install` with it must still be denied."""
        assert find_denying_bash_pattern(
            "pip install --break-system-packages requests", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_user_denied_even_without_editable(self) -> None:
        assert find_denying_bash_pattern(
            "pip install --user requests", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_ordinary_pip_install_is_not_denied(self) -> None:
        """Positive control: the new patterns must not collaterally block a
        completely unrelated, ordinary install."""
        assert find_denying_bash_pattern(
            "pip install requests", DEFAULT_DENY_COMMANDS
        ) is None

    def test_package_name_containing_dash_e_is_not_denied(self) -> None:
        """The `* -e *`/`* --editable *` forms require `-e`/`--editable` as
        their OWN space-delimited token — a package spec that merely
        contains the substring `-e` inside a longer name must not
        collaterally trip the deny list."""
        assert find_denying_bash_pattern(
            "pip install django-extensions", DEFAULT_DENY_COMMANDS
        ) is None


class TestGitPushFlagReorderingEvasion:
    """#2314's audit ask: other `Bash(...)` deny patterns have the identical
    positional weakness whenever a real CLI lets its flags be reordered."""

    def test_force_immediately_after_push_is_denied(self) -> None:
        assert find_denying_bash_pattern(
            "git push --force origin main", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_force_after_an_inserted_flag_is_denied(self) -> None:
        assert find_denying_bash_pattern(
            "git push --quiet --force origin main", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_dash_f_after_an_inserted_flag_is_denied(self) -> None:
        assert find_denying_bash_pattern(
            "git push --quiet -f origin main", DEFAULT_DENY_COMMANDS
        ) is not None

    def test_force_with_lease_is_not_denied(self) -> None:
        """The intentional non-match: `--force-with-lease` is the sanctioned
        safe alternative (see coord/conflict_fix.py) and must stay allowed —
        the reordering fix must not widen the pattern into matching it."""
        assert find_denying_bash_pattern(
            "git push --force-with-lease origin main", DEFAULT_DENY_COMMANDS
        ) is None

    def test_rm_rf_letter_swap_is_denied(self) -> None:
        """`rm -fr` is byte-for-byte equivalent to `rm -rf` to the real
        `rm` binary; the deny list must not care about the letter order."""
        assert find_denying_bash_pattern(
            "rm -fr /tmp/scratch", DEFAULT_DENY_COMMANDS
        ) is not None


class TestBashDenyPatternMatcher:
    """Direct unit tests for the matcher itself, independent of any
    particular deny list."""

    def test_non_bash_pattern_never_matches(self) -> None:
        assert not bash_deny_pattern_matches("Edit(secrets/**)", "pip install -e .")

    def test_exact_prefix_match(self) -> None:
        assert bash_deny_pattern_matches("Bash(rm -rf *)", "rm -rf /tmp/x")

    def test_no_match_returns_false(self) -> None:
        assert not bash_deny_pattern_matches("Bash(rm -rf *)", "ls -la")

    def test_find_denying_bash_pattern_returns_none_when_nothing_matches(self) -> None:
        assert find_denying_bash_pattern("ls -la", DEFAULT_DENY_COMMANDS) is None

    def test_find_denying_bash_pattern_returns_the_matching_pattern(self) -> None:
        pattern = find_denying_bash_pattern("gh issue list", DEFAULT_DENY_COMMANDS)
        assert pattern == "Bash(gh *)"


# ── #3663: Windows image-name kill guard ─────────────────────────────────────


class TestIsWindowsImageNameKill:
    """#3663: a dell64 worker ran
    ``Get-Process | Where-Object {...} | Stop-Process -Force
    -ErrorAction SilentlyContinue`` ~40 times to "clean up" between manual
    repro attempts — Windows Terminal hosts every window on the box in one
    process, so this killed the user's own terminal too. These are the
    acceptance examples from the issue itself."""

    REAL_WORLD_REPRO_KILL = (
        "powershell.exe -NoProfile -Command 'Get-Process | Where-Object "
        '{$_.ProcessName -match "vcd|WindowsTerminal"} | Stop-Process '
        "-Force -ErrorAction SilentlyContinue'"
    )

    def test_refuses_the_exact_dell64_repro_command(self) -> None:
        assert is_windows_image_name_kill(self.REAL_WORLD_REPRO_KILL)

    def test_refuses_taskkill_by_image_name(self) -> None:
        assert is_windows_image_name_kill("taskkill /IM WindowsTerminal.exe /F")

    def test_refuses_taskkill_by_image_name_lowercase(self) -> None:
        assert is_windows_image_name_kill("taskkill /im openconsole.exe /f")

    def test_refuses_stop_process_by_name_without_a_get_process_pipeline(self) -> None:
        assert is_windows_image_name_kill("Stop-Process -Name WindowsTerminal -Force")

    def test_refuses_pkill_against_a_windows_exe(self) -> None:
        assert is_windows_image_name_kill("pkill -f WindowsTerminal.exe")

    def test_refuses_killall_against_a_windows_exe(self) -> None:
        assert is_windows_image_name_kill("killall WindowsTerminal.exe")

    def test_refuses_wmic_process_kill_by_name(self) -> None:
        assert is_windows_image_name_kill(
            "wmic process where name='WindowsTerminal.exe' delete"
        )

    def test_case_insensitive_unlike_the_posix_deny_patterns(self) -> None:
        """PowerShell/cmd.exe are case-insensitive shells — a differently
        cased invocation of the exact same cmdlet must still be refused."""
        assert is_windows_image_name_kill(
            "get-process | stop-process -force"
        )
        assert is_windows_image_name_kill(
            "GET-PROCESS | STOP-PROCESS -FORCE"
        )

    def test_allows_stop_process_by_pid(self) -> None:
        """Killing a PID the worker itself launched must stay allowed."""
        assert not is_windows_image_name_kill("Stop-Process -Id 1234")

    def test_allows_taskkill_by_pid(self) -> None:
        assert not is_windows_image_name_kill("taskkill /PID 1234")

    def test_allows_an_unrelated_command(self) -> None:
        assert not is_windows_image_name_kill("cargo build --release")


class TestDenyCommandsForMachine:
    """#3663: :func:`deny_commands_for_machine` is the SINGLE function that
    decides whether a dispatch's deny list gets the Windows guard — no
    second, independently-maintained copy of "is this machine windows-
    capable" anywhere else."""

    def test_windows_capable_machine_gets_the_guard_added(self) -> None:
        result = deny_commands_for_machine(["Bash(gh *)"], ["windows"])
        assert "Bash(gh *)" in result
        for pattern in WINDOWS_IMAGE_NAME_KILL_DENY_COMMANDS:
            assert pattern in result

    def test_non_windows_machine_is_unaffected(self) -> None:
        assert deny_commands_for_machine(["Bash(gh *)"], ["gtk"]) == ["Bash(gh *)"]

    def test_empty_base_list_on_a_windows_machine_still_gets_the_guard(self) -> None:
        """An operator's explicit `deny: []` (opt out of every REPO-level
        restriction) must not opt out of this machine-level safety guard."""
        result = deny_commands_for_machine([], ["windows"])
        assert result == WINDOWS_IMAGE_NAME_KILL_DENY_COMMANDS

    def test_no_duplicate_entries_when_base_list_already_has_them(self) -> None:
        base = list(WINDOWS_IMAGE_NAME_KILL_DENY_COMMANDS)
        result = deny_commands_for_machine(base, ["windows"])
        assert result == base


class TestWindowsImageNameKillHardEnforced:
    """#3663: unlike a worker's ordinary `deny_commands` (soft system-
    prompt reminder only), the Windows guard must also land in
    `--disallowedTools` — a prompt rule alone was proven insufficient."""

    def test_disallowed_tools_includes_the_guard_when_present_in_deny_commands(
        self,
    ) -> None:
        spec = AssignmentSpec(
            repo_name="api", repo_path="/tmp/repo", issue_number=1,
            issue_title="t", briefing="do the thing",
            deny_commands=deny_commands_for_machine([], ["windows"]),
        )
        disallowed = worker_disallowed_tools(spec, allowed_tools="Read,Edit,Write,Bash")
        for pattern in WINDOWS_IMAGE_NAME_KILL_DENY_COMMANDS:
            assert pattern in disallowed

    def test_disallowed_tools_omits_the_guard_for_a_non_windows_dispatch(self) -> None:
        spec = AssignmentSpec(
            repo_name="api", repo_path="/tmp/repo", issue_number=1,
            issue_title="t", briefing="do the thing", deny_commands=[],
        )
        disallowed = worker_disallowed_tools(spec, allowed_tools="Read,Edit,Write,Bash")
        for pattern in WINDOWS_IMAGE_NAME_KILL_DENY_COMMANDS:
            assert pattern not in disallowed
