"""#4092: failure-evidence extractor — unit tests over captured logs.

Three fixtures under ``tests/fixtures/failure_evidence/``:

- ``cargo_multistage_middle_truncated.log`` — a multi-stage cargo run whose
  only failure sits in the middle, buried under enough padding either side
  that the existing head/tail ``_tail_text`` split (#3715) would drop it.
- ``clean_pass.log`` — every stage passes; the extractor must report no
  stages, no errors, and every signal False.
- ``pytest_failure.log`` — a pytest run with one failing test and no cargo
  markers at all.

Plus direct (non-fixture) coverage for the ``compile_error``/``ratchet``/
``lint``/``no_tests_ran`` signals the three fixtures above don't each
exercise, and an integration check that :func:`coord.agent.run_plain_runner_
command` actually splices the extracted block into a truncated log.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coord.failure_evidence import (
    FailureEvidence,
    FailureSignals,
    extract_failure_evidence,
    format_evidence_block,
)

FIXTURES = Path(__file__).parent / "fixtures" / "failure_evidence"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


# ── Fixture 1: middle-truncated multi-stage cargo log ──────────────────────


def test_cargo_multistage_names_the_right_stage() -> None:
    evidence = extract_failure_evidence(_read("cargo_multistage_middle_truncated.log"))
    assert evidence.failed_stages == ("cargo test --workspace --lib",)


def test_cargo_multistage_captures_panic_and_rustc_blocks() -> None:
    evidence = extract_failure_evidence(_read("cargo_multistage_middle_truncated.log"))
    assert "thread 'tests::test_widget_resize' panicked at" in evidence.errors
    assert "assertion `left == right` failed" in evidence.errors
    # The rustc block, including the `-->` location line the issue calls
    # out by name.
    assert "error[E0308]: mismatched types" in evidence.errors
    assert "--> src/widget.rs:120:9" in evidence.errors
    assert "test result: FAILED" in evidence.errors


def test_cargo_multistage_signals() -> None:
    evidence = extract_failure_evidence(_read("cargo_multistage_middle_truncated.log"))
    signals = evidence.signals.to_dict()
    assert signals["compile_error"] is True
    assert signals["test_assertion"] is True
    assert signals["ratchet"] is False
    assert signals["lint"] is False
    assert signals["no_tests_ran"] is False


def test_cargo_multistage_errors_are_bounded() -> None:
    evidence = extract_failure_evidence(_read("cargo_multistage_middle_truncated.log"))
    assert len(evidence.errors) < 8192 + 64


def test_cargo_multistage_survives_tail_truncation() -> None:
    """The acceptance bar this module exists for: a plain `_tail_text`-style
    head/tail split drops the failure, but splicing in the extracted block
    keeps it.
    """
    from coord.agent import _tail_text

    log = _read("cargo_multistage_middle_truncated.log")
    evidence = extract_failure_evidence(log)

    plain = _tail_text(log)
    assert "panicked at" not in plain  # the #3715 split alone loses it

    spliced = _tail_text(log, evidence=evidence)
    assert "panicked at" in spliced
    assert "error[E0308]" in spliced
    assert "cargo test --workspace --lib" in spliced


# ── Fixture 2: clean pass ────────────────────────────────────────────────


def test_clean_pass_has_no_stages_or_errors() -> None:
    evidence = extract_failure_evidence(_read("clean_pass.log"))
    assert evidence.failed_stages == ()
    assert evidence.errors == ""
    assert evidence.is_empty


def test_clean_pass_every_signal_false() -> None:
    evidence = extract_failure_evidence(_read("clean_pass.log"))
    assert evidence.signals == FailureSignals()


def test_clean_pass_formats_to_empty_block() -> None:
    evidence = extract_failure_evidence(_read("clean_pass.log"))
    assert format_evidence_block(evidence) == ""


# ── Fixture 3: pytest failure ────────────────────────────────────────────


def test_pytest_failure_names_pytest_as_the_stage() -> None:
    evidence = extract_failure_evidence(_read("pytest_failure.log"))
    assert evidence.failed_stages == ("pytest",)


def test_pytest_failure_captures_failed_and_assertion_lines() -> None:
    evidence = extract_failure_evidence(_read("pytest_failure.log"))
    assert "FAILED tests/test_widgets.py::test_widget_resize" in evidence.errors
    assert "E       assert 80 == 100" in evidence.errors


def test_pytest_failure_signals() -> None:
    evidence = extract_failure_evidence(_read("pytest_failure.log"))
    signals = evidence.signals.to_dict()
    assert signals["test_assertion"] is True
    assert signals["compile_error"] is False
    assert signals["lint"] is False
    assert signals["ratchet"] is False
    assert signals["no_tests_ran"] is False


# ── Direct (non-fixture) signal coverage ────────────────────────────────


def test_compile_error_signal_from_could_not_compile() -> None:
    log = (
        "   Compiling mycrate v0.1.0\n"
        "error[E0433]: failed to resolve: use of undeclared crate\n"
        " --> src/lib.rs:3:5\n"
        "  |\n"
        "3 |     foo::bar();\n"
        "  |     ^^^ use of undeclared crate `foo`\n"
        "\n"
        "error: could not compile `mycrate` (lib) due to 1 previous error\n"
    )
    evidence = extract_failure_evidence(log)
    assert evidence.failed_stages == ("mycrate",)
    assert evidence.signals.compile_error is True
    assert "failed to resolve" in evidence.errors


def test_ratchet_signal() -> None:
    log = (
        "running coverage ratchet check\n"
        "FAIL: coverage may only shrink to 0, it grew from 82 to 79\n"
    )
    evidence = extract_failure_evidence(log)
    assert evidence.signals.ratchet is True


def test_lint_signal_from_clippy_lint_id() -> None:
    log = (
        "=== stage: cargo clippy --all-targets -- -D warnings ===\n"
        "error: unused import: `std::fmt`\n"
        " --> src/lib.rs:1:5\n"
        "  |\n"
        "1 | use std::fmt;\n"
        "  |     ^^^^^^^^\n"
        "  |\n"
        "  = note: `-D unused-imports` implied by `-D warnings`\n"
        "  = note: #[deny(clippy::all)] on by default\n"
        "clippy::unused_imports\n"
    )
    evidence = extract_failure_evidence(log)
    assert evidence.signals.lint is True


def test_lint_signal_is_not_set_by_a_stage_name_alone() -> None:
    # A stage merely NAMED "cargo fmt --check" that then passes must not
    # look like a lint failure just because the words appear in the log.
    log = "=== stage 1/8: cargo fmt --all -- --check ===\n=== stage 1/8 OK ===\n"
    evidence = extract_failure_evidence(log)
    assert evidence.signals.lint is False


def test_no_tests_ran_signal() -> None:
    log = "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
    evidence = extract_failure_evidence(log)
    assert evidence.signals.no_tests_ran is True


# ── Round-trip (dict) serialisation — this is what gets persisted ──────────


def test_to_dict_and_from_dict_round_trip() -> None:
    evidence = extract_failure_evidence(_read("cargo_multistage_middle_truncated.log"))
    restored = FailureEvidence.from_dict(evidence.to_dict())
    assert restored == evidence


def test_from_dict_of_none_is_an_empty_record() -> None:
    assert FailureEvidence.from_dict(None) == FailureEvidence()
    assert FailureEvidence.from_dict({}) == FailureEvidence()


# ── Integration: the agent-side plain-runner path persists evidence ───────


def test_run_plain_runner_command_carries_failure_evidence() -> None:
    from coord.agent import run_plain_runner_command

    log = _read("cargo_multistage_middle_truncated.log")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=["x"], returncode=1, stdout=log, stderr="")

    verdict = run_plain_runner_command("whatever", "/tmp", timeout=5.0, run=fake_run)
    assert verdict.failure_evidence is not None
    assert verdict.failure_evidence.failed_stages == ("cargo test --workspace --lib",)
    assert "panicked at" in verdict.log_tail


def test_run_plain_runner_command_pass_has_empty_evidence() -> None:
    from coord.agent import run_plain_runner_command

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["x"], returncode=0, stdout=_read("clean_pass.log"), stderr="",
        )

    verdict = run_plain_runner_command("whatever", "/tmp", timeout=5.0, run=fake_run)
    assert verdict.failure_evidence is not None
    assert verdict.failure_evidence.is_empty
