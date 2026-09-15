"""Registry mechanics for the fleet-health engine (#1628).

The load-bearing test here is
:func:`test_adding_a_check_touches_only_its_own_module` — the acceptance bar
for the abstraction, not a nice-to-have.  If registering a check ever starts
requiring an edit to the renderer, the CLI, or the registry itself, this
fails, and the whole reason H-1 exists (settle the registry shape before
anything else in the milestone starts) has been quietly given up.
"""

from __future__ import annotations

import hashlib
import importlib
import time
from pathlib import Path

import pytest

from coord.config import HealthConfig
from coord.health import models, registry
from coord.health.models import CheckResult, HealthContext, Severity, worst
from coord.health.registry import Check, HealthReport, run_all, run_check


@pytest.fixture
def ctx(tmp_path: Path) -> HealthContext:
    return HealthContext(
        thresholds=HealthConfig(),
        home=tmp_path,
        coord_dir=tmp_path / ".coord",
        now=1_800_000_000.0,
    )


@pytest.fixture
def isolated_registry(monkeypatch):
    """Swap in an empty registry so a test's checks don't leak into others."""
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_discovered", True)
    return registry


# ── severity ordering ────────────────────────────────────────────────────────


def test_unknown_ranks_above_ok_and_below_warn() -> None:
    """A probe that could not run is not a clean bill of health.

    It has to surface (above OK) without paging (below WARN) — treating it
    as OK is how a silently-broken check becomes indistinguishable from a
    healthy machine.
    """
    assert Severity.OK.rank < Severity.UNKNOWN.rank < Severity.WARN.rank < Severity.CRIT.rank
    assert worst([Severity.OK, Severity.UNKNOWN]) is Severity.UNKNOWN
    assert worst([Severity.UNKNOWN, Severity.WARN]) is Severity.WARN
    assert worst([]) is Severity.OK


def test_severity_labels() -> None:
    assert Severity.OK.label == "OK"
    assert Severity.WARN.label == "WARN"
    assert Severity.CRIT.label == "CRIT"
    assert Severity.UNKNOWN.label == "?"


# ── CheckResult contract ─────────────────────────────────────────────────────


def test_result_key_and_label() -> None:
    with_subject = CheckResult(
        check_id="disk", scope="machine", severity=Severity.OK,
        headroom="ok", title="disk", subject="/home",
    )
    assert with_subject.key == "disk:/home"
    assert with_subject.label == "disk /home"

    singleton = CheckResult(
        check_id="claude_binary", scope="machine", severity=Severity.OK,
        headroom="ok", title="claude binary",
    )
    assert singleton.key == "claude_binary"
    assert singleton.label == "claude binary"


def test_result_to_dict_carries_rendered_headroom_and_raw_values() -> None:
    """The contract H-3/H-4 consume: raw values AND a rendered headroom.

    Both must be present — raw-only forces every renderer to re-derive
    severity (the fork this design exists to prevent), rendered-only leaves
    machine consumers nothing to trend.
    """
    result = CheckResult(
        check_id="disk", scope="machine", subject="/home", title="disk",
        severity=Severity.WARN, headroom="86% used (22G free)",
        threshold="crit at 93%", values={"free_pct": 14.0},
    )
    payload = result.to_dict()
    assert payload["severity"] == "warn"
    assert payload["headroom"] == "86% used (22G free)"
    assert payload["threshold"] == "crit at 93%"
    assert payload["values"]["free_pct"] == 14.0
    assert payload["key"] == "disk:/home"


def test_result_values_are_copied_not_aliased() -> None:
    values = {"a": 1}
    result = CheckResult(
        check_id="x", scope="machine", severity=Severity.OK, headroom="", values=values
    )
    result.to_dict()["values"]["a"] = 999
    assert values == {"a": 1}


# ── registration + validation ────────────────────────────────────────────────


def test_check_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError, match="scope"):
        Check(id="x", scope="galaxy", probe=lambda ctx: None)


def test_check_rejects_unknown_cost() -> None:
    with pytest.raises(ValueError, match="cost"):
        Check(id="x", scope="machine", probe=lambda ctx: None, cost="expensive")


def test_fleet_scope_is_registrable_even_though_no_seed_probe_uses_it(
    isolated_registry, ctx
) -> None:
    """H-3's probes must not need a registry change to land."""
    isolated_registry.register(
        Check(
            id="fleet_thing",
            scope="fleet",
            probe=lambda c: CheckResult(
                check_id="fleet_thing", scope="fleet",
                severity=Severity.OK, headroom="fine",
            ),
        )
    )
    assert run_all(ctx, scopes=("fleet",)).results[0].check_id == "fleet_thing"
    # ...and --local (machine+checkout) excludes it.
    assert run_all(ctx, scopes=("machine", "checkout")).results == []


# ── fail-soft ────────────────────────────────────────────────────────────────


def test_raising_probe_becomes_unknown_and_does_not_kill_the_run(
    isolated_registry, ctx
) -> None:
    """A health engine that dies on its weakest check reports nothing."""

    def _boom(_ctx):
        raise RuntimeError("disk went away")

    isolated_registry.register(Check(id="boom", scope="machine", probe=_boom, order=1))
    isolated_registry.register(
        Check(
            id="fine",
            scope="machine",
            order=2,
            probe=lambda c: CheckResult(
                check_id="fine", scope="machine", severity=Severity.OK, headroom="fine"
            ),
        )
    )

    report = run_all(ctx)
    assert [r.check_id for r in report.results] == ["boom", "fine"]
    broken = report.results[0]
    assert broken.severity is Severity.UNKNOWN
    assert "disk went away" in (broken.error or "")
    assert "disk went away" in broken.headroom
    # The healthy check still ran, and the run's severity is UNKNOWN not CRIT.
    assert report.severity is Severity.UNKNOWN


def test_probe_returning_none_produces_no_rows(isolated_registry, ctx) -> None:
    isolated_registry.register(Check(id="quiet", scope="machine", probe=lambda c: None))
    assert run_all(ctx).results == []


def test_probe_result_inherits_the_checks_title(isolated_registry, ctx) -> None:
    """A probe that omits ``title`` still renders with a human label."""
    isolated_registry.register(
        Check(
            id="cargo_targets",
            scope="machine",
            title="cargo targets",
            probe=lambda c: CheckResult(
                check_id="cargo_targets", scope="machine",
                severity=Severity.OK, headroom="29G",
            ),
        )
    )
    assert run_all(ctx).results[0].label == "cargo targets"


# ── filtering ────────────────────────────────────────────────────────────────


def test_network_probes_are_skipped_and_recorded_not_silently_dropped(
    isolated_registry, ctx
) -> None:
    """"We didn't look" must never render as "nothing wrong"."""
    isolated_registry.register(
        Check(
            id="net",
            scope="machine",
            cost=registry.COST_NETWORK,
            probe=lambda c: pytest.fail("network probe ran with allow_network=False"),
        )
    )
    ctx.allow_network = False
    report = run_all(ctx)
    assert report.results == []
    assert report.skipped == ["net (network probe, --no-network)"]
    assert "net" in report.to_dict()["skipped"][0]


def test_disabled_checks_config_is_honoured_and_recorded(isolated_registry, ctx) -> None:
    isolated_registry.register(
        Check(id="noisy", scope="machine",
              probe=lambda c: pytest.fail("disabled check ran"))
    )
    ctx.thresholds.disabled_checks = ["noisy"]
    report = run_all(ctx)
    assert report.results == []
    assert "noisy" in report.skipped[0]


def test_only_filter_restricts_to_named_ids(isolated_registry, ctx) -> None:
    for name in ("a", "b"):
        isolated_registry.register(
            Check(
                id=name,
                scope="machine",
                probe=lambda c, n=name: CheckResult(
                    check_id=n, scope="machine", severity=Severity.OK, headroom=""
                ),
            )
        )
    assert [r.check_id for r in run_all(ctx, only=["b"]).results] == ["b"]


def test_checks_run_in_declared_order(isolated_registry, ctx) -> None:
    for name, order in (("late", 90), ("early", 10)):
        isolated_registry.register(
            Check(
                id=name,
                scope="machine",
                order=order,
                probe=lambda c, n=name: CheckResult(
                    check_id=n, scope="machine", severity=Severity.OK, headroom=""
                ),
            )
        )
    assert [r.check_id for r in run_all(ctx).results] == ["early", "late"]


# ── per-check timing (#3344) ─────────────────────────────────────────────────


def test_run_all_records_a_duration_for_every_check_that_ran(
    isolated_registry, ctx
) -> None:
    """#3344: `/health`'s `health_timing_ms` broke down the outer handler
    into sections, but `local_health` — one of those sections — was itself
    a single opaque block. This is the registry half of un-opaquing it:
    every check that actually runs gets its own wall-clock entry, not just
    the run's total."""
    isolated_registry.register(
        Check(
            id="fast",
            scope="machine",
            probe=lambda c: CheckResult(
                check_id="fast", scope="machine", severity=Severity.OK, headroom=""
            ),
        )
    )

    def _slow(_ctx: HealthContext) -> CheckResult:
        time.sleep(0.05)
        return CheckResult(
            check_id="slow", scope="machine", severity=Severity.OK, headroom=""
        )

    isolated_registry.register(Check(id="slow", scope="machine", probe=_slow))

    report = run_all(ctx)
    assert set(report.check_durations_ms) == {"fast", "slow"}
    for ms in report.check_durations_ms.values():
        assert ms >= 0
    # The slow probe's own sleep must show up on ITS entry, not blurred into
    # the total or attributed to its neighbour.
    assert report.check_durations_ms["slow"] >= 45  # 50ms sleep, slack for jitter
    assert report.check_durations_ms["fast"] < report.check_durations_ms["slow"]


def test_check_durations_exclude_skipped_and_disabled_checks(
    isolated_registry, ctx
) -> None:
    """A check that never ran (network-skipped, operator-disabled) must not
    get a fabricated 0ms entry — that would read as "ran instantly" instead
    of "didn't run", the same distinction `skipped` already protects for
    `results`."""
    isolated_registry.register(
        Check(
            id="net",
            scope="machine",
            cost=registry.COST_NETWORK,
            probe=lambda c: pytest.fail("should be skipped"),
        )
    )
    isolated_registry.register(
        Check(id="disabled", scope="machine", probe=lambda c: pytest.fail("should be skipped"))
    )
    ctx.allow_network = False
    ctx.thresholds.disabled_checks = ["disabled"]
    report = run_all(ctx)
    assert report.check_durations_ms == {}


def test_check_durations_ms_survives_a_raising_probe(isolated_registry, ctx) -> None:
    """`run_check` converts a raised probe into an `unknown` result rather
    than aborting the run (see the fail-soft test above) — the timing
    entry must follow the same contract: the probe still cost wall time,
    even though it produced no useful data."""

    def _boom(_ctx: HealthContext) -> CheckResult:
        raise RuntimeError("disk went away")

    isolated_registry.register(Check(id="boom", scope="machine", probe=_boom))
    report = run_all(ctx)
    assert "boom" in report.check_durations_ms
    assert report.check_durations_ms["boom"] >= 0


def test_check_durations_ms_in_to_dict(isolated_registry, ctx) -> None:
    isolated_registry.register(
        Check(
            id="a",
            scope="machine",
            probe=lambda c: CheckResult(
                check_id="a", scope="machine", severity=Severity.OK, headroom=""
            ),
        )
    )
    body = run_all(ctx).to_dict()
    assert "check_durations_ms" in body
    assert set(body["check_durations_ms"]) == {"a"}
    assert isinstance(body["check_durations_ms"]["a"], (int, float))


# ── report aggregation ───────────────────────────────────────────────────────


def test_report_severity_is_the_worst_result() -> None:
    report = HealthReport(
        results=[
            CheckResult(check_id="a", scope="machine", severity=Severity.OK, headroom=""),
            CheckResult(check_id="b", scope="machine", severity=Severity.CRIT, headroom=""),
            CheckResult(check_id="c", scope="machine", severity=Severity.WARN, headroom=""),
        ]
    )
    assert report.severity is Severity.CRIT
    assert report.counts() == {"ok": 1, "warn": 1, "crit": 1, "unknown": 0}


# ── the acceptance bar ───────────────────────────────────────────────────────

_UNTOUCHABLE = (
    "coord/health/render.py",
    "coord/health/cli.py",
    "coord/health/registry.py",
    "coord/health/models.py",
    "coord/health/checks/__init__.py",
    "coord/cli.py",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _hashes() -> dict[str, str]:
    root = _repo_root()
    return {
        rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in _UNTOUCHABLE
    }


def test_adding_a_check_touches_only_its_own_module(tmp_path: Path) -> None:
    """Drop one file into ``coord/health/checks/`` and it is a live check.

    No renderer edit, no CLI edit, no registry edit, not even an ``__init__``
    import — that is the acceptance bar for this abstraction (the issue's
    words: "the acceptance bar for the abstraction, not a nice-to-have"),
    and this test is what holds it.
    """
    checks_dir = _repo_root() / "coord" / "health" / "checks"
    module_name = "zz_probe_added_by_test"
    module_path = checks_dir / f"{module_name}.py"
    assert not module_path.exists(), "leftover fixture module from a previous run"

    before = _hashes()
    module_path.write_text(
        '"""Throwaway check written by tests/test_health_registry.py."""\n'
        "from coord.health.models import CheckResult, Severity\n"
        "from coord.health.registry import check\n"
        "\n"
        '@check(id="zz_added_by_test", scope="machine", title="added by test", order=999)\n'
        "def probe(ctx):\n"
        "    return CheckResult(\n"
        '        check_id="zz_added_by_test", scope="machine",\n'
        "        severity=Severity.WARN,\n"
        '        headroom="42 widgets left", threshold="crit at 0",\n'
        '        values={"widgets": 42},\n'
        "    )\n",
        encoding="utf-8",
    )
    try:
        registry.discover(force=True)

        # 1. It is registered, with no central list edited.
        added = registry.get("zz_added_by_test")
        assert added is not None
        assert added.title == "added by test"

        # 2. It flows through the runner...
        ctx = HealthContext(
            thresholds=HealthConfig(),
            home=tmp_path,
            coord_dir=tmp_path / ".coord",
            now=0.0,
        )
        results = run_check(added, ctx)
        assert results[0].headroom == "42 widgets left"

        # 3. ...the text renderer...
        from coord.health.render import render_result

        line = render_result(results[0])
        assert "added by test" in line
        assert "WARN" in line
        assert "42 widgets left" in line

        # 4. ...and the JSON contract, with no per-check knowledge anywhere.
        payload = HealthReport(results=results).to_dict()
        assert payload["results"][0]["values"] == {"widgets": 42}
        assert payload["severity"] == "warn"

        # 5. And nothing else on disk changed to make that work.
        assert _hashes() == before
    finally:
        module_path.unlink(missing_ok=True)
        registry._REGISTRY.pop("zz_added_by_test", None)
        registry.discover(force=True)


def test_checks_package_init_imports_no_check_modules() -> None:
    """Discovery is pkgutil, not a hand-maintained import list.

    A single ``from . import disk`` here would be the thin end of exactly the
    central registration this design rejects, so assert the file has no
    imports at all.
    """
    source = (_repo_root() / "coord" / "health" / "checks" / "__init__.py").read_text()
    code_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Everything outside the module docstring should be nothing.
    module = importlib.import_module("coord.health.checks")
    assert module.__doc__, "the __init__ should explain why it is empty"
    assert not any(
        line.startswith(("import ", "from ")) for line in code_lines
    ), f"coord/health/checks/__init__.py must not import check modules: {code_lines}"


def _code_without_docstrings(path: Path) -> str:
    """*path*'s source with every docstring blanked out.

    The prose in these modules explains the very rules being asserted (and so
    legitimately contains the strings we're grepping for); only executable
    code is evidence of a violation.
    """
    import ast

    source = path.read_text()
    tree = ast.parse(source)
    doc_spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            doc_spans.append((first.lineno, first.end_lineno or first.lineno))
    blanked = set()
    for start, end in doc_spans:
        blanked.update(range(start, end + 1))
    return "\n".join(
        "" if n in blanked else line for n, line in enumerate(source.splitlines(), start=1)
    )


def test_renderer_never_branches_on_a_check_id() -> None:
    """The renderer is layout-only; a check-id special case is the fork.

    Grep-level, deliberately: the moment ``render.py`` says ``if check_id ==
    "disk"``, severity logic has started leaking out of the probes and every
    future surface gets to disagree about what WARN means.
    """
    source = _code_without_docstrings(_repo_root() / "coord" / "health" / "render.py")
    for check_obj in registry.all_checks():
        assert f'"{check_obj.id}"' not in source, (
            f"render.py mentions check id {check_obj.id!r} — renderers must not "
            f"know what any individual check measures"
        )
    # It also must not read raw values, which is where re-derivation starts.
    assert ".values" not in source


def test_models_module_has_no_dependency_on_a_renderer() -> None:
    """Probes import ``units``; nothing in the core imports ``render``."""
    for module in (models, registry):
        source = Path(module.__file__).read_text()
        assert "health.render" not in source
        assert "health.cli" not in source
