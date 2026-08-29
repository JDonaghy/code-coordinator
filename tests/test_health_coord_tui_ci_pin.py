"""Black-box tests for the ``coord_tui_ci_pin`` check (#2900, Phase 4 of #2894).

The subject: since #2899 put coord-tui in its own repo, **coord-tui's CI is
the only thing standing between a field rename here and a wire contract that
has silently drifted**. Two ways that guarantee rots, and this check has to
catch both:

1. the ``pip install code-coordinator[server]`` that supplies the generator
   goes stale / tombstoned / client-only, and
2. the ``scripts/codegen.py --rust --check`` step simply **disappears**,
   which is worse because nothing goes red — there is just no longer anything
   checking.

Everything here drives the probe through a real on-disk checkout + real
workflow YAML — the same path the health tick takes — rather than poking at
parser internals. The last test points it at the scaffold this repo actually
ships, so the template and the check that grades it cannot drift apart.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from coord.config import HealthConfig
from coord.health.checks import coord_tui_ci_pin as ctp
from coord.health.models import Checkout, HealthContext, Severity

NOW = 1_800_000_000.0

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAFFOLD = REPO_ROOT / "scripts" / "coord-tui-scaffold"

#: The drift step exactly as the shipped scaffold writes it.
DRIFT_STEP = 'COORD_TUI_SRC="$GITHUB_WORKSPACE" python scripts/codegen.py --rust --check'
GOOD_INSTALL = "pip install 'code-coordinator[server]'"


@pytest.fixture(autouse=True)
def _stable_local_version(monkeypatch):
    """Pin the "what coord is on this box" side of the floor comparison.

    Otherwise these tests would grade differently on a source checkout
    (``0+unknown``) than on a machine with a real wheel installed.
    """
    monkeypatch.setattr(ctp, "LOCAL_COORD_VERSION", "0.4.90")


def make_ctx(tmp_path: Path, **kwargs) -> HealthContext:
    home = kwargs.pop("home", tmp_path)
    return HealthContext(
        thresholds=kwargs.pop("thresholds", None) or HealthConfig(),
        home=home,
        coord_dir=kwargs.pop("coord_dir", home / ".coord"),
        now=kwargs.pop("now", NOW),
        checkouts=kwargs.pop("checkouts", ()),
        config=kwargs.pop("config", None),
        allow_network=kwargs.pop("allow_network", True),
    )


def write_ci(
    root: Path,
    install_line: str | None,
    drift_line: str | None,
    *,
    name: str = "codegen-drift.yml",
) -> Path:
    """A minimal but structurally real `coord-tui` drift workflow."""
    steps = ""
    if install_line:
        steps += f"      - run: {install_line}\n"
    if drift_line:
        steps += f"      - run: {drift_line}\n"
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows" / name).write_text(
        textwrap.dedent(
            """\
            name: Generated-artifact drift
            on:
              pull_request:
                branches: [main]
            jobs:
              cargo-test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: cargo test
              drift:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
            """
        )
        + steps,
        encoding="utf-8",
    )
    return root


def make_coord_tui(
    tmp_path: Path,
    install_line: str | None,
    drift_line: str | None = DRIFT_STEP,
    *,
    name: str = "coord-tui",
) -> Path:
    root = tmp_path / "src" / name
    (root / "src" / "app").mkdir(parents=True, exist_ok=True)
    # The structural marker `resolve_coord_tui_checkout` falls back to.
    (root / "src" / "app" / "data.rs").write_text("// coord-tui\n")
    write_ci(root, install_line, drift_line)
    return root


def probe(tmp_path: Path, install_line: str | None, drift_line=DRIFT_STEP, **ctx_kwargs):
    root = make_coord_tui(tmp_path, install_line, drift_line)
    checkouts = ctx_kwargs.pop("checkouts", (Checkout(name="coord-tui", path=root),))
    return ctp.probe_coord_tui_ci_pin(make_ctx(tmp_path, checkouts=checkouts, **ctx_kwargs))


# ── absence ──────────────────────────────────────────────────────────────────


def test_no_coord_tui_checkout_is_ok_not_a_fault(tmp_path) -> None:
    """Most machines have no coord-tui checkout. That is not a finding."""
    result = ctp.probe_coord_tui_ci_pin(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert "not present" in result.headroom
    assert result.values["present"] is False


def test_a_configured_checkout_that_does_not_exist_is_ok_not_a_crash(tmp_path) -> None:
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(coord_tui_checkout=str(tmp_path / "nope"))
    )
    result = ctp.probe_coord_tui_ci_pin(ctx)
    assert result.severity is Severity.OK
    assert result.values["present"] is False


# ── the install half ─────────────────────────────────────────────────────────


def test_healthy_ci_is_ok_and_names_the_spec_and_the_gate(tmp_path) -> None:
    """The acceptance criterion: green, with the installed spec in the output."""
    result = probe(tmp_path, GOOD_INSTALL)
    assert result.severity is Severity.OK
    assert "code-coordinator[server]" in result.headroom
    assert "codegen-drift.yml:drift" in result.headroom
    # ...and the reader is told WHAT the pin protects.
    assert "src/app/types/generated.rs" in result.detail
    assert "src/app/types/generated_requests.rs" in result.detail


def test_a_floor_is_fine_and_is_reported(tmp_path) -> None:
    result = probe(tmp_path, "pip install 'code-coordinator[server]>=0.4.10'")
    assert result.severity is Severity.OK
    assert "floor 0.4.10" in result.headroom


def test_no_coord_install_at_all_warns(tmp_path) -> None:
    result = probe(tmp_path, None)
    assert result.severity is Severity.WARN
    assert "installs no coord" in result.headroom
    assert "cannot import" in result.detail


def test_the_tombstone_distribution_is_crit(tmp_path) -> None:
    """`claude-coordinator` can never gain another release (#2106) — a gate
    pinned to it stays green through every change made here, forever."""
    result = probe(tmp_path, "pip install 'claude-coordinator[server]'")
    assert result.severity is Severity.CRIT
    assert "dead distribution" in result.headroom


def test_a_client_only_coord_is_crit(tmp_path) -> None:
    """`codegen.py --rust` imports `coord.serve_app`, i.e. Starlette (#1237)."""
    result = probe(tmp_path, "pip install code-coordinator")
    assert result.severity is Severity.CRIT
    assert "client-only" in result.headroom
    assert "ModuleNotFoundError" in result.detail


def test_an_exact_pin_warns(tmp_path) -> None:
    result = probe(tmp_path, "pip install 'code-coordinator[server]==0.4.90'")
    assert result.severity is Severity.WARN
    assert "exact-pins" in result.headroom


def test_a_compatible_release_pin_counts_as_exact(tmp_path) -> None:
    """`~=0.4.90` freezes the minor series just as effectively on a 0.x line."""
    result = probe(tmp_path, "pip install 'code-coordinator[server]~=0.4.90'")
    assert result.severity is Severity.WARN
    assert "exact-pins" in result.headroom


def test_a_floor_ahead_of_this_machine_warns(tmp_path) -> None:
    result = probe(tmp_path, "pip install 'code-coordinator[server]>=9.9.9'")
    assert result.severity is Severity.WARN
    assert "ahead of this machine" in result.headroom


def test_an_unknown_local_version_never_fabricates_a_floor_finding(
    tmp_path, monkeypatch
) -> None:
    """A source checkout reports `0+unknown`; grading every floor as "ahead"
    off that is how a check earns the right to be ignored."""
    monkeypatch.setattr(ctp, "LOCAL_COORD_VERSION", "0+unknown")
    result = probe(tmp_path, "pip install 'code-coordinator[server]>=9.9.9'")
    assert result.severity is Severity.OK


# ── the gate half: the finding this check exists for ─────────────────────────


def test_a_missing_drift_step_warns_even_with_a_perfect_pin(tmp_path) -> None:
    """The silent failure: CI installs a flawless coord and checks nothing."""
    result = probe(tmp_path, GOOD_INSTALL, drift_line=None)
    assert result.severity is Severity.WARN
    assert "no codegen drift gate" in result.headroom
    # The finding has to say what would go undetected, not just that a step
    # is missing.
    assert "board_schema.py" in result.detail
    assert result.values["drift_gate_steps"] == []


def test_regenerating_without_check_is_not_a_gate(tmp_path) -> None:
    """`--rust` alone rewrites the file and exits 0 — green while drifting."""
    result = probe(
        tmp_path,
        GOOD_INSTALL,
        drift_line="COORD_TUI_SRC=. python scripts/codegen.py --rust",
    )
    assert result.severity is Severity.WARN
    assert "no codegen drift gate" in result.headroom


def test_the_gate_is_found_regardless_of_env_prefix_or_interpreter(tmp_path) -> None:
    """The workflow may change how it invokes the generator; what it may not
    drop is any of `codegen.py`, `--rust`, `--check`."""
    result = probe(
        tmp_path,
        GOOD_INSTALL,
        drift_line="python3 .coord-upstream/scripts/codegen.py --rust --check",
    )
    assert result.severity is Severity.OK
    assert result.values["drift_gate_steps"] == ["codegen-drift.yml:drift"]


def test_a_broken_install_outranks_a_present_gate(tmp_path) -> None:
    """A gate that cannot import the generator is not a working gate; the
    CRIT names the cause rather than the symptom."""
    result = probe(tmp_path, "pip install code-coordinator", drift_line=DRIFT_STEP)
    assert result.severity is Severity.CRIT


def test_a_gate_step_in_a_comment_does_not_count(tmp_path) -> None:
    """Grading prose is how a check starts lying. Only `run:` scripts count,
    and `iter_run_steps` never yields a YAML comment."""
    root = make_coord_tui(tmp_path, GOOD_INSTALL, drift_line=None)
    wf = root / ".github" / "workflows" / "codegen-drift.yml"
    wf.write_text(
        wf.read_text() + "      # python scripts/codegen.py --rust --check\n",
        encoding="utf-8",
    )
    result = ctp.probe_coord_tui_ci_pin(
        make_ctx(tmp_path, checkouts=(Checkout(name="coord-tui", path=root),))
    )
    assert result.severity is Severity.WARN
    assert "no codegen drift gate" in result.headroom


# ── discovery ────────────────────────────────────────────────────────────────


def test_a_configured_checkout_wins_over_discovery(tmp_path) -> None:
    configured = make_coord_tui(tmp_path, GOOD_INSTALL, name="configured")
    decoy = make_coord_tui(tmp_path, "pip install claude-coordinator", name="coord-tui")
    result = ctp.probe_coord_tui_ci_pin(
        make_ctx(
            tmp_path,
            thresholds=HealthConfig(coord_tui_checkout=str(configured)),
            checkouts=(Checkout(name="coord-tui", path=decoy),),
        )
    )
    assert result.severity is Severity.OK
    assert result.values["checkout"] == str(configured)


def test_a_renamed_checkout_is_found_by_its_structural_marker(tmp_path) -> None:
    """An off lane is indistinguishable from a healthy one — so a rename must
    not silently turn this check off."""
    root = make_coord_tui(tmp_path, "pip install claude-coordinator", name="tui-fork")
    result = ctp.probe_coord_tui_ci_pin(
        make_ctx(tmp_path, checkouts=(Checkout(name="tui-fork", path=root),))
    )
    assert result.severity is Severity.CRIT
    assert result.values["checkout"] == str(root)


# ── the scaffold this repo actually ships ────────────────────────────────────


def test_the_shipped_coord_tui_scaffold_passes_its_own_check(tmp_path) -> None:
    """`scripts/coord-tui-scaffold/` is the CI this repo hands to coord-tui.

    If the template and the check that grades it drift apart, the check is
    measuring a shape nobody deploys. Graded here against the real files.
    """
    result = ctp.probe_coord_tui_ci_pin(
        make_ctx(
            tmp_path,
            thresholds=HealthConfig(coord_tui_checkout=str(SCAFFOLD)),
        )
    )
    assert result.severity is Severity.OK, (
        f"the shipped scaffold does not satisfy coord_tui_ci_pin: "
        f"{result.headroom} / {result.detail}"
    )
    assert "code-coordinator[server]" in result.headroom
    assert result.values["drift_gate_steps"], "scaffold lost its --rust --check step"


def test_find_drift_gate_steps_deduplicates_per_job(tmp_path) -> None:
    """Two invocations in one job are one gate, not two findings' worth."""
    root = make_coord_tui(tmp_path, GOOD_INSTALL, drift_line=None)
    wf = root / ".github" / "workflows" / "codegen-drift.yml"
    wf.write_text(
        wf.read_text()
        + "      - run: |\n"
        + "          python scripts/codegen.py --rust --check\n"
        + "          python scripts/codegen.py --rust --check\n",
        encoding="utf-8",
    )
    assert ctp.find_drift_gate_steps(root) == [("codegen-drift.yml", "drift")]
