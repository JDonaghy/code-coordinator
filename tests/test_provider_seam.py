"""#1710: assert coordinator-side log consumers actually route through
``provider.parse_log()`` instead of assuming every worker log is claude's
stream-json shape.

Before this issue, ``coord.progress``, ``coord.usage``, and
``coord.failure_class`` imported :mod:`coord.worker_events` directly and
gated on ``is_stream_json()`` — so a second, non-claude provider's log would
either silently parse as empty (no error, no signal) or, worse, silently
misparse if it also happened to be JSON-per-line but shaped differently.
``coord.review``'s three ``coord.worker_events`` call sites are a separate,
justified case (see the PR description's inventory table): they decode the
generic Anthropic-Messages-API ``type: "assistant"`` / ``message.content``
envelope, not claude *business* semantics, so they already degrade
correctly for any provider that reuses that envelope for turn text — which
is exactly what :class:`FakeAgentProvider` below does, on purpose, so this
file can assert review extraction "just works" for a second provider too.

:class:`FakeAgentProvider` is a from-scratch, non-claude, non-opencode
:class:`~coord.providers.base.Provider`. Its log is NDJSON that reuses
claude's ``type: "assistant"`` turn envelope (so the review/plan/smoke
*text*-decode helpers, which only assume that generic shape, keep working
unchanged) but reports cost/tokens/errors through a **custom** terminal
event (``type: "fake_result"``) that :func:`coord.worker_events.parse_log`
does not recognise at all. This is the crux of the regression this file
guards: routing progress/cost/failure-classification through
``coord.worker_events.parse_log()`` directly (the pre-#1710 bug) silently
yields zero cost and "no error" for this provider's logs; routing through
``FakeAgentProvider().parse_log()`` (the fix) yields the real numbers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from coord.config import Config, ProviderDef, ProvidersConfig
from coord.failure_class import ENVIRONMENTAL, WORK, classify_log
from coord.progress import parse_progress
from coord.providers import get_provider
from coord.providers.base import Capabilities, Provider, WorkerSummary
from coord.providers.claude import ClaudeProvider
from coord.providers.opencode import OpenCodeProvider
from coord.review import parse_review_from_log
from coord.usage import parse_usage_from_log


#: A real, verbatim opencode capture (#1703/#1704) — same fixture
#: ``tests/test_providers.py`` pins ``OpenCodeProvider.parse_log()``'s exact
#: field values against. Reused here (#2305) as the "vehicle" log for
#: proving ``cfg``-based resolution of a **tier-named** provider definition
#: (e.g. ``oc-mid``) actually reaches ``OpenCodeProvider.parse_log()`` at
#: each of the four #2305 call sites, without depending on a real opencode
#: binary — the fixture is a static capture, not a live run.
_OPENCODE_FIXTURE = Path(__file__).parent / "fixtures" / "opencode_run_sample.jsonl"


def _cfg_with_tier_provider(name: str, ptype: str) -> Config:
    """A minimal :class:`~coord.config.Config` whose ``providers.definitions``
    carries one entry keyed by a **tier name** (e.g. ``"oc-mid"``) rather
    than a built-in provider *type* name — the #2305 shape: the name alone
    doesn't match ``_BUILTIN_PROVIDER_TYPES``, so resolution only succeeds
    when the caller actually threads ``cfg`` through to
    :func:`coord.providers.get_provider`.
    """
    return Config(
        repos=[],
        machines=[],
        providers=ProvidersConfig(definitions={name: ProviderDef(type=ptype)}),
    )


# ── The fake provider ───────────────────────────────────────────────────────


class FakeAgentProvider(Provider):
    """Minimal non-claude :class:`Provider` used only by this test module.

    Deliberately NOT registered in ``coord.providers.build_provider`` / the
    ``_BUILTIN_PROVIDER_TYPES`` registry — tests construct it directly and
    pass it via the ``provider=`` escape hatch each migrated consumer added
    for #1710, so no production wiring is touched.
    """

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resume=False,
            inject=False,
            cost_reporting=True,
            true_system_prompt=False,
            enforces_deny_list=False,
            billing_mode="metered",
        )

    def build_command(  # noqa: D102 — trivial stub, not exercised here
        self,
        spec,
        *,
        resolved_model=None,
        system_prompt=None,
        allowed_tools=None,
        permission_mode="acceptEdits",
    ) -> list[str]:
        return ["fake-agent", "run"]

    def initial_input(self, spec) -> bytes:  # noqa: D102
        return b""

    def result_marker(self) -> str:
        return '"type":"fake_result"'

    def env(self) -> dict[str, str]:
        return {}

    def oneshot_command(self, *, system_prompt: str, output_format=None) -> list[str]:
        return ["fake-agent", "run", "-p", system_prompt]

    def parse_log(
        self, log_path: str | Path, tail_bytes: int = 65536
    ) -> WorkerSummary:
        """Parse this provider's own NDJSON shape.

        Recognises ``type: "assistant"`` (shared envelope, counts a turn)
        and ``type: "fake_result"`` (custom terminal event — cost, tokens,
        error state). Anything else (including claude's own ``"result"``
        events, if such a log were ever handed to this provider by
        mistake) is silently ignored, same permissive contract every other
        concrete ``Provider.parse_log()`` documents.
        """
        summary = WorkerSummary()
        p = Path(log_path)
        if not p.exists():
            return summary
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return summary
        if tail_bytes and len(text) > tail_bytes:
            text = text[-tail_bytes:]
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            etype = obj.get("type")
            if etype == "assistant":
                summary.num_turns += 1
                summary.last_tool = "assistant"
            elif etype == "fake_result":
                summary.is_error = bool(obj.get("is_error"))
                status = obj.get("status")
                summary.api_error_status = status if isinstance(status, int) else None
                reason = obj.get("reason")
                summary.result_text = reason if isinstance(reason, str) else None
                cost = obj.get("cost_usd")
                if isinstance(cost, (int, float)):
                    summary.total_cost_usd = float(cost)
                tin = obj.get("tokens_in")
                if isinstance(tin, int):
                    summary.input_tokens = tin
                tout = obj.get("tokens_out")
                if isinstance(tout, int):
                    summary.output_tokens = tout
        return summary


def _assistant_line(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _fake_result_line(**kwargs) -> str:
    return json.dumps({"type": "fake_result", **kwargs})


def _write_fake_log(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


# ── get_provider ─────────────────────────────────────────────────────────────


class TestGetProvider:
    def test_none_defaults_to_claude(self) -> None:
        assert isinstance(get_provider(None), ClaudeProvider)

    def test_builtin_names_resolve_without_cfg(self) -> None:
        from coord.providers.claude_pty import ClaudePtyProvider
        from coord.providers.opencode import OpenCodeProvider

        assert isinstance(get_provider("claude"), ClaudeProvider)
        assert isinstance(get_provider("claude-pty"), ClaudePtyProvider)
        assert isinstance(get_provider("opencode"), OpenCodeProvider)

    def test_unknown_name_warns_and_falls_back_to_claude(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="coord.providers"):
            provider = get_provider("totally-unknown-provider")
        assert isinstance(provider, ClaudeProvider)
        assert any(
            "totally-unknown-provider" in r.message for r in caplog.records
        ), "unknown provider_name must produce a LOUD warning, not a silent fallback (#1710)"

    def test_tier_named_definition_degrades_to_claude_without_cfg(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#2305: this is the bug itself, pinned directly at the seam —
        ``oc-mid`` is a real fleet tier name (see the issue), not a typo.
        Without ``cfg`` there is no ``providers.definitions`` to consult, so
        it degrades to :class:`ClaudeProvider`, loudly."""
        with caplog.at_level(logging.WARNING, logger="coord.providers"):
            provider = get_provider("oc-mid")
        assert isinstance(provider, ClaudeProvider)
        assert any("oc-mid" in r.message for r in caplog.records)

    def test_tier_named_definition_resolves_via_cfg(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#2305 fix: with ``cfg`` threaded through, the same tier name
        resolves against ``cfg.providers.definitions`` — the *type* backing
        it (``opencode``), not :class:`ClaudeProvider`, and with no warning."""
        cfg = _cfg_with_tier_provider("oc-mid", "opencode")
        with caplog.at_level(logging.WARNING, logger="coord.providers"):
            provider = get_provider("oc-mid", cfg=cfg)
        assert isinstance(provider, OpenCodeProvider)
        assert not caplog.records


# ── progress.py ──────────────────────────────────────────────────────────────


class TestProgressSeam:
    def test_fake_provider_progress_is_correct(self, tmp_path: Path) -> None:
        log = _write_fake_log(
            tmp_path / "fake.log",
            [
                _assistant_line("STATUS: starting → doing the thing → confidence: high"),
                _assistant_line("STATUS: still going → confidence: high"),
                _fake_result_line(is_error=False, cost_usd=0.05, tokens_in=10, tokens_out=5),
            ],
        )
        progress = parse_progress(log, provider=FakeAgentProvider())
        assert progress.updates == ["Turn 2: assistant"]

    def test_claude_worker_events_directly_misses_fake_cost_signal(
        self, tmp_path: Path
    ) -> None:
        """Direct proof of the pre-#1710 bug: calling the claude-shaped
        parser straight (bypassing the provider seam) on a real (fake, in
        this test) second provider's log silently returns no cost/error
        signal — the exact silent-degradation failure mode #1710 fixes.
        """
        from coord.worker_events import parse_log as claude_parse_log

        log = _write_fake_log(
            tmp_path / "fake.log",
            [
                _assistant_line("hello"),
                _fake_result_line(is_error=True, status=503, cost_usd=1.23),
            ],
        )
        legacy_summary = claude_parse_log(log, tail_bytes=0)
        assert legacy_summary.total_cost_usd == 0.0
        assert legacy_summary.is_error is False

        fixed_summary = FakeAgentProvider().parse_log(log, tail_bytes=0)
        assert fixed_summary.total_cost_usd == 1.23
        assert fixed_summary.is_error is True

    def test_noop_for_claude_default(self, tmp_path: Path) -> None:
        """No provider/provider_name passed → identical to pre-#1710 (plain
        ClaudeProvider default)."""
        log = tmp_path / "claude.log"
        log.write_text(
            "\n".join(
                [
                    json.dumps({"type": "system", "subtype": "init", "model": "sonnet"}),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [{"type": "tool_use", "name": "Bash", "input": {}}]
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )
        progress = parse_progress(log)
        assert progress.updates == ["Turn 1: Bash"]

    def test_tier_named_definition_misparses_without_cfg(self) -> None:
        """#2305 regression: `parse_progress`'s own `get_provider()` call
        (the site at `coord/progress.py:150`) is never handed `cfg` here —
        `oc-mid` degrades to ClaudeProvider, which has no idea how to read
        opencode's `step_finish` events, so the real 4-turn run reads as 0."""
        assert _OPENCODE_FIXTURE.exists(), "opencode_run_sample.jsonl fixture is missing"
        progress = parse_progress(_OPENCODE_FIXTURE, provider_name="oc-mid")
        assert progress.updates == ["Turn 0: edit"]

    def test_tier_named_definition_parses_correctly_with_cfg(self) -> None:
        """#2305 fix: threading `cfg` through resolves `oc-mid` to
        `OpenCodeProvider` at the `coord/progress.py:150` site, and the real
        4-turn count from the verbatim capture comes through."""
        cfg = _cfg_with_tier_provider("oc-mid", "opencode")
        progress = parse_progress(_OPENCODE_FIXTURE, provider_name="oc-mid", cfg=cfg)
        assert progress.updates == ["Turn 4: edit"]


# ── usage.py ─────────────────────────────────────────────────────────────────


class TestUsageSeam:
    def test_fake_provider_cost_is_correct(self, tmp_path: Path) -> None:
        log = _write_fake_log(
            tmp_path / "fake.log",
            [
                _assistant_line("working"),
                _fake_result_line(
                    is_error=False, cost_usd=2.5, tokens_in=1000, tokens_out=250,
                ),
            ],
        )
        usage = parse_usage_from_log(log, provider=FakeAgentProvider())
        assert usage is not None
        assert usage.total_cost_usd == 2.5
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 250

    def test_provider_name_string_resolves_and_parses(self, tmp_path: Path) -> None:
        """`provider_name` alone (no cfg) resolves a built-in provider — the
        shape `Assignment.provider_name` actually arrives in at real call
        sites (`_assignment_to_usage`, `_capture_cost`)."""
        log = tmp_path / "claude.log"
        log.write_text(
            json.dumps(
                {"type": "result", "total_cost_usd": 0.42, "num_turns": 3}
            )
            + "\n"
        )
        usage = parse_usage_from_log(log, provider_name="claude")
        assert usage is not None
        assert usage.total_cost_usd == 0.42

    def test_cost_reporting_provider_warns_on_unparseable_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#1710: a provider that claims cost_reporting=True but whose log
        isn't stream-json shaped must warn — never silently return None as
        if nothing were wrong."""
        log = tmp_path / "plaintext.log"
        log.write_text("just some plain text, not NDJSON at all\n")
        with caplog.at_level(logging.WARNING, logger="coord.usage"):
            usage = parse_usage_from_log(log, provider=FakeAgentProvider())
        assert usage is None
        assert any("cost_reporting=True" in r.message for r in caplog.records)

    def test_non_cost_reporting_provider_stays_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A provider that legitimately never reports cost (e.g.
        claude-pty) must NOT warn on a non-stream-json log — that's
        expected, not a defect."""
        from coord.providers.claude_pty import ClaudePtyProvider

        log = tmp_path / "pty.log"
        log.write_text("raw TTY bytes, not NDJSON\n")
        with caplog.at_level(logging.WARNING, logger="coord.usage"):
            usage = parse_usage_from_log(log, provider=ClaudePtyProvider())
        assert usage is None
        assert not caplog.records

    def test_tier_named_definition_misparses_without_cfg(self) -> None:
        """#2305 regression: the `get_provider()` call at `coord/usage.py:182`
        with no `cfg` degrades `oc-mid` to ClaudeProvider — the real 8392
        input tokens in the verbatim opencode capture read as 0."""
        u = parse_usage_from_log(_OPENCODE_FIXTURE, provider_name="oc-mid")
        assert u is not None
        assert u.input_tokens == 0
        assert u.num_turns == 0

    def test_tier_named_definition_parses_correctly_with_cfg(self) -> None:
        """#2305 fix: `cfg` threaded through resolves `oc-mid` to
        `OpenCodeProvider` at `coord/usage.py:182`, so the real numbers from
        the verbatim capture come through (see
        `test_opencode_parse_log_real_success_fixture` in
        `tests/test_providers.py` for where these values are pinned)."""
        cfg = _cfg_with_tier_provider("oc-mid", "opencode")
        u = parse_usage_from_log(_OPENCODE_FIXTURE, provider_name="oc-mid", cfg=cfg)
        assert u is not None
        assert u.input_tokens == 8392
        assert u.num_turns == 4


# ── failure_class.py ─────────────────────────────────────────────────────────


class TestFailureClassificationSeam:
    def test_fake_provider_environmental_classification(self, tmp_path: Path) -> None:
        log = _write_fake_log(
            tmp_path / "fake.log",
            [
                _assistant_line("hello"),
                _fake_result_line(is_error=True, status=503, reason="upstream flaky"),
            ],
        )
        c = classify_log(log, provider=FakeAgentProvider())
        assert c.failure_class == ENVIRONMENTAL
        assert c.api_status == 503

    def test_fake_provider_work_classification(self, tmp_path: Path) -> None:
        log = _write_fake_log(
            tmp_path / "fake.log",
            [
                _assistant_line("hello"),
                _fake_result_line(is_error=False),
            ],
        )
        c = classify_log(
            log, failure_reason="tests failed: 1 failed", provider=FakeAgentProvider(),
        )
        assert c.failure_class == WORK
        assert "1 failed" in c.reason

    def test_provider_name_threading_for_a_missing_provider_falls_back_loudly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = _write_fake_log(tmp_path / "fake.log", [_assistant_line("hello")])
        with caplog.at_level(logging.WARNING, logger="coord.providers"):
            c = classify_log(log, provider_name="nonexistent-provider")
        assert c.failure_class == WORK
        assert any("nonexistent-provider" in r.message for r in caplog.records)

    def test_tier_named_definition_misclassifies_without_cfg(
        self, tmp_path: Path
    ) -> None:
        """#2305 regression: the `get_provider()` call at
        `coord/failure_class.py:~511` with no `cfg` degrades `oc-mid` to
        ClaudeProvider — ClaudeProvider's parser has no idea how to read
        opencode's `type: "error"` event, so a genuine environmental 503
        classifies as an unknown WORK failure instead."""
        log = _write_fake_log(
            tmp_path / "opencode-error.log",
            [
                json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "name": "UnknownError",
                            "data": {"message": "API Error: 503 overloaded_error"},
                        },
                    }
                ),
            ],
        )
        c = classify_log(log, provider_name="oc-mid")
        assert c.failure_class == WORK

    def test_tier_named_definition_classifies_correctly_with_cfg(
        self, tmp_path: Path
    ) -> None:
        """#2305 fix: `cfg` threaded through resolves `oc-mid` to
        `OpenCodeProvider` at the `coord/failure_class.py` site, so the same
        log correctly classifies as ENVIRONMENTAL."""
        log = _write_fake_log(
            tmp_path / "opencode-error.log",
            [
                json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "name": "UnknownError",
                            "data": {"message": "API Error: 503 overloaded_error"},
                        },
                    }
                ),
            ],
        )
        cfg = _cfg_with_tier_provider("oc-mid", "opencode")
        c = classify_log(log, provider_name="oc-mid", cfg=cfg)
        assert c.failure_class == ENVIRONMENTAL
        assert c.api_status == 503


# ── review.py (unchanged code path — asserted for #1710's acceptance bar) ───


class TestReviewExtractionForASecondProvider:
    def test_review_verdict_extracted_from_fake_provider_log(self, tmp_path: Path) -> None:
        """review.py's stream-json branch only assumes the generic
        `type: "assistant"` / `message.content` envelope (shared by any
        Anthropic-Messages-API-shaped backend, not claude-CLI-specific
        business logic) — so it already extracts a verdict correctly from
        a second provider's log that reuses that envelope, with zero code
        changes. This is the "keep direct, justified" resolution from the
        #1710 inventory, verified here rather than just asserted in prose.
        """
        log = _write_fake_log(
            tmp_path / "fake.log",
            [
                _assistant_line("Looking at the diff now."),
                _assistant_line(
                    "REVIEW_VERDICT: approve\n"
                    "REVIEW_BODY:\n"
                    "Looks good, no blocking issues.\n"
                    "END_REVIEW"
                ),
                _fake_result_line(is_error=False),
            ],
        )
        findings = parse_review_from_log(log)
        assert findings is not None
        assert findings.verdict == "approve"
        assert "no blocking issues" in findings.body
