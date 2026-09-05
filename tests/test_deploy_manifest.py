"""`coord/deploy_manifest.py` vs. its two other copies (#2098).

The manifest exists so "which units does this host run" lives in exactly
one place a lost machine can be rebuilt from. That guarantee is only as
good as its consistency with the two things that must agree with it:

* `docs/AGENT_OPERATIONS.md`'s "Daemon-host unit inventory" table — the
  human-facing transcription. If it drifts from `ROLE_UNITS`, the doc goes
  back to being folklore that happens to also exist in code, which is
  exactly the trap #2098 closed for `coord-release-propagate.timer`.
* `deploy/` (and its packaged mirror `coord/deploy/`) — every unit named by
  the manifest must actually exist as a shippable unit file, or the
  manifest names a unit nobody can ever install.
"""

from __future__ import annotations

import re
from pathlib import Path

from coord.deploy_manifest import (
    ROLE_DAEMON,
    ROLE_ENV_VAR,
    ROLE_UNITS,
    ROLE_WORKER,
    RoleDeclaration,
    all_manifest_units,
    resolve_role,
    units_for_role,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "AGENT_OPERATIONS.md"
DEPLOY_DIR = REPO_ROOT / "deploy"

_DOC_SECTION_RE = re.compile(
    r"## Daemon-host unit inventory.*?\n(.*?)\n## ", re.DOTALL
)
_DOC_ROW_RE = re.compile(r"^\|\s*`([\w.-]+)`\s*\|\s*([\w\s]+?)\s*\|", re.MULTILINE)


def _doc_table_units() -> dict[str, str]:
    """{unit_name: role_text} as transcribed in the doc's inventory table."""
    text = DOC_PATH.read_text(encoding="utf-8")
    section = _DOC_SECTION_RE.search(text)
    assert section, "docs/AGENT_OPERATIONS.md's 'Daemon-host unit inventory' section moved or was renamed"
    return dict(_DOC_ROW_RE.findall(section.group(1)))


def test_manifest_has_worker_and_daemon_roles() -> None:
    assert ROLE_WORKER in ROLE_UNITS
    assert ROLE_DAEMON in ROLE_UNITS
    assert ROLE_UNITS[ROLE_WORKER]
    assert ROLE_UNITS[ROLE_DAEMON]


def test_daemon_role_is_a_superset_of_worker_role() -> None:
    # Every machine, including the daemon host, runs coord-agent.
    assert set(units_for_role(ROLE_WORKER)) <= set(units_for_role(ROLE_DAEMON))


def test_all_manifest_units_is_deduped_and_sorted() -> None:
    units = all_manifest_units()
    assert list(units) == sorted(set(units))
    assert len(units) == len(set(units))


def test_all_manifest_units_covers_every_role() -> None:
    everything = set(all_manifest_units())
    for role, units in ROLE_UNITS.items():
        assert set(units) <= everything, f"role {role!r} names a unit missing from all_manifest_units()"


def test_manifest_units_exist_in_deploy_dir() -> None:
    """Every manifest-named unit is a real file `deploy/` can ship (#2098
    item 1) — a manifest entry with no backing file would tell an operator
    to enable something that was never installed."""
    for name in all_manifest_units():
        assert (DEPLOY_DIR / name).is_file(), f"{name} is in ROLE_UNITS but missing from deploy/"


def test_doc_table_matches_manifest() -> None:
    """docs/AGENT_OPERATIONS.md's inventory table names exactly the units
    `all_manifest_units()` does — the doc is a transcription, not an
    independent source, and must not silently diverge from it."""
    doc_units = set(_doc_table_units())
    assert doc_units == set(all_manifest_units())


def test_dr_verify_timer_is_registered_on_the_daemon_host() -> None:
    """#3119: the DR-verify lane is only worth building if a rebuilt daemon
    host is *told* it exists.

    The unit itself is the easy half; being in the manifest (and, through the
    cross-checks above, in the doc table and `deploy/`) is what stops it from
    becoming the folklore #2098 exists to kill — and what makes
    `unit_enablement` WARN when it is installed but not enabled, which is the
    exact state that hid the propagate timer.
    """
    assert "coord-dr-verify.timer" in units_for_role(ROLE_DAEMON)
    assert "coord-dr-verify.timer" not in units_for_role(ROLE_WORKER)
    # The timer, never the oneshot it fires: enabling the .service wires
    # nothing into timers.target.wants/ (see deploy_manifest's docstring).
    assert "coord-dr-verify.service" not in all_manifest_units()


def test_doc_table_roles_match_manifest() -> None:
    doc_roles = _doc_table_units()
    for name, role_text in doc_roles.items():
        if "every machine" in role_text:
            assert name in units_for_role(ROLE_WORKER)
            assert name in units_for_role(ROLE_DAEMON)
        elif "daemon host" in role_text:
            assert name in units_for_role(ROLE_DAEMON)
            assert name not in units_for_role(ROLE_WORKER)
        else:  # pragma: no cover - guard against a role spelling this test doesn't know
            raise AssertionError(f"unrecognized role text for {name!r}: {role_text!r}")


# ── resolve_role (#3128) ─────────────────────────────────────────────────
#
# `unit_enablement` is the only consumer, and per that module's own tests it
# never re-reads `~/.coord/role`/`COORD_ROLE` itself — so the file/env
# parsing contract lives entirely here.


def test_resolve_role_defaults_to_worker_when_nothing_declared(tmp_path) -> None:
    """No file, no env var: the safe majority, and `declared=False` is what
    lets `unit_enablement` stay byte-identical to pre-#3128 behaviour."""
    result = resolve_role(tmp_path / ".coord", env={})
    assert result == RoleDeclaration(role=ROLE_WORKER, raw=None, source="default", valid=True)
    assert result.declared is False


def test_resolve_role_reads_the_coord_dir_role_file(tmp_path) -> None:
    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    (coord_dir / "role").write_text("daemon\n")
    result = resolve_role(coord_dir, env={})
    assert result.role == ROLE_DAEMON
    assert result.raw == "daemon"
    assert result.source == "file"
    assert result.valid is True
    assert result.declared is True


def test_resolve_role_env_var_wins_over_file(tmp_path) -> None:
    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    (coord_dir / "role").write_text("daemon\n")
    result = resolve_role(coord_dir, env={ROLE_ENV_VAR: "worker"})
    assert result.role == ROLE_WORKER
    assert result.source == "env"
    assert result.valid is True
    assert result.declared is True


def test_resolve_role_is_case_insensitive_and_strips_whitespace(tmp_path) -> None:
    result = resolve_role(tmp_path / ".coord", env={ROLE_ENV_VAR: "  DAEMON  "})
    assert result.role == ROLE_DAEMON
    assert result.valid is True


def test_resolve_role_unknown_value_falls_back_to_worker_but_is_flagged_invalid(tmp_path) -> None:
    """#3128 acceptance: an unparseable/unknown role never raises and never
    silently reads as `daemon` — it falls back to `worker` with `valid=False`
    so `unit_enablement` can still raise the fact as a WARN."""
    result = resolve_role(tmp_path / ".coord", env={ROLE_ENV_VAR: "production"})
    assert result.role == ROLE_WORKER
    assert result.raw == "production"
    assert result.valid is False
    assert result.declared is True


def test_resolve_role_missing_coord_dir_does_not_raise(tmp_path) -> None:
    """`<coord_dir>/role` under a `coord_dir` that doesn't exist at all must
    read the same as "nothing declared" (#3117 is precisely the scenario
    where a fresh/rebuilt host has no `~/.coord/` yet)."""
    result = resolve_role(tmp_path / "does-not-exist", env={})
    assert result == RoleDeclaration(role=ROLE_WORKER, raw=None, source="default", valid=True)


def test_resolve_role_blank_file_reads_as_undeclared(tmp_path) -> None:
    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    (coord_dir / "role").write_text("   \n")
    result = resolve_role(coord_dir, env={})
    assert result.declared is False
    assert result.role == ROLE_WORKER


def test_resolve_role_result_is_always_a_valid_role_key() -> None:
    """Every `RoleDeclaration.role` this function can return must be usable
    directly as `units_for_role(result.role)` without the caller
    re-validating it first — including the `valid=False` fallback case."""
    for role in (ROLE_WORKER, ROLE_DAEMON, "worker", "bogus", ""):
        result = resolve_role(Path("/nonexistent"), env={ROLE_ENV_VAR: role} if role else {})
        assert result.role in ROLE_UNITS
