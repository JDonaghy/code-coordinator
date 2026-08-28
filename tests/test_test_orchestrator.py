"""Unit tests for coord/test_orchestrator.py — Phase A of #342.

Covers:
- JSON shape validation (_validate_plan): valid passes, missing keys rejected,
  extra keys allowed, steps capped at 8.
- Manifest-merging logic: when a non-empty manifest is available, it appears
  in the generated prompt (which prompts Claude to prefer pull steps).
- Retry-once behaviour: first call returns malformed JSON → second call is
  made with a "your previous output was not valid JSON; try again" hint.
- generate_plan falls back to the error dict when both attempts fail.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from coord.config import Config
from coord.models import Machine, Repo
from coord.test_orchestrator import (
    PLAN_SYSTEM_PROMPT,
    _COORD_CONFIG_RE,
    _build_user_prompt,
    _strip_fences,
    _validate_plan,
    find_local_repo_path,
    generate_plan,
    local_machine,
)


# ── PLAN_SYSTEM_PROMPT & _COORD_CONFIG_RE (#1100) ────────────────────────────

class TestCoordConfigRegex:
    """_COORD_CONFIG_RE must match COORD_CONFIG=<value> tokens only."""

    def test_matches_simple_env_var(self) -> None:
        assert _COORD_CONFIG_RE.search("COORD_CONFIG=/tmp/coord.yml coord status")

    def test_matches_home_dir_path(self) -> None:
        assert _COORD_CONFIG_RE.search(
            "COORD_CONFIG=/home/user/.coord/coordinator.yml coord test-plan abc"
        )

    def test_does_not_match_timeout_prefix(self) -> None:
        assert _COORD_CONFIG_RE.search("timeout 3 coord status") is None

    def test_does_not_match_plain_coord_command(self) -> None:
        assert _COORD_CONFIG_RE.search("coord test-plan abc") is None

    def test_substitution_leaves_rest_intact(self) -> None:
        cmd = "COORD_CONFIG=/tmp/c.yml coord status --freshness"
        cleaned = _COORD_CONFIG_RE.sub("", cmd).strip()
        assert cleaned == "coord status --freshness"


class TestPlanSystemPromptContainsCoordConfigRule:
    """PLAN_SYSTEM_PROMPT must instruct Claude not to emit COORD_CONFIG= (#1100)."""

    def test_system_prompt_prohibits_coord_config(self) -> None:
        assert "COORD_CONFIG" in PLAN_SYSTEM_PROMPT


# ── _strip_fences ─────────────────────────────────────────────────────────────

class TestStripFences:
    def test_no_fence(self) -> None:
        raw = '{"steps": [], "blockers": []}'
        assert _strip_fences(raw) == raw.strip()

    def test_json_fence(self) -> None:
        raw = '```json\n{"steps": [], "blockers": []}\n```'
        assert _strip_fences(raw) == '{"steps": [], "blockers": []}'

    def test_bare_fence(self) -> None:
        raw = '```\n{"steps": [], "blockers": []}\n```'
        assert _strip_fences(raw) == '{"steps": [], "blockers": []}'

    def test_extra_whitespace(self) -> None:
        raw = '  \n```json\n{"steps": [], "blockers": []}\n```\n  '
        assert _strip_fences(raw.strip()) == '{"steps": [], "blockers": []}'


# ── _validate_plan ────────────────────────────────────────────────────────────

class TestValidatePlan:
    def _valid(self) -> dict:
        return {
            "steps": [
                {"kind": "pull", "cmd": "coord pull-artifact abc", "label": "binary"},
                {"kind": "run", "cmd": "pytest tests/"},
                {"kind": "verify", "check": "exit code 0, no FAILED lines"},
            ],
            "blockers": ["python 3.12 required"],
        }

    def test_valid_plan_passes(self) -> None:
        result = _validate_plan(self._valid())
        assert result["blockers"] == ["python 3.12 required"]
        assert len(result["steps"]) == 3
        assert result["steps"][0]["kind"] == "pull"

    def test_extra_keys_are_allowed(self) -> None:
        plan = self._valid()
        plan["steps"][0]["extra_key"] = "ignored"
        plan["future_field"] = "ignored"
        result = _validate_plan(plan)
        # Extra key on step is preserved.
        assert result["steps"][0].get("extra_key") == "ignored"

    def test_missing_steps_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required key 'steps'"):
            _validate_plan({"blockers": []})

    def test_missing_blockers_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required key 'blockers'"):
            _validate_plan({"steps": []})

    def test_not_a_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a JSON object"):
            _validate_plan([{"kind": "run"}])

    def test_steps_not_a_list_raises(self) -> None:
        with pytest.raises(ValueError, match="'steps' must be an array"):
            _validate_plan({"steps": "run tests", "blockers": []})

    def test_invalid_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid kind"):
            _validate_plan({"steps": [{"kind": "unknown"}], "blockers": []})

    def test_steps_capped_at_8(self) -> None:
        many_steps = [{"kind": "run", "cmd": f"echo {i}"} for i in range(12)]
        result = _validate_plan({"steps": many_steps, "blockers": []})
        assert len(result["steps"]) == 8

    def test_blockers_stringified(self) -> None:
        result = _validate_plan({"steps": [], "blockers": [42, "need gtk"]})
        assert result["blockers"] == ["42", "need gtk"]

    def test_step_element_not_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="step 0 must be an object"):
            _validate_plan({"steps": ["not a dict"], "blockers": []})

    def test_all_valid_kinds(self) -> None:
        for kind in ("pull", "run", "verify"):
            result = _validate_plan({"steps": [{"kind": kind}], "blockers": []})
            assert result["steps"][0]["kind"] == kind

    # ── COORD_CONFIG stripping (#1100) ────────────────────────────────────

    def test_coord_config_stripped_from_cmd(self) -> None:
        """COORD_CONFIG=<value> prepended by Claude is removed from cmd fields."""
        plan = {
            "steps": [
                {
                    "kind": "run",
                    "cmd": "COORD_CONFIG=/home/user/.coord/coordinator.yml coord test-plan abc",
                }
            ],
            "blockers": [],
        }
        result = _validate_plan(plan)
        assert result["steps"][0]["cmd"] == "coord test-plan abc"

    def test_coord_config_stripped_when_not_at_start(self) -> None:
        """COORD_CONFIG=<value> is stripped even when preceded by other tokens."""
        plan = {
            "steps": [
                {
                    "kind": "run",
                    "cmd": "env COORD_CONFIG=/tmp/coord.yml coord status",
                }
            ],
            "blockers": [],
        }
        result = _validate_plan(plan)
        assert result["steps"][0]["cmd"] == "env coord status"

    def test_other_leading_tokens_untouched(self) -> None:
        """Tokens that are not COORD_CONFIG=... are preserved (e.g. 'timeout 3 ...')."""
        plan = {
            "steps": [
                {"kind": "run", "cmd": "timeout 3 coord status"},
            ],
            "blockers": [],
        }
        result = _validate_plan(plan)
        assert result["steps"][0]["cmd"] == "timeout 3 coord status"

    def test_cmd_without_coord_config_unchanged(self) -> None:
        """A normal cmd with no COORD_CONFIG token is returned as-is."""
        plan = {
            "steps": [{"kind": "run", "cmd": "pytest tests/ -x"}],
            "blockers": [],
        }
        result = _validate_plan(plan)
        assert result["steps"][0]["cmd"] == "pytest tests/ -x"

    def test_non_cmd_fields_not_stripped(self) -> None:
        """COORD_CONFIG appearing in 'check' or 'label' fields is NOT stripped."""
        plan = {
            "steps": [
                {
                    "kind": "verify",
                    "check": "COORD_CONFIG is not set in the environment",
                    "label": "COORD_CONFIG should be absent",
                }
            ],
            "blockers": [],
        }
        result = _validate_plan(plan)
        # 'check' and 'label' must survive unchanged.
        assert "COORD_CONFIG" in result["steps"][0]["check"]
        assert "COORD_CONFIG" in result["steps"][0]["label"]


# ── resolve_claude_bin / _call_claude binary resolution (#859) ───────────────

class TestResolveClaudeBin:
    """coord-serve's systemd --user PATH lacks ~/.local/bin (#859) — bare
    'claude' fails on daemon-side plan generation. resolve_claude_bin() must
    resolve an absolute path instead."""

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_BIN", "/opt/custom/claude")
        with patch("shutil.which", return_value="/should/not/be/used/claude"):
            from coord.test_orchestrator import resolve_claude_bin
            assert resolve_claude_bin() == "/opt/custom/claude"

    def test_path_lookup_used_when_no_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        with patch("shutil.which", return_value="/usr/local/bin/claude") as mock_which:
            from coord.test_orchestrator import resolve_claude_bin
            assert resolve_claude_bin() == "/usr/local/bin/claude"
        mock_which.assert_called_once_with("claude")

    def test_falls_back_to_local_bin_when_not_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        with patch("shutil.which", return_value=None):
            from coord.test_orchestrator import resolve_claude_bin
            result = resolve_claude_bin()
        assert result == str(Path.home() / ".local" / "bin" / "claude")


class TestCallClaudeUsesResolvedBinary:
    """_call_claude must invoke the resolved absolute path, not bare 'claude'."""

    def test_subprocess_argv0_is_resolved_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from coord.test_orchestrator import _call_claude

        monkeypatch.setenv("CLAUDE_BIN", "/home/svc/.local/bin/claude")
        fake_result = MagicMock(
            returncode=0, stdout='{"result": "ok"}', stderr=""
        )
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            result = _call_claude("sys", "user")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/home/svc/.local/bin/claude"
        assert result == "ok"


# ── _build_user_prompt / manifest-merging ────────────────────────────────────

class TestBuildUserPrompt:
    """Verify that manifest presence controls what appears in the prompt."""

    def _prompt(self, *, manifest: dict | None = None, diff: str = "diff here") -> str:
        return _build_user_prompt(
            issue_number=42,
            issue_body="Fix the bug",
            claude_md="## Rules",
            diff_text=diff,
            manifest=manifest,
        )

    def test_no_manifest_includes_rebuild_instruction(self) -> None:
        prompt = self._prompt(manifest=None)
        assert "not available" in prompt
        assert "local rebuild" in prompt
        # The phrase that tells Claude there are no artifacts.
        assert "no pre-built artifacts" in prompt.lower() or "not available" in prompt

    def test_non_empty_manifest_included_in_prompt(self) -> None:
        manifest = {
            "files": [{"name": "coord-tui", "size": 2048, "mtime": 1700000000}],
            "total_bytes": 2048,
            "built_by_assignment_id": "abc123",
        }
        prompt = self._prompt(manifest=manifest)
        # Manifest JSON should appear verbatim.
        assert "coord-tui" in prompt
        assert "abc123" in prompt
        # Instruction to prefer pull over rebuild.
        assert "pull-artifact" in prompt or "Pre-built" in prompt

    def test_large_diff_is_truncated(self) -> None:
        big_diff = "+" + "x" * 25_000
        prompt = self._prompt(diff=big_diff)
        assert "truncated" in prompt
        # The raw diff should NOT appear in full.
        assert len(prompt) < 30_000

    def test_diff_present_in_prompt(self) -> None:
        prompt = self._prompt(diff="- old line\n+ new line")
        assert "- old line" in prompt
        assert "+ new line" in prompt

    def test_claude_md_included(self) -> None:
        prompt = self._prompt()
        assert "## Rules" in prompt

    def test_issue_body_included(self) -> None:
        prompt = self._prompt()
        assert "Fix the bug" in prompt


# ── generate_plan — happy path ────────────────────────────────────────────────

def _insert_assignment(
    conn: sqlite3.Connection,
    *,
    assignment_id: str = "abc123",
    branch: str = "issue-42-fix-bug",
) -> None:
    conn.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, branch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (assignment_id, "laptop", "api", "acme/api", 42, "Fix bug", "done", branch),
    )
    conn.commit()


def _make_config() -> MagicMock:
    """Return a minimal Config-like mock."""
    from coord.models import Machine, Repo

    repo = Repo(name="api", github="acme/api", default_branch="main")
    machine = Machine(
        name="laptop",
        host="laptop.tailnet",
        capabilities=["python"],
        repos=["api"],
        repo_paths={"api": "/tmp/nonexistent-repo"},
    )
    cfg = MagicMock()
    cfg.repos = [repo]
    cfg.machines = [machine]
    cfg.repo.return_value = repo
    return cfg


class TestGeneratePlan:
    """Tests for generate_plan() using mocked subprocess and httpx."""

    VALID_PLAN = {"steps": [{"kind": "run", "cmd": "pytest"}], "blockers": []}

    def test_happy_path(self, coord_db: sqlite3.Connection) -> None:
        _insert_assignment(coord_db)
        cfg = _make_config()

        with (
            patch("coord.test_orchestrator._call_claude") as mock_claude,
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value="diff"),
            patch("coord.test_orchestrator._get_issue_body", return_value="Issue body"),
        ):
            mock_claude.return_value = json.dumps(self.VALID_PLAN)
            result = generate_plan("abc123", cfg)

        assert result["steps"] == [{"kind": "run", "cmd": "pytest"}]
        assert result["blockers"] == []
        mock_claude.assert_called_once()

    def test_unknown_assignment_returns_error(self, coord_db: sqlite3.Connection) -> None:
        cfg = _make_config()
        result = generate_plan("no-such-id", cfg)
        assert result["steps"] == []
        assert "not found" in result["blockers"][0]

    def test_manifest_injected_into_prompt_when_non_empty(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """When manifest is non-empty the prompt sent to Claude contains it."""
        _insert_assignment(coord_db)
        cfg = _make_config()
        manifest = {
            "files": [{"name": "coord-tui", "size": 1024, "mtime": 1700000000}],
            "total_bytes": 1024,
            "built_by_assignment_id": "abc123",
        }

        captured_prompts: list[str] = []

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            captured_prompts.append(user)
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch(
                "coord.test_orchestrator._fetch_artifact_manifest",
                return_value=manifest,
            ),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            generate_plan("abc123", cfg)

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        # Manifest content must appear in the prompt.
        assert "coord-tui" in prompt

    def test_empty_manifest_triggers_rebuild_note_in_prompt(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """When manifest is None the prompt tells Claude there are no artifacts."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        captured_prompts: list[str] = []

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            captured_prompts.append(user)
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            generate_plan("abc123", cfg)

        assert "not available" in captured_prompts[0]

    def test_retry_once_on_bad_json(self, coord_db: sqlite3.Connection) -> None:
        """First attempt returns invalid JSON → second attempt is called with hint."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        call_count = 0

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "this is not json"
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            result = generate_plan("abc123", cfg)

        assert call_count == 2, "should have retried exactly once"
        assert result["steps"] == [{"kind": "run", "cmd": "pytest"}]

    def test_retry_hint_contains_error_message(self, coord_db: sqlite3.Connection) -> None:
        """The retry prompt must include 'not valid JSON' language."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        prompts_seen: list[str] = []
        call_count = 0

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            nonlocal call_count
            call_count += 1
            prompts_seen.append(user)
            if call_count == 1:
                return "not json"
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            generate_plan("abc123", cfg)

        assert len(prompts_seen) == 2
        assert "not valid JSON" in prompts_seen[1]

    def test_fallback_on_two_failures(self, coord_db: sqlite3.Connection) -> None:
        """When both attempts fail, generate_plan returns the fallback dict."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        with (
            patch(
                "coord.test_orchestrator._call_claude",
                return_value="still not json",
            ),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            result = generate_plan("abc123", cfg)

        assert result == {"steps": [], "blockers": ["plan generation failed"]}

    def test_claude_error_triggers_retry(self, coord_db: sqlite3.Connection) -> None:
        """A RuntimeError from claude -p on attempt 1 → attempt 2 is still made."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        call_count = 0

        def fake_claude(system: str, user: str, *, model: str = "haiku") -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("claude -p failed (exit 1): some error")
            return json.dumps(self.VALID_PLAN)

        with (
            patch("coord.test_orchestrator._call_claude", side_effect=fake_claude),
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            result = generate_plan("abc123", cfg)

        assert call_count == 2
        assert result["steps"] == [{"kind": "run", "cmd": "pytest"}]

    def test_model_passed_through(self, coord_db: sqlite3.Connection) -> None:
        """The model parameter is forwarded to _call_claude."""
        _insert_assignment(coord_db)
        cfg = _make_config()

        with (
            patch("coord.test_orchestrator._call_claude") as mock_claude,
            patch("coord.test_orchestrator._fetch_artifact_manifest", return_value=None),
            patch("coord.test_orchestrator._get_pr_diff", return_value=""),
            patch("coord.test_orchestrator._get_issue_body", return_value=""),
        ):
            mock_claude.return_value = json.dumps(self.VALID_PLAN)
            generate_plan("abc123", cfg, model="opus")

        _, call_kwargs = mock_claude.call_args
        assert call_kwargs.get("model") == "opus"


# ── Schema migration idempotency ──────────────────────────────────────────────

class TestSchemaMigration:
    """test_plan column migrations are idempotent (safe to run twice)."""

    def test_add_column_twice_is_safe(self) -> None:
        """Running _migrate_add_columns twice must not raise."""
        from coord.db import _migrate_add_columns

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from coord.db import _ensure_schema
        _ensure_schema(conn)

        # A second call to _migrate_add_columns must be a no-op (not raise).
        _migrate_add_columns(conn)
        _migrate_add_columns(conn)

        # Confirm the column exists by inserting a value.
        conn.execute(
            "UPDATE assignments SET test_plan = ? WHERE 1=0",
            ('{"steps":[],"blockers":[]}',),
        )

    def test_test_plan_column_exists_after_schema(self) -> None:
        """The test_plan column must be present in a freshly created schema."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from coord.db import _ensure_schema
        _ensure_schema(conn)

        cursor = conn.execute("PRAGMA table_info(assignments)")
        columns = {row["name"] for row in cursor}
        assert "test_plan" in columns


class TestLocalMachine:
    """#966: `local_machine` extracted out of `find_local_repo_path`'s
    hostname-matching so callers needing the `Machine` object (e.g. an
    acceptance-driver capability check) don't re-derive the match."""

    @staticmethod
    def _config() -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tail.ts.net", capabilities=["rust"],
                    repos=["api"], repo_paths={"api": "/home/laptop/api"},
                ),
                Machine(
                    name="server", host="server.tail.ts.net", capabilities=["gtk"],
                    repos=["api"], repo_paths={"api": "/home/server/api"},
                ),
            ],
        )

    def test_matches_by_machine_name(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "laptop")
        cfg = self._config()
        machine = local_machine(cfg)
        assert machine is not None
        assert machine.name == "laptop"

    def test_matches_by_host_prefix(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "server.tail.ts.net")
        cfg = self._config()
        machine = local_machine(cfg)
        assert machine is not None
        assert machine.name == "server"

    def test_no_match_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "nowhere")
        assert local_machine(self._config()) is None

    def test_matches_by_machine_name_case_insensitively(self, monkeypatch) -> None:
        # #2860: OS hostnames commonly carry capitals (e.g. an Ubuntu install
        # named "john-HP-EliteBook-830-G7-Notebook-PC") while coordinator.yml
        # conventionally lowercases `name`/`host` -- the match must not care.
        monkeypatch.setattr("socket.gethostname", lambda: "LAPTOP")
        cfg = self._config()
        machine = local_machine(cfg)
        assert machine is not None
        assert machine.name == "laptop"

    def test_matches_by_host_prefix_case_insensitively_with_fqdn(self, monkeypatch) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "SERVER.Tail.TS.Net")
        cfg = self._config()
        machine = local_machine(cfg)
        assert machine is not None
        assert machine.name == "server"

    def test_unlisted_hostname_still_returns_none(self, monkeypatch) -> None:
        # A genuinely unrecognized hostname must keep failing closed even
        # after casefolding both sides.
        monkeypatch.setattr("socket.gethostname", lambda: "SOME-OTHER-BOX")
        assert local_machine(self._config()) is None

    def test_find_local_repo_path_still_prefers_matching_machine(self, monkeypatch) -> None:
        # Regression: local_machine() extraction must not change
        # find_local_repo_path's existing preference behavior.
        monkeypatch.setattr("socket.gethostname", lambda: "server")
        cfg = self._config()
        assert find_local_repo_path("api", cfg) == Path("/home/server/api")
