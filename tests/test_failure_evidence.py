"""#4092: failure-evidence extractor — unit tests over captured logs.

Three fixtures under ``tests/fixtures/failure_evidence/``:

- ``cargo_multistage_middle_truncated.log`` — a multi-stage cargo run whose
  only failure sits in the middle, buried under enough padding either side
  that the existing head/tail ``_tail_text`` split (#3715) would drop it.
- ``clean_pass.log`` — every stage passes, but still exercises the two
  false-positive traps a green run routinely hits: a pytest ``-ra`` "short
  test summary info" banner with ``SKIPPED`` entries (no actual failure),
  and a cargo target that prints ``running 0 tests`` alongside others that
  ran real tests. The extractor must still report no stages, no errors, and
  every signal False.
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
    failure_signature,
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


def test_clean_pass_skip_banner_does_not_name_pytest_as_failed() -> None:
    # The fixture's `-ra` "short test summary info" banner + SKIPPED lines
    # must not, by themselves, be read as a pytest failure.
    log = _read("clean_pass.log")
    assert "short test summary info" in log
    assert "SKIPPED" in log
    evidence = extract_failure_evidence(log)
    assert evidence.failed_stages == ()


def test_clean_pass_zero_test_target_does_not_set_no_tests_ran() -> None:
    # The fixture's `running 0 tests` target sits alongside targets that ran
    # real tests — not every target running zero tests, so this must stay
    # False.
    log = _read("clean_pass.log")
    assert "running 0 tests" in log
    evidence = extract_failure_evidence(log)
    assert evidence.signals.no_tests_ran is False


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


def test_pytest_ra_banner_alone_is_not_a_failure() -> None:
    # `-ra` (this repo's own addopts) prints the "short test summary info"
    # banner for ANY non-passed outcome, including a plain SKIPPED — that
    # banner alone must never be read as a pytest failure.
    log = (
        "collected 10 items\n"
        "tests/test_x.py ..........                                        [100%]\n"
        "=========================== short test summary info =============================\n"
        "SKIPPED [1] tests/test_x.py:5: only fires off macOS\n"
        "============================ 9 passed, 1 skipped in 0.12s ========================\n"
    )
    evidence = extract_failure_evidence(log)
    assert evidence.failed_stages == ()
    assert evidence.is_empty


def test_pytest_failed_tally_names_pytest_even_without_failed_line() -> None:
    # A closing tally line ("N failed, ...") is itself a real failure
    # indication, independent of a `FAILED <nodeid>` short-summary line.
    log = (
        "collected 2 items\n"
        "========================= 1 failed, 1 passed in 0.05s =====================\n"
    )
    evidence = extract_failure_evidence(log)
    assert evidence.failed_stages == ("pytest",)


# ── Echo-trap defense ───────────────────────────────────────────────────────


def test_echoed_test_command_does_not_shadow_the_real_failed_stage() -> None:
    """A multi-stage runner's own `test_command` is routinely echoed into the
    log before it ever runs (a `set -x` trace, a script-source dump for a
    verbose failure report) — and that echo can itself contain the literal
    `FAILED STAGES (stage order, first is likely root): ...` phrasing as
    unexpanded template text, with no real failure behind it yet. This is
    the real production shape (#4092's extractor runs over the stored leg
    output exactly as `coord.notify._record_smoke_verdict` persists it,
    which never carries a trailing `SMOKE:` line of its own — that marker
    is a worker-transport detail stripped before storage). The FIRST
    occurrence here is exactly that echo; only the LAST is the real verdict,
    and it must win even with no further corroboration.
    """
    log = (
        "+ grep -A1 FAILED_STAGES ci-check.sh\n"
        "FAILED STAGES (stage order, first is likely root): $failed_list\n"
        "running stage 1/2: cargo fmt --check\n"
        "stage 1/2 OK\n"
        "running stage 2/2: cargo test --workspace --lib\n"
        "error[E0308]: mismatched types\n"
        " --> src/widget.rs:120:9\n"
        "test result: FAILED. 0 passed; 1 failed\n"
        "FAILED STAGES (stage order, first is likely root): cargo test --workspace --lib\n"
    )
    evidence = extract_failure_evidence(log)
    assert evidence.failed_stages == ("cargo test --workspace --lib",)


def test_single_failed_stages_line_is_still_trusted() -> None:
    """The common case: exactly one `FAILED STAGES` line in the log is
    trusted on its own.
    """
    log = (
        "running stage 1/1: cargo test --workspace --lib\n"
        "test result: FAILED. 0 passed; 1 failed\n"
        "FAILED STAGES (stage order, first is likely root): cargo test --workspace --lib\n"
    )
    evidence = extract_failure_evidence(log)
    assert evidence.failed_stages == ("cargo test --workspace --lib",)


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


# ── #4166: failure fingerprinting (the per-base-SHA cache's key) ───────────


def test_the_same_defect_on_two_machines_fingerprints_identically() -> None:
    """Two runs of ONE defect differ in everything that isn't the defect:
    temp dirs, elapsed times, pids, surrounding line numbers. The cache key
    must not move because of any of it."""
    run_a = (
        "running stage 1/2: cargo test --workspace --lib\n"
        "thread 'tests::resize' panicked at /tmp/build-1234/src/widget.rs:120:9:\n"
        "assertion `left == right` failed\n"
        "test result: FAILED. 0 passed; 1 failed; finished in 12.84s\n"
        "FAILED STAGES (stage order, first is likely root): cargo test --workspace --lib\n"
    )
    run_b = (
        "running stage 1/2: cargo test --workspace --lib\n"
        "thread 'tests::resize' panicked at /var/folders/zz/build-98765/src/widget.rs:131:9:\n"
        "assertion `left == right` failed\n"
        "test result: FAILED. 0 passed; 1 failed; finished in 41.02s\n"
        "FAILED STAGES (stage order, first is likely root): cargo test --workspace --lib\n"
    )

    sig_a = failure_signature(extract_failure_evidence(run_a))
    sig_b = failure_signature(extract_failure_evidence(run_b))

    assert sig_a
    assert sig_a == sig_b


def test_two_different_defects_fingerprint_differently() -> None:
    compile_failure = extract_failure_evidence(
        "error[E0308]: mismatched types\n"
        " --> src/widget.rs:120:9\n"
        "error: could not compile `quadraui`\n"
    )
    test_failure = extract_failure_evidence(_read("pytest_failure.log"))

    assert failure_signature(compile_failure) != failure_signature(test_failure)


def test_an_empty_evidence_record_has_no_fingerprint() -> None:
    """Nothing may ever be keyed off "no evidence at all" — a clean pass and
    an unparseable log must not collide on one cache key."""
    assert failure_signature(FailureEvidence()) == ""
    assert failure_signature(extract_failure_evidence(_read("clean_pass.log"))) == ""


def test_a_fingerprint_is_short_and_stable_across_calls() -> None:
    evidence = extract_failure_evidence(_read("pytest_failure.log"))

    first = failure_signature(evidence)
    assert first == failure_signature(evidence)
    assert len(first) == 16
