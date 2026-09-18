"""#3371 Part A: the long-lived, subscription-backed `claude setup-token`
credential — resolution, injection into the headless worker environment, and
what `coord doctor`/`/health` report about it.

The constraint these tests exist to hold is the issue's headline one: the
fleet's credential stays a **Claude subscription** OAuth token
(`CLAUDE_CODE_OAUTH_TOKEN`, what `claude setup-token` mints) and never
becomes an `ANTHROPIC_API_KEY` — #2462 measured what that costs when it
happens by accident (every dispatch on every machine failing within the
hour).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coord import agent, claude_setup_token, prereqs
from coord.claude_setup_token import (
    CLAUDE_OAUTH_TOKEN_ENV,
    inject_setup_token,
    load_setup_token,
    setup_token_path,
)

REAL_TOKEN = "sk-ant-oat01-EXAMPLE-not-a-real-token"
API_KEY = "sk-ant-api03-EXAMPLE-not-a-real-key"


@pytest.fixture(autouse=True)
def _isolated_coord_dir(tmp_path, monkeypatch):
    """Point `~/.coord` at a private sandbox and clear any ambient token.

    Without this, a run on a real agent host (which may have adopted a
    token) would answer differently from a run on a laptop that hasn't.
    """
    coord_dir = tmp_path / "coord-home"
    coord_dir.mkdir()
    monkeypatch.setenv("COORD_DIR", str(coord_dir))
    monkeypatch.delenv(CLAUDE_OAUTH_TOKEN_ENV, raising=False)
    return coord_dir


def _write_token(value: str, *, mode: int = 0o600) -> Path:
    path = setup_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, mode)
    return path


class TestResolution:
    def test_no_token_configured_is_absent(self) -> None:
        status = load_setup_token({})
        assert status.present is False
        assert status.usable is False
        assert status.token is None
        assert "no long-lived" in status.describe()

    def test_token_file_is_picked_up(self) -> None:
        path = _write_token(f"  {REAL_TOKEN}\n")
        status = load_setup_token({})
        assert status.source == "file"
        assert status.token == REAL_TOKEN  # surrounding whitespace stripped
        assert status.usable is True
        assert status.path == path

    def test_empty_token_file_is_absent_not_a_broken_credential(self) -> None:
        """A half-written/truncated file must read as "not adopted", so the
        host keeps authenticating with its interactive session instead of
        being reported broken."""
        _write_token("   \n")
        assert load_setup_token({}).present is False

    def test_environment_token_wins_over_the_file(self) -> None:
        """`claude` itself prefers the environment; dispatch must too, and
        the probe must report on the credential dispatch will use."""
        _write_token(REAL_TOKEN)
        env_token = "sk-ant-oat01-FROM-ENVIRONMENT"
        status = load_setup_token({CLAUDE_OAUTH_TOKEN_ENV: env_token})
        assert status.source == "env"
        assert status.token == env_token

    def test_api_key_is_refused_not_adopted(self) -> None:
        """#3371's headline constraint, mechanically enforced."""
        _write_token(API_KEY)
        status = load_setup_token({})
        assert status.present is True
        assert status.usable is False
        assert "API key" in (status.problem or "")
        assert "subscription" in (status.problem or "")

    def test_unfamiliar_prefix_is_a_note_not_a_refusal(self) -> None:
        """Fail-closed on shape would let a renamed token prefix silently
        drop every adopting host out of the routing pool — worse than the
        failure it would prevent."""
        _write_token("whatever-anthropic-renames-this-to")
        status = load_setup_token({})
        assert status.usable is True
        assert "setup-token" in (status.note or "")

    def test_loose_file_permissions_are_flagged(self) -> None:
        _write_token(REAL_TOKEN, mode=0o644)
        status = load_setup_token({})
        assert status.usable is True
        assert "chmod 600" in (status.note or "")


class TestTheTokenIsNeverLogged:
    def test_repr_and_describe_redact_the_secret(self) -> None:
        _write_token(REAL_TOKEN)
        status = load_setup_token({})
        assert REAL_TOKEN not in repr(status)
        assert "<redacted>" in repr(status)
        assert REAL_TOKEN not in status.describe()

    def test_probe_payload_never_carries_the_secret(self, monkeypatch) -> None:
        """`/health`'s `tool_versions` is fetched by every thin client and
        rendered by `coord doctor` — the token must not ride along."""
        _write_token(REAL_TOKEN)
        monkeypatch.setattr(prereqs.shutil, "which", lambda _b: "/usr/bin/claude")
        probe = prereqs.probe(_claude_prereq())
        assert REAL_TOKEN not in str(probe.to_dict())
        assert REAL_TOKEN not in probe.version
        assert REAL_TOKEN not in probe.what_breaks


class TestWorkerEnvironment:
    """`coord.agent._worker_subprocess_env` is the ONE place a headless
    `claude -p` leg's environment is built, so it is the one place the
    credential has to be threaded through."""

    def _worker_env(self, base: dict[str, str]) -> dict[str, str]:
        return agent._worker_subprocess_env(base, prefix="/x", base_prefix="/x")

    def test_unadopted_host_env_is_untouched(self) -> None:
        """The #2462 safety property: a host that has not opted in must
        produce a byte-for-byte identical worker environment."""
        base = {"PATH": "/usr/bin", "HOME": "/home/john"}
        env = self._worker_env(dict(base))
        assert CLAUDE_OAUTH_TOKEN_ENV not in env

    def test_adopted_host_injects_the_long_lived_token(self) -> None:
        _write_token(REAL_TOKEN)
        env = self._worker_env({"PATH": "/usr/bin"})
        assert env[CLAUDE_OAUTH_TOKEN_ENV] == REAL_TOKEN

    def test_inherited_token_is_never_overwritten(self) -> None:
        _write_token(REAL_TOKEN)
        inherited = "sk-ant-oat01-SET-BY-THE-OPERATOR"
        env = self._worker_env(
            {"PATH": "/usr/bin", CLAUDE_OAUTH_TOKEN_ENV: inherited}
        )
        assert env[CLAUDE_OAUTH_TOKEN_ENV] == inherited

    def test_an_api_key_is_never_injected(self) -> None:
        """A worker must not be handed API-key auth even if an operator
        drops one into the token file by mistake."""
        _write_token(API_KEY)
        env = self._worker_env({"PATH": "/usr/bin"})
        assert CLAUDE_OAUTH_TOKEN_ENV not in env
        assert "ANTHROPIC_API_KEY" not in env

    def test_inject_reports_which_source_it_used(self) -> None:
        env: dict[str, str] = {}
        assert inject_setup_token(env) is None
        _write_token(REAL_TOKEN)
        assert inject_setup_token(env) == "file"
        assert inject_setup_token(env) == "env"  # now inherited from itself


def _claude_prereq():
    return next(p for p in prereqs.BASELINE_PREREQS if p.tool == "claude")


class TestCredentialProbe:
    """What `coord doctor` / `/health` say about a setup-token host — and
    therefore whether #3371 Part B's routing gate keeps it in the pool."""

    def test_setup_token_host_probes_healthy_without_a_login_session(
        self, tmp_path, monkeypatch
    ) -> None:
        """The whole point of Part A: a host with NO interactive
        `~/.claude/.credentials.json` at all is fully routable once it has
        a long-lived token, because that is what its workers will
        authenticate with."""
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        _write_token(REAL_TOKEN)
        monkeypatch.setattr(prereqs.shutil, "which", lambda _b: "/usr/bin/claude")
        probe = prereqs.probe(_claude_prereq())
        assert probe.found is True
        assert probe.ok is True
        assert "setup-token" in (probe.version or "")
        # No expiry is knowable for a minted token — `None` must mean "no
        # expiry known", never a fabricated "does not expire".
        assert probe.expires_at is None
        assert prereqs.claude_credential_ok({"claude": probe.to_dict()}) is True

    def test_environment_sourced_token_is_named_as_such(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        monkeypatch.setenv(CLAUDE_OAUTH_TOKEN_ENV, REAL_TOKEN)
        monkeypatch.setattr(prereqs.shutil, "which", lambda _b: "/usr/bin/claude")
        probe = prereqs.probe(_claude_prereq())
        assert probe.ok is True
        assert CLAUDE_OAUTH_TOKEN_ENV in (probe.version or "")

    def test_api_key_token_takes_the_host_out_of_the_pool(
        self, tmp_path, monkeypatch
    ) -> None:
        """An API-key-shaped credential is not merely ignored — the host
        reports UNMET, so #3371 Part B's credential-health gate refuses to
        route it rather than letting it fail at turn 1 for $0."""
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        _write_token(API_KEY)
        monkeypatch.setattr(prereqs.shutil, "which", lambda _b: "/usr/bin/claude")
        probe = prereqs.probe(_claude_prereq())
        assert probe.found is False
        assert probe.ok is False
        assert "API key" in probe.what_breaks
        assert prereqs.claude_credential_ok({"claude": probe.to_dict()}) is False

    def test_unadopted_host_still_probes_its_login_session(
        self, tmp_path, monkeypatch
    ) -> None:
        """No token file → the #3326 interactive-session probe is reached
        unchanged (regression guard for the new early-return)."""
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        monkeypatch.setattr(prereqs.shutil, "which", lambda _b: "/usr/bin/claude")
        monkeypatch.setattr(prereqs.sys, "platform", "linux")
        probe = prereqs.probe(_claude_prereq())
        assert probe.found is False
        assert "does not exist" in probe.what_breaks
        # ...and it now teaches BOTH remedies, not just the interactive one.
        assert "claude setup-token" in probe.what_breaks

    def test_probe_ranks_sources_the_same_way_dispatch_does(
        self, tmp_path, monkeypatch
    ) -> None:
        """#2096 "one question, one answer": the probe must not report on a
        different credential from the one `_worker_subprocess_env` will hand
        the worker."""
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        env_token = "sk-ant-oat01-FROM-ENVIRONMENT"
        monkeypatch.setenv(CLAUDE_OAUTH_TOKEN_ENV, env_token)
        _write_token(REAL_TOKEN)
        monkeypatch.setattr(prereqs.shutil, "which", lambda _b: "/usr/bin/claude")
        probe = prereqs.probe(_claude_prereq())
        worker_env = agent._worker_subprocess_env(
            {"PATH": "/usr/bin", CLAUDE_OAUTH_TOKEN_ENV: env_token},
            prefix="/x", base_prefix="/x",
        )
        assert worker_env[CLAUDE_OAUTH_TOKEN_ENV] == env_token
        assert CLAUDE_OAUTH_TOKEN_ENV in (probe.version or "")
        assert load_setup_token().source == "env"


class TestApiKeyStaysUnsupported:
    def test_module_documents_the_constraint(self) -> None:
        """#2462's lesson must stay next to the code that could re-litigate
        it — a future reader reaching for `ANTHROPIC_API_KEY` has to trip
        over the reason not to."""
        doc = claude_setup_token.__doc__ or ""
        assert "ANTHROPIC_API_KEY" in doc
        assert "#2462" in doc

    def test_nothing_in_the_injection_path_sets_an_api_key_variable(self) -> None:
        _write_token(REAL_TOKEN)
        env: dict[str, str] = {}
        inject_setup_token(env)
        assert set(env) == {CLAUDE_OAUTH_TOKEN_ENV}
