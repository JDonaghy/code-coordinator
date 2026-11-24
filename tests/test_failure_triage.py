"""#4093: triage-first failure classifier — deterministic rules plus the
cheap-model fallback tier.

Three groups:

* ``TestInfraRule`` / ``TestMechanicalRule`` — the deterministic tier, which
  must decide WITHOUT ever touching ``subprocess.run`` (asserted directly,
  not just inferred from a passing test).
* ``TestModelTier`` — the one-cheap-``claude -p``-call tier, with
  ``subprocess.run`` stubbed to return valid, invalid, and low-confidence
  JSON.
* ``TestQuadraui2992`` — the issue's own acceptance fixture: an E0063 in two
  macOS files plus a ratchet off by one, which must classify as
  ``mechanical`` with ``repro_capability="macos"`` and no model call.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from coord.failure_evidence import FailureEvidence, FailureSignals, extract_failure_evidence
from coord.failure_triage import (
    BUCKET_FLAKY,
    BUCKET_HUMAN,
    BUCKET_INFRA,
    BUCKET_MECHANICAL,
    BUCKET_SUBSTANTIVE,
    DiffStat,
    ProposedScope,
    TriageVerdict,
    is_degraded_verdict,
    triage_failure,
)


def _evidence(**signals: bool) -> FailureEvidence:
    return FailureEvidence(signals=FailureSignals(**signals))


# ── deterministic tier 1: infra ─────────────────────────────────────────────


class TestInfraRule:
    def test_usage_limit_is_infra_with_no_model_call(self) -> None:
        with patch("subprocess.run") as mock_run:
            verdict = triage_failure(
                evidence=_evidence(),
                failure_reason="usage limit reached, resets 8:30pm (America/Chicago)",
            )
        mock_run.assert_not_called()
        assert verdict.bucket == BUCKET_INFRA
        assert verdict.confidence == 1.0
        assert verdict.via.startswith("deterministic:infra:")
        assert verdict.proposed_scope == ProposedScope()

    def test_api_5xx_is_infra_with_no_model_call(self) -> None:
        with patch("subprocess.run") as mock_run:
            verdict = triage_failure(evidence=_evidence(), api_error_status=529)
        mock_run.assert_not_called()
        assert verdict.bucket == BUCKET_INFRA

    def test_network_error_is_infra_with_no_model_call(self) -> None:
        with patch("subprocess.run") as mock_run:
            verdict = triage_failure(evidence=_evidence(), network_error_state="timeout")
        mock_run.assert_not_called()
        assert verdict.bucket == BUCKET_INFRA

    def test_3912_toolchain_collision_text_is_infra(self) -> None:
        """#3912's signature (folded into coord.failure_class's own infra
        regex) — a #3182 fan-out leg racing a sibling for a shared build
        dir deletes a `.d` file mid-compile."""
        with patch("subprocess.run") as mock_run:
            verdict = triage_failure(
                evidence=_evidence(compile_error=True),
                failure_reason=(
                    "error: could not parse dep info at: target/debug/.fingerprint/"
                    "foo-1/dep-lib-foo: No such file or directory"
                ),
            )
        mock_run.assert_not_called()
        assert verdict.bucket == BUCKET_INFRA

    def test_ordinary_work_failure_is_not_infra(self) -> None:
        """No environmental signal at all -> the infra rule must not fire,
        so an ordinary work failure falls through to the later tiers."""
        with patch("subprocess.run") as mock_run:
            verdict = triage_failure(
                evidence=_evidence(test_assertion=True),
                failure_reason="assertion failed: expected 3 got 4",
                diff_stat=DiffStat(files=("coord/foo.py",)),
            )
        # No infra signal and no mechanical signal -> falls through to the
        # model tier, which IS allowed to call subprocess here.
        mock_run.assert_called_once()
        assert verdict.bucket != BUCKET_INFRA


# ── deterministic tier 2: mechanical ────────────────────────────────────────


class TestMechanicalRule:
    def test_single_ratchet_confined_to_diff_is_mechanical_no_model_call(self) -> None:
        with patch("subprocess.run") as mock_run:
            verdict = triage_failure(
                evidence=_evidence(ratchet=True),
                diff_stat=DiffStat(files=("coord/db.py",)),
            )
        mock_run.assert_not_called()
        assert verdict.bucket == BUCKET_MECHANICAL
        assert verdict.confidence == 1.0
        assert "ratchet" in verdict.via

    def test_lint_confined_to_diff_is_mechanical_no_model_call(self) -> None:
        with patch("subprocess.run") as mock_run:
            verdict = triage_failure(
                evidence=_evidence(lint=True),
                diff_stat=DiffStat(files=("coord/foo.py",)),
            )
        mock_run.assert_not_called()
        assert verdict.bucket == BUCKET_MECHANICAL
        assert "lint" in verdict.via

    def test_compile_error_not_confined_to_diff_falls_to_model(self) -> None:
        """The failure names a file the diff never touched -> the
        confinement gate must refuse mechanical and fall through."""
        log = (
            "error[E0308]: mismatched types\n"
            "  --> src/unrelated.rs:10:1\n"
            "   |\n"
            "10 |     let x: i32 = \"oops\";\n"
            "   |                  ^^^^^^ expected `i32`, found `&str`\n"
        )
        evidence = extract_failure_evidence(log)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["claude"], returncode=0,
                stdout=json.dumps({"result": json.dumps({
                    "bucket": "substantive",
                    "diagnosis": "a real behavioural bug, needs investigation here.",
                    "repro_capability": None,
                    "proposed_scope": {"files": [], "estimated_line_delta": 10},
                    "confidence": 0.9,
                })}),
                stderr="",
            )
            verdict = triage_failure(
                evidence=evidence,
                diff_stat=DiffStat(files=("src/other.rs",)),
            )
        mock_run.assert_called_once()
        assert verdict.bucket == BUCKET_SUBSTANTIVE

    def test_too_many_error_sites_falls_to_model(self) -> None:
        """More error sites than the 'small count' gate allows -> not a
        rote fix, fall through rather than guessing mechanical."""
        header = "error[E0308]: mismatched types\n"
        log = header * 10
        evidence = extract_failure_evidence(log)
        assert evidence.signals.compile_error is True
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["claude"], returncode=1, stdout="", stderr="boom",
            )
            verdict = triage_failure(
                evidence=evidence, diff_stat=DiffStat(files=("src/widget.rs",))
            )
        mock_run.assert_called_once()
        # The model call itself failed in this test, so the safe fallback
        # fires -- the point being asserted is that the mechanical rule did
        # NOT short-circuit it.
        assert verdict.bucket == BUCKET_SUBSTANTIVE

    def test_no_signal_at_all_falls_to_model(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["claude"], returncode=1, stdout="", stderr="boom",
            )
            triage_failure(evidence=_evidence(), diff_stat=DiffStat(files=("a.py",)))
        mock_run.assert_called_once()

    def test_compile_error_with_no_located_file_falls_to_model(self) -> None:
        """A compile_error signal whose text never named a file at all
        can't have its confinement verified -- must not guess mechanical."""
        evidence = FailureEvidence(
            errors="error: something went wrong, no location given",
            signals=FailureSignals(compile_error=True),
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["claude"], returncode=1, stdout="", stderr="boom",
            )
            verdict = triage_failure(evidence=evidence, diff_stat=DiffStat(files=("a.rs",)))
        mock_run.assert_called_once()
        assert verdict.bucket == BUCKET_SUBSTANTIVE


# ── model tier ───────────────────────────────────────────────────────────────


def _claude_result(payload: object, *, returncode: int = 0) -> subprocess.CompletedProcess:
    if isinstance(payload, str):
        stdout_result = payload
    else:
        stdout_result = json.dumps(payload)
    return subprocess.CompletedProcess(
        args=["claude"],
        returncode=returncode,
        stdout=json.dumps({"result": stdout_result, "total_cost_usd": 0.001}),
        stderr="",
    )


class TestModelTier:
    def test_valid_json_is_used_verbatim(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _claude_result({
                "bucket": "flaky",
                "diagnosis": "This test is order-dependent and failed only once in ten reruns.",
                "repro_capability": None,
                "proposed_scope": {"files": [], "estimated_line_delta": 0},
                "confidence": 0.8,
            })
            verdict = triage_failure(
                evidence=_evidence(test_assertion=True),
                diff_stat=DiffStat(files=("coord/foo.py",)),
            )
        assert verdict.bucket == BUCKET_FLAKY
        assert verdict.confidence == pytest.approx(0.8)
        assert verdict.via == "model"

    def test_model_sees_evidence_and_diff_stat_not_raw_log(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _claude_result({
                "bucket": "human",
                "diagnosis": "This looks like a deliberate product decision, not a bug.",
                "repro_capability": None,
                "proposed_scope": {"files": [], "estimated_line_delta": 0},
                "confidence": 0.7,
            })
            evidence = _evidence(test_assertion=True)
            triage_failure(
                evidence=evidence,
                diff_stat=DiffStat(files=("coord/foo.py",), total_lines=42),
                declared_files=("coord/foo.py",),
                machine_capabilities=("linux",),
            )
        user_message = mock_run.call_args.kwargs["input"]
        payload = json.loads(user_message)
        assert payload["diff_stat"] == {"files": ["coord/foo.py"], "total_lines": 42}
        assert payload["declared_files"] == ["coord/foo.py"]
        assert payload["machine_capabilities"] == ["linux"]
        assert "evidence" in payload

    def test_invalid_json_falls_back_to_substantive(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _claude_result("not json at all, just prose")
            verdict = triage_failure(evidence=_evidence(test_assertion=True))
        assert verdict.bucket == BUCKET_SUBSTANTIVE
        assert verdict.via == "model:call_failed"
        assert verdict.confidence == 0.0
        assert is_degraded_verdict(verdict) is True

    def test_json_missing_required_field_falls_back_to_substantive(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _claude_result({"diagnosis": "no bucket field here"})
            verdict = triage_failure(evidence=_evidence(test_assertion=True))
        assert verdict.bucket == BUCKET_SUBSTANTIVE
        assert verdict.via == "model:schema_error"
        assert is_degraded_verdict(verdict) is True

    def test_unknown_bucket_falls_back_to_substantive(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _claude_result({
                "bucket": "not-a-real-bucket",
                "diagnosis": "whatever",
                "repro_capability": None,
                "proposed_scope": {"files": [], "estimated_line_delta": 0},
                "confidence": 0.9,
            })
            verdict = triage_failure(evidence=_evidence(test_assertion=True))
        assert verdict.bucket == BUCKET_SUBSTANTIVE
        assert verdict.via == "model:schema_error"
        assert is_degraded_verdict(verdict) is True

    def test_low_confidence_falls_back_to_substantive(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _claude_result({
                "bucket": "mechanical",
                "diagnosis": "maybe a rote fix, not sure.",
                "repro_capability": None,
                "proposed_scope": {"files": [], "estimated_line_delta": 1},
                "confidence": 0.2,
            })
            verdict = triage_failure(evidence=_evidence(test_assertion=True), min_confidence=0.5)
        assert verdict.bucket == BUCKET_SUBSTANTIVE
        assert is_degraded_verdict(verdict) is True
        assert verdict.via == "model:low_confidence"

    def test_compile_error_can_never_be_flaky_even_from_model(self) -> None:
        """#4093: the model proposing 'flaky' over a compile_error signal
        must be overridden -- a compile error is a durable defect, never
        noise that clears on retry."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _claude_result({
                "bucket": "flaky",
                "diagnosis": "This looks like a non-deterministic ordering issue.",
                "repro_capability": None,
                "proposed_scope": {"files": [], "estimated_line_delta": 0},
                "confidence": 0.9,
            })
            # Too many error sites for the mechanical rule, so it legitimately
            # reaches the model tier with compile_error still set.
            evidence = extract_failure_evidence("error[E0308]: mismatched types\n" * 10)
            verdict = triage_failure(evidence=evidence, diff_stat=DiffStat(files=("src/a.rs",)))
        assert verdict.bucket == BUCKET_SUBSTANTIVE
        assert verdict.via == "model:compile_error_guard"
        # #4093 review (blocking finding 1): this is a CONFIDENT,
        # evidence-backed override, not "the classifier had nothing to
        # say" — callers must not treat it as a degraded/no-verdict
        # sentinel (coord.drive's `_decide_test` would otherwise retry
        # the exact quadraui#2992 shape this guard exists to catch).
        assert is_degraded_verdict(verdict) is False

    def test_nonzero_exit_falls_back_to_substantive(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["claude"], returncode=1, stdout="", stderr="no credentials",
            )
            verdict = triage_failure(evidence=_evidence(test_assertion=True))
        assert verdict.bucket == BUCKET_SUBSTANTIVE
        assert verdict.via == "model:call_failed"
        assert is_degraded_verdict(verdict) is True

    def test_timeout_falls_back_to_substantive(self) -> None:
        timeout_error = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        with patch("subprocess.run", side_effect=timeout_error):
            verdict = triage_failure(evidence=_evidence(test_assertion=True))
        assert verdict.bucket == BUCKET_SUBSTANTIVE
        assert verdict.via == "model:call_failed"
        assert is_degraded_verdict(verdict) is True

    def test_configured_model_and_low_thinking_budget_are_used(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _claude_result({
                "bucket": "substantive",
                "diagnosis": "needs a human look, not obviously anything else.",
                "repro_capability": None,
                "proposed_scope": {"files": [], "estimated_line_delta": 5},
                "confidence": 0.6,
            })
            triage_failure(
                evidence=_evidence(test_assertion=True),
                model="claude-sonnet-4-5",
                max_thinking_tokens=256,
            )
        argv = mock_run.call_args.args[0]
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "claude-sonnet-4-5"
        env = mock_run.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "256"


# ── quadraui#2992 acceptance fixture ────────────────────────────────────────


_QUADRAUI_2992_LOG = """\
running 2 tests
error[E0063]: missing field `foo` in initializer of `Bar`
  --> src/platform/macos/input.rs:42:5
   |
42 |     Bar {}
   |     ^^^^^^^

error[E0063]: missing field `baz` in initializer of `Qux`
  --> src/platform/macos/window.rs:88:9
   |
88 |     Qux {}
   |     ^^^^^^^

ratchet: coverage grew from 10 to 11, may only shrink
FAILED STAGES (stage order, first is likely root): cargo build
"""


class TestQuadraui2992:
    """The epic's own case study (quadraui#2992): an E0063 compile error in
    two macOS-specific files, plus a ratchet that moved by one -- a small,
    well-scoped, deterministically mechanical failure."""

    def _evidence(self):
        return extract_failure_evidence(_QUADRAUI_2992_LOG)

    def test_signals_extracted_as_expected(self) -> None:
        evidence = self._evidence()
        assert evidence.signals.compile_error is True
        assert evidence.signals.ratchet is True
        assert evidence.signals.lint is False

    def test_classifies_mechanical_with_macos_capability_no_model_call(self) -> None:
        evidence = self._evidence()
        with patch("subprocess.run") as mock_run:
            verdict = triage_failure(
                evidence=evidence,
                diff_stat=DiffStat(
                    files=(
                        "src/platform/macos/input.rs",
                        "src/platform/macos/window.rs",
                    )
                ),
                machine_capabilities=("macos", "arm64"),
            )
        mock_run.assert_not_called()
        assert verdict.bucket == BUCKET_MECHANICAL
        assert verdict.repro_capability == "macos"
        assert verdict.confidence == 1.0
        assert set(verdict.proposed_scope.files) == {
            "src/platform/macos/input.rs",
            "src/platform/macos/window.rs",
        }

    def test_repro_capability_inferred_from_path_even_without_machine_hint(self) -> None:
        """The path-based inference must not require the caller to already
        know the machine's capabilities."""
        evidence = self._evidence()
        with patch("subprocess.run") as mock_run:
            verdict = triage_failure(
                evidence=evidence,
                diff_stat=DiffStat(
                    files=(
                        "src/platform/macos/input.rs",
                        "src/platform/macos/window.rs",
                    )
                ),
            )
        mock_run.assert_not_called()
        assert verdict.bucket == BUCKET_MECHANICAL
        assert verdict.repro_capability == "macos"


# ── dataclass plumbing ───────────────────────────────────────────────────────


def test_diffstat_to_dict() -> None:
    assert DiffStat(files=("a.py",), total_lines=5).to_dict() == {
        "files": ["a.py"], "total_lines": 5,
    }


def test_proposed_scope_to_dict() -> None:
    assert ProposedScope(files=("a.py",), estimated_line_delta=3).to_dict() == {
        "files": ["a.py"], "estimated_line_delta": 3,
    }


def test_triage_verdict_to_dict_round_trips_shape() -> None:
    verdict = TriageVerdict(
        bucket=BUCKET_HUMAN,
        diagnosis="needs a human",
        repro_capability="macos",
        proposed_scope=ProposedScope(files=("a.py",), estimated_line_delta=1),
        confidence=0.75,
        via="model",
    )
    d = verdict.to_dict()
    assert d["bucket"] == BUCKET_HUMAN
    assert d["repro_capability"] == "macos"
    assert d["proposed_scope"] == {"files": ["a.py"], "estimated_line_delta": 1}
    assert d["confidence"] == 0.75


# ── #4093 review (blocking finding 1): `is_degraded_verdict` ─────────────────


def _verdict(via: str, confidence: float = 0.0, bucket: str = BUCKET_SUBSTANTIVE) -> TriageVerdict:
    return TriageVerdict(
        bucket=bucket,
        diagnosis="x",
        repro_capability=None,
        proposed_scope=ProposedScope(),
        confidence=confidence,
        via=via,
    )


@pytest.mark.parametrize(
    "via", ["model:call_failed", "model:schema_error", "model:low_confidence"]
)
def test_is_degraded_verdict_true_for_classifier_unusable_sentinels(via: str) -> None:
    assert is_degraded_verdict(_verdict(via)) is True


@pytest.mark.parametrize(
    "via",
    [
        "model",
        "model:compile_error_guard",
        "deterministic:infra:usage_limit",
        "deterministic:mechanical:compile_error",
    ],
)
def test_is_degraded_verdict_false_for_real_decisions(via: str) -> None:
    """A real model verdict, the compile-error guard override, and both
    deterministic tiers are all confident decisions ABOUT THE CODE — none
    of them is "the classifier had nothing to say"."""
    assert is_degraded_verdict(_verdict(via)) is False
