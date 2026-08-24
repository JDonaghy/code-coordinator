"""Tests for #437: human-attended interactive launcher + structural ToS guardrail.

Covers the THREE deltas:

(1) Capability flag — ``Capabilities.human_attended_only`` exists, default
    ``False``; ``ClaudeProvider`` reports ``False``; ``ClaudePtyProvider``
    reports ``True``.

(2) STRUCTURAL GUARDRAIL — the unattended dispatch entry points
    (``coord.dispatch.dispatch`` / ``coord.review.dispatch_review`` /
    ``coord.reconcile._reassign``) refuse to route through a provider
    whose capabilities mark it ``human_attended_only``, with the
    ``spec → repo → providers.default`` precedence chain enforced at
    EACH of the three precedence levels.

(3) HUMAN-ATTENDED SPAWN — the agent's PTY path PRE-FILLS the briefing
    via bracketed paste but writes NO trailing carriage return (no
    auto-submit).  The ``claude -p`` (``ClaudeProvider``) path is
    completely unaffected.

Plus a no-scraper STRUCTURAL assertion: the human-attended spawn path
contains no completion-sentinel watcher / no parse-TTY-to-advance-state
helper — grep the source code for the forbidden constructs and fail if
they reappear.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coord.agent import AgentServer, AssignmentSpec, WRITE_CAPABLE_SPEC_TYPES
from coord.config import (
    Config,
    DispatchConfig,
    ModelsConfig,
    PipelineConfig,
    ProviderDef,
    ProvidersConfig,
    ReviewsConfig,
)
from coord.providers import (
    ClaudeProvider,
    ClaudePtyProvider,
    build_provider,
    guard_unattended_dispatch,
    resolve_provider_name,
)
from coord.providers.base import Capabilities


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_spec(**kwargs) -> AssignmentSpec:
    defaults: dict = {
        "repo_name": "myrepo",
        "repo_path": "/some/path",
        "issue_number": 7,
        "issue_title": "Do the thing",
        "briefing": "Hello.",
    }
    defaults.update(kwargs)
    return AssignmentSpec(**defaults)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    (path / "README").write_text("init\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README"], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Delta (1) — capability flag
# ─────────────────────────────────────────────────────────────────────────────


def test_capability_default_is_false() -> None:
    """A new Capabilities() defaults human_attended_only to False (fail-safe)."""
    caps = Capabilities(
        resume=False,
        inject=False,
        cost_reporting=False,
        true_system_prompt=False,
        enforces_deny_list=False,
        billing_mode="unknown",
    )
    assert caps.human_attended_only is False


def test_claude_provider_is_not_human_attended_only() -> None:
    """ClaudeProvider (`claude -p`) is the unattended/headless path."""
    caps = ClaudeProvider().capabilities()
    assert caps.human_attended_only is False


def test_claude_pty_provider_is_human_attended_only() -> None:
    """ClaudePtyProvider opts out of unattended routing — the structural
    guarantee that makes the subscription path ToS-compliant."""
    caps = ClaudePtyProvider().capabilities()
    assert caps.human_attended_only is True


# ─────────────────────────────────────────────────────────────────────────────
# Delta (2) — structural guardrail (gate + precedence)
# ─────────────────────────────────────────────────────────────────────────────


def test_guard_accepts_automatable_provider() -> None:
    """A provider that does NOT opt out is accepted on the unattended path."""
    cfg = ProvidersConfig()  # implicit `claude` definition
    name = guard_unattended_dispatch(
        spec_provider=None,
        repo_provider=None,
        providers_cfg=cfg,
        models_cfg=None,
    )
    assert name == "claude"


def test_guard_refuses_human_attended_provider_at_default_level() -> None:
    """If providers.default points at a human-attended provider, refuse."""
    cfg = ProvidersConfig(
        default="subscription-claude",
        definitions={"subscription-claude": ProviderDef(type="claude-pty")},
    )
    with pytest.raises(ValueError, match="human_attended_only=True"):
        guard_unattended_dispatch(
            spec_provider=None,
            repo_provider=None,
            providers_cfg=cfg,
            models_cfg=None,
        )


def test_guard_refuses_human_attended_provider_at_repo_level() -> None:
    """A per-repo provider override pointing at human-attended → refuse."""
    cfg = ProvidersConfig(
        default="claude",
        definitions={
            "claude": ProviderDef(type="claude"),
            "subscription-claude": ProviderDef(type="claude-pty"),
        },
    )
    with pytest.raises(ValueError, match="human_attended_only=True"):
        guard_unattended_dispatch(
            spec_provider=None,
            repo_provider="subscription-claude",
            providers_cfg=cfg,
            models_cfg=None,
        )


def test_guard_refuses_human_attended_provider_at_spec_level() -> None:
    """A per-spec/per-proposal provider naming a human-attended → refuse."""
    cfg = ProvidersConfig(
        default="claude",
        definitions={
            "claude": ProviderDef(type="claude"),
            "subscription-claude": ProviderDef(type="claude-pty"),
        },
    )
    with pytest.raises(ValueError, match="human_attended_only=True"):
        guard_unattended_dispatch(
            spec_provider="subscription-claude",
            repo_provider=None,
            providers_cfg=cfg,
            models_cfg=None,
        )


def test_guard_message_names_the_provider_and_redirects_to_interactive() -> None:
    """The error message must name the provider and point at --interactive."""
    cfg = ProvidersConfig(
        default="subscription-claude",
        definitions={"subscription-claude": ProviderDef(type="claude-pty")},
    )
    with pytest.raises(ValueError) as excinfo:
        guard_unattended_dispatch(
            spec_provider=None,
            repo_provider=None,
            providers_cfg=cfg,
            models_cfg=None,
        )
    msg = str(excinfo.value)
    assert "subscription-claude" in msg
    assert "--interactive" in msg


def test_guard_unknown_provider_name_falls_through() -> None:
    """Unknown provider names don't fabricate a refusal — agent-side validation
    surfaces the typo on dispatch."""
    cfg = ProvidersConfig()
    name = guard_unattended_dispatch(
        spec_provider="typo-here",
        repo_provider=None,
        providers_cfg=cfg,
        models_cfg=None,
    )
    assert name == "typo-here"


# ── Dispatch call-site gating ────────────────────────────────────────────────


@dataclass
class _StubRepo:
    name: str
    github: str
    default_branch: str = "main"
    worker_permissions: object | None = None
    coordinator_only_files: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    new_issue_guidance: str | None = None
    provider: str | None = None


@dataclass
class _StubMachine:
    name: str
    host: str = "localhost"
    repos: list[str] = field(default_factory=list)
    _paths: dict[str, str] = field(default_factory=dict)
    # #1417: _reassign's capacity check reads Machine.max_workers — keep
    # this stub in sync with the real dataclass's optional override.
    max_workers: int | None = None
    # #1862: paused_set() reads Machine.quiet_hours — keep this stub in
    # sync with the real dataclass's optional field too.
    quiet_hours: "object | None" = None

    def repo_path(self, name: str) -> str | None:
        return self._paths.get(name)

    def can_work_on(self, name: str) -> bool:
        return name in self.repos


def _make_config_with_default_provider(default_provider: str) -> Config:
    """Build a Config whose providers.default is `default_provider`."""
    definitions = {
        "claude": ProviderDef(type="claude"),
        "subscription-claude": ProviderDef(type="claude-pty"),
    }
    return Config(
        repos=[_StubRepo(name="myrepo", github="org/myrepo")],
        machines=[
            _StubMachine(
                name="m1",
                repos=["myrepo"],
                _paths={"myrepo": "/tmp/myrepo"},
            )
        ],
        providers=ProvidersConfig(
            default=default_provider, definitions=definitions
        ),
    )


def test_dispatch_refuses_human_attended_default() -> None:
    """coord.dispatch.dispatch refuses when providers.default is human-attended."""
    from coord.dispatch import dispatch
    from coord.models import Proposal

    cfg = _make_config_with_default_provider("subscription-claude")
    proposal = Proposal(
        id=1,
        machine_name="m1",
        repo_name="myrepo",
        issue_number=42,
        issue_title="Test",
        rationale="t",
        briefing="b",
    )
    with pytest.raises(ValueError, match="human_attended_only=True"):
        dispatch(proposal, cfg)


def test_dispatch_refuses_human_attended_repo_override() -> None:
    """coord.dispatch.dispatch refuses when Repo.provider is human-attended."""
    from coord.dispatch import dispatch
    from coord.models import Proposal

    cfg = _make_config_with_default_provider("claude")
    cfg.repos[0].provider = "subscription-claude"
    proposal = Proposal(
        id=1,
        machine_name="m1",
        repo_name="myrepo",
        issue_number=42,
        issue_title="Test",
        rationale="t",
        briefing="b",
    )
    with pytest.raises(ValueError, match="human_attended_only=True"):
        dispatch(proposal, cfg)


def test_dispatch_review_refuses_human_attended(monkeypatch, capsys) -> None:
    """dispatch_review returns None (and prints a warning) on a human-attended repo.

    The function must NOT raise ValueError — callers (notify.py, reconcile.py)
    only check for a None return value and would crash if a ValueError escaped.
    Returning None leaves review_state as 'pending' so the next notify call
    retries, consistent with how _reassign handles the same guard.
    """
    from coord.review import dispatch_review
    from coord.models import Assignment, Board

    cfg = _make_config_with_default_provider("claude")
    cfg.repos[0].provider = "subscription-claude"
    cfg.reviews = ReviewsConfig(enabled=True, auto_dispatch=True)

    board = Board()
    completed = Assignment(
        machine_name="m1",
        repo_name="myrepo",
        issue_number=42,
        issue_title="Test",
        files_allowed=[],
        files_forbidden=[],
        briefing="b",
        assignment_id="abc",
        status="done",
        branch="issue-42-test",
        type="work",
    )
    result = dispatch_review(completed, board, cfg)
    assert result is None, "dispatch_review must return None for human-attended providers"
    captured = capsys.readouterr()
    assert "human_attended_only=True" in captured.out, (
        "Expected a warning mentioning human_attended_only=True in stdout"
    )


def test_dispatch_review_refuses_human_attended_via_reviews_provider(
    monkeypatch, capsys,
) -> None:
    """#1811 regression: the #437 TOS-compliance gate must still apply when
    the human-attended provider comes from ``reviews.provider`` rather than
    ``repo.provider`` — routing the new field through
    ``guard_unattended_dispatch``'s ``spec_provider`` (which already
    outranks ``repo_provider``) must not create a way to smuggle a
    human-attended provider past the gate that #437 established. The repo's
    OWN provider here is the ordinary, unattended "claude" — only
    ``reviews.provider`` names the human-attended one — so a refusal here
    can only be explained by the new field actually being checked.
    """
    from coord.review import dispatch_review
    from coord.models import Assignment, Board

    cfg = _make_config_with_default_provider("claude")
    # repo.provider left unset (implicit "claude", not human-attended).
    cfg.reviews = ReviewsConfig(
        enabled=True, auto_dispatch=True, provider="subscription-claude",
    )

    board = Board()
    completed = Assignment(
        machine_name="m1",
        repo_name="myrepo",
        issue_number=42,
        issue_title="Test",
        files_allowed=[],
        files_forbidden=[],
        briefing="b",
        assignment_id="abc",
        status="done",
        branch="issue-42-test",
        type="work",
    )
    result = dispatch_review(completed, board, cfg)
    assert result is None, (
        "dispatch_review must refuse a human-attended reviews.provider"
    )
    captured = capsys.readouterr()
    assert "human_attended_only=True" in captured.out, (
        "Expected a warning mentioning human_attended_only=True in stdout"
    )


def test_reassign_refuses_human_attended() -> None:
    """coord.reconcile._reassign returns None when the provider is human-attended."""
    from coord.reconcile import _reassign
    from coord.models import Assignment, Board

    cfg = _make_config_with_default_provider("claude")
    cfg.repos[0].provider = "subscription-claude"
    board = Board()
    failed = Assignment(
        machine_name="m1",
        repo_name="myrepo",
        issue_number=42,
        issue_title="Test",
        files_allowed=[],
        files_forbidden=[],
        briefing="b",
        assignment_id="abc",
        status="failed",
        type="work",
    )
    # _reassign catches the ValueError and returns None — the failed
    # assignment is left alone for human attention rather than silently
    # retried on the wrong provider.
    result = _reassign(failed, board, cfg)
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Delta (3) — pre-fill but NO auto-submit
# ─────────────────────────────────────────────────────────────────────────────


def test_spawn_pty_does_not_write_carriage_return(tmp_path, monkeypatch) -> None:
    """The agent's PTY path writes the bracketed-paste pre-fill ONLY — never
    a trailing carriage return.  This is the structural ToS change for #437.

    We monkey-patch ``os.write`` while the spawn runs and record every byte
    string written to the master fd.  The assertion: the paste markers and
    body appear; a bare ``b"\\r"`` write never does.
    """
    from coord.providers.claude_pty import (
        BRACKETED_PASTE_END,
        BRACKETED_PASTE_START,
    )

    repo = _init_repo(tmp_path / "repo")
    captured_writes: list[bytes] = []

    real_write = os.write

    def recording_write(fd: int, data: bytes) -> int:
        captured_writes.append(data)
        return real_write(fd, data)

    monkeypatch.setattr("coord.agent.os.write", recording_write)

    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["myrepo"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo unused"],
        repo_paths={"myrepo": str(repo)},
        providers={"claude-pty": _EnableThenSleepProvider()},
    )
    spec = _make_spec(
        repo_name="myrepo",
        repo_path=str(repo),
        type="plan",
        provider="claude-pty",
        briefing="HELLO",
    )
    record = server.assign(spec)
    server.wait_for(record.id, timeout=15.0)
    server.shutdown()

    # The bracketed-paste-wrapped briefing must have been written.
    pre_fill = BRACKETED_PASTE_START + b"HELLO" + BRACKETED_PASTE_END
    assert any(pre_fill in w for w in captured_writes), (
        f"pre-fill block was not written to the PTY master: {captured_writes!r}"
    )
    # And the bare carriage return MUST NOT appear as a discrete write.
    # The previous (auto-submit) behaviour was os.write(master_fd, b"\r")
    # as a separate write following the paste.  After #437 that write is
    # gone — no other code path in the agent's PTY spawn writes a bare
    # b"\r" to the master fd.
    assert b"\r" not in captured_writes, (
        f"agent wrote a bare \\r to the PTY master (auto-submit regression!): "
        f"{captured_writes!r}"
    )


class _EnableThenSleepProvider(ClaudePtyProvider):
    """Mock claude binary that emits the bracketed-paste-enable DECSET, then
    sleeps long enough for the spawn path to pre-fill, then exits.  No
    interactive read; we only care about what the AGENT wrote to the
    master fd."""

    def build_command(
        self,
        spec: AssignmentSpec,
        *,
        resolved_model=None,
        system_prompt=None,
        allowed_tools=None,
        permission_mode="acceptEdits",
    ) -> list[str]:
        return [
            "/bin/sh",
            "-c",
            "printf '\\033[?2004h'; echo READY; sleep 2",
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Delta (3) — claude -p (ClaudeProvider) path is COMPLETELY unaffected
# ─────────────────────────────────────────────────────────────────────────────


def test_claude_p_path_unchanged(tmp_path) -> None:
    """Spawning a worker via the default (legacy) `claude -p` path does NOT
    touch the PTY branch, does NOT write any bracketed-paste markers, and
    the worker process is fed via stdin pipe as before."""
    from coord.providers.claude_pty import (
        BRACKETED_PASTE_END,
        BRACKETED_PASTE_START,
    )

    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["myrepo"],
        state_dir=tmp_path / "state",
        # A "stream-json" mock: read one line of JSON from stdin (the
        # briefing), echo a fake result event, exit.  The legacy path
        # writes the briefing via stdin pipe — NOT via a PTY master.
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            "read line; echo \"got=$line\"; echo '{\"type\":\"result\"}'",
        ],
        repo_paths={"myrepo": str(repo)},
        # No providers dict at all — back-compat (no-provider) mode.
    )
    spec = _make_spec(
        repo_name="myrepo",
        repo_path=str(repo),
        type="plan",
        provider=None,  # legacy default path
        briefing="LEGACY_BRIEFING",
    )
    record = server.assign(spec)
    final = server.wait_for(record.id, timeout=15.0)
    # The content here is asserted on (LEGACY_BRIEFING membership, the
    # bracketed-paste byte check below), so this reads strict UTF-8 — the
    # same encoding coord/agent.py's _append_log_line writes with — rather
    # than errors="replace", which is reserved for diagnostic-only reads.
    log_text = Path(final.log_path).read_text(encoding="utf-8")
    # The legacy path used a stdin pipe + stream-json — NO bracketed paste.
    assert BRACKETED_PASTE_START not in log_text.encode("utf-8")
    assert BRACKETED_PASTE_END not in log_text.encode("utf-8")
    # The briefing reached the worker via stdin pipe (proves the legacy
    # path still works).
    assert "LEGACY_BRIEFING" in log_text
    # And the spawn header is the legacy one — no provider= token.
    assert "provider=" not in log_text.splitlines()[0]
    server.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# No-scraper STRUCTURAL assertion
# ─────────────────────────────────────────────────────────────────────────────


_FORBIDDEN_PATTERNS = (
    # Auto-submit writes — the literal sequence we removed in delta (3).
    re.compile(rb"os\.write\(\s*master_fd\s*,\s*b['\"]\\r['\"]"),
    # Settle-then-submit timer — the support code for the removed write.
    re.compile(rb"_PTY_SUBMIT_SETTLE_S\s*\)\s*\n\s*os\.write\("),
)


def test_no_auto_submit_or_scraper_in_human_attended_path() -> None:
    """Grep the agent's PTY spawn path for the forbidden auto-submit /
    scraper constructs that #426 was closed for.

    If a future change reintroduces a coordinator-side carriage-return
    inject after the pre-fill, OR a TTY-content scraper that watches the
    session log to advance pipeline state, this test fails — the
    reviewer's mechanical check.
    """
    agent_src = Path("coord/agent.py").read_bytes()
    for pattern in _FORBIDDEN_PATTERNS:
        m = pattern.search(agent_src)
        assert m is None, (
            f"Forbidden auto-submit / TTY-scraper pattern reintroduced in "
            f"coord/agent.py: {pattern.pattern!r} matched {m.group(0)!r}.  "
            "The human-attended path must PRE-FILL only; the operator "
            "presses Enter themselves.  See #437."
        )


def test_interactive_launcher_has_no_completion_sentinel_watcher() -> None:
    """coord/interactive.py must not scrape the LIVE PTY/TTY to advance state
    (ToS §3.7 / #437): no content-based completion-sentinel watcher, no result-
    marker or progress-signal grep on the live output stream.

    Carve-out (#606 transcript-floor): post-exit recovery is allowed and is NOT
    live scraping.  After the HUMAN exits, `finalize_interactive_exit` may
    recover a review verdict from Claude's *persisted* session transcript (a
    `.jsonl` file) via the file-based `coord.review.parse_review_from_log` — it
    reads a file, makes no automated access to Claude, and runs only on exit
    (structurally the same as the git-floor reading commits).  So the guard bans
    the live-stream markers but permits the file-based verdict parse.
    """
    interactive_src = Path("coord/interactive.py").read_text(encoding="utf-8")
    # Sanity: the module exists and exports the launcher.
    assert "launch_human_attended_interactive" in interactive_src
    # Markers that only make sense if something watches the LIVE output stream to
    # decide the session is "done" / advance state — still forbidden.  (Note:
    # REVIEW_VERDICT is intentionally NOT here — the post-exit transcript-floor
    # legitimately parses it from a persisted file; see the carve-out check.)
    forbidden = [
        "result_marker",
        "PTY_RESULT_MARKER",
        "STATUS:",
        "STUCK:",
        "SMOKE_TESTS",
    ]
    for token in forbidden:
        assert token not in interactive_src, (
            f"coord/interactive.py references {token!r} — the human-attended "
            "launcher must NOT scrape the live TTY to advance state (#437)."
        )
    # Carve-out enforcement: any review-verdict recovery MUST be the post-exit,
    # FILE-based transcript-floor (Claude's persisted .jsonl via
    # parse_review_from_log), never a live-pane scrape.  `capture-pane` stays
    # permitted ONLY for the readiness poll, not to extract a verdict.
    if "REVIEW_VERDICT" in interactive_src:
        assert "parse_review_from_log" in interactive_src, (
            "review-verdict recovery in coord/interactive.py must go through the "
            "file-based parse_review_from_log (post-exit transcript-floor), not a "
            "live-TTY scrape (#437 / #606)."
        )
